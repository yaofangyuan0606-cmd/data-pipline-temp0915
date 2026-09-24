"""Annotation API behind the VAST-style slice viewer: per-slice images, label index maps, pick / fill / paint / undo."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query
from pydantic import BaseModel, Field, field_validator
from typing import Literal

from emqc.annotate.store import Actor, AnnotateStore
from emqc.auth import actor_for, can_override
from emqc.config import settings

from .auth import require_user

# 整个标注接口都要先登录（EMQC_AUTH_DISABLED=1 时 require_user 直接放行）
router = APIRouter(prefix="/api/v1/annotate", tags=["annotation"], dependencies=[Depends(require_user)])
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


class EditIn(BaseModel):
    """每一笔写入除了自己的参数之外还带两样：谁在改（annotator），以及他动手时看到的是这一片的哪个版本
    （expect_rev，见 Block.conflicts）。都可以不给——脚本照旧能用；工作台总是带上。"""
    annotator: str | None = Field(default=None, max_length=64)
    expect_rev: int | None = Field(default=None, ge=0)

    @field_validator("annotator")
    @classmethod
    def _clean_name(cls, v):
        if v is None:
            return None
        v = " ".join(v.split())                    # 去首尾空白，把换行、连续空格压成一个空格
        if any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError("标注人里不能有控制字符")
        return v or None


class UndoIn(EditIn):
    force: bool = False                            # 明确要撤别人的那一笔（界面确认过之后才带）
    n: int | None = Field(default=None, ge=1)      # 钉住要撤的记录号：确认框里看到的那一笔，中间又多了一笔就拒绝


def _who(body: EditIn, user=None) -> Actor | None:
    """谁在写。开了登录就是登录用户——页面传什么名字都不认，身份只认会话；没开登录才用页面里填的名字。"""
    if user is not None:
        return actor_for(user)
    if body.annotator is None and settings.annotate_require_annotator:
        raise HTTPException(422, "缺少标注人：请先在工作台右上角填写你的名字，再改标签")
    return Actor(body.annotator) if body.annotator else None


def _rev(b, z) -> int | None:
    try:
        return b.slice_rev(int(z)) if z is not None else None
    except ValueError:
        return None


def _when(ts) -> str:
    return ts[11:16] if isinstance(ts, str) and len(ts) >= 16 else "刚才"


def _fresh(b, z, body: EditIn, actor: Actor | None) -> None:
    """有人在客户端看到的版本之后改过第 z 片 → 409，让页面刷新后重来。匿名调用、没说自己看到哪个版本的调用
    （脚本，以及工作台刚改完还没重载完的那一瞬）不检查。"""
    if z is None or body.expect_rev is None or actor is None:
        return
    c = b.conflicts(int(z), body.expect_rev, actor)
    if c is not None:
        who = c.get("by") or "未署名的操作"
        what = "撤销了一次改动" if c.get("action") == "undo" else f"改了 {c.get('n_px') or 0} 像素"
        raise HTTPException(409, {"code": "stale", "message": f"{who} 在 {_when(c.get('ts'))} {what}，本片已刷新为最新结果，请再操作一次",
                                  "latest": c, "rev": _rev(b, z)})


@router.get("/blocks")
def list_blocks():
    st = get_store()
    return {"root": str(st.root) if st.root else None, "blocks": st.refresh()}


@router.get("/comparison-blocks")
def comparison_blocks():
    st = get_store(read_only=True)
    return {"root": str(st.root) if st.root else None, "blocks": st.refresh()}


class SAMPredictIn(BaseModel):
    model_config = {"extra": "forbid"}

    z: int = Field(ge=0)
    points: list[tuple[int, int]] = Field(default_factory=list, max_length=64)
    labels: list[Literal[0, 1]] = Field(default_factory=list, max_length=64)
    box: tuple[int, int, int, int] | None = None
    only_background: bool = True
    snap_boundary: bool = False                      # 贴合膜边界: post-process the mask with emqc.annotate.boundary
    boundary_sensitivity: float = Field(default=0.5, ge=0.0, le=1.0)


class SAMApplyIn(EditIn):
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
        return service.predict(b, body.z, body.points, body.labels, body.box, body.only_background,
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
    radius: int = Field(default=10, ge=1, le=32)
    z_src: int | None = Field(default=None, ge=0)          # 指定去哪一片取色；不给就自动挑最近的


@router.get("/blocks/{block_id}/neuroglancer/embed")
def neuroglancer_embed(block_id: str, z: int = Query(ge=0), px: int = Query(default=600, ge=100, le=4000),
                       layers: Literal["em", "em+seg"] = "em+seg"):
    """A public-viewer URL framing this block at section z, flat xy layout, for the compare page's viewer panes:
    `layers=em` is the bare EM, `em+seg` the EM under the full c3 segmentation."""
    from emqc.annotate.neuroglancer import link_for_embed

    b = _block(block_id, read_only=True)
    if not 0 <= z < b.shape_zyx[0]:
        raise HTTPException(404, "z outside the block")
    return {**link_for_embed(b.meta, b.shape_zyx, z, px, with_seg=(layers == "em+seg")), "block_id": b.id, "z": z, "layers": layers}


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
def sam_apply(block_id: str, body: SAMApplyIn, user=Depends(require_user)):
    from emqc.annotate.sam import service
    b = _block(block_id)
    actor = _who(body, user)
    try:
        with b.lock:
            rec = service.apply(b, body.token, _int_id(body.new_id), by=actor)
            rev = _rev(b, rec.get("z")) if isinstance(rec, dict) else None
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return _edit_response(b, rec, rev)


@router.get("/blocks/{block_id}")
def block_info(block_id: str):
    return _block(block_id).info()


class WarmIn(BaseModel):
    z0: int = Field(ge=0)
    z1: int = Field(ge=0)


@router.post("/blocks/{block_id}/compare/warm")
def compare_warm(block_id: str, body: WarmIn):
    """Pre-build the label indices for a z-range in one pass, so the ±10 playback's 21 light frames are cheap."""
    b = _block(block_id, read_only=True)
    if body.z1 - body.z0 > 60:
        raise HTTPException(422, "一次最多预热 61 片")
    try:
        return b.warm_labels(body.z0, body.z1)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/blocks/{block_id}/compare/{z}")
def compare_slice(block_id: str, z: int, light: bool = False):
    """`light=1` returns the images only (no provenance) — for the compare page's ±10 playback prefetch."""
    from fastapi.responses import JSONResponse
    from emqc.annotate.provenance import comparison, comparison_light

    try:
        b = _block(block_id, read_only=True)
        return JSONResponse((comparison_light if light else comparison)(b, z), headers={"Cache-Control": "no-store"})
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
    from fastapi.responses import JSONResponse

    b = _block(block_id)
    if not b.has_seg:
        raise HTTPException(404, "block has no segmentation")
    # 这一片的版本号随标签表一起下发：页面拿着它改标签，服务端就知道他看到的是不是最新的（见 _fresh）。
    # 先取版本号再取表、同一把锁里：表至多比版本号新（宁可多报一次冲突），绝不能比它旧——那会漏掉别人刚做的改动。
    with b.lock:
        rev = _rev(b, z)
        try:
            table = b.labels_table(z)
        except IndexError as e:
            raise HTTPException(404, str(e))
    table["rev"] = rev
    return JSONResponse(table, headers={"Cache-Control": "no-store"})


class RepairPreviewIn(BaseModel):
    z: int = Field(ge=0)
    dark: int = Field(default=12, ge=0, le=255)     # "destroyed" means at or below this grey level


class TokenOnlyIn(EditIn):
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
def repair_apply(block_id: str, body: TokenOnlyIn, user=Depends(require_user)):
    from emqc.annotate.interpolate import service as repair_service

    b = _block(block_id)
    actor = _who(body, user)
    try:
        with b.lock:
            rec = repair_service.apply(b, body.token, by=actor)
            rev = _rev(b, rec.get("z")) if isinstance(rec, dict) else None
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _edit_response(b, rec, rev)


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


class FillIn(EditIn):
    z: int
    x: int
    y: int
    new_id: str | int
    whole_slice: bool = False


class MergeIn(EditIn):
    from_id: str | int
    to_id: str | int
    scope: str = Field(default="block", pattern="^(block|slice)$")
    z: int | None = None


class MergePairIn(EditIn):
    z: int
    first: tuple[int, int]
    second: tuple[int, int]


class PaintIn(EditIn):
    z: int
    points: list[list[int]] = Field(min_length=1)
    radius: int = Field(default=3, ge=0, le=200)
    new_id: str | int
    only_id: str | int | None = None               # 橡皮（new_id 0）时：只擦这个颜色


class RefineIn(EditIn):
    z: int = Field(ge=0)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    sensitivity: float = Field(default=0.5, ge=0.0, le=1.0)


def _edit_response(b, rec, rev=None):
    out = {"edit": rec, "n_edits": len(b.edits()), "max_id": str(b.max_id())}
    if rev is not None:
        out["rev"] = rev                           # 改完这一片的新版本号（在锁里取的），页面立刻接上，不必等重载
    return out


class ClearLabelsIn(EditIn):
    z: int = Field(ge=0)
    ids: list[str | int] = Field(min_length=1, max_length=5000)


@router.post("/blocks/{block_id}/clear-labels")
def clear_labels(block_id: str, body: ClearLabelsIn, user=Depends(require_user)):
    """批量删除: clear every pixel of the given ids on section z, as one undoable edit."""
    b = _block(block_id)
    if not 0 <= body.z < b.shape_zyx[0]:
        raise HTTPException(404, "z outside the block")
    actor = _who(body, user)
    try:
        with b.lock:
            _fresh(b, body.z, body, actor)
            rec = b.clear_labels(body.z, [_int_id(i) for i in body.ids], by=actor)
            rev = _rev(b, body.z)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec, rev)


@router.post("/blocks/{block_id}/fill")
def fill(block_id: str, body: FillIn, user=Depends(require_user)):
    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if not (0 <= body.z < Z and 0 <= body.x < W and 0 <= body.y < H):
        raise HTTPException(404, "outside the block")
    actor = _who(body, user)
    try:
        with b.lock:
            _fresh(b, body.z, body, actor)
            rec = b.fill(body.z, body.x, body.y, _int_id(body.new_id), body.whole_slice, by=actor)
            rev = _rev(b, body.z)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec, rev)


@router.post("/blocks/{block_id}/paint")
def paint(block_id: str, body: PaintIn, user=Depends(require_user)):
    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if not 0 <= body.z < Z:
        raise HTTPException(404, "z outside the block")
    pts = [(min(max(int(p[0]), 0), W - 1), min(max(int(p[1]), 0), H - 1)) for p in body.points if len(p) >= 2]
    actor = _who(body, user)
    try:
        with b.lock:
            _fresh(b, body.z, body, actor)
            rec = b.paint(body.z, pts, body.radius, _int_id(body.new_id), by=actor,
                          only_id=_int_id(body.only_id) if body.only_id is not None else None)
            rev = _rev(b, body.z)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec, rev)


@router.post("/blocks/{block_id}/refine-edge")
def refine_edge(block_id: str, body: RefineIn, user=Depends(require_user)):
    """修缮边缘：点到的标签块若压过了黑色的膜，往里收到膜为止（只收缩，收掉的像素清成背景，可撤销）。"""
    b = _block(block_id)
    Z, H, W = b.shape_zyx
    if not (0 <= body.z < Z and 0 <= body.x < W and 0 <= body.y < H):
        raise HTTPException(404, "outside the block")
    actor = _who(body, user)
    try:
        with b.lock:
            _fresh(b, body.z, body, actor)
            rec = b.refine_edge(body.z, body.x, body.y, body.sensitivity, by=actor)
            rev = _rev(b, body.z)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec, rev)


@router.post("/blocks/{block_id}/merge-pair")
def merge_pair(block_id: str, body: MergePairIn, user=Depends(require_user)):
    b = _block(block_id)
    if not 0 <= body.z < b.shape_zyx[0]:
        raise HTTPException(422, "invalid z")
    actor = _who(body, user)
    try:
        with b.lock:
            _fresh(b, body.z, body, actor)
            rec = b.merge_pair(body.z, body.first, body.second, by=actor)
            rev = _rev(b, body.z)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec, rev)


@router.post("/blocks/{block_id}/merge")
def merge(block_id: str, body: MergeIn, user=Depends(require_user)):
    b = _block(block_id)
    if body.scope == "slice" and (body.z is None or not 0 <= body.z < b.shape_zyx[0]):
        raise HTTPException(422, "slice scope needs a valid z")
    actor = _who(body, user)
    try:
        with b.lock:
            _fresh(b, body.z if body.scope == "slice" else None, body, actor)   # 整块合并动所有片，没有单片版本可对
            rec = b.merge(_int_id(body.from_id), _int_id(body.to_id), body.scope, body.z, by=actor)
            rev = _rev(b, body.z) if body.scope == "slice" else None
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _edit_response(b, rec, rev)


@router.post("/blocks/{block_id}/undo")
def undo(block_id: str, z: int | None = None, body: UndoIn | None = None, user=Depends(require_user)):
    """撤销本片最近一笔。有身份时只能撤自己的：最近一笔是别人的 → 409 not_yours；审核员 / 管理员在界面确认后带
    force（和确认时看到的记录号 n）再来，标注员没有这个权限（403）。"""
    from emqc.annotate.store import UndoForbidden, UndoMismatch

    body = body or UndoIn()
    b = _block(block_id)
    actor = _who(body, user)
    if body.force and not can_override(user):
        raise HTTPException(403, {"code": "forbidden", "message": "只有审核员或管理员能撤销别人的改动"})
    try:
        with b.lock:
            _fresh(b, z, body, actor)
            rec = b.undo(z, by=actor, force=body.force, expect_n=body.n)
            return {"undone": rec, "n_edits": len(b.edits()), "n_slice_edits": len(b.edits(z)) if z is not None else None,
                    "rev": _rev(b, z)}
    except UndoMismatch as e:
        raise HTTPException(409, {"code": "stale", "message": str(e), "latest": e.record, "rev": _rev(b, z)})
    except UndoForbidden as e:
        raise HTTPException(409, {"code": "not_yours", "message": str(e), "latest": e.record, "can_override": can_override(user)})
    except (ValueError, IndexError, OSError) as e:
        raise HTTPException(422, str(e))


@router.post("/blocks/{block_id}/undo-all")
def undo_all(block_id: str, z: int, body: UndoIn | None = None, user=Depends(require_user)):
    """一次撤掉本片全部改动，回到标注前。本片有别人的记录 → 409 not_yours；审核员 / 管理员确认后带 force 再来。"""
    from emqc.annotate.store import UndoForbidden

    body = body or UndoIn()
    b = _block(block_id)
    if not 0 <= z < b.shape_zyx[0]:
        raise HTTPException(404, "z outside the block")
    actor = _who(body, user)
    if body.force and not can_override(user):
        raise HTTPException(403, {"code": "forbidden", "message": "只有审核员或管理员能撤销别人的改动"})
    try:
        with b.lock:
            _fresh(b, z, body, actor)
            done = b.undo_all(z, by=actor, force=body.force)
            return {"undone": done, "n_edits": len(b.edits()), "n_slice_edits": 0, "rev": _rev(b, z)}
    except UndoForbidden as e:
        raise HTTPException(409, {"code": "not_yours", "message": str(e), "latest": e.record, "can_override": can_override(user)})
    except (ValueError, IndexError, OSError) as e:
        raise HTTPException(422, str(e))


@router.post("/blocks/{block_id}/new-id")
def new_id(block_id: str, z: int = 0):
    b = _block(block_id)
    if not b.has_seg:
        raise HTTPException(404, "block has no segmentation")
    try:
        return {"id": str(b.new_id(z)), "created_ids": b.created_ids()}
    except (ValueError, IndexError) as e:
        raise HTTPException(422, str(e))


@router.get("/blocks/{block_id}/edits")
def edits(block_id: str, limit: int = 50, z: int | None = None):
    b = _block(block_id)
    try:
        e = b.edits(z)
        return {"n": len(e), "edits": e[-limit:][::-1], "editors": b.editors(records=e),
                "rev": b.slice_rev(z) if z is not None else None}
    except (ValueError, IndexError, OSError) as err:
        raise HTTPException(422, str(err))


@router.get("/blocks/{block_id}/audit")
def audit(block_id: str, limit: int = Query(default=100, ge=1, le=2000), z: int | None = None):
    """操作流水：每一次写入和撤销、谁、几点——包括已被撤销、edits 里已经看不到的那些。"""
    b = _block(block_id, read_only=True)           # 对比页读它：只读句柄，不触发旧工作文件迁移
    try:
        entries = b.audit_entries()
    except ValueError as e:
        raise HTTPException(422, str(e))
    if z is not None:
        entries = [e for e in entries if e.get("z") is None or e.get("z") == z]
    return {"n": len(entries), "entries": entries[-limit:][::-1]}


# ---------------------------------------------------------------- 谁在线：工作台每 15 秒报一次自己在哪一片
_presence: dict[str, dict[str, dict]] = {}     # block_id -> 人 -> {"z", "at", "name", "user"}；只在内存里，说的是"现在"
_presence_lock = threading.Lock()              # 接口跑在线程池里，两次心跳可能同时到
PRESENCE_TTL_S = 60
PRESENCE_MAX = 500                             # 没开登录时键是自报的名字，别让它无限长


class PresenceIn(EditIn):
    z: int = Field(ge=0)


@router.post("/blocks/{block_id}/presence")
def presence(block_id: str, body: PresenceIn, user=Depends(require_user)):
    """心跳：登记"我在这块的第 z 片"，换回同块还有谁、他们在哪一片，以及这一片的当前版本和最近一次操作。
    页面用版本号发现"别人刚改了我正在看的这一片"，自动重载并提示。不写任何文件。
    登录了按用户 id 记（改显示名不会变成"另一个人"）；没开登录时按自报的名字记。"""
    b = _block(block_id, read_only=True)
    actor = actor_for(user) if user is not None else (Actor(body.annotator) if body.annotator else None)
    key = None if actor is None else (f"u{actor.id}" if actor.id is not None else f"n:{actor.name}")
    now = time.monotonic()
    with _presence_lock:
        room = _presence.setdefault(b.id, {})
        for k in [k for k, v in room.items() if now - v["at"] > PRESENCE_TTL_S]:
            room.pop(k, None)
        if key:
            room[key] = {"z": body.z, "at": now, "name": actor.name, "user": actor.user}
        while len(room) > PRESENCE_MAX:
            room.pop(min(room, key=lambda k: room[k]["at"]))
        others = sorted(({"by": v["name"], "user": v.get("user"), "z": v["z"], "same_slice": v["z"] == body.z, "ago_s": int(now - v["at"])}
                         for k, v in room.items() if k != key), key=lambda o: (not o["same_slice"], o["by"]))
        n_online = len(room)
    try:
        rev, entries = b.slice_rev(body.z), b.audit_entries()
    except ValueError:
        rev, entries = None, []
    latest = next((e for e in reversed(entries) if e.get("z") is None or e.get("z") == body.z), None)
    return {"others": others, "rev": rev, "latest": latest, "n_online": n_online}
