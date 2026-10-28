"""Phase 10F-B: credential-derived tenant routing for the SaaS sync API,
without a database: the REAL resolver, tenant connection manager,
SaasDeviceAuthenticator and create_saas_app, with one in-memory repository
per tenant database and a fake control directory/router. The PostgreSQL
proof is in test_saas_postgres.py."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from deskmate.zaza.sync.protocol import BATCH_PATH, DEVICE_ID_HEADER, DEVICE_PATH, HEALTH_PATH  # noqa: E402
from deskmate.zaza_server.app import create_app, create_saas_app  # noqa: E402
from deskmate.zaza_server.auth import register_device  # noqa: E402
from deskmate.zaza_server.config import DatabaseSettings  # noqa: E402
from deskmate.zaza_server.repository import InMemoryRepository  # noqa: E402
from deskmate.zaza_server.tenancy.connections import TenantPoolLimits, TenantRepositoryManager  # noqa: E402
from deskmate.zaza_server.tenancy.context import TenantDatabaseDescriptor  # noqa: E402
from deskmate.zaza_server.tenancy.device_routes import routing_hash  # noqa: E402
from deskmate.zaza_server.tenancy.domain import OrganizationStatus, TenantDatabaseStatus  # noqa: E402
from deskmate.zaza_server.tenancy.resolver import TenantResolver  # noqa: E402
from deskmate.zaza_server.tenancy.saas_sync import SaasDeviceAuthenticator  # noqa: E402

from .test_postgres_integration import incoming, usage  # noqa: E402
from .test_tenancy import FakeDirectory  # noqa: E402

BASE = DatabaseSettings(host="/cloudsql/zaza-employee-monitor:asia-south1:zaza-test", dbname="zaza", user="zaza_app")


class NonClosing:
    """The tenant's repository as the manager sees it (close = no-op, so an
    evicted pool can be re-opened onto the same in-memory data)."""

    def __init__(self, repo: InMemoryRepository) -> None:
        self._repo = repo
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return getattr(self._repo, name)


class Router:
    def __init__(self) -> None:
        self.routes: dict[str, uuid.UUID] = {}

    def organization_for_credential(self, route_hash: str):  # noqa: ANN201
        return self.routes.get(route_hash)


@dataclass
class Runtime:
    """What create_saas_app needs for the SYNC routes (auth is unused here)."""

    control: FakeDirectory
    manager: TenantRepositoryManager
    routes: Router
    resolver: TenantResolver = None
    devices: SaasDeviceAuthenticator = None
    auth: object = None
    tenant_repos: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        from types import SimpleNamespace

        from deskmate.zaza_server.tenancy.saas_auth import SaasAuthSettings

        self.auth = self.auth or SimpleNamespace(settings=SaasAuthSettings(cookie_secure=False))
        self.resolver = TenantResolver(self.control)
        self.devices = SaasDeviceAuthenticator(self.routes, self.resolver, self.manager)


def _world(cache_max: int = 8):  # noqa: ANN202
    directory = FakeDirectory()
    repos: dict[str, InMemoryRepository] = {}
    opened: list[str] = []

    def factory(settings: DatabaseSettings):  # noqa: ANN202
        opened.append(settings.dbname)
        return NonClosing(repos.setdefault(settings.dbname, InMemoryRepository()))

    manager = TenantRepositoryManager(BASE, TenantPoolLimits(cache_max=cache_max), repository_factory=factory)
    runtime = Runtime(directory, manager, Router())
    runtime.tenant_repos = repos
    tenants = {}
    for name in ("a", "b"):
        org = directory.org(f"tenant-{name}")
        repo = repos.setdefault(directory.dbs[org].database_name, InMemoryRepository())
        repo.add_employee("employee-1", f"Employee of {name}")
        token = register_device(repo, "device-1", "employee-1").token
        runtime.routes.routes[routing_hash(token)] = org
        tenants[name] = {"org": org, "repo": repo, "token": token, "db": directory.dbs[org].database_name}
    return runtime, tenants, opened


def _h(token: str, device: str = "device-1") -> dict:
    return {"Authorization": f"Bearer {token}", DEVICE_ID_HEADER: device}


def _batch(label: str, n: int = 2, device: str = "device-1") -> tuple[dict, set[str]]:
    records = []
    for i in range(n):
        raw = usage(app_name=f"{label}-{i}.exe")
        raw["employee_id"] = "employee-1"
        raw["device_id"] = device
        records.append(raw)
    batch = {"batch_id": str(uuid.uuid4()), "device_id": device, "agent_version": "test",
             "sent_at": records[0]["updated_at"], "records": records}
    return batch, {r["record_id"] for r in records}


# ─── same ids in two tenants ───────────────────────────────────────────────


def test_same_device_and_employee_ids_route_by_credential():
    runtime, t, _ = _world()
    client = TestClient(create_saas_app(runtime))
    for name in ("a", "b"):
        r = client.get(DEVICE_PATH, headers=_h(t[name]["token"]))
        assert r.status_code == 200 and r.json() == {"device_id": "device-1", "employee_id": "employee-1",
                                                      "status": "ACTIVE"}
    written = {}
    for name in ("a", "b"):
        batch, ids = _batch(f"tenant-{name}")
        r = client.post(BATCH_PATH, json=batch, headers=_h(t[name]["token"]))
        assert r.status_code == 200 and all(x["status"] == "accepted" for x in r.json()["results"])
        written[name] = ids
    for name, other in (("a", "b"), ("b", "a")):
        mine = {r.record_id for r in t[name]["repo"].list_records()}
        assert mine == written[name] and not mine & written[other]
        assert all(r.payload["data"]["app_name"].startswith(f"tenant-{name}") for r in t[name]["repo"].list_records())
    # last_seen bookkeeping happened in the RIGHT tenant only
    assert t["a"]["repo"].get_device("device-1").last_seen_at and t["b"]["repo"].get_device("device-1").last_seen_at


def test_a_token_never_authenticates_in_b_even_with_the_same_device_id():
    runtime, t, opened = _world()
    client = TestClient(create_saas_app(runtime))
    # A's token routes to A; B's device id is the same string, but B is never opened for A's token
    opened.clear()
    assert client.get(DEVICE_PATH, headers=_h(t["a"]["token"])).status_code == 200
    assert set(opened) <= {t["a"]["db"]}
    # route A, but the token exists only in B -> refused in A, never retried in B
    runtime.routes.routes[routing_hash(t["b"]["token"])] = t["a"]["org"]
    opened.clear()
    r = client.get(DEVICE_PATH, headers=_h(t["b"]["token"]))
    assert r.status_code == 401 and r.json() == {"error": "invalid credentials"}
    assert t["b"]["db"] not in opened


# ─── wrong / malicious routes ──────────────────────────────────────────────


def _status(client, headers, path=DEVICE_PATH, **kw):  # noqa: ANN001, ANN202
    return client.get(path, headers=headers, **kw)


def test_wrong_route_matrix_fails_safely():
    runtime, t, _ = _world()
    client = TestClient(create_saas_app(runtime))
    a, b = t["a"], t["b"]
    leak_words = (a["db"], b["db"], str(a["org"]), str(b["org"]), "tenant-a", "tenant-b", "zaza_t_", "organization")

    def check(response, status, error):  # noqa: ANN001, ANN202
        assert (response.status_code, response.json().get("error")) == (status, error)
        assert not any(w in response.text for w in leak_words)

    # unknown route (a token nobody routes)
    check(_status(client, _h("zzd_" + "x" * 43)), 401, "invalid credentials")
    # valid A token with a device id that is not its device
    check(_status(client, _h(a["token"], "device-2")), 401, "invalid credentials")
    # missing / malformed headers
    check(client.get(DEVICE_PATH), 401, "missing credentials")
    check(_status(client, {"Authorization": "Basic abc", DEVICE_ID_HEADER: "device-1"}), 401,
          "malformed authorization header")
    # route deleted
    route = routing_hash(a["token"])
    saved = runtime.routes.routes.pop(route)
    check(_status(client, _h(a["token"])), 401, "invalid credentials")
    runtime.routes.routes[route] = saved
    # route to a SUSPENDED organization
    org = runtime.control.orgs[a["org"]]
    runtime.control.orgs[a["org"]] = org.__class__(org.organization_id, org.slug, org.display_name,
                                                   OrganizationStatus.SUSPENDED, "UTC")
    check(_status(client, _h(a["token"])), 401, "invalid credentials")
    runtime.control.orgs[a["org"]] = org
    # route to a non-READY tenant database
    db = runtime.control.dbs[a["org"]]
    runtime.control.dbs[a["org"]] = TenantDatabaseDescriptor(db.organization_id, db.database_name, db.region,
                                                             TenantDatabaseStatus.PROVISIONING)
    check(_status(client, _h(a["token"])), 401, "invalid credentials")
    runtime.control.dbs[a["org"]] = db
    # token revoked INSIDE the tenant (route still active)
    a["repo"].revoke_tokens("device-1")
    check(_status(client, _h(a["token"])), 403, "token revoked")
    # device disabled inside the tenant
    t2 = register_device(b["repo"], "device-2", "employee-1").token
    runtime.routes.routes[routing_hash(t2)] = b["org"]
    b["repo"].set_device_status("device-2", "DISABLED")
    check(_status(client, _h(t2, "device-2")), 403, "device disabled")
    # B unaffected through all of this
    assert _status(client, _h(b["token"])).status_code == 200


@pytest.mark.parametrize("forged", [
    {"X-ZaZa-Organization": "tenant-b"}, {"X-ZaZa-Organization-Id": "00000000-0000-0000-0000-000000000000"},
    {"X-ZaZa-Database": "zaza_t_aaaaaaaaaaaaaaaaaaaaaaaaaa"}, {"X-ZaZa-Tenant": "tenant-b"},
])
def test_client_supplied_organization_or_database_is_ignored(forged):
    runtime, t, opened = _world()
    client = TestClient(create_saas_app(runtime))
    opened.clear()
    r = client.get(DEVICE_PATH, headers={**_h(t["a"]["token"]), **forged},
                   params={"organization_id": str(t["b"]["org"]), "tenant": "tenant-b", "database": t["b"]["db"]})
    assert r.status_code == 200 and set(opened) <= {t["a"]["db"]}  # still A, decided by the credential only
    batch, ids = _batch("forged")
    batch_with_extra = {**batch, "organization_id": str(t["b"]["org"])}
    r = client.post(BATCH_PATH, json=batch_with_extra, headers={**_h(t["a"]["token"]), **forged})
    assert r.status_code == 422  # unknown envelope fields are refused, never interpreted
    assert not {rec.record_id for rec in t["b"]["repo"].list_records()} & ids


def test_resolver_compares_but_never_follows_a_claimed_organization():
    runtime, t, _ = _world()
    from deskmate.zaza_server.tenancy.resolver import TenantAccessDenied

    assert runtime.resolver.for_device_route(runtime.routes, t["a"]["token"]).organization_id == t["a"]["org"]
    with pytest.raises(TenantAccessDenied):
        runtime.resolver.for_device_route(runtime.routes, t["a"]["token"], claimed_organization_id=t["b"]["org"])
    assert runtime.resolver.for_device_route(runtime.routes, t["a"]["token"],
                                             claimed_organization_id=t["a"]["org"]).organization_id == t["a"]["org"]


def test_saas_sync_routes_take_no_tenant_parameters():
    import re

    runtime, _, _ = _world()
    app = create_saas_app(runtime)
    for route in app.routes:
        if getattr(route, "path", "") not in (DEVICE_PATH, BATCH_PATH, HEALTH_PATH):
            continue
        dependant = route.dependant
        names = {p.name.lower() for g in (dependant.query_params, dependant.header_params, dependant.path_params,
                                           dependant.cookie_params) for p in g}
        assert not [n for n in names if re.search(r"org|tenant|database|dbname|slug", n)]


def test_routing_hash_is_not_the_tenant_token_hash():
    from deskmate.zaza_server.auth import hash_token

    token = "zzd_" + "q" * 43
    assert routing_hash(token) != hash_token(token) and len(routing_hash(token)) == 64
    assert routing_hash(f"  {token} ") == routing_hash(token)


# ─── health and legacy ─────────────────────────────────────────────────────


def test_health_is_tenant_independent_and_minimal():
    runtime, _, opened = _world()
    opened.clear()
    r = TestClient(create_saas_app(runtime)).get(HEALTH_PATH)
    assert r.status_code == 200 and set(r.json()) == {"status", "api_version", "server_time"} and opened == []


def test_legacy_single_repository_app_is_unchanged():
    repo = InMemoryRepository()
    repo.add_employee("employee-1", "Legacy")
    token = register_device(repo, "device-1", "employee-1").token
    client = TestClient(create_app(repo))
    assert client.get(DEVICE_PATH, headers=_h(token)).json()["device_id"] == "device-1"
    batch, ids = _batch("legacy")
    assert client.post(BATCH_PATH, json=batch, headers=_h(token)).status_code == 200
    assert {r.record_id for r in repo.list_records()} == ids


# ─── concurrency and pool eviction ─────────────────────────────────────────


def test_concurrent_interleaved_tenants_stay_isolated_through_pool_eviction():
    runtime, t, opened = _world(cache_max=1)  # every switch between A and B evicts the other pool
    client = TestClient(create_saas_app(runtime))
    written = {"a": set(), "b": set()}
    lock = threading.Lock()
    errors: list[str] = []

    def worker(name: str, w: int, rounds: int) -> None:
        for i in range(rounds):
            batch, ids = _batch(f"tenant-{name}-w{w}-{i}", n=3)  # unique like PostgreSQL requires
            for attempt in range(400):  # a 503 (the one pool busy) is retried, like an agent would
                r = client.post(BATCH_PATH, json=batch, headers=_h(t[name]["token"]))
                if r.status_code != 503:
                    break
                time.sleep(0.002 * (1 + attempt % 10))
            if r.status_code != 200 or not all(x["status"] == "accepted" for x in r.json()["results"]):
                errors.append(f"{name}: {r.status_code} {r.text[:120]}")
                continue
            with lock:
                written[name] |= ids

    threads = [threading.Thread(target=worker, args=(name, w, 25)) for w, name in enumerate(("a", "b") * 4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    assert errors == []
    assert len(written["a"]) == len(written["b"]) == 4 * 25 * 3  # every batch landed
    for name, other in (("a", "b"), ("b", "a")):
        mine = {r.record_id for r in t[name]["repo"].list_records()}
        assert mine == written[name] and not mine & written[other]
        assert all(r.payload["data"]["app_name"].startswith(f"tenant-{name}-") for r in t[name]["repo"].list_records())
    assert len(opened) > 2  # pools were evicted and re-opened during the run


# ─── serve / preflight wiring ──────────────────────────────────────────────


def test_saas_serve_never_migrates_or_mounts_the_legacy_dashboard():
    import inspect

    from deskmate.zaza_server import __main__ as cli

    source = inspect.getsource(cli._serve_saas).split("\"\"\"")[2]  # code after the docstring
    for word in ("upgrade", "migrat", "provision", "_mount_dashboard", "create_app("):
        assert word not in source


def test_serve_in_saas_mode_runs_the_saas_app(monkeypatch, capsys):
    from deskmate.zaza_server import __main__ as cli
    from deskmate.zaza_server.tenancy import saas_sync

    runtime, _, _ = _world()
    runtime.control.settings = BASE
    runtime.close = lambda: setattr(runtime, "closed", True)
    monkeypatch.setattr(saas_sync.SaasRuntime, "from_env", classmethod(lambda cls, env=None: runtime))
    captured: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(app=app, **kw))
    for key in ("ZAZA_PUBLIC_HOST", "ZAZA_TRUSTED_PROXY_IPS", "ZAZA_LOG_FILE", "ZAZA_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    host = "zaza-server-627254142785.asia-south1.run.app"
    for key, value in {"ZAZA_TENANCY_MODE": "saas", "ZAZA_HTTP_DEPLOYMENT_MODE": "cloud-run",
                       "ZAZA_PUBLIC_HOST": host, "ZAZA_DASHBOARD_COOKIE_SECURE": "true"}.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["--backend", "postgres", "serve", "--host", "0.0.0.0", "--port", "8080"]) == 0
    out = capsys.readouterr().out
    assert "SaaS mode" in out and "legacy dashboard is not served" in out and runtime.closed
    assert captured["proxy_headers"] is False
    paths = {getattr(r, "path", "") for r in captured["app"].routes}
    assert {"/app/login", DEVICE_PATH, BATCH_PATH, HEALTH_PATH} <= paths and not [p for p in paths if "manager" in p]
    client = TestClient(captured["app"], base_url=f"http://{host}", client=("169.254.1.1", 1))
    assert client.get(HEALTH_PATH).status_code == 200
    assert TestClient(captured["app"], base_url="http://evil.example").get(HEALTH_PATH).status_code == 400


def test_tenancy_mode_values():
    from deskmate.zaza_server.__main__ import tenancy_mode_from_env
    from deskmate.zaza_server.config import ConfigError

    assert tenancy_mode_from_env({}) == "single" and tenancy_mode_from_env({"ZAZA_TENANCY_MODE": " SaaS "}) == "saas"
    with pytest.raises(ConfigError):
        tenancy_mode_from_env({"ZAZA_TENANCY_MODE": "multi"})


def test_cloud_run_preflight_requires_saas_tenancy(tmp_path):
    from deskmate.zaza_server.preflight import FAIL, check_cloud_run_config

    from .test_cloud_sql import _cloud_run_env

    named = {c.name: c for c in check_cloud_run_config(_cloud_run_env(tmp_path, ZAZA_TENANCY_MODE="single"))}
    assert named["tenancy mode"].status == FAIL
    named = {c.name: c for c in check_cloud_run_config(_cloud_run_env(tmp_path))}
    assert named["tenancy mode"].status != FAIL
