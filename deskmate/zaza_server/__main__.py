"""Development server CLI: ``python -m deskmate.zaza_server <command>``.

Commands:

- ``serve``             run the API (default http://127.0.0.1:8765)
- ``register-device``   add a device for an employee and print its token ONCE
- ``rotate-token``      issue an additional token (optionally revoke the old)
- ``disable-device`` / ``enable-device``
- ``list-devices``
- ``records``           show what has been received (counts + latest)

The development database defaults to ``~/.zaza_server_dev/central_dev.db``
(override with ``--db`` or ``ZAZA_SERVER_DB``). This is NOT production
storage — PostgreSQL arrives in Phase 4.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections import Counter
from pathlib import Path

from .auth import register_device, rotate_token
from .repository import SqliteDevRepository


def default_db() -> Path:
    if env := os.environ.get("ZAZA_SERVER_DB"):
        return Path(env).expanduser()
    return Path.home() / ".zaza_server_dev" / "central_dev.db"


def _print_token(device_id: str, token: str) -> None:
    print(f"\nDevice token for {device_id} (shown once — store it now):\n")
    print(f"    {token}\n")
    print("On the employee PC run:  python -m deskmate.zaza set-credentials")
    print("and paste this token when asked. Do not email or chat it in plain text.\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m deskmate.zaza_server")
    parser.add_argument("--db", type=Path, default=None, help="development database path")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the development API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    reg = sub.add_parser("register-device", help="register a device and issue its token")
    reg.add_argument("--device-id", required=True)
    reg.add_argument("--employee-id", required=True)

    rot = sub.add_parser("rotate-token", help="issue a new token for a device")
    rot.add_argument("--device-id", required=True)
    rot.add_argument("--revoke-old", action="store_true", help="revoke all other tokens for the device")

    for name in ("disable-device", "enable-device"):
        p = sub.add_parser(name)
        p.add_argument("--device-id", required=True)

    sub.add_parser("list-devices")
    rec = sub.add_parser("records", help="show received records")
    rec.add_argument("--device-id")
    rec.add_argument("--limit", type=int, default=15)

    args = parser.parse_args(argv)
    db = args.db or default_db()
    repo = SqliteDevRepository(db)

    if args.command == "serve":
        import uvicorn  # noqa: PLC0415

        from .app import create_app  # noqa: PLC0415

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print("WARNING: listening beyond localhost over plain HTTP. Production must sit behind HTTPS.")
        print(f"ZaZa development sync API on http://{args.host}:{args.port}  (db: {db})")
        uvicorn.run(create_app(repo), host=args.host, port=args.port, log_level="info")
    elif args.command == "register-device":
        issued = register_device(repo, args.device_id, args.employee_id)
        print(f"Registered device {args.device_id} for employee {args.employee_id}.")
        _print_token(args.device_id, issued.token)
    elif args.command == "rotate-token":
        issued = rotate_token(repo, args.device_id, revoke_old=args.revoke_old)
        _print_token(args.device_id, issued.token)
        if args.revoke_old:
            print("All previous tokens for this device are revoked.")
    elif args.command in ("disable-device", "enable-device"):
        repo.set_device_status(args.device_id, "DISABLED" if args.command == "disable-device" else "ACTIVE")
        print(f"{args.device_id}: {'DISABLED' if args.command == 'disable-device' else 'ACTIVE'}")
    elif args.command == "list-devices":
        for d in repo.list_devices():
            print(f"{d.device_id:<28} employee={d.employee_id:<20} {d.status:<9} since {d.created_at}")
    elif args.command == "records":
        rows = repo.list_records(device_id=args.device_id)
        counts = Counter(r.record_type for r in rows)
        print(f"Database: {db}")
        print(f"Total records: {len(rows)}  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        latest = sorted(rows, key=lambda r: r.last_received_at)[-args.limit:]
        for r in latest:
            data = r.payload["data"]
            what = data.get("app_name") or data.get("status") or ""
            print(f"  {r.last_received_at}  {r.record_type:<16} v{r.record_version:<4} {r.device_id}  {what}")
        if not rows:
            print("  (nothing received yet)")
    repo.close()


if __name__ == "__main__":
    main()
