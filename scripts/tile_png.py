"""Cut every PNG in a directory tree into fixed-size tiles, writing to a separate output directory.

The source is opened read-only and never modified. Tiles are lossless (PNG -> PNG, same mode, no resampling).

Layout (default "quadrant"): one folder per tile position, the section files inside keep their original names, so
sorting a folder gives the z sequence of that position — open it and flip through to follow cells from section to
section:   <sub>/y0000_x0512/z0000.png, z0001.png, ...
Layout "flat": everything in one folder, named <stem>_y<row0>_x<col0>_s<size>.png.
manifest.csv lists every tile ordered by position, then z.

Usage:  python scripts/tile_png.py SRC_DIR OUT_DIR [--size 512] [--layout quadrant|flat] [--verify]
    --verify checksums every source file before and after, re-reads every tile, stitches the tiles of each source
    image back together and checks the result is pixel-identical to the source (proves the cut is lossless).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--layout", choices=["quadrant", "flat"], default="quadrant")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    src, out, S = Path(a.src).resolve(), Path(a.out).resolve(), a.size
    if out == src or out.is_relative_to(src):
        sys.exit("refusing to write inside the source directory")
    files = sorted(p for p in src.rglob("*.png") if p.is_file())
    if not files:
        sys.exit(f"no PNG under {src}")
    sums_before = {p: md5(p) for p in files} if a.verify else {}
    rows, n_tiles = [], 0
    for p in files:
        rel = p.relative_to(src)
        with Image.open(p) as im:
            im.load()
            W, H = im.size
            if W % S or H % S:
                print(f"skip {rel}: {W}x{H} is not a multiple of {S}", file=sys.stderr)
                continue
            dst_dir = out / rel.parent
            dst_dir.mkdir(parents=True, exist_ok=True)
            ny, nx = H // S, W // S
            for y0 in range(0, H, S):
                for x0 in range(0, W, S):
                    pos = f"y{y0:04d}_x{x0:04d}"
                    q = (y0 // S) * nx + (x0 // S)                     # position index, row-major (0 = top-left)
                    if a.layout == "quadrant":
                        (dst_dir / pos).mkdir(exist_ok=True)
                        tile_rel = rel.parent / pos / p.name
                    else:
                        tile_rel = rel.parent / f"{p.stem}_{pos}_s{S}.png"
                    im.crop((x0, y0, x0 + S, y0 + S)).save(out / tile_rel, format="PNG", optimize=False)
                    rows.append({"position": pos, "q": q, "z": p.stem, "source": str(rel), "tile": str(tile_rel), "y0": y0, "x0": x0, "size": S, "mode": im.mode})
                    n_tiles += 1
    rows.sort(key=lambda r: (str(Path(r["source"]).parent), r["q"], r["z"]))   # position first, then z: the browsing order
    with open(out / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["position", "q", "z", "source", "tile", "y0", "x0", "size", "mode"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(files)} source images -> {n_tiles} tiles of {S}x{S} under {out}")
    if not a.verify:
        return 0
    changed = [p for p in files if md5(p) != sums_before[p]]
    bad = []
    by_src: dict[str, list[dict]] = {}
    for r in rows:
        by_src.setdefault(r["source"], []).append(r)
    for rel, tiles in by_src.items():
        orig = np.asarray(Image.open(src / rel))
        back = np.zeros_like(orig)
        for t in tiles:
            y0, x0 = t["y0"], t["x0"]
            back[y0:y0 + S, x0:x0 + S] = np.asarray(Image.open(out / t["tile"]))
        if not np.array_equal(orig, back):
            bad.append(rel)
    print(f"verify: source files changed: {len(changed)} | images that do not stitch back identically: {len(bad)}")
    return 1 if (changed or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
