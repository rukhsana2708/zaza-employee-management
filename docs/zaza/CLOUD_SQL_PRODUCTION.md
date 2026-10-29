# ZaZa production database foundation: Cloud Run + Cloud SQL (Phase 10D)

## Status (2026-10-29)

| Phase | What exists in Google Cloud (project `zaza-employee-monitor`, `asia-south1`) |
|---|---|
| 10D | design and code only (this document); the Terraform was written and validated offline |
| 10E-A | private, versioned Terraform state bucket `zaza-employee-monitor-tfstate-e33ac2a6`; reviewed plan |
| 10E-B | **core infrastructure applied**: Cloud SQL `zaza-prod-pg16` (PostgreSQL 16, Enterprise, `db-custom-1-3840`, ZONAL, 20 GB SSD with auto-resize to 200 GB, connector enforcement REQUIRED, no authorized networks, deletion protection, 14 backups, 7-day PITR, `max_connections` 100); the `zaza_control` database; Artifact Registry `zaza`; service accounts `zaza-run` and `zaza-migrate`; three password secrets (version 1); database roles `zaza_app`, `zaza_control_app`, `zaza_migrator` |
| 10E-C | build identity `zaza-build` and the private build-source bucket; production image `zaza-server@sha256:318a9d19d385…` (commit `7b7c223`, deployed by digest); migration job `zaza-migrate`; control schema migrated to `c0001_control_plane`; control runtime grants applied; legacy Cloud Build builder role removed |
| 10F-A | **code only**: Cloud Run HTTP security foundation (§10a); no infrastructure change |
| 10F-B | **code only**: SaaS authentication + credential-derived tenant routing (control migration `c0002_saas_auth_routing`) |
| 10F-C | image `sha256:b7ffd93a82c1…` (commit `1f63fb2`) built as `zaza-build` and set on `zaza-migrate`; **production control schema migrated to c0002**; runtime grants applied and verified; auth/routing tables empty; no web service, user, tenant or route |
| 10G-A | **code only**: tenant provisioning commands + one-time device enrollment (control migration `c0003_device_enrollment`, including the durable CLEANUP_PENDING reconciliation repair); see [ENROLLMENT.md](ENROLLMENT.md) |
| 10G-B | image `sha256:b7ad3a892306…` (commit `778488c`, build `efe5ed6f…`) built as `zaza-build` and set on `zaza-migrate`; **production control schema migrated to c0003**; the database-enforced status model verified live; c0003 runtime grants applied and verified; `enrollment-reconcile` ran once (all 0); enrollment tables empty; no web service, organization, user, tenant, code or route |
| 10G-C0 | `zaza-migrate` environment only: `ZAZA_DB_NAME = "postgres"` (the migrator's maintenance connection for provisioning, §7a) and `ZAZA_TENANT_RUNTIME_ROLE = "zaza_app"`; plan 0 add / 1 change / 0 destroy; `db-status` (read only) reached `postgres`, `control-db-status` still c0003; no tenant created |
| 10G-C | **first internal organization and tenant**, through the job: `zaza-internal` ("ZaZa Internal Pilot", `Asia/Dhaka`, ACTIVE); server-generated tenant database `zaza_t_7jihzm6q6wvdogkv535q45mhdy` in `asia-south1`, READY at `0004_manager_dashboard_auth`; access boundary, default privileges, resolver and pool manager verified live; second `tenant-provision` idempotent; no user, membership, employee, device, code or route |
| 10G-D | **backup and PITR restore drill: PASS** ([RECOVERY_RUNBOOK.md](RECOVERY_RUNBOOK.md)): on-demand backup `1793272723324` (kept) restored to the temporary instance `zaza-restore-drill-20261029a`, and PITR to `2026-10-29T10:30:00Z` into `zaza-pitr-drill-20261029a`; both held the control plane and the first tenant with correct schema, directory linkage, roles and grants; both deleted; source never overwritten and unchanged |
| 10G-E-A | **code only**: tenant-aware SaaS employee operator commands (`saas-employee-create/list/disable/enable --slug`, resolved through the control plane; see [ENROLLMENT.md §1a](ENROLLMENT.md)); not deployed, production unchanged |

**Not created:** no Cloud Run web service, no public application URL, no
SaaS user, membership, employee, device or enrollment code, no customer, no
employee traffic. Exactly one tenant database exists: `zaza_t_7jihzm6q6wvdogkv535q45mhdy` (internal
pilot, empty). The 10G-D restore drill passed (instance-level).

Google Cloud project `zaza-employee-monitor`, region `asia-south1`,
PostgreSQL **16** (the version every local and in-image test runs against).

## 1. Architecture

```
            employee agents / browsers (HTTPS)
                           │
                 Cloud Run: zaza-server          (max N instances, min 0)
                 runtime identity zaza-run
                           │  Cloud SQL connection on the SERVICE
                           │  → Unix socket /cloudsql/PROJECT:REGION:INSTANCE
                           ▼
        ┌──────────── Cloud SQL for PostgreSQL 16 (one instance) ───────────┐
        │  zaza_control     organizations, users, memberships,              │
        │                   tenant_databases, control_audit_logs            │
        │  zaza_t_<id A>    tenant A: employees, devices, sessions, …       │
        │  zaza_t_<id B>    tenant B                                        │
        │  zaza_t_<id …>    one database per customer                       │
        └───────────────────────────────────────────────────────────────────┘
                           ▲
                 Cloud Run job: zaza-migrate      (explicit runs only)
                 identity zaza-migrate (migration/provisioning role)
```

- **One instance for the pilot** keeps the cost reasonable. Every customer
  still has its own database (the Phase 10C isolation boundary).
- **The control database** (`zaza_control`) holds no activity, no
  sessions or periods, no screenshots or content, no CRM/task data, no
  database passwords and no connection strings.
- **Tenant databases** keep the server-generated names `zaza_t_` + 26
  random base32 characters. A name is never derived from an organization
  name, a slug, employee input or an HTTP parameter.

## 2. Connection model

| Mode | Setting | Used by |
|---|---|---|
| Cloud SQL socket (production) | `ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME=zaza-employee-monitor:asia-south1:<instance>` | Cloud Run service and job |
| Control DB on the socket | `ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME=…` | same |
| TCP (unchanged) | `ZAZA_DB_HOST` / `ZAZA_DATABASE_URL` | local development, tests, the historical Windows deployment |

- The instance connection name is validated strictly
  (`deskmate/zaza_server/cloudsql.py`): `PROJECT:REGION:INSTANCE`, lower
  case, no paths, ports, URLs or extra colons. The server turns it into the
  socket directory `/cloudsql/<name>` and gives that to libpq as `host`.
- It cannot be combined with `ZAZA_DB_HOST` or `ZAZA_DATABASE_URL`: on
  Cloud Run the socket is the **only** database address.
- Every host value is validated too (`DatabaseSettings`): a host name, an
  IP address or an absolute socket directory, never a URL, list or
  key=value string.
- **Nothing request-controlled reaches a connection.** HTTP handlers never
  build database settings (a test scans `app.py`, `auth.py`, `service.py`
  and the dashboard). Tenant connections take **only** a resolved
  `TenantContext` (§4).
- The Cloud SQL connector authenticates the service's IAM identity and
  encrypts the traffic. The instance is configured with
  `connector_enforcement = REQUIRED`: only connectors may connect, and no
  authorized networks exist. TLS settings in the app are not used on the
  socket.
- **No service-account JSON key** is created or used. `check-config
  --cloud-run` fails if `ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE` is set.

## 3. Passwords: Secret Manager files

For the first production deployment, PostgreSQL password authentication is
used. Passwords never appear in source code, the image, Terraform variables
or Terraform state:

| Secret (Secret Manager) | Mounted at | Read via |
|---|---|---|
| `zaza-db-app-password` | `/secrets/db-app/password` | `ZAZA_DB_PASSWORD_FILE` (web service, tenant runtime role) |
| `zaza-db-control-app-password` | `/secrets/db-control/password` | `ZAZA_CONTROL_DB_PASSWORD_FILE` (web service, control role) |
| `zaza-db-migrator-password` | `/secrets/db-migrator/password` | both `_PASSWORD_FILE` variables of the migration job |

- Terraform creates the secret **containers** and grants each identity
  access to its own secrets only. The **values** are generated and added
  out of band in Phase 10E, e.g. by piping a random password straight into
  `gcloud secrets versions add --data-file=-` and into `ALTER ROLE … PASSWORD`.
- `ZAZA_DB_PASSWORD` / `ZAZA_CONTROL_DB_PASSWORD` environment variables
  still work for local development; the Cloud Run preflight refuses them.
- **Redaction:** passwords are excluded from `repr()`, `display()` and every
  error message; driver errors are scrubbed; the log formatter redacts both
  database passwords; migration results are scrubbed.

**Later hardening — IAM database authentication.** Cloud SQL can
authenticate the service account itself as a database user, removing
passwords entirely. It needs `cloudsql.iam_authentication=on`, IAM database
users, and either the Cloud SQL Python Connector or the connector's
automatic IAM login. It is deliberately not in 10D: it adds a dependency
and changes the role design. The connection layer is ready for it, since
only the server-side settings would change.

## 4. Tenant connection manager

`deskmate/zaza_server/tenancy/connections.py`:

```
TenantContext (issued only by TenantResolver)
   → TenantRepositoryManager.repository(context)        thread-safe, bounded cache
   → tenant_database_settings(server base, context)     host/socket, user, password: SERVER config
                                                        database name: the validated context value
   → PostgresRepository (the existing production repository, no duplicated SQL)
```

- **Lazy:** no pool exists because an organization exists. The first
  request for a tenant opens its pool.
- **Small:** `min_size` is always 0. `ZAZA_TENANT_POOL_MAX` defaults to 2
  connections per tenant, hard limit 4.
- **Bounded:** `ZAZA_TENANT_POOL_CACHE_MAX` defaults to 8 open tenant pools
  per instance, hard limit 64. Opening one more closes the least recently
  used pool that is not in use. If all of them are in use, the request
  fails fast with `TenantPoolsExhausted` (HTTP 503 later) instead of
  exceeding the budget.
- **Idle eviction:** `ZAZA_TENANT_POOL_IDLE_SECONDS` defaults to 300
  (10–3600). Idle pools are closed on the next access and by an optional
  reaper thread. psycopg also closes idle connections inside a pool.
- **Verified target:** after opening, the manager checks
  `current_database()` against the context's database name and refuses an
  out-of-date schema (`serve`'s existing rule).
- **Broken connections:** tenant and control pools check each connection
  before handing it out, so a dropped one is replaced. A database failure
  while a pool is in use retires that tenant's pool only; it is rebuilt,
  for the same tenant, on next use.
- **Isolation:** pools are keyed by (organization id, database name). A
  failing tenant never touches another tenant's pool. `discard(org)` closes
  a tenant's pool at once, e.g. right after suspension. A suspended or
  non-READY tenant cannot obtain a context, so it never gets a pool.
- **Shutdown:** `close()` closes every pool and refuses new ones.

**Cloud Run scale.** The cache is **per Cloud Run instance**. It is an
optimisation, not coordination: no correctness depends on it, and each
instance opens its own pools independently. No Redis or shared pooler is
used for connection pooling.

The manager is not yet wired into request handling: SaaS login, enrollment
and per-request tenant routing come in later phases. Today `serve` still
opens the single database named by `ZAZA_DB_NAME`.

## 5. Connection budget

```
max_instances × (tenant_pool_cache_max × tenant_pool_max + control pool 2 + server pool db_pool_max)
   ≤ ZAZA_DB_CONNECTION_BUDGET  <  PostgreSQL max_connections
```

The budget is what the database grants the web service, after keeping a
reserve for the migration job, administration and Cloud SQL itself.

**Initial (pilot) values:**

`3 × (8 × 2 + 2 + 2) = 60 ≤ 70 < 100`

- `max_instances`: 3
- `max_connections`: 100
- reserve of 30 for the migrator, admin and Cloud SQL

`check-config --cloud-run` computes this and **fails** when the total
exceeds `ZAZA_DB_CONNECTION_BUDGET`. It also requires
`ZAZA_CLOUD_RUN_MAX_INSTANCES`, because Cloud Run must never be assumed to
run one instance. Raising `max_instances` requires recomputing the budget,
and Terraform keeps the two values next to each other.

## 6. Database roles (created in Phase 10E-B, with SQL)

| Role | Purpose | Attributes | Privileges |
|---|---|---|---|
| `zaza_app` | web runtime, tenant databases | LOGIN only: **no** SUPERUSER, CREATEDB, CREATEROLE | CONNECT/TEMP on each tenant database (granted at provisioning); SELECT/INSERT/UPDATE/DELETE on tenant tables, USAGE/SELECT on sequences; no DDL, owns nothing |
| `zaza_control_app` | web runtime, control database | LOGIN only | DML on control tables (the audit trigger keeps the log append-only) |
| `zaza_migrator` | migrations and tenant provisioning (Cloud Run job only) | LOGIN, CREATEDB; no SUPERUSER | owns schemas/tables; runs `control-migrate`, `migrate`, `tenant-migrate`; creates tenant databases |
| `zaza_reporting` (when needed) | read-only reporting / exports | LOGIN only | SELECT on report tables of the tenants it is granted |

- The **web process never has schema or database-creation privileges.**
  Tenant provisioning is a separate, privileged path (§8).
- **Per-tenant privileges.** Each new tenant database is created with
  `REVOKE ALL … FROM PUBLIC`, and only the runtime role is granted
  `CONNECT`. Per-tenant **roles** (a credential for A cannot even connect to
  B) remain a later option. They are not created now: hundreds of
  permanent roles would need their own secret and rotation design.
- The local proof creates a real runtime role with exactly these
  attributes. It shows that the role works through the tenant manager, and
  that `CREATE TABLE` and `CREATE DATABASE` are refused.

**How the roles were created (Phase 10E-B).**
- The administration connection used the Cloud SQL Auth Proxy (a connector,
  IAM-authorized), never an authorized network.
- Passwords were generated in memory and written to Secret Manager over
  stdin.
- Roles were created with client-computed SCRAM-SHA-256 verifiers, so no
  plaintext password reached SQL, logs or command lines.
- The built-in `postgres` password was set for that session only, then
  replaced with a discarded random value. No standing admin password
  exists; reset it through the Cloud SQL Admin API when administration is
  needed.

Role details:
- **Connection limits** follow the budget (§5): `zaza_app` 60,
  `zaza_control_app` 10, `zaza_migrator` 10. None of the three is a member
  of `cloudsqlsuperuser`.
- **`zaza_control`:**
  - CONNECT is revoked from PUBLIC;
  - `zaza_migrator` has CONNECT, TEMP and CREATE on schema `public`;
  - `zaza_control_app` has CONNECT and USAGE only, and gets SELECT on
    future control tables through default privileges.

**Right after the first `control-migrate`**, grant the control runtime role
exactly what `PostgresControlRepository` uses (as `zaza_migrator`, the table
owner):

```sql
GRANT INSERT, UPDATE ON organizations, users, organization_memberships, tenant_databases TO zaza_control_app;
GRANT INSERT ON control_audit_logs TO zaza_control_app;
```

It gets no DELETE anywhere, and nothing beyond SELECT on
`zaza_control_version`.

## 7. Migrations: explicit only

The Cloud Run web service **never** migrates. Its entrypoint only runs
`serve`, and `serve` refuses an out-of-date schema. A test also checks that
the serve path contains no migration or provisioning calls.

| Command | What |
|---|---|
| `control-migrate` / `control-db-status` | the control chain (`c0001_control_plane`), version table `zaza_control_version` |
| `migrate` / `db-status` | the tenant chain (`0001`…`0004`) on the single `ZAZA_DB_NAME` database |

### 7a. Which database each connection uses (production job)

| Database | Role in ZaZa |
|---|---|
| `zaza_control` (`ZAZA_CONTROL_DB_NAME`) | the control plane |
| `postgres` (`ZAZA_DB_NAME` on `zaza-migrate`) | the instance's maintenance database: only the migrator's initial connection target for instance-level operations (`pg_database`, `CREATE DATABASE`, database grants) in `tenant-provision`. It is **not** a tenant, is **never** migrated and stores **no** ZaZa data |
| `zaza_t_*` | the tenant databases: the server-generated name from the control directory replaces the base name in `tenant-migrate`, provisioning grants and at runtime |

Without `ZAZA_DB_NAME` the base settings fall back to a database called
`zaza`, which does not exist in production; do not create one to satisfy
that fallback. `db-status` on the job reports `postgres` as an "(empty
database)": that is expected, and `migrate` must **never** be run against
it (the job's default command is `control-db-status`).

| `tenant-migrate --organization ID` (repeatable) | the tenant chain on that organization's tenant database, from the control directory |
| `tenant-migrate --all-ready [--continue-on-error]` | every READY tenant, sequentially in a stable order |

`tenant-migrate` (`tenancy/tenant_migrations.py`):

- takes the database name from the control plane and validates it again;
- never accepts a typed database name, and refuses the control database:
  - the name pattern does not match;
  - the name equals the control database;
  - `postgres.migrate.upgrade` refuses databases containing control tables;
- reports one line per tenant (organization, database, before → after,
  or the scrubbed error);
- marks a failed tenant `ERROR`, which takes it out of routing, and stops
  unless `--continue-on-error` is given;
- moves READY or ERROR tenants to READY at the new revision; PROVISIONING
  tenants stay as they are (the provisioning flow marks them READY).

On Cloud Run, these run as the `zaza-migrate` job with the migrator
identity, for example:

`gcloud run jobs execute zaza-migrate --args=tenant-migrate,--all-ready`

The job's default arguments only report status.

## 8. Provisioning boundary (for Phase 10E/10F)

`tenancy/provisioning.py`:

```
organization ACTIVE
  → assign server-generated name (control: PROVISIONING)
  → DatabaseCreator.create_database(name)            CREATE DATABASE, REVOKE … FROM PUBLIC, GRANT CONNECT to runtime role
  → TenantMigrationRunner.migrate(org)               tenant chain to head
  → verify head
  → DatabaseCreator.grant_runtime_access(name)       DML + default privileges for the runtime role
  → mark READY with schema_revision
  any failure → ERROR (provision again to retry; READY is never set half-way)
```

- `PostgresDatabaseCreator` uses plain SQL on a server-configured admin
  connection (the migrator). It quotes identifiers and validates names
  against the tenant pattern.
- Phase 10E decides whether it runs against Cloud SQL directly, or is
  replaced by a Cloud SQL Admin API implementation behind the same
  interface. Nothing calls a Google API today.
- No CLI or HTTP route exposes provisioning yet.

## 9. Production preflight: `check-config --cloud-run`

This is a separate mode. `check-config --production`, the Windows/VPS rules,
is unchanged.

| Check | Rule |
|---|---|
| backend | `postgres`; SQLite and `ZAZA_CONTAINER_ALLOW_SQLITE` refused |
| PORT | if set, 1..65535 (Cloud Run sets it; default 8080 on 0.0.0.0) |
| database / control database | Cloud SQL instance name valid; no `*_DB_HOST` / `*_DATABASE_URL`; user set and not an admin user; password **file** readable; no plaintext password variable |
| pools | tenant limits within bounds; server pool ≤ 4 |
| budget | `ZAZA_CLOUD_RUN_MAX_INSTANCES` (1..100) and `ZAZA_DB_CONNECTION_BUDGET` required; total within budget |
| logs | stdout/stderr; `ZAZA_LOG_FILE` warns (temporary disk) |
| credentials | `ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE` refused |
| http mode | `ZAZA_HTTP_DEPLOYMENT_MODE=cloud-run` required (10F-A) |
| cookies / host | Secure cookies required; `ZAZA_PUBLIC_HOST` missing = FAIL (deployment blocker; `serve` refuses Cloud Run mode without it) |
| legacy dashboard | must be disabled (it is, by policy, in Cloud Run mode) |
| `--connect` | read-only check that the control schema is at head |

It never prints secret contents. It reads password files only to check
that they are non-empty.

## 10. Health and readiness

- `GET /api/v1/sync/health` stays unauthenticated and minimal: status, API
  version and server time.
  - It exposes no database name, tenant, organization or Cloud SQL
    detail; a test checks this.
  - Cloud Run's startup and liveness probes use it.
- **Internal readiness** (design, not exposed): when tenant routing is
  wired in, a readiness signal can check the control database (`SELECT 1`
  plus the schema revision) without enumerating tenants. It must not be a
  public route. Tenant availability is reported per request (503 for that
  tenant), never as a list.

## 10a. Cloud Run HTTP trust model (Phase 10F-A)

`ZAZA_HTTP_DEPLOYMENT_MODE` is an explicit server setting, never inferred
from a request:

| Mode | Used by | Forwarded headers | External scheme | Client address |
|---|---|---|---|---|
| `loopback-proxy` (default) | Windows/VPS behind Caddy (unchanged) | trusted only from loopback peers | from the trusted proxy | the real client (via the trusted proxy) |
| `local` | development | never trusted | the TCP scheme | the TCP peer |
| `cloud-run` | the future web service | **never trusted** (`X-Forwarded-For/Proto/Host`, `Forwarded`) | **https**, by deployment definition (wss for websockets) | **unverified**: the key `unverified`, never a header value |

- **Scheme.** Cloud Run serves the application only over HTTPS and
  terminates TLS in front of the container, so the external scheme is
  https by definition. A client cannot downgrade it, because
  `X-Forwarded-Proto` is ignored. The container itself listens on
  `0.0.0.0:$PORT` inside the platform; that hop is not public HTTPS, and
  `serve` says so.
- **Host.** Host is validated against `ZAZA_PUBLIC_HOST`, exactly as in the
  other modes:
  - case-insensitive, trailing dot and port ignored; anything else is
    400;
  - Terraform fills it with the service's deterministic run.app host
    (`zaza-server-<project number>.asia-south1.run.app`), plus any custom
    domain later;
  - `serve` refuses Cloud Run mode without it.
- **Origin.** `http_security.external_request()` is the one place that
  derives the external scheme, host and origin, and `same_origin()` is the
  one same-origin rule:
  - no Origin header: allowed (the CSRF tokens still apply);
  - otherwise exactly one Origin, equal to the external `https://host`;
  - `http://…`, `null`, other hosts or ports, paths, credentials and
    several values are refused.
- **Client IP.** No authentication, authorization or rate-limit decision
  depends on `X-Forwarded-For` in Cloud Run mode. Source-IP controls belong
  to a future edge layer (Google load balancer plus Cloud Armor).
- **Legacy dashboard.** Never mounted in Cloud Run mode. It signs in with
  per-database `manager_users` and `manager_sessions`, while the SaaS
  identity model (control-plane users and memberships plus tenant routing)
  replaces it. Mounting it now would ship the wrong authentication model.
  Local and loopback-proxy keep the Phase 7/10 rule (PostgreSQL and a
  loopback bind).
- **Login rate limiting.** The in-memory limiter is per process: fine for
  the single legacy server, NOT global across Cloud Run instances.
  `dashboard/ratelimit.py` defines the `RateLimiter` boundary. The SaaS
  login must use shared state (database- or edge-backed); no Redis was
  added.
- **uvicorn** `proxy_headers` stays off; forwarded headers are interpreted
  only in `http_security.py`.
- **Still a single-database API.** `create_app()` takes ONE repository, and
  device authentication runs against it. No organization header or
  parameter, database name or slug routing exists, and tests forbid them.
  The SaaS path will be: authenticated device credential → TenantResolver →
  TenantContext → TenantRepositoryManager → that tenant's repository → sync
  service.

## 11. Backups and point-in-time recovery

Terraform (`deploy/gcp/terraform/cloudsql.tf`) sets:

- automated daily backups (window 20:30 UTC = 02:00 IST), stored in
  `asia-south1`, **14** retained;
- **point-in-time recovery** with **7 days** of transaction logs;
- **deletion protection** at both the Terraform level and the Cloud SQL API
  level; the control database resource uses `deletion_policy = ABANDON`;
- maintenance window Sunday 21:00 UTC (stable track), and Query Insights
  without client addresses or application tags.

No restore is performed in 10D. **Phase 10E-B verified these on the live
instance after creating it** (a restore drill is still due before the
employee pilot):

- backups are enabled and the first backup succeeded;
- PITR is on and the retention matches;
- deletion protection is on.

**Phase 10G-D drilled both paths (PASS):** an on-demand backup restored to
a NEW temporary instance, and PITR into another NEW temporary instance; each
held `zaza_control` and the first tenant intact, and both were deleted.
Recovery is instance-level; the procedures, including the planned
single-tenant path, are in [RECOVERY_RUNBOOK.md](RECOVERY_RUNBOOK.md).
Per-tenant restore options are in SAAS_TENANCY.md §19.

## 12. A dedicated instance for a large customer, later

A tenant descriptor today names only its database; the server supplies the
instance and credentials. To move one customer to its own instance later:

1. Add a server-side **placement** key to `tenant_databases`, e.g.
   `shared-1` or `dedicated-acme`. It is a label, never a host.
2. Map each placement to a **server-configured** base `DatabaseSettings`
   (its own Cloud SQL socket, role and secret file), keyed in
   configuration.
3. Have `TenantRepositoryManager` choose the base by the context's
   placement.

Authorization (`TenantResolver` → `TenantContext`) does not change, and no
host ever comes from the control database or a request. Not implemented
now: nothing needs it yet.

## 13. Infrastructure as code (`deploy/gcp/terraform/`)

Phase 10D checked it offline (`terraform fmt -check`, `init
-backend=false`, `validate`; Terraform 1.9.8, google provider 7.46.2, lock
file committed). It has since been applied in stages from remote state
(10E-A/B/C, see Status); the web service is still gated off.

| File | Defines |
|---|---|
| `versions.tf` | Terraform/provider constraints, default labels, the GCS backend |
| `variables.tf` | project `zaza-employee-monitor`, region `asia-south1`, sizes and budget; **no secrets**; no default instance name |
| `main.tf` | required APIs, Artifact Registry `zaza` (immutable tags), service accounts `zaza-run` / `zaza-migrate`, `roles/cloudsql.client`, secret containers + per-secret accessor bindings |
| `cloudsql.tf` | PostgreSQL 16 instance, backups/PITR/deletion protection, connector-only access, `max_connections`, the `zaza_control` database |
| `cloud_run.tf` | web service (Cloud Run HTTP mode, run.app host, Cloud SQL socket, password secrets as files, max instances, probes; `deploy_service = false`) and the `zaza-migrate` job (`deploy_migration_job`) |
| `build.tf` | `zaza-build` identity and the private build-source bucket (10E-C) |
| `outputs.tf` | non-secret identifiers |
| `terraform.tfvars.example` | example inputs (copy to the git-ignored `terraform.tfvars`) |

Deliberately **not** in Terraform:

- tenant databases (application lifecycle);
- database roles and passwords (SQL plus Secret Manager, so no secret
  enters state);
- secret versions;
- service-account keys;
- load balancers, Cloud Armor and DNS (the only Terraform bucket is the private build-source bucket).

`.gitignore` excludes `*.tfstate*`, `*.tfplan`, `*.tfvars` and `.terraform/`.

**What Phase 10E did, in order (done):**

1. Create a versioned state bucket and enable the GCS backend.
2. Choose `sql_instance_name`.
3. Run `terraform plan`, review it, then `apply` with
   `deploy_service = false`.
4. Create the roles with SQL as the built-in admin user. Generate each
   password directly into its Secret Manager secret.
5. Build the image, push it, and record its digest.
6. Run the migration job with `control-migrate`.
7. Set `container_image` and `deploy_migration_job = true` (NOT `deploy_service`), then plan and apply.
8. Verify backups, PITR and deletion protection, `check-config --cloud-run
   --connect`, and health.

The Cloud Run HTTP foundation followed in 10F-A (§10a). SaaS login,
credential-derived tenant routing and enrollment remain separate phases
before any customer traffic.

## 14. Product roadmap after the production foundation (NOT built)

These are approved product directions. They will be built on top of this
foundation, inside tenant databases, so nothing needs redesigning:

1. **Live Team**: the current state of each employee for managers.
2. **Teams and manager scopes**: which employees a manager may see.
3. **CRM clients, projects and tasks.**
4. **Idle-context prompts, employee Pause/Resume and My Day corrections**:
   the employee explains or corrects their own time.
5. **Messaging and task comments.**
6. **Document uploads and work submissions.**
7. **AI task suggestions.**

None of them exists in code. The privacy guarantees are unchanged:

- no screenshots, screen recording or OCR;
- no keystrokes or text, clipboard, microphone or webcam;
- no full URLs, browser history, search queries or page contents;
- no stealth monitoring.

ACTIVE means observed computer activity, not proof of productivity. IDLE
means no recent input, not proof of non-work.
