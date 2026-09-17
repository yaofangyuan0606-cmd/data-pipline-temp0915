"""Pre-label an EM block with Segment Anything, then correct it in the annotation page.

Reads a delivery block (`em.npy`, (rows, cols, z) uint8), runs SAM's automatic mask generator on every section,
turns the masks into a label image, links labels across neighbouring sections by overlap so one cell keeps one id
through the stack, and writes a *new* block directory next to nothing else:

    <out>/<block>_sam/em.npy      the same EM (copied, so the block is self-contained)
    <out>/<block>_sam/seg.npy     (rows, cols, z) uint32 SAM labels, 0 = unlabelled
    <out>/<block>_sam/meta.json   provenance: model, checkpoint, parameters, per-section mask counts

The delivery directory is never written to. The annotation page picks the new block up from
EMQC_SAM_BLOCKS_DIR (default var/sam_blocks) and edits go to its own work directory as usual.

Needs a GPU in practice: SAM ViT-H is ~3 s per 1024² section on a large GPU and minutes per section on CPU.
    pip install --index-url https://download.pytorch.org/whl/cu128 torch torchvision
    pip install segment-anything opencv-python-headless
    curl -LO https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth

Usage:
    python scripts/sam_label.py BLOCK_DIR --checkpoint sam_vit_h_4b8939.pth [--out var/sam_blocks]
           [--model vit_h] [--points-per-side 32] [--pred-iou 0.86] [--stability 0.92] [--min-area 64]
           [--max-area-frac 0.25] [--link-iou 0.5] [--z0 0] [--z1 N] [--device cuda]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np


def masks_to_labels(masks: list[dict], shape: tuple[int, int], min_area: int, max_area: int) -> tuple[np.ndarray, int]:
    """Paint SAM masks into one label plane. Larger masks first, and a pixel is only assigned once, so a whole cell
    beats the organelle-sized masks SAM also proposes inside it. Masks above `max_area` (background, big mergers)
    and below `min_area` are skipped. Labels are local (1..n) here; linking across z happens later."""
    plane = np.zeros(shape, dtype=np.uint32)
    n = 0
    for m in sorted(masks, key=lambda m: -int(m["area"])):
        a = int(m["area"])
        if a < min_area or a > max_area:
            continue
        seg = m["segmentation"]
        free = seg & (plane == 0)
        if free.sum() < min_area:
            continue
        n += 1
        plane[free] = n
    return plane, n


def link_ids(prev: np.ndarray | None, cur_local: np.ndarray, next_id: int, link_iou: float) -> tuple[np.ndarray, int, int]:
    """Give every local label of this section a global id: the id of the previous section's label it overlaps most,
    if their IoU >= link_iou, otherwise a fresh id. Returns (global plane, next free id, number of links made)."""
    out = np.zeros(cur_local.shape, dtype=np.uint32)
    n_local = int(cur_local.max())
    if n_local == 0:
        return out, next_id, 0
    linked = 0
    if prev is None or not prev.any():
        for k in range(1, n_local + 1):
            out[cur_local == k] = next_id
            next_id += 1
        return out, next_id, 0
    prev_sizes = np.bincount(prev.ravel().astype(np.int64))
    cur_sizes = np.bincount(cur_local.ravel().astype(np.int64), minlength=n_local + 1)
    # joint histogram of (local label, previous id) over overlapping pixels
    both = (cur_local > 0) & (prev > 0)
    if both.any():
        pairs = np.stack([cur_local[both].astype(np.int64), prev[both].astype(np.int64)], axis=1)
        keys, counts = np.unique(pairs, axis=0, return_counts=True)
    else:
        keys, counts = np.zeros((0, 2), np.int64), np.zeros(0, np.int64)
    best: dict[int, tuple[int, float]] = {}
    for (k, pid), c in zip(keys, counts):
        iou = c / (cur_sizes[k] + prev_sizes[pid] - c)
        if k not in best or iou > best[k][1]:
            best[k] = (int(pid), float(iou))
    taken: set[int] = set()
    for k in range(1, n_local + 1):
        pid, iou = best.get(k, (0, 0.0))
        if pid and iou >= link_iou and pid not in taken:   # one previous cell continues into at most one current label
            out[cur_local == k] = pid
            taken.add(pid)
            linked += 1
        else:
            out[cur_local == k] = next_id
            next_id += 1
    return out, next_id, linked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("block")
    ap.add_argument("--out", default="var/sam_blocks")
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--model", default="vit_h", choices=["vit_h", "vit_l", "vit_b"])
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--points-per-side", type=int, default=32)
    ap.add_argument("--pred-iou", type=float, default=0.86)
    ap.add_argument("--stability", type=float, default=0.92)
    ap.add_argument("--min-area", type=int, default=64, help="drop masks smaller than this many pixels")
    ap.add_argument("--max-area-frac", type=float, default=0.25, help="drop masks covering more than this fraction of the section")
    ap.add_argument("--link-iou", type=float, default=0.5, help="IoU with the previous section needed to keep the same id")
    ap.add_argument("--z0", type=int, default=0)
    ap.add_argument("--z1", type=int, default=None)
    ap.add_argument("--name", default=None, help="output block name (default <block>_sam)")
    a = ap.parse_args()

    import torch  # noqa: E402  (imported late so --help works without it)
    from segment_anything import SamAutomaticMaskGenerator, sam_model_registry

    block = Path(a.block)
    em = np.load(block / "em.npy", mmap_mode="r")
    if em.ndim != 3:
        sys.exit(f"{block}/em.npy must be (rows, cols, z), got {em.shape}")
    H, W, Z = em.shape
    z1 = Z if a.z1 is None else min(Z, a.z1)
    out = Path(a.out) / (a.name or f"{block.name}_sam")
    out.mkdir(parents=True, exist_ok=True)
    if not (out / "em.npy").exists():
        shutil.copyfile(block / "em.npy", out / "em.npy")
    seg_path = out / "seg.npy"
    if seg_path.exists():
        seg = np.load(seg_path, mmap_mode="r+")           # resume: keep sections already done
    else:
        seg = np.lib.format.open_memmap(seg_path, mode="w+", dtype=np.uint32, shape=(H, W, Z))
    meta_path = out / "meta.json"
    meta = json.load(open(meta_path)) if meta_path.exists() else {}
    src_meta = json.load(open(block / "meta.json")) if (block / "meta.json").exists() else {}
    meta.setdefault("dataset", {**src_meta.get("dataset", {}), "id": src_meta.get("dataset", {}).get("id", block.name) + "-sam"})
    meta.setdefault("geometry", src_meta.get("geometry", {}))
    meta["source_block"] = str(block)
    meta["sam"] = {"model": a.model, "checkpoint": Path(a.checkpoint).name, "points_per_side": a.points_per_side, "pred_iou_thresh": a.pred_iou,
                   "stability_score_thresh": a.stability, "min_area": a.min_area, "max_area_frac": a.max_area_frac, "link_iou": a.link_iou}
    per_z = meta.setdefault("per_section", {})

    device = a.device if (a.device != "cuda" or torch.cuda.is_available()) else "cpu"
    sam = sam_model_registry[a.model](checkpoint=a.checkpoint).to(device)
    gen = SamAutomaticMaskGenerator(sam, points_per_side=a.points_per_side, pred_iou_thresh=a.pred_iou,
                                    stability_score_thresh=a.stability, min_mask_region_area=a.min_area)
    print(f"{block.name}: {H}x{W}x{Z}, sections {a.z0}..{z1 - 1}, SAM {a.model} on {device}", flush=True)

    next_id = int(seg.max()) + 1 if seg_path.exists() and int(np.asarray(seg).max()) > 0 else 1
    prev = np.asarray(seg[:, :, a.z0 - 1]) if a.z0 > 0 else None
    max_area = int(a.max_area_frac * H * W)
    t_all = time.perf_counter()
    for z in range(a.z0, z1):
        t0 = time.perf_counter()
        img = np.asarray(em[:, :, z])
        rgb = np.repeat(img[:, :, None], 3, axis=2)
        masks = gen.generate(rgb)
        local, n_used = masks_to_labels(masks, (H, W), a.min_area, max_area)
        plane, next_id, linked = link_ids(prev, local, next_id, a.link_iou)
        seg[:, :, z] = plane
        prev = plane
        covered = float((plane > 0).mean())
        per_z[str(z)] = {"masks": len(masks), "used": n_used, "linked": linked, "coverage": round(covered, 3), "seconds": round(time.perf_counter() - t0, 1)}
        print(f"z{z:04d}: {len(masks):4d} masks -> {n_used:4d} labels, {linked:4d} linked to z-1, coverage {covered:5.1%}, {time.perf_counter() - t0:5.1f}s", flush=True)
        if z % 5 == 4 or z == z1 - 1:
            seg.flush()
            meta["n_ids"] = int(next_id - 1)
            json.dump(meta, open(meta_path, "w"), indent=1, ensure_ascii=False)
    seg.flush()
    meta["n_ids"] = int(next_id - 1)
    meta["seconds_total"] = round(time.perf_counter() - t_all, 1)
    json.dump(meta, open(meta_path, "w"), indent=1, ensure_ascii=False)
    print(f"done: {next_id - 1} ids, {meta['seconds_total']} s -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
