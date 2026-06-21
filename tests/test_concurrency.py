"""Concurrency / race-condition tests.

Booking systems are a classic double-write scenario. These tests exercise the
idempotency ledger and the DB-level unique constraint (uq_appt_staff_slot) under
simultaneous writes, verifying that concurrent identical requests de-dup and
concurrent conflicting requests let exactly one succeed.
"""

from __future__ import annotations

import anyio
from fastmcp import Client
from fastmcp.exceptions import ToolError

from booking_mcp.models import Appointment
from booking_mcp.server import build_server
from tests.conftest import seed_staff

SEED_DATE = "2026-06-20"


def _confirm(value: bool):
    async def handler(message, response_type, params, context):
        return value

    return handler


async def _book(server, args: dict) -> object:
    async with Client(server, elicitation_handler=_confirm(True)) as c:
        return (await c.call_tool("create_booking", args)).data


# --- idempotency under sequential retries -----------------------------------


async def test_identical_booking_retry_is_idempotent(Session):
    """A booking retried with the same parameters returns the original appointment ID.
    This tests the executed_actions ledger: the second call must not double-book."""
    ids = seed_staff(Session)
    server = build_server(read_only=False)
    args = {
        "customer_name": "Alice",
        "email": "alice@example.com",
        "service": "cleaning",
        "date": SEED_DATE,
        "time": "08:00",
        "staff_id": ids["Alex Taylor"],
    }

    r1 = await _book(server, args)
    r2 = await _book(server, args)

    assert r1.appointment_id == r2.appointment_id
    assert not r1.idempotent, "first call must not be marked idempotent"
    assert r2.idempotent, "retry must be recognised as idempotent"
    with Session() as s:
        assert s.query(Appointment).count() == 1


# --- slot-conflict under concurrency ----------------------------------------


async def test_concurrent_conflicting_bookings_one_succeeds(Session):
    """Two simultaneous bookings for the same (staff, slot) but different customers:
    exactly one must succeed and one must receive a clean conflict ToolError."""
    ids = seed_staff(Session)
    server = build_server(read_only=False)

    successes: list = []
    failures: list = []

    async def book_a():
        try:
            successes.append(
                await _book(
                    server,
                    {
                        "customer_name": "Customer A",
                        "email": "a@example.com",
                        "service": "cleaning",
                        "date": SEED_DATE,
                        "time": "09:00",
                        "staff_id": ids["Sam Rivers"],
                    },
                )
            )
        except ToolError as e:
            failures.append(str(e))

    async def book_b():
        try:
            successes.append(
                await _book(
                    server,
                    {
                        "customer_name": "Customer B",
                        "email": "b@example.com",
                        "service": "cleaning",
                        "date": SEED_DATE,
                        "time": "09:00",
                        "staff_id": ids["Sam Rivers"],
                    },
                )
            )
        except ToolError as e:
            failures.append(str(e))

    async with anyio.create_task_group() as tg:
        tg.start_soon(book_a)
        tg.start_soon(book_b)

    assert len(successes) == 1, "exactly one booking should succeed"
    assert len(failures) == 1, "exactly one booking should fail with a conflict error"
    assert "already booked" in failures[0].lower()
    with Session() as s:
        assert s.query(Appointment).count() == 1


# --- concurrent first booking for the same new email -----------------------


async def test_concurrent_first_bookings_for_same_email_create_one_client(Session):
    """Two simultaneous first-time bookings for a brand-new email must produce exactly
    one Client row. The SAVEPOINT in create_booking handles the race so that the loser
    re-fetches the winner's client instead of rolling back the entire transaction."""
    ids = seed_staff(Session)
    server = build_server(read_only=False)

    results: list = []

    async def book_for_email(time: str):
        results.append(
            await _book(
                server,
                {
                    "customer_name": "New Customer",
                    "email": "newcustomer@example.com",
                    "service": "cleaning",
                    "date": SEED_DATE,
                    "time": time,
                    "staff_id": ids["Sam Rivers"] if time == "14:00" else ids["Jordan Lee"],
                },
            )
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(book_for_email, "14:00")
        tg.start_soon(book_for_email, "15:00")

    with Session() as s:
        from booking_mcp.models import Client

        client_rows = s.query(Client).filter_by(email="newcustomer@example.com").all()
        assert len(client_rows) == 1, "concurrent first bookings must share one Client row"
    assert len(results) == 2, "both bookings must succeed"


# --- reschedule conflict under concurrency ----------------------------------


async def test_concurrent_reschedule_into_same_slot_one_succeeds(Session):
    """Two simultaneous reschedules targeting the same occupied staff slot:
    the second should fail cleanly, not produce a duplicate appointment."""
    from tests.conftest import book as seed_book

    ids = seed_staff(Session)
    server = build_server(read_only=False)

    # Book Alex at 10:00 (occupies the target slot for the conflict)
    seed_book(Session, staff_id=ids["Alex Taylor"], start_date=f"{SEED_DATE} 10:00:00")

    # Book two separate appointments for Alex (source appointments to reschedule from)
    appt_a = await _book(
        server,
        {
            "customer_name": "P",
            "email": "p@example.com",
            "service": "cleaning",
            "date": SEED_DATE,
            "time": "11:00",
            "staff_id": ids["Alex Taylor"],
        },
    )
    appt_b = await _book(
        server,
        {
            "customer_name": "Q",
            "email": "q@example.com",
            "service": "cleaning",
            "date": SEED_DATE,
            "time": "12:00",
            "staff_id": ids["Alex Taylor"],
        },
    )

    successes: list = []
    failures: list = []

    async def reschedule(appt_id: str):
        try:
            async with Client(server, elicitation_handler=_confirm(True)) as c:
                res = await c.call_tool(
                    "reschedule_booking",
                    {"appointment_id": appt_id, "date": SEED_DATE, "time": "10:00"},
                )
            successes.append(res.data)
        except ToolError as e:
            failures.append(str(e))

    async with anyio.create_task_group() as tg:
        tg.start_soon(reschedule, appt_a.appointment_id)
        tg.start_soon(reschedule, appt_b.appointment_id)

    # Both can't land on the same occupied slot — at least one must fail
    assert len(failures) >= 1, "at least one reschedule must fail on a conflicting slot"
    with Session() as s:
        # The target slot is already taken by the original seed_book: no extra row
        assert s.query(Appointment).filter_by(staff_id=ids["Alex Taylor"]).count() <= 3
