"""Phase 10F-B: SaaS authentication — passwords, sessions, the shared login
rate limiter, CSRF/origin, membership authorization and user/employee
separation — without a database (an in-memory AuthStore with the same
contract). test_saas_postgres.py runs the same flows on the real c0002
schema."""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
from fastapi.testclient import TestClient  # noqa: E402

from deskmate.zaza_server import http_security  # noqa: E402
from deskmate.zaza_server.app import create_saas_app  # noqa: E402
from deskmate.zaza_server.config import ConfigError, DatabaseSettings  # noqa: E402
from deskmate.zaza_server.dashboard.security import PasswordPolicyError  # noqa: E402
from deskmate.zaza_server.repository import InMemoryRepository  # noqa: E402
from deskmate.zaza_server.tenancy import saas_auth  # noqa: E402
from deskmate.zaza_server.tenancy.connections import TenantRepositoryManager  # noqa: E402
from deskmate.zaza_server.tenancy.control_repository import User  # noqa: E402
from deskmate.zaza_server.tenancy.domain import MembershipRole, OrganizationStatus, UserStatus  # noqa: E402
from deskmate.zaza_server.tenancy.resolver import TenantResolver  # noqa: E402
from deskmate.zaza_server.tenancy.saas_auth import (  # noqa: E402
    LOGIN_COOKIE,
    SESSION_COOKIE,
    LoginRateLimited,
    Permission,
    PermissionDenied,
    SaasAuthService,
    SaasAuthSettings,
    SaasSession,
    permissions,
    require,
    saas_auth_settings_from_env,
)
from deskmate.zaza_server.tenancy.saas_sync import SaasDeviceAuthenticator  # noqa: E402

from .test_tenancy import FakeDirectory  # noqa: E402

UTC = timezone.utc
PW = "correct horse battery 42"   # test-only value
PW2 = "another fine passphrase 7"  # test-only value
HOST = "zaza-server-627254142785.asia-south1.run.app"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class MemoryAuthStore:
    """In-memory AuthStore (same contract as PostgresAuthStore)."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.users: dict[uuid.UUID, User] = {}
        self.hashes: dict[uuid.UUID, str] = {}
        self.versions: dict[uuid.UUID, int] = {}
        self.sessions: dict[str, SaasSession] = {}
        self.counters: dict[tuple[str, str, int], int] = {}
        self.audit: list[tuple] = []

    def add_user(self, email: str, status=UserStatus.ACTIVE) -> User:  # noqa: ANN001
        user = User(uuid.uuid4(), email, email.split("@")[0], status)
        self.users[user.user_id] = user
        return user

    def find_user_by_email(self, email):  # noqa: ANN001, ANN201
        from deskmate.zaza_server.tenancy import domain

        normalized = domain.normalize_email(email)
        return next((u for u in self.users.values() if u.email == normalized), None)

    def get_user(self, user_id):  # noqa: ANN001, ANN201
        return self.users.get(user_id)

    def get_password_hash(self, user_id):  # noqa: ANN001, ANN201
        return self.hashes.get(user_id)

    def set_password_hash(self, user_id, password_hash, now):  # noqa: ANN001, ANN201
        with self.lock:
            self.hashes[user_id] = password_hash
            self.versions[user_id] = self.versions.get(user_id, 0) + 1
            revoked = self._revoke(user_id, now, "password_change")
            self.audit.append(("user.password_set", user_id, {"sessions_revoked": revoked}))
            return self.versions[user_id], revoked

    def rehash_password(self, user_id, password_hash):  # noqa: ANN001, ANN201
        self.hashes[user_id] = password_hash

    def create_session(self, session, token_digest):  # noqa: ANN001, ANN201
        assert re.fullmatch(r"[0-9a-f]{64}", token_digest)
        self.sessions[token_digest] = session

    def session_by_hash(self, token_digest):  # noqa: ANN001, ANN201
        s = self.sessions.get(token_digest)
        return (s, self.users[s.user_id]) if s else None

    def touch_session(self, session_id, now):  # noqa: ANN001, ANN201
        for k, s in self.sessions.items():
            if s.session_id == session_id:
                self.sessions[k] = SaasSession(s.session_id, s.user_id, s.created_at, s.expires_at, now, s.revoked_at)

    def revoke_session(self, session_id, now, reason):  # noqa: ANN001, ANN201
        for k, s in self.sessions.items():
            if s.session_id == session_id and s.revoked_at is None:
                self.sessions[k] = SaasSession(s.session_id, s.user_id, s.created_at, s.expires_at, s.last_seen_at, now)

    def _revoke(self, user_id, now, reason) -> int:  # noqa: ANN001
        n = 0
        for k, s in self.sessions.items():
            if s.user_id == user_id and s.revoked_at is None:
                self.sessions[k] = SaasSession(s.session_id, s.user_id, s.created_at, s.expires_at, s.last_seen_at, now)
                n += 1
        return n

    def revoke_user_sessions(self, user_id, now, reason, *, audit):  # noqa: ANN001, ANN201
        with self.lock:
            n = self._revoke(user_id, now, reason)
            if audit:
                self.audit.append(("user.sessions_revoked", user_id, {"reason": reason, "sessions_revoked": n}))
            return n

    def count_attempt(self, bucket, key_hash, window_seconds):  # noqa: ANN001, ANN201
        with self.lock:  # atomic, like the PostgreSQL upsert
            epoch = int(self.clock().timestamp())
            window = epoch - epoch % window_seconds
            key = (bucket, key_hash, window)
            self.counters[key] = self.counters.get(key, 0) + 1
            return self.counters[key], max(1, window + window_seconds - epoch)

    def clear_attempts(self, bucket, key_hash):  # noqa: ANN001, ANN201
        with self.lock:
            for k in [k for k in self.counters if k[:2] == (bucket, key_hash)]:
                del self.counters[k]

    def cleanup(self, session_retention, counter_retention):  # noqa: ANN001, ANN201
        return 0, 0


@pytest.fixture
def env():
    clock = Clock()
    store = MemoryAuthStore(clock)
    service = SaasAuthService(store, SaasAuthSettings(cookie_secure=False, identity_max_attempts=5,
                                                      global_max_attempts=50), clock=clock)
    user = store.add_user("owner@example.com")
    service.set_password(user.user_id, PW)
    return SimpleNamespace(clock=clock, store=store, auth=service, user=user)


# ─── passwords and login ───────────────────────────────────────────────────


def test_passwords_are_argon2id_only_and_policy_checked(env):
    stored = env.store.hashes[env.user.user_id]
    assert stored.startswith("$argon2id$") and PW not in stored
    for weak in ("short", "aaaaaaaaaaaaaaaa", " leading-space-password", "owner-is-in-here-123"):
        with pytest.raises(PasswordPolicyError):
            env.auth.set_password(env.user.user_id, weak)


def test_login_success_and_generic_failures(env, monkeypatch):
    token, ctx = env.auth.login("Owner@Example.com ", PW)
    assert ctx.user_id == env.user.user_id and len(token) >= 40 and token not in env.store.sessions
    burned = []
    monkeypatch.setattr(saas_auth, "burn_password_check", lambda p: burned.append(1))
    assert env.auth.login("owner@example.com", "wrong password 123") is None
    assert env.auth.login("nobody@example.com", PW) is None           # unknown email -> dummy Argon2 check
    assert env.auth.login("not an email", PW) is None
    assert burned == [1, 1]
    nopw = env.store.add_user("nopw@example.com")
    assert env.auth.login(nopw.email, PW) is None and burned == [1, 1, 1]  # no credential -> dummy check too


def test_disabled_user_cannot_log_in_and_loses_existing_sessions(env):
    token, _ = env.auth.login("owner@example.com", PW)
    assert env.auth.validate_session(token) is not None
    env.store.users[env.user.user_id] = User(env.user.user_id, env.user.email, "x", UserStatus.DISABLED)
    assert env.auth.validate_session(token) is None      # immediately, even with an old token
    assert env.auth.login("owner@example.com", PW) is None


def test_session_expiry_revocation_and_password_change(env):
    token, ctx = env.auth.login("owner@example.com", PW)
    env.clock.now += timedelta(hours=11, minutes=59)
    assert env.auth.validate_session(token) is not None
    env.clock.now += timedelta(minutes=2)
    assert env.auth.validate_session(token) is None      # expired (12 h)
    t2, c2 = env.auth.login("owner@example.com", PW)
    env.auth.logout(c2)
    assert env.auth.validate_session(t2) is None         # revoked by logout
    t3, _ = env.auth.login("owner@example.com", PW)
    t4, _ = env.auth.login("owner@example.com", PW)
    version, revoked = env.auth.set_password(env.user.user_id, PW2)
    assert (version, revoked) == (2, 3)  # every unrevoked session (the expired one too)
    assert env.auth.validate_session(t3) is None and env.auth.validate_session(t4) is None
    assert env.auth.login("owner@example.com", PW) is None and env.auth.login("owner@example.com", PW2)
    assert env.auth.revoke_user_sessions(env.user.user_id) == 1
    assert [a[0] for a in env.store.audit][-1] == "user.sessions_revoked"
    for _, _, details in env.store.audit:
        assert not any(PW in str(v) or PW2 in str(v) for v in details.values())


def test_session_tokens_are_validated_strictly(env):
    for bogus in (None, "", "x" * 201, "not-a-session"):
        assert env.auth.validate_session(bogus) is None


# ─── shared rate limiter ───────────────────────────────────────────────────


def test_identity_limit_blocks_before_the_password_check(env):
    for _ in range(5):
        assert env.auth.login("owner@example.com", "wrong password 123") is None
    with pytest.raises(LoginRateLimited) as exc:
        env.auth.login("owner@example.com", PW)            # correct, but refused before checking
    assert 1 <= exc.value.retry_after <= 900
    env.clock.now += timedelta(seconds=901)
    assert env.auth.login("owner@example.com", PW) is not None  # next window


def test_unknown_identities_are_limited_identically(env):
    """No difference between existing and unknown emails (no enumeration)."""
    for email in ("owner@example.com", "ghost@example.com"):
        for _ in range(5):
            env.auth.login(email, "wrong password 123")
        with pytest.raises(LoginRateLimited):
            env.auth.login(email, "wrong password 123")


def test_success_clears_the_identity_bucket(env):
    for _ in range(4):
        env.auth.login("owner@example.com", "wrong password 123")
    assert env.auth.login("owner@example.com", PW) is not None
    for _ in range(5):
        env.auth.login("owner@example.com", "wrong password 123")  # a fresh 5 allowed again


def test_global_bucket_limits_bursts_across_identities(env):
    for i in range(50):
        env.auth.login(f"user{i}@example.com", "wrong password 123")
    with pytest.raises(LoginRateLimited):
        env.auth.login("owner@example.com", PW)


def test_concurrent_attempts_cannot_exceed_the_limit(env):
    results: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        try:
            outcome = "ok" if env.auth.login("owner@example.com", "wrong password 123") else "fail"
        except LoginRateLimited:
            outcome = "limited"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    assert results.count("fail") == 5 and results.count("limited") == 15


def test_limiter_keys_never_contain_the_email(env):
    env.auth.login("owner@example.com", "wrong password 123")
    for bucket, key, _ in env.store.counters:
        assert "owner" not in key and "@" not in key and re.fullmatch(r"[0-9a-f]{64}", key)


def test_auth_settings_bounds():
    assert saas_auth_settings_from_env({}) == SaasAuthSettings()
    for name, value in (("ZAZA_SAAS_SESSION_HOURS", "0"), ("ZAZA_SAAS_SESSION_HOURS", "1000"),
                        ("ZAZA_SAAS_LOGIN_MAX_ATTEMPTS_PER_IDENTITY", "1"),
                        ("ZAZA_SAAS_LOGIN_MAX_ATTEMPTS_GLOBAL", "5"), ("ZAZA_SAAS_COOKIE_SECURE", "maybe")):
        with pytest.raises(ConfigError):
            saas_auth_settings_from_env({name: value})


# ─── membership authorization ──────────────────────────────────────────────


def test_role_permissions_are_explicit_sets():
    perms = saas_auth.ROLE_PERMISSIONS
    assert set(perms) == set(MembershipRole)
    assert Permission.EMPLOYEES_VIEW_ALL in perms[MembershipRole.OWNER] | perms[MembershipRole.ADMIN]
    assert Permission.EMPLOYEES_VIEW_ALL not in perms[MembershipRole.MANAGER]   # team scopes come later
    assert perms[MembershipRole.MANAGER] == {Permission.ORG_VIEW, Permission.EMPLOYEES_VIEW_SCOPED,
                                             Permission.EMPLOYEES_VIEW_SELF}
    assert perms[MembershipRole.VIEWER] == {Permission.ORG_VIEW}
    assert Permission.ORG_SETTINGS_MANAGE not in perms[MembershipRole.ADMIN]


def test_device_and_job_contexts_carry_no_saas_permissions():
    d = FakeDirectory()
    org = d.org("acme")
    ctx = TenantResolver(d).for_job(org)
    assert permissions(ctx) == frozenset()
    with pytest.raises(PermissionDenied):
        require(ctx, Permission.ORG_VIEW)


def test_employee_role_never_becomes_a_saas_role():
    """The tenant `employees.role` and the control-plane membership role are
    unrelated: no mapping exists in either direction."""
    import inspect

    from deskmate.zaza_server.tenancy import saas_web

    for module in (saas_auth, saas_web):
        source = inspect.getsource(module)
        assert ".role ==" not in source.replace("tenant.role", "") and "employees.role" not in source.replace(
            "`employees.role`", "")
    repo = InMemoryRepository()
    repo.add_employee("e1", "Same Name", role="ADMIN")
    assert repo.get_employee("e1").role == "ADMIN"  # untouched; grants nothing in the SaaS


# ─── the SaaS web session API ──────────────────────────────────────────────


class Directory(FakeDirectory):
    def find_organization_id_by_slug(self, slug):  # noqa: ANN001, ANN201
        return next((o for o, org in self.orgs.items() if org.slug == slug), None)

    def list_memberships_for_user(self, user_id):  # noqa: ANN001, ANN201
        return [m for (o, u), m in self.members.items() if u == user_id]


@pytest.fixture
def web(env):
    d = Directory()
    a, b, c = d.org("org-a"), d.org("org-b"), d.org("org-c")
    d.users[env.user.user_id] = env.user
    d.member(a, env.user.user_id, "OWNER")
    d.member(b, env.user.user_id, "VIEWER")
    repos: dict[str, InMemoryRepository] = {}
    for org, name in ((a, "A"), (b, "B"), (c, "C")):
        repo = repos.setdefault(d.dbs[org].database_name, InMemoryRepository())
        repo.add_employee("employee-1", f"Employee of {name}", role="ADMIN")
    manager = TenantRepositoryManager(DatabaseSettings(dbname="zaza"),
                                      repository_factory=lambda s: repos[s.dbname])
    resolver = TenantResolver(d)
    runtime = SimpleNamespace(control=d, resolver=resolver, manager=manager, auth=env.auth,
                              devices=SaasDeviceAuthenticator(SimpleNamespace(organization_for_credential=lambda h: None),
                                                              resolver, manager))
    app = create_saas_app(runtime)
    http_security.install(app, http_security.HttpSettings((HOST,), mode="cloud-run"))
    client = TestClient(app, follow_redirects=False, base_url=f"http://{HOST}", client=("169.254.1.1", 1))
    return SimpleNamespace(client=client, dir=d, a=a, b=b, c=c, env=env)


def _sign_in(client, email="owner@example.com", password=PW, origin=f"https://{HOST}", token=None):  # noqa: ANN001, ANN202
    page = client.get("/app/login")
    login_token = page.json()["login_token"]
    return client.post("/app/login", headers={"Origin": origin} if origin else {},
                       data={"email": email, "password": password, "login_token": token or login_token})


def test_login_sets_secure_strict_cookies_over_the_external_https_origin(web):
    page = web.client.get("/app/login")
    login_cookie = page.headers["set-cookie"]
    assert "HttpOnly" in login_cookie and "SameSite=strict" in login_cookie and "Path=/app/login" in login_cookie
    web.env.auth.settings = SaasAuthSettings(cookie_secure=True, identity_max_attempts=5, global_max_attempts=50)
    secure = TestClient(web.client.app, follow_redirects=False, base_url=f"https://{HOST}", client=("169.254.1.1", 1))
    response = _sign_in(secure)
    assert response.status_code == 200
    cookie = [c for c in response.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE)][0]
    for flag in ("Secure", "HttpOnly", "SameSite=strict", "Path=/app"):
        assert flag in cookie
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("origin", [f"http://{HOST}", "https://evil.example", "null"])
def test_login_refuses_foreign_origins(web, origin):
    assert _sign_in(web.client, origin=origin).status_code == 403


def test_login_needs_the_double_submit_token(web):
    assert _sign_in(web.client, token="forged-token").status_code == 403
    fresh = TestClient(web.client.app, base_url=f"http://{HOST}", client=("169.254.1.1", 1))
    no_cookie = fresh.post("/app/login", headers={"Origin": f"https://{HOST}"},
                           data={"email": "owner@example.com", "password": PW, "login_token": "x"})
    assert no_cookie.status_code == 403


def test_login_errors_are_generic(web):
    wrong = _sign_in(web.client, password="wrong password 123")
    unknown = _sign_in(web.client, email="ghost@example.com")
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()
    assert wrong.json() == {"error": "invalid email or password"}


def test_login_rate_limit_returns_429_with_retry_after(web):
    for _ in range(5):
        _sign_in(web.client, password="wrong password 123")
    limited = _sign_in(web.client)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1


def test_one_session_multiple_organizations(web):
    assert _sign_in(web.client).status_code == 200
    me = web.client.get("/app/session").json()
    assert me["organizations"] == [{"slug": "org-a", "role": "OWNER"}, {"slug": "org-b", "role": "VIEWER"}]
    a = web.client.get("/app/orgs/org-a").json()
    assert a["role"] == "OWNER" and "employees.view_all" in a["permissions"]
    b = web.client.get("/app/orgs/org-b").json()
    assert b["role"] == "VIEWER" and b["permissions"] == ["org.view"]
    # no membership in C, and an unknown slug: the same 404 (no existence leak)
    assert web.client.get("/app/orgs/org-c").json() == web.client.get("/app/orgs/no-such-org").json() == {
        "error": "not found"}
    assert web.client.get("/app/orgs/org-c").status_code == 404


def test_tenant_data_only_through_the_resolved_membership(web):
    _sign_in(web.client)
    employees = web.client.get("/app/orgs/org-a/employees")
    assert employees.status_code == 200 and employees.json()["employees"] == [
        {"employee_id": "employee-1", "display_name": "Employee of A", "is_active": True}]
    assert web.client.get("/app/orgs/org-b/employees").status_code == 403   # VIEWER lacks employees.view_all
    assert web.client.get("/app/orgs/org-c/employees").status_code == 404   # no membership
    # employees.role=ADMIN in a tenant grants nothing: the SaaS role decides
    viewer = web.dir.members[(web.b, web.env.user.user_id)]
    assert viewer.role is MembershipRole.VIEWER


def test_suspended_organization_or_disabled_membership_is_not_offered(web):
    _sign_in(web.client)
    org = web.dir.orgs[web.a]
    web.dir.orgs[web.a] = org.__class__(org.organization_id, org.slug, org.display_name, OrganizationStatus.SUSPENDED,
                                        "UTC")
    assert web.client.get("/app/orgs/org-a").status_code == 404
    assert [o["slug"] for o in web.client.get("/app/session").json()["organizations"]] == ["org-b"]


def test_logout_needs_csrf_and_origin(web):
    csrf = _sign_in(web.client).json()["csrf_token"]
    assert web.client.post("/app/logout", headers={"Origin": "https://evil.example"},
                           data={"csrf_token": csrf}).status_code == 403
    assert web.client.post("/app/logout", headers={"Origin": f"https://{HOST}"},
                           data={"csrf_token": "wrong"}).status_code == 403
    assert web.client.post("/app/logout", headers={"Origin": f"https://{HOST}"},
                           data={"csrf_token": csrf}).status_code == 200
    assert web.client.get("/app/session").status_code == 401


def test_unauthenticated_requests_get_nothing(web):
    for path in ("/app/session", "/app/orgs/org-a", "/app/orgs/org-a/employees"):
        assert web.client.get(path).status_code == 401


def test_no_self_signup_routes(web):
    paths = {getattr(r, "path", "") for r in web.client.app.routes}
    for forbidden in ("signup", "register", "orgs/new", "tenants", "enroll"):
        assert not [p for p in paths if forbidden in p]
    methods = {(m, getattr(r, "path", "")) for r in web.client.app.routes for m in getattr(r, "methods", set())}
    posts = {p for m, p in methods if m == "POST"}
    assert posts == {"/app/login", "/app/logout", "/app/sign-in", "/app/sign-out", "/api/v1/sync/batch"}


def test_saas_web_has_no_private_origin_logic_or_sql():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "deskmate" / "zaza_server" / "tenancy" / "saas_web.py").read_text(
        encoding="utf-8")
    shared = (Path(__file__).resolve().parents[2] / "deskmate" / "zaza_server" / "tenancy" / "web_access.py").read_text(encoding="utf-8")
    assert "SaasWebAccess(runtime)" in source
    assert "http_security.same_origin(request)" in shared
    assert "request.url" not in source and "x-forwarded" not in source.lower()
    assert not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\s", source)


def test_cli_never_takes_a_password_argument():
    from deskmate.zaza_server.__main__ import main

    for command in ("saas-user-create", "saas-user-set-password"):
        with pytest.raises(SystemExit):
            main([command, "--email", "x@example.com", "--name", "X", "--password", "secret"])
