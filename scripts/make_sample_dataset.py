#!/usr/bin/env python
"""Build demo datasets under <data_root>/project_terminal/demo/datasets/datasets/.

  h01_demo_z2048        real H01 sections (64 x 896x768 png) copied from the local Neuroglancer demo output
  h01_precomputed_demo  real H01 sub-volume in Neuroglancer precomputed format (256x256x80)
  synthetic_defects     synthetic serial sections with a known list of injected defects (defects_manifest.json)

Usage: python scripts/make_sample_dataset.py [--dest DATA_ROOT] [--only synthetic]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emqc.config import settings  # noqa: E402

H01_PNG_DIR = Path("/Users/mac/PycharmProjects/Neuroglancer/demos/demo2_fetch/out/aligned")
H01_PRECOMPUTED = Path("/Users/mac/PycharmProjects/Neuroglancer/backend/data/local_volumes/25d04a3ed497/image_subvolume")


def _write_png(path: Path, arr: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path)


# --------------------------------------------------------------------------- synthetic


def synth_em_stack(n_z: int, size: int, rng: np.random.Generator) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """EM-looking serial sections: dark membranes at the zero crossings of a slowly drifting smooth field."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    field = ndimage.gaussian_filter(rng.standard_normal((size, size)).astype(np.float32), 4)
    field /= field.std()
    ems, labels = [], []
    for _ in range(n_z):
        dy = ndimage.gaussian_filter(rng.standard_normal((size, size)).astype(np.float32), 24) * 60
        dx = ndimage.gaussian_filter(rng.standard_normal((size, size)).astype(np.float32), 24) * 60
        field = ndimage.map_coordinates(field, [yy + dy, xx + dx], order=1, mode="reflect")
        fresh = ndimage.gaussian_filter(rng.standard_normal((size, size)).astype(np.float32), 4)
        field = 0.92 * field + 0.08 * fresh / (fresh.std() + 1e-6)
        field /= field.std()
        membrane = 1.0 - np.exp(-((field / 0.12) ** 2))
        texture = ndimage.gaussian_filter(rng.standard_normal((size, size)).astype(np.float32), 1.0) * 0.06
        em = np.clip(0.30 + 0.45 * membrane + texture, 0, 1)
        ems.append((em * 255).astype(np.uint8))
        pos, npos = ndimage.label(field > 0)
        neg, _ = ndimage.label(field < 0)
        labels.append((pos + np.where(neg > 0, neg + npos, 0)).astype(np.uint16))
    return ems, labels


def make_synthetic(dst: Path, n_z: int = 128, size: int = 512, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    ems, labels = synth_em_stack(n_z, size, rng)
    defects: list[dict] = []

    def add(z, t, **kw):
        defects.append({"z": z, "type": t, **kw})

    def blank(z):
        ems[z][:] = 128
        add(z, "blank")

    def blur(z):
        ems[z] = ndimage.gaussian_filter(ems[z].astype(np.float32), 6).astype(np.uint8)
        add(z, "blur")

    def bright(z, delta=60):
        ems[z] = np.clip(ems[z].astype(np.int16) + delta, 0, 255).astype(np.uint8)
        add(z, "brightness_jump", delta=delta)

    def contrast(z0, factors):
        for i, f in enumerate(factors):
            z = z0 + i
            a = ems[z].astype(np.float32)
            ems[z] = np.clip(a.mean() + (a - a.mean()) * f, 0, 255).astype(np.uint8)
        add(z0, "contrast_drift", z_to=z0 + len(factors) - 1, factors=factors)

    def saturate(z, thr=150):
        a = ems[z].copy()
        a[a > thr] = 255
        ems[z] = a
        add(z, "saturation", threshold=thr)

    def gshift(z, dy, dx):
        ems[z] = ndimage.shift(ems[z], (dy, dx), order=0, mode="nearest")
        add(z, "global_misalign", dy=dy, dx=dx)

    def jump(z):
        other, _ = synth_em_stack(1, size, np.random.default_rng(seed + 999))
        ems[z] = other[0]
        add(z, "slice_jump")

    def swap(z):
        ems[z], ems[z + 1] = ems[z + 1], ems[z]
        labels[z], labels[z + 1] = labels[z + 1], labels[z]
        add(z, "z_order", z_to=z + 1, note="adjacent sections swapped")

    def duplicate(z):
        ems[z] = ems[z - 1].copy()
        add(z, "z_order", z_to=z - 1, note="duplicate of previous section")

    def charging(z):
        a = ems[z].astype(np.float32)
        y0, y1 = size // 3, size // 3 + size // 10
        ramp = np.linspace(0, 1, y1 - y0)[:, None] ** 0.5
        a[y0:y1] = a[y0:y1] * (1 - ramp) + 255 * ramp
        a[y1 - 6 : y1] = 255
        ems[z] = np.clip(a, 0, 255).astype(np.uint8)
        add(z, "charging", bbox=[0, y0, size, y1], note="directional band + brightness ramp")

    def crack(z, half_width=6):
        # a fold / crack exported as a zero-filled band that crosses the whole section (as seen in real mouse_30um data)
        a = ems[z].copy()
        yy, xx = np.mgrid[0:size, 0:size]
        curve = size / 2 + 40 * np.sin(xx / 37.0) + 0.35 * (xx - size / 2)
        a[np.abs(yy - curve) <= half_width] = 0
        ems[z] = a
        add(z, "crack", note="zero-filled band across the section")

    def contamination(z, n=3):
        """表面污染：几个高对比的孤立团块（灰尘 / 冰晶）。"""
        a = ems[z].astype(np.float32)
        yy, xx = np.mgrid[0:size, 0:size]
        boxes = []
        for i in range(n):
            cy, cx = int(size * (0.25 + 0.2 * i)), int(size * (0.7 - 0.15 * i))
            rad = max(4, size // 22)
            m = (yy - cy) ** 2 + (xx - cx) ** 2 <= rad * rad
            a[m] = 250 if i % 2 == 0 else 5
            boxes.append([cx - rad, cy - rad, cx + rad, cy + rad])
        ems[z] = np.clip(a, 0, 255).astype(np.uint8)
        add(z, "contamination", bbox=boxes[0], note=f"{n} high-contrast isolated blobs")

    def section_deform(z, stretch=0.06, shear=0.03):
        """整片形变：拉伸 + 剪切（切片制备的机械形变）。"""
        c = (size - 1) / 2.0
        M = np.array([[1.0 + stretch, shear], [0.0, 1.0 - stretch * 0.5]])
        off = np.array([c, c]) - M @ np.array([c, c])
        ems[z] = ndimage.affine_transform(ems[z], M, offset=off, order=1, mode="nearest").astype(np.uint8)
        add(z, "section_deformation", stretch=stretch, shear=shear, note="affine stretch + shear of the whole section")

    def local_deform(z, amp=6.0):
        """局部形变：平滑的非刚性扭曲，去掉仿射后仍有残差。"""
        rng2 = np.random.default_rng(z)
        coarse = rng2.normal(0, 1, (4, 4, 2))
        fy = ndimage.zoom(coarse[..., 0], size / 4, order=3) * amp
        fx = ndimage.zoom(coarse[..., 1], size / 4, order=3) * amp
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        ems[z] = ndimage.map_coordinates(ems[z], [yy + fy[:size, :size], xx + fx[:size, :size]], order=1, mode="nearest").astype(np.uint8)
        add(z, "local_deformation", amp=amp, note="smooth non-rigid warp (non-affine)")

    def local_shift(z, dx=20):
        a = ems[z].copy()
        a[size // 2 :] = ndimage.shift(a[size // 2 :], (0, dx), order=0, mode="nearest")
        ems[z] = a
        add(z, "local_misalign", bbox=[0, size // 2, size, size], dx=dx)

    plan = [
        (5, lambda: blank(5)),
        (9, lambda: add(9, "missing")),
        (13, lambda: add(13, "corrupt")),
        (20, lambda: blur(20)),
        (27, lambda: bright(27)),
        (40, lambda: contrast(33, [0.8, 0.65, 0.5, 0.4, 0.35, 0.35, 0.35, 0.35])),
        (45, lambda: saturate(45)),
        (52, lambda: gshift(52, 25, -18)),
        (58, lambda: jump(58)),
        (71, lambda: swap(70)),
        (77, lambda: charging(77)),
        (85, lambda: crack(85)),
        (100, lambda: duplicate(100)),
        (110, lambda: local_shift(110)),
        (118, lambda: contamination(118)),
        (122, lambda: section_deform(122)),
        (126, lambda: local_deform(126)),
    ]
    for last_z, inject in plan:
        if last_z < n_z:
            inject()

    em_dir, gt_dir, pred_dir = dst / "em", dst / "gt", dst / "pred"
    for d in (em_dir, gt_dir, pred_dir):
        if d.exists():
            shutil.rmtree(d)
    for z in range(n_z):
        name = f"sec_{z:04d}.png"
        if z == 9 and n_z > 9:
            pass  # missing on purpose
        elif z == 13 and n_z > 13:
            em_dir.mkdir(parents=True, exist_ok=True)
            (em_dir / name).write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(rng.integers(0, 256, 200, dtype=np.uint8)))
        else:
            _write_png(em_dir / name, ems[z])
        _write_png(gt_dir / name, labels[z])
        pred = np.where(rng.random(labels[z].shape) < 0.02, 0, labels[z]).astype(np.uint16)
        _write_png(pred_dir / name, pred)

    (dst / "synapse").mkdir(parents=True, exist_ok=True)
    (dst / "synapse" / "synapses.json").write_text(
        json.dumps([{"id": i, "x": int(rng.integers(0, size)), "y": int(rng.integers(0, size)), "z": int(rng.integers(0, n_z)), "pre": int(rng.integers(1, 200)), "post": int(rng.integers(1, 200)), "score": float(rng.random())} for i in range(50)], indent=1)
    )
    (dst / "mito").mkdir(parents=True, exist_ok=True)
    (dst / "mito" / "mito_pred.json").write_text(
        json.dumps([{"id": i, "bbox": [int(v) for v in sorted(rng.integers(0, size, 2))] + [int(v) for v in sorted(rng.integers(0, size, 2))], "z": int(rng.integers(0, n_z))} for i in range(30)], indent=1)
    )
    (dst / "skeleton").mkdir(parents=True, exist_ok=True)
    (dst / "skeleton" / "neuron_0001.swc").write_text("# synthetic skeleton\n" + "\n".join(f"{i+1} 3 {i*10.0} {i*4.0} {i*40.0} 1.0 {i}" for i in range(20)) + "\n")
    (dst / "traces").mkdir(parents=True, exist_ok=True)
    (dst / "traces" / "agent_run_0001.json").write_text(
        json.dumps({"agent": "seg-agent", "algo_version": "seg-algo-0.3.0", "model_version": "unet-2026-09-01", "experiment_id": "exp-synth-001", "steps": [{"step": "load", "status": "ok"}, {"step": "predict", "status": "ok", "blocks": 2}, {"step": "agglomerate", "status": "ok"}]}, indent=1)
    )
    manifest = {
        "dataset_id": dst.name,
        "name": "Synthetic serial sections with injected defects",
        "species": "mouse",
        "brain_region": "V1 (synthetic)",
        "voxel_size_nm": [8, 8, 40],
        "staining": "synthetic",
        "imaging_modality": "synthetic-ssEM",
        "acquisition_batch": "synth-2026-09",
        "size_class": "large",
        "z_offset": 0,
        "em": {"path": "em", "format": "image_stack"},
        "assets": [
            {"type": "gt_segmentation", "path": "gt", "format": "image_stack", "version": "v1"},
            {"type": "model_prediction", "path": "pred", "format": "image_stack", "version": "v1", "algo_version": "seg-algo-0.3.0", "model_version": "unet-2026-09-01", "experiment_id": "exp-synth-001"},
            {"type": "synapse_prediction", "path": "synapse/synapses.json", "format": "json", "version": "v1", "model_version": "syn-2026-08-15"},
            {"type": "mitochondria_prediction", "path": "mito/mito_pred.json", "format": "json", "version": "v1", "model_version": "mito-2026-08-20"},
            {"type": "skeleton", "path": "skeleton", "format": "swc", "version": "v1"},
            {"type": "agent_trace", "path": "traces", "format": "json", "version": "v1"},
        ],
        "versions": {"data": "v1", "algo": "seg-algo-0.3.0", "model": "unet-2026-09-01", "experiment": "exp-synth-001"},
        "extra": {"defects_manifest": "defects_manifest.json", "generator_seed": seed},
    }
    (dst / "dataset.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    (dst / "defects_manifest.json").write_text(json.dumps({"n_z": n_z, "size": size, "defects": defects}, indent=1))
    return {"dataset_id": dst.name, "n_z": n_z, "size": size, "defects": defects}


# --------------------------------------------------------------------------- real H01 samples


def make_h01_png(dst: Path) -> dict | None:
    files = sorted(H01_PNG_DIR.glob("em_z_*.png"))
    if not files:
        return None
    em = dst / "em"
    em.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, em / f.name)
    z0 = min(int(f.stem.split("_")[-1]) for f in files)
    manifest = {
        "dataset_id": dst.name,
        "name": "H01 demo sub-stack (real sections)",
        "species": "human",
        "brain_region": "temporal cortex (H01)",
        "voxel_size_nm": [4, 4, 33],
        "staining": "heavy metal (OsO4 / UA / Pb)",
        "imaging_modality": "ssEM (multibeam SEM)",
        "acquisition_batch": "h01-demo2-fetch",
        "size_class": "auto",
        "z_offset": z0,
        "em": {"path": "em", "format": "image_stack"},
        "versions": {"data": "v1"},
        "extra": {"source": str(H01_PNG_DIR)},
    }
    (dst / "dataset.json").write_text(json.dumps(manifest, indent=1))
    return {"dataset_id": dst.name, "n_files": len(files), "z_offset": z0}


def make_h01_precomputed(dst: Path) -> dict | None:
    if not (H01_PRECOMPUTED / "info").is_file():
        return None
    em = dst / "em"
    if em.exists():
        shutil.rmtree(em)
    shutil.copytree(H01_PRECOMPUTED, em)
    manifest = {
        "dataset_id": dst.name,
        "name": "H01 precomputed sub-volume",
        "species": "human",
        "brain_region": "temporal cortex (H01)",
        "staining": "heavy metal (OsO4 / UA / Pb)",
        "imaging_modality": "ssEM (multibeam SEM)",
        "acquisition_batch": "h01-local-volume-25d04a3ed497",
        "size_class": "auto",
        "em": {"path": "em", "format": "precomputed"},
        "versions": {"data": "v1"},
        "extra": {"source": str(H01_PRECOMPUTED)},
    }
    (dst / "dataset.json").write_text(json.dumps(manifest, indent=1))
    return {"dataset_id": dst.name}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default=str(settings.data_root), help="data root (default: EMQC_DATA_ROOT)")
    ap.add_argument("--only", choices=["synthetic", "h01", "precomputed"], nargs="*")
    ap.add_argument("--n-z", type=int, default=128)
    ap.add_argument("--size", type=int, default=512)
    args = ap.parse_args(argv)
    base = Path(args.dest) / "project_terminal" / "demo" / "datasets" / "datasets"
    base.mkdir(parents=True, exist_ok=True)
    only = set(args.only or ["synthetic", "h01", "precomputed"])
    if "synthetic" in only:
        r = make_synthetic(base / "synthetic_defects", n_z=args.n_z, size=args.size)
        print(f"synthetic_defects: {r['n_z']} sections, {len(r['defects'])} injected defects")
    if "h01" in only:
        r = make_h01_png(base / "h01_demo_z2048")
        print("h01_demo_z2048:", r or "source not found, skipped")
    if "precomputed" in only:
        r = make_h01_precomputed(base / "h01_precomputed_demo")
        print("h01_precomputed_demo:", r or "source not found, skipped")
    print("data root:", base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
