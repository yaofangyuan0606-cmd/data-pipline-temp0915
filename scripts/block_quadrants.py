"""Split a block (em.npy [+ seg.npy], (rows, cols, z)) into fixed-size sub-blocks, one per tile position, so the
annotation page can open a single quadrant and flip through z with the segmentation overlaid.

The source block is read-only. Output: <out>/<block>_<size>/<block>_y0000_x0512/{em.npy, seg.npy, meta.json}.
Usage:  python scripts/block_quadrants.py BLOCK_DIR OUT_DIR [--size 512]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("block")
    ap.add_argument("out")
    ap.add_argument("--size", type=int, default=512)
    a = ap.parse_args()
    src, S = Path(a.block).resolve(), a.size
    out = Path(a.out).resolve() / f"{src.name}_{S}"
    if out.is_relative_to(src):
        sys.exit("refusing to write inside the source block")
    em = np.load(src / "em.npy", mmap_mode="r")
    seg = np.load(src / "seg.npy", mmap_mode="r") if (src / "seg.npy").exists() else None
    meta = json.load(open(src / "meta.json")) if (src / "meta.json").exists() else {}
    H, W, Z = em.shape
    if H % S or W % S:
        sys.exit(f"{H}x{W} is not a multiple of {S}")
    n = 0
    for y0 in range(0, H, S):
        for x0 in range(0, W, S):
            d = out / f"{src.name}_y{y0:04d}_x{x0:04d}"   # block ids must be unique across the annotation roots
            d.mkdir(parents=True, exist_ok=True)
            np.save(d / "em.npy", np.ascontiguousarray(em[y0:y0 + S, x0:x0 + S, :]))
            if seg is not None:
                np.save(d / "seg.npy", np.ascontiguousarray(seg[y0:y0 + S, x0:x0 + S, :]))
            g = dict(meta.get("geometry", {}))
            g["size"] = {"rows": S, "cols": S, "z": Z}
            g["offset_in_parent"] = {"y0": y0, "x0": x0}
            json.dump({**meta, "dataset": {**meta.get("dataset", {}), "id": f"{meta.get('dataset', {}).get('id', src.name)}-q"},
                       "geometry": g, "parent_block": str(src), "quadrant": {"index": (y0 // S) * (W // S) + (x0 // S), "y0": y0, "x0": x0, "size": S}},
                      open(d / "meta.json", "w"), indent=1, ensure_ascii=False)
            n += 1
    print(f"{src.name}: {H}x{W}x{Z} -> {n} sub-blocks of {S}x{S}x{Z} under {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
