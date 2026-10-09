"""Entry point of the installed product, ``ZaZaWorkAgent.exe``.

Employee / logon (run as the signed-in user, never elevated):

- ``--background``      the recorder (started by the logon task)
- ``--status``          the Status & Privacy window (Start menu)
- ``--enroll``          enroll or re-enroll this device (Start menu)
- ``--first-run``       after installation: enroll if needed, start the
                        agent, show the status
- ``--enroll-stdin``    administrator provisioning: one JSON object
                        ``{"server_url", "device_id", "token"}`` on standard
                        input (never on the command line)
- ``--remove-local-data``  delete this user's ZaZa data (asks first, showing
                        the number of unsynced records)

Installer / uninstaller (run elevated):

- ``--register-autostart`` / ``--unregister-autostart``
- ``--stop-agents``     ask running agents to stop, then terminate stragglers;
                        exits 1 if any agent is still running (setup then
                        stops instead of replacing files in use)
- ``--uninstall-cleanup``  stop agents, then remove the startup task (local
                        data is preserved); exits 1 if an agent survives

Output: the production build has no console window, so text goes to a
redirected standard output if there is one (scripts: ``| Out-String``), else
to the console the command was typed in.

There is deliberately no option to pass a token, disable TLS verification,
or enable any other kind of capture.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import autostart, control, paths
from .edition import PRODUCT_NAME, VERSION, build_flavor, install_dir, is_frozen

_DETACHED = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW


def _exe() -> Path:
    return Path(sys.executable).resolve()


def _out(text: str) -> None:
    """Print, also from the windowed (no console) build: there ``sys.stdout``
    is None unless output is redirected, so attach to the parent console."""
    stream = sys.stdout
    if stream is None and os.name == "nt":
        try:
            import ctypes  # noqa: PLC0415

            if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
                stream = open("CONOUT$", "w", encoding="utf-8")  # noqa: SIM115
        except (OSError, AttributeError):
            stream = None
    if stream is not None:
        try:
            print(text, file=stream, flush=True)
        except (OSError, ValueError):
            pass


def _setup_logging() -> None:
    from .app_logging import configure  # noqa: PLC0415

    flavor = build_flavor()
    configure(paths.logs_dir(), debug=flavor == "development", console=flavor in ("development", "source"))


def start_background() -> None:
    """Launch the recorder as a detached process of the current user."""
    if not is_frozen():
        return
    subprocess.Popen([str(_exe()), "--background"], creationflags=_DETACHED, close_fds=True,  # noqa: S603
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_background() -> int:
    _setup_logging()
    from .logger import get  # noqa: PLC0415
    from .runner import Runner  # noqa: PLC0415

    get("workagent").info("%s %s starting (%s build)", PRODUCT_NAME, VERSION, build_flavor())
    return Runner(install_dir=install_dir()).run()


def enroll_from_stdin() -> int:
    from .enrollment import EnrollmentError, enroll  # noqa: PLC0415

    if sys.stdin is None:
        _out("Enrollment failed: no input (pipe the JSON object to standard input)")
        return 2
    try:
        data = json.loads(sys.stdin.read() or "{}")
        result = enroll(str(data.get("server_url", "")), str(data.get("device_id", "")), str(data.get("token", "")),
                        allow_device_change=bool(data.get("allow_device_change", False)))
    except (ValueError, EnrollmentError) as exc:
        _out(f"Enrollment failed: {exc if isinstance(exc, EnrollmentError) else 'invalid JSON input'}")
        return 2
    finally:
        data = None  # noqa: F841 — drop the token reference promptly
    _out(result.message)
    return 0 if result.ok else 1


def first_run() -> int:
    from .enrollment import current_state  # noqa: PLC0415
    from .ui import show_enrollment, show_status  # noqa: PLC0415

    enrollment, creds_ok = current_state()
    if enrollment is None or not creds_ok:
        show_enrollment()
    start_background()
    time.sleep(3)  # let the agent write its first status before the window opens
    return show_status()


def remove_local_data(*, confirmed: bool = False, ask=None, wait: float = 30.0) -> int:  # noqa: ANN001
    """Stop this user's agent, then delete its data and credentials. Asks
    first, stating how many records are not yet uploaded."""
    from .enrollment import pending_records  # noqa: PLC0415
    from .instance import is_running  # noqa: PLC0415

    root = paths.root()
    if not root.exists():
        _out("No ZaZa Work Agent data for this user.")
        return 0
    was_running = is_running(paths.lock_path())
    if was_running:
        control.request_stop(paths.stop_request_path())
        deadline = time.time() + wait
        while is_running(paths.lock_path()) and time.time() < deadline:
            time.sleep(0.5)
        if is_running(paths.lock_path()):
            _out("The agent did not stop; nothing was deleted.")
            return 1
    unsynced = pending_records()
    if not confirmed:
        if ask is None:
            from .ui import confirm_remove_local_data as ask  # noqa: PLC0415
        if not ask(unsynced):
            control.clear(paths.stop_request_path())
            if was_running:
                start_background()  # cancelled: monitoring resumes, as before the question
            _out("Nothing was deleted.")
            return 1
    from .sync.credentials import delete_credentials  # noqa: PLC0415

    delete_credentials()
    shutil.rmtree(root, ignore_errors=False)
    _out(f"Removed ZaZa Work Agent data for this user ({unsynced} unsynced record(s) deleted).")
    return 0


def stop_agents(*, uninstall: bool = False) -> int:
    """Installer/uninstaller: stop every agent of this installation. Exit 1
    (and, on uninstall, keep the startup task) if one is still running."""
    directory = install_dir() or Path(os.getcwd())
    control.stop_all(directory)
    left = control.agent_processes()
    if left:
        _out(f"{len(left)} {PRODUCT_NAME} process(es) could not be stopped.")
        return 1
    if uninstall:
        autostart.unregister()
        if autostart.exists():
            _out("The startup task could not be removed.")
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ZaZaWorkAgent", description=f"{PRODUCT_NAME} {VERSION}")
    modes = parser.add_mutually_exclusive_group()
    for flag in ("--background", "--status", "--enroll", "--first-run", "--enroll-stdin", "--remove-local-data",
                 "--register-autostart", "--unregister-autostart", "--stop-agents", "--uninstall-cleanup",
                 "--version"):
        modes.add_argument(flag, action="store_true")
    parser.add_argument("--yes", action="store_true", help="with --remove-local-data: do not ask (administrators)")
    args = parser.parse_args(argv)

    if args.version:
        _out(f"{PRODUCT_NAME} {VERSION}")
        return 0
    if args.status:
        from .ui import show_status  # noqa: PLC0415

        return show_status()
    if args.enroll:
        from .ui import show_enrollment  # noqa: PLC0415

        return 0 if show_enrollment() else 1
    if args.first_run:
        return first_run()
    if args.enroll_stdin:
        return enroll_from_stdin()
    if args.remove_local_data:
        return remove_local_data(confirmed=args.yes)
    if args.register_autostart:
        try:
            autostart.register(_exe(), Path(tempfile.gettempdir()))
        except (OSError, RuntimeError) as exc:
            _out(f"Startup task: {exc}")
            return 1
        return 0
    if args.unregister_autostart:
        autostart.unregister()
        return 1 if autostart.exists() else 0
    if args.stop_agents or args.uninstall_cleanup:
        return stop_agents(uninstall=args.uninstall_cleanup)
    return run_background()


if __name__ == "__main__":
    raise SystemExit(main())
