"""Validated argument types shared by tools and resources — checked for real
calendar/clock validity, not just format."""

from __future__ import annotations

import re
from datetime import date as _date
from typing import Annotated

from pydantic import AfterValidator, Field

_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _valid_date(v: str) -> str:
    try:
        _date.fromisoformat(v)  # enforces YYYY-MM-DD and a real calendar date
    except ValueError as e:
        raise ValueError(f"{v!r} is not a valid ISO date (YYYY-MM-DD)") from e
    return v


def _valid_time(v: str) -> str:
    if not _TIME_RE.match(v) or not (0 <= int(v[:2]) <= 23 and 0 <= int(v[3:]) <= 59):
        raise ValueError(f"{v!r} is not a valid 24h time (HH:MM)")
    return v


DateArg = Annotated[str, Field(description="ISO date YYYY-MM-DD"), AfterValidator(_valid_date)]
TimeArg = Annotated[str, Field(description="24h time HH:MM"), AfterValidator(_valid_time)]
EmailArg = Annotated[
    str, Field(description="Customer email", pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
]
