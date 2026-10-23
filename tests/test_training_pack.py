"""训练数据包（文件名排在 test_patches 之后：它要先验证 GT，test_patches 需要看到「还没验证」的初始状态）：矩阵规划、按格产出、index.csv 与文件对得上、标签与 EM 同形状、页面和缩略图接口。"""
import csv
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient


def _client():
    from emqc.api.app import app

    return TestClient(app)


def _validate_gt(c, ds):
    labs = c.get(f"/api/v1/data/{ds}/labels").json()
    gt = [a for a in labs if a["asset_type"] == "gt_segmentation"]
    if gt and not (gt[0].get("validation") or {}).get("usable_for_patches"):
        c.post(f"/api/v1/datasets/{ds}/assets/{gt[0]['asset_id'] if 'asset_id' in gt[0] else gt[0]['id']}/validate")
    return bool(gt)


def test_plan_matrix(registered, qc_run):
    c = _client()
    p = c.post("/api/v1/packs/plan", json={"dataset_id": registered, "max_slices": 20, "patch_size": [8, 64, 64], "n_patches": 6}).json()
    cells = {(x["task"], x["level"]): x for x in p["cells"]}
    assert len(p["cells"]) == 8 * 3
    assert cells[("raw", "slice")]["ready"] and 0 < cells[("raw", "slice")]["n"] <= 20
    assert cells[("qc", "segment")]["applicable"] is False                     # 质检分类没有 3D 连续段
    assert cells[("hard_negative", "slice")]["applicable"] is False
    assert all(x["reason"] for x in p["cells"] if x["applicable"] and not x["ready"])   # 不能产出的格子都写了原因
    q = cells[("qc", "slice")]
    assert q["ready"] and q["by_class"]["fail"] > 0 and q["by_class"]["clean"] == q["by_class"]["fail"]


def test_build_pack(registered, qc_run):
    from emqc.patches.pack import build_pack

    c = _client()
    has_gt = _validate_gt(c, registered)
    cells = [["raw", "slice"], ["raw", "segment"], ["raw", "patch"], ["qc", "slice"], ["qc", "patch"]] + ([["segmentation", "patch"], ["membrane", "slice"]] if has_gt else [])
    m = build_pack(registered, cells=cells, max_slices=16, min_run=4, shard_z=8, patch_size=[4, 64, 64], n_patches=5)
    assert m["status"] == "done"
    root = Path(c.get(f"/api/v1/exports/{m['job_id']}").json()["out_dir"])
    assert (root / "README.md").is_file() and (root / "pack_manifest.json").is_file()
    got = {(x["task"], x["level"]): x for x in m["cells"]}
    for t, lv in cells:
        cell = got[(t, lv)]
        assert cell["status"] == "done" and cell["n"] > 0, cell
        rows = list(csv.DictReader((root / t / lv / "index.csv").open(encoding="utf-8")))
        assert len(rows) == cell["n"]
        for r in rows[:5]:
            assert (root / r["file"]).is_file()
            if r["label_file"]:
                x, y = np.load(root / r["file"]) if r["file"].endswith(".npy") else None, np.load(root / r["label_file"])
                if x is not None:
                    assert x.shape == y.shape
            if lv == "segment":
                assert int(r["z1"]) - int(r["z0"]) >= 4
            if lv == "patch":
                assert int(r["z1"]) - int(r["z0"]) == 4 and int(r["y1"]) - int(r["y0"]) == 64
    qc_rows = list(csv.DictReader((root / "qc/slice/index.csv").open(encoding="utf-8")))
    assert {r["label"] == "clean" for r in qc_rows} == {True, False}
    assert all(("/clean/" in r["file"]) == (r["label"] == "clean") for r in qc_rows)    # ImageFolder 结构
    # the pack is listed, samples and thumbnails come back
    lst = c.get(f"/api/v1/packs?dataset_id={registered}").json()
    assert any(j["job_id"] == m["job_id"] and j["cells"] for j in lst)
    assert all(j["params"].get("kind") != "pack" for j in c.get("/api/v1/exports").json())      # 不混进普通导出列表
    sm = c.get(f"/api/v1/packs/{m['job_id']}/samples", params={"task": "raw", "level": "slice", "n": 4}).json()
    assert len(sm["samples"]) == 4
    r = c.get(sm["samples"][0]["thumb"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert c.get(f"/api/v1/packs/{m['job_id']}/samples", params={"task": "..", "level": "slice"}).status_code in (404, 422)


def test_pack_api_and_page(registered, qc_run):
    c = _client()
    assert c.post("/api/v1/packs", json={"dataset_id": registered, "cells": []}).status_code == 422
    assert c.post("/api/v1/packs", json={"dataset_id": registered, "cells": [["raw", "slice"]], "patch_size": [1, 2]}).status_code == 422
    page = c.get(f"/training?dataset={registered}").text
    assert "训练数据包" in page and "training.js" in page and 'href="/training"' in page


def test_lan_share_guard(monkeypatch):
    import pytest

    from emqc import share
    from emqc.config import settings

    assert share.check_before_listen("127.0.0.1") == []
    monkeypatch.setattr(settings, "auth_disabled", True)
    with pytest.raises(SystemExit):
        share.check_before_listen("0.0.0.0")
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "auth_open", True)
    assert share.check_before_listen("0.0.0.0")            # 试用模式：放行但有警告
    monkeypatch.setattr(settings, "auth_open", False)
    assert share.check_before_listen("0.0.0.0") == []
    monkeypatch.setattr(settings, "api_host", "0.0.0.0")
    i = share.info(8793)
    assert i["lan"] and i["auth"] == "password" and all(u.endswith(":8793") for u in i["urls"])
