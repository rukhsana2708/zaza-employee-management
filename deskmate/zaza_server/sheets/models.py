"""Tab layouts and the data the export reads.

The five managed tabs and their columns are defined here once; the
formatter fills them and the exporter writes them. Nothing in these layouts
exposes tokens, hashes, JSON payloads, database IDs or raw events.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..attendance.models import DailySummary, PeriodSummary
from ..attendance.rollup import month_bounds
from ..attendance.schedule import local_midnight

UTC = timezone.utc

TAB_ACTIVITY = "Activity Log"
TAB_DAILY = "Daily Summary"
TAB_WEEKLY = "Weekly Summary"
TAB_MONTHLY = "Monthly Summary"
TAB_DASHBOARD = "Dashboard"
REQUIRED_TABS = (TAB_ACTIVITY, TAB_DAILY, TAB_WEEKLY, TAB_MONTHLY, TAB_DASHBOARD)


class Kind(str, enum.Enum):
    """How a column's values are written and displayed."""

    TEXT = "TEXT"
    INTEGER = "INTEGER"          # 0
    MINUTES = "MINUTES"          # 12.5
    DATE = "DATE"                # 2026-10-08
    MONTH = "MONTH"              # 2026-10
    DATETIME = "DATETIME"        # 2026-10-08 09:00
    DATETIME_S = "DATETIME_S"    # 2026-10-08 09:00:05
    TIME = "TIME"                # 09:00:05
    HOURS = "HOURS"              # 7:30   (a duration in hours:minutes)
    DURATION = "DURATION"        # 0:12:05 (a duration in hours:minutes:seconds)
    PERCENT = "PERCENT"          # 95.12%


@dataclass(frozen=True)
class Column:
    header: str
    kind: Kind = Kind.TEXT
    width: int = 110  # pixels


@dataclass(frozen=True)
class TabSpec:
    title: str
    columns: tuple[Column, ...]

    @property
    def headers(self) -> list[str]:
        return [c.header for c in self.columns]


def _cols(*items: tuple) -> tuple[Column, ...]:
    return tuple(Column(*item) for item in items)


ACTIVITY_SPEC = TabSpec(TAB_ACTIVITY, _cols(
    ("Employee", Kind.TEXT, 150), ("Employee ID", Kind.TEXT, 110),
    ("Timestamp", Kind.DATETIME_S, 150), ("Date", Kind.DATE, 95), ("Time", Kind.TIME, 80),
    ("Period End", Kind.DATETIME_S, 150), ("Time Zone", Kind.TEXT, 120),
    ("Event / Period Type", Kind.TEXT, 130), ("Application", Kind.TEXT, 150),
    ("Window / Activity", Kind.TEXT, 280), ("Domain", Kind.TEXT, 160), ("Status", Kind.TEXT, 80),
    ("Active Duration", Kind.DURATION, 105), ("Idle Duration", Kind.DURATION, 105),
    ("Unknown Duration", Kind.DURATION, 115), ("Locked Duration", Kind.DURATION, 110),
    ("Notes", Kind.TEXT, 260),
))

DAILY_SPEC = TabSpec(TAB_DAILY, _cols(
    ("Employee", Kind.TEXT, 150), ("Employee ID", Kind.TEXT, 110), ("Date", Kind.DATE, 95),
    ("Scheduled Start", Kind.DATETIME, 135), ("Scheduled End", Kind.DATETIME, 135),
    ("Scheduled Hours", Kind.HOURS, 95), ("Tracked Hours", Kind.HOURS, 90), ("Active Hours", Kind.HOURS, 90),
    ("Idle Hours", Kind.HOURS, 80), ("Unknown Hours", Kind.HOURS, 95), ("Locked Hours", Kind.HOURS, 90),
    ("Detected Break / Idle", Kind.HOURS, 120), ("First Activity", Kind.DATETIME, 135),
    ("Last Activity", Kind.DATETIME, 135), ("Late Minutes", Kind.MINUTES, 85),
    ("Early Leave Minutes", Kind.MINUTES, 110), ("Overtime", Kind.HOURS, 80),
    ("Attendance Status", Kind.TEXT, 140), ("Attendance %", Kind.PERCENT, 95),
    ("Data Quality", Kind.TEXT, 95), ("Time Zone", Kind.TEXT, 120), ("Notes / Quality Flags", Kind.TEXT, 320),
))

_ROLLUP_TAIL = (
    ("Scheduled Hours", Kind.HOURS, 95), ("Tracked Hours", Kind.HOURS, 90), ("Active Hours", Kind.HOURS, 90),
    ("Idle Hours", Kind.HOURS, 80), ("Unknown Hours", Kind.HOURS, 95), ("Locked Hours", Kind.HOURS, 90),
    ("Overtime", Kind.HOURS, 80), ("Average Active Hours / Worked Day", Kind.HOURS, 150),
    ("Attendance %", Kind.PERCENT, 95), ("Absent Days", Kind.INTEGER, 80), ("Late Days", Kind.INTEGER, 75),
    ("Early Leave Days", Kind.INTEGER, 100), ("Incomplete Days", Kind.INTEGER, 100),
    ("Data Quality", Kind.TEXT, 95), ("Notes", Kind.TEXT, 260),
)

WEEKLY_SPEC = TabSpec(TAB_WEEKLY, _cols(
    ("Employee", Kind.TEXT, 150), ("Employee ID", Kind.TEXT, 110),
    ("Week Start", Kind.DATE, 95), ("Week End", Kind.DATE, 95),
    ("Working Days", Kind.INTEGER, 90), ("Days Worked", Kind.INTEGER, 90), *_ROLLUP_TAIL,
))

MONTHLY_SPEC = TabSpec(TAB_MONTHLY, _cols(
    ("Employee", Kind.TEXT, 150), ("Employee ID", Kind.TEXT, 110), ("Month", Kind.MONTH, 80),
    ("Days Scheduled", Kind.INTEGER, 100), ("Days Worked", Kind.INTEGER, 90), *_ROLLUP_TAIL,
))

DATA_SPECS = (ACTIVITY_SPEC, DAILY_SPEC, WEEKLY_SPEC, MONTHLY_SPEC)
DASHBOARD_WIDTHS = (300, 170, 170, 170, 120)  # columns A..E


# ─── data read from PostgreSQL ─────────────────────────────────────────────


@dataclass(frozen=True)
class EmployeeRow:
    employee_id: str
    display_name: str
    timezone: str
    is_active: bool = True


@dataclass(frozen=True)
class ActivityRow:
    """One synced activity period (as stored; nothing derived)."""

    period_id: str
    employee_id: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    is_open: bool
    status: str  # ACTIVE | IDLE | UNKNOWN | LOCKED
    status_detail: str | None = None
    app_name: str | None = None
    window_title: str | None = None
    domain: str | None = None
    privacy_excluded: bool = False
    end_reason: str | None = None


@dataclass(frozen=True)
class ReportWindow:
    """What the Sheet shows. Display only: PostgreSQL keeps everything."""

    activity_since: dict[str, datetime]  # per employee: local midnight of the first shown date (UTC)
    summary_since: date | None           # first date shown in the summary tabs; None = all history


@dataclass(frozen=True)
class ReportData:
    employees: tuple[EmployeeRow, ...]
    activity: tuple[ActivityRow, ...]
    daily: tuple[DailySummary, ...]
    weekly: tuple[PeriodSummary, ...]
    monthly: tuple[PeriodSummary, ...]
    window: ReportWindow
    summaries_updated_at: datetime | None = None


def local_today(tz: str, now: datetime) -> date:
    return now.astimezone(ZoneInfo(tz)).date()


def _months_back(first_of_month: date, months: int) -> date:
    index = first_of_month.year * 12 + first_of_month.month - 1 - months
    return date(index // 12, index % 12 + 1, 1)


def report_window(employees: tuple[EmployeeRow, ...] | list[EmployeeRow], now: datetime,
                  activity_days: int, summary_months: int) -> ReportWindow:
    """- Activity Log: each employee's last ``activity_days`` local dates,
      today included (the same rule as ``recalculate --recent-days``).
    - Summary tabs: from the first day of the month ``summary_months - 1``
      months before the earliest employee-local today (0 = everything)."""
    since = {
        e.employee_id: datetime.fromtimestamp(
            local_midnight(local_today(e.timezone, now) - timedelta(days=activity_days - 1), e.timezone), UTC)
        for e in employees
    }
    if summary_months == 0:
        return ReportWindow(since, None)
    earliest = min([local_today(e.timezone, now) for e in employees] or [now.astimezone(UTC).date()])
    return ReportWindow(since, _months_back(month_bounds(earliest)[0], summary_months - 1))


def overlaps(period: ActivityRow, cutoff: datetime) -> bool:
    """Interval overlap with [cutoff, ∞): a period running into the window is
    kept whole (never split or duplicated); one starting exactly at the
    cutoff, even with zero length, is kept too."""
    return period.ended_at > cutoff or period.started_at >= cutoff


def apply_window(data: ReportData) -> ReportData:
    """Keep only what the window shows (sources may over-fetch)."""
    w = data.window
    known = {e.employee_id for e in data.employees}
    activity = tuple(a for a in data.activity if a.employee_id in known and overlaps(a, w.activity_since[a.employee_id]))
    if w.summary_since is None:
        daily, weekly, monthly = data.daily, data.weekly, data.monthly
    else:
        daily = tuple(d for d in data.daily if d.local_date >= w.summary_since)
        weekly = tuple(p for p in data.weekly if p.period_end >= w.summary_since)
        monthly = tuple(p for p in data.monthly if p.period_end >= w.summary_since)
    return ReportData(data.employees, activity, daily, weekly, monthly, w, data.summaries_updated_at)


@dataclass
class TabValues:
    """Prepared values for one tab: every cell is a str, int or float
    (never None: an empty cell is written as "" so old content is replaced)."""

    title: str
    rows: list[list] = field(default_factory=list)  # including the header row
    width: int = 0                                  # managed columns (A..)
