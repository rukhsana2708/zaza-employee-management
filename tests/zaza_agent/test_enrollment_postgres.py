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
import time
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
        # short pool wait: the outage tests make a tenant unreachable on purpose
        runtime_base = dataclasses.replace(admin, user=role, password=role_pw, dbname="unused", pool_timeout=4.0)
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


def _crash_after_token(env, device="device-1"):  # noqa: ANN001, ANN202
    a = env.tenants["a"]
    info, code = env.store.create_code(a.org.organization_id, "employee-1", ttl_hours=1)

    def crash(step: str) -> None:
        if step == "token_issued":
            raise RuntimeError("injected crash")

    env.service._fault = crash
    try:
        assert _enroll(env, code, device=device).status_code == 500
    finally:
        env.service._fault = lambda step: None
    return info.enrollment_id


def _state(env, eid):  # noqa: ANN001, ANN202
    return _exec(env.control_settings, "SELECT status, failure_reason, token_id FROM device_enrollment_codes "
                 "WHERE enrollment_id = %s", (eid,))[0]


def _token_status(env, token_id, name="a"):  # noqa: ANN001, ANN202
    rows = _exec(env.tenants[name].settings, "SELECT status FROM device_tokens WHERE token_id = %s", (token_id,))
    return rows[0][0] if rows else None


def _age_claims(env):  # noqa: ANN001
    _exec(env.control_settings, "UPDATE device_enrollment_codes SET claimed_at = now() - interval '1 hour' "
          "WHERE status = 'CLAIMED'")


def _routes(env):  # noqa: ANN001, ANN202
    return _exec(env.control_settings, "SELECT count(*) FROM device_credential_routes")[0][0]


def _tenant_offline(env, name="a"):  # noqa: ANN001
    """A REAL outage of tenant ``name``: no new connections, existing ones killed."""
    db = env.tenants[name].db
    _exec(env.admin, sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(sql.Identifier(db)))
    _exec(env.admin, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s "
          "AND pid <> pg_backend_pid()", (db,))


def _tenant_online(env, name="a"):  # noqa: ANN001
    _exec(env.admin, sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(
        sql.Identifier(env.tenants[name].db)))


def test_interrupted_enrollment_is_reconciled_on_postgres(env):
    eid = _crash_after_token(env)
    status, _, token_id = _state(env, eid)
    assert status == "CLAIMED" and _routes(env) == 0
    assert _token_status(env, token_id) == "ACTIVE"  # exists but unroutable
    _age_claims(env)
    result = env.service.reconcile()
    assert (result.interrupted, result.cleanup_completed, result.tokens_revoked, result.cleanup_pending) == (1, 1, 1, 0)
    assert _state(env, eid)[:2] == ("FAILED", "interrupted")
    assert _token_status(env, token_id) == "REVOKED"


def test_tenant_outage_during_cleanup_is_retried_until_it_succeeds_on_postgres(env):
    """The 10G-A repair on real databases: the tenant goes down BETWEEN the
    CLEANUP_PENDING transition and the revocation, stays down for three runs,
    and the fourth run (tenant back) finishes the cleanup."""
    eid = _crash_after_token(env)
    token_id = _state(env, eid)[2]
    _age_claims(env)
    went_down = []

    def outage(step: str) -> None:
        if step == "cleanup_started" and not went_down:
            # the claim is already CLEANUP_PENDING (committed) when the tenant disappears
            assert _state(env, eid)[0] == "CLEANUP_PENDING"
            _tenant_offline(env)
            went_down.append(True)

    env.service._fault = outage
    try:
        for run in range(3):
            r = env.service.reconcile()
            assert (r.interrupted, r.cleanup_completed, r.cleanup_pending, r.tenant_unreachable) == (
                int(run == 0), 0, 1, 1), r
            assert _state(env, eid)[:2] == ("CLEANUP_PENDING", "interrupted")
            assert _routes(env) == 0
    finally:
        env.service._fault = lambda step: None
        _tenant_online(env)
    assert _token_status(env, token_id) == "ACTIVE"  # still there: nothing pretended it was cleaned up
    r = env.service.reconcile()
    assert (r.cleanup_completed, r.tokens_revoked, r.cleanup_pending, r.tenant_unreachable) == (1, 1, 0, 0)
    assert _state(env, eid)[:2] == ("FAILED", "interrupted") and _token_status(env, token_id) == "REVOKED"
    again = env.service.reconcile()  # idempotent
    assert (again.interrupted, again.cleanup_completed, again.tokens_revoked, again.cleanup_pending) == (0, 0, 0, 0)
    assert _state(env, eid)[:2] == ("FAILED", "interrupted") and _token_status(env, token_id) == "REVOKED"
    assert _routes(env) == 0


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


def test_route_failure_with_tenant_down_for_revocation_stays_cleanup_pending_on_postgres(env):
    a = env.tenants["a"]
    info, code = env.store.create_code(a.org.organization_id, "employee-1", ttl_hours=1)

    def completion_fails(claim, route_hash):  # noqa: ANN001, ANN202
        _tenant_offline(env)  # ... and the tenant is gone too when compensation tries to revoke
        raise psycopg.OperationalError("control down")

    original = env.store.complete
    env.store.complete = completion_fails
    try:
        assert _enroll(env, code).status_code == 503
    finally:
        env.store.complete = original
        _tenant_online(env)
    status, reason, token_id = _state(env, info.enrollment_id)
    assert (status, reason) == ("CLEANUP_PENDING", "route_failed") and _routes(env) == 0
    assert _token_status(env, token_id) == "ACTIVE"  # revocation failed: NOT recorded as cleaned up
    r = env.service.reconcile()
    assert (r.interrupted, r.cleanup_completed, r.tokens_revoked, r.cleanup_pending) == (0, 1, 1, 0)
    assert _state(env, info.enrollment_id)[:2] == ("FAILED", "route_failed")
    assert _token_status(env, token_id) == "REVOKED" and _routes(env) == 0


def test_completion_and_reconciliation_race_on_postgres(env):
    import random

    a = env.tenants["a"]
    outcomes = []
    for i in range(12):
        info, code = env.store.create_code(a.org.organization_id, "employee-1", ttl_hours=1)
        go = threading.Barrier(2)
        late = ("enroll", "reconcile")[i % 2]
        result = {}

        def at_token(step: str) -> None:
            if step == "token_issued":
                _age_claims(env)  # the claim is stale: reconciliation may take it from now on
                go.wait(30)
                if late == "enroll":
                    time.sleep(random.uniform(0.02, 0.05))

        def enroll() -> None:
            result["enroll"] = _enroll(env, code, device=f"race-{i}").status_code

        def reconcile() -> None:
            go.wait(30)
            if late == "reconcile":
                time.sleep(random.uniform(0.02, 0.05))
            result["reconcile"] = env.service.reconcile()

        env.service._fault = at_token
        threads = [threading.Thread(target=enroll), threading.Thread(target=reconcile)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(120)
        env.service._fault = lambda step: None
        env.service.reconcile()  # finish any cleanup still pending
        status, _, token_id = _state(env, info.enrollment_id)
        routes = _exec(env.control_settings, "SELECT count(*) FROM device_credential_routes WHERE device_id = %s",
                       (f"race-{i}",))[0][0]
        token = _token_status(env, token_id)
        if result["enroll"] == 200:  # A: completion won
            assert (status, routes, token) == ("COMPLETED", 1, "ACTIVE")
        else:  # B: reconciliation won
            assert result["enroll"] == 401
            assert (status, routes, token) == ("FAILED", 0, "REVOKED")
        outcomes.append(result["enroll"])
    assert set(outcomes) == {200, 401}, outcomes  # both orders really happened
    with pytest.raises(psycopg.errors.CheckViolation):  # and a FAILED race loser can never be revived
        _exec(env.control_settings, "UPDATE device_enrollment_codes SET status = 'COMPLETED' WHERE status = 'FAILED'")


def test_device_row_of_an_interrupted_enrollment_is_recovered_on_postgres(env):
    a = env.tenants["a"]
    eid = _crash_after_token(env, device="device-7")
    _age_claims(env)
    assert env.service.reconcile().cleanup_completed == 1
    assert _state(env, eid)[0] == "FAILED"
    _, code = env.store.create_code(a.org.organization_id, "employee-1", ttl_hours=1)
    r = _enroll(env, code, device="device-7")  # plain code, same employee and device: recovered
    assert r.status_code == 200, r.text
    assert _exec(a.settings, "SELECT count(*) FROM devices WHERE device_id = 'device-7'")[0][0] == 1
    assert sorted(x[0] for x in _exec(a.settings, "SELECT status FROM device_tokens WHERE device_id = 'device-7'")) == [
        "ACTIVE", "REVOKED"]


def test_c0003_cleanup_pending_transitions(env):
    a = env.tenants["a"]
    cs = env.control_settings
    eid = _crash_after_token(env)
    _age_claims(env)
    _tenant_offline(env)
    try:
        env.service.reconcile()
    finally:
        _tenant_online(env)
    assert _state(env, eid)[0] == "CLEANUP_PENDING"
    for bad in ("ISSUED", "CLAIMED", "COMPLETED", "REVOKED", "EXPIRED"):  # only forward, to FAILED
        with pytest.raises(psycopg.errors.CheckViolation):
            _exec(cs, "UPDATE device_enrollment_codes SET status = %s WHERE enrollment_id = %s", (bad, eid))
    with pytest.raises(psycopg.errors.CheckViolation):  # cleanup-pending always says why
        _exec(cs, "UPDATE device_enrollment_codes SET failure_reason = NULL WHERE enrollment_id = %s", (eid,))
    env.service.reconcile()
    assert _state(env, eid)[0] == "FAILED"
    for bad in ("CLEANUP_PENDING", "CLAIMED", "ISSUED", "COMPLETED"):  # FAILED is terminal
        with pytest.raises(psycopg.errors.CheckViolation):
            _exec(cs, "UPDATE device_enrollment_codes SET status = %s WHERE enrollment_id = %s", (bad, eid))
    info, _ = env.store.create_code(a.org.organization_id, "employee-1", ttl_hours=1)
    with pytest.raises(psycopg.errors.CheckViolation):  # an unused code can't jump to FAILED/COMPLETED
        _exec(cs, "UPDATE device_enrollment_codes SET status = 'FAILED', failure_reason = 'x' "
              "WHERE enrollment_id = %s", (info.enrollment_id,))
