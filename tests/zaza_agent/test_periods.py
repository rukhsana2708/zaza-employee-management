"""Activity-period aggregation: merging, splitting, ACTIVE / IDLE / UNKNOWN /
LOCKED transitions, idle periods, telemetry gaps, and the invariant that a
session's periods tile it exactly."""

from __future__ import annotations

from deskmate.zaza.health import KEYBOARD_HOOK, HealthState
from deskmate.zaza.timeutil import parse_iso

from .conftest import T0, mark_hooks, tick_for


def _periods(agent):
    return agent.store.periods(agent.session_id)


def _active_ticks(agent, clock, seconds, step=10):
    """User is busy: input just before every tick."""
    remaining = seconds
    while remaining > 0:
        clock.advance(step)
        agent.recorder.record_keyboard()
        agent.tick()
        remaining -= step


def _start_active(agent):
    agent.recorder.record_keyboard()
    agent.tick()


def _assert_contiguous(agent):
    periods = _periods(agent)
    for prev, nxt in zip(periods, periods[1:]):
        assert prev["ended_at"] == nxt["started_at"], (prev, nxt)
    session = agent.store.get_session(agent.session_id)
    total = sum(p["duration_seconds"] for p in periods)
    assert abs(total - session["tracked_seconds"]) < 1e-6


# ─── merging and splitting ─────────────────────────────────────────────────


def test_consecutive_identical_activity_merges_into_one_period(agent, clock):
    _start_active(agent)
    _active_ticks(agent, clock, 40)
    periods = _periods(agent)
    assert len(periods) == 1
    p = periods[0]
    assert p["status"] == "ACTIVE"
    assert p["app_name"] == "Code.exe"
    assert parse_iso(p["started_at"]) == T0
    assert parse_iso(p["ended_at"]) == T0 + 40
    assert p["duration_seconds"] == 40
    assert p["is_open"] == 1
    # raw rows still exist for each tick, but reporting uses the one period
    assert len([e for e in agent.store.recent_events(1000) if e["event_type"] == "ACTIVITY"]) == 5


def test_app_change_creates_new_period(agent, clock, probe):
    _start_active(agent)
    _active_ticks(agent, clock, 20)
    probe.set("chrome.exe", "Docs - Google Chrome")
    _active_ticks(agent, clock, 30)
    periods = _periods(agent)
    assert [p["app_name"] for p in periods] == ["Code.exe", "chrome.exe"]
    first, second = periods
    assert first["is_open"] == 0 and first["end_reason"] == "APP_CHANGE"
    assert second["start_reason"] == "APP_CHANGE"
    assert parse_iso(first["ended_at"]) == T0 + 30  # switch observed at this tick
    assert first["ended_at"] == second["started_at"]
    _assert_contiguous(agent)


def test_brief_title_flicker_does_not_split_period(agent, clock, probe):
    _start_active(agent)
    probe.set("Code.exe", "other.py - zaza - Visual Studio Code")
    _active_ticks(agent, clock, 10)
    probe.set("Code.exe", "agent.py - zaza - Visual Studio Code")
    _active_ticks(agent, clock, 40)
    assert len(_periods(agent)) == 1


def test_title_noise_is_normalized_away(agent, clock, probe):
    probe.set("OUTLOOK.EXE", "(3) Inbox - Outlook")
    _start_active(agent)
    probe.set("OUTLOOK.EXE", "(4) Inbox - Outlook")
    _active_ticks(agent, clock, 60)
    probe.set("OUTLOOK.EXE", "● Inbox - Outlook")
    _active_ticks(agent, clock, 60)
    periods = _periods(agent)
    assert len(periods) == 1
    assert periods[0]["window_title"] == "Inbox - Outlook"


def test_stable_title_change_splits_where_it_was_first_seen(agent, clock, probe):
    _start_active(agent)
    _active_ticks(agent, clock, 20)
    probe.set("Code.exe", "README.md - zaza - Visual Studio Code")
    _active_ticks(agent, clock, 60)
    periods = _periods(agent)
    assert len(periods) == 2
    assert periods[0]["end_reason"] == "WINDOW_CHANGE"
    assert parse_iso(periods[1]["started_at"]) == T0 + 30
    assert periods[1]["window_title"] == "README.md - zaza - Visual Studio Code"
    _assert_contiguous(agent)


# ─── ACTIVE / IDLE ─────────────────────────────────────────────────────────


def test_active_to_idle_transition_starts_idle_after_grace_period(agent, clock):
    _start_active(agent)
    _active_ticks(agent, clock, 60)  # last input at T0+60
    tick_for(agent, clock, 400)
    periods = _periods(agent)
    assert [p["status"] for p in periods] == ["ACTIVE", "IDLE"]
    # the 300s threshold is a grace period: still ACTIVE until T0+60+300
    assert parse_iso(periods[0]["ended_at"]) == T0 + 360
    assert parse_iso(periods[1]["started_at"]) == T0 + 360
    idle = agent.store.idle_periods(agent.session_id)
    assert len(idle) == 1 and idle[0]["is_open"] == 1
    assert parse_iso(idle[0]["started_at"]) == T0 + 360
    assert idle[0]["duration_seconds"] == 100
    _assert_contiguous(agent)


def test_idle_to_active_closes_idle_period_cleanly(agent, clock):
    _start_active(agent)
    _active_ticks(agent, clock, 60)
    tick_for(agent, clock, 400)  # idle from T0+360
    clock.advance(5)
    agent.recorder.record_mouse()  # user returns at T0+465
    clock.advance(5)
    agent.tick()
    periods = _periods(agent)
    assert [p["status"] for p in periods] == ["ACTIVE", "IDLE", "ACTIVE"]
    assert parse_iso(periods[1]["started_at"]) == T0 + 360
    assert parse_iso(periods[1]["ended_at"]) == T0 + 465
    assert parse_iso(periods[2]["started_at"]) == T0 + 465
    idle = agent.store.idle_periods(agent.session_id)
    assert len(idle) == 1
    assert idle[0]["is_open"] == 0
    assert idle[0]["end_reason"] == "ACTIVITY_RESUMED"
    assert idle[0]["duration_seconds"] == 105
    _assert_contiguous(agent)


def test_idle_period_duration_matches_idle_activity_periods(agent, clock, probe):
    _start_active(agent)
    tick_for(agent, clock, 350)
    probe.set("explorer.exe", "Desktop")  # focus changes while idle
    tick_for(agent, clock, 100)
    agent.recorder.record_keyboard()
    clock.advance(10)
    agent.tick()
    idle = agent.store.idle_periods(agent.session_id)
    assert len(idle) == 1  # app switch while idle does not split the idle period
    idle_from_periods = sum(p["duration_seconds"] for p in _periods(agent) if p["status"] == "IDLE")
    assert idle[0]["duration_seconds"] == idle_from_periods


def test_pause_shorter_than_threshold_is_not_idle(agent, clock):
    _start_active(agent)
    tick_for(agent, clock, 290)
    _active_ticks(agent, clock, 30)
    assert agent.store.idle_periods(agent.session_id) == []
    assert [p["status"] for p in _periods(agent)] == ["ACTIVE"]
    assert agent.store.get_session(agent.session_id)["idle_seconds"] == 0


def test_idle_threshold_is_configurable(make_agent, clock):
    agent = make_agent(idle_threshold_seconds=60)
    _start_active(agent)
    tick_for(agent, clock, 70)
    assert [p["status"] for p in _periods(agent)] == ["ACTIVE", "IDLE"]


# ─── UNKNOWN: monitoring not trustworthy ───────────────────────────────────


def test_unknown_when_hooks_not_started(make_agent, clock):
    agent = make_agent(healthy_hooks=False)  # hooks DEGRADED "not started"
    agent.tick()
    tick_for(agent, clock, 600)
    periods = _periods(agent)
    assert [(p["status"], p["status_detail"]) for p in periods] == [("UNKNOWN", "MONITORING_UNAVAILABLE")]
    assert agent.store.idle_periods(agent.session_id) == []
    session = agent.store.get_session(agent.session_id)
    assert session["idle_seconds"] == 0
    assert session["unknown_seconds"] == 600
    assert not [e for e in agent.store.recent_events(1000) if e["event_type"] == "IDLE"]


def test_degraded_hook_is_not_counted_as_idle(agent, clock):
    _start_active(agent)
    _active_ticks(agent, clock, 30)
    agent.health.set(KEYBOARD_HOOK, HealthState.DEGRADED, "simulated restart")
    tick_for(agent, clock, 900)
    periods = _periods(agent)
    assert [p["status"] for p in periods] == ["ACTIVE", "UNKNOWN"]
    assert periods[1]["status_detail"] == "MONITORING_UNAVAILABLE"
    assert periods[0]["end_reason"] == "STATUS_CHANGE"
    assert agent.store.idle_periods(agent.session_id) == []
    latest = agent.store.recent_events(1)[0]
    assert latest["keyboard_active"] is None and latest["idle"] is None


def test_error_hook_mid_idle_closes_idle_period(agent, clock):
    _start_active(agent)
    tick_for(agent, clock, 400)
    mark_hooks(agent, HealthState.ERROR)
    tick_for(agent, clock, 30)
    idle = agent.store.idle_periods(agent.session_id)
    assert len(idle) == 1 and idle[0]["is_open"] == 0
    assert idle[0]["end_reason"] == "MONITORING_UNKNOWN"
    assert _periods(agent)[-1]["status"] == "UNKNOWN"


def test_idle_after_recovery_only_counts_trusted_time(agent, clock):
    _start_active(agent)
    mark_hooks(agent, HealthState.ERROR)
    tick_for(agent, clock, 600)  # 600s unknown
    mark_hooks(agent, HealthState.HEALTHY)
    recovered_at = clock.now + 10
    tick_for(agent, clock, 200)
    assert _periods(agent)[-1]["status_detail"] == "AWAITING_INPUT"  # not idle yet
    tick_for(agent, clock, 200)
    idle_period = _periods(agent)[-1]
    assert idle_period["status"] == "IDLE"
    # trusted again at recovered_at; idle only after a full threshold of that
    assert parse_iso(idle_period["started_at"]) == recovered_at + 300


def test_new_session_is_unknown_until_input_seen(agent, clock):
    agent.tick()
    tick_for(agent, clock, 60)
    assert _periods(agent)[0]["status_detail"] == "AWAITING_INPUT"
    agent.recorder.record_keyboard()
    clock.advance(10)
    agent.tick()
    assert [p["status"] for p in _periods(agent)] == ["UNKNOWN", "ACTIVE"]


# ─── LOCK / UNLOCK ─────────────────────────────────────────────────────────


def test_lock_and_unlock_create_locked_period(agent, clock):
    _start_active(agent)
    _active_ticks(agent, clock, 30)
    clock.advance(3)
    agent._on_session_event("LOCK")
    tick_for(agent, clock, 600)  # locked far longer than the idle threshold
    agent._on_session_event("UNLOCK")
    _active_ticks(agent, clock, 20)
    periods = _periods(agent)
    assert [p["status"] for p in periods] == ["ACTIVE", "LOCKED", "ACTIVE"]
    locked = periods[1]
    assert locked["start_reason"] == "LOCK" and locked["end_reason"] == "UNLOCK"
    assert locked["app_name"] is None and locked["window_title"] is None
    assert locked["duration_seconds"] == 600
    assert agent.store.idle_periods(agent.session_id) == []  # locked is not idle
    session = agent.store.get_session(agent.session_id)
    assert session["locked_seconds"] == 600 and session["idle_seconds"] == 0
    _assert_contiguous(agent)


def test_lock_closes_open_idle_period(agent, clock):
    _start_active(agent)
    tick_for(agent, clock, 400)
    agent._on_session_event("LOCK")
    idle = agent.store.idle_periods(agent.session_id)
    assert idle[0]["is_open"] == 0 and idle[0]["end_reason"] == "LOCK"


# ─── telemetry gaps ────────────────────────────────────────────────────────


def test_telemetry_gap_becomes_unknown_not_activity(agent, clock):
    _start_active(agent)
    _active_ticks(agent, clock, 30)
    agent.recorder.record_keyboard()  # last input right before the machine sleeps
    clock.advance(3600)
    agent.tick()
    periods = _periods(agent)
    gap = [p for p in periods if p["status_detail"] == "TELEMETRY_GAP"]
    assert len(gap) == 1
    assert gap[0]["duration_seconds"] == 3600 and gap[0]["is_open"] == 0
    # input from before the gap doesn't make the user look active after it
    assert periods[-1]["status"] == "UNKNOWN" and periods[-1]["status_detail"] == "AWAITING_INPUT"
    session = agent.store.get_session(agent.session_id)
    assert session["active_seconds"] == 30
    assert session["idle_seconds"] == 0
    _assert_contiguous(agent)
