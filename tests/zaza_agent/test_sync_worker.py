"""Agent-side sync worker: end-to-end sync against the in-process API,
confirmation-only marking, partial acknowledgement, every failure mode the
spec lists, backoff, health states, and that tracking never stops."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid

import httpx
import pytest

from deskmate.zaza.health import HealthState
from deskmate.zaza.sync.backoff import Backoff
from deskmate.zaza.sync.credentials import DeviceCredentials
from deskmate.zaza.sync.protocol import BATCH_PATH
from deskmate.zaza.sync.transport import SyncTransport, validate_base_url
from deskmate.zaza.sync.worker import SyncState
from deskmate.zaza.timeutil import iso_utc

from .conftest import tick_for, work_session

SYNC_TABLES = ("work_sessions", "activity_periods", "idle_periods", "app_usage_daily")
SECRET = "zzd_THIS-IS-A-SECRET-TOKEN-VALUE"


def _local_rows(store):
    return {t: store._query(f"SELECT * FROM {t}") for t in SYNC_TABLES}


def _local_sync_states(store):
    return {t: [(r["sync_status"], r["record_version"], r["sync_attempts"]) for r in rows]
            for t, rows in _local_rows(store).items()}


def _closed_unsynced(store):
    return sum(len(store.pending_sync(t)) for t in SYNC_TABLES)


def mock_transport(handler, *, token=SECRET) -> SyncTransport:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SyncTransport("https://sync.example.test", DeviceCredentials("device-1", token), client=client)


def ack_all(request: httpx.Request, status="accepted") -> httpx.Response:
    body = json.loads(request.content)
    results = [
        {"record_type": r["record_type"], "record_id": r["record_id"], "record_version": r["record_version"],
         "status": status, "server_version": r["record_version"]}
        for r in body["records"]
    ]
    return httpx.Response(200, json={
        "batch_id": body["batch_id"], "server_time": "2026-10-07T09:00:00+00:00",
        "results": results, "summary": {status: len(results)},
    })


# ─── end to end ────────────────────────────────────────────────────────────


def test_end_to_end_sync_reaches_server_and_marks_local_synced(agent, clock, server, make_worker):
    session_id = work_session(agent, clock)
    worker = make_worker(agent, server.transport())
    result = worker.run_once()
    assert result.ok and result.failed == 0
    types = {r.record_type for r in server.repo.list_records()}
    assert types == {"work_session", "activity_period", "idle_period", "app_usage_daily"}
    session = server.repo.get_record("work_session", session_id)
    assert session.payload["data"]["status"] == "CLOSED"
    assert session.employee_id == "emp-1"
    assert _closed_unsynced(agent.store) == 0
    for rows in _local_rows(agent.store).values():
        for row in rows:
            assert row["sync_status"] == "SYNCED" and row["synced_version"] == row["record_version"]
    assert worker.health().state == SyncState.HEALTHY


def test_resync_after_success_sends_nothing_and_creates_no_duplicates(agent, clock, server, make_worker):
    work_session(agent, clock)
    worker = make_worker(agent, server.transport())
    worker.run_once()
    count = server.repo.count_records()
    again = worker.run_once()
    assert again.sent == 0
    assert server.repo.count_records() == count


def test_open_records_are_sent_and_later_versions_update(agent, clock, server, make_worker):
    agent.recorder.record_keyboard()
    agent.tick()
    worker = make_worker(agent, server.transport())
    worker.run_once()
    open_period = agent.store.periods(agent.session_id)[0]
    assert server.repo.get_record("activity_period", open_period["period_id"]).payload["data"]["is_open"] is True
    clock.advance(10)
    agent.recorder.record_keyboard()
    agent.tick()
    agent.end_session()
    worker.run_once()
    stored = server.repo.get_record("activity_period", open_period["period_id"])
    assert stored.payload["data"]["is_open"] is False
    assert stored.record_version == agent.store.get_period(open_period["period_id"])["record_version"]
    assert server.repo.count_records(record_type="activity_period") == 1


def test_no_activity_events_are_ever_sent(agent, clock, make_worker):
    work_session(agent, clock)
    assert agent.store.count_events() > 0
    sent_types = set()

    def handler(request):
        body = json.loads(request.content)
        sent_types.update(r["record_type"] for r in body["records"])
        assert "activity_events" not in request.content.decode()
        return ack_all(request)

    make_worker(agent, mock_transport(handler)).run_once()
    assert sent_types == {"work_session", "activity_period", "idle_period", "app_usage_daily"}


def test_all_sent_timestamps_are_utc(agent, clock, make_worker):
    work_session(agent, clock)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body["sent_at"])
        for rec in body["records"]:
            seen.extend([rec["created_at"], rec["updated_at"]])
            seen.extend(v for k, v in rec["data"].items() if k.endswith(("_at", "_utc")) and v)
        return ack_all(request)

    make_worker(agent, mock_transport(handler)).run_once()
    assert seen and all(ts.endswith("+00:00") for ts in seen)


# ─── confirmation-only marking & partial acknowledgement ───────────────────


def test_records_marked_synced_only_when_confirmed(agent, clock, make_worker):
    work_session(agent, clock)

    def handler(request):
        body = json.loads(request.content)
        first = body["records"][0]
        return httpx.Response(200, json={
            "batch_id": body["batch_id"], "server_time": "2026-10-07T09:00:00+00:00",
            "results": [{"record_type": first["record_type"], "record_id": first["record_id"],
                         "record_version": first["record_version"], "status": "accepted"}],
            "summary": {"accepted": 1},
        })

    result = make_worker(agent, mock_transport(handler)).run_once()
    assert result.confirmed == 1
    assert result.failed == result.sent - 1  # unacknowledged ones are not assumed synced
    synced = [r for rows in _local_rows(agent.store).values() for r in rows if r["sync_status"] == "SYNCED"]
    assert len(synced) == 1


def test_partial_acknowledgement_maps_each_status(agent, clock, make_worker):
    work_session(agent, clock)
    plan = ["accepted", "stale", "conflict", "rejected"]

    def handler(request):
        body = json.loads(request.content)
        results = []
        for i, rec in enumerate(body["records"]):
            status = plan[i] if i < len(plan) else "already_current"
            results.append({
                "record_type": rec["record_type"], "record_id": rec["record_id"],
                "record_version": rec["record_version"], "status": status,
                "server_version": rec["record_version"] + 5 if status == "stale" else rec["record_version"],
                "error": "bad data" if status == "rejected" else None,
            })
        return httpx.Response(200, json={"batch_id": body["batch_id"], "server_time": "2026-10-07T09:00:00Z",
                                         "results": results, "summary": {}})

    order = sorted(
        ((t, r) for t, rows in _local_rows(agent.store).items() for r in rows),
        key=lambda item: item[1]["local_seq"],
    )
    result = make_worker(agent, mock_transport(handler)).run_once()
    assert (result.confirmed, result.stale, result.conflicts, result.failed) == (len(order) - 3, 1, 1, 1)
    after = {(t, r["local_seq"]): r for t, rows in _local_rows(agent.store).items() for r in rows}
    accepted, stale, conflict, rejected = (after[(t, r["local_seq"])] for t, r in order[:4])
    assert accepted["sync_status"] == "SYNCED"
    assert stale["sync_status"] == "SYNCED" and stale["record_version"] == order[1][1]["record_version"] + 5
    assert conflict["sync_status"] == "PENDING" and conflict["record_version"] == order[2][1]["record_version"] + 1
    assert rejected["sync_status"] == "FAILED" and "bad data" in rejected["last_sync_error"]
    assert result.ok and SyncState(make_worker(agent, mock_transport(handler)).health().state) == SyncState.BACKLOG


def test_version_changed_while_request_in_flight_stays_pending(agent, clock, server, make_worker):
    agent.recorder.record_keyboard()
    agent.tick()
    real = server.transport()
    period_id = agent.store.periods(agent.session_id)[0]["period_id"]

    class TickDuringSend:
        def send_batch(self, payload):
            clock.advance(10)
            agent.recorder.record_keyboard()
            agent.tick()  # local versions move on while the request is "on the wire"
            return real.send_batch(payload)

    worker = make_worker(agent, TickDuringSend())
    worker.run_once()
    local = agent.store.get_period(period_id)
    server_copy = server.repo.get_record("activity_period", period_id)
    assert server_copy.record_version < local["record_version"]
    assert local["sync_status"] == "PENDING"  # newer local version not falsely marked synced
    worker.transport = real
    worker.run_once()
    assert server.repo.get_record("activity_period", period_id).record_version == local["record_version"]


def test_conflict_is_resolved_by_resending_newer_version(agent, clock, server, make_worker):
    session_id = work_session(agent, clock)
    worker = make_worker(agent, server.transport())
    worker.run_once()
    # local content changes without a version bump (simulated corruption/bug)
    agent.store._conn.execute(
        "UPDATE work_sessions SET end_reason = 'X', sync_status = 'PENDING' WHERE session_id = ?", (session_id,)
    )
    first = worker.run_once()
    assert first.conflicts == 1
    second = worker.run_once()
    assert second.confirmed == 1
    assert server.repo.get_record("work_session", session_id).payload["data"]["end_reason"] == "X"


def test_stale_adopts_server_version_so_later_changes_are_accepted(agent, clock, server, make_worker):
    agent.recorder.record_keyboard()
    agent.tick()
    for _ in range(3):  # a few ticks, so the period is at a version > 1
        clock.advance(10)
        agent.recorder.record_keyboard()
        agent.tick()
    worker = make_worker(agent, server.transport())
    worker.run_once()
    period = agent.store.periods(agent.session_id)[0]
    server_version = server.repo.get_record("activity_period", period["period_id"]).record_version
    assert server_version > 1
    # local DB rolled back (e.g. restored backup): local version far below server's
    agent.store._conn.execute(
        "UPDATE activity_periods SET record_version = 1, sync_status = 'PENDING' WHERE period_id = ?",
        (period["period_id"],),
    )
    result = worker.run_once()
    assert result.stale >= 1
    assert agent.store.get_period(period["period_id"])["record_version"] == server_version
    clock.advance(10)
    agent.recorder.record_keyboard()
    agent.tick()
    worker.run_once()
    assert server.repo.get_record("activity_period", period["period_id"]).record_version > server_version


# ─── failure modes: nothing is lost, nothing is marked ─────────────────────


@pytest.mark.parametrize(
    ("exc", "expected_state"),
    [
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), SyncState.OFFLINE),  # DNS failure
        (httpx.ConnectError("[WinError 10061] connection refused"), SyncState.OFFLINE),
        (httpx.ReadTimeout("timed out"), SyncState.OFFLINE),
        (httpx.ConnectTimeout("timed out"), SyncState.OFFLINE),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"), SyncState.OFFLINE),  # TLS
        (httpx.RemoteProtocolError("peer closed connection"), SyncState.OFFLINE),
    ],
)
def test_network_failures_leave_records_untouched(agent, clock, make_worker, exc, expected_state):
    work_session(agent, clock)
    before = _local_sync_states(agent.store)

    def handler(request):
        raise exc

    worker = make_worker(agent, mock_transport(handler))
    result = worker.run_once()
    assert not result.ok and result.error_kind == "network"
    assert _local_sync_states(agent.store) == before
    assert worker.health().state == expected_state


@pytest.mark.parametrize(
    ("status", "kind", "state"),
    [
        (500, "server", SyncState.SERVER_ERROR),
        (502, "server", SyncState.SERVER_ERROR),
        (503, "server", SyncState.SERVER_ERROR),
        (504, "server", SyncState.SERVER_ERROR),
        (429, "rate_limited", SyncState.SERVER_ERROR),
        (401, "auth", SyncState.AUTH_ERROR),
        (403, "auth", SyncState.AUTH_ERROR),
        (400, "protocol", SyncState.SERVER_ERROR),
    ],
)
def test_http_errors_leave_records_untouched(agent, clock, make_worker, status, kind, state):
    work_session(agent, clock)
    before = _local_sync_states(agent.store)
    worker = make_worker(agent, mock_transport(lambda r: httpx.Response(status, json={"error": "x"})))
    result = worker.run_once()
    assert not result.ok and result.error_kind == kind
    assert _local_sync_states(agent.store) == before
    assert worker.health().state == state


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>proxy error</html>"),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json={"batch_id": str(uuid.uuid4()), "server_time": "2026-10-07T09:00:00Z",
                                  "results": [], "summary": {}}),  # someone else's batch
        httpx.Response(200, json=[1, 2, 3]),
    ],
)
def test_malformed_server_response_marks_nothing(agent, clock, make_worker, response):
    work_session(agent, clock)
    before = _local_sync_states(agent.store)
    worker = make_worker(agent, mock_transport(lambda r: response))
    result = worker.run_once()
    assert not result.ok and result.error_kind == "protocol"
    assert _local_sync_states(agent.store) == before
    assert worker.health().state == SyncState.SERVER_ERROR


def test_real_connection_refused_is_offline(agent, clock, make_worker):
    work_session(agent, clock)
    transport = SyncTransport("http://127.0.0.1:9", DeviceCredentials("device-1", SECRET), timeout_seconds=3)
    worker = make_worker(agent, transport)
    assert worker.run_once().error_kind == "network"
    assert worker.health().state == SyncState.OFFLINE
    transport.close()


def test_auth_failure_keeps_local_data_and_credentials(agent, clock, make_worker, tmp_path):
    from deskmate.zaza.sync.credentials import load_credentials, save_credentials

    cred_file = tmp_path / "device_credentials.json"
    save_credentials(DeviceCredentials("device-1", SECRET), cred_file, protection="none")
    work_session(agent, clock)
    rows_before = {t: len(r) for t, r in _local_rows(agent.store).items()}
    worker = make_worker(agent, mock_transport(lambda r: httpx.Response(401, json={"error": "invalid credentials"})))
    result = worker.run_once()
    assert result.error_kind == "auth"
    assert {t: len(r) for t, r in _local_rows(agent.store).items()} == rows_before
    assert load_credentials(cred_file).token == SECRET
    delay = worker.next_delay(result)
    assert delay >= 600  # long, fixed-ish retry — not the transient backoff, not a tight loop


def test_server_503_then_recovery_retries_and_catches_up(agent, clock, server, make_worker):
    work_session(agent, clock)
    real = server.transport()
    calls = {"n": 0}

    class Flaky:
        def send_batch(self, payload):
            calls["n"] += 1
            if calls["n"] <= 2:
                from deskmate.zaza.sync.transport import ServerError

                raise ServerError(503, "HTTP 503")
            return real.send_batch(payload)

    worker = make_worker(agent, Flaky())
    delays = []
    for _ in range(3):
        result = worker.run_once()
        delays.append(worker.next_delay(result))
    assert delays[0] == pytest.approx(5) and delays[1] == pytest.approx(10)
    assert result.ok and _closed_unsynced(agent.store) == 0
    assert worker.health().state == SyncState.HEALTHY
    assert worker.health().consecutive_failures == 0


def test_429_honours_retry_after(agent, clock, make_worker):
    work_session(agent, clock)
    worker = make_worker(agent, mock_transport(lambda r: httpx.Response(429, headers={"Retry-After": "120"})))
    result = worker.run_once()
    assert result.error_kind == "rate_limited" and result.retry_after == 120
    assert worker.next_delay(result) == pytest.approx(120)


def test_413_shrinks_batch_size_and_retries(agent, clock, make_worker):
    work_session(agent, clock)
    sizes = []

    def handler(request):
        n = len(json.loads(request.content)["records"])
        sizes.append(n)
        return httpx.Response(413) if n > 2 else ack_all(request)

    worker = make_worker(agent, mock_transport(handler), batch_size=8)
    result = worker.run_once()
    assert result.ok and _closed_unsynced(agent.store) == 0
    assert worker.batch_size == 2 and sizes[:3] == [sizes[0], 4, 2]


# ─── backoff ───────────────────────────────────────────────────────────────


def test_backoff_is_exponential_bounded_and_resettable():
    b = Backoff(base_seconds=5, max_seconds=300, rng=lambda: 0.5)
    assert [b.next_delay() for _ in range(9)] == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    b.reset()
    assert b.next_delay() == 5


def test_backoff_jitter_stays_within_bounds():
    low = Backoff(rng=lambda: 0.0)
    high = Backoff(rng=lambda: 0.999999)
    assert low.next_delay() == pytest.approx(4.0)
    assert high.next_delay() == pytest.approx(6.0, rel=1e-3)
    capped = Backoff(max_seconds=300, rng=lambda: 0.999999)
    for _ in range(20):
        assert capped.next_delay() <= 300


def test_backoff_never_produces_a_tight_loop():
    b = Backoff(rng=lambda: 0.0)
    assert min(b.next_delay() for _ in range(50)) >= 4.0


# ─── large backlog, restart, health ────────────────────────────────────────


def _make_backlog(agent, clock, n):
    agent.recorder.record_keyboard()
    agent.tick()
    for i in range(n):
        agent.store.insert_period(
            period_id=str(uuid.uuid4()), session_id=agent.session_id, device_id="device-1",
            started_at=clock.now + i, ended_at=clock.now + i + 1, status="ACTIVE",
            start_reason="APP_CHANGE", end_reason="APP_CHANGE", app_name=f"app{i % 7}.exe", is_open=False,
        )


def test_large_backlog_processed_in_batches(agent, clock, server, make_worker):
    _make_backlog(agent, clock, 250)
    batch_sizes = []
    real = server.transport()

    class Spy:
        def send_batch(self, payload):
            batch_sizes.append(len(payload["records"]))
            return real.send_batch(payload)

    worker = make_worker(agent, Spy(), batch_size=100, max_batches_per_cycle=2)
    first = worker.run_once()
    assert first.more_pending and batch_sizes == [100, 100]
    assert worker.health().state == SyncState.BACKLOG
    assert worker.next_delay(first) == 1.0  # catch up promptly, but not in a tight loop
    second = worker.run_once()
    assert second.ok and not second.more_pending
    assert all(size <= 100 for size in batch_sizes)
    assert server.repo.count_records(record_type="activity_period") == 251
    assert _closed_unsynced(agent.store) == 0


def test_worker_restart_resumes_without_duplicates(agent, clock, server, make_worker):
    _make_backlog(agent, clock, 120)
    first = make_worker(agent, server.transport(), batch_size=50, max_batches_per_cycle=1)
    first.run_once()
    synced_after_first = server.repo.count_records()
    del first  # agent/worker restart: only SQLite state carries over
    second = make_worker(agent, server.transport(), batch_size=50)
    second.run_once()
    assert server.repo.count_records() == 121 + 1  # periods + the open session
    assert synced_after_first < server.repo.count_records()
    assert _closed_unsynced(agent.store) == 0


def test_health_reports_counts_and_timestamps(agent, clock, make_worker):
    work_session(agent, clock)
    worker = make_worker(agent, mock_transport(lambda r: httpx.Response(503)))
    assert worker.health().state == SyncState.NOT_CONFIGURED  # never attempted yet
    worker.run_once()
    health = worker.health()
    assert health.state == SyncState.SERVER_ERROR
    assert health.last_attempt_at == iso_utc(clock.now)
    assert health.last_success_at is None
    assert health.pending_count > 0 and health.consecutive_failures == 1
    assert "503" in health.last_error
    worker.transport = mock_transport(ack_all)
    clock.advance(30)
    worker.run_once()
    health = worker.health()
    assert health.state == SyncState.HEALTHY and health.last_success_at == iso_utc(clock.now)
    assert health.pending_count == 0 and health.failed_count == 0 and health.last_error is None


# ─── tracking never depends on sync ────────────────────────────────────────


def test_activity_recording_continues_while_sync_blocks_and_fails(make_agent, clock, tmp_path):
    from deskmate.zaza.storage import ActivityStore
    from deskmate.zaza.sync.worker import SyncWorker

    agent = make_agent()
    agent.recorder.record_keyboard()
    agent.tick()
    release = threading.Event()
    entered = threading.Event()

    class Hanging:
        def send_batch(self, payload):
            entered.set()
            release.wait(5)
            from deskmate.zaza.sync.transport import NetworkError

            raise NetworkError("timeout: ReadTimeout")

    worker_store = ActivityStore(agent.config.db_path)  # separate connection, as in production
    worker = SyncWorker(worker_store, Hanging(), device_id="device-1", employee_id="emp-1",
                        interval_seconds=3600, rng=lambda: 0.5)
    worker.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        for _ in range(20):
            clock.advance(10)
            agent.recorder.record_keyboard()
            agent.tick()  # must not wait on the stuck sync request
        assert time.monotonic() - started < 3
        assert agent.store.periods(agent.session_id)[0]["duration_seconds"] == 200
        assert agent.health_status() != HealthState.ERROR
    finally:
        release.set()
        worker.stop()
        worker_store.close()
    assert worker_store is not agent.store
    period = agent.store.periods(agent.session_id)[0]
    assert period["status"] == "ACTIVE"  # sync failure never turns into inactivity


def test_sync_state_does_not_affect_activity_classification(agent, clock, make_worker):
    agent.recorder.record_keyboard()
    agent.tick()
    worker = make_worker(agent, mock_transport(lambda r: httpx.Response(503)))
    for _ in range(5):
        worker.run_once()
        clock.advance(10)
        agent.recorder.record_keyboard()
        agent.tick()
    assert [p["status"] for p in agent.store.periods(agent.session_id)] == ["ACTIVE"]


# ─── secrets ───────────────────────────────────────────────────────────────


def test_secrets_are_never_logged_or_exposed(agent, clock, make_worker, caplog):
    caplog.set_level(logging.DEBUG)
    work_session(agent, clock)
    responses = iter([
        httpx.Response(401, json={"error": "invalid credentials"}),
        httpx.Response(503),
        httpx.Response(200, content=b"garbage"),
    ])

    def handler(request):
        assert request.headers["authorization"] == f"Bearer {SECRET}"  # header only…
        assert SECRET not in str(request.url)  # …never in the URL
        try:
            return next(responses)
        except StopIteration:
            raise httpx.ConnectError("refused") from None

    worker = make_worker(agent, mock_transport(handler))
    errors = []
    for _ in range(4):
        result = worker.run_once()
        errors.append(result.error or "")
    health = worker.health()
    assert SECRET not in caplog.text
    assert SECRET not in " ".join(errors)
    assert SECRET not in json.dumps(health.__dict__, default=str)
    assert SECRET not in repr(DeviceCredentials("device-1", SECRET))
    assert SECRET not in json.dumps(_local_rows(agent.store), default=str)
    assert SECRET not in agent.store._query("SELECT group_concat(value) AS v FROM sync_state")[0]["v"]


# ─── transport / config validation ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://sync.example.com", True),
        ("http://127.0.0.1:8765", True),
        ("http://localhost:8765/", True),
        ("http://sync.example.com", False),  # plain HTTP beyond localhost
        ("https://user:pw@sync.example.com", False),
        ("https://sync.example.com/?token=abc", False),  # no secrets in URLs
        ("ftp://sync.example.com", False),
    ],
)
def test_sync_url_validation(url, ok):
    if ok:
        assert validate_base_url(url)
    else:
        with pytest.raises(ValueError):
            validate_base_url(url)


def test_client_batch_size_is_bounded(agent, make_worker):
    with pytest.raises(ValueError):
        make_worker(agent, mock_transport(ack_all), batch_size=501)
    with pytest.raises(ValueError):
        make_worker(agent, mock_transport(ack_all), batch_size=0)


def test_batch_endpoint_path_is_versioned():
    assert BATCH_PATH == "/api/v1/sync/batch"


def test_unsynced_open_session_is_not_counted_as_backlog(agent, clock, make_worker):
    agent.recorder.record_keyboard()
    agent.tick()
    tick_for(agent, clock, 30)
    worker = make_worker(agent, mock_transport(ack_all))
    worker.run_once()
    health = worker.health()
    assert health.state == SyncState.HEALTHY
    assert health.open_count >= 2


# ─── regression: stale replies (Phase 3 review fix 1) ──────────────────────


def respond(decide):
    """Mock server: ``decide(rec)`` returns (status, server_version) per record."""
    def handler(request):
        body = json.loads(request.content)
        results = []
        for rec in body["records"]:
            status, server_version = decide(rec)
            results.append({"record_type": rec["record_type"], "record_id": rec["record_id"],
                            "record_version": rec["record_version"], "status": status,
                            "server_version": server_version})
        return httpx.Response(200, json={"batch_id": body["batch_id"], "server_time": "2026-10-07T09:00:00Z",
                                         "results": results, "summary": {}})
    return handler


def _closed_session_at_version(agent, clock, version):
    session_id = work_session(agent, clock)
    agent.store._conn.execute(
        "UPDATE work_sessions SET record_version = ?, sync_status = 'PENDING' WHERE session_id = ?",
        (version, session_id),
    )
    return session_id


def _session_row(agent, session_id):
    return agent.store._query("SELECT * FROM work_sessions WHERE session_id = ?", (session_id,))[0]


def _stale_for_session(session_id, server_version_of):
    def decide(rec):
        if rec["record_id"] == session_id:
            return "stale", server_version_of(rec["record_version"])
        return "accepted", rec["record_version"]
    return decide


def _assert_stale_not_synced(agent, worker, session_id, result):
    row = _session_row(agent, session_id)
    assert row["sync_status"] == "FAILED"
    assert row["record_version"] == 3 and row["synced_version"] != 3
    assert "protocol" in row["last_sync_error"]
    assert result.failed == 1 and result.stale == 0
    assert worker.health().state == SyncState.BACKLOG
    assert any(r["session_id"] == session_id for r in agent.store.pending_sync("work_sessions"))  # still retriable


def test_stale_with_missing_server_version_is_not_synced(agent, clock, make_worker):
    session_id = _closed_session_at_version(agent, clock, 3)
    worker = make_worker(agent, mock_transport(respond(_stale_for_session(session_id, lambda v: None))))
    _assert_stale_not_synced(agent, worker, session_id, worker.run_once())


def test_stale_with_equal_server_version_is_not_synced(agent, clock, make_worker):
    session_id = _closed_session_at_version(agent, clock, 3)
    worker = make_worker(agent, mock_transport(respond(_stale_for_session(session_id, lambda v: v))))
    _assert_stale_not_synced(agent, worker, session_id, worker.run_once())


def test_stale_with_lower_server_version_is_not_synced(agent, clock, make_worker):
    session_id = _closed_session_at_version(agent, clock, 3)
    worker = make_worker(agent, mock_transport(respond(_stale_for_session(session_id, lambda v: v - 1))))
    _assert_stale_not_synced(agent, worker, session_id, worker.run_once())


def test_stale_with_higher_server_version_adopts_it(agent, clock, make_worker):
    session_id = _closed_session_at_version(agent, clock, 3)
    worker = make_worker(agent, mock_transport(respond(_stale_for_session(session_id, lambda v: v + 4))))
    result = worker.run_once()
    row = _session_row(agent, session_id)
    assert result.stale == 1 and result.failed == 0
    assert row["sync_status"] == "SYNCED" and row["record_version"] == 7 and row["synced_version"] == 7
    assert worker.health().state == SyncState.HEALTHY


def test_valid_stale_while_local_changed_in_flight_keeps_newer_local_pending(agent, clock, make_worker):
    session_id = _closed_session_at_version(agent, clock, 3)
    handler = respond(_stale_for_session(session_id, lambda v: v + 4))

    def change_in_flight(request):
        agent.store.requeue_record("work_sessions", session_id, expected_version=3, at=clock.now)  # now v4
        return handler(request)

    worker = make_worker(agent, mock_transport(change_in_flight))
    result = worker.run_once()
    row = _session_row(agent, session_id)
    assert result.stale == 1
    assert row["record_version"] == 4 and row["sync_status"] == "PENDING"  # not adopted, not marked synced
    assert row["synced_version"] != 4
    assert worker.health().state == SyncState.BACKLOG


# ─── regression: any closed durable backlog is BACKLOG (review fix 2) ──────


def test_one_closed_pending_record_is_backlog(agent, clock, make_worker):
    session_id = work_session(agent, clock)

    def touch_in_flight(request):
        version = _session_row(agent, session_id)["record_version"]
        agent.store.requeue_record("work_sessions", session_id, expected_version=version, at=clock.now)
        return ack_all(request)

    worker = make_worker(agent, mock_transport(touch_in_flight), batch_size=100)
    result = worker.run_once()
    assert result.ok and not result.more_pending
    health = worker.health()
    assert (health.pending_count, health.failed_count) == (1, 0)
    assert health.state == SyncState.BACKLOG


def test_accepted_plus_one_conflict_remaining_is_backlog(agent, clock, make_worker):
    session_id = work_session(agent, clock)

    def decide(rec):
        return ("conflict" if rec["record_id"] == session_id else "accepted"), rec["record_version"]

    worker = make_worker(agent, mock_transport(respond(decide)))
    result = worker.run_once()
    assert result.conflicts == 1 and result.confirmed == result.sent - 1
    health = worker.health()
    assert health.pending_count == 1 and health.state == SyncState.BACKLOG


def test_conflict_only_result_is_backlog(agent, clock, make_worker):
    session_id = work_session(agent, clock)
    make_worker(agent, mock_transport(ack_all)).run_once()
    agent.store._conn.execute(
        "UPDATE work_sessions SET end_reason = 'X', sync_status = 'PENDING' WHERE session_id = ?", (session_id,)
    )
    worker = make_worker(agent, mock_transport(respond(lambda rec: ("conflict", rec["record_version"]))))
    result = worker.run_once()
    assert (result.sent, result.conflicts, result.confirmed) == (1, 1, 0)
    assert worker.health().state == SyncState.BACKLOG


def test_requeued_newer_local_version_is_backlog(agent, clock, make_worker):
    session_id = _closed_session_at_version(agent, clock, 3)
    handler = respond(_stale_for_session(session_id, lambda v: v + 4))

    def change_in_flight(request):
        agent.store.requeue_record("work_sessions", session_id, expected_version=3, at=clock.now)
        return handler(request)

    worker = make_worker(agent, mock_transport(change_in_flight), batch_size=100)
    worker.run_once()
    health = worker.health()
    assert (health.pending_count, health.failed_count) == (1, 0)
    assert health.state == SyncState.BACKLOG


def test_all_closed_durable_records_confirmed_is_healthy(agent, clock, make_worker):
    work_session(agent, clock)
    worker = make_worker(agent, mock_transport(ack_all))
    worker.run_once()
    health = worker.health()
    assert (health.pending_count, health.failed_count) == (0, 0)
    assert health.state == SyncState.HEALTHY


def test_only_current_open_records_unsynced_is_healthy(agent, clock, make_worker):
    agent.recorder.record_keyboard()
    agent.tick()

    def tick_in_flight(request):
        clock.advance(10)
        agent.recorder.record_keyboard()
        agent.tick()  # open session/period move to a newer version mid-request
        return ack_all(request)

    worker = make_worker(agent, mock_transport(tick_in_flight))
    worker.run_once()
    open_period = agent.store.periods(agent.session_id)[0]
    assert open_period["is_open"] and open_period["sync_status"] == "PENDING"
    health = worker.health()
    assert (health.pending_count, health.failed_count) == (0, 0) and health.open_count >= 2
    assert health.state == SyncState.HEALTHY
