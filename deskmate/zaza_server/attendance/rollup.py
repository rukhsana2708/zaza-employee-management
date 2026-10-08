"""Weekly and monthly summaries, built only from daily summaries.

Week = ISO week, Monday → Sunday of local dates. Month = calendar month of
local dates. Sums are plain sums of the daily integers, so a week always
equals the sum of its days (no re-derivation from raw data that could
disagree).
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from datetime import date, timedelta

from .models import AttendanceStatus, DailySummary, DataQuality, PeriodSummary

_SUMMED = (
    "scheduled_seconds", "measurable_scheduled_seconds", "tracked_seconds", "active_seconds", "idle_seconds",
    "unknown_seconds", "locked_seconds", "active_in_shift_seconds", "detected_break_seconds", "late_seconds",
    "early_leave_seconds", "overtime_seconds", "attendance_credit_seconds", "attendance_basis_seconds",
)


def week_bounds(any_day: date) -> tuple[date, date]:
    start = any_day - timedelta(days=any_day.isoweekday() - 1)
    return start, start + timedelta(days=6)


def month_bounds(any_day: date) -> tuple[date, date]:
    start = any_day.replace(day=1)
    return start, start.replace(day=calendar.monthrange(start.year, start.month)[1])


def dates_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _quality(days: Sequence[DailySummary]) -> DataQuality:
    if not days:
        return DataQuality.INSUFFICIENT
    insufficient = sum(d.data_quality is DataQuality.INSUFFICIENT for d in days)
    working = sum(d.is_working_day for d in days) or len(days)
    if insufficient * 2 > working:
        return DataQuality.INSUFFICIENT
    if all(d.data_quality is DataQuality.COMPLETE for d in days):
        return DataQuality.COMPLETE
    return DataQuality.PARTIAL


def aggregate(
    employee_id: str, kind: str, start: date, end: date, timezone: str, days: Sequence[DailySummary]
) -> PeriodSummary:
    days = [d for d in days if start <= d.local_date <= end]
    S = AttendanceStatus
    sums = {name: sum(getattr(d, name) for d in days) for name in _SUMMED}
    days_worked = sum(d.worked_day for d in days)
    basis = sums["attendance_basis_seconds"]
    return PeriodSummary(
        employee_id=employee_id,
        period_kind=kind,
        period_start=start,
        period_end=end,
        timezone=timezone,
        working_days=sum(d.is_working_day for d in days),
        days_worked=days_worked,
        **sums,
        attendance_percentage=round(sums["attendance_credit_seconds"] / basis * 100, 2) if basis else None,
        average_active_seconds_per_worked_day=(sums["active_seconds"] // days_worked) if days_worked else None,
        absent_days=sum(d.attendance_status is S.ABSENT for d in days),
        late_days=sum(d.attendance_status in (S.LATE, S.LATE_AND_EARLY) for d in days),
        early_leave_days=sum(d.attendance_status in (S.EARLY_LEAVE, S.LATE_AND_EARLY) for d in days),
        incomplete_days=sum(d.attendance_status is S.DATA_INCOMPLETE for d in days),
        worked_day_off_days=sum(d.attendance_status is S.WORKED_DAY_OFF for d in days),
        pending_days=sum(d.attendance_status is S.PENDING for d in days),
        data_quality=_quality(days),
        is_provisional=any(d.is_provisional for d in days) or len(days) < (end - start).days + 1,
    )
