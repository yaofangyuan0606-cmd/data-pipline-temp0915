"""对比页的标记与评论：钉在某个体素 (block, z, x, y) 上的一句话 + 线程。

给人用：在对比页开「标记」，点图上的位置写一句话，同事打开同一个链接就看到钉在同一个地方的标记，能回复、能标为已解决，
链接里带 mark=<id> 直接定位。给 AI agent 用：同一套 JSON 接口——登录后 GET 列表、POST 评论（kind=agent，界面上打「AI」标）。
所有接口都要先登录（EMQC_AUTH_DISABLED 时放行，作者按 annotator 字段记名字）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc.config import settings
from emqc.db.base import get_session
from emqc.db.models import Mark, MarkComment, User

from .auth import require_user

router = APIRouter(prefix="/api/v1/annotate", tags=["marks"], dependencies=[Depends(require_user)])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


class MarkIn(BaseModel):
    z: int = Field(ge=0)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=2000)
    label_id: str | None = Field(default=None, max_length=32)
    annotator: str | None = Field(default=None, max_length=64)     # 没开登录时记名字用


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    kind: str = Field(default="comment", pattern="^(comment|agent)$")
    annotator: str | None = Field(default=None, max_length=64)


class MarkPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(open|resolved)$")
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    annotator: str | None = Field(default=None, max_length=64)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise HTTPException(422, "内容不能为空")
    return text


def _who(user: User | None, annotator: str | None) -> tuple[int | None, str | None, str]:
    if user is not None:
        return user.id, user.username, (user.display_name or user.username)
    name = " ".join((annotator or "").split())[:64]
    return None, None, (name or "匿名")


def _can_delete(user: User | None, m: Mark) -> bool:
    if user is None:
        return True                                   # 没开登录：谁都能删（内部试用）
    return user.role == "admin" or (m.created_by is not None and m.created_by == user.id)


def _block_shape(block_id: str):
    from .annotate import _block

    b = _block(block_id, read_only=True)
    return b.shape_zyx


def mark_dict(m: Mark, comments: list[MarkComment]) -> dict:
    return {"id": m.id, "block_id": m.block_id, "z": m.z, "x": m.x, "y": m.y, "label_id": m.label_id, "text": m.text,
            "status": m.status, "created_by": m.created_by, "created_by_user": m.created_by_user, "created_by_name": m.created_by_name,
            "created_at": _iso(m.created_at), "updated_at": _iso(m.updated_at),
            "resolved_by_name": m.resolved_by_name, "resolved_at": _iso(m.resolved_at),
            "comments": [{"id": c.id, "kind": c.kind, "text": c.text, "created_by": c.created_by, "created_by_user": c.created_by_user,
                          "created_by_name": c.created_by_name, "created_at": _iso(c.created_at)} for c in comments],
            "link": f"/annotate/compare?block={m.block_id}&z={m.z}&mark={m.id}"}


def _with_comments(s: Session, marks: list[Mark]) -> list[dict]:
    if not marks:
        return []
    ids = [m.id for m in marks]
    by_mark: dict[int, list[MarkComment]] = {i: [] for i in ids}
    for c in s.scalars(select(MarkComment).where(MarkComment.mark_id.in_(ids)).order_by(MarkComment.id)):
        by_mark[c.mark_id].append(c)
    return [mark_dict(m, by_mark[m.id]) for m in marks]


@router.get("/blocks/{block_id}/marks")
def list_marks(block_id: str, z: int | None = None, status: str | None = None, s: Session = Depends(get_session)):
    """这个数据块的标记（含评论）。不传 z 给整块，页面自己按片过滤；by_z 是每片的待处理数，给翻页条打点用。"""
    _block_shape(block_id)
    q = select(Mark).where(Mark.block_id == block_id).order_by(Mark.id)
    if z is not None:
        q = q.where(Mark.z == z)
    if status in ("open", "resolved"):
        q = q.where(Mark.status == status)
    marks = list(s.scalars(q))
    by_z: dict[int, int] = {}
    for m in s.scalars(select(Mark).where(Mark.block_id == block_id, Mark.status == "open")):
        by_z[m.z] = by_z.get(m.z, 0) + 1
    return {"marks": _with_comments(s, marks), "n_open": sum(by_z.values()), "by_z": {str(k): v for k, v in sorted(by_z.items())}}


@router.post("/blocks/{block_id}/marks")
def create_mark(block_id: str, body: MarkIn, user: User | None = Depends(require_user), s: Session = Depends(get_session)):
    Z, H, W = _block_shape(block_id)
    if not (body.z < Z and body.x < W and body.y < H):
        raise HTTPException(404, "outside the block")
    uid, uname, name = _who(user, body.annotator)
    m = Mark(block_id=block_id, z=body.z, x=body.x, y=body.y, label_id=body.label_id, text=_clean(body.text),
             status="open", created_by=uid, created_by_user=uname, created_by_name=name)
    s.add(m)
    s.commit()
    return mark_dict(m, [])


@router.get("/marks/{mark_id}")
def get_mark(mark_id: int, s: Session = Depends(get_session)):
    m = s.get(Mark, mark_id)
    if m is None:
        raise HTTPException(404, "没有这个标记")
    return _with_comments(s, [m])[0]


@router.post("/marks/{mark_id}/comments")
def add_comment(mark_id: int, body: CommentIn, user: User | None = Depends(require_user), s: Session = Depends(get_session)):
    m = s.get(Mark, mark_id)
    if m is None:
        raise HTTPException(404, "没有这个标记")
    uid, uname, name = _who(user, body.annotator)
    c = MarkComment(mark_id=m.id, kind=body.kind, text=_clean(body.text), created_by=uid, created_by_user=uname, created_by_name=name)
    m.updated_at = _now()
    s.add(c)
    s.commit()
    return _with_comments(s, [m])[0]


@router.patch("/marks/{mark_id}")
def patch_mark(mark_id: int, body: MarkPatch, user: User | None = Depends(require_user), s: Session = Depends(get_session)):
    m = s.get(Mark, mark_id)
    if m is None:
        raise HTTPException(404, "没有这个标记")
    uid, uname, name = _who(user, body.annotator)
    if body.text is not None:
        if not _can_delete(user, m):
            raise HTTPException(403, "只有作者或管理员能改标记的内容；要补充请回复")
        m.text = _clean(body.text)
    if body.status is not None and body.status != m.status:
        m.status = body.status
        m.resolved_by_name, m.resolved_at = (name, _now()) if body.status == "resolved" else (None, None)
    m.updated_at = _now()
    s.commit()
    return _with_comments(s, [m])[0]


@router.delete("/marks/{mark_id}")
def delete_mark(mark_id: int, user: User | None = Depends(require_user), s: Session = Depends(get_session)):
    m = s.get(Mark, mark_id)
    if m is None:
        raise HTTPException(404, "没有这个标记")
    if not _can_delete(user, m):
        raise HTTPException(403, "只有作者或管理员能删标记；不需要了请标为已解决")
    for c in s.scalars(select(MarkComment).where(MarkComment.mark_id == m.id)):
        s.delete(c)
    s.delete(m)
    s.commit()
    return {"ok": True, "id": mark_id}
