"""Pack a crop delivery directory (per-crop PNG) into contiguous .npy arrays for training.

Why: PNG is one file per crop — convenient to inspect, but every sample costs a decode (~160 crops/s per worker).
A memory-mapped .npy is ~12x faster and lets a 3-D sampler slice consecutive sections without decoding anything.
Pixels are identical; this is a container change, not a data change.

Writes into <dir>/npy/:
  em.npy     (N, S, S) uint8            — the crops, in index.csv order
  seg.npy    (N, S, S) uint16 or uint32 — label ids already decoded from the RGB packing
  index.csv  one row per array index: i, z, y0, x0, size, case, seam  (+ the source filenames)
  volumes.csv  runs of >= min_run consecutive z at one window position -> (i0, i1) ranges usable as 3-D volumes

Usage:  python scripts/pack_npy.py DELIVERY_DIR [--min-run 8]
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def decode_ids(p: Path) -> np.ndarray:
    a = np.asarray(Image.open(p))
    if a.ndim == 2:
        return a.astype(np.uint32)
    a = a.astype(np.uint32)
    return (a[..., 0] << 16) | (a[..., 1] << 8) | a[..., 2]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir")
    ap.add_argument("--min-run", type=int, default=8, help="shortest run of consecutive z to record as a 3-D volume")
    a = ap.parse_args()
    d = Path(a.dir)
    rows = list(csv.DictReader(open(d / "manifest.csv")))
    if not rows:
        print(f"empty manifest in {d}", file=sys.stderr)
        return 1
    # stable order: z, then window position — so consecutive z at one position are adjacent in the array
    rows.sort(key=lambda r: (int(r["y0"]), int(r["x0"]), int(r["z"])))
    S = int(rows[0]["size"])
    N = len(rows)
    out = d / "npy"
    out.mkdir(exist_ok=True)

    has_seg = bool(rows[0].get("seg"))
    # one pass to find the largest label id, so seg gets the smallest dtype that holds it
    dtype = None
    if has_seg:
        mx = 0
        for r in rows:
            mx = max(mx, int(decode_ids(d / r["seg"]).max()))
        dtype = np.uint16 if mx < 65536 else np.uint32
        print(f"largest label id {mx} -> seg dtype {np.dtype(dtype).name}")

    em = np.lib.format.open_memmap(out / "em.npy", mode="w+", dtype=np.uint8, shape=(N, S, S))
    seg = np.lib.format.open_memmap(out / "seg.npy", mode="w+", dtype=dtype, shape=(N, S, S)) if has_seg else None
    with open(out / "index.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["i", "z", "y0", "x0", "size", "case", "seam", "em_png", "seg_png"])
        for i, r in enumerate(rows):
            em[i] = np.asarray(Image.open(d / r["em"]))
            if seg is not None:
                seg[i] = decode_ids(d / r["seg"])
            w.writerow([i, r["z"], r["y0"], r["x0"], r["size"], r["case"], r["seam"], r["em"], r.get("seg", "")])
    em.flush()
    if seg is not None:
        seg.flush()

    # runs of consecutive z at one window position are contiguous in the array by construction
    pos = defaultdict(list)
    for i, r in enumerate(rows):
        pos[(int(r["y0"]), int(r["x0"]))].append((int(r["z"]), i))
    vols = []
    for (y, x), lst in sorted(pos.items()):
        run = [lst[0]]
        for z, i in lst[1:]:
            if z == run[-1][0] + 1 and i == run[-1][1] + 1:
                run.append((z, i))
            else:
                if len(run) >= a.min_run:
                    vols.append((y, x, run[0][0], run[-1][0], run[0][1], run[-1][1] + 1))
                run = [(z, i)]
        if len(run) >= a.min_run:
            vols.append((y, x, run[0][0], run[-1][0], run[0][1], run[-1][1] + 1))
    vols.sort(key=lambda v: v[5] - v[4], reverse=True)
    with open(out / "volumes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["y0", "x0", "z_start", "z_end", "i0", "i1", "n_sections"])
        for v in vols:
            w.writerow([*v, v[5] - v[4]])

    mb = lambda p: p.stat().st_size / 1e6
    print(f"{d.name}: {N} crops of {S}²  ->  em.npy {mb(out/'em.npy'):.0f} MB"
          + (f", seg.npy {mb(out/'seg.npy'):.0f} MB" if seg is not None else "")
          + f"  |  3-D volumes >= {a.min_run} sections: {len(vols)}"
          + (f", longest {vols[0][5]-vols[0][4]} sections" if vols else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
