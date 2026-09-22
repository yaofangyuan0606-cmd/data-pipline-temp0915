"""EM membrane detection and optional boundary refinement for SAM masks.

Membrane strength measures local darkness in units of the neighbourhood's standard
deviation, so globally dark and washed-out tiles use the same scale. SAM refinement
keeps prompt-selected interiors, fills holes and assigns shared membrane pixels to
the nearest interior. This module does not create or apply annotation previews.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage

P = dict(
    sig=1.0,  # pre-smoothing of the EM (px): kills shot noise, keeps membranes
    win=63,   # box for the local mean / standard deviation (px)
    eps=4.0,  # floor on the local sd (grey levels), so flat areas do not blow up
)


def membraneness(em: np.ndarray, p: dict | None = None) -> np.ndarray:
    """How much darker each pixel is than its own neighbourhood, in local standard deviations.

    Positive on membranes and on dark organelles, near zero in cytoplasm, and unaffected by a tile being globally
    dark or washed out — which is what makes it work on a montage."""
    p = dict(P, **(p or {}))
    f = np.asarray(em, np.float32)
    if p["sig"]:
        f = ndimage.gaussian_filter(f, p["sig"])
    mu = ndimage.uniform_filter(f, p["win"])
    var = ndimage.uniform_filter(f * f, p["win"]) - mu * mu
    sd = np.sqrt(np.maximum(var, 0.0))
    return (mu - f) / (sd + p["eps"])


def membrane_map(em: np.ndarray, sensitivity: float = 0.5, p: dict | None = None) -> np.ndarray:
    """Boolean membrane / boundary map, for the callers that want a hard mask (SAM snapping, tests).

    `sensitivity` 0..1 shifts the quantile that separates membrane from interior: higher marks more pixels as
    boundary during SAM mask refinement."""
    p = dict(P, **(p or {}))
    s = float(np.clip(sensitivity, 0.0, 1.0))
    m = membraneness(em, p)
    if not np.isfinite(m).any() or float(np.ptp(m)) < 1e-6:
        return np.zeros(m.shape, dtype=bool)   # flat section (blank page / placeholder): no boundaries anywhere
    q = np.clip(0.85 - 0.45 * s, 0.30, 0.95)   # s=0 -> darkest 15%, s=1 -> darkest 60%
    b = m > np.quantile(m, q)
    b = np.pad(b, 1, mode="edge")              # pad so the closing does not eat the image border
    b = ndimage.binary_closing(b, structure=np.ones((3, 3), bool))   # seal 1-px gaps in a membrane
    return b[1:-1, 1:-1]


def assign_membrane(region: np.ndarray, boundary: np.ndarray, allowed: np.ndarray, reach: int = 4) -> np.ndarray:
    """Give boundary pixels to `region` when they are closer to it than to any other non-boundary pixel — the
    membrane two cells share is split down the middle — and never further than `reach` px from the region."""
    other = ~boundary & ~region
    d_reg = ndimage.distance_transform_edt(~region)
    d_oth = ndimage.distance_transform_edt(~other) if other.any() else np.full(region.shape, np.inf)
    return region | (boundary & allowed & (d_reg <= reach) & (d_reg <= d_oth))


def refine_mask(mask: np.ndarray, em: np.ndarray, points=(), labels=(), sensitivity: float = 0.5, reach: int = 4,
                margin: int = 8) -> np.ndarray:
    """Snap a SAM mask to the membranes. Non-membrane pixels within `margin` px of the mask form the growth zone; of
    its connected components keep those holding a positive prompt (else the one overlapping the mask most), so a
    part of the mask that leaked through a membrane into the neighbour is dropped and a mask that stopped short of
    the membrane is extended to it. Holes are filled and the shared membrane is split with the neighbour."""
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return mask
    b = membrane_map(em, sensitivity)
    zone = ndimage.binary_dilation(mask, iterations=max(0, int(margin))) & ~b
    lab, n = ndimage.label(zone)
    if n == 0:
        return mask
    H, W = lab.shape
    keep = {int(lab[y, x]) for (x, y), l in zip(points, labels) if l == 1 and 0 <= y < H and 0 <= x < W and lab[y, x]}
    if not keep:
        overlap = np.bincount(lab[mask & (lab > 0)].ravel(), minlength=n + 1)
        overlap[0] = 0
        keep = {int(overlap.argmax())} if overlap.any() else set()
    if not keep:
        return mask
    region = np.isin(lab, list(keep))
    filled = ndimage.binary_fill_holes(region)
    region |= filled & (b | mask)
    return assign_membrane(region, b, np.ones_like(b), reach)
