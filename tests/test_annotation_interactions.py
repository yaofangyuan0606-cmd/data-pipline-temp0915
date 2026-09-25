"""Browser checks for selection footprints, navigation and retired controls."""
import numpy as np

from annotation_data import write_pairs
from emqc.annotate.store import Block
from test_sam_browser import browser_for


def test_workbench_wheel_zoom_and_keyboard_navigation(tmp_path):
    data = tmp_path / 'blocks' / 'stack'
    original = write_pairs(data)
    np.save(data / 'seg.npy', np.repeat(original, 3, axis=2))
    np.save(data / 'em.npy', np.repeat(np.load(data / 'em.npy'), 3, axis=2))
    with browser_for(tmp_path, data) as (_, page):
        stage = page.locator('#an-stage')
        assert page.locator('[id^="an-rp-"]').count() == 0
        assert '切片修补' not in page.locator('#an-help').text_content()
        before = page.locator('.vast-canvas').first.evaluate('e => e.style.transform')
        stage.dispatch_event('wheel', {'deltaY': -80, 'clientX': 700, 'clientY': 500})
        zoomed = page.locator('.vast-canvas').first.evaluate('e => e.style.transform')
        assert before != zoomed and page.locator('#an-z').input_value() == '0'
        stage.dispatch_event('wheel', {'deltaY': 80, 'clientX': 700, 'clientY': 500})
        assert page.locator('.vast-canvas').first.evaluate('e => e.style.transform') != zoomed
        stage.focus()
        for key, z in [('ArrowDown', '1'), ('z', '2'), ('a', '1'), ('ArrowUp', '0')]:
            page.keyboard.press(key)
            page.wait_for_function('z => document.querySelector("#an-z").value === z', arg=z)
        for key in ['w', 's', 'PageDown', 'PageUp', 'Home', 'End', 'ArrowLeft', 'ArrowRight']:
            page.keyboard.press(key)
        stage.dispatch_event('keydown', {'key': 'z', 'repeat': True})
        assert page.locator('#an-z').input_value() == '0'
        page.locator('#an-z').focus()
        page.locator('#an-z').hover()
        page.mouse.wheel(0, -100)
        page.wait_for_timeout(100)
        assert page.locator('#an-z').input_value() == '0'


def test_labels_and_edit_records_highlight_exact_regions_and_clear_stale_responses(tmp_path):
    data = tmp_path / 'blocks' / 'pairs'
    write_pairs(data)
    block = Block(data, tmp_path / 'work')
    label = str(block.new_id(0))
    block.paint(0, [(12, 4)], 0, int(label))
    block.paint(0, [(12, 20)], 0, int(label))
    erase = block.paint(0, [(20, 4)], 0, 0)
    before = (block.work / 'seg_edit.npy').read_bytes()
    with browser_for(tmp_path, data) as (adapter, page):
        page.locator('#an-hover').uncheck()
        row = page.locator(f'#an-segs [data-id="{label}"]')
        row.click()
        assert row.get_attribute('aria-pressed') == 'true'
        adapter.wait(adapter.alpha((12, 4)) + ' > 0 && ' + adapter.alpha((12, 20)) + ' > 0')
        assert adapter.evaluate(adapter.alpha((20, 4))) == 0
        page.locator('input[name="an-view"][value="side"]').check()
        assert page.locator('#an-stage2 canvas').nth(2).evaluate('c => c.getContext("2d").getImageData(12,20,1,1).data[3]') > 0
        record = page.locator(f'#an-edits [data-edit="{erase["n"]}"]')
        record.click()
        adapter.wait(adapter.alpha((20, 4)) + ' > 0')
        assert adapter.evaluate(adapter.alpha((12, 4))) == 0
        assert record.get_attribute('aria-pressed') == 'true' and row.get_attribute('aria-pressed') == 'false'
        assert page.locator('#an-cur-id').inner_text() == label
        page.screenshot(path=str(tmp_path / 'selected-edit.png'), full_page=True)
        page.keyboard.press('Escape')
        assert page.locator('.vast-list .selected').count() == 0
        assert adapter.evaluate(adapter.alpha((20, 4))) == 0
        pending = []
        page.route('**/edits/*/mask.png?*', lambda route: pending.append((route, route.fetch())))
        record.click()
        page.wait_for_timeout(150)
        assert pending
        row.click()
        request, response = pending.pop()
        request.fulfill(response=response)
        page.wait_for_timeout(100)
        assert adapter.evaluate(adapter.alpha((20, 4))) == 0
        assert adapter.evaluate(adapter.alpha((12, 20))) > 0
        record.click()
        page.wait_for_timeout(150)
        page.locator('#an-next').click()
        page.wait_for_function('document.querySelector("#an-z").value === "1"')
        request, response = pending.pop()
        request.fulfill(response=response)
        page.wait_for_timeout(100)
        assert page.locator('.vast-list .selected').count() == 0
        assert adapter.evaluate(adapter.alpha((20, 4))) == 0
    assert (block.work / 'seg_edit.npy').read_bytes() == before


def test_new_label_highlights_list_then_painted_pixels(tmp_path):
    data = tmp_path / 'blocks' / 'new-label'
    original = write_pairs(data)
    with browser_for(tmp_path, data) as (adapter, page):
        page.locator('#an-hover').uncheck()
        with page.expect_response(lambda r: '/new-id?' in r.url) as response:
            page.locator('#an-newid').click()
        assert response.value.status == 200, response.value.text()
        page.wait_for_function('document.querySelector("#an-segs .selected") !== null')
        new_id = page.locator('#an-cur-id').inner_text()
        assert page.locator(f'#an-segs [data-id="{new_id}"]').inner_text().endswith('未使用')
        assert adapter.evaluate(adapter.alpha((12, 4))) == 0
        page.locator('[data-tool="brush"]').click()
        page.locator('#an-brush').evaluate("e => { e.value = 0; e.dispatchEvent(new Event('input', {bubbles:true})); }")
        adapter.move((12, 4), click=True)
        page.locator('#an-cur-id').hover()
        page.wait_for_function('document.querySelector("#an-nedit").textContent === "1 次改动"')
        adapter.wait(adapter.alpha((12, 4)) + ' > 0')
        edited = np.load(tmp_path / 'work' / 'new-label' / 'seg_edit.npy')
        assert int(edited[12, 4, 0]) == int(new_id)
        assert np.array_equal(np.load(data / 'seg.npy'), original)
