"""Product identity and build flavour of the installed ZaZa Work Agent.

- ``PRODUCT_NAME`` / ``PUBLISHER`` are what employees see (never "DeskMate").
- The version is :data:`deskmate.zaza.__version__` (one source of truth; the
  build script reads it for the executable and the installer).
- Build flavour: a frozen build ships ``build_flavor.txt`` next to its
  modules (``production`` or ``development``). Both flavours keep TLS
  verification on and refuse plain HTTP to anything but loopback;
  ``development`` only adds a console window and debug logging.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import __version__

PRODUCT_NAME = "ZaZa Work Agent"
PUBLISHER = "ZaZa"
EXE_NAME = "ZaZaWorkAgent.exe"
TASK_NAME = r"ZaZa\ZaZa Work Agent"
VERSION = __version__


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path | None:
    """The folder holding ZaZaWorkAgent.exe (frozen builds only)."""
    return Path(sys.executable).resolve().parent if is_frozen() else None


def build_flavor() -> str:
    if not is_frozen():
        return "source"
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    try:
        flavor = (base / "build_flavor.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "production"
    return flavor if flavor in ("production", "development") else "production"


def version_label() -> str:
    return f"{PRODUCT_NAME}\nVersion {VERSION}"
