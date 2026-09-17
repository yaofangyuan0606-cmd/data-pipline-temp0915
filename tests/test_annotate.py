"""Annotation store + API: label index maps, exact 64-bit ids, copy-on-write fill / paint / undo."""
import io
import json

import numpy as np
import pytest
from PIL import Image

BIG = 9007199254740993  # 2**53 + 1: would lose precision as a JSON number, must survive as a string


@pytest.fixture(scope="module")
def ann_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("ann") / "blocks" / "demo"
    b = root / "b0"
    b.mkdir(parents=True)
    rng = np.random.default_rng(0)
    em = rng.integers(0, 255, (32, 24, 5), dtype=np.uint8)  # (x, y, z)
    seg = np.zeros((32, 24, 5), np.uint64)
    seg[:16] = 5
    seg[16:] = BIG
    seg[4:8, 4:8, :] = 0  # a hole inside label 5
    np.save(b / "em.npy", em)
    np.save(b / "seg.npy", seg)
    json.dump({"geometry": {"voxel_size_nm": [8, 8, 33]}, "dataset": {"id": "demo"}}, open(b / "meta.json", "w"))
    return root.parent


@pytest.fixture(scope="module")
def client(ann_root):
    from fastapi.testclient import TestClient

    from emqc.api.app import app
    from emqc.api.routers.annotate import reset_store
    from emqc.config import settings

    old = settings.annotate_root
    settings.annotate_root = ann_root
    reset_store()
    with TestClient(app) as c:
        yield c
    settings.annotate_root = old
    reset_store()


def _idx_map(png_bytes):
    a = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGB")).astype(np.uint16)
    return (a[..., 0] << 8) | a[..., 1]


def test_blocks_and_slices(client, ann_root):
    r = client.get("/api/v1/annotate/blocks").json()
    assert r["blocks"][0]["block_id"] == "b0" and r["blocks"][0]["nz"] == 5 and r["blocks"][0]["width"] == 32 and r["blocks"][0]["height"] == 24
    info = client.get("/api/v1/annotate/blocks/b0").json()
    assert info["shape_zyx"] == [5, 24, 32] and info["has_seg"] and info["voxel_size_nm"] == [8, 8, 33]

    em = np.load(ann_root / "demo" / "b0" / "em.npy")
    png = client.get("/api/v1/annotate/blocks/b0/em/2.png")
    assert png.status_code == 200
    got = np.asarray(Image.open(io.BytesIO(png.content)))
    assert got.shape == (24, 32) and np.array_equal(got, em[:, :, 2].T)  # (y, x) for display
    assert client.get("/api/v1/annotate/blocks/b0/em/9.png").status_code == 404


def test_label_index_map_and_exact_ids(client):
    tab = client.get("/api/v1/annotate/blocks/b0/labels/0.json").json()
    assert tab["ids"][0] == "0" and set(tab["ids"]) == {"0", "5", str(BIG)}
    assert sum(tab["counts"]) == 32 * 24
    idx = _idx_map(client.get("/api/v1/annotate/blocks/b0/labels/0.png").content)
    assert idx.shape == (24, 32)
    assert tab["ids"][idx[10, 20]] == str(BIG) and tab["ids"][idx[10, 2]] == "5" and tab["ids"][idx[5, 5]] == "0"
    assert client.get("/api/v1/annotate/blocks/b0/pick", params={"z": 0, "x": 20, "y": 10}).json()["id"] == str(BIG)
    assert client.get("/api/v1/annotate/blocks/b0/pick", params={"z": 0, "x": 40, "y": 0}).status_code == 404


def test_fill_is_copy_on_write_and_undoable(client, ann_root):
    b = ann_root / "demo" / "b0"
    r = client.post("/api/v1/annotate/blocks/b0/fill", json={"z": 0, "x": 5, "y": 5, "new_id": "5"}).json()
    assert r["edit"]["kind"] == "fill" and r["edit"]["n_px"] == 16 and r["edit"]["old_id"] == "0" and r["n_edits"] == 1
    assert (b / "seg_edit.npy").exists()
    assert np.load(b / "seg.npy", mmap_mode="r")[5, 5, 0] == 0, "the original must never change"
    assert np.load(b / "seg_edit.npy", mmap_mode="r")[5, 5, 0] == 5
    assert np.load(b / "seg_edit.npy", mmap_mode="r")[5, 5, 1] == 0, "fill is per slice"
    tab = client.get("/api/v1/annotate/blocks/b0/labels/0.json").json()
    assert tab["counts"][tab["ids"].index("0")] == 0

    # filling with the id already there is a no-op, not an edit
    assert client.post("/api/v1/annotate/blocks/b0/fill", json={"z": 0, "x": 5, "y": 5, "new_id": "5"}).json()["edit"] is None

    # whole-slice relabel of id 5 -> BIG, exact 64-bit id round-trips through JSON as a string
    r = client.post("/api/v1/annotate/blocks/b0/fill", json={"z": 0, "x": 1, "y": 1, "new_id": str(BIG), "whole_slice": True}).json()
    assert r["edit"]["n_px"] == 16 * 24 and r["n_edits"] == 2
    assert int(np.load(b / "seg_edit.npy", mmap_mode="r")[1, 1, 0]) == BIG
    assert client.get("/api/v1/annotate/blocks/b0/labels/0.json").json()["ids"] == ["0", str(BIG)]

    # paint a 3-px line with a brand-new id; the brush records the previous id of every pixel it touches
    r = client.post("/api/v1/annotate/blocks/b0/paint", json={"z": 1, "points": [[1, 1], [3, 1]], "radius": 0, "new_id": "42"}).json()
    assert r["edit"]["kind"] == "paint" and r["edit"]["n_px"] == 3 and r["edit"]["old_id"] is None
    seg = np.load(b / "seg_edit.npy", mmap_mode="r")
    assert [int(seg[x, 1, 1]) for x in (1, 2, 3)] == [42, 42, 42] and int(seg[4, 1, 1]) == 5
    assert client.post("/api/v1/annotate/blocks/b0/new-id").json()["id"] == str(BIG + 1)

    # undo restores exactly, most recent first
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"]["kind"] == "paint"
    seg = np.load(b / "seg_edit.npy", mmap_mode="r")
    assert [int(seg[x, 1, 1]) for x in (1, 2, 3)] == [5, 5, 5]
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"]["n"] == 2
    assert int(np.load(b / "seg_edit.npy", mmap_mode="r")[1, 1, 0]) == 5
    assert client.get("/api/v1/annotate/blocks/b0/edits").json()["n"] == 1
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["n_edits"] == 0
    assert np.array_equal(np.load(b / "seg_edit.npy"), np.load(b / "seg.npy"))
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"] is None


def test_page_renders(client):
    r = client.get("/annotate")
    assert r.status_code == 200 and "an-stage" in r.text and "annotate.js" in r.text


def test_merge_block_scope_and_legacy_undo(client, ann_root):
    b = ann_root / "demo" / "b0"
    # merge BIG into 5 across the whole block: every section changes, one edit record
    r = client.post("/api/v1/annotate/blocks/b0/merge", json={"from_id": str(BIG), "to_id": "5", "scope": "block"}).json()
    assert r["edit"]["kind"] == "merge" and r["edit"]["scope"] == "block" and r["edit"]["z"] is None
    assert r["edit"]["n_px"] == 16 * 24 * 5 and r["edit"]["n_slices"] == 5 and r["edit"]["old_id"] == str(BIG)
    seg = np.load(b / "seg_edit.npy", mmap_mode="r")
    assert not (np.asarray(seg) == BIG).any() and int(seg[20, 10, 4]) == 5
    for z in range(5):
        assert client.get(f"/api/v1/annotate/blocks/b0/labels/{z}.json").json()["ids"] == ["0", "5"]
    # merging an id that is not there is a no-op; from == to too
    assert client.post("/api/v1/annotate/blocks/b0/merge", json={"from_id": "777", "to_id": "5"}).json()["edit"] is None
    assert client.post("/api/v1/annotate/blocks/b0/merge", json={"from_id": "5", "to_id": "5"}).json()["edit"] is None
    # slice scope touches one section only
    r = client.post("/api/v1/annotate/blocks/b0/merge", json={"from_id": "5", "to_id": "9", "scope": "slice", "z": 2}).json()
    assert r["edit"]["n_slices"] == 1 and r["edit"]["z"] == 2
    seg = np.load(b / "seg_edit.npy", mmap_mode="r")
    assert int(seg[1, 1, 2]) == 9 and int(seg[1, 1, 3]) == 5
    assert client.post("/api/v1/annotate/blocks/b0/merge", json={"from_id": "5", "to_id": "9", "scope": "slice"}).status_code == 422

    # undo both (3-D undo restores per-voxel z)
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"]["kind"] == "merge"
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"]["n_slices"] == 5
    assert np.array_equal(np.load(b / "seg_edit.npy"), np.load(b / "seg.npy"))

    # a record written by the first version (no zs array) still undoes
    r = client.post("/api/v1/annotate/blocks/b0/fill", json={"z": 3, "x": 5, "y": 5, "new_id": "5"}).json()
    f = b / "edits" / f"{r['edit']['n']:06d}.npz"
    d = dict(np.load(f))
    d.pop("zs")
    np.savez_compressed(f, **d)
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"]["n"] == r["edit"]["n"]
    assert np.array_equal(np.load(b / "seg_edit.npy"), np.load(b / "seg.npy"))
