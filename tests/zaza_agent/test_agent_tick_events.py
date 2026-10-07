"""End-to-end (but offline/local) behavior of ActivityAgent.tick(): app
detection, keyboard/mouse boolean events, and idle/continue transitions,
all verified through what actually lands in the local SQLite store."""

from __future__ import annotations

import pytest

from deskmate.zaza.agent import ActivityAgent

from .conftest import Probe, tick_for


@pytest.fixture
def agent(make_agent, probe: Probe):
    probe.set("notepad.exe", "Untitled - Notepad")
    return make_agent()


def _event_types(agent: ActivityAgent) -> list[str]:
    return [row["event_type"] for row in reversed(agent.store.recent_events(limit=1000))]


def test_app_change_detected_on_first_tick(agent, clock):
    agent.tick()
    assert "APP_CHANGE" in _event_types(agent)
    rows = agent.store.recent_events(limit=1000)
    app_change = [r for r in rows if r["event_type"] == "APP_CHANGE"][0]
    assert app_change["app_name"] == "notepad.exe"
    assert app_change["window_title"] == "Untitled - Notepad"


def test_no_duplicate_app_change_when_window_unchanged(agent, clock):
    agent.tick()
    agent.tick()
    rows = agent.store.recent_events(limit=1000)
    app_changes = [r for r in rows if r["event_type"] == "APP_CHANGE"]
    assert len(app_changes) == 1


def test_keyboard_activity_produces_boolean_only_event(agent, clock):
    agent.recorder.record_keyboard()
    agent.tick()
    rows = agent.store.recent_events(limit=1000)
    activity_rows = [r for r in rows if r["event_type"] == "ACTIVITY"]
    assert len(activity_rows) == 1
    row = activity_rows[0]
    assert row["keyboard_active"] == 1
    assert row["mouse_active"] == 0
    # no column exists for the actual key, so there's nothing beyond the flag
    assert set(row.keys()) == {
        "id", "ts", "event_type", "app_name", "window_title", "keyboard_active", "mouse_active", "idle",
        "event_id", "session_id", "status", "privacy_excluded",
    }


def test_mouse_activity_produces_boolean_only_event(agent, clock):
    agent.recorder.record_mouse()
    agent.tick()
    rows = agent.store.recent_events(limit=1000)
    activity_rows = [r for r in rows if r["event_type"] == "ACTIVITY"]
    assert activity_rows[0]["mouse_active"] == 1
    assert activity_rows[0]["keyboard_active"] == 0


def test_idle_transition_after_configured_threshold(agent, clock):
    agent.tick()  # establishes baseline, not yet idle
    assert "IDLE" not in _event_types(agent)
    tick_for(agent, clock, 301)
    assert "IDLE" in _event_types(agent)


def test_idle_event_fires_only_once_while_remaining_idle(agent, clock):
    agent.tick()
    tick_for(agent, clock, 301)
    agent.tick()
    idle_events = [r for r in agent.store.recent_events(limit=1000) if r["event_type"] == "IDLE"]
    assert len(idle_events) == 1


def test_return_from_idle_to_active_emits_continue(agent, clock):
    agent.tick()
    tick_for(agent, clock, 301)  # now idle
    agent.recorder.record_keyboard()
    agent.tick()
    assert "CONTINUE" in _event_types(agent)


def test_lock_and_unlock_events_are_recorded_verbatim(agent, clock):
    agent._on_session_event("LOCK")
    agent._on_session_event("UNLOCK")
    types = _event_types(agent)
    assert types.count("LOCK") == 1
    assert types.count("UNLOCK") == 1
