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
from emqc.qc.workflow import WORKFLOW_STAGE, topology as workflow_topology

from ..serializers import event_to_dict, finding_to_dict, metric_to_dict, qcblock_to_dict, qcslice_to_dict, run_to_dict

from .auth import require_reviewer

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


@router.post("/runs/batch", status_code=202, dependencies=[Depends(require_reviewer)])
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


@router.post("/runs", status_code=202, dependencies=[Depends(require_reviewer)])
def create_run(body: RunIn, s: Session = Depends(get_session)):
    if s.get(Dataset, body.dataset_id) is None:
        raise HTTPException(404, f"unknown dataset {body.dataset_id}")
    if body.sync and not settings.auth_disabled:
        raise HTTPException(422, "正式部署里不能在请求里同步跑 QC（会一直占着一个工作线程），去掉 sync 走后台任务")
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


@router.post("/runs/{run_id}/cancel", dependencies=[Depends(require_reviewer)])
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
    else:  # LangGraph node enter / exit records feed the workflow view, not the log (ask for stage=workflow to see them)
        qy = qy.where((QCRunEvent.stage.is_(None)) | (QCRunEvent.stage != WORKFLOW_STAGE))
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
    events = [e for e in s.scalars(select(QCRunEvent).where(QCRunEvent.run_id == run.id).order_by(QCRunEvent.id)) if e.stage != WORKFLOW_STAGE]
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


@router.get("/workflow")
def get_workflow_topology():
    """The LangGraph state graph every QC run executes: nodes, edges (conditional / back edges), a layered layout, mermaid source."""
    return workflow_topology()


@router.get("/runs/{run_id}/workflow")
def get_run_workflow(run_id: int, after_id: int = 0, limit: int = 2000, s: Session = Depends(get_session)):
    """Live state of a run's LangGraph execution.

    trace: node enter / exit / error records with id > after_id (poll with `last_id`), in execution order;
    nodes: per node visits, total / last duration, last phase (over the whole run, so a late joiner sees the full picture);
    active: the node currently executing (entered, not yet exited) while the run is live."""
    run = _run(s, run_id)
    engine = (run.config_json or {}).get("engine")
    if not engine:  # runs from before this field existed ran the loop; a run that has not started yet will use today's setting
        from emqc.qc.runner import QCRunner

        engine = QCRunner.engine() if run.status in ("queued", "running") else "loop"
    base = select(QCRunEvent).where(QCRunEvent.run_id == run_id, QCRunEvent.stage == WORKFLOW_STAGE)
    nodes: dict[str, dict] = {}
    active = None
    last_enter = None
    n_steps = 0
    for e in s.scalars(base.order_by(QCRunEvent.id)):
        d = e.data_json or {}
        n = d.get("node")
        st = nodes.setdefault(n, {"visits": 0, "total_s": 0.0, "last_s": None, "phase": None, "errors": 0})
        st["phase"] = d.get("phase")
        if d.get("phase") == "enter":
            st["visits"] += 1
            n_steps += 1
            active, last_enter = n, d
        else:
            if d.get("duration_s") is not None:
                st["total_s"] = round(st["total_s"] + d["duration_s"], 4)
                st["last_s"] = d["duration_s"]
            if d.get("phase") == "error":
                st["errors"] += 1
            if active == n:
                active = None
    trace = [{"id": e.id, "ts": e.ts.isoformat(timespec="milliseconds") if e.ts else None, **(e.data_json or {})}
             for e in s.scalars(base.where(QCRunEvent.id > after_id).order_by(QCRunEvent.id).limit(limit))]
    live = run.status in ("queued", "running")
    return {"run_id": run.id, "status": run.status, "engine": engine, "n_steps": n_steps, "nodes": nodes,
            "active": active if live else None, "position": last_enter if live else None,
            "n_blocks": run.n_blocks, "n_blocks_done": run.n_blocks_done,
            "trace": trace, "last_id": trace[-1]["id"] if trace else after_id}


_SIM_CACHE: dict = {}


def _similar_space(s: Session, run_ids: tuple[int, ...]):
    """Feature space over every readable slice of the given runs; cached until those runs gain / lose slices."""
    from emqc.qc.similar import build_space

    n = s.scalar(select(func.count()).select_from(QCSlice).where(QCSlice.run_id.in_(run_ids))) or 0
    key = (run_ids, n)
    if key in _SIM_CACHE:
        return _SIM_CACHE[key]
    rows = s.execute(select(QCSlice.run_id, QCSlice.dataset_id, QCSlice.block_id, QCSlice.z, QCSlice.stats_json, QCSlice.scores_json, QCSlice.failure_types,
                            QCSlice.max_severity, QCSlice.passed, QCSlice.preview_path, QCSlice.quality_score)
                     .where(QCSlice.run_id.in_(run_ids), QCSlice.status == "ok").order_by(QCSlice.dataset_id, QCSlice.z, QCSlice.block_id)).all()
    space = build_space([(r.stats_json or {}, r.scores_json or {}) for r in rows], keys=[(r.run_id, r.block_id, r.z) for r in rows], meta=rows) if rows else None
    _SIM_CACHE.clear()  # keep only the newest pool
    _SIM_CACHE[key] = space
    return space


@router.get("/similar")
def similar_slices(run_id: int, block_id: str, z: int, k: int = 12, s: Session = Depends(get_session)):
    """Slices that look most like this one, by the numbers QC already computed (statistics + check scores; no model).

    Searched over the latest run of every dataset plus this run. Other tiles of the same section are left out
    (they are trivially similar). Each hit carries its problems, severity, a thumbnail and a distance (smaller = closer)."""
    from emqc.api.routers.dashboard import FAILURE_ZH

    from ..serializers import preview_url

    _run(s, run_id)
    latest = {r for (r,) in s.execute(select(Dataset.latest_run_id).where(Dataset.latest_run_id.is_not(None))).all()}
    space = _similar_space(s, tuple(sorted(latest | {run_id})))
    if space is None:
        return {"query": None, "hits": []}
    try:
        i = space.keys.index((run_id, block_id, z))
    except ValueError:
        raise HTTPException(404, f"no QC record for {block_id} z{z} in run {run_id}")
    q = space.meta[i]
    hits = []
    for j, dist in space.nearest(i, k=max(1, min(k, 50)), exclude=lambda j: space.meta[j].dataset_id == q.dataset_id and space.meta[j].z == q.z):
        m = space.meta[j]
        types = list(m.failure_types or [])
        url = preview_url(m.preview_path) or f"/api/v1/data/{m.dataset_id}/preview/z/{m.z}.png"
        hits.append({"dataset_id": m.dataset_id, "block_id": m.block_id, "z": m.z, "run_id": m.run_id, "distance": round(dist, 3),
                     "failure_types": types, "zh": [FAILURE_ZH.get(t, t) for t in types], "severity": m.max_severity, "passed": m.passed,
                     "quality": None if m.quality_score is None else round(m.quality_score, 3), "url": url,
                     "href": f"/datasets/{m.dataset_id}/blocks/{m.block_id}?run_id={m.run_id}#z{m.z}"})
    qtypes = list(q.failure_types or [])
    same = sum(1 for h in hits if set(h["failure_types"]) & set(qtypes)) if qtypes else None
    return {"query": {"dataset_id": q.dataset_id, "block_id": q.block_id, "z": q.z, "failure_types": qtypes, "zh": [FAILURE_ZH.get(t, t) for t in qtypes]},
            "n_pool": len(space.keys), "n_same_problem": same, "features": {"stats": space.stat_names, "scores": space.score_names}, "hits": hits}


@router.get("/runs/{run_id}/map")
def run_slice_map(run_id: int, max_points: int = 4000, s: Session = Depends(get_session)):
    """Every slice of a run as a point on a 2-D map (PCA of the same features "find similar" uses: statistics + check scores).

    Close points look alike by the numbers; a cluster of failed slices away from the clean cloud is one kind of problem.
    Each axis says which features drive it, so the map is readable without knowing PCA."""
    from emqc.api.routers.dashboard import FAILURE_ZH
    import numpy as np

    from emqc.qc.similar import CHECK_ZH, STAT_ZH, build_space

    from ..serializers import preview_url

    _run(s, run_id)
    rows = s.execute(select(QCSlice.block_id, QCSlice.z, QCSlice.stats_json, QCSlice.scores_json, QCSlice.failure_types, QCSlice.max_severity,
                            QCSlice.passed, QCSlice.preview_path, QCSlice.quality_score, QCSlice.dataset_id)
                     .where(QCSlice.run_id == run_id, QCSlice.status == "ok").order_by(QCSlice.z, QCSlice.block_id)).all()
    if len(rows) < 3:
        return {"run_id": run_id, "points": [], "axes": [], "n": len(rows)}
    if len(rows) > max_points:   # keep every failed slice, thin the clean ones evenly
        bad = [r for r in rows if not r.passed]
        good = [r for r in rows if r.passed]
        step = max(1, len(good) // max(1, max_points - len(bad)))
        rows = bad + good[::step]
    space = build_space([(r.stats_json or {}, r.scores_json or {}) for r in rows], keys=list(range(len(rows))))
    X = space.X - space.X.mean(axis=0)
    if X.shape[1] < 2:
        return {"run_id": run_id, "points": [], "axes": [], "n": len(rows)}
    U, S, Vt = np.linalg.svd(X, full_matrices=False)
    P = X @ Vt[:2].T
    var = (S ** 2) / max(float((S ** 2).sum()), 1e-12)
    names = [STAT_ZH.get(n, n) for n in space.stat_names] + [CHECK_ZH.get(n, FAILURE_ZH.get(n, n)) + "分数" for n in space.score_names]
    axes = []
    for k in range(2):
        order = np.argsort(-np.abs(Vt[k]))[:3]
        axes.append({"explained": round(float(var[k]), 3), "drivers": [{"feature": names[i], "sign": 1 if Vt[k][i] > 0 else -1} for i in order]})
    pts = []
    for r, (x, y) in zip(rows, P.tolist()):
        types = list(r.failure_types or [])
        pts.append({"x": round(x, 4), "y": round(y, 4), "block_id": r.block_id, "z": r.z, "passed": bool(r.passed), "type": types[0] if types else None,
                    "types": types, "zh": [FAILURE_ZH.get(t, t) for t in types], "severity": r.max_severity,
                    "quality": None if r.quality_score is None else round(float(r.quality_score), 3),
                    "url": preview_url(r.preview_path) or f"/api/v1/data/{r.dataset_id}/preview/z/{r.z}.png",
                    "href": f"/datasets/{r.dataset_id}/blocks/{r.block_id}?run_id={run_id}#z{r.z}"})
    return {"run_id": run_id, "n": len(pts), "axes": axes, "points": pts, "type_zh": {t: FAILURE_ZH.get(t, t) for p in pts for t in p["types"]}}


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
    from emqc import share

    return {
        "share": share.info(),
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


@pipeline_router.get("/dashboard")
def pipeline_dashboard(days: int = 0, dataset_id: str | None = None, s: Session = Depends(get_session)):
    """Everything the pipeline dashboard draws, per dimension, in one call.

    days: only runs finished in the last N days (0 = all). dataset_id: only that dataset.
    runs     every finished run (oldest first): retention, quality, grades, sections, wall time, throughput, per-stage seconds
    datasets latest result per dataset: retention, grade, block grades, sections, problem sections
    failures high / critical findings by problem type over the datasets' latest runs
    checks   findings per check, split by severity, over the same runs
    delivery training exports and inference streams"""
    from datetime import datetime, timedelta, timezone

    from emqc.api.routers.dashboard import FAILURE_ZH
    from emqc.db.models import ExportJob, StreamSession

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days) if days > 0 else None
    q = select(QCRun).where(QCRun.status == "done").order_by(QCRun.id)
    if since:
        q = q.where(QCRun.finished_at >= since)
    if dataset_id:
        q = q.where(QCRun.dataset_id == dataset_id)
    runs_rows = list(s.scalars(q))
    ids = [r.id for r in runs_rows]
    dsm: dict[int, dict] = {}
    for m in s.scalars(select(ETLMetric).where(ETLMetric.run_id.in_(ids or [-1]), ETLMetric.block_id.is_(None))):
        dsm.setdefault(m.run_id, {})[m.metric_name] = m.metric_value
    stage: dict[int, dict] = {}
    for rid, sd in s.execute(select(QCBlock.run_id, QCBlock.stage_durations_json).where(QCBlock.run_id.in_(ids or [-1]))):
        acc = stage.setdefault(rid, {})
        for k, v in (sd or {}).items():
            acc[k] = acc.get(k, 0.0) + float(v or 0)
    runs = []
    for r in runs_rows:
        m = dsm.get(r.id, {})
        wall = (r.finished_at - r.started_at).total_seconds() if r.started_at and r.finished_at else m.get("duration_s")
        n = m.get("n_slices_input")
        runs.append({"run_id": r.id, "dataset_id": r.dataset_id, "finished_at": r.finished_at.isoformat(timespec="minutes") if r.finished_at else None,
                     "retention": r.retention_rate, "quality": r.quality_score, "grades": r.grade_counts_json or {}, "n_sections": n,
                     "n_findings": m.get("n_findings"), "wall_s": wall, "sections_per_s": (n / wall) if n and wall else None,
                     "bytes_read": m.get("bytes_read"), "stages": {k: round(v, 3) for k, v in stage.get(r.id, {}).items()}})

    dq = select(Dataset).where(Dataset.latest_run_id.is_not(None)).order_by(Dataset.dataset_id)
    if dataset_id:
        dq = dq.where(Dataset.dataset_id == dataset_id)
    in_window = set(ids)
    dsets = [d for d in s.scalars(dq) if d.latest_run_id in in_window]
    latest = [d.latest_run_id for d in dsets]
    bad = dict(s.execute(select(QCSlice.run_id, func.count()).where(QCSlice.run_id.in_(latest or [-1]), QCSlice.passed.is_(False)).group_by(QCSlice.run_id)).all())
    datasets = []
    for d in dsets:
        grades: dict[str, int] = {}
        for b in d.blocks:
            if b.latest_grade:
                grades[b.latest_grade] = grades.get(b.latest_grade, 0) + 1
        m = dsm.get(d.latest_run_id, {})
        datasets.append({"dataset_id": d.dataset_id, "run_id": d.latest_run_id, "retention": d.latest_retention_rate, "grade": d.latest_grade,
                         "block_grades": grades, "n_sections": m.get("n_slices_input"), "n_bad": bad.get(d.latest_run_id, 0)})
    fq = select(QCFinding.failure_type, func.count()).where(QCFinding.run_id.in_(latest or [-1]), QCFinding.severity.in_(["high", "critical"])).group_by(QCFinding.failure_type)
    failures = sorted(({"type": t, "zh": FAILURE_ZH.get(t, t), "n": n} for t, n in s.execute(fq).all()), key=lambda x: -x["n"])
    checks: dict[str, dict] = {}
    for chk, sev, n in s.execute(select(QCFinding.check_name, QCFinding.severity, func.count()).where(QCFinding.run_id.in_(latest or [-1])).group_by(QCFinding.check_name, QCFinding.severity)):
        checks.setdefault(chk, {"check": chk, "by_severity": {}, "n": 0})
        checks[chk]["by_severity"][sev] = n
        checks[chk]["n"] += n

    eq = select(ExportJob).where(ExportJob.status == "done")
    sq = select(StreamSession)
    if dataset_id:
        eq, sq = eq.where(ExportJob.dataset_id == dataset_id), sq.where(StreamSession.dataset_id == dataset_id)
    if since:
        eq, sq = eq.where(ExportJob.finished_at >= since), sq.where(StreamSession.created_at >= since)
    exports, streams = list(s.scalars(eq)), list(s.scalars(sq))
    delivery = {"n_exports": len(exports), "export_sections": sum(j.n_sections or 0 for j in exports), "export_bytes": sum(j.n_bytes or 0 for j in exports),
                "n_streams": len(streams), "stream_chunks": sum(x.n_acked or 0 for x in streams), "stream_bytes": sum(x.n_bytes or 0 for x in streams),
                "n_streams_open": sum(1 for x in streams if x.status == "open")}

    tot = sum(d["n_sections"] or 0 for d in datasets)
    kept = sum((d["n_sections"] or 0) * (d["retention"] or 0) for d in datasets)
    sps = sorted(r["sections_per_s"] for r in runs if r["sections_per_s"])
    n_err = s.scalar(select(func.count()).select_from(QCRun).where(QCRun.status == "error", *( [QCRun.finished_at >= since] if since else []), *( [QCRun.dataset_id == dataset_id] if dataset_id else []))) or 0
    kpi = {"n_datasets": len(datasets), "n_runs": len(runs), "n_runs_error": n_err, "n_sections": tot, "retention": (kept / tot) if tot else None,
           "n_bad": sum(d["n_bad"] for d in datasets), "n_problems": sum(f["n"] for f in failures), "sections_per_s": sps[len(sps) // 2] if sps else None,
           "export_sections": delivery["export_sections"], "stream_chunks": delivery["stream_chunks"]}
    all_ds = [x for (x,) in s.execute(select(Dataset.dataset_id).order_by(Dataset.dataset_id)).all()]
    return {"filters": {"days": days, "dataset_id": dataset_id, "datasets": all_ds}, "kpi": kpi, "runs": runs, "datasets": datasets,
            "failures": failures, "checks": sorted(checks.values(), key=lambda c: -c["n"]), "delivery": delivery}
