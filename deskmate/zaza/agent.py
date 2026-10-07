"""Orchestrator: wires the window watcher, input hooks, lock/unlock watcher,
health registry, period aggregator, and local SQLite store into one agent.

Each ``tick()`` classifies the moment as ACTIVE / IDLE / UNKNOWN / LOCKED,
feeds that to the :class:`PeriodAggregator`, heartbeats the work session, and
writes one raw ACTIVITY event — all in a single transaction.

Classification (see ARCHITECTURE.md §2.3), in order:

- LOCKED while the session is known to be locked. The lock state is
  trusted only if it was established (by a LOCK/UNLOCK event or the
  watcher's current-state query) during the session-lock watcher's current
  HEALTHY stint. A state from before a watcher outage is stale.
- UNKNOWN / MONITORING_UNAVAILABLE unless *both* input hooks are HEALTHY —
  a hook that is starting, stopped, or failed produces no input events,
  which would otherwise be indistinguishable from an idle user.
- ACTIVE if input was observed within the idle threshold (input proves
  presence, whatever the lock watcher's state).
- UNKNOWN / LOCK_STATE_UNAVAILABLE if the lock state isn't trusted: without
  it, "no input" could just as well mean "locked", so IDLE is never inferred.
- IDLE once the quiet time *while fully trusted* reaches the threshold. The
  threshold is a grace period: idle begins at last input + threshold (or
  trusted-since + threshold), never at the last input itself.
- UNKNOWN / AWAITING_INPUT otherwise — trusted, but no input seen yet and
  the threshold hasn't elapsed (e.g. right after start).

``tick()`` is synchronous and clock-injectable so tests drive it directly;
``start()``/``stop()`` add the real Windows hooks and a background timer.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from datetime import tzinfo

from . import rollup
from .aggregator import ACTIVE, IDLE, LOCKED, UNKNOWN, Observation, PeriodAggregator
from .config import AgentConfig
from .health import (
    KEYBOARD_HOOK,
    MOUSE_HOOK,
    SESSION_LOCK,
    STORAGE,
    WINDOW_WATCH,
    HealthRegistry,
    HealthState,
)
from .logger import get
from .privacy import PrivacyFilter
from .recorder import ActivityRecorder
from .session_lock import SessionLockWatcher
from .storage import ActivityStore
from .timeutil import iso_utc, local_days_between, parse_iso
from .window_watch import ForegroundProbe, WindowChangeTracker, WindowSample, make_foreground_probe

logger = get("agent")

# Wall-clock steps backwards smaller than this are absorbed (time is held at
# the last tick); larger ones end the session and start a new one.
CLOCK_BACKWARD_TOLERANCE_SECONDS = 2.0


class ActivityAgent:
    def __init__(
        self,
        config: AgentConfig,
        *,
        store: ActivityStore | None = None,
        window_probe: ForegroundProbe | None = None,
        health: HealthRegistry | None = None,
        wall_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
        tz: tzinfo | None = None,
    ) -> None:
        self.config = config
        self.health = health or HealthRegistry()
        try:
            self.store = store or ActivityStore(config.db_path)
            self.store.ping()
        except sqlite3.Error as exc:
            self.health.set(STORAGE, HealthState.ERROR, f"cannot open database: {exc}")
            raise
        self.health.set(STORAGE, HealthState.HEALTHY, "ready")

        self.privacy = PrivacyFilter.from_config(config)
        probe = window_probe or make_foreground_probe(self.privacy)
        # Second line of defence: every sample is filtered again before use,
        # whether or not the probe already applied the exclusions.
        self._window_tracker = WindowChangeTracker(probe=lambda: self.privacy.apply(probe()))
        self._wall = wall_clock
        self._mono = mono_clock
        self.tz = tz
        self.recorder = ActivityRecorder(idle_threshold_seconds=config.idle_threshold_seconds, clock=mono_clock)
        self.aggregator = PeriodAggregator(
            self.store,
            device_id=config.device_id,
            title_debounce_seconds=config.title_debounce_seconds,
            on_period_closed=self._on_period_closed,
        )

        self.session_id: str | None = None
        self._state_lock = threading.RLock()
        self._locked = False
        # Health generation of the session-lock watcher when _locked was last
        # established; None = never. Trusted only if it still matches.
        self._lock_generation: int | None = None
        self._trusted_since: float | None = None
        self._last_tick_wall: float | None = None
        self._last_status: str | None = None
        self._input_untrusted_logged = False

        self._timer_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._input_hooks = None
        self._lock_watcher = SessionLockWatcher(
            on_event=self._on_session_event, on_state=self._on_lock_state, health=self.health
        )

    def health_status(self) -> HealthState:
        return self.health.overall()

    # ─── work sessions ─────────────────────────────────────────────────────
    def recover_interrupted_sessions(self) -> list[str]:
        """Close any session a previous run left OPEN (crash, power loss,
        killed process) as INTERRUPTED at its last heartbeat."""
        recovered = []
        for session in self.store.open_sessions():
            if session["session_id"] == self.session_id:
                continue
            closed = self.store.close_interrupted_session(session["session_id"])
            for period in closed:
                self._on_period_closed(parse_iso(period["started_at"]), parse_iso(period["ended_at"]))
            recovered.append(session["session_id"])
            logger.warning(
                "recovered interrupted session %s (last heartbeat %s)",
                session["session_id"], session["last_heartbeat_at"],
            )
        return recovered

    def begin_session(self) -> str:
        with self._state_lock:
            if self.session_id is not None:
                return self.session_id
            interrupted = self.recover_interrupted_sessions()
            now = self._wall()
            session_id = str(uuid.uuid4())
            with self.store.transaction():
                self.store.create_session(
                    session_id=session_id,
                    device_id=self.config.device_id,
                    employee_id=self.config.employee_id,
                    started_at=now,
                    start_reason="AGENT_START_AFTER_INTERRUPTION" if interrupted else "AGENT_START",
                    previous_session_id=interrupted[-1] if interrupted else None,
                )
                self.store.insert_event(event_type="SESSION_START", ts=iso_utc(now), session_id=session_id)
            self.session_id = session_id
            self.aggregator.begin(session_id, now)
            self._last_tick_wall = now
            self._trusted_since = None
            self._last_status = None
            logger.info("work session %s started", session_id)
            return session_id

    def end_session(self, reason: str = "AGENT_STOP", *, at: float | None = None) -> None:
        with self._state_lock:
            if self.session_id is None:
                return
            now = self._wall() if at is None else at
            with self.store.transaction():
                self.aggregator.finish(now, "SESSION_END")
                self.store.finalize_session(self.session_id, ended_at=now, status="CLOSED", end_reason=reason)
                self.store.insert_event(event_type="SESSION_END", ts=iso_utc(now), session_id=self.session_id)
            logger.info("work session %s closed (%s)", self.session_id, reason)
            self.session_id = None

    # ─── one sampling step — called by the timer thread, or directly in tests ──
    def tick(self, reason: str | None = None) -> None:
        with self._state_lock:
            try:
                self._handle_clock_step_back()
                if self.session_id is None:
                    self.begin_session()
                with self.store.transaction():
                    self._tick(reason)
            except sqlite3.Error as exc:
                self.health.set(STORAGE, HealthState.ERROR, f"write failed: {exc}")
                try:
                    self.aggregator.reload()
                except sqlite3.Error:
                    pass
                raise
            self.health.set(STORAGE, HealthState.HEALTHY, "ready")

    def _handle_clock_step_back(self) -> None:
        """If the wall clock moved backwards (manual change, NTP step), close
        the session at the last good timestamp and let a new one start, so no
        period ever ends before it began."""
        last = self._last_tick_wall
        if self.session_id is None or last is None:
            return
        behind = last - self._wall()
        if behind > CLOCK_BACKWARD_TOLERANCE_SECONDS:
            logger.warning("wall clock moved back %.0fs — closing session and starting a new one", behind)
            self.end_session("CLOCK_CHANGED", at=last)

    def _tick(self, reason: str | None) -> None:
        now = max(self._wall(), self._last_tick_wall or 0.0)
        mono = self._mono()
        sid = self.session_id

        gap_start = self._last_tick_wall
        if gap_start is not None and now - gap_start > self.config.effective_gap_threshold_seconds:
            self.aggregator.record_gap(gap_start, now)
            self.recorder.forget_input()
            self._trusted_since = None
            self.store.insert_event(event_type="TELEMETRY_GAP", ts=iso_utc(now), session_id=sid, status=UNKNOWN)
            logger.warning("telemetry gap of %.0fs recorded as UNKNOWN", now - gap_start)
        self._last_tick_wall = now

        status, detail, effective_at = self._classify(now, mono)
        sample = None if status == LOCKED else self._poll_window(now)

        keyboard_active, mouse_active = self.recorder.snapshot_and_reset()
        # Only a HEALTHY hook's silence means "no input"; otherwise unknown.
        if self.health.get(KEYBOARD_HOOK).state != HealthState.HEALTHY:
            keyboard_active = None
        if self.health.get(MOUSE_HOOK).state != HealthState.HEALTHY:
            mouse_active = None

        obs = Observation(
            status=status,
            app_name=sample.app_name if sample else None,
            window_title=sample.window_title if sample else None,
            domain=sample.domain if sample else None,
            privacy_excluded=sample.privacy_excluded if sample else False,
            detail=detail,
        )
        self.aggregator.observe(obs, now=now, effective_at=effective_at, reason=reason)
        self.store.heartbeat_session(sid, now)

        self.store.insert_event(
            event_type="ACTIVITY",
            ts=iso_utc(now),
            session_id=sid,
            status=status,
            keyboard_active=keyboard_active,
            mouse_active=mouse_active,
            idle={IDLE: True, ACTIVE: False}.get(status),
            privacy_excluded=obs.privacy_excluded,
        )
        logger.info("ACTIVITY status=%s keyboard=%s mouse=%s", status, keyboard_active, mouse_active)

        if status == UNKNOWN and detail == "MONITORING_UNAVAILABLE":
            if not self._input_untrusted_logged:
                logger.warning(
                    "input hooks not HEALTHY — status UNKNOWN, not idle (health: %s)", self.health.overall().value
                )
                self._input_untrusted_logged = True
        else:
            self._input_untrusted_logged = False

        if status == IDLE and self._last_status != IDLE:
            self.store.insert_event(event_type="IDLE", ts=iso_utc(now), session_id=sid, status=IDLE)
            logger.info("IDLE (no input for >= %ss)", self.config.idle_threshold_seconds)
        elif status == ACTIVE and self._last_status == IDLE:
            self.store.insert_event(event_type="CONTINUE", ts=iso_utc(now), session_id=sid, status=ACTIVE)
            logger.info("CONTINUE (activity resumed)")
        self._last_status = status

    def lock_state_trusted(self) -> bool:
        """True only if the session-lock watcher is HEALTHY *and* the current
        lock state was learned during this HEALTHY stint."""
        entry = self.health.get(SESSION_LOCK)
        return (
            entry.state == HealthState.HEALTHY
            and self._lock_generation is not None
            and self._lock_generation == self.health.generation(SESSION_LOCK)
        )

    def _classify(self, now: float, mono: float) -> tuple[str, str | None, float | None]:
        """Returns (status, unknown_detail, effective_at)."""
        lock_trusted = self.lock_state_trusted()
        if lock_trusted and self._locked:
            return LOCKED, None, None
        if not self.health.input_trustworthy():
            self._trusted_since = None
            return UNKNOWN, "MONITORING_UNAVAILABLE", None
        threshold = self.config.idle_threshold_seconds
        since_input = self.recorder.seconds_since_input()
        if since_input is not None and since_input < threshold:
            if lock_trusted and self._trusted_since is None:
                self._trusted_since = mono
            return ACTIVE, None, now - since_input
        if not lock_trusted:
            # Without a trustworthy lock state, silence may simply mean the
            # session is locked: never turn that into IDLE.
            self._trusted_since = None
            return UNKNOWN, "LOCK_STATE_UNAVAILABLE", None
        if self._trusted_since is None:
            self._trusted_since = mono
        # Quiet time only counts while everything was trustworthy, and the
        # threshold itself is a grace period that is never counted as idle.
        trusted_for = mono - self._trusted_since
        quiet_for = trusted_for if since_input is None else min(since_input, trusted_for)
        if quiet_for >= threshold:
            return IDLE, None, now - (quiet_for - threshold)
        return UNKNOWN, "AWAITING_INPUT", None

    def _poll_window(self, now: float) -> WindowSample | None:
        try:
            changed = self._window_tracker.poll()
        except Exception as exc:  # noqa: BLE001
            self.health.set(WINDOW_WATCH, HealthState.ERROR, f"foreground probe failed: {exc}")
            logger.warning("foreground probe failed: %s", exc)
            return None
        self.health.set(WINDOW_WATCH, HealthState.HEALTHY, "polling")
        if changed is not None:
            self.store.insert_event(
                event_type="APP_CHANGE",
                ts=iso_utc(now),
                session_id=self.session_id,
                app_name=changed.app_name,
                window_title=changed.window_title,
                privacy_excluded=changed.privacy_excluded,
            )
            logger.info("APP_CHANGE app=%s title=%s", changed.app_name, changed.window_title)
        return self._window_tracker.last

    def _on_session_event(self, name: str) -> None:
        with self._state_lock:
            if self.session_id is None:
                self.begin_session()
            self.store.insert_event(event_type=name, ts=iso_utc(self._wall()), session_id=self.session_id)
            logger.info("%s", name)
            if name == "LOCK":
                self._set_lock_state(True)
            elif name == "UNLOCK":
                self._set_lock_state(False)
                # Unlocking requires the user at the keyboard: proof of
                # presence, even though the secure desktop hides the input.
                self.recorder.mark_presence()
            self.tick(reason=name)

    def _set_lock_state(self, locked: bool) -> None:
        self._locked = locked
        self._lock_generation = self.health.generation(SESSION_LOCK)

    def _on_lock_state(self, locked: bool) -> None:
        """Current lock state reported by the watcher when it (re)registers.
        Not a transition, so no LOCK/UNLOCK event is written; the next tick
        classifies with it."""
        with self._state_lock:
            self._set_lock_state(locked)
            logger.info("session lock state: %s", "LOCKED" if locked else "UNLOCKED")

    def _on_period_closed(self, started: float, ended: float) -> None:
        for day in local_days_between(started, ended, self.tz):
            rollup.recompute_day(
                self.store, day, device_id=self.config.device_id, employee_id=self.config.employee_id,
                at=self._wall(), tz=self.tz,
            )

    # ─── retention ─────────────────────────────────────────────────────────
    def run_retention(self, now: float | None = None) -> dict[str, int]:
        deleted = self.store.apply_retention(
            now=self._wall() if now is None else now,
            raw_retention_days=self.config.raw_retention_days,
            synced_retention_days=self.config.synced_retention_days,
        )
        if any(deleted.values()):
            logger.info("retention cleanup removed %s", deleted)
        return deleted

    # ─── real run loop (Windows hooks + periodic sampler) ──────────────────
    def start(self) -> None:
        self.begin_session()
        try:
            self.run_retention()
        except sqlite3.Error as exc:
            logger.warning("retention cleanup failed: %s", exc)
        if self._input_hooks is None:
            from .input_hooks import ZazaInputHooks  # noqa: PLC0415

            self._input_hooks = ZazaInputHooks(self.recorder, health=self.health)
        self._input_hooks.start()
        self._lock_watcher.start()
        self._stop.clear()
        self._timer_thread = threading.Thread(target=self._run_timer, name="ZazaAgentTimer", daemon=True)
        self._timer_thread.start()
        logger.info(
            "ActivityAgent started (employee=%s device=%s idle_threshold=%ss tick=%ss)",
            self.config.employee_id,
            self.config.device_id,
            self.config.idle_threshold_seconds,
            self.config.tick_seconds,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._timer_thread:
            self._timer_thread.join(timeout=self.config.tick_seconds + 2.0)
            self._timer_thread = None
        if self._input_hooks:
            self._input_hooks.stop()
        self._lock_watcher.stop()
        try:
            self.end_session()
        except sqlite3.Error as exc:
            logger.error("could not close work session cleanly: %s", exc)
        self.health.set(WINDOW_WATCH, HealthState.DEGRADED, "stopped")
        logger.info("ActivityAgent stopped")

    def _run_timer(self) -> None:
        last_retention = self._mono()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("tick failed: %s", exc)
            if self._mono() - last_retention >= self.config.retention_interval_seconds:
                last_retention = self._mono()
                try:
                    self.run_retention()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("retention cleanup failed: %s", exc)
            self._stop.wait(self.config.tick_seconds)
