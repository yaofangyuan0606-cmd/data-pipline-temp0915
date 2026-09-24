"""MySQL schema for the dataset registry and the QC pipeline results.

Entity map
  datasets ─┬─ dataset_assets     (EM / GT / prediction / synapse / mito / skeleton / agent trace files)
            ├─ dataset_versions   (data / algo / model / experiment versions)
            ├─ blocks             (serial-section sequences; QC unit for large datasets)
            └─ qc_runs ─┬─ qc_slices    (one row per slice per run: multi-score, failure types, severity, coords, preview)
                        ├─ qc_blocks    (one row per block per run: aggregated scores + retention)
                        ├─ qc_findings  (one row per detected problem)
                        └─ etl_metrics  (retention rate, counts, durations per stage)
  agent_traces  (execution traces of agents/algorithms that touched a dataset)
  serve_log     (every cutout handed to the algorithm side -> data lineage)
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Dataset(Base):
    __tablename__ = "datasets"

    dataset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    project: Mapped[str] = mapped_column(String(128), default="")
    root_path: Mapped[str] = mapped_column(String(1024))
    em_path: Mapped[str] = mapped_column(String(1024), default="")
    em_format: Mapped[str] = mapped_column(String(32), default="image_stack")  # image_stack | npy | precomputed
    em_axes: Mapped[str] = mapped_column(String(8), default="zyx")
    size_class: Mapped[str] = mapped_column(String(16), default="small")  # small | large
    usage: Mapped[str] = mapped_column(String(32), default="train")  # train | inference | train+inference
    dtype: Mapped[str] = mapped_column(String(16), default="uint8")
    size_x: Mapped[int] = mapped_column(Integer, default=0)
    size_y: Mapped[int] = mapped_column(Integer, default=0)
    size_z: Mapped[int] = mapped_column(Integer, default=0)
    n_voxels: Mapped[int] = mapped_column(BigInteger, default=0)
    # dataset metadata
    species: Mapped[str | None] = mapped_column(String(64))
    brain_region: Mapped[str | None] = mapped_column(String(128))
    voxel_size_x_nm: Mapped[float | None] = mapped_column(Float)
    voxel_size_y_nm: Mapped[float | None] = mapped_column(Float)
    voxel_size_z_nm: Mapped[float | None] = mapped_column(Float)
    staining: Mapped[str | None] = mapped_column(String(128))
    imaging_modality: Mapped[str | None] = mapped_column(String(64))
    acquisition_batch: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    data_version: Mapped[str] = mapped_column(String(64), default="v1")
    status: Mapped[str] = mapped_column(String(32), default="registered")  # registered | qc_running | qc_done | error
    latest_run_id: Mapped[int | None] = mapped_column(Integer)
    latest_quality_score: Mapped[float | None] = mapped_column(Float)
    latest_retention_rate: Mapped[float | None] = mapped_column(Float)
    latest_grade: Mapped[str | None] = mapped_column(String(2))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    assets: Mapped[list["DatasetAsset"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")
    versions: Mapped[list["DatasetVersion"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")
    blocks: Mapped[list["Block"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")


ASSET_TYPES = (
    "em_image",
    "gt_segmentation",
    "gt_annotation",  # class masks / proofread labels (axon, dendrite, synapse, vesicle, ...) -> extra_json.class
    "model_prediction",
    "synapse_prediction",
    "mitochondria_prediction",
    "organelle_prediction",
    "skeleton",
    "agent_trace",
    "other",
)


class DatasetAsset(Base):
    __tablename__ = "dataset_assets"
    __table_args__ = (UniqueConstraint("dataset_id", "asset_type", "path", name="uq_asset"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.dataset_id", ondelete="CASCADE"), index=True)
    asset_type: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(String(512))
    format: Mapped[str] = mapped_column(String(32), default="")
    version: Mapped[str] = mapped_column(String(64), default="")
    algo_version: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(64))
    experiment_id: Mapped[str | None] = mapped_column(String(128))
    exists: Mapped[bool] = mapped_column(Boolean, default=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    dataset: Mapped[Dataset] = relationship(back_populates="assets")


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "kind", "version", name="uq_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.dataset_id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # data | algo | model | experiment | qc_pipeline
    version: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    dataset: Mapped[Dataset] = relationship(back_populates="versions")


class Block(Base):
    """A block is a contiguous serial-section sequence (z range) over an XY region.

    v0.1 tiles along Z only (full XY plane); x/y ranges are stored so XY tiling can be added later.
    """

    __tablename__ = "blocks"
    __table_args__ = (UniqueConstraint("dataset_id", "block_id", name="uq_block"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.dataset_id", ondelete="CASCADE"), index=True)
    block_id: Mapped[str] = mapped_column(String(64))  # e.g. z0000-0063
    z_start: Mapped[int] = mapped_column(Integer)
    z_end: Mapped[int] = mapped_column(Integer)  # exclusive
    y_start: Mapped[int] = mapped_column(Integer, default=0)
    y_end: Mapped[int] = mapped_column(Integer, default=0)
    x_start: Mapped[int] = mapped_column(Integer, default=0)
    x_end: Mapped[int] = mapped_column(Integer, default=0)
    n_slices: Mapped[int] = mapped_column(Integer)
    split: Mapped[str] = mapped_column(String(16), default="unassigned")  # USAGE axis: unassigned (before first QC run) | train | train_sample | inference
    partition: Mapped[str] = mapped_column("holdout", String(16), default="none")  # HOLDOUT axis: none | train | val | test | excluded (grade D). Orthogonal to split. (column named holdout: PARTITION is reserved in MySQL)
    partition_seed: Mapped[int | None] = mapped_column(Integer)
    difficulty: Mapped[float | None] = mapped_column(Float)  # 0 easy .. 1 hard, from the latest QC (see patches/partition.difficulty_of)
    difficulty_json: Mapped[dict | None] = mapped_column(JSON)  # the components behind `difficulty`
    label_version: Mapped[str | None] = mapped_column(String(64))  # version of the validated GT label asset this block's patches use
    preprocessing_json: Mapped[dict | None] = mapped_column(JSON)  # declared preprocessing for patches cut from this block
    augmentation_json: Mapped[dict | None] = mapped_column(JSON)  # declared augmentation policy
    source_volume: Mapped[str | None] = mapped_column(String(512))  # URL of the EM volume this block is cut from
    region: Mapped[str | None] = mapped_column(String(128))  # brain region (defaults to the dataset's, may differ per block in large volumes)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending | running | done | error
    latest_quality_score: Mapped[float | None] = mapped_column(Float)
    latest_severity: Mapped[str | None] = mapped_column(String(16))
    latest_retention_rate: Mapped[float | None] = mapped_column(Float)
    latest_grade: Mapped[str | None] = mapped_column(String(2))  # A | B | C | D, see qc/stages.grade_block
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    dataset: Mapped[Dataset] = relationship(back_populates="blocks")


class QCRun(Base):
    __tablename__ = "qc_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.dataset_id", ondelete="CASCADE"), index=True)
    pipeline_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="queued")  # queued | running | done | error
    stage: Mapped[str] = mapped_column(String(128), default="")  # current stage, e.g. "slice_qc · z0-99 · z00000-00099_y01024_x00000"
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    n_blocks: Mapped[int] = mapped_column(Integer, default=0)
    n_blocks_done: Mapped[int] = mapped_column(Integer, default=0)
    quality_score: Mapped[float | None] = mapped_column(Float)
    retention_rate: Mapped[float | None] = mapped_column(Float)
    grade_counts_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {"A": n, "B": n, ...}
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class QCRunEvent(Base):
    """Live log of a run: one row per stage transition / block result / warning (tail it from the pipeline page)."""

    __tablename__ = "qc_run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("qc_runs.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    level: Mapped[str] = mapped_column(String(8), default="info")  # info | warn | error
    stage: Mapped[str | None] = mapped_column(String(32))
    block_id: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(512))
    data_json: Mapped[dict] = mapped_column(JSON, default=dict)  # structured payload: z range, durations, cpu / rss, grade ...


class QCSlice(Base):
    """Per-slice QC record: multi-score + failure types + severity + coordinate + preview."""

    __tablename__ = "qc_slices"
    __table_args__ = (
        UniqueConstraint("run_id", "dataset_id", "block_id", "z", name="uq_qc_slice"),
        Index("ix_qc_slices_ds_z", "dataset_id", "z"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("qc_runs.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    block_id: Mapped[str] = mapped_column(String(64), index=True)
    z: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | missing | corrupt
    scores_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {check_name: score 0..1 | null}
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)  # mean/std/p1/p99/sharpness/...
    failure_types: Mapped[list] = mapped_column(JSON, default=list)
    max_severity: Mapped[str] = mapped_column(String(16), default="none")
    quality_score: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean, default=True)
    coordinate_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {"z":..,"bbox":[x0,y0,x1,y1]|null}
    preview_path: Mapped[str | None] = mapped_column(String(1024))


class QCBlock(Base):
    __tablename__ = "qc_blocks"
    __table_args__ = (UniqueConstraint("run_id", "dataset_id", "block_id", name="uq_qc_block"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("qc_runs.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    block_id: Mapped[str] = mapped_column(String(64), index=True)
    scores_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {check_name: {"min":..,"mean":..}}
    failure_types: Mapped[list] = mapped_column(JSON, default=list)
    max_severity: Mapped[str] = mapped_column(String(16), default="none")
    quality_score: Mapped[float | None] = mapped_column(Float)
    n_slices: Mapped[int] = mapped_column(Integer, default=0)
    n_passed: Mapped[int] = mapped_column(Integer, default=0)
    n_missing: Mapped[int] = mapped_column(Integer, default=0)
    n_corrupt: Mapped[int] = mapped_column(Integer, default=0)
    retention_rate: Mapped[float | None] = mapped_column(Float)
    grade: Mapped[str | None] = mapped_column(String(2))  # A | B | C | D
    longest_clean_run: Mapped[int | None] = mapped_column(Integer)  # max consecutive passed sections
    dominant_failure: Mapped[str | None] = mapped_column(String(64))
    coordinate_json: Mapped[dict] = mapped_column(JSON, default=dict)
    preview_path: Mapped[str | None] = mapped_column(String(1024))
    duration_s: Mapped[float | None] = mapped_column(Float)
    stage_durations_json: Mapped[dict] = mapped_column(JSON, default=dict)


class QCFinding(Base):
    """One row per detected problem (slice-level or serial-level)."""

    __tablename__ = "qc_findings"
    __table_args__ = (Index("ix_findings_ds_type", "dataset_id", "failure_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("qc_runs.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    block_id: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(16))  # slice | serial
    stage: Mapped[str] = mapped_column(String(32))
    check_name: Mapped[str] = mapped_column(String(64))
    failure_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    score: Mapped[float | None] = mapped_column(Float)
    z: Mapped[int | None] = mapped_column(Integer)
    z_to: Mapped[int | None] = mapped_column(Integer)
    coordinate_json: Mapped[dict] = mapped_column(JSON, default=dict)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    preview_path: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ETLMetric(Base):
    """ETL indicators per run / block / stage (retention rate, counts, durations, bytes)."""

    __tablename__ = "etl_metrics"
    __table_args__ = (Index("ix_etl_run_metric", "run_id", "metric_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("qc_runs.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    block_id: Mapped[str | None] = mapped_column(String(64))  # NULL = dataset level
    stage: Mapped[str] = mapped_column(String(32), default="")
    metric_name: Mapped[str] = mapped_column(String(64))
    metric_value: Mapped[float | None] = mapped_column(Float)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AgentTrace(Base):
    """Execution trace of an agent / algorithm step against a dataset."""

    __tablename__ = "agent_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    agent: Mapped[str] = mapped_column(String(128))
    step: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), default="")
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="ok")
    duration_ms: Mapped[float | None] = mapped_column(Float)
    algo_version: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(64))
    experiment_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ServeLog(Base):
    """Every cutout served to the algorithm side (data lineage for training / inference)."""

    __tablename__ = "serve_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # slice | cutout | patch_sample
    bbox_json: Mapped[dict] = mapped_column(JSON, default=dict)
    n_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    fmt: Mapped[str] = mapped_column(String(16), default="npy")
    client: Mapped[str | None] = mapped_column(String(128))
    qc_filter_json: Mapped[dict] = mapped_column(JSON, default=dict)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    stream_id: Mapped[int | None] = mapped_column(Integer, index=True)  # set when a cutout belongs to a stream session
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, server_default=func.now())


class ExportJob(Base):
    """Training-side delivery: a background job that writes the QC-passing sections of a dataset as local shards."""

    __tablename__ = "export_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer)  # QC run the pass/fail decisions come from
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | done | error | cancelled
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)  # min_run, shard_z, min_quality, with_assets, fmt, block_ids, resume
    out_dir: Mapped[str] = mapped_column(String(1024), default="")
    n_blocks: Mapped[int] = mapped_column(Integer, default=0)
    n_blocks_done: Mapped[int] = mapped_column(Integer, default=0)
    n_shards: Mapped[int] = mapped_column(Integer, default=0)
    n_shards_reused: Mapped[int] = mapped_column(Integer, default=0)
    n_sections: Mapped[int] = mapped_column(Integer, default=0)
    n_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    n_asset_files: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ExportEvent(Base):
    __tablename__ = "export_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("export_jobs.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    level: Mapped[str] = mapped_column(String(8), default="info")
    block_id: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(512))
    data_json: Mapped[dict] = mapped_column(JSON, default=dict)


class StreamSession(Base):
    """Inference-side delivery: a client walks the whole volume block by block through cutouts, nothing is copied.

    The plan is the ordered list of cutouts; `cursor` is how far the client has been handed items; acks record what it
    actually processed. Everything served is also in serve_log, so the lineage of an inference run is complete.
    """

    __tablename__ = "stream_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer)  # QC run whose grades / pass flags annotate the plan
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | done | aborted
    client: Mapped[str | None] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(32), default="inference")
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)  # z_chunk, block_ids, order, skip_failed
    plan_json: Mapped[list] = mapped_column(JSON, default=list)  # [{i, block_id, z0, z1, y0, y1, x0, x1, url, grade, n_failed}]
    n_items: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[int] = mapped_column(Integer, default=0)  # items handed out
    n_acked: Mapped[int] = mapped_column(Integer, default=0)
    n_failed_items: Mapped[int] = mapped_column(Integer, default=0)
    n_bytes: Mapped[int] = mapped_column(BigInteger, default=0)  # bytes served to this session (from cutout calls that quote it)
    est_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class LabelQC(Base):
    """Per-section result of validating a label asset (GT segmentation, class mask, prediction) against the EM volume."""

    __tablename__ = "label_qc"
    __table_args__ = (UniqueConstraint("asset_id", "z", name="uq_label_qc"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("dataset_assets.id", ondelete="CASCADE"), index=True)
    z: Mapped[int] = mapped_column(Integer)
    present: Mapped[bool] = mapped_column(Boolean, default=True)  # a label file exists for this z
    shape_ok: Mapped[bool] = mapped_column(Boolean, default=True)  # label shape matches EM (after the declared scale)
    em_status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | missing | corrupt | blank
    n_ids: Mapped[int | None] = mapped_column(Integer)  # distinct label values on the section (subsampled)
    frac_background: Mapped[float | None] = mapped_column(Float)
    frac_label_in_fill: Mapped[float | None] = mapped_column(Float)  # label painted where the EM has no data
    issues: Mapped[list] = mapped_column(JSON, default=list)  # label_missing | label_empty | label_in_fill | shape_mismatch | label_without_image | encoding_inconsistent
    passed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


PATCH_TYPES = ("segmentation", "membrane", "synapse", "mitochondria", "proofreading", "hard_negative", "failure")


class PatchSet(Base):
    """One generated set of training patches: coordinates + lineage, never pixels (those are cut on demand)."""

    __tablename__ = "patch_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    patch_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="done")  # done | empty | needs_prediction_asset | no_aligned_label | error
    reason: Mapped[str | None] = mapped_column(String(512))
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)  # size, n, seed, partition filter, acceptance thresholds
    qc_run_id: Mapped[int | None] = mapped_column(Integer)  # QC run the pass/fail filter came from
    label_asset_id: Mapped[int | None] = mapped_column(Integer)
    label_version: Mapped[str | None] = mapped_column(String(64))
    source_volume: Mapped[str | None] = mapped_column(String(512))
    preprocessing_json: Mapped[dict] = mapped_column(JSON, default=dict)
    augmentation_json: Mapped[dict] = mapped_column(JSON, default=dict)
    n_patches: Mapped[int] = mapped_column(Integer, default=0)
    counts_json: Mapped[dict] = mapped_column(JSON, default=dict)  # per partition / per block / per failure type
    checks_json: Mapped[dict] = mapped_column(JSON, default=dict)  # duplicate / overlap / leakage results
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Patch(Base):
    __tablename__ = "patches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    set_id: Mapped[int] = mapped_column(ForeignKey("patch_sets.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    block_id: Mapped[str] = mapped_column(String(64), index=True)
    patch_type: Mapped[str] = mapped_column(String(32))
    z0: Mapped[int] = mapped_column(Integer)
    z1: Mapped[int] = mapped_column(Integer)
    y0: Mapped[int] = mapped_column(Integer)
    y1: Mapped[int] = mapped_column(Integer)
    x0: Mapped[int] = mapped_column(Integer)
    x1: Mapped[int] = mapped_column(Integer)
    partition: Mapped[str] = mapped_column("holdout", String(16), default="none")
    difficulty: Mapped[float | None] = mapped_column(Float)
    grade: Mapped[str | None] = mapped_column(String(2))
    region: Mapped[str | None] = mapped_column(String(128))
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)  # per-type evidence: n_ids, boundary_frac, failure_type, severity ...


class CrawlJob(Base):
    """Materialise an ROI of a cloud precomputed volume into data_root as an ordinary image stack."""

    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(128), index=True)
    url: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | done | error | cancelled
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)  # roi, mip, align, resume, precheck
    out_dir: Mapped[str] = mapped_column(String(1024), default="")
    n_sections: Mapped[int] = mapped_column(Integer, default=0)
    n_done: Mapped[int] = mapped_column(Integer, default=0)
    n_voxels: Mapped[int] = mapped_column(BigInteger, default=0)
    n_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    wire_json: Mapped[dict] = mapped_column(JSON, default=dict)  # chunks fetched, decoded bytes, seconds
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class CrawlEvent(Base):
    __tablename__ = "crawl_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("crawl_jobs.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    level: Mapped[str] = mapped_column(String(8), default="info")
    message: Mapped[str] = mapped_column(String(512))
    data_json: Mapped[dict] = mapped_column(JSON, default=dict)


# ----------------------------------------------------------------------------- 账号与登录（切片标注工作区）
class User(Base):
    """一个能登录的人。角色三种：admin 管理员（管账号，什么都能做）、reviewer 审核员（能改、能撤别人的改动）、
    annotator 标注员（能改，只能撤自己的）。停用（is_active=False）而不是删除：旧记录里的 by_id 还要能对回人。"""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)   # 登录名，小写
    display_name: Mapped[str] = mapped_column(String(64), default="")                # 记录里显示的名字（by）
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)          # bcrypt
    role: Mapped[str] = mapped_column(String(16), default="annotator")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)       # 管理员发的初始密码，首次登录要改
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_by: Mapped[int | None] = mapped_column(Integer)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuthSession(Base):
    """一次登录。Cookie 里是随机令牌，这里只存它的 SHA-256——库泄露了也拿不到能用的 Cookie。"""
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


# ----------------------------------------------------------------------------- 对比页的标记与评论
class Mark(Base):
    """钉在某个体素 (block, z, x, y) 上的一句话，加一条评论线程。给人在对比页上指着图说话用，也给 AI agent 用同一套接口读写。
    标记属于数据块而不是某个人：谁都能看、都能回复、都能标为已解决；只有作者和管理员能删。"""
    __tablename__ = "annot_marks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    block_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    z: Mapped[int] = mapped_column(Integer, nullable=False)
    x: Mapped[int] = mapped_column(Integer, nullable=False)
    y: Mapped[int] = mapped_column(Integer, nullable=False)
    label_id: Mapped[str | None] = mapped_column(String(32))          # 钉下去时光标下的标签 id（字符串，H01 的 id 超过 2^53）
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open")   # open | resolved
    created_by: Mapped[int | None] = mapped_column(Integer)           # users.id；没开登录时为空
    created_by_user: Mapped[str | None] = mapped_column(String(32))   # 登录名（改显示名不影响归属）
    created_by_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    resolved_by_name: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class MarkComment(Base):
    __tablename__ = "annot_mark_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mark_id: Mapped[int] = mapped_column(ForeignKey("annot_marks.id", ondelete="CASCADE"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="comment")  # comment | agent（AI 写的，界面上打个标）
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_by_user: Mapped[str | None] = mapped_column(String(32))
    created_by_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
