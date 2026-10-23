"""Data delivery: training-side export jobs (local shards) and inference-side stream sessions (no copies)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.base import get_session
from emqc.db.models import Block, Dataset, ExportEvent, ExportJob, QCBlock, QCSlice, StreamSession
from emqc.qc.export import ExportRunner, create_export_job, request_cancel_export, start_export_async

from ..serializers import export_event_to_dict, export_job_to_dict, stream_to_dict

from .auth import require_reviewer

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
    out_dir: str | None = None          # 不再接受：导出固定写到服务器的导出目录（EMQC_EXPORT_DIR）
    sync: bool = False

    @field_validator("out_dir")
    @classmethod
    def _no_out_dir(cls, v):
        if v is not None:
            raise ValueError("不再支持指定输出目录：导出固定写到服务器的导出目录（EMQC_EXPORT_DIR）")
        return v


@exports.post("", status_code=202, dependencies=[Depends(require_reviewer)])
def create_export(body: ExportIn, s: Session = Depends(get_session)):
    if s.get(Dataset, body.dataset_id) is None:
        raise HTTPException(404, f"unknown dataset {body.dataset_id}")
    if body.sync and not settings.auth_disabled:
        raise HTTPException(422, "正式部署里不能在请求里同步导出（会一直占着一个工作线程），去掉 sync 走后台任务")
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


@exports.get("/preview")
def preview_export(dataset_id: str, min_run: int = 1, shard_z: int = 16, min_quality: float | None = None, block_ids: str | None = None, run_id: int | None = None,
                   s: Session = Depends(get_session)):
    """Dry run: what an export with these settings would deliver (per section: delivered / why not), without writing anything."""
    from emqc.qc.export import plan_export

    ds = s.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, f"unknown dataset {dataset_id}")
    ids = [x.strip() for x in (block_ids or "").split(",") if x.strip()] or None
    return plan_export(s, ds, {"min_run": max(1, min_run), "shard_z": max(0, shard_z), "min_quality": min_quality, "block_ids": ids}, run_id=run_id)


@exports.get("")
def list_exports(dataset_id: str | None = None, status: str | None = None, limit: int = 50, s: Session = Depends(get_session)):
    q = select(ExportJob).order_by(ExportJob.id.desc()).limit(limit * 3)
    if dataset_id:
        q = q.where(ExportJob.dataset_id == dataset_id)
    if status:
        q = q.where(ExportJob.status.in_(status.split(",")))
    # 训练数据包也记在导出任务表里，但有自己的页面（/training），这里不混进来
    return [export_job_to_dict(j) for j in s.scalars(q) if (j.params_json or {}).get("kind") != "pack"][:limit]


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


@exports.post("/{job_id}/cancel", dependencies=[Depends(require_reviewer)])
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
    client: str | None = Field(default=None, max_length=64)
    purpose: str = Field(default="inference", max_length=64)

    @field_validator("client", "purpose")
    @classmethod
    def _names(cls, v):
        from emqc.ids import check_client

        return check_client(v)


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


@streams.post("", status_code=201, dependencies=[Depends(require_reviewer)])
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


@streams.post("/close-unused", dependencies=[Depends(require_reviewer)])
def close_unused(s: Session = Depends(get_session)):
    """Close every open session no client has taken anything from yet (created on the page and never picked up)."""
    rows = list(s.scalars(select(StreamSession).where(StreamSession.status == "open", StreamSession.cursor == 0)))
    for x in rows:
        x.status, x.finished_at = "aborted", _now()
    s.commit()
    return {"closed": [x.id for x in rows]}


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


@streams.get("/{stream_id}/map")
def stream_map(stream_id: int, s: Session = Depends(get_session)):
    """The session as a picture: per block, one cell per sub-chunk with its state
    (pending / out = handed to the client / ok / fail / skipped = left out by skip_failed), plus progress and rate."""
    sess = _sess(s, stream_id)
    p = sess.params_json or {}
    plan = list(sess.plan_json)
    if (sess.n_acked or sess.cursor) and not any(it.get("state") for it in plan):
        # sessions from before per-chunk tracking: only the counters exist; items are handed out in order
        plan = [{**it, "state": "ok" if it["i"] < sess.n_acked else ("out" if it["i"] < sess.cursor else "pending")} for it in plan]
    in_plan = {(it["block_id"], it["z0"]): it for it in plan}
    ds = s.get(Dataset, sess.dataset_id)
    full = in_plan.values()
    if ds is not None and p.get("skip_failed"):  # show what skip_failed left out, so the gaps are explained
        full, _ = build_plan(s, ds, int(p.get("z_chunk") or 16), p.get("block_ids"), p.get("order") or "z", False)
    blocks: dict[str, dict] = {}
    for it in full:
        mine = in_plan.get((it["block_id"], it["z0"]))
        state = (mine.get("state") or "pending") if mine else "skipped"
        b = blocks.setdefault(it["block_id"], {"block_id": it["block_id"], "grade": it.get("grade"), "z_start": it["z0"], "z_end": it["z1"], "cells": []})
        b["z_start"], b["z_end"] = min(b["z_start"], it["z0"]), max(b["z_end"], it["z1"])
        b["cells"].append({"i": mine["i"] if mine else None, "z0": it["z0"], "z1": it["z1"], "state": state, "n_failed": it.get("n_failed", 0)})
    for b in blocks.values():
        b["cells"].sort(key=lambda c: c["z0"])
    acks = sorted(it["t_ack"] for it in plan if it.get("t_ack"))
    rate = None
    if len(acks) >= 2:
        span = (datetime.fromisoformat(acks[-1]) - datetime.fromisoformat(acks[0])).total_seconds()
        rate = (len(acks) - 1) / span * 60 if span > 0 else None
    counts: dict[str, int] = {}
    for b in blocks.values():
        for c in b["cells"]:
            counts[c["state"]] = counts.get(c["state"], 0) + 1
    current = next((it for it in plan if it.get("state") == "out"), None) if sess.status == "open" else None
    return {**stream_to_dict(sess), "blocks": list(blocks.values()), "counts": counts, "chunks_per_min": rate,
            "current": {"block_id": current["block_id"], "z0": current["z0"], "z1": current["z1"]} if current else None}


@streams.post("/{stream_id}/next", dependencies=[Depends(require_reviewer)])
def stream_next(stream_id: int, n: int = 1, s: Session = Depends(get_session)):
    """Hand out the next n cutouts of the plan (and warm the cache for the ones after them on remote sources)."""
    sess = _sess(s, stream_id)
    if sess.status != "open":
        raise HTTPException(409, f"stream {stream_id} is {sess.status}")
    items = sess.plan_json[sess.cursor : sess.cursor + max(1, n)]
    t = _now().isoformat(timespec="milliseconds")
    plan = list(sess.plan_json)  # per-item state for the session map (JSON columns need a new object to register a change)
    for it in items:
        plan[it["i"]] = {**plan[it["i"]], "state": "out", "t_out": t}
    sess.plan_json = plan
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


@streams.post("/{stream_id}/ack", dependencies=[Depends(require_reviewer)])
def stream_ack(stream_id: int, body: AckIn, s: Session = Depends(get_session)):
    sess = _sess(s, stream_id)
    if body.ok:
        sess.n_acked += len(body.indices)
    else:
        sess.n_failed_items += len(body.indices)
    t = _now().isoformat(timespec="milliseconds")
    plan = list(sess.plan_json)
    for i in body.indices:
        if 0 <= i < len(plan):
            plan[i] = {**plan[i], "state": "ok" if body.ok else "fail", "t_ack": t, **({"note": body.note} if body.note else {})}
    sess.plan_json = plan
    s.commit()
    return stream_to_dict(sess)


class CloseIn(BaseModel):
    status: str = "done"  # done | aborted


@streams.post("/{stream_id}/close", dependencies=[Depends(require_reviewer)])
def stream_close(stream_id: int, body: CloseIn, s: Session = Depends(get_session)):
    sess = _sess(s, stream_id)
    sess.status = body.status if body.status in ("done", "aborted") else "done"
    sess.finished_at = _now()
    s.commit()
    return stream_to_dict(sess)


# ============================================================================= training packs (task x granularity)

packs = APIRouter(prefix="/api/v1/packs", tags=["delivery"])


class PackIn(BaseModel):
    dataset_id: str
    cells: list[list[str]] | None = None      # [[task, level], ...]
    max_slices: int | None = Field(None, ge=1, le=100000)
    min_run: int | None = Field(None, ge=1, le=10000)
    shard_z: int | None = Field(None, ge=0, le=10000)
    max_segments: int | None = Field(None, ge=1, le=100000)
    patch_size: list[int] | None = None
    n_patches: int | None = Field(None, ge=1, le=20000)
    seed: int | None = None
    qc_clean_ratio: float | None = Field(None, ge=0, le=20)
    min_quality: float | None = None

    @field_validator("patch_size")
    @classmethod
    def _size(cls, v):
        if v is not None and (len(v) != 3 or any(int(x) < 1 for x in v)):
            raise ValueError("patch_size 要三个正整数：dz, dy, dx")
        return v

    def params(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if k != "dataset_id" and v is not None}


def _pack_ds(s: Session, dataset_id: str) -> Dataset:
    ds = s.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, f"unknown dataset {dataset_id}")
    return ds


@packs.post("/plan")
def pack_plan(body: PackIn, s: Session = Depends(get_session)):
    """The task x granularity matrix for one dataset: which cells can be made, why not, and how many samples. Writes nothing."""
    from emqc.patches.pack import plan_pack

    return plan_pack(s, _pack_ds(s, body.dataset_id), body.params())


@packs.post("", status_code=202, dependencies=[Depends(require_reviewer)])
def create_pack(body: PackIn, s: Session = Depends(get_session)):
    from emqc.patches.pack import LEVELS_OF, start_pack_async

    _pack_ds(s, body.dataset_id)
    cells = [c for c in (body.cells or []) if len(c) == 2 and c[0] in LEVELS_OF and c[1] in LEVELS_OF[c[0]]]
    if not cells:
        raise HTTPException(422, "至少选一格（功能 × 粒度）")
    job_id = start_pack_async(body.dataset_id, **{**body.params(), "cells": cells})
    s.rollback()
    return export_job_to_dict(s.get(ExportJob, job_id))


@packs.get("")
def list_packs(dataset_id: str | None = None, limit: int = 30, s: Session = Depends(get_session)):
    q = select(ExportJob).order_by(ExportJob.id.desc()).limit(500)
    if dataset_id:
        q = q.where(ExportJob.dataset_id == dataset_id)
    out = []
    for j in s.scalars(q):
        if (j.params_json or {}).get("kind") == "pack":
            d = export_job_to_dict(j)
            m = Path(j.out_dir) / j.dataset_id / "pack_manifest.json"
            if m.is_file():
                try:
                    d["cells"] = json.loads(m.read_text()).get("cells", [])
                except ValueError:
                    d["cells"] = []
            out.append(d)
            if len(out) >= limit:
                break
    return out


def _pack_job(s: Session, job_id: int) -> tuple[ExportJob, Path]:
    j = _job(s, job_id)
    if (j.params_json or {}).get("kind") != "pack":
        raise HTTPException(404, f"export job {job_id} is not a training pack")
    return j, (Path(j.out_dir) / j.dataset_id).resolve()


@packs.get("/{job_id}/manifest")
def pack_manifest(job_id: int, s: Session = Depends(get_session)):
    j, root = _pack_job(s, job_id)
    p = root / "pack_manifest.json"
    if not p.is_file():
        raise HTTPException(404, "manifest not written yet")
    return json.loads(p.read_text())


def _pack_rows(root: Path, task: str, level: str) -> list[dict]:
    import csv

    if not task.isidentifier() or not level.isidentifier():
        raise HTTPException(422, "bad task / level")
    f = root / task / level / "index.csv"
    if not f.is_file():
        raise HTTPException(404, f"{task}/{level} 不在这个数据包里")
    return list(csv.DictReader(f.open(encoding="utf-8")))


@packs.get("/{job_id}/samples")
def pack_samples(job_id: int, task: str, level: str, n: int = 12, partition: str | None = None, s: Session = Depends(get_session)):
    """A spread of n samples of one cell (for the thumbnails on the page), with their index rows."""
    j, root = _pack_job(s, job_id)
    rows = _pack_rows(root, task, level)
    idx = [i for i, r in enumerate(rows) if not partition or r["partition"] == partition]
    if len(idx) > n:
        idx = [idx[int(k)] for k in np.linspace(0, len(idx) - 1, n).round()]
    return {"n_total": len(rows), "samples": [{"i": i, **{k: rows[i][k] for k in ("file", "label_file", "label", "partition", "block_id", "z0", "z1", "grade", "quality")},
                                                "thumb": f"/api/v1/packs/{job_id}/thumb?task={task}&level={level}&i={i}"} for i in idx]}


@packs.get("/{job_id}/thumb")
def pack_thumb(job_id: int, task: str, level: str, i: int, size: int = 160, s: Session = Depends(get_session)):
    """PNG of one sample: its middle section, with the label (ids / boundary / mask) drawn on top in colour."""
    from fastapi.responses import Response
    from PIL import Image

    j, root = _pack_job(s, job_id)
    rows = _pack_rows(root, task, level)
    if not 0 <= i < len(rows):
        raise HTTPException(404, "no such sample")
    r = rows[i]

    def load(rel):
        f = (root / rel).resolve()
        if root not in f.parents or not f.is_file():
            raise HTTPException(404, "sample file missing")
        a = np.load(f, mmap_mode="r") if f.suffix == ".npy" else np.asarray(Image.open(f))
        return np.asarray(a[a.shape[0] // 2] if a.ndim == 3 else a)

    em = load(r["file"])
    if em.dtype != np.uint8:
        lo, hi = np.percentile(em, 0.5), np.percentile(em, 99.5)
        em = (np.clip((em.astype(np.float32) - lo) / ((hi - lo) or 1), 0, 1) * 255).astype(np.uint8)
    rgb = np.stack([em] * 3, -1).astype(np.float32)
    if r.get("label_file"):
        L = load(r["label_file"])
        if task == "segmentation" or L.max() > 1:
            ids = L.astype(np.uint64)
            col = np.stack([(ids * 2654435761) % 255, (ids * 40503) % 255, (ids * 2246822519) % 255], -1).astype(np.float32)
            m = (ids != 0)[..., None]
            rgb = np.where(m, rgb * 0.55 + col * 0.45, rgb)
        else:
            m = (L != 0)[..., None]
            rgb = np.where(m, rgb * 0.35 + np.array([255, 70, 60], np.float32) * 0.65, rgb)
    im = Image.fromarray(rgb.clip(0, 255).astype(np.uint8))
    im.thumbnail((max(32, min(size, 512)),) * 2)
    buf = __import__("io").BytesIO()
    im.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "max-age=3600"})
