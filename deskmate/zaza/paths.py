"""Filesystem layout for the ZaZa agent — deliberately separate from
``deskmate/paths.py`` so ZaZa's activity database never shares a directory,
file, or schema with upstream DeskMate's data store.
"""

from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    """ZaZa agent data directory (``~/.zaza_agent`` by default)."""
    override = os.environ.get("ZAZA_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".zaza_agent"


def db_path() -> Path:
    return root() / "activity.db"


def ensure_dirs() -> None:
    root().mkdir(parents=True, exist_ok=True)
