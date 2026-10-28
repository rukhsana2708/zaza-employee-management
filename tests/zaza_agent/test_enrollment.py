"""Phase 10G-A: one-time device enrollment without a database — the REAL
EnrollmentService, resolver, tenant connection manager, device routing and
create_saas_app, with an in-memory enrollment store that has the same
atomic-claim/complete semantics as PostgresEnrollmentStore. The PostgreSQL
proof is in test_enrollment_postgres.py."""

from __future__ import annotations

import hashlib
import random
import threading
import time
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
        self.fail_mark = False
        self.fail_finish = False
        self.transitions: list[tuple] = []

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
                self._move(r, "REVOKED")
                return True
            return False

    def claim(self, digest, device_id, token_id):  # noqa: ANN001, ANN201
        if self.fail_claim:
            raise ConnectionError("control database down")
        with self.lock:
            for r in self.rows.values():
                if r["code_hash"] == digest and r["status"] == "ISSUED" and r["expires_at"] > self.now:
                    self._move(r, "CLAIMED", claimed_at=self.now, device_id=device_id, token_id=token_id)
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
            self._move(r, "COMPLETED", completed_at=self.now, route_hash=route_hash)
            self.routes.routes[route_hash] = claim.organization_id
            self.audit.append(("enrollment.complete", claim.enrollment_id))
            return True

    # the c0003 trigger: terminal states never change; CLEANUP_PENDING only moves to FAILED
    ALLOWED = {"ISSUED": {"CLAIMED", "REVOKED", "EXPIRED"}, "CLAIMED": {"COMPLETED", "CLEANUP_PENDING", "FAILED"},
               "CLEANUP_PENDING": {"FAILED"}}

    def _move(self, r: dict, status: str, **fields) -> None:  # noqa: ANN003
        assert status in self.ALLOWED.get(r["status"], set()), f"forbidden {r['status']} -> {status}"
        r.update(status=status, **fields)
        self.transitions.append((r["enrollment_id"], status))

    @staticmethod
    def _claim(r: dict) -> E.Claim:
        return E.Claim(r["enrollment_id"], r["organization_id"], r["employee_id"], r["device_display_name"],
                       r["allow_existing_device"], r["device_id"], r["token_id"])

    def fail(self, claim, reason):  # noqa: ANN001, ANN201
        with self.lock:
            r = self.rows[claim.enrollment_id]
            if r["status"] != "CLAIMED":
                return False
            self._move(r, "FAILED", failure_reason=reason)
            return True

    def mark_cleanup_pending(self, claim, reason):  # noqa: ANN001, ANN201
        if self.fail_mark:
            raise ConnectionError("control database down")
        with self.lock:
            r = self.rows[claim.enrollment_id]
            if r["status"] != "CLAIMED":
                return False
            self._move(r, "CLEANUP_PENDING", failure_reason=reason)
            return True

    def take_stale_claims(self, older_than_seconds):  # noqa: ANN001, ANN201
        with self.lock:
            n = 0
            for r in self.rows.values():
                if r["status"] == "CLAIMED" and r["claimed_at"] < self.now - timedelta(seconds=older_than_seconds):
                    self._move(r, "CLEANUP_PENDING", failure_reason="interrupted")
                    n += 1
            return n

    def cleanup_pending(self):  # noqa: ANN201
        with self.lock:
            return [self._claim(r) for r in self.rows.values() if r["status"] == "CLEANUP_PENDING"]

    def finish_cleanup(self, claim):  # noqa: ANN001, ANN201
        if self.fail_finish:
            raise ConnectionError("control database down")
        with self.lock:
            r = self.rows[claim.enrollment_id]
            if r["status"] != "CLEANUP_PENDING":
                return False
            self._move(r, "FAILED")
            return True

    def expire_codes(self):  # noqa: ANN201
        with self.lock:
            n = 0
            for r in self.rows.values():
                if r["status"] == "ISSUED" and r["expires_at"] <= self.now:
                    self._move(r, "EXPIRED")
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
    # reconcile after the delay: CLEANUP_PENDING, then (tenant reachable) FAILED + the token (if any) revoked
    _age(env)
    result = env.service.reconcile()
    assert result == E.ReconcileResult(0, 1, 1, 1 if step == "token_issued" else 0, 0, 0)
    assert row["status"] == "FAILED" and row["failure_reason"] == "interrupted"
    assert [s for _, s in env.store.transitions][-2:] == ["CLEANUP_PENDING", "FAILED"]
    assert all(t.status == "REVOKED" for t in env.t["a"]["repo"]._tokens.values())


def _age(env) -> None:  # noqa: ANN001
    env.store.now += timedelta(seconds=env.service.settings.reconcile_after_seconds + 1)


def _tenant_down(env, name="a"):  # noqa: ANN001, ANN202
    """Tenant ``name`` unreachable for token revocation; returns the restore function."""
    repo = env.t[name]["repo"]

    def down(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RepositoryUnavailable(f"tenant {name} down")

    repo.revoke_token = down
    return lambda: repo.__dict__.pop("revoke_token", None)


def _crash_after_token(env):  # noqa: ANN001, ANN202
    """An enrollment whose process died after issuing the token: CLAIMED, ACTIVE unroutable token."""
    _, code = _code(env)
    env.faults.append("token_issued")
    assert _enroll(env, code).status_code == 500
    env.faults.remove("token_issued")
    row = next(r for r in env.store.rows.values() if r["status"] == "CLAIMED")
    token = next(t for t in env.t["a"]["repo"]._tokens.values() if t.token_id == str(row["token_id"]))
    assert token.status == "ACTIVE"
    return row, token


def _token(env, token_id, name="a"):  # noqa: ANN001, ANN202
    return next(t for t in env.t[name]["repo"]._tokens.values() if t.token_id == token_id)


def test_unreachable_tenant_keeps_cleanup_pending_and_a_later_run_finishes_it():
    """The 10G-A repair: an interrupted enrollment whose tenant is down is NOT
    marked FAILED (which would abandon its ACTIVE token); the next run retries."""
    env = _env()
    row, token = _crash_after_token(env)
    _age(env)
    restore = _tenant_down(env)
    first = env.service.reconcile()
    assert first == E.ReconcileResult(expired_codes=0, interrupted=1, cleanup_completed=0, tokens_revoked=0,
                                      cleanup_pending=1, tenant_unreachable=1)
    assert row["status"] == "CLEANUP_PENDING" and row["failure_reason"] == "interrupted"
    assert _token(env, token.token_id).status == "ACTIVE" and not env.runtime.routes.routes  # live but unroutable
    restore()
    second = env.service.reconcile()
    assert second == E.ReconcileResult(0, 0, 1, 1, 0, 0)
    assert row["status"] == "FAILED" and _token(env, token.token_id).status == "REVOKED"
    assert not env.runtime.routes.routes
    before = list(env.store.transitions)
    third = env.service.reconcile()  # idempotent
    assert third == E.ReconcileResult(0, 0, 0, 0, 0, 0)
    assert env.store.transitions == before and row["status"] == "FAILED"
    assert _token(env, token.token_id).status == "REVOKED" and not env.runtime.routes.routes


def test_cleanup_is_retried_on_every_run_without_a_give_up_limit():
    env = _env()
    row, token = _crash_after_token(env)
    _age(env)
    restore = _tenant_down(env)
    for run in range(3):
        r = env.service.reconcile()
        assert (r.interrupted, r.cleanup_pending, r.tenant_unreachable, r.cleanup_completed) == (int(run == 0), 1, 1, 0)
        assert row["status"] == "CLEANUP_PENDING" and _token(env, token.token_id).status == "ACTIVE"
    restore()
    r = env.service.reconcile()
    assert (r.cleanup_completed, r.tokens_revoked, r.cleanup_pending) == (1, 1, 0)
    assert row["status"] == "FAILED" and _token(env, token.token_id).status == "REVOKED"


def test_tenant_not_resolvable_also_keeps_cleanup_pending():
    env = _env()
    row, token = _crash_after_token(env)
    _age(env)
    org_id = env.t["a"]["org"]
    org = env.runtime.control.orgs[org_id]
    env.runtime.control.orgs[org_id] = replace(org, status=OrganizationStatus.SUSPENDED)
    r = env.service.reconcile()
    assert (r.cleanup_pending, r.tenant_unreachable) == (1, 1) and row["status"] == "CLEANUP_PENDING"
    env.runtime.control.orgs[org_id] = org
    assert env.service.reconcile().cleanup_completed == 1 and _token(env, token.token_id).status == "REVOKED"


def test_cleanup_recorded_later_if_control_fails_after_the_revocation():
    env = _env()
    row, token = _crash_after_token(env)
    _age(env)
    env.store.fail_finish = True
    r = env.service.reconcile()
    assert (r.tokens_revoked, r.cleanup_completed, r.cleanup_pending, r.tenant_unreachable) == (1, 0, 1, 0)
    assert row["status"] == "CLEANUP_PENDING" and _token(env, token.token_id).status == "REVOKED"
    env.store.fail_finish = False
    r = env.service.reconcile()  # token already revoked = cleanup confirmed
    assert (r.tokens_revoked, r.cleanup_completed) == (0, 1) and row["status"] == "FAILED"


def test_token_never_issued_completes_cleanup():
    env = _env()
    env.faults.append("device_ready")  # died after CLAIM and device creation, before the token
    _, code = _code(env)
    assert _enroll(env, code).status_code == 500
    row = next(iter(env.store.rows.values()))
    assert row["status"] == "CLAIMED" and not env.t["a"]["repo"]._tokens
    _age(env)
    assert env.service.reconcile() == E.ReconcileResult(0, 1, 1, 0, 0, 0)
    assert row["status"] == "FAILED" and not env.t["a"]["repo"]._tokens


def test_token_already_revoked_completes_cleanup():
    env = _env()
    row, token = _crash_after_token(env)
    assert env.t["a"]["repo"].revoke_token(token.token_id)  # e.g. an operator revoked it meanwhile
    assert env.t["a"]["repo"].revoke_token(token.token_id) is False  # idempotent
    _age(env)
    assert env.service.reconcile() == E.ReconcileResult(0, 1, 1, 0, 0, 0)
    assert row["status"] == "FAILED"


def test_route_failure_revokes_the_token_immediately():
    env = _env()
    env.store.fail_complete = True
    _, code = _code(env)
    r = _enroll(env, code)
    assert r.status_code == 503
    tokens = list(env.t["a"]["repo"]._tokens.values())
    assert len(tokens) == 1 and tokens[0].status == "REVOKED"
    assert not env.runtime.routes.routes
    row = next(iter(env.store.rows.values()))
    assert (row["status"], row["failure_reason"]) == ("FAILED", "route_failed")


def test_route_failure_with_revocation_failure_stays_cleanup_pending():
    env = _env()
    env.store.fail_complete = True
    restore = _tenant_down(env)
    _, code = _code(env)
    assert _enroll(env, code).status_code == 503
    row = next(iter(env.store.rows.values()))
    token = _token(env, str(row["token_id"]))
    assert (row["status"], row["failure_reason"]) == ("CLEANUP_PENDING", "route_failed")  # NOT terminal
    assert token.status == "ACTIVE" and not env.runtime.routes.routes
    restore()
    r = env.service.reconcile()  # no need to wait for the delay: it is already cleanup-pending
    assert (r.interrupted, r.cleanup_completed, r.tokens_revoked) == (0, 1, 1)
    assert row["status"] == "FAILED" and _token(env, token.token_id).status == "REVOKED"


def test_route_and_revocation_and_control_failure_falls_back_to_reconcile():
    env = _env()
    env.store.fail_complete = env.store.fail_mark = True
    restore = _tenant_down(env)
    _, code = _code(env)
    assert _enroll(env, code).status_code == 503
    row = next(iter(env.store.rows.values()))
    assert row["status"] == "CLAIMED"  # nothing could be recorded: the stale-claim path takes over
    restore()
    env.store.fail_complete = env.store.fail_mark = False
    _age(env)
    assert env.service.reconcile() == E.ReconcileResult(0, 1, 1, 1, 0, 0)
    assert row["status"] == "FAILED" and _token(env, str(row["token_id"])).status == "REVOKED"


def test_ambiguous_token_write_is_treated_as_possibly_issued():
    env = _env()
    repo = env.t["a"]["repo"]
    real_add = repo.add_token

    def committed_then_lost(*a, **k):  # noqa: ANN002, ANN003, ANN202
        real_add(*a, **k)  # the INSERT committed ...
        raise RepositoryUnavailable("connection lost")  # ... but the answer never arrived

    repo.add_token = committed_then_lost
    _, code = _code(env)
    assert _enroll(env, code).status_code == 503
    del repo.add_token
    row = next(iter(env.store.rows.values()))
    assert (row["status"], row["failure_reason"]) == ("CLEANUP_PENDING", "token_uncertain")
    assert _token(env, str(row["token_id"])).status == "ACTIVE"
    assert env.service.reconcile().cleanup_completed == 1
    assert row["status"] == "FAILED" and _token(env, str(row["token_id"])).status == "REVOKED"


def test_slow_enrollment_refuses_to_issue_after_half_the_reconcile_delay(monkeypatch):
    env = _env()
    _, code = _code(env)
    clock = [1000.0]
    monkeypatch.setattr(E.time, "monotonic", lambda: clock[0])

    def stall(step: str) -> None:
        if step == "device_ready":
            clock[0] += env.service.settings.reconcile_after_seconds / 2 + 1

    env.service._fault = stall
    assert _enroll(env, code).status_code == 503
    row = next(iter(env.store.rows.values()))
    assert (row["status"], row["failure_reason"]) == ("FAILED", "timeout") and not env.t["a"]["repo"]._tokens


# ─── device rows left by interrupted enrollments ───────────────────────────


@pytest.mark.parametrize("step", ["device_ready", "token_issued"])
def test_device_row_of_an_interrupted_enrollment_is_recovered_by_a_plain_code(step):
    env = _env()
    env.faults.append(step)
    _, code = _code(env)
    assert _enroll(env, code).status_code == 500
    env.faults.remove(step)
    assert env.t["a"]["repo"].get_device("device-1").employee_id == "employee-1"
    # while the cleanup has not run, the unroutable token still counts: a plain code is refused
    if step == "token_issued":
        _, code = _code(env)
        assert _enroll(env, code).status_code == 409
    _age(env)
    env.service.reconcile()
    assert not env.t["a"]["repo"].has_active_token("device-1")
    _, code = _code(env)
    r = _enroll(env, code)  # same employee, same device id, plain code: recovered, no bypass needed
    assert r.status_code == 200 and _sync_ok(env, r.json()["device_token"]).status_code == 200
    assert len(env.t["a"]["repo"].list_devices()) == 1


def test_orphan_device_row_is_never_reassigned_to_another_employee():
    env = _env()
    env.faults.append("device_ready")
    _, code = _code(env)
    assert _enroll(env, code).status_code == 500
    env.faults.clear()
    _age(env)
    env.service.reconcile()
    env.t["a"]["repo"].add_employee("employee-2", "Someone else")
    for reenroll in (False, True):
        _, code = _code(env, employee="employee-2", allow_existing_device=reenroll)
        assert _enroll(env, code).status_code == 409
    assert env.t["a"]["repo"].get_device("device-1").employee_id == "employee-1"
    assert not env.t["a"]["repo"]._tokens


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
        if step == "token_issued":  # a full reconcile runs between token issue and completion
            _age(env)
            assert env.service.reconcile().interrupted == 1

    env.service._fault = reconcile_wins
    assert _enroll(env, code).status_code == 401
    row = next(iter(env.store.rows.values()))
    assert row["status"] == "FAILED" and row["route_hash"] is None and not env.runtime.routes.routes
    assert all(t.status == "REVOKED" for t in env.t["a"]["repo"]._tokens.values())


def test_reconcile_moved_to_cleanup_pending_blocks_completion_even_if_the_tenant_is_down():
    env = _env()
    _, code = _code(env)
    restore = None

    def reconcile_wins(step: str) -> None:
        nonlocal restore
        if step == "token_issued":
            _age(env)
            restore = _tenant_down(env)
            env.service.reconcile()

    env.service._fault = reconcile_wins
    assert _enroll(env, code).status_code == 401  # completion found CLEANUP_PENDING: no route written
    row = next(iter(env.store.rows.values()))
    assert row["status"] == "CLEANUP_PENDING" and not env.runtime.routes.routes
    restore()
    env.service._fault = lambda step: None
    assert env.service.reconcile().cleanup_completed == 1
    assert row["status"] == "FAILED" and all(t.status == "REVOKED" for t in env.t["a"]["repo"]._tokens.values())


def test_completion_winning_is_never_touched_by_reconcile():
    env = _env()
    _, code = _code(env)
    token = _enroll(env, code).json()["device_token"]
    _age(env)
    assert env.service.reconcile() == E.ReconcileResult(0, 0, 0, 0, 0, 0)
    row = next(iter(env.store.rows.values()))
    assert row["status"] == "COMPLETED" and len(env.runtime.routes.routes) == 1
    assert _sync_ok(env, token).status_code == 200


def test_concurrent_completion_and_reconciliation_have_exactly_one_valid_outcome():
    seen = set()
    for _ in range(40):
        env = _env()
        _, code = _code(env)
        go = threading.Barrier(2)
        results: dict[str, object] = {}
        late = random.choice(("enroll", "reconcile"))  # which side starts a little later: both orders occur

        def at_token(step: str) -> None:
            if step == "token_issued":
                go.wait(10)  # completion and reconciliation start together
                if late == "enroll":
                    time.sleep(0.005)

        env.service._fault = at_token

        def enroll() -> None:
            results["enroll"] = _enroll(env, code)

        def reconcile() -> None:
            go.wait(10)
            if late == "reconcile":
                time.sleep(0.005)
            _age(env)
            results["reconcile"] = env.service.reconcile()

        threads = [threading.Thread(target=enroll), threading.Thread(target=reconcile)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
        env.service._fault = lambda step: None
        env.service.reconcile()  # let any pending cleanup finish
        row = next(iter(env.store.rows.values()))
        tokens = list(env.t["a"]["repo"]._tokens.values())
        assert len(tokens) == 1
        seen.add(results["enroll"].status_code)
        if results["enroll"].status_code == 200:  # A: completion won
            assert row["status"] == "COMPLETED" and len(env.runtime.routes.routes) == 1
            assert tokens[0].status == "ACTIVE"
        else:  # B: reconciliation won
            assert results["enroll"].status_code == 401
            assert row["status"] == "FAILED" and not env.runtime.routes.routes and tokens[0].status == "REVOKED"
        assert [s for _, s in env.store.transitions].count("COMPLETED") <= 1
    assert seen == {200, 401}, seen  # both valid outcomes were exercised


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
