"""Training-side delivery: export the QC-passing part of a dataset as local shards, as a resumable background job.

Layout (one directory per dataset export):
  <out>/<dataset>/<block_id>/z00019-00026.npy      shard: (n, H, W) sections of the block's tile, n <= shard_z
  <out>/<dataset>/<block_id>/index.json            shards of this block + excluded sections and why
  <out>/<dataset>/assets/<asset_type>/<name>/...   matching asset files (e.g. GT) for the exported z, copied untouched
  <out>/<dataset>/export_manifest.json             everything: provenance (QC run), shards, exclusions, how to load

Resume: a shard whose file already exists with the expected size is reused, not rewritten. Re-running the same
export after a new QC run therefore only rewrites what changed.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.base import session_scope
from emqc.db.models import Block, Dataset, DatasetAsset, ExportEvent, ExportJob, QCBlock, QCRun, QCSlice
from emqc.registry.fs import parse_root
from emqc.registry.readers import IMAGE_EXTS, _Z_RE

from .runner import open_dataset_volume

log = __import__("logging").getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def contiguous_runs(zs: list[int], min_run: int) -> list[tuple[int, int]]:
    """Contiguous [z0, z1) runs of at least min_run sections."""
    out, start, prev = [], None, None
    for z in sorted(zs):
        if start is None:
            start = prev = z
        elif z == prev + 1:
            prev = z
        else:
            if prev - start + 1 >= min_run:
                out.append((start, prev + 1))
            start = prev = z
    if start is not None and prev - start + 1 >= min_run:
        out.append((start, prev + 1))
    return out


def split_shards(runs: list[tuple[int, int]], shard_z: int) -> list[tuple[int, int]]:
    """Cut long runs into shards of at most shard_z sections (0 = no limit)."""
    if not shard_z or shard_z <= 0:
        return runs
    out = []
    for z0, z1 in runs:
        for a in range(z0, z1, shard_z):
            out.append((a, min(a + shard_z, z1)))
    return out


def default_params(**kw) -> dict:
    p = {"fmt": "npy", "min_run": 1, "shard_z": 16, "min_quality": None, "with_assets": [], "block_ids": None, "resume": True}
    p.update({k: v for k, v in kw.items() if v is not None})
    return p


class ExportRunner:
    def __init__(self, job_id: int):
        self.job_id = job_id

    def event(self, s: Session, job: ExportJob, message: str, level: str = "info", block_id: str | None = None, data: dict | None = None) -> None:
        s.add(ExportEvent(job_id=job.id, level=level, block_id=block_id, message=message[:512], data_json=data or {}))
        s.commit()

    def run(self) -> dict:
        t_start = time.perf_counter()
        with session_scope() as s:
            job = s.get(ExportJob, self.job_id)
            ds = s.get(Dataset, job.dataset_id)
            p = job.params_json
            rid = job.run_id or ds.latest_run_id
            if not rid:
                job.status, job.error, job.finished_at = "error", f"{ds.dataset_id} has no QC run yet", _now()
                s.commit()
                raise RuntimeError(job.error)
            job.run_id = rid
            run = s.get(QCRun, rid)
            out_root = Path(job.out_dir) / ds.dataset_id
            out_root.mkdir(parents=True, exist_ok=True)
            blocks = list(s.scalars(select(Block).where(Block.dataset_id == ds.dataset_id).order_by(Block.z_start, Block.y_start, Block.x_start)))
            if p.get("block_ids"):
                blocks = [b for b in blocks if b.block_id in p["block_ids"]]
            qcb = {q.block_id: q for q in s.scalars(select(QCBlock).where(QCBlock.run_id == rid))}
            by_block: dict[str, dict[int, QCSlice]] = {}
            for x in s.scalars(select(QCSlice).where(QCSlice.run_id == rid)):
                by_block.setdefault(x.block_id, {})[x.z] = x
            job.status, job.started_at, job.n_blocks, job.n_blocks_done = "running", _now(), len(blocks), 0
            s.commit()
            self.event(s, job, f"export started: {len(blocks)} block(s) from QC run #{rid}, min_run {p['min_run']}, shard_z {p['shard_z']}, fmt {p['fmt']}", data={"params": p, "run_id": rid})
            manifest = {
                "dataset_id": ds.dataset_id, "name": ds.name, "source": ds.root_path, "run_id": rid, "pipeline_version": run.pipeline_version, "dataset_grade": ds.latest_grade,
                "shape": {"z": ds.size_z, "y": ds.size_y, "x": ds.size_x}, "dtype": ds.dtype, "voxel_size_nm": [ds.voxel_size_x_nm, ds.voxel_size_y_nm, ds.voxel_size_z_nm],
                "z_offset": (ds.metadata_json or {}).get("z_offset", 0), "species": ds.species, "brain_region": ds.brain_region, "params": p, "job_id": job.id,
                "generated_at": _now().isoformat(timespec="seconds"), "status": "running", "blocks": [], "shards": [], "excluded": {}, "assets": {},
                "how_to_load": "np.load(shard.file, mmap_mode='r') -> (n, H, W) = sections z_start..z_end-1 of tile bbox [x0, y0, x1, y1]; absolute section index = z + z_offset. See emqc/loader.py.",
            }
            itemsize = np.dtype(ds.dtype).itemsize
            reader = None
            try:
                cancelled = False
                for b in blocks:
                    s.refresh(job)
                    if job.cancel_requested:
                        cancelled = True
                        break
                    rows = by_block.get(b.block_id, {})
                    ok = [z for z, x in rows.items() if x.passed and (p.get("min_quality") is None or (x.quality_score or 0) >= p["min_quality"])]
                    excluded = [{"z": z, "status": x.status, "severity": x.max_severity, "failure_types": x.failure_types} for z, x in sorted(rows.items()) if z not in ok]
                    shards = split_shards(contiguous_runs(ok, int(p["min_run"])), int(p["shard_z"]))
                    q = qcb.get(b.block_id)
                    bdir = out_root / b.block_id
                    H, W = b.y_end - b.y_start, b.x_end - b.x_start
                    block_entry = {"block_id": b.block_id, "bbox": [b.x_start, b.y_start, b.x_end, b.y_end], "z_start": b.z_start, "z_end": b.z_end, "grade": q.grade if q else None, "retention_rate": q.retention_rate if q else None, "split": b.split, "n_passed": len(ok), "n_excluded": len(excluded), "shards": [], "excluded": excluded}
                    t_b = time.perf_counter()
                    n_new = n_reused = 0
                    for z0, z1 in shards:
                        bdir.mkdir(parents=True, exist_ok=True)
                        name = f"z{z0:05d}-{z1 - 1:05d}"
                        if p["fmt"] == "npy":
                            f = bdir / f"{name}.npy"
                            expected = (z1 - z0) * H * W * itemsize
                            if p.get("resume", True) and f.is_file() and f.stat().st_size >= expected and f.stat().st_size <= expected + 256:
                                n_reused += 1
                            else:
                                reader = reader or open_dataset_volume(ds)
                                if hasattr(reader, "prefetch_range"):
                                    reader.prefetch_range(z0, z1)
                                stack = np.stack([reader.read_slice(z)[b.y_start : b.y_end, b.x_start : b.x_end] for z in range(z0, z1)])
                                tmp = f.with_name(f.name + ".part")
                                with open(tmp, "wb") as fh:  # np.save would append .npy to a bare path
                                    np.save(fh, stack)
                                tmp.replace(f)
                                n_new += 1
                            size = f.stat().st_size
                            rel = str(f.relative_to(out_root))
                        else:
                            from PIL import Image

                            d = bdir / name
                            d.mkdir(exist_ok=True)
                            reader = reader or open_dataset_volume(ds)
                            size = 0
                            for z in range(z0, z1):
                                fp = d / f"z{z:05d}.png"
                                if not (p.get("resume", True) and fp.is_file()):
                                    Image.fromarray(reader.read_slice(z)[b.y_start : b.y_end, b.x_start : b.x_end]).save(fp)
                                size += fp.stat().st_size
                            n_new += 1
                            rel = str(d.relative_to(out_root))
                        sh = {"block_id": b.block_id, "z_start": z0, "z_end": z1, "n": z1 - z0, "bbox": [b.x_start, b.y_start, b.x_end, b.y_end], "file": rel, "bytes": size, "grade": q.grade if q else None}
                        block_entry["shards"].append(sh)
                        manifest["shards"].append(sh)
                        job.n_shards += 1
                        job.n_sections += z1 - z0
                        job.n_bytes += size
                    job.n_shards_reused += n_reused
                    (bdir if shards else out_root).mkdir(parents=True, exist_ok=True)
                    if shards:
                        (bdir / "index.json").write_text(json.dumps(block_entry, indent=1, ensure_ascii=False))
                    manifest["blocks"].append({k: v for k, v in block_entry.items() if k not in ("shards", "excluded")})
                    manifest["excluded"][b.block_id] = excluded
                    job.n_blocks_done += 1
                    s.commit()
                    (out_root / "export_manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
                    self.event(s, job, f"{b.block_id}: {len(shards)} shard(s), {len(ok)} sections kept, {len(excluded)} excluded, {n_reused} reused, {time.perf_counter() - t_b:.1f} s", block_id=b.block_id,
                               data={"n_shards": len(shards), "n_kept": len(ok), "n_excluded": len(excluded), "n_reused": n_reused, "wall_s": round(time.perf_counter() - t_b, 2), "grade": q.grade if q else None})
                if not cancelled and p.get("with_assets"):
                    manifest["assets"] = self._copy_assets(s, job, ds, out_root, manifest, set(p["with_assets"]))
                    (out_root / "export_manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
                if reader:
                    reader.close()
                manifest["status"] = "cancelled" if cancelled else "done"
                manifest["n_shards"], manifest["n_sections"], manifest["bytes"], manifest["n_shards_reused"] = job.n_shards, job.n_sections, job.n_bytes, job.n_shards_reused
                (out_root / "export_manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
                job.status, job.finished_at = ("cancelled" if cancelled else "done"), _now()
                s.commit()
                self.event(s, job, f"export {job.status}: {job.n_sections} sections in {job.n_shards} shards ({job.n_bytes / 1e6:.1f} MB, {job.n_shards_reused} reused), {job.n_asset_files} asset files, {time.perf_counter() - t_start:.1f} s",
                           level="warn" if cancelled else "info", data={"wall_s": round(time.perf_counter() - t_start, 1), "out_dir": str(out_root)})
                return manifest
            except Exception as e:
                s.rollback()
                job = s.get(ExportJob, self.job_id)
                job.status, job.error, job.finished_at = "error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}", _now()
                s.commit()
                self.event(s, job, f"export failed: {type(e).__name__}: {e}"[:500], level="error")
                raise

    def _copy_assets(self, s: Session, job: ExportJob, ds: Dataset, out_root: Path, manifest: dict, wanted: set[str]) -> dict:
        report = {}
        fs, base = parse_root(ds.root_path, cache_dir=settings.cache_dir, timeout=settings.ssh_timeout, key_file=settings.ssh_key_file)
        zs_needed = sorted({z for sh in manifest["shards"] for z in range(sh["z_start"], sh["z_end"])})
        assets = list(s.scalars(select(DatasetAsset).where(DatasetAsset.dataset_id == ds.dataset_id)))
        for a in assets:
            key = f"{a.asset_type}:{a.path}"
            if not (a.asset_type in wanted or key in wanted or a.path in wanted) or not a.format.startswith("image_stack"):
                continue
            adir = fs.join(base, a.path)
            if not fs.is_dir(adir):
                continue
            files = {}
            for e in fs.listdir(adir):
                if e.is_dir or Path(e.name).suffix.lower() not in IMAGE_EXTS:
                    continue
                m = _Z_RE.search(Path(e.name).stem)
                if m:
                    files[int(m.group(1))] = e.name
            zmin = min(files) if files else 0
            dest = out_root / "assets" / a.asset_type / Path(a.path).name
            dest.mkdir(parents=True, exist_ok=True)
            paths = [fs.join(adir, files[z + zmin]) for z in zs_needed if (z + zmin) in files]
            fs.prefetch(paths)
            copied = reused = 0
            for pth in paths:
                target = dest / fs.basename(pth)
                if target.is_file():
                    reused += 1
                    continue
                target.write_bytes(fs.read_bytes(pth))
                copied += 1
            job.n_asset_files += copied + reused
            s.commit()
            report[key] = {"dir": str(dest.relative_to(out_root)), "copied": copied, "reused": reused, "z_offset_in_files": zmin, "note": "whole sections copied as-is (not cropped to tiles, label encoding untouched)"}
            self.event(s, job, f"assets {key}: {copied} copied, {reused} reused -> {dest.relative_to(out_root)}", data=report[key])
        return report


# ----------------------------------------------------------------------------- job control

_jobs: dict[int, threading.Thread] = {}


def create_export_job(dataset_id: str, out_dir: str | None = None, run_id: int | None = None, **params) -> int:
    with session_scope() as s:
        ds = s.get(Dataset, dataset_id)
        if ds is None:
            raise KeyError(dataset_id)
        job = ExportJob(dataset_id=dataset_id, run_id=run_id or ds.latest_run_id, params_json=default_params(**params), out_dir=str(out_dir or settings.export_dir))
        s.add(job)
        s.flush()
        return job.id


def start_export_async(dataset_id: str, out_dir: str | None = None, run_id: int | None = None, **params) -> int:
    job_id = create_export_job(dataset_id, out_dir, run_id, **params)

    def _target():
        try:
            ExportRunner(job_id).run()
        except Exception:
            log.exception("export job %s failed", job_id)

    t = threading.Thread(target=_target, name=f"export-{job_id}", daemon=True)
    _jobs[job_id] = t
    t.start()
    return job_id


def export_passed(dataset_id: str, out_dir: str | Path, run_id: int | None = None, **params) -> dict:
    """Synchronous convenience wrapper used by the CLI and tests."""
    job_id = create_export_job(dataset_id, str(out_dir), run_id, **params)
    m = ExportRunner(job_id).run()
    m["job_id"] = job_id
    return m


def request_cancel_export(job_id: int) -> bool:
    with session_scope() as s:
        job = s.get(ExportJob, job_id)
        if job is None or job.status not in ("queued", "running"):
            return False
        job.cancel_requested = True
        s.add(ExportEvent(job_id=job.id, level="warn", message="cancel requested; stopping after the current block"))
        return True
