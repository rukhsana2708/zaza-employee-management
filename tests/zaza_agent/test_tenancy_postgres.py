"""Phase 10C on PostgreSQL (opt-in: ZAZA_TEST_POSTGRES_URL, see conftest.py).

1. Control-plane schema rules (in its own schema ``zaza_pytest_control`` of
   the disposable test database): immutable ids, unique slugs, SaaS roles
   only, one tenant database per organization, append-only audit.
2. The database-per-tenant proof: a CONTROL database plus tenant databases A
   and B (real, separate PostgreSQL databases with server-generated names),
   the existing tenant migrations applied to each independently, identical
   employee/device ids in both, and no data visible across them. Needs a
   role that may CREATE DATABASE; every database it creates is dropped.
"""

from __future__ import annotations

import dataclasses
import secrets
import uuid

import pytest

from deskmate.zaza_server.tenancy import domain
from deskmate.zaza_server.tenancy.domain import MembershipRole, TenancyError

from .conftest import pg_admin_execute, pg_test_settings

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402

CONTROL_SCHEMA = "zaza_pytest_control"


@pytest.fixture(scope="module")
def control_settings():
    settings = pg_test_settings(CONTROL_SCHEMA)
    from deskmate.zaza_server.tenancy.control import migrate as control

    pg_admin_execute(settings, f"DROP SCHEMA IF EXISTS {CONTROL_SCHEMA} CASCADE", f"CREATE SCHEMA {CONTROL_SCHEMA}")
    control.upgrade(settings)
    yield settings
    pg_admin_execute(settings, f"DROP SCHEMA IF EXISTS {CONTROL_SCHEMA} CASCADE")


@pytest.fixture
def control(control_settings):
    from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository

    pg_admin_execute(control_settings, *(f"ALTER TABLE {CONTROL_SCHEMA}.control_audit_logs {m} TRIGGER "
                                         "control_audit_logs_append_only" for m in ("DISABLE",)),
                     f"TRUNCATE {CONTROL_SCHEMA}.device_enrollment_codes, {CONTROL_SCHEMA}.device_credential_routes, "
                     f"{CONTROL_SCHEMA}.user_sessions, "
                     f"{CONTROL_SCHEMA}.user_password_credentials, {CONTROL_SCHEMA}.login_attempt_counters, "
                     f"{CONTROL_SCHEMA}.control_audit_logs, {CONTROL_SCHEMA}.tenant_databases, "
                     f"{CONTROL_SCHEMA}.organization_memberships, {CONTROL_SCHEMA}.users, "
                     f"{CONTROL_SCHEMA}.organizations",
                     f"ALTER TABLE {CONTROL_SCHEMA}.control_audit_logs ENABLE TRIGGER control_audit_logs_append_only")
    repo = PostgresControlRepository(control_settings, actor_type="CLI", actor_id="pytest")
    yield repo
    repo.close()


def _raw(settings, statement: str, params: tuple = ()):  # noqa: ANN001, ANN202
    with psycopg.connect(**settings.connect_kwargs(), autocommit=True) as conn:
        cur = conn.execute(statement, params)
        return cur.fetchall() if cur.description else None


# ─── 1. control-plane schema rules ─────────────────────────────────────────


def test_control_schema_reaches_its_own_head(control_settings):
    from deskmate.zaza_server.tenancy.control import migrate as control

    with psycopg.connect(**control_settings.connect_kwargs()) as conn:
        status = control.schema_status(conn)
        assert status.up_to_date and status.current == control.head_revision() == "c0005_employee_membership"
        assert conn.execute("SELECT to_regclass('alembic_version')::text").fetchone()[0] is None  # no tenant chain


def test_organization_ids_are_immutable(control, control_settings):
    org = control.create_organization("Zaza Studio", "Zaza Studio", default_timezone="Asia/Dhaka")
    assert org.slug == "zaza-studio" and isinstance(org.organization_id, uuid.UUID)
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw(control_settings, "UPDATE organizations SET organization_id = %s WHERE organization_id = %s",
             (uuid.uuid4(), org.organization_id))
    renamed = _raw(control_settings, "UPDATE organizations SET display_name = 'Zaza Studio Ltd' "
                                     "WHERE organization_id = %s RETURNING organization_id, updated_at > created_at",
                   (org.organization_id,))
    assert renamed[0][0] == org.organization_id


def test_duplicate_slugs_are_rejected(control, control_settings):
    control.create_organization("abc-school", "ABC School")
    with pytest.raises(TenancyError, match="slug already exists"):
        control.create_organization("ABC School", "Another ABC")  # normalizes to the same slug
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw(control_settings, "INSERT INTO organizations (organization_id, slug, display_name) "
                               "VALUES (%s, 'Bad Slug', 'x')", (uuid.uuid4(),))


def test_membership_roles_are_enforced_by_the_database(control, control_settings):
    org = control.create_organization("acme-call-center", "Acme Call Center")
    user = control.create_user("Owner@Acme.example", "Acme Owner")
    assert user.email == "owner@acme.example"
    assert MembershipRole.EMPLOYEE.value == "EMPLOYEE"
    for bad in ("PLATFORM_SUPERADMIN", "owner"):
        with pytest.raises(psycopg.errors.CheckViolation):
            _raw(control_settings, "INSERT INTO organization_memberships (membership_id, organization_id, user_id, "
                                   "role) VALUES (%s, %s, %s, %s)", (uuid.uuid4(), org.organization_id, user.user_id,
                                                                      bad))
    m = control.add_membership(org.organization_id, user.user_id, "OWNER")
    assert m.role is MembershipRole.OWNER
    with pytest.raises(TenancyError, match="already a member"):
        control.add_membership(org.organization_id, user.user_id, "VIEWER")
    assert control.change_membership_role(org.organization_id, user.user_id, "ADMIN").role is MembershipRole.ADMIN


def test_a_user_may_belong_to_several_organizations(control):
    a = control.create_organization("tenant-a", "Tenant A")
    b = control.create_organization("tenant-b", "Tenant B")
    u = control.create_user("consultant@example.com", "Consultant")
    control.add_membership(a.organization_id, u.user_id, "ADMIN")
    control.add_membership(b.organization_id, u.user_id, "VIEWER")
    roles = {m.organization_id: m.role for m in control.list_memberships_for_user(u.user_id)}
    assert roles == {a.organization_id: MembershipRole.ADMIN, b.organization_id: MembershipRole.VIEWER}


def test_tenant_database_mapping_is_one_to_one_and_server_named(control, control_settings):
    a = control.create_organization("tenant-a", "Tenant A")
    b = control.create_organization("tenant-b", "Tenant B")
    da = control.assign_tenant_database(a.organization_id, "asia-south1")
    db = control.assign_tenant_database(b.organization_id, "asia-south1")
    assert domain.tenant_database_name(da.database_name) and da.database_name != db.database_name
    assert da.status.value == "PROVISIONING"
    with pytest.raises(TenancyError, match="already has a tenant database"):
        control.assign_tenant_database(a.organization_id, "asia-south1")
    c = control.create_organization("tenant-c", "Tenant C")
    with pytest.raises(psycopg.errors.UniqueViolation):  # two organizations can't share one database
        _raw(control_settings, "INSERT INTO tenant_databases (organization_id, database_name, region) "
                               "VALUES (%s, %s, 'asia-south1')", (c.organization_id, da.database_name))
    with pytest.raises(psycopg.errors.CheckViolation):  # nor take over another's name by renaming
        _raw(control_settings, "UPDATE tenant_databases SET database_name = %s WHERE organization_id = %s",
             (da.database_name, b.organization_id))
    with pytest.raises(psycopg.errors.CheckViolation):    # nor can it be renamed / moved to another organization
        _raw(control_settings, "UPDATE tenant_databases SET database_name = %s WHERE organization_id = %s",
             (domain.generate_tenant_database_name(), a.organization_id))
    for bad in ("postgres", "zaza_control", "zaza_t_x; DROP DATABASE postgres"):
        with pytest.raises(psycopg.errors.CheckViolation):
            _raw(control_settings, "INSERT INTO tenant_databases (organization_id, database_name, region) "
                                   "VALUES (%s, %s, 'asia-south1')", (control.create_organization(
                                       f"t-{secrets.token_hex(3)}", "T").organization_id, bad))
    with pytest.raises(psycopg.errors.CheckViolation):  # READY needs a schema revision
        _raw(control_settings, "UPDATE tenant_databases SET status = 'READY' WHERE organization_id = %s",
             (a.organization_id,))
    ready = control.mark_tenant_database(a.organization_id, "READY", "0004_manager_dashboard_auth")
    assert ready.status.value == "READY"
    assert control.list_ready_organization_ids() == [a.organization_id]
    control.set_organization_status(a.organization_id, "SUSPENDED")
    assert control.list_ready_organization_ids() == []


def test_control_audit_records_changes_without_secrets_and_is_append_only(control, control_settings):
    org = control.create_organization("audited", "Audited Org")
    user = control.create_user("admin@audited.example", "Admin")
    control.add_membership(org.organization_id, user.user_id, "MANAGER")
    control.change_membership_role(org.organization_id, user.user_id, "ADMIN")
    control.assign_tenant_database(org.organization_id, "asia-south1")
    control.set_organization_status(org.organization_id, "SUSPENDED")
    entries = control.list_audit(org.organization_id)
    assert [e.action for e in entries] == ["organization.create", "membership.create", "membership.role_change",
                                           "tenant_database.assign", "organization.suspend"]
    assert all(e.actor_type == "CLI" and e.actor_id == "pytest" and e.organization_id == org.organization_id
               for e in entries)
    assert entries[2].details == {"user_id": str(user.user_id), "from": "MANAGER", "to": "ADMIN"}
    blob = repr([e.details for e in entries]).lower()
    assert "password" not in blob and "zzd_" not in blob and "postgresql://" not in blob
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw(control_settings, "UPDATE control_audit_logs SET action = 'user.create'")
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw(control_settings, "DELETE FROM control_audit_logs")


def test_tenant_schema_refuses_to_install_into_a_control_database(control_settings):
    from deskmate.zaza_server.postgres import migrate as tenant

    with pytest.raises(RuntimeError, match="control-plane table"):
        tenant.upgrade(control_settings)


def test_control_schema_refuses_to_install_into_a_tenant_database(pg_settings):
    from deskmate.zaza_server.tenancy.control import migrate as control

    with pytest.raises(RuntimeError, match="tenant table"):
        control.upgrade(pg_settings)  # the migrated tenant test schema


# ─── 2. database-per-tenant proof (separate databases) ─────────────────────


@pytest.fixture
def server_base():
    """Server-side connection settings (host/user/password) — the only source
    of connection details for every tenant database."""
    settings = pg_test_settings("public")
    with psycopg.connect(**dataclasses.replace(settings, schema=None).connect_kwargs()) as conn:
        can = conn.execute("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()[0]
    if not can:
        pytest.skip("the test role may not CREATE DATABASE")
    return dataclasses.replace(settings, schema=None)


def _create_db(base, name: str) -> None:  # noqa: ANN001
    with psycopg.connect(**base.connect_kwargs(), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _drop_db(base, name: str) -> None:  # noqa: ANN001
    with psycopg.connect(**base.connect_kwargs(), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_database_per_tenant_isolation(server_base):
    from deskmate.zaza_server.auth import AuthError, authenticate, register_device
    from deskmate.zaza_server.postgres import PostgresRepository
    from deskmate.zaza_server.postgres import migrate as tenant_migrate
    from deskmate.zaza_server.tenancy.context import tenant_database_settings

    from .test_postgres_integration import incoming, usage
    from deskmate.zaza_server.tenancy.control import migrate as control_migrate
    from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository
    from deskmate.zaza_server.tenancy.resolver import TenantAccessDenied, TenantResolver

    control_db = f"zaza_test_control_{secrets.token_hex(4)}"
    created: list[str] = []
    repos: list = []
    try:
        # control database
        _create_db(server_base, control_db)
        created.append(control_db)
        control_settings = dataclasses.replace(server_base, dbname=control_db)
        control_migrate.upgrade(control_settings)
        control = PostgresControlRepository(control_settings, actor_type="CLI", actor_id="isolation-proof")
        repos.append(control)

        # organizations, owners, tenant databases (names generated by the server)
        orgs = {}
        for slug in ("tenant-a", "tenant-b"):
            org = control.create_organization(slug, slug.replace("-", " ").title())
            owner = control.create_user(f"owner@{slug}.example", f"Owner {slug}")
            control.add_membership(org.organization_id, owner.user_id, "OWNER")
            descriptor = control.assign_tenant_database(org.organization_id, "asia-south1")
            _create_db(server_base, descriptor.database_name)
            created.append(descriptor.database_name)
            # the existing tenant migration chain, applied to THIS tenant database only
            tenant_migrate.upgrade(dataclasses.replace(server_base, dbname=descriptor.database_name))
            control.mark_tenant_database(org.organization_id, "READY", tenant_migrate.head_revision())
            orgs[slug] = (org, owner)

        resolver = TenantResolver(control)
        ctx = {slug: resolver.for_member(owner.user_id, org.organization_id) for slug, (org, owner) in orgs.items()}
        assert ctx["tenant-a"].database.database_name != ctx["tenant-b"].database.database_name

        # cross-tenant negative: A's owner is not authorized for B (and vice versa)
        with pytest.raises(TenantAccessDenied):
            resolver.for_member(orgs["tenant-a"][1].user_id, orgs["tenant-b"][0].organization_id)
        with pytest.raises(TenantAccessDenied):
            resolver.for_member(orgs["tenant-b"][1].user_id, orgs["tenant-a"][0].organization_id)

        # each tenant database reached the same schema head on its own
        heads = set()
        for c in ctx.values():
            with psycopg.connect(**tenant_database_settings(server_base, c).connect_kwargs()) as conn:
                heads.add(tenant_migrate.current_revision(conn))
                assert conn.execute("SELECT to_regclass('organizations')::text").fetchone()[0] is None
        assert heads == {tenant_migrate.head_revision()}
        with psycopg.connect(**control_settings.connect_kwargs()) as conn:  # no activity tables in the control DB
            for table in ("employees", "devices", "activity_periods", "work_sessions"):
                assert conn.execute("SELECT to_regclass(%s)::text", (table,)).fetchone()[0] is None

        # the SAME employee and device ids in both tenants, independently
        tenant = {}
        tokens = {}
        for slug, c in ctx.items():
            repo = PostgresRepository(tenant_database_settings(server_base, c), actor_type="CLI", actor_id=slug)
            repos.append(repo)
            repo.add_employee("employee-1", f"Employee of {slug}", role="ADMIN", timezone="UTC")
            tokens[slug] = register_device(repo, "device-1", "employee-1", display_name=f"PC of {slug}").token
            tenant[slug] = repo

        for slug, repo in tenant.items():
            other = "tenant-b" if slug == "tenant-a" else "tenant-a"
            assert repo.get_employee("employee-1").display_name == f"Employee of {slug}"
            assert [e.employee_id for e in repo.list_employees()] == ["employee-1"]
            assert [d.device_id for d in repo.list_devices()] == ["device-1"]
            # A's token authenticates in A only; in B the very same device id rejects it
            assert authenticate(repo, f"Bearer {tokens[slug]}", "device-1").device_id == "device-1"
            with pytest.raises(AuthError):
                authenticate(repo, f"Bearer {tokens[other]}", "device-1")

        # activity written to A is not visible in B, and vice versa (real wire records, same device/employee ids)
        written = {}
        for slug, repo in tenant.items():
            records = []
            for app in ("Code.exe", "Excel.exe"):
                raw = usage(app_name=f"{slug}-{app}")
                raw["employee_id"] = "employee-1"
                records.append(incoming(raw))
            outcomes = repo.upsert_records(records)
            assert all(o.status == "accepted" for o in outcomes), outcomes
            written[slug] = {r.record_id for r in records}
        for slug, repo in tenant.items():
            other = "tenant-b" if slug == "tenant-a" else "tenant-a"
            mine = repo.list_records()
            assert {r.record_id for r in mine} == written[slug]
            assert all(r.payload["data"]["app_name"].startswith(slug) for r in mine)
            assert repo.count_records(device_id="device-1") == 2
            for rid in written[other]:
                assert repo.get_record("app_usage_daily", rid) is None
    finally:
        for repo in repos:
            repo.close()
        for name in reversed(created):
            _drop_db(server_base, name)
    with psycopg.connect(**server_base.connect_kwargs()) as conn:
        left = conn.execute("SELECT datname FROM pg_database WHERE datname = ANY(%s)", (created,)).fetchall()
    assert left == [], "disposable databases were not removed"
