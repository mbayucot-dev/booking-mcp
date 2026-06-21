"""Test harness: a real Postgres (testcontainers) bound to the MCP's own models,
wiped per test. Tools run against it via the in-memory FastMCP Client."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from booking_mcp import db
from booking_mcp.models import (
    Appointment,
    Base,
    Client,
    Contact,
    CustomerMemory,
    Job,
    Staff,
    StaffSkill,
)

STAFF = [
    {"name": "Alex Taylor", "skills": ["cleaning", "contact work"], "lat": -27.47, "lng": 153.02},
    {"name": "Sam Rivers", "skills": ["cleaning", "gardening"], "lat": -27.50, "lng": 153.05},
    {"name": "Jordan Lee", "skills": ["plumbing", "contact work"], "lat": -27.45, "lng": 152.98},
]


@pytest.fixture(scope="session")
def pg_engine():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        engine = create_engine(pg.get_connection_url(), future=True)
        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()


@pytest.fixture()
def Session(pg_engine):
    """Sessionmaker bound to the container; also configures the server's db layer.
    Data is truncated after each test."""
    db.configure(pg_engine)
    maker = sessionmaker(bind=pg_engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        with pg_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


def seed_staff(Session) -> dict[str, str]:
    """Seed the default cleaners; return {name: id}."""
    ids: dict[str, str] = {}
    with Session() as s:
        for spec in STAFF:
            staff = Staff(
                name=spec["name"],
                skills=spec["skills"],
                latitude=spec["lat"],
                longitude=spec["lng"],
            )
            s.add(staff)
            s.flush()
            ids[spec["name"]] = staff.id
            for skill in spec["skills"]:
                s.add(StaffSkill(staff_id=staff.id, skill=skill))
        s.commit()
    return ids


def seed_client(Session, *, email="priya@example.com", name="Priya Nair") -> str:
    with Session() as s:
        client = Client(name=name, email=email, phone="0400111222", address="5 Park Rd")
        s.add(client)
        s.flush()
        s.add(Contact(client_id=client.id, name=name, email=email, phone="0400111222"))
        s.add(
            CustomerMemory(
                customer_key=email,
                memory_type="preference",
                content={"note": "calm with anxious dogs"},
            )
        )
        s.commit()
        return client.id


def book(Session, *, staff_id, start_date, service="cleaning"):
    """Create a job + appointment occupying a staff slot."""
    with Session() as s:
        client = Client(name="C", email=None, phone="1", address="z")
        s.add(client)
        s.flush()
        job = Job(client_id=client.id, service=service, address="z")
        s.add(job)
        s.flush()
        when = datetime.fromisoformat(start_date) if isinstance(start_date, str) else start_date
        s.add(Appointment(job_id=job.id, staff_id=staff_id, staff_name="x", start_date=when))
        s.commit()
