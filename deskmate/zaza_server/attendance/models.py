"""Inputs, outputs and policy for the attendance calculations.

Everything here is plain data. The calculation itself is in
``calculator.py`` (pure functions), storage in ``store.py`` /
``postgres_store.py``, orchestration in ``summary_service.py``.
"""

from __future__ import annotations

import enum
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time

# Bump when a formula changes; stored on every summary row so old rows can be
# found and recalculated.
#   1  initial Phase 5 formulas
#   2  early leave excludes only the uncertain part of the shift's end
CALCULATION_VERSION = 2


class AttendanceStatus(str, enum.Enum):
    PRESENT = "PRESENT"                # worked during the shift; not late, no early leave
    LATE = "LATE"
    EARLY_LEAVE = "EARLY_LEAVE"
    LATE_AND_EARLY = "LATE_AND_EARLY"
    ABSENT = "ABSENT"                  # shift over, no ACTIVE time in the shift, data reliable
    DAY_OFF = "DAY_OFF"
    WORKED_DAY_OFF = "WORKED_DAY_OFF"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"  # no ACTIVE time in the shift, but data can't support "absent"
    NO_SCHEDULE = "NO_SCHEDULE"        # no schedule rule applies to this date
    PENDING = "PENDING"                # shift not over and no ACTIVE time in it yet


class DataQuality(str, enum.Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class QualityFlag(str, enum.Enum):
    PROVISIONAL = "PROVISIONAL"                    # day/shift not over, or a session is still open
    UNKNOWN_TIME = "UNKNOWN_TIME"                  # some time was UNKNOWN (monitoring uncertain)
    START_UNCERTAIN = "START_UNCERTAIN"            # UNKNOWN before first activity: lateness not charged for it
    END_UNCERTAIN = "END_UNCERTAIN"                # part of the shift's end can't be judged: not charged as early leave
    AWAITING_DEVICE_SYNC = "AWAITING_DEVICE_SYNC"  # a device hasn't contacted the server since the shift/day ended
    NO_DEVICE = "NO_DEVICE"                        # employee has no enabled device
    NO_SCHEDULE = "NO_SCHEDULE"
    INTERRUPTED_SESSION = "INTERRUPTED_SESSION"    # the agent stopped without a clean end (crash/power loss)
    OVERLAPPING_PERIODS = "OVERLAPPING_PERIODS"    # input periods overlapped; normalized, not double counted
    SCHEDULE_OVERLAP = "SCHEDULE_OVERLAP"          # this shift overlaps another date's shift
    DST_ADJUSTED = "DST_ADJUSTED"                  # informational: DST changed the real shift length/time


# Flags that are informational only and don't lower data quality.
INFORMATIONAL_FLAGS = frozenset({QualityFlag.DST_ADJUSTED})

STATUS_PRIORITY = {"ACTIVE": 4, "IDLE": 3, "LOCKED": 2, "UNKNOWN": 1}


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _env_raw(name: str) -> str | None:
    raw = os.environ.get(name)
    return raw.strip() if raw is not None and raw.strip() != "" else None


def _env_int(name: str, default: int) -> int:
    raw = _env_raw(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number of seconds, got {raw!r}") from None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_raw(name)
    if raw is None:
        return default
    if raw.lower() in _TRUE:
        return True
    if raw.lower() in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _env_float(name: str, default: float) -> float:
    raw = _env_raw(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    return value


# AttendancePolicy field -> environment variable (all optional; see .env.example)
POLICY_ENV = {
    "late_grace_seconds": "ZAZA_ATTENDANCE_LATE_GRACE_SECONDS",
    "early_leave_grace_seconds": "ZAZA_ATTENDANCE_EARLY_LEAVE_GRACE_SECONDS",
    "overtime_min_seconds": "ZAZA_ATTENDANCE_OVERTIME_MIN_SECONDS",
    "count_pre_shift_overtime": "ZAZA_ATTENDANCE_COUNT_PRE_SHIFT_OVERTIME",
    "attribution_margin_seconds": "ZAZA_ATTENDANCE_ATTRIBUTION_MARGIN_SECONDS",
    "break_min_seconds": "ZAZA_ATTENDANCE_BREAK_MIN_SECONDS",
    "incomplete_unknown_ratio": "ZAZA_ATTENDANCE_INCOMPLETE_UNKNOWN_RATIO",
}


@dataclass(frozen=True)
class AttendancePolicy:
    """Tunable rules. Defaults invent nothing: no grace periods and no
    overtime minimum unless explicitly configured."""

    late_grace_seconds: int = 0
    early_leave_grace_seconds: int = 0
    overtime_min_seconds: int = 0          # per side (before / after the shift)
    count_pre_shift_overtime: bool = True  # ACTIVE time before the shift counts as overtime
    attribution_margin_seconds: int = 4 * 3600  # activity this close to a shift belongs to that shift's date
    break_min_seconds: int = 15 * 60       # IDLE/LOCKED runs at least this long count as "detected break / idle"
    incomplete_unknown_ratio: float = 0.5  # UNKNOWN share of a shift at which "absent" can't be concluded

    def __post_init__(self) -> None:
        for name in ("late_grace_seconds", "early_leave_grace_seconds", "overtime_min_seconds",
                     "break_min_seconds"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if not 0 <= self.attribution_margin_seconds <= 12 * 3600:
            raise ValueError("attribution_margin_seconds must be 0..43200 (12 h)")
        if not isinstance(self.count_pre_shift_overtime, bool):
            raise ValueError("count_pre_shift_overtime must be true or false")
        if not 0 < self.incomplete_unknown_ratio <= 1:
            raise ValueError("incomplete_unknown_ratio must be in (0, 1]")

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_env(cls) -> AttendancePolicy:
        """Every field can be set from the environment (``POLICY_ENV``);
        unset or empty variables keep the defaults. Invalid values raise
        ``ValueError`` naming the variable."""
        defaults = cls()
        values: dict = {}
        for name, env in POLICY_ENV.items():
            default = getattr(defaults, name)
            if isinstance(default, bool):
                values[name] = _env_bool(env, default)
            elif isinstance(default, float):
                values[name] = _env_float(env, default)
            else:
                values[name] = _env_int(env, default)
        try:
            return cls(**values)
        except ValueError as exc:
            field_name = str(exc).split()[0]
            raise ValueError(f"{POLICY_ENV.get(field_name, field_name)}: {exc}") from None


# ─── inputs ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmployeeInfo:
    employee_id: str
    timezone: str
    is_active: bool = True


@dataclass(frozen=True)
class ScheduleRule:
    schedule_id: str
    employee_id: str
    is_working_day: bool
    timezone: str
    day_of_week: int | None = None      # ISO 1=Mon..7=Sun (weekly rule)
    effective_from: date | None = None
    effective_to: date | None = None
    schedule_date: date | None = None   # one-off rule (overrides weekly)
    start_time: time | None = None
    end_time: time | None = None
    expected_work_seconds: int | None = None


@dataclass(frozen=True)
class PeriodInput:
    """One synced activity period (all devices of the employee)."""

    record_id: str
    device_id: str
    started_at: datetime
    ended_at: datetime
    status: str  # ACTIVE | IDLE | UNKNOWN | LOCKED
    is_open: bool = False
    record_version: int = 1


@dataclass(frozen=True)
class SessionInput:
    session_id: str
    device_id: str
    status: str  # OPEN | CLOSED | INTERRUPTED
    started_at: datetime
    ended_at: datetime | None
    last_heartbeat_at: datetime

    @property
    def effective_end(self) -> datetime:
        return self.ended_at or self.last_heartbeat_at


@dataclass(frozen=True)
class DeviceInput:
    device_id: str
    status: str  # ACTIVE | DISABLED
    last_seen_at: datetime | None


# ─── outputs ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DailySummary:
    employee_id: str
    local_date: date
    timezone: str
    schedule_id: str | None
    schedule_kind: str  # WEEKLY | DATE | NONE
    is_working_day: bool
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    shift_span_seconds: int
    scheduled_seconds: int
    measurable_scheduled_seconds: int
    window_start: datetime
    window_end: datetime
    tracked_seconds: int
    active_seconds: int
    idle_seconds: int
    unknown_seconds: int
    locked_seconds: int
    active_in_shift_seconds: int
    tracked_in_shift_seconds: int
    unknown_in_shift_seconds: int
    pre_shift_active_seconds: int
    post_shift_active_seconds: int
    detected_break_seconds: int
    first_activity_at: datetime | None
    last_activity_at: datetime | None
    first_tracked_at: datetime | None
    last_tracked_at: datetime | None
    late_seconds: int
    early_leave_seconds: int
    overtime_seconds: int
    attendance_status: AttendanceStatus
    attendance_credit_seconds: int
    attendance_basis_seconds: int
    attendance_percentage: float | None
    worked_day: bool
    data_quality: DataQuality
    quality_flags: tuple[str, ...]
    is_provisional: bool
    session_count: int
    device_count: int
    calculation_version: int = CALCULATION_VERSION
    policy: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PeriodSummary:
    """Weekly or monthly roll-up of daily summaries."""

    employee_id: str
    period_kind: str  # WEEK | MONTH
    period_start: date
    period_end: date
    timezone: str
    working_days: int
    days_worked: int
    scheduled_seconds: int
    measurable_scheduled_seconds: int
    tracked_seconds: int
    active_seconds: int
    idle_seconds: int
    unknown_seconds: int
    locked_seconds: int
    active_in_shift_seconds: int
    detected_break_seconds: int
    late_seconds: int
    early_leave_seconds: int
    overtime_seconds: int
    attendance_credit_seconds: int
    attendance_basis_seconds: int
    attendance_percentage: float | None
    average_active_seconds_per_worked_day: int | None
    absent_days: int
    late_days: int
    early_leave_days: int
    incomplete_days: int
    worked_day_off_days: int
    pending_days: int
    data_quality: DataQuality
    is_provisional: bool
    calculation_version: int = CALCULATION_VERSION
