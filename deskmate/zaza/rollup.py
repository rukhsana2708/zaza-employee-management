"""Daily application-usage rollups, computed from activity periods.

For one local calendar day, every period overlapping that day is clipped to
the day's bounds and summed per application into active / idle / unknown
seconds. LOCKED periods and periods without an application are left out.
No productivity classification — just time per app.

Rollups are recomputed (upserted) whenever a period closes, for each day the
period touched. Rows are keyed by a deterministic ``usage_id`` so a
recomputed rollup updates the same record instead of adding a new one.
"""

from __future__ import annotations

from datetime import date, tzinfo
from typing import Any

from .storage import ActivityStore
from .timeutil import local_day_bounds, parse_iso

_COUNTED = {"ACTIVE": "active", "IDLE": "idle", "UNKNOWN": "unknown"}


def compute_day(
    store: ActivityStore, day: date, *, device_id: str, tz: tzinfo | None = None
) -> dict[str, dict[str, float]]:
    start, end = local_day_bounds(day, tz)
    per_app: dict[str, dict[str, float]] = {}
    for period in store.periods_overlapping(start, end):
        bucket_name = _COUNTED.get(period["status"])
        if bucket_name is None or not period["app_name"] or period["device_id"] != device_id:
            continue
        clipped = min(parse_iso(period["ended_at"]), end) - max(parse_iso(period["started_at"]), start)
        if clipped <= 0:
            continue
        bucket = per_app.setdefault(period["app_name"], {"active": 0.0, "idle": 0.0, "unknown": 0.0, "count": 0})
        bucket[bucket_name] += clipped
        bucket["count"] += 1
    return per_app


def recompute_day(
    store: ActivityStore,
    day: date,
    *,
    device_id: str,
    employee_id: str,
    at: float,
    tz: tzinfo | None = None,
) -> dict[str, dict[str, float]]:
    per_app = compute_day(store, day, device_id=device_id, tz=tz)
    with store.transaction():
        for app_name, bucket in per_app.items():
            store.upsert_app_usage(
                device_id=device_id,
                employee_id=employee_id,
                usage_date=day.isoformat(),
                app_name=app_name,
                active_seconds=bucket["active"],
                idle_seconds=bucket["idle"],
                unknown_seconds=bucket["unknown"],
                period_count=int(bucket["count"]),
                at=at,
            )
    return per_app


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_usage(rows: list[dict[str, Any]]) -> str:
    """``app_usage_daily`` rows as "app — 3h 42m" lines, busiest first."""
    lines = []
    for row in sorted(rows, key=lambda r: r["active_seconds"], reverse=True):
        extra = "".join(
            f", {format_duration(row[col])} {label}"
            for col, label in (("idle_seconds", "idle"), ("unknown_seconds", "unknown"))
            if row[col]
        )
        lines.append(f"  {row['app_name']:<32} {format_duration(row['active_seconds'])} active{extra}")
    return "\n".join(lines) if lines else "  (no application usage recorded)"
