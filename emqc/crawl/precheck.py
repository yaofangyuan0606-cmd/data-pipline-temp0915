"""Judge an ROI before paying to crawl it, using a low-resolution tissue-type mask.

H01 ships a `masking` layer: a 64 nm uint64 **tissue-type** segmentation, not a defect mask. Its official
labels (from the volume's own segment_properties) are the ones below. Only `fissure` is an imaging defect;
`neuropil` is the tissue we actually want, because that is where the synapses are.

The per-axis divisor matters: masking is [64, 64, 66] nm while a mip1 ROI is [8, 8, 33] nm, so x and y
divide by 8 but z only by 2. Using one divisor for all three axes reads a completely different part of the
volume and does not raise — that was a real bug in the crawler this is ported from (coverage 25% -> 100%
after the fix, and 2 of 3 test ROIs had their verdict flip).
"""
from __future__ import annotations

import time

import numpy as np

from .. registry.cloud import open_cv

H01_MASK_URL = "gs://h01-release/data/20210601/masking"
H01_MASK_LABELS = {1: "neuropil", 3: "nucleus", 4: "blood vessel", 5: "myelin", 7: "fissure"}
DEFECT_LABELS = ("fissure",)
WANTED_LABELS = ("neuropil",)


def tissue_profile(roi: dict, mask_url: str = H01_MASK_URL, roi_resolution_nm=(8.0, 8.0, 33.0), labels: dict | None = None,
                   min_wanted: float = 0.5, max_defect: float = 0.02, mip: int = 0) -> dict:
    """Fraction of each tissue type inside `roi`, plus a verdict. `roi` is in `roi_resolution_nm` voxel units."""
    labels = labels or H01_MASK_LABELS
    t0 = time.perf_counter()
    cv = open_cv(mask_url, mip=mip)
    res = [float(v) for v in cv.resolution]
    div = []
    for r, u in zip(res, roi_resolution_nm):
        d = r / u
        if d < 1:
            raise ValueError(f"mask resolution {res} is finer than the ROI unit {roi_resolution_nm}")
        div.append(int(round(d)))
    (x0, x1), (y0, y1), (z0, z1) = roi["x"], roi["y"], roi["z"]
    mx0, mx1 = x0 // div[0], -(-x1 // div[0])
    my0, my1 = y0 // div[1], -(-y1 // div[1])
    mz0, mz1 = z0 // div[2], -(-z1 // div[2])
    arr = np.asarray(cv[mx0:max(mx1, mx0 + 1), my0:max(my1, my0 + 1), mz0:max(mz1, mz0 + 1)])
    if arr.ndim == 4:
        arr = arr[..., 0]
    vals, counts = np.unique(arr, return_counts=True)
    total = int(arr.size)
    comp = {labels.get(int(v), f"label_{int(v)}"): round(float(c / total), 4) for v, c in zip(vals, counts)}
    wanted = sum(comp.get(k, 0.0) for k in WANTED_LABELS)
    defect = sum(comp.get(k, 0.0) for k in DEFECT_LABELS)
    reasons = []
    if wanted < min_wanted:
        reasons.append(f"neuropil {wanted:.0%} < {min_wanted:.0%}：可学的神经元结构不足")
    if defect > max_defect:
        reasons.append(f"fissure {defect:.1%} > {max_defect:.0%}：有成像缺陷（裂隙 / 撕裂）")
    return {
        "roi": roi, "mask_url": mask_url, "mask_resolution_nm": res, "divisors_xyz": div,
        "mask_shape_xyz": [int(mx1 - mx0), int(my1 - my0), int(mz1 - mz0)], "n_mask_voxels": total,
        "composition": dict(sorted(comp.items(), key=lambda kv: -kv[1])),
        "wanted_frac": round(wanted, 4), "defect_frac": round(defect, 4),
        "thresholds": {"min_wanted": min_wanted, "max_defect": max_defect},
        "verdict": "ok" if not reasons else "reject", "reasons": reasons,
        "seconds": round(time.perf_counter() - t0, 2),
        "note": "masking 是组织类型图，不是缺陷掩码；只有 fissure 是成像缺陷",
    }
