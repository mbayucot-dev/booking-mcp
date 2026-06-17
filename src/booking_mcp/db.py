"""Engine + session for the shared booking database.

One engine/sessionmaker per process; a context-managed session per tool call.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

log = logging.getLogger("booking_mcp.db")

_engine: Engine | None = None
_sessionmaker: sessionmaker | None = None
_owns_engine = False  # True only when we created the engine, so dispose is safe


def configure(engine: Engine) -> None:
    """Bind the session factory to a caller-owned engine (used by tests); dispose won't touch it."""
    global _engine, _sessionmaker, _owns_engine
    _engine = engine
    _sessionmaker = sessionmaker(bind=engine, expire_on_commit=False)
    _owns_engine = False


def _build_engine() -> Engine:
    s = get_settings()
    url = s.database_url
    if url.startswith("sqlite"):
        return create_engine(
            url, future=True, pool_pre_ping=True, connect_args={"check_same_thread": False}
        )
    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=s.db_pool_size,
        max_overflow=s.db_max_overflow,
        pool_recycle=s.db_pool_recycle,
        pool_timeout=s.db_pool_timeout,
        # Per-statement cap so a slow query can't pin a worker thread.
        connect_args={"options": f"-c statement_timeout={s.db_statement_timeout_ms}"},
    )


def _factory() -> sessionmaker:
    global _engine, _sessionmaker, _owns_engine
    if _sessionmaker is None:
        _engine = _build_engine()
        _owns_engine = True
        _sessionmaker = sessionmaker(bind=_engine, expire_on_commit=False)
    return _sessionmaker


def get_sessionmaker() -> sessionmaker:
    """The active session factory (building the default engine if needed)."""
    return _factory()


def get_engine() -> Engine:
    """The active engine (building the default one if needed)."""
    _factory()
    assert _engine is not None
    return _engine


def create_all() -> None:
    """Create any missing tables (idempotent). For standalone use against a fresh DB;
    a no-op when tables exist, so it's safe against the shared booking-agent DB."""
    from .models import Base

    Base.metadata.create_all(get_engine())


def dispose() -> None:
    """Release the connection pool on shutdown. No-op for an injected (test) engine."""
    global _engine, _sessionmaker, _owns_engine
    if not _owns_engine:
        return
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None
    _owns_engine = False


@contextmanager
def session() -> Iterator[Session]:
    s = _factory()()
    try:
        yield s
    finally:
        s.close()
