"""Optional real browser regression for comparison, source filtering and downloads."""
import json

import numpy as np

from annotation_data import write_pairs
from emqc.annotate.store import Block
from test_sam_browser import browser_for


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
    block.apply_labels(0, repair, repair != 0, {"interpolated": True, "source_sections": [0, 1]})
    before_work = (block.work / "seg_edit.npy").read_bytes()
    with browser_for(tmp_path, data) as (_, page):
        page.locator("#an-compare").click()
        page.wait_for_function("document.querySelector('#cmp-page').getAttribute('aria-busy') === 'false' && !document.querySelector('#cmp-report').hidden")
        assert page.locator("#cmp-status").inner_text().endswith("3 像素与原始分割不同")
        assert page.locator("#cmp-before").evaluate("c => [c.width,c.height]") == [64, 32]
        assert page.locator("#cmp-after").evaluate("c => [c.width,c.height]") == [64, 32]
        # Both canvases now paint changed pixels with the same fixed pink highlight.
        # Check the underlying before/after labels via the linked pixel readout.
        page.locator("#cmp-before").evaluate('''c => {
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
        page.locator(".cmp-viewport").first.evaluate("el => {el.scrollLeft = 120; el.dispatchEvent(new Event('scroll'));}")
        page.wait_for_function("Math.abs(document.querySelectorAll('.cmp-viewport')[0].scrollLeft-document.querySelectorAll('.cmp-viewport')[1].scrollLeft)<2")
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
        assert page.locator("#cmp-before").evaluate("c => c.toDataURL()") == page.locator("#cmp-after").evaluate("c => c.toDataURL()")
        page.locator("#cmp-edit").click()
        page.wait_for_function("document.querySelector('#an-z').value === '1'")
        page.locator("#an-compare").click()
        page.wait_for_function("document.querySelector('#cmp-status').textContent.includes('Z 1') && document.querySelector('#cmp-status').textContent.includes('一致')")
        page.set_viewport_size({"width": 1280, "height": 1100})
        page.locator("#cmp-prev").click()
        page.wait_for_function("document.querySelector('#cmp-status').textContent.includes('3 像素')")
        page.screenshot(path=str(tmp_path / "comparison-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 640, "height": 1000})
        assert page.locator("#cmp-before").is_visible() and page.locator("#cmp-after").is_visible()
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
        assert page.locator("#cmp-before").is_visible()
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
