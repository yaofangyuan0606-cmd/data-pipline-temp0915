"""Holdout partition (train / val / test) and difficulty per block.

Two orthogonal axes live on `blocks`:
    split      USAGE   - unassigned | train | train_sample | inference   (requirement 1: small datasets train,
                         large datasets: a graded sample trains, the whole volume is inferred)
    partition  HOLDOUT - none | train | val | test | excluded             (requirement 3: train/val/test split)

Rules
  * only training-eligible blocks (split in train/train_sample) with grade A/B/C get a holdout partition;
    grade D -> excluded, inference-only / ungraded -> none
  * assignment is by *block*, never by patch: block boundaries are hard cuts, patches never straddle blocks,
    so patch-level overlap leakage across partitions is impossible by construction
  * stable: a block keeps its partition once assigned (re-running QC must not reshuffle the test set);
    newly eligible blocks fill whatever the ratios are short of; `force=True` reassigns everything
  * deterministic: blocks are visited in the order of sha1(f"{seed}:{block_id}")
  * a block that later drops to grade D is moved to `excluded` and its old partition is recorded
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

USABLE_GRADES = ("A", "B", "C")
PARTITIONS = ("train", "val", "test")


def parse_ratios(spec: str | tuple | list) -> tuple[float, float, float]:
    if isinstance(spec, str):
        parts = [float(x) for x in spec.replace(":", ",").split(",") if x.strip()]
    else:
        parts = [float(x) for x in spec]
    if len(parts) != 3 or any(p < 0 for p in parts) or sum(parts) <= 0:
        raise ValueError(f"partition ratios must be three non-negative numbers (train,val,test), got {spec!r}")
    s = sum(parts)
    return (parts[0] / s, parts[1] / s, parts[2] / s)


def targets(n: int, ratios: tuple[float, float, float]) -> dict[str, int]:
    """Largest-remainder split of n blocks; val and test get at least one block each once n >= 3."""
    if n <= 0:
        return {"train": 0, "val": 0, "test": 0}
    if n < 3:
        return {"train": n, "val": 0, "test": 0}
    raw = {p: n * r for p, r in zip(PARTITIONS, ratios)}
    out = {p: int(v) for p, v in raw.items()}
    for p in ("val", "test"):
        if ratios[PARTITIONS.index(p)] > 0 and out[p] == 0:
            out[p] = 1
    while sum(out.values()) > n:  # the minimums may have overshot: take from train first
        for p in ("train", "val", "test"):
            if sum(out.values()) > n and out[p] > (1 if p != "train" else 0):
                out[p] -= 1
    rema = sorted(PARTITIONS, key=lambda p: raw[p] - int(raw[p]), reverse=True)
    i = 0
    while sum(out.values()) < n:
        out[rema[i % 3]] += 1
        i += 1
    return out


def _order_key(seed: int, block_id: str) -> str:
    return hashlib.sha1(f"{seed}:{block_id}".encode()).hexdigest()


@dataclass
class PartitionResult:
    scheme: str = "block-stratified-stable"
    ratios: tuple[float, float, float] = (0.8, 0.1, 0.1)
    seed: int = 42
    n_eligible: int = 0
    counts: dict = field(default_factory=dict)
    changed: dict = field(default_factory=dict)  # block_id -> (old, new)
    excluded: list = field(default_factory=list)
    adjacent_cross_partition_pairs: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return {"scheme": self.scheme, "ratios": list(self.ratios), "seed": self.seed, "n_eligible": self.n_eligible, "counts": self.counts,
                "n_changed": len(self.changed), "excluded": self.excluded, "adjacent_cross_partition_pairs": self.adjacent_cross_partition_pairs, "note": self.note}


def _adjacent(a, b) -> bool:
    """Two blocks share a face (touch in exactly one axis while overlapping in the other two)."""
    def touch(a0, a1, b0, b1):
        return a1 == b0 or b1 == a0

    def overlap(a0, a1, b0, b1):
        return a0 < b1 and b0 < a1

    z = touch(a.z_start, a.z_end, b.z_start, b.z_end), overlap(a.z_start, a.z_end, b.z_start, b.z_end)
    y = touch(a.y_start, a.y_end, b.y_start, b.y_end), overlap(a.y_start, a.y_end, b.y_start, b.y_end)
    x = touch(a.x_start, a.x_end, b.x_start, b.x_end), overlap(a.x_start, a.x_end, b.x_start, b.x_end)
    return (z[0] and y[1] and x[1]) or (y[0] and z[1] and x[1]) or (x[0] and z[1] and y[1])


def assign_partitions(blocks: list, ratios=(0.8, 0.1, 0.1), seed: int = 42, force: bool = False) -> PartitionResult:
    """Mutates `partition` / `partition_seed` on the block rows. Works on ORM rows or any object with the same attributes."""
    ratios = parse_ratios(ratios)
    res = PartitionResult(ratios=ratios, seed=seed)
    eligible, current = [], {}
    for b in blocks:
        old = b.partition or "none"
        if b.split in ("train", "train_sample") and (b.latest_grade or "") in USABLE_GRADES:
            eligible.append(b)
        elif (b.latest_grade or "") == "D" and b.split in ("train", "train_sample"):
            if old != "excluded":
                res.changed[b.block_id] = (old, "excluded")
                res.excluded.append(b.block_id)
            b.partition = "excluded"
        else:
            if old not in ("none",):
                res.changed[b.block_id] = (old, "none")
            b.partition = "none"
    res.n_eligible = len(eligible)
    tgt = targets(len(eligible), ratios)
    if force:
        for b in eligible:
            b.partition = "none"
    have = {p: sum(1 for b in eligible if b.partition == p) for p in PARTITIONS}
    for b in eligible:  # an eligible block that somehow carries a non-partition value gets reassigned
        if b.partition not in PARTITIONS:
            b.partition = "none"
    new = sorted((b for b in eligible if b.partition == "none"), key=lambda b: _order_key(seed, b.block_id))
    for b in new:
        deficits = {p: tgt[p] - have[p] for p in PARTITIONS}
        pick = max(PARTITIONS, key=lambda p: (deficits[p], p == "train"))
        if deficits[pick] <= 0:
            pick = "train"
        old = "none"
        b.partition, b.partition_seed = pick, seed
        have[pick] += 1
        res.changed[b.block_id] = (old, pick)
    res.counts = {**have, "excluded": sum(1 for b in blocks if b.partition == "excluded"), "none": sum(1 for b in blocks if b.partition == "none")}
    parted = [b for b in eligible if b.partition in PARTITIONS]
    res.adjacent_cross_partition_pairs = sum(1 for i, a in enumerate(parted) for b in parted[i + 1 :] if a.partition != b.partition and _adjacent(a, b))
    if len(eligible) < 3:
        res.note = "fewer than 3 eligible blocks: everything is train, no holdout possible yet"
    elif res.adjacent_cross_partition_pairs:
        res.note = "block-level split: no patch straddles partitions, but adjacent blocks share tissue context (see adjacent_cross_partition_pairs)"
    return res


def difficulty_of(quality: float | None, retention: float | None, n_flagged_medium_plus: int, n_slices: int, grade: str | None) -> tuple[float | None, dict]:
    """0 (easy) .. 1 (hard). Weighted: 50% lost quality, 30% lost retention, 20% density of medium+ findings.
    Grade D is pinned to >= 0.9 so it never sorts below a C block."""
    if quality is None and retention is None:
        return None, {}
    q = 1.0 - float(quality if quality is not None else 0.0)
    r = 1.0 - float(retention if retention is not None else 0.0)
    dens = min(1.0, n_flagged_medium_plus / n_slices) if n_slices else 0.0
    d = 0.5 * q + 0.3 * r + 0.2 * dens
    if grade == "D":
        d = max(d, 0.9)
    d = float(min(1.0, max(0.0, d)))
    return d, {"lost_quality": round(q, 4), "lost_retention": round(r, 4), "flag_density": round(dens, 4), "weights": [0.5, 0.3, 0.2], "grade": grade, "formula": "0.5*(1-quality) + 0.3*(1-retention) + 0.2*min(1, n_medium_plus/n_slices); D >= 0.9"}
