"""Optional real browser regression for comparison, source filtering and downloads."""
from annotation_data import historical_interpolation

import json

import numpy as np
import pytest

from annotation_data import write_pairs
from emqc.annotate.store import Block
from test_sam_browser import browser_for


@pytest.fixture
def comparison_page(tmp_path):
    data = tmp_path / "blocks" / "stack"
    original = write_pairs(data)
    np.save(data / "seg.npy", np.repeat(original, 4, axis=2))
    np.save(data / "em.npy", np.repeat(np.load(data / "em.npy"), 4, axis=2))
    with browser_for(tmp_path, data) as (_, page):
        requests = []
        page.on("request", lambda r: requests.append(r.url) if "/compare/" in r.url else None)
        page.locator("#an-compare").click()
        page.wait_for_function("document.querySelector('#cmp-page').getAttribute('aria-busy') === 'false' && !document.querySelector('#cmp-images').hidden")
        yield page, requests


def test_comparison_wheel_zooms_without_turning_slices(comparison_page):
    page, requests = comparison_page
    viewport = page.locator(".cmp-viewport").first
    for delta in ({"deltaY": -40}, {"deltaY": -40, "ctrlKey": True}, {"deltaY": -40, "shiftKey": True}):
        viewport.dispatch_event("wheel", delta)
    assert page.locator("#cmp-zoom").input_value() == "175"
    viewport.dispatch_event("wheel", {"deltaY": 40})
    assert page.locator("#cmp-zoom").input_value() == "150"
    for delta in ({"deltaX": 50, "deltaY": 0}, {"deltaX": 50, "deltaY": 1}, {"deltaY": 0}):
        viewport.dispatch_event("wheel", delta)
    page.wait_for_timeout(300)
    assert page.locator("#cmp-zoom").input_value() == "150"
    assert page.locator("#cmp-z").input_value() == "0" and len(requests) == 1
    viewport.focus()
    for key, z in [("ArrowDown", "1"), ("z", "2"), ("a", "1"), ("ArrowUp", "0")]:
        page.keyboard.press(key)
        page.wait_for_function("z => document.querySelector('#cmp-z').value === z && document.querySelector('#cmp-page').getAttribute('aria-busy') === 'false'", arg=z)
    count = len(requests)
    for key in ["ArrowRight", "ArrowLeft", "w", "s", "PageDown", "End"]:
        page.keyboard.press(key)
    assert len(requests) == count and page.locator("#cmp-z").input_value() == "0"


def test_comparison_boundaries_same_slice_and_key_repeat_do_not_reload(comparison_page):
    page, requests = comparison_page
    viewport = page.locator(".cmp-viewport").first
    viewport.dispatch_event("wheel", {"deltaY": -40})
    viewport.focus()
    page.keyboard.press("ArrowUp")
    page.locator("#cmp-z").dispatch_event("change")
    page.wait_for_timeout(350)
    assert len(requests) == 1, "the first slice must not be reloaded by backward navigation"
    viewport.dispatch_event("keydown", {"key": "ArrowDown", "repeat": True})
    page.wait_for_timeout(200)
    assert len(requests) == 1, "holding a key must not start automatic paging"
    page.locator("#cmp-z").fill("7")
    page.locator("#cmp-z").dispatch_event("change")
    page.wait_for_function("document.querySelector('#cmp-page').getAttribute('aria-busy') === 'false'")
    assert len(requests) == 2
    viewport.focus()
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(350)
    assert len(requests) == 2, "the last slice must not be reloaded by forward navigation"
    page.locator("#cmp-z").focus()
    page.locator("#cmp-z").hover()
    page.mouse.wheel(0, -100)
    page.wait_for_timeout(300)
    assert page.locator("#cmp-z").input_value() == "7" and len(requests) == 2
    page.locator("#cmp-refresh").click()
    page.wait_for_function("document.querySelector('#cmp-page').getAttribute('aria-busy') === 'false'")
    assert len(requests) == 3, "explicit refresh still fetches fresh data"


def test_comparison_loading_keeps_page_layout_zoom_and_pan(comparison_page):
    page, requests = comparison_page
    page.set_viewport_size({"width": 1400, "height": 900})
    page.locator("#cmp-zoom").fill("300")
    page.locator(".cmp-viewport").first.evaluate("el => { el.scrollLeft = 100; el.scrollTop = 80; }")
    page.wait_for_function("[...document.querySelectorAll('.cmp-viewport')].every(v => v.scrollLeft === 100 && v.scrollTop === 80)")
    page.evaluate("window.scrollTo(0, 120)")
    geometry = """() => ({y: scrollY, height: document.documentElement.scrollHeight,
        panes: [...document.querySelectorAll('.cmp-viewport')].map(v => [v.scrollLeft, v.scrollTop, v.getBoundingClientRect().top])})"""
    before = page.evaluate(geometry)
    pending = []
    page.route("**/compare/1", lambda route: pending.append(route))
    page.locator("#cmp-next").evaluate("button => button.click()")
    page.wait_for_function("document.querySelector('#cmp-page').getAttribute('aria-busy') === 'true'")
    page.wait_for_timeout(150)
    assert pending
    assert page.locator("#cmp-images").is_visible(), "loading must not collapse the image area"
    assert page.evaluate(geometry) == before, "loading must not jump or scroll the document"
    pending[0].fulfill(response=pending[0].fetch())
    page.wait_for_function("document.querySelector('#cmp-page').getAttribute('aria-busy') === 'false'")
    assert page.evaluate(geometry) == before
    assert page.locator("#cmp-zoom").input_value() == "300"
    assert page.locator("#cmp-z").input_value() == "1" and len(requests) == 2


def test_comparison_pending_and_stale_requests_cannot_turn_back(comparison_page):
    page, requests = comparison_page
    pending = []
    page.route("**/compare/1", lambda route: pending.append((route, route.fetch())))
    page.locator("#cmp-next").click()
    page.wait_for_timeout(150)
    assert len(pending) == 1 and len(requests) == 2
    page.locator("#cmp-z").dispatch_event("change")
    page.wait_for_timeout(150)
    assert len(pending) == 1 and len(requests) == 2, "the same pending slice must not restart its request"
    page.locator("#cmp-next").click()
    page.wait_for_function("document.querySelector('#cmp-status').textContent.includes('Z 2') && document.querySelector('#cmp-page').getAttribute('aria-busy') === 'false'")
    route, response = pending.pop()
    route.fulfill(response=response)
    page.wait_for_timeout(350)
    assert page.locator("#cmp-z").input_value() == "2"
    assert "Z 2" in page.locator("#cmp-status").inner_text() and len(requests) == 3


def test_comparison_failed_refresh_waits_for_explicit_retry(comparison_page):
    page, requests = comparison_page
    pattern = "**/compare/0"
    page.route(pattern, lambda route: route.fulfill(status=503, json={"detail": "临时不可用"}))
    height = page.evaluate("document.documentElement.scrollHeight")
    page.locator("#cmp-refresh").click()
    page.wait_for_function("document.querySelector('#cmp-status').textContent.includes('加载失败')")
    assert not page.locator("#cmp-images").is_visible(), "a failed request must not show stale images as current"
    assert page.evaluate("document.documentElement.scrollHeight") == height
    assert page.locator("#cmp-refresh").is_enabled()
    page.wait_for_timeout(1200)
    assert len(requests) == 2, "failure must not start an automatic refresh loop"
    page.unroute(pattern)
    page.locator("#cmp-refresh").click()
    page.wait_for_function("document.querySelector('#cmp-page').getAttribute('aria-busy') === 'false' && !document.querySelector('#cmp-status').textContent.includes('加载失败')")
    assert page.locator("#cmp-images").is_visible() and len(requests) == 3


def test_neuroglancer_third_pane_keeps_images_fitted_and_aligned(comparison_page):
    from urllib.parse import parse_qs, urlsplit

    page, requests = comparison_page
    page.route("https://neuroglancer.example/**", lambda r: r.fulfill(body="<!doctype html><title>Viewer</title>"))

    def embed(route):
        z = parse_qs(urlsplit(route.request.url).query)["z"][0]
        route.fulfill(json={"url": f"https://neuroglancer.example/viewer#z={z}", "center": [10, 20, int(z)]})

    page.route("**/neuroglancer/embed?*", embed)
    # Load the viewer only after installing its mock, resetting the cached location.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("document.querySelector('#cmp-ng-seg-caption').textContent.includes('Z 0')")   # 两栏 Neuroglancer 默认常驻
    initial_requests = len(requests)
    for width, height in [(2559, 1345), (1366, 768), (1024, 768), (768, 1024), (390, 844), (320, 740)]:
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        geometry = page.evaluate("""() => ({
            width: innerWidth, scroll: document.documentElement.scrollWidth,
            panes: [...document.querySelectorAll('.cmp-viewport,.cmp-ngbox')].map(e => {
                const r=e.getBoundingClientRect(); return {y:r.y,height:r.height};
            }),
            fitted: [...document.querySelectorAll('.cmp-viewport')].every(v =>
                v.scrollWidth<=v.clientWidth+1 && v.scrollHeight<=v.clientHeight+1)
        })""")
        assert geometry["scroll"] <= width + 1, page.locator("body *").evaluate_all("es => es.filter(e => e.getBoundingClientRect().right > innerWidth + 1).map(e => [e.tagName, e.id, e.className]).slice(0, 12)")
        assert geometry["fitted"]
        panes = geometry["panes"]
        assert all(p["height"] == pytest.approx(panes[0]["height"], abs=1) for p in panes)
        if width > 1100:
            assert all(p["y"] == pytest.approx(panes[0]["y"], abs=1) for p in panes)
    assert len(requests) == initial_requests, "enabling or resizing the third pane must not reload the comparison"
    page.locator("#cmp-next").click()
    page.wait_for_function("document.querySelector('#cmp-ng-seg-frame').getAttribute('src').endsWith('z=1') && document.querySelector('#cmp-ng-em-frame').getAttribute('src').endsWith('z=1')")
    assert len(requests) == initial_requests + 1
    assert page.locator(".cmp-viewport").evaluate_all("es=>es.every(v=>v.scrollWidth<=v.clientWidth+1&&v.scrollHeight<=v.clientHeight+1)")


def test_comparison_in_browser(tmp_path):
    data = tmp_path / "blocks" / "pairs"
    original = write_pairs(data)
    block = Block(data, tmp_path / "work")
    block.paint(0, [(4, 4)], 0, 0)  # Relabelling with the brush requires erasing first.
    block.paint(0, [(4, 4)], 0, 77)
    mask = np.zeros(block.shape_zyx[1:], bool)
    mask[4, 20] = True
    block.apply_mask(0, mask, 77, {"model": "SAM 2.1"})
    repair = np.zeros(block.shape_zyx[1:], dtype=np.uint64)
    repair[4, 36] = 77
    historical_interpolation(block, 0, repair, repair != 0, {"interpolated": True, "source_sections": [0, 1]})
    before_work = (block.work / "seg_edit.npy").read_bytes()
    with browser_for(tmp_path, data) as (_, page):
        page.locator("#an-compare").click()
        page.wait_for_function("document.querySelector('#cmp-page').getAttribute('aria-busy') === 'false' && !document.querySelector('#cmp-report').hidden")
        assert page.locator("#cmp-status").inner_text().endswith("3 像素与原始分割不同")
        assert page.locator("#cmp-after").evaluate("c => [c.width,c.height]") == [64, 32]
        # Our single canvas outlines changes; the "before" pictures are the embedded viewer. The readout shows both labels.
        page.locator("#cmp-after").evaluate('''c => {
            const r = c.getBoundingClientRect();
            c.dispatchEvent(new MouseEvent('mousemove', {clientX: r.left + 4.5*r.width/c.width, clientY: r.top + 4.5*r.height/c.height}));
        }''')
        assert "原始 9007199254740993 → 当前 77" in page.locator("#cmp-pixel").inner_text()
        # 页面默认只给两样东西：改了哪些地方，标签增减了什么
        assert page.locator("#cmp-regions tr[data-region]").count() == 3, "三处改动各自成一块"
        assert "3 处" in page.locator("#cmp-regions-count").inner_text()
        assert "77" in page.locator("#cmp-labels").inner_text() and "新出现" in page.locator("#cmp-labels").inner_text()
        page.locator("#cmp-regions tr[data-region]").first.click()          # 点一行，两侧定位到那处改动
        page.wait_for_function("![...document.querySelectorAll('.cmp-ring')].some(r => r.hidden)")
        # 完整溯源报表默认收起，展开后才是同事那套按来源的明细与导出
        assert page.locator("#cmp-rows").is_visible() is False
        page.locator("#cmp-report > summary").click()
        page.locator("#cmp-search").fill("77")
        assert page.locator("#cmp-rows tr").count() == 1
        assert "混合" in page.locator("#cmp-rows").inner_text()
        page.locator("#cmp-source").select_option("sam")
        assert "77" in page.locator("#cmp-rows").inner_text()
        page.locator("#cmp-mode").select_option("sources")
        assert "按来源着色" in page.locator("#cmp-after-caption").inner_text()
        page.locator("#cmp-zoom").fill("300")
        page.locator(".cmp-viewport").evaluate("el => {el.scrollLeft = 120; el.dispatchEvent(new Event('scroll'));}")
        page.wait_for_function("document.querySelector('.cmp-viewport').scrollLeft > 0")
        with page.expect_download() as pending:
            page.locator("#cmp-json").click()
        download = pending.value
        target = tmp_path / "slice-provenance.json"
        download.save_as(target)
        exported = json.loads(target.read_text())
        assert exported["changed_px"] == 3 and exported["sources"]["sam"]["label_pixels"] == 1
        with page.expect_download() as pending:
            page.locator("#cmp-csv").click()
        target = tmp_path / "slice-provenance.csv"
        pending.value.save_as(target)
        assert "9007199254740993" in target.read_text()
        page.locator("#cmp-mode").select_option("labels")
        page.locator("#cmp-report > summary").click()          # 收起，回到默认的精简视图
        page.locator("#cmp-next").click()
        page.wait_for_function("document.querySelector('#cmp-status').textContent.includes('Z 1') && document.querySelector('#cmp-status').textContent.includes('一致')")
        page.locator("#cmp-edit").click()
        page.wait_for_function("document.querySelector('#an-z').value === '1'")
        page.locator("#an-compare").click()
        page.wait_for_function("document.querySelector('#cmp-status').textContent.includes('Z 1') && document.querySelector('#cmp-status').textContent.includes('一致')")
        page.set_viewport_size({"width": 1280, "height": 1100})
        page.locator("#cmp-prev").click()
        page.wait_for_function("document.querySelector('#cmp-status').textContent.includes('3 像素')")
        page.screenshot(path=str(tmp_path / "comparison-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 640, "height": 1000})
        assert page.locator("#cmp-after").is_visible() and page.locator("#cmp-ng-seg-frame").is_visible()
        page.screenshot(path=str(tmp_path / "comparison-mobile.png"), full_page=True)
    assert np.array_equal(np.load(data / "seg.npy"), original)
    assert (block.work / "seg_edit.npy").read_bytes() == before_work


def test_comparison_recovers_from_discovery_failure_and_keeps_export_status_separate(tmp_path):
    data = tmp_path / "blocks" / "pairs"
    write_pairs(data)
    with browser_for(tmp_path, data) as (_, page):
        pattern = "**/api/v1/annotate/comparison-blocks"
        page.route(pattern, lambda route: route.fulfill(status=503, json={"detail": "临时不可用"}))
        page.locator("#an-compare").click()
        page.wait_for_function("document.querySelector('#cmp-status').textContent.includes('临时不可用')")
        assert page.locator("#cmp-refresh").is_enabled()
        page.unroute(pattern)
        page.locator("#cmp-refresh").click()
        page.wait_for_function("!document.querySelector('#cmp-report').hidden")
        assert page.locator("#cmp-after").is_visible()
        # A slow export of Z 0 must not clobber the status of a newer slice.
        page.locator("#cmp-report > summary").click()
        pending = []
        page.route("**/provenance?format=json", lambda route: pending.append(route))
        page.locator("#cmp-scope").select_option("block")
        page.locator("#cmp-json").click()
        page.wait_for_function("document.querySelector('#cmp-export-status').textContent.includes('正在导出')")
        page.locator("#cmp-next").click()
        page.wait_for_function("document.querySelector('#cmp-status').textContent.includes('Z 1') && !document.querySelector('#cmp-report').hidden")
        assert page.locator("#cmp-json").is_disabled()
        assert pending
        pending[0].fulfill(status=503, body="temporary error")
        page.wait_for_function("document.querySelector('#cmp-export-status').textContent.includes('503')")
        assert "Z 1" in page.locator("#cmp-status").inner_text()
        assert "一致" in page.locator("#cmp-status").inner_text()
        assert page.locator("#cmp-json").is_enabled()
