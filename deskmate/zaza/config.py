"""Agent configuration.

Who/what is being tracked, the idle threshold, aggregation tuning, local
retention, privacy exclusions, and (Phase 3) where/how to sync. The device
token is NOT configuration: it lives in the DPAPI-protected credentials
file (see ``sync/credentials.py``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import paths

DEFAULT_IDLE_THRESHOLD_SECONDS = 300  # 5 minutes, per spec
DEFAULT_TICK_SECONDS = 10  # how often the sampler writes an ACTIVITY row
DEFAULT_TITLE_DEBOUNCE_SECONDS = 30  # a new window title must persist this long to split a period
DEFAULT_RAW_RETENTION_DAYS = 7  # raw high-frequency events
DEFAULT_SYNCED_RETENTION_DAYS = 30  # local safety window for records already confirmed synced
DEFAULT_RETENTION_INTERVAL_SECONDS = 3600  # how often cleanup runs while the agent is up
DEFAULT_SYNC_BATCH_SIZE = 100
DEFAULT_SYNC_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class AgentConfig:
    employee_id: str = "unassigned"
    device_id: str = "unassigned-device"
    idle_threshold_seconds: int = DEFAULT_IDLE_THRESHOLD_SECONDS
    tick_seconds: int = DEFAULT_TICK_SECONDS
    db_path: str = field(default_factory=lambda: str(paths.db_path()))

    title_debounce_seconds: int = DEFAULT_TITLE_DEBOUNCE_SECONDS
    # A wall-clock jump between ticks longer than this (sleep/hibernate, a
    # suspended process) is recorded as an UNKNOWN telemetry gap. None means
    # max(3 * tick_seconds, 30).
    gap_threshold_seconds: int | None = None

    raw_retention_days: int = DEFAULT_RAW_RETENTION_DAYS
    synced_retention_days: int = DEFAULT_SYNCED_RETENTION_DAYS
    retention_interval_seconds: int = DEFAULT_RETENTION_INTERVAL_SECONDS

    # Privacy exclusions — matched case-insensitively. See privacy.py.
    excluded_apps: tuple[str, ...] = ()  # window title never read or stored
    hidden_app_names: tuple[str, ...] = ()  # also replace the app name itself
    excluded_domains: tuple[str, ...] = ()  # domain (and subdomains) never stored

    # Sync (Phase 3). No URL = sync disabled; the agent works fully offline.
    sync_url: str | None = None
    sync_batch_size: int = DEFAULT_SYNC_BATCH_SIZE
    sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS
    sync_max_batches_per_cycle: int = 20
    sync_timeout_seconds: float = 15.0
    sync_backoff_base_seconds: float = 5.0
    sync_backoff_max_seconds: float = 300.0
    sync_auth_retry_seconds: float = 900.0
    sync_allow_insecure_http: bool = False  # plain HTTP beyond localhost (never in production)

    def __post_init__(self) -> None:
        for name in ("idle_threshold_seconds", "tick_seconds", "raw_retention_days", "synced_retention_days",
                     "retention_interval_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.title_debounce_seconds < 0:
            raise ValueError("title_debounce_seconds must be >= 0")
        if self.gap_threshold_seconds is not None and self.gap_threshold_seconds <= self.tick_seconds:
            raise ValueError("gap_threshold_seconds must be greater than tick_seconds")
        if not 1 <= self.sync_batch_size <= 500:
            raise ValueError("sync_batch_size must be 1..500")
        for name in ("sync_interval_seconds", "sync_max_batches_per_cycle", "sync_timeout_seconds",
                     "sync_backoff_base_seconds", "sync_auth_retry_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.sync_backoff_max_seconds < self.sync_backoff_base_seconds:
            raise ValueError("sync_backoff_max_seconds must be >= sync_backoff_base_seconds")

    @property
    def effective_gap_threshold_seconds(self) -> int:
        if self.gap_threshold_seconds is not None:
            return self.gap_threshold_seconds
        return max(3 * self.tick_seconds, 30)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def from_env() -> AgentConfig:
    """Build config from environment variables, falling back to defaults.

    ``ZAZA_EMPLOYEE_ID``, ``ZAZA_DEVICE_ID``, ``ZAZA_IDLE_THRESHOLD_SECONDS``,
    ``ZAZA_TICK_SECONDS``, ``ZAZA_TITLE_DEBOUNCE_SECONDS``,
    ``ZAZA_RAW_RETENTION_DAYS``, ``ZAZA_SYNCED_RETENTION_DAYS``, and the
    comma-separated ``ZAZA_EXCLUDED_APPS``, ``ZAZA_HIDDEN_APPS``,
    ``ZAZA_EXCLUDED_DOMAINS``, and for sync ``ZAZA_SYNC_URL``,
    ``ZAZA_SYNC_BATCH_SIZE``, ``ZAZA_SYNC_INTERVAL_SECONDS``,
    ``ZAZA_SYNC_ALLOW_INSECURE_HTTP`` (``1`` to allow plain HTTP beyond
    localhost — development only). All optional.
    """
    kwargs: dict[str, object] = {}
    if v := os.environ.get("ZAZA_EMPLOYEE_ID"):
        kwargs["employee_id"] = v
    if v := os.environ.get("ZAZA_DEVICE_ID"):
        kwargs["device_id"] = v
    for env, key in (
        ("ZAZA_IDLE_THRESHOLD_SECONDS", "idle_threshold_seconds"),
        ("ZAZA_TICK_SECONDS", "tick_seconds"),
        ("ZAZA_TITLE_DEBOUNCE_SECONDS", "title_debounce_seconds"),
        ("ZAZA_RAW_RETENTION_DAYS", "raw_retention_days"),
        ("ZAZA_SYNCED_RETENTION_DAYS", "synced_retention_days"),
        ("ZAZA_SYNC_BATCH_SIZE", "sync_batch_size"),
        ("ZAZA_SYNC_INTERVAL_SECONDS", "sync_interval_seconds"),
    ):
        if v := os.environ.get(env):
            kwargs[key] = int(v)
    for env, key in (
        ("ZAZA_EXCLUDED_APPS", "excluded_apps"),
        ("ZAZA_HIDDEN_APPS", "hidden_app_names"),
        ("ZAZA_EXCLUDED_DOMAINS", "excluded_domains"),
    ):
        if v := os.environ.get(env):
            kwargs[key] = _csv(v)
    if v := os.environ.get("ZAZA_SYNC_URL"):
        kwargs["sync_url"] = v.strip()
    if os.environ.get("ZAZA_SYNC_ALLOW_INSECURE_HTTP") == "1":
        kwargs["sync_allow_insecure_http"] = True
    return AgentConfig(**kwargs)
