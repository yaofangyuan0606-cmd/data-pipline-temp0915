"""Current per-pixel provenance, exact ids, undo, exports, and read-only comparison."""
import base64
import csv
import io
import json

import numpy as np
import pytest
from PIL import Image

from emqc.annotate.provenance import SOURCES, comparison, csv_report, report
from emqc.annotate.store import Block

BIG = 9007199254740993


@pytest.fixture
def block(tmp_path):
    path = tmp_path / "blocks" / "sample"
    path.mkdir(parents=True)
    np.save(path / "em.npy", np.arange(7 * 5 * 3, dtype=np.uint8).reshape(7, 5, 3))
    seg = np.zeros((7, 5, 3), dtype=np.uint64)
    seg[:2, :, :] = BIG
    np.save(path / "seg.npy", seg)
    return Block(path, tmp_path / "work")


def mask(block, *points):
    result = np.zeros(block.shape_zyx[1:], bool)
    for x, y in points:
        result[y, x] = True
    return result


def png(url):
    return np.asarray(Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))))


def labels(image):
    rgb = png(image["png"]).astype(np.uint16)
    return np.asarray(image["ids"], dtype=np.uint64)[(rgb[..., 0] << 8) | rgb[..., 1]]


def test_pristine_comparison_is_readonly_and_uses_display_axes(block):
    original = (block.path / "seg.npy").read_bytes()
    result = comparison(block, 1)
    assert result["report"]["shape_yx"] == [5, 7]
    assert result["report"]["changed_px"] == 0
    assert result["report"]["sources"]["manual"]["pixels"] == 0
    assert np.array_equal(png(result["em_png"]), block.em[:, :, 1].T)
    assert np.array_equal(labels(result["before"]), block._seg_ro[:, :, 1].T)
    assert np.array_equal(labels(result["before"]), labels(result["after"]))
    assert str(BIG) in result["before"]["ids"]
    assert (block.path / "seg.npy").read_bytes() == original
    assert not block.work.exists()


def test_mixed_labels_last_writer_undo_and_interpolation(block):
    rec = block.apply_mask(0, mask(block, (2, 1), (3, 1)), BIG, {"model": "SAM 2.1"})
    assert rec["source"] == "sam" and rec["provenance_version"] == 1
    block.paint(0, [(2, 1)], 0, 7)
    block.paint(0, [(2, 1)], 0, BIG)
    repaired = np.zeros(block.shape_zyx[1:], np.uint64)
    repaired[2, 4], repaired[2, 5] = BIG, 99
    block.apply_labels(0, repaired, mask(block, (4, 2), (5, 2)), {"interpolated": True, "source_sections": [1, 2]})
    data = report(block, 0)
    row = next(r for r in data["labels"] if r["id"] == str(BIG))
    assert row["before_px"] == 10 and row["current_px"] == 13 and row["mixed"]
    assert row["sources"] == dict(baseline=10, manual=1, sam=1, interpolation=1, assisted=0, unknown=0)
    assert data["changed_px"] == 4 and data["added_px"] == 4
    assert data["operations"][-1]["source_sections"] == [1, 2]
    assert data["operations"][0]["current_px"] == 1
    assert data["operations"][1]["current_px"] == 0
    block.undo()  # repair
    block.undo()  # manual BIG
    block.undo()  # manual 7
    undone = report(block, 0)
    assert undone["sources"]["sam"]["label_pixels"] == 2
    assert undone["sources"]["manual"]["pixels"] == 0
    assert undone["sources"]["interpolation"]["pixels"] == 0
    block.undo()
    assert report(block, 0)["changed_px"] == 0


def test_erase_and_whole_block_export_conserve_counts(block):
    block.merge(BIG, 44)
    block.paint(1, [(0, 0)], 0, 0)
    data = report(block)
    assert data["scope"] == "block" and data["total_px"] == 105
    assert data["changed_px"] == 30 and data["relabeled_px"] == 29 and data["removed_px"] == 1
    assert data["sources"]["manual"] == dict(pixels=30, label_pixels=29, background_pixels=1)
    assert sum(r["current_px"] for r in data["labels"]) == data["total_px"]
    assert sum(s["pixels"] for s in data["sources"].values()) == data["total_px"]
    assert all(sum(r["sources"].values()) == r["current_px"] for r in data["labels"])
    exported = list(csv.DictReader(io.StringIO(csv_report(data).lstrip("\ufeff"))))
    assert next(r for r in exported if r["label_id"] == str(BIG))["before_px"] == "30"
    assert next(r for r in exported if r["label_id"] == "44")["manual"] == "29"
    assert len(data["slices"]) == 3


def test_legacy_records_and_unlogged_changes_are_not_guessed(block):
    block.apply_mask(0, mask(block, (3, 2)), 8, {}, kind="smartfill")
    path = block.work / "edits" / "000001.npz"
    with np.load(path) as f:
        content = {k: f[k] for k in f.files if k != "zs"}
    np.savez_compressed(path, **content)
    log = block.edits()
    log[0].pop("source"); log[0].pop("provenance_version")
    (block.work / "edits.jsonl").write_text(json.dumps(log[0]) + "\n")
    block._seg_rw[6, 4, 0] = 8  # imported edit without any evidence
    block._seg_rw.flush()
    data = report(block, 0)
    assert data["sources"]["assisted"]["label_pixels"] == 1
    assert data["sources"]["unknown"]["label_pixels"] == 1
    assert data["sources"]["manual"]["pixels"] == 0
    path.unlink()
    data = report(block, 0)
    assert data["warnings"] and data["sources"]["unknown"]["pixels"] == 35
    assert report(block, 1)["sources"]["baseline"]["pixels"] == 35


def test_stale_and_unknown_operations_remain_unknown(block):
    block.apply_mask(0, mask(block, (3, 2)), 8, {}, kind="future-tool")
    assert report(block, 0)["sources"]["unknown"]["label_pixels"] == 1
    block.paint(0, [(4, 2)], 0, 9)
    block._seg_rw[4, 2, 0] = 10
    block._seg_rw.flush()
    data = report(block, 0)
    assert data["warnings"] and data["sources"]["manual"]["pixels"] == 0
    assert data["sources"]["unknown"]["label_pixels"] == 2


def test_sam_baseline_and_comparison_do_not_modify_work_files(block):
    block.meta["sam"] = {"model": "vit_b"}
    assert report(block, 0)["sources"]["sam"]["label_pixels"] == 10
    block.paint(0, [(0, 0)], 0, 0)
    files = [block.path / "seg.npy", block.work / "seg_edit.npy", block.work / "edits.jsonl", block.work / "edits" / "000001.npz"]
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in files}
    data = comparison(block, 0)
    assert data["report"]["sources"]["sam"]["label_pixels"] == 9
    assert data["report"]["sources"]["manual"]["background_pixels"] == 1
    assert png(data["sources_png"])[0, 0] == SOURCES.index("manual")
    assert before == {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in files}


def test_comparison_api_exports_bounds_and_no_seg(block, monkeypatch):
    from fastapi.testclient import TestClient
    from emqc.api.app import app
    from emqc.api.routers import annotate
    from emqc.annotate.store import AnnotateStore
    st = AnnotateStore(block.path.parent, block.work.parent)
    st._blocks[block.id] = block
    monkeypatch.setattr(annotate, "get_store", lambda **kwargs: st)
    with TestClient(app) as client:
        root = f"/api/v1/annotate/blocks/{block.id}"
        page = client.get("/annotate/compare")
        assert page.status_code == 200 and 'id="cmp-before"' in page.text and 'id="cmp-after"' in page.text
        assert 'class="ws-annotate"' in page.text
        response = client.get(root + "/compare/0")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert client.get(root + "/compare/3").status_code == 404
        assert client.get(root + "/provenance?z=-1").status_code == 404
        assert client.get(root + "/provenance?format=pdf").status_code == 422
        assert client.get("/api/v1/annotate/blocks/missing/compare/0").status_code == 404
        assert client.get(root + "/provenance").json()["scope"] == "block"
        csv_response = client.get(root + "/provenance?z=0&format=csv")
        assert str(BIG) in csv_response.text and "attachment" in csv_response.headers["content-disposition"]
        block.has_seg = False
        data = client.get(root + "/compare/0").json()
        assert data["report"]["has_seg"] is False and data["report"]["changed_px"] == 0


def test_cut_multiple_ids_and_missing_older_record(block):
    # Three components, representing the vector assignment made by a multi-piece cut.
    cut_mask = mask(block, (2, 2), (3, 2))
    block.apply_mask(0, cut_mask, 51, {}, kind="split")
    block._seg_rw[3, 2, 0] = 52
    block._seg_rw.flush()
    record = block.edits()[0]
    record["new_ids"] = [51, 52]
    (block.work / "edits.jsonl").write_text(json.dumps(record) + "\n")
    assert report(block, 0)["sources"]["unknown"]["label_pixels"] == 2
    block.paint(0, [(2, 2)], 0, 70)
    (block.work / "edits" / "000001.npz").unlink()
    data = report(block, 0)
    assert data["sources"]["manual"]["pixels"] == 1
    assert data["sources"]["unknown"]["pixels"] == 34


def test_merged_sam_baseline_keeps_original_labels_separate(block):
    original = np.load(block.path / "seg.npy")
    original[3, 1, 0], original[4, 1, 0] = BIG + 1, BIG + 2
    np.save(block.path / "seg.npy", original)
    block._seg_ro = np.load(block.path / "seg.npy", mmap_mode="r")
    block.meta["sam_merge"] = {"first_new_id": str(BIG + 1), "n_new_ids": 2}
    data = report(block, 0)
    assert data["sources"]["sam"]["label_pixels"] == 2
    assert data["sources"]["baseline"]["label_pixels"] == 10
    block.paint(0, [(3, 1)], 0, BIG + 3)
    assert report(block, 0)["sources"]["manual"]["label_pixels"] == 1
    assert report(block, 0)["sources"]["sam"]["label_pixels"] == 1


@pytest.mark.parametrize("log", ["null\n", '{"n":1,"kind":"sam","z":"0"}\n', '{"n":1,'])
def test_invalid_log_cannot_invent_sources(block, log):
    block.work.mkdir(parents=True)
    (block.work / "edits.jsonl").write_text(log)
    data = report(block, 0)
    assert data["sources"]["unknown"]["pixels"] == 35
    assert data["warnings"]


def test_comparison_discovery_never_migrates_legacy_and_survives_bad_logs(block, monkeypatch):
    from fastapi.testclient import TestClient
    from emqc.api.app import app
    from emqc.api.routers import annotate
    from emqc.config import settings
    # Legacy work is intentionally in the source directory, as in older deliveries.
    legacy = Block(block.path)
    legacy.paint(0, [(3, 2)], 0, 91)
    paths = [p for p in block.path.rglob("*") if p.is_file()]
    original = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in paths}
    monkeypatch.setattr(settings, "annotate_root", block.path.parent)
    monkeypatch.setattr(settings, "annotate_workdir", block.work.parent)
    monkeypatch.setattr(annotate, "_roots", lambda: [])
    annotate.reset_store()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            root = f"/api/v1/annotate/blocks/{block.id}"
            listing = client.get("/api/v1/annotate/comparison-blocks")
            assert listing.status_code == 200
            data = client.get(root + "/compare/0").json()
            assert data["report"]["changed_px"] == 1
            assert client.get(root + "/provenance?z=0").status_code == 200
            assert not block.work.exists()
            assert original == {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in paths}
            (block.path / "edits.jsonl").write_text('{"n":1,')
            annotate.reset_store()  # Include discovery on a fresh server.
            assert client.get("/api/v1/annotate/comparison-blocks").status_code == 200
            response = client.get(root + "/compare/0")
            assert response.status_code == 200 and response.json()["report"]["warnings"]
            assert not block.work.exists()
    finally:
        annotate.reset_store()


def test_readonly_and_editor_share_lock_and_read_fresh_work(block):
    viewer = Block(block.path, block.work.parent, read_only=True)
    assert viewer.lock is block.lock
    assert report(viewer, 0)["changed_px"] == 0
    block.paint(0, [(3, 2)], 0, 77)
    assert report(viewer, 0)["sources"]["manual"]["pixels"] == 1
    with pytest.raises(ValueError, match="只读"):
        viewer.paint(0, [(4, 2)], 0, 99)
    block.undo()
    assert report(viewer, 0)["changed_px"] == 0


def test_multicut_verifies_each_new_label_and_detects_swaps(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    np.save(source / "em.npy", np.zeros((30, 20, 1), np.uint8))
    np.save(source / "seg.npy", np.full((30, 20, 1), 7, np.uint64))
    block = Block(source, tmp_path / "work")
    rec = block.cut(0, [(9, 0), (9, 19), (19, 19), (19, 0)])
    assert len(rec["new_ids"]) == 2
    with np.load(block.work / "edits" / "000001.npz") as data:
        assert data["new"].shape == data["xs"].shape
        assert np.array_equal(data["new"], block._seg()[data["xs"], data["ys"], 0])
    assert report(block, 0)["sources"]["manual"]["label_pixels"] == rec["n_px"]
    current = block._seg()
    x, y = np.argwhere(current[:, :, 0] == int(rec["new_ids"][0]))[0]
    current[x, y, 0] = int(rec["new_ids"][1])
    current.flush()
    data = report(block, 0)
    assert data["sources"]["unknown"]["label_pixels"] == 1
    block.undo()
    assert np.all(block._seg() == 7)


@pytest.mark.parametrize("entry", [{"first_new_id": "broken"}, {"first_new_id": 5.5}, None])
def test_invalid_sam_metadata_is_not_presented_as_pristine(block, entry):
    block.meta["sam_merge"] = entry
    data = report(block, 0)
    assert data["warnings"] and data["sources"]["unknown"]["label_pixels"] == 10


def test_fractional_edit_coordinates_are_rejected(block):
    block.paint(0, [(3, 2)], 0, 77)
    path = block.work / "edits" / "000001.npz"
    with np.load(path) as f:
        arrays = dict(f)
    arrays["xs"] = arrays["xs"].astype(float) + .5
    np.savez_compressed(path, **arrays)
    data = report(block, 0)
    assert data["warnings"] and data["sources"]["manual"]["pixels"] == 0


def test_block_list_page_handles_unreadable_history(block, monkeypatch):
    from fastapi.testclient import TestClient
    from emqc.api.app import app
    from emqc.api.routers import annotate
    from emqc.annotate.store import AnnotateStore
    block.work.mkdir(parents=True)
    (block.work / "edits.jsonl").write_text('{"n":1,')
    store = AnnotateStore(block.path.parent, block.work.parent)
    store._blocks[block.id] = block
    monkeypatch.setattr(annotate, "get_store", lambda **kwargs: store)
    with TestClient(app) as client:
        response = client.get("/annotate/blocks")
        assert response.status_code == 200
        assert "1 个块的记录异常，未计入" in response.text
        assert "查看对比" in response.text and "记录异常" in response.text
        assert (block.work / "edits.jsonl").read_text() == '{"n":1,'
