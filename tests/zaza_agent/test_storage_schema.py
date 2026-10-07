"""Storage-layer privacy guards: the schema has no column for prohibited
content, and insert_event's fixed keyword signature cannot be used to smuggle
any in, even if a caller tries."""

from __future__ import annotations

import sqlite3

import pytest

from deskmate.zaza.storage import ActivityStore


@pytest.fixture
def store(tmp_path):
    db_file = tmp_path / "activity.db"
    s = ActivityStore(db_file)
    yield s
    s.close()


def test_schema_has_no_prohibited_columns(store):
    conn: sqlite3.Connection = store._conn  # inspecting the real schema, not a mock
    cols = {row["name"].lower() for row in conn.execute("PRAGMA table_info(activity_events)").fetchall()}
    expected = {
        "id", "ts", "event_type", "app_name", "window_title", "keyboard_active", "mouse_active", "idle",
        "event_id", "session_id", "status", "privacy_excluded",
    }
    assert cols == expected
    prohibited_exact_names = {
        "text", "key_char", "key_code", "clipboard_text", "clipboard",
        "screenshot", "screenshot_path", "frame", "image", "ocr_text",
        "audio", "video", "password",
    }
    assert cols.isdisjoint(prohibited_exact_names)


def test_insert_event_rejects_unknown_keyword_clipboard_text(store):
    with pytest.raises(TypeError):
        store.insert_event(event_type="ACTIVITY", clipboard_text="something copied")  # type: ignore[call-arg]


def test_insert_event_rejects_unknown_keyword_key_char(store):
    with pytest.raises(TypeError):
        store.insert_event(event_type="ACTIVITY", key_char="a")  # type: ignore[call-arg]


def test_insert_event_rejects_unknown_keyword_screenshot_path(store):
    with pytest.raises(TypeError):
        store.insert_event(event_type="ACTIVITY", screenshot_path="/tmp/shot.png")  # type: ignore[call-arg]


def test_insert_event_rejects_unknown_event_type(store):
    with pytest.raises(ValueError):
        store.insert_event(event_type="SCREENSHOT")


def test_insert_and_read_back_only_allowed_fields(store):
    store.insert_event(
        event_type="ACTIVITY",
        app_name="notepad.exe",
        window_title="Untitled - Notepad",
        keyboard_active=True,
        mouse_active=False,
        idle=False,
    )
    rows = store.recent_events(limit=1)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "ACTIVITY"
    assert row["app_name"] == "notepad.exe"
    assert row["keyboard_active"] == 1
    assert row["mouse_active"] == 0
    assert set(row.keys()) == {
        "id", "ts", "event_type", "app_name", "window_title", "keyboard_active", "mouse_active", "idle",
        "event_id", "session_id", "status", "privacy_excluded",
    }


# ─── Phase 2: every table, not just raw events ─────────────────────────────

PROHIBITED_SUBSTRINGS = (
    "clipboard", "screenshot", "screen_", "image", "frame", "ocr", "audio", "video", "webcam", "microphone",
    "password", "keystroke", "key_char", "key_code", "vk_code", "scan_code", "typed", "text_content",
    "url", "path", "query", "history", "content", "body",
)

EXPECTED_COLUMNS = {
    "work_sessions": {
        "session_id", "device_id", "employee_id", "local_seq", "started_at", "ended_at", "last_heartbeat_at",
        "status", "start_reason", "end_reason", "previous_session_id", "tracked_seconds", "active_seconds",
        "idle_seconds", "unknown_seconds", "locked_seconds", "created_at", "updated_at",
    },
    "activity_periods": {
        "period_id", "session_id", "device_id", "local_seq", "started_at", "ended_at", "duration_seconds",
        "is_open", "status", "status_detail", "app_name", "window_title", "domain", "privacy_excluded",
        "start_reason", "end_reason", "created_at", "updated_at",
    },
    "idle_periods": {
        "idle_id", "session_id", "device_id", "local_seq", "started_at", "ended_at", "duration_seconds",
        "is_open", "end_reason", "created_at", "updated_at",
    },
    "app_usage_daily": {
        "usage_id", "device_id", "employee_id", "local_seq", "usage_date", "app_name", "active_seconds",
        "idle_seconds", "unknown_seconds", "period_count", "created_at", "updated_at",
    },
    "sync_state": {"key", "value", "updated_at"},
    "counters": {"name", "value"},
    "schema_meta": {"key", "value"},
}
SYNC_COLUMNS = {
    "record_version", "sync_status", "sync_attempts", "last_sync_attempt_at", "last_sync_error",
    "synced_at", "synced_version",
}


def _columns(store, table):
    return {row["name"].lower() for row in store._conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_every_table_has_exactly_the_reviewed_columns(store):
    tables = {r["name"] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()}
    assert tables == set(EXPECTED_COLUMNS) | {"activity_events"}
    for table, expected in EXPECTED_COLUMNS.items():
        extra = SYNC_COLUMNS if table in ("work_sessions", "activity_periods", "idle_periods", "app_usage_daily") else set()
        assert _columns(store, table) == expected | extra, table


def test_no_column_anywhere_looks_like_prohibited_content(store):
    offenders = []
    for table in [*EXPECTED_COLUMNS, "activity_events"]:
        for col in _columns(store, table):
            if any(bad in col for bad in PROHIBITED_SUBSTRINGS):
                offenders.append(f"{table}.{col}")
    assert not offenders, offenders


@pytest.mark.parametrize("bad_kwarg", ["window_text", "typed_text", "clipboard_text", "screenshot_path", "url"])
def test_period_writer_rejects_unknown_content_keywords(store, bad_kwarg):
    with pytest.raises(TypeError):
        store.insert_period(  # type: ignore[call-arg]
            period_id="p", session_id="s", device_id="d", started_at=0, ended_at=1,
            status="ACTIVE", start_reason="x", **{bad_kwarg: "something"},
        )
