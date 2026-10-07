"""In-memory keyboard/mouse activity presence tracking.

This is the single place that receives "a key went down" / "a mouse button,
wheel, or move happened" signals. It never receives — and has no parameter
through which it *could* receive — which key, what text, or clipboard/screen
content. Callers can only say "activity happened," nothing else.
"""

from __future__ import annotations

import threading
import time


class ActivityRecorder:
    """Thread-safe boolean activity presence + idle-threshold tracking."""

    def __init__(self, *, idle_threshold_seconds: int, clock=time.monotonic) -> None:
        self.idle_threshold_seconds = idle_threshold_seconds
        self._clock = clock
        self._lock = threading.Lock()
        now = self._clock()
        self._last_keyboard_at: float | None = None
        self._last_mouse_at: float | None = None
        self._last_activity_at: float = now
        # Last time input (or other proof of presence, e.g. an unlock) was
        # actually observed. None until the first one — unlike
        # _last_activity_at, which starts at construction for is_idle().
        self._last_input_at: float | None = None
        # Flags accumulated since the last snapshot() call — this is how the
        # sampler gets "did keyboard/mouse activity occur since I last looked"
        # without storing any per-event detail.
        self._keyboard_since_snapshot = False
        self._mouse_since_snapshot = False

    def record_keyboard(self) -> None:
        now = self._clock()
        with self._lock:
            self._last_keyboard_at = now
            self._last_activity_at = now
            self._last_input_at = now
            self._keyboard_since_snapshot = True

    def record_mouse(self) -> None:
        now = self._clock()
        with self._lock:
            self._last_mouse_at = now
            self._last_activity_at = now
            self._last_input_at = now
            self._mouse_since_snapshot = True

    def mark_presence(self) -> None:
        """Proof the user is present that didn't come through the hooks (a
        session unlock). Counts for idle/active, but sets neither the
        keyboard nor the mouse flag."""
        now = self._clock()
        with self._lock:
            self._last_activity_at = now
            self._last_input_at = now

    def forget_input(self) -> None:
        """Drop the last-input timestamp, e.g. after a telemetry gap, so input
        seen before the gap can't make the user look active after it."""
        with self._lock:
            self._last_input_at = None
            self._keyboard_since_snapshot = False
            self._mouse_since_snapshot = False

    def seconds_since_input(self) -> float | None:
        """Seconds since input was last observed, or None if none has been."""
        with self._lock:
            last = self._last_input_at
        if last is None:
            return None
        return max(0.0, self._clock() - last)

    def snapshot_and_reset(self) -> tuple[bool, bool]:
        """Return (keyboard_active, mouse_active) since the last call, then reset."""
        with self._lock:
            kb, mouse = self._keyboard_since_snapshot, self._mouse_since_snapshot
            self._keyboard_since_snapshot = False
            self._mouse_since_snapshot = False
            return kb, mouse

    def seconds_idle(self) -> float:
        with self._lock:
            last = self._last_activity_at
        return max(0.0, self._clock() - last)

    def is_idle(self) -> bool:
        return self.seconds_idle() >= self.idle_threshold_seconds
