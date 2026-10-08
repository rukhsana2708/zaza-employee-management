"""Single-instance protection: one recording agent per Windows user.

An exclusive OS lock on ``<data dir>/agent.lock`` — the same directory that
holds the activity database, so two recorders can never write the same
database (and double-count activity). The lock is released automatically
by the OS if the process dies, so a crash never leaves a stale lock.
"""

from __future__ import annotations

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        """True if this process now holds the lock; False if another does."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 — held for the process lifetime
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode("ascii"))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None


def is_running(path: Path) -> bool:
    """True if another process holds the lock (i.e. an agent is running)."""
    probe = InstanceLock(path)
    if probe.acquire():
        probe.release()
        return False
    return True
