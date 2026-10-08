"""Central persistence boundary.

:class:`CentralRepository` is the one interface the service layer talks to.
Implementations:

- :class:`InMemoryRepository` — tests.
- :class:`SqliteDevRepository` — local development; deliberately simple
  (one generic JSON table, no production migrations).
- :class:`deskmate.zaza_server.postgres.PostgresRepository` — production:
  typed, constrained tables managed by Alembic migrations.

Idempotency lives here, in :func:`decide`, and is shared by every backend:

- unknown (type, id)                  -> ``accepted`` (insert)
- same version, same content          -> ``already_current`` (no-op)
- same version, different content     -> ``conflict`` (server keeps its copy)
- higher version                      -> ``updated`` (replace)
- lower version                       -> ``stale`` (server keeps the newer one)
- id owned by a different device      -> ``rejected``

Every backend also requires a period's ``session_id`` to name a work
session already synced *by the same device* (PostgreSQL enforces this with
a composite foreign key; the dev backends check it in code). The agent
always sends a session before its periods, so this only rejects orphans.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RepositoryUnavailable(Exception):
    """The backing store can't be reached right now (the API answers 503 so
    agents back off and retry). Messages never contain credentials."""


EMPLOYEE_ROLES = ("EMPLOYEE", "MANAGER", "ADMIN")
ORPHAN_PERIOD_ERROR = "session_id is not a synced work_session of this device"
_SESSION_CHILD_TYPES = frozenset({"activity_period", "idle_period"})


@dataclass(frozen=True)
class EmployeeRecord:
    employee_id: str
    display_name: str
    role: str  # EMPLOYEE | MANAGER | ADMIN
    is_active: bool
    timezone: str
    created_at: str


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    employee_id: str
    status: str  # ACTIVE | DISABLED
    created_at: str
    display_name: str | None = None
    last_seen_at: str | None = None


@dataclass(frozen=True)
class TokenRecord:
    token_id: str
    device_id: str
    token_hash: str
    status: str  # ACTIVE | REVOKED
    created_at: str
    revoked_at: str | None = None


@dataclass(frozen=True)
class IncomingRecord:
    record_type: str
    record_id: str
    record_version: int
    device_id: str
    employee_id: str | None
    local_seq: int
    content_hash: str
    payload_json: str
    batch_id: str


@dataclass(frozen=True)
class StoredRecord:
    record_type: str
    record_id: str
    record_version: int
    device_id: str
    employee_id: str | None
    local_seq: int
    content_hash: str
    payload_json: str
    first_received_at: str
    last_received_at: str
    last_batch_id: str

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class UpsertOutcome:
    status: str  # accepted | updated | already_current | conflict | stale | rejected
    server_version: int | None
    error: str | None = None


def decide(existing: StoredRecord | None, incoming: IncomingRecord) -> UpsertOutcome:
    if existing is None:
        return UpsertOutcome("accepted", incoming.record_version)
    if existing.device_id != incoming.device_id:
        return UpsertOutcome("rejected", None, "record_id belongs to another device")
    if incoming.record_version > existing.record_version:
        return UpsertOutcome("updated", incoming.record_version)
    if incoming.record_version < existing.record_version:
        return UpsertOutcome("stale", existing.record_version, "server holds a newer version")
    if incoming.content_hash == existing.content_hash:
        return UpsertOutcome("already_current", existing.record_version)
    return UpsertOutcome("conflict", existing.record_version, "same version with different content")


class CentralRepository(Protocol):
    # employees
    def add_employee(
        self, employee_id: str, display_name: str, *, role: str = "EMPLOYEE", timezone: str = "UTC"
    ) -> EmployeeRecord: ...
    def get_employee(self, employee_id: str) -> EmployeeRecord | None: ...
    def list_employees(self) -> list[EmployeeRecord]: ...

    # devices & credentials
    def add_device(self, device_id: str, employee_id: str, *, display_name: str | None = None) -> DeviceRecord: ...
    def get_device(self, device_id: str) -> DeviceRecord | None: ...
    def list_devices(self) -> list[DeviceRecord]: ...
    def set_device_status(self, device_id: str, status: str) -> None: ...
    def add_token(self, token_id: str, device_id: str, token_hash: str) -> TokenRecord: ...
    def find_token(self, token_hash: str) -> TokenRecord | None: ...
    def revoke_tokens(self, device_id: str, *, except_token_id: str | None = None) -> int: ...
    def touch_device(self, device_id: str, *, token_id: str | None = None) -> None:
        """Record API connectivity (``last_seen_at``) after a successful
        authentication. This is device connectivity, never employee activity."""

    # synced records
    def upsert_records(self, records: Iterable[IncomingRecord]) -> list[UpsertOutcome]: ...
    def get_record(self, record_type: str, record_id: str) -> StoredRecord | None: ...
    def list_records(self, *, device_id: str | None = None, record_type: str | None = None) -> list[StoredRecord]: ...
    def count_records(self, *, device_id: str | None = None, record_type: str | None = None) -> int: ...

    def close(self) -> None: ...


def session_id_of(incoming: IncomingRecord) -> str | None:
    """The parent work session of a period record (None for other types)."""
    if incoming.record_type not in _SESSION_CHILD_TYPES:
        return None
    return json.loads(incoming.payload_json)["data"]["session_id"]


def check_employee_fields(display_name: str, role: str, timezone_name: str) -> None:
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    if role not in EMPLOYEE_ROLES:
        raise ValueError(f"role must be one of {', '.join(EMPLOYEE_ROLES)}")
    if not display_name.strip():
        raise ValueError("display name must not be empty")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"unknown timezone: {timezone_name}") from exc


def _apply(existing: StoredRecord | None, incoming: IncomingRecord, now: str) -> tuple[UpsertOutcome, StoredRecord | None]:
    """Decide, and return the row to write (None = leave as is)."""
    outcome = decide(existing, incoming)
    if outcome.status == "accepted":
        return outcome, StoredRecord(
            record_type=incoming.record_type, record_id=incoming.record_id,
            record_version=incoming.record_version, device_id=incoming.device_id,
            employee_id=incoming.employee_id, local_seq=incoming.local_seq,
            content_hash=incoming.content_hash, payload_json=incoming.payload_json,
            first_received_at=now, last_received_at=now, last_batch_id=incoming.batch_id,
        )
    if outcome.status == "updated":
        return outcome, replace(
            existing, record_version=incoming.record_version, employee_id=incoming.employee_id,
            local_seq=incoming.local_seq, content_hash=incoming.content_hash,
            payload_json=incoming.payload_json, last_received_at=now, last_batch_id=incoming.batch_id,
        )
    return outcome, None


class InMemoryRepository:
    """Thread-safe in-memory repository for tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._employees: dict[str, EmployeeRecord] = {}
        self._devices: dict[str, DeviceRecord] = {}
        self._tokens: dict[str, TokenRecord] = {}  # by token_hash
        self._records: dict[tuple[str, str], StoredRecord] = {}

    def close(self) -> None:
        pass

    def add_employee(
        self, employee_id: str, display_name: str, *, role: str = "EMPLOYEE", timezone: str = "UTC"
    ) -> EmployeeRecord:
        check_employee_fields(display_name, role, timezone)
        with self._lock:
            if employee_id in self._employees:
                raise ValueError(f"employee already exists: {employee_id}")
            employee = EmployeeRecord(employee_id, display_name, role, True, timezone, utc_now())
            self._employees[employee_id] = employee
            return employee

    def get_employee(self, employee_id: str) -> EmployeeRecord | None:
        with self._lock:
            return self._employees.get(employee_id)

    def list_employees(self) -> list[EmployeeRecord]:
        with self._lock:
            return sorted(self._employees.values(), key=lambda e: e.employee_id)

    def add_device(self, device_id: str, employee_id: str, *, display_name: str | None = None) -> DeviceRecord:
        with self._lock:
            if employee_id not in self._employees:
                raise ValueError(f"unknown employee: {employee_id} (add the employee first)")
            if device_id in self._devices:
                raise ValueError(f"device already registered: {device_id}")
            device = DeviceRecord(device_id, employee_id, "ACTIVE", utc_now(), display_name)
            self._devices[device_id] = device
            return device

    def touch_device(self, device_id: str, *, token_id: str | None = None) -> None:
        with self._lock:
            if device_id in self._devices:
                self._devices[device_id] = replace(self._devices[device_id], last_seen_at=utc_now())

    def get_device(self, device_id: str) -> DeviceRecord | None:
        with self._lock:
            return self._devices.get(device_id)

    def list_devices(self) -> list[DeviceRecord]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda d: d.device_id)

    def set_device_status(self, device_id: str, status: str) -> None:
        with self._lock:
            self._devices[device_id] = replace(self._devices[device_id], status=status)

    def add_token(self, token_id: str, device_id: str, token_hash: str) -> TokenRecord:
        with self._lock:
            token = TokenRecord(token_id, device_id, token_hash, "ACTIVE", utc_now())
            self._tokens[token_hash] = token
            return token

    def find_token(self, token_hash: str) -> TokenRecord | None:
        with self._lock:
            return self._tokens.get(token_hash)

    def revoke_tokens(self, device_id: str, *, except_token_id: str | None = None) -> int:
        with self._lock:
            count = 0
            for key, token in list(self._tokens.items()):
                if token.device_id == device_id and token.status == "ACTIVE" and token.token_id != except_token_id:
                    self._tokens[key] = replace(token, status="REVOKED", revoked_at=utc_now())
                    count += 1
            return count

    def upsert_records(self, records: Iterable[IncomingRecord]) -> list[UpsertOutcome]:
        with self._lock:
            now = utc_now()
            outcomes = []
            for incoming in records:
                key = (incoming.record_type, incoming.record_id)
                existing = self._records.get(key)
                session_id = session_id_of(incoming)
                if session_id and (existing is None or existing.device_id == incoming.device_id):
                    parent = self._records.get(("work_session", session_id))
                    if parent is None or parent.device_id != incoming.device_id:
                        outcomes.append(UpsertOutcome("rejected", None, ORPHAN_PERIOD_ERROR))
                        continue
                outcome, row = _apply(existing, incoming, now)
                if row is not None:
                    self._records[key] = row
                outcomes.append(outcome)
            return outcomes

    def get_record(self, record_type: str, record_id: str) -> StoredRecord | None:
        with self._lock:
            return self._records.get((record_type, record_id))

    def list_records(self, *, device_id: str | None = None, record_type: str | None = None) -> list[StoredRecord]:
        with self._lock:
            rows = [
                r for r in self._records.values()
                if (device_id is None or r.device_id == device_id)
                and (record_type is None or r.record_type == record_type)
            ]
        return sorted(rows, key=lambda r: (r.device_id, r.local_seq))

    def count_records(self, *, device_id: str | None = None, record_type: str | None = None) -> int:
        return len(self.list_records(device_id=device_id, record_type=record_type))


_SQLITE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS employees (
        employee_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('EMPLOYEE', 'MANAGER', 'ADMIN')),
        is_active INTEGER NOT NULL DEFAULT 1,
        timezone TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED')),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS device_tokens (
        token_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL REFERENCES devices(device_id),
        token_hash TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'REVOKED')),
        created_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS synced_records (
        record_type TEXT NOT NULL,
        record_id TEXT NOT NULL,
        record_version INTEGER NOT NULL,
        device_id TEXT NOT NULL,
        employee_id TEXT,
        local_seq INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        first_received_at TEXT NOT NULL,
        last_received_at TEXT NOT NULL,
        last_batch_id TEXT NOT NULL,
        PRIMARY KEY (record_type, record_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_synced_records_device ON synced_records(device_id, local_seq)",
]
# Columns added after the first Phase 3 development databases were created.
_SQLITE_ADDED_COLUMNS = {
    "devices": [("display_name", "TEXT"), ("last_seen_at", "TEXT")],
}


class SqliteDevRepository:
    """Local development store. A single generic ``synced_records`` table
    keeps the payload as JSON — Phase 4 replaces this with typed PostgreSQL
    tables behind the same interface."""

    def __init__(self, path: Path | str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            for statement in _SQLITE_SCHEMA:
                self._conn.execute(statement)
            for table, columns in _SQLITE_ADDED_COLUMNS.items():
                existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                for name, decl in columns:
                    if name not in existing:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _device(row) -> DeviceRecord:  # noqa: ANN001
        return DeviceRecord(row["device_id"], row["employee_id"], row["status"], row["created_at"],
                            row["display_name"], row["last_seen_at"])

    @staticmethod
    def _employee(row) -> EmployeeRecord:  # noqa: ANN001
        return EmployeeRecord(row["employee_id"], row["display_name"], row["role"], bool(row["is_active"]),
                              row["timezone"], row["created_at"])

    def add_employee(
        self, employee_id: str, display_name: str, *, role: str = "EMPLOYEE", timezone: str = "UTC"
    ) -> EmployeeRecord:
        check_employee_fields(display_name, role, timezone)
        now = utc_now()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO employees(employee_id, display_name, role, is_active, timezone, created_at) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (employee_id, display_name, role, timezone, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"employee already exists: {employee_id}") from exc
        return EmployeeRecord(employee_id, display_name, role, True, timezone, now)

    def get_employee(self, employee_id: str) -> EmployeeRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM employees WHERE employee_id = ?", (employee_id,)).fetchone()
        return self._employee(row) if row else None

    def list_employees(self) -> list[EmployeeRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM employees ORDER BY employee_id").fetchall()
        return [self._employee(r) for r in rows]

    def touch_device(self, device_id: str, *, token_id: str | None = None) -> None:
        with self._lock:
            self._conn.execute("UPDATE devices SET last_seen_at = ? WHERE device_id = ?", (utc_now(), device_id))

    @staticmethod
    def _token(row) -> TokenRecord:  # noqa: ANN001
        return TokenRecord(row["token_id"], row["device_id"], row["token_hash"], row["status"],
                           row["created_at"], row["revoked_at"])

    @staticmethod
    def _record(row) -> StoredRecord:  # noqa: ANN001
        return StoredRecord(**{k: row[k] for k in row.keys()})

    def add_device(self, device_id: str, employee_id: str, *, display_name: str | None = None) -> DeviceRecord:
        if self.get_employee(employee_id) is None:
            raise ValueError(f"unknown employee: {employee_id} (add the employee first)")
        now = utc_now()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO devices(device_id, employee_id, status, created_at, display_name) "
                    "VALUES (?, ?, 'ACTIVE', ?, ?)",
                    (device_id, employee_id, now, display_name),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"device already registered: {device_id}") from exc
        return DeviceRecord(device_id, employee_id, "ACTIVE", now, display_name)

    def get_device(self, device_id: str) -> DeviceRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        return self._device(row) if row else None

    def list_devices(self) -> list[DeviceRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM devices ORDER BY device_id").fetchall()
        return [self._device(r) for r in rows]

    def set_device_status(self, device_id: str, status: str) -> None:
        with self._lock:
            cur = self._conn.execute("UPDATE devices SET status = ? WHERE device_id = ?", (status, device_id))
        if cur.rowcount == 0:
            raise KeyError(device_id)

    def add_token(self, token_id: str, device_id: str, token_hash: str) -> TokenRecord:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_tokens(token_id, device_id, token_hash, status, created_at) "
                "VALUES (?, ?, ?, 'ACTIVE', ?)",
                (token_id, device_id, token_hash, now),
            )
        return TokenRecord(token_id, device_id, token_hash, "ACTIVE", now)

    def find_token(self, token_hash: str) -> TokenRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM device_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
        return self._token(row) if row else None

    def revoke_tokens(self, device_id: str, *, except_token_id: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE device_tokens SET status = 'REVOKED', revoked_at = ? "
                "WHERE device_id = ? AND status = 'ACTIVE' AND token_id IS NOT ?",
                (utc_now(), device_id, except_token_id),
            )
        return cur.rowcount

    def upsert_records(self, records: Iterable[IncomingRecord]) -> list[UpsertOutcome]:
        outcomes = []
        now = utc_now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for incoming in records:
                    row = self._conn.execute(
                        "SELECT * FROM synced_records WHERE record_type = ? AND record_id = ?",
                        (incoming.record_type, incoming.record_id),
                    ).fetchone()
                    existing = self._record(row) if row else None
                    session_id = session_id_of(incoming)
                    if session_id and (existing is None or existing.device_id == incoming.device_id):
                        parent = self._conn.execute(
                            "SELECT device_id FROM synced_records WHERE record_type = 'work_session' "
                            "AND record_id = ?", (session_id,),
                        ).fetchone()
                        if parent is None or parent["device_id"] != incoming.device_id:
                            outcomes.append(UpsertOutcome("rejected", None, ORPHAN_PERIOD_ERROR))
                            continue
                    outcome, new_row = _apply(existing, incoming, now)
                    if new_row is not None:
                        self._conn.execute(
                            """
                            INSERT INTO synced_records VALUES
                                (:record_type, :record_id, :record_version, :device_id, :employee_id, :local_seq,
                                 :content_hash, :payload_json, :first_received_at, :last_received_at, :last_batch_id)
                            ON CONFLICT(record_type, record_id) DO UPDATE SET
                                record_version = excluded.record_version, employee_id = excluded.employee_id,
                                local_seq = excluded.local_seq, content_hash = excluded.content_hash,
                                payload_json = excluded.payload_json, last_received_at = excluded.last_received_at,
                                last_batch_id = excluded.last_batch_id
                            """,
                            new_row.__dict__,
                        )
                    outcomes.append(outcome)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return outcomes

    def get_record(self, record_type: str, record_id: str) -> StoredRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM synced_records WHERE record_type = ? AND record_id = ?", (record_type, record_id)
            ).fetchone()
        return self._record(row) if row else None

    def list_records(self, *, device_id: str | None = None, record_type: str | None = None) -> list[StoredRecord]:
        clauses, params = [], []
        if device_id is not None:
            clauses.append("device_id = ?")
            params.append(device_id)
        if record_type is not None:
            clauses.append("record_type = ?")
            params.append(record_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM synced_records {where} ORDER BY device_id, local_seq", params
            ).fetchall()
        return [self._record(r) for r in rows]

    def count_records(self, *, device_id: str | None = None, record_type: str | None = None) -> int:
        return len(self.list_records(device_id=device_id, record_type=record_type))
