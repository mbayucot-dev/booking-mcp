"""Tools exercised through the in-memory FastMCP client against real Postgres."""

from datetime import datetime

import anyio
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from booking_mcp.models import Appointment
from booking_mcp.models import Client as ClientRow
from booking_mcp.server import build_server
from tests.conftest import book, seed_client, seed_staff

SEED_DATE = "2026-06-20"


def _handler(value: bool):
    async def handler(message, response_type, params, context):
        return value  # elicitation reply: True = confirm, False = decline

    return handler


async def _call(server, name, args, *, confirm: bool = True):
    """Call a tool through the in-memory client, auto-answering any confirmation with `confirm`."""
    async with Client(server, elicitation_handler=_handler(confirm)) as c:
        return await c.call_tool(name, args)


async def test_search_availability_filters_by_skill(Session):
    seed_staff(Session)
    res = await _call(
        build_server(read_only=True),
        "search_availability",
        {"service": "cleaning", "date": SEED_DATE, "time": "10:00"},
    )
    names = {row.name for row in res.data}
    assert names == {"Alex Taylor", "Sam Rivers"}  # Jordan lacks cleaning


async def test_search_availability_excludes_busy_and_honors_geo(Session):
    ids = seed_staff(Session)
    book(Session, staff_id=ids["Alex Taylor"], start_date=f"{SEED_DATE} 10:00:00")
    # Alex busy at 10:00 → only Sam for cleaning.
    res = await _call(
        build_server(read_only=True),
        "search_availability",
        {"service": "cleaning", "date": SEED_DATE, "time": "10:00"},
    )
    assert {r.name for r in res.data} == {"Sam Rivers"}
    # Tight geo box around Alex's base → only Alex (at 11:00 he's free).
    res2 = await _call(
        build_server(read_only=True),
        "search_availability",
        {
            "service": "cleaning",
            "date": SEED_DATE,
            "time": "11:00",
            "latitude": -27.47,
            "longitude": 153.02,
            "radius_km": 2.0,
        },
    )
    assert {r.name for r in res2.data} == {"Alex Taylor"}


async def test_list_staff_and_daily_schedule(Session):
    ids = seed_staff(Session)
    book(
        Session, staff_id=ids["Jordan Lee"], start_date=f"{SEED_DATE} 09:00:00", service="plumbing"
    )
    plumbers = await _call(build_server(read_only=True), "list_staff", {"skill": "plumbing"})
    assert {r.name for r in plumbers.data} == {"Jordan Lee"}

    sched = await _call(build_server(read_only=True), "daily_schedule", {"date": SEED_DATE})
    assert len(sched.data) == 1
    assert sched.data[0].service == "plumbing"


async def test_get_client_with_contacts_and_memories(Session):
    seed_client(Session)
    res = await _call(build_server(read_only=True), "get_client", {"email": "priya@example.com"})
    assert res.data.name == "Priya Nair"
    assert res.data.contacts[0].email == "priya@example.com"
    assert any(m.content.get("note") == "calm with anxious dogs" for m in res.data.memories)


async def test_get_client_unknown_returns_none(Session):
    res = await _call(build_server(read_only=True), "get_client", {"email": "nobody@example.com"})
    assert res.data is None


async def test_read_only_hides_write_tools(Session):
    async with Client(build_server(read_only=True)) as c:
        names = {t.name for t in await c.list_tools()}
    assert names.isdisjoint(
        {
            "create_booking",
            "cancel_booking",
            "reschedule_booking",
            "add_customer_preference",
            "book_from_text",
        }
    )
    assert {"search_availability", "find_next_available"} <= names  # reads always present


async def test_write_tools_present_when_enabled(Session):
    async with Client(build_server(read_only=False)) as c:
        names = {t.name for t in await c.list_tools()}
    assert {
        "create_booking",
        "cancel_booking",
        "reschedule_booking",
        "add_customer_preference",
        "book_from_text",
    } <= names


async def test_create_booking_is_idempotent(Session):
    ids = seed_staff(Session)
    args = {
        "customer_name": "John Doe",
        "email": "john@example.com",
        "service": "cleaning",
        "date": SEED_DATE,
        "time": "13:00",
        "phone": "0400000000",
        "address": "12 Queen St",
        "staff_id": ids["Alex Taylor"],
    }
    server = build_server(read_only=False)
    first = await _call(server, "create_booking", args)
    second = await _call(server, "create_booking", args)

    assert first.data.idempotent is False
    assert second.data.idempotent is True
    assert first.data.appointment_id == second.data.appointment_id
    with Session() as s:
        assert s.query(Appointment).count() == 1  # not double-booked


async def test_add_customer_preference(Session):
    seed_client(Session)
    res = await _call(
        build_server(read_only=False),
        "add_customer_preference",
        {"email": "priya@example.com", "note": "fragrance-free"},
    )
    assert res.data.created is False  # a preference already existed → updated
    follow = await _call(build_server(read_only=True), "get_client", {"email": "priya@example.com"})
    assert any(m.content.get("note") == "fragrance-free" for m in follow.data.memories)


async def test_add_preference_creates_when_absent(Session):
    res = await _call(
        build_server(read_only=False),
        "add_customer_preference",
        {"email": "new@example.com", "note": "no pets"},
    )
    assert res.data.created is True  # no prior preference → created


async def test_add_preference_declined_writes_nothing(Session):
    seed_client(Session)  # seeds an existing "calm with anxious dogs" preference
    with pytest.raises(ToolError):
        await _call(
            build_server(read_only=False),
            "add_customer_preference",
            {"email": "priya@example.com", "note": "no pets"},
            confirm=False,
        )
    follow = await _call(build_server(read_only=True), "get_client", {"email": "priya@example.com"})
    assert all(m.content.get("note") != "no pets" for m in follow.data.memories)  # unchanged


async def test_invalid_date_is_rejected(Session):
    with pytest.raises(ToolError):
        await _call(
            build_server(read_only=True),
            "search_availability",
            {"service": "cleaning", "date": "20 June 2026", "time": "10:00"},
        )


async def test_semantically_invalid_date_and_time_rejected(Session):
    server = build_server(read_only=True)
    for bad in (
        {"service": "cleaning", "date": "2026-13-20", "time": "10:00"},  # month 13
        {"service": "cleaning", "date": "2026-02-30", "time": "10:00"},  # Feb 30
        {"service": "cleaning", "date": SEED_DATE, "time": "25:99"},  # impossible time
    ):
        with pytest.raises(ToolError):
            await _call(server, "search_availability", bad)


async def test_search_no_results_returns_empty(Session):
    seed_staff(Session)
    res = await _call(
        build_server(read_only=True),
        "search_availability",
        {"service": "no-such-skill", "date": SEED_DATE, "time": "10:00"},
    )
    assert res.data == []


async def test_create_booking_rejects_unknown_staff(Session):
    seed_staff(Session)
    with pytest.raises(ToolError):
        await _call(
            build_server(read_only=False),
            "create_booking",
            {
                "customer_name": "X",
                "email": "x@example.com",
                "service": "cleaning",
                "date": SEED_DATE,
                "time": "14:00",
                "staff_id": "does-not-exist",
            },
        )


async def test_create_booking_different_person_same_slot_not_deduped(Session):
    seed_staff(Session)
    # Unassigned (no staff_id) so both rows are exempt from uq_appt_staff_slot — this
    # exercises that the idempotency ledger keys on the person, not just the slot.
    base = {
        "email": "family@example.com",
        "service": "cleaning",
        "date": SEED_DATE,
        "time": "15:00",
    }
    server = build_server(read_only=False)
    a = await _call(server, "create_booking", {**base, "customer_name": "Parent"})
    b = await _call(server, "create_booking", {**base, "customer_name": "Child"})
    assert a.data.idempotent is False and b.data.idempotent is False
    assert a.data.appointment_id != b.data.appointment_id  # different people → distinct bookings
    with Session() as s:
        assert s.query(Appointment).count() == 2


async def test_create_booking_double_books_staff_rejected_cleanly(Session):
    ids = seed_staff(Session)
    base = {
        "service": "cleaning",
        "date": SEED_DATE,
        "time": "17:00",
        "staff_id": ids["Alex Taylor"],
    }
    server = build_server(read_only=False)
    first = await _call(server, "create_booking", {**base, "customer_name": "A", "email": "a@x.com"})
    assert first.data.idempotent is False
    # Different person, same (staff, slot) → uq_appt_staff_slot rejects it as a clean conflict.
    with pytest.raises(ToolError, match="already booked at this time"):
        await _call(server, "create_booking", {**base, "customer_name": "B", "email": "b@x.com"})
    with Session() as s:
        assert s.query(Appointment).count() == 1  # the conflict wasn't written


async def test_db_error_is_wrapped_as_tool_error(Session):
    from sqlalchemy import create_engine

    from booking_mcp import db

    # Unreachable DB → the tool surfaces a clean ToolError, not a raw traceback.
    db.configure(create_engine("postgresql+psycopg://u:u@127.0.0.1:1/x", future=True))
    with pytest.raises(ToolError):
        await _call(build_server(read_only=True), "list_staff", {})


# --- elicitation: bookings are confirmed before writing -------------------


async def test_create_booking_declined_writes_nothing(Session):
    ids = seed_staff(Session)
    with pytest.raises(ToolError):
        await _call(
            build_server(read_only=False),
            "create_booking",
            {
                "customer_name": "X",
                "email": "x@example.com",
                "service": "cleaning",
                "date": SEED_DATE,
                "time": "16:00",
                "staff_id": ids["Alex Taylor"],
            },
            confirm=False,
        )
    with Session() as s:
        assert s.query(Appointment).count() == 0  # decline → no write


# --- find_next_available ---------------------------------------------------


async def test_find_next_available_returns_first_open_day(Session):
    seed_staff(Session)
    res = await _call(
        build_server(read_only=True),
        "find_next_available",
        {"service": "cleaning", "date": SEED_DATE, "time": "10:00", "days": 3},
    )
    assert res.data.date == SEED_DATE
    assert res.data.staff_name in {"Alex Taylor", "Sam Rivers"}


async def test_find_next_available_rolls_to_next_day(Session):
    from datetime import date, timedelta

    ids = seed_staff(Session)
    book(Session, staff_id=ids["Alex Taylor"], start_date=f"{SEED_DATE} 10:00:00")
    book(Session, staff_id=ids["Sam Rivers"], start_date=f"{SEED_DATE} 10:00:00")
    res = await _call(
        build_server(read_only=True),
        "find_next_available",
        {"service": "cleaning", "date": SEED_DATE, "time": "10:00", "days": 3},
    )
    assert res.data.date == (date.fromisoformat(SEED_DATE) + timedelta(days=1)).isoformat()


async def test_find_next_available_none_when_no_skill(Session):
    seed_staff(Session)
    res = await _call(
        build_server(read_only=True),
        "find_next_available",
        {"service": "no-such-skill", "date": SEED_DATE, "time": "10:00", "days": 2},
    )
    assert res.data is None


# --- cancel / reschedule ---------------------------------------------------


async def _book_one(server, Session, *, time="12:00", staff_id=None, email="c@example.com"):
    args = {
        "customer_name": "C",
        "email": email,
        "service": "cleaning",
        "date": SEED_DATE,
        "time": time,
    }
    if staff_id:
        args["staff_id"] = staff_id
    return (await _call(server, "create_booking", args)).data.appointment_id


async def test_cancel_booking_is_idempotent(Session):
    ids = seed_staff(Session)
    server = build_server(read_only=False)
    appt_id = await _book_one(server, Session, staff_id=ids["Alex Taylor"])
    assert (
        await _call(server, "cancel_booking", {"appointment_id": appt_id})
    ).data.cancelled is True
    assert (
        await _call(server, "cancel_booking", {"appointment_id": appt_id})
    ).data.cancelled is False
    with Session() as s:
        assert s.query(Appointment).count() == 0


async def test_cancel_declined_keeps_appointment(Session):
    ids = seed_staff(Session)
    server = build_server(read_only=False)
    appt_id = await _book_one(server, Session, staff_id=ids["Alex Taylor"])
    with pytest.raises(ToolError):
        await _call(server, "cancel_booking", {"appointment_id": appt_id}, confirm=False)
    with Session() as s:
        assert s.query(Appointment).count() == 1


async def test_reschedule_unstaffed_booking(Session):
    seed_staff(Session)
    server = build_server(read_only=False)
    appt_id = await _book_one(server, Session, time="13:00")  # no staff_id
    res = await _call(
        server,
        "reschedule_booking",
        {"appointment_id": appt_id, "date": SEED_DATE, "time": "14:00"},
    )
    assert res.data.start_date == f"{SEED_DATE} 14:00:00"


async def test_reschedule_staffed_to_free_slot(Session):
    ids = seed_staff(Session)
    server = build_server(read_only=False)
    appt_id = await _book_one(server, Session, time="13:00", staff_id=ids["Alex Taylor"])
    res = await _call(
        server,
        "reschedule_booking",
        {"appointment_id": appt_id, "date": SEED_DATE, "time": "15:00"},
    )
    assert res.data.start_date == f"{SEED_DATE} 15:00:00"


async def test_reschedule_conflict_is_rejected(Session):
    ids = seed_staff(Session)
    server = build_server(read_only=False)
    first = await _book_one(
        server, Session, time="09:00", staff_id=ids["Alex Taylor"], email="a@example.com"
    )
    second = await _book_one(
        server, Session, time="11:00", staff_id=ids["Alex Taylor"], email="b@example.com"
    )
    assert first  # Alex now has 09:00 and 11:00
    with pytest.raises(ToolError):  # move the 11:00 onto the occupied 09:00
        await _call(
            server,
            "reschedule_booking",
            {"appointment_id": second, "date": SEED_DATE, "time": "09:00"},
        )


async def test_reschedule_unknown_appointment(Session):
    seed_staff(Session)
    with pytest.raises(ToolError):
        await _call(
            build_server(read_only=False),
            "reschedule_booking",
            {"appointment_id": "nope", "date": SEED_DATE, "time": "14:00"},
        )


async def test_reschedule_declined_keeps_slot(Session):
    seed_staff(Session)
    server = build_server(read_only=False)
    appt_id = await _book_one(server, Session, time="13:00")
    with pytest.raises(ToolError):
        await _call(
            server,
            "reschedule_booking",
            {"appointment_id": appt_id, "date": SEED_DATE, "time": "14:00"},
            confirm=False,
        )
    with Session() as s:
        appt = s.query(Appointment).filter_by(id=appt_id).one()
        assert appt.start_date == datetime.fromisoformat(f"{SEED_DATE} 13:00:00")  # unchanged


# --- book_from_text: LLM sampling extracts the request, then confirm + write ---

_EXTRACT_JSON = (
    '{"customer_name": "Jane Roe", "email": "jane@example.com", "service": "cleaning", '
    '"date": "2026-06-20", "time": "10:00", "phone": "0400999888", "address": "9 Hill St"}'
)


def _sampler(payload: str):
    async def handler(messages, params, context):
        return payload  # the client's LLM "reply" — book_from_text parses it

    return handler


async def _call_text(server, request, *, payload, confirm=True):
    async with Client(
        server, sampling_handler=_sampler(payload), elicitation_handler=_handler(confirm)
    ) as c:
        return await c.call_tool("book_from_text", {"request": request})


async def test_book_from_text_creates_booking(Session):
    server = build_server(read_only=False)
    res = await _call_text(server, "Book Jane a clean", payload=_EXTRACT_JSON)
    assert res.data.idempotent is False
    with Session() as s:
        assert s.query(Appointment).count() == 1
        client_emails = {c.email for c in s.query(ClientRow).all()}
    assert "jane@example.com" in client_emails


async def test_book_from_text_strips_code_fences(Session):
    server = build_server(read_only=False)
    fenced = f"```json\n{_EXTRACT_JSON}\n```"
    res = await _call_text(server, "Book Jane a clean", payload=fenced)
    assert res.data.idempotent is False
    with Session() as s:
        assert s.query(Appointment).count() == 1


async def test_book_from_text_unparseable_extraction_raises(Session):
    server = build_server(read_only=False)
    with pytest.raises(ToolError):
        await _call_text(server, "uhh do a thing", payload="Sorry, I couldn't parse that.")
    with Session() as s:
        assert s.query(Appointment).count() == 0  # nothing written


async def test_book_from_text_invalid_field_raises(Session):
    # Valid JSON but a hallucinated impossible date → rejected before any write.
    bad = _EXTRACT_JSON.replace("2026-06-20", "2026-13-40")
    with pytest.raises(ToolError):
        await _call_text(build_server(read_only=False), "Book Jane", payload=bad)
    with Session() as s:
        assert s.query(Appointment).count() == 0


async def test_book_from_text_declined_writes_nothing(Session):
    server = build_server(read_only=False)
    with pytest.raises(ToolError):
        await _call_text(server, "Book Jane a clean", payload=_EXTRACT_JSON, confirm=False)
    with Session() as s:
        assert s.query(Appointment).count() == 0


# --- graceful degradation when the client lacks a capability ----------------


async def test_book_from_text_without_sampling_is_clean_error(Session):
    # No sampling_handler → ctx.sample raises a raw protocol error; we translate it.
    server = build_server(read_only=False)
    with pytest.raises(ToolError, match="does not support sampling"):
        async with Client(server, elicitation_handler=_handler(True)) as c:
            await c.call_tool("book_from_text", {"request": "Book Jane a clean"})


async def test_create_booking_without_elicitation_is_clean_error(Session):
    # No elicitation_handler → ctx.elicit errors; _confirm turns it into a clean ToolError.
    server = build_server(read_only=False)
    with pytest.raises(ToolError, match="does not support elicitation"):
        async with Client(server) as c:
            await c.call_tool(
                "create_booking",
                {
                    "customer_name": "X",
                    "email": "x@example.com",
                    "service": "cleaning",
                    "date": SEED_DATE,
                    "time": "10:00",
                },
            )


async def test_book_from_text_times_out_on_hung_client_llm(Session, monkeypatch):
    # A client LLM that takes too long must not pin the worker: fail_after fires. We patch
    # ctx.sample to outlast the (tiny) timeout — cancelled cleanly inside the cancel scope.
    monkeypatch.setenv("SAMPLE_TIMEOUT", "0.05")

    async def _slow_sample(self, *args, **kwargs):
        await anyio.sleep(5)  # never reached: the cancel scope tears it down first

    monkeypatch.setattr("fastmcp.server.context.Context.sample", _slow_sample)
    server = build_server(read_only=False)
    with pytest.raises(ToolError, match="did not respond in time"):
        await _call_text(server, "Book Jane a clean", payload="ignored")
    with Session() as s:
        assert s.query(Appointment).count() == 0  # nothing written
