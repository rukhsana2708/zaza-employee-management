"""Schema versioning and migrations, including upgrading a Phase 1 (v1)
database in place without losing its rows."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from deskmate.zaza import schema
from deskmate.zaza.storage import SCHEMA_VERSION, ActivityStore

PHASE1_SQL = """
CREATE TABLE activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    app_name TEXT,
    window_title TEXT,
    keyboard_active INTEGER,
    mouse_active INTEGER,
    idle INTEGER
);
CREATE INDEX idx_activity_events_ts ON activity_events(ts);
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1');
INSERT INTO activity_events(ts, event_type, app_name, window_title, keyboard_active, mouse_active, idle)
VALUES ('2026-10-06T09:00:00+00:00', 'APP_CHANGE', 'notepad.exe', 'Untitled', NULL, NULL, NULL),
       ('2026-10-06T09:00:10+00:00', 'ACTIVITY', NULL, NULL, 1, 0, 0);
"""


def _make_v1(path):
    conn = sqlite3.connect(path)
    conn.executescript(PHASE1_SQL)
    conn.commit()
    conn.close()


def _tables(store):
    return {r["name"] for r in store._query("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_fresh_database_is_current_version_with_wal(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    assert store.schema_version() == SCHEMA_VERSION == 2
    assert store.journal_mode() == "wal"
    assert {"activity_events", "work_sessions", "activity_periods", "idle_periods", "app_usage_daily",
            "sync_state", "counters", "schema_meta"} <= _tables(store)
    store.close()


def test_phase1_database_upgrades_in_place(tmp_path):
    path = tmp_path / "a.db"
    _make_v1(path)
    store = ActivityStore(path)
    assert store.schema_version() == 2
    rows = list(reversed(store.recent_events()))
    assert [(r["event_type"], r["app_name"]) for r in rows] == [("APP_CHANGE", "notepad.exe"), ("ACTIVITY", None)]
    assert rows[1]["keyboard_active"] == 1
    ids = [r["event_id"] for r in rows]
    assert len(set(ids)) == 2 and all(uuid.UUID(i) for i in ids)
    assert rows[0]["privacy_excluded"] == 0
    assert "work_sessions" in _tables(store)
    store.close()


def test_reopening_current_database_is_a_no_op(tmp_path):
    path = tmp_path / "a.db"
    store = ActivityStore(path)
    store.insert_event(event_type="ACTIVITY")
    store.close()
    store = ActivityStore(path)
    assert store.count_events() == 1
    assert store.schema_version() == 2
    store.close()


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "a.db"
    ActivityStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        ActivityStore(path)


def test_failed_migration_rolls_back_to_previous_version(tmp_path, monkeypatch):
    path = tmp_path / "a.db"
    _make_v1(path)

    def broken(conn):
        conn.execute("ALTER TABLE activity_events ADD COLUMN event_id TEXT")
        raise sqlite3.OperationalError("simulated failure mid-migration")

    monkeypatch.setitem(schema.MIGRATIONS, 2, broken)
    with pytest.raises(sqlite3.OperationalError):
        ActivityStore(path)
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(activity_events)")}
    version = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
    conn.close()
    assert "event_id" not in cols
    assert version == "1"

    monkeypatch.undo()
    store = ActivityStore(path)  # a later start completes the upgrade
    assert store.schema_version() == 2
    assert store.count_events() == 2
    store.close()


def test_session_status_and_period_status_are_constrained(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_session(session_id="s", device_id="d", employee_id="e", started_at=0, start_reason="x")
        store._conn.execute("UPDATE work_sessions SET status = 'BOGUS'")
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_period(period_id="p", session_id="s", device_id="d", started_at=0, ended_at=1,
                            status="WORKING", start_reason="x")
    store.close()
