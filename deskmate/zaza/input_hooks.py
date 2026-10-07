"""Low-level keyboard + mouse hooks — presence only.

Modeled on the hook-installation pattern in ``deskmate/a11y/input_hooks.py``
(``WH_KEYBOARD_LL`` / ``WH_MOUSE_LL`` on a dedicated message-pump thread), but
stripped to the one thing Phase 1 needs: "did a key go down" / "did a mouse
button, wheel, or move happen." The callbacks below never cast ``lparam`` to
read the keystroke (``vkCode``) or pointer (``pt``/coordinates) struct — they
only ever look at ``wparam``, the Windows message identifier (WM_KEYDOWN,
WM_LBUTTONDOWN, ...), so there is no code path here that can see, let alone
store, which key was pressed or where the mouse was.

Contrast with upstream ``deskmate.a11y.input_hooks.InputHooks``, which *does*
read key codes, captures the full text of the focused input box on Enter via
UIA, and triggers screenshot capture — none of that is imported or
replicated here. See SECURITY.md.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading
from collections.abc import Callable

from .health import KEYBOARD_HOOK, MOUSE_HOOK, HealthRegistry, HealthState
from .logger import get
from .recorder import ActivityRecorder

logger = get("zaza.input_hooks")

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_XBUTTONDOWN = 0x020B
WM_MOUSEWHEEL = 0x020A
WM_MOUSEMOVE = 0x0200
WM_QUIT = 0x0012
HC_ACTION = 0

_KEY_DOWN_MESSAGES = frozenset({WM_KEYDOWN, WM_SYSKEYDOWN})
_MOUSE_ACTIVITY_MESSAGES = frozenset(
    {WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN, WM_MOUSEWHEEL, WM_MOUSEMOVE}
)

# LRESULT is pointer-sized and signed (LONG_PTR): 64 bits on Win64. A 32-bit
# ``c_long`` return type would truncate whatever CallNextHookEx hands back.
LRESULT = ctypes.c_ssize_t

HOOKPROC = (
    ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)  # type: ignore[attr-defined]
    if os.name == "nt"
    else None
)


def is_key_down_message(wparam: int) -> bool:
    """Pure classifier (no ctypes/struct access) — easy to unit test."""
    return wparam in _KEY_DOWN_MESSAGES


def is_mouse_activity_message(wparam: int) -> bool:
    return wparam in _MOUSE_ACTIVITY_MESSAGES


class ZazaInputHooks:
    """Installs WH_KEYBOARD_LL + WH_MOUSE_LL and feeds boolean presence only
    into an :class:`ActivityRecorder`. No keystroke or pointer content is ever
    read out of the hook structs."""

    def __init__(self, recorder: ActivityRecorder, health: HealthRegistry | None = None) -> None:
        self._recorder = recorder
        self._health = health or HealthRegistry()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tid = 0
        self._call_next = None
        self._kproc = None
        self._mproc = None

    @property
    def available(self) -> bool:
        return os.name == "nt"

    def start(self) -> None:
        if self._thread:
            return
        if not self.available:
            for component in (KEYBOARD_HOOK, MOUSE_HOOK):
                self._health.set(component, HealthState.ERROR, "unsupported platform (Windows only)")
            return
        self._stop.clear()
        for component in (KEYBOARD_HOOK, MOUSE_HOOK):
            self._health.set(component, HealthState.DEGRADED, "installing")
        self._thread = threading.Thread(target=self._run, name="ZazaInputHooks", daemon=True)
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
        for component in (KEYBOARD_HOOK, MOUSE_HOOK):
            self._health.set(component, HealthState.DEGRADED, "stopped")

    def _run(self) -> None:
        try:
            self._run_message_loop()
        except Exception as exc:  # noqa: BLE001
            logger.error("input hook thread failed: %s", exc)
            for component in (KEYBOARD_HOOK, MOUSE_HOOK):
                self._health.set(component, HealthState.ERROR, f"hook thread failed: {exc}")

    @staticmethod
    def _bind_signatures(user32, kernel32) -> None:  # noqa: ANN001
        kernel32.GetModuleHandleW.restype = wt.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wt.HMODULE, wt.DWORD]
        user32.UnhookWindowsHookEx.restype = wt.BOOL
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int

    def _install(
        self,
        user32,  # noqa: ANN001
        kernel32,  # noqa: ANN001
        hookproc_factory: Callable | None = None,
    ) -> tuple[int, int]:
        """Install both hooks and report each one's health. Returns the raw
        (keyboard, mouse) hook handles; 0 means that install failed.

        Split out from the message loop so tests can drive it with fake
        ``user32``/``kernel32`` objects and simulate install failures."""
        factory = hookproc_factory or HOOKPROC
        self._call_next = user32.CallNextHookEx
        mod = kernel32.GetModuleHandleW(None)
        self._kproc = factory(self._kb_callback)
        self._mproc = factory(self._mouse_callback)

        handles = []
        for component, hook_id, proc in (
            (KEYBOARD_HOOK, WH_KEYBOARD_LL, self._kproc),
            (MOUSE_HOOK, WH_MOUSE_LL, self._mproc),
        ):
            handle = user32.SetWindowsHookExW(hook_id, proc, mod, 0) or 0
            if handle:
                self._health.set(component, HealthState.HEALTHY, "installed")
            else:
                err = kernel32.GetLastError()
                logger.error("%s install failed (GetLastError=%s)", component, err)
                self._health.set(
                    component, HealthState.ERROR, f"SetWindowsHookExW failed (GetLastError={err})"
                )
            handles.append(handle)
        return handles[0], handles[1]

    def _run_message_loop(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self._bind_signatures(user32, kernel32)

        khook, mhook = self._install(user32, kernel32)
        self._tid = int(kernel32.GetCurrentThreadId())

        msg = wt.MSG()
        while not self._stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if r == 0 or r == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if not self._stop.is_set():
            # The pump ended without stop() asking it to — hooks are no longer
            # being serviced, so input absence can't be trusted from here on.
            for component in (KEYBOARD_HOOK, MOUSE_HOOK):
                self._health.set(component, HealthState.ERROR, "message loop exited unexpectedly")

        if khook:
            user32.UnhookWindowsHookEx(khook)
        if mhook:
            user32.UnhookWindowsHookEx(mhook)

    def _kb_callback(self, ncode: int, wparam: int, lparam: int) -> int:
        try:
            if ncode == HC_ACTION and is_key_down_message(wparam):
                self._recorder.record_keyboard()
        except Exception as exc:  # noqa: BLE001
            logger.debug("kb cb err: %s", exc)
        return self._call_next(0, ncode, wparam, lparam)

    def _mouse_callback(self, ncode: int, wparam: int, lparam: int) -> int:
        try:
            if ncode == HC_ACTION and is_mouse_activity_message(wparam):
                self._recorder.record_mouse()
        except Exception as exc:  # noqa: BLE001
            logger.debug("mouse cb err: %s", exc)
        return self._call_next(0, ncode, wparam, lparam)
