"""EM-boundary-aware region tools for the annotation page ("智能填充" and the SAM mask snapping).

The annotator clicks inside a cell and the tool must fill exactly that cell, stopping at the membrane. Doing that
from the labels alone fails whenever the labels are the thing being fixed, so everything here reads the electron
microscopy image and nothing else. numpy + scipy only, a few hundred milliseconds per click, no model, no GPU.

Two ideas carry the whole module:

* **膜强度是局部的.** `membraneness` measures how much darker a pixel is than its own neighbourhood, in units of
  the local standard deviation. This dataset is a montage: a dark tile, a washed-out tile and a well-exposed one
  must all behave the same, and a single global threshold (what this module used to do) cannot achieve that. It
  scored a mean IoU of 0.49 against the delivered labels; the local formulation below scores 0.72.

* **区域是解出来的，不是漫出来的.** `bounded_region` marks the interiors of the clicked cell and of its neighbours,
  then solves a random-walk (Dirichlet) problem: for every pixel, the probability that a walker starting there
  reaches the clicked cell before any other one, with the conductance between neighbouring pixels falling off
  exponentially with membrane strength. Being a global solve it is not defeated by a one-pixel hole in a membrane
  the way a flood fill is, and it naturally puts the border down the middle of a membrane two cells share.

Measured on real H01 sections, seeding at the deepest interior pixel of 10 mid-sized delivered cells per section
(`scratchpad/eval_harness.py`), mean IoU against the delivered label:

    sections 0, 37, 74 (tuned on)   0.723   (previous implementation 0.493)
    sections 12, 55, 88 (held out)  0.736   (previous 0.516)
    sections 25, 60, 95 (held out)  0.664   (previous 0.577)

and, with the clicked cell's own label blanked first — which is the real job, filling where there is no label yet —
it stays at 0.723, whereas a variant that leant on the neighbouring labels dropped from 0.864 to 0.667.

`SmartFillService` wraps it in the same preview/token/apply flow as SAM: nothing is written until the user has
looked at the proposed region, and a preview is refused if the labels changed in the meantime.
"""
from __future__ import annotations

import base64
import io
import threading
import time
import uuid
from collections import OrderedDict

import numpy as np
from PIL import Image
from scipy import ndimage, sparse
from scipy.sparse.linalg import cg

SCOPES = ("same", "same_bg", "any")
PREVIEW_TTL_S = 900
PREVIEW_RGBA = (255, 170, 0, 150)  # orange, so it cannot be confused with SAM's cyan
S4 = ndimage.generate_binary_structure(2, 1)  # 4-connectivity: an 8-connected membrane line is then a solid wall

# Tuned on sections 0/37/74 and left alone while the held-out sections were scored.
P = dict(
    sig=1.0,        # pre-smoothing of the EM (px): kills shot noise, keeps membranes
    win=63,         # box for the local mean / standard deviation (px)
    eps=4.0,        # floor on the local sd (grey levels), so flat areas do not blow up
    R=128,          # half window around the click; keeps the cost independent of the block size
    qt=0.60,        # quantile of membraneness separating "not membrane" from "membrane"
    dmin=4.5,       # an interior core must be this far from anything membrane-like
    minmark=12,     # cores smaller than this are neither marker nor background
    reloc=25,       # move a click that landed on an organelle onto the nearest core, at most this far
    beta=25.0,      # conductance = exp(-beta * m / mcap)
    mcap=0.8,
    floor=1e-4,     # conductance floor: keeps the system solvable across a solid membrane
    margin=45,      # solve on the markers' bounding box grown by this
    thr=0.80,       # probability level taken as the region at sensitivity 0.5
    lo=0.55, hi=0.97,   # probability level at sensitivity 0 / 1
    rtol=1e-4, maxit=300,
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
    boundary, so a fill stops earlier."""
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


# ------------------------------------------------------------------------------------------------ markers + solve
def _markers(m: np.ndarray, sy: int, sx: int, free_edges, p: dict):
    """(foreground, background, relocated seed). Foreground = the interior cores of the clicked cell, background =
    every other core plus the window border where that border is not the image border (so a cell running off the
    image is not truncated)."""
    nm = m <= np.quantile(m, p["qt"])
    if not nm.any():
        return None, None, (sy, sx)
    core = ndimage.distance_transform_edt(nm) >= p["dmin"]
    lab, n = ndimage.label(core, structure=S4)
    if n == 0:
        return None, None, (sy, sx)
    if not core[sy, sx] and p["reloc"]:   # the deepest point of a cell is very often inside a mitochondrion
        d, (iy, ix) = ndimage.distance_transform_edt(~core, return_indices=True)
        if d[sy, sx] <= p["reloc"]:
            sy, sx = int(iy[sy, sx]), int(ix[sy, sx])
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    sizes[0] = 0

    nml, _ = ndimage.label(nm, structure=S4)
    l_nm = nml[sy, sx]
    if l_nm == 0:
        d, (iy, ix) = ndimage.distance_transform_edt(~nm, return_indices=True)
        l_nm = nml[iy[sy, sx], ix[sy, sx]]
    own = np.bincount(lab[nml == l_nm].ravel(), minlength=n + 1) > 0.5 * np.maximum(sizes, 1)
    own[0] = False
    if not own.any():                      # the clicked cell has no core of its own: take the nearest one
        k = lab[sy, sx]
        if k == 0:
            d, (iy, ix) = ndimage.distance_transform_edt(~core, return_indices=True)
            k = lab[iy[sy, sx], ix[sy, sx]]
        if not k:
            return None, None, (sy, sx)
        own[k] = True
    fg = ndimage.binary_fill_holes(own[lab])
    keep = (sizes >= p["minmark"]) & ~own
    keep[0] = False
    bg = keep[lab]
    top, bottom, left, right = free_edges
    if top:
        bg[0, :] = True
    if bottom:
        bg[-1, :] = True
    if left:
        bg[:, 0] = True
    if right:
        bg[:, -1] = True
    bg &= ~fg
    return fg, bg, (sy, sx)


def _walker(m: np.ndarray, fg: np.ndarray, bg: np.ndarray, p: dict) -> np.ndarray:
    """P(a random walker started here reaches fg before bg), 4-connected, conductance exp(-beta*max(m_i, m_j))."""
    h, w = m.shape
    mm = np.clip(m, 0.0, p["mcap"]) / p["mcap"]
    ew = np.concatenate([
        (np.exp(-p["beta"] * np.maximum(mm[:, :-1], mm[:, 1:])) + p["floor"]).ravel(),
        (np.exp(-p["beta"] * np.maximum(mm[:-1, :], mm[1:, :])) + p["floor"]).ravel()]).astype(np.float64)
    unknown = ~(fg | bg)
    nu = int(unknown.sum())
    prob = fg.astype(np.float64)
    if nu == 0:
        return prob
    ids = np.full(h * w, -1, np.int64)
    ids[unknown.ravel()] = np.arange(nu)
    ids = ids.reshape(h, w)
    ea = np.concatenate([ids[:, :-1].ravel(), ids[:-1, :].ravel()])
    eb = np.concatenate([ids[:, 1:].ravel(), ids[1:, :].ravel()])
    fa = np.concatenate([fg[:, :-1].ravel(), fg[:-1, :].ravel()])
    fb = np.concatenate([fg[:, 1:].ravel(), fg[1:, :].ravel()])
    ua, ub = ea >= 0, eb >= 0
    diag = (np.bincount(ea[ua], weights=ew[ua], minlength=nu)
            + np.bincount(eb[ub], weights=ew[ub], minlength=nu))
    s = ua & fb
    rhs = np.bincount(ea[s], weights=ew[s], minlength=nu)
    s = ub & fa
    rhs += np.bincount(eb[s], weights=ew[s], minlength=nu)
    s = ua & ub
    rows = np.concatenate([ea[s], eb[s], np.arange(nu)])
    cols = np.concatenate([eb[s], ea[s], np.arange(nu)])
    data = np.concatenate([-ew[s], -ew[s], diag])
    A = sparse.coo_matrix((data, (rows, cols)), shape=(nu, nu)).tocsr()
    M = sparse.diags(1.0 / np.maximum(A.diagonal(), 1e-12))
    x, _ = cg(A, rhs, rtol=p["rtol"], atol=0.0, maxiter=p["maxit"], M=M)
    prob[unknown] = np.clip(x, 0.0, 1.0)
    return prob


def bounded_region(em: np.ndarray, seg: np.ndarray | None, x: int, y: int, sensitivity: float = 0.5,
                   max_radius: int = 0, scope: str = "same", reach: int = 4,
                   p: dict | None = None) -> tuple[np.ndarray, dict]:
    """The region for a boundary-aware fill from the click (x = column, y = row). Returns (mask, info).

    `scope` limits which existing labels may be overwritten: "same" only the clicked pixel's label (the safe
    default), "same_bg" that label plus unlabelled, "any" no restriction. `max_radius` > 0 caps how far the region
    may reach from the click, a blunt safety net for a membrane with a hole in it."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    p = dict(P, **(p or {}))
    em = np.asarray(em)
    H, W = em.shape
    x = int(np.clip(int(x), 0, W - 1))
    y = int(np.clip(int(y), 0, H - 1))
    s = float(np.clip(sensitivity, 0.0, 1.0))
    thr = (p["lo"] + (p["thr"] - p["lo"]) * s / 0.5) if s <= 0.5 else \
          (p["thr"] + (p["hi"] - p["thr"]) * (s - 0.5) / 0.5)

    R = p["R"]
    y0, y1 = max(0, y - R), min(H, y + R + 1)
    x0, x1 = max(0, x - R), min(W, x + R + 1)
    sub = em[y0:y1, x0:x1]
    sy, sx = y - y0, x - x0
    out = np.zeros((H, W), bool)
    label = int(seg[y, x]) if seg is not None else 0
    info: dict = {"seed": [x, y], "seed_id": str(label), "threshold": round(float(thr), 3), "scope": scope}

    m = membraneness(sub, p)
    fg, bg, (sy, sx) = _markers(m, sy, sx, (y0 > 0, y1 < H, x0 > 0, x1 < W), p)
    # _markers may move the seed off a dark organelle; everything below must use the moved one, not the click
    if fg is None or not fg.any():
        raise ValueError("点在膜/边界上或附近找不到细胞内部，请点细胞中间")

    ys, xs = np.nonzero(fg)          # solve only around the markers
    g = p["margin"]
    a0, a1 = max(0, ys.min() - g), min(m.shape[0], ys.max() + g + 1)
    b0, b1 = max(0, xs.min() - g), min(m.shape[1], xs.max() + g + 1)
    mc, fc, bc = m[a0:a1, b0:b1], fg[a0:a1, b0:b1], bg[a0:a1, b0:b1].copy()
    if a0 > 0:
        bc[0, :] |= ~fc[0, :]
    if a1 < m.shape[0]:
        bc[-1, :] |= ~fc[-1, :]
    if b0 > 0:
        bc[:, 0] |= ~fc[:, 0]
    if b1 < m.shape[1]:
        bc[:, -1] |= ~fc[:, -1]
    prob = _walker(mc, fc, bc, p)

    reg = prob >= thr
    lab, _ = ndimage.label(reg, structure=S4)
    k = lab[sy - a0, sx - b0] if (a0 <= sy < a1 and b0 <= sx < b1) else 0
    if k == 0:
        counts = np.bincount(lab[fc].ravel())
        counts[0] = 0
        k = int(counts.argmax()) if counts.size > 1 and counts.any() else 0
    reg = ndimage.binary_fill_holes(lab == k) if k else fc   # organelles and texture belong to the cell
    out[y0 + a0:y0 + a1, x0 + b0:x0 + b1] = reg

    gy, gx = y0 + sy, x0 + sx                       # the seed actually used, in block coordinates
    clipped = False
    if max_radius and max_radius > 0:
        yy, xx = np.ogrid[:H, :W]
        out &= (yy - y) ** 2 + (xx - x) ** 2 <= int(max_radius) ** 2
        clipped = True
    if scope != "any" and seg is not None:
        out &= (seg == label) if scope == "same" else ((seg == label) | (seg == 0))
        clipped = True
    if clipped and out.any():                       # clipping can break the region up; keep the piece at the seed
        lab2, _ = ndimage.label(out, structure=S4)
        k2 = lab2[gy, gx] or lab2[y, x]
        if k2:
            out = lab2 == k2
        else:
            counts = np.bincount(lab2.ravel())
            counts[0] = 0
            out = lab2 == int(counts.argmax())
    info["seed_used"] = [int(gx), int(gy)]

    ys, xs = np.nonzero(out)
    info["bbox"] = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1] if xs.size else None
    info["n_px"] = int(out.sum())
    return out, info


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


def overlay_png(mask: np.ndarray, rgba=PREVIEW_RGBA) -> str:
    over = np.zeros((*mask.shape, 4), dtype=np.uint8)
    over[mask] = rgba
    buf = io.BytesIO()
    Image.fromarray(over).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class SmartFillService:
    """Preview → token → apply. A preview is bound to the block, its work directory and the labels' revision, so a
    stale preview (someone edited in between) is refused instead of painting over the newer labels."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proposals: OrderedDict[str, dict] = OrderedDict()

    def preview(self, block, z: int, x: int, y: int, sensitivity: float = 0.5, max_radius: int = 0,
                scope: str = "same") -> dict:
        from emqc.annotate.sam import revision

        started = time.perf_counter()
        with block.lock:
            em = block.em_slice(z)
            seg = block.seg_slice(z) if block.has_seg else None
            mask, info = bounded_region(em, seg, x, y, sensitivity, max_radius, scope)
            rev = revision(block)
        token = uuid.uuid4().hex
        now = time.monotonic()
        with self.lock:
            self.proposals = OrderedDict((k, v) for k, v in self.proposals.items() if now - v["created"] < PREVIEW_TTL_S)
            self.proposals[token] = {"path": str(block.path.resolve()), "work": str(block.work.resolve()),
                                     "z": int(z), "mask": mask, "revision": rev, "created": now,
                                     "x": int(x), "y": int(y), "sensitivity": float(sensitivity),
                                     "max_radius": int(max_radius), "scope": scope, **info}
            while len(self.proposals) > 32:
                self.proposals.popitem(last=False)
        return {"token": token, "z": int(z), "n_px": int(mask.sum()),
                "seconds": round(time.perf_counter() - started, 3), "mask_png": overlay_png(mask), **info}

    def apply(self, block, token: str, new_id: int) -> dict | None:
        from emqc.annotate.sam import revision

        with self.lock:
            p = self.proposals.get(token)
            if p is None or time.monotonic() - p["created"] >= PREVIEW_TTL_S:
                raise ValueError("预览已过期，请重新点选")
            if p["path"] != str(block.path.resolve()) or p["work"] != str(block.work.resolve()):
                raise ValueError("预览不属于当前数据块")
        with block.lock:
            if p["revision"] != revision(block):
                raise ValueError("标注已变化，请重新点选后应用")
            rec = block.apply_mask(p["z"], p["mask"], new_id,
                                   {"x": p["x"], "y": p["y"], "seed_id": p["seed_id"], "sensitivity": p["sensitivity"],
                                    "max_radius": p["max_radius"], "scope": p["scope"]}, kind="smartfill")
        with self.lock:
            self.proposals.pop(token, None)
        return rec


service = SmartFillService()
