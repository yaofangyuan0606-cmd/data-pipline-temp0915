"""Requirement 3: label reading & validation, holdout partition, leakage checks, patch generators, Training Data API."""
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(qc_run):
    from emqc.api.app import app

    with TestClient(app) as c:
        yield c


def test_decode_label_rgb24_and_gray():
    from PIL import Image

    from emqc.registry.labels import boundary_map, decode_label, pack_rgb24

    ids = np.array([[0, 1, 1], [70000, 70000, 2], [2, 2, 2]], dtype=np.uint32)
    rgb = np.stack([(ids >> 16) & 255, (ids >> 8) & 255, ids & 255], axis=-1).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "PNG")
    out = decode_label(buf.getvalue(), "seg0000.png")
    assert out.dtype == np.uint32 and np.array_equal(out, ids) and np.array_equal(pack_rgb24(rgb), ids)
    g = np.array([[0, 5], [5, 300]], dtype=np.uint16)
    buf = io.BytesIO()
    Image.fromarray(g).save(buf, "PNG")
    assert np.array_equal(decode_label(buf.getvalue(), "m.png"), g)
    b = boundary_map(ids)
    assert b[0, 0] == 1 and b[2, 2] == 0 and b.dtype == np.uint8


def test_partition_targets_and_stability():
    from types import SimpleNamespace

    from emqc.patches.partition import assign_partitions, difficulty_of, targets

    assert targets(4, (0.8, 0.1, 0.1)) == {"train": 2, "val": 1, "test": 1}
    assert targets(2, (0.8, 0.1, 0.1)) == {"train": 2, "val": 0, "test": 0}
    assert targets(20, (0.8, 0.1, 0.1)) == {"train": 16, "val": 2, "test": 2}

    def mk(i, grade="B", split="train"):
        return SimpleNamespace(block_id=f"b{i:02d}", split=split, latest_grade=grade, partition="none", partition_seed=None, z_start=0, z_end=10, y_start=i * 100, y_end=i * 100 + 100, x_start=0, x_end=100)

    blocks = [mk(i) for i in range(10)] + [mk(10, "D"), mk(11, "A", "inference"), mk(12, None, "unassigned")]
    r1 = assign_partitions(blocks, (0.8, 0.1, 0.1), seed=7)
    assert r1.counts["train"] == 8 and r1.counts["val"] == 1 and r1.counts["test"] == 1 and r1.counts["excluded"] == 1 and r1.counts["none"] == 2
    first = {b.block_id: b.partition for b in blocks}
    assert first["b10"] == "excluded" and first["b11"] == "none" and first["b12"] == "none"
    # stable: same call again changes nothing; a new eligible block fills the largest deficit without touching others
    r2 = assign_partitions(blocks, (0.8, 0.1, 0.1), seed=7)
    assert not r2.changed and {b.block_id: b.partition for b in blocks} == first
    blocks.append(mk(13))
    assign_partitions(blocks, (0.8, 0.1, 0.1), seed=7)
    assert all(b.partition == first[b.block_id] for b in blocks if b.block_id in first)
    # force reshuffles deterministically for a seed
    assign_partitions(blocks, (0.8, 0.1, 0.1), seed=99, force=True)
    a = {b.block_id: b.partition for b in blocks}
    assign_partitions(blocks, (0.8, 0.1, 0.1), seed=99, force=True)
    assert {b.block_id: b.partition for b in blocks} == a
    d, comp = difficulty_of(0.7, 0.85, 12, 100, "B")
    assert abs(d - (0.5 * 0.3 + 0.3 * 0.15 + 0.2 * 0.12)) < 1e-9 and comp["grade"] == "B"
    assert difficulty_of(0.95, 0.99, 0, 100, "D")[0] >= 0.9


def test_patch_checks_detect_duplicates_overlap_and_leaks():
    from types import SimpleNamespace

    from emqc.patches.checks import check_patches

    blk = {"a": SimpleNamespace(block_id="a", partition="train", z_start=0, z_end=20, y_start=0, y_end=128, x_start=0, x_end=128),
           "b": SimpleNamespace(block_id="b", partition="test", z_start=20, z_end=40, y_start=0, y_end=128, x_start=0, x_end=128)}
    p = lambda bid, part, z0, y0, x0: {"block_id": bid, "partition": part, "z0": z0, "z1": z0 + 4, "y0": y0, "y1": y0 + 32, "x0": x0, "x1": x0 + 32}
    good = [p("a", "train", 0, 0, 0), p("a", "train", 8, 64, 64), p("b", "test", 20, 0, 0)]
    r = check_patches(good, blk)
    assert r["passed"] and r["n_duplicate_exact"] == 0 and r["n_overlap_cross_partition"] == 0 and r["adjacent_cross_partition_block_pairs"] == 1
    bad = good + [p("a", "train", 0, 0, 0), p("a", "test", 1, 2, 2), p("a", "train", 18, 0, 0)]  # dup, cross-partition overlap+mismatch, straddles block
    r = check_patches(bad, blk)
    assert not r["passed"] and r["n_duplicate_exact"] == 1 and r["n_overlap_cross_partition"] >= 1 and r["n_partition_mismatch"] == 1 and r["n_outside_block"] == 1


def test_label_validation_on_synthetic(client, registered, qc_run):
    labels = client.get(f"/api/v1/data/{registered}/labels").json()
    gt = next(a for a in labels if a["asset_type"] == "gt_segmentation")
    assert gt["validation"] is None and not gt["usable_for_patches"]
    v = client.post(f"/api/v1/datasets/{registered}/assets/{gt['id']}/validate").json()
    assert v["scale_to_em"] == [1, 1] and v["n_sections_checked"] == 40 and v["usable_for_patches"] is True
    # z=9 missing / z=13 corrupt / z=5 blank EM sections still carry synthetic labels -> label_without_image
    assert v["issues"].get("label_without_image", 0) >= 3 and v["status"] == "warn"
    det = client.get(f"/api/v1/datasets/{registered}/assets/{gt['id']}/validation", params={"only_flagged": True}).json()
    assert {r["z"] for r in det["sections"]} >= {5, 9, 13}
    pred = next(a for a in labels if a["asset_type"] == "model_prediction")
    assert client.post(f"/api/v1/datasets/{registered}/assets/{pred['id']}/validate").json()["usable_for_patches"] is True
    syn = next(a for a in labels if a["asset_type"] == "synapse_prediction")
    assert client.post(f"/api/v1/datasets/{registered}/assets/{syn['id']}/validate").status_code == 422  # json, not a volume
    # label cutout aligned with the EM cutout; boundary / mask derivations
    ids = np.load(io.BytesIO(client.get(f"/api/v1/data/{registered}/labels/{gt['id']}/cutout", params={"z0": 20, "z1": 22, "y0": 0, "y1": 32, "x0": 0, "x1": 32}).content))
    bd = np.load(io.BytesIO(client.get(f"/api/v1/data/{registered}/labels/{gt['id']}/cutout", params={"z0": 20, "z1": 22, "y0": 0, "y1": 32, "x0": 0, "x1": 32, "derive": "boundary"}).content))
    assert ids.shape == (2, 32, 32) and ids.dtype.kind == "u" and bd.shape == ids.shape and bd.dtype == np.uint8 and bd.max() == 1


def test_partition_and_block_lineage_after_qc(client, registered, qc_run, monkeypatch):
    part = client.get(f"/api/v1/datasets/{registered}/partition").json()
    blocks = part["blocks"]
    assert all(b["source_volume"] and b["preprocessing"] and b["augmentation"] and b["difficulty"] is not None for b in blocks)
    assert all(b["partition"] in ("train", "none", "excluded") for b in blocks) and part["config"]["n_eligible"] <= 2  # 2 blocks: no holdout yet
    # eligible = training-usage blocks with grade A/B/C (an earlier test may have re-split one block to inference)
    expected = sum(1 for b in blocks if b["split"] in ("train", "train_sample") and (b["latest_grade"] or "") in "ABC")
    r = client.post(f"/api/v1/datasets/{registered}/partition", json={"ratios": "0.5,0.25,0.25", "force": True}).json()
    assert r["result"]["n_eligible"] == expected and r["result"]["note"].startswith("fewer than 3")


def test_patch_sets_end_to_end(client, registered, qc_run):
    ready = client.get(f"/api/v1/data/{registered}/patch-readiness").json()["types"]
    assert ready["failure"]["ready"] and ready["segmentation"]["ready"] and ready["hard_negative"]["ready"]
    assert not ready["synapse"]["ready"] and "synapse" in ready["synapse"]["reason"]
    # failure patches around QC findings
    f = client.post("/api/v1/patchsets", json={"dataset_id": registered, "patch_type": "failure", "size": [4, 32, 32], "n": 20}).json()
    assert f["status"] == "done" and f["n_patches"] > 0 and f["checks"]["passed"] and set(f["counts"]["by_failure_type"]) >= {"blank", "blur"}
    # segmentation / membrane over passed sections with GT ids; partitions default to train/val/test -> our 2-block fixture is all train
    seg = client.post("/api/v1/patchsets", json={"dataset_id": registered, "patch_type": "segmentation", "size": [2, 32, 32], "n": 16, "params": {"min_fg_frac": 0.2}}).json()
    assert seg["status"] == "done" and seg["n_patches"] == 16 and seg["label_version"] == "v1" and seg["checks"]["passed"]
    mem = client.post("/api/v1/patchsets", json={"dataset_id": registered, "patch_type": "membrane", "size": [2, 32, 32], "n": 8}).json()
    assert mem["status"] == "done" and mem["n_patches"] == 8
    hn = client.post("/api/v1/patchsets", json={"dataset_id": registered, "patch_type": "hard_negative", "size": [2, 32, 32], "n": 8, "params": {"min_disagreement": 0.01}}).json()
    assert hn["status"] == "done" and hn["n_patches"] == 8 and hn["params"]["prediction_version"] == "v1"
    pr = client.post("/api/v1/patchsets", json={"dataset_id": registered, "patch_type": "proofreading", "size": [2, 32, 32], "n": 5}).json()
    assert pr["status"] == "done" and pr["n_patches"] == 5
    syn = client.post("/api/v1/patchsets", json={"dataset_id": registered, "patch_type": "synapse", "size": [2, 32, 32], "n": 5}).json()
    assert syn["status"] == "no_aligned_label" and syn["n_patches"] == 0 and syn["reason"]
    # patches: EM and label cutouts of the same bbox; failing sections never inside a segmentation patch
    items = client.get(f"/api/v1/patchsets/{seg['set_id']}/patches").json()["items"]
    p0 = items[0]
    em = np.load(io.BytesIO(client.get(p0["url_em"]).content))
    lab = np.load(io.BytesIO(client.get(p0["url_label"]).content))
    assert em.shape == lab.shape == (2, 32, 32) and p0["meta"]["n_ids"] >= 2 and p0["partition"] == "train"
    assert all(not ({5, 9, 13, 20, 27} & set(range(it["bbox"]["z0"], it["bbox"]["z1"]))) for it in items)
    ranks = [x["meta"]["rank"] for x in client.get(f"/api/v1/patchsets/{pr['set_id']}/patches").json()["items"]]
    assert ranks == [1, 2, 3, 4, 5]
    man = client.get(f"/api/v1/patchsets/{seg['set_id']}/manifest").json()
    assert len(man["patches"]) == 16 and man["preprocessing"]["normalize"] and man["dataset"]["dataset_id"] == registered
    assert client.get("/api/v1/patchsets", params={"dataset_id": registered, "patch_type": "membrane"}).json()[0]["set_id"] == mem["set_id"]
    assert client.delete(f"/api/v1/patchsets/{syn['set_id']}").json()["deleted"] == syn["set_id"]
