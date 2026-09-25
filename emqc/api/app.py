"""FastAPI application: REST API + server-rendered dashboard."""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from emqc import __version__, logs
from emqc.config import settings
from emqc.db import init_db, recover_stale_runs

from .routers import admin, auth, crawl, dashboard, data, datasets, delivery, marks, patches, qc, traces, annotate

logs.setup_logging()
log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent

app = FastAPI(title="EM Image QC platform", version=__version__, description="Dataset registry, slice/serial-section QC pipeline, algorithm-facing data API")


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
app.include_router(patches.router)
app.include_router(crawl.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(admin.client_router)
app.include_router(annotate.router)
app.include_router(marks.router)
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
    """给外部探活用（定时 curl 这个地址，连不上就是挂了）：进程在不在、跑了多久、数据库通不通。"""
    from sqlalchemy import text

    from emqc.db.base import engine

    db_ok = True
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok, "version": __version__, "uptime_s": round(time.time() - logs.STARTED_AT),
            "pipeline_version": settings.pipeline_version, "data_root": str(settings.data_root)}
