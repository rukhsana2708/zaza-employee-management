"""Server CLI: ``python -m deskmate.zaza_server <command>``.

Commands:

- ``serve``             run the API (default http://127.0.0.1:8765)
- ``add-employee``      add an employee (needed before registering a device)
- ``list-employees``
- ``register-device``   add a device for an employee and print its token ONCE
- ``rotate-token``      issue an additional token (optionally revoke the old)
- ``disable-device`` / ``enable-device``
- ``list-devices``
- ``records``           show what has been received (counts + latest)
- ``migrate``           (PostgreSQL) apply schema migrations up to the latest
- ``db-status``         (PostgreSQL) show connection target and schema revision

Attendance (PostgreSQL only, Phase 5):

- ``add-schedule`` / ``list-schedules``   weekly rule, one-off date, or day off
- ``summarize-day``     calculate (upsert) daily summaries for a date
- ``summarize-week``    the ISO week (Mon–Sun) containing a date
- ``summarize-month``   a calendar month (``--month 2026-10``)
- ``recalculate``       a date range, or ``--recent-days N`` (whole weeks/months)

Backend: ``--backend sqlite|postgres`` or ``ZAZA_SERVER_BACKEND`` (default
``sqlite``). The SQLite development database defaults to
``~/.zaza_server_dev/central_dev.db`` (override with ``--db`` or
``ZAZA_SERVER_DB``). PostgreSQL settings come from the environment — see
``deskmate/zaza_server/config.py`` and ``.env.example``.

``serve`` never changes the database schema: on PostgreSQL it refuses to
start until ``migrate`` has been run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from datetime import date, time
from pathlib import Path

from .auth import register_device, rotate_token
from .config import BACKENDS, ConfigError, backend_from_env, database_settings_from_env
from .repository import RepositoryUnavailable


def default_db() -> Path:
    if env := os.environ.get("ZAZA_SERVER_DB"):
        return Path(env).expanduser()
    return Path.home() / ".zaza_server_dev" / "central_dev.db"


def _print_token(device_id: str, token: str) -> None:
    print(f"\nDevice token for {device_id} (shown once — store it now):\n")
    print(f"    {token}\n")
    print("On the employee PC run:  python -m deskmate.zaza set-credentials")
    print("and paste this token when asked. Do not email or chat it in plain text.\n")


def _migrate(show_only: bool) -> int:
    import psycopg  # noqa: PLC0415

    from .postgres import migrate  # noqa: PLC0415

    settings = database_settings_from_env()
    print(f"Database: {settings.display()}")
    try:
        with psycopg.connect(**settings.connect_kwargs()) as conn:
            before = migrate.current_revision(conn)
    except psycopg.Error as exc:
        print(f"Cannot connect: {settings.scrub(str(exc)).strip()}", file=sys.stderr)
        return 2
    head = migrate.head_revision()
    print(f"Schema revision: {before or '(empty database)'}   latest: {head}")
    if show_only:
        print("Up to date." if before == head else "Run `python -m deskmate.zaza_server migrate` to upgrade.")
        return 0
    if before == head:
        print("Nothing to do: the schema is already up to date.")
        return 0
    migrate.upgrade(settings)
    print(f"Upgraded {before or '(empty)'} -> {head}.")
    return 0


def _date(text: str) -> date:
    return date.fromisoformat(text)


def _time(text: str) -> time:
    return time.fromisoformat(text)


def _month(text: str) -> date:
    return date.fromisoformat(f"{text}-01")


def _weekdays(text: str) -> list[int]:
    days: set[int] = set()
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        days.update(range(int(lo), int(hi or lo) + 1))
    if not days or not days <= set(range(1, 8)):
        raise ValueError("weekdays must be 1..7 (1 = Monday)")
    return sorted(days)


_ATTENDANCE_COMMANDS = ("add-schedule", "list-schedules", "summarize-day", "summarize-week", "summarize-month",
                        "recalculate")


def _print_day(s) -> None:  # noqa: ANN001
    def hours(sec: int) -> str:
        return f"{sec / 3600:5.2f}h"

    pct = f"{s.attendance_percentage:6.2f}%" if s.attendance_percentage is not None else "     - "
    print(f"  {s.employee_id:<12} {s.local_date}  {s.attendance_status.value:<16} sched {hours(s.scheduled_seconds)}"
          f"  active {hours(s.active_seconds)}  idle {hours(s.idle_seconds)}  unknown {hours(s.unknown_seconds)}"
          f"  locked {hours(s.locked_seconds)}  late {s.late_seconds // 60:>3}m  early {s.early_leave_seconds // 60:>3}m"
          f"  overtime {s.overtime_seconds // 60:>4}m  attendance {pct}  {s.data_quality.value}"
          f"{'  (provisional)' if s.is_provisional else ''}")


def _print_period(p) -> None:  # noqa: ANN001
    pct = f"{p.attendance_percentage:.2f}%" if p.attendance_percentage is not None else "-"
    print(f"  {p.employee_id:<12} {p.period_kind} {p.period_start}..{p.period_end}  working days {p.working_days}"
          f"  worked {p.days_worked}  absent {p.absent_days}  late {p.late_days}  early {p.early_leave_days}"
          f"  incomplete {p.incomplete_days}  active {p.active_seconds / 3600:.2f}h"
          f"  overtime {p.overtime_seconds / 3600:.2f}h  attendance {pct}  {p.data_quality.value}"
          f"{'  (provisional)' if p.is_provisional else ''}")


def _attendance(args: argparse.Namespace) -> int:
    from .attendance import AttendancePolicy, SummaryService  # noqa: PLC0415
    from .attendance.postgres_store import PostgresAttendanceStore  # noqa: PLC0415
    from .postgres import PostgresRepository  # noqa: PLC0415

    repo = PostgresRepository(database_settings_from_env(), actor_type="CLI", actor_id="server-cli")
    try:
        store = PostgresAttendanceStore(repo)
        if args.command == "add-schedule":
            expected = round(args.expected_hours * 3600) if args.expected_hours is not None else None
            common = {"timezone": args.timezone, "is_working_day": not args.day_off,
                      "start_time": None if args.day_off else args.start,
                      "end_time": None if args.day_off else args.end,
                      "expected_work_seconds": None if args.day_off else expected}
            if args.date:
                store.add_schedule(args.employee_id, schedule_date=args.date, **common)
                print(f"Added schedule for {args.employee_id} on {args.date}.")
            else:
                start = args.effective_from or date.today()
                for dow in _weekdays(args.weekdays):
                    store.add_schedule(args.employee_id, day_of_week=dow, effective_from=start,
                                       effective_to=args.effective_to, **common)
                print(f"Added weekly schedule for {args.employee_id}, weekdays {args.weekdays}, from {start}.")
            return 0
        if args.command == "list-schedules":
            for r in store.schedules(args.employee_id):
                if r.schedule_date:
                    when = f"date {r.schedule_date}"
                else:
                    when = f"weekday {r.day_of_week} from {r.effective_from} to {r.effective_to or 'open'}"
                if r.is_working_day:
                    hours = f"{r.start_time:%H:%M}-{r.end_time:%H:%M} expected {r.expected_work_seconds / 3600:g}h"
                else:
                    hours = "day off"
                print(f"  {when:<40} {hours:<32} {r.timezone}")
            return 0

        service = SummaryService(store, policy=AttendancePolicy.from_env())
        ids = [args.employee_id] if args.employee_id else [e.employee_id for e in store.list_employees()]
        if args.command == "recalculate":
            if args.recent_days is not None:
                start, end = service.recent_range(args.recent_days)
            elif args.date_from and args.date_to:
                start, end = args.date_from, args.date_to
            else:
                raise ValueError("give --from and --to, or --recent-days N")
            result = service.recalculate(start, end, ids)
            print(f"Recalculated {result.days} day(s) ({result.days_changed} changed), "
                  f"{result.weeks} week(s), {result.months} month(s) for {len(ids)} employee(s).")
            return 0
        for employee_id in ids:
            if args.command == "summarize-day":
                _print_day(service.calculate_daily(employee_id, args.date))
            elif args.command == "summarize-week":
                _print_period(service.calculate_week(employee_id, args.date))
            else:
                _print_period(service.calculate_month(employee_id, args.month))
        return 0
    finally:
        repo.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m deskmate.zaza_server")
    parser.add_argument("--db", type=Path, default=None, help="SQLite development database path")
    parser.add_argument("--backend", choices=BACKENDS, default=None,
                        help="repository backend (default: ZAZA_SERVER_BACKEND or sqlite)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the sync API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    emp = sub.add_parser("add-employee", help="add an employee")
    emp.add_argument("--employee-id", required=True)
    emp.add_argument("--name", required=True)
    emp.add_argument("--role", default="EMPLOYEE", choices=("EMPLOYEE", "MANAGER", "ADMIN"))
    emp.add_argument("--timezone", default="UTC", help="IANA name used for reports, e.g. Asia/Dhaka")
    sub.add_parser("list-employees")

    reg = sub.add_parser("register-device", help="register a device and issue its token")
    reg.add_argument("--device-id", required=True)
    reg.add_argument("--employee-id", required=True)
    reg.add_argument("--name", default=None, help="display name, e.g. 'Alice laptop'")

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

    sub.add_parser("migrate", help="(PostgreSQL) apply schema migrations")
    sub.add_parser("db-status", help="(PostgreSQL) show schema revision")

    sch = sub.add_parser("add-schedule", help="(PostgreSQL) add a schedule rule")
    sch.add_argument("--employee-id", required=True)
    when = sch.add_mutually_exclusive_group(required=True)
    when.add_argument("--weekdays", help="ISO weekdays, e.g. 1-5 or 6,7 (1=Monday)")
    when.add_argument("--date", type=_date, help="one-off rule for this local date (overrides weekly rules)")
    sch.add_argument("--start", type=_time, help="HH:MM local start (shift belongs to the day it starts)")
    sch.add_argument("--end", type=_time, help="HH:MM local end; earlier than start = ends next day")
    sch.add_argument("--expected-hours", type=float, help="expected work hours (> 0, <= shift span)")
    sch.add_argument("--day-off", action="store_true")
    sch.add_argument("--timezone", required=True, help="IANA name, e.g. Asia/Dhaka")
    sch.add_argument("--effective-from", type=_date, help="weekly rules: first date (default today)")
    sch.add_argument("--effective-to", type=_date, help="weekly rules: last date (default open-ended)")
    lsch = sub.add_parser("list-schedules", help="(PostgreSQL) list schedule rules")
    lsch.add_argument("--employee-id", required=True)

    for name, helptext in (("summarize-day", "daily summaries for one local date"),
                           ("summarize-week", "the ISO week (Mon-Sun) containing --date"),
                           ("summarize-month", "a calendar month")):
        p = sub.add_parser(name, help=f"(PostgreSQL) {helptext}")
        p.add_argument("--employee-id", help="default: every active employee")
        if name == "summarize-month":
            p.add_argument("--month", type=_month, required=True, help="YYYY-MM")
        else:
            p.add_argument("--date", type=_date, required=True, help="YYYY-MM-DD (local date)")
    recalc = sub.add_parser("recalculate", help="(PostgreSQL) recalculate a date range (whole weeks/months)")
    recalc.add_argument("--employee-id", help="default: every active employee")
    recalc.add_argument("--from", dest="date_from", type=_date)
    recalc.add_argument("--to", dest="date_to", type=_date)
    recalc.add_argument("--recent-days", type=int, help="instead of --from/--to: the last N days up to today")

    args = parser.parse_args(argv)
    try:
        backend = args.backend or backend_from_env()
        if args.command in ("migrate", "db-status"):
            return _migrate(show_only=args.command == "db-status")
        if args.command in _ATTENDANCE_COMMANDS:
            if backend != "postgres":
                raise ConfigError("attendance summaries need the PostgreSQL backend (ZAZA_SERVER_BACKEND=postgres)")
            return _attendance(args)
        return _run(args, backend)
    except (ConfigError, RepositoryUnavailable) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, backend: str) -> int:
    from .backends import open_repository  # noqa: PLC0415

    db = args.db or default_db()
    repo = open_repository(backend, sqlite_path=db, actor_type="CLI", actor_id="server-cli")
    where = f"SQLite {db}" if backend == "sqlite" else database_settings_from_env().display()
    try:
        if args.command == "serve":
            import uvicorn  # noqa: PLC0415

            from .app import create_app  # noqa: PLC0415

            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
            if args.host not in ("127.0.0.1", "localhost", "::1"):
                print("WARNING: listening beyond localhost over plain HTTP. Production must sit behind HTTPS.")
            print(f"ZaZa sync API on http://{args.host}:{args.port}  ({backend}: {where})")
            uvicorn.run(create_app(repo), host=args.host, port=args.port, log_level="info")
        elif args.command == "add-employee":
            e = repo.add_employee(args.employee_id, args.name, role=args.role, timezone=args.timezone)
            print(f"Added employee {e.employee_id} ({e.display_name}, {e.role}, {e.timezone}).")
        elif args.command == "list-employees":
            for e in repo.list_employees():
                print(f"{e.employee_id:<20} {e.display_name:<28} {e.role:<9} {e.timezone:<20} "
                      f"{'active' if e.is_active else 'inactive'}")
        elif args.command == "register-device":
            issued = register_device(repo, args.device_id, args.employee_id, display_name=args.name)
            print(f"Registered device {args.device_id} for employee {args.employee_id}.")
            _print_token(args.device_id, issued.token)
        elif args.command == "rotate-token":
            issued = rotate_token(repo, args.device_id, revoke_old=args.revoke_old)
            _print_token(args.device_id, issued.token)
            if args.revoke_old:
                print("All previous tokens for this device are revoked.")
        elif args.command in ("disable-device", "enable-device"):
            status = "DISABLED" if args.command == "disable-device" else "ACTIVE"
            repo.set_device_status(args.device_id, status)
            print(f"{args.device_id}: {status}")
        elif args.command == "list-devices":
            for d in repo.list_devices():
                print(f"{d.device_id:<28} employee={d.employee_id:<20} {d.status:<9} since {d.created_at}"
                      f"  last seen {d.last_seen_at or 'never'}")
        elif args.command == "records":
            rows = repo.list_records(device_id=args.device_id)
            counts = Counter(r.record_type for r in rows)
            print(f"Database: {where}")
            print(f"Total records: {len(rows)}  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            latest = sorted(rows, key=lambda r: r.last_received_at)[-args.limit:]
            for r in latest:
                data = r.payload["data"]
                what = data.get("app_name") or data.get("status") or ""
                print(f"  {r.last_received_at}  {r.record_type:<16} v{r.record_version:<4} {r.device_id}  {what}")
            if not rows:
                print("  (nothing received yet)")
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
