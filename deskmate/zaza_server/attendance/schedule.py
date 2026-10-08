"""Schedule resolution, local → UTC conversion, and date attribution.

Resolution (``resolve``) for an employee and local date D:

1. A one-off rule with ``schedule_date = D`` wins.
2. Otherwise the weekly rule with ``day_of_week = ISO weekday(D)`` and
   ``effective_from <= D <= effective_to`` (open-ended if NULL); if several
   match, the one with the latest ``effective_from`` (the newest rule).
3. Otherwise there is no schedule (``NO_SCHEDULE``).

A shift belongs to the local date it STARTS on, in the rule's time zone:
``start = D start_time``; ``end = D end_time`` if ``end_time > start_time``,
else ``(D + 1) end_time``. Both are converted to UTC instants with the IANA
rules in force on those dates, so the real span can differ from the nominal
one across a DST change. A local time that doesn't exist (spring-forward gap)
is moved forward by the gap; an ambiguous one (fall-back) takes its first
occurrence.

Attribution (``owner_date``) assigns every instant to exactly one date:

1. the date whose shift contains it (if shifts overlap: the earliest start);
2. else the date whose shift start/end is nearest, if within
   ``attribution_margin_seconds`` (before a start or after an end);
3. else its local calendar date in the employee's time zone.

Because it is a function of the instant, the dates partition the timeline:
no moment is ever counted on two days.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import ScheduleRule

UTC = timezone.utc


@dataclass(frozen=True)
class ResolvedDay:
    local_date: date
    rule: ScheduleRule | None
    kind: str  # DATE | WEEKLY | NONE
    timezone: str
    shift_start: float | None = None  # epoch seconds (UTC)
    shift_end: float | None = None
    dst_adjusted: bool = False

    @property
    def is_working_day(self) -> bool:
        return self.rule is not None and self.rule.is_working_day

    @property
    def has_shift(self) -> bool:
        return self.shift_start is not None

    @property
    def span_seconds(self) -> float:
        return (self.shift_end - self.shift_start) if self.has_shift else 0.0

    @property
    def scheduled_seconds(self) -> int:
        """Expected work, never more than the shift really lasts (a DST
        spring-forward night is an hour shorter)."""
        if not self.has_shift:
            return 0
        return int(min(self.rule.expected_work_seconds or 0, round(self.span_seconds)))


def find_rule(rules: Sequence[ScheduleRule], local_date: date) -> tuple[ScheduleRule | None, str]:
    for rule in rules:
        if rule.schedule_date == local_date:
            return rule, "DATE"
    weekday = local_date.isoweekday()
    weekly = [
        r for r in rules
        if r.day_of_week == weekday and r.effective_from is not None and r.effective_from <= local_date
        and (r.effective_to is None or local_date <= r.effective_to)
    ]
    if weekly:
        return max(weekly, key=lambda r: (r.effective_from, r.schedule_id)), "WEEKLY"
    return None, "NONE"


def local_to_utc(day: date, clock: time, tz_name: str) -> tuple[datetime, bool]:
    """UTC instant of a local wall-clock time; flag if DST made it
    nonexistent or ambiguous."""
    zone = ZoneInfo(tz_name)
    local = datetime.combine(day, clock).replace(tzinfo=zone, fold=0)
    utc = local.astimezone(UTC)
    nonexistent = utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None)
    ambiguous = local.replace(fold=1).utcoffset() != local.utcoffset()
    return utc, nonexistent or ambiguous


def local_midnight(day: date, tz_name: str) -> float:
    return local_to_utc(day, time(0), tz_name)[0].timestamp()


def resolve(rules: Sequence[ScheduleRule], local_date: date, employee_tz: str) -> ResolvedDay:
    rule, kind = find_rule(rules, local_date)
    if rule is None:
        return ResolvedDay(local_date, None, kind, employee_tz)
    if not rule.is_working_day:
        return ResolvedDay(local_date, rule, kind, rule.timezone)
    start, adj1 = local_to_utc(local_date, rule.start_time, rule.timezone)
    end_day = local_date if rule.end_time > rule.start_time else local_date + timedelta(days=1)
    end, adj2 = local_to_utc(end_day, rule.end_time, rule.timezone)
    nominal = (datetime.combine(end_day, rule.end_time) - datetime.combine(local_date, rule.start_time))
    real = end - start
    return ResolvedDay(
        local_date, rule, kind, rule.timezone, start.timestamp(), end.timestamp(),
        dst_adjusted=adj1 or adj2 or real != nominal,
    )


class Attribution:
    """Answers "which local date does this instant belong to?" for instants
    near ``center`` (needs the resolved days center-2 .. center+2)."""

    def __init__(self, days: dict[date, ResolvedDay], employee_tz: str, margin: float) -> None:
        self.days = days
        self.employee_tz = employee_tz
        self.margin = margin
        self.shifts = sorted(
            ((d.shift_start, d.shift_end, d.local_date) for d in days.values() if d.has_shift),
            key=lambda s: (s[0], s[2]),
        )
        self._zone = ZoneInfo(employee_tz)

    def owner(self, t: float) -> date:
        containing = [s for s in self.shifts if s[0] <= t < s[1]]
        if containing:
            return containing[0][2]
        near: list[tuple[float, date]] = []
        for start, end, d in self.shifts:
            if end <= t < end + self.margin:
                near.append((t - end, d))
            if start - self.margin <= t < start:
                near.append((start - t, d))
        if near:
            return min(near)[1]
        return datetime.fromtimestamp(t, self._zone).date()

    def window(self, target: date) -> list[tuple[float, float]]:
        """All instants owned by ``target``, as merged intervals."""
        dates = sorted(self.days)
        points = {local_midnight(d, self.employee_tz) for d in dates}
        points.add(local_midnight(dates[-1] + timedelta(days=1), self.employee_tz))
        for start, end, _ in self.shifts:
            points.update((start, end, start - self.margin, end + self.margin))
        for _, end_i, _ in self.shifts:
            for start_j, _, _ in self.shifts:
                if start_j > end_i:
                    points.add((end_i + start_j) / 2)
        day = self.days[target]
        lo = local_midnight(target, self.employee_tz)
        hi = local_midnight(target + timedelta(days=1), self.employee_tz)
        if day.has_shift:
            lo = min(lo, day.shift_start - self.margin)
            hi = max(hi, day.shift_end + self.margin)
        points.update((lo, hi))
        ordered = sorted(p for p in points if lo <= p <= hi)
        out: list[tuple[float, float]] = []
        for a, b in zip(ordered, ordered[1:], strict=False):
            if b > a and self.owner((a + b) / 2) == target:
                if out and out[-1][1] == a:
                    out[-1] = (out[-1][0], b)
                else:
                    out.append((a, b))
        return out
