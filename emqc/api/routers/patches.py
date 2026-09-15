"""Patch Factory / Training Data API (requirement 3): label validation, holdout partition, patch sets and label cutouts."""
from __future__ import annotations

import io
import time

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.base import get_session
from emqc.db.models import PATCH_TYPES, Block, Dataset, DatasetAsset, LabelQC, Patch, PatchSet, ServeLog
from emqc.patches.generate import PatchSpec, generate_patch_set, readiness
from emqc.patches.partition import assign_partitions
from emqc.qc.labels import open_label_for, validate_exclusive_masks, validate_label_asset
from emqc.registry.labels import boundary_map

from ..serializers import asset_to_dict, block_to_dict, label_qc_to_dict, patch_to_dict, patchset_to_dict

router = APIRouter(prefix="/api/v1", tags=["patch-factory"])

_label_readers: dict[int, object] = {}


def _ds(s: Session, dataset_id: str) -> Dataset:
    ds = s.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, f"unknown dataset {dataset_id}")
    return ds


def _asset(s: Session, ds: Dataset, asset_id: int) -> DatasetAsset:
    a = s.get(DatasetAsset, asset_id)
    if a is None or a.dataset_id != ds.dataset_id:
        raise HTTPException(404, f"asset {asset_id} not in {ds.dataset_id}")
    return a


# ============================================================================= labels


@router.get("/data/{dataset_id}/labels")
def list_labels(dataset_id: str, s: Session = Depends(get_session)):
    ds = _ds(s, dataset_id)
    out = []
    for a in ds.assets:
        if a.asset_type == "em_image":
            continue
        d = asset_to_dict(a)
        v = (a.extra_json or {}).get("validation")
        d["validation"] = v
        d["usable_for_patches"] = bool(v and v.get("usable_for_patches"))
        out.append(d)
    return out


@router.post("/datasets/{dataset_id}/assets/{asset_id}/validate")
def validate_asset(dataset_id: str, asset_id: int, step: int = 1, max_sections: int | None = None, s: Session = Depends(get_session)):
    """Check a label asset against the EM: missing / empty sections, labels over fill regions, shape & z alignment."""
    ds = _ds(s, dataset_id)
    a = _asset(s, ds, asset_id)
    if not (a.format or "").startswith(("image_stack", "precomputed")) and a.format not in ("npy", ""):
        raise HTTPException(422, f"asset format {a.format!r} is not a volume; only image stacks / npy / precomputed labels can be validated")
    summary = validate_label_asset(s, ds, a, step=max(1, step), max_sections=max_sections)
    _label_readers.pop(asset_id, None)
    return {"asset_id": a.id, "path": a.path, **summary}


class ExclusiveIn(BaseModel):
    asset_ids: list[int]
    step: int = 1
    max_sections: int | None = None


@router.post("/datasets/{dataset_id}/assets/validate-exclusive")
def validate_exclusive(dataset_id: str, body: ExclusiveIn, s: Session = Depends(get_session)):
    """Class masks that must not overlap (label conflict)."""
    ds = _ds(s, dataset_id)
    assets = [_asset(s, ds, i) for i in body.asset_ids]
    if len(assets) < 2:
        raise HTTPException(422, "need at least two masks")
    return validate_exclusive_masks(s, ds, assets, step=max(1, body.step), max_sections=body.max_sections)


@router.get("/datasets/{dataset_id}/assets/{asset_id}/validation")
def get_validation(dataset_id: str, asset_id: int, only_flagged: bool = False, s: Session = Depends(get_session)):
    ds = _ds(s, dataset_id)
    a = _asset(s, ds, asset_id)
    q = select(LabelQC).where(LabelQC.asset_id == a.id).order_by(LabelQC.z)
    if only_flagged:
        q = q.where(LabelQC.passed.is_(False))
    return {"asset_id": a.id, "path": a.path, "summary": (a.extra_json or {}).get("validation"), "label_conflict": (a.extra_json or {}).get("label_conflict"), "sections": [label_qc_to_dict(r) for r in s.scalars(q)]}


def _label_reader(ds: Dataset, a: DatasetAsset):
    r = _label_readers.get(a.id)
    if r is None:
        r = open_label_for(ds, a)
        _label_readers[a.id] = r
    return r


@router.get("/data/{dataset_id}/labels/{asset_id}/cutout")
def label_cutout(dataset_id: str, asset_id: int, request: Request, z0: int, z1: int, y0: int, y1: int, x0: int, x1: int, derive: str = "ids", s: Session = Depends(get_session)):
    """Label cutout aligned with the EM cutout of the same bbox. derive = ids (uint32) | boundary (uint8) | mask (uint8, != 0)."""
    t0 = time.perf_counter()
    ds = _ds(s, dataset_id)
    a = _asset(s, ds, asset_id)
    v = (a.extra_json or {}).get("validation") or {}
    scale = v.get("scale_to_em") or [1, 1]
    if scale != [1, 1]:
        raise HTTPException(409, f"label is at 1/{scale[0]} of EM resolution; only EM-resolution labels are served as cutouts")
    if not (0 <= z0 < z1 <= ds.size_z and 0 <= y0 < y1 <= ds.size_y and 0 <= x0 < x1 <= ds.size_x):
        raise HTTPException(422, "cutout out of bounds")
    if (z1 - z0) * (y1 - y0) * (x1 - x0) * 4 > 256 * 1024 * 1024:
        raise HTTPException(413, "label cutout too large")
    reader = _label_reader(ds, a)
    arr = reader.read_cutout(z0, z1, y0, y1, x0, x1)
    if derive == "boundary":
        arr = np.stack([boundary_map(sl) for sl in arr]).astype(np.uint8)
    elif derive == "mask":
        arr = (arr != 0).astype(np.uint8)
    elif derive != "ids":
        raise HTTPException(422, "derive must be ids | boundary | mask")
    buf = io.BytesIO()
    np.save(buf, np.ascontiguousarray(arr))
    body = buf.getvalue()
    s.add(ServeLog(dataset_id=ds.dataset_id, kind=f"label_cutout:{derive}", bbox_json={"z0": z0, "z1": z1, "y0": y0, "y1": y1, "x0": x0, "x1": x1, "asset_id": a.id}, n_bytes=len(body), fmt="npy",
                   client=request.client.host if request.client else None, duration_ms=(time.perf_counter() - t0) * 1000.0))
    s.commit()
    return Response(body, media_type="application/x-npy")


# ============================================================================= partition


class PartitionIn(BaseModel):
    ratios: str | None = None  # "0.8,0.1,0.1"
    seed: int | None = None
    force: bool = False


def _partition_summary(s: Session, ds: Dataset) -> dict:
    blocks = list(s.scalars(select(Block).where(Block.dataset_id == ds.dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start)))
    counts: dict[str, int] = {}
    for b in blocks:
        counts[b.partition or "none"] = counts.get(b.partition or "none", 0) + 1
    return {"dataset_id": ds.dataset_id, "counts": counts, "config": (ds.metadata_json or {}).get("partition"), "blocks": [block_to_dict(b) for b in blocks]}


@router.get("/datasets/{dataset_id}/partition")
def get_partition(dataset_id: str, s: Session = Depends(get_session)):
    return _partition_summary(s, _ds(s, dataset_id))


@router.post("/datasets/{dataset_id}/partition")
def set_partition(dataset_id: str, body: PartitionIn, s: Session = Depends(get_session)):
    """(Re)assign the holdout partition. Without force only unassigned eligible blocks are placed; force reshuffles all."""
    ds = _ds(s, dataset_id)
    blocks = list(s.scalars(select(Block).where(Block.dataset_id == ds.dataset_id)))
    res = assign_partitions(blocks, ratios=body.ratios or settings.partition_ratios, seed=body.seed if body.seed is not None else settings.partition_seed, force=body.force)
    ds.metadata_json = {**(ds.metadata_json or {}), "partition": {**res.as_dict(), "forced": body.force}}
    s.commit()
    return {"result": res.as_dict(), **_partition_summary(s, ds)}


# ============================================================================= patch sets


class PatchSetIn(BaseModel):
    dataset_id: str
    patch_type: str
    size: list[int] = Field(default=[16, 256, 256], min_length=3, max_length=3)
    n: int = 64
    seed: int = 0
    partitions: list[str] | None = None
    block_ids: list[str] | None = None
    only_passed: bool = True
    min_quality: float | None = None
    label_asset_id: int | None = None
    params: dict = Field(default_factory=dict)
    preprocessing: dict | None = None
    augmentation: dict | None = None
    candidate_factor: int = 8


@router.get("/data/{dataset_id}/patch-readiness")
def patch_readiness(dataset_id: str, s: Session = Depends(get_session)):
    ds = _ds(s, dataset_id)
    return {"dataset_id": dataset_id, "types": readiness(s, ds), "partition": {k: v for k, v in _partition_summary(s, ds).items() if k != "blocks"}}


@router.post("/patchsets", status_code=201)
def create_patchset(body: PatchSetIn, s: Session = Depends(get_session)):
    ds = _ds(s, body.dataset_id)
    if body.patch_type not in PATCH_TYPES:
        raise HTTPException(422, f"patch_type must be one of {PATCH_TYPES}")
    dz, dy, dx = body.size
    if dz > ds.size_z or dy > ds.size_y or dx > ds.size_x or min(body.size) < 1:
        raise HTTPException(422, "patch size out of range for this volume")
    spec = PatchSpec(patch_type=body.patch_type, size=(dz, dy, dx), n=body.n, seed=body.seed, partitions=body.partitions, block_ids=body.block_ids, only_passed=body.only_passed,
                     min_quality=body.min_quality, label_asset_id=body.label_asset_id, params=body.params, preprocessing=body.preprocessing, augmentation=body.augmentation, candidate_factor=body.candidate_factor)
    try:
        pset = generate_patch_set(s, ds, spec)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return patchset_to_dict(pset)


@router.get("/patchsets")
def list_patchsets(dataset_id: str | None = None, patch_type: str | None = None, limit: int = 50, s: Session = Depends(get_session)):
    q = select(PatchSet).order_by(PatchSet.id.desc()).limit(limit)
    if dataset_id:
        q = q.where(PatchSet.dataset_id == dataset_id)
    if patch_type:
        q = q.where(PatchSet.patch_type == patch_type)
    return [patchset_to_dict(p) for p in s.scalars(q)]


def _pset(s: Session, set_id: int) -> PatchSet:
    p = s.get(PatchSet, set_id)
    if p is None:
        raise HTTPException(404, f"unknown patch set {set_id}")
    return p


@router.get("/patchsets/{set_id}")
def get_patchset(set_id: int, s: Session = Depends(get_session)):
    return patchset_to_dict(_pset(s, set_id))


@router.get("/patchsets/{set_id}/patches")
def list_patches(set_id: int, partition: str | None = None, block_id: str | None = None, offset: int = 0, limit: int = 500, s: Session = Depends(get_session)):
    p = _pset(s, set_id)
    q = select(Patch).where(Patch.set_id == p.id).order_by(Patch.id).offset(offset).limit(min(limit, 5000))
    cnt = select(func.count()).select_from(Patch).where(Patch.set_id == p.id)
    if partition:
        q, cnt = q.where(Patch.partition == partition), cnt.where(Patch.partition == partition)
    if block_id:
        q, cnt = q.where(Patch.block_id == block_id), cnt.where(Patch.block_id == block_id)
    total = s.scalar(cnt) or 0
    items = [patch_to_dict(x, p.label_asset_id) for x in s.scalars(q)]
    return {"set_id": p.id, "total": total, "offset": offset, "limit": limit, "items": items, "next_offset": offset + len(items) if offset + len(items) < total else None}


@router.get("/patchsets/{set_id}/manifest")
def patchset_manifest(set_id: int, s: Session = Depends(get_session)):
    """Everything a training job needs in one document: set lineage + all patches with EM / label URLs."""
    p = _pset(s, set_id)
    ds = _ds(s, p.dataset_id)
    items = [patch_to_dict(x, p.label_asset_id) for x in s.scalars(select(Patch).where(Patch.set_id == p.id).order_by(Patch.id))]
    return {**patchset_to_dict(p), "dataset": {"dataset_id": ds.dataset_id, "shape": {"z": ds.size_z, "y": ds.size_y, "x": ds.size_x}, "dtype": ds.dtype, "voxel_size_nm": [ds.voxel_size_x_nm, ds.voxel_size_y_nm, ds.voxel_size_z_nm], "species": ds.species, "brain_region": ds.brain_region},
            "patches": items, "how_to_load": "GET url_em -> npy (dz, dy, dx) EM; GET url_label -> npy labels of the same bbox (ids uint32 | boundary uint8 | mask uint8). See emqc/loader.py PatchSetClient."}


@router.delete("/patchsets/{set_id}")
def delete_patchset(set_id: int, s: Session = Depends(get_session)):
    p = _pset(s, set_id)
    s.execute(Patch.__table__.delete().where(Patch.set_id == p.id))
    s.delete(p)
    s.commit()
    return {"deleted": set_id}
