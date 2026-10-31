# Phase C - employee self-service / My Work

Status: local code/test implementation for review; NOT deployed. No production build, migration or change.
Schema heads remain tenant `0005_workforce_foundation` and control `c0005_employee_membership`.

## Implemented routes and presentation

GET /app/orgs/{slug}/my-work
GET /app/orgs/{slug}/my-work/activity
GET /app/orgs/{slug}/my-work/attendance
GET /app/orgs/{slug}/my-work/applications

No employee ID appears in these route definitions. My Work contains personal identity/profile, current
monitoring information, employee-local today/schedule/attendance/quality, recent activity and application
summaries. Dedicated personal templates replace manager-style presentation; no coworker dropdowns,
comparison/ranking, management controls, editing or upload placeholders. Shared Phase B reporting components,
formatting, filters, SQL adapter, status calculation and deterministic Phase 5 summaries are reused.

Today remains the employee's local today even when a historical period is selected for recent evidence.
Missing summaries are unavailable, not presumed absence/inactivity. No new calculation algorithm is added.

## Identity, authorization and navigation

Session -> active user -> active membership -> TenantResolver -> current explicit tenant account linkage ->
employee lifecycle -> one self SubjectScope -> Phase B SQL adapter inside the existing read-only snapshot.
WorkforceService.self_dashboard accepts NO employee selector. Broad OWNER/ADMIN employee-report authority
cannot bypass this gate or the employee lifecycle check. The reporting service admits only period/from/to
filters on personal pages; employee/employee_id/user/name query values are ignored and never echoed as
identity. No identity may be selected by name/email/title/legacy classification or a form.

Linked EMPLOYEE/MANAGER/ADMIN/OWNER with employees.view_self and ACTIVE legacy activation may use My Work
when employment is ACTIVE or LEAVE. LEAVE is read-only and explicitly labeled. INACTIVE/TERMINATED,
legacy activation false, revoked linkage or missing self capability deny self-service. Fresh user/membership,
organization and tenant checks remain in force. Membership in A does not supply a linkage in B; a user with
memberships and explicit links in both organizations resolves each link independently. A slug alone grants
nothing. No employee is automatically created for an unlinked administrator.

Navigation uses current self authorization and explicit management capabilities. Ordinary employees see
My Work/My Activity/My Attendance/My Applications; linked managers/admins/owners additionally retain
company sections. Unlinked owners/admins keep authorized company reporting but have no personal menu.
VIEWER gains no personal access merely from a link. Employee-only organization cards lead to My Work;
management cards retain the company overview. Ending report responsibilities/demoting a manager to EMPLOYEE
does not remove a still-valid self identity. Company history administration remains separate from self lifecycle.
All scope decisions are request-local; already-authorized in-flight requests are not distributed cancellation.

## Privacy, monitoring independence and browser security

Monitoring policy remains separate from SaaS role/linkage. Unmonitored employees can read their own records;
Monitoring not enabled is clearly labeled, with historical data retained and device-collection confirmation
referred to administrators. The policy flag alone does not remotely stop an agent. No role exempts monitoring.

The transparency component describes current computer states, privacy-filtered app/window metadata,
work-session/attendance summaries and device connectivity. It states no screenshots, screen/OCR contents,
typed key contents, clipboard, webcam/video or microphone/audio. It is product information, not an employer
privacy notice or an unreviewed legal/compliance claim. Activity is not a productivity score or proof of work.

No device credentials/hash, database name, internal routing/account-link/membership IDs or coworker private
records are rendered. Existing SaaS sessions, Argon2id, shared login limits, Secure/HttpOnly/SameSite cookies,
CSRF/same-origin logout, CSP/no-store and Jinja escaping remain. Only GET personal routes are added; POSTs
are refused. Existing JSON /app endpoints, legacy /manager and Phase B SQL/history protections remain.

## Validation and limitations

Disposable PostgreSQL 16 validation: targeted suite **192 passed**; complete ZaZa suite
**1849 passed, 12 skipped**, with two dependency deprecation warnings. Includes 28 new Phase C cases.
Changed-source lint and whitespace checks passed. A synthetic My Work screenshot was reviewed locally
(.pytest_cache/phase-c-my-work.png); no production data was used. Tests cover all linked roles,
organization-wide-role lifecycle bypass attempts, no-link/viewer refusal, revocation/replacement after login,
query/form impersonation, identical IDs across tenants, independent multi-organization links, reporting-role
changes preserving self history, user/membership/org/tenant state, monitoring independence, local dates,
read-only SQL and privacy/browser controls. Synthetic personal HTML inventory is generated under ignored
.pytest_cache/phase-c-pages. Existing Phase B and full PostgreSQL-enabled suites run separately.

No tasks/projects/timers/comments/messages, agent UI, attendance editing/declarations, uploads, exports,
entitlements/billing, integrations or AI are implemented. No optional separate profile route: the overview
contains the personal profile. Existing activity limits/manual refresh apply. Future write operations must
use their stronger employee-work ACTIVE gate, not assume this read-only LEAVE authorization permits writes.

## Required future Phase D Files / Deliverables

[Files / Deliverables design](FILES_DELIVERABLES_DESIGN.md) is a mandatory future Tasks/Projects module:
requirements, submissions, immutable versions and version-bound manager review; tenant PostgreSQL metadata
and private object-storage binaries. It specifies tenant/participant/manager authorization, signed-link lifetime,
quarantine/malware scanning, type/size/filename/checksum checks, quotas/retention/deletion, audit and plan limits.
Standard Sheets/Excel exports contain authorized metadata only, not document content. AI requires explicit
authorized invocation and applicable policy; it is never automatic on upload. File content remains source
of truth and interpretation derived. None of Files / Deliverables is implemented or deployed in Phase C.

## Revocation timestamp robustness

Local PostgreSQL testing exposed backwards clock steps (approximately 0.14 and 1.02 seconds).
Link revocation now uses GREATEST(clock_timestamp(), created_at), preserving the existing timestamp
CHECK and link history while making logical revocation immediate (revoked_at is no longer NULL).
A deterministic future-creation-time regression verifies this without changing the schema or disabling
constraints. This does not redefine reporting dates or provide a global monotonic wall clock.
