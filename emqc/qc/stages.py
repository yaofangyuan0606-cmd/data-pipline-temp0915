"""The QC pipeline is five stages, run per block:

  1. ingest       read every slice once -> status (ok/missing/corrupt), statistics, two thumbnails
                  (preview thumbnail; serial-analysis thumbnail at ~32 nm/px)
  2. slice_qc     slice-level checks on the statistics/thumbnails (no full-res image needed any more)
  3. serial_qc    serial-section checks over the block's serial thumbnails (may add block-level findings)
  4. aggregate    per-slice / per-block scores, severities, failure types, retention, previews
  5. persist      write everything to MySQL + ETL metrics (see runner.py)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage

from emqc.registry.readers import CorruptSliceError, MissingSliceError, SourceUnavailableError, VolumeReader

from .base import BlockContext, Finding, QCCheck, QCConfig, DatasetInfo, Severity, SliceRecord
from .preview import block_mean, downsample, save_montage, save_thumb

STAGES = ("ingest", "slice_qc", "serial_qc", "aggregate", "persist")


# ----------------------------------------------------------------------------- 1. ingest


def serial_factor(ds: DatasetInfo, cfg: QCConfig, extent: tuple[int, int] | None = None) -> int:
    """Integer downsampling factor for serial-analysis thumbnails of one block (tile).

    Aim for cfg.serial_target_nm per pixel (needs the voxel size); always keep the long side <= serial_max_px
    and the short side >= serial_min_px. Without a voxel size fall back to the preview thumbnail size.
    """
    H, W = extent if extent else ds.shape[1:]
    long_, short = max(H, W), min(H, W)
    nm = ds.nm_per_px_xy
    f = int(round(cfg.serial_target_nm / nm)) if nm else int(np.ceil(long_ / cfg.thumb_px))
    f = max(f, int(np.ceil(long_ / cfg.serial_max_px)))
    f = min(f, max(1, short // cfg.serial_min_px))
    return max(1, f)


def compute_stats(img: np.ndarray, dtype_max: float, max_pixels: int, thumb_px: int, sfactor: int, fill_value: float | None = 0, fill_min_frac: float = 0.001) -> tuple[dict, np.ndarray, np.ndarray]:
    """Per-slice statistics + thumbnails.

    "No data" regions (connected areas of exactly `fill_value`, e.g. cracks / folds / missing tiles exported as 0)
    are detected first; intensity and sharpness statistics are then computed on the *valid* pixels only, so a
    cracked section is reported as a crack rather than as blur + brightness jump + saturation.
    """
    H, W = img.shape
    step = max(1, int(np.ceil(np.sqrt(H * W / max_pixels))))
    sub = img[::step, ::step]
    fill = fill_region_stats(sub, fill_value, fill_min_frac) if fill_value is not None else {}
    valid = (sub != fill_value) if fill.get("frac_fill", 0.0) > 0 else np.ones(sub.shape, dtype=bool)
    f = sub.astype(np.float32) / float(dtype_max)
    fv = f[valid] if valid.any() else f.ravel()
    p1, p50, p99 = np.percentile(fv, [1, 50, 99])
    std = float(fv.std())
    lap = ndimage.laplace(f)
    if fill.get("frac_fill", 0.0) > 0:
        inner = ndimage.binary_erosion(valid, iterations=2, border_value=1)
        lapvar = float(lap[inner].var()) if inner.any() else 0.0
    else:
        lapvar = float(lap.var())
    if img.dtype == np.uint8:
        hist = np.bincount(sub[valid].ravel(), minlength=256).astype(np.float64)
    elif img.dtype.kind in "ui":
        hist = np.bincount((sub[valid].ravel().astype(np.int64) * 255 // int(dtype_max)).clip(0, 255), minlength=256).astype(np.float64)
    else:
        hist, _ = np.histogram(fv, bins=256, range=(0.0, 1.0))
        hist = hist.astype(np.float64)
    pr = hist / max(hist.sum(), 1.0)
    nz = pr[pr > 0]
    frac_fill = float(fill.get("frac_fill", 0.0))
    stats = {
        "mean": float(fv.mean()),
        "std": std,
        "min": float(fv.min()),
        "max": float(fv.max()),
        "p1": float(p1),
        "p50": float(p50),
        "p99": float(p99),
        "frac_low_sat": float(max((sub <= 0).mean() - (frac_fill if fill_value == 0 else 0.0), 0.0)),
        "frac_high_sat": float((sub >= dtype_max).mean()),
        "frac_mode": float(pr.max()),
        "entropy_bits": float(-(nz * np.log2(nz)).sum()),
        "lapvar": lapvar,
        "sharpness": float(lapvar / (std * std + 1e-6)),
        "stat_subsample": step,
        **fill,
    }
    thumb = downsample(img, thumb_px, dtype_max)
    sthumb = block_mean(img, sfactor, dtype_max)
    return stats, thumb, sthumb


def fill_region_stats(sub: np.ndarray, fill_value: float, min_frac: float, max_px: int = 512) -> dict:
    """Connected components of exactly `fill_value` (8-connectivity) on a <=max_px version of the slice.

    Returns frac_fill (fraction of the slice covered by components >= min_frac), n_fill_components, and for
    the largest component: fill_bbox (in *sub* pixel coords, scaled by the caller), fill_elongation
    (sqrt of principal-axis variance ratio; a band across the section gives >> 1), fill_span (bbox diagonal /
    image diagonal). Empty dict when there is no fill at all.
    """
    zero = sub == fill_value
    if not zero.any():
        return {}
    H, W = zero.shape
    k = max(1, int(np.ceil(max(H, W) / max_px)))
    small = zero[::k, ::k]
    lab, n = ndimage.label(small, structure=np.ones((3, 3), dtype=int))
    if n == 0:
        return {}
    sizes = ndimage.sum(np.ones_like(small, dtype=np.float64), lab, index=np.arange(1, n + 1))
    total = float(small.size)
    keep = [i + 1 for i, sz in enumerate(sizes) if sz / total >= min_frac]
    if not keep:
        return {"frac_fill": 0.0, "n_fill_components": 0, "frac_zero_raw": float(zero.mean())}
    big = max(keep, key=lambda i: sizes[i - 1])
    ys, xs = np.nonzero(lab == big)
    cov = np.cov(np.vstack([ys, xs]).astype(np.float64)) if ys.size > 2 else np.eye(2)
    ev = np.sort(np.linalg.eigvalsh(cov))
    elong = float(np.sqrt(max(ev[1], 1e-9) / max(ev[0], 1e-9)))
    bbox = [int(xs.min() * k), int(ys.min() * k), int((xs.max() + 1) * k), int((ys.max() + 1) * k)]  # sub coords
    span = float(np.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]) / np.hypot(W, H))
    hs, ws = small.shape
    sides = int(ys.min() == 0) + int(xs.min() == 0) + int(ys.max() == hs - 1) + int(xs.max() == ws - 1)
    return {
        "fill_border_sides": sides,  # how many image edges the largest component touches (0..4)
        "frac_fill": float(sum(sizes[i - 1] for i in keep) / total),
        "n_fill_components": len(keep),
        "fill_largest_frac": float(sizes[big - 1] / total),
        "fill_bbox_sub": bbox,
        "fill_elongation": elong,
        "fill_span": span,
        "frac_zero_raw": float(zero.mean()),
    }


def stage_ingest(ctx: BlockContext, reader: VolumeReader) -> None:
    stage_ingest_group([ctx], reader)


def _ingest_reference_slice(ctxs: list[BlockContext], reader: VolumeReader) -> None:
    """Read the section just before the block's z range and attach it as a reference-only slice.

    Without it the first section of every block has nothing to compare against, so a volume split into
    blocks of 64 sections gets one blind seam per block boundary. The extra section is never scored,
    never persisted, and costs one decode per block group.
    """
    z = ctxs[0].block.z_start - 1
    if z < 0:
        return
    try:
        img = reader.read_slice(z)
    except Exception:  # a missing / corrupt neighbour simply means no cross-boundary reference
        return
    for c in ctxs:
        b = c.block
        tile = img[b.y_start : b.y_end, b.x_start : b.x_end] if c.is_tile else img
        rec = SliceRecord(z=z, reference_only=True)
        rec.stats, rec.thumb, rec.sthumb = compute_stats(tile, c.ds.dtype_max, c.config.max_stat_pixels, c.config.thumb_px, c.serial_factor, fill_value=c.ds.fill_value)
        rec.thumb = None  # 只需要序列缩略图，预览图不留
        c.pre = [rec]


def stage_ingest_group(ctxs: list[BlockContext], reader: VolumeReader) -> None:
    """Ingest several blocks that share the same z range: every section is read and decoded once and
    each block gets its own XY crop (tiled datasets), so I/O does not scale with the number of tiles."""
    if not ctxs:
        return
    t0 = time.perf_counter()
    first = ctxs[0]
    assert all((c.block.z_start, c.block.z_end) == (first.block.z_start, first.block.z_end) for c in ctxs), "group must share a z range"
    for c in ctxs:
        c.serial_factor = serial_factor(c.ds, c.config, c.extent)
    if hasattr(reader, "prefetch_range"):  # remote sources: start pulling the whole z range now
        reader.prefetch_range(max(first.block.z_start - 1, 0), first.block.z_end)
    _ingest_reference_slice(ctxs, reader)
    for i, z in enumerate(range(first.block.z_start, first.block.z_end)):
        try:
            img = reader.read_slice(z)
        except MissingSliceError as e:
            for c in ctxs:
                c.slices[i].status, c.slices[i].error = "missing", str(e)
            continue
        except CorruptSliceError as e:
            for c in ctxs:
                c.slices[i].status, c.slices[i].error = "corrupt", str(e)
            continue
        except SourceUnavailableError:
            raise
        except Exception as e:  # unexpected reader failure -> corrupt, but keep going
            for c in ctxs:
                c.slices[i].status, c.slices[i].error = "corrupt", f"{type(e).__name__}: {e}"
            continue
        for c in ctxs:
            rec = c.slices[i]
            b = c.block
            tile = img[b.y_start : b.y_end, b.x_start : b.x_end] if c.is_tile else img
            rec.bytes_read = int(tile.nbytes)
            rec.stats, rec.thumb, rec.sthumb = compute_stats(tile, c.ds.dtype_max, c.config.max_stat_pixels, c.config.thumb_px, c.serial_factor, fill_value=c.ds.fill_value)
            if "fill_bbox_sub" in rec.stats:  # scale bbox from the subsampled grid to block-local full-res pixels
                st = rec.stats["stat_subsample"]
                rec.stats["fill_bbox"] = [v * st for v in rec.stats.pop("fill_bbox_sub")]
        del img  # full-res image is dropped immediately
    dt = time.perf_counter() - t0
    for c in ctxs:
        c.stage_durations["ingest"] = dt / len(ctxs)  # shared decode cost, attributed evenly


# ----------------------------------------------------------------------------- 2./3. checks


def stage_checks(ctx: BlockContext, checks: list[QCCheck], stage_name: str) -> None:
    t0 = time.perf_counter()
    for chk in checks:
        chk.run(ctx)
    ctx.stage_durations[stage_name] = time.perf_counter() - t0


# ----------------------------------------------------------------------------- 4. aggregate


@dataclass
class BlockResult:
    scores: dict = field(default_factory=dict)  # check -> {min, mean, n_flagged, implemented}; "_block" -> block stats
    failure_types: list[str] = field(default_factory=list)
    max_severity: Severity = Severity.NONE
    block_severity: Severity = Severity.NONE  # from block-level findings only
    quality_score: float | None = None
    n_slices: int = 0
    n_ok: int = 0
    n_missing: int = 0
    n_corrupt: int = 0
    n_passed: int = 0
    n_flagged: int = 0
    retention_rate: float | None = None
    bytes_read: int = 0
    coordinate: dict = field(default_factory=dict)
    preview_path: str | None = None
    findings_by_type: dict = field(default_factory=dict)
    block_findings: list[Finding] = field(default_factory=list)
    z_profile: list = field(default_factory=list)  # [(z, quality_score, severity)]
    grade: str = "A"  # A | B | C | D, see grade_block
    longest_clean_run: int = 0  # max number of consecutive passed sections
    n_critical: int = 0
    dominant_failure: str | None = None


GRADE_RULES = {"A": "留存率 >= 95% 且没有 critical 切片：直接可用", "B": "留存率 >= 85%：剔除少数坏切片后可用", "C": "留存率 >= 60%：只有短的连续可用段，训练需按可用段采样", "D": "留存率 < 60% 或 block 级失败：不可用 / 需重新采集或修复"}


def grade_from(retention: float | None, n_critical: int, block_fail: bool) -> str:
    if block_fail or retention is None or retention < 0.6:
        return "D"
    if retention >= 0.95 and n_critical == 0:
        return "A"
    if retention >= 0.85:
        return "B"
    return "C"


def longest_run(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def _slice_quality(rec: SliceRecord) -> float | None:
    if not rec.ok:
        return 0.0
    vals = [v for v in rec.scores.values() if v is not None]
    return float(min(vals)) if vals else None


def _block_stats(ctx: BlockContext) -> dict:
    good = ctx.good()
    out = {"serial_factor": ctx.serial_factor, "serial_nm_per_px": ctx.serial_nm_per_px, "n_ok": len(good)}
    for k in ("mean", "std", "sharpness", "entropy_bits"):
        vals = [r.stats[k] for r in good if k in r.stats]
        out[f"median_{k}"] = float(np.median(vals)) if vals else None
    zs = np.array([r.z for r in good if r.stats.get("std", 0) > 0], dtype=float)
    if zs.size >= 4:
        stds = np.log2([r.stats["std"] for r in good if r.stats.get("std", 0) > 0])
        means = np.array([r.stats["mean"] for r in good if r.stats.get("std", 0) > 0])
        out["std_trend_log2_per_100z"] = float(np.polyfit(zs, stds, 1)[0] * 100)
        out["mean_trend_per_100z"] = float(np.polyfit(zs, means, 1)[0] * 100)
    pw = ctx.cache.get("pairwise")
    if pw:
        out["median_ncc_gap1"] = pw["median_ncc"] if np.isfinite(pw["median_ncc"]) else None
        out["median_ncc_gap2"] = pw["median_ncc_gap2"] if np.isfinite(pw["median_ncc_gap2"]) else None
        out["n_reference_chain"] = len(pw["chain"])
    return out


def stage_aggregate(ctx: BlockContext, checks: list[QCCheck], preview_root: Path | None) -> BlockResult:
    t0 = time.perf_counter()
    cfg = ctx.config
    res = BlockResult(n_slices=len(ctx.slices))
    res.coordinate = {
        "z_start": ctx.block.z_start,
        "z_end": ctx.block.z_end,
        "z_abs_start": ctx.block.z_start + ctx.ds.z_offset,
        "bbox": [ctx.block.x_start, ctx.block.y_start, ctx.block.x_end, ctx.block.y_end],
    }
    res.block_findings = list(ctx.block_findings)
    res.block_severity = max((f.severity for f in res.block_findings), default=Severity.NONE)
    block_fail = res.block_severity >= cfg.pass_max_severity
    prev_dir = (preview_root / ctx.ds.dataset_id / ctx.block.block_id) if preview_root else None
    for rec in ctx.slices:
        rec.quality_score = _slice_quality(rec)
        rec.max_severity = max((f.severity for f in rec.findings), default=Severity.NONE)
        rec.failure_types = sorted({f.failure_type for f in rec.findings})
        rec.passed = rec.ok and rec.max_severity < cfg.pass_max_severity and not block_fail
        if block_fail and rec.ok:
            rec.note("_block", "failed by block-level finding")
        res.bytes_read += rec.bytes_read
        if rec.status == "missing":
            res.n_missing += 1
        elif rec.status == "corrupt":
            res.n_corrupt += 1
        else:
            res.n_ok += 1
        if rec.passed:
            res.n_passed += 1
        if rec.findings:
            res.n_flagged += 1
        for f in rec.findings:
            res.findings_by_type[f.failure_type] = res.findings_by_type.get(f.failure_type, 0) + 1
        if prev_dir is not None and rec.thumb is not None and rec.max_severity >= cfg.preview_min_severity:
            rec.preview_path = str(save_thumb(rec.thumb, prev_dir / f"z{rec.z:05d}.png"))
            for f in rec.findings:
                f.preview_path = rec.preview_path
        res.z_profile.append((rec.z, rec.quality_score, rec.max_severity.label))
    for f in res.block_findings:
        res.findings_by_type[f.failure_type] = res.findings_by_type.get(f.failure_type, 0) + 1
    # per-check aggregates
    for chk in checks:
        vals = [r.scores.get(chk.name) for r in ctx.slices if r.scores.get(chk.name) is not None]
        n_flag = sum(1 for r in ctx.slices for f in r.findings if f.check == chk.name) + sum(1 for f in res.block_findings if f.check == chk.name)
        res.scores[chk.name] = {
            "min": float(min(vals)) if vals else None,
            "mean": float(np.mean(vals)) if vals else None,
            "n_flagged": n_flag,
            "n_evaluated": len(vals),
            "implemented": chk.implemented,
        }
    res.scores["_block"] = _block_stats(ctx)
    qs = [r.quality_score for r in ctx.slices if r.quality_score is not None]
    res.quality_score = float(np.mean(qs)) if qs else None
    if block_fail and res.quality_score is not None:
        res.quality_score = min(res.quality_score, float(res.block_findings[0].score or 0.0))
    res.max_severity = max([r.max_severity for r in ctx.slices] + [res.block_severity], default=Severity.NONE)
    res.failure_types = sorted(res.findings_by_type)
    res.retention_rate = res.n_passed / res.n_slices if res.n_slices else None
    res.n_critical = sum(1 for r in ctx.slices if r.max_severity >= Severity.CRITICAL or not r.ok)
    res.longest_clean_run = longest_run([r.passed for r in ctx.slices])
    res.grade = grade_from(res.retention_rate, res.n_critical, block_fail)
    serious = {}
    for r in ctx.slices:
        for f in r.findings:
            if f.severity >= Severity.MEDIUM:
                serious[f.failure_type] = serious.get(f.failure_type, 0) + 1
    for f in res.block_findings:
        serious[f.failure_type] = serious.get(f.failure_type, 0) + 1
    pool = serious or res.findings_by_type
    res.dominant_failure = max(pool, key=pool.get) if pool else None
    res.scores["_block"].update({"grade": res.grade, "longest_clean_run": res.longest_clean_run, "n_critical": res.n_critical, "dominant_failure": res.dominant_failure, "extent": list(ctx.extent), "is_tile": ctx.is_tile})
    if prev_dir is not None:
        p = save_montage(ctx.slices, prev_dir / "montage.png")
        res.preview_path = str(p) if p else None
        for f in res.block_findings:
            f.preview_path = res.preview_path
    ctx.stage_durations["aggregate"] = time.perf_counter() - t0
    return res
