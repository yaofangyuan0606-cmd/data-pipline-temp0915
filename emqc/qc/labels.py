"""Label QC: validate a label asset (GT segmentation ids, class masks, predictions) against the EM volume.

Per section (label_qc rows) and per asset (dataset_assets.extra_json["validation"]):
    label_missing          EM section exists but the label file does not
    label_empty            label present but all background where the EM section has data
    label_without_image    label has content where the EM section is missing / corrupt / blank
    label_in_fill          label painted over the EM fill region (no image data underneath)
    shape_mismatch         label plane is not the EM plane at an integer scale
    z_count_mismatch       number of label sections differs from the EM
    encoding_inconsistent  sections decode to different dtypes
Exclusive class masks (several assets that must not overlap) get `label_conflict`: fraction of pixels claimed by > 1 mask.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import delete
from sqlalchemy.orm import Session

from emqc.db.models import Dataset, DatasetAsset, LabelQC
from emqc.registry.labels import open_label_volume, to_em_resolution
from emqc.registry.readers import MissingSliceError, SliceReadError, VolumeReader


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _scale(em_shape, lab_shape) -> tuple[int, int] | None:
    """Integer (sy, sx) such that em = label * s; None when the label plane is not an integer downscale/equal."""
    ey, ex = em_shape
    ly, lx = lab_shape
    if ly <= 0 or lx <= 0 or ey % ly or ex % lx:
        return None
    sy, sx = ey // ly, ex // lx
    return (sy, sx) if sy == sx else None


def open_label_for(ds: Dataset, asset: DatasetAsset) -> VolumeReader:
    from emqc.registry.scanner import open_root

    extra = asset.extra_json or {}
    if (asset.format or "").startswith("precomputed_cloud") or extra.get("cloud_url"):
        from emqc.config import settings as _s
        from emqc.registry.cloud import CloudVolumeReader

        return CloudVolumeReader(extra.get("cloud_url") or asset.path, extra.get("roi") or (ds.metadata_json or {}).get("roi"),
                                 mip=int(extra.get("mip", 0)), cache_dir=_s.cache_dir, align=False, label=True)
    fs, base = open_root(ds.root_path)
    r = open_label_volume(fs, base, asset.path, asset.format, extra, z_offset=extra.get("z_offset"))
    return to_em_resolution(r, (ds.size_y, ds.size_x))  # masks exported at a finer mip are strided down to the EM grid


def validate_label_asset(s: Session, ds: Dataset, asset: DatasetAsset, em: VolumeReader | None = None, step: int = 1, max_sections: int | None = None,
                         in_fill_max: float = 0.02, empty_bg_min: float = 0.999, blank_std: float = 1e-3) -> dict:
    """Run the per-section checks, persist label_qc rows and the asset-level summary. Returns the summary."""
    from emqc.qc.runner import open_dataset_volume

    t0 = time.perf_counter()
    em = em or open_dataset_volume(ds)
    fill_value = (ds.metadata_json or {}).get("fill_value", 0)
    summary: dict = {"checked_at": _now_iso(), "status": "ok", "issues": {}, "notes": []}
    try:
        lab = open_label_for(ds, asset)
    except Exception as e:
        summary.update(status="fail", error=f"{type(e).__name__}: {e}")
        asset.extra_json = {**(asset.extra_json or {}), "validation": summary}
        s.commit()
        return summary
    ez, ey, ex = em.shape
    lz, ly, lx = lab.shape
    scale = _scale((ey, ex), (ly, lx))
    native = lab.info.extra.get("native_shape")
    if native:
        summary["notes"].append(f"label stored at {native[1]}x{native[2]} ({lab.info.extra['downsampled_by']}x the EM plane); compared and served at EM resolution by striding (nearest neighbour)")
    summary.update(label_shape=[lz, ly, lx], native_shape=native, downsampled_by=lab.info.extra.get("downsampled_by", 1), em_shape=[ez, ey, ex], dtype=lab.info.dtype, scale_to_em=list(scale) if scale else None, encoding=(asset.extra_json or {}).get("label_encoding") or ("rgb24" if "rgb" in (asset.format or "") else "auto"))
    issues: dict[str, int] = {}

    def bump(k):
        issues[k] = issues.get(k, 0) + 1

    if lz != ez:
        bump("z_count_mismatch")
        summary["notes"].append(f"label has {lz} sections, EM has {ez}")
    if scale is None:
        bump("shape_mismatch")
        summary["notes"].append(f"label plane {ly}x{lx} is not an integer downscale of EM {ey}x{ex}")
    zs = list(range(0, min(ez, lz), max(1, step)))
    if max_sections:
        zs = zs[:max_sections]
    if scale is None or lz != ez:
        # not alignable to the EM: nothing downstream can use it, so do not spend a read per section
        summary["notes"].append("per-section checks skipped: the asset cannot be aligned to the EM (the z / scale mapping must come from the data owner)")
        zs = []
    if hasattr(lab, "prefetch_range") and zs:  # remote sources: one bulk transfer instead of a fetch per section
        lab.prefetch_range(zs[0], zs[-1] + 1)
    rows, dtypes = [], set()
    n_ids_total = 0
    for z in zs:
        em_status, em_img = "ok", None
        try:
            em_img = em.read_slice(z)
            if float(em_img.std()) < blank_std:
                em_status = "blank"
        except SliceReadError as e:
            em_status = "missing" if "Missing" in type(e).__name__ else "corrupt"
        row = LabelQC(dataset_id=ds.dataset_id, asset_id=asset.id, z=z, em_status=em_status, issues=[])
        if hasattr(lab, "path_for") and not isinstance(lab, __import__("emqc.registry.cloud", fromlist=["CloudVolumeReader"]).CloudVolumeReader) and lab.path_for(z) is None:
            row.present = False
            if em_status == "ok":
                row.issues.append("label_missing")
                bump("label_missing")
            rows.append(row)
            continue
        try:
            L = lab.read_slice(z)  # one read per section (slice_status would read it a second time)
        except MissingSliceError:
            row.present = False
            if em_status == "ok":
                row.issues.append("label_missing")
                bump("label_missing")
            rows.append(row)
            continue
        except SliceReadError:
            row.present, row.shape_ok = False, False
            row.issues.append("label_corrupt")
            bump("label_corrupt")
            rows.append(row)
            continue
        dtypes.add(str(L.dtype))
        fg = L != 0
        row.frac_background = float(1.0 - fg.mean())
        row.n_ids = int(np.unique(L[::4, ::4]).size)
        n_ids_total = max(n_ids_total, row.n_ids)
        row.shape_ok = scale is not None
        if em_status == "ok" and em_img is not None and scale is not None:
            E = em_img[:: scale[0], :: scale[1]] if scale != (1, 1) else em_img
            if E.shape == L.shape:
                infill = E == fill_value
                n_fg = int(fg.sum())
                row.frac_label_in_fill = float((fg & infill).sum() / n_fg) if n_fg else 0.0
                if infill.mean() > 0.001 and row.frac_label_in_fill > in_fill_max:
                    row.issues.append("label_in_fill")
                    bump("label_in_fill")
            if row.frac_background >= empty_bg_min:
                row.issues.append("label_empty")
                bump("label_empty")
        elif em_status != "ok" and fg.mean() > 0.01:
            row.issues.append("label_without_image")
            bump("label_without_image")
        rows.append(row)
    if len(dtypes) > 1:
        bump("encoding_inconsistent")
        summary["notes"].append(f"sections decode to several dtypes: {sorted(dtypes)}")
    for r in rows:
        r.passed = not r.issues
    s.execute(delete(LabelQC).where(LabelQC.asset_id == asset.id))
    s.add_all(rows)
    n_checked = len(rows)
    n_bad = sum(1 for r in rows if not r.passed)
    summary.update(issues=issues, n_sections_checked=n_checked, n_sections_flagged=n_bad, step=step, max_ids_per_section=n_ids_total, duration_s=round(time.perf_counter() - t0, 2))
    if scale is None or lz != ez or issues.get("label_corrupt"):
        summary["status"] = "fail"
    elif n_bad:
        summary["status"] = "warn"
    summary["usable_for_patches"] = summary["status"] != "fail" and scale == (1, 1)
    if scale and scale != (1, 1):
        summary["notes"].append(f"label is at 1/{scale[0]} of EM resolution: fine for QC, not for EM-resolution patches")
    asset.extra_json = {**(asset.extra_json or {}), "validation": summary}
    s.commit()
    return summary


def validate_exclusive_masks(s: Session, ds: Dataset, assets: list[DatasetAsset], step: int = 1, max_sections: int | None = None) -> dict:
    """Class masks that must not overlap (e.g. axon / dendrite / cell body): fraction of pixels claimed by more than one.
    Only evaluable when every mask has the same plane and section count; otherwise records why not."""
    from emqc.qc.runner import open_dataset_volume

    em = open_dataset_volume(ds)
    readers = {}
    for a in assets:
        try:
            readers[a.id] = open_label_for(ds, a)
        except Exception as e:
            return {"status": "not_evaluated", "reason": f"{a.path}: {type(e).__name__}: {e}"}
    shapes = {tuple(r.shape) for r in readers.values()}
    if len(shapes) != 1:
        out = {"status": "not_evaluated", "reason": f"masks have different shapes: {sorted(shapes)}"}
    else:
        (lz, ly, lx), = shapes
        ez, ey, ex = em.shape
        if lz != ez or _scale((ey, ex), (ly, lx)) is None:
            out = {"status": "not_evaluated", "reason": f"masks {lz}x{ly}x{lx} are not z-aligned / integer-scaled to the EM {ez}x{ey}x{ex}; the mapping must come from the data owner"}
        else:
            zs = list(range(0, lz, max(1, step)))
            if max_sections:
                zs = zs[:max_sections]
            worst, total = 0.0, 0.0
            for z in zs:
                count = None
                for r in readers.values():
                    m = (r.read_slice(z) != 0).astype(np.uint8)
                    count = m if count is None else count + m
                frac = float((count > 1).mean())
                total += frac
                worst = max(worst, frac)
            out = {"status": "ok" if worst < 0.001 else "warn", "n_sections": len(zs), "conflict_frac_mean": total / max(len(zs), 1), "conflict_frac_max": worst}
    out["checked_at"] = _now_iso()
    out["assets"] = [a.path for a in assets]
    for a in assets:
        a.extra_json = {**(a.extra_json or {}), "label_conflict": out}
    s.commit()
    return out
