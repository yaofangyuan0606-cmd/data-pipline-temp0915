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
coordinates — the tools, SAM — is automatically right. Only the edit records go the other way:
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

import fcntl
import io
import json
import logging
import os
import shutil
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakValueDictionary

import numpy as np
from PIL import Image
from scipy import ndimage

SEG_EDIT = "seg_edit.npy"
EDIT_DIR = "edits"
EDIT_LOG = "edits.jsonl"
CREATED_LABELS = "created_labels.json"
# 操作流水，只追加：每一笔写入和每一次撤销，带标注人。edits.jsonl 是"当前有效"的记录，被撤销的会从里面消失，
# 而"谁在几点撤销了谁的改动"正是追溯时要问的，所以另记一份。
AUDIT_LOG = "audit.jsonl"
WORKDIR_LOCK = ".server.lock"
MAX_LABELS_PER_SLICE = 65535
_BLOCK_LOCKS = WeakValueDictionary()
_BLOCK_LOCKS_GUARD = threading.Lock()
_HELD_WORKDIRS: dict[str, object] = {}
_HELD_GUARD = threading.Lock()
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Actor:
    """谁在操作。`name` 是给人看的（记录里的 by）；登录用户另带账号名和用户 id，改名后旧记录仍能对上人。
    没有登录系统时（EMQC_AUTH_DISABLED）只有 name——页面里填的名字。"""
    name: str
    user: str | None = None
    id: int | None = None

    @classmethod
    def coerce(cls, by) -> "Actor | None":
        if by is None or isinstance(by, Actor):
            return by
        return cls(str(by))

    def stamp(self) -> dict:
        d: dict = {"by": self.name}
        if self.user is not None:
            d["by_user"] = self.user
        if self.id is not None:
            d["by_id"] = self.id
        return d


def same_actor(entry: dict, actor: Actor | None) -> bool:
    """这条记录（edits.jsonl 或 audit.jsonl 里的一行）是不是 actor 做的。有用户 id 就比 id，其次比账号名，最后比显示名。"""
    if actor is None:
        return False
    if entry.get("by_id") is not None and actor.id is not None:
        return entry["by_id"] == actor.id
    if isinstance(entry.get("by_user"), str) and actor.user is not None:
        return entry["by_user"] == actor.user
    return entry.get("by") == actor.name


class UndoForbidden(ValueError):
    """本片最近一次改动是别人做的：撤销要么撤自己的，要么明确说"我就是要撤他的"（force）。"""

    def __init__(self, record: dict):
        super().__init__(f"本片最近一次改动是 {record.get('by')} 做的，不能撤销别人的改动")
        self.record = record


class UndoMismatch(ValueError):
    """撤销时钉住的记录号已经不是本片最近一笔：中间有人又改了，界面上确认的那一笔不是现在会被撤掉的那一笔。"""

    def __init__(self, record: dict, expected: int):
        super().__init__(f"本片最近一笔已是 #{record.get('n')}（不是确认时的 #{expected}），请刷新后再撤销")
        self.record = record


def hold_workdir(root: Path) -> None:
    """把工作目录（所有块的 seg_edit / edits 都在它下面）占住，直到本进程退出。

    两个服务进程写同一个工作目录会怎样：edits.jsonl 的追加互相穿插、撤销时整文件重写把对方刚写的行冲掉、
    各自的缓存又都看不见对方的改动——悄无声息地坏。flock 把这变成第一次写入时的一句明确报错。
    进程退出（包括崩溃）时内核自动释放，不会留下死锁文件。不支持 flock 的文件系统上退回到只靠进程内的锁。"""
    key = str(root.resolve())
    with _HELD_GUARD:
        if key in _HELD_WORKDIRS:
            return
        root.mkdir(parents=True, exist_ok=True)
        fh = open(root / WORKDIR_LOCK, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise ValueError(f"标注工作目录 {root} 正被另一个服务进程使用。同一个工作目录只能由一个服务写入："
                             "多人标注请都连到那个服务；确实要再起一个服务，就给它另一个 EMQC_ANNOTATE_WORKDIR")
        except OSError:
            fh.close()
            return
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        _HELD_WORKDIRS[key] = fh


def label_color(label: int) -> tuple[int, int, int]:
    """Match the browser's FNV-1a/HSL palette, including Math.round rounding."""
    h = 2166136261
    for c in str(label):
        h = ((h ^ ord(c)) * 16777619) & 0xffffffff
    hue, sat, light = h % 360, .62 + ((h >> 9) % 30) / 100, .48 + ((h >> 17) % 16) / 100
    a = sat * min(light, 1 - light)
    def channel(n):
        k = (n + hue / 30) % 12
        return int(255 * (light - a * max(-1, min(k - 3, 9 - k, 1))) + .5)
    return tuple(channel(n) for n in (0, 8, 4))


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
    def __init__(self, path: Path, workdir: Path | None = None, *, read_only: bool = False):
        self.path = Path(path)
        self.id = self.path.name
        self.work = Path(workdir) / self.id if workdir else self.path  # None only for legacy callers/tests
        self.read_only = read_only
        # Comparison and editing stores share a lock, even when they use separate read-only handles.
        key = (self.path.resolve(), self.work.resolve())
        with _BLOCK_LOCKS_GUARD:
            self.lock = _BLOCK_LOCKS.setdefault(key, threading.RLock())
        self.em = np.load(self.path / "em.npy", mmap_mode="r")  # (rows, cols, z)
        if self.em.ndim != 3:
            raise ValueError(f"{self.path}/em.npy must be 3-D, got {self.em.shape}")
        self.has_seg = (self.path / "seg.npy").exists()
        self._seg_ro = np.load(self.path / "seg.npy", mmap_mode="r") if self.has_seg else None
        self._seg_rw = None
        self.meta = json.load(open(self.path / "meta.json")) if (self.path / "meta.json").exists() else {}
        self.visual_em = self._find_visual_em()
        if not read_only:
            with self.lock:
                self._migrate_legacy()
        self._max_id: int | None = None
        self._audit: list[dict] | None = None   # audit.jsonl 的内存副本，只有写入方缓存（见 audit_entries）
        self._png_cache: OrderedDict[tuple, bytes] = OrderedDict()
        self._label_cache: OrderedDict[int, tuple[np.ndarray, np.ndarray]] = OrderedDict()
        # same thing for the DELIVERED labels: never invalidated (seg.npy is read-only), used by the compare page
        # to rebuild a baseline plane with a 5 ms gather instead of a 250 ms strided read of the whole file
        self._label_cache_ro: OrderedDict[int, tuple[np.ndarray, np.ndarray]] = OrderedDict()

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
            "n_edits": len(log := self.edits()), "editors": [r["by"] for r in self.editors(records=log)],
            "last_edit": self.last_edit(log), "has_working_copy": (self.work / SEG_EDIT).exists(),
            "working_copy": str(self.work / SEG_EDIT), "workdir": str(self.work), "em_source": "em.npy",
            "em_version": "3-transposed",  # bump when the EM rendering changes; the viewer keys its image URLs on it
        }

    # ------------------------------------------------------------------ label volume access
    def work_path(self, name: str | Path) -> Path:
        """Comparison can read legacy work in place; it must never migrate or copy it."""
        target = self.work / name
        if self.read_only and not target.exists() and (self.path / name).exists():
            return self.path / name
        return target

    def _seg(self) -> np.ndarray:
        """Read view: the working copy when it exists, else the pristine seg.npy."""
        if self._seg_rw is None and self.work_path(SEG_EDIT).exists():
            self._seg_rw = np.load(self.work_path(SEG_EDIT), mmap_mode="r" if self.read_only else "r+")
        return self._seg_rw if self._seg_rw is not None else self._seg_ro

    def _hold(self) -> None:
        # 旧式无工作目录的调用者直接写在数据目录旁边——那里一个锁文件都不该留
        if self.work != self.path:
            hold_workdir(self.work.parent)
        # 先把审计流水读进来：它要是坏了，就在写任何像素之前失败，而不是像素和 edits.jsonl 都写完了才在追加流水时抛错
        self.audit_entries()

    def _seg_writable(self) -> np.ndarray:
        """Copy-on-write: materialise seg_edit.npy from seg.npy on first edit; seg.npy is never modified."""
        if self.read_only:
            raise ValueError("当前数据块以只读方式打开")
        if not self.has_seg:
            raise ValueError("block has no seg.npy")
        self._hold()
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

    LABEL_CACHE_SIZE = 48          # the compare page's ±10 playback holds 21 sections of both caches at once

    @staticmethod
    def _index(s: np.ndarray, z: int) -> tuple[np.ndarray, np.ndarray]:
        """(idx16, ids) for one displayed plane: idx 0 is always id 0 / background. `ids[idx]` rebuilds the plane."""
        # `np.unique(..., return_inverse=True)` argsorts every pixel; we only need "value -> position in ids",
        # and searchsorted on the (already sorted) ids gives exactly that, 6x faster on a 1024² section.
        ids = np.unique(s)
        if ids.size == 0 or ids[0] != 0:
            ids = np.concatenate([np.zeros(1, dtype=ids.dtype), ids])
        if ids.size > MAX_LABELS_PER_SLICE:
            raise ValueError(f"slice {z} has {ids.size} labels; the uint16 index map holds at most {MAX_LABELS_PER_SLICE}")
        return np.searchsorted(ids, s).astype(np.uint16), ids

    def _labels_cached(self, cache: OrderedDict, z: int, plane) -> tuple[np.ndarray, np.ndarray]:
        with self.lock:
            if z in cache:
                cache.move_to_end(z)
                return cache[z]
        got = self._index(plane(), z)
        with self.lock:
            cache[z] = got
            while len(cache) > self.LABEL_CACHE_SIZE:
                cache.popitem(last=False)
        return got

    def labels(self, z: int) -> tuple[np.ndarray, np.ndarray]:
        """(idx16 (y, x), ids) of the CURRENT labels — idx 0 is always id 0 / background."""
        self._check_z(z)
        return self._labels_cached(self._label_cache, z, lambda: self.seg_slice(z))

    def labels_baseline(self, z: int) -> tuple[np.ndarray, np.ndarray]:
        """Same for the DELIVERED labels (seg.npy). Cached for good — the delivery never changes."""
        self._check_z(z)
        return self._labels_cached(self._label_cache_ro, z, lambda: np.ascontiguousarray(self._seg_ro[:, :, z].T))

    def warm_labels(self, z0: int, z1: int) -> dict:
        """Fill both label caches for sections z0..z1 in ONE pass over each volume.

        On disk the arrays are (X, Y, Z) with Z fastest, so a single section is a 1M-element gather 800 bytes
        apart that pages through the whole file (~250 ms on a 1024² block) — but 21 sections touch the same
        pages as one, so reading the slab once costs about the same as one section. That is the difference
        between the ±10 playback taking 10 s to start and taking 1 s."""
        z0, z1 = max(0, int(z0)), min(self.shape_zyx[0] - 1, int(z1))
        if z1 < z0:
            raise ValueError("z1 must be >= z0")
        todo_rw = [z for z in range(z0, z1 + 1) if z not in self._label_cache]
        todo_ro = [z for z in range(z0, z1 + 1) if z not in self._label_cache_ro] if self.has_seg else []
        started = time.perf_counter()
        for todo, vol, cache in ((todo_ro, (lambda: self._seg_ro), self._label_cache_ro),
                                 (todo_rw, (lambda: self._seg()), self._label_cache)):
            if not todo or not self.has_seg:
                continue
            lo, hi = min(todo), max(todo) + 1
            slab = np.ascontiguousarray(vol()[:, :, lo:hi])          # one pass over the pages
            for z in todo:
                got = self._index(np.ascontiguousarray(slab[:, :, z - lo].T), z)
                with self.lock:
                    cache[z] = got
                    cache.move_to_end(z)
                    while len(cache) > self.LABEL_CACHE_SIZE:
                        cache.popitem(last=False)
        return {"z0": z0, "z1": z1, "warmed_current": len(todo_rw), "warmed_baseline": len(todo_ro),
                "seconds": round(time.perf_counter() - started, 3)}

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
        return {"z": z, "n": int(ids.size), "ids": [str(int(i)) for i in ids], "counts": counts.tolist(),
                "created_ids": self.created_ids()}

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

    def created_ids(self) -> list[str]:
        with self.lock:
            path = self.work_path(CREATED_LABELS)
            return json.loads(path.read_text()) if path.exists() else []

    def new_id(self, z: int = 0) -> int:
        self._hold()                     # created_labels.json 也是工作目录里的一笔写入
        """Reserve a reusable block label without changing pixels or edit history."""
        with self.lock:
            if self.read_only:
                raise ValueError("当前数据块以只读方式打开")
            self._check_z(z)
            created = self.created_ids()
            used = {label_color(int(i)) for i in self.labels(z)[1] if i != 0}
            used.update(label_color(int(i)) for i in created)
            label = max(self.max_id(), max(map(int, created), default=0)) + 1
            limit = np.iinfo(self._seg_ro.dtype).max
            while label <= limit and label_color(label) in used:
                label += 1
            if label > limit:
                raise ValueError("标签编号已用尽")
            self.work.mkdir(parents=True, exist_ok=True)
            tmp = self.work / (CREATED_LABELS + ".part")
            tmp.write_text(json.dumps([*created, str(label)]))
            tmp.replace(self.work / CREATED_LABELS)
            self._max_id = label
            return label

    # ------------------------------------------------------------------ edits
    def _edit_dir(self) -> Path:
        if self.read_only:
            raise ValueError("当前数据块以只读方式打开")
        self._hold()
        d = self.work / EDIT_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _edit_in_slice(self, rec: dict, z: int) -> dict | None:
        if rec.get("z") is not None:
            return rec if rec["z"] == z else None
        with np.load(self.work_path(f"{EDIT_DIR}/{rec['n']:06d}.npz"), allow_pickle=False) as data:
            zs = data["zs"] if "zs" in data.files else np.full(data["xs"].shape, int(data["z"]))
            n_px = int(np.count_nonzero(zs == z))
        return {**rec, "z": z, "n_px": n_px, "n_slices": 1} if n_px else None

    def edits(self, z: int | None = None) -> list[dict]:
        if z is not None:
            self._check_z(z)
            with self.lock:
                return [entry for rec in self.edits() if (entry := self._edit_in_slice(rec, z)) is not None]
        p = self.work_path(EDIT_LOG)
        if not p.exists():
            return []
        records = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
        previous = 0
        for rec in records:
            if (not isinstance(rec, dict) or type(rec.get("n")) is not int or rec["n"] <= previous
                    or ("kind" in rec and not isinstance(rec["kind"], str))
                    or (rec.get("z") is not None and (type(rec["z"]) is not int or not 0 <= rec["z"] < self.nz))):
                raise ValueError("编辑日志格式损坏，无法可靠读取操作顺序或切片坐标")
            previous = rec["n"]
        return records

    def edit_mask_png(self, n: int, z: int) -> bytes:
        """Read an active edit's footprint in display coordinates, including erased pixels."""
        self._check_z(z)
        with self.lock:
            rec = next((rec for rec in self.edits() if rec["n"] == n), None)
            if rec is None or (rec.get("z") is not None and rec["z"] != z):
                raise KeyError("本片没有这笔改动，可能已撤销")
            with np.load(self.work_path(f"{EDIT_DIR}/{n:06d}.npz"), allow_pickle=False) as data:
                xs, ys = data["xs"].astype(np.intp), data["ys"].astype(np.intp)
                zs = data["zs"] if "zs" in data.files else np.full(xs.shape, int(data["z"]))
                if xs.shape != ys.shape or xs.shape != zs.shape or xs.ndim != 1:
                    raise ValueError("改动像素记录格式无效")
                xs, ys = xs[zs == z], ys[zs == z]
            if not xs.size:
                raise KeyError("本片没有这笔改动")
            h, w = self.shape_zyx[1:]
            if np.any((xs < 0) | (xs >= w) | (ys < 0) | (ys >= h)):
                raise ValueError("改动像素超出本片范围")
            mask = np.zeros((h, w), dtype=bool)
            mask[ys, xs] = True
            edge = mask & ~ndimage.binary_erosion(mask)
            rgba = np.zeros((h, w, 4), dtype=np.uint8)
            rgba[mask] = [255, 214, 10, 100]
            rgba[edge] = [255, 214, 10, 235]
            return _png(Image.fromarray(rgba))

    def _record(self, kind: str, z: int | None, xs: np.ndarray, ys: np.ndarray, old, new_id: int, extra: dict | None = None,
                zs: np.ndarray | None = None, by: "str | Actor | None" = None) -> dict:
        """Persist one edit: every changed voxel (x, y, z) and the id it had before. `z` is the section shown in the
        UI (None for a 3-D edit spanning several sections); `zs` defaults to a constant z. `by` is who did it: the
        logged-in user (an Actor with account name and id), or just a display name when running without accounts."""
        actor = Actor.coerce(by)
        log = self.edits()
        n = (log[-1]["n"] + 1) if log else 1
        if zs is None:
            zs = np.full(xs.shape, int(z), dtype=np.uint16)
        np.savez_compressed(self._edit_dir() / f"{n:06d}.npz", z=-1 if z is None else int(z), xs=xs.astype(np.uint16), ys=ys.astype(np.uint16),
                            zs=zs.astype(np.uint16), old=np.asarray(old), new=np.asarray(new_id))
        many = np.ndim(new_id) > 0                  # historical edits may write a different id per pixel
        rec = {"n": n, "kind": kind, "z": (None if z is None else int(z)), "n_px": int(xs.size),
               "new_id": (f"{int(np.unique(new_id).size)} 个 id" if many else str(int(new_id))),
               "old_id": (str(int(old)) if np.ndim(old) == 0 else None), "n_slices": int(np.unique(zs).size),
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": None, **(actor.stamp() if actor else {}), **(extra or {})}
        from emqc.annotate.provenance import source_for_edit
        rec["source"] = source_for_edit(rec)
        rec["provenance_version"] = 1
        self.work.mkdir(parents=True, exist_ok=True)
        with open(self.work / EDIT_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self._audit_append({"action": "edit", "by": None, **(actor.stamp() if actor else {}), "n": n, "kind": kind, "z": rec["z"],
                            "n_px": rec["n_px"], "new_id": rec["new_id"], "n_slices": rec["n_slices"]})
        if self._max_id is not None:
            self._max_id = max(self._max_id, int(np.max(new_id)) if many else int(new_id))
        return rec

    # ------------------------------------------------------------------ 谁改的：审计流水、切片版本、改动人
    def _audit_append(self, entry: dict) -> dict:
        with self.lock:
            entries = self.audit_entries()
            entry = {"seq": (int(entries[-1]["seq"]) + 1) if entries else 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
            self.work.mkdir(parents=True, exist_ok=True)
            p = self.work / AUDIT_LOG
            # 上一次追加要是在半路断了（崩溃、磁盘满），文件末尾是半行：先把那半行切掉（audit_entries 读的时候已经
            # 忽略了它），再追加，免得它留在文件中间变成一行读不出来的垃圾
            if p.exists():
                data = p.read_bytes()
                if data and not data.endswith(b"\n"):
                    with open(p, "r+b") as f:
                        f.truncate(data.rfind(b"\n") + 1)
            with open(p, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            entries.append(entry)
            return entry

    def audit_entries(self) -> list[dict]:
        """每一次写入和撤销，从旧到新。写入方缓存在内存里（本进程是唯一的写入者，见 hold_workdir）；
        只读句柄（对比页）每次重读文件，才看得到工作台刚做的动作。

        末尾那一行要是残缺的（追加时崩溃），丢掉它继续——这是追加日志唯一合理的损坏方式；
        中间坏了则说明文件被人改过，拒绝读取，宁可停下也不给出错的"谁改的"。"""
        cached = self._audit
        if cached is not None:
            return cached
        with self.lock:
            if self._audit is not None:
                return self._audit
            p = self.work_path(AUDIT_LOG)
            entries: list[dict] = []
            if p.exists():
                lines = p.read_text().splitlines()
                seq = 0
                for i, line in enumerate(lines):
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        if i == len(lines) - 1:
                            log.warning("%s: 末尾一行残缺（上次追加中断），已忽略", p)
                            break
                        raise ValueError("审计日志格式损坏，无法可靠读取操作顺序")
                    if not isinstance(e, dict) or type(e.get("seq")) is not int or e["seq"] <= seq:
                        raise ValueError("审计日志格式损坏，无法可靠读取操作顺序")
                    seq = e["seq"]
                    entries.append(e)
            if not self.read_only:
                self._audit = entries
            return entries

    def slice_rev(self, z: int) -> int:
        """第 z 片的版本号：最后一次动过它的操作（写入或撤销）的流水号，没动过是 0。

        用它而不用改动次数，因为改动次数撤销后会退回去——A 看到的是 5，B 撤销一次变成 4，A 再来一笔又是 5，
        什么都察觉不到。流水号只增不减，客户端拿着它就能问"我看过之后有没有人动过这一片"。"""
        for e in reversed(self.audit_entries()):
            if e.get("z") is None or e.get("z") == z:
                return int(e["seq"])
        return 0

    def conflicts(self, z: int, since: int, by: "str | Actor | None") -> dict | None:
        """版本 since 之后，别人（不是 `by`）对第 z 片做的最近一次操作；没有则 None。

        两个人改同一片：各自对着自己加载时的画面下手。B 把一块区域改成了别的颜色，A 还对着旧画面点填充，
        填的就是 A 没看见的东西。服务端没法替两个人合并意图，只能拒绝（409），让页面重新加载这一片再来。
        自己的后续操作永远不算冲突：连续几笔涂抹到服务端的速度比页面重载快。"""
        actor = Actor.coerce(by)
        for e in reversed(self.audit_entries()):
            if int(e["seq"]) <= since:
                break
            if (e.get("z") is None or e.get("z") == z) and not same_actor(e, actor):
                return e
        return None

    def editors(self, z: int | None = None, records: list[dict] | None = None) -> list[dict]:
        """谁在第 z 片（不给 z 就是整块）上有仍然有效的改动：每人一行，最近改过的排前面。旧记录没名字的归为一行 by=None。"""
        rows: dict = {}
        latest: dict = {}
        for rec in (self.edits(z) if records is None else records):
            by = rec.get("by") if isinstance(rec.get("by"), str) else None
            r = rows.setdefault(by, {"by": by, "n": 0, "n_px": 0, "first": rec.get("ts"), "last": rec.get("ts")})
            r["n"] += 1
            r["n_px"] += int(rec.get("n_px") or 0)
            if isinstance(rec.get("ts"), str):
                r["last"] = rec["ts"]
            latest[by] = int(rec.get("n") or 0)             # record numbers order edits within the same second too
        return sorted(rows.values(), key=lambda r: -latest[r["by"]])

    def last_edit(self, records: list[dict] | None = None) -> dict | None:
        log = self.edits() if records is None else records
        if not log:
            return None
        return {k: log[-1].get(k) for k in ("n", "kind", "z", "n_px", "ts", "by")}

    def _check_id(self, new_id: int) -> int:
        new_id = int(new_id)
        if not 0 <= new_id <= np.iinfo(self._seg_ro.dtype).max:
            raise ValueError("label id is outside the segmentation dtype range")
        return new_id

    def apply_mask(self, z: int, mask: np.ndarray, new_id: int, metadata: dict, kind: str = "sam",
                   by: str | None = None) -> dict | None:
        """Apply a previewed mask (SAM) in display (y, x) coordinates; preserve exact undo."""
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
            rec = self._record(kind, z, xs, ys, old, new_id, metadata, by=by)
            seg[xs, ys, z] = new_id
            seg.flush()
            self._invalidate(z)
            return rec

    def clear_labels(self, z: int, ids, by: str | None = None) -> dict | None:
        """批量删除：把本片上这几个 id 的像素全部清为背景 0，记成一笔，可整笔撤销。只动第 z 片。"""
        self._check_z(z)
        ids = sorted({int(i) for i in ids} - {0})
        if not ids:
            raise ValueError("没有选中任何标签")
        with self.lock:
            seg = self._seg_writable()
            plane = self._plane(z)
            mask = np.isin(plane, np.asarray(ids, dtype=plane.dtype))
            xs, ys = self._disk_idx(mask)
            if not xs.size:
                return None
            old = seg[xs, ys, z].copy()
            plane[mask] = 0
            seg.flush()
            self._invalidate(z)
            return self._record("clear", z, xs, ys, old, 0, {"scope": "batch", "ids": [str(i) for i in ids], "n_ids": len(ids)}, by=by)

    def fill(self, z: int, x: int, y: int, new_id: int, whole_slice: bool = False, by: str | None = None) -> dict | None:
        """Bucket fill: relabel the connected component of the clicked pixel (4-connectivity within the slice) to
        `new_id`; with whole_slice, every pixel of that id in the slice. Returns the edit record, or None if the
        clicked pixel already has new_id."""
        self._check_z(z)
        new_id = self._check_id(new_id)
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
            return self._record("fill", z, xs, ys, old, new_id, {"x": int(x), "y": int(y), "whole_slice": bool(whole_slice)}, by=by)

    def paint(self, z: int, points: list[tuple[int, int]], radius: int, new_id: int, by: str | None = None,
              only_id: int | None = None) -> dict | None:
        """Brush: stamp a disc of `radius` at every point of the stroke (points are consecutive, so gaps are
        bridged by interpolation). Nonzero labels only fill background pixels; new_id=0 erases existing labels —
        all of them, or with `only_id` just that one label (擦除只擦当前颜色，压到邻居身上也不会把邻居擦掉)."""
        self._check_z(z)
        new_id = self._check_id(new_id)
        if only_id is not None:
            only_id = self._check_id(only_id)
            if only_id == 0:
                raise ValueError("要擦的颜色不能是背景")
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
            if new_id != 0:
                mask &= plane == 0               # 所有画笔颜色都只补空白；橡皮仍可擦除已有标签
            elif only_id is not None:
                mask &= plane == only_id         # 橡皮只擦当前颜色
            xs, ys = self._disk_idx(mask)
            if xs.size == 0:
                return None
            old = seg[xs, ys, z].copy()
            plane[mask] = new_id
            seg.flush()
            self._invalidate(z)
            extra = {"radius": radius, "n_points": len(points)}
            if only_id is not None:
                extra["only_id"] = str(only_id)
            return self._record("paint", z, xs, ys, old, new_id, extra, by=by)

    def refine_edge(self, z: int, x: int, y: int, sensitivity: float = 0.5, reach: int = 4, by=None) -> dict | None:
        """修缮边缘：点到的那块标签如果压过了黑色的膜、跨到邻居身上，就往里收——收到膜为止。

        只动最外层：跨过膜、大段贴着标签外面的部分才收；被细胞包着的暗色细胞器（线粒体等）和胞质一律留下。
        算法见 boundary.pull_back_to_membrane。只收缩、从不扩张；收掉的像素清成背景，留给邻居去填。
        返回改动记录；边缘本来就贴合时返回 None。"""
        from emqc.annotate.boundary import pull_back_to_membrane

        self._check_z(z)
        _, H, W = self.shape_zyx
        if not (0 <= x < W and 0 <= y < H):
            raise ValueError("outside the block")
        if not self.has_seg:
            raise ValueError("block has no segmentation")
        with self.lock:
            plane = self._plane_ro(z)
            label = int(plane[y, x])
            if label == 0:
                raise ValueError("这里是背景，没有可修缮的标签")
            comps, _ = ndimage.label(plane == label)
            mask = comps == comps[y, x]
            region = pull_back_to_membrane(mask, self.em_slice(z), int(x), int(y), float(sensitivity))
            removed = mask & ~region
            xs, ys = self._disk_idx(removed)
            if xs.size == 0:
                return None
            seg = self._seg_writable()
            old = seg[xs, ys, z].copy()
            seg[xs, ys, z] = 0
            seg.flush()
            self._invalidate(z)
            return self._record("refine", z, xs, ys, old, 0, {"x": int(x), "y": int(y), "label": str(label),
                                                            "sensitivity": float(sensitivity), "reach": int(reach)}, by=by)

    def merge_pair(self, z: int, first: tuple[int, int], second: tuple[int, int], by: str | None = None) -> dict | None:
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
                                {"scope": "component", "first": list(first), "second": list(second)}, by=by)

    def merge(self, from_id: int, to_id: int, scope: str = "block", z: int | None = None, by: str | None = None) -> dict | None:
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
            return self._record("merge", (int(z) if scope == "slice" else None), xs, ys, from_id, to_id, {"scope": scope}, zs=zs, by=by)

    def undo(self, z: int | None = None, by: "str | Actor | None" = None, force: bool = False,
             expect_n: int | None = None) -> dict | None:
        """Undo the latest edit in one slice, or the latest whole operation when z is omitted.

        A block-wide record is trimmed when only one of its slices is undone; its remaining
        voxels stay available for undo and provenance. Later edits on other slices are untouched.

        多人时撤销是最危险的一键：撤销永远撤本片**最近**的一笔，而最近的一笔可能是别人刚做的。所以给了名字（by）
        的撤销只能撤自己的；最近一笔是别人的就抛 UndoForbidden，让界面问清楚了再带 force 来。不给名字的调用
        （脚本）照旧。为什么不允许"跳过别人的那笔撤我自己上一笔"：那笔记录里存的是改动前的旧值，别人后来在同一些
        像素上写过的话，回填旧值会把他的改动一起冲掉。

        `expect_n` 把撤销钉在一条记录上：界面确认"撤销 李四 的 #7"之后到请求到达之间，本片可能又多了一笔；
        钉住了就不会撤错。
        """
        actor = Actor.coerce(by)
        if z is not None:
            self._check_z(z)
        with self.lock:
            log = self.edits()
            selected = None
            for i in range(len(log) - 1, -1, -1):
                entry = log[i] if z is None else self._edit_in_slice(log[i], z)
                if entry is not None:
                    selected = i, log[i], entry
                    break
            if selected is None:
                return None
            index, rec, undone = selected
            if expect_n is not None and int(rec["n"]) != int(expect_n):
                raise UndoMismatch(undone, int(expect_n))
            owner = rec.get("by") if isinstance(rec.get("by"), str) else None
            # 只拦"确知是别人的"：记录带账号（by_id / by_user）而且不是我。登录系统之前的旧记录只有个自报的名字，
            # 名字对不上多半是同一个人换了登录名（以前填"张三"，现在账号叫 y），登录用户撤它照旧放行；
            # 没开登录（actor 也只有名字）时仍按名字比。
            verified = rec.get("by_id") is not None or isinstance(rec.get("by_user"), str)
            if actor is not None and owner is not None and not same_actor(rec, actor) and not force and (verified or actor.id is None):
                raise UndoForbidden(undone)
            f = self._edit_dir() / f"{rec['n']:06d}.npz"
            with np.load(f, allow_pickle=False) as data:
                d = {key: data[key] for key in data.files}
            xs, ys = d["xs"].astype(np.intp), d["ys"].astype(np.intp)
            zs = d["zs"].astype(np.intp) if "zs" in d else np.full(xs.shape, int(d["z"]), dtype=np.intp)
            chosen = np.ones(xs.shape, dtype=bool) if z is None else zs == z
            remaining = ~chosen
            if remaining.any():
                rest = {key: (value[remaining] if key in {"xs", "ys", "zs", "old", "new"} and value.ndim else value)
                        for key, value in d.items()}
                partial = f.with_suffix(".npz.part")
                with partial.open("wb") as out:
                    np.savez_compressed(out, **rest)
                log[index] = {**rec, "n_px": int(remaining.sum()), "n_slices": int(np.unique(zs[remaining]).size)}
            else:
                log.pop(index)
            seg = self._seg_writable()
            for k in np.unique(zs[chosen]):
                m = chosen & (zs == k)
                seg[xs[m], ys[m], int(k)] = d["old"][m] if np.ndim(d["old"]) else d["old"]
            seg.flush()
            if remaining.any():
                partial.replace(f)
            else:
                f.unlink()
            log_path = self.work / (EDIT_LOG + ".part")
            log_path.write_text("".join(json.dumps(r) + "\n" for r in log))
            log_path.replace(self.work / EDIT_LOG)
            for k in np.unique(zs[chosen]):
                self._invalidate(int(k))
            self._audit_append({"action": "undo", "by": None, **(actor.stamp() if actor else {}), "n": int(rec["n"]),
                                "kind": rec.get("kind"), "z": (None if z is None else int(z)), "n_px": int(undone.get("n_px") or 0),
                                "of": owner, "of_user": rec.get("by_user"), "of_id": rec.get("by_id"),
                                "forced": bool(force and owner is not None and not same_actor(rec, actor))})
            # deliberately NOT resetting _max_id: recomputing it rescans the whole volume (1.2 s on a 1024²x100
            # block) and it is only ever used to hand out an unused id. Staying high is safe — ids just skip —
            # and it also guarantees an undone id is never handed out again while its edit record still exists.
            return undone


    def undo_all(self, z: int, by: "str | Actor | None" = None, force: bool = False) -> dict:
        """撤销本片全部有效改动——回到这一片标注前的样子。从最近一笔往前逐笔撤，直到本片没有记录；每一笔照样
        记入审计流水。权限同 undo：本片有别人的记录而没带 force，一笔都不动，直接抛 UndoForbidden（带那一笔）。"""
        self._check_z(z)
        actor = Actor.coerce(by)
        with self.lock:
            if actor is not None and not force:
                for rec in self.edits(z):
                    owner = rec.get("by") if isinstance(rec.get("by"), str) else None
                    verified = rec.get("by_id") is not None or isinstance(rec.get("by_user"), str)
                    if owner is not None and not same_actor(rec, actor) and (verified or actor.id is None):
                        raise UndoForbidden(rec)
            n = px = 0
            while True:
                undone = self.undo(z, by=actor, force=force)
                if undone is None:
                    break
                n += 1
                px += int(undone.get("n_px") or 0)
            return {"n": n, "n_px": px, "z": int(z)}


class AnnotateStore:
    def __init__(self, root: Path | None, workdir: Path | None = None, extra_roots: list[Path] | None = None, *, read_only: bool = False):
        self.root = Path(root) if root else None
        self.roots = [r for r in [self.root, *(Path(x) for x in (extra_roots or []))] if r]
        self.workdir = Path(workdir) if workdir else None
        self.read_only = read_only
        self._blocks: dict[str, Block] = {}
        self._lock = threading.Lock()

    def refresh(self) -> list[dict]:
        found = [p for r in self.roots for p in find_blocks(r)]
        with self._lock:
            for p in found:
                if p.name not in self._blocks:
                    try:
                        self._blocks[p.name] = Block(p, self.workdir, read_only=self.read_only)
                    except Exception as e:  # a half-copied block must not take the page down
                        self._blocks[p.name] = e  # type: ignore[assignment]
            return [self._summary(k) for k in sorted(self._blocks)]

    def _summary(self, key: str) -> dict:
        b = self._blocks[key]
        if isinstance(b, Exception):
            return {"block_id": key, "error": str(b)}
        z, y, x = b.shape_zyx
        try:
            log = b.edits()
            n_edits, history_error = len(log), None
            editors, last = [r["by"] for r in b.editors(records=log)], b.last_edit(log)
        except (OSError, ValueError) as e:
            n_edits, history_error, editors, last = None, str(e), [], None
        return {"block_id": b.id, "has_seg": b.has_seg, "nz": z, "height": y, "width": x, "dataset": b.meta.get("dataset", {}).get("id"),
                "n_edits": n_edits, "history_error": history_error, "editors": editors, "last_edit": last,
                "has_working_copy": b.work_path(SEG_EDIT).exists(), "path": str(b.path),
                # always em.npy: since sections are displayed transposed, the delivery's own PNGs can no longer be
                # served verbatim, so visual/slices_em is not an EM source any more (see em_png)
                "em_source": "em.npy", "voxel_size_nm": b.meta.get("geometry", {}).get("voxel_size_nm")}

    def get(self, block_id: str) -> Block:
        if block_id not in self._blocks:
            self.refresh()
        b = self._blocks.get(block_id)
        if b is None:
            raise KeyError(block_id)
        if isinstance(b, Exception):
            raise ValueError(str(b))
        return b
