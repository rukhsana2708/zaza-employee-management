"""Work sessions: creation, clean closure, crash/interrupted-session recovery,
and persistence across agent restarts."""

from __future__ import annotations

import uuid

from deskmate.zaza.timeutil import parse_iso

from .conftest import T0, tick_for


def _busy(agent, clock, seconds, step=10):
    agent.recorder.record_keyboard()
    agent.tick()
    remaining = seconds
    while remaining > 0:
        clock.advance(step)
        agent.recorder.record_keyboard()
        agent.tick()
        remaining -= step


def test_session_created_on_begin(agent):
    session_id = agent.begin_session()
    session = agent.store.get_session(session_id)
    assert uuid.UUID(session["session_id"])
    assert session["status"] == "OPEN"
    assert session["start_reason"] == "AGENT_START"
    assert session["previous_session_id"] is None
    assert parse_iso(session["started_at"]) == T0
    assert session["ended_at"] is None
    assert session["device_id"] == "device-1" and session["employee_id"] == "emp-1"
    assert session["sync_status"] == "PENDING"
    assert [e["event_type"] for e in agent.store.recent_events(10)] == ["SESSION_START"]


def test_begin_session_is_idempotent_while_open(agent):
    assert agent.begin_session() == agent.begin_session()
    assert len(agent.store.sessions()) == 1


def test_clean_session_closure_finalizes_totals(agent, clock):
    _busy(agent, clock, 60)
    tick_for(agent, clock, 400)  # grace until T0+360, then idle
    session_id = agent.session_id
    agent.end_session()
    assert agent.session_id is None
    session = agent.store.get_session(session_id)
    assert session["status"] == "CLOSED"
    assert session["end_reason"] == "AGENT_STOP"
    assert parse_iso(session["ended_at"]) == T0 + 460
    assert session["active_seconds"] == 360
    assert session["idle_seconds"] == 100
    assert session["unknown_seconds"] == 0
    assert session["tracked_seconds"] == 460
    assert session["tracked_seconds"] == (
        session["active_seconds"] + session["idle_seconds"] + session["unknown_seconds"] + session["locked_seconds"]
    )
    periods = agent.store.periods(session_id)
    assert all(p["is_open"] == 0 for p in periods)
    assert periods[-1]["end_reason"] == "SESSION_END"
    idle = agent.store.idle_periods(session_id)
    assert idle[0]["is_open"] == 0 and idle[0]["end_reason"] == "SESSION_END"
    assert agent.store.recent_events(1)[0]["event_type"] == "SESSION_END"


def test_tick_after_close_starts_a_new_session(agent, clock):
    agent.tick()
    first = agent.session_id
    agent.end_session()
    clock.advance(10)
    agent.tick()
    assert agent.session_id != first
    assert len(agent.store.sessions()) == 2


def test_crash_recovery_marks_session_interrupted_without_inventing_time(make_agent, clock):
    crashed = make_agent()
    _busy(crashed, clock, 100)  # last heartbeat at T0+100
    crashed_id = crashed.session_id
    crashed.store.close()  # process dies: no end_session()

    clock.advance(3600)  # agent is down for an hour
    restarted = make_agent()
    new_id = restarted.begin_session()

    old = restarted.store.get_session(crashed_id)
    assert old["status"] == "INTERRUPTED"
    assert old["end_reason"] == "AGENT_INTERRUPTED"
    assert parse_iso(old["ended_at"]) == T0 + 100  # nothing after the last heartbeat
    assert old["active_seconds"] == 100
    assert old["tracked_seconds"] == 100
    old_periods = restarted.store.periods(crashed_id)
    assert all(p["is_open"] == 0 for p in old_periods)
    assert old_periods[-1]["end_reason"] == "INTERRUPTED"
    assert max(parse_iso(p["ended_at"]) for p in old_periods) == T0 + 100

    new = restarted.store.get_session(new_id)
    assert new["status"] == "OPEN"
    assert new["start_reason"] == "AGENT_START_AFTER_INTERRUPTION"
    assert new["previous_session_id"] == crashed_id
    assert parse_iso(new["started_at"]) == T0 + 3700

    # The new session starts UNKNOWN: there's no evidence of activity yet.
    restarted.tick()
    assert restarted.store.periods(new_id)[0]["status"] == "UNKNOWN"


def test_crash_recovery_closes_open_idle_period(make_agent, clock):
    crashed = make_agent()
    _busy(crashed, clock, 10)
    tick_for(crashed, clock, 400)
    crashed_id = crashed.session_id
    crashed.store.close()
    clock.advance(600)
    restarted = make_agent()
    restarted.begin_session()
    idle = restarted.store.idle_periods(crashed_id)
    assert idle[0]["is_open"] == 0 and idle[0]["end_reason"] == "INTERRUPTED"
    assert parse_iso(idle[0]["ended_at"]) == T0 + 410


def test_crash_recovery_rolls_up_app_usage(make_agent, clock):
    crashed = make_agent()
    _busy(crashed, clock, 120)
    crashed.store.close()
    restarted = make_agent()
    restarted.begin_session()
    usage = restarted.store.app_usage("2026-10-07")
    assert [(u["app_name"], u["active_seconds"]) for u in usage] == [("Code.exe", 120)]


def test_data_persists_across_clean_restart(make_agent, clock):
    first = make_agent()
    _busy(first, clock, 50)
    first_id = first.session_id
    first.end_session()
    first.store.close()

    clock.advance(60)
    second = make_agent()
    second.begin_session()
    sessions = second.store.sessions()
    assert [s["session_id"] for s in sessions][0] == first_id
    assert sessions[0]["status"] == "CLOSED"  # not touched by recovery
    assert sessions[1]["start_reason"] == "AGENT_START"
    assert second.store.periods(first_id)[0]["duration_seconds"] == 50


def test_clock_stepping_backwards_starts_a_new_session(agent, clock):
    _busy(agent, clock, 60)
    first = agent.session_id
    clock.advance(-3600)  # clock corrected back an hour
    agent.recorder.record_keyboard()
    agent.tick()
    old = agent.store.get_session(first)
    assert old["status"] == "CLOSED" and old["end_reason"] == "CLOCK_CHANGED"
    assert parse_iso(old["ended_at"]) == T0 + 60
    assert agent.session_id != first
    for p in agent.store.periods():
        assert parse_iso(p["ended_at"]) >= parse_iso(p["started_at"])


def test_tiny_clock_jitter_backwards_is_absorbed(agent, clock):
    _busy(agent, clock, 30)
    first = agent.session_id
    clock.advance(-1)
    agent.recorder.record_keyboard()
    agent.tick()
    assert agent.session_id == first
    for p in agent.store.periods():
        assert p["duration_seconds"] >= 0
