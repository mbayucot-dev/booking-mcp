"""Drift guard: run booking-agent's real Alembic migrations into a fresh Postgres and
exercise the MCP's models/queries against that schema, catching silent drift from the
owning service. Skipped when booking-agent isn't checked out alongside.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from booking_mcp import db, queries

BOOKING_AGENT = Path(__file__).resolve().parents[2] / "booking-agent" / "backend"
AGENT_PY = BOOKING_AGENT / ".venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(
    not AGENT_PY.exists(),
    reason="booking-agent backend venv not found — skipping cross-repo schema-contract test",
)


def test_mcp_queries_run_against_booking_agent_migrated_schema():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        url = pg.get_connection_url()
        # Apply booking-agent's real migrations (it owns the schema).
        proc = subprocess.run(
            [str(AGENT_PY), "-m", "alembic", "upgrade", "head"],
            cwd=str(BOOKING_AGENT),
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"alembic upgrade failed:\n{proc.stderr}"

        engine = create_engine(url, future=True)
        db.configure(engine)
        try:
            # Reads + a write against the migrated schema (no create_all) prove the
            # MCP's models map onto the real columns.
            with db.session() as s:
                assert queries.active_staff(s) == []
                assert (
                    queries.eligible_staff(s, date_iso="2026-06-20", time="10:00", skill="cleaning")
                    == []
                )
                res = queries.create_booking(
                    s,
                    name="Jo",
                    email="jo@example.com",
                    phone="1",
                    address="z",
                    service="cleaning",
                    date_iso="2026-06-20",
                    time="10:00",
                    staff_id=None,
                    staff_name=None,
                )
                assert res["idempotent"] is False and res["appointment_id"]
        finally:
            engine.dispose()
