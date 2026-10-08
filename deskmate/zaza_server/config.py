"""Server configuration: which repository backend to use, and the
PostgreSQL connection settings.

All credentials come from the environment (or a password file), never from
source code. The password is excluded from ``repr()`` and every message
this module produces; :func:`DatabaseSettings.display` is the only form
that may be logged.

Environment variables
---------------------

``ZAZA_SERVER_BACKEND``     ``sqlite`` (development, default) or ``postgres``.

Either one URL:

``ZAZA_DATABASE_URL``       ``postgresql://user:password@host:5432/dbname``

or separate parts (each overrides the matching URL part if both are set):

``ZAZA_DB_HOST``            default ``127.0.0.1``
``ZAZA_DB_PORT``            default ``5432``
``ZAZA_DB_NAME``            default ``zaza``
``ZAZA_DB_USER``
``ZAZA_DB_PASSWORD``
``ZAZA_DB_PASSWORD_FILE``   read the password from this file instead
``ZAZA_DB_SSLMODE``         default ``prefer``
``ZAZA_DB_SCHEMA``          optional; sets ``search_path`` (default: public)

Connection pool (kept deliberately small — the VPS is resource-constrained):

``ZAZA_DB_POOL_MIN``        default 1
``ZAZA_DB_POOL_MAX``        default 4 (hard upper bound 20)
``ZAZA_DB_POOL_TIMEOUT``    seconds to wait for a free connection, default 10
``ZAZA_DB_CONNECT_TIMEOUT`` seconds, default 5
``ZAZA_DB_STATEMENT_TIMEOUT_MS`` per statement, default 15000
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

BACKENDS = ("sqlite", "postgres")
POOL_MAX_LIMIT = 20
_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class ConfigError(ValueError):
    """Invalid server configuration. Messages never contain the password."""


@dataclass(frozen=True)
class DatabaseSettings:
    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "zaza"
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    sslmode: str = "prefer"
    schema: str | None = None
    pool_min: int = 1
    pool_max: int = 4
    pool_timeout: float = 10.0
    connect_timeout: int = 5
    statement_timeout_ms: int = 15000
    application_name: str = "zaza-sync"

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError("database port must be 1..65535")
        if self.sslmode not in _SSL_MODES:
            raise ConfigError(f"ZAZA_DB_SSLMODE must be one of {', '.join(_SSL_MODES)}")
        if self.schema is not None and not _SCHEMA_RE.match(self.schema):
            raise ConfigError("ZAZA_DB_SCHEMA must be a plain lower-case identifier")
        if not 0 <= self.pool_min <= self.pool_max <= POOL_MAX_LIMIT or self.pool_max < 1:
            raise ConfigError(f"pool sizes must satisfy 0 <= min <= max <= {POOL_MAX_LIMIT}, max >= 1")
        if self.pool_timeout <= 0 or self.connect_timeout <= 0 or self.statement_timeout_ms <= 0:
            raise ConfigError("timeouts must be positive")

    # ─── connection forms ──────────────────────────────────────────────────
    def pg_options(self) -> str:
        opts = f"-c TimeZone=UTC -c statement_timeout={self.statement_timeout_ms}"
        if self.schema:
            opts += f" -c search_path={self.schema}"
        return opts

    def connect_kwargs(self) -> dict:
        """Keyword arguments for ``psycopg.connect`` (contains the password —
        pass straight to the driver, never log)."""
        kwargs = {
            "host": self.host, "port": self.port, "dbname": self.dbname, "sslmode": self.sslmode,
            "connect_timeout": self.connect_timeout, "application_name": self.application_name,
            "options": self.pg_options(),
        }
        if self.user:
            kwargs["user"] = self.user
        if self.password:
            kwargs["password"] = self.password
        return kwargs

    def conninfo(self) -> str:
        from psycopg.conninfo import make_conninfo  # noqa: PLC0415

        return make_conninfo(**self.connect_kwargs())

    def sqlalchemy_url(self):  # noqa: ANN201 — sqlalchemy.engine.URL
        """URL object for Alembic/SQLAlchemy. Built from parts (never parsed
        from a string), so special characters in the password are safe."""
        from sqlalchemy.engine import URL  # noqa: PLC0415

        return URL.create(
            "postgresql+psycopg", username=self.user, password=self.password,
            host=self.host, port=self.port, database=self.dbname,
            query={"sslmode": self.sslmode, "application_name": f"{self.application_name}-migrate"},
        )

    def display(self) -> str:
        """Safe, loggable description: no password, ever."""
        user = f"{self.user}:***@" if self.password else (f"{self.user}@" if self.user else "")
        schema = f" schema={self.schema}" if self.schema else ""
        return f"postgresql://{user}{self.host}:{self.port}/{self.dbname} (sslmode={self.sslmode}{schema})"

    def scrub(self, text: str) -> str:
        """Remove the password (and any ``password=...`` fragment) from text
        that might be shown or logged, e.g. a driver error message."""
        if self.password:
            text = text.replace(self.password, "***")
        return re.sub(r"(password\s*=\s*)(\S+)", r"\1***", text, flags=re.IGNORECASE)


def _int_env(env: dict, name: str, default: int) -> int:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def database_settings_from_env(env: dict | None = None) -> DatabaseSettings:
    env = dict(os.environ if env is None else env)
    parts: dict = {}
    if url := env.get("ZAZA_DATABASE_URL"):
        from psycopg.conninfo import conninfo_to_dict  # noqa: PLC0415

        try:
            parsed = conninfo_to_dict(url)
        except Exception:  # noqa: BLE001 — the driver's message may echo the URL
            raise ConfigError("ZAZA_DATABASE_URL is not a valid PostgreSQL URL") from None
        parts = {
            "host": parsed.get("host"), "port": int(parsed["port"]) if parsed.get("port") else None,
            "dbname": parsed.get("dbname"), "user": parsed.get("user"), "password": parsed.get("password"),
            "sslmode": parsed.get("sslmode"),
        }
    overrides = {
        "host": env.get("ZAZA_DB_HOST"), "dbname": env.get("ZAZA_DB_NAME"), "user": env.get("ZAZA_DB_USER"),
        "password": env.get("ZAZA_DB_PASSWORD"), "sslmode": env.get("ZAZA_DB_SSLMODE"),
        "schema": env.get("ZAZA_DB_SCHEMA"),
    }
    if env.get("ZAZA_DB_PORT"):
        overrides["port"] = _int_env(env, "ZAZA_DB_PORT", 5432)
    if pw_file := env.get("ZAZA_DB_PASSWORD_FILE"):
        try:
            overrides["password"] = Path(pw_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read ZAZA_DB_PASSWORD_FILE ({exc.strerror})") from None
    merged = {k: v for k, v in {**parts, **{k: v for k, v in overrides.items() if v}}.items() if v is not None}
    return DatabaseSettings(
        **merged,
        pool_min=_int_env(env, "ZAZA_DB_POOL_MIN", 1),
        pool_max=_int_env(env, "ZAZA_DB_POOL_MAX", 4),
        pool_timeout=float(_int_env(env, "ZAZA_DB_POOL_TIMEOUT", 10)),
        connect_timeout=_int_env(env, "ZAZA_DB_CONNECT_TIMEOUT", 5),
        statement_timeout_ms=_int_env(env, "ZAZA_DB_STATEMENT_TIMEOUT_MS", 15000),
    )


def backend_from_env(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    backend = (env.get("ZAZA_SERVER_BACKEND") or "sqlite").strip().lower()
    if backend not in BACKENDS:
        raise ConfigError(f"ZAZA_SERVER_BACKEND must be one of {', '.join(BACKENDS)}")
    return backend
