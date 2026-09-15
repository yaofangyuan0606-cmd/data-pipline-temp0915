"""Run the QC pipeline for a dataset (block by block) and persist results to MySQL."""
from __future__ import annotations

import json
import logging
import resource
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.base import session_scope
from emqc.db.models import AgentTrace, Block, Dataset, ETLMetric, QCBlock, QCFinding, QCRun, QCRunEvent, QCSlice
from emqc.registry.readers import VolumeReader, open_volume

from emqc.patches.partition import assign_partitions, difficulty_of

from .base import SERIAL_CHECKS, SLICE_CHECKS, BlockContext, BlockInfo, DatasetInfo, QCConfig, Severity, build_checks
from .sampling import select_train_blocks
from .stages import BlockResult, grade_from, stage_aggregate, stage_checks, stage_ingest_group

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _union_bbox(boxes) -> list | None:
    """Union of [x0, y0, x1, y1] boxes (None entries skipped); None when there is none."""
    bs = [b for b in boxes if b and len(b) == 4]
    if not bs:
        return None
    return [int(min(b[0] for b in bs)), int(min(b[1] for b in bs)), int(max(b[2] for b in bs)), int(max(b[3] for b in bs))]


def dataset_info(ds: Dataset) -> DatasetInfo:
    vs = None
    if ds.voxel_size_x_nm is not None:
        vs = (ds.voxel_size_x_nm, ds.voxel_size_y_nm, ds.voxel_size_z_nm)
    return DatasetInfo(
        dataset_id=ds.dataset_id,
        shape=(ds.size_z, ds.size_y, ds.size_x),
        dtype=ds.dtype,
        z_offset=int((ds.metadata_json or {}).get("z_offset", 0) or 0),
        voxel_size_nm=vs,
        size_class=ds.size_class,
        fill_value=(ds.metadata_json or {}).get("fill_value", 0),
    )


def block_info(b: Block) -> BlockInfo:
    return BlockInfo(block_id=b.block_id, z_start=b.z_start, z_end=b.z_end, y_start=b.y_start, y_end=b.y_end, x_start=b.x_start, x_end=b.x_end)


def open_dataset_volume(ds: Dataset) -> VolumeReader:
    """root_path is a local directory, an sftp:// URL, or a cloud precomputed URL; slices are read on demand."""
    from emqc.registry.cloud import CloudVolumeReader, is_cloud
    from emqc.registry.scanner import open_root

    meta = ds.metadata_json or {}
    if ds.em_format == "precomputed_cloud" or (is_cloud(ds.root_path) and meta.get("roi")):
        return CloudVolumeReader(ds.root_path, meta["roi"], mip=int(meta.get("mip", 0)), cache_dir=settings.cache_dir, align=False)
    z_off = (ds.metadata_json or {}).get("z_offset")
    fs, base = open_root(ds.root_path)
    return open_volume(fs.join(base, ds.em_path) if ds.em_path not in ("", ".") else base, ds.em_format, ds.em_axes, z_off, fs=fs)



def _rusage() -> tuple[float, float]:
    """(cpu seconds of this process, peak RSS in MB)."""
    ru = resource.getrusage(resource.RUSAGE_SELF)
    rss_mb = ru.ru_maxrss / (1e6 if sys.platform == "darwin" else 1e3)
    return ru.ru_utime + ru.ru_stime, rss_mb


class QCRunner:
    def __init__(self, config: QCConfig | None = None, preview_root: Path | None = None):
        self.config = config or QCConfig(thumb_px=settings.preview_max_px)
        if not self.config.tile_xy:
            self.config.tile_xy = settings.block_size_xy
        self.preview_root = Path(preview_root or settings.preview_dir)
        self.slice_checks = build_checks(self.config, SLICE_CHECKS)
        self.serial_checks = build_checks(self.config, SERIAL_CHECKS)

    # ------------------------------------------------------------------ in-memory part (stages 1-4)
    def process_block(self, reader: VolumeReader, ds: DatasetInfo, blk: BlockInfo) -> tuple[BlockContext, BlockResult]:
        return self.process_group(reader, ds, [blk])[0]

    def process_group(self, reader: VolumeReader, ds: DatasetInfo, blocks: list[BlockInfo], on_stage=None) -> list[tuple[BlockContext, BlockResult]]:
        """Stages 1-4 for all XY tiles of one z range: sections are decoded once and cropped per tile.

        on_stage(stage_name, block_id | None) is called at every stage transition (live progress for the UI)."""
        notify = on_stage or (lambda stage, bid: None)
        ctxs = [BlockContext(ds, b, self.config) for b in blocks]
        notify("ingest", None)
        stage_ingest_group(ctxs, reader)
        out = []
        for ctx in ctxs:
            notify("slice_qc", ctx.block.block_id)
            stage_checks(ctx, self.slice_checks, "slice_qc")
            notify("serial_qc", ctx.block.block_id)
            stage_checks(ctx, self.serial_checks, "serial_qc")
            notify("aggregate", ctx.block.block_id)
            res = stage_aggregate(ctx, [*self.slice_checks, *self.serial_checks], self.preview_root)
            out.append((ctx, res))
        return out

    # ------------------------------------------------------------------ live log
    @staticmethod
    def event(s: Session, run: QCRun, message: str, level: str = "info", stage: str | None = None, block_id: str | None = None, data: dict | None = None) -> None:
        s.add(QCRunEvent(run_id=run.id, level=level, stage=stage, block_id=block_id, message=message[:512], data_json=data or {}))
        s.commit()


    # ------------------------------------------------------------------ stage 5: persist
    def persist_block(self, s: Session, run: QCRun, ctx: BlockContext, res: BlockResult, block_row: Block) -> None:
        t0 = time.perf_counter()
        ds_id, bid = ctx.ds.dataset_id, ctx.block.block_id
        for model in (QCSlice, QCBlock, QCFinding, ETLMetric):
            s.execute(delete(model).where(model.run_id == run.id, model.dataset_id == ds_id, model.block_id == bid))
        s.add_all(
            [
                QCSlice(
                    run_id=run.id, dataset_id=ds_id, block_id=bid, z=r.z, status=r.status,
                    scores_json=r.scores, stats_json=r.stats, failure_types=r.failure_types,
                    max_severity=r.max_severity.label, quality_score=r.quality_score, passed=r.passed,
                    coordinate_json={"z": r.z, "z_abs": r.z + ctx.ds.z_offset, "bbox": _union_bbox(f.coordinate.get("bbox") for f in r.findings)},
                    preview_path=r.preview_path,
                )
                for r in ctx.slices
            ]
        )
        s.add_all(
            [
                QCFinding(
                    run_id=run.id, dataset_id=ds_id, block_id=bid, level=f.level, stage=f.stage, check_name=f.check,
                    failure_type=f.failure_type, severity=f.severity.label, score=f.score, z=f.z, z_to=f.z_to,
                    coordinate_json=f.coordinate, details_json=f.details, preview_path=f.preview_path,
                )
                for r in ctx.slices
                for f in r.findings
            ]
        )
        s.add_all(
            [
                QCFinding(
                    run_id=run.id, dataset_id=ds_id, block_id=bid, level="block", stage=f.stage, check_name=f.check,
                    failure_type=f.failure_type, severity=f.severity.label, score=f.score, z=None, z_to=None,
                    coordinate_json=f.coordinate, details_json=f.details, preview_path=f.preview_path,
                )
                for f in res.block_findings
            ]
        )
        s.flush()
        ctx.stage_durations["persist"] = time.perf_counter() - t0
        total = sum(ctx.stage_durations.values())
        s.add(
            QCBlock(
                run_id=run.id, dataset_id=ds_id, block_id=bid, scores_json=res.scores, failure_types=res.failure_types,
                max_severity=res.max_severity.label, quality_score=res.quality_score, n_slices=res.n_slices, n_passed=res.n_passed,
                n_missing=res.n_missing, n_corrupt=res.n_corrupt, retention_rate=res.retention_rate, coordinate_json=res.coordinate,
                preview_path=res.preview_path, duration_s=total, stage_durations_json=ctx.stage_durations,
                grade=res.grade, longest_clean_run=res.longest_clean_run, dominant_failure=res.dominant_failure,
            )
        )
        metrics = {
            "n_slices_input": res.n_slices, "n_slices_ok": res.n_ok, "n_missing": res.n_missing, "n_corrupt": res.n_corrupt,
            "n_flagged": res.n_flagged, "n_passed": res.n_passed, "retention_rate": res.retention_rate,
            "bytes_read": res.bytes_read, "duration_s": total, "slices_per_s": (res.n_slices / total) if total > 0 else None,
            "longest_clean_run": res.longest_clean_run, "n_critical": res.n_critical,
        }
        if getattr(ctx, "resource", None):
            metrics.update(ctx.resource)  # cpu_time_s (share of the group's decode + this tile), peak_rss_mb
        s.add_all([ETLMetric(run_id=run.id, dataset_id=ds_id, block_id=bid, stage="aggregate", metric_name=k, metric_value=v) for k, v in metrics.items()])
        s.add_all([ETLMetric(run_id=run.id, dataset_id=ds_id, block_id=bid, stage=st, metric_name="stage_duration_s", metric_value=v) for st, v in ctx.stage_durations.items()])
        s.add(ETLMetric(run_id=run.id, dataset_id=ds_id, block_id=bid, stage="aggregate", metric_name="findings_by_type", metric_value=float(sum(res.findings_by_type.values())), extra_json=res.findings_by_type))
        block_row.status = "done"
        block_row.latest_quality_score = res.quality_score
        block_row.latest_severity = res.max_severity.label
        block_row.latest_retention_rate = res.retention_rate
        block_row.latest_grade = res.grade
        n_med = sum(1 for r in ctx.slices if r.max_severity >= Severity.MEDIUM)
        block_row.difficulty, block_row.difficulty_json = difficulty_of(res.quality_score, res.retention_rate, n_med, res.n_slices, res.grade)
        run.n_blocks_done += 1
        s.commit()

    # ------------------------------------------------------------------ dataset-level wrap-up
    def finalize(self, s: Session, run: QCRun, ds: Dataset, started: float) -> None:
        blocks = list(s.scalars(select(QCBlock).where(QCBlock.run_id == run.id)))
        n_slices = sum(b.n_slices for b in blocks)
        n_passed = sum(b.n_passed for b in blocks)
        wq = [(b.quality_score, b.n_slices) for b in blocks if b.quality_score is not None]
        run.quality_score = (sum(q * n for q, n in wq) / sum(n for _, n in wq)) if wq else None
        run.retention_rate = (n_passed / n_slices) if n_slices else None
        n_find = s.scalar(select(func.count()).select_from(QCFinding).where(QCFinding.run_id == run.id)) or 0
        by_type = dict(s.execute(select(QCFinding.failure_type, func.count()).where(QCFinding.run_id == run.id).group_by(QCFinding.failure_type)).all())
        grade_counts: dict[str, int] = {}
        for b in blocks:
            grade_counts[b.grade or "?"] = grade_counts.get(b.grade or "?", 0) + 1
        run.grade_counts_json = grade_counts
        # train / inference split per block.
        # Decided from each block's LATEST KNOWN grade (Block.latest_grade, refreshed by persist_block), not from
        # this run's blocks alone: a run over a subset of blocks must not demote blocks it never looked at, and a
        # block that has never been QC'd stays "unassigned" rather than being silently called inference.
        rows = {b.block_id: b for b in s.scalars(select(Block).where(Block.dataset_id == ds.dataset_id))}
        graded = [b for b in rows.values() if b.latest_grade]
        partial = len(blocks) < len(rows)
        if ds.size_class == "small":
            for b in graded:
                b.split = "train"
            n_train = len(graded)
        else:
            # user decision: training sample stratified by grade (see qc/sampling.py); D never qualifies
            cands = [SimpleNamespace(block_id=b.block_id, grade=b.latest_grade, retention_rate=b.latest_retention_rate, quality_score=b.latest_quality_score) for b in graded]
            sampling = select_train_blocks(cands, policy=settings.train_sample_policy, n=settings.train_sample_blocks, strata=settings.train_sample_strata, seed=settings.train_sample_seed)
            chosen = sampling.block_ids
            for b in graded:
                b.split = "train_sample" if b.block_id in chosen else "inference"
            n_train = len(chosen)
            run.config_json = {**(run.config_json or {}), "train_sampling": {**sampling.as_dict(), "decided_over": "latest known grade of every block", "n_graded": len(graded), "n_ungraded": len(rows) - len(graded), "partial_run": partial}}
        if partial:
            self.event(s, run, f"partial run ({len(blocks)}/{len(rows)} blocks): split recomputed from the latest known grade of all {len(graded)} graded block(s); blocks never QC'd stay unassigned", level="warn")
        # holdout partition (train / val / test) - by block, stable across runs, D excluded; see patches/partition.py
        part = assign_partitions(list(rows.values()), ratios=settings.partition_ratios, seed=settings.partition_seed)
        pre, aug = json.loads(settings.patch_preprocessing), json.loads(settings.patch_augmentation)
        gt = next((a for a in ds.assets if a.asset_type == "gt_segmentation" and (a.extra_json or {}).get("validation", {}).get("usable_for_patches")), None)
        em_url = None
        try:
            from emqc.registry.scanner import open_root

            fs, base = open_root(ds.root_path)
            em_url = fs.url(fs.join(base, ds.em_path) if ds.em_path not in ("", ".") else base)
        except Exception:
            pass
        for b in rows.values():
            if not b.source_volume and em_url:
                b.source_volume = em_url
            if not b.region:
                b.region = ds.brain_region
            if b.preprocessing_json is None:
                b.preprocessing_json = pre
            if b.augmentation_json is None:
                b.augmentation_json = aug
            if gt is not None and not b.label_version:
                b.label_version = gt.version or gt.path
        ds.metadata_json = {**(ds.metadata_json or {}), "partition": part.as_dict()}
        run.config_json = {**(run.config_json or {}), "partition": part.as_dict()}
        if part.changed:
            self.event(s, run, f"holdout partition: {part.counts} ({len(part.changed)} block(s) newly assigned; seed {part.seed}, ratios {[round(r, 2) for r in part.ratios]})" + (f"; {part.note}" if part.note else ""))
        dur = time.perf_counter() - started
        ds_metrics = {
            "n_blocks": len(blocks), "n_slices_input": n_slices, "n_slices_passed": n_passed, "retention_rate": run.retention_rate,
            "quality_score": run.quality_score, "n_findings": n_find, "n_missing": sum(b.n_missing for b in blocks),
            "n_corrupt": sum(b.n_corrupt for b in blocks), "duration_s": dur, "n_train_blocks": n_train,
            "n_train_slices": sum(rows[b.block_id].n_slices for b in blocks if rows[b.block_id].split in ("train", "train_sample")),
            "bytes_read": sum((m.metric_value or 0) for m in s.scalars(select(ETLMetric).where(ETLMetric.run_id == run.id, ETLMetric.metric_name == "bytes_read", ETLMetric.block_id.is_not(None)))),
        }
        s.add_all([ETLMetric(run_id=run.id, dataset_id=ds.dataset_id, block_id=None, stage="dataset", metric_name=k, metric_value=v) for k, v in ds_metrics.items()])
        s.add(ETLMetric(run_id=run.id, dataset_id=ds.dataset_id, block_id=None, stage="dataset", metric_name="findings_by_type", metric_value=float(n_find), extra_json=by_type))
        run.status, run.finished_at, run.stage = "done", _now(), "done"
        ds.status = "qc_done"
        ds.latest_run_id = run.id
        ds.latest_quality_score = run.quality_score
        ds.latest_retention_rate = run.retention_rate
        n_crit = int(sum(m.metric_value or 0 for m in s.scalars(select(ETLMetric).where(ETLMetric.run_id == run.id, ETLMetric.metric_name == "n_critical", ETLMetric.block_id.is_not(None)))))
        ds_grade = grade_from(run.retention_rate, n_crit, False)  # same rule as a block, over all sections
        if ds_grade == "A" and any((b.grade == "D") for b in blocks):
            ds_grade = "B"  # a dataset with an unusable block is never "directly usable"
        ds.latest_grade = ds_grade
        s.add(
            AgentTrace(
                dataset_id=ds.dataset_id, run_id=run.id, agent="emqc.qc_runner", step="run_qc", action="qc_pipeline",
                input_json={"pipeline_version": run.pipeline_version, "config": run.config_json, "n_blocks": len(blocks)},
                output_json={**ds_metrics, "findings_by_type": by_type}, status="ok", duration_ms=dur * 1000.0, algo_version=settings.pipeline_version,
            )
        )
        s.commit()

    # ------------------------------------------------------------------ entry point
    def run_dataset(self, dataset_id: str, block_ids: list[str] | None = None, run_id: int | None = None) -> int:
        started = time.perf_counter()
        with session_scope() as s:
            ds = s.get(Dataset, dataset_id)
            if ds is None:
                raise KeyError(f"unknown dataset {dataset_id}")
            q = select(Block).where(Block.dataset_id == dataset_id).order_by(Block.z_start)
            if block_ids:
                q = q.where(Block.block_id.in_(block_ids))
            blocks = list(s.scalars(q))
            run = s.get(QCRun, run_id) if run_id else None
            if run is None:
                run = QCRun(dataset_id=dataset_id, pipeline_version=settings.pipeline_version, config_json=self.config.as_dict())
                s.add(run)
            prev_status = ds.status
            run.status, run.started_at, run.n_blocks, run.n_blocks_done, run.stage = "running", _now(), len(blocks), 0, "ingest"
            run.config_json = {**self.config.as_dict(), "block_ids": block_ids}
            ds.status = "qc_running"
            s.commit()
            run_id = run.id
            info = dataset_info(ds)
            self.event(s, run, f"run started: {len(blocks)} block(s), pipeline {run.pipeline_version}, checks {len(self.slice_checks)} slice + {len(self.serial_checks)} serial",
                       data={"n_blocks": len(blocks), "shape": list(info.shape), "checks": [c.name for c in [*self.slice_checks, *self.serial_checks]]})
            try:
                reader = open_dataset_volume(ds)
                groups: dict[tuple[int, int], list[Block]] = {}
                for b in blocks:  # all XY tiles of one z range are processed together (sections decoded once)
                    groups.setdefault((b.z_start, b.z_end), []).append(b)
                cancelled = False
                for gi, ((z0, z1), grp) in enumerate(groups.items()):
                    s.refresh(run)
                    if run.cancel_requested:
                        cancelled = True
                        break
                    for b in grp:
                        b.status = "running"
                    s.commit()
                    zr = f"z{z0}-{z1 - 1}"
                    cpu0, _ = _rusage()
                    t_group = time.perf_counter()
                    last = {"t": t_group, "stage": None, "bid": None}

                    def on_stage(stage: str, bid: str | None, zr=zr, n=len(grp), z0=z0, z1=z1):
                        now = time.perf_counter()
                        cpu, rss = _rusage()
                        prev = dict(last)
                        last.update(t=now, stage=stage, bid=bid)
                        run.stage = (f"{stage} · {zr}" + (f" · {bid}" if bid else f" · {n} tile(s)"))[:120]
                        s.commit()
                        data = {"z_start": z0, "z_end": z1, "n_tiles": n, "rss_mb": round(rss, 1), "cpu_time_s": round(cpu - cpu0, 2)}
                        if prev["stage"]:
                            data["prev_stage"] = prev["stage"]
                            data["prev_duration_s"] = round(now - prev["t"], 3)
                        self.event(s, run, f"{stage} {zr}" + (f" {bid}" if bid else f" ({n} tiles, decode once)"), stage=stage, block_id=bid, data=data)

                    results = self.process_group(reader, info, [block_info(b) for b in grp], on_stage=on_stage)
                    for b, (ctx, res) in zip(grp, results):
                        on_stage("persist", b.block_id)
                        cpu, rss = _rusage()
                        ctx.resource = {"cpu_time_s": round((cpu - cpu0) / len(grp), 2), "peak_rss_mb": round(rss, 1)}
                        self.persist_block(s, run, ctx, res, b)
                        msg = f"{b.block_id}: grade {res.grade}, quality {res.quality_score or 0:.3f}, retention {100 * (res.retention_rate or 0):.0f}%, worst {res.max_severity.label}, {sum(res.findings_by_type.values())} findings"
                        data = {"grade": res.grade, "quality": res.quality_score, "retention": res.retention_rate, "n_findings": sum(res.findings_by_type.values()), "findings_by_type": res.findings_by_type,
                                "durations": {k: round(v, 3) for k, v in ctx.stage_durations.items()}, "z_start": z0, "z_end": z1, **ctx.resource}
                        self.event(s, run, msg, level="warn" if res.grade in ("C", "D") else "info", stage="done", block_id=b.block_id, data=data)
                        log.info("%s %s", dataset_id, msg)
                    self.event(s, run, f"{zr}: {len(grp)} tile(s) finished in {time.perf_counter() - t_group:.1f} s, cpu {(_rusage()[0] - cpu0):.1f} s", stage="group_done", data={"z_start": z0, "z_end": z1, "wall_s": round(time.perf_counter() - t_group, 2), "cpu_time_s": round(_rusage()[0] - cpu0, 2)})
                reader.close()
                if cancelled:
                    run.status, run.finished_at, run.stage = "cancelled", _now(), "cancelled"
                    for b in blocks:
                        if b.status == "running":
                            b.status = "pending"
                    ds.status = prev_status if prev_status not in ("qc_running", "error") else "registered"
                    self.event(s, run, "run cancelled by user", level="warn")
                    s.commit()
                else:
                    self.finalize(s, run, ds, started)
                    self.event(s, run, f"run finished: grade {ds.latest_grade}, quality {run.quality_score or 0:.3f}, retention {100 * (run.retention_rate or 0):.0f}%",
                               data={"grade": ds.latest_grade, "quality": run.quality_score, "retention": run.retention_rate, "grade_counts": run.grade_counts_json, "wall_s": round(time.perf_counter() - started, 2)})
            except Exception as e:
                s.rollback()  # the failed flush may have left the session unusable
                run = s.get(QCRun, run_id)
                ds = s.get(Dataset, dataset_id)
                run.status, run.error, run.finished_at, run.stage = "error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}", _now(), "error"
                ds.status = "error"
                s.commit()
                self.event(s, run, f"run failed: {type(e).__name__}: {e}"[:500], level="error")
                raise
        return run_id


# ----------------------------------------------------------------------------- background jobs (v0.1: threads)

_jobs: dict[int, threading.Thread] = {}


def start_run_async(dataset_id: str, block_ids: list[str] | None = None, config: QCConfig | None = None) -> int:
    """Create the run row synchronously (so the caller gets an id), execute in a background thread."""
    with session_scope() as s:
        if s.get(Dataset, dataset_id) is None:
            raise KeyError(dataset_id)
        run = QCRun(dataset_id=dataset_id, pipeline_version=settings.pipeline_version, status="queued", config_json={**((config or QCConfig()).as_dict()), "block_ids": block_ids})
        s.add(run)
        s.flush()
        run_id = run.id

    def _target():
        try:
            QCRunner(config).run_dataset(dataset_id, block_ids, run_id=run_id)
        except Exception:
            log.exception("qc run %s failed", run_id)

    t = threading.Thread(target=_target, name=f"qc-run-{run_id}", daemon=True)
    _jobs[run_id] = t
    t.start()
    return run_id


def run_sync(dataset_id: str, block_ids: list[str] | None = None, config: QCConfig | None = None) -> int:
    return QCRunner(config).run_dataset(dataset_id, block_ids)


def request_cancel(run_id: int) -> bool:
    """Ask a queued/running run to stop after the current block group. Returns False if it is not active."""
    with session_scope() as s:
        run = s.get(QCRun, run_id)
        if run is None or run.status not in ("queued", "running"):
            return False
        run.cancel_requested = True
        s.add(QCRunEvent(run_id=run.id, level="warn", message="cancel requested; stopping after the current block group"))
        return True
