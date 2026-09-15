import json

from sqlalchemy import select

from emqc.db import session_scope
from emqc.db.models import Block, Dataset, ETLMetric, QCBlock, QCFinding, QCRun, QCSlice
from emqc.qc import catalog


def test_catalog_has_all_16_checks():
    names = {c["name"] for c in catalog()}
    assert len(names) == 16
    assert {"missing_slice", "corrupt_slice", "blank_slice", "severe_blur", "brightness_jump", "contrast_drift", "charging_artifact", "contamination", "crack", "saturation"} <= names
    assert {"slice_jump", "z_order_error", "global_misalignment", "local_misalignment", "local_deformation", "section_deformation"} <= names
    stubs = {c["name"] for c in catalog() if not c["implemented"]}
    assert stubs == set(), f"16 项检查应全部实现，仍是 stub 的: {stubs}"


def test_registry(registered, data_root):
    with session_scope() as s:
        ds = s.get(Dataset, registered)
        assert ds.size_z == 40 and ds.size_y == 128 and ds.size_x == 128
        assert ds.size_class == "large"  # forced by dataset.json
        assert ds.species == "mouse" and ds.voxel_size_x_nm == 8
        types = {a.asset_type for a in ds.assets}
        assert {"em_image", "gt_segmentation", "model_prediction", "synapse_prediction", "mitochondria_prediction", "skeleton", "agent_trace"} <= types
        assert {v.kind for v in ds.versions} == {"data", "algo", "model", "experiment"}
        assert [b.block_id for b in sorted(ds.blocks, key=lambda b: b.z_start)] == ["z00000-00019", "z00020-00039"]


def test_qc_run_detects_injected_defects(qc_run, registered, data_root):
    manifest = json.loads((data_root / "project_terminal/test/datasets/datasets/synthetic_small/defects_manifest.json").read_text())
    injected = {d["z"]: d["type"] for d in manifest["defects"]}
    assert injected == {5: "blank", 9: "missing", 13: "corrupt", 20: "blur", 27: "brightness_jump"}
    with session_scope() as s:
        run = s.get(QCRun, qc_run)
        assert run.status == "done" and run.n_blocks == 2 and run.n_blocks_done == 2
        slices = {x.z: x for x in s.scalars(select(QCSlice).where(QCSlice.run_id == qc_run))}
        assert len(slices) == 40
        assert slices[9].status == "missing" and "missing" in slices[9].failure_types
        assert slices[13].status == "corrupt" and "corrupt" in slices[13].failure_types
        assert "blank" in slices[5].failure_types
        assert "blur" in slices[20].failure_types
        assert "brightness_jump" in slices[27].failure_types
        for z in (5, 9, 13, 20, 27):
            assert not slices[z].passed and slices[z].max_severity in ("high", "critical")
        # no HIGH+ false positives on clean slices
        bad = [z for z, x in slices.items() if not x.passed and z not in injected]
        assert bad == [], bad
        # 16 项全部实现之后，分数为 None 只允许出现在"检查明确记录了跳过原因"的情况下。
        # 这条不变式比"必须都有分"更严格也更有用：它禁止静默的空值。
        clean = slices[30]
        notes = clean.stats_json.get("_notes") or {}
        unexplained = [k for k, v in clean.scores_json.items() if v is None and k not in notes]
        assert unexplained == [], f"这些检查给了 None 却没说原因: {unexplained}"
        assert clean.scores_json["charging_artifact"] > 0.5 and clean.scores_json["slice_jump"] is not None
        # 形变检查在 64 px 的序列缩略图上能跑（4x4 网格），但区分度有限：
        # 它们相对本 block 的典型值判断，所以干净切片应当接近满分
        assert clean.scores_json["section_deformation"] > 0.7 and clean.scores_json["local_deformation"] > 0.7
        blocks = {b.block_id: b for b in s.scalars(select(QCBlock).where(QCBlock.run_id == qc_run))}
        assert blocks["z00000-00019"].n_missing == 1 and blocks["z00000-00019"].n_corrupt == 1
        assert blocks["z00000-00019"].grade == "B" and blocks["z00020-00039"].grade == "B"  # 85 % / 90 % retention, with critical slices
        assert blocks["z00000-00019"].longest_clean_run == 6 and blocks["z00020-00039"].longest_clean_run == 12
        assert blocks["z00000-00019"].dominant_failure in {"blank", "missing", "corrupt"}
        assert run.grade_counts_json == {"B": 2}
        assert abs(blocks["z00000-00019"].retention_rate - 17 / 20) < 1e-6
        assert blocks["z00020-00039"].retention_rate == 18 / 20
        assert abs(run.retention_rate - 35 / 40) < 1e-6
        metrics = {(m.block_id, m.metric_name): m for m in s.scalars(select(ETLMetric).where(ETLMetric.run_id == qc_run))}
        assert metrics[(None, "retention_rate")].metric_value == run.retention_rate
        assert metrics[("z00000-00019", "n_slices_input")].metric_value == 20
        assert metrics[(None, "findings_by_type")].extra_json.get("missing") == 1
        assert s.scalar(select(QCFinding).where(QCFinding.run_id == qc_run, QCFinding.failure_type == "blank")).z == 5
        splits = {b.block_id: b.split for b in s.scalars(select(Block).where(Block.dataset_id == registered))}
        assert set(splits.values()) <= {"train_sample", "inference"} and "train_sample" in splits.values()
        ds = s.get(Dataset, registered)
        latest = s.get(QCRun, ds.latest_run_id)  # other tests may have started later runs
        assert ds.status == "qc_done" and latest.status == "done" and ds.latest_retention_rate == latest.retention_rate


def test_fill_regions_and_crack_check():
    import numpy as np

    from emqc.qc.base import BlockContext, BlockInfo, DatasetInfo, QCConfig
    from emqc.qc.slice_checks import CrackCheck
    from emqc.qc.stages import compute_stats

    rng = np.random.default_rng(0)
    base = (rng.random((512, 512)) * 200 + 30).astype(np.uint8)
    yy, xx = np.mgrid[0:512, 0:512]
    band = base.copy()
    band[np.abs(yy - xx) < 14] = 0  # diagonal zero band, ~5 % of the area
    blob = base.copy()
    blob[100:200, 100:200] = 0  # compact missing tile, ~4 %
    clean_stats, _, _ = compute_stats(base, 255, 4_000_000, 256, 4)
    band_stats, _, _ = compute_stats(band, 255, 4_000_000, 256, 4)
    blob_stats, _, _ = compute_stats(blob, 255, 4_000_000, 256, 4)
    assert "frac_fill" not in clean_stats
    assert 0.04 < band_stats["frac_fill"] < 0.07 and band_stats["fill_elongation"] > 3 and band_stats["fill_span"] > 0.9
    assert abs(band_stats["mean"] - clean_stats["mean"]) < 0.01  # statistics ignore the fill region
    assert band_stats["frac_low_sat"] < 0.005
    assert 0.03 < blob_stats["frac_fill"] < 0.05 and blob_stats["fill_elongation"] < 1.5

    # a big compact region hugging the border = tissue does not fill the canvas (the real mouse_30um case):
    # a staircase-edged triangle covering ~35 % of the lower-left corner, elongation < 3
    corner = base.copy()
    corner[(yy - 80) > xx * 1.1] = 0
    corner_stats, _, _ = compute_stats(corner, 255, 4_000_000, 256, 4)
    assert corner_stats["frac_fill"] > 0.25 and corner_stats["fill_elongation"] < 3 and corner_stats["fill_border_sides"] >= 2

    ds = DatasetInfo("t", (4, 512, 512), "uint8")
    ctx = BlockContext(ds, BlockInfo("z00000-00003", 0, 4, 0, 512, 0, 512), QCConfig())
    for rec, st in zip(ctx.slices, (clean_stats, band_stats, blob_stats, corner_stats)):
        rec.stats = dict(st)
        if "fill_bbox_sub" in rec.stats:
            rec.stats["fill_bbox"] = rec.stats.pop("fill_bbox_sub")
    CrackCheck().run(ctx)
    assert ctx.slices[0].scores["crack"] == 1.0 and not ctx.slices[0].findings
    assert [f.failure_type for f in ctx.slices[1].findings] == ["crack"]  # thin band -> crack
    assert [f.failure_type for f in ctx.slices[2].findings] == ["missing_region"]  # interior blob
    assert [f.failure_type for f in ctx.slices[3].findings] == ["no_coverage"]  # border-hugging bulk -> not a crack
    assert ctx.slices[1].findings[0].coordinate["bbox"] is not None


def test_edge_tile_thresholds_use_nominal_tile():
    """A cropped rim tile (1024x200) must not get a 5x tighter misalignment limit than a full 1024x1024 tile."""
    from emqc.qc.base import BlockContext, BlockInfo, DatasetInfo, QCConfig

    ds = DatasetInfo("t", (10, 3000, 5000), "uint8")
    full = BlockContext(ds, BlockInfo("b", 0, 10, 0, 1024, 0, 1024), QCConfig(tile_xy=1024))
    rim = BlockContext(ds, BlockInfo("b", 0, 10, 0, 1024, 4800, 5000), QCConfig(tile_xy=1024))
    plane = BlockContext(ds, BlockInfo("b", 0, 10, 0, 3000, 0, 5000), QCConfig(tile_xy=1024))
    assert rim.extent == (1024, 200) and rim.is_tile
    assert full.nominal_short == rim.nominal_short == 1024.0
    assert plane.nominal_short == 3000.0  # whole-plane block: the plane's short side


def test_slice_coordinate_carries_union_bbox():
    from emqc.qc.runner import _union_bbox

    assert _union_bbox([None, [10, 20, 30, 40], [5, 25, 15, 60]]) == [5, 20, 30, 60]
    assert _union_bbox([None, None]) is None


def test_make_blocks_xy_tiling():
    from emqc.registry.scanner import make_blocks

    whole = make_blocks(40, 128, 128, 20, 0)
    assert [b["block_id"] for b in whole] == ["z00000-00019", "z00020-00039"] and whole[0]["x_end"] == 128
    tiled = make_blocks(40, 128, 200, 20, 64)
    assert len(tiled) == 2 * 2 * 4  # 2 z ranges x 2 y tiles x 4 x tiles (200 = 64+64+64+8)
    assert tiled[0]["block_id"] == "z00000-00019_y00000_x00000" and tiled[3]["x_start"] == 192 and tiled[3]["x_end"] == 200
    assert make_blocks(40, 128, 128, 20, 1024)[0]["block_id"] == "z00000-00019"  # tile bigger than plane -> whole plane


def test_tiled_group_processing(registered, data_root):
    """All XY tiles of one z range are ingested together; statistics and coordinates are per tile."""
    from emqc.qc.base import BlockInfo, DatasetInfo, QCConfig
    from emqc.qc.runner import QCRunner
    from emqc.registry.readers import open_volume
    from emqc.registry.scanner import make_blocks

    reader = open_volume(data_root / "project_terminal/test/datasets/datasets/synthetic_small/em")
    ds = DatasetInfo("synthetic_small", reader.shape, reader.info.dtype, voxel_size_nm=(8, 8, 40), size_class="small")
    tiles = [BlockInfo(**{k: v for k, v in b.items() if k != "n_slices"}) for b in make_blocks(40, 128, 128, 20, 64) if b["z_start"] == 0]
    assert len(tiles) == 4
    results = QCRunner(QCConfig(), preview_root=None).process_group(reader, ds, tiles)
    assert len(results) == 4
    for (ctx, res), blk in zip(results, tiles):
        assert ctx.is_tile and ctx.extent == (64, 64)
        ok = [r for r in ctx.slices if r.ok]
        assert ok and all(r.thumb.shape == (64, 64) for r in ok)  # preview thumbnail of the 64x64 crop
        assert res.n_slices == 20 and res.n_missing == 1 and res.n_corrupt == 1
        assert res.coordinate["bbox"] == [blk.x_start, blk.y_start, blk.x_end, blk.y_end]
        for r in ctx.slices:
            for f in r.findings:
                bb = f.coordinate.get("bbox")
                if bb:  # finding bboxes are reported in dataset pixels, inside this tile
                    assert blk.x_start <= bb[0] <= bb[2] <= blk.x_end and blk.y_start <= bb[1] <= bb[3] <= blk.y_end


def test_stratified_train_sampling():
    from types import SimpleNamespace as B

    from emqc.qc.sampling import parse_strata, select_train_blocks

    assert parse_strata("A:0.5,B:0.3,C:0.2") == {"A": 0.5, "B": 0.3, "C": 0.2}
    assert abs(sum(parse_strata("A:2,B:1,D:9").values()) - 1.0) < 1e-9 and "D" not in parse_strata("A:2,B:1,D:9")
    blocks = [B(block_id=f"a{i}", grade="A", retention_rate=1.0, quality_score=0.9) for i in range(10)]
    blocks += [B(block_id=f"b{i}", grade="B", retention_rate=0.9, quality_score=0.8) for i in range(2)]
    blocks += [B(block_id=f"c{i}", grade="C", retention_rate=0.7, quality_score=0.6) for i in range(5)]
    blocks += [B(block_id=f"d{i}", grade="D", retention_rate=0.2, quality_score=0.1) for i in range(3)]
    r = select_train_blocks(blocks, policy="stratified", n=10, strata="A:0.5,B:0.3,C:0.2", seed=1)
    assert r.quota == {"A": 5, "B": 3, "C": 2}
    assert len(r.chosen["B"]) == 2 and len(r.chosen["C"]) == 3  # B short by one -> carried to C
    assert len(r.chosen["A"]) == 5 and len(r.block_ids) == 10
    assert not any(b.startswith("d") for b in r.block_ids)
    assert r.block_ids == select_train_blocks(blocks, n=10, seed=1).block_ids  # reproducible
    assert select_train_blocks(blocks, n=10, seed=2).block_ids != r.block_ids  # but random within strata
    only_c = select_train_blocks([b for b in blocks if b.grade in "CD"], n=4, seed=1)
    assert len(only_c.block_ids) == 4 and set(only_c.chosen) == {"C"}
    best = select_train_blocks(blocks, policy="best", n=3)
    assert set(best.chosen) == {"A"}
    assert select_train_blocks([b for b in blocks if b.grade == "D"], n=4).block_ids == set()


def test_stage_text_fits_column_and_stale_runs_recover(registered):
    from sqlalchemy import select

    from emqc.db import recover_stale_runs, session_scope
    from emqc.db.models import QCRun, QCRunEvent

    assert QCRun.__table__.c.stage.type.length >= 120  # runner truncates stage text to 120 chars
    with session_scope() as s:
        run = QCRun(dataset_id=registered, pipeline_version="test", status="running", stage="slice_qc · z0-99 · z00000-00099_y01024_x01024")
        s.add(run)
        s.flush()
        rid = run.id
    assert recover_stale_runs() >= 1
    with session_scope() as s:
        run = s.get(QCRun, rid)
        assert run.status == "error" and "interrupted" in run.error
        assert s.scalar(select(QCRunEvent).where(QCRunEvent.run_id == rid)).level == "error"
