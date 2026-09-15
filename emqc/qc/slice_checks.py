"""Slice-level QC checks (10). Operate on per-slice statistics + thumbnails computed at ingest.

Implemented (heuristic v0.1): missing, corrupt, blank, blur, saturation, crack (fill regions), brightness_jump, contrast_drift.
Stubs (framework only):      charging, contamination.

Order matters: the neighbour-based checks (brightness_jump, contrast_drift) only use slices that
passed the structural checks before them as references (see base.reference_chain).
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from .base import BlockContext, QCCheck, Severity, StubCheck, bbox_from_mask, chain_position, clip01, pick_reference, reference_chain, register


@register
class MissingSliceCheck(QCCheck):
    name = "missing_slice"
    failure_type = "missing"
    description = "缺失切片：该 z 没有文件 / 没有任何 chunk。"
    maturity = "stable"

    def run(self, ctx: BlockContext) -> None:
        for r in ctx.slices:
            if r.status == "missing":
                self.emit(ctx, r, 0.0, details={"error": r.error})
            else:
                r.scores[self.name] = 1.0


@register
class CorruptSliceCheck(QCCheck):
    name = "corrupt_slice"
    failure_type = "corrupt"
    description = "损坏切片：文件无法解码，或尺寸与数据集不一致。"
    maturity = "stable"

    def run(self, ctx: BlockContext) -> None:
        for r in ctx.slices:
            if r.status == "corrupt":
                self.emit(ctx, r, 0.0, details={"error": r.error})
            elif r.ok:
                r.scores[self.name] = 1.0
            else:
                self.skip(r)


@register
class BlankSliceCheck(QCCheck):
    name = "blank_slice"
    failure_type = "blank"
    description = "空白切片：几乎没有灰度变化（std≈0）或绝大多数像素为同一值。"
    default_params = {"std_ref": 0.02, "mode_frac_start": 0.8}

    def run(self, ctx: BlockContext) -> None:
        p = self.params
        for r in ctx.slices:
            if not r.ok:
                self.skip(r)
                continue
            s_std = clip01(r.stats["std"] / p["std_ref"])
            fm = r.stats.get("frac_mode", 0.0)
            s_mode = clip01(1.0 - max(fm - p["mode_frac_start"], 0.0) / (1.0 - p["mode_frac_start"]))
            self.emit(ctx, r, min(s_std, s_mode), details={"std": r.stats["std"], "frac_mode": fm})


@register
class SevereBlurCheck(QCCheck):
    name = "severe_blur"
    failure_type = "blur"
    description = (
        "严重模糊：Laplacian 方差归一化后的清晰度。取两个信号的最小值——相对本 block 中位数的比值（发现块内个别糊的），"
        "以及相对绝对下限的比值（发现整块都糊的：只看相对值时，均匀模糊的 block 每张都是 1.0）。"
    )
    default_params = {"min_reference": 1e-6, "absolute_floor": 0.02}
    default_thresholds = {"low": 0.75, "medium": 0.55, "high": 0.35, "critical": 0.2}

    def run(self, ctx: BlockContext) -> None:
        cands = [r for r in ctx.slices if r.ok and r.severity_of(["blank_slice"]) < Severity.HIGH]
        ref = float(np.median([r.stats["sharpness"] for r in cands])) if cands else float("nan")
        for r in ctx.slices:
            if not r.ok or r.severity_of(["blank_slice"]) >= Severity.HIGH:
                self.skip(r, "blank" if r.ok else None)
                continue
            if not np.isfinite(ref) or ref < self.params["min_reference"]:
                r.scores[self.name] = 1.0
                continue
            sharp = float(r.stats["sharpness"])
            ratio = sharp / ref
            floor = float(self.params["absolute_floor"])
            abs_ratio = (sharp / floor) if floor > 0 else float("inf")
            self.emit(ctx, r, min(clip01(ratio), clip01(abs_ratio)),
                      details={"sharpness": sharp, "block_median": ref, "ratio": ratio, "absolute_floor": floor,
                               "absolute_ratio": round(abs_ratio, 4), "limited_by": "absolute" if abs_ratio < ratio else "block_median"})


@register
class SaturationCheck(QCCheck):
    name = "saturation"
    failure_type = "saturation"
    description = "饱和：处于灰度上下限（0 / max）的像素比例，并给出饱和区域包围盒。"
    default_params = {"frac_ref": 0.20, "thumb_low": 0.005, "thumb_high": 0.995}

    def run(self, ctx: BlockContext) -> None:
        for r in ctx.slices:
            if not r.ok:
                self.skip(r)
                continue
            frac = r.stats.get("frac_low_sat", 0.0) + r.stats.get("frac_high_sat", 0.0)
            score = 1.0 - clip01(frac / self.params["frac_ref"])
            bbox = None
            if r.thumb is not None and score < 1.0:
                mask = (r.thumb <= self.params["thumb_low"]) | (r.thumb >= self.params["thumb_high"])
                bbox = bbox_from_mask(mask, ctx.scale_yx)
            self.emit(ctx, r, score, coordinate={"bbox": bbox}, details={"frac_low": r.stats.get("frac_low_sat"), "frac_high": r.stats.get("frac_high_sat")})


@register
class CrackCheck(QCCheck):
    name = "crack"
    failure_type = "crack"
    description = (
        "填充值（默认 0）连通域，按形状分三类：狭长的（主轴比 ≥ 3）记 crack（裂缝 / 褶皱）；成块且贴着图像边缘、面积超过 15% 的记 no_coverage"
        "（组织没填满画布：对齐后移出视野或缺 tile，是采集 / 拼接问题而不是切片损伤）；其余成块的记 missing_region。分数按面积占比。"
    )
    # area_ref 是"开始扣分"的面积，area_full 是"扣到 0 分"的面积；两者之间按 log 递减，
    # 这样 6% 和 90% 的无数据区不会同样是 critical（旧版线性刻度在 5% 就饱和到 0）
    default_params = {"area_ref": 0.02, "area_full": 0.60, "min_frac": 0.002, "elongation_crack": 3.0, "no_coverage_min_frac": 0.15, "no_coverage_max_elong": 6.0}
    failure_types = ("crack", "no_coverage", "missing_region")
    default_thresholds = {"low": 0.9, "medium": 0.7, "high": 0.5, "critical": 0.2}

    def run(self, ctx: BlockContext) -> None:
        p = self.params
        for r in ctx.slices:
            if not r.ok:
                self.skip(r)
                continue
            frac = float(r.stats.get("frac_fill", 0.0) or 0.0)
            if frac < p["min_frac"]:
                r.scores[self.name] = 1.0
                continue
            elong = float(r.stats.get("fill_elongation", 1.0) or 1.0)
            span = float(r.stats.get("fill_span", 0.0) or 0.0)
            sides = int(r.stats.get("fill_border_sides", 0) or 0)
            largest = float(r.stats.get("fill_largest_frac", frac) or frac)
            # a bulk region on the border is uncovered canvas even if mildly elongated (a 45 %-area triangle has
            # elongation ~3); only a genuinely thin band (elongation >= 6) at that size is still a fold / crack
            if sides >= 1 and largest >= p["no_coverage_min_frac"] and elong < p["no_coverage_max_elong"]:
                ftype = "no_coverage"
            elif elong >= p["elongation_crack"]:
                ftype = "crack"
            else:
                ftype = "missing_region"
            lo, hi = float(p["area_ref"]), float(p["area_full"])
            score = 1.0 - clip01(np.log(max(frac, lo) / lo) / np.log(hi / lo))
            self.emit(
                ctx, r, score,
                failure_type=ftype,
                coordinate={"bbox": r.stats.get("fill_bbox")},
                details={"frac_fill": frac, "n_components": r.stats.get("n_fill_components"), "largest_frac": largest, "elongation": elong, "span": span, "border_sides": sides, "fill_value": ctx.ds.fill_value},
            )


@register
class BrightnessJumpCheck(QCCheck):
    name = "brightness_jump"
    failure_type = "brightness_jump"
    description = "亮度突变：与参考切片（上一张可用切片；若其本身是突变则再往前一张）的平均灰度差，归一化到 [0,1] 灰度范围。"
    default_params = {"jump_ref": 0.2}

    def run(self, ctx: BlockContext) -> None:
        chain = reference_chain(ctx)
        pos = {id(r): i for i, r in enumerate(chain)}
        outlier: list[bool] = [False] * len(chain)
        for r in ctx.slices:
            if not r.ok:
                self.skip(r)
                continue
            k = pos.get(id(r), chain_position(chain, r))
            if k == 0:
                r.scores[self.name] = 1.0
                continue
            j = pick_reference(k, outlier)
            if j is None:
                self.skip(r, "reference_reset")
                continue
            ref = chain[j]
            d = abs(r.stats["mean"] - ref.stats["mean"])
            sev = self.emit(ctx, r, 1.0 - clip01(d / self.params["jump_ref"]), z_to=ref.z, details={"delta_mean": d, "mean": r.stats["mean"], "ref_mean": ref.stats["mean"], "ref_z": ref.z})
            if id(r) in pos:
                outlier[pos[id(r)]] = sev >= Severity.HIGH


@register
class ContrastDriftCheck(QCCheck):
    name = "contrast_drift"
    failure_type = "contrast_drift"
    description = (
        "对比度漂移：两个层面。切片级是灰度标准差相对本 block 中位数的 log2 偏移（单张异常）；"
        "block 级是标准差随 z 的 log2 斜率（整段单调漂移——这才是'漂移'本义，单张偏差检不出来）。"
    )
    default_params = {"log2_ref": 1.0, "trend_ref_per_100z": 0.5, "min_slices_for_trend": 8, "min_trend_r2": 0.5}

    def run(self, ctx: BlockContext) -> None:
        chain = reference_chain(ctx)
        in_chain = {id(r) for r in chain}
        stds = [r.stats["std"] for r in chain if r.stats["std"] > 0]
        ref = float(np.median(stds)) if stds else 0.0
        for r in ctx.slices:
            if id(r) not in in_chain:
                self.skip(r, "not_evaluated" if r.ok else None)
                continue
            std = r.stats["std"]
            if ref <= 0 or std <= 0:
                r.scores[self.name] = 1.0
                continue
            l2 = float(np.log2(std / ref))
            self.emit(ctx, r, 1.0 - clip01(abs(l2) / self.params["log2_ref"]), details={"std": std, "block_median_std": ref, "log2_ratio": l2})
        # block 级：整段的单调漂移。逐张偏差都小、但从头到尾一路变化的情况，只有这里能发现
        zs = np.array([r.z for r in chain if r.stats.get("std", 0) > 0], dtype=float)
        vals = np.array([r.stats["std"] for r in chain if r.stats.get("std", 0) > 0], dtype=float)
        if zs.size >= self.params["min_slices_for_trend"] and ref > 0:
            y = np.log2(vals / ref)
            slope, intercept = np.polyfit(zs, y, 1)
            resid = y - (slope * zs + intercept)
            ss_tot = float(((y - y.mean()) ** 2).sum())
            r2 = float(1.0 - (resid ** 2).sum() / ss_tot) if ss_tot > 1e-12 else 0.0
            per100 = float(slope * 100)
            score = 1.0 - clip01(abs(per100) / self.params["trend_ref_per_100z"])
            # 只在拟合确实解释了大部分变化时才算"漂移"；散乱的起伏不是趋势
            if r2 >= self.params["min_trend_r2"]:
                sev = self.severity(score)
                # 上限 MEDIUM：对比度整体漂移是提示，不该像"切片间互不相关"那样把整个 block 判死
                sev = min(sev, Severity.MEDIUM)
                if sev >= ctx.config.finding_min_severity:
                    self.emit_block(ctx, sev, score, details={"reason": "contrast_trend", "log2_per_100z": round(per100, 4),
                                                              "r2": round(r2, 3), "n_slices": int(zs.size), "block_median_std": ref,
                                                              "note": "整段对比度单调漂移；逐切片偏差可能都在容许范围内。仅作提示，不判 block 失败"})


@register
class ChargingArtifactCheck(QCCheck):
    name = "charging_artifact"
    failure_type = "charging"
    description = (
        "Charging artifact：电荷积累造成的条带与亮度斜坡。三个信号取最小值——"
        "沿扫描方向的行 / 列亮度条带（最主要的特征：整行整列偏亮或偏暗）、频谱各向异性（细条纹）、"
        "整幅亮度平面的倾斜。真实 charging 主要表现为条带，所以条带信号权重最大。"
    )
    default_params = {"band_ref": 0.10, "anisotropy_ref": 0.20, "ramp_ref": 0.15, "min_px": 64, "n_bins": 12, "baseline_frac": 0.12,
                      "k_mad": 3.0, "min_spread_band": 0.01, "min_spread_aniso": 0.02, "min_spread_ramp": 0.02}
    # 改成相对本 block 判断之后，干净切片的三个信号都接近基线，分数集中在 1.0 附近，
    # 所以档位回到接近默认（早期用绝对阈值时不得不整体下移到 0.50）
    default_thresholds = {"low": 0.75, "medium": 0.6, "high": 0.4, "critical": 0.2}

    def _band(self, t: np.ndarray) -> tuple[float, str, int]:
        """行 / 列亮度剖面相对其自身基线的最大偏离。charging 的典型表现是整行（或整列）偏亮。"""
        best = (0.0, "row", 0)
        for axis, name in ((1, "row"), (0, "col")):
            prof = t.mean(axis=axis).astype(np.float64)
            n = prof.size
            w = max(3, int(n * self.params["baseline_frac"]) | 1)  # 奇数窗口
            base = ndimage.median_filter(prof, size=w, mode="nearest")
            dev = np.abs(prof - base)
            i = int(np.argmax(dev))
            if float(dev[i]) > best[0]:
                best = (float(dev[i]), name, i)
        return best

    def _anisotropy(self, t: np.ndarray) -> tuple[float, float]:
        a = t - t.mean()
        if a.std() < 1e-8:
            return 0.0, 0.0
        F = np.abs(np.fft.fftshift(np.fft.fft2(a))) ** 2
        H, W = F.shape
        yy, xx = np.mgrid[0:H, 0:W]
        ry, rx = yy - H / 2.0, xx - W / 2.0
        rad = np.hypot(ry, rx)
        keep = (rad > max(H, W) * 0.04) & (rad < max(H, W) * 0.5)
        if keep.sum() < 16:
            return 0.0, 0.0
        ang = np.degrees(np.arctan2(ry[keep], rx[keep])) % 180.0
        n = int(self.params["n_bins"])
        hist = np.zeros(n)
        np.add.at(hist, np.clip((ang / 180.0 * n).astype(int), 0, n - 1), F[keep])
        tot = hist.sum()
        if tot <= 0:
            return 0.0, 0.0
        share = hist / tot
        peak = int(np.argmax(share))
        return float(np.clip((share[peak] - 1.0 / n) / (1.0 - 1.0 / n), 0.0, 1.0)), float((peak + 0.5) * 180.0 / n)

    def _ramp(self, t: np.ndarray) -> float:
        H, W = t.shape
        y = (np.arange(H) / max(H - 1, 1) * 2 - 1)[:, None] * np.ones((1, W))
        x = (np.arange(W) / max(W - 1, 1) * 2 - 1)[None, :] * np.ones((H, 1))
        M = np.stack([y.ravel(), x.ravel(), np.ones(H * W)], axis=1)
        try:
            c, *_ = np.linalg.lstsq(M, t.ravel().astype(np.float64), rcond=None)
        except np.linalg.LinAlgError:
            return 0.0
        return float(2.0 * np.hypot(c[0], c[1]))

    def run(self, ctx: BlockContext) -> None:
        p = self.params
        # 两遍：先量出本 block 每张切片的三个信号，再相对本 block 的典型值判断。
        # 绝对阈值不能跨数据集迁移——真实 EM 本来就有轻微的照明梯度（实测 mouse_30um 的斜坡中位数 0.176），
        # 按合成数据标定的绝对阈值会把这层正常梯度整片报成 charging（实测 400 张里报了 213 张）。
        measured = []
        for r in ctx.slices:
            if not r.ok or r.thumb is None:
                self.skip(r)
                continue
            t = r.thumb
            if min(t.shape) < p["min_px"]:
                self.skip(r, "thumbnail_too_small")
                continue
            band, band_axis, band_idx = self._band(t)
            aniso, angle = self._anisotropy(t)
            ramp = self._ramp(t)
            measured.append((r, t, band, band_axis, band_idx, aniso, angle, ramp))
        if not measured:
            return

        def baseline(vals: list[float]) -> tuple[float, float]:
            a = np.asarray(vals, dtype=float)
            med = float(np.median(a))
            mad = float(np.median(np.abs(a - med))) * 1.4826
            return med, mad

        b_med, b_mad = baseline([m[2] for m in measured])
        a_med, a_mad = baseline([m[5] for m in measured])
        r_med, r_mad = baseline([m[7] for m in measured])
        k = float(p["k_mad"])
        for r, t, band, band_axis, band_idx, aniso, angle, ramp in measured:
            ex_band = max(0.0, band - b_med - k * max(b_mad, p["min_spread_band"]))
            ex_aniso = max(0.0, aniso - a_med - k * max(a_mad, p["min_spread_aniso"]))
            ex_ramp = max(0.0, ramp - r_med - k * max(r_mad, p["min_spread_ramp"]))
            s_band = 1.0 - clip01(ex_band / p["band_ref"])
            s_aniso = 1.0 - clip01(ex_aniso / p["anisotropy_ref"])
            s_ramp = 1.0 - clip01(ex_ramp / p["ramp_ref"])
            score = min(s_band, s_aniso, s_ramp)
            limited = "band" if score == s_band else ("anisotropy" if score == s_aniso else "ramp")
            bbox = None
            if limited == "band" and score < 1.0:
                sy, sx = ctx.scale_yx
                if band_axis == "row":
                    bbox = [0, int(band_idx * sy), int(t.shape[1] * sx), int((band_idx + 1) * sy)]
                else:
                    bbox = [int(band_idx * sx), 0, int((band_idx + 1) * sx), int(t.shape[0] * sy)]
            self.emit(ctx, r, score, coordinate={"bbox": bbox},
                      details={"band_deviation": round(band, 4), "band_axis": band_axis, "band_index": band_idx,
                               "anisotropy": round(aniso, 4), "dominant_angle_deg": round(angle, 1),
                               "brightness_ramp": round(ramp, 4), "limited_by": limited,
                               "block_median": {"band": round(b_med, 4), "anisotropy": round(a_med, 4), "ramp": round(r_med, 4)},
                               "excess_over_block": {"band": round(ex_band, 4), "anisotropy": round(ex_aniso, 4), "ramp": round(ex_ramp, 4)},
                               "note": "三个信号都相对本 block 的典型值判断：整卷共有的照明梯度不算 charging，异常突出的才算"})


@register
class ContaminationCheck(QCCheck):
    name = "contamination"
    failure_type = "contamination"
    description = (
        "污染：切片表面的灰尘 / 冰晶 / 碎屑，表现为高对比的孤立团块。做形态学开运算取出比组织结构大得多的"
        "亮暗斑块，按面积占比打分，并给出最大斑块的包围盒。"
    )
    # 只靠"灰度极端"会把线粒体、髓鞘这类深色组织当成污染（实测 128 张里误报 76 张）。
    # 真正的判据是"极端 **且内部平坦**"：污染斑块是均匀的一块，组织再深也有纹理。
    default_params = {"area_ref": 0.02, "min_frac": 0.0008, "sigma_k": 3.5, "open_iter": 2, "min_px": 64,
                      "max_blob_frac": 0.25, "flat_quantile": 0.15, "min_blob_px": 12}

    def run(self, ctx: BlockContext) -> None:
        p = self.params
        for r in ctx.slices:
            if not r.ok or r.thumb is None:
                self.skip(r)
                continue
            t = r.thumb
            if min(t.shape) < p["min_px"]:
                self.skip(r, "thumbnail_too_small")
                continue
            med = float(np.median(t))
            mad = float(np.median(np.abs(t - med))) * 1.4826
            if mad < 1e-6:
                r.scores[self.name] = 1.0
                continue
            k = float(p["sigma_k"])
            extreme = (t > med + k * mad) | (t < med - k * mad)
            # 局部平坦度：3x3 邻域标准差。污染斑块内部接近常数，组织即使很深也有纹理
            local_var = ndimage.uniform_filter(t.astype(np.float64) ** 2, 3) - ndimage.uniform_filter(t.astype(np.float64), 3) ** 2
            local_std = np.sqrt(np.clip(local_var, 0, None))
            flat_cut = float(np.quantile(local_std, p["flat_quantile"]))
            flat = local_std <= flat_cut
            blobs = ndimage.binary_opening(extreme & flat, structure=np.ones((3, 3)), iterations=int(p["open_iter"]))
            # 只保留成块的（太小的是噪点）
            lab0, n0 = ndimage.label(blobs)
            if n0:
                sz = ndimage.sum(blobs, lab0, index=np.arange(1, n0 + 1))
                keep = {i + 1 for i, v in enumerate(sz) if v >= p["min_blob_px"]}
                blobs = np.isin(lab0, list(keep)) if keep else np.zeros_like(blobs)
            frac = float(blobs.mean())
            if frac < p["min_frac"] or frac > p["max_blob_frac"]:
                # 超过 max_blob_frac 说明整幅都被判成异常，那是对比度问题不是污染
                r.scores[self.name] = 1.0
                r.note(self.name, "no_blob" if frac < p["min_frac"] else "too_widespread_for_contamination")
                continue
            lab, n = ndimage.label(blobs)
            sizes = ndimage.sum(blobs, lab, index=np.arange(1, n + 1)) if n else np.array([])
            bbox = None
            if n:
                big = int(np.argmax(sizes)) + 1
                bbox = bbox_from_mask(lab == big, ctx.scale_yx)
            self.emit(ctx, r, 1.0 - clip01(frac / p["area_ref"]), coordinate={"bbox": bbox},
                      details={"blob_frac": round(frac, 5), "n_blobs": int(n),
                               "largest_blob_frac": round(float(sizes.max() / blobs.size), 5) if n else 0.0,
                               "median": round(med, 4), "mad": round(mad, 5)})
