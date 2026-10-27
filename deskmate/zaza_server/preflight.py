"""``check-config``: validate the server configuration without printing secrets.

Every check reads the same environment the server would use and reports
OK / WARN / FAIL with a safe description (host names, paths, user names —
never a password, key or token). ``--production`` adds the Phase 10
production rules: PostgreSQL backend, password from a file, small pool,
Secure cookies, a public host name, loopback-only trusted proxies and a
bounded log file. No database connection is made (``db-status`` does that).

``--cloud-run`` (Phase 10D) is a SEPARATE mode for Cloud Run + Cloud SQL:
the Cloud SQL socket instead of a host, password files from Secret Manager,
a control database, bounded tenant pools and a connection budget across
all instances, logs on stdout, no JSON keys. ``--connect`` additionally
checks the control schema revision (read-only).
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


# ─── Cloud Run (Phase 10D) ─────────────────────────────────────────────────
# A separate mode: `check-config --production` keeps describing the Windows
# server unchanged. `check-config --cloud-run` validates the Cloud Run +
# Cloud SQL configuration (see docs/zaza/CLOUD_SQL_PRODUCTION.md).

CLOUD_RUN_MAX_INSTANCES_LIMIT = 100
_ADMIN_USERS = ("postgres", "cloudsqlsuperuser", "cloudsqladmin")


def _check_cloud_sql_database(add, env: dict, prefix: str, label: str):  # noqa: ANN001, ANN202
    """Shared rules for the activity/tenant base settings (``ZAZA_DB_*``) and
    the control database (``ZAZA_CONTROL_*``). Returns the settings or None."""
    from .cloudsql import CloudSqlConfigError, instance_connection_name, is_cloudsql_socket  # noqa: PLC0415

    instance = (env.get(f"{prefix}CLOUDSQL_INSTANCE_CONNECTION_NAME") or "").strip()
    if not instance:
        add(FAIL, f"{label} Cloud SQL", f"{prefix}CLOUDSQL_INSTANCE_CONNECTION_NAME is not set "
            "(Cloud Run reaches Cloud SQL only through the service's Unix socket)")
    else:
        try:
            add(OK, f"{label} Cloud SQL", f"instance {instance_connection_name(instance)}")
        except CloudSqlConfigError as exc:
            add(FAIL, f"{label} Cloud SQL", str(exc))
    for name in (f"{prefix}DATABASE_URL", f"{prefix}DB_HOST"):
        if env.get(name):
            add(FAIL, f"{label} address", f"{name} is set; on Cloud Run the Cloud SQL socket is the only address")
    if env.get(f"{prefix}DB_PASSWORD"):
        add(FAIL, f"{label} password source",
            f"{prefix}DB_PASSWORD is set in the environment; use {prefix}DB_PASSWORD_FILE (Secret Manager file)")
    pw_file = env.get(f"{prefix}DB_PASSWORD_FILE")
    if not pw_file:
        add(FAIL, f"{label} password file", f"{prefix}DB_PASSWORD_FILE is not set")
    else:
        problem = _readable_secret_file(pw_file)
        add(FAIL if problem else OK, f"{label} password file",
            f"{pw_file}: {problem or 'present (contents not shown)'}")
    user = env.get(f"{prefix}DB_USER")
    if not user:
        add(FAIL, f"{label} user", f"{prefix}DB_USER is not set")
    elif user in _ADMIN_USERS:
        add(FAIL, f"{label} user", "the web service must not connect as a Cloud SQL administrator")
    try:
        # the password file is reported above; describe the rest without reading it
        without_pw = {k: v for k, v in env.items() if k != f"{prefix}DB_PASSWORD_FILE"}
        if prefix == "ZAZA_":
            settings = database_settings_from_env(without_pw)
        else:
            from .tenancy.config import control_database_settings_from_env  # noqa: PLC0415

            settings = control_database_settings_from_env(without_pw)
    except ConfigError as exc:
        add(FAIL, label, str(exc))
        return None
    add(OK if is_cloudsql_socket(settings.host) else FAIL, label, settings.display())
    return settings


def check_cloud_run_config(env: dict | None = None, *, connect: bool = False) -> list[Check]:  # noqa: C901
    """Cloud Run production rules. ``connect``: also open the control
    database and verify its schema revision (read-only)."""
    env = dict(os.environ if env is None else env)
    checks: list[Check] = []

    def add(status: str, name: str, detail: str) -> None:
        checks.append(Check(status, name, detail))

    try:
        backend = backend_from_env(env)
        add(OK if backend == "postgres" else FAIL, "backend",
            backend + ("" if backend == "postgres" else " (Cloud Run needs postgres; SQLite would lose data)"))
    except ConfigError as exc:
        add(FAIL, "backend", str(exc))
    if env.get("ZAZA_CONTAINER_ALLOW_SQLITE"):
        add(FAIL, "sqlite opt-in", "ZAZA_CONTAINER_ALLOW_SQLITE is for local container tests only")

    port = (env.get("PORT") or "").strip()
    if port and not (port.isdigit() and 1 <= int(port) <= 65535):
        add(FAIL, "PORT", "PORT must be a number 1..65535 (Cloud Run sets it)")
    else:
        add(OK, "PORT", f"{port or '8080 (default)'} on 0.0.0.0 (set by Cloud Run)")

    db = _check_cloud_sql_database(add, env, "ZAZA_", "database")
    control = _check_cloud_sql_database(add, env, "ZAZA_CONTROL_", "control database")

    from .tenancy.connections import connection_budget, tenant_pool_limits_from_env  # noqa: PLC0415

    try:
        limits = tenant_pool_limits_from_env(env)
        add(OK, "tenant pools", f"up to {limits.cache_max} pool(s) per instance x max {limits.pool_max} "
            f"connection(s), min 0, closed after {limits.idle_seconds} s idle")
    except ConfigError as exc:
        limits = None
        add(FAIL, "tenant pools", str(exc))
    if db is not None:
        too_big = db.pool_max > PRODUCTION_POOL_MAX
        add(FAIL if too_big else OK, "database pool",
            f"pool {db.pool_min}..{db.pool_max}" + (f" (Cloud Run: max <= {PRODUCTION_POOL_MAX})" if too_big else ""))

    def _whole(name: str, upper: int | None = None) -> int | None:
        raw = (env.get(name) or "").strip()
        if not raw.isdigit() or int(raw) < 1 or (upper is not None and int(raw) > upper):
            add(FAIL, name.removeprefix("ZAZA_").lower().replace("_", " "),
                f"{name} must be a whole number 1..{upper}" if upper else f"{name} must be a positive whole number")
            return None
        return int(raw)

    instances = _whole("ZAZA_CLOUD_RUN_MAX_INSTANCES", CLOUD_RUN_MAX_INSTANCES_LIMIT)
    capacity = _whole("ZAZA_DB_CONNECTION_BUDGET")
    if limits is not None and db is not None and instances and capacity:
        budget = connection_budget(max_instances=instances, limits=limits, capacity=capacity,
                                   other_per_instance=db.pool_max)
        add(OK if budget.ok else FAIL, "connection budget",
            budget.describe() + ("" if budget.ok else " — lower instances or pool sizes"))

    if env.get("ZAZA_LOG_FILE"):
        add(WARN, "log file", "ZAZA_LOG_FILE is set, but a Cloud Run disk is temporary; logs belong on "
            "stdout/stderr (Cloud Logging)")
    else:
        add(OK, "log file", "stdout/stderr only (Cloud Logging)")
    if env.get("ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE"):
        add(FAIL, "google credentials", "ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE is set; Cloud Run uses the service's "
            "runtime identity, never a downloaded JSON key")

    try:
        from .dashboard.config import dashboard_settings_from_env  # noqa: PLC0415

        dash = dashboard_settings_from_env(env)
        add(OK if dash.cookie_secure else FAIL, "dashboard cookies",
            "Secure=on" if dash.cookie_secure else "Cloud Run serves HTTPS: set ZAZA_DASHBOARD_COOKIE_SECURE=true")
    except ConfigError as exc:
        add(FAIL, "dashboard", str(exc))
    try:
        from .http_security import http_settings_from_env  # noqa: PLC0415

        http = http_settings_from_env(env)
        add(OK if http.public_hosts else WARN, "public host",
            ", ".join(http.public_hosts) if http.public_hosts else
            "ZAZA_PUBLIC_HOST is not set (set the service / custom domain host before going live)")
        add(WARN, "front-end proxy", "Cloud Run front-end proxy support is a later phase: forwarded headers "
            "are ignored (fail-closed) and the dashboard is not mounted on 0.0.0.0")
    except ConfigError as exc:
        add(FAIL, "http", str(exc))

    if connect:
        if any(c.status == FAIL for c in checks):
            add(FAIL, "control schema", "not checked: fix the problems above first")
        else:
            add(*_control_schema_check(env))
    return checks


def _control_schema_check(env: dict) -> tuple[str, str, str]:
    import psycopg  # noqa: PLC0415

    from .tenancy.config import control_database_settings_from_env  # noqa: PLC0415
    from .tenancy.control import migrate as control_migrate  # noqa: PLC0415

    settings = control_database_settings_from_env(env)
    try:
        with psycopg.connect(**settings.connect_kwargs()) as conn:
            status = control_migrate.schema_status(conn)
    except psycopg.Error as exc:
        return FAIL, "control schema", f"cannot connect: {settings.scrub(str(exc)).strip()[:200]}"
    return (OK if status.up_to_date else FAIL, "control schema",
            f"{status.current or '(empty)'}; latest {status.head}"
            + ("" if status.up_to_date else " (run the control-migrate job)"))
