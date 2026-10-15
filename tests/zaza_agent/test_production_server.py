"""Phase 10 production hardening: trusted reverse proxy, HTTPS scheme,
Host validation, manager login rate limiting, bounded/redacted logging,
the --env-file loader and `check-config`."""

from __future__ import annotations

import asyncio
import logging
import re

import pytest

pytest.importorskip("jinja2")
pytest.importorskip("argon2")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from deskmate.zaza_server.config import ConfigError, read_env_file  # noqa: E402
from deskmate.zaza_server.dashboard.config import DashboardSettings, dashboard_settings_from_env  # noqa: E402
from deskmate.zaza_server.dashboard.ratelimit import LoginLimits, LoginRateLimiter  # noqa: E402
from deskmate.zaza_server.dashboard.routes import mount_dashboard  # noqa: E402
from deskmate.zaza_server.dashboard.security import COOKIE_NAME  # noqa: E402
from deskmate.zaza_server.http_security import (  # noqa: E402
    HostCheckMiddleware,
    HttpSettings,
    TrustedProxyMiddleware,
    http_settings_from_env,
    install,
)

from .test_dashboard import PASSWORD, standard  # noqa: E402

PUBLIC = "zaza.example.test"


@pytest.fixture(autouse=True)
def _no_leftover_log_handlers():
    """`serve` and configure_logging() add root handlers; remove them after each test."""
    yield
    _remove_zaza_handlers()


# ─── ASGI helpers ──────────────────────────────────────────────────────────


def _scope(peer: str | None, headers: dict[str, str] | None = None, scheme: str = "http") -> dict:
    return {"type": "http", "scheme": scheme, "client": (peer, 50000) if peer else None, "path": "/",
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]}


def _through(middleware_cls, settings: HttpSettings, scope: dict) -> tuple[dict | None, list[dict]]:
    """Run one request through the middleware; return the scope the app saw
    (None if it was not called) and the messages sent."""
    seen: dict = {}
    sent: list[dict] = []

    async def app(s, receive, send):  # noqa: ANN001, ANN202
        seen.update(s)

    async def receive():  # noqa: ANN202
        return {"type": "http.request", "body": b""}

    async def send(message):  # noqa: ANN001, ANN202
        sent.append(message)

    asyncio.run(middleware_cls(app, settings)(scope, receive, send))
    return (seen or None), sent


def _proxied(peer: str, headers: dict[str, str]) -> dict:
    seen, _ = _through(TrustedProxyMiddleware, HttpSettings(), _scope(peer, headers))
    return seen


# ─── settings ──────────────────────────────────────────────────────────────


def test_http_settings_defaults_and_env():
    assert http_settings_from_env({}) == HttpSettings((), ("127.0.0.1", "::1"))
    s = http_settings_from_env({"ZAZA_PUBLIC_HOST": " Zaza.Example.Test. , other.example.test",
                                "ZAZA_TRUSTED_PROXY_IPS": "127.0.0.1"})
    assert s.public_hosts == ("zaza.example.test", "other.example.test") and s.trusted_proxies == ("127.0.0.1",)
    assert s.host_check and not HttpSettings().host_check


@pytest.mark.parametrize("host", ["https://zaza.example.test", "zaza.example.test:443", "*.example.test",
                                  "zaza.example.test/manager", "bad host", "-zaza.example.test"])
def test_public_host_must_be_a_plain_host_name(host):
    with pytest.raises(ConfigError, match="ZAZA_PUBLIC_HOST"):
        http_settings_from_env({"ZAZA_PUBLIC_HOST": host})


@pytest.mark.parametrize("proxy", ["0.0.0.0", "10.0.0.5", "192.168.1.1", "203.0.113.7", "::", "localhost", "caddy"])
def test_only_loopback_proxies_can_be_trusted(proxy):
    with pytest.raises(ConfigError, match="ZAZA_TRUSTED_PROXY_IPS"):
        http_settings_from_env({"ZAZA_TRUSTED_PROXY_IPS": proxy})


# ─── trusted proxy ─────────────────────────────────────────────────────────


def test_forwarded_headers_from_the_local_proxy_are_used():
    seen = _proxied("127.0.0.1", {"X-Forwarded-For": "198.51.100.23", "X-Forwarded-Proto": "https"})
    assert seen["client"] == ("198.51.100.23", 0) and seen["scheme"] == "https"
    seen = _proxied("::1", {"X-Forwarded-For": "2001:db8::7", "X-Forwarded-Proto": "https"})
    assert seen["client"] == ("2001:db8::7", 0) and seen["scheme"] == "https"


@pytest.mark.parametrize("peer", ["203.0.113.50", "10.0.0.9", "192.168.1.20", "testclient"])
def test_forwarded_headers_from_anyone_else_are_ignored(peer):
    seen = _proxied(peer, {"X-Forwarded-For": "198.51.100.23", "X-Forwarded-Proto": "https"})
    assert seen["client"] == (peer, 50000) and seen["scheme"] == "http"


def test_the_rightmost_untrusted_address_is_the_client():
    # a client-supplied XFF value followed by the address the proxy saw
    seen = _proxied("127.0.0.1", {"X-Forwarded-For": "1.2.3.4, 198.51.100.23"})
    assert seen["client"] == ("198.51.100.23", 0)
    seen = _proxied("127.0.0.1", {"X-Forwarded-For": "198.51.100.23, 127.0.0.1"})
    assert seen["client"] == ("198.51.100.23", 0)


def test_unusable_forwarded_values_keep_the_peer():
    for xff in ("garbage", "198.51.100.23, not-an-ip", "", "127.0.0.1"):
        assert _proxied("127.0.0.1", {"X-Forwarded-For": xff})["client"] == ("127.0.0.1", 50000)
    assert _proxied("127.0.0.1", {"X-Forwarded-Proto": "gopher"})["scheme"] == "http"
    assert _proxied("127.0.0.1", {"X-Forwarded-For": "198.51.100.23:4711"})["client"] == ("198.51.100.23", 0)


def test_without_forwarded_headers_nothing_changes():
    seen = _proxied("127.0.0.1", {})
    assert seen["client"] == ("127.0.0.1", 50000) and seen["scheme"] == "http"


# ─── Host check ────────────────────────────────────────────────────────────


def _host(host: str, *, forwarded: bool = False) -> int:
    headers = {"Host": host}
    if forwarded:
        headers["X-Forwarded-For"] = "198.51.100.23"
    seen, sent = _through(HostCheckMiddleware, HttpSettings((PUBLIC,)), _scope("127.0.0.1", headers))
    return 200 if seen else sent[0]["status"]


def test_public_host_is_accepted_with_or_without_port():
    assert _host(PUBLIC, forwarded=True) == 200
    assert _host(f"{PUBLIC}:443", forwarded=True) == 200
    assert _host("ZAZA.Example.Test", forwarded=True) == 200


@pytest.mark.parametrize("host", ["evil.example", "zaza.example.test.evil.example", "", "203.0.113.7"])
def test_unexpected_hosts_are_rejected(host):
    assert _host(host, forwarded=True) == 400
    assert _host(host) == 400


def test_loopback_host_only_for_direct_local_requests():
    for host in ("127.0.0.1:8100", "localhost:8100", "[::1]:8100"):
        assert _host(host) == 200                  # health check on the server itself
        assert _host(host, forwarded=True) == 400  # never through the public proxy


def test_no_host_check_in_local_development():
    app = FastAPI()
    app.get("/ping")(lambda: {"ok": True})
    install(app, HttpSettings())
    assert TestClient(app, base_url="http://anything.example").get("/ping").status_code == 200


# ─── login rate limiter (unit) ─────────────────────────────────────────────


class Tick:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_client_limit_blocks_then_expires():
    clock = Tick()
    rl = LoginRateLimiter(LoginLimits(3, 100, 60), clock)
    for _ in range(3):
        assert rl.retry_after("198.51.100.1", "alice") is None
        rl.failure("198.51.100.1", "alice")
    assert rl.retry_after("198.51.100.1", "bob") == 60   # any username from that client
    assert rl.retry_after("198.51.100.2", "bob") is None  # other clients unaffected
    clock.now += 59
    assert rl.retry_after("198.51.100.1", "bob") == 1
    clock.now += 1
    assert rl.retry_after("198.51.100.1", "bob") is None


def test_username_limit_applies_across_clients_and_to_unknown_names():
    rl = LoginRateLimiter(LoginLimits(100, 3, 60), Tick())
    for i in range(3):
        rl.failure(f"198.51.100.{i}", "No-Such-User ")
    assert rl.retry_after("203.0.113.9", "no-such-user") is not None
    assert rl.retry_after("203.0.113.9", "other") is None


def test_success_clears_the_failures():
    rl = LoginRateLimiter(LoginLimits(3, 3, 60), Tick())
    rl.failure("198.51.100.1", "alice")
    rl.failure("198.51.100.1", "alice")
    rl.success("198.51.100.1", "alice")
    rl.failure("198.51.100.1", "alice")
    rl.failure("198.51.100.1", "alice")
    assert rl.retry_after("198.51.100.1", "alice") is None


def test_acquire_counts_before_the_check_and_refusals_are_not_counted():
    clock = Tick()
    rl = LoginRateLimiter(LoginLimits(3, 100, 60), clock)
    assert [rl.acquire("198.51.100.1", "alice") for _ in range(3)] == [(None, False), (None, False), (None, True)]
    assert rl.acquire("198.51.100.1", "alice") == (60, False)  # refused, not counted
    clock.now += 60
    assert rl.acquire("198.51.100.1", "alice")[0] is None
    rl.success("198.51.100.1", "alice")
    assert rl.retry_after("198.51.100.1", "alice") is None


def test_parallel_attempts_cannot_exceed_the_limit():
    import threading

    rl = LoginRateLimiter(LoginLimits(3, 100, 900))
    allowed: list[bool] = []
    start = threading.Barrier(50)

    def attempt() -> None:
        start.wait()
        allowed.append(rl.acquire("198.51.100.1", "alice")[0] is None)

    threads = [threading.Thread(target=attempt) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert allowed.count(True) == 3


def test_state_is_bounded_and_expires():
    clock = Tick()
    rl = LoginRateLimiter(LoginLimits(3, 3, 60, max_tracked_keys=50), clock)
    for i in range(500):
        rl.failure(f"10.0.{i // 250}.{i % 250}", f"user{i}")
    assert len(rl) == 50
    clock.now += 61
    rl.retry_after("10.9.9.9", "x")
    assert len(rl) == 0


def test_limiter_settings_from_env():
    s = dashboard_settings_from_env({"ZAZA_DASHBOARD_LOGIN_MAX_FAILURES_PER_CLIENT": "5",
                                     "ZAZA_DASHBOARD_LOGIN_MAX_FAILURES_PER_USERNAME": "7",
                                     "ZAZA_DASHBOARD_LOGIN_WINDOW_SECONDS": "120"})
    assert s.login_limits() == LoginLimits(5, 7, 120)
    assert DashboardSettings().login_limits() == LoginLimits(10, 20, 900)
    for env in ({"ZAZA_DASHBOARD_LOGIN_MAX_FAILURES_PER_CLIENT": "1"},
                {"ZAZA_DASHBOARD_LOGIN_WINDOW_SECONDS": "5"}, {"ZAZA_DASHBOARD_LOGIN_MAX_FAILURES_PER_USERNAME": "x"}):
        with pytest.raises(ConfigError):
            dashboard_settings_from_env(env)


# ─── login rate limiting through the real app ──────────────────────────────


def _app(d, *, http: HttpSettings | None = None, limits: LoginLimits = LoginLimits(3, 6, 900), secure=False):  # noqa: ANN001, ANN202
    app = FastAPI()
    mount_dashboard(app, d.repo, DashboardSettings(cookie_secure=secure), d.clock, LoginRateLimiter(limits))
    install(app, http or HttpSettings())
    return app


def _client(app, peer: str = "127.0.0.1", base_url: str = "http://testserver") -> TestClient:  # noqa: ANN001
    return TestClient(app, follow_redirects=False, base_url=base_url, client=(peer, 50000))


def _login(client: TestClient, username: str, password: str, headers: dict | None = None):  # noqa: ANN202
    page = client.get("/manager/login", headers=headers)
    token = re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
    return client.post("/manager/login", data={"username": username, "password": password, "login_token": token},
                       headers=headers)


def test_repeated_failures_get_429_with_retry_after_and_block_even_the_right_password():
    d = standard()
    d.manager()
    client = _client(_app(d))
    for _ in range(3):
        assert _login(client, "manager", "wrong-password-123").status_code == 401
    limited = _login(client, "manager", PASSWORD)  # correct, but refused before checking
    assert limited.status_code == 429
    assert 890 <= int(limited.headers["retry-after"]) <= 900
    assert "Too many sign-in attempts" in limited.text and "15 minutes" in limited.text
    assert COOKIE_NAME not in limited.cookies
    assert not any(r["action"] == "manager.login" for r in d.repo.audit_rows)


def test_rate_limit_does_not_reveal_which_usernames_exist():
    d = standard()
    d.manager()
    real, fake = _client(_app(d), "198.51.100.1"), _client(_app(d), "198.51.100.1")
    real_codes = [_login(real, "manager", "wrong-password-123").status_code for _ in range(4)]
    fake_codes = [_login(fake, "nobody-here", "wrong-password-123").status_code for _ in range(4)]
    assert real_codes == fake_codes == [401, 401, 401, 429]


def test_fake_forwarded_for_from_a_direct_client_cannot_escape_the_limit():
    d = standard()
    d.manager()
    client = _client(_app(d), peer="203.0.113.50")  # not the trusted proxy
    for i in range(3):
        _login(client, "manager", "wrong-password-123", {"X-Forwarded-For": f"198.51.100.{i}"})
    response = _login(client, "manager", "wrong-password-123", {"X-Forwarded-For": "198.51.100.99"})
    assert response.status_code == 429


def test_clients_behind_the_proxy_are_limited_separately_by_real_address():
    d = standard()
    d.manager()
    app = _app(d, limits=LoginLimits(3, 100, 900))
    proxy = _client(app, peer="127.0.0.1")
    for _ in range(3):
        _login(proxy, "manager", "wrong-password-123", {"X-Forwarded-For": "198.51.100.1"})
    assert _login(proxy, "manager", "wrong-password-123", {"X-Forwarded-For": "198.51.100.1"}).status_code == 429
    other = _login(proxy, "manager", PASSWORD, {"X-Forwarded-For": "198.51.100.2"})
    assert other.status_code == 303 and other.headers["location"] == "/manager"


def test_success_resets_and_passwords_are_never_logged(caplog):
    d = standard()
    d.manager()
    client = _client(_app(d))
    secret = "wrong-but-secret-pw-77"
    with caplog.at_level(logging.DEBUG):
        _login(client, "manager", secret)
        _login(client, "manager", secret)
        assert _login(client, "manager", PASSWORD).status_code == 303
        client.cookies.clear()
        for _ in range(3):
            _login(client, "manager", secret)
    assert secret not in caplog.text and PASSWORD not in caplog.text
    assert "reached the rate limit" in caplog.text  # the client address only


# ─── HTTPS through the proxy: same-origin checks and Secure cookies ────────


def test_https_origin_is_accepted_when_the_trusted_proxy_forwards_https():
    """Caddy speaks plain HTTP to the app; X-Forwarded-Proto from the trusted
    proxy makes the request https, so the browser's https Origin matches."""
    d = standard()
    d.manager()
    app = _app(d, http=HttpSettings((PUBLIC,)))
    proxy = _client(app, peer="127.0.0.1", base_url=f"http://{PUBLIC}")
    headers = {"X-Forwarded-For": "198.51.100.5", "X-Forwarded-Proto": "https", "Origin": f"https://{PUBLIC}"}
    response = _login(proxy, "manager", PASSWORD, headers)
    assert response.status_code == 303 and response.headers["location"] == "/manager"
    cookie = [c for c in response.headers.get_list("set-cookie") if c.startswith(COOKIE_NAME)][0]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    proxy.cookies.clear()  # signed out again
    without_proto =_login(proxy, "manager", PASSWORD, {"X-Forwarded-For": "198.51.100.5", "Origin": f"https://{PUBLIC}"})
    assert without_proto.status_code == 403  # http request with an https Origin: not same-origin


def test_secure_cookie_over_https_behind_the_proxy():
    d = standard()
    d.manager()
    app = _app(d, http=HttpSettings((PUBLIC,)), secure=True)
    proxy = _client(app, peer="127.0.0.1", base_url=f"https://{PUBLIC}")
    headers = {"X-Forwarded-For": "198.51.100.5", "X-Forwarded-Proto": "https", "Origin": f"https://{PUBLIC}"}
    response = _login(proxy, "manager", PASSWORD, headers)
    cookie = [c for c in response.headers.get_list("set-cookie") if c.startswith(COOKIE_NAME)][0]
    assert response.status_code == 303 and "Secure" in cookie


def test_https_origin_is_refused_when_the_scheme_was_not_forwarded_by_the_proxy():
    d = standard()
    d.manager()
    app = _app(d, http=HttpSettings((PUBLIC,)))
    direct = _client(app, peer="203.0.113.50", base_url=f"http://{PUBLIC}")  # claims https, not trusted
    headers = {"X-Forwarded-Proto": "https", "Origin": f"https://{PUBLIC}"}
    assert _login(direct, "manager", PASSWORD, headers).status_code == 403


def test_wrong_host_never_reaches_the_dashboard():
    d = standard()
    d.manager()
    app = _app(d, http=HttpSettings((PUBLIC,)))
    proxy = _client(app, peer="127.0.0.1", base_url="http://evil.example")
    response = proxy.get("/manager/login", headers={"X-Forwarded-For": "198.51.100.5"})
    assert response.status_code == 400 and "login_token" not in response.text


# ─── `serve` wiring ────────────────────────────────────────────────────────


def _serve(monkeypatch, env: dict) -> tuple[int, dict]:  # noqa: ANN001
    from deskmate.zaza_server import __main__ as cli
    from deskmate.zaza_server import backends
    from deskmate.zaza_server.repository import InMemoryRepository

    captured: dict = {}
    monkeypatch.setattr(backends, "open_repository", lambda *a, **k: InMemoryRepository())
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(app=app, **kw))
    for key in ("ZAZA_DATABASE_URL", "ZAZA_PUBLIC_HOST", "ZAZA_DASHBOARD_COOKIE_SECURE", "ZAZA_LOG_FILE",
                "ZAZA_TRUSTED_PROXY_IPS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = cli.main(["--backend", "postgres", "serve", "--host", "127.0.0.1", "--port", "8100"])
    return code, captured


def test_serve_production_options(monkeypatch, tmp_path):
    code, captured = _serve(monkeypatch, {"ZAZA_PUBLIC_HOST": PUBLIC, "ZAZA_DASHBOARD_COOKIE_SECURE": "true",
                                          "ZAZA_LOG_FILE": str(tmp_path / "zaza-server.log")})
    assert code == 0
    assert (captured["host"], captured["port"]) == ("127.0.0.1", 8100)
    assert captured["proxy_headers"] is False and captured["log_config"] is None
    assert captured["server_header"] is False and "workers" not in captured
    client = TestClient(captured["app"], follow_redirects=False, client=("127.0.0.1", 1), base_url=f"http://{PUBLIC}")
    assert client.get("/api/v1/sync/health", headers={"X-Forwarded-For": "198.51.100.5"}).status_code == 200
    evil = TestClient(captured["app"], client=("127.0.0.1", 1), base_url="http://evil.example")
    assert evil.get("/api/v1/sync/health", headers={"X-Forwarded-For": "198.51.100.5"}).status_code == 400
    local = TestClient(captured["app"], client=("127.0.0.1", 1), base_url="http://127.0.0.1:8100")
    assert local.get("/api/v1/sync/health").status_code == 200  # local health check


def test_serve_refuses_a_public_host_without_secure_cookies(monkeypatch, capsys):
    code, captured = _serve(monkeypatch, {"ZAZA_PUBLIC_HOST": PUBLIC})
    assert code == 2 and "app" not in captured
    assert "ZAZA_DASHBOARD_COOKIE_SECURE" in capsys.readouterr().err


def test_serve_refuses_a_non_loopback_trusted_proxy(monkeypatch, capsys):
    code, _ = _serve(monkeypatch, {"ZAZA_TRUSTED_PROXY_IPS": "203.0.113.7"})
    assert code == 2 and "loopback" in capsys.readouterr().err


# ─── logging: redaction and bounded files ──────────────────────────────────


@pytest.mark.parametrize(("text", "leak"), [
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ("headers {'authorization': 'Bearer tok_123456'}", "tok_123456"),
    ("Cookie: zaza_manager_session=s3ss10nv4lue; other=1", "s3ss10nv4lue"),
    ("set zaza_manager_session=s3ss10nv4lue", "s3ss10nv4lue"),
    ("connect postgresql://zaza_employee_app:Hunter2Secret@127.0.0.1:5432/db", "Hunter2Secret"),
    ("password=Hunter2Secret host=127.0.0.1", "Hunter2Secret"),
    ('{"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEv...\\n-----END PRIVATE KEY-----\\n"}', "MIIEv"),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----", "MIIEvQIBADANBg"),
    ("token ya29.a0AfH6SMBx-abc refreshed", "ya29.a0AfH6SMBx-abc"),
    ('{"access_token": "ya29.zzz", "refresh_token": "1//0abc"}', "1//0abc"),
    ("device token=Zm9vYmFyYmF6cXV4 issued", "Zm9vYmFyYmF6cXV4"),
])
def test_redaction(text, leak):
    from deskmate.zaza_server.logs import redact

    assert leak not in redact(text)


def test_redaction_keeps_ordinary_lines():
    from deskmate.zaza_server.logs import redact

    line = '127.0.0.1:0 - "GET /manager/attendance?period=week HTTP/1.1" 200'
    assert redact(line) == line
    assert redact("sync rejected: invalid token (device header 'pc-1')") == \
        "sync rejected: invalid token (device header 'pc-1')"


def test_registered_secrets_are_masked_in_messages_and_tracebacks(tmp_path):
    from deskmate.zaza_server.logs import LogSettings, configure_logging

    log = tmp_path / "server.log"
    configure_logging(LogSettings(file=log), secrets=["Pg-Pw-That-Is-Long-42"])
    logger = logging.getLogger("zaza_server.test")
    try:
        logger.error("connect failed for Pg-Pw-That-Is-Long-42")
        try:
            raise RuntimeError("driver said Pg-Pw-That-Is-Long-42")
        except RuntimeError:
            logger.exception("boom")
    finally:
        _remove_zaza_handlers()
    text = log.read_text(encoding="utf-8")
    assert "Pg-Pw-That-Is-Long-42" not in text and text.count("***") >= 2 and "Traceback" in text


def _remove_zaza_handlers() -> None:
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "zaza_handler", False)]:
        root.removeHandler(h)
        h.close()


def test_log_files_are_bounded(tmp_path):
    from deskmate.zaza_server.logs import LogSettings, MiB, configure_logging

    log = tmp_path / "server.log"
    settings = configure_logging(LogSettings(file=log, max_bytes=MiB, backup_count=2))
    logger = logging.getLogger("zaza_server.test")
    try:
        line = "x" * 1000
        for _ in range(4000):  # ~4 MB written
            logger.info(line)
    finally:
        _remove_zaza_handlers()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["server.log", "server.log.1", "server.log.2"]
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= settings.max_disk_bytes


def test_log_settings_validation():
    from deskmate.zaza_server.logs import log_settings_from_env

    assert log_settings_from_env({}).file is None
    for env in ({"ZAZA_LOG_LEVEL": "LOUD"}, {"ZAZA_LOG_MAX_BYTES": "100"}, {"ZAZA_LOG_BACKUP_COUNT": "0"}):
        with pytest.raises(ConfigError):
            log_settings_from_env(env)


# ─── --env-file and check-config ───────────────────────────────────────────


def test_env_file(tmp_path):
    f = tmp_path / "zaza-server.env"
    f.write_text("# comment\n\nZAZA_DB_HOST=127.0.0.1\nZAZA_PUBLIC_HOST=\"zaza.example.test\"\nZAZA_EMPTY=\n",
                 encoding="utf-8")
    assert read_env_file(f) == {"ZAZA_DB_HOST": "127.0.0.1", "ZAZA_PUBLIC_HOST": "zaza.example.test"}
    f.write_text("ZAZA_DB_HOST=127.0.0.1\nPATH=C:\\evil\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="line 2") as exc:
        read_env_file(f)
    assert "evil" not in str(exc.value)
    with pytest.raises(ConfigError, match="cannot read"):
        read_env_file(tmp_path / "missing.env")


def test_env_file_does_not_override_the_process_environment(monkeypatch, tmp_path, capsys):
    from deskmate.zaza_server import __main__ as cli

    f = tmp_path / "zaza-server.env"
    f.write_text("ZAZA_SERVER_BACKEND=postgres\nZAZA_DB_USER=zaza_employee_app\nZAZA_DB_NAME=zaza_employee_management\n",
                 encoding="utf-8")
    monkeypatch.setenv("ZAZA_DB_USER", "zaza_employee_report")
    monkeypatch.delenv("ZAZA_DB_NAME", raising=False)
    monkeypatch.delenv("ZAZA_DATABASE_URL", raising=False)
    cli.main(["--env-file", str(f), "check-config"])
    out = capsys.readouterr().out
    assert "zaza_employee_report@127.0.0.1:5432/zaza_employee_management" in out


def _production_env(tmp_path) -> dict:
    pw = tmp_path / "db-app.password"
    pw.write_text("Sup3r-S3cret-Value-Never-Shown", encoding="utf-8")
    return {"ZAZA_SERVER_BACKEND": "postgres", "ZAZA_DB_NAME": "zaza_employee_management",
            "ZAZA_DB_USER": "zaza_employee_app", "ZAZA_DB_PASSWORD_FILE": str(pw), "ZAZA_DB_POOL_MAX": "3",
            "ZAZA_DASHBOARD_COOKIE_SECURE": "true", "ZAZA_PUBLIC_HOST": PUBLIC,
            "ZAZA_LOG_FILE": str(tmp_path / "zaza-server.log")}


def test_check_config_production_ok_and_never_shows_the_password(tmp_path, capsys):
    from deskmate.zaza_server.preflight import FAIL, check_config, print_checks

    checks = check_config(_production_env(tmp_path), production=True)
    assert not [c for c in checks if c.status == FAIL]
    assert print_checks(checks) == 0
    assert "Sup3r-S3cret-Value-Never-Shown" not in capsys.readouterr().out


@pytest.mark.parametrize(("change", "name"), [
    ({"ZAZA_SERVER_BACKEND": "sqlite"}, "backend"),
    ({"ZAZA_DASHBOARD_COOKIE_SECURE": "false"}, "dashboard cookies"),
    ({"ZAZA_PUBLIC_HOST": ""}, "public host"),
    ({"ZAZA_LOG_FILE": ""}, "log file"),
    ({"ZAZA_DB_PASSWORD": "inline-password"}, "database password source"),
    ({"ZAZA_DB_PASSWORD_FILE": "C:/nowhere/db-app.password"}, "database password file"),
    ({"ZAZA_TRUSTED_PROXY_IPS": "10.0.0.1"}, "http"),
])
def test_check_config_production_failures(tmp_path, change, name):
    from deskmate.zaza_server.preflight import FAIL, check_config

    env = {**_production_env(tmp_path), **change}
    checks = check_config(env, production=True)
    assert any(c.status == FAIL and c.name == name for c in checks), checks
    assert all("inline-password" not in c.detail for c in checks)


def test_check_config_development_defaults_pass():
    from deskmate.zaza_server.preflight import FAIL, check_config

    assert not [c for c in check_config({}) if c.status == FAIL]
