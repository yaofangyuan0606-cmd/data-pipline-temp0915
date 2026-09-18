"""Turn raw SAM masks into a label volume you can open in the annotation page.

SAM's automatic mask generator returns, per section, a pile of boolean masks that overlap each other, carry no ids
and know nothing about the neighbouring sections. This script turns that pile into one `seg.npy`:

  1. per section, paint the masks into a label plane, largest first, a pixel only once — so a whole cell beats the
     organelle-sized masks SAM proposes inside it;
  2. keep the delivered labels and only fill SAM where the delivery left the pixel unlabelled (measured on this data:
     SAM matches delivered cells above 3000 px with a median IoU of 0.90, but misses 64 % of the cells below 500 px,
     so overwriting good labels with SAM would lose work — see --overwrite to change that policy);
  3. drop the masks that are mostly on top of existing labels, and the crumbs left after the intersection;
  4. link ids across z by overlap so one cell keeps one colour as you flip through the stack;
  5. write a NEW block directory. Nothing under the source block or the mask directory is modified.

    python scripts/merge_sam_masks.py RAW_MASKS_DIR SOURCE_BLOCK [--out var/sam_blocks] [--name NAME]
           [--min-area 300] [--min-bg-frac 0.5] [--link-iou 0.4] [--overwrite] [--dry-run]

RAW_MASKS_DIR holds z####.npz (key "masks", shape (n, rows, cols), bool) and optional z####.json.
SOURCE_BLOCK is a block directory with em.npy (+ seg.npy, meta.json).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from scipy import ndimage


def masks_to_plane(masks: np.ndarray, allowed: np.ndarray, min_area: int, min_bg_frac: float) -> tuple[np.ndarray, dict]:
    """Paint masks into a local label plane (1..n), largest first, each pixel assigned once and only where `allowed`.

    A mask whose pixels are mostly outside `allowed` is skipped entirely: the delivery already has an opinion there,
    and the sliver that remains would be a fragment of a cell rather than a cell."""
    plane = np.zeros(allowed.shape, dtype=np.int32)
    stats = {"masks": int(len(masks)), "skipped_overlap": 0, "skipped_small": 0, "used": 0}
    for i in np.argsort([-int(m.sum()) for m in masks]):
        m = masks[i]
        area = int(m.sum())
        if area == 0:
            continue
        if area and (m & allowed).sum() / area < min_bg_frac:
            stats["skipped_overlap"] += 1
            continue
        free = m & allowed & (plane == 0)
        if free.sum() < min_area:
            stats["skipped_small"] += 1
            continue
        stats["used"] += 1
        plane[free] = stats["used"]
    return plane, stats


def link(prev: np.ndarray | None, cur: np.ndarray, next_id: int, link_iou: float) -> tuple[np.ndarray, int, int]:
    """Give each local label of this section a global id: the previous section's id it overlaps most, when their IoU
    reaches `link_iou`, else a fresh id. One previous cell continues into at most one current label."""
    out = np.zeros(cur.shape, dtype=np.uint64)
    n = int(cur.max())
    if n == 0:
        return out, next_id, 0
    if prev is None or not prev.any():
        for k in range(1, n + 1):
            out[cur == k] = next_id
            next_id += 1
        return out, next_id, 0
    cur_sizes = np.bincount(cur.ravel(), minlength=n + 1)
    prev_ids, prev_inv = np.unique(prev, return_inverse=True)
    prev_sizes = np.bincount(prev_inv.ravel())
    both = (cur > 0) & (prev > 0)
    best: dict[int, tuple[int, float]] = {}
    if both.any():
        pairs = np.stack([cur[both].astype(np.int64), prev_inv.reshape(prev.shape)[both].astype(np.int64)], axis=1)
        keys, counts = np.unique(pairs, axis=0, return_counts=True)
        for (k, pj), c in zip(keys, counts):
            iou = c / (cur_sizes[k] + prev_sizes[pj] - c)
            if k not in best or iou > best[k][1]:
                best[k] = (int(prev_ids[pj]), float(iou))
    taken: set[int] = set()
    linked = 0
    for k in range(1, n + 1):
        pid, iou = best.get(k, (0, 0.0))
        if pid and iou >= link_iou and pid not in taken:
            out[cur == k] = pid
            taken.add(pid)
            linked += 1
        else:
            out[cur == k] = next_id
            next_id += 1
    return out, next_id, linked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("masks_dir")
    ap.add_argument("block")
    ap.add_argument("--out", default="var/sam_blocks")
    ap.add_argument("--name", default=None, help="output block name (default <block>_sam_filled)")
    ap.add_argument("--min-area", type=int, default=300, help="drop a mask whose free area is smaller than this")
    ap.add_argument("--min-bg-frac", type=float, default=0.5,
                    help="drop a mask unless at least this fraction of it lies in the area SAM is allowed to fill")
    ap.add_argument("--link-iou", type=float, default=0.3, help="overlap with the previous section needed to keep an id")
    ap.add_argument("--link-gap", type=int, default=2, help="also try to link to a section this many steps back, so a "
                                                            "cell SAM failed to propose on one section does not break the chain")
    ap.add_argument("--overwrite", action="store_true",
                    help="let SAM replace delivered labels too (measured worse on this data; off by default)")
    ap.add_argument("--dry-run", action="store_true", help="report what would be written and stop")
    a = ap.parse_args()

    block = Path(a.block)
    em_path = block / "em.npy"
    if not em_path.exists():
        sys.exit(f"{em_path} not found")
    em = np.load(em_path, mmap_mode="r")
    H, W, Z = em.shape
    seg_src = block / "seg.npy"
    have_seg = seg_src.exists()
    delivered = np.load(seg_src, mmap_mode="r") if have_seg else None

    files = {int(p.name[1:5]): p for p in sorted(Path(a.masks_dir).glob("z*.npz"))}
    if not files:
        sys.exit(f"no z####.npz under {a.masks_dir}")
    bad = [z for z in files if not 0 <= z < Z]
    if bad:
        sys.exit(f"mask sections outside the block's z range: {bad[:5]}")
    print(f"{block.name}: {H}x{W}x{Z}, masks for {len(files)} sections ({min(files)}..{max(files)}), "
          f"delivered labels: {'yes' if have_seg else 'no'}")

    out = Path(a.out) / (a.name or f"{block.name}_sam_filled")
    base_id = int(max(int(np.asarray(delivered[:, :, k]).max()) for k in range(Z))) if have_seg else 0
    next_id = base_id + 1
    print(f"new ids start at {next_id} (above every delivered id), output -> {out}")

    if not a.dry_run:
        out.mkdir(parents=True, exist_ok=True)
        if not (out / "em.npy").exists():
            shutil.copyfile(em_path, out / "em.npy")
        seg = np.lib.format.open_memmap(out / "seg.npy", mode="w+", dtype=np.uint64, shape=(H, W, Z))
    else:
        seg = np.zeros((H, W, 1), dtype=np.uint64)

    per_z, recent, t0 = {}, [], time.perf_counter()   # recent = the last few global planes, newest first
    tot = {"used": 0, "skipped_overlap": 0, "skipped_small": 0, "masks": 0, "filled": 0}
    for z in range(Z):
        base = np.asarray(delivered[:, :, z]).astype(np.uint64) if have_seg else np.zeros((H, W), np.uint64)
        if z not in files:
            if not a.dry_run:
                seg[:, :, z] = base
            recent = []                         # a gap in the masks breaks the chain: do not link across it
            continue
        masks = np.load(files[z])["masks"]
        if masks.shape[1:] != (H, W):
            sys.exit(f"z{z:04d}: masks are {masks.shape[1:]}, the block is {(H, W)}")
        allowed = np.ones((H, W), bool) if (a.overwrite or not have_seg) else (base == 0)
        local, st = masks_to_plane(masks, allowed, a.min_area, a.min_bg_frac)
        # Try the immediately preceding section first; if SAM did not propose the cell there, reach one section
        # further back, so a single missed proposal does not restart the id.
        reference = recent[0] if recent else None
        for older in recent[1:]:
            if reference is None:
                reference = older
                break
            reference = np.where(reference > 0, reference, older)
        glob_, next_id, linked = link(reference, local, next_id, a.link_iou)
        recent = ([glob_] + recent)[:max(1, a.link_gap)]
        plane = base.copy()
        fill = glob_ > 0
        plane[fill] = glob_[fill]
        if not a.dry_run:
            seg[:, :, z] = plane
        for k in ("used", "skipped_overlap", "skipped_small", "masks"):
            tot[k] += st[k]
        tot["filled"] += int(fill.sum())
        per_z[str(z)] = {**st, "linked": linked, "filled_px": int(fill.sum()),
                         "coverage_before": round(float((base > 0).mean()), 4),
                         "coverage_after": round(float((plane > 0).mean()), 4)}
        if z % 10 == 0 or z == max(files):
            print(f"  z{z:04d}: {st['masks']:3d} masks -> {st['used']:3d} used "
                  f"({st['skipped_overlap']} on existing labels, {st['skipped_small']} too small), "
                  f"{linked:3d} linked, coverage {per_z[str(z)]['coverage_before']:.1%} -> {per_z[str(z)]['coverage_after']:.1%}")

    n_new = next_id - base_id - 1
    cov_before = float(np.mean([v["coverage_before"] for v in per_z.values()])) if per_z else 0.0
    cov_after = float(np.mean([v["coverage_after"] for v in per_z.values()])) if per_z else 0.0
    print(f"\n{tot['masks']} masks: {tot['used']} used, {tot['skipped_overlap']} dropped (mostly on delivered labels), "
          f"{tot['skipped_small']} dropped (smaller than {a.min_area} px)")
    print(f"{n_new} new ids, {tot['filled']} px filled, coverage {cov_before:.1%} -> {cov_after:.1%} "
          f"on the {len(per_z)} sections that had masks")
    if a.dry_run:
        print("dry run: nothing written")
        return 0

    seg.flush()
    src_meta = json.loads((block / "meta.json").read_text()) if (block / "meta.json").exists() else {}
    meta = {**src_meta,
            "dataset": {**src_meta.get("dataset", {}), "id": (src_meta.get("dataset", {}).get("id") or block.name) + "-sam-filled"},
            "source_block": str(block.resolve()), "masks_dir": str(Path(a.masks_dir).resolve()),
            "sam_merge": {"min_area": a.min_area, "min_bg_frac": a.min_bg_frac, "link_iou": a.link_iou, "link_gap": a.link_gap,
                          "overwrite_delivered": bool(a.overwrite), "first_new_id": base_id + 1, "n_new_ids": n_new,
                          "sections_with_masks": sorted(files), "coverage_before": round(cov_before, 4),
                          "coverage_after": round(cov_after, 4), "seconds": round(time.perf_counter() - t0, 1)},
            "per_section": per_z}
    (out / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    print(f"done in {time.perf_counter() - t0:.1f}s -> {out}")
    print("open it in the annotation page: it is picked up from EMQC_SAM_BLOCKS_DIR automatically")
    return 0


if __name__ == "__main__":
    sys.exit(main())
