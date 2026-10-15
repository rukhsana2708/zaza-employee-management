"""``check-config``: validate the server configuration without printing secrets.

Every check reads the same environment the server would use and reports
OK / WARN / FAIL with a safe description (host names, paths, user names —
never a password, key or token). ``--production`` adds the Phase 10
production rules: PostgreSQL backend, password from a file, small pool,
Secure cookies, a public host name, loopback-only trusted proxies and a
bounded log file. No database connection is made (``db-status`` does that).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, backend_from_env, database_settings_from_env

OK, WARN, FAIL = "OK", "WARN", "FAIL"
PRODUCTION_POOL_MAX = 4


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str


def _readable_secret_file(path: str) -> str | None:
    """Problem with a secret file, or ``None``. Reads only its size."""
    p = Path(path)
    if not p.is_file():
        return "file not found"
    try:
        with p.open("rb") as handle:
            if not handle.read(1):
                return "file is empty"
    except OSError as exc:
        return f"file can't be read ({exc.strerror})"
    return None


def check_config(env: dict | None = None, *, production: bool = False) -> list[Check]:  # noqa: C901
    env = dict(os.environ if env is None else env)
    checks: list[Check] = []

    def add(status: str, name: str, detail: str) -> None:
        checks.append(Check(status, name, detail))

    try:
        backend = backend_from_env(env)
        add(FAIL if production and backend != "postgres" else OK, "backend",
            backend + (" (production needs postgres)" if production and backend != "postgres" else ""))
    except ConfigError as exc:
        add(FAIL, "backend", str(exc))
        backend = None

    if backend == "postgres":
        try:
            pw_file = env.get("ZAZA_DB_PASSWORD_FILE")
            problem = _readable_secret_file(pw_file) if pw_file else None
            # a missing file is reported on its own line; describe the rest without it
            db = database_settings_from_env({k: v for k, v in env.items()
                                             if not (problem and k == "ZAZA_DB_PASSWORD_FILE")})
            add(OK, "database", db.display())
            if not db.user:
                add(FAIL, "database user", "ZAZA_DB_USER is not set")
            if pw_file:
                add(FAIL if problem else OK, "database password file",
                    f"{pw_file}: {problem or 'present (contents not shown)'}")
            elif production:
                add(FAIL, "database password file", "ZAZA_DB_PASSWORD_FILE is not set")
            if production and (env.get("ZAZA_DB_PASSWORD") or (db.password and not env.get("ZAZA_DB_PASSWORD_FILE"))):
                add(FAIL, "database password source",
                    "a password is set in the environment or URL; production reads it from ZAZA_DB_PASSWORD_FILE only")
            pool = f"pool {db.pool_min}..{db.pool_max}"
            add(WARN if production and db.pool_max > PRODUCTION_POOL_MAX else OK, "database pool",
                pool + (f" (production on this server: max <= {PRODUCTION_POOL_MAX})"
                        if production and db.pool_max > PRODUCTION_POOL_MAX else ""))
        except ConfigError as exc:
            add(FAIL, "database", str(exc))

    try:
        from .dashboard.config import dashboard_settings_from_env  # noqa: PLC0415

        dash = dashboard_settings_from_env(env)
        add(FAIL if production and not dash.cookie_secure else OK, "dashboard cookies",
            f"Secure={'on' if dash.cookie_secure else 'off'}, session {dash.session_hours} h"
            + (" (production needs ZAZA_DASHBOARD_COOKIE_SECURE=true)" if production and not dash.cookie_secure else ""))
        add(OK, "login rate limit",
            f"{dash.login_max_failures_per_client} failures per client / {dash.login_max_failures_per_username} per "
            f"username in {dash.login_window_seconds} s")
    except ConfigError as exc:
        add(FAIL, "dashboard", str(exc))

    try:
        from .http_security import http_settings_from_env  # noqa: PLC0415

        http = http_settings_from_env(env)
        if http.public_hosts:
            add(OK, "public host", ", ".join(http.public_hosts))
        else:
            add(FAIL if production else OK, "public host",
                "ZAZA_PUBLIC_HOST is not set" + (" (required before activation)" if production
                                                  else " (no Host check: local development)"))
        add(OK, "trusted proxies", ", ".join(http.trusted_proxies))
    except ConfigError as exc:
        add(FAIL, "http", str(exc))

    try:
        from .logs import log_settings_from_env  # noqa: PLC0415

        logs = log_settings_from_env(env)
        if logs.file:
            add(OK, "log file", f"{logs.file} (rotates at {logs.max_bytes // (1024 * 1024)} MiB, keeps "
                f"{logs.backup_count}; at most {logs.max_disk_bytes // (1024 * 1024)} MiB)")
        else:
            add(FAIL if production else OK, "log file", "ZAZA_LOG_FILE is not set (stderr only)")
    except ConfigError as exc:
        add(FAIL, "logging", str(exc))

    if env.get("ZAZA_GOOGLE_SHEET_ID") or env.get("ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE"):
        try:
            from .sheets.config import sheets_settings_from_env  # noqa: PLC0415

            sheets = sheets_settings_from_env(env)
            problem = _readable_secret_file(str(sheets.service_account_file))
            add(FAIL if problem else OK, "google sheets",
                f"spreadsheet {sheets.masked_id}, key {sheets.service_account_file}: "
                f"{problem or 'present (contents not shown)'}")
        except ConfigError as exc:
            add(FAIL, "google sheets", str(exc))
    else:
        add(OK, "google sheets", "not configured")
    return checks


def print_checks(checks: list[Check]) -> int:
    for c in checks:
        print(f"[{c.status:<4}] {c.name:<26} {c.detail}")
    failed = sum(c.status == FAIL for c in checks)
    print(f"\n{failed} problem(s)." if failed else "\nConfiguration OK.")
    return 2 if failed else 0
