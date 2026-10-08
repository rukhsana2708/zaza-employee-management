"""Agent status for the employee-facing Status & Privacy window.

The running agent writes ``<data dir>/status.json`` (atomically, every few
seconds and on every change). It contains health and sync state only —
never the token, window titles, applications or any activity data. The
window reads it; nothing listens on the network.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .instance import is_running

STALE_SECONDS = 90  # a status file older than this means the agent isn't updating it

COMPONENT_LABELS = {
    "keyboard_hook": "Keyboard activity presence",
    "mouse_hook": "Mouse activity presence",
    "window_watch": "Foreground-app monitoring",
    "session_lock": "Windows lock monitoring",
    "storage": "Local storage",
}
SYNC_LABELS = {
    "HEALTHY": "Healthy", "BACKLOG": "Backlog (uploading)", "OFFLINE": "Offline (records kept locally)",
    "AUTH_ERROR": "Authentication error", "SERVER_ERROR": "Server error", "NOT_CONFIGURED": "Not enrolled",
}


@dataclass
class AgentStatus:
    state: str = "STARTING"  # RUNNING | DEGRADED | WAITING_FOR_ENROLLMENT | STOPPED
    version: str = ""
    pid: int = 0
    started_at: str = ""
    updated_at: str = ""
    device_id: str = ""
    server_host: str = ""
    health: dict[str, str] = field(default_factory=dict)
    sync_state: str = "NOT_CONFIGURED"
    last_sync_at: str | None = None
    pending: int = 0
    sync_error: str | None = None


def write_status(status: AgentStatus, path: Path | None = None) -> None:
    path = path or paths.status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    status.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(status), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_status(path: Path | None = None) -> AgentStatus | None:
    path = path or paths.status_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AgentStatus(**{k: v for k, v in raw.items() if k in AgentStatus.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def effective_status(now: datetime | None = None) -> AgentStatus:
    """What the window shows: STOPPED unless an agent holds the lock AND keeps
    its status file fresh."""
    now = now or datetime.now(timezone.utc)
    status = read_status() or AgentStatus()
    running = is_running(paths.lock_path())
    fresh = False
    if status.updated_at:
        try:
            fresh = (now - datetime.fromisoformat(status.updated_at)).total_seconds() <= STALE_SECONDS
        except ValueError:
            fresh = False
    if not running or not fresh:
        status.state = "STOPPED"
    return status
