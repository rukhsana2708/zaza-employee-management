"""The installed agent's background process (``ZaZaWorkAgent.exe --background``).

Started at logon by the scheduled task, in the user's interactive session,
non-elevated. It changes none of the approved Phase 1-3 behaviour — it only
wraps the same :class:`ActivityAgent` and :class:`SyncWorker`:

1. take the single-instance lock (a second copy exits at once);
2. move a pre-Phase-9 ``~/.zaza_agent`` data set, if any;
3. wait — without recording — until the device is enrolled (records are
   stamped with the device and employee IDs, so nothing is recorded under a
   placeholder identity);
4. run the recorder (local-first SQLite) and, independently, the sync
   worker: the server being unreachable never stops recording;
5. every few seconds: write ``status.json``, react to a re-enrollment
   (restart cleanly with the new identity/credentials) and to a stop
   request (close the work session normally and exit 0).

A crash exits non-zero; the scheduled task restarts it after 5 minutes (at
most 3 times) and Phase 2 recovery closes the interrupted session.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__, paths
from .agent import ActivityAgent
from .control import clear, requested
from .enrollment import Enrollment, current_state
from .health import HealthState
from .instance import InstanceLock
from .logger import get
from .status import AgentStatus, write_status
from .sync.factory import build_sync_worker
from .sync.worker import SyncWorker

logger = get("runner")
STATUS_INTERVAL = 5.0
ENROLLMENT_POLL = 10.0


class Runner:
    def __init__(self, *, install_dir: Path | None = None, poll: float = 1.0,
                 agent_factory: Callable[..., ActivityAgent] = ActivityAgent,
                 worker_factory: Callable = build_sync_worker, clock: Callable[[], float] = time.time) -> None:
        self.install_dir = install_dir
        self.poll = poll
        self.agent_factory = agent_factory
        self.worker_factory = worker_factory
        self.clock = clock
        self.lock = InstanceLock(paths.lock_path())
        self.agent: ActivityAgent | None = None
        self.worker: SyncWorker | None = None
        self.enrollment: Enrollment | None = None
        self.started_at = clock()
        self.status = AgentStatus(version=__version__)
        self._signature: tuple | None = None

    # ── control files ─────────────────────────────────────────────────────
    def stop_requested(self) -> bool:
        machine = self.install_dir / "stop.request" if self.install_dir else None
        return requested(paths.stop_request_path(), self.started_at) or requested(machine, self.started_at)

    @staticmethod
    def _signature_now() -> tuple:
        """Changes when the employee (re-)enrolls."""
        out = []
        for p in (paths.config_path(), paths.root() / "device_credentials.json"):
            try:
                st = p.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _start_recording(self, enrollment: Enrollment) -> None:
        config = enrollment.agent_config()
        self.agent = self.agent_factory(config)
        self.agent.start()
        worker, reason = self.worker_factory(config)
        self.worker = worker
        if worker is None:
            logger.warning("sync not available: %s (recording continues locally)", reason)
        else:
            worker.start()
        self.enrollment = enrollment
        logger.info("recording started (device %s, server %s)", enrollment.device_id, enrollment.server_host)

    def _stop_recording(self) -> None:
        if self.worker is not None:
            self.worker.stop()
        if self.agent is not None:
            self.agent.stop()
        if self.worker is not None:
            try:
                self.worker.run_once()  # best effort: upload the just-closed session
            except Exception:  # noqa: BLE001 — offline is fine; records stay queued
                pass
            self.worker.transport.close()
            self.worker.store.close()
        if self.agent is not None:
            self.agent.store.close()
        self.agent = self.worker = None

    def _update_status(self) -> None:
        s = self.status
        s.pid = os.getpid()
        s.started_at = s.started_at or time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if self.agent is None:
            s.state, s.health = "WAITING_FOR_ENROLLMENT", {}
            s.device_id, s.server_host, s.sync_state = "", "", "NOT_CONFIGURED"
        else:
            snapshot = self.agent.health.snapshot()
            s.health = {name: c.state.value for name, c in snapshot.items()}
            s.state = "RUNNING" if self.agent.health.overall() == HealthState.HEALTHY else "DEGRADED"
            s.device_id, s.server_host = self.enrollment.device_id, self.enrollment.server_host
            if self.worker is None:
                s.sync_state, s.sync_error = "NOT_CONFIGURED", "device credentials missing or unusable"
                counts = self.agent.store.sync_counts()
                s.pending = counts["pending"] + counts["failed"] + counts["open"]
            else:
                h = self.worker.health()
                s.sync_state, s.last_sync_at = h.state.value, h.last_success_at
                s.pending = h.pending_count + h.failed_count + h.open_count
                s.sync_error = h.state.value if h.state.value not in ("HEALTHY", "BACKLOG") else None
        write_status(s)

    def _check_enrollment(self) -> None:
        """Start recording once enrolled; restart it after a re-enrollment."""
        signature = self._signature_now()
        enrollment, creds_ok = current_state()
        if enrollment is not None and creds_ok:
            if self.agent is not None:
                logger.info("enrollment changed; restarting recording")
                self._stop_recording()
            self._start_recording(enrollment)
        self._signature = signature

    def run(self) -> int:
        paths.ensure_dirs()
        if not self.lock.acquire():
            logger.info("another ZaZa Work Agent is already running for this user; exiting")
            return 0
        try:
            moved = paths.migrate_legacy()
            if moved:
                logger.info("moved local data from the previous location: %s", ", ".join(moved))
            if not requested(paths.stop_request_path(), self.started_at):
                clear(paths.stop_request_path())  # a leftover request from before this start
            last_status, last_check = float("-inf"), float("-inf")
            while not self.stop_requested():
                now = self.clock()
                if self.agent is None:
                    if now - last_check >= ENROLLMENT_POLL:  # not enrolled yet: look again every 10 s
                        last_check = now
                        self._check_enrollment()
                elif self._signature_now() != self._signature:  # re-enrolled while running
                    self._check_enrollment()
                if now - last_status >= STATUS_INTERVAL:
                    last_status = now
                    try:
                        self._update_status()
                    except Exception as exc:  # noqa: BLE001 — status is informational only
                        logger.warning("could not write status: %s", type(exc).__name__)
                time.sleep(self.poll)
            logger.info("stop requested; closing the work session")
            return 0
        finally:
            self._stop_recording()
            self.status.state = "STOPPED"
            try:
                write_status(self.status)
            except OSError:
                pass
            self.lock.release()
