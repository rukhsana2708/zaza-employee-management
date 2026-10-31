"""Phase A real database-per-tenant identity, lifecycle, scope and migration tests."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from deskmate.zaza_server.tenancy.domain import MembershipRole, TenancyError
from deskmate.zaza_server.tenancy.resolver import TenantAccessDenied
from deskmate.zaza_server.tenancy.saas_auth import (
    ROLE_PERMISSIONS,
    Permission,
    PermissionDenied,
    permissions,
)
from deskmate.zaza_server.workforce.service import SubjectScope, WorkforceService

from .test_saas_postgres import saas  # noqa: F401 -- reuse real disposable database fixture

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


@pytest.fixture
def workforce(saas):  # noqa: F811 - imported PostgreSQL fixture
    c, runtime = saas.control, saas.runtime
    owner = c.create_user("owner@workforce.test", "Owner")
    manager = c.create_user("manager@workforce.test", "Manager")
    employee = c.create_user("employee@workforce.test", "Employee")
    viewer = c.create_user("viewer@workforce.test", "Viewer")
    svc = WorkforceService(c, runtime.resolver, runtime.manager, clock=lambda: NOW)
    contexts, memberships = {}, {}
    for name, tenant in saas.tenants.items():
        contexts[name], memberships[name] = {}, {}
        for key, user, role in [
            ("owner", owner, "OWNER"),
            ("manager", manager, "MANAGER"),
            ("employee", employee, "EMPLOYEE"),
            ("viewer", viewer, "VIEWER"),
        ]:
            m = c.add_membership(tenant.org.organization_id, user.user_id, role)
            memberships[name][key] = m
            contexts[name][key] = runtime.resolver.for_member(
                user.user_id, tenant.org.organization_id
            )
        with runtime.manager.repository(contexts[name]["owner"]) as repo:
            for identity in ["boss", "report", "unrelated", "nested"]:
                repo.add_employee(identity, identity)
        for key, eid in [("manager", "boss"), ("employee", "report"), ("viewer", "unrelated")]:
            m = memberships[name][key]
            svc.link_account(contexts[name]["owner"], eid, m.user_id, m.membership_id)
    yield SimpleNamespace(
        saas=saas,
        service=svc,
        ctx=contexts,
        members=memberships,
        owner=owner,
        manager=manager,
        employee=employee,
        viewer=viewer,
    )


def test_tenant_catalogs_profiles_teams_are_isolated(workforce):
    w = workforce
    ids = {}
    for name in ["a", "b"]:
        ctx = w.ctx[name]["owner"]
        d = w.service.create_catalog(ctx, "designation", "Project Manager")
        dept = w.service.create_catalog(ctx, "department", "Engineering")
        team = w.service.create_catalog(ctx, "team", "Delivery")
        ids[name] = d["id"]
        p = w.service.update_profile(
            ctx, "boss", version=1, designation_id=d["id"], department_id=dept["id"]
        )
        w.service.set_team(ctx, "boss", team["id"])
        assert p["role"] == "EMPLOYEE" and p["monitoring_enabled"]
        assert w.service.profile(w.ctx[name]["manager"], "boss")["team_ids"] == [team["id"]]
    assert ids["a"] != ids["b"]
    with pytest.raises(TenancyError):
        w.service.update_profile(w.ctx["a"]["owner"], "report", version=1, designation_id=ids["b"])
    assert w.service.profile(w.ctx["a"]["owner"], "report")["version"] == 1


def test_linkage_exact_membership_and_atomic_replace(workforce):
    w = workforce
    foreign = w.members["b"]["employee"]
    with pytest.raises(PermissionDenied):
        w.service.link_account(
            w.ctx["a"]["owner"], "unrelated", foreign.user_id, foreign.membership_id
        )
    local = w.members["a"]["employee"]
    with pytest.raises(TenancyError):
        w.service.link_account(
            w.ctx["a"]["owner"], "unrelated", local.user_id, local.membership_id, replace=True
        )
    # Failed replacement did not revoke the previous viewer linkage.
    with w.saas.runtime.manager.repository(w.ctx["a"]["owner"]) as repo:
        with repo.connection() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM employee_account_links WHERE employee_id='unrelated' "
                    "AND revoked_at IS NULL"
                ).fetchone()["n"]
                == 1
            )
    assert w.service.revoke_link(w.ctx["a"]["owner"], "report") == 1
    w.service.link_account(
        w.ctx["a"]["owner"], "unrelated", local.user_id, local.membership_id, replace=True
    )
    assert w.service.subject_scope(w.ctx["a"]["employee"], "unrelated").kind == "self"
    with pytest.raises(PermissionDenied):
        w.service.subject_scope(w.ctx["a"]["employee"], "report")
    assert w.service.subject_scope(w.ctx["b"]["employee"], "report").kind == "self"


def test_manager_scope_and_history(workforce):
    w = workforce
    owner = w.ctx["a"]["owner"]
    manager = w.ctx["a"]["manager"]
    start = NOW - timedelta(days=2)
    r = w.service.add_reporting(owner, "report", "boss", start)
    w.service.add_reporting(owner, "nested", "report", start)
    assert w.service.subject_scope(manager, "boss").kind == "self"
    scope = w.service.subject_scope(manager, "report")
    assert scope.kind == "direct_report"
    assert scope.clip(NOW - timedelta(days=100), NOW + timedelta(days=1)) == (start, NOW)
    for identity in ["nested", "unrelated"]:
        with pytest.raises(PermissionDenied):
            w.service.subject_scope(manager, identity)
    w.service.end_reporting(owner, r["id"], NOW - timedelta(seconds=1))
    with pytest.raises(PermissionDenied):
        w.service.subject_scope(manager, "report")
    assert w.service.subject_scope(manager, "boss").kind == "self"


def test_relationship_and_capability_both_required(workforce):
    w = workforce
    w.service.add_reporting(w.ctx["a"]["owner"], "report", "unrelated", NOW - timedelta(days=1))
    with pytest.raises(PermissionDenied):
        w.service.subject_scope(w.ctx["a"]["viewer"], "report")
    with pytest.raises(PermissionDenied):
        w.service.subject_scope(w.ctx["a"]["manager"], "report")
    assert w.service.subject_scope(w.ctx["a"]["owner"], "report").kind == "organization"


@pytest.mark.parametrize("case", ["self", "overlap", "cycle", "future_cycle"])
def test_reporting_constraints(workforce, case):
    w = workforce
    ctx = w.ctx["a"]["owner"]
    start = NOW - timedelta(days=1)
    with pytest.raises(TenancyError):
        if case == "self":
            w.service.add_reporting(ctx, "report", "report", start)
        elif case == "overlap":
            w.service.add_reporting(ctx, "report", "boss", start)
            w.service.add_reporting(ctx, "report", "unrelated", NOW)
        elif case == "cycle":
            w.service.add_reporting(ctx, "report", "boss", start)
            w.service.add_reporting(ctx, "boss", "report", start)
        else:
            w.service.add_reporting(ctx, "report", "boss", NOW + timedelta(days=5))
            w.service.add_reporting(ctx, "boss", "report", NOW + timedelta(days=6))


@pytest.mark.parametrize(
    "status,can_read,can_work",
    [
        ("ACTIVE", True, True),
        ("LEAVE", True, False),
        ("INACTIVE", False, False),
        ("TERMINATED", False, False),
    ],
)
def test_employment_lifecycle_is_separate(workforce, status, can_read, can_work):
    w = workforce
    ctx = w.ctx["a"]["owner"]
    own = w.ctx["a"]["employee"]
    p = w.service.update_profile(ctx, "report", version=1, employment_status=status)
    assert p["is_active"] and p["monitoring_enabled"]
    for work, allowed in [(False, can_read), (True, can_work)]:
        if allowed:
            assert w.service.subject_scope(own, "report", employee_work=work).kind == "self"
        else:
            with pytest.raises(PermissionDenied):
                w.service.subject_scope(own, "report", employee_work=work)


@pytest.mark.parametrize("change", ["user", "membership", "organization", "tenant"])
def test_fresh_lifecycle_checks_refuse_stale_context(workforce, change):
    w = workforce
    c = w.saas.control
    ctx = w.ctx["a"]["employee"]
    org = ctx.organization_id
    if change == "user":
        c.set_user_status(ctx.user_id, "DISABLED")
    if change == "membership":
        c.set_membership_status(org, ctx.user_id, "DISABLED")
    if change == "organization":
        c.set_organization_status(org, "SUSPENDED")
    if change == "tenant":
        c.mark_tenant_database(org, "ERROR")
    with pytest.raises(TenantAccessDenied):
        w.service.subject_scope(ctx, "report")


def test_monitoring_and_legacy_data_independent_of_roles(workforce):
    from deskmate.zaza.sync.protocol import SyncBatchRequest
    from deskmate.zaza_server.auth import authenticate_request
    from deskmate.zaza_server.service import SyncService

    from .test_saas_postgres import _batch

    w = workforce
    c = w.saas.control
    ctx = w.ctx["a"]["owner"]
    member = w.members["a"]["manager"]
    w.service.revoke_link(ctx, "boss")
    w.service.link_account(ctx, "employee-1", member.user_id, member.membership_id)
    with w.saas.runtime.manager.repository(ctx) as repo:
        before = repo.get_employee("employee-1")
        device = repo.get_device("device-1")
        body, _ = _batch("role-history")
        result = SyncService(repo).process_batch(device, SyncBatchRequest.model_validate(body))
        assert result.summary.accepted == 2
        history = repo.list_records(device_id="device-1")
        assert len(history) == 2
        for role in ["MANAGER", "OWNER", "EMPLOYEE"]:
            c.change_membership_role(ctx.organization_id, w.manager.user_id, role)
            assert w.service.profile(ctx, "employee-1")["monitoring_enabled"]
            assert repo.list_records(device_id="device-1") == history
        assert Permission.EMPLOYEES_VIEW_ALL not in permissions(
            w.saas.runtime.resolver.for_member(w.manager.user_id, ctx.organization_id)
        )
        w.service.update_profile(
            ctx, "employee-1", version=1, employment_status="TERMINATED", monitoring_enabled=False
        )
        c.set_user_status(w.manager.user_id, "DISABLED")
        assert repo.get_employee("employee-1") == before
        assert repo.get_device("device-1") == device
        assert repo.list_records(device_id="device-1") == history
        token = w.saas.tenants["a"].token
        assert authenticate_request(repo, "Bearer " + token, "device-1").device == device


def test_audit_atomicity_and_version_conflict(workforce):
    w = workforce
    ctx = w.ctx["a"]["owner"]
    d = w.service.create_catalog(ctx, "designation", "Researcher")
    w.service.update_catalog(
        ctx, "designation", d["id"], version=1, name="Researcher", active=False
    )
    with pytest.raises(TenancyError):
        w.service.update_profile(ctx, "report", version=1, designation_id=d["id"])
    w.service.update_profile(ctx, "report", version=1, monitoring_enabled=False)
    with pytest.raises(TenancyError):
        w.service.update_profile(ctx, "report", version=1, monitoring_enabled=True)
    with w.saas.runtime.manager.repository(ctx) as repo:
        audit = repo.list_audit(entity_type="employee_profile", entity_id="report")
        assert len(audit) == 1 and audit[0]["actor_type"] == "SAAS_USER"
        # Force audit insertion to fail: the business update must roll back.
        with repo.connection() as conn:
            conn.execute(
                "CREATE FUNCTION reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'test audit failure'; END $$"
            )
            conn.execute(
                "CREATE TRIGGER reject_test_audit BEFORE INSERT ON audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION reject_audit()"
            )
        try:
            with pytest.raises(Exception, match="test audit failure"):
                w.service.update_profile(ctx, "report", version=2, monitoring_enabled=True)
        finally:
            with repo.connection() as conn:
                conn.execute("DROP TRIGGER reject_test_audit ON audit_logs")
                conn.execute("DROP FUNCTION reject_audit()")
    assert not w.service.profile(ctx, "report")["monitoring_enabled"]


def test_context_is_mandatory_and_presets_are_explicit(workforce, monkeypatch):
    w = workforce
    for context in [None, "tenant-a", SimpleNamespace(user_id=w.owner.user_id)]:
        with pytest.raises(PermissionDenied):
            w.service.subject_scope(context, "report")
    marker = "future.unrelated"
    # Extending enum iteration must never expand any preset implicitly.
    monkeypatch.setattr(
        type(Permission), "__iter__", lambda cls: iter([*Permission.__members__.values(), marker])
    )
    for role in ["OWNER", "ADMIN", "MANAGER"]:
        assert marker not in permissions(
            w.ctx["a"][role.lower()]
            if role != "ADMIN"
            else w.saas.runtime.resolver.for_member(
                w.owner.user_id, w.ctx["a"]["owner"].organization_id
            )
        )
    assert ROLE_PERMISSIONS[MembershipRole.EMPLOYEE] == {
        Permission.ORG_VIEW,
        Permission.EMPLOYEES_VIEW_SELF,
    }


def test_scope_rejects_uncovered_history():
    s = SubjectScope("report", "direct_report", NOW, NOW + timedelta(days=1))
    with pytest.raises(PermissionDenied):
        s.clip(NOW - timedelta(days=1), NOW)
    with pytest.raises(TenancyError):
        s.clip(NOW.replace(tzinfo=None), NOW)


def test_same_reporting_ids_and_missing_foreign_membership(workforce):
    w = workforce
    identity = uuid.uuid4()
    for name in ["a", "b"]:
        with w.saas.runtime.manager.repository(w.ctx[name]["owner"]) as repo:
            with repo.connection() as conn:
                conn.execute(
                    "INSERT INTO employee_reporting_relationships "
                    "(id,employee_id,manager_employee_id,effective_from) VALUES (%s,'report',%s,%s)",
                    (identity, "boss" if name == "a" else "unrelated", NOW - timedelta(days=1)),
                )
    assert w.service.subject_scope(w.ctx["a"]["manager"], "report").kind == "direct_report"
    with pytest.raises(PermissionDenied):
        w.service.subject_scope(w.ctx["b"]["manager"], "report")
    user = w.saas.control.create_user("only-a@workforce.test", "Only A")
    member = w.saas.control.add_membership(
        w.ctx["a"]["owner"].organization_id, user.user_id, "EMPLOYEE"
    )
    with pytest.raises(PermissionDenied):
        w.service.link_account(w.ctx["b"]["owner"], "nested", user.user_id, member.membership_id)
    with pytest.raises(PermissionDenied):
        w.service.subject_scope(
            w.saas.runtime.resolver.for_member(user.user_id, member.organization_id), "report"
        )
    with pytest.raises(PermissionDenied):
        w.service.subject_scope(w.saas.runtime.resolver.for_job(member.organization_id), "report")


def test_concurrent_cycle_and_profile_updates_serialize(workforce):
    from concurrent.futures import ThreadPoolExecutor

    w = workforce
    ctx = w.ctx["a"]["owner"]

    def relationship(pair):
        try:
            w.service.add_reporting(ctx, *pair, NOW - timedelta(days=1))
            return True
        except TenancyError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(relationship, [("boss", "report"), ("report", "boss")])) == [
            False,
            True,
        ]

    def profile(enabled):
        try:
            w.service.update_profile(ctx, "report", version=1, monitoring_enabled=enabled)
            return True
        except TenancyError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(profile, [True, False])) == [False, True]


def test_catalog_changes_do_not_change_authority_and_ids_are_immutable(workforce):
    import psycopg

    w = workforce
    ctx = w.ctx["a"]["owner"]
    before = permissions(w.ctx["a"]["manager"])
    entry = w.service.create_catalog(ctx, "designation", "Manager")
    w.service.update_profile(ctx, "boss", version=1, designation_id=entry["id"])
    assert permissions(w.ctx["a"]["manager"]) == before
    with w.saas.runtime.manager.repository(ctx) as repo:
        with repo.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "UPDATE designations SET id=%s WHERE id=%s", (uuid.uuid4(), entry["id"])
                )
        assert repo.get_employee("boss").employee_id == "boss"
    # Preset definitions must be explicit even when the enum later grows.
    import ast
    from pathlib import Path

    from deskmate.zaza_server.tenancy import saas_auth

    tree = ast.parse(Path(saas_auth.__file__).read_text(encoding="utf-8"))
    preset = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "ROLE_PERMISSIONS"
    )
    assert all(
        isinstance(v, ast.Call) and len(v.args) == 1 and isinstance(v.args[0], ast.Set)
        for v in preset.value.values
    )
