"""Timestamp helpers.

All persisted timestamps are UTC ISO-8601 strings in one fixed format
(millisecond precision, ``+00:00`` suffix), so they sort and compare
correctly as plain strings in SQLite.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo


def iso_utc(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def local_day_bounds(day: date, tz: tzinfo | None = None) -> tuple[float, float]:
    """Epoch seconds of [start, end) for ``day`` in ``tz`` (local time if None)."""
    start = datetime.combine(day, time.min)
    end = datetime.combine(day + timedelta(days=1), time.min)
    if tz is None:
        return start.astimezone().timestamp(), end.astimezone().timestamp()
    return start.replace(tzinfo=tz).timestamp(), end.replace(tzinfo=tz).timestamp()


def local_days_between(start: float, end: float, tz: tzinfo | None = None) -> list[date]:
    """Every local calendar day touched by the interval [start, end]."""
    first = datetime.fromtimestamp(start, tz).date() if tz else datetime.fromtimestamp(start).date()
    last = datetime.fromtimestamp(end, tz).date() if tz else datetime.fromtimestamp(end).date()
    days = []
    current = first
    while current <= last:
        days.append(current)
        current += timedelta(days=1)
    return days
