"""Tenant-aware device administration for the trusted SaaS operator.

The same boundary as the employee operator (``employees.py``): the slug names
an organization, the control plane resolves it, ``TenantResolver.for_job``
issues the tenant context, and the operation runs inside that tenant's
database only. There is no database, host, URL or organization-id input and
no fallback to the base database, the control database or another tenant.

The legacy ``list-devices`` / ``disable-device`` / ``rotate-token`` commands
open the single base database and must never be used for a SaaS organization.

- ``list``: device metadata (never a token or a token hash).
- ``disable``: the device can no longer authenticate; its tokens are kept
  (revoke them explicitly) and it is never re-enabled by enrollment.
- ``revoke_tokens``: every ACTIVE credential of the device is revoked. The
  device then needs a re-enrollment code (``enrollment-code-create
  --reenroll``) to sync again.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..repository import DeviceRecord
from . import domain
from .domain import TenancyError
from .employees import SaasEmployeeOperator


@dataclass(frozen=True)
class DeviceView:
    device_id: str
    employee_id: str
    status: str
    display_name: str | None
    last_seen_at: str | None
    has_active_credential: bool


class SaasDeviceOperator(SaasEmployeeOperator):
    def list(self, slug: object, employee_id: object = None) -> list[DeviceView]:  # type: ignore[override]
        employee = domain.identifier(employee_id, "employee id") if employee_id else None

        def operation(repo):  # noqa: ANN001, ANN202
            return [DeviceView(d.device_id, d.employee_id, d.status, d.display_name, d.last_seen_at,
                               repo.has_active_token(d.device_id))
                    for d in repo.list_devices() if employee is None or d.employee_id == employee]

        return self._run(self.context(slug), operation)

    @staticmethod
    def _existing(repo, device_id: str) -> DeviceRecord:  # noqa: ANN001
        device = repo.get_device(device_id)
        if device is None:
            raise TenancyError("no device with that id in this organization")
        return device

    def disable(self, slug: object, device_id: object) -> DeviceRecord:
        device_id = domain.identifier(device_id, "device id")

        def operation(repo):  # noqa: ANN001, ANN202
            self._existing(repo, device_id)
            repo.set_device_status(device_id, "DISABLED")
            return repo.get_device(device_id)

        return self._run(self.context(slug), operation)

    def revoke_tokens(self, slug: object, device_id: object) -> int:
        device_id = domain.identifier(device_id, "device id")

        def operation(repo):  # noqa: ANN001, ANN202
            self._existing(repo, device_id)
            return repo.revoke_tokens(device_id)

        return self._run(self.context(slug), operation)
