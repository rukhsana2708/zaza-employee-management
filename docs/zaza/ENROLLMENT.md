# Tenant provisioning and one-time device enrollment (Phase 10G-A)

> **Status: code and tests only.**
> - Control migration `c0003_device_enrollment` is **not applied in
>   production**; production is at `c0002_saas_auth_routing`.
> - No organization, SaaS user, tenant, enrollment code, device route or
>   token exists in production, and the web service is not deployed.

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
| 2 RESOLVE | — | `TenantResolver.for_enrollment(organization)`: ACTIVE organization, READY tenant. | FAILED `tenant_unavailable` (403). |
| 3 TENANT | tenant | Employee exists and is active. Create the device if new; issue the token with the recorded `token_id` (existing implementation: `zzd_` + 256 bits, SHA-256 stored). | Before the token: FAILED (403/409/503). A crash leaves CLAIMED with no token. |
| 4 COMPLETE | control, **one transaction** | CLAIMED → COMPLETED + `route_hash`, INSERT the device credential route, audit. | Immediate **compensation**: revoke the token (by `token_id`), FAILED `route_failed` (503). |

**Why no usable orphan can survive:** the sync API routes a token only
through `device_credential_routes`, and a route is written only in step 4,
atomically with COMPLETED. An interruption before step 4 leaves at most a
tenant token **without a route**, which is unusable. It is revoked by
compensation, or later by **`enrollment-reconcile`**. That command marks
CLAIMED codes older than 15 minutes FAILED (`interrupted`), revokes their
recorded `token_id` in the tenant, and expires unused codes past their
expiry.

Completion and reconciliation can't both win:
- completion requires status CLAIMED;
- reconciliation flips CLAIMED to FAILED first;
- a completion that loses revokes its own token.

The raw token is never persisted for recovery.

**Lost response:** if the agent never receives the token after step 4,
the code is used, and a retry gets the generic refusal. The operator issues
a re-enrollment code (`--reenroll`) and, after the switch, revokes the
unused token. There is no "replay the token" path.

## 4. Re-enrollment and device rules

| Device id in the tenant | Plain code | `--reenroll` code |
|---|---|---|
| new | device + first token | device + first token |
| exists, same employee, ACTIVE | **409** (already enrolled) | **additional** token; the old one keeps working until the operator revokes it |
| exists, other employee | 409 | 409 |
| exists, DISABLED | 409 | **403**: never silently re-enabled |

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

## 7. Production privileges (to apply with c0003, NOT applied)

As `zaza_migrator`, the table owner, after `control-migrate` to c0003:

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
| `test_enrollment_postgres.py` | PostgreSQL 16, in a deterministic container network | provisioning A/B, recovery, c0003 constraints, A/B enrollment and sync, 16-way concurrent use of one code, reconciliation, compensation |
