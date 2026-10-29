"""Phase 10C: SaaS tenancy foundation — validation, TenantContext issuance,
trusted resolution and the isolation invariants, without a database.

The PostgreSQL side (control-plane constraints and the database-per-tenant
proof) is in test_tenancy_postgres.py (opt-in: ZAZA_TEST_POSTGRES_URL)."""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import pickle
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from deskmate.zaza_server.config import ConfigError, DatabaseSettings
from deskmate.zaza_server.tenancy import domain
from deskmate.zaza_server.tenancy.context import (
    ContextKind,
    TenantContext,
    TenantDatabaseDescriptor,
    tenant_database_settings,
)
from deskmate.zaza_server.tenancy.control_repository import Membership, Organization, User
from deskmate.zaza_server.tenancy.domain import (
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
    TenancyError,
    TenantDatabaseStatus,
    UserStatus,
)
from deskmate.zaza_server.tenancy.resolver import TenantAccessDenied, TenantResolver, run_per_tenant

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "deskmate" / "zaza_server"
TENANCY = SERVER / "tenancy"

# ─── a fake control directory (the real one is PostgresControlRepository) ──


class FakeDirectory:
    def __init__(self) -> None:
        self.orgs: dict[uuid.UUID, Organization] = {}
        self.users: dict[uuid.UUID, User] = {}
        self.members: dict[tuple[uuid.UUID, uuid.UUID], Membership] = {}
        self.dbs: dict[uuid.UUID, TenantDatabaseDescriptor] = {}

    def org(self, slug: str, *, status=OrganizationStatus.ACTIVE, db_status=TenantDatabaseStatus.READY):  # noqa: ANN001
        oid = uuid.uuid4()
        self.orgs[oid] = Organization(oid, slug, slug.title(), status, "UTC")
        if db_status is not None:
            self.dbs[oid] = TenantDatabaseDescriptor(oid, domain.generate_tenant_database_name(), "asia-south1",
                                                     db_status, "0004_manager_dashboard_auth"
                                                     if db_status is TenantDatabaseStatus.READY else None)
        return oid

    def user(self, *, status=UserStatus.ACTIVE) -> uuid.UUID:  # noqa: ANN001
        uid = uuid.uuid4()
        self.users[uid] = User(uid, f"{uid.hex[:8]}@example.com", "Someone", status)
        return uid

    def member(self, org: uuid.UUID, user: uuid.UUID, role: str, status=MembershipStatus.ACTIVE) -> None:  # noqa: ANN001
        self.members[(org, user)] = Membership(uuid.uuid4(), org, user, MembershipRole(role), status)

    def get_organization(self, organization_id):  # noqa: ANN001, ANN201
        return self.orgs.get(organization_id)

    def get_user(self, user_id):  # noqa: ANN001, ANN201
        return self.users.get(user_id)

    def get_membership(self, organization_id, user_id):  # noqa: ANN001, ANN201
        return self.members.get((organization_id, user_id))

    def get_tenant_database(self, organization_id):  # noqa: ANN001, ANN201
        return self.dbs.get(organization_id)

    def list_ready_organization_ids(self):  # noqa: ANN201
        return [o for o, org in self.orgs.items() if org.status is OrganizationStatus.ACTIVE
                and o in self.dbs and self.dbs[o].status is TenantDatabaseStatus.READY]


# ─── B. slugs, D. roles, G. database names, statuses, ids ──────────────────


@pytest.mark.parametrize(("raw", "slug"), [
    ("Zaza Studio", "zaza-studio"), ("  ABC_School ", "abc-school"), ("acme--call  center", "acme-call-center"),
    ("a1b", "a1b"), ("x" * 63, "x" * 63),
])
def test_slugs_are_normalized(raw, slug):
    assert domain.normalize_slug(raw) == slug


@pytest.mark.parametrize("bad", ["ab", "x" * 64, "-abc", "abc-", "ünïcode", "a/b", "a.b", "a;drop", "", "admin",
                                 "www", "zaza", None, 42])
def test_bad_slugs_are_rejected(bad):
    with pytest.raises(TenancyError):
        domain.normalize_slug(bad)


def test_membership_roles_are_only_the_saas_roles():
    assert {r.value for r in MembershipRole} == {"OWNER", "ADMIN", "MANAGER", "VIEWER"}
    for ok in ("OWNER", "ADMIN", "MANAGER", "VIEWER"):
        assert domain.membership_role(ok).value == ok
    for bad in ("EMPLOYEE", "PLATFORM_SUPERADMIN", "SUPERADMIN", "owner", "", None):
        with pytest.raises(TenancyError):
            domain.membership_role(bad)


def test_statuses_are_closed_sets():
    assert {s.value for s in OrganizationStatus} == {"ACTIVE", "SUSPENDED", "CLOSED"}
    assert {s.value for s in TenantDatabaseStatus} == {"PROVISIONING", "READY", "SUSPENDED", "ERROR"}
    for fn in (domain.organization_status, domain.tenant_database_status, domain.user_status):
        with pytest.raises(TenancyError):
            fn("DELETED")


def test_generated_tenant_database_names_are_safe_and_unique():
    names = {domain.generate_tenant_database_name() for _ in range(2000)}
    assert len(names) == 2000
    for name in names:
        assert domain.tenant_database_name(name) == name and len(name) <= 63


@pytest.mark.parametrize("bad", [
    "postgres", "template1", "zaza", "zaza_control", "zaza_t_", "zaza_t_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "zaza_t_abcdefghijklmnopqrstuvwxy", "zaza_t_abcdefghijklmnopqrstuvwxyz1", "zaza_t_abcdefghijklmnopqrstuvwxy0",
    "zaza_t_abcdefghijklmnopqrstuvwxyz;", 'zaza_t_abcdefghijklmnopqrstuvw"x', "zaza_t_abcd efghijklmnopqrstuvwxyz",
    "postgresql://u:p@h/zaza_t_abcdefghijklmnopqrstuvwxyz", "host=evil dbname=zaza_t_abcdefghijklmnopqrstuvwxyz",
    "../zaza_t_abcdefghijklmnopqrstuvwxyz", "zaza-studio", "Zaza Studio", None, 7,
])
def test_unsafe_database_names_are_rejected(bad):
    with pytest.raises(TenancyError):
        domain.tenant_database_name(bad)


def test_organization_ids_are_uuids_never_names_or_slugs():
    oid = uuid.uuid4()
    assert domain.organization_id(str(oid)) == oid and domain.organization_id(oid) is oid
    for bad in ("zaza-studio", "Zaza Studio", "1", "", None, "00000000-0000-0000-0000-00000000000g"):
        with pytest.raises(TenancyError):
            domain.organization_id(bad)


def test_emails_are_normalized():
    assert domain.normalize_email("  Alice.Manager@Example.COM ") == "alice.manager@example.com"
    for bad in ("alice", "a@b", "a b@example.com", "a@@example.com", "x" * 70 + "@example.com"):
        with pytest.raises(TenancyError):
            domain.normalize_email(bad)


# ─── A./F. descriptor + L. TenantContext issuance ──────────────────────────


def _ready_descriptor(org: uuid.UUID | None = None) -> TenantDatabaseDescriptor:
    return TenantDatabaseDescriptor(org or uuid.uuid4(), domain.generate_tenant_database_name(), "asia-south1",
                                    TenantDatabaseStatus.READY, "0004_manager_dashboard_auth")


def test_descriptor_validates_everything():
    with pytest.raises(TenancyError):
        TenantDatabaseDescriptor(uuid.uuid4(), "postgres", "asia-south1", TenantDatabaseStatus.READY)
    with pytest.raises(TenancyError):
        TenantDatabaseDescriptor("zaza-studio", domain.generate_tenant_database_name(), "asia-south1", "READY")
    with pytest.raises(TenancyError):
        TenantDatabaseDescriptor(uuid.uuid4(), domain.generate_tenant_database_name(), "asia south", "READY")
    d = _ready_descriptor()
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.organization_id = uuid.uuid4()  # identifiers are immutable


def test_tenant_context_cannot_be_built_outside_the_resolver():
    d = _ready_descriptor()
    with pytest.raises(TenancyError):
        TenantContext(organization_id=d.organization_id, slug="acme", database=d, kind=ContextKind.JOB, role=None,
                      user_id=None, _issuer=object())
    with pytest.raises(TypeError):
        TenantContext(organization_id=d.organization_id, slug="acme", database=d, kind=ContextKind.JOB, role=None,
                      user_id=None)


def test_tenant_context_is_immutable_and_not_substitutable():
    directory = FakeDirectory()
    a, b = directory.org("tenant-a"), directory.org("tenant-b")
    ctx = TenantResolver(directory).for_job(a)
    for name, value in (("organization_id", b), ("slug", "tenant-b"), ("database", directory.dbs[b])):
        with pytest.raises(AttributeError):
            setattr(ctx, name, value)
    assert copy.copy(ctx) is ctx and copy.deepcopy(ctx) is ctx
    with pytest.raises(TypeError):
        pickle.dumps(ctx)
    with pytest.raises(TypeError):
        dataclasses.replace(ctx, slug="tenant-b")  # not a dataclass: no replace()


def test_context_refuses_a_database_of_another_organization():
    from deskmate.zaza_server.tenancy.context import issue_context

    org_a = uuid.uuid4()
    with pytest.raises(TenancyError):
        issue_context(organization_id=org_a, slug="tenant-a", database=_ready_descriptor(uuid.uuid4()),
                      kind=ContextKind.JOB, role=None, user_id=None)
    not_ready = TenantDatabaseDescriptor(org_a, domain.generate_tenant_database_name(), "asia-south1",
                                         TenantDatabaseStatus.PROVISIONING)
    with pytest.raises(TenancyError):
        issue_context(organization_id=org_a, slug="tenant-a", database=not_ready, kind=ContextKind.JOB, role=None,
                      user_id=None)


# ─── K. connections come from server configuration only ───────────────────


def test_tenant_connection_settings_change_only_the_validated_database_name():
    base = DatabaseSettings(host="10.1.2.3", port=5432, dbname="ignored", user="zaza_app", password="s3cret-pw",
                            sslmode="verify-full", schema="ignored_schema")
    directory = FakeDirectory()
    ctx = TenantResolver(directory).for_job(directory.org("tenant-a"))
    settings = tenant_database_settings(base, ctx)
    assert settings.dbname == ctx.database.database_name and settings.schema is None
    assert (settings.host, settings.port, settings.user, settings.password, settings.sslmode) == \
        ("10.1.2.3", 5432, "zaza_app", "s3cret-pw", "verify-full")
    import inspect

    assert list(inspect.signature(tenant_database_settings).parameters) == ["base", "context"]


def test_tenant_connection_settings_need_a_resolved_context():
    base = DatabaseSettings(user="zaza_app")
    for fake in ("zaza_t_abcdefghijklmnopqrstuvwxyz", {"dbname": "postgres"}, _ready_descriptor(),
                 "postgresql://evil@host/db"):
        with pytest.raises(TenancyError):
            tenant_database_settings(base, fake)


# ─── manager resolution: E., membership A never authorizes B ───────────────


def test_membership_authorizes_only_its_own_organization():
    directory = FakeDirectory()
    a, b = directory.org("tenant-a"), directory.org("tenant-b")
    alice = directory.user()
    directory.member(a, alice, "OWNER")
    resolver = TenantResolver(directory)
    ctx = resolver.for_member(alice, a)
    assert ctx.organization_id == a and ctx.role is MembershipRole.OWNER and ctx.kind is ContextKind.MEMBER
    assert ctx.database is directory.dbs[a]
    with pytest.raises(TenantAccessDenied):
        resolver.for_member(alice, b)


def test_a_user_may_belong_to_several_organizations_with_different_roles():
    directory = FakeDirectory()
    a, b = directory.org("tenant-a"), directory.org("tenant-b")
    consultant = directory.user()
    directory.member(a, consultant, "ADMIN")
    directory.member(b, consultant, "VIEWER")
    resolver = TenantResolver(directory)
    ctx_a, ctx_b = resolver.for_member(consultant, a), resolver.for_member(consultant, b)
    assert (ctx_a.role, ctx_b.role) == (MembershipRole.ADMIN, MembershipRole.VIEWER)
    assert ctx_a.database.database_name != ctx_b.database.database_name


@pytest.mark.parametrize("case", ["suspended org", "closed org", "unknown org", "db provisioning", "db suspended",
                                  "db error", "no db", "disabled user", "disabled membership", "slug as id",
                                  "db name as id"])
def test_unusable_tenants_never_produce_a_context(case):
    directory = FakeDirectory()
    org_status = {"suspended org": OrganizationStatus.SUSPENDED, "closed org": OrganizationStatus.CLOSED}.get(
        case, OrganizationStatus.ACTIVE)
    db_status = {"db provisioning": TenantDatabaseStatus.PROVISIONING, "db suspended": TenantDatabaseStatus.SUSPENDED,
                 "db error": TenantDatabaseStatus.ERROR, "no db": None}.get(case, TenantDatabaseStatus.READY)
    org = directory.org("tenant-a", status=org_status, db_status=db_status)
    user = directory.user(status=UserStatus.DISABLED if case == "disabled user" else UserStatus.ACTIVE)
    directory.member(org, user, "OWNER", MembershipStatus.DISABLED if case == "disabled membership"
                     else MembershipStatus.ACTIVE)
    target = {"unknown org": uuid.uuid4(), "slug as id": "tenant-a",
              "db name as id": directory.dbs[org].database_name if org in directory.dbs else "x"}.get(case, org)
    resolver = TenantResolver(directory)
    with pytest.raises(TenantAccessDenied):
        resolver.for_member(user, target)
    if case not in ("disabled user", "disabled membership"):
        with pytest.raises(TenantAccessDenied):
            resolver.for_job(target)


# ─── agent resolution: the credential decides the organization ─────────────


class Router:
    def __init__(self, mapping: dict[str, uuid.UUID]) -> None:
        self.mapping = mapping

    def organization_for_credential(self, token_hash: str):  # noqa: ANN201
        return self.mapping.get(token_hash)


def _tenant_auth(tokens_by_org: dict[uuid.UUID, dict[str, str]], calls: list):  # noqa: ANN202
    """Stand-in for opening the tenant DB and running auth.authenticate_request there."""
    def authenticate(ctx, token, device_id):  # noqa: ANN001, ANN202
        calls.append(ctx.organization_id)
        if tokens_by_org.get(ctx.organization_id, {}).get(token) != device_id:
            raise TenantAccessDenied("invalid credentials")
        return {"device_id": device_id, "organization_id": ctx.organization_id}
    return authenticate


def test_device_is_resolved_from_its_credential_and_checked_inside_that_tenant():
    from deskmate.zaza_server.tenancy.device_routes import routing_hash as hash_token

    directory = FakeDirectory()
    a, b = directory.org("tenant-a"), directory.org("tenant-b")
    token_a, token_b = "zzd_" + "a" * 43, "zzd_" + "b" * 43
    router = Router({hash_token(token_a): a, hash_token(token_b): b})
    calls: list = []
    auth = _tenant_auth({a: {token_a: "device-1"}, b: {token_b: "device-1"}}, calls)
    resolver = TenantResolver(directory)
    result = resolver.for_device(router, token_a, "device-1", auth)
    assert result.context.organization_id == a and result.context.kind is ContextKind.DEVICE and calls == [a]
    assert resolver.for_device(router, token_b, "device-1", auth).context.organization_id == b


def test_client_claimed_organization_never_overrides_the_credential():
    """Valid token of organization A + 'organization=B' -> refused, and B's
    database is never opened."""
    from deskmate.zaza_server.tenancy.device_routes import routing_hash as hash_token

    directory = FakeDirectory()
    a, b = directory.org("tenant-a"), directory.org("tenant-b")
    token_a = "zzd_" + "a" * 43
    calls: list = []
    auth = _tenant_auth({a: {token_a: "device-1"}, b: {}}, calls)
    resolver = TenantResolver(directory)
    for claim in (b, str(b), "tenant-b", directory.dbs[b].database_name):
        with pytest.raises(TenantAccessDenied):
            resolver.for_device(Router({hash_token(token_a): a}), token_a, "device-1", auth,
                                claimed_organization_id=claim)
    assert calls == []  # no tenant database was touched
    assert resolver.for_device(Router({hash_token(token_a): a}), token_a, "device-1", auth,
                               claimed_organization_id=a).context.organization_id == a


def test_unknown_credentials_and_unusable_tenants_are_refused_for_devices():
    from deskmate.zaza_server.tenancy.device_routes import routing_hash as hash_token

    directory = FakeDirectory()
    suspended = directory.org("tenant-s", status=OrganizationStatus.SUSPENDED)
    token = "zzd_" + "s" * 43
    calls: list = []
    auth = _tenant_auth({suspended: {token: "device-1"}}, calls)
    resolver = TenantResolver(directory)
    with pytest.raises(TenantAccessDenied):
        resolver.for_device(Router({}), token, "device-1", auth)
    with pytest.raises(TenantAccessDenied):
        resolver.for_device(Router({hash_token(token): suspended}), token, "device-1", auth)
    for bad_token, bad_device in (("", "device-1"), ("   ", "device-1"), (token, "")):
        with pytest.raises(TenantAccessDenied):
            resolver.for_device(Router({hash_token(token): suspended}), bad_token, bad_device, auth)
    assert calls == []


# ─── per-tenant jobs ───────────────────────────────────────────────────────


def test_jobs_run_once_per_ready_tenant_and_failures_stay_in_their_tenant():
    directory = FakeDirectory()
    a, b = directory.org("tenant-a"), directory.org("tenant-b")
    directory.org("tenant-c", db_status=TenantDatabaseStatus.PROVISIONING)
    directory.org("tenant-d", status=OrganizationStatus.SUSPENDED)
    seen: list = []

    def job(ctx):  # noqa: ANN001, ANN202
        seen.append((ctx.organization_id, ctx.database.database_name))
        if ctx.organization_id == a:
            raise RuntimeError("tenant A failed")
        return "ok"

    outcomes = {o.organization_id: o for o in run_per_tenant(TenantResolver(directory), job)}
    assert set(outcomes) == {a, b}
    assert not outcomes[a].ok and outcomes[a].error == "RuntimeError"
    assert outcomes[b].ok and outcomes[b].result == "ok"
    assert dict(seen) == {a: directory.dbs[a].database_name, b: directory.dbs[b].database_name}


# ─── J. SaaS roles are independent of employees.role ──────────────────────


def _code_files() -> list[Path]:
    return [p for p in TENANCY.rglob("*.py") if "migrations" not in p.parts]


def test_tenancy_code_never_reads_employee_roles_or_activity_tables():
    activity = ("employees", "devices", "device_tokens", "work_sessions", "activity_periods", "idle_periods",
                "daily_summaries", "weekly_summaries", "monthly_summaries", "audit_logs", "application_usage_daily",
                "work_schedules", "manager_users", "manager_sessions")
    for path in _code_files():
        strings = [n.value for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        sql = [s for s in strings if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN)\b", s)]
        for statement in sql:
            for table in activity:
                assert not re.search(rf"\b(FROM|JOIN|INTO|UPDATE)\s+{table}\b", statement, re.IGNORECASE), \
                    f"{path.name}: control plane must not touch tenant table {table}"
        if path.name == "employees.py":
            # the tenant-aware employee operator (10G-E-A) handles EmployeeRecords, but only those returned by
            # a resolved tenant repository: it holds no SQL at all and never opens a database itself
            assert sql == [] and "psycopg" not in path.read_text(encoding="utf-8")
            assert "manager.repository(context)" in path.read_text(encoding="utf-8")
            continue
        assert "EmployeeRecord" not in path.read_text(encoding="utf-8"), path.name


def test_employee_role_values_do_not_become_saas_roles():
    from deskmate.zaza_server.repository import check_employee_fields

    check_employee_fields("Alice", "ADMIN", "UTC")  # a valid employee classification ...
    with pytest.raises(TenancyError):
        domain.membership_role("EMPLOYEE")          # ... and EMPLOYEE is no SaaS role
    directory = FakeDirectory()
    org = directory.org("tenant-a")
    no_membership_user = directory.user()
    with pytest.raises(TenantAccessDenied):         # being an employee 'ADMIN' grants nothing
        TenantResolver(directory).for_member(no_membership_user, org)


# ─── no cross-tenant API in normal code ────────────────────────────────────


def test_no_cross_tenant_listing_api_exists():
    banned = re.compile(r"(all_tenants|cross_tenant|every_tenant|all_organizations_activity|list_all_employees|"
                        r"list_all_activity|global_activity)", re.IGNORECASE)
    for path in [*_code_files(), *SERVER.glob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                assert not banned.search(node.name), f"{path.name}: {node.name}"
    from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository

    public = {n for n in dir(PostgresControlRepository) if not n.startswith("_")}
    assert not {n for n in public if re.search(r"employee|device|activity|session|record", n)}


def test_request_handlers_cannot_build_tenant_contexts():
    """Only resolver.py may call issue_context; nothing else imports it."""
    for path in [*_code_files(), *SERVER.rglob("*.py")]:
        if path.name in ("resolver.py", "context.py") or "migrations" in path.parts:
            continue
        assert "issue_context" not in path.read_text(encoding="utf-8"), path


# ─── migrations: separate chains, tenant schema untouched ─────────────────

TENANT_REVISION_SHA256 = {  # the approved tenant schema template; Phase 10C must not change it
    "0001_initial_schema.py": "3da655b1acbc7f640664f1229d89109f2f2a8459aa2fc1c59323efa2df703a67",
    "0002_attendance_summaries.py": "ffa20e551e50db885ddac29abf72f6cfbd472ffeddcb6dd9abf1bc1a2bc106f3",
    "0003_schedule_timezone_matches_employee.py": "f2f4447ca13e24881a338d91086c115e57f02d088a143a798fdb92b19a72a68f",
    "0004_manager_dashboard_auth.py": "c610cd8b78caabb232fe8b0cf6bec40dcc260d49fbf3ade1e3715e466d439fa0",
}


def test_tenant_migrations_are_unchanged_and_still_four():
    versions = SERVER / "postgres" / "migrations" / "versions"
    files = sorted(p.name for p in versions.glob("*.py"))
    assert files == sorted(TENANT_REVISION_SHA256)
    for name, digest in TENANT_REVISION_SHA256.items():
        data = (versions / name).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == digest, f"{name} was modified"


CONTROL_C0001_SHA256 = "41dab6c2b8ac6bb6f7791d3bf8b7ba3a4d2cc420b773f64421d6e77b3c477273"  # applied in production
CONTROL_C0002_SHA256 = "4ba4804eba87305da252d7a47fc420635cdb65766918c39d1c8de5cef9a111a3"  # applied in production (10F-C)


def test_c0001_is_unchanged_and_revision_ids_fit_the_version_column():
    versions = TENANCY / "control" / "migrations" / "versions"
    data = (versions / "c0001_control_plane.py").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == CONTROL_C0001_SHA256, "c0001 was modified"
    data2 = (versions / "c0002_saas_auth_routing.py").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data2).hexdigest() == CONTROL_C0002_SHA256, "c0002 was modified"
    for path in versions.glob("c*.py"):
        rev = re.search(r'^revision = "([^"]+)"', path.read_text(encoding="utf-8"), re.MULTILINE).group(1)
        assert len(rev) <= 32, f"{rev}: alembic version_num is VARCHAR(32)"


def test_control_and_tenant_chains_are_separate():
    from deskmate.zaza_server.postgres import migrate as tenant
    from deskmate.zaza_server.tenancy.control import migrate as control

    assert control.MIGRATIONS_DIR != tenant.MIGRATIONS_DIR
    assert control.VERSION_TABLE == "zaza_control_version" != tenant.VERSION_TABLE
    assert control.head_revision() == "c0003_device_enrollment"
    assert tenant.head_revision() == "0004_manager_dashboard_auth"
    control_revs = {p.stem for p in (control.MIGRATIONS_DIR / "versions").glob("*.py")}
    assert all(r.startswith("c") for r in control_revs)
    tenant_sql, control_sql = tenant.render_sql(), control.render_sql()
    for table in ("organizations", "users", "organization_memberships", "tenant_databases", "control_audit_logs"):
        assert f"CREATE TABLE {table}" in control_sql and f"CREATE TABLE {table} " not in tenant_sql
    for table in ("employees", "devices", "activity_periods", "manager_users"):
        assert f"CREATE TABLE {table}" in tenant_sql and f"CREATE TABLE {table} " not in control_sql
    assert "organization_id" not in tenant_sql  # no organization_id sprinkled over activity tables


def test_control_schema_constraints_match_the_domain():
    text = (TENANCY / "control" / "migrations" / "versions" / "c0001_control_plane.py").read_text(encoding="utf-8")
    ns: dict = {}
    exec(compile(re.search(r"^ACTIONS = \(.*?\)\n", text, re.MULTILINE | re.DOTALL).group(0), "c0001", "exec"), ns)
    assert set(ns["ACTIONS"]) == domain.C0001_AUDIT_ACTIONS
    c2 = (TENANCY / "control" / "migrations" / "versions" / "c0002_saas_auth_routing.py").read_text(encoding="utf-8")
    exec(compile(re.search(r"^NEW_ACTIONS = \(.*?\)\n", c2, re.MULTILINE | re.DOTALL).group(0), "c0002", "exec"), ns)
    assert set(ns["NEW_ACTIONS"]) == domain.C0002_AUDIT_ACTIONS
    c3 = (TENANCY / "control" / "migrations" / "versions" / "c0003_device_enrollment.py").read_text(encoding="utf-8")
    ns3: dict = {}
    exec(compile(re.search(r"^NEW_ACTIONS = \(.*?\)\n", c3, re.MULTILINE | re.DOTALL).group(0), "c0003", "exec"), ns3)
    assert set(ns3["NEW_ACTIONS"]) == domain.C0003_AUDIT_ACTIONS
    assert domain.AUDIT_ACTIONS == (domain.C0001_AUDIT_ACTIONS | domain.C0002_AUDIT_ACTIONS
                                    | domain.C0003_AUDIT_ACTIONS)
    assert "'OWNER', 'ADMIN', 'MANAGER', 'VIEWER'" in text and "'EMPLOYEE'" not in text
    columns = re.findall(r"^\s{8}([a-z_]+)\s+(?:uuid|text|jsonb|timestamptz)\b", text, re.MULTILINE)
    assert columns and not [c for c in columns if re.search(r"password|secret|token|key|credential|host|url", c)]
    instants = re.findall(r"^\s{8}([a-z_]+_at)\s+(\w+)", text, re.MULTILINE)
    assert len(instants) == 9 and all(kind == "timestamptz" for _, kind in instants)  # UTC instants only


def test_control_migrations_are_never_run_by_serve():
    main = (SERVER / "__main__.py").read_text(encoding="utf-8")
    serve_block = main[main.index("def _serve_prepare"):main.index("elif args.command == \"add-employee\"")]
    assert "upgrade" not in serve_block and "control" not in serve_block
    entry = (ROOT / "deploy" / "gcp" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "control-migrate" not in entry


def test_control_migrate_sql_cli_needs_no_database():
    result = subprocess.run([sys.executable, "-m", "deskmate.zaza_server", "control-migrate", "--sql"],
                            capture_output=True, text=True, check=False, cwd=ROOT)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "CREATE TABLE organizations" in result.stdout and "zaza_control_version" in result.stdout


# ─── control database configuration ────────────────────────────────────────


def test_control_database_must_be_configured_separately():
    from deskmate.zaza_server.tenancy.config import control_database_settings_from_env

    with pytest.raises(ConfigError):
        control_database_settings_from_env({"ZAZA_DB_NAME": "zaza"})
    with pytest.raises(ConfigError):
        control_database_settings_from_env({"ZAZA_CONTROL_DB_NAME": "zaza"})  # the activity database
    with pytest.raises(ConfigError):
        control_database_settings_from_env({"ZAZA_CONTROL_DB_NAME": "zaza_main", "ZAZA_DB_NAME": "zaza_main"})
    with pytest.raises(ConfigError):
        control_database_settings_from_env({"ZAZA_CONTROL_DB_NAME": domain.generate_tenant_database_name()})
    s = control_database_settings_from_env({"ZAZA_CONTROL_DB_NAME": "zaza_control", "ZAZA_CONTROL_DB_USER": "ctl",
                                            "ZAZA_DB_NAME": "zaza", "ZAZA_DB_USER": "tenant_user"})
    assert (s.dbname, s.user, s.pool_max) == ("zaza_control", "ctl", 2)
