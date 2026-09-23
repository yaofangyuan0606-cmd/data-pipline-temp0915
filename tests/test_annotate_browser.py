"""Real Chromium interactions against an isolated server and synthetic label data.

Requires a local google-chrome/chromium and websockets (provided by uvicorn[standard]).
No browser downloads, user profiles or live annotation data are used.
CHROME_TMPDIR can select a shorter temporary path for Chromium's Unix sockets.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pytest

from annotation_data import IDS, POINTS, write_pairs


class Browser:
    def __init__(self, ws, base_url):
        self.ws, self.sequence, self.errors = ws, 0, []
        self.events = []
        self.base_url = base_url
        self.ignored = set()

    def relay(self, params):
        """Bridge local HTTP via the test runner when Chromium networking is restricted.

        The browser still runs the actual page and sends the real request bodies;
        every response comes from the isolated FastAPI server, not a mock.
        """
        request = params["request"]
        assert request["url"].startswith(self.base_url + "/"), request["url"]
        headers = {k: v for k, v in request["headers"].items() if k.lower() not in ("host", "content-length")}
        body = request.get("postData")
        req = Request(request["url"], data=body.encode() if body is not None else None, headers=headers, method=request["method"])
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            reply = {"requestId": params["requestId"], "responseCode": response.status,
                     "responseHeaders": [{"name": k, "value": v} for k, v in response.headers.items()
                                         if k.lower() not in ("content-length", "transfer-encoding")],
                     "body": base64.b64encode(response.read()).decode()}
        self.sequence += 1
        self.ignored.add(self.sequence)
        self.ws.send(json.dumps({"id": self.sequence, "method": "Fetch.fulfillRequest", "params": reply}))

    def call(self, method, **params):
        self.sequence += 1
        sequence = self.sequence
        self.ws.send(json.dumps({"id": sequence, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv(timeout=20))
            if message.get("id") in self.ignored:
                self.ignored.remove(message["id"])
                assert "error" not in message, message
                continue
            if "method" in message:
                self.events.append(message)
            if message.get("method") == "Fetch.requestPaused":
                self.relay(message["params"])
            if message.get("method") == "Runtime.exceptionThrown":
                self.errors.append(message["params"])
            if message.get("id") == sequence:
                assert "error" not in message, message
                return message.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    def wait(self, expression):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(.05)
        raise AssertionError("Browser condition timed out: " + expression)

    def position(self, point):
        return self.evaluate(f"""(() => {{
            const c = document.querySelector('#an-stage .vast-canvas'), r = c.getBoundingClientRect();
            const image = c.querySelector('canvas');
            return [r.left + ({point[0]} + .5) * r.width / image.width, r.top + ({point[1]} + .5) * r.height / image.height];
        }})()""")

    def move(self, point, click=False):
        x, y = self.position(point)
        self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        if click:
            self.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
            self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)

    def alpha(self, point):
        return f"document.querySelectorAll('#an-stage canvas')[2].getContext('2d').getImageData({point[0]},{point[1]},1,1).data[3]"

    def edits(self, n):
        self.wait(f"document.getElementById('an-nedit').textContent === '{n} 次改动' && !document.getElementById('an-undo').disabled")


@pytest.fixture
def browser(tmp_path):
    if os.environ.get("EMQC_BROWSER_TESTS") != "1":
        pytest.skip("Set EMQC_BROWSER_TESTS=1 to run local Chromium integration tests")
    chrome = os.environ.get("EMQC_BROWSER_BINARY") or shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        pytest.skip("Chromium is not installed")
    connect = pytest.importorskip("websockets.sync.client").connect
    root = Path(__file__).resolve().parents[1]
    data = tmp_path / "blocks" / "pairs"
    original = write_pairs(data)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, EMQC_DB_URL=f"sqlite:///{tmp_path / 'browser.db'}", EMQC_API_HOST="127.0.0.1",
               EMQC_ANNOTATE_ROOT=str(data.parent), EMQC_ANNOTATE_WORKDIR=str(tmp_path / "work"),
               EMQC_ANNOTATE_EXTRA_ROOTS="", EMQC_SAM_BLOCKS_DIR=str(tmp_path / "sam_blocks"), EMQC_PREVIEW_DIR=str(tmp_path / "previews"),
               EMQC_DATA_ROOT=str(tmp_path / "unused"), EMQC_REMOTE_ROOTS="", PYTHONDONTWRITEBYTECODE="1")
    profile = tmp_path / "chrome"
    processes = []
    ws = None
    with (tmp_path / "server.log").open("wb") as server_log, (tmp_path / "chrome.log").open("wb") as chrome_log:
        try:
            processes.append(subprocess.Popen([sys.executable, "-B", "scripts/serve.py", str(port)], cwd=root,
                                              env=env, stdout=server_log, stderr=subprocess.STDOUT))
            for _ in range(150):
                try:
                    with urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=1):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                pytest.fail((tmp_path / "server.log").read_text())
            processes.append(subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                                              "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                                              "--window-size=1400,1100", f"--user-data-dir={profile}",
                                              "--remote-debugging-port=0", "about:blank"],
                                              env=dict(os.environ, TMPDIR=os.environ.get("CHROME_TMPDIR", os.environ.get("TMPDIR", "/tmp"))),
                                              stdout=chrome_log, stderr=subprocess.STDOUT))
            for _ in range(150):
                if (profile / "DevToolsActivePort").exists():
                    break
                time.sleep(.1)
            else:
                pytest.fail((tmp_path / "chrome.log").read_text())
            debug_port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
            with urlopen(f"http://127.0.0.1:{debug_port}/json/list") as response:
                target = next(t for t in json.load(response) if t["type"] == "page")
            ws = connect(target["webSocketDebuggerUrl"], max_size=10_000_000)
            page = Browser(ws, f"http://127.0.0.1:{port}")
            page.call("Runtime.enable")
            page.call("Page.enable")
            page.call("Network.enable")
            page.call("Fetch.enable", patterns=[{"urlPattern": "*", "requestStage": "Request"}])
            page.call("Page.navigate", url=f"http://127.0.0.1:{port}/annotate?block=pairs")
            page.wait("document.querySelector('#an-meta')?.textContent.startsWith('64×32×2') && document.querySelectorAll('#an-segs .row').length >= 4")
            page.evaluate("document.getElementById('an-stage').scrollIntoView({block:'center'})")
            yield page, data, original
            assert not page.errors, page.errors
        finally:
            if ws:
                try:
                    screenshot = page.call("Page.captureScreenshot", format="png")
                    (tmp_path / "annotation-pairwise.png").write_bytes(base64.b64decode(screenshot["data"]))
                finally:
                    (tmp_path / "browser-events.json").write_text(json.dumps(page.events, indent=2))
                    ws.close()
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def test_hover_and_independent_merge_pairs_in_browser(browser):
    page, data, original = browser
    work = data.parent.parent / "work" / "pairs"
    a, b, c, d = POINTS
    page.move(a)
    page.wait(page.alpha(a) + ' > 0')
    assert page.evaluate(page.alpha((4, 20))) == 0
    assert page.evaluate(page.alpha((10, 10))) == 0
    # Move directly to a disconnected island of the same id: highlight must move.
    page.move((4, 20))
    page.wait(page.alpha((4, 20)) + ' > 0')
    assert page.evaluate(page.alpha(a)) == 0
    page.evaluate("document.querySelector('[data-tool=merge]').click()")
    page.move(a, click=True)
    page.move(b)
    page.wait(page.alpha(a) + ' > 0 && ' + page.alpha(b) + ' > 0')
    assert page.evaluate(page.alpha((4, 20))) == 0
    # Simulate B, C, D in the same event turn: only B may complete the pending pair.
    coords = [page.position(p) for p in (b, c, d)]
    page.evaluate(f"""{json.dumps(coords)}.forEach(([x,y]) => document.getElementById('an-stage').dispatchEvent(
        new MouseEvent('mousedown', {{clientX:x, clientY:y, button:0, bubbles:true}})))""")
    page.edits(1)
    expected = original.copy()
    expected[18:26, 2:10, 0] = IDS[0]
    assert np.array_equal(np.load(work / "seg_edit.npy"), expected)
    page.move(c, click=True)
    page.move(d, click=True)
    page.edits(2)
    expected[50:58, 2:10, 0] = IDS[2]
    assert np.array_equal(np.load(work / "seg_edit.npy"), expected)
    assert np.array_equal(np.load(data / "seg.npy"), original)
    # Same-colour pairs are consumed, rather than keeping an old first click armed.
    page.move(a, click=True)
    page.move(b, click=True)
    page.wait("!document.getElementById('an-undo').disabled && document.getElementById('an-merge-hint').textContent.startsWith('依次点两块')")
    page.edits(2)
    # Failed requests also clear the pair. A following C,D pair must still keep C.
    page.evaluate("""window.realFetch = window.fetch; window.failPair = true;
        window.fetch = (...args) => {
            if (window.failPair && String(args[0]).endsWith('/merge-pair')) {
                window.failPair = false; return Promise.reject(new Error('simulated failure'));
            } return window.realFetch(...args);
        };""")
    page.move(a, click=True)
    page.move(c, click=True)
    page.wait("!window.failPair && !document.getElementById('an-undo').disabled && document.getElementById('an-merge-hint').textContent.startsWith('依次点两块')")
    page.evaluate("window.fetch = window.realFetch; document.getElementById('an-undo').click()")
    page.edits(1)
    page.move(c, click=True)
    page.move(d, click=True)
    page.edits(2)
    assert np.array_equal(np.load(work / "seg_edit.npy"), expected)
    # Each undo restores one whole pair, with the earlier pair left intact.
    page.evaluate("document.getElementById('an-undo').click()")
    page.edits(1)
    page.evaluate("document.getElementById('an-undo').click()")
    page.edits(0)
    assert np.array_equal(np.load(work / "seg_edit.npy"), original)
    assert page.evaluate("document.querySelectorAll('[name=an-dir], [name=an-scope]').length") == 0


@pytest.mark.parametrize("view,outline", [("overlay", False), ("overlay", True), ("side", False), ("side", True)])
def test_brush_preview_and_saved_stroke_preserve_existing_labels(browser, view, outline):
    from emqc.annotate.labels import GAP_ID

    page, data, original = browser
    work = data.parent.parent / "work" / "pairs"
    # Put a gap label in the stroke's path through the UI, before switching to a cell colour.
    page.evaluate("""{ document.getElementById('an-gap').click();
        document.querySelector('[data-tool=brush]').click();
        const radius = document.getElementById('an-brush');
        radius.value = 0; radius.dispatchEvent(new Event('input')); }""")
    page.move((12, 4), click=True)
    page.edits(1)
    before = original.copy()
    before[12, 4, 0] = GAP_ID
    assert np.array_equal(np.load(work / "seg_edit.npy"), before)
    page.evaluate("document.querySelector('[data-tool=pick]').click()")
    page.move(POINTS[2], click=True)
    page.wait(f"document.getElementById('an-cur-id').textContent === '{IDS[2]}'")
    page.evaluate(f"document.querySelector('[name=an-view][value={view}]').click()")
    if outline:
        page.evaluate("document.getElementById('an-outline').click()")
    page.evaluate("""{ document.querySelector('[data-tool=brush]').click();
        const radius = document.getElementById('an-brush');
        radius.value = 4; radius.dispatchEvent(new Event('input'));
        document.getElementById('an-stage').scrollIntoView({block:'center'}); }""")
    stage = "an-stage2" if view == "side" else "an-stage"
    pixels = f"Array.from(document.querySelectorAll('#{stage} canvas')[1].getContext('2d').getImageData(0,0,64,32).data)"
    canvas_before = np.asarray(page.evaluate(pixels)).reshape(32, 64, 4)
    x, y = page.position(POINTS[0])
    page.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
    x, y = page.position(POINTS[1])
    page.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, buttons=1)
    preview = np.asarray(page.evaluate(pixels)).reshape(32, 64, 4)
    occupied = before[:, :, 0].T != 0
    assert np.array_equal(preview[occupied], canvas_before[occupied]), "preview must not colour any existing label"
    assert canvas_before[5, 12, 3] == 0 and preview[5, 12, 3] > 0, "background gets an immediate preview"
    page.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
    page.edits(2)
    edited = np.load(work / "seg_edit.npy")
    assert np.array_equal(edited[before != 0], before[before != 0])
    assert edited[12, 5, 0] == IDS[2]
    assert np.array_equal(edited[:, :, 1], original[:, :, 1])
    assert np.array_equal(np.load(data / "seg.npy"), original)
    page.evaluate("document.getElementById('an-undo').click()")
    page.edits(1)
    assert np.array_equal(np.load(work / "seg_edit.npy"), before)
