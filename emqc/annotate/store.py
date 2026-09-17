"""On-disk annotation store.

A *block* is a directory with `em.npy` (x, y, z) uint8 and optionally `seg.npy` (x, y, z) integer ids,
plus `meta.json`. Edits never touch `seg.npy`: the first write copies it to `seg_edit.npy` (copy-on-write)
and every subsequent change goes there. Each edit is also logged as `edits/<n>.npz` (the pixels it changed
and their previous ids) so it can be undone exactly, and summarised in `edits.jsonl` for the UI.

Per slice the viewer gets a *label index map*: ids are renumbered 0..k (0 stays background) into a uint16
image, shipped as a lossless RGB PNG (R = hi byte, G = lo byte) together with the index -> id table. The
browser does colouring, eyedropping and hover-highlight on that map without a round trip; ids are sent as
strings because H01 ids exceed 2^53 and would lose precision as JSON numbers.
"""
from __future__ import annotations

import io
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

SEG_EDIT = "seg_edit.npy"
EDIT_DIR = "edits"
EDIT_LOG = "edits.jsonl"
MAX_LABELS_PER_SLICE = 65535


def find_blocks(root: Path | None, max_depth: int = 4) -> list[Path]:
    """Directories under `root` (up to max_depth deep) that contain em.npy — the delivery layout is
    blocks/<dataset>/<block>/em.npy, but a flat <block>/em.npy works too."""
    if not root or not Path(root).exists():
        return []
    root = Path(root)
    return sorted(p.parent for p in root.rglob("em.npy") if len(p.relative_to(root).parts) <= max_depth)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)  # speed over size: these are streamed per slice
    return buf.getvalue()


class Block:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.id = self.path.name
        self.em = np.load(self.path / "em.npy", mmap_mode="r")  # (x, y, z)
        if self.em.ndim != 3:
            raise ValueError(f"{self.path}/em.npy must be 3-D (x, y, z), got {self.em.shape}")
        self.has_seg = (self.path / "seg.npy").exists()
        self._seg_ro = np.load(self.path / "seg.npy", mmap_mode="r") if self.has_seg else None
        self._seg_rw = None
        self.meta = json.load(open(self.path / "meta.json")) if (self.path / "meta.json").exists() else {}
        self.lock = threading.RLock()
        self._max_id: int | None = None
        self._png_cache: OrderedDict[tuple, bytes] = OrderedDict()
        self._label_cache: OrderedDict[int, tuple[np.ndarray, np.ndarray]] = OrderedDict()

    # ------------------------------------------------------------------ geometry
    @property
    def nz(self) -> int:
        return int(self.em.shape[2])

    @property
    def shape_zyx(self) -> tuple[int, int, int]:
        x, y, z = self.em.shape
        return (int(z), int(y), int(x))

    def info(self) -> dict:
        g = self.meta.get("geometry", {})
        return {
            "block_id": self.id, "path": str(self.path), "has_seg": self.has_seg,
            "shape_zyx": list(self.shape_zyx), "dtype_em": str(self.em.dtype),
            "dtype_seg": str(self._seg_ro.dtype) if self.has_seg else None,
            "voxel_size_nm": g.get("voxel_size_nm"), "origin": g.get("origin"), "dataset": self.meta.get("dataset", {}).get("id"),
            "n_edits": len(self.edits()), "has_working_copy": (self.path / SEG_EDIT).exists(),
            "working_copy": str(self.path / SEG_EDIT),
        }

    # ------------------------------------------------------------------ label volume access
    def _seg(self) -> np.ndarray:
        """Read view: the working copy when it exists, else the pristine seg.npy."""
        if self._seg_rw is None and (self.path / SEG_EDIT).exists():
            self._seg_rw = np.load(self.path / SEG_EDIT, mmap_mode="r+")
        return self._seg_rw if self._seg_rw is not None else self._seg_ro

    def _seg_writable(self) -> np.ndarray:
        """Copy-on-write: materialise seg_edit.npy from seg.npy on first edit; seg.npy is never modified."""
        if not self.has_seg:
            raise ValueError("block has no seg.npy")
        p = self.path / SEG_EDIT
        if self._seg_rw is None:
            if not p.exists():
                src = self._seg_ro
                tmp = self.path / (SEG_EDIT + ".part")
                dst = np.lib.format.open_memmap(tmp, mode="w+", dtype=src.dtype, shape=src.shape)
                step = max(1, src.shape[2] // 10)
                for k in range(0, src.shape[2], step):  # chunked so an 800 MB volume never sits in RAM twice
                    dst[:, :, k:k + step] = src[:, :, k:k + step]
                dst.flush()
                del dst
                tmp.rename(p)
            self._seg_rw = np.load(p, mmap_mode="r+")
        return self._seg_rw

    def em_slice(self, z: int) -> np.ndarray:
        """(y, x) view of section z for display."""
        return np.ascontiguousarray(self.em[:, :, z].T)

    def seg_slice(self, z: int) -> np.ndarray:
        return np.ascontiguousarray(self._seg()[:, :, z].T)

    def _check_z(self, z: int) -> None:
        if not 0 <= z < self.nz:
            raise IndexError(f"z {z} out of range 0..{self.nz - 1}")

    # ------------------------------------------------------------------ renderings (cached)
    def _cached(self, key: tuple, make):
        with self.lock:
            if key in self._png_cache:
                self._png_cache.move_to_end(key)
                return self._png_cache[key]
        val = make()
        with self.lock:
            self._png_cache[key] = val
            while len(self._png_cache) > 64:
                self._png_cache.popitem(last=False)
        return val

    def em_png(self, z: int) -> bytes:
        self._check_z(z)
        return self._cached(("em", z), lambda: _png(Image.fromarray(self.em_slice(z), mode="L")))

    def labels(self, z: int) -> tuple[np.ndarray, np.ndarray]:
        """(idx16 (y, x), ids) — idx 0 is always id 0 / background, even when the slice has no background."""
        self._check_z(z)
        with self.lock:
            if z in self._label_cache:
                self._label_cache.move_to_end(z)
                return self._label_cache[z]
        s = self.seg_slice(z)
        ids, inv = np.unique(s, return_inverse=True)
        if ids.size == 0 or ids[0] != 0:
            ids = np.concatenate([np.zeros(1, dtype=ids.dtype), ids])
            inv = inv + 1
        if ids.size > MAX_LABELS_PER_SLICE:
            raise ValueError(f"slice {z} has {ids.size} labels; the uint16 index map holds at most {MAX_LABELS_PER_SLICE}")
        idx = inv.reshape(s.shape).astype(np.uint16)
        with self.lock:
            self._label_cache[z] = (idx, ids)
            while len(self._label_cache) > 16:
                self._label_cache.popitem(last=False)
        return idx, ids

    def labels_png(self, z: int) -> bytes:
        def make():
            idx, _ = self.labels(z)
            rgb = np.zeros(idx.shape + (3,), dtype=np.uint8)
            rgb[..., 0] = idx >> 8
            rgb[..., 1] = idx & 255
            return _png(Image.fromarray(rgb, mode="RGB"))
        return self._cached(("labels", z), make)

    def labels_table(self, z: int) -> dict:
        idx, ids = self.labels(z)
        counts = np.bincount(idx.ravel(), minlength=ids.size)
        return {"z": z, "n": int(ids.size), "ids": [str(int(i)) for i in ids], "counts": counts.tolist()}

    def pick(self, z: int, x: int, y: int) -> int:
        self._check_z(z)
        return int(self._seg()[x, y, z])

    def _invalidate(self, z: int) -> None:
        with self.lock:
            self._label_cache.pop(z, None)
            self._png_cache.pop(("labels", z), None)

    # ------------------------------------------------------------------ ids
    def max_id(self) -> int:
        if self._max_id is None:
            seg = self._seg()
            m = 0
            for k in range(seg.shape[2]):  # one section at a time: a full-volume max would page in 800 MB
                m = max(m, int(seg[:, :, k].max()))
            self._max_id = m
        return self._max_id

    def new_id(self) -> int:
        with self.lock:
            self._max_id = self.max_id() + 1
            return self._max_id

    # ------------------------------------------------------------------ edits
    def _edit_dir(self) -> Path:
        d = self.path / EDIT_DIR
        d.mkdir(exist_ok=True)
        return d

    def edits(self) -> list[dict]:
        p = self.path / EDIT_LOG
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def _record(self, kind: str, z: int | None, xs: np.ndarray, ys: np.ndarray, old, new_id: int, extra: dict | None = None,
                zs: np.ndarray | None = None) -> dict:
        """Persist one edit: every changed voxel (x, y, z) and the id it had before. `z` is the section shown in the
        UI (None for a 3-D edit spanning several sections); `zs` defaults to a constant z."""
        log = self.edits()
        n = (log[-1]["n"] + 1) if log else 1
        if zs is None:
            zs = np.full(xs.shape, int(z), dtype=np.uint16)
        np.savez_compressed(self._edit_dir() / f"{n:06d}.npz", z=-1 if z is None else int(z), xs=xs.astype(np.uint16), ys=ys.astype(np.uint16),
                            zs=zs.astype(np.uint16), old=np.asarray(old), new=np.asarray(new_id))
        rec = {"n": n, "kind": kind, "z": (None if z is None else int(z)), "n_px": int(xs.size), "new_id": str(int(new_id)),
               "old_id": (str(int(old)) if np.ndim(old) == 0 else None), "n_slices": int(np.unique(zs).size),
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **(extra or {})}
        with open(self.path / EDIT_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self._max_id = None if self._max_id is None else max(self._max_id, int(new_id))
        return rec

    def fill(self, z: int, x: int, y: int, new_id: int, whole_slice: bool = False) -> dict | None:
        """Bucket fill: relabel the connected component of the clicked pixel (4-connectivity within the slice) to
        `new_id`; with whole_slice, every pixel of that id in the slice. Returns the edit record, or None if the
        clicked pixel already has new_id."""
        self._check_z(z)
        with self.lock:
            seg = self._seg_writable()
            plane = seg[:, :, z]  # (x, y) view on the memmap: writes go straight to disk
            old = int(plane[x, y])
            if old == new_id:
                return None
            mask = plane == old
            if not whole_slice:
                lab, _ = ndimage.label(mask)
                mask = lab == lab[x, y]
            xs, ys = np.nonzero(mask)
            plane[mask] = new_id
            seg.flush()
            self._invalidate(z)
            return self._record("fill", z, xs, ys, old, new_id, {"x": int(x), "y": int(y), "whole_slice": bool(whole_slice)})

    def paint(self, z: int, points: list[tuple[int, int]], radius: int, new_id: int) -> dict | None:
        """Brush: stamp a disc of `radius` at every point of the stroke (points are consecutive, so gaps are
        bridged by interpolation) and set those pixels to new_id. Pixels already carrying new_id are skipped."""
        self._check_z(z)
        if not points:
            return None
        radius = max(0, int(radius))
        with self.lock:
            seg = self._seg_writable()
            plane = seg[:, :, z]
            W, H = plane.shape
            mask = np.zeros(plane.shape, dtype=bool)
            yy, xx = np.ogrid[-radius:radius + 1, -radius:radius + 1]
            disc = (xx * xx + yy * yy) <= radius * radius
            pts = [points[0]]
            for (x0, y0), (x1, y1) in zip(points, points[1:]):  # bridge gaps between sampled mouse positions
                n = int(max(abs(x1 - x0), abs(y1 - y0)))
                pts.extend((int(round(x0 + (x1 - x0) * t / n)), int(round(y0 + (y1 - y0) * t / n))) for t in range(1, n + 1))
            for x, y in pts:
                x0, x1 = max(0, x - radius), min(W, x + radius + 1)
                y0, y1 = max(0, y - radius), min(H, y + radius + 1)
                if x1 <= x0 or y1 <= y0:
                    continue
                mask[x0:x1, y0:y1] |= disc[x0 - (x - radius):x1 - (x - radius), y0 - (y - radius):y1 - (y - radius)]
            mask &= plane != new_id
            xs, ys = np.nonzero(mask)
            if xs.size == 0:
                return None
            old = plane[xs, ys].copy()
            plane[xs, ys] = new_id
            seg.flush()
            self._invalidate(z)
            return self._record("paint", z, xs, ys, old, new_id, {"radius": radius, "n_points": len(points)})

    def merge(self, from_id: int, to_id: int, scope: str = "block", z: int | None = None) -> dict | None:
        """Give every voxel of `from_id` the id `to_id` — the two cells become one segment (one colour).
        scope "block": all sections; "slice": only section z. Returns the edit record, or None if nothing changed."""
        if from_id == to_id:
            return None
        with self.lock:
            seg = self._seg_writable()
            ks = [int(z)] if scope == "slice" else range(seg.shape[2])
            if scope == "slice":
                self._check_z(int(z))
            xs_all, ys_all, zs_all = [], [], []
            for k in ks:
                plane = seg[:, :, k]
                m = plane == from_id
                if not m.any():
                    continue
                xs, ys = np.nonzero(m)
                plane[m] = to_id
                xs_all.append(xs); ys_all.append(ys); zs_all.append(np.full(xs.shape, k, dtype=np.uint16))
            if not xs_all:
                return None
            seg.flush()
            xs, ys, zs = np.concatenate(xs_all), np.concatenate(ys_all), np.concatenate(zs_all)
            for k in np.unique(zs):
                self._invalidate(int(k))
            return self._record("merge", (int(z) if scope == "slice" else None), xs, ys, from_id, to_id, {"scope": scope}, zs=zs)

    def undo(self) -> dict | None:
        """Revert the most recent edit exactly (per-pixel previous ids) and drop it from the log."""
        with self.lock:
            log = self.edits()
            if not log:
                return None
            rec = log[-1]
            f = self._edit_dir() / f"{rec['n']:06d}.npz"
            d = np.load(f)
            seg = self._seg_writable()
            xs, ys = d["xs"].astype(np.intp), d["ys"].astype(np.intp)
            zs = d["zs"].astype(np.intp) if "zs" in d.files else np.full(xs.shape, int(d["z"]), dtype=np.intp)
            for k in np.unique(zs):  # one section at a time keeps the memmap writes sequential
                m = zs == k
                seg[xs[m], ys[m], int(k)] = d["old"][m] if np.ndim(d["old"]) else d["old"]
            seg.flush()
            f.unlink()
            (self.path / EDIT_LOG).write_text("".join(json.dumps(r) + "\n" for r in log[:-1]))
            for k in np.unique(zs):
                self._invalidate(int(k))
            self._max_id = None
            return rec


class AnnotateStore:
    def __init__(self, root: Path | None):
        self.root = Path(root) if root else None
        self._blocks: dict[str, Block] = {}
        self._lock = threading.Lock()

    def refresh(self) -> list[dict]:
        found = find_blocks(self.root)
        with self._lock:
            for p in found:
                if p.name not in self._blocks:
                    try:
                        self._blocks[p.name] = Block(p)
                    except Exception as e:  # a half-copied block must not take the page down
                        self._blocks[p.name] = e  # type: ignore[assignment]
            return [self._summary(k) for k in sorted(self._blocks)]

    def _summary(self, key: str) -> dict:
        b = self._blocks[key]
        if isinstance(b, Exception):
            return {"block_id": key, "error": str(b)}
        z, y, x = b.shape_zyx
        return {"block_id": b.id, "has_seg": b.has_seg, "nz": z, "height": y, "width": x, "dataset": b.meta.get("dataset", {}).get("id"),
                "n_edits": len(b.edits()), "has_working_copy": (b.path / SEG_EDIT).exists()}

    def get(self, block_id: str) -> Block:
        if block_id not in self._blocks:
            self.refresh()
        b = self._blocks.get(block_id)
        if b is None:
            raise KeyError(block_id)
        if isinstance(b, Exception):
            raise ValueError(str(b))
        return b
