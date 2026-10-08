"""Phase 5 attendance calculations — deterministic, no database.

The pure engine (``calculator.py``) is driven through ``SummaryService``
with an in-memory store, so these tests cover schedule resolution, window
attribution, every formula, statuses, data quality, idempotency and the
weekly/monthly roll-ups. PostgreSQL storage of the same results is covered
in test_attendance_postgres.py (opt-in).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from deskmate.zaza_server.attendance.models import (
    AttendancePolicy,
    AttendanceStatus,
    DataQuality,
    DeviceInput,
    PeriodInput,
    ScheduleRule,
    SessionInput,
)
from deskmate.zaza_server.attendance.rollup import aggregate
from deskmate.zaza_server.attendance.schedule import find_rule, local_to_utc
from deskmate.zaza_server.attendance.store import InMemoryAttendanceStore
from deskmate.zaza_server.attendance.summary_service import SummaryService

S = AttendanceStatus
H = 3600
UTC = timezone.utc
TZ = "Asia/Dhaka"  # UTC+6, no DST
MON = date(2026, 10, 5)
TUE, WED, THU, FRI, SAT, SUN = (MON + timedelta(days=i) for i in range(1, 7))
LATER = datetime(2026, 12, 1, tzinfo=UTC)  # "now" for finished days


def at(day: date, hhmm: str, tz: str = TZ) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm)).replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


class World:
    """One employee, one device, Mon–Fri 09:00–17:00 (8 h), weekends off."""

    def __init__(self, *, tz: str = TZ, weekly: bool = True, now: datetime = LATER,
                 policy: AttendancePolicy | None = None) -> None:
        self.tz = tz
        self.now = now
        self.store = InMemoryAttendanceStore()
        self.store.add_employee("emp-1", tz)
        self.store.add_device("emp-1", DeviceInput("dev-1", "ACTIVE", datetime(2027, 1, 1, tzinfo=UTC)))
        if weekly:
            for dow in range(1, 6):
                self.rule(day_of_week=dow, start="09:00", end="17:00", expected=8 * H)
            for dow in (6, 7):
                self.rule(day_of_week=dow, working=False)
        self.service = SummaryService(self.store, policy=policy or AttendancePolicy(), clock=lambda: self.now)

    def rule(self, *, day_of_week=None, schedule_date=None, start=None, end=None, expected=None, working=True,
             effective_from=date(2026, 1, 1), effective_to=None, tz=None) -> ScheduleRule:
        r = ScheduleRule(
            schedule_id=str(uuid.uuid4()), employee_id="emp-1", is_working_day=working, timezone=tz or self.tz,
            day_of_week=day_of_week, effective_from=effective_from if day_of_week else None,
            effective_to=effective_to if day_of_week else None, schedule_date=schedule_date,
            start_time=time.fromisoformat(start) if start else None,
            end_time=time.fromisoformat(end) if end else None, expected_work_seconds=expected,
        )
        self.store.add_rule(r)
        return r

    def period(self, status: str, start: datetime, end: datetime, *, device="dev-1", record_id=None, version=1,
               is_open=False) -> str:
        rid = record_id or str(uuid.uuid4())
        self.store.add_period("emp-1", PeriodInput(rid, device, start, end, status, is_open, version))
        return rid

    def active(self, day: date, start: str, end: str, end_day: date | None = None, **kw) -> str:
        return self.period("ACTIVE", at(day, start, self.tz), at(end_day or day, end, self.tz), **kw)

    def status(self, status: str, day: date, start: str, end: str, end_day: date | None = None, **kw) -> str:
        return self.period(status, at(day, start, self.tz), at(end_day or day, end, self.tz), **kw)

    def session(self, start: datetime, end: datetime | None, status="CLOSED", heartbeat=None, device="dev-1"):
        self.store.add_session("emp-1", SessionInput(str(uuid.uuid4()), device, status, start, end,
                                                     heartbeat or end or start))

    def day(self, d: date):
        return self.service.calculate_daily("emp-1", d)


# ─── 1-4: normal day, late, early, both ────────────────────────────────────


def test_01_normal_full_day():
    w = World()
    w.active(MON, "09:00", "17:00")
    s = w.day(MON)
    assert s.attendance_status is S.PRESENT
    assert (s.scheduled_seconds, s.active_seconds, s.tracked_seconds) == (8 * H, 8 * H, 8 * H)
    assert (s.late_seconds, s.early_leave_seconds, s.overtime_seconds) == (0, 0, 0)
    assert s.attendance_percentage == 100.0 and s.worked_day
    assert s.first_activity_at == at(MON, "09:00") and s.last_activity_at == at(MON, "17:00")
    assert s.data_quality is DataQuality.COMPLETE and s.quality_flags == ()
    assert not s.is_provisional


def test_02_starts_late():
    w = World()
    w.active(MON, "09:40", "17:00")
    s = w.day(MON)
    assert s.attendance_status is S.LATE and s.late_seconds == 40 * 60 and s.early_leave_seconds == 0


def test_03_finishes_early():
    w = World()
    w.active(MON, "09:00", "16:30")
    s = w.day(MON)
    assert s.attendance_status is S.EARLY_LEAVE and s.early_leave_seconds == 30 * 60 and s.late_seconds == 0


def test_04_late_and_early():
    w = World()
    w.active(MON, "09:30", "16:00")
    s = w.day(MON)
    assert s.attendance_status is S.LATE_AND_EARLY
    assert (s.late_seconds, s.early_leave_seconds) == (30 * 60, H)


def test_late_grace_is_configurable_and_zero_by_default():
    w = World(policy=AttendancePolicy(late_grace_seconds=5 * 60))
    w.active(MON, "09:04", "17:00")
    w.active(TUE, "09:06", "17:00")
    assert w.day(MON).attendance_status is S.PRESENT
    assert w.day(TUE).late_seconds == 6 * 60  # beyond the grace: the full lateness counts
    assert AttendancePolicy().late_grace_seconds == 0


# ─── 5-6: overtime and work before the shift ───────────────────────────────


def test_05_overtime_after_shift():
    w = World()
    w.active(MON, "09:00", "18:30")
    s = w.day(MON)
    assert s.attendance_status is S.PRESENT
    assert (s.post_shift_active_seconds, s.overtime_seconds) == (90 * 60, 90 * 60)
    assert s.active_in_shift_seconds == 8 * H and s.active_seconds == 9.5 * H


def test_06_active_work_before_shift():
    w = World()
    w.active(MON, "08:00", "17:00")
    s = w.day(MON)
    assert s.late_seconds == 0 and s.pre_shift_active_seconds == H and s.overtime_seconds == H
    w2 = World(policy=AttendancePolicy(count_pre_shift_overtime=False))
    w2.active(MON, "08:00", "17:00")
    assert w2.day(MON).overtime_seconds == 0 and w2.day(MON).pre_shift_active_seconds == H


# ─── 7-9: days off and absence ─────────────────────────────────────────────


def test_07_day_off_without_work():
    s = World().day(SAT)
    assert s.attendance_status is S.DAY_OFF and not s.is_working_day
    assert (s.scheduled_seconds, s.late_seconds, s.overtime_seconds) == (0, 0, 0)
    assert s.attendance_percentage is None and not s.worked_day


def test_08_work_on_day_off():
    w = World()
    w.active(SAT, "10:00", "12:00")
    s = w.day(SAT)
    assert s.attendance_status is S.WORKED_DAY_OFF and s.worked_day
    assert (s.overtime_seconds, s.late_seconds, s.early_leave_seconds) == (2 * H, 0, 0)
    assert s.scheduled_seconds == 0 and s.attendance_percentage is None


def test_09_complete_absence():
    s = World().day(MON)
    assert s.attendance_status is S.ABSENT
    assert (s.active_seconds, s.late_seconds, s.early_leave_seconds) == (0, 0, 0)
    assert s.attendance_percentage == 0.0 and s.data_quality is DataQuality.COMPLETE


def test_absence_not_concluded_while_a_device_has_not_synced():
    w = World()
    w.store.set_last_seen("dev-1", at(MON, "12:00"))  # last contact during the shift
    s = w.day(MON)
    assert s.attendance_status is S.DATA_INCOMPLETE
    assert "AWAITING_DEVICE_SYNC" in s.quality_flags and s.attendance_percentage is None
    w.store.set_last_seen("dev-1", at(TUE, "09:00"))  # device came back: nothing was pending
    assert w.day(MON).attendance_status is S.ABSENT


def test_no_enabled_device_is_data_incomplete_not_absent():
    w = World()
    w.store.device_inputs.clear()
    s = w.day(MON)
    assert s.attendance_status is S.DATA_INCOMPLETE and "NO_DEVICE" in s.quality_flags


# ─── 10-12: UNKNOWN monitoring ─────────────────────────────────────────────


def test_10_unknown_around_start_is_not_lateness():
    w = World()
    w.status("UNKNOWN", MON, "08:50", "09:30")
    w.active(MON, "09:30", "17:00")
    s = w.day(MON)
    assert s.late_seconds == 0 and s.attendance_status is S.PRESENT
    assert "START_UNCERTAIN" in s.quality_flags and s.data_quality is DataQuality.PARTIAL


def test_10b_lateness_counts_only_after_the_uncertain_part():
    w = World()
    w.status("UNKNOWN", MON, "09:00", "09:10")
    w.active(MON, "09:40", "17:00")  # 09:10-09:40: no data at all, i.e. not working
    s = w.day(MON)
    assert s.late_seconds == 30 * 60 and s.attendance_status is S.LATE


def test_11_unknown_around_end_is_not_early_leave():
    w = World()
    w.active(MON, "09:00", "16:00")
    w.status("UNKNOWN", MON, "16:00", "17:00")
    s = w.day(MON)
    assert s.early_leave_seconds == 0 and s.attendance_status is S.PRESENT
    assert "END_UNCERTAIN" in s.quality_flags
    assert s.measurable_scheduled_seconds == 7 * H and s.attendance_percentage == 100.0


def test_12_full_day_unknown_is_data_incomplete_not_absent():
    w = World()
    w.status("UNKNOWN", MON, "09:00", "17:00")
    s = w.day(MON)
    assert s.attendance_status is S.DATA_INCOMPLETE
    assert s.data_quality is DataQuality.INSUFFICIENT
    assert s.attendance_percentage is None and s.attendance_basis_seconds == 0
    assert (s.unknown_seconds, s.idle_seconds, s.active_seconds) == (8 * H, 0, 0)


def test_small_unknown_does_not_hide_a_clear_absence():
    w = World()
    w.status("UNKNOWN", MON, "09:00", "09:30")
    w.status("IDLE", MON, "09:30", "17:00")  # PC on, nobody using it
    assert w.day(MON).attendance_status is S.ABSENT


# ─── 13-14: several sessions, restarts ─────────────────────────────────────


def test_13_multiple_sessions_same_day():
    w = World()
    w.session(at(MON, "09:00"), at(MON, "12:00"))
    w.active(MON, "09:00", "12:00")
    w.session(at(MON, "13:00"), at(MON, "17:00"))
    w.active(MON, "13:00", "17:00")
    s = w.day(MON)
    assert s.session_count == 2 and s.active_seconds == 7 * H
    assert s.attendance_status is S.PRESENT and s.tracked_seconds == 7 * H


def test_14_agent_restart_during_workday():
    w = World()
    w.session(at(MON, "09:00"), at(MON, "12:00"), status="INTERRUPTED")
    w.active(MON, "09:00", "12:00")
    w.session(at(MON, "12:05"), at(MON, "17:00"))
    w.active(MON, "12:05", "17:00")
    s = w.day(MON)
    assert s.attendance_status is S.PRESENT and s.active_seconds == 8 * H - 5 * 60
    assert "INTERRUPTED_SESSION" in s.quality_flags and s.data_quality is DataQuality.PARTIAL


def test_crash_near_the_end_is_not_early_leave():
    w = World()
    w.session(at(MON, "09:00"), None, status="INTERRUPTED", heartbeat=at(MON, "15:00"))
    w.active(MON, "09:00", "15:00")
    s = w.day(MON)
    assert s.early_leave_seconds == 0 and "END_UNCERTAIN" in s.quality_flags


# ─── 15-16: midnight and overnight shifts ──────────────────────────────────


def test_15_activity_crossing_midnight_is_split_once_and_never_masks_lateness():
    w = World()
    w.active(MON, "09:00", "17:00")
    w.active(MON, "22:00", "01:00", end_day=TUE)  # evening work past midnight
    w.active(TUE, "09:30", "17:00")
    mon, tue = w.day(MON), w.day(TUE)
    assert mon.overtime_seconds == 2 * H and tue.pre_shift_active_seconds == H
    assert mon.active_seconds + tue.active_seconds == (8 + 3 + 7.5) * H  # nothing lost, nothing doubled
    assert tue.late_seconds == 30 * 60  # 00:00-01:00 work doesn't hide the late arrival
    assert tue.first_activity_at == at(TUE, "00:00")


def test_16_overnight_shift_20_to_04():
    w = World(weekly=False)
    for dow in range(1, 6):
        w.rule(day_of_week=dow, start="20:00", end="04:00", expected=8 * H)
    w.active(MON, "19:30", "04:45", end_day=TUE)
    mon, tue = w.day(MON), w.day(TUE)
    assert mon.scheduled_start == at(MON, "20:00") and mon.scheduled_end == at(TUE, "04:00")
    assert (mon.scheduled_seconds, mon.active_in_shift_seconds) == (8 * H, 8 * H)
    assert (mon.pre_shift_active_seconds, mon.post_shift_active_seconds) == (30 * 60, 45 * 60)
    assert mon.overtime_seconds == 75 * 60 and mon.attendance_status is S.PRESENT
    assert tue.active_seconds == 0  # Tuesday's own shift starts Tuesday 20:00


def test_overnight_shift_before_a_day_off_keeps_its_overtime():
    w = World(weekly=False)
    w.rule(day_of_week=5, start="20:00", end="04:00", expected=8 * H)
    w.rule(day_of_week=6, working=False)
    w.active(FRI, "20:00", "05:00", end_day=SAT)
    w.active(SAT, "14:00", "15:00")
    fri, sat = w.day(FRI), w.day(SAT)
    assert fri.overtime_seconds == H  # 04:00-05:00 belongs to Friday's shift
    assert sat.attendance_status is S.WORKED_DAY_OFF and sat.active_seconds == H


# ─── 17-19: schedule resolution ────────────────────────────────────────────


def test_17_specific_date_overrides_weekly_rule():
    w = World()
    w.rule(schedule_date=MON, start="12:00", end="16:00", expected=4 * H)
    w.rule(schedule_date=TUE, working=False)
    w.active(MON, "12:00", "16:00")
    mon, tue = w.day(MON), w.day(TUE)
    assert mon.schedule_kind == "DATE" and mon.scheduled_seconds == 4 * H and mon.attendance_status is S.PRESENT
    assert tue.attendance_status is S.DAY_OFF


def test_18_effective_from_and_effective_to():
    w = World(weekly=False)
    w.rule(day_of_week=1, start="09:00", end="17:00", expected=8 * H, effective_to=date(2026, 10, 11))
    w.rule(day_of_week=1, start="10:00", end="18:00", expected=8 * H, effective_from=date(2026, 10, 12),
           effective_to=date(2026, 10, 18))
    assert w.day(MON).scheduled_start == at(MON, "09:00")
    assert w.day(MON + timedelta(days=7)).scheduled_start == at(MON + timedelta(days=7), "10:00")
    assert w.day(MON + timedelta(days=14)).attendance_status is S.NO_SCHEDULE


def test_newest_overlapping_weekly_rule_wins():
    w = World(weekly=False)
    w.rule(day_of_week=1, start="09:00", end="17:00", expected=8 * H, effective_from=date(2026, 1, 1))
    w.rule(day_of_week=1, start="08:00", end="16:00", expected=8 * H, effective_from=date(2026, 9, 1))
    rule, kind = find_rule(w.store.rules, MON)
    assert kind == "WEEKLY" and rule.start_time == time(8)


def test_19_no_applicable_schedule():
    w = World(weekly=False)
    w.active(MON, "10:00", "12:00")
    s = w.day(MON)
    assert s.attendance_status is S.NO_SCHEDULE and not s.is_working_day
    assert (s.scheduled_seconds, s.overtime_seconds, s.late_seconds) == (0, 0, 0)
    assert s.active_seconds == 2 * H and s.worked_day and "NO_SCHEDULE" in s.quality_flags


# ─── 20-21: daylight saving time ───────────────────────────────────────────


NY = "America/New_York"


def test_20_dst_spring_forward_shift_is_an_hour_shorter():
    sat = date(2026, 3, 7)  # DST starts Sunday 2026-03-08 02:00
    w = World(tz=NY, weekly=False)
    w.rule(day_of_week=6, start="22:00", end="06:00", expected=8 * H)
    w.period("ACTIVE", at(sat, "22:00", NY), at(sat + timedelta(days=1), "06:00", NY))
    s = w.day(sat)
    assert s.shift_span_seconds == 7 * H and s.scheduled_seconds == 7 * H
    assert s.active_in_shift_seconds == 7 * H and s.attendance_percentage == 100.0
    assert "DST_ADJUSTED" in s.quality_flags and s.data_quality is DataQuality.COMPLETE  # informational only


def test_21_dst_fall_back_shift_is_an_hour_longer():
    sat = date(2026, 10, 31)  # DST ends Sunday 2026-11-01 02:00
    w = World(tz=NY, weekly=False)
    w.rule(day_of_week=6, start="22:00", end="06:00", expected=8 * H)
    w.period("ACTIVE", at(sat, "22:00", NY), at(sat + timedelta(days=1), "06:00", NY))
    s = w.day(sat)
    assert s.shift_span_seconds == 9 * H and s.scheduled_seconds == 8 * H
    assert s.active_in_shift_seconds == 9 * H and s.overtime_seconds == 0
    assert s.attendance_percentage == 100.0


def test_nonexistent_and_ambiguous_local_times():
    gap, flagged = local_to_utc(date(2026, 3, 8), time(2, 30), NY)  # doesn't exist
    assert flagged and gap == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)  # moved forward: 03:30 EDT
    twice, flagged = local_to_utc(date(2026, 11, 1), time(1, 30), NY)  # happens twice
    assert flagged and twice == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # first occurrence (EDT)


def test_calendar_days_without_a_shift_follow_dst_lengths():
    w = World(tz=NY, weekly=False)
    w.rule(day_of_week=7, working=False)
    sunday = date(2026, 3, 8)
    s = w.day(sunday)
    assert (s.window_end - s.window_start) == timedelta(hours=23)


# ─── 22-24: idempotency, new versions, provisional ─────────────────────────


def test_22_recalculation_is_idempotent():
    w = World()
    w.active(MON, "09:00", "17:00")
    first = w.day(MON)
    writes = w.store.writes
    assert w.store.save_daily(w.service.compute_daily("emp-1", MON)) is False
    second = w.day(MON)
    assert first == second and w.store.writes == writes
    assert len(w.store.daily) == 1


def test_23_higher_version_activity_changes_the_summary():
    w = World()
    rid = w.active(MON, "09:00", "12:00")
    assert w.day(MON).attendance_status is S.EARLY_LEAVE
    w.active(MON, "09:00", "17:00", record_id=rid, version=2)
    w.active(MON, "09:00", "10:00", record_id=rid, version=1)  # an older version never wins
    s = w.day(MON)
    assert s.attendance_status is S.PRESENT and s.active_seconds == 8 * H


def test_24_open_session_gives_a_provisional_summary():
    w = World(now=at(MON, "13:00"))
    w.session(at(MON, "08:55"), None, status="OPEN", heartbeat=at(MON, "13:00"))
    w.active(MON, "09:00", "13:00", is_open=True)
    s = w.day(MON)
    assert s.is_provisional and "PROVISIONAL" in s.quality_flags
    assert s.attendance_status is S.PRESENT and s.early_leave_seconds == 0  # can't leave early mid-shift
    assert s.active_seconds == 4 * H
    w.now = at(MON, "11:00")
    w.store.period_inputs.clear()
    assert w.day(MON).attendance_status is S.PENDING  # not absent before the shift is over


# ─── 25-27: roll-ups and attendance % ──────────────────────────────────────


def _week(w: World):
    w.active(MON, "09:00", "17:00")             # present
    w.active(TUE, "09:30", "17:00")             # late 30m
    w.active(WED, "09:00", "16:00")             # early 1h
    w.status("UNKNOWN", THU, "09:00", "17:00")  # data incomplete
    # FRI absent
    w.active(SAT, "10:00", "11:00")             # worked day off


def test_25_weekly_aggregation_from_daily():
    w = World()
    _week(w)
    week = w.service.calculate_week("emp-1", WED)
    days = [w.store.get_daily("emp-1", d) for d in (MON, TUE, WED, THU, FRI, SAT, SUN)]
    assert (week.period_start, week.period_end) == (MON, SUN)
    for name in ("scheduled_seconds", "active_seconds", "idle_seconds", "unknown_seconds", "locked_seconds",
                 "tracked_seconds", "late_seconds", "early_leave_seconds", "overtime_seconds"):
        assert getattr(week, name) == sum(getattr(d, name) for d in days), name
    assert (week.working_days, week.days_worked) == (5, 4)
    assert (week.absent_days, week.late_days, week.early_leave_days) == (1, 1, 1)
    assert (week.incomplete_days, week.worked_day_off_days) == (1, 1)
    assert week.average_active_seconds_per_worked_day == week.active_seconds // 4
    assert week.data_quality is DataQuality.PARTIAL


def test_26_monthly_aggregation_from_daily():
    w = World()
    _week(w)
    month = w.service.calculate_month("emp-1", date(2026, 10, 20))
    assert (month.period_start, month.period_end) == (date(2026, 10, 1), date(2026, 10, 31))
    days = w.store.list_daily("emp-1", date(2026, 10, 1), date(2026, 10, 31))
    assert len(days) == 31
    assert month.active_seconds == sum(d.active_seconds for d in days)
    assert month.working_days == 22 and month.days_worked == 4
    assert month.absent_days == 22 - 3 - 1  # every other weekday had no activity
    assert month.late_days == 1


def test_27_attendance_percentage_formula():
    w = World()
    w.active(MON, "09:00", "15:00")                 # 6 h of 8 h
    w.active(TUE, "09:00", "15:00")
    w.status("UNKNOWN", TUE, "15:00", "17:00")      # 2 h unmeasurable
    mon, tue = w.day(MON), w.day(TUE)
    assert mon.attendance_percentage == 75.0
    assert (tue.measurable_scheduled_seconds, tue.attendance_percentage) == (6 * H, 100.0)
    # week: sum(credit) / sum(basis), DATA_INCOMPLETE days excluded from both
    w.status("UNKNOWN", WED, "09:00", "17:00")
    week = w.service.calculate_week("emp-1", MON)
    # Mon 6/8, Tue 6/6, Wed excluded, Thu & Fri absent 0/8 each
    assert week.attendance_basis_seconds == (8 + 6 + 8 + 8) * H
    assert week.attendance_percentage == round(12 / 30 * 100, 2)


def test_attendance_is_capped_at_100_percent():
    w = World(weekly=False)
    w.rule(day_of_week=1, start="09:00", end="17:00", expected=7 * H)  # 1 h break allowed
    w.active(MON, "09:00", "17:00")
    s = w.day(MON)
    assert s.attendance_credit_seconds == 7 * H and s.attendance_percentage == 100.0


# ─── 28-32: status separation, overtime, double counting, time zones ──────


def test_28_unknown_is_not_idle():
    w = World()
    w.active(MON, "09:00", "12:00")
    w.status("UNKNOWN", MON, "12:00", "13:00")
    w.active(MON, "13:00", "17:00")
    s = w.day(MON)
    assert (s.unknown_seconds, s.idle_seconds, s.active_seconds) == (H, 0, 7 * H)
    assert s.tracked_seconds == 8 * H and s.detected_break_seconds == 0


def test_29_locked_is_not_idle():
    w = World()
    w.active(MON, "09:00", "12:00")
    w.status("LOCKED", MON, "12:00", "13:00")
    w.active(MON, "13:00", "17:00")
    s = w.day(MON)
    assert (s.locked_seconds, s.idle_seconds) == (H, 0)
    assert s.detected_break_seconds == H  # a qualifying away-from-desk run, labelled "detected break / idle"


def test_detected_break_needs_a_long_enough_run():
    w = World()
    w.active(MON, "09:00", "11:00")
    w.status("IDLE", MON, "11:00", "11:10")  # 10 min: too short
    w.active(MON, "11:10", "12:00")
    w.status("IDLE", MON, "12:00", "12:20")
    w.status("LOCKED", MON, "12:20", "12:45")  # 45 min run of IDLE+LOCKED
    w.active(MON, "12:45", "17:00")
    s = w.day(MON)
    assert s.detected_break_seconds == 45 * 60 and s.idle_seconds == 30 * 60


def test_30_overtime_counts_active_time_only():
    w = World()
    w.active(MON, "09:00", "17:00")
    w.status("IDLE", MON, "17:00", "19:00")
    w.status("UNKNOWN", MON, "19:00", "20:00")
    s = w.day(MON)
    assert s.overtime_seconds == 0 and s.tracked_seconds == 11 * H
    w.active(MON, "20:00", "20:30")
    assert w.day(MON).overtime_seconds == 30 * 60


def test_31_no_double_counting_of_overlapping_periods():
    w = World()
    w.active(MON, "09:00", "17:00", device="dev-1")
    w.active(MON, "09:00", "17:00", device="dev-1")             # duplicate record
    w.store.add_device("emp-1", DeviceInput("dev-2", "ACTIVE", datetime(2027, 1, 1, tzinfo=UTC)))
    w.status("IDLE", MON, "08:00", "18:00", device="dev-2")     # second PC idle meanwhile
    s = w.day(MON)
    assert s.active_seconds == 8 * H and s.idle_seconds == 2 * H and s.tracked_seconds == 10 * H
    assert "OVERLAPPING_PERIODS" in s.quality_flags and s.device_count == 2


def test_days_partition_time_exactly_once():
    w = World(weekly=False)
    w.rule(day_of_week=1, start="20:00", end="04:00", expected=8 * H)
    w.rule(day_of_week=2, start="09:00", end="17:00", expected=8 * H)
    w.active(MON, "06:00", "23:00", end_day=TUE)  # 41 hours of continuous activity
    days = [w.day(MON + timedelta(days=i)) for i in range(-1, 3)]
    assert sum(d.active_seconds for d in days) == 41 * H
    windows = sorted((d.window_start, d.window_end) for d in days)
    assert all(a[1] <= b[0] for a, b in zip(windows, windows[1:], strict=False))


def test_32_timezone_conversion():
    w = World()  # Asia/Dhaka, UTC+6
    w.period("ACTIVE", datetime(2026, 10, 5, 3, 0, tzinfo=UTC), datetime(2026, 10, 5, 11, 0, tzinfo=UTC))
    s = w.day(MON)
    assert s.scheduled_start == datetime(2026, 10, 5, 3, 0, tzinfo=UTC)  # 09:00 Dhaka
    assert s.scheduled_end == datetime(2026, 10, 5, 11, 0, tzinfo=UTC)
    assert s.attendance_status is S.PRESENT and s.timezone == TZ
    # The same UTC activity is Sunday 20:00 -> Monday 04:00 in Los Angeles:
    # split at local midnight (04:00 is outside Monday's 4 h pre-shift margin).
    other = World(tz="America/Los_Angeles")
    other.period("ACTIVE", datetime(2026, 10, 5, 3, 0, tzinfo=UTC), datetime(2026, 10, 5, 11, 0, tzinfo=UTC))
    sun, mon = other.day(date(2026, 10, 4)), other.day(MON)
    assert (sun.active_seconds, mon.active_seconds) == (4 * H, 4 * H)
    assert sun.attendance_status is S.WORKED_DAY_OFF and mon.attendance_status is S.ABSENT


# ─── service plumbing ──────────────────────────────────────────────────────


def test_recalculate_covers_whole_weeks_and_months_but_never_future_days():
    w = World(now=at(date(2026, 10, 14), "12:00"))
    w.active(MON, "09:00", "17:00")
    result = w.service.recalculate(MON, MON)
    days = w.store.list_daily("emp-1", date(2026, 10, 1), date(2026, 10, 31))
    assert days[0].local_date == date(2026, 10, 1) and days[-1].local_date == date(2026, 10, 14)
    assert result.days == 14 and result.weeks == 1 and result.months == 1
    month = w.store.get_period("emp-1", "MONTH", date(2026, 10, 1))
    assert month.is_provisional
    again = w.service.recalculate(MON, MON)
    assert again.days_changed == 0


def test_aggregate_of_no_days_is_insufficient():
    p = aggregate("emp-1", "WEEK", MON, SUN, TZ, [])
    assert p.data_quality is DataQuality.INSUFFICIENT and p.attendance_percentage is None and p.is_provisional


def test_policy_validation():
    with pytest.raises(ValueError):
        AttendancePolicy(attribution_margin_seconds=13 * H)
    with pytest.raises(ValueError):
        AttendancePolicy(late_grace_seconds=-1)


# ─── review fix 1: schedule timezone = employee timezone ───────────────────


def test_schedule_in_the_employee_timezone_is_accepted():
    w = World(weekly=False)
    w.rule(day_of_week=1, start="09:00", end="17:00", expected=8 * H)  # the employee's timezone
    w.rule(day_of_week=2, start="09:00", end="17:00", expected=8 * H, tz=TZ)  # explicit and equal
    assert {r.timezone for r in w.store.rules} == {TZ}


def test_schedule_in_a_different_timezone_is_rejected():
    w = World(weekly=False)
    with pytest.raises(ValueError, match="differs from employee 'emp-1' timezone 'Asia/Dhaka'"):
        w.rule(day_of_week=1, start="09:00", end="17:00", expected=8 * H, tz="Europe/London")
    assert w.store.rules == []


def test_overnight_schedule_in_the_employee_timezone_still_works():
    w = World(weekly=False)
    w.rule(day_of_week=1, start="20:00", end="04:00", expected=8 * H, tz=TZ)
    w.active(MON, "20:00", "04:00", end_day=TUE)
    s = w.day(MON)
    assert s.attendance_status is S.PRESENT and s.active_in_shift_seconds == 8 * H
    assert s.scheduled_end == at(TUE, "04:00")


def test_dst_schedule_in_the_employee_timezone_still_works():
    sat = date(2026, 3, 7)
    w = World(tz=NY, weekly=False)
    w.rule(day_of_week=6, start="22:00", end="06:00", expected=8 * H, tz=NY)
    w.period("ACTIVE", at(sat, "22:00", NY), at(sat + timedelta(days=1), "06:00", NY))
    s = w.day(sat)
    assert s.timezone == NY and s.scheduled_seconds == 7 * H and s.attendance_status is S.PRESENT


# ─── review fix 2: --recent-days per employee local date ───────────────────

# 2026-10-07 18:30 UTC = 2026-10-08 00:30 in Dhaka (UTC+6) = 2026-10-07 14:30 in New York
JUST_AFTER_DHAKA_MIDNIGHT = datetime(2026, 10, 7, 18, 30, tzinfo=UTC)


def _two_timezone_world() -> World:
    w = World(now=JUST_AFTER_DHAKA_MIDNIGHT)
    w.store.add_employee("emp-ny", NY)
    w.store.add_device("emp-ny", DeviceInput("dev-ny", "ACTIVE", JUST_AFTER_DHAKA_MIDNIGHT))
    for dow in range(1, 6):
        w.store.add_rule(ScheduleRule(str(uuid.uuid4()), "emp-ny", True, NY, day_of_week=dow,
                                      effective_from=date(2026, 1, 1), start_time=time(9), end_time=time(17),
                                      expected_work_seconds=8 * H))
    return w


def test_recent_days_1_is_only_the_employee_local_today():
    w = World(now=JUST_AFTER_DHAKA_MIDNIGHT)
    assert JUST_AFTER_DHAKA_MIDNIGHT.date() == date(2026, 10, 7)  # UTC is still the previous date
    assert w.service.recent_range("emp-1", 1) == (date(2026, 10, 8), date(2026, 10, 8))


def test_recent_days_7_is_today_and_the_previous_6_local_dates():
    w = World(now=JUST_AFTER_DHAKA_MIDNIGHT)
    assert w.service.recent_range("emp-1", 7) == (date(2026, 10, 2), date(2026, 10, 8))


def test_recent_days_use_each_employee_own_today_and_never_create_future_days():
    w = _two_timezone_world()
    assert w.service.recent_range("emp-ny", 7) == (date(2026, 10, 1), date(2026, 10, 7))
    w.service.recalculate_recent(7)
    dhaka = w.store.list_daily("emp-1", date(2026, 1, 1), date(2027, 1, 1))
    ny = w.store.list_daily("emp-ny", date(2026, 1, 1), date(2027, 1, 1))
    assert dhaka[-1].local_date == date(2026, 10, 8)  # Dhaka is already on October 8
    assert ny[-1].local_date == date(2026, 10, 7)     # New York is still on October 7
    assert dhaka[0].local_date == ny[0].local_date == date(2026, 9, 28)  # whole ISO week (Mon)
    assert w.store.get_period("emp-1", "WEEK", MON) is not None
    assert w.store.get_period("emp-ny", "MONTH", date(2026, 10, 1)).is_provisional
    assert w.service.recalculate_recent(7).days_changed == 0  # idempotent


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_recent_days_must_be_at_least_1(bad):
    w = World(now=JUST_AFTER_DHAKA_MIDNIGHT)
    with pytest.raises(ValueError, match=">= 1"):
        w.service.recalculate_recent(bad)
    with pytest.raises(ValueError, match=">= 1"):
        w.service.recent_range("emp-1", bad)


# ─── review fix 3: uncertainty protects only the uncertain part ────────────


def test_early_leave_crash_immediately_after_last_activity_is_not_charged():
    w = World()
    w.session(at(MON, "09:00"), None, status="INTERRUPTED", heartbeat=at(MON, "15:00"))
    w.active(MON, "09:00", "15:00")
    s = w.day(MON)
    assert s.early_leave_seconds == 0 and s.attendance_status is S.PRESENT
    assert "END_UNCERTAIN" in s.quality_flags


def test_early_leave_keeps_reliable_idle_before_a_crash():
    w = World()
    w.session(at(MON, "09:00"), None, status="INTERRUPTED", heartbeat=at(MON, "15:00"))
    w.active(MON, "09:00", "12:00")
    w.status("IDLE", MON, "12:00", "14:00")
    w.status("LOCKED", MON, "14:00", "15:00")  # reliably not working until the crash at 15:00
    s = w.day(MON)
    assert s.early_leave_seconds == 3 * H and s.attendance_status is S.EARLY_LEAVE
    assert "END_UNCERTAIN" in s.quality_flags and "INTERRUPTED_SESSION" in s.quality_flags


def test_early_leave_with_unknown_tail_counts_only_until_unknown_begins():
    w = World()
    w.active(MON, "09:00", "12:00")
    w.status("IDLE", MON, "12:00", "14:00")
    w.status("UNKNOWN", MON, "14:00", "17:00")
    s = w.day(MON)
    assert s.early_leave_seconds == 2 * H and "END_UNCERTAIN" in s.quality_flags


def test_early_leave_excludes_only_an_uncertain_gap_followed_by_reliable_idle():
    w = World()
    w.session(at(MON, "09:00"), at(MON, "12:00"), status="INTERRUPTED")
    w.active(MON, "09:00", "12:00")
    w.session(at(MON, "12:05"), at(MON, "17:00"))  # agent restarted: reliably idle afterwards
    w.status("IDLE", MON, "12:05", "17:00")
    s = w.day(MON)
    assert s.early_leave_seconds == 5 * H - 5 * 60 and "END_UNCERTAIN" in s.quality_flags


def test_early_leave_with_unsynced_tail_keeps_earlier_reliable_inactivity():
    w = World()
    w.active(MON, "09:00", "12:00")
    w.status("IDLE", MON, "12:00", "15:00")
    w.store.set_last_seen("dev-1", at(MON, "15:00"))  # disconnected at 15:00, not synced since
    s = w.day(MON)
    assert s.early_leave_seconds == 3 * H and s.attendance_status is S.EARLY_LEAVE
    assert {"END_UNCERTAIN", "AWAITING_DEVICE_SYNC"} <= set(s.quality_flags)
    w.store.set_last_seen("dev-1", at(MON, "12:00"))  # nothing after the last activity is known
    assert w.day(MON).early_leave_seconds == 0


def test_clean_early_leave_is_unchanged():
    w = World()
    w.session(at(MON, "09:00"), at(MON, "16:30"))  # clean sign-out
    w.active(MON, "09:00", "16:30")
    s = w.day(MON)
    assert s.early_leave_seconds == 30 * 60 and s.attendance_status is S.EARLY_LEAVE
    assert s.quality_flags == () and s.data_quality is DataQuality.COMPLETE
    w2 = World(policy=AttendancePolicy(early_leave_grace_seconds=30 * 60))
    w2.active(MON, "09:00", "16:30")
    assert w2.day(MON).attendance_status is S.PRESENT  # within the grace


def test_crash_after_the_shift_does_not_erase_early_leave():
    w = World()
    w.session(at(MON, "09:00"), None, status="INTERRUPTED", heartbeat=at(MON, "18:00"))
    w.active(MON, "09:00", "16:00")
    w.status("IDLE", MON, "16:00", "18:00")
    s = w.day(MON)
    assert s.early_leave_seconds == H and "END_UNCERTAIN" not in s.quality_flags


# ─── review fix 4: every policy value configurable ─────────────────────────

POLICY_ENV_NAMES = ("LATE_GRACE_SECONDS", "EARLY_LEAVE_GRACE_SECONDS", "OVERTIME_MIN_SECONDS",
                    "COUNT_PRE_SHIFT_OVERTIME", "ATTRIBUTION_MARGIN_SECONDS", "BREAK_MIN_SECONDS",
                    "INCOMPLETE_UNKNOWN_RATIO")


@pytest.fixture
def clean_policy_env(monkeypatch):
    for name in POLICY_ENV_NAMES:
        monkeypatch.delenv(f"ZAZA_ATTENDANCE_{name}", raising=False)
    return monkeypatch


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("FALSE", False), (" 1 ", True), ("0", False),
                                                ("yes", True), ("off", False), ("", True)])
def test_policy_env_pre_shift_overtime_boolean(clean_policy_env, raw, expected):
    clean_policy_env.setenv("ZAZA_ATTENDANCE_COUNT_PRE_SHIFT_OVERTIME", raw)
    assert AttendancePolicy.from_env().count_pre_shift_overtime is expected


def test_policy_env_loads_every_field(clean_policy_env):
    values = dict(zip(POLICY_ENV_NAMES, ("60", "120", "900", "false", "7200", "600", "0.75"), strict=True))
    for k, v in values.items():
        clean_policy_env.setenv(f"ZAZA_ATTENDANCE_{k}", v)
    assert AttendancePolicy.from_env() == AttendancePolicy(
        late_grace_seconds=60, early_leave_grace_seconds=120, overtime_min_seconds=900,
        count_pre_shift_overtime=False, attribution_margin_seconds=7200, break_min_seconds=600,
        incomplete_unknown_ratio=0.75)


def test_policy_env_defaults_when_unset(clean_policy_env):
    assert AttendancePolicy.from_env() == AttendancePolicy()


@pytest.mark.parametrize(("name", "raw", "message"), [
    ("COUNT_PRE_SHIFT_OVERTIME", "maybe", "must be true or false"),
    ("ATTRIBUTION_MARGIN_SECONDS", "50000", "ZAZA_ATTENDANCE_ATTRIBUTION_MARGIN_SECONDS: .*0..43200"),
    ("ATTRIBUTION_MARGIN_SECONDS", "-1", "ZAZA_ATTENDANCE_ATTRIBUTION_MARGIN_SECONDS"),
    ("ATTRIBUTION_MARGIN_SECONDS", "4h", "must be a whole number of seconds"),
    ("INCOMPLETE_UNKNOWN_RATIO", "0", r"ZAZA_ATTENDANCE_INCOMPLETE_UNKNOWN_RATIO: .*\(0, 1\]"),
    ("INCOMPLETE_UNKNOWN_RATIO", "1.5", "ZAZA_ATTENDANCE_INCOMPLETE_UNKNOWN_RATIO"),
    ("INCOMPLETE_UNKNOWN_RATIO", "nan", "finite"),
    ("INCOMPLETE_UNKNOWN_RATIO", "half", "must be a number"),
    ("LATE_GRACE_SECONDS", "-5", "ZAZA_ATTENDANCE_LATE_GRACE_SECONDS: late_grace_seconds must be >= 0"),
])
def test_policy_env_invalid_values_are_rejected_clearly(clean_policy_env, name, raw, message):
    clean_policy_env.setenv(f"ZAZA_ATTENDANCE_{name}", raw)
    with pytest.raises(ValueError, match=message):
        AttendancePolicy.from_env()
