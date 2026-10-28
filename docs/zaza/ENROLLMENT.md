# Tenant provisioning and one-time device enrollment (Phase 10G-A)

> **Status:**
> - Phase 10G-A (code, with the durable-reconciliation repair) is
>   approved.
> - **Phase 10G-B applied control migration `c0003_device_enrollment` in
>   production**, with the §7 runtime grants. The migration job runs image
>   `sha256:b7ad3a892306…` (commit `778488c`).
> - The database-enforced status model was verified live, and
>   `enrollment-reconcile` ran once (nothing to do).
> - **The enrollment tables are empty.** No organization, SaaS user,
>   tenant, enrollment code, device route or token exists in production.
> - The web service is not deployed, and the edge (§8) doesn't exist yet.

Code:
- `deskmate/zaza_server/tenancy/enrollment.py`: codes, store, saga service;
- `tenancy/provisioning.py`: tenant provisioning;
- `tenancy/saas_web.py`: `POST /api/v1/enroll`;
- `__main__.py`: operator commands.

## 1. Customer onboarding (trusted operator path)

```
saas-org-create --slug acme --name "Acme Ltd"                 # ACTIVE, no tenant DB, nothing public
tenant-provision --slug acme [--region asia-south1]           # server-generated zaza_t_… -> READY
saas-user-create --email owner@acme.example --name "…" --no-password
saas-membership-add --email owner@acme.example --slug acme --role OWNER
(saas-user-set-password later: hidden prompt; invitations are a later phase)
```

In production these run as the `zaza-migrate` job, with the migrator
identity.

**Tenant provisioning:**
1. assign a server-generated `zaza_t_` name (never operator input);
2. `CREATE DATABASE` if absent;
3. **restrict access**: `REVOKE ALL … FROM PUBLIC` and `GRANT CONNECT,
   TEMPORARY` to the runtime role, on **every** attempt (idempotent);
4. tenant migrations, then verify the head;
5. grant the runtime role DML only;
6. mark READY.

Any failure leaves `ERROR`; running `tenant-provision` again resumes it,
re-uses the same database, and repairs the grants. It can never create a
second database for the organization: there is one tenant database row per
organization (primary key). A READY tenant is a no-op.

## 2. Enrollment codes

- **Format:** `ZZE-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX`: 160
  cryptographically random bits in base32, case-insensitive, with dashes
  and spaces ignored. Not an OTP.
- **Storage:** `device_enrollment_codes` keeps
  `SHA-256("zaza-enrollment-code-v1\0" + code)`, never the code. Listings
  (`enrollment-code-list`, the `EnrollmentCode` value) never contain the
  code or its hash.
- **Shown once:** `enrollment-code-create` prints the code once and says so.
  It is never logged, never put in a URL, and never accepted on a command
  line later.
- **Lifetime:** expiry (default 72 h, at most 720 h), single use,
  revocable while unused (`enrollment-code-revoke`). A database trigger
  forbids any change out of COMPLETED, REVOKED, EXPIRED or FAILED, so a
  code can never be reactivated.
- **Server-side authority.** The code record holds the organization, the
  intended **employee** (who must already exist in that tenant and be
  active), an optional intended device name, and a `--reenroll` flag. The
  client sends only the code, its device id and an optional display name
  (used only if the operator gave none).

## 3. The enrollment saga

The control database and the tenant database can't commit atomically
together. The flow is therefore an explicit saga:

| Step | Database | What | If it fails or the process dies here |
|---|---|---|---|
| 1 CLAIM | control | `UPDATE … SET status='CLAIMED', device_id, token_id WHERE code_hash=… AND status='ISSUED' AND expires_at>now()`. One atomic statement: with N concurrent requests, exactly one claims. The `token_id` is chosen now and recorded. | Nothing claimed (503); the code stays usable. |
| 2 RESOLVE | — | `TenantResolver.for_enrollment(organization)`: ACTIVE organization, READY tenant. | FAILED `tenant_unavailable` (403). No token can exist. |
| 3 TENANT | tenant | Employee exists and is active. Create the device if new; issue the token with the recorded `token_id` (existing implementation: `zzd_` + 256 bits, SHA-256 stored). | Refused before the token: FAILED (403/409/503). The token write itself fails (its INSERT may have committed): **CLEANUP_PENDING** `token_uncertain`. A crash leaves CLAIMED. |
| 4 COMPLETE | control, **one transaction** | CLAIMED → COMPLETED + `route_hash`, INSERT the device credential route, audit. | Immediate **compensation**: revoke the token by `token_id`. Revocation confirmed: FAILED `route_failed`. Revocation failed: **CLEANUP_PENDING** `route_failed`. Either way 503. |

### Status model (c0003)

```
ISSUED ──claim──> CLAIMED ──complete──> COMPLETED
  │                 │  └──refused before any token / token confirmed revoked──> FAILED
  │                 └──token may exist, not confirmed revoked──> CLEANUP_PENDING ──cleanup confirmed──> FAILED
  └──> REVOKED | EXPIRED
```

- **Terminal:** COMPLETED, REVOKED, EXPIRED and FAILED. A database trigger
  refuses any change out of them.
- **CLEANUP_PENDING is not terminal,** but it can only move on to FAILED:
  never back to ISSUED or CLAIMED, and never to COMPLETED.
- CLEANUP_PENDING and FAILED always carry a `failure_reason`: one of
  `interrupted`, `route_failed`, `token_uncertain`, `tenant_unavailable`,
  `employee_unavailable`, `device_exists`, `device_disabled` or `timeout`.
- **FAILED means the credential cleanup is complete:** the claim's token
  was revoked, was already revoked, or was never issued.

**Why no usable orphan can survive:** the sync API routes a token only
through `device_credential_routes`, and a route is written only in step 4,
atomically with COMPLETED. An interruption before step 4 leaves at most a
tenant token **without a route**, which is unusable. It is still revoked,
and nothing marks the enrollment done until that is confirmed.

### Reconciliation (`enrollment-reconcile`, safe to run any number of times)

1. Unused codes past their expiry become EXPIRED.
2. **Interrupted claims first become cleanup-pending:** CLAIMED codes older
   than 15 minutes move to CLEANUP_PENDING (`interrupted`) in one atomic
   UPDATE.
3. **Every** CLEANUP_PENDING enrollment is retried on **every** run. Its
   tenant is resolved and `revoke_token(token_id)` is called:
   - **revoked now, already revoked, or never existed** (a crash between
     claim and token): the cleanup is confirmed, and the enrollment
     becomes FAILED;
   - **tenant unreachable** (down, not resolvable, or the organization not
     ACTIVE): the enrollment **stays CLEANUP_PENDING**. An unreachable
     tenant never causes cleanup to be abandoned: there is no retry limit,
     and the next run tries again;
   - token revoked but the FAILED state can't be recorded (control down):
     it stays CLEANUP_PENDING, and the next run finds the token already
     revoked and records FAILED.

`revoke_token(token_id)` is idempotent. It returns True if this call
revoked an ACTIVE token, and False if the token was already revoked or
never existed; either way it guarantees no ACTIVE token has that id. It
*raises* when storage is unavailable, so an outage can never be mistaken
for "nothing to revoke".

Operator output separates:
- expired codes;
- newly interrupted enrollments;
- cleanup completed, and how many tokens were revoked now;
- cleanup still pending, and how many of those had an unreachable tenant.

The command exits 1 while anything is still pending. It never prints
database names, token hashes, tokens or code hashes.

### Completion vs. reconciliation

Both transitions out of CLAIMED are single conditional UPDATEs on the same
row (`… WHERE status = 'CLAIMED'`), so exactly one wins:
- **completion wins:** COMPLETED, which is terminal. Reconciliation only
  selects CLAIMED and CLEANUP_PENDING rows, so it never touches the
  legitimate credential.
- **reconciliation wins:** CLEANUP_PENDING. Completion then finds no
  CLAIMED row, writes **no route**, revokes its own token and answers 401.
  Reconciliation revokes the same token (idempotently), and the
  enrollment ends FAILED with the token REVOKED.

Late issuing is closed too:
- a still-running enrollment refuses to issue its token once half the
  reconcile delay has passed since its claim (`timeout`, FAILED, no token);
- tenant statements time out after `ZAZA_DB_STATEMENT_TIMEOUT_MS` (15 s),
  far below the 450 s margin;
- so no token can appear after reconciliation has confirmed it absent.

**Residual edge, documented:** if the completion transaction commits but
its acknowledgement is lost, compensation still revokes the token. The
result is COMPLETED with a route to a revoked token: safe (it authenticates
nothing), and the agent saw 503, so it never had the token anyway.

The raw token is never persisted for recovery. The recovery metadata is
only `token_id`, `organization_id` and `device_id`.

**Lost response:** if the agent never receives the token after step 4,
the code is used, and a retry gets the generic refusal. The operator issues
a re-enrollment code (`--reenroll`) and, after the switch, revokes the
unused token. There is no "replay the token" path.

## 4. Re-enrollment and device rules

| Device id in the tenant | Plain code | `--reenroll` code |
|---|---|---|
| new | device + first token | device + first token |
| exists, same employee, ACTIVE, **with** an ACTIVE token | **409** (already enrolled) | **additional** token; the old one keeps working until the operator revokes it |
| exists, same employee, ACTIVE, **no** ACTIVE token | token issued (recovery, see below) | token issued |
| exists, other employee | 409 | 409 |
| exists, DISABLED | 409 | **403**: never silently re-enabled |

**Device rows left by interrupted enrollments (decision: keep and
recover).** An enrollment that dies after creating the device but before
completing leaves a device row. A device row is not a credential.
Deleting it would need a cross-database check that no token, record or
audit entry depends on it, so it is kept. A later **plain** code for the
**same employee** and device id recovers cleanly once the device has no
ACTIVE token, that is, once the cleanup has revoked the interrupted token
or none was ever issued. No `--reenroll` bypass is needed. While a
cleanup-pending token is still ACTIVE, a plain code is refused (409) until
reconciliation has revoked it. A device is never reassigned to another
employee, with either kind of code.

Device ids use the sync protocol's grammar, `[A-Za-z0-9._:-]{1,128}`
(`domain.identifier`). Spaces, control characters, slashes, quotes and
over-long values are refused with 422.

## 5. HTTP API (`create_saas_app` only; not deployed)

`POST /api/v1/enroll` with JSON `{"enrollment_code", "device_id",
"device_name"?}`:
- strict: unknown fields such as `organization_id`, `tenant`, `employee_id`,
  `database` or `role` give 422;
- at most 4 KiB; the code is never read from the query string;
- 200 returns `{"device_id", "employee_id", "device_token", "api_version"}`
  with `Cache-Control: no-store`. It never contains a database name, host,
  organization id or control-plane detail, and the token is never logged.

| Status | Meaning |
|---|---|
| 401 | `invalid, expired or already used enrollment code` (unknown, expired, revoked, used and malformed all look the same) |
| 403 | organization or tenant unavailable, employee inactive, device disabled |
| 409 | device already enrolled |
| 422 | invalid request or device id |
| 429 | rate limited |
| 503 | storage unavailable |

**Rate limiting:** shared control-database counters (the 10F-B table), per
code digest (5 per hour) and one global bucket (60 per minute;
`ZAZA_ENROLLMENT_MAX_ATTEMPTS_GLOBAL`). No client IP. Guessing a 160-bit
code is infeasible anyway; the limits protect the databases.

## 6. Isolation and routing (unchanged 10F-B model)

Enrollment writes the device and token only into the tenant the CODE
resolves to. Afterwards, sync is the normal path: token → route →
organization → tenant → `authenticate_request` inside that tenant. A route
alone still authenticates nothing. Tests use the same `employee-1` and
`device-1` in tenants A and B: code A enrolls only in A, code B only in B,
and each token syncs only in its own tenant.

## 7. Production privileges (applied in Phase 10G-B)

These grants were applied as `zaza_migrator`, the table owner, after
`control-migrate` to c0003. They were verified in production:
- the runtime's exact statements are permitted (in rolled-back
  transactions);
- 20 forbidden operations are refused;
- `zaza_control_app` has these table privileges and UPDATE on exactly
  these 7 columns, and nothing more.

The grants:

```sql
GRANT SELECT ON device_enrollment_codes TO zaza_control_app;
GRANT UPDATE (status, claimed_at, completed_at, device_id, token_id, route_hash, failure_reason)
  ON device_enrollment_codes TO zaza_control_app;          -- claim / complete / fail only (column-level)
GRANT INSERT ON device_credential_routes TO zaza_control_app;  -- route written at completion
```

- The runtime role gets no INSERT on codes (creation is an operator action),
  no DELETE anywhere new, and no UPDATE or DELETE on routes (revocation is
  an operator action).
- The terminal-state trigger blocks reactivation even for a role with
  UPDATE.
- Operator actions (create, revoke, reconcile, route revoke) run as the
  migrator in the `zaza-migrate` job.

**No separate enrollment role.** The web runtime role needs only the narrow
grants above. A new privileged credential would add a secret without
reducing what the web service can do: it must complete enrollments either
way.

Tenant side: enrollment uses the existing tenant runtime role's DML
(devices, tokens, audit). Nothing new is needed.

## 8. Edge protection before public traffic (design only; NOT created)

Both `/app/login` and `/api/v1/enroll` must sit behind:

```
Internet -> External Application Load Balancer (managed TLS, custom hostname)
         -> Cloud Armor (rate-based rules for /app/login and /api/v1/enroll,
                         preconfigured WAF rules, optional geo/IP lists)
         -> serverless NEG -> Cloud Run zaza-server
```

- Set Cloud Run ingress to **`internal-and-cloud-load-balancing`**, and
  **disable the default `run.app` URL** (Cloud Run `default_uri_disabled`).
  Otherwise the run.app endpoint would bypass Cloud Armor.
- Move `ZAZA_PUBLIC_HOST` from the run.app host to the load balancer's
  hostname. Host validation then refuses anything else.
- Client IPs seen by Cloud Armor become usable edge signals. The
  application still never trusts `X-Forwarded-For` (10F-A).
- Terraform for these (`google_compute_region_network_endpoint_group`,
  backend service with `security_policy`, URL map, managed certificate,
  forwarding rule) belongs to the deployment phase. None of it exists yet.

## 9. Verified locally

| Test file | Database | What |
|---|---|---|
| `test_enrollment.py` | none | the real service, resolver, manager and SaaS app with in-memory stores |
| `test_enrollment_postgres.py` | PostgreSQL 16, in a deterministic container network | provisioning A/B, recovery, c0003 constraints and CLEANUP_PENDING transitions, A/B enrollment and sync, 16-way concurrent use of one code, reconciliation, compensation, a **real tenant outage** (`ALLOW_CONNECTIONS false`) between the CLEANUP_PENDING transition and the revocation across three runs (the fourth finishes), route failure plus revocation failure, completion racing reconciliation (both orders observed), device-row recovery |
