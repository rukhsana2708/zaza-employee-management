"""Session-lock watcher lifecycle (agent 0.9.3).

Defect fixed: a re-enrollment restarts recording inside the SAME process. The
watcher registered a Windows window class once, kept its callback only in a local
variable (freed when the first run ended) and never unregistered the class, so the
second run created its message-only window against a stale class and failed:
``session_lock=ERROR`` / agent DEGRADED. These tests drive the REAL Win32 code
(message-only window + WTS registration) and are skipped off Windows."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import gc
import os
import threading
import time

import pytest

from deskmate.zaza import session_lock
from deskmate.zaza.health import (
    KEYBOARD_HOOK,
    MOUSE_HOOK,
    SESSION_LOCK,
    WINDOW_WATCH,
    HealthRegistry,
    HealthState,
)
from deskmate.zaza.session_lock import SessionLockWatcher

pytestmark = pytest.mark.skipif(os.name != "nt", reason="needs the Win32 window and WTS APIs")

GR_USEROBJECTS = 1


def _wait(cond, timeout: float = 8.0) -> bool:  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _watcher(health=None, events=None, states=None):  # noqa: ANN001, ANN202
    return SessionLockWatcher(
        on_event=(events.append if events is not None else (lambda name: None)),
        health=health or HealthRegistry(),
        on_state=(states.append if states is not None else None),
        query_locked=lambda: False,
    )


def _churn_callbacks() -> list:
    """Free the first run's callback memory and let OTHER ctypes callbacks (the input hooks do this in the real
    agent) reuse it. With the old code the window class then pointed at someone else's code."""
    gc.collect()
    return [session_lock.WNDPROC(lambda *args: 0) for _ in range(500)]


def _healthy(w: SessionLockWatcher) -> bool:
    return w._health.get(SESSION_LOCK).state == HealthState.HEALTHY


def _watcher_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "ZazaSessionLock" and t.is_alive()]


def _user_objects() -> int:
    u = ctypes.windll.user32
    u.GetGuiResources.argtypes = [wt.HANDLE, wt.DWORD]
    u.GetGuiResources.restype = wt.DWORD
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = wt.HANDLE
    return int(u.GetGuiResources(k.GetCurrentProcess(), GR_USEROBJECTS))


def _handle_count() -> int:
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = wt.HANDLE
    k.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    count = wt.DWORD(0)
    k.GetProcessHandleCount(k.GetCurrentProcess(), ctypes.byref(count))
    return int(count.value)


def _class_registered(name: str) -> bool:
    class WNDCLASSEX(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                    ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

    k, u = ctypes.windll.kernel32, ctypes.windll.user32
    k.GetModuleHandleW.restype = wt.HMODULE
    u.GetClassInfoExW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, ctypes.POINTER(WNDCLASSEX)]
    info = WNDCLASSEX()
    info.cbSize = ctypes.sizeof(WNDCLASSEX)
    return bool(u.GetClassInfoExW(k.GetModuleHandleW(None), name, ctypes.byref(info)))


# ─── the defect itself ─────────────────────────────────────────────────────


def test_start_stop_start_with_a_new_watcher_in_the_same_process_stays_healthy():
    """The re-enrollment case: the runner builds a NEW agent (and watcher) in the same process."""
    first = _watcher()
    first.start()
    assert _wait(lambda: _healthy(first))
    first.stop()
    keep = _churn_callbacks()
    second = _watcher()
    second.start()
    try:
        assert _wait(lambda: _healthy(second)), second._health.get(SESSION_LOCK)
        assert second._health.get(SESSION_LOCK).state != HealthState.ERROR
    finally:
        second.stop()
        del keep


def test_the_same_watcher_can_be_restarted_after_stop():
    w = _watcher()
    for _ in range(3):
        w.start()
        assert _wait(lambda: _healthy(w))
        w.stop()
        keep = _churn_callbacks()
        assert w._health.get(SESSION_LOCK).state == HealthState.DEGRADED  # "stopped", not ERROR
        del keep
    assert not _watcher_threads()


def test_many_restart_cycles_leave_no_threads_windows_classes_or_handles():
    w = _watcher()
    w.start()
    assert _wait(lambda: _healthy(w))
    w.stop()  # warm-up so one-time process allocations are not counted
    users, handles = _user_objects(), _handle_count()
    for _ in range(25):
        w.start()
        assert _wait(lambda: _healthy(w))
        w.stop()
        keep = _churn_callbacks()
        del keep
    assert not _watcher_threads()
    assert _user_objects() <= users + 1, "USER objects (windows/classes) leaked"
    assert _handle_count() <= handles + 5, "kernel handles leaked"


def test_the_window_class_is_unregistered_after_stop():
    w = _watcher()
    w.start()
    assert _wait(lambda: _healthy(w))
    name = w._class_name
    assert name and _class_registered(name)
    w.stop()
    assert not _class_registered(name)


def test_no_duplicate_watcher_thread_is_left_behind():
    w = _watcher()
    w.start()
    w.start()  # idempotent: a running watcher is never doubled
    assert _wait(lambda: _healthy(w))
    assert len(_watcher_threads()) == 1
    w.stop()
    assert not _watcher_threads()


def test_stop_is_idempotent_and_safe_before_start():
    w = _watcher()
    w.stop()
    w.stop()
    w.start()
    assert _wait(lambda: _healthy(w))
    w.stop()
    w.stop()
    assert not _watcher_threads()
    assert w._tid == 0  # a stale thread id is never kept (PostThreadMessage must not hit a reused id)


def test_two_watchers_can_coexist_and_stopping_one_does_not_affect_the_other():
    a, b = _watcher(), _watcher()
    a.start()
    b.start()
    assert _wait(lambda: _healthy(a) and _healthy(b))
    assert a._class_name != b._class_name
    a.stop()
    assert _healthy(b) and len(_watcher_threads()) == 1
    b.stop()
    assert not _watcher_threads()


def test_other_components_are_unaffected_by_watcher_cycles():
    health = HealthRegistry()
    for name in (KEYBOARD_HOOK, MOUSE_HOOK, WINDOW_WATCH):
        health.set(name, HealthState.HEALTHY, "test")
    w = _watcher(health=health)
    for _ in range(3):
        w.start()
        assert _wait(lambda: _healthy(w))
        w.stop()
    for name in (KEYBOARD_HOOK, MOUSE_HOOK, WINDOW_WATCH):
        assert health.get(name).state == HealthState.HEALTHY


def test_the_initial_lock_state_is_reported_after_every_restart():
    states: list[bool] = []
    w = _watcher(states=states)
    for expected in (1, 2, 3):
        w.start()
        assert _wait(lambda n=expected: len(states) >= n)
        w.stop()
    assert states == [False, False, False]


# ─── failure paths clean up after themselves ───────────────────────────────


def test_a_failed_window_creation_is_cleaned_up_and_the_next_start_works(monkeypatch):
    user32 = ctypes.windll.user32
    real = user32.CreateWindowExW
    calls = {"n": 0}

    def failing(*args):  # noqa: ANN002, ANN202
        calls["n"] += 1
        return 0

    monkeypatch.setattr(user32, "CreateWindowExW", failing, raising=False)
    w = _watcher()
    w.start()
    assert _wait(lambda: w._health.get(SESSION_LOCK).state == HealthState.ERROR)
    assert _wait(lambda: not _watcher_threads())
    assert not _class_registered(w._class_name)  # the class registered for this attempt was removed
    w.stop()
    monkeypatch.setattr(user32, "CreateWindowExW", real, raising=False)
    w.start()
    try:
        assert _wait(lambda: _healthy(w)), w._health.get(SESSION_LOCK)
    finally:
        w.stop()
    assert calls["n"] == 1


def test_a_failed_wts_registration_is_cleaned_up(monkeypatch):
    wts = ctypes.windll.wtsapi32
    real = wts.WTSRegisterSessionNotification
    monkeypatch.setattr(wts, "WTSRegisterSessionNotification", lambda hwnd, flags: 0, raising=False)
    w = _watcher()
    w.start()
    assert _wait(lambda: w._health.get(SESSION_LOCK).state == HealthState.ERROR)
    assert _wait(lambda: not _watcher_threads())
    name = w._class_name
    assert not _class_registered(name)
    w.stop()
    monkeypatch.setattr(wts, "WTSRegisterSessionNotification", real, raising=False)
    w.start()
    try:
        assert _wait(lambda: _healthy(w))
    finally:
        w.stop()


# ─── agent level: recording restart after re-enrollment ────────────────────


def test_agents_built_in_sequence_in_one_process_keep_the_lock_state_trusted(tmp_path):
    """What the runner does on a re-enrollment: stop recording, build a new agent, start again."""
    from datetime import timezone

    from deskmate.zaza.agent import ActivityAgent
    from deskmate.zaza.config import AgentConfig
    from deskmate.zaza.storage import ActivityStore

    def build(name: str) -> ActivityAgent:
        config = AgentConfig(employee_id="e", device_id="d", db_path=str(tmp_path / name))
        agent = ActivityAgent(config, store=ActivityStore(config.db_path), tz=timezone.utc,
                              window_probe=lambda: session_lock_probe)
        agent._lock_watcher._query_locked = lambda: False  # only the watcher is exercised, not the input hooks
        return agent

    from deskmate.zaza.window_watch import WindowSample

    session_lock_probe = WindowSample("Code.exe", "x")
    agents = []
    try:
        for i in range(3):
            agent = build(f"a{i}.db")
            agents.append(agent)
            agent._lock_watcher.start()
            assert _wait(agent.lock_state_trusted), agent.health.get(SESSION_LOCK)
            agent._lock_watcher.stop()
        assert not _watcher_threads()
    finally:
        for agent in agents:
            agent._lock_watcher.stop()
            agent.store.close()


def test_class_names_are_unique_per_run_and_the_module_keeps_no_stale_state():
    names = set()
    for _ in range(3):
        w = _watcher()
        w.start()
        assert _wait(lambda w=w: _healthy(w))
        names.add(w._class_name)
        w.stop()
    assert len(names) == 3
    assert all(n.startswith("ZazaSessionLockWatcher") for n in names)
    assert not session_lock._ACTIVE_CALLBACKS  # every callback reference was released
