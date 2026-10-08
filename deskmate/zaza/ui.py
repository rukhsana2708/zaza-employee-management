"""Employee-facing windows (tkinter, bundled with the agent):

- **Status & Privacy** (``--status``): agent and sync state, last sync,
  pending records, device ID, server host, monitoring health, and what
  ZaZa does / does not collect. Never the token, no manager data, no
  activity details. No control to stop or pause monitoring.
- **Enrollment** (``--enroll`` / first run): server address, device ID,
  device token (masked; never shown again after saving), verified with the
  server before anything is stored.
- **Remove local data** (``--remove-local-data``, administrators): warns
  with the number of unsynced records before deleting anything.

All logic lives in ``enrollment.py`` / ``status.py``; these are thin views.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from . import paths
from .control import requested
from .edition import PRODUCT_NAME, VERSION, install_dir
from .enrollment import EnrollmentError, current_state, enroll
from .privacy_notice import ACTIVE_HOURS_NOTE, COLLECTS, NEVER_COLLECTS
from .status import COMPONENT_LABELS, SYNC_LABELS, effective_status

AGENT_LABELS = {"RUNNING": "Running", "DEGRADED": "Running - some monitoring degraded",
                "WAITING_FOR_ENROLLMENT": "Running - waiting for enrollment", "STOPPED": "Stopped",
                "STARTING": "Starting"}
HEALTH_LABELS = {"HEALTHY": "OK", "DEGRADED": "Degraded", "ERROR": "Not working"}


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    local = then.astimezone().strftime("%Y-%m-%d %H:%M")
    if seconds < 90:
        return f"{local} (just now)"
    if seconds < 5400:
        return f"{local} ({seconds // 60} min ago)"
    return local


def status_lines() -> list[tuple[str, str]]:
    """The Status section as (label, value) rows — testable without a display."""
    st = effective_status()
    enrollment, creds_ok = current_state()
    rows = [("Version", VERSION), ("Agent status", AGENT_LABELS.get(st.state, st.state))]
    if enrollment is None:
        rows.append(("Enrollment", "Not enrolled - use 'Enroll this device'"))
    else:
        rows += [("Device ID", enrollment.device_id), ("Server", enrollment.server_host)]
        if not creds_ok:
            rows.append(("Enrollment", "Device credentials missing - re-enroll this device"))
    sync = "Not running" if st.state == "STOPPED" else SYNC_LABELS.get(st.sync_state, st.sync_state)
    rows += [("Synchronization", sync), ("Last successful sync", _ago(st.last_sync_at)),
             ("Records waiting to upload", str(st.pending))]
    for component, label in COMPONENT_LABELS.items():
        value = HEALTH_LABELS.get(st.health.get(component, ""), "-") if st.state != "STOPPED" else "-"
        rows.append((label, value))
    return rows


def _watch_stop(root, started: float) -> None:  # noqa: ANN001
    """Windows also close for an installer upgrade/uninstall."""
    machine = install_dir() / "stop.request" if install_dir() else None
    if requested(machine, started) or requested(paths.stop_request_path(), started):
        root.destroy()
        return
    root.after(1000, _watch_stop, root, started)


def show_status() -> int:
    import tkinter as tk  # noqa: PLC0415
    from tkinter import ttk  # noqa: PLC0415

    root = tk.Tk()
    root.title(f"{PRODUCT_NAME} - Status & Privacy")
    root.minsize(560, 600)
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=PRODUCT_NAME, font=("Segoe UI", 16, "bold")).pack(anchor="w")
    ttk.Label(frame, text=f"Version {VERSION}").pack(anchor="w", pady=(0, 10))
    grid = ttk.Frame(frame)
    grid.pack(fill="x")
    value_vars: list[tuple[tk.StringVar, tk.StringVar]] = []

    def refresh() -> None:
        rows = status_lines()
        while len(value_vars) < len(rows):
            k, v = tk.StringVar(), tk.StringVar()
            r = len(value_vars)
            ttk.Label(grid, textvariable=k).grid(row=r, column=0, sticky="w", padx=(0, 16))
            ttk.Label(grid, textvariable=v).grid(row=r, column=1, sticky="w")
            value_vars.append((k, v))
        for (k, v), (label, value) in zip(value_vars, rows, strict=False):
            k.set(label)
            v.set(value)
        root.after(5000, refresh)

    refresh()
    for title, items in (("ZaZa collects", COLLECTS), ("ZaZa does not collect", NEVER_COLLECTS)):
        ttk.Label(frame, text=title, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(14, 2))
        ttk.Label(frame, text="\n".join(f"• {i}" for i in items), wraplength=520, justify="left").pack(anchor="w")
    ttk.Label(frame, text=ACTIVE_HOURS_NOTE, wraplength=520, justify="left").pack(anchor="w", pady=(10, 0))
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(16, 0))
    enrolled, _ = current_state()
    ttk.Button(buttons, text="Re-enroll device..." if enrolled else "Enroll this device",
               command=lambda: show_enrollment(parent=root)).pack(side="left")
    ttk.Button(buttons, text="Close", command=root.destroy).pack(side="right")
    _watch_stop(root, time.time())
    root.mainloop()
    return 0


def show_enrollment(parent=None) -> bool:  # noqa: ANN001
    """Returns True if the device was (re-)enrolled."""
    import tkinter as tk  # noqa: PLC0415
    from tkinter import messagebox, ttk  # noqa: PLC0415

    own_root = parent is None
    win = tk.Tk() if own_root else tk.Toplevel(parent)
    win.title(f"{PRODUCT_NAME} - Enroll this device")
    win.minsize(520, 330)
    frame = ttk.Frame(win, padding=16)
    frame.pack(fill="both", expand=True)
    previous, _ = current_state()
    intro = ("Enter the details your administrator gave you. The token is checked with the server, then stored "
             "protected by Windows for your account only. It will not be shown again.")
    if previous:
        intro = (f"This computer is enrolled as device {previous.device_id} ({previous.server_host}). "
                 "Re-enrolling requires the device token again; the current token is never shown.")
    ttk.Label(frame, text=intro, wraplength=480, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
    url = tk.StringVar(value=previous.server_url if previous else "https://")
    dev = tk.StringVar(value=previous.device_id if previous else "")
    tok = tk.StringVar()
    for r, (label, var, secret) in enumerate((("Server address", url, False), ("Device ID", dev, False),
                                              ("Device token", tok, True)), start=1):
        ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=var, width=46, show="•" if secret else "").grid(row=r, column=1, sticky="we")
    message = tk.StringVar()
    ttk.Label(frame, textvariable=message, wraplength=480, justify="left").grid(row=4, column=0, columnspan=2,
                                                                                 sticky="w", pady=8)
    done = {"ok": False}

    def submit(allow_change: bool = False) -> None:
        message.set("Checking with the server...")
        win.update_idletasks()
        try:
            result = enroll(url.get(), dev.get(), tok.get(), allow_device_change=allow_change)
        except EnrollmentError as exc:
            if "Confirm the change" in str(exc) and messagebox.askyesno(PRODUCT_NAME, str(exc), parent=win):
                return submit(allow_change=True)
            message.set(str(exc))
            return None
        tok.set("")  # the token never stays in the window
        message.set(result.message)
        if result.ok:
            done["ok"] = True
            win.after(1500, win.destroy)
        return None

    buttons = ttk.Frame(frame)
    buttons.grid(row=5, column=0, columnspan=2, sticky="e")
    ttk.Button(buttons, text="Check and save", command=submit).pack(side="left", padx=4)
    ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="left")
    if own_root:
        win.mainloop()
    else:
        win.grab_set()
        win.wait_window()
    return done["ok"]


def confirm_remove_local_data(unsynced: int) -> bool:
    import tkinter as tk  # noqa: PLC0415
    from tkinter import messagebox  # noqa: PLC0415

    root = tk.Tk()
    root.withdraw()
    text = ("Remove all ZaZa Work Agent data for this Windows user from this computer?\n\n"
            "This deletes the local activity database, the stored device credentials, the configuration and "
            "the logs.\n\n")
    if unsynced:
        text += (f"WARNING: {unsynced} activity record(s) have NOT been uploaded yet. "
                 "Unsynced activity records will be permanently deleted.\n\n")
    text += "This cannot be undone."
    answer = messagebox.askyesno(PRODUCT_NAME, text, icon="warning", default="no", parent=root)
    root.destroy()
    return bool(answer)
