"""QC framework primitives: severities, findings, contexts, check base class and registry.

Score convention
  Every implemented check emits a *quality score* in [0, 1] per slice (1 = perfect, 0 = unusable).
  Severity is derived from the score with per-check thresholds (defaults below).
  Checks that are not implemented yet emit score `None` so the gap is visible in the results.
  Serial checks may also emit *block-level* findings (z = NULL), e.g. "sections are uncorrelated".

Coordinate convention
  z is dataset-relative and 0-based; absolute section index = z + dataset.z_offset.
  bbox = [x0, y0, x1, y1] in full-resolution pixels, end-exclusive. Serial findings carry z (later slice) and z_to.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Iterable

import numpy as np


class Severity(IntEnum):
    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def from_label(cls, s: str | None) -> "Severity":
        return cls[(s or "none").upper()]


# score >= low -> NONE ; < low -> LOW ; < medium -> MEDIUM ; < high -> HIGH ; < critical -> CRITICAL
DEFAULT_THRESHOLDS = {"low": 0.75, "medium": 0.6, "high": 0.4, "critical": 0.2}


def severity_from_score(score: float | None, thresholds: dict | None = None) -> Severity:
    if score is None:
        return Severity.NONE
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if score < t["critical"]:
        return Severity.CRITICAL
    if score < t["high"]:
        return Severity.HIGH
    if score < t["medium"]:
        return Severity.MEDIUM
    if score < t["low"]:
        return Severity.LOW
    return Severity.NONE


@dataclass
class Finding:
    check: str
    failure_type: str
    level: str  # slice | serial | block
    stage: str
    severity: Severity
    score: float | None
    z: int | None = None
    z_to: int | None = None
    coordinate: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    preview_path: str | None = None

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "failure_type": self.failure_type,
            "level": self.level,
            "stage": self.stage,
            "severity": self.severity.label,
            "score": self.score,
            "z": self.z,
            "z_to": self.z_to,
            "coordinate": self.coordinate,
            "details": self.details,
            "preview_path": self.preview_path,
        }


@dataclass
class SliceRecord:
    z: int
    status: str = "ok"  # ok | missing | corrupt
    reference_only: bool = False  # 上一段末尾带过来的切片：只当序列比较的参考，不评分、不落库
    error: str | None = None
    stats: dict = field(default_factory=dict)
    thumb: np.ndarray | None = None  # preview thumbnail, float32 in [0,1], long side <= config.thumb_px
    sthumb: np.ndarray | None = None  # serial-analysis thumbnail at ~config.serial_target_nm per pixel
    scores: dict = field(default_factory=dict)  # check name -> score | None
    findings: list[Finding] = field(default_factory=list)
    bytes_read: int = 0
    # filled by aggregate stage
    quality_score: float | None = None
    max_severity: Severity = Severity.NONE
    failure_types: list[str] = field(default_factory=list)
    passed: bool = True
    preview_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def severity_of(self, checks: Iterable[str] | None = None, level: str | None = None) -> Severity:
        """Worst severity among this slice's findings (optionally restricted to some checks / a level)."""
        cs = set(checks) if checks is not None else None
        return max((f.severity for f in self.findings if (cs is None or f.check in cs) and (level is None or f.level == level)), default=Severity.NONE)

    def note(self, check: str, text: str) -> None:
        self.stats.setdefault("_notes", {})[check] = text


@dataclass
class DatasetInfo:
    dataset_id: str
    shape: tuple[int, int, int]  # (z, y, x)
    dtype: str
    z_offset: int = 0
    voxel_size_nm: tuple[float, float, float] | None = None  # (x, y, z)
    size_class: str = "small"
    fill_value: int | float | None = 0  # pixel value used for "no data" regions (cracks, missing tiles); None = disabled

    @property
    def dtype_max(self) -> float:
        dt = np.dtype(self.dtype)
        return float(np.iinfo(dt).max) if dt.kind in "ui" else 1.0

    @property
    def nm_per_px_xy(self) -> float | None:
        if not self.voxel_size_nm:
            return None
        return float((self.voxel_size_nm[0] + self.voxel_size_nm[1]) / 2.0)


@dataclass
class BlockInfo:
    block_id: str
    z_start: int
    z_end: int
    y_start: int = 0
    y_end: int = 0
    x_start: int = 0
    x_end: int = 0

    @property
    def n_slices(self) -> int:
        return self.z_end - self.z_start


@dataclass
class QCConfig:
    thresholds: dict[str, dict] = field(default_factory=dict)  # per-check severity thresholds
    params: dict[str, dict] = field(default_factory=dict)  # per-check parameters
    thumb_px: int = 256  # preview thumbnails (long side)
    serial_target_nm: float = 32.0  # serial checks compare sections at about this physical pixel size
    serial_max_px: int = 512  # ... but never more than this many pixels on the long side
    serial_min_px: int = 48  # ... and never fewer than this many on the short side
    max_stat_pixels: int = 4_000_000  # subsample bigger slices for statistics
    pass_max_severity: Severity = Severity.HIGH  # a slice passes if its worst severity is below this
    finding_min_severity: Severity = Severity.LOW  # record findings at or above this
    preview_min_severity: Severity = Severity.MEDIUM  # save slice thumbnails at or above this
    checks_enabled: list[str] | None = None  # None = all
    tile_xy: int = 0  # nominal XY tile size (settings.block_size_xy); edge tiles are smaller but thresholds use this

    @classmethod
    def from_dict(cls, d: dict | None) -> "QCConfig":
        """Build from the JSON a client sends (unknown keys ignored, severities as labels)."""
        d = dict(d or {})
        kw: dict = {}
        for k in ("thresholds", "params", "thumb_px", "serial_target_nm", "serial_max_px", "serial_min_px", "max_stat_pixels", "checks_enabled", "tile_xy"):
            if d.get(k) is not None:
                kw[k] = d[k]
        for k in ("pass_max_severity", "finding_min_severity", "preview_min_severity"):
            if d.get(k):
                kw[k] = Severity.from_label(d[k])
        return cls(**kw)

    def as_dict(self) -> dict:
        return {
            "thresholds": self.thresholds,
            "params": self.params,
            "thumb_px": self.thumb_px,
            "serial_target_nm": self.serial_target_nm,
            "serial_max_px": self.serial_max_px,
            "serial_min_px": self.serial_min_px,
            "max_stat_pixels": self.max_stat_pixels,
            "pass_max_severity": self.pass_max_severity.label,
            "finding_min_severity": self.finding_min_severity.label,
            "preview_min_severity": self.preview_min_severity.label,
            "checks_enabled": self.checks_enabled,
            "tile_xy": self.tile_xy,
        }


class BlockContext:
    """Everything a check needs about one block. Slices keep only stats + thumbnails after ingest."""

    def __init__(self, ds: DatasetInfo, block: BlockInfo, config: QCConfig):
        self.ds = ds
        self.block = block
        self.config = config
        self.slices: list[SliceRecord] = [SliceRecord(z=z) for z in range(block.z_start, block.z_end)]
        # 上一段末尾的切片（reference_only），用来消除 z 段边界上的盲缝：
        # 不带它的话，每个 block 的第一张切片没有参考，块长 64 的万层数据会有上百个盲缝
        self.pre: list[SliceRecord] = []
        self.block_findings: list[Finding] = []
        self.cache: dict[str, Any] = {}  # shared intermediate results (e.g. pairwise shifts)
        self.stage_durations: dict[str, float] = {}
        self.serial_factor: int = 1  # full-res pixels per serial-thumbnail pixel (isotropic, integer)

    def rec(self, z: int) -> SliceRecord:
        return self.slices[z - self.block.z_start]

    def good(self) -> list[SliceRecord]:
        return [r for r in self.slices if r.ok]

    def stat(self, name: str) -> np.ndarray:
        """Array over the block's slices (NaN where the slice is not ok / stat missing)."""
        out = np.full(len(self.slices), np.nan, dtype=np.float64)
        for i, r in enumerate(self.slices):
            if r.ok and name in r.stats and r.stats[name] is not None:
                out[i] = r.stats[name]
        return out

    def median_stat(self, name: str) -> float:
        a = self.stat(name)
        return float(np.nanmedian(a)) if np.isfinite(a).any() else float("nan")

    @property
    def extent(self) -> tuple[int, int]:
        """(H, W) of this block's XY tile in full-res pixels."""
        b = self.block
        H = (b.y_end - b.y_start) if b.y_end > b.y_start else self.ds.shape[1]
        W = (b.x_end - b.x_start) if b.x_end > b.x_start else self.ds.shape[2]
        return (H, W)

    @property
    def is_tile(self) -> bool:
        H, W = self.extent
        return (H, W) != (self.ds.shape[1], self.ds.shape[2])

    @property
    def nominal_short(self) -> float:
        """Short side of a *full* tile in pixels. Edge tiles are cropped, but pixel-fraction thresholds must not
        shrink with them, otherwise a 200-px rim tile flags a 12-px shift as critical."""
        if self.is_tile and self.config.tile_xy > 0:
            return float(min(self.config.tile_xy, max(self.ds.shape[1], self.ds.shape[2])))
        return float(min(self.ds.shape[1], self.ds.shape[2]))

    def to_dataset_bbox(self, bbox: list | None) -> list | None:
        """block-local [x0, y0, x1, y1] -> dataset-frame pixels."""
        if not bbox:
            return bbox
        ox, oy = self.block.x_start, self.block.y_start
        return [int(bbox[0] + ox), int(bbox[1] + oy), int(bbox[2] + ox), int(bbox[3] + oy)]

    @property
    def scale_yx(self) -> tuple[float, float]:
        """full-res pixels per *preview* thumbnail pixel (y, x)."""
        H, W = self.extent
        for r in self.slices:
            if r.thumb is not None:
                return (H / r.thumb.shape[0], W / r.thumb.shape[1])
        return (1.0, 1.0)

    @property
    def serial_nm_per_px(self) -> float | None:
        base = self.ds.nm_per_px_xy
        return None if base is None else base * self.serial_factor


class QCCheck(ABC):
    name: str = ""
    failure_type: str = ""
    level: str = "slice"  # slice | serial
    stage: str = "slice_qc"  # slice_qc | serial_qc
    description: str = ""
    implemented: bool = True
    maturity: str = "heuristic"  # stub | heuristic | stable
    planned: str = ""  # for stubs: what the real algorithm will do
    default_params: dict = {}
    default_thresholds: dict | None = None

    def __init__(self, params: dict | None = None, thresholds: dict | None = None):
        self.params = {**self.default_params, **(params or {})}
        self.thresholds = {**DEFAULT_THRESHOLDS, **(self.default_thresholds or {}), **(thresholds or {})}

    # -- helpers -----------------------------------------------------------------
    def severity(self, score: float | None) -> Severity:
        return severity_from_score(score, self.thresholds)

    def emit(self, ctx: BlockContext, rec: SliceRecord, score: float | None, *, z_to: int | None = None, coordinate: dict | None = None, details: dict | None = None, failure_type: str | None = None) -> Severity:
        """Store the score on the slice and add a finding when the severity warrants it."""
        rec.scores[self.name] = None if score is None else float(np.clip(score, 0.0, 1.0))
        sev = self.severity(rec.scores[self.name])
        if sev >= ctx.config.finding_min_severity:
            coord = {"z": rec.z, "z_abs": rec.z + ctx.ds.z_offset, "bbox": None}
            if coordinate:
                coord.update(coordinate)
            if coord.get("bbox"):
                coord["bbox"] = ctx.to_dataset_bbox(coord["bbox"])  # checks work in block-local pixels
            rec.findings.append(
                Finding(check=self.name, failure_type=failure_type or self.failure_type, level=self.level, stage=self.stage, severity=sev, score=rec.scores[self.name], z=rec.z, z_to=z_to, coordinate=coord, details=details or {})
            )
        return sev

    def emit_block(self, ctx: BlockContext, severity: Severity, score: float | None = None, *, details: dict | None = None) -> None:
        """A finding about the block as a whole (persisted with z = NULL)."""
        ctx.block_findings.append(
            Finding(
                check=self.name, failure_type=self.failure_type, level="block", stage=self.stage, severity=severity, score=score,
                coordinate={"z_start": ctx.block.z_start, "z_end": ctx.block.z_end, "bbox": [ctx.block.x_start, ctx.block.y_start, ctx.block.x_end, ctx.block.y_end]},
                details=details or {},
            )
        )

    def skip(self, rec: SliceRecord, note: str | None = None) -> None:
        rec.scores[self.name] = None
        if note:
            rec.note(self.name, note)

    # -- interface ---------------------------------------------------------------
    @abstractmethod
    def run(self, ctx: BlockContext) -> None:
        """Evaluate every slice of the block; write scores / findings onto the SliceRecords."""

    @classmethod
    def describe(cls) -> dict:
        return {
            "name": cls.name,
            "failure_type": cls.failure_type,
            "failure_types": list(getattr(cls, "failure_types", (cls.failure_type,))),
            "level": cls.level,
            "stage": cls.stage,
            "description": cls.description,
            "implemented": cls.implemented,
            "maturity": cls.maturity,
            "planned": cls.planned,
            "default_params": cls.default_params,
            "thresholds": {**DEFAULT_THRESHOLDS, **(cls.default_thresholds or {})},
        }


class StubCheck(QCCheck):
    """A check whose algorithm is not implemented yet: records `None` scores, never fails a slice."""

    implemented = False
    maturity = "stub"

    def run(self, ctx: BlockContext) -> None:
        for r in ctx.slices:
            self.skip(r)


# ----------------------------------------------------------------------------- registry

SLICE_CHECKS: list[type[QCCheck]] = []
SERIAL_CHECKS: list[type[QCCheck]] = []


def register(cls: type[QCCheck]) -> type[QCCheck]:
    target = SLICE_CHECKS if cls.level == "slice" else SERIAL_CHECKS
    if all(c.name != cls.name for c in target):
        target.append(cls)
    return cls


def all_checks() -> list[type[QCCheck]]:
    return [*SLICE_CHECKS, *SERIAL_CHECKS]


def catalog() -> list[dict]:
    return [c.describe() for c in all_checks()]


def build_checks(config: QCConfig, classes: Iterable[type[QCCheck]]) -> list[QCCheck]:
    out = []
    for cls in classes:
        if config.checks_enabled is not None and cls.name not in config.checks_enabled:
            continue
        out.append(cls(params=config.params.get(cls.name), thresholds=config.thresholds.get(cls.name)))
    return out


# ----------------------------------------------------------------------------- shared helpers


def bbox_from_mask(mask: np.ndarray, scale_yx: tuple[float, float]) -> list[int] | None:
    """[x0, y0, x1, y1] (full-res pixels) of the True region of a thumbnail mask."""
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None
    sy, sx = scale_yx
    return [int(xs.min() * sx), int(ys.min() * sy), int((xs.max() + 1) * sx), int((ys.max() + 1) * sy)]


def clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


# slice-level checks whose HIGH+ findings make a slice unusable as a *reference* for neighbour comparisons
REFERENCE_BLOCKERS = ("missing_slice", "corrupt_slice", "blank_slice", "severe_blur", "saturation", "crack")


def reference_chain(ctx: BlockContext, extra_blockers: Iterable[str] = ()) -> list[SliceRecord]:
    """Slices that are ok and not disqualified by slice-level checks -> usable for neighbour comparisons."""
    blockers = set(REFERENCE_BLOCKERS) | set(extra_blockers)
    # 参考链包含上一段带过来的切片；它们只当基准，本身不被评估（见 pairwise 的 evaluated）
    return [r for r in (ctx.pre + ctx.slices) if r.ok and r.severity_of(blockers) < ctx.config.pass_max_severity]


def pick_reference(k: int, outlier: list[bool]) -> int | None:
    """Two-back reference rule for the slice that sits at chain position k (k = number of chain slices
    before it): the previous chain slice unless it was an outlier, then the one before; else reset."""
    if k >= 1 and not outlier[k - 1]:
        return k - 1
    if k >= 2 and not outlier[k - 2]:
        return k - 2
    return None


def chain_position(chain: list[SliceRecord], rec: SliceRecord) -> int:
    """Number of chain slices strictly before rec (== its own index when rec is a chain member)."""
    import bisect

    return bisect.bisect_left([c.z for c in chain], rec.z)
