"""Cloud precomputed volumes as a platform data source (Neuroglancer / CloudVolume).

A cloud dataset is an **ROI view** of a public precomputed volume: a source URL, a mip level and an
xyz bounding box. The platform never treats the whole volume as a dataset — H01 at 4 nm is
1031784 x 712800 x 5293 voxels, so the unit of work is always a bounding box.

Two facts from the 2026-09-08 benchmark of the crawler this is ported from, both load-bearing here:

* Chunks are the real unit of transfer. Reading one z-slice at a time re-downloads the whole
  128x128x32 chunk 32 times, so this reader fetches a z-chunk once and serves 32 slices from it.
* Aligning the request to the chunk grid is free: the same chunks cross the wire either way, so an
  aligned read returns far more voxels for identical bytes. `align=True` is the default.

Coordinates: `roi` is in the voxel grid of the chosen `mip` (no hidden unit conversion — the ported
crawler had a per-axis divisor bug from exactly that). `describe()` prints the nm extent to check against.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from pathlib import Path

import numpy as np

from .readers import CorruptSliceError, MissingSliceError, VolumeInfo, VolumeReader

log = logging.getLogger(__name__)

CLOUD_SCHEMES = ("precomputed://", "gs://", "s3://", "http://", "https://", "file://")


def is_cloud(url: str | Path) -> bool:
    s = str(url)
    if s.startswith("precomputed://"):
        return True
    return s.startswith(("gs://", "s3://")) or bool(re.match(r"^https?://", s))


def normalize_url(url: str) -> str:
    """CloudVolume wants a `precomputed://` prefix; accept the bare bucket / https form too."""
    s = str(url).strip()
    if s.startswith("precomputed://"):
        return s
    if s.startswith(("gs://", "s3://", "file://")):
        return "precomputed://" + s
    m = re.match(r"^https?://storage\.googleapis\.com/([^/].*)$", s)
    if m:
        return "precomputed://gs://" + m.group(1).rstrip("/")
    if re.match(r"^https?://", s):
        return "precomputed://" + s
    raise ValueError(f"not a cloud volume url: {url!r}")


def open_cv(url: str, mip: int = 0, fill_missing: bool = True, parallel: int = 1):
    """Open a CloudVolume. parallel stays 1: H01's scales are sharded, where CloudVolume drops the
    argument anyway, and on non-sharded volumes it forks subprocesses that break byte accounting."""
    try:
        from cloudvolume import CloudVolume
    except ImportError as e:  # pragma: no cover - optional dependency
        raise RuntimeError("cloud sources need cloud-volume: pip install 'cloud-volume>=12'") from e
    return CloudVolume(normalize_url(url), mip=mip, use_https=True, fill_missing=fill_missing, parallel=parallel, progress=False)


def align_outward(lo: int, hi: int, chunk: int) -> tuple[int, int]:
    """Snap a half-open interval outward to the chunk grid."""
    return int(lo // chunk * chunk), int(math.ceil(hi / chunk) * chunk)


def volume_info(url: str, mip: int = 0) -> dict:
    """Shape, chunk grid, resolution and encoding of a cloud volume, without downloading voxels."""
    cv = open_cv(url, mip=mip)
    sc = cv.scale
    return {
        "url": normalize_url(url), "mip": mip, "key": sc["key"], "encoding": sc.get("encoding"),
        "size_xyz": [int(v) for v in sc["size"]], "chunk_xyz": [int(v) for v in sc["chunk_sizes"][0]],
        "resolution_nm": [float(v) for v in sc["resolution"]], "voxel_offset": [int(v) for v in sc.get("voxel_offset", [0, 0, 0])],
        "dtype": str(cv.dtype), "num_channels": int(cv.num_channels), "n_mips": len(cv.info["scales"]),
        "lossy": sc.get("encoding") in ("jpeg",),
    }


class CloudVolumeReader(VolumeReader):
    """One ROI of a cloud precomputed volume, served slice by slice in the platform's (z, y, x) order.

    Voxels are cached per z-chunk on local disk, so QC over a 100-section block downloads each chunk once.
    """

    def __init__(self, url: str, roi: dict | list | tuple, mip: int = 0, cache_dir: str | Path | None = None,
                 align: bool = True, fill_missing: bool = True, label: bool = False):
        self.url = normalize_url(url)
        self.mip = int(mip)
        self.align = bool(align)
        self.label = bool(label)
        self._cv = open_cv(self.url, mip=self.mip, fill_missing=fill_missing)
        sc = self._cv.scale
        self.chunk = tuple(int(v) for v in sc["chunk_sizes"][0])  # (cx, cy, cz)
        self.offset = tuple(int(v) for v in sc.get("voxel_offset", [0, 0, 0]))
        size = tuple(int(v) for v in sc["size"])
        x0, x1, y0, y1, z0, z1 = _roi_tuple(roi)
        lim = [(self.offset[i], self.offset[i] + size[i]) for i in range(3)]
        for name, (a, b), (lo, hi) in zip("xyz", [(x0, x1), (y0, y1), (z0, z1)], lim):
            if not (lo <= a < b <= hi):
                raise ValueError(f"roi {name}=[{a},{b}) outside the volume's [{lo},{hi}) at mip {self.mip}")
        if self.align:  # xy only: z alignment would pull in sections outside the ROI
            x0, x1 = align_outward(x0, x1, self.chunk[0])
            y0, y1 = align_outward(y0, y1, self.chunk[1])
            x0, y0 = max(x0, lim[0][0]), max(y0, lim[1][0])
            x1, y1 = min(x1, lim[0][1]), min(y1, lim[1][1])
        self.roi = (x0, x1, y0, y1, z0, z1)
        self.cache_dir = Path(cache_dir) / "cloud" / self._key() if cache_dir else None
        self._lock = threading.Lock()
        self._mem: dict[int, np.ndarray] = {}
        self.wire = {"chunks_fetched": 0, "decoded_bytes": 0, "seconds": 0.0, "cache_hits": 0}
        self.info = VolumeInfo(
            shape=(z1 - z0, y1 - y0, x1 - x0), dtype=str(self._cv.dtype), fmt="precomputed_cloud", path=f"{self.url}#mip{self.mip}",
            z_offset=z0, n_files=0,
            extra={"source": "cloud", "roi_xyz": list(self.roi), "mip": self.mip, "chunk_xyz": list(self.chunk),
                   "resolution_nm": [float(v) for v in sc["resolution"]], "encoding": sc.get("encoding"),
                   "lossy": sc.get("encoding") in ("jpeg",), "aligned_xy": self.align, "label": self.label},
        )

    # -- plumbing
    def _key(self) -> str:
        h = hashlib.sha1(f"{self.url}|{self.mip}|{self.roi}".encode()).hexdigest()[:16]
        return h

    def _chunk_file(self, k: int) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / f"z{k:06d}.npy"

    def _load_zchunk(self, k: int) -> np.ndarray:
        """(nz, ny, nx) block of the ROI for z-chunk index k (absolute chunk grid)."""
        with self._lock:
            a = self._mem.get(k)
        if a is not None:
            self.wire["cache_hits"] += 1
            return a
        f = self._chunk_file(k)
        if f is not None and f.is_file():
            a = np.load(f)
            with self._lock:
                self._remember(k, a)
            self.wire["cache_hits"] += 1
            return a
        import time

        x0, x1, y0, y1, z0, z1 = self.roi
        cz = self.chunk[2]
        za = max(k * cz, z0)
        zb = min((k + 1) * cz, z1)
        if zb <= za:
            raise MissingSliceError(f"z-chunk {k} outside roi")
        t0 = time.perf_counter()
        try:
            arr = np.asarray(self._cv[x0:x1, y0:y1, za:zb])
        except Exception as e:  # cloud-volume raises many types
            raise CorruptSliceError(f"{self.url} z[{za},{zb}): {type(e).__name__}: {e}") from e
        if arr.ndim == 4:
            arr = arr[..., 0]
        block = np.ascontiguousarray(np.transpose(arr, (2, 1, 0)))  # (x,y,z) -> (z,y,x)
        self.wire["chunks_fetched"] += 1
        self.wire["decoded_bytes"] += int(block.nbytes)
        self.wire["seconds"] += time.perf_counter() - t0
        if f is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_name(f.name + ".part")
            with open(tmp, "wb") as fh:
                np.save(fh, block)
            tmp.replace(f)
        with self._lock:
            self._remember(k, block)
        return block

    def _remember(self, k: int, a: np.ndarray, keep: int = 2) -> None:
        self._mem[k] = a
        while len(self._mem) > keep:
            self._mem.pop(next(iter(self._mem)))

    # -- reader interface
    def read_slice(self, z: int) -> np.ndarray:
        nz = self.shape[0]
        if not 0 <= z < nz:
            raise IndexError(z)
        zabs = self.roi[4] + z
        k = zabs // self.chunk[2]
        block = self._load_zchunk(k)
        za = max(k * self.chunk[2], self.roi[4])
        return block[zabs - za]

    def prefetch_range(self, z0: int, z1: int) -> None:
        cz = self.chunk[2]
        za, zb = self.roi[4] + z0, self.roi[4] + max(z0 + 1, z1)
        for k in range(za // cz, (zb - 1) // cz + 1):
            try:
                self._load_zchunk(k)
            except Exception as e:  # pragma: no cover - network
                log.warning("prefetch z-chunk %s failed: %s", k, e)

    def describe(self) -> dict:
        e = self.info.extra
        r = e["resolution_nm"]
        x0, x1, y0, y1, z0, z1 = self.roi
        return {
            "url": self.url, "mip": self.mip, "roi_xyz": list(self.roi), "shape_zyx": list(self.shape), "dtype": self.info.dtype,
            "resolution_nm": r, "extent_um": [round((x1 - x0) * r[0] / 1000, 2), round((y1 - y0) * r[1] / 1000, 2), round((z1 - z0) * r[2] / 1000, 2)],
            "chunk_xyz": e["chunk_xyz"], "encoding": e["encoding"], "lossy": e["lossy"], "aligned_xy": e["aligned_xy"],
            "n_voxels": int((x1 - x0) * (y1 - y0) * (z1 - z0)), "wire": dict(self.wire),
        }

    def close(self) -> None:
        self._mem.clear()


def _roi_tuple(roi) -> tuple[int, int, int, int, int, int]:
    if isinstance(roi, dict):
        return (int(roi["x"][0]), int(roi["x"][1]), int(roi["y"][0]), int(roi["y"][1]), int(roi["z"][0]), int(roi["z"][1]))
    v = [int(x) for x in roi]
    if len(v) != 6:
        raise ValueError("roi must be [x0,x1,y0,y1,z0,z1] or {'x':[..],'y':[..],'z':[..]}")
    return tuple(v)  # type: ignore[return-value]


def roi_dict(roi) -> dict:
    x0, x1, y0, y1, z0, z1 = _roi_tuple(roi)
    return {"x": [x0, x1], "y": [y0, y1], "z": [z0, z1]}


def parse_roi_arg(s: str) -> dict:
    """'246000-246700,201300-201900,2050-2060' or 'x=..,y=..,z=..' -> roi dict."""
    parts = [p.strip() for p in str(s).split(",") if p.strip()]
    out: dict[str, list[int]] = {}
    for i, p in enumerate(parts):
        if "=" in p:
            axis, rng = p.split("=", 1)
            axis = axis.strip().lower()
        else:
            axis, rng = "xyz"[i], p
        a, _, b = rng.partition("-")
        out[axis] = [int(a), int(b)]
    if set(out) != {"x", "y", "z"}:
        raise ValueError(f"roi needs x, y and z ranges, got {sorted(out)}")
    return out
