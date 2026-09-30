"""Seed segmentation behavior, uint64 coordinates, ownership, conflicts and undo."""
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from emqc.annotate.seeds import SeedService, segment
from emqc.annotate.store import Actor, Block


def cell_image():
    y, x = np.mgrid[:80, :120]
    radius = np.hypot(x - 38, y - 40)
    image = np.full((80, 120), 180, np.uint8)
    image[np.abs(radius - 20) < 2] = 20
    return image, radius


BOX = [4, 4, 110, 76]
STROKES = [{'label': 1, 'points': [[38, 40]]}, {'label': 0, 'points': [[75, 40]]}]


@pytest.fixture
def block(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    image, _ = cell_image()
    # Non-square disk array; current viewer transposes each section.
    em = np.stack([image.T, image.T], axis=2)
    labels = np.zeros(em.shape, np.uint64)
    labels[45:50, 42:47, :] = 9007199254740993
    np.save(source / 'em.npy', em)
    np.save(source / 'seg.npy', labels)
    return Block(source, tmp_path / 'work')


def test_closed_membrane_and_negative_stroke():
    image, radius = cell_image()
    mask, warnings = segment(image, np.zeros_like(image), BOX, STROKES)
    r = radius[4:76, 4:110]
    assert mask[r < 16].mean() > .98
    assert not mask[r > 23].any(), 'a clear enclosing membrane must stop the proposal'
    assert not warnings
    # Sparse end points must form a continuous exclusion stroke through the cell.
    exclusion = {'label': 0, 'points': [[44, 24], [44, 56]]}
    refined, _ = segment(image, np.zeros_like(image), BOX, [*STROKES, exclusion])
    assert not refined[20:53, 40].any()
    assert refined[36, 34], 'positive seed remains selected'
    assert not refined[36, 71], 'negative seed stays excluded'


def test_preview_apply_protection_axes_provenance_and_undo(block):
    from emqc.annotate.provenance import report
    old = np.load(block.path / 'seg.npy').copy()
    service = SeedService()
    actor = Actor('Alice', 'alice', 42)
    result = service.predict(block, 0, BOX, STROKES, by=actor)
    assert result['n_px'] > 500
    assert not (block.work / 'seg_edit.npy').exists()
    rec = service.apply(block, result['token'], 9007199254741017, by=actor)
    edited = np.load(block.work / 'seg_edit.npy')
    assert edited[38, 40, 0] == 9007199254741017
    assert edited[75, 40, 0] == 0
    assert np.array_equal(edited[old != 0], old[old != 0])
    assert np.array_equal(edited[:, :, 1], old[:, :, 1])
    assert np.array_equal(np.load(block.path / 'seg.npy'), old)
    assert rec['kind'] == 'seed' and rec['by_user'] == 'alice'
    assert rec['only_background'] is True
    assert report(block, 0)['sources']['assisted']['label_pixels'] == rec['n_px']
    with pytest.raises(ValueError, match='过期'):
        service.apply(block, result['token'], 55, by=actor)
    block.undo(z=0, by=actor)
    assert np.array_equal(np.load(block.work / 'seg_edit.npy'), old)


def test_owner_expiration_wrong_work_id_and_slice_revision(block):
    service = SeedService()
    alice, bob = Actor('Alice', 'alice', 1), Actor('Bob', 'bob', 2)
    r = service.predict(block, 0, BOX, STROKES, by=alice)
    for wrong in (bob, None):
        with pytest.raises(ValueError, match='自己的'):
            service.apply(block, r['token'], 66, by=wrong)
    for bad_id in (0, -1, 2**64):
        with pytest.raises(ValueError):
            service.apply(block, r['token'], bad_id, by=alice)
    assert not (block.work / 'seg_edit.npy').exists()
    proposal = service.proposals[r['token']]
    proposal['work'] += '-other'
    with pytest.raises(ValueError, match='工作目录'):
        service.apply(block, r['token'], 66, by=alice)
    proposal['work'] = str(block.work.resolve())
    # Editing another section does not invalidate this preview.
    block.paint(1, [(10, 10)], 0, 77, by=bob)
    service.apply(block, r['token'], 66, by=alice)
    block.undo(z=0, by=alice)
    r = service.predict(block, 0, BOX, STROKES, by=alice)
    block.paint(0, [(10, 10)], 0, 77, by=bob)
    block.undo(z=0, by=bob)
    with pytest.raises(ValueError, match='已变化'):
        service.apply(block, r['token'], 66, by=alice)
    r = service.predict(block, 0, BOX, STROKES, by=alice)
    service.proposals[r['token']]['created'] = time.monotonic() - 901
    with pytest.raises(ValueError, match='过期'):
        service.apply(block, r['token'], 66, by=alice)


def test_api_validation_and_apply(block, monkeypatch):
    from emqc.api.app import app
    from emqc.api.routers import annotate
    monkeypatch.setattr(annotate, '_block', lambda *a, **kw: block)
    c = TestClient(app)
    endpoint = '/api/v1/annotate/blocks/source/seed'
    good = dict(z=0, box=BOX, strokes=STROKES, annotator='tester')
    invalid = [
        dict(z=2), dict(box=[0, 0, 500, 80]), dict(box=[1, 1, 3, 3]),
        dict(strokes=[]), dict(strokes=[{'label': 0, 'points': [[20, 20]]}]),
        dict(strokes=[{'label': 1, 'points': [[0, 0]]}]),
        dict(strokes=[{'label': 1, 'points': [[46, 44]]}]),
        dict(strokes=[{'label': 1, 'points': [[38, 40]]}, {'label': 0, 'points': [[38, 40]]}]),
        dict(strokes=[{'label': 3, 'points': [[38, 40]]}]), dict(scale=0), dict(only_background=False),
    ]
    for change in invalid:
        r = c.post(endpoint + '/predict', json={**good, **change})
        assert r.status_code == 422, (change, r.text)
    r = c.post(endpoint + '/predict', json=good)
    assert r.status_code == 200, r.text
    assert not (block.work / 'seg_edit.npy').exists()
    result = c.post(endpoint + '/apply', json=dict(token=r.json()['token'], new_id='123', annotator='tester'))
    assert result.status_code == 200, result.text
    assert result.json()['edit']['kind'] == 'seed'
    assert result.json()['rev'] > 0


def test_box_edge_is_not_background_and_membrane_still_stops_growth():
    y, x = np.mgrid[:100, :140]
    radius = np.hypot(x - 65, y - 35)
    image = np.full((100, 140), 180, np.uint8)
    image[np.abs(radius - 28) < 2] = 20
    # The cell crosses the ROI top but is fully visible in the context halo.
    box = [25, 25, 108, 85]
    mask, warnings = segment(image, np.zeros_like(image), box, [{'label': 1, 'points': [[65, 40]]}])
    r = radius[25:85, 25:108]
    assert mask.shape == (60, 83)
    assert mask[0, r[0] < 23].all(), 'no artificial unfilled stripe on the ROI edge'
    assert mask[r < 23].all()
    assert not mask[r > 32].any(), 'context must not mean unconditional outward dilation'
    assert any('框边' in warning for warning in warnings)


def test_physical_image_edge_and_exclusions_remain_protected():
    y, x = np.mgrid[:80, :120]
    radius = np.hypot(x - 60, y - 3)
    image = np.full((80, 120), 180, np.uint8)
    image[np.abs(radius - 26) < 2] = 20
    labels = np.zeros(image.shape, np.uint64)
    labels[0:4, 62:67] = 9007199254740993
    box = [20, 0, 105, 60]
    strokes = [{'label': 1, 'points': [[48, 8]]}, {'label': 0, 'points': [[93, 10]]}]
    unconstrained, _ = segment(image, np.zeros_like(labels), box, strokes)
    assert unconstrained[0, 25:36].all(), 'cell can reach the true top image edge'
    mask, _ = segment(image, labels, box, strokes)
    assert mask[0, 25:30].all(), 'existing labels must not force a whole blank border'
    assert not mask[labels[:60, 20:105] != 0].any()
    assert not mask[10, 73], 'explicit negative seed cannot be painted'
    assert not mask[radius[:60, 20:105] > 30].any()


def test_full_image_requires_background_evidence():
    image = np.full((30, 40), 180, np.uint8)
    strokes = [{'label': 1, 'points': [[10, 15]]}]
    with pytest.raises(ValueError, match='红色排除'):
        segment(image, np.zeros_like(image), [0, 0, 40, 30], strokes)
    mask, _ = segment(image, np.zeros_like(image), [0, 0, 40, 30],
                      [*strokes, {'label': 0, 'points': [[30, 15]]}])
    assert mask[15, 10] and not mask[15, 30]
