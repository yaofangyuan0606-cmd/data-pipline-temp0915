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
from emqc.db.models import User

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
    return {"auth_disabled": settings.auth_disabled, "auth_open": settings.auth_open,
            "has_users": (auth.count_users(s) > 0) if not settings.auth_disabled else None}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, s: Session = Depends(get_session)):
    if settings.auth_disabled:
        raise HTTPException(409, {"code": "disabled", "message": "这个实例没有开启登录（EMQC_AUTH_DISABLED=1），直接打开工作台即可"})
    if settings.auth_open:
        # 试用模式：不看密码，账号不存在就建；只有停用的账号进不来
        user = auth.open_login(s, body.username)
        if user is None:
            raise HTTPException(401, {"code": "bad_login", "message": "这个账号已停用，或者名字是空的"})
    else:
        if not body.password:
            raise HTTPException(401, {"code": "bad_login", "message": "登录名或密码不对"})
        keys = (f"u:{body.username.strip().lower()}", f"ip:{client_ip(request)}")
        wait = auth.throttled(*keys)
        if wait:
            raise HTTPException(429, {"code": "throttled", "message": f"尝试太频繁，请 {wait} 秒后再试"})
        user = auth.authenticate(s, body.username, body.password)
        if user is None:
            auth.note_failure(*keys)
            raise HTTPException(401, {"code": "bad_login", "message": "登录名或密码不对"})
        auth.clear_failures(*keys)
    token = auth.open_session(s, user, request.headers.get("user-agent", ""), client_ip(request))
    auth.purge_expired_sessions(s)
    s.commit()
    _set_cookie(response, token)
    return {"user": user_dict(user)}


@router.post("/logout")
def logout(request: Request, response: Response, s: Session = Depends(get_session)):
    auth.revoke_session(s, request.cookies.get(COOKIE))
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
    s.commit()
    return {"ok": True, "user": user_dict(u)}


# ---------------------------------------------------------------- 管理员：账号管理
class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    display_name: str | None = Field(default=None, max_length=64)
    role: str = Field(default="annotator", pattern="^(admin|reviewer|annotator)$")
    password: str | None = Field(default=None, max_length=256)     # 不给就生成初始密码，首次登录必须改


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    role: str | None = Field(default=None, pattern="^(admin|reviewer|annotator)$")
    is_active: bool | None = None


def _active_admins(s: Session) -> int:
    return len(list(s.scalars(select(User.id).where(User.role == "admin", User.is_active.is_(True)))))


@router.get("/users")
def list_users(admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    return {"users": [user_dict(u) for u in s.scalars(select(User).order_by(User.id))], "roles": list(ROLES)}


@router.post("/users")
def create_user(body: UserIn, admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    try:
        u, issued = auth.create_user(s, body.username, body.password, body.display_name, body.role,
                                     created_by=admin.id if admin else None)
    except ValueError as e:
        raise HTTPException(422, str(e))
    s.commit()
    return {"user": user_dict(u), "temp_password": issued}


@router.patch("/users/{uid}")
def patch_user(uid: int, body: UserPatch, admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    u = s.get(User, uid)
    if u is None:
        raise HTTPException(404, "没有这个账号")
    demoting = body.role is not None and body.role != "admin"
    deactivating = body.is_active is False
    if u.role == "admin" and u.is_active and (demoting or deactivating) and _active_admins(s) <= 1:
        raise HTTPException(422, "至少要保留一个可用的管理员")
    if admin is not None and u.id == admin.id and (demoting or deactivating):
        raise HTTPException(422, "不能停用或降级自己，请让另一位管理员操作")
    if body.display_name is not None:
        try:
            u.display_name = auth.clean_display_name(body.display_name, u.username)
        except ValueError as e:
            raise HTTPException(422, str(e))
    if body.role is not None:
        u.role = body.role
    if body.is_active is not None:
        u.is_active = body.is_active
        if not body.is_active:
            auth.revoke_user_sessions(s, u.id)         # 停用立刻生效，不等会话过期
    s.commit()
    return {"user": user_dict(u)}


@router.post("/users/{uid}/reset-password")
def reset_password(uid: int, admin: User | None = Depends(require_admin), s: Session = Depends(get_session)):
    u = s.get(User, uid)
    if u is None:
        raise HTTPException(404, "没有这个账号")
    temp = auth.temp_password()
    auth.set_password(s, u, temp, must_change=True)
    s.commit()
    return {"user": user_dict(u), "temp_password": temp}
