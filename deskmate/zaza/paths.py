"""Filesystem layout for the ZaZa agent — deliberately separate from
``deskmate/paths.py`` so ZaZa's activity database never shares a directory,
file, or schema with upstream DeskMate's data store.

Per-user data directory (Phase 9):

- ``ZAZA_HOME`` if set (development / tests);
- Windows: ``%LOCALAPPDATA%\\ZaZa\\WorkAgent`` (never inside Program Files);
- elsewhere: ``~/.zaza_agent``.

Earlier development builds used ``~/.zaza_agent`` on Windows too;
:func:`migrate_legacy` moves that data (database, WAL, credentials) to the
new location once, so no unsynced record is lost on upgrade.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

LEGACY_DIRNAME = ".zaza_agent"
# Files that make up the agent's state. The database is checkpointed first so
# its -wal/-shm companions are empty, but they are moved too, just in case.
STATE_FILES = ("activity.db", "activity.db-wal", "activity.db-shm", "device_credentials.json", "config.json")


def legacy_root() -> Path:
    return Path.home() / LEGACY_DIRNAME


def root() -> Path:
    """ZaZa agent data directory."""
    override = os.environ.get("ZAZA_HOME")
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local:
        return Path(local) / "ZaZa" / "WorkAgent"
    return legacy_root()


def db_path() -> Path:
    return root() / "activity.db"


def config_path() -> Path:
    """Non-secret enrollment/configuration (server URL, device ID, ...)."""
    return root() / "config.json"


def status_path() -> Path:
    """Written by the running agent for the Status & Privacy window."""
    return root() / "status.json"


def lock_path() -> Path:
    return root() / "agent.lock"


def stop_request_path() -> Path:
    """Per-user request for the running agent to stop cleanly."""
    return root() / "stop.request"


def logs_dir() -> Path:
    return root() / "logs"


def ensure_dirs() -> None:
    root().mkdir(parents=True, exist_ok=True)


def migrate_legacy(src: Path | None = None, dst: Path | None = None) -> list[str]:
    """Move a pre-Phase-9 ``~/.zaza_agent`` data set to the current root.

    Only when the old database exists and the new one doesn't (never
    overwrites, never merges). The old database is checkpointed and closed
    first; files are moved, not copied-and-deleted, so a failure part-way
    leaves every file in one of the two places. Returns the moved names."""
    src = src or legacy_root()
    dst = dst or root()
    if src.resolve() == dst.resolve() or not (src / "activity.db").exists() or (dst / "activity.db").exists():
        return []
    conn = sqlite3.connect(src / "activity.db")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    dst.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in STATE_FILES:
        if (src / name).exists() and not (dst / name).exists():
            shutil.move(str(src / name), str(dst / name))
            moved.append(name)
    return moved
