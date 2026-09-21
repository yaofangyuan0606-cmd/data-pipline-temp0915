"""On-disk annotation store.

A *block* is a directory with `em.npy` and optionally `seg.npy`, plus `meta.json`.

**Orientation.** The arrays are stored the way `fetch_train.py` received them from CloudVolume, i.e. axis 0 runs
along the source volume's X and axis 1 along its Y. The viewer draws a section with X horizontal and Y vertical,
the same way Neuroglancer and the rest of the connectomics world draw it, so a displayed section is the TRANSPOSE
of the stored one: `arr[:, :, z].T`. Screen (x = column, y = row) therefore reaches `arr[x, y, z]`.

Getting this backwards was a real bug: the page used to draw `arr[:, :, z]` directly, which showed every section
mirrored across its diagonal relative to the source dataset, and a jump to Neuroglancer landed on the transposed
pixel. The mapping was pinned down by downloading the volume with CloudVolume and cross-correlating: all four
quadrant blocks match at exactly 1.000 with zero displacement under the convention above.

`em_slice` / `seg_slice` hand out the displayed (transposed) orientation, so everything that thinks in screen
coordinates — the tools, the boundary fill, SAM — is automatically right. Only the edit records go the other way:
they store indices into the array as it sits on disk, so that records written before this change still replay.

The data directory is treated as read-only. Everything the viewer writes lives in a separate *work directory*
(`<workdir>/<block_id>/`): the first edit copies `seg.npy` there as `seg_edit.npy` (copy-on-write) and every
change goes to that copy; each edit is logged as `edits/<n>.npz` (the voxels it changed and their previous ids)
so it can be undone exactly, and summarised in `edits.jsonl` for the UI.

Screen coordinates are (x = column, y = row); on a displayed plane that is `plane[y, x]`. Edit records store
indices into the on-disk array: `xs` = axis-0, `ys` = axis-1 — the historical names, kept so older records replay.
`_disk_idx` converts a displayed-frame mask into that pair.

Per slice the viewer gets a *label index map*: ids are renumbered 0..k (0 stays background) into a uint16
image, shipped as a lossless RGB PNG (R = hi byte, G = lo byte) together with the index -> id table. The
browser does colouring, eyedropping and hover-highlight on that map without a round trip; ids are sent as
strings because H01 ids exceed 2^53 and would lose precision as JSON numbers.
"""
from __future__ import annotations

import io
import json
import shutil
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
    def __init__(self, path: Path, workdir: Path | None = None):
        self.path = Path(path)
        self.id = self.path.name
        self.work = Path(workdir) / self.id if workdir else self.path  # None only for legacy callers/tests
        self.em = np.load(self.path / "em.npy", mmap_mode="r")  # (rows, cols, z)
        if self.em.ndim != 3:
            raise ValueError(f"{self.path}/em.npy must be 3-D, got {self.em.shape}")
        self.has_seg = (self.path / "seg.npy").exists()
        self._seg_ro = np.load(self.path / "seg.npy", mmap_mode="r") if self.has_seg else None
        self._seg_rw = None
        self.meta = json.load(open(self.path / "meta.json")) if (self.path / "meta.json").exists() else {}
        self.visual_em = self._find_visual_em()
        self._migrate_legacy()
        self.lock = threading.RLock()
        self._max_id: int | None = None
        self._png_cache: OrderedDict[tuple, bytes] = OrderedDict()
        self._label_cache: OrderedDict[int, tuple[np.ndarray, np.ndarray]] = OrderedDict()

    def _find_visual_em(self) -> Path | None:
        """`visual/slices_em/z0000.png` next to em.npy: the delivery's reference rendering of each section.
        Used verbatim as the EM layer when its size matches the array (rows x cols)."""
        d = self.path / "visual" / "slices_em"
        f = d / "z0000.png"
        if not f.exists():
            return None
        try:
            with Image.open(f) as im:
                w, h = im.size
        except Exception:
            return None
        return d if (h, w) == tuple(int(v) for v in self.em.shape[:2]) else None

    def _migrate_legacy(self) -> None:
        """Earlier versions wrote seg_edit.npy / edits into the data directory. Move them to the work directory
        once, so the data directory is left exactly as delivered and no edit is lost."""
        if self.work == self.path:
            return
        legacy = [self.path / SEG_EDIT, self.path / EDIT_DIR, self.path / EDIT_LOG]
        if not any(p.exists() for p in legacy):
            return
        self.work.mkdir(parents=True, exist_ok=True)
        for src in legacy:
            if src.exists() and not (self.work / src.name).exists():
                shutil.move(str(src), str(self.work / src.name))

    # ------------------------------------------------------------------ geometry
    @property
    def nz(self) -> int:
        return int(self.em.shape[2])

    @property
    def shape_zyx(self) -> tuple[int, int, int]:
        a0, a1, z = self.em.shape          # on disk: (volume X, volume Y, z)
        return (int(z), int(a1), int(a0))  # displayed: (z, rows = Y, cols = X)

    def info(self) -> dict:
        g = self.meta.get("geometry", {})
        return {
            "block_id": self.id, "path": str(self.path), "has_seg": self.has_seg,
            "shape_zyx": list(self.shape_zyx), "dtype_em": str(self.em.dtype),
            "dtype_seg": str(self._seg_ro.dtype) if self.has_seg else None,
            "voxel_size_nm": g.get("voxel_size_nm"), "origin": g.get("origin"), "dataset": self.meta.get("dataset", {}).get("id"),
            "n_edits": len(self.edits()), "has_working_copy": (self.work / SEG_EDIT).exists(),
            "working_copy": str(self.work / SEG_EDIT), "workdir": str(self.work), "em_source": "em.npy",
            "em_version": "3-transposed",  # bump when the EM rendering changes; the viewer keys its image URLs on it
        }

    # ------------------------------------------------------------------ label volume access
    def _seg(self) -> np.ndarray:
        """Read view: the working copy when it exists, else the pristine seg.npy."""
        if self._seg_rw is None and (self.work / SEG_EDIT).exists():
            self._seg_rw = np.load(self.work / SEG_EDIT, mmap_mode="r+")
        return self._seg_rw if self._seg_rw is not None else self._seg_ro

    def _seg_writable(self) -> np.ndarray:
        """Copy-on-write: materialise seg_edit.npy from seg.npy on first edit; seg.npy is never modified."""
        if not self.has_seg:
            raise ValueError("block has no seg.npy")
        p = self.work / SEG_EDIT
        if self._seg_rw is None:
            if not p.exists():
                self.work.mkdir(parents=True, exist_ok=True)
                src = self._seg_ro
                tmp = self.work / (SEG_EDIT + ".part")
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
        """Section z as displayed: (rows = volume Y, cols = volume X). See the orientation note at the top."""
        return np.ascontiguousarray(self.em[:, :, z].T)

    def seg_slice(self, z: int) -> np.ndarray:
        return np.ascontiguousarray(self._seg()[:, :, z].T)

    def _plane(self, z: int) -> np.ndarray:
        """A writable, displayed-orientation view of section z. Writes go through to the memmap."""
        return self._seg_writable()[:, :, z].T

    def _plane_ro(self, z: int) -> np.ndarray:
        return self._seg()[:, :, z].T

    @staticmethod
    def _disk_idx(mask_display: np.ndarray):
        """A displayed-frame boolean mask -> (xs, ys), indices into the on-disk array, for the edit record."""
        rows, cols = np.nonzero(mask_display)
        return cols, rows        # displayed row = on-disk axis 1, displayed column = on-disk axis 0

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
        # The delivery's own PNGs are in the on-disk orientation; since sections are now displayed transposed they
        # can no longer be served verbatim, so the EM layer is always rendered from em.npy.
        return self._cached(("em", z), lambda: _png(Image.fromarray(self.em_slice(z), mode="L")))

    def labels(self, z: int) -> tuple[np.ndarray, np.ndarray]:
        """(idx16 (y, x), ids) — idx 0 is always id 0 / background, even when the slice has no background."""
        self._check_z(z)
        with self.lock:
            if z in self._label_cache:
                self._label_cache.move_to_end(z)
                return self._label_cache[z]
        s = self.seg_slice(z)
        # `np.unique(..., return_inverse=True)` argsorts every pixel; we only need "value -> position in ids",
        # and searchsorted on the (already sorted) ids gives exactly that, 6x faster on a 1024² section.
        ids = np.unique(s)
        if ids.size == 0 or ids[0] != 0:
            ids = np.concatenate([np.zeros(1, dtype=ids.dtype), ids])
        if ids.size > MAX_LABELS_PER_SLICE:
            raise ValueError(f"slice {z} has {ids.size} labels; the uint16 index map holds at most {MAX_LABELS_PER_SLICE}")
        idx = np.searchsorted(ids, s).astype(np.uint16)
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
        return int(self._seg()[x, y, z])        # screen x -> axis 0, screen y -> axis 1

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
        d = self.work / EDIT_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def edits(self) -> list[dict]:
        p = self.work / EDIT_LOG
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
        many = np.ndim(new_id) > 0                  # a repair writes a different id per pixel
        rec = {"n": n, "kind": kind, "z": (None if z is None else int(z)), "n_px": int(xs.size),
               "new_id": (f"{int(np.unique(new_id).size)} 个 id" if many else str(int(new_id))),
               "old_id": (str(int(old)) if np.ndim(old) == 0 else None), "n_slices": int(np.unique(zs).size),
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **(extra or {})}
        self.work.mkdir(parents=True, exist_ok=True)
        with open(self.work / EDIT_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        if self._max_id is not None:
            self._max_id = max(self._max_id, int(np.max(new_id)) if many else int(new_id))
        return rec

    def apply_mask(self, z: int, mask: np.ndarray, new_id: int, metadata: dict, kind: str = "sam") -> dict | None:
        """Apply a previewed mask (SAM or boundary-aware fill) in display (y, x) coordinates; preserve exact undo."""
        self._check_z(z)
        if mask.shape != self.shape_zyx[1:] or mask.dtype != np.bool_:
            raise ValueError("mask shape or dtype does not match the slice")
        with self.lock:
            if not self.has_seg:
                raise ValueError("当前数据块没有标签基线，无法应用；仍可预览分割")
            limits = np.iinfo(self._seg_ro.dtype)
            if not 0 <= new_id <= limits.max:
                raise ValueError("label id is outside the segmentation dtype range")
            changed = mask & (self._plane_ro(z) != new_id)
            xs, ys = self._disk_idx(changed)
            if not xs.size:
                return None
            seg = self._seg_writable()
            old = seg[xs, ys, z].copy()
            rec = self._record(kind, z, xs, ys, old, new_id, metadata)
            seg[xs, ys, z] = new_id
            seg.flush()
            self._invalidate(z)
            return rec

    def _fresh_ids(self, n: int) -> list[int]:
        """n unused ids (max + 1 ..), reserved immediately. Called with the edit lock held."""
        base = self.max_id()
        ids = [base + k for k in range(1, n + 1)]
        self._max_id = ids[-1]
        return ids

    def split_component(self, z: int, x: int, y: int) -> dict:
        """分离: the clicked 4-connected piece of its label gets a fresh id. Only meaningful when the label has other,
        disconnected pieces in this slice (two cells wrongly sharing one id that do not touch here)."""
        self._check_z(z)
        with self.lock:
            plane = self._plane_ro(z)
            old = int(plane[y, x])
            if old == 0:
                raise ValueError("背景不能分离，请点一个色块")
            comps, n = ndimage.label(plane == old)
            if n < 2:
                raise ValueError("该颜色在本片只有一块；相连的色块请用切割线画开")
            mask = comps == comps[y, x]
            xs, ys = self._disk_idx(mask)
            (new_id,) = self._fresh_ids(1)
            seg = self._seg_writable()
            seg[xs, ys, z] = new_id
            seg.flush()
            self._invalidate(z)
            return self._record("split", z, xs, ys, old, new_id, {"mode": "component", "x": int(x), "y": int(y), "pieces": int(n)})

    @staticmethod
    def _rasterize(points: list[tuple[int, int]], shape: tuple[int, int]) -> np.ndarray:
        """8-connected 1-px path through the points (x = column, y = row), which is a wall for 4-connected labelling."""
        path = np.zeros(shape, dtype=bool)
        H, W = shape
        pts = [points[0]]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            n = int(max(abs(x1 - x0), abs(y1 - y0)))
            pts.extend((int(round(x0 + (x1 - x0) * t / n)), int(round(y0 + (y1 - y0) * t / n))) for t in range(1, n + 1))
        for x, y in pts:
            if 0 <= x < W and 0 <= y < H:
                path[y, x] = True
        return path

    def cut(self, z: int, points: list[tuple[int, int]]) -> dict:
        """切割: draw a line through a label; the label's component(s) crossed by the line fall apart into pieces.
        The largest piece keeps the id, every other piece gets a fresh id, and the pixels under the line itself go
        to the nearest piece so no gap is left. The label to cut is the one the line runs over the most."""
        self._check_z(z)
        if len(points) < 2:
            raise ValueError("切割线至少需要两个点")
        with self.lock:
            plane = self._plane_ro(z)
            path = self._rasterize(points, plane.shape)
            ids, counts = np.unique(plane[path], return_counts=True)
            keep = ids != 0
            if not keep.any():
                raise ValueError("切割线没有经过任何色块")
            cut_id = int(ids[keep][np.argmax(counts[keep])])
            comps, _ = ndimage.label(plane == cut_id)
            touched = np.unique(comps[path & (plane == cut_id)])
            xs_all, ys_all, vals_all, new_ids = [], [], [], []
            for c in touched[touched > 0]:
                comp = comps == c
                pieces, n = ndimage.label(comp & ~path)
                if n < 2:
                    continue
                sizes = np.bincount(pieces.ravel())
                sizes[0] = 0
                largest = int(sizes.argmax())
                _, (iy, ix) = ndimage.distance_transform_edt(pieces == 0, return_indices=True)
                assigned = pieces.copy()
                bar = comp & path
                assigned[bar] = pieces[iy[bar], ix[bar]]  # line pixels join the nearest piece
                fresh = self._fresh_ids(n - 1)
                for k in [k for k in range(1, n + 1) if k != largest]:
                    nid = fresh.pop(0)
                    xs, ys = self._disk_idx(assigned == k)
                    xs_all.append(xs); ys_all.append(ys); vals_all.append(np.full(xs.shape, nid, dtype=np.uint64)); new_ids.append(nid)
            if not new_ids:
                raise ValueError("切割线没有把色块分开，请从色块外画到色块外、穿过整个色块")
            xs, ys, vals = np.concatenate(xs_all), np.concatenate(ys_all), np.concatenate(vals_all)
            seg = self._seg_writable()
            seg[xs, ys, z] = vals.astype(seg.dtype)
            seg.flush()
            self._invalidate(z)
            return self._record("split", z, xs, ys, cut_id, new_ids[0],
                                {"mode": "line", "cut_id": str(cut_id), "pieces": len(new_ids) + 1, "new_ids": [str(i) for i in new_ids], "n_points": len(points)})

    def apply_labels(self, z: int, labels: np.ndarray, where: np.ndarray, metadata: dict) -> dict | None:
        """Write many different ids at once, inside `where` — what repairing a destroyed section needs.

        Unlike every other operation this one has no single new id, so the record keeps the whole array of new ids
        in the npz alongside the old ones. Undo only ever reads `old`, so older records replay unchanged."""
        self._check_z(z)
        labels = np.asarray(labels)
        where = np.asarray(where, bool)
        if labels.shape != self.shape_zyx[1:] or where.shape != labels.shape:
            raise ValueError("labels/where shape does not match the displayed slice")
        with self.lock:
            if not self.has_seg:
                raise ValueError("block has no seg.npy")
            limits = np.iinfo(self._seg_ro.dtype)
            if labels.size and (labels.min() < 0 or labels.max() > limits.max):
                raise ValueError("label id is outside the segmentation dtype range")
            plane = self._plane_ro(z)
            changed = where & (labels != plane)
            xs, ys = self._disk_idx(changed)
            if not xs.size:
                return None
            seg = self._seg_writable()
            old = seg[xs, ys, z].copy()
            new = labels.T[xs, ys].astype(seg.dtype)      # labels are in the displayed frame
            rec = self._record("repair", z, xs, ys, old, new, metadata)
            seg[xs, ys, z] = new
            seg.flush()
            self._invalidate(z)
            return rec

    def fill(self, z: int, x: int, y: int, new_id: int, whole_slice: bool = False) -> dict | None:
        """Bucket fill: relabel the connected component of the clicked pixel (4-connectivity within the slice) to
        `new_id`; with whole_slice, every pixel of that id in the slice. Returns the edit record, or None if the
        clicked pixel already has new_id."""
        self._check_z(z)
        with self.lock:
            seg = self._seg_writable()
            plane = self._plane(z)  # displayed-orientation view on the memmap: writes go straight to disk
            old = int(plane[y, x])
            if old == new_id:
                return None
            mask = plane == old
            if not whole_slice:
                lab, _ = ndimage.label(mask)
                mask = lab == lab[y, x]
            xs, ys = self._disk_idx(mask)
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
            plane = self._plane(z)
            H, W = plane.shape
            mask = np.zeros(plane.shape, dtype=bool)
            yy, xx = np.ogrid[-radius:radius + 1, -radius:radius + 1]
            disc = (xx * xx + yy * yy) <= radius * radius
            pts = [points[0]]
            for (x0, y0), (x1, y1) in zip(points, points[1:]):  # bridge gaps between sampled mouse positions
                n = int(max(abs(x1 - x0), abs(y1 - y0)))
                pts.extend((int(round(x0 + (x1 - x0) * t / n)), int(round(y0 + (y1 - y0) * t / n))) for t in range(1, n + 1))
            for x, y in pts:  # x = column, y = row
                c0, c1 = max(0, x - radius), min(W, x + radius + 1)
                r0, r1 = max(0, y - radius), min(H, y + radius + 1)
                if c1 <= c0 or r1 <= r0:
                    continue
                mask[r0:r1, c0:c1] |= disc[r0 - (y - radius):r1 - (y - radius), c0 - (x - radius):c1 - (x - radius)]
            mask &= plane != new_id
            xs, ys = self._disk_idx(mask)
            if xs.size == 0:
                return None
            old = seg[xs, ys, z].copy()
            plane[mask] = new_id
            seg.flush()
            self._invalidate(z)
            return self._record("paint", z, xs, ys, old, new_id, {"radius": radius, "n_points": len(points)})

    def merge_pair(self, z: int, first: tuple[int, int], second: tuple[int, int]) -> dict | None:
        """Relabel only the second clicked 4-connected region using the first's id.

        Both ids are read under the edit lock, so stale client-side label tables
        cannot choose the wrong keeper. Other islands and slices stay unchanged.
        """
        self._check_z(z)
        _, H, W = self.shape_zyx
        if any(not (0 <= x < W and 0 <= y < H) for x, y in (first, second)):
            raise ValueError("outside the block")
        if not self.has_seg:
            raise ValueError("block has no segmentation")
        with self.lock:
            plane = self._plane_ro(z)
            (fx, fy), (sx, sy) = first, second
            to_id, from_id = int(plane[fy, fx]), int(plane[sy, sx])
            if not to_id or not from_id:
                raise ValueError("请选择两个非背景色块")
            if from_id == to_id:
                return None
            components, _ = ndimage.label(plane == from_id)
            xs, ys = self._disk_idx(components == components[sy, sx])
            seg = self._seg_writable()
            seg[xs, ys, z] = to_id
            seg.flush()
            self._invalidate(z)
            return self._record("merge", z, xs, ys, from_id, to_id,
                                {"scope": "component", "first": list(first), "second": list(second)})

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
            (self.work / EDIT_LOG).write_text("".join(json.dumps(r) + "\n" for r in log[:-1]))
            for k in np.unique(zs):
                self._invalidate(int(k))
            # deliberately NOT resetting _max_id: recomputing it rescans the whole volume (1.2 s on a 1024²x100
            # block) and it is only ever used to hand out an unused id. Staying high is safe — ids just skip —
            # and it also guarantees an undone id is never handed out again while its edit record still exists.
            return rec


class AnnotateStore:
    def __init__(self, root: Path | None, workdir: Path | None = None, extra_roots: list[Path] | None = None):
        self.root = Path(root) if root else None
        self.roots = [r for r in [self.root, *(Path(x) for x in (extra_roots or []))] if r]
        self.workdir = Path(workdir) if workdir else None
        self._blocks: dict[str, Block] = {}
        self._lock = threading.Lock()

    def refresh(self) -> list[dict]:
        found = [p for r in self.roots for p in find_blocks(r)]
        with self._lock:
            for p in found:
                if p.name not in self._blocks:
                    try:
                        self._blocks[p.name] = Block(p, self.workdir)
                    except Exception as e:  # a half-copied block must not take the page down
                        self._blocks[p.name] = e  # type: ignore[assignment]
            return [self._summary(k) for k in sorted(self._blocks)]

    def _summary(self, key: str) -> dict:
        b = self._blocks[key]
        if isinstance(b, Exception):
            return {"block_id": key, "error": str(b)}
        z, y, x = b.shape_zyx
        return {"block_id": b.id, "has_seg": b.has_seg, "nz": z, "height": y, "width": x, "dataset": b.meta.get("dataset", {}).get("id"),
                "n_edits": len(b.edits()), "has_working_copy": (b.work / SEG_EDIT).exists(), "path": str(b.path),
                "em_source": "visual/slices_em" if b.visual_em else "em.npy", "voxel_size_nm": b.meta.get("geometry", {}).get("voxel_size_nm")}

    def get(self, block_id: str) -> Block:
        if block_id not in self._blocks:
            self.refresh()
        b = self._blocks.get(block_id)
        if b is None:
            raise KeyError(block_id)
        if isinstance(b, Exception):
            raise ValueError(str(b))
        return b
