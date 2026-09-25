"""管理员看的运行状况：进程跑了多久、重启过几次、最近的报错、请求记录、谁在线、每个人标了多少；以及浏览器上报前端报错的入口。"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from emqc import __version__, logs
from emqc.config import settings
from emqc.db.base import get_session
from emqc.db.models import AccountEvent, User

from .auth import client_ip, current_user, require_admin

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
client_router = APIRouter(prefix="/api/v1/logs", tags=["admin"])
log = logging.getLogger("emqc.admin")


def _online() -> list[dict]:
    """工作台每 15 秒报一次自己在哪块哪片（见 annotate.presence）；60 秒内报过的算在线。"""
    from .annotate import PRESENCE_TTL_S, _presence, _presence_lock

    now = time.monotonic()
    people: dict = {}
    with _presence_lock:
        for block_id, room in _presence.items():
            for key, v in room.items():
                if now - v["at"] > PRESENCE_TTL_S:
                    continue
                p = people.setdefault(key, {"name": v["name"], "user": v.get("user"), "where": []})
                p["where"].append({"block": block_id, "z": v["z"], "ago_s": int(now - v["at"])})
    return sorted(people.values(), key=lambda p: p["name"] or "")


@router.get("/overview")
def overview(admin=Depends(require_admin), s: Session = Depends(get_session)):
    life = logs.read_lifecycle(500)
    week = time.time() - 7 * 86400

    def recent(e):
        try:
            return datetime.fromisoformat(e["ts"]).timestamp() >= week
        except (KeyError, ValueError):
            return False

    starts = [e for e in life if e.get("event") == "start" and recent(e)]
    crashes = [e for e in life if recent(e) and (e.get("event") == "unclean" or (e.get("event") == "exit" and e.get("code") not in (0, None)))]
    this = next((e for e in reversed(life) if e.get("event") == "start" and e.get("pid") == os.getpid()), None)
    n_users = s.scalar(select(func.count(User.id))) or 0
    n_active = s.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    failed = s.scalar(select(func.count(AccountEvent.id)).where(AccountEvent.action == "login_failed", AccountEvent.ts >= since)) or 0
    return {
        "version": __version__, "commit": (this or {}).get("commit"), "pid": os.getpid(), "deployment": settings.deployment_name or None,
        "started_at": (this or {}).get("ts"), "uptime_s": round(time.time() - logs.STARTED_AT),
        "starts_7d": len(starts), "crashes_7d": len(crashes), "last_crash": crashes[-1] if crashes else None,
        "errors": logs.error_counts(), "online": _online(), "n_users": n_users, "n_active_users": n_active,
        "failed_logins_24h": failed, "alert_configured": bool((settings.alert_webhook or "").strip()),
        "log_dir": str(logs.log_dir()), "supervised": bool(os.environ.get("EMQC_SUPERVISED")),
    }


@router.get("/logs")
def read_logs(kind: str = Query("problems", pattern="^(problems|access|lifecycle)$"), limit: int = Query(200, ge=1, le=2000),
              level: str = Query("WARNING", pattern="^(WARNING|ERROR)$"), only_errors: bool = False, admin=Depends(require_admin)):
    """problems = 最近的 WARNING / ERROR（含浏览器上报的前端报错，带堆栈）；access = 请求记录；lifecycle = 进程启停。新的在前。"""
    if kind == "problems":
        return {"items": logs.recent_problems(limit, level)}
    if kind == "access":
        return {"items": logs.read_access(limit, only_errors)}
    return {"items": logs.read_lifecycle(limit)[::-1]}


_activity_cache: dict = {"at": 0.0, "data": None}
_activity_lock = threading.Lock()


@router.get("/activity")
def activity(admin=Depends(require_admin)):
    """每个人仍然有效的标注改动：几笔、多少像素、在哪几块、最近一次什么时候。一分钟算一次（要读每块的改动记录）。"""
    with _activity_lock:
        if _activity_cache["data"] is not None and time.time() - _activity_cache["at"] < 60:
            return _activity_cache["data"]
    from .annotate import get_store

    per: dict = {}
    st = get_store(read_only=True)
    for info in st.refresh():
        bid = info.get("block_id") or info.get("id")
        if not bid or info.get("error"):
            continue
        try:
            recs = st.get(bid).edits()
        except Exception:
            continue
        for r in recs:
            key = f"id:{r['by_id']}" if r.get("by_id") is not None else (f"u:{r['by_user']}" if r.get("by_user") else f"n:{r.get('by')}")
            p = per.setdefault(key, {"by_id": r.get("by_id"), "by_user": r.get("by_user"), "by": r.get("by"), "n": 0, "n_px": 0,
                                     "blocks": set(), "last": None})
            p["n"] += 1
            p["n_px"] += int(r.get("n_px") or 0)
            p["blocks"].add(bid)
            if isinstance(r.get("ts"), str) and (p["last"] is None or r["ts"] > p["last"]):
                p["last"] = r["ts"]
                p["by"] = r.get("by") or p["by"]
    data = {"people": sorted(({**p, "blocks": sorted(p["blocks"])} for p in per.values()), key=lambda p: -p["n"]),
            "computed_at": time.time()}
    with _activity_lock:
        _activity_cache.update(at=time.time(), data=data)
    return data


# ---------------------------------------------------------------- 浏览器上报的前端报错
class ClientError(BaseModel):
    message: str = Field(default="", max_length=1000)
    source: str = Field(default="", max_length=500)
    line: int | None = None
    col: int | None = None
    stack: str = Field(default="", max_length=4000)
    page: str = Field(default="", max_length=500)


_client_hits: dict[str, deque] = {}
_client_lock = threading.Lock()


@client_router.post("/client")
def client_error(body: ClientError, request: Request):
    """页面里的 JS 报错（window.onerror / 未处理的 Promise）报到这里，记进日志，管理员在「运行日志」里看得到。
    不需要登录（登录页自己也可能出错）；同一来源一分钟最多 30 条。"""
    ip = client_ip(request)
    now = time.time()
    with _client_lock:
        q = _client_hits.setdefault(ip, deque(maxlen=30))
        if len(q) == q.maxlen and now - q[0] < 60:
            raise HTTPException(429, "上报太频繁")
        q.append(now)
        if len(_client_hits) > 2000:
            _client_hits.clear()
    user = current_user(request)
    log.warning("前端报错 [%s] %s（%s:%s:%s）页面 %s ua=%s\n%s", user.username if user else "未登录", body.message,
                body.source, body.line, body.col, body.page, request.headers.get("user-agent", "")[:160], body.stack)
    return {"ok": True}
