"""Isolated test environment: SQLite database, temp data root, small synthetic dataset."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="emqc-test-"))
os.environ["EMQC_DB_URL"] = f"sqlite:///{TMP / 'test.db'}"
os.environ["EMQC_DATA_ROOT"] = str(TMP / "data_root")
os.environ["EMQC_DATASET_GLOB"] = "project_terminal/*/datasets/datasets/*"
os.environ["EMQC_ANNOTATE_ROOT"] = str(TMP / "annotation")
os.environ["EMQC_ANNOTATE_WORKDIR"] = str(TMP / "annotation-work")
os.environ["EMQC_ANNOTATE_EXTRA_ROOTS"] = ""
os.environ["EMQC_SAM_BLOCKS_DIR"] = str(TMP / "sam-blocks")
os.environ["EMQC_PREVIEW_DIR"] = str(TMP / "previews")
os.environ["EMQC_BLOCK_SIZE_Z"] = "20"
os.environ["EMQC_BLOCK_SIZE_XY"] = "0"
os.environ["EMQC_REMOTE_ROOTS"] = ""
os.environ["EMQC_CACHE_DIR"] = str(TMP / "cache")
os.environ["EMQC_MANIFEST_DIR"] = str(TMP / "manifests")
os.environ["EMQC_AUTH_DISABLED"] = "1"   # 绝大多数测试不关心登录；test_auth.py 自己再打开
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def data_root() -> Path:
    from scripts.make_sample_dataset import make_synthetic

    base = Path(os.environ["EMQC_DATA_ROOT"]) / "project_terminal" / "test" / "datasets" / "datasets"
    make_synthetic(base / "synthetic_small", n_z=40, size=128, seed=1)
    return Path(os.environ["EMQC_DATA_ROOT"])


@pytest.fixture(scope="session")
def registered(data_root):
    from emqc.db import init_db, session_scope
    from emqc.registry.scanner import scan

    init_db()
    with session_scope() as s:
        res = scan(s)
    assert not res.errors, res.errors
    return "synthetic_small"


@pytest.fixture(scope="session")
def qc_run(registered):
    from emqc.qc.runner import run_sync

    return run_sync(registered)
