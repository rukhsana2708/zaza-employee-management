"""Phase 10D on real PostgreSQL (opt-in: ZAZA_TEST_POSTGRES_URL).

The production path end to end, in disposable databases:

- a control database, migrated with the control chain;
- tenants A and B provisioned by the real TenantProvisioner: server-named
  databases created by PostgresDatabaseCreator (privileged test role),
  migrated by TenantMigrationRunner, runtime privileges granted, READY;
- the web side connecting as a separate RUNTIME role (LOGIN only: no
  SUPERUSER, CREATEDB or CREATEROLE) through the real
  TenantRepositoryManager — the same employee/device ids in A and B, pool
  reuse, LRU eviction, a broken tenant, and no connections left after close.

Optional: ZAZA_TEST_CLOUDSQL_INSTANCE=<project:region:instance> when a
PostgreSQL Unix socket is mounted at /cloudsql/<that name> (as Cloud Run
does) — the same proof then runs over the Cloud SQL socket path.

Every database and role it creates is dropped.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
import time

import pytest

from .conftest import pg_test_settings

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402


def _admin_base():
    settings = dataclasses.replace(pg_test_settings("public"), schema=None)
    with psycopg.connect(**settings.connect_kwargs()) as conn:
        ok = conn.execute("SELECT rolsuper OR (rolcreatedb AND rolcreaterole) FROM pg_roles "
                          "WHERE rolname = current_user").fetchone()[0]
    if not ok:
        pytest.skip("the test role may not create databases and roles")
    return settings


def _execute(settings, statement, params=()):  # noqa: ANN001, ANN202
    with psycopg.connect(**settings.connect_kwargs(), autocommit=True) as conn:
        cur = conn.execute(statement, params)
        return cur.fetchall() if cur.description else None


def _cloudsql_variant(settings):  # noqa: ANN001, ANN202
    """The same server settings, but over the Cloud SQL socket path (opt-in)."""
    instance = os.environ.get("ZAZA_TEST_CLOUDSQL_INSTANCE")
    if not instance:
        return settings
    from deskmate.zaza_server.config import database_settings_from_env

    via_socket = database_settings_from_env({"ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME": instance,
                                             "ZAZA_DB_NAME": settings.dbname, "ZAZA_DB_USER": settings.user,
                                             "ZAZA_DB_PASSWORD": settings.password or ""})
    assert via_socket.host == f"/cloudsql/{instance}"
    return via_socket


@pytest.fixture
def saas():
    from deskmate.zaza_server.tenancy.control import migrate as control_migrate
    from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository

    admin = _cloudsql_variant(_admin_base())
    suffix = secrets.token_hex(4)
    control_db, runtime_role = f"zaza_test_control_{suffix}", f"zaza_test_app_{suffix}"
    runtime_password = secrets.token_urlsafe(24)
    created: list[str] = []
    closers: list = []
    try:
        _execute(admin, sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
            sql.Identifier(runtime_role), sql.Literal(runtime_password)))
        _execute(admin, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(control_db)))
        created.append(control_db)
        control_settings = dataclasses.replace(admin, dbname=control_db, pool_min=0, pool_max=2)
        control_migrate.upgrade(control_settings)
        control = PostgresControlRepository(control_settings, actor_type="CLI", actor_id="phase-10d-proof")
        closers.append(control)
        runtime = dataclasses.replace(admin, user=runtime_role, password=runtime_password, dbname="zaza")
        yield dataclasses.make_dataclass("Saas", ["admin", "control", "control_db", "runtime", "role", "created",
                                                  "closers"])(admin, control, control_db, runtime, runtime_role,
                                                             created, closers)
    finally:
        for closer in reversed(closers):
            closer.close()
        for name in reversed(created):
            _execute(admin, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        _execute(admin, sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime_role)))
        left = _execute(admin, "SELECT datname FROM pg_database WHERE datname = ANY(%s)", (created,))
        assert left == [], "disposable databases were not removed"
        assert _execute(admin, "SELECT 1 FROM pg_roles WHERE rolname = %s", (runtime_role,)) == []


def _provision(saas, slug):  # noqa: ANN001, ANN202
    from deskmate.zaza_server.tenancy.provisioning import PostgresDatabaseCreator, TenantProvisioner
    from deskmate.zaza_server.tenancy.tenant_migrations import TenantMigrationRunner

    org = saas.control.create_organization(slug, slug.title())
    owner = saas.control.create_user(f"owner@{slug}.example", f"Owner {slug}")
    saas.control.add_membership(org.organization_id, owner.user_id, "OWNER")
    runner = TenantMigrationRunner(saas.control, saas.admin, control_database_name=saas.control_db)
    provisioner = TenantProvisioner(saas.control, PostgresDatabaseCreator(saas.admin, runtime_role=saas.role), runner)
    result = provisioner.provision(org.organization_id, "asia-south1")
    if result.database_name:
        saas.created.append(result.database_name)
    assert result.ok, result.error
    return org, owner, result


def _runtime_connections(saas) -> int:  # noqa: ANN001
    return _execute(saas.admin, "SELECT count(*) FROM pg_stat_activity WHERE usename = %s",
                    (saas.role,))[0][0]


def test_cloud_sql_tenant_connectivity_end_to_end(saas):
    from deskmate.zaza_server.auth import AuthError, authenticate, register_device
    from deskmate.zaza_server.postgres import migrate as tenant_migrate
    from deskmate.zaza_server.tenancy.connections import TenantPoolLimits, TenantRepositoryManager
    from deskmate.zaza_server.tenancy.resolver import TenantResolver

    from .test_postgres_integration import incoming, usage

    # control connection works, and holds no activity tables
    assert saas.control.list_ready_organization_ids() == []
    rows = _execute(dataclasses.replace(saas.admin, dbname=saas.control_db),
                    "SELECT to_regclass('employees')::text, to_regclass('organizations')::text")
    assert rows == [(None, "organizations")]

    tenants = {slug: _provision(saas, slug) for slug in ("tenant-a", "tenant-b")}
    for org, _, result in tenants.values():
        assert result.schema_revision == tenant_migrate.head_revision()
        descriptor = saas.control.get_tenant_database(org.organization_id)
        assert descriptor.status.value == "READY" and descriptor.database_name == result.database_name
    names = {r.database_name for _, _, r in tenants.values()}
    assert len(names) == 2

    resolver = TenantResolver(saas.control)
    ctx = {slug: resolver.for_member(owner.user_id, org.organization_id) for slug, (org, owner, _) in tenants.items()}

    # the web side: RUNTIME role, real manager, small limits
    manager = TenantRepositoryManager(saas.runtime, TenantPoolLimits(pool_max=2, cache_max=1, idle_seconds=60))
    tokens, first = {}, {}
    try:
        for slug, c in ctx.items():
            with manager.repository(c) as repo:
                with repo.connection() as conn:  # A connects only to A, B only to B
                    db, user = conn.execute("SELECT current_database() AS d, current_user AS u").fetchone().values()
                assert (db, user) == (c.database.database_name, saas.role)
                repo.add_employee("employee-1", f"Employee of {slug}", timezone="UTC")
                tokens[slug] = register_device(repo, "device-1", "employee-1").token
                raw = usage(app_name=f"{slug}-Code.exe")
                raw["employee_id"] = "employee-1"
                assert repo.upsert_records([incoming(raw)])[0].status == "accepted"
                first[slug] = repo

        # cache_max=1: opening B evicted (closed) A's pool
        assert first["tenant-a"].closed and not first["tenant-b"].closed
        assert manager.stats()["open_pools"] == 1

        # repeated access to B reuses B's pool; A is re-created for A
        with manager.repository(ctx["tenant-b"]) as b_again:
            assert b_again is first["tenant-b"]
        for slug, c in ctx.items():
            other = "tenant-b" if slug == "tenant-a" else "tenant-a"
            with manager.repository(c) as repo:
                assert repo.get_employee("employee-1").display_name == f"Employee of {slug}"
                assert [d.device_id for d in repo.list_devices()] == ["device-1"]
                assert authenticate(repo, f"Bearer {tokens[slug]}", "device-1").device_id == "device-1"
                with pytest.raises(AuthError):
                    authenticate(repo, f"Bearer {tokens[other]}", "device-1")
                apps = {r.payload["data"]["app_name"] for r in repo.list_records()}
                assert apps == {f"{slug}-Code.exe"}
        assert first["tenant-a"].closed

        # the runtime role has DML only: no DDL in a tenant, no new databases
        with manager.repository(ctx["tenant-a"]) as repo, repo.connection() as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE sneaky (x int)")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE DATABASE zaza_t_" + "a" * 26)
        attrs = _execute(saas.admin, "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s",
                         (saas.role,))
        assert attrs == [(False, False, False)]
        assert _runtime_connections(saas) >= 1
    finally:
        manager.close()

    # closing the manager leaves no application connections behind
    deadline = time.monotonic() + 10
    while _runtime_connections(saas) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _runtime_connections(saas) == 0


def test_an_unavailable_tenant_database_does_not_affect_another(saas):
    from deskmate.zaza_server.repository import RepositoryUnavailable
    from deskmate.zaza_server.tenancy.connections import TenantPoolLimits, TenantRepositoryManager
    from deskmate.zaza_server.tenancy.resolver import TenantResolver

    (org_a, owner_a, a), (org_b, owner_b, b) = _provision(saas, "tenant-a"), _provision(saas, "tenant-b")
    resolver = TenantResolver(saas.control)
    ctx_a = resolver.for_member(owner_a.user_id, org_a.organization_id)
    ctx_b = resolver.for_member(owner_b.user_id, org_b.organization_id)
    manager = TenantRepositoryManager(saas.runtime, TenantPoolLimits(cache_max=4))
    try:
        with manager.repository(ctx_b) as b_repo:
            b_repo.add_employee("employee-1", "B", timezone="UTC")
        # tenant A's database becomes unreachable for the runtime role (outage stand-in)
        _execute(saas.admin, sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
            sql.Identifier(a.database_name), sql.Identifier(saas.role)))
        for _ in range(2):
            with pytest.raises(RepositoryUnavailable), manager.repository(ctx_a):
                pass
        with manager.repository(ctx_b) as same:  # B keeps its own, healthy pool
            assert same is b_repo and same.get_employee("employee-1").display_name == "B"
            with same.connection() as conn:
                assert conn.execute("SELECT current_database() AS d").fetchone()["d"] == b.database_name
        assert manager.stats()["open_pools"] == 1
        # A recovers on its next use — still A's database
        _execute(saas.admin, sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(a.database_name), sql.Identifier(saas.role)))
        with manager.repository(ctx_a) as a_repo, a_repo.connection() as conn:
            assert conn.execute("SELECT current_database() AS d").fetchone()["d"] == a.database_name
            assert a_repo.get_employee("employee-1") is None
    finally:
        manager.close()


def test_tenant_migrate_runner_on_real_databases(saas):
    from deskmate.zaza_server.postgres import migrate as tenant_migrate
    from deskmate.zaza_server.tenancy.tenant_migrations import TenantMigrationRunner

    (org_a, _, a), (org_b, _, b) = _provision(saas, "tenant-a"), _provision(saas, "tenant-b")
    runner = TenantMigrationRunner(saas.control, saas.admin, control_database_name=saas.control_db)
    results = runner.migrate_many()
    assert {r.organization_id for r in results} == {org_a.organization_id, org_b.organization_id}
    assert all(r.ok and r.before == r.after == tenant_migrate.head_revision() for r in results)
    # the control database is refused by the real upgrade path as well
    with pytest.raises(RuntimeError, match="control-plane"):
        tenant_migrate.upgrade(dataclasses.replace(saas.admin, dbname=saas.control_db))
