"""Annotation API behind the VAST-style slice viewer: per-slice images, label index maps, pick / fill / paint / undo."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from typing import Literal

from emqc.annotate.store import AnnotateStore
from emqc.config import settings

router = APIRouter(prefix="/api/v1/annotate", tags=["annotation"])
_store: AnnotateStore | None = None
_comparison_store: AnnotateStore | None = None


def _roots() -> list:
    extra = [Path(x.strip()).resolve() for x in settings.annotate_extra_roots.split(";") if x.strip()]
    if settings.sam_blocks_dir and Path(settings.sam_blocks_dir).exists():
        extra.append(Path(settings.sam_blocks_dir).resolve())
    return extra


def get_store(*, read_only: bool = False) -> AnnotateStore:
    global _store, _comparison_store
    store = _comparison_store if read_only else _store
    root = settings.annotate_root.resolve() if settings.annotate_root else None
    if (store is None or store.roots != [r for r in [root, *_roots()] if r]
            or store.workdir != settings.annotate_workdir):
        store = AnnotateStore(root, settings.annotate_workdir, _roots(), read_only=read_only)
        if read_only:
            _comparison_store = store
        else:
            _store = store
    return store


def reset_store() -> None:
    global _store, _comparison_store
    _store = None
    _comparison_store = None


def _block(block_id: str, *, read_only: bool = False):
    try:
        return (get_store(read_only=True) if read_only else get_store()).get(block_id)
    except KeyError:
        raise HTTPException(404, f"unknown block {block_id}")
    except ValueError as e:
        raise HTTPException(422, str(e))


def _int_id(v: str | int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise HTTPException(422, f"bad label id {v!r}")


@router.get("/blocks")
def list_blocks():
    st = get_store()
    return {"root": str(st.root) if st.root else None, "blocks": st.refresh()}


@router.get("/comparison-blocks")
def comparison_blocks():
    st = get_store(read_only=True)
    return {"root": str(st.root) if st.root else None, "blocks": st.refresh()}


class SAMPredictIn(BaseModel):
    z: int = Field(ge=0)
    points: list[tuple[int, int]] = Field(default_factory=list, max_length=64)
    labels: list[Literal[0, 1]] = Field(default_factory=list, max_length=64)
    box: tuple[int, int, int, int] | None = None
    only_background: bool = True
    candidate: int | None = Field(default=None, ge=0, le=2)
    snap_boundary: bool = False                      # 贴合膜边界: post-process the mask with emqc.annotate.boundary
    boundary_sensitivity: float = Field(default=0.5, ge=0.0, le=1.0)


class SAMApplyIn(BaseModel):
    token: str = Field(min_length=32, max_length=32)
    new_id: str | int


@router.get("/sam/status")
def sam_status():
    from emqc.annotate.sam import service
    return service.status()


@router.post("/blocks/{block_id}/sam/predict")
def sam_predict(block_id: str, body: SAMPredictIn):
    from emqc.annotate.sam import SAMUnavailable, service
    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if body.z >= Z or len(body.points) != len(body.labels):
        raise HTTPException(422, "invalid z or mismatched point labels")
    if not body.points and body.box is None:
        raise HTTPException(422, "请添加提示点或框选区域")
    if any(not (0 <= x < W and 0 <= y < H) for x, y in body.points):
        raise HTTPException(422, "point outside the slice")
    if body.box is not None:
        x0, y0, x1, y1 = body.box
        if not (0 <= x0 < x1 <= W and 0 <= y0 < y1 <= H):
            raise HTTPException(422, "invalid box")
    try:
        return service.predict(b, body.z, body.points, body.labels, body.box, body.only_background, body.candidate,
                               snap_boundary=body.snap_boundary, boundary_sensitivity=body.boundary_sensitivity)
    except SAMUnavailable as exc:
        raise HTTPException(503, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


class NeighbourLabelIn(BaseModel):
    z: int = Field(ge=0)
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)
    token: str | None = Field(default=None, min_length=32, max_length=32)   # a pending SAM mask, voted over
    radius: int = Field(default=6, ge=1, le=32)
    z_src: int | None = Field(default=None, ge=0)          # 指定去哪一片取色；不给就自动挑最近的


@router.post("/blocks/{block_id}/neighbour-label")
def neighbour_label(block_id: str, body: NeighbourLabelIn):
    """跨片取色: which cell owns this place on the nearest section that has a label there.

    Read-only. It writes nothing — the annotator gets an id back and fills by hand as before."""
    from emqc.annotate.neighbour import lookup
    from emqc.annotate.sam import service as sam_service

    b = _block(block_id, read_only=True)
    mask, z = None, body.z
    if body.token:
        try:
            p = sam_service.proposal(b, body.token)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        mask, z = p["mask"], int(p["z"])
    try:
        return lookup(b, z, mask=mask, x=body.x, y=body.y, radius=body.radius, z_src=body.z_src)
    except IndexError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/blocks/{block_id}/sam/apply")
def sam_apply(block_id: str, body: SAMApplyIn):
    from emqc.annotate.sam import service
    b = _block(block_id)
    try:
        rec = service.apply(b, body.token, _int_id(body.new_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return _edit_response(b, rec)


class SmartFillIn(BaseModel):
    z: int = Field(ge=0)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    sensitivity: float = Field(default=0.5, ge=0.0, le=1.0)
    max_radius: int = Field(default=0, ge=0, le=8192)  # 0 = unlimited
    scope: Literal["same", "same_bg", "any"] = "same"


class TokenApplyIn(BaseModel):
    token: str = Field(min_length=32, max_length=32)
    new_id: str | int


class SplitIn(BaseModel):
    z: int = Field(ge=0)
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class CutIn(BaseModel):
    z: int = Field(ge=0)
    points: list[list[int]] = Field(min_length=2, max_length=5000)


@router.post("/blocks/{block_id}/smart-fill/preview")
def smart_fill_preview(block_id: str, body: SmartFillIn):
    """智能填充 preview: the membrane-bounded region around the click, as an overlay PNG plus a token to apply it."""
    from emqc.annotate.boundary import service as smart

    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if not (0 <= body.z < Z and 0 <= body.x < W and 0 <= body.y < H):
        raise HTTPException(404, "outside the block")
    try:
        return smart.preview(b, body.z, body.x, body.y, body.sensitivity, body.max_radius, body.scope)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/blocks/{block_id}/smart-fill/apply")
def smart_fill_apply(block_id: str, body: TokenApplyIn):
    from emqc.annotate.boundary import service as smart

    b = _block(block_id)
    try:
        rec = smart.apply(b, body.token, _int_id(body.new_id))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _edit_response(b, rec)


@router.post("/blocks/{block_id}/split")
def split(block_id: str, body: SplitIn):
    """分离: the clicked component of its label becomes a new id (the label must have other pieces in this slice)."""
    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if not (0 <= body.z < Z and 0 <= body.x < W and 0 <= body.y < H):
        raise HTTPException(404, "outside the block")
    if not b.has_seg:
        raise HTTPException(404, "block has no segmentation")
    try:
        rec = b.split_component(body.z, body.x, body.y)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec)


@router.post("/blocks/{block_id}/cut")
def cut(block_id: str, body: CutIn):
    """切割: a drawn line splits the label it crosses; the largest piece keeps the id, the others get new ids."""
    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if not 0 <= body.z < Z:
        raise HTTPException(404, "z outside the block")
    if not b.has_seg:
        raise HTTPException(404, "block has no segmentation")
    pts = [(min(max(int(p[0]), 0), W - 1), min(max(int(p[1]), 0), H - 1)) for p in body.points if len(p) >= 2]
    try:
        rec = b.cut(body.z, pts)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec)


@router.get("/blocks/{block_id}")
def block_info(block_id: str):
    return _block(block_id).info()


@router.get("/blocks/{block_id}/compare/{z}")
def compare_slice(block_id: str, z: int):
    from fastapi.responses import JSONResponse
    from emqc.annotate.provenance import comparison

    try:
        return JSONResponse(comparison(_block(block_id, read_only=True), z), headers={"Cache-Control": "no-store"})
    except IndexError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/blocks/{block_id}/provenance")
def provenance_report(block_id: str, z: int | None = None, format: Literal["json", "csv"] = "json"):
    from fastapi.responses import JSONResponse
    from emqc.annotate.provenance import csv_report, report

    try:
        data = report(_block(block_id, read_only=True), z)
    except IndexError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    headers = {"Cache-Control": "no-store"}
    if format == "csv":
        headers["Content-Disposition"] = 'attachment; filename="annotation-provenance.csv"'
        return Response(csv_report(data), media_type="text/csv; charset=utf-8", headers=headers)
    return JSONResponse(data, headers=headers)


@router.get("/blocks/{block_id}/em/{z}.png")
def em_png(block_id: str, z: int, request: Request):
    b = _block(block_id)
    try:
        data = b.em_png(z)
    except IndexError as e:
        raise HTTPException(404, str(e))
    import hashlib
    etag = '"' + hashlib.md5(data).hexdigest()[:16] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-cache", "ETag": etag})


@router.get("/blocks/{block_id}/labels/{z}.png")
def labels_png(block_id: str, z: int):
    b = _block(block_id)
    if not b.has_seg:
        raise HTTPException(404, "block has no segmentation")
    try:
        data = b.labels_png(z)
    except IndexError as e:
        raise HTTPException(404, str(e))
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-cache"})


@router.get("/blocks/{block_id}/labels/{z}.json")
def labels_json(block_id: str, z: int):
    b = _block(block_id)
    if not b.has_seg:
        raise HTTPException(404, "block has no segmentation")
    try:
        return b.labels_table(z)
    except IndexError as e:
        raise HTTPException(404, str(e))


class RepairPreviewIn(BaseModel):
    z: int = Field(ge=0)
    dark: int = Field(default=12, ge=0, le=255)     # "destroyed" means at or below this grey level


class TokenOnlyIn(BaseModel):
    token: str = Field(min_length=32, max_length=32)


@router.get("/blocks/{block_id}/repair/scan")
def repair_scan(block_id: str, dark: int = 12):
    """Which sections look destroyed, so the annotator does not have to hunt for them."""
    from emqc.annotate.interpolate import detect_damage

    b = _block(block_id)
    out = []
    for z in range(b.shape_zyx[0]):
        m = detect_damage(b.em_slice(z), dark)
        f = float(m.mean())
        if f > 0:
            out.append({"z": z, "fraction": round(f, 4), "whole": bool(f > 0.97)})
    return {"block_id": b.id, "dark": dark, "n": len(out), "sections": out}


@router.post("/blocks/{block_id}/repair/preview")
def repair_preview(block_id: str, body: RepairPreviewIn):
    """修补损坏切片: the labels the cells would have carried across the destroyed area, as a preview."""
    from emqc.annotate.interpolate import service as repair_service

    b = _block(block_id)
    if not 0 <= body.z < b.shape_zyx[0]:
        raise HTTPException(404, "z outside the block")
    try:
        return repair_service.preview(b, body.z, body.dark)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/blocks/{block_id}/repair/apply")
def repair_apply(block_id: str, body: TokenOnlyIn):
    from emqc.annotate.interpolate import service as repair_service

    b = _block(block_id)
    try:
        rec = repair_service.apply(b, body.token)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _edit_response(b, rec)


@router.get("/blocks/{block_id}/neuroglancer/block")
def neuroglancer_block(block_id: str, z: int = 0):
    """A link that opens the WHOLE block in the 3D viewer, with its outline drawn.

    For the dark blobs that carry no label there is nothing to select, so the annotator has to arrive with the block
    framed and look around at full resolution themselves."""
    from emqc.annotate.neuroglancer import link_for_block

    b = _block(block_id)
    if not 0 <= z < b.shape_zyx[0]:
        raise HTTPException(404, "z outside the block")
    return {**link_for_block(b.meta, b.shape_zyx, z), "block_id": b.id, "shape_zyx": list(b.shape_zyx)}


@router.get("/blocks/{block_id}/neuroglancer")
def neuroglancer(block_id: str, z: int, x: int, y: int, zoom_nm: float = 4.0):
    """A link that opens this pixel in the public 3D viewer, with the segment under it selected when there is one.

    Used to answer "what is this dark blob" — a question one section usually cannot settle but 3D can."""
    from emqc.annotate.neuroglancer import link_for

    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if not (0 <= z < Z and 0 <= x < W and 0 <= y < H):
        raise HTTPException(404, "outside the block")
    # the pristine label, not the working copy: the public viewer serves the original c3 segmentation and knows
    # nothing about edits made here, so selecting the edited id would point at the wrong cell.
    seg_id = int(b._seg_ro[x, y, z]) if b.has_seg else 0   # screen x -> axis 0
    neighbours = None
    if b.has_seg and not seg_id:
        # a dark blob with no label of its own: collect the cells around it so the viewer has something to render
        import numpy as np

        r = 24
        H2, W2 = H, W
        win = np.asarray(b._seg_ro[max(0, x - r):min(W2, x + r + 1), max(0, y - r):min(H2, y + r + 1), z])
        ids, counts = np.unique(win[win > 0], return_counts=True)
        neighbours = [int(i) for i in ids[np.argsort(-counts)][:8]]
    return {**link_for(b.meta, x, y, z, seg_id or None, zoom_nm, b.shape_zyx, neighbours),
            "block_id": b.id, "clicked": {"x": x, "y": y, "z": z},
            "edited_id": str(b.pick(z, x, y)) if b.has_seg else None}


@router.get("/blocks/{block_id}/pick")
def pick(block_id: str, z: int, x: int, y: int):
    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if not (0 <= z < Z and 0 <= x < W and 0 <= y < H):
        raise HTTPException(404, "outside the block")
    return {"id": str(b.pick(z, x, y)) if b.has_seg else None}


class FillIn(BaseModel):
    z: int
    x: int
    y: int
    new_id: str | int
    whole_slice: bool = False


class MergeIn(BaseModel):
    from_id: str | int
    to_id: str | int
    scope: str = Field(default="block", pattern="^(block|slice)$")
    z: int | None = None


class MergePairIn(BaseModel):
    z: int
    first: tuple[int, int]
    second: tuple[int, int]


class PaintIn(BaseModel):
    z: int
    points: list[list[int]] = Field(min_length=1)
    radius: int = Field(default=3, ge=0, le=200)
    new_id: str | int


def _edit_response(b, rec):
    return {"edit": rec, "n_edits": len(b.edits()), "max_id": str(b.max_id())}


@router.post("/blocks/{block_id}/fill")
def fill(block_id: str, body: FillIn):
    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if not (0 <= body.z < Z and 0 <= body.x < W and 0 <= body.y < H):
        raise HTTPException(404, "outside the block")
    try:
        rec = b.fill(body.z, body.x, body.y, _int_id(body.new_id), body.whole_slice)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec)


@router.post("/blocks/{block_id}/paint")
def paint(block_id: str, body: PaintIn):
    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if not 0 <= body.z < Z:
        raise HTTPException(404, "z outside the block")
    pts = [(min(max(int(p[0]), 0), W - 1), min(max(int(p[1]), 0), H - 1)) for p in body.points if len(p) >= 2]
    try:
        rec = b.paint(body.z, pts, body.radius, _int_id(body.new_id))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec)


@router.post("/blocks/{block_id}/merge-pair")
def merge_pair(block_id: str, body: MergePairIn):
    b = _block(block_id)
    if not 0 <= body.z < b.shape_zyx[0]:
        raise HTTPException(422, "invalid z")
    try:
        rec = b.merge_pair(body.z, body.first, body.second)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec)


@router.post("/blocks/{block_id}/merge")
def merge(block_id: str, body: MergeIn):
    b = _block(block_id)
    if body.scope == "slice" and (body.z is None or not 0 <= body.z < b.shape_zyx[0]):
        raise HTTPException(422, "slice scope needs a valid z")
    try:
        rec = b.merge(_int_id(body.from_id), _int_id(body.to_id), body.scope, body.z)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec)


@router.post("/blocks/{block_id}/undo")
def undo(block_id: str):
    b = _block(block_id)
    rec = b.undo()
    return {"undone": rec, "n_edits": len(b.edits())}


@router.post("/blocks/{block_id}/new-id")
def new_id(block_id: str):
    b = _block(block_id)
    if not b.has_seg:
        raise HTTPException(404, "block has no segmentation")
    return {"id": str(b.new_id())}


@router.get("/blocks/{block_id}/edits")
def edits(block_id: str, limit: int = 50):
    e = _block(block_id).edits()
    return {"n": len(e), "edits": e[-limit:][::-1]}
