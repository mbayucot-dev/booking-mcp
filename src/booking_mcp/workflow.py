"""Decoupled bridge to booking-agent's REST API.

These tools drive booking-agent's full workflow — including its human-approval gate —
over HTTP, without importing booking-agent. They are the safe counterpart to the
direct-write tools in ``tools.py`` (which bypass approval). Registered only when
``BOOKING_AGENT_URL`` is configured.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .schemas import WorkflowRun

log = logging.getLogger("booking_mcp.workflow")

# These tools reach an external service → openWorldHint. Not "destructive" in the DB
# sense, since the approval gate guards the actual write.
_READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
_ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def register(
    mcp: FastMCP,
    *,
    base_url: str,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Register the workflow-bridge tools against ``base_url`` (booking-agent's REST root).
    ``transport`` is an injection seam for tests (httpx MockTransport)."""
    base = base_url.rstrip("/")

    async def _request(method: str, path: str, *, json: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(
                base_url=base, timeout=timeout, transport=transport
            ) as client:
                resp = await client.request(method, path, json=json)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            detail = e.response.text.strip()
            raise ToolError(
                f"booking-agent returned HTTP {e.response.status_code} for {method} {path}"
                + (f": {detail}" if detail else "")
            ) from e
        except httpx.HTTPError as e:
            raise ToolError(f"Could not reach booking-agent at {base}: {e}") from e

    @mcp.tool(annotations=_ACT, tags={"workflow"})
    async def book_via_workflow(
        message: Annotated[
            str, Field(description="Natural-language booking request", min_length=1)
        ],
    ) -> WorkflowRun:
        """Start booking-agent's FULL workflow for a natural-language request.

        Unlike create_booking / book_from_text (which write directly), this goes
        through booking-agent's human-approval gate. Poll get_workflow_run to see
        the approval card, then approve/reject with decide_workflow_run."""
        data = await _request("POST", "/api/v1/runs", json={"message": message})
        log.info("started workflow run %s", data.get("run_id"))
        return WorkflowRun(**data)

    @mcp.tool(annotations=_READ, tags={"workflow"})
    async def get_workflow_run(
        run_id: Annotated[str, Field(description="Run id from book_via_workflow")],
    ) -> WorkflowRun:
        """Current state of a workflow run: per-node statuses, the approval card
        (while paused awaiting a decision), and the final response (when done)."""
        return WorkflowRun(**await _request("GET", f"/api/v1/runs/{run_id}"))

    @mcp.tool(annotations=_ACT, tags={"workflow"})
    async def decide_workflow_run(
        run_id: Annotated[str, Field(description="Run id awaiting approval")],
        approve: Annotated[bool, Field(description="True to approve, False to reject")],
        by: Annotated[str | None, Field(description="Who is deciding")] = None,
        reason: Annotated[str | None, Field(description="Reason (esp. for a reject)")] = None,
    ) -> WorkflowRun:
        """Approve or reject a paused workflow run — the human-in-the-loop decision
        that lets booking-agent commit (or abandon) the booking."""
        path = f"/api/v1/runs/{run_id}/{'approve' if approve else 'reject'}"
        return WorkflowRun(**await _request("POST", path, json={"by": by, "reason": reason}))
