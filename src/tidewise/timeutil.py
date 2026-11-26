"""Parsing and formatting times.

Rules, kept the same across all tools:
- "now" means the current time.
- A date only ("2026-11-26") means that whole day in the station's local time.
- A date and time with no offset is read as station local time.
- A date and time with an offset or "Z" is used as given.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo

from .errors import TidewiseError

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_time(value: str, tz: tzinfo, *, now: datetime, end: bool = False) -> datetime:
    """Turn user text into an aware UTC datetime. `end` makes a date-only value
    mean the end of that day instead of the start."""
    text = value.strip()
    if text.lower() == "now":
        return now
    try:
        if _DATE_ONLY.match(text):
            start = datetime.fromisoformat(text).replace(tzinfo=tz)
            if end:
                start = (datetime.fromisoformat(text) + timedelta(days=1)).replace(tzinfo=tz)
            return start.astimezone(UTC)
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise TidewiseError(
            f"Could not read the time {value!r}. Use ISO 8601 such as "
            "'2026-11-26', '2026-11-26T08:30' (station local time), "
            "'2026-11-26T12:30:00Z' (UTC), or 'now'."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def check_range(start: datetime, end: datetime, *, max_days: int = 7) -> None:
    if end <= start:
        raise TidewiseError("The end time must be after the start time.")
    if end - start > timedelta(days=max_days):
        raise TidewiseError(
            f"The time range is {(end - start).total_seconds() / 86400:.1f} days. "
            f"Please ask for at most {max_days} days at a time."
        )


def iso_utc(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_local(t: datetime, tz: tzinfo) -> str:
    return t.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def api_time(t: datetime) -> str:
    """Format for the IWLS query string."""
    return iso_utc(t)
