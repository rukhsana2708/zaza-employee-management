"""Regression tests for the Phase 2 review findings:

1. The idle threshold is a grace period: IDLE starts at last input +
   threshold, never at the last input.
2. Session-lock watcher health gates IDLE: an untrustworthy (or stale) lock
   state can never become IDLE or a stale LOCKED.
(3. Raw-event retention during long OPEN sessions is covered in
   test_retention_sync.py.)
"""

from __future__ import annotations

from deskmate.zaza.health import KEYBOARD_HOOK, SESSION_LOCK, HealthRegistry, HealthState
from deskmate.zaza.session_lock import SessionLockWatcher, classify_session_flags
from deskmate.zaza.timeutil import parse_iso

from .conftest import T0, mark_hooks, mark_lock_watcher, tick_for


def _periods(agent):
    return agent.store.periods(agent.session_id)


def _statuses(agent):
    return [(p["status"], p["status_detail"]) for p in _periods(agent)]


def _idle_total(agent):
    return sum(i["duration_seconds"] for i in agent.store.idle_periods(agent.session_id))


def _input_then_tick(agent, clock, step=10):
    clock.advance(step)
    agent.recorder.record_keyboard()
    agent.tick()


def _start_with_input(agent):
    agent.recorder.record_keyboard()
    agent.tick()


# ─── finding 1: idle threshold is a grace period ───────────────────────────


def test_299_seconds_without_input_is_not_idle(agent, clock):
    _start_with_input(agent)
    tick_for(agent, clock, 299)
    assert [s for s, _ in _statuses(agent)] == ["ACTIVE"]
    assert agent.store.idle_periods(agent.session_id) == []


def test_310_seconds_without_input_is_about_10_seconds_idle(agent, clock):
    _start_with_input(agent)
    tick_for(agent, clock, 310)
    periods = _periods(agent)
    assert [p["status"] for p in periods] == ["ACTIVE", "IDLE"]
    assert periods[0]["duration_seconds"] == 300
    assert periods[1]["duration_seconds"] == 10
    assert _idle_total(agent) == 10


def test_returning_one_minute_after_threshold_counts_one_minute_idle(agent, clock):
    # Last input 10:00 (T0), threshold reached 10:05, employee returns 10:06.
    _start_with_input(agent)
    tick_for(agent, clock, 350)
    clock.advance(10)
    agent.recorder.record_mouse()  # return at T0+360
    agent.tick()
    periods = _periods(agent)
    assert [p["status"] for p in periods] == ["ACTIVE", "IDLE", "ACTIVE"]
    active, idle, resumed = periods
    assert (parse_iso(active["started_at"]), parse_iso(active["ended_at"])) == (T0, T0 + 300)
    assert (parse_iso(idle["started_at"]), parse_iso(idle["ended_at"])) == (T0 + 300, T0 + 360)
    assert parse_iso(resumed["started_at"]) == T0 + 360
    assert _idle_total(agent) == 60
    session = agent.store.get_session(agent.session_id)
    assert session["idle_seconds"] == 60
    assert session["active_seconds"] == 300


def test_multiple_idle_episodes_do_not_each_add_the_grace_period(agent, clock):
    _start_with_input(agent)
    for _ in range(3):
        tick_for(agent, clock, 350)
        _input_then_tick(agent, clock)  # returns 360s after last input: 300s grace + 60s idle
    idle = agent.store.idle_periods(agent.session_id)
    assert len(idle) == 3
    assert [i["duration_seconds"] for i in idle] == [60, 60, 60]
    session = agent.store.get_session(agent.session_id)
    assert session["idle_seconds"] == 180
    assert session["active_seconds"] == session["tracked_seconds"] - 180


def test_startup_without_input_keeps_pre_threshold_time_unknown(agent, clock):
    agent.tick()  # trusted from T0, no input ever
    tick_for(agent, clock, 360)
    periods = _periods(agent)
    assert [(p["status"], p["status_detail"]) for p in periods] == [
        ("UNKNOWN", "AWAITING_INPUT"),
        ("IDLE", None),
    ]
    unknown, idle = periods
    assert parse_iso(unknown["ended_at"]) == T0 + 300  # not converted to idle
    assert unknown["duration_seconds"] == 300
    assert parse_iso(idle["started_at"]) == T0 + 300
    assert idle["duration_seconds"] == 60
    session = agent.store.get_session(agent.session_id)
    assert session["unknown_seconds"] == 300 and session["idle_seconds"] == 60


def test_custom_threshold_is_also_a_grace_period(make_agent, clock):
    agent = make_agent(idle_threshold_seconds=60)
    _start_with_input(agent)
    tick_for(agent, clock, 90)
    assert [p["duration_seconds"] for p in _periods(agent)] == [60, 30]


# ─── finding 2: lock-watcher health gates IDLE ─────────────────────────────


def test_lock_watcher_error_and_no_input_is_unknown_not_idle(make_agent, clock):
    agent = make_agent(healthy_lock=False)
    agent.health.set(SESSION_LOCK, HealthState.ERROR, "WTSRegisterSessionNotification failed")
    _start_with_input(agent)
    tick_for(agent, clock, 900)
    statuses = _statuses(agent)
    assert ("UNKNOWN", "LOCK_STATE_UNAVAILABLE") in statuses
    assert all(s != "IDLE" for s, _ in statuses)
    assert agent.store.idle_periods(agent.session_id) == []
    assert agent.store.get_session(agent.session_id)["idle_seconds"] == 0
    assert not [e for e in agent.store.recent_events(1000) if e["event_type"] == "IDLE"]


def test_lock_watcher_degraded_and_no_input_is_unknown(make_agent, clock):
    agent = make_agent(healthy_lock=False)  # watcher DEGRADED "not started"
    agent.tick()
    tick_for(agent, clock, 900)
    assert _statuses(agent) == [("UNKNOWN", "LOCK_STATE_UNAVAILABLE")]
    assert agent.store.idle_periods(agent.session_id) == []


def test_lock_watcher_failing_mid_session_stops_idle_inference(agent, clock):
    _start_with_input(agent)
    tick_for(agent, clock, 100)
    agent.health.set(SESSION_LOCK, HealthState.ERROR, "watcher thread died")
    tick_for(agent, clock, 900)
    assert [s for s, _ in _statuses(agent)] == ["ACTIVE", "UNKNOWN"]
    assert _periods(agent)[1]["status_detail"] == "LOCK_STATE_UNAVAILABLE"
    assert _idle_total(agent) == 0


def test_lock_watcher_recovery_restores_normal_classification(make_agent, clock):
    agent = make_agent(healthy_lock=False)
    agent.health.set(SESSION_LOCK, HealthState.ERROR, "simulated")
    _start_with_input(agent)
    tick_for(agent, clock, 400)
    assert _periods(agent)[-1]["status_detail"] == "LOCK_STATE_UNAVAILABLE"
    recovered_at = clock.now + 10
    clock.advance(10)
    mark_lock_watcher(agent, locked=False)  # re-registered, reports "unlocked"
    agent.tick()
    tick_for(agent, clock, 400)
    idle = _periods(agent)[-1]
    assert idle["status"] == "IDLE"
    # idle only after a full threshold of trustworthy time
    assert parse_iso(idle["started_at"]) == recovered_at + 300


def test_recovered_watcher_without_fresh_state_is_not_trusted(make_agent, clock):
    agent = make_agent()  # lock state established while HEALTHY
    agent.health.set(SESSION_LOCK, HealthState.ERROR, "outage")
    agent.health.set(SESSION_LOCK, HealthState.HEALTHY, "back, but no state report")
    assert agent.lock_state_trusted() is False  # pre-outage state is stale
    agent.tick()
    tick_for(agent, clock, 600)
    assert all(s != "IDLE" for s, _ in _statuses(agent))


def test_known_lock_while_watcher_healthy_is_locked(agent, clock):
    _start_with_input(agent)
    clock.advance(10)
    agent._on_session_event("LOCK")
    tick_for(agent, clock, 600)
    assert [s for s, _ in _statuses(agent)] == ["ACTIVE", "LOCKED"]
    assert _idle_total(agent) == 0


def test_stale_locked_state_is_not_trusted_after_watcher_failure(agent, clock):
    _start_with_input(agent)
    clock.advance(10)
    agent._on_session_event("LOCK")
    tick_for(agent, clock, 60)
    agent.health.set(SESSION_LOCK, HealthState.ERROR, "watcher died while locked")
    tick_for(agent, clock, 600)  # an UNLOCK may have been missed
    statuses = _statuses(agent)
    assert statuses[-1] == ("UNKNOWN", "LOCK_STATE_UNAVAILABLE")  # not LOCKED, not IDLE
    assert _idle_total(agent) == 0
    _input_then_tick(agent, clock)  # input proves the user is at the desktop
    assert _statuses(agent)[-1] == ("ACTIVE", None)


def test_normal_lock_unlock_cycle_still_works(agent, clock):
    _start_with_input(agent)
    clock.advance(10)
    agent._on_session_event("LOCK")
    tick_for(agent, clock, 120)
    agent._on_session_event("UNLOCK")
    tick_for(agent, clock, 20)
    assert [s for s, _ in _statuses(agent)] == ["ACTIVE", "LOCKED", "ACTIVE"]
    locked = _periods(agent)[1]
    assert locked["start_reason"] == "LOCK" and locked["end_reason"] == "UNLOCK"
    assert locked["duration_seconds"] == 120


def test_agent_started_while_session_locked_reports_locked(make_agent, clock):
    agent = make_agent(healthy_lock=False)
    mark_lock_watcher(agent, locked=True)  # initial state query: locked
    agent.tick()
    tick_for(agent, clock, 600)
    assert _statuses(agent) == [("LOCKED", None)]


def test_hook_protection_still_takes_precedence(agent, clock):
    _start_with_input(agent)
    agent.health.set(KEYBOARD_HOOK, HealthState.ERROR, "simulated")
    tick_for(agent, clock, 600)
    assert _statuses(agent)[-1] == ("UNKNOWN", "MONITORING_UNAVAILABLE")
    mark_hooks(agent)
    assert agent.lock_state_trusted() is True


# ─── watcher side ──────────────────────────────────────────────────────────


def test_session_flags_classification():
    assert classify_session_flags(0) is True
    assert classify_session_flags(1) is False
    assert classify_session_flags(-1) is None


def test_watcher_healthy_and_reports_state_after_registration():
    registry = HealthRegistry()
    reported = []

    def on_state(locked):
        # state must arrive after HEALTHY, so it belongs to this healthy stint
        reported.append((locked, registry.get(SESSION_LOCK).state, registry.generation(SESSION_LOCK)))

    watcher = SessionLockWatcher(on_event=lambda n: None, health=registry, on_state=on_state,
                                 query_locked=lambda: True)
    watcher._established()
    assert registry.get(SESSION_LOCK).state == HealthState.HEALTHY
    assert reported == [(True, HealthState.HEALTHY, registry.generation(SESSION_LOCK))]


def test_watcher_unknown_initial_state_stays_degraded_until_event():
    registry = HealthRegistry()
    events = []
    watcher = SessionLockWatcher(on_event=events.append, health=registry, query_locked=lambda: None)
    watcher._established()
    assert registry.get(SESSION_LOCK).state == HealthState.DEGRADED
    watcher._deliver("UNLOCK")  # a real notification is definitive
    assert registry.get(SESSION_LOCK).state == HealthState.HEALTHY
    assert events == ["UNLOCK"]


def test_watcher_query_failure_is_not_fatal():
    registry = HealthRegistry()

    def boom():
        raise OSError("wtsapi32 unavailable")

    SessionLockWatcher(on_event=lambda n: None, health=registry, query_locked=boom)._established()
    assert registry.get(SESSION_LOCK).state == HealthState.DEGRADED


def test_health_generation_changes_only_on_state_change():
    registry = HealthRegistry()
    g0 = registry.generation(SESSION_LOCK)
    registry.set(SESSION_LOCK, HealthState.HEALTHY, "a")
    g1 = registry.generation(SESSION_LOCK)
    registry.set(SESSION_LOCK, HealthState.HEALTHY, "b")  # detail only
    assert registry.generation(SESSION_LOCK) == g1 != g0
    registry.set(SESSION_LOCK, HealthState.ERROR, "x")
    registry.set(SESSION_LOCK, HealthState.HEALTHY, "a")
    assert registry.generation(SESSION_LOCK) == g1 + 2
