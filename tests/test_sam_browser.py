"""Optional real browser + GPU checks; all writes target isolated copies.

EMQC_PLAYWRIGHT_TESTS=1 enables the existing pairwise UI regression with Headless Shell.
EMQC_SAM_SMOKE_SOURCE points to a canonical YXZ block to additionally test real SAM.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import numpy as np
import pytest

from annotation_data import write_pairs
from test_annotate_browser import Browser, test_hover_and_independent_merge_pairs_in_browser as check_pairs


class PlaywrightBrowser(Browser):
    def __init__(self, session, base_url):
        self.session, self.base_url = session, base_url
        self.errors, self.events = [], []
        session.on("Runtime.exceptionThrown", lambda event: self.errors.append(event))

    def call(self, method, **params):
        return self.session.send(method, params)


@contextmanager
def browser_for(tmp_path, data):
    if os.environ.get("EMQC_PLAYWRIGHT_TESTS") != "1":
        pytest.skip("Set EMQC_PLAYWRIGHT_TESTS=1 for real browser checks")
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, EMQC_DB_URL=f"sqlite:///{tmp_path / 'test.db'}",
               EMQC_ANNOTATE_ROOT=str(data.parent), EMQC_ANNOTATE_WORKDIR=str(tmp_path / "work"),
               EMQC_ANNOTATE_EXTRA_ROOTS="", EMQC_SAM_BLOCKS_DIR=str(tmp_path / "sam-blocks"),
               EMQC_DATA_ROOT=str(tmp_path / "unused"), EMQC_REMOTE_ROOTS="",
               EMQC_PREVIEW_DIR=str(tmp_path / "previews"), PYTHONDONTWRITEBYTECODE="1")
    base_url = f"http://127.0.0.1:{port}"
    with (tmp_path / "server.log").open("w") as log:
        server = subprocess.Popen([sys.executable, "scripts/serve.py", str(port)], cwd=root, env=env,
                                  stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(trust_env=False, timeout=1) as client:
                for _ in range(100):
                    try:
                        if client.get(base_url + "/api/v1/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    pytest.fail((tmp_path / "server.log").read_text())
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--no-proxy-server"],
                                             env=dict(os.environ, TMPDIR=str(root)))
                try:
                    page = browser.new_page(viewport={"width": 1600, "height": 1200})
                    page_errors = []
                    page.on("pageerror", lambda error: page_errors.append(error))
                    page.goto(base_url + "/annotate?block=" + data.name, wait_until="domcontentloaded")
                    page.wait_for_function("document.getElementById('an-meta').textContent.includes('×')")
                    adapter = PlaywrightBrowser(page.context.new_cdp_session(page), base_url)
                    adapter.call("Runtime.enable")
                    page.locator("#an-stage").scroll_into_view_if_needed()
                    yield adapter, page
                    assert not page_errors, page_errors
                    assert not adapter.errors, adapter.errors
                finally:
                    browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def test_pairwise_annotation_with_headless_shell(tmp_path):
    data = tmp_path / "blocks" / "pairs"
    original = write_pairs(data)
    with browser_for(tmp_path, data) as (adapter, page):
        check_pairs((adapter, data, original))
        assert not (data / "seg_edit.npy").exists()
        page.screenshot(path=str(tmp_path / "pairwise.png"), full_page=True)


def test_real_sam_preview_apply_undo_in_browser(tmp_path):
    source = os.environ.get("EMQC_SAM_SMOKE_SOURCE")
    if not source:
        pytest.skip("Set EMQC_SAM_SMOKE_SOURCE to a canonical YXZ block")
    data = tmp_path / "blocks" / "sample"
    data.mkdir(parents=True)
    for name in ("em.npy", "seg.npy"):
        np.save(data / name, np.load(Path(source) / name, mmap_mode="r")[:, :, :2])
    original = np.load(data / "seg.npy")
    work = tmp_path / "work" / "sample"
    with browser_for(tmp_path, data) as (adapter, page):
        page.locator('[data-tool="sam"]').click()
        page.locator("#an-stage").scroll_into_view_if_needed()
        adapter.move((150, 200), click=True)
        page.wait_for_function("!document.getElementById('an-sam-new').disabled", timeout=60000)
        assert not (work / "seg_edit.npy").exists(), "preview must not write labels"
        page.screenshot(path=str(tmp_path / "sam-preview.png"), full_page=True)
        page.locator("#an-sam-new").click()
        page.wait_for_function("document.getElementById('an-nedit').textContent === '1 次改动'")
        page.wait_for_function("!document.getElementById('an-undo').disabled")
        edited = np.load(work / "seg_edit.npy")
        assert np.any(edited != original)
        assert np.array_equal(edited[original != 0], original[original != 0])
        assert np.array_equal(edited[:, :, 1], original[:, :, 1])
        assert np.array_equal(np.load(data / "seg.npy"), original)
        assert not (data / "seg_edit.npy").exists()
        page.locator("#an-undo").click()
        page.wait_for_function("document.getElementById('an-nedit').textContent === '0 次改动' && !document.getElementById('an-undo').disabled")
        assert np.array_equal(np.load(work / "seg_edit.npy"), original)
        # Page changes invalidate the old proposal before it can be applied elsewhere.
        page.locator('[data-tool="sam"]').click()
        page.locator("#an-stage").scroll_into_view_if_needed()
        adapter.move((150, 200), click=True)
        page.wait_for_function("!document.getElementById('an-sam-new').disabled", timeout=60000)
        page.locator("#an-next").click()
        page.wait_for_function("document.getElementById('an-z').value === '1'")
        assert page.locator("#an-sam-new").is_disabled()
        assert page.locator("#an-sam-apply").is_disabled()
