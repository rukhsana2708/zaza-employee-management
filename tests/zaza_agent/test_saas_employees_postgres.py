"""Phase 10G-E-A on real PostgreSQL (opt-in: ZAZA_TEST_POSTGRES_URL).

The SaaS employee operator commands run through the real CLI against a
disposable control database, a stand-in for the production maintenance
database (``ZAZA_DB_NAME``, ``postgres`` in production) and tenants A and B
provisioned by the real TenantProvisioner. Proves: the same employee id in
two tenants stays separate; unusable organizations/tenants are refused with
no fallback; the control and maintenance databases never receive employee
data; and the future internal bootstrap sequence. Every database and role it
creates is dropped.
"""

from __future__ import annotations

import dataclasses
import secrets
from types import SimpleNamespace

import pytest

from .conftest import pg_test_settings

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402

from deskmate.zaza_server.__main__ import main  # noqa: E402


def _admin():
    s = dataclasses.replace(pg_test_settings("public"), schema=None)
    with psycopg.connect(**s.connect_kwargs()) as conn:
        if not conn.execute("SELECT rolsuper OR (rolcreatedb AND rolcreaterole) FROM pg_roles "
                            "WHERE rolname = current_user").fetchone()[0]:
            pytest.skip("the test role may not create databases and roles")
    return s


def _q(settings, dbname, statement, params=()):  # noqa: ANN001, ANN202
    s = dataclasses.replace(settings, dbname=dbname)
    with psycopg.connect(**s.connect_kwargs(), autocommit=True) as conn:
        cur = conn.execute(statement, params)
        return cur.fetchall() if cur.description else None


@pytest.fixture
def env(monkeypatch):
    from deskmate.zaza_server.tenancy.control import migrate as control_migrate
    from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository

    admin = _admin()
    suffix = secrets.token_hex(4)
    control_db, maint_db, role = f"zaza_test_control_{suffix}", f"zaza_test_maint_{suffix}", f"zaza_test_app_{suffix}"
    created, control = [], None
    try:
        _q(admin, admin.dbname, sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(secrets.token_urlsafe(24))))
        for name in (control_db, maint_db):
            _q(admin, admin.dbname, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created.append(name)
        control_settings = dataclasses.replace(admin, dbname=control_db, pool_min=0, pool_max=2)
        control_migrate.upgrade(control_settings)
        control = PostgresControlRepository(control_settings, actor_type="CLI", actor_id="pytest-10gea")
        for key in ("ZAZA_DATABASE_URL", "ZAZA_CONTROL_DATABASE_URL", "ZAZA_DB_SCHEMA", "ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME",
                    "ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME", "ZAZA_DB_PASSWORD_FILE"):
            monkeypatch.delenv(key, raising=False)
        for prefix, dbname in (("ZAZA_", maint_db), ("ZAZA_CONTROL_", control_db)):
            monkeypatch.setenv(f"{prefix}DB_HOST", admin.host)
            monkeypatch.setenv(f"{prefix}DB_PORT", str(admin.port))
            monkeypatch.setenv(f"{prefix}DB_USER", admin.user)
            monkeypatch.setenv(f"{prefix}DB_PASSWORD", admin.password or "")
            monkeypatch.setenv(f"{prefix}DB_NAME", dbname)
            if admin.sslmode:
                monkeypatch.setenv(f"{prefix}DB_SSLMODE", admin.sslmode)
        monkeypatch.setenv("ZAZA_TENANT_RUNTIME_ROLE", role)
        monkeypatch.setenv("ZAZA_SERVER_BACKEND", "postgres")
        e = SimpleNamespace(admin=admin, control=control, control_db=control_db, maint_db=maint_db, role=role,
                            created=created, tenants={})

        def tenant(slug: str) -> SimpleNamespace:
            assert main(["saas-org-create", "--slug", slug, "--name", slug.title(), "--timezone", "Asia/Dhaka"]) == 0
            assert main(["tenant-provision", "--slug", slug]) == 0
            org_id = control.find_organization_id_by_slug(slug)
            db = control.get_tenant_database(org_id).database_name
            created.append(db)
            e.tenants[slug] = SimpleNamespace(org_id=org_id, db=db)
            return e.tenants[slug]

        e.tenant = tenant
        yield e
    finally:
        if control is not None:
            for t in control.list_ready_organization_ids():
                d = control.get_tenant_database(t)
                if d and d.database_name not in created:
                    created.append(d.database_name)
            control.close()
        for name in reversed(created):
            _q(admin, admin.dbname, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        _q(admin, admin.dbname, sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        assert _q(admin, admin.dbname, "SELECT datname FROM pg_database WHERE datname = ANY(%s)", (created,)) == []


def _employees(e, db):  # noqa: ANN001, ANN202
    return _q(e.admin, db, "SELECT employee_id, display_name, role, timezone, is_active FROM employees "
                           "ORDER BY employee_id")


def _no_employee_data_outside_tenants(e) -> None:  # noqa: ANN001
    for db in (e.control_db, e.maint_db):
        tables = {r[0] for r in _q(e.admin, db, "SELECT tablename FROM pg_tables WHERE schemaname NOT IN "
                                               "('pg_catalog', 'information_schema')")}
        assert "employees" not in tables and "devices" not in tables and "audit_logs" not in tables, (db, tables)
    assert _q(e.admin, e.maint_db, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == [(0,)]
    rows = _q(e.admin, e.control_db, "SELECT count(*) FROM control_audit_logs WHERE entity_type = 'employee' "
                                     "OR details::text ILIKE '%%employee%%'")
    assert rows == [(0,)]


def test_same_employee_id_in_two_tenants_through_the_cli(env, capsys):
    a, b = env.tenant("tenant-a"), env.tenant("tenant-b")
    assert main(["saas-employee-create", "--slug", "tenant-a", "--employee-id", "employee-1", "--name", "Alice A",
                 "--timezone", "Asia/Dhaka"]) == 0
    assert _employees(env, a.db) == [("employee-1", "Alice A", "EMPLOYEE", "Asia/Dhaka", True)]
    assert _employees(env, b.db) == []  # A's create wrote only A
    assert main(["saas-employee-create", "--slug", "TENANT-B", "--employee-id", "employee-1", "--name", "Bob B",
                 "--timezone", "UTC", "--role", "MANAGER"]) == 0
    assert _employees(env, b.db) == [("employee-1", "Bob B", "MANAGER", "UTC", True)]
    assert _employees(env, a.db) == [("employee-1", "Alice A", "EMPLOYEE", "Asia/Dhaka", True)]
    capsys.readouterr()

    assert main(["saas-employee-list", "--slug", "tenant-a"]) == 0
    out_a = capsys.readouterr().out
    assert "Alice A" in out_a and "Bob B" not in out_a
    assert main(["saas-employee-list", "--slug", "tenant-b"]) == 0
    out_b = capsys.readouterr().out
    assert "Bob B" in out_b and "Alice A" not in out_b
    for out in (out_a, out_b):  # metadata only: no database name, hashes or tokens
        assert a.db not in out and b.db not in out and "zaza_t_" not in out and "token" not in out.lower()

    # duplicate in the same tenant refused; B unaffected
    assert main(["saas-employee-create", "--slug", "tenant-a", "--employee-id", "employee-1", "--name", "Again",
                 "--timezone", "UTC"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert len(_employees(env, a.db)) == 1

    # disable A's employee-1: B's same id, memberships and organizations untouched
    orgs_before = _q(env.admin, env.control_db, "SELECT organization_id, status FROM organizations ORDER BY 1")
    assert main(["saas-employee-disable", "--slug", "tenant-a", "--employee-id", "employee-1"]) == 0
    assert _employees(env, a.db)[0][4] is False and _employees(env, b.db)[0][4] is True
    assert main(["saas-employee-disable", "--slug", "tenant-a", "--employee-id", "employee-1"]) == 0  # idempotent
    assert main(["saas-employee-enable", "--slug", "tenant-a", "--employee-id", "employee-1"]) == 0
    assert _employees(env, a.db)[0][4] is True
    assert _q(env.admin, env.control_db, "SELECT organization_id, status FROM organizations ORDER BY 1") == orgs_before
    assert _q(env.admin, env.control_db, "SELECT count(*) FROM organization_memberships") == [(0,)]
    assert main(["saas-employee-disable", "--slug", "tenant-b", "--employee-id", "nobody"]) == 2

    # tenant audit: the trusted operator is the actor; one entry per real change
    audit_a = _q(env.admin, a.db, "SELECT actor_type, actor_id, action, entity_id FROM audit_logs ORDER BY audit_id")
    assert audit_a == [("CLI", "saas-operator", "employee.create", "employee-1"),
                       ("CLI", "saas-operator", "employee.disable", "employee-1"),
                       ("CLI", "saas-operator", "employee.enable", "employee-1")]
    assert _q(env.admin, b.db, "SELECT actor_type, actor_id, action FROM audit_logs") == [
        ("CLI", "saas-operator", "employee.create")]
    _no_employee_data_outside_tenants(env)


@pytest.mark.parametrize("case", ["unknown", "suspended", "missing-tenant", "not-ready", "out-of-date", "unavailable"])
def test_unusable_organization_or_tenant_is_refused_without_fallback(env, capsys, case):
    b = env.tenant("tenant-b")
    assert main(["saas-employee-create", "--slug", "tenant-b", "--employee-id", "employee-1", "--name", "Bob",
                 "--timezone", "UTC"]) == 0
    slug = "tenant-x"
    if case == "missing-tenant":
        assert main(["saas-org-create", "--slug", slug, "--name", "X"]) == 0
    elif case != "unknown":
        x = env.tenant(slug)
        if case == "suspended":
            env.control.set_organization_status(x.org_id, "SUSPENDED")
        elif case == "not-ready":
            env.control.mark_tenant_database(x.org_id, "ERROR")
        elif case == "out-of-date":
            _q(env.admin, x.db, "UPDATE alembic_version SET version_num = '0003_schedule_timezone'")
        elif case == "unavailable":
            _q(env.admin, env.admin.dbname, sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                sql.Identifier(x.db)))
    capsys.readouterr()
    for argv in (["saas-employee-create", "--slug", slug, "--employee-id", "employee-1", "--name", "X",
                  "--timezone", "UTC"],
                 ["saas-employee-list", "--slug", slug],
                 ["saas-employee-disable", "--slug", slug, "--employee-id", "employee-1"]):
        assert main(argv) == 2, (case, argv)
        err = capsys.readouterr().err
        assert err.startswith("Error:") and "Traceback" not in err
        assert "zaza_t_" not in err  # the tenant database name is not echoed
        expected = {"unknown": "no organization", "out-of-date": "schema", "unavailable": "not usable"}.get(
            case, "not ACTIVE or its tenant database is not READY")
        assert expected in err, (case, err)
    if case == "unavailable":
        _q(env.admin, env.admin.dbname, sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(
            sql.Identifier(env.tenants[slug].db)))
    if slug in env.tenants:
        assert _employees(env, env.tenants[slug].db) == []
    assert _employees(env, b.db) == [("employee-1", "Bob", "EMPLOYEE", "UTC", True)]  # no fallback to B
    _no_employee_data_outside_tenants(env)


def test_internal_bootstrap_sequence_end_to_end(env):
    """The planned internal pilot bootstrap, on disposable databases:
    organization -> tenant -> user WITHOUT password -> OWNER membership ->
    employee through the tenant-aware command; then resolution checks."""
    from deskmate.zaza_server.tenancy.resolver import TenantAccessDenied, TenantResolver

    pilot = env.tenant("zaza-pilot")
    other = env.tenant("other-org")
    assert main(["saas-user-create", "--email", "owner@example.test", "--name", "Pilot Owner", "--no-password"]) == 0
    assert main(["saas-membership-add", "--email", "owner@example.test", "--slug", "zaza-pilot", "--role", "OWNER"]) == 0
    assert main(["saas-employee-create", "--slug", "zaza-pilot", "--employee-id", "pilot-employee-1",
                 "--name", "Pilot Employee", "--timezone", "Asia/Dhaka"]) == 0

    c = env.control
    user = c.find_user_by_email("owner@example.test")
    resolver = TenantResolver(c)
    owner_ctx = resolver.for_member(user.user_id, pilot.org_id)
    assert owner_ctx.database.database_name == pilot.db and owner_ctx.role.value == "OWNER"
    with pytest.raises(TenantAccessDenied):  # the OWNER membership grants only its organization
        resolver.for_member(user.user_id, other.org_id)
    assert resolver.for_job(pilot.org_id).database.database_name == pilot.db

    # a second, separate membership resolves to its own tenant with its own role
    assert main(["saas-membership-add", "--email", "owner@example.test", "--slug", "other-org", "--role", "VIEWER"]) == 0
    other_ctx = resolver.for_member(user.user_id, other.org_id)
    assert other_ctx.database.database_name == other.db and other_ctx.role.value == "VIEWER"
    assert resolver.for_member(user.user_id, pilot.org_id).role.value == "OWNER"

    counts = {t: _q(env.admin, env.control_db, sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t)))[0][0]
              for t in ("organizations", "users", "organization_memberships", "tenant_databases",
                        "user_password_credentials", "user_sessions", "device_enrollment_codes",
                        "device_credential_routes")}
    assert counts == {"organizations": 2, "users": 1, "organization_memberships": 2, "tenant_databases": 2,
                      "user_password_credentials": 0, "user_sessions": 0, "device_enrollment_codes": 0,
                      "device_credential_routes": 0}
    assert _employees(env, pilot.db) == [("pilot-employee-1", "Pilot Employee", "EMPLOYEE", "Asia/Dhaka", True)]
    assert _employees(env, other.db) == []
    for table in ("devices", "device_tokens", "manager_users"):
        for t in (pilot, other):
            assert _q(env.admin, t.db, sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))) == [(0,)]
    # the SaaS user exists only in the control plane; the employee only in its tenant
    for t in (pilot, other):
        assert {r[0] for r in _q(env.admin, t.db, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")} \
            .isdisjoint({"users", "organization_memberships", "organizations"})
    _no_employee_data_outside_tenants(env)
