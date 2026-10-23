"""训练数据包：把一个数据集 QC 之后能用的部分，按「粒度 × 功能」两个层级一次产出，训练侧拿到就能用。

粒度（怎么切）
    slice    2D 切片      每张通过 QC 的切片裁到所在 block 的 tile，一张 PNG（+ 同尺寸标签 .npy）
    segment  3D 连续段    block 里连续通过 QC 的切片段（≥ min_run 张），按 shard_z 切成 (n, H, W) .npy（+ 标签）
    patch    3D patch     固定大小 (dz, dy, dx) 的小体块 .npy（+ 标签），带 Patch Factory 的采样与验收规则

功能（拿来训什么）
    raw            原图          只要 EM：自监督 / 预训练 / 去噪 / 超分
    qc             质检分类      坏切片 vs 好切片（标签 = 问题类型），训练 QC 模型本身
    segmentation   分割          EM + 实例 id（需要通过验证的 GT 分割）
    membrane       膜 / 边界     EM + 由 id 推出的边界图（同上）
    synapse        突触          EM + 突触掩码
    mitochondria   线粒体        EM + 线粒体 / 细胞器掩码
    hard_negative  困难负样本    模型预测与 GT 不一致的窗口（只有 patch 粒度）
    proofreading   校对候选      不一致度最高的窗口，带排名（只有 patch 粒度）

划分：train / val / test 跟 block 的 holdout 走（emqc.patches.partition），块边界是硬切分，任何粒度都不会把同一块切到两边。
质检分类例外：D 级 block 正是坏切片最多的地方，不能丢，没有划分的 block 归到 unsplit/，由训练侧决定怎么用。

每个样本一行写进 <功能>/<粒度>/index.csv（文件、标签文件、划分、block、坐标、等级、质量分、QC run），
整包的参数、血缘（QC run、标签版本）、各格数量写进 pack_manifest.json，README.md 里有读取示例。
"""
from __future__ import annotations

import csv
import io
import json
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.base import session_scope
from emqc.db.models import Block, Dataset, DatasetAsset, ExportEvent, ExportJob, QCSlice

from .generate import HOLDOUT, PatchSpec, _SliceCache, generate_patch_set, latest_run_per_block, readiness

log = __import__("logging").getLogger(__name__)

LEVELS = ("slice", "segment", "patch")
LEVEL_ZH = {"slice": "2D 切片", "segment": "3D 连续段", "patch": "3D patch"}
TASKS = ("raw", "qc", "segmentation", "membrane", "synapse", "mitochondria", "hard_negative", "proofreading")
TASK_ZH = {"raw": "原图", "qc": "质检分类", "segmentation": "分割", "membrane": "膜 / 边界", "synapse": "突触", "mitochondria": "线粒体",
           "hard_negative": "困难负样本", "proofreading": "校对候选"}
TASK_USE = {"raw": "自监督 / 预训练 / 去噪", "qc": "训练坏切片检测模型", "segmentation": "实例分割", "membrane": "边界 / 膜预测",
            "synapse": "突触检测", "mitochondria": "线粒体分割", "hard_negative": "针对模型错处再训练", "proofreading": "人工优先校对的位置"}
TASK_LABEL = {"raw": None, "qc": "类别（clean / 问题类型）", "segmentation": "实例 id（uint32）", "membrane": "边界 0/1（uint8）",
              "synapse": "掩码 0/1（uint8）", "mitochondria": "掩码 0/1（uint8）", "hard_negative": "实例 id（uint32）", "proofreading": "实例 id（uint32）"}
LEVELS_OF = {t: LEVELS for t in TASKS}
LEVELS_OF.update({"qc": ("slice", "patch"), "hard_negative": ("patch",), "proofreading": ("patch",)})


def default_params(**kw) -> dict:
    p = {"kind": "pack", "cells": [["raw", "slice"], ["raw", "patch"], ["qc", "slice"]], "max_slices": 400, "min_run": 8, "shard_z": 32, "max_segments": 200,
         "patch_size": [16, 256, 256], "n_patches": 64, "seed": 0, "qc_clean_ratio": 1.0, "min_quality": None}
    p.update({k: v for k, v in kw.items() if v is not None})
    return p


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------------- what is available


class _Ctx:
    """Everything the planner and the writer need about one dataset, read once."""

    def __init__(self, s: Session, ds: Dataset, p: dict):
        self.s, self.ds, self.p = s, ds, p
        self.blocks = list(s.scalars(select(Block).where(Block.dataset_id == ds.dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start)))
        self.runs = latest_run_per_block(s, ds, self.blocks)
        self.rows: dict[str, dict[int, QCSlice]] = {}
        for bid, rid in self.runs.items():
            self.rows[bid] = {x.z: x for x in s.scalars(select(QCSlice).where(QCSlice.run_id == rid, QCSlice.block_id == bid))}
        self.ready = readiness(s, ds)
        self.train_blocks = [b for b in self.blocks if (b.partition or "none") in HOLDOUT and b.block_id in self.runs]
        mq = p.get("min_quality")
        self.ok = lambda x: bool(x.passed) and (mq is None or (x.quality_score or 0) >= mq)

    def passed(self, b: Block) -> list[int]:
        return sorted(z for z, x in self.rows.get(b.block_id, {}).items() if self.ok(x))

    def failed(self, b: Block) -> list[int]:
        return sorted(z for z, x in self.rows.get(b.block_id, {}).items() if not x.passed)

    def label_asset(self, task: str) -> int | None:
        r = self.ready.get({"raw": "failure", "qc": "failure"}.get(task, task)) or {}
        return r.get("label_asset_id")

    def label_reason(self, task: str) -> str | None:
        if task in ("raw", "qc"):
            return None if self.runs else "还没有 QC 结果，先跑一次 QC"
        r = self.ready.get(task) or {}
        if r.get("ready"):
            return None
        reason = r.get("reason") or "缺少标签"
        if "is registered" in reason:
            return "需要模型预测（model_prediction），还没有登记" if task in ("hard_negative", "proofreading") and "model_prediction" in reason else "没有登记这类标签"
        if "not validated yet" in reason:
            import re

            ids = {int(x) for x in re.findall(r"/assets/(\d+)/validate", reason)}
            vol = [a for a in self.ds.assets if a.id in ids and ((a.format or "").startswith("image_stack") or (a.format or "").startswith("precomputed") or a.format == "npy")]
            if not vol:
                return "登记的是点 / 框标注（json），不是逐层掩码，出不了这类数据"
            what = "模型预测" if task in ("hard_negative", "proofreading") and "model_prediction" in reason else "标签"
            return f"{what}已登记，但还没验证（点上方「验证」）"
        return reason


def _even(seq: list, n: int) -> list:
    """n items spread evenly over seq (deterministic, keeps z order)."""
    if n <= 0 or len(seq) <= n:
        return list(seq)
    idx = np.linspace(0, len(seq) - 1, n).round().astype(int)
    return [seq[i] for i in sorted(set(idx.tolist()))]


def _part(b: Block, task: str) -> str:
    part = b.partition or "none"
    return part if part in HOLDOUT else ("unsplit" if task == "qc" else part)


def _slice_items(c: _Ctx, task: str) -> list[dict]:
    p = c.p
    if task == "qc":
        bad = [(b, z) for b in c.blocks if b.block_id in c.runs for z in c.failed(b)]
        good = [(b, z) for b in c.blocks if b.block_id in c.runs for z in c.passed(b)]
        half = max(1, int(p["max_slices"]) // 2)
        bad = _even(bad, half)
        good = _even(good, int(round(len(bad) * float(p.get("qc_clean_ratio") or 1.0))) or (half if not bad else 0))
        out = []
        for b, z in bad:
            x = c.rows[b.block_id][z]
            out.append({"b": b, "z": z, "cls": "fail", "label": "+".join(x.failure_types or []) or x.status or "fail"})
        out += [{"b": b, "z": z, "cls": "clean", "label": "clean"} for b, z in good]
        return out
    items = [{"b": b, "z": z} for b in c.train_blocks for z in c.passed(b)]
    return _even(items, int(p["max_slices"]))


def _segment_items(c: _Ctx) -> list[dict]:
    from emqc.qc.export import contiguous_runs, split_shards

    out = []
    for b in c.train_blocks:
        for z0, z1 in split_shards(contiguous_runs(c.passed(b), int(c.p["min_run"] or 1)), int(c.p["shard_z"] or 0)):
            out.append({"b": b, "z0": z0, "z1": z1})
    return _even(out, int(c.p["max_segments"]))


def _patch_capacity(c: _Ctx, task: str) -> int:
    """Upper bound on patches for the estimate (the generator may accept fewer)."""
    from emqc.db.models import QCFinding

    dz, dy, dx = c.p["patch_size"]
    n = int(c.p["n_patches"])
    if task == "qc":
        k = 0
        for b in c.blocks:
            if b.block_id in c.runs and b.z_end - b.z_start >= dz and b.y_end - b.y_start >= dy and b.x_end - b.x_start >= dx:
                k += c.s.query(QCFinding).filter(QCFinding.run_id == c.runs[b.block_id], QCFinding.block_id == b.block_id, QCFinding.z.is_not(None)).count()
        return min(n, k)
    if task == "raw":
        return len(raw_windows(c.s, c, c.ds, (dz, dy, dx), n, int(c.p.get("seed") or 0)))
    wins = 0
    for b in c.train_blocks:
        if b.y_end - b.y_start < dy or b.x_end - b.x_start < dx:
            continue
        ok = np.zeros(b.z_end - b.z_start, dtype=int)
        for z in c.passed(b):
            ok[z - b.z_start] = 1
        if len(ok) >= dz:
            wins += int((np.convolve(ok, np.ones(dz, dtype=int), mode="valid") == dz).sum())
    return min(n, wins * 4) if wins else 0


def raw_windows(s: Session, c: _Ctx, ds: Dataset, size, n: int, seed: int) -> list[dict]:
    """Random windows over passed sections of train/val/test blocks, overlapping each other by at most ~half a patch."""
    from .generate import _random_xy, _record, allowed_windows

    dz, dy, dx = size
    rng = np.random.default_rng(seed)
    cands, _ = allowed_windows(s, ds, c.train_blocks, dz, True, c.p.get("min_quality"))
    cands = [(b, z0) for b, z0 in cands if b.y_end - b.y_start >= dy and b.x_end - b.x_start >= dx]
    out, seen = [], set()
    for i in (rng.permutation(len(cands))[: n * 6] if cands else []):
        b, z0 = cands[int(i)]
        y0, x0 = _random_xy(rng, b, dy, dx)
        key = (b.block_id, z0 // max(1, dz // 2), y0 // max(1, dy // 2), x0 // max(1, dx // 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(_record(b, "raw", z0, y0, x0, size, {}))
        if len(out) >= n:
            break
    return out


def plan_pack(s: Session, ds: Dataset, params: dict) -> dict:
    """The task × level matrix for this dataset: per cell whether it can be made, why not, and how much it would be. Writes nothing."""
    p = default_params(**params)
    c = _Ctx(s, ds, p)
    itemsize = np.dtype(ds.dtype).itemsize
    dz, dy, dx = p["patch_size"]
    cells = []
    for t in TASKS:
        reason = c.label_reason(t)
        for lv in LEVELS:
            cell = {"task": t, "level": lv, "applicable": lv in LEVELS_OF[t], "ready": False, "reason": None, "n": 0, "est_bytes": 0, "by_partition": {}}
            if not cell["applicable"]:
                cell["reason"] = "这个功能没有这种粒度"
            elif reason:
                cell["reason"] = reason
            else:
                lbl_bytes = 0 if t in ("raw", "qc") else (4 if t in ("segmentation", "hard_negative", "proofreading") else 1)
                if lv == "slice":
                    items = _slice_items(c, t)
                    cell["n"] = len(items)
                    cell["est_bytes"] = sum((it["b"].y_end - it["b"].y_start) * (it["b"].x_end - it["b"].x_start) for it in items) * (itemsize + lbl_bytes)
                    for it in items:
                        k = _part(it["b"], t)
                        cell["by_partition"][k] = cell["by_partition"].get(k, 0) + 1
                    if t == "qc":
                        cell["by_class"] = {"fail": sum(1 for it in items if it["cls"] == "fail"), "clean": sum(1 for it in items if it["cls"] == "clean")}
                elif lv == "segment":
                    items = _segment_items(c)
                    cell["n"] = len(items)
                    cell["n_sections"] = sum(it["z1"] - it["z0"] for it in items)
                    cell["est_bytes"] = sum((it["z1"] - it["z0"]) * (it["b"].y_end - it["b"].y_start) * (it["b"].x_end - it["b"].x_start) for it in items) * (itemsize + lbl_bytes)
                    for it in items:
                        k = _part(it["b"], t)
                        cell["by_partition"][k] = cell["by_partition"].get(k, 0) + 1
                else:
                    cell["n"] = _patch_capacity(c, t)
                    cell["est_bytes"] = cell["n"] * dz * dy * dx * (itemsize + lbl_bytes)
                    cell["approx"] = True
                cell["ready"] = cell["n"] > 0
                if not cell["ready"]:
                    cell["reason"] = {"slice": "没有符合条件的切片", "segment": f"没有 ≥ {p['min_run']} 张的连续通过段", "patch": f"没有放得下 {dz}×{dy}×{dx} 的窗口"}[lv]
            cells.append(cell)
    labels = [{"asset_id": a.id, "asset_type": a.asset_type, "path": a.path, "format": a.format,
               "validated": bool((a.extra_json or {}).get("validation")), "usable": bool((a.extra_json or {}).get("validation", {}).get("usable_for_patches")),
               "can_validate": (a.format or "").startswith("image_stack") or a.format in ("npy", "precomputed")}
              for a in ds.assets if a.asset_type not in ("em_image",)]
    parts: dict[str, int] = {}
    for b in c.blocks:
        parts[b.partition or "none"] = parts.get(b.partition or "none", 0) + 1
    return {"dataset_id": ds.dataset_id, "run_id": ds.latest_run_id, "params": p, "tasks": [{"task": t, "zh": TASK_ZH[t], "use": TASK_USE[t], "label": TASK_LABEL[t]} for t in TASKS],
            "levels": [{"level": lv, "zh": LEVEL_ZH[lv]} for lv in LEVELS], "cells": cells, "labels": labels, "blocks_by_partition": parts,
            "n_blocks": len(c.blocks), "n_blocks_qc": len(c.runs), "n_train_blocks": len(c.train_blocks)}


# ----------------------------------------------------------------------------- writing


class _Writer:
    def __init__(self, root: Path):
        self.root, self.n_files, self.n_bytes = root, 0, 0

    def npy(self, rel: str, arr: np.ndarray) -> str:
        f = self.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".part")
        with open(tmp, "wb") as fh:
            np.save(fh, arr)
        tmp.replace(f)
        self.n_files += 1
        self.n_bytes += f.stat().st_size
        return rel

    def png(self, rel: str, arr: np.ndarray) -> str:
        from PIL import Image

        f = self.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        a = arr if arr.dtype == np.uint8 else _to_u8(arr)
        Image.fromarray(a).save(f)
        self.n_files += 1
        self.n_bytes += f.stat().st_size
        return rel


def _to_u8(a: np.ndarray) -> np.ndarray:
    a = a.astype(np.float32)
    lo, hi = np.percentile(a, 0.5), np.percentile(a, 99.5)
    return (np.clip((a - lo) / ((hi - lo) or 1), 0, 1) * 255).astype(np.uint8)


def _derive(task: str, ids: np.ndarray) -> np.ndarray:
    from emqc.registry.labels import boundary_map

    if task == "membrane":
        return boundary_map(ids) if ids.ndim == 2 else np.stack([boundary_map(x) for x in ids])
    if task in ("synapse", "mitochondria"):
        return (ids != 0).astype(np.uint8)
    return ids.astype(np.uint32)


INDEX_COLS = ["file", "label_file", "label", "partition", "block_id", "z0", "z1", "y0", "y1", "x0", "x1", "grade", "quality", "qc_run", "extra"]


class PackRunner:
    def __init__(self, job_id: int):
        self.job_id = job_id

    def event(self, s: Session, job: ExportJob, message: str, level: str = "info", data: dict | None = None) -> None:
        s.add(ExportEvent(job_id=job.id, level=level, message=message[:512], data_json=data or {}))
        s.commit()

    def run(self) -> dict:
        from emqc.qc.labels import open_label_for
        from emqc.qc.runner import open_dataset_volume

        t_start = time.perf_counter()
        with session_scope() as s:
            job = s.get(ExportJob, self.job_id)
            ds = s.get(Dataset, job.dataset_id)
            p = job.params_json
            root = Path(job.out_dir) / ds.dataset_id
            root.mkdir(parents=True, exist_ok=True)
            try:
                c = _Ctx(s, ds, p)
                if not c.runs:
                    raise RuntimeError(f"{ds.dataset_id} 还没有 QC 结果，先跑一次 QC")
                cells = [tuple(x) for x in p["cells"] if x[0] in TASKS and x[1] in LEVELS_OF[x[0]]]
                job.status, job.started_at, job.n_blocks, job.n_blocks_done, job.run_id = "running", _now(), len(cells), 0, ds.latest_run_id
                s.commit()
                self.event(s, job, f"训练数据包开始：{len(cells)} 格（{', '.join(TASK_ZH[t] + '·' + LEVEL_ZH[lv] for t, lv in cells)}）", data={"params": p})
                w = _Writer(root)
                em = _SliceCache(open_dataset_volume(ds), maxsize=48)
                labels: dict[int, _SliceCache] = {}
                manifest = {"kind": "training_pack", "dataset_id": ds.dataset_id, "name": ds.name, "source": ds.root_path, "dtype": ds.dtype,
                            "voxel_size_nm": [ds.voxel_size_x_nm, ds.voxel_size_y_nm, ds.voxel_size_z_nm], "z_offset": (ds.metadata_json or {}).get("z_offset", 0),
                            "qc_run_by_block": c.runs, "params": p, "job_id": job.id, "generated_at": _now().isoformat(timespec="seconds"), "status": "running", "cells": []}
                cancelled = False
                for task, lv in cells:
                    s.refresh(job)
                    if job.cancel_requested:
                        cancelled = True
                        break
                    t0 = time.perf_counter()
                    reason = c.label_reason(task)
                    if reason:
                        manifest["cells"].append({"task": task, "level": lv, "status": "skipped", "reason": reason, "n": 0})
                        self.event(s, job, f"{TASK_ZH[task]}·{LEVEL_ZH[lv]}：跳过（{reason}）", level="warn")
                        job.n_blocks_done += 1
                        s.commit()
                        continue
                    lab = None
                    aid = c.label_asset(task)
                    if aid:
                        if aid not in labels:
                            labels[aid] = _SliceCache(open_label_for(ds, s.get(DatasetAsset, aid)), maxsize=16)
                        lab = labels[aid]
                    rows, extra_info = self._cell(s, c, ds, w, em, lab, task, lv)
                    base = f"{task}/{lv}"
                    buf = io.StringIO()
                    wr = csv.DictWriter(buf, fieldnames=INDEX_COLS)
                    wr.writeheader()
                    for r in rows:
                        wr.writerow({k: (json.dumps(r[k], ensure_ascii=False) if k == "extra" else r.get(k)) for k in INDEX_COLS})
                    (root / base).mkdir(parents=True, exist_ok=True)
                    (root / base / "index.csv").write_text(buf.getvalue(), encoding="utf-8")
                    by_part: dict[str, int] = {}
                    for r in rows:
                        by_part[r["partition"]] = by_part.get(r["partition"], 0) + 1
                    entry = {"task": task, "task_zh": TASK_ZH[task], "level": lv, "level_zh": LEVEL_ZH[lv], "status": "done" if rows else "empty", "n": len(rows), "by_partition": by_part,
                             "index": f"{base}/index.csv", "label": TASK_LABEL[task], "label_asset_id": aid, "wall_s": round(time.perf_counter() - t0, 1), **extra_info}
                    manifest["cells"].append(entry)
                    job.n_sections += len(rows)
                    job.n_shards, job.n_bytes = w.n_files, w.n_bytes
                    job.n_blocks_done += 1
                    s.commit()
                    (root / "pack_manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
                    self.event(s, job, f"{TASK_ZH[task]}·{LEVEL_ZH[lv]}：{len(rows)} 个样本 {json.dumps(by_part, ensure_ascii=False)}，{entry['wall_s']} s",
                               data={k: v for k, v in entry.items() if k != "label"})
                manifest["status"] = "cancelled" if cancelled else "done"
                manifest["n_files"], manifest["bytes"] = w.n_files, w.n_bytes
                (root / "pack_manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
                (root / "README.md").write_text(readme(manifest), encoding="utf-8")
                job.status, job.finished_at = manifest["status"], _now()
                s.commit()
                self.event(s, job, f"训练数据包{'已取消' if cancelled else '完成'}：{job.n_sections} 个样本、{w.n_files} 个文件、{w.n_bytes / 1e6:.1f} MB，{time.perf_counter() - t_start:.0f} s，在 {root}",
                           level="warn" if cancelled else "info", data={"out_dir": str(root)})
                return manifest
            except Exception as e:
                s.rollback()
                job = s.get(ExportJob, self.job_id)
                job.status, job.error, job.finished_at = "error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}", _now()
                s.commit()
                self.event(s, job, f"训练数据包失败：{type(e).__name__}: {e}"[:500], level="error")
                raise

    # one cell -> index rows
    def _cell(self, s, c: _Ctx, ds, w: _Writer, em: _SliceCache, lab: _SliceCache | None, task: str, lv: str) -> tuple[list[dict], dict]:
        rows, info = [], {}
        crop = lambda a, b: a[b.y_start:b.y_end, b.x_start:b.x_end]
        unreadable = []

        def emz(z):
            """EM section z, or None when it cannot be decoded (missing / corrupt sections are exactly what QC flags)."""
            try:
                return em.get(z)
            except Exception as e:
                unreadable.append(z)
                log.info("pack: section %s unreadable: %s", z, e)
                return None

        def row(file, b, z0, z1, y0, y1, x0, x1, label_file=None, label=None, part=None, extra=None):
            x = c.rows.get(b.block_id, {}).get(z0)
            rows.append({"file": file, "label_file": label_file, "label": label, "partition": part or _part(b, task), "block_id": b.block_id, "z0": z0, "z1": z1,
                         "y0": y0, "y1": y1, "x0": x0, "x1": x1, "grade": b.latest_grade, "quality": None if x is None or x.quality_score is None else round(float(x.quality_score), 4),
                         "qc_run": c.runs.get(b.block_id), "extra": extra or {}})

        if lv == "slice":
            items = _slice_items(c, task)
            n_empty = 0
            for it in items:
                b, z = it["b"], it["z"]
                part = _part(b, task)
                stem = f"{b.block_id}__z{z:05d}"
                img = emz(z)
                if img is None:
                    continue
                if task == "qc":
                    f = w.png(f"{task}/slice/{part}/{it['cls']}/{stem}.png", crop(img, b))
                    row(f, b, z, z + 1, b.y_start, b.y_end, b.x_start, b.x_end, label=it["label"], part=part)
                    continue
                lf = None
                if lab is not None:
                    L = _derive(task, crop(lab.get(z), b))
                    if not L.any():
                        n_empty += 1
                        continue
                    lf = w.npy(f"{task}/slice/{part}/labels/{stem}.npy", L)
                f = w.png(f"{task}/slice/{part}/images/{stem}.png", crop(img, b))
                row(f, b, z, z + 1, b.y_start, b.y_end, b.x_start, b.x_end, label_file=lf)
            if n_empty:
                info["skipped_empty_label"] = n_empty
        elif lv == "segment":
            for it in _segment_items(c):
                b, z0, z1 = it["b"], it["z0"], it["z1"]
                part = _part(b, task)
                stem = f"{b.block_id}__z{z0:05d}-{z1 - 1:05d}"
                secs = [emz(z) for z in range(z0, z1)]
                if any(x is None for x in secs):
                    continue
                f = w.npy(f"{task}/segment/{part}/images/{stem}.npy", np.stack([crop(x, b) for x in secs]))
                lf = None
                if lab is not None:
                    lf = w.npy(f"{task}/segment/{part}/labels/{stem}.npy", _derive(task, np.stack([crop(lab.get(z), b) for z in range(z0, z1)])))
                row(f, b, z0, z1, b.y_start, b.y_end, b.x_start, b.x_end, label_file=lf)
        else:
            dz, dy, dx = (int(v) for v in c.p["patch_size"])
            recs = self._patch_records(s, c, ds, task, (dz, dy, dx), info)
            by = {b.block_id: b for b in c.blocks}
            for k, r in enumerate(recs):
                b = by[r["block_id"]]
                part = _part(b, task)
                sub = f"{part}/{r['meta'].get('failure_type', 'other')}" if task == "qc" else part
                stem = f"p{k:05d}__{b.block_id}__z{r['z0']:05d}_y{r['y0']:05d}_x{r['x0']:05d}"
                secs = [emz(z) for z in range(r["z0"], r["z1"])]
                if task == "qc":   # a failure window may hold the broken section itself: keep it, as zeros
                    secs = [np.zeros((ds.size_y, ds.size_x), dtype=ds.dtype) if x is None else x for x in secs]
                elif any(x is None for x in secs):
                    continue
                vol = np.stack([x[r["y0"]:r["y1"], r["x0"]:r["x1"]] for x in secs])
                f = w.npy(f"{task}/patch/{sub}/images/{stem}.npy", vol)
                lf = None
                if lab is not None and task != "qc":
                    lf = w.npy(f"{task}/patch/{sub}/labels/{stem}.npy", _derive(task, np.stack([lab.get(z)[r["y0"]:r["y1"], r["x0"]:r["x1"]] for z in range(r["z0"], r["z1"])])))
                label = r["meta"].get("failure_type") if task == "qc" else None
                row(f, b, r["z0"], r["z1"], r["y0"], r["y1"], r["x0"], r["x1"], label_file=lf, label=label, part=part, extra=r["meta"])
        if unreadable:
            info["unreadable_sections"] = sorted(set(unreadable))
        return rows, info

    def _patch_records(self, s, c: _Ctx, ds, task: str, size, info: dict) -> list[dict]:
        """raw: random windows over passed sections (no label needed); everything else: the Patch Factory generator (saved as a patch set too)."""
        n, seed = int(c.p["n_patches"]), int(c.p.get("seed") or 0)
        if task == "raw":
            return raw_windows(s, c, ds, size, n, seed)
        ptype = "failure" if task == "qc" else task
        spec = PatchSpec(patch_type=ptype, size=tuple(size), n=n, seed=seed, min_quality=c.p.get("min_quality"))
        pset = generate_patch_set(s, ds, spec)
        info["patch_set_id"], info["patch_set_status"] = pset.id, pset.status
        if pset.reason:
            info["patch_set_reason"] = pset.reason
        from emqc.db.models import Patch

        return [{"block_id": x.block_id, "z0": x.z0, "z1": x.z1, "y0": x.y0, "y1": x.y1, "x0": x.x0, "x1": x.x1, "meta": x.meta_json or {}}
                for x in s.scalars(select(Patch).where(Patch.set_id == pset.id).order_by(Patch.id))]


def readme(m: dict) -> str:
    rows = "\n".join(f"| {c.get('task_zh', TASK_ZH.get(c['task']))} | {c.get('level_zh', LEVEL_ZH.get(c['level']))} | {c['n']} | "
                     f"{' / '.join(f'{k} {v}' for k, v in (c.get('by_partition') or {}).items()) or '–'} | `{c.get('index', '–')}` | {c.get('label') or '–'} |"
                     for c in m["cells"])
    return f"""# 训练数据包 · {m['dataset_id']}

生成于 {m['generated_at']}（平台导出任务 #{m['job_id']}）。数据来自各 block 最近一次 QC 运行，只含通过 QC 的切片（质检分类除外）。

| 功能 | 粒度 | 样本数 | 划分 | 索引 | 标签 |
|---|---|---:|---|---|---|
{rows}

## 目录

    <功能>/<粒度>/index.csv                       每个样本一行：file, label_file, label, partition, block_id, z0..x1, grade, quality, qc_run, extra
    <功能>/slice/<划分>/images/*.png  labels/*.npy   2D：一张切片裁到所在 block 的 tile
    <功能>/segment/<划分>/images/*.npy             3D 连续段：(n, H, W)，n 张切片全部通过 QC
    <功能>/patch/<划分>/images/*.npy               3D patch：(dz, dy, dx)
    qc/slice/<划分>/{{clean,fail}}/*.png             质检分类：和 torchvision ImageFolder 的目录结构一样

坐标都是数据集体素坐标（z 加上 manifest 里的 z_offset 就是原始切片号）。train / val / test 跟 block 走，同一个 block 不会出现在两边。
质检分类里没有划分的 block（D 级等）放在 unsplit/。

## 读取

```python
import csv, numpy as np
from PIL import Image

rows = list(csv.DictReader(open("segmentation/patch/index.csv")))
train = [r for r in rows if r["partition"] == "train"]
x = np.load(train[0]["file"])                 # (16, 256, 256) uint8
y = np.load(train[0]["label_file"])           # 同形状：实例 id / 边界 / 掩码

img = np.asarray(Image.open("raw/slice/train/images/<文件名>.png"))   # 2D
```

PyTorch：

```python
class PackDataset(torch.utils.data.Dataset):
    def __init__(self, index_csv, partition="train"):
        self.rows = [r for r in csv.DictReader(open(index_csv)) if r["partition"] == partition]
    def __len__(self): return len(self.rows)
    def __getitem__(self, i):
        r = self.rows[i]
        x = np.load(r["file"]) if r["file"].endswith(".npy") else np.asarray(Image.open(r["file"]))
        x = (x.astype("float32") - x.mean()) / (x.std() + 1e-6)
        y = np.load(r["label_file"]) if r["label_file"] else r["label"]
        return x, y
```

完整的参数、每个 block 用的 QC run、标签版本在 `pack_manifest.json`。
"""


# ----------------------------------------------------------------------------- job control

_jobs: dict[int, threading.Thread] = {}


def create_pack_job(dataset_id: str, **params) -> int:
    with session_scope() as s:
        ds = s.get(Dataset, dataset_id)
        if ds is None:
            raise KeyError(dataset_id)
        job = ExportJob(dataset_id=dataset_id, run_id=ds.latest_run_id, params_json=default_params(**params), out_dir="")
        s.add(job)
        s.flush()
        job.out_dir = str(Path(settings.export_dir) / "packs" / f"pack-{job.id:04d}")
        return job.id


def start_pack_async(dataset_id: str, **params) -> int:
    job_id = create_pack_job(dataset_id, **params)

    def _target():
        from emqc.jobslots import slot

        try:
            with slot("export"):
                PackRunner(job_id).run()
        except Exception:
            log.exception("pack job %s failed", job_id)

    t = threading.Thread(target=_target, name=f"pack-{job_id}", daemon=True)
    _jobs[job_id] = t
    t.start()
    return job_id


def build_pack(dataset_id: str, **params) -> dict:
    """Synchronous wrapper (CLI, tests)."""
    job_id = create_pack_job(dataset_id, **params)
    m = PackRunner(job_id).run()
    m["job_id"] = job_id
    return m
