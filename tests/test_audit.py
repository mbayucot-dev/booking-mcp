"""Audit-log assertions: sensitive write operations must emit structured log records
so that operators can reconstruct what happened in production.

Tests verify: that the right logger, level, and key fields appear for each
mutation; that internal DB errors in resources are logged server-side without
leaking details to the caller; and that upstream workflow errors are logged
rather than forwarded to the client.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from booking_mcp import workflow
from booking_mcp.server import build_server
from tests.conftest import seed_client, seed_staff

SEED_DATE = "2026-06-20"


def _confirm(value: bool):
    async def handler(message, response_type, params, context):
        return value

    return handler


async def _call(server, tool: str, args: dict, *, confirm: bool = True):
    async with Client(server, elicitation_handler=_confirm(confirm)) as c:
        return (await c.call_tool(tool, args)).data


# ── write-tool audit logs ────────────────────────────────────────────────────


async def test_create_booking_emits_audit_log(Session, caplog):
    seed_staff(Session)
    with caplog.at_level(logging.INFO, logger="booking_mcp.tools"):
        await _call(
            build_server(read_only=False),
            "create_booking",
            {
                "customer_name": "Audit User",
                "email": "audit@example.com",
                "service": "cleaning",
                "date": SEED_DATE,
                "time": "07:00",
            },
        )
    records = [r for r in caplog.records if r.name == "booking_mcp.tools"]
    assert any("booking created" in r.message for r in records)
    created = next(r for r in records if "booking created" in r.message)
    assert "audit@example.com" in created.message
    assert "cleaning" in created.message
    assert SEED_DATE in created.message
    assert "idempotent=False" in created.message


async def test_create_booking_idempotent_logs_idempotent_true(Session, caplog):
    seed_staff(Session)
    server = build_server(read_only=False)
    args = {
        "customer_name": "Repeat",
        "email": "repeat@example.com",
        "service": "cleaning",
        "date": SEED_DATE,
        "time": "07:30",
    }
    await _call(server, "create_booking", args)
    with caplog.at_level(logging.INFO, logger="booking_mcp.tools"):
        await _call(server, "create_booking", args)
    assert any(
        "booking created" in r.message and "idempotent=True" in r.message
        for r in caplog.records
        if r.name == "booking_mcp.tools"
    )


# ── audit records include correlation identifiers ────────────────────────────


async def test_create_booking_audit_includes_request_id(Session, caplog):
    """Write-tool audit logs must contain request_id so ops can correlate events."""
    seed_staff(Session)
    with caplog.at_level(logging.INFO, logger="booking_mcp.tools"):
        await _call(
            build_server(read_only=False),
            "create_booking",
            {
                "customer_name": "Corr User",
                "email": "corr@example.com",
                "service": "cleaning",
                "date": SEED_DATE,
                "time": "07:15",
            },
        )
    created = next(
        (r for r in caplog.records if r.name == "booking_mcp.tools" and "booking created" in r.message),
        None,
    )
    assert created is not None
    assert "request_id=" in created.message
    assert "client_id=" in created.message


# ── PII resource access is audited ───────────────────────────────────────────


async def test_get_client_tool_emits_pii_access_log(Session, caplog):
    """get_client must emit a pii_access log with request_id so every data pull is traceable."""
    seed_client(Session)
    with caplog.at_level(logging.INFO, logger="booking_mcp.tools"):
        async with Client(build_server()) as c:
            await c.call_tool("get_client", {"email": "priya@example.com"})
    records = [r for r in caplog.records if "pii_access" in r.message]
    assert records, "get_client must emit a pii_access audit log"
    assert "priya@example.com" in records[0].message
    assert "request_id=" in records[0].message


# ── book_from_text extraction quality is logged ──────────────────────────────


async def test_book_from_text_logs_extraction_quality(Session, caplog):
    """book_from_text must log extracted fields and null count so low-confidence
    extractions are visible in the audit trail."""
    from tests.test_tools import _book_from_text

    seed_staff(Session)
    with caplog.at_level(logging.INFO, logger="booking_mcp.tools"):
        await _book_from_text(
            build_server(read_only=False),
            "Book Log User for cleaning",
            payload=(
                '{"customer_name":"Log User","email":"log@example.com","service":"cleaning",'
                '"date":"2026-06-20","time":"06:00","phone":null,"address":null}'
            ),
        )
    records = [r for r in caplog.records if "book_from_text extraction ok" in r.message]
    assert records, "book_from_text must emit an extraction-quality audit log"
    assert "request_id=" in records[0].message
    assert "null_fields=" in records[0].message


async def test_cancel_booking_emits_audit_log(Session, caplog):
    ids = seed_staff(Session)
    server = build_server(read_only=False)
    appt_id = (
        await _call(
            server,
            "create_booking",
            {
                "customer_name": "C",
                "email": "c@example.com",
                "service": "cleaning",
                "date": SEED_DATE,
                "time": "08:30",
                "staff_id": ids["Alex Taylor"],
            },
        )
    ).appointment_id

    with caplog.at_level(logging.INFO, logger="booking_mcp.tools"):
        await _call(server, "cancel_booking", {"appointment_id": appt_id})

    records = [r for r in caplog.records if "booking cancelled" in r.message]
    assert records, "cancel_booking must emit an audit log record"
    assert appt_id in records[0].message
    assert "cancelled=True" in records[0].message


async def test_reschedule_booking_emits_audit_log(Session, caplog):
    seed_staff(Session)
    server = build_server(read_only=False)
    appt_id = (
        await _call(
            server,
            "create_booking",
            {
                "customer_name": "R",
                "email": "r@example.com",
                "service": "cleaning",
                "date": SEED_DATE,
                "time": "09:30",
            },
        )
    ).appointment_id

    with caplog.at_level(logging.INFO, logger="booking_mcp.tools"):
        await _call(
            server,
            "reschedule_booking",
            {"appointment_id": appt_id, "date": SEED_DATE, "time": "10:30"},
        )

    records = [r for r in caplog.records if "booking rescheduled" in r.message]
    assert records, "reschedule_booking must emit an audit log record"
    assert appt_id in records[0].message


async def test_add_customer_preference_emits_audit_log(Session, caplog):
    seed_client(Session)
    server = build_server(read_only=False)
    with caplog.at_level(logging.INFO, logger="booking_mcp.tools"):
        await _call(server, "add_customer_preference", {"email": "priya@example.com", "note": "no fragrance"})
    records = [r for r in caplog.records if "preference saved" in r.message]
    assert records, "add_customer_preference must emit an audit log record"
    assert "priya@example.com" in records[0].message
    assert "request_id=" in records[0].message


# ── resource DB errors are logged, not leaked ────────────────────────────────


async def test_resource_db_error_is_logged_server_side(Session, caplog):
    from sqlalchemy.exc import OperationalError

    from booking_mcp.resources import _guard

    with caplog.at_level(logging.ERROR, logger="booking_mcp.resources"):
        with pytest.raises(RuntimeError):
            with _guard():
                raise OperationalError("stmt", {}, Exception("secret DB detail"))

    records = [r for r in caplog.records if r.name == "booking_mcp.resources"]
    assert records, "DB error must be logged at ERROR level"
    assert any("database error in resource" in r.message for r in records)


# ── workflow upstream errors are logged, not forwarded ───────────────────────


async def test_workflow_upstream_error_is_logged_not_forwarded(caplog):
    def handler(request):
        return httpx.Response(500, text="SELECT * FROM internal_secrets")

    mcp = FastMCP(name="wf-audit-test")
    workflow.register(
        mcp, base_url="http://agent/", transport=httpx.MockTransport(handler)
    )

    with caplog.at_level(logging.ERROR, logger="booking_mcp.workflow"):
        with pytest.raises(ToolError) as exc_info:
            async with Client(mcp) as c:
                await c.call_tool("get_workflow_run", {"run_id": "x"})

    # The upstream body must appear in the server log (for ops debugging)...
    assert any(
        "internal_secrets" in r.message
        for r in caplog.records
        if r.name == "booking_mcp.workflow"
    )
    # ...but NOT in the error returned to the client.
    assert "internal_secrets" not in str(exc_info.value)
