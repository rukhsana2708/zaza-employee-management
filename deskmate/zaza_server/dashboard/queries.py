"""Data access for the manager dashboard.

:class:`PostgresDashboardRepository` reads PostgreSQL directly — never Google
Sheets. Report reads select display columns only (no payloads, content
hashes, record versions or token data). Schedule writes go through the
single Phase 5 schedule path (``PostgresAttendanceStore``), where the
database constraints validate every rule.

:class:`InMemoryDashboardRepository` implements the same interface for the
automated tests. It stores schedule changes without validating them — the
database is the validator — so schedule *validation* is tested on PostgreSQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from ..attendance.models import DailySummary, ScheduleRule
from .models import AppUsage, Device, Employee, ManagerSession, ManagerUser, Period

UTC = timezone.utc


class DuplicateUsername(ValueError):
    pass


class DashboardRepository(Protocol):
    # reports (read-only)
    def employees(self) -> list[Employee]: ...
    def devices(self) -> list[Device]: ...
    def latest_periods(self, since: datetime) -> list[Period]: ...
    def daily(self, employee_ids: list[str], start: date, end: date) -> list[DailySummary]: ...
    def summaries_updated_at(self) -> datetime | None: ...
    def app_usage(self, employee_ids: list[str], start: date, end: date) -> list[AppUsage]: ...
    def activity(self, employee_id: str, start: datetime, end: datetime, limit: int) -> list[Period]: ...
    def schedules(self, employee_ids: list[str]) -> list[ScheduleRule]: ...
    # schedules (the Phase 5 write path)
    def get_schedule(self, schedule_id: str) -> ScheduleRule | None: ...
    def add_schedules(self, employee_id: str, rules: list[dict], *, actor: tuple[str, str]) -> list[str]: ...
    def update_schedule(self, schedule_id: str, changes: dict, *, actor: tuple[str, str]) -> ScheduleRule: ...
    def split_schedule(self, schedule_id: str, from_date: date, changes: dict, *, actor: tuple[str, str]) -> str: ...
    def delete_schedule(self, schedule_id: str, *, actor: tuple[str, str]) -> ScheduleRule: ...
    # managers
    def user_by_username(self, username: str) -> ManagerUser | None: ...
    def users(self) -> list[ManagerUser]: ...
    def create_user(self, username: str, display_name: str, password_hash: str, role: str) -> ManagerUser: ...
    def update_user(self, manager_user_id: str, **fields) -> None: ...  # noqa: ANN003
    def create_session(self, session: ManagerSession) -> None: ...
    def session_by_hash(self, token_hash: str) -> tuple[ManagerSession, ManagerUser] | None: ...
    def touch_session(self, session_id: str, at: datetime) -> None: ...
    def revoke_session(self, session_id: str, at: datetime) -> None: ...
    def revoke_user_sessions(self, manager_user_id: str, at: datetime) -> int: ...
    def audit(self, actor: tuple[str, str | None], action: str, entity_type: str, entity_id: str,
              old: dict | None = None, new: dict | None = None) -> None: ...


# ─── PostgreSQL ───────────────────────────────────────────────────────────

_PERIOD_COLUMNS = ("period_id::text AS period_id, employee_id, device_id, started_at, ended_at, duration_seconds, "
                   "status, is_open, app_name, window_title, domain, privacy_excluded")
_USER_COLUMNS = ("manager_user_id::text AS manager_user_id, username, display_name, password_hash, role, "
                 "is_active, created_at, last_login_at")


class PostgresDashboardRepository:
    def __init__(self, repo) -> None:  # noqa: ANN001 — PostgresRepository
        from ..attendance.postgres_store import PostgresAttendanceStore  # noqa: PLC0415

        self.repo = repo
        self.store = PostgresAttendanceStore(repo)

    def _rows(self, query: str, params: Iterable = ()) -> list[dict]:
        with self.repo.connection() as conn:
            return conn.execute(query, tuple(params)).fetchall()

    def _write(self, query: str, params: Iterable = ()) -> int:
        with self.repo.connection() as conn, conn.transaction():
            return conn.execute(query, tuple(params)).rowcount

    # reports
    def employees(self) -> list[Employee]:
        return [Employee(**r) for r in self._rows(
            "SELECT employee_id, display_name, timezone, is_active FROM employees ORDER BY employee_id")]

    def devices(self) -> list[Device]:
        return [Device(**r) for r in self._rows(
            "SELECT device_id, employee_id, status, last_seen_at FROM devices ORDER BY device_id")]

    def latest_periods(self, since: datetime) -> list[Period]:
        return [Period(**r) for r in self._rows(
            f"SELECT DISTINCT ON (device_id) {_PERIOD_COLUMNS} FROM activity_periods WHERE ended_at >= %s "
            "ORDER BY device_id, ended_at DESC, started_at DESC, period_id", (since,))]

    def daily(self, employee_ids: list[str], start: date, end: date) -> list[DailySummary]:
        rows = self._rows("SELECT * FROM daily_summaries WHERE employee_id = ANY(%s) AND local_date BETWEEN %s AND %s "
                          "ORDER BY local_date, employee_id", (list(employee_ids), start, end))
        return [self.store._daily(r) for r in rows]

    def summaries_updated_at(self) -> datetime | None:
        return self._rows("SELECT max(updated_at) AS t FROM daily_summaries")[0]["t"]

    def app_usage(self, employee_ids: list[str], start: date, end: date) -> list[AppUsage]:
        return [AppUsage(**r) for r in self._rows(
            "SELECT employee_id, usage_date, app_name, sum(active_seconds) AS active_seconds, "
            "sum(idle_seconds) AS idle_seconds, sum(unknown_seconds) AS unknown_seconds "
            "FROM application_usage_daily WHERE employee_id = ANY(%s) AND usage_date BETWEEN %s AND %s "
            "GROUP BY employee_id, usage_date, app_name ORDER BY employee_id, usage_date, app_name",
            (list(employee_ids), start, end))]

    def activity(self, employee_id: str, start: datetime, end: datetime, limit: int) -> list[Period]:
        return [Period(**r) for r in self._rows(
            f"SELECT {_PERIOD_COLUMNS} FROM activity_periods WHERE employee_id = %s AND ended_at > %s "
            "AND started_at < %s ORDER BY started_at DESC, period_id LIMIT %s", (employee_id, start, end, limit))]

    def schedules(self, employee_ids: list[str]) -> list[ScheduleRule]:
        return [ScheduleRule(**r) for r in self._rows(
            "SELECT schedule_id::text AS schedule_id, employee_id, is_working_day, timezone, day_of_week, "
            "effective_from, effective_to, schedule_date, start_time, end_time, expected_work_seconds "
            "FROM work_schedules WHERE employee_id = ANY(%s) "
            "ORDER BY employee_id, schedule_date NULLS FIRST, day_of_week, effective_from", (list(employee_ids),))]

    # schedules: the single Phase 5 write path
    def get_schedule(self, schedule_id: str) -> ScheduleRule | None:
        return self.store.get_schedule(schedule_id)

    def add_schedules(self, employee_id: str, rules: list[dict], *, actor: tuple[str, str]) -> list[str]:
        return self.store.add_schedules(employee_id, rules, actor=actor)

    def update_schedule(self, schedule_id: str, changes: dict, *, actor: tuple[str, str]) -> ScheduleRule:
        return self.store.update_schedule(schedule_id, changes, actor=actor)

    def split_schedule(self, schedule_id: str, from_date: date, changes: dict, *, actor: tuple[str, str]) -> str:
        return self.store.split_schedule(schedule_id, from_date, changes, actor=actor)

    def delete_schedule(self, schedule_id: str, *, actor: tuple[str, str]) -> ScheduleRule:
        return self.store.delete_schedule(schedule_id, actor=actor)

    # managers
    def user_by_username(self, username: str) -> ManagerUser | None:
        rows = self._rows(f"SELECT {_USER_COLUMNS} FROM manager_users WHERE lower(username) = lower(%s)", (username,))
        return ManagerUser(**rows[0]) if rows else None

    def users(self) -> list[ManagerUser]:
        return [ManagerUser(**r) for r in self._rows(f"SELECT {_USER_COLUMNS} FROM manager_users ORDER BY username")]

    def create_user(self, username: str, display_name: str, password_hash: str, role: str) -> ManagerUser:
        from psycopg import errors  # noqa: PLC0415

        try:
            rows = self._rows_tx(
                f"INSERT INTO manager_users (username, display_name, password_hash, role) VALUES (%s, %s, %s, %s) "
                f"RETURNING {_USER_COLUMNS}", (username, display_name, password_hash, role))
        except errors.UniqueViolation:
            raise DuplicateUsername("that username is already taken") from None
        except errors.CheckViolation as exc:
            raise ValueError(f"invalid manager account ({exc.diag.constraint_name})") from None
        return ManagerUser(**rows[0])

    def _rows_tx(self, query: str, params: Iterable = ()) -> list[dict]:
        with self.repo.connection() as conn, conn.transaction():
            return conn.execute(query, tuple(params)).fetchall()

    def update_user(self, manager_user_id: str, **fields) -> None:  # noqa: ANN003
        allowed = {"password_hash", "is_active", "last_login_at", "display_name", "role"}
        if not fields or not set(fields) <= allowed:  # column names come only from this list
            raise ValueError("unsupported manager account fields")
        sets = ", ".join(f"{k} = %s" for k in fields)
        self._write(f"UPDATE manager_users SET {sets} WHERE manager_user_id = %s::uuid",
                    (*fields.values(), manager_user_id))

    def create_session(self, session: ManagerSession) -> None:
        self._write(
            "INSERT INTO manager_sessions (session_id, manager_user_id, session_token_hash, csrf_token_hash, "
            "created_at, expires_at, last_seen_at) VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s)",
            (session.session_id, session.manager_user_id, session.session_token_hash, session.csrf_token_hash,
             session.created_at, session.expires_at, session.last_seen_at))

    def session_by_hash(self, token_hash: str) -> tuple[ManagerSession, ManagerUser] | None:
        rows = self._rows(
            "SELECT s.session_id::text AS session_id, s.manager_user_id::text AS s_user, s.session_token_hash, "
            "s.csrf_token_hash, s.created_at AS s_created, s.expires_at, s.last_seen_at, s.revoked_at, "
            f"{', '.join('u.' + c for c in _USER_COLUMNS.split(', '))} "
            "FROM manager_sessions s JOIN manager_users u USING (manager_user_id) WHERE s.session_token_hash = %s",
            (token_hash,))
        if not rows:
            return None
        r = rows[0]
        session = ManagerSession(r["session_id"], r["s_user"], r["session_token_hash"], r["csrf_token_hash"],
                                 r["s_created"], r["expires_at"], r["last_seen_at"], r["revoked_at"])
        user = ManagerUser(r["manager_user_id"], r["username"], r["display_name"], r["password_hash"], r["role"],
                           r["is_active"], r["created_at"], r["last_login_at"])
        return session, user

    def touch_session(self, session_id: str, at: datetime) -> None:
        self._write("UPDATE manager_sessions SET last_seen_at = %s WHERE session_id = %s::uuid", (at, session_id))

    def revoke_session(self, session_id: str, at: datetime) -> None:
        self._write("UPDATE manager_sessions SET revoked_at = %s WHERE session_id = %s::uuid AND revoked_at IS NULL",
                    (at, session_id))

    def revoke_user_sessions(self, manager_user_id: str, at: datetime) -> int:
        return self._write("UPDATE manager_sessions SET revoked_at = %s WHERE manager_user_id = %s::uuid "
                           "AND revoked_at IS NULL AND expires_at > %s", (at, manager_user_id, at))

    def audit(self, actor: tuple[str, str | None], action: str, entity_type: str, entity_id: str,
              old: dict | None = None, new: dict | None = None) -> None:
        import json  # noqa: PLC0415

        self._write(
            "INSERT INTO audit_logs (actor_type, actor_id, action, entity_type, entity_id, old_values, new_values) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)",
            (actor[0], actor[1], action, entity_type, entity_id,
             json.dumps(old, default=str) if old is not None else None,
             json.dumps(new, default=str) if new is not None else None))


# ─── in memory (tests) ────────────────────────────────────────────────────


class InMemoryDashboardRepository:
    def __init__(self) -> None:
        self.employee_rows: dict[str, Employee] = {}
        self.device_rows: dict[str, Device] = {}
        self.periods: list[Period] = []
        self.summaries: dict[tuple[str, date], DailySummary] = {}
        self.usage: list[AppUsage] = []
        self.rules: dict[str, ScheduleRule] = {}
        self.updated_at: datetime | None = None
        self.user_rows: dict[str, ManagerUser] = {}
        self.session_rows: dict[str, ManagerSession] = {}
        self.audit_rows: list[dict] = []

    # setup helpers
    def add_employee(self, employee: Employee) -> None:
        self.employee_rows[employee.employee_id] = employee

    def add_device(self, device: Device) -> None:
        self.device_rows[device.device_id] = device

    def add_summary(self, summary: DailySummary) -> None:
        self.summaries[(summary.employee_id, summary.local_date)] = summary

    # reports
    def employees(self) -> list[Employee]:
        return sorted(self.employee_rows.values(), key=lambda e: e.employee_id)

    def devices(self) -> list[Device]:
        return sorted(self.device_rows.values(), key=lambda d: d.device_id)

    def latest_periods(self, since: datetime) -> list[Period]:
        latest: dict[str, Period] = {}
        for p in sorted(self.periods, key=lambda p: (p.ended_at, p.started_at)):
            if p.ended_at >= since:
                latest[p.device_id] = p
        return list(latest.values())

    def daily(self, employee_ids: list[str], start: date, end: date) -> list[DailySummary]:
        return sorted((s for (e, d), s in self.summaries.items() if e in employee_ids and start <= d <= end),
                      key=lambda s: (s.local_date, s.employee_id))

    def summaries_updated_at(self) -> datetime | None:
        return self.updated_at

    def app_usage(self, employee_ids: list[str], start: date, end: date) -> list[AppUsage]:
        return [u for u in self.usage if u.employee_id in employee_ids and start <= u.usage_date <= end]

    def activity(self, employee_id: str, start: datetime, end: datetime, limit: int) -> list[Period]:
        rows = [p for p in self.periods if p.employee_id == employee_id and p.ended_at > start and p.started_at < end]
        rows.sort(key=lambda p: p.period_id)
        rows.sort(key=lambda p: p.started_at, reverse=True)
        return rows[:limit]

    def schedules(self, employee_ids: list[str]) -> list[ScheduleRule]:
        return [r for r in self.rules.values() if r.employee_id in employee_ids]

    # schedules (no validation here: on PostgreSQL the database validates)
    def get_schedule(self, schedule_id: str) -> ScheduleRule | None:
        return self.rules.get(schedule_id)

    def _audit_rule(self, actor, action, rule_id, old, new) -> None:  # noqa: ANN001
        self.audit(actor, action, "work_schedule", rule_id, old, new)

    def add_schedules(self, employee_id: str, rules: list[dict], *, actor: tuple[str, str]) -> list[str]:
        ids = []
        for rule in rules:
            rid = str(uuid.uuid4())
            tz = self.employee_rows[employee_id].timezone
            self.rules[rid] = ScheduleRule(rid, employee_id, rule["is_working_day"], tz, **{
                k: rule.get(k) for k in ("day_of_week", "effective_from", "effective_to", "schedule_date",
                                         "start_time", "end_time", "expected_work_seconds")})
            self._audit_rule(actor, "work_schedule.create", rid, None, rule)
            ids.append(rid)
        return ids

    def update_schedule(self, schedule_id: str, changes: dict, *, actor: tuple[str, str]) -> ScheduleRule:
        old = self.rules[schedule_id]
        self.rules[schedule_id] = replace(old, **changes)
        self._audit_rule(actor, "work_schedule.update", schedule_id, vars(old), changes)
        return self.rules[schedule_id]

    def split_schedule(self, schedule_id: str, from_date: date, changes: dict, *, actor: tuple[str, str]) -> str:
        old = self.rules[schedule_id]
        self.update_schedule(schedule_id, {"effective_to": from_date - timedelta(days=1)}, actor=actor)
        rid = str(uuid.uuid4())
        self.rules[rid] = replace(old, schedule_id=rid, effective_from=from_date, **changes)
        self._audit_rule(actor, "work_schedule.create", rid, None, changes)
        return rid

    def delete_schedule(self, schedule_id: str, *, actor: tuple[str, str]) -> ScheduleRule:
        old = self.rules.pop(schedule_id)
        self._audit_rule(actor, "work_schedule.delete", schedule_id, vars(old), None)
        return old

    # managers
    def user_by_username(self, username: str) -> ManagerUser | None:
        return next((u for u in self.user_rows.values() if u.username == username.lower()), None)

    def users(self) -> list[ManagerUser]:
        return sorted(self.user_rows.values(), key=lambda u: u.username)

    def create_user(self, username: str, display_name: str, password_hash: str, role: str) -> ManagerUser:
        if self.user_by_username(username) is not None:
            raise DuplicateUsername("that username is already taken")
        if not password_hash.startswith("$argon2id$"):
            raise ValueError("invalid manager account (manager_users_password_hash_argon2id)")
        user = ManagerUser(str(uuid.uuid4()), username, display_name, password_hash, role, True,
                           datetime.now(UTC), None)
        self.user_rows[user.manager_user_id] = user
        return user

    def update_user(self, manager_user_id: str, **fields) -> None:  # noqa: ANN003
        self.user_rows[manager_user_id] = replace(self.user_rows[manager_user_id], **fields)

    def create_session(self, session: ManagerSession) -> None:
        self.session_rows[session.session_id] = session

    def session_by_hash(self, token_hash: str) -> tuple[ManagerSession, ManagerUser] | None:
        s = next((s for s in self.session_rows.values() if s.session_token_hash == token_hash), None)
        return (s, self.user_rows[s.manager_user_id]) if s else None

    def touch_session(self, session_id: str, at: datetime) -> None:
        self.session_rows[session_id] = replace(self.session_rows[session_id], last_seen_at=at)

    def revoke_session(self, session_id: str, at: datetime) -> None:
        s = self.session_rows[session_id]
        if s.revoked_at is None:
            self.session_rows[session_id] = replace(s, revoked_at=at)

    def revoke_user_sessions(self, manager_user_id: str, at: datetime) -> int:
        n = 0
        for sid, s in list(self.session_rows.items()):
            if s.manager_user_id == manager_user_id and s.revoked_at is None and s.expires_at > at:
                self.session_rows[sid] = replace(s, revoked_at=at)
                n += 1
        return n

    def audit(self, actor: tuple[str, str | None], action: str, entity_type: str, entity_id: str,
              old: dict | None = None, new: dict | None = None) -> None:
        self.audit_rows.append({"actor_type": actor[0], "actor_id": actor[1], "action": action,
                                "entity_type": entity_type, "entity_id": entity_id, "old_values": old,
                                "new_values": new})
