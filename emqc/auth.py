"""登录与账号：用户、角色、会话。

业界标注平台（CVAT、Label Studio）的做法是账号 + 角色 + 服务端会话，每一笔标注记在登录用户名下，而不是让人在
页面上自报姓名。这里照这个来，但不引入整套框架：一张 users 表、一张 auth_sessions 表（都在平台已有的 MySQL 里），
bcrypt 存密码，HttpOnly Cookie 里放随机会话令牌（库里只存它的哈希），14 天滑动过期。

角色：admin 管理员（管账号，什么都能做）、reviewer 审核员（能改、能撤别人的改动）、annotator 标注员（能改，只能撤自己的）。
账号不删除只停用——旧记录里的 by_id 还得对回人。
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc.annotate.store import Actor
from emqc.config import settings
from emqc.db.models import AuthSession, User

ROLES = ("admin", "reviewer", "annotator")
ROLE_NAMES = {"admin": "管理员", "reviewer": "审核员", "annotator": "标注员"}
COOKIE = "emqc_session"
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")
MIN_PASSWORD = 8
MAX_PASSWORD_BYTES = 72                 # bcrypt 的硬上限


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- 密码
def hash_password(password: str) -> str:
    check_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def check_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"密码至少 {MIN_PASSWORD} 位")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"密码最长 {MAX_PASSWORD_BYTES} 字节")
    if password.strip() != password:
        raise ValueError("密码首尾不能有空格")


def temp_password() -> str:
    """管理员建号 / 重置时发的初始密码：够长、好念、必须改。"""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def normalize_username(username: str) -> str:
    u = (username or "").strip().lower()
    if not USERNAME_RE.match(u):
        raise ValueError("登录名 2～32 位，小写字母、数字、_ . -，以字母或数字开头")
    return u


def clean_display_name(name: str | None, fallback: str) -> str:
    v = " ".join(str(name or "").split())
    if any(ord(c) < 32 or ord(c) == 127 or 0x80 <= ord(c) <= 0x9F or c in "​‌‍⁠﻿‪‫‬‭‮" for c in v):
        raise ValueError("显示名里不能有控制字符或不可见字符")
    return v[:64] or fallback


# ---------------------------------------------------------------- 用户
def create_user(s: Session, username: str, password: str | None, display_name: str | None = None,
                role: str = "annotator", created_by: int | None = None, must_change: bool | None = None) -> tuple[User, str | None]:
    """建号。不给密码就生成一个初始密码（返回给调用方显示一次，库里只存哈希），并要求首次登录修改。"""
    username = normalize_username(username)
    if role not in ROLES:
        raise ValueError(f"角色只能是 {', '.join(ROLES)}")
    if s.scalar(select(User).where(User.username == username)) is not None:
        raise ValueError(f"登录名 {username} 已存在")
    issued = None
    if password is None:
        issued = password = temp_password()
        must_change = True if must_change is None else must_change
    user = User(username=username, display_name=clean_display_name(display_name, username), password_hash=hash_password(password),
                role=role, is_active=True, must_change_password=bool(must_change), created_by=created_by)
    s.add(user)
    s.flush()
    return user, issued


def set_password(s: Session, user: User, password: str, *, must_change: bool = False, keep_session: str | None = None) -> None:
    """改密码；其他登录全部作废（keep_session 是当前这次登录的原始令牌，留着不踢自己）。"""
    user.password_hash = hash_password(password)
    user.must_change_password = must_change
    revoke_user_sessions(s, user.id, keep=keep_session)


def user_dict(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name or u.username, "role": u.role,
            "role_name": ROLE_NAMES.get(u.role, u.role), "is_active": bool(u.is_active),
            "must_change_password": bool(u.must_change_password),
            "created_at": u.created_at.isoformat(timespec="seconds") if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat(timespec="seconds") if u.last_login_at else None}


def actor_for(u: User | None) -> Actor | None:
    return None if u is None else Actor(u.display_name or u.username, u.username, u.id)


def can_override(u: User | None) -> bool:
    """能不能撤别人的改动：审核员和管理员能。没开登录时（u 为 None）沿用旧规则——界面确认后就能。"""
    return u is None or u.role in ("admin", "reviewer")


# ---------------------------------------------------------------- 登录节流：同一登录名 / 来源 IP 一分钟内错 5 次就等一会
_FAILS: dict[str, list[float]] = {}
_FAILS_LOCK = threading.Lock()
FAIL_LIMIT, FAIL_WINDOW_S, FAIL_LOCK_S = 5, 60.0, 60.0


def throttled(*keys: str) -> int:
    """还要等几秒才能再试；0 = 现在可以。"""
    now = time.monotonic()
    with _FAILS_LOCK:
        wait = 0
        for k in keys:
            hits = [t for t in _FAILS.get(k, []) if now - t < FAIL_WINDOW_S]
            _FAILS[k] = hits
            if len(hits) >= FAIL_LIMIT:
                wait = max(wait, int(FAIL_LOCK_S - (now - hits[-1])) + 1)
        return wait


def note_failure(*keys: str) -> None:
    now = time.monotonic()
    with _FAILS_LOCK:
        for k in keys:
            _FAILS.setdefault(k, []).append(now)
        if len(_FAILS) > 5000:            # 有人在扫：只留最近的，别让字典无限长
            for k in sorted(_FAILS, key=lambda k: _FAILS[k][-1] if _FAILS[k] else 0)[:2500]:
                _FAILS.pop(k, None)


def clear_failures(*keys: str) -> None:
    with _FAILS_LOCK:
        for k in keys:
            _FAILS.pop(k, None)


def authenticate(s: Session, username: str, password: str) -> User | None:
    """对上了返回用户，否则 None。停用的账号、不存在的账号和密码错误对外一个样子，不泄露哪个存在。"""
    try:
        username = normalize_username(username)
    except ValueError:
        return None
    user = s.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active or not verify_password(password or "", user.password_hash):
        return None
    return user


# ---------------------------------------------------------------- 会话
def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def open_session(s: Session, user: User, user_agent: str = "", ip: str = "") -> str:
    raw = secrets.token_urlsafe(32)
    now = _now()
    s.add(AuthSession(token_hash=_token_hash(raw), user_id=user.id, created_at=now, last_seen_at=now,
                      expires_at=now + timedelta(days=settings.session_days), user_agent=(user_agent or "")[:255], ip=(ip or "")[:64]))
    user.last_login_at = now
    s.flush()
    return raw


def resolve_session(s: Session, raw: str | None) -> User | None:
    """Cookie 里的令牌 → 用户。过期、停用、不存在都是 None。活跃时滑动续期：最近一次访问离现在超过一小时才写库，
    免得每个请求都 UPDATE。"""
    if not raw or len(raw) > 128:
        return None
    sess = s.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(raw)))
    now = _now()
    if sess is None or sess.expires_at <= now:
        return None
    user = s.get(User, sess.user_id)
    if user is None or not user.is_active:
        return None
    if now - sess.last_seen_at > timedelta(hours=1):
        sess.last_seen_at = now
        sess.expires_at = now + timedelta(days=settings.session_days)
    return user


def revoke_session(s: Session, raw: str | None) -> None:
    if raw:
        sess = s.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(raw)))
        if sess is not None:
            s.delete(sess)


def revoke_user_sessions(s: Session, user_id: int, keep: str | None = None) -> int:
    keep_hash = _token_hash(keep) if keep else None
    n = 0
    for sess in s.scalars(select(AuthSession).where(AuthSession.user_id == user_id)):
        if sess.token_hash != keep_hash:
            s.delete(sess)
            n += 1
    return n


def purge_expired_sessions(s: Session) -> int:
    n = 0
    for sess in s.scalars(select(AuthSession).where(AuthSession.expires_at <= _now())):
        s.delete(sess)
        n += 1
    return n


def count_users(s: Session) -> int:
    return len(list(s.scalars(select(User.id))))
