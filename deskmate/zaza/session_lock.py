"""Windows session lock/unlock detection via WTS session notifications.

Not derived from any existing DeskMate module — DeskMate has no lock/unlock
watcher today. Uses a hidden message-only window + ``WTSRegisterSessionNotification``
and listens for ``WM_WTSSESSION_CHANGE``. Only the lock/unlock transition
itself is observed; no window/session content is read.

Right after registering, the watcher also asks Windows for the session's
*current* lock state (``WTSQuerySessionInformationW`` / ``WTSSessionInfoEx``,
reading only ``SessionFlags``) and reports it via ``on_state``. Without that
the agent would have to assume "unlocked" at start, or keep trusting a lock
state from before a watcher outage. If the current state can't be determined,
the watcher stays DEGRADED until the next lock/unlock event establishes it.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading
from collections.abc import Callable

from .health import SESSION_LOCK, HealthRegistry, HealthState
from .logger import get

logger = get("session_lock")

WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
HWND_MESSAGE = -3
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_SESSION_INFO_EX = 25  # WTS_INFO_CLASS.WTSSessionInfoEx
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1
# WTSINFOEXW: DWORD Level, then (8-byte aligned) WTSINFOEX_LEVEL1_W:
# ULONG SessionId, WTS_CONNECTSTATE_CLASS SessionState, LONG SessionFlags.
_WTSINFOEX_LEVEL_OFFSET = 0
_WTSINFOEX_SESSION_FLAGS_OFFSET = 16

# LRESULT is pointer-sized and signed (LONG_PTR): 64 bits on Win64, so window
# procedures and DefWindowProcW must not be declared as returning ``c_long``.
LRESULT = ctypes.c_ssize_t

WNDPROC = (
    ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)  # type: ignore[attr-defined]
    if os.name == "nt"
    else None
)

_EVENT_NAMES = {WTS_SESSION_LOCK: "LOCK", WTS_SESSION_UNLOCK: "UNLOCK"}


def classify_session_flags(flags: int) -> bool | None:
    """WTSINFOEX ``SessionFlags`` -> True (locked) / False (unlocked) / None
    (unknown). Windows 10/11 semantics; Windows 7/2008 R2 report these
    inverted, and are not supported targets."""
    return {WTS_SESSIONSTATE_LOCK: True, WTS_SESSIONSTATE_UNLOCK: False}.get(flags)


def query_session_locked() -> bool | None:
    """Ask Windows whether the current session is locked right now. Reads
    only the ``Level`` and ``SessionFlags`` fields of the returned struct."""
    if os.name != "nt":
        return None
    wtsapi32 = ctypes.windll.wtsapi32  # type: ignore[attr-defined]
    wtsapi32.WTSQuerySessionInformationW.restype = wt.BOOL
    wtsapi32.WTSQuerySessionInformationW.argtypes = [
        wt.HANDLE, wt.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD),
    ]
    wtsapi32.WTSFreeMemory.restype = None
    wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    buf = ctypes.c_void_p()
    size = wt.DWORD(0)
    if not wtsapi32.WTSQuerySessionInformationW(
        None, WTS_CURRENT_SESSION, WTS_SESSION_INFO_EX, ctypes.byref(buf), ctypes.byref(size)
    ):
        return None
    try:
        if not buf.value or size.value < _WTSINFOEX_SESSION_FLAGS_OFFSET + 4:
            return None
        level = ctypes.c_uint32.from_address(buf.value + _WTSINFOEX_LEVEL_OFFSET).value
        if level != 1:
            return None
        flags = ctypes.c_int32.from_address(buf.value + _WTSINFOEX_SESSION_FLAGS_OFFSET).value
    finally:
        wtsapi32.WTSFreeMemory(buf)
    return classify_session_flags(flags)


def classify_session_change(wparam: int) -> str | None:
    """Pure mapping from a WM_WTSSESSION_CHANGE wParam to "LOCK"/"UNLOCK"/None.

    Kept separate from the window/message-loop plumbing below so it is
    trivially unit-testable without a real Windows session.
    """
    return _EVENT_NAMES.get(wparam)


class SessionLockWatcher:
    """Owns a hidden message-only window registered for session notifications."""

    def __init__(
        self,
        on_event: Callable[[str], None],
        health: HealthRegistry | None = None,
        on_state: Callable[[bool], None] | None = None,
        query_locked: Callable[[], bool | None] = query_session_locked,
    ) -> None:
        self._on_event = on_event
        self._on_state = on_state
        self._query_locked = query_locked
        self._health = health or HealthRegistry()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tid = 0

    @property
    def available(self) -> bool:
        return os.name == "nt"

    def start(self) -> None:
        if self._thread:
            return
        if not self.available:
            self._health.set(SESSION_LOCK, HealthState.ERROR, "unsupported platform (Windows only)")
            return
        self._stop.clear()
        self._health.set(SESSION_LOCK, HealthState.DEGRADED, "starting")
        self._thread = threading.Thread(target=self._run, name="ZazaSessionLock", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._tid and os.name == "nt":
            try:
                ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._health.set(SESSION_LOCK, HealthState.DEGRADED, "stopped")

    def _established(self) -> None:
        """Called once notifications are registered: HEALTHY only if the
        current lock state is known, and that state is reported *after*
        HEALTHY is set so it belongs to the new healthy stint."""
        try:
            locked = self._query_locked()
        except Exception as exc:  # noqa: BLE001
            logger.warning("lock state query failed: %s", exc)
            locked = None
        if locked is None:
            self._health.set(
                SESSION_LOCK, HealthState.DEGRADED, "registered; current lock state unknown until next lock/unlock"
            )
            return
        self._health.set(SESSION_LOCK, HealthState.HEALTHY, "registered for session notifications")
        if self._on_state is not None:
            self._on_state(locked)

    def _deliver(self, name: str) -> None:
        """A LOCK/UNLOCK notification is definitive: it (re)establishes the
        lock state even if the initial query couldn't."""
        if self._health.get(SESSION_LOCK).state == HealthState.DEGRADED and not self._stop.is_set():
            self._health.set(SESSION_LOCK, HealthState.HEALTHY, "registered for session notifications")
        self._on_event(name)

    def _run(self) -> None:
        try:
            self._run_message_loop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("session lock watcher failed: %s", exc)
            self._health.set(SESSION_LOCK, HealthState.ERROR, f"watcher failed: {exc}")
            return
        if not self._stop.is_set() and self._health.get(SESSION_LOCK).state != HealthState.ERROR:
            self._health.set(SESSION_LOCK, HealthState.ERROR, "message loop exited unexpectedly")

    def _run_message_loop(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        wtsapi32 = ctypes.windll.wtsapi32  # type: ignore[attr-defined]

        # Pin correct 64-bit signatures. Without these, ctypes defaults integer
        # args/return values to a 32-bit C int, truncating/overflowing on the
        # pointer-sized handles below (same failure mode called out in
        # ``deskmate/a11y/input_hooks.py`` for SetWindowsHookExW).
        kernel32.GetModuleHandleW.restype = wt.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
        user32.RegisterClassW.restype = wt.ATOM
        user32.CreateWindowExW.restype = wt.HWND
        user32.CreateWindowExW.argtypes = [
            wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
        ]
        user32.DestroyWindow.restype = wt.BOOL
        user32.DestroyWindow.argtypes = [wt.HWND]
        wtsapi32.WTSRegisterSessionNotification.restype = wt.BOOL
        wtsapi32.WTSRegisterSessionNotification.argtypes = [wt.HWND, wt.DWORD]
        wtsapi32.WTSUnRegisterSessionNotification.restype = wt.BOOL
        wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wt.HWND]
        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, ctypes.c_uint, ctypes.c_uint]

        def _wnd_proc(hwnd, msg, wparam, lparam):  # noqa: ANN001
            if msg == WM_WTSSESSION_CHANGE:
                name = classify_session_change(wparam)
                if name:
                    try:
                        self._deliver(name)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("session lock handler err: %s", exc)
                return 0
            if msg in (WM_DESTROY, WM_CLOSE):
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR),
            ]

        wnd_proc = WNDPROC(_wnd_proc)
        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "ZazaSessionLockWatcher"

        wndclass = WNDCLASS()
        wndclass.style = 0
        wndclass.lpfnWndProc = wnd_proc
        wndclass.cbClsExtra = 0
        wndclass.cbWndExtra = 0
        wndclass.hInstance = hinstance
        wndclass.hIcon = None
        wndclass.hCursor = None
        wndclass.hbrBackground = None
        wndclass.lpszMenuName = None
        wndclass.lpszClassName = class_name

        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            # Class may already be registered from a previous run in-process.
            pass

        hwnd = user32.CreateWindowExW(
            0, class_name, "ZazaSessionLockWatcher", 0, 0, 0, 0, 0,
            wt.HWND(HWND_MESSAGE), None, hinstance, None,
        )
        if not hwnd:
            err = kernel32.GetLastError()
            logger.error("failed to create message-only window for session notifications")
            self._health.set(SESSION_LOCK, HealthState.ERROR, f"CreateWindowExW failed (GetLastError={err})")
            return

        if wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION):
            self._established()
        else:
            err = kernel32.GetLastError()
            logger.error("WTSRegisterSessionNotification failed (GetLastError=%s)", err)
            self._health.set(
                SESSION_LOCK, HealthState.ERROR, f"WTSRegisterSessionNotification failed (GetLastError={err})"
            )

        self._tid = int(kernel32.GetCurrentThreadId())

        msg = wt.MSG()
        while not self._stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if r == 0 or r == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        try:
            wtsapi32.WTSUnRegisterSessionNotification(hwnd)
        except Exception:  # noqa: BLE001
            pass
        try:
            user32.DestroyWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
