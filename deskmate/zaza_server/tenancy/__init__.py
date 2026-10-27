"""SaaS tenancy foundation (Phase 10C) — control plane + database-per-tenant.

- :mod:`.domain`              central validation (ids, slugs, statuses, SaaS
                              roles, tenant database names, audit actions)
- :mod:`.context`             TenantDatabaseDescriptor, TenantContext (issued
                              only by the resolver), tenant connection settings
- :mod:`.control_repository`  the control-plane store (no activity data, no
                              secrets) and its read interface ControlDirectory
- :mod:`.resolver`            trusted tenant resolution for managers, devices
                              and per-tenant jobs
- :mod:`.control`             the control-plane migration chain (separate from
                              the tenant schema 0001-0004)

Not wired into the running sync API or dashboard yet: today's deployment is
still one organization per database. See docs/zaza/SAAS_TENANCY.md.
"""
