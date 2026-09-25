"""运行日志：服务挂了、报错了、谁登录了、管理员改了谁的账号，事后都查得到，要紧的事还能推到群里。

都在 EMQC_LOG_DIR（默认 var/log）下：
- emqc.log         应用日志（INFO 以上），10 MB 滚动留 10 份；报错带完整堆栈和请求编号（页面上报错时显示的那个编号）
- access.log       请求记录：写操作全记；读操作只记出错的和慢的（超过 2 秒），切片图和心跳这类高频请求不刷屏
- lifecycle.jsonl  进程启停：每次启动、正常退出，以及守护脚本（scripts/supervise.py）看到的异常退出。
                   启动时发现上一次既没有正常退出、也没有被守护脚本记下，就补记一笔"上次没有正常退出"
另有内存里最近 500 条 WARNING 以上（含浏览器上报的前端报错），给管理员的「运行日志」页直接看。
账号相关的操作（登录、登录失败、建号、改角色、停用、重置密码）写进数据库的 account_events 表，见 emqc.auth.record_event。

配置 EMQC_ALERT_WEBHOOK（企业微信 / 钉钉 / 飞书群机器人的地址）后：进程异常退出、上次没有正常退出、5 分钟内报错
5 次以上，都会推一条消息；同一类消息 5 分钟内只推一次。
"""
from __future__ import annotations

import collections
import contextvars
import json
import logging
import logging.handlers
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from emqc.config import settings

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
STARTED_AT = time.time()
_setup_done = False
_ring: collections.deque = collections.deque(maxlen=500)
_ring_lock = threading.Lock()
_errors_at: collections.deque = collections.deque(maxlen=200)
_alerted: dict[str, float] = {}
_alert_lock = threading.Lock()
FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"


def log_dir() -> Path:
    d = Path(settings.log_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class _RequestId(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id.get()
        return True


class _Ring(logging.Handler):
    """最近的 WARNING 以上留在内存里给管理页看；ERROR 计数，5 分钟内 5 次以上推一次告警。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exc = self.formatter.formatException(record.exc_info) if record.exc_info and self.formatter else None
            item = {"ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds"),
                    "level": record.levelname, "logger": record.name, "request_id": getattr(record, "request_id", "-"),
                    "message": record.getMessage()[:4000], "exc": exc[-6000:] if exc else None}
            with _ring_lock:
                _ring.append(item)
            if record.levelno >= logging.ERROR:
                now = time.time()
                _errors_at.append(now)
                recent = sum(1 for t in _errors_at if now - t < 300)
                if recent >= 5:
                    alert("errors", f"5 分钟内报错 {recent} 次，最近一条：{item['message'][:300]}")
        except Exception:  # pragma: no cover - a log handler must never raise
            self.handleError(record)


def setup_logging() -> None:
    """装好文件日志、内存环和请求编号。重复调用无害（测试里 app 会启动好几次）。"""
    global _setup_done
    if _setup_done:
        return
    _setup_done = True
    d = log_dir()
    fmt = logging.Formatter(FORMAT)
    rid = _RequestId()
    app_file = logging.handlers.RotatingFileHandler(d / "emqc.log", maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8")
    app_file.setLevel(logging.INFO)
    ring = _Ring(level=logging.WARNING)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    for h in (app_file, ring, console):
        h.setFormatter(fmt)
        h.addFilter(rid)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (app_file, ring, console):
        root.addHandler(h)
    # uvicorn 自己的 logger 不往上传，单独挂上文件和内存环，才看得到 "Exception in ASGI application" 这类
    uv = logging.getLogger("uvicorn")
    for h in (app_file, ring):
        uv.addHandler(h)
    acc = logging.getLogger("emqc.access")
    acc.propagate = False
    acc.setLevel(logging.INFO)
    acc_file = logging.handlers.RotatingFileHandler(d / "access.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    acc_file.setFormatter(logging.Formatter("%(message)s"))
    acc.addHandler(acc_file)


# ---------------------------------------------------------------- 请求记录
_QUIET_PREFIXES = ("/static/", "/previews/", "/favicon")
SLOW_MS = 2000


def access(method: str, path: str, query: str, status: int, ms: float, user: str | None, ip: str, rid: str) -> None:
    if path.startswith(_QUIET_PREFIXES) and status < 400:
        return
    if method in ("GET", "HEAD", "OPTIONS") and status < 400 and ms < SLOW_MS:
        return
    if path.endswith("/presence") and status < 400 and ms < SLOW_MS:
        return
    q = f"?{query}" if query else ""
    logging.getLogger("emqc.access").info(json.dumps(
        {"ts": _now_iso(), "rid": rid, "method": method, "path": (path + q)[:500], "status": status, "ms": round(ms),
         "user": user, "ip": ip}, ensure_ascii=False))


# ---------------------------------------------------------------- 进程启停
def _lifecycle_path() -> Path:
    return log_dir() / "lifecycle.jsonl"


def lifecycle_event(event: str, **fields) -> dict:
    rec = {"ts": _now_iso(), "event": event, "pid": os.getpid(), **fields}
    with open(_lifecycle_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read_lifecycle(limit: int = 200) -> list[dict]:
    out = []
    for line in tail_lines(_lifecycle_path(), limit):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def on_startup(port: int | None = None) -> dict | None:
    """记一笔启动；上一次如果既没正常退出、也没被守护脚本记下退出，补记"上次没有正常退出"并告警。返回那条补记（或 None）。"""
    last = next(iter(read_lifecycle(1)), None)
    unclean = None
    if last and last.get("event") == "start":
        unclean = lifecycle_event("unclean", prev_pid=last.get("pid"), prev_start=last.get("ts"),
                                  note="上次没有正常退出（被强杀、内存不够被系统杀掉、或机器重启）")
        logging.getLogger("emqc").error("上次运行（pid %s，%s 启动）没有正常退出", last.get("pid"), last.get("ts"))
        alert("unclean", f"服务刚刚重新启动；上次运行（{last.get('ts')} 启动）没有正常退出，可能是崩溃或机器重启")
    elif last and last.get("event") == "exit" and last.get("code") not in (0, None):
        logging.getLogger("emqc").warning("守护脚本记录上次异常退出：code=%s", last.get("code"))
    from emqc import __version__

    lifecycle_event("start", version=__version__, commit=_git_commit(), port=port, deployment=settings.deployment_name or None)
    return unclean


def on_shutdown() -> None:
    lifecycle_event("stop", uptime_s=round(time.time() - STARTED_AT))


def _git_commit() -> str | None:
    head = Path(__file__).resolve().parents[1] / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            return (head.parent / ref[5:]).read_text().strip()[:7]
        return ref[:7]
    except OSError:
        return None


# ---------------------------------------------------------------- 读日志（管理页）
def tail_lines(path: Path, limit: int, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
    """文件最后 limit 行（只读末尾 max_bytes，大日志也不慢）。文件不存在就是空。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]                  # 第一行多半被截断了
    return [ln for ln in lines if ln.strip()][-limit:]


def recent_problems(limit: int = 200, level: str = "WARNING") -> list[dict]:
    want = logging.getLevelName(level) if isinstance(logging.getLevelName(level), int) else logging.WARNING
    with _ring_lock:
        items = [i for i in _ring if logging.getLevelName(i["level"]) >= want]
    return items[-limit:][::-1]


def read_access(limit: int = 200, only_errors: bool = False) -> list[dict]:
    out = []
    for line in tail_lines(log_dir() / "access.log", limit * (5 if only_errors else 1)):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if only_errors and int(rec.get("status") or 0) < 400:
            continue
        out.append(rec)
    return out[-limit:][::-1]


def error_counts() -> dict:
    now = time.time()
    return {"last_hour": sum(1 for t in _errors_at if now - t < 3600), "last_5min": sum(1 for t in _errors_at if now - t < 300)}


# ---------------------------------------------------------------- 告警推送
def _payload(url: str, text: str) -> dict:
    if "feishu" in url or "larksuite" in url:
        return {"msg_type": "text", "content": {"text": text}}
    if "qyapi.weixin.qq.com" in url or "dingtalk" in url:
        return {"msgtype": "text", "text": {"content": text}}
    return {"text": text}


def alert(key: str, text: str, *, wait: bool = False) -> bool:
    """推一条告警到 EMQC_ALERT_WEBHOOK；没配就只记日志。同一 key 5 分钟内只推一次。返回这次有没有真的发出去。"""
    url = (settings.alert_webhook or "").strip()
    if not url:
        return False
    now = time.time()
    with _alert_lock:
        if now - _alerted.get(key, 0) < 300:
            return False
        _alerted[key] = now
    name = settings.deployment_name or f"端口 {settings.api_port}"
    body = json.dumps(_payload(url, f"[标注平台 · {name}] {text}"), ensure_ascii=False).encode("utf-8")

    def send():
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:  # 告警本身失败只记一笔，不能再触发告警
            logging.getLogger("emqc.alert").info("告警没有发出去：%s", e)

    if wait:
        send()
    else:
        threading.Thread(target=send, daemon=True).start()
    return True
