"""Data delivery: training-side export jobs (local shards) and inference-side stream sessions (no copies)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.base import get_session
from emqc.db.models import Block, Dataset, ExportEvent, ExportJob, QCBlock, QCSlice, StreamSession
from emqc.qc.export import ExportRunner, create_export_job, request_cancel_export, start_export_async

from ..serializers import export_event_to_dict, export_job_to_dict, stream_to_dict

exports = APIRouter(prefix="/api/v1/exports", tags=["delivery"])
streams = APIRouter(prefix="/api/v1/streams", tags=["delivery"])


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ============================================================================= training shards


class ExportIn(BaseModel):
    dataset_id: str
    run_id: int | None = None
    fmt: str = "npy"
    min_run: int = 1
    shard_z: int = 16
    min_quality: float | None = None
    with_assets: list[str] | None = None
    block_ids: list[str] | None = None
    resume: bool = True
    out_dir: str | None = None
    sync: bool = False


@exports.post("", status_code=202)
def create_export(body: ExportIn, s: Session = Depends(get_session)):
    if s.get(Dataset, body.dataset_id) is None:
        raise HTTPException(404, f"unknown dataset {body.dataset_id}")
    params = dict(fmt=body.fmt, min_run=body.min_run, shard_z=body.shard_z, min_quality=body.min_quality, with_assets=body.with_assets or [], block_ids=body.block_ids, resume=body.resume)
    if body.sync:
        job_id = create_export_job(body.dataset_id, body.out_dir, body.run_id, **params)
        try:
            ExportRunner(job_id).run()
        except RuntimeError as e:
            raise HTTPException(409, str(e))
    else:
        job_id = start_export_async(body.dataset_id, body.out_dir, body.run_id, **params)
    s.rollback()
    return export_job_to_dict(s.get(ExportJob, job_id))


@exports.get("")
def list_exports(dataset_id: str | None = None, status: str | None = None, limit: int = 50, s: Session = Depends(get_session)):
    q = select(ExportJob).order_by(ExportJob.id.desc()).limit(limit)
    if dataset_id:
        q = q.where(ExportJob.dataset_id == dataset_id)
    if status:
        q = q.where(ExportJob.status.in_(status.split(",")))
    return [export_job_to_dict(j) for j in s.scalars(q)]


def _job(s: Session, job_id: int) -> ExportJob:
    j = s.get(ExportJob, job_id)
    if j is None:
        raise HTTPException(404, f"unknown export job {job_id}")
    return j


@exports.get("/{job_id}")
def get_export(job_id: int, s: Session = Depends(get_session)):
    return export_job_to_dict(_job(s, job_id))


@exports.get("/{job_id}/events")
def export_events(job_id: int, after_id: int = 0, limit: int = 300, s: Session = Depends(get_session)):
    _job(s, job_id)
    return [export_event_to_dict(e) for e in s.scalars(select(ExportEvent).where(ExportEvent.job_id == job_id, ExportEvent.id > after_id).order_by(ExportEvent.id).limit(limit))]


@exports.get("/{job_id}/manifest")
def export_manifest(job_id: int, s: Session = Depends(get_session)):
    j = _job(s, job_id)
    p = Path(j.out_dir) / j.dataset_id / "export_manifest.json"
    if not p.is_file():
        raise HTTPException(404, "manifest not written yet")
    return json.loads(p.read_text())


@exports.post("/{job_id}/cancel")
def cancel_export(job_id: int, s: Session = Depends(get_session)):
    j = _job(s, job_id)
    if not request_cancel_export(job_id):
        raise HTTPException(409, f"export job {job_id} is {j.status}, nothing to cancel")
    s.rollback()
    return export_job_to_dict(s.get(ExportJob, job_id))


# ============================================================================= inference streams


class StreamIn(BaseModel):
    dataset_id: str
    z_chunk: int = 16
    block_ids: list[str] | None = None
    order: str = "z"  # z (spatial) | grade (best blocks first)
    skip_failed: bool = False  # inference normally covers the whole volume; True drops chunks containing failed sections
    client: str | None = None
    purpose: str = "inference"


def build_plan(s: Session, ds: Dataset, z_chunk: int, block_ids: list[str] | None, order: str, skip_failed: bool) -> tuple[list[dict], int | None]:
    rid = ds.latest_run_id
    blocks = list(s.scalars(select(Block).where(Block.dataset_id == ds.dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start)))
    if block_ids:
        blocks = [b for b in blocks if b.block_id in block_ids]
    grades = {q.block_id: q for q in s.scalars(select(QCBlock).where(QCBlock.run_id == rid))} if rid else {}
    if order == "grade":
        rank = {"A": 0, "B": 1, "C": 2, "D": 3, None: 4}
        blocks.sort(key=lambda b: (rank.get(grades[b.block_id].grade if b.block_id in grades else None, 4), b.z_start, b.y_start, b.x_start))
    failed: dict[str, set[int]] = {}
    if rid:
        for bid, z, passed in s.execute(select(QCSlice.block_id, QCSlice.z, QCSlice.passed).where(QCSlice.run_id == rid)):
            if not passed:
                failed.setdefault(bid, set()).add(z)
    itemsize = np.dtype(ds.dtype).itemsize
    plan, i = [], 0
    for b in blocks:
        q = grades.get(b.block_id)
        for z0 in range(b.z_start, b.z_end, max(1, z_chunk)):
            z1 = min(z0 + z_chunk, b.z_end)
            n_failed = len([z for z in range(z0, z1) if z in failed.get(b.block_id, set())])
            if skip_failed and n_failed:
                continue
            plan.append({"i": i, "block_id": b.block_id, "z0": z0, "z1": z1, "y0": b.y_start, "y1": b.y_end, "x0": b.x_start, "x1": b.x_end,
                         "bytes": (z1 - z0) * (b.y_end - b.y_start) * (b.x_end - b.x_start) * itemsize, "grade": q.grade if q else None, "n_failed": n_failed,
                         "failed_z": sorted(z for z in range(z0, z1) if z in failed.get(b.block_id, set()))})
            i += 1
    return plan, rid


def _with_urls(sess: StreamSession, items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        out.append({**it, "url": f"/api/v1/data/{sess.dataset_id}/cutout?z0={it['z0']}&z1={it['z1']}&y0={it['y0']}&y1={it['y1']}&x0={it['x0']}&x1={it['x1']}&fmt=npy&stream_id={sess.id}"})
    return out


@streams.post("", status_code=201)
def create_stream(body: StreamIn, s: Session = Depends(get_session)):
    ds = s.get(Dataset, body.dataset_id)
    if ds is None:
        raise HTTPException(404, f"unknown dataset {body.dataset_id}")
    plan, rid = build_plan(s, ds, body.z_chunk, body.block_ids, body.order, body.skip_failed)
    if not plan:
        raise HTTPException(409, "empty plan")
    sess = StreamSession(dataset_id=ds.dataset_id, run_id=rid, client=body.client, purpose=body.purpose, params_json={"z_chunk": body.z_chunk, "block_ids": body.block_ids, "order": body.order, "skip_failed": body.skip_failed},
                         plan_json=plan, n_items=len(plan), est_bytes=int(sum(p["bytes"] for p in plan)))
    s.add(sess)
    s.commit()
    d = stream_to_dict(sess)
    d["first_items"] = _with_urls(sess, plan[:3])
    return d


@streams.get("")
def list_streams(dataset_id: str | None = None, status: str | None = None, limit: int = 50, s: Session = Depends(get_session)):
    q = select(StreamSession).order_by(StreamSession.id.desc()).limit(limit)
    if dataset_id:
        q = q.where(StreamSession.dataset_id == dataset_id)
    if status:
        q = q.where(StreamSession.status.in_(status.split(",")))
    return [stream_to_dict(x) for x in s.scalars(q)]


def _sess(s: Session, stream_id: int) -> StreamSession:
    x = s.get(StreamSession, stream_id)
    if x is None:
        raise HTTPException(404, f"unknown stream {stream_id}")
    return x


@streams.get("/{stream_id}")
def get_stream(stream_id: int, s: Session = Depends(get_session)):
    return stream_to_dict(_sess(s, stream_id))


@streams.get("/{stream_id}/plan")
def stream_plan(stream_id: int, s: Session = Depends(get_session)):
    sess = _sess(s, stream_id)
    return {"stream_id": sess.id, "items": _with_urls(sess, sess.plan_json)}


@streams.post("/{stream_id}/next")
def stream_next(stream_id: int, n: int = 1, s: Session = Depends(get_session)):
    """Hand out the next n cutouts of the plan (and warm the cache for the ones after them on remote sources)."""
    sess = _sess(s, stream_id)
    if sess.status != "open":
        raise HTTPException(409, f"stream {stream_id} is {sess.status}")
    items = sess.plan_json[sess.cursor : sess.cursor + max(1, n)]
    sess.cursor = min(sess.n_items, sess.cursor + len(items))
    s.commit()
    if items:
        try:  # read-ahead: the items right after the ones just handed out
            from .data import get_reader

            ds = s.get(Dataset, sess.dataset_id)
            reader = get_reader(ds)
            nxt = sess.plan_json[sess.cursor : sess.cursor + max(1, n)]
            if nxt and hasattr(reader, "prefetch_range"):
                reader.prefetch_range(min(x["z0"] for x in nxt), max(x["z1"] for x in nxt))
        except Exception:
            pass
    return {"stream_id": sess.id, "items": _with_urls(sess, items), "cursor": sess.cursor, "n_items": sess.n_items, "done": sess.cursor >= sess.n_items}


class AckIn(BaseModel):
    indices: list[int]
    ok: bool = True
    note: str | None = None


@streams.post("/{stream_id}/ack")
def stream_ack(stream_id: int, body: AckIn, s: Session = Depends(get_session)):
    sess = _sess(s, stream_id)
    if body.ok:
        sess.n_acked += len(body.indices)
    else:
        sess.n_failed_items += len(body.indices)
    s.commit()
    return stream_to_dict(sess)


class CloseIn(BaseModel):
    status: str = "done"  # done | aborted


@streams.post("/{stream_id}/close")
def stream_close(stream_id: int, body: CloseIn, s: Session = Depends(get_session)):
    sess = _sess(s, stream_id)
    sess.status = body.status if body.status in ("done", "aborted") else "done"
    sess.finished_at = _now()
    s.commit()
    return stream_to_dict(sess)
