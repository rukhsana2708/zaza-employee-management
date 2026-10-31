# Phase B - read-only tenant-aware SaaS workforce dashboard

Status: local code/test implementation for review. NOT built/deployed/migrated in production.
Phase A is approved locally. Tenant head remains `0005_workforce_foundation`; control head remains
`c0005_employee_membership`. No migration was created or historical migration changed.

## Browser page inventory

| Method | Path | Purpose |
| --- | --- | --- |
| GET, POST | /app/sign-in | HTML sign-in, generic failure, successful 303 to organization selector |
| GET | /app/organizations | Only organizations resolvable by the current active membership |
| GET | /app/orgs/{slug}/workspace | Authorized overview; org-context-only landing for VIEWER |
| GET | /app/orgs/{slug}/workforce | Authorized profiles/current status |
| GET | /app/orgs/{slug}/workforce/{employee_id} | Authorized profile, schedule, history, applications, clipped activity |
| GET | /app/orgs/{slug}/attendance | Stored deterministic daily results, quality and late/early/overtime |
| GET | /app/orgs/{slug}/applications | Authorized application summaries |
| GET | /app/orgs/{slug}/analytics | Accessible SVG charts with identical source data in Show the numbers tables |
| POST | /app/sign-out | Same-origin + session CSRF; revoke existing SaaS session |

The selector remains visible even for a single organization. Navigation exposes only implemented reports,
organization switching and sign-out. Slugs are handles, never database routing authority. There are no
workforce write forms/endpoints, tasks, messages, administration pages or agent features.

Existing JSON /app/login, /app/session, /app/logout, /app/orgs/{slug}, and employees endpoints retain their
contracts. /manager is not mounted by the SaaS application. The legacy local dashboard remains separate.

## Authentication and presentation reuse

SaasWebAccess shares the existing SaasAuthService, Argon2id, control password/session stores, shared login
limiter, same-origin helper and form-size validation between JSON and HTML. There is no JWT, second
password/session system or use of tenant manager_users/manager_sessions for SaaS identity.
HTML login uses a separate HttpOnly pre-session double-submit cookie scoped to /app/sign-in so it does
not interfere with /app/login's existing cookie. Both use the SAME login service and limiter. Session
cookie settings/path remain unchanged: HttpOnly, SameSite=Strict and Secure according to existing settings.
Browser responses, errors, redirects and assets under /app receive no-store/CSP/nosniff/frame/referrer headers.
Jinja autoescape/StrictUndefined is retained. No external assets, scripts, fonts, frontend build chain or CDN.

Legacy display helpers/template environment moved unchanged to dashboard/presentation.py, with historical
routes reexports retained. SaaS reuses CSS, safe macros, SVG rendering, DashboardService period selection,
current-status logic, deterministic summary conversion/totals and selected Analysis chart builders.
It does not mount legacy routes, instantiate legacy authentication or call legacy unscoped repositories.
Local templates/assets are explicitly included in package data.

## Authorization and query boundary

SaaS session -> user -> current membership -> TenantResolver -> WorkforceService.dashboard -> authorized
subject set -> read-only tenant repository/query adapter -> reporting service -> escaped HTML.

AuthorizedWorkforceScope contains request-local SubjectScope decisions derived from Phase A. Only
organization-wide readers enumerate all employee IDs. Others derive their explicit account link and
current direct-report IDs, then evaluate the existing subject authorization rule. An unauthorized detail ID
is refused before any dashboard profile/activity query. No company-wide employee list is fetched for a manager.

| Role | Available workforce data |
| --- | --- |
| OWNER / ADMIN | Organization-wide according to explicit capability, no employee link required |
| MANAGER | Linked self plus current permitted direct reports; no descendants/department/team expansion |
| EMPLOYEE | Linked self only |
| VIEWER | Organization context only; workforce/attendance/app/analytics denied |

A monitored manager/owner remains an employee. Business titles, legacy classification and team/department
membership grant nothing. A manager on LEAVE retains self reads but not report access. Disabled employee,
revoked link, disabled user/membership, suspended organization and non-READY tenant fail applicable checks.
A manager's own access survives reporting changes. Current reporting-manager name is contextual profile
metadata, not permission to inspect that manager's monitoring.

Dashboard reads acquire a tenant-local shared advisory lock BEFORE opening a repeatable-read READ ONLY
transaction. This orders snapshots after completed Phase A scope mutations, which use the exclusive lock.
The lock is released on success/error; a failed unlock closes the connection rather than leaking a pooled lock.
Every dashboard workforce query joins a parameterized authorized-subject CTE. Filters, dropdowns, counts,
last-refresh timestamps, device status, profile labels, activity, aggregates and charts share that boundary.
Charts/tables read the same snapshot. No business mutation is permitted by the read transaction.
Control-session touch/login/logout housekeeping remains allowed. Cross-database checks are not a distributed
transaction or cancellation of in-flight requests; the next request revalidates current authority.

## Exact historical policy

Self and explicit organization scopes retain Phase A's unrestricted historical authorization.
Direct-report scopes use the CURRENT relationship's start through evaluation time. Former relationships
provide no ongoing scope; previous reporting episodes do not unlock earlier history.

- Activity periods must overlap both authorization and requested windows. SQL clips start/end and derives
  duration from that clipped interval. Pre-authorization periods and original earlier timestamps are not returned.
- Last-seen values outside the authorized range are masked. Latest-period reads are scoped before status logic.
- Daily summaries must have their COMPLETE employee-local calendar day, stored window_start/window_end,
  and any scheduled_start/scheduled_end within the authorization window. This additionally protects overnight
  shifts and attribution margins. Boundary buckets are omitted; attendance is never prorated/recalculated.
- Application rollups require full stored day_start_utc/day_end_utc coverage. A DST day is its real interval,
  not an assumed 24 hours.
- Stored weekly/monthly aggregate tables are not queried. Selected-period totals sum eligible stored daily
  summaries and identify the number of included rows. They do not claim to be an unrestricted full-month total.
- Daily trend shows at most the latest 62 available complete-summary dates. Omitted dates are not zero.
  Other charts cover eligible data in the selected period; legacy weekly condensation is not used.

The UI states the access limits with employee-local timestamps/timezones and explains omitted boundary days.
Current schedule configuration is shown read-only; it does not expose a schedule-history editor or change
attendance. Existing employee-local periods, 366-day custom limit, credit/basis ratio, quality flags, incomplete
vs absent, late/early/overtime and uncertainty semantics are preserved.

## Privacy and safety

No new application logger emits employee/device/org IDs, credentials or public IPs. The existing
lazy PostgreSQL connection INFO log now uses the shared safe db_ref helper instead of a database name. Templates do not render
control linkage IDs, organization UUIDs, database names or connection metadata. Necessary employee identifiers
appear only in authorized navigation/filter parameters, not new application logging. Provider access-log paths
remain distinct from application messages. User content is escaped, including HTML/SVG and form attributes.

Read-only pages do not touch device connectivity, monitoring state, schedules, roles, employment policy or
attendance calculations. Monitoring evidence, work declarations, task state and attendance remain separate.
No productivity score, Actual Work Time, task-completion inference or communication-performance metric.
Tenant PostgreSQL remains authoritative. Existing Sheets reporting is untouched; expansion stays Phase J.

## Local validation and limitations

Final targeted suite: 164 passed. Logging/privacy/PostgreSQL-unit checks: 37 passed; strengthened
lazy-pool privacy check also passed. Complete PostgreSQL-enabled ZaZa suite: 1821 passed, 12 skipped,
with two existing dependency deprecation warnings. No new lint findings; three existing findings retained.
Desktop/narrow-layout captures of synthetic local pages were reviewed; header-link contrast was improved. Browser tests use synthetic users/data in disposable
PostgreSQL 16, real separate tenant databases and real shared authentication. Coverage includes identical IDs
and names, cross-tenant links, mid-day boundaries, DST rollups, read-only rollback/lock cleanup, current-status
states, role/lifecycle revocation, expired/revoked sessions, CSRF/origin, cookies, headers, shared API/browser
rate limiting, XSS and privacy logs. Legacy dashboard and complete PostgreSQL-enabled ZaZa regressions run.
Synthetic rendered HTML inventory is generated under ignored .pytest_cache/phase-b-pages; it is not production data.

Manual refresh only; no push/live task state. No new exports, administrative writes, pagination, device-work grants,
agent changes or Phase C task workspace. Existing activity-row limits apply. No production readiness/deployment
approval is inferred from passing local tests. Browser pages require the local Phase A schemas and refuse
workforce reads while migration is pending; existing compatible monitoring remains independently supported.

Production remains zaza-server-00006-7tw / privacy-hotfix image, control c0004, device/IP shadow, HSTS 0,
Cloud Armor disabled. No production image, migration, Terraform, Cloud Run, DNS/Cloudflare, agent or enrollment
operation is performed. The separate shadow observation/review gate remains authoritative.
