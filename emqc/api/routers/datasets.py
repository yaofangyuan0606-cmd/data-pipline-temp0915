"""Dataset registry API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.base import get_session
from emqc.db.models import ASSET_TYPES, Block, Dataset, DatasetAsset, DatasetVersion, QCRun
from emqc.registry.scanner import delete_dataset, scan

from ..serializers import asset_to_dict, block_to_dict, dataset_to_dict, run_to_dict, version_to_dict

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])

LAST_SCAN: dict = {}  # in-process memory of the most recent scan (shown on the pipeline page)


def _get(s: Session, dataset_id: str) -> Dataset:
    ds = s.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, f"unknown dataset {dataset_id}")
    return ds


@router.get("")
def list_datasets(size_class: str | None = None, status: str | None = None, s: Session = Depends(get_session)):
    q = select(Dataset).order_by(Dataset.dataset_id)
    if size_class:
        q = q.where(Dataset.size_class == size_class)
    if status:
        q = q.where(Dataset.status == status)
    return [dataset_to_dict(d) for d in s.scalars(q)]


@router.post("/scan")
def scan_datasets(s: Session = Depends(get_session)):
    from .data import invalidate_readers

    from datetime import datetime, timezone

    res = scan(s)
    invalidate_readers()
    LAST_SCAN.clear()
    LAST_SCAN.update({**res.as_dict(), "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "data_root": str(settings.data_root), "glob": settings.dataset_glob})
    return LAST_SCAN


@router.get("/{dataset_id}")
def get_dataset(dataset_id: str, s: Session = Depends(get_session)):
    ds = _get(s, dataset_id)
    d = dataset_to_dict(ds, with_children=True)
    if ds.latest_run_id:
        run = s.get(QCRun, ds.latest_run_id)
        d["latest_run"] = run_to_dict(run) if run else None
    return d


@router.delete("/{dataset_id}")
def remove_dataset(dataset_id: str, remove_files: bool = False, s: Session = Depends(get_session)):
    """Unregister a dataset and delete its QC results, previews and (optionally) files under data_root."""
    from .data import invalidate_readers

    _get(s, dataset_id)
    try:
        counts = delete_dataset(s, dataset_id, remove_files=remove_files)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    invalidate_readers()
    return {"deleted": dataset_id, **counts}


@router.get("/{dataset_id}/blocks")
def get_blocks(dataset_id: str, s: Session = Depends(get_session)):
    _get(s, dataset_id)
    return [block_to_dict(b) for b in s.scalars(select(Block).where(Block.dataset_id == dataset_id).order_by(Block.z_start))]


@router.get("/{dataset_id}/assets")
def get_assets(dataset_id: str, s: Session = Depends(get_session)):
    return [asset_to_dict(a) for a in _get(s, dataset_id).assets]


class AssetIn(BaseModel):
    asset_type: str
    path: str
    format: str = ""
    version: str = ""
    algo_version: str | None = None
    model_version: str | None = None
    experiment_id: str | None = None
    extra: dict = {}


@router.post("/{dataset_id}/assets", status_code=201)
def add_asset(dataset_id: str, body: AssetIn, s: Session = Depends(get_session)):
    """Register a new prediction / skeleton / trace file produced by an algorithm run.

    The existence check goes through the dataset's own file system, so it works for a dataset that lives on
    a remote machine (sftp://...) as well as a local one. An unreachable source is recorded rather than
    reported as "missing"."""
    from emqc.registry import scanner

    ds = _get(s, dataset_id)
    if body.asset_type not in ASSET_TYPES:
        raise HTTPException(422, f"asset_type must be one of {ASSET_TYPES}")
    extra = dict(body.extra or {})
    try:
        fs, base = scanner.open_root(ds.root_path)
        full = fs.join(base, body.path) if body.path not in ("", ".") else base
        exists = fs.exists(full)
        size = scanner._size(fs, full) if exists else None
    except Exception as e:  # source unreachable: register anyway, say why
        exists, size = False, None
        extra["source_check_error"] = f"{type(e).__name__}: {e}"[:300]
    a = DatasetAsset(
        dataset_id=dataset_id, asset_type=body.asset_type, path=body.path, format=body.format, version=body.version,
        algo_version=body.algo_version, model_version=body.model_version, experiment_id=body.experiment_id, extra_json=extra,
        exists=exists, size_bytes=size,
    )
    s.add(a)
    s.commit()
    return asset_to_dict(a)


@router.get("/{dataset_id}/versions")
def get_versions(dataset_id: str, s: Session = Depends(get_session)):
    return [version_to_dict(v) for v in _get(s, dataset_id).versions]


class VersionIn(BaseModel):
    kind: str  # data | algo | model | experiment | qc_pipeline
    version: str
    note: str = ""


@router.post("/{dataset_id}/versions", status_code=201)
def add_version(dataset_id: str, body: VersionIn, s: Session = Depends(get_session)):
    _get(s, dataset_id)
    existing = s.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id, DatasetVersion.kind == body.kind, DatasetVersion.version == body.version))
    if existing:
        existing.note = body.note or existing.note
        s.commit()
        return version_to_dict(existing)
    v = DatasetVersion(dataset_id=dataset_id, kind=body.kind, version=body.version, note=body.note)
    s.add(v)
    s.commit()
    return version_to_dict(v)


class MetadataIn(BaseModel):
    name: str | None = None
    species: str | None = None
    brain_region: str | None = None
    voxel_size_nm: list[float] | None = None
    staining: str | None = None
    imaging_modality: str | None = None
    acquisition_batch: str | None = None
    size_class: str | None = None


@router.patch("/{dataset_id}")
def patch_dataset(dataset_id: str, body: MetadataIn, s: Session = Depends(get_session)):
    ds = _get(s, dataset_id)
    for k in ("name", "species", "brain_region", "staining", "imaging_modality", "acquisition_batch"):
        v = getattr(body, k)
        if v is not None:
            setattr(ds, k, v)
    if body.voxel_size_nm:
        ds.voxel_size_x_nm, ds.voxel_size_y_nm, ds.voxel_size_z_nm = body.voxel_size_nm
    if body.size_class in ("small", "large"):
        ds.size_class = body.size_class
        ds.usage = "train" if body.size_class == "small" else "train+inference"
    s.commit()
    return dataset_to_dict(ds, with_children=True)
