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

    def rclick(self, point, drag_to=None):
        """Right button press and release at an image point; with drag_to, move there while held."""
        x, y = self.position(point)
        self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        self.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="right", buttons=2, clickCount=1)
        if drag_to is not None:
            x, y = self.position(drag_to)
            self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="right", buttons=2)
        self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="right", buttons=0, clickCount=1)

    def key(self, key, kind="keydown"):
        self.evaluate(f"document.getElementById('an-stage').dispatchEvent(new KeyboardEvent('{kind}', {{key: {json.dumps(key)}, bubbles: true}}))")

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
            page.call("Page.addScriptToEvaluateOnNewDocument",
                      source="localStorage.setItem('emqc.annotator', 'browser-test')")
            page.call("Network.enable")
            page.call("Fetch.enable", patterns=[{"urlPattern": "*", "requestStage": "Request"}])
            page.call("Page.navigate", url=f"http://127.0.0.1:{port}/annotate?block=pairs")
            page.wait("document.querySelector('#an-meta')?.textContent.includes('64×32×2') && document.querySelectorAll('#an-segs .row').length >= 4")
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
    page, data, original = browser
    work = data.parent.parent / "work" / "pairs"
    # Paint another label into the stroke's path through the UI before switching colours.
    page.move(POINTS[0], click=True)
    page.wait(f"document.getElementById('an-cur-id').textContent === '{IDS[0]}'")
    page.evaluate("""{ document.querySelector('[data-tool=brush]').click();
        const radius = document.getElementById('an-brush');
        radius.value = 0; radius.dispatchEvent(new Event('input')); }""")
    page.move((12, 4), click=True)
    page.edits(1)
    before = original.copy()
    before[12, 4, 0] = IDS[0]
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


MENU_OPEN = "!document.getElementById('an-ctx').hidden"
FOLDS = "['an-seed-panel', 'an-bf-panel', 'an-ng-panel'].map(id => document.getElementById(id).open)"


def test_left_panel_folds_and_block_info_in_tooltip(browser):
    page, data, original = browser
    assert page.evaluate(FOLDS) == [False, False, False]
    assert page.evaluate("document.getElementById('an-meta').hidden") is True
    assert "64×32×2" in page.evaluate("document.getElementById('an-block').title")
    # Choosing a tool that lives in a folded section opens it, by button or by shortcut.
    page.evaluate("document.querySelector('[data-tool=seed-box]').click()")
    page.wait("document.getElementById('an-seed-panel').open")
    page.key("w")
    page.wait("document.getElementById('an-bf-panel').open && document.querySelector('[data-tool=bigfill]').getAttribute('aria-pressed') === 'true'")
    # Folding the section of the active tool leaves that tool, like the SAM panel does.
    page.evaluate("document.querySelector('#an-bf-panel > summary').click()")
    page.wait("!document.getElementById('an-bf-panel').open && document.querySelector('[data-tool=pick]').getAttribute('aria-pressed') === 'true'")
    page.evaluate("document.querySelector('#an-ng-panel > summary').click()")
    page.wait("document.getElementById('an-ng-panel').open")
    # Only the annotator's own clicks are remembered; the automatic opening of the seed section is not.
    assert page.evaluate("JSON.parse(localStorage.getItem('emqc.annotate.folds'))") == {"an-bf-panel": False, "an-ng-panel": True}
    page.evaluate("window.beforeReload = true")
    page.call("Page.reload")
    page.wait("!window.beforeReload && document.querySelector('#an-meta')?.textContent.includes('64×32×2') && document.querySelectorAll('#an-segs .row').length >= 4")
    assert page.evaluate(FOLDS) == [False, False, True]


def test_right_click_menu_acts_on_the_block_under_the_cursor(browser):
    page, data, original = browser
    work = data.parent.parent / "work" / "pairs"
    page.rclick(POINTS[0])
    page.wait(MENU_OPEN)
    assert str(IDS[0]) in page.evaluate("document.getElementById('an-ctx-id').textContent")
    state = "Object.fromEntries([...document.querySelectorAll('#an-ctx [data-act]')].map(b => [b.dataset.act, !b.disabled]))"
    assert page.evaluate(state) == {"pick": True, "fill": False, "clear": True, "refine": True}  # no current label yet
    page.evaluate("document.querySelector('#an-ctx [data-act=pick]').click()")
    page.wait(f"document.getElementById('an-ctx').hidden && document.getElementById('an-cur-id').textContent === '{IDS[0]}'")
    # Fill another block with the current label.
    page.rclick(POINTS[2])
    page.wait(MENU_OPEN)
    assert str(IDS[0]) in page.evaluate("document.getElementById('an-ctx-fill').textContent")
    page.evaluate("document.querySelector('#an-ctx [data-act=fill]').click()")
    page.edits(1)
    expected = original.copy()
    expected[34:42, 2:10, 0] = IDS[0]
    assert np.array_equal(np.load(work / "seg_edit.npy"), expected)
    # Clear a block; the current label stays.
    page.rclick(POINTS[1])
    page.wait(MENU_OPEN)
    page.evaluate("document.querySelector('#an-ctx [data-act=clear]').click()")
    page.edits(2)
    expected[18:26, 2:10, 0] = 0
    assert np.array_equal(np.load(work / "seg_edit.npy"), expected)
    assert page.evaluate("document.getElementById('an-cur-id').textContent") == str(IDS[0])
    # On background only filling makes sense; Escape closes the menu without changing anything.
    page.rclick((12, 14))
    page.wait(MENU_OPEN)
    assert page.evaluate(state) == {"pick": False, "fill": True, "clear": False, "refine": False}
    page.key("Escape")
    page.wait("document.getElementById('an-ctx').hidden")
    # Keyboard: arrows move between enabled items, Enter runs one. Refine posts the clicked point.
    page.rclick(POINTS[3])
    page.wait(MENU_OPEN)
    assert page.evaluate("document.activeElement.dataset.act") == "pick"
    for _ in range(3):
        page.key("ArrowDown")
    assert page.evaluate("document.activeElement.dataset.act") == "refine"
    page.key("Enter")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        posted = [json.loads(e["params"]["request"]["postData"]) for e in page.events
                  if e.get("method") == "Fetch.requestPaused" and e["params"]["request"]["url"].endswith("/refine-edge")]
        if posted:
            break
        page.evaluate("1")
    assert posted and (posted[0]["x"], posted[0]["y"], posted[0]["z"]) == (*POINTS[3], 0)
    page.wait("!document.getElementById('an-undo').disabled")
    assert np.array_equal(np.load(data / "seg.npy"), original)


def test_right_drag_still_erases_and_pans(browser):
    page, data, original = browser
    work = data.parent.parent / "work" / "pairs"
    page.move(POINTS[0], click=True)
    page.wait(f"document.getElementById('an-cur-id').textContent === '{IDS[0]}'")
    page.evaluate("""{ document.querySelector('[data-tool=brush]').click();
        const radius = document.getElementById('an-brush');
        radius.value = 0; radius.dispatchEvent(new Event('input')); }""")
    # A right click without moving opens the menu and paints nothing.
    page.rclick(POINTS[0])
    page.wait(MENU_OPEN)
    page.key("Escape")
    page.wait("document.getElementById('an-ctx').hidden")
    assert page.evaluate("document.getElementById('an-nedit').textContent") == "0 次改动"
    # A right drag in the brush erases the current colour from the press point on, and opens no menu.
    page.rclick((4, 4), drag_to=(7, 4))
    page.edits(1)
    expected = original.copy()
    expected[4:8, 4, 0] = 0
    assert np.array_equal(np.load(work / "seg_edit.npy"), expected)
    assert page.evaluate("document.getElementById('an-ctx').hidden")
    # Other tools: a right drag pans; a right click opens the menu and leaves the view where it was.
    page.evaluate("document.querySelector('[data-tool=pick]').click()")
    transform = "document.querySelector('#an-stage .vast-canvas').style.transform"
    before = page.evaluate(transform)
    page.rclick(POINTS[2], drag_to=POINTS[3])
    moved = page.evaluate(transform)
    assert moved != before and page.evaluate("document.getElementById('an-ctx').hidden")
    page.rclick(POINTS[2])
    page.wait(MENU_OPEN)
    assert page.evaluate(transform) == moved
    page.key("Escape")
    page.wait("document.getElementById('an-ctx').hidden")
    assert np.array_equal(np.load(data / "seg.npy"), original)


def test_closing_the_menu_never_edits(browser):
    page, data, original = browser
    work = data.parent.parent / "work" / "pairs"
    page.move(POINTS[0], click=True)
    page.wait(f"document.getElementById('an-cur-id').textContent === '{IDS[0]}'")
    page.evaluate("document.querySelector('[data-tool=fill]').click()")
    settle = "new Promise(r => setTimeout(() => r(true), 400))"
    # Space is the pan key: it closes the menu and runs nothing, even with a write action focused.
    page.rclick(POINTS[2])
    page.wait(MENU_OPEN)
    page.key("Shift")                                        # a modifier on its own keeps the menu open
    assert page.evaluate(MENU_OPEN)
    page.evaluate("document.querySelector('#an-ctx [data-act=fill]').focus()")
    page.key(" ")
    page.key(" ", "keyup")
    page.wait("document.getElementById('an-ctx').hidden")
    # A left click on the image only closes the menu; the fill tool does not run. The menu opens to the right of
    # the cursor, so click a block to its left.
    page.rclick(POINTS[3])
    page.wait(MENU_OPEN)
    page.move(POINTS[2], click=True)
    page.wait("document.getElementById('an-ctx').hidden")
    page.evaluate(settle)
    assert page.evaluate("document.getElementById('an-nedit').textContent") == "0 次改动"
    assert not (work / "seg_edit.npy").exists() or np.array_equal(np.load(work / "seg_edit.npy"), original)
    # The next left click works as usual.
    page.move(POINTS[2], click=True)
    page.edits(1)
    expected = original.copy()
    expected[34:42, 2:10, 0] = IDS[0]
    assert np.array_equal(np.load(work / "seg_edit.npy"), expected)
