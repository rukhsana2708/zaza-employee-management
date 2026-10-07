"""Shared fixtures: a fake clock driving both the agent's wall clock and its
monotonic clock, and an agent factory wired to a temp SQLite file and an
injectable foreground-window probe."""

from __future__ import annotations

from datetime import timezone

import pytest

from deskmate.zaza.agent import ActivityAgent
from deskmate.zaza.config import AgentConfig
from deskmate.zaza.health import KEYBOARD_HOOK, MOUSE_HOOK, SESSION_LOCK, HealthState
from deskmate.zaza.storage import ActivityStore
from deskmate.zaza.window_watch import WindowSample

# 2026-10-07 09:00:00 UTC — mid-day in UTC so rollup day boundaries are stable.
T0 = 1_791_363_600.0


class FakeClock:
    def __init__(self, start: float = T0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Probe:
    """Mutable foreground-window probe: set ``.sample`` to "switch apps"."""

    def __init__(self, sample: WindowSample | None = None) -> None:
        self.sample = sample or WindowSample("Code.exe", "agent.py - zaza - Visual Studio Code")

    def __call__(self) -> WindowSample:
        return self.sample

    def set(self, app: str, title: str = "", domain: str | None = None) -> None:
        self.sample = WindowSample(app, title, domain=domain)


def mark_hooks(agent: ActivityAgent, state: HealthState = HealthState.HEALTHY) -> None:
    agent.health.set(KEYBOARD_HOOK, state, "simulated")
    agent.health.set(MOUSE_HOOK, state, "simulated")


def mark_lock_watcher(agent: ActivityAgent, state: HealthState = HealthState.HEALTHY, *, locked: bool | None = False) -> None:
    """Simulate the session-lock watcher: set its health and, like the real
    watcher on (re)registration, report the current lock state."""
    agent.health.set(SESSION_LOCK, state, "simulated")
    if locked is not None:
        agent._on_lock_state(locked)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def probe() -> Probe:
    return Probe()


@pytest.fixture
def make_agent(tmp_path, clock, probe):
    created: list[ActivityAgent] = []

    def _make(
        *, healthy_hooks: bool = True, healthy_lock: bool = True, db_name: str = "activity.db", **config_overrides
    ) -> ActivityAgent:
        config = AgentConfig(
            employee_id="emp-1",
            device_id="device-1",
            db_path=str(tmp_path / db_name),
            **config_overrides,
        )
        agent = ActivityAgent(
            config,
            store=ActivityStore(config.db_path),
            window_probe=probe,
            wall_clock=clock,
            mono_clock=clock,
            tz=timezone.utc,
        )
        if healthy_hooks:
            mark_hooks(agent)
        if healthy_lock:
            mark_lock_watcher(agent)
        created.append(agent)
        return agent

    yield _make
    for agent in created:
        try:
            agent.store.close()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def agent(make_agent) -> ActivityAgent:
    return make_agent()


def tick_for(agent: ActivityAgent, clock: FakeClock, seconds: float, step: float | None = None) -> None:
    """Advance the clock by ``seconds`` in tick-sized steps, ticking after
    each — like the real timer, so no telemetry gap is detected."""
    step = step or agent.config.tick_seconds
    remaining = seconds
    while remaining > 0:
        delta = min(step, remaining)
        clock.advance(delta)
        agent.tick()
        remaining -= delta


# ─── Phase 3: sync fixtures ────────────────────────────────────────────────

class SyncServer:
    """In-process sync API (FastAPI TestClient) backed by a repository, with
    the fixture agent's device (device-1 / emp-1) registered."""

    def __init__(self, repo) -> None:
        from fastapi.testclient import TestClient

        from deskmate.zaza_server.app import create_app
        from deskmate.zaza_server.auth import register_device

        self.repo = repo
        self.app = create_app(repo)
        self.client = TestClient(self.app)
        self.token = register_device(repo, "device-1", "emp-1").token

    def headers(self, token: str | None = None, device_id: str = "device-1") -> dict:
        return {"Authorization": f"Bearer {token or self.token}", "X-ZaZa-Device-Id": device_id}

    def transport(self, token: str | None = None, device_id: str = "device-1"):
        from deskmate.zaza.sync.credentials import DeviceCredentials
        from deskmate.zaza.sync.transport import SyncTransport

        return SyncTransport(
            "http://testserver", DeviceCredentials(device_id, token or self.token),
            client=self.client, allow_insecure=True,
        )


@pytest.fixture
def server() -> SyncServer:
    from deskmate.zaza_server.repository import InMemoryRepository

    return SyncServer(InMemoryRepository())


@pytest.fixture
def make_worker(clock):
    from deskmate.zaza.sync.backoff import Backoff
    from deskmate.zaza.sync.worker import SyncWorker

    def _make(agent: ActivityAgent, transport, **kwargs):
        kwargs.setdefault("batch_size", 100)
        return SyncWorker(
            agent.store, transport, device_id=agent.config.device_id, employee_id=agent.config.employee_id,
            wall_clock=clock, rng=lambda: 0.5, tz=timezone.utc,
            backoff=kwargs.pop("backoff", Backoff(rng=lambda: 0.5)), **kwargs,
        )

    return _make


def work_session(agent: ActivityAgent, clock: FakeClock, *, busy: int = 60, quiet: int = 400) -> str:
    """Busy for ``busy`` seconds, quiet for ``quiet`` (idle after the grace
    period), then close the session — producing closed session, period, idle
    and usage records."""
    agent.recorder.record_keyboard()
    agent.tick()
    remaining = busy
    while remaining > 0:
        clock.advance(10)
        agent.recorder.record_keyboard()
        agent.tick()
        remaining -= 10
    tick_for(agent, clock, quiet)
    session_id = agent.session_id
    agent.end_session()
    return session_id
