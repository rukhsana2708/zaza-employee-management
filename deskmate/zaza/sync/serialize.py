"""SQLite row -> wire record (``protocol.py``).

Only summarized tables are mapped; there is deliberately no mapping for
``activity_events``. Sync bookkeeping columns (sync_status, attempts, ...)
are local and never sent.
"""

from __future__ import annotations

from datetime import date, tzinfo
from typing import Any

from ..timeutil import iso_utc, local_day_bounds
from .protocol import TABLE_RECORD_TYPES


def _bool(value: Any) -> bool:
    return bool(value)


def _session_data(row: dict) -> dict:
    return {
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "status": row["status"],
        "start_reason": row["start_reason"],
        "end_reason": row["end_reason"],
        "previous_session_id": row["previous_session_id"],
        "tracked_seconds": row["tracked_seconds"],
        "active_seconds": row["active_seconds"],
        "idle_seconds": row["idle_seconds"],
        "unknown_seconds": row["unknown_seconds"],
        "locked_seconds": row["locked_seconds"],
    }


def _period_data(row: dict) -> dict:
    return {
        "session_id": row["session_id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_seconds": row["duration_seconds"],
        "is_open": _bool(row["is_open"]),
        "status": row["status"],
        "status_detail": row["status_detail"],
        "app_name": row["app_name"],
        "window_title": row["window_title"],
        "domain": row["domain"],
        "privacy_excluded": _bool(row["privacy_excluded"]),
        "start_reason": row["start_reason"],
        "end_reason": row["end_reason"],
    }


def _idle_data(row: dict) -> dict:
    return {
        "session_id": row["session_id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_seconds": row["duration_seconds"],
        "is_open": _bool(row["is_open"]),
        "end_reason": row["end_reason"],
    }


def _usage_data(row: dict, tz: tzinfo | None) -> dict:
    start, end = local_day_bounds(date.fromisoformat(row["usage_date"]), tz)
    return {
        "usage_date": row["usage_date"],
        "day_start_utc": iso_utc(start),
        "day_end_utc": iso_utc(end),
        "app_name": row["app_name"],
        "active_seconds": row["active_seconds"],
        "idle_seconds": row["idle_seconds"],
        "unknown_seconds": row["unknown_seconds"],
        "period_count": row["period_count"],
    }


_ID_COLUMN = {
    "work_sessions": "session_id",
    "activity_periods": "period_id",
    "idle_periods": "idle_id",
    "app_usage_daily": "usage_id",
}


def to_wire(table: str, row: dict, *, employee_id: str | None, tz: tzinfo | None = None) -> dict:
    """Wire record for one row of a synced table. ``employee_id`` is the
    agent's configured employee; tables without an employee column still
    carry it so the server can check the device binding."""
    if table not in TABLE_RECORD_TYPES:
        raise ValueError(f"table is not synced: {table!r}")
    if table == "work_sessions":
        data = _session_data(row)
    elif table == "activity_periods":
        data = _period_data(row)
    elif table == "idle_periods":
        data = _idle_data(row)
    else:
        data = _usage_data(row, tz)
    return {
        "record_type": TABLE_RECORD_TYPES[table],
        "record_id": row[_ID_COLUMN[table]],
        "record_version": row["record_version"],
        "device_id": row["device_id"],
        "employee_id": row.get("employee_id", employee_id),
        "local_seq": row["local_seq"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "data": data,
    }
