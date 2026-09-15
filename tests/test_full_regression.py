"""全缺陷回归：128 张合成数据，注入 16 种缺陷，逐项确认能检出且不误报。

原来的回归只注入 4 种（空白 / 缺失 / 损坏 / 模糊 / 亮度突变里的前几种），
其余八种只做过一次性人工验证，没有测试保护。这个文件补上那块空白。
比默认套件慢（约 1 分钟），所以放在单独文件里，需要时 -k 选中跑。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def full_run():
    tmp = Path(tempfile.mkdtemp(prefix="emqc-full-"))
    env = {
        "EMQC_DB_URL": f"sqlite:///{tmp / 'full.db'}", "EMQC_DATA_ROOT": str(tmp / "data_root"),
        "EMQC_PREVIEW_DIR": str(tmp / "previews"), "EMQC_CACHE_DIR": str(tmp / "cache"),
        "EMQC_MANIFEST_DIR": str(tmp / "manifests"), "EMQC_REMOTE_ROOTS": "",
        "EMQC_BLOCK_SIZE_Z": "64", "EMQC_BLOCK_SIZE_XY": "0",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.make_sample_dataset import make_synthetic

    base = tmp / "data_root" / "project_terminal" / "demo" / "datasets" / "datasets"
    base.mkdir(parents=True)
    manifest = make_synthetic(base / "synthetic_full", n_z=128, size=256, seed=1)

    import emqc.config as cfgmod
    from emqc.config import Settings
    from emqc.db import base as dbbase

    # 这个 fixture 会改全局配置与数据库绑定，退出时必须原样还原，
    # 否则同一进程里后面的测试会连到这份临时库上（踩过一次）
    saved_settings = dict(cfgmod.settings.__dict__)
    saved_db_url = saved_settings.get("db_url")
    cfgmod.settings.__dict__.update(Settings().__dict__)
    dbbase.rebind(env["EMQC_DB_URL"])
    from emqc.db import init_db, session_scope
    from emqc.qc.runner import run_sync
    from emqc.registry.scanner import scan

    init_db()
    with session_scope() as s:
        res = scan(s)
    assert not res.errors, res.errors
    run_id = run_sync("synthetic_full")
    try:
        yield manifest, run_id
    finally:
        cfgmod.settings.__dict__.clear()
        cfgmod.settings.__dict__.update(saved_settings)
        if saved_db_url:
            dbbase.rebind(saved_db_url)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _slices(run_id):
    from sqlalchemy import select

    from emqc.db import session_scope
    from emqc.db.models import QCFinding, QCSlice

    with session_scope() as s:
        rows = {r.z: r for r in s.scalars(select(QCSlice).where(QCSlice.run_id == run_id))}
        finds = [(f.z, f.check_name, f.failure_type, f.severity, f.level) for f in s.scalars(select(QCFinding).where(QCFinding.run_id == run_id))]
    return rows, finds


# 每种注入缺陷 -> 允许判定它的 failure_type（有些缺陷天然会同时触发相关项）
ACCEPTS = {
    "blank": {"blank"}, "missing": {"missing"}, "corrupt": {"corrupt"}, "blur": {"blur"},
    "brightness_jump": {"brightness_jump"}, "contrast_drift": {"contrast_drift"},
    "saturation": {"saturation", "brightness_jump"}, "crack": {"crack", "missing_region", "no_coverage"},
    "charging": {"charging", "saturation", "brightness_jump"}, "contamination": {"contamination", "saturation"},
    "slice_jump": {"slice_jump"}, "z_order": {"z_order", "slice_jump"},
    "global_misalign": {"global_misalign", "slice_jump"}, "local_misalign": {"local_misalign", "local_deformation", "section_deformation"},
    "section_deformation": {"section_deformation", "local_deformation", "global_misalign"},
    "local_deformation": {"local_deformation", "section_deformation", "local_misalign"},
}


def test_every_injected_defect_is_detected(full_run):
    manifest, run_id = full_run
    rows, _ = _slices(run_id)
    injected = {}
    for d in manifest["defects"]:
        zs = [d["z"]] if d["type"] != "contrast_drift" else list(range(d["z"], d.get("z_to", d["z"]) + 1))
        for z in zs:
            injected.setdefault(z, set()).add(d["type"])
    missed = []
    for z, types in sorted(injected.items()):
        got = set(rows[z].failure_types or [])
        for t in types:
            if not (got & ACCEPTS[t]):
                missed.append((z, t, sorted(got)))
    assert not missed, f"注入后未检出: {missed}"
    kinds = {t for ts in injected.values() for t in ts}
    assert len(kinds) >= 14, f"合成集覆盖的缺陷种类偏少: {sorted(kinds)}"


def test_no_high_severity_false_positives_on_clean_slices(full_run):
    """干净切片上不应出现 high 及以上。这条比"能检出"更难保证，也更重要。"""
    manifest, run_id = full_run
    rows, _ = _slices(run_id)
    dirty = set()
    for d in manifest["defects"]:
        lo = d["z"]
        hi = d.get("z_to", d["z"])
        for z in range(min(lo, hi) - 1, max(lo, hi) + 2):  # 缺陷的紧邻切片也算受影响
            dirty.add(z)
    fp = [(z, r.failure_types, r.max_severity) for z, r in rows.items() if z not in dirty and r.max_severity in ("high", "critical")]
    assert not fp, f"干净切片上的 high+ 误报: {fp}"


def test_newly_implemented_checks_fire_on_their_own_defect(full_run):
    """四项 2026-09-14 新实现的检查，各自在对应的注入缺陷上确实给出了低分。"""
    manifest, run_id = full_run
    rows, _ = _slices(run_id)
    by_type = {d["type"]: d["z"] for d in manifest["defects"]}
    for defect, check in [("charging", "charging_artifact"), ("contamination", "contamination"),
                          ("section_deformation", "section_deformation"), ("local_deformation", "local_deformation")]:
        z = by_type.get(defect)
        if z is None:
            pytest.skip(f"合成集里没有 {defect}")
        score = rows[z].scores_json.get(check)
        assert score is not None, f"{check} 在 z{z} 上没有给分（注入的是 {defect}）"
        # 阈值按该检查自己的档位判（见各 check 的 default_thresholds，已用合成缺陷标定过）
        from emqc.qc import catalog

        th = {c["name"]: c["thresholds"] for c in catalog()}[check]
        assert score < th["low"], f"{check} 在注入了 {defect} 的 z{z} 上得分 {score}，未低于它的 low 阈值 {th['low']}"


def test_all_16_checks_score_on_a_clean_slice(full_run):
    """256 px 的序列缩略图足够大，16 项都应真正评估，不应有静默的空值。"""
    manifest, run_id = full_run
    rows, _ = _slices(run_id)
    dirty = {d["z"] for d in manifest["defects"]} | {d.get("z_to") for d in manifest["defects"]}
    clean = next(r for z, r in sorted(rows.items()) if z > 30 and z not in dirty and r.max_severity in ("none", "low"))
    notes = clean.stats_json.get("_notes") or {}
    unexplained = [k for k, v in clean.scores_json.items() if v is None and k not in notes]
    assert unexplained == [], f"这些检查给了 None 却没说原因: {unexplained}"
    assert len(clean.scores_json) == 16
