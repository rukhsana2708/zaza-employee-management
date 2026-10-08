"""Read the report data from PostgreSQL — read-only.

Everything is read in ONE ``REPEATABLE READ, READ ONLY`` transaction, so
all five tabs come from the same consistent snapshot, and the export cannot
modify the database even by mistake.

Sources:

- ``employees``                 names and reporting time zones
- ``activity_periods``          the Activity Log (one row per period). The
                                central database has no raw activity events
                                and none are exported or invented.
- ``daily_summaries``, ``weekly_summaries``, ``monthly_summaries`` — the
  authoritative Phase 5 figures, exported as stored.

Only display columns are selected: no record payloads, content hashes,
summary hashes, token data or sync bookkeeping.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime

from ..attendance.models import DailySummary, PeriodSummary
from ..attendance.postgres_store import PostgresAttendanceStore
from .client import SheetsSyncBusy
from .models import ActivityRow, EmployeeRow, ReportData, ReportWindow, apply_window, report_window

EMPLOYEES_SQL = "SELECT employee_id, display_name, timezone, is_active FROM employees ORDER BY employee_id"
ACTIVITY_SQL = (
    "SELECT period_id::text AS period_id, employee_id, started_at, ended_at, duration_seconds, is_open, status, "
    "status_detail, app_name, window_title, domain, privacy_excluded, end_reason "
    # interval overlap: a period that started before the cutoff but runs into
    # the window is shown (once, with its true start)
    "FROM activity_periods WHERE ended_at > %s OR started_at >= %s "
    "ORDER BY started_at DESC, employee_id, period_id"
)
DAILY_SQL = "SELECT * FROM daily_summaries WHERE local_date >= %s::date ORDER BY local_date, employee_id"
WEEKLY_SQL = "SELECT * FROM weekly_summaries WHERE week_end >= %s::date ORDER BY week_start, employee_id"
MONTHLY_SQL = "SELECT * FROM monthly_summaries WHERE month_end >= %s::date ORDER BY month, employee_id"
UPDATED_SQL = "SELECT max(updated_at) AS t FROM daily_summaries"
SYNC_LOCK_KEY = 0x5A5A5348  # "ZZSH": one Sheets refresh at a time


class PostgresReportSource:
    """Reads the report through ONE pooled connection, so a refresh works
    even with ``ZAZA_DB_POOL_MAX=1``:

    1. take the session advisory lock (one Sheets refresh at a time);
    2. on the same connection, read everything in one REPEATABLE READ,
       READ ONLY transaction, which then ends;
    3. keep the lock (but no transaction) while the caller writes to Google;
    4. release the lock.
    """

    def __init__(self, repo) -> None:  # noqa: ANN001 — PostgresRepository
        self.repo = repo

    @contextmanager
    def refresh(self, now: datetime, *, activity_days: int, summary_months: int) -> Iterator[ReportData]:
        """Lock, load, and keep the lock until the ``with`` block ends."""
        with self.repo.connection() as conn:
            if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (SYNC_LOCK_KEY,)).fetchone()["ok"]:
                raise SheetsSyncBusy("another Google Sheets refresh is already running")
            try:
                yield self._load(conn, now, activity_days, summary_months)
            finally:
                try:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
                except Exception:  # noqa: BLE001 — close it: the server then drops the session's lock
                    conn.close()

    def load(self, now: datetime, *, activity_days: int, summary_months: int) -> ReportData:
        with self.refresh(now, activity_days=activity_days, summary_months=summary_months) as data:
            return data

    @contextmanager
    def snapshot(self, conn=None) -> Iterator:  # noqa: ANN001
        """A consistent, read-only view of the database (any write in it
        fails). Uses ``conn`` if given, else a pooled connection."""
        if conn is None:
            with self.repo.connection() as own, self.snapshot(own) as snap:
                yield snap
            return
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            yield conn

    def _load(self, conn, now: datetime, activity_days: int, summary_months: int) -> ReportData:  # noqa: ANN001
        with self.snapshot(conn):
            employees = tuple(EmployeeRow(**r) for r in conn.execute(EMPLOYEES_SQL).fetchall())
            window = report_window(employees, now, activity_days, summary_months)
            first = min(window.activity_since.values(), default=now)
            activity = tuple(ActivityRow(**r) for r in conn.execute(ACTIVITY_SQL, (first, first)).fetchall())
            since = window.summary_since or date(1, 1, 1)
            daily = tuple(PostgresAttendanceStore._daily(r) for r in conn.execute(DAILY_SQL, (since,)).fetchall())
            weekly = tuple(PostgresAttendanceStore._period(r, "WEEK")
                           for r in conn.execute(WEEKLY_SQL, (since,)).fetchall())
            monthly = tuple(PostgresAttendanceStore._period(r, "MONTH")
                            for r in conn.execute(MONTHLY_SQL, (since,)).fetchall())
            updated = conn.execute(UPDATED_SQL).fetchone()["t"]
        return apply_window(ReportData(employees, activity, daily, weekly, monthly, window, updated))


class InMemoryReportSource:
    """Report data held in memory (tests, and the optional live test)."""

    def __init__(self, employees: list[EmployeeRow] | None = None, activity: list[ActivityRow] | None = None,
                 daily: list[DailySummary] | None = None, weekly: list[PeriodSummary] | None = None,
                 monthly: list[PeriodSummary] | None = None, summaries_updated_at: datetime | None = None) -> None:
        self.employees = list(employees or [])
        self.activity = list(activity or [])
        self.daily = list(daily or [])
        self.weekly = list(weekly or [])
        self.monthly = list(monthly or [])
        self.summaries_updated_at = summaries_updated_at
        self.loads = 0

    @contextmanager
    def refresh(self, now: datetime, *, activity_days: int, summary_months: int) -> Iterator[ReportData]:
        yield self.load(now, activity_days=activity_days, summary_months=summary_months)

    def load(self, now: datetime, *, activity_days: int, summary_months: int) -> ReportData:
        self.loads += 1
        employees = tuple(self.employees)
        data = ReportData(employees, tuple(self.activity), tuple(self.daily), tuple(self.weekly),
                          tuple(self.monthly), ReportWindow({}, None), self.summaries_updated_at)
        return apply_window(replace(data, window=report_window(employees, now, activity_days, summary_months)))
