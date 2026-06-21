"""Schema-drift tests: verify DB-level constraints are present and enforced.

These tests run directly against the testcontainers Postgres — no booking-agent
dependency. They catch silent drift between the ORM model definitions and the actual
DB schema that create_all() emits.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from booking_mcp.models import (
    Appointment,
    Client,
    CustomerMemory,
    ExecutedAction,
    Job,
    Staff,
)

# --- uq_client_email ----------------------------------------------------------


def test_client_email_unique_constraint_exists(pg_engine):
    inspector = inspect(pg_engine)
    constraints = {u["name"] for u in inspector.get_unique_constraints("clients")}
    assert "uq_client_email" in constraints


def test_client_email_unique_constraint_fires(Session):
    with Session() as s:
        s.add(Client(name="A", email="dup@x.com"))
        s.flush()
        s.add(Client(name="B", email="dup@x.com"))
        with pytest.raises(IntegrityError, match="uq_client_email"):
            s.flush()
        s.rollback()


def test_client_null_email_allows_multiple_rows(Session):
    """NULL emails must not trigger the unique constraint (Postgres NULLs ≠ NULLs)."""
    with Session() as s:
        s.add(Client(name="X", email=None))
        s.add(Client(name="Y", email=None))
        s.flush()  # must not raise
        s.rollback()


# --- uq_appt_staff_slot -------------------------------------------------------


def test_uq_appt_staff_slot_index_exists(pg_engine):
    inspector = inspect(pg_engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("appointments")}
    assert "uq_appt_staff_slot" in indexes


def test_uq_appt_staff_slot_fires_on_double_book(Session):
    from datetime import datetime

    with Session() as s:
        staff = Staff(name="T", skills=[])
        s.add(staff)
        s.flush()
        client = Client(name="C")
        s.add(client)
        s.flush()
        job1 = Job(client_id=client.id, service="cleaning", address="z")
        job2 = Job(client_id=client.id, service="cleaning", address="z")
        s.add(job1)
        s.add(job2)
        s.flush()
        start = datetime(2026, 6, 20, 9, 0)
        s.add(Appointment(job_id=job1.id, staff_id=staff.id, staff_name="T", start_date=start))
        s.flush()
        s.add(Appointment(job_id=job2.id, staff_id=staff.id, staff_name="T", start_date=start))
        with pytest.raises(IntegrityError, match="uq_appt_staff_slot"):
            s.flush()
        s.rollback()


def test_uq_appt_staff_slot_allows_null_staff(Session):
    """Null-staff rows are exempt from the partial unique index."""
    from datetime import datetime

    with Session() as s:
        client = Client(name="C")
        s.add(client)
        s.flush()
        job1 = Job(client_id=client.id, service="cleaning", address="z")
        job2 = Job(client_id=client.id, service="cleaning", address="z")
        s.add(job1)
        s.add(job2)
        s.flush()
        start = datetime(2026, 6, 20, 9, 0)
        s.add(Appointment(job_id=job1.id, staff_id=None, start_date=start))
        s.add(Appointment(job_id=job2.id, staff_id=None, start_date=start))
        s.flush()  # must not raise
        s.rollback()


# --- ck_customer_memory_type --------------------------------------------------


def test_ck_customer_memory_type_constraint_exists(pg_engine):
    inspector = inspect(pg_engine)
    constraints = {c["name"] for c in inspector.get_check_constraints("customer_memories")}
    assert "ck_customer_memory_type" in constraints


def test_ck_customer_memory_type_fires_on_unknown_type(Session):
    with Session() as s:
        s.add(CustomerMemory(customer_key="x@x.com", memory_type="invalid_type", content={}))
        with pytest.raises(IntegrityError, match="ck_customer_memory_type"):
            s.flush()
        s.rollback()


def test_ck_customer_memory_type_accepts_all_valid_types(Session):
    from booking_mcp.models import _MEMORY_TYPES

    with Session() as s:
        for mtype in _MEMORY_TYPES:
            s.add(CustomerMemory(customer_key=f"{mtype}@x.com", memory_type=mtype, content={}))
        s.flush()  # must not raise
        s.rollback()


# --- uq_customer_memory -------------------------------------------------------


def test_uq_customer_memory_constraint_exists(pg_engine):
    inspector = inspect(pg_engine)
    constraints = {u["name"] for u in inspector.get_unique_constraints("customer_memories")}
    assert "uq_customer_memory" in constraints


def test_uq_customer_memory_fires_on_duplicate(Session):
    with Session() as s:
        s.add(CustomerMemory(customer_key="k@x.com", memory_type="preference", content={}))
        s.flush()
        s.add(CustomerMemory(customer_key="k@x.com", memory_type="preference", content={}))
        with pytest.raises(IntegrityError, match="uq_customer_memory"):
            s.flush()
        s.rollback()


# --- executed_actions PK ------------------------------------------------------


def test_executed_actions_pk_is_idempotency_key(pg_engine):
    inspector = inspect(pg_engine)
    pk = inspector.get_pk_constraint("executed_actions")
    assert pk["constrained_columns"] == ["idempotency_key"]


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_executed_actions_pk_fires_on_duplicate_key(Session):
    with Session() as s:
        ea = ExecutedAction(
            idempotency_key="mcp:test:abc123",
            run_id=None,
            action="test",
            result={"ok": True},
        )
        s.add(ea)
        s.flush()
        s.add(
            ExecutedAction(
                idempotency_key="mcp:test:abc123",
                run_id=None,
                action="test",
                result={"ok": True},
            )
        )
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
