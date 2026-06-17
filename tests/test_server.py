"""Server wiring: readiness check + lifespan."""

import logging

import pytest
from fastmcp import Client
from sqlalchemy import text

from booking_mcp import db, server


def test_health_status_ok(Session):
    body, code = server.health_status()
    assert code == 200
    assert body == {"status": "ok"}


async def test_lifespan_runs_and_keeps_injected_engine(Session):
    # Entering/exiting the server lifespan calls db.dispose(); for an injected
    # (test) engine that's a no-op, so the DB stays usable afterward.
    async with server._lifespan(server.mcp):
        pass
    with db.session() as s:
        assert s.execute(text("SELECT 1")).scalar() == 1


def test_auth_disabled_without_token(monkeypatch):
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    assert server._auth() is None


def test_auth_enabled_with_token(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "s3cret")
    verifier = server._auth()
    assert verifier is not None
    # build_server wires it onto the FastMCP instance.
    assert server.build_server(read_only=True).auth is not None


async def test_workflow_tools_registered_when_agent_url_set(monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_URL", "http://localhost:8000")
    async with Client(server.build_server(read_only=True)) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"book_via_workflow", "get_workflow_run", "decide_workflow_run"} <= names


async def test_workflow_tools_absent_without_agent_url(monkeypatch):
    monkeypatch.delenv("BOOKING_AGENT_URL", raising=False)
    async with Client(server.build_server(read_only=True)) as c:
        names = {t.name for t in await c.list_tools()}
    assert names.isdisjoint({"book_via_workflow", "get_workflow_run", "decide_workflow_run"})


# --- fail-fast: don't expose write tools unauthenticated over HTTP ----------


def test_http_write_without_auth_token_refuses_to_start(monkeypatch):
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="Refusing to start"):
        server.build_server(read_only=False, transport="http")


def test_http_write_with_auth_token_starts(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "s3cret")
    assert server.build_server(read_only=False, transport="http") is not None


def test_http_read_only_starts_without_token(monkeypatch):
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    assert server.build_server(read_only=True, transport="http") is not None


def test_stdio_write_without_token_is_exempt(monkeypatch):
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    # No transport (stdio is client-launched) → the HTTP guard doesn't apply.
    assert server.build_server(read_only=False) is not None


# --- loud warning when direct writes are enabled ----------------------------


def test_write_mode_logs_workflow_bypass_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="booking_mcp"):
        server.build_server(read_only=False)
    assert any(
        "bypass" in r.message and "book_via_workflow" in r.message for r in caplog.records
    )


def test_read_only_logs_no_bypass_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="booking_mcp"):
        server.build_server(read_only=True)
    assert not any("bypass" in r.message for r in caplog.records)
