"""Daily attendance calculation — a pure, deterministic function.

``calculate_daily`` takes everything it needs as arguments (schedules,
periods, sessions, devices, "now", policy) and returns a
:class:`DailySummary`. Same inputs, same output. The formulas are documented
in ARCHITECTURE.md §4.11; in short, for local date D:

- **window(D)**: the instants attributed to D (see ``schedule.py``).
- Activity periods from all the employee's devices are normalized into one
  non-overlapping timeline (``timeline.py``) and clipped to window(D).
- ``active/idle/unknown/locked_seconds`` = time in that status in window(D).
  ``tracked_seconds`` = their sum = time covered by monitoring data. Time
  with no data at all (PC off, agent not running) is *untracked* and is in
  none of them.
- In-shift figures use window(D) ∩ [shift start, shift end).
- Lateness and early leave look only at the shift neighbourhood
  [start − margin, end + margin), so overtime late the previous night can't
  mask a late arrival.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

from . import timeline as tl
from .models import (
    INFORMATIONAL_FLAGS,
    AttendancePolicy,
    AttendanceStatus,
    DailySummary,
    DataQuality,
    DeviceInput,
    PeriodInput,
    QualityFlag,
    ScheduleRule,
    SessionInput,
)
from .schedule import Attribution, ResolvedDay, resolve

UTC = timezone.utc
NEIGHBOUR_DAYS = 2  # resolve D-2 .. D+2 so attribution near D is exact


def _r(seconds: float) -> int:
    return int(math.floor(seconds + 0.5))


def _dt(t: float | None) -> datetime | None:
    return datetime.fromtimestamp(t, UTC) if t is not None else None


def _ts(d: datetime) -> float:
    return d.timestamp()


def resolve_neighbourhood(rules: Sequence[ScheduleRule], local_date: date, employee_tz: str) -> dict[date, ResolvedDay]:
    return {
        local_date + timedelta(days=k): resolve(rules, local_date + timedelta(days=k), employee_tz)
        for k in range(-NEIGHBOUR_DAYS, NEIGHBOUR_DAYS + 1)
    }


def window_bounds(rules: Sequence[ScheduleRule], local_date: date, employee_tz: str,
                  policy: AttendancePolicy) -> tuple[float, float]:
    """Outer bounds of window(D) — what to fetch from storage."""
    days = resolve_neighbourhood(rules, local_date, employee_tz)
    window = Attribution(days, employee_tz, policy.attribution_margin_seconds).window(local_date)
    return window[0][0], window[-1][1]


def _uncertain_intervals(unknown: Sequence[tl.Segment], sessions: Sequence[SessionInput],
                         devices: Sequence[DeviceInput]) -> list[tl.Interval]:
    """Times whose activity can't be judged (used for early leave):

    - UNKNOWN periods;
    - after an INTERRUPTED session (crash, power loss): from its last sign of
      life until that device's next session starts (open-ended if none);
    - after a device's last contact with the server: data for that time may
      not have been uploaded yet (open-ended; never contacted = everything).

    Time with no data after a cleanly CLOSED session is not uncertain: the
    agent was stopped normally (PC shut down, user signed out).
    """
    out: list[tl.Interval] = [(u.start, u.end) for u in unknown]
    for s in sessions:
        if s.status != "INTERRUPTED":
            continue
        crash = _ts(s.effective_end)
        resumed = [_ts(o.started_at) for o in sessions
                   if o is not s and o.device_id == s.device_id and _ts(o.started_at) >= crash]
        out.append((crash, min(resumed, default=math.inf)))
    for d in devices:
        out.append((_ts(d.last_seen_at) if d.last_seen_at is not None else -math.inf, math.inf))
    return out


def calculate_daily(
    *,
    employee_id: str,
    employee_tz: str,
    local_date: date,
    rules: Sequence[ScheduleRule],
    periods: Sequence[PeriodInput],
    sessions: Sequence[SessionInput],
    devices: Sequence[DeviceInput],
    now: datetime,
    policy: AttendancePolicy,
) -> DailySummary:
    margin = float(policy.attribution_margin_seconds)
    days = resolve_neighbourhood(rules, local_date, employee_tz)
    day = days[local_date]
    attribution = Attribution(days, employee_tz, margin)
    window = attribution.window(local_date)
    now_t = _ts(now)
    flags: set[QualityFlag] = set()

    # ── normalized activity inside the window ───────────────────────────
    norm = tl.normalize((_ts(p.started_at), _ts(p.ended_at), p.status) for p in periods)
    if norm.overlap_seconds > 1.0:
        flags.add(QualityFlag.OVERLAPPING_PERIODS)
    segs = tl.clip(norm.segments, window)
    active_s = tl.seconds(segs, "ACTIVE")
    idle_s = tl.seconds(segs, "IDLE")
    unknown_s = tl.seconds(segs, "UNKNOWN")
    locked_s = tl.seconds(segs, "LOCKED")
    if unknown_s > 0:
        flags.add(QualityFlag.UNKNOWN_TIME)
    active_segs = [s for s in segs if s.status == "ACTIVE"]
    first_active = active_segs[0].start if active_segs else None
    last_active = active_segs[-1].end if active_segs else None
    first_tracked = segs[0].start if segs else None
    last_tracked = segs[-1].end if segs else None

    # ── devices, sessions ───────────────────────────────────────────────
    window_lo, window_hi = window[0][0], window[-1][1]
    in_window = [p for p in periods if _ts(p.ended_at) > window_lo and _ts(p.started_at) < window_hi]
    used_devices = {p.device_id for p in in_window}
    enabled = [d for d in devices if d.status == "ACTIVE"]
    relevant = [d for d in devices if d.device_id in used_devices] or enabled
    if not relevant:
        flags.add(QualityFlag.NO_DEVICE)
    win_sessions = [s for s in sessions if _ts(s.effective_end) >= window_lo and _ts(s.started_at) < window_hi]
    if any(s.status == "INTERRUPTED" for s in win_sessions):
        flags.add(QualityFlag.INTERRUPTED_SESSION)
    provisional = now_t < window_hi or any(s.status == "OPEN" for s in win_sessions) or any(p.is_open for p in in_window)

    # A device that hasn't contacted the server since `point` may still hold
    # data for the time before it: don't judge that time yet.
    def awaiting_sync(point: float) -> bool:
        if now_t < point:
            return False
        return any(d.last_seen_at is None or _ts(d.last_seen_at) < point for d in relevant)

    # ── shift figures ───────────────────────────────────────────────────
    shift_iv = [(day.shift_start, day.shift_end)] if day.has_shift else []
    shift_window = tl.intersect(window, shift_iv)
    shift_segs = tl.clip(segs, shift_window)
    active_in_shift = tl.seconds(shift_segs, "ACTIVE")
    unknown_in_shift = tl.seconds(shift_segs, "UNKNOWN")
    tracked_in_shift = tl.seconds(shift_segs)
    pre_active = post_active = 0.0
    if day.has_shift:
        pre_active = tl.seconds(tl.clip(active_segs, [(window_lo, day.shift_start)]))
        post_active = tl.seconds(tl.clip(active_segs, [(day.shift_end, window_hi)]))
        others = [d for k, d in days.items() if k != local_date and d.has_shift]
        if any(o.shift_start < day.shift_end and day.shift_start < o.shift_end for o in others):
            flags.add(QualityFlag.SCHEDULE_OVERLAP)
        if day.dst_adjusted:
            flags.add(QualityFlag.DST_ADJUSTED)

    # Detected break / idle: runs of IDLE or LOCKED (no ACTIVE or UNKNOWN in
    # between) inside the shift, at least break_min_seconds long.
    detected_break = 0.0
    run: list[float] | None = None  # [start, end] of the current IDLE/LOCKED run
    for seg in shift_segs + [tl.Segment(math.inf, math.inf, "END")]:
        if seg.status in ("IDLE", "LOCKED") and run is not None and seg.start == run[1]:
            run[1] = seg.end
            continue
        if run is not None and run[1] - run[0] >= policy.break_min_seconds:
            detected_break += run[1] - run[0]
        run = [seg.start, seg.end] if seg.status in ("IDLE", "LOCKED") else None

    # ── punctuality (working days with a shift) ─────────────────────────
    late = early = 0.0
    if day.has_shift:
        S, E = day.shift_start, day.shift_end
        neighbourhood = tl.intersect(window, [(S - margin, E + margin)])
        near_active = tl.clip(active_segs, neighbourhood)
        near_unknown = [s for s in tl.clip(segs, neighbourhood) if s.status == "UNKNOWN"]
        if near_active:
            f, last = near_active[0].start, near_active[-1].end
            if f - S > policy.late_grace_seconds:
                # UNKNOWN time before the first activity goes to the
                # employee's benefit: lateness counts only from its end.
                unknowns = [u for u in near_unknown if u.end > S and u.start < f]
                base = max([S] + [min(u.end, f) for u in unknowns])
                late = max(0.0, f - base)
                if unknowns:
                    flags.add(QualityFlag.START_UNCERTAIN)
            if now_t >= E and last > S and E - last > 0:
                # Early leave = the part of [last activity, shift end) that
                # was reliably observed as not working. Only the uncertain
                # parts are excluded, so a later crash or an unsynced tail
                # doesn't erase earlier reliable IDLE/LOCKED/no-data time.
                tail = [(last, E)]
                uncertain = tl.intersect(_uncertain_intervals(near_unknown, win_sessions, relevant), tail)
                reliable = (E - last) - tl.total(uncertain)
                if uncertain:
                    flags.add(QualityFlag.END_UNCERTAIN)
                if reliable > policy.early_leave_grace_seconds:
                    early = reliable

    # ── status ──────────────────────────────────────────────────────────
    if day.rule is None:
        status = AttendanceStatus.NO_SCHEDULE
        flags.add(QualityFlag.NO_SCHEDULE)
    elif not day.is_working_day:
        status = AttendanceStatus.WORKED_DAY_OFF if active_s > 0 else AttendanceStatus.DAY_OFF
    elif active_in_shift > 0:
        if now_t < day.shift_end:
            early = 0.0
        late_r, early_r = _r(late) > 0, _r(early) > 0
        status = (AttendanceStatus.LATE_AND_EARLY if late_r and early_r else AttendanceStatus.LATE if late_r
                  else AttendanceStatus.EARLY_LEAVE if early_r else AttendanceStatus.PRESENT)
    else:
        late = early = 0.0  # absence is not lateness
        unknown_ratio = unknown_in_shift / day.span_seconds if day.span_seconds else 0.0
        if now_t < day.shift_end:
            status = AttendanceStatus.PENDING
        elif (unknown_ratio >= policy.incomplete_unknown_ratio or not relevant
              or awaiting_sync(day.shift_end)):
            status = AttendanceStatus.DATA_INCOMPLETE
        else:
            status = AttendanceStatus.ABSENT
    if awaiting_sync(day.shift_end if day.has_shift else window_hi):
        flags.add(QualityFlag.AWAITING_DEVICE_SYNC)
    if provisional:
        flags.add(QualityFlag.PROVISIONAL)

    # ── overtime ────────────────────────────────────────────────────────
    if day.rule is None:
        overtime = 0.0
    elif not day.is_working_day:
        overtime = active_s  # all ACTIVE work on a day off
    else:
        sides = ([pre_active] if policy.count_pre_shift_overtime else []) + [post_active]
        overtime = sum(x for x in sides if x >= max(policy.overtime_min_seconds, 1e-9))

    # ── rounding (whole seconds; tracked is the sum of its parts) ───────
    active_i, idle_i, unknown_i, locked_i = _r(active_s), _r(idle_s), _r(unknown_s), _r(locked_s)
    scheduled = day.scheduled_seconds if day.is_working_day else 0
    unknown_in_shift_i = min(_r(unknown_in_shift), _r(day.span_seconds))
    measurable = max(0, scheduled - unknown_in_shift_i)
    active_in_shift_i = _r(active_in_shift)
    counted = day.is_working_day and status not in (AttendanceStatus.PENDING, AttendanceStatus.DATA_INCOMPLETE)
    basis = measurable if counted else 0
    credit = min(active_in_shift_i, basis)
    percentage = round(credit / basis * 100, 2) if basis > 0 else None

    # ── data quality ────────────────────────────────────────────────────
    quality_flags = sorted(f.value for f in flags)
    if status is AttendanceStatus.DATA_INCOMPLETE or (
        day.span_seconds and unknown_in_shift / day.span_seconds >= policy.incomplete_unknown_ratio
    ):
        quality = DataQuality.INSUFFICIENT
    elif flags - INFORMATIONAL_FLAGS:
        quality = DataQuality.PARTIAL
    else:
        quality = DataQuality.COMPLETE

    return DailySummary(
        employee_id=employee_id,
        local_date=local_date,
        timezone=day.timezone,
        schedule_id=day.rule.schedule_id if day.rule else None,
        schedule_kind=day.kind,
        is_working_day=day.is_working_day,
        scheduled_start=_dt(day.shift_start),
        scheduled_end=_dt(day.shift_end),
        shift_span_seconds=_r(day.span_seconds),
        scheduled_seconds=scheduled,
        measurable_scheduled_seconds=measurable,
        window_start=_dt(window_lo),
        window_end=_dt(window_hi),
        tracked_seconds=active_i + idle_i + unknown_i + locked_i,
        active_seconds=active_i,
        idle_seconds=idle_i,
        unknown_seconds=unknown_i,
        locked_seconds=locked_i,
        active_in_shift_seconds=active_in_shift_i,
        tracked_in_shift_seconds=_r(tracked_in_shift),
        unknown_in_shift_seconds=unknown_in_shift_i,
        pre_shift_active_seconds=_r(pre_active),
        post_shift_active_seconds=_r(post_active),
        detected_break_seconds=_r(detected_break),
        first_activity_at=_dt(first_active),
        last_activity_at=_dt(last_active),
        first_tracked_at=_dt(first_tracked),
        last_tracked_at=_dt(last_tracked),
        late_seconds=_r(late),
        early_leave_seconds=_r(early),
        overtime_seconds=_r(overtime),
        attendance_status=status,
        attendance_credit_seconds=credit,
        attendance_basis_seconds=basis,
        attendance_percentage=percentage,
        worked_day=active_i > 0,
        data_quality=quality,
        quality_flags=tuple(quality_flags),
        is_provisional=provisional,
        session_count=len({s.session_id for s in win_sessions}),
        device_count=len(used_devices),
        policy=policy.as_dict(),
    )
