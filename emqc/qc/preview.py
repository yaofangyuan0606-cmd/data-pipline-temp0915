"""Preview generation. Only thumbnails are ever written; full-resolution images are never re-saved."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .base import Severity

SEVERITY_RGB = {
    Severity.NONE: (76, 175, 80),
    Severity.LOW: (255, 213, 79),
    Severity.MEDIUM: (255, 152, 0),
    Severity.HIGH: (244, 67, 54),
    Severity.CRITICAL: (136, 14, 79),
}


def downsample(img: np.ndarray, max_px: int, dtype_max: float) -> np.ndarray:
    """Block-mean downsample to <= max_px on the long side. Returns float32 in [0, 1]."""
    H, W = img.shape
    f = max(1, int(np.ceil(max(H, W) / max_px)))
    if f > 1:
        Hc, Wc = (H // f) * f, (W // f) * f
        img = img[:Hc, :Wc].reshape(Hc // f, f, Wc // f, f).mean(axis=(1, 3), dtype=np.float32)
    return np.asarray(img, dtype=np.float32) / float(dtype_max)


def to_uint8(thumb: np.ndarray) -> np.ndarray:
    return np.clip(thumb * 255.0 + 0.5, 0, 255).astype(np.uint8)


def save_thumb(thumb: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(to_uint8(thumb), mode="L").save(path, optimize=True)
    return path


def save_montage(records, path: Path, tile: int = 96, cols: int = 8) -> Path | None:
    """Grid of slice thumbnails; each tile carries a severity-coloured bar and its z."""
    recs = list(records)
    if not recs:
        return None
    rows = int(np.ceil(len(recs) / cols))
    canvas = Image.new("RGB", (cols * tile, rows * tile), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    for i, r in enumerate(recs):
        x0, y0 = (i % cols) * tile, (i // cols) * tile
        if r.thumb is not None:
            im = Image.fromarray(to_uint8(r.thumb), mode="L").convert("RGB")
            im.thumbnail((tile - 2, tile - 2))
            canvas.paste(im, (x0 + 1, y0 + 1))
        else:
            draw.rectangle([x0 + 1, y0 + 1, x0 + tile - 2, y0 + tile - 2], fill=(60, 60, 60))
            draw.text((x0 + 8, y0 + tile // 2 - 6), r.status.upper(), fill=(230, 230, 230))
        draw.rectangle([x0, y0, x0 + tile - 1, y0 + 4], fill=SEVERITY_RGB[r.max_severity])
        draw.text((x0 + 4, y0 + 6), f"z{r.z}", fill=(255, 255, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, optimize=True)
    return path


def block_mean(img: np.ndarray, f: int, dtype_max: float) -> np.ndarray:
    """Block-mean downsample by an integer factor. Returns float32 in [0, 1]."""
    H, W = img.shape
    f = max(1, int(f))
    if f > 1:
        Hc, Wc = (H // f) * f, (W // f) * f
        img = img[:Hc, :Wc].reshape(Hc // f, f, Wc // f, f).mean(axis=(1, 3), dtype=np.float32)
    return np.asarray(img, dtype=np.float32) / float(dtype_max)
