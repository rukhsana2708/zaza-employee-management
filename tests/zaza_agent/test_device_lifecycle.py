"""Device lifecycle: failure-safe re-enrollment and tenant-aware device administration.

Re-enrollment contract (docs/zaza/ENROLLMENT.md section 4):
- a `--reenroll` code replaces the credential of an EXISTING device of the same employee; it never creates a
  second device and never re-enables a disabled one;
- the OLD credential stays valid until the NEW one has been used successfully once (so a failed or lost
  enrollment never leaves the employee with a broken installation); then every older credential is revoked in
  one transaction; `enrollment-reconcile` finishes the revocation if that step failed.
"""

from __future__ import annotations

import ast
import contextlib
import logging
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from deskmate.zaza_server.repository import DeviceRecord  # noqa: E402
from deskmate.zaza_server.tenancy.devices import SaasDeviceOperator  # noqa: E402
from deskmate.zaza_server.tenancy.domain import TenancyError  # noqa: E402
from deskmate.zaza_server.tenancy.resolver import TenantResolver  # noqa: E402

from .test_enrollment import _code, _enroll, _env, _sync_ok  # noqa: E402
from .test_saas_employees import Directory  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _tokens(env, name="a"):  # noqa: ANN001, ANN202
    return list(env.t[name]["repo"]._tokens.values())


def _active(env, name="a"):  # noqa: ANN001, ANN202
    return [t for t in _tokens(env, name) if t.status == "ACTIVE"]


def _enrolled(env):  # noqa: ANN001, ANN202
    _, code = _code(env)
    return _enroll(env, code).json()["device_token"]


# ─── server: failure-safe replacement ──────────────────────────────────────


def test_reenrollment_keeps_one_device_and_replaces_the_credential_after_first_use():
    env = _env()
    old = _enrolled(env)
    assert _sync_ok(env, old).status_code == 200
    _, code = _code(env, allow_existing_device=True)
    r = _enroll(env, code)  # SAME device id
    assert r.status_code == 200 and r.json()["device_id"] == "device-1"
    new = r.json()["device_token"]
    assert [d.device_id for d in env.t["a"]["repo"].list_devices()] == ["device-1"]  # never a second device
    assert len(_active(env)) == 2  # not superseded yet: the new credential is still unproven
    assert _sync_ok(env, old).status_code == 200
    assert _sync_ok(env, new).status_code == 200  # first successful use ...
    assert _sync_ok(env, old).status_code in (401, 403)  # ... supersedes the old one
    assert len(_active(env)) == 1 and _sync_ok(env, new).status_code == 200


def test_a_lost_response_never_breaks_the_installed_credential():
    env = _env()
    old = _enrolled(env)
    _, code = _code(env, allow_existing_device=True)
    assert _enroll(env, code).status_code == 200  # the agent never receives (or never uses) the new token
    for _ in range(3):
        assert _sync_ok(env, old).status_code == 200  # the working credential is untouched
    assert len(_active(env)) == 2
    assert env.service.reconcile().superseded_tokens == 0  # reconcile also waits for proof of use
    assert _sync_ok(env, old).status_code == 200


def test_a_failure_before_completion_leaves_the_old_credential_and_no_new_one():
    env = _env()
    old = _enrolled(env)
    _, code = _code(env, allow_existing_device=True)
    env.store.fail_complete = True  # the control plane is down at the completion step
    r = _enroll(env, code)
    assert r.status_code >= 500
    assert _sync_ok(env, old).status_code == 200
    assert len(_active(env)) == 1  # the unrouted new token was revoked
    env.store.fail_complete = False


def test_reconcile_finishes_a_supersession_that_failed_at_first_use():
    env = _env()
    old = _enrolled(env)
    _, code = _code(env, allow_existing_device=True)
    new = _enroll(env, code).json()["device_token"]
    repo = env.t["a"]["repo"]
    real = repo.confirm_token_use
    repo.confirm_token_use = lambda token_id: (_ for _ in ()).throw(RuntimeError("storage hiccup"))
    try:
        assert _sync_ok(env, new).status_code == 200  # authentication still works
        assert _sync_ok(env, old).status_code == 200  # nothing was revoked, nothing was lost
    finally:
        repo.confirm_token_use = real
    reenrolled = next(r for r in env.store.rows.values() if r["status"] == "COMPLETED" and r["allow_existing_device"])
    repo.__dict__.setdefault("_used_tokens", set()).add(str(reenrolled["token_id"]))  # the new credential was used
    result = env.service.reconcile()
    assert result.superseded_tokens == 1
    assert _sync_ok(env, old).status_code in (401, 403) and _sync_ok(env, new).status_code == 200
    assert env.service.reconcile().superseded_tokens == 0  # idempotent


def test_a_reenrollment_code_never_creates_a_second_device_and_is_single_use():
    env = _env()
    _enrolled(env)
    _, code = _code(env, allow_existing_device=True)
    r = _enroll(env, code, device="some-other-device")
    assert r.status_code == 403 and "some-other-device" not in r.text
    assert [d.device_id for d in env.t["a"]["repo"].list_devices()] == ["device-1"]
    assert _enroll(env, code).status_code == 401  # the code is used up even though it was refused
    _, good = _code(env, allow_existing_device=True)
    assert _enroll(env, good).status_code == 200
    assert _enroll(env, good).status_code == 401  # single use


def test_security_boundaries_are_unchanged():
    env = _env()
    a = env.t["a"]
    _enrolled(env)
    _, plain = _code(env)
    assert _enroll(env, plain).status_code == 409  # an ordinary code never adds a credential to an enrolled device
    a["repo"].add_employee("employee-3", "Someone else")
    _, other = _code(env, employee="employee-3", allow_existing_device=True)
    assert _enroll(env, other).status_code == 409  # never reassigned to another employee
    a["repo"].set_device_status("device-1", "DISABLED")
    _, disabled = _code(env, allow_existing_device=True)
    assert _enroll(env, disabled).status_code == 403
    assert a["repo"].get_device("device-1").status == "DISABLED"  # never silently re-enabled


def test_no_token_or_code_in_logs_during_reenrollment(caplog):
    env = _env()
    old = _enrolled(env)
    _, code = _code(env, allow_existing_device=True)
    with caplog.at_level(logging.DEBUG):
        r = _enroll(env, code)
        new = r.json()["device_token"]
        _sync_ok(env, new)
        _sync_ok(env, old)
        env.service.reconcile()
    for secret in (old, new, code, code.replace("-", "")):
        assert secret not in caplog.text


# ─── operator: tenant-aware device administration ──────────────────────────


class DeviceRepo:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceRecord] = {}
        self.active_tokens: dict[str, int] = {}

    def list_devices(self):  # noqa: ANN201
        return sorted(self.devices.values(), key=lambda d: d.device_id)

    def get_device(self, device_id):  # noqa: ANN001, ANN201
        return self.devices.get(device_id)

    def has_active_token(self, device_id):  # noqa: ANN001, ANN201
        return self.active_tokens.get(device_id, 0) > 0

    def set_device_status(self, device_id, status):  # noqa: ANN001
        d = self.devices[device_id]
        self.devices[device_id] = DeviceRecord(d.device_id, d.employee_id, status, d.created_at, d.display_name)

    def revoke_tokens(self, device_id, *, except_token_id=None):  # noqa: ANN001, ANN201
        n, self.active_tokens[device_id] = self.active_tokens.get(device_id, 0), 0
        return n


class DeviceManager:
    def __init__(self) -> None:
        self.repos: dict[str, DeviceRepo] = {}
        self.leases: list[str] = []

    @contextlib.contextmanager
    def repository(self, context):  # noqa: ANN001, ANN201
        name = context.database.database_name
        self.leases.append(name)
        yield self.repos.setdefault(name, DeviceRepo())


def _setup():  # noqa: ANN202
    d, m = Directory(), DeviceManager()
    op = SaasDeviceOperator(d, TenantResolver(d), m)
    a, b = d.org("tenant-a"), d.org("tenant-b")
    for oid, employee in ((a, "alice"), (b, "bob")):
        repo = m.repos.setdefault(d.dbs[oid].database_name, DeviceRepo())
        repo.devices["pc-1"] = DeviceRecord("pc-1", employee, "ACTIVE", "now", f"{employee} PC")
        repo.active_tokens["pc-1"] = 2
    return d, m, op


def test_device_commands_stay_inside_the_named_tenant():
    d, m, op = _setup()
    assert [(v.device_id, v.employee_id, v.has_active_credential) for v in op.list("tenant-a")] == [
        ("pc-1", "alice", True)]
    assert [v.employee_id for v in op.list("tenant-b")] == ["bob"]
    assert op.list("tenant-a", "bob") == []  # filter by employee
    assert op.disable("tenant-a", "pc-1").status == "DISABLED"
    assert [v.status for v in op.list("tenant-b")] == ["ACTIVE"]  # same device id in tenant B untouched
    assert op.revoke_tokens("tenant-b", "pc-1") == 2
    assert [v.has_active_credential for v in op.list("tenant-b")] == [False]
    assert [v.has_active_credential for v in op.list("tenant-a")] == [True]  # A's credentials untouched


def test_unknown_devices_and_unusable_organizations_are_refused():
    d, m, op = _setup()
    with pytest.raises(TenancyError, match="no device"):
        op.disable("tenant-a", "nope")
    with pytest.raises(TenancyError, match="no device"):
        op.revoke_tokens("tenant-a", "nope")
    with pytest.raises(TenancyError):
        op.list("no-such-org")
    d.org("suspended-org", status=__import__("deskmate.zaza_server.tenancy.domain", fromlist=["x"]).OrganizationStatus.SUSPENDED)
    leases = len(m.leases)
    with pytest.raises(TenancyError):
        op.list("suspended-org")
    assert len(m.leases) == leases  # no database was opened
    with pytest.raises(TenancyError):
        op.disable("tenant-a", "bad device id!")


def test_device_listing_exposes_no_secret_fields():
    d, m, op = _setup()
    fields = set(op.list("tenant-a")[0].__dataclass_fields__)
    assert fields == {"device_id", "employee_id", "status", "display_name", "last_seen_at", "has_active_credential"}


def test_device_admin_never_uses_the_legacy_single_database_path():
    source = (ROOT / "deskmate/zaza_server/tenancy/devices.py").read_text(encoding="utf-8")
    names = {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)}
    assert not {"open_repository", "database_settings_from_env", "register_device", "rotate_token"} & names
    cli = (ROOT / "deskmate/zaza_server/__main__.py").read_text(encoding="utf-8")
    for command in ("saas-device-list", "saas-device-disable", "saas-device-revoke-tokens"):
        assert f'"{command}"' in cli
    assert "repeat with --yes to confirm" in cli  # revoking all credentials needs an explicit confirmation


def test_identifiers_are_validated():
    d, m, op = _setup()
    for bad in ("", "x" * 300, "a b", "../x"):
        with pytest.raises((TenancyError, ValueError)):
            op.revoke_tokens("tenant-a", bad)
    assert uuid  # (module kept for parity with the employee tests)
