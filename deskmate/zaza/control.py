"""Stopping running agents cleanly (installer upgrades, uninstall, local-data
removal) — without any network listener or inter-process channel.

- Per user: ``<data dir>/stop.request``.
- Machine-wide (installer/uninstaller, which run elevated and possibly as a
  different account): ``<install dir>/stop.request`` — Program Files is
  writable only by administrators, so employees can't create it.

A running agent polls both every second; a request is honoured only if it
is newer than the agent's own start, so a leftover file never stops a
freshly started agent. Agents then close their work session normally.
Anything still running after the grace period is terminated; Phase 2 crash
recovery closes its session as INTERRUPTED on the next start and no
recorded data is lost (SQLite WAL).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .edition import EXE_NAME

GRACE_SECONDS = 20.0


def request_stop(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="ascii")


def requested(path: Path | None, since: float) -> bool:
    if path is None:
        return False
    try:
        return path.stat().st_mtime >= since - 1.0
    except OSError:
        return False


def clear(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def agent_processes(exe_name: str = EXE_NAME) -> list:
    import psutil  # noqa: PLC0415

    me = os.getpid()
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["pid"] != me and (proc.info["name"] or "").lower() == exe_name.lower():
            found.append(proc)
    return found


def stop_all(install_dir: Path, *, grace: float = GRACE_SECONDS, exe_name: str = EXE_NAME) -> tuple[int, int]:
    """Ask every agent of this installation to stop, wait, then terminate
    stragglers. Returns (stopped cleanly, terminated)."""
    import psutil  # noqa: PLC0415

    marker = install_dir / "stop.request"
    before = agent_processes(exe_name)
    if not before:
        return 0, 0
    request_stop(marker)
    try:
        _, alive = psutil.wait_procs(before, timeout=grace)
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(alive, timeout=5)
        return len(before) - len(alive), len(alive)
    finally:
        clear(marker)
