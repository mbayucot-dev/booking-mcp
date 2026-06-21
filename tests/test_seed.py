"""Standalone seed: schema bootstrap + idempotent demo data."""

import pytest

import booking_mcp.seed as seed
from booking_mcp.models import Appointment, Client, Contact, CustomerMemory, Staff
from booking_mcp.seed import DEMO_APPOINTMENTS, seed_all


def test_seed_all_populates_and_is_idempotent(Session):
    first = seed_all(Session)
    assert first == {
        "staff": 3,
        "clients": 2,
        "contacts": 2,
        "memories": 2,
        "appointments": len(DEMO_APPOINTMENTS),
    }
    with Session() as s:
        assert s.query(Staff).count() == 3
        assert s.query(Client).count() == 2
        assert s.query(Contact).count() == 2
        assert s.query(CustomerMemory).count() == 2
        assert s.query(Appointment).count() == len(DEMO_APPOINTMENTS)

    # Re-running creates nothing — exercises the "already seeded" paths.
    assert seed_all(Session) == {
        "staff": 0,
        "clients": 0,
        "contacts": 0,
        "memories": 0,
        "appointments": 0,
    }


def test_seed_skips_appointment_for_unknown_staff(Session, monkeypatch):
    monkeypatch.setattr(seed, "DEMO_APPOINTMENTS", [("Ghost Cleaner", "cleaning", "09:00")])
    summary = seed_all(Session)
    assert summary["appointments"] == 0  # unknown staff name → skipped, no crash


def test_main_bootstraps_schema_and_seeds(Session, monkeypatch):
    monkeypatch.setenv("STANDALONE_MODE", "true")
    # Session fixture configured db to the testcontainer; main() runs create_all + seed_all.
    seed.main()
    with Session() as s:
        assert s.query(Staff).count() == 3
        assert s.query(Appointment).count() == len(DEMO_APPOINTMENTS)


def test_main_blocked_without_standalone_mode(monkeypatch):
    monkeypatch.delenv("STANDALONE_MODE", raising=False)
    with pytest.raises(SystemExit, match="STANDALONE_MODE"):
        seed.main()
