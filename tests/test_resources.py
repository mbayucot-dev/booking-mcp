"""Resources + prompts via the in-memory FastMCP client."""

import json
import logging

import pytest
from fastmcp import Client
from sqlalchemy.exc import OperationalError

from booking_mcp.resources import _guard
from booking_mcp.server import build_server
from tests.conftest import book, seed_client, seed_staff

SEED_DATE = "2026-06-20"


def _json(result):
    """read_resource returns a list of contents; parse the first as JSON."""
    return json.loads(result[0].text)


async def test_staff_resources(Session):
    ids = seed_staff(Session)
    async with Client(build_server()) as c:
        listing = _json(await c.read_resource("booking://staff"))
        assert {s["name"] for s in listing} == {"Alex Taylor", "Sam Rivers", "Jordan Lee"}

        one = _json(await c.read_resource(f"booking://staff/{ids['Alex Taylor']}"))
        assert one["name"] == "Alex Taylor"
        assert "cleaning" in one["skills"]

        missing = _json(await c.read_resource("booking://staff/nope"))
        assert missing is None  # unknown staff id


async def test_schedule_resource(Session):
    ids = seed_staff(Session)
    book(
        Session, staff_id=ids["Sam Rivers"], start_date=f"{SEED_DATE} 09:00:00", service="gardening"
    )
    async with Client(build_server()) as c:
        sched = _json(await c.read_resource(f"booking://schedule/{SEED_DATE}"))
    assert sched["count"] == 1
    assert sched["appointments"][0]["service"] == "gardening"


async def test_client_resource_redacts_pii_by_default(Session):
    """Default REDACT_PII=true: phone masked to last 4 digits, address replaced."""
    seed_client(Session)
    async with Client(build_server()) as c:
        data = _json(await c.read_resource("booking://clients/priya@example.com"))
        unknown = _json(await c.read_resource("booking://clients/nobody@example.com"))
    assert data["name"] == "Priya Nair"
    assert data["memories"][0]["content"]["note"] == "calm with anxious dogs"
    assert unknown is None
    assert data["phone"] == "***1222", "phone must show only last 4 digits"
    assert data["address"] == "[REDACTED]"
    assert data["contacts"][0]["phone"] == "***1222"


async def test_client_resource_shows_full_pii_when_redact_disabled(Session, monkeypatch):
    """REDACT_PII=false: phone/address returned verbatim."""
    monkeypatch.setenv("REDACT_PII", "false")
    seed_client(Session)
    async with Client(build_server()) as c:
        data = _json(await c.read_resource("booking://clients/priya@example.com"))
    assert data["phone"] == "0400111222"
    assert data["address"] == "5 Park Rd"


async def test_client_resource_emits_pii_access_audit_log(Session, caplog):
    """Reading the client resource must emit a pii_access log entry."""
    seed_client(Session)
    with caplog.at_level(logging.INFO, logger="booking_mcp.resources"):
        async with Client(build_server()) as c:
            await c.read_resource("booking://clients/priya@example.com")
    records = [r for r in caplog.records if "pii_access" in r.message]
    assert records, "client resource must emit a pii_access audit log"
    assert "priya@example.com" in records[0].message
    assert "redacted=True" in records[0].message


def test_resource_guard_sanitizes_db_error():
    """SQLAlchemy errors are caught by _guard() and re-raised as a clean RuntimeError."""
    with pytest.raises(RuntimeError, match="A database error occurred"):
        with _guard():
            raise OperationalError("stmt", {}, Exception("internal DB detail"))


def test_resource_guard_passes_tool_error_through():
    """ToolErrors raised inside a resource are re-raised as-is (not double-wrapped)."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="expected error"):
        with _guard():
            raise ToolError("expected error")


# --- URI encoding: emails with reserved characters --------------------------
# The booking://clients/{email} template contains emails, which may include
# RFC-reserved characters (+, @). These tests document FastMCP's URI template
# matching behaviour for percent-encoded paths.


async def test_client_resource_email_with_plus_sign(Session):
    """Email with a + in the local part (e.g. jane+vip@example.com) must be
    accessible using the literal URI — + is not percent-encoded in a URI path."""
    seed_client(Session, email="jane+vip@example.com", name="Jane VIP")
    async with Client(build_server()) as c:
        data = _json(await c.read_resource("booking://clients/jane+vip@example.com"))
    assert data is not None, "client with + in email must be found via literal URI"
    assert data["name"] == "Jane VIP"


async def test_client_resource_encoded_at_sign_resolves_to_same_client(Session):
    """booking://clients/jane%40example.com (encoded @) must resolve to the same
    client as booking://clients/jane@example.com.

    This documents FastMCP's URI template reserved-character handling. If FastMCP
    does not percent-decode path segments before matching the {email} variable, this
    lookup returns None (a known FastMCP URI-template bug)."""
    seed_client(Session, email="jane@example.com", name="Jane Doe")
    async with Client(build_server()) as c:
        literal = _json(await c.read_resource("booking://clients/jane@example.com"))
        encoded = _json(await c.read_resource("booking://clients/jane%40example.com"))
    assert literal is not None, "literal @ form must resolve"
    assert encoded == literal, (
        "encoded %40 form must resolve to the same client as the literal @ form; "
        "if this fails, FastMCP does not percent-decode URI path segments before template matching"
    )


async def test_client_resource_fully_encoded_email_with_plus(Session):
    """booking://clients/jane%2Bvip%40example.com (encoded + and @) must resolve
    to jane+vip@example.com.

    Documents FastMCP behaviour for fully percent-encoded emails."""
    seed_client(Session, email="jane+vip@example.com", name="Jane VIP Encoded")
    async with Client(build_server()) as c:
        literal = _json(await c.read_resource("booking://clients/jane+vip@example.com"))
        encoded = _json(await c.read_resource("booking://clients/jane%2Bvip%40example.com"))
    assert literal is not None, "literal form must resolve"
    assert encoded == literal, (
        "fully encoded form %2B/%40 must resolve to the same client as the literal form; "
        "if this fails, FastMCP does not percent-decode URI template variables"
    )


async def test_prompts_are_registered(Session):
    async with Client(build_server()) as c:
        prompts = {p.name for p in await c.list_prompts()}
        assert {"book_cleaning", "summarize_schedule"} <= prompts
        rendered = await c.get_prompt(
            "book_cleaning",
            {
                "customer": "John",
                "service": "cleaning",
                "date": SEED_DATE,
                "time": "10:00",
                "email": "john@example.com",
            },
        )
        summary = await c.get_prompt("summarize_schedule", {"date": SEED_DATE})
    text = rendered.messages[0].content.text
    assert "John" in text and "cleaning" in text and SEED_DATE in text
    assert SEED_DATE in summary.messages[0].content.text
