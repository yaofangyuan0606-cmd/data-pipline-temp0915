"""Render before / after pictures for a SAM-filled block, so people can see what the pre-labelling actually added.

SAM never touches the electron-microscopy image; what changes is the label layer. So each picture is three panels of
the same section: the bare EM, the labels as they were delivered, and the labels after the SAM masks were merged in,
with everything SAM contributed drawn in a way you cannot confuse with the delivered labels (brighter fill plus a
white outline). The caption carries the numbers for that section.

    python scripts/render_sam_compare.py SOURCE_BLOCK MERGED_BLOCK [--out var/sam_compare] [--n 10] [--zs 0,5,11]
"""
from __future__ import annotations

import argparse
import colorsys
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

FONTS = ["/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
         "/System/Library/Fonts/Supplemental/Songti.ttc"]
INK = (236, 240, 244)
DIM = (150, 158, 168)
BG = (22, 24, 28)
SAM_MARK = (255, 214, 10)


def font(size: int):
    for f in FONTS:
        if Path(f).exists():
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                continue
    return ImageFont.load_default()


def color_of(v: int) -> np.ndarray:
    h = (int(v) * 2654435761) % 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, 0.55, 0.62)
    return np.array([r * 255, g * 255, b * 255], np.float32)


def paint(em: np.ndarray, seg: np.ndarray, highlight: np.ndarray | None = None, alpha=0.45) -> np.ndarray:
    """EM in grey with the labels on top; `highlight` pixels get a stronger fill and a white outline."""
    out = np.repeat(em[:, :, None], 3, axis=2).astype(np.float32)
    for lid in np.unique(seg):
        if lid == 0:
            continue
        m = seg == lid
        a = alpha if highlight is None else np.where(highlight[m].any(), alpha, alpha)
        out[m] = a * color_of(int(lid)) + (1 - a) * out[m]
    if highlight is not None and highlight.any():
        for lid in np.unique(seg[highlight]):
            if lid == 0:
                continue
            m = (seg == lid) & highlight
            out[m] = 0.62 * color_of(int(lid)) + 0.38 * out[m]
        edge = highlight ^ ndimage.binary_erosion(highlight, np.ones((3, 3), bool))
        out[edge] = [255, 255, 255]
    return np.clip(out, 0, 255).astype(np.uint8)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("merged")
    ap.add_argument("--out", default="var/sam_compare")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--zs", default=None, help="comma separated section numbers instead of an even spread")
    a = ap.parse_args()

    src, mrg = Path(a.source), Path(a.merged)
    em = np.load(src / "em.npy", mmap_mode="r")
    before = np.load(src / "seg.npy", mmap_mode="r")
    after = np.load(mrg / "seg.npy", mmap_mode="r")
    meta = json.loads((mrg / "meta.json").read_text()) if (mrg / "meta.json").exists() else {}
    touched = sorted(int(z) for z in meta.get("per_section", {}))
    if not touched:
        sys.exit("the merged block's meta.json lists no sections with masks")

    if a.zs:
        zs = [int(x) for x in a.zs.split(",")]
    else:
        zs = [touched[round(i * (len(touched) - 1) / max(1, a.n - 1))] for i in range(a.n)]
        zs = sorted(dict.fromkeys(zs))

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    H, W = em.shape[:2]
    gap, pad, head, foot = 10, 14, 52, 46
    f_title, f_panel, f_small = font(22), font(17), font(14)

    print(f"{len(zs)} sections -> {out}")
    for z in zs:
        e = np.ascontiguousarray(em[:, :, z])
        b = np.ascontiguousarray(before[:, :, z])
        m = np.ascontiguousarray(after[:, :, z])
        added = (b == 0) & (m > 0)
        n_added = len(np.unique(m[added])) if added.any() else 0
        cov_b, cov_a = float((b > 0).mean()), float((m > 0).mean())

        panels = [("电镜原图（SAM 前后完全相同）", np.repeat(e[:, :, None], 3, axis=2).astype(np.uint8), None),
                  (f"处理前 · 交付标签  覆盖 {cov_b:.1%}", paint(e, b), None),
                  (f"处理后 · 加入 SAM  覆盖 {cov_a:.1%}", paint(e, m, added), None)]

        cw = W * 3 + gap * 2 + pad * 2
        ch = H + head + foot + pad
        canvas = Image.new("RGB", (cw, ch), BG)
        d = ImageDraw.Draw(canvas)
        d.text((pad, 12), f"z{z:04d}", font=f_title, fill=INK)
        d.text((pad + 78, 18), f"{mrg.name}", font=f_small, fill=DIM)
        for i, (name, arr, _) in enumerate(panels):
            x = pad + i * (W + gap)
            canvas.paste(Image.fromarray(arr), (x, head))
            d.text((x, head - 22), name, font=f_panel, fill=INK if i else DIM)
        y = head + H + 12
        d.text((pad, y), f"SAM 新增 {n_added} 个色块，{int(added.sum())} 像素，覆盖 {cov_b:.1%} → {cov_a:.1%}"
                         f"（黄白描边处为 SAM 补的区域；交付标签一个像素都没被改写）", font=f_small, fill=DIM)
        p = out / f"sam_compare_z{z:04d}.png"
        canvas.save(p)
        print(f"  z{z:04d}: +{n_added} 块 / {int(added.sum())}px, {cov_b:.1%} -> {cov_a:.1%}  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
