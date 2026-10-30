"""SaaS device authentication and the SaaS runtime (Phase 10F-B).

Device request::

    Authorization: Bearer <token>, X-ZaZa-Device-Id
      -> routing_hash(token) -> control route -> organization_id      (routing only)
      -> TenantResolver: ACTIVE organization, READY tenant -> TenantContext(DEVICE)
      -> TenantRepositoryManager.repository(context)                  (lease held)
         -> auth.authenticate_request() INSIDE that tenant database   (the proof)
         -> touch the device, SyncService on THE SAME repository
      -> lease released

A client never names the organization or database. If the route is
missing or revoked, or the organization is not ACTIVE, or the tenant is not
READY, or the token is unknown, revoked or bound to another device inside
the tenant, the request is refused with the existing generic errors. It
never falls through to another tenant, and no error names an organization
or database.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from ..auth import AuthError, authenticate_request
from ..ratelimit import SyncRateLimiter
from ..repository import DeviceRecord, RepositoryUnavailable
from ..service import SyncService
from .connections import TenantRepositoryManager
from .context import TenantContext
from .device_routes import routing_hash
from .resolver import DeviceCredentialRouter, TenantAccessDenied, TenantResolver

logger = logging.getLogger("zaza_server.saas")


@dataclass(frozen=True)
class AuthenticatedDevice:
    """An authenticated device request: its tenant context, the device as
    stored IN that tenant, and a sync service bound to that tenant's leased
    repository (valid only inside :meth:`SaasDeviceAuthenticator.access`)."""

    context: TenantContext
    device: DeviceRecord
    token_id: str
    service: SyncService


def _bearer(authorization: str | None, device_header: str | None) -> str:
    """The same header rules and messages as ``auth.authenticate_request``."""
    if not authorization or not device_header:
        raise AuthError(401, "missing credentials")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError(401, "malformed authorization header")
    return token.strip()


class SaasDeviceAuthenticator:
    def __init__(self, router: DeviceCredentialRouter, resolver: TenantResolver,
                 manager: TenantRepositoryManager, limiter: SyncRateLimiter | None = None) -> None:
        self.router = router
        self.resolver = resolver
        self.manager = manager
        self.limiter = limiter

    @contextmanager
    def access(self, authorization: str | None, device_header: str | None,
               client_ip: str | None = None) -> Iterator[AuthenticatedDevice]:
        """``client_ip`` is the VERIFIED address (``http_security.trusted_client_ip``) or None; the per-IP limiters are
        skipped without it. Rate limiting (``limiter``) never changes who is authenticated: it only refuses earlier,
        with :class:`~..ratelimit.RateLimitExceeded`, and only in ``enforce`` mode."""
        limiter = self.limiter if self.limiter is not None and self.limiter.enabled else None
        try:
            token = _bearer(authorization, device_header)
        except AuthError:
            if limiter:
                limiter.gate(client_ip, None)
                limiter.auth_failed(client_ip)
            raise
        digest = routing_hash(token)
        if limiter:
            limiter.gate(client_ip, digest)
        try:
            context, route_device_id = self.resolver.for_device_route_info(self.router, token)
        except TenantAccessDenied:
            if limiter:
                limiter.auth_failed(client_ip)
            raise AuthError(401, "invalid credentials") from None
        if limiter:
            # before ANY tenant database is touched; the key is the server-validated route identity, never the IP
            limiter.admit_device(context.organization_id, route_device_id, digest, client_ip)
        with self.manager.repository(context) as repo:
            # the route only said WHERE to look; the tenant database decides
            try:
                auth = authenticate_request(repo, authorization, device_header)
            except AuthError:
                if limiter:
                    limiter.auth_failed(client_ip)
                raise
            if limiter:
                limiter.auth_succeeded(digest)
            try:
                # Re-enrollment: the first successful use of the NEW credential proves the device has it;
                # only then are the device's older credentials revoked (one transaction, retried if it fails).
                repo.confirm_token_use(auth.token_id)
            except RepositoryUnavailable:
                raise
            except Exception:  # noqa: BLE001 — never fail an authenticated request; reconcile() heals it
                logger.warning("could not confirm first use of a device credential")
            try:
                # API connectivity only — never treated as employee activity.
                repo.touch_device(auth.device.device_id, token_id=auth.token_id)
            except RepositoryUnavailable:
                raise
            except Exception:  # noqa: BLE001 — bookkeeping must not fail an authenticated request
                logger.warning("could not update last_seen_at for an authenticated device")
            yield AuthenticatedDevice(context, auth.device, auth.token_id, SyncService(repo))


class SaasRuntime:
    """Everything the SaaS app needs, built once per process and closed at
    shutdown. No module-level singletons."""

    def __init__(self, *, control, resolver: TenantResolver, manager: TenantRepositoryManager,  # noqa: ANN001
                 routes, auth, enrollment=None, limiter=None) -> None:  # noqa: ANN001 — routes, SaasAuthService, ...
        self.control = control
        self.enrollment = enrollment
        self.resolver = resolver
        self.manager = manager
        self.routes = routes
        self.auth = auth
        self.limiter = limiter
        self.devices = SaasDeviceAuthenticator(routes, resolver, manager, limiter)

    @classmethod
    def from_env(cls, env: dict | None = None) -> SaasRuntime:
        from ..config import database_settings_from_env  # noqa: PLC0415
        from .config import control_database_settings_from_env  # noqa: PLC0415
        from .connections import tenant_pool_limits_from_env  # noqa: PLC0415
        from .control_repository import PostgresControlRepository  # noqa: PLC0415
        from .device_routes import PostgresDeviceRoutes  # noqa: PLC0415
        from .saas_auth import PostgresAuthStore, SaasAuthService, saas_auth_settings_from_env  # noqa: PLC0415

        env = dict(os.environ if env is None else env)
        control = PostgresControlRepository(control_database_settings_from_env(env), actor_type="SYSTEM",
                                            actor_id="saas")
        try:
            manager = TenantRepositoryManager(database_settings_from_env(env), tenant_pool_limits_from_env(env))
            manager.start_reaper()
            from .enrollment import EnrollmentService, PostgresEnrollmentStore, enrollment_settings_from_env  # noqa: PLC0415

            from ..ratelimit import SyncRateLimiter, sync_limit_settings_from_env  # noqa: PLC0415

            resolver, store = TenantResolver(control), PostgresAuthStore(control)
            return cls(control=control, resolver=resolver, manager=manager, routes=PostgresDeviceRoutes(control),
                       auth=SaasAuthService(store, saas_auth_settings_from_env(env)),
                       enrollment=EnrollmentService(PostgresEnrollmentStore(control), resolver, manager, store,
                                                    enrollment_settings_from_env(env)),
                       limiter=SyncRateLimiter(sync_limit_settings_from_env(env), store))
        except BaseException:
            control.close()
            raise

    def close(self) -> None:
        try:
            self.manager.close()
        finally:
            self.control.close()
