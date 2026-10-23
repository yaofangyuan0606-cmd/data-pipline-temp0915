"""FastAPI application: REST API + server-rendered dashboard."""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from emqc import __version__, logs
from emqc.config import settings
from emqc.db import init_db, recover_stale_runs

from urllib.parse import urlsplit

from .routers import admin, auth, crawl, dashboard, data, datasets, delivery, marks, patches, qc, review, traces, annotate

logs.setup_logging()
log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent

app = FastAPI(title="EM Image QC platform", version=__version__, description="Dataset registry, slice/serial-section QC pipeline, algorithm-facing data API")
# 中间件后声明的在外层：请求先进 _request_log（编号、出错兜底、记日志），再进 _login_gate


@app.middleware("http")
async def _login_gate(request: Request, call_next):
    """全站登录关卡，见 auth.gate。放在中间件里而不是路由依赖里，/previews 这类挂载的静态目录也一并管到。"""
    blocked = await run_in_threadpool(auth.gate, request)       # 查会话要访问数据库，不能在事件循环里做
    if blocked is not None:
        return blocked
    return await call_next(request)


@app.middleware("http")
async def _same_origin(request: Request, call_next):
    """写接口只接受本站页面发来的请求。登录 cookie 对同一台机器上所有端口都算「同站」（经 SSH 隧道时都是 localhost），
    别的端口上的页面也能带着你的会话发 POST；浏览器发写请求时会带 Origin 头，和本站地址对不上就拒绝。
    脚本（不带 Origin）不受影响。"""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        # Origin: null（沙箱 iframe、本地文件、某些跳转后的页面）也算外站：正常的本站页面总会带上本站地址
        if origin is not None and (origin == "null" or urlsplit(origin).netloc != request.headers.get("host", "")):
            return JSONResponse({"detail": {"code": "origin", "message": "请求来自别的网站，已拒绝"}}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


@app.middleware("http")
async def _request_log(request: Request, call_next):
    """每个请求一个编号：日志里、出错时返回给页面的提示里都是它，标注员报"编号 xxxx 出错了"就能直接查到堆栈。"""
    rid = uuid.uuid4().hex[:8]
    token = logs.request_id.set(rid)
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("未处理的异常：%s %s", request.method, request.url.path)
        response = JSONResponse({"detail": {"code": "server", "message": f"服务器出错了（编号 {rid}），已记录，请把编号告诉管理员"}},
                                status_code=500)
    ms = (time.perf_counter() - t0) * 1000
    try:
        if response.status_code >= 500:
            log.warning("%s %s 返回 %s（%.0f ms）", request.method, request.url.path, response.status_code, ms)
        logs.access(request.method, request.url.path, request.url.query, response.status_code, ms,
                    getattr(request.state, "username", None), auth.client_ip(request), rid)
    except Exception:  # 记日志本身出错也不能把请求搞挂
        log.exception("请求日志没写成")
    finally:
        logs.request_id.reset(token)
    response.headers["X-Request-Id"] = rid
    return response


@app.on_event("startup")
async def _threads() -> None:
    # 同步接口都跑在 anyio 的线程池里，默认只有 40 个线程：几个慢请求（整块溯源、预热、SAM）占住之后，
    # 连登录都排不上队。调大一些当缓冲；真正的防线是那些重请求各自的并发上限。
    import anyio

    anyio.to_thread.current_default_thread_limiter().total_tokens = 100


@app.on_event("startup")
def _startup() -> None:
    logs.on_startup(settings.api_port)
    init_db()
    n = recover_stale_runs()
    if n:
        log.warning("marked %d interrupted run(s) as error", n)
    Path(settings.preview_dir).mkdir(parents=True, exist_ok=True)
    from emqc.sysmon import monitor

    monitor.start()
    log.info("data_root=%s db=%s", settings.data_root, settings.db_url.split("@")[-1])


@app.on_event("shutdown")
def _shutdown() -> None:
    logs.on_shutdown()
    log.info("正常退出")


app.include_router(datasets.router)
app.include_router(qc.router)
app.include_router(qc.pipeline_router)
app.include_router(qc.system_router)
app.include_router(data.router)
app.include_router(traces.router)
app.include_router(delivery.exports)
app.include_router(delivery.streams)
app.include_router(delivery.packs)
app.include_router(patches.router)
app.include_router(crawl.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(admin.client_router)
app.include_router(annotate.router)
app.include_router(marks.router)
app.include_router(review.router)
app.include_router(dashboard.router)
Path(settings.preview_dir).mkdir(parents=True, exist_ok=True)
app.mount("/previews", StaticFiles(directory=str(settings.preview_dir)), name="previews")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.get("/api/v1/deployment")
def deployment():
    """What kind of instance this is — shown in the UI and useful when several instances exist."""
    return {"name": settings.deployment_name or "local", "allow_delete_files": settings.allow_delete_files,
            "db": settings.db_url.split("://")[0], "host": settings.api_host, "port": settings.api_port}


@app.get("/api/v1/health")
def health():
    """给外部探活用（定时 curl 这个地址，连不上就是挂了）：进程在不在、跑了多久、数据库通不通、盘够不够。
    不用登录，所以不报任何路径。"""
    from sqlalchemy import text

    from emqc.db.base import engine
    from emqc.sysmon import disk_report

    db_ok = True
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    try:
        disks = disk_report()          # 只看不报：告警由监控线程每分钟发一次，探活被人一秒钟调十次也不会刷屏
    except Exception:
        disks = []
    disk_ok = all(d["ok"] for d in disks)
    return {"status": "ok" if db_ok and disk_ok else "degraded", "db": db_ok, "disk_ok": disk_ok,
            "disk_free_gb": min((d["free_gb"] for d in disks), default=None),
            "version": __version__, "uptime_s": round(time.time() - logs.STARTED_AT), "pipeline_version": settings.pipeline_version}
