from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from emqc.config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str | None = None):
    url = url or settings.db_url
    kwargs: dict = {"echo": settings.db_echo, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs.update(pool_pre_ping=True, pool_recycle=1800)
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite") and ":memory:" not in url:
        # shared deployments run background QC threads while people browse: WAL lets readers work during a write
        from sqlalchemy import event

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _rec):  # pragma: no cover - driver level
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return eng


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def rebind(url: str) -> None:
    """Point the global engine/session at another database (used by tests)."""
    global engine
    engine = _make_engine(url)
    SessionLocal.configure(bind=engine)


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    ensure_columns()


def ensure_columns() -> None:
    """Additive migrations: add columns that exist in the models but not in the database, widen VARCHARs that grew."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"]: c for c in insp.get_columns(table.name)}
            for col in table.columns:
                ddl = col.type.compile(dialect=engine.dialect)
                if col.name not in existing:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}"))
                    continue
                want = getattr(col.type, "length", None)
                have = getattr(existing[col.name]["type"], "length", None)
                if engine.dialect.name == "mysql" and want and have and have < want:
                    nullable = "NULL" if existing[col.name].get("nullable", True) else "NOT NULL"
                    conn.execute(text(f"ALTER TABLE {table.name} MODIFY COLUMN {col.name} {ddl} {nullable}"))


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def recover_stale_runs() -> int:
    """Runs left 'queued' / 'running' by a previous process can never finish: mark them as error (called at startup)."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from .models import Block, Dataset, QCRun, QCRunEvent

    n = 0
    with session_scope() as s:
        for run in s.scalars(select(QCRun).where(QCRun.status.in_(["queued", "running"]))):
            run.status, run.error, run.finished_at = "error", "interrupted: the server process ended before the run finished", datetime.now(timezone.utc).replace(tzinfo=None)
            s.add(QCRunEvent(run_id=run.id, level="error", message="run interrupted by a server restart; blocks already persisted are kept"))
            ds = s.get(Dataset, run.dataset_id)
            if ds is not None and ds.status == "qc_running":
                ds.status = "qc_done" if ds.latest_run_id else "registered"
            for b in s.scalars(select(Block).where(Block.dataset_id == run.dataset_id, Block.status == "running")):
                b.status = "pending"
            n += 1
    return n
