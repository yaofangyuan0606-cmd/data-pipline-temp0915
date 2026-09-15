"""dataset.json parsing and layout inference, over any FS (local directory or sftp://).

A dataset directory (project_terminal/<project>/datasets/datasets/<dataset_id>/) may carry a
`dataset.json` describing where things are. Everything is optional; missing fields are inferred
from conventional sub-directory names. A local *override* manifest (settings.manifest_dir/<dir>.json)
takes precedence, so read-only or remote datasets can be described without writing into them.
See docs/INPUT_FIELDS.md for the full field list.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .fs import FS, LocalFS
from .readers import IMAGE_EXTS, detect_format

MANIFEST_NAMES = ("dataset.json", "manifest.json", "metadata.json")
EM_DIR_CANDIDATES = ("em", "raw", "image", "images", "em_image", "EM", "img", "volume")
_MIP_RE = re.compile(r"^(?:mip|s|scale)?(\d+)$", re.I)

# conventional sub-directory / file names -> asset type
ASSET_NAME_MAP = {
    "gt": "gt_segmentation", "ground_truth": "gt_segmentation", "groundtruth": "gt_segmentation", "seg_gt": "gt_segmentation",
    "labels": "gt_segmentation", "label": "gt_segmentation", "segmentation_gt": "gt_segmentation", "seg": "gt_segmentation",
    "pred": "model_prediction", "prediction": "model_prediction", "predictions": "model_prediction", "seg_pred": "model_prediction", "affinity": "model_prediction",
    "synapse": "synapse_prediction", "synapses": "synapse_prediction", "syn": "synapse_prediction",
    "mito": "mitochondria_prediction", "mitochondria": "mitochondria_prediction",
    "organelle": "organelle_prediction", "organelles": "organelle_prediction",
    "skeleton": "skeleton", "skeletons": "skeleton", "skel": "skeleton", "swc": "skeleton",
    "annotation": "gt_annotation", "annotations": "gt_annotation", "masks": "gt_annotation",
    "trace": "agent_trace", "traces": "agent_trace", "agent_trace": "agent_trace", "agent_traces": "agent_trace", "agent_logs": "agent_trace",
}


@dataclass
class AssetSpec:
    type: str
    path: str  # relative to dataset dir
    format: str = ""
    version: str = ""
    algo_version: str | None = None
    model_version: str | None = None
    experiment_id: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class DatasetManifest:
    dataset_id: str
    root: str  # path on `fs`
    fs: FS = field(default_factory=LocalFS, repr=False)
    project: str = ""
    name: str = ""
    species: str | None = None
    brain_region: str | None = None
    voxel_size_nm: tuple[float, float, float] | None = None  # (x, y, z)
    staining: str | None = None
    imaging_modality: str | None = None
    acquisition_batch: str | None = None
    size_class: str = "auto"  # auto | small | large
    z_offset: int | None = None
    em_path: str = ""
    em_format: str = "auto"
    em_axes: str = "zyx"
    em_fill_value: int | float | None = 0  # pixel value of no-data regions; null in dataset.json disables
    assets: list[AssetSpec] = field(default_factory=list)
    versions: dict = field(default_factory=dict)  # data / algo / model / experiment
    extra: dict = field(default_factory=dict)
    manifest_file: str | None = None
    inferred: list[str] = field(default_factory=list)  # which fields were guessed

    def full(self, rel: str) -> str:
        return self.root if rel in ("", ".") else self.fs.join(self.root, rel)


def _pick_mip_dir(fs: FS, path: str) -> str | None:
    """A directory of mip0/mip1/... sub-directories: choose the finest level that holds images."""
    levels = []
    for e in fs.listdir(path):
        m = _MIP_RE.match(e.name) if e.is_dir else None
        if m:
            levels.append((int(m.group(1)), e.name))
    for _, name in sorted(levels):
        if detect_format(fs, fs.join(path, name)) == "image_stack":
            return name
    return None


def _infer_em_path(fs: FS, root: str) -> tuple[str, str] | None:
    for name in EM_DIR_CANDIDATES:
        p = fs.join(root, name)
        if fs.exists(p):
            fmt = detect_format(fs, p)
            if fmt:
                return name, fmt
            if fs.is_dir(p):
                mip = _pick_mip_dir(fs, p)
                if mip:
                    return f"{name}/{mip}", "image_stack"
    entries = fs.listdir(root)
    for e in entries:  # npy files at root
        if not e.is_dir and e.name.endswith(".npy"):
            return e.name, "npy"
    if any(not e.is_dir and Path(e.name).suffix.lower() in IMAGE_EXTS for e in entries):
        return ".", "image_stack"
    if any(not e.is_dir and e.name == "info" for e in entries):
        return ".", "precomputed"
    return None


def _infer_assets(fs: FS, root: str, em_rel: str) -> list[AssetSpec]:
    out: list[AssetSpec] = []
    em_top = em_rel.split("/")[0]
    for e in fs.listdir(root):
        if e.name.startswith(".") or e.name == em_top or e.name in MANIFEST_NAMES:
            continue
        key = Path(e.name).stem.lower() if not e.is_dir else e.name.lower()
        t = ASSET_NAME_MAP.get(key)
        if t is None:
            continue
        p = fs.join(root, e.name)
        fmt = detect_format(fs, p) or (Path(e.name).suffix.lstrip(".").lower() if not e.is_dir else "dir")
        if e.is_dir and fmt == "dir":
            mip = _pick_mip_dir(fs, p)
            if mip:
                out.append(AssetSpec(type=t, path=f"{e.name}/{mip}", format="image_stack", extra={"mip_dir": True}))
                continue
        out.append(AssetSpec(type=t, path=e.name, format=fmt or ""))
    return out


def load_manifest(root: str | Path, project: str = "", fs: FS | None = None, override: dict | None = None) -> DatasetManifest:
    fs = fs or LocalFS()
    root = str(root)
    raw: dict = {}
    mf_name = None
    if override:
        raw, mf_name = dict(override), "override"
    else:
        for name in MANIFEST_NAMES:
            p = fs.join(root, name)
            if fs.is_file(p):
                raw = json.loads(fs.read_text(p))
                mf_name = name
                break
    m = DatasetManifest(dataset_id=str(raw.get("dataset_id") or fs.basename(root)), root=root, fs=fs, project=project, manifest_file=mf_name)
    m.name = raw.get("name") or m.dataset_id
    m.species = raw.get("species")
    m.brain_region = raw.get("brain_region")
    vs = raw.get("voxel_size_nm") or raw.get("voxel_size") or raw.get("resolution")
    if vs:
        m.voxel_size_nm = tuple(float(v) for v in vs)
    m.staining = raw.get("staining")
    m.imaging_modality = raw.get("imaging_modality") or raw.get("modality")
    m.acquisition_batch = raw.get("acquisition_batch") or raw.get("batch")
    m.size_class = raw.get("size_class", "auto") or "auto"
    m.z_offset = raw.get("z_offset")
    m.versions = dict(raw.get("versions") or {})
    m.extra = dict(raw.get("extra") or {})

    em = raw.get("em") or {}
    if isinstance(em, str):
        em = {"path": em}
    if em.get("path"):
        m.em_path = em["path"]
        m.em_format = em.get("format", "auto") or "auto"
        m.em_axes = em.get("axes", "zyx") or "zyx"
        m.em_fill_value = em.get("fill_value", 0)
        if m.em_format == "auto":
            m.em_format = detect_format(fs, m.full(m.em_path)) or "auto"
    else:
        found = _infer_em_path(fs, root)
        if found:
            m.em_path, m.em_format = found
            m.inferred.append("em")

    if raw.get("assets"):
        for a in raw["assets"]:
            m.assets.append(
                AssetSpec(
                    type=a.get("type", "other"), path=a.get("path", ""),
                    format=a.get("format", "") or (detect_format(fs, m.full(a.get("path", ""))) or ""),
                    version=a.get("version", "") or "", algo_version=a.get("algo_version"), model_version=a.get("model_version"), experiment_id=a.get("experiment_id"),
                    extra={k: v for k, v in a.items() if k not in {"type", "path", "format", "version", "algo_version", "model_version", "experiment_id"}},
                )
            )
    else:
        m.assets = _infer_assets(fs, root, m.em_path)
        if m.assets:
            m.inferred.append("assets")
    if m.voxel_size_nm is None and m.em_format == "precomputed":  # precomputed volumes carry their own resolution
        try:
            info = json.loads(fs.read_text(fs.join(m.full(m.em_path), "info")))
            res = info["scales"][0].get("resolution")
            if res:
                m.voxel_size_nm = tuple(float(v) for v in res)
                m.inferred.append("voxel_size_nm")
        except Exception:
            pass
    return m
