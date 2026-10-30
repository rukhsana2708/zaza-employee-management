"""Enrollment of the installed agent with a one-time code.

The agent talks to the REAL server enrollment service (create_saas_app with an
in-memory enrollment store, see test_enrollment.py) through an ASGI test
client, so the code -> token exchange, the token check and the credential
storage are exercised together. Error messages are checked against mocked
server answers."""

from __future__ import annotations

import inspect
import io
import json
import logging
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from deskmate.zaza import control, enrollment, paths  # noqa: E402
from deskmate.zaza.enrollment import EnrollmentError, enroll_with_code  # noqa: E402
from deskmate.zaza.runner import Runner  # noqa: E402
from deskmate.zaza.sync.credentials import load_credentials  # noqa: E402
from deskmate.zaza.sync.protocol import DEVICE_ID_HEADER, DEVICE_PATH  # noqa: E402
from deskmate.zaza.sync.transport import SyncTransport  # noqa: E402

from .test_enrollment import _code, _env  # noqa: E402
from .test_installer import FakeAgent, _runner_thread, _wait  # noqa: E402

SERVER = "https://work.example.test"


@pytest.fixture
def home(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setenv("ZAZA_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class SharedClient:
    """The ASGI test client, reusable across calls (close is a no-op), recording what was sent."""

    def __init__(self, client) -> None:  # noqa: ANN001
        self.client, self.sent = client, []

    def post(self, url, **kw):  # noqa: ANN001, ANN201
        self.sent.append(kw.get("json"))
        return self.client.post(url, **kw)

    def close(self) -> None:
        pass


def _run(env, code, **kw):  # noqa: ANN001, ANN201
    shared = kw.pop("shared", None) or SharedClient(env.client)
    result = enroll_with_code(
        SERVER, code, client_factory=lambda: shared,
        transport_factory=lambda url, creds: SyncTransport(url, creds, client=env.client), **kw)
    return result, shared


def _mock(handler):  # noqa: ANN001, ANN202
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def _files_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in root.rglob("*") if p.is_file())


def _sync_ok(env, creds):  # noqa: ANN001, ANN202
    return env.client.get(DEVICE_PATH, headers={"Authorization": f"Bearer {creds.token}",
                                                DEVICE_ID_HEADER: creds.device_id})


# ─── success, persistence, secrecy ─────────────────────────────────────────


def test_successful_enrollment_stores_identity_and_credentials_and_they_authenticate(home):
    env = _env()
    _, code = _code(env)
    result, _ = _run(env, code)
    assert result.ok and result.enrollment.employee_id == "employee-1"  # the employee comes from the server
    state, creds_ok = enrollment.current_state()
    assert creds_ok and state.server_url == SERVER and state.device_id == result.enrollment.device_id
    assert state.device_id.startswith("pc-")
    creds = load_credentials()
    assert creds.device_id == state.device_id
    assert _sync_ok(env, creds).status_code == 200  # the stored credential really authenticates


def test_neither_code_nor_token_is_stored_in_plaintext_or_logged(home, caplog, capsys):
    env = _env()
    _, code = _code(env)
    with caplog.at_level(logging.DEBUG):
        result, _ = _run(env, code)
    assert result.ok
    token = load_credentials().token
    assert len(token) > 20
    on_disk = _files_text(home)
    bare = code.replace("ZZE-", "", 1).replace("-", "")
    assert code not in on_disk and bare not in on_disk
    assert token not in paths.config_path().read_text(encoding="utf-8")
    if json.loads((home / "device_credentials.json").read_text()).get("protection") == "dpapi":
        assert token not in on_disk  # encrypted at rest (Windows DPAPI, current user)
    out = capsys.readouterr()
    for secret in (code, bare, token):
        assert secret not in caplog.text and secret not in out.out and secret not in out.err
        assert secret not in result.message


def test_the_client_sends_no_organization_or_tenant_and_the_server_would_refuse_one(home):
    env = _env()
    _, code = _code(env)
    result, shared = _run(env, code)
    assert result.ok
    assert len(shared.sent) == 1
    assert set(shared.sent[0]) == {"enrollment_code", "device_id", "device_name"}
    _, other = _code(env, "b")
    for field in ("organization_id", "tenant", "database", "employee_id"):
        r = env.client.post("/api/v1/enroll", json={"enrollment_code": other, "device_id": "x-1", field: "evil"})
        assert r.status_code == 422  # unknown fields are refused and the code is untouched
    assert env.store.list_codes()[-1].status == "ISSUED"


# ─── refusals ──────────────────────────────────────────────────────────────


def test_invalid_expired_and_used_codes_are_refused_and_nothing_is_saved(home):
    env = _env()
    bad, _ = _run(env, "ZZE-AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-GGGG-HHHH")
    assert not bad.ok and "Invalid or expired enrollment code" in bad.message
    assert enrollment.current_state() == (None, False)

    _, expired = _code(env)
    env.store.now += timedelta(hours=73)
    result, _ = _run(env, expired)
    assert not result.ok and "Invalid or expired" in result.message
    env.store.now -= timedelta(hours=73)
    assert enrollment.current_state() == (None, False)

    _, once = _code(env)
    first, _ = _run(env, once)
    assert first.ok
    before = load_credentials()
    again, _ = _run(env, once, replace_existing=True)  # explicit replacement, but the code is used up
    assert not again.ok and "Invalid or expired" in again.message
    assert load_credentials().token == before.token and load_credentials().device_id == before.device_id


def test_an_enrolled_device_is_not_replaced_without_an_explicit_request(home):
    env = _env()
    _, code1 = _code(env)
    assert _run(env, code1)[0].ok
    identity = enrollment.load_enrollment()
    old_token = load_credentials().token
    _, code2 = _code(env, allow_existing_device=True)
    sent = SharedClient(env.client)
    with pytest.raises(EnrollmentError, match="already enrolled"):
        _run(env, code2, shared=sent)
    assert sent.sent == []  # not even a request: the second code is untouched
    assert enrollment.load_enrollment() == identity
    replaced, shared = _run(env, code2, replace_existing=True)
    assert replaced.ok
    # the physical device keeps ONE identity: the same device id is sent, a new credential is stored
    assert shared.sent[0]["device_id"] == identity.device_id
    assert enrollment.load_enrollment().device_id == identity.device_id
    assert len(env.t["a"]["repo"].list_devices()) == 1
    new_token = load_credentials().token
    assert new_token != old_token
    # the check the agent ran with the new token was its first use: the old credential is superseded
    assert _sync_ok(env, load_credentials()).status_code == 200
    assert env.client.get(DEVICE_PATH, headers={"Authorization": f"Bearer {old_token}",
                                                DEVICE_ID_HEADER: identity.device_id}).status_code in (401, 403)


def test_replacing_with_a_plain_code_or_a_failed_attempt_keeps_the_working_identity(home):
    env = _env()
    _, first = _code(env)
    assert _run(env, first)[0].ok
    identity, creds = enrollment.load_enrollment(), load_credentials()
    _, plain = _code(env)  # an ordinary code cannot replace an enrolled device
    refused, _ = _run(env, plain, replace_existing=True)
    assert not refused.ok and "re-enrollment code" in refused.message
    bad, _ = _run(env, "ZZE-AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-GGGG-HHHH", replace_existing=True)
    assert not bad.ok
    assert enrollment.load_enrollment() == identity and load_credentials().token == creds.token
    assert _sync_ok(env, load_credentials()).status_code == 200  # the old credential still works
    assert len(env.t["a"]["repo"].list_devices()) == 1


def test_malformed_input_is_rejected_before_any_request(home):
    env = _env()
    sent = SharedClient(env.client)
    for bad_code in ("", "   ", "x" * 80, "ZZE-éé", "AB\tCD"):
        with pytest.raises(EnrollmentError):
            enroll_with_code(SERVER, bad_code, client_factory=lambda: sent)
    for bad_url in ("", "work.example.test", "http://work.example.test", "https://user:pw@x.test", "ftp://x.test"):
        with pytest.raises(EnrollmentError):
            enroll_with_code(bad_url, "ZZE-AAAA", client_factory=lambda: sent)
    assert sent.sent == []


@pytest.mark.parametrize(("status", "headers", "expected"), [
    (401, {}, "Invalid or expired enrollment code"),
    (429, {"retry-after": "120"}, "Wait about 2 minutes"),
    (429, {}, "Too many attempts"),
    (403, {}, "could not be completed"),
    (422, {}, "could not be completed"),
    (500, {}, "had a problem"),
    (503, {}, "had a problem"),
])
def test_server_answers_become_plain_messages(home, status, headers, expected):
    def handler(request):  # noqa: ANN001, ANN202
        return httpx.Response(status, headers=headers, json={"error": "internal detail zaza_t_secret traceback"})

    result = enroll_with_code(SERVER, "ZZE-AAAA-BBBB", client_factory=_mock(handler))
    assert not result.ok and expected in result.message
    assert "zaza_t_" not in result.message and "traceback" not in result.message.lower()
    assert enrollment.current_state() == (None, False)


@pytest.mark.parametrize(("exc", "expected"), [
    (httpx.ConnectError("dns failure for work.example.test"), "Unable to reach the ZaZa Work server"),
    (httpx.ReadTimeout("slow"), "did not answer in time"),
    (httpx.ConnectTimeout("slow"), "did not answer in time"),
])
def test_network_problems_become_plain_messages(home, exc, expected):
    def handler(request):  # noqa: ANN001, ANN202
        raise exc

    result = enroll_with_code(SERVER, "ZZE-AAAA-BBBB", client_factory=_mock(handler))
    assert not result.ok and expected in result.message and "dns failure" not in result.message
    assert enrollment.current_state() == (None, False)


def test_a_garbled_success_answer_is_not_trusted(home):
    for body in ({}, {"device_token": "", "device_id": "x"}, {"device_token": "tok", "device_id": "other"}):
        def handler(request, b=body):  # noqa: ANN001, ANN202
            return httpx.Response(200, json=b)

        result = enroll_with_code(SERVER, "ZZE-AAAA-BBBB", client_factory=_mock(handler))
        assert not result.ok and "not understood" in result.message
    assert enrollment.current_state() == (None, False)


def test_a_device_id_collision_is_retried_with_a_new_id(home):
    ids = iter(["pc-taken", "pc-free"])
    seen = []

    def handler(request):  # noqa: ANN001, ANN202
        device = json.loads(request.content)["device_id"]
        seen.append(device)
        if device == "pc-taken":
            return httpx.Response(409, json={"error": "exists"})
        return httpx.Response(200, json={"device_id": device, "employee_id": "e1", "device_token": "tok-" + "x" * 30,
                                         "api_version": "v1"})

    class Ok:
        def check_device(self):  # noqa: ANN201
            return type("I", (), {"device_id": "pc-free", "employee_id": "e1"})()

        def close(self) -> None:
            pass

    result = enroll_with_code(SERVER, "ZZE-AAAA-BBBB", client_factory=_mock(handler),
                              device_id_factory=lambda: next(ids), transport_factory=lambda url, creds: Ok())
    assert result.ok and seen == ["pc-taken", "pc-free"]


def test_no_redirect_is_followed_and_tls_is_never_disabled():
    src = inspect.getsource(enrollment._default_client)
    assert "follow_redirects=False" in src and "verify" not in src


# ─── startup and syncing ───────────────────────────────────────────────────


def test_the_enrollment_window_is_shown_only_when_not_enrolled(home, monkeypatch):
    import deskmate.zaza.ui as ui
    import deskmate.zaza.workagent as wa

    calls = []
    monkeypatch.setattr(ui, "show_enrollment", lambda *a, **k: calls.append("enroll") or True)
    monkeypatch.setattr(ui, "show_status", lambda *a, **k: 0)
    monkeypatch.setattr(wa, "start_background", lambda: calls.append("start"))
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)
    assert wa.first_run() == 0 and calls == ["enroll", "start"]  # not enrolled: ask, then start
    env = _env()
    _, code = _code(env)
    assert _run(env, code)[0].ok
    calls.clear()
    assert wa.first_run() == 0 and calls == ["start"]  # enrolled: straight to the agent, no window


def test_the_running_agent_starts_recording_and_syncing_after_enrollment(home, monkeypatch):
    import deskmate.zaza.runner as runner_mod

    monkeypatch.setattr(runner_mod, "ENROLLMENT_POLL", 0.1)
    monkeypatch.setattr(runner_mod, "STATUS_INTERVAL", 0.1)
    FakeAgent.instances = []
    workers = []

    class FakeWorker:
        def __init__(self, config) -> None:  # noqa: ANN001
            self.config, self.started = config, False
            self.transport = type("T", (), {"close": lambda self: None})()
            self.store = type("S", (), {"close": lambda self: None})()

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            pass

        def run_once(self) -> None:
            pass

    def make_worker(config):  # noqa: ANN001, ANN202
        workers.append(FakeWorker(config))
        return workers[-1], ""

    runner = Runner(poll=0.02, agent_factory=FakeAgent, worker_factory=make_worker)
    thread, _ = _runner_thread(runner)
    try:
        _wait(lambda: paths.status_path().exists())
        assert json.loads(paths.status_path().read_text())["state"] == "WAITING_FOR_ENROLLMENT"
        env = _env()
        _, code = _code(env)
        assert _run(env, code)[0].ok  # the employee enrolls; no restart, no manual token handling
        _wait(lambda: FakeAgent.instances and FakeAgent.instances[0].started and workers and workers[0].started)
        assert workers[0].config.sync_url == SERVER
        assert FakeAgent.instances[0].config.device_id == enrollment.load_enrollment().device_id
    finally:
        time.sleep(1.1)
        control.request_stop(paths.stop_request_path())
        thread.join(10)


# ─── operator side: the plaintext code must not reach persistent logs ──────


def test_enrollment_code_create_refuses_a_non_interactive_output_before_creating_a_code():
    import deskmate.zaza_server.__main__ as cli

    source = inspect.getsource(cli._saas_operator)
    branch = source[source.index('args.command == "enrollment-code-create"'):]
    guard, create = branch.index("_stdout_is_terminal()"), branch.index("store.create_code(")
    assert guard < create  # refused BEFORE a code is generated, so none is left behind
    assert "TenancyError" in branch[guard:create]
    after = branch[create:branch.index('elif args.command == "enrollment-code-list"')]
    assert "ENROLLMENT CODE" in after


def test_terminal_detection(monkeypatch):
    import deskmate.zaza_server.__main__ as cli

    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert cli._stdout_is_terminal() is False  # a job log, pipe or file

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdout", Tty())
    assert cli._stdout_is_terminal() is True
