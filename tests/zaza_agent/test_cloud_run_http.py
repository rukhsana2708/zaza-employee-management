"""Phase 10F-A: the Cloud Run HTTP security foundation.

Explicit deployment modes; Cloud Run external HTTPS by deployment (never from
a header); forwarded headers ignored; strict Host validation; one central
external-origin / same-origin rule; the legacy dashboard never mounted in
Cloud Run mode; the sync API still single-database with no client-chosen
tenant. The Windows loopback-proxy behaviour is covered, unchanged, by
test_production_server.py."""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from deskmate.zaza_server.config import ConfigError  # noqa: E402
from deskmate.zaza_server.dashboard.config import DashboardSettings  # noqa: E402
from deskmate.zaza_server.dashboard.ratelimit import LoginLimits, LoginRateLimiter, RateLimiter  # noqa: E402
from deskmate.zaza_server.dashboard.routes import mount_dashboard  # noqa: E402
from deskmate.zaza_server.dashboard.security import COOKIE_NAME  # noqa: E402
from deskmate.zaza_server.http_security import (  # noqa: E402
    CLIENT_UNVERIFIED,
    CloudRunMiddleware,
    HttpMode,
    HttpSettings,
    TrustedProxyMiddleware,
    external_request,
    http_settings_from_env,
    install,
    same_origin,
)

from .test_dashboard import PASSWORD, standard  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "deskmate" / "zaza_server"
PUBLIC = "zaza-server-627254142785.asia-south1.run.app"
CLOUD = HttpSettings((PUBLIC,), mode="cloud-run")


def _code_text(path: Path) -> str:
    """Identifiers and string literals of a module, WITHOUT comments and
    docstrings (so prose about tenants or headers doesn't count)."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body                 and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            docstrings.add(id(node.body[0].value))
    parts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            parts.append(node.value)
        elif isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(node.name)
        elif isinstance(node, ast.arg):
            parts.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            parts.append(node.arg)
    return "\n".join(parts).lower()


def _scope(peer="169.254.1.1", headers=None, scheme="http", kind="http"):  # noqa: ANN001, ANN202
    return {"type": kind, "scheme": scheme, "client": (peer, 40000) if peer else None, "path": "/",
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or [])]}


def _through(middleware_cls, settings, scope):  # noqa: ANN001, ANN202
    seen: dict = {}

    async def app(s, receive, send):  # noqa: ANN001, ANN202
        seen.update(s)

    async def receive():  # noqa: ANN202
        return {"type": "http.request", "body": b""}

    async def send(message):  # noqa: ANN001, ANN202
        pass

    asyncio.run(middleware_cls(app, settings)(scope, receive, send))
    return seen


# ─── modes ─────────────────────────────────────────────────────────────────


def test_deployment_modes_are_explicit():
    assert http_settings_from_env({}).mode is HttpMode.LOOPBACK_PROXY  # unchanged default (Windows/VPS)
    for value in ("local", "loopback-proxy", "cloud-run", " Cloud-Run "):
        assert http_settings_from_env({"ZAZA_HTTP_DEPLOYMENT_MODE": value}).mode.value == value.strip().lower()
    for bad in ("cloudrun", "auto", "gcp", "proxy", "https"):
        with pytest.raises(ConfigError, match="ZAZA_HTTP_DEPLOYMENT_MODE"):
            http_settings_from_env({"ZAZA_HTTP_DEPLOYMENT_MODE": bad})


def test_trusted_proxy_setting_is_refused_in_cloud_run_mode():
    with pytest.raises(ConfigError, match="does not apply in cloud-run mode"):
        http_settings_from_env({"ZAZA_HTTP_DEPLOYMENT_MODE": "cloud-run", "ZAZA_TRUSTED_PROXY_IPS": "127.0.0.1"})


def test_mode_is_never_inferred_from_request_headers():
    """A request that LOOKS like Cloud Run (Google headers, X-Forwarded-*)
    changes nothing in loopback-proxy or local mode."""
    headers = [("x-forwarded-proto", "https"), ("x-cloud-trace-context", "abc/1;o=1"), ("forwarded", "proto=https"),
               ("x-forwarded-for", "198.51.100.9")]
    for mode in ("local", "loopback-proxy"):
        app = FastAPI()

        @app.get("/probe")
        def probe(request):  # noqa: ANN001, ANN202
            return {}

        seen = _through(TrustedProxyMiddleware, HttpSettings(), _scope("203.0.113.5", headers))
        assert seen["scheme"] == "http" and seen["client"][0] == "203.0.113.5"
        assert external_request(seen).client_verified


# ─── Cloud Run scheme, client and forwarded headers ────────────────────────


@pytest.mark.parametrize("proto", [None, "http", "https", "ftp", "http, https", "https, http"])
def test_cloud_run_external_scheme_is_https_whatever_the_header_says(proto):
    headers = [("host", PUBLIC)] + ([("x-forwarded-proto", proto)] if proto else [])
    seen = _through(CloudRunMiddleware, CLOUD, _scope(headers=headers))
    ext = external_request(seen)
    assert seen["scheme"] == "https" and ext.scheme == "https" and ext.origin == f"https://{PUBLIC}"


def test_cloud_run_websocket_scheme_is_wss():
    seen = _through(CloudRunMiddleware, CLOUD, _scope(headers=[("host", PUBLIC)], scheme="ws", kind="websocket"))
    assert seen["scheme"] == "wss" and external_request(seen).origin == f"https://{PUBLIC}"


@pytest.mark.parametrize("xff", ["198.51.100.7", "10.0.0.1, 198.51.100.7", "127.0.0.1", "not-an-ip", ""])
def test_cloud_run_never_takes_the_client_from_x_forwarded_for(xff):
    seen = _through(CloudRunMiddleware, CLOUD, _scope(peer="169.254.8.1", headers=[("x-forwarded-for", xff)]))
    assert seen["client"] == ("169.254.8.1", 40000)  # untouched TCP peer
    ext = external_request(seen)
    assert ext.client == CLIENT_UNVERIFIED and ext.client_verified is False


def test_cloud_run_ignores_forwarded_host():
    seen = _through(CloudRunMiddleware, CLOUD, _scope(headers=[("host", PUBLIC), ("x-forwarded-host", "evil.example"),
                                                               ("forwarded", "host=evil.example;proto=http")]))
    assert external_request(seen).host == PUBLIC


def test_local_mode_trusts_no_forwarded_header_even_from_loopback():
    app = FastAPI()

    @app.get("/who")
    def who(request: __import__("fastapi").Request):  # noqa: ANN202
        ext = external_request(request)
        return {"scheme": ext.scheme, "client": ext.client}

    install(app, HttpSettings(mode="local"))
    client = TestClient(app, client=("127.0.0.1", 1))
    body = client.get("/who", headers={"X-Forwarded-For": "198.51.100.5", "X-Forwarded-Proto": "https"}).json()
    assert body == {"scheme": "http", "client": "127.0.0.1"}


# ─── Host validation in Cloud Run mode ─────────────────────────────────────


def _health_app(settings=CLOUD):  # noqa: ANN001, ANN202
    from deskmate.zaza_server.app import create_app
    from deskmate.zaza_server.repository import InMemoryRepository

    app = create_app(InMemoryRepository())
    install(app, settings)
    return app


@pytest.mark.parametrize(("host", "status"), [
    (PUBLIC, 200), (PUBLIC.upper(), 200), (PUBLIC + ".", 200), (PUBLIC + ":443", 200), (PUBLIC + ":8080", 200),
    ("evil.example", 400), (f"{PUBLIC}.evil.example", 400), (f"evil-{PUBLIC}", 400),
    ("zaza-server-1.asia-south1.run.app", 400), ("", 400),
])
def test_cloud_run_host_validation(host, status):
    client = TestClient(_health_app(), client=("169.254.1.1", 1))
    assert client.get("/api/v1/sync/health", headers={"Host": host, "X-Forwarded-For": "198.51.100.1"}).status_code \
        == status


def test_crafted_forwarding_headers_cannot_bypass_the_host_check():
    client = TestClient(_health_app(), client=("169.254.1.1", 1))
    for headers in ({"Host": "evil.example", "X-Forwarded-Host": PUBLIC},
                    {"Host": "evil.example", "Forwarded": f"host={PUBLIC}"},
                    {"Host": "localhost", "X-Forwarded-For": "198.51.100.1"}):  # loopback name only when not forwarded
        assert client.get("/api/v1/sync/health", headers=headers).status_code == 400


def test_health_stays_minimal_in_cloud_run_mode():
    response = TestClient(_health_app(), client=("169.254.1.1", 1)).get(
        "/api/v1/sync/health", headers={"Host": PUBLIC})
    assert response.status_code == 200
    assert set(response.json()) == {"status", "api_version", "server_time"}
    for word in ("cloudsql", "zaza_t_", "postgres", "tenant", "organization", "run.app", "client"):
        assert word not in response.text.lower()


# ─── the one same-origin rule ──────────────────────────────────────────────


def _cloud_scope(*origins: str) -> dict:
    scope = _through(CloudRunMiddleware, CLOUD, _scope(headers=[("host", PUBLIC)] + [("origin", o) for o in origins]))
    return scope


@pytest.mark.parametrize(("origins", "ok"), [
    ((), True),                                          # no Origin: CSRF tokens still required
    ((f"https://{PUBLIC}",), True),
    ((f"https://{PUBLIC.upper()}",), True),
    ((f"https://{PUBLIC}:443",), True),
    ((f"http://{PUBLIC}",), False),                      # downgraded scheme
    ((f"https://{PUBLIC}:8443",), False),
    (("https://evil.example",), False),
    ((f"https://{PUBLIC}.evil.example",), False),
    (("null",), False),
    ((f"https://{PUBLIC}/manager",), False),             # an Origin never has a path
    ((f"https://user@{PUBLIC}",), False),
    ((f"https://{PUBLIC}, https://evil.example",), False),
    ((f"https://{PUBLIC}", f"https://{PUBLIC}"), False),  # several Origin headers
    (("",), False),
])
def test_same_origin_against_the_external_https_origin(origins, ok):
    assert same_origin(_cloud_scope(*origins)) is ok


def test_dashboard_has_no_private_origin_reconstruction():
    routes = (SERVER / "dashboard" / "routes.py").read_text(encoding="utf-8")
    assert "http_security.same_origin(request)" in routes
    assert "request.url.scheme" not in routes and "request.url.netloc" not in routes
    code = _code_text(SERVER / "dashboard" / "routes.py")
    assert "x-forwarded" not in code and "netloc" not in code


# ─── dashboard CSRF / cookies when mounted behind Cloud Run semantics ─────
# (serve never mounts it in Cloud Run mode; these prove the rules are right
# for the external https origin anyway)


def _dash(secure=False, limits=LoginLimits(3, 6, 900)):  # noqa: ANN001, ANN202
    d = standard()
    d.manager()
    app = FastAPI()
    mount_dashboard(app, d.repo, DashboardSettings(cookie_secure=secure), d.clock, LoginRateLimiter(limits))
    install(app, CLOUD)
    return d, app


def _login(client, headers=None, token_override=None):  # noqa: ANN001, ANN202
    page = client.get("/manager/login", headers={"Host": PUBLIC})
    token = re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
    return client.post("/manager/login", headers={"Host": PUBLIC, **(headers or {})},
                       data={"username": "manager", "password": PASSWORD,
                             "login_token": token if token_override is None else token_override})


def test_login_accepts_the_external_https_origin_and_refuses_others():
    _, app = _dash()
    good = TestClient(app, follow_redirects=False, base_url=f"http://{PUBLIC}", client=("169.254.1.1", 1))
    assert _login(good, {"Origin": f"https://{PUBLIC}"}).status_code == 303
    for origin in (f"http://{PUBLIC}", "https://evil.example", "null"):
        c = TestClient(app, follow_redirects=False, base_url=f"http://{PUBLIC}", client=("169.254.1.1", 1))
        assert _login(c, {"Origin": origin}).status_code == 403


def test_login_double_submit_token_still_required():
    _, app = _dash()
    c = TestClient(app, follow_redirects=False, base_url=f"http://{PUBLIC}", client=("169.254.1.1", 1))
    assert _login(c, {"Origin": f"https://{PUBLIC}"}, token_override="forged").status_code == 403


def test_authenticated_post_needs_csrf_token_and_external_origin():
    _, app = _dash()
    c = TestClient(app, follow_redirects=False, base_url=f"http://{PUBLIC}", client=("169.254.1.1", 1))
    assert _login(c, {"Origin": f"https://{PUBLIC}"}).status_code == 303
    page = c.get("/manager", headers={"Host": PUBLIC})
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    post = {"Host": PUBLIC}
    assert c.post("/manager/logout", headers={**post, "Origin": "https://evil.example"},
                  data={"csrf_token": csrf}).status_code == 403
    assert c.post("/manager/logout", headers={**post, "Origin": f"https://{PUBLIC}"},
                  data={"csrf_token": "wrong"}).status_code == 403
    assert c.post("/manager/logout", headers={**post, "Origin": f"https://{PUBLIC}"},
                  data={"csrf_token": csrf}).status_code == 303


def test_session_cookie_is_secure_and_strict():
    _, app = _dash(secure=True)
    c = TestClient(app, follow_redirects=False, base_url=f"https://{PUBLIC}", client=("169.254.1.1", 1))
    response = _login(c, {"Origin": f"https://{PUBLIC}"})
    cookie = [v for v in response.headers.get_list("set-cookie") if v.startswith(COOKIE_NAME)][0]
    assert response.status_code == 303 and "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie


def test_spoofed_forwarded_for_cannot_escape_the_login_limit_in_cloud_run():
    _, app = _dash(limits=LoginLimits(3, 50, 900))
    for i in range(3):
        c = TestClient(app, follow_redirects=False, base_url=f"http://{PUBLIC}", client=("169.254.1.1", 1))
        page = c.get("/manager/login", headers={"Host": PUBLIC})
        token = re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
        c.post("/manager/login", headers={"Host": PUBLIC, "X-Forwarded-For": f"198.51.100.{i}"},
               data={"username": f"user{i}", "password": "wrong-password", "login_token": token})
    c = TestClient(app, follow_redirects=False, base_url=f"http://{PUBLIC}", client=("169.254.1.1", 1))
    assert _login(c, {"X-Forwarded-For": "203.0.113.200"}).status_code == 429  # one "unverified" bucket


def test_rate_limiter_boundary_exists_for_the_future_saas_login():
    for name in ("acquire", "retry_after", "failure", "success"):
        assert hasattr(RateLimiter, name) and callable(getattr(LoginRateLimiter, name))
    doc = (SERVER / "dashboard" / "ratelimit.py").read_text(encoding="utf-8")
    assert "per PROCESS" in doc and "shared state" in doc


# ─── serve wiring ──────────────────────────────────────────────────────────


def _serve(monkeypatch, env, host="0.0.0.0", backend="postgres"):  # noqa: ANN001, ANN202
    from deskmate.zaza_server import __main__ as cli
    from deskmate.zaza_server import backends
    from deskmate.zaza_server.repository import InMemoryRepository

    captured: dict = {}
    monkeypatch.setattr(backends, "open_repository", lambda *a, **k: InMemoryRepository())
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(app=app, **kw))
    for key in ("ZAZA_DATABASE_URL", "ZAZA_PUBLIC_HOST", "ZAZA_DASHBOARD_COOKIE_SECURE", "ZAZA_LOG_FILE",
                "ZAZA_TRUSTED_PROXY_IPS", "ZAZA_HTTP_DEPLOYMENT_MODE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = cli.main(["--backend", backend, "serve", "--host", host, "--port", "8080"])
    return code, captured


CLOUD_ENV = {"ZAZA_HTTP_DEPLOYMENT_MODE": "cloud-run", "ZAZA_PUBLIC_HOST": PUBLIC, "ZAZA_DASHBOARD_COOKIE_SECURE": "true"}


def test_serve_in_cloud_run_mode(monkeypatch, capsys):
    code, captured = _serve(monkeypatch, CLOUD_ENV)
    out = capsys.readouterr().out
    assert code == 0 and (captured["host"], captured["port"]) == ("0.0.0.0", 8080)
    assert captured["proxy_headers"] is False and captured["server_header"] is False and "workers" not in captured
    assert "Cloud Run mode: public HTTPS is terminated by Cloud Run" in out and "listens internally on 0.0.0.0:8080" in out
    assert "WARNING: listening beyond localhost" not in out
    assert "Manager dashboard: disabled in Cloud Run mode" in out
    client = TestClient(captured["app"], follow_redirects=False, client=("169.254.1.1", 1), base_url=f"http://{PUBLIC}")
    assert client.get("/api/v1/sync/health").status_code == 200
    assert client.get("/manager/login").status_code == 404 and client.get("/manager").status_code == 404
    evil = TestClient(captured["app"], client=("169.254.1.1", 1), base_url="http://evil.example")
    assert evil.get("/api/v1/sync/health").status_code == 400


@pytest.mark.parametrize("missing", ["ZAZA_PUBLIC_HOST", "ZAZA_DASHBOARD_COOKIE_SECURE"])
def test_serve_refuses_incomplete_cloud_run_configuration(monkeypatch, capsys, missing):
    env = {k: v for k, v in CLOUD_ENV.items() if k != missing}
    code, captured = _serve(monkeypatch, env)
    assert code == 2 and "app" not in captured and missing in capsys.readouterr().err


def test_serve_refuses_trusted_proxies_in_cloud_run_mode(monkeypatch, capsys):
    code, _ = _serve(monkeypatch, {**CLOUD_ENV, "ZAZA_TRUSTED_PROXY_IPS": "127.0.0.1"})
    assert code == 2 and "cloud-run" in capsys.readouterr().err


def test_dashboard_policy_is_an_explicit_decision():
    from deskmate.zaza_server.__main__ import dashboard_policy

    cloud = HttpSettings(mode="cloud-run")
    for host in ("127.0.0.1", "0.0.0.0", "localhost"):
        assert dashboard_policy("postgres", host, cloud)[0] is False  # never, even on loopback
    legacy = HttpSettings()
    assert dashboard_policy("postgres", "127.0.0.1", legacy) == (True, "")
    assert dashboard_policy("postgres", "0.0.0.0", legacy)[0] is False   # unchanged Phase 7/10 rule
    assert dashboard_policy("sqlite", "127.0.0.1", legacy)[0] is False
    assert dashboard_policy("postgres", "127.0.0.1", HttpSettings(mode="local")) == (True, "")


def test_local_serve_is_unchanged(monkeypatch, capsys):
    code, captured = _serve(monkeypatch, {}, host="127.0.0.1", backend="sqlite")
    out = capsys.readouterr().out
    assert code == 0 and "Cloud Run mode" not in out and "ZaZa sync API on http://127.0.0.1:8080" in out


# ─── architecture guard: still the single-database sync API ───────────────


def test_sync_api_is_still_single_repository():
    from deskmate.zaza_server.app import create_app

    params = list(inspect.signature(create_app).parameters)
    assert params[0] == "repo" and not {"resolver", "tenant_manager", "context"} & set(params)


def test_no_client_supplied_tenant_routing():
    """Nothing in the HTTP path picks a tenant from the request: no
    organization header/parameter, no database name, no slug routing."""
    from deskmate.zaza_server.app import create_app
    from deskmate.zaza_server.repository import InMemoryRepository

    app = create_app(InMemoryRepository())
    names = set()
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for group in (dependant.query_params, dependant.header_params, dependant.path_params, dependant.cookie_params):
            names |= {p.name.lower() for p in group}
    assert not [n for n in names if re.search(r"org|tenant|database|dbname|slug", n)], names
    for path in (SERVER / "app.py", SERVER / "auth.py", SERVER / "service.py", SERVER / "http_security.py"):
        text = _code_text(path)
        for needle in ("x-zaza-organization", "organization_id", "tenant", "database_name", "dbname", "slug"):
            assert needle not in text, f"{path.name}: {needle}"


def test_entrypoint_and_uvicorn_proxy_headers_stay_off():
    main = (SERVER / "__main__.py").read_text(encoding="utf-8")
    assert "proxy_headers=False" in main and "forwarded_allow_ips" not in main
