"""Phase 10G-A on real PostgreSQL (opt-in: ZAZA_TEST_POSTGRES_URL).

Disposable control database at the control head (c0003); tenants A and B
PROVISIONED by the real TenantProvisioner (server-named databases, closed to
PUBLIC, runtime role granted); the web side runs as a separate LOGIN-only
runtime role; enrollment through the real PostgresEnrollmentStore and the
SaaS app. Every database and role it creates is dropped.
"""

from __future__ import annotations

import dataclasses
import secrets
import threading
from types import SimpleNamespace

import pytest

from .conftest import pg_test_settings

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("argon2")
from fastapi.testclient import TestClient  # noqa: E402
from psycopg import sql  # noqa: E402


def _admin():
    s = dataclasses.replace(pg_test_settings("public"), schema=None)
    with psycopg.connect(**s.connect_kwargs()) as conn:
        if not conn.execute("SELECT rolsuper OR (rolcreatedb AND rolcreaterole) FROM pg_roles "
                            "WHERE rolname = current_user").fetchone()[0]:
            pytest.skip("the test role may not create databases and roles")
    return s


def _exec(settings, statement, params=()):  # noqa: ANN001, ANN202
    with psycopg.connect(**settings.connect_kwargs(), autocommit=True) as conn:
        cur = conn.execute(statement, params)
        return cur.fetchall() if cur.description else None


@pytest.fixture
def env():
    from deskmate.zaza_server.postgres import PostgresRepository
    from deskmate.zaza_server.tenancy.connections import TenantPoolLimits, TenantRepositoryManager
    from deskmate.zaza_server.tenancy.control import migrate as control_migrate
    from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository
    from deskmate.zaza_server.tenancy.device_routes import PostgresDeviceRoutes
    from deskmate.zaza_server.tenancy.enrollment import EnrollmentService, PostgresEnrollmentStore
    from deskmate.zaza_server.tenancy.provisioning import PostgresDatabaseCreator, TenantProvisioner
    from deskmate.zaza_server.tenancy.resolver import TenantResolver
    from deskmate.zaza_server.tenancy.saas_auth import PostgresAuthStore, SaasAuthService, SaasAuthSettings
    from deskmate.zaza_server.tenancy.saas_sync import SaasRuntime
    from deskmate.zaza_server.tenancy.tenant_migrations import TenantMigrationRunner

    admin = _admin()
    suffix = secrets.token_hex(4)
    control_db, role = f"zaza_test_control_{suffix}", f"zaza_test_app_{suffix}"
    role_pw = secrets.token_urlsafe(24)
    created, closers = [], []
    try:
        _exec(admin, sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(role_pw)))
        _exec(admin, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(control_db)))
        created.append(control_db)
        control_settings = dataclasses.replace(admin, dbname=control_db, pool_min=0, pool_max=2)
        control_migrate.upgrade(control_settings)
        control = PostgresControlRepository(control_settings, actor_type="CLI", actor_id="pytest-10ga")
        closers.append(control)
        runner = TenantMigrationRunner(control, admin, control_database_name=control_db)
        creator = PostgresDatabaseCreator(admin, runtime_role=role)
        provisioner = TenantProvisioner(control, creator, runner)
        tenants = {}
        for name in ("a", "b"):
            org = control.create_organization(f"tenant-{name}", f"Tenant {name.upper()}")
            result = provisioner.provision(org.organization_id, "asia-south1")
            if result.database_name:
                created.append(result.database_name)
            assert result.ok, result.error
            settings = dataclasses.replace(admin, dbname=result.database_name)
            repo = PostgresRepository(settings, actor_type="CLI", actor_id="pytest")
            try:
                repo.add_employee("employee-1", f"Employee of {name.upper()}", timezone="UTC")
            finally:
                repo.close()
            tenants[name] = SimpleNamespace(org=org, db=result.database_name, settings=settings, result=result)
        runtime_base = dataclasses.replace(admin, user=role, password=role_pw, dbname="unused")
        manager = TenantRepositoryManager(runtime_base, TenantPoolLimits(cache_max=2))
        closers.append(manager)
        resolver, counters = TenantResolver(control), PostgresAuthStore(control)
        store = PostgresEnrollmentStore(control)
        service = EnrollmentService(store, resolver, manager, counters)
        runtime = SaasRuntime(control=control, resolver=resolver, manager=manager, routes=PostgresDeviceRoutes(control),
                              auth=SaasAuthService(counters, SaasAuthSettings(cookie_secure=False)), enrollment=service)
        from deskmate.zaza_server.app import create_saas_app

        yield SimpleNamespace(admin=admin, control=control, control_settings=control_settings, tenants=tenants,
                              store=store, service=service, runtime=runtime, provisioner=provisioner, creator=creator,
                              runner=runner, role=role, created=created,
                              client=TestClient(create_saas_app(runtime), raise_server_exceptions=False))
    finally:
        for closer in reversed(closers):
            closer.close()
        for name in reversed(created):
            _exec(admin, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        _exec(admin, sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        assert _exec(admin, "SELECT datname FROM pg_database WHERE datname = ANY(%s)", (created,)) == []


def _enroll(env, code, device="device-1"):  # noqa: ANN001, ANN202
    return env.client.post("/api/v1/enroll", json={"enrollment_code": code, "device_id": device})


# ─── provisioning ──────────────────────────────────────────────────────────


def test_provisioned_tenants_are_separate_current_and_closed(env):
    from deskmate.zaza_server.postgres import migrate as tenant_migrate

    a, b = env.tenants["a"], env.tenants["b"]
    assert a.db != b.db and a.db.startswith("zaza_t_") and b.db.startswith("zaza_t_")
    for t in (a, b):
        assert t.result.schema_revision == tenant_migrate.head_revision()
        assert env.control.get_tenant_database(t.org.organization_id).status.value == "READY"
        acl = _exec(env.admin, "SELECT datacl::text FROM pg_database WHERE datname = %s", (t.db,))[0][0]
        assert acl and not any(item.startswith("=") for item in acl.strip("{}").split(","))  # no PUBLIC entry
        assert f"{env.role}=Tc" in acl or f"{env.role}=cT" in acl
        priv = _exec(env.admin, "SELECT has_database_privilege(%s, %s, 'CONNECT')", (env.role, t.db))[0][0]
        assert priv
    assert _exec(env.admin, "SELECT has_database_privilege('public', %s, 'CONNECT')", (a.db,))[0][0] is False


def test_repeated_provisioning_never_creates_a_second_database(env):
    a = env.tenants["a"]
    count = "SELECT count(*) FROM pg_database WHERE left(datname, 7) = 'zaza_t_'"
    before = _exec(env.admin, count)[0][0]
    again = env.provisioner.provision(a.org.organization_id, "asia-south1")
    assert again.ok and again.database_name == a.db
    assert _exec(env.admin, count)[0][0] == before


def test_provisioning_failure_is_recoverable(env):
    from deskmate.zaza_server.tenancy.provisioning import TenantProvisioner

    org = env.control.create_organization("tenant-c", "Tenant C")

    class GrantFails:
        def __getattr__(self, name):  # noqa: ANN001, ANN204
            return getattr(env.creator, name)

        def grant_runtime_access(self, name):  # noqa: ANN001, ANN201
            raise RuntimeError("injected grant failure")

    failed = TenantProvisioner(env.control, GrantFails(), env.runner).provision(org.organization_id, "asia-south1")
    env.created.append(failed.database_name)
    assert not failed.ok and failed.status.value == "ERROR"
    assert env.control.get_tenant_database(org.organization_id).status.value == "ERROR"
    retry = env.provisioner.provision(org.organization_id, "asia-south1")
    assert retry.ok and retry.status.value == "READY" and retry.database_name == failed.database_name


# ─── c0003 schema ──────────────────────────────────────────────────────────


def test_c0003_terminal_states_and_constraints(env):
    a = env.tenants["a"]
    info, _ = env.store.create_code(a.org.organization_id, "employee-1", ttl_hours=1)
    cs = env.control_settings
    assert env.store.revoke_code(info.enrollment_id)
    with pytest.raises(psycopg.errors.CheckViolation):  # no reactivation
        _exec(cs, "UPDATE device_enrollment_codes SET status = 'ISSUED', revoked_at = NULL WHERE enrollment_id = %s",
              (info.enrollment_id,))
    with pytest.raises(psycopg.errors.CheckViolation):  # raw code instead of a hash
        _exec(cs, "INSERT INTO device_enrollment_codes (enrollment_id, code_hash, organization_id, employee_id, "
              "expires_at) VALUES (gen_random_uuid(), 'ZZE-RAW-CODE', %s, 'employee-1', now() + interval '1 hour')",
              (a.org.organization_id,))
    with pytest.raises(psycopg.errors.CheckViolation):  # identity can't be re-pointed
        _exec(cs, "UPDATE device_enrollment_codes SET organization_id = %s WHERE enrollment_id = %s",
              (env.tenants["b"].org.organization_id, info.enrollment_id))


# ─── enrollment end to end ─────────────────────────────────────────────────


def test_enrollment_a_b_isolation_and_sync(env):
    from deskmate.zaza.sync.protocol import DEVICE_ID_HEADER, DEVICE_PATH
    from deskmate.zaza_server.auth import hash_token
    from deskmate.zaza_server.tenancy.enrollment import normalize_code

    codes, tokens = {}, {}
    for name, t in env.tenants.items():
        _, codes[name] = env.store.create_code(t.org.organization_id, "employee-1", ttl_hours=24)
    for name in ("a", "b"):
        r = _enroll(env, codes[name])
        assert r.status_code == 200, r.text
        tokens[name] = r.json()["device_token"]
    for name, other in (("a", "b"), ("b", "a")):
        t, o = env.tenants[name], env.tenants[other]
        rows = _exec(t.settings, "SELECT token_hash, status FROM device_tokens")
        assert rows == [(hash_token(tokens[name]), "ACTIVE")]
        assert _exec(o.settings, "SELECT count(*) FROM device_tokens WHERE token_hash = %s",
                     (hash_token(tokens[name]),))[0][0] == 0
        me = env.client.get(DEVICE_PATH, headers={"Authorization": f"Bearer {tokens[name]}", DEVICE_ID_HEADER: "device-1"})
        assert me.status_code == 200
    dump = "\n".join(str(r) for tbl in ("device_enrollment_codes", "device_credential_routes", "control_audit_logs")
                     for r in _exec(env.control_settings, sql.SQL("SELECT * FROM {}").format(sql.Identifier(tbl))))
    for name in ("a", "b"):
        assert normalize_code(codes[name]) not in dump and codes[name] not in dump
        assert tokens[name] not in dump and hash_token(tokens[name]) not in dump
    assert {r[0] for r in _exec(env.control_settings, "SELECT status FROM device_enrollment_codes")} == {"COMPLETED"}
    assert _exec(env.control_settings, "SELECT count(*) FROM device_credential_routes")[0][0] == 2


def test_concurrent_same_code_on_postgres(env):
    a = env.tenants["a"]
    from deskmate.zaza_server.tenancy.enrollment import EnrollmentSettings

    # raise the per-code limit so all 16 reach the claim race (the limiter is tested elsewhere)
    env.service.settings = EnrollmentSettings(code_max_attempts=100, global_max_attempts=1000)
    _, code = env.store.create_code(a.org.organization_id, "employee-1", ttl_hours=1)
    results, lock = [], threading.Lock()
    barrier = threading.Barrier(16)

    def go(i: int) -> None:
        barrier.wait()
        r = _enroll(env, code, device=f"device-{i}")
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(120)
    assert results.count(200) == 1 and results.count(401) == 15, results
    assert _exec(a.settings, "SELECT count(*) FROM device_tokens")[0][0] == 1
    assert _exec(a.settings, "SELECT count(*) FROM devices")[0][0] == 1
    assert _exec(env.control_settings, "SELECT count(*) FROM device_credential_routes")[0][0] == 1


def test_interrupted_enrollment_is_reconciled_on_postgres(env):
    a = env.tenants["a"]
    _, code = env.store.create_code(a.org.organization_id, "employee-1", ttl_hours=1)

    def crash(step: str) -> None:
        if step == "token_issued":
            raise RuntimeError("injected crash")

    env.service._fault = crash
    assert _enroll(env, code).status_code == 500
    env.service._fault = lambda step: None
    assert _exec(env.control_settings, "SELECT status FROM device_enrollment_codes")[0][0] == "CLAIMED"
    assert _exec(env.control_settings, "SELECT count(*) FROM device_credential_routes")[0][0] == 0
    assert _exec(a.settings, "SELECT status FROM device_tokens")[0][0] == "ACTIVE"  # exists but unroutable
    _exec(env.control_settings, "UPDATE device_enrollment_codes SET claimed_at = now() - interval '1 hour'")
    result = env.service.reconcile()
    assert (result.interrupted, result.tokens_revoked) == (1, 1)
    assert _exec(env.control_settings, "SELECT status, failure_reason FROM device_enrollment_codes")[0] == (
        "FAILED", "interrupted")
    assert _exec(a.settings, "SELECT status FROM device_tokens")[0][0] == "REVOKED"


def test_route_failure_is_compensated_on_postgres(env):
    a = env.tenants["a"]
    _, code = env.store.create_code(a.org.organization_id, "employee-1", ttl_hours=1)
    original = env.store.complete
    env.store.complete = lambda claim, route_hash: (_ for _ in ()).throw(psycopg.OperationalError("control down"))
    try:
        assert _enroll(env, code).status_code == 503
    finally:
        env.store.complete = original
    assert _exec(a.settings, "SELECT status FROM device_tokens")[0][0] == "REVOKED"
    assert _exec(env.control_settings, "SELECT status, failure_reason FROM device_enrollment_codes")[0] == (
        "FAILED", "route_failed")
