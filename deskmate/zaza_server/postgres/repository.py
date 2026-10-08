"""PostgreSQL implementation of :class:`CentralRepository`.

Idempotent upsert, per record, inside one batch transaction:

    BEGIN                                   -- one transaction per batch
      SAVEPOINT                             -- one savepoint per record
        SELECT ... FOR UPDATE               -- lock the existing row, if any
        (none) INSERT ... ON CONFLICT DO NOTHING RETURNING
               -> inserted: accepted
               -> lost a race: re-SELECT ... FOR UPDATE, then decide
        decide(existing, incoming)          -- the shared Phase 3 rule
        (updated) UPDATE ...
      RELEASE / ROLLBACK TO SAVEPOINT       -- a constraint failure rejects
                                            -- only this record
    COMMIT

Concurrent requests touching the same record serialize on its row lock (or
on the primary-key insert), and every decision is made on the locked,
committed row, so two submissions can never both "win" and a lower version
can never overwrite a higher one. A deadlock or serialization failure rolls
the whole batch back and it is retried (up to 3 times); nothing partial is
ever committed from a failed attempt.

Credentials: the pool gets them from :class:`DatabaseSettings` and every
error that leaves this module is scrubbed of the password.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg import errors, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from deskmate.zaza.sync.protocol import SYNC_RECORD_ADAPTER

from ..config import DatabaseSettings
from ..repository import (
    ORPHAN_PERIOD_ERROR,
    DeviceRecord,
    EmployeeRecord,
    IncomingRecord,
    RepositoryUnavailable,
    StoredRecord,
    TokenRecord,
    UpsertOutcome,
    check_employee_fields,
    decide,
)
from . import migrate

logger = logging.getLogger("zaza_server.db")

_BATCH_ATTEMPTS = 3
# SQLSTATE class 40: the transaction was rolled back and may simply be retried.
# (In psycopg these are sibling classes, not subclasses of TransactionRollback.)
_RETRYABLE = (errors.TransactionRollback, errors.SerializationFailure, errors.DeadlockDetected)
_LAST_SEEN_RESOLUTION = "30 seconds"  # don't write last_seen_at on every request


@dataclass(frozen=True)
class _TableSpec:
    table: str
    id_column: str
    data_columns: tuple[str, ...]


TABLES: dict[str, _TableSpec] = {
    "work_session": _TableSpec("work_sessions", "session_id", (
        "started_at", "ended_at", "last_heartbeat_at", "status", "start_reason", "end_reason",
        "previous_session_id", "tracked_seconds", "active_seconds", "idle_seconds", "unknown_seconds",
        "locked_seconds",
    )),
    "activity_period": _TableSpec("activity_periods", "period_id", (
        "session_id", "started_at", "ended_at", "duration_seconds", "is_open", "status", "status_detail",
        "app_name", "window_title", "domain", "privacy_excluded", "start_reason", "end_reason",
    )),
    "idle_period": _TableSpec("idle_periods", "idle_id", (
        "session_id", "started_at", "ended_at", "duration_seconds", "is_open", "end_reason",
    )),
    "app_usage_daily": _TableSpec("application_usage_daily", "usage_id", (
        "usage_date", "day_start_utc", "day_end_utc", "app_name", "active_seconds", "idle_seconds",
        "unknown_seconds", "period_count",
    )),
}
_SYNC_COLUMNS = (
    "device_id", "employee_id", "record_version", "local_seq", "content_hash",
    "device_created_at", "device_updated_at", "last_batch_id",
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") if value is not None else None


def _constraint_message(exc: psycopg.Error) -> str:
    """Short, safe reason for a rejected record. Never echoes row values
    (the driver's DETAIL can contain them, e.g. a window title)."""
    name = getattr(exc.diag, "constraint_name", None) or ""
    if name.endswith("_session_fk"):
        return ORPHAN_PERIOD_ERROR
    if isinstance(exc, errors.UniqueViolation):
        return f"conflicts with an existing record ({name})"
    if name:
        return f"violates database constraint {name}"
    return "invalid value for the database"


def _build_sql(spec: _TableSpec) -> dict[str, sql.Composed]:
    table, id_col = sql.Identifier(spec.table), sql.Identifier(spec.id_column)
    write_cols = (*_SYNC_COLUMNS, *spec.data_columns)
    placeholders = [sql.Placeholder(c) for c in write_cols]
    insert = sql.SQL(
        "INSERT INTO {t} ({id}, {cols}, payload) VALUES ({pid}, {vals}, {payload}::jsonb) "
        "ON CONFLICT ({id}) DO NOTHING RETURNING {id}"
    ).format(
        t=table, id=id_col, cols=sql.SQL(", ").join(map(sql.Identifier, write_cols)),
        pid=sql.Placeholder("record_id"), vals=sql.SQL(", ").join(placeholders),
        payload=sql.Placeholder("payload"),
    )
    # clock_timestamp(), not now(): now() is the transaction START, and a
    # transaction that waited on another's row lock can have started before
    # that row was first received. GREATEST keeps last_received_at monotonic.
    update = sql.SQL(
        "UPDATE {t} SET {sets}, payload = {payload}::jsonb, "
        "last_received_at = GREATEST(clock_timestamp(), last_received_at) WHERE {id} = {pid}"
    ).format(
        t=table, id=id_col, pid=sql.Placeholder("record_id"), payload=sql.Placeholder("payload"),
        sets=sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder(c))
            for c in write_cols if c != "device_id"
        ),
    )
    lock = sql.SQL(
        "SELECT device_id, record_version, content_hash FROM {t} WHERE {id} = %s FOR UPDATE"
    ).format(t=table, id=id_col)
    select = sql.SQL(
        "SELECT {id}::text AS record_id, device_id, employee_id, record_version, local_seq, content_hash, "
        "payload, first_received_at, last_received_at, last_batch_id::text AS last_batch_id FROM {t}"
    ).format(t=table, id=id_col)
    return {"insert": insert, "update": update, "lock": lock, "select": select}


_SQL = {rtype: _build_sql(spec) for rtype, spec in TABLES.items()}


class PostgresRepository:
    """Production repository. ``actor_type``/``actor_id`` label the audit
    entries written for administrative changes (e.g. ``CLI``/``admin``)."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        actor_type: str = "SYSTEM",
        actor_id: str | None = None,
        require_current_schema: bool = True,
    ) -> None:
        self.settings = settings
        self.actor_type = actor_type
        self.actor_id = actor_id
        self._pool = ConnectionPool(
            "",
            kwargs={**settings.connect_kwargs(), "autocommit": True, "row_factory": dict_row},
            min_size=settings.pool_min,
            max_size=settings.pool_max,
            timeout=settings.pool_timeout,
            max_idle=300,
            max_lifetime=3600,
            name="zaza-db",
            open=False,
        )
        try:
            try:
                self._pool.open(wait=settings.pool_min > 0, timeout=settings.connect_timeout + 2)
            except PoolTimeout:
                raise RepositoryUnavailable(f"cannot connect to {settings.display()}") from None
            with self._conn() as conn:
                status = migrate.schema_status(conn)
        except BaseException:
            self._pool.close()
            raise
        if require_current_schema and not status.up_to_date:
            self._pool.close()
            raise RepositoryUnavailable(
                f"database schema is at {status.current or 'nothing (empty database)'}, "
                f"this server needs {status.head}; run `python -m deskmate.zaza_server migrate` first"
            )
        logger.info("connected to %s (pool %d..%d)", settings.display(), settings.pool_min, settings.pool_max)

    def close(self) -> None:
        self._pool.close()

    def connection(self):  # noqa: ANN201 — context manager yielding a pooled psycopg connection
        """A pooled connection (autocommit, dict rows) for other server
        modules that read or write the central database, e.g. attendance."""
        return self._conn()

    # ─── connections ───────────────────────────────────────────────────────
    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        try:
            with self._pool.connection() as conn:
                yield conn
        except PoolTimeout:
            raise RepositoryUnavailable(
                f"no database connection available ({self.settings.display()})"
            ) from None
        except _RETRYABLE:
            raise  # deadlock/serialization: the caller decides whether to retry
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(
                f"database unavailable ({self.settings.display()}): {self.settings.scrub(str(exc)).strip()[:300]}"
            ) from None

    # ─── audit ─────────────────────────────────────────────────────────────
    def _audit(self, conn, action: str, entity_type: str, entity_id: str,  # noqa: ANN001
               old: dict | None = None, new: dict | None = None) -> None:
        conn.execute(
            "INSERT INTO audit_logs (actor_type, actor_id, action, entity_type, entity_id, old_values, new_values) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)",
            (self.actor_type, self.actor_id, action, entity_type, entity_id,
             json.dumps(old) if old is not None else None, json.dumps(new) if new is not None else None),
        )

    def list_audit(self, *, entity_type: str | None = None, entity_id: str | None = None) -> list[dict]:
        clauses, params = [], []
        if entity_type:
            clauses.append("entity_type = %s")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id = %s")
            params.append(entity_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn:
            return conn.execute(f"SELECT * FROM audit_logs {where} ORDER BY audit_id", params).fetchall()

    # ─── employees ─────────────────────────────────────────────────────────
    @staticmethod
    def _employee(row: dict) -> EmployeeRecord:
        return EmployeeRecord(row["employee_id"], row["display_name"], row["role"], row["is_active"],
                              row["timezone"], _iso(row["created_at"]))

    def add_employee(
        self, employee_id: str, display_name: str, *, role: str = "EMPLOYEE", timezone: str = "UTC"
    ) -> EmployeeRecord:
        check_employee_fields(display_name, role, timezone)
        with self._conn() as conn:
            try:
                with conn.transaction():
                    row = conn.execute(
                        "INSERT INTO employees (employee_id, display_name, role, timezone) VALUES (%s, %s, %s, %s) "
                        "RETURNING *",
                        (employee_id, display_name, role, timezone),
                    ).fetchone()
                    self._audit(conn, "employee.create", "employee", employee_id,
                                new={"display_name": display_name, "role": role, "timezone": timezone})
            except errors.UniqueViolation:
                raise ValueError(f"employee already exists: {employee_id}") from None
            except (errors.IntegrityError, errors.DataError) as exc:
                raise ValueError(f"invalid employee: {_constraint_message(exc)}") from None
        return self._employee(row)

    def get_employee(self, employee_id: str) -> EmployeeRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM employees WHERE employee_id = %s", (employee_id,)).fetchone()
        return self._employee(row) if row else None

    def list_employees(self) -> list[EmployeeRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM employees ORDER BY employee_id").fetchall()
        return [self._employee(r) for r in rows]

    # ─── devices & tokens ──────────────────────────────────────────────────
    @staticmethod
    def _device(row: dict) -> DeviceRecord:
        return DeviceRecord(row["device_id"], row["employee_id"], row["status"], _iso(row["created_at"]),
                            row["display_name"], _iso(row["last_seen_at"]))

    @staticmethod
    def _token(row: dict) -> TokenRecord:
        return TokenRecord(str(row["token_id"]), row["device_id"], row["token_hash"], row["status"],
                           _iso(row["created_at"]), _iso(row["revoked_at"]))

    def add_device(self, device_id: str, employee_id: str, *, display_name: str | None = None) -> DeviceRecord:
        with self._conn() as conn:
            try:
                with conn.transaction():
                    row = conn.execute(
                        "INSERT INTO devices (device_id, employee_id, display_name) VALUES (%s, %s, %s) RETURNING *",
                        (device_id, employee_id, display_name),
                    ).fetchone()
                    self._audit(conn, "device.register", "device", device_id,
                                new={"employee_id": employee_id, "display_name": display_name})
            except errors.ForeignKeyViolation:
                raise ValueError(f"unknown employee: {employee_id} (add the employee first)") from None
            except errors.UniqueViolation:
                raise ValueError(f"device already registered: {device_id}") from None
            except (errors.IntegrityError, errors.DataError) as exc:
                raise ValueError(f"invalid device: {_constraint_message(exc)}") from None
        return self._device(row)

    def get_device(self, device_id: str) -> DeviceRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,)).fetchone()
        return self._device(row) if row else None

    def list_devices(self) -> list[DeviceRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM devices ORDER BY device_id").fetchall()
        return [self._device(r) for r in rows]

    def set_device_status(self, device_id: str, status: str) -> None:
        with self._conn() as conn, conn.transaction():
            old = conn.execute("SELECT status FROM devices WHERE device_id = %s FOR UPDATE", (device_id,)).fetchone()
            if old is None:
                raise KeyError(device_id)
            conn.execute(
                "UPDATE devices SET status = %s, "
                "disabled_at = CASE WHEN %s = 'DISABLED' THEN COALESCE(disabled_at, now()) ELSE NULL END "
                "WHERE device_id = %s",
                (status, status, device_id),
            )
            if old["status"] != status:
                self._audit(conn, "device.disable" if status == "DISABLED" else "device.enable", "device",
                            device_id, old={"status": old["status"]}, new={"status": status})

    def add_token(self, token_id: str, device_id: str, token_hash: str) -> TokenRecord:
        with self._conn() as conn, conn.transaction():
            row = conn.execute(
                "INSERT INTO device_tokens (token_id, device_id, token_hash) VALUES (%s::uuid, %s, %s) RETURNING *",
                (token_id, device_id, token_hash),
            ).fetchone()
            self._audit(conn, "device_token.issue", "device_token", token_id, new={"device_id": device_id})
        return self._token(row)

    def find_token(self, token_hash: str) -> TokenRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM device_tokens WHERE token_hash = %s", (token_hash,)).fetchone()
        return self._token(row) if row else None

    def revoke_tokens(self, device_id: str, *, except_token_id: str | None = None) -> int:
        with self._conn() as conn, conn.transaction():
            rows = conn.execute(
                "UPDATE device_tokens SET status = 'REVOKED', revoked_at = now() "
                "WHERE device_id = %s AND status = 'ACTIVE' AND token_id IS DISTINCT FROM %s::uuid "
                "RETURNING token_id",
                (device_id, except_token_id),
            ).fetchall()
            for row in rows:
                self._audit(conn, "device_token.revoke", "device_token", str(row["token_id"]),
                            old={"status": "ACTIVE"}, new={"status": "REVOKED", "device_id": device_id})
        return len(rows)

    def touch_device(self, device_id: str, *, token_id: str | None = None) -> None:
        stale = f"now() - interval '{_LAST_SEEN_RESOLUTION}'"
        with self._conn() as conn, conn.transaction():
            conn.execute(
                f"UPDATE devices SET last_seen_at = now() WHERE device_id = %s "
                f"AND (last_seen_at IS NULL OR last_seen_at < {stale})",
                (device_id,),
            )
            if token_id:
                conn.execute(
                    f"UPDATE device_tokens SET last_used_at = now() WHERE token_id = %s::uuid "
                    f"AND (last_used_at IS NULL OR last_used_at < {stale})",
                    (token_id,),
                )

    # ─── synced records ────────────────────────────────────────────────────
    def upsert_records(self, records: Iterable[IncomingRecord]) -> list[UpsertOutcome]:
        records = list(records)
        if not records:
            return []
        for attempt in range(1, _BATCH_ATTEMPTS + 1):
            try:
                with self._conn() as conn, conn.transaction():
                    return [self._upsert_one(conn, incoming) for incoming in records]
            except _RETRYABLE as exc:
                logger.warning("batch transaction rolled back (%s); attempt %d/%d",
                               type(exc).__name__, attempt, _BATCH_ATTEMPTS)
        raise RepositoryUnavailable("database busy: batch could not be committed, retry later")

    def _upsert_one(self, conn: psycopg.Connection, incoming: IncomingRecord) -> UpsertOutcome:
        statements = _SQL[incoming.record_type]
        spec = TABLES[incoming.record_type]
        record = SYNC_RECORD_ADAPTER.validate_json(incoming.payload_json)
        params = {
            "record_id": uuid.UUID(incoming.record_id),
            "device_id": incoming.device_id,
            "employee_id": incoming.employee_id,
            "record_version": incoming.record_version,
            "local_seq": incoming.local_seq,
            "content_hash": incoming.content_hash,
            "device_created_at": record.created_at,
            "device_updated_at": record.updated_at,
            "last_batch_id": uuid.UUID(incoming.batch_id),
            "payload": incoming.payload_json,
            **{col: getattr(record.data, col) for col in spec.data_columns},
        }
        try:
            with conn.transaction():  # savepoint: a failure here rejects only this record
                existing = conn.execute(statements["lock"], (params["record_id"],)).fetchone()
                if existing is None:
                    if conn.execute(statements["insert"], params).fetchone() is not None:
                        return UpsertOutcome("accepted", incoming.record_version)
                    # A concurrent request inserted it first; decide against that row.
                    existing = conn.execute(statements["lock"], (params["record_id"],)).fetchone()
                outcome = decide(_as_stored(existing, incoming), incoming)
                if outcome.status == "updated":
                    conn.execute(statements["update"], params)
                return outcome
        except (errors.IntegrityError, errors.DataError) as exc:
            return UpsertOutcome("rejected", None, _constraint_message(exc))

    def _select(self, record_type: str, where: str = "", params: tuple = ()) -> list[StoredRecord]:
        query = _SQL[record_type]["select"] + sql.SQL(where)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            StoredRecord(
                record_type=record_type, record_id=r["record_id"], record_version=r["record_version"],
                device_id=r["device_id"], employee_id=r["employee_id"], local_seq=r["local_seq"],
                content_hash=r["content_hash"], payload_json=json.dumps(r["payload"]),
                first_received_at=_iso(r["first_received_at"]), last_received_at=_iso(r["last_received_at"]),
                last_batch_id=r["last_batch_id"],
            )
            for r in rows
        ]

    def get_record(self, record_type: str, record_id: str) -> StoredRecord | None:
        if record_type not in TABLES:
            return None
        try:
            key = uuid.UUID(record_id)
        except ValueError:
            return None
        rows = self._select(record_type, f" WHERE {TABLES[record_type].id_column} = %s", (key,))
        return rows[0] if rows else None

    def list_records(self, *, device_id: str | None = None, record_type: str | None = None) -> list[StoredRecord]:
        types = [record_type] if record_type else list(TABLES)
        rows: list[StoredRecord] = []
        for rtype in types:
            if rtype not in TABLES:
                continue
            if device_id is None:
                rows.extend(self._select(rtype))
            else:
                rows.extend(self._select(rtype, " WHERE device_id = %s", (device_id,)))
        return sorted(rows, key=lambda r: (r.device_id, r.local_seq))

    def count_records(self, *, device_id: str | None = None, record_type: str | None = None) -> int:
        total = 0
        with self._conn() as conn:
            for rtype in [record_type] if record_type else list(TABLES):
                if rtype not in TABLES:
                    continue
                query = sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(TABLES[rtype].table))
                if device_id is not None:
                    query += sql.SQL(" WHERE device_id = %s")
                    total += conn.execute(query, (device_id,)).fetchone()["n"]
                else:
                    total += conn.execute(query).fetchone()["n"]
        return total


def _as_stored(row: dict, incoming: IncomingRecord) -> StoredRecord:
    """Just the fields :func:`decide` compares, from the locked row."""
    return StoredRecord(
        record_type=incoming.record_type, record_id=incoming.record_id, record_version=row["record_version"],
        device_id=row["device_id"], employee_id=None, local_seq=0, content_hash=row["content_hash"],
        payload_json="{}", first_received_at="", last_received_at="", last_batch_id="",
    )

