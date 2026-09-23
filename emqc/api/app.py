"""FastAPI application: REST API + server-rendered dashboard."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from emqc import __version__
from emqc.config import settings
from emqc.db import init_db, recover_stale_runs

from .routers import auth, crawl, dashboard, data, datasets, delivery, patches, qc, traces, annotate

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent

app = FastAPI(title="EM Image QC platform", version=__version__, description="Dataset registry, slice/serial-section QC pipeline, algorithm-facing data API")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    n = recover_stale_runs()
    if n:
        log.warning("marked %d interrupted run(s) as error", n)
    Path(settings.preview_dir).mkdir(parents=True, exist_ok=True)
    from emqc.sysmon import monitor

    monitor.start()
    log.info("data_root=%s db=%s", settings.data_root, settings.db_url.split("@")[-1])


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
app.include_router(annotate.router)
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
    return {"status": "ok", "version": __version__, "pipeline_version": settings.pipeline_version, "data_root": str(settings.data_root)}
