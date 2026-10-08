"""Storage boundary for attendance calculations.

:class:`AttendanceStore` reads the synced inputs and writes summaries.
:class:`InMemoryAttendanceStore` backs the unit tests;
:class:`~deskmate.zaza_server.attendance.postgres_store.PostgresAttendanceStore`
is production. Saving is an idempotent upsert keyed by
(employee_id, local_date) / (employee_id, week_start) / (employee_id, month):
an unchanged result writes nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import date, datetime
from typing import Protocol

from .models import (
    DailySummary,
    DeviceInput,
    EmployeeInfo,
    PeriodInput,
    PeriodSummary,
    ScheduleRule,
    SessionInput,
)


def summary_hash(summary: DailySummary | PeriodSummary) -> str:
    data = asdict(summary)
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class AttendanceStore(Protocol):
    def get_employee(self, employee_id: str) -> EmployeeInfo | None: ...
    def list_employees(self, *, active_only: bool = True) -> list[EmployeeInfo]: ...
    def schedules(self, employee_id: str) -> list[ScheduleRule]: ...
    def periods(self, employee_id: str, start: datetime, end: datetime) -> list[PeriodInput]: ...
    def sessions(self, employee_id: str, start: datetime, end: datetime) -> list[SessionInput]: ...
    def devices(self, employee_id: str) -> list[DeviceInput]: ...

    def save_daily(self, summary: DailySummary) -> bool:
        """Upsert; True if the stored row was created or changed."""
    def get_daily(self, employee_id: str, local_date: date) -> DailySummary | None: ...
    def list_daily(self, employee_id: str, start: date, end: date) -> list[DailySummary]: ...
    def save_period(self, summary: PeriodSummary) -> bool: ...
    def get_period(self, employee_id: str, kind: str, start: date) -> PeriodSummary | None: ...


class InMemoryAttendanceStore:
    def __init__(self) -> None:
        self.employees: dict[str, EmployeeInfo] = {}
        self.rules: list[ScheduleRule] = []
        self.period_inputs: dict[str, PeriodInput] = {}  # by record_id (higher version replaces)
        self.session_inputs: dict[str, SessionInput] = {}
        self.device_inputs: dict[str, tuple[str, DeviceInput]] = {}  # device_id -> (employee, device)
        self.daily: dict[tuple[str, date], tuple[str, DailySummary]] = {}
        self.period_summaries: dict[tuple[str, str, date], tuple[str, PeriodSummary]] = {}
        self.writes = 0
        self._employee_of_period: dict[str, str] = {}
        self._employee_of_session: dict[str, str] = {}

    # ── test setup helpers ────────────────────────────────────────────────
    def add_employee(self, employee_id: str, timezone: str, *, is_active: bool = True) -> None:
        self.employees[employee_id] = EmployeeInfo(employee_id, timezone, is_active)

    def add_rule(self, rule: ScheduleRule) -> None:
        self.rules.append(rule)

    def add_device(self, employee_id: str, device: DeviceInput) -> None:
        self.device_inputs[device.device_id] = (employee_id, device)

    def set_last_seen(self, device_id: str, last_seen_at: datetime | None) -> None:
        employee_id, device = self.device_inputs[device_id]
        self.device_inputs[device_id] = (employee_id, replace(device, last_seen_at=last_seen_at))

    def add_period(self, employee_id: str, period: PeriodInput) -> None:
        current = self.period_inputs.get(period.record_id)
        if current is None or period.record_version > current.record_version:
            self.period_inputs[period.record_id] = period
            self._employee_of_period[period.record_id] = employee_id

    def add_session(self, employee_id: str, session: SessionInput) -> None:
        self.session_inputs[session.session_id] = session
        self._employee_of_session[session.session_id] = employee_id

    # ── AttendanceStore ───────────────────────────────────────────────────
    def get_employee(self, employee_id: str) -> EmployeeInfo | None:
        return self.employees.get(employee_id)

    def list_employees(self, *, active_only: bool = True) -> list[EmployeeInfo]:
        return sorted((e for e in self.employees.values() if e.is_active or not active_only),
                      key=lambda e: e.employee_id)

    def schedules(self, employee_id: str) -> list[ScheduleRule]:
        return [r for r in self.rules if r.employee_id == employee_id]

    def periods(self, employee_id: str, start: datetime, end: datetime) -> list[PeriodInput]:
        return sorted(
            (p for rid, p in self.period_inputs.items()
             if self._employee_of_period[rid] == employee_id and p.ended_at > start and p.started_at < end),
            key=lambda p: (p.started_at, p.record_id),
        )

    def sessions(self, employee_id: str, start: datetime, end: datetime) -> list[SessionInput]:
        return [s for sid, s in self.session_inputs.items()
                if self._employee_of_session[sid] == employee_id and s.effective_end >= start
                and s.started_at < end]

    def devices(self, employee_id: str) -> list[DeviceInput]:
        return [d for e, d in self.device_inputs.values() if e == employee_id]

    def save_daily(self, summary: DailySummary) -> bool:
        key = (summary.employee_id, summary.local_date)
        digest = summary_hash(summary)
        if key in self.daily and self.daily[key][0] == digest:
            return False
        self.daily[key] = (digest, summary)
        self.writes += 1
        return True

    def get_daily(self, employee_id: str, local_date: date) -> DailySummary | None:
        found = self.daily.get((employee_id, local_date))
        return found[1] if found else None

    def list_daily(self, employee_id: str, start: date, end: date) -> list[DailySummary]:
        return sorted((s for (e, d), (_, s) in self.daily.items() if e == employee_id and start <= d <= end),
                      key=lambda s: s.local_date)

    def save_period(self, summary: PeriodSummary) -> bool:
        key = (summary.employee_id, summary.period_kind, summary.period_start)
        digest = summary_hash(summary)
        if key in self.period_summaries and self.period_summaries[key][0] == digest:
            return False
        self.period_summaries[key] = (digest, summary)
        self.writes += 1
        return True

    def get_period(self, employee_id: str, kind: str, start: date) -> PeriodSummary | None:
        found = self.period_summaries.get((employee_id, kind, start))
        return found[1] if found else None
