"""Phase 8 — chart data and automatic, rule-based period analysis.

Deterministic, explainable, non-AI: every figure is a sum of stored Phase 5
``daily_summaries`` (or ``application_usage_daily`` for applications), and
every insight comes from an explicit rule below. No LLM, no model, no
network call, no hidden score. Nothing here combines metrics into a ranking
number; employees are only ever compared on one plainly named measurement
(recorded Active Hours), with the disclaimer that it is not a productivity
score.

Periods are the Phase 7 :class:`Selection`: each employee's own local
calendar range. The previous comparable period is derived per employee by
:func:`previous_range`.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from ..attendance.models import AttendanceStatus, DataQuality
from ..attendance.rollup import month_bounds, week_bounds
from .config import DashboardSettings
from .models import DayRow, Employee, EmployeeRange, Selection
from .service import DashboardService, employee_labels, local_today, range_label

VERSION = 1
MAX_DAILY_POINTS = 62    # longer ranges are shown per ISO week in the daily trend
WEEKLY_TREND_WEEKS = 8   # the weekly trend shows at least this many weeks
UNKNOWN_CAUTION = 0.10   # UNKNOWN above 10% of tracked time qualifies "% of scheduled"
LATE = (AttendanceStatus.LATE, AttendanceStatus.LATE_AND_EARLY)
EARLY = (AttendanceStatus.EARLY_LEAVE, AttendanceStatus.LATE_AND_EARLY)
ACTIVE_DISCLAIMER = "Active Hours measure recorded computer activity and are not a productivity score."
OVERTIME_DISCLAIMER = ("ZaZa overtime reflects recorded ACTIVE time outside the scheduled shift and is not "
                       "automatically a payroll entitlement.")
APP_DISCLAIMER = "Application usage is based on recorded application periods and is not a productivity score."


def hm(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def hours(seconds: float) -> float:
    return round(seconds / 3600, 2)


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


# ─── previous comparable period ───────────────────────────────────────────


def previous_range(kind: str, start: date, end: date) -> tuple[date, date]:
    """The previous comparable local date range for one employee.

    today / yesterday / custom → the same number of days immediately before;
    this_week / last_week → the ISO week before; this_month / last_month →
    the whole calendar month before (months differ in length)."""
    if kind in ("this_month", "last_month"):
        return month_bounds(start - timedelta(days=1))
    if kind in ("this_week", "last_week"):
        return start - timedelta(days=7), end - timedelta(days=7)
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


def previous_selection(sel: Selection) -> Selection:
    return Selection(sel.kind, tuple(EmployeeRange(r.employee, *previous_range(sel.kind, r.start, r.end))
                                     for r in sel.ranges), sel.custom_from, sel.custom_to, sel.employee_id)


def _ranges_label(sel: Selection) -> str:
    if not sel.ranges:
        return ""
    if sel.uniform:
        return range_label(sel.ranges[0].start, sel.ranges[0].end)
    return "each employee's own local period"


# ─── per-employee figures ─────────────────────────────────────────────────


@dataclass
class EmployeeFigures:
    employee: Employee
    label: str
    days: int = 0
    working_days: int = 0
    scheduled: int = 0
    tracked: int = 0
    active: int = 0
    idle: int = 0
    unknown: int = 0
    locked: int = 0
    overtime: int = 0
    credit: int = 0
    basis: int = 0
    late_days: int = 0
    early_days: int = 0
    absent_days: int = 0
    incomplete_days: int = 0
    insufficient_days: int = 0
    uncertain_days: int = 0  # START/END_UNCERTAIN or AWAITING_DEVICE_SYNC

    @property
    def eligible(self) -> bool:
        """Comparable (highest / lowest recorded Active Hours, idle share):
        at least one calculated working day, attendance basis > 0, and not
        every working day DATA_INCOMPLETE."""
        return self.working_days > 0 and self.basis > 0 and self.incomplete_days < self.working_days


def figures(rows: Iterable[DayRow], labels: dict[str, str]) -> dict[str, EmployeeFigures]:
    out: dict[str, EmployeeFigures] = {}
    for row in rows:
        s = row.summary
        f = out.setdefault(row.employee.employee_id, EmployeeFigures(row.employee, labels[row.employee.employee_id]))
        f.days += 1
        f.working_days += int(s.is_working_day)
        f.scheduled += s.scheduled_seconds
        f.tracked += s.tracked_seconds
        f.active += s.active_seconds
        f.idle += s.idle_seconds
        f.unknown += s.unknown_seconds
        f.locked += s.locked_seconds
        f.overtime += s.overtime_seconds
        f.credit += s.attendance_credit_seconds
        f.basis += s.attendance_basis_seconds
        f.late_days += s.attendance_status in LATE
        f.early_days += s.attendance_status in EARLY
        f.absent_days += s.attendance_status is AttendanceStatus.ABSENT
        f.incomplete_days += s.attendance_status is AttendanceStatus.DATA_INCOMPLETE
        f.insufficient_days += s.data_quality is DataQuality.INSUFFICIENT
        f.uncertain_days += bool({"START_UNCERTAIN", "END_UNCERTAIN", "AWAITING_DEVICE_SYNC"} & set(s.quality_flags))
    return out


def _sum(figs: Iterable[EmployeeFigures], attr: str) -> int:
    return sum(getattr(f, attr) for f in figs)


# ─── charts (data only; charts.py turns these into SVG) ───────────────────


@dataclass(frozen=True)
class Series:
    key: str     # css/legend key: active | idle | unknown | locked | scheduled | late | early | absent | incomplete
    label: str   # human-readable


STATUS_SERIES = (Series("active", "Active Hours"), Series("idle", "Idle Hours"),
                 Series("unknown", "Unknown Hours"), Series("locked", "Locked Hours"))
WEEKLY_SERIES = (Series("scheduled", "Scheduled Hours"), Series("active", "Active Hours"),
                 Series("idle", "Idle Hours"), Series("unknown", "Unknown Hours"))
ATTENDANCE_SERIES = (Series("late", "Late Days"), Series("early", "Early Leave Days"),
                     Series("absent", "Absent Days"), Series("incomplete", "Data-Incomplete Days"))


@dataclass
class ChartData:
    """One chart: categories × series values, in display units (hours or
    days). ``kind`` = bar (one series), stacked (parts of a whole), grouped
    (side by side), columns (a vertical trend)."""

    key: str
    title: str
    description: str
    unit: str          # "h" or "days"
    kind: str
    series: tuple[Series, ...]
    categories: list[str] = field(default_factory=list)
    values: list[list[float | None]] = field(default_factory=list)  # [category][series]
    note: str = ""
    empty: str = "No calculated data for this period."

    @property
    def is_empty(self) -> bool:
        """No data at all (all zeros is real data, e.g. everyone absent)."""
        return not self.categories or all(v is None for row in self.values for v in row)

    @property
    def all_zero(self) -> bool:
        return not self.is_empty and all(not v for row in self.values for v in row)

    def as_json(self) -> dict:
        return {"title": self.title, "description": self.description, "unit": self.unit, "kind": self.kind,
                "series": [s.label for s in self.series], "categories": self.categories,
                "values": self.values, "note": self.note, "empty": self.is_empty, "all_zero": self.all_zero}


# ─── insights ─────────────────────────────────────────────────────────────


class Severity(str, enum.Enum):
    INFO = "INFO"
    NOTICE = "NOTICE"
    DATA_QUALITY = "DATA_QUALITY"


# Fixed category order (lower first): data quality, attendance, active
# summary, comparison, late/early, idle, overtime, applications.
ORDER = {"DATA_QUALITY": 1, "MISSING_SUMMARIES": 1, "ABSENCE": 2, "ACTIVE_SUMMARY": 3, "ACTIVE_RANGE": 3,
         "ACTIVE_OF_SCHEDULED": 3, "ACTIVE_CHANGE": 4, "LATE": 5, "EARLY_LEAVE": 5, "HIGH_IDLE": 6,
         "OVERTIME": 7, "TOP_APPLICATION": 8}


@dataclass(frozen=True)
class Insight:
    kind: str
    severity: Severity
    title: str
    message: str
    employee_id: str | None = None
    metric: str | None = None
    current_value: float | None = None
    previous_value: float | None = None

    def as_json(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass(frozen=True)
class Comparison:
    metric: str
    label: str
    unit: str  # "h" or "%"
    current: float | None
    previous: float | None

    @property
    def change(self) -> float | None:
        if self.current is None or self.previous is None:
            return None
        return self.current - self.previous

    @property
    def relative(self) -> float | None:
        """Relative change; None when the previous value is 0 (no ∞%)."""
        if self.change is None or not self.previous:
            return None
        return self.change / self.previous

    def as_json(self) -> dict:
        return {"metric": self.metric, "label": self.label, "unit": self.unit, "current": self.current,
                "previous": self.previous, "change": self.change, "relative_change": self.relative}


# ─── the engine ───────────────────────────────────────────────────────────


class Analysis:
    """Everything the Analytics page shows for one selection."""

    def __init__(self, service: DashboardService, sel: Selection, settings: DashboardSettings | None = None) -> None:
        self.service = service
        self.sel = sel
        self.settings = settings or service.settings
        self.now: datetime = service.clock()
        self.people = [r.employee for r in sel.ranges]
        self.labels = employee_labels(self.people)
        self.single = sel.employee_id is not None
        self.previous = previous_selection(sel)
        # one daily_summaries query covers the period, the previous period and the weekly trend
        self.weekly_sel = Selection(sel.kind, tuple(
            EmployeeRange(r.employee, min(r.start, week_bounds(r.end)[0] - timedelta(weeks=WEEKLY_TREND_WEEKS - 1)),
                          r.end) for r in sel.ranges), employee_id=sel.employee_id)
        wide = Selection(sel.kind, tuple(
            EmployeeRange(r.employee, min(p.start, w.start), r.end)
            for r, p, w in zip(sel.ranges, self.previous.ranges, self.weekly_sel.ranges, strict=True)),
            employee_id=sel.employee_id)
        all_rows = service.day_rows(wide)
        self.rows = self._within(all_rows, sel)
        self.prev_rows = self._within(all_rows, self.previous)
        self.week_rows = self._within(all_rows, self.weekly_sel)
        self.figs = figures(self.rows, self.labels)
        self.prev_figs = figures(self.prev_rows, self.labels)
        self.apps = service.application_rows(sel, by_employee=False)
        self.last_calculated = service.repo.summaries_updated_at()

    @staticmethod
    def _within(rows: list[DayRow], sel: Selection) -> list[DayRow]:
        ranges = {r.employee.employee_id: r for r in sel.ranges}
        return [r for r in rows if ranges[r.employee.employee_id].start <= r.summary.local_date
                <= ranges[r.employee.employee_id].end]

    # ── chart data ────────────────────────────────────────────────────────
    def _ordered_figs(self) -> list[EmployeeFigures]:
        figs = [self.figs.get(e.employee_id) or EmployeeFigures(e, self.labels[e.employee_id]) for e in self.people]
        return sorted(figs, key=lambda f: (-f.active, f.label.casefold(), f.employee.employee_id))

    def active_by_employee(self) -> ChartData:
        figs = [f for f in self._ordered_figs() if f.days]
        return ChartData("active_by_employee", "Active Hours by Employee",
                         "Recorded Active Hours per employee over the selected period, highest first.",
                         "h", "bar", (Series("active", "Active Hours"),),
                         [f.label for f in figs], [[hours(f.active)] for f in figs],
                         note=ACTIVE_DISCLAIMER)

    def status_hours(self) -> ChartData:
        if self.single:
            figs = list(self.figs.values())
            cats, vals = (["Selected period"], [[hours(_sum(figs, s.key)) for s in STATUS_SERIES]]) if figs else ([], [])
        else:
            figs = [f for f in self._ordered_figs() if f.days]
            cats = [f.label for f in figs]
            vals = [[hours(getattr(f, s.key)) for s in STATUS_SERIES] for f in figs]
        return ChartData("status_hours", "Active, Idle, Unknown and Locked Hours",
                         "Tracked time split by recorded status. Unknown (monitoring uncertain) and Locked are "
                         "shown separately and never counted as Idle.", "h", "stacked", STATUS_SERIES, cats, vals)

    def daily_trend(self) -> ChartData:
        per_day: dict[date, int] = {}
        for row in self.rows:
            per_day[row.summary.local_date] = per_day.get(row.summary.local_date, 0) + row.summary.active_seconds
        cats: list[str] = []
        vals: list[list[float | None]] = []
        note = "" if self.sel.uniform else ("Dates are each employee's own local reporting date; employees in "
                                            "different time zones were on different dates at the period boundary.")
        if self.sel.ranges:
            start = min(r.start for r in self.sel.ranges)
            last_today = max(local_today(r.employee.timezone, self.now) for r in self.sel.ranges)
            end = min(max(r.end for r in self.sel.ranges), last_today)
            days = [start + timedelta(days=i) for i in range(max(0, (end - start).days + 1))]
            if len(days) > MAX_DAILY_POINTS:  # readable: one bar per ISO week; totals unchanged
                weeks: dict[date, int] = {}
                for d in days:
                    weeks.setdefault(week_bounds(d)[0], 0)
                for d, seconds in per_day.items():
                    weeks[week_bounds(d)[0]] = weeks.get(week_bounds(d)[0], 0) + seconds
                cats = [f"Week of {w:%d %b}" for w in sorted(weeks)]
                vals = [[hours(weeks[w])] for w in sorted(weeks)]
                note = (note + " " if note else "") + "The range is long, so each bar is one ISO week (Monday–Sunday)."
            else:
                cats = [f"{d:%a %d %b}" for d in days]
                vals = [[hours(per_day[d]) if d in per_day else None] for d in days]
        title = "Daily Active Hours" if self.single else "Daily Active Hours (team total)"
        return ChartData("daily_trend", title, "Recorded Active Hours for each local date in the period. "
                         "Dates without a calculated summary are left empty.", "h", "columns",
                         (Series("active", "Active Hours"),), cats, vals, note=note.strip())

    def weekly_trend(self) -> ChartData:
        buckets: dict[date, list[int]] = {}
        for row in self.week_rows:
            b = buckets.setdefault(week_bounds(row.summary.local_date)[0], [0, 0, 0, 0])
            s = row.summary
            b[0] += s.scheduled_seconds
            b[1] += s.active_seconds
            b[2] += s.idle_seconds
            b[3] += s.unknown_seconds
        weeks = sorted(buckets)
        note = ("Weeks run Monday–Sunday in each employee's own time zone; team weeks add up each employee's "
                "local week." if not self.single else "Weeks run Monday–Sunday in the employee's time zone.")
        return ChartData("weekly_trend", "Weekly Work Trend",
                         f"Scheduled, Active, Idle and Unknown Hours per ISO week, for at least the last "
                         f"{WEEKLY_TREND_WEEKS} weeks up to the end of the selected period.", "h", "grouped",
                         WEEKLY_SERIES, [f"Week of {w:%d %b %Y}" for w in weeks],
                         [[hours(v) for v in buckets[w]] for w in weeks], note=note)

    def applications(self) -> ChartData:
        top = self.apps[: self.settings.top_applications]
        return ChartData("applications", f"Application Usage (top {self.settings.top_applications})",
                         "Recorded Active Hours per application, highest first.", "h", "bar",
                         (Series("active", "Active Hours"),), [g["app"] for g in top],
                         [[hours(g["active"])] for g in top], note=APP_DISCLAIMER,
                         empty="No application usage recorded in this period.")

    def attendance(self) -> ChartData:
        if self.single:
            figs = list(self.figs.values())
            cats = ["Selected period"] if figs else []
            vals = [[_sum(figs, a) for a in ("late_days", "early_days", "absent_days", "incomplete_days")]] if figs else []
        else:
            figs = sorted((f for f in self.figs.values()), key=lambda f: (f.label.casefold(), f.employee.employee_id))
            cats = [f.label for f in figs]
            vals = [[f.late_days, f.early_days, f.absent_days, f.incomplete_days] for f in figs]
        return ChartData("attendance", "Attendance: Late, Early Leave, Absent and Data-Incomplete Days",
                         "Number of days with each stored attendance status. Data-incomplete days are shown "
                         "separately and are not counted as absence.", "days", "grouped", ATTENDANCE_SERIES,
                         cats, vals)

    def charts(self, names: Iterable[str] | None = None) -> list[ChartData]:
        builders = {"active_by_employee": self.active_by_employee, "status_hours": self.status_hours,
                    "daily_trend": self.daily_trend, "weekly_trend": self.weekly_trend,
                    "applications": self.applications, "attendance": self.attendance}
        chosen = list(names) if names is not None else list(builders)
        if self.single and "active_by_employee" in chosen and names is None:
            chosen.remove("active_by_employee")  # one bar says nothing; the status chart shows the totals
        return [builders[n]() for n in chosen]

    # ── comparison ────────────────────────────────────────────────────────
    def comparisons(self) -> list[Comparison]:
        cur, prev = list(self.figs.values()), list(self.prev_figs.values())

        def att(figs: list[EmployeeFigures]) -> float | None:
            basis = _sum(figs, "basis")
            return _sum(figs, "credit") / basis * 100 if basis else None

        return [
            Comparison("active_seconds", "Active Hours", "h", hours(_sum(cur, "active")), hours(_sum(prev, "active"))),
            Comparison("idle_seconds", "Idle Hours", "h", hours(_sum(cur, "idle")), hours(_sum(prev, "idle"))),
            Comparison("scheduled_seconds", "Scheduled Hours", "h", hours(_sum(cur, "scheduled")),
                       hours(_sum(prev, "scheduled"))),
            Comparison("overtime_seconds", "Overtime Active Hours", "h", hours(_sum(cur, "overtime")),
                       hours(_sum(prev, "overtime"))),
            Comparison("attendance_percentage", "Attendance %", "%",
                       None if att(cur) is None else round(att(cur), 2),
                       None if att(prev) is None else round(att(prev), 2)),
        ]

    def previous_label(self) -> str:
        return _ranges_label(self.previous)

    def in_progress(self) -> bool:
        return any(r.end >= local_today(r.employee.timezone, self.now) for r in self.sel.ranges)

    # ── insights ──────────────────────────────────────────────────────────
    def _missing_days(self) -> tuple[int, list[str]]:
        """Employee-days up to each employee's local today with no stored summary."""
        have = {(r.employee.employee_id, r.summary.local_date) for r in self.rows}
        missing, without = 0, []
        for rng in self.sel.ranges:
            end = min(rng.end, local_today(rng.employee.timezone, self.now))
            days = [rng.start + timedelta(days=i) for i in range(max(0, (end - rng.start).days + 1))]
            gaps = sum((rng.employee.employee_id, d) not in have for d in days)
            missing += gaps
            if days and gaps == len(days):
                without.append(self.labels[rng.employee.employee_id])
        return missing, without

    def insights(self, limit: int | None = None) -> list[Insight]:
        out: list[Insight] = []
        figs = sorted(self.figs.values(), key=lambda f: (f.label.casefold(), f.employee.employee_id))
        scope = "" if not self.single else f"{self.labels[self.sel.employee_id]} "

        # 1. data quality
        insufficient = _sum(figs, "insufficient_days")
        uncertain = _sum(figs, "uncertain_days")
        if insufficient:
            out.append(Insight("DATA_QUALITY", Severity.DATA_QUALITY, "Monitoring data incomplete",
                               f"Some insights are limited because monitoring data is incomplete for {insufficient} "
                               f"employee-day(s). Those days are not treated as absence."))
        elif uncertain:
            out.append(Insight("DATA_QUALITY", Severity.DATA_QUALITY, "Some start or end times uncertain",
                               f"Monitoring was uncertain at the start or end of {uncertain} employee-day(s) "
                               "(or a device had not synced). Uncertain time is not counted as late or early."))
        missing, without = self._missing_days()
        if without:
            out.append(Insight("MISSING_SUMMARIES", Severity.DATA_QUALITY, "Summaries not calculated yet",
                               "Some employees do not yet have calculated summaries for this period: "
                               + ", ".join(without) + "."))
        elif missing:
            out.append(Insight("MISSING_SUMMARIES", Severity.DATA_QUALITY, "Summaries not calculated yet",
                               f"{missing} employee-day(s) in this period do not have a calculated summary yet."))
        if not figs:
            return self._limit(out, limit)

        # 2. absence (stored ABSENT only — never DATA_INCOMPLETE, UNKNOWN or a missing summary)
        absent = [f for f in figs if f.absent_days]
        if absent:
            names = ", ".join(f"{f.label} ({f.absent_days} day{'s' if f.absent_days != 1 else ''})" for f in absent)
            msg = (f"{scope}had {absent[0].absent_days} absent day(s) in this period." if self.single
                   else f"{len(absent)} employee(s) had at least one absent day: {names}.")
            out.append(Insight("ABSENCE", Severity.NOTICE, "Absent days", msg))

        # 3. active hours
        total_active = _sum(figs, "active")
        total_scheduled = _sum(figs, "scheduled")
        eligible = [f for f in figs if f.eligible]
        if self.single:
            f = figs[0]
            out.append(Insight("ACTIVE_SUMMARY", Severity.INFO, "Recorded Active Hours",
                               f"{f.label} recorded {hm(f.active)} of Active Hours over {f.days} calculated "
                               f"day(s). {ACTIVE_DISCLAIMER}", f.employee.employee_id, "active_seconds", f.active))
        elif eligible:
            avg = _sum(eligible, "active") / len(eligible)
            out.append(Insight("ACTIVE_SUMMARY", Severity.INFO, "Team Active Hours",
                               f"{len(eligible)} employee(s) have comparable calculated attendance data for this "
                               f"period. Total Active Hours: {hm(total_active)}. Average Active Hours per employee: "
                               f"{hm(avg)}.", metric="active_seconds", current_value=total_active))
            if len(eligible) >= 2:
                ranked = sorted(eligible, key=lambda f: (-f.active, f.label.casefold(), f.employee.employee_id))
                hi, lo = ranked[0], ranked[-1]
                excluded = len(figs) - len(eligible)
                extra = (f" {excluded} employee(s) without comparable data yet (for example a shift not finished, "
                         "or incomplete monitoring) are not included in this comparison." if excluded else "")
                out.append(Insight("ACTIVE_RANGE", Severity.INFO, "Highest and lowest recorded Active Hours",
                                   f"Highest recorded Active Hours: {hi.label} — {hm(hi.active)}. Lowest recorded "
                                   f"Active Hours among employees with reportable data: {lo.label} — {hm(lo.active)}."
                                   f"{extra} {ACTIVE_DISCLAIMER}", metric="active_seconds"))
        else:
            out.append(Insight("ACTIVE_SUMMARY", Severity.DATA_QUALITY, "Not enough comparable data",
                               "No employee has enough calculated attendance data in this period to compare "
                               f"Active Hours. Total recorded Active Hours: {hm(total_active)}."))
        if total_scheduled > 0:
            share = total_active / total_scheduled
            tracked = _sum(figs, "tracked")
            caution = ""
            if insufficient or (tracked and _sum(figs, "unknown") / tracked > UNKNOWN_CAUTION):
                caution = (" Monitoring data was incomplete or uncertain on some days, so this percentage should be "
                           "interpreted cautiously.")
            out.append(Insight("ACTIVE_OF_SCHEDULED", Severity.INFO, "Active time as % of scheduled time",
                               f"Recorded Active Hours were {pct(share)} of scheduled time. This is not the "
                               f"Attendance % (which compares active time inside shifts with observable scheduled "
                               f"time).{caution}", metric="active_of_scheduled", current_value=round(share * 100, 2)))

        # 4. comparison with the previous equivalent period
        cmp = self.comparisons()[0]
        prev_label = _ranges_label(self.previous)
        ongoing = " The current period is still in progress." if self.in_progress() else ""
        if not self.prev_rows:
            if self.rows:
                out.append(Insight("ACTIVE_CHANGE", Severity.INFO, "Compared with the previous period",
                                   f"No calculated summaries exist for the previous period ({prev_label}), so no "
                                   "comparison is made."))
        elif cmp.previous == 0 and cmp.current:
            out.append(Insight("ACTIVE_CHANGE", Severity.INFO, "Compared with the previous period",
                               f"Previous period ({prev_label}) had no recorded Active Hours; current period "
                               f"recorded {hm(cmp.current * 3600)}.{ongoing}", metric="active_seconds",
                               current_value=cmp.current * 3600, previous_value=0))
        elif cmp.previous and cmp.change is not None:
            change_s = cmp.change * 3600
            if abs(change_s) < 60:
                text = f"Recorded Active Hours were unchanged compared with the previous period ({prev_label})."
            else:
                direction = "increased" if change_s > 0 else "decreased"
                text = (f"Recorded Active Hours {direction} by {pct(abs(cmp.relative))} ({hm(abs(change_s))}) "
                        f"compared with the previous equivalent period ({prev_label}).")
            out.append(Insight("ACTIVE_CHANGE", Severity.INFO, "Compared with the previous period", text + ongoing,
                               metric="active_seconds", current_value=cmp.current * 3600,
                               previous_value=cmp.previous * 3600))

        # 5. late starts and early leave (stored statuses only)
        for kind, attr, words in (("LATE", "late_days", "late-start"), ("EARLY_LEAVE", "early_days", "early-leave")):
            who = [f for f in figs if getattr(f, attr)]
            if not who:
                continue
            if self.single:
                n = getattr(who[0], attr)
                msg = f"{who[0].label} had {n} {words} day{'s' if n != 1 else ''} during the selected period."
            else:
                detail = ", ".join(f"{f.label} ({getattr(f, attr)} day{'s' if getattr(f, attr) != 1 else ''})"
                                   for f in who)
                msg = f"{len(who)} employee(s) had at least one {words} day: {detail}."
            out.append(Insight(kind, Severity.NOTICE, f"{words.capitalize()} days", msg))

        # 6. high idle share (eligible employees only)
        threshold = self.settings.high_idle_percent / 100
        high = [f for f in eligible if f.tracked > 0 and f.idle / f.tracked >= threshold]
        if high:
            detail = ", ".join(f"{f.label} ({pct(f.idle / f.tracked)})" for f in high)
            subject = (f"Recorded Idle time represented {pct(high[0].idle / high[0].tracked)} of tracked computer "
                       f"time for {high[0].label}" if self.single
                       else f"Recorded Idle time was at least {self.settings.high_idle_percent}% of tracked computer "
                            f"time for {len(high)} employee(s): {detail}")
            out.append(Insight("HIGH_IDLE", Severity.NOTICE, "Idle share of tracked time",
                               f"{subject}. This may include legitimate non-computer work or breaks, and idle time "
                               "alone is not a basis for conclusions about an employee.", metric="idle_share"))

        # 7. overtime
        overtime = _sum(figs, "overtime")
        if overtime:
            lead = (f"{figs[0].label} recorded {hm(overtime)} of overtime Active Hours." if self.single
                    else f"Total recorded overtime activity: {hm(overtime)}.")
            out.append(Insight("OVERTIME", Severity.INFO, "Overtime Active Hours", f"{lead} {OVERTIME_DISCLAIMER}",
                               metric="overtime_seconds", current_value=overtime))

        # 8. applications
        if self.apps and self.apps[0]["active"] > 0:
            top = self.apps[0]
            out.append(Insight("TOP_APPLICATION", Severity.INFO, "Most recorded application time",
                               f"{top['app']} had the most recorded Active Hours among applications during this "
                               f"period ({hm(top['active'])}). {APP_DISCLAIMER}", metric="application_active_seconds",
                               current_value=top["active"]))
        return self._limit(out, limit)

    def _limit(self, insights: list[Insight], limit: int | None) -> list[Insight]:
        ordered = sorted(enumerate(insights), key=lambda pair: (ORDER[pair[1].kind], pair[0]))
        return [i for _, i in ordered][: (limit or self.settings.max_insights)]

    # ── JSON ──────────────────────────────────────────────────────────────
    def as_json(self, limit: int | None = None) -> dict:
        return {
            "version": VERSION,
            "period": self.service.describe(self.sel),
            "previous_period": self.previous_label(),
            "summaries_last_calculated": self.last_calculated.isoformat() if self.last_calculated else None,
            "charts": {c.key: c.as_json() for c in self.charts(names=["active_by_employee", "status_hours",
                                                                      "daily_trend", "weekly_trend",
                                                                      "applications", "attendance"])},
            "comparison": [c.as_json() for c in self.comparisons()],
            "insights": [i.as_json() for i in self.insights(limit)],
            "analysis": "deterministic rule-based analysis (not artificial intelligence)",
        }
