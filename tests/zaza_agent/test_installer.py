"""Phase 9 — the installed ZaZa Work Agent (packaging, enrollment, secrets,
autostart, single instance, upgrade/uninstall data rules, privacy audit).

Runs in the minimal build environment too (installer/build.ps1): it imports
only the agent and the installer tooling — no server, no capture library.
The real Windows install/upgrade/uninstall is installer/smoke-test.ps1.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "installer"))

import audit_package  # noqa: E402
import build_config  # noqa: E402

from deskmate.zaza import __version__, autostart, control, paths, workagent  # noqa: E402
from deskmate.zaza.app_logging import PrivacyLogFilter, configure  # noqa: E402
from deskmate.zaza.enrollment import (  # noqa: E402
    Enrollment,
    EnrollmentError,
    current_state,
    enroll,
    load_enrollment,
    save_enrollment,
    validate_server_url,
)
from deskmate.zaza.instance import InstanceLock, is_running  # noqa: E402
from deskmate.zaza.runner import Runner  # noqa: E402
from deskmate.zaza.status import AgentStatus, effective_status, write_status  # noqa: E402
from deskmate.zaza.storage import ActivityStore  # noqa: E402
from deskmate.zaza.sync.credentials import load_credentials  # noqa: E402
from deskmate.zaza.sync.protocol import DeviceInfoResponse  # noqa: E402
from deskmate.zaza.sync.transport import AuthError, NetworkError, SyncTransport  # noqa: E402

TOKEN = "zzd_TEST-secret-token-value-1234567890"
UTC = timezone.utc


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAZA_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class FakeTransport:
    def __init__(self, device_id="pc-1", employee_id="emp-1", error=None):  # noqa: ANN001
        self.device_id, self.employee_id, self.error, self.closed = device_id, employee_id, error, False

    def check_device(self):  # noqa: ANN201
        if self.error:
            raise self.error
        return DeviceInfoResponse(device_id=self.device_id, employee_id=self.employee_id, status="ACTIVE")

    def close(self) -> None:
        self.closed = True


def factory(transport: FakeTransport):  # noqa: ANN201
    return lambda url, creds: transport


def _enroll(token: str = TOKEN, device: str = "pc-1", **kw):  # noqa: ANN003, ANN201
    return enroll("https://zaza.example.com", device, token, transport_factory=factory(FakeTransport(device)), **kw)


# ─── packaging / privacy ───────────────────────────────────────────────────


def test_entry_point_imports_only_the_agent():
    code = ("import sys, json, deskmate.zaza.workagent; "
            "print(json.dumps(sorted(m for m in sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT)
    loaded = json.loads(out.stdout)
    for mod in loaded:
        assert not build_config.is_under(mod, build_config.DESKMATE_PROHIBITED), mod
        assert not build_config.is_under(mod, build_config.THIRD_PARTY_PROHIBITED), mod
    assert "deskmate.zaza.workagent" in loaded and "deskmate.zaza.agent" not in loaded  # UI/agent load lazily


def test_entry_script_and_spec_start_from_the_zaza_agent():
    assert "from deskmate.zaza.workagent import main" in (ROOT / "installer/entry.py").read_text()
    spec = (ROOT / "installer/ZaZaWorkAgent.spec").read_text()
    assert 'name="ZaZaWorkAgent"' in spec and "excludes=EXCLUDES" in spec
    assert 'console=(FLAVOR == "development")' in spec  # production: no console window


def _all_agent_modules() -> list[str]:
    mods = ["deskmate", "deskmate.zaza"]
    for path in (ROOT / "deskmate/zaza").rglob("*.py"):
        rel = path.relative_to(ROOT).with_suffix("")
        name = ".".join(rel.parts)
        if not name.endswith("__init__") and name != "deskmate.zaza.__main__":
            mods.append(name)
    return sorted(mods)


def test_shipped_agent_source_has_no_prohibited_capture_calls():
    result = audit_package.AuditResult()
    audit_package.check_sources(_all_agent_modules(), result)
    assert result.failures == []


@pytest.mark.parametrize(("module", "reason"), [
    ("deskmate.screen.capture", "prohibited upstream"), ("deskmate.a11y.clipboard", "prohibited upstream"),
    ("deskmate.audio.capture", "prohibited upstream"), ("deskmate.zaza_server.app", "prohibited upstream"),
    ("deskmate.zaza.__main__", "prohibited upstream"), ("mss", "prohibited library"), ("PIL.ImageGrab",
                                                                                         "prohibited library"),
    ("pytesseract", "prohibited library"), ("sounddevice", "prohibited library"), ("uiautomation", "prohibited library"),
    ("requests", "not allowlisted"),
])
def test_audit_rejects_prohibited_modules(module, reason):
    result = audit_package.AuditResult()
    audit_package.check_modules(["deskmate.zaza.workagent", module], result)
    assert any(reason in f for f in result.failures), result.failures


@pytest.mark.parametrize("snippet", [
    "user32.GetClipboardData(1)", "gdi32.BitBlt(a, b)", "import mss", "user32.ToUnicode(vk)",
    "kb.vkCode", "winmm.waveInOpen()", "httpx.Client(verify=False)", "GetKeyNameTextW(x)",
    "IUIAutomationElement", "cv2.VideoCapture(0)",
])
def test_source_patterns_catch_capture_code(snippet):
    assert any(re.search(p, snippet) for p in build_config.PROHIBITED_SOURCE_PATTERNS), snippet


def test_audit_accepts_the_allowed_set_and_documents_exceptions():
    result = audit_package.AuditResult()
    audit_package.check_modules(["deskmate", "deskmate.zaza", "deskmate.zaza.workagent", "httpx", "pydantic",
                                 "psutil", "tkinter", "ctypes", "sqlite3", "certifi"], result)
    assert result.failures == []
    assert any(n.startswith("allowed exception: tkinter") for n in result.notes)


def test_key_identities_and_full_urls_are_never_read():
    hooks = (ROOT / "deskmate/zaza/input_hooks.py").read_text(encoding="utf-8")
    assert not re.search(r"\.vkCode\b|\.scanCode\b|\.pt\b", hooks)
    privacy = (ROOT / "deskmate/zaza/privacy.py").read_text(encoding="utf-8").lower()
    assert "domain" in privacy and "url_path" not in privacy


def test_no_option_can_enable_capture_or_pass_a_token():
    help_text = subprocess.run([sys.executable, "-c", "from deskmate.zaza.workagent import main; main(['--help'])"],
                               capture_output=True, text=True, cwd=ROOT).stdout.lower()
    for word in ("token", "screenshot", "clipboard", "ocr", "audio", "insecure", "verify", "keylog"):
        assert f"--{word}" not in help_text and f"-{word}" not in help_text.replace("--enroll-stdin", "")
    with pytest.raises(SystemExit):
        workagent.main(["--token", TOKEN])


# ─── enrollment, secrets ───────────────────────────────────────────────────


def test_enrollment_verifies_then_stores_secret_and_config_separately(home):
    result = _enroll()
    assert result.ok and result.message == "Connection successful. This device is enrolled."
    enrollment = load_enrollment()
    assert (enrollment.server_url, enrollment.device_id, enrollment.employee_id) == (
        "https://zaza.example.com", "pc-1", "emp-1")
    assert load_credentials().token == TOKEN  # secure store round trip
    config_text = paths.config_path().read_text()
    assert TOKEN not in config_text and "token" not in config_text
    creds_raw = json.loads((home / "device_credentials.json").read_text())
    if os.name == "nt":
        assert creds_raw["protection"] == "dpapi" and TOKEN not in json.dumps(creds_raw)
    assert current_state() == (enrollment, True)


def test_token_is_not_in_the_database_or_any_other_file(home):
    _enroll()
    store = ActivityStore(str(paths.db_path()))
    store.create_session(session_id="s1", device_id="pc-1", employee_id="emp-1", started_at=time.time(),
                         start_reason="AGENT_START")
    store.close()
    for path in home.rglob("*"):
        if path.is_file() and path.name != "device_credentials.json":
            assert TOKEN.encode() not in path.read_bytes(), path


def test_nothing_is_saved_when_the_server_refuses(home):
    for error, text in ((AuthError(401, "x"), "Device token was not accepted"),
                        (NetworkError("x"), "Could not reach the server")):
        result = enroll("https://zaza.example.com", "pc-1", TOKEN,
                        transport_factory=factory(FakeTransport(error=error)))
        assert not result.ok and text in result.message and TOKEN not in result.message
    assert load_enrollment() is None and load_credentials() is None
    result = enroll("https://zaza.example.com", "pc-1", TOKEN, transport_factory=factory(FakeTransport("other")))
    assert not result.ok and "different device" in result.message


def test_reenrollment_replaces_credentials_and_device_change_needs_confirmation(home):
    _enroll()
    _enroll(token="zzd_second-token-abcdefghijkl")
    assert load_credentials().token == "zzd_second-token-abcdefghijkl"
    store = ActivityStore(str(paths.db_path()))
    store.create_session(session_id="s1", device_id="pc-1", employee_id="emp-1", started_at=time.time(),
                         start_reason="AGENT_START")
    store.close()
    with pytest.raises(EnrollmentError, match="1 unsynced record"):
        _enroll(device="pc-2")
    assert load_enrollment().device_id == "pc-1"
    assert _enroll(device="pc-2", allow_device_change=True).ok
    assert load_enrollment().device_id == "pc-2"


def test_enroll_from_stdin_never_uses_the_command_line(monkeypatch, capsys):
    import io

    import deskmate.zaza.enrollment as enrollment_mod

    monkeypatch.setattr(enrollment_mod, "_default_transport", lambda url, creds: FakeTransport())
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"server_url": "https://zaza.example.com", "device_id": "pc-1", "token": TOKEN})))
    monkeypatch.setattr(enrollment_mod, "enroll", lambda *a, **k: enroll(
        *a, transport_factory=factory(FakeTransport()), **k))
    assert workagent.main(["--enroll-stdin"]) == 0
    assert TOKEN not in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert workagent.main(["--enroll-stdin"]) == 2


def test_logs_never_contain_tokens_or_window_titles(home):
    path = configure(home / "logs")
    log = logging.getLogger("zaza.agent")
    log.info("APP_CHANGE app=%s title=%s", "Code.exe", "secret-project.docx - Word")
    log.info("ACTIVITY status=%s keyboard=%s mouse=%s", "ACTIVE", True, False)
    log.warning("request failed: Authorization: Bearer %s", TOKEN)
    log.info("work session %s started", "s1")
    for handler in logging.getLogger("zaza").handlers:
        handler.flush()
    text = path.read_text(encoding="utf-8")
    assert "secret-project" not in text and "Code.exe" not in text and "ACTIVITY" not in text
    assert TOKEN not in text and "Bearer ***" in text and "work session s1 started" in text
    handler = logging.getLogger("zaza").handlers[0]
    assert handler.maxBytes == 1_000_000 and handler.backupCount == 5  # bounded logs
    assert isinstance(handler.filters[0], PrivacyLogFilter)


# ─── server URL / TLS ──────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["https://zaza.example.com", "https://zaza.example.com:8443/", "http://127.0.0.1:8765",
                                 "http://localhost:8765", "http://[::1]:8765"])
def test_accepted_server_urls(url):
    assert validate_server_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", ["http://zaza.example.com", "http://192.168.1.10:8765", "ftp://x", "zaza.example.com",
                                 "https://user:pw@zaza.example.com", "https://zaza.example.com/?insecure=1", ""])
def test_rejected_server_urls(url):
    with pytest.raises(EnrollmentError):
        validate_server_url(url)


def test_tls_verification_cannot_be_disabled():
    config = Enrollment("https://zaza.example.com", "pc-1", "emp-1").agent_config()
    assert config.sync_allow_insecure_http is False
    from deskmate.zaza.sync.credentials import DeviceCredentials

    transport = SyncTransport("https://zaza.example.com", DeviceCredentials("pc-1", TOKEN))
    ctx = transport._client._transport._pool._ssl_context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    transport.close()
    import inspect

    assert "verify" not in inspect.signature(SyncTransport).parameters


# ─── autostart ─────────────────────────────────────────────────────────────


def test_autostart_task_is_transparent_least_privilege_and_bounded():
    exe = Path(r"C:\Program Files\ZaZa Work Agent\ZaZaWorkAgent.exe")
    xml = autostart.task_xml(exe)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))
    assert root.find("t:Triggers/t:LogonTrigger", ns) is not None
    principal = root.find("t:Principals/t:Principal", ns)
    assert principal.find("t:GroupId", ns).text == "S-1-5-32-545"  # every user, in their own session
    assert principal.find("t:RunLevel", ns).text == "LeastPrivilege"
    assert principal.find("t:UserId", ns) is None and principal.find("t:LogonType", ns) is None  # no stored password
    settings = root.find("t:Settings", ns)
    assert settings.find("t:Hidden", ns).text == "false"
    assert settings.find("t:MultipleInstancesPolicy", ns).text == "IgnoreNew"
    assert settings.find("t:RestartOnFailure/t:Count", ns).text == "3"
    assert settings.find("t:RestartOnFailure/t:Interval", ns).text == "PT5M"
    action = root.find("t:Actions/t:Exec", ns)
    assert action.find("t:Command", ns).text == f'"{exe}"' and action.find("t:Arguments", ns).text == "--background"


def test_registering_twice_replaces_the_same_task(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(autostart, "_schtasks", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0))
    for _ in range(2):
        autostart.register(Path("C:/x/ZaZaWorkAgent.exe"), tmp_path)
    assert calls[0] == calls[1] and calls[0][:3] == ("/Create", "/TN", r"ZaZa\ZaZa Work Agent") and "/F" in calls[0]
    assert not list(tmp_path.glob("*.xml"))  # temporary XML removed


def test_uninstall_cleanup_stops_agents_removes_task_and_keeps_data(monkeypatch, home):
    _enroll()
    done = []
    monkeypatch.setattr(control, "stop_all", lambda d: done.append("stop") or (1, 0))
    monkeypatch.setattr(autostart, "unregister", lambda: done.append("unregister") or True)
    assert workagent.main(["--uninstall-cleanup"]) == 0
    assert done == ["stop", "unregister"]
    assert paths.config_path().exists() and (home / "device_credentials.json").exists()  # data preserved


# ─── single instance, stop requests ────────────────────────────────────────


def test_single_instance_lock(home):
    first, second = InstanceLock(paths.lock_path()), InstanceLock(paths.lock_path())
    assert first.acquire() and not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_lock_held_by_another_process_and_released_when_it_dies(home):
    code = ("import time, sys; from deskmate.zaza.instance import InstanceLock; from pathlib import Path; "
            "l = InstanceLock(Path(sys.argv[1])); assert l.acquire(); print('locked', flush=True); time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(paths.lock_path())], stdout=subprocess.PIPE, text=True,
                            cwd=ROOT)
    try:
        assert proc.stdout.readline().strip() == "locked"
        assert is_running(paths.lock_path())
        assert Runner(poll=0.01).run() == 0  # a second agent exits immediately
    finally:
        import psutil  # the venv python.exe on Windows is a launcher: kill the real interpreter too

        for child in psutil.Process(proc.pid).children(recursive=True):
            child.kill()
        proc.kill()
        proc.wait()
    _wait(lambda: not is_running(paths.lock_path()), 10)  # a killed agent never leaves a stale lock


def test_stop_requests_older_than_the_agent_are_ignored(tmp_path):
    marker = tmp_path / "stop.request"
    control.request_stop(marker)
    assert control.requested(marker, since=time.time() - 5)
    old = time.time() - 3600
    os.utime(marker, (old, old))
    assert not control.requested(marker, since=time.time())


# ─── runner (fake recorder; the real agent is covered by the Phase 1-3 tests) ─


class FakeAgent:
    instances: list = []

    def __init__(self, config) -> None:  # noqa: ANN001
        from deskmate.zaza.health import HealthRegistry

        self.config, self.started, self.stopped = config, False, False
        self.health = HealthRegistry()
        self.store = ActivityStore(config.db_path)
        FakeAgent.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _runner_thread(runner: Runner):  # noqa: ANN202
    import threading

    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", runner.run()), daemon=True)
    thread.start()
    return thread, result


def _wait(cond, timeout: float = 15.0) -> None:  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def test_runner_waits_for_enrollment_records_and_stops_cleanly(monkeypatch, home):
    import deskmate.zaza.runner as runner_mod

    monkeypatch.setattr(runner_mod, "ENROLLMENT_POLL", 0.1)
    monkeypatch.setattr(runner_mod, "STATUS_INTERVAL", 0.1)
    FakeAgent.instances = []
    runner = Runner(poll=0.02, agent_factory=FakeAgent, worker_factory=lambda c: (None, "offline test"))
    thread, result = _runner_thread(runner)
    try:
        _check_runner(thread, result)
    finally:
        control.request_stop(paths.stop_request_path())  # never leak a running thread into other tests
        thread.join(10)


def _check_runner(thread, result) -> None:  # noqa: ANN001
    _wait(lambda: paths.status_path().exists())
    assert json.loads(paths.status_path().read_text())["state"] == "WAITING_FOR_ENROLLMENT"
    assert FakeAgent.instances == []  # nothing recorded before enrollment
    _enroll()
    _wait(lambda: FakeAgent.instances and FakeAgent.instances[0].started)
    assert FakeAgent.instances[0].config.device_id == "pc-1"
    assert FakeAgent.instances[0].config.sync_allow_insecure_http is False
    _enroll(token="zzd_rotated-token-abcdefghijk")  # re-enrollment: restart with the new credentials
    _wait(lambda: len(FakeAgent.instances) == 2)
    assert FakeAgent.instances[0].stopped
    time.sleep(1.1)  # stop request must be newer than the agent start (mtime resolution)
    control.request_stop(paths.stop_request_path())
    thread.join(10)
    assert result["code"] == 0 and FakeAgent.instances[1].stopped
    assert json.loads(paths.status_path().read_text())["state"] == "STOPPED"
    assert not is_running(paths.lock_path())


def test_status_window_reports_stopped_unless_running_and_fresh(home):
    write_status(AgentStatus(state="RUNNING", device_id="pc-1", sync_state="HEALTHY"))
    assert effective_status().state == "STOPPED"  # no agent holds the lock
    lock = InstanceLock(paths.lock_path())
    assert lock.acquire()
    try:
        assert effective_status().state == "RUNNING"
        later = datetime.now(UTC) + timedelta(minutes=5)
        assert effective_status(later).state == "STOPPED"  # stale status file
    finally:
        lock.release()
    raw = paths.status_path().read_text()
    assert "token" not in raw.lower()


def test_status_rows_never_show_the_token(home):
    from deskmate.zaza.ui import status_lines

    _enroll()
    rows = dict(status_lines())
    assert rows["Device ID"] == "pc-1" and rows["Server"] == "zaza.example.com"
    assert TOKEN not in json.dumps(rows) and rows["Version"] == __version__ == "0.9.0"
    for label in ("Keyboard activity presence", "Mouse activity presence", "Foreground-app monitoring",
                  "Windows lock monitoring", "Local storage", "Last successful sync", "Records waiting to upload"):
        assert label in rows


# ─── upgrade / uninstall data rules ────────────────────────────────────────


def test_legacy_data_is_migrated_without_losing_unsynced_records(tmp_path):
    legacy, new = tmp_path / ".zaza_agent", tmp_path / "ZaZa" / "WorkAgent"
    legacy.mkdir()
    store = ActivityStore(str(legacy / "activity.db"))
    store.create_session(session_id="s1", device_id="pc-1", employee_id="emp-1", started_at=time.time(),
                         start_reason="AGENT_START")
    before = store.sync_counts()
    store.close()
    (legacy / "device_credentials.json").write_text("{}")
    moved = paths.migrate_legacy(legacy, new)
    assert "activity.db" in moved and "device_credentials.json" in moved
    store = ActivityStore(str(new / "activity.db"))
    assert store.sync_counts() == before and len(store.sessions()) == 1
    store.close()
    assert paths.migrate_legacy(legacy, new) == []  # never twice, never overwrites


def test_new_data_location_is_outside_program_files(monkeypatch):
    monkeypatch.delenv("ZAZA_HOME")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\emp\AppData\Local")
    if os.name == "nt":
        assert paths.root() == Path(r"C:\Users\emp\AppData\Local\ZaZa\WorkAgent")
    assert "Program Files" not in str(paths.root())


def test_remove_local_data_warns_with_unsynced_count_and_respects_no(home):
    _enroll()
    store = ActivityStore(str(paths.db_path()))
    store.create_session(session_id="s1", device_id="pc-1", employee_id="emp-1", started_at=time.time(),
                         start_reason="AGENT_START")
    store.close()
    asked = []
    assert workagent.remove_local_data(ask=lambda n: asked.append(n) or False) == 1
    assert asked == [1] and paths.db_path().exists() and load_credentials() is not None
    assert workagent.remove_local_data(ask=lambda n: True) == 0
    assert not home.exists() and load_credentials() is None and load_enrollment() is None


def test_preserved_enrollment_is_reused_after_reinstall(home):
    _enroll()
    save_enrollment(load_enrollment())  # what a reinstall finds on disk
    enrollment, ok = current_state()
    assert ok and enrollment.device_id == "pc-1"


# ─── installer script & build script sanity ────────────────────────────────


def test_installer_script_branding_and_rules():
    iss = (ROOT / "installer/ZaZaWorkAgent.iss").read_text(encoding="utf-8")
    assert "deskmate" not in iss.lower()
    assert "AppName=ZaZa Work Agent" in iss and "OutputBaseFilename=ZaZaWorkAgentSetup" in iss
    assert "AppPublisher=ZaZa" in iss and re.search(r"AppId=\{\{[0-9A-F-]{36}\}", iss)
    assert "PrivilegesRequired=admin" in iss and "{autopf}\\ZaZa Work Agent" in iss
    assert "runasoriginaluser" in iss and "--register-autostart" in iss and "--uninstall-cleanup" in iss
    assert "/TOKEN" not in iss.upper().replace("DEVICE TOKEN", "") and "token=" not in iss.lower()
    assert "ZaZa Work Agent — Status & Privacy" in iss
    for never in ("screenshots", "clipboard", "microphone or webcam", "full web addresses"):
        assert never in iss.lower()


def test_build_script_steps():
    ps1 = (ROOT / "installer/build.ps1").read_text(encoding="utf-8")
    for needle in ("audit_package.py", "PyInstaller", "ISCC", "Get-FileHash", "SHA256SUMS.txt", "Windows_NT",
                   "requirements-build.txt", "UNSIGNED"):
        assert needle in ps1
    reqs = (ROOT / "installer/requirements-build.txt").read_text()
    pins = [line for line in reqs.splitlines() if line and not line.startswith("#")]
    assert all("==" in line for line in pins)
    assert not any(line.split("==")[0].lower() in ("mss", "pillow", "pytesseract", "fastapi", "uiautomation")
                   for line in pins)
    assert build_config.product_version() == __version__
