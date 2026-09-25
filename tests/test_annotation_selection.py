"""Read-only edit footprints keep exact display coordinates and historical undo."""
import io

import numpy as np
from PIL import Image

from annotation_data import IDS, write_pairs
from emqc.annotate.store import Block
from test_annotate import ann_root, client, workdir  # shared API fixture definitions


def alpha(data):
    return np.asarray(Image.open(io.BytesIO(data)))[..., 3]


def test_edit_footprint_is_exact_readonly_and_handles_cross_slice_history(tmp_path):
    data = tmp_path / 'blocks' / 'pairs'
    write_pairs(data)
    block = Block(data, tmp_path / 'work')
    erase = block.paint(0, [(20, 4)], 0, 0)
    before = {p.name: p.read_bytes() for p in block.work.iterdir() if p.is_file()}
    mask = alpha(block.edit_mask_png(erase['n'], 0))
    assert mask.shape == (32, 64)
    assert mask[4, 20] > 0 and np.count_nonzero(mask) == 1
    assert before == {p.name: p.read_bytes() for p in block.work.iterdir() if p.is_file()}
    rec = block.merge(IDS[0], 77)
    for z in (0, 1):
        mask = alpha(block.edit_mask_png(rec['n'], z))
        assert np.array_equal(mask > 0, block.seg_slice(z) == 77)
    block.undo(0)
    import pytest
    with pytest.raises(KeyError):
        block.edit_mask_png(rec['n'], 0)
    assert alpha(block.edit_mask_png(rec['n'], 1)).any()


def test_edit_mask_api_and_retired_repair_routes(client):
    url = '/api/v1/annotate/blocks/b0'
    rec = client.post(url + '/paint', json={'z': 0, 'points': [[20, 3]], 'radius': 0, 'new_id': '0'}).json()['edit']
    target = url + f"/edits/{rec['n']}/mask.png"
    response = client.get(target, params={'z': 0})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    mask = alpha(response.content)
    assert mask.shape == (24, 32) and mask[3, 20] > 0 and np.count_nonzero(mask) == 1
    assert client.get(target, params={'z': 1}).status_code == 404
    assert client.get(target, params={'z': 99}).status_code == 404
    assert client.get(target, params={'z': -1}).status_code == 422
    client.post(url + '/undo?z=0')
    assert client.get(target, params={'z': 0}).status_code == 404
    for method, path in [('get', '/repair/scan'), ('post', '/repair/preview'), ('post', '/repair/apply')]:
        assert getattr(client, method)(url + path).status_code == 404
    assert all('/repair/' not in p for p in client.get('/openapi.json').json()['paths'])
