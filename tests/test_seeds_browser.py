"""Real browser checks against isolated data and the actual CPU watershed API."""
import numpy as np

from test_sam_browser import browser_for
from test_seeds import cell_image


def write_cell(data):
    data.mkdir(parents=True)
    image, _ = cell_image()
    np.save(data / 'em.npy', np.stack([image.T, image.T], axis=2))
    labels = np.zeros((120, 80, 2), np.uint64)
    labels[45:50, 42:47, :] = 9007199254740993
    np.save(data / 'seg.npy', labels)
    return labels


def draw_box(adapter, page):
    page.locator('button[data-tool="seed-box"]').click()
    page.locator('#an-stage').scroll_into_view_if_needed()
    page.mouse.move(*adapter.position((4, 4)))
    page.mouse.down()
    page.mouse.move(*adapter.position((109, 75)), steps=6)
    page.mouse.up()
    assert page.locator('button[data-tool="seed-fg"]').get_attribute('aria-pressed') == 'true'


def preview(adapter, page):
    draw_box(adapter, page)
    page.mouse.click(*adapter.position((38, 40)))
    page.wait_for_function("!document.getElementById('an-seed-new').disabled")


def test_seed_preview_refine_apply_undo_and_navigation(tmp_path):
    data = tmp_path / 'blocks' / 'cell'
    original = write_cell(data)
    work = tmp_path / 'work' / 'cell'
    with browser_for(tmp_path, data) as (adapter, page):
        preview(adapter, page)
        assert not (work / 'seg_edit.npy').exists()
        assert '预览' in page.locator('#an-seed-result').inner_text()
        # Exclusion stroke, followed by undoing only that prompt.
        page.locator('button[data-tool="seed-bg"]').click()
        page.mouse.move(*adapter.position((75, 25)))
        page.mouse.down()
        page.mouse.move(*adapter.position((75, 55)), steps=7)
        page.mouse.up()
        page.wait_for_function("!document.getElementById('an-seed-new').disabled")
        page.locator('#an-seed-back').click()
        page.wait_for_function("!document.getElementById('an-seed-new').disabled")
        page.screenshot(path=str(tmp_path / 'seed-preview.png'), full_page=True)
        page.locator('#an-seed-new').click()
        page.wait_for_function("document.getElementById('an-nedit').textContent === '1 次改动'")
        page.wait_for_function("!document.getElementById('an-undo').disabled")
        edited = np.load(work / 'seg_edit.npy')
        assert edited[38, 40, 0] != 0
        assert np.array_equal(edited[original != 0], original[original != 0])
        assert np.array_equal(edited[:, :, 1], original[:, :, 1])
        assert '种子分割' in page.locator('#an-edits').inner_text()
        assert page.locator('#an-seed-new').is_disabled()
        page.locator('#an-undo').click()
        page.wait_for_function("document.getElementById('an-nedit').textContent === '0 次改动' && !document.getElementById('an-undo').disabled")
        assert np.array_equal(np.load(work / 'seg_edit.npy'), original)
        preview(adapter, page)
        page.locator('#an-next').click()
        page.wait_for_function("document.getElementById('an-z').value === '1'")
        assert page.locator('#an-seed-new').is_disabled()
        assert page.locator('#an-seed-back').is_disabled()
        assert np.array_equal(np.load(data / 'seg.npy'), original)


def test_seed_preview_rejects_other_editor_changes(tmp_path):
    data = tmp_path / 'blocks' / 'cell'
    original = write_cell(data)
    work = tmp_path / 'work' / 'cell'
    with browser_for(tmp_path, data) as (adapter, page):
        preview(adapter, page)
        response = page.request.post(adapter.base_url + '/api/v1/annotate/blocks/cell/paint', data={
            'z': 0, 'points': [[20, 20]], 'radius': 0, 'new_id': '42', 'annotator': 'colleague'})
        assert response.ok
        before = np.load(work / 'seg_edit.npy').copy()
        page.locator('#an-seed-new').click()
        page.wait_for_function("document.getElementById('an-seed-result').textContent.includes('已变化')")
        assert np.array_equal(np.load(work / 'seg_edit.npy'), before)
        assert np.array_equal(np.load(data / 'seg.npy'), original)


def test_delayed_seed_preview_cannot_return_after_navigation(tmp_path):
    data = tmp_path / 'blocks' / 'cell'
    write_cell(data)
    with browser_for(tmp_path, data) as (adapter, page):
        pending = []
        page.route('**/seed/predict', lambda route: pending.append(route))
        draw_box(adapter, page)
        page.mouse.click(*adapter.position((38, 40)))
        page.wait_for_timeout(100)
        assert len(pending) == 1
        page.locator('#an-next').click()
        page.wait_for_function("document.getElementById('an-z').value === '1'")
        # A reply from the previous plane must never re-enable Apply.
        pending[0].fulfill(json={'token': 'a' * 32, 'n_px': 100, 'z': 0, 'box': [4, 4, 110, 76],
                                'seconds': .1, 'warnings': [], 'mask_png': 'data:image/png;base64,invalid'})
        page.wait_for_timeout(100)
        assert page.locator('#an-seed-new').is_disabled()
        assert page.locator('#an-seed-apply').is_disabled()


def test_full_slice_and_snapped_edges_can_fill_top_and_bottom(tmp_path):
    data = tmp_path / 'blocks' / 'edge-cell'
    data.mkdir(parents=True)
    # A neurite cut by both physical image edges, separated by vertical membranes.
    em = np.full((120, 80, 2), 180, np.uint8)
    em[39:43, :, :] = 20
    em[77:81, :, :] = 20
    original = np.zeros(em.shape, np.uint64)
    original[98:105, :, :] = 9007199254740993
    np.save(data / 'em.npy', em)
    np.save(data / 'seg.npy', original)
    work = tmp_path / 'work' / 'edge-cell'
    with browser_for(tmp_path, data) as (adapter, page):
        requests = []
        page.on('request', lambda r: requests.append(r.post_data_json) if r.url.endswith('/seed/predict') else None)
        page.locator('button[data-tool="seed-box"]').click()
        page.locator('#an-stage').scroll_into_view_if_needed()
        # Near-edge drags snap to exact top/bottom bounds without pixel-perfect aim.
        page.mouse.move(*adapter.position((25, 1)))
        page.mouse.down()
        page.mouse.move(*adapter.position((89, 78)), steps=4)
        page.mouse.up()
        page.mouse.click(*adapter.position((60, 40)))
        page.wait_for_function("!document.getElementById('an-seed-new').disabled")
        assert requests[-1]['box'][1] == 0 and requests[-1]['box'][3] == 80
        page.locator('#an-seed-full').click()
        assert page.locator('#an-seed-new').is_disabled()
        page.locator('#an-stage').scroll_into_view_if_needed()
        page.mouse.click(*adapter.position((60, 40)))
        page.wait_for_function("!document.getElementById('an-seed-new').disabled")
        assert requests[-1]['box'] == [0, 0, 120, 80]
        page.locator('#an-seed-new').click()
        page.wait_for_function("document.getElementById('an-nedit').textContent === '1 次改动' && !document.getElementById('an-undo').disabled")
        edited = np.load(work / 'seg_edit.npy')
        assert edited[60, 0, 0] != 0 and edited[60, -1, 0] != 0
        assert not edited[85:95, :, 0].any(), 'no growth through the right membrane'
        assert np.array_equal(edited[original != 0], original[original != 0])
        assert np.array_equal(edited[:, :, 1], original[:, :, 1])
        page.locator('#an-undo').click()
        page.wait_for_function("document.getElementById('an-nedit').textContent === '0 次改动' && !document.getElementById('an-undo').disabled")
        assert np.array_equal(np.load(work / 'seg_edit.npy'), original)


def test_seed_guided_controls_and_independent_refine_settings(tmp_path):
    data = tmp_path / 'blocks' / 'cell'
    original = write_cell(data)
    with browser_for(tmp_path, data) as (adapter, page):
        assert not page.locator('#an-sam-panel').evaluate('(el) => el.open')
        assert not page.locator('#an-seed-options').evaluate('(el) => el.open')
        assert page.locator('button[data-tool="seed-fg"]').is_disabled()
        assert page.locator('#an-seed-step-1').get_attribute('aria-current') == 'step'
        assert page.locator('#an-refine-options').is_hidden()
        requests = []
        page.on('request', lambda r: requests.append(r.post_data_json) if r.url.endswith('/seed/predict') else None)
        preview(adapter, page)
        assert page.locator('#an-seed-step-3').get_attribute('aria-current') == 'step'
        assert page.locator('#an-seed-apply').is_disabled()
        page.locator('#an-seed-options > summary').click()
        page.locator('#an-seed-scale').fill('1.8')
        page.wait_for_function("!document.getElementById('an-seed-new').disabled")
        assert requests[-1]['scale'] == 1.8
        # Alt-pick keeps the proposal and makes the target explicit, even for uint64 IDs.
        page.locator('#an-stage').scroll_into_view_if_needed()
        page.keyboard.down('Alt')
        page.mouse.click(*adapter.position((47, 44)))
        page.keyboard.up('Alt')
        assert page.locator('#an-seed-apply').is_enabled()
        assert '9007199254740993' in page.locator('#an-seed-apply').get_attribute('title')
        page.locator('#an-seed-options > summary').click()
        page.locator('#an-seed-clear').click()
        assert page.locator('#an-seed-new').is_disabled()
        assert page.locator('button[data-tool="seed-box"]').get_attribute('aria-pressed') == 'true'
        assert page.locator('button[data-tool="seed-fg"]').is_disabled()
        assert page.locator('#an-seed-range').inner_text() == '未选择'
        assert not (tmp_path / 'work' / 'cell' / 'seg_edit.npy').exists()
        # Refinement is configurable without opening SAM, and zero must stay zero.
        page.locator('button[data-tool="refine"]').click()
        assert page.locator('#an-refine-options').is_visible()
        page.locator('#an-refine-sens').fill('0')
        assert page.locator('#an-refine-sens-v').inner_text() == '0%'
        assert page.locator('#an-sam-sens').input_value() == '50'
        page.locator('#an-stage').scroll_into_view_if_needed()
        with page.expect_request('**/refine-edge') as request:
            page.mouse.click(*adapter.position((47, 44)))
        assert request.value.post_data_json['sensitivity'] == 0
        page.wait_for_function("!document.getElementById('an-undo').disabled")
        page.locator('button[data-tool="pick"]').click()
        assert page.locator('#an-refine-options').is_hidden()
        assert np.array_equal(np.load(data / 'seg.npy'), original)
        # Compact and expanded panels must fit the available column at each size.
        for width, height in [(1600, 1000), (1366, 768), (1024, 768), (390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            for expanded in [False, True]:
                page.locator('#an-sam-panel, #an-seed-options').evaluate_all('(els, open) => els.forEach(el => el.open = open)', expanded)
                page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                assert page.locator('.vast-tools').evaluate('(el) => el.scrollWidth <= el.clientWidth + 1'), (width, height, expanded, page.locator('.vast-tools').evaluate('''el => ({
                    width: el.clientWidth, scroll: el.scrollWidth,
                    overflow: [...el.querySelectorAll('*')].filter(c => c.getBoundingClientRect().right > el.getBoundingClientRect().right).map(c => [c.tagName, c.id, c.className, c.getBoundingClientRect().width])
                })'''))
        page.set_viewport_size({'width': 1600, 'height': 1000})
        page.locator('#an-sam-panel, #an-seed-options').evaluate_all('(els) => els.forEach(el => el.open = false)')
        page.locator('.vast-tools').evaluate('(el) => el.scrollTop = 0')
        page.screenshot(path=str(tmp_path / 'seed-ui.png'), full_page=True)
