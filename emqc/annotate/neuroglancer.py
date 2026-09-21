"""Open a pixel of a block in the public Neuroglancer viewer, to see in 3D what a flat section cannot show.

The annotation page shows one section at a time. When a dark blob has no label, the question "is this an organelle
inside a cell, or a cell of its own, or a slice artefact" is often unanswerable from that one section — but obvious
in 3D. The H01 release is already served by a public Neuroglancer at h01-dot-neuroglancer-demo.appspot.com, so the
answer is one link away: translate the clicked pixel into the dataset's own coordinates and hand the viewer a state
that centres on it and, when the pixel carries a label, selects that segment so its mesh is rendered.

Coordinates. `meta.json` records the block's origin in the EM's own voxel grid together with the voxel size:

    geometry.origin          {"x": 355846, "y": 68275, "z": 1225, "unit": "mip1 voxel"}
    geometry.voxel_size_nm   [8, 8, 33]
    geometry.offset_in_parent {"y0": 512, "x0": 512}      # quadrant blocks only

Neuroglancer's own default dimensions for H01 are exactly 8 / 8 / 33 nm, so the block's pixel indices add to the
origin with no unit conversion at all:

    position = [origin.x + offset.x0 + x,  origin.y + offset.y0 + y,  origin.z + z]

Blocks from another dataset (the mouse volumes, a SAM-derived block) have no such origin, or a different source, and
the link is simply not offered — `link_for` returns None with a reason rather than guessing.
"""
from __future__ import annotations

import json
import urllib.parse

VIEWER = "https://h01-dot-neuroglancer-demo.appspot.com/"
# The public viewer reads these two directly from Google Storage; they are the same volumes meta.json names.
H01_EM = "precomputed://gs://h01-release/data/20210601/4nm_raw"
H01_SEG = "precomputed://gs://h01-release/data/20210601/c3"
H01_VOXEL_NM = [8, 8, 33]


def _dataset_is_h01(meta: dict) -> bool:
    d = meta.get("dataset") or {}
    src = str(d.get("em_source") or "") + " " + str(d.get("seg_source") or "")
    return "h01-release" in src or str(d.get("id", "")).startswith("h01")


def global_position(meta: dict, x: int, y: int, z: int) -> list[int] | None:
    """The clicked pixel in the source volume's voxel grid, or None when the block does not say where it came from."""
    g = meta.get("geometry") or {}
    o = g.get("origin")
    if not isinstance(o, dict) or not all(k in o for k in ("x", "y", "z")):
        return None
    off = g.get("offset_in_parent") or {}
    return [int(o["x"]) + int(off.get("x0", 0)) + int(x),
            int(o["y"]) + int(off.get("y0", 0)) + int(y),
            int(o["z"]) + int(z)]


def segment_is_public(meta: dict, segment: int | None) -> bool:
    """Whether this id means anything in the public c3 segmentation.

    Two kinds of id in this platform are NOT c3 ids and would select an unrelated cell in the viewer: the ids a
    derived block invents (SAM pre-fill starts numbering above the delivered maximum, recorded as
    `sam_merge.first_new_id`), and the ids the annotator creates with 新建 ID, which are also max + 1. Both sit above
    the delivered maximum, so one threshold rules out both."""
    if not segment:
        return False
    first_new = ((meta.get("sam_merge") or {}).get("first_new_id"))
    if first_new is not None and int(segment) >= int(first_new):
        return False
    return "h01-release" in str((meta.get("dataset") or {}).get("seg_source") or "")


def state_for(meta: dict, x: int, y: int, z: int, segment: int | None = None, zoom_nm: float = 4.0) -> dict | None:
    """The Neuroglancer state that centres on the pixel; None if this block cannot be located in H01."""
    pos = global_position(meta, x, y, z)
    if pos is None or not _dataset_is_h01(meta):
        return None
    vx, vy, vz = (meta.get("geometry") or {}).get("voxel_size_nm") or H01_VOXEL_NM
    seg_layer = {"type": "segmentation", "source": H01_SEG, "tab": "segments", "name": "c3"}
    if segment_is_public(meta, segment):
        seg_layer["segments"] = [str(int(segment))]          # ids are strings: H01 ids exceed 2^53
    return {
        "dimensions": {"x": [round(vx * 1e-9, 12), "m"], "y": [round(vy * 1e-9, 12), "m"], "z": [round(vz * 1e-9, 12), "m"]},
        "position": [p + 0.5 for p in pos],                  # centre of the voxel, not its corner
        "crossSectionScale": max(0.25, float(zoom_nm) / max(1.0, float(vx))),
        "projectionScale": 4096,
        "layers": [{"type": "image", "source": H01_EM, "tab": "source", "name": "4nm EM"}, seg_layer],
        "layout": "4panel",                                  # three orthogonal cuts + the 3D view
        "showSlices": True,
    }


def link_for(meta: dict, x: int, y: int, z: int, segment: int | None = None, zoom_nm: float = 4.0) -> dict:
    """{'url', 'position', 'segment', 'physical_um'} for the viewer, or {'url': None, 'reason': ...}."""
    state = state_for(meta, x, y, z, segment, zoom_nm)
    if state is None:
        pos = global_position(meta, x, y, z)
        reason = ("这个数据块的 meta.json 里没有 geometry.origin，无法定位到源数据集中的位置"
                  if pos is None else "这个数据块不是 H01 数据，公开的 Neuroglancer 里没有对应的体数据")
        return {"url": None, "reason": reason, "position": pos}
    pos = [int(p - 0.5) for p in state["position"]]
    vx, vy, vz = (meta.get("geometry") or {}).get("voxel_size_nm") or H01_VOXEL_NM
    public = segment_is_public(meta, segment)
    return {
        "url": VIEWER + "#!" + urllib.parse.quote(json.dumps(state, separators=(",", ":")), safe=""),
        "position": pos,
        "segment": str(int(segment)) if public else None,
        "segment_note": (None if public else
                         ("这一点没有标签，查看器只定位不选中细胞" if not segment else
                          f"标签 {segment} 是本平台新建的 id，公开的 c3 分割里没有它，查看器只定位不选中细胞")),
        "physical_um": [round(pos[0] * vx / 1000, 3), round(pos[1] * vy / 1000, 3), round(pos[2] * vz / 1000, 3)],
        "voxel_size_nm": [vx, vy, vz],
    }
