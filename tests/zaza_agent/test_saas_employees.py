"""Phase 10G-E-A: tenant-aware SaaS employee operator, without a database.

Slug -> control plane -> TenantResolver.for_job -> TenantRepositoryManager ->
the tenant's repository; no database input, no fallback, no legacy
add-employee path. The PostgreSQL proof (same employee id in two tenants,
refusals, control/maintenance database untouched, bootstrap sequence) is in
test_saas_employees_postgres.py (opt-in: ZAZA_TEST_POSTGRES_URL)."""

from __future__ import annotations

import ast
import contextlib
import inspect
import uuid
from pathlib import Path

import pytest

from deskmate.zaza_server.config import DatabaseSettings
from deskmate.zaza_server.repository import EmployeeRecord, RepositoryUnavailable
from deskmate.zaza_server.tenancy import domain
from deskmate.zaza_server.tenancy.context import TenantDatabaseDescriptor
from deskmate.zaza_server.tenancy.control_repository import Organization
from deskmate.zaza_server.tenancy.domain import (
    OrganizationStatus,
    TenancyError,
    TenantDatabaseStatus,
)
from deskmate.zaza_server.tenancy.employees import OPERATOR_ACTOR, SaasEmployeeOperator
from deskmate.zaza_server.tenancy.resolver import TenantResolver

ROOT = Path(__file__).resolve().parents[2]
EMPLOYEES_PY = ROOT / "deskmate" / "zaza_server" / "tenancy" / "employees.py"
COMMANDS = ("saas-employee-create", "saas-employee-list", "saas-employee-disable", "saas-employee-enable")


class Directory:
    """Control directory fake: organizations by slug + tenant descriptors."""

    def __init__(self) -> None:
        self.orgs: dict[uuid.UUID, Organization] = {}
        self.dbs: dict[uuid.UUID, TenantDatabaseDescriptor] = {}

    def org(self, slug: str, *, status=OrganizationStatus.ACTIVE, db_status=TenantDatabaseStatus.READY):  # noqa: ANN001
        oid = uuid.uuid4()
        self.orgs[oid] = Organization(oid, slug, slug.title(), status, "UTC")
        if db_status is not None:
            self.dbs[oid] = TenantDatabaseDescriptor(oid, domain.generate_tenant_database_name(), "asia-south1",
                                                     db_status, "0004_manager_dashboard_auth")
        return oid

    def find_organization_id_by_slug(self, slug):  # noqa: ANN001, ANN201
        return next((o for o, org in self.orgs.items() if org.slug == slug), None)

    def get_organization(self, organization_id):  # noqa: ANN001, ANN201
        return self.orgs.get(organization_id)

    def get_tenant_database(self, organization_id):  # noqa: ANN001, ANN201
        return self.dbs.get(organization_id)


class Repo:
    def __init__(self) -> None:
        self.rows: dict[str, EmployeeRecord] = {}

    def add_employee(self, employee_id, display_name, *, role="EMPLOYEE", timezone="UTC"):  # noqa: ANN001, ANN201
        if employee_id in self.rows:
            raise ValueError(f"employee already exists: {employee_id}")
        self.rows[employee_id] = EmployeeRecord(employee_id, display_name, role, True, timezone, "now")
        return self.rows[employee_id]

    def list_employees(self):  # noqa: ANN201
        return sorted(self.rows.values(), key=lambda e: e.employee_id)

    def set_employee_active(self, employee_id, active):  # noqa: ANN001, ANN201
        e = self.rows[employee_id]
        self.rows[employee_id] = EmployeeRecord(e.employee_id, e.display_name, e.role, active, e.timezone, e.created_at)
        return self.rows[employee_id]


class Manager:
    """Records which database each lease was for; one repository per database."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.repos: dict[str, Repo] = {}
        self.leases: list[str] = []
        self.fail = fail

    @contextlib.contextmanager
    def repository(self, context):  # noqa: ANN001, ANN201
        name = context.database.database_name
        self.leases.append(name)
        if self.fail is not None:
            raise self.fail
        yield self.repos.setdefault(name, Repo())


def _operator(manager=None):  # noqa: ANN001, ANN202
    d = Directory()
    m = manager or Manager()
    return d, m, SaasEmployeeOperator(d, TenantResolver(d), m)


def test_same_employee_id_in_two_organizations_goes_to_each_own_tenant_database():
    d, m, op = _operator()
    a, b = d.org("tenant-a"), d.org("tenant-b")
    op.create("tenant-a", "employee-1", "Alice", timezone="Asia/Dhaka")
    op.create("Tenant-B", "employee-1", "Bob", timezone="UTC", role="MANAGER")
    db_a, db_b = d.dbs[a].database_name, d.dbs[b].database_name
    assert m.leases == [db_a, db_b]
    assert [e.display_name for e in op.list("tenant-a")] == ["Alice"]
    assert [(e.display_name, e.role) for e in op.list("tenant-b")] == [("Bob", "MANAGER")]
    op.set_active("tenant-a", "employee-1", False)
    assert [e.is_active for e in op.list("tenant-a")] == [False]
    assert [e.is_active for e in op.list("tenant-b")] == [True]  # B's same id untouched
    assert set(m.leases) == {db_a, db_b}


@pytest.mark.parametrize("case", ["unknown", "suspended", "closed", "no-tenant", "provisioning", "error",
                                  "tenant-suspended"])
def test_unusable_organizations_are_refused_before_any_database_is_opened(case):
    d, m, op = _operator()
    if case == "suspended":
        d.org("org-x", status=OrganizationStatus.SUSPENDED)
    elif case == "closed":
        d.org("org-x", status=OrganizationStatus.CLOSED)
    elif case == "no-tenant":
        d.org("org-x", db_status=None)
    elif case == "provisioning":
        d.org("org-x", db_status=TenantDatabaseStatus.PROVISIONING)
    elif case == "error":
        d.org("org-x", db_status=TenantDatabaseStatus.ERROR)
    elif case == "tenant-suspended":
        d.org("org-x", db_status=TenantDatabaseStatus.SUSPENDED)
    for call in (lambda: op.create("org-x", "employee-1", "X", timezone="UTC"), lambda: op.list("org-x"),
                 lambda: op.set_active("org-x", "employee-1", False)):
        with pytest.raises(TenancyError):
            call()
    assert m.leases == []


def test_a_tenant_database_failure_is_refused_without_naming_the_database():
    d = Directory()
    oid = d.org("org-x")
    name = d.dbs[oid].database_name
    m = Manager(fail=RepositoryUnavailable(f"cannot connect to host/{name}"))
    op = SaasEmployeeOperator(d, TenantResolver(d), m)
    with pytest.raises(TenancyError) as err:
        op.list("org-x")
    assert name not in str(err.value) and "not usable" in str(err.value)
    assert m.leases == [name]  # tried exactly that tenant, nothing else


@pytest.mark.parametrize(("field", "value"), [
    ("employee_id", ""), ("employee_id", "has space"), ("employee_id", "bad\x00id"), ("employee_id", "a/b"),
    ("employee_id", "x" * 129), ("name", ""), ("name", "   "), ("name", "bad\x07name"), ("name", "line\nbreak\x1b"),
    ("timezone", "Mars/Olympus"), ("timezone", ""), ("role", "OWNER"), ("role", "employee"), ("role", "VIEWER"),
])
def test_invalid_employee_input_is_refused_before_resolution(field, value):
    d, m, op = _operator()
    d.org("org-x")
    args = {"employee_id": "employee-1", "name": "Valid Name", "timezone": "UTC", "role": "EMPLOYEE", field: value}
    with pytest.raises(TenancyError):
        op.create("org-x", args["employee_id"], args["name"], timezone=args["timezone"], role=args["role"])
    assert m.leases == []


def test_duplicate_employee_id_in_the_same_organization_is_refused():
    d, m, op = _operator()
    d.org("org-x")
    op.create("org-x", "employee-1", "Alice", timezone="UTC")
    with pytest.raises(ValueError, match="already exists"):
        op.create("org-x", "employee-1", "Alice again", timezone="UTC")


@pytest.mark.parametrize("bad_slug", ["", "ab", "../x", "org x;drop", None])
def test_bad_slugs_are_refused(bad_slug):
    d, m, op = _operator()
    with pytest.raises(TenancyError):
        op.list(bad_slug)
    assert m.leases == []


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("option", ["--database", "--tenant-database", "--db", "--database-url", "--host",
                                    "--organization", "--organization-id", "--dbname"])
def test_cli_takes_no_database_or_organization_id_input(command, option):
    from deskmate.zaza_server.__main__ import main

    argv = [command, "--slug", "org-x", option, "zaza_t_aaaaaaaaaaaaaaaaaaaaaaaaaa"]
    if command != "saas-employee-list":
        argv += ["--employee-id", "employee-1"]
    if command == "saas-employee-create":
        argv += ["--name", "X", "--timezone", "UTC"]
    with pytest.raises(SystemExit) as exit_:
        main(argv)
    assert exit_.value.code == 2  # argparse: unrecognized argument


def test_cli_requires_slug_and_offers_only_employee_roles():
    from deskmate.zaza_server.__main__ import main

    with pytest.raises(SystemExit):
        main(["saas-employee-list"])
    with pytest.raises(SystemExit):
        main(["saas-employee-create", "--slug", "org-x", "--employee-id", "e", "--name", "X", "--timezone", "UTC",
              "--role", "OWNER"])


def test_new_cli_path_never_uses_the_legacy_single_database_repository(monkeypatch):
    """Production zaza-migrate has ZAZA_DB_NAME=postgres: the new commands must
    never reach open_repository / a repository on the base database."""
    import deskmate.zaza_server.__main__ as cli
    import deskmate.zaza_server.backends as backends
    from deskmate.zaza_server.tenancy import connections, control_repository

    def forbidden(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy single-database path used")

    monkeypatch.setattr(backends, "open_repository", forbidden)
    monkeypatch.setattr(cli, "_run", forbidden)
    d = Directory()
    a = d.org("tenant-a")
    leased: list[str] = []
    base_seen: list[DatabaseSettings] = []

    class FakeControl:
        def __init__(self, settings, **_k):  # noqa: ANN001, ANN003
            pass

        def __getattr__(self, name):  # noqa: ANN001, ANN204
            return getattr(d, name)

        def close(self) -> None:
            pass

    real_manager = connections.TenantRepositoryManager

    def manager(base, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        base_seen.append(base)
        repo = Repo()

        def factory(settings):  # noqa: ANN001, ANN202
            leased.append(settings.dbname)
            return repo

        kwargs["repository_factory"] = factory
        return real_manager(base, *args, **kwargs)

    monkeypatch.setattr(control_repository, "PostgresControlRepository", FakeControl)
    monkeypatch.setattr(connections, "TenantRepositoryManager", manager)
    monkeypatch.setenv("ZAZA_DB_NAME", "postgres")
    monkeypatch.setenv("ZAZA_DB_USER", "zaza_migrator")
    monkeypatch.setenv("ZAZA_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("ZAZA_CONTROL_DB_NAME", "zaza_control")
    rc = cli.main(["saas-employee-create", "--slug", "tenant-a", "--employee-id", "employee-1", "--name", "A",
                   "--timezone", "Asia/Dhaka"])
    assert rc == 0
    assert base_seen and base_seen[0].dbname == "postgres"
    assert leased == [d.dbs[a].database_name]  # only the resolved tenant, never postgres or zaza_control
    assert "postgres" not in leased and "zaza_control" not in leased


def test_employee_module_has_no_legacy_or_operator_database_input():
    source = EMPLOYEES_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert not imported & {"open_repository", "database_settings_from_env", "PostgresRepository", "psycopg"}
    assert "ZAZA_DB_NAME" not in source.replace("``ZAZA_DB_NAME``", "")
    assert "for_job" in source and "find_organization_id_by_slug" in source
    params = {p for fn in (SaasEmployeeOperator.create, SaasEmployeeOperator.list, SaasEmployeeOperator.set_active)
              for p in inspect.signature(fn).parameters}
    assert not {p for p in params if "database" in p or "host" in p or "url" in p or p == "organization_id"}


def test_cli_employee_handler_does_not_touch_the_legacy_path():
    import deskmate.zaza_server.__main__ as cli

    source = inspect.getsource(cli._saas_employees)
    for forbidden in ("open_repository", "add-employee", "_run(", "ZAZA_DB_NAME", "os.environ"):
        assert forbidden not in source.replace("(add-employee)", "")
    assert "operator_repository_factory" in source and "SaasEmployeeOperator" in source


def test_operator_audit_actor_is_the_trusted_operator_path():
    assert OPERATOR_ACTOR == ("CLI", "saas-operator")
