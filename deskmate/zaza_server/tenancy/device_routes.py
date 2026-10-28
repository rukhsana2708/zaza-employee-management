"""Device credential routing (Phase 10F-B): which ORGANIZATION must verify
a device credential, known before any tenant database is opened.

    Bearer token --routing_hash()--> device_credential_routes --> organization_id

- The control database stores ``routing_hash(token)``: SHA-256 over a fixed
  domain prefix and the token. It is NOT the hash the tenant database keeps
  (``auth.hash_token``), and the raw token is never stored, so control-plane
  data alone can't be matched against, or replayed into, a tenant database.
- A route is NOT authentication. It only says where to look. The tenant
  database must still find the token hash, match it to ``X-ZaZa-Device-Id``,
  and see an ACTIVE token and an ACTIVE device (``auth.authenticate_request``).
  A stale or forged route therefore authenticates nothing.
- One hash routes to exactly one organization (primary key). Only ACTIVE
  routes are followed.
- Lifecycle for the enrollment phase (no public endpoint yet): create a
  route when a token is issued, revoke on revocation. Rotation is: issue the
  new tenant token, create its route, switch the agent, then revoke the old
  token and route.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from . import domain
from .domain import TenancyError

_ROUTE_DOMAIN = b"zaza-device-route-v1\x00"


def routing_hash(token: str) -> str:
    if not isinstance(token, str) or not token.strip():
        raise TenancyError("a device credential is required")
    return hashlib.sha256(_ROUTE_DOMAIN + token.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeviceRoute:
    organization_id: uuid.UUID
    device_id: str
    status: str
    created_at: datetime
    revoked_at: datetime | None


class PostgresDeviceRoutes:
    """``DeviceCredentialRouter`` (see resolver.py) on the control database,
    plus the route lifecycle. Uses the control repository's pool and audit."""

    def __init__(self, control) -> None:  # noqa: ANN001 — PostgresControlRepository
        self.control = control

    def organization_for_credential(self, route_hash: str) -> uuid.UUID | None:
        with self.control.connection() as conn:
            row = conn.execute("SELECT organization_id FROM device_credential_routes "
                               "WHERE route_hash = %s AND status = 'ACTIVE'", (route_hash,)).fetchone()
        return row["organization_id"] if row else None

    def create_route(self, organization_id: object, device_id: str, token: str) -> str:
        """Route a NEW credential (the raw token exists only in memory at
        issue time) to its organization. Returns the routing hash."""
        org_id, digest = domain.organization_id(organization_id), routing_hash(token)
        with self.control.transaction() as cur:
            cur.execute("INSERT INTO device_credential_routes (route_hash, organization_id, device_id) "
                        "VALUES (%s, %s, %s)", (digest, org_id, device_id))
            self.control.write_audit(cur, "device_route.create", "device_route", device_id, org_id)
        return digest

    def revoke_route(self, route_hash: str) -> bool:
        with self.control.transaction() as cur:
            row = cur.execute("UPDATE device_credential_routes SET status = 'REVOKED', revoked_at = now() "
                              "WHERE route_hash = %s AND status = 'ACTIVE' RETURNING organization_id, device_id",
                              (route_hash,)).fetchone()
            if row:
                self.control.write_audit(cur, "device_route.revoke", "device_route", row["device_id"],
                                         row["organization_id"], {"routes_revoked": 1})
        return row is not None

    def revoke_device_routes(self, organization_id: object, device_id: str, *,
                             except_route_hash: str | None = None) -> int:
        """Revoke every active route of one device (e.g. after rotation, all
        but the new one)."""
        org_id = domain.organization_id(organization_id)
        with self.control.transaction() as cur:
            count = cur.execute(
                "UPDATE device_credential_routes SET status = 'REVOKED', revoked_at = now() "
                "WHERE organization_id = %s AND device_id = %s AND status = 'ACTIVE' "
                "AND route_hash IS DISTINCT FROM %s", (org_id, device_id, except_route_hash)).rowcount
            if count:
                self.control.write_audit(cur, "device_route.revoke", "device_route", device_id, org_id,
                                         {"routes_revoked": count})
        return count

    def list_routes(self, organization_id: object, device_id: str) -> list[DeviceRoute]:
        """Route metadata for operators (never the hashes)."""
        with self.control.connection() as conn:
            rows = conn.execute("SELECT organization_id, device_id, status, created_at, revoked_at "
                                "FROM device_credential_routes WHERE organization_id = %s AND device_id = %s "
                                "ORDER BY created_at", (domain.organization_id(organization_id), device_id)).fetchall()
        return [DeviceRoute(r["organization_id"], r["device_id"], r["status"], r["created_at"], r["revoked_at"])
                for r in rows]
