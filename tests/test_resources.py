"""Resources + prompts via the in-memory FastMCP client."""

import json

from fastmcp import Client

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


async def test_client_resource(Session):
    seed_client(Session)
    async with Client(build_server()) as c:
        data = _json(await c.read_resource("booking://clients/priya@example.com"))
        unknown = _json(await c.read_resource("booking://clients/nobody@example.com"))
    assert data["name"] == "Priya Nair"
    assert data["memories"][0]["content"]["note"] == "calm with anxious dogs"
    assert unknown is None  # no such client


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
