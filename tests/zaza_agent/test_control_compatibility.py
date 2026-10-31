"""Sequence B: migration-specific runtime compatibility, with strict operator/tenant status."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from deskmate.zaza_server.config import ConfigError, DatabaseSettings
from deskmate.zaza_server.repository import RepositoryUnavailable
from deskmate.zaza_server.tenancy.control import migrate
from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository
from deskmate.zaza_server.tenancy.saas_sync import SaasRuntime

C3 = "c0003_device_enrollment"
C4 = "c0004_sync_rate_limits"
REVISIONS = [C3, C4, "c0001_control_plane", "c0002_saas_auth_routing", None,
             "unknown", "c0005_future", "0004_manager_dashboard_auth"]


@pytest.mark.parametrize("revision", REVISIONS)
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_runtime_startup_matrix(revision, mode, monkeypatch):
    import psycopg_pool

    from deskmate.zaza_server.tenancy import config, connections

    pool = MagicMock()
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", MagicMock(return_value=pool))
    monkeypatch.setattr(migrate, "schema_status", lambda conn: migrate.SchemaStatus(revision, C4))
    monkeypatch.setattr(config, "control_database_settings_from_env", lambda env: DatabaseSettings())
    monkeypatch.setattr(connections, "TenantRepositoryManager", MagicMock())
    allowed = revision == C4 or (revision == C3 and mode == "off")
    if allowed:
        runtime = SaasRuntime.from_env({"ZAZA_SYNC_LIMIT_MODE": mode})
        try:
            assert runtime.limiter.settings.mode == mode
        finally:
            runtime.close()
    else:
        with pytest.raises(RepositoryUnavailable, match="incompatible with ZAZA_SYNC_LIMIT_MODE"):
            SaasRuntime.from_env({"ZAZA_SYNC_LIMIT_MODE": mode})
        pool.close.assert_called_once()
    assert migrate.SchemaStatus(revision, C4).up_to_date == (revision == C4)


def test_operator_repository_remains_strict(monkeypatch):
    import psycopg_pool

    pool = MagicMock()
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", MagicMock(return_value=pool))
    monkeypatch.setattr(migrate, "schema_status", lambda conn: migrate.SchemaStatus(C3, C4))
    with pytest.raises(RepositoryUnavailable, match="this server needs"):
        PostgresControlRepository(DatabaseSettings())
    pool.close.assert_called_once()


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_tenant_schema_status_is_still_exact(mode):
    from deskmate.zaza_server.postgres.migrate import SchemaStatus

    assert not SchemaStatus(C3, "0004_manager_dashboard_auth").up_to_date
    assert not SchemaStatus("0003_previous", "0004_manager_dashboard_auth").up_to_date
    assert not migrate.runtime_compatible(migrate.SchemaStatus(C3, "c0005_future"), sync_limit_mode=mode)


def test_invalid_limiter_mode_fails_before_opening_database(monkeypatch):
    import psycopg_pool

    factory = MagicMock()
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", factory)
    with pytest.raises(ConfigError, match="ZAZA_SYNC_LIMIT_MODE"):
        SaasRuntime.from_env({"ZAZA_SYNC_LIMIT_MODE": "disabled"})
    factory.assert_not_called()


@pytest.mark.parametrize("revision", [C3, C4, "c0005_future"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_connected_preflight_uses_runtime_compatibility(revision, mode, monkeypatch):
    import psycopg

    from deskmate.zaza_server.preflight import FAIL, OK, _control_schema_check
    from deskmate.zaza_server.tenancy import config

    monkeypatch.setattr(psycopg, "connect", MagicMock())
    monkeypatch.setattr(config, "control_database_settings_from_env", lambda env: DatabaseSettings())
    monkeypatch.setattr(migrate, "schema_status", lambda conn: migrate.SchemaStatus(revision, C4))
    expected = OK if revision == C4 or (revision == C3 and mode == "off") else FAIL
    assert _control_schema_check({"ZAZA_SYNC_LIMIT_MODE": mode})[0] == expected
