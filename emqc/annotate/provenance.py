"""Read-only comparisons and provenance of the current annotation, in display coordinates.

Provenance describes the last surviving write to each pixel, not human verification or
the original creator of a reused label id. Undo removes a record from the active log.
Missing evidence is reported as unknown; pristine labels are never assumed to be manual.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
from zipfile import BadZipFile

import numpy as np
from PIL import Image


SOURCES = ("baseline", "manual", "sam", "interpolation", "assisted", "unknown")
SOURCE_NAMES = ("原始标签", "人工编辑", "SAM", "插值修补", "智能填充（历史）", "来源不明")
SOURCE_COLORS = ("#89939f", "#f0a33a", "#35b9dd", "#aa83f5", "#4cbf8b", "#ed647a")
POLICY = "按当前像素最后一次有效写入统计；同一标签可混合来源。原始标签不代表人工真值，人工编辑不代表人工审核。撤销操作不计入。"


def source_for_edit(record: dict) -> str:
    kind = record.get("kind")
    if kind == "repair":
        return "interpolation"
    if kind == "sam":
        return "sam"
    # Retired tools remain identifiable when reading existing edit logs.
    if kind == "smartfill":
        return "assisted"
    if kind in {"paint", "fill", "merge", "split", "clear"}:
        return "manual"
    return "unknown"


def _read_arrays(block, z):
    before = (np.array(block._seg_ro[:, :, z].T) if block.has_seg
              else np.zeros(block.shape_zyx[1:], dtype=np.uint64))
    # Open read-only, without materialising/migrating a working copy or touching its mtime.
    path = block.work_path("seg_edit.npy")
    if block.has_seg and path.exists():
        try:
            volume = np.load(path, mmap_mode="r", allow_pickle=False)
        except (OSError, EOFError, ValueError) as e:
            raise ValueError("工作副本无法读取，不能可靠展示当前结果") from e
        if volume.shape != block.em.shape or volume.dtype != block._seg_ro.dtype:
            raise ValueError("工作副本的形状或标签类型与原始分割不一致")
        after = np.array(volume[:, :, z].T)
    else:
        after = before.copy()
    return before, after


def _sources(block, z, before, after, records):
    sources = np.zeros(before.shape, dtype=np.uint8)
    warnings = []
    if isinstance(block.meta.get("sam"), dict) and block.meta["sam"]:
        sources[before != 0] = SOURCES.index("sam")
    if "sam_merge" in block.meta:
        try:
            metadata = block.meta["sam_merge"]
            first = metadata["first_new_id"]
            if isinstance(first, bool) or not isinstance(first, (str, int)):
                raise ValueError("invalid first id")
            first = int(first)
            count = metadata.get("n_new_ids")
            if first <= 0 or first > np.iinfo(before.dtype).max:
                raise ValueError("invalid id range")
            from_sam = before >= first
            if count is not None:
                if type(count) is not int or count < 0:
                    raise ValueError("invalid id count")
                # Avoid an overflowing scalar when the last valid uint64 id was allocated.
                last = first + count - 1
                if last <= np.iinfo(before.dtype).max:
                    from_sam &= before <= last
            sources[from_sam] = SOURCES.index("sam")
        except (ValueError, TypeError, KeyError, OverflowError):
            sources[before != 0] = SOURCES.index("unknown")
            warnings.append("SAM 预填元数据无效，无法区分原始标签与预填标签。")
    sources[before != after] = SOURCES.index("unknown")
    seen = np.zeros(before.shape, dtype=bool)
    operations = []
    for rec in reversed(records):
        if rec.get("z") is not None and rec["z"] != z:
            continue
        try:
            n = int(rec["n"])
            if n < 1:
                raise ValueError("invalid edit number")
            with np.load(block.work_path(f"edits/{n:06d}.npz"), allow_pickle=False) as data:
                xs, ys = data["xs"], data["ys"]
                if "zs" in data.files:
                    zs = data["zs"]
                else:
                    section = data["z"]
                    if section.ndim or section.dtype.kind not in "iu":
                        raise ValueError("invalid legacy section")
                    zs = np.full(xs.shape, int(section))
                if xs.ndim != 1 or xs.shape != ys.shape or xs.shape != zs.shape:
                    raise ValueError("invalid coordinates")
                if any(a.dtype.kind not in "iu" for a in (xs, ys, zs)):
                    raise ValueError("non-integer coordinates")
                if np.any((xs < 0) | (xs >= before.shape[1]) | (ys < 0) | (ys >= before.shape[0])
                          | (zs < 0) | (zs >= block.nz)):
                    raise ValueError("coordinates out of bounds")
                in_slice = zs == z
                x, y = xs[in_slice].astype(np.intp), ys[in_slice].astype(np.intp)
                new = data["new"]
                if new.dtype.kind not in "iu" or (new.ndim and new.shape != xs.shape):
                    raise ValueError("invalid labels")
                expected = new[in_slice] if new.ndim else new
                matches = after[y, x] == expected
                # Historical cuts record one scalar new id plus the list of new piece ids.
                # Membership in that list cannot verify which id was written at a given pixel.
                ambiguous_cut = rec.get("kind") == "split" and not new.ndim and len(rec.get("new_ids", [])) > 1
                if ambiguous_cut:
                    matches = np.zeros(x.shape, dtype=bool)
            if not x.size:
                continue
            latest = ~seen[y, x]
            source = source_for_edit(rec)
            sources[y[latest], x[latest]] = SOURCES.index("unknown")
            confirmed = latest & matches
            sources[y[confirmed], x[confirmed]] = SOURCES.index(source)
            seen[y, x] = True
            if np.any(latest & ~matches):
                warnings.append(f"编辑 #{n} " + ("为旧切割记录，缺少逐像素新标签，对应像素记为来源不明。" if ambiguous_cut
                                              else "与当前标签不一致，对应像素记为来源不明。"))
            operations.append({"n": n, "kind": rec.get("kind"), "source": source, "z": rec.get("z"),
                               "ts": rec.get("ts") if isinstance(rec.get("ts"), str) else None, "n_px_in_slice": int(x.size),
                               "current_px": int(confirmed.sum()),
                               "model": rec.get("model") if isinstance(rec.get("model"), str) else None,
                               "source_sections": (rec["source_sections"] if isinstance(rec.get("source_sections"), list)
                                                   and all(type(k) is int and 0 <= k < block.nz for k in rec["source_sections"]) else None)})
        except (OSError, ValueError, KeyError, TypeError, OverflowError, EOFError, BadZipFile):
            # With unknown coordinates, any pixel not accounted for by a later write is uncertain.
            sources[~seen] = SOURCES.index("unknown")
            seen[:] = True
            warnings.append(f"编辑 #{rec.get('n', '?')} 的像素记录缺失或损坏，无法完整溯源。")
    return sources, list(reversed(operations)), warnings


def _snapshot(block, z):
    block._check_z(z)
    before, after = _read_arrays(block, z)
    try:
        records = block.edits()
        sources, operations, warnings = _sources(block, z, before, after, records)
    except (ValueError, OSError):
        sources = np.full(before.shape, SOURCES.index("unknown"), dtype=np.uint8)
        operations, warnings = [], ["编辑日志无法读取，当前切片记为来源不明。"]
    changed = before != after
    ids = np.union1d(before, after)
    before_counts = np.bincount(np.searchsorted(ids, before).ravel(), minlength=len(ids))
    after_idx = np.searchsorted(ids, after)
    counts = np.bincount((after_idx * len(SOURCES) + sources).ravel(),
                         minlength=len(ids) * len(SOURCES)).reshape(len(ids), len(SOURCES))
    changed_counts = np.bincount(after_idx[changed], minlength=len(ids))
    labels = [{"id": str(int(label)), "before_px": int(before_counts[i]),
               "current_px": int(counts[i].sum()), "changed_px": int(changed_counts[i]),
               "sources": {s: int(counts[i, k]) for k, s in enumerate(SOURCES)},
               "mixed": int(np.count_nonzero(counts[i])) > 1} for i, label in enumerate(ids)]
    summary = {s: {"pixels": int((sources == k).sum()),
                   "label_pixels": int(((sources == k) & (after != 0)).sum()),
                   "background_pixels": int(((sources == k) & (after == 0)).sum())}
               for k, s in enumerate(SOURCES)}
    revision = hashlib.sha256(before.tobytes() + after.tobytes() + sources.tobytes()).hexdigest()[:20]
    report = {"schema_version": 1, "block_id": block.id, "z": z, "scope": "slice", "revision": revision,
              "has_seg": block.has_seg, "shape_yx": list(before.shape), "policy": POLICY,
              "source_legend": [{"key": s, "name": SOURCE_NAMES[k], "color": SOURCE_COLORS[k]} for k, s in enumerate(SOURCES)],
              "total_px": int(before.size), "changed_px": int(changed.sum()),
              "added_px": int(((before == 0) & (after != 0)).sum()),
              "removed_px": int(((before != 0) & (after == 0)).sum()),
              "relabeled_px": int(((before != 0) & (after != 0) & changed).sum()),
              "sources": summary, "labels": labels, "operations": operations, "warnings": warnings}
    return report, before, after, sources, changed


def report(block, z: int | None = None) -> dict:
    with block.lock:
        if z is not None:
            return _snapshot(block, z)[0]
        # Slice at a time: never copy an entire uint64 volume into memory.
        combined, labels, slices, warnings = None, {}, [], set()
        for section in range(block.nz):
            part = _snapshot(block, section)[0]
            if combined is None:
                combined = {**part, "scope": "block", "z": None, "shape_zyx": list(block.shape_zyx)}
                for key in ("revision", "shape_yx", "operations"):
                    combined.pop(key)
                for key in ("total_px", "changed_px", "added_px", "removed_px", "relabeled_px"):
                    combined[key] = 0
                combined["sources"] = {s: dict.fromkeys(("pixels", "label_pixels", "background_pixels"), 0) for s in SOURCES}
            for key in ("total_px", "changed_px", "added_px", "removed_px", "relabeled_px"):
                combined[key] += part[key]
            for s in SOURCES:
                for key, value in part["sources"][s].items():
                    combined["sources"][s][key] += value
            for row in part["labels"]:
                dest = labels.setdefault(row["id"], {"id": row["id"], "before_px": 0, "current_px": 0,
                                                    "changed_px": 0, "sources": dict.fromkeys(SOURCES, 0)})
                for key in ("before_px", "current_px", "changed_px"):
                    dest[key] += row[key]
                for s in SOURCES:
                    dest["sources"][s] += row["sources"][s]
            slices.append({k: part[k] for k in ("z", "revision", "changed_px", "operations", "warnings")})
            warnings.update(part["warnings"])
        for row in labels.values():
            row["mixed"] = sum(v > 0 for v in row["sources"].values()) > 1
        combined.update(labels=sorted(labels.values(), key=lambda r: int(r["id"])), slices=slices, warnings=sorted(warnings))
        return combined


def _png_url(array):
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, format="PNG", compress_level=1)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _label_image(plane):
    ids, inverse = np.unique(plane, return_inverse=True)
    if len(ids) > 65536:
        raise ValueError("当前切片标签过多，无法生成对比索引图")
    idx = inverse.reshape(plane.shape).astype(np.uint16)
    rgb = np.zeros((*plane.shape, 3), dtype=np.uint8)
    rgb[..., 0], rgb[..., 1] = idx >> 8, idx & 255
    return {"png": _png_url(rgb), "ids": [str(int(i)) for i in ids]}


def changed_regions(changed, before, after, limit: int = 200) -> dict:
    """The connected patches of change, largest first — "which places changed", as a list you can walk.

    The highlight overlay answers this visually, but on a 512x512 section a few scattered brush strokes are easy to
    miss, and there is no way to step through them. Each region carries its own bounding box so the page can scroll
    both viewports onto it, and the label it went from / to (the most common one inside that patch, since a single
    stroke can clip a neighbour). Ids stay strings: H01 ids exceed 2^53 and JSON numbers would round them."""
    from scipy import ndimage

    lab, n = ndimage.label(changed, structure=np.ones((3, 3), bool))
    if not n:
        return {"regions": [], "n_total": 0, "hidden": 0, "hidden_px": 0}
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    order = [int(k) for k in np.argsort(sizes)[::-1].tolist() if sizes[k]][:limit]
    boxes = ndimage.find_objects(lab)
    regions = []
    for k in order:
        sl = boxes[k - 1]
        m = lab[sl] == k
        ys, xs = np.nonzero(m)
        def top(arr):
            v, c = np.unique(arr[sl][m], return_counts=True)
            return str(int(v[int(np.argmax(c))]))
        regions.append({"px": int(sizes[k]), "from_id": top(before), "to_id": top(after),
                        "x0": int(sl[1].start), "y0": int(sl[0].start),
                        "x1": int(sl[1].stop), "y1": int(sl[0].stop),
                        "cx": int(sl[1].start + xs.mean()), "cy": int(sl[0].start + ys.mean())})
    shown = sum(r["px"] for r in regions)
    return {"regions": regions, "n_total": int(n), "hidden": int(n - len(regions)),
            "hidden_px": int(int(sizes.sum()) - shown)}


def comparison_light(block, z: int) -> dict:
    """Images only, for the compare page's ±10 playback prefetch — no provenance walk, no label table, and no
    `before` image (playback never shows it).

    Everything but the change mask comes from the annotation store's per-section caches: `em_png` and
    `labels_png` are the same bytes the workbench serves (index map packed R = high byte, G = low byte, ids as
    strings), and `labels()` builds that index with a searchsorted that is ~6x faster than the
    `np.unique(return_inverse=True)` the full comparison uses. Those calls take the block lock themselves, so
    they are made outside our own `with block.lock` below."""
    block._check_z(z)
    em_png = block.em_png(z)                               # cached bytes
    if block.has_seg:
        labels_png = block.labels_png(z)
        idx_a, ids = block.labels(z)                       # current labels, cached index
        idx_b, ids_b = block.labels_baseline(z)            # delivered labels, cached index
        before, after = ids_b[idx_b], ids[idx_a]           # two 5 ms gathers instead of two 250 ms strided reads
    else:
        labels_png, ids = None, np.zeros(1, dtype=np.uint64)
        before = after = np.zeros(block.shape_zyx[1:], dtype=np.uint64)
    changed = before != after
    b64 = lambda raw: "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    report = {"schema_version": 1, "block_id": block.id, "z": z, "scope": "slice", "light": True,
              "has_seg": block.has_seg, "shape_yx": list(before.shape), "total_px": int(before.size),
              "changed_px": int(changed.sum()), "source_legend": [], "labels": [], "operations": [], "warnings": []}
    after_img = ({"png": b64(labels_png), "ids": [str(int(i)) for i in ids]} if block.has_seg
                 else _label_image(after))
    return {"report": report, "em_png": b64(em_png), "after": after_img,
            "changes_png": _png_url(changed.astype(np.uint8) * 255)}


def comparison(block, z: int) -> dict:
    # A single response binds both images and the report to the same edit state.
    with block.lock:
        data, before, after, sources, changed = _snapshot(block, z)
        return {"report": data, "em_png": _png_url(block.em_slice(z)),
                "before": _label_image(before), "after": _label_image(after),
                "sources_png": _png_url(sources), "changes_png": _png_url(changed.astype(np.uint8) * 255),
                "changes": changed_regions(changed, before, after)}


def csv_report(data):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(["block_id", "z", "label_id", "before_px", "current_px", "changed_px", *SOURCES, "mixed"])
    # Guard user-supplied names against spreadsheet formulas. Label ids remain exact decimal strings.
    block_id = data["block_id"]
    if block_id.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        block_id = "'" + block_id
    for row in data["labels"]:
        writer.writerow([block_id, data["z"] if data["z"] is not None else "all", row["id"], row["before_px"],
                         row["current_px"], row["changed_px"], *[row["sources"][s] for s in SOURCES], row["mixed"]])
    return "\ufeff" + out.getvalue()
