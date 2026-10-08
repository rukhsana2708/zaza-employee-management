"""PostgreSQL rows → Sheet values and formatting requests (pure functions).

Values are written with ``valueInputOption=RAW``: text is never evaluated
as a formula, so a window title such as ``=HYPERLINK(...)`` stays text.
Dates and times are written as spreadsheet serial numbers (days since
1899-12-30) **already converted to the employee's reporting timezone**, and
displayed with explicit number formats, so the spreadsheet's own locale and
time-zone settings change nothing. Durations are fractions of a day shown
as ``[h]:mm`` or ``[h]:mm:ss``; percentages are fractions shown as ``0.00%``.

Nothing here recalculates attendance: every figure is a stored PostgreSQL
summary value, only converted for display. Team totals on the Dashboard are
plain sums of stored seconds, and team Attendance % is Σ credit ÷ Σ basis —
the definition in ARCHITECTURE.md §4.11.7.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ..attendance.models import AttendanceStatus, DailySummary, PeriodSummary
from ..attendance.rollup import month_bounds, week_bounds
from .models import (
    ACTIVITY_SPEC,
    DAILY_SPEC,
    DASHBOARD_WIDTHS,
    MONTHLY_SPEC,
    TAB_DASHBOARD,
    WEEKLY_SPEC,
    ActivityRow,
    EmployeeRow,
    Kind,
    ReportData,
    TabSpec,
    TabValues,
    local_today,
)

UTC = timezone.utc
_EPOCH = datetime(1899, 12, 30)
_EPOCH_DATE = date(1899, 12, 30)

NUMBER_FORMATS = {
    Kind.INTEGER: {"type": "NUMBER", "pattern": "0"},
    Kind.MINUTES: {"type": "NUMBER", "pattern": "0.0"},
    Kind.DATE: {"type": "DATE", "pattern": "yyyy-mm-dd"},
    Kind.MONTH: {"type": "DATE", "pattern": "yyyy-mm"},
    Kind.DATETIME: {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm"},
    Kind.DATETIME_S: {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm:ss"},
    Kind.TIME: {"type": "TIME", "pattern": "hh:mm:ss"},
    Kind.HOURS: {"type": "TIME", "pattern": "[h]:mm"},
    Kind.DURATION: {"type": "TIME", "pattern": "[h]:mm:ss"},
    Kind.PERCENT: {"type": "PERCENT", "pattern": "0.00%"},
}

STATUS_LABELS = {
    AttendanceStatus.PRESENT: "Present",
    AttendanceStatus.LATE: "Late",
    AttendanceStatus.EARLY_LEAVE: "Early leave",
    AttendanceStatus.LATE_AND_EARLY: "Late and early leave",
    AttendanceStatus.ABSENT: "Absent",
    AttendanceStatus.DAY_OFF: "Day off",
    AttendanceStatus.WORKED_DAY_OFF: "Worked on day off",
    AttendanceStatus.DATA_INCOMPLETE: "Data incomplete",
    AttendanceStatus.NO_SCHEDULE: "No schedule",
    AttendanceStatus.PENDING: "Pending (shift not over)",
}

STATUS_NOTES = {
    AttendanceStatus.DATA_INCOMPLETE: "Not enough reliable data to judge attendance — not counted as absent",
    AttendanceStatus.PENDING: "Shift not finished yet",
    AttendanceStatus.NO_SCHEDULE: "No schedule applies to this date",
}

FLAG_NOTES = {
    "PROVISIONAL": "Provisional (may still change)",
    "UNKNOWN_TIME": "Some monitoring time unknown",
    "START_UNCERTAIN": "Start uncertain (not charged as late)",
    "END_UNCERTAIN": "End uncertain (uncertain part not charged as early leave)",
    "AWAITING_DEVICE_SYNC": "Waiting for a device to sync",
    "NO_DEVICE": "No enabled device",
    "INTERRUPTED_SESSION": "Agent stopped unexpectedly",
    "OVERLAPPING_PERIODS": "Overlapping device data merged (not double counted)",
    "SCHEDULE_OVERLAP": "Shift overlaps another day's shift",
    "DST_ADJUSTED": "Daylight-saving change affected the shift",
}

QUALITY_LABELS = {"COMPLETE": "Complete", "PARTIAL": "Partial", "INSUFFICIENT": "Insufficient"}

PERIOD_TYPES = {"ACTIVE": "Active period", "IDLE": "Idle period", "UNKNOWN": "Unknown period",
                "LOCKED": "Locked period"}
STATUS_TEXT = {"ACTIVE": "Active", "IDLE": "Idle", "UNKNOWN": "Unknown", "LOCKED": "Locked"}
DETAIL_NOTES = {"MONITORING_UNAVAILABLE": "Monitoring unavailable", "AWAITING_INPUT": "Waiting for input monitoring",
                "TELEMETRY_GAP": "Gap in telemetry (agent not running)"}
END_REASON_NOTES = {"AGENT_INTERRUPTED": "Agent stopped unexpectedly", "TELEMETRY_GAP": "Gap in telemetry"}

# ─── value conversion ─────────────────────────────────────────────────────


def local(moment: datetime, tz: str) -> datetime:
    """A UTC (aware) instant as naive local time in ``tz``."""
    if moment.tzinfo is None:
        raise ValueError("database timestamps must be timezone-aware (UTC)")
    return moment.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def serial(moment: datetime | None, tz: str) -> float | str:
    if moment is None:
        return ""
    return (local(moment, tz) - _EPOCH).total_seconds() / 86400


def date_serial(day: date | None) -> int | str:
    return (day - _EPOCH_DATE).days if day is not None else ""


def time_serial(moment: datetime, tz: str) -> float:
    t = local(moment, tz)
    return (t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6) / 86400


def hours(seconds: float | None) -> float | str:
    return seconds / 86400 if seconds is not None else ""


def minutes(seconds: int) -> float:
    return round(seconds / 60, 1)


def percent(value: float | None) -> float | str:
    return round(value / 100, 6) if value is not None else ""


def _ratio(credit: int, basis: int) -> float | str:
    return round(credit / basis, 6) if basis > 0 else ""


def _sort_name(names: dict[str, EmployeeRow], employee_id: str) -> tuple[str, str]:
    e = names.get(employee_id)
    return ((e.display_name if e else employee_id).casefold(), employee_id)


def _name(names: dict[str, EmployeeRow], employee_id: str) -> str:
    e = names.get(employee_id)
    return e.display_name if e else employee_id


# ─── data tabs ────────────────────────────────────────────────────────────


def _activity_row(a: ActivityRow, e: EmployeeRow) -> list:
    tz = e.timezone
    durations = ["", "", "", ""]
    durations[("ACTIVE", "IDLE", "UNKNOWN", "LOCKED").index(a.status)] = hours(a.duration_seconds)
    notes = []
    if a.is_open:
        notes.append("Still in progress at last sync")
    if a.status_detail:
        notes.append(DETAIL_NOTES.get(a.status_detail, a.status_detail.replace("_", " ").capitalize()))
    if a.end_reason in END_REASON_NOTES:
        notes.append(END_REASON_NOTES[a.end_reason])
    if a.privacy_excluded:
        notes.append("Privacy-excluded (content hidden)")
    start_local = local(a.started_at, tz)
    return [
        e.display_name, e.employee_id, serial(a.started_at, tz), date_serial(start_local.date()),
        time_serial(a.started_at, tz), serial(a.ended_at, tz), tz,
        PERIOD_TYPES[a.status], a.app_name or "", a.window_title or "", a.domain or "", STATUS_TEXT[a.status],
        *durations, "; ".join(dict.fromkeys(notes)),
    ]


def activity_tab(data: ReportData) -> TabValues:
    """One row per stored activity period, newest first (ties: employee,
    then period). Raw activity events are not stored centrally and are
    never exported or invented."""
    names = {e.employee_id: e for e in data.employees}
    rows = sorted(data.activity, key=lambda a: (a.employee_id, a.period_id))
    rows.sort(key=lambda a: a.started_at, reverse=True)
    return _tab(ACTIVITY_SPEC, [_activity_row(a, names[a.employee_id]) for a in rows if a.employee_id in names])


def daily_notes(s: DailySummary) -> str:
    notes = [STATUS_NOTES[s.attendance_status]] if s.attendance_status in STATUS_NOTES else []
    notes += [FLAG_NOTES[f] for f in s.quality_flags if f in FLAG_NOTES]
    return "; ".join(notes)


def _daily_row(s: DailySummary, names: dict[str, EmployeeRow]) -> list:
    tz = s.timezone
    return [
        _name(names, s.employee_id), s.employee_id, date_serial(s.local_date),
        serial(s.scheduled_start, tz), serial(s.scheduled_end, tz), hours(s.scheduled_seconds),
        hours(s.tracked_seconds), hours(s.active_seconds), hours(s.idle_seconds), hours(s.unknown_seconds),
        hours(s.locked_seconds), hours(s.detected_break_seconds),
        serial(s.first_activity_at, tz), serial(s.last_activity_at, tz),
        minutes(s.late_seconds), minutes(s.early_leave_seconds), hours(s.overtime_seconds),
        STATUS_LABELS[s.attendance_status], percent(s.attendance_percentage),
        QUALITY_LABELS[s.data_quality.value], tz, daily_notes(s),
    ]


def daily_tab(data: ReportData) -> TabValues:
    names = {e.employee_id: e for e in data.employees}
    rows = sorted(data.daily, key=lambda s: _sort_name(names, s.employee_id))
    rows.sort(key=lambda s: s.local_date, reverse=True)
    return _tab(DAILY_SPEC, [_daily_row(s, names) for s in rows])


def _period_tail(p: PeriodSummary) -> list:
    notes = []
    if p.is_provisional:
        notes.append(f"Provisional ({'week' if p.period_kind == 'WEEK' else 'month'} in progress, "
                     "or some days may still change)")
    if p.pending_days:
        notes.append(f"{p.pending_days} day(s) pending")
    if p.worked_day_off_days:
        notes.append(f"Worked on {p.worked_day_off_days} day(s) off")
    return [
        hours(p.scheduled_seconds), hours(p.tracked_seconds), hours(p.active_seconds), hours(p.idle_seconds),
        hours(p.unknown_seconds), hours(p.locked_seconds), hours(p.overtime_seconds),
        hours(p.average_active_seconds_per_worked_day), percent(p.attendance_percentage),
        p.absent_days, p.late_days, p.early_leave_days, p.incomplete_days,
        QUALITY_LABELS[p.data_quality.value], "; ".join(notes),
    ]


def weekly_tab(data: ReportData) -> TabValues:
    names = {e.employee_id: e for e in data.employees}
    rows = sorted(data.weekly, key=lambda p: _sort_name(names, p.employee_id))
    rows.sort(key=lambda p: p.period_start, reverse=True)
    return _tab(WEEKLY_SPEC, [
        [_name(names, p.employee_id), p.employee_id, date_serial(p.period_start), date_serial(p.period_end),
         p.working_days, p.days_worked, *_period_tail(p)] for p in rows
    ])


def monthly_tab(data: ReportData) -> TabValues:
    names = {e.employee_id: e for e in data.employees}
    rows = sorted(data.monthly, key=lambda p: _sort_name(names, p.employee_id))
    rows.sort(key=lambda p: p.period_start, reverse=True)
    return _tab(MONTHLY_SPEC, [
        [_name(names, p.employee_id), p.employee_id, date_serial(p.period_start), p.working_days, p.days_worked,
         *_period_tail(p)] for p in rows
    ])


def _tab(spec: TabSpec, rows: list[list]) -> TabValues:
    assert all(len(r) == len(spec.columns) for r in rows)
    return TabValues(spec.title, [spec.headers, *rows], len(spec.columns))


# ─── dashboard ────────────────────────────────────────────────────────────

LAST_REFRESH_LABEL = "Last successful refresh"
STATUS_LABEL = "Last refresh status"
LAST_REFRESH_ROW = 4  # 1-based rows of the two status lines
STATUS_ROW = 5
DASHBOARD_COLUMNS = len(DASHBOARD_WIDTHS)


def report_timezone(employees: Sequence[EmployeeRow], configured: str | None) -> str:
    """Team-level times: the configured zone, else the active employees'
    common zone, else UTC."""
    if configured:
        return configured
    zones = {e.timezone for e in employees if e.is_active}
    return zones.pop() if len(zones) == 1 else "UTC"


def stamp(moment: datetime, tz: str) -> str:
    return f"{local(moment, tz):%Y-%m-%d %H:%M:%S} ({tz})"


def in_progress_text(started: datetime, tz: str) -> str:
    return (f"IN PROGRESS since {stamp(started, tz)} — if this is still shown, that refresh did not "
            "finish and some tabs may be partly outdated; the next refresh repairs them")


class _Team:
    """Team totals over one period for the active employees."""

    def __init__(self) -> None:
        self.seconds = dict.fromkeys(("scheduled", "tracked", "active", "idle", "unknown", "locked", "overtime"), 0)
        self.credit = self.basis = 0
        self.late: list[str] = []
        self.absent: list[str] = []
        self.incomplete: list[str] = []
        self.missing: list[str] = []

    def add(self, s: DailySummary | PeriodSummary) -> None:
        for k in self.seconds:
            self.seconds[k] += getattr(s, f"{k}_seconds")
        self.credit += s.attendance_credit_seconds
        self.basis += s.attendance_basis_seconds


def _team_today(employees: list[EmployeeRow], daily: dict, now: datetime) -> _Team:
    team = _Team()
    for e in employees:
        s = daily.get((e.employee_id, local_today(e.timezone, now)))
        if s is None:
            team.missing.append(e.display_name)
            continue
        team.add(s)
        if s.attendance_status in (AttendanceStatus.LATE, AttendanceStatus.LATE_AND_EARLY):
            team.late.append(e.display_name)
        elif s.attendance_status is AttendanceStatus.ABSENT:
            team.absent.append(e.display_name)
        elif s.attendance_status is AttendanceStatus.DATA_INCOMPLETE:
            team.incomplete.append(e.display_name)
    return team


def _team_period(employees: list[EmployeeRow], periods: dict, now: datetime, bounds) -> _Team:  # noqa: ANN001
    team = _Team()
    for e in employees:
        p = periods.get((e.employee_id, bounds(local_today(e.timezone, now))[0]))
        if p is None:
            team.missing.append(e.display_name)
            continue
        team.add(p)
        if p.late_days:
            team.late.append(e.display_name)
        if p.absent_days:
            team.absent.append(e.display_name)
        if p.incomplete_days:
            team.incomplete.append(e.display_name)
    return team


def dashboard_tab(data: ReportData, now: datetime, *, report_tz: str, activity_days: int,
                  summary_months: int) -> tuple[TabValues, list[tuple[int, int, int, Kind]], list[int]]:
    """Values, per-cell number formats ``(row0, col0, col_end, kind)`` and
    bold rows (0-based) for the Dashboard."""
    active = sorted((e for e in data.employees if e.is_active), key=lambda e: (e.display_name.casefold(), e.employee_id))
    daily = {(s.employee_id, s.local_date): s for s in data.daily}
    weekly = {(p.employee_id, p.period_start): p for p in data.weekly}
    monthly = {(p.employee_id, p.period_start): p for p in data.monthly}
    today = _team_today(active, daily, now)
    week = _team_period(active, weekly, now, week_bounds)
    month = _team_period(active, monthly, now, month_bounds)
    teams = (today, week, month)
    report_date = local_today(report_tz, now)
    ws, we = week_bounds(report_date)
    rows: list[list] = []
    formats: list[tuple[int, int, int, Kind]] = []
    bold: list[int] = []

    def add(*cells, fmt: Kind | None = None, strong: bool = False) -> None:  # noqa: ANN002
        rows.append(list(cells) + [""] * (DASHBOARD_COLUMNS - len(cells)))
        if fmt is not None:
            formats.append((len(rows) - 1, 1, 4, fmt))
        if strong:
            bold.append(len(rows) - 1)

    add("ZaZa Attendance Report", strong=True)
    add("Read-only report generated from the ZaZa database (PostgreSQL). Manual edits in this spreadsheet are "
        "overwritten at the next refresh and are never saved back.")
    add()
    add(LAST_REFRESH_LABEL, stamp(now, report_tz))
    add(STATUS_LABEL, "OK")
    add("Report time zone", f"{report_tz} (refresh time and reporting date)")
    add("Reporting date", date_serial(report_date), fmt=Kind.DATE)
    add("Active employees", len(active), fmt=Kind.INTEGER)
    add("Summaries last calculated",
        stamp(data.summaries_updated_at, report_tz) if data.summaries_updated_at else "Never")
    add("Activity Log shows", f"the last {activity_days} day(s) of each employee's local dates")
    add("Summary tabs show", "all history" if summary_months == 0 else
        f"the current month and the {summary_months - 1} before it" if summary_months > 1 else "the current month")
    add()
    add("", "Today", "This Week", "This Month", strong=True)
    add("Period", report_date.isoformat(), f"{ws.isoformat()} – {we.isoformat()}", f"{report_date:%B %Y}")
    for label, key in (("Scheduled Hours", "scheduled"), ("Tracked Hours", "tracked"), ("Active Hours", "active"),
                       ("Idle Hours", "idle"), ("Unknown Hours", "unknown"), ("Locked Hours", "locked"),
                       ("Overtime", "overtime")):
        add(label, *(hours(t.seconds[key]) for t in teams), fmt=Kind.HOURS)
    add("Attendance %", *(_ratio(t.credit, t.basis) for t in teams), fmt=Kind.PERCENT)
    add("Late employees", *(len(t.late) for t in teams), fmt=Kind.INTEGER)
    add("Absent employees", *(len(t.absent) for t in teams), fmt=Kind.INTEGER)
    add("Data-incomplete employees", *(len(t.incomplete) for t in teams), fmt=Kind.INTEGER)
    add("Employees without a calculated summary", *(len(t.missing) for t in teams), fmt=Kind.INTEGER)
    add()
    add("Late (names)", *(", ".join(t.late) for t in teams))
    add("Absent (names)", *(", ".join(t.absent) for t in teams))
    add("Data incomplete (names)", *(", ".join(t.incomplete) for t in teams))
    add()
    add("How to read this", "Today / This Week / This Month use each employee's own local date and time "
        "zone; weeks run Monday–Sunday. For a week or month, Late / Absent / Data-incomplete count employees "
        "with at least one such day. Attendance % = active time in scheduled shifts ÷ observable scheduled "
        "time. It is not a productivity score. Detected Break / Idle is not an official break.")
    add()
    add("Today by employee", strong=True)
    add("Employee", "Status", "Active Hours", "Attendance %", "Data Quality", strong=True)
    for e in active:
        s = daily.get((e.employee_id, local_today(e.timezone, now)))
        if s is None:
            add(e.display_name, "No summary calculated yet")
            continue
        add(e.display_name, STATUS_LABELS[s.attendance_status], hours(s.active_seconds),
            percent(s.attendance_percentage), QUALITY_LABELS[s.data_quality.value])
        formats.append((len(rows) - 1, 2, 3, Kind.HOURS))
        formats.append((len(rows) - 1, 3, 4, Kind.PERCENT))
    return TabValues(TAB_DASHBOARD, rows, DASHBOARD_COLUMNS), formats, bold


# ─── formatting requests (Sheets API batchUpdate) ─────────────────────────

HEADER_BG = {"red": 0.93, "green": 0.93, "blue": 0.93}


def _range(sheet_id: int, r0: int, r1: int | None, c0: int, c1: int) -> dict:
    rng = {"sheetId": sheet_id, "startRowIndex": r0, "startColumnIndex": c0, "endColumnIndex": c1}
    if r1 is not None:
        rng["endRowIndex"] = r1
    return rng


def _number_format(sheet_id: int, r0: int, r1: int | None, c0: int, c1: int, kind: Kind) -> dict:
    return {"repeatCell": {
        "range": _range(sheet_id, r0, r1, c0, c1),
        "cell": {"userEnteredFormat": {"numberFormat": NUMBER_FORMATS[kind]}},
        "fields": "userEnteredFormat.numberFormat",
    }}


def _widths(sheet_id: int, widths: Iterable[int]) -> list[dict]:
    return [{"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
        "properties": {"pixelSize": w}, "fields": "pixelSize"}} for i, w in enumerate(widths)]


def grid_request(sheet_id: int, rows: int, columns: int) -> dict:
    return {"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": {"rowCount": rows, "columnCount": columns}},
        "fields": "gridProperties.rowCount,gridProperties.columnCount"}}


def data_tab_layout(sheet_id: int, spec: TabSpec) -> list[dict]:
    """Bold, shaded, frozen header row; column widths; number formats for
    every data row of each column."""
    n = len(spec.columns)
    requests = [
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {
            "range": _range(sheet_id, 0, 1, 0, n),
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": HEADER_BG,
                                           "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(textFormat,backgroundColor,wrapStrategy,verticalAlignment)"}},
        *_widths(sheet_id, (c.width for c in spec.columns)),
    ]
    requests += [_number_format(sheet_id, 1, None, i, i + 1, c.kind)
                 for i, c in enumerate(spec.columns) if c.kind in NUMBER_FORMATS]
    return requests


def dashboard_layout(sheet_id: int, formats: list[tuple[int, int, int, Kind]], bold: list[int],
                     rows: int) -> list[dict]:
    """Widths, title, bold section rows and per-cell number formats. With
    ``rows`` > 0 the managed area's formatting is reset first, so formats
    from an older layout don't linger."""
    requests = [
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 0}},
                                   "fields": "gridProperties.frozenRowCount"}},
        *_widths(sheet_id, DASHBOARD_WIDTHS),
    ]
    if rows:
        requests.append({"repeatCell": {"range": _range(sheet_id, 0, rows, 0, DASHBOARD_COLUMNS),
                                        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP",
                                                                       "verticalAlignment": "TOP"}},
                                        "fields": "userEnteredFormat"}})
    requests.append({"repeatCell": {"range": _range(sheet_id, 0, 1, 0, 1),
                                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14}}},
                                    "fields": "userEnteredFormat.textFormat"}})
    requests += [{"repeatCell": {"range": _range(sheet_id, r, r + 1, 0, DASHBOARD_COLUMNS),
                                 "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                                "backgroundColor": HEADER_BG}},
                                 "fields": "userEnteredFormat(textFormat,backgroundColor)"}}
                 for r in bold if r > 0]
    requests += [_number_format(sheet_id, r, r + 1, c0, c1, kind) for r, c0, c1, kind in formats]
    return requests
