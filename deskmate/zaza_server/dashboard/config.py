"""Manager dashboard settings (environment only; no secrets here).

``ZAZA_DASHBOARD_SESSION_HOURS``            session lifetime, 1..168 (default 12)
``ZAZA_DASHBOARD_COOKIE_SECURE``            send the session cookie over HTTPS only
                                            (default false for localhost development;
                                            production MUST set true behind HTTPS)
``ZAZA_DASHBOARD_ONLINE_THRESHOLD_SECONDS`` a device is online if it contacted the
                                            server this recently, 30..86400 (default 300)
``ZAZA_DASHBOARD_ACTIVITY_ROWS``            max activity periods on an employee page,
                                            10..2000 (default 200)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import ConfigError

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


@dataclass(frozen=True)
class DashboardSettings:
    session_hours: int = 12
    cookie_secure: bool = False
    online_threshold_seconds: int = 300
    activity_rows: int = 200

    def __post_init__(self) -> None:
        if not 1 <= self.session_hours <= 168:
            raise ConfigError("ZAZA_DASHBOARD_SESSION_HOURS must be 1..168")
        if not 30 <= self.online_threshold_seconds <= 86400:
            raise ConfigError("ZAZA_DASHBOARD_ONLINE_THRESHOLD_SECONDS must be 30..86400")
        if not 10 <= self.activity_rows <= 2000:
            raise ConfigError("ZAZA_DASHBOARD_ACTIVITY_ROWS must be 10..2000")


def _int(env: dict, name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None


def _bool(env: dict, name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")


def dashboard_settings_from_env(env: dict | None = None) -> DashboardSettings:
    env = dict(os.environ if env is None else env)
    return DashboardSettings(
        session_hours=_int(env, "ZAZA_DASHBOARD_SESSION_HOURS", 12),
        cookie_secure=_bool(env, "ZAZA_DASHBOARD_COOKIE_SECURE", False),
        online_threshold_seconds=_int(env, "ZAZA_DASHBOARD_ONLINE_THRESHOLD_SECONDS", 300),
        activity_rows=_int(env, "ZAZA_DASHBOARD_ACTIVITY_ROWS", 200),
    )
