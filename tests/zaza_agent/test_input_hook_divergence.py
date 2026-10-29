"""A silently removed input hook must read as UNKNOWN, never IDLE."""

from __future__ import annotations

from datetime import timezone

from deskmate.zaza.agent import ActivityAgent
from deskmate.zaza.config import AgentConfig
from deskmate.zaza.os_input import os_seconds_since_input
from deskmate.zaza.storage import ActivityStore

from .conftest import mark_hooks, mark_lock_watcher


def _agent(tmp_path, clock, probe, os_idle):
    config = AgentConfig(employee_id="e", device_id="d", db_path=str(tmp_path / "a.db"))
    agent = ActivityAgent(
        config, store=ActivityStore(config.db_path), window_probe=probe,
        wall_clock=clock, mono_clock=clock, tz=timezone.utc, os_idle_probe=lambda: os_idle[0],
    )
    mark_hooks(agent)
    mark_lock_watcher(agent)
    return agent


def test_hooks_silent_while_os_sees_input_is_unknown_not_idle(tmp_path, clock, probe):
    os_idle = [1.0]
    agent = _agent(tmp_path, clock, probe, os_idle)
    agent.begin_session()
    for _ in range(400):  # well past the 5 minute threshold
        clock.advance(1)
        agent._tick(None)
        status, detail, _ = agent._classify(clock(), clock())
        assert status != "IDLE"
    assert (status, detail) == ("UNKNOWN", "INPUT_HOOK_DIVERGENCE")


def test_idle_still_works_when_os_agrees(tmp_path, clock, probe):
    os_idle = [999.0]
    agent = _agent(tmp_path, clock, probe, os_idle)
    agent.begin_session()
    for _ in range(400):
        clock.advance(1)
        agent._tick(None)
    assert agent._classify(clock(), clock())[0] == "IDLE"


def test_unknown_os_idle_changes_nothing(tmp_path, clock, probe):
    agent = _agent(tmp_path, clock, probe, [None])
    assert agent._hooks_diverge(None, 300) is False


def test_os_probe_is_a_number_or_none():
    value = os_seconds_since_input()
    assert value is None or value >= 0
