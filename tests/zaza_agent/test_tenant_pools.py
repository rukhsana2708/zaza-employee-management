"""Phase 10D: the tenant connection manager, the tenant migration runner and
the provisioning boundary — with fake repositories (no database). The real
PostgreSQL proof is in test_tenant_pools_postgres.py (opt-in)."""

from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from deskmate.zaza_server.config import ConfigError, DatabaseSettings
from deskmate.zaza_server.repository import RepositoryUnavailable
from deskmate.zaza_server.tenancy import domain
from deskmate.zaza_server.tenancy.connections import (
    TENANT_POOL_CACHE_MAX_LIMIT,
    TENANT_POOL_MAX_LIMIT,
    TenantPoolLimits,
    TenantPoolsExhausted,
    TenantRepositoryManager,
    connection_budget,
    tenant_pool_limits_from_env,
)
from deskmate.zaza_server.tenancy.context import TenantDatabaseDescriptor
from deskmate.zaza_server.tenancy.domain import OrganizationStatus, TenancyError, TenantDatabaseStatus
from deskmate.zaza_server.tenancy.resolver import TenantAccessDenied, TenantResolver

from .test_tenancy import FakeDirectory

FAKE_PASSWORD = "not-a-real-password-77e0"  # test value only
BASE = DatabaseSettings(host="/cloudsql/zaza-employee-monitor:asia-south1:zaza-pg", dbname="zaza",
                        user="zaza_app", password=FAKE_PASSWORD, pool_min=1, pool_max=4)


class FakeRepo:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        self.closed = False

    def close(self) -> None:
        assert not self.closed, "closed twice"
        self.closed = True


class Factory:
    """Records every repository it opens; can be told to fail for a database."""

    def __init__(self) -> None:
        self.opened: list[FakeRepo] = []
        self.fail: set[str] = set()
        self.delay = 0.0
        self.lock = threading.Lock()

    def __call__(self, settings: DatabaseSettings) -> FakeRepo:
        if self.delay:
            time.sleep(self.delay)
        if settings.dbname in self.fail:
            raise RepositoryUnavailable(f"database unavailable ({settings.display()})")
        repo = FakeRepo(settings)
        with self.lock:
            self.opened.append(repo)
        return repo

    def open_count(self) -> int:
        return sum(not r.closed for r in self.opened)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def world():
    directory = FakeDirectory()
    resolver = TenantResolver(directory)
    orgs = [directory.org(f"tenant-{c}") for c in "abcdefghij"]
    contexts = [resolver.for_job(o) for o in orgs]
    return SimpleNamespace(directory=directory, resolver=resolver, orgs=orgs, ctx=contexts)


def _manager(factory, clock=None, **limits) -> TenantRepositoryManager:  # noqa: ANN001
    return TenantRepositoryManager(BASE, TenantPoolLimits(**limits), repository_factory=factory,
                                   clock=clock or Clock())


# ─── limits and budget ──────────────────────────────────────────────────────


def test_default_limits_are_small():
    limits = TenantPoolLimits()
    assert (limits.pool_max, limits.cache_max, limits.idle_seconds) == (2, 8, 300)
    assert limits.connections_per_instance == 16
    assert tenant_pool_limits_from_env({}) == limits


@pytest.mark.parametrize(("env", "message"), [
    ({"ZAZA_TENANT_POOL_MAX": "0"}, "ZAZA_TENANT_POOL_MAX"),
    ({"ZAZA_TENANT_POOL_MAX": str(TENANT_POOL_MAX_LIMIT + 1)}, "ZAZA_TENANT_POOL_MAX"),
    ({"ZAZA_TENANT_POOL_MAX": "two"}, "ZAZA_TENANT_POOL_MAX"),
    ({"ZAZA_TENANT_POOL_CACHE_MAX": "0"}, "ZAZA_TENANT_POOL_CACHE_MAX"),
    ({"ZAZA_TENANT_POOL_CACHE_MAX": str(TENANT_POOL_CACHE_MAX_LIMIT + 1)}, "ZAZA_TENANT_POOL_CACHE_MAX"),
    ({"ZAZA_TENANT_POOL_IDLE_SECONDS": "1"}, "ZAZA_TENANT_POOL_IDLE_SECONDS"),
    ({"ZAZA_TENANT_POOL_IDLE_SECONDS": "86400"}, "ZAZA_TENANT_POOL_IDLE_SECONDS"),
])
def test_limits_have_hard_upper_bounds(env, message):
    with pytest.raises(ConfigError, match=message):
        tenant_pool_limits_from_env(env)


def test_connection_budget_formula():
    budget = connection_budget(max_instances=3, limits=TenantPoolLimits(), capacity=70, other_per_instance=2)
    assert (budget.tenant_per_instance, budget.control_per_instance, budget.per_instance) == (16, 2, 20)
    assert budget.total == 60 and budget.ok
    assert "3 instance(s) x (16 tenant + 2 control + 2 other) = 60 of 70" in budget.describe()
    # horizontal scaling multiplies everything: never assume one instance
    assert not connection_budget(max_instances=4, limits=TenantPoolLimits(), capacity=70, other_per_instance=2).ok
    worst = connection_budget(max_instances=100, limits=TenantPoolLimits(pool_max=TENANT_POOL_MAX_LIMIT,
                                                                        cache_max=TENANT_POOL_CACHE_MAX_LIMIT),
                              capacity=100)
    assert worst.total == 100 * (4 * 64 + 2) and not worst.ok
    with pytest.raises(ConfigError):
        connection_budget(max_instances=0, limits=TenantPoolLimits(), capacity=10)


# ─── lazy creation, reuse, settings ─────────────────────────────────────────


def test_pools_are_created_lazily_and_reused(world):
    factory = Factory()
    manager = _manager(factory)
    assert factory.opened == [] and manager.stats()["open_pools"] == 0  # organizations exist, no pools
    with manager.repository(world.ctx[0]) as a1:
        assert manager.stats() == {"open_pools": 1, "in_use": 1, "cache_max": 8, "pool_max": 2}
    with manager.repository(world.ctx[0]) as a2:
        pass
    assert a1 is a2 and len(factory.opened) == 1
    manager.close()


def test_tenant_settings_come_from_the_server_and_the_context_only(world):
    factory = Factory()
    manager = _manager(factory, pool_max=1)
    with manager.repository(world.ctx[0]) as repo:
        s = repo.settings
    assert s.dbname == world.ctx[0].database.database_name
    assert (s.host, s.user, s.password, s.port) == (BASE.host, BASE.user, BASE.password, BASE.port)
    assert (s.pool_min, s.pool_max, s.schema, s.application_name) == (0, 1, None, "zaza-tenant")
    manager.close()


def test_only_a_resolved_context_is_accepted(world):
    manager = _manager(Factory())
    for bogus in (None, "zaza_t_" + "a" * 26, world.orgs[0], {"organization_id": world.orgs[0]},
                  world.ctx[0].database):
        with pytest.raises(TenancyError), manager.repository(bogus):
            pass
    manager.close()


def test_manager_has_no_api_taking_a_host_name_or_url():
    import inspect

    for name, member in inspect.getmembers(TenantRepositoryManager, inspect.isfunction):
        params = set(inspect.signature(member).parameters) - {"self"}
        assert not params & {"host", "dbname", "database_name", "url", "conninfo", "password", "user"}, name


# ─── isolation ──────────────────────────────────────────────────────────────


def test_each_context_gets_its_own_tenant_pool(world):
    factory = Factory()
    manager = _manager(factory)
    seen = {}
    for ctx in world.ctx[:3]:
        with manager.repository(ctx) as repo:
            seen[ctx.organization_id] = repo
            assert repo.settings.dbname == ctx.database.database_name
    assert len({id(r) for r in seen.values()}) == 3
    for ctx in reversed(world.ctx[:3]):  # repeated access returns the SAME tenant's pool
        with manager.repository(ctx) as repo:
            assert repo is seen[ctx.organization_id]
    manager.close()


def test_suspended_or_not_ready_tenants_never_get_a_pool(world):
    factory = Factory()
    manager = _manager(factory)
    d = world.directory
    suspended = d.org("suspended", status=OrganizationStatus.SUSPENDED)
    provisioning = d.org("provisioning", db_status=TenantDatabaseStatus.PROVISIONING)
    errored = d.org("errored", db_status=TenantDatabaseStatus.ERROR)
    db_suspended = d.org("db-suspended", db_status=TenantDatabaseStatus.SUSPENDED)
    for org in (suspended, provisioning, errored, db_suspended):
        with pytest.raises(TenantAccessDenied):
            world.resolver.for_job(org)
    # a context cannot be forged for them either: TenantContext demands READY + the resolver's key
    with pytest.raises(TenancyError):
        TenantDatabaseDescriptor(provisioning, "postgres", "asia-south1", TenantDatabaseStatus.READY, "x")
    assert factory.opened == []
    manager.close()


def test_suspending_an_organization_closes_its_cached_pool(world):
    factory = Factory()
    manager = _manager(factory)
    with manager.repository(world.ctx[0]) as a, manager.repository(world.ctx[1]) as b:
        pass
    world.directory.orgs[world.orgs[0]] = world.directory.orgs[world.orgs[0]].__class__(
        world.orgs[0], "tenant-a", "Tenant A", OrganizationStatus.SUSPENDED, "UTC")
    assert manager.discard(world.orgs[0]) == 1
    assert a.closed and not b.closed
    with pytest.raises(TenantAccessDenied):
        world.resolver.for_job(world.orgs[0])
    manager.close()


def test_a_failing_tenant_does_not_affect_another(world):
    factory = Factory()
    manager = _manager(factory)
    a, b = world.ctx[0], world.ctx[1]
    with manager.repository(b) as b_repo:
        pass
    factory.fail.add(a.database.database_name)
    for _ in range(2):
        with pytest.raises(RepositoryUnavailable), manager.repository(a):
            pass
    with manager.repository(b) as again:  # B unaffected, same pool
        assert again is b_repo and again.settings.dbname == b.database.database_name and not again.closed
    assert manager.stats()["open_pools"] == 1  # the failed tenant left no entry behind
    factory.fail.clear()
    with manager.repository(a) as a_repo:  # A recovers with ITS OWN database
        assert a_repo.settings.dbname == a.database.database_name and a_repo is not b_repo
    manager.close()


def test_an_error_while_using_a_pool_retires_only_that_pool(world):
    factory = Factory()
    manager = _manager(factory)
    a, b = world.ctx[0], world.ctx[1]
    with manager.repository(b) as b_repo:
        pass
    with pytest.raises(RepositoryUnavailable), manager.repository(a) as broken:
        raise RepositoryUnavailable("connection lost")
    assert broken.closed and not b_repo.closed
    with manager.repository(a) as fresh:  # broken pool is re-created, for the right tenant
        assert fresh is not broken and fresh.settings.dbname == a.database.database_name
    with manager.repository(b) as same:
        assert same is b_repo
    manager.close()


def test_application_errors_do_not_retire_a_pool(world):
    manager = _manager(Factory())
    with pytest.raises(ValueError), manager.repository(world.ctx[0]) as repo:
        raise ValueError("bad request data")
    with manager.repository(world.ctx[0]) as again:
        assert again is repo and not repo.closed
    manager.close()


# ─── bounded cache, LRU and idle eviction ───────────────────────────────────


def test_cache_is_bounded_and_evicts_the_least_recently_used(world):
    factory = Factory()
    manager = _manager(factory, cache_max=3)
    repos = {}
    for i in range(3):
        with manager.repository(world.ctx[i]) as repo:
            repos[i] = repo
    with manager.repository(world.ctx[0]):  # 0 becomes most recent; 1 is now the LRU
        pass
    with manager.repository(world.ctx[3]) as r3:
        repos[3] = r3
    assert repos[1].closed and not repos[0].closed and not repos[2].closed
    assert manager.stats()["open_pools"] == 3 and factory.open_count() == 3
    for i in range(4, 10):
        with manager.repository(world.ctx[i]):
            assert factory.open_count() <= 3
    assert factory.open_count() == 3 and len(factory.opened) == 10
    manager.close()


def test_pools_in_use_are_never_evicted_and_the_bound_holds(world):
    factory = Factory()
    manager = _manager(factory, cache_max=2)
    with manager.repository(world.ctx[0]) as a, manager.repository(world.ctx[1]) as b:
        with pytest.raises(TenantPoolsExhausted), manager.repository(world.ctx[2]):
            pass
        assert not a.closed and not b.closed and factory.open_count() == 2
    with manager.repository(world.ctx[2]):  # once one is free, the LRU is replaced
        assert factory.open_count() == 2
    manager.close()


def test_idle_pools_are_closed(world):
    factory, clock = Factory(), Clock()
    manager = _manager(factory, clock, idle_seconds=60)
    with manager.repository(world.ctx[0]) as a:
        pass
    clock.now += 30
    with manager.repository(world.ctx[1]) as b:
        pass
    clock.now += 31  # a: 61 s idle, b: 31 s idle
    assert manager.evict_idle() == 1 and a.closed and not b.closed
    clock.now += 60
    with manager.repository(world.ctx[2]) as c:  # eviction also happens on access
        assert b.closed and not c.closed
    with manager.repository(world.ctx[0]) as a2:  # re-created after eviction, same tenant
        assert a2 is not a and a2.settings.dbname == world.ctx[0].database.database_name
    manager.close()


def test_a_pool_in_use_is_not_idle(world):
    factory, clock = Factory(), Clock()
    manager = _manager(factory, clock, idle_seconds=60)
    with manager.repository(world.ctx[0]) as a:
        clock.now += 3600
        assert manager.evict_idle() == 0 and not a.closed
    clock.now += 61
    assert manager.evict_idle() == 1 and a.closed
    manager.close()


def test_reaper_thread_evicts_without_traffic(world):
    factory, clock = Factory(), Clock()
    manager = _manager(factory, clock, idle_seconds=10)
    with manager.repository(world.ctx[0]) as a:
        pass
    clock.now += 11
    manager.start_reaper(interval=0.05)
    deadline = time.monotonic() + 5
    while not a.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert a.closed
    manager.close()


# ─── shutdown ───────────────────────────────────────────────────────────────


def test_close_closes_every_pool_and_refuses_new_ones(world):
    factory = Factory()
    manager = _manager(factory)
    for ctx in world.ctx[:5]:
        with manager.repository(ctx):
            pass
    manager.start_reaper(interval=0.05)
    manager.close()
    assert len(factory.opened) == 5 and factory.open_count() == 0
    assert manager.stats()["open_pools"] == 0
    with pytest.raises(RepositoryUnavailable), manager.repository(world.ctx[0]):
        pass
    manager.close()  # idempotent


def test_close_while_a_pool_is_in_use(world):
    factory = Factory()
    manager = _manager(factory)
    with manager.repository(world.ctx[0]) as a:
        manager.close()
        assert a.closed
    assert factory.open_count() == 0  # the lease ending does not close twice (FakeRepo asserts)


# ─── concurrency ────────────────────────────────────────────────────────────


def test_concurrent_access_is_thread_safe_and_isolated(world):
    factory = Factory()
    factory.delay = 0.01
    manager = _manager(factory, cache_max=4)
    errors: list[str] = []
    max_open = [0]

    def worker(n: int) -> None:
        for i in range(40):
            ctx = world.ctx[(n + i) % len(world.ctx)]
            try:
                with manager.repository(ctx) as repo:
                    if repo.settings.dbname != ctx.database.database_name or repo.closed:
                        errors.append("wrong or closed pool")
                    max_open[0] = max(max_open[0], factory.open_count())
            except TenantPoolsExhausted:
                pass  # all 4 pools busy: fail fast, never exceed the bound
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert max_open[0] <= 4 and factory.open_count() <= 4
    manager.close()
    assert factory.open_count() == 0


def test_concurrent_first_use_of_one_tenant_opens_one_pool(world):
    factory = Factory()
    factory.delay = 0.1
    manager = _manager(factory)
    got: list[FakeRepo] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        with manager.repository(world.ctx[0]) as repo:
            got.append(repo)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(got) == 8 and len({id(r) for r in got}) == 1 and len(factory.opened) == 1
    manager.close()


def test_waiters_of_a_failing_tenant_all_fail(world):
    factory = Factory()
    factory.delay = 0.1
    factory.fail.add(world.ctx[0].database.database_name)
    manager = _manager(factory)
    outcomes: list[str] = []

    def worker() -> None:
        try:
            with manager.repository(world.ctx[0]):
                outcomes.append("ok")
        except RepositoryUnavailable:
            outcomes.append("unavailable")

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert outcomes == ["unavailable"] * 5 and manager.stats()["open_pools"] == 0
    manager.close()


# ─── tenant migration runner ────────────────────────────────────────────────


class MigrationDirectory(FakeDirectory):
    def __init__(self) -> None:
        super().__init__()
        self.marks: list[tuple[uuid.UUID, str, str | None]] = []

    def mark_tenant_database(self, organization_id, status, schema_revision=None):  # noqa: ANN001, ANN201
        old = self.dbs[organization_id]
        self.dbs[organization_id] = TenantDatabaseDescriptor(
            organization_id, old.database_name, old.region, TenantDatabaseStatus(status),
            schema_revision or old.schema_revision)
        self.marks.append((organization_id, status, schema_revision))
        return self.dbs[organization_id]


class FakeSchema:
    HEAD = "0004_manager_dashboard_auth"

    def __init__(self) -> None:
        self.revisions: dict[str, str | None] = {}
        self.fail: set[str] = set()
        self.upgraded: list[DatabaseSettings] = []

    def status(self, settings: DatabaseSettings):  # noqa: ANN201
        current = self.revisions.get(settings.dbname)
        return SimpleNamespace(current=current, head=self.HEAD, up_to_date=current == self.HEAD)

    def upgrade(self, settings: DatabaseSettings) -> None:
        self.upgraded.append(settings)
        if settings.dbname in self.fail:
            raise RuntimeError(f"migration failed for {settings.display()} password={FAKE_PASSWORD}")
        self.revisions[settings.dbname] = self.HEAD


def _runner(directory, schema):  # noqa: ANN001, ANN202
    from deskmate.zaza_server.tenancy.tenant_migrations import TenantMigrationRunner

    return TenantMigrationRunner(directory, BASE, control_database_name="zaza_control", upgrade=schema.upgrade,
                                 status=schema.status)


def test_migrate_one_tenant_explicitly():
    d, schema = MigrationDirectory(), FakeSchema()
    org = d.org("a")
    name = d.dbs[org].database_name
    schema.revisions[name] = "0003_attendance"
    result = _runner(d, schema).migrate(org)
    assert result.ok and (result.before, result.after) == ("0003_attendance", FakeSchema.HEAD)
    assert [s.dbname for s in schema.upgraded] == [name]
    s = schema.upgraded[0]
    assert (s.host, s.user, s.schema, s.application_name) == (BASE.host, BASE.user, None, "zaza-tenant-migrate")
    assert d.marks == [(org, "READY", FakeSchema.HEAD)]
    assert "OK" in result.describe() and str(org) in result.describe()


def test_migrate_ready_tenants_sequentially_and_stop_on_failure():
    d, schema = MigrationDirectory(), FakeSchema()
    orgs = sorted((d.org(s) for s in ("a", "b", "c")), key=str)
    schema.fail.add(d.dbs[orgs[1]].database_name)
    results = _runner(d, schema).migrate_many()
    assert [r.organization_id for r in results] == orgs[:2]  # stopped after the failure
    assert [r.ok for r in results] == [True, False]
    assert d.dbs[orgs[1]].status is TenantDatabaseStatus.ERROR
    assert d.dbs[orgs[2]].status is TenantDatabaseStatus.READY and schema.revisions.get(
        d.dbs[orgs[2]].database_name) is None
    failed = results[1]
    assert FAKE_PASSWORD not in (failed.error or "") and "RuntimeError" in failed.error
    assert d.dbs[orgs[1]].database_name in failed.describe()


def test_migrate_many_can_continue_after_a_failure():
    d, schema = MigrationDirectory(), FakeSchema()
    orgs = sorted((d.org(s) for s in ("a", "b", "c")), key=str)
    schema.fail.add(d.dbs[orgs[0]].database_name)
    results = _runner(d, schema).migrate_many(stop_on_failure=False)
    assert [r.ok for r in results] == [False, True, True]


def test_migration_skips_suspended_and_unknown_tenants():
    d, schema = MigrationDirectory(), FakeSchema()
    suspended = d.org("s", db_status=TenantDatabaseStatus.SUSPENDED)
    none = d.org("n", db_status=None)
    runner = _runner(d, schema)
    assert not runner.migrate(suspended).ok and "SUSPENDED" in runner.migrate(suspended).error
    assert runner.migrate(none).error == "organization has no tenant database"
    assert schema.upgraded == []
    assert runner.migrate_many() == []  # only READY tenants by default


def test_migration_can_never_target_the_control_database():
    from deskmate.zaza_server.tenancy.tenant_migrations import TenantMigrationRunner

    d, schema = MigrationDirectory(), FakeSchema()
    org = d.org("a")
    name = d.dbs[org].database_name
    runner = TenantMigrationRunner(d, BASE, control_database_name=name, upgrade=schema.upgrade, status=schema.status)
    assert not runner.migrate(org).ok and schema.upgraded == []
    with pytest.raises(TenancyError):
        TenantMigrationRunner(d, BASE, control_database_name="")
    with pytest.raises(TenancyError):  # a descriptor can't even hold a non-tenant name
        TenantDatabaseDescriptor(org, "zaza_control", "asia-south1", TenantDatabaseStatus.READY, "x")


def test_postgres_upgrade_refuses_a_control_database():
    """Second line of defence inside the real tenant upgrade path."""
    import inspect

    from deskmate.zaza_server.postgres import migrate

    source = inspect.getsource(migrate.upgrade)
    assert source.index("refuse_control_database") < source.index("command.upgrade")


def test_tenant_migrate_cli_requires_an_explicit_selection():
    import subprocess
    import sys

    run = subprocess.run([sys.executable, "-m", "deskmate.zaza_server", "tenant-migrate"], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 2 and "--organization" in run.stderr


# ─── provisioning boundary ──────────────────────────────────────────────────


class FakeCreator:
    def __init__(self) -> None:
        self.databases: set[str] = set()
        self.granted: list[str] = []
        self.restricted: list[str] = []
        self.fail_create = False

    def database_exists(self, name: str) -> bool:
        return name in self.databases

    def create_database(self, name: str) -> None:
        domain.tenant_database_name(name)
        if self.fail_create:
            raise RuntimeError("permission denied to create database")
        self.databases.add(name)

    def restrict_access(self, name: str) -> None:
        self.restricted.append(name)

    def grant_runtime_access(self, name: str) -> None:
        self.granted.append(name)


class ProvisioningControl(MigrationDirectory):
    def assign_tenant_database(self, organization_id, region):  # noqa: ANN001, ANN201
        self.dbs[organization_id] = TenantDatabaseDescriptor(
            organization_id, domain.generate_tenant_database_name(), region, TenantDatabaseStatus.PROVISIONING)
        return self.dbs[organization_id]


def _provisioner(control, creator, schema):  # noqa: ANN001, ANN202
    from deskmate.zaza_server.tenancy.provisioning import TenantProvisioner

    return TenantProvisioner(control, creator, _runner(control, schema))


def test_provisioning_workflow_marks_ready_only_after_verified_migration():
    control, creator, schema = ProvisioningControl(), FakeCreator(), FakeSchema()
    org = control.org("new-customer", db_status=None)
    result = _provisioner(control, creator, schema).provision(org, "asia-south1")
    assert result.ok and result.status is TenantDatabaseStatus.READY and result.schema_revision == FakeSchema.HEAD
    name = control.dbs[org].database_name
    assert domain.TENANT_DB_RE.match(name) and creator.databases == {name} and creator.granted == [name]
    assert [m[1] for m in control.marks] == ["PROVISIONING", "READY"]
    # idempotent: provisioning a READY tenant changes nothing
    again = _provisioner(control, creator, schema).provision(org, "asia-south1")
    assert again.ok and len(schema.upgraded) == 1


def test_provisioning_failure_leaves_the_tenant_in_error_and_can_be_retried():
    control, creator, schema = ProvisioningControl(), FakeCreator(), FakeSchema()
    org = control.org("new-customer", db_status=None)
    creator.fail_create = True
    failed = _provisioner(control, creator, schema).provision(org, "asia-south1")
    assert not failed.ok and failed.status is TenantDatabaseStatus.ERROR
    assert control.dbs[org].status is TenantDatabaseStatus.ERROR
    creator.fail_create = False
    retried = _provisioner(control, creator, schema).provision(org, "asia-south1")
    assert retried.ok and control.dbs[org].status is TenantDatabaseStatus.READY


def test_provisioning_migration_failure_never_marks_ready():
    control, creator, schema = ProvisioningControl(), FakeCreator(), FakeSchema()
    org = control.org("new-customer", db_status=None)
    original = control.assign_tenant_database

    def assign(o, r):  # noqa: ANN001, ANN202
        descriptor = original(o, r)
        schema.fail.add(descriptor.database_name)
        return descriptor

    control.assign_tenant_database = assign
    result = _provisioner(control, creator, schema).provision(org, "asia-south1")
    assert not result.ok and control.dbs[org].status is TenantDatabaseStatus.ERROR and creator.granted == []


def test_provisioning_refuses_inactive_organizations():
    control, creator, schema = ProvisioningControl(), FakeCreator(), FakeSchema()
    org = control.org("closed", status=OrganizationStatus.CLOSED, db_status=None)
    result = _provisioner(control, creator, schema).provision(org, "asia-south1")
    assert not result.ok and creator.databases == set() and org not in control.dbs


def test_runtime_role_names_are_validated():
    from deskmate.zaza_server.tenancy.provisioning import runtime_role_name

    assert runtime_role_name("zaza_app") == "zaza_app"
    for bad in ("postgres", "public", "pg_read_all_data", "Zaza", "zaza app", "zaza;drop", "", "cloudsqlsuperuser"):
        with pytest.raises(TenancyError):
            runtime_role_name(bad)
