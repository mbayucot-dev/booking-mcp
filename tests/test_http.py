"""HTTP transport integration: auth enforcement, health check, and MCP endpoint wiring.

Tests run against the real ASGI app (Starlette / FastMCP) using TestClient so the
full middleware stack — auth, routing, lifespan — executes identically to production.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from booking_mcp.auth import PII, READ, WORKFLOW, WRITE, hash_key
from booking_mcp.server import build_server

# Plaintext API keys for the scope-gating tests; only their hashes go in API_KEYS.
READ_KEY = "bmcp_read_only_key_for_tests"
WRITE_KEY = "bmcp_read_write_key_for_tests"
PII_KEY = "bmcp_pii_key_for_tests"
WORKFLOW_KEY = "bmcp_workflow_key_for_tests"
WORKFLOW_ONLY_KEY = "bmcp_workflow_only_key_for_tests"  # no read scope
EMPTY_SCOPE_KEY = "bmcp_empty_scope_key_for_tests"      # no scopes at all

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
    """Write-mode HTTP app with a full-access key (all four scopes) for the startup guard."""
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    keys = [
        {
            "hash": hash_key(WRITE_KEY),
            "client_id": "writer",
            "scopes": [READ, WRITE, PII, WORKFLOW],
        }
    ]
    monkeypatch.setenv("API_KEYS", json.dumps(keys))
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
        sid = _mcp_session(client, auth=WRITE_KEY)
        result = _mcp_rpc(client, "tools/list", session_id=sid, auth=WRITE_KEY)
    assert result is not None, "tools/list returned no parseable response"
    names = {t["name"] for t in result["result"]["tools"]}
    missing_write = expected_write - names
    assert not missing_write, f"write tools absent in write-mode tools/list: {missing_write}"
    missing_read = expected_read - names
    assert not missing_read, f"read tools absent in write-mode tools/list: {missing_read}"


# --- write-scope gating over real HTTP (least privilege) ---------------------
#
# These run against the full ASGI stack, so HTTP auth + the write-scope
# AuthMiddleware execute exactly as in production (an in-memory Client bypasses
# HTTP auth, so this gating can only be exercised over the HTTP transport).

WRITE_TOOLS = {"create_booking", "cancel_booking", "reschedule_booking", "book_from_text"}


@pytest.fixture
def http_app_scoped(monkeypatch, Session):
    """Write-mode HTTP app with two API keys: one read-only, one read+write."""
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    keys = [
        {"hash": hash_key(READ_KEY), "client_id": "reader", "scopes": [READ]},
        {"hash": hash_key(WRITE_KEY), "client_id": "writer", "scopes": [READ, WRITE]},
    ]
    monkeypatch.setenv("API_KEYS", json.dumps(keys))
    return build_server(read_only=False, transport="http").http_app(transport="streamable-http")


def test_no_key_is_unauthorized(http_app_scoped):
    """No bearer token → 401 before any tool dispatch."""
    with TestClient(http_app_scoped, raise_server_exceptions=False) as client:
        r = client.post("/mcp", headers=MCP_HEADERS, json=MCP_INIT)
    assert r.status_code == 401


def test_read_key_is_denied_write_tools(http_app_scoped):
    """A read-scoped key must not see write tools in tools/list, and a write tools/call
    is rejected at the auth layer (before the tool body / elicitation runs)."""
    with TestClient(http_app_scoped, raise_server_exceptions=False) as client:
        sid = _mcp_session(client, auth=READ_KEY)
        listed = _mcp_rpc(client, "tools/list", session_id=sid, auth=READ_KEY)
        called = _mcp_rpc(
            client,
            "tools/call",
            {"name": "cancel_booking", "arguments": {"appointment_id": "x"}},
            rpc_id=5,
            session_id=sid,
            auth=READ_KEY,
        )
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names.isdisjoint(WRITE_TOOLS), f"read key saw write tools: {names & WRITE_TOOLS}"
    # The call is rejected — either a JSON-RPC error or an isError tool result.
    assert called is not None
    body = json.dumps(called).lower()
    assert "error" in called or called["result"].get("isError")
    assert "authoriz" in body or "permission" in body or "not found" in body


def test_write_key_is_allowed_write_tools(http_app_scoped):
    """A read+write key sees the write tools (passes the scope filter).

    We assert via tools/list rather than tools/call: the auth check passes for this key, so a
    tools/call would reach the tool body and block on elicitation (the synchronous TestClient
    can't answer it — see test_write_mode_over_http_exposes_write_tools). The deny path above
    short-circuits before the body, so it's safe to call there but not here."""
    with TestClient(http_app_scoped, raise_server_exceptions=False) as client:
        sid = _mcp_session(client, auth=WRITE_KEY)
        listed = _mcp_rpc(client, "tools/list", session_id=sid, auth=WRITE_KEY)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert WRITE_TOOLS <= names, f"write key missing write tools: {WRITE_TOOLS - names}"


# --- pii-scope gating over real HTTP -----------------------------------------
#
# get_client and booking://clients/{email} are tagged `pii` and must be hidden
# from keys without the `pii` scope, regardless of read/write access.

PII_TOOLS = {"get_client"}


@pytest.fixture
def http_app_pii_scoped(monkeypatch, Session):
    """Read-only HTTP app with a read-only key and a read+pii key."""
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    keys = [
        {"hash": hash_key(READ_KEY), "client_id": "reader", "scopes": [READ]},
        {"hash": hash_key(PII_KEY), "client_id": "pii-reader", "scopes": [READ, PII]},
    ]
    monkeypatch.setenv("API_KEYS", json.dumps(keys))
    return build_server(read_only=True).http_app(transport="streamable-http")


def test_read_key_is_denied_pii_tools(http_app_pii_scoped):
    """A read-scoped key must not see get_client (tagged pii) in tools/list."""
    with TestClient(http_app_pii_scoped, raise_server_exceptions=False) as client:
        sid = _mcp_session(client, auth=READ_KEY)
        listed = _mcp_rpc(client, "tools/list", session_id=sid, auth=READ_KEY)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names.isdisjoint(PII_TOOLS), f"read key exposed pii tools: {names & PII_TOOLS}"


def test_pii_key_can_access_pii_tools(http_app_pii_scoped):
    """A read+pii-scoped key must see get_client in tools/list."""
    with TestClient(http_app_pii_scoped, raise_server_exceptions=False) as client:
        sid = _mcp_session(client, auth=PII_KEY)
        listed = _mcp_rpc(client, "tools/list", session_id=sid, auth=PII_KEY)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert PII_TOOLS <= names, f"pii key missing pii tools: {PII_TOOLS - names}"


def test_read_key_is_denied_pii_resource_template(http_app_pii_scoped):
    """A read-scoped key must not see booking://clients/{email} in resources/templates/list."""
    with TestClient(http_app_pii_scoped, raise_server_exceptions=False) as client:
        sid = _mcp_session(client, auth=READ_KEY)
        result = _mcp_rpc(client, "resources/templates/list", rpc_id=3, session_id=sid, auth=READ_KEY)
    uris = {t["uriTemplate"] for t in result["result"]["resourceTemplates"]}
    assert "booking://clients/{email}" not in uris, "read key exposed the clients PII template"


def test_pii_key_can_access_pii_resource_template(http_app_pii_scoped):
    """A read+pii-scoped key must see booking://clients/{email} in resources/templates/list."""
    with TestClient(http_app_pii_scoped, raise_server_exceptions=False) as client:
        sid = _mcp_session(client, auth=PII_KEY)
        result = _mcp_rpc(client, "resources/templates/list", rpc_id=3, session_id=sid, auth=PII_KEY)
    uris = {t["uriTemplate"] for t in result["result"]["resourceTemplates"]}
    assert "booking://clients/{email}" in uris, "pii key missing clients PII template"


# --- workflow-scope gating over real HTTP ------------------------------------
#
# book_via_workflow, get_workflow_run, decide_workflow_run are tagged `workflow`.
# A key without the workflow scope must not see or call them even when
# BOOKING_AGENT_URL is set and the tools are registered.

WORKFLOW_TOOLS = {"book_via_workflow", "get_workflow_run", "decide_workflow_run"}


@pytest.fixture
def http_app_workflow_scoped(monkeypatch, Session):
    """Read-only HTTP app with BOOKING_AGENT_URL set, a read key, and a read+workflow key."""
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("BOOKING_AGENT_URL", "http://booking-agent:8080")
    keys = [
        {"hash": hash_key(READ_KEY), "client_id": "reader", "scopes": [READ]},
        {"hash": hash_key(WORKFLOW_KEY), "client_id": "workflow-user", "scopes": [READ, WORKFLOW]},
    ]
    monkeypatch.setenv("API_KEYS", json.dumps(keys))
    return build_server(read_only=True).http_app(transport="streamable-http")


def test_read_key_is_denied_workflow_tools(http_app_workflow_scoped):
    """A read-scoped key must not see workflow tools even when BOOKING_AGENT_URL is set."""
    with TestClient(http_app_workflow_scoped, raise_server_exceptions=False) as client:
        sid = _mcp_session(client, auth=READ_KEY)
        listed = _mcp_rpc(client, "tools/list", session_id=sid, auth=READ_KEY)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names.isdisjoint(WORKFLOW_TOOLS), f"read key exposed workflow tools: {names & WORKFLOW_TOOLS}"


def test_workflow_key_can_access_workflow_tools(http_app_workflow_scoped):
    """A read+workflow-scoped key must see all three workflow tools in tools/list."""
    with TestClient(http_app_workflow_scoped, raise_server_exceptions=False) as client:
        sid = _mcp_session(client, auth=WORKFLOW_KEY)
        listed = _mcp_rpc(client, "tools/list", session_id=sid, auth=WORKFLOW_KEY)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert WORKFLOW_TOOLS <= names, f"workflow key missing workflow tools: {WORKFLOW_TOOLS - names}"


# --- read-scope enforcement: `read` is a real scope, not implicit ---------------
#
# Any valid key (regardless of other scopes) must NOT see read-tagged tools unless
# it explicitly carries the `read` scope. This closes the gap where a workflow-only
# or empty-scope key could observe ordinary read tools.

_READ_TOOLS = {"search_availability", "list_staff", "daily_schedule", "find_next_available"}


@pytest.fixture
def http_app_read_enforced(monkeypatch, Session):
    """App with BOOKING_AGENT_URL set and keys that deliberately lack the read scope."""
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("BOOKING_AGENT_URL", "http://booking-agent:8080")
    keys = [
        # workflow scope only — no read; should still see workflow tools
        {"hash": hash_key(WORKFLOW_ONLY_KEY), "client_id": "wf-only", "scopes": [WORKFLOW]},
        # no scopes at all — should see nothing
        {"hash": hash_key(EMPTY_SCOPE_KEY), "client_id": "no-scopes", "scopes": []},
    ]
    monkeypatch.setenv("API_KEYS", json.dumps(keys))
    return build_server(read_only=True).http_app(transport="streamable-http")


def test_workflow_only_key_cannot_see_read_tools(http_app_read_enforced):
    """A workflow-only key (no read scope) must not see read-tagged tools, but must see
    workflow tools (workflow tools are tagged only 'workflow', not 'read')."""
    with TestClient(http_app_read_enforced, raise_server_exceptions=False) as client:
        sid = _mcp_session(client, auth=WORKFLOW_ONLY_KEY)
        listed = _mcp_rpc(client, "tools/list", session_id=sid, auth=WORKFLOW_ONLY_KEY)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names.isdisjoint(_READ_TOOLS), f"workflow-only key exposed read tools: {names & _READ_TOOLS}"
    assert WORKFLOW_TOOLS <= names, f"workflow-only key missing workflow tools: {WORKFLOW_TOOLS - names}"


def test_empty_scope_key_sees_no_tools(http_app_read_enforced):
    """A key with no scopes must see no tools at all — every tool requires at least one scope."""
    with TestClient(http_app_read_enforced, raise_server_exceptions=False) as client:
        sid = _mcp_session(client, auth=EMPTY_SCOPE_KEY)
        listed = _mcp_rpc(client, "tools/list", session_id=sid, auth=EMPTY_SCOPE_KEY)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert not names, f"empty-scope key should see no tools, saw: {names}"
