"""Volume readers.

Every reader exposes the EM volume as a z-indexed stack (0-based, dataset-relative z) and never
loads more than what is asked for: one slice, or one cutout. Nothing is re-saved to disk.
Readers work through the FS abstraction (emqc.registry.fs), so a dataset can sit on a local disk
or on a remote machine reached over SSH (sftp://user@host/path) without any other code changing.

Supported on-disk formats
  image_stack  - a directory of 2-D images (png / jpg / tif / bmp), one file per section.
                 The z index is the last integer in the file name (em_z_2048.png -> 2048, 0007.tif -> 7).
                 Gaps in the numbering are *missing* slices; unreadable files are *corrupt* slices.
  npy          - a single .npy array, opened with mmap (remote files are cached locally first).
  precomputed  - Neuroglancer precomputed volume (info + chunk files), raw encoding, optional .gz chunks.
"""
from __future__ import annotations

import gzip
import io
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .fs import FS, LocalFS, is_remote, parse_root

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
_Z_RE = re.compile(r"(\d+)(?!.*\d)")  # last integer group in a name


class SliceReadError(Exception):
    """Base class for per-slice problems that the QC pipeline records instead of crashing on."""


class MissingSliceError(SliceReadError):
    pass


class CorruptSliceError(SliceReadError):
    pass


class SourceUnavailableError(Exception):
    """The data source itself failed (network, permissions): abort the run instead of marking slices corrupt."""


@dataclass
class VolumeInfo:
    shape: tuple[int, int, int]  # (z, y, x)
    dtype: str
    fmt: str
    path: str
    z_offset: int = 0  # absolute z of dataset-relative z=0 (e.g. 2048 for an H01 sub-stack)
    n_files: int = 0
    n_missing: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def n_voxels(self) -> int:
        z, y, x = self.shape
        return int(z) * int(y) * int(x)


class VolumeReader(ABC):
    info: VolumeInfo
    fs: FS

    @property
    def shape(self) -> tuple[int, int, int]:
        return self.info.shape

    @abstractmethod
    def read_slice(self, z: int) -> np.ndarray:
        """Return the 2-D section at dataset-relative z. Raises MissingSliceError / CorruptSliceError."""

    def slice_status(self, z: int) -> str:
        """ok | missing | corrupt (cheap check where possible, falls back to a read)."""
        try:
            self.read_slice(z)
            return "ok"
        except MissingSliceError:
            return "missing"
        except CorruptSliceError:
            return "corrupt"

    def read_cutout(self, z0: int, z1: int, y0: int, y1: int, x0: int, x1: int, fill: int = 0) -> np.ndarray:
        """Return array of shape (z1-z0, y1-y0, x1-x0). Missing / corrupt sections are filled with `fill`."""
        zs, ys, xs = self.shape
        if not (0 <= z0 < z1 <= zs and 0 <= y0 < y1 <= ys and 0 <= x0 < x1 <= xs):
            raise ValueError(f"cutout out of bounds: z[{z0},{z1}) y[{y0},{y1}) x[{x0},{x1}) for shape {self.shape}")
        out = np.full((z1 - z0, y1 - y0, x1 - x0), fill, dtype=self.info.dtype)
        for i, z in enumerate(range(z0, z1)):
            try:
                out[i] = self.read_slice(z)[y0:y1, x0:x1]
            except SliceReadError:
                pass
        return out

    def close(self) -> None:  # pragma: no cover - trivial
        pass


# ----------------------------------------------------------------------------- image decoding


def _to_gray2d(arr: np.ndarray) -> np.ndarray:
    if arr.ndim == 3:
        if arr.shape[-1] in (3, 4):
            arr = arr[..., 0] if np.array_equal(arr[..., 0], arr[..., 1]) else arr[..., :3].mean(axis=-1).astype(arr.dtype)
        else:
            arr = arr[0]
    if arr.ndim != 2:
        raise CorruptSliceError(f"unexpected image ndim={arr.ndim} shape={arr.shape}")
    return arr


def decode_image(data: bytes, name: str) -> np.ndarray:
    suffix = Path(name).suffix.lower()
    try:
        if suffix in (".tif", ".tiff"):
            import tifffile

            arr = tifffile.imread(io.BytesIO(data))
        else:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                im.load()
                arr = np.asarray(im)
    except Exception as e:  # PIL / tifffile raise many different types
        raise CorruptSliceError(f"{name}: {type(e).__name__}: {e}") from e
    return _to_gray2d(arr)


def _read(fs: FS, path: str) -> bytes:
    try:
        return fs.read_bytes(path)
    except FileNotFoundError as e:
        raise MissingSliceError(str(e)) from e
    except OSError as e:
        if isinstance(fs, LocalFS):
            raise CorruptSliceError(f"{path}: {e}") from e
        raise SourceUnavailableError(f"{fs.url(path)}: {type(e).__name__}: {e}") from e


# ----------------------------------------------------------------------------- image stack


class ImageStackReader(VolumeReader):
    def __init__(self, directory: str | Path, pattern: str | None = None, z_offset: int | None = None, fs: FS | None = None, readahead: int = 6, decoder=None):
        self.fs = fs or LocalFS()
        self.dir = str(directory)
        self.readahead = readahead
        self._decode = decoder or decode_image  # label stacks plug in an integer-preserving decoder
        if not self.fs.is_dir(self.dir):
            raise FileNotFoundError(self.dir)
        rx = re.compile(pattern) if pattern else None
        files: dict[int, str] = {}
        for e in self.fs.listdir(self.dir):
            if e.is_dir or e.name.startswith(".") or Path(e.name).suffix.lower() not in IMAGE_EXTS:
                continue
            if rx and not rx.search(e.name):
                continue
            m = _Z_RE.search(Path(e.name).stem)
            if not m:
                continue
            files[int(m.group(1))] = self.fs.join(self.dir, e.name)
        if not files:
            raise FileNotFoundError(f"no image files with a z index in {self.dir}")
        self._files = files
        zmin, zmax = min(files), max(files)
        self._z_offset = zmin if z_offset is None else z_offset
        n_z = zmax - self._z_offset + 1
        probe_shape, probe_dtype = None, None
        for z in sorted(files):  # probe the first readable slice for (y, x) and dtype
            try:
                a = self._decode(_read(self.fs, files[z]), files[z])
                probe_shape, probe_dtype = a.shape, a.dtype
                break
            except CorruptSliceError:
                continue
        if probe_shape is None:
            raise CorruptSliceError(f"no readable image in {self.dir}")
        self.info = VolumeInfo(
            shape=(n_z, int(probe_shape[0]), int(probe_shape[1])),
            dtype=str(probe_dtype),
            fmt="image_stack",
            path=self.fs.url(self.dir),
            z_offset=self._z_offset,
            n_files=len(files),
            n_missing=n_z - len([z for z in files if z >= self._z_offset]),
            extra={"source": self.fs.scheme},
        )

    def path_for(self, z: int) -> str | None:
        return self._files.get(z + self._z_offset)

    def prefetch_range(self, z0: int, z1: int) -> None:
        """Ask the FS to bring a whole z range into the local cache (one bulk transfer per batch for remote sources)."""
        self.fs.prefetch([q for q in (self.path_for(z) for z in range(z0, z1)) if q])

    def slice_status(self, z: int) -> str:
        if self.path_for(z) is None:
            return "missing"
        return super().slice_status(z)

    def read_slice(self, z: int) -> np.ndarray:
        if not 0 <= z < self.shape[0]:
            raise IndexError(z)
        p = self.path_for(z)
        if p is None:
            raise MissingSliceError(f"z={z} (abs {z + self._z_offset}) has no file")
        if self.readahead:
            self.fs.prefetch([q for q in (self.path_for(z + k) for k in range(1, self.readahead + 1)) if q])
        arr = self._decode(_read(self.fs, p), p)
        if arr.shape != self.shape[1:]:
            raise CorruptSliceError(f"{self.fs.basename(p)}: shape {arr.shape} != {self.shape[1:]}")
        return arr


# ----------------------------------------------------------------------------- npy


class NpyReader(VolumeReader):
    def __init__(self, path: str | Path, axes: str = "zyx", z_offset: int = 0, fs: FS | None = None):
        self.fs = fs or LocalFS()
        self.path = str(path)
        local = self.fs.local_path(self.path)
        if local is None:
            raise ValueError("npy volumes need a local (or cached) file")
        arr = np.load(local, mmap_mode="r")
        if arr.ndim != 3:
            raise ValueError(f"{self.path}: expected 3-D array, got shape {arr.shape}")
        axes = (axes or "zyx").lower()
        if sorted(axes) != ["x", "y", "z"]:
            raise ValueError(f"axes must be a permutation of xyz, got {axes!r}")
        order = [axes.index(a) for a in "zyx"]
        self._arr = np.transpose(arr, order)  # view, still memory-mapped
        self.info = VolumeInfo(shape=tuple(int(s) for s in self._arr.shape), dtype=str(arr.dtype), fmt="npy", path=self.fs.url(self.path), z_offset=z_offset, n_files=1, extra={"axes": axes, "source": self.fs.scheme})

    def read_slice(self, z: int) -> np.ndarray:
        if not 0 <= z < self.shape[0]:
            raise IndexError(z)
        return np.ascontiguousarray(self._arr[z])


# ----------------------------------------------------------------------------- precomputed (raw)


class PrecomputedRawReader(VolumeReader):
    """Minimal reader for Neuroglancer precomputed volumes with `raw` encoding (scale 0)."""

    def __init__(self, directory: str | Path, z_offset: int | None = None, fs: FS | None = None):
        self.fs = fs or LocalFS()
        self.dir = str(directory)
        info_path = self.fs.join(self.dir, "info")
        if not self.fs.is_file(info_path):
            raise FileNotFoundError(info_path)
        meta = json.loads(self.fs.read_text(info_path))
        scale = meta["scales"][0]
        if scale.get("encoding", "raw") != "raw":
            raise NotImplementedError(f"precomputed encoding {scale.get('encoding')!r} not supported in v0.1 (raw only)")
        self.key = scale["key"]
        self.chunk = tuple(int(v) for v in scale["chunk_sizes"][0])  # (cx, cy, cz)
        self.size = tuple(int(v) for v in scale["size"])  # (x, y, z)
        self.offset = tuple(int(v) for v in scale.get("voxel_offset", [0, 0, 0]))
        self.n_channels = int(meta.get("num_channels", 1))
        self._dtype = np.dtype(meta["data_type"])
        self._names = {e.name for e in self.fs.listdir(self.fs.join(self.dir, self.key))} if self.fs.is_dir(self.fs.join(self.dir, self.key)) else set()
        sx, sy, sz = self.size
        self.info = VolumeInfo(
            shape=(sz, sy, sx), dtype=str(self._dtype), fmt="precomputed", path=self.fs.url(self.dir), z_offset=self.offset[2] if z_offset is None else z_offset,
            extra={"key": self.key, "chunk_size": list(self.chunk), "resolution": scale.get("resolution"), "voxel_offset": list(self.offset), "source": self.fs.scheme},
        )

    def _chunk_path(self, x0: int, x1: int, y0: int, y1: int, z0: int, z1: int) -> str | None:
        name = f"{x0}-{x1}_{y0}-{y1}_{z0}-{z1}"
        for cand in (name, name + ".gz"):
            if cand in self._names:
                return self.fs.join(self.dir, self.key, cand)
        return None

    def read_slice(self, z: int) -> np.ndarray:
        sz, sy, sx = self.shape
        if not 0 <= z < sz:
            raise IndexError(z)
        cx, cy, cz = self.chunk
        ox, oy, oz = self.offset
        za = z + oz
        cz0 = (za - oz) // cz * cz + oz
        cz1 = min(cz0 + cz, oz + sz)
        out = np.zeros((sy, sx), dtype=self._dtype)
        found = 0
        for y0 in range(oy, oy + sy, cy):
            y1 = min(y0 + cy, oy + sy)
            for x0 in range(ox, ox + sx, cx):
                x1 = min(x0 + cx, ox + sx)
                p = self._chunk_path(x0, x1, y0, y1, cz0, cz1)
                if p is None:
                    continue
                try:
                    buf = _read(self.fs, p)
                    if p.endswith(".gz"):
                        buf = gzip.decompress(buf)
                    arr = np.frombuffer(buf, dtype=self._dtype).reshape((self.n_channels, cz1 - cz0, y1 - y0, x1 - x0))
                except (SliceReadError, SourceUnavailableError):
                    raise
                except Exception as e:
                    raise CorruptSliceError(f"{self.fs.basename(p)}: {type(e).__name__}: {e}") from e
                out[y0 - oy : y1 - oy, x0 - ox : x1 - ox] = arr[0, za - cz0]
                found += 1
        if found == 0:
            raise MissingSliceError(f"z={z}: no chunk present")
        return out


# ----------------------------------------------------------------------------- factory


def detect_format(fs: FS, path: str) -> str | None:
    if fs.is_file(path) and fs.suffix(path) == ".npy":
        return "npy"
    if fs.is_dir(path):
        entries = fs.listdir(path)
        if any(not e.is_dir and e.name == "info" for e in entries):
            return "precomputed"
        if any(not e.is_dir and Path(e.name).suffix.lower() in IMAGE_EXTS for e in entries):
            return "image_stack"
    return None


def open_volume(path: str | Path, fmt: str = "auto", axes: str = "zyx", z_offset: int | None = None, fs: FS | None = None, cache_dir: str | Path | None = None) -> VolumeReader:
    """`path` may be a local path, or an sftp:// URL when `fs` is not given."""
    p = str(path)
    if fs is None:
        fs, p = parse_root(p, cache_dir=cache_dir) if is_remote(p) else (LocalFS(), p)
    if fmt in (None, "", "auto"):
        fmt = detect_format(fs, p)
        if fmt is None:
            raise FileNotFoundError(f"cannot detect EM volume format at {fs.url(p)}")
    if fmt == "image_stack":
        return ImageStackReader(p, z_offset=z_offset, fs=fs)
    if fmt == "npy":
        return NpyReader(p, axes=axes, z_offset=z_offset or 0, fs=fs)
    if fmt == "precomputed":
        return PrecomputedRawReader(p, z_offset=z_offset, fs=fs)
    raise ValueError(f"unknown format {fmt!r}")
