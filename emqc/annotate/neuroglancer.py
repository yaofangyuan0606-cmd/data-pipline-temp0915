"""Open a pixel of a block in the public Neuroglancer viewer, to see in 3D what a flat section cannot show.

The annotation page shows one section at a time. When a dark blob has no label, the question "is this an organelle
inside a cell, or a cell of its own, or a slice artefact" is often unanswerable from that one section — but obvious
in 3D. The H01 release is already served by a public Neuroglancer at h01-dot-neuroglancer-demo.appspot.com, so the
answer is one link away: translate the clicked pixel into the dataset's own coordinates and hand the viewer a state
that centres on it and, when the pixel carries a label, selects that segment so its mesh is rendered.

Coordinates, and the axis swap that is easy to get wrong. `meta.json` records:

    geometry.origin          {"x": 355846, "y": 68275, "z": 1225, "unit": "mip1 voxel"}
    geometry.voxel_size_nm   [8, 8, 33]
    geometry.offset_in_parent {"y0": 512, "x0": 512}      # quadrant blocks only

Neuroglancer's default dimensions for H01 are exactly 8 / 8 / 33 nm, so no unit conversion is needed. But the axes
do NOT line up with the screen. `fetch_train.py` saved what CloudVolume handed it, which is (x, y, z) in the source
volume's own order, so the array's axis 0 is the volume's X and axis 1 its Y. The annotation page now draws a
section transposed, X horizontal and Y vertical, exactly as the viewer does, so screen and volume line up:

    screen x (columns) -> the volume's X        screen y (rows) -> its Y

    position = [origin.x + offset.y0 + screen_x,  origin.y + offset.x0 + screen_y,  origin.z + z]

Note that `offset_in_parent.y0`, the offset along axis 0, pairs with `origin.x` — the names in meta.json follow the
array, not the volume. This was verified, not assumed: all four quadrant blocks were cross-correlated against the
volume downloaded with CloudVolume and every one matched at exactly 1.000 with zero displacement (512 px off under
the other assignment), and single pixels were then compared by value.

Blocks from another dataset (the mouse volumes) have no such origin, or a different source, and the link is simply
not offered — `link_for` returns None with a reason rather than guessing.
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
    """The clicked pixel (screen x = column, y = row) in the source volume's voxel grid, or None when the block does
    not say where it came from. The axes swap — see the module docstring."""
    g = meta.get("geometry") or {}
    o = g.get("origin")
    if not isinstance(o, dict) or not all(k in o for k in ("x", "y", "z")):
        return None
    off = g.get("offset_in_parent") or {}
    return [int(o["x"]) + int(off.get("y0", 0)) + int(x),      # the page now draws X horizontally
            int(o["y"]) + int(off.get("x0", 0)) + int(y),      # and Y vertically, like the viewer
            int(o["z"]) + int(z)]


def segment_is_public(meta: dict, segment: int | None) -> bool:
    """Whether this id means anything in the public c3 segmentation.

    Two kinds of id in this platform are NOT c3 ids and would select an unrelated cell in the viewer: the ids a
    derived block invents (SAM pre-fill starts numbering above the delivered maximum, recorded as
    `sam_merge.first_new_id`), and the ids the annotator creates with 新建标签, which are also above the original maximum. Both sit above
    the delivered maximum, so one threshold rules out both."""
    if not segment:
        return False
    first_new = ((meta.get("sam_merge") or {}).get("first_new_id"))
    if first_new is not None and int(segment) >= int(first_new):
        return False
    return "h01-release" in str((meta.get("dataset") or {}).get("seg_source") or "")


def _block_box(meta: dict, shape_zyx) -> dict | None:
    """The block's outline as an annotation, so the viewer shows which part of H01 this platform is working on."""
    if shape_zyx is None:
        return None
    nz, H, W = shape_zyx
    g = meta.get("geometry") or {}
    o = g.get("origin")
    if not isinstance(o, dict):
        return None
    off = g.get("offset_in_parent") or {}
    # W columns run along the volume's X, H rows along its Y
    x0, y0, z0 = int(o["x"]) + int(off.get("y0", 0)), int(o["y"]) + int(off.get("x0", 0)), int(o["z"])
    vx, vy, vz = g.get("voxel_size_nm") or H01_VOXEL_NM
    return {"type": "axis_aligned_bounding_box", "id": "block",
            "pointA": [x0, y0, z0], "pointB": [x0 + W, y0 + H, z0 + nz],
            "description": f"本数据块 {W}x{H}x{nz} @ {vx}x{vy}x{vz} nm"}


def _annotation_layer(annotations: list) -> dict:
    return {"type": "annotation", "source": "local://annotations", "name": "本数据块",
            "annotationColor": "#ffd60a", "tab": "annotations", "annotations": annotations}


def state_for(meta: dict, x: int, y: int, z: int, segment: int | None = None, zoom_nm: float = 4.0,
              shape_zyx=None, neighbours: list | None = None) -> dict | None:
    """The Neuroglancer state that centres on the pixel; None if this block cannot be located in H01."""
    pos = global_position(meta, x, y, z)
    if pos is None or not _dataset_is_h01(meta):
        return None
    vx, vy, vz = (meta.get("geometry") or {}).get("voxel_size_nm") or H01_VOXEL_NM
    # The subsources must be spelled out with mesh: true. With the short string form the 3D panel stays empty —
    # the meshes are a non-default subsource of this volume, so nothing is there to render.
    seg_layer = {
        "type": "segmentation",
        "source": {"url": H01_SEG,
                   "subsources": {"default": True, "bounds": True, "properties": True, "mesh": True},
                   "enableDefaultSubsources": False},
        "tab": "segments", "name": "c3",
    }
    if segment_is_public(meta, segment):
        seg_layer["segments"] = [str(int(segment))]          # ids are strings: H01 ids exceed 2^53
    elif neighbours:
        # Nothing to select at the click — which is the normal case for the dark blobs, they sit BETWEEN cells.
        # Render the cells around it instead: the blob then shows up in 3D as the gap they leave, which is the
        # answer to "what is this".
        seg_layer["segments"] = [str(int(n)) for n in neighbours if segment_is_public(meta, n)][:8]
    state = {
        "dimensions": {"x": [round(vx * 1e-9, 12), "m"], "y": [round(vy * 1e-9, 12), "m"], "z": [round(vz * 1e-9, 12), "m"]},
        "position": [p + 0.5 for p in pos],                  # centre of the voxel, not its corner
        "crossSectionScale": max(0.25, float(zoom_nm) / max(1.0, float(vx))),
        "projectionScale": 4096,
        "layers": [{"type": "image", "source": H01_EM, "tab": "source", "name": "4nm EM"}, seg_layer],
        # A big 3D panel beside the section, the way the official H01 gallery links are laid out. "4panel" also has
        # a 3D view but squeezes it into a quarter, which is easy to miss.
        "layout": {"type": "xy-3d", "orthographicProjection": True},
        "showSlices": False,
        "projectionDepth": -100,
    }
    marks = [{"type": "point", "id": "clicked", "point": [p + 0.5 for p in pos],
              "description": f"你看的位置 (x={x}, y={y}, z={z})"}]
    box = _block_box(meta, shape_zyx)
    if box:
        marks.append(box)
    state["layers"].append(_annotation_layer(marks))
    return state


def block_state(meta: dict, shape_zyx, z: int, screen_px: int = 700) -> dict | None:
    """A state framing the WHOLE block, with its outline drawn as an annotation.

    Point-jumping answers "what is this cell". It cannot answer "what is this dark blob that has no label at all" —
    there is nothing to select, and the blobs in this data sit between cells, not inside one. For that the annotator
    needs to arrive in the viewer with the block framed and its boundary visible, then look around and click things
    at full resolution themselves."""
    nz, H, W = shape_zyx
    g = meta.get("geometry") or {}
    o = g.get("origin")
    if not isinstance(o, dict) or not _dataset_is_h01(meta):
        return None
    # The block's own corner is computed once, in _block_box, which state_for calls; recomputing it here only
    # created a second copy of the x0/y0 pairing to get wrong.
    state = state_for(meta, W // 2, H // 2, int(z), None, shape_zyx=shape_zyx)
    if state is None:
        return None
    state["crossSectionScale"] = max(W, H) / float(screen_px)     # the whole block just fits a panel
    state["projectionScale"] = max(W, H) * 2.5
    state["layers"][-1]["annotations"] = [a for a in state["layers"][-1]["annotations"] if a["id"] != "clicked"]
    return state


def embed_state(meta: dict, shape_zyx, z: int, screen_px: int = 600, with_seg: bool = True) -> dict | None:
    """A state for EMBEDDING the public viewer beside our own before/after panes on the compare page.

    Same framing as block_state, but flat: a single xy cross-section (no 3D panel, no slice planes), the EM under
    the full c3 segmentation. Neuroglancer shows every segment when the layer's `segments` list is empty, which is
    exactly the "reference segmentation" picture wanted here; selectedAlpha keeps the EM readable through it."""
    state = block_state(meta, shape_zyx, z, screen_px)
    if state is None:
        return None
    state["layout"] = "xy"
    state.pop("projectionScale", None)
    state.pop("projectionDepth", None)
    state["showAxisLines"] = False
    state["showDefaultAnnotations"] = False
    # No top bars inside the embed: the xy panel then fills the whole iframe, so the page can map a voxel to an
    # iframe pixel exactly (centre = position, scale = crossSectionScale) and draw its own hover marker on top
    # instead of pushing a hashchange into the viewer for every mouse move (which was visibly laggy).
    state["showUIControls"] = False
    state["showPanelBorders"] = False
    for layer in state["layers"]:
        if layer.get("type") == "segmentation":
            layer["selectedAlpha"] = 0.45
            layer["notSelectedAlpha"] = 0
            layer.pop("segments", None)                     # empty selection = every segment rendered
    if not with_seg:                                        # the plain-EM pane: image + block outline only
        state["layers"] = [l for l in state["layers"] if l.get("type") != "segmentation"]
    return state


def link_for_embed(meta: dict, shape_zyx, z: int, screen_px: int = 600, with_seg: bool = True) -> dict:
    """{'url', 'center', ...} for an iframe framing this block at section z, or {'url': None, 'reason': ...}."""
    state = embed_state(meta, shape_zyx, z, screen_px, with_seg)
    if state is None:
        return {"url": None, "reason": "这个数据块没有 geometry.origin 或不是 H01 数据，公开查看器里没有对应的体数据"}
    box = next((a for a in state["layers"][-1]["annotations"] if a.get("id") == "block"), None)
    return {"url": VIEWER + "#!" + urllib.parse.quote(json.dumps(state, separators=(",", ":")), safe=""),
            "center": [int(p - 0.5) for p in state["position"]], "layout": state["layout"],
            # the block's corner in volume voxels: screen (x, y) on section z maps to corner + (x, y, z)
            "corner": list(box["pointA"]) if box else None}


def link_for(meta: dict, x: int, y: int, z: int, segment: int | None = None, zoom_nm: float = 4.0,
             shape_zyx=None, neighbours: list | None = None) -> dict:
    """{'url', 'position', 'segment', 'position_um'} for the viewer, or {'url': None, 'reason': ...}.

    `position_um` is where the pixel is, in micrometres from the volume's origin — not to be confused with
    `link_for_block`'s `size_um`, which is how big the block is."""
    state = state_for(meta, x, y, z, segment, zoom_nm, shape_zyx, neighbours)
    if state is None:
        pos = global_position(meta, x, y, z)
        reason = ("这个数据块的 meta.json 里没有 geometry.origin，无法定位到源数据集中的位置"
                  if pos is None else "这个数据块不是 H01 数据，公开的 Neuroglancer 里没有对应的体数据")
        return {"url": None, "reason": reason, "position": pos}
    pos = [int(p - 0.5) for p in state["position"]]
    vx, vy, vz = (meta.get("geometry") or {}).get("voxel_size_nm") or H01_VOXEL_NM
    public = segment_is_public(meta, segment)
    shown = [l for l in state["layers"] if l["type"] == "segmentation"][0].get("segments") or []
    return {
        "neighbours": None if public else (shown or None),
        "url": VIEWER + "#!" + urllib.parse.quote(json.dumps(state, separators=(",", ":")), safe=""),
        "position": pos,
        "segment": str(int(segment)) if public else None,
        "segment_note": (None if public else
                         ((f"这一点没有标签（黑团多在细胞之间），已改为选中周围 {len(shown)} 个细胞，"
                           "在 3D 里黑团就是它们之间的空隙" if shown else "这一点没有标签，查看器只定位") if not segment else
                          f"标签 {segment} 是本平台新建的 id，公开的 c3 分割里没有它，查看器只定位不选中细胞")),
        "position_um": [round(pos[0] * vx / 1000, 3), round(pos[1] * vy / 1000, 3), round(pos[2] * vz / 1000, 3)],
        "voxel_size_nm": [vx, vy, vz],
    }


def link_for_block(meta: dict, shape_zyx, z: int) -> dict:
    """{'url', 'bounds', 'size_um', ...} framing the whole block, or {'url': None, 'reason': ...}.

    `size_um` is the block's extent in micrometres — `link_for` reports a position under its own name."""
    state = block_state(meta, shape_zyx, z)
    if state is None:
        return {"url": None, "reason": "这个数据块没有 geometry.origin 或不是 H01 数据，无法在公开查看器里定位"}
    box = state["layers"][-1]["annotations"][0]
    g = meta.get("geometry") or {}
    vx, vy, vz = g.get("voxel_size_nm") or H01_VOXEL_NM
    return {
        "url": VIEWER + "#!" + urllib.parse.quote(json.dumps(state, separators=(",", ":")), safe=""),
        "bounds": {"from": box["pointA"], "to": box["pointB"]},
        "center": [int(p - 0.5) for p in state["position"]],
        "size_um": [round(shape_zyx[2] * vx / 1000, 2), round(shape_zyx[1] * vy / 1000, 2),
                    round(shape_zyx[0] * vz / 1000, 2)],
        "voxel_size_nm": [vx, vy, vz],
    }
