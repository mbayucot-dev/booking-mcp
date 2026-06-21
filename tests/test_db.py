"""The lazy session factory (used when no engine is injected)."""

import pytest
from sqlalchemy import text

from booking_mcp import db


def test_lazy_factory_builds_from_settings(monkeypatch):
    # No injected engine → build one from DATABASE_URL (sqlite branch here).
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(db, "_sessionmaker", None)
    try:
        with db.session() as s:
            assert s.execute(text("SELECT 1")).scalar() == 1
    finally:
        db._sessionmaker = None  # don't leak the sqlite factory to other tests


def test_lazy_factory_postgres_branch(monkeypatch):
    # Postgres URL → pooled engine (create_engine is lazy, so no connection here).
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setattr(db, "_sessionmaker", None)
    try:
        assert db._factory() is not None
    finally:
        db._sessionmaker = None


def test_create_all_blocked_without_standalone_mode(monkeypatch):
    monkeypatch.delenv("STANDALONE_MODE", raising=False)
    with pytest.raises(RuntimeError, match="STANDALONE_MODE"):
        db.create_all()


def test_create_all_allowed_in_standalone_mode(Session, monkeypatch):
    monkeypatch.setenv("STANDALONE_MODE", "true")
    db.create_all()  # tables already exist — no-op; must not raise


def test_dispose_releases_owned_engine(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(db, "_sessionmaker", None)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_owns_engine", False)
    db._factory()  # builds an engine we own
    assert db._engine is not None and db._owns_engine is True
    db.dispose()
    assert db._engine is None and db._sessionmaker is None and db._owns_engine is False
