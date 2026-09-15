"""FS abstraction: readers, inference and globbing work on any FS (here an in-memory fake standing in for SFTP)."""
import io

import numpy as np
import pytest
from PIL import Image

from emqc.registry.fs import FS, Entry, fs_glob, parse_root
from emqc.registry.manifest import load_manifest
from emqc.registry.readers import CorruptSliceError, ImageStackReader, MissingSliceError, detect_format, open_volume


class FakeFS(FS):
    scheme = "fake"

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.reads = 0

    def join(self, *parts):
        return "/".join(p.strip("/") for p in parts if p)

    def _children(self, path):
        pre = path.strip("/") + "/"
        return {f[len(pre):].split("/")[0] for f in self.files if f.startswith(pre)}

    def listdir(self, path):
        names = self._children(path)
        if not names and path.strip("/") not in {f.rsplit("/", 1)[0] for f in self.files}:
            raise FileNotFoundError(path)
        return sorted((Entry(n, self.is_dir(self.join(path, n)), self.files.get(self.join(path, n)) and len(self.files[self.join(path, n)])) for n in names), key=lambda e: e.name)

    def is_dir(self, path):
        return bool(self._children(path))

    def is_file(self, path):
        return path.strip("/") in self.files

    def stat_size(self, path):
        return len(self.files[path.strip("/")]) if self.is_file(path) else None

    def read_bytes(self, path):
        self.reads += 1
        return self.files[path.strip("/")]

    def url(self, path):
        return "fake://" + path.strip("/")


def png(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def fake():
    rng = np.random.default_rng(0)
    files = {}
    base = "srv/project_terminal/lab/datasets/datasets/remote_ds"
    for z in range(6):
        if z == 3:
            continue  # missing
        files[f"{base}/em/mip1/im{z:04d}.png"] = b"not a png" if z == 4 else png((rng.random((16, 16)) * 255).astype(np.uint8))
        files[f"{base}/em/mip2/im{z:04d}.png"] = png((rng.random((8, 8)) * 255).astype(np.uint8))
        files[f"{base}/seg/mip1/seg{z:04d}.png"] = png(np.zeros((16, 16), np.uint8))
    files[f"{base}/delete/axon/axon_s000.png"] = png(np.zeros((32, 32), np.uint8))
    files["srv/project_terminal/lab/datasets/datasets/other/em/x0000.png"] = png(np.zeros((4, 4), np.uint8))
    return FakeFS(files)


def test_glob_and_inference_on_remote_layout(fake):
    dirs = fs_glob(fake, "srv", "project_terminal/*/datasets/datasets/*")
    assert dirs == ["srv/project_terminal/lab/datasets/datasets/other", "srv/project_terminal/lab/datasets/datasets/remote_ds"]
    # a root that already points inside the pattern (…/project_terminal or …/project_terminal/<project>) globs the remainder
    assert fs_glob(fake, "srv/project_terminal", "project_terminal/*/datasets/datasets/*") == dirs
    assert fs_glob(fake, "srv/project_terminal/lab", "project_terminal/*/datasets/datasets/*") == dirs
    m = load_manifest(dirs[1], project="lab", fs=fake)
    assert m.em_path == "em/mip1" and m.em_format == "image_stack"  # finest mip level under em/
    assert "em" in m.inferred
    types = {a.type: a.path for a in m.assets}
    assert types["gt_segmentation"] == "seg/mip1"
    assert m.fs is fake and m.full("em/mip1").endswith("remote_ds/em/mip1")
    # an override manifest wins over inference
    m2 = load_manifest(dirs[1], fs=fake, override={"dataset_id": "renamed", "species": "mouse", "em": {"path": "em/mip2"}})
    assert m2.dataset_id == "renamed" and m2.species == "mouse" and m2.em_path == "em/mip2" and m2.manifest_file == "override"


def test_image_stack_reader_over_fake_fs(fake):
    d = "srv/project_terminal/lab/datasets/datasets/remote_ds/em/mip1"
    assert detect_format(fake, d) == "image_stack"
    r = ImageStackReader(d, fs=fake)
    assert r.shape == (6, 16, 16) and r.info.n_missing == 1 and r.info.path.startswith("fake://")
    assert r.read_slice(0).shape == (16, 16)
    with pytest.raises(MissingSliceError):
        r.read_slice(3)
    with pytest.raises(CorruptSliceError):
        r.read_slice(4)
    cut = r.read_cutout(0, 6, 0, 4, 0, 4)
    assert cut.shape == (6, 4, 4) and cut[3].max() == 0
    assert open_volume(d, fs=fake).info.fmt == "image_stack"


def test_parse_root_variants():
    fs, path = parse_root("/tmp/x")
    assert fs.scheme == "file" and path.endswith("x")
    fs2, path2 = parse_root("sftp://alice@example-host/srv/project_terminal", cache_dir="/tmp/emqc-cache-test")
    assert fs2.scheme == "sftp" and fs2.user == "alice" and fs2.host == "example-host" and fs2.port == 22 and path2 == "/srv/project_terminal"
    assert fs2.url(path2 + "/a") == "sftp://alice@example-host/srv/project_terminal/a"
    with pytest.raises(ValueError):
        parse_root("sftp://nohost")
