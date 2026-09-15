"""Algorithm-facing data API: slices, cutouts and QC-filtered patch sampling, served on demand.

Nothing is pre-cut or re-saved: every request reads the needed region from the source volume and
streams it back (npy / raw / png). Every served region is logged to `serve_log` for lineage.
"""
from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.base import get_session
from emqc.db.models import Block, Dataset, QCBlock, QCRun, QCSlice, ServeLog, StreamSession
from emqc.qc.preview import downsample, to_uint8
from emqc.qc.runner import open_dataset_volume
from emqc.registry.readers import SliceReadError, VolumeReader

router = APIRouter(prefix="/api/v1/data", tags=["data"])

MAX_CUTOUT_BYTES = 512 * 1024 * 1024
_readers: dict[str, VolumeReader] = {}
_lock = threading.Lock()


def invalidate_readers() -> None:
    with _lock:
        _readers.clear()


def get_reader(ds: Dataset) -> VolumeReader:
    with _lock:
        r = _readers.get(ds.dataset_id)
        if r is None:
            r = open_dataset_volume(ds)
            _readers[ds.dataset_id] = r
        return r


def _ds(s: Session, dataset_id: str) -> Dataset:
    ds = s.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, f"unknown dataset {dataset_id}")
    return ds


def _log(s: Session, ds: Dataset, kind: str, bbox: dict, n_bytes: int, fmt: str, request: Request, t0: float, qc_filter: dict | None = None, stream_id: int | None = None) -> None:
    s.add(ServeLog(dataset_id=ds.dataset_id, kind=kind, bbox_json=bbox, n_bytes=n_bytes, fmt=fmt, client=request.client.host if request.client else None, qc_filter_json=qc_filter or {}, duration_ms=(time.perf_counter() - t0) * 1000.0, stream_id=stream_id))
    if stream_id:
        sess = s.get(StreamSession, stream_id)
        if sess is not None:
            sess.n_bytes = (sess.n_bytes or 0) + n_bytes
    s.commit()


def _encode(arr: np.ndarray, fmt: str) -> tuple[bytes, str]:
    if fmt == "npy":
        buf = io.BytesIO()
        np.save(buf, np.ascontiguousarray(arr))
        return buf.getvalue(), "application/x-npy"
    if fmt == "raw":
        return np.ascontiguousarray(arr).tobytes(), "application/octet-stream"
    if fmt == "png":
        from PIL import Image

        if arr.ndim != 2:
            raise HTTPException(422, "png only for a single slice")
        im = Image.fromarray(arr if arr.dtype in (np.uint8, np.uint16) else to_uint8(arr.astype(np.float32) / max(float(arr.max()), 1.0)))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    raise HTTPException(422, f"unknown fmt {fmt!r} (npy | raw | png)")


def _bounds(ds: Dataset, z0, z1, y0, y1, x0, x1) -> tuple[int, int, int, int, int, int]:
    z1 = ds.size_z if z1 is None else z1
    y1 = ds.size_y if y1 is None else y1
    x1 = ds.size_x if x1 is None else x1
    if not (0 <= z0 < z1 <= ds.size_z and 0 <= y0 < y1 <= ds.size_y and 0 <= x0 < x1 <= ds.size_x):
        raise HTTPException(422, f"bbox out of range for shape z={ds.size_z} y={ds.size_y} x={ds.size_x}")
    return z0, z1, y0, y1, x0, x1


@router.get("/{dataset_id}/info")
def info(dataset_id: str, s: Session = Depends(get_session)):
    ds = _ds(s, dataset_id)
    blocks = list(s.scalars(select(Block).where(Block.dataset_id == dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start)))
    passed_by_block: dict[str, dict[int, bool]] = {}
    if ds.latest_run_id:
        for bid, z, p in s.execute(select(QCSlice.block_id, QCSlice.z, QCSlice.passed).where(QCSlice.run_id == ds.latest_run_id)):
            passed_by_block.setdefault(bid, {})[z] = bool(p)
    slice_passed = None
    if passed_by_block:
        slice_passed = [True] * ds.size_z
        for m in passed_by_block.values():
            for z, p in m.items():
                slice_passed[z] = slice_passed[z] and p
    return {
        "dataset_id": ds.dataset_id,
        "shape": {"z": ds.size_z, "y": ds.size_y, "x": ds.size_x},
        "dtype": ds.dtype,
        "voxel_size_nm": [ds.voxel_size_x_nm, ds.voxel_size_y_nm, ds.voxel_size_z_nm],
        "z_offset": (ds.metadata_json or {}).get("z_offset", 0),
        "size_class": ds.size_class,
        "usage": ds.usage,
        "grade": ds.latest_grade,
        "latest_run_id": ds.latest_run_id,
        "blocks": [
            {
                "block_id": b.block_id, "z_start": b.z_start, "z_end": b.z_end, "y_start": b.y_start, "y_end": b.y_end, "x_start": b.x_start, "x_end": b.x_end,
                "split": b.split, "grade": b.latest_grade, "quality_score": b.latest_quality_score, "retention_rate": b.latest_retention_rate, "severity": b.latest_severity,
                "passed_z": sorted(z for z, p in passed_by_block.get(b.block_id, {}).items() if p) if b.block_id in passed_by_block else None,
            }
            for b in blocks
        ],
        "slice_passed": slice_passed,  # passed in every tile covering that z
        "endpoints": {
            "slice": f"/api/v1/data/{ds.dataset_id}/slice/{{z}}?fmt=npy",
            "cutout": f"/api/v1/data/{ds.dataset_id}/cutout?z0=&z1=&y0=&y1=&x0=&x1=&fmt=npy",
            "sample_patches": f"/api/v1/data/{ds.dataset_id}/patches/sample",
            "batch": f"/api/v1/data/{ds.dataset_id}/cutouts/batch",
        },
    }


@router.get("/{dataset_id}/slice/{z}")
def get_slice(dataset_id: str, z: int, request: Request, fmt: str = "npy", y0: int = 0, y1: int | None = None, x0: int = 0, x1: int | None = None, s: Session = Depends(get_session)):
    t0 = time.perf_counter()
    ds = _ds(s, dataset_id)
    if not 0 <= z < ds.size_z:
        raise HTTPException(422, f"z out of range [0, {ds.size_z})")
    _, _, y0, y1, x0, x1 = _bounds(ds, z, z + 1, y0, y1, x0, x1)
    try:
        arr = get_reader(ds).read_slice(z)[y0:y1, x0:x1]
    except SliceReadError as e:
        raise HTTPException(404, f"slice {z} unavailable: {e}")
    body, media = _encode(arr, fmt)
    _log(s, ds, "slice", {"z": z, "y0": y0, "y1": y1, "x0": x0, "x1": x1}, len(body), fmt, request, t0)
    return Response(body, media_type=media, headers={"X-Shape": "x".join(map(str, arr.shape)), "X-Dtype": str(arr.dtype)})


@router.get("/{dataset_id}/preview/z/{z}.png")
def preview_slice(dataset_id: str, z: int, s: Session = Depends(get_session)):
    """On-the-fly thumbnail (never stored)."""
    ds = _ds(s, dataset_id)
    if not 0 <= z < ds.size_z:
        raise HTTPException(422, "z out of range")
    try:
        arr = get_reader(ds).read_slice(z)
    except SliceReadError as e:
        raise HTTPException(404, f"slice {z} unavailable: {e}")
    dmax = float(np.iinfo(arr.dtype).max) if arr.dtype.kind in "ui" else 1.0
    thumb = to_uint8(downsample(arr, settings.preview_max_px * 2, dmax))
    body, media = _encode(thumb, "png")
    return Response(body, media_type=media, headers={"Cache-Control": "max-age=3600"})


@router.get("/{dataset_id}/cutout")
def cutout(dataset_id: str, request: Request, z0: int, z1: int, y0: int, y1: int, x0: int, x1: int, fmt: str = "npy", stream_id: int | None = None, s: Session = Depends(get_session)):
    t0 = time.perf_counter()
    ds = _ds(s, dataset_id)
    z0, z1, y0, y1, x0, x1 = _bounds(ds, z0, z1, y0, y1, x0, x1)
    nbytes = (z1 - z0) * (y1 - y0) * (x1 - x0) * np.dtype(ds.dtype).itemsize
    if nbytes > MAX_CUTOUT_BYTES:
        raise HTTPException(413, f"cutout is {nbytes} bytes; limit {MAX_CUTOUT_BYTES}")
    arr = get_reader(ds).read_cutout(z0, z1, y0, y1, x0, x1)
    body, media = _encode(arr, fmt)
    _log(s, ds, "cutout", {"z0": z0, "z1": z1, "y0": y0, "y1": y1, "x0": x0, "x1": x1}, len(body), fmt, request, t0, stream_id=stream_id)
    return Response(body, media_type=media, headers={"X-Shape": "x".join(map(str, arr.shape)), "X-Dtype": str(arr.dtype)})


class PatchSampleIn(BaseModel):
    size: list[int] = Field(default=[16, 256, 256], description="patch size (dz, dy, dx)")
    n: int = 16
    seed: int | None = None
    only_passed: bool = True  # only z windows whose slices all passed the latest QC run
    min_quality: float | None = None  # additionally require slice quality_score >= this
    split: str | None = None  # train | train_sample | inference  (block split filter)
    block_ids: list[str] | None = None


@router.post("/{dataset_id}/patches/sample")
def sample_patches(dataset_id: str, body: PatchSampleIn, request: Request, s: Session = Depends(get_session)):
    """Pick random patch coordinates that satisfy the QC filter. Returns coordinates + cutout URLs (no pixels).

    Patches never straddle blocks: a patch lies inside one block's XY tile and inside a z window whose sections all
    passed the latest QC run for that block (a crack in one tile does not disqualify the other tiles).
    """
    t0 = time.perf_counter()
    ds = _ds(s, dataset_id)
    dz, dy, dx = body.size
    if dz > ds.size_z or dy > ds.size_y or dx > ds.size_x:
        raise HTTPException(422, "patch larger than the volume")
    blocks = list(s.scalars(select(Block).where(Block.dataset_id == dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start)))
    if body.split or body.block_ids:
        blocks = [b for b in blocks if (body.split and b.split == body.split) or (body.block_ids and b.block_id in body.block_ids)]
    qc_used = bool(ds.latest_run_id and (body.only_passed or body.min_quality is not None))
    allowed: dict[str, np.ndarray] = {b.block_id: np.ones(b.z_end - b.z_start, dtype=bool) for b in blocks}
    if qc_used:
        for m in allowed.values():
            m[:] = False
        by_block = {b.block_id: b for b in blocks}
        for bid, z, passed, q in s.execute(select(QCSlice.block_id, QCSlice.z, QCSlice.passed, QCSlice.quality_score).where(QCSlice.run_id == ds.latest_run_id)):
            b = by_block.get(bid)
            if b is None:
                continue
            ok = (bool(passed) or not body.only_passed) and (body.min_quality is None or (q or 0.0) >= body.min_quality)
            allowed[bid][z - b.z_start] = ok
    cands: list[tuple[Block, int]] = []
    for b in blocks:
        if (b.y_end - b.y_start) < dy or (b.x_end - b.x_start) < dx or (b.z_end - b.z_start) < dz:
            continue
        ok = np.convolve(allowed[b.block_id].astype(int), np.ones(dz, dtype=int), mode="valid") == dz
        cands.extend((b, b.z_start + int(i)) for i in np.flatnonzero(ok))
    if not cands:
        raise HTTPException(409, "no z window satisfies the QC filter")
    rng = np.random.default_rng(body.seed)
    picks = rng.choice(len(cands), size=body.n, replace=len(cands) < body.n)
    out = []
    for i in picks:
        b, z0 = cands[int(i)]
        y0 = int(rng.integers(b.y_start, b.y_end - dy + 1))
        x0 = int(rng.integers(b.x_start, b.x_end - dx + 1))
        out.append({
            "bbox": {"z0": z0, "z1": z0 + dz, "y0": y0, "y1": y0 + dy, "x0": x0, "x1": x0 + dx},
            "block_id": b.block_id,
            "grade": b.latest_grade,
            "url": f"/api/v1/data/{dataset_id}/cutout?z0={z0}&z1={z0 + dz}&y0={y0}&y1={y0 + dy}&x0={x0}&x1={x0 + dx}&fmt=npy",
        })
    qc_filter = {"only_passed": body.only_passed, "min_quality": body.min_quality, "split": body.split, "block_ids": body.block_ids, "run_id": ds.latest_run_id if qc_used else None}
    _log(s, ds, "patch_sample", {"size": body.size, "n": body.n, "seed": body.seed}, 0, "json", request, t0, qc_filter)
    n_allowed = int(sum(int(m.sum()) for m in allowed.values()))
    return {"dataset_id": dataset_id, "run_id": ds.latest_run_id if qc_used else None, "qc_filter": qc_filter, "n_allowed_slices": n_allowed, "n_candidate_windows": len(cands), "n_blocks_considered": len(blocks), "patches": out}


class ExportIn(BaseModel):
    fmt: str = "npy"
    min_run: int = 1
    min_quality: float | None = None
    run_id: int | None = None
    block_ids: list[str] | None = None
    with_assets: list[str] | None = None
    out_dir: str | None = None  # default var/exports (server side)


@router.post("/{dataset_id}/export")
def export_dataset(dataset_id: str, body: ExportIn, request: Request, s: Session = Depends(get_session)):
    """Write the QC-passing sections to files on the platform host and return the export manifest."""
    from emqc.qc.export import export_passed

    t0 = time.perf_counter()
    ds = _ds(s, dataset_id)
    out = body.out_dir or str(settings.export_dir)
    try:
        m = export_passed(dataset_id, out, run_id=body.run_id, fmt=body.fmt, min_run=body.min_run, block_ids=body.block_ids, min_quality=body.min_quality, with_assets=body.with_assets or [])
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    _log(s, ds, "export", {"min_run": body.min_run, "n_sections": m["n_sections"], "n_shards": m["n_shards"]}, m["bytes"], body.fmt, request, t0, {"run_id": m["run_id"], "min_quality": body.min_quality})
    return {"out_dir": str(Path(out) / dataset_id), **m}


class BatchIn(BaseModel):
    bboxes: list[list[int]] = Field(description="list of [z0, z1, y0, y1, x0, x1]")


@router.post("/{dataset_id}/cutouts/batch")
def cutouts_batch(dataset_id: str, body: BatchIn, request: Request, s: Session = Depends(get_session)):
    """Many cutouts in one response: an .npz with arrays patch_0, patch_1, ... and a `bboxes` array."""
    t0 = time.perf_counter()
    ds = _ds(s, dataset_id)
    reader = get_reader(ds)
    total = 0
    arrays: dict[str, np.ndarray] = {}
    for i, bb in enumerate(body.bboxes):
        if len(bb) != 6:
            raise HTTPException(422, "each bbox must be [z0, z1, y0, y1, x0, x1]")
        z0, z1, y0, y1, x0, x1 = _bounds(ds, *bb)
        total += (z1 - z0) * (y1 - y0) * (x1 - x0) * np.dtype(ds.dtype).itemsize
        if total > MAX_CUTOUT_BYTES:
            raise HTTPException(413, f"batch exceeds {MAX_CUTOUT_BYTES} bytes")
        arrays[f"patch_{i}"] = reader.read_cutout(z0, z1, y0, y1, x0, x1)
    arrays["bboxes"] = np.asarray(body.bboxes, dtype=np.int64)
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    data = buf.getvalue()
    _log(s, ds, "batch", {"n": len(body.bboxes)}, len(data), "npz", request, t0)
    return Response(data, media_type="application/octet-stream", headers={"X-Count": str(len(body.bboxes))})


@router.get("/training/manifest")
def training_manifest(s: Session = Depends(get_session)):
    """What goes where: small datasets -> training; large datasets -> sampled blocks for training, whole volume for inference."""
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pipeline_version": settings.pipeline_version, "train": [], "inference": []}
    for ds in s.scalars(select(Dataset).order_by(Dataset.dataset_id)):
        blocks = list(s.scalars(select(Block).where(Block.dataset_id == ds.dataset_id).order_by(Block.z_start)))
        qc = {b.block_id: b for b in s.scalars(select(QCBlock).where(QCBlock.run_id == ds.latest_run_id))} if ds.latest_run_id else {}

        def bd(b: Block) -> dict:
            q = qc.get(b.block_id)
            return {"block_id": b.block_id, "z_start": b.z_start, "z_end": b.z_end, "y_start": b.y_start, "y_end": b.y_end, "x_start": b.x_start, "x_end": b.x_end, "split": b.split, "grade": q.grade if q else None, "longest_clean_run": q.longest_clean_run if q else None, "quality_score": q.quality_score if q else None, "retention_rate": q.retention_rate if q else None, "max_severity": q.max_severity if q else None, "dominant_failure": q.dominant_failure if q else None, "failure_types": (q.failure_types if q else [])}

        entry = {"dataset_id": ds.dataset_id, "size_class": ds.size_class, "grade": ds.latest_grade, "shape": {"z": ds.size_z, "y": ds.size_y, "x": ds.size_x}, "voxel_size_nm": [ds.voxel_size_x_nm, ds.voxel_size_y_nm, ds.voxel_size_z_nm], "species": ds.species, "brain_region": ds.brain_region, "qc_run_id": ds.latest_run_id, "quality_score": ds.latest_quality_score, "retention_rate": ds.latest_retention_rate, "data_url": f"/api/v1/data/{ds.dataset_id}/info"}
        if ds.size_class == "small":
            out["train"].append({**entry, "blocks": [bd(b) for b in blocks], "note": "small dataset: all blocks"})
        else:
            run = s.get(QCRun, ds.latest_run_id) if ds.latest_run_id else None
            sampling = (run.config_json or {}).get("train_sampling") if run else None
            out["train"].append({**entry, "blocks": [bd(b) for b in blocks if b.split == "train_sample"], "note": "large dataset: sampled blocks", "sampling": sampling})
            out["inference"].append({**entry, "blocks": [bd(b) for b in blocks]})
    out["policy"] = {"small": "all blocks -> train", "large": f"{settings.train_sample_policy} sample of {settings.train_sample_blocks} blocks ({settings.train_sample_strata}), grade D excluded -> train; whole volume -> inference"}
    return out
