"""SQLite schema and forward-only migrations for the local activity store.

Version history:

- **v1** (Phase 1): ``activity_events`` raw event log + ``schema_meta``.
- **v2** (Phase 2): stable IDs/session/status on raw events; summarized
  ``activity_periods``, ``idle_periods``, ``work_sessions``,
  ``app_usage_daily``; per-record sync metadata; a device-local sequence
  counter; ``sync_state`` key/value for Phase 3.

Each migration runs in its own transaction and bumps
``schema_meta.schema_version``, so an interrupted upgrade leaves the database
at the last fully applied version. A database newer than this code supports
is refused rather than guessed at.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable

SCHEMA_VERSION = 2

# Tables whose rows are intended for Phase 3 synchronization.
SYNCED_TABLES = ("work_sessions", "activity_periods", "idle_periods", "app_usage_daily")
ID_COLUMNS = {
    "work_sessions": "session_id",
    "activity_periods": "period_id",
    "idle_periods": "idle_id",
    "app_usage_daily": "usage_id",
}

PERIOD_STATUSES = ("ACTIVE", "IDLE", "UNKNOWN", "LOCKED")
SESSION_STATUSES = ("OPEN", "CLOSED", "INTERRUPTED")
SYNC_STATUSES = ("PENDING", "SYNCED", "FAILED")

# Appended to every synced table. ``record_version`` increments on each
# material change; Phase 3 marks a row SYNCED only for the version it sent,
# so a change made mid-upload leaves the row PENDING.
_SYNC_COLUMNS = """
    record_version INTEGER NOT NULL DEFAULT 1,
    sync_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (sync_status IN ('PENDING', 'SYNCED', 'FAILED')),
    sync_attempts INTEGER NOT NULL DEFAULT 0,
    last_sync_attempt_at TEXT,
    last_sync_error TEXT,
    synced_at TEXT,
    synced_version INTEGER"""

_V1 = [
    """
    CREATE TABLE IF NOT EXISTS activity_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        event_type TEXT NOT NULL,
        app_name TEXT,
        window_title TEXT,
        keyboard_active INTEGER,
        mouse_active INTEGER,
        idle INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_activity_events_ts ON activity_events(ts)",
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
]

_V2 = [
    "ALTER TABLE activity_events ADD COLUMN event_id TEXT",
    "ALTER TABLE activity_events ADD COLUMN session_id TEXT",
    "ALTER TABLE activity_events ADD COLUMN status TEXT",
    "ALTER TABLE activity_events ADD COLUMN privacy_excluded INTEGER NOT NULL DEFAULT 0",
    # (event_id backfill for v1 rows happens in _migrate_v2 before this index)
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_activity_events_event_id ON activity_events(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_activity_events_session ON activity_events(session_id)",
    """
    CREATE TABLE counters (
        name TEXT PRIMARY KEY,
        value INTEGER NOT NULL
    )
    """,
    "INSERT INTO counters(name, value) VALUES ('local_seq', 0)",
    f"""
    CREATE TABLE work_sessions (
        session_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        employee_id TEXT NOT NULL,
        local_seq INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        last_heartbeat_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED', 'INTERRUPTED')),
        start_reason TEXT NOT NULL,
        end_reason TEXT,
        previous_session_id TEXT,
        tracked_seconds REAL NOT NULL DEFAULT 0,
        active_seconds REAL NOT NULL DEFAULT 0,
        idle_seconds REAL NOT NULL DEFAULT 0,
        unknown_seconds REAL NOT NULL DEFAULT 0,
        locked_seconds REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,{_SYNC_COLUMNS}
    )
    """,
    "CREATE INDEX idx_work_sessions_status ON work_sessions(status)",
    "CREATE INDEX idx_work_sessions_started ON work_sessions(started_at)",
    "CREATE INDEX idx_work_sessions_sync ON work_sessions(sync_status, local_seq)",
    f"""
    CREATE TABLE activity_periods (
        period_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        device_id TEXT NOT NULL,
        local_seq INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        duration_seconds REAL NOT NULL DEFAULT 0,
        is_open INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'IDLE', 'UNKNOWN', 'LOCKED')),
        status_detail TEXT,
        app_name TEXT,
        window_title TEXT,
        domain TEXT,
        privacy_excluded INTEGER NOT NULL DEFAULT 0,
        start_reason TEXT NOT NULL,
        end_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,{_SYNC_COLUMNS}
    )
    """,
    "CREATE INDEX idx_activity_periods_session ON activity_periods(session_id, started_at)",
    "CREATE INDEX idx_activity_periods_started ON activity_periods(started_at)",
    "CREATE INDEX idx_activity_periods_ended ON activity_periods(ended_at)",
    "CREATE INDEX idx_activity_periods_open ON activity_periods(is_open)",
    "CREATE INDEX idx_activity_periods_sync ON activity_periods(sync_status, local_seq)",
    f"""
    CREATE TABLE idle_periods (
        idle_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        device_id TEXT NOT NULL,
        local_seq INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        duration_seconds REAL NOT NULL DEFAULT 0,
        is_open INTEGER NOT NULL DEFAULT 1,
        end_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,{_SYNC_COLUMNS}
    )
    """,
    "CREATE INDEX idx_idle_periods_session ON idle_periods(session_id, started_at)",
    "CREATE INDEX idx_idle_periods_ended ON idle_periods(ended_at)",
    "CREATE INDEX idx_idle_periods_sync ON idle_periods(sync_status, local_seq)",
    f"""
    CREATE TABLE app_usage_daily (
        usage_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        employee_id TEXT NOT NULL,
        local_seq INTEGER NOT NULL,
        usage_date TEXT NOT NULL,
        app_name TEXT NOT NULL,
        active_seconds REAL NOT NULL DEFAULT 0,
        idle_seconds REAL NOT NULL DEFAULT 0,
        unknown_seconds REAL NOT NULL DEFAULT 0,
        period_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,{_SYNC_COLUMNS},
        UNIQUE (device_id, usage_date, app_name)
    )
    """,
    "CREATE INDEX idx_app_usage_date ON app_usage_daily(usage_date)",
    "CREATE INDEX idx_app_usage_sync ON app_usage_daily(sync_status, local_seq)",
    """
    CREATE TABLE sync_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
]


def _run(conn: sqlite3.Connection, statements: list[str]) -> None:
    for statement in statements:
        conn.execute(statement)


def _migrate_v1(conn: sqlite3.Connection) -> None:
    _run(conn, _V1)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    _run(conn, _V2[:4])
    rows = conn.execute("SELECT id FROM activity_events WHERE event_id IS NULL").fetchall()
    conn.executemany(
        "UPDATE activity_events SET event_id = ? WHERE id = ?",
        [(str(uuid.uuid4()), _row_id(row)) for row in rows],
    )
    _run(conn, _V2[4:])


def _row_id(row) -> int:  # noqa: ANN001
    return row["id"] if isinstance(row, dict) else row[0]


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _migrate_v1, 2: _migrate_v2}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def current_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_meta"):
        return 0
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return 1 if _table_exists(conn, "activity_events") else 0
    value = row["value"] if isinstance(row, dict) else row[0]
    return int(value)


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database up to SCHEMA_VERSION. ``conn`` must be in
    autocommit mode (isolation_level=None); each step is its own transaction."""
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{version} is newer than this agent supports (v{SCHEMA_VERSION})"
        )
    for target in range(version + 1, SCHEMA_VERSION + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            MIGRATIONS[target](conn)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(target),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return SCHEMA_VERSION
