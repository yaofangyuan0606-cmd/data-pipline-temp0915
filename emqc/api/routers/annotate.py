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


def _roots() -> list:
    extra = [Path(x.strip()).resolve() for x in settings.annotate_extra_roots.split(";") if x.strip()]
    if settings.sam_blocks_dir and Path(settings.sam_blocks_dir).exists():
        extra.append(Path(settings.sam_blocks_dir).resolve())
    return extra


def get_store() -> AnnotateStore:
    global _store
    root = settings.annotate_root.resolve() if settings.annotate_root else None
    if (_store is None or _store.roots != [r for r in [root, *_roots()] if r]
            or _store.workdir != settings.annotate_workdir):
        _store = AnnotateStore(root, settings.annotate_workdir, _roots())
    return _store


def reset_store() -> None:
    global _store
    _store = None


def _block(block_id: str):
    try:
        return get_store().get(block_id)
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


class SAMPredictIn(BaseModel):
    z: int = Field(ge=0)
    points: list[tuple[int, int]] = Field(default_factory=list, max_length=64)
    labels: list[Literal[0, 1]] = Field(default_factory=list, max_length=64)
    box: tuple[int, int, int, int] | None = None
    only_background: bool = True
    candidate: int | None = Field(default=None, ge=0, le=2)


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
        return service.predict(b, body.z, body.points, body.labels, body.box, body.only_background, body.candidate)
    except SAMUnavailable as exc:
        raise HTTPException(503, str(exc))
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


@router.get("/blocks/{block_id}")
def block_info(block_id: str):
    return _block(block_id).info()


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
