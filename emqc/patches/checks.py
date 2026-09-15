"""Leakage / duplicate checks over a patch set (requirement 3: train/test 泄漏, patch 重复, overlap 泄漏).

Patches are dicts with block_id, partition, z0..x1. Blocks are the ORM rows (or anything with the same attributes).
Hard failures (any > 0 fails the set): exact duplicates, cross-partition overlap, patch outside its block,
patch partition differing from its block's. Soft signals are reported but do not fail the set.
"""
from __future__ import annotations

from collections import defaultdict


def _iou(a: dict, b: dict) -> float:
    dz = min(a["z1"], b["z1"]) - max(a["z0"], b["z0"])
    dy = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
    dx = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
    if dz <= 0 or dy <= 0 or dx <= 0:
        return 0.0
    inter = dz * dy * dx
    va = (a["z1"] - a["z0"]) * (a["y1"] - a["y0"]) * (a["x1"] - a["x0"])
    vb = (b["z1"] - b["z0"]) * (b["y1"] - b["y0"]) * (b["x1"] - b["x0"])
    return inter / float(va + vb - inter)


def _inside(p: dict, b) -> bool:
    return b.z_start <= p["z0"] < p["z1"] <= b.z_end and b.y_start <= p["y0"] < p["y1"] <= b.y_end and b.x_start <= p["x0"] < p["x1"] <= b.x_end


def check_patches(patches: list[dict], blocks: dict, near_dup_iou: float = 0.9, max_pairs: int = 2_000_000) -> dict:
    seen: dict[tuple, int] = defaultdict(int)
    by_block: dict[str, list[dict]] = defaultdict(list)
    n_outside = n_mismatch = n_unpartitioned = 0
    for p in patches:
        seen[(p["block_id"], p["z0"], p["z1"], p["y0"], p["y1"], p["x0"], p["x1"])] += 1
        by_block[p["block_id"]].append(p)
        b = blocks.get(p["block_id"])
        if b is None or not _inside(p, b):
            n_outside += 1
        elif (b.partition or "none") != p.get("partition", "none"):
            n_mismatch += 1
        if p.get("partition", "none") not in ("train", "val", "test"):
            n_unpartitioned += 1
    n_dup = sum(c - 1 for c in seen.values() if c > 1)
    # overlaps: blocks are disjoint boxes, so only patches of the same block can overlap; cross-partition overlap
    # would need two partitions inside one block, which the schema forbids - still measured, never assumed
    n_near, n_cross, pairs = 0, 0, 0
    for bid, ps in by_block.items():
        ps.sort(key=lambda p: p["z0"])
        for i, a in enumerate(ps):
            for b in ps[i + 1 :]:
                if b["z0"] >= a["z1"]:
                    break
                pairs += 1
                if pairs > max_pairs:
                    break
                iou = _iou(a, b)
                if iou >= near_dup_iou:
                    n_near += 1
                if iou > 0 and a.get("partition") != b.get("partition"):
                    n_cross += 1
    # adjacency across partitions at block level (soft): shared faces between blocks of different holdout partitions
    from .partition import _adjacent

    parted = [b for b in blocks.values() if (b.partition or "none") in ("train", "val", "test") and b.block_id in by_block]
    n_adj = sum(1 for i, a in enumerate(parted) for b in parted[i + 1 :] if a.partition != b.partition and _adjacent(a, b))
    hard = {"n_duplicate_exact": n_dup, "n_overlap_cross_partition": n_cross, "n_outside_block": n_outside, "n_partition_mismatch": n_mismatch}
    return {
        **hard,
        "n_near_duplicate_pairs": n_near,
        "n_unpartitioned": n_unpartitioned,
        "adjacent_cross_partition_block_pairs": n_adj,
        "n_patches": len(patches),
        "passed": all(v == 0 for v in hard.values()),
        "definitions": {
            "n_duplicate_exact": "identical bbox listed twice",
            "n_near_duplicate_pairs": f"pairs with 3-D IoU >= {near_dup_iou} (soft)",
            "n_overlap_cross_partition": "overlapping patches in different holdout partitions (leak)",
            "n_outside_block": "patch not fully inside its block (would straddle a partition boundary)",
            "n_partition_mismatch": "patch partition differs from its block's",
            "n_unpartitioned": "patches in blocks with partition none/excluded (allowed for failure patches only)",
            "adjacent_cross_partition_block_pairs": "blocks of different partitions sharing a face (tissue context leaks across, soft)",
        },
    }
