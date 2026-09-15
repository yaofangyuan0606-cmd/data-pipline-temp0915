import io

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(qc_run):
    from emqc.api.app import app

    with TestClient(app) as c:
        yield c


def test_health_and_datasets(client, registered):
    assert client.get("/api/v1/health").json()["status"] == "ok"
    ids = [d["dataset_id"] for d in client.get("/api/v1/datasets").json()]
    assert registered in ids
    d = client.get(f"/api/v1/datasets/{registered}").json()
    assert d["shape"] == {"z": 40, "y": 128, "x": 128} and len(d["blocks"]) == 2 and d["latest_run"]["status"] == "done"
    assert client.get("/api/v1/qc/checks").json()[0]["name"] == "missing_slice"


def test_qc_endpoints(client, registered, qc_run):
    run = client.get(f"/api/v1/qc/runs/{qc_run}").json()
    assert run["status"] == "done" and len(run["blocks"]) == 2 and "retention_rate" in run["metrics"]
    prof = client.get(f"/api/v1/qc/runs/{qc_run}/profile").json()
    assert len(prof) == 40 and prof[9]["status"] == "missing"
    f = client.get(f"/api/v1/qc/runs/{qc_run}/findings", params={"failure_type": "blur"}).json()
    assert [x["z"] for x in f] == [20]
    latest = client.get(f"/api/v1/qc/datasets/{registered}/latest").json()
    assert latest["run"]["run_id"] == qc_run
    assert client.get("/api/v1/qc/findings", params={"min_severity": "high"}).status_code == 200


def test_data_serving(client, registered):
    info = client.get(f"/api/v1/data/{registered}/info").json()
    assert info["shape"]["z"] == 40 and info["slice_passed"][9] is False and info["slice_passed"][30] is True
    r = client.get(f"/api/v1/data/{registered}/slice/3", params={"fmt": "npy", "y0": 10, "y1": 42, "x0": 0, "x1": 64})
    assert r.status_code == 200
    a = np.load(io.BytesIO(r.content))
    assert a.shape == (32, 64) and a.dtype == np.uint8
    assert client.get(f"/api/v1/data/{registered}/slice/9").status_code == 404  # missing slice
    assert client.get(f"/api/v1/data/{registered}/slice/3", params={"fmt": "png"}).headers["content-type"] == "image/png"
    r = client.get(f"/api/v1/data/{registered}/cutout", params={"z0": 30, "z1": 34, "y0": 0, "y1": 32, "x0": 8, "x1": 40})
    assert np.load(io.BytesIO(r.content)).shape == (4, 32, 32)
    assert client.get(f"/api/v1/data/{registered}/cutout", params={"z0": 30, "z1": 99, "y0": 0, "y1": 32, "x0": 8, "x1": 40}).status_code == 422
    r = client.post(f"/api/v1/data/{registered}/cutouts/batch", json={"bboxes": [[0, 2, 0, 8, 0, 8], [30, 31, 0, 4, 0, 4]]})
    z = np.load(io.BytesIO(r.content))
    assert z["patch_0"].shape == (2, 8, 8) and z["patch_1"].shape == (1, 4, 4)
    assert client.get(f"/api/v1/data/{registered}/preview/z/3.png").status_code == 200


def test_patch_sampling_respects_qc(client, registered):
    body = {"size": [4, 32, 32], "n": 40, "seed": 7, "only_passed": True}
    r = client.post(f"/api/v1/data/{registered}/patches/sample", json=body)
    assert r.status_code == 200, r.text
    res = r.json()
    bad = {5, 9, 13, 20, 27}
    for p in res["patches"]:
        zs = set(range(p["bbox"]["z0"], p["bbox"]["z1"]))
        assert not (zs & bad), p
        assert p["url"].startswith(f"/api/v1/data/{registered}/cutout?")
    assert res["n_allowed_slices"] == 35
    # a window that cannot avoid the bad slices -> 409
    assert client.post(f"/api/v1/data/{registered}/patches/sample", json={"size": [40, 8, 8], "n": 1}).status_code == 409
    # split filter: only train_sample blocks
    r = client.post(f"/api/v1/data/{registered}/patches/sample", json={"size": [2, 8, 8], "n": 5, "split": "train_sample"}).json()
    assert r["patches"] and all(p["block_id"] for p in r["patches"])
    manifest = client.get("/api/v1/data/training/manifest").json()
    assert any(e["dataset_id"] == registered for e in manifest["inference"])


def test_traces_and_assets(client, registered):
    r = client.post("/api/v1/traces", json={"dataset_id": registered, "agent": "seg-agent", "step": "predict", "action": "infer", "output": {"blocks": 2}, "model_version": "unet-x"})
    assert r.status_code == 201
    traces = client.get("/api/v1/traces", params={"dataset_id": registered}).json()
    assert traces[0]["agent"] == "seg-agent" and any(t["agent"] == "emqc.qc_runner" for t in traces)
    r = client.post(f"/api/v1/datasets/{registered}/assets", json={"asset_type": "model_prediction", "path": "pred_v2", "version": "v2", "model_version": "unet-x"})
    assert r.status_code == 201 and r.json()["exists"] is False
    r = client.post(f"/api/v1/datasets/{registered}/versions", json={"kind": "model", "version": "unet-x", "note": "test"})
    assert r.status_code == 201


def test_dashboard_pages(client, registered, qc_run):
    for url in ("/", f"/datasets/{registered}", f"/datasets/{registered}/blocks/z00000-00019", f"/runs/{qc_run}", "/runs", "/checks", "/traces"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert "<html" in r.text


def test_async_run_endpoint(client, registered):
    import time

    r = client.post("/api/v1/qc/runs", json={"dataset_id": registered, "block_ids": ["z00020-00039"]})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    for _ in range(60):
        run = client.get(f"/api/v1/qc/runs/{run_id}").json()
        if run["status"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert run["status"] == "done", run
    assert run["n_blocks"] == 1 and run["n_blocks_done"] == 1 and run["config"]["block_ids"] == ["z00020-00039"]


def test_pipeline_console_endpoints(client, registered):
    import time

    st = client.get("/api/v1/pipeline/status").json()
    assert st["pipeline"]["checks"]["total"] == 16 and st["datasets"]["total"] >= 1 and "active" in st
    # batch start with a config override: only two cheap checks, blocks restricted
    r = client.post("/api/v1/qc/runs/batch", json={"dataset_ids": [registered], "block_ids": ["z00000-00019"], "config": {"checks_enabled": ["missing_slice", "blank_slice"], "pass_max_severity": "critical"}})
    assert r.status_code == 202, r.text
    run_id = r.json()[0]["run_id"]
    assert client.get("/api/v1/qc/runs/active").status_code == 200
    for _ in range(80):
        run = client.get(f"/api/v1/qc/runs/{run_id}").json()
        if run["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.25)
    assert run["status"] == "done", run
    assert run["config"]["checks_enabled"] == ["missing_slice", "blank_slice"] and run["config"]["pass_max_severity"] == "critical"
    events = client.get(f"/api/v1/qc/runs/{run_id}/events").json()
    assert events and events[0]["message"].startswith("run started") and events[-1]["message"].startswith("run finished")
    assert any(e["stage"] == "ingest" for e in events) and any(e["stage"] == "done" and e["block_id"] == "z00000-00019" for e in events)
    # with only those two checks, the blur / brightness slices are not evaluated -> only missing (z9) and blank (z5) fail
    sl = client.get(f"/api/v1/qc/runs/{run_id}/slices").json()
    assert {x["z"] for x in sl if not x["passed"]} == {5, 9, 13}  # 13 is corrupt (status), always fails
    assert all(x["scores"].get("severe_blur") is None for x in sl)
    # cancelling a finished run is a 409; bad config is a 422
    assert client.post(f"/api/v1/qc/runs/{run_id}/cancel").status_code == 409
    assert client.post("/api/v1/qc/runs", json={"dataset_id": registered, "config": {"pass_max_severity": "bogus"}}).status_code == 422
    assert client.get("/pipeline").status_code == 200


def test_delete_dataset_removes_everything(client, data_root, monkeypatch):
    """Register a throw-away copy of the dataset, run QC, then delete it with its files.

    This test exercises the capability, so it turns file deletion on for its own scope: shared
    deployments run with EMQC_ALLOW_DELETE_FILES=0 and would otherwise fail here.
    """
    import shutil

    from sqlalchemy import select

    from emqc.config import settings
    from emqc.db import session_scope

    monkeypatch.setattr(settings, "allow_delete_files", True)
    from emqc.db.models import Block, Dataset, QCFinding, QCRun, QCSlice

    src = data_root / "project_terminal/test/datasets/datasets/synthetic_small"
    dst = data_root / "project_terminal/test/datasets/datasets/to_delete"
    shutil.copytree(src, dst)
    (dst / "dataset.json").write_text((dst / "dataset.json").read_text().replace('"dataset_id": "synthetic_small"', '"dataset_id": "to_delete"'))
    assert "to_delete" in client.post("/api/v1/datasets/scan").json()["registered"]
    run = client.post("/api/v1/qc/runs", json={"dataset_id": "to_delete", "sync": True}).json()
    assert run["status"] == "done"
    res = client.delete("/api/v1/datasets/to_delete", params={"remove_files": "true"}).json()
    assert res["deleted"] == "to_delete" and res["runs"] == 1 and res["qc_slices"] == 40 and res.get("files_removed")
    assert not dst.exists()
    with session_scope() as s:
        assert s.get(Dataset, "to_delete") is None
        assert not list(s.scalars(select(QCRun).where(QCRun.dataset_id == "to_delete")))
        assert not list(s.scalars(select(Block).where(Block.dataset_id == "to_delete")))
        assert not list(s.scalars(select(QCSlice).where(QCSlice.dataset_id == "to_delete")))
        assert not list(s.scalars(select(QCFinding).where(QCFinding.dataset_id == "to_delete")))
    assert client.get("/api/v1/datasets/to_delete").status_code == 404
    assert client.delete("/api/v1/datasets/to_delete").status_code == 404


def test_run_graph_events_and_system_metrics(client, registered, qc_run):
    g = client.get(f"/api/v1/qc/runs/{qc_run}/graph").json()
    assert g["stages"] == ["ingest", "slice_qc", "serial_qc", "aggregate", "persist"]
    assert [grp["z_start"] for grp in g["groups"]] == [0, 20]
    for grp in g["groups"]:
        assert grp["ingest"]["status"] == "done" and grp["ingest"]["n_tiles"] == 1
        for t in grp["tiles"]:
            assert t["done"] and t["grade"] in ("A", "B", "C", "D")
            assert all(t["nodes"][st]["status"] == "done" for st in ("slice_qc", "serial_qc", "aggregate", "persist"))
    ev = client.get(f"/api/v1/qc/runs/{qc_run}/events", params={"stage": "done"}).json()
    assert len(ev) == 2 and all(e["data"].get("grade") and "durations" in e["data"] and "peak_rss_mb" in e["data"] for e in ev)
    assert client.get(f"/api/v1/qc/runs/{qc_run}/events", params={"block_id": "z00000-00019", "stage": "slice_qc"}).json()[0]["data"]["z_start"] == 0
    assert client.get(f"/api/v1/qc/runs/{qc_run}/events", params={"level": "error"}).json() == []
    assert client.get(f"/api/v1/qc/runs/{qc_run}/events", params={"q": "finished"}).json()[-1]["message"].startswith("run finished")
    m = client.get("/api/v1/system/metrics").json()
    assert m["available"] is True and "cpu_pct" in (m["latest"] or {}) and isinstance(m["gpu_available"], bool)
    metrics = client.get(f"/api/v1/qc/runs/{qc_run}/metrics", params={"block_id": "z00000-00019"}).json()
    names = {x["name"] for x in metrics}
    assert {"cpu_time_s", "peak_rss_mb", "longest_clean_run"} <= names


def test_export_passed_sections(client, registered, qc_run, data_root, tmp_path):
    import json

    r = client.post(f"/api/v1/data/{registered}/export", json={"min_run": 3, "run_id": qc_run, "with_assets": ["gt_segmentation"], "out_dir": str(tmp_path)})
    assert r.status_code == 200, r.text
    m = r.json()
    out = tmp_path / registered
    assert (out / "export_manifest.json").is_file() and m["run_id"] == qc_run
    # failing sections 5 / 9 / 13 / 20 / 27 never appear inside a chunk; every chunk is >= 3 long
    for c in m["shards"]:
        zs = set(range(c["z_start"], c["z_end"]))
        assert not (zs & {5, 9, 13, 20, 27}) and c["n"] >= 3
        arr = np.load(out / c["file"])
        assert arr.shape == (c["n"], 128, 128) and arr.dtype == np.uint8
    assert m["n_sections"] == sum(c["n"] for c in m["shards"]) and m["n_sections"] <= 35
    assert {e["z"] for e in m["excluded"]["z00000-00019"]} >= {5, 9, 13}
    gt = m["assets"]["gt_segmentation:gt"]
    assert gt["copied"] == m["n_sections"] and (out / gt["dir"]).is_dir()


def test_export_jobs_shards_and_resume(client, registered, qc_run, tmp_path):
    body = {"dataset_id": registered, "run_id": qc_run, "min_run": 2, "shard_z": 4, "out_dir": str(tmp_path), "sync": True}
    j = client.post("/api/v1/exports", json=body).json()
    assert j["status"] == "done" and j["n_blocks_done"] == 2 and j["n_shards"] > 0 and j["n_shards_reused"] == 0
    m = client.get(f"/api/v1/exports/{j['job_id']}/manifest").json()
    assert all(s["n"] <= 4 for s in m["shards"]) and m["status"] == "done" and (tmp_path / registered / "z00000-00019" / "index.json").is_file()
    ev = client.get(f"/api/v1/exports/{j['job_id']}/events").json()
    assert ev[0]["message"].startswith("export started") and ev[-1]["message"].startswith("export done")
    j2 = client.post("/api/v1/exports", json=body).json()  # same target again -> everything reused
    assert j2["n_shards_reused"] == j["n_shards"] and j2["n_sections"] == j["n_sections"]
    assert client.get("/api/v1/exports", params={"dataset_id": registered}).json()[0]["job_id"] == j2["job_id"]
    assert client.post(f"/api/v1/exports/{j['job_id']}/cancel").status_code == 409
    from emqc.loader import ShardStore

    store = ShardStore(tmp_path / registered)
    patch, meta = store.random_patch(np.random.default_rng(0), (2, 32, 32))
    assert patch.shape == (2, 32, 32) and meta["block_id"].startswith("z0")
    assert sum(1 for _ in store.iter_patches((2, 64, 64))) > 0


def test_stream_sessions(client, registered, qc_run):
    r = client.post("/api/v1/streams", json={"dataset_id": registered, "z_chunk": 8, "client": "test-infer"})
    assert r.status_code == 201, r.text
    sess = r.json()
    assert sess["n_items"] == 6 and sess["cursor"] == 0 and sess["first_items"][0]["url"].endswith(f"stream_id={sess['stream_id']}")
    nxt = client.post(f"/api/v1/streams/{sess['stream_id']}/next", params={"n": 2}).json()
    assert [it["i"] for it in nxt["items"]] == [0, 1] and nxt["cursor"] == 2 and not nxt["done"]
    a = np.load(io.BytesIO(client.get(nxt["items"][0]["url"]).content))
    assert a.shape == (8, 128, 128)
    assert client.get(f"/api/v1/streams/{sess['stream_id']}").json()["n_bytes"] > 0  # the cutout was attributed to the stream
    client.post(f"/api/v1/streams/{sess['stream_id']}/ack", json={"indices": [0, 1]})
    for _ in range(5):
        nxt = client.post(f"/api/v1/streams/{sess['stream_id']}/next", params={"n": 2}).json()
        if nxt["done"]:
            break
    assert nxt["done"] and nxt["cursor"] == 6
    done = client.post(f"/api/v1/streams/{sess['stream_id']}/close", json={"status": "done"}).json()
    assert done["status"] == "done" and done["n_acked"] == 2
    assert client.post(f"/api/v1/streams/{sess['stream_id']}/next").status_code == 409
    # a plan that skips chunks with failed sections is shorter, and the item that would hold z=9 is gone
    skip = client.post("/api/v1/streams", json={"dataset_id": registered, "z_chunk": 8, "skip_failed": True}).json()
    assert skip["n_items"] < 6
    plan = client.get(f"/api/v1/streams/{skip['stream_id']}/plan").json()["items"]
    assert all(not (it["z0"] <= 9 < it["z1"]) for it in plan)
    assert client.get("/api/v1/streams", params={"dataset_id": registered}).json()[0]["stream_id"] == skip["stream_id"]


def test_asset_registration_uses_the_dataset_filesystem(client, registered, monkeypatch):
    """Regression: registration used local pathlib, so every asset of a remote (sftp://) dataset was
    recorded as missing. It must go through the dataset's own FS."""
    from test_fs import FakeFS

    from emqc.registry import scanner

    fake = FakeFS({"root/pred/unet_v3/im0000.png": b"x" * 1234, "root/pred/unet_v3/im0001.png": b"y" * 766})
    monkeypatch.setattr(scanner, "open_root", lambda root: (fake, "root"))
    r = client.post(f"/api/v1/datasets/{registered}/assets", json={"asset_type": "model_prediction", "path": "pred/unet_v3", "format": "image_stack", "version": "v3", "model_version": "unet-x"})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["exists"] is True and a["size_bytes"] == 2000  # shallow sum of the directory's files
    r2 = client.post(f"/api/v1/datasets/{registered}/assets", json={"asset_type": "skeleton", "path": "pred/nope", "version": "v1"})
    assert r2.json()["exists"] is False and r2.json()["size_bytes"] is None

    def boom(root):
        raise OSError("host unreachable")

    monkeypatch.setattr(scanner, "open_root", boom)
    r3 = client.post(f"/api/v1/datasets/{registered}/assets", json={"asset_type": "other", "path": "pred/x", "version": "v1"})
    assert r3.status_code == 201 and r3.json()["exists"] is False
    assert "host unreachable" in r3.json()["extra"]["source_check_error"]  # recorded, not silently "missing"


def test_partial_run_does_not_demote_blocks_it_never_looked_at(client, registered, monkeypatch):
    """Regression: finalize() rewrote split on every block of the dataset using only the current run's
    blocks, so re-running QC on a subset silently demoted the previously sampled blocks to inference."""
    from sqlalchemy import select

    from emqc.config import settings
    from emqc.db import session_scope
    from emqc.db.models import Block

    monkeypatch.setattr(settings, "train_sample_blocks", 1)  # 2 blocks -> 1 train_sample, 1 inference

    def splits():
        with session_scope() as s:
            return {b.block_id: b.split for b in s.scalars(select(Block).where(Block.dataset_id == registered))}

    assert client.post("/api/v1/qc/runs", json={"dataset_id": registered, "sync": True}).json()["status"] == "done"
    full = splits()
    assert sorted(full.values()) == ["inference", "train_sample"], full
    sampled = [b for b, sp in full.items() if sp == "train_sample"][0]
    other = [b for b, sp in full.items() if sp == "inference"][0]

    for only in (other, sampled):  # re-run each block on its own
        assert client.post("/api/v1/qc/runs", json={"dataset_id": registered, "block_ids": [only], "sync": True}).json()["status"] == "done"
        assert splits() == full, f"re-running only {only} changed the split: {splits()} != {full}"
