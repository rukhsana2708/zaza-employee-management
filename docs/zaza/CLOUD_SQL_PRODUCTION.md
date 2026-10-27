# ZaZa production database foundation: Cloud Run + Cloud SQL (Phase 10D)

> Phase 10D designed this; **Core infrastructure created in Phase 10E-B (2026-10-28):**
> - **Cloud SQL `zaza-prod-pg16`** (PostgreSQL 16, Enterprise, `db-custom-1-3840`,
>   ZONAL in asia-south1-c, 20 GB SSD with auto-resize to 200 GB). It has
>   connector enforcement REQUIRED, no authorized networks, deletion
>   protection, 14 daily backups, 7-day PITR and `max_connections` 100.
> - **The empty `zaza_control` database.**
> - **Artifact Registry `zaza`** (immutable tags, empty).
> - **Service accounts `zaza-run` and `zaza-migrate`**: `cloudsql.client`
>   only, no keys.
> - **Three Secret Manager secrets**, each with version 1.
> - **Database roles `zaza_app`, `zaza_control_app` and `zaza_migrator`.**
> 
> Terraform state is in the private, versioned bucket
> `zaza-employee-monitor-tfstate-e33ac2a6`.
> 
> **Still NOT created:** no Cloud Run service or job, no image pushed, no
> tenant database, no customer, no employee traffic. The control schema is
> not migrated yet.

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
| cookies / host | Secure cookies required; `ZAZA_PUBLIC_HOST` warns until known |
| proxy | always warns: Cloud Run front-end proxy support is a later phase (fail-closed today) |
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

## 11. Backups and point-in-time recovery

Terraform (`deploy/gcp/terraform/cloudsql.tf`) sets:

- automated daily backups (window 20:30 UTC = 02:00 IST), stored in
  `asia-south1`, **14** retained;
- **point-in-time recovery** with **7 days** of transaction logs;
- **deletion protection** at both the Terraform level and the Cloud SQL API
  level; the control database resource uses `deletion_policy = ABANDON`;
- maintenance window Sunday 21:00 UTC (stable track), and Query Insights
  without client addresses or application tags.

No restore is performed in 10D. **Phase 10E must verify, after creating
the instance:**

- backups are enabled and the first backup succeeded;
- PITR is on and the retention matches;
- deletion protection is on.

A restore drill belongs before the employee pilot. Per-tenant restore
options are in SAAS_TENANCY.md §19.

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

The Terraform is a definition only. It was checked offline with
`terraform fmt -check`, `init -backend=false` and `validate` (Terraform
1.9.8, google provider 7.46.2, lock file committed). It has **never been
planned or applied**.

| File | Defines |
|---|---|
| `versions.tf` | Terraform/provider constraints, default labels, commented GCS backend |
| `variables.tf` | project `zaza-employee-monitor`, region `asia-south1`, sizes and budget; **no secrets**; no default instance name |
| `main.tf` | required APIs, Artifact Registry `zaza` (immutable tags), service accounts `zaza-run` / `zaza-migrate`, `roles/cloudsql.client`, secret containers + per-secret accessor bindings |
| `cloudsql.tf` | PostgreSQL 16 instance, backups/PITR/deletion protection, connector-only access, `max_connections`, the `zaza_control` database |
| `cloud_run.tf` | service (Cloud SQL socket volume, password secrets as files, max instances, health probes) and the `zaza-migrate` job; both gated by `deploy_service = false` |
| `outputs.tf` | non-secret identifiers |
| `terraform.tfvars.example` | example inputs (copy to the git-ignored `terraform.tfvars`) |

Deliberately **not** in Terraform:

- tenant databases (application lifecycle);
- database roles and passwords (SQL plus Secret Manager, so no secret
  enters state);
- secret versions;
- service-account keys;
- load balancers, Cloud Armor, DNS and Cloud Storage.

`.gitignore` excludes `*.tfstate*`, `*.tfplan`, `*.tfvars` and `.terraform/`.

**What Phase 10E does, in order:**

1. Create a versioned state bucket and enable the GCS backend.
2. Choose `sql_instance_name`.
3. Run `terraform plan`, review it, then `apply` with
   `deploy_service = false`.
4. Create the roles with SQL as the built-in admin user. Generate each
   password directly into its Secret Manager secret.
5. Build the image, push it, and record its digest.
6. Run the migration job with `control-migrate`.
7. Set `container_image` and `deploy_service = true`, then plan and apply.
8. Verify backups, PITR and deletion protection, `check-config --cloud-run
   --connect`, and health.

Cloud Run proxy/HTTPS-origin support, SaaS login and enrollment remain
separate phases before any customer traffic.

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
