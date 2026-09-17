"""Cut clean, black-free, uniform-size training crops out of a directory of EM sections.

Cases handled:
  1. sections with black (fill-value) cuts / missing tiles  -> keep every window that stays clear of black
  2. sections that are uniform (no content, e.g. a 128-gray placeholder) -> dropped, listed in the report
  3. montage sections with uneven tile brightness           -> `--avoid-seams`: the section's montage tilt is estimated,
     the section is de-rotated, straight tile seams are detected there (a brightness step that keeps its sign over
     >= 90 % of a 192-px window and runs >= 200 px), the seam mask is rotated back and treated like black: no window
     may cross a seam, so every crop lies inside one tile. As a second net a window is accepted only if the
     strongest brightness step inside it (`seam`, axis-aligned and de-rotated) is <= `--max-seam`.
     Pixels are never modified.

Windows never overlap, are placed greedily (grid-aligned first, so a clean section yields the same windows in
every z -> stackable in 3-D) and never contain a fill pixel (`--margin` extra pixels away from black).
Labels may live at a different mip than the EM (EM mip0 4096², labels mip1 2048²): the integer scale is detected
from the first pair and label crops are resampled (nearest) to the EM window size, so em/ and seg/ pair 1:1.

Usage:
  python scripts/crop_clean.py SRC_DIR OUT_DIR [--labels LABEL_DIR] [--size 1024] [--margin 8] [--avoid-seams]
                               [--max-seam 0.15] [--min-fill-px 64] [--fill-value 0] [--workers 8]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = None
_Z = re.compile(r"(\d+)(?!.*\d)")


def z_of(p: Path) -> int:
    m = _Z.search(p.stem)
    return int(m.group(1)) if m else -1


# ----------------------------------------------------------------------------- fill (black) regions
def fill_mask(img: np.ndarray, fill_value: int, min_px: int, margin: int) -> np.ndarray:
    """True where a crop must not go: large connected regions of exactly `fill_value`, grown by `margin`."""
    z = img == fill_value
    if not z.any():
        return z
    lab, n = ndimage.label(z, structure=np.ones((3, 3), int))
    sizes = ndimage.sum(np.ones_like(z, dtype=np.int64), lab, index=np.arange(1, n + 1))
    keep = np.isin(lab, np.flatnonzero(sizes >= min_px) + 1)  # drop isolated dark pixels inside tissue
    if margin > 0 and keep.any():
        keep = ndimage.binary_dilation(keep, iterations=margin)
    return keep


# ----------------------------------------------------------------------------- window packing
def integral(m: np.ndarray) -> np.ndarray:
    s = np.zeros((m.shape[0] + 1, m.shape[1] + 1), dtype=np.int64)
    s[1:, 1:] = np.cumsum(np.cumsum(m.astype(np.int64), axis=0), axis=1)
    return s


def window_sum(S: np.ndarray, y: int, x: int, s: int) -> int:
    return int(S[y + s, x + s] - S[y, x + s] - S[y + s, x] + S[y, x])


def pack(invalid: np.ndarray, size: int) -> list[tuple[int, int, int]]:
    """Greedy non-overlapping packing of fully-valid windows; grid-aligned positions first."""
    H, W = invalid.shape
    S = integral(invalid)
    occupied = np.zeros((H, W), dtype=bool)
    out: list[tuple[int, int, int]] = []
    stride = max(size // 8, 64)
    cands = [(y, x) for y in range(0, H - size + 1, stride) for x in range(0, W - size + 1, stride)]
    cands.sort(key=lambda p: (not (p[0] % size == 0 and p[1] % size == 0), p[0] % size + p[1] % size, p))
    for y, x in cands:
        if window_sum(S, y, x, size) or occupied[y : y + size, x : x + size].any():
            continue
        occupied[y : y + size, x : x + size] = True
        out.append((y, x, size))
    return out


def _band_diff(f: np.ndarray, axis: int, w: int, gap: int) -> np.ndarray:
    """mean of the band [i+gap, i+gap+w) minus mean of [i-gap-w, i-gap) along `axis` (NaN-aware)."""
    ok = np.isfinite(f).astype(np.float32)
    g = np.nan_to_num(f)

    def band_mean(shift):
        sm = np.roll(ndimage.uniform_filter1d(g, w, axis=axis, mode="constant"), shift, axis=axis)
        n = np.roll(ndimage.uniform_filter1d(ok, w, axis=axis, mode="constant"), shift, axis=axis)
        return np.where(n > 0.95, sm / np.maximum(n, 1e-6), np.nan)

    half = w // 2
    return band_mean(-(gap + half)) - band_mean(gap + half)


def seam_lines(img: np.ndarray, fill: np.ndarray, win: int = 192, min_cons: float = 0.9, min_step: float = 12.0,
               min_run: int = 200, w: int = 16, gap: int = 6) -> np.ndarray:
    """Axis-aligned straight seams: a column (row) where the step between the bands on its two sides keeps one sign
    in >= min_cons of a `win`-long window and averages >= min_step gray levels, running >= min_run px; the plateau
    is thinned to its centre line."""
    f = img.astype(np.float32)
    f[fill] = np.nan
    out = np.zeros(img.shape, bool)
    for axis, along in ((1, 0), (0, 1)):
        D = _band_diff(f, axis, w, gap)
        valid = np.isfinite(D).astype(np.float32)
        Dz = np.nan_to_num(D)
        uf = lambda a, n: ndimage.uniform_filter1d(a.astype(np.float32), n, axis=along, mode="constant")
        pos, neg, cnt = uf((Dz > 0) & (valid > 0), win), uf((Dz < 0) & (valid > 0), win), uf(valid, win)
        cons = np.where(cnt > 0.9, np.maximum(pos, neg) / np.maximum(cnt, 1e-6), 0)
        step = np.abs(uf(Dz, win))
        plateau = (cons >= min_cons) & (step >= min_step)
        struct = np.ones((min_run, 1), bool) if axis == 1 else np.ones((1, min_run), bool)
        plateau = ndimage.binary_opening(plateau, structure=struct)
        S = np.where(plateau, np.abs(uf(Dz, 32)), 0)
        out |= plateau & (S >= ndimage.maximum_filter1d(S, 2 * (w + gap) + 1, axis=axis)) & (S > 0)
    return out


def seams_tilt_aware(img: np.ndarray, fill: np.ndarray, tilt_deg: float) -> np.ndarray:
    """Detect seams in the de-rotated frame (where they are axis-aligned) and map the mask back."""
    if abs(tilt_deg) < 0.25:
        return seam_lines(img, fill)
    H, W = img.shape
    med = float(np.median(img[~fill])) if (~fill).any() else 0.0
    g = np.where(fill, med, img.astype(np.float32))
    r = ndimage.rotate(g, tilt_deg, reshape=True, order=1, mode="constant", cval=med)
    rf = ndimage.rotate(fill.astype(np.uint8), tilt_deg, reshape=True, order=0, mode="constant", cval=1).astype(bool)
    outside = ndimage.rotate(np.ones((H, W), np.uint8), tilt_deg, reshape=True, order=0, mode="constant", cval=0) == 0
    seam_r = seam_lines(np.clip(r, 0, 255).astype(np.uint8), rf | outside)
    back = ndimage.rotate(seam_r.astype(np.uint8), -tilt_deg, reshape=True, order=0, mode="constant", cval=0).astype(bool)
    h, w = back.shape
    y0, x0 = (h - H) // 2, (w - W) // 2
    return back[y0 : y0 + H, x0 : x0 + W]


def pack_filtered(img: np.ndarray, invalid: np.ndarray, size: int, max_seam: float, tilt_deg: float = 0.0, stride: int = 64) -> tuple[list[tuple[int, int, int]], int]:
    """Greedy non-overlapping packing of windows that are fully valid AND have no visible brightness step inside
    (seam_step <= max_seam). Grid-aligned positions first, then the flattest remaining candidates.
    Returns (windows, number of otherwise-valid candidates rejected for a seam)."""
    H, W = invalid.shape
    S = integral(invalid)
    cands = []
    rejected = 0
    for y in range(0, H - size + 1, stride):
        for x in range(0, W - size + 1, stride):
            if window_sum(S, y, x, size):
                continue
            sm = seam_step_tilted(img[y : y + size, x : x + size], tilt_deg)
            if sm > max_seam:
                rejected += 1
                continue
            cands.append((not (y % size == 0 and x % size == 0), sm, y, x))
    cands.sort()
    occupied = np.zeros((H, W), dtype=bool)
    out = []
    for _, _, y, x in cands:
        if occupied[y : y + size, x : x + size].any():
            continue
        occupied[y : y + size, x : x + size] = True
        out.append((y, x, size))
    return out, rejected


# ----------------------------------------------------------------------------- seam metric
def seam_step(crop: np.ndarray, w: int = 16) -> float:
    """Strongest brightness *step* between two adjacent 16-px bands along rows or columns (0-1 scale).
    Tissue varies smoothly, so clean crops sit around 0.06-0.13; a montage seam inside the crop gives 0.2-0.45."""
    best = 0.0
    for prof in (crop.mean(axis=0), crop.mean(axis=1)):
        band = np.convolve(prof.astype(np.float64), np.ones(w) / w, mode="valid")
        if band.size > w:
            best = max(best, float(np.abs(band[w:] - band[:-w]).max()))
    return best / 255.0


def _profile_step(a: np.ndarray, w: int) -> float:
    best = 0.0
    for prof in (a.mean(axis=0), a.mean(axis=1)):
        band = np.convolve(prof.astype(np.float64), np.ones(w) / w, mode="valid")
        if band.size > w:
            best = max(best, float(np.abs(band[w:] - band[:-w]).max()))
    return best / 255.0


def estimate_tilt(img: np.ndarray, fill: np.ndarray, max_deg: float = 12.0, step_deg: float = 0.5, down: int = 4,
                  min_resp: float = 0.15) -> tuple[float, float]:
    """Rotation (deg) of the montage grid: the angle at which the de-rotated, downsampled section shows the
    strongest straight brightness step in its row/column profiles. Fill is neutralised first. Returns
    (tilt, response); a weak response (< min_resp) means no seams worth aligning to and the tilt is reported as 0."""
    f = img.astype(np.float32).copy()
    if fill.any():
        f[fill] = np.median(img[~fill])
    small = f[::down, ::down]
    n = min(small.shape)
    best = (0.0, 0.0)
    for th in np.arange(-max_deg, max_deg + 1e-6, step_deg):
        r = ndimage.rotate(small, th, reshape=False, order=1, mode="nearest")
        m = int(n * np.sin(np.radians(abs(th)))) + 4 + int(n * 0.1)
        sc = _profile_step(r[m:-m, m:-m], max(16 // down, 4))
        if sc > best[0]:
            best = (sc, float(th))
    return (best[1] if best[0] >= min_resp else 0.0), best[0]


def seam_step_tilted(crop: np.ndarray, tilt_deg: float) -> float:
    """seam_step, also measured after de-rotating the crop by the section tilt (on a 2x downsampled copy)."""
    base = seam_step(crop)
    if abs(tilt_deg) < 0.25:
        return base
    small = crop[::2, ::2].astype(np.float32)
    r = ndimage.rotate(small, tilt_deg, reshape=False, order=1, mode="nearest")
    m = int(small.shape[0] * np.sin(np.radians(abs(tilt_deg)))) + 4
    return max(base, _profile_step(r[m:-m, m:-m], 8))


# ----------------------------------------------------------------------------- labels
def label_window(lab: np.ndarray, y: int, x: int, s: int, scale: int) -> np.ndarray:
    """Cut the label at the EM window (y, x, s) and bring it to the EM pixel grid.
    scale > 0: label coarser by `scale` -> nearest upsample; scale < 0: label finer by |scale| -> stride-subsample."""
    if scale >= 1:
        w = lab[y // scale : (y + s) // scale, x // scale : (x + s) // scale]
        return np.repeat(np.repeat(w, scale, axis=0), scale, axis=1) if scale > 1 else w
    k = -scale
    return lab[y * k : (y + s) * k : k, x * k : (x + s) * k : k]


def detect_label_scale(em_shape, lab_shape) -> int | None:
    eh, lh = em_shape[0], lab_shape[0]
    if eh >= lh and eh % lh == 0:
        return eh // lh
    if lh > eh and lh % eh == 0:
        return -(lh // eh)
    return None


# ----------------------------------------------------------------------------- per-section worker
def process_section(job: dict) -> tuple[tuple, list[dict], str]:
    """One section: detect fill, pack windows, (optionally) flatten tile seams, write crops.
    Returns (per_z record, manifest rows, log line)."""
    p, z, out, size, a = Path(job["path"]), job["z"], Path(job["out"]), job["size"], job["args"]
    img = np.asarray(Image.open(p))
    if img.ndim == 3:
        img = img[..., 0]
    if img.dtype != np.uint8:
        img = (img.astype(np.float32) / max(float(img.max()), 1.0) * 255).astype(np.uint8)
    std = float(img.std())
    if std < a["blank_std"]:
        return (z, p.name, "blank_dropped", 0.0, 0, img.size, 0, 0.0, 0), [], f"z{z:04d} blank_dropped (std {std:.2f})"
    inv = fill_mask(img, a["fill_value"], a["min_fill_px"], a["margin"])
    fill_frac = float((img == a["fill_value"]).mean())
    wins = pack(inv, size)
    case = "black_cut" if fill_frac > 0.005 else "clean"
    lab = np.asarray(Image.open(job["label"])) if job.get("label") else None
    scale = detect_label_scale(img.shape, lab.shape) if lab is not None else None
    if lab is not None and scale is None:
        raise SystemExit(f"label {job['label']} shape {lab.shape[:2]} is not an integer multiple of EM {img.shape}")

    n_rejected, tilt, resp, n_seam_px = 0, 0.0, 0.0, 0
    if a["avoid_seams"]:
        tilt, resp = estimate_tilt(img, img == a["fill_value"])
        seam = seams_tilt_aware(img, fill_mask(img, a["fill_value"], a["min_fill_px"], 0), tilt)
        n_seam_px = int(seam.sum())
        if n_seam_px:
            inv = inv | ndimage.binary_dilation(seam, iterations=a["margin"])
        wins, n_rejected = pack_filtered(img, inv, size, a["max_seam"], tilt)
    rows = []
    for y, x, s in wins:
        crop = img[y : y + s, x : x + s]
        sm = seam_step_tilted(crop, tilt) if a["avoid_seams"] else seam_step(crop)
        name = f"z{z:04d}_y{y:04d}_x{x:04d}_s{s}.png"
        Image.fromarray(crop).save(out / "em" / name, format="PNG")
        if lab is not None:
            Image.fromarray(label_window(lab, y, x, s, scale)).save(out / "seg" / name, format="PNG")
        rows.append({"z": z, "source": p.name, "y0": y, "x0": x, "size": s, "case": case,
                     "mean": round(float(crop.mean()), 1), "std": round(float(crop.std()), 1), "seam": round(sm, 3), "tilt_deg": tilt,
                     "label_scale": scale if lab is not None else "",
                     "em": f"em/{name}", "seg": f"seg/{name}" if lab is not None else ""})
    valid = float((~inv).mean())
    covered = len(rows) * size * size / img.size
    line = (f"z{z:04d} {case:12s} fill {fill_frac:5.1%}  valid {valid:5.1%}  crops {len(rows):2d}×{size}  covered {covered:5.1%}"
            + (f"  tilt {tilt:+.1f}° seam px {n_seam_px:5d}  candidates rejected by metric: {n_rejected}" if a["avoid_seams"] else ""))
    return (z, p.name, case, fill_frac, len(rows), img.size, n_rejected, tilt, n_seam_px), rows, line


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--labels", help="directory of label images (same z numbering); cut at the same windows")
    ap.add_argument("--size", type=int, default=1024, help="one crop size for the whole set")
    ap.add_argument("--margin", type=int, default=8, help="pixels to keep away from black")
    ap.add_argument("--min-fill-px", type=int, default=64, help="zero components smaller than this are tissue, not fill")
    ap.add_argument("--fill-value", type=int, default=0)
    ap.add_argument("--blank-std", type=float, default=1.0, help="sections with gray std below this are dropped as blank")
    ap.add_argument("--avoid-seams", action="store_true", help="treat detected montage seams as barriers; drop crops whose seam metric exceeds --max-seam")
    ap.add_argument("--max-seam", type=float, default=0.15, help="largest allowed brightness step inside a crop (clean tissue is 0.06-0.13)")
    ap.add_argument("--workers", type=int, default=1, help="parallel sections")
    a = ap.parse_args()

    src, out = Path(a.src), Path(a.out)
    files = sorted((p for p in src.iterdir() if p.suffix.lower() in {".png", ".tif", ".tiff", ".jpg"}), key=z_of)
    if not files:
        print(f"no images in {src}", file=sys.stderr)
        return 1
    labels = {z_of(p): p for p in Path(a.labels).iterdir()} if a.labels else {}
    for d in ["em"] + (["seg"] if labels else []):
        (out / d).mkdir(parents=True, exist_ok=True)

    ap_args = {"blank_std": a.blank_std, "fill_value": a.fill_value, "min_fill_px": a.min_fill_px, "margin": a.margin, "avoid_seams": a.avoid_seams, "max_seam": a.max_seam}
    jobs = [{"path": str(p), "z": z_of(p), "out": str(out), "size": a.size, "args": ap_args,
             "label": str(labels[z_of(p)]) if z_of(p) in labels else None} for p in files]
    rows, per_z = [], []
    if a.workers > 1:
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for rec, rs, line in ex.map(process_section, jobs, chunksize=1):
                per_z.append(rec); rows.extend(rs); print(line, flush=True)
    else:
        for job in jobs:
            rec, rs, line = process_section(job)
            per_z.append(rec); rows.extend(rs); print(line, flush=True)
    rows.sort(key=lambda r: (r["z"], r["y0"], r["x0"]))

    with open(out / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["z"])
        w.writeheader()
        w.writerows(rows)

    dropped = [f"{rec[0]} ({rec[1]})" for rec in per_z if rec[2] == "blank_dropped"]
    cut = [rec[0] for rec in per_z if rec[2] == "black_cut"]
    total_px = sum(r["size"] ** 2 for r in rows)
    src_px = sum(rec[5] for rec in per_z if rec[2] != "blank_dropped")
    n_rejected = sum(rec[6] for rec in per_z)
    sd = [r["seam"] for r in rows]
    lines = [
        f"# Clean crops from {src}", "",
        f"Sections: {len(files)} in, {len(files) - len(dropped)} used, {len(dropped)} dropped as blank: {', '.join(dropped) or '-'}",
        f"Sections with black cuts / missing tiles: {len(cut)} -> z {cut}",
        f"Crops: {len(rows)} × {a.size}²  (pixels untouched)",
        f"Tissue kept: {total_px / src_px:.1%} of the pixels of the used sections (rest is black, its {a.margin}-px margin, tissue next to montage seams, or strips narrower than {a.size})",
        (f"Montage seams: detected after de-rotating each section by its montage tilt ({sum(1 for rec in per_z if rec[7] != 0)} sections tilted, up to {max(abs(rec[7]) for rec in per_z):.1f}°; "
         f"seams found in {sum(1 for rec in per_z if rec[8] >= 500)} sections) and treated as barriers, so no crop crosses a tile boundary; "
         f"in addition a window is accepted only if the strongest brightness step inside it is <= {a.max_seam} (clean tissue 0.06-0.13, a seam 0.2+), which rejected {n_rejected} more candidates. "
         f"Delivered crops: seam median {np.median(sd):.3f}, max {max(sd):.3f}." if a.avoid_seams else
         f"Montage seams: {sum(1 for v in sd if v > 0.2)} crops contain a visible seam (seam > 0.2); run with --avoid-seams to exclude them."),
        f"Labels: {'cut at the same windows into seg/ (label_scale = label mip factor relative to EM; resampled to the EM grid)' if labels else 'none'}", "",
        "Rule: a crop never contains a fill pixel; windows never overlap; grid-aligned first so clean sections give the same windows in every z.",
        "manifest.csv columns: z, source, y0, x0, size, case (clean|black_cut), mean, std, seam (largest brightness step between adjacent 16-px bands, 0-1, tilt-aware), tilt_deg (montage rotation of the section), label_scale, em, seg.",
    ]
    (out / "README.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[2:8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
