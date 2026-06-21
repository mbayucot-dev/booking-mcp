"""MCP tools — thin wrappers over the query layer.

Read tools are always registered; write tools only when ``read_only`` is False
(they bypass booking-agent's human-approval workflow, so they're opt-in).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Annotated

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.elicitation import AcceptedElicitation
from mcp.shared.exceptions import McpError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from . import queries
from .config import get_settings
from .db import session
from .schemas import (
    AppointmentDTO,
    BookingExtract,
    BookingResult,
    CancelResult,
    ClientDTO,
    ContactDTO,
    MemoryDTO,
    NextAvailable,
    PreferenceResult,
    RescheduleResult,
    StaffDTO,
    _mask_address,
    _mask_phone,
)
from .validation import DateArg, EmailArg, TimeArg

log = logging.getLogger("booking_mcp.tools")

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
MUTATE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)

_EXTRACT_SYSTEM = (
    "You extract booking details from a customer's message. Respond with ONLY a "
    "JSON object (no prose, no markdown) with these keys: customer_name, email, "
    "service, date (YYYY-MM-DD), time (24-hour HH:MM), phone (string or null), "
    "address (string or null). Use null when a detail is not present."
)


@contextmanager
def _guard():
    """Translate raw DB errors into a clean MCP ToolError (don't leak internals)."""
    try:
        yield
    except ToolError:
        raise
    except SQLAlchemyError as e:
        log.error("database error: %s", e)
        raise ToolError("A database error occurred. Please retry.") from e


async def _confirm(ctx: Context, message: str) -> bool:
    """Ask the MCP client's user to approve a destructive action (elicitation)."""
    try:
        res = await ctx.elicit(message, response_type=bool)
    except McpError as e:
        # A client without elicitation can't confirm; surface a clean, actionable error.
        raise ToolError(
            "This client does not support elicitation, which is required to confirm "
            "this action. Use book_via_workflow instead."
        ) from e
    return isinstance(res, AcceptedElicitation) and res.data is True


def _strip_fences(raw: str) -> str:
    """Tolerate an LLM that wraps its JSON in a ``` / ```json code fence."""
    t = raw.strip()
    if not t.startswith("```"):
        return t
    t = t[3:]
    if t[:4].lower() == "json":
        t = t[4:]
    return t.strip().removesuffix("```").strip()


def register(mcp: FastMCP, *, read_only: bool) -> None:
    # --- read tools -------------------------------------------------------
    @mcp.tool(annotations=READ, tags={"read"})
    def search_availability(
        service: Annotated[str, Field(description="Service/skill, e.g. 'cleaning'")],
        date: DateArg,
        time: TimeArg,
        latitude: Annotated[float | None, Field(description="Job latitude", ge=-90, le=90)] = None,
        longitude: Annotated[
            float | None, Field(description="Job longitude", ge=-180, le=180)
        ] = None,
        radius_km: Annotated[float, Field(description="Search radius (km)", gt=0)] = 25.0,
    ) -> list[StaffDTO]:
        """Find staff who can do the service, are free at the slot, and (if coords
        given) within range. Same skill/free/geo filter the booking engine uses."""
        with _guard(), session() as s:
            rows = queries.eligible_staff(
                s,
                date_iso=date,
                time=time,
                skill=service,
                lat=latitude,
                lng=longitude,
                radius_km=radius_km,
            )
            return [StaffDTO.from_row(r) for r in rows]

    @mcp.tool(annotations=READ, tags={"read"})
    def list_staff(
        skill: Annotated[str | None, Field(description="Filter by skill")] = None,
    ) -> list[StaffDTO]:
        """List active staff, optionally filtered by a skill."""
        with _guard(), session() as s:
            return [StaffDTO.from_row(r) for r in queries.active_staff(s, skill=skill)]

    @mcp.tool(annotations=READ, tags={"read"})
    def daily_schedule(date: DateArg) -> list[AppointmentDTO]:
        """All appointments booked on a given date."""
        with _guard(), session() as s:
            return [
                AppointmentDTO.from_row(a, service=svc)
                for a, svc in queries.appointments_with_service(s, date)
            ]

    @mcp.tool(annotations=READ, tags={"read", "pii"})
    def get_client(email: EmailArg, ctx: Context) -> ClientDTO | None:
        """Look up a client by email with their contacts and saved preferences.
        Phone and address are masked by default (REDACT_PII=true); set false only for
        internal/admin tooling backed by a scoped token."""
        redact = get_settings().redact_pii
        log.info(
            "pii_access: tool=get_client request_id=%s client_id=%s email=%s redacted=%s",
            ctx.request_id,
            ctx.client_id,
            email,
            redact,
        )
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
            )

    @mcp.tool(annotations=READ, tags={"read"})
    def find_next_available(
        service: Annotated[str, Field(description="Service/skill, e.g. 'cleaning'")],
        date: DateArg,
        time: TimeArg,
        days: Annotated[int, Field(description="Days ahead to search", ge=1, le=30)] = 7,
        latitude: Annotated[float | None, Field(ge=-90, le=90)] = None,
        longitude: Annotated[float | None, Field(ge=-180, le=180)] = None,
        radius_km: Annotated[float, Field(gt=0)] = 25.0,
    ) -> NextAvailable | None:
        """The first day from ``date`` (within ``days``) with a free, qualified
        staff member at ``time`` — or null if none in the window."""
        with _guard(), session() as s:
            res = queries.find_next_available(
                s,
                service=service,
                date_iso=date,
                time=time,
                days=days,
                lat=latitude,
                lng=longitude,
                radius_km=radius_km,
            )
        return NextAvailable(**res) if res else None

    if read_only:
        return

    # --- write tools (opt-in; each confirms via elicitation before writing) ---
    @mcp.tool(annotations=CREATE, tags={"write"})
    async def create_booking(
        customer_name: Annotated[str, Field(description="Customer name")],
        email: EmailArg,
        service: Annotated[str, Field(description="Service, e.g. 'cleaning'")],
        date: DateArg,
        time: TimeArg,
        ctx: Context,
        phone: Annotated[str | None, Field(description="Customer phone")] = None,
        address: Annotated[str | None, Field(description="Job address")] = None,
        staff_id: Annotated[str | None, Field(description="Assigned staff id")] = None,
    ) -> BookingResult:
        """Create a booking (client + job + appointment) after the user confirms. Idempotent —
        a repeat of the same request returns the existing booking. Writes directly, bypassing
        booking-agent's approval workflow; only enabled when READ_ONLY is false."""

        def _resolve_staff() -> str | None:
            with _guard(), session() as s:
                if not staff_id:
                    return None
                staff = queries.staff_by_id(s, staff_id)
                if staff is None or not staff.active:
                    raise ToolError(f"Unknown or inactive staff_id: {staff_id!r}")
                return staff.name

        staff_name = await anyio.to_thread.run_sync(_resolve_staff)
        summary = f"Book {service} for {customer_name} on {date} at {time}"
        if staff_name:
            summary += f" with {staff_name}"
        if not await _confirm(ctx, summary + "?"):
            raise ToolError("Booking was not confirmed.")

        def _create() -> dict:
            with _guard(), session() as s:
                try:
                    return queries.create_booking(
                        s,
                        name=customer_name,
                        email=email,
                        phone=phone,
                        address=address,
                        service=service,
                        date_iso=date,
                        time=time,
                        staff_id=staff_id,
                        staff_name=staff_name,
                    )
                except ValueError as e:  # slot already taken (uq_appt_staff_slot)
                    raise ToolError(str(e)) from e

        result = BookingResult(**await anyio.to_thread.run_sync(_create))
        log.info(
            "booking created: request_id=%s client_id=%s id=%s email=%s service=%s date=%s idempotent=%s",
            ctx.request_id,
            ctx.client_id,
            result.appointment_id,
            email,
            service,
            date,
            result.idempotent,
        )
        return result

    @mcp.tool(annotations=MUTATE, tags={"write"})
    async def cancel_booking(
        appointment_id: Annotated[str, Field(description="Appointment id to cancel")],
        ctx: Context,
    ) -> CancelResult:
        """Cancel (delete) an appointment after the user confirms. Idempotent. Writes
        directly, bypassing booking-agent's approval workflow; use book_via_workflow for
        the approval-gated path."""
        if not await _confirm(ctx, f"Cancel appointment {appointment_id}?"):
            raise ToolError("Cancellation was not confirmed.")

        def _cancel() -> bool:
            with _guard(), session() as s:
                return queries.cancel_appointment(s, appointment_id)

        result = CancelResult(
            appointment_id=appointment_id, cancelled=await anyio.to_thread.run_sync(_cancel)
        )
        log.info(
            "booking cancelled: request_id=%s client_id=%s id=%s cancelled=%s",
            ctx.request_id,
            ctx.client_id,
            appointment_id,
            result.cancelled,
        )
        return result

    @mcp.tool(annotations=MUTATE, tags={"write"})
    async def reschedule_booking(
        appointment_id: Annotated[str, Field(description="Appointment id to move")],
        date: DateArg,
        time: TimeArg,
        ctx: Context,
    ) -> RescheduleResult:
        """Move an appointment to a new slot after the user confirms. Writes directly,
        bypassing booking-agent's approval workflow; use book_via_workflow for the
        approval-gated path."""
        if not await _confirm(ctx, f"Move appointment {appointment_id} to {date} {time}?"):
            raise ToolError("Reschedule was not confirmed.")

        def _reschedule() -> dict:
            with _guard(), session() as s:
                try:
                    res = queries.reschedule_appointment(
                        s, appointment_id=appointment_id, new_date=date, new_time=time
                    )
                except ValueError as e:
                    raise ToolError(str(e)) from e
                if res is None:
                    raise ToolError(f"Unknown appointment_id: {appointment_id!r}")
                return res

        result = RescheduleResult(**await anyio.to_thread.run_sync(_reschedule))
        log.info(
            "booking rescheduled: request_id=%s client_id=%s id=%s new_slot=%s",
            ctx.request_id,
            ctx.client_id,
            appointment_id,
            result.start_date,
        )
        return result

    @mcp.tool(annotations=MUTATE, tags={"write"})
    async def add_customer_preference(
        email: EmailArg,
        note: Annotated[str, Field(description="Durable preference note", min_length=1)],
        ctx: Context,
    ) -> PreferenceResult:
        """Save/overwrite a customer's free-text preference (e.g. 'fragrance-free') after the
        user confirms. Writes directly, bypassing booking-agent's approval workflow; use
        book_via_workflow for the approval-gated path."""
        if not await _confirm(ctx, f"Save preference for {email}: {note!r}?"):
            raise ToolError("Preference was not confirmed.")

        def _save() -> dict:
            with _guard(), session() as s:
                return queries.upsert_preference(s, email, note)

        result = PreferenceResult(**await anyio.to_thread.run_sync(_save))
        log.info(
            "preference saved: request_id=%s client_id=%s email=%s created=%s",
            ctx.request_id,
            ctx.client_id,
            email,
            result.created,
        )
        return result

    @mcp.tool(annotations=CREATE, tags={"write"})
    async def book_from_text(
        request: Annotated[str, Field(description="Free-text booking request", min_length=1)],
        ctx: Context,
    ) -> BookingResult:
        """Extract booking details from a natural-language request using the client's LLM
        (MCP sampling), then create the booking after the user confirms. Requires a
        sampling-capable client. Idempotent. Bypasses booking-agent's approval workflow (the
        confirmation is the gate); only registered when READ_ONLY is false. For the full
        approval workflow, use book_via_workflow.

        Production note: set FORCE_WORKFLOW_FOR_SAMPLING=true to disable the direct-write path
        and redirect all sampled bookings through the approval workflow (book_via_workflow).
        Sampled output has implicit trust risks; the workflow gate adds a human checkpoint."""
        if get_settings().force_workflow_for_sampling:
            raise ToolError(
                "book_from_text is disabled by server policy (FORCE_WORKFLOW_FOR_SAMPLING=true): "
                "sampled output must go through the approval workflow to prevent automated "
                "booking abuse. Use book_via_workflow to route the request through the "
                "booking-agent approval process."
            )
        try:
            with anyio.fail_after(get_settings().sample_timeout):
                sampled = await ctx.sample(
                    request, system_prompt=_EXTRACT_SYSTEM, temperature=0, max_tokens=512
                )
        except ValueError as e:
            # A client without sampling raises a raw protocol error; translate it.
            raise ToolError(
                "This client does not support sampling; use book_via_workflow instead."
            ) from e
        except TimeoutError as e:
            # The client's LLM took too long; don't pin the worker waiting on it.
            raise ToolError("The client's LLM did not respond in time. Please retry.") from e
        try:
            fields = BookingExtract.model_validate_json(_strip_fences(sampled.text))
        except (ValidationError, ValueError) as e:
            log.warning(
                "book_from_text extraction failed: request_id=%s error=%s",
                ctx.request_id,
                e,
            )
            raise ToolError(f"Could not extract booking details from the request: {e}") from e

        null_fields = [k for k, v in fields.model_dump().items() if v is None]
        log.info(
            "book_from_text extraction ok: request_id=%s service=%s date=%s null_fields=%s",
            ctx.request_id,
            fields.service,
            fields.date,
            null_fields or "none",
        )

        summary = f"Book {fields.service} for {fields.customer_name} on {fields.date} at {fields.time}"
        if not await _confirm(ctx, summary + "?"):
            raise ToolError("Booking was not confirmed.")

        def _create() -> dict:
            # No staff_id on this path → the booking is unassigned, so it's exempt from the
            # uq_appt_staff_slot conflict create_booking guards (no ValueError to translate).
            with _guard(), session() as s:
                return queries.create_booking(
                    s,
                    name=fields.customer_name,
                    email=fields.email,
                    phone=fields.phone,
                    address=fields.address,
                    service=fields.service,
                    date_iso=fields.date,
                    time=fields.time,
                )

        return BookingResult(**await anyio.to_thread.run_sync(_create))
