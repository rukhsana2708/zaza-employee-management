"""Explicit Phase A expand/rollback contracts on disposable PostgreSQL."""

from unittest.mock import patch

import pytest
from alembic import command
from fastapi.testclient import TestClient

from deskmate.zaza_server.app import create_saas_app
from deskmate.zaza_server.postgres import PostgresRepository
from deskmate.zaza_server.postgres import migrate as tenant
from deskmate.zaza_server.repository import RepositoryUnavailable
from deskmate.zaza_server.tenancy.connections import open_tenant_repository
from deskmate.zaza_server.tenancy.control import migrate as control
from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository
from deskmate.zaza_server.tenancy.domain import TenancyError
from deskmate.zaza_server.workforce.service import WorkforceService

from .test_saas_postgres import _batch, _exec, _h, saas  # noqa: F401

T4 = "0004_manager_dashboard_auth"
T5 = "0005_workforce_foundation"
C4 = "c0004_sync_rate_limits"
C5 = "c0005_employee_membership"


@pytest.mark.parametrize(
    "revision", [None, "0003_schedule_timezone", T4, T5, "unknown", "0006_future"]
)
def test_tenant_runtime_matrix(revision):
    status = tenant.SchemaStatus(revision, T5)
    assert tenant.runtime_compatible(status) == (revision in {T4, T5})
    assert status.up_to_date == (revision == T5)
    assert not tenant.runtime_compatible(tenant.SchemaStatus(revision, "unknown"))


@pytest.mark.parametrize(
    "revision", [C4, C5, "c0003_device_enrollment", "unknown", "c0006_future", None]
)
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_control_runtime_matrix(revision, mode):
    status = control.SchemaStatus(revision, C5)
    assert control.runtime_compatible(status, sync_limit_mode=mode) == (
        revision in {C4, C5} or revision == "c0003_device_enrollment" and mode == "off"
    )
    assert status.up_to_date == (revision == C5)


def test_live_monitoring_old_new_and_rollback(saas):  # noqa: F811
    settings = saas.tenants["a"].settings
    command.downgrade(tenant.alembic_config(settings), T4)
    command.downgrade(control.alembic_config(saas.control_settings), C4)
    # Strict operator startup refuses pending migrations.
    with pytest.raises(RepositoryUnavailable):
        PostgresRepository(settings)
    with pytest.raises(RepositoryUnavailable):
        PostgresControlRepository(saas.control_settings)
    from deskmate.zaza_server.tenancy.employees import operator_repository_factory

    with pytest.raises(RepositoryUnavailable):
        operator_repository_factory(settings)
    cold = open_tenant_repository(settings)
    cold.close()
    c = saas.control
    user = c.create_user("foundation@compat.test", "Foundation")
    m = c.add_membership(saas.tenants["a"].org.organization_id, user.user_id, "OWNER")
    ctx = saas.runtime.resolver.for_member(user.user_id, m.organization_id)
    svc = WorkforceService(c, saas.runtime.resolver, saas.runtime.manager)
    with pytest.raises(TenancyError, match="control workforce migration"):
        svc.profile(ctx, "employee-1")
    with TestClient(create_saas_app(saas.runtime)) as client:
        headers = _h(saas.tenants["a"].token)

        def sync(label):
            assert client.get("/api/v1/sync/health").status_code == 200
            assert client.get("/api/v1/devices/me", headers=headers).status_code == 200
            body, _ = _batch(label)
            response = client.post("/api/v1/sync/batch", headers=headers, json=body)
            assert response.status_code == 200 and response.json()["summary"]["accepted"] == 2

        sync("before")
        control.upgrade(saas.control_settings)
        with pytest.raises(TenancyError, match="workforce schema migration"):
            svc.profile(ctx, "employee-1")
        before = _exec(
            settings, "SELECT employee_id,display_name,role,is_active,timezone FROM employees"
        )
        tenant.upgrade(settings)
        assert (
            _exec(
                settings, "SELECT employee_id,display_name,role,is_active,timezone FROM employees"
            )
            == before
        )
        assert svc.profile(ctx, "employee-1")["monitoring_enabled"]
        sync("after")
        # Old exact-head code cannot start against new schemas.
        with patch.object(tenant, "head_revision", return_value=T4):
            with pytest.raises(RepositoryUnavailable):
                PostgresRepository(settings)
        with patch.object(control, "head_revision", return_value=C4):
            with pytest.raises(RepositoryUnavailable):
                PostgresControlRepository(saas.control_settings)
        command.downgrade(tenant.alembic_config(settings), T4)
        command.downgrade(control.alembic_config(saas.control_settings), C4)
        sync("rollback")


def test_control_downgrade_refuses_employee_reclassification(saas):  # noqa: F811
    c = saas.control
    user = c.create_user("self@compat.test", "Self")
    c.add_membership(saas.tenants["a"].org.organization_id, user.user_id, "EMPLOYEE")
    with pytest.raises(Exception, match="organization_memberships_role_valid"):
        command.downgrade(control.alembic_config(saas.control_settings), C4)
    assert _exec(saas.control_settings, "SELECT version_num FROM zaza_control_version") == [(C5,)]
    assert (
        c.get_membership(saas.tenants["a"].org.organization_id, user.user_id).role.value
        == "EMPLOYEE"
    )


def test_tenant_downgrade_refuses_data_loss(saas):  # noqa: F811
    settings = saas.tenants["a"].settings
    _exec(
        settings,
        "INSERT INTO designations (id,display_name) VALUES (gen_random_uuid(),'Researcher')",
    )
    with pytest.raises(Exception, match="reviewed data archival"):
        command.downgrade(tenant.alembic_config(settings), T4)
    assert _exec(settings, "SELECT version_num FROM alembic_version") == [(T5,)]
    assert _exec(settings, "SELECT count(*) FROM designations") == [(1,)]


def test_failed_expansion_is_atomic(saas, monkeypatch):  # noqa: F811
    from alembic.operations import Operations

    settings = saas.tenants["a"].settings
    command.downgrade(tenant.alembic_config(settings), T4)
    original = Operations.execute

    def fail(self, statement, *args, **kwargs):
        if str(statement).startswith("CREATE TABLE employee_account_links"):
            return original(self, "SELECT phase_a_injected_failure()")
        return original(self, statement, *args, **kwargs)

    with monkeypatch.context() as injected:
        injected.setattr(Operations, "execute", fail)
        with pytest.raises(Exception, match="phase_a_injected_failure"):
            tenant.upgrade(settings)
    assert _exec(settings, "SELECT version_num FROM alembic_version") == [(T4,)]
    assert _exec(settings, "SELECT to_regclass('employee_profiles')") == [(None,)]
    tenant.upgrade(settings)


def test_unknown_equal_head_is_not_runtime_compatible():
    assert not control.runtime_compatible(
        control.SchemaStatus("future", "future"), sync_limit_mode="off"
    )
    assert not tenant.runtime_compatible(tenant.SchemaStatus("future", "future"))


def test_employee_role_accepted_by_existing_operator_parser(monkeypatch):
    from deskmate.zaza_server import __main__ as cli

    seen = []
    monkeypatch.setattr(cli, "_saas_operator", lambda args: seen.append(args.role) or 0)
    assert (
        cli.main(
            [
                "saas-membership-add",
                "--slug",
                "tenant-a",
                "--email",
                "self@example.test",
                "--role",
                "EMPLOYEE",
            ]
        )
        == 0
    )
    assert seen == ["EMPLOYEE"]


def test_control_expansion_failure_is_atomic(saas, monkeypatch):  # noqa: F811
    from alembic.operations import Operations

    settings = saas.control_settings
    command.downgrade(control.alembic_config(settings), C4)
    original = Operations.execute

    def fail(self, statement, *args, **kwargs):
        if str(statement).startswith("ALTER TABLE organization_memberships ADD CONSTRAINT"):
            return original(self, "SELECT phase_a_control_failure()")
        return original(self, statement, *args, **kwargs)

    with monkeypatch.context() as injected:
        injected.setattr(Operations, "execute", fail)
        with pytest.raises(Exception, match="phase_a_control_failure"):
            control.upgrade(settings)
    assert _exec(settings, "SELECT version_num FROM zaza_control_version") == [(C4,)]
    assert _exec(
        settings,
        "SELECT count(*) FROM pg_constraint WHERE conrelid='organization_memberships'::regclass "
        "AND conname='organization_memberships_role_valid'",
    ) == [(1,)]
    control.upgrade(settings)


def test_backfill_preserves_legacy_activation_and_audit(saas):  # noqa: F811
    settings = saas.tenants["a"].settings
    command.downgrade(tenant.alembic_config(settings), T4)
    cold = open_tenant_repository(settings)
    try:
        cold.add_employee("inactive", "Inactive", role="MANAGER")
        cold.set_employee_active("inactive", False)
        before = cold.get_employee("inactive")
        audit = cold.list_audit()
    finally:
        cold.close()
    tenant.upgrade(settings)
    repo = PostgresRepository(settings)
    try:
        assert repo.get_employee("inactive") == before
        assert repo.list_audit() == audit
        with repo.connection() as conn:
            p = conn.execute(
                "SELECT * FROM employee_profiles WHERE employee_id='inactive'"
            ).fetchone()
            assert p["employment_status"] == "INACTIVE" and p["monitoring_enabled"]
            assert p["designation_id"] is None and p["department_id"] is None
            assert (
                conn.execute("SELECT count(*) AS n FROM employee_account_links").fetchone()["n"]
                == 0
            )
    finally:
        repo.close()
