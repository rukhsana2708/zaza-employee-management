"""Phase 10F-B on real PostgreSQL (opt-in: ZAZA_TEST_POSTGRES_URL).

A disposable control database migrated to c0002, two tenant databases (A,
B) migrated to the tenant head, the REAL stores (PostgresAuthStore,
PostgresDeviceRoutes, PostgresControlRepository), the real resolver, tenant
connection manager and create_saas_app. Every database it creates is dropped.
"""

from __future__ import annotations

import dataclasses
import secrets
import threading
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from .conftest import pg_test_settings

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("argon2")
from fastapi.testclient import TestClient  # noqa: E402
from psycopg import sql  # noqa: E402

PW = "pg test passphrase 9"  # test-only value
CONTROL_TABLES = {"organizations", "users", "organization_memberships", "tenant_databases", "control_audit_logs",
                  "zaza_control_version", "user_password_credentials", "user_sessions", "login_attempt_counters",
                  "device_credential_routes"}
FORBIDDEN = {"employees", "devices", "device_tokens", "activity_periods", "work_sessions", "idle_periods",
             "application_usage_daily", "daily_summaries", "weekly_summaries", "monthly_summaries", "audit_logs",
             "manager_users", "manager_sessions", "tasks", "messages", "attachments", "alembic_version"}


def _admin():
    settings = dataclasses.replace(pg_test_settings("public"), schema=None)
    with psycopg.connect(**settings.connect_kwargs()) as conn:
        if not conn.execute("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()[0]:
            pytest.skip("the test role may not create databases")
    return settings


def _exec(settings, statement, params=()):  # noqa: ANN001, ANN202
    with psycopg.connect(**settings.connect_kwargs(), autocommit=True) as conn:
        cur = conn.execute(statement, params)
        return cur.fetchall() if cur.description else None


@pytest.fixture
def saas():
    from deskmate.zaza_server.auth import register_device
    from deskmate.zaza_server.postgres import migrate as tenant_migrate
    from deskmate.zaza_server.tenancy.connections import TenantPoolLimits, TenantRepositoryManager
    from deskmate.zaza_server.tenancy.control import migrate as control_migrate
    from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository
    from deskmate.zaza_server.tenancy.device_routes import PostgresDeviceRoutes
    from deskmate.zaza_server.tenancy.resolver import TenantResolver
    from deskmate.zaza_server.tenancy.saas_auth import PostgresAuthStore, SaasAuthService, SaasAuthSettings
    from deskmate.zaza_server.tenancy.saas_sync import SaasRuntime

    admin = _admin()
    control_db = f"zaza_test_control_{secrets.token_hex(4)}"
    created, closers = [], []
    try:
        _exec(admin, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(control_db)))
        created.append(control_db)
        control_settings = dataclasses.replace(admin, dbname=control_db, pool_min=0, pool_max=2)
        control_migrate.upgrade(control_settings)
        control = PostgresControlRepository(control_settings, actor_type="CLI", actor_id="pytest-10fb")
        closers.append(control)
        tenants = {}
        for name in ("a", "b"):
            org = control.create_organization(f"tenant-{name}", f"Tenant {name.upper()}")
            descriptor = control.assign_tenant_database(org.organization_id, "asia-south1")
            _exec(admin, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(descriptor.database_name)))
            created.append(descriptor.database_name)
            tenant_settings = dataclasses.replace(admin, dbname=descriptor.database_name)
            tenant_migrate.upgrade(tenant_settings)
            control.mark_tenant_database(org.organization_id, "READY", tenant_migrate.head_revision())
            tenants[name] = SimpleNamespace(org=org, db=descriptor.database_name, settings=tenant_settings)
        manager = TenantRepositoryManager(dataclasses.replace(admin, dbname="unused"), TenantPoolLimits(cache_max=1))
        closers.append(manager)
        routes = PostgresDeviceRoutes(control)
        auth = SaasAuthService(PostgresAuthStore(control), SaasAuthSettings(cookie_secure=False, identity_max_attempts=5,
                                                                            global_max_attempts=200))
        runtime = SaasRuntime(control=control, resolver=TenantResolver(control), manager=manager, routes=routes,
                              auth=auth)
        # same employee and device ids in BOTH tenants, with their own tokens and routes
        for name, t in tenants.items():
            from deskmate.zaza_server.postgres import PostgresRepository

            repo = PostgresRepository(t.settings, actor_type="CLI", actor_id="pytest")
            try:
                repo.add_employee("employee-1", f"Employee of {name.upper()}", role="ADMIN")
                t.token = register_device(repo, "device-1", "employee-1").token
            finally:
                repo.close()
            routes.create_route(t.org.organization_id, "device-1", t.token)
        yield SimpleNamespace(admin=admin, control=control, control_db=control_db, control_settings=control_settings,
                              runtime=runtime, tenants=tenants, created=created)
    finally:
        for closer in reversed(closers):
            closer.close()
        for name in reversed(created):
            _exec(admin, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        left = _exec(admin, "SELECT datname FROM pg_database WHERE datname = ANY(%s)", (created,))
        assert left == [], "disposable databases were not removed"


def _h(token, device="device-1"):  # noqa: ANN001, ANN202
    from deskmate.zaza.sync.protocol import DEVICE_ID_HEADER

    return {"Authorization": f"Bearer {token}", DEVICE_ID_HEADER: device}


def _batch(label, n=2):  # noqa: ANN001, ANN202
    from .test_postgres_integration import usage

    records = []
    for i in range(n):
        raw = usage(app_name=f"{label}-{i}.exe")
        raw["employee_id"] = "employee-1"
        records.append(raw)
    return ({"batch_id": str(uuid.uuid4()), "device_id": "device-1", "agent_version": "test",
             "sent_at": records[0]["updated_at"], "records": records}, {r["record_id"] for r in records})


def _tenant_record_ids(settings) -> set[str]:  # noqa: ANN001
    rows = _exec(settings, "SELECT usage_id::text FROM application_usage_daily")
    return {r[0] for r in rows}


# ─── schema ────────────────────────────────────────────────────────────────


def test_c0002_head_and_control_data_boundary(saas):
    from deskmate.zaza_server.tenancy.control import migrate as control_migrate

    with psycopg.connect(**saas.control_settings.connect_kwargs()) as conn:
        assert control_migrate.schema_status(conn).current == "c0002_saas_auth_routing"
    tables = {r[0] for r in _exec(saas.control_settings,
                                  "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert tables == CONTROL_TABLES and not tables & FORBIDDEN


def test_c0002_constraints(saas):
    org = saas.tenants["a"].org.organization_id
    user = saas.control.create_user("schema@example.com", "Schema")
    for bad_hash in ("plaintext-password", "$argon2i$v=19$...", "$2b$12$bcrypt"):
        with pytest.raises(psycopg.errors.CheckViolation):
            _exec(saas.control_settings, "INSERT INTO user_password_credentials (user_id, password_hash) "
                  "VALUES (%s, %s)", (user.user_id, bad_hash))
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec(saas.control_settings, "INSERT INTO user_sessions (session_id, user_id, token_hash, expires_at) "
              "VALUES (%s, %s, 'raw-token-not-a-hash', now() + interval '1 hour')", (uuid.uuid4(), user.user_id))
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec(saas.control_settings, "INSERT INTO device_credential_routes (route_hash, organization_id, device_id) "
              "VALUES ('zzd_raw_token', %s, 'device-9')", (org,))
    # one hash routes to ONE organization: a second route for the same credential is refused
    from deskmate.zaza_server.tenancy.domain import TenancyError

    with pytest.raises(TenancyError, match="already routed"):
        saas.runtime.routes.create_route(saas.tenants["b"].org.organization_id, "device-1", saas.tenants["a"].token)
    # a route can't be re-pointed at another organization
    from deskmate.zaza_server.tenancy.device_routes import routing_hash

    with pytest.raises(psycopg.errors.CheckViolation):
        _exec(saas.control_settings, "UPDATE device_credential_routes SET organization_id = %s WHERE route_hash = %s",
              (saas.tenants["b"].org.organization_id, routing_hash(saas.tenants["a"].token)))
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec(saas.control_settings, "UPDATE device_credential_routes SET status = 'REVOKED' WHERE route_hash = %s",
              (routing_hash(saas.tenants["a"].token),))  # revoked_at must be set with the status


def test_no_raw_device_token_or_tenant_hash_in_the_control_database(saas):
    from deskmate.zaza_server.auth import hash_token
    from deskmate.zaza_server.tenancy.device_routes import routing_hash

    dump = []
    for table in sorted(CONTROL_TABLES - {"zaza_control_version"}):
        dump += [str(r) for r in _exec(saas.control_settings, sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))]
    text = "\n".join(dump)
    for t in saas.tenants.values():
        assert t.token not in text and hash_token(t.token) not in text   # neither raw nor the tenant's hash
        assert routing_hash(t.token) in text                             # only the domain-separated route hash


# ─── device routing end to end ─────────────────────────────────────────────


def test_same_ids_route_by_credential_and_stay_isolated(saas):
    from deskmate.zaza.sync.protocol import BATCH_PATH, DEVICE_PATH
    from deskmate.zaza_server.app import create_saas_app

    client = TestClient(create_saas_app(saas.runtime))
    written = {}
    for name, t in saas.tenants.items():
        assert client.get(DEVICE_PATH, headers=_h(t.token)).json()["employee_id"] == "employee-1"
        batch, ids = _batch(f"tenant-{name}")
        r = client.post(BATCH_PATH, json=batch, headers=_h(t.token))
        assert r.status_code == 200 and all(x["status"] == "accepted" for x in r.json()["results"])
        written[name] = ids
    a, b = saas.tenants["a"], saas.tenants["b"]
    assert _tenant_record_ids(a.settings) == written["a"] and _tenant_record_ids(b.settings) == written["b"]
    assert not written["a"] & written["b"]


def test_wrong_routes_fail_safely_on_real_databases(saas):
    from deskmate.zaza.sync.protocol import DEVICE_PATH
    from deskmate.zaza_server.app import create_saas_app
    from deskmate.zaza_server.auth import hash_token
    from deskmate.zaza_server.tenancy.device_routes import routing_hash

    client = TestClient(create_saas_app(saas.runtime))
    a, b = saas.tenants["a"], saas.tenants["b"]
    words = (a.db, b.db, str(a.org.organization_id), str(b.org.organization_id), "tenant-a", "tenant-b")

    def expect(r, status, error):  # noqa: ANN001, ANN202
        assert (r.status_code, r.json()["error"]) == (status, error) and not any(w in r.text for w in words)

    expect(client.get(DEVICE_PATH, headers=_h("zzd_" + secrets.token_urlsafe(32))), 401, "invalid credentials")
    expect(client.get(DEVICE_PATH, headers=_h(a.token, "device-2")), 401, "invalid credentials")
    forged = client.get(DEVICE_PATH, headers={**_h(a.token), "X-ZaZa-Organization": b.org.slug,
                                              "X-ZaZa-Database": b.db}, params={"organization_id": str(b.org.organization_id)})
    assert forged.status_code == 200  # still A: the credential decided, the forged fields were ignored
    # a stale route pointing at A for a token that exists only in B: refused in A, never tried in B
    stray = "zzd_" + secrets.token_urlsafe(32)
    _exec(b.settings, "INSERT INTO device_tokens (token_id, device_id, token_hash) VALUES (%s, 'device-1', %s)",
          (str(uuid.uuid4()), hash_token(stray)))
    saas.runtime.routes.create_route(a.org.organization_id, "device-1", stray)
    expect(client.get(DEVICE_PATH, headers=_h(stray)), 401, "invalid credentials")
    # route revoked
    saas.runtime.routes.revoke_route(routing_hash(a.token))
    expect(client.get(DEVICE_PATH, headers=_h(a.token)), 401, "invalid credentials")
    # rotation: new token + new route first, then the old route is revoked (done above)
    from deskmate.zaza_server.postgres import PostgresRepository

    repo = PostgresRepository(a.settings, actor_type="CLI", actor_id="pytest")
    try:
        from deskmate.zaza_server.auth import issue_token

        new = issue_token(repo, "device-1").token
    finally:
        repo.close()
    new_hash = saas.runtime.routes.create_route(a.org.organization_id, "device-1", new)
    assert client.get(DEVICE_PATH, headers=_h(new)).status_code == 200
    assert saas.runtime.routes.revoke_device_routes(a.org.organization_id, "device-1", except_route_hash=new_hash) == 1
    assert client.get(DEVICE_PATH, headers=_h(new)).status_code == 200
    # organization suspended / tenant not READY
    saas.control.set_organization_status(b.org.organization_id, "SUSPENDED")
    expect(client.get(DEVICE_PATH, headers=_h(b.token)), 401, "invalid credentials")
    saas.control.set_organization_status(b.org.organization_id, "ACTIVE")
    saas.control.mark_tenant_database(b.org.organization_id, "ERROR")
    expect(client.get(DEVICE_PATH, headers=_h(b.token)), 401, "invalid credentials")
    saas.control.mark_tenant_database(b.org.organization_id, "READY")
    # token revoked / device disabled INSIDE the tenant
    _exec(b.settings, "UPDATE device_tokens SET status = 'REVOKED', revoked_at = now() WHERE token_hash = %s",
          (hash_token(b.token),))
    expect(client.get(DEVICE_PATH, headers=_h(b.token)), 403, "token revoked")
    audit = {e.action for e in saas.control.list_audit(limit=500)}
    assert {"device_route.create", "device_route.revoke"} <= audit


def test_concurrent_tenants_with_real_pools_and_eviction(saas):
    """cache_max=1: A and B keep evicting each other's pool while 8 threads
    interleave batches. Every record must end in its own tenant."""
    from deskmate.zaza.sync.protocol import BATCH_PATH
    from deskmate.zaza_server.app import create_saas_app

    client = TestClient(create_saas_app(saas.runtime))
    written = {"a": set(), "b": set()}
    lock, errors, retries = threading.Lock(), [], [0]

    def worker(name: str, w: int) -> None:
        for i in range(12):
            # unique per thread: app_usage_daily is unique per (device, date, app) in PostgreSQL
            batch, ids = _batch(f"tenant-{name}-w{w}-{i}", n=3)
            for attempt in range(400):  # like an agent: a 503 (the one pool busy) is retried after a pause
                r = client.post(BATCH_PATH, json=batch, headers=_h(saas.tenants[name].token))
                if r.status_code != 503:
                    break
                with lock:
                    retries[0] += 1
                time.sleep(0.005 * (1 + attempt % 10))
            if r.status_code != 200 or not all(x["status"] == "accepted" for x in r.json()["results"]):
                bad = [x for x in r.json().get("results", []) if x["status"] != "accepted"][:1]
                errors.append(f"{name}: {r.status_code} {bad or r.text[:200]}")
                continue
            with lock:
                written[name] |= ids

    threads = [threading.Thread(target=worker, args=(n, w)) for w, n in enumerate(("a", "b") * 4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(180)
    assert errors == []
    assert len(written["a"]) == len(written["b"]) == 4 * 12 * 3  # every batch landed (after retries)
    assert retries[0] > 0  # the single pool really was contended / evicted
    a, b = _tenant_record_ids(saas.tenants["a"].settings), _tenant_record_ids(saas.tenants["b"].settings)
    assert a == written["a"] and b == written["b"] and not a & b


# ─── SaaS users, sessions, shared limiter ──────────────────────────────────


def test_auth_end_to_end_on_the_control_database(saas):
    auth, control = saas.runtime.auth, saas.control
    user = control.create_user("Owner@Example.com", "Owner")
    version, _ = auth.set_password(user.user_id, PW)
    assert version == 1
    token, ctx = auth.login("owner@example.com", PW)
    stored = _exec(saas.control_settings, "SELECT token_hash FROM user_sessions")
    assert stored and all(token not in r[0] for r in stored)
    assert auth.validate_session(token).user_id == user.user_id
    assert auth.login("owner@example.com", "wrong password 123") is None
    assert auth.login("ghost@example.com", PW) is None
    pw_hash = _exec(saas.control_settings, "SELECT password_hash FROM user_password_credentials")[0][0]
    assert pw_hash.startswith("$argon2id$") and PW not in pw_hash
    # password change revokes; disable revokes effect immediately
    assert auth.set_password(user.user_id, PW + "x") == (2, 1)
    assert auth.validate_session(token) is None
    token2, _ = auth.login("owner@example.com", PW + "x")
    control.set_user_status(user.user_id, "DISABLED")
    assert auth.validate_session(token2) is None and auth.login("owner@example.com", PW + "x") is None
    rows = _exec(saas.control_settings, "SELECT action, details::text FROM control_audit_logs WHERE entity_type = 'user'")
    assert {"user.create", "user.password_set", "user.status_change"} <= {r[0] for r in rows}
    assert not any(PW in r[1] or "argon2" in r[1] or token in r[1] for r in rows)


def test_multi_organization_membership_with_real_resolution(saas):
    from deskmate.zaza_server.tenancy.resolver import TenantAccessDenied

    control, resolver = saas.control, saas.runtime.resolver
    a, b = saas.tenants["a"].org.organization_id, saas.tenants["b"].org.organization_id
    c = control.create_organization("tenant-c", "Tenant C").organization_id
    user = control.create_user("multi@example.com", "Multi")
    control.add_membership(a, user.user_id, "OWNER")
    control.add_membership(b, user.user_id, "VIEWER")
    assert resolver.for_member(user.user_id, a).role.value == "OWNER"
    assert resolver.for_member(user.user_id, b).role.value == "VIEWER"
    with pytest.raises(TenantAccessDenied):
        resolver.for_member(user.user_id, c)
    # employees.role = ADMIN inside tenant B did not make the VIEWER an admin
    assert _exec(saas.tenants["b"].settings, "SELECT role FROM employees")[0][0] == "ADMIN"
    assert resolver.for_member(user.user_id, b).role.value == "VIEWER"


def test_shared_rate_limiter_across_two_instances(saas):
    """Two independent control repositories = two Cloud Run instances with
    separate pools; 24 concurrent attempts on one identity: exactly the
    limit gets a password check."""
    from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository
    from deskmate.zaza_server.tenancy.saas_auth import (
        LoginRateLimited,
        PostgresAuthStore,
        SaasAuthService,
        SaasAuthSettings,
    )

    user = saas.control.create_user("limited@example.com", "Limited")
    saas.runtime.auth.set_password(user.user_id, PW)
    settings = SaasAuthSettings(cookie_secure=False, identity_max_attempts=5, global_max_attempts=500)
    instances = [PostgresControlRepository(saas.control_settings, actor_type="SYSTEM", actor_id="instance")
                 for _ in range(2)]
    try:
        services = [SaasAuthService(PostgresAuthStore(i), settings) for i in instances]
        outcomes, lock = [], threading.Lock()

        def attempt(k: int) -> None:
            try:
                r = "fail" if services[k % 2].login("limited@example.com", "wrong password 123") is None else "ok"
            except LoginRateLimited:
                r = "limited"
            with lock:
                outcomes.append(r)

        threads = [threading.Thread(target=attempt, args=(k,)) for k in range(24)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(120)
        assert outcomes.count("fail") == 5 and outcomes.count("limited") == 19
        keys = _exec(saas.control_settings, "SELECT key_hash FROM login_attempt_counters")
        assert keys and all("limited" not in k[0] and "@" not in k[0] for k in keys)
        # cleanup keeps storage bounded
        store = PostgresAuthStore(instances[0])
        _, removed = store.cleanup(timedelta(days=30), timedelta(0))
        assert removed >= 1 and _exec(saas.control_settings, "SELECT count(*) FROM login_attempt_counters")[0][0] == 0
    finally:
        for i in instances:
            i.close()


def test_saas_app_login_session_and_org_routes_on_postgres(saas):
    from deskmate.zaza_server.app import create_saas_app

    control = saas.control
    user = control.create_user("web@example.com", "Web")
    saas.runtime.auth.set_password(user.user_id, PW)
    control.add_membership(saas.tenants["a"].org.organization_id, user.user_id, "ADMIN")
    client = TestClient(create_saas_app(saas.runtime), base_url="http://localhost")
    login_token = client.get("/app/login").json()["login_token"]
    r = client.post("/app/login", data={"email": "web@example.com", "password": PW, "login_token": login_token})
    assert r.status_code == 200
    assert client.get("/app/session").json()["organizations"] == [{"slug": "tenant-a", "role": "ADMIN"}]
    employees = client.get("/app/orgs/tenant-a/employees").json()["employees"]
    assert employees == [{"employee_id": "employee-1", "display_name": "Employee of A", "is_active": True}]
    assert client.get("/app/orgs/tenant-b/employees").status_code == 404
