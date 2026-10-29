"""OS-level seconds since the last user input (GetLastInputInfo).

Used only as a cross-check on the low-level input hooks: Windows can silently
remove a hook that times out (LowLevelHooksTimeout), after which the hooks
report silence while the user is still typing. The result is a number of
seconds; no input content is ever read."""

from __future__ import annotations

import sys


def os_seconds_since_input() -> float | None:
    """Seconds since the OS last saw keyboard/mouse input, or None if unknown."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:  # noqa: BLE001
        return None
