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


def _disk(r: int) -> np.ndarray:
    yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
    return yy * yy + xx * xx <= r * r


def pull_back_to_membrane(mask: np.ndarray, em: np.ndarray, x: int, y: int, sensitivity: float = 0.5) -> np.ndarray:
    """修缮边缘：一块标签（`mask`）跨过黑色细胞膜、溢到膜外时，把膜外的部分收回来，停在膜的外侧——膜本身算细胞的，留下。
    细胞里面的一律不管。

    三步：先把越过很黑的膜、溢进亮胞质的薄片切掉（`_cut_bright_leaks`）；再把压在外沿黑膜上的一层剥开，好把膜外断下来的
    碎块分出来去掉（`_peel_dark_rim`）；最后把剥掉的膜从本体往外沿暗像素并回来，最多 `MEMBRANE_PX` 像素厚
    （`_restore_membrane`）。所以最后收掉的只有膜外的部分。离外沿 10 像素左右以外的像素永远不动；细胞里的暗色细胞器
    （线粒体、囊泡）不管贴不贴边都留下。`sensitivity` 0–1：越高，越浅的暗线也算膜。只收缩、从不扩张；返回留下的部分。"""
    mask = np.asarray(mask, bool)
    if not mask.any():
        return np.zeros_like(mask)
    score = membraneness(em)
    kept = _cut_bright_leaks(mask, score, x, y, sensitivity)
    kept = _peel_dark_rim(kept, score, x, y, sensitivity)
    return _restore_membrane(mask, kept, score, sensitivity)


MEMBRANE_PX = 6     # 并回来的膜最多这么厚：H01 上细胞膜大约 3～6 像素


def _restore_membrane(mask: np.ndarray, kept: np.ndarray, score: np.ndarray, sensitivity: float = 0.5,
                      reach: int = MEMBRANE_PX, loosen: float = 0.4, margin: int = 12) -> np.ndarray:
    """第三步：标签停在膜的外侧。前两步收掉的像素里，从留下的本体出发、沿着暗像素（比剥膜时的门槛再松 `loosen` 个标准差）
    走 `reach` 步以内够得到的，是细胞自己的膜，并回来；再往外的亮胞质不回来。"""
    removed = mask & ~kept
    if not removed.any() or not kept.any():
        return kept
    H, W = mask.shape
    ys, xs = np.nonzero(removed)
    y0, y1 = max(0, ys.min() - reach - margin), min(H, ys.max() + reach + margin + 1)
    x0, x1 = max(0, xs.min() - reach - margin), min(W, xs.max() + reach + margin + 1)
    dark = score[y0:y1, x0:x1] > 1.4 - 0.8 * float(np.clip(sensitivity, 0.0, 1.0)) - loosen
    walk = removed[y0:y1, x0:x1] & dark
    grown = kept[y0:y1, x0:x1].copy()
    n8 = np.ones((3, 3), bool)
    for _ in range(reach):
        step = ndimage.binary_dilation(grown, structure=n8) & walk & ~grown
        if not step.any():
            break
        grown |= step
    out = kept.copy()
    out[y0:y1, x0:x1] = grown
    return ndimage.binary_fill_holes(out) & mask


def _cut_bright_leaks(mask: np.ndarray, score: np.ndarray, x: int, y: int, sensitivity: float = 0.5,
                      depth: int = 10, bright_share: float = 0.5) -> np.ndarray:
    """第一步：越过一道很黑的膜、溢进亮胞质的薄片切掉。

    只认明显比周围暗的膜（`thr` 个标准差以上，1 像素断口先补上），它把标签分成若干片；离外沿超过 `depth` 的片和点击处
    那片是本体。其余贴边的薄片（连同归它的膜）看它和标签外面接触的地方：大多是亮的胞质（超过 `bright_share`）→
    从邻居胞质里切出来的，收掉；大多是暗的 → 隔着细胞膜，是细胞自己的，留下。"""
    mask = np.asarray(mask, bool)
    if not mask.any():
        return np.zeros_like(mask)
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


def _peel_dark_rim(mask: np.ndarray, score_full: np.ndarray, x: int, y: int, sensitivity: float = 0.5,
                   rim: int = 8, depth: int = 10, slack: int = 2, organelle_r: int = 3, min_px: int = 6,
                   margin: int = 12) -> np.ndarray:
    """第二步：压在外沿黑膜上的那一层剥开，剥开后断下来的、漏进邻居的碎片去掉（剥掉的膜在第三步并回来）。

    1. 剥膜。「暗」= 比局部暗 `thr` 个标准差以上。从标签外面的黑膜出发，把标签最外 `rim` 像素里、顺着暗像素直接够得到的
       部分剥掉——只剥"表层"：沿暗像素走过去的步数不能比直线进来的深度多 `slack` 步以上，顺着贴边细胞器钻进去的不剥。
    2. 细胞器不动。粗得不像膜（放得下半径 `organelle_r` 的圆）、而且大半落在标签里面的暗块是细胞器，剥膜绕开它们。
    3. 剥开后与本体断开的碎片，只有「朝外接触的大多是亮的胞质」而且比本体小得多，才一起去掉；本体 = 离外沿超过
       `depth` 的部分加上点击处那一块。被一条横穿的暗纹隔开的另一半细胞之类，留下。
    4. 被留下部分围住的洞补回来。不到 `min_px` 像素的零星剥落不算。"""
    mask = np.asarray(mask, bool)
    if not mask.any():
        return np.zeros_like(mask)
    H, W = mask.shape
    ys, xs = np.nonzero(mask)
    y0, y1 = max(0, ys.min() - margin), min(H, ys.max() + margin + 1)
    x0, x1 = max(0, xs.min() - margin), min(W, xs.max() + margin + 1)
    score = score_full[y0:y1, x0:x1]
    m = mask[y0:y1, x0:x1]
    n8 = np.ones((3, 3), bool)
    dark = score > 1.4 - 0.8 * float(np.clip(sensitivity, 0.0, 1.0))    # 0 → 1.4σ，0.5 → 1.0σ，1 → 0.6σ

    blobs = ndimage.binary_dilation(ndimage.binary_opening(dark, structure=_disk(organelle_r)), structure=n8, iterations=2) & dark
    bl, nb = ndimage.label(blobs, structure=n8)
    protect = np.zeros_like(m)
    if nb:
        total = np.bincount(bl.ravel(), minlength=nb + 1)
        ours = np.bincount(bl[m], minlength=nb + 1) * 2 >= total          # 大半在标签里：细胞自己的细胞器
        ours[0] = False
        protect = ours[bl] & m

    inside = ndimage.distance_transform_edt(np.pad(m, 1))[1:-1, 1:-1]    # 到标签外沿的距离（图像边不算外沿）
    walkable = m & dark & (inside <= rim) & ~protect
    front = ~m & dark
    reached = front.copy()
    peel = np.zeros_like(m)
    for step in range(1, rim + slack + 1):
        nxt = ndimage.binary_dilation(front, structure=n8) & walkable & ~reached
        if not nxt.any():
            break
        ok = nxt & (step <= inside + slack)
        peel |= ok
        reached |= nxt
        front = ok
    specks, ns = ndimage.label(peel, structure=n8)
    if ns:
        peel &= (np.bincount(specks.ravel(), minlength=ns + 1) >= min_px)[specks]   # 零星几个像素不值得动，免得边缘变毛

    lab, n = ndimage.label(m & ~peel, structure=n8)
    body = np.zeros(n + 1, bool)
    body[np.unique(lab[inside > depth])] = True
    cy, cx = int(y) - y0, int(x) - x0
    if 0 <= cy < m.shape[0] and 0 <= cx < m.shape[1] and lab[cy, cx]:
        body[lab[cy, cx]] = True
    body[0] = False
    if n and not body.any():                                            # 细得没有"深处"：最大的那块当本体
        sizes = np.bincount(lab.ravel()); sizes[0] = 0
        body[int(sizes.argmax())] = True
    keep = body[lab]
    body_px = max(int(keep.sum()), 1)
    bright_out = ~m & (score < 0.5)
    for k, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None or body[k]:
            continue
        sl = tuple(slice(max(0, a.start - 1), a.stop + 1) for a in sl)
        piece = lab[sl] == k
        touch = ndimage.binary_dilation(piece, structure=n8) & ~m[sl]
        n_out = int(touch.sum())
        leaked = n_out and piece.sum() * 4 <= body_px and (touch & bright_out[sl]).sum() * 2 >= n_out
        if not leaked:
            keep[sl] |= piece
    keep = ndimage.binary_fill_holes(keep) & m
    out = mask.copy()
    out[y0:y1, x0:x1] = keep
    return out
