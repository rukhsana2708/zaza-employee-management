"""Phase 10G-A: one-time device enrollment without a database — the REAL
EnrollmentService, resolver, tenant connection manager, device routing and
create_saas_app, with an in-memory enrollment store that has the same
atomic-claim/complete semantics as PostgresEnrollmentStore. The PostgreSQL
proof is in test_enrollment_postgres.py."""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from deskmate.zaza.sync.protocol import DEVICE_ID_HEADER, DEVICE_PATH  # noqa: E402
from deskmate.zaza_server.app import create_saas_app  # noqa: E402
from deskmate.zaza_server.auth import hash_token  # noqa: E402
from deskmate.zaza_server.repository import RepositoryUnavailable  # noqa: E402
from deskmate.zaza_server.tenancy import enrollment as E  # noqa: E402
from deskmate.zaza_server.tenancy.context import TenantDatabaseDescriptor  # noqa: E402
from deskmate.zaza_server.tenancy.device_routes import routing_hash  # noqa: E402
from deskmate.zaza_server.tenancy.domain import OrganizationStatus, TenantDatabaseStatus  # noqa: E402

from .test_saas_routing import _world  # noqa: E402

UTC = timezone.utc


class MemoryEnrollmentStore:
    """Same contract as PostgresEnrollmentStore; one lock = the row lock."""

    def __init__(self, routes) -> None:  # noqa: ANN001 — test_saas_routing.Router
        self.routes = routes
        self.lock = threading.Lock()
        self.rows: dict[uuid.UUID, dict] = {}
        self.audit: list[tuple] = []
        self.now = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
        self.fail_complete = False
        self.fail_claim = False

    def create_code(self, organization_id, employee_id, *, ttl_hours, device_display_name=None,  # noqa: ANN001, ANN201
                    allow_existing_device=False):
        code = E.generate_code()
        eid = uuid.uuid4()
        self.rows[eid] = {"enrollment_id": eid, "code_hash": E.code_hash(E.normalize_code(code)),
                          "organization_id": organization_id, "employee_id": employee_id,
                          "device_display_name": device_display_name, "allow_existing_device": allow_existing_device,
                          "status": "ISSUED", "created_at": self.now, "expires_at": self.now + timedelta(hours=ttl_hours),
                          "claimed_at": None, "completed_at": None, "device_id": None, "token_id": None,
                          "route_hash": None, "failure_reason": None}
        self.audit.append(("enrollment.code_create", eid))
        return self._info(self.rows[eid]), code

    @staticmethod
    def _info(r: dict) -> E.EnrollmentCode:
        return E.EnrollmentCode(r["enrollment_id"], r["organization_id"], r["employee_id"], r["status"], r["created_at"],
                                r["expires_at"], r["claimed_at"], r["completed_at"], r["device_id"],
                                r["allow_existing_device"], r["failure_reason"])

    def list_codes(self, organization_id=None):  # noqa: ANN001, ANN201
        return [self._info(r) for r in self.rows.values()
                if organization_id is None or r["organization_id"] == organization_id]

    def revoke_code(self, enrollment_id):  # noqa: ANN001, ANN201
        with self.lock:
            r = self.rows.get(enrollment_id)
            if r and r["status"] == "ISSUED":
                r.update(status="REVOKED")
                return True
            return False

    def claim(self, digest, device_id, token_id):  # noqa: ANN001, ANN201
        if self.fail_claim:
            raise ConnectionError("control database down")
        with self.lock:
            for r in self.rows.values():
                if r["code_hash"] == digest and r["status"] == "ISSUED" and r["expires_at"] > self.now:
                    r.update(status="CLAIMED", claimed_at=self.now, device_id=device_id, token_id=token_id)
                    return E.Claim(r["enrollment_id"], r["organization_id"], r["employee_id"],
                                   r["device_display_name"], r["allow_existing_device"], device_id, token_id)
            return None

    def complete(self, claim, route_hash):  # noqa: ANN001, ANN201
        if self.fail_complete:
            raise ConnectionError("control database down during completion")
        with self.lock:
            r = self.rows[claim.enrollment_id]
            if r["status"] != "CLAIMED":
                return False
            assert route_hash not in self.routes.routes, "route collision"
            r.update(status="COMPLETED", completed_at=self.now, route_hash=route_hash)
            self.routes.routes[route_hash] = claim.organization_id
            self.audit.append(("enrollment.complete", claim.enrollment_id))
            return True

    def fail(self, claim, reason):  # noqa: ANN001, ANN201
        with self.lock:
            r = self.rows[claim.enrollment_id]
            if r["status"] != "CLAIMED":
                return False
            r.update(status="FAILED", failure_reason=reason)
            return True

    def take_stale_claims(self, older_than_seconds):  # noqa: ANN001, ANN201
        with self.lock:
            out = []
            for r in self.rows.values():
                if r["status"] == "CLAIMED" and r["claimed_at"] < self.now - timedelta(seconds=older_than_seconds):
                    r.update(status="FAILED", failure_reason="interrupted")
                    out.append(E.Claim(r["enrollment_id"], r["organization_id"], r["employee_id"],
                                       r["device_display_name"], r["allow_existing_device"], r["device_id"],
                                       r["token_id"]))
            return out

    def expire_codes(self):  # noqa: ANN201
        with self.lock:
            n = 0
            for r in self.rows.values():
                if r["status"] == "ISSUED" and r["expires_at"] <= self.now:
                    r.update(status="EXPIRED")
                    n += 1
            return n


class Counters:
    def __init__(self) -> None:
        self.lock, self.n = threading.Lock(), {}

    def count_attempt(self, bucket, key, window):  # noqa: ANN001, ANN201
        with self.lock:
            self.n[(bucket, key)] = self.n.get((bucket, key), 0) + 1
            return self.n[(bucket, key)], window


def _env(**settings):  # noqa: ANN202
    runtime, t, opened = _world()
    for name in ("a", "b"):  # fresh tenants: the employees exist, no device enrolled yet
        t[name]["repo"]._devices.clear()
        t[name]["repo"]._tokens.clear()
        runtime.routes.routes.clear()
    store = MemoryEnrollmentStore(runtime.routes)
    faults: list[str] = []

    def fault(step: str) -> None:
        if step in faults:
            raise RuntimeError(f"injected crash after {step}")

    runtime.enrollment = E.EnrollmentService(store, runtime.resolver, runtime.manager, Counters(),
                                             E.EnrollmentSettings(**settings), fault=fault)
    client = TestClient(create_saas_app(runtime), raise_server_exceptions=False)
    return SimpleNamespace(runtime=runtime, t=t, store=store, faults=faults, client=client,
                           service=runtime.enrollment, opened=opened)


def _code(env, name="a", employee="employee-1", **kw):  # noqa: ANN001, ANN202
    return env.store.create_code(env.t[name]["org"], employee, ttl_hours=72, **kw)


def _enroll(env, code, device="device-1", **extra):  # noqa: ANN001, ANN202
    return env.client.post("/api/v1/enroll", json={"enrollment_code": code, "device_id": device, **extra})


def _sync_ok(env, token, device="device-1"):  # noqa: ANN001, ANN202
    return env.client.get(DEVICE_PATH, headers={"Authorization": f"Bearer {token}", DEVICE_ID_HEADER: device})


# ─── codes ─────────────────────────────────────────────────────────────────


def test_codes_have_160_bits_and_only_a_domain_separated_hash_is_kept():
    codes = {E.generate_code() for _ in range(200)}
    assert len(codes) == 200
    code = next(iter(codes))
    normalized = E.normalize_code(code)
    assert len(normalized) == 32 and set(normalized) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")  # 32*5 = 160 bits
    assert E.normalize_code(code.lower().replace("-", " ")) == normalized
    digest = E.code_hash(normalized)
    assert digest != hashlib.sha256(normalized.encode()).hexdigest() and len(digest) == 64
    for bad in ("", "123456", "ZZE-0000", "x" * 80, None, 12345, code + "A"):
        assert E.normalize_code(bad) is None


def test_listing_never_contains_the_code_or_hash():
    env = _env()
    _, code = _code(env)
    info = env.store.list_codes()[0]
    text = repr(info)
    assert E.normalize_code(code) not in text and env.store.rows[info.enrollment_id]["code_hash"] not in text
    assert not {"code", "code_hash"} & set(E.EnrollmentCode.__dataclass_fields__)


# ─── success, isolation ────────────────────────────────────────────────────


def test_enrollment_issues_one_token_routes_it_and_it_syncs_only_in_its_tenant():
    env = _env()
    _, code_a = _code(env, "a")
    _, code_b = _code(env, "b")
    ra, rb = _enroll(env, code_a), _enroll(env, code_b)
    assert ra.status_code == rb.status_code == 200
    body = ra.json()
    assert set(body) == {"device_id", "employee_id", "device_token", "api_version"}
    assert body["device_id"] == "device-1" and body["employee_id"] == "employee-1"
    assert ra.headers["cache-control"] == "no-store"
    ta, tb = ra.json()["device_token"], rb.json()["device_token"]
    for tok, name, other in ((ta, "a", "b"), (tb, "b", "a")):
        # tenant stores only SHA-256; control routes only the domain-separated hash; neither stores the token
        stored = env.t[name]["repo"].find_token(hash_token(tok))
        assert stored and stored.status == "ACTIVE" and env.t[other]["repo"].find_token(hash_token(tok)) is None
        assert env.runtime.routes.routes[routing_hash(tok)] == env.t[name]["org"]
        assert tok not in repr(env.store.rows) and tok not in repr(env.runtime.routes.routes)
        assert _sync_ok(env, tok).status_code == 200
    # same device id in both tenants, different employees' devices, isolated
    assert env.t["a"]["repo"].get_device("device-1") and env.t["b"]["repo"].get_device("device-1")
    assert {r["status"] for r in env.store.rows.values()} == {"COMPLETED"}


def test_client_cannot_redirect_enrollment():
    env = _env()
    _, code_a = _code(env, "a")
    for extra in ({"organization_id": str(env.t["b"]["org"])}, {"tenant": "tenant-b"}, {"employee_id": "boss"},
                  {"database": env.t["b"]["db"]}, {"role": "ADMIN"}):
        assert _enroll(env, code_a, **extra).status_code == 422  # unknown fields refused, code untouched
    r = env.client.post("/api/v1/enroll", params={"enrollment_code": code_a, "organization_id": str(env.t["b"]["org"])},
                        headers={"X-ZaZa-Organization": "tenant-b"},
                        json={"enrollment_code": code_a, "device_id": "device-1"})
    assert r.status_code == 200
    assert env.t["a"]["repo"].get_device("device-1") and env.t["b"]["repo"].get_device("device-1") is None
    # a code in the query string alone is never read
    _, code2 = _code(env, "a", employee="employee-1", allow_existing_device=True)
    assert env.client.post("/api/v1/enroll", params={"enrollment_code": code2},
                           json={"device_id": "device-9"}).status_code == 422


# ─── one-time, concurrency ─────────────────────────────────────────────────


def test_a_code_is_single_use():
    env = _env()
    _, code = _code(env)
    first = _enroll(env, code)
    second = _enroll(env, code, device="device-2")
    assert first.status_code == 200 and second.status_code == 401
    assert second.json() == {"error": E.GENERIC_REFUSAL}
    assert len(env.runtime.routes.routes) == 1


def test_concurrent_use_of_one_code_yields_exactly_one_credential():
    env = _env(global_max_attempts=1000, code_max_attempts=100)
    _, code = _code(env)
    results: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(24)

    def go(i: int) -> None:
        barrier.wait()
        r = _enroll(env, code, device=f"device-{i}")
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(24)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    assert results.count(200) == 1 and results.count(401) == 23
    tokens = [tk for tk in env.t["a"]["repo"]._tokens.values()]
    assert len(tokens) == 1 and len(env.runtime.routes.routes) == 1
    assert len(env.t["a"]["repo"].list_devices()) == 1  # no orphan devices either


# ─── refusals ──────────────────────────────────────────────────────────────


def test_refusals():
    env = _env()
    a = env.t["a"]
    # unknown / malformed
    assert _enroll(env, E.generate_code()).status_code == 401
    assert _enroll(env, "123456").status_code == 401
    # revoked
    info, code = _code(env)
    assert env.store.revoke_code(info.enrollment_id)
    assert _enroll(env, code).status_code == 401
    # expired
    _, code = _code(env)
    env.store.now += timedelta(hours=73)
    assert _enroll(env, code).status_code == 401
    env.store.now -= timedelta(hours=73)
    # organization suspended
    _, code = _code(env)
    org = env.runtime.control.orgs[a["org"]]
    env.runtime.control.orgs[a["org"]] = replace(org, status=OrganizationStatus.SUSPENDED)
    r = _enroll(env, code)
    assert r.status_code == 403 and "zaza_t_" not in r.text and "tenant-a" not in r.text
    env.runtime.control.orgs[a["org"]] = org
    # tenant not READY
    _, code = _code(env)
    db = env.runtime.control.dbs[a["org"]]
    env.runtime.control.dbs[a["org"]] = TenantDatabaseDescriptor(db.organization_id, db.database_name, db.region,
                                                                 TenantDatabaseStatus.PROVISIONING)
    assert _enroll(env, code).status_code == 403
    env.runtime.control.dbs[a["org"]] = db
    # employee unknown / inactive
    _, code = _code(env, employee="employee-404")
    assert _enroll(env, code).status_code == 403
    a["repo"].add_employee("employee-2", "Left the company")
    a["repo"]._employees["employee-2"] = replace(a["repo"].get_employee("employee-2"), is_active=False)
    _, code = _code(env, employee="employee-2")
    assert _enroll(env, code).status_code == 403
    # invalid device ids
    for bad in ("", "a b", "x" * 129, "../etc", "dev;drop", "dev\n1"):
        _, code = _code(env)
        assert _enroll(env, code, device=bad).status_code == 422
    assert not env.runtime.routes.routes and not a["repo"]._tokens
    reasons = {r["failure_reason"] for r in env.store.rows.values() if r["status"] == "FAILED"}
    assert reasons == {"tenant_unavailable", "employee_unavailable"}


def test_existing_device_and_reenrollment_policy():
    env = _env()
    a = env.t["a"]
    _, code = _code(env)
    first = _enroll(env, code).json()["device_token"]
    # a plain code for an already enrolled device: refused (409), nothing issued
    _, code = _code(env)
    r = _enroll(env, code)
    assert r.status_code == 409 and r.json() == {"error": E.DEVICE_EXISTS}
    # explicit re-enrollment code: an ADDITIONAL token; the old one stays until the operator revokes it
    _, code = _code(env, allow_existing_device=True)
    second = _enroll(env, code).json()["device_token"]
    assert _sync_ok(env, first).status_code == 200 and _sync_ok(env, second).status_code == 200
    # re-enrollment for ANOTHER employee's device: refused
    a["repo"].add_employee("employee-3", "Someone else")
    _, code = _code(env, employee="employee-3", allow_existing_device=True)
    assert _enroll(env, code).status_code == 409
    # disabled device: never silently re-enabled
    a["repo"].set_device_status("device-1", "DISABLED")
    _, code = _code(env, allow_existing_device=True)
    assert _enroll(env, code).status_code == 403 and a["repo"].get_device("device-1").status == "DISABLED"


# ─── failure injection / saga ──────────────────────────────────────────────


@pytest.mark.parametrize("step", ["claimed", "device_ready", "token_issued"])
def test_crash_before_completion_leaves_no_usable_credential_and_reconcile_cleans_up(step):
    env = _env()
    env.faults.append(step)
    _, code = _code(env)
    r = _enroll(env, code)
    assert r.status_code == 500  # crash-equivalent: the process died mid-enrollment
    row = next(iter(env.store.rows.values()))
    assert row["status"] == "CLAIMED" and not env.runtime.routes.routes  # never routable
    tokens = list(env.t["a"]["repo"]._tokens.values())
    if step == "token_issued":
        assert len(tokens) == 1 and tokens[0].status == "ACTIVE" and tokens[0].token_id == str(row["token_id"])
    # the code is not reusable while/after it is claimed
    assert _enroll(env, code).status_code == 401
    # reconcile after the delay: FAILED + the issued token (if any) revoked
    env.store.now += timedelta(seconds=env.service.settings.reconcile_after_seconds + 1)
    result = env.service.reconcile()
    assert result.interrupted == 1 and result.tokens_revoked == (1 if step == "token_issued" else 0)
    assert row["status"] == "FAILED" and row["failure_reason"] == "interrupted"
    assert all(t.status == "REVOKED" for t in env.t["a"]["repo"]._tokens.values())


def test_route_failure_revokes_the_token_immediately():
    env = _env()
    env.store.fail_complete = True
    _, code = _code(env)
    r = _enroll(env, code)
    assert r.status_code == 503
    tokens = list(env.t["a"]["repo"]._tokens.values())
    assert len(tokens) == 1 and tokens[0].status == "REVOKED"
    assert not env.runtime.routes.routes
    assert next(iter(env.store.rows.values()))["failure_reason"] == "route_failed"


def test_control_database_down_at_claim_claims_nothing():
    env = _env()
    env.store.fail_claim = True
    _, code = _code(env)
    assert _enroll(env, code).status_code == 503
    env.store.fail_claim = False
    assert next(iter(env.store.rows.values()))["status"] == "ISSUED"  # untouched: usable once the DB is back
    assert _enroll(env, code).status_code == 200


def test_tenant_database_unavailable_fails_safely_without_falling_through():
    env = _env()
    _, code = _code(env)
    orig = env.t["a"]["repo"].get_employee

    def down(*a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        raise RepositoryUnavailable("tenant A down")

    env.t["a"]["repo"].get_employee = down
    r = _enroll(env, code)
    env.t["a"]["repo"].get_employee = orig
    assert r.status_code == 503
    assert next(iter(env.store.rows.values()))["failure_reason"] == "tenant_unavailable"
    assert env.t["b"]["repo"].get_device("device-1") is None  # never tried in B


def test_completion_losing_to_reconcile_revokes_the_token():
    env = _env()
    _, code = _code(env)

    def reconcile_wins(step: str) -> None:
        if step == "token_issued":  # reconcile runs between token issue and completion
            for r in env.store.rows.values():
                r.update(status="FAILED", failure_reason="interrupted")

    env.service._fault = reconcile_wins
    assert _enroll(env, code).status_code == 401
    assert all(t.status == "REVOKED" for t in env.t["a"]["repo"]._tokens.values())
    assert not env.runtime.routes.routes


# ─── limits and HTTP hygiene ───────────────────────────────────────────────


def test_rate_limits_per_code_and_global():
    env = _env(code_max_attempts=3, global_max_attempts=10)
    code = E.generate_code()
    for _ in range(3):
        assert _enroll(env, code).status_code == 401
    r = _enroll(env, code)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    for _ in range(6):
        _enroll(env, E.generate_code())
    assert _enroll(env, E.generate_code()).status_code == 429  # global bucket


def test_request_size_and_shape(caplog):
    env = _env()
    big = {"enrollment_code": "A" * 60, "device_id": "d", "device_name": "x" * 5000}
    assert env.client.post("/api/v1/enroll", json=big).status_code == 413
    assert env.client.post("/api/v1/enroll", content=b"not json",
                           headers={"content-type": "application/json"}).status_code == 422
    assert env.client.post("/api/v1/enroll", json={"enrollment_code": 123, "device_id": "d"}).status_code == 422
    _, code = _code(env)
    import logging

    caplog.set_level(logging.DEBUG)
    token = _enroll(env, code).json()["device_token"]
    assert token not in caplog.text and E.normalize_code(code) not in caplog.text


def test_enroll_route_exists_only_in_the_saas_app():
    from deskmate.zaza_server.app import create_app
    from deskmate.zaza_server.repository import InMemoryRepository

    paths = {getattr(r, "path", "") for r in create_app(InMemoryRepository()).routes}
    assert "/api/v1/enroll" not in paths
