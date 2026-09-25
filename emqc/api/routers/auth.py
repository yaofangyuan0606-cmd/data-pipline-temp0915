"""登录、账号管理接口，以及给别的路由用的"当前用户"依赖。

Cookie 里是随机会话令牌（HttpOnly、SameSite=Lax），库里只存哈希；每个请求最多查一次库（结果记在 request.state）。
开着 EMQC_AUTH_DISABLED 时所有依赖都返回 None，路由按"没有账号"的老方式工作。
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc import auth
from emqc.auth import COOKIE, ROLES, user_dict
from emqc.config import settings
from emqc.db.base import get_session, session_scope
from emqc.db.models import AccountEvent, User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


# ---------------------------------------------------------------- 依赖
def current_user(request: Request) -> User | None:
    """Cookie → 用户；没登录是 None。一个请求里只查一次。"""
    if settings.auth_disabled:
        return None
    if getattr(request.state, "user_checked", False):
        return getattr(request.state, "user", None)
    request.state.user_checked = True
    request.state.user = None
    token = request.cookies.get(COOKIE)
    if token:
        with session_scope() as s:
            request.state.user = auth.resolve_session(s, token)
            request.state.username = request.state.user.username if request.state.user is not None else None   # 给请求日志用，不再碰 ORM 对象
    return request.state.user


def require_user(request: Request) -> User | None:
    """接口用：没登录 401。拿着初始密码还没改的，除了改密码什么都不能做。开着 AUTH_DISABLED 时返回 None（不拦）。"""
    if settings.auth_disabled:
        return None
    user = current_user(request)
    if user is None:
        raise HTTPException(401, {"code": "auth", "message": "请先登录"})
    if user.must_change_password and not request.url.path.startswith("/api/v1/auth/"):
        raise HTTPException(403, {"code": "password", "message": "请先修改初始密码（右上角「账号」）"})
    return user


def require_admin(request: Request) -> User | None:
    user = require_user(request)
    if user is not None and user.role != "admin":
        raise HTTPException(403, {"code": "forbidden", "message": "只有管理员能管理账号"})
    return user


def page_user(request: Request) -> User | None:
    """HTML 页面用：没登录跳到登录页并记住要回哪里；拿着初始密码的先去改密码。"""
    if settings.auth_disabled:
        return None
    user = current_user(request)
    if user is None:
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise HTTPException(303, headers={"Location": f"/login?next={quote(nxt, safe='')}"})
    if user.must_change_password and request.url.path != "/account":
        raise HTTPException(303, headers={"Location": "/account?force=1"})
    return user


def safe_next(target: str | None) -> str:
    """登录后跳回的地址只允许本站路径：防开放重定向。"""
    t = target or ""
    return t if t.startswith("/") and not t.startswith("//") and "\\" not in t else "/annotate"


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=settings.session_days * 86400, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/")


# ---------------------------------------------------------------- 登录 / 登出 / 我是谁 / 改密码
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(default="", max_length=256)     # 试用模式下可以为空


@router.get("/status")
def status(s: Session = Depends(get_session)):
    """登录页用：这个实例开了登录没有、是不是试用模式、有没有任何账号（一个都没有就提示先用命令行建管理员）。"""
    if settings.auth_disabled:
        return {"auth_disabled": True, "auth_open": False, "has_users": None}
    return {"auth_disabled": False, "auth_open": auth.login_mode(s) == "open", "has_users": auth.count_users(s) > 0}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, s: Session = Depends(get_session)):
    if settings.auth_disabled:
        raise HTTPException(409, {"code": "disabled", "message": "这个实例没有开启登录（EMQC_AUTH_DISABLED=1），直接打开工作台即可"})
    ip = client_ip(request)
    existing = auth.find_user(s, body.username)
    if auth.login_mode(s) == "open" and (existing is None or not auth.has_password(existing)):
        # 试用模式：没有真正密码的账号不看密码，账号不存在就建；只有停用的账号进不来
        user = auth.open_login(s, body.username)
        if user is None:
            auth.record_event(s, "login_failed", target=existing, target_name=None if existing else body.username[:64],
                              detail="账号已停用" if existing else "名字是空的", ip=ip)
            s.commit()
            raise HTTPException(401, {"code": "bad_login", "message": "这个账号已停用，或者名字是空的"})
    else:
        keys = (f"u:{body.username.strip().lower()}", f"ip:{ip}")
        wait = auth.throttled(*keys)
        if wait:
            raise HTTPException(429, {"code": "throttled", "message": f"尝试太频繁，请 {wait} 秒后再试"})
        user = auth.authenticate(s, body.username, body.password) if body.password else None
        if user is None:
            auth.note_failure(*keys)
            auth.record_event(s, "login_failed", target=existing, target_name=None if existing else body.username[:64],
                              detail="没填密码" if not body.password else "密码不对或账号不可用", ip=ip)
            s.commit()
            if existing is not None and not body.password and auth.login_mode(s) == "open":
                raise HTTPException(401, {"code": "need_password", "message": "这个账号设了密码，请输入密码"})
            raise HTTPException(401, {"code": "bad_login", "message": "登录名或密码不对"})
        auth.clear_failures(*keys)
    token = auth.open_session(s, user, request.headers.get("user-agent", ""), ip)
    auth.record_event(s, "login", actor=user, target=user, ip=ip)
    auth.purge_expired_sessions(s)
    s.commit()
    _set_cookie(response, token)
    return {"user": user_dict(user)}


@router.post("/logout")
def logout(request: Request, response: Response, s: Session = Depends(get_session)):
    user = current_user(request)
    auth.revoke_session(s, request.cookies.get(COOKIE))
    if user is not None:
        auth.record_event(s, "logout", actor=user, target=user, ip=client_ip(request))
    s.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    if settings.auth_disabled:
        return {"user": None, "auth_disabled": True}
    user = current_user(request)
    if user is None:
        raise HTTPException(401, {"code": "auth", "message": "请先登录"})
    return {"user": user_dict(user), "auth_disabled": False}


class PasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


@router.post("/password")
def change_password(body: PasswordIn, request: Request, s: Session = Depends(get_session)):
    user = require_user(request)
    if user is None:
        raise HTTPException(409, {"code": "disabled", "message": "这个实例没有开启登录"})
    u = s.get(User, user.id)
    if u is None or not auth.verify_password(body.old_password, u.password_hash):
        raise HTTPException(422, "原密码不对")
    if body.new_password == body.old_password:
        raise HTTPException(422, "新密码不能和原密码一样")
    try:
        auth.set_password(s, u, body.new_password, keep_session=request.cookies.get(COOKIE))
    except ValueError as e:
        raise HTTPException(422, str(e))
    auth.record_event(s, "change_password", actor=u, target=u, ip=client_ip(request))
    s.commit()
    return {"ok": True, "user": user_dict(u)}


# ---------------------------------------------------------------- 管理员：账号管理
class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)            # 合规的登录名，或中文名（登录名由它生成，见 auth.username_for）
    display_name: str | None = Field(default=None, max_length=64)
    role: str = Field(default="annotator", pattern="^(admin|reviewer|annotator)$")
    password: str | None = Field(default=None, max_length=256)     # 不给就生成初始密码，首次登录必须改


class BulkIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)              # 一行一个：登录名或名字[,显示名[,角色]]
    role: str = Field(default="annotator", pattern="^(admin|reviewer|annotator)$")


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    role: str | None = Field(default=None, pattern="^(admin|reviewer|annotator)$")
    is_active: bool | None = None


class ModeIn(BaseModel):
    mode: str = Field(pattern="^(open|password)$")


def _active_admins(s: Session) -> int:
    return len(list(s.scalars(select(User.id).where(User.role == "admin", User.is_active.is_(True)))))


def _new_user(s: Session, admin: User | None, typed: str, display: str | None, role: str, password: str | None):
    """填的是英文就必须是合规的登录名（打错了要报出来，不能悄悄换成别的）；填中文名则登录名由名字生成，本人填名字就能登录。"""
    typed = " ".join((typed or "").split())
    if typed.isascii():
        username, fallback = auth.normalize_username(typed), typed
    else:
        username, fallback = auth.username_for(typed)
    return auth.create_user(s, username, password, display or fallback, role, created_by=admin.id if admin else None)


@router.get("/users")
def list_users(admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    live: dict[int, int] = {}
    for sess in s.scalars(select(auth.AuthSession).where(auth.AuthSession.expires_at > now)):
        live[sess.user_id] = live.get(sess.user_id, 0) + 1
    users = []
    for u in s.scalars(select(User).order_by(User.id)):
        d = user_dict(u)
        d["n_sessions"] = live.get(u.id, 0)
        users.append(d)
    return {"users": users, "roles": list(ROLES), "login_mode": auth.login_mode(s), "login_modes": auth.LOGIN_MODES}


@router.post("/users")
def create_user(body: UserIn, request: Request, admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    try:
        u, issued = _new_user(s, admin, body.username, body.display_name, body.role, body.password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    auth.record_event(s, "create", actor=admin, target=u, detail=f"角色 {auth.ROLE_NAMES[u.role]}", ip=client_ip(request))
    s.commit()
    return {"user": user_dict(u), "temp_password": issued}


@router.post("/users/bulk")
def bulk_create(body: BulkIn, request: Request, admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    """一次建一批：每行「登录名或名字[,显示名[,角色]]」，逗号或 Tab 分隔，角色可写中文。每个人各发一个初始密码（只返回这一次）。
    某一行出错不影响其他行。"""
    names = {v: k for k, v in auth.ROLE_NAMES.items()}
    results = []
    for n, raw in enumerate(body.text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.replace("\t", ",").replace("，", ",").split(",")]
        typed, display = parts[0], (parts[1] if len(parts) > 1 and parts[1] else None)
        role = parts[2] if len(parts) > 2 and parts[2] else body.role
        role = names.get(role, role)
        try:
            if role not in ROLES:
                raise ValueError(f"角色「{parts[2]}」不认识，只能是管理员 / 审核员 / 标注员")
            u, issued = _new_user(s, admin, typed, display, role, None)
            auth.record_event(s, "create", actor=admin, target=u, detail=f"批量 · 角色 {auth.ROLE_NAMES[u.role]}", ip=client_ip(request))
            results.append({"line": n, "ok": True, "user": user_dict(u), "temp_password": issued})
        except ValueError as e:
            results.append({"line": n, "ok": False, "input": line[:100], "error": str(e)})
    s.commit()
    return {"results": results, "n_ok": sum(r["ok"] for r in results)}


@router.patch("/users/{uid}")
def patch_user(uid: int, body: UserPatch, request: Request, admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    u = s.get(User, uid)
    if u is None:
        raise HTTPException(404, "没有这个账号")
    demoting = body.role is not None and body.role != "admin"
    deactivating = body.is_active is False
    if u.role == "admin" and u.is_active and (demoting or deactivating) and _active_admins(s) <= 1:
        raise HTTPException(422, "至少要保留一个可用的管理员")
    if admin is not None and u.id == admin.id and (demoting or deactivating):
        raise HTTPException(422, "不能停用或降级自己，请让另一位管理员操作")
    changes, issued = [], None
    if body.display_name is not None:
        try:
            name = auth.clean_display_name(body.display_name, u.username)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if name != u.display_name:
            changes.append(f"显示名 {u.display_name} → {name}")
            u.display_name = name
    if body.role is not None and body.role != u.role:
        changes.append(f"角色 {auth.ROLE_NAMES[u.role]} → {auth.ROLE_NAMES[body.role]}")
        u.role = body.role
        if body.role == "admin" and not auth.has_password(u):
            # 管理员必须有密码（试用模式下也要校验）：从试用账号升上来的，顺手发一个初始密码
            issued = auth.temp_password()
            auth.set_password(s, u, issued, must_change=True)
            changes.append("发了初始密码")
    if body.is_active is not None and body.is_active != u.is_active:
        changes.append("启用" if body.is_active else "停用")
        u.is_active = body.is_active
        if not body.is_active:
            auth.revoke_user_sessions(s, u.id)         # 停用立刻生效，不等会话过期
    if changes:
        auth.record_event(s, "update", actor=admin, target=u, detail="；".join(changes), ip=client_ip(request))
    s.commit()
    return {"user": user_dict(u), "temp_password": issued}


@router.post("/users/{uid}/reset-password")
def reset_password(uid: int, request: Request, admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    u = s.get(User, uid)
    if u is None:
        raise HTTPException(404, "没有这个账号")
    temp = auth.temp_password()
    auth.set_password(s, u, temp, must_change=True)
    auth.record_event(s, "reset_password", actor=admin, target=u, ip=client_ip(request))
    s.commit()
    return {"user": user_dict(u), "temp_password": temp}


@router.post("/users/{uid}/revoke-sessions")
def revoke_sessions(uid: int, request: Request, admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    """强制下线：这个人所有浏览器里的登录立刻作废（账号本身不动，重新登录就行）。"""
    u = s.get(User, uid)
    if u is None:
        raise HTTPException(404, "没有这个账号")
    keep = request.cookies.get(COOKIE) if admin is not None and u.id == admin.id else None
    n = auth.revoke_user_sessions(s, u.id, keep=keep)
    auth.record_event(s, "revoke", actor=admin, target=u, detail=f"{n} 个登录", ip=client_ip(request))
    s.commit()
    return {"user": user_dict(u), "n_revoked": n}


@router.post("/users/issue-passwords")
def issue_passwords(request: Request, admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    """给所有还没有真正密码的可用账号（试用模式自动建的）各发一个初始密码——切到「正式」登录方式之前用。只返回这一次。"""
    out = []
    for u in s.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id)):
        if auth.has_password(u):
            continue
        temp = auth.temp_password()
        auth.set_password(s, u, temp, must_change=True)
        auth.record_event(s, "reset_password", actor=admin, target=u, detail="批量发初始密码", ip=client_ip(request))
        out.append({"user": user_dict(u), "temp_password": temp})
    s.commit()
    return {"results": out}


@router.get("/login-mode")
def get_login_mode(admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    n_nopw = sum(1 for u in s.scalars(select(User).where(User.is_active.is_(True))) if not auth.has_password(u))
    return {"mode": auth.login_mode(s), "modes": auth.LOGIN_MODES, "env_default": "open" if settings.auth_open else "password",
            "n_without_password": n_nopw, "open_role": settings.auth_open_role}


@router.put("/login-mode")
def put_login_mode(body: ModeIn, request: Request, admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    before = auth.login_mode(s)
    auth.set_login_mode(s, body.mode, admin)
    if before != body.mode:
        auth.record_event(s, "login_mode", actor=admin, detail=f"{before} → {body.mode}", ip=client_ip(request))
    s.commit()
    return get_login_mode(admin, s)


@router.get("/events")
def account_events(limit: int = 200, user_id: int | None = None, action: str | None = None,
                   admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    """账号操作记录，新的在前。可以按人（actor 或 target 是他）、按动作筛。"""
    q = select(AccountEvent).order_by(AccountEvent.id.desc()).limit(max(1, min(limit, 1000)))
    if user_id is not None:
        q = q.where((AccountEvent.actor_id == user_id) | (AccountEvent.target_id == user_id))
    if action:
        q = q.where(AccountEvent.action == action)
    return {"events": [auth.event_dict(e) for e in s.scalars(q)], "actions": auth.ACTIONS}
