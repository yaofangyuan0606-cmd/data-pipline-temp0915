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
    # On disk the arrays are (volume X, volume Y, z); a displayed section is the transpose, so this volume shows
    # as 24 rows x 32 columns, with label 5 in columns 0..15, BIG in columns 16..31, and a hole at rows/cols 4..7.
    em = rng.integers(0, 255, (32, 24, 5), dtype=np.uint8)
    seg = np.zeros((32, 24, 5), np.uint64)
    seg[:16] = 5
    seg[16:] = BIG
    seg[4:8, 4:8, :] = 0  # a hole inside label 5
    np.save(b / "em.npy", em)
    np.save(b / "seg.npy", seg)
    json.dump({"geometry": {"voxel_size_nm": [8, 8, 33]}, "dataset": {"id": "demo"}}, open(b / "meta.json", "w"))
    return root.parent


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    return tmp_path_factory.mktemp("ann-work")


@pytest.fixture(scope="module")
def client(ann_root, workdir):
    from fastapi.testclient import TestClient

    from emqc.api.app import app
    from emqc.api.routers.annotate import reset_store
    from emqc.config import settings

    old = settings.annotate_root, settings.annotate_workdir
    settings.annotate_root, settings.annotate_workdir = ann_root, workdir
    reset_store()
    with TestClient(app) as c:
        yield c
    settings.annotate_root, settings.annotate_workdir = old
    reset_store()


def _idx_map(png_bytes):
    a = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGB")).astype(np.uint16)
    return (a[..., 0] << 8) | a[..., 1]


def test_blocks_and_slices(client, ann_root):
    r = client.get("/api/v1/annotate/blocks").json()
    assert r["blocks"][0]["block_id"] == "b0" and r["blocks"][0]["nz"] == 5 and r["blocks"][0]["width"] == 32 and r["blocks"][0]["height"] == 24
    info = client.get("/api/v1/annotate/blocks/b0").json()
    assert info["shape_zyx"] == [5, 24, 32], "displayed shape: rows = volume Y, columns = volume X"
    assert info["has_seg"] and info["voxel_size_nm"] == [8, 8, 33]
    assert info["em_source"] == "em.npy" and info["workdir"].endswith("b0")

    em = np.load(ann_root / "demo" / "b0" / "em.npy")
    png = client.get("/api/v1/annotate/blocks/b0/em/2.png")
    assert png.status_code == 200
    got = np.asarray(Image.open(io.BytesIO(png.content)))
    assert got.shape == (24, 32) and np.array_equal(got, em[:, :, 2].T), "the EM is served transposed, X horizontal"
    assert client.get("/api/v1/annotate/blocks/b0/em/9.png").status_code == 404


def test_label_index_map_and_exact_ids(client):
    tab = client.get("/api/v1/annotate/blocks/b0/labels/0.json").json()
    assert tab["ids"][0] == "0" and set(tab["ids"]) == {"0", "5", str(BIG)}
    assert sum(tab["counts"]) == 32 * 24
    idx = _idx_map(client.get("/api/v1/annotate/blocks/b0/labels/0.png").content)
    assert idx.shape == (24, 32)                       # (rows, cols) as displayed
    # columns 0..15 carry 5, columns 16..31 carry BIG, rows/cols 4..7 are the hole
    assert tab["ids"][idx[10, 20]] == str(BIG) and tab["ids"][idx[2, 10]] == "5" and tab["ids"][idx[5, 5]] == "0"
    # screen (x = column, y = row); x now indexes the volume's X, i.e. the array's axis 0
    assert client.get("/api/v1/annotate/blocks/b0/pick", params={"z": 0, "x": 20, "y": 10}).json()["id"] == str(BIG)
    assert client.get("/api/v1/annotate/blocks/b0/pick", params={"z": 0, "x": 10, "y": 2}).json()["id"] == "5"
    assert client.get("/api/v1/annotate/blocks/b0/pick", params={"z": 0, "x": 0, "y": 30}).status_code == 404


def test_fill_is_copy_on_write_and_undoable(client, ann_root, workdir):
    b = ann_root / "demo" / "b0"
    w = workdir / "b0"
    r = client.post("/api/v1/annotate/blocks/b0/fill", json={"z": 0, "x": 5, "y": 5, "new_id": "5"}).json()
    assert r["edit"]["kind"] == "fill" and r["edit"]["n_px"] == 16 and r["edit"]["old_id"] == "0" and r["n_edits"] == 1
    assert (w / "seg_edit.npy").exists() and (w / "edits.jsonl").exists()
    assert sorted(p.name for p in b.iterdir()) == ["em.npy", "meta.json", "seg.npy"], "the data directory must stay exactly as delivered"
    assert np.load(b / "seg.npy", mmap_mode="r")[5, 5, 0] == 0, "the original must never change"
    assert np.load(w / "seg_edit.npy", mmap_mode="r")[5, 5, 0] == 5
    assert np.load(w / "seg_edit.npy", mmap_mode="r")[5, 5, 1] == 0, "fill is per slice"
    tab = client.get("/api/v1/annotate/blocks/b0/labels/0.json").json()
    assert tab["counts"][tab["ids"].index("0")] == 0

    # filling with the id already there is a no-op, not an edit
    assert client.post("/api/v1/annotate/blocks/b0/fill", json={"z": 0, "x": 5, "y": 5, "new_id": "5"}).json()["edit"] is None

    # whole-slice relabel of id 5 -> BIG, exact 64-bit id round-trips through JSON as a string
    r = client.post("/api/v1/annotate/blocks/b0/fill", json={"z": 0, "x": 1, "y": 1, "new_id": str(BIG), "whole_slice": True}).json()
    assert r["edit"]["n_px"] == 16 * 24 and r["n_edits"] == 2
    assert int(np.load(w / "seg_edit.npy", mmap_mode="r")[1, 1, 0]) == BIG
    assert client.get("/api/v1/annotate/blocks/b0/labels/0.json").json()["ids"] == ["0", str(BIG)]

    # Paint a 3-px line in the background hole with a brand-new id.
    r = client.post("/api/v1/annotate/blocks/b0/paint", json={"z": 1, "points": [[4, 5], [6, 5]], "radius": 0, "new_id": "42"}).json()
    assert r["edit"]["kind"] == "paint" and r["edit"]["n_px"] == 3 and r["edit"]["old_id"] is None
    seg = np.load(w / "seg_edit.npy", mmap_mode="r")
    assert [int(seg[x, 5, 1]) for x in (4, 5, 6)] == [42, 42, 42] and int(seg[7, 5, 1]) == 0
    assert client.post("/api/v1/annotate/blocks/b0/new-id").json()["id"] == str(BIG + 1)

    # undo restores exactly, most recent first
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"]["kind"] == "paint"
    seg = np.load(w / "seg_edit.npy", mmap_mode="r")
    assert [int(seg[x, 5, 1]) for x in (4, 5, 6)] == [0, 0, 0]
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"]["n"] == 2
    assert int(np.load(w / "seg_edit.npy", mmap_mode="r")[1, 1, 0]) == 5
    assert client.get("/api/v1/annotate/blocks/b0/edits").json()["n"] == 1
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["n_edits"] == 0
    assert np.array_equal(np.load(w / "seg_edit.npy"), np.load(b / "seg.npy"))
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"] is None


@pytest.mark.parametrize("new_id", [42, BIG + 17])
def test_brush_only_fills_background_and_eraser_still_works(tmp_path, monkeypatch, new_id):
    from fastapi.testclient import TestClient
    from emqc.annotate.store import Block
    from emqc.api.app import app
    from emqc.api.routers import annotate

    source = tmp_path / "brush"
    source.mkdir()
    original = np.zeros((12, 7, 2), np.uint64)
    original[:3] = BIG
    original[5:7] = 2**63  # Historical gap labels remain protected as ordinary nonzero ids.
    original[10:] = new_id  # Even the selected label is left untouched.
    np.save(source / "em.npy", np.zeros(original.shape, np.uint8))
    np.save(source / "seg.npy", original)
    block = Block(source, tmp_path / "work")
    monkeypatch.setattr(annotate, "_block", lambda _: block)
    url = "/api/v1/annotate/blocks/brush"
    with TestClient(app) as c:
        # Cross cells and an existing gap label, interpolating between the image edges.
        stroke = {"z": 0, "points": [[0, 3], [11, 3]], "radius": 1, "new_id": str(new_id)}
        response = c.post(url + "/paint", json=stroke)
        assert response.status_code == 200
        result = response.json()
        assert result["edit"]["n_px"] == 15 and result["edit"]["new_id"] == str(new_id)
        expected = original.copy()
        for x in (3, 4, 7, 8, 9):
            expected[x, 2:5, 0] = new_id
        assert np.array_equal(block._seg(), expected)
        assert np.array_equal(np.load(source / "seg.npy"), original)
        with np.load(block.work / "edits" / "000001.npz") as edit:
            assert np.all(edit["old"] == 0), "undo must only record previously empty pixels"
        # Repainting or touching only a cell must not change labels or add undo entries.
        assert c.post(url + "/paint", json=stroke).json()["edit"] is None
        occupied = {"z": 0, "points": [[1, 3]], "radius": 0, "new_id": str(new_id)}
        assert c.post(url + "/paint", json=occupied).json()["edit"] is None
        assert len(block.edits()) == 1 and np.array_equal(block._seg(), expected)
        # The same endpoint is used by the eraser: new_id=0 must still remove labels.
        occupied["new_id"] = "0"
        assert c.post(url + "/paint", json=occupied).json()["edit"]["n_px"] == 1
        assert block.pick(0, 1, 3) == 0
        c.post(url + "/undo?z=0")
        assert np.array_equal(block._seg(), expected)
        c.post(url + "/undo?z=0")
        assert np.array_equal(block._seg(), original) and block.edits() == []


def test_page_renders(client):
    r = client.get("/annotate")
    assert r.status_code == 200 and "an-stage" in r.text and "annotate.js" in r.text


def test_annotation_is_its_own_workspace(client, workdir):
    """The annotation pages and the QC pages are two shells: each has its own nav, and the switcher is the only link between them."""
    ann = client.get("/annotate").text
    assert 'class="ws-annotate"' in ann and "ws-switch" in ann
    assert 'href="/annotate/blocks"' in ann and 'href="/annotate/guide"' in ann
    assert 'id="live"' not in ann, "the QC pipeline status pill does not belong on annotation pages"
    for qc_href in ('href="/crawl"', 'href="/patches"', 'href="/runs"', 'href="/checks"', 'href="/traces"', 'href="/delivery"'):
        assert qc_href not in ann, f"QC nav item {qc_href} leaked into the annotation shell"
    qc = client.get("/pipeline").text
    assert 'class="ws-qc"' in qc and "ws-switch" in qc and 'id="live"' in qc
    assert 'href="/annotate/blocks"' not in qc and 'href="/annotate/guide"' not in qc and "an-stage" not in qc

    blocks = client.get("/annotate/blocks")
    assert blocks.status_code == 200 and "b0" in blocks.text and "5 · 24 · 32" in blocks.text and str(workdir) in blocks.text
    guide = client.get("/annotate/guide")
    assert guide.status_code == 200 and "merge-pair" in guide.text and str(workdir) in guide.text
    summary = client.get("/api/v1/annotate/blocks").json()["blocks"][0]
    assert summary["em_source"] == "em.npy" and summary["voxel_size_nm"] == [8, 8, 33] and summary["path"].endswith("b0")


def test_merge_block_scope_and_legacy_undo(client, ann_root, workdir):
    b = ann_root / "demo" / "b0"
    w = workdir / "b0"
    # merge BIG into 5 across the whole block: every section changes, one edit record
    r = client.post("/api/v1/annotate/blocks/b0/merge", json={"from_id": str(BIG), "to_id": "5", "scope": "block"}).json()
    assert r["edit"]["kind"] == "merge" and r["edit"]["scope"] == "block" and r["edit"]["z"] is None
    assert r["edit"]["n_px"] == 16 * 24 * 5 and r["edit"]["n_slices"] == 5 and r["edit"]["old_id"] == str(BIG)
    seg = np.load(w / "seg_edit.npy", mmap_mode="r")
    assert not (np.asarray(seg) == BIG).any() and int(seg[20, 10, 4]) == 5   # axis 0 = 20 was BIG
    for z in range(5):
        assert client.get(f"/api/v1/annotate/blocks/b0/labels/{z}.json").json()["ids"] == ["0", "5"]
    # merging an id that is not there is a no-op; from == to too
    assert client.post("/api/v1/annotate/blocks/b0/merge", json={"from_id": "777", "to_id": "5"}).json()["edit"] is None
    assert client.post("/api/v1/annotate/blocks/b0/merge", json={"from_id": "5", "to_id": "5"}).json()["edit"] is None
    # slice scope touches one section only
    r = client.post("/api/v1/annotate/blocks/b0/merge", json={"from_id": "5", "to_id": "9", "scope": "slice", "z": 2}).json()
    assert r["edit"]["n_slices"] == 1 and r["edit"]["z"] == 2
    seg = np.load(w / "seg_edit.npy", mmap_mode="r")
    assert int(seg[1, 1, 2]) == 9 and int(seg[1, 1, 3]) == 5
    assert client.post("/api/v1/annotate/blocks/b0/merge", json={"from_id": "5", "to_id": "9", "scope": "slice"}).status_code == 422

    # undo both (3-D undo restores per-voxel z)
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"]["kind"] == "merge"
    assert client.post("/api/v1/annotate/blocks/b0/undo").json()["undone"]["n_slices"] == 5
    assert np.array_equal(np.load(w / "seg_edit.npy"), np.load(b / "seg.npy"))

    # a record written by the first version (no zs array) still undoes
    r = client.post("/api/v1/annotate/blocks/b0/fill", json={"z": 3, "x": 5, "y": 5, "new_id": "5"}).json()
    f = w / "edits" / f"{r['edit']['n']:06d}.npz"
    d = dict(np.load(f))
    d.pop("zs")
    np.savez_compressed(f, **d)
    assert client.post("/api/v1/annotate/blocks/b0/undo?z=3").json()["undone"]["n"] == r["edit"]["n"]
    assert np.array_equal(np.load(w / "seg_edit.npy"), np.load(b / "seg.npy"))


def test_merge_pairs_keep_first_and_leave_other_islands_and_slices(client, ann_root, workdir):
    from annotation_data import IDS, POINTS, write_pairs

    b = ann_root / "demo" / "pairs"
    original = write_pairs(b)
    client.get("/api/v1/annotate/blocks")  # discover the additional block
    url = "/api/v1/annotate/blocks/pairs"
    a, b_point, c, d = POINTS
    for body in [
        {"z": -1, "first": a, "second": b_point},
        {"z": 0, "first": (-1, 0), "second": b_point},
        {"z": 0, "first": a, "second": (64, 0)},
        {"z": 0, "first": (0, 0), "second": b_point},
    ]:
        assert client.post(url + "/merge-pair", json=body).status_code == 422
    w = workdir / "pairs"
    assert not (w / "seg_edit.npy").exists()
    result = client.post(url + "/merge-pair", json={"z": 0, "first": a, "second": b_point}).json()
    assert result["edit"]["scope"] == "component"
    assert result["edit"]["new_id"] == str(IDS[0]) and result["edit"]["n_px"] == 64
    expected = original.copy()
    expected[18:26, 2:10, 0] = IDS[0]
    assert np.array_equal(np.load(w / "seg_edit.npy"), expected)
    result = client.post(url + "/merge-pair", json={"z": 0, "first": c, "second": d}).json()
    assert result["edit"]["new_id"] == str(IDS[2]) and result["n_edits"] == 2
    after_first = expected.copy()
    expected[50:58, 2:10, 0] = IDS[2]
    assert np.array_equal(np.load(w / "seg_edit.npy"), expected)
    assert np.array_equal(np.load(b / "seg.npy"), original)
    same = client.post(url + "/merge-pair", json={"z": 0, "first": a, "second": b_point}).json()
    assert same["edit"] is None and same["n_edits"] == 2
    assert client.post(url + "/undo").json()["n_edits"] == 1
    assert np.array_equal(np.load(w / "seg_edit.npy"), after_first)
    assert client.post(url + "/undo").json()["n_edits"] == 0
    assert np.array_equal(np.load(w / "seg_edit.npy"), original)


def test_legacy_edits_in_data_dir_are_migrated(tmp_path):
    """Older builds wrote seg_edit.npy / edits into the block directory; opening the block moves them out."""
    from emqc.annotate.store import Block

    b = tmp_path / "blk"
    b.mkdir()
    np.save(b / "em.npy", np.zeros((8, 6, 2), np.uint8))
    np.save(b / "seg.npy", np.ones((8, 6, 2), np.uint64))
    np.save(b / "seg_edit.npy", np.full((8, 6, 2), 7, np.uint64))
    (b / "edits").mkdir()
    (b / "edits" / "000001.npz").write_bytes(b"x")
    (b / "edits.jsonl").write_text('{"n": 1}\n')
    work = tmp_path / "work"
    blk = Block(b, work)
    assert sorted(p.name for p in b.iterdir()) == ["em.npy", "seg.npy"]
    assert (work / "blk" / "seg_edit.npy").exists() and (work / "blk" / "edits" / "000001.npz").exists()
    assert blk.edits() == [{"n": 1}] and int(blk.pick(0, 0, 0)) == 7


def test_blocks_page_survives_an_unreadable_block(client, ann_root):
    """A half-copied block is listed with an error by the store; the page must render it as such, not 500."""
    broken = ann_root / "demo" / "zz_broken"
    broken.mkdir()
    (broken / "em.npy").write_bytes(b"not a numpy file")
    api = client.get("/api/v1/annotate/blocks").json()["blocks"]
    bad = [b for b in api if b["block_id"] == "zz_broken"]
    assert bad and "error" in bad[0]
    page = client.get("/annotate/blocks")
    assert page.status_code == 200
    assert "无法打开" in page.text and "zz_broken" in page.text and "1 个打不开" in page.text
    assert "b0" in page.text, "the healthy block is still listed"


def test_created_labels_persist_without_editing_pixels(tmp_path, monkeypatch):
    from emqc.annotate import store
    from annotation_data import write_pairs
    data = tmp_path / 'blocks' / 'palette'
    original = write_pairs(data)
    work = tmp_path / 'work'
    b = store.Block(data, work)
    # Force the first unused ID to collide with an existing display colour.
    real_color = store.label_color
    monkeypatch.setattr(store, 'label_color', lambda label: real_color(BIG) if label == BIG + 1 else real_color(label))
    first = b.new_id(0)
    assert first == BIG + 2
    assert b.labels_table(0)['created_ids'] == [str(first)]
    assert str(first) not in b.labels_table(0)['ids']
    assert b.edits() == [] and not (b.work / 'seg_edit.npy').exists()
    reopened = store.Block(data, work)
    second = reopened.new_id(1)
    assert second > first
    assert reopened.labels_table(0)['created_ids'] == [str(first), str(second)]
    assert reopened.labels_table(1)['created_ids'] == [str(first), str(second)]
    reopened.paint(0, [[12, 12]], 0, first)
    assert str(first) in reopened.labels_table(0)['ids']
    reopened.undo()
    assert reopened.created_ids() == [str(first), str(second)]
    assert np.array_equal(np.load(data / 'seg.npy'), original)
    assert sorted(p.name for p in data.iterdir()) == ['em.npy', 'meta.json', 'seg.npy']
    with pytest.raises(ValueError, match='只读'):
        store.Block(data, work, read_only=True).new_id()


def test_new_label_rejects_invalid_slice(client):
    assert client.post('/api/v1/annotate/blocks/b0/new-id?z=-1').status_code == 422
    assert client.post('/api/v1/annotate/blocks/b0/new-id?z=999').status_code == 422


def test_slice_history_and_undo_leave_other_slices_untouched(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from emqc.api.app import app
    from emqc.api.routers import annotate
    from emqc.annotate.store import Block
    from annotation_data import write_pairs
    data = tmp_path / 'blocks' / 'slice-history'
    original = write_pairs(data)
    b = Block(data, tmp_path / 'work')
    monkeypatch.setattr(annotate, '_block', lambda _: b)
    with TestClient(app) as c:
        url = '/api/v1/annotate/blocks/slice-history'
        for z, x, label in [(0, 12, '101'), (1, 12, '202'), (0, 13, '303')]:
            assert c.post(url + '/paint', json={'z': z, 'points': [[x, 12]], 'radius': 0, 'new_id': label}).status_code == 200
        history = c.get(url + '/edits?z=0&limit=1').json()
        assert history['n'] == 2 and [r['n'] for r in history['edits']] == [3]
        assert c.get(url + '/edits?z=1').json()['n'] == 1
        assert c.post(url + '/undo?z=0').json()['undone']['n'] == 3
        assert b.pick(0, 12, 12) == 101 and b.pick(1, 12, 12) == 202 and b.pick(0, 13, 12) == 0
        # The latest block edit is now on slice 1; undo on slice 0 must skip it.
        assert c.post(url + '/undo?z=0').json()['undone']['n'] == 1
        assert b.pick(0, 12, 12) == int(original[12, 12, 0]) and b.pick(1, 12, 12) == 202
        assert c.post(url + '/undo?z=0').json()['undone'] is None
        history = c.get(url + '/edits?z=0').json()
        assert (history['n'], history['edits'], history['editors']) == (0, [], [])
        assert [r['n'] for r in b.edits()] == [2]
        b = Block(data, tmp_path / 'work')
        rec = b.paint(0, [(12, 12)], 0, 404)
        assert rec['n'] == 3  # remaining history is never overwritten
        for endpoint in ['/edits?z=99', '/edits?z=-1']:
            assert c.get(url + endpoint).status_code == 422
        assert c.post(url + '/undo?z=99').status_code == 422
        b.undo(0)
        b.undo(1)
        assert np.array_equal(b._seg(), original)
        assert np.array_equal(np.load(data / 'seg.npy'), original)


def test_partial_block_undo_preserves_history_and_provenance(tmp_path):
    from emqc.annotate.store import Block
    from emqc.annotate.provenance import report
    path = tmp_path / 'block'
    path.mkdir()
    original = np.zeros((4, 3, 3), dtype=np.uint64)
    original[1:3, :, 0] = BIG
    original[1:3, :, 2] = BIG
    np.save(path / 'em.npy', np.zeros(original.shape, dtype=np.uint8))
    np.save(path / 'seg.npy', original)
    b = Block(path, tmp_path / 'work')
    rec = b.merge(BIG, 55, 'block')
    assert b.edits(1) == []  # a block operation need not affect every slice
    assert b.edits(0)[0]['n_px'] == 6
    b.paint(1, [(1, 1)], 0, 77)
    undone = b.undo(0)
    assert undone['n'] == rec['n'] and undone['n_px'] == 6 and undone['z'] == 0
    assert np.array_equal(b._seg()[:, :, 0], original[:, :, 0])
    assert b.pick(1, 1, 1) == 77 and b.pick(2, 1, 1) == 55
    assert b.edits(0) == [] and b.edits(2)[0]['n_px'] == 6
    remaining = b.edits()[0]
    assert remaining['n_px'] == 6 and remaining['n_slices'] == 1
    with np.load(b.work / 'edits' / '000001.npz') as saved:
        assert np.all(saved['zs'] == 2)
    b = Block(path, tmp_path / 'work')
    assert report(b, 0)['changed_px'] == 0
    assert report(b, 2)['sources']['manual']['pixels'] == 6
    assert report(b, 2)['sources']['unknown']['pixels'] == 0
    # Legacy callers can still undo whole remaining operations without a z argument.
    assert b.undo()['n'] == 2
    assert b.undo()['n'] == 1
    assert np.array_equal(b._seg(), original)
    assert np.array_equal(np.load(path / 'seg.npy'), original)
