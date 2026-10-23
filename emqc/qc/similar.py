"""找相似切片：用 QC 已经算好的数值当特征，不跑任何模型。

每张切片一个特征向量，分两部分：
  - 原始统计量（STAT_FEATURES）：亮度、对比度、清晰度、熵、饱和、填充区形状……与判定无关，只描述"图像长什么样"
  - 检查分数（各检查项 0–1 分）：描述"像哪一类问题"
两部分各自做稳健标准化（中位数 / IQR），再各占一半权重，用欧氏距离找最近邻。

同一套特征给两处用：浏览器里的「找相似」（/api/v1/qc/similar，特征来自数据库里的 qc_slices），
和标定集评测（scripts/calib_similarity_eval.py，特征来自直接对图片跑的 QC）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# (stats key, transform). log 变换给跨数量级的量（清晰度、Laplacian 方差）
STAT_FEATURES: list[tuple[str, str]] = [
    ("mean", "lin"), ("std", "lin"), ("p1", "lin"), ("p50", "lin"), ("p99", "lin"),
    ("frac_low_sat", "lin"), ("frac_high_sat", "lin"), ("frac_mode", "lin"), ("entropy_bits", "lin"),
    ("lapvar", "log"), ("sharpness", "log"),
    ("frac_fill", "lin"), ("n_fill_components", "lin"), ("fill_largest_frac", "lin"), ("fill_elongation", "log"), ("fill_span", "lin"),
]
# 给人看的名字（数据分布图的坐标轴说明用）
STAT_ZH = {"mean": "平均亮度", "std": "对比度", "p1": "暗部亮度", "p50": "中位亮度", "p99": "亮部亮度", "frac_low_sat": "过暗像素比例", "frac_high_sat": "过亮像素比例",
           "frac_mode": "单一灰度占比", "entropy_bits": "信息量（熵）", "lapvar": "细节量", "sharpness": "清晰度", "frac_fill": "无数据区比例",
           "n_fill_components": "无数据区块数", "fill_largest_frac": "最大无数据区", "fill_elongation": "无数据区细长度", "fill_span": "无数据区跨度"}
CHECK_ZH = {"blank_slice": "空白切片", "missing_slice": "缺失切片", "corrupt_slice": "损坏切片", "severe_blur": "严重模糊", "saturation": "过曝 / 饱和",
            "crack": "裂缝 / 褶皱", "brightness_jump": "亮度突变", "contrast_drift": "对比度漂移", "charging_artifact": "荷电伪影", "contamination": "表面污染",
            "slice_jump": "切片跳变", "local_misalignment": "局部错位", "global_misalignment": "整体错位", "z_order_error": "顺序错误",
            "local_deformation": "局部形变", "section_deformation": "切片形变"}
# 没有填充区的切片这些字段不存在：等价于 0（没有黑块）
STAT_ABSENT_IS_ZERO = {"frac_fill", "n_fill_components", "fill_largest_frac", "fill_elongation", "fill_span"}


def _tf(v, how: str) -> float:
    if v is None:
        return math.nan
    v = float(v)
    if how == "log":
        return math.log10(max(v, 0.0) + 1e-6)
    return v


@dataclass
class FeatureSpace:
    """Feature matrix of a pool of slices, normalized once; query with `nearest`."""

    keys: list                      # one identifier per row (caller-defined)
    stat_names: list[str]
    score_names: list[str]
    X: np.ndarray                   # normalized, weighted features (n, d)
    meta: list = field(default_factory=list)

    def nearest(self, i: int, k: int = 10, exclude=None) -> list[tuple[int, float]]:
        """k nearest rows to row i (excluding i itself and rows for which exclude(j) is true): [(row, distance)]."""
        d = np.sqrt(((self.X - self.X[i]) ** 2).sum(axis=1))
        d[i] = np.inf
        order = np.argsort(d, kind="stable")
        out = []
        for j in order:
            if not np.isfinite(d[j]):
                break
            if exclude is not None and exclude(int(j)):
                continue
            out.append((int(j), float(d[j])))
            if len(out) >= k:
                break
        return out


def _robust(cols: np.ndarray) -> np.ndarray:
    """Per column: impute NaN with the median, scale by IQR (fallback std, then 1), clip to +-6."""
    X = cols.astype(np.float64).copy()
    for c in range(X.shape[1]):
        col = X[:, c]
        ok = np.isfinite(col)
        med = float(np.median(col[ok])) if ok.any() else 0.0
        col[~ok] = med
        q1, q3 = np.percentile(col, [25, 75])
        scale = (q3 - q1) or float(col.std()) or 1.0
        X[:, c] = np.clip((col - med) / scale, -6, 6)
    return X


def build_space(records: list[tuple[dict, dict]], keys: list, *, use_stats: bool = True, score_names: list[str] | None = None, meta: list | None = None) -> FeatureSpace:
    """records: [(stats, scores)] per slice. score_names=None -> every score key seen; [] -> no scores (statistics only)."""
    if score_names is None:
        seen: set[str] = set()
        for _, sc in records:
            seen.update(k for k, v in (sc or {}).items() if not k.startswith("_"))
        score_names = sorted(seen)
    blocks = []
    stat_names = [k for k, _ in STAT_FEATURES] if use_stats else []
    if use_stats:
        S = np.array([[(0.0 if (k in STAT_ABSENT_IS_ZERO and k not in (st or {})) else _tf((st or {}).get(k), how)) for k, how in STAT_FEATURES] for st, _ in records], dtype=np.float64)
        S = _robust(S)
        keep = S.std(axis=0) > 1e-9          # constant columns carry no information
        S, stat_names = S[:, keep], [n for n, k in zip(stat_names, keep) if k]
        if S.shape[1]:
            blocks.append(S / math.sqrt(S.shape[1]))
    if score_names:
        # a score that was not computed (None) means "no evidence of this problem": treat as 1 (clean)
        C = np.array([[1.0 if (sc or {}).get(n) is None else float(sc[n]) for n in score_names] for _, sc in records], dtype=np.float64)
        # scores are already on [0, 1]; scale so one check going from 1 to 0 is a big move
        keep = C.std(axis=0) > 1e-9
        C, score_names = C[:, keep], [n for n, k in zip(score_names, keep) if k]
        if C.shape[1]:
            blocks.append(C * 3.0 / math.sqrt(C.shape[1]))
    X = np.hstack(blocks) if blocks else np.zeros((len(records), 1))
    return FeatureSpace(keys=list(keys), stat_names=stat_names, score_names=score_names, X=X, meta=list(meta or []))
