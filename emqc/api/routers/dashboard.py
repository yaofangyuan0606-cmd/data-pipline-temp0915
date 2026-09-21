"""Server-rendered pages (Jinja2). Numbers come from the same serializers as the REST API."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.base import get_session
from emqc.db.models import AgentTrace, Block, Dataset, ETLMetric, ExportJob, QCBlock, QCFinding, QCRun, QCSlice, ServeLog, StreamSession

from emqc.qc import catalog

from ..serializers import block_to_dict, dataset_to_dict, export_job_to_dict, finding_to_dict, qcblock_to_dict, qcslice_to_dict, run_to_dict, stream_to_dict, trace_to_dict
from .qc import pipeline_status, run_graph, run_summary

router = APIRouter(tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _fmt(v, nd=2):
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _pct(v):
    return "–" if v is None else f"{100 * v:.0f}%"


def _bytes(n):
    if n is None:
        return "–"
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


_STATIC = Path(__file__).resolve().parents[1] / "static"


def _static_v(name: str) -> int:
    """Cache-buster for /static links: the file's mtime, so browsers pick up a changed stylesheet/script after a deploy."""
    try:
        return int((_STATIC / name).stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["static_v"] = _static_v
templates.env.filters["fmt"] = _fmt
templates.env.filters["pct"] = _pct
templates.env.filters["bytes"] = _bytes
templates.env.globals["settings"] = settings


def _render(name: str, request: Request, **ctx):
    ctx.setdefault("workspace", "qc")  # which side of the shell the page belongs to: "qc" (cleaning) or "annotate"
    return templates.TemplateResponse(request, name, ctx)


def _profile(s: Session, run_id: int) -> list[dict]:
    return [{"z": z, "block_id": b, "q": q, "sev": sev, "passed": p, "status": st} for z, b, q, sev, p, st in s.execute(select(QCSlice.z, QCSlice.block_id, QCSlice.quality_score, QCSlice.max_severity, QCSlice.passed, QCSlice.status).where(QCSlice.run_id == run_id).order_by(QCSlice.z, QCSlice.block_id)).all()]


SEV_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _worst_per_z(profile: list[dict]) -> list[dict]:
    """Collapse tiles: for each z keep the worst tile (used for the compact dataset strips)."""
    best: dict[int, dict] = {}
    for p in profile:
        cur = best.get(p["z"])
        rank = 5 if p["status"] != "ok" else SEV_RANK.get(p["sev"], 0)
        if cur is None or rank > cur["_rank"]:
            best[p["z"]] = {**p, "_rank": rank}
    return [best[z] for z in sorted(best)]


# ----------------------------------------------------------------------------- pages


@router.get("/", response_class=HTMLResponse)
def index(request: Request, s: Session = Depends(get_session)):
    dss = list(s.scalars(select(Dataset).order_by(Dataset.dataset_id)))
    rows = []
    kpi = {"n_datasets": len(dss), "n_sections": 0, "n_blocks": 0, "passed": 0, "evaluated": 0, "grades": Counter(), "size": Counter()}
    for d in dss:
        dd = dataset_to_dict(d)
        kpi["n_sections"] += d.size_z
        kpi["n_blocks"] += len(d.blocks)
        kpi["size"][d.size_class] += 1
        for b in d.blocks:
            if b.latest_grade:
                kpi["grades"][b.latest_grade] += 1
        dd["strip"] = _worst_per_z(_profile(s, d.latest_run_id)) if d.latest_run_id else None
        dd["blocks_grades"] = Counter(b.latest_grade or "pending" for b in d.blocks)
        if d.latest_run_id:
            m = {x.metric_name: x.metric_value for x in s.scalars(select(ETLMetric).where(ETLMetric.run_id == d.latest_run_id, ETLMetric.block_id.is_(None)))}
            kpi["passed"] += m.get("n_slices_passed") or 0
            kpi["evaluated"] += m.get("n_slices_input") or 0
            dd["n_findings"] = m.get("n_findings")
        rows.append(dd)
    kpi["retention"] = (kpi["passed"] / kpi["evaluated"]) if kpi["evaluated"] else None
    latest_ids = select(Dataset.latest_run_id).where(Dataset.latest_run_id.is_not(None))
    by_type = s.execute(select(QCFinding.failure_type, func.count()).where(QCFinding.run_id.in_(latest_ids), QCFinding.severity.in_(["high", "critical"])).group_by(QCFinding.failure_type).order_by(func.count().desc())).all()
    runs = [run_to_dict(r) for r in s.scalars(select(QCRun).order_by(QCRun.id.desc()).limit(6))]
    n_served = s.scalar(select(func.count()).select_from(ServeLog)) or 0
    return _render("index.html", request, datasets=rows, kpi=kpi, failure_counts=by_type, runs=runs, n_served=n_served, active="datasets")


@router.get("/datasets/{dataset_id}", response_class=HTMLResponse)
def dataset_page(dataset_id: str, request: Request, s: Session = Depends(get_session)):
    ds = s.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404)
    d = dataset_to_dict(ds, with_children=True)
    run = s.get(QCRun, ds.latest_run_id) if ds.latest_run_id else None
    summary = run_summary(s, run) if run else None
    qc_blocks = {b["block_id"]: b for b in summary["blocks"]} if summary else {}
    profile = _profile(s, run.id) if run else []
    by_block: dict[str, list] = {}
    for p in profile:
        by_block.setdefault(p["block_id"], []).append(p)
    findings = [finding_to_dict(f) for f in s.scalars(select(QCFinding).where(QCFinding.run_id == run.id, QCFinding.severity.in_(["medium", "high", "critical"])).order_by(QCFinding.block_id, QCFinding.z).limit(300))] if run else []
    by_type = dict(s.execute(select(QCFinding.failure_type, func.count()).where(QCFinding.run_id == run.id).group_by(QCFinding.failure_type).order_by(func.count().desc())).all()) if run else {}
    by_type_hi = dict(s.execute(select(QCFinding.failure_type, func.count()).where(QCFinding.run_id == run.id, QCFinding.severity.in_(["high", "critical"])).group_by(QCFinding.failure_type)).all()) if run else {}
    by_sev = dict(s.execute(select(QCFinding.severity, func.count()).where(QCFinding.run_id == run.id).group_by(QCFinding.severity)).all()) if run else {}
    # per-check aggregate across blocks
    checks = catalog()
    check_summary = []
    for c in checks:
        mins, means, flagged, evaluated = [], [], 0, 0
        for q in qc_blocks.values():
            sc = (q.get("scores") or {}).get(c["name"])
            if sc:
                if sc.get("min") is not None:
                    mins.append(sc["min"])
                if sc.get("mean") is not None:
                    means.append(sc["mean"])
                flagged += sc.get("n_flagged") or 0
                evaluated += sc.get("n_evaluated") or 0
        check_summary.append({**c, "min": min(mins) if mins else None, "mean": (sum(means) / len(means)) if means else None, "n_flagged": flagged, "n_evaluated": evaluated})
    runs = [run_to_dict(r) for r in s.scalars(select(QCRun).where(QCRun.dataset_id == dataset_id).order_by(QCRun.id.desc()).limit(10))]
    traces = [trace_to_dict(t) for t in s.scalars(select(AgentTrace).where(AgentTrace.dataset_id == dataset_id).order_by(AgentTrace.id.desc()).limit(8))]
    assets_by_type: dict[str, list] = {}
    for a in d["assets"]:
        assets_by_type.setdefault(a["asset_type"], []).append(a)
    return _render("dataset.html", request, ds=d, run=summary, qc_blocks=qc_blocks, profile_by_block=by_block, findings=findings, by_type=by_type, by_type_hi=by_type_hi, by_sev=by_sev, check_summary=check_summary, runs=runs, traces=traces, assets_by_type=assets_by_type, active="datasets")


@router.get("/datasets/{dataset_id}/blocks/{block_id}", response_class=HTMLResponse)
def block_page(dataset_id: str, block_id: str, request: Request, run_id: int | None = None, s: Session = Depends(get_session)):
    ds = s.get(Dataset, dataset_id)
    blk = s.scalar(select(Block).where(Block.dataset_id == dataset_id, Block.block_id == block_id))
    if ds is None or blk is None:
        raise HTTPException(404)
    rid = run_id or ds.latest_run_id
    qcb = s.scalar(select(QCBlock).where(QCBlock.run_id == rid, QCBlock.block_id == block_id)) if rid else None
    slices = [qcslice_to_dict(x, with_stats=True) for x in s.scalars(select(QCSlice).where(QCSlice.run_id == rid, QCSlice.block_id == block_id).order_by(QCSlice.z))] if rid else []
    findings = [finding_to_dict(f) for f in s.scalars(select(QCFinding).where(QCFinding.run_id == rid, QCFinding.block_id == block_id).order_by(QCFinding.z, QCFinding.check_name))] if rid else []
    metrics = {m.metric_name: (m.extra_json if m.metric_name == "findings_by_type" else m.metric_value) for m in s.scalars(select(ETLMetric).where(ETLMetric.run_id == rid, ETLMetric.block_id == block_id, ETLMetric.stage == "aggregate"))} if rid else {}
    siblings = [block_to_dict(b) for b in s.scalars(select(Block).where(Block.dataset_id == dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start))]
    by_sev = Counter(x["max_severity"] for x in slices)
    return _render("block.html", request, ds=dataset_to_dict(ds), block=block_to_dict(blk), siblings=siblings, qcb=qcblock_to_dict(qcb) if qcb else None, slices=slices, findings=findings, metrics=metrics, checks=catalog(), run_id=rid, by_sev=by_sev, active="datasets")


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(run_id: int, request: Request, s: Session = Depends(get_session)):
    run = s.get(QCRun, run_id)
    if run is None:
        raise HTTPException(404)
    metrics = list(s.scalars(select(ETLMetric).where(ETLMetric.run_id == run_id).order_by(ETLMetric.block_id, ETLMetric.stage, ETLMetric.metric_name)))
    per_block: dict[str, dict] = {}
    for m in metrics:
        if m.block_id:
            per_block.setdefault(m.block_id, {})[f"{m.stage}:{m.metric_name}" if m.metric_name == "stage_duration_s" else m.metric_name] = m.extra_json if m.metric_name == "findings_by_type" else m.metric_value
    ds = s.get(Dataset, run.dataset_id)
    blocks = [block_to_dict(b) for b in s.scalars(select(Block).where(Block.dataset_id == run.dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start))]
    summary = run_summary(s, run)
    qc_blocks = {b["block_id"]: b for b in summary["blocks"]}
    max_dur = max([b.get("duration_s") or 0 for b in summary["blocks"]] + [1])
    return _render("run.html", request, run=summary, graph=run_graph(s, run), per_block=per_block, ds=dataset_to_dict(ds) if ds else None, blocks=blocks, qc_blocks=qc_blocks, max_dur=max_dur, active="runs")


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, s: Session = Depends(get_session)):
    runs = [run_to_dict(r) for r in s.scalars(select(QCRun).order_by(QCRun.id.desc()).limit(100))]
    by_status = Counter(r["status"] for r in runs)
    return _render("runs.html", request, runs=runs, by_status=by_status, active="runs")


@router.get("/checks", response_class=HTMLResponse)
def checks_page(request: Request):
    return _render("checks.html", request, checks=catalog(), active="checks")


@router.get("/traces", response_class=HTMLResponse)
def traces_page(request: Request, s: Session = Depends(get_session)):
    traces = [trace_to_dict(t) for t in s.scalars(select(AgentTrace).order_by(AgentTrace.id.desc()).limit(200))]
    served = list(s.scalars(select(ServeLog).order_by(ServeLog.id.desc()).limit(100)))
    served_kinds = Counter(x.kind for x in served)
    return _render("traces.html", request, traces=traces, served=served, served_kinds=served_kinds, active="traces")


@router.get("/pipeline", response_class=HTMLResponse)
def pipeline_page(request: Request, s: Session = Depends(get_session)):
    status = pipeline_status(s)
    datasets = [dataset_to_dict(d) for d in s.scalars(select(Dataset).order_by(Dataset.dataset_id))]
    recent = [run_to_dict(r) for r in s.scalars(select(QCRun).where(QCRun.status.in_(["done", "error", "cancelled"])).order_by(QCRun.id.desc()).limit(12))]
    return _render("pipeline.html", request, status=status, datasets=datasets, checks=catalog(), recent=recent, active="pipeline")


@router.get("/delivery", response_class=HTMLResponse)
def delivery_page(request: Request, dataset: str | None = None, s: Session = Depends(get_session)):
    datasets = [dataset_to_dict(d) for d in s.scalars(select(Dataset).order_by(Dataset.dataset_id))]
    jobs = [export_job_to_dict(j) for j in s.scalars(select(ExportJob).order_by(ExportJob.id.desc()).limit(20))]
    sessions = [stream_to_dict(x) for x in s.scalars(select(StreamSession).order_by(StreamSession.id.desc()).limit(30))]
    base = str(request.base_url).rstrip("/")
    if not dataset:  # default to a dataset that already has QC results
        dataset = next((d["dataset_id"] for d in datasets if d["latest_run_id"]), datasets[0]["dataset_id"] if datasets else None)
    return _render("delivery.html", request, datasets=datasets, jobs=jobs, sessions=sessions, preselect=dataset, export_dir=str(settings.export_dir), api_base=base, active="delivery")


@router.get("/patches", response_class=HTMLResponse)
def patches_page(request: Request, dataset: str | None = None, s: Session = Depends(get_session)):
    from emqc.db.models import PATCH_TYPES

    datasets = [dataset_to_dict(d) for d in s.scalars(select(Dataset).order_by(Dataset.dataset_id))]
    if not dataset:
        dataset = next((d["dataset_id"] for d in datasets if d["latest_run_id"]), datasets[0]["dataset_id"] if datasets else None)
    base = str(request.base_url).rstrip("/")
    return _render("patches.html", request, datasets=datasets, preselect=dataset, types=list(PATCH_TYPES), api_base=base, active="patches")


@router.get("/crawl", response_class=HTMLResponse)
def crawl_page(request: Request, s: Session = Depends(get_session)):
    from emqc.api.routers.crawl import PRESETS

    return _render("crawl.html", request, presets=PRESETS, active="crawl")


# ----------------------------------------------------------------------------- annotation workspace
# Separate shell: its own nav, top bar and accent colour. Shares the service and the stylesheet with the QC pages.


@router.get("/annotate", response_class=HTMLResponse)
def annotate_page(request: Request, block: str | None = None):
    return _render("annotate.html", request, preselect=block, active="workbench", workspace="annotate")


@router.get("/annotate/blocks", response_class=HTMLResponse)
def annotate_blocks_page(request: Request):
    from emqc.api.routers.annotate import get_store

    st = get_store()
    blocks = st.refresh()
    ok = [b for b in blocks if not b.get("error")]  # a half-copied block is listed with an error and must not take the page down
    stats = {"n_seg": sum(1 for b in ok if b["has_seg"]), "n_working": sum(1 for b in ok if b["has_working_copy"]),
             "n_edits": sum(b["n_edits"] or 0 for b in ok), "n_error": len(blocks) - len(ok),
             "n_history_error": sum(bool(b.get("history_error")) for b in ok)}
    return _render("annotate_blocks.html", request, blocks=blocks, stats=stats, roots=[str(r) for r in st.roots], workdir=str(settings.annotate_workdir),
                   sam_dir=str(settings.sam_blocks_dir), active="blocks", workspace="annotate")


@router.get("/annotate/compare", response_class=HTMLResponse)
def annotate_compare_page(request: Request):
    return _render("annotate_compare.html", request, active="compare", workspace="annotate")


@router.get("/annotate/guide", response_class=HTMLResponse)
def annotate_guide_page(request: Request):
    return _render("annotate_guide.html", request, workdir=str(settings.annotate_workdir), sam_dir=str(settings.sam_blocks_dir), active="guide", workspace="annotate")
