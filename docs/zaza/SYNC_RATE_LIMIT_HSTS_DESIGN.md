# Sync API rate limiting and HSTS: design proposal

**Status: DESIGN ONLY (2026-10-31). Nothing here is implemented or deployed.**
Cloud Armor stays unavailable (project quota 0), so protection lives in the application.

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
| 8 hours | about 1,500 | about 16 (one cycle, a few seconds) |
| 24 hours | about 4,600 | about 47 (3 cycles) |
| 7 days | about 32,000 | about 323 (roughly 1 to 2 minutes at 4 requests per second) |

## 2. Limiter design

Three layers, each answering a different threat. All answers are HTTP 429 with a stable body.

### A. Authenticated device traffic (primary)

Key: a one-way digest of `(organization_id, device_id)`, taken from the control-plane route row that is already read
to find the tenant (`device_credential_routes` stores both). The check runs right after that route lookup and
**before any tenant database is opened**. Never the public IP, never the token.

| Limit | Value | Reason |
|---|---|---|
| Local watermark (per instance, per device) | 10 requests per rolling 60 s | 10x the normal rate of 1 per minute. At or below it the request touches no shared state at all. |
| Shared sustained limit (all instances, per device) | **120 requests per 60 s window** | 120x normal. Draining a 7-day backlog needs about 323 requests, so even that completes in about 3 windows with pacing. An 8 to 24 hour backlog is never throttled. Caps one device at about 12,000 records per minute, which one 1-vCPU database can absorb. |
| Local burst guard (per instance, per device) | 40 requests per 10 s | normal peak is 2 per 10 s; the agent's natural drain peaks around 40 to 60 per 10 s, so a legitimate catch-up is slightly smoothed, never blocked |
| Global bound | at most 120 + 10 x instances = **150 requests per minute per device** | exact, because only the excess above the watermark is counted in shared state |

Response: 429, `Retry-After` = seconds to the end of the window (1 to 60), +/-20% jitter so a fleet does not retry in
lockstep.

### B. Unauthenticated or invalid traffic (per client IP)

Counted: missing, malformed, unknown and revoked credentials (anything that does not authenticate).

| Limit | Value | Reason |
|---|---|---|
| Failed requests per IP per instance | **20 per 60 s**, 10 per 10 s | a broken agent retries once per 900 s; even 1,000 broken devices behind one NAT make about 1 failure a minute. |
| Worst case across 3 instances | 60 per minute per IP | acceptable: tokens carry 256 bits, so guessing is infeasible; the limit protects control-database reads. |
| Known-good fast path | a small in-process cache of recently authenticated routing digests (10,000 entries, 120 s) bypasses the IP failure limit | a hostile user on shared Wi-Fi cannot lock out legitimate agents on the same public IP |
| Memory | LRU of 50,000 IPs, about 5 MB | bounded; eviction never fails a request |

Identical 429 body for every failure mode, so the limiter reveals nothing about whether a device or token exists.
Process-local on purpose: a failed request must not become a database write, or a flood would amplify load.

### C. Coarse safeguard for authenticated traffic per IP

Recommended, loose: **1,800 requests per 60 s per IP per instance** (30 per second). An office of 500 devices makes
about 500 per minute; a whole-office reconnect is spread by backoff jitter. This only stops one address from
consuming the service (it exceeds nothing legitimate and fits the 1-vCPU database). Local, no shared state.

### Rollout safety

`ZAZA_SYNC_LIMIT_MODE = off | shadow | enforce`. Ship in `shadow` (count and log, never reject) for at least a week of
pilot traffic, compare against the thresholds above, then `enforce`.

## 3. Where the state lives

| Option | Correct across instances | Latency | If it fails | Complexity | Cost | DB impact | New dependency |
|---|---|---|---|---|---|---|---|
| 1. Process-local only | no: up to N x the limit (N <= 3), resets on scale-to-zero | none | cannot fail | lowest | none | none | no |
| 2. Control PostgreSQL (existing counters) | **yes** (database clock, atomic upsert; proven by the login and enrollment limiters) | 1 write per counted request | shares fate with the route lookup, which already needs the same database | low: reuses `count_attempt` | none | only abnormal traffic writes (see below) | **no** |
| 3. Redis / Memorystore | yes | sub-millisecond | its own outage mode | high: VPC access, Terraform networking, a new managed service | tens of USD per month (check current pricing) plus connectivity | none | **yes** |
| 4. Other Google options | Cloud Armor: unavailable (quota 0). Cloudflare rate rules: needs proxying, which is off by design. Firestore counters: new service, per-document write ceiling. API Gateway/Endpoints quotas: heavy and key-based. Cloud Run limits: only instance count and concurrency, nothing per client. | | | | | | |

**Recommendation: option 2, with a process-local first stage.** Normal devices (1 per minute, below the watermark)
cause **zero** shared writes. Only a device that exceeds 10 requests per minute on an instance starts counting in
PostgreSQL. This keeps the limit exact and bounded (150 per minute), adds no service, and means a flood cannot become a
database-write flood. Scale trigger to revisit Redis: sustained shared-counter writes above about 100 per second or
well over 5,000 active devices.

Table churn: counters live in `login_attempt_counters` (fixed windows). Expired rows are deleted opportunistically by
the limiter (a bounded `DELETE ... WHERE window_start < now() - 1 hour`, about 1 request in 200), so no scheduler or
extra service is needed. The existing `saas-auth-cleanup` command also removes them.

## 4. Failure behaviour

| Path | State unavailable | Behaviour |
|---|---|---|
| Authenticated device | shared counter write fails (timeout, outage) | **fail open**: serve the request, fall back to the local limiter at 2x its thresholds, log a warning with a counter. Rejecting would silently delay telemetry because of our own fault. If the control database is fully down, the route lookup fails first and the agent already gets `503` + `Retry-After: 30`, keeps everything queued and retries. |
| Unauthenticated | none (process-local) | cannot fail; at memory limit the oldest entries are evicted. A bogus token fails authentication anyway, so being permissive here never grants access. |

No path drops telemetry: a 429 or 503 leaves the batch unconfirmed, and the agent re-sends the same records.

## 5. HTTP behaviour

```
HTTP/1.1 429 Too Many Requests
Retry-After: 17
Content-Type: application/json

{"error":"rate limit exceeded"}
```

- Same body and headers for every limit and every failure mode. No token, device id, tenant, counter or fingerprint
  in the response or in logs (logs carry only the limit name and a short digest of the key).
- `Retry-After` is an integer number of seconds, never above 60 (the agent caps its own wait at 300 s).

**Agent behaviour, already correct:** a 429 raises `RateLimited`, which is *not* an authentication error. The cycle
stops, nothing is marked confirmed or discarded, the next attempt waits at least `Retry-After` (with the existing
exponential backoff and jitter), and records are re-sent idempotently (per-record upsert). It never re-enrolls. No agent
code change is required for correctness. Optional polish: show "rate limited" instead of `SERVER_ERROR` in the status
window, and pause about 0.25 s between batches of a catch-up cycle so large backlogs never trip the burst guard.

## 6. HSTS

`Strict-Transport-Security: max-age=86400` (one day) on every response the application produces in Cloud Run mode,
where HTTPS is guaranteed by the deployment (load balancer plus ingress lock). **No `includeSubDomains`, no `preload`.**

- HTTP never reaches the application: the load balancer's port-80 map answers 301 to HTTPS, and that response carries no
  HSTS (browsers ignore HSTS over HTTP, and it must not be sent there).
- Configurable (`ZAZA_HSTS_MAX_AGE_SECONDS`, `0` = off, default off) so each increase is an environment change.
- Why one day first: the host is currently an API for agents (which ignore HSTS) and a future browser UI. One day is long
  enough to catch a redirect or certificate problem and short enough to unwind. A 5-minute canary can precede it.
- Ramp: 300 s, then 86,400 s (24 h later), then 604,800 s (one week of clean operation), then 2,592,000 s (30 days), then
  31,536,000 s (one year). Each step needs: certificate ACTIVE and auto-renewing, HTTP to HTTPS redirect verified, no
  mixed content, Cloudflare still DNS-only, `run.app` still 404. `includeSubDomains` or `preload` only with explicit
  approval, because they bind other `zazavoice.com` hosts and are very hard to undo.
- Existing security headers on web pages are untouched; HSTS is added alongside them.

## 7. Required work

**Application:** new `deskmate/zaza_server/ratelimit.py` (policy, local tables, decisions);
`deskmate/zaza_server/tenancy/saas_sync.py` (call the limiter after the route lookup); `tenancy/device_routes.py` and
`tenancy/resolver.py` (the route lookup also returns `device_id`); `deskmate/zaza_server/app.py` (429 mapping);
`deskmate/zaza_server/http_security.py` (verified client IP; HSTS middleware); settings parsing for the new
`ZAZA_SYNC_LIMIT_*` and `ZAZA_HSTS_MAX_AGE_SECONDS` variables.

**Client IP:** `CloudRunMiddleware` deliberately ignores `X-Forwarded-For` today. Behind the Google load balancer the
trustworthy value is the entry the load balancer appended (second from the right); everything to its left is
client-controlled. This **must be proven empirically before use**: send requests with a forged header and compare with
Cloud Logging's `remoteIp`. Until proven, the IP limiter would run in `shadow` mode only.

**Database:** a control migration **`c0004`** is required. The `login_attempt_counters.bucket` CHECK currently allows only
`identity` and `global`; it must also allow the new device bucket. It is additive, on a tiny table, and applied
explicitly with the `zaza-migrate` job (`control-migrate`) before the new image is enabled. No new table.

**Agent:** none required, so no installer rebuild.

**Terraform:** in-place update of the `zaza-server` service only (new environment variables, `hsts_max_age_seconds` variable). No new
resources, no networking change, no new Google Cloud service.

**Dependencies:** none new.

**Deployment order:** build image, run `control-migrate`, deploy in `shadow`, observe, `enforce`, then HSTS 300 s and onward.

## 8. Tests

*Authenticated limiter:* once-per-minute sync is unaffected (clock-driven, no 429 across hours); a normal 2-request burst
passes; a 20-batch reconnect cycle passes; a catch-up of 120 requests inside one window passes and request 121 gets 429;
sustained abuse is limited to the stated bound; one device at its limit does not affect another; many devices behind one
IP all keep working; `Retry-After` is an integer within 1 to 60 and is honoured by the real worker (fake clock) with no
record lost, no `AUTH_ERROR` and no duplicate.

*Invalid traffic:* repeated missing, malformed, unknown and revoked tokens are limited per IP; different IPs are
isolated; a known-good credential still works while its IP is throttled; bodies are byte-identical across failure
modes; the response never contains a token, device id or tenant.

*Distributed:* N (3) application instances over one real PostgreSQL, with concurrent threads, never admit more than
`L + W x N` requests per window; counter upserts are race-free; opportunistic cleanup deletes only expired rows;
simulated counter outage fails open with the local fallback and logs, without a rejection.

*HSTS:* header present and exactly `max-age=<configured>` on 200, 401, 404 and 429 in Cloud Run mode; absent when
disabled or outside Cloud Run mode; never contains `includeSubDomains` or `preload`; existing headers unchanged; Terraform
static check that the variable defaults to off; a post-deploy script verifies HTTPS carries it and HTTP answers 301
without it.

*Regression:* existing login and enrollment limiter tests untouched and passing.

## 9. Risks that remain

- Per-IP limits cannot stop a large distributed flood of bogus tokens; only Cloud Armor (quota) can. Each such request
  still costs one control-database read.
- Process-local state resets when an instance restarts or scales to zero (bounded by the figures above).
- A hostile user behind a shared NAT can consume that IP's failure budget (mitigated by the known-good fast path).
- The client-IP derivation must be validated in production before it is enforced.
- Revoked credentials answer 403 while unknown ones answer 401 (documented behaviour); the limiter responses do not add to
  that, and 256-bit tokens make enumeration infeasible.
- Compromised-but-valid credentials can still upload up to the per-device ceiling; per-organization quotas are future work.
- HSTS is sticky for its `max-age`: hence the staged ramp.

## 10. Agent polish backlog (not part of this work)

1. Handle the Windows logoff/shutdown notification so the session closes and flushes cleanly instead of being recovered as
   "interrupted" at the next start.
2. Quiet the harmless first-tick "input hooks not HEALTHY, status UNKNOWN" startup log line.
3. Retry sooner when Windows reports that network connectivity has returned (today recovery can take one to two minutes).
