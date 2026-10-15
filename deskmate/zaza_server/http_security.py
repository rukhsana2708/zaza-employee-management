"""Reverse-proxy and Host-header hardening for ``serve`` (Phase 10).

Production layout: the server listens on ``127.0.0.1:8100`` only, and Caddy
on the same machine terminates HTTPS and forwards requests to it.

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
                            ``127.0.0.1,::1``).
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass

from .config import ConfigError

ENV_PUBLIC_HOST = "ZAZA_PUBLIC_HOST"
ENV_TRUSTED_PROXIES = "ZAZA_TRUSTED_PROXY_IPS"
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

    def __post_init__(self) -> None:
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

    def trusted(self) -> frozenset[IPAddress]:
        return frozenset(ipaddress.ip_address(p) for p in self.trusted_proxies)


def http_settings_from_env(env: dict | None = None) -> HttpSettings:
    env = dict(os.environ if env is None else env)
    hosts = tuple(h.strip().lower().rstrip(".") for h in (env.get(ENV_PUBLIC_HOST) or "").split(",") if h.strip())
    raw = (env.get(ENV_TRUSTED_PROXIES) or "").strip()
    proxies = tuple(p.strip() for p in raw.split(",") if p.strip()) if raw else DEFAULT_TRUSTED_PROXIES
    return HttpSettings(public_hosts=hosts, trusted_proxies=proxies)


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


def install(app, settings: HttpSettings) -> None:  # noqa: ANN001
    """Add the middleware to a FastAPI/Starlette app. The proxy middleware
    is outermost, so the Host check and the application see the real
    client address and scheme."""
    if settings.host_check:
        app.add_middleware(HostCheckMiddleware, settings=settings)
    app.add_middleware(TrustedProxyMiddleware, settings=settings)
