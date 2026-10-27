"""Control-plane database settings (Phase 10C).

The control database is configured separately from the (single-organization
today, per-tenant tomorrow) activity database, with ``ZAZA_CONTROL_`` names
that mirror the existing ``ZAZA_DB_`` ones:

``ZAZA_CONTROL_DATABASE_URL`` or ``ZAZA_CONTROL_DB_HOST`` / ``_PORT`` /
``_NAME`` / ``_USER`` / ``_PASSWORD_FILE`` / ``_SSLMODE`` / ``_SCHEMA``.

A database name is required explicitly (no default), and it may not be a
tenant database name or the activity database configured in ``ZAZA_DB_NAME``.
"""

from __future__ import annotations

import os

from ..config import ConfigError, DatabaseSettings, database_settings_from_env
from . import domain

_PREFIX = "ZAZA_CONTROL_"
_KEYS = ("DATABASE_URL", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PASSWORD_FILE",
         "DB_SSLMODE", "DB_SCHEMA", "DB_CONNECT_TIMEOUT", "DB_STATEMENT_TIMEOUT_MS")


def control_database_settings_from_env(env: dict | None = None) -> DatabaseSettings:
    env = dict(os.environ if env is None else env)
    mapped = {f"ZAZA_{key}": env[f"{_PREFIX}{key}"] for key in _KEYS if env.get(f"{_PREFIX}{key}")}
    if not (mapped.get("ZAZA_DATABASE_URL") or mapped.get("ZAZA_DB_NAME")):
        raise ConfigError("the control-plane database is not configured "
                          "(set ZAZA_CONTROL_DB_NAME or ZAZA_CONTROL_DATABASE_URL)")
    settings = database_settings_from_env({**mapped, "ZAZA_DB_POOL_MIN": "0", "ZAZA_DB_POOL_MAX": "2"})
    if domain.TENANT_DB_RE.match(settings.dbname):
        raise ConfigError("the control-plane database cannot be a tenant database")
    if settings.dbname == (env.get("ZAZA_DB_NAME") or "zaza"):
        raise ConfigError("the control-plane database must not be the activity database (ZAZA_DB_NAME)")
    return settings
