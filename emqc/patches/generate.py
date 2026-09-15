"""Patch generators (requirement 3). A patch set is coordinates + lineage; pixels are cut on demand through the data API.

    failure        around QC findings (crack / no_coverage / saturation / misalignment ...) - needs only QC results
    segmentation   windows over passed sections whose GT ids show >= 2 objects and enough foreground
    membrane       same source, target = boundary map derived from the ids; needs enough boundary pixels
    synapse        windows centred on foreground components of a z-aligned synapse mask / prediction
    mitochondria   same for a mitochondria / organelle mask
    hard_negative  windows where model prediction and GT disagree beyond a threshold
    proofreading   the top-n most disagreeing windows (what a human should look at first)

Every generator returns (status, reason, patches). A generator that cannot run on this dataset says why
(no_aligned_label / needs_prediction_asset) instead of producing an empty set silently.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.models import PATCH_TYPES, Block, Dataset, DatasetAsset, Patch, PatchSet, QCFinding, QCSlice
from emqc.registry.labels import boundary_map

from .checks import check_patches

SEV_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
HOLDOUT = ("train", "val", "test")


@dataclass
class PatchSpec:
    patch_type: str
    size: tuple[int, int, int] = (16, 256, 256)  # dz, dy, dx
    n: int = 64
    seed: int = 0
    partitions: list[str] | None = None  # default: train/val/test (failure: every block)
    block_ids: list[str] | None = None
    only_passed: bool = True
    min_quality: float | None = None
    label_asset_id: int | None = None
    params: dict = field(default_factory=dict)
    preprocessing: dict | None = None
    augmentation: dict | None = None
    candidate_factor: int = 8  # how many random candidates to try per accepted patch

    DEFAULTS = {"min_fg_frac": 0.3, "min_ids": 2, "min_boundary_frac": 0.02, "min_mask_frac": 0.002, "min_disagreement": 0.05, "min_severity": "low"}

    def p(self, k):
        return self.params.get(k, self.DEFAULTS[k])

    def as_dict(self) -> dict:
        return {"patch_type": self.patch_type, "size": list(self.size), "n": self.n, "seed": self.seed, "partitions": self.partitions, "block_ids": self.block_ids,
                "only_passed": self.only_passed, "min_quality": self.min_quality, "label_asset_id": self.label_asset_id, "params": {**self.DEFAULTS, **self.params}, "candidate_factor": self.candidate_factor}


class _SliceCache:
    """Small LRU of decoded label sections (a 2048^2 uint32 section is 16 MB; 8 of them is fine)."""

    def __init__(self, reader, maxsize: int = 8):
        self.reader, self.maxsize, self._d = reader, maxsize, {}

    def get(self, z: int) -> np.ndarray:
        a = self._d.pop(z, None)
        if a is None:
            a = self.reader.read_slice(z)
            if len(self._d) >= self.maxsize:
                self._d.pop(next(iter(self._d)))
        self._d[z] = a
        return a


def usable_label_assets(ds: Dataset, types: tuple[str, ...], hint: str | None = None) -> list[DatasetAsset]:
    out = []
    for a in ds.assets:
        if a.asset_type not in types:
            continue
        if hint and hint not in (a.path or "").lower() and (a.extra_json or {}).get("class", "").lower() != hint:
            continue
        if (a.extra_json or {}).get("validation", {}).get("usable_for_patches"):
            out.append(a)
    return out


def _explain_missing(ds: Dataset, types: tuple[str, ...], hint: str | None) -> str:
    cands = [a for a in ds.assets if a.asset_type in types and (not hint or hint in (a.path or "").lower() or (a.extra_json or {}).get("class", "").lower() == hint)]
    if not cands:
        return f"no asset of type {list(types)}" + (f" for '{hint}'" if hint else "") + " is registered"
    parts = []
    for a in cands:
        v = (a.extra_json or {}).get("validation")
        if not v:
            parts.append(f"{a.path}: not validated yet (POST /datasets/{ds.dataset_id}/assets/{a.id}/validate)")
        else:
            parts.append(f"{a.path}: validation {v.get('status')}, " + ("; ".join(v.get("notes") or []) or json.dumps(v.get("issues") or {})))
    return "no z-aligned, EM-resolution label usable: " + " | ".join(parts)


def readiness(s: Session, ds: Dataset) -> dict:
    """Per patch type: can it be generated on this dataset right now, and if not, why."""
    out = {}
    has_qc = bool(ds.latest_run_id)
    gt = usable_label_assets(ds, ("gt_segmentation",))
    pred = usable_label_assets(ds, ("model_prediction",))
    syn = usable_label_assets(ds, ("synapse_prediction", "gt_annotation"), "synapse")
    mito = usable_label_assets(ds, ("mitochondria_prediction", "organelle_prediction", "gt_annotation"), "mito")
    out["failure"] = {"ready": has_qc, "reason": None if has_qc else "run QC first", "needs": ["qc"]}
    for t in ("segmentation", "membrane"):
        out[t] = {"ready": has_qc and bool(gt), "reason": None if gt else _explain_missing(ds, ("gt_segmentation",), None), "needs": ["qc", "gt_segmentation"], "label_asset_id": gt[0].id if gt else None}
    out["synapse"] = {"ready": has_qc and bool(syn), "reason": None if syn else _explain_missing(ds, ("synapse_prediction", "gt_annotation"), "synapse"), "needs": ["qc", "synapse mask"], "label_asset_id": syn[0].id if syn else None}
    out["mitochondria"] = {"ready": has_qc and bool(mito), "reason": None if mito else _explain_missing(ds, ("mitochondria_prediction", "organelle_prediction", "gt_annotation"), "mito"), "needs": ["qc", "mitochondria mask"], "label_asset_id": mito[0].id if mito else None}
    for t in ("hard_negative", "proofreading"):
        r = None if (gt and pred) else (_explain_missing(ds, ("model_prediction",), None) if gt else _explain_missing(ds, ("gt_segmentation",), None))
        out[t] = {"ready": has_qc and bool(gt) and bool(pred), "reason": r, "needs": ["qc", "gt_segmentation", "model_prediction"], "label_asset_id": gt[0].id if gt else None, "prediction_asset_id": pred[0].id if pred else None}
    return out


def latest_run_per_block(s: Session, ds: Dataset, blocks: list[Block]) -> dict[str, int]:
    """block_id -> id of the most recent QC run that covered that block (a partial run only updates its own blocks)."""
    from emqc.db.models import QCBlock

    out: dict[str, int] = {}
    ids = {b.block_id for b in blocks}
    for bid, rid in s.execute(select(QCBlock.block_id, func.max(QCBlock.run_id)).where(QCBlock.dataset_id == ds.dataset_id).group_by(QCBlock.block_id)):
        if bid in ids:
            out[bid] = int(rid)
    return out


def allowed_windows(s: Session, ds: Dataset, blocks: list[Block], dz: int, only_passed: bool, min_quality: float | None) -> tuple[list[tuple[Block, int]], int]:
    """(block, z0) windows of dz sections that all satisfy the QC filter, judged by each block's latest QC run."""
    allowed = {b.block_id: np.ones(b.z_end - b.z_start, dtype=bool) for b in blocks}
    runs = latest_run_per_block(s, ds, blocks)
    use_qc = bool(runs and (only_passed or min_quality is not None))
    if use_qc:
        for m in allowed.values():
            m[:] = False
        by = {b.block_id: b for b in blocks}
        for bid, rid in runs.items():
            b = by[bid]
            for z, passed, q in s.execute(select(QCSlice.z, QCSlice.passed, QCSlice.quality_score).where(QCSlice.run_id == rid, QCSlice.block_id == bid)):
                allowed[bid][z - b.z_start] = (bool(passed) or not only_passed) and (min_quality is None or (q or 0.0) >= min_quality)
    cands = []
    for b in blocks:
        if b.z_end - b.z_start < dz:
            continue
        ok = np.convolve(allowed[b.block_id].astype(int), np.ones(dz, dtype=int), mode="valid") == dz
        cands.extend((b, b.z_start + int(i)) for i in np.flatnonzero(ok))
    return cands, int(sum(int(m.sum()) for m in allowed.values()))


def _clamp(v, lo, hi):
    return int(max(lo, min(v, hi)))


def _record(b: Block, ptype: str, z0, y0, x0, size, meta) -> dict:
    dz, dy, dx = size
    return {"block_id": b.block_id, "patch_type": ptype, "z0": int(z0), "z1": int(z0 + dz), "y0": int(y0), "y1": int(y0 + dy), "x0": int(x0), "x1": int(x0 + dx),
            "partition": b.partition or "none", "difficulty": b.difficulty, "grade": b.latest_grade, "region": b.region, "meta": meta}


def _random_xy(rng, b: Block, dy, dx):
    return int(rng.integers(b.y_start, b.y_end - dy + 1)), int(rng.integers(b.x_start, b.x_end - dx + 1))


# ----------------------------------------------------------------------------- generators


def gen_failure(s: Session, ds: Dataset, blocks: list[Block], spec: PatchSpec, rng) -> tuple[str, str | None, list[dict]]:
    runs = latest_run_per_block(s, ds, blocks)
    if not runs:
        return "error", "dataset has no QC run", []
    dz, dy, dx = spec.size
    by = {b.block_id: b for b in blocks}
    min_rank = SEV_RANK.get(spec.p("min_severity"), 1)
    rows = []
    for bid, rid in runs.items():
        rows.extend(f for f in s.scalars(select(QCFinding).where(QCFinding.run_id == rid, QCFinding.block_id == bid, QCFinding.z.is_not(None))) if SEV_RANK.get(f.severity, 0) >= min_rank)
    rows.sort(key=lambda f: (-SEV_RANK.get(f.severity, 0), f.z, f.check_name))
    out, seen = [], set()
    for f in rows:
        b = by[f.block_id]
        if b.z_end - b.z_start < dz or b.y_end - b.y_start < dy or b.x_end - b.x_start < dx:
            continue
        z0 = _clamp(f.z - dz // 2, b.z_start, b.z_end - dz)
        bbox = (f.coordinate_json or {}).get("bbox")
        if bbox and len(bbox) == 4:
            cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
            y0, x0 = _clamp(cy - dy / 2, b.y_start, b.y_end - dy), _clamp(cx - dx / 2, b.x_start, b.x_end - dx)
        else:
            y0, x0 = _random_xy(rng, b, dy, dx)
        key = (b.block_id, z0, y0, x0, f.failure_type)
        if key in seen:
            continue
        seen.add(key)
        out.append(_record(b, "failure", z0, y0, x0, spec.size, {"failure_type": f.failure_type, "severity": f.severity, "check": f.check_name, "score": f.score, "z_center": f.z, "has_bbox": bool(bbox), "finding_id": f.id}))
        if len(out) >= spec.n:
            break
    return ("done" if out else "empty"), (None if out else f"no findings at severity >= {spec.p('min_severity')}"), out


def gen_label_windows(s: Session, ds: Dataset, blocks: list[Block], spec: PatchSpec, rng, label: DatasetAsset, accept) -> tuple[str, str | None, list[dict]]:
    """Shared driver for segmentation / membrane / hard_negative / proofreading: random candidate windows, an accept
    function looking at the centre section of the labels."""
    from emqc.qc.labels import open_label_for

    dz, dy, dx = spec.size
    cands, n_allowed = allowed_windows(s, ds, blocks, dz, spec.only_passed, spec.min_quality)
    cands = [(b, z0) for b, z0 in cands if b.y_end - b.y_start >= dy and b.x_end - b.x_start >= dx]
    if not cands:
        return "empty", f"no z window of {dz} sections satisfies the QC filter ({n_allowed} allowed sections)", []
    cache = _SliceCache(open_label_for(ds, label))
    out, seen, tried, scored = [], set(), 0, []
    budget = spec.n * spec.candidate_factor
    order = rng.permutation(len(cands))
    i = 0
    while tried < budget and (spec.patch_type == "proofreading" or len(out) < spec.n):
        b, z0 = cands[int(order[i % len(order)])]
        i += 1
        tried += 1
        y0, x0 = _random_xy(rng, b, dy, dx)
        key = (b.block_id, z0, y0 // 8, x0 // 8)
        if key in seen:
            continue
        seen.add(key)
        zc = z0 + dz // 2
        try:
            L = cache.get(zc)[y0 : y0 + dy, x0 : x0 + dx]
        except Exception as e:
            return "error", f"label read failed at z={zc}: {type(e).__name__}: {e}", out
        ok, meta = accept(L, zc, y0, x0)
        if ok:
            rec = _record(b, spec.patch_type, z0, y0, x0, spec.size, meta)
            if spec.patch_type == "proofreading":
                scored.append(rec)
            else:
                out.append(rec)
    if spec.patch_type == "proofreading":
        scored.sort(key=lambda r: -r["meta"].get("disagreement", 0))
        out = scored[: spec.n]
        for k, r in enumerate(out):
            r["meta"]["rank"] = k + 1
    status = "done" if out else "empty"
    return status, (None if out else f"none of {tried} candidate windows met the acceptance rule {spec.as_dict()['params']}"), out


def gen_mask_components(s: Session, ds: Dataset, blocks: list[Block], spec: PatchSpec, rng, label: DatasetAsset) -> tuple[str, str | None, list[dict]]:
    """synapse / mitochondria: windows centred on connected components of the mask."""
    from emqc.qc.labels import open_label_for

    dz, dy, dx = spec.size
    cands, _ = allowed_windows(s, ds, blocks, dz, spec.only_passed, spec.min_quality)
    cands = [(b, z0) for b, z0 in cands if b.y_end - b.y_start >= dy and b.x_end - b.x_start >= dx]
    if not cands:
        return "empty", "no z window satisfies the QC filter", []
    cache = _SliceCache(open_label_for(ds, label))
    out, seen = [], set()
    for idx in rng.permutation(len(cands)):
        b, z0 = cands[int(idx)]
        zc = z0 + dz // 2
        M = cache.get(zc)[b.y_start : b.y_end, b.x_start : b.x_end] != 0
        if not M.any():
            continue
        lab, n = ndimage.label(M)
        cents = ndimage.center_of_mass(M, lab, index=np.arange(1, n + 1))
        sizes = ndimage.sum(M, lab, index=np.arange(1, n + 1))
        for (cy, cx), sz in sorted(zip(cents, sizes), key=lambda t: -t[1]):
            y0 = _clamp(b.y_start + cy - dy / 2, b.y_start, b.y_end - dy)
            x0 = _clamp(b.x_start + cx - dx / 2, b.x_start, b.x_end - dx)
            key = (b.block_id, z0, y0 // 16, x0 // 16)
            if key in seen:
                continue
            seen.add(key)
            win = cache.get(zc)[y0 : y0 + dy, x0 : x0 + dx] != 0
            frac = float(win.mean())
            if frac < spec.p("min_mask_frac"):
                continue
            out.append(_record(b, spec.patch_type, z0, y0, x0, spec.size, {"mask_frac": frac, "component_px": int(sz), "n_components_in_section": int(n), "centroid_yx": [round(float(b.y_start + cy), 1), round(float(b.x_start + cx), 1)]}))
            if len(out) >= spec.n:
                return "done", None, out
    return ("done" if out else "empty"), (None if out else "mask has no components inside the allowed windows"), out


def generate_patch_set(s: Session, ds: Dataset, spec: PatchSpec) -> PatchSet:
    if spec.patch_type not in PATCH_TYPES:
        raise ValueError(f"patch_type must be one of {PATCH_TYPES}")
    t0 = time.perf_counter()
    rng = np.random.default_rng(spec.seed)
    blocks = list(s.scalars(select(Block).where(Block.dataset_id == ds.dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start)))
    if spec.block_ids:
        blocks = [b for b in blocks if b.block_id in spec.block_ids]
    parts = spec.partitions if spec.partitions is not None else (None if spec.patch_type == "failure" else list(HOLDOUT))
    if parts is not None:
        blocks = [b for b in blocks if (b.partition or "none") in parts]
    ready = readiness(s, ds)[spec.patch_type]
    label = None
    if spec.label_asset_id:
        label = s.get(DatasetAsset, spec.label_asset_id)
    elif ready.get("label_asset_id"):
        label = s.get(DatasetAsset, ready["label_asset_id"])
    pset = PatchSet(dataset_id=ds.dataset_id, patch_type=spec.patch_type, params_json={**spec.as_dict(), "qc_run_by_block": latest_run_per_block(s, ds, blocks)}, qc_run_id=ds.latest_run_id, label_asset_id=label.id if label else None,
                    label_version=(label.version or label.path) if label else None, source_volume=blocks[0].source_volume if blocks else None,
                    preprocessing_json=spec.preprocessing or json.loads(settings.patch_preprocessing), augmentation_json=spec.augmentation or json.loads(settings.patch_augmentation))
    if not blocks:
        status, reason, recs = "empty", f"no block matches partitions={parts} block_ids={spec.block_ids}", []
    elif not ready["ready"] and spec.patch_type != "failure":
        status, reason, recs = ("needs_prediction_asset" if spec.patch_type in ("hard_negative", "proofreading") and ready.get("label_asset_id") else "no_aligned_label"), ready["reason"], []
    elif spec.patch_type == "failure":
        status, reason, recs = gen_failure(s, ds, blocks, spec, rng)
    elif spec.patch_type == "segmentation":
        def acc(L, zc, y0, x0):
            fg = L != 0
            n_ids = int(np.unique(L[fg]).size) if fg.any() else 0
            f = float(fg.mean())
            return (f >= spec.p("min_fg_frac") and n_ids >= spec.p("min_ids")), {"n_ids": n_ids, "fg_frac": round(f, 4), "z_center": zc}
        status, reason, recs = gen_label_windows(s, ds, blocks, spec, rng, label, acc)
    elif spec.patch_type == "membrane":
        def acc(L, zc, y0, x0):
            bd = float(boundary_map(L).mean())
            return bd >= spec.p("min_boundary_frac"), {"boundary_frac": round(bd, 4), "n_ids": int(np.unique(L).size), "z_center": zc, "target": "boundary_from_ids"}
        status, reason, recs = gen_label_windows(s, ds, blocks, spec, rng, label, acc)
    elif spec.patch_type in ("synapse", "mitochondria"):
        status, reason, recs = gen_mask_components(s, ds, blocks, spec, rng, label)
    else:  # hard_negative / proofreading
        from emqc.qc.labels import open_label_for

        pred = s.get(DatasetAsset, ready["prediction_asset_id"])
        pcache = _SliceCache(open_label_for(ds, pred))
        pset.params_json["prediction_asset_id"] = pred.id
        pset.params_json["prediction_version"] = pred.version or pred.path

        def acc(L, zc, y0, x0):
            P = pcache.get(zc)[y0 : y0 + L.shape[0], x0 : x0 + L.shape[1]]
            fg_dis = float(((L != 0) != (P != 0)).mean())
            bd_dis = float((boundary_map(L) != boundary_map(P)).mean())
            d = 0.5 * fg_dis + 0.5 * bd_dis
            ok = d >= spec.p("min_disagreement") if spec.patch_type == "hard_negative" else True
            return ok, {"disagreement": round(d, 4), "fg_disagreement": round(fg_dis, 4), "boundary_disagreement": round(bd_dis, 4), "z_center": zc, "prediction": pred.version or pred.path}
        status, reason, recs = gen_label_windows(s, ds, blocks, spec, rng, label, acc)
    by_block = {b.block_id: b for b in blocks}
    checks = check_patches(recs, by_block) if recs else {"n_patches": 0, "passed": True}
    counts = {"by_partition": {}, "by_block": {}, "by_failure_type": {}, "by_grade": {}}
    for r in recs:
        counts["by_partition"][r["partition"]] = counts["by_partition"].get(r["partition"], 0) + 1
        counts["by_block"][r["block_id"]] = counts["by_block"].get(r["block_id"], 0) + 1
        counts["by_grade"][r["grade"] or "?"] = counts["by_grade"].get(r["grade"] or "?", 0) + 1
        ft = r["meta"].get("failure_type")
        if ft:
            counts["by_failure_type"][ft] = counts["by_failure_type"].get(ft, 0) + 1
    pset.status, pset.reason, pset.n_patches, pset.counts_json = status, reason, len(recs), {**counts, "duration_s": round(time.perf_counter() - t0, 2)}
    pset.checks_json = checks
    s.add(pset)
    s.flush()
    s.add_all([Patch(set_id=pset.id, dataset_id=ds.dataset_id, block_id=r["block_id"], patch_type=r["patch_type"], z0=r["z0"], z1=r["z1"], y0=r["y0"], y1=r["y1"], x0=r["x0"], x1=r["x1"],
                     partition=r["partition"], difficulty=r["difficulty"], grade=r["grade"], region=r["region"], meta_json=r["meta"]) for r in recs])
    s.commit()
    return pset
