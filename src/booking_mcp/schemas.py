"""Pydantic DTOs — the structured output clients receive from tools/resources."""

from __future__ import annotations

from pydantic import BaseModel

from .validation import DateArg, EmailArg, TimeArg


class StaffDTO(BaseModel):
    id: str
    name: str
    skills: list[str] = []
    latitude: float | None = None
    longitude: float | None = None
    bio: str | None = None

    @classmethod
    def from_row(cls, s) -> StaffDTO:
        return cls(
            id=s.id,
            name=s.name,
            skills=list(s.skills or ()),
            latitude=s.latitude,
            longitude=s.longitude,
            bio=s.bio,
        )


class ContactDTO(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None


class MemoryDTO(BaseModel):
    type: str
    content: dict


class ClientDTO(BaseModel):
    id: str
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    contacts: list[ContactDTO] = []
    memories: list[MemoryDTO] = []


class AppointmentDTO(BaseModel):
    id: str
    staff_id: str | None = None
    staff_name: str | None = None
    start_date: str
    service: str | None = None

    @classmethod
    def from_row(cls, appt, service: str | None = None) -> AppointmentDTO:
        return cls(
            id=appt.id,
            staff_id=appt.staff_id,
            staff_name=appt.staff_name,
            # start_date is a datetime column; render the stable wall-clock string.
            start_date=appt.start_date.strftime("%Y-%m-%d %H:%M:%S"),
            service=service,
        )


class BookingResult(BaseModel):
    client_id: str
    job_id: str
    appointment_id: str
    staff_id: str | None = None
    start_date: str
    idempotent: bool  # True when an identical booking already existed (deduped)


class NextAvailable(BaseModel):
    date: str
    time: str
    staff_id: str
    staff_name: str


class CancelResult(BaseModel):
    appointment_id: str
    cancelled: bool  # False if it didn't exist (idempotent)


class RescheduleResult(BaseModel):
    appointment_id: str
    start_date: str
    staff_id: str | None = None


class PreferenceResult(BaseModel):
    customer_key: str
    note: str
    created: bool


class BookingExtract(BaseModel):
    """Booking details the client's LLM extracts from a free-text request (``book_from_text``).
    Fields reuse the typed tool args' validation, so a hallucinated date/time/email is rejected
    before any write."""

    customer_name: str
    email: EmailArg
    service: str
    date: DateArg
    time: TimeArg
    phone: str | None = None
    address: str | None = None


class WorkflowRun(BaseModel):
    """A booking-agent run as seen over the HTTP bridge (mirrors its RunResponse).
    ``approval_card`` is set while paused for a human decision; ``final_response`` once done."""

    run_id: str
    status: str
    node_statuses: dict[str, str] = {}
    approval_card: dict | None = None
    final_response: str | None = None
