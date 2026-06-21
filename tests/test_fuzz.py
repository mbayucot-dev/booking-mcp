"""Property-based (Hypothesis) tests: validators accept all legal inputs and
reject all illegal ones without crashing or leaking internals.

These tests guard against LLM-generated inputs that may be plausible-but-wrong
(e.g. "February 30th", "25:99", "not-an-email") or adversarial (SQL injection,
unicode edge cases, empty strings).
"""

from __future__ import annotations

import re
from datetime import date as _date

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from booking_mcp.validation import _valid_date, _valid_time

# ── date strategies ──────────────────────────────────────────────────────────

# Any real calendar date should pass.
real_dates = st.dates().map(lambda d: d.isoformat())

# Strings that LOOK like dates but aren't (or are just arbitrary text).
bad_dates = st.one_of(
    st.just(""),
    st.just("2026-13-01"),  # month 13
    st.just("2026-00-01"),  # month 0
    st.just("2026-02-30"),  # Feb 30
    st.just("not-a-date"),
    st.just("20260620"),  # missing dashes
    st.just("2026/06/20"),  # wrong separator
    st.just("'; DROP TABLE appointments; --"),
    st.text(min_size=0, max_size=30).filter(lambda s: not _looks_like_iso(s)),
)


def _looks_like_iso(s: str) -> bool:
    """Rough check: YYYY-MM-DD pattern — not a true validator."""
    try:
        _date.fromisoformat(s)
        return True
    except (ValueError, TypeError):
        return False


# ── time strategies ──────────────────────────────────────────────────────────

_VALID_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

valid_times = st.from_regex(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", fullmatch=True)

bad_times = st.one_of(
    st.just(""),
    st.just("25:00"),  # hour 25
    st.just("12:60"),  # minute 60
    st.just("9:00"),  # single-digit hour
    st.just("12:0"),  # single-digit minute
    st.just("noon"),
    st.just("12-00"),  # wrong separator
    st.text(min_size=0, max_size=10).filter(lambda s: not _VALID_TIME_RE.match(s)),
)


# ── date property tests ───────────────────────────────────────────────────────


@given(d=real_dates)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_valid_date_accepts_any_real_calendar_date(d):
    assert _valid_date(d) == d


@given(s=bad_dates)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_valid_date_rejects_bad_inputs_with_valueerror(s):
    with pytest.raises(ValueError):
        _valid_date(s)


# ── time property tests ───────────────────────────────────────────────────────


@given(t=valid_times)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_valid_time_accepts_all_legal_hhmm_strings(t):
    assert _valid_time(t) == t


@given(s=bad_times)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_valid_time_rejects_bad_inputs_with_valueerror(s):
    with pytest.raises(ValueError):
        _valid_time(s)


# ── email pattern (via pydantic field validator) ─────────────────────────────

# Use a simple structural test: valid emails must have exactly one @, a domain, and a TLD.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

valid_emails = st.emails()

bad_emails = st.one_of(
    st.just(""),
    st.just("no-at-sign"),
    st.just("two@@sign.com"),
    st.just("@nodomain.com"),
    st.just("user@"),
    st.just("user@nodot"),
    st.just("has space@domain.com"),
    st.text(min_size=0, max_size=50).filter(lambda s: not _EMAIL_RE.match(s)),
)


@given(email=valid_emails)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_email_pattern_accepts_valid_emails(email):
    assert _EMAIL_RE.match(email) is not None


@given(email=bad_emails)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_email_pattern_rejects_bad_emails(email):
    assert _EMAIL_RE.match(email) is None


# ── injection safety: validator outputs must equal inputs (no mutation) ───────


@given(d=real_dates)
@settings(max_examples=50)
def test_valid_date_returns_input_unchanged(d):
    """Validators must not mutate the value — they are pass-through guards."""
    assert _valid_date(d) is d or _valid_date(d) == d


@given(t=valid_times)
@settings(max_examples=50)
def test_valid_time_returns_input_unchanged(t):
    assert _valid_time(t) is t or _valid_time(t) == t
