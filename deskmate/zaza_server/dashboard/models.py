"""Plain data for the manager dashboard (no SQL, no HTML)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime

from ..attendance.models import DailySummary


class Role(str, enum.Enum):
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"


@dataclass(frozen=True)
class ManagerUser:
    manager_user_id: str
    username: str
    display_name: str
    password_hash: str = field(repr=False)
    role: str = "MANAGER"
    is_active: bool = True
    created_at: datetime | None = None
    last_login_at: datetime | None = None


@dataclass(frozen=True)
class ManagerSession:
    session_id: str
    manager_user_id: str
    session_token_hash: str = field(repr=False)
    csrf_token_hash: str = field(repr=False)
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class Employee:
    employee_id: str
    display_name: str
    timezone: str
    is_active: bool = True


@dataclass(frozen=True)
class Device:
    device_id: str
    employee_id: str
    status: str  # ACTIVE | DISABLED
    last_seen_at: datetime | None


@dataclass(frozen=True)
class Period:
    """One synced activity period (display fields only)."""

    period_id: str
    employee_id: str
    device_id: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    status: str
    is_open: bool = False
    app_name: str | None = None
    window_title: str | None = None
    domain: str | None = None
    privacy_excluded: bool = False


@dataclass(frozen=True)
class AppUsage:
    """One application_usage_daily row (the device's calendar date)."""

    employee_id: str
    usage_date: date
    app_name: str
    active_seconds: float
    idle_seconds: float
    unknown_seconds: float


# ─── current status ───────────────────────────────────────────────────────


class CurrentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    LOCKED = "LOCKED"
    UNKNOWN = "UNKNOWN"
    ONLINE_NO_ACTIVITY = "ONLINE_NO_ACTIVITY"
    OFFLINE = "OFFLINE"
    NO_DEVICE = "NO_DEVICE"


# Several devices: the employee shows the highest of their devices' states.
STATUS_PRIORITY = {
    CurrentStatus.ACTIVE: 6, CurrentStatus.IDLE: 5, CurrentStatus.LOCKED: 4, CurrentStatus.UNKNOWN: 3,
    CurrentStatus.ONLINE_NO_ACTIVITY: 2, CurrentStatus.OFFLINE: 1, CurrentStatus.NO_DEVICE: 0,
}

STATUS_LABELS = {
    CurrentStatus.ACTIVE: "Active",
    CurrentStatus.IDLE: "Idle",
    CurrentStatus.LOCKED: "Locked",
    CurrentStatus.UNKNOWN: "Unknown (monitoring uncertain)",
    CurrentStatus.ONLINE_NO_ACTIVITY: "Online, no recent activity",
    CurrentStatus.OFFLINE: "Offline",
    CurrentStatus.NO_DEVICE: "No enabled device",
}


@dataclass(frozen=True)
class EmployeeStatus:
    employee_id: str
    status: CurrentStatus
    last_seen_at: datetime | None
    last_period_end: datetime | None
    online_devices: int
    enabled_devices: int

    @property
    def label(self) -> str:
        return STATUS_LABELS[self.status]


# ─── period selection and totals ──────────────────────────────────────────


@dataclass(frozen=True)
class EmployeeRange:
    employee: Employee
    start: date
    end: date


@dataclass(frozen=True)
class Selection:
    """The resolved period filter: one local date range per employee."""

    kind: str
    ranges: tuple[EmployeeRange, ...]
    custom_from: date | None = None
    custom_to: date | None = None
    employee_id: str | None = None  # None = all employees

    @property
    def uniform(self) -> bool:
        return len({(r.start, r.end) for r in self.ranges}) <= 1


@dataclass
class Totals:
    """Sums of stored Phase 5 daily summaries (never recalculated)."""

    scheduled: int = 0
    tracked: int = 0
    active: int = 0
    idle: int = 0
    unknown: int = 0
    locked: int = 0
    overtime: int = 0
    detected_break: int = 0
    late_seconds: int = 0
    early_leave_seconds: int = 0
    credit: int = 0
    basis: int = 0
    days: int = 0
    days_worked: int = 0
    late_days: int = 0
    early_leave_days: int = 0
    absent_days: int = 0
    incomplete_days: int = 0
    # Distinct employees, counted by employee_id (names are not unique); the
    # *_employees lists are the matching display labels for the UI.
    late_employee_ids: list[str] = field(default_factory=list)
    absent_employee_ids: list[str] = field(default_factory=list)
    incomplete_employee_ids: list[str] = field(default_factory=list)
    late_employees: list[str] = field(default_factory=list)
    absent_employees: list[str] = field(default_factory=list)
    incomplete_employees: list[str] = field(default_factory=list)

    @property
    def attendance_fraction(self) -> float | None:
        """Σ credit ÷ Σ basis (ARCHITECTURE.md §4.11.7) — never an average of percentages."""
        return self.credit / self.basis if self.basis > 0 else None


@dataclass(frozen=True)
class DayRow:
    employee: Employee
    summary: DailySummary
