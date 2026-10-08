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
from datetime import date, datetime, time
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
from .store import summary_hash

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
        if not rows:
            return None
        row = rows[0]
        data = {k: row[k] for k in _PERIOD_FIELDS}
        data.update(
            period_kind=kind, period_start=row[start_col], period_end=row[end_col],
            data_quality=DataQuality(row["data_quality"]),
            attendance_percentage=float(row["attendance_percentage"])
            if row["attendance_percentage"] is not None else None,
        )
        return PeriodSummary(**data)

    # ── schedules (admin input; audited) ──────────────────────────────────
    def add_schedule(
        self,
        employee_id: str,
        *,
        timezone: str,
        is_working_day: bool,
        day_of_week: int | None = None,
        effective_from: date | None = None,
        effective_to: date | None = None,
        schedule_date: date | None = None,
        start_time: time | None = None,
        end_time: time | None = None,
        expected_work_seconds: int | None = None,
    ) -> str:
        """Insert one schedule rule (database constraints validate it) and
        write an audit row in the same transaction."""
        from psycopg import errors  # noqa: PLC0415

        schedule_id = str(uuid.uuid4())
        values = {
            "schedule_id": schedule_id, "employee_id": employee_id, "day_of_week": day_of_week,
            "effective_from": effective_from, "effective_to": effective_to, "schedule_date": schedule_date,
            "is_working_day": is_working_day, "start_time": start_time, "end_time": end_time,
            "expected_work_seconds": expected_work_seconds, "timezone": timezone,
        }
        try:
            with self.repo.connection() as conn, conn.transaction():
                conn.execute(
                    "INSERT INTO work_schedules (schedule_id, employee_id, day_of_week, effective_from, effective_to, "
                    "schedule_date, is_working_day, start_time, end_time, expected_work_seconds, timezone) VALUES "
                    "(%(schedule_id)s::uuid, %(employee_id)s, %(day_of_week)s, %(effective_from)s, %(effective_to)s, "
                    "%(schedule_date)s, %(is_working_day)s, %(start_time)s, %(end_time)s, "
                    "%(expected_work_seconds)s, %(timezone)s)", values)
                conn.execute(
                    "INSERT INTO audit_logs (actor_type, actor_id, action, entity_type, entity_id, new_values) "
                    "VALUES (%s, %s, 'work_schedule.create', 'work_schedule', %s, %s::jsonb)",
                    (self.repo.actor_type, self.repo.actor_id, schedule_id,
                     json.dumps({k: v for k, v in values.items() if k != "schedule_id"}, default=str)),
                )
        except (errors.IntegrityError, errors.DataError) as exc:
            name = getattr(exc.diag, "constraint_name", None)
            raise ValueError(f"invalid schedule ({name or 'invalid value'})") from None
        return schedule_id
