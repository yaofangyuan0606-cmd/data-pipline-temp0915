"""Boundary-aware fill (智能填充), split / cut (分离 / 切割), SAM mask snapping — store level and through the API."""
import json

import numpy as np
import pytest

from emqc.annotate import boundary
from emqc.annotate.store import Block


def two_cells(H=64, W=96, wall_x=48, membrane=18, cytoplasm=200):
    """Synthetic EM: two cells side by side separated by a 2-px dark membrane, with an outer membrane ring; plus a
    dark organelle inside the left cell. Returns (em, left_mask, right_mask) in (rows, cols)."""
    em = np.full((H, W), cytoplasm, dtype=np.uint8)
    em[:2, :] = em[-2:, :] = membrane
    em[:, :2] = em[:, -2:] = membrane
    em[:, wall_x:wall_x + 2] = membrane  # shared wall
    em[20:30, 20:30] = membrane  # organelle outline (a filled dark blob)
    rng = np.random.default_rng(0)
    em = np.clip(em.astype(int) + rng.integers(-6, 7, em.shape), 0, 255).astype(np.uint8)  # light texture
    left = np.zeros((H, W), bool); left[2:-2, 2:wall_x] = True
    right = np.zeros((H, W), bool); right[2:-2, wall_x + 2:-2] = True
    return em, left, right


def test_membrane_map_marks_walls_not_cytoplasm():
    em, left, right = two_cells()
    b = boundary.membrane_map(em, 0.5)
    assert b[:, 48].all() and b[:, 49].all(), "the shared wall is boundary"
    assert b[25, 25], "the dark organelle is boundary too (it becomes a hole that gets filled)"
    assert b[10:50, 6:40].mean() < 0.35, "most of the cytoplasm is not"
    flat = np.full((64, 96), 20, np.uint8)
    assert not boundary.membrane_map(flat, 0.5).any(), "a flat section has no membranes at all"


def test_bounded_region_stops_at_the_membrane():
    """The point of the tool: from one click inside a cell whose label is wrong, take that cell and not its
    neighbour. The two cells share one id here, so a plain bucket fill would take both."""
    em, left, right = two_cells()
    seg = np.full(em.shape, 7, dtype=np.uint64)  # both cells wrongly share one id
    mask, info = boundary.bounded_region(em, seg, x=20, y=40, sensitivity=0.5, scope="same")
    assert info["seed_id"] == "7" and info["seed"] == [20, 40]
    assert (mask & left).sum() / left.sum() > 0.95, "nearly all of the left cell"
    assert mask[25, 25], "the organelle hole is filled: it belongs to the cell"
    assert (mask & right).sum() == 0, "not one pixel of the right cell, although they share the id"


def test_bounded_region_respects_label_scope_and_radius():
    em, left, right = two_cells()
    seg = np.zeros(em.shape, dtype=np.uint64)
    seg[left] = 7
    seg[right] = 9
    # scope=same from a pixel of 7 never enters 9's membrane half even though the EM wall is shared
    mask, _ = boundary.bounded_region(em, seg, 20, 40, scope="same")
    assert not (mask & (seg == 9)).any()
    # scope=any ignores labels: still stops at the membrane
    mask_any, _ = boundary.bounded_region(em, seg, 20, 40, scope="any")
    assert not mask_any[2:-2, 52:].any()
    # radius limit
    small, _ = boundary.bounded_region(em, seg, 20, 40, scope="same", max_radius=6)
    ys, xs = np.nonzero(small)
    assert small.sum() < 200 and np.hypot(xs - 20, ys - 40).max() <= 6 + 4.01
    # background scope: a cleared (0) region fills up to the membrane
    seg0 = np.zeros(em.shape, dtype=np.uint64); seg0[right] = 9
    m0, info0 = boundary.bounded_region(em, seg0, 20, 40, scope="same")
    assert info0["seed_id"] == "0" and m0[2:-2, 4:46].all() and not (m0 & (seg0 == 9)).any()


def test_click_on_a_membrane_is_moved_into_a_cell():
    em, left, right = two_cells()
    seg = np.zeros(em.shape, dtype=np.uint64)
    mask, info = boundary.bounded_region(em, seg, x=48, y=30, scope="any")  # exactly on the shared wall
    assert info["seed_used"] != [48, 30], "the seed moved off the membrane"
    assert mask.sum() > 1000 and ((mask & left).any() or (mask & right).any())
    flat = np.full((64, 96), 20, dtype=np.uint8)
    m, _ = boundary.bounded_region(flat, None, 20, 30, scope="any")
    assert m.all(), "a flat section has no membranes: the region is everything allowed"


def test_refine_mask_cuts_leaks_and_extends_to_the_membrane():
    em, left, right = two_cells()
    leaky = left.copy()
    leaky[10:20, 40:70] = True  # SAM leaked across the wall into the right cell
    leaky[:, 34:46] = False     # and stopped 12 px short of the wall elsewhere
    out = boundary.refine_mask(leaky, em, points=[(10, 10)], labels=[1], sensitivity=0.5)
    assert (leaky & right).sum() > 0 and (out & right).sum() == 0, "the leak into the neighbour is cut off"
    assert (out & left).sum() / left.sum() > 0.95 > (leaky & left).sum() / left.sum(), "and it grew back to the wall"
    assert boundary.refine_mask(np.zeros(em.shape, bool), em).sum() == 0


@pytest.fixture
def block(tmp_path):
    em, left, right = two_cells()
    Z = 3
    # A displayed section is the transpose of what is stored, so store the transpose: the block then *shows*
    # exactly the picture two_cells() drew, and the screen coordinates below keep their meaning.
    vol = np.repeat(em.T[:, :, None], Z, axis=2)
    seg = np.zeros((em.shape[1], em.shape[0], Z), dtype=np.uint64)
    # indices below are (displayed row, displayed column) transposed onto the stored array
    disp = np.zeros((em.shape[0], em.shape[1], Z), dtype=np.uint64)
    disp[2:-2, 2:-2, :] = 7   # both cells, wall included, wrongly share id 7 → one connected blob
    disp[2:-2, 52:-2, 1] = 0  # z1: the right cell is unlabelled ...
    disp[40:50, 60:70, 1] = 7  # ... except an island of 7 → id 7 has two disconnected pieces on z1
    seg = np.ascontiguousarray(disp.transpose(1, 0, 2))
    d = tmp_path / "blocks" / "demo" / "b1"
    d.mkdir(parents=True)
    np.save(d / "em.npy", vol)
    np.save(d / "seg.npy", seg)
    json.dump({"dataset": {"id": "demo"}}, open(d / "meta.json", "w"))
    return Block(d, tmp_path / "work")


def test_split_component_only_when_the_label_has_several_pieces(block):
    with pytest.raises(ValueError):  # z0: id 7 is one connected blob (the wall pixels carry 7 too)
        block.split_component(0, 10, 10)
    with pytest.raises(ValueError):
        block.split_component(1, 60, 5)  # background
    rec = block.split_component(1, 65, 45)  # the island
    assert rec["kind"] == "split" and rec["mode"] == "component" and rec["old_id"] == "7" and rec["new_id"] == "8"
    plane = block.seg_slice(1)
    assert plane[45, 65] == 8 and plane[10, 10] == 7 and rec["n_px"] == 100
    assert block.undo()["n"] == rec["n"] and block.seg_slice(1)[45, 65] == 7


def test_cut_line_splits_the_blob_largest_keeps_the_id(block):
    # vertical line from above the image to below it, along the wall at x=49: separates left (bigger, x 2..48) from right
    rec = block.cut(0, [(49, -5), (49, 70)])
    assert rec["kind"] == "split" and rec["mode"] == "line" and rec["cut_id"] == "7" and rec["pieces"] == 2
    plane = block.seg_slice(0)
    new = int(rec["new_id"])
    assert plane[30, 20] == 7, "left piece (larger) keeps 7"
    assert plane[30, 80] == new and plane[30, 49] in (7, new), "right piece is new; the line pixels joined a side"
    assert not ((plane != 7) & (plane != new) & (plane != 0)).any()
    assert np.array_equal(plane == 0, block.seg_slice(2) == 0), "background untouched"
    # a line that does not go all the way through leaves the blob whole
    with pytest.raises(ValueError):
        block.cut(0, [(49, 10), (49, 30)])
    with pytest.raises(ValueError):
        block.cut(2, [(0, 0), (1, 0)])  # only background (the outer ring is 0)
    assert block.undo()["n_px"] == rec["n_px"] and block.seg_slice(0)[30, 80] == 7


def test_cut_three_pieces_assigns_a_fresh_id_per_extra_piece(block):
    rec = block.cut(0, [(30, -5), (30, 70), (70, 70), (70, -5)])  # two vertical cuts drawn as one U-shaped stroke
    assert rec["pieces"] == 3 and len(rec["new_ids"]) == 2 and rec["new_ids"][0] == rec["new_id"]
    plane = block.seg_slice(0)
    ids = {int(plane[30, 10]), int(plane[30, 40]), int(plane[30, 85])}
    assert 7 in ids and len(ids) == 3


def test_smart_fill_api_preview_apply_and_stale_token(client_tools):
    c, block_id = client_tools
    r = c.post(f"/api/v1/annotate/blocks/{block_id}/smart-fill/preview", json={"z": 0, "x": 20, "y": 40, "sensitivity": 0.5, "scope": "same"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["n_px"] > 2000 and p["seed_id"] == "7" and p["mask_png"].startswith("data:image/png;base64,") and p["bbox"][2] <= 50
    # applying with the id already there is a no-op, with a fresh id it relabels only the left cell
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/smart-fill/apply", json={"token": p["token"], "new_id": "7"}).json()["edit"] is None
    p2 = c.post(f"/api/v1/annotate/blocks/{block_id}/smart-fill/preview", json={"z": 0, "x": 20, "y": 40}).json()
    r = c.post(f"/api/v1/annotate/blocks/{block_id}/smart-fill/apply", json={"token": p2["token"], "new_id": "42"})
    assert r.status_code == 200 and r.json()["edit"]["kind"] == "smartfill" and r.json()["edit"]["n_px"] == p2["n_px"]
    assert c.get(f"/api/v1/annotate/blocks/{block_id}/pick", params={"z": 0, "x": 20, "y": 40}).json()["id"] == "42"
    assert c.get(f"/api/v1/annotate/blocks/{block_id}/pick", params={"z": 0, "x": 80, "y": 40}).json()["id"] == "7"
    # a preview taken before an edit is refused afterwards
    stale = c.post(f"/api/v1/annotate/blocks/{block_id}/smart-fill/preview", json={"z": 0, "x": 80, "y": 40}).json()
    c.post(f"/api/v1/annotate/blocks/{block_id}/fill", json={"z": 0, "x": 80, "y": 40, "new_id": "43"})
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/smart-fill/apply", json={"token": stale["token"], "new_id": "44"}).status_code == 409
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/smart-fill/apply", json={"token": "0" * 32, "new_id": "1"}).status_code == 409
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/smart-fill/preview", json={"z": 0, "x": 999, "y": 40}).status_code == 404
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/smart-fill/preview", json={"z": 0, "x": 1, "y": 1, "scope": "nope"}).status_code == 422
    for _ in range(2):
        c.post(f"/api/v1/annotate/blocks/{block_id}/undo")


def test_split_and_cut_api(client_tools):
    c, block_id = client_tools
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/split", json={"z": 0, "x": 10, "y": 10}).status_code == 422
    r = c.post(f"/api/v1/annotate/blocks/{block_id}/split", json={"z": 1, "x": 65, "y": 45})
    assert r.status_code == 200 and r.json()["edit"]["mode"] == "component"
    r = c.post(f"/api/v1/annotate/blocks/{block_id}/cut", json={"z": 0, "points": [[49, 0], [49, 63]]})
    assert r.status_code == 200 and r.json()["edit"]["mode"] == "line" and r.json()["edit"]["pieces"] == 2
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/cut", json={"z": 0, "points": [[49, 10], [49, 20]]}).status_code == 422
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/cut", json={"z": 0, "points": [[1, 1]]}).status_code == 422
    e = c.get(f"/api/v1/annotate/blocks/{block_id}/edits").json()["edits"]
    assert [x["kind"] for x in e[:2]] == ["split", "split"]
    for _ in range(2):
        assert c.post(f"/api/v1/annotate/blocks/{block_id}/undo").json()["undone"]["kind"] == "split"


@pytest.fixture
def client_tools(block, tmp_path):
    from fastapi.testclient import TestClient

    from emqc.api.app import app
    from emqc.api.routers.annotate import reset_store
    from emqc.config import settings

    old = settings.annotate_root, settings.annotate_workdir, settings.annotate_extra_roots
    settings.annotate_root, settings.annotate_workdir, settings.annotate_extra_roots = block.path.parent.parent, tmp_path / "work", ""
    reset_store()
    with TestClient(app) as c:
        yield c, block.id
    settings.annotate_root, settings.annotate_workdir, settings.annotate_extra_roots = old
    reset_store()


# ----------------------------------------------------------------------------- Neuroglancer 跳转
H01_META = {
    "dataset": {"id": "h01-q", "em_source": "precomputed://https://storage.googleapis.com/h01-release/data/20210601/4nm_raw",
                "seg_source": "precomputed://https://storage.googleapis.com/h01-release/data/20210601/c3"},
    "geometry": {"origin": {"x": 355846, "y": 68275, "z": 1225, "unit": "mip1 voxel"},
                 "voxel_size_nm": [8, 8, 33], "offset_in_parent": {"y0": 512, "x0": 512}},
}


def test_neuroglancer_position_adds_origin_and_quadrant_offset():
    from emqc.annotate import neuroglancer as ng

    assert ng.global_position(H01_META, x=60, y=470, z=0) == [355846 + 512 + 60, 68275 + 512 + 470, 1225]
    no_origin = {"dataset": {"id": "mouse_30um"}, "geometry": {"voxel_size_nm": [30, 30, 30]}}
    assert ng.global_position(no_origin, 1, 2, 3) is None
    assert ng.link_for(no_origin, 1, 2, 3)["url"] is None, "a block we cannot locate gets no link, not a wrong one"
    assert "origin" in ng.link_for(no_origin, 1, 2, 3)["reason"]


def test_neuroglancer_only_passes_ids_the_public_viewer_knows():
    """The viewer serves the original c3 segmentation. Ids this platform invented (SAM pre-fill, 新建 ID) would
    select an unrelated cell there, so they must not be sent."""
    from emqc.annotate import neuroglancer as ng

    assert ng.segment_is_public(H01_META, 18861616579) is True
    assert ng.segment_is_public(H01_META, None) is False
    derived = {**H01_META, "sam_merge": {"first_new_id": 18876189027}}
    assert ng.segment_is_public(derived, 18861616579) is True, "a delivered id still resolves"
    assert ng.segment_is_public(derived, 18876189027) is False, "an id SAM invented does not"
    link = ng.link_for(derived, 10, 10, 0, 18876189027)
    assert link["segment"] is None and "公开的 c3 分割里没有它" in link["segment_note"]
    assert '"segments":' not in link["url"] and "%22segments%22%3A" not in link["url"]
    ok = ng.link_for(H01_META, 60, 470, 0, 18861616579)
    assert ok["segment"] == "18861616579" and "%22segments%22%3A" in ok["url"]
    assert ok["url"].startswith("https://h01-dot-neuroglancer-demo.appspot.com/#!")
    assert ok["physical_um"] == [round((355846 + 512 + 60) * 8 / 1000, 3), round((68275 + 512 + 470) * 8 / 1000, 3),
                                 round(1225 * 33 / 1000, 3)]


def test_neuroglancer_endpoint(client_tools):
    c, block_id = client_tools
    r = c.get(f"/api/v1/annotate/blocks/{block_id}/neuroglancer", params={"z": 0, "x": 10, "y": 10})
    assert r.status_code == 200
    d = r.json()
    assert d["url"] is None and "origin" in d["reason"], "the synthetic test block has no origin, so no link"
    assert c.get(f"/api/v1/annotate/blocks/{block_id}/neuroglancer", params={"z": 0, "x": 999, "y": 10}).status_code == 404


# ----------------------------------------------------------------------------- 修补损坏切片
def test_detect_damage_finds_bands_not_organelles():
    from emqc.annotate import interpolate as ip

    em, _, _ = two_cells()
    assert not ip.detect_damage(em).any(), "dark organelles and membranes are not damage"
    cut = em.copy()
    cut[20:40, :] = 0
    m = ip.detect_damage(cut)
    assert m[20:40, :].all() and not m[:18, :].any(), "a black band across the section is"
    assert ip.detect_damage(np.zeros_like(em)).all(), "a blank page is damage everywhere"


def test_interpolation_recovers_cells_and_flags_its_own_uncertainty():
    """A destroyed section is repaired from the cells' shapes above and below; the pixels where those two
    disagree are reported so the annotator can see where the fill is a guess."""
    from emqc.annotate import interpolate as ip

    H, W, K = 48, 48, 5
    seg = np.zeros((H, W, K), np.uint64)
    for k in range(K):                       # one cell drifting two pixels per section
        seg[10 + 2 * k:30 + 2 * k, 10:30, k] = 7
    truth = seg[:, :, 2].copy()
    hole = np.ones((H, W), bool)
    seg[:, :, 2] = 0                         # section 2 destroyed: labels gone too
    r = ip.interpolate_section(seg, 2, hole)
    assert r.interpolated is True and r.source_sections == (1, 3)
    inter = float(((r.labels == 7) & (truth == 7)).sum() / max(1, ((r.labels == 7) | (truth == 7)).sum()))
    assert inter > 0.75, f"the drifting cell is recovered (IoU {inter:.2f})"
    assert r.uncertain.any() and not r.uncertain[19, 19], "disagreement is flagged, the cell's core is not"
    assert r.stats["hole_px"] == H * W and r.n_px > 0


def test_interpolation_steps_over_a_second_destroyed_section():
    """Real black cuts come in runs. A neighbour that is itself blank must not be treated as data."""
    from emqc.annotate import interpolate as ip

    H, W, K = 40, 40, 6
    seg = np.zeros((H, W, K), np.uint64)
    seg[10:30, 10:30, :] = 5
    seg[:, :, 2] = 0
    seg[:, :, 3] = 0                          # two consecutive destroyed sections
    r = ip.interpolate_section(seg, 2, np.ones((H, W), bool))
    assert r.source_sections == (1, 4), "it reaches past the blank neighbour"
    assert "相邻切片也是坏的" in r.note
    assert float(((r.labels == 5) & (seg[:, :, 1] == 5)).sum() / 400) > 0.9
    empty = np.zeros((H, W, 3), np.uint64)
    empty[:, :, 1] = 0
    r2 = ip.interpolate_section(empty, 1, np.ones((H, W), bool))
    assert r2.n_px == 0 and "无法修补" in r2.note, "no good neighbour at all: refuse, do not invent"


def test_repair_api_preview_apply_and_undo(client_tools):
    c, block_id = client_tools
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/repair/preview", json={"z": 0}).status_code == 422, "no damage here"
    scan = c.get(f"/api/v1/annotate/blocks/{block_id}/repair/scan").json()
    assert scan["n"] == 0
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/repair/apply", json={"token": "0" * 32}).status_code == 409
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/repair/preview", json={"z": 99}).status_code == 404


def test_apply_labels_writes_many_ids_and_undoes_exactly(block):
    """The repair writes a different id per pixel, which no other operation does."""
    original = np.load(block.path / "seg.npy").copy()
    labels = np.zeros(block.shape_zyx[1:], np.uint64)
    where = np.zeros(labels.shape, bool)
    labels[5:9, 5:9] = 111
    labels[5:9, 9:13] = 222
    where[5:9, 5:13] = True
    rec = block.apply_labels(0, labels, where, {"interpolated": True, "source_sections": [0, 2]})
    assert rec["kind"] == "repair" and rec["interpolated"] is True and rec["n_px"] == 32
    assert "2 个 id" in rec["new_id"]
    plane = block.seg_slice(0)
    assert int(plane[6, 6]) == 111 and int(plane[6, 10]) == 222
    assert np.array_equal(np.load(block.path / "seg.npy"), original), "the delivered array is never written"
    block.undo()
    assert np.array_equal(np.load(block.work / "seg_edit.npy"), original)
