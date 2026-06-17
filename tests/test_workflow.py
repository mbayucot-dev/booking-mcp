"""The decoupled HTTP bridge to booking-agent, exercised with an httpx
MockTransport (no real network) through the in-memory FastMCP client."""

import json

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from booking_mcp import workflow


def _server(handler):
    mcp = FastMCP(name="wf-test")
    workflow.register(mcp, base_url="http://agent/", transport=httpx.MockTransport(handler))
    return mcp


async def _call(mcp, name, args):
    async with Client(mcp) as c:
        return await c.call_tool(name, args)


async def test_book_via_workflow_posts_message_and_returns_run():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/runs"
        assert json.loads(request.content)["message"] == "book a clean for Jane"
        return httpx.Response(202, json={"run_id": "r1", "status": "running"})

    res = await _call(_server(handler), "book_via_workflow", {"message": "book a clean for Jane"})
    assert res.data.run_id == "r1"
    assert res.data.status == "running"
    assert res.data.node_statuses == {}  # default when omitted by the 202 response


async def test_get_workflow_run_surfaces_approval_card():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/runs/r1"
        return httpx.Response(
            200,
            json={
                "run_id": "r1",
                "status": "paused",
                "node_statuses": {"extract": "completed", "approval": "running"},
                "approval_card": {"summary": "Clean for Jane on 2026-06-20"},
                "final_response": None,
            },
        )

    res = await _call(_server(handler), "get_workflow_run", {"run_id": "r1"})
    assert res.data.status == "paused"
    assert res.data.approval_card["summary"].startswith("Clean for Jane")
    assert res.data.node_statuses["extract"] == "completed"


async def test_decide_workflow_run_approve_then_reject():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"run_id": "r1", "status": "running"})

    srv = _server(handler)

    await _call(srv, "decide_workflow_run", {"run_id": "r1", "approve": True, "by": "agent"})
    assert seen["path"] == "/api/v1/runs/r1/approve"
    assert seen["body"] == {"by": "agent", "reason": None}

    await _call(
        srv, "decide_workflow_run", {"run_id": "r1", "approve": False, "reason": "wrong slot"}
    )
    assert seen["path"] == "/api/v1/runs/r1/reject"
    assert seen["body"] == {"by": None, "reason": "wrong slot"}


async def test_http_error_status_becomes_tool_error():
    def handler(request):
        return httpx.Response(404, text="Run not found")

    with pytest.raises(ToolError, match="404"):
        await _call(_server(handler), "get_workflow_run", {"run_id": "missing"})


async def test_connection_error_becomes_tool_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ToolError, match="Could not reach booking-agent"):
        await _call(_server(handler), "book_via_workflow", {"message": "x"})
