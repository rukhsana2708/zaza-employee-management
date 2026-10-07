"""Manual local run: ``python -m deskmate.zaza``.

Starts the agent, prints events to the console as they're written (local
console/debug output is acceptable for this phase), and stops cleanly on
Ctrl+C, closing the work session. Component health (keyboard/mouse hooks,
session-lock watcher, foreground-window watcher, SQLite storage) is printed
at startup and again whenever any component changes state, so a failed hook
is visible instead of looking like an idle user.

Commands:

- ``python -m deskmate.zaza``                  run the agent (and the sync
  worker, if ``ZAZA_SYNC_URL`` and device credentials are set up)
- ``python -m deskmate.zaza report``           what is stored locally
  (``--report`` still works)
- ``python -m deskmate.zaza set-credentials``  store this device's token
  (DPAPI-protected; the token is read from a hidden prompt or stdin, never
  from the command line)
- ``python -m deskmate.zaza sync-now``         run sync cycles until caught up
- ``python -m deskmate.zaza sync-status``      show sync health and backlog
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from datetime import date

from .agent import ActivityAgent
from .config import AgentConfig, from_env
from .health import ComponentHealth
from .rollup import format_duration, format_usage
from .storage import ActivityStore
from .sync.credentials import DeviceCredentials, credentials_path, load_credentials, save_credentials
from .sync.factory import build_sync_worker
from .sync.transport import SyncTransport, SyncTransportError
from .sync.worker import SyncHealth, SyncState
from .timeutil import local_day_bounds, parse_iso


def print_report(config: AgentConfig, limit: int = 40) -> None:
    store = ActivityStore(config.db_path)
    try:
        print(f"Database: {store.path} (schema v{store.schema_version()}, journal={store.journal_mode()})\n")
        print("Recent work sessions:")
        for s in store.sessions()[-5:]:
            print(
                f"  {s['session_id'][:8]} {s['status']:<11} {s['started_at']} -> {s['ended_at'] or '...'}  "
                f"tracked {format_duration(s['tracked_seconds'])}  active {format_duration(s['active_seconds'])}  "
                f"idle {format_duration(s['idle_seconds'])}  unknown {format_duration(s['unknown_seconds'])}  "
                f"locked {format_duration(s['locked_seconds'])}  [{s['sync_status']}]"
            )
        today = date.today()
        start, end = local_day_bounds(today)
        print(f"\nActivity periods today (last {limit}):")
        for p in store.periods_overlapping(start, end)[-limit:]:
            title = (p["window_title"] or "")[:50]
            flag = " [private]" if p["privacy_excluded"] else ""
            detail = f"/{p['status_detail']}" if p["status_detail"] else ""
            print(
                f"  {p['started_at'][11:19]}-{p['ended_at'][11:19]} UTC {p['status'] + detail:<26} "
                f"{format_duration(p['duration_seconds']):>8}  {p['app_name'] or '-'}  {title}{flag}"
                f"{'  (open)' if p['is_open'] else ''}"
            )
        print("\nIdle periods today:")
        idle = [i for i in store.idle_periods() if start <= parse_iso(i["ended_at"]) and parse_iso(i["started_at"]) < end]
        for i in idle[-limit:] or []:
            print(f"  {i['started_at'][11:19]}-{i['ended_at'][11:19]} UTC  {format_duration(i['duration_seconds'])}")
        if not idle:
            print("  (none)")
        print(f"\nApplication usage {today.isoformat()}:")
        print(format_usage(store.app_usage(today.isoformat())))
    finally:
        store.close()


def _print_sync_health(health: SyncHealth) -> None:
    print(f"[sync] {health.summary()}")


def run() -> None:
    config = from_env()
    agent = ActivityAgent(config)

    def on_health_change(entry: ComponentHealth) -> None:
        suffix = f" - {entry.detail}" if entry.detail else ""
        print(
            f"[health] {entry.component}: {entry.state.value}{suffix} "
            f"(overall {agent.health.overall().value})"
        )

    agent.health.set_on_change(on_health_change)
    print(
        f"ZaZa activity agent — employee={config.employee_id} "
        f"device={config.device_id} idle_threshold={config.idle_threshold_seconds}s "
        f"db={config.db_path}"
    )
    print("Tracking: active app/window, keyboard/mouse presence, idle, lock/unlock.")
    print("NOT tracking: screenshots, OCR, typed text, clipboard, audio, video.")
    if config.excluded_apps or config.hidden_app_names or config.excluded_domains:
        print(
            f"Privacy exclusions: apps={list(config.excluded_apps)} hidden={list(config.hidden_app_names)} "
            f"domains={list(config.excluded_domains)}"
        )
    print("Press Ctrl+C to stop.\n")
    agent.start()
    print(f"Work session: {agent.session_id}")

    worker, reason = build_sync_worker(config, on_state_change=_print_sync_health)
    if worker is None:
        print(f"[sync] disabled: {reason}. Activity is still recorded locally.")
    else:
        print(f"[sync] uploading to {worker.transport.base_url} every {config.sync_interval_seconds}s")
        worker.start()

    time.sleep(1)  # let the hook/watcher threads report in before the summary
    print(agent.health.format_table() + "\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if worker is not None:
            worker.stop()
        agent.stop()
        if worker is not None:
            try:
                worker.run_once()  # best effort: send the just-closed session
            except Exception:  # noqa: BLE001
                pass
            _print_sync_health(worker.health())
            worker.transport.close()
            worker.store.close()
        print("\nSession closed. Today's application usage:")
        print(format_usage(agent.store.app_usage(date.today().isoformat())))


def set_credentials(config: AgentConfig, device_id: str | None, token_stdin: bool) -> int:
    device_id = device_id or config.device_id
    if token_stdin:
        token = sys.stdin.readline().strip()
    else:
        token = getpass.getpass(f"Paste the device token for {device_id} (input is hidden): ").strip()
    if not token:
        print("No token entered; nothing saved.")
        return 1
    creds = DeviceCredentials(device_id=device_id, token=token)
    path = save_credentials(creds)
    print(f"Saved credentials for device {device_id} to {path}")
    if device_id != config.device_id:
        print(f"NOTE: set ZAZA_DEVICE_ID={device_id} for the agent, so its records match these credentials.")
    if config.sync_url:
        try:
            transport = SyncTransport(config.sync_url, creds, allow_insecure=config.sync_allow_insecure_http)
            info = transport.check_device()
            print(f"Server accepted the token: device {info.device_id}, employee {info.employee_id}.")
            if info.employee_id != config.employee_id:
                print(f"NOTE: set ZAZA_EMPLOYEE_ID={info.employee_id} so session records match the registration.")
            transport.close()
        except (SyncTransportError, ValueError) as exc:
            print(f"Saved, but could not verify with the server: {exc}")
    return 0


def sync_now(config: AgentConfig) -> int:
    worker, reason = build_sync_worker(config)
    if worker is None:
        print(f"Sync unavailable: {reason}")
        return 1
    try:
        for _ in range(100):
            result = worker.run_once()
            if not result.ok:
                print(f"Sync failed: {result.error_kind}: {result.error}")
                break
            print(
                f"Sent {result.sent} records in {result.batches} batch(es): confirmed={result.confirmed} "
                f"stale={result.stale} conflicts={result.conflicts} failed={result.failed}"
            )
            if not result.more_pending:
                break
        _print_sync_health(worker.health())
        return 0 if worker.health().state in (SyncState.HEALTHY, SyncState.BACKLOG) else 2
    finally:
        worker.transport.close()
        worker.store.close()


def sync_status(config: AgentConfig) -> int:
    worker, reason = build_sync_worker(config)
    if worker is None:
        store = ActivityStore(config.db_path)
        counts = store.sync_counts()
        store.close()
        print(f"[sync] NOT_CONFIGURED: {reason}")
        print(f"[sync] local backlog: pending={counts['pending']} failed={counts['failed']} open={counts['open']}")
        return 1
    health = worker.health()
    for key, value in health.__dict__.items():
        print(f"  {key:<22} {value.value if isinstance(value, SyncState) else value}")
    worker.transport.close()
    worker.store.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m deskmate.zaza")
    parser.add_argument("--report", action="store_true", help="print locally stored data and exit")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the agent (default)")
    sub.add_parser("report", help="print locally stored data and exit")
    creds = sub.add_parser("set-credentials", help="store this device's sync token")
    creds.add_argument("--device-id", help="defaults to ZAZA_DEVICE_ID")
    creds.add_argument("--token-stdin", action="store_true", help="read the token from stdin instead of a prompt")
    sub.add_parser("sync-now", help="sync until caught up, then exit")
    sub.add_parser("sync-status", help="show sync health and local backlog")
    args = parser.parse_args()
    config = from_env()

    if args.report or args.command == "report":
        print_report(config)
    elif args.command == "set-credentials":
        raise SystemExit(set_credentials(config, args.device_id, args.token_stdin))
    elif args.command == "sync-now":
        raise SystemExit(sync_now(config))
    elif args.command == "sync-status":
        raise SystemExit(sync_status(config))
    else:
        run()


if __name__ == "__main__":
    main()
