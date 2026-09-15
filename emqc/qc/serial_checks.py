"""Serial-section QC checks (6). Operate on serial thumbnails (~32 nm/px) of consecutive slices.

Implemented (heuristic v0.1): slice_jump, local_misalign (tile-wise shift residual), global_misalign,
                              z_order (duplicate / swap / misplaced).
Stubs (framework only):      local_deformation, section_deformation.

Reference selection (shared by all neighbour comparisons)
  * every ok slice is *evaluated*, but only slices that passed the slice-level checks form the
    "reference chain" that later slices are compared against;
  * a slice is compared with the previous chain slice, unless that one is a known outlier (for this
    or any earlier serial check), in which case the slice two back is used; if both are outliers the
    chain resets (score None, note "reference_reset") so one bad section cannot cascade into many flags;
  * the expected correlation depends on the z gap (measured per block from gap-1 and gap-2 pairs);
  * thresholds are physical (nm) when the voxel size is known, fractions of the image otherwise.
Block-level: if consecutive sections are uncorrelated at the analysis scale the block gets one
  HIGH finding and per-slice serial checks are skipped instead of flagging every slice.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from .base import BlockContext, QCCheck, Severity, SliceRecord, StubCheck, chain_position, clip01, pick_reference, reference_chain, register

# ----------------------------------------------------------------------------- image helpers


def _prep(thumb: np.ndarray, sigma: float) -> np.ndarray:
    """High-pass + normalise so that dot products are correlation coefficients."""
    t = thumb.astype(np.float32)
    hp = t - ndimage.gaussian_filter(t, sigma)
    hp -= hp.mean()
    n = np.sqrt((hp * hp).sum()) + 1e-8
    return hp / n


def _parabolic(c_m: float, c_0: float, c_p: float) -> float:
    """Sub-sample peak offset in [-0.5, 0.5] from three samples around the maximum."""
    d = c_m - 2.0 * c_0 + c_p
    if abs(d) < 1e-12:
        return 0.0
    return float(np.clip(0.5 * (c_m - c_p) / d, -0.5, 0.5))


def _phase_corr(a: np.ndarray, b: np.ndarray, subpixel: bool = True) -> tuple[float, float, float]:
    """Shift (dy, dx) that maps b onto a, and the correlation peak height.

    The integer peak is refined by a parabolic fit on its two neighbours per axis. Without this the
    smallest detectable shift is one thumbnail pixel, i.e. `serial_factor` full-res pixels (4 on
    mouse_30um), which is far coarser than the 200 nm the misalignment checks are meant to catch.
    """
    fa, fb = np.fft.rfft2(a), np.fft.rfft2(b)
    cps = fa * np.conj(fb)
    cps /= np.abs(cps) + 1e-8
    corr = np.fft.irfft2(cps, s=a.shape)
    H, W = corr.shape
    iy, ix = np.unravel_index(int(np.argmax(corr)), corr.shape)
    peak = float(corr[iy, ix])
    oy = _parabolic(float(corr[(iy - 1) % H, ix]), peak, float(corr[(iy + 1) % H, ix])) if subpixel else 0.0
    ox = _parabolic(float(corr[iy, (ix - 1) % W]), peak, float(corr[iy, (ix + 1) % W])) if subpixel else 0.0
    dy, dx = int(iy), int(ix)
    if dy > H // 2:
        dy -= H
    if dx > W // 2:
        dx -= W
    return dy + oy, dx + ox, peak


def _ncc_shifted(a: np.ndarray, b: np.ndarray, dy: int, dx: int) -> float:
    """Correlation of a with b after shifting b by (dy, dx), computed on the overlap only."""
    H, W = a.shape
    if abs(dy) >= H or abs(dx) >= W:
        return 0.0
    ay0, ay1 = max(dy, 0), H + min(dy, 0)
    ax0, ax1 = max(dx, 0), W + min(dx, 0)
    by0, by1 = max(-dy, 0), H + min(-dy, 0)
    bx0, bx1 = max(-dx, 0), W + min(-dx, 0)
    pa = a[ay0:ay1, ax0:ax1]
    pb = b[by0:by1, bx0:bx1]
    pa = pa - pa.mean()
    pb = pb - pb.mean()
    denom = np.sqrt((pa * pa).sum() * (pb * pb).sum()) + 1e-8
    return float((pa * pb).sum() / denom)


def _pair(pa: np.ndarray, pb: np.ndarray, ref_z: int, z: int, factor: float) -> dict:
    dy, dx, peak = _phase_corr(pa, pb)
    return dict(ref_z=ref_z, z=z, dy=dy, dx=dx, peak=peak, ncc0=float((pa * pb).sum()),
                ncc=_ncc_shifted(pa, pb, int(round(dy)), int(round(dx))),  # 对齐取整，位移量保留小数
                gap=abs(z - ref_z), shift_px=float(np.hypot(dy, dx) * factor))


# ----------------------------------------------------------------------------- shared pairwise cache


def pairwise(ctx: BlockContext, min_median_ncc: float = 0.15) -> dict:
    """Pair statistics, cached on the context.

    chain        : slices usable as references (ok + passed slice-level checks + has sthumb)
    evaluated    : every ok slice with a thumbnail (chain members and non-members)
    p1[z], p2[z] : pair(previous chain slice, z) and pair(chain slice two back, z)
    expected(g)  : expected aligned NCC for a z gap of g (from the block's own gap-1 / gap-2 medians)
    """
    if "pairwise" in ctx.cache:
        return ctx.cache["pairwise"]
    evaluated = [r for r in ctx.slices if r.ok and r.sthumb is not None and not r.reference_only]
    chain = [r for r in reference_chain(ctx) if r.sthumb is not None]
    nm = ctx.serial_nm_per_px
    sigma = max(1.0, 128.0 / nm) if nm else 2.0  # remove structure coarser than ~128 nm
    # 预处理要覆盖"被评估的"和"只当参考的"两类，后者来自上一段（reference_only）
    prepped = {r.z: _prep(r.sthumb, sigma) for r in {id(x): x for x in list(evaluated) + [c for c in chain if c.sthumb is not None]}.values()}
    factor = float(ctx.serial_factor)
    pos = {id(r): i for i, r in enumerate(chain)}
    p1: dict[int, dict] = {}
    p2: dict[int, dict] = {}
    for r in evaluated:
        k = pos.get(id(r), chain_position(chain, r))
        if k >= 1:
            p1[r.z] = _pair(prepped[chain[k - 1].z], prepped[r.z], chain[k - 1].z, r.z, factor)
        if k >= 2:
            p2[r.z] = _pair(prepped[chain[k - 2].z], prepped[r.z], chain[k - 2].z, r.z, factor)
    g1 = [p1[r.z]["ncc"] for r in chain if r.z in p1 and p1[r.z]["gap"] == 1] or [p1[r.z]["ncc"] for r in chain if r.z in p1]
    g2 = [p2[r.z]["ncc"] for r in chain if r.z in p2 and p2[r.z]["gap"] == 2] or [p2[r.z]["ncc"] for r in chain if r.z in p2]
    m1 = float(np.median(g1)) if g1 else float("nan")
    m2 = float(np.median(g2)) if g2 else float("nan")

    def expected(gap: int) -> float:
        a = float(np.clip(m1, 0.05, 0.999)) if np.isfinite(m1) else 0.5
        if gap <= 1 or not np.isfinite(m2) or m2 <= 0:
            return a**gap
        b = float(np.clip(m2, 0.02, a))
        return b * (b / a) ** (gap - 2)

    out = {
        "chain": chain, "evaluated": evaluated, "pos": pos, "prepped": prepped, "p1": p1, "p2": p2,
        "median_ncc": m1, "median_ncc_gap2": m2, "expected": expected, "sigma": sigma,
        "uncorrelated": bool(np.isfinite(m1) and m1 < min_median_ncc and len(chain) >= 4),
    }
    ctx.cache["pairwise"] = out
    ctx.cache.setdefault("serial_outliers", set())
    return out


def tile_shift_field(ctx: BlockContext, prepped: dict, p: dict, params: dict) -> dict | None:
    """Per-tile shifts between a slice and its reference, cached per (ref_z, z) pair.

    Three checks read this one computation: local_misalignment (residual vs the global translation),
    section_deformation (the affine fitted to the field: scale + shear) and local_deformation
    (what is left after removing that affine — genuinely non-rigid movement).
    """
    key = ("tile_field", p["ref_z"], p.get("z", id(p)), int(params.get("grid", 4)), int(params.get("min_tile_px", 32)), int(params.get("min_grid", 2)))
    cached = ctx.cache.get(key)
    if cached is not None:
        return cached
    a, b = prepped[p["ref_z"]], prepped[p["z"]]
    H, W = a.shape
    g = int(min(params.get("grid", 4), min(H, W) // params.get("min_tile_px", 32)))
    # 仿射拟合每个方向有 3 个未知数：2x2 的 4 个点等于在拟合噪声，会造出虚假的应变。
    # 所以形变类检查要求至少 3x3（min_grid=3），错位检查 2x2 就够（它只用中位残差）。
    if g < int(params.get("min_grid", 2)):
        ctx.cache[key] = None
        return None
    th, tw = H // g, W // g
    f = float(ctx.serial_factor)
    tol = params.get("tolerance_thumb_px", 1.0)
    tiles = []
    for gy in range(g):
        for gx in range(g):
            ta = a[gy * th : (gy + 1) * th, gx * tw : (gx + 1) * tw]
            tb = b[gy * th : (gy + 1) * th, gx * tw : (gx + 1) * tw]
            if ta.std() < params.get("min_tile_std", 1e-4) or tb.std() < params.get("min_tile_std", 1e-4):
                continue
            dy, dx, peak = _phase_corr(ta - ta.mean(), tb - tb.mean())
            if peak < params.get("min_peak", 0.05):
                continue
            res = max(0.0, float(np.hypot(dy - p["dy"], dx - p["dx"])) - tol) * f
            tiles.append({"tile": [gy, gx], "cy": (gy + 0.5) * th, "cx": (gx + 0.5) * tw,
                          "dy_thumb": float(dy), "dx_thumb": float(dx), "dy": dy * f, "dx": dx * f, "residual_px": res, "peak": peak})
    out = {"tiles": tiles, "n": len(tiles), "g": g, "th": th, "tw": tw, "factor": f}
    ctx.cache[key] = out
    return out


def fit_affine(tiles: list[dict], H: float, W: float) -> dict | None:
    """Least-squares affine fit of the tile displacement field: d = A · (centred position) + t.

    A - I is the deformation of the section relative to a pure translation: its symmetric part carries
    scaling / stretch, its antisymmetric part rotation, the off-diagonal symmetric term shear.
    Positions are normalised to [-1, 1] so the coefficients read as "fraction of the tile across the field".
    """
    if len(tiles) < 4:
        return None
    y = np.array([(t["cy"] / H) * 2 - 1 for t in tiles])
    x = np.array([(t["cx"] / W) * 2 - 1 for t in tiles])
    M = np.stack([y, x, np.ones_like(y)], axis=1)
    dy = np.array([t["dy_thumb"] for t in tiles])
    dx = np.array([t["dx_thumb"] for t in tiles])
    try:
        cy, *_ = np.linalg.lstsq(M, dy, rcond=None)
        cx, *_ = np.linalg.lstsq(M, dx, rcond=None)
    except np.linalg.LinAlgError:
        return None
    A = np.array([[cy[0], cy[1]], [cx[0], cx[1]]])  # d(dy)/dy, d(dy)/dx ; d(dx)/dy, d(dx)/dx
    sym = 0.5 * (A + A.T)
    resid_y = dy - M @ cy
    resid_x = dx - M @ cx
    resid = np.hypot(resid_y, resid_x)
    return {"A": A.tolist(), "translation_thumb": [float(cy[2]), float(cx[2])],
            "scale_y": float(sym[0, 0]), "scale_x": float(sym[1, 1]), "shear": float(sym[0, 1]),
            "rotation": float(0.5 * (A[1, 0] - A[0, 1])),
            "residual_thumb_px": resid.tolist(), "median_residual_thumb": float(np.median(resid)),
            "max_residual_thumb": float(resid.max()), "n_tiles": len(tiles)}


class _SerialWalk:
    """Iterate over evaluated slices with the shared two-back reference rule."""

    def __init__(self, check: QCCheck, ctx: BlockContext, pw: dict):
        self.check, self.ctx, self.pw = check, ctx, pw
        shared = ctx.cache["serial_outliers"]
        self.outlier = [r.z in shared for r in pw["chain"]]
        self.flagged: set[int] = set()

    def __iter__(self):
        pw = self.pw
        for r in pw["evaluated"]:
            k = pw["pos"].get(id(r), chain_position(pw["chain"], r))
            if k == 0:
                r.scores[self.check.name] = 1.0
                continue
            j = pick_reference(k, self.outlier)
            if j is None:
                self.check.skip(r, "reference_reset")
                continue
            p = pw["p1"][r.z] if j == k - 1 else pw["p2"][r.z]
            yield r, p

    def mark(self, r: SliceRecord, is_outlier: bool) -> None:
        if is_outlier:
            self.flagged.add(r.z)
            if id(r) in self.pw["pos"]:
                self.outlier[self.pw["pos"][id(r)]] = True

    def finish(self) -> None:
        self.ctx.cache["serial_outliers"] |= self.flagged


def _skip_all(check: QCCheck, ctx: BlockContext, note: str) -> None:
    for r in ctx.slices:
        check.skip(r, note if r.ok else None)


# ----------------------------------------------------------------------------- checks


@register
class SliceJumpCheck(QCCheck):
    name = "slice_jump"
    failure_type = "slice_jump"
    level = "serial"
    stage = "serial_qc"
    description = "切片跳变：对齐后与参考切片的相关性，相对本 block 在相同 z 间距下的期望值的比值（内容突变、连续缺片、撕裂）。"
    default_params = {"min_median_ncc": 0.15}
    default_thresholds = {"low": 0.6, "medium": 0.45, "high": 0.3, "critical": 0.15}

    def run(self, ctx: BlockContext) -> None:
        pw = pairwise(ctx, self.params["min_median_ncc"])
        for r in ctx.slices:
            if not r.ok or r.sthumb is None:
                self.skip(r)
        if pw["uncorrelated"]:
            self.emit_block(ctx, Severity.HIGH, pw["median_ncc"], details={"reason": "sections_uncorrelated", "median_ncc_gap1": pw["median_ncc"], "median_ncc_gap2": pw["median_ncc_gap2"], "serial_nm_per_px": ctx.serial_nm_per_px, "n_chain": len(pw["chain"])})
            _skip_all(self, ctx, "block_uncorrelated")
            return
        walk = _SerialWalk(self, ctx, pw)
        for r, p in walk:
            exp = pw["expected"](p["gap"])
            score = clip01(p["ncc"] / exp) if exp > 0 else 1.0
            sev = self.emit(ctx, r, score, z_to=p["ref_z"], details={"ncc_aligned": p["ncc"], "ncc_raw": p["ncc0"], "expected_ncc": exp, "gap": p["gap"], "ref_z": p["ref_z"]})
            walk.mark(r, sev >= Severity.HIGH)
        walk.finish()


@register
class LocalMisalignmentCheck(QCCheck):
    name = "local_misalignment"
    failure_type = "local_misalign"
    level = "serial"
    stage = "serial_qc"
    description = "局部错位（启发式）：把序列缩略图分成网格，逐块相位相关，块位移相对全局位移的残差中位数（nm）。"
    default_params = {"grid": 4, "min_tile_px": 32, "max_residual_nm": 150.0, "max_residual_frac": 0.03, "min_tile_std": 1e-4,
                      "min_peak": 0.02, "gate_frac": 0.25, "tolerance_thumb_px": 1.0, "max_gap": 2}

    def run(self, ctx: BlockContext) -> None:
        pw = pairwise(ctx)
        prepped = pw["prepped"]
        for r in ctx.slices:
            if not r.ok or r.sthumb is None:
                self.skip(r)
        if pw["uncorrelated"]:
            _skip_all(self, ctx, "block_uncorrelated")
            return
        f = float(ctx.serial_factor)
        nm = ctx.ds.nm_per_px_xy
        short = ctx.nominal_short
        limit_px = (self.params["max_residual_nm"] / nm) if nm else self.params["max_residual_frac"] * short
        walk = _SerialWalk(self, ctx, pw)
        for r, p in walk:
            if p["gap"] > self.params["max_gap"]:
                self.skip(r, f"reference_too_far(gap={p['gap']})")  # tissue moves too much over several missing sections
                continue
            if p["ncc"] < self.params["gate_frac"] * pw["expected"](p["gap"]):
                self.skip(r, "pair_uncorrelated")  # a jump, not a local misalignment
                walk.mark(r, True)
                continue
            field = tile_shift_field(ctx, prepped, p, self.params)
            if field is None:
                self.skip(r, "thumbnail_too_small")
                continue
            if field["n"] < 3:
                self.skip(r, "too_few_textured_tiles")
                continue
            tiles = field["tiles"]
            residuals = [t["residual_px"] for t in tiles]
            th, tw = field["th"], field["tw"]
            med = float(np.median(residuals))
            score = 1.0 - clip01(med / limit_px)
            worst = max(tiles, key=lambda t: t["residual_px"])
            bbox = [int(worst["tile"][1] * tw * f), int(worst["tile"][0] * th * f), int((worst["tile"][1] + 1) * tw * f), int((worst["tile"][0] + 1) * th * f)]
            sev = self.emit(ctx, r, score, z_to=p["ref_z"], coordinate={"bbox": bbox}, details={"median_residual_px": med, "median_residual_nm": med * nm if nm else None, "n_tiles": len(residuals), "grid": field["g"], "worst_tile": worst, "ref_z": p["ref_z"]})
            walk.mark(r, sev >= Severity.HIGH)
        walk.finish()


@register
class GlobalMisalignmentCheck(QCCheck):
    name = "global_misalignment"
    failure_type = "global_misalign"
    level = "serial"
    stage = "serial_qc"
    description = "全局错位：相位相关得到的相对参考切片的整体平移量（nm；无体素尺寸时用图像短边比例）。"
    default_params = {"max_shift_nm": 200.0, "max_shift_frac": 0.05, "gate_frac": 0.5, "max_gap": 3}

    def run(self, ctx: BlockContext) -> None:
        pw = pairwise(ctx)
        for r in ctx.slices:
            if not r.ok or r.sthumb is None:
                self.skip(r)
        if pw["uncorrelated"]:
            _skip_all(self, ctx, "block_uncorrelated")
            return
        nm = ctx.ds.nm_per_px_xy
        short = ctx.nominal_short
        limit_px = (self.params["max_shift_nm"] / nm) if nm else self.params["max_shift_frac"] * short
        f = float(ctx.serial_factor)
        walk = _SerialWalk(self, ctx, pw)
        for r, p in walk:
            if p["gap"] > self.params["max_gap"]:
                self.skip(r, f"reference_too_far(gap={p['gap']})")
                continue
            if p["ncc"] < self.params["gate_frac"] * pw["expected"](p["gap"]):
                self.skip(r, "pair_uncorrelated")
                walk.mark(r, True)
                continue
            score = 1.0 - clip01(p["shift_px"] / limit_px)
            sev = self.emit(ctx, r, score, z_to=p["ref_z"], coordinate={"shift": {"dx": p["dx"] * f, "dy": p["dy"] * f}}, details={"shift_px": p["shift_px"], "shift_nm": p["shift_px"] * nm if nm else None, "peak": p["peak"], "ref_z": p["ref_z"], "gap": p["gap"]})
            walk.mark(r, sev >= Severity.HIGH)
        walk.finish()


@register
class ZOrderCheck(QCCheck):
    name = "z_order_error"
    failure_type = "z_order"
    level = "serial"
    stage = "serial_qc"
    description = "z-order 错误：重复切片（与前一张几乎相同）、相邻两张互换（弱-强-弱链接模式）、或某张放错位置（与非相邻切片更相似）。"
    default_params = {"duplicate_ncc": 0.985, "margin": 0.03, "swap_ref": 0.10, "odd_ref": 0.15, "match_frac": 0.8}

    def run(self, ctx: BlockContext) -> None:
        pw = pairwise(ctx)
        chain, p1, p2, prepped = pw["chain"], pw["p1"], pw["p2"], pw["prepped"]
        for r in ctx.slices:
            if not r.ok or r.sthumb is None:
                self.skip(r)
            elif pw["uncorrelated"]:
                self.skip(r, "block_uncorrelated")
            else:
                r.scores[self.name] = 1.0
        if pw["uncorrelated"]:
            return
        m = self.params["margin"]
        exp1 = pw["expected"](1)
        n = len(chain)
        zs = [r.z for r in chain]
        for i, r in enumerate(chain):
            p = p1.get(r.z)
            if p is None:
                continue
            # (a) duplicate of the previous chain slice
            if p["gap"] == 1 and p["ncc"] >= self.params["duplicate_ncc"] and abs(p["dy"]) + abs(p["dx"]) <= 1:
                self.emit(ctx, r, 0.0, z_to=p["ref_z"], details={"reason": "duplicate", "ncc": p["ncc"]})
                ctx.cache["serial_outliers"].add(r.z)
                continue
            nxt = chain[i + 1] if i + 1 < n else None
            nxt2 = chain[i + 2] if i + 2 < n else None
            # (b) swap of (r, next): weak-strong-weak links, strong skip links on both sides
            if nxt2 is not None and nxt.z in p2 and nxt2.z in p2 and nxt2.z in p1:
                A = p2[nxt.z]["ncc"] - p["ncc"]  # (prev, next) beats (prev, r)
                B = p2[nxt2.z]["ncc"] - p1[nxt2.z]["ncc"]  # (r, next2) beats (next, next2)
                if A > m and B > m:
                    s = 1.0 - clip01(min(A, B) / self.params["swap_ref"])
                    self.emit(ctx, r, s, z_to=nxt.z, details={"reason": "swap_suspected", "A": A, "B": B})
                    self.emit(ctx, nxt, s, z_to=r.z, details={"reason": "swap_suspected", "A": A, "B": B})
                    continue
            # (c) misplaced: neighbours match each other better than this slice, and this slice matches some other z well
            if nxt is not None and nxt.z in p2 and nxt.z in p1:
                excess = p2[nxt.z]["ncc"] - max(p["ncc"], p1[nxt.z]["ncc"])
                if excess > m:
                    best_z, best = None, -1.0
                    for k, other in enumerate(chain):
                        if abs(k - i) < 2:
                            continue
                        q = _pair(prepped[other.z], prepped[r.z], other.z, r.z, 1.0)
                        if q["ncc"] > best:
                            best_z, best = other.z, q["ncc"]
                    if best_z is not None and best >= self.params["match_frac"] * exp1:
                        self.emit(ctx, r, 1.0 - clip01(excess / self.params["odd_ref"]), z_to=best_z, details={"reason": "misplaced", "excess": excess, "best_match_z": best_z, "best_match_ncc": best})


@register
class LocalDeformationCheck(QCCheck):
    name = "local_deformation"
    failure_type = "local_deformation"
    level = "serial"
    stage = "serial_qc"
    description = (
        "局部形变：把逐格位移场拟合成仿射变换后剩下的残差。仿射部分（整体平移 + 拉伸 + 剪切）交给 section_deformation，"
        "这里只看去掉仿射之后仍然存在的、非刚性的位移——也就是组织内部不均匀地动了。"
    )
    # gate_frac 比跳变检查低：形变会拉低整体相关性，用同样的门槛等于把自己要找的东西挡在外面。
    # 逐格的 min_peak 才是真正保证"这一格对得上"的判据。
    default_params = {"grid": 4, "min_tile_px": 16, "min_tile_std": 1e-4, "min_peak": 0.02, "tolerance_thumb_px": 0.5,
                      "max_residual_nm": 120.0, "max_residual_frac": 0.02, "gate_frac": 0.2, "max_gap": 2, "min_tiles": 6, "min_grid": 3}
    # 合成缺陷标定：注入的非刚性扭曲得 0.813，最差干净切片 0.971，间隔仅 0.158 —— 这是四项里区分度最差的，
    # 档位必须整体上移才抓得住，代价是对干净数据的容忍度变小。真实数据上要重新标定。
    default_thresholds = {"low": 0.93, "medium": 0.88, "high": 0.78, "critical": 0.60}

    def run(self, ctx: BlockContext) -> None:
        pw = pairwise(ctx)
        prepped = pw["prepped"]
        for r in ctx.slices:
            if not r.ok or r.sthumb is None:
                self.skip(r)
        if pw["uncorrelated"]:
            _skip_all(self, ctx, "block_uncorrelated")
            return
        f = float(ctx.serial_factor)
        nm = ctx.ds.nm_per_px_xy
        limit_px = (self.params["max_residual_nm"] / nm) if nm else self.params["max_residual_frac"] * ctx.nominal_short
        walk = _SerialWalk(self, ctx, pw)
        for r, p in walk:
            if p["gap"] > self.params["max_gap"]:
                self.skip(r, f"reference_too_far(gap={p['gap']})")
                continue
            if p["ncc"] < self.params["gate_frac"] * pw["expected"](p["gap"]):
                self.skip(r, "pair_uncorrelated")
                continue
            field = tile_shift_field(ctx, prepped, p, self.params)
            if field is None or field["n"] < self.params["min_tiles"]:
                self.skip(r, "too_few_textured_tiles" if field else "thumbnail_too_small")
                continue
            a = prepped[p["ref_z"]]
            fit = fit_affine(field["tiles"], a.shape[0], a.shape[1])
            if fit is None:
                self.skip(r, "affine_fit_failed")
                continue
            med_px = fit["median_residual_thumb"] * f
            score = 1.0 - clip01(med_px / limit_px)
            worst = max(zip(field["tiles"], fit["residual_thumb_px"]), key=lambda t: t[1])
            th, tw = field["th"], field["tw"]
            gy, gx = worst[0]["tile"]
            bbox = [int(gx * tw * f), int(gy * th * f), int((gx + 1) * tw * f), int((gy + 1) * th * f)]
            sev = self.emit(ctx, r, score, z_to=p["ref_z"], coordinate={"bbox": bbox},
                            details={"median_residual_after_affine_px": round(med_px, 3),
                                     "median_residual_nm": round(med_px * nm, 1) if nm else None,
                                     "max_residual_px": round(worst[1] * f, 3), "n_tiles": field["n"],
                                     "worst_tile": worst[0]["tile"], "ref_z": p["ref_z"],
                                     "note": "已扣除整体平移与仿射形变，剩下的是非刚性位移"})
            walk.mark(r, sev >= Severity.HIGH)
        walk.finish()


@register
class SectionDeformationCheck(QCCheck):
    name = "section_deformation"
    failure_type = "section_deformation"
    level = "serial"
    stage = "serial_qc"
    description = (
        "Section deformation：整张切片相对参考切片的拉伸 / 压缩 / 剪切（切片制备时的机械形变）。"
        "对逐格位移场拟合仿射变换，取其中的尺度与剪切分量偏离刚体的程度；纯平移和纯旋转不算形变。"
    )
    default_params = {"grid": 4, "min_tile_px": 16, "min_tile_std": 1e-4, "min_peak": 0.02, "tolerance_thumb_px": 0.5,
                      "strain_ref": 0.03, "k_mad": 3.0, "min_spread": 0.004, "gate_frac": 0.2, "max_gap": 2, "min_tiles": 6, "min_grid": 3}
    # 合成缺陷标定：注入的仿射形变得 0.0，最差干净切片 0.726（5 分位 0.792）
    default_thresholds = {"low": 0.70, "medium": 0.55, "high": 0.40, "critical": 0.20}

    def run(self, ctx: BlockContext) -> None:
        pw = pairwise(ctx)
        prepped = pw["prepped"]
        for r in ctx.slices:
            if not r.ok or r.sthumb is None:
                self.skip(r)
        if pw["uncorrelated"]:
            _skip_all(self, ctx, "block_uncorrelated")
            return
        # 两遍：先量出本 block 每一对的应变，再相对本 block 的典型值判断。
        # 原因：相邻切片的组织结构本来就在变，仿射拟合分不清"切片被拉伸"和"组织换了一层"。
        # 绝对阈值会把这份内在变化整体判成形变（实测干净切片也能量到 0.02–0.05 的应变）。
        walk = _SerialWalk(self, ctx, pw)
        measured = []
        for r, p in walk:
            if p["gap"] > self.params["max_gap"]:
                self.skip(r, f"reference_too_far(gap={p['gap']})")
                continue
            if p["ncc"] < self.params["gate_frac"] * pw["expected"](p["gap"]):
                self.skip(r, "pair_uncorrelated")
                continue
            field = tile_shift_field(ctx, prepped, p, self.params)
            if field is None or field["n"] < self.params["min_tiles"]:
                self.skip(r, "too_few_textured_tiles" if field else "thumbnail_too_small")
                continue
            a = prepped[p["ref_z"]]
            fit = fit_affine(field["tiles"], a.shape[0], a.shape[1])
            if fit is None:
                self.skip(r, "affine_fit_failed")
                continue
            half = 0.5 * min(a.shape)
            strain_y, strain_x = fit["scale_y"] / half, fit["scale_x"] / half
            shear = fit["shear"] / half
            strain = float(max(abs(strain_y), abs(strain_x), abs(shear)))
            measured.append((r, p, fit, field, strain, strain_y, strain_x, shear, half))
        if not measured:
            walk.finish()
            return
        vals = np.array([m[4] for m in measured])
        base = float(np.median(vals))
        mad = float(np.median(np.abs(vals - base))) * 1.4826
        floor = max(mad, self.params["min_spread"])
        for r, p, fit, field, strain, sy, sx, sh, half in measured:
            excess = max(0.0, strain - base - self.params["k_mad"] * floor)
            score = 1.0 - clip01(excess / self.params["strain_ref"])
            sev = self.emit(ctx, r, score, z_to=p["ref_z"],
                            details={"strain_y": round(sy, 5), "strain_x": round(sx, 5), "shear": round(sh, 5),
                                     "strain": round(strain, 5), "block_median_strain": round(base, 5),
                                     "block_mad": round(mad, 5), "excess_over_block": round(excess, 5),
                                     "rotation_thumb": round(fit["rotation"], 4), "n_tiles": field["n"], "ref_z": p["ref_z"],
                                     "note": "应变相对本 block 的典型值判断：相邻切片的组织变化会造成一个共同的基线，只有明显超出基线的才是形变"})
            walk.mark(r, sev >= Severity.HIGH)
        walk.finish()
