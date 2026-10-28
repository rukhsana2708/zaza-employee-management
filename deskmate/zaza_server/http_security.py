"""Reverse-proxy and Host-header hardening for ``serve`` (Phase 10, 10F-A).

The deployment mode is an explicit server setting, never inferred from a
request (``ZAZA_HTTP_DEPLOYMENT_MODE``):

``loopback-proxy`` (default)  the Windows/VPS layout: the server listens on
                    ``127.0.0.1:8100`` and Caddy on the same machine
                    terminates HTTPS. Forwarded headers are trusted only
                    from loopback peers (below). Unchanged since Phase 10.
``local``           direct connections (development): forwarded headers are
                    never trusted; the TCP peer and scheme are used as is.
``cloud-run``       Google Cloud Run terminates public TLS and forwards to
                    the container on ``0.0.0.0:$PORT`` (Phase 10F-A). The
                    external scheme is HTTPS **by deployment definition**
                    (Cloud Run serves only HTTPS publicly); every
                    ``X-Forwarded-*`` header is ignored, so a client cannot
                    choose the scheme. The client address is NOT trusted:
                    the TCP peer is Google's front end, and
                    ``X-Forwarded-For`` can be extended by the client, so no
                    security decision may depend on it. Host is validated
                    against ``ZAZA_PUBLIC_HOST`` exactly as elsewhere, and
                    ``serve`` refuses Cloud Run mode without it.

:func:`external_request` is the ONE place that derives the external
scheme, host and origin of a request; :func:`same_origin` is the one
same-origin check (dashboard forms and logins use it).

Loopback-proxy details:

- :class:`TrustedProxyMiddleware` uses ``X-Forwarded-For`` and
  ``X-Forwarded-Proto`` **only** when the TCP peer is one of the trusted
  proxy addresses (loopback only: the proxy must run on this server). From
  any other peer those headers are ignored, so a client cannot choose its
  own address (login rate limiting) or scheme (same-origin checks) by
  sending them. uvicorn's own proxy-header handling is switched off so the
  headers are interpreted in exactly one place.
- :class:`HostCheckMiddleware` answers 400 to any ``Host`` that is not one
  of the configured public names. Loopback names (``127.0.0.1``,
  ``localhost``, ``::1``) are accepted only for requests that did not come
  through the proxy, for local health checks on the server itself.

Environment:

``ZAZA_PUBLIC_HOST``        the public host name(s) of the server, comma-
                            separated (no scheme, no port). Unset: no Host
                            check (local development).
``ZAZA_TRUSTED_PROXY_IPS``  addresses whose forwarded headers are trusted,
                            comma-separated loopback addresses (default
                            ``127.0.0.1,::1``). loopback-proxy mode only;
                            refused in cloud-run mode.
``ZAZA_HTTP_DEPLOYMENT_MODE`` ``loopback-proxy`` (default), ``local`` or
                            ``cloud-run``.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from enum import Enum

from .config import ConfigError

ENV_PUBLIC_HOST = "ZAZA_PUBLIC_HOST"
ENV_TRUSTED_PROXIES = "ZAZA_TRUSTED_PROXY_IPS"
ENV_HTTP_MODE = "ZAZA_HTTP_DEPLOYMENT_MODE"


class HttpMode(str, Enum):
    LOCAL = "local"
    LOOPBACK_PROXY = "loopback-proxy"
    CLOUD_RUN = "cloud-run"


CLIENT_UNVERIFIED = "unverified"  # client key when the platform hides the real client
DEFAULT_TRUSTED_PROXIES = ("127.0.0.1", "::1")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _ip(text: str) -> IPAddress | None:
    """An IP address from a header value such as ``203.0.113.7``,
    ``203.0.113.7:4711``, ``2001:db8::1`` or ``[2001:db8::1]:4711``."""
    text = text.strip()
    if text.startswith("["):
        text = text[1:].partition("]")[0]
    elif text.count(":") == 1:
        text = text.partition(":")[0]  # IPv4 with a port
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


@dataclass(frozen=True)
class HttpSettings:
    public_hosts: tuple[str, ...] = ()
    trusted_proxies: tuple[str, ...] = DEFAULT_TRUSTED_PROXIES
    mode: HttpMode = HttpMode.LOOPBACK_PROXY

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "mode", HttpMode(self.mode))
        except ValueError:
            raise ConfigError(f"{ENV_HTTP_MODE} must be one of {', '.join(m.value for m in HttpMode)}") from None
        for host in self.public_hosts:
            if not _HOSTNAME_RE.match(host):
                raise ConfigError(f"{ENV_PUBLIC_HOST}: {host!r} is not a plain host name "
                                  "(no scheme, port, path or wildcard)")
        for proxy in self.trusted_proxies:
            address = _ip(proxy)
            if address is None or str(address) != proxy:
                raise ConfigError(f"{ENV_TRUSTED_PROXIES}: {proxy!r} is not an IP address")
            if not address.is_loopback:
                raise ConfigError(f"{ENV_TRUSTED_PROXIES}: {proxy} is not a loopback address; "
                                  "the reverse proxy must run on this server")

    @property
    def host_check(self) -> bool:
        return bool(self.public_hosts)

    @property
    def cloud_run(self) -> bool:
        return self.mode is HttpMode.CLOUD_RUN

    @property
    def legacy_dashboard_allowed(self) -> bool:
        """The legacy single-organization manager dashboard is never served in
        Cloud Run mode: SaaS authentication (control-plane users and
        memberships) replaces its per-tenant manager accounts first."""
        return self.mode is not HttpMode.CLOUD_RUN

    def trusted(self) -> frozenset[IPAddress]:
        return frozenset(ipaddress.ip_address(p) for p in self.trusted_proxies)


def http_settings_from_env(env: dict | None = None) -> HttpSettings:
    env = dict(os.environ if env is None else env)
    hosts = tuple(h.strip().lower().rstrip(".") for h in (env.get(ENV_PUBLIC_HOST) or "").split(",") if h.strip())
    raw = (env.get(ENV_TRUSTED_PROXIES) or "").strip()
    proxies = tuple(p.strip() for p in raw.split(",") if p.strip()) if raw else DEFAULT_TRUSTED_PROXIES
    mode = (env.get(ENV_HTTP_MODE) or HttpMode.LOOPBACK_PROXY.value).strip().lower()
    if mode == HttpMode.CLOUD_RUN.value and raw:
        raise ConfigError(f"{ENV_TRUSTED_PROXIES} does not apply in cloud-run mode: forwarded headers are never "
                          "trusted there")
    return HttpSettings(public_hosts=hosts, trusted_proxies=proxies, mode=mode)


def _header(scope: dict, name: bytes) -> str | None:
    """All values of a request header, joined with commas (as HTTP allows)."""
    values = [v.decode("latin-1") for k, v in scope.get("headers") or () if k.lower() == name]
    return ",".join(values) if values else None


class TrustedProxyMiddleware:
    """Takes the client address and scheme from the trusted proxy's
    forwarded headers; ignores those headers from anyone else.

    The client address is the right-most ``X-Forwarded-For`` entry that is
    not itself a trusted proxy (Caddy appends the address it saw). An
    unparsable entry stops the walk and the TCP peer is kept."""

    def __init__(self, app, settings: HttpSettings) -> None:  # noqa: ANN001
        self.app = app
        self.trusted = settings.trusted()

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            peer = _ip(client[0]) if client else None
            if peer is not None and peer in self.trusted:
                self._apply(scope)
        await self.app(scope, receive, send)

    def _apply(self, scope: dict) -> None:
        forwarded_for = _header(scope, b"x-forwarded-for")
        if forwarded_for:
            for entry in reversed(forwarded_for.split(",")):
                address = _ip(entry)
                if address is None:
                    break
                if address not in self.trusted:
                    scope["client"] = (str(address), 0)
                    break
        proto = _header(scope, b"x-forwarded-proto")
        if proto:
            proto = proto.split(",")[-1].strip().lower()
            if proto in ("http", "https"):
                if scope["type"] == "websocket":
                    proto = "wss" if proto == "https" else "ws"
                scope["scheme"] = proto


def _host_name(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].partition("]")[0]
    return host.rpartition(":")[0] if host.count(":") == 1 else host


class HostCheckMiddleware:
    """400 for a ``Host`` header that names neither a configured public host
    nor (for direct, non-proxied local requests) a loopback name."""

    def __init__(self, app, settings: HttpSettings) -> None:  # noqa: ANN001
        self.app = app
        self.allowed = frozenset(h.rstrip(".") for h in settings.public_hosts)

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] in ("http", "websocket") and not self._ok(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({"type": "http.response.start", "status": 400,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b"Invalid host header"})
            return
        await self.app(scope, receive, send)

    def _ok(self, scope: dict) -> bool:
        host = _host_name(_header(scope, b"host") or "").rstrip(".")
        if host in self.allowed:
            return True
        return host in LOOPBACK_HOSTS and _header(scope, b"x-forwarded-for") is None


_CLIENT_TRUST_KEY = "zaza.client_trusted"
_DEFAULT_PORTS = {"http": "80", "https": "443"}
_ORIGIN_RE = re.compile(r"^(https?)://([^/?#@\s,]+)$")


class CloudRunMiddleware:
    """Cloud Run mode: the external scheme is HTTPS (WSS for websockets) by
    deployment definition, whatever ``X-Forwarded-Proto`` says, and the
    client address is marked unverified. ``X-Forwarded-For`` is never used."""

    def __init__(self, app, settings: HttpSettings) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] in ("http", "websocket"):
            scope["scheme"] = "wss" if scope["type"] == "websocket" else "https"
            scope[_CLIENT_TRUST_KEY] = False
        await self.app(scope, receive, send)


def _normalize_netloc(scheme: str, netloc: str) -> str:
    """Lower case, no trailing dot, no default port: ``Zaza.Example.com.:443``
    over https becomes ``zaza.example.com``."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        inner, _, rest = netloc[1:].partition("]")
        host, port = f"[{inner}]", rest[1:] if rest.startswith(":") else ""
    elif netloc.count(":") == 1:
        host, _, port = netloc.partition(":")
    else:
        host, port = netloc, ""
    host = host.rstrip(".")
    return host if not port or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"


@dataclass(frozen=True)
class ExternalRequest:
    """How the request looks from OUTSIDE the deployment.

    ``client_verified`` is False when the platform hides the real client
    (Cloud Run): ``client`` is then :data:`CLIENT_UNVERIFIED`, never a
    header value, and must not be used for security decisions."""

    scheme: str
    host: str
    origin: str
    client: str
    client_verified: bool


def external_request(request_or_scope) -> ExternalRequest:  # noqa: ANN001 — Starlette Request or ASGI scope
    scope = getattr(request_or_scope, "scope", request_or_scope)
    raw = scope.get("scheme", "http")
    scheme = {"ws": "http", "wss": "https"}.get(raw, raw)
    host = _normalize_netloc(scheme, _header(scope, b"host") or "")
    verified = scope.get(_CLIENT_TRUST_KEY, True)
    peer = scope.get("client")
    client = (peer[0] if peer else "unknown") if verified else CLIENT_UNVERIFIED
    return ExternalRequest(scheme, host, f"{scheme}://{host}", client, verified)


def same_origin(request_or_scope) -> bool:  # noqa: ANN001
    """The same-origin rule, in one place. No ``Origin`` header: allowed (the
    CSRF tokens still apply). Otherwise exactly one Origin, equal to the
    EXTERNAL origin; ``null``, several values, a path, credentials or another
    scheme/host/port are refused."""
    scope = getattr(request_or_scope, "scope", request_or_scope)
    values = [v.decode("latin-1") for k, v in scope.get("headers") or () if k.lower() == b"origin"]
    if not values:
        return True
    if len(values) != 1:
        return False
    match = _ORIGIN_RE.match(values[0].strip())
    if match is None:
        return False
    origin = f"{match.group(1)}://{_normalize_netloc(match.group(1), match.group(2))}"
    return origin == external_request(scope).origin


def install(app, settings: HttpSettings) -> None:  # noqa: ANN001
    """Add the middleware to a FastAPI/Starlette app. The mode middleware is
    outermost, so the Host check and the application see the external
    scheme (and, behind the local proxy, the real client address).

    - loopback-proxy: TrustedProxyMiddleware (Phase 10, unchanged)
    - local: nothing is taken from forwarded headers
    - cloud-run: CloudRunMiddleware (https scheme, unverified client)"""
    if settings.host_check:
        app.add_middleware(HostCheckMiddleware, settings=settings)
    if settings.mode is HttpMode.CLOUD_RUN:
        app.add_middleware(CloudRunMiddleware, settings=settings)
    elif settings.mode is HttpMode.LOOPBACK_PROXY:
        app.add_middleware(TrustedProxyMiddleware, settings=settings)
