"""booking-mcp — a standalone FastMCP server over the shared booking datastore.

Run locally (stdio):           python -m booking_mcp.server
Inspect:                       fastmcp dev src/booking_mcp/server.py
Register in an MCP client with command ``booking-mcp`` (see README).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from sqlalchemy import text
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import db, prompts, resources, tools, workflow
from .config import get_settings

logging.basicConfig(level=get_settings().log_level.upper())
log = logging.getLogger("booking_mcp")

INSTRUCTIONS = (
    "Tools and resources for a cleaning-booking datastore: search staff availability "
    "(skill + free slot + proximity), read schedules and clients, and (when enabled) "
    "create bookings. Dates are ISO YYYY-MM-DD and times are 24h HH:MM."
)


@asynccontextmanager
async def _lifespan(server: FastMCP):
    yield
    db.dispose()


def health_status() -> tuple[dict, int]:
    """Readiness: the DB is reachable. Returned by the /_health route."""
    try:
        with db.session() as s:
            s.execute(text("SELECT 1"))
        return {"status": "ok"}, 200
    except Exception:
        log.exception("readiness check failed")
        return {"status": "unavailable"}, 503


def _auth():
    """Bearer-token auth for the HTTP transport when AUTH_TOKEN is configured."""
    token = get_settings().auth_token
    if not token:
        return None
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    return StaticTokenVerifier(tokens={token: {"client_id": "booking-mcp"}})


def _check_write_auth(*, transport: str | None, read_only: bool) -> None:
    """Refuse to start an unauthenticated, write-enabled HTTP server (write tools bypass
    booking-agent's approval workflow). stdio is client-launched, so it's exempt."""
    if (
        transport in ("http", "streamable-http", "sse")
        and not read_only
        and not get_settings().auth_token
    ):
        raise RuntimeError(
            "Refusing to start: write tools are enabled (READ_ONLY=false) over HTTP with no "
            "AUTH_TOKEN set — that exposes write access unauthenticated. Set AUTH_TOKEN, or "
            "run with READ_ONLY=true."
        )


def build_server(read_only: bool | None = None, *, transport: str | None = None) -> FastMCP:
    """Construct the MCP server. Read tools/resources/prompts are always present;
    write tools are registered only when not read-only. ``transport`` is used only to
    fail-fast on an unauthenticated write-enabled HTTP deployment."""
    settings = get_settings()
    ro = settings.read_only if read_only is None else read_only
    _check_write_auth(transport=transport, read_only=ro)
    if not ro:
        log.warning(
            "WRITE MODE ENABLED (READ_ONLY=false): direct write tools bypass booking-agent's "
            "human-approval workflow. Use book_via_workflow for the approval-gated safe path."
        )
    mcp = FastMCP(name="booking-mcp", instructions=INSTRUCTIONS, lifespan=_lifespan, auth=_auth())
    resources.register(mcp)
    prompts.register(mcp)
    tools.register(mcp, read_only=ro)
    if settings.booking_agent_url:
        # Decoupled HTTP bridge to booking-agent's full approval workflow.
        workflow.register(
            mcp,
            base_url=settings.booking_agent_url,
            timeout=settings.booking_agent_timeout,
        )

    @mcp.custom_route("/_health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        body, code = health_status()
        return JSONResponse(body, status_code=code)

    return mcp


# Module-level instance for `fastmcp run booking_mcp.server:mcp`.
# Hardcoded read-only: no transport context is available at import time, so
# _check_write_auth cannot fire. Write mode requires explicit build_server() calls
# (main() for stdio, the Dockerfile CMD for HTTP with transport= and AUTH_TOKEN).
mcp = build_server(read_only=True)


def main() -> None:  # pragma: no cover - stdio entrypoint
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
