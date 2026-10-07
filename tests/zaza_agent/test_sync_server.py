"""Central sync API: authentication, idempotent per-record processing,
version handling, partial success, and request validation. Runs against
both the in-memory and the SQLite development repositories."""

from __future__ import annotations

import logging
import uuid

import pytest

from deskmate.zaza.sync.protocol import BATCH_PATH, DEVICE_PATH, HEALTH_PATH, MAX_RECORDS_PER_BATCH
from deskmate.zaza_server.auth import hash_token, register_device, rotate_token
from deskmate.zaza_server.repository import InMemoryRepository, SqliteDevRepository

from .conftest import SyncServer

TS = "2026-10-07T09:00:00.000+00:00"
TS2 = "2026-10-07T09:05:00.000+00:00"


@pytest.fixture(params=["memory", "sqlite"])
def srv(request, tmp_path):
    repo = InMemoryRepository() if request.param == "memory" else SqliteDevRepository(tmp_path / "central.db")
    yield SyncServer(repo)
    if isinstance(repo, SqliteDevRepository):
        repo.close()


def period(record_id: str | None = None, version: int = 1, *, app: str = "Code.exe", **overrides) -> dict:
    record = {
        "record_type": "activity_period",
        "record_id": record_id or str(uuid.uuid4()),
        "record_version": version,
        "device_id": "device-1",
        "employee_id": "emp-1",
        "local_seq": 1,
        "created_at": TS,
        "updated_at": TS2,
        "data": {
            "session_id": str(uuid.uuid4()),
            "started_at": TS,
            "ended_at": TS2,
            "duration_seconds": 300.0,
            "is_open": False,
            "status": "ACTIVE",
            "status_detail": None,
            "app_name": app,
            "window_title": "agent.py - zaza",
            "domain": None,
            "privacy_excluded": False,
            "start_reason": "SESSION_START",
            "end_reason": "APP_CHANGE",
        },
    }
    for key, value in overrides.items():
        if key in record["data"]:
            record["data"][key] = value
        else:
            record[key] = value
    return record


def batch(*records: dict, **overrides) -> dict:
    body = {
        "batch_id": str(uuid.uuid4()),
        "device_id": "device-1",
        "agent_version": "0.3.0",
        "sent_at": TS2,
        "records": list(records),
    }
    body.update(overrides)
    return body


def post(srv: SyncServer, body: dict, **kw):
    return srv.client.post(BATCH_PATH, json=body, headers=kw.pop("headers", srv.headers()), **kw)


def statuses(response) -> list[str]:
    return [r["status"] for r in response.json()["results"]]


# ─── health & auth ─────────────────────────────────────────────────────────


def test_health_endpoint_needs_no_auth_and_exposes_no_data(srv):
    r = srv.client.get(HEALTH_PATH)
    assert r.status_code == 200
    assert set(r.json()) == {"status", "api_version", "server_time"}


def test_valid_authenticated_device_sync(srv):
    p = period()
    r = post(srv, batch(p))
    assert r.status_code == 200
    assert statuses(r) == ["accepted"]
    stored = srv.repo.get_record("activity_period", p["record_id"])
    assert stored.record_version == 1 and stored.device_id == "device-1" and stored.employee_id == "emp-1"
    assert stored.payload["data"]["app_name"] == "Code.exe"


def test_device_me_confirms_credentials(srv):
    r = srv.client.get(DEVICE_PATH, headers=srv.headers())
    assert r.json() == {"device_id": "device-1", "employee_id": "emp-1", "status": "ACTIVE"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer zzd_wrong", "X-ZaZa-Device-Id": "device-1"},
        {"Authorization": "Basic dXNlcjpwYXNz", "X-ZaZa-Device-Id": "device-1"},
        {"X-ZaZa-Device-Id": "device-1"},
    ],
)
def test_invalid_or_missing_token_rejected(srv, headers):
    r = post(srv, batch(period()), headers=headers)
    assert r.status_code == 401
    assert srv.repo.count_records() == 0


def test_token_for_another_device_rejected(srv):
    other = register_device(srv.repo, "device-2", "emp-2").token
    r = post(srv, batch(period()), headers=srv.headers(token=other, device_id="device-1"))
    assert r.status_code == 401


def test_disabled_device_rejected(srv):
    srv.repo.set_device_status("device-1", "DISABLED")
    r = post(srv, batch(period()))
    assert r.status_code == 403
    assert r.json()["error"] == "device disabled"
    assert srv.repo.count_records() == 0


def test_token_rotation_and_revocation(srv):
    new = rotate_token(srv.repo, "device-1").token
    assert post(srv, batch(period()), headers=srv.headers(token=new)).status_code == 200
    assert post(srv, batch(period())).status_code == 200  # old token still valid until revoked
    rotate_token(srv.repo, "device-1", revoke_old=True)
    assert post(srv, batch(period())).status_code == 403
    assert post(srv, batch(period()), headers=srv.headers(token=new)).status_code == 403


def test_server_stores_only_token_hash(srv):
    record = srv.repo.find_token(hash_token(srv.token))
    assert record is not None and record.token_hash != srv.token and srv.token not in repr(record)


def test_auth_failures_and_successes_never_log_tokens(srv, caplog):
    caplog.set_level(logging.DEBUG)
    post(srv, batch(period()))
    post(srv, batch(period()), headers=srv.headers(token="zzd_attempted_secret_value"))
    assert srv.token not in caplog.text
    assert "zzd_attempted_secret_value" not in caplog.text
    assert "Bearer" not in caplog.text


# ─── idempotency & versions ────────────────────────────────────────────────


def test_duplicate_identical_record_is_not_duplicated(srv):
    p = period()
    body = batch(p)
    assert statuses(post(srv, body)) == ["accepted"]
    assert statuses(post(srv, body)) == ["already_current"]  # exact resend (lost ack)
    assert statuses(post(srv, batch(p))) == ["already_current"]  # same record, new batch
    assert srv.repo.count_records() == 1


def test_higher_version_updates(srv):
    rid = str(uuid.uuid4())
    post(srv, batch(period(rid, 1, duration_seconds=100.0)))
    r = post(srv, batch(period(rid, 2, duration_seconds=250.0)))
    assert statuses(r) == ["updated"]
    stored = srv.repo.get_record("activity_period", rid)
    assert stored.record_version == 2
    assert stored.payload["data"]["duration_seconds"] == 250.0
    assert srv.repo.count_records() == 1


def test_lower_version_is_stale_and_does_not_overwrite(srv):
    rid = str(uuid.uuid4())
    post(srv, batch(period(rid, 4, app="newer.exe")))
    r = post(srv, batch(period(rid, 3, app="older.exe")))
    result = r.json()["results"][0]
    assert result["status"] == "stale"
    assert result["record_version"] == 3 and result["server_version"] == 4
    stored = srv.repo.get_record("activity_period", rid)
    assert stored.record_version == 4 and stored.payload["data"]["app_name"] == "newer.exe"


def test_same_version_different_content_is_conflict_and_server_keeps_original(srv):
    rid = str(uuid.uuid4())
    post(srv, batch(period(rid, 2, app="first.exe")))
    r = post(srv, batch(period(rid, 2, app="second.exe")))
    assert statuses(r) == ["conflict"]
    assert srv.repo.get_record("activity_period", rid).payload["data"]["app_name"] == "first.exe"


def test_record_id_owned_by_another_device_is_rejected(srv):
    other_token = register_device(srv.repo, "device-2", "emp-2").token
    rid = str(uuid.uuid4())
    post(srv, batch(period(rid)))
    r = post(
        srv,
        batch(period(rid, 9, device_id="device-2", employee_id="emp-2"), device_id="device-2"),
        headers=srv.headers(token=other_token, device_id="device-2"),
    )
    assert statuses(r) == ["rejected"]
    assert srv.repo.get_record("activity_period", rid).device_id == "device-1"


def test_record_device_or_employee_mismatch_rejected(srv):
    r = post(srv, batch(period(device_id="device-9"), period(employee_id="someone-else"), period()))
    assert statuses(r) == ["rejected", "rejected", "accepted"]


# ─── partial success & per-record validation ───────────────────────────────


def test_partial_batch_returns_per_record_results(srv):
    current = [period() for _ in range(5)]
    newer = [period(version=5) for _ in range(3)]
    post(srv, batch(*current, *newer))
    stale = [dict(p, record_version=2) for p in newer]
    fresh = [period() for _ in range(90)]
    invalid = [period(started_at="2026-10-07T09:00:00"), period(record_type="activity_event")]
    r = post(srv, batch(*fresh, *current, *stale, *invalid))
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == {
        "accepted": 90, "updated": 0, "already_current": 5, "stale": 3, "conflict": 0, "rejected": 2,
    }
    assert len(body["results"]) == 100
    assert [x["status"] for x in body["results"][95:]] == ["stale", "stale", "stale", "rejected", "rejected"]
    assert srv.repo.count_records() == 98


@pytest.mark.parametrize(
    "bad_ts", ["2026-10-07T09:00:00", "2026-10-07T11:00:00+02:00", "not-a-time"],
)
def test_non_utc_record_timestamps_rejected_per_record(srv, bad_ts):
    r = post(srv, batch(period(started_at=bad_ts), period()))
    assert statuses(r) == ["rejected", "accepted"]
    assert "started_at" in r.json()["results"][0]["error"]


def test_unknown_record_type_rejected(srv):
    raw_event = {"record_type": "activity_event", "record_id": str(uuid.uuid4()), "record_version": 1}
    assert statuses(post(srv, batch(raw_event))) == ["rejected"]


def test_unexpected_record_field_rejected(srv):
    p = period()
    p["data"]["typed_text"] = "should never be accepted"
    assert statuses(post(srv, batch(p))) == ["rejected"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(extra_field=1),
        lambda b: b.update(sent_at="2026-10-07T09:00:00"),
        lambda b: b.update(records=[]),
        lambda b: b.update(device_id="device-2"),
    ],
)
def test_invalid_envelope_rejected_with_422(srv, mutate):
    body = batch(period())
    mutate(body)
    assert post(srv, body).status_code == 422
    assert srv.repo.count_records() == 0


def test_batch_size_limit_enforced(srv):
    body = batch(*[period() for _ in range(MAX_RECORDS_PER_BATCH + 1)])
    assert post(srv, body).status_code == 422


def test_payload_byte_limit_enforced():
    from fastapi.testclient import TestClient

    from deskmate.zaza_server.app import create_app

    repo = InMemoryRepository()
    token = register_device(repo, "device-1", "emp-1").token
    client = TestClient(create_app(repo, max_body_bytes=2048))
    headers = {"Authorization": f"Bearer {token}", "X-ZaZa-Device-Id": "device-1"}
    r = client.post(BATCH_PATH, json=batch(*[period() for _ in range(10)]), headers=headers)
    assert r.status_code == 413
    assert repo.count_records() == 0


def test_sqlite_dev_repository_persists_across_restart(tmp_path):
    path = tmp_path / "central.db"
    first = SyncServer(SqliteDevRepository(path))
    p = period()
    post(first, batch(p))
    first.repo.close()
    repo = SqliteDevRepository(path)  # a server restart
    assert repo.get_record("activity_period", p["record_id"]).record_version == 1
    assert repo.get_device("device-1").employee_id == "emp-1"
    repo.close()
