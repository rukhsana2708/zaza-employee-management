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
from ..repository import DeviceRecord, RepositoryUnavailable
from ..service import SyncService
from .connections import TenantRepositoryManager
from .context import TenantContext
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
                 manager: TenantRepositoryManager) -> None:
        self.router = router
        self.resolver = resolver
        self.manager = manager

    @contextmanager
    def access(self, authorization: str | None, device_header: str | None) -> Iterator[AuthenticatedDevice]:
        token = _bearer(authorization, device_header)
        try:
            context = self.resolver.for_device_route(self.router, token)
        except TenantAccessDenied:
            raise AuthError(401, "invalid credentials") from None
        with self.manager.repository(context) as repo:
            # the route only said WHERE to look; the tenant database decides
            auth = authenticate_request(repo, authorization, device_header)
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
                 routes, auth) -> None:  # noqa: ANN001 — PostgresDeviceRoutes-like, SaasAuthService
        self.control = control
        self.resolver = resolver
        self.manager = manager
        self.routes = routes
        self.auth = auth
        self.devices = SaasDeviceAuthenticator(routes, resolver, manager)

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
            return cls(control=control, resolver=TenantResolver(control), manager=manager,
                       routes=PostgresDeviceRoutes(control),
                       auth=SaasAuthService(PostgresAuthStore(control), saas_auth_settings_from_env(env)))
        except BaseException:
            control.close()
            raise

    def close(self) -> None:
        try:
            self.manager.close()
        finally:
            self.control.close()
