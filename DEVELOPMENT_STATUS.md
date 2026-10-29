# ZaZa Employee Management System — Development Status

**Last updated:** 2026-10-29 (Phase 10G-E-A — tenant-aware SaaS employee operator, code only)

## Agent hardening — input-hook divergence (code only)

- `deskmate/zaza/os_input.py` reads `GetLastInputInfo` (seconds only, no content).
- If the hooks report silence while the OS saw recent input (a hook silently removed by
  `LowLevelHooksTimeout`), the agent reports `UNKNOWN / INPUT_HOOK_DIVERGENCE`, never IDLE.
- Wired into the installed runner; tests in `test_input_hook_divergence.py`.
- 10G-E-B: image `bc81ce5` built on Cloud Build (digest `sha256:dda9c500...0d53`); deploying it to
  the `zaza-migrate` job: DONE (execution zaza-migrate-nscq5, control-db-status OK at c0003). The user, membership and employee bootstrap is still pending.

## Phase 10H-A — public edge definition (code only; pending your review)

Terraform and static tests only. Nothing was planned, applied or deployed.

- `deploy/gcp/terraform/edge.tf`: global external Application Load Balancer,
  managed certificate, serverless NEG, Cloud Armor per-IP throttle, http to
  https redirect. Every resource is gated by `deploy_edge` (default false).
- With `deploy_edge`, Cloud Run ingress becomes `INTERNAL_LOAD_BALANCER` and the
  default run.app URL is disabled, so Cloud Armor cannot be bypassed. It
  requires `deploy_service` and `edge_domain`.
- Tests: `test_edge_terraform.py` (static; no Terraform binary here, so
  `terraform validate` and `plan` still need to run before any apply).
- 10G-E-A review: full suite 1345 passed, 173 skipped (PostgreSQL opt-in not set);
  one import-order lint fixed in its test file.

## Current phase: Phase 10G-E-A — tenant-aware SaaS employee operator (code only; pending your review)

Phase 10G-D is **approved**. 10G-E-A is **code + tests only**: no image was
built, `zaza-migrate` is unchanged, and production was not touched (still
1 organization, 1 READY tenant, 0 users, memberships, employees, devices,
codes and routes).

- **Why:** the legacy `add-employee` / `list-employees` open the single
  database of `database_settings_from_env()` (`open_repository`) and
  resolve no tenant. In the production job that database is `postgres`.
  **They must never be used for a SaaS organization.**
- **New operator commands** (`tenancy/employees.py`, details in
  [docs/zaza/ENROLLMENT.md §1a](docs/zaza/ENROLLMENT.md)):
  `saas-employee-create`, `saas-employee-list`, `saas-employee-disable`,
  `saas-employee-enable`, each with `--slug`.
  - Path: slug → control plane → `TenantResolver.for_job` → immutable
    `TenantContext` → `TenantRepositoryManager` → tenant repository
    (current schema, `current_database()` checked).
  - No database, host, URL or organization-id input. An unknown,
    suspended or closed organization, a missing, non-READY, out-of-date or
    unreachable tenant are refused, with no fallback to `postgres`,
    `zaza_control` or another tenant.
- **Validation:** tenancy domain rules (id format, printable display name,
  IANA timezone, employee role EMPLOYEE/MANAGER/ADMIN), then the
  repository and database constraints. The employee role is not a SaaS
  membership role.
- **Disable/enable:** a new `PostgresRepository.set_employee_active` on the
  existing `is_active` column (no schema change), idempotent, audited. It
  doesn't revoke existing devices or tokens.
- **Audit:** the tenant `audit_logs` records `employee.create` /
  `employee.disable` / `employee.enable` with actor `CLI` /
  `saas-operator` (the operator's tenant repository factory). The control
  plane stores no employee data.
- **Identity:** in production these run in `zaza-migrate` as
  `zaza_migrator` — acceptable for bootstrap only; never over HTTP.
- **Tests:** unit tests (`test_saas_employees.py`) plus real PostgreSQL 16
  (`test_saas_employees_postgres.py`):
  - the same `employee-1` in tenants A and B stays separate for create,
    list and disable;
  - every refusal case is covered;
  - the control and maintenance databases get zero employee data;
  - the legacy path is never called;
  - the full internal bootstrap sequence runs (organization, tenant, user
    without password, OWNER membership, employee).
- **Runbook:** RECOVERY_RUNBOOK.md corrected:
  - observed label behaviour: the backup restore had no labels, the PITR
    clone copied production labels; relabel with `gcloud beta sql
    instances patch … --update-labels`;
  - `--location asia-south1` guidance for on-demand backups;
  - the retained drill backup `1793272723324` and a review policy for
    on-demand backups.

## Earlier: Phase 10G-D — production backup and PITR restore drill (approved)

Phase 10G-C is **approved**. 10G-D is a disaster-recovery DRILL: **PASS**.
The source `zaza-prod-pg16` was never restored onto, overwritten,
restarted or reconfigured. The only production write was one on-demand
backup. Details and the procedure: [docs/zaza/RECOVERY_RUNBOOK.md](docs/zaza/RECOVERY_RUNBOOK.md).

- **On-demand backup:** ID `1793272723324` ("Phase 10G-D restore drill
  after first tenant"), SUCCESSFUL, 2026-10-29 11:18:43 → 11:20:14 UTC,
  taken after the tenant existed. It is the first verified restore-drill
  backup and is **kept**. It is stored in the `asia` multi-region (the
  default without `--location`).
- **Backup restore → NEW instance `zaza-restore-drill-20261029a`:** about 6
  minutes. The copy independently held `zaza_control` (c0003, 1
  organization `zaza-internal` ACTIVE, 1 tenant row READY) AND
  `zaza_t_7jihzm6q6wvdogkv535q45mhdy` (`0004_manager_dashboard_auth`, 15
  tenant tables, 0 business rows), with the roles, `zaza_migrator`
  ownership, ACL, `zaza_app` grants and default privileges intact. Deleted.
- **PITR → NEW instance `zaza-pitr-drill-20261029a`:** point
  `2026-10-29T10:30:00Z` (after the last 10G-C write at 09:53:31Z), inside
  the window 2026-10-27T20:25:09Z → 2026-10-29T11:44:51Z. Same checks:
  PASS. Deleted.
- **Source after the drill:** unchanged (c0003, 1 organization, 1 READY
  tenant, 0 users, memberships, employees, devices, tokens, codes and
  routes); backups (14), PITR (7 days) and deletion protection unchanged.
- **Instance-level only:** this proves whole-instance recovery. There is no
  Cloud SQL backup of a single tenant; single-tenant recovery is the
  documented temporary-instance + `pg_dump` path (runbook §B), not yet
  exercised.
- No cutover, no Terraform/Cloud Run/secret/DNS change; Terraform: no
  changes. 0 Cloud Run services, 1 job, no public URL, no user, employee,
  enrollment code or device credential.

## Earlier: Phase 10G-C — first internal production tenant (approved)

Phase 10G-C0 is **approved**. 10G-C is the FIRST deliberate production
tenant creation. Everything ran through the `zaza-migrate` operator job
(image `778488c`); no code, image, Terraform or role changed.

- **Organization:** slug `zaza-internal`, display name "ZaZa Internal
  Pilot", default timezone `Asia/Dhaka`, ACTIVE (organization id
  `08b9f8d9-0cce-47d3-8665-3ed63c08fe57`, recorded here for operators; it is never a routing input).
- **Tenant database:** `zaza_t_7jihzm6q6wvdogkv535q45mhdy`, generated by the server
  (`^zaza_t_[a-z2-7]{26}$`), region `asia-south1`, READY, schema
  `0004_manager_dashboard_auth` (the tenant head, current). It is the
  ONLY `zaza_t_*` database; no database named `zaza` exists.
- **Tenant contents:** the 15 tenant tables only (no control-plane
  tables, no extra schemas), every business table empty, all owned by
  `zaza_migrator`.
- **Access boundary (verified live):** PUBLIC has no database privilege
  and no CREATE on schema `public`; `zaza_app` has CONNECT + TEMPORARY,
  exactly SELECT/INSERT/UPDATE/DELETE on all tenant tables, USAGE+SELECT
  on the sequence, and no DDL: CREATE TABLE/SCHEMA/DATABASE/ROLE, ALTER,
  DROP, TRUNCATE and ownership changes were refused (rollback-safe
  probes, no data left). Default privileges give `zaza_app` the same DML
  on tables/sequences the migrator creates later. `zaza_control_app` has
  no access to the tenant.
- **Runtime path:** `TenantResolver.for_job` returned the organization and
  the generated database; `TenantRepositoryManager` opened it as
  `zaza_app` (`current_database()` = `zaza_t_7jihzm6q6wvdogkv535q45mhdy`, schema check passed) and
  closed every pool.
- **Idempotency:** a second `tenant-provision` returned the same READY
  database (no second database or directory row); `tenant-migrate
  --organization` chose the database from the control directory: "already
  at head". `enrollment-reconcile`: all 0.
- **Control plane:** 1 organization, 1 tenant directory row, 5 operator
  audit rows (`organization.create`, `tenant_database.assign`, status
  changes; metadata only).
- **Still nothing customer-facing:** 0 SaaS users, 0 memberships, 0
  employees, 0 devices, 0 device tokens, 0 enrollment codes, 0 device
  routes; no web service, no public URL, no load balancer or Cloud Armor,
  no customer or employee traffic.
- **Backups:** automated backups (latest successful), PITR (7-day logs) and
  deletion protection unchanged. Cloud SQL backup and restore are
  instance-level: this tenant has no separately restorable Cloud SQL
  backup. (The restore drill followed in 10G-D.)
- Terraform: tenant databases are application lifecycle, not Terraform
  resources; the final plan shows no changes.

## Earlier: Phase 10G-C0 — production tenant provisioning preflight (approved)

Phase 10G-B is **approved**, and its concurrent A/B tenant stress gate
passed 3/3 in one clean run (production code `778488c`, PostgreSQL 16).
10G-C0 changed **only the `zaza-migrate` job's environment**; no
application code, image, database, role or secret changed.

- **Root cause:** `tenant-provision` builds `PostgresDatabaseCreator` and
  `TenantMigrationRunner` from `database_settings_from_env()`. The
  creator's administrative connection (`database_exists`,
  `create_database`, `restrict_access`) uses that base database name, and
  without `ZAZA_DB_NAME` it falls back to `zaza`, a database production
  does not have.
- **Fix (Terraform, job only):** `ZAZA_DB_NAME = "postgres"` and
  `ZAZA_TENANT_RUNTIME_ROLE = var.db_app_user` (`zaza_app`, the approved
  runtime role, read only by `tenant-provision`). Plan: 0 add, 1 change
  (exactly those two variables), 0 destroy; the follow-up plan shows no
  changes. The web service configuration is unchanged.
- **Three kinds of database** in production:
  - `zaza_control`: the control plane;
  - `postgres`: the instance's maintenance database, used only as the
    migrator's connection target for instance-level operations
    (`pg_database`, `CREATE DATABASE`). It is not a tenant, is never
    migrated and holds no ZaZa data;
  - `zaza_t_*`: the tenant databases, named by the server and
    substituted for the base name in tenant migrations and at runtime.
- **Verified through the job:** `db-status` connected to `postgres` over
  the Cloud SQL socket as `zaza_migrator` (read only; it reports
  "(empty database)", and `migrate` was deliberately NOT run there).
  `control-db-status` still reports `zaza_control` at c0003, "Up to
  date". `postgres` still has 0 user objects.
- **Roles unchanged:** `zaza_migrator` LOGIN + CREATEDB only; `zaza_app`
  LOGIN only (no SUPERUSER/CREATEDB/CREATEROLE/BYPASSRLS, member of no
  role).
- **No tenant has been created yet:** 0 organizations, users,
  memberships, tenant directory rows, `zaza_t_*` databases, enrollment
  codes and device routes; no web service, no public URL, no load
  balancer or Cloud Armor.

## Earlier: Phase 10G-B — production enrollment control schema (approved)

Phase 10G-A and its durable-reconciliation repair (`778488c`) are
**approved**. 10G-B deployed the reviewed foundation to production. It
changed only the image, the migration job, the control schema and the
grants.

- **Image:** commit `778488cfd848bb6064231c787541b3454fbb417b`.
  - Built on Cloud Build as `zaza-build` (build
    `efe5ed6f-0a5e-4489-8f01-1cbc01779695`), using the private
    build-source staging directory.
  - Pushed as `asia-south1-docker.pkg.dev/zaza-employee-monitor/zaza/zaza-server@sha256:b7ad3a8923069c63a62cb6ee6839419c3c13fa7c540e7dcf6a480810537a3bc8`
    (tag: the full Git SHA).
  - Verified: linux/amd64, uid 10001, `/app` byte-identical to the commit
    (89 files), container checks 26/26.
  - It contains c0003 and the CLEANUP_PENDING repair, and no secrets,
    state or capture dependencies.
  - Before production was touched, the enrollment suites (47/47,
    including all 13 PostgreSQL tests) ran against **the image's own
    code** on a disposable PostgreSQL 16.
- **Migration job:** `zaza-migrate` now runs that digest. The Terraform
  plan was 0 add, 1 change (the job image only), 0 destroy, and the
  follow-up plan shows no changes.
- **Control schema:** `control-migrate` through the job,
  `c0002_saas_auth_routing → c0003_device_enrollment`. Checked against the
  live database:
  - the 16 c0003 constraints, the indexes, and the identity and
    terminal-state triggers, with the trigger body identical to the
    reviewed migration;
  - the audit and rate-limit vocabularies;
  - the status model is enforced **by the database itself**: of all 42
    transitions, exactly the 7 reviewed ones are allowed
    (ISSUED→CLAIMED/REVOKED/EXPIRED, CLAIMED→COMPLETED/CLEANUP_PENDING/
    FAILED, CLEANUP_PENDING→FAILED), and the trigger refuses the other 35.
- **c0003 runtime grants applied** (as `zaza_migrator`, the owner).
  `zaza_control_app` gets SELECT on `device_enrollment_codes`, UPDATE on
  exactly the 7 saga columns, and INSERT on `device_credential_routes`.
  - The runtime's exact SQL was proven permitted, rolled back.
  - 20 forbidden operations were proven refused: creating or deleting
    codes, deleting or updating routes, TRUNCATE/ALTER/DROP, CREATE
    TABLE/DATABASE/ROLE, and authority columns such as `code_hash`,
    `organization_id`, `employee_id`, `expires_at` and `revoked_at`.
  - `zaza_migrator` keeps LOGIN and CREATEDB only.
- **Operator path:** `enrollment-reconcile` ran once in production through
  the job: everything 0, exit 0. `check-config --cloud-run --connect`
  (with the web service's settings as execution-only overrides) reports
  "Configuration OK", control schema c0003.
- **Enrollment tables EMPTY; still nothing customer-facing:**
  - 0 organizations, users, memberships, tenant directory rows,
    `zaza_t_*` databases, enrollment codes, device routes, sessions and
    credentials;
  - no web service and no public URL;
  - the load balancer and Cloud Armor (ENROLLMENT.md §8) remain a later
    deployment phase.

## Earlier: Phase 10G-A — tenant provisioning and one-time device enrollment (approved)

Phase 10F-C is **approved**. 10G-A was **code and tests only**; production
was deployed in 10G-B (above).

Design: [docs/zaza/ENROLLMENT.md](docs/zaza/ENROLLMENT.md).

- **Control migration `c0003_device_enrollment`** (additive; c0001 and
  c0002 are pinned by hash):
  - `device_enrollment_codes` holds only a domain-separated hash of each
    code, never the code. A database trigger forbids reactivating a used,
    revoked, expired or failed code;
  - enrollment rate-limit buckets;
  - `enrollment.*` audit actions.
- **One-time enrollment codes:** 160 random bits (`ZZE-XXXX-…`), shown once,
  expiring, single-use and revocable. The code's server-side record
  decides the organization and employee; nothing the client sends does.
- **The enrollment saga** (claim → tenant device + token → completion):
  - the route is inserted in the same control transaction that marks the
    code COMPLETED, so an interruption leaves at most an unroutable token;
  - compensation revokes that token immediately, and `enrollment-reconcile`
    finishes anything left;
  - concurrent use of one code yields exactly one credential.
- **Repair before approval (durable reconciliation):** an interrupted
  enrollment used to be marked FAILED *before* its token was revoked, so a
  tenant outage at that moment left an ACTIVE, unroutable token that was
  never retried.
  - c0003 (still unapplied everywhere, so edited in place) adds the
    non-terminal state **`CLEANUP_PENDING`**:
    `CLAIMED → CLEANUP_PENDING → FAILED`. It can only move on to FAILED.
  - **FAILED now means the credential cleanup is complete:** the token was
    revoked, was already revoked, or was never issued.
  - `enrollment-reconcile` moves stale claims to CLEANUP_PENDING atomically
    and retries every cleanup-pending enrollment on every run, with no
    give-up limit. An unreachable tenant leaves it CLEANUP_PENDING.
  - **Compensation:** if revoking the token fails during compensation, the
    enrollment becomes CLEANUP_PENDING rather than FAILED. An ambiguous
    token write is treated as "possibly issued".
  - **Late enrollments:** an enrollment that is still running refuses to
    issue a token after half the reconcile delay.
  - **Device rows of interrupted enrollments:** they are kept. A plain code
    for the same employee recovers such a device once it has no ACTIVE
    token, and a device is never reassigned to another employee.
  - Proven in memory and on real PostgreSQL 16:
    - a real tenant outage (`ALLOW_CONNECTIONS false`) across three
      reconcile runs, finished by the fourth;
    - a route failure combined with a revocation failure;
    - completion racing reconciliation, in both orders.
- **`POST /api/v1/enroll`** (SaaS app only; not deployed): strict JSON, 4 KiB
  limit, shared per-code and global rate limits, token returned once.
- **Provisioning** is now repairable on retry: a separate idempotent
  `restrict_access` step runs on every attempt.
- **Operator commands:**
  - organizations, provisioning, membership: `saas-org-create`,
    `saas-org-status`, `tenant-provision`, `saas-membership-add`;
  - enrollment codes: `enrollment-code-create`, `-list`, `-revoke`;
  - `enrollment-reconcile`.
- **Before public traffic:** an external Application Load Balancer and
  Cloud Armor in front of `/app/login` and `/api/v1/enroll`, with Cloud Run
  ingress limited to internal and cloud load balancing (ENROLLMENT.md §8).

## Earlier: Phase 10F-C — production SaaS auth schema and privileges (approved)

Phase 10F-B is **approved**. In production, 10F-C did the following:
- **Image:** built the reviewed commit `1f63fb2` on Cloud Build as
  `zaza-build` (build `289ca24a-fda6-45da-960b-6c7229b65be4`) and pushed it
  by digest: `asia-south1-docker.pkg.dev/zaza-employee-monitor/zaza/zaza-server@sha256:b7ffd93a82c1ed61a5c3c9c8bb7487af03b7023f6d48953be21fcc13f665022f`. `/app` is byte-identical to the commit; it
  is linux/amd64, uid 10001, and passes 26/26 container checks.
- **Migration job:** `zaza-migrate` was updated to that digest. The
  Terraform change was the job image only, and the follow-up plan shows no
  changes.
- **Control schema:** migrated through the job, `c0001_control_plane →
  c0002_saas_auth_routing`. The four new tables exist with all 21
  constraints, the indexes and the triggers, and they are EMPTY.
- **Runtime grants (as `zaza_migrator`, the owner):** `zaza_control_app`
  gets SELECT/INSERT/UPDATE on `user_password_credentials`,
  SELECT/INSERT/UPDATE/DELETE on `user_sessions` and
  `login_attempt_counters`, and SELECT only on `device_credential_routes`.
  They are verified positively and negatively (60/60 checks); every test
  write was rolled back or deleted.
- **Preflight:** `check-config --cloud-run --connect` returns
  "Configuration OK" with no warnings (control schema current at c0002).
- **Still nothing customer-facing:** no web service, no public URL, no SaaS
  user, organization, membership, tenant database, device route, enrollment
  or traffic. Every production auth and routing table has 0 rows.

**Before ANY public login traffic, add edge protection (deployment blocker).**
The shared global login bucket protects expensive Argon2 work across all
instances. But if the login endpoint were public without edge filtering, an
attacker could deliberately exhaust that bucket and temporarily block new
sign-ins for everyone. Before customer traffic:
- put Cloud Armor (or an equivalent reviewed edge control) with rate-based
  rules in front of `/app/login`;
- tune `ZAZA_SAAS_LOGIN_MAX_ATTEMPTS_GLOBAL` and the per-identity limit from
  pilot measurements.

This doesn't block the current private phase, because no web service
exists.

## Earlier: Phase 10F-B — SaaS authentication and tenant sync routing (approved)

10F-B was **code and tests only**:
- the production control schema is still `c0001`; c0002 is NOT applied;
- no image was pushed and no web service exists;
- no tenant, SaaS user, device route or token exists in production.

Design: [docs/zaza/SAAS_AUTH.md](docs/zaza/SAAS_AUTH.md).

- **Control migration `c0002_saas_auth_routing`** (additive; c0001 pinned by
  hash) adds four tables:
  - `user_password_credentials`: Argon2id only;
  - `user_sessions`: SHA-256 of the token only;
  - `login_attempt_counters`: the shared limiter;
  - `device_credential_routes`: a domain-separated token hash →
    organization, never the raw token or the tenant hash.

  It also extends the audit vocabulary.
- **SaaS auth service** (`tenancy/saas_auth.py`):
  - Argon2id passwords, with a dummy check for unknown emails;
  - server-side sessions (cookie `zaza_session`: Secure, HttpOnly,
    SameSite=Strict, Path=/app);
  - a disabled user loses access at once; a password change revokes every
    session;
  - a SHARED PostgreSQL limiter per identity digest plus a global bucket
    (no IPs, no Redis), atomic across instances.

  SQL lives only in `PostgresAuthStore`.
- **Session API** `/app/login`, `/app/session`, `/app/logout`,
  `/app/orgs/{slug}`, `/app/orgs/{slug}/employees`:
  - double-submit login token, CSRF token and `http_security.same_origin`;
  - generic errors; an unknown slug and a missing membership give the same
    404;
  - no self-signup.
- **Authorization:** a session identifies a USER. Every organization goes
  through `TenantResolver.for_member`. Explicit permission sets per role:
  MANAGER has no company-wide employee access (team scopes later), and
  OWNER is not a platform admin. `employees.role` grants nothing in the
  SaaS.
- **Device routing:**
  - `create_saas_app(runtime)` (separate from the unchanged
    `create_app(repo)`): bearer credential → control route → organization →
    TenantContext(DEVICE) → leased tenant repository →
    `authenticate_request` INSIDE that tenant → touch + sync in the same
    tenant;
  - a route is not authentication;
  - nothing client-supplied selects a tenant.
- **Operator CLI:** `saas-user-create/-set-password/-disable/-enable/`
  `-revoke-sessions`, `saas-auth-cleanup` (hidden password prompt only).
  `serve` runs the SaaS app with `ZAZA_TENANCY_MODE=saas`, which
  `check-config --cloud-run` requires.
- **Tests** (details in the final report of this phase):
  - same ids in A/B;
  - the cross-tenant token and wrong-route matrix;
  - forged organization/database headers;
  - sessions, CSRF and origin;
  - the shared limiter across two instances;
  - multi-org membership; user/employee separation;
  - a concurrent A/B stress through real pools with eviction;
  - repeated isolation proofs.

**Next:** after review, apply c0002 in production through the migration
job, then the control runtime grants (SAAS_AUTH.md §9). Enrollment and the
web-service deployment follow.

## Earlier: Phase 10F-A — Cloud Run HTTP security foundation (approved)

Phases 10D, 10E-A, 10E-B and 10E-C are **approved**. 10F-A is **code only**.
The web service is NOT deployed (`deploy_service = false`); there is no
public URL, no tenant and no employee traffic. The one live change was
deleting the accidental `zaza-employee-monitor_cloudbuild` bucket from 10E-C.

**Summary:**
- **Explicit `ZAZA_HTTP_DEPLOYMENT_MODE`:**
  - `loopback-proxy` (default; Windows/Caddy, unchanged);
  - `local` (no forwarded headers trusted);
  - `cloud-run`.
- **Cloud Run mode:**
  - the external scheme is https by deployment;
  - every `X-Forwarded-*` / `Forwarded` header is ignored;
  - the client address is "unverified" and used for nothing
    security-relevant;
  - Host is validated against `ZAZA_PUBLIC_HOST` (Terraform: the run.app
    host); `serve` refuses Cloud Run mode without it or without Secure
    cookies.
- **One external-origin abstraction** (`http_security.external_request` /
  `same_origin`), used by the dashboard login and forms. CSRF tokens are
  unchanged.
- **The legacy manager dashboard is never mounted in Cloud Run mode**
  (explicit `dashboard_policy`); the SaaS auth phase replaces it.
- **Login rate limiting:** `RateLimiter` boundary; the in-memory limiter is
  documented as per process; the SaaS login needs shared state.
- **`check-config --cloud-run`** now requires the cloud-run HTTP mode, the
  public host (missing = deployment blocker) and the disabled dashboard.
- **Architecture guard:** the sync API is still single-database (one
  repository); tests forbid any client-supplied organization, tenant,
  database or slug routing. Tenant routing will be credential-derived:
  device credential → TenantResolver → TenantContext →
  TenantRepositoryManager.

**Next:** SaaS authentication and credential-derived tenant routing for the
sync API, before `deploy_service = true`.

## Earlier: Phase 10E-C — secure build pipeline, production image, migration job, control schema (approved)

**Phase 10E-C (2026-10-28): secure build, migration job, control schema:**
- **Build identity `zaza-build`** (Terraform `build.tf`):
  - project-level `roles/logging.logWriter`;
  - `artifactregistry.writer` on the `zaza` repository only;
  - `storage.objectViewer` on the private build-source bucket only;
  - no keys, no secrets, no Cloud SQL, no Terraform-state access.
- **Legacy build account hardened:** `roles/cloudbuild.builds.builder` was
  removed from the legacy Cloud Build service account. There were no
  triggers, and no build ever used that account.
- **Production image:** built on Cloud Build as `zaza-build` from a
  `git archive` of commit `7b7c223` (tag = full SHA), linux/amd64, uid 10001.
  `/app` is byte-identical to the commit. It is deployed **by digest**:
  `asia-south1-docker.pkg.dev/zaza-employee-monitor/zaza/zaza-server@sha256:318a9d19d385303fa11be95cb2e887d67643c1cde2fba8813756a7a544505482`.
- **Migration job `zaza-migrate`** (`deploy_migration_job = true`):
  - identity `zaza-migrate`;
  - the Cloud SQL socket and the migrator secret only;
  - no HTTP endpoint, no IAM bindings;
  - default arguments `control-db-status`.
- **Control schema:** `control-migrate` ran through the job (`(empty) →
  c0001_control_plane`). The runtime grants for `zaza_control_app` are
  applied (INSERT/UPDATE on 4 tables, INSERT on the audit log, SELECT; no
  DELETE). They were verified positively and negatively, and every test
  write was rolled back.
- **Preflight:** `check-config --cloud-run --connect` passed inside the job.
  The control schema is current; the warnings for the public host and the
  front-end proxy are expected.
- **Still NOT created:** no web service, no public URL, no tenant database
  (`zaza_t_*` = 0), no customer, no employee traffic.

**Next:** the Cloud Run web-service hardening, before `deploy_service =
true`:
- trusted proxy and HTTPS/origin reconstruction;
- dashboard mounting;
- distributed login rate limiting;
- the web preflight.

## Earlier: Phase 10E-B — core production infrastructure (approved)

**Core infrastructure created in Phase 10E-B (2026-10-28):**
- **Cloud SQL `zaza-prod-pg16`** (PostgreSQL 16, Enterprise, `db-custom-1-3840`,
  ZONAL in asia-south1-c, 20 GB SSD with auto-resize to 200 GB). It has
  connector enforcement REQUIRED, no authorized networks, deletion
  protection, 14 daily backups, 7-day PITR and `max_connections` 100.
- **The empty `zaza_control` database.**
- **Artifact Registry `zaza`** (immutable tags, empty).
- **Service accounts `zaza-run` and `zaza-migrate`**: `cloudsql.client`
  only, no keys.
- **Three Secret Manager secrets**, each with version 1.
- **Database roles `zaza_app`, `zaza_control_app` and `zaza_migrator`.**

Terraform state is in the private, versioned bucket
`zaza-employee-monitor-tfstate-e33ac2a6`.

**Still NOT created:** no Cloud Run service or job, no image pushed, no
tenant database, no customer, no employee traffic. The control schema is
not migrated yet.

**Roles and secrets:**
- `zaza_app` and `zaza_control_app`: LOGIN only, no SUPERUSER, CREATEDB or
  CREATEROLE.
- `zaza_migrator`: LOGIN and CREATEDB, no SUPERUSER or CREATEROLE.
- Passwords exist only in Secret Manager. They were never printed, and no
  plaintext reached SQL.
- There is no standing `postgres` admin password.

**Expected baseline cost:** about US$64/month before tax (Cloud SQL). A
billing budget with alerts is recommended; the Budget API is not enabled on
the project.

(The image, migration job and control migration followed in 10E-C, above.)

## Earlier: Phase 10D — production Cloud SQL and tenant connection foundation (approved)

Phases 10A, 10B and 10C are **approved**. Phase 10D prepares the code and
reproducible configuration for Cloud Run + Cloud SQL. **In 10D, nothing was created
in Google Cloud:**
- no Cloud SQL, no Cloud Run, no Artifact Registry;
- no secrets, no service accounts, no keys;
- no Terraform plan or apply, no deployment.

Design: [docs/zaza/CLOUD_SQL_PRODUCTION.md](docs/zaza/CLOUD_SQL_PRODUCTION.md).

### Phase 10D summary

- **Architecture:** one Cloud SQL for PostgreSQL **16** instance in
  `asia-south1`, holding `zaza_control` plus one server-named `zaza_t_…`
  database per customer. A placement key can move a large customer to a
  dedicated instance later; the authorization path is unchanged.
- **Connection model** (`deskmate/zaza_server/cloudsql.py`, `config.py`):
  - `ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME` and
    `ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME` give the Unix socket
    `/cloudsql/PROJECT:REGION:INSTANCE`; the value is strictly validated and
    cannot be combined with a host or URL;
  - every host value is validated;
  - TCP mode is unchanged for local use, tests and the Windows deployment.
- **Secrets:** `ZAZA_DB_PASSWORD_FILE` and `ZAZA_CONTROL_DB_PASSWORD_FILE`
  hold Secret Manager files. The log formatter now redacts both database
  passwords.
- **Tenant connection manager** (`tenancy/connections.py`):
  - takes only a `TenantContext` and reuses `PostgresRepository`;
  - lazy, thread-safe, pool min 0 / max 2 (hard limit 4), at most 8 cached
    tenant pools per instance (hard limit 64), LRU eviction, 300 s idle
    eviction plus an optional reaper;
  - fails fast when all pools are busy;
  - checks connections before use, verifies `current_database()`, retires
    only a failing tenant's pool, and closes everything on shutdown.
  - The control pool stays min 0 / max 2, now also with connection checks.
- **Connection budget:** `max_instances × (cache × pool + control 2 +
  server pool)`; the pilot is `3 × 20 = 60 ≤ 70 < max_connections 100`.
  `check-config --cloud-run` checks it.
- **Migrations:** `tenant-migrate --organization ID | --all-ready
  [--continue-on-error]` (`tenancy/tenant_migrations.py`):
  - sequential, reporting each tenant;
  - a failure marks that tenant ERROR;
  - never the control database;
  - never run by `serve`.
- **Provisioning boundary** (`tenancy/provisioning.py`): assign name →
  create database (closed to PUBLIC) → migrate → verify head → grant the
  runtime role DML → READY; any failure leaves ERROR. Nothing exposes it
  yet, and nothing calls a Google API.
- **Roles (designed):**
  - `zaza_app` and `zaza_control_app`: runtime, no SUPERUSER, CREATEDB or
    CREATEROLE;
  - `zaza_migrator`: CREATEDB, used by the migration job only;
  - `zaza_reporting`: optional, read-only.
- **`check-config --cloud-run [--connect]`:** a separate mode;
  `--production` (Windows) is unchanged.
- **Terraform** (`deploy/gcp/terraform/`):
  - Artifact Registry, service accounts, secret containers (no values),
    PostgreSQL 16 with backups (14), PITR (7 days) and deletion protection,
    connector-only access, `zaza_control`, and the Cloud Run service and
    migration job (gated off);
  - validated offline in 10D (applied later, in 10E); no state committed.
- **Local proof** (disposable PostgreSQL 16, WSL Docker): control + tenants
  A/B provisioned by the real provisioner, with a real runtime role (no
  CREATEDB/CREATEROLE) working through the real manager:
  - A reaches only A and B only B;
  - the same `employee-1` / `device-1` exist in both;
  - pools are reused; the cache bound evicts and closes;
  - an unavailable tenant A does not affect B;
  - DDL is refused to the runtime role;
  - after `close()` no runtime connections remain;
  - everything is dropped afterwards.
- **Container:**
  - the 10B checks still pass;
  - the image migrates over a `/cloudsql/<name>` socket via an explicit
    command;
  - `check-config --cloud-run --connect` passes inside it;
  - it serves health 200 with `--network none`, never auto-migrates and
    never logs the password.
- **Tests:**
  - new: 89 in `test_cloud_sql.py`, 41 in `test_tenant_pools.py`, and 3 in
    `test_tenant_pools_postgres.py`;
  - default: 1134 passed, 0 failed, 142 skipped;
  - PostgreSQL-enabled: 1275 passed, 0 failed, 1 skipped (live Google).
- **Not built (approved product direction, after the production
  foundation):** Live Team, teams/scopes, CRM clients/projects/tasks,
  idle-context prompts, Pause/Resume, My Day corrections, messaging,
  comments, uploads, submissions, AI suggestions. The privacy guarantees
  are unchanged.

### Next: Phase 10E (not started)

Create the real Google Cloud resources from the Terraform definition
(state bucket, plan review, apply), then:
- create the database roles, with passwords generated straight into
  Secret Manager;
- push the image by digest and run `control-migrate`;
- verify backups, PITR and deletion protection.

The Cloud Run proxy/origin items, SaaS login and enrollment remain before
any customer traffic.

## Earlier: Phase 10C — SaaS tenant / organization architecture (approved)

**Direction change (2026-10-27):** ZaZa is becoming a commercial
**multi-tenant SaaS on Google Cloud**: project `zaza-employee-monitor`,
region `asia-south1`. The single Windows VPS production deployment below is
abandoned and kept as historical reference; its files in `deploy/windows/`
are unchanged.

- **Customer-data isolation:** database-per-tenant, with a shared control
  plane. See [docs/zaza/SAAS_TENANCY.md](docs/zaza/SAAS_TENANCY.md).
- **Phase 10C is architecture and local foundation only.** Nothing is
  deployed:
  - no Google Cloud resources and **no Cloud SQL**;
  - **no customer data in Google Cloud**;
  - no billing, no SaaS login, no enrollment.
- **Approved:** Phase 10A and Phase 10B (the Cloud Run container of the
  central server, `deploy/gcp/`).

### Phase 10C summary

- **Control plane** (`deskmate/zaza_server/tenancy/`, its own database):
  - tables: `organizations`, `users` (SaaS logins, not employees),
    `organization_memberships` (OWNER/ADMIN/MANAGER/VIEWER),
    `tenant_databases` (organization → server-generated `zaza_t_…` name; no
    secrets), `control_audit_logs` (append-only);
  - a separate Alembic chain: head `c0001_control_plane`, version table
    `zaza_control_version`;
  - run with `control-migrate`, never at web start-up.
- **Tenant data plane:** the existing schema 0001–0004, unchanged (a test
  pins the file hashes), applied once per tenant database. There is no
  `organization_id` on activity tables.
- **Trusted resolution:** `TenantResolver` is the only issuer of the
  immutable `TenantContext`.
  - Managers: from user + membership.
  - Devices: from the credential, then verified inside the tenant; a
    client-claimed organization is never followed.
  - Jobs: one context per READY tenant (`run_per_tenant`).
  - Connections: from server configuration + the validated database name
    only.
- **Guards:**
  - the control chain refuses tenant databases, and the tenant runner
    refuses control databases;
  - no cross-tenant listing API;
  - `employees.role` never maps to SaaS roles.
- **Local proof:** in disposable PostgreSQL 16 in WSL Docker (see
  `test_database_per_tenant_isolation`):
  - one control database and tenant databases A and B, each migrated
    independently to `0004_manager_dashboard_auth`;
  - `employee-1` and `device-1` exist in both;
  - records, employees and tokens of A are invisible or unusable in B, and
    vice versa;
  - all databases are dropped afterwards.
- **Tests:**
  - new: 76 in `test_tenancy.py` and 10 in `test_tenancy_postgres.py`;
  - default run: 1004 passed, 0 failed, 139 skipped;
  - PostgreSQL-enabled: 1142 passed, 0 failed, 1 skipped.

### Still open before any public deployment

- The Cloud Run trusted-proxy, HTTPS/origin, dashboard mounting and login
  rate limiting items (`deploy/gcp/README.md`). The Cloud Run preflight
  exists since Phase 10D.
- Creating Cloud SQL and the other resources (Phase 10E). The connection
  design is done in Phase 10D.
- SaaS authentication (it replaces the per-tenant `manager_users`), device
  enrollment, and Google Sheets with the runtime identity.
- Billing (not designed yet).

## Earlier: Phase 10 — Windows VPS production deployment (abandoned for the SaaS direction; historical)

> Naming note: the "Phase 10A/10B" in this historical section are the
> Windows VPS steps. The approved **Phase 10B** of the SaaS direction is
> the Cloud Run container (`deploy/gcp/`), and Phase 10C is above.

Phase 9 and the Phase 10A read-only server inspection were approved. Phase
10B prepared the production runtime on the Windows Server 2022 VPS and
hardened the server for a public reverse proxy. **Production is NOT
active:**

- The PostgreSQL database and roles are **not created**.
- Caddy is **not modified**.
- The ZaZa Windows service is **not registered**.
- Google Sheets is **not configured**.
- **No employee traffic** reaches the server.
- **Phase 11 has not begun.**

The runbook is [docs/zaza/PRODUCTION_SERVER.md](docs/zaza/PRODUCTION_SERVER.md).

### Phase 10B summary

- **Server prerequisites (outside Git):**
  - Python 3.14.8 x64 for all users, in `C:\Program Files\Python314`: not
    on PATH, no associations. Python 3.15 is unchanged.
  - `C:\apps\ZaZaEmployeeManagement\{app,releases,venv,config,secrets,logs,run,backup,scripts}`:
    Administrators/SYSTEM only. `secrets` and `backup` block inheritance.
  - A Python 3.14 venv with **server-only** packages, installed from a
    hashed lock: 44 packages, none of them employee-side.
- **Server-only dependencies:** `deploy/windows/requirements-server.txt`
  plus `requirements-server.lock.txt`. Tests derive the set from the
  server's imports and forbid employee-side packages.
- **Exact-commit deployment** (`Deploy-ZaZaServer.ps1`):
  - `git archive` of `deploy/windows/server-files.txt` (server code + the
    shared protocol only);
  - forbidden-file checks, then `DEPLOYED_COMMIT.json` with per-file
    SHA-256;
  - an `app` junction switch; rollback with `Rollback-ZaZaServer.ps1`.
- **Hardening (new code):**
  - `http_security.py`: forwarded headers only from loopback proxies;
    https scheme from the proxy; `ZAZA_PUBLIC_HOST` Host validation.
  - `dashboard/ratelimit.py`: `/manager/login` rate limiting with 429 and
    `Retry-After`; no username enumeration.
  - `logs.py`: rotating, redacted logs.
  - `preflight.py`: `check-config [--production]`.
  - `--env-file`, a shared non-secret configuration file.
  - `serve` runs with uvicorn proxy headers off, one worker, and refuses
    a public host without Secure cookies.
- **Prepared for Phase 10C, not run:**
  - `ZaZaService.ps1`: NSSM, virtual account
    `NT SERVICE\ZaZaEmployeeManagement`, `127.0.0.1:8100`.
  - `Initialize-ZaZaDatabase.ps1` with `deploy/postgres/*.sql`: three
    roles, SCRAM verifiers, explicit grants.
  - `Invoke-ZaZaJobs.ps1`: `recalculate --recent-days 7`, then
    `sheets-sync` as the read-only role, with a lock, timeout and log.
  - `Test-ZaZaServer.ps1` and `Install-ZaZaConfig.ps1`.

### Phase 10B test results

| Environment | Result |
|---|---|
| Production venv (Python 3.14.8, server-only packages + pytest/httpx from a scratch folder), new tests | `test_production_server.py` + `test_production_deploy.py`: 132 passed |
| Production venv, whole `tests/zaza_agent` | 886 passed, 129 skipped, 2 failed |
| Full development environment (scratch venv, Python 3.14.8, all project dependencies) | 888 passed, 0 failed, 129 skipped |
| Phase 9 commit `305b763` in the same environment | 756 passed, 129 skipped |

- **The 2 failures in the production venv** are employee-agent tests that
  need `psutil` and `PIL`. Those packages are deliberately absent from the
  server venv.
- **The 888 includes** the 756 existing tests and 132 new ones.
- **Skipped tests** are the opt-in PostgreSQL integration tests and the
  live Google test.
- `ruff check` is clean on all changed Python.

### Phase 10 known limitations / risks

- **Single process only:** the login rate limiter is in memory. Running
  more than one worker would split the counts.
- **Recalculation window:** data uploaded more than 7 days late needs a
  manual `recalculate --from/--to` or the optional weekly
  `-RecentDays 35` run.
- **Jobs account:** the jobs are planned to run as SYSTEM. A per-task SID
  would be narrower (Phase 10C decision).
- **NSSM rotation:** NSSM 2.24 rotates stdout/stderr without a count
  limit. The job script prunes them; if the jobs are not scheduled, prune
  them manually.
- **No live PostgreSQL run yet:** the role and grant SQL has not been run
  against PostgreSQL 18. It matches the grants rehearsed on PostgreSQL 16
  in Phases 4–7.
- **Central retention** windows are still undecided (SECURITY.md §7).

### Phase 9 (approved)

Phase 8 was approved with its review fix. Phase 9 packages the approved ZaZa
agent as `ZaZaWorkAgentSetup.exe`. **No VPS deployment, pilot or AI.** The
central server is not deployed.

### Phase 9 summary

- **Product:** ZaZa Work Agent 0.9.0, publisher ZaZa.
  - `ZaZaWorkAgent.exe` (PyInstaller 6.22.3, one folder, no console window).
  - `ZaZaWorkAgentSetup.exe` (Inno Setup 6.7.3; installs into Program
    Files, admin once).
- **New agent modules** (`deskmate/zaza/`):
  - `workagent.py` (entry point), `runner.py`, `enrollment.py`;
  - `instance.py` (single-instance lock), `autostart.py` (logon task XML),
    `control.py` (stop requests);
  - `status.py`, `ui.py` (Status & Privacy and enrollment windows);
  - `app_logging.py` (rotating, privacy-filtered logs), `edition.py`,
    `privacy_notice.py`.

  `paths.py` now uses `%LOCALAPPDATA%\ZaZa\WorkAgent` and migrates the old
  path. The recorder and sync code are unchanged.
- **Installer tooling** (`installer/`): `build.ps1`, `ZaZaWorkAgent.spec`,
  `build_config.py`, `audit_package.py`, `ZaZaWorkAgent.iss`,
  `smoke-test.ps1`, `make_icon.py` and `assets/zaza.ico` (a replaceable
  placeholder). `installer/vm/` holds the disposable Hyper-V VM tooling used
  for the frozen validation.
- **Docs:** `docs/zaza/ADMIN_INSTALL.md`, `docs/zaza/EMPLOYEE_PRIVACY.md`,
  `installer/vm/README.md`.

### Phase 9 frozen Windows validation (2026-10-09/10)

The real frozen artifacts were run in a **disposable Hyper-V VM**: Windows 11
Enterprise Evaluation, build 26300, with Smart App Control off inside the VM
only. The build PC's security settings were not changed. Only the local
development server (`http://127.0.0.1`) was used, never the VPS.

- **Build:** clean `installer\build.ps1`, production flavour, **with tests**.
  - Build PC: Windows 11 Pro 10.0.26300, Python 3.14.6 (64-bit),
    PyInstaller 6.22.3, Inno Setup 6.7.3.
  - `ZaZaWorkAgentSetup.exe`: 15,884,037 bytes, **unsigned**.
    SHA-256 `a0aca39aa0be46cd1886e36a5be77f7980c2ea4284e4e8401818385520f4ce72`.
  - `ZaZaWorkAgent.exe`: SHA-256
    `84e7449851f5bc8b3831da5d4f02076a1b3dce62325118a1426ddcab45800568`.

  The build writes the hashes to `dist\SHA256SUMS.txt` and the audit to
  `dist\package-audit.txt`; both are kept with the build output, not in Git.
- **Package privacy audit:** `RESULT: PASS`. An independent read of the
  `PYZ.pyz` inside the built exe found:
  - 509 modules, of which 36 are first party, all `deskmate.zaza.*`;
  - no upstream DeskMate module, no server, PostgreSQL, Sheets or dashboard
    code, and no prohibited library.
- **Frozen exe before installation**, with no Python on the machine:
  - `--version` prints `ZaZa Work Agent 0.9.0`;
  - `--status` opens the Status & Privacy window (Tcl/Tk loads from the
    bundle).
- **`installer\smoke-test.ps1`** against that exact installer, from a fresh
  VM checkpoint: **78 passed, 0 failed**. It covers:
  - the install, version resource, the real logon task and
    `--enroll-stdin`;
  - DPAPI and the token absent from every data file;
  - URL/TLS rules (public CA accepted; self-signed, expired and wrong-host
    certificates refused);
  - the recorder as the user, not SYSTEM and not elevated;
  - single instance, online sync, the offline queue and its drain;
  - upgrade with pending records, uninstall, reinstall with the same device
    identity, log privacy, and `--remove-local-data`.
- **Interactive pass**, as the standard user `zazaemp`, by screenshots and
  keyboard/mouse:
  - UAC credential prompt ("Publisher: Unknown").
  - Wizard: Welcome → What is recorded → Server and device enrollment →
    Ready → Finish.
  - Enrollment as the employee:
    - a non-loopback `http://` address is refused;
    - a wrong token gets "Device token was not accepted";
    - the real token gets "Connection successful".
  - Status & Privacy shows every required field and no secrets.
  - Re-enroll never shows the stored token.
- **Real logon:** after sign-out and sign-in at the lock screen, the logon
  task started exactly one `ZaZaWorkAgent.exe --background`:
  - parent: the Task Scheduler service host;
  - user `ZAZA-TEST\zazaemp`, not SYSTEM, in the user's interactive session;
  - not elevated;
  - it synced with its DPAPI credentials.

  The same was repeated after the upgrade.
- **Cross-user DPAPI:** with a byte-identical copy of `zazaemp`'s
  `device_credentials.json` and `config.json` in `zazaemp2`'s profile,
  `zazaemp2`'s agent stayed `WAITING_FOR_ENROLLMENT`. It had no database and
  sent no request to the server. The Status window said "Device credentials
  missing - re-enroll this device".
- **Interactive upgrade** while offline with pending records:
  - no restart needed, nothing scheduled to be replaced at reboot;
  - the agent was stopped first;
  - config and credentials stayed byte-identical, and pending records were
    kept;
  - one task and one recorder afterwards; no re-enrollment;
  - the queue drained when the server returned.
- **Uninstall from Settings › Installed apps:**
  - removed: the agent process, program files, task, Start-menu folder and
    uninstall registration;
  - `activity.db`, config, credentials and logs were kept.
- **Reinstall:** the existing enrollment was reused, with no enrollment
  window. The server still had exactly one device.
- **`--remove-local-data`:**
  - with unsynced records it warns "8 activity record(s) have NOT been
    uploaded yet. Unsynced activity records will be permanently deleted";
  - **No** keeps everything, and the agent restarts;
  - **Yes** removes the data folder and the DPAPI credentials.
- **Logs:** the employee's `agent.log` contained no token, Bearer or
  Authorization text, URL, window title, per-tick activity, clipboard,
  screenshot or keystroke content, or database credentials. It is rotated at
  1 MB with 5 files kept.

**Defects found by the frozen validation and fixed** (each has a regression
test where testable):

1. **Upgrade ignored a failed stop.** `--stop-agents` always returned 0, and
   `PrepareToInstall` ignored the exit code. Now a survivor gives exit 1,
   and setup aborts before changing anything. Startup-task registration and
   the uninstall cleanup also check their results.
2. **No console in the windowed exe.** `--version` and `--enroll-stdin`
   output went nowhere. Output now uses a redirected stdout or the parent
   console, and `--enroll-stdin` without input fails clearly.
3. **Tk's feather icon** on every window. The ZaZa icon is now bundled and
   used.
4. **Status window taller than a 768-pixel screen,** so its buttons were
   off-screen. The buttons are now pinned at the bottom, the privacy lists
   sit side by side, and the window is sized to the screen.
5. **Welcome page skipped.** Inno Setup 6 hides it by default; it is now
   enabled (`DisableWelcomePage=no`).
6. **"No" in `--remove-local-data` left monitoring stopped** until the next
   sign-in. The agent now restarts. With 0 unsynced records, the dialog says
   so.
7. **Re-enrollment could restart recording twice** and split the work
   session. The credentials file and `config.json` are written separately;
   the runner now waits until both have settled (2 s). This showed up as a
   flaky test.
8. **Enrollment window:** the cursor starts in the first empty field, and
   Enter submits.

The smoke test itself was also corrected:
- PowerShell 5.1 `.Count` on a single CIM object;
- a locked `agent.lock` during the token scan;
- `[::1]` expectations;
- counting the VM harness's own tasks.

### Phase 9 test results

- **Default run** (build PC and VM): 756 passed, 0 failed, 129 skipped.
- **PostgreSQL-enabled** (in the VM, throwaway loopback PostgreSQL 16.15):
  884 passed, 0 failed, 1 skipped (the live Google Sheets test).
- `test_installer.py`: 67 tests.
- **Frozen Windows smoke test:** 78 passed, 0 failed.

### Phase 9 known limitations / risks

- **Unsigned build:** SmartScreen and Smart App Control warn or block until
  a real Authenticode certificate is used. On a PC with Smart App Control
  on, the unsigned installer will not run at all.
- **The build PC's Smart App Control blocks unsigned DLLs, intermittently.**
  For example, SQLAlchemy's compiled extension was blocked once during a
  build. So the PostgreSQL-enabled suite was run in the VM, and the build's
  test step may need a re-run on this PC.
- **Enrollment window focus:** it opens on top, but Windows' foreground
  lock can leave it without keyboard focus until the employee clicks into
  it.
- **Sign-out ends the agent abruptly.** The session is closed by Phase 2
  recovery at the next start (logged as an interrupted session), not by a
  clean shutdown.
- **No tray icon** (deferred), and no automatic updates.
- **Enrollment** happens after installation, as the employee (DPAPI is per
  user); silent installs need a separate per-user enrollment step.
- **Website domains** are not detected (unchanged from Phase 2); the
  privacy notice says so.
- `deskmate/zaza.zip`, an old source snapshot committed in Phase 3, is still
  in the repository. It is not packaged, and can be deleted in a cleanup.

### Phase 8 (approved)

Approved with its review fix (summary coverage gates comparisons).


Phase 7 was approved with its review fixes. Phase 8 adds six charts and
automatic, **deterministic rule-based analysis (not artificial
intelligence)** to the dashboard. **No installer, VPS deployment, pilot or
AI.** No new telemetry. Everything runs on localhost only.

### Phase 8 summary

- **Code:**
  - `dashboard/analysis.py`: chart data, previous-period comparison,
    eligibility, insight rules.
  - `dashboard/charts.py`: SVG geometry.
  - `templates/_charts.html`, `templates/analytics.html`, and chart CSS.
- **Charts:** server-rendered SVG, with no JavaScript library and no CDN,
  so the CSP is unchanged.
  - The six charts: Active Hours by Employee; Active/Idle/Unknown/Locked;
    Daily Active Hours; Weekly Work Trend; Application Usage (top 10);
    Attendance (late, early, absent, data-incomplete days).
  - Every chart has a title, units, a description, a legend, patterns as
    well as colours, and a data table.
- **Pages:**
  - New `/manager/analytics` page: all charts, up to 8 insights and a
    previous-period comparison table.
  - Overview: 3 compact charts and up to 5 insights.
  - Employee page: 4 charts above the existing tables.
  - `GET /manager/api/analytics` (version 1).
- **Data sources:** stored `daily_summaries` (one query per page),
  `application_usage_daily` (one aggregated query) and the Phase 7
  employee-local selection. Nothing is recalculated from activity periods.
- **Analysis:**
  - Fixed rule order: data quality, absence, Active Hours (team total,
    average, highest/lowest among eligible employees, active % of
    scheduled), comparison, late/early, high idle (≥ 40% of tracked),
    overtime, top application.
  - Neutral wording, with disclaimers; no productivity score.
- **Tests:**
  - Default run: 689 pass and 129 skip. The skips are the opt-in PostgreSQL
    tests and the opt-in live-Google test.
  - With `ZAZA_TEST_POSTGRES_URL` set: 817 pass and 1 skip (the live
    Google test).
  - New: 62 tests in `test_analytics.py` and 1 PostgreSQL analytics test.

### Phase 8 review fix (approved)

- **Summary coverage.** For each employee and period, the expected dates are
  the selected local dates up to their local today; future dates are never
  missing.
- **Comparable employee** now also requires no missing expected summary.
  This applies to highest/lowest, the comparable average and the high-idle
  observation. Charts still show all recorded data.
- **Team wording separates the two groups:** "Total recorded Active Hours
  across available summaries" versus "N employee(s) have complete enough
  data for employee-to-employee comparison. Average … among those
  employees". Excluded employees are named.
- **Period comparisons check both periods.** Missing summaries or
  INSUFFICIENT/DATA_INCOMPLETE days on either side make the comparison
  **incomplete**: no percentage or increase/decrease, a DATA_QUALITY notice,
  and table rows marked "not comparable".
  START/END_UNCERTAIN or AWAITING_DEVICE_SYNC makes it **qualified**: the
  change is shown with a caution.

### Phase 8 known limitations / risks

- **No manual browser review yet.** SVG layout, long labels (cut at 28
  characters, full name on hover and in the table) and the 420 px chart grid
  have only been checked through TestClient output.
- **Comparing a period still in progress** (This Week, This Month) with a
  full previous period naturally shows a decrease. The insight and the page
  say "still in progress", but managers may still misread it.
- **Team weekly buckets add up each employee's local week.** With mixed time
  zones, the week boundaries differ by employee; this is labelled.
- **The "active % of scheduled" caution threshold** (UNKNOWN above 10% of
  tracked time) is a fixed constant, not a setting.
- **Highest/lowest** names individuals. It is neutral and disclaimed, but it
  is still a comparison of individuals; managers should read it with the
  data-quality notes.

### Phase 7 (approved)

Approved with its review fixes (ID-based KPI counts, `is_active` Active
Employees, localhost-only dashboard, real `serve` wiring tests).


Phase 6 was approved with its review fixes. Phase 7 adds a server-rendered
manager dashboard under `/manager` in the existing FastAPI server. It reads
PostgreSQL directly and never Google Sheets. **No charts, analysis,
installer, VPS deployment or AI.** It runs on localhost only. PostgreSQL
tests used a throwaway local instance, which has since been deleted.

### Phase 7 summary

- **Code:** `deskmate/zaza_server/dashboard/`:
  - `config`, `security`, `models`
  - `queries`: the PostgreSQL repository, plus an in-memory one for tests
  - `auth`, `service`
  - `routes`: rendering only
  - `templates/`, `static/`
- **Migration `0004_manager_dashboard_auth`** (forward-only; 0001–0003
  untouched):
  - `manager_users`: lower-case unique username; a CHECK that only accepts
    Argon2id hashes; role ADMIN/MANAGER.
  - `manager_sessions`: SHA-256 hashes of the session and CSRF tokens; an
    expiry; revocation.
  - A `(device_id, ended_at)` index for current status.
  - `audit_logs` now also accepts entity `manager_user`.
- **Auth:**
  - Argon2id passwords (minimum 12 characters) and a generic login error.
  - Opaque random session token in a cookie that is HttpOnly,
    SameSite=Strict, Path=/manager and Secure when configured; absolute
    12-hour expiry.
  - Per-session CSRF token on every POST, plus an Origin check.
  - The login form has its own token.
  - Strict security headers and CSP.
  - Accounts are managed only by the CLI: `manager-create`, `-list`,
    `-disable`, `-enable`, `-reset-password`, `-revoke-sessions`, with
    `getpass` prompts.
- **Pages:** Overview, Employees, Employee detail, Attendance, Applications,
  Schedules; plus authenticated JSON for current status, overview,
  attendance and applications. Status refreshes every 60 s while the tab is
  visible. No charts.
- **Figures:**
  - Totals are sums of stored `daily_summaries` over each employee's own
    local period.
  - Attendance % = Σ credit ÷ Σ basis, never an average of daily
    percentages.
  - Where employees' local periods differ, the page says so.
- **Current status:**
  - Enabled devices only.
  - Online means `last_seen_at` within 300 s; the device's state comes from
    its latest period if it is recent.
  - Several devices: ACTIVE > IDLE > LOCKED > UNKNOWN > online-no-activity
    > offline.
  - UNKNOWN is never shown as idle.
- **Schedules:**
  - They use the Phase 5 write path, so the database validates every rule
    and every change is audited with the manager's identity and old/new
    values.
  - Changes apply from the employee's local today; running weekly rules are
    split or ended from a date, and past rules are read-only.
- **Tests:**
  - Default run: 627 pass and 128 skip. The skips are the opt-in PostgreSQL
    tests and the opt-in live-Google test.
  - With `ZAZA_TEST_POSTGRES_URL` set: 754 pass and 1 skip (the live
    Google test).
  - New: 96 dashboard tests (`test_dashboard.py`, no database) and 7
    PostgreSQL tests (`test_dashboard_postgres.py`).

### Phase 7 review fixes (approved)

1. **Employee-level flags are counted by employee ID.** Late, absent and
   data-incomplete employees are deduplicated by ID, not display name.
   Duplicate names are shown with their ID ("John Smith (emp-01)"). The JSON
   adds `*_count` and `*_ids`.
2. **"Active Employees" counts `is_active` employees.** An explicitly
   selected former employee gives 0, while their history stays reportable.
3. **The dashboard is localhost-only.** `serve` mounts `/manager` only on a
   loopback bind host (`build_server_app()`); otherwise it prints a notice
   and serves the sync API alone, whose behaviour is unchanged.
4. **Real `serve` wiring tests.** `main(["serve", …])` is run with
   `uvicorn.run` replaced: offline with an in-memory repository, and on
   PostgreSQL with a full login. The tests check that the dashboard and the
   sync API share the app, and that the dashboard is refused on non-loopback
   hosts.

### Phase 7 known limitations / risks

- **The loopback check trusts the `--host` value.** A host name other than
  `localhost` is refused, even one that resolves locally. Phase 10 replaces
  this with the HTTPS reverse-proxy setup.
- **No login rate limiting or lockout.** Argon2 slows guessing, but Phase 10
  should rate-limit `/manager/login` at the reverse proxy.
- **The Secure cookie flag is off by default** so the dashboard works on
  localhost. Production must set `ZAZA_DASHBOARD_COOKIE_SECURE=true` behind
  HTTPS.
- **The Origin check compares with the request's own scheme and host.**
  Behind a TLS-terminating proxy, Phase 10 must forward the original scheme
  and host (proxy headers) or adjust the check.
- **Figures are only as fresh as the last `recalculate`.** The dashboard
  shows "last calculated …", and today's row may be missing until the next
  run. There is still no scheduler (Phase 10).
- **Application usage** is grouped by the device's calendar date, which can
  differ from an overnight shift's attendance date. This is labelled on the
  pages.
- **Removing an upcoming rule is a real DELETE** (audited, with old values),
  so production `zaza_app` needs `GRANT DELETE ON work_schedules`
  (SECURITY.md §6.2).
- **Account management is CLI-only**, and the ADMIN role adds nothing in the
  UI yet.
- **No manual browser testing yet.** The pages have only been exercised
  through FastAPI's TestClient.

### Phase 6 (approved)

Approved with its review fixes (one-connection refresh, interval-overlap
Activity Log window, truthful per-employee Dashboard labels).


Phase 5 was approved with its review fixes. Phase 6 adds a one-way,
read-only export from PostgreSQL to an existing Google spreadsheet. **No
dashboard, charts, installer, VPS deployment or AI.** Nothing connected to
a real Google account or to the VPS. PostgreSQL tests used a throwaway local
instance, which has since been deleted.

### Phase 6 summary

- **Code:** `deskmate/zaza_server/sheets/`:
  - `config.py`, `models.py`, `queries.py`, `formatter.py`
  - `client.py`: the `SheetsClient` interface; `GoogleSheetsClient` using
    the official Google libraries (optional extra `zaza-sheets`);
    `FakeSheetsClient` for tests.
  - `exporter.py`
- **CLI:** `sheets-init`, `sheets-sync` and `sheets-status`.
- **Tabs:** Activity Log, Daily Summary, Weekly Summary, Monthly Summary,
  Dashboard (no charts).
  - The Activity Log has one row per stored activity period. It has no raw
    events and invents no LOGIN/LOGOUT events.
  - Each duration goes only in its status's column.
  - Privacy-excluded periods stay redacted exactly as stored.
- **Authority:** every figure is a stored Phase 5 value. Dashboard totals
  are sums of stored seconds; team Attendance % is Σ credit ÷ Σ basis.
- **Time zones:** employee rows use the employee's time zone. Team-level
  times use `ZAZA_SHEETS_TIMEZONE`, or the employees' common zone, or UTC,
  and are always labelled.
- **Safety:**
  - PostgreSQL is read in one read-only snapshot before Google is touched.
  - Tabs are overwritten first, and stale rows are cleared only afterwards.
  - The Dashboard's "Last successful refresh" is written last.
  - An advisory lock prevents concurrent refreshes. The lock and the
    read-only snapshot share one connection, so `ZAZA_DB_POOL_MAX=1` works.
  - Values are written as RAW, so no formula injection.
- **Secrets:** the key file is validated without echoing it. Errors are
  scrubbed (PEM keys, key IDs, OAuth tokens), and the spreadsheet ID is
  masked.
- **Tests:**
  - Default run: 531 pass and 121 skip. The skips are 120 opt-in PostgreSQL
    tests and 1 opt-in live-Google test.
  - With `ZAZA_TEST_POSTGRES_URL` set: 651 pass and 1 skip (the live test).
  - New: 78 Sheets tests (`test_sheets.py`), 8 PostgreSQL tests
    (`test_sheets_postgres.py`), and 1 opt-in live test
    (`test_sheets_google_live.py`).
  - The Google adapter is tested offline with the library's
    `HttpMockSequence`.

### Phase 6 review fixes (approved)

1. **One connection per refresh.** `PostgresReportSource.refresh()` takes the
   advisory lock, reads the read-only snapshot on the same connection, and
   keeps the lock through the Google writes. Sheets sync works with
   `ZAZA_DB_POOL_MAX=1`, and a concurrent refresh is still refused.
2. **Activity window overlap.** A period is shown if it overlaps the window
   (`ended_at > cutoff OR started_at >= cutoff`), both in SQL and in memory,
   with each employee's own cutoff. A period crossing the boundary is shown
   once, whole.
3. **Truthful Dashboard period labels.** When active employees are on
   different local dates / weeks / months, the label reads "Per employee
   local date / week / month (…)" instead of naming one.

### Phase 6 known limitations / risks

- **No scheduler yet.** Phase 10 runs `recalculate --recent-days 7` and then
  `sheets-sync`. The Sheet is only as fresh as the last run of both.
- **Not atomic across tabs.** A refresh that fails part-way can leave a tab
  with a mix of new and old rows. The Dashboard then still shows the old
  "Last successful refresh" and "IN PROGRESS", and the next successful run
  repairs it.
- **Activity Log size.** With many short periods, 30 days for 5 employees
  can reach tens of thousands of rows (Google's limit is 10 million cells).
  Lower `ZAZA_SHEETS_ACTIVITY_DAYS` if the Sheet becomes slow. The grid
  grows but is never shrunk (cleared rows stay as empty grid).
- **Managed tabs are fully rewritten.** Manual edits or extra columns in
  the five tabs are lost at the next refresh. Other tabs are untouched.
- **Not yet tested against real Google.** Only the fake and the official
  library's offline mocks have been used. The opt-in live test needs a
  disposable spreadsheet.
- **Spreadsheet version history** (Google's) keeps older contents. That is
  outside ZaZa's control.

### Phase 5 (approved)

Approved with its review fixes (schedule timezone = employee timezone,
per-employee `--recent-days`, early-leave uncertainty, full policy
configuration).

Phase 4 was approved after the overnight-shift schedule fix. Phase 5 adds
deterministic attendance calculations. The results are stored in PostgreSQL
and are the authoritative source for Sheets, the dashboard and charts (Phases
6–8). **No Sheets, dashboard, charts, AI, installer or VPS work.** All
PostgreSQL testing used a throwaway local instance, which has since been
deleted.

### Phase 5 summary

- **Code:** `deskmate/zaza_server/attendance/`. The daily calculation is a
  pure function, so the same inputs always give the same output.
  - `calculator.py`: the daily calculation.
  - `schedule.py`: schedule resolution, DST-correct UTC shift times, and
    date attribution.
  - `timeline.py`: the non-overlapping timeline.
  - `rollup.py`: ISO week and month roll-ups.
  - `summary_service.py`: `calculate_daily`, `calculate_week`,
    `calculate_month` and `recalculate`.
  - `store.py` and `postgres_store.py`: storage.
- **Migration `0002_attendance_summaries`:** forward-only. It adds
  `daily_summaries`, `weekly_summaries` and `monthly_summaries`, with keys,
  foreign keys and named CHECKs (for example, tracked = active + idle +
  unknown + locked; no lateness on a non-late day; ISO Monday weeks). It
  also adds team-view indexes. `0001_initial` was not touched.
- **Fairness:**
  - UNKNOWN is never treated as idle, lateness or early leave.
  - Missing or unsynced data gives `DATA_INCOMPLETE`, not `ABSENT`.
  - An agent crash or unsynced device excludes only the uncertain part
    of the shift's end from early leave.
  - Every instant belongs to exactly one date, so nothing is counted twice.
- **Formulas and statuses:** ARCHITECTURE.md §4.11; the reporting
  interpretation is in SECURITY.md §6.4.
- **CLI:** `add-schedule`, `list-schedules`, `summarize-day`,
  `summarize-week`, `summarize-month` and `recalculate`. They need the
  PostgreSQL backend.
- **Tests:**
  - Without PostgreSQL: 453 pass in `tests/zaza_agent/` and 112 skip. The
    skips are the opt-in PostgreSQL tests.
  - With `ZAZA_TEST_POSTGRES_URL` set: 565 pass and 0 skip.
  - New: 84 engine tests (`test_attendance.py`) and 13 PostgreSQL tests
    (`test_attendance_postgres.py`). The PostgreSQL tests include exact
    equality between the PostgreSQL and in-memory results.

### Phase 5 review fixes (approved)

1. **Schedule timezone = employee timezone.** `add_schedule` /
   `add-schedule` default to the employee's timezone and reject any other;
   migration `0003_schedule_timezone` adds a composite foreign key so direct
   SQL can't create a mismatch (and refuses to apply over existing
   mismatches). Per-schedule travel timezones are not supported.
2. **`--recent-days N`** = the last N local dates including today, per
   employee timezone (`recalculate_recent`); N ≥ 1. `add-schedule`'s default
   `--effective-from` is the employee's local today.
3. **Early leave** excludes only uncertain time (UNKNOWN, after a crash until
   the agent restarts, after a device's last contact). Earlier reliable
   inactivity still counts. `calculation_version` is now 2.
4. **Policy settings:** every `AttendancePolicy` field is configurable from
   the environment with validation (`.env.example`).

### Phase 5 known limitations / risks

- **No production scheduler.** Summaries are only as fresh as the last
  `recalculate`. Phase 10 should run `recalculate --recent-days 7`
  periodically.
- **"Device has synced since" uses `devices.last_seen_at`.** It is a single
  timestamp, so a backlog still uploading after a reconnect could briefly
  look complete.
- **Attribution margin (4 h).** Work more than 4 h after a shift and past
  midnight counts on the next calendar day. It is still counted exactly once,
  and it never hides lateness, because punctuality only looks at the shift
  neighbourhood.
- **Overlapping shifts** (allowed by the schedule constraints) are resolved
  by the earliest-starting shift and flagged `SCHEDULE_OVERLAP`. They are not
  blocked at entry.
- **Schedule rules can't be edited or removed yet.** Only `add-schedule`
  exists, so managing schedules properly is a Phase 7 dashboard feature. A
  newer weekly rule supersedes an older one from its `effective_from` date.
- **The policy is global.** It is not per employee, and changing it
  requires recalculating.
- **One timezone per employee.** Schedules can't use a different zone
  (travel, remote shifts in another region). Changing an employee's timezone
  is blocked while they have schedules; it needs a deliberate migration.
- **Crash point = last heartbeat.** After an INTERRUPTED session, uncertainty
  starts at its last heartbeat; reliable data synced just after it (within
  one heartbeat interval) is not charged as early leave. This errs in the
  employee's favour.
- **`application_usage_daily` is not part of the attendance figures,** by
  design, because of its device-local dates.

### Phase 4 (approved)

Approved after the overnight-shift fix to `work_schedules_hours_valid`.

Phase 4 added a production
PostgreSQL repository behind the unchanged Phase 3 repository interface. The
wire protocol and the agent are unchanged. **Nothing connected to the
production VPS**: all development and testing used a throwaway local
PostgreSQL 16 in a temporary folder, which has been deleted. No Sheets, no
dashboard, no attendance calculations.

### Phase 4 summary

- **Library:** psycopg 3 driver + `psycopg_pool`, using plain SQL with no
  ORM. Alembic handles migrations only. Everything is in the optional extra
  `zaza-postgres`.
- **Code:** `deskmate/zaza_server/postgres/`, with three parts:
  - `repository.py`: `PostgresRepository`.
  - `migrate.py`: upgrade, status, and offline SQL rendering.
  - `migrations/versions/0001_initial_schema.py`: the schema.

  Configuration is in `zaza_server/config.py`, and backend selection in
  `zaza_server/backends.py`.
- **Schema:**
  - Tables: employees, devices, device_tokens, work_schedules (prepared
    only), work_sessions, activity_periods, idle_periods,
    application_usage_daily, and audit_logs (append-only).
  - All columns are typed, and every instant is TIMESTAMPTZ. Each synced row
    also keeps its wire record in JSONB for audit only.
  - See ARCHITECTURE.md §4.2–4.4 and §4.9.
- **Integrity:** primary keys, foreign keys (periods → a session of the
  *same* device), unique keys, named CHECKs, and triggers for time zones,
  `updated_at` and audit append-only. Python validation is not the only
  guard.
- **Idempotency and concurrency:** the shared `decide()` runs on a row
  locked with `SELECT … FOR UPDATE`. A new record uses `INSERT … ON CONFLICT
  DO NOTHING` and then re-decides if it lost a race. See ARCHITECTURE.md
  §4.5.
- **Transactions:** one transaction per batch with a savepoint per record,
  so a constraint failure rejects only that record. A deadlock or
  serialization failure rolls back the whole batch and retries it, up to 3
  times, then answers 503. See ARCHITECTURE.md §4.6.
- **Migrations:** run only by an operator (`migrate`) and idempotent. The
  server refuses to start on an out-of-date schema and never migrates
  itself.
- **Device auth:** the registry is now in PostgreSQL. Token hashes are
  CHECK-constrained to SHA-256 hex, so a raw token can't be stored.
  `last_seen_at` (connectivity, not activity) and token `last_used_at` are
  updated after successful authentication.
- **Shared-interface changes:**
  - Employees are now part of the interface. A device needs an existing
    employee, so the CLI gains `add-employee` and `list-employees`.
  - Every backend now rejects an orphan period, or one that points to another
    device's session.
  - The API runs repository calls in the thread pool and answers 503 when
    storage is unavailable.
- **Tests:**
  - Without PostgreSQL: 369 pass in `tests/zaza_agent/` and 99 skip. The
    skips are the opt-in PostgreSQL tests.
  - With `ZAZA_TEST_POSTGRES_URL` set: 468 pass and 0 skip.
  - A real-process rehearsal (owner/app roles, live agent, outage and
    catch-up, leak checks) also passed.

### Phase 4 review fix (2026-10-08): overnight shifts

`work_schedules_hours_valid` now allows shifts that cross midnight
(20:00→04:00 = 8 h, ending the next local day).
- Equal start and end times, and `24:00`, are rejected: neither is read as
  a 24-hour shift.
- A working day must expect more than 0 seconds, and no more than the span.
- No Phase 4 database had been deployed or kept, so the initial migration
  `0001_initial` was corrected in place rather than adding a revision.
- The day rule for Phase 5 is in ARCHITECTURE.md §4.2.

### Phase 4 known limitations / risks

- **Not yet run on the real VPS.** The tests used PostgreSQL 16.2 locally,
  and the VPS runs 16.x. Creating the database and roles there is Phase 10
  (ARCHITECTURE.md §4.10, SECURITY.md §6.2).
- **The `payload` JSONB duplicates the typed columns,** window titles
  included. It is kept for audit and forward compatibility. It roughly
  doubles storage per row, which is small for 5 employees.
- **Composite session FK:** a period whose session never reaches the server
  is rejected and retried each cycle, which shows as BACKLOG. The agent's
  ordering makes this an edge case: a session that is itself rejected, or
  data lost on the server.
- **Durations use `double precision`.** That's exact enough for seconds, but
  Phase 5 sums should round for display.
- **No central retention or deletion yet** (Phase 5). The API role has no
  DELETE privilege.
- **Coupling to the agent's privacy placeholders:** the
  `privacy_excluded` CHECK mirrors the agent's placeholder strings, so
  changing them in the agent needs a migration.
- **Smart App Control blocks SQLAlchemy's compiled extensions on this PC.**
  `migrate.py` forces SQLAlchemy's pure-Python mode, so migrations work
  regardless.

### Phase 3 (approved)

Approved after the two client fixes: an invalid `stale` reply is never marked
synced, and any closed unconfirmed record reports BACKLOG.

### Phase 3 summary

- **Wire contract** (`deskmate/zaza/sync/protocol.py`, Pydantic):
  `GET /api/v1/sync/health`, `GET /api/v1/devices/me`, and
  `POST /api/v1/sync/batch` (1–500 records, 4 MiB). Requests are strict
  (unknown fields rejected, UTC-only timestamps). Each record is validated
  on its own, so one bad record doesn't fail the batch. See ARCHITECTURE.md
  §3.1.
- **Development server** (`deskmate/zaza_server/`): FastAPI app →
  `SyncService` → `CentralRepository`, with in-memory (tests) and SQLite
  development implementations. A CLI handles device registration, token
  rotation, disabling devices, and viewing records.
- **Idempotency:** keyed on `(record_type, record_id)` + `record_version` +
  content hash. Results are accepted / already_current / updated / stale
  (server keeps the newer) / conflict / rejected, for each record. See
  ARCHITECTURE.md §3.2–3.3.
- **Device auth:** an opaque per-device token stored only as a hash on the
  server, DPAPI-encrypted on the agent outside SQLite. Disable, revoke and
  rotate are supported. See SECURITY.md §6.1.
- **Sync worker** (`deskmate/zaza/sync/worker.py`): its own thread and DB
  connection. Batches go out in device order, and records are marked synced
  only for the version the server confirmed. Backoff is exponential with
  jitter (5s→300s), honours 429 Retry-After, and uses a long fixed retry for
  auth errors. A 413 halves the batch size. Sync health is HEALTHY /
  BACKLOG / OFFLINE / AUTH_ERROR / SERVER_ERROR / NOT_CONFIGURED. See
  ARCHITECTURE.md §3.4–3.5.
- **Agent CLI:** `set-credentials`, `sync-now`, `sync-status`, `report`. The
  normal run starts the sync worker automatically when it is configured.
- **Tests:** 329 in `tests/zaza_agent/`, all passing (137 new for Phase 3, including 11 review-fix regressions).
  A real end-to-end run (uvicorn server + live agent + DPAPI credentials
  + server down/up) also passed.

### Phase 3 known limitations / risks

- **No server-side rate limiting or TLS** in the development server. Both
  belong at the Phase 10 reverse proxy.
- **The employee can read their own token.** DPAPI protects the token from
  other accounts and machines, not from the employee's own account. The
  server limits the damage: a token can only write its own device/employee
  records.
- **Open records are re-sent each cycle** while open, because their version
  moves every tick. That's about 3 small records per minute: fine for 5
  devices, but worth revisiting at scale.
- **A permanently rejected record** stays FAILED and is retried every cycle,
  which keeps the state at BACKLOG. That's visible, but not auto-resolved.
- **`stale` after a local database restore:** the server's newer copy is
  kept, and the local version adopts the server's number.
- **App usage day bounds** come from the agent's current timezone when it
  is sent. If the PC's timezone changes, a later resend can produce a
  `conflict`, which resolves automatically by re-sending a newer version.
- **Concurrent sync processes** (e.g. `sync-now` while the agent runs) may
  send the same records twice. The server is idempotent, so the result is
  harmless.

### Phase 2 (approved)

Phase 1 was approved after Windows manual testing. Phase 2 is implemented
locally. Nothing in it touches the network, PostgreSQL, Google Sheets, a
dashboard, or AI.

### Phase 2 summary

- **Schema v2** (`deskmate/zaza/schema.py`): versioned forward-only
  migrations (a Phase 1 v1 database is upgraded in place), WAL mode. New
  tables: `work_sessions`, `activity_periods`, `idle_periods`,
  `app_usage_daily`, `sync_state`, `counters`. Raw `activity_events` gained
  `event_id`, `session_id`, `status`, `privacy_excluded`. See
  ARCHITECTURE.md §2.5.
- **Aggregation** (`aggregator.py`): raw ticks become contiguous periods
  keyed by status + app + domain. Window titles are normalized, and a title
  change splits a period only after 30s. Idle begins at last input + the idle
  threshold, since the threshold is a grace period. Idle periods are merged.
  See ARCHITECTURE.md §2.6.
- **ACTIVE / IDLE / UNKNOWN / LOCKED:** input is trusted only while both
  hooks are HEALTHY, and lock state only while the session-lock watcher is
  HEALTHY with a lock state established during that stint. DEGRADED/ERROR
  monitoring, an untrusted or stale lock state, a new or restarted session
  with no input yet, and telemetry gaps are all UNKNOWN, never idle. See
  ARCHITECTURE.md §2.3.
- **Work sessions & crash recovery:** a session per agent run, with
  heartbeats and totals. On restart, an OPEN session left by a dead run is
  marked INTERRUPTED at its last heartbeat. A backwards clock step closes the
  session (`CLOCK_CHANGED`). See ARCHITECTURE.md §2.7.
- **App usage** (`rollup.py`): per local day and app, recomputed from
  periods whenever one closes, split at midnight, with deterministic IDs.
- **Privacy exclusions** (`privacy.py`): excluded apps (the title is never
  read), hidden app names, and excluded domains. Domains are reduced to a
  bare hostname. See SECURITY.md §2.3.
- **Retention:** raw events are kept 7 days, including during a long OPEN
  session. Synced records are kept 30 days after being synced.
  PENDING/FAILED records are never deleted. See ARCHITECTURE.md §2.8.
- **Sync readiness (no network):** UUID record IDs, a device-wide
  `local_seq`, `record_version`, sync status/attempts/error fields, and
  `pending_sync` / `mark_synced` (version-checked) / `mark_sync_failed`.
- **Console:** `python -m deskmate.zaza --report` shows stored sessions,
  periods, idle periods, and app usage.
- **Tests:** 192 in `tests/zaza_agent/`, all passing.

### Phase 2 review fixes (2026-10-07)

1. **Idle threshold is a grace period.** IDLE now starts at
   `last input + threshold` (or `trusted since + threshold`), not at the last
   input. Before the first input, the pre-threshold time stays
   UNKNOWN/AWAITING_INPUT and is not converted to idle.
2. **Lock-watcher health gates IDLE.** Without a trusted lock state (watcher
   not HEALTHY, or a state from before an outage), the agent reports
   UNKNOWN/LOCK_STATE_UNAVAILABLE instead of IDLE, and never a stale LOCKED.
   The watcher now queries the current lock state when it registers, and the
   health registry has a per-component generation counter so stale state is
   detectable.
3. **Raw retention applies to OPEN sessions too.** The blanket OPEN-session
   exemption is removed. Nothing reads raw events to rebuild state.

Regression tests: `tests/zaza_agent/test_review_fixes.py`, plus
`test_retention_sync.py::test_long_running_open_session_old_raw_events_deleted_summaries_kept`
and `::test_crash_recovery_works_without_any_raw_events`.

### Phase 2 known limitations / risks

- **Single instance assumed.** If two agent processes share one database,
  the second treats the first's OPEN session as interrupted. The Phase 9
  installer should enforce one instance (e.g. a named mutex).
- **Gap detection** relies on ticks running. A hung (not dead) tick thread
  shows up as a gap only when it resumes, and if the process dies the time
  isn't recorded at all, as intended.
- **Low-level hook silent removal** (`LowLevelHooksTimeout`) is still
  undetectable. Such time would read as idle. Carried over from Phase 1.
- **Input as proof of presence while the lock watcher is down** assumes
  low-level hooks don't receive input typed on the secure lock-screen
  desktop. If that assumption were wrong, lock-screen input could count as
  up to one threshold of ACTIVE time. It would still never count as IDLE.
- **A missed LOCK while the watcher is down** after recent input: up to one
  threshold (5 min) is counted ACTIVE before the time turns
  UNKNOWN/LOCK_STATE_UNAVAILABLE.
- **Lock-state query** uses Windows 10/11 `SessionFlags` semantics. Windows
  7 / Server 2008 R2 report these inverted and are not supported targets.
- **Unsynced data grows** until Phase 3 exists (estimated at a few MB per
  year).
- `synchronous=NORMAL`: an OS crash or power loss can lose the last few
  seconds of committed ticks. An application crash cannot.
- Raw `activity_events` still holds per-tick window titles for 7 days.

### Phase 1 (approved)

Phase 0 (architecture & specification) is **approved**, subject to two
documentation corrections that have been applied:

1. **README.md** now represents the ZaZa Employee Management System first —
   what it is, what it never collects, PostgreSQL/Sheets/dashboard framing,
   and current under-development status — with DeskMate attribution/MIT
   license information kept intact. The original, unmodified DeskMate README
   is preserved at
   [docs/UPSTREAM_DESKMATE_README.md](docs/UPSTREAM_DESKMATE_README.md) for
   upstream reference.
2. **ARCHITECTURE.md** and **SECURITY.md** now explicitly state that browser
   tracking means only the *currently active domain*, where technically
   practical — never full URL paths, browser history, page contents, or
   search/query parameters.

Phase 1 then implemented the privacy-safe local Windows activity agent:

- **New, clearly separated module:** `deskmate/zaza/` — zero changes to any
  existing upstream `deskmate/` file. Upstream DeskMate source is left
  exactly as it was, so the two remain directly comparable.
- **What it tracks:** timestamp, active application/process, active window
  title, keyboard activity presence (boolean), mouse activity presence
  (boolean), idle/active status (default 5-minute threshold, configurable),
  and Windows lock/unlock events.
- **What it cannot capture:** typed characters/key names, clipboard
  contents, screenshots, OCR/accessibility screen text, audio, video — not
  hidden from a UI, not present in any code path or storage column. See
  `tests/zaza_agent/test_no_prohibited_capture.py` for the automated guard.
- **Storage:** a new, separate SQLite database (`~/.zaza_agent/activity.db`,
  overridable via `ZAZA_HOME`) with a single `activity_events` table and a
  fixed, narrow column set. No sync, no PostgreSQL, no network calls.
- **Manual run:** `python -m deskmate.zaza` — console output only, per the
  Phase 1 allowance.
- **Tests:** 82 tests in `tests/zaza_agent/` at Phase 1 approval. Existing
  DeskMate test suite re-run and unaffected (pre-existing, unrelated
  failures noted below, not caused by this work).

### Phase 1 review fixes (2026-10-07)

- **Win64 ctypes types:** `HOOKPROC` (`input_hooks.py`), `WNDPROC` and
  `DefWindowProcW.restype` (`session_lock.py`), and `CallNextHookEx.restype`
  now use `LRESULT = ctypes.c_ssize_t` (pointer-sized, signed) instead of
  `c_long`. `test_win32_signatures.py` guards against regressions.
- **Component health:** new `deskmate/zaza/health.py` — keyboard hook, mouse
  hook, session-lock watcher, foreground-window watcher, SQLite storage, each
  `HEALTHY` / `DEGRADED` / `ERROR`, plus an overall state. A failed hook is
  `ERROR`, not silent: ACTIVITY rows store NULL for the affected flag and for
  `idle`, and IDLE/CONTINUE are suppressed. Shown in the console run
  (startup table + a `[health]` line on each change). `test_health.py`
  simulates hook-install failure with fake `user32`/`kernel32`.
- **Privacy guards:** AST-based checks for keystroke/pointer content APIs in
  executable code (docstrings/comments ignored), a check that hook callbacks
  only forward `lparam` to `CallNextHookEx`, and self-tests proving the
  guards catch offending code. Existing screenshot/OCR/clipboard/audio/video
  guards are unchanged.
- **Known limitation:** Windows can silently remove a low-level hook whose
  callback exceeds `LowLevelHooksTimeout`; that can't be detected directly
  and is not reported as a health change.

## Phase checklist

- [x] Phase 0 — Architecture & specification documents (approved, with README/domain corrections applied)
- [x] Phase 1 — Privacy-safe local DeskMate-derived Windows activity agent (`deskmate/zaza/`)
- [x] Phase 2 — Local SQLite activity storage & aggregation (approved)
- [x] Phase 3 — Central synchronization API (approved)
- [x] Phase 4 — PostgreSQL central storage (approved; local testing only, no VPS)
- [x] Phase 5 — Attendance & work-time calculations (approved)
- [x] Phase 6 — Google Sheets reporting (approved)
- [x] Phase 7 — Manager web dashboard (approved; localhost only)
- [x] Phase 8 — Charts & automatic rule-based analysis (approved)
- [x] Phase 9 — Windows employee installer (approved; unsigned)
- [ ] Phase 10 — Production VPS deployment (**IN PROGRESS**: 10A inspection approved; 10B runtime prepared; 10C activation not started)
- [ ] Phase 11 — Pilot testing
- [ ] Phase 12 — Optional AI analysis

## Known pre-existing issues (not introduced by this work)

Running the full upstream test suite in this environment surfaces a few
pre-existing issues unrelated to the ZaZa module (confirmed via `git status`
— none of the affected files were touched in this or the prior pass):

- `tests/test_live_translation.py` fails to collect — `numpy` is not
  installed in this venv (an optional extra, not installed here).
- `tests/test_ai_prompt_journal.py` (2 tests), `tests/test_day_recap_context.py`
  (1 test) — timezone/locale-sensitive assertions that don't match this
  machine's local timezone.
- `tests/test_console.py::test_safe_stream_handler_falls_back_on_invalid_handle`
  and `tests/test_smoke.py::test_rapidocr_maps_result_to_normalized_words` —
  also pre-existing, the latter due to the same missing `numpy`.

None of these are in `deskmate/zaza/` or `tests/zaza_agent/`.

## Blocking item

**Awaiting your review of Phase 10B** before Phase 10C (activation). Phase
10C also needs these inputs:

1. the PostgreSQL administrative password, entered interactively;
2. the ZaZa public host name, its DNS record and the Cloudflare choice;
3. the Google service-account JSON key;
4. the spreadsheet ID.

Caddy, DNS, the firewall, PostgreSQL's configuration and roles, and
ZazaVoice have **not** been touched.

## Explicitly cancelled from any earlier direction

- Screenshot recording/storage
- Google Drive screenshot storage (or any Google Drive integration)
- Audio recording, webcam/video recording, keylogging, clipboard collection

None of the above exist in the current specification or in the Phase 1
implementation, and none should be reintroduced without a new, explicit
requirements change.
