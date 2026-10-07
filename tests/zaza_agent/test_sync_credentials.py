"""Device credential storage and sync-worker setup."""

from __future__ import annotations

import json
import os

import pytest

from deskmate.zaza.config import AgentConfig, from_env
from deskmate.zaza.sync.credentials import (
    CredentialsError,
    DeviceCredentials,
    delete_credentials,
    load_credentials,
    save_credentials,
)
from deskmate.zaza.sync.factory import build_sync_worker

SECRET = "zzd_credential-test-secret"


@pytest.fixture
def zaza_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAZA_HOME", str(tmp_path))
    return tmp_path


def test_roundtrip_plain_protection(tmp_path):
    path = tmp_path / "c.json"
    save_credentials(DeviceCredentials("device-1", SECRET), path, protection="none")
    assert load_credentials(path) == DeviceCredentials("device-1", SECRET)


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_dpapi_protection_keeps_token_out_of_the_file(tmp_path):
    path = tmp_path / "c.json"
    save_credentials(DeviceCredentials("device-1", SECRET), path)
    raw = json.loads(path.read_text())
    assert raw["protection"] == "dpapi"
    assert SECRET not in path.read_text()
    assert load_credentials(path).token == SECRET


def test_credentials_live_outside_the_activity_database(zaza_home):
    from deskmate.zaza import paths
    from deskmate.zaza.storage import ActivityStore

    store = ActivityStore()
    save_credentials(DeviceCredentials("device-1", SECRET), protection="none")
    assert (zaza_home / "device_credentials.json").exists()
    assert paths.db_path() != zaza_home / "device_credentials.json"
    dump = "\n".join(store._conn.iterdump())
    assert SECRET not in dump
    store.close()


def test_missing_file_is_none_and_corrupt_file_is_an_error(tmp_path):
    assert load_credentials(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(CredentialsError) as info:
        load_credentials(bad)
    assert SECRET not in str(info.value)


def test_delete_credentials(tmp_path):
    path = tmp_path / "c.json"
    save_credentials(DeviceCredentials("device-1", SECRET), path, protection="none")
    assert delete_credentials(path) and not path.exists()


def test_repr_never_shows_token():
    assert SECRET not in repr(DeviceCredentials("device-1", SECRET))
    assert SECRET not in str(DeviceCredentials("device-1", SECRET))


# ─── factory: why sync is (not) running ────────────────────────────────────


def test_no_url_means_sync_disabled(zaza_home):
    worker, reason = build_sync_worker(AgentConfig(db_path=str(zaza_home / "a.db")))
    assert worker is None and "ZAZA_SYNC_URL" in reason


def test_no_credentials_means_sync_disabled(zaza_home):
    config = AgentConfig(db_path=str(zaza_home / "a.db"), sync_url="https://sync.example.com")
    worker, reason = build_sync_worker(config)
    assert worker is None and "set-credentials" in reason


def test_device_mismatch_refuses_to_sync(zaza_home):
    save_credentials(DeviceCredentials("device-9", SECRET), protection="none")
    config = AgentConfig(device_id="device-1", db_path=str(zaza_home / "a.db"), sync_url="https://sync.example.com")
    worker, reason = build_sync_worker(config)
    assert worker is None and "device-9" in reason
    assert SECRET not in reason


def test_insecure_url_refused_unless_explicitly_allowed(zaza_home):
    save_credentials(DeviceCredentials("device-1", SECRET), protection="none")
    base = {"device_id": "device-1", "db_path": str(zaza_home / "a.db"), "sync_url": "http://10.0.0.5:8765"}
    worker, reason = build_sync_worker(AgentConfig(**base))
    assert worker is None and "HTTPS" in reason
    worker, reason = build_sync_worker(AgentConfig(**base, sync_allow_insecure_http=True))
    assert worker is not None
    worker.store.close()
    worker.transport.close()


def test_configured_worker_uses_its_own_database_connection(zaza_home):
    save_credentials(DeviceCredentials("device-1", SECRET), protection="none")
    config = AgentConfig(device_id="device-1", db_path=str(zaza_home / "a.db"), sync_url="https://sync.example.com",
                         sync_batch_size=50)
    worker, reason = build_sync_worker(config)
    assert reason is None and worker.batch_size == 50
    assert worker.store.path == zaza_home / "a.db"
    worker.store.close()
    worker.transport.close()


def test_sync_settings_from_env(monkeypatch):
    monkeypatch.setenv("ZAZA_SYNC_URL", "https://sync.example.com")
    monkeypatch.setenv("ZAZA_SYNC_BATCH_SIZE", "150")
    monkeypatch.setenv("ZAZA_SYNC_INTERVAL_SECONDS", "30")
    config = from_env()
    assert (config.sync_url, config.sync_batch_size, config.sync_interval_seconds) == (
        "https://sync.example.com", 150, 30,
    )
    with pytest.raises(ValueError):
        AgentConfig(sync_batch_size=1000)
