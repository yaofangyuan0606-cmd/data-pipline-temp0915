"""Responsive layout regressions using isolated QC data and real browser geometry."""
import os
from urllib.parse import urlsplit

import numpy as np
import pytest

from test_api import client  # Reuse the isolated QC dataset/run, never the live database.
from test_sam_browser import browser_for

pytestmark = pytest.mark.skipif(os.environ.get("EMQC_PLAYWRIGHT_TESTS") != "1",
                                reason="Set EMQC_PLAYWRIGHT_TESTS=1 for real browser checks")
SIZES = [(2559, 1345), (1920, 1080), (1366, 768), (1024, 768), (768, 1024), (390, 844), (320, 740)]


def assert_page_fits(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (page.url, page.evaluate("""() => ({
        width:innerWidth, scroll:document.documentElement.scrollWidth,
        overflow: [...document.querySelectorAll('main *')].filter(e=> {
            for(let p=e.parentElement;p;p=p.parentElement) if(['auto','scroll','hidden'].includes(getComputedStyle(p).overflowX)) return false;
            return e.getBoundingClientRect().right>innerWidth+1;
        }).slice(0,8).map(e=>[e.tagName,e.className,e.id,e.textContent.slice(0,80),e.getBoundingClientRect().width])})"""))


def resize(page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    # ResizeObserver and canvas transforms settle at the next rendering opportunity.
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


def test_dashboard_pages_fit_with_populated_tables(client, registered, qc_run, tmp_path):
    from playwright.sync_api import sync_playwright

    urls = ["/", "/pipeline", "/runs", f"/runs/{qc_run}", f"/datasets/{registered}",
            f"/datasets/{registered}/blocks/z00000-00019", "/checks", "/traces", "/delivery",
            "/patches", "/crawl", "/annotate/blocks", "/annotate/guide"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--no-proxy-server"],
                                     env=dict(os.environ, TMPDIR=str(tmp_path)))
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def serve(route):
            assert route.request.method == "GET", "layout checks must not submit forms"
            url = urlsplit(route.request.url)
            response = client.get(url.path + ("?" + url.query if url.query else ""))
            route.fulfill(status=response.status_code, body=response.content,
                          content_type=response.headers.get("content-type", "text/plain"))

        page.route("**/*", serve)
        try:
            for url in urls:
                response = page.goto("http://layout.test" + url, wait_until="networkidle")
                assert response.status == 200
                # Include tables/logs hidden in disclosure panels, not just the initial empty layout.
                page.locator("details").evaluate_all("es => es.forEach(e => e.open = true)")
                for width, height in SIZES:
                    resize(page, width, height)
                    assert_page_fits(page)
                    if width == 2559 and url != "/annotate/guide":
                        assert page.locator("main").bounding_box()["width"] == width - 216
                page.screenshot(path=str(tmp_path / (url.strip("/").replace("/", "-") + "-narrow.png")), full_page=True)
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width,height", [(512, 512), (768, 192), (192, 768)])
def test_image_fit_resize_and_coordinates(tmp_path, width, height):
    data = tmp_path / "blocks" / "aspect"
    data.mkdir(parents=True)
    # Store's current screen mapping is arr[:, :, z].T. Include an identifiable corner label.
    em = np.full((width, height, 2), 128, dtype=np.uint8)
    seg = np.zeros(em.shape, dtype=np.uint64)
    seg[2:10, 2:10, :] = 7
    np.save(data / "em.npy", em)
    np.save(data / "seg.npy", seg)
    with browser_for(tmp_path, data) as (_, page):
        fitted = """() => [...document.querySelectorAll('.vast-stage:not(.off)')].every(v => {
            const a=v.getBoundingClientRect(), c=v.querySelector('canvas').getBoundingClientRect();
            return c.width>0 && c.left>=a.left && c.top>=a.top && c.right<=a.right && c.bottom<=a.bottom;
        })"""
        for view in ["overlay", "side"]:
            page.locator(f'input[name=an-view][value={view}]').check()
            for w, h in SIZES:
                resize(page, w, h)
                page.wait_for_function(fitted)
                assert_page_fits(page)
                for canvas in page.locator(".vast-stage:not(.off) canvas:first-child").all():
                    rect = canvas.bounding_box()
                    assert rect["width"] / rect["height"] == pytest.approx(width / height, abs=.001)
        # Resizing the stage itself (without a window resize) must also recompute the fit.
        resize(page, 1600, 1000)
        page.locator("#an-stages").evaluate("e => e.style.height='260px'")
        page.wait_for_function(fitted)
        page.evaluate("localStorage.setItem('cmp-zoom', '400')")
        page.locator("#an-compare").click()
        page.wait_for_function("document.querySelector('#cmp-page').getAttribute('aria-busy') === 'false'")
        assert page.locator("#cmp-zoom").input_value() == "100", "opening a comparison must show the whole image"
        requests = []
        page.on("request", lambda r: requests.append(r.url) if "/compare/" in r.url else None)
        for w, h in SIZES:
            resize(page, w, h)
            page.wait_for_function("""() => [...document.querySelectorAll('.cmp-viewport')].every(v => {
                const a=v.getBoundingClientRect(), c=v.querySelector('canvas').getBoundingClientRect();
                return c.width>0 && c.left>=a.left-1 && c.top>=a.top-1 && c.right<=a.right+1 && c.bottom<=a.bottom+1
                    && v.scrollWidth<=v.clientWidth+1 && v.scrollHeight<=v.clientHeight+1;
            })""")
            assert_page_fits(page)
            before, after = [c.bounding_box() for c in page.locator(".cmp-canvas-wrap canvas").all()]
            assert before["width"] / before["height"] == pytest.approx(width / height, abs=.001)
            assert before["width"] == pytest.approx(after["width"], abs=1)
            if w > 700:
                assert before["y"] == pytest.approx(after["y"], abs=1), "both images must start on the same row"
        resize(page, 1366, 768)
        page.locator("#cmp-fit").click()
        fit = page.locator("#cmp-before").bounding_box()
        page.locator("#cmp-zoom").fill("300")
        zoom = page.locator("#cmp-before").bounding_box()
        assert zoom["width"] == pytest.approx(fit["width"] * 3, abs=1)
        page.locator(".cmp-viewport").first.evaluate("v => {v.scrollLeft=100;v.scrollTop=100}")
        page.wait_for_function("""() => {const [a,b]=document.querySelectorAll('.cmp-viewport');
            return a.scrollLeft===b.scrollLeft && a.scrollTop===b.scrollTop;}""")
        page.locator(".cmp-viewport").first.scroll_into_view_if_needed()
        page.locator(".cmp-viewport").evaluate_all("es=>es.forEach(v=>v.scrollTo(0,0))")
        page.locator("#cmp-before").evaluate("""c => {
            const r=c.getBoundingClientRect();c.dispatchEvent(new MouseEvent('mousemove',
                {clientX:Math.round(r.left+4.5*r.width/c.width),clientY:Math.round(r.top+4.5*r.height/c.height)}));
        }""")
        assert "X 4 · Y 4" in page.locator("#cmp-pixel").inner_text()
        assert "原始 7 → 当前 7" in page.locator("#cmp-pixel").inner_text()
        page.locator("#cmp-fit").click()
        assert page.locator("#cmp-before").bounding_box()["width"] == pytest.approx(fit["width"], abs=1)
        assert page.locator(".cmp-viewport").evaluate_all("es=>es.every(v=>v.scrollLeft===0&&v.scrollTop===0)")
        page.locator("#cmp-report").evaluate("e => e.open=true")
        resize(page, 320, 740)
        assert_page_fits(page)
        assert not requests, "resize, zoom and fit must not reload or turn slices"
    assert np.array_equal(np.load(data / "seg.npy"), seg)
