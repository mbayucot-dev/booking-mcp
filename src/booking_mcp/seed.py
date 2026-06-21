"""Standalone schema bootstrap + demo seed.

Lets booking-mcp run on its own: create any missing tables and insert demo staff /
clients / appointments / preferences. Idempotent — re-runs report zero rows created.

    python -m booking_mcp.seed      # or the `booking-mcp-seed` console script
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from . import db
from .models import Appointment, Client, Contact, CustomerMemory, Job, Staff, StaffSkill

log = logging.getLogger("booking_mcp.seed")

SEED_DATE = "2026-06-20"

DEMO_STAFF = [
    {
        "name": "Alex Taylor",
        "skills": ["cleaning", "contact work"],
        "lat": -27.47,
        "lng": 153.02,
        "bio": "Detail-oriented; great with pets and nervous animals.",
    },
    {
        "name": "Sam Rivers",
        "skills": ["cleaning", "gardening"],
        "lat": -27.50,
        "lng": 153.05,
        "bio": "Fast, eco-friendly products.",
    },
    {
        "name": "Jordan Lee",
        "skills": ["plumbing", "contact work"],
        "lat": -27.45,
        "lng": 152.98,
        "bio": "Deep cleans; calm and patient, good with anxious dogs.",
    },
]

DEMO_CLIENTS = [
    {
        "name": "Priya Nair",
        "email": "priya@example.com",
        "phone": "0400111222",
        "address": "5 Park Rd, Brisbane",
    },
    {
        "name": "Liam O'Brien",
        "email": "liam@example.com",
        "phone": "0400333444",
        "address": "88 River St, Brisbane",
    },
]

DEMO_MEMORIES = [
    ("priya@example.com", "preference", {"note": "calm with anxious dogs"}),
    ("priya@example.com", "vip", {"tier": "gold"}),
]

# (staff_name, service, "HH:MM") on SEED_DATE — gives the schedule realistic load.
DEMO_APPOINTMENTS = [
    ("Alex Taylor", "cleaning", "09:00"),
    ("Alex Taylor", "contact work", "10:00"),
    ("Sam Rivers", "gardening", "09:00"),
]


@dataclass
class SeedSummary:
    staff: int = 0
    clients: int = 0
    contacts: int = 0
    memories: int = 0
    appointments: int = 0


def seed_all(session_factory: sessionmaker) -> dict:
    """Idempotently seed demo data. Returns counts created this run."""
    summary = SeedSummary()
    with session_factory() as s:
        if s.scalar(select(Staff).limit(1)) is None:
            staff_by_name: dict[str, Staff] = {}
            for spec in DEMO_STAFF:
                staff = Staff(
                    name=spec["name"],
                    skills=spec["skills"],
                    latitude=spec["lat"],
                    longitude=spec["lng"],
                    bio=spec["bio"],
                )
                s.add(staff)
                s.flush()
                for skill in spec["skills"]:
                    s.add(StaffSkill(staff_id=staff.id, skill=skill))
                staff_by_name[spec["name"]] = staff
                summary.staff += 1
        else:
            staff_by_name = {st.name: st for st in s.scalars(select(Staff))}

        client_by_email: dict[str, Client] = {}
        for spec in DEMO_CLIENTS:
            existing = s.scalar(select(Client).where(Client.email == spec["email"]))
            if existing is not None:
                client_by_email[spec["email"]] = existing
                continue
            client = Client(
                name=spec["name"], email=spec["email"], phone=spec["phone"], address=spec["address"]
            )
            s.add(client)
            s.flush()
            s.add(
                Contact(
                    client_id=client.id, name=spec["name"], email=spec["email"], phone=spec["phone"]
                )
            )
            client_by_email[spec["email"]] = client
            summary.clients += 1
            summary.contacts += 1

        for key, mtype, content in DEMO_MEMORIES:
            exists = s.scalar(
                select(CustomerMemory).where(
                    CustomerMemory.customer_key == key, CustomerMemory.memory_type == mtype
                )
            )
            if exists is None:
                s.add(CustomerMemory(customer_key=key, memory_type=mtype, content=content))
                summary.memories += 1

        anchor = client_by_email.get(DEMO_CLIENTS[0]["email"])
        for staff_name, service, hhmm in DEMO_APPOINTMENTS:
            staff = staff_by_name.get(staff_name)
            if staff is None or anchor is None:
                continue
            start = datetime.fromisoformat(f"{SEED_DATE} {hhmm}:00")
            booked = s.scalar(
                select(Appointment).where(
                    Appointment.staff_id == staff.id, Appointment.start_date == start
                )
            )
            if booked is not None:
                continue
            job = Job(client_id=anchor.id, service=service, address=anchor.address)
            s.add(job)
            s.flush()
            s.add(
                Appointment(
                    job_id=job.id, staff_id=staff.id, staff_name=staff.name, start_date=start
                )
            )
            summary.appointments += 1

        s.commit()
    return asdict(summary)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from .config import get_settings

    if not get_settings().standalone_mode:
        raise SystemExit(
            "ERROR: Refusing to seed — STANDALONE_MODE is not true. "
            "Seeding against the shared booking-agent DB would corrupt production data. "
            "Set STANDALONE_MODE=true for demo/dev use only."
        )
    db.create_all()
    summary = seed_all(db.get_sessionmaker())
    log.info("booking-mcp seeded (rows created this run): %s", summary)


if __name__ == "__main__":  # pragma: no cover
    main()
