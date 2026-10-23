"""找相似：特征空间的近邻是对的、接口能用、评测脚本在小样本上跑得通。"""
import numpy as np
from fastapi.testclient import TestClient
from sqlalchemy import select

from emqc.db import session_scope
from emqc.db.models import QCSlice
from emqc.qc.similar import build_space


def _rec(mean, std, sharp, blur_score=1.0, fill=None):
    st = {"mean": mean, "std": std, "sharpness": sharp, "lapvar": sharp * std * std, "entropy_bits": 6.0}
    if fill:
        st.update(frac_fill=fill, n_fill_components=1, fill_elongation=8.0, fill_span=0.9, fill_largest_frac=fill)
    return st, {"severe_blur": blur_score, "crack": 0.1 if fill else 1.0}


def test_nearest_groups_like_with_like():
    rng = np.random.default_rng(0)
    recs, kinds = [], []
    for _ in range(20):
        recs.append(_rec(0.5 + rng.normal(0, .01), 0.1 + rng.normal(0, .005), 5 + rng.normal(0, .2))); kinds.append("clean")
    for _ in range(6):
        recs.append(_rec(0.5 + rng.normal(0, .01), 0.06, 0.4 + rng.normal(0, .05), blur_score=0.2)); kinds.append("blur")
    for _ in range(6):
        recs.append(_rec(0.45 + rng.normal(0, .01), 0.12, 5, fill=0.05 + rng.normal(0, .005))); kinds.append("crack")
    for space in (build_space(recs, keys=range(len(recs)), score_names=[]), build_space(recs, keys=range(len(recs)))):
        for i, k in enumerate(kinds):
            if k == "clean":
                continue
            nn = space.nearest(i, k=5)
            assert all(kinds[j] == k for j, _ in nn), (k, [kinds[j] for j, _ in nn])
            assert i not in [j for j, _ in nn]


def test_absent_fill_stats_mean_no_fill_and_constant_columns_drop():
    recs = [({"mean": 0.5, "std": 0.1}, {}), ({"mean": 0.5, "std": 0.1, "frac_fill": 0.3}, {}), ({"mean": 0.5, "std": 0.1}, {})]
    sp = build_space(recs, keys=range(3), score_names=[])
    assert "frac_fill" in sp.stat_names and "mean" not in sp.stat_names  # mean is constant -> dropped
    assert sp.nearest(0, k=1)[0][0] == 2                                  # the two fill-free slices pair up


def test_similar_endpoint(registered, qc_run):
    from emqc.api.app import app

    c = TestClient(app)
    with session_scope() as s:
        q = s.scalars(select(QCSlice).where(QCSlice.run_id == qc_run, QCSlice.status == "ok", QCSlice.z == 20)).first()
        bid = q.block_id
    r = c.get("/api/v1/qc/similar", params={"run_id": qc_run, "block_id": bid, "z": 20, "k": 5})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["query"]["z"] == 20 and len(d["hits"]) == 5 and d["n_pool"] > 5
    assert all(h["z"] != 20 for h in d["hits"])                      # other tiles of the same section are left out
    assert [h["distance"] for h in d["hits"]] == sorted(h["distance"] for h in d["hits"])
    assert all(h["url"] and h["href"] for h in d["hits"])
    assert c.get("/api/v1/qc/similar", params={"run_id": qc_run, "block_id": bid, "z": 9999}).status_code == 404


def test_calib_eval_script_runs(tmp_path, data_root):
    import subprocess
    import sys
    from pathlib import Path

    from PIL import Image

    em = sorted((data_root / "project_terminal/test/datasets/datasets/synthetic_small/em").glob("*.png"))
    good = []
    for p in em:
        try:
            good.append(np.asarray(Image.open(p).convert("L")))
        except Exception:
            pass
    crops = tmp_path / "calib" / "crops"
    for chk, fn in (("severe_blur", lambda a: (a * 0 + a.mean()).astype(np.uint8)), ("crack", lambda a: np.where(np.arange(a.shape[1])[None, :] % 40 < 6, 0, a).astype(np.uint8))):
        for cat, n, f in (("fail", 4, fn), ("clean", 5, lambda a: a)):
            d = crops / chk / cat
            d.mkdir(parents=True)
            for i in range(n):
                Image.fromarray(f(good[(i * 3 + len(chk)) % len(good)])).save(d / f"s{chk[:3]}{cat}{i}_x_z{i}.png")
    out = tmp_path / "out"
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(root / "scripts/calib_similarity_eval.py"), str(crops), "--k", "3", "--out", str(out)], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr[-2000:]
    rep = (out / "report.md").read_text()
    assert "只用统计量" in rep and "裂缝" in rep and (out / "results.json").is_file()


def test_run_slice_map(registered, qc_run):
    from emqc.api.app import app

    c = TestClient(app)
    d = c.get(f"/api/v1/qc/runs/{qc_run}/map").json()
    assert d["n"] > 10 and len(d["points"]) == d["n"] and len(d["axes"]) == 2
    assert all(a["drivers"] and 0 <= a["explained"] <= 1 for a in d["axes"])
    assert d["axes"][0]["explained"] >= d["axes"][1]["explained"]
    p = d["points"][0]
    assert {"x", "y", "z", "passed", "url", "href"} <= set(p)
    assert any(not q["passed"] for q in d["points"])
    small = c.get(f"/api/v1/qc/runs/{qc_run}/map", params={"max_points": 10}).json()
    assert sum(not q["passed"] for q in small["points"]) == sum(not q["passed"] for q in d["points"])   # 抽稀时坏切片一张不丢
    assert f"sliceMapInit({qc_run})" in c.get(f"/runs/{qc_run}").text
