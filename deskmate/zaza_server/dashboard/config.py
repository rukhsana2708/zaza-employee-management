"""Manager dashboard settings (environment only; no secrets here).

``ZAZA_DASHBOARD_SESSION_HOURS``            session lifetime, 1..168 (default 12)
``ZAZA_DASHBOARD_COOKIE_SECURE``            send the session cookie over HTTPS only
                                            (default false for localhost development;
                                            production MUST set true behind HTTPS)
``ZAZA_DASHBOARD_ONLINE_THRESHOLD_SECONDS`` a device is online if it contacted the
                                            server this recently, 30..86400 (default 300)
``ZAZA_DASHBOARD_ACTIVITY_ROWS``            max activity periods on an employee page,
                                            10..2000 (default 200)

Phase 8 charts and rule-based analysis (deterministic; no AI):

``ZAZA_DASHBOARD_TOP_APPLICATIONS``         applications in the usage chart, 3..50 (default 10)
``ZAZA_ANALYSIS_HIGH_IDLE_PERCENT``         idle share of tracked time noted as high, 1..100 (default 40)
``ZAZA_ANALYSIS_MAX_INSIGHTS``              insights shown on the Analytics page, 1..20 (default 8)

Phase 10 login rate limiting (failed sign-ins in a sliding window):

``ZAZA_DASHBOARD_LOGIN_MAX_FAILURES_PER_CLIENT``   per client address, 3..100 (default 10)
``ZAZA_DASHBOARD_LOGIN_MAX_FAILURES_PER_USERNAME`` per submitted username, 3..1000 (default 20)
``ZAZA_DASHBOARD_LOGIN_WINDOW_SECONDS``            window length, 60..86400 (default 900)
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
    top_applications: int = 10
    high_idle_percent: int = 40
    max_insights: int = 8
    login_max_failures_per_client: int = 10
    login_max_failures_per_username: int = 20
    login_window_seconds: int = 900

    def __post_init__(self) -> None:
        if not 1 <= self.session_hours <= 168:
            raise ConfigError("ZAZA_DASHBOARD_SESSION_HOURS must be 1..168")
        if not 30 <= self.online_threshold_seconds <= 86400:
            raise ConfigError("ZAZA_DASHBOARD_ONLINE_THRESHOLD_SECONDS must be 30..86400")
        if not 10 <= self.activity_rows <= 2000:
            raise ConfigError("ZAZA_DASHBOARD_ACTIVITY_ROWS must be 10..2000")
        if not 3 <= self.top_applications <= 50:
            raise ConfigError("ZAZA_DASHBOARD_TOP_APPLICATIONS must be 3..50")
        if not 1 <= self.high_idle_percent <= 100:
            raise ConfigError("ZAZA_ANALYSIS_HIGH_IDLE_PERCENT must be 1..100")
        if not 1 <= self.max_insights <= 20:
            raise ConfigError("ZAZA_ANALYSIS_MAX_INSIGHTS must be 1..20")
        if not 3 <= self.login_max_failures_per_client <= 100:
            raise ConfigError("ZAZA_DASHBOARD_LOGIN_MAX_FAILURES_PER_CLIENT must be 3..100")
        if not 3 <= self.login_max_failures_per_username <= 1000:
            raise ConfigError("ZAZA_DASHBOARD_LOGIN_MAX_FAILURES_PER_USERNAME must be 3..1000")
        if not 60 <= self.login_window_seconds <= 86400:
            raise ConfigError("ZAZA_DASHBOARD_LOGIN_WINDOW_SECONDS must be 60..86400")

    def login_limits(self):  # noqa: ANN201 — ratelimit.LoginLimits
        from .ratelimit import LoginLimits  # noqa: PLC0415

        return LoginLimits(self.login_max_failures_per_client, self.login_max_failures_per_username,
                           self.login_window_seconds)


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
        top_applications=_int(env, "ZAZA_DASHBOARD_TOP_APPLICATIONS", 10),
        high_idle_percent=_int(env, "ZAZA_ANALYSIS_HIGH_IDLE_PERCENT", 40),
        max_insights=_int(env, "ZAZA_ANALYSIS_MAX_INSIGHTS", 8),
        login_max_failures_per_client=_int(env, "ZAZA_DASHBOARD_LOGIN_MAX_FAILURES_PER_CLIENT", 10),
        login_max_failures_per_username=_int(env, "ZAZA_DASHBOARD_LOGIN_MAX_FAILURES_PER_USERNAME", 20),
        login_window_seconds=_int(env, "ZAZA_DASHBOARD_LOGIN_WINDOW_SECONDS", 900),
    )
