"""Dashboard business logic: period filters, KPIs, current status, pages,
schedule management. No SQL (that's ``queries.py``) and no HTML.

Sources (ARCHITECTURE.md §6):

- Historical figures: the stored Phase 5 ``daily_summaries`` — summed, never
  recalculated. Attendance % = Σ credit ÷ Σ basis × 100, never an average of
  daily percentages.
- Current status: ``devices.last_seen_at`` and each device's latest
  ``activity_periods`` row. Never used for historical figures.
- Application usage: ``application_usage_daily`` — supplementary activity
  information keyed by the device's calendar date, not attendance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..attendance.models import AttendanceStatus, DailySummary, ScheduleRule
from ..attendance.rollup import month_bounds, week_bounds
from ..attendance.schedule import local_midnight, resolve
from .config import DashboardSettings
from .models import (
    STATUS_PRIORITY,
    CurrentStatus,
    DayRow,
    Employee,
    EmployeeRange,
    EmployeeStatus,
    Period,
    Selection,
    Totals,
)
from .queries import DashboardRepository

UTC = timezone.utc
MAX_CUSTOM_DAYS = 366

PERIODS = {
    "today": "Today", "yesterday": "Yesterday", "this_week": "This Week", "last_week": "Last Week",
    "this_month": "This Month", "last_month": "Last Month", "custom": "Custom Date Range",
}


class NotFound(LookupError):
    pass


def local_today(tz: str, now: datetime) -> date:
    return now.astimezone(ZoneInfo(tz)).date()


def period_range(kind: str, today: date, custom_from: date | None = None,
                 custom_to: date | None = None) -> tuple[date, date]:
    """The local calendar dates a period covers, given one employee's local
    today. Weeks are ISO Monday-Sunday."""
    if kind == "today":
        return today, today
    if kind == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if kind == "this_week":
        return week_bounds(today)
    if kind == "last_week":
        return week_bounds(today - timedelta(days=7))
    if kind == "this_month":
        return month_bounds(today)
    if kind == "last_month":
        return month_bounds(month_bounds(today)[0] - timedelta(days=1))
    if kind == "custom":
        if custom_from is None or custom_to is None:
            raise ValueError("a custom range needs both From and To dates")
        if custom_to < custom_from:
            raise ValueError("'To' must not be before 'From'")
        if (custom_to - custom_from).days >= MAX_CUSTOM_DAYS:
            raise ValueError(f"a custom range can cover at most {MAX_CUSTOM_DAYS} days")
        return custom_from, custom_to
    raise ValueError("unknown period")


def range_label(start: date, end: date) -> str:
    if start == end:
        return f"{start:%a %d %b %Y}"
    return f"{start:%d %b %Y} – {end:%d %b %Y}"


def add_summary(totals: Totals, s: DailySummary) -> None:
    totals.scheduled += s.scheduled_seconds
    totals.tracked += s.tracked_seconds
    totals.active += s.active_seconds
    totals.idle += s.idle_seconds
    totals.unknown += s.unknown_seconds
    totals.locked += s.locked_seconds
    totals.overtime += s.overtime_seconds
    totals.detected_break += s.detected_break_seconds
    totals.late_seconds += s.late_seconds
    totals.early_leave_seconds += s.early_leave_seconds
    totals.credit += s.attendance_credit_seconds
    totals.basis += s.attendance_basis_seconds
    totals.days += 1
    totals.days_worked += int(s.worked_day)
    st = s.attendance_status
    totals.late_days += st in (AttendanceStatus.LATE, AttendanceStatus.LATE_AND_EARLY)
    totals.early_leave_days += st in (AttendanceStatus.EARLY_LEAVE, AttendanceStatus.LATE_AND_EARLY)
    totals.absent_days += st is AttendanceStatus.ABSENT
    totals.incomplete_days += st is AttendanceStatus.DATA_INCOMPLETE


def team_totals(rows: list[DayRow]) -> Totals:
    """Sum the stored daily rows. An employee counts as late / absent /
    data-incomplete if at least one of their days in the period was."""
    totals = Totals()
    flags: dict[str, set[str]] = {}
    for row in rows:
        add_summary(totals, row.summary)
        st = row.summary.attendance_status
        name = row.employee.display_name
        if st in (AttendanceStatus.LATE, AttendanceStatus.LATE_AND_EARLY):
            flags.setdefault("late", set()).add(name)
        if st is AttendanceStatus.ABSENT:
            flags.setdefault("absent", set()).add(name)
        if st is AttendanceStatus.DATA_INCOMPLETE:
            flags.setdefault("incomplete", set()).add(name)
    totals.late_employees = sorted(flags.get("late", ()), key=str.casefold)
    totals.absent_employees = sorted(flags.get("absent", ()), key=str.casefold)
    totals.incomplete_employees = sorted(flags.get("incomplete", ()), key=str.casefold)
    return totals


def employee_status(enabled: list, latest: dict[str, Period], now: datetime, threshold: int,  # noqa: ANN001
                    employee_id: str) -> EmployeeStatus:
    """The current-status rule (ARCHITECTURE.md §6.3):

    - Only ENABLED devices count. None → NO_DEVICE.
    - A device is online if it contacted the server within ``threshold``
      seconds (``devices.last_seen_at``); otherwise it is OFFLINE.
    - An online device's state is the status of its latest activity period
      if that period ended within the threshold (ACTIVE / IDLE / LOCKED /
      UNKNOWN, as recorded); otherwise ONLINE_NO_ACTIVITY.
    - Several devices: the highest by ACTIVE > IDLE > LOCKED > UNKNOWN >
      ONLINE_NO_ACTIVITY > OFFLINE.

    UNKNOWN stays UNKNOWN (never idle); online is never called working.
    """
    if not enabled:
        return EmployeeStatus(employee_id, CurrentStatus.NO_DEVICE, None, None, 0, 0)
    cutoff = now - timedelta(seconds=threshold)
    best, online, last_end = CurrentStatus.OFFLINE, 0, None
    for d in enabled:
        if d.last_seen_at is None or d.last_seen_at < cutoff:
            state = CurrentStatus.OFFLINE
        else:
            online += 1
            p = latest.get(d.device_id)
            if p is not None and p.ended_at >= cutoff:
                state = CurrentStatus(p.status)
                last_end = max(last_end or p.ended_at, p.ended_at)
            else:
                state = CurrentStatus.ONLINE_NO_ACTIVITY
        if STATUS_PRIORITY[state] > STATUS_PRIORITY[best]:
            best = state
    seen = [d.last_seen_at for d in enabled if d.last_seen_at is not None]
    return EmployeeStatus(employee_id, best, max(seen) if seen else None, last_end, online, len(enabled))


@dataclass(frozen=True)
class TodaySchedule:
    text: str
    start: datetime | None = None  # local
    end: datetime | None = None    # local


def schedule_text(start_utc: datetime | None, end_utc: datetime | None, tz: str, scheduled_seconds: int,
                  is_working_day: bool, has_rule: bool) -> TodaySchedule:
    if not has_rule:
        return TodaySchedule("No schedule")
    if not is_working_day or start_utc is None or end_utc is None:
        return TodaySchedule("Day off")
    zone = ZoneInfo(tz)
    s, e = start_utc.astimezone(zone), end_utc.astimezone(zone)
    plus = f" (+{(e.date() - s.date()).days} day)" if e.date() != s.date() else ""
    return TodaySchedule(f"{s:%H:%M} – {e:%H:%M}{plus}, {scheduled_seconds / 3600:g} h expected", s, e)


class DashboardService:
    def __init__(self, repo: DashboardRepository, settings: DashboardSettings | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.repo = repo
        self.settings = settings or DashboardSettings()
        self.clock = clock

    # ── employees and the period filter ───────────────────────────────────
    def employees(self, *, include_inactive: bool = True) -> list[Employee]:
        rows = [e for e in self.repo.employees() if include_inactive or e.is_active]
        return sorted(rows, key=lambda e: (e.display_name.casefold(), e.employee_id))

    def employee(self, employee_id: str) -> Employee:
        found = next((e for e in self.repo.employees() if e.employee_id == employee_id), None)
        if found is None:
            raise NotFound("employee not found")
        return found

    def selection(self, kind: str = "today", employee_id: str | None = None, custom_from: date | None = None,
                  custom_to: date | None = None) -> Selection:
        """Resolve the filter to each employee's OWN local date range."""
        if kind not in PERIODS:
            raise ValueError("unknown period")
        people = [self.employee(employee_id)] if employee_id else self.employees(include_inactive=False)
        now = self.clock()
        ranges = tuple(EmployeeRange(e, *period_range(kind, local_today(e.timezone, now), custom_from, custom_to))
                       for e in people)
        return Selection(kind, ranges, custom_from, custom_to, employee_id)

    def describe(self, sel: Selection) -> dict:
        """What the UI says about the period: one range, or an explicit note
        that employees' local periods differ (listing each)."""
        if not sel.ranges:
            return {"uniform": True, "label": PERIODS[sel.kind], "ranges": []}
        if sel.uniform:
            r = sel.ranges[0]
            return {"uniform": True, "label": f"{PERIODS[sel.kind]}: {range_label(r.start, r.end)}", "ranges": []}
        return {"uniform": False,
                "label": f"{PERIODS[sel.kind]}: local periods differ between employees",
                "ranges": [(r.employee.display_name, r.employee.timezone, range_label(r.start, r.end))
                           for r in sel.ranges]}

    def day_rows(self, sel: Selection) -> list[DayRow]:
        """Stored daily summaries inside each employee's own range."""
        if not sel.ranges:
            return []
        lo = min(r.start for r in sel.ranges)
        hi = max(r.end for r in sel.ranges)
        by_id = {r.employee.employee_id: r for r in sel.ranges}
        rows = []
        for s in self.repo.daily(list(by_id), lo, hi):
            r = by_id[s.employee_id]
            if r.start <= s.local_date <= r.end:
                rows.append(DayRow(r.employee, s))
        return rows

    # ── current status ────────────────────────────────────────────────────
    def current_status(self) -> dict[str, EmployeeStatus]:
        now = self.clock()
        threshold = self.settings.online_threshold_seconds
        latest = {p.device_id: p for p in self.repo.latest_periods(now - timedelta(seconds=threshold))}
        enabled: dict[str, list] = {}
        for d in self.repo.devices():
            if d.status == "ACTIVE":
                enabled.setdefault(d.employee_id, []).append(d)
        return {e.employee_id: employee_status(enabled.get(e.employee_id, []), latest, now, threshold, e.employee_id)
                for e in self.repo.employees()}

    @staticmethod
    def status_counts(statuses: list[EmployeeStatus]) -> dict[str, int]:
        counts = {s.value: 0 for s in CurrentStatus}
        for st in statuses:
            counts[st.status.value] += 1
        counts["OFFLINE_TOTAL"] = counts["OFFLINE"] + counts["NO_DEVICE"]
        return counts

    def _today(self, employees: list[Employee]) -> dict[str, tuple[date, DailySummary | None, TodaySchedule]]:
        """Each employee's local today, its stored summary (if calculated)
        and the schedule of the shift that STARTS today (an overnight shift
        belongs to its start date)."""
        if not employees:
            return {}
        now = self.clock()
        todays = {e.employee_id: local_today(e.timezone, now) for e in employees}
        stored = {(s.employee_id, s.local_date): s
                  for s in self.repo.daily(list(todays), min(todays.values()), max(todays.values()))}
        rules: dict[str, list[ScheduleRule]] = {}
        for r in self.repo.schedules(list(todays)):
            rules.setdefault(r.employee_id, []).append(r)
        out = {}
        for e in employees:
            d = todays[e.employee_id]
            s = stored.get((e.employee_id, d))
            if s is not None:
                sched = schedule_text(s.scheduled_start, s.scheduled_end, e.timezone, s.scheduled_seconds,
                                      s.is_working_day, s.schedule_kind != "NONE")
            else:  # not calculated yet: show the rule (Phase 5 resolution), no figures
                day = resolve(rules.get(e.employee_id, []), d, e.timezone)
                sched = schedule_text(
                    datetime.fromtimestamp(day.shift_start, UTC) if day.has_shift else None,
                    datetime.fromtimestamp(day.shift_end, UTC) if day.has_shift else None,
                    e.timezone, day.scheduled_seconds, day.is_working_day, day.rule is not None)
            out[e.employee_id] = (d, s, sched)
        return out

    # ── pages ─────────────────────────────────────────────────────────────
    def overview(self, sel: Selection) -> dict:
        people = [r.employee for r in sel.ranges]
        statuses = self.current_status()
        today = self._today(people)
        rows = []
        for e in people:
            d, s, sched = today[e.employee_id]
            rows.append({"employee": e, "status": statuses[e.employee_id], "today": d, "summary": s,
                         "schedule": sched})
        return {
            "selection": sel, "period": self.describe(sel), "totals": team_totals(self.day_rows(sel)),
            "active_employees": len(people),
            "status_counts": self.status_counts([statuses[e.employee_id] for e in people]),
            "rows": rows, "summaries_updated_at": self.repo.summaries_updated_at(), "now": self.clock(),
        }

    def employee_detail(self, employee_id: str, sel: Selection) -> dict:
        e = self.employee(employee_id)
        rng = sel.ranges[0]
        day_rows = self.day_rows(sel)
        d, s, sched = self._today([e])[e.employee_id]
        start = datetime.fromtimestamp(local_midnight(rng.start, e.timezone), UTC)
        end = datetime.fromtimestamp(local_midnight(rng.end + timedelta(days=1), e.timezone), UTC)
        limit = self.settings.activity_rows
        activity = self.repo.activity(e.employee_id, start, end, limit + 1)
        return {
            "employee": e, "status": self.current_status()[e.employee_id], "today": d, "today_summary": s,
            "schedule": sched, "selection": sel, "period": self.describe(sel), "totals": team_totals(day_rows),
            "days": sorted(day_rows, key=lambda r: r.summary.local_date, reverse=True),
            "apps": self.application_rows(sel, by_employee=False),
            "activity": activity[:limit], "activity_truncated": len(activity) > limit, "activity_limit": limit,
        }

    ATTENDANCE_SORTS = {
        "employee": lambda r: (r.employee.display_name.casefold(), r.employee.employee_id),
        "date": lambda r: r.summary.local_date,
        "scheduled": lambda r: r.summary.scheduled_seconds,
        "tracked": lambda r: r.summary.tracked_seconds,
        "active": lambda r: r.summary.active_seconds,
        "idle": lambda r: r.summary.idle_seconds,
        "unknown": lambda r: r.summary.unknown_seconds,
        "locked": lambda r: r.summary.locked_seconds,
        "late": lambda r: r.summary.late_seconds,
        "early": lambda r: r.summary.early_leave_seconds,
        "overtime": lambda r: r.summary.overtime_seconds,
        "status": lambda r: r.summary.attendance_status.value,
        "attendance": lambda r: (r.summary.attendance_percentage is not None, r.summary.attendance_percentage or 0),
        "quality": lambda r: r.summary.data_quality.value,
    }

    def attendance(self, sel: Selection, sort: str = "date", direction: str = "desc") -> dict:
        if sort not in self.ATTENDANCE_SORTS:
            sort = "date"
        descending = direction != "asc"
        rows = self.day_rows(sel)
        # deterministic: tie-break by date then employee, then the chosen key
        rows.sort(key=lambda r: (r.summary.local_date, r.employee.display_name.casefold(), r.employee.employee_id))
        rows.sort(key=self.ATTENDANCE_SORTS[sort], reverse=descending)
        return {"selection": sel, "period": self.describe(sel), "rows": rows, "sort": sort,
                "direction": "desc" if descending else "asc", "totals": team_totals(rows)}

    def application_rows(self, sel: Selection, *, by_employee: bool) -> list[dict]:
        """Application usage from ``application_usage_daily`` (device-local
        dates) inside each employee's local range. Supplementary activity
        information — not attendance, not a productivity score."""
        if not sel.ranges:
            return []
        by_id = {r.employee.employee_id: r for r in sel.ranges}
        lo = min(r.start for r in sel.ranges)
        hi = max(r.end for r in sel.ranges)
        groups: dict[tuple, dict] = {}
        for u in self.repo.app_usage(list(by_id), lo, hi):
            r = by_id[u.employee_id]
            if not r.start <= u.usage_date <= r.end:
                continue
            key = (u.app_name, u.employee_id) if by_employee else (u.app_name,)
            g = groups.setdefault(key, {"app": u.app_name, "employee": r.employee if by_employee else None,
                                        "active": 0.0, "idle": 0.0, "unknown": 0.0, "days": set(),
                                        "employees": set()})
            g["active"] += u.active_seconds
            g["idle"] += u.idle_seconds
            g["unknown"] += u.unknown_seconds
            g["days"].add(u.usage_date)  # distinct calendar dates with usage
            g["employees"].add(u.employee_id)
        rows = [{**g, "usage_days": len(g["days"]), "employee_count": len(g["employees"])} for g in groups.values()]
        rows.sort(key=lambda g: (-g["active"], g["app"].casefold(),
                                 g["employee"].display_name.casefold() if g["employee"] else ""))
        return rows

    def applications(self, sel: Selection, group: str = "app") -> dict:
        by_employee = group == "employee" or sel.employee_id is not None
        return {"selection": sel, "period": self.describe(sel), "group": "employee" if by_employee else "app",
                "rows": self.application_rows(sel, by_employee=by_employee)}

    # ── schedules ─────────────────────────────────────────────────────────
    def rule_state(self, rule: ScheduleRule, today: date) -> str:
        """past (read-only here), running (change/end from a date >= today)
        or future (edit/remove freely)."""
        if rule.schedule_date is not None:
            return "past" if rule.schedule_date < today else "future"
        if rule.effective_to is not None and rule.effective_to < today:
            return "past"
        return "future" if rule.effective_from >= today else "running"

    def schedules(self, employee_id: str | None = None) -> dict:
        people = [self.employee(employee_id)] if employee_id else self.employees()
        now = self.clock()
        rules: dict[str, list[ScheduleRule]] = {}
        for r in self.repo.schedules([e.employee_id for e in people]):
            rules.setdefault(r.employee_id, []).append(r)
        out = []
        for e in people:
            today = local_today(e.timezone, now)
            items = rules.get(e.employee_id, [])
            weekly = sorted((r for r in items if r.day_of_week is not None),
                            key=lambda r: (r.day_of_week, r.effective_from))
            dated = sorted((r for r in items if r.schedule_date is not None), key=lambda r: r.schedule_date)
            out.append({"employee": e, "today": today,
                        "weekly": [(r, self.rule_state(r, today)) for r in weekly],
                        "dated": [(r, self.rule_state(r, today)) for r in dated]})
        return {"employees": out, "selected": employee_id}

    def _employee_today(self, employee_id: str) -> tuple[Employee, date]:
        e = self.employee(employee_id)
        return e, local_today(e.timezone, self.clock())

    def add_weekly(self, actor: tuple[str, str], employee_id: str, weekdays: list[int], *, effective_from: date,
                   effective_to: date | None, hours: tuple | None) -> list[str]:
        """``hours`` = (start, end, expected seconds), or None for a day off.
        History rule: a new weekly rule starts today or later (employee's
        local date), so already-calculated days don't change."""
        _, today = self._employee_today(employee_id)
        if effective_from < today:
            raise ValueError(f"'effective from' must be today ({today}) or later; past schedules are "
                             "corrected by an administrator with the CLI")
        if not weekdays:
            raise ValueError("choose at least one weekday")
        rules = [{"day_of_week": d, "effective_from": effective_from, "effective_to": effective_to,
                  **self._hours(hours)} for d in sorted(set(weekdays))]
        return self.repo.add_schedules(employee_id, rules, actor=actor)

    def add_date(self, actor: tuple[str, str], employee_id: str, day: date, hours: tuple | None) -> list[str]:
        _, today = self._employee_today(employee_id)
        if day < today:
            raise ValueError(f"the date must be today ({today}) or later")
        return self.repo.add_schedules(employee_id, [{"schedule_date": day, **self._hours(hours)}], actor=actor)

    @staticmethod
    def _hours(hours: tuple | None) -> dict:
        if hours is None:
            return {"is_working_day": False, "start_time": None, "end_time": None, "expected_work_seconds": None}
        start, end, expected = hours
        return {"is_working_day": True, "start_time": start, "end_time": end, "expected_work_seconds": expected}

    def _rule(self, schedule_id: str) -> tuple[ScheduleRule, date, str]:
        rule = self.repo.get_schedule(schedule_id)
        if rule is None:
            raise NotFound("schedule not found")
        _, today = self._employee_today(rule.employee_id)
        return rule, today, self.rule_state(rule, today)

    def update_rule(self, actor: tuple[str, str], schedule_id: str, *, hours: tuple | None,
                    from_date: date | None = None, schedule_date: date | None = None,
                    effective_to: date | None = None) -> str:
        """Change hours. A future rule changes in place; a weekly rule that is
        already running changes from ``from_date`` (today or later): the old
        rule ends the day before and a new rule starts. Past rules can't be
        changed here."""
        rule, today, state = self._rule(schedule_id)
        changes = self._hours(hours)
        if state == "past":
            raise ValueError("this rule only covers past dates; it can't be changed from the dashboard")
        if rule.schedule_date is not None:
            new_date = schedule_date or rule.schedule_date
            if new_date < today:
                raise ValueError(f"the date must be today ({today}) or later")
            self.repo.update_schedule(schedule_id, {**changes, "schedule_date": new_date}, actor=actor)
            return schedule_id
        if state == "future":
            start = from_date or rule.effective_from
            if start < today:
                raise ValueError(f"'effective from' must be today ({today}) or later")
            self.repo.update_schedule(schedule_id, {**changes, "effective_from": start,
                                                    "effective_to": effective_to}, actor=actor)
            return schedule_id
        start = from_date or today
        if start < today:
            raise ValueError(f"changes take effect from today ({today}) or later")
        return self.repo.split_schedule(schedule_id, start, changes, actor=actor)

    def remove_rule(self, actor: tuple[str, str], schedule_id: str, from_date: date | None = None) -> None:
        """A future rule is deleted. A running weekly rule ends the day
        before ``from_date`` (today or later). Past rules stay."""
        rule, today, state = self._rule(schedule_id)
        if state == "past":
            raise ValueError("this rule only covers past dates; it can't be removed from the dashboard")
        if state == "future":
            self.repo.delete_schedule(schedule_id, actor=actor)
            return
        end_from = from_date or today
        if end_from < today:
            raise ValueError(f"a running rule can be ended from today ({today}) or later")
        if rule.effective_to is not None and end_from > rule.effective_to:
            raise ValueError(f"this rule already ends on {rule.effective_to}")
        self.repo.update_schedule(schedule_id, {"effective_to": end_from - timedelta(days=1)}, actor=actor)


def parse_hours(start: str, end: str, expected_hours: str) -> tuple[time, time, int]:
    """Form input → (start, end, expected seconds). Shape only; the schedule
    rules themselves are validated by the database (one validator)."""
    try:
        s, e = time.fromisoformat(start.strip()), time.fromisoformat(end.strip())
    except ValueError:
        raise ValueError("start and end must be times like 09:00") from None
    try:
        expected = float(expected_hours)
    except ValueError:
        raise ValueError("expected hours must be a number such as 8 or 7.5") from None
    if not 0 < expected <= 24:
        raise ValueError("expected hours must be above 0 and at most 24")
    return s.replace(second=0, microsecond=0), e.replace(second=0, microsecond=0), round(expected * 3600)
