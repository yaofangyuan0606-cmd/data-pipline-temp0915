"""Label volumes (GT segmentation ids, class masks, model predictions) read through the same FS / reader stack as EM.

Encodings
    gray     one integer channel (L, I;16, I, uint8/16/32 arrays) - used as is
    palette  PNG palette indices (mode P) - the index is the class id, the palette colors are ignored
    rgb24    RGB where the id is packed into three bytes: id = R<<16 | G<<8 | B  (common export of segmentations
             into ordinary PNG viewers). 3872 distinct colors on mouse_30um seg/mip1 map to 3872 distinct ids.
    auto     RGB/RGBA -> rgb24, everything else -> gray/palette

Nothing here writes to the data source.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np

from .fs import FS
from .readers import CorruptSliceError, ImageStackReader, VolumeReader, open_volume

LABEL_ENCODINGS = ("auto", "gray", "palette", "rgb24")


def pack_rgb24(arr: np.ndarray) -> np.ndarray:
    a = arr[..., :3].astype(np.uint32)
    return (a[..., 0] << 16) | (a[..., 1] << 8) | a[..., 2]


def decode_label(data: bytes, name: str, encoding: str = "auto") -> np.ndarray:
    """Bytes of one label section -> 2-D integer array. Never averages channels (that is what the EM decoder does)."""
    suffix = Path(name).suffix.lower()
    try:
        if suffix in (".tif", ".tiff"):
            import tifffile

            arr = np.asarray(tifffile.imread(io.BytesIO(data)))
            mode = "tiff"
        else:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                im.load()
                mode = im.mode
                arr = np.asarray(im)
    except Exception as e:
        raise CorruptSliceError(f"{name}: {type(e).__name__}: {e}") from e
    if arr.ndim == 3:
        if arr.shape[-1] in (3, 4):
            if encoding in ("auto", "rgb24"):
                arr = pack_rgb24(arr)
            else:  # gray requested on an RGB file: take one channel
                arr = arr[..., 0]
        else:
            arr = arr[0]
    elif encoding == "rgb24":
        raise CorruptSliceError(f"{name}: rgb24 encoding requested but the image has one channel (mode {mode})")
    if arr.ndim != 2:
        raise CorruptSliceError(f"{name}: unexpected label ndim={arr.ndim}")
    if arr.dtype.kind == "f":
        arr = arr.astype(np.int64)
    if arr.dtype == np.bool_:
        arr = arr.astype(np.uint8)
    return arr


def encoding_for(asset_format: str, extra: dict | None) -> str:
    e = (extra or {}).get("label_encoding")
    if e in LABEL_ENCODINGS:
        return e
    if "rgb" in (asset_format or "").lower():
        return "rgb24"
    return "auto"


def open_label_volume(fs: FS, base: str, asset_path: str, asset_format: str = "", extra: dict | None = None, z_offset: int | None = None) -> VolumeReader:
    """Open a label asset relative to the dataset root. image_stack* formats get the integer-preserving decoder."""
    path = fs.join(base, asset_path) if asset_path not in ("", ".") else base
    fmt = (asset_format or "auto").lower()
    if fmt.startswith("image_stack") or (fmt == "auto" and fs.is_dir(path) and not fs.is_file(fs.join(path, "info"))):
        enc = encoding_for(asset_format, extra)
        return ImageStackReader(path, z_offset=z_offset, fs=fs, decoder=lambda data, name: decode_label(data, name, enc))
    return open_volume(path, fmt if fmt != "auto" else "auto", "zyx", z_offset, fs=fs)  # npy / precomputed keep their dtype


class DownsampledLabelReader(VolumeReader):
    """A label volume at k x the EM resolution, served at EM resolution by striding (nearest neighbour: ids stay ids)."""

    def __init__(self, inner: VolumeReader, k: int):
        from .readers import VolumeInfo

        self.inner, self.k, self.fs = inner, int(k), inner.fs
        z, y, x = inner.shape
        self.info = VolumeInfo(shape=(z, y // self.k, x // self.k), dtype=inner.info.dtype, fmt=inner.info.fmt, path=inner.info.path, z_offset=inner.info.z_offset,
                               n_files=inner.info.n_files, n_missing=inner.info.n_missing, extra={**inner.info.extra, "downsampled_by": self.k, "native_shape": list(inner.shape)})

    def read_slice(self, z: int) -> np.ndarray:
        return np.ascontiguousarray(self.inner.read_slice(z)[:: self.k, :: self.k])

    def slice_status(self, z: int) -> str:
        return self.inner.slice_status(z)

    def path_for(self, z: int):
        return self.inner.path_for(z) if hasattr(self.inner, "path_for") else None

    def prefetch_range(self, z0: int, z1: int) -> None:
        if hasattr(self.inner, "prefetch_range"):
            self.inner.prefetch_range(z0, z1)

    def close(self) -> None:
        self.inner.close()


def to_em_resolution(reader: VolumeReader, em_shape_yx: tuple[int, int]) -> VolumeReader:
    """Wrap a label reader whose plane is an integer multiple of the EM plane; anything else is returned as is."""
    ly, lx = reader.shape[1:]
    ey, ex = em_shape_yx
    if ly > ey and lx > ex and ly % ey == 0 and lx % ex == 0 and ly // ey == lx // ex:
        return DownsampledLabelReader(reader, ly // ey)
    return reader


def boundary_map(ids: np.ndarray) -> np.ndarray:
    """1 where a pixel's id differs from its right or lower neighbour: the membrane / boundary target derived from ids."""
    b = np.zeros(ids.shape, dtype=np.uint8)
    b[:, :-1] |= (ids[:, :-1] != ids[:, 1:]).astype(np.uint8)
    b[:-1, :] |= (ids[:-1, :] != ids[1:, :]).astype(np.uint8)
    return b
