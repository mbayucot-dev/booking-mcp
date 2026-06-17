"""Data-access layer: read/write query functions over the shared schema.

Mirrors booking-agent's repository logic for the slice this server exposes (the
skill/free@hour/geo hard filter, get-or-create-by-email, idempotent booking).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date as _date
from datetime import datetime, timedelta
from math import cos, radians

from sqlalchemy import and_, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    Appointment,
    Client,
    Contact,
    CustomerMemory,
    ExecutedAction,
    Job,
    Staff,
    StaffSkill,
)

log = logging.getLogger("booking_mcp.queries")

# Result format for the appointment start (matches the legacy string shape).
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _bbox(lat: float, lng: float, radius_km: float):
    """Cheap, index-usable lat/lng box (~111 km per degree)."""
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(cos(radians(lat)), 0.01))
    return lat - dlat, lat + dlat, lng - dlng, lng + dlng


# --- reads ----------------------------------------------------------------


def active_staff(session: Session, skill: str | None = None) -> list[Staff]:
    q = select(Staff).where(Staff.active.is_(True))
    if skill:
        q = q.where(
            exists().where(and_(StaffSkill.staff_id == Staff.id, StaffSkill.skill == skill))
        )
    return list(session.scalars(q.order_by(Staff.name)))


def staff_by_id(session: Session, staff_id: str) -> Staff | None:
    return session.get(Staff, staff_id)


def eligible_staff(
    session: Session,
    *,
    date_iso: str,
    time: str,
    skill: str,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float = 25.0,
    limit: int = 50,
) -> list[Staff]:
    """Active staff with the skill, free at the requested hour, and (if coords given) within
    the geo box — the same hard filter booking-agent uses."""
    start = datetime.fromisoformat(f"{date_iso} {time}:00")
    q = (
        select(Staff)
        .where(Staff.active.is_(True))
        .where(exists().where(and_(StaffSkill.staff_id == Staff.id, StaffSkill.skill == skill)))
        .where(
            ~exists().where(and_(Appointment.staff_id == Staff.id, Appointment.start_date == start))
        )
    )
    if lat is not None and lng is not None:
        min_la, max_la, min_lo, max_lo = _bbox(lat, lng, radius_km)
        q = q.where(
            Staff.latitude.between(min_la, max_la),
            Staff.longitude.between(min_lo, max_lo),
        )
    return list(session.scalars(q.order_by(Staff.name).limit(limit)))


def appointments_with_service(
    session: Session, date_iso: str, limit: int = 2000
) -> list[tuple[Appointment, str | None]]:
    """Appointments on a date, each joined to its job's service in one query (no N+1)."""
    day = datetime.fromisoformat(f"{date_iso} 00:00:00")
    rows = session.execute(
        select(Appointment, Job.service)
        .outerjoin(Job, Job.id == Appointment.job_id)
        .where(Appointment.start_date >= day, Appointment.start_date < day + timedelta(days=1))
        .limit(limit)
    ).all()
    return [(appt, service) for appt, service in rows]


def client_by_email(session: Session, email: str) -> Client | None:
    return session.scalar(select(Client).where(Client.email == email))


def contacts_for(session: Session, client_id: str) -> list[Contact]:
    return list(session.scalars(select(Contact).where(Contact.client_id == client_id)))


def memories_for(session: Session, customer_key: str) -> list[CustomerMemory]:
    return list(
        session.scalars(
            select(CustomerMemory)
            .where(CustomerMemory.customer_key == customer_key)
            .order_by(CustomerMemory.memory_type)
        )
    )


def find_next_available(
    session: Session,
    *,
    service: str,
    date_iso: str,
    time: str,
    days: int = 7,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float = 25.0,
) -> dict | None:
    """First day (from ``date_iso``, within ``days``) that has a staff member free
    for the service at ``time``. Returns that slot, or None if none in the window."""
    base = _date.fromisoformat(date_iso)
    for offset in range(days):
        day = (base + timedelta(days=offset)).isoformat()
        staff = eligible_staff(
            session,
            date_iso=day,
            time=time,
            skill=service,
            lat=lat,
            lng=lng,
            radius_km=radius_km,
            limit=1,
        )
        if staff:
            return {"date": day, "time": time, "staff_id": staff[0].id, "staff_name": staff[0].name}
    return None


# --- writes (idempotent) --------------------------------------------------


def _booking_key(**fields: object) -> str:
    """Stable key over all material booking fields, so identical requests dedupe but a
    genuinely different booking (e.g. a different person on the same slot) does not."""
    raw = "|".join(f"{k}={fields[k]}" for k in sorted(fields))
    return "mcp:create_booking:" + hashlib.sha256(raw.encode()).hexdigest()[:48]


def create_booking(
    session: Session,
    *,
    name: str | None,
    email: str | None,
    phone: str | None,
    address: str | None,
    service: str | None,
    date_iso: str,
    time: str,
    staff_id: str | None = None,
    staff_name: str | None = None,
) -> dict:
    """Idempotently create client (get-or-create by email) + job + appointment.

    Deduped via the ``executed_actions`` ledger on a hash of all material fields, so a
    replay with identical args returns the recorded result instead of double-booking.
    """
    key = _booking_key(
        name=name,
        email=email,
        phone=phone,
        address=address,
        service=service,
        date=date_iso,
        time=time,
        staff_id=staff_id,
    )
    prior = session.get(ExecutedAction, key)
    if prior is not None:
        return {**prior.result, "idempotent": True}

    client = client_by_email(session, email) if email else None
    if client is None:
        client = Client(name=name, email=email, phone=phone, address=address)
        session.add(client)
        session.flush()

    job = Job(client_id=client.id, service=service, address=address)
    session.add(job)
    session.flush()

    start_dt = datetime.fromisoformat(f"{date_iso} {time}:00")
    appt = Appointment(job_id=job.id, staff_id=staff_id, staff_name=staff_name, start_date=start_dt)
    session.add(appt)
    try:
        session.flush()  # INSERT happens here — uq_appt_staff_slot fires on a double-book
    except IntegrityError as e:
        session.rollback()
        if "uq_appt_staff_slot" in str(e.orig):
            raise ValueError("That staff member is already booked at this time.") from e
        raise  # pragma: no cover - some other constraint (defensive) → re-raise

    result = {
        "client_id": client.id,
        "job_id": job.id,
        "appointment_id": appt.id,
        "staff_id": staff_id,
        # String in the result/ledger (JSON-safe); the column holds a datetime.
        "start_date": start_dt.strftime(_TS_FMT),
    }
    session.add(
        ExecutedAction(idempotency_key=key, run_id=None, action="mcp_create_booking", result=result)
    )
    try:
        session.commit()
    except IntegrityError:  # pragma: no cover - concurrent-duplicate race (defensive)
        # A concurrent identical booking won the race on the ledger key — return theirs.
        # A violation on some other constraint won't have the key present → re-raise.
        session.rollback()
        winner = session.get(ExecutedAction, key)
        if winner is None:
            raise
        log.warning("create_booking: concurrent duplicate for key %s — returning existing", key)
        return {**winner.result, "idempotent": True}
    log.info("create_booking: booked %s for %s at %s", service, email, result["start_date"])
    return {**result, "idempotent": False}


def cancel_appointment(session: Session, appointment_id: str) -> bool:
    """Delete an appointment. Idempotent: returns False if it didn't exist."""
    appt = session.get(Appointment, appointment_id)
    if appt is None:
        return False
    session.delete(appt)
    session.commit()
    return True


def reschedule_appointment(
    session: Session, *, appointment_id: str, new_date: str, new_time: str
) -> dict | None:
    """Move an appointment to a new slot. Returns None if it doesn't exist; raises
    ValueError if the assigned staff is already booked at the new time."""
    appt = session.get(Appointment, appointment_id)
    if appt is None:
        return None
    new_start = datetime.fromisoformat(f"{new_date} {new_time}:00")
    if appt.staff_id is not None:
        clash = session.scalar(
            select(Appointment).where(
                Appointment.staff_id == appt.staff_id,
                Appointment.start_date == new_start,
                Appointment.id != appointment_id,
            )
        )
        if clash is not None:
            raise ValueError("That staff member is already booked at the requested time.")
    appt.start_date = new_start
    session.commit()
    return {
        "appointment_id": appt.id,
        "start_date": new_start.strftime(_TS_FMT),
        "staff_id": appt.staff_id,
    }


def _get_preference(session: Session, email: str) -> CustomerMemory | None:
    return session.scalar(
        select(CustomerMemory).where(
            CustomerMemory.customer_key == email, CustomerMemory.memory_type == "preference"
        )
    )


def upsert_preference(session: Session, email: str, note: str) -> dict:
    row = _get_preference(session, email)
    created = row is None
    if row is None:
        row = CustomerMemory(customer_key=email, memory_type="preference", content={"note": note})
        session.add(row)
    else:
        row.content = {**(row.content or {}), "note": note}
    try:
        session.commit()
    except IntegrityError:  # pragma: no cover - concurrent first-write race (defensive)
        # Concurrent first-write on the unique key — update the row the other writer inserted.
        session.rollback()
        existing = _get_preference(session, email)
        if existing is None:
            raise
        existing.content = {**(existing.content or {}), "note": note}
        session.commit()
        created = False
    return {"customer_key": email, "note": note, "created": created}
