"""Local SQLite store for the ZaZa activity agent.

Separate database file from upstream DeskMate (see ``zaza/paths.py``). The
schema (``schema.py``) has no column that could hold typed text, clipboard
contents, a screenshot, or audio/video, and every write method takes a
fixed, keyword-only parameter set, so a caller cannot smuggle extra content
through even by accident.

Reliability: WAL journal + ``synchronous=NORMAL`` (an application crash never
loses a committed transaction; a power cut can lose at most the last few),
explicit ``BEGIN IMMEDIATE`` transactions via :meth:`ActivityStore.transaction`
(re-entrant, so one agent tick is one atomic commit), and versioned
migrations. Writes are small row-level inserts/updates — nothing rewrites the
database.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import paths, schema
from .schema import ID_COLUMNS, SCHEMA_VERSION, SESSION_STATUSES, SYNCED_TABLES
from .timeutil import iso_utc

__all__ = ["ActivityStore", "EVENT_TYPES", "SCHEMA_VERSION", "USAGE_ID_NAMESPACE"]

EVENT_TYPES = frozenset(
    {
        "APP_CHANGE", "ACTIVITY", "IDLE", "CONTINUE", "LOCK", "UNLOCK",
        "SESSION_START", "SESSION_END", "TELEMETRY_GAP",
    }
)

# Deterministic IDs for daily app-usage rollups: the same device/day/app
# always maps to the same usage_id, so re-sending a rollup is idempotent.
USAGE_ID_NAMESPACE = uuid.UUID("6f3a1c2e-8d4b-4f7a-9c1e-2b5d7a9e0f13")

_RETENTION_BATCH = 5000
_SECONDS_PER_DAY = 86400

# Material change: bump the version and put the row back in the sync queue.
_TOUCH = "record_version = record_version + 1, sync_status = 'PENDING', updated_at = :now"


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


class ActivityStore:
    def __init__(self, db_file: Path | str | None = None) -> None:
        paths.ensure_dirs()
        self.path = Path(db_file) if db_file else paths.db_path()
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._conn.row_factory = _dict_factory
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            schema.migrate(self._conn)

    # ─── plumbing ──────────────────────────────────────────────────────────
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant transaction. Only the outermost level commits; an
        exception escaping the outermost level rolls everything back."""
        with self._lock:
            outermost = self._tx_depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield self._conn
            except BaseException:
                self._tx_depth -= 1
                if outermost:
                    self._conn.execute("ROLLBACK")
                raise
            self._tx_depth -= 1
            if outermost:
                self._conn.execute("COMMIT")

    def ping(self) -> None:
        """Cheap readiness probe; raises ``sqlite3.Error`` if the DB is unusable."""
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def schema_version(self) -> int:
        with self._lock:
            return schema.current_version(self._conn)

    def journal_mode(self) -> str:
        with self._lock:
            return self._conn.execute("PRAGMA journal_mode").fetchone()["journal_mode"]

    def _next_seq(self) -> int:
        """Device-local, strictly increasing sequence shared by every synced
        record — gives Phase 3 a total creation order independent of clocks."""
        with self.transaction() as conn:
            conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'local_seq'")
            return int(conn.execute("SELECT value FROM counters WHERE name = 'local_seq'").fetchone()["value"])

    def _query(self, sql: str, params: Iterable[Any] | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    # ─── raw events (local only, short retention) ──────────────────────────
    def insert_event(
        self,
        *,
        event_type: str,
        ts: str | None = None,
        app_name: str | None = None,
        window_title: str | None = None,
        keyboard_active: bool | None = None,
        mouse_active: bool | None = None,
        idle: bool | None = None,
        session_id: str | None = None,
        status: str | None = None,
        privacy_excluded: bool = False,
    ) -> int:
        """Insert one raw activity event. Keyword-only, fixed parameter set —
        there is no argument here that could carry typed text, clipboard
        contents, a screenshot, or audio/video."""
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event_type: {event_type!r}")
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO activity_events
                    (event_id, ts, event_type, session_id, status, app_name, window_title,
                     keyboard_active, mouse_active, idle, privacy_excluded)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), ts or iso_utc_now(), event_type, session_id, status,
                    app_name, window_title, _bool(keyboard_active), _bool(mouse_active), _bool(idle),
                    int(privacy_excluded),
                ),
            )
            return int(cur.lastrowid)

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM activity_events ORDER BY id DESC LIMIT ?", (limit,))

    def count_events(self) -> int:
        return int(self._query("SELECT COUNT(*) AS n FROM activity_events")[0]["n"])

    # ─── work sessions ─────────────────────────────────────────────────────
    def create_session(
        self,
        *,
        session_id: str,
        device_id: str,
        employee_id: str,
        started_at: float,
        start_reason: str,
        previous_session_id: str | None = None,
    ) -> None:
        ts = iso_utc(started_at)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO work_sessions
                    (session_id, device_id, employee_id, local_seq, started_at, last_heartbeat_at,
                     status, start_reason, previous_session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
                """,
                (session_id, device_id, employee_id, self._next_seq(), ts, ts, start_reason,
                 previous_session_id, ts, ts),
            )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM work_sessions WHERE session_id = ?", (session_id,))
        return rows[0] if rows else None

    def sessions(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM work_sessions ORDER BY local_seq")

    def open_sessions(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM work_sessions WHERE status = 'OPEN' ORDER BY local_seq")

    def _session_totals(self, session_id: str) -> dict[str, float]:
        totals = {status: 0.0 for status in schema.PERIOD_STATUSES}
        for row in self._query(
            "SELECT status, SUM(duration_seconds) AS secs FROM activity_periods WHERE session_id = ? GROUP BY status",
            (session_id,),
        ):
            totals[row["status"]] = float(row["secs"] or 0.0)
        return totals

    def _write_session(self, session_id: str, at_iso: str, extra_sql: str = "", extra: dict | None = None) -> None:
        totals = self._session_totals(session_id)
        params = {
            "id": session_id,
            "now": at_iso,
            "tracked": sum(totals.values()),
            "active": totals["ACTIVE"],
            "idle": totals["IDLE"],
            "unknown": totals["UNKNOWN"],
            "locked": totals["LOCKED"],
            **(extra or {}),
        }
        with self.transaction() as conn:
            conn.execute(
                f"""
                UPDATE work_sessions SET
                    tracked_seconds = :tracked, active_seconds = :active, idle_seconds = :idle,
                    unknown_seconds = :unknown, locked_seconds = :locked, {_TOUCH}{extra_sql}
                WHERE session_id = :id
                """,
                params,
            )

    def heartbeat_session(self, session_id: str, at: float) -> None:
        """Mark the session alive at ``at`` and refresh its duration totals."""
        ts = iso_utc(at)
        self._write_session(session_id, ts, ", last_heartbeat_at = :now")

    def finalize_session(self, session_id: str, *, ended_at: float | str, status: str, end_reason: str) -> None:
        if status not in SESSION_STATUSES or status == "OPEN":
            raise ValueError(f"invalid final session status: {status!r}")
        ts = ended_at if isinstance(ended_at, str) else iso_utc(ended_at)
        self._write_session(
            session_id,
            ts,
            ", ended_at = :ended, status = :status, end_reason = :reason",
            {"ended": ts, "status": status, "reason": end_reason},
        )

    def close_interrupted_session(self, session_id: str) -> list[dict[str, Any]]:
        """Crash recovery for one OPEN session left behind by a previous run.

        Open periods are closed exactly where they were last extended (the
        last heartbeat) — no time after that is attributed to anything. The
        session is marked INTERRUPTED and ended at its last heartbeat.
        Returns the periods that were closed."""
        session = self.get_session(session_id)
        if session is None or session["status"] != "OPEN":
            return []
        now = session["last_heartbeat_at"]
        with self.transaction() as conn:
            closed = list(
                conn.execute(
                    "SELECT * FROM activity_periods WHERE session_id = ? AND is_open = 1", (session_id,)
                ).fetchall()
            )
            for table in ("activity_periods", "idle_periods"):
                conn.execute(
                    f"UPDATE {table} SET is_open = 0, end_reason = 'INTERRUPTED', {_TOUCH} "
                    "WHERE session_id = :id AND is_open = 1",
                    {"id": session_id, "now": now},
                )
            self.finalize_session(session_id, ended_at=now, status="INTERRUPTED", end_reason="AGENT_INTERRUPTED")
        return closed

    # ─── activity periods ──────────────────────────────────────────────────
    def insert_period(
        self,
        *,
        period_id: str,
        session_id: str,
        device_id: str,
        started_at: float,
        ended_at: float,
        status: str,
        start_reason: str,
        status_detail: str | None = None,
        app_name: str | None = None,
        window_title: str | None = None,
        domain: str | None = None,
        privacy_excluded: bool = False,
        is_open: bool = True,
        end_reason: str | None = None,
    ) -> None:
        now = iso_utc(ended_at)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO activity_periods
                    (period_id, session_id, device_id, local_seq, started_at, ended_at, duration_seconds,
                     is_open, status, status_detail, app_name, window_title, domain, privacy_excluded,
                     start_reason, end_reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    period_id, session_id, device_id, self._next_seq(), iso_utc(started_at), now,
                    max(0.0, ended_at - started_at), int(is_open), status, status_detail, app_name,
                    window_title, domain, int(privacy_excluded), start_reason, end_reason, now, now,
                ),
            )

    def update_period_end(
        self, period_id: str, *, started_at: float, ended_at: float, close_reason: str | None = None
    ) -> None:
        """Extend an open period to ``ended_at`` or, with ``close_reason``, close it there."""
        params = {
            "id": period_id,
            "ended": iso_utc(ended_at),
            "dur": max(0.0, ended_at - started_at),
            "now": iso_utc(ended_at),
            "reason": close_reason,
        }
        closing = ", is_open = 0, end_reason = :reason" if close_reason else ""
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE activity_periods SET ended_at = :ended, duration_seconds = :dur, {_TOUCH}{closing} "
                "WHERE period_id = :id",
                params,
            )

    def get_period(self, period_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM activity_periods WHERE period_id = ?", (period_id,))
        return rows[0] if rows else None

    def periods(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id is None:
            return self._query("SELECT * FROM activity_periods ORDER BY started_at, local_seq")
        return self._query(
            "SELECT * FROM activity_periods WHERE session_id = ? ORDER BY started_at, local_seq", (session_id,)
        )

    def periods_overlapping(self, start: float, end: float) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM activity_periods WHERE started_at < ? AND ended_at > ? ORDER BY started_at",
            (iso_utc(end), iso_utc(start)),
        )

    # ─── idle periods ──────────────────────────────────────────────────────
    def insert_idle(self, *, idle_id: str, session_id: str, device_id: str, started_at: float, ended_at: float) -> None:
        now = iso_utc(ended_at)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO idle_periods
                    (idle_id, session_id, device_id, local_seq, started_at, ended_at, duration_seconds,
                     is_open, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (idle_id, session_id, device_id, self._next_seq(), iso_utc(started_at), now,
                 max(0.0, ended_at - started_at), now, now),
            )

    def update_idle_end(
        self, idle_id: str, *, started_at: float, ended_at: float, close_reason: str | None = None
    ) -> None:
        params = {
            "id": idle_id,
            "ended": iso_utc(ended_at),
            "dur": max(0.0, ended_at - started_at),
            "now": iso_utc(ended_at),
            "reason": close_reason,
        }
        closing = ", is_open = 0, end_reason = :reason" if close_reason else ""
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE idle_periods SET ended_at = :ended, duration_seconds = :dur, {_TOUCH}{closing} "
                "WHERE idle_id = :id",
                params,
            )

    def idle_periods(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id is None:
            return self._query("SELECT * FROM idle_periods ORDER BY started_at, local_seq")
        return self._query(
            "SELECT * FROM idle_periods WHERE session_id = ? ORDER BY started_at, local_seq", (session_id,)
        )

    # ─── application usage rollups ─────────────────────────────────────────
    @staticmethod
    def usage_id(device_id: str, usage_date: str, app_name: str) -> str:
        return str(uuid.uuid5(USAGE_ID_NAMESPACE, f"{device_id}|{usage_date}|{app_name}"))

    def upsert_app_usage(
        self,
        *,
        device_id: str,
        employee_id: str,
        usage_date: str,
        app_name: str,
        active_seconds: float,
        idle_seconds: float,
        unknown_seconds: float,
        period_count: int,
        at: float,
    ) -> bool:
        """Insert or update one (device, day, app) rollup. Returns True if
        anything changed; an unchanged rollup is not re-queued for sync."""
        usage_id = self.usage_id(device_id, usage_date, app_name)
        values = {
            "id": usage_id,
            "active": round(active_seconds, 3),
            "idle": round(idle_seconds, 3),
            "unknown": round(unknown_seconds, 3),
            "count": period_count,
            "now": iso_utc(at),
        }
        with self.transaction() as conn:
            existing = conn.execute("SELECT * FROM app_usage_daily WHERE usage_id = ?", (usage_id,)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO app_usage_daily
                        (usage_id, device_id, employee_id, local_seq, usage_date, app_name, active_seconds,
                         idle_seconds, unknown_seconds, period_count, created_at, updated_at)
                    VALUES (:id, :device, :employee, :seq, :date, :app, :active, :idle, :unknown, :count, :now, :now)
                    """,
                    {**values, "device": device_id, "employee": employee_id, "seq": self._next_seq(),
                     "date": usage_date, "app": app_name},
                )
                return True
            if (
                existing["active_seconds"] == values["active"]
                and existing["idle_seconds"] == values["idle"]
                and existing["unknown_seconds"] == values["unknown"]
                and existing["period_count"] == values["count"]
            ):
                return False
            conn.execute(
                f"""
                UPDATE app_usage_daily SET active_seconds = :active, idle_seconds = :idle,
                    unknown_seconds = :unknown, period_count = :count, {_TOUCH}
                WHERE usage_id = :id
                """,
                values,
            )
            return True

    def app_usage(self, usage_date: str | None = None) -> list[dict[str, Any]]:
        if usage_date is None:
            return self._query("SELECT * FROM app_usage_daily ORDER BY usage_date, active_seconds DESC")
        return self._query(
            "SELECT * FROM app_usage_daily WHERE usage_date = ? ORDER BY active_seconds DESC", (usage_date,)
        )

    # ─── sync queue metadata (consumed by Phase 3; no network here) ────────
    @staticmethod
    def _check_table(table: str) -> str:
        if table not in SYNCED_TABLES:
            raise ValueError(f"not a synced table: {table!r}")
        return table

    @staticmethod
    def _closed_clause(table: str) -> str:
        if table == "work_sessions":
            return " AND status != 'OPEN'"
        if table in ("activity_periods", "idle_periods"):
            return " AND is_open = 0"
        return ""

    def pending_sync(
        self, table: str, *, limit: int = 500, include_open: bool = False, after_seq: int | None = None
    ) -> list[dict[str, Any]]:
        """Records not yet confirmed synced (PENDING or FAILED), in device order.
        Open periods/sessions are excluded unless ``include_open``.
        ``after_seq`` continues a scan within one sync cycle."""
        self._check_table(table)
        where = "sync_status IN ('PENDING', 'FAILED')"
        if not include_open:
            where += self._closed_clause(table)
        params: list[Any] = []
        if after_seq is not None:
            where += " AND local_seq > ?"
            params.append(after_seq)
        params.append(limit)
        return self._query(f"SELECT * FROM {table} WHERE {where} ORDER BY local_seq LIMIT ?", params)

    def sync_counts(self) -> dict[str, int]:
        """Backlog across synced tables: closed records still PENDING, records
        whose last attempt FAILED, and open records (always re-sent while open)."""
        counts = {"pending": 0, "failed": 0, "open": 0}
        for table in SYNCED_TABLES:
            closed = self._closed_clause(table)
            row = self._query(
                f"""
                SELECT
                    SUM(CASE WHEN sync_status = 'PENDING'{closed} THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN sync_status = 'FAILED' THEN 1 ELSE 0 END) AS failed
                FROM {table}
                """
            )[0]
            counts["pending"] += int(row["pending"] or 0)
            counts["failed"] += int(row["failed"] or 0)
            if closed:
                open_clause = "status = 'OPEN'" if table == "work_sessions" else "is_open = 1"
                counts["open"] += int(
                    self._query(f"SELECT COUNT(*) AS n FROM {table} WHERE {open_clause}")[0]["n"]
                )
        return counts

    def requeue_record(self, table: str, record_id: str, *, expected_version: int, at: float) -> bool:
        """Bump a record to a new version and put it back in the queue — used
        when the server reports a same-version conflict, so the device's
        current content is sent again as a strictly newer version."""
        id_col = ID_COLUMNS[self._check_table(table)]
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE {table} SET {_TOUCH} WHERE {id_col} = :id AND record_version = :v",
                {"id": record_id, "v": expected_version, "now": iso_utc(at)},
            )
        return cur.rowcount == 1

    def adopt_server_version(
        self, table: str, record_id: str, *, sent_version: int, server_version: int, at: float
    ) -> bool:
        """The server already holds a newer version than we sent (``stale``).
        Keep the server's copy, and move the local version number up to match
        so the next local change is sent as a version the server will accept."""
        id_col = ID_COLUMNS[self._check_table(table)]
        with self.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE {table} SET record_version = :sv, sync_status = 'SYNCED', synced_version = :sv,
                    synced_at = :now, last_sync_attempt_at = :now, sync_attempts = sync_attempts + 1,
                    last_sync_error = NULL
                WHERE {id_col} = :id AND record_version = :v AND :sv > :v
                """,
                {"id": record_id, "v": sent_version, "sv": server_version, "now": iso_utc(at)},
            )
        return cur.rowcount == 1

    def mark_synced(self, table: str, records: Iterable[tuple[str, int]], *, at: float) -> int:
        """Mark (id, record_version) pairs SYNCED. A row changed since that
        version was read stays PENDING, so the newer version is sent later."""
        id_col = ID_COLUMNS[self._check_table(table)]
        count = 0
        with self.transaction() as conn:
            for record_id, version in records:
                cur = conn.execute(
                    f"""
                    UPDATE {table} SET sync_status = 'SYNCED', synced_at = ?, synced_version = ?,
                        last_sync_attempt_at = ?, sync_attempts = sync_attempts + 1, last_sync_error = NULL
                    WHERE {id_col} = ? AND record_version = ?
                    """,
                    (iso_utc(at), version, iso_utc(at), record_id, version),
                )
                count += cur.rowcount
        return count

    def mark_sync_failed(self, table: str, record_ids: Iterable[str], *, error: str, at: float) -> int:
        id_col = ID_COLUMNS[self._check_table(table)]
        count = 0
        with self.transaction() as conn:
            for record_id in record_ids:
                cur = conn.execute(
                    f"""
                    UPDATE {table} SET sync_status = 'FAILED', sync_attempts = sync_attempts + 1,
                        last_sync_attempt_at = ?, last_sync_error = ?
                    WHERE {id_col} = ? AND sync_status != 'SYNCED'
                    """,
                    (iso_utc(at), error[:500], record_id),
                )
                count += cur.rowcount
        return count

    def get_sync_state(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM sync_state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_sync_state(self, key: str, value: str, *, at: float) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO sync_state(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, iso_utc(at)),
            )

    # ─── retention ─────────────────────────────────────────────────────────
    def _delete_in_batches(self, table: str, rowid_sql: str, params: tuple) -> int:
        total = 0
        while True:
            with self.transaction() as conn:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN ({rowid_sql} LIMIT {_RETENTION_BATCH})", params
                )
            total += cur.rowcount
            if cur.rowcount < _RETENTION_BATCH:
                return total

    def apply_retention(self, *, now: float, raw_retention_days: int, synced_retention_days: int) -> dict[str, int]:
        """Delete old data that is safe to delete, in small batches.

        - Raw events older than ``raw_retention_days`` — including those of a
          still-OPEN session. Raw events are local, high-frequency, never
          synced, and never read back to rebuild state: the open period, idle
          period and session live in their own tables, which is what crash
          recovery and the aggregator use.
        - Synced-table rows only when confirmed SYNCED (at their current
          version), closed, and older than ``synced_retention_days``.
          PENDING/FAILED rows are never deleted, however old.
        - A session row only once none of its periods/idle periods remain.
        """
        raw_cutoff = iso_utc(now - raw_retention_days * _SECONDS_PER_DAY)
        synced_cutoff = iso_utc(now - synced_retention_days * _SECONDS_PER_DAY)
        synced = "sync_status = 'SYNCED' AND synced_version = record_version"
        deleted = {
            "activity_events": self._delete_in_batches(
                "activity_events",
                "SELECT rowid FROM activity_events WHERE ts < ?",
                (raw_cutoff,),
            ),
            "activity_periods": self._delete_in_batches(
                "activity_periods",
                f"SELECT rowid FROM activity_periods WHERE {synced} AND is_open = 0 AND ended_at < ?",
                (synced_cutoff,),
            ),
            "idle_periods": self._delete_in_batches(
                "idle_periods",
                f"SELECT rowid FROM idle_periods WHERE {synced} AND is_open = 0 AND ended_at < ?",
                (synced_cutoff,),
            ),
            "app_usage_daily": self._delete_in_batches(
                "app_usage_daily",
                f"SELECT rowid FROM app_usage_daily WHERE {synced} AND usage_date < ?",
                (synced_cutoff[:10],),
            ),
        }
        deleted["work_sessions"] = self._delete_in_batches(
            "work_sessions",
            f"SELECT rowid FROM work_sessions s WHERE {synced} AND status != 'OPEN' AND ended_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM activity_periods p WHERE p.session_id = s.session_id) "
            "AND NOT EXISTS (SELECT 1 FROM idle_periods i WHERE i.session_id = s.session_id)",
            (synced_cutoff,),
        )
        return deleted


def iso_utc_now() -> str:
    return iso_utc(time.time())
