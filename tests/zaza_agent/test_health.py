"""Component health registry, simulated hook/watcher/storage failures, and
how the agent behaves when input hooks can't be trusted.

Hook installation is driven with fake ``user32``/``kernel32`` objects, so a
failed ``SetWindowsHookExW`` can be simulated on any machine without a real
Windows session."""

from __future__ import annotations

import ctypes
import sqlite3

import pytest

from deskmate.zaza.agent import ActivityAgent
from deskmate.zaza.config import AgentConfig
from deskmate.zaza.health import (
    COMPONENTS,
    KEYBOARD_HOOK,
    MOUSE_HOOK,
    SESSION_LOCK,
    STORAGE,
    WINDOW_WATCH,
    HealthRegistry,
    HealthState,
)
from deskmate.zaza.input_hooks import WH_KEYBOARD_LL, WH_MOUSE_LL, WM_KEYDOWN, ZazaInputHooks
from deskmate.zaza.recorder import ActivityRecorder
from deskmate.zaza.session_lock import SessionLockWatcher
from deskmate.zaza.storage import ActivityStore
from deskmate.zaza.window_watch import WindowSample

from .conftest import tick_for

ERROR_ACCESS_DENIED = 5


class FakeKernel32:
    def GetModuleHandleW(self, _name):  # noqa: N802, ANN001
        return 1

    def GetLastError(self):  # noqa: N802
        return ERROR_ACCESS_DENIED


class FakeUser32:
    """``SetWindowsHookExW`` succeeds only for the hook ids in ``succeed``."""

    def __init__(self, succeed: set[int]) -> None:
        self._succeed = succeed
        self.next_calls: list[tuple] = []

    def SetWindowsHookExW(self, hook_id, _proc, _mod, _tid):  # noqa: N802, ANN001
        return 0x1000 + hook_id if hook_id in self._succeed else 0

    def CallNextHookEx(self, *args):  # noqa: N802, ANN002
        self.next_calls.append(args)
        return 0


def _install(hooks: ZazaInputHooks, succeed: set[int]) -> tuple[int, int]:
    return hooks._install(FakeUser32(succeed), FakeKernel32(), hookproc_factory=lambda f: f)


def _all_healthy(registry: HealthRegistry, *, except_: dict[str, HealthState] | None = None) -> None:
    for name in COMPONENTS:
        registry.set(name, HealthState.HEALTHY, "ok")
    for name, state in (except_ or {}).items():
        registry.set(name, state, "simulated")


@pytest.fixture
def agent(make_agent, clock):
    a = make_agent(healthy_hooks=False)
    a._clock = clock
    return a


def _rows(agent: ActivityAgent, event_type: str) -> list[dict]:
    return [r for r in agent.store.recent_events(limit=1000) if r["event_type"] == event_type]


# ─── registry semantics ────────────────────────────────────────────────────


def test_components_start_degraded_not_started():
    registry = HealthRegistry()
    for name in (KEYBOARD_HOOK, MOUSE_HOOK, SESSION_LOCK, WINDOW_WATCH, STORAGE):
        entry = registry.get(name)
        assert entry.state == HealthState.DEGRADED
        assert entry.detail == "not started"
    assert registry.overall() == HealthState.DEGRADED


def test_overall_healthy_only_when_every_component_healthy():
    registry = HealthRegistry()
    _all_healthy(registry)
    assert registry.overall() == HealthState.HEALTHY
    registry.set(SESSION_LOCK, HealthState.ERROR, "x")
    assert registry.overall() == HealthState.DEGRADED


def test_one_failed_input_hook_is_degraded_and_untrusted():
    registry = HealthRegistry()
    _all_healthy(registry, except_={KEYBOARD_HOOK: HealthState.ERROR})
    assert registry.overall() == HealthState.DEGRADED
    assert registry.input_trustworthy() is False


def test_both_input_hooks_failed_is_error():
    registry = HealthRegistry()
    _all_healthy(registry, except_={KEYBOARD_HOOK: HealthState.ERROR, MOUSE_HOOK: HealthState.ERROR})
    assert registry.overall() == HealthState.ERROR


def test_storage_failure_is_error():
    registry = HealthRegistry()
    _all_healthy(registry, except_={STORAGE: HealthState.ERROR})
    assert registry.overall() == HealthState.ERROR


def test_on_change_fires_only_on_actual_change():
    seen = []
    registry = HealthRegistry(on_change=seen.append)
    registry.set(STORAGE, HealthState.HEALTHY, "ready")
    registry.set(STORAGE, HealthState.HEALTHY, "ready")
    registry.set(STORAGE, HealthState.ERROR, "boom")
    assert [(e.component, e.state) for e in seen] == [
        (STORAGE, HealthState.HEALTHY),
        (STORAGE, HealthState.ERROR),
    ]


def test_unknown_component_rejected():
    with pytest.raises(ValueError):
        HealthRegistry().set("screenshot", HealthState.HEALTHY)


def test_format_table_lists_every_component():
    registry = HealthRegistry()
    registry.set(KEYBOARD_HOOK, HealthState.ERROR, "SetWindowsHookExW failed (GetLastError=5)")
    table = registry.format_table()
    for name in COMPONENTS:
        assert name in table
    assert "ERROR" in table and "GetLastError=5" in table


# ─── simulated hook installation ───────────────────────────────────────────


def test_both_hooks_install_successfully_report_healthy():
    registry = HealthRegistry()
    hooks = ZazaInputHooks(ActivityRecorder(idle_threshold_seconds=300), health=registry)
    khook, mhook = _install(hooks, {WH_KEYBOARD_LL, WH_MOUSE_LL})
    assert khook and mhook
    assert registry.get(KEYBOARD_HOOK).state == HealthState.HEALTHY
    assert registry.get(MOUSE_HOOK).state == HealthState.HEALTHY


def test_both_hook_installs_failing_report_error_with_last_error():
    registry = HealthRegistry()
    hooks = ZazaInputHooks(ActivityRecorder(idle_threshold_seconds=300), health=registry)
    khook, mhook = _install(hooks, set())
    assert (khook, mhook) == (0, 0)
    for name in (KEYBOARD_HOOK, MOUSE_HOOK):
        entry = registry.get(name)
        assert entry.state == HealthState.ERROR
        assert f"GetLastError={ERROR_ACCESS_DENIED}" in entry.detail
    _all_healthy(registry, except_={KEYBOARD_HOOK: HealthState.ERROR, MOUSE_HOOK: HealthState.ERROR})
    assert registry.overall() == HealthState.ERROR


def test_keyboard_only_failure_leaves_mouse_healthy():
    registry = HealthRegistry()
    hooks = ZazaInputHooks(ActivityRecorder(idle_threshold_seconds=300), health=registry)
    _install(hooks, {WH_MOUSE_LL})
    assert registry.get(KEYBOARD_HOOK).state == HealthState.ERROR
    assert registry.get(MOUSE_HOOK).state == HealthState.HEALTHY


def test_hook_thread_crash_reports_error(monkeypatch):
    registry = HealthRegistry()
    hooks = ZazaInputHooks(ActivityRecorder(idle_threshold_seconds=300), health=registry)

    def boom():
        raise OSError("user32 unavailable")

    monkeypatch.setattr(hooks, "_run_message_loop", boom)
    hooks._run()
    assert registry.get(KEYBOARD_HOOK).state == HealthState.ERROR
    assert registry.get(MOUSE_HOOK).state == HealthState.ERROR


def test_hooks_unavailable_platform_is_error_not_silent(monkeypatch):
    monkeypatch.setattr(ZazaInputHooks, "available", property(lambda self: False))
    registry = HealthRegistry()
    ZazaInputHooks(ActivityRecorder(idle_threshold_seconds=300), health=registry).start()
    assert registry.get(KEYBOARD_HOOK).state == HealthState.ERROR
    assert registry.get(MOUSE_HOOK).state == HealthState.ERROR


def test_keyboard_callback_forwards_lparam_untouched():
    """Callback only classifies wparam and forwards lparam opaquely. lparam=0
    here is a NULL pointer; any attempt to read a struct through it would
    fail rather than record activity."""
    recorder = ActivityRecorder(idle_threshold_seconds=300)
    hooks = ZazaInputHooks(recorder)
    user32 = FakeUser32({WH_KEYBOARD_LL, WH_MOUSE_LL})
    hooks._install(user32, FakeKernel32(), hookproc_factory=lambda f: f)
    hooks._kb_callback(0, WM_KEYDOWN, 0)
    assert recorder.snapshot_and_reset() == (True, False)
    assert user32.next_calls == [(0, 0, WM_KEYDOWN, 0)]


# ─── agent behavior when hooks have failed ─────────────────────────────────


def test_failed_hooks_do_not_look_like_genuine_inactivity(agent):
    hooks = ZazaInputHooks(agent.recorder, health=agent.health)
    _install(hooks, set())

    agent.tick()
    tick_for(agent, agent._clock, 301)

    assert _rows(agent, "IDLE") == []
    for row in _rows(agent, "ACTIVITY"):
        assert row["keyboard_active"] is None
        assert row["mouse_active"] is None
        assert row["idle"] is None
    assert agent.health_status() == HealthState.ERROR


def test_keyboard_hook_failure_alone_still_suppresses_idle(agent):
    hooks = ZazaInputHooks(agent.recorder, health=agent.health)
    _install(hooks, {WH_MOUSE_LL})

    agent.tick()
    tick_for(agent, agent._clock, 301)
    agent.recorder.record_mouse()
    agent._clock.advance(10)
    agent.tick()

    assert _rows(agent, "IDLE") == []
    latest = _rows(agent, "ACTIVITY")[0]
    assert latest["keyboard_active"] is None
    assert latest["mouse_active"] == 1
    assert latest["idle"] is None
    assert agent.health_status() == HealthState.DEGRADED


def test_healthy_hooks_still_produce_idle(agent):
    hooks = ZazaInputHooks(agent.recorder, health=agent.health)
    _install(hooks, {WH_KEYBOARD_LL, WH_MOUSE_LL})
    agent.tick()
    tick_for(agent, agent._clock, 301)
    assert len(_rows(agent, "IDLE")) == 1
    assert _rows(agent, "ACTIVITY")[0]["idle"] == 1


# ─── other components ──────────────────────────────────────────────────────


def test_session_lock_watcher_failure_reports_error(monkeypatch):
    registry = HealthRegistry()
    watcher = SessionLockWatcher(on_event=lambda _n: None, health=registry)

    def boom():
        raise OSError("wtsapi32 unavailable")

    monkeypatch.setattr(watcher, "_run_message_loop", boom)
    watcher._run()
    assert registry.get(SESSION_LOCK).state == HealthState.ERROR


def test_session_lock_unavailable_platform_is_error(monkeypatch):
    monkeypatch.setattr(SessionLockWatcher, "available", property(lambda self: False))
    registry = HealthRegistry()
    SessionLockWatcher(on_event=lambda _n: None, health=registry).start()
    assert registry.get(SESSION_LOCK).state == HealthState.ERROR


def test_window_probe_failure_reports_error_but_activity_still_written(tmp_path):
    def broken_probe():
        raise OSError("GetForegroundWindow failed")

    config = AgentConfig(db_path=str(tmp_path / "activity.db"))
    store = ActivityStore(config.db_path)
    a = ActivityAgent(config, store=store, window_probe=broken_probe)
    a.tick()
    assert a.health.get(WINDOW_WATCH).state == HealthState.ERROR
    assert _rows(a, "ACTIVITY")
    store.close()


def test_window_probe_success_reports_healthy(agent):
    agent.tick()
    assert agent.health.get(WINDOW_WATCH).state == HealthState.HEALTHY


def test_storage_healthy_after_open_and_error_after_write_failure(agent):
    assert agent.health.get(STORAGE).state == HealthState.HEALTHY
    agent.store.close()
    with pytest.raises(sqlite3.Error):
        agent.tick()
    assert agent.health.get(STORAGE).state == HealthState.ERROR
    assert agent.health_status() == HealthState.ERROR


def test_lresult_is_pointer_sized():
    from deskmate.zaza import input_hooks, session_lock

    for mod in (input_hooks, session_lock):
        assert mod.LRESULT is ctypes.c_ssize_t
        assert ctypes.sizeof(mod.LRESULT) == ctypes.sizeof(ctypes.c_void_p)
