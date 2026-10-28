# SaaS authentication and credential-derived tenant routing (Phase 10F-B)

> **Status:**
> - Phase 10F-B wrote the code.
> - **Phase 10F-C applied c0002 in production.** The runtime grants in §9
>   are applied and verified.
> - **Phase 10G-B then applied c0003** (device enrollment; see
>   [ENROLLMENT.md](ENROLLMENT.md)). The production control schema is now
>   `c0003_device_enrollment`, and the migration job runs image
>   `sha256:b7ad3a892306…` (commit `778488c`). c0003 added INSERT on
>   `device_credential_routes` for the runtime, used only when an
>   enrollment completes (ENROLLMENT.md §7).
> - **No production SaaS user, organization, membership, tenant, device
>   route or token exists.** All four auth/routing tables are empty.
> - The Cloud Run web service is NOT deployed.

Code: `deskmate/zaza_server/tenancy/`:
- `saas_auth.py`: passwords, sessions, the shared limiter, permissions;
- `device_routes.py`: credential routes;
- `saas_sync.py`: the device authenticator and the runtime;
- `saas_web.py`: the `/app` session API;
- `app.py`: `create_saas_app`.

Migration: `tenancy/control/migrations/versions/c0002_saas_auth_routing.py`.

## 1. Request flows

```
DEVICE (agent sync)
  Authorization: Bearer <token> + X-ZaZa-Device-Id
    -> routing_hash(token) -> device_credential_routes (ACTIVE)  -> organization_id   [routing only]
    -> TenantResolver.for_device_route: ACTIVE organization, READY tenant -> TenantContext(DEVICE)
    -> TenantRepositoryManager.repository(context)   (lease held for the whole request)
         -> auth.authenticate_request() IN THAT TENANT: token hash exists, belongs to the
            device header, ACTIVE token, ACTIVE device                       [the proof]
         -> touch the device; SyncService on the SAME repository
    -> lease released

SaaS USER (manager / admin / viewer)
  zaza_session cookie
    -> SHA-256(token) -> user_sessions (not revoked, not expired) -> user ACTIVE -> user_id
    -> /app/orgs/{slug}: slug -> immutable organization_id (a handle, grants nothing)
    -> TenantResolver.for_member(user_id, organization_id):
         ACTIVE user, ACTIVE membership, ACTIVE organization, READY tenant -> TenantContext(MEMBER, role)
    -> permission check (explicit role -> permission sets)
    -> that tenant's repository only
```

Nothing a client sends selects a database:
- no organization header, query parameter or body field;
- no slug-to-database shortcut;
- no database name or URL.

The batch envelope is strict, so an extra `organization_id` field is
refused (422), never interpreted. A claimed organization is supported only
inside `for_device_route(..., claimed_organization_id=)`. It is compared
with the routed one, and a mismatch is refused.

`create_app(repo)` is unchanged and remains the single-database path used
by local development, tests and the Windows server. `create_saas_app(runtime)`
is the separate multi-tenant path. `serve` uses it with
`ZAZA_TENANCY_MODE=saas`, which `check-config --cloud-run` requires.

## 2. Control tables added by c0002

| Table | Holds | Never holds |
|---|---|---|
| `user_password_credentials` | one Argon2id PHC hash per SaaS user (CHECK: `$argon2id$` prefix), credential version, change time | plaintext, reversible secrets, hints, security questions |
| `user_sessions` | session id, user, **SHA-256(session token)**, created/expiry/last-seen, revocation time + reason (`logout`, `admin`, `password_change`, `user_disabled`) | the session token |
| `login_attempt_counters` | (bucket, key digest, window start) → attempts | emails, IPs, passwords |
| `device_credential_routes` | **domain-separated SHA-256 of the device token** → organization, device id, ACTIVE/REVOKED | the raw token, the tenant's token hash, database names, hosts, connection strings |

The `users` table stays the single identity. A SaaS user is not an
employee (§6). c0001 is unchanged, and a test pins its file hash. c0002 also
extends the audit CHECK constraints with the new actions and the
`device_route` entity type.

## 3. Passwords

- Argon2id via `argon2-cffi`, the same maintained helpers as the legacy
  dashboard (`dashboard/security.py`). A hash is upgraded on login when the
  library defaults change.
- Policy: 12–256 characters, not mostly repeated characters, and not
  containing the email's local part.
- An unknown email, an invalid email or a user without a credential costs
  one dummy Argon2 verification, so there is no timing shortcut.
- A password is set only through the operator CLI's hidden prompt (§8). A
  password change revokes every session of the user in the same
  transaction, and is audited as `user.password_set` without the password
  or hash.

## 4. Sessions and CSRF

- **Token:** 256 random bits. The browser keeps it in the cookie
  `zaza_session` (Secure, HttpOnly, SameSite=Strict, Path=/app, 12 h by
  default via `ZAZA_SAAS_SESSION_HOURS`, 1..168). The database stores
  SHA-256 only.
- **Validity:** every request checks revocation, expiry and that the user
  is ACTIVE. A disabled user loses access immediately, even with an old
  token.
- **CSRF:**
  - a per-session token (HMAC of the session token) for authenticated POSTs;
  - a pre-session double-submit token (`zaza_login` cookie, Path=/app/login)
    for the login form;
  - `http_security.same_origin()` (Phase 10F-A) against the external
    `https://host`;
  - no origin parsing of its own in the SaaS code.
- **Errors:** login failures are always `401 {"error": "invalid email or
  password"}`. An unknown organization slug and an organization without the
  user's membership return the same `404 {"error": "not found"}`.
- **No JWTs:** server-side sessions only.

## 5. Shared login rate limiting

The legacy dashboard limiter is per process, which is not enough for Cloud
Run. The SaaS limiter keeps its state in the control database
(`login_attempt_counters`), so every instance sees the same counts:

- Each attempt is counted before the password check, by one atomic
  `INSERT … ON CONFLICT DO UPDATE SET attempts = attempts + 1 RETURNING`
  per bucket. Concurrent attempts serialize on the row, so they can't race
  past the limit; tests prove this across two independent "instances".
- **Buckets:**
  - the login identity (SHA-256 of the normalized email with a domain
    prefix): 10 attempts per 15 minutes by default;
  - one global bucket against bursts: 300 per minute.

  Client IPs are not used, because Cloud Run mode does not trust them.
- Unknown and existing emails are limited identically. A successful sign-in
  clears the identity bucket.
- Windows use database time, so all instances agree. Rows for old windows
  are removed by `saas-auth-cleanup`, which also deletes sessions that
  ended more than 30 days ago. Storage stays bounded: at most one identity
  row per attempted identity per window, under the global cap.
- No Redis.

## 6. Users vs employees, roles and permissions

- **Separate identities.** Control-plane `users` (SaaS logins) and tenant
  `employees` (monitored people) are different things. Matching names,
  emails or ids never merge them.
- **No implicit conversion.** `employees.role` (EMPLOYEE/MANAGER/ADMIN)
  grants nothing in the SaaS, and a membership role never changes
  `employees.role`. Tests check both directions.
- **Membership roles (OWNER/ADMIN/MANAGER/VIEWER) map to explicit
  permission sets, not an ordering:**

| Permission | OWNER | ADMIN | MANAGER | VIEWER |
|---|---|---|---|---|
| `org.view` | ✓ | ✓ | ✓ | ✓ |
| `employees.view_all` | ✓ | ✓ | – | – |
| `employees.view_scoped` (team scopes: future; grants nothing yet) | ✓ | – | ✓ | – |
| `devices.manage` (future enrollment) | ✓ | ✓ | – | – |
| `members.manage` (future invitations) | ✓ | ✓ | – | – |
| `org.settings.manage` | ✓ | – | – | – |

- A **MANAGER** deliberately does not get company-wide employee access;
  team scopes come later.
- **OWNER** is scoped to one organization. It is not a platform super-admin,
  and no role bypasses `TenantResolver`.
- Device and job contexts carry no SaaS permissions.
- **Multiple organizations.** A user can be OWNER of A and VIEWER of B with
  one session. Each request resolves its own context; an organization
  without a membership is denied.

## 7. Device credential routing: a route is not authentication

- `routing_hash(token)` = SHA-256(`zaza-device-route-v1\0` + token). It
  differs from the tenant's `device_tokens.token_hash`, so control-plane
  data can't be matched against, or replayed into, a tenant database. A
  test scans every control table and finds neither the raw token nor the
  tenant hash.
- **One hash, one organization** (primary key), and a route can't be
  re-pointed (an immutability trigger). Only ACTIVE routes are followed.
- **The route only says WHERE to verify.** The tenant database must still
  find the token hash, match it to `X-ZaZa-Device-Id`, and see an ACTIVE
  token and an ACTIVE device. These are refused and never fall through to
  another tenant (all tested):
  - a stale or malicious route, such as route A for a token that exists
    only in B;
  - an unknown or revoked route;
  - a suspended organization or a non-READY tenant.
- **Responses** are the existing generic API errors: `invalid credentials`
  (401), `token revoked` / `device disabled` (403). They never contain an
  organization, slug or database name.
- **Lifecycle** for the enrollment phase (`PostgresDeviceRoutes`):
  - `create_route` (with the raw token in memory at issue time);
  - `revoke_route`;
  - `revoke_device_routes(..., except_route_hash=)`.

  Rotation: issue the new tenant token, create its route, switch the agent,
  then revoke the old token and its route. Both actions are audited
  (`device_route.create/revoke`) without hashes. There is no public
  enrollment endpoint yet, and no production token has been issued.

## 8. Operator commands (trusted path; no self-signup)

```
saas-user-create --email E --name N [--no-password]   (prompts for the password)
saas-user-set-password --email E                      (prompts; revokes the user's sessions)
saas-user-disable --email E                           (DISABLED + revokes sessions)
saas-user-enable --email E
saas-user-revoke-sessions --email E
saas-auth-cleanup                                     (ended sessions, old counter windows)
```

- Passwords come only from the hidden prompt, never from an argument,
  environment variable, URL or log line.
- There is no signup or registration route. Organizations, users,
  memberships, tenant databases and device routes are created only through
  trusted operator paths.
- In production these run as the `zaza-migrate` job. Its `ZAZA_CONTROL_*`
  settings use the migrator role, which owns the control tables.

## 9. Production steps (done in Phase 10F-C)

1. Build and push a new image by digest, and update the migration job's
   image.
2. Run `control-migrate` through the job (c0001 → c0002).
3. Grant the control runtime role exactly what the SaaS app uses, as
   `zaza_migrator`, the table owner:

   ```sql
   GRANT SELECT, INSERT, UPDATE ON user_password_credentials TO zaza_control_app;  -- rehash on login, future self-service change
   GRANT SELECT, INSERT, UPDATE, DELETE ON user_sessions TO zaza_control_app;      -- DELETE only for cleanup of ended sessions
   GRANT SELECT, INSERT, UPDATE, DELETE ON login_attempt_counters TO zaza_control_app;  -- DELETE: clear on success + cleanup
   GRANT SELECT ON device_credential_routes TO zaza_control_app;                   -- routing reads only
   ```

   - No DELETE or CREATE on the core control tables.
   - Route creation and revocation (enrollment) gets its own reviewed grant
     in the enrollment phase.
   - SELECT on the new tables already follows from the 10E-B default
     privileges; the statements above make it explicit.
4. Re-run the privilege verification, positive and negative, as in 10E-C.

## 9a. Edge protection before public login

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

## 10. Reliability evidence

- **Tenant-isolation proof:** run repeatedly in a deterministic container
  network (the WSL localhost relay is not used). Each run uses a fresh
  control database plus A/B tenants, the same ids, the cross-tenant
  negative cases, record writes, closed pools and dropped databases.
- **Concurrent A/B stress:** a pool cache of 1 forces constant eviction
  while threads interleave batches. Every record stays in its tenant.
- See DEVELOPMENT_STATUS.md for the counts of the latest run.
