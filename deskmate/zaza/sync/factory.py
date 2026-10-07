"""Builds a :class:`SyncWorker` from agent config + stored credentials, or
explains why sync is not available. A missing/invalid setup never stops the
agent — it just keeps recording locally."""

from __future__ import annotations

from collections.abc import Callable
from datetime import tzinfo

from ..config import AgentConfig
from ..storage import ActivityStore
from .backoff import Backoff
from .credentials import CredentialsError, load_credentials
from .transport import SyncTransport
from .worker import SyncHealth, SyncWorker


def build_sync_worker(
    config: AgentConfig,
    *,
    tz: tzinfo | None = None,
    on_state_change: Callable[[SyncHealth], None] | None = None,
) -> tuple[SyncWorker | None, str | None]:
    if not config.sync_url:
        return None, "sync not configured (set ZAZA_SYNC_URL)"
    try:
        creds = load_credentials()
    except CredentialsError as exc:
        return None, f"device credentials unusable: {exc}"
    if creds is None:
        return None, "no device credentials (run: python -m deskmate.zaza set-credentials)"
    if creds.device_id != config.device_id:
        return None, (
            f"credentials are for device {creds.device_id!r} but ZAZA_DEVICE_ID is {config.device_id!r}"
        )
    try:
        transport = SyncTransport(
            config.sync_url, creds,
            timeout_seconds=config.sync_timeout_seconds,
            allow_insecure=config.sync_allow_insecure_http,
        )
    except ValueError as exc:
        return None, f"invalid sync URL: {exc}"
    worker = SyncWorker(
        ActivityStore(config.db_path),  # own connection: never contends for the sampler's lock
        transport,
        device_id=config.device_id,
        employee_id=config.employee_id,
        batch_size=config.sync_batch_size,
        max_batches_per_cycle=config.sync_max_batches_per_cycle,
        interval_seconds=config.sync_interval_seconds,
        auth_retry_seconds=config.sync_auth_retry_seconds,
        backoff=Backoff(base_seconds=config.sync_backoff_base_seconds, max_seconds=config.sync_backoff_max_seconds),
        tz=tz,
        on_state_change=on_state_change,
    )
    return worker, None
