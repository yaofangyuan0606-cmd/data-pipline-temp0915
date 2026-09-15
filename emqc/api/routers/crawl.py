"""Acquisition API: inspect a cloud volume, judge an ROI before paying for it, register it, materialise it."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.crawl.index import H01_PROPS_URL, load_properties, parse, select as select_rows
from emqc.crawl.precheck import H01_MASK_URL, tissue_profile
from emqc.crawl.register import CrawlRunner, create_crawl_job, register_cloud_roi, request_cancel_crawl, start_crawl_async
from emqc.db.base import get_session
from emqc.db.models import CrawlEvent, CrawlJob, Dataset
from emqc.registry.cloud import CloudVolumeReader, roi_dict, volume_info

from ..serializers import crawl_event_to_dict, crawl_job_to_dict, dataset_to_dict

router = APIRouter(prefix="/api/v1/crawl", tags=["acquisition"])

PRESETS = {
    "h01": {
        "label": "H01 human temporal cortex (Harvard / Google)",
        "em": {"url": "gs://h01-release/data/20210601/4nm_raw", "mip": 1, "note": "mip1 = 8 nm; jpeg, 有损"},
        "seg": {"url": "gs://h01-release/data/20210601/c3", "mip": 0, "note": "compressed_segmentation, 无损, 与 EM mip1 同为 8 nm 网格"},
        "mask": {"url": H01_MASK_URL, "mip": 0, "note": "64 nm 组织类型图，用于爬前预判"},
        "props": H01_PROPS_URL,
        "species": "human", "brain_region": "temporal cortex",
    }
}


class RoiIn(BaseModel):
    x: list[int] = Field(min_length=2, max_length=2)
    y: list[int] = Field(min_length=2, max_length=2)
    z: list[int] = Field(min_length=2, max_length=2)


@router.get("/presets")
def presets():
    return PRESETS


@router.get("/volume")
def volume(url: str, mip: int = 0):
    """Shape, chunk grid, resolution and encoding of a cloud volume — no voxels downloaded."""
    try:
        return volume_info(url, mip)
    except Exception as e:
        raise HTTPException(422, f"{type(e).__name__}: {e}")


class PrecheckIn(BaseModel):
    roi: RoiIn
    mask_url: str = H01_MASK_URL
    roi_resolution_nm: list[float] = [8.0, 8.0, 33.0]
    min_wanted: float = 0.5
    max_defect: float = 0.02


@router.post("/precheck")
def precheck(body: PrecheckIn):
    """Judge an ROI from the tissue-type mask before spending bandwidth on voxels."""
    try:
        return tissue_profile(body.roi.model_dump(), mask_url=body.mask_url, roi_resolution_nm=tuple(body.roi_resolution_nm),
                              min_wanted=body.min_wanted, max_defect=body.max_defect)
    except Exception as e:
        raise HTTPException(422, f"{type(e).__name__}: {e}")


@router.get("/index")
def index(props_url: str = H01_PROPS_URL, tag: str | None = None, min_voxels: float = 0.0, top: int = 20, list_tags: bool = False):
    """Candidate segments from the volume's segment_properties table (a few MB)."""
    try:
        rows, tags = parse(load_properties(props_url))
    except Exception as e:
        raise HTTPException(422, f"{type(e).__name__}: {e}")
    if list_tags:
        return {"n_segments": len(rows), "tags": tags}
    return {"n_segments": len(rows), "tags": tags[:40], "selected": select_rows(rows, tag=tag, min_voxels=min_voxels, top=top)}


class RegisterIn(BaseModel):
    dataset_id: str
    url: str
    roi: RoiIn
    mip: int = 0
    name: str | None = None
    species: str | None = None
    brain_region: str | None = None
    assets: list[dict] | None = None
    run_precheck: bool = True
    mask_url: str = H01_MASK_URL
    require_precheck_ok: bool = False


@router.post("/register", status_code=201)
def register(body: RegisterIn, s: Session = Depends(get_session)):
    """Register an ROI as a dataset. Nothing is downloaded — QC and patches read it on demand."""
    roi = body.roi.model_dump()
    pre = None
    if body.run_precheck:
        try:
            pre = tissue_profile(roi, mask_url=body.mask_url)
        except Exception as e:
            pre = {"verdict": "unknown", "error": f"{type(e).__name__}: {e}"}
        if body.require_precheck_ok and pre.get("verdict") == "reject":
            raise HTTPException(409, f"ROI 预判不合格：{'; '.join(pre.get('reasons') or [])}")
    try:
        ds = register_cloud_roi(s, body.dataset_id, body.url, roi, mip=body.mip, name=body.name, species=body.species,
                                brain_region=body.brain_region, assets=body.assets, precheck=pre)
    except Exception as e:
        raise HTTPException(422, f"{type(e).__name__}: {e}")
    s.commit()
    return {**dataset_to_dict(ds), "precheck": pre}


class CrawlIn(BaseModel):
    dataset_id: str
    url: str
    roi: RoiIn
    mip: int = 0
    out_dir: str | None = None
    align: bool = True
    resume: bool = True
    sync: bool = False


@router.post("/jobs", status_code=202)
def create_job(body: CrawlIn, s: Session = Depends(get_session)):
    """Materialise an ROI into data_root as an image stack (resumable)."""
    if body.sync:
        job_id = create_crawl_job(body.dataset_id, body.url, body.roi.model_dump(), body.mip, body.out_dir, align=body.align, resume=body.resume)
        try:
            CrawlRunner(job_id).run()
        except Exception as e:
            raise HTTPException(422, f"{type(e).__name__}: {e}")
    else:
        job_id = start_crawl_async(body.dataset_id, body.url, body.roi.model_dump(), body.mip, body.out_dir, align=body.align, resume=body.resume)
    s.rollback()
    return crawl_job_to_dict(s.get(CrawlJob, job_id))


@router.get("/jobs")
def list_jobs(dataset_id: str | None = None, limit: int = 50, s: Session = Depends(get_session)):
    q = select(CrawlJob).order_by(CrawlJob.id.desc()).limit(limit)
    if dataset_id:
        q = q.where(CrawlJob.dataset_id == dataset_id)
    return [crawl_job_to_dict(j) for j in s.scalars(q)]


def _job(s: Session, job_id: int) -> CrawlJob:
    j = s.get(CrawlJob, job_id)
    if j is None:
        raise HTTPException(404, f"unknown crawl job {job_id}")
    return j


@router.get("/jobs/{job_id}")
def get_job(job_id: int, s: Session = Depends(get_session)):
    return crawl_job_to_dict(_job(s, job_id))


@router.get("/jobs/{job_id}/events")
def job_events(job_id: int, after_id: int = 0, limit: int = 300, s: Session = Depends(get_session)):
    _job(s, job_id)
    return [crawl_event_to_dict(e) for e in s.scalars(select(CrawlEvent).where(CrawlEvent.job_id == job_id, CrawlEvent.id > after_id).order_by(CrawlEvent.id).limit(limit))]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, s: Session = Depends(get_session)):
    j = _job(s, job_id)
    if not request_cancel_crawl(job_id):
        raise HTTPException(409, f"crawl job {job_id} is {j.status}, nothing to cancel")
    s.rollback()
    return crawl_job_to_dict(s.get(CrawlJob, job_id))
