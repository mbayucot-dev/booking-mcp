"""Read-only resources (URI-addressed views of the booking datastore)."""

from __future__ import annotations

import logging
from contextlib import contextmanager

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy.exc import SQLAlchemyError

from . import queries
from .config import get_settings
from .db import session
from .schemas import (
    AppointmentDTO,
    ClientDTO,
    ContactDTO,
    MemoryDTO,
    StaffDTO,
    _mask_address,
    _mask_phone,
)
from .validation import DateArg, EmailArg

log = logging.getLogger("booking_mcp.resources")


@contextmanager
def _guard():
    """Translate raw DB errors into a clean error (don't leak internals to the client)."""
    try:
        yield
    except ToolError:
        raise
    except SQLAlchemyError as e:
        log.error("database error in resource: %s", e)
        raise RuntimeError("A database error occurred. Please retry.") from e


def register(mcp: FastMCP) -> None:
    @mcp.resource("booking://staff", tags={"read"})
    def all_staff() -> list[dict]:
        """All active staff (cleaners) with skills + home location."""
        with _guard(), session() as s:
            return [StaffDTO.from_row(r).model_dump() for r in queries.active_staff(s)]

    @mcp.resource("booking://staff/{staff_id}", tags={"read"})
    def one_staff(staff_id: str) -> dict | None:
        """A single staff member by id."""
        with _guard(), session() as s:
            row = queries.staff_by_id(s, staff_id)
            return StaffDTO.from_row(row).model_dump() if row else None

    @mcp.resource("booking://schedule/{date}", tags={"read"})
    def schedule(date: DateArg) -> dict:
        """All appointments on a given ISO date (YYYY-MM-DD). Wrapped in an object
        (a templated resource treats a bare list as multiple contents)."""
        with _guard(), session() as s:
            appts = [
                AppointmentDTO.from_row(a, service=svc).model_dump()
                for a, svc in queries.appointments_with_service(s, date)
            ]
        return {"date": date, "count": len(appts), "appointments": appts}

    @mcp.resource("booking://clients/{email}", tags={"read", "pii"})
    def client(email: EmailArg) -> dict | None:
        """A client (by email) with their contacts and long-term memories.
        Phone and address are masked by default (REDACT_PII=true); set false only for
        internal/admin tooling backed by a scoped token."""
        redact = get_settings().redact_pii
        log.info("pii_access: resource=clients email=%s redacted=%s", email, redact)
        with _guard(), session() as s:
            row = queries.client_by_email(s, email)
            if row is None:
                return None
            return ClientDTO(
                id=row.id,
                name=row.name,
                email=row.email,
                phone=_mask_phone(row.phone) if redact else row.phone,
                address=_mask_address(row.address) if redact else row.address,
                contacts=[
                    ContactDTO(
                        name=c.name,
                        email=c.email,
                        phone=_mask_phone(c.phone) if redact else c.phone,
                    )
                    for c in queries.contacts_for(s, row.id)
                ],
                memories=[
                    MemoryDTO(type=m.memory_type, content=m.content)
                    for m in queries.memories_for(s, email)
                ],
            ).model_dump()
