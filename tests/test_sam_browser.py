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
        # The tool remains selected after apply; clicking again would cancel it.
        assert page.locator('[data-tool="sam"]').get_attribute("aria-pressed") == "true"
        # Page changes invalidate the old proposal before it can be applied elsewhere.
        page.locator("#an-stage").scroll_into_view_if_needed()
        adapter.move((150, 200), click=True)
        page.wait_for_function("!document.getElementById('an-sam-new').disabled", timeout=60000)
        page.locator("#an-next").click()
        page.wait_for_function("document.getElementById('an-z').value === '1'")
        assert page.locator("#an-sam-new").is_disabled()
        assert page.locator("#an-sam-apply").is_disabled()


def test_sam_click_starts_target_modifiers_refine_it(tmp_path):
    import base64
    import io
    from PIL import Image
    data = tmp_path / 'blocks' / 'prompts'
    original = write_pairs(data)
    png = io.BytesIO()
    Image.new('RGBA', (original.shape[1], original.shape[0]), (0, 230, 200, 160)).save(png, format='PNG')
    requests = []
    with browser_for(tmp_path, data) as (adapter, page):
        assert page.locator('[data-tool="smart"], [data-tool="cut"], [data-tool="split"], [id^="an-smart-"]').count() == 0
        for name in ("切割", "分离", "智能填充"):
            assert name not in page.locator("#an-help").text_content()
        page.locator("#an-stage").focus()
        for key in ("k", "x", "d", "Enter"):
            page.keyboard.press(key)
            assert page.locator(".tool.active").get_attribute("data-tool") == "pick"
        assert page.locator("#an-nedit").inner_text() == "0 次改动"

        def predict(route):
            requests.append(route.request.post_data_json)
            route.fulfill(json={'token': 'a' * 32, 'candidate': 0, 'n_px': 1, 'seconds': .01,
                                'mask_png': 'data:image/png;base64,' + base64.b64encode(png.getvalue()).decode()})
        page.route('**/sam/predict', predict)
        page.locator('[data-tool="sam"]').click()

        def click(point, modifier=None):
            count = len(requests)
            if modifier:
                page.keyboard.down(modifier)
            page.mouse.click(*adapter.position(point))
            if modifier:
                page.keyboard.up(modifier)
            page.wait_for_function("!document.getElementById('an-sam-new').disabled")
            assert len(requests) == count + 1

        click((5, 5))
        assert page.locator("#an-sam-candidate, #an-sam-predict").count() == 0
        assert "candidate" not in requests[-1]
        assert requests[-1]['snap_boundary'] is True
        assert requests[-1]['boundary_sensitivity'] == .5
        first = requests[-1]['points'][0]
        count = len(requests)
        page.locator('#an-sam-sens').fill('70')
        page.wait_for_function("!document.getElementById('an-sam-new').disabled")
        assert len(requests) == count + 1
        assert requests[-1]['boundary_sensitivity'] == .7
        assert requests[-1]['points'] == [first]
        assert page.locator('#an-sam-sens-v').inner_text() == '70%'
        click((10, 10))
        assert len(requests[-1]['points']) == 1 and requests[-1]['points'][0] != first
        click((15, 15), 'Meta')
        assert requests[-1]['labels'] == [1, 1]
        click((20, 20), 'Control')
        assert requests[-1]['labels'] == [1, 1, 1]
        click((25, 25), 'Shift')
        assert requests[-1]['labels'] == [1, 1, 1, 0]
        click((5, 30))
        assert requests[-1]['labels'] == [1]  # clears positive and negative prompts

        page.locator('[data-tool="sam-box"]').click()
        page.mouse.move(*adapter.position((3, 3)))
        page.mouse.down()
        page.mouse.move(*adapter.position((20, 40)))
        page.mouse.up()
        page.wait_for_function("!document.getElementById('an-sam-new').disabled")
        box = requests[-1]['box']
        assert box is not None and requests[-1]['points'] == []
        page.locator('[data-tool="sam"]').click()
        click((10, 10), 'Meta')
        assert requests[-1]['box'] == box and requests[-1]['labels'] == [1]
        click((20, 20))
        assert requests[-1]['box'] is None and requests[-1]['labels'] == [1]
        page.locator('[data-tool="sam"]').click()
        assert page.locator('[data-tool="sam"]').get_attribute('aria-pressed') == 'false'
        assert page.locator('#an-sam-new').is_disabled()
        page.locator('[data-tool="sam-box"]').click()
        assert page.locator('[data-tool="sam-box"]').get_attribute('aria-pressed') == 'true'
        page.locator('[data-tool="sam-box"]').click()
        assert page.locator('[data-tool="sam-box"]').get_attribute('aria-pressed') == 'false'
        assert not (tmp_path / 'work' / 'prompts' / 'seg_edit.npy').exists()


def test_current_and_new_labels_in_browser(tmp_path):
    data = tmp_path / 'blocks' / 'palette'
    original = write_pairs(data)
    original[:, :, 1] = 0
    np.save(data / 'seg.npy', original)
    with browser_for(tmp_path, data) as (adapter, page):
        assert page.locator('#an-bg').count() == 0
        assert page.locator('#an-cur-id').inner_text() == '未选择'
        assert page.evaluate('''() => {
            const sections = [...document.querySelectorAll('.vast-tools .vast-sec')].map(e => e.textContent);
            return sections.indexOf('工具') < sections.indexOf('当前标签') && sections.indexOf('当前标签') < sections.findIndex(t => t.includes('SAM'));
        }''')
        before = page.locator('#an-segs .row[data-id]').count()
        created = page.locator('#an-segs [data-label-group="created"]')
        existing = page.locator('#an-segs [data-label-group="existing"]')
        assert created.locator('.row[data-id]').count() == 0
        assert existing.locator('.row[data-id]').count() == before
        colors = page.locator('#an-segs .sw').evaluate_all('(els) => els.map(e => e.style.backgroundColor)')
        page.locator('#an-newid').click()
        page.wait_for_function("document.getElementById('an-cur-id').textContent !== '未选择'")
        label = page.locator('#an-cur-id').inner_text()
        row = page.locator(f'#an-segs [data-id="{label}"]')
        assert row.locator('.n').inner_text() == '未使用'
        assert page.locator('#an-rp-apply').is_disabled()
        assert row.locator('.sw').evaluate('(e) => e.style.backgroundColor') not in colors
        assert page.locator('#an-segs .row[data-id]').count() == before + 1
        assert created.locator(f'[data-id="{label}"]').count() == 1
        assert existing.locator(f'[data-id="{label}"]').count() == 0
        page.locator('#an-search').fill(label)
        assert created.locator('.row[data-id]').count() == 1
        assert existing.locator('.row[data-id]').count() == 0
        page.locator('#an-search').fill('22')
        assert existing.locator('[data-id="22"]').count() == 1
        page.locator('#an-search').fill('')
        assert existing.locator('.row[data-id]').count() == before
        assert not (tmp_path / 'work' / 'palette' / 'seg_edit.npy').exists()
        page.reload()
        page.wait_for_selector(f'#an-segs [data-id="{label}"]')
        assert created.locator(f'[data-id="{label}"]').count() == 1
        page.locator(f'#an-segs [data-id="{label}"]').click()
        page.locator('[data-tool="brush"]').click()
        page.locator('#an-brush').fill('0')
        adapter.move((5, 5), click=True)
        page.wait_for_function("document.getElementById('an-nedit').textContent === '1 次改动'")
        page.wait_for_function(f'''document.querySelector('#an-segs [data-id="{label}"] .n').textContent === '1' ''')
        assert created.locator(f'[data-id="{label}"]').count() == 1
        assert existing.locator(f'[data-id="{label}"]').count() == 0
        page.locator('#an-undo').click()
        page.wait_for_function("document.getElementById('an-nedit').textContent === '0 次改动'")
        page.wait_for_function(f'''document.querySelector('#an-segs [data-id="{label}"] .n').textContent === '未使用' ''')
        # Existing label selection survives moving to a slice where it has no pixels.
        page.locator('#an-segs [data-id="22"]').click()
        page.locator('#an-next').click()
        page.wait_for_function("document.getElementById('an-z').value === '1' && document.querySelectorAll('#an-segs .row[data-id]').length === 1")
        assert page.locator('#an-cur-id').inner_text() == '22'
        assert page.locator('#an-segs [data-id="22"]').count() == 0
        assert row.count() == 1
        assert created.locator(f'[data-id="{label}"]').count() == 1
        assert existing.locator('.row[data-id]').count() == 0
        assert np.array_equal(np.load(data / 'seg.npy'), original)
        assert np.array_equal(np.load(tmp_path / 'work' / 'palette' / 'seg_edit.npy'), original)


def test_sam_cancel_during_prediction_ignores_late_result(tmp_path):
    data = tmp_path / 'blocks' / 'cancel'
    write_pairs(data)
    with browser_for(tmp_path, data) as (adapter, page):
        page.evaluate('''() => {
            const realFetch = window.fetch;
            window.fetch = (url, options) => String(url).endsWith('/sam/predict')
                ? new Promise(resolve => { window.finishPrediction = () => resolve(new Response(JSON.stringify({
                    token: 'a'.repeat(32), n_px: 10, mask_png: document.querySelector('canvas').toDataURL()
                }), {status: 200, headers: {'Content-Type': 'application/json'}})); })
                : realFetch(url, options);
        }''')
        page.locator('[data-tool="sam"]').click()
        adapter.move((5, 5), click=True)
        page.wait_for_function('!!window.finishPrediction')
        assert page.locator('#an-sam-new').is_disabled()
        page.locator('[data-tool="sam"]').click()
        page.evaluate('window.finishPrediction()')
        page.wait_for_timeout(100)
        assert page.locator('[data-tool="sam"]').get_attribute('aria-pressed') == 'false'
        assert page.locator('#an-sam-new').is_disabled()
        assert '预览 10' not in page.locator('#an-sam-result').inner_text()
        assert not (tmp_path / 'work' / 'cancel' / 'seg_edit.npy').exists()


def test_3d_modes_toggle_and_close_viewer(tmp_path):
    data = tmp_path / 'blocks' / 'view3d'
    original = write_pairs(data)
    urls = []
    with browser_for(tmp_path, data) as (adapter, page):
        def link(route):
            urls.append(route.request.url)
            route.fulfill(json={'url': 'about:blank#view3d'})
        page.route('**/neuroglancer**', link)
        point, block = page.locator('#an-ng'), page.locator('#an-ng-block')
        assert page.locator('.vast-sec', has_text='3D').inner_text() == '3D'
        assert '黑团就是' not in page.content()
        inactive = point.evaluate('(e) => getComputedStyle(e).backgroundColor')
        point.click()
        assert point.get_attribute('aria-pressed') == 'true'
        page.wait_for_function('(before) => getComputedStyle(document.getElementById("an-ng")).backgroundColor !== before', arg=inactive)
        assert not urls
        with page.expect_popup() as popup:
            adapter.move((5, 5), click=True)
        viewer = popup.value
        viewer.wait_for_url('about:blank#view3d')
        assert 'z=0&x=5&y=5' in urls[-1]
        with viewer.expect_event('close'):
            point.click()
        assert point.get_attribute('aria-pressed') == 'false'
        with page.expect_popup() as popup:
            block.click()
        viewer = popup.value
        viewer.wait_for_url('about:blank#view3d')
        assert block.get_attribute('aria-pressed') == 'true'
        assert '/neuroglancer/block?z=0' in urls[-1]
        with viewer.expect_event('close'):
            block.click()
        assert block.get_attribute('aria-pressed') == 'false'
        # U opens the hovered point, a second U cancels it.
        adapter.move((20, 4))
        page.locator('#an-stage').focus()
        with page.expect_popup() as popup:
            page.keyboard.press('u')
        viewer = popup.value
        viewer.wait_for_url('about:blank#view3d')
        assert 'x=20&y=4' in urls[-1]
        with viewer.expect_event('close'):
            page.keyboard.press('u')
        assert point.get_attribute('aria-pressed') == 'false'
        # Switching to SAM cancels point mode; navigating cancels a viewer.
        point.click()
        page.locator('[data-tool="sam"]').click()
        assert point.get_attribute('aria-pressed') == 'false'
        with page.expect_popup() as popup:
            block.click()
        viewer = popup.value
        viewer.wait_for_url('about:blank#view3d')
        assert page.locator('[data-tool="sam"]').get_attribute('aria-pressed') == 'false'
        with viewer.expect_event('close'):
            page.locator('#an-next').click()
        assert block.get_attribute('aria-pressed') == 'false'
        point.click()
        page.locator('#an-stage').focus()
        page.keyboard.press('Escape')
        assert point.get_attribute('aria-pressed') == 'false'
        page.screenshot(path=str(tmp_path / 'workbench.png'), full_page=True)
        assert np.array_equal(np.load(data / 'seg.npy'), original)
        assert not (tmp_path / 'work' / 'view3d' / 'seg_edit.npy').exists()


def test_3d_cancel_pending_and_failed_links(tmp_path):
    data = tmp_path / 'blocks' / 'late3d'
    write_pairs(data)
    with browser_for(tmp_path, data) as (adapter, page):
        page.evaluate('''() => {
            const realFetch = window.fetch;
            window.fetch = (url, options) => String(url).includes('/neuroglancer')
                ? new Promise(resolve => { window.finish3D = () => resolve(new Response(JSON.stringify({url: 'about:blank#late'}),
                    {status: 200, headers: {'Content-Type': 'application/json'}})); })
                : realFetch(url, options);
        }''')
        with page.expect_popup() as popup:
            page.locator('#an-ng-block').click()
        viewer = popup.value
        page.wait_for_function('!!window.finish3D')
        with viewer.expect_event('close'):
            page.locator('#an-ng-block').click()
        page.evaluate('window.finish3D()')
        page.wait_for_timeout(100)
        assert page.locator('#an-ng-block').get_attribute('aria-pressed') == 'false'
        assert page.locator('#an-ng-info').inner_text() == ''
        assert len(page.context.pages) == 1
        page.evaluate('''() => { window.fetch = () => Promise.resolve(new Response(JSON.stringify({url: null, reason: '不支持此数据块'}),
            {status: 200, headers: {'Content-Type': 'application/json'}})); }''')
        page.locator('#an-ng-block').click()
        page.wait_for_function("document.getElementById('an-ng-info').textContent.includes('不支持此数据块')")
        assert page.locator('#an-ng-block').get_attribute('aria-pressed') == 'false'
        page.wait_for_function('document.querySelector("[data-tool=pick]").classList.contains("active")')
        # Popup blockers produce a recoverable message, with no stuck active button.
        page.evaluate('window.open = () => null')
        page.locator('#an-ng-block').click()
        assert page.locator('#an-ng-info').inner_text() == '请允许弹出 3D 窗口后重试'
        assert page.locator('#an-ng-block').get_attribute('aria-pressed') == 'false'


def test_notices_do_not_cover_image_and_history_follows_slice(tmp_path):
    data = tmp_path / 'blocks' / 'slice-history'
    original = write_pairs(data)
    with browser_for(tmp_path, data) as (adapter, page):
        page.wait_for_function("document.getElementById('an-nedit').textContent === '0 次改动'")
        before = page.locator('#an-stage').bounding_box()
        page.locator('#an-neighbour-pick').click()          # 光标不在图上 → 只弹一条提示，不改任何数据
        assert page.locator('#an-notices #flash').is_visible()
        assert '取色' in page.locator('#flash').inner_text()
        assert page.locator('#an-stage').bounding_box() == before
        note = page.locator('#flash').bounding_box()
        assert note['x'] >= before['x'] + before['width']
        page.locator('#an-notice-close').click()
        assert not page.locator('#flash').is_visible()
        # Also check the stacked, narrow layout: the note is after the image.
        page.set_viewport_size({'width': 900, 'height': 1000})
        page.locator('#an-neighbour-pick').click()
        note, stage = page.locator('#flash').bounding_box(), page.locator('#an-stage').bounding_box()
        assert note['y'] >= stage['y'] + stage['height']
        page.locator('#an-notice-close').click()
        page.set_viewport_size({'width': 1600, 'height': 1200})
        page.locator('#an-segs [data-id="22"]').click()
        page.locator('[data-tool="brush"]').click()
        page.locator('#an-brush').fill('0')
        adapter.move((5, 5), click=True)
        page.wait_for_function("document.getElementById('an-nedit').textContent === '1 次改动'")
        assert '#1 ' in page.locator('#an-edits').inner_text()
        page.locator('#an-next').click()
        page.wait_for_function("document.getElementById('an-z').value === '1' && document.getElementById('an-nedit').textContent === '0 次改动'")
        assert '本片还没有改动' in page.locator('#an-edits').inner_text()
        adapter.move((5, 5), click=True)
        page.wait_for_function("document.getElementById('an-nedit').textContent === '1 次改动'")
        assert '#2 ' in page.locator('#an-edits').inner_text()
        assert '#1 ' not in page.locator('#an-edits').inner_text()
        page.locator('#an-prev').click()
        page.wait_for_function("document.getElementById('an-z').value === '0' && document.getElementById('an-edits').textContent.includes('#1 ')")
        page.locator('#an-undo').click()
        page.wait_for_function("document.getElementById('an-nedit').textContent === '0 次改动' && !document.getElementById('an-undo').disabled")
        work = tmp_path / 'work' / 'slice-history' / 'seg_edit.npy'
        assert np.array_equal(np.load(work)[:, :, 0], original[:, :, 0])
        assert int(np.load(work)[5, 5, 1]) == 22
        page.locator('#an-next').click()
        page.wait_for_function("document.getElementById('an-nedit').textContent === '1 次改动'")
        page.locator('#an-stage').focus()
        page.keyboard.press('Control+z')
        page.wait_for_function("document.getElementById('an-nedit').textContent === '0 次改动' && !document.getElementById('an-undo').disabled")
        assert np.array_equal(np.load(work), original)
        assert np.array_equal(np.load(data / 'seg.npy'), original)


def test_late_history_response_cannot_replace_current_slice(tmp_path):
    data = tmp_path / 'blocks' / 'history-race'
    write_pairs(data)
    with browser_for(tmp_path, data) as (adapter, page):
        page.wait_for_function("document.getElementById('an-nedit').textContent === '0 次改动'")
        page.evaluate('''() => {
            const realFetch = window.fetch;
            window.fetch = (url, options) => {
                if (String(url).includes('/edits?z=1')) return new Promise(resolve => {
                    window.finishOldHistory = () => resolve(new Response(JSON.stringify({n: 99, edits: []}),
                        {status: 200, headers: {'Content-Type': 'application/json'}}));
                });
                return realFetch(url, options);
            };
        }''')
        page.locator('#an-next').click()
        page.wait_for_function('!!window.finishOldHistory')
        page.locator('#an-prev').click()
        page.wait_for_function("document.getElementById('an-z').value === '0' && document.getElementById('an-nedit').textContent === '0 次改动'")
        page.evaluate('window.finishOldHistory()')
        page.wait_for_timeout(100)
        assert page.locator('#an-nedit').inner_text() == '0 次改动'
        assert '本片还没有改动' in page.locator('#an-edits').inner_text()
