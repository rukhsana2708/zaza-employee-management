"""Retention cleanup and Phase 3 sync-readiness metadata (no network)."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from deskmate.zaza.schema import SYNCED_TABLES
from deskmate.zaza.timeutil import iso_utc

from .conftest import T0, tick_for

DAY = 86400


def _busy_session(agent, clock, seconds=60):
    agent.recorder.record_keyboard()
    agent.tick()
    remaining = seconds
    while remaining > 0:
        clock.advance(10)
        agent.recorder.record_keyboard()
        agent.tick()
        remaining -= 10
    tick_for(agent, clock, 400)
    session_id = agent.session_id
    agent.end_session()
    return session_id


def _mark_all_synced(store, at):
    for table in SYNCED_TABLES:
        rows = store.pending_sync(table, limit=10_000)
        id_col = {"work_sessions": "session_id", "activity_periods": "period_id",
                  "idle_periods": "idle_id", "app_usage_daily": "usage_id"}[table]
        store.mark_synced(table, [(r[id_col], r["record_version"]) for r in rows], at=at)


def _counts(store):
    return {t: store._query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in (*SYNCED_TABLES, "activity_events")}


# ─── retention ─────────────────────────────────────────────────────────────


def test_raw_events_older_than_retention_are_deleted(agent):
    store = agent.store
    store.insert_event(event_type="ACTIVITY", ts=iso_utc(T0 - 8 * DAY))
    store.insert_event(event_type="ACTIVITY", ts=iso_utc(T0 - 6 * DAY))
    deleted = agent.run_retention(now=T0)
    assert deleted["activity_events"] == 1
    assert [e["ts"] for e in store.recent_events()] == [iso_utc(T0 - 6 * DAY)]


def test_raw_retention_period_is_configurable(make_agent):
    agent = make_agent(raw_retention_days=2)
    agent.store.insert_event(event_type="ACTIVITY", ts=iso_utc(T0 - 3 * DAY))
    assert agent.run_retention(now=T0)["activity_events"] == 1


def test_long_running_open_session_old_raw_events_deleted_summaries_kept(make_agent, clock):
    """Regression (review finding 3): an agent running for weeks must not
    keep raw events forever just because its session is still OPEN."""
    agent = make_agent(raw_retention_days=7)
    agent.recorder.record_keyboard()
    agent.tick()
    session_id = agent.session_id
    old_event = agent.store.insert_event(
        event_type="ACTIVITY", ts=iso_utc(clock.now - 8 * DAY), session_id=session_id, status="ACTIVE"
    )
    for _ in range(3):
        clock.advance(10)
        agent.recorder.record_keyboard()
        agent.tick()
    recent_ids = {e["id"] for e in agent.store.recent_events(1000)} - {old_event}
    periods_before = agent.store.periods(session_id)

    deleted = agent.run_retention(now=clock.now)

    assert deleted["activity_events"] == 1
    remaining = {e["id"] for e in agent.store.recent_events(1000)}
    assert old_event not in remaining
    assert remaining == recent_ids  # current/recent raw events stay
    assert agent.store.get_session(session_id)["status"] == "OPEN"
    assert agent.store.periods(session_id) == periods_before  # summaries untouched
    assert all(deleted[t] == 0 for t in SYNCED_TABLES)  # unsynced summaries never deleted
    clock.advance(10)
    agent.recorder.record_keyboard()
    agent.tick()  # aggregation carries on without the raw history
    assert len(agent.store.periods(session_id)) == 1


def test_crash_recovery_works_without_any_raw_events(make_agent, clock):
    crashed = make_agent()
    crashed.recorder.record_keyboard()
    crashed.tick()
    for _ in range(6):
        clock.advance(10)
        crashed.recorder.record_keyboard()
        crashed.tick()
    crashed_id = crashed.session_id
    crashed.store._conn.execute("DELETE FROM activity_events")  # retention removed everything raw
    crashed.store.close()

    restarted = make_agent()
    restarted.begin_session()
    old = restarted.store.get_session(crashed_id)
    assert old["status"] == "INTERRUPTED"
    assert old["active_seconds"] == 60
    assert all(p["is_open"] == 0 for p in restarted.store.periods(crashed_id))


def test_unsynced_records_are_never_deleted(agent, clock):
    _busy_session(agent, clock)
    before = _counts(agent.store)
    deleted = agent.run_retention(now=clock.now + 3650 * DAY)  # ten years later
    assert deleted["activity_events"] > 0  # raw events are not sync records
    for table in SYNCED_TABLES:
        assert deleted[table] == 0
    after = _counts(agent.store)
    assert {t: after[t] for t in SYNCED_TABLES} == {t: before[t] for t in SYNCED_TABLES}


def test_failed_sync_records_are_never_deleted(agent, clock):
    _busy_session(agent, clock)
    periods = agent.store.pending_sync("activity_periods")
    agent.store.mark_sync_failed("activity_periods", [p["period_id"] for p in periods], error="timeout", at=clock.now)
    deleted = agent.run_retention(now=clock.now + 3650 * DAY)
    assert deleted["activity_periods"] == 0


def test_synced_records_kept_during_safety_window_then_deleted(agent, clock):
    session_id = _busy_session(agent, clock)
    _mark_all_synced(agent.store, clock.now)
    assert all(v == 0 for k, v in agent.run_retention(now=clock.now + 29 * DAY).items() if k in SYNCED_TABLES)
    deleted = agent.run_retention(now=clock.now + 31 * DAY)
    for table in SYNCED_TABLES:
        assert deleted[table] > 0, table
    assert agent.store.get_session(session_id) is None


def test_session_kept_while_any_child_record_remains(agent, clock):
    session_id = _busy_session(agent, clock)
    _mark_all_synced(agent.store, clock.now)
    # one period gets re-queued (e.g. a correction) — it and its session stay
    period = agent.store.periods(session_id)[0]
    agent.store.update_period_end(period["period_id"], started_at=T0, ended_at=T0 + 60, close_reason="SESSION_END")
    agent.run_retention(now=clock.now + 365 * DAY)
    assert agent.store.get_period(period["period_id"]) is not None
    assert agent.store.get_session(session_id) is not None


# ─── sync metadata ─────────────────────────────────────────────────────────


def test_pending_sync_excludes_open_records_by_default(agent, clock):
    agent.recorder.record_keyboard()
    agent.tick()
    assert agent.store.pending_sync("activity_periods") == []
    assert agent.store.pending_sync("work_sessions") == []
    assert len(agent.store.pending_sync("activity_periods", include_open=True)) == 1
    agent.end_session()
    assert len(agent.store.pending_sync("activity_periods")) == 1
    assert len(agent.store.pending_sync("work_sessions")) == 1


def test_mark_synced_only_for_the_version_that_was_sent(agent, clock):
    agent.recorder.record_keyboard()
    agent.tick()
    row = agent.store.pending_sync("activity_periods", include_open=True)[0]
    clock.advance(10)
    agent.recorder.record_keyboard()
    agent.tick()  # period extended -> new version while "upload" was in flight
    assert agent.store.mark_synced("activity_periods", [(row["period_id"], row["record_version"])], at=clock.now) == 0
    current = agent.store.get_period(row["period_id"])
    assert current["sync_status"] == "PENDING"
    assert agent.store.mark_synced(
        "activity_periods", [(row["period_id"], current["record_version"])], at=clock.now
    ) == 1
    synced = agent.store.get_period(row["period_id"])
    assert synced["sync_status"] == "SYNCED" and synced["synced_version"] == current["record_version"]


def test_sync_failure_tracks_attempts_and_error(agent, clock):
    _busy_session(agent, clock)
    pid = agent.store.pending_sync("activity_periods")[0]["period_id"]
    agent.store.mark_sync_failed("activity_periods", [pid], error="HTTP 503", at=clock.now)
    agent.store.mark_sync_failed("activity_periods", [pid], error="HTTP 503", at=clock.now + 60)
    row = agent.store.get_period(pid)
    assert row["sync_status"] == "FAILED"
    assert row["sync_attempts"] == 2
    assert row["last_sync_error"] == "HTTP 503"
    assert row["last_sync_attempt_at"] == iso_utc(clock.now + 60)
    assert pid in [r["period_id"] for r in agent.store.pending_sync("activity_periods")]  # retried


def test_update_after_sync_requeues_record(agent, clock):
    _busy_session(agent, clock)
    _mark_all_synced(agent.store, clock.now)
    period = agent.store.periods()[0]
    agent.store.update_period_end(period["period_id"], started_at=T0, ended_at=T0 + 60, close_reason="SESSION_END")
    assert agent.store.get_period(period["period_id"])["sync_status"] == "PENDING"


def test_sync_state_key_value(agent):
    assert agent.store.get_sync_state("last_successful_sync_at") is None
    agent.store.set_sync_state("last_successful_sync_at", "2026-10-07T09:00:00.000+00:00", at=T0)
    assert agent.store.get_sync_state("last_successful_sync_at") == "2026-10-07T09:00:00.000+00:00"


def test_unknown_table_rejected(agent):
    with pytest.raises(ValueError):
        agent.store.pending_sync("activity_events")


# ─── unique IDs and device order ───────────────────────────────────────────


def test_all_sync_records_have_unique_uuid_ids_and_increasing_sequence(agent, clock, probe):
    agent.recorder.record_keyboard()
    agent.tick()
    for app in ("a.exe", "b.exe", "c.exe"):
        probe.set(app, "x")
        clock.advance(10)
        agent.recorder.record_keyboard()
        agent.tick()
    tick_for(agent, clock, 400)
    agent.end_session()
    seqs = []
    for table, id_col in (("work_sessions", "session_id"), ("activity_periods", "period_id"),
                          ("idle_periods", "idle_id"), ("app_usage_daily", "usage_id")):
        rows = agent.store._query(f"SELECT {id_col} AS id, local_seq FROM {table}")
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)), table
        for record_id in ids:
            uuid.UUID(record_id)
        seqs.extend(r["local_seq"] for r in rows)
    assert len(seqs) == len(set(seqs))  # one device-wide sequence, no reuse
    periods = agent.store.periods()
    assert [p["local_seq"] for p in periods] == sorted(p["local_seq"] for p in periods)


def test_raw_event_ids_are_unique_and_enforced(agent):
    agent.store.insert_event(event_type="ACTIVITY")
    agent.store.insert_event(event_type="ACTIVITY")
    ids = [e["event_id"] for e in agent.store.recent_events()]
    assert len(set(ids)) == 2
    with pytest.raises(sqlite3.IntegrityError):
        agent.store._conn.execute(
            "INSERT INTO activity_events(event_id, ts, event_type) VALUES (?, 'x', 'ACTIVITY')", (ids[0],)
        )


def test_failed_tick_rolls_back_and_aggregator_recovers(agent, clock, monkeypatch):
    agent.recorder.record_keyboard()
    agent.tick()
    original = agent.store.heartbeat_session

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(agent.store, "heartbeat_session", boom)
    clock.advance(10)
    with pytest.raises(sqlite3.OperationalError):
        agent.tick()
    monkeypatch.setattr(agent.store, "heartbeat_session", original)
    clock.advance(10)
    agent.recorder.record_keyboard()
    agent.tick()
    periods = agent.store.periods(agent.session_id)
    assert len(periods) == 1 and periods[0]["duration_seconds"] == 20
