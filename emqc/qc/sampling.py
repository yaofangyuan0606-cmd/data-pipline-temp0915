"""Which blocks of a *large* dataset become training samples.

Requirement (from the user): small datasets are used whole for training; large datasets contribute a
*sample* of blocks to training and the whole volume to inference.
Sampling policy (user decision 2026-09-12): **stratified by grade** - a fixed share of the sample comes
from each grade so the training set sees clean blocks as well as usable-but-flawed ones; grade D never
qualifies. Within a stratum blocks are drawn at random with a fixed seed, so the choice is reproducible.
Other policies are kept selectable for experiments.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

GRADE_ORDER = ("A", "B", "C")


def parse_strata(spec: str) -> dict[str, float]:
    """'A:0.5,B:0.3,C:0.2' -> {'A': .5, 'B': .3, 'C': .2} (normalised, D ignored)."""
    out: dict[str, float] = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        g, _, w = part.partition(":")
        g = g.strip().upper()
        if g in GRADE_ORDER:
            out[g] = max(0.0, float(w or 0))
    total = sum(out.values())
    if total <= 0:
        return {"A": 0.5, "B": 0.3, "C": 0.2}
    return {g: w / total for g, w in out.items()}


@dataclass
class SamplingResult:
    policy: str
    n_requested: int
    quota: dict[str, int] = field(default_factory=dict)  # planned per grade
    chosen: dict[str, list[str]] = field(default_factory=dict)  # actual per grade
    available: dict[str, int] = field(default_factory=dict)
    seed: int | None = None
    note: str = ""

    @property
    def block_ids(self) -> set[str]:
        return {b for ids in self.chosen.values() for b in ids}

    def as_dict(self) -> dict:
        return {"policy": self.policy, "n_requested": self.n_requested, "quota": self.quota, "chosen": self.chosen, "available": self.available, "seed": self.seed, "note": self.note}


def select_train_blocks(blocks: list, *, policy: str = "stratified", n: int = 8, strata: str = "A:0.5,B:0.3,C:0.2", seed: int | None = 42) -> SamplingResult:
    """blocks: objects with .block_id, .grade, .retention_rate, .quality_score (QCBlock rows or alike)."""
    usable = [b for b in blocks if (getattr(b, "grade", None) or "D") in GRADE_ORDER]
    by_grade = {g: sorted((b for b in usable if b.grade == g), key=lambda b: b.block_id) for g in GRADE_ORDER}
    res = SamplingResult(policy=policy, n_requested=n, available={g: len(v) for g, v in by_grade.items()}, seed=seed)
    n = max(0, min(n, len(usable)))
    if n == 0:
        res.note = "no usable (A/B/C) blocks"
        return res
    rng = random.Random(seed)
    if policy == "best":
        ranked = sorted(usable, key=lambda b: (GRADE_ORDER.index(b.grade), -(b.retention_rate or 0), -(b.quality_score or 0)))
        for b in ranked[:n]:
            res.chosen.setdefault(b.grade, []).append(b.block_id)
        res.note = "best grade first, then retention, then quality"
        return res
    if policy == "random_usable":
        picks = rng.sample(usable, n)
        for b in picks:
            res.chosen.setdefault(b.grade, []).append(b.block_id)
        res.note = "uniform random over A/B/C"
        return res
    # ---- stratified (default)
    weights = parse_strata(strata)
    quota = {g: int(round(n * weights.get(g, 0.0))) for g in GRADE_ORDER}
    # rounding drift -> fix on the largest stratum
    drift = n - sum(quota.values())
    if drift:
        g_fix = max(weights, key=weights.get)
        quota[g_fix] += drift
    res.quota = dict(quota)
    chosen: dict[str, list[str]] = {}
    leftover = 0
    for g in GRADE_ORDER:  # take what each stratum can give, carry the shortfall forward
        want = quota[g] + leftover
        pool = list(by_grade[g])
        take = min(want, len(pool))
        chosen[g] = [b.block_id for b in rng.sample(pool, take)] if take else []
        leftover = want - take
    if leftover:  # strata later in the order were short: go back and top up earlier ones
        for g in GRADE_ORDER:
            pool = [b for b in by_grade[g] if b.block_id not in chosen[g]]
            take = min(leftover, len(pool))
            if take:
                chosen[g].extend(b.block_id for b in rng.sample(pool, take))
                leftover -= take
            if leftover == 0:
                break
    res.chosen = {g: ids for g, ids in chosen.items() if ids}
    res.note = "stratified by grade, random within stratum; shortfalls carried to the next grade"
    return res
