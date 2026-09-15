"""Register an ROI of a cloud volume as a platform dataset, and (optionally) materialise it to disk.

Registering does **not** download voxels: the dataset's root_path is the precomputed URL and its ROI lives
in metadata, so QC, patch generation and the data API read through CloudVolumeReader on demand, exactly as
they do for an SSH source. Materialising is a separate, resumable job for when the same ROI will be read
many times.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db.base import session_scope
from ..db.models import Block, CrawlEvent, CrawlJob, Dataset, DatasetAsset, DatasetVersion
from ..registry.cloud import CloudVolumeReader, normalize_url, roi_dict, volume_info
from ..registry.scanner import make_blocks

log = __import__("logging").getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def register_cloud_roi(s: Session, dataset_id: str, url: str, roi, mip: int = 0, name: str | None = None, project: str = "cloud",
                       species: str | None = None, brain_region: str | None = None, assets: list[dict] | None = None,
                       precheck: dict | None = None, size_class: str = "auto", extra: dict | None = None) -> Dataset:
    """Create / update a dataset row backed by a cloud ROI. Voxels are read on demand."""
    requested = roi_dict(roi)
    reader = CloudVolumeReader(url, requested, mip=mip, cache_dir=settings.cache_dir)
    d = reader.describe()
    # the stored ROI is the one that will actually be read: aligning outward to the chunk grid is free
    # (identical bytes cross the wire) but it changes the shape, so shape and ROI must agree from here on
    r = roi_dict(d["roi_xyz"])
    nz, ny, nx = reader.shape
    res = d["resolution_nm"]
    ds = s.get(Dataset, dataset_id)
    created = ds is None
    if ds is None:
        ds = Dataset(dataset_id=dataset_id)
        s.add(ds)
    ds.name = name or dataset_id
    ds.project = project
    ds.root_path = normalize_url(url)
    ds.em_path = ""
    ds.em_format = "precomputed_cloud"
    ds.em_axes = "zyx"
    ds.dtype = reader.info.dtype
    ds.size_z, ds.size_y, ds.size_x = nz, ny, nx
    ds.n_voxels = nz * ny * nx
    ds.voxel_size_x_nm, ds.voxel_size_y_nm, ds.voxel_size_z_nm = res[0], res[1], res[2]
    if species:
        ds.species = species
    if brain_region:
        ds.brain_region = brain_region
    ds.acquisition_batch = ds.acquisition_batch or dataset_id
    n_vox = ds.n_voxels
    ds.size_class = size_class if size_class in ("small", "large") else ("large" if n_vox >= settings.large_dataset_voxels else "small")
    ds.usage = "train" if ds.size_class == "small" else "train+inference"
    ds.status = ds.status or "registered"
    ds.metadata_json = {
        **(ds.metadata_json or {}), **(extra or {}),
        "source": "cloud", "cloud_url": normalize_url(url), "roi_xyz": d["roi_xyz"], "roi": r, "roi_requested": requested, "mip": mip,
        "z_offset": d["roi_xyz"][4], "chunk_xyz": d["chunk_xyz"], "encoding": d["encoding"], "lossy": d["lossy"],
        "aligned_xy": d["aligned_xy"], "extent_um": d["extent_um"], "fill_value": 0,
        **({"roi_precheck": precheck} if precheck else {}),
    }
    reader.close()
    # assets on the same ROI (e.g. H01's c3 segmentation) are cloud volumes too
    for a in assets or []:
        aurl = normalize_url(a["url"])
        key = (a.get("type", "other"), aurl)
        row = next((x for x in ds.assets if (x.asset_type, x.path) == key), None)
        if row is None:
            row = DatasetAsset(dataset_id=dataset_id, asset_type=a.get("type", "other"), path=aurl)
            ds.assets.append(row)
        row.format = a.get("format", "precomputed_cloud")
        row.version = a.get("version", "") or f"mip{a.get('mip', 0)}"
        row.exists = True
        row.extra_json = {**(row.extra_json or {}), "cloud_url": aurl, "mip": int(a.get("mip", 0)), "roi": r, "label_encoding": a.get("label_encoding", "auto")}
    if not any(v.kind == "data" for v in ds.versions):
        ds.versions.append(DatasetVersion(dataset_id=dataset_id, kind="data", version="roi-v1", note="cloud ROI registration"))
    want = make_blocks(nz, ny, nx)
    have = {b.block_id for b in ds.blocks}
    if have != {b["block_id"] for b in want}:
        for b in list(ds.blocks):
            s.delete(b)
        ds.blocks = [Block(dataset_id=dataset_id, **b) for b in want]
    for b in ds.blocks:
        b.source_volume = f"{normalize_url(url)}#mip{mip}"
        if not b.region:
            b.region = ds.brain_region
    s.flush()
    log.info("registered cloud ROI %s: %s mip%s %s -> %sx%sx%s", dataset_id, url, mip, d["roi_xyz"], nz, ny, nx)
    return ds


# ----------------------------------------------------------------------------- materialise (crawl job)


class CrawlRunner:
    """Download an ROI into data_root as a plain image stack, so it becomes an ordinary local dataset."""

    def __init__(self, job_id: int):
        self.job_id = job_id

    def event(self, s: Session, job: CrawlJob, msg: str, level: str = "info", data: dict | None = None) -> None:
        s.add(CrawlEvent(job_id=job.id, level=level, message=msg[:512], data_json=data or {}))
        s.commit()

    def run(self) -> dict:
        t0 = time.perf_counter()
        with session_scope() as s:
            job = s.get(CrawlJob, self.job_id)
            p = job.params_json
            roi = p["roi"]
            out = Path(job.out_dir)
            em_dir = out / "em"
            em_dir.mkdir(parents=True, exist_ok=True)
            job.status, job.started_at = "running", _now()
            s.commit()
            self.event(s, job, f"crawl started: {job.url} mip{p['mip']} roi {roi}", data={"params": p})
            try:
                reader = CloudVolumeReader(job.url, roi, mip=int(p["mip"]), cache_dir=settings.cache_dir, align=p.get("align", True))
                d = reader.describe()
                nz = reader.shape[0]
                job.n_sections, job.n_voxels = nz, d["n_voxels"]
                s.commit()
                self.event(s, job, f"volume ready: {d['shape_zyx']} voxels, {d['extent_um']} µm, chunk {d['chunk_xyz']}, encoding {d['encoding']}", data=d)
                from PIL import Image

                written = reused = 0
                for z in range(nz):
                    s.refresh(job)
                    if job.cancel_requested:
                        job.status = "cancelled"
                        break
                    f = em_dir / f"z{z + d['roi_xyz'][4]:05d}.png"
                    if f.is_file() and p.get("resume", True):
                        reused += 1
                    else:
                        arr = reader.read_slice(z)
                        tmp = f.with_name(f.name + ".part")
                        with open(tmp, "wb") as fh:  # PIL infers the format from the name, so say it explicitly
                            Image.fromarray(arr).save(fh, format="PNG")
                        tmp.replace(f)
                        written += 1
                    job.n_done = z + 1
                    job.n_bytes = sum(x.stat().st_size for x in em_dir.glob("z*.png"))
                    if (z + 1) % 10 == 0 or z == nz - 1:
                        s.commit()
                        self.event(s, job, f"{z + 1}/{nz} sections ({written} new, {reused} reused, {job.n_bytes / 1e6:.1f} MB)", data={"z": z, "wire": reader.describe()["wire"]})
                wire = reader.describe()["wire"]
                reader.close()
                (out / "dataset.json").write_text(json.dumps({
                    "dataset_id": job.dataset_id, "name": job.dataset_id, "em": {"path": "em", "format": "image_stack"},
                    "z_offset": d["roi_xyz"][4], "voxel_size_nm": d["resolution_nm"],
                    "extra": {"cloud_url": job.url, "mip": p["mip"], "roi_xyz": d["roi_xyz"], "encoding": d["encoding"], "lossy": d["lossy"], "materialised_from": "emqc crawl"},
                }, indent=1, ensure_ascii=False))
                if job.status != "cancelled":
                    job.status = "done"
                job.finished_at = _now()
                job.wire_json = wire
                s.commit()
                self.event(s, job, f"crawl {job.status}: {written} written, {reused} reused, {job.n_bytes / 1e6:.1f} MB on disk, {wire['chunks_fetched']} chunks fetched in {wire['seconds']:.1f}s, total {time.perf_counter() - t0:.1f}s",
                           level="warn" if job.status == "cancelled" else "info")
                return {"status": job.status, "n_sections": job.n_done, "bytes": job.n_bytes, "out_dir": str(out), "wire": wire}
            except Exception as e:
                s.rollback()
                job = s.get(CrawlJob, self.job_id)
                job.status, job.error, job.finished_at = "error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}", _now()
                s.commit()
                self.event(s, job, f"crawl failed: {type(e).__name__}: {e}"[:500], level="error")
                raise


def create_crawl_job(dataset_id: str, url: str, roi, mip: int = 0, out_dir: str | None = None, align: bool = True, resume: bool = True, precheck: dict | None = None) -> int:
    with session_scope() as s:
        out = Path(out_dir or (settings.data_root / "project_terminal" / "cloud" / "datasets" / "datasets" / dataset_id))
        job = CrawlJob(dataset_id=dataset_id, url=normalize_url(url), out_dir=str(out),
                       params_json={"roi": roi_dict(roi), "mip": int(mip), "align": align, "resume": resume, **({"precheck": precheck} if precheck else {})})
        s.add(job)
        s.flush()
        return job.id


def start_crawl_async(dataset_id: str, url: str, roi, mip: int = 0, out_dir: str | None = None, **kw) -> int:
    job_id = create_crawl_job(dataset_id, url, roi, mip, out_dir, **kw)

    def _target():
        try:
            CrawlRunner(job_id).run()
        except Exception:
            log.exception("crawl job %s failed", job_id)

    threading.Thread(target=_target, name=f"crawl-{job_id}", daemon=True).start()
    return job_id


def request_cancel_crawl(job_id: int) -> bool:
    with session_scope() as s:
        j = s.get(CrawlJob, job_id)
        if j is None or j.status not in ("queued", "running"):
            return False
        j.cancel_requested = True
        s.add(CrawlEvent(job_id=j.id, level="warn", message="cancel requested; stopping after the current section"))
        return True
