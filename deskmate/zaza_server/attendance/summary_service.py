"""Summary calculation service: load inputs, calculate, upsert.

- ``calculate_daily(employee_id, local_date)``
- ``calculate_week(employee_id, any_date_in_week)``   (Monday → Sunday)
- ``calculate_month(employee_id, any_date_in_month)``
- ``recalculate(start, end, employee_ids=None)``      (days, then the weeks and
  months they touch)
- ``recalculate_recent(days, employee_ids=None)``     (the last N local dates,
  today included, evaluated in EACH employee's own timezone)

Every call is idempotent: the same inputs give the same rows, keyed by
employee + date / week / month, and an unchanged result writes nothing.
Weekly and monthly figures are aggregated from the stored daily rows,
which are (re)calculated first by default. Days after the employee's local
"today" are never calculated, so a week or month in progress covers the
days so far (and is marked provisional).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .calculator import calculate_daily, window_bounds
from .models import AttendancePolicy, DailySummary, PeriodSummary
from .rollup import aggregate, dates_between, month_bounds, week_bounds
from .store import AttendanceStore

logger = logging.getLogger("zaza_server.attendance")
UTC = timezone.utc


class UnknownEmployee(KeyError):
    pass


def local_date_at(tz: str, now: datetime) -> date:
    """The calendar date in ``tz`` at the instant ``now``."""
    return now.astimezone(ZoneInfo(tz)).date()


@dataclass(frozen=True)
class RecalculationResult:
    days: int
    days_changed: int
    weeks: int
    months: int


class SummaryService:
    def __init__(
        self,
        store: AttendanceStore,
        *,
        policy: AttendancePolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.policy = policy or AttendancePolicy()
        self.clock = clock

    def _employee(self, employee_id: str):  # noqa: ANN202
        employee = self.store.get_employee(employee_id)
        if employee is None:
            raise UnknownEmployee(employee_id)
        return employee

    def compute_daily(self, employee_id: str, local_date: date) -> DailySummary:
        """Calculate without saving."""
        employee = self._employee(employee_id)
        rules = self.store.schedules(employee_id)
        lo, hi = window_bounds(rules, local_date, employee.timezone, self.policy)
        start, end = datetime.fromtimestamp(lo, UTC), datetime.fromtimestamp(hi, UTC)
        return calculate_daily(
            employee_id=employee_id,
            employee_tz=employee.timezone,
            local_date=local_date,
            rules=rules,
            periods=self.store.periods(employee_id, start, end),
            sessions=self.store.sessions(employee_id, start, end),
            devices=self.store.devices(employee_id),
            now=self.clock(),
            policy=self.policy,
        )

    def calculate_daily(self, employee_id: str, local_date: date) -> DailySummary:
        summary = self.compute_daily(employee_id, local_date)
        self.store.save_daily(summary)
        return summary

    def local_today(self, employee_id: str) -> date:
        return local_date_at(self._employee(employee_id).timezone, self.clock())

    def _aggregate(self, employee_id: str, kind: str, start: date, end: date, refresh: bool) -> PeriodSummary:
        employee = self._employee(employee_id)
        if refresh:
            for d in dates_between(start, min(end, self.local_today(employee_id))):
                self.calculate_daily(employee_id, d)
        summary = aggregate(employee_id, kind, start, end, employee.timezone,
                            self.store.list_daily(employee_id, start, end))
        self.store.save_period(summary)
        return summary

    def calculate_week(self, employee_id: str, any_day: date, *, refresh_daily: bool = True) -> PeriodSummary:
        start, end = week_bounds(any_day)
        return self._aggregate(employee_id, "WEEK", start, end, refresh_daily)

    def calculate_month(self, employee_id: str, any_day: date, *, refresh_daily: bool = True) -> PeriodSummary:
        start, end = month_bounds(any_day)
        return self._aggregate(employee_id, "MONTH", start, end, refresh_daily)

    def _recalculate_employee(self, employee_id: str, start: date, end: date,
                              weeks: set[date], months: set[date]) -> tuple[int, int]:
        self._employee(employee_id)
        # Whole weeks and months, so every roll-up is built from fresh days;
        # never past the employee's local today.
        today = self.local_today(employee_id)
        lo = min(week_bounds(start)[0], month_bounds(start)[0])
        hi = min(max(week_bounds(end)[1], month_bounds(end)[1]), today)
        days = changed = 0
        for d in dates_between(lo, hi):
            changed += self.store.save_daily(self.compute_daily(employee_id, d))
            days += 1
        touched = dates_between(start, min(end, today))
        week_starts = {week_bounds(d)[0] for d in touched}
        month_starts = {month_bounds(d)[0] for d in touched}
        for ws in sorted(week_starts):
            self.calculate_week(employee_id, ws, refresh_daily=False)
        for ms in sorted(month_starts):
            self.calculate_month(employee_id, ms, refresh_daily=False)
        weeks |= week_starts
        months |= month_starts
        return days, changed

    def _ids(self, employee_ids: Iterable[str] | None) -> list[str]:
        return list(employee_ids) if employee_ids else [e.employee_id for e in self.store.list_employees()]

    def _result(self, days: int, changed: int, weeks: set[date], months: set[date]) -> RecalculationResult:
        logger.info("recalculated %d day(s), %d changed, %d week(s), %d month(s)",
                    days, changed, len(weeks), len(months))
        return RecalculationResult(days, changed, len(weeks), len(months))

    def recalculate(self, start: date, end: date, employee_ids: Iterable[str] | None = None) -> RecalculationResult:
        """Recalculate [start, end] (local dates) for each employee, expanded
        to whole weeks/months and capped at that employee's local today."""
        if end < start:
            raise ValueError("end date is before start date")
        days = changed = 0
        weeks: set[date] = set()
        months: set[date] = set()
        for employee_id in self._ids(employee_ids):
            d, c = self._recalculate_employee(employee_id, start, end, weeks, months)
            days, changed = days + d, changed + c
        return self._result(days, changed, weeks, months)

    def recent_range(self, employee_id: str, recent_days: int) -> tuple[date, date]:
        """The last ``recent_days`` local dates for this employee, today
        included: 7 on October 8 (local) = October 2 .. October 8."""
        if isinstance(recent_days, bool) or not isinstance(recent_days, int) or recent_days < 1:
            raise ValueError("recent days must be a whole number >= 1")
        today = self.local_today(employee_id)
        return today - timedelta(days=recent_days - 1), today

    def recalculate_recent(self, recent_days: int, employee_ids: Iterable[str] | None = None) -> RecalculationResult:
        """``recalculate`` over each employee's own last ``recent_days``
        local dates (employees in different timezones can have different
        "today"s)."""
        if isinstance(recent_days, bool) or not isinstance(recent_days, int) or recent_days < 1:
            raise ValueError("recent days must be a whole number >= 1")
        days = changed = 0
        weeks: set[date] = set()
        months: set[date] = set()
        for employee_id in self._ids(employee_ids):
            start, end = self.recent_range(employee_id, recent_days)
            d, c = self._recalculate_employee(employee_id, start, end, weeks, months)
            days, changed = days + d, changed + c
        return self._result(days, changed, weeks, months)
