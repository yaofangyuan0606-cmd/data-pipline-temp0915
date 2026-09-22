"""SAM membrane refinement, retired API routes, Neuroglancer links and section repair."""
import json
import pathlib

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


def test_retired_annotation_routes_are_unavailable_and_do_not_write(client_tools, block):
    client, block_id = client_tools
    root = f"/api/v1/annotate/blocks/{block_id}"
    original = (block.path / "seg.npy").read_bytes()
    paths = client.get("/openapi.json").json()["paths"]
    for suffix, body in [
        ("smart-fill/preview", {"z": 0, "x": 20, "y": 40}),
        ("smart-fill/apply", {"token": "a" * 32, "new_id": "42"}),
        ("split", {"z": 1, "x": 65, "y": 45}),
        ("cut", {"z": 0, "points": [[49, 0], [49, 63]]}),
    ]:
        assert client.post(f"{root}/{suffix}", json=body).status_code == 404
        assert f"/api/v1/annotate/blocks/{{block_id}}/{suffix}" not in paths
    assert client.get(root + "/edits").json()["n"] == 0
    assert not block.work.exists()
    assert (block.path / "seg.npy").read_bytes() == original


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
    assert ok["position_um"] == [round((355846 + 512 + 60) * 8 / 1000, 3), round((68275 + 512 + 470) * 8 / 1000, 3),
                                 round(1225 * 33 / 1000, 3)]


def test_neuroglancer_endpoint(client_tools):
    c, block_id = client_tools
    r = c.get(f"/api/v1/annotate/blocks/{block_id}/neuroglancer", params={"z": 0, "x": 10, "y": 10})
    assert r.status_code == 200
    d = r.json()
    assert d["url"] is None and "origin" in d["reason"], "the synthetic test block has no origin, so no link"
    assert c.get(f"/api/v1/annotate/blocks/{block_id}/neuroglancer", params={"z": 0, "x": 999, "y": 10}).status_code == 404


# ----------------------------------------------------------------------------- 细胞间隙
def test_gap_label_only_ever_lands_on_unlabelled_pixels(block):
    """「细胞间隙」是通用保留标签，用得很频繁，所以规矩要硬：它只落在 id 为 0 的像素上，永远不盖掉细胞。
    画笔扫过细胞、SAM 掩膜压到细胞、修补说这里是间隙——三条路都要被挡住。"""
    from emqc.annotate.labels import GAP_ID, GAP_NAME

    before = block.seg_slice(1).copy()                       # z1: 左半是细胞 7，右半是 0
    assert (before == 7).any() and (before == 0).any()
    # 画笔：一笔横跨细胞和空白
    rec = block.paint(1, [(40, 30), (70, 30)], 6, GAP_ID)
    after = block.seg_slice(1)
    assert rec["kind"] == "paint" and rec["n_px"] > 0
    assert np.array_equal(after[before == 7], before[before == 7]), "细胞 7 的像素一个都没变"
    assert ((after == GAP_ID) & (before == 0)).sum() == rec["n_px"], "写进去的全是原来为 0 的像素"
    block.undo()
    # SAM 掩膜：整片压下去
    rec = block.apply_mask(1, np.ones(block.shape_zyx[1:], bool), GAP_ID, {"model": "test"})
    after = block.seg_slice(1)
    assert np.array_equal(after[before != 0], before[before != 0])
    assert (after[before == 0] == GAP_ID).all()
    block.undo()
    # 填充：点在细胞上要被拒绝，并说明原因；点在空白上正常
    with pytest.raises(ValueError, match=GAP_NAME):
        block.fill(1, 10, 10, GAP_ID)
    rec = block.fill(1, 60, 30, GAP_ID)
    assert rec and int(block.seg_slice(1)[30, 60]) == GAP_ID
    assert np.array_equal(block.seg_slice(1)[before == 7], before[before == 7])
    block.undo()
    # 修补：邻片说是间隙，也不许盖洞里已有的细胞
    labels = np.full(block.shape_zyx[1:], GAP_ID, np.uint64)
    rec = block.apply_labels(1, labels, np.ones(labels.shape, bool), {"interpolated": True})
    after = block.seg_slice(1)
    assert np.array_equal(after[before != 0], before[before != 0]) and (after[before == 0] == GAP_ID).all()
    block.undo()
    assert np.array_equal(block.seg_slice(1), before), "四次撤销后逐像素还原"


def test_gap_label_cannot_be_merged_and_does_not_inflate_new_ids(block):
    """并进去会删掉一个细胞，并出来会把整片间隙染成一个细胞；而且它是 2**63，不能把「最大 id + 1」顶到天上去。"""
    from emqc.annotate import neuroglancer
    from emqc.annotate.labels import GAP_ID

    assert block.paint(1, [(60, 30)], 3, GAP_ID)["n_px"] > 0    # z1 右半是空的，间隙真的写进卷里了
    assert int(block.seg_slice(1)[30, 60]) == GAP_ID
    with pytest.raises(ValueError, match="合并"):
        block.merge(GAP_ID, 7)
    with pytest.raises(ValueError, match="合并"):
        block.merge(7, GAP_ID)
    with pytest.raises(ValueError, match="合并"):
        block.merge_pair(1, (10, 10), (60, 30))                 # 细胞 7 ← 间隙
    with pytest.raises(ValueError, match="合并"):
        block.merge_pair(1, (60, 30), (10, 10))                 # 间隙 ← 细胞 7
    assert block.max_id() == 7, "max_id 忽略保留 id"
    assert block.new_id() < GAP_ID // 2, "新建的 id 仍然是小数字，没有被 2**63 顶上去"
    assert neuroglancer.segment_is_public({"dataset": {"seg_source": "h01-release/x"}}, GAP_ID) is False


def test_gap_label_constant_is_shared_by_server_and_page(client_tools):
    """前端硬编码了同一个数字和颜色；服务端通过 info 把它交出来，两边必须一致。"""
    from emqc.annotate.labels import GAP_COLOR, GAP_ID, GAP_NAME
    from emqc.annotate.store import label_color

    c, block_id = client_tools
    info = c.get(f"/api/v1/annotate/blocks/{block_id}").json()
    assert info["gap_id"] == str(GAP_ID) == "9223372036854775808" and info["gap_name"] == GAP_NAME
    assert tuple(info["gap_color"]) == GAP_COLOR == label_color(GAP_ID)
    js = (pathlib.Path(__file__).resolve().parents[1] / "emqc/api/static/annotate.js").read_text()
    assert f'GAP_ID = "{GAP_ID}"' in js and f"GAP_COLOR = [{GAP_COLOR[0]}, {GAP_COLOR[1]}, {GAP_COLOR[2]}]" in js
    # 用它填色走 API：只动 0 像素，撤销还原
    r = c.post(f"/api/v1/annotate/blocks/{block_id}/paint", json={"z": 1, "points": [[40, 30], [70, 30]], "radius": 6, "new_id": str(GAP_ID)})
    assert r.status_code == 200 and r.json()["edit"]["n_px"] > 0
    r = c.post(f"/api/v1/annotate/blocks/{block_id}/fill", json={"z": 1, "x": 10, "y": 10, "new_id": str(GAP_ID)})
    assert r.status_code in (409, 422), "往细胞上填间隙要被拒绝而不是 500"


# ----------------------------------------------------------------------------- 跨片取色
def test_neighbour_lookup_finds_the_colour_the_current_slice_is_missing(block):
    """漏标的那一片没有颜色可吸——取色要去最近一张有标签的邻片上拿，并且说清楚是从哪一片拿的。

    fixture 里 z1 的右半边是空的（模拟分割漏标），z0 和 z2 上那里是 id 7。"""
    from emqc.annotate import neighbour

    assert int(block.seg_slice(1)[30, 60]) == 0, "这一点在本片确实没有标签"
    r = neighbour.lookup(block, 1, x=60, y=30)
    assert r["found"] and r["id"] == "7"
    assert r["distance"] == 1 and r["z_src"] in (0, 2), "取最近的那一片"
    assert r["searched"] == [0, 2]


def test_neighbour_lookup_votes_over_a_mask_and_reports_the_runner_up(tmp_path):
    """SAM 给出一块区域时按多数投票定色，但少数派也要报出来——一块区域可能横跨两个细胞。"""
    from emqc.annotate import neighbour

    d = tmp_path / "b"
    d.mkdir()
    np.save(d / "em.npy", np.zeros((20, 20, 3), np.uint8))
    seg = np.zeros((20, 20, 3), np.uint64)
    seg[:12, :, 0] = 11          # 邻片 z0：上面一大块是 11
    seg[12:, :, 0] = 22          # 下面一小块是 22
    np.save(d / "seg.npy", seg)
    json.dump({"dataset": {"id": "demo"}}, open(d / "meta.json", "w"))
    b = Block(d, tmp_path / "work")

    mask = np.zeros(b.shape_zyx[1:], bool)
    mask[:, 5:17] = True         # 显示方向上横跨 11 和 22，11 占多数
    r = neighbour.lookup(b, 1, mask=mask)
    assert r["found"] and r["id"] == "11" and r["z_src"] == 0
    assert 0 < r["share"] < 1 and r["others"] and r["others"][0]["id"] == "22"


def test_neighbour_lookup_lists_every_candidate_slice_for_manual_choice(tmp_path):
    """自动挑的是最近的一片，可能不是想要的那个细胞——半径内每一片的答案都要列出来让人自己选。"""
    from emqc.annotate import neighbour

    d = tmp_path / "b"
    d.mkdir()
    np.save(d / "em.npy", np.zeros((10, 10, 7), np.uint8))
    seg = np.zeros((10, 10, 7), np.uint64)
    seg[:, :, 1] = 11          # z1（相隔 2）是 11
    seg[:, :, 5] = 22          # z5（相隔 2，另一侧）是 22
    seg[:, :, 6] = 33          # z6（相隔 3）是 33
    np.save(d / "seg.npy", seg)
    json.dump({"dataset": {"id": "demo"}}, open(d / "meta.json", "w"))
    b = Block(d, tmp_path / "work")

    r = neighbour.lookup(b, 3, x=5, y=5)
    assert r["found"] and r["picked"] == "auto" and r["distance"] == 2
    got = [(c["z_src"], c["id"]) for c in r["candidates"]]
    assert got == sorted(got, key=lambda t: abs(t[0] - 3)), "按距离从近到远"
    assert set(got) == {(1, "11"), (5, "22"), (6, "33")}, "半径内每一片都列出来，不只是自动挑中的那片"

    # 同一个颜色连着好几片都在是常态，列成好几行只占地方——一种颜色一行，记下它出现在哪几片
    d2 = tmp_path / "b2"          # 另起一个目录：覆盖上面那个块的 seg.npy 会把 b 也改掉
    d2.mkdir()
    np.save(d2 / "em.npy", np.zeros((10, 10, 7), np.uint8))
    seg2 = np.zeros((10, 10, 7), np.uint64)
    seg2[:, :, 0] = seg2[:, :, 1] = seg2[:, :, 5] = 11      # 同一个 id，三片
    seg2[:, :, 6] = 22
    np.save(d2 / "seg.npy", seg2)
    json.dump({"dataset": {"id": "demo"}}, open(d2 / "meta.json", "w"))
    b2 = Block(d2, tmp_path / "work2")
    r2 = neighbour.lookup(b2, 3, x=5, y=5)
    ids = [c["id"] for c in r2["candidates"]]
    assert ids == ["11", "22"], f"一种颜色一行，去重后应只剩两种，实际 {ids}"
    first = r2["candidates"][0]
    # z1 和 z5 距离都是 2，同距离时取下方那一侧，和自动挑选的规则一致
    assert first["z_src"] == 1 and first["distance"] == 2, "留最近的那一片"
    assert first["n_slices"] == 3 and first["slices"] == [1, 5, 0], "记下它出现在哪几片，按由近及远"

    # 指定某一片：只看那片，不搜
    m = neighbour.lookup(b, 3, x=5, y=5, z_src=6)
    assert m["found"] and m["id"] == "33" and m["picked"] == "manual" and m["searched"] == [6]
    blank = neighbour.lookup(b, 3, x=5, y=5, z_src=4)
    assert blank["found"] is False and "z4" in blank["reason"], "指定的那片没有标签就直说，不退回自动"
    with pytest.raises(ValueError):
        neighbour.lookup(b, 3, x=5, y=5, z_src=3)
    with pytest.raises(IndexError):
        neighbour.lookup(b, 3, x=5, y=5, z_src=99)


def test_neighbour_lookup_refuses_to_guess_and_writes_nothing(block):
    """前后都没有标签时如实说没有，不猜；而且整个过程一个像素都不写。"""
    from emqc.annotate import neighbour

    before_delivery = np.load(block.path / "seg.npy").copy()
    r = neighbour.lookup(block, 1, x=0, y=0, radius=2)      # 角上是背景，上下片也都是背景
    assert r["found"] is False and "没有标签" in r["reason"]
    assert np.array_equal(np.load(block.path / "seg.npy"), before_delivery), "交付目录只读"
    assert not (block.work / "seg_edit.npy").exists(), "取色不该把工作副本也建出来"
    with pytest.raises(IndexError):
        neighbour.lookup(block, 99, x=1, y=1)


def test_neighbour_label_api_by_point_and_by_sam_mask(client_tools):
    """接口两种用法：手工填充用「一个点」，SAM 点填充用「一块掩膜」。"""
    import time

    from emqc.annotate.sam import revision, service as sam_service

    c, block_id = client_tools
    url = f"/api/v1/annotate/blocks/{block_id}/neighbour-label"
    r = c.post(url, json={"z": 1, "x": 60, "y": 30})
    assert r.status_code == 200 and r.json()["found"] and r.json()["id"] == "7"
    assert c.post(url, json={"z": 99, "x": 1, "y": 1}).status_code == 404
    assert c.post(url, json={"z": 1, "token": "0" * 32}).status_code == 409, "过期或不属于本块的预览"

    from emqc.api.routers.annotate import _block as get_block

    b = get_block(block_id)
    mask = np.zeros(b.shape_zyx[1:], bool)
    mask[20:40, 55:70] = True                     # z1 上空着的那一块
    token = "c" * 32
    sam_service.proposals[token] = {"path": str(b.path.resolve()), "work": str(b.work.resolve()), "z": 1,
                                    "mask": mask, "revision": revision(b), "created": time.monotonic(),
                                    "score": .9, "points": [(60, 30)], "labels": [1], "box": None,
                                    "only_background": True, "candidate": 0}
    try:
        got = c.post(url, json={"z": 1, "token": token}).json()
        assert got["found"] and got["id"] == "7" and got["share"] > 0.9
        assert token in sam_service.proposals, "取色不能把预览消费掉，后面还要用它来填"
    finally:
        sam_service.proposals.pop(token, None)


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
    # the reported uncertain count is the hatched region — filled AND disputed. Reporting the raw `uncertain` mask
    # let the preview claim more disputed pixels than it had filled, and more than the overlay actually hatches.
    assert r.stats["uncertain_px"] == int((r.uncertain & (r.labels != 0)).sum())
    assert r.stats["uncertain_px"] <= r.n_px, "a subset of what was filled, never more"


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
    # stats has to be complete even on that refusal: the preview reads it unconditionally, and an empty dict here
    # turned "no usable neighbour" into a KeyError and a 500 instead of a message the annotator can read
    assert r2.stats["hole_px"] == H * W and r2.stats["unfilled_px"] == H * W and r2.stats["kept_px"] == 0


def test_repair_api_preview_apply_and_undo(client_tools):
    c, block_id = client_tools
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/repair/preview", json={"z": 0}).status_code == 422, "no damage here"
    scan = c.get(f"/api/v1/annotate/blocks/{block_id}/repair/scan").json()
    assert scan["n"] == 0
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/repair/apply", json={"token": "0" * 32}).status_code == 409
    assert c.post(f"/api/v1/annotate/blocks/{block_id}/repair/preview", json={"z": 99}).status_code == 404


def test_detect_damage_ignores_dark_tissue_however_big(block):
    """The size rule this replaced flagged 46 healthy sections out of 400 real ones — blood vessel lumens and myelin
    are near-black and can run to thousands of pixels. Damage is told apart by shape: it spans the section."""
    from emqc.annotate import interpolate as ip

    em, _, _ = two_cells()
    blob = em.copy()
    blob[20:40, 20:40] = 0                        # 400 px of near-black, compact — a vessel, not a cut
    assert not ip.detect_damage(blob).any(), "a compact dark blob is tissue, whatever its area"
    crack = em.copy()
    crack[:, 40:43] = 0                           # 3 px wide, but it runs the whole height
    assert ip.detect_damage(crack)[:, 41].all(), "a thin crack that spans the section is damage"
    assert not ip.detect_damage(crack)[:, :20].any(), "and only the crack is"


def test_repair_never_clears_an_existing_label(block):
    """A 0 in the proposal means "nobody claimed this pixel", never "this pixel is background". Writing those zeros
    would wipe delivered labels wherever the hole was detected too eagerly, which is the one thing repair must not do."""
    before = block.seg_slice(0).copy()
    ry, rx = (int(v) for v in np.argwhere(before != 0)[0])     # a pixel that already carries a delivered id
    labels = np.zeros(block.shape_zyx[1:], np.uint64)
    where = np.ones(labels.shape, bool)                        # "the whole section is damaged", nothing claimed
    assert block.apply_labels(0, labels, where, {"interpolated": True}) is None, "nothing to write, nothing written"
    assert np.array_equal(block.seg_slice(0), before)
    labels[1, 1] = 99                                          # (1, 1) is outside both cells, so it really is background
    rec = block.apply_labels(0, labels, where, {"interpolated": True})
    assert rec["n_px"] == 1, "only the claimed pixel"
    assert int(block.seg_slice(0)[ry, rx]) == int(before[ry, rx]), "the labelled pixel keeps its id"
    assert int(block.seg_slice(0)[1, 1]) == 99
    block.undo()
    assert np.array_equal(block.seg_slice(0), before)


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
