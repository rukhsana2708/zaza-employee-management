"""Sync API rate limiting on REAL PostgreSQL (opt-in: ZAZA_TEST_POSTGRES_URL, a disposable database whose name contains
'test'): migration c0004, the shared counter on the database clock, cross-instance exactness under concurrency, cleanup,
fail-open, and the real SaaS app end to end.

Every database this module creates is dropped."""

from __future__ import annotations

import dataclasses
import secrets
import threading
import time

import pytest

from .conftest import pg_test_settings

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402

from deskmate.zaza.sync.protocol import DEVICE_ID_HEADER, DEVICE_PATH  # noqa: E402
from deskmate.zaza_server.ratelimit import (  # noqa: E402
    DEVICE_BUCKET,
    RateLimitExceeded,
    SyncLimitSettings,
    SyncRateLimiter,
    device_key,
)
from deskmate.zaza_server.tenancy.control import migrate as control_migrate  # noqa: E402
from deskmate.zaza_server.tenancy.control_repository import PostgresControlRepository  # noqa: E402
from deskmate.zaza_server.tenancy.saas_auth import PostgresAuthStore  # noqa: E402

from .test_enrollment_postgres import _enroll, _exec, env  # noqa: E402, F401  (env is a fixture)


def _admin():  # noqa: ANN202
    s = dataclasses.replace(pg_test_settings("public"), schema=None)
    with psycopg.connect(**s.connect_kwargs()) as conn:
        if not conn.execute("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()[0]:
            pytest.skip("the test role may not create databases")
    return s


@pytest.fixture
def control():  # noqa: ANN201
    """A disposable control database at the head revision (c0004)."""
    admin = _admin()
    name = f"zaza_test_rl_{secrets.token_hex(4)}"
    _exec(admin, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings = dataclasses.replace(admin, dbname=name, pool_min=0, pool_max=8)
    repo = None
    try:
        control_migrate.upgrade(settings)
        repo = PostgresControlRepository(settings, actor_type="CLI", actor_id="pytest-ratelimit")
        yield SimpleControl(settings, repo, PostgresAuthStore(repo))
    finally:
        if repo is not None:
            repo.close()
        _exec(admin, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@dataclasses.dataclass
class SimpleControl:
    settings: object
    repo: object
    store: PostgresAuthStore


def _db_now(settings) -> float:  # noqa: ANN001
    return float(_exec(settings, "SELECT extract(epoch FROM clock_timestamp())")[0][0])


def _wait_for_fresh_window(settings, margin: float = 12.0) -> None:  # noqa: ANN001
    """Make sure the next few seconds fall inside ONE database-aligned 60 s window."""
    now = _db_now(settings)
    left = 60 - (now % 60)
    if left < margin:
        time.sleep(left + 0.3)


def _limiter(store, **overrides):  # noqa: ANN001, ANN003, ANN202
    return SyncRateLimiter(SyncLimitSettings(mode="enforce", burst_limit=100_000, **overrides), store)


# ─── migration c0004 ───────────────────────────────────────────────────────


def test_c0004_allows_the_sync_device_bucket_and_keeps_the_others(control):
    assert control_migrate.head_revision() == "c0005_employee_membership"
    with psycopg.connect(**control.settings.connect_kwargs()) as conn:
        assert control_migrate.current_revision(conn) == "c0005_employee_membership"
    key = "a" * 64
    for bucket in ("identity", "global", "enrollment_code", "enrollment_global", DEVICE_BUCKET):
        assert control.store.count_attempt(bucket, key, 60)[0] == 1
    with pytest.raises(psycopg.errors.CheckViolation):
        control.store.count_attempt("not_a_bucket", key, 60)
    with pytest.raises(psycopg.errors.CheckViolation):
        control.store.count_attempt(DEVICE_BUCKET, "not-hex", 60)  # the key CHECK still applies


def test_c0004_upgrades_from_c0003_and_downgrades_cleanly():
    admin = _admin()
    name = f"zaza_test_mig_{secrets.token_hex(4)}"
    _exec(admin, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings = dataclasses.replace(admin, dbname=name, pool_min=0, pool_max=2)
    try:
        control_migrate.upgrade(settings, "c0003_device_enrollment")
        with pytest.raises(psycopg.errors.CheckViolation):
            _exec(settings, "INSERT INTO login_attempt_counters (bucket, key_hash, window_start) "
                            "VALUES ('sync_device', repeat('b', 64), now())")
        _exec(settings, "INSERT INTO login_attempt_counters (bucket, key_hash, window_start) "
                        "VALUES ('identity', repeat('c', 64), now())")
        control_migrate.upgrade(settings)
        _exec(settings, "INSERT INTO login_attempt_counters (bucket, key_hash, window_start) "
                        "VALUES ('sync_device', repeat('b', 64), now())")
        from alembic import command  # noqa: PLC0415

        command.downgrade(control_migrate.alembic_config(settings), "c0003_device_enrollment")
        rows = _exec(settings, "SELECT bucket FROM login_attempt_counters ORDER BY bucket")
        assert rows == [("identity",)]  # the new bucket's rows were removed, the others kept
        with pytest.raises(psycopg.errors.CheckViolation):
            _exec(settings, "INSERT INTO login_attempt_counters (bucket, key_hash, window_start) "
                            "VALUES ('sync_device', repeat('b', 64), now())")
    finally:
        _exec(admin, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


# ─── the shared counter and the database clock ─────────────────────────────


def test_windows_are_aligned_to_the_database_clock(control):
    key = "d" * 64
    attempts, retry_after = control.store.count_attempt(DEVICE_BUCKET, key, 60)
    assert attempts == 1 and 1 <= retry_after <= 60
    ((start_mod, count),) = _exec(control.settings, "SELECT extract(epoch FROM window_start)::bigint %% 60, attempts "
                                                    "FROM login_attempt_counters WHERE bucket = 'sync_device'")
    assert start_mod == 0 and count == 1  # a window starts on a multiple of 60 s of database time
    assert control.store.count_attempt(DEVICE_BUCKET, key, 60)[0] == 2


def test_a_new_window_starts_a_fresh_count(control):
    key = "e" * 64
    first = control.store.count_attempt(DEVICE_BUCKET, key, 2)
    assert first[0] == 1
    assert control.store.count_attempt(DEVICE_BUCKET, key, 2)[0] == 2
    time.sleep(2.2)
    assert control.store.count_attempt(DEVICE_BUCKET, key, 2)[0] == 1  # reset by the database clock, not by the client


def test_retry_after_matches_the_time_left_in_the_database_window(control):
    key = "f" * 64
    now = _db_now(control.settings)
    attempts, retry_after = control.store.count_attempt(DEVICE_BUCKET, key, 60)
    expected = 60 - (now % 60)
    assert abs(retry_after - expected) <= 2


# ─── cross-instance exactness and concurrency ──────────────────────────────


def test_the_shared_limit_is_exact_across_instances_under_concurrency(control):
    """Three limiter instances (separate local state) share one PostgreSQL counter; 6 threads fire 300 requests for the
    SAME device. Exactly 120 are admitted in the aligned window, whatever the interleaving."""
    _wait_for_fresh_window(control.settings)
    instances = [_limiter(control.store) for _ in range(3)]
    outcome = {"ok": 0, "limited": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def worker(n: int) -> None:
        limiter = instances[n % 3]
        barrier.wait()
        for _ in range(50):
            try:
                limiter.admit_device("org-1", "device-1", "digest-1", None)
                result = "ok"
            except RateLimitExceeded:
                result = "limited"
            with lock:
                outcome[result] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert outcome == {"ok": 120, "limited": 180}
    key = device_key("org-1", "device-1", "digest-1")
    ((count,),) = _exec(control.settings, "SELECT sum(attempts) FROM login_attempt_counters WHERE bucket = 'sync_device' "
                                          f"AND key_hash = '{key}'")
    assert count == 300  # every request that reached the shared counter was counted, atomically
    # a refusal reports the real time to the next database window
    with pytest.raises(RateLimitExceeded) as info:
        instances[0].admit_device("org-1", "device-1", "digest-1", None)
    assert 1 <= info.value.retry_after <= 60
    # another device is unaffected by that device's limit
    instances[1].admit_device("org-1", "device-2", "digest-2", None)


def test_the_process_local_burst_guard_cannot_bypass_the_shared_counter(control):
    _wait_for_fresh_window(control.settings)
    limiter = SyncRateLimiter(SyncLimitSettings(mode="enforce", burst_limit=40), control.store)
    admitted = 0
    for _ in range(60):
        try:
            limiter.admit_device("org-1", "device-9", "digest-9", None)
            admitted += 1
        except RateLimitExceeded:
            pass
    assert admitted == 40  # the burst guard stopped the rest locally
    key = device_key("org-1", "device-9", "digest-9")
    assert _exec(control.settings, "SELECT sum(attempts) FROM login_attempt_counters WHERE key_hash = %s", (key,)) \
        == [(40,)]  # exactly the admitted requests were counted


def test_a_failing_database_fails_open_with_the_local_ceiling(control, monkeypatch):
    limiter = SyncRateLimiter(SyncLimitSettings(mode="enforce", burst_limit=100_000, fallback_device_per_window=3),
                              control.store)

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise psycopg.OperationalError("connection to server at 10.0.0.5 failed: password authentication failed")

    monkeypatch.setattr(control.store, "count_attempt", boom)
    for _ in range(3):
        limiter.admit_device("org-1", "device-1", "digest-1", None)  # served
    with pytest.raises(RateLimitExceeded):
        limiter.admit_device("org-1", "device-1", "digest-1", None)  # only the local ceiling
    assert limiter.stats["device_shared_counter_failure"] == 4


# ─── cleanup ───────────────────────────────────────────────────────────────


def test_cleanup_deletes_only_old_sync_device_rows(control):
    s = control.settings
    _exec(s, "INSERT INTO login_attempt_counters (bucket, key_hash, window_start, attempts) VALUES "
             "('sync_device', repeat('1', 64), now() - interval '2 hours', 5), "
             "('sync_device', repeat('2', 64), now() - interval '3 hours', 5), "
             "('sync_device', repeat('3', 64), now(), 5), "
             "('identity', repeat('4', 64), now() - interval '5 hours', 5), "
             "('global', repeat('5', 64), now() - interval '5 hours', 5)")
    assert control.store.cleanup_expired_counters(limit=1) == 1  # bounded batch
    assert control.store.cleanup_expired_counters() == 1
    assert control.store.cleanup_expired_counters() == 0
    left = sorted(r[0] for r in _exec(s, "SELECT key_hash FROM login_attempt_counters"))
    assert left == sorted(["3" * 64, "4" * 64, "5" * 64])  # fresh sync row and other buckets untouched


# ─── the real SaaS app over real PostgreSQL ────────────────────────────────


def test_the_real_app_limits_a_device_using_the_route_device_id(env):  # noqa: F811
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from deskmate.zaza_server.app import create_saas_app  # noqa: PLC0415

    _wait_for_fresh_window(env.control_settings, margin=15.0)
    tokens = {}
    for name in ("a", "b"):
        _, code = env.store.create_code(env.tenants[name].org.organization_id, "employee-1", ttl_hours=24)
        tokens[name] = _enroll(env, code).json()["device_token"]  # the SAME device id in two tenants
    limiter = SyncRateLimiter(SyncLimitSettings(mode="enforce", device_per_window=5, burst_limit=100_000),
                              PostgresAuthStore(env.control))
    env.runtime.devices.limiter = limiter
    client = TestClient(create_saas_app(env.runtime), raise_server_exceptions=False)

    def me(name):  # noqa: ANN001, ANN202
        return client.get(DEVICE_PATH, headers={"Authorization": f"Bearer {tokens[name]}", DEVICE_ID_HEADER: "device-1"})

    assert [me("a").status_code for _ in range(5)] == [200] * 5
    limited = me("a")
    assert limited.status_code == 429 and limited.json() == {"error": "rate limit exceeded"}
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    assert [me("b").status_code for _ in range(5)] == [200] * 5  # tenant B's device-1 has its own counter
    org_a = env.tenants["a"].org.organization_id
    key = device_key(org_a, "device-1", "ignored-because-the-route-supplies-the-device-id")
    rows = _exec(env.control_settings, "SELECT attempts FROM login_attempt_counters WHERE bucket = 'sync_device' "
                                       "AND key_hash = %s", (key,))
    assert rows and rows[0][0] >= 6  # keyed by (organization, device id) from the control route row
    # nothing in the control plane stores a token or a raw identifier in the counter table
    dump = str(_exec(env.control_settings, "SELECT * FROM login_attempt_counters"))
    assert tokens["a"] not in dump and "device-1" not in dump
