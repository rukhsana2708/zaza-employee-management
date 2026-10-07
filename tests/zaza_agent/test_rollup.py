"""Daily application-usage rollups built from activity periods."""

from __future__ import annotations

from datetime import date, datetime, timezone

from deskmate.zaza.rollup import compute_day, format_duration, format_usage
from deskmate.zaza.storage import ActivityStore

from .conftest import tick_for


def _busy(agent, clock, seconds, step=10):
    remaining = seconds
    while remaining > 0:
        clock.advance(step)
        agent.recorder.record_keyboard()
        agent.tick()
        remaining -= step


def _usage(agent, day="2026-10-07"):
    return {u["app_name"]: u for u in agent.store.app_usage(day)}


def test_app_usage_rollup_sums_active_time_per_app(agent, clock, probe):
    agent.recorder.record_keyboard()
    agent.tick()
    _busy(agent, clock, 120)  # Code 120s
    probe.set("chrome.exe", "Docs")
    _busy(agent, clock, 60)  # Chrome 60s
    probe.set("Code.exe", "agent.py - zaza - Visual Studio Code")
    _busy(agent, clock, 30)  # Code +30s
    agent.end_session()
    usage = _usage(agent)
    assert usage["Code.exe"]["active_seconds"] == 150
    assert usage["Code.exe"]["period_count"] == 2
    assert usage["chrome.exe"]["active_seconds"] == 60
    assert format_usage(list(usage.values())).splitlines()[0].strip().startswith("Code.exe")


def test_rollup_separates_idle_and_unknown_and_skips_locked(agent, clock):
    agent.recorder.record_keyboard()
    agent.tick()
    _busy(agent, clock, 60)
    tick_for(agent, clock, 400)  # active through T0+360 (grace), idle T0+360 to T0+460
    agent._on_session_event("LOCK")
    tick_for(agent, clock, 100)
    agent.end_session()
    code = _usage(agent)["Code.exe"]
    assert code["active_seconds"] == 360
    assert code["idle_seconds"] == 100
    assert code["unknown_seconds"] == 0
    assert set(_usage(agent)) == {"Code.exe"}  # LOCKED periods have no app


def test_rollup_matches_period_totals(agent, clock, probe):
    agent.recorder.record_keyboard()
    agent.tick()
    for app in ("a.exe", "b.exe", "a.exe", "c.exe"):
        probe.set(app, "x")
        _busy(agent, clock, 40)
    agent.end_session()
    total_usage = sum(u["active_seconds"] for u in agent.store.app_usage())
    total_periods = sum(p["duration_seconds"] for p in agent.store.periods() if p["status"] == "ACTIVE")
    assert total_usage == total_periods


def test_rollup_splits_period_across_midnight(make_agent, clock):
    clock.now = datetime(2026, 10, 7, 23, 59, tzinfo=timezone.utc).timestamp()
    agent = make_agent()
    agent.recorder.record_keyboard()
    agent.tick()
    _busy(agent, clock, 120)
    agent.end_session()
    assert _usage(agent, "2026-10-07")["Code.exe"]["active_seconds"] == 60
    assert _usage(agent, "2026-10-08")["Code.exe"]["active_seconds"] == 60


def test_usage_id_is_deterministic_and_recompute_does_not_requeue(agent, clock):
    agent.recorder.record_keyboard()
    agent.tick()
    _busy(agent, clock, 30)
    agent.end_session()
    row = _usage(agent)["Code.exe"]
    assert row["usage_id"] == ActivityStore.usage_id("device-1", "2026-10-07", "Code.exe")
    assert row["usage_id"] != ActivityStore.usage_id("device-1", "2026-10-07", "chrome.exe")
    agent.store.mark_synced("app_usage_daily", [(row["usage_id"], row["record_version"])], at=clock.now)
    agent._on_period_closed(clock.now - 30, clock.now)  # recompute with no new data
    again = _usage(agent)["Code.exe"]
    assert again["record_version"] == row["record_version"]
    assert again["sync_status"] == "SYNCED"


def test_new_activity_requeues_synced_rollup(agent, clock):
    agent.recorder.record_keyboard()
    agent.tick()
    _busy(agent, clock, 30)
    agent.end_session()
    row = _usage(agent)["Code.exe"]
    agent.store.mark_synced("app_usage_daily", [(row["usage_id"], row["record_version"])], at=clock.now)
    clock.advance(10)
    agent.recorder.record_keyboard()
    agent.tick()
    _busy(agent, clock, 20)
    agent.end_session()
    again = _usage(agent)["Code.exe"]
    assert again["usage_id"] == row["usage_id"]
    assert again["active_seconds"] == 50
    assert again["sync_status"] == "PENDING"
    assert again["record_version"] > row["record_version"]


def test_compute_day_ignores_other_devices(agent, clock):
    agent.recorder.record_keyboard()
    agent.tick()
    _busy(agent, clock, 30)
    agent.end_session()
    assert compute_day(agent.store, date(2026, 10, 7), device_id="other-device", tz=timezone.utc) == {}


def test_format_duration():
    assert format_duration(3 * 3600 + 42 * 60) == "3h 42m"
    assert format_duration(38 * 60) == "38m 00s"
    assert format_duration(40) == "40s"
