"""Foreground application + window title detection.

Same Win32 primitives as ``deskmate/a11y/win_events.py`` (``GetForegroundWindow``
/ ``GetWindowThreadProcessId`` / ``GetWindowTextW`` + a psutil process-name
lookup), reimplemented standalone so this module has no dependency on
DeskMate's event bus, UIA tree reader, or WinEvent hook plumbing — just "what
app/window is in front right now."

Only the application name and window title are read. No window content
(text, OCR, accessibility tree) is touched. For a privacy-excluded
application (see ``privacy.py``) the title is not read at all: the process
name is resolved first, and ``GetWindowTextW`` is skipped when it matches.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .privacy import PrivacyFilter


@dataclass(frozen=True)
class WindowSample:
    app_name: str | None
    window_title: str | None
    # Bare hostname of the active browser tab. Nothing populates this yet
    # (there is no domain detection in Phases 1-2); it exists so the privacy
    # filter and schema are ready for it.
    domain: str | None = None
    privacy_excluded: bool = False


class ForegroundProbe(Protocol):
    def __call__(self) -> WindowSample: ...


_APP_NAME_CACHE: dict[int, str] = {}
_APP_NAME_CACHE_MAX = 128


def _foreground_app_name(pid: int) -> str:
    if os.name != "nt" or not pid:
        return ""
    cached = _APP_NAME_CACHE.get(pid)
    if cached is not None:
        return cached
    try:
        import psutil  # noqa: PLC0415

        name = psutil.Process(pid).name() or ""
    except Exception:  # noqa: BLE001
        name = ""
    if len(_APP_NAME_CACHE) >= _APP_NAME_CACHE_MAX:
        _APP_NAME_CACHE.clear()
    _APP_NAME_CACHE[pid] = name
    return name


def get_foreground_window(privacy: PrivacyFilter | None = None) -> WindowSample:
    """Default probe: real Win32 foreground app/title. No-op off Windows.

    With ``privacy``, an excluded application's title is never read."""
    if os.name != "nt":
        return WindowSample(app_name="", window_title="")
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    hwnd = user32.GetForegroundWindow() or 0
    if not hwnd:
        return WindowSample(app_name="", window_title="")
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app_name = _foreground_app_name(int(pid.value))
    if privacy is not None and privacy.is_excluded_app(app_name):
        # Decided before the title is read — it never enters this process.
        return privacy.apply(WindowSample(app_name=app_name, window_title=None, privacy_excluded=True))
    buf = ctypes.create_unicode_buffer(1024)
    user32.GetWindowTextW(hwnd, buf, 1024)
    return WindowSample(app_name=app_name, window_title=buf.value or "")


def make_foreground_probe(privacy: PrivacyFilter) -> Callable[[], WindowSample]:
    return lambda: get_foreground_window(privacy)


class WindowChangeTracker:
    """Dedupes consecutive identical samples so callers only hear about an
    actual app/title change, not every poll tick."""

    def __init__(self, probe: ForegroundProbe = get_foreground_window) -> None:
        self._probe = probe
        self._last: WindowSample | None = None

    @property
    def last(self) -> WindowSample | None:
        return self._last

    def poll(self) -> WindowSample | None:
        """Return the new sample if it differs from the last one seen, else None."""
        current = self._probe()
        if self._last is not None and current == self._last:
            return None
        self._last = current
        return current
