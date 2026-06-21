"""HTTP transport integration: auth enforcement, health check, and MCP endpoint wiring.

Tests run against the real ASGI app (Starlette / FastMCP) using TestClient so the
full middleware stack — auth, routing, lifespan — executes identically to production.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from booking_mcp.server import build_server

# Minimal valid MCP initialize request (streamable-http transport).
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
MCP_INIT = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0"},
    },
}


@pytest.fixture
def http_app(Session):
    """Read-only HTTP ASGI app backed by real Postgres (from the Session fixture)."""
    return build_server(read_only=True).http_app(transport="streamable-http")


@pytest.fixture
def http_app_with_auth(monkeypatch, Session):
    """HTTP app with AUTH_TOKEN set — /mcp requires a valid Bearer token."""
    monkeypatch.setenv("AUTH_TOKEN", "test-secret")
    return build_server(read_only=True).http_app(transport="streamable-http")


# --- health endpoint -------------------------------------------------------


def test_health_returns_200_when_db_is_up(http_app):
    with TestClient(http_app) as client:
        r = client.get("/_health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_is_accessible_without_auth_token(http_app_with_auth):
    """/_health is a custom route; FastMCP docs state custom routes bypass auth."""
    with TestClient(http_app_with_auth) as client:
        r = client.get("/_health")
    assert r.status_code == 200


# --- auth enforcement on /mcp ----------------------------------------------


def test_mcp_no_token_401_when_auth_configured(http_app_with_auth):
    with TestClient(http_app_with_auth, raise_server_exceptions=False) as client:
        r = client.post("/mcp", headers=MCP_HEADERS, json=MCP_INIT)
    assert r.status_code == 401


def test_mcp_bad_token_401(http_app_with_auth):
    with TestClient(http_app_with_auth, raise_server_exceptions=False) as client:
        r = client.post(
            "/mcp",
            headers={**MCP_HEADERS, "Authorization": "Bearer WRONG_TOKEN"},
            json=MCP_INIT,
        )
    assert r.status_code == 401


def test_mcp_correct_token_200(http_app_with_auth):
    with TestClient(http_app_with_auth, raise_server_exceptions=False) as client:
        r = client.post(
            "/mcp",
            headers={**MCP_HEADERS, "Authorization": "Bearer test-secret"},
            json=MCP_INIT,
        )
    assert r.status_code == 200


def test_mcp_no_auth_configured_allows_access(http_app):
    """Without AUTH_TOKEN the MCP endpoint is open (read-only is the protection)."""
    with TestClient(http_app, raise_server_exceptions=False) as client:
        r = client.post("/mcp", headers=MCP_HEADERS, json=MCP_INIT)
    assert r.status_code == 200


def test_write_http_without_token_refuses_to_start(monkeypatch):
    """build_server fails fast rather than exposing unauthenticated write access over HTTP."""
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="Refusing to start"):
        build_server(read_only=False, transport="http")


# --- MCP JSON-RPC helpers ----------------------------------------------------


def _sse_result(r, *, rpc_id: int | None = None) -> dict | None:
    """Parse the first matching JSON-RPC object from a streamable-http response.

    FastMCP may respond with text/event-stream (SSE data: lines) or application/json
    depending on content negotiation; this handles both.
    """
    ct = r.headers.get("content-type", "")
    candidates: list[dict] = []
    if "event-stream" in ct:
        for line in r.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        candidates.append(json.loads(payload))
                    except json.JSONDecodeError:
                        pass
    elif r.text:
        try:
            obj = r.json()
            if isinstance(obj, dict):
                candidates.append(obj)
        except Exception:
            pass
    if rpc_id is not None:
        return next((c for c in candidates if c.get("id") == rpc_id), None)
    return candidates[0] if candidates else None


def _mcp_session(client: TestClient, *, auth: str | None = None) -> str | None:
    """Send MCP initialize; return the mcp-session-id header value (may be None)."""
    headers = {**MCP_HEADERS}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    r = client.post("/mcp", headers=headers, json=MCP_INIT)
    assert r.status_code == 200, f"initialize failed: {r.status_code} {r.text[:200]}"
    return r.headers.get("mcp-session-id")


def _mcp_rpc(
    client: TestClient,
    method: str,
    params: dict | None = None,
    *,
    rpc_id: int = 2,
    session_id: str | None = None,
    auth: str | None = None,
) -> dict | None:
    """POST one JSON-RPC request to /mcp and return the parsed response object."""
    headers = {**MCP_HEADERS}
    if session_id:
        headers["mcp-session-id"] = session_id
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    r = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "method": method, "id": rpc_id, "params": params or {}},
    )
    assert r.status_code == 200, f"{method} failed: {r.status_code} {r.text[:200]}"
    return _sse_result(r, rpc_id=rpc_id)


# --- MCP protocol: tools/list ------------------------------------------------


def test_tools_list_over_http_returns_read_tools(http_app):
    """tools/list over streamable-HTTP must expose all read-only tools and hide write tools."""
    expected_read = {
        "search_availability",
        "list_staff",
        "daily_schedule",
        "get_client",
        "find_next_available",
    }
    write_tools = {"create_booking", "cancel_booking", "reschedule_booking", "book_from_text"}
    with TestClient(http_app) as client:
        sid = _mcp_session(client)
        result = _mcp_rpc(client, "tools/list", session_id=sid)
    assert result is not None, "tools/list returned no parseable response"
    names = {t["name"] for t in result["result"]["tools"]}
    missing = expected_read - names
    assert not missing, f"read tools absent in tools/list: {missing}"
    exposed_write = write_tools & names
    assert not exposed_write, f"write tools exposed in read-only HTTP mode: {exposed_write}"


# --- MCP protocol: resources/templates/list ----------------------------------


def test_resources_templates_list_over_http_returns_booking_templates(http_app):
    """resources/templates/list must expose the URI-template resources including the PII resource."""
    expected = {
        "booking://staff/{staff_id}",
        "booking://schedule/{date}",
        "booking://clients/{email}",
    }
    with TestClient(http_app) as client:
        sid = _mcp_session(client)
        result = _mcp_rpc(client, "resources/templates/list", rpc_id=3, session_id=sid)
    assert result is not None, "resources/templates/list returned no parseable response"
    uris = {t["uriTemplate"] for t in result["result"]["resourceTemplates"]}
    missing = expected - uris
    assert not missing, f"resource templates absent from listing: {missing}"


# --- MCP protocol: prompts/list ----------------------------------------------


def test_prompts_list_over_http_returns_expected_prompts(http_app):
    """prompts/list must return the two registered booking prompts."""
    with TestClient(http_app) as client:
        sid = _mcp_session(client)
        result = _mcp_rpc(client, "prompts/list", rpc_id=4, session_id=sid)
    assert result is not None, "prompts/list returned no parseable response"
    names = {p["name"] for p in result["result"]["prompts"]}
    assert {"book_cleaning", "summarize_schedule"} <= names


# --- MCP protocol: write tool without elicitation support --------------------


@pytest.fixture
def http_app_write(monkeypatch, Session):
    """Write-mode HTTP app; AUTH_TOKEN is required to pass the startup guard."""
    monkeypatch.setenv("AUTH_TOKEN", "write-secret")
    return build_server(read_only=False, transport="http").http_app(transport="streamable-http")


def test_write_mode_over_http_exposes_write_tools(http_app_write):
    """tools/list in write mode must include write tools; all read tools must remain present.

    Note: the full elicitation failure path (tools/call → isError on unsupported client) cannot
    be tested via Starlette TestClient because the MCP SDK awaits the elicitation response
    indefinitely without checking client capabilities first — a synchronous test client will
    deadlock. That path is covered by the in-memory client tests in test_tools.py."""
    expected_write = {"create_booking", "cancel_booking", "reschedule_booking", "book_from_text"}
    expected_read = {
        "search_availability",
        "list_staff",
        "daily_schedule",
        "get_client",
        "find_next_available",
    }
    with TestClient(http_app_write) as client:
        sid = _mcp_session(client, auth="write-secret")
        result = _mcp_rpc(client, "tools/list", session_id=sid, auth="write-secret")
    assert result is not None, "tools/list returned no parseable response"
    names = {t["name"] for t in result["result"]["tools"]}
    missing_write = expected_write - names
    assert not missing_write, f"write tools absent in write-mode tools/list: {missing_write}"
    missing_read = expected_read - names
    assert not missing_read, f"read tools absent in write-mode tools/list: {missing_read}"
