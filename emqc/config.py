from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """All knobs are environment variables prefixed EMQC_ (or a .env at project root)."""

    model_config = SettingsConfigDict(env_prefix="EMQC_", env_file=str(PROJECT_ROOT / ".env"), extra="ignore")

    # Root that contains project_terminal/<project>/datasets/datasets/<dataset_id>/ .
    # In production this is the Tailscale mounted directory.
    data_root: Path = PROJECT_ROOT / "data_root"
    # Glob (relative to each root) that yields one directory per dataset.
    dataset_glob: str = "project_terminal/*/datasets/datasets/*"
    # Additional roots on other machines, semicolon separated, e.g. "sftp://<用户>@<服务器地址><数据根目录>".
    # Slices are read on demand over SSH (read-ahead + local byte cache); nothing is copied up front.
    remote_roots: str = ""
    cache_dir: Path = PROJECT_ROOT / "var" / "cache"  # raw bytes of remote files (no eviction in v0.1)
    manifest_dir: Path = PROJECT_ROOT / "var" / "manifests"  # <dataset dir name>.json overrides for read-only / remote datasets
    export_dir: Path = PROJECT_ROOT / "var" / "exports"  # training shards written by export jobs
    ssh_timeout: float = 10.0
    ssh_key_file: str | None = None  # default: agent + ~/.ssh keys

    db_url: str = "mysql+pymysql://em_qc:CHANGE_ME@127.0.0.1:3306/em_qc?charset=utf8mb4"  # 用 .env 覆盖，不要用这个默认值
    db_echo: bool = False

    preview_dir: Path = PROJECT_ROOT / "var" / "previews"
    preview_max_px: int = 256  # thumbnails only; full images are never re-saved

    pipeline_version: str = "qc-v0.1"
    block_size_z: int = 64  # a block = a contiguous serial-section sequence of this many slices ...
    block_size_xy: int = 0  # ... over an XY tile of this many pixels (0 = the whole plane)
    # Datasets whose EM volume has >= this many voxels are "large" (full volume -> inference,
    # sampled blocks -> training). Smaller ones are "small" (whole dataset -> training).
    large_dataset_voxels: int = 2_000_000_000
    # Large datasets: how their training *sample* of blocks is drawn (small datasets train on all blocks).
    train_sample_policy: str = "stratified"  # stratified | best | random_usable   (grade D never qualifies)
    train_sample_blocks: int = 8  # total blocks marked train_sample per large dataset
    train_sample_strata: str = "A:0.5,B:0.3,C:0.2"  # share of the sample per grade (stratified policy)
    train_sample_seed: int = 42  # reproducible random draw within each stratum
    # --- patch factory (requirement 3): holdout partition + declared preprocessing / augmentation per block
    partition_ratios: str = "0.8,0.1,0.1"  # train,val,test over training-eligible A/B/C blocks; by block, stable across runs
    partition_seed: int = 42
    patch_preprocessing: str = '{"normalize": "per_patch_zscore", "clip_percentiles": [0.5, 99.5], "dtype": "float32"}'
    patch_augmentation: str = '{"flip_x": true, "flip_y": true, "rot90_xy": true, "flip_z": false, "intensity_jitter": 0.1, "elastic": false}'

    # shared deployments: the data root there points at the lab's real data, so deleting files must be off
    allow_delete_files: bool = True
    deployment_name: str = ""  # shown in the UI topbar so people know which instance they are looking at

    # slice annotation (VAST-style viewer): directory whose sub-directories hold em.npy (+ seg.npy) in (x, y, z) order
    annotate_root: Path | None = None
    # where the viewer keeps its working copies (seg_edit.npy, edits/) — never inside the data directory
    annotate_workdir: Path = PROJECT_ROOT / "var" / "annotate"
    annotate_extra_roots: str = ""  # more block roots, semicolon separated (e.g. where SAM pre-labels are written)
    sam_blocks_dir: Path = PROJECT_ROOT / "var" / "sam_blocks"  # blocks produced by scripts/sam_label.py (em + SAM seg)

    api_host: str = "127.0.0.1"
    api_port: int = 8765

    @property
    def all_roots(self) -> list[str]:
        roots = [str(self.data_root)]
        roots += [r.strip() for r in self.remote_roots.split(";") if r.strip()]
        return roots

    @field_validator("data_root", "preview_dir", "cache_dir", "manifest_dir", "export_dir", mode="after")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        # relative paths in .env are relative to the project root, never to the current working directory
        return v if v.is_absolute() else (PROJECT_ROOT / v).resolve()


settings = Settings()
