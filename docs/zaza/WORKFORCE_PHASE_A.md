# Phase A - workforce identity and authorization foundation

Status: IMPLEMENTED locally for review; NOT deployed or migrated in production.
Production state remains the separately approved shadow-observation baseline in DEVELOPMENT_STATUS.md.

## Inspection and deliberate changes

Before implementation, Alembic reported tenant `0004_manager_dashboard_auth` and control
`c0004_sync_rate_limits`. Employees contain immutable application identity, display name,
legacy EMPLOYEE/MANAGER/ADMIN classification, is_active, timezone and timestamps. Memberships
are control-owned and distinct from employees. Session validation and TenantResolver check
active user, membership, organization and READY tenant. Device credentials route through
control metadata and authenticate inside the tenant; they do not imply an employee-work grant.
The employee operator's activation change does not revoke device tokens. Attendance remains
Phase 5. Tenant audit previously allowed SYSTEM/CLI/ADMIN/MANAGER actors and five entity types.
OWNER previously used frozenset(Permission), inadvertently granting future enum additions.

Phase A adds tenant `0005_workforce_foundation` and control `c0005_employee_membership`.
No historical migration file is edited. No web endpoints, agent changes or new operator commands
are added. The existing saas-membership-add parser accepts EMPLOYEE. Standalone PostgreSQL
application grants list the new tables explicitly; the existing report role is not expanded. The public Python service is the reusable authorization boundary for later interfaces.

## IMPLEMENTED

- Tenant-owned designation, department and team catalogs: UUID, display name, archive state,
  created/updated timestamps and optimistic version. Names are case-insensitively unique per tenant;
  archival keeps references/history. No customer-specific entries are seeded.
- One employee_profiles extension per employee, automatically created for legacy employee inserts.
  Optional designation/department, employment lifecycle, explicit monitoring policy, timestamps/version.
  Team membership is many-to-many with ended history. The existing EmployeeRecord is unchanged;
  WorkforceService.profile returns the extended profile and current team IDs.
- Existing is_active maps only to initial ACTIVE/INACTIVE; monitoring defaults true to preserve
  existing monitoring. Names/roles are never used to infer links, designation or hierarchy.
- Explicit account links reference control user AND exact organization membership. Active employee,
  membership and user uniqueness are enforced in PostgreSQL. Replacement revokes and inserts in one
  transaction, rolling everything back on conflict. Revocation history is retained.
- Effective-dated reporting: one primary manager at a time; same-tenant foreign keys, no self link,
  no temporal overlap and no temporally intersecting cycles, including future scheduled cycles.
  Administrative writes serialize with a tenant-local transaction advisory lock, also taken by the
  graph validation trigger. Team/department membership grants no report access. No nested-report scope.
- Explicit EMPLOYEE membership role in domain and control CHECK; existing control membership operations
  continue writing control audit. No workforce data is copied to the control database.
- New employees.view_self and employees.manage capabilities. Every role preset enumerates its grants;
  adding an enum value does not grant it automatically. Existing intended organization-wide reads remain.
- Self, direct-report and organization SubjectScope values. A manager needs BOTH scoped capability and
  current direct relationship. Historical bounds start at the CURRENT relationship's effective_from
  and end at evaluation time; prior reporting episodes do not grant unlimited history. Consumers MUST
  clip all monitoring queries/aggregates through SubjectScope.clip. No history endpoints are added here. Scopes are request-local decisions, not durable grants;
  later consumers must reevaluate authorization on every request and job execution.
- Each service operation requires a real resolver-issued MEMBER context, refreshes current authorization
  (including after acquiring the tenant write lock), and leases the resolved tenant repository.
- Mutations and tenant audit commit together. SAAS_USER actor and specific workforce entity types are
  added; append-only audit history remains. Logs receive no new employee/org identifiers or secrets.

## Presets

| Role | Explicit grants |
| --- | --- |
| OWNER | org.view; employees.view_all/view_scoped/view_self; employees.manage; devices.manage; members.manage; org.settings.manage |
| ADMIN | org.view; employees.view_all/view_self; employees.manage; devices.manage; members.manage |
| MANAGER | org.view; employees.view_scoped/view_self |
| EMPLOYEE | org.view; employees.view_self |
| VIEWER | org.view |

Self and report access require valid employee linkage. Organization-wide readers/admins need no employee
linkage. Link/revoke additionally requires members.manage. No numeric role hierarchy is used.
A current capability is not an entitlement or a promise of an implemented dashboard.

## Lifecycle and monitoring independence

- ACTIVE permits linked self reads and future employee-work authorization.
- LEAVE permits self reads but refuses the future work-mutation gate; report management requires ACTIVE.
- INACTIVE/TERMINATED refuse employee self/work access. Legacy is_active=false also refuses it.
- Organization-wide authorized administration/history inspection remains possible for offboarding.
- Revoked link, disabled user/membership, suspended organization or non-READY tenant deny access on the
  next evaluation. A previously issued context cannot retain former role or reporting access.
- The future work gate additionally requires the subject to be the actor's linked ACTIVE employee.
- Employment state does not rewrite legacy is_active, schedules, attendance, devices or telemetry.
- monitoring_enabled is independent configuration/intent. It grants no SaaS authority and does not change
  existing device authentication or remotely stop an existing agent in Phase A. Operational collection
  changes require the later reviewed device/offboarding workflow; the flag alone is not proof collection stopped.
- Manager, admin and owner roles never exempt employees from monitoring. A non-employee admin is valid.

Control/tenant checks are request-boundary checks, not a distributed transaction across databases.
There is no cross-database foreign key or instant cancellation of an operation already authorized/in flight.
Future long-running work must reauthorize at execution/delivery boundaries.

## Schema compatibility and rollback

Exact operator SchemaStatus.up_to_date is unchanged. No lexical, ancestral or transitive acceptance.

| Application | Tenant | Control | Result |
| --- | --- | --- | --- |
| Existing production code | 0004 | c0004 | Existing supported baseline |
| Existing production code | 0005 or c0005 applied to respective DB | new respective head | Old exact-head startup refuses; do not migrate first |
| Phase A monitoring runtime | 0004 | c0004 | Explicitly supported, all existing limiter modes |
| Phase A monitoring runtime | 0005 | c0005 | Supported |
| Phase A workforce service | 0004 or control c0004 | either pending | Refused, no partial feature availability |
| Phase A workforce service | 0005 | c0005 | Supported |
| Phase A monitoring runtime | 0004/0005 | c0003 | Only limiter off, preserving explicit Sequence B policy |
| Phase A runtime | older/unknown/future tenant or control revision | any | Refused except the explicit cases above |

Tenant monitoring compatibility is opt-in in open_tenant_repository; standalone/operator repositories
stay strict. The control runtime reads c0005's explicit predecessor declaration. Unknown declared
heads are not used to infer compatibility. No schema change is performed at startup.

A future production rollout requires separate approval: first deploy compatible code against old schemas,
then reviewed control/tenant migrations, then enable workforce interfaces. This task performs none of those.
The new compatible application must stay serving if rollback is assessed. Never deploy the old exact-head
application over a new schema. An empty tenant expansion may downgrade; once extension data/customized
profiles exist, downgrade refuses rather than deleting customer data. Expanded audit constraints/history
are retained. Control downgrade refuses while EMPLOYEE memberships exist; it never reclassifies them.
After feature use, prefer roll-forward or a separately reviewed archival/restore procedure. No automatic rollback.

## Legacy employee role deprecation

The tenant employees.role field retains its original values and semantics, separate from MembershipRole.
No automatic conversion is performed, even where the strings coincide. Future deprecation requires an
inventory of callers/exports, explicit business metadata migration, a compatibility window, and later removal
only after old clients/operators cease reading it. SaaS authorization never consults it.

## Future Extensibility and Module Boundaries

Use a modular monolith: identity/tenancy, workforce, monitoring, attendance, tasks/projects, task sessions,
communication, notifications, reports, devices and integrations own their domain writes. Stable IDs are reused;
extension tables, versioned contracts and explicit reviewed permissions add modules without redesigning core
identity, employee, tenancy or evidence foundations. Routes contain no SQL; server services own business rules.
Future entitlements use a central organization feature/limit policy separate from user authorization; no billing
or plan checks are implemented. A tenant-local outbox can support delivery/integrations without a distributed
platform. Provider mappings stay outside core tables. AI remains permission-filtered derived interpretation,
never attendance/evidence/identity/permission/task-completion authority. Existing monitoring APIs remain stable;
new work APIs/commands will be versioned independently, with old-agent compatibility tests. No plugin runtime,
generic workflow engine or speculative microservices are introduced.

## DESIGNED FOR FUTURE PHASE; NOT IMPLEMENTED

B read-only SaaS workforce dashboard; C employee workspace; D tasks/projects/progress/comments;
E task sessions; F basic direct manager-employee messaging (required V1); G desktop work UI/offline protocol;
H idle declarations with optional authorized task/project references; I integrated Live Work;
J per-organization Sheets and Excel/CSV reporting; K commercial onboarding/administration.
Entitlements/billing, external integrations and AI are also future work.

Tenant PostgreSQL stays authoritative; SaaS dashboard is the intended operational interface; Sheets/Excel/CSV
are optional one-way scoped reports. Existing five Sheets concepts remain supported; sheet edits never update
ZaZa. Communication is plain-text tenant-local V1, not a Slack replacement. Observed IDLE remains IDLE after
any future declaration. Attendance remains deterministic and separate; no productivity score is introduced.

## Validation

Final validation: targeted 203 passed; complete PostgreSQL-enabled ZaZa suite 1791 passed, 12 skipped.
The 55 new tests include 28 real PostgreSQL cases. Two existing dependency deprecation warnings remain.
Syntax/diff checks pass; no new lint findings (seven pre-existing findings left unchanged). Tests use disposable PostgreSQL 16, real tenant databases,
existing monitoring clients, transactional migration failures, duplicate links, scope/date denial, lifecycle,
role independence, audit rollback and downgrade refusal. No production database or agent is exercised.

## Roadmap update after Phase A

Phase B reporting and Phase C My Work are now implemented locally (not deployed); their implementation
records supersede the earlier future-phase labels for those delivered read-only surfaces. Phase D must
include first-class Files / Deliverables alongside Tasks/Projects: see FILES_DELIVERABLES_DESIGN.md.
This requirement is design-only, with no upload/schema/storage implementation in Phase C. Monitoring,
task timer selection, declared progress, delivered outputs and manager review remain separate evidence.
