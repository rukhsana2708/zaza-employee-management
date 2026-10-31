# Server privacy logging hotfix (2026-10-31)

## Status and production boundary

Code/tests and a new exact-commit image only. This hotfix is NOT deployed.
Approved production remains revision `zaza-server-00005-gxm`, control schema `c0004_sync_rate_limits`,
image `sha256:2d35877e2706c7e8d351a18558a68caf9b21ec1a1f725ed46cb6163297302b24`,
device limiter `shadow`, IP limiter `shadow`, HSTS `0`. Enforcement is not approved.
No production runtime/database change, Terraform apply, migration, agent change, enrollment action, DNS/Cloudflare
or Cloud Armor change is part of this hotfix. Existing historical production logs are not modified.

## Focused audit and changes

| Logger / path | Finding | Privacy-only correction |
|---|---|---|
| `service.py`, successful batch | Raw device ID in `device=%s`; confirmed in baseline and Stage D production logs | `device_ref` from the neutral keyed-reference helper; retain batch UUID, record count and result counts |
| `app.py`, rejected batch | Raw, untrusted device header (potentially any user-supplied sensitive string) | Reference the header without emitting its contents; retain fixed authentication reason/status |
| `app.py`, legacy connectivity warning | Raw authenticated device ID | Safe device reference; warning retained |
| `app.py`, storage unavailable | Arbitrary driver/exception text forwarded to the logger, potentially containing connection/input details | Exception class plus safe `error_ref`; keep ERROR severity and unchanged 503/Retry-After response |
| `dashboard/routes.py`, legacy failed login threshold | Raw client address | Safe `client_ref`; limiter decisions and warning retained |
| `dashboard/routes.py`, legacy CSRF rejection | Concrete request path can contain a sensitive path parameter | Registered route template; preserve useful route context without parameter values |

The legacy dashboard is not mounted in Cloud Run; its equivalent privacy violations are fixed without changing
its authentication, CSRF, response or limiting behavior.

Reviewed and retained: limiter events already avoid raw subjects; they now reuse the neutral helper.
Shared-counter failures/cleanup, tenant pool failures and enrollment compensation log exception classes only.
Enrollment logging uses an opaque enrollment RECORD UUID, never the one-time code or issued token.
PostgreSQL retry messages contain exception class/attempt counts; connection startup uses password-masked settings.
Attendance and Sheets logs contain counts. Secret redaction and registered database-password masking remain intact.
No additional direct employee/org ID, bearer token, enrollment code or password logger was found in these application paths.

Trusted CLI inventory/report output intentionally contains requested operator data; it is not changed by this
application-logging hotfix. Executing such inventory commands as Cloud Run jobs records that output in job logs.
Cloud Run's platform HTTP request metadata remains separate from application log messages and retains its normal
network fields. This audit does not claim that provider-managed access/audit logs contain no network addresses.

## Neutral reference helper and correlation limits

`logs.log_reference` uses HMAC-SHA256 with a random, private 32-byte in-process key, returning 16 lowercase hex
characters (or `-` for a missing value). A public SHA digest of a predictable device ID or IPv4 address could be
matched by guessing candidates; the private key prevents that offline lookup. The key is never configured,
persisted or logged. No new managed secret or deployment configuration is required.

References are deterministic for the same input within a running process. They rotate on restart and are not
a persistent cross-instance identity. This limits operational correlation deliberately. They are not tokens,
authentication decisions or database identities. External tooling cannot reproduce them from a raw ID/IP without
the private process key; the key must not be exported to enable such comparison. A future trusted-IP gate needs
separately reviewed privacy-safe diagnostics rather than an externally computed public SHA label.

Core sync imports this existing neutral logging module, not the rate-limiter module.
`ratelimit.digest` remains an import alias for compatibility and reuses the same helper for log labels.
Only logged references change: shared counter keys (`device_key`), authentication hashes, counter upserts,
cleanup policy, limiter thresholds/modes and Retry-After behavior are unchanged.

Regression coverage includes successful batches on memory, SQLite and disposable PostgreSQL; rejected arbitrary
headers; connectivity warnings; storage exceptions; legacy dashboard client IP/path warnings; helper stability,
key separation and bounded output; and unchanged shared-counter key derivation. Existing limiter privacy tests
remain part of the targeted run. No production load test is used.

## Stage D latency observation (no optimization)

| Sample | Requests | Median | Maximum |
|---|---:|---:|---:|
| Baseline, limiter off | 10 | 68.7 ms | 104.1 ms |
| Device shadow | 5 | 136.9 ms | 236.8 ms |

These samples are too small to establish a reliable regression or attribute causality. Collect a larger normal
shadow-mode sample before deciding whether optimization is needed. The shared PostgreSQL counter write remains
in place for every authenticated request admitted by the burst guard. This hotfix makes no limiter performance change.

## Validation

Disposable PostgreSQL 16, with `ZAZA_TEST_POSTGRES_URL` set only to the isolated `zaza_test` database:
- Targeted helper, sync API, limiter (including real PostgreSQL), and production-security/privacy tests: **276 passed**.
- Complete `tests/zaza_agent` PostgreSQL-enabled suite: **1730 passed, 12 skipped**.
- Changed Python file lint and Git whitespace checks: passed.

No production runtime setting or data was changed to run these tests.
