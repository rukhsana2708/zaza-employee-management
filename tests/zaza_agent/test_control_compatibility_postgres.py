"""Sequence B on disposable PostgreSQL: real startup, transactional DDL, and live rollback."""
from __future__ import annotations

import dataclasses

import pytest
from alembic import command
from fastapi.testclient import TestClient

from deskmate.zaza.sync.protocol import BATCH_PATH, DEVICE_ID_HEADER, DEVICE_PATH, HEALTH_PATH
from deskmate.zaza_server.app import create_saas_app
from deskmate.zaza_server.repository import RepositoryUnavailable
from deskmate.zaza_server.tenancy.control import migrate
from deskmate.zaza_server.tenancy.saas_sync import SaasRuntime

from .test_control_compatibility import C3, C4, HEAD, REVISIONS
from .test_enrollment_postgres import _enroll, _exec, env  # noqa: F401
from .test_postgres_integration import usage
from .test_sync_ratelimit_postgres import control  # noqa: F401
from .test_sync_server import batch


def _runtime(settings, mode, monkeypatch, base=None):
    from deskmate.zaza_server import config
    from deskmate.zaza_server.tenancy import config as control_config

    monkeypatch.setattr(control_config, "control_database_settings_from_env", lambda _: settings)
    monkeypatch.setattr(config, "database_settings_from_env", lambda _: base or settings)
    return SaasRuntime.from_env({"ZAZA_SYNC_LIMIT_MODE": mode})


@pytest.mark.parametrize("revision", REVISIONS)
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_real_startup_matrix(control, revision, mode, monkeypatch):  # noqa: F811
    settings = control.settings
    if revision == C3:
        command.downgrade(migrate.alembic_config(settings), C3)
    elif revision == C4:
        command.downgrade(migrate.alembic_config(settings), C4)
    elif revision != HEAD:
        if revision is None:
            _exec(settings, "DELETE FROM zaza_control_version")
        else:
            _exec(settings, "UPDATE zaza_control_version SET version_num = %s", (revision,))
    allowed = revision in {C4, HEAD} or (revision == C3 and mode == "off")
    if allowed:
        runtime = _runtime(settings, mode, monkeypatch)
        runtime.close()
    else:
        with pytest.raises(RepositoryUnavailable, match="incompatible with ZAZA_SYNC_LIMIT_MODE"):
            _runtime(settings, mode, monkeypatch)


def _snapshot(settings):
    return (
        _exec(settings, "SELECT version_num FROM zaza_control_version"),
        _exec(settings, "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid = 'login_attempt_counters'::regclass ORDER BY conname"),
        _exec(settings, "SELECT * FROM login_attempt_counters ORDER BY bucket, key_hash, window_start"),
    )


def test_failed_c0004_restores_revision_constraint_and_rows(control, monkeypatch):  # noqa: F811
    import psycopg
    from alembic.operations import Operations

    settings = control.settings
    command.downgrade(migrate.alembic_config(settings), C3)
    _exec(settings, "INSERT INTO login_attempt_counters (bucket, key_hash, window_start) "
                    "VALUES ('identity', repeat('a', 64), now())")
    before = _snapshot(settings)
    original = Operations.execute

    def fail_add(self, statement, *args, **kwargs):
        if "ADD CONSTRAINT login_attempt_counters_bucket_valid" in str(statement):
            return original(self, "SELECT sequence_b_injected_failure()")
        return original(self, statement, *args, **kwargs)

    with monkeypatch.context() as injected:
        injected.setattr(Operations, "execute", fail_add)
        with pytest.raises(Exception, match="sequence_b_injected_failure"):
            migrate.upgrade(settings)
    assert _snapshot(settings) == before
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec(settings, "INSERT INTO login_attempt_counters (bucket, key_hash, window_start) "
                        "VALUES ('sync_device', repeat('b', 64), now())")
    migrate.upgrade(settings)
    assert _exec(settings, "SELECT version_num FROM zaza_control_version") == [(HEAD,)]
    _exec(settings, "INSERT INTO login_attempt_counters (bucket, key_hash, window_start) "
                    "VALUES ('sync_device', repeat('b', 64), now())")


def test_compatible_server_serves_across_upgrade_and_downgrade(env, monkeypatch):  # noqa: F811
    import psycopg

    from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository

    _, code = env.store.create_code(env.tenants['a'].org.organization_id, "employee-1", ttl_hours=1)
    token = _enroll(env, code).json()["device_token"]
    settings = env.control_settings
    command.downgrade(migrate.alembic_config(settings), C3)
    original = _snapshot(settings)[1]
    base = dataclasses.replace(env.admin, dbname="unused")
    runtime = _runtime(settings, "off", monkeypatch, base)
    headers = {"Authorization": f"Bearer {token}", DEVICE_ID_HEADER: "device-1"}
    try:
        with TestClient(create_saas_app(runtime)) as client:
            def verify():
                assert client.get(HEALTH_PATH).status_code == 200
                assert client.get(DEVICE_PATH, headers=headers).status_code == 200
                record = usage()
                record["employee_id"] = "employee-1"
                record["data"]["app_name"] = record["record_id"]
                response = client.post(BATCH_PATH, headers=headers, json=batch(record))
                assert response.status_code == 200
                assert response.json()["summary"]["accepted"] == 1, response.json()
                assert client.get(DEVICE_PATH, headers={**headers, "Authorization": "Bearer bogus"}).status_code == 401

            verify()  # NEW runtime cold-starts against c0003
            with pytest.raises(RepositoryUnavailable):
                PostgresControlRepository(settings)  # strict operator check stays strict
            migrate.upgrade(settings)
            verify()
            _exec(settings, "INSERT INTO login_attempt_counters (bucket, key_hash, window_start) "
                            "VALUES ('sync_device', repeat('d', 64), now())")
            command.downgrade(migrate.alembic_config(settings), C3)
            assert _exec(settings, "SELECT version_num FROM zaza_control_version") == [(C3,)]
            assert _exec(settings, "SELECT count(*) FROM login_attempt_counters WHERE bucket='sync_device'") == [(0,)]
            assert _snapshot(settings)[1] == original
            with pytest.raises(psycopg.errors.CheckViolation):
                _exec(settings, "INSERT INTO login_attempt_counters (bucket, key_hash, window_start) "
                                "VALUES ('sync_device', repeat('d', 64), now())")
            verify()  # SAME process and pools keep authenticating against the tenant
            restarted = _runtime(settings, "off", monkeypatch, base)
            try:
                with TestClient(create_saas_app(restarted)) as cold:
                    assert cold.get(DEVICE_PATH, headers=headers).status_code == 200
            finally:
                restarted.close()
    finally:
        runtime.close()
