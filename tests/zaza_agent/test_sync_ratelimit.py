"""Sync API rate limiting (design: docs/zaza/SYNC_RATE_LIMIT_HSTS_DESIGN.md, revision 2).

Unit tests drive :class:`SyncRateLimiter` with a fake clock and a fake counter that behaves like the PostgreSQL one
(fixed windows aligned to the DATABASE clock). App-level tests go through the real SaaS app (``create_saas_app``) and the
real ``SaasDeviceAuthenticator``. PostgreSQL behaviour is in test_sync_ratelimit_postgres.py (opt-in)."""

from __future__ import annotations

import logging
import math
import threading

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from deskmate.zaza.sync.protocol import BATCH_PATH, DEVICE_ID_HEADER, DEVICE_PATH  # noqa: E402
from deskmate.zaza_server import http_security  # noqa: E402
from deskmate.zaza_server.app import create_saas_app  # noqa: E402
from deskmate.zaza_server.config import ConfigError  # noqa: E402
from deskmate.zaza_server.http_security import HttpSettings, trusted_client_ip  # noqa: E402
from deskmate.zaza_server.ratelimit import (  # noqa: E402
    DEVICE_BUCKET,
    MAX_RETRY_AFTER,
    RateLimitExceeded,
    SyncLimitSettings,
    SyncRateLimiter,
    device_key,
    ip_key,
    sync_limit_settings_from_env,
)

from .test_enrollment import _code, _enroll, _env  # noqa: E402
from .test_sync_worker import ack_all, mock_transport  # noqa: E402


class Clock:
    """One fake time for the limiter (monotonic) and for the 'database' (epoch-aligned at 0)."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeCounters:
    """The same contract as PostgresAuthStore.count_attempt: fixed windows aligned to the database clock."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.rows: dict[tuple, int] = {}
        self.calls = 0
        self.fail: Exception | None = None
        self.lock = threading.Lock()

    def count_attempt(self, bucket: str, key_hash: str, window_seconds: int) -> tuple[int, int]:
        if self.fail is not None:
            raise self.fail
        with self.lock:
            self.calls += 1
            now = self.clock()
            start = math.floor(now / window_seconds) * window_seconds
            self.rows[(bucket, key_hash, start)] = self.rows.get((bucket, key_hash, start), 0) + 1
            return self.rows[(bucket, key_hash, start)], max(1, math.ceil(start + window_seconds - now))

    def count_for(self, key_hash: str) -> int:
        return sum(v for (b, k, _), v in self.rows.items() if b == DEVICE_BUCKET and k == key_hash)


def make(mode: str = "enforce", **overrides):  # noqa: ANN003, ANN202
    clock = Clock()
    counters = FakeCounters(clock)
    overrides.setdefault("ip_mode", mode)  # production defaults the IP limiters to shadow; tests state it explicitly
    limiter = SyncRateLimiter(SyncLimitSettings(mode=mode, **overrides), counters, clock=clock, rng=lambda: 1.0)
    return limiter, counters, clock


def events(limiter):  # noqa: ANN001, ANN201
    """Limit decisions only (the 'ip_unverified_skipped' counter is bookkeeping, not a limit event)."""
    return {k: v for k, v in limiter.stats.items() if k != "ip_unverified_skipped"}


def hit(limiter, device="dev-1", org="org-1", ip=None):  # noqa: ANN001, ANN201
    limiter.admit_device(org, device, f"digest-{device}", ip)


def refused(limiter, **kw):  # noqa: ANN003, ANN201
    with pytest.raises(RateLimitExceeded) as info:
        hit(limiter, **kw)
    return info.value


# ─── settings ──────────────────────────────────────────────────────────────


def test_defaults_are_the_approved_values_and_the_limiter_is_off():
    s = SyncLimitSettings()
    assert (s.mode, s.device_per_window, s.device_window_seconds) == ("off", 120, 60)
    assert (s.burst_limit, s.burst_window_seconds) == (40, 10)
    assert (s.ip_fail_per_window, s.ip_fail_window_seconds) == (20, 60)
    assert (s.ip_fail_burst_limit, s.ip_fail_burst_window_seconds) == (10, 10)
    assert s.ip_coarse_per_minute == 1800 and not s.enabled
    assert s.ip_mode == "shadow"  # the per-IP limiters never enforce until explicitly switched on


def test_settings_from_env_and_validation():
    assert sync_limit_settings_from_env({}).mode == "off"
    s = sync_limit_settings_from_env({"ZAZA_SYNC_LIMIT_MODE": "Shadow", "ZAZA_SYNC_LIMIT_DEVICE_PER_MINUTE": "90"})
    assert s.mode == "shadow" and s.device_per_window == 90 and s.enabled and not s.enforcing
    for bad in ({"ZAZA_SYNC_LIMIT_MODE": "on"}, {"ZAZA_SYNC_LIMIT_BURST_PER_10S": "x"},
                {"ZAZA_SYNC_LIMIT_BURST_PER_10S": "0"}):
        with pytest.raises(ConfigError):
            sync_limit_settings_from_env(bad)
    with pytest.raises(ConfigError):
        SyncLimitSettings(device_window_seconds=30)  # the shared window is fixed at 60 s


def test_off_mode_never_touches_the_counter():
    limiter, counters, _ = make("off")
    for _ in range(500):
        hit(limiter)
        limiter.gate("203.0.113.5", "x")
        limiter.auth_failed("203.0.113.5")
    assert counters.calls == 0 and not events(limiter)


# ─── authenticated device limit ────────────────────────────────────────────


def test_normal_once_per_minute_sync_is_never_limited():
    limiter, counters, clock = make()
    for _ in range(6 * 60):  # six hours of minute-by-minute sync
        hit(limiter)
        clock.advance(60)
    assert counters.calls == 360 and not events(limiter)


def test_a_20_request_reconnect_burst_passes():
    """The real agent drains up to 20 batches back to back (about 4 to 6 per second)."""
    limiter, counters, clock = make()
    for _ in range(20):
        hit(limiter)
        clock.advance(0.2)
    assert counters.count_for(device_key("org-1", "dev-1", "digest-dev-1")) == 20 and not events(limiter)


def test_40_requests_inside_10_seconds_pass_and_the_41st_is_limited():
    limiter, counters, clock = make()
    for _ in range(40):
        hit(limiter)
        clock.advance(0.2)  # 40 requests inside 8 s
    err = refused(limiter)
    assert 1 <= err.retry_after <= 10
    assert limiter.stats["device_burst_limited"] == 1
    # what the guard admitted was counted by the shared counter; the request it refused was not processed, so not counted
    assert counters.count_for(device_key("org-1", "dev-1", "digest-dev-1")) == 40


def test_burst_guard_retry_after_is_the_time_until_the_oldest_request_ages_out():
    limiter, _, clock = make()
    for _ in range(40):
        hit(limiter)  # all at t=0
    clock.advance(3.4)
    assert refused(limiter).retry_after == 7  # oldest at 0 leaves the 10 s window at t=10: 6.6 s -> 7
    clock.advance(6.6)
    hit(limiter)  # eligible exactly when the oldest has aged out


def test_every_request_the_burst_guard_admits_is_counted_by_the_shared_counter():
    limiter, counters, clock = make(device_per_window=100_000)  # only the burst guard can refuse here
    admitted = 0
    for _ in range(300):
        try:
            hit(limiter)
            admitted += 1
        except RateLimitExceeded:
            pass
        clock.advance(0.05)  # 20 requests a second offered
    refused_by_guard = limiter.stats["device_burst_limited"]
    assert admitted > 0 and refused_by_guard > 0 and admitted + refused_by_guard == 300
    # no uncounted bypass: exactly the admitted requests reached the shared counter
    assert counters.count_for(device_key("org-1", "dev-1", "digest-dev-1")) == admitted == counters.calls


def test_120_requests_in_one_fixed_window_pass_and_the_121st_is_limited():
    limiter, counters, clock = make()
    for _ in range(120):
        hit(limiter)
        clock.advance(0.45)  # 54 s in total, 22 per 10 s: clear of the burst guard
    err = refused(limiter)
    assert limiter.stats["device_shared_limited"] == 1 and limiter.stats["device_burst_limited"] == 0
    assert err.retry_after == math.ceil(60 - clock.t)  # seconds until the next database-aligned window
    clock.advance(err.retry_after)
    hit(limiter)  # a fresh window


def test_fixed_window_boundary_allows_up_to_240_inside_any_60_seconds():
    """Documented behaviour: a FIXED window, not a rolling guarantee. 120 at the end of one window plus 120 at the start of
    the next are all admitted: 240 requests inside 60 s (4 per second is also the burst guard's sustained rate)."""
    limiter, counters, clock = make()
    clock.t = 30.0  # the window boundary is at t=60
    for _ in range(240):
        hit(limiter)
        clock.advance(0.25)
    assert clock.t - 30.0 == 60.0
    assert not events(limiter)  # all 240 admitted
    key = device_key("org-1", "dev-1", "digest-dev-1")
    assert [v for (b, k, _), v in sorted(counters.rows.items()) if k == key] == [120, 120]


def test_another_device_is_not_affected():
    limiter, _, clock = make()
    for _ in range(40):
        hit(limiter, device="busy")
    refused(limiter, device="busy")
    hit(limiter, device="quiet")
    hit(limiter, device="quiet", org="org-2")  # same device id in another organization is a different key


def test_many_devices_behind_one_ip_stay_independent_and_unlimited_by_the_device_limit():
    limiter, _, clock = make()
    for i in range(300):
        hit(limiter, device=f"pc-{i}", ip="198.51.100.9")  # an office of 300 devices, one public IP
        clock.advance(0.01)
    assert not events(limiter)


def test_the_key_is_the_server_side_identity_not_the_ip():
    a = device_key("org-1", "dev-1", "x")
    assert a == device_key("org-1", "dev-1", "different-digest")  # device id known: the credential digest is irrelevant
    assert a != device_key("org-1", "dev-2", "x") and a != device_key("org-2", "dev-1", "x")
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)  # fits the control table's key CHECK
    assert device_key("org-1", None, "digest-a") != device_key("org-1", None, "digest-b")  # fallback identity


def test_retry_after_is_integer_rounded_up_capped_and_deterministic():
    limiter, _, clock = make(burst_limit=1, burst_window_seconds=100)
    hit(limiter)
    clock.advance(0.1)
    waits = {refused(limiter).retry_after for _ in range(5)}
    assert waits == {MAX_RETRY_AFTER}  # 99.9 s -> capped at 60
    limiter2, _, clock2 = make(burst_limit=1, burst_window_seconds=10)
    hit(limiter2)
    clock2.advance(9.01)
    assert refused(limiter2).retry_after == 1  # 0.99 s -> rounded up


def test_the_longer_of_two_waits_is_reported():
    limiter, _, clock = make(device_per_window=3, burst_limit=3)
    clock.t = 5.0
    for _ in range(3):
        hit(limiter)  # the shared counter is now at its limit AND the burst guard is full
    clock.advance(1.0)
    err = refused(limiter)
    # burst guard alone: 9 s; the shared window ends at t=60: 54 s left. The longer wait is reported.
    assert err.retry_after == 54


def test_shared_counter_limit_uses_database_time_for_alignment():
    limiter, counters, clock = make(device_per_window=2)
    clock.t = 59.0
    hit(limiter)
    hit(limiter)
    err = refused(limiter)
    assert err.retry_after == 1
    clock.advance(1.0)  # t=60: a new database-aligned window
    hit(limiter)


# ─── fail open ─────────────────────────────────────────────────────────────


def test_a_failing_counter_fails_open_with_a_local_ceiling_and_a_safe_warning(caplog):
    limiter, counters, clock = make(burst_limit=1000, fallback_device_per_window=5)
    counters.fail = RuntimeError("password=hunter2 host=db.internal")
    with caplog.at_level(logging.DEBUG, logger="zaza_server.ratelimit"):
        for _ in range(5):
            hit(limiter)  # served: telemetry is never lost because the limiter is broken
        assert limiter.stats["device_shared_counter_failure"] == 5
        err = refused(limiter)  # the local ceiling still protects
        assert err.retry_after >= 1 and limiter.stats["device_fallback_limited"] == 1
    text = caplog.text
    assert "counter_failure" in text and "hunter2" not in text and "db.internal" not in text
    counters.fail = None
    clock.advance(60)
    hit(limiter)  # recovered


def test_no_counter_store_behaves_like_a_failing_one():
    clock = Clock()
    limiter = SyncRateLimiter(SyncLimitSettings(mode="enforce", fallback_device_per_window=3), None, clock=clock)
    for _ in range(3):
        hit(limiter)
    with pytest.raises(RateLimitExceeded):
        hit(limiter)


# ─── invalid or unauthenticated traffic: process-local, per verified IP ────


def fail(limiter, ip, n=1):  # noqa: ANN001, ANN201
    for _ in range(n):
        limiter.gate(ip, None)
        limiter.auth_failed(ip)


def test_ten_failures_in_ten_seconds_block_that_ip():
    limiter, _, clock = make()
    fail(limiter, "203.0.113.5", 10)
    with pytest.raises(RateLimitExceeded) as info:
        limiter.gate("203.0.113.5", None)
    assert 1 <= info.value.retry_after <= 10
    limiter.gate("203.0.113.6", None)  # a different IP is independent
    clock.advance(info.value.retry_after)
    limiter.gate("203.0.113.5", None)  # eligible again


def test_twenty_failures_in_a_minute_block_even_when_spread_out():
    limiter, _, clock = make()
    for _ in range(20):
        fail(limiter, "203.0.113.5")
        clock.advance(2.9)  # never 10 inside 10 s, 20 inside 60 s
    with pytest.raises(RateLimitExceeded) as info:
        limiter.gate("203.0.113.5", None)
    assert 1 <= info.value.retry_after <= 60


def test_blocked_attempts_do_not_extend_the_block():
    limiter, _, clock = make()
    fail(limiter, "203.0.113.5", 10)
    for _ in range(50):
        with pytest.raises(RateLimitExceeded):
            limiter.gate("203.0.113.5", None)  # refused attempts are not recorded as new failures
    clock.advance(10.1)
    limiter.gate("203.0.113.5", None)


def test_the_ip_limiter_is_process_local_two_instances_do_not_share():
    """Documented: with several Cloud Run instances the effective limit is a multiple of 20 per minute."""
    a, _, _ = make()
    b, _, _ = make()
    for limiter in (a, b):
        for _ in range(9):
            fail(limiter, "203.0.113.5")
        limiter.gate("203.0.113.5", None)  # neither instance saw more than 9
    assert events(a) == events(b) == {}


def test_unverified_ip_skips_every_ip_limit_but_not_the_device_limit():
    limiter, _, _ = make(device_per_window=3, burst_limit=100)
    fail(limiter, None, 500)
    limiter.gate(None, None)
    assert limiter.stats["ip_unverified_skipped"] >= 1
    for _ in range(3):
        hit(limiter, ip=None)
    refused(limiter, ip=None)  # the device limiter keeps working without an IP


def test_known_good_credentials_pass_while_their_ip_is_blocked():
    limiter, _, clock = make()
    limiter.auth_succeeded("known-digest")
    fail(limiter, "203.0.113.5", 10)
    limiter.gate("203.0.113.5", "known-digest")  # a legitimate agent on the same public IP
    with pytest.raises(RateLimitExceeded):
        limiter.gate("203.0.113.5", "other-digest")
    clock.advance(121)  # the known-good entry expires
    fail(limiter, "203.0.113.5", 10)
    with pytest.raises(RateLimitExceeded):
        limiter.gate("203.0.113.5", "known-digest")


def test_ipv6_clients_are_grouped_by_their_64():
    assert ip_key("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == ip_key("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert ip_key("2001:db8:1:3::1") != ip_key("2001:db8:1:2::1")
    assert ip_key("::ffff:203.0.113.5") == "203.0.113.5" and ip_key("not an ip") is None and ip_key(None) is None


def test_coarse_ip_safeguard_does_not_replace_the_device_limit():
    limiter, _, clock = make(ip_coarse_per_minute=50, burst_limit=1000, device_per_window=1000)
    for i in range(50):
        hit(limiter, device=f"pc-{i}", ip="198.51.100.9")
    with pytest.raises(RateLimitExceeded) as info:
        hit(limiter, device="pc-x", ip="198.51.100.9")
    assert limiter.stats["ip_coarse_limited"] == 1 and info.value.retry_after == 60
    hit(limiter, device="pc-y", ip="198.51.100.10")  # another IP is fine
    hit(limiter, device="pc-z", ip=None)  # unverified: skipped
    clock.advance(60)
    hit(limiter, device="pc-x", ip="198.51.100.9")


def test_tables_are_bounded():
    limiter, _, _ = make(max_tracked_ips=100, max_tracked_devices=100)
    for i in range(1000):
        fail(limiter, f"10.0.{i // 250}.{i % 250 + 1}")
        hit(limiter, device=f"d{i}", ip=f"10.1.{i // 250}.{i % 250 + 1}")
    assert len(limiter._ip_failures) <= 100 and len(limiter._burst) <= 100 and len(limiter._ip_coarse) <= 100


# ─── shadow mode ───────────────────────────────────────────────────────────


def test_shadow_mode_records_would_limit_decisions_safely_and_never_refuses(caplog):
    limiter, counters, _ = make("shadow", device_per_window=3, burst_limit=1000)
    with caplog.at_level(logging.INFO, logger="zaza_server.ratelimit"):
        for _ in range(8):
            hit(limiter, device="secret-device-id", org="secret-org-id", ip="203.0.113.77")
    assert limiter.stats["device_shared_would_limit"] == 5 and counters.calls == 8  # still counted
    assert "limited" not in {k.split("_")[-1] for k in limiter.stats}
    text = caplog.text
    assert "sync_rate_limit event=would_limit limit=device_shared mode=shadow" in text
    for secret in ("secret-device-id", "secret-org-id", "203.0.113.77", "digest-"):
        assert secret not in text


def test_enforce_mode_logs_limited_events_without_secrets(caplog):
    limiter, _, _ = make(device_per_window=1, burst_limit=1000)
    with caplog.at_level(logging.INFO, logger="zaza_server.ratelimit"):
        hit(limiter, device="secret-device-id", org="secret-org-id", ip="203.0.113.77")
        refused(limiter, device="secret-device-id", org="secret-org-id", ip="203.0.113.77")
    assert "event=limited limit=device_shared mode=enforce" in caplog.text
    assert "secret" not in caplog.text and "203.0.113" not in caplog.text


# ─── trusted client IP ─────────────────────────────────────────────────────


def _client(env, *, header="X-Zaza-Client-IP", hsts=0, mode="cloud-run"):  # noqa: ANN001, ANN202
    app = create_saas_app(env.runtime)
    http_security.install(app, HttpSettings(public_hosts=("work.example.test",), mode=mode,
                                            trusted_client_ip_header=header if mode == "cloud-run" else "",
                                            hsts_max_age_seconds=hsts))
    return TestClient(app, base_url="https://work.example.test", raise_server_exceptions=False)


def _ip_seen(env, headers, **client_kw):  # noqa: ANN001, ANN003, ANN202
    """What trusted_client_ip() returns for a request with these headers."""
    app = FastAPI()
    seen: dict = {}

    @app.get("/probe")
    def probe(request: Request):  # noqa: ANN202
        seen["ip"] = trusted_client_ip(request)
        return {}

    settings = HttpSettings(public_hosts=("work.example.test",), mode="cloud-run",
                            trusted_client_ip_header=client_kw.get("header", "X-Zaza-Client-IP"))
    http_security.install(app, settings)
    TestClient(app, base_url="https://work.example.test").get("/probe", headers=headers)
    return seen["ip"]


@pytest.mark.parametrize(("headers", "expected"), [
    ([("X-Zaza-Client-IP", "203.0.113.7")], "203.0.113.7"),
    ([("X-Zaza-Client-IP", "2001:db8::7")], "2001:db8::7"),
    ([("X-Zaza-Client-IP", "::ffff:203.0.113.7")], "203.0.113.7"),
    ([("x-zaza-client-ip", " 203.0.113.7 ")], "203.0.113.7"),
    ([], None),
    ([("X-Zaza-Client-IP", "")], None),
    ([("X-Zaza-Client-IP", "not-an-ip")], None),
    ([("X-Zaza-Client-IP", "203.0.113.7, 198.51.100.1")], None),  # a list is not one address
    ([("X-Zaza-Client-IP", "203.0.113.7:4711")], None),  # no ports
    ([("X-Zaza-Client-IP", "[2001:db8::7]")], None),
    ([("X-Zaza-Client-IP", "203.0.113.7"), ("X-Zaza-Client-IP", "198.51.100.1")], None),  # two values: unverified
    ([("X-Forwarded-For", "203.0.113.7")], None),  # never used
    ([("X-Forwarded-For", "203.0.113.7"), ("X-Zaza-Client-IP", "198.51.100.1")], "198.51.100.1"),  # XFF ignored
])
def test_trusted_client_ip_extraction(headers, expected):
    assert _ip_seen(None, headers) == expected


def test_the_trusted_header_is_only_accepted_in_the_load_balanced_cloud_run_mode():
    for mode in ("local", "loopback-proxy"):
        with pytest.raises(ConfigError):
            HttpSettings(mode=mode, trusted_client_ip_header="X-Zaza-Client-IP")
    with pytest.raises(ConfigError):
        HttpSettings(mode="cloud-run", trusted_client_ip_header="X-Forwarded-For")
    with pytest.raises(ConfigError):
        HttpSettings(mode="cloud-run", trusted_client_ip_header="bad header!")
    s = http_security.http_settings_from_env({"ZAZA_HTTP_DEPLOYMENT_MODE": "cloud-run", "ZAZA_PUBLIC_HOST": "work.example.test",
                                              "ZAZA_TRUSTED_CLIENT_IP_HEADER": "X-Zaza-Client-IP"})
    assert s.trusted_client_ip_header == "X-Zaza-Client-IP"
    # without the Cloud Run middleware nothing is ever trusted, whatever the request carries
    app = FastAPI()
    seen: dict = {}

    @app.get("/probe")
    def probe(request: Request):  # noqa: ANN202
        seen["ip"] = trusted_client_ip(request)
        return {}

    http_security.install(app, HttpSettings(mode="local"))
    TestClient(app).get("/probe", headers={"X-Zaza-Client-IP": "203.0.113.7"})
    assert seen["ip"] is None


# ─── the real app: enforce, shadow, invalid traffic ────────────────────────


def _enrolled(env):  # noqa: ANN001, ANN202
    _, code = _code(env)
    return _enroll(env, code).json()["device_token"]


def _me(client, token, device="device-1", ip=None, extra=None):  # noqa: ANN001, ANN202
    headers = {"Authorization": f"Bearer {token}", DEVICE_ID_HEADER: device}
    if ip:
        headers["X-Zaza-Client-IP"] = ip
    headers.update(extra or {})
    return client.get(DEVICE_PATH, headers=headers)


def _install(env, mode="enforce", **overrides):  # noqa: ANN001, ANN003, ANN202
    clock = Clock()
    counters = FakeCounters(clock)
    overrides.setdefault("ip_mode", mode)
    limiter = SyncRateLimiter(SyncLimitSettings(mode=mode, **overrides), counters, clock=clock, rng=lambda: 1.0)
    env.runtime.devices.limiter = limiter
    env.runtime.limiter = limiter
    return limiter, counters, clock


def test_enforced_device_limit_returns_a_stable_non_secret_429():
    env = _env()
    token = _enrolled(env)
    limiter, counters, clock = _install(env, device_per_window=3, burst_limit=100)
    client = _client(env)
    assert [_me(client, token).status_code for _ in range(3)] == [200, 200, 200]
    r = _me(client, token)
    assert r.status_code == 429 and r.json() == {"error": "rate limit exceeded"}
    assert r.headers["retry-after"].isdigit() and 1 <= int(r.headers["retry-after"]) <= MAX_RETRY_AFTER
    text = r.text + str(dict(r.headers))
    for secret in (token, "device-1", str(env.t["a"]["org"]), "employee", "tenant", "zaza_t_"):
        assert secret not in text
    assert "www-authenticate" not in r.headers  # a 429 is not an authentication failure
    clock.advance(int(r.headers["retry-after"]))
    assert _me(client, token).status_code == 200


def test_the_batch_endpoint_is_limited_the_same_way_and_a_limited_batch_is_not_processed():
    env = _env()
    token = _enrolled(env)
    _install(env, device_per_window=1, burst_limit=100)
    client = _client(env)
    headers = {"Authorization": f"Bearer {token}", DEVICE_ID_HEADER: "device-1", "content-type": "application/json"}
    assert client.post(BATCH_PATH, headers=headers, content=b"{}").status_code in (200, 422)  # counted, then validated
    r = client.post(BATCH_PATH, headers=headers, content=b"{}")
    assert r.status_code == 429 and r.json() == {"error": "rate limit exceeded"} and "retry-after" in r.headers


def test_shadow_mode_never_returns_429_and_records_would_limit():
    env = _env()
    token = _enrolled(env)
    limiter, _, _ = _install(env, mode="shadow", device_per_window=2, burst_limit=100)
    client = _client(env)
    assert all(_me(client, token).status_code == 200 for _ in range(6))
    assert limiter.stats["device_shared_would_limit"] == 4


def test_off_mode_changes_nothing():
    env = _env()
    token = _enrolled(env)
    limiter, counters, _ = _install(env, mode="off", device_per_window=1)
    client = _client(env)
    assert all(_me(client, token).status_code == 200 for _ in range(5)) and counters.calls == 0


def test_a_counter_failure_does_not_lose_telemetry_in_the_real_app():
    env = _env()
    token = _enrolled(env)
    limiter, counters, _ = _install(env, device_per_window=1, burst_limit=100)
    counters.fail = RuntimeError("counter store down")
    client = _client(env)
    assert all(_me(client, token).status_code == 200 for _ in range(10))
    assert limiter.stats["device_shared_counter_failure"] == 10


def test_two_devices_in_one_organization_have_independent_limits():
    env = _env()
    t1 = _enrolled(env)
    env.t["a"]["repo"].add_employee("employee-2", "Second")
    _, code2 = _code(env, employee="employee-2")
    t2 = _enroll(env, code2, device="device-2").json()["device_token"]
    _install(env, device_per_window=2, burst_limit=100)
    client = _client(env)
    assert [_me(client, t1).status_code for _ in range(3)] == [200, 200, 429]
    assert [_me(client, t2, device="device-2").status_code for _ in range(2)] == [200, 200]


def test_invalid_traffic_is_limited_per_verified_ip_and_responses_leak_nothing():
    env = _env()
    token = _enrolled(env)
    limiter, _, clock = _install(env, ip_fail_burst_limit=5, ip_fail_per_window=8)
    client = _client(env)
    bad_ip, other_ip = "203.0.113.50", "203.0.113.51"
    unknown = "zzd_" + "u" * 43
    failures = [
        client.get(DEVICE_PATH, headers={"X-Zaza-Client-IP": bad_ip}),                         # no credentials at all
        client.get(DEVICE_PATH, headers={"X-Zaza-Client-IP": bad_ip, "Authorization": "Basic abc",
                                         DEVICE_ID_HEADER: "device-1"}),                       # malformed header
        _me(client, unknown, ip=bad_ip),                                                       # unknown token
        _me(client, "zzd_" + "k" * 43, ip=bad_ip),                                             # another unknown token
        _me(client, unknown, device="no-such-device", ip=bad_ip),                              # unknown token, odd device
    ]
    assert [r.status_code for r in failures] == [401] * 5
    blocked = [_me(client, unknown, ip=bad_ip), client.get(DEVICE_PATH, headers={"X-Zaza-Client-IP": bad_ip}),
               _me(client, "zzd_" + "m" * 43, ip=bad_ip)]
    assert [r.status_code for r in blocked] == [429] * 3
    assert {r.text for r in blocked} == {'{"error":"rate limit exceeded"}'}  # identical for every kind of bad request
    assert all(r.headers["retry-after"].isdigit() for r in blocked)
    assert limiter.stats["ip_failures_limited"] == 3
    # nothing about the account, device or token leaks, and the answer never says which request kind was refused
    assert all(s not in r.text + str(dict(r.headers)) for r in blocked for s in (token, unknown, "device-1", "zaza"))
    assert _me(client, unknown, ip=other_ip).status_code == 401  # a different IP is independent
    clock.advance(70)
    assert _me(client, unknown, ip=bad_ip).status_code == 401  # the block expired


def test_known_good_device_on_a_blocked_ip_still_syncs():
    env = _env()
    token = _enrolled(env)
    limiter, _, _ = _install(env, ip_fail_burst_limit=3, ip_fail_per_window=6)
    client = _client(env)
    ip = "203.0.113.60"
    assert _me(client, token, ip=ip).status_code == 200  # authenticates: now known good
    for _ in range(3):
        assert _me(client, "zzd_" + "x" * 43, ip=ip).status_code == 401
    assert _me(client, "zzd_" + "y" * 43, ip=ip).status_code == 429  # the IP is over its failure budget
    assert _me(client, token, ip=ip).status_code == 200  # a legitimate agent on the same public IP is unaffected


def test_revoked_credentials_count_as_failures_for_the_ip():
    env = _env()
    token = _enrolled(env)
    limiter, _, _ = _install(env, ip_fail_burst_limit=3, ip_fail_per_window=6)
    env.t["a"]["repo"].revoke_tokens("device-1")
    client = _client(env)
    statuses = [_me(client, token, ip="203.0.113.70").status_code for _ in range(5)]
    assert statuses[:3] == [403, 403, 403] and statuses[3:] == [429, 429]  # revoked -> 403, then the IP is limited


def test_unverified_client_ip_skips_the_ip_limiters_in_the_real_app():
    env = _env()
    token = _enrolled(env)
    limiter, _, _ = _install(env, ip_fail_burst_limit=2, ip_fail_per_window=4)
    client = _client(env)
    forged = {"X-Forwarded-For": "203.0.113.80"}
    for _ in range(20):  # no trusted header at all: never limited by IP, XFF is ignored
        assert _me(client, "zzd_" + "q" * 43, extra=forged).status_code == 401
    assert limiter.stats.get("ip_failures_limited", 0) == 0 and limiter.stats["ip_unverified_skipped"] >= 20
    # malformed and duplicated trusted headers are unverified too
    assert _me(client, "zzd_" + "q" * 43, ip="not-an-ip").status_code == 401
    r = client.get(DEVICE_PATH, headers=[("X-Zaza-Client-IP", "203.0.113.81"), ("X-Zaza-Client-IP", "203.0.113.82"),
                                         ("Authorization", "Bearer zzd_" + "q" * 43), (DEVICE_ID_HEADER, "device-1")])
    assert r.status_code == 401
    # the authenticated device limiter keeps working without any trusted IP
    _install(env, device_per_window=2, burst_limit=100)
    assert [_me(client, token).status_code for _ in range(3)] == [200, 200, 429]


def test_a_forged_x_forwarded_for_cannot_evade_or_trigger_the_ip_limiter():
    env = _env()
    _enrolled(env)
    limiter, _, _ = _install(env, ip_fail_burst_limit=3, ip_fail_per_window=6)
    client = _client(env)
    for i in range(10):
        r = _me(client, "zzd_" + "w" * 43, ip="203.0.113.90", extra={"X-Forwarded-For": f"198.51.100.{i}"})
        assert r.status_code in (401, 429)
    assert limiter.stats["ip_failures_limited"] >= 1  # keyed by the trusted header, not by the rotating XFF value


# ─── HSTS ──────────────────────────────────────────────────────────────────


def test_hsts_on_every_https_response_when_enabled():
    env = _env()
    token = _enrolled(env)
    _install(env, device_per_window=1, burst_limit=100)
    client = _client(env, hsts=300)
    responses = {
        200: client.get("/api/v1/sync/health"),
        401: client.get(DEVICE_PATH),
        404: client.get("/no/such/path"),
    }
    assert _me(client, token).status_code == 200
    responses[429] = _me(client, token)
    for status, r in responses.items():
        assert r.status_code == status
        assert r.headers["strict-transport-security"] == "max-age=300", status
        value = r.headers["strict-transport-security"].lower()
        assert "includesubdomains" not in value and "preload" not in value
    bad_host = client.get("/api/v1/sync/health", headers={"Host": "evil.example"})
    assert bad_host.status_code == 400 and bad_host.headers["strict-transport-security"] == "max-age=300"


def test_hsts_is_absent_when_disabled_and_exactly_the_configured_value_otherwise():
    env = _env()
    assert "strict-transport-security" not in _client(env, hsts=0).get("/api/v1/sync/health").headers
    assert _client(env, hsts=86400).get("/api/v1/sync/health").headers["strict-transport-security"] == "max-age=86400"
    assert http_security.http_settings_from_env({"ZAZA_HTTP_DEPLOYMENT_MODE": "cloud-run"}).hsts_max_age_seconds == 0
    s = http_security.http_settings_from_env({"ZAZA_HTTP_DEPLOYMENT_MODE": "cloud-run", "ZAZA_HSTS_MAX_AGE_SECONDS": "300"})
    assert s.hsts_max_age_seconds == 300


def test_hsts_only_in_cloud_run_mode_and_only_within_range():
    for mode in ("local", "loopback-proxy"):
        with pytest.raises(ConfigError):
            HttpSettings(mode=mode, hsts_max_age_seconds=300)  # never on a deployment that may serve plain HTTP
    for bad in (-1, 63_072_001):
        with pytest.raises(ConfigError):
            HttpSettings(mode="cloud-run", hsts_max_age_seconds=bad)
    with pytest.raises(ConfigError):
        http_security.http_settings_from_env({"ZAZA_HTTP_DEPLOYMENT_MODE": "cloud-run", "ZAZA_HSTS_MAX_AGE_SECONDS": "x"})
    app_env = _env()
    plain = TestClient(create_saas_app(app_env.runtime))  # no middleware at all, e.g. local development
    assert "strict-transport-security" not in plain.get("/api/v1/sync/health").headers


def test_existing_security_headers_are_unchanged_by_hsts():
    env = _env()
    without = _client(env, hsts=0).get("/app/login")
    with_hsts = _client(env, hsts=300).get("/app/login")
    assert without.status_code == with_hsts.status_code == 200
    base = {k: v for k, v in with_hsts.headers.items() if k != "strict-transport-security"}
    assert base == dict(without.headers) or {k: v for k, v in base.items() if k != "set-cookie"} == {
        k: v for k, v in without.headers.items() if k != "set-cookie"}
    for name in ("content-security-policy", "x-frame-options", "x-content-type-options", "cache-control",
                 "referrer-policy"):
        assert with_hsts.headers[name] == without.headers[name]
    assert "frame-ancestors 'none'" in with_hsts.headers["content-security-policy"]


# ─── the real agent worker and a 429 ───────────────────────────────────────


def test_agent_treats_429_as_retryable_keeps_records_queued_and_retries_idempotently(agent, clock, make_worker):
    from deskmate.zaza.sync.worker import SyncState  # noqa: PLC0415

    from .conftest import work_session  # noqa: PLC0415

    work_session(agent, clock)
    seen: list[tuple[str, frozenset]] = []
    answers = iter([429, 429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        seen.append((request.url.path, frozenset((r["record_type"], r["record_id"], r["record_version"])
                                                 for r in body["records"])))
        status = next(answers)
        if status == 429:
            return httpx.Response(429, headers={"Retry-After": "17"}, json={"error": "rate limit exceeded"})
        return ack_all(request)

    worker = make_worker(agent, mock_transport(handler))
    pending_before = sum(len(agent.store.pending_sync(t)) for t in
                         ("work_sessions", "activity_periods", "idle_periods", "app_usage_daily"))
    first = worker.run_once()
    assert not first.ok and first.error_kind == "rate_limited" and first.retry_after == 17
    assert worker.health().state == SyncState.SERVER_ERROR != SyncState.AUTH_ERROR  # not an authentication failure
    assert worker.next_delay(first) >= 17  # Retry-After is respected
    assert sum(len(agent.store.pending_sync(t)) for t in
               ("work_sessions", "activity_periods", "idle_periods", "app_usage_daily")) == pending_before  # still queued
    second = worker.run_once()
    assert second.error_kind == "rate_limited"
    final = worker.run_once()
    assert final.ok and final.sent > 0 and worker.health().state == SyncState.HEALTHY
    assert sum(len(agent.store.pending_sync(t)) for t in
               ("work_sessions", "activity_periods", "idle_periods", "app_usage_daily")) == 0  # normally confirmed
    # idempotent: every attempt re-sent the SAME records (the server upserts per record), only the batch endpoint was used
    assert seen[0][1] == seen[1][1] == seen[2][1] and {p for p, _ in seen} == {BATCH_PATH}


def test_the_saas_runtime_wires_one_limiter_into_the_device_authenticator():
    from types import SimpleNamespace  # noqa: PLC0415

    from deskmate.zaza_server.tenancy.saas_sync import SaasRuntime  # noqa: PLC0415

    limiter = SyncRateLimiter(SyncLimitSettings(mode="shadow"))
    fake = SimpleNamespace()
    runtime = SaasRuntime(control=fake, resolver=fake, manager=fake, routes=fake, auth=fake, limiter=limiter)
    assert runtime.limiter is limiter and runtime.devices.limiter is limiter
    assert SaasRuntime(control=fake, resolver=fake, manager=fake, routes=fake, auth=fake).devices.limiter is None


# ─── separate modes: the device limiter and the per-IP limiters ────────────


def test_ip_mode_defaults_to_shadow_and_is_parsed_and_validated():
    assert SyncLimitSettings(mode="enforce").ip_mode == "shadow"
    s = sync_limit_settings_from_env({"ZAZA_SYNC_LIMIT_MODE": "enforce", "ZAZA_SYNC_LIMIT_IP_MODE": "Enforce"})
    assert s.enforcing and s.ip_enforcing
    assert not sync_limit_settings_from_env({"ZAZA_SYNC_LIMIT_MODE": "enforce"}).ip_enforcing
    assert not sync_limit_settings_from_env({"ZAZA_SYNC_LIMIT_IP_MODE": "enforce"}).ip_active  # limiter itself is off
    with pytest.raises(ConfigError):
        sync_limit_settings_from_env({"ZAZA_SYNC_LIMIT_IP_MODE": "strict"})


def test_device_limiter_can_enforce_while_the_ip_limiters_stay_in_shadow(caplog):
    limiter, _, _ = make("enforce", ip_mode="shadow", device_per_window=3, burst_limit=1000, ip_fail_burst_limit=3,
                         ip_coarse_per_minute=5)
    with caplog.at_level(logging.INFO, logger="zaza_server.ratelimit"):
        for _ in range(10):
            fail(limiter, "203.0.113.5")  # far over the failure budget: recorded, never refused
        for i in range(3):
            hit(limiter, device=f"pc-{i}", ip="198.51.100.9")
        for i in range(3, 8):
            hit(limiter, device=f"pc-{i}", ip="198.51.100.9")  # over the coarse safeguard: recorded, never refused
        for _ in range(3):
            hit(limiter)  # one device, up to its shared limit of 3
        refused(limiter)  # the DEVICE limit is still enforced
    assert limiter.stats["ip_failures_would_limit"] >= 1 and limiter.stats["ip_coarse_would_limit"] >= 1
    assert limiter.stats.get("ip_failures_limited", 0) == 0 and limiter.stats.get("ip_coarse_limited", 0) == 0
    assert "limit=ip_failures mode=shadow" in caplog.text and "203.0.113.5" not in caplog.text


def test_ip_limiters_can_enforce_while_the_device_limiter_only_observes():
    limiter, _, _ = make("shadow", ip_mode="enforce", device_per_window=1, burst_limit=1000, ip_fail_burst_limit=3)
    for _ in range(5):
        hit(limiter)  # device limit exceeded: observed only
    assert limiter.stats["device_shared_would_limit"] == 4
    fail(limiter, "203.0.113.5", 3)
    with pytest.raises(RateLimitExceeded):
        limiter.gate("203.0.113.5", None)


def test_ip_mode_off_disables_every_ip_limiter_and_mode_off_disables_everything():
    limiter, counters, _ = make("enforce", ip_mode="off", ip_fail_burst_limit=1, ip_coarse_per_minute=1)
    for _ in range(20):
        fail(limiter, "203.0.113.5")
        hit(limiter, device="d", ip="203.0.113.5")
    assert not events(limiter) and counters.calls == 20
    off, off_counters, _ = make("off", ip_mode="enforce")
    for _ in range(20):
        fail(off, "203.0.113.5")
        hit(off, ip="203.0.113.5")
    assert off_counters.calls == 0 and not off.stats
