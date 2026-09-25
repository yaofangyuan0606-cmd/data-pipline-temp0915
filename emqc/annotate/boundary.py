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


def pull_back_to_membrane(mask: np.ndarray, em: np.ndarray, x: int, y: int, sensitivity: float = 0.5,
                          depth: int = 10, bright_share: float = 0.5) -> np.ndarray:
    """修缮边缘：一块标签（`mask`）跨过黑膜溢到邻居身上时，把溢出去的那一层收回来。只动最外层，细胞里面的一律不管。

    1. 「膜」只认明显比周围暗的线（比局部暗 `thr` 个标准差以上，1 像素的断口先补上）。膜把标签分成若干片。
    2. 细胞本体 = 点击处那一片，加上所有离标签外沿超过 `depth` 像素的片——也就是说，只有贴着外沿、
       不到 `depth` 厚的薄片才可能被收；细胞里面的东西（线粒体等暗色细胞器、被它们隔开的胞质）永远是本体。
    3. 膜上的像素归离它最近的那一片。每个贴边的薄片（连同归它的膜），看它和标签外面接触的地方：
       大多是亮的胞质（超过 `bright_share`）→ 标签是从邻居的胞质里切出来的，跨过膜溢出去了，收掉；
       大多是暗的膜 → 它和外面之间隔着细胞膜，是细胞自己的，留下。
    4. 被留下部分完全围住的洞补回来。

    `sensitivity` 0–1：越高，越浅的暗线也算膜，收得越积极。只收缩、从不扩张；返回留下的部分（`mask` 的子集）。"""
    mask = np.asarray(mask, bool)
    if not mask.any():
        return np.zeros_like(mask)
    score = membraneness(em)
    thr = 2.0 - 1.2 * float(np.clip(sensitivity, 0.0, 1.0))      # 0 → 2.0σ（只认很黑的膜），0.5 → 1.4σ，1 → 0.8σ
    barrier = ndimage.binary_closing(score > thr, structure=np.ones((3, 3), bool)) & mask
    pieces, n = ndimage.label(mask & ~barrier)
    if n == 0:
        return mask.copy()                                  # 整块都在膜上：没有"里面"可言，不动
    inside = ndimage.distance_transform_edt(mask)           # 到标签外沿的距离
    deep = np.zeros(n + 1, bool)
    deep[np.unique(pieces[inside > depth])] = True
    deep[0] = False
    if 0 <= y < pieces.shape[0] and 0 <= x < pieces.shape[1] and pieces[y, x]:
        deep[pieces[y, x]] = True                           # 点击的那一片总是本体
    if not deep[1:].any():                                  # 细得没有"深处"的标签：取最大的那一片当本体
        sizes = np.bincount(pieces.ravel()); sizes[0] = 0
        deep[int(sizes.argmax())] = True
    _, (iy, ix) = ndimage.distance_transform_edt(pieces == 0, return_indices=True)
    owner = np.where(mask, pieces[iy, ix], 0)               # 膜上的像素归离它最近的那一片
    region = mask & deep[owner]
    outside = ~mask
    dark = score > thr
    for k, sl in enumerate(ndimage.find_objects(owner), start=1):
        if sl is None or deep[k]:
            continue
        sl = tuple(slice(max(0, s.start - 1), s.stop + 1) for s in sl)
        piece = owner[sl] == k
        ring = ndimage.binary_dilation(piece, structure=np.ones((3, 3), bool)) & ~piece
        touch = ring & outside[sl]
        n_out = int(touch.sum())
        if n_out == 0 or (touch & ~dark[sl]).sum() / n_out < bright_share:
            region[sl] |= piece                             # 隔着细胞膜或被包在里面：细胞自己的，留下
    return ndimage.binary_fill_holes(region) & mask
