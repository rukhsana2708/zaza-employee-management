"""Component health/readiness for the ZaZa activity agent.

Each moving part (keyboard hook, mouse hook, session-lock watcher,
foreground-window watcher, SQLite storage) reports its own state here. The
agent consults it before trusting "no input seen" as genuine inactivity: a
keyboard/mouse hook that failed to install produces exactly the same empty
signal as a user who walked away, so the two must be told apart explicitly.
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone


class HealthState(str, enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


KEYBOARD_HOOK = "keyboard_hook"
MOUSE_HOOK = "mouse_hook"
SESSION_LOCK = "session_lock"
WINDOW_WATCH = "window_watch"
STORAGE = "storage"

COMPONENTS = (KEYBOARD_HOOK, MOUSE_HOOK, SESSION_LOCK, WINDOW_WATCH, STORAGE)
INPUT_HOOKS = (KEYBOARD_HOOK, MOUSE_HOOK)


@dataclass(frozen=True)
class ComponentHealth:
    component: str
    state: HealthState
    detail: str
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HealthRegistry:
    """Thread-safe map of component -> latest :class:`ComponentHealth`.

    Every component starts DEGRADED ("not started") until it reports in.
    ``on_change`` fires only on an actual state/detail change, so console
    output stays quiet while nothing is happening.
    """

    def __init__(self, on_change: Callable[[ComponentHealth], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._on_change = on_change
        now = _now_iso()
        self._components: dict[str, ComponentHealth] = {
            name: ComponentHealth(name, HealthState.DEGRADED, "not started", now) for name in COMPONENTS
        }
        # Incremented on every state change, so callers can tell whether
        # something they learned belongs to the component's current stint in
        # its current state (see ActivityAgent's lock-state handling).
        self._generation: dict[str, int] = {name: 0 for name in COMPONENTS}

    def set_on_change(self, on_change: Callable[[ComponentHealth], None] | None) -> None:
        self._on_change = on_change

    def set(self, component: str, state: HealthState, detail: str = "") -> None:
        if component not in COMPONENTS:
            raise ValueError(f"unknown component: {component!r}")
        with self._lock:
            prev = self._components[component]
            if prev.state == state and prev.detail == detail:
                return
            entry = ComponentHealth(component, state, detail, _now_iso())
            self._components[component] = entry
            if prev.state != state:
                self._generation[component] += 1
        if self._on_change:
            try:
                self._on_change(entry)
            except Exception:  # noqa: BLE001
                pass

    def get(self, component: str) -> ComponentHealth:
        with self._lock:
            return self._components[component]

    def generation(self, component: str) -> int:
        with self._lock:
            return self._generation[component]

    def snapshot(self) -> dict[str, ComponentHealth]:
        with self._lock:
            return dict(self._components)

    def input_trustworthy(self) -> bool:
        """True only when both keyboard and mouse hooks are HEALTHY — i.e.
        whether an absence of input events can be read as the user actually
        being idle. DEGRADED (starting, stopped) is not trustworthy either."""
        with self._lock:
            return all(self._components[c].state == HealthState.HEALTHY for c in INPUT_HOOKS)

    def overall(self) -> HealthState:
        """ERROR when the agent can't do its core job at all (storage down, or
        both input hooks down); DEGRADED when any component isn't HEALTHY;
        otherwise HEALTHY."""
        with self._lock:
            states = {name: c.state for name, c in self._components.items()}
        if states[STORAGE] == HealthState.ERROR or all(
            states[c] == HealthState.ERROR for c in INPUT_HOOKS
        ):
            return HealthState.ERROR
        if any(s != HealthState.HEALTHY for s in states.values()):
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    def format_table(self) -> str:
        lines = [f"Health: {self.overall().value}"]
        for entry in self.snapshot().values():
            suffix = f" - {entry.detail}" if entry.detail else ""
            lines.append(f"  {entry.component:<14} {entry.state.value:<8}{suffix}")
        return "\n".join(lines)
