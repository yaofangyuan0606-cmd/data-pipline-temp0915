"""ORM rows -> JSON-able dicts (shared by the REST API and the dashboard templates)."""
from __future__ import annotations

from pathlib import Path

from emqc.config import settings
from emqc.db.models import AgentTrace, Block, CrawlEvent, CrawlJob, Dataset, DatasetAsset, DatasetVersion, ETLMetric, ExportEvent, ExportJob, LabelQC, Patch, PatchSet, QCBlock, QCFinding, QCRun, QCRunEvent, QCSlice, StreamSession


def preview_url(path: str | None) -> str | None:
    if not path:
        return None
    try:
        rel = Path(path).resolve().relative_to(Path(settings.preview_dir).resolve())
    except ValueError:
        return None
    return f"/previews/{rel.as_posix()}"


def _dt(v):
    return v.isoformat(timespec="seconds") if v else None


def dataset_to_dict(ds: Dataset, with_children: bool = False) -> dict:
    d = {
        "dataset_id": ds.dataset_id,
        "name": ds.name,
        "project": ds.project,
        "root_path": ds.root_path,
        "em": {"path": ds.em_path, "format": ds.em_format, "axes": ds.em_axes, "dtype": ds.dtype},
        "shape": {"z": ds.size_z, "y": ds.size_y, "x": ds.size_x},
        "n_voxels": ds.n_voxels,
        "size_class": ds.size_class,
        "usage": ds.usage,
        "metadata": {
            "species": ds.species,
            "brain_region": ds.brain_region,
            "voxel_size_nm": [ds.voxel_size_x_nm, ds.voxel_size_y_nm, ds.voxel_size_z_nm] if ds.voxel_size_x_nm is not None else None,
            "staining": ds.staining,
            "imaging_modality": ds.imaging_modality,
            "acquisition_batch": ds.acquisition_batch,
            "z_offset": (ds.metadata_json or {}).get("z_offset", 0),
            "extra": ds.metadata_json or {},
        },
        "data_version": ds.data_version,
        "source": (ds.metadata_json or {}).get("source", "file"),
        "host": (ds.metadata_json or {}).get("host"),
        "status": ds.status,
        "latest_run_id": ds.latest_run_id,
        "latest_quality_score": ds.latest_quality_score,
        "latest_retention_rate": ds.latest_retention_rate,
        "latest_grade": ds.latest_grade,
        "n_blocks": len(ds.blocks),
        "created_at": _dt(ds.created_at),
        "updated_at": _dt(ds.updated_at),
    }
    if with_children:
        d["assets"] = [asset_to_dict(a) for a in ds.assets]
        d["versions"] = [version_to_dict(v) for v in ds.versions]
        d["blocks"] = [block_to_dict(b) for b in sorted(ds.blocks, key=lambda b: b.z_start)]
    return d


def asset_to_dict(a: DatasetAsset) -> dict:
    return {
        "id": a.id,
        "dataset_id": a.dataset_id,
        "asset_type": a.asset_type,
        "path": a.path,
        "format": a.format,
        "version": a.version,
        "algo_version": a.algo_version,
        "model_version": a.model_version,
        "experiment_id": a.experiment_id,
        "exists": a.exists,
        "size_bytes": a.size_bytes,
        "extra": a.extra_json or {},
    }


def version_to_dict(v: DatasetVersion) -> dict:
    return {"id": v.id, "dataset_id": v.dataset_id, "kind": v.kind, "version": v.version, "note": v.note, "created_at": _dt(v.created_at)}


def block_to_dict(b: Block) -> dict:
    return {
        "id": b.id,
        "dataset_id": b.dataset_id,
        "block_id": b.block_id,
        "z_start": b.z_start,
        "z_end": b.z_end,
        "y_start": b.y_start,
        "y_end": b.y_end,
        "x_start": b.x_start,
        "x_end": b.x_end,
        "n_slices": b.n_slices,
        "split": b.split,
        "status": b.status,
        "latest_quality_score": b.latest_quality_score,
        "latest_severity": b.latest_severity,
        "latest_retention_rate": b.latest_retention_rate,
        "latest_grade": b.latest_grade,
        "is_tile": not (b.y_start == 0 and b.x_start == 0 and b.y_end >= 0 and "_y" not in b.block_id),
        # requirement 3: per-block lineage
        "partition": b.partition or "none",
        "difficulty": b.difficulty,
        "difficulty_components": b.difficulty_json or {},
        "label_version": b.label_version,
        "preprocessing": b.preprocessing_json or {},
        "augmentation": b.augmentation_json or {},
        "source_volume": b.source_volume,
        "region": b.region,
        "coordinate": {"z": [b.z_start, b.z_end], "y": [b.y_start, b.y_end], "x": [b.x_start, b.x_end]},
    }


def run_to_dict(r: QCRun) -> dict:
    return {
        "run_id": r.id,
        "dataset_id": r.dataset_id,
        "pipeline_version": r.pipeline_version,
        "status": r.status,
        "stage": r.stage,
        "n_blocks": r.n_blocks,
        "n_blocks_done": r.n_blocks_done,
        "progress": (r.n_blocks_done / r.n_blocks) if r.n_blocks else None,
        "quality_score": r.quality_score,
        "retention_rate": r.retention_rate,
        "grade_counts": r.grade_counts_json or {},
        "cancel_requested": bool(r.cancel_requested),
        "error": r.error,
        "config": r.config_json or {},
        "started_at": _dt(r.started_at),
        "finished_at": _dt(r.finished_at),
        "created_at": _dt(r.created_at),
    }


def qcslice_to_dict(s: QCSlice, with_stats: bool = False) -> dict:
    d = {
        "run_id": s.run_id,
        "dataset_id": s.dataset_id,
        "block_id": s.block_id,
        "z": s.z,
        "status": s.status,
        "quality_score": s.quality_score,
        "max_severity": s.max_severity,
        "passed": s.passed,
        "failure_types": s.failure_types or [],
        "scores": s.scores_json or {},
        "coordinate": s.coordinate_json or {},
        "preview_url": preview_url(s.preview_path),
        "notes": (s.stats_json or {}).get("_notes", {}),
    }
    if with_stats:
        d["stats"] = {k: v for k, v in (s.stats_json or {}).items() if k != "_notes"}
    return d


def qcblock_to_dict(b: QCBlock) -> dict:
    return {
        "run_id": b.run_id,
        "dataset_id": b.dataset_id,
        "block_id": b.block_id,
        "quality_score": b.quality_score,
        "max_severity": b.max_severity,
        "failure_types": b.failure_types or [],
        "n_slices": b.n_slices,
        "n_passed": b.n_passed,
        "n_missing": b.n_missing,
        "n_corrupt": b.n_corrupt,
        "retention_rate": b.retention_rate,
        "grade": b.grade,
        "longest_clean_run": b.longest_clean_run,
        "dominant_failure": b.dominant_failure,
        "scores": b.scores_json or {},
        "coordinate": b.coordinate_json or {},
        "preview_url": preview_url(b.preview_path),
        "duration_s": b.duration_s,
        "stage_durations": b.stage_durations_json or {},
    }


def finding_to_dict(f: QCFinding) -> dict:
    return {
        "id": f.id,
        "run_id": f.run_id,
        "dataset_id": f.dataset_id,
        "block_id": f.block_id,
        "level": f.level,
        "stage": f.stage,
        "check": f.check_name,
        "failure_type": f.failure_type,
        "severity": f.severity,
        "score": f.score,
        "z": f.z,
        "z_to": f.z_to,
        "coordinate": f.coordinate_json or {},
        "details": f.details_json or {},
        "preview_url": preview_url(f.preview_path),
        "created_at": _dt(f.created_at),
    }


def metric_to_dict(m: ETLMetric) -> dict:
    return {"run_id": m.run_id, "dataset_id": m.dataset_id, "block_id": m.block_id, "stage": m.stage, "name": m.metric_name, "value": m.metric_value, "extra": m.extra_json or {}}


def trace_to_dict(t: AgentTrace) -> dict:
    return {
        "id": t.id,
        "dataset_id": t.dataset_id,
        "run_id": t.run_id,
        "agent": t.agent,
        "step": t.step,
        "action": t.action,
        "status": t.status,
        "duration_ms": t.duration_ms,
        "algo_version": t.algo_version,
        "model_version": t.model_version,
        "experiment_id": t.experiment_id,
        "input": t.input_json or {},
        "output": t.output_json or {},
        "created_at": _dt(t.created_at),
    }


def event_to_dict(e: QCRunEvent) -> dict:
    return {"id": e.id, "run_id": e.run_id, "ts": _dt(e.ts), "level": e.level, "stage": e.stage, "block_id": e.block_id, "message": e.message, "data": e.data_json or {}}


def export_job_to_dict(j: ExportJob) -> dict:
    return {
        "job_id": j.id, "dataset_id": j.dataset_id, "run_id": j.run_id, "status": j.status, "params": j.params_json or {}, "out_dir": str(Path(j.out_dir) / j.dataset_id) if j.out_dir else None,
        "n_blocks": j.n_blocks, "n_blocks_done": j.n_blocks_done, "progress": (j.n_blocks_done / j.n_blocks) if j.n_blocks else None,
        "n_shards": j.n_shards, "n_shards_reused": j.n_shards_reused, "n_sections": j.n_sections, "n_bytes": j.n_bytes, "n_asset_files": j.n_asset_files,
        "cancel_requested": bool(j.cancel_requested), "error": j.error, "created_at": _dt(j.created_at), "started_at": _dt(j.started_at), "finished_at": _dt(j.finished_at),
    }


def export_event_to_dict(e: ExportEvent) -> dict:
    return {"id": e.id, "job_id": e.job_id, "ts": _dt(e.ts), "level": e.level, "block_id": e.block_id, "message": e.message, "data": e.data_json or {}}


def stream_to_dict(x: StreamSession) -> dict:
    return {
        "stream_id": x.id, "dataset_id": x.dataset_id, "run_id": x.run_id, "status": x.status, "client": x.client, "purpose": x.purpose, "params": x.params_json or {},
        "n_items": x.n_items, "cursor": x.cursor, "n_acked": x.n_acked, "n_failed_items": x.n_failed_items, "progress": (x.cursor / x.n_items) if x.n_items else None,
        "n_bytes": x.n_bytes, "est_bytes": x.est_bytes, "created_at": _dt(x.created_at), "updated_at": _dt(x.updated_at), "finished_at": _dt(x.finished_at),
    }


def label_qc_to_dict(r: LabelQC) -> dict:
    return {"z": r.z, "present": r.present, "shape_ok": r.shape_ok, "em_status": r.em_status, "n_ids": r.n_ids, "frac_background": r.frac_background,
            "frac_label_in_fill": r.frac_label_in_fill, "issues": r.issues or [], "passed": r.passed}


def patchset_to_dict(p: PatchSet) -> dict:
    return {
        "set_id": p.id, "dataset_id": p.dataset_id, "patch_type": p.patch_type, "status": p.status, "reason": p.reason, "params": p.params_json or {},
        "qc_run_id": p.qc_run_id, "label_asset_id": p.label_asset_id, "label_version": p.label_version, "source_volume": p.source_volume,
        "preprocessing": p.preprocessing_json or {}, "augmentation": p.augmentation_json or {}, "n_patches": p.n_patches, "counts": p.counts_json or {},
        "checks": p.checks_json or {}, "created_at": _dt(p.created_at),
    }


def patch_to_dict(x: Patch, label_asset_id: int | None = None) -> dict:
    q = f"z0={x.z0}&z1={x.z1}&y0={x.y0}&y1={x.y1}&x0={x.x0}&x1={x.x1}"
    d = {
        "patch_id": x.id, "set_id": x.set_id, "dataset_id": x.dataset_id, "block_id": x.block_id, "patch_type": x.patch_type,
        "bbox": {"z0": x.z0, "z1": x.z1, "y0": x.y0, "y1": x.y1, "x0": x.x0, "x1": x.x1}, "partition": x.partition, "difficulty": x.difficulty, "grade": x.grade,
        "region": x.region, "meta": x.meta_json or {}, "url_em": f"/api/v1/data/{x.dataset_id}/cutout?{q}&fmt=npy",
    }
    if label_asset_id:
        derive = "boundary" if x.patch_type == "membrane" else ("mask" if x.patch_type in ("synapse", "mitochondria") else "ids")
        d["url_label"] = f"/api/v1/data/{x.dataset_id}/labels/{label_asset_id}/cutout?{q}&derive={derive}"
    return d


def crawl_job_to_dict(j: CrawlJob) -> dict:
    p = j.params_json or {}
    return {
        "job_id": j.id, "dataset_id": j.dataset_id, "url": j.url, "status": j.status, "params": p, "roi": p.get("roi"), "mip": p.get("mip"),
        "out_dir": j.out_dir, "n_sections": j.n_sections, "n_done": j.n_done, "progress": (j.n_done / j.n_sections) if j.n_sections else None,
        "n_voxels": j.n_voxels, "n_bytes": j.n_bytes, "wire": j.wire_json or {}, "cancel_requested": bool(j.cancel_requested),
        "error": j.error, "created_at": _dt(j.created_at), "started_at": _dt(j.started_at), "finished_at": _dt(j.finished_at),
    }


def crawl_event_to_dict(e: CrawlEvent) -> dict:
    return {"id": e.id, "job_id": e.job_id, "ts": _dt(e.ts), "level": e.level, "message": e.message, "data": e.data_json or {}}
