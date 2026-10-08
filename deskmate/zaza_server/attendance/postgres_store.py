"""PostgreSQL implementation of :class:`AttendanceStore`.

Reads the Phase 4 typed tables and upserts the Phase 5 summary tables.
Upserts are keyed by (employee_id, local_date / week_start / month) and only
write when the summary's content hash differs, so recalculating unchanged
data leaves ``updated_at`` alone.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, fields
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from psycopg import sql

from ..postgres.repository import PostgresRepository
from .models import (
    AttendanceStatus,
    DailySummary,
    DataQuality,
    DeviceInput,
    EmployeeInfo,
    PeriodInput,
    PeriodSummary,
    ScheduleRule,
    SessionInput,
)
from .store import check_schedule_timezone, summary_hash

_DAILY_FIELDS = [f.name for f in fields(DailySummary)]
_PERIOD_FIELDS = [f.name for f in fields(PeriodSummary) if f.name not in ("period_kind", "period_start",
                                                                           "period_end")]
_PERIOD_TABLES = {
    "WEEK": ("weekly_summaries", "week_start", "week_end"),
    "MONTH": ("monthly_summaries", "month", "month_end"),
}


def _upsert(table: str, key: tuple[str, ...], columns: list[str]) -> sql.Composed:
    return sql.SQL(
        "INSERT INTO {t} ({cols}) VALUES ({vals}) ON CONFLICT ({key}) DO UPDATE SET {sets}, "
        "updated_at = clock_timestamp() WHERE {t}.summary_hash IS DISTINCT FROM EXCLUDED.summary_hash "
        "RETURNING 1"
    ).format(
        t=sql.Identifier(table),
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in columns),
        key=sql.SQL(", ").join(map(sql.Identifier, key)),
        sets=sql.SQL(", ").join(
            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in columns if c not in key
        ),
    )


_DAILY_UPSERT = _upsert("daily_summaries", ("employee_id", "local_date"), _DAILY_FIELDS + ["summary_hash"])


class PostgresAttendanceStore:
    def __init__(self, repo: PostgresRepository) -> None:
        self.repo = repo

    def _rows(self, query, params=()) -> list[dict]:  # noqa: ANN001
        with self.repo.connection() as conn:
            return conn.execute(query, params).fetchall()

    # ── inputs ────────────────────────────────────────────────────────────
    def get_employee(self, employee_id: str) -> EmployeeInfo | None:
        rows = self._rows("SELECT employee_id, timezone, is_active FROM employees WHERE employee_id = %s",
                          (employee_id,))
        return EmployeeInfo(**rows[0]) if rows else None

    def list_employees(self, *, active_only: bool = True) -> list[EmployeeInfo]:
        where = "WHERE is_active" if active_only else ""
        return [EmployeeInfo(**r) for r in self._rows(
            f"SELECT employee_id, timezone, is_active FROM employees {where} ORDER BY employee_id")]

    def schedules(self, employee_id: str) -> list[ScheduleRule]:
        rows = self._rows(
            "SELECT schedule_id::text AS schedule_id, employee_id, is_working_day, timezone, day_of_week, "
            "effective_from, effective_to, schedule_date, start_time, end_time, expected_work_seconds "
            "FROM work_schedules WHERE employee_id = %s ORDER BY schedule_id", (employee_id,))
        return [ScheduleRule(**r) for r in rows]

    def periods(self, employee_id: str, start: datetime, end: datetime) -> list[PeriodInput]:
        rows = self._rows(
            "SELECT period_id::text AS record_id, device_id, started_at, ended_at, status, is_open, record_version "
            "FROM activity_periods WHERE employee_id = %s AND started_at < %s AND ended_at > %s "
            "ORDER BY started_at, period_id", (employee_id, end, start))
        return [PeriodInput(**r) for r in rows]

    def sessions(self, employee_id: str, start: datetime, end: datetime) -> list[SessionInput]:
        rows = self._rows(
            "SELECT session_id::text AS session_id, device_id, status, started_at, ended_at, last_heartbeat_at "
            "FROM work_sessions WHERE employee_id = %s AND started_at < %s "
            "AND COALESCE(ended_at, last_heartbeat_at) >= %s ORDER BY started_at", (employee_id, end, start))
        return [SessionInput(**r) for r in rows]

    def devices(self, employee_id: str) -> list[DeviceInput]:
        rows = self._rows("SELECT device_id, status, last_seen_at FROM devices WHERE employee_id = %s "
                          "ORDER BY device_id", (employee_id,))
        return [DeviceInput(**r) for r in rows]

    # ── daily ─────────────────────────────────────────────────────────────
    def save_daily(self, summary: DailySummary) -> bool:
        params = asdict(summary)
        params.update(
            attendance_status=summary.attendance_status.value,
            data_quality=summary.data_quality.value,
            quality_flags=list(summary.quality_flags),
            policy=json.dumps(summary.policy),
            summary_hash=summary_hash(summary),
        )
        with self.repo.connection() as conn, conn.transaction():
            return conn.execute(_DAILY_UPSERT, params).fetchone() is not None

    @staticmethod
    def _daily(row: dict) -> DailySummary:
        data = {k: row[k] for k in _DAILY_FIELDS}
        data.update(
            schedule_id=str(row["schedule_id"]) if row["schedule_id"] else None,
            attendance_status=AttendanceStatus(row["attendance_status"]),
            data_quality=DataQuality(row["data_quality"]),
            quality_flags=tuple(row["quality_flags"]),
            attendance_percentage=float(row["attendance_percentage"])
            if isinstance(row["attendance_percentage"], Decimal) else row["attendance_percentage"],
        )
        return DailySummary(**data)

    def get_daily(self, employee_id: str, local_date: date) -> DailySummary | None:
        rows = self._rows("SELECT * FROM daily_summaries WHERE employee_id = %s AND local_date = %s",
                          (employee_id, local_date))
        return self._daily(rows[0]) if rows else None

    def list_daily(self, employee_id: str, start: date, end: date) -> list[DailySummary]:
        rows = self._rows("SELECT * FROM daily_summaries WHERE employee_id = %s AND local_date BETWEEN %s AND %s "
                          "ORDER BY local_date", (employee_id, start, end))
        return [self._daily(r) for r in rows]

    # ── weekly / monthly ──────────────────────────────────────────────────
    def save_period(self, summary: PeriodSummary) -> bool:
        table, start_col, end_col = _PERIOD_TABLES[summary.period_kind]
        params = {k: v for k, v in asdict(summary).items() if k in _PERIOD_FIELDS}
        params.update({start_col: summary.period_start, end_col: summary.period_end,
                       "data_quality": summary.data_quality.value, "summary_hash": summary_hash(summary)})
        query = _upsert(table, ("employee_id", start_col), [start_col, end_col, *_PERIOD_FIELDS, "summary_hash"])
        with self.repo.connection() as conn, conn.transaction():
            return conn.execute(query, params).fetchone() is not None

    def get_period(self, employee_id: str, kind: str, start: date) -> PeriodSummary | None:
        table, start_col, end_col = _PERIOD_TABLES[kind]
        rows = self._rows(sql.SQL("SELECT * FROM {} WHERE employee_id = %s AND {} = %s").format(
            sql.Identifier(table), sql.Identifier(start_col)), (employee_id, start))
        return self._period(rows[0], kind) if rows else None

    @staticmethod
    def _period(row: dict, kind: str) -> PeriodSummary:
        """A weekly_summaries / monthly_summaries row as a PeriodSummary."""
        _, start_col, end_col = _PERIOD_TABLES[kind]
        data = {k: row[k] for k in _PERIOD_FIELDS}
        data.update(
            period_kind=kind, period_start=row[start_col], period_end=row[end_col],
            data_quality=DataQuality(row["data_quality"]),
            attendance_percentage=float(row["attendance_percentage"])
            if row["attendance_percentage"] is not None else None,
        )
        return PeriodSummary(**data)

    # ── schedules (admin input; audited) ──────────────────────────────────
    # The ONE schedule write path (CLI and manager dashboard): the database
    # constraints are the validator (overnight shifts, start != end,
    # 0 < expected <= shift span, day-off shape, timezone = employee's), and
    # every change writes its audit row in the same transaction.

    _COLUMNS = ("employee_id", "day_of_week", "effective_from", "effective_to", "schedule_date", "is_working_day",
                "start_time", "end_time", "expected_work_seconds", "timezone")

    def _actor(self, actor: tuple[str, str] | None) -> tuple[str, str | None]:
        return actor or (self.repo.actor_type, self.repo.actor_id)

    @staticmethod
    def _audit(conn, actor: tuple[str, str | None], action: str, schedule_id: str,  # noqa: ANN001
               old: dict | None, new: dict | None) -> None:
        def clean(v: dict | None) -> str | None:
            return json.dumps({k: x for k, x in v.items() if k != "schedule_id"}, default=str) if v else None

        conn.execute(
            "INSERT INTO audit_logs (actor_type, actor_id, action, entity_type, entity_id, old_values, new_values) "
            "VALUES (%s, %s, %s, 'work_schedule', %s, %s::jsonb, %s::jsonb)",
            (actor[0], actor[1], action, schedule_id, clean(old), clean(new)))

    @staticmethod
    def _invalid(exc: Exception) -> ValueError:
        name = getattr(getattr(exc, "diag", None), "constraint_name", None)
        reason = SCHEDULE_ERRORS.get(name or "", "a value is not valid")
        return ValueError(f"invalid schedule: {reason} ({name or 'invalid value'})")

    def get_schedule(self, schedule_id: str) -> ScheduleRule | None:
        try:
            uuid.UUID(str(schedule_id))
        except ValueError:
            return None
        rows = self._rows(
            "SELECT schedule_id::text AS schedule_id, employee_id, is_working_day, timezone, day_of_week, "
            "effective_from, effective_to, schedule_date, start_time, end_time, expected_work_seconds "
            "FROM work_schedules WHERE schedule_id = %s::uuid", (str(schedule_id),))
        return ScheduleRule(**rows[0]) if rows else None

    @staticmethod
    def _insert(conn, values: dict) -> None:  # noqa: ANN001
        conn.execute(
            "INSERT INTO work_schedules (schedule_id, employee_id, day_of_week, effective_from, effective_to, "
            "schedule_date, is_working_day, start_time, end_time, expected_work_seconds, timezone) VALUES "
            "(%(schedule_id)s::uuid, %(employee_id)s, %(day_of_week)s, %(effective_from)s, %(effective_to)s, "
            "%(schedule_date)s, %(is_working_day)s, %(start_time)s, %(end_time)s, "
            "%(expected_work_seconds)s, %(timezone)s)", values)

    def add_schedule(
        self,
        employee_id: str,
        *,
        is_working_day: bool,
        timezone: str | None = None,
        day_of_week: int | None = None,
        effective_from: date | None = None,
        effective_to: date | None = None,
        schedule_date: date | None = None,
        start_time: time | None = None,
        end_time: time | None = None,
        expected_work_seconds: int | None = None,
        actor: tuple[str, str] | None = None,
    ) -> str:
        """Insert one schedule rule (database constraints validate it) and
        write an audit row in the same transaction.

        Phase 5 rule: a schedule's timezone is the employee's reporting
        timezone. ``timezone`` defaults to it; any other zone is rejected
        (and the database enforces the same with
        ``work_schedules_timezone_matches_employee``)."""
        from psycopg import errors  # noqa: PLC0415

        employee = self.get_employee(employee_id)
        if employee is None:
            raise ValueError(f"unknown employee {employee_id!r}")
        timezone = check_schedule_timezone(employee, timezone)
        schedule_id = str(uuid.uuid4())
        values = {
            "schedule_id": schedule_id, "employee_id": employee_id, "day_of_week": day_of_week,
            "effective_from": effective_from, "effective_to": effective_to, "schedule_date": schedule_date,
            "is_working_day": is_working_day, "start_time": start_time, "end_time": end_time,
            "expected_work_seconds": expected_work_seconds, "timezone": timezone,
        }
        try:
            with self.repo.connection() as conn, conn.transaction():
                self._insert(conn, values)
                self._audit(conn, self._actor(actor), "work_schedule.create", schedule_id, None, values)
        except (errors.IntegrityError, errors.DataError) as exc:
            raise self._invalid(exc) from None
        return schedule_id

    def add_schedules(self, employee_id: str, rules: list[dict], *,
                      actor: tuple[str, str] | None = None) -> list[str]:
        """Insert several rules (e.g. one per weekday) all-or-nothing, each
        validated by the database and audited."""
        from psycopg import errors  # noqa: PLC0415

        employee = self.get_employee(employee_id)
        if employee is None:
            raise ValueError(f"unknown employee {employee_id!r}")
        rows = []
        for rule in rules:
            fields = {k: rule.get(k) for k in self._COLUMNS if k not in ("employee_id", "timezone")}
            rows.append({**fields, "schedule_id": str(uuid.uuid4()), "employee_id": employee_id,
                         "timezone": check_schedule_timezone(employee, rule.get("timezone"))})
        try:
            with self.repo.connection() as conn, conn.transaction():
                for values in rows:
                    self._insert(conn, values)
                    self._audit(conn, self._actor(actor), "work_schedule.create", values["schedule_id"], None, values)
        except (errors.IntegrityError, errors.DataError) as exc:
            raise self._invalid(exc) from None
        return [r["schedule_id"] for r in rows]

    def update_schedule(self, schedule_id: str, changes: dict, *,
                        actor: tuple[str, str] | None = None) -> ScheduleRule:
        """Change fields of one rule in place (validated by the database).
        The employee, timezone and rule kind can't be changed."""
        from psycopg import errors  # noqa: PLC0415

        allowed = {"effective_from", "effective_to", "schedule_date", "is_working_day", "start_time", "end_time",
                   "expected_work_seconds", "day_of_week"}
        if set(changes) - allowed:
            raise ValueError(f"these schedule fields can't be changed: {', '.join(sorted(set(changes) - allowed))}")
        old = self.get_schedule(schedule_id)
        if old is None:
            raise ValueError("schedule not found")
        old_values = {k: getattr(old, k) for k in self._COLUMNS}
        new_values = {**old_values, **changes}
        sets = sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in changes)
        try:
            with self.repo.connection() as conn, conn.transaction():
                conn.execute(sql.SQL("UPDATE work_schedules SET {} WHERE schedule_id = %(schedule_id)s::uuid")
                             .format(sets), {**changes, "schedule_id": str(schedule_id)})
                self._audit(conn, self._actor(actor), "work_schedule.update", str(schedule_id),
                            old_values, new_values)
        except (errors.IntegrityError, errors.DataError) as exc:
            raise self._invalid(exc) from None
        return self.get_schedule(schedule_id)

    def split_schedule(self, schedule_id: str, from_date: date, changes: dict, *,
                       actor: tuple[str, str] | None = None) -> str:
        """For a weekly rule: end it on ``from_date - 1`` and start a new rule
        with ``changes`` on ``from_date`` (until the old rule's end), in one
        transaction. Days before ``from_date`` keep the old rule."""
        from psycopg import errors  # noqa: PLC0415

        old = self.get_schedule(schedule_id)
        if old is None or old.day_of_week is None:
            raise ValueError("only a weekly rule can be changed from a date")
        if not old.effective_from < from_date <= (old.effective_to or date.max):
            raise ValueError("the change date must be after the rule's start and within its effective range")
        old_values = {k: getattr(old, k) for k in self._COLUMNS}
        new_id = str(uuid.uuid4())
        ended = {**old_values, "effective_to": from_date - timedelta(days=1)}
        new_values = {**old_values, **changes, "schedule_id": new_id, "effective_from": from_date,
                      "effective_to": old.effective_to}
        try:
            with self.repo.connection() as conn, conn.transaction():
                conn.execute("UPDATE work_schedules SET effective_to = %s WHERE schedule_id = %s::uuid",
                             (ended["effective_to"], str(schedule_id)))
                self._audit(conn, self._actor(actor), "work_schedule.update", str(schedule_id), old_values, ended)
                self._insert(conn, new_values)
                self._audit(conn, self._actor(actor), "work_schedule.create", new_id, None, new_values)
        except (errors.IntegrityError, errors.DataError) as exc:
            raise self._invalid(exc) from None
        return new_id

    def delete_schedule(self, schedule_id: str, *, actor: tuple[str, str] | None = None) -> ScheduleRule:
        old = self.get_schedule(schedule_id)
        if old is None:
            raise ValueError("schedule not found")
        with self.repo.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM work_schedules WHERE schedule_id = %s::uuid", (str(schedule_id),))
            self._audit(conn, self._actor(actor), "work_schedule.delete", str(schedule_id),
                        {k: getattr(old, k) for k in self._COLUMNS}, None)
        return old


# Friendly reasons for the schedule constraints (the database stays the validator).
SCHEDULE_ERRORS = {
    "work_schedules_hours_valid": "a working day needs a start and an end that differ, and expected hours "
                                  "above 0 and no longer than the shift; a day off has no hours",
    "work_schedules_kind_valid": "a rule is either weekly (weekday + effective from) or for one date",
    "work_schedules_day_of_week_valid": "weekday must be 1 (Monday) to 7 (Sunday)",
    "work_schedules_effective_range": "'effective to' can't be before 'effective from'",
    "work_schedules_timezone_matches_employee": "the schedule must use the employee's timezone",
    "work_schedules_employee_fk": "unknown employee",
    "work_schedules_weekly_unique": "a rule for that weekday already starts on that date",
    "work_schedules_date_unique": "there is already a rule for that date",
}
