import gzip
import json

import numpy as np
import pytest

from emqc.registry.readers import CorruptSliceError, ImageStackReader, MissingSliceError, NpyReader, PrecomputedRawReader, open_volume


def test_image_stack_missing_and_corrupt(data_root):
    d = data_root / "project_terminal/test/datasets/datasets/synthetic_small/em"
    r = ImageStackReader(d)
    assert r.shape == (40, 128, 128) and r.info.dtype == "uint8"
    assert r.info.n_missing == 1
    assert r.slice_status(9) == "missing" and r.slice_status(13) == "corrupt" and r.slice_status(0) == "ok"
    with pytest.raises(MissingSliceError):
        r.read_slice(9)
    with pytest.raises(CorruptSliceError):
        r.read_slice(13)
    cut = r.read_cutout(8, 11, 0, 16, 0, 16)
    assert cut.shape == (3, 16, 16) and cut[1].max() == 0 and cut[0].max() > 0
    assert open_volume(d).info.fmt == "image_stack"


def test_npy_axes(tmp_path):
    a = np.arange(2 * 3 * 4, dtype=np.uint8).reshape(4, 3, 2)  # (x, y, z)
    np.save(tmp_path / "em.npy", a)
    r = NpyReader(tmp_path / "em.npy", axes="xyz")
    assert r.shape == (2, 3, 4)
    assert np.array_equal(r.read_slice(1), a[:, :, 1].T)


def test_precomputed_raw(tmp_path):
    vol = (np.random.default_rng(0).random((8, 6, 4)) * 255).astype(np.uint8)  # (z, y, x)
    info = {"data_type": "uint8", "num_channels": 1, "type": "image", "scales": [{"key": "8_8_40", "encoding": "raw", "resolution": [8, 8, 40], "size": [4, 6, 8], "chunk_sizes": [[4, 3, 4]], "voxel_offset": [0, 0, 0]}]}
    (tmp_path / "info").write_text(json.dumps(info))
    key = tmp_path / "8_8_40"
    key.mkdir()
    for y0 in (0, 3):
        for z0 in (0, 4):
            chunk = vol[z0 : z0 + 4, y0 : y0 + 3, :]  # (z, y, x) == C order of (c, z, y, x)
            name = f"0-4_{y0}-{y0 + 3}_{z0}-{z0 + 4}"
            if z0 == 0:
                (key / name).write_bytes(chunk.tobytes())
            else:
                (key / (name + ".gz")).write_bytes(gzip.compress(chunk.tobytes()))
    r = PrecomputedRawReader(tmp_path)
    assert r.shape == (8, 6, 4)
    for z in range(8):
        assert np.array_equal(r.read_slice(z), vol[z])
    (key / "0-4_0-3_0-4").unlink()
    (key / "0-4_3-6_0-4").unlink()
    with pytest.raises(MissingSliceError):
        r.read_slice(0)
