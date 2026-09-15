"""QC pipeline API: checks catalog, runs, per-slice / per-block results, findings, ETL metrics."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from emqc.db.base import get_session
from emqc.config import settings
from emqc.db.models import Block, Dataset, ETLMetric, QCBlock, QCFinding, QCRun, QCRunEvent, QCSlice
from emqc.qc import catalog
from emqc.qc.base import QCConfig, Severity
from emqc.qc.runner import request_cancel, run_sync, start_run_async

from ..serializers import event_to_dict, finding_to_dict, metric_to_dict, qcblock_to_dict, qcslice_to_dict, run_to_dict

router = APIRouter(prefix="/api/v1/qc", tags=["qc"])

SEVERITY_ORDER = [s.label for s in Severity]


def _sev_at_least(min_severity: str | None) -> list[str]:
    if not min_severity:
        return SEVERITY_ORDER
    i = SEVERITY_ORDER.index(min_severity)
    return SEVERITY_ORDER[i:]


@router.get("/checks")
def list_checks():
    return catalog()


class RunIn(BaseModel):
    dataset_id: str
    block_ids: list[str] | None = None
    config: dict | None = None  # QCConfig overrides: checks_enabled, thresholds, params, pass_max_severity, ...
    sync: bool = False  # run in the request (tests / small datasets) instead of a background thread


class BatchRunIn(BaseModel):
    dataset_ids: list[str]
    block_ids: list[str] | None = None  # applies to every dataset (usually None)
    config: dict | None = None


def _config(d: dict | None) -> QCConfig | None:
    if not d:
        return None
    try:
        return QCConfig.from_dict(d)
    except Exception as e:  # bad severity label, wrong types ...
        raise HTTPException(422, f"invalid config: {e}")


@router.post("/runs/batch", status_code=202)
def create_runs(body: BatchRunIn, s: Session = Depends(get_session)):
    """Start one run per dataset (background threads). Used by the pipeline console."""
    missing = [d for d in body.dataset_ids if s.get(Dataset, d) is None]
    if missing:
        raise HTTPException(404, f"unknown dataset(s): {missing}")
    cfg = _config(body.config)
    run_ids = [start_run_async(d, body.block_ids, cfg) for d in body.dataset_ids]
    s.rollback()
    return [run_to_dict(s.get(QCRun, rid)) for rid in run_ids]


@router.get("/runs/active")
def active_runs(s: Session = Depends(get_session)):
    """Queued / running runs with live progress, newest first."""
    out = []
    for r in s.scalars(select(QCRun).where(QCRun.status.in_(["queued", "running"])).order_by(QCRun.id.desc())):
        d = run_to_dict(r)
        d["blocks"] = [{"block_id": b.block_id, "status": b.status, "grade": b.latest_grade} for b in s.scalars(select(Block).where(Block.dataset_id == r.dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start))]
        done = {b.block_id: b for b in s.scalars(select(QCBlock).where(QCBlock.run_id == r.id))}
        for b in d["blocks"]:
            q = done.get(b["block_id"])
            b["done"] = q is not None
            if q is not None:
                b.update({"grade": q.grade, "retention_rate": q.retention_rate, "n_passed": q.n_passed, "n_slices": q.n_slices})
        n_slices = sum(q.n_slices for q in done.values())
        d["retention_so_far"] = (sum(q.n_passed for q in done.values()) / n_slices) if n_slices else None
        d["grades_so_far"] = {g: sum(1 for q in done.values() if q.grade == g) for g in ("A", "B", "C", "D") if any(q.grade == g for q in done.values())}
        d["n_findings"] = s.scalar(select(func.count()).select_from(QCFinding).where(QCFinding.run_id == r.id)) or 0
        out.append(d)
    return out


@router.post("/runs", status_code=202)
def create_run(body: RunIn, s: Session = Depends(get_session)):
    if s.get(Dataset, body.dataset_id) is None:
        raise HTTPException(404, f"unknown dataset {body.dataset_id}")
    cfg = _config(body.config)
    run_id = run_sync(body.dataset_id, body.block_ids, cfg) if body.sync else start_run_async(body.dataset_id, body.block_ids, cfg)
    # the run row was committed by another session; end this request's REPEATABLE READ snapshot before re-reading
    s.rollback()
    run = s.get(QCRun, run_id)
    if run is None:
        raise HTTPException(500, f"run {run_id} was created but is not visible yet")
    return run_to_dict(run)


@router.get("/runs")
def list_runs(dataset_id: str | None = None, limit: int = 50, s: Session = Depends(get_session)):
    q = select(QCRun).order_by(QCRun.id.desc()).limit(limit)
    if dataset_id:
        q = q.where(QCRun.dataset_id == dataset_id)
    return [run_to_dict(r) for r in s.scalars(q)]


def _run(s: Session, run_id: int) -> QCRun:
    run = s.get(QCRun, run_id)
    if run is None:
        raise HTTPException(404, f"unknown run {run_id}")
    return run


def run_summary(s: Session, run: QCRun) -> dict:
    d = run_to_dict(run)
    d["blocks"] = [qcblock_to_dict(b) for b in s.scalars(select(QCBlock).where(QCBlock.run_id == run.id).order_by(QCBlock.block_id))]
    d["metrics"] = {m.metric_name: (m.extra_json if m.metric_name == "findings_by_type" else m.metric_value) for m in s.scalars(select(ETLMetric).where(ETLMetric.run_id == run.id, ETLMetric.block_id.is_(None)))}
    d["findings_by_severity"] = dict(s.execute(select(QCFinding.severity, func.count()).where(QCFinding.run_id == run.id).group_by(QCFinding.severity)).all())
    return d


@router.get("/runs/{run_id}")
def get_run(run_id: int, s: Session = Depends(get_session)):
    return run_summary(s, _run(s, run_id))


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, s: Session = Depends(get_session)):
    run = _run(s, run_id)
    if not request_cancel(run_id):
        raise HTTPException(409, f"run {run_id} is {run.status}, nothing to cancel")
    s.rollback()
    return run_to_dict(s.get(QCRun, run_id))


@router.get("/runs/{run_id}/events")
def run_events(run_id: int, after_id: int = 0, limit: int = 500, stage: str | None = None, block_id: str | None = None, level: str | None = None, q: str | None = None, s: Session = Depends(get_session)):
    """Live log; poll with the last id you saw. Filter by node (stage + block_id), level, or a text search."""
    _run(s, run_id)
    qy = select(QCRunEvent).where(QCRunEvent.run_id == run_id, QCRunEvent.id > after_id)
    if stage:
        qy = qy.where(QCRunEvent.stage == stage)
    if block_id:
        qy = qy.where(QCRunEvent.block_id == block_id)
    if level:
        qy = qy.where(QCRunEvent.level.in_({"warn": ["warn", "error"], "error": ["error"]}.get(level, [level])))
    if q:
        qy = qy.where(QCRunEvent.message.contains(q))
    return [event_to_dict(e) for e in s.scalars(qy.order_by(QCRunEvent.id).limit(limit))]


STAGE_ORDER = ["ingest", "slice_qc", "serial_qc", "aggregate", "persist"]


def run_graph(s: Session, run: QCRun) -> dict:
    """The run as an execution graph derived from its events.

    One `ingest` node per z range (all tiles share the decode), then per tile: slice_qc -> serial_qc -> aggregate -> persist.
    Node status: pending | running | done | error, with start / end / duration and the number of log lines.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    blocks = list(s.scalars(select(Block).where(Block.dataset_id == run.dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start)))
    wanted = set((run.config_json or {}).get("block_ids") or []) or None
    blocks = [b for b in blocks if wanted is None or b.block_id in wanted]
    events = list(s.scalars(select(QCRunEvent).where(QCRunEvent.run_id == run.id).order_by(QCRunEvent.id)))
    done_blocks = {b.block_id: b for b in s.scalars(select(QCBlock).where(QCBlock.run_id == run.id))}
    finished = run.status in ("done", "error", "cancelled")

    def node(key, stage, block_id, started, ended, n_events, warn, extra=None):
        if started is None:
            status = "pending"
        elif ended is not None:
            status = "done"
        elif run.status == "error":
            status = "error"
        elif finished:
            status = "done"
        else:
            status = "running"
        end_ref = ended or (run.finished_at if finished else now)
        return {"key": key, "stage": stage, "block_id": block_id, "status": status, "started": started.isoformat(timespec="seconds") if started else None,
                "ended": ended.isoformat(timespec="seconds") if ended else None, "duration_s": round((end_ref - started).total_seconds(), 1) if started and end_ref else None,
                "n_events": n_events, "warn": warn, **(extra or {})}

    groups: dict[tuple[int, int], list[Block]] = {}
    for b in blocks:
        groups.setdefault((b.z_start, b.z_end), []).append(b)
    out_groups = []
    for (z0, z1), grp in groups.items():
        g_events = [e for e in events if (e.data_json or {}).get("z_start") == z0 and (e.data_json or {}).get("z_end") == z1]
        ing = [e for e in g_events if e.stage == "ingest"]
        ing_end = next((e.ts for e in g_events if e.stage in ("slice_qc", "persist", "group_done")), None)
        ingest_node = node(f"ingest:{z0}-{z1}", "ingest", None, ing[0].ts if ing else None, ing_end, len(ing), False, {"n_tiles": len(grp), "z_start": z0, "z_end": z1})
        tiles = []
        for b in grp:
            b_events = [e for e in events if e.block_id == b.block_id]
            nodes = {}
            for i, st in enumerate(STAGE_ORDER[1:]):
                mine = [e for e in b_events if e.stage == st]
                started = mine[0].ts if mine else None
                later = [e for e in b_events if e.stage in STAGE_ORDER[i + 2 :] or e.stage == "done"]
                ended = later[0].ts if (mine and later) else None
                if st == "persist" and b.block_id in done_blocks and mine:
                    ended = ended or next((e.ts for e in b_events if e.stage == "done"), None)
                nodes[st] = node(f"{st}:{b.block_id}", st, b.block_id, started, ended, len(mine), any(e.level in ("warn", "error") for e in mine))
            q = done_blocks.get(b.block_id)
            tiles.append({"block_id": b.block_id, "y_start": b.y_start, "x_start": b.x_start, "nodes": nodes, "done": q is not None,
                          "grade": q.grade if q else None, "retention_rate": q.retention_rate if q else None, "n_findings": sum((q.scores_json or {}).get("_block", {}).get("_", 0) for _ in []) if q else None,
                          "durations": (q.stage_durations_json if q else None), "n_events": len(b_events), "warn": any(e.level in ("warn", "error") for e in b_events)})
        out_groups.append({"z_start": z0, "z_end": z1, "ingest": ingest_node, "tiles": tiles})
    return {"run_id": run.id, "dataset_id": run.dataset_id, "status": run.status, "stage": run.stage, "stages": STAGE_ORDER, "groups": out_groups,
            "n_events": len(events), "n_warn": sum(1 for e in events if e.level == "warn"), "n_error": sum(1 for e in events if e.level == "error")}


@router.get("/runs/{run_id}/graph")
def get_run_graph(run_id: int, s: Session = Depends(get_session)):
    return run_graph(s, _run(s, run_id))


@router.get("/runs/{run_id}/blocks")
def get_run_blocks(run_id: int, s: Session = Depends(get_session)):
    _run(s, run_id)
    return [qcblock_to_dict(b) for b in s.scalars(select(QCBlock).where(QCBlock.run_id == run_id).order_by(QCBlock.block_id))]


@router.get("/runs/{run_id}/slices")
def get_run_slices(run_id: int, block_id: str | None = None, with_stats: bool = False, s: Session = Depends(get_session)):
    _run(s, run_id)
    q = select(QCSlice).where(QCSlice.run_id == run_id).order_by(QCSlice.z)
    if block_id:
        q = q.where(QCSlice.block_id == block_id)
    return [qcslice_to_dict(x, with_stats) for x in s.scalars(q)]


@router.get("/runs/{run_id}/profile")
def get_run_profile(run_id: int, s: Session = Depends(get_session)):
    """Compact z-profile for charts: one entry per slice."""
    _run(s, run_id)
    rows = s.execute(select(QCSlice.z, QCSlice.block_id, QCSlice.quality_score, QCSlice.max_severity, QCSlice.passed, QCSlice.status).where(QCSlice.run_id == run_id).order_by(QCSlice.z)).all()
    return [{"z": z, "block_id": b, "quality_score": q, "severity": sev, "passed": p, "status": st} for z, b, q, sev, p, st in rows]


@router.get("/runs/{run_id}/findings")
def get_run_findings(run_id: int, block_id: str | None = None, failure_type: str | None = None, min_severity: str | None = None, limit: int = 1000, s: Session = Depends(get_session)):
    _run(s, run_id)
    q = select(QCFinding).where(QCFinding.run_id == run_id, QCFinding.severity.in_(_sev_at_least(min_severity))).order_by(QCFinding.z, QCFinding.check_name).limit(limit)
    if block_id:
        q = q.where(QCFinding.block_id == block_id)
    if failure_type:
        q = q.where(QCFinding.failure_type == failure_type)
    return [finding_to_dict(f) for f in s.scalars(q)]


@router.get("/runs/{run_id}/metrics")
def get_run_metrics(run_id: int, block_id: str | None = None, s: Session = Depends(get_session)):
    _run(s, run_id)
    q = select(ETLMetric).where(ETLMetric.run_id == run_id).order_by(ETLMetric.block_id, ETLMetric.stage, ETLMetric.metric_name)
    if block_id:
        q = q.where(ETLMetric.block_id == block_id)
    return [metric_to_dict(m) for m in s.scalars(q)]


@router.get("/findings")
def list_findings(dataset_id: str | None = None, failure_type: str | None = None, min_severity: str | None = None, level: str | None = None, limit: int = 500, s: Session = Depends(get_session)):
    """Findings of the latest run of each dataset (or of one dataset)."""
    latest = select(Dataset.latest_run_id).where(Dataset.latest_run_id.is_not(None))
    if dataset_id:
        latest = latest.where(Dataset.dataset_id == dataset_id)
    q = select(QCFinding).where(QCFinding.run_id.in_(latest), QCFinding.severity.in_(_sev_at_least(min_severity))).order_by(QCFinding.dataset_id, QCFinding.z).limit(limit)
    if failure_type:
        q = q.where(QCFinding.failure_type == failure_type)
    if level:
        q = q.where(QCFinding.level == level)
    return [finding_to_dict(f) for f in s.scalars(q)]


@router.get("/datasets/{dataset_id}/latest")
def latest_for_dataset(dataset_id: str, s: Session = Depends(get_session)):
    ds = s.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, f"unknown dataset {dataset_id}")
    if not ds.latest_run_id:
        return {"dataset_id": dataset_id, "run": None}
    return {"dataset_id": dataset_id, "run": run_summary(s, _run(s, ds.latest_run_id))}


# ----------------------------------------------------------------------------- pipeline console

pipeline_router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])
system_router = APIRouter(prefix="/api/v1/system", tags=["system"])


@system_router.get("/metrics")
def system_metrics(last: int = 150):
    """Host CPU / memory / load, this process, and GPUs (nvidia-smi) sampled every 2 s."""
    from emqc.sysmon import monitor

    return monitor.snapshot(last=max(1, min(last, 450)))


@pipeline_router.get("/status")
def pipeline_status(s: Session = Depends(get_session)):
    """Everything the pipeline console needs in one call: source config, dataset & run counts, active runs, last scan."""
    from .datasets import LAST_SCAN

    ds_status = dict(s.execute(select(Dataset.status, func.count()).group_by(Dataset.status)).all())
    run_status = dict(s.execute(select(QCRun.status, func.count()).group_by(QCRun.status)).all())
    return {
        "source": {"data_root": str(settings.data_root), "dataset_glob": settings.dataset_glob, "preview_dir": str(settings.preview_dir), "roots": settings.all_roots, "n_remote": len(settings.all_roots) - 1},
        "pipeline": {
            "version": settings.pipeline_version, "block_size_z": settings.block_size_z, "block_size_xy": settings.block_size_xy,
            "large_dataset_voxels": settings.large_dataset_voxels, "train_sample": {"policy": settings.train_sample_policy, "blocks": settings.train_sample_blocks, "strata": settings.train_sample_strata, "seed": settings.train_sample_seed},
            "checks": {"total": len(catalog()), "implemented": sum(1 for c in catalog() if c["implemented"])},
        },
        "datasets": {"total": sum(ds_status.values()), "by_status": ds_status},
        "runs": {"total": sum(run_status.values()), "by_status": run_status},
        "active": active_runs(s),
        "last_scan": LAST_SCAN or None,
    }
