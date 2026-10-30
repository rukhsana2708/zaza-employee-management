# Sync API rate limiting and HSTS: design (revision 2, approved with corrections)

**Status: DESIGN ONLY (2026-10-31). Nothing here is implemented, applied or deployed.**
Cloud Armor stays unavailable (project quota 0), so protection lives in the application.

Revision 2 changes: the authenticated limit is one deterministic shared counter (no local watermark); the client IP
comes from a header the load balancer sets, not from `X-Forwarded-For`; the per-IP limiter is explicitly process-local
and approximate; HSTS starts at 300 s; `Retry-After` is the real time until the request becomes eligible again.

## 1. Observed pilot sync profile

Source: Cloud Logging, 921 `POST /api/v1/sync/batch` requests from one real device over 23 h (2026-10-29 22:02 to
2026-10-30 21:26 UTC), plus the agent's code (`deskmate/zaza/sync/worker.py`, `backoff.py`, `config.py`).

| Measure | Observed / defined |
|---|---|
| Normal interval | 60 s (agent default); 97% of gaps are 55 to 66 s, median 60.5 s |
| Requests per device per minute | 1 typical; at most 3 in any clock minute (those included my own tests) |
| Requests per 10 s | at most 2 |
| Records per batch | median 3, p90 4, p99 9, max 20 (mean 3.2) |
| Largest normal batch | 11 records; largest ever 20 (the catch-up after a 12-minute outage) |
| Catch-up after the 4-minute outage test | ONE request, 19 records (accepted 12, updated 7) |
| Burst pattern after reconnect | no multi-request burst observed (nothing was large enough). By code: up to 20 batches back-to-back per cycle (100 records each), then a 1 s pause, repeated until drained. Estimated 4 to 6 requests per second (server latency median 0.10 s, p95 0.18 s). |
| Retry while offline | exponential backoff 5, 10, 20, 40, 80, 160, 300 s (cap), +/-20% jitter; failures are local (DNS) and send nothing. Auth errors (401/403) retry only every 900 s +/-20%. |
| 429 / 503 handling in the agent | already implemented: `RateLimited` honours `Retry-After` (capped at 300 s) with backoff; `503` carries `Retry-After: 30` today |
| Server limits today | body 4 MiB, 500 records per batch (agent sends up to 100). No request-rate limit on the sync API; login and enrollment have shared limiters. |
| Capacity | Cloud Run max 3 instances, concurrency 20, 1 vCPU / 512 Mi; Cloud SQL `db-custom-1-3840` (1 vCPU), max_connections 100, service connection budget 70, tenant pool 2 |

Recording continues only while the PC is awake (the 377-minute sleep gap produced a 10-record catch-up), so a backlog
grows at about 3.2 records per minute **of running time**:

| Offline while running | Backlog | Requests to drain (100 per batch) |
|---|---|---|
| 5 minutes | about 16 | 1 |
| 30 minutes | about 100 | 1 to 2 |
| 8 hours | about 1,500 | about 16 (inside one agent cycle) |
| 24 hours | about 4,600 | about 47 (3 cycles) |
| 7 days | about 32,000 | about 323 |

## 2. Limiter design

All limits answer HTTP 429 with a stable body. Two mode switches, each `off | shadow | enforce`: `ZAZA_SYNC_LIMIT_MODE`
(the DEVICE limiter, and the master switch: `off` turns everything off) and `ZAZA_SYNC_LIMIT_IP_MODE` (the per-IP limiters;
**default `shadow`**, so they never refuse a request until someone explicitly sets `enforce` after the client address has been
verified). Ship in `shadow` (count and log, never reject) and compare with the numbers below before enforcing.

### A. Authenticated device traffic (primary)

Key: a one-way digest of `(organization_id, device_id)`, taken from the control-plane route row that is already read
to find the tenant (`device_credential_routes` stores both). The checks run right after that route lookup and
**before any tenant database is opened**. Never the public IP, never the token.

Every authenticated request passes two checks, in this order:

1. **Local emergency burst guard: 40 requests per 10 s per device, per instance.** It can only *reject*. It never admits
   anything: a request that passes the guard is *still counted* by check 2, so the guard cannot create an uncounted bypass
   around the shared limit. It is per instance, so across 3 instances it is an approximation, and it is not the
   cross-instance limit.
2. **Shared limit: 120 requests per 60 s window per device, stored in PostgreSQL, counted for every admitted request.**
   This is the actual cross-instance limit. One atomic upsert per request in the control database, using the database
   clock, so all instances agree. It is a *fixed* window aligned to the database clock: at most 120 admitted requests in
   each window, and a burst that straddles a window boundary can reach up to 240 in any 60 s span.

| Limit | Value | Reason |
|---|---|---|
| Shared sustained limit | **120 per 60 s per device** | 120x the normal 1 per minute. An 8 hour backlog (16 requests) is never affected. |
| Local burst guard | **40 per 10 s per device per instance** | normal peak is 2 per 10 s; a single 20-request agent cycle fits with room to spare |

Honest consequences for large catch-ups: a 20-request reconnect burst always passes. The agent's drain rate (an
estimated 4 to 6 requests per second, so 40 to 60 per 10 s) can brush the burst guard on backlogs above about 40
requests (roughly 12 hours or more offline). The result is a short 429 and a `Retry-After` of a few seconds, after which
the drain continues: a 24 hour backlog completes in about a minute, a 7 day backlog (about 323 requests, capped at 120
per window) in a few minutes. No record is lost and nothing becomes an authentication error.

Cost: one write per authenticated request, so about 1.7 writes per second at 100 devices and about 17 per second at
1,000 devices on the 1-vCPU database. That is acceptable now. **Re-evaluate Redis/Memorystore** when the fleet reaches
about 1,000 active devices, when counter writes exceed about 30 per second sustained, or when the limiter's p95 latency
exceeds about 20 ms.

### B. Invalid or unauthenticated traffic (per client IP) — PROCESS-LOCAL

Counted: missing, malformed, unknown and revoked credentials (anything that does not authenticate).

| Limit | Value |
|---|---|
| Authentication failures per IP per instance | **20 per 60 s** |
| Authentication failures per IP per instance | **10 per 10 s** |

**These limits are process-local.** Each Cloud Run instance keeps its own counters. With up to 3 instances the effective
limit for one IP is anywhere from 20 to about 60 failures per minute depending on how the load balancer spreads the
requests, and counters reset when an instance restarts or scales to zero. It is **not** an exact global 20 per minute and
must not be described as one. This is acceptable as a stopgap until Cloud Armor is available; its purpose is to stop a
single address from generating unlimited control-database reads, not to be a precise quota.

- Why those numbers: an agent with a bad credential retries once per 900 s, so even 1,000 broken devices behind one NAT
  produce about 1 failure per minute.
- A small cache of recently authenticated routing digests (10,000 entries, 120 s) lets known-good credentials bypass the
  IP failure limit, so a hostile user on shared Wi-Fi cannot lock out legitimate agents on the same public IP.
- Memory is bounded: an LRU of 50,000 IPs (about 5 MB); eviction never fails a request.
- Identical 429 for every failure mode, so the limiter reveals nothing about whether a device or token exists.
- Process-local on purpose: a failed request must not become a database write, or a flood would amplify load.

### C. Coarse safeguard for authenticated traffic per IP

**1,800 requests per 60 s per IP per instance** (process-local, also approximate across instances). An office of 500 devices
makes about 500 per minute; a whole-office reconnect is spread by backoff jitter. It only stops one address from consuming
the service.

## 3. Trusted client IP

The application does **not** trust `X-Forwarded-For` or any header a client can set. The production load balancer
injects a dedicated header using Google's trusted client-IP variable:

```
X-Zaza-Client-IP: {client_ip_address}
```

Terraform: `custom_request_headers = ["X-Zaza-Client-IP: {client_ip_address}"]` on
`google_compute_backend_service.edge` (inside `edge.tf`, only when the edge is enabled). The application reads it only
when both `ZAZA_HTTP_DEPLOYMENT_MODE=cloud-run` and `ZAZA_TRUSTED_CLIENT_IP_HEADER=X-Zaza-Client-IP` are set; it requires
exactly one header value that parses as a single IPv4 or IPv6 literal. Anything else counts as "client unverified".

**Trust assumptions (must stay true):**
1. Cloud Run ingress remains `internal-and-cloud-load-balancing`, so every request passes through the load balancer.
2. The default `run.app` URL remains disabled.
3. The load balancer sets or overwrites the header on every request it forwards.
4. The application never treats this header as trustworthy in an unrestricted direct-public deployment (local development,
   the legacy single-database app, or any mode other than the load-balanced Cloud Run mode).

Behaviour when the client IP is unverified: the per-IP limiters are skipped (never collapsing everyone into one shared
bucket), a counter and warning are logged, and the device limiter still works because it does not use the IP.

**Verification gate (the IP limiter stays in `shadow` until this passes).** In shadow mode the application logs a short
digest of the derived IP, never the address. Compare it with Cloud Logging's `httpRequest.remoteIp` for:
1. normal requests;
2. requests with a deliberately forged `X-Forwarded-For`;
3. requests with a deliberately forged `X-Zaza-Client-IP` (this proves the load balancer overwrites a client-sent value);
4. requests from at least two different public IPs if practical (this PC, plus a phone hotspot).
Only when all of them match is `ZAZA_SYNC_LIMIT_IP_MODE` switched to `enforce`. If the overwrite behaviour is not as assumed, the IP
limiter stays off and only the device limiter ships.

## 4. Where the state lives

| Option | Correct across instances | Latency | If it fails | Complexity | Cost | DB impact | New dependency |
|---|---|---|---|---|---|---|---|
| 1. Process-local only | no: up to N x the limit (N <= 3), resets on scale-to-zero | none | cannot fail | lowest | none | none | no |
| 2. Control PostgreSQL (existing counters) | **yes** (database clock, atomic upsert; already proven by the login and enrollment limiters) | one write per authenticated request | shares fate with the route lookup, which needs the same database | low: reuses `count_attempt` | none | one upsert per authenticated request | **no** |
| 3. Redis / Memorystore | yes | sub-millisecond | its own failure mode | high: VPC access, Terraform networking, a new managed service | tens of USD per month (check current pricing) plus connectivity | none | **yes** |
| 4. Other Google options | Cloud Armor: unavailable (quota 0). Cloudflare rate rules: needs proxying, which is off by design. Firestore counters: new service with a per-document write ceiling. API Gateway/Endpoints quotas: heavy and key-based. Cloud Run: only instance count and concurrency. | | | | | | |

**Recommendation: PostgreSQL for the authenticated per-device limit (every request counted), process-local for the
unauthenticated per-IP limits.** Counter rows are cleaned up opportunistically (a bounded delete of expired rows on about 1
request in 200), so no scheduler or extra service is needed; `saas-auth-cleanup` also removes them.

## 5. Failure behaviour

| Path | State unavailable | Behaviour |
|---|---|---|
| Authenticated device | shared counter write fails (timeout, outage) | **fail open**: serve the request, keep the local burst guard and add a local per-device ceiling of 240 per 60 s, log a warning with a counter. Rejecting would silently delay telemetry because of our own fault. If the control database is fully down, the route lookup fails first and the agent already gets `503` + `Retry-After: 30`, keeps everything queued and retries. |
| Unauthenticated | none (process-local) | cannot fail; at the memory limit the oldest entries are evicted. A bogus token fails authentication anyway, so being permissive here never grants access. |

No path drops telemetry: a 429 or 503 leaves the batch unconfirmed and the agent re-sends the same records.

## 6. HTTP behaviour

```
HTTP/1.1 429 Too Many Requests
Retry-After: 17
Content-Type: application/json

{"error":"rate limit exceeded"}
```

- **`Retry-After` is the actual time until the limiter expects the request to be eligible again**, as an integer number of
  seconds (rounded up, minimum 1): for the shared counter, the seconds left in the current window; for the burst guard,
  the seconds until the oldest request in the 10 s window ages out; for a combination, the larger of the two. No random
  value, no server-side jitter. The cap is 60 s.
- Same body and headers for every limit and every failure mode. No token, device id, tenant, counter or fingerprint in the
  response or in logs (logs carry only the limit name and a short digest of the key).

**Agent behaviour, already correct:** a 429 raises `RateLimited`, which is *not* an authentication error. The cycle stops,
nothing is marked confirmed or discarded, and the next attempt waits at least `Retry-After` (floored by the existing
exponential backoff). It never re-enrolls, and uploads are idempotent per record, so a retry cannot duplicate telemetry.
No agent change is required for correctness. The agent may add a small jitter (up to +20%) around the returned interval to
avoid synchronized retries; this and the two items below are optional polish: show "rate limited" instead of `SERVER_ERROR`
in the status window, and pause about 0.25 s between batches of a catch-up cycle so large backlogs avoid the burst guard.

## 7. HSTS

**First enforced value: `Strict-Transport-Security: max-age=300`** on every response the application produces in Cloud Run
mode, where HTTPS is guaranteed by the deployment (load balancer plus ingress lock). **No `includeSubDomains`, no `preload`**
unless explicitly approved later.

- HTTP never reaches the application: the load balancer's port-80 map answers 301 to HTTPS, and that response carries no
  HSTS (browsers ignore HSTS over HTTP and it must not be sent there).
- Configurable through `ZAZA_HSTS_MAX_AGE_SECONDS` (`0` = off, **default off**), exposed as the Terraform variable
  `hsts_max_age_seconds` (default 0), so each increase is a configuration change and a new revision, not a code change.
- Ramp after successful production observation: **300 s, then 86,400 s, then 604,800 s, then 2,592,000 s, then
  31,536,000 s.** Each step needs: certificate ACTIVE and auto-renewing, HTTP to HTTPS redirect verified, no mixed content,
  Cloudflare still DNS-only, `run.app` still 404, no support complaints. Existing security headers on web pages are untouched.

## 8. Exact changes required

**Application (`deskmate/zaza_server/`):**
- `ratelimit.py` (new): policy and settings, the local burst guard (sliding log), the process-local IP tables (LRU, bounded),
  the known-good digest cache, the shared-counter call, decisions with an exact retry time, modes `off|shadow|enforce`.
- `tenancy/device_routes.py` and `tenancy/resolver.py`: the route lookup also returns `device_id` (the column is already read).
- `tenancy/saas_sync.py`: apply the limiter right after the route lookup and before the tenant database; map a refusal to a
  `RateLimited` exception.
- `app.py`: translate `RateLimited` into the 429 response with `Retry-After`.
- `http_security.py`: trusted client-IP extraction (single validated header, cloud-run mode only); `Strict-Transport-Security`
  middleware.
- `tenancy/saas_auth.py` (`PostgresAuthStore`): reuse `count_attempt` as is; add a bounded expired-row cleanup helper.
- Settings parsing: `ZAZA_SYNC_LIMIT_MODE`, `ZAZA_SYNC_LIMIT_IP_MODE`, thresholds (code defaults, environment overrides),
  `ZAZA_TRUSTED_CLIENT_IP_HEADER`, `ZAZA_HSTS_MAX_AGE_SECONDS`.

**Database:** one control migration, **`c0004`**, required. It follows the precedent of `c0003`: drop and re-add the CHECK
`login_attempt_counters_bucket_valid` to allow the new bucket `sync_device` as well as the existing ones. Additive, on a tiny
table, run with the `zaza-migrate` job (`control-migrate`) *before* the new image serves traffic. **No new table.**

**Terraform (production):**
- `edge.tf`: `google_compute_backend_service.edge[0]` gets `custom_request_headers = ["X-Zaza-Client-IP: {client_ip_address}"]`
  (in-place update).
- `cloud_run.tf`: new environment variables on `zaza-server` (`ZAZA_SYNC_LIMIT_MODE`, `ZAZA_SYNC_LIMIT_IP_MODE`, `ZAZA_TRUSTED_CLIENT_IP_HEADER`,
  `ZAZA_HSTS_MAX_AGE_SECONDS`) and the image digest (in-place update, new revision).
- `variables.tf`: `sync_limit_mode` (default `off`), `sync_limit_ip_mode` (default `shadow`) and `hsts_max_age_seconds`
  (default `0`). Thresholds stay as code defaults.
- Expected plan: **0 to add, 2 to change, 0 to destroy** (the backend service and the Cloud Run service). No new resources,
  no networking, no new Google Cloud service, no new dependency.

**Agent:** none required, so no installer rebuild.

**Deployment order:** build the image, run `control-migrate` (c0004), apply Terraform with the header and `sync_limit_mode=shadow`
and HSTS off, run the verification gate in section 3, observe a week, move the device limiter to `enforce`, then the IP
limiter, then start HSTS at 300 s and ramp.

## 9. Tests

**Authenticated limiter:**
- once-per-minute sync is unaffected across hours of simulated time;
- a normal 2-request burst passes;
- **a 20-request back-to-back reconnect burst passes** (within the 40 per 10 s guard and the 120 per window);
- the burst guard rejects the 41st request inside 10 s, and what it admits is still counted in the shared counter;
- the shared limit: 120 requests spaced beyond the burst guard (at least 0.25 s apart) pass inside one window, and the 121st
  gets 429; the next window starts fresh;
- the guard cannot bypass the counter: the shared count equals the number of admitted requests;
- sustained abusive traffic is limited; one device at its limit does not affect another; many devices behind one IP all keep
  working;
- `Retry-After` equals the real time to eligibility (checked against the window end and the guard) and is honoured by the real
  sync worker with a fake clock: no record lost, no `AUTH_ERROR`, no re-enrollment, no duplicate after the retry;
- a 429 mid-catch-up leaves the confirmed batches confirmed and the rest pending.

**Invalid traffic:** repeated missing, malformed, unknown and revoked tokens are limited per IP (process-local semantics
asserted, including that two instances can together exceed a single instance's limit); different IPs are isolated; a
known-good credential still works while its IP is throttled; bodies are byte-identical across failure modes; no response or
log contains a token, device id or tenant.

**Trusted client IP:** the header is used only in the load-balanced mode; a forged `X-Forwarded-For` is ignored; a missing,
duplicated, comma-separated or non-IP value yields "unverified" and skips the IP limiter; an unrestricted deployment never
trusts it.

**Distributed:** 3 application instances over one real PostgreSQL, concurrent threads, never admitting more than 120 per window
per device; the counter upsert is race-free; cleanup removes only expired rows; a simulated counter outage fails open with the
local ceiling and a warning, without a rejection.

**HSTS:** header present and exactly `max-age=300` (when configured to 300) on 200, 401, 404 and 429 in Cloud Run mode; absent
when disabled or outside Cloud Run mode; never `includeSubDomains` or `preload`; existing headers unchanged; a static Terraform
test that the variable defaults to off and `custom_request_headers` is present; a post-deploy script checks HTTPS carries the
header and HTTP answers 301 without it.

**Regression:** the existing login and enrollment limiter tests stay untouched and passing.

## 10. Risks that remain

- Per-IP limits (process-local) cannot stop a large distributed flood of bogus tokens; only Cloud Armor can. Each such request
  still costs one control-database read.
- The per-IP limits are approximate across instances and reset on restart or scale-to-zero.
- The load balancer's header-overwrite behaviour and `{client_ip_address}` must be verified before enforcing (section 3).
- A hostile user behind a shared NAT can consume that IP's failure budget (mitigated by the known-good cache).
- The shared counter is one control-database write per authenticated request: fine now, a scaling trigger later.
- A fixed window allows up to 240 requests across a window boundary.
- Compromised-but-valid credentials can still upload up to the per-device ceiling; per-organization quotas are future work.
- Revoked credentials answer 403 while unknown ones answer 401 (documented behaviour); the limiter responses do not add to
  that, and 256-bit tokens make enumeration infeasible.
- HSTS is sticky for its `max-age`: hence 300 s first and a staged ramp.

## 11. Agent polish backlog (not part of this work)

1. Handle the Windows logoff/shutdown notification so the session closes and flushes cleanly instead of being recovered as
   "interrupted" at the next start.
2. Quiet the harmless first-tick "input hooks not HEALTHY, status UNKNOWN" startup log line.
3. Retry sooner when Windows reports that network connectivity has returned (today recovery can take one to two minutes).

## 12. Implementation status (code and tests only; NOTHING deployed, migrated or applied)

Implemented in the repository as specified above:

- `deskmate/zaza_server/ratelimit.py` (new): settings and modes, the local burst guard, the shared per-device counter call, the
  process-local per-IP failure tables, the coarse per-IP safeguard, the known-good cache, fail-open with a local ceiling.
- `deskmate/zaza_server/tenancy/saas_sync.py`, `resolver.py`, `device_routes.py`: the route lookup also returns `device_id`; the
  limiter runs after the route read and before any tenant database.
- `deskmate/zaza_server/app.py`: HTTP 429 `{"error":"rate limit exceeded"}` with an integer `Retry-After`.
- `deskmate/zaza_server/http_security.py`: `X-Zaza-Client-IP` extraction (cloud-run mode only, exactly one valid IP literal,
  X-Forwarded-For never used) and the HSTS middleware (`ZAZA_HSTS_MAX_AGE_SECONDS`, cloud-run mode only).
- `deskmate/zaza_server/tenancy/saas_auth.py`: bounded expired-counter cleanup for the new bucket.
- Control migration `c0004_sync_rate_limits` (adds the `sync_device` bucket; tested on a disposable PostgreSQL only).
- Terraform source: `edge.tf` (backend `custom_request_headers`), `cloud_run.tf` and `variables.tf` (mode, IP mode, HSTS).
  Read-only plan: 0 to add, 2 to change, 0 to destroy.
- Agent: **no change** (a 429 is already a retryable, non-auth failure that honours `Retry-After`; a test proves it).

## 13. Future production deployment plan (NOT executed)

1. Commit and push; build the image from that commit on Cloud Build and record the digest.
2. Update ONLY the `zaza-migrate` job to the new image (targeted), run `control-migrate`, and confirm the control schema is at
   `c0004_sync_rate_limits` (`control-db-status`). The old service keeps running: the migration is additive.
3. Set the new image digest in the local `terraform.tfvars`; `terraform plan` must show exactly 0 to add, 2 to change, 0 to
   destroy (the Cloud Run service, and the load-balancer backend for the header). Apply.
   Settings for the first deployment: `sync_limit_mode = "shadow"`, `sync_limit_ip_mode = "shadow"`, `hsts_max_age_seconds = 0`.
4. Run the client-IP verification gate (section 3) and watch the shadow telemetry for at least a week.
5. `sync_limit_mode = "enforce"` (device limiter). Re-check.
6. `sync_limit_ip_mode = "enforce"` only after the gate passed.
7. `hsts_max_age_seconds = 300`, then ramp 86,400, 604,800, 2,592,000, 31,536,000, each after a clean observation period.
