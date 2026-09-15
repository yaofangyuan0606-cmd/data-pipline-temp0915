"""Discover datasets under the data root and register them in MySQL."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import shutil

from sqlalchemy import delete, select  # noqa: F401
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.models import AgentTrace, Block, Dataset, DatasetAsset, DatasetVersion, ETLMetric, QCBlock, QCFinding, QCRun, QCRunEvent, QCSlice, ServeLog

from .fs import FS, LocalFS, fs_glob, is_remote, parse_root
from .manifest import DatasetManifest, load_manifest
from .readers import open_volume

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    scanned: int = 0
    registered: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"scanned": self.scanned, "registered": self.registered, "updated": self.updated, "errors": self.errors}


def open_root(root: str | Path) -> tuple[FS, str]:
    return parse_root(root, cache_dir=settings.cache_dir, timeout=settings.ssh_timeout, key_file=settings.ssh_key_file)


def discover_dataset_dirs(data_root: Path | str | None = None, pattern: str | None = None) -> list[tuple[str, FS, str, str]]:
    """Yield (project, fs, dataset_dir, root) for every directory matching settings.dataset_glob under each root.

    Roots are the local data_root plus every entry of settings.remote_roots (sftp://...). Passing data_root scans only that root."""
    roots = [str(data_root)] if data_root else settings.all_roots
    pattern = pattern or settings.dataset_glob
    out = []
    for root in roots:
        try:
            fs, base = open_root(root)
        except Exception as e:  # unreachable remote host: skip, report
            log.warning("cannot open root %s: %s", root, e)
            raise
        for d in fs_glob(fs, base, pattern):
            comps = [c for c in d.replace("\\", "/").split("/") if c]
            project = comps[comps.index("project_terminal") + 1] if "project_terminal" in comps and comps.index("project_terminal") + 1 < len(comps) else ""
            out.append((project, fs, d, root))
    return out


def load_override(dir_name: str) -> dict | None:
    p = Path(settings.manifest_dir) / f"{dir_name}.json"
    if p.is_file():
        import json

        return json.loads(p.read_text())
    return None


def block_id_for(z0: int, z1: int, y0: int | None = None, x0: int | None = None) -> str:
    bid = f"z{z0:05d}-{z1 - 1:05d}"
    if y0 is not None and x0 is not None:
        bid += f"_y{y0:05d}_x{x0:05d}"
    return bid


def make_blocks(n_z: int, size_y: int, size_x: int, block_size_z: int | None = None, block_size_xy: int | None = None) -> list[dict]:
    """Blocks = z ranges of `block_size_z` sections x XY tiles of `block_size_xy` pixels (0/None = whole plane).

    Tile ids carry the tile origin (z00000-00099_y01024_x00000); a single whole-plane tile keeps the short id.
    """
    bz = block_size_z or settings.block_size_z
    bxy = settings.block_size_xy if block_size_xy is None else block_size_xy
    if not bxy or bxy <= 0 or (bxy >= size_y and bxy >= size_x):
        tiles = [(0, size_y, 0, size_x)]
        tiled = False
    else:
        tiles = [(y0, min(y0 + bxy, size_y), x0, min(x0 + bxy, size_x)) for y0 in range(0, size_y, bxy) for x0 in range(0, size_x, bxy)]
        tiled = True
    blocks = []
    for z0 in range(0, n_z, bz):
        z1 = min(z0 + bz, n_z)
        for y0, y1, x0, x1 in tiles:
            blocks.append(
                dict(block_id=block_id_for(z0, z1, y0, x0) if tiled else block_id_for(z0, z1), z_start=z0, z_end=z1, y_start=y0, y_end=y1, x_start=x0, x_end=x1, n_slices=z1 - z0)
            )
    return blocks


def classify_size(n_voxels: int, requested: str = "auto") -> str:
    if requested in ("small", "large"):
        return requested
    return "large" if n_voxels >= settings.large_dataset_voxels else "small"


def _size(fs: FS, path: str) -> int | None:
    """Bytes of a file, or of the files directly inside a directory (shallow, so remote scans stay quick)."""
    try:
        if fs.is_file(path):
            return fs.stat_size(path)
        if fs.is_dir(path):
            return sum((e.size or 0) for e in fs.listdir(path) if not e.is_dir)
    except OSError:
        return None
    return None


def register_manifest(session: Session, m: DatasetManifest) -> tuple[Dataset, bool]:
    """Insert or update a dataset (+assets, versions, blocks) from a manifest. Returns (row, created)."""
    if not m.em_path:
        raise FileNotFoundError(f"{m.fs.url(m.root)}: no EM volume found (set em.path in dataset.json)")
    reader = open_volume(m.full(m.em_path), m.em_format, m.em_axes, m.z_offset, fs=m.fs)
    info = reader.info
    n_z, n_y, n_x = info.shape

    ds = session.get(Dataset, m.dataset_id)
    created = ds is None
    if created:
        ds = Dataset(dataset_id=m.dataset_id, root_path=m.fs.url(m.root))
        session.add(ds)
    ds.name = m.name or m.dataset_id
    ds.project = m.project
    ds.root_path = m.fs.url(m.root)
    ds.em_path = m.em_path
    ds.em_format = info.fmt
    ds.em_axes = m.em_axes
    ds.dtype = info.dtype
    ds.size_x, ds.size_y, ds.size_z = n_x, n_y, n_z
    ds.n_voxels = info.n_voxels
    ds.size_class = classify_size(info.n_voxels, m.size_class)
    ds.usage = "train" if ds.size_class == "small" else "train+inference"
    ds.species = m.species
    ds.brain_region = m.brain_region
    if m.voxel_size_nm:
        ds.voxel_size_x_nm, ds.voxel_size_y_nm, ds.voxel_size_z_nm = m.voxel_size_nm
    ds.staining = m.staining
    ds.imaging_modality = m.imaging_modality
    ds.acquisition_batch = m.acquisition_batch
    ds.data_version = str(m.versions.get("data") or ds.data_version or "v1")
    ds.metadata_json = {
        **m.extra,
        "z_offset": info.z_offset,
        "fill_value": m.em_fill_value,
        "source": m.fs.scheme,
        "host": getattr(m.fs, "host", None),
        "n_files": info.n_files,
        "n_missing_files": info.n_missing,
        "manifest_file": m.manifest_file,
        "inferred_fields": m.inferred,
        "reader_extra": info.extra,
        "size_class_requested": m.size_class,
    }
    if ds.status in (None, "", "error"):
        ds.status = "registered"

    # --- assets (EM itself is asset #0)
    existing = {(a.asset_type, a.path): a for a in ds.assets}
    wanted: list[tuple[str, str, dict]] = [("em_image", m.em_path, dict(format=info.fmt, version=ds.data_version))]
    for a in m.assets:
        wanted.append(
            (
                a.type,
                a.path,
                dict(format=a.format, version=a.version, algo_version=a.algo_version, model_version=a.model_version, experiment_id=a.experiment_id, extra_json=a.extra),
            )
        )
    for atype, apath, kw in wanted:
        row = existing.get((atype, apath))
        if row is None:
            row = DatasetAsset(dataset_id=ds.dataset_id, asset_type=atype, path=apath)
            ds.assets.append(row)
        for k, v in kw.items():
            if k == "extra_json":  # merge: keep platform-written keys (validation, label_conflict) across rescans
                row.extra_json = {**(row.extra_json or {}), **(v or {})}
                continue
            setattr(row, k, v if v is not None else getattr(row, k))
        full = m.full(apath)
        row.exists = m.fs.exists(full)
        row.size_bytes = _size(m.fs, full) if row.exists else None

    # --- versions
    have = {(v.kind, v.version) for v in ds.versions}
    for kind, ver in {"data": ds.data_version, **{k: v for k, v in m.versions.items() if k != "data"}}.items():
        if ver and (kind, str(ver)) not in have:
            ds.versions.append(DatasetVersion(dataset_id=ds.dataset_id, kind=kind, version=str(ver), note="from dataset.json" if m.manifest_file else "default"))

    # --- blocks (only (re)built when geometry changed)
    want_blocks = make_blocks(n_z, n_y, n_x)
    have_blocks = {b.block_id: b for b in ds.blocks}
    if set(have_blocks) != {b["block_id"] for b in want_blocks}:
        for b in list(ds.blocks):
            session.delete(b)
        ds.blocks = [Block(dataset_id=ds.dataset_id, **b) for b in want_blocks]
    em_url = m.fs.url(m.full(m.em_path)) if m.em_path not in ("", ".") else m.fs.url(m.root)
    for b in ds.blocks:  # lineage fields required per block (requirement 3): source volume + region
        b.source_volume = em_url
        if not b.region:
            b.region = ds.brain_region
    session.flush()
    reader.close()
    return ds, created


def scan(session: Session, data_root: Path | str | None = None) -> ScanResult:
    res = ScanResult()
    try:
        dirs = discover_dataset_dirs(data_root)
    except Exception as e:
        res.errors["<roots>"] = f"{type(e).__name__}: {e}"
        return res
    for project, fs, d, root in dirs:
        res.scanned += 1
        try:
            m = load_manifest(d, project=project, fs=fs, override=load_override(fs.basename(d)))
            ds, created = register_manifest(session, m)
            (res.registered if created else res.updated).append(ds.dataset_id)
        except Exception as e:  # keep scanning the rest
            log.exception("failed to register %s", fs.url(d))
            res.errors[fs.url(d)] = f"{type(e).__name__}: {e}"
    session.commit()
    return res


def list_datasets(session: Session) -> list[Dataset]:
    return list(session.scalars(select(Dataset).order_by(Dataset.dataset_id)))


def delete_dataset(session: Session, dataset_id: str, remove_files: bool = False) -> dict:
    """Remove a dataset and everything derived from it: QC runs (slices, blocks, findings, metrics, events),
    traces, serve log, preview thumbnails; with remove_files=True also the data directory itself,
    which is only allowed when it lies under settings.data_root."""
    ds = session.get(Dataset, dataset_id)
    if ds is None:
        raise KeyError(dataset_id)
    root = Path(ds.root_path)
    run_ids = list(session.scalars(select(QCRun.id).where(QCRun.dataset_id == dataset_id)))
    counts = {"runs": len(run_ids)}
    for model in (QCSlice, QCBlock, QCFinding, ETLMetric, QCRunEvent):
        if run_ids:
            counts[model.__tablename__] = session.execute(delete(model).where(model.run_id.in_(run_ids))).rowcount
    session.execute(delete(QCRun).where(QCRun.dataset_id == dataset_id))
    counts["agent_traces"] = session.execute(delete(AgentTrace).where(AgentTrace.dataset_id == dataset_id)).rowcount
    counts["serve_log"] = session.execute(delete(ServeLog).where(ServeLog.dataset_id == dataset_id)).rowcount
    session.delete(ds)  # assets / versions / blocks cascade through the ORM relationships
    session.flush()
    prev = Path(settings.preview_dir) / dataset_id
    if prev.exists():
        shutil.rmtree(prev)
        counts["previews_removed"] = True
    if remove_files:
        if not settings.allow_delete_files:
            raise PermissionError("this deployment has file deletion disabled (EMQC_ALLOW_DELETE_FILES=0); only the database rows were removed")
        if is_remote(ds.root_path):
            raise PermissionError(f"refusing to delete files on a remote source: {ds.root_path}")
        data_root = Path(settings.data_root).resolve()
        target = root.resolve()
        if data_root not in target.parents:
            raise PermissionError(f"refusing to delete {target}: not under data_root {data_root}")
        if target.exists():
            shutil.rmtree(target)
            counts["files_removed"] = str(target)
    session.commit()
    return counts
