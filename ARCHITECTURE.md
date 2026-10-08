# ZaZa Employee Management System — Architecture

**Status:** Specification (Phase 0). No component below is deployed yet.
**Last updated:** 2026-10-06

## 1. System overview

```
 ┌────────────────────┐        offline-tolerant sync        ┌──────────────────────────┐
 │ Windows Agent (x5)  │ ───────────────────────────────────▶│ Central Sync API         │
 │ DeskMate-derived    │   HTTPS, batched events, retries     │ 127.0.0.1:8100 (future)  │
 │ - activity watcher  │◀──────────────────────────────────── │ on existing VPS           │
 │ - local SQLite queue│          sync ack / dedup result     └───────────┬──────────────┘
 └────────────────────┘                                                   │
                                                                           ▼
                                                              ┌────────────────────────┐
                                                              │ PostgreSQL 16 (existing)│
                                                              │ dedicated schema/DB     │
                                                              │ source of truth         │
                                                              └───────────┬─────────────┘
                                                                           │
                                               ┌───────────────────────────┼───────────────────────────┐
                                               ▼                                                       ▼
                                  ┌─────────────────────────┐                             ┌─────────────────────────┐
                                  │ Google Sheets sync job   │                             │ Manager Web Dashboard   │
                                  │ (summaries → 5 sheets)   │                             │ KPIs, filters, charts   │
                                  │ read-only, one-way view  │                             │ reads from PostgreSQL   │
                                  └─────────────────────────┘                             └─────────────────────────┘
```

PostgreSQL is the **only** source of truth. Google Sheets and the dashboard are
both read-side consumers / reporting layers derived from it — neither stores
anything that PostgreSQL doesn't already have.

## 2. Component 1 — Windows Employee Agent

Derived from DeskMate's capture layer (`deskmate/capture/`, `deskmate/a11y/`)
where useful, but stripped down to activity metadata only — none of DeskMate's
screenshot, OCR, or audio capture code is reused or enabled.

### 2.1 What it tracks

| Signal | Detail |
|---|---|
| Employee identity | Configured at install time, tied to a registered device |
| Registered device | One device record per machine, stable device ID |
| Work session start/end | Login → logout of the tracked session |
| Active application | Foreground process/application name |
| Active window title | Foreground window title text |
| Browser/domain info | Where technically practical: only the **currently active domain** (e.g. `github.com`), nothing more |
| Keyboard activity | Boolean presence of input in a time slice — **never the keys themselves** |
| Mouse activity | Boolean presence of input in a time slice |
| Idle status | Derived from keyboard+mouse inactivity vs. threshold |
| Computer lock/unlock | OS lock-state events |
| Online/offline status | Whether the agent currently has connectivity to the central API |
| Autostart | Registered to start with Windows |
| Work schedule | Pulled from central config, used for local attendance hints |

### 2.1.1 Browser/domain tracking boundary

"Browser/domain information" means exactly one thing: the **domain of the
currently active browser tab** (e.g. `github.com`, `mail.google.com`), read
only where technically practical (i.e., where the browser/OS exposes it
without reading page content). It explicitly excludes:

- Full URL paths (no `/repo/pull/123`, no file names, no query strings)
- Search or query parameters (`?q=...`, tracking params, form values)
- Browser history (only the *current* tab's domain, never prior tabs/visits)
- Page contents (no page text, no DOM, no title beyond the window-title field
  already covered in §2.1)

If a given browser doesn't expose even the domain without deeper inspection,
the agent simply records no domain for that window — it does not fall back
to reading more.

### 2.2 What it must never collect (hard constraint, enforced in code, not just policy)

Screenshots, screen recordings, typed text/keystroke content, clipboard
contents, microphone audio, webcam/video. No code path in the agent may touch
these. See [SECURITY.md](SECURITY.md).

### 2.3 Idle threshold

Configurable; **default 5 minutes** of no keyboard/mouse activity → idle.

Idle is only derived when the input it depends on can be trusted. The agent
tracks per-component health (`deskmate/zaza/health.py`: keyboard hook, mouse
hook, session-lock watcher, foreground-window watcher, SQLite storage), each
`HEALTHY` / `DEGRADED` / `ERROR`. Input is trustworthy **only while both the
keyboard and mouse hooks are `HEALTHY`**. In any other hook state (starting,
stopped, failed), the moment is recorded as `UNKNOWN` — never `IDLE` — the
affected raw activity flag and the raw `idle` column are NULL, and no
IDLE/CONTINUE events are emitted. A failed or restarting hook is never
mistaken for an inactive employee. Overall health is `ERROR` when storage is
down or both input hooks are down, `DEGRADED` when any component is not
healthy, and `HEALTHY` otherwise.

Status rules, evaluated every tick (`deskmate/zaza/agent.py`):

| Status (in evaluation order) | When |
|---|---|
| `LOCKED` | The session is known to be locked, and that lock state is trusted (see below). |
| `UNKNOWN` / `MONITORING_UNAVAILABLE` | Either input hook is not `HEALTHY`. |
| `ACTIVE` | Input observed within the idle threshold. Input proves presence, whatever the lock watcher's state. |
| `UNKNOWN` / `LOCK_STATE_UNAVAILABLE` | No recent input, and the lock state isn't trusted. Silence could just mean "locked", so IDLE is never inferred. |
| `IDLE` | The quiet time while hooks *and* lock state were trusted reached the threshold. |
| `UNKNOWN` / `AWAITING_INPUT` | Everything is trusted, but no input has been seen yet and the threshold hasn't elapsed (e.g. just after start, restart, or resume). |
| `UNKNOWN` / `TELEMETRY_GAP` | The agent wasn't sampling (sleep/hibernate, suspended process): wall time between ticks exceeded `max(3 x tick, 30s)`. |

**The idle threshold is a grace period.** IDLE begins at
`last input + threshold`, or `trusted since + threshold` if monitoring became
trustworthy later. It never starts at the last input itself. With a 5-minute
threshold, last input 10:00 and return at 10:06, the result is 10:00–10:05
ACTIVE and 10:05–10:06 IDLE. Each idle episode contributes only the time
beyond its own grace period. Right after start with no input, the first
threshold stays `UNKNOWN / AWAITING_INPUT`, and IDLE begins only at the
threshold crossing.

**Lock-state trust.** The session-lock watcher reports the session's current
lock state when it registers (`WTSQuerySessionInformationW`, reading only
`SessionFlags`). LOCK/UNLOCK notifications keep that state current. The agent
trusts its lock state only while the watcher is `HEALTHY` **and** the state
was established during the watcher's *current* `HEALTHY` stint. This is
tracked with a per-component health generation counter. If the watcher
fails, a remembered `LOCKED`/unlocked state becomes stale: the agent reports
neither LOCKED nor IDLE from it, only ACTIVE when input is actually observed,
and otherwise `UNKNOWN / LOCK_STATE_UNAVAILABLE`. After recovery, a fresh
state report is required, followed by a full threshold of trusted quiet time,
before IDLE can occur. If the current state can't be queried, the watcher
stays `DEGRADED` until a real LOCK/UNLOCK notification arrives.

`UNKNOWN` is never counted as idle. An UNLOCK counts as proof of presence
(the user just signed in), so the period after unlock starts `ACTIVE`.

### 2.4 Local storage & offline operation

- SQLite acts as a local **event queue**, not a long-term archive.
- All signals above are written locally first, regardless of connectivity.
- When offline, the agent keeps recording; nothing is lost.
- When connectivity returns, the agent:
  1. Sends unsynced events to the central API in order.
  2. Uses an idempotency key (device ID + event ID/sequence) so re-sent events
     are deduplicated server-side.
  3. Waits for a sync acknowledgement per batch.
  4. Marks locally synced records only after ack — never optimistically.

Phase 2 built the local side (the queue metadata below). Phase 3 implements
steps 1–4: see §3.1–3.8. Only summarized records are sent; raw
`activity_events` never leave the machine.

### 2.5 Local SQLite schema (Phase 2, schema v2)

File: `~/.zaza_agent/activity.db` (`ZAZA_HOME` overrides). WAL journal,
`synchronous=NORMAL`, `busy_timeout=5000`. All timestamps are UTC ISO-8601
with millisecond precision. Migrations are forward-only, one transaction
each, recorded in `schema_meta.schema_version`; a Phase 1 (v1) database is
upgraded in place, and a database newer than the agent is refused.

| Table | Purpose | Synced in Phase 3? |
|---|---|---|
| `activity_events` | Raw per-tick observations + APP_CHANGE / IDLE / CONTINUE / LOCK / UNLOCK / SESSION_START / SESSION_END / TELEMETRY_GAP. `event_id` (UUID, unique), `session_id`, `status`, `privacy_excluded`, plus the Phase 1 columns. | No — local only, 7-day retention |
| `work_sessions` | One row per agent run: `session_id`, `started_at`, `ended_at`, `last_heartbeat_at`, `status` (OPEN / CLOSED / INTERRUPTED), `start_reason`, `end_reason`, `previous_session_id`, and tracked / active / idle / unknown / locked seconds. | Yes |
| `activity_periods` | Summarized periods: `period_id`, `session_id`, `started_at`, `ended_at`, `duration_seconds`, `is_open`, `status` (ACTIVE / IDLE / UNKNOWN / LOCKED), `status_detail`, `app_name`, `window_title`, `domain`, `privacy_excluded`, `start_reason`, `end_reason`. | Yes |
| `idle_periods` | Merged idle stretches: `idle_id`, `session_id`, `started_at`, `ended_at`, `duration_seconds`, `is_open`, `end_reason`. | Yes |
| `app_usage_daily` | Per local day, per app: active / idle / unknown seconds and `period_count`. Unique on (device, day, app). | Yes |
| `sync_state` | Key/value for Phase 3 cursors (e.g. last successful sync). | — |
| `counters` | `local_seq`: one device-wide, strictly increasing sequence. | — |
| `schema_meta` | `schema_version`. | — |

Every synced row carries `device_id`, `local_seq` (device-local creation
order), `created_at` / `updated_at`, and sync metadata: `record_version`
(incremented on every change), `sync_status` (PENDING / SYNCED / FAILED),
`sync_attempts`, `last_sync_attempt_at`, `last_sync_error`, `synced_at`,
`synced_version`. IDs are UUIDv4, except `app_usage_daily.usage_id`, which
is UUIDv5 of (device, day, app), so a recomputed rollup keeps its ID. Phase 3
dedups by upserting on the ID and only accepts a higher `record_version`.
`mark_synced(id, version)` succeeds only if the row is still at that version,
so a change made during an upload keeps the row PENDING. Any later change
sets a SYNCED row back to PENDING.

Indexes cover timestamps (`ts`, `started_at`, `ended_at`), session lookups,
open-period lookups, and `(sync_status, local_seq)` for queue scans.

### 2.6 Activity period aggregation rules (Phase 2)

Implemented in `deskmate/zaza/aggregator.py`. Raw ticks become periods; the
reporting unit is the period, not the tick.

- **Period key** = status + application + domain (+ the UNKNOWN reason).
  Consecutive ticks with the same key extend one period (four 10-second
  VS Code ticks become one 40-second ACTIVE period).
- **A new period starts** when the key changes: app switch, ACTIVE to/from
  IDLE, LOCK, UNLOCK, monitoring becoming untrustworthy or trustworthy again,
  telemetry gap, session start/end.
- **Window titles** are normalized first: unread counters such as `(3)`,
  unsaved markers (`●`, trailing `*`), control characters, and repeated
  whitespace are removed, and the title is capped at 256 characters. A
  *different* normalized title in the same app splits the period only after
  it has been stable for `title_debounce_seconds` (default 30s). The split
  goes where that title was first seen. Shorter flicker is absorbed.
- **Placement:** an app/title change is placed at the tick that observed it.
  A status change is placed at its real time: idle starts when the grace
  period ran out (last input + threshold), and activity resumes at the input
  that ended the idle stretch. This is always clamped to the open period, so
  closed periods are never rewritten.
- **Contiguity:** within a session, each period starts exactly where the
  previous one ended. The period durations add up to the session's
  `tracked_seconds`.
- **Idle periods** merge consecutive IDLE activity periods (an app switch
  while idle doesn't split them). They close on activity
  (`ACTIVITY_RESUMED`), LOCK, monitoring loss (`MONITORING_UNKNOWN`), a
  telemetry gap, or session end.
- **App-usage rollups** are recomputed from periods whenever a period closes,
  for every local day it touches. Periods crossing midnight are split at the
  day boundary. LOCKED periods and periods without an app are excluded.
  There is no productivity classification.
- **One tick is one transaction:** the period update, session heartbeat and
  totals, and raw event commit together. If a write fails, the tick rolls
  back and the aggregator reloads its state from the database.

### 2.7 Work sessions & crash recovery (Phase 2)

- A session opens when the agent starts (`AGENT_START`) and closes on a clean
  stop (`CLOSED` / `AGENT_STOP`). Each tick heartbeats it and refreshes its
  totals.
- On the next start, any session still `OPEN` belongs to a run that died. It
  becomes `INTERRUPTED`, ended at its **last heartbeat**, and its open
  periods and idle period close there with `end_reason = INTERRUPTED`. No
  time after the last heartbeat is attributed to anything. The new session
  records `AGENT_START_AFTER_INTERRUPTION` and `previous_session_id`, and
  starts `UNKNOWN` until input is seen.
- If the wall clock steps backwards by more than 2s, the session is closed
  (`CLOCK_CHANGED`) at the last good timestamp and a new one starts. Smaller
  jitter is absorbed.
- Attendance policy (late/early/overtime) is **not** computed locally — that
  belongs to Phase 5.

### 2.8 Local retention (Phase 2)

| Data | Default | Rule |
|---|---|---|
| Raw `activity_events` | 7 days (`ZAZA_RAW_RETENTION_DAYS`) | Deleted by age, **including** events of a still-OPEN session, so a weeks-long session can't grow the raw log without bound. Raw events are never synced and never read back: the open period, idle period, and session state live in their own tables, which is what aggregation and crash recovery use. |
| Synced records (sessions, periods, idle periods, usage) | 30 days after end (`ZAZA_SYNCED_RETENTION_DAYS`) | Deleted only if `SYNCED` at their current version, closed, and older than the window. A session is deleted only after all its periods/idle periods are gone. |
| PENDING / FAILED records | Never deleted | However old. |

Cleanup runs at agent start and hourly, in batches of 5,000 rows, each batch
its own short transaction. Because Phase 3 isn't built yet, nothing is
SYNCED today, so summarized data is currently retained indefinitely. At
roughly a few hundred periods per working day, that is a few MB per year.

## 3. Component 2 — Central Synchronization API

- Future home: existing Windows Server 2022 VPS, bound to `127.0.0.1:8100` so
  it is not exposed beyond whatever reverse proxy / firewall rule the VPS
  already uses for other apps. **Not deployed in this phase.**
  - At `127.0.0.1:8100`, the service is reachable only from processes on the
    VPS itself; exposing it to the agents on the 5 employee machines will
    require an explicit, deliberate choice (e.g. a reverse-proxy rule or a
    VPN) made during Phase 10 deployment — not an accidental side effect of
    this binding.
- Responsibilities: authenticate agents/devices, accept batched activity
  events, deduplicate, write into PostgreSQL, expose read endpoints for the
  dashboard, and trigger summary/aggregation jobs.
- Must remain isolated from whatever else already runs on that VPS: its own
  process, own port, own database/schema, no shared state with existing
  applications.

Phase 3 implemented the sync contract, the server (`deskmate/zaza_server/`)
and the agent's sync client (`deskmate/zaza/sync/`). Phase 4 added the
PostgreSQL repository behind the same interface (§4). Deployment comes in
Phase 10.

```
agent sampler ──> local SQLite ──> sync worker (own thread, own DB connection)
                                     │  HTTPS, batched, per-device token
                                     v
                       FastAPI app (zaza_server/app.py)
                                     v
                       SyncService (service.py): per-record validation,
                       device/employee binding
                                     v
                       CentralRepository (repository.py)
                         ├─ InMemoryRepository   (tests)
                         ├─ SqliteDevRepository  (local development)
                         └─ PostgresRepository   (Phase 4, production; §4)
```

### 3.1 API contract (`/api/v1`, `deskmate/zaza/sync/protocol.py`)

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /api/v1/sync/health` | none | Liveness probe: `{status, api_version, server_time}`. No data. |
| `GET /api/v1/devices/me` | device | Credential check: `{device_id, employee_id, status}`. |
| `POST /api/v1/sync/batch` | device | Upload 1–500 records of any synced type; per-record results. |

**Request** (`SyncBatchRequest`, unknown fields rejected):
`batch_id` (UUID), `device_id`, `agent_version`, `sent_at` (UTC), and
`records`. Each record has: `record_type` (`work_session` /
`activity_period` / `idle_period` / `app_usage_daily`), `record_id` (UUID),
`record_version` (≥1), `device_id`, `employee_id`, `local_seq`,
`created_at`, `updated_at` (UTC), and `data`. The `data` field is
type-specific, mirrors the local table minus sync bookkeeping, and also
rejects unknown fields.
`app_usage_daily` carries its local `usage_date` plus `day_start_utc` /
`day_end_utc`, so the "local day" is unambiguous.

**Timestamps:** every timestamp must be UTC with an explicit offset (`Z` or
`+00:00`). Naive or non-UTC timestamps are rejected. Time-zone conversion
belongs to reporting.

**Response** (`SyncBatchResponse`, unknown fields ignored, for forward
compatibility): `batch_id`, `server_time`, `results[]` (one per submitted
record, same order: `record_type`, `record_id`, `record_version`, `status`,
`server_version`, `error`), and `summary` counts per status.

**Limits:** 500 records and 4 MiB per request. The byte cap is enforced while
reading, before parsing (413). Envelope errors (unknown fields, non-UTC
`sent_at`, empty or oversized `records`, `device_id` ≠ authenticated
device) return 422. A malformed *record* is rejected individually; the rest
of the batch is still processed.

### 3.2 Idempotency & versions

Key: `(record_type, record_id)`. The record's content hash covers its data,
`local_seq`, `employee_id` and `created_at`.

| Server already has | Client sends | Result | Server state |
|---|---|---|---|
| nothing | v_n | `accepted` | stores v_n |
| v_n, same content | v_n | `already_current` | unchanged (resend after a lost ack, restart, timeout) |
| v_n | v_m, m > n | `updated` | replaced by v_m |
| v_n | v_m, m < n | `stale` (+ `server_version` = n) | **unchanged**; newer data never overwritten |
| v_n, different content | v_n | `conflict` | unchanged |
| a record owned by another device | any | `rejected` | unchanged |

Example: the server holds `period ABC v4` and the client sends v3. The
server keeps v4 and answers `stale, server_version 4`.

### 3.3 Partial acknowledgement (client)

The client acts per record, never per batch:

- `accepted` / `updated` / `already_current` → `mark_synced(id, sent_version)`.
  This is version-checked: if the record changed locally while the request
  was in flight, it stays PENDING and the newer version is sent next cycle.
- `stale` → the server's newer copy wins; the local `record_version` is
  raised to `server_version` and marked SYNCED. Any *later* local change
  then gets a higher version the server will accept. This applies only
  when `server_version > sent version`. A `stale` with a missing, equal or
  lower `server_version` is a protocol fault: the record is marked `FAILED`
  (retried), never SYNCED. If the record changed locally in flight, the
  adoption is skipped and the newer local version stays PENDING.
- `conflict` → the local version is bumped and re-queued, so the device's
  current content goes out as a strictly newer version.
- `rejected`, or no result for a record → `FAILED` with the error stored;
  retried on later cycles. Never deleted.

### 3.4 Sync worker & retry

`deskmate/zaza/sync/worker.py` runs on its own thread with its own SQLite
connection, so sampling never waits on the network.

**Each cycle:**

- Collects PENDING/FAILED records from the four tables in `local_seq` order,
  `sync_batch_size` per request (default 100, max 500), up to
  `sync_max_batches_per_cycle` (default 20) requests per cycle.
- Open records (the current session, period and idle period) are sent too,
  so the server stays current. Their version changes every tick, so they
  are simply re-sent.

**When the next cycle runs:**

| Outcome | Next attempt |
|---|---|
| Success, nothing left | `sync_interval_seconds` (default 60s) |
| Success, more pending (hit the per-cycle cap) | 1s |
| Network error (DNS, refused, timeout, TLS) / 5xx / malformed response | Exponential backoff with ±20% jitter: ~5s, 10s, 20s, 40s … capped at 300s; reset on success |
| HTTP 429 | Backoff, but at least `Retry-After` (capped at 300s) |
| HTTP 401/403 | Fixed ~15 min (±20%): not a transient fault, never a tight loop |
| HTTP 413 | Batch size halved and retried immediately |

A transport-level failure marks nothing: every record stays exactly as it
was. Sync never deletes data, and sync state never affects activity
classification: being offline is not inactivity.

### 3.5 Sync health

Stored locally in `sync_state` and shown by `python -m deskmate.zaza
sync-status`:

| State | Meaning |
|---|---|
| `HEALTHY` | Last cycle succeeded and the backlog is small (open records don't count). |
| `BACKLOG` | Last cycle succeeded, but at least one closed record is still unconfirmed (pending or failed), or the per-cycle cap was hit. Open records (current session/period) don't count. |
| `OFFLINE` | Network unreachable (DNS, refused, timeout, TLS). |
| `AUTH_ERROR` | 401/403: invalid, revoked, or disabled device. |
| `SERVER_ERROR` | 5xx, 429, malformed response, or unexpected status. |
| `NOT_CONFIGURED` | No sync URL or credentials, or nothing attempted yet. |

It also tracks `last_success_at`, `last_attempt_at`, `last_error`,
`consecutive_failures`, `next_attempt_at`, and pending / failed / open
record counts.

### 3.6 Device authentication

- **Tokens:** an opaque random bearer token per device (256-bit, `zzd_`
  prefix). Every request sends `Authorization: Bearer <token>` plus
  `X-ZaZa-Device-Id`, and the token must belong to that device.
- **Server side:** only the SHA-256 hash of each token is stored.
  - 401 = missing, unknown, or mismatched credentials.
  - 403 = revoked token or `DISABLED` device.
- **Rotation:** a device can hold several active tokens, so rotation is
  "issue new → switch agent → revoke old".
- **Agent side:** the token lives in `<ZAZA_HOME>/device_credentials.json`,
  encrypted with Windows DPAPI for the current user. That file is outside
  the SQLite database and outside the source code.
- **Failures keep data:** an authentication failure never deletes local data
  or credentials.

### 3.7 Repository boundary

```
FastAPI app (app.py)          — HTTP, auth headers, size limits; no SQL
      │
SyncService (service.py)      — per-record validation, device/employee binding
      │
CentralRepository (repository.py, a Protocol)
      ├── InMemoryRepository        tests
      ├── SqliteDevRepository       local development (generic JSON table)
      └── PostgresRepository        production (postgres/repository.py)
```

`CentralRepository` is the only persistence interface. It covers:
- **Employees:** `add_employee`, `get_employee`, `list_employees`.
- **Devices and tokens:** `add_device`, `get_device`, `list_devices`,
  `set_device_status`, `add_token`, `find_token`, `revoke_tokens`,
  `touch_device`.
- **Records:** `upsert_records` (one outcome per record), `get_record`,
  `list_records`, `count_records`.
- **Lifecycle:** `close`.

The version and idempotency decision is the shared pure function `decide()`,
so every backend behaves identically. The same API conformance tests
(`tests/zaza_agent/test_sync_server.py`) run against all three backends. The
wire protocol and the agent did not change for PostgreSQL.

Rules every backend enforces:
- A device belongs to an existing employee.
- An activity or idle period's `session_id` names a work session already
  synced **by the same device**. Otherwise the period is `rejected` with
  "session_id is not a synced work_session of this device". The agent sends
  rows in `local_seq` order, so a session always precedes its periods. A
  period rejected this way stays FAILED on the agent and is retried, so it
  heals itself once the session arrives.

### 3.8 Running the server, and choosing a backend

`python -m deskmate.zaza_server serve` listens on `127.0.0.1:8765` by
default. The backend comes from `--backend` or `ZAZA_SERVER_BACKEND`:
- `sqlite` (default) uses `~/.zaza_server_dev/central_dev.db`.
- `postgres` reads the settings in §4.1.

Admin commands:
- `add-employee`, `list-employees`
- `register-device`, `rotate-token`
- `disable-device` / `enable-device`
- `list-devices`, `records`
- PostgreSQL only: `migrate`, `db-status`

Plain HTTP is for localhost development only. The agent refuses `http://` to
any other host unless `ZAZA_SYNC_ALLOW_INSECURE_HTTP=1` is set explicitly.

## 4. Component 3 — PostgreSQL central storage (Phase 4)

PostgreSQL is the central source of truth. In production it will be the
VPS's existing PostgreSQL 16 instance: **no new PostgreSQL server is
created**. This system gets its own database there, created only in
Phase 10 with separate approval, and other VPS databases are left
untouched. Phase 4 was built and tested locally only; nothing connected to
the VPS.

**Libraries** (optional extra `pip install -e .[zaza-postgres]`):
- **psycopg 3** is the driver, with `psycopg_pool` for connection pooling.
  It is mature, typed and has native TIMESTAMPTZ, UUID and JSONB support.
  The repository uses plain SQL, which keeps it simple, explicit and
  reviewable, with no ORM.
- **Alembic** is used for migrations only. It needs SQLAlchemy, but no
  SQLAlchemy models exist.
  - On Windows machines with Application Control (Smart App Control),
    SQLAlchemy's optional compiled extensions can be blocked, so
    `migrate.py` uses its pure-Python mode.

### 4.1 Configuration (environment only)

| Variable | Default | Meaning |
|---|---|---|
| `ZAZA_SERVER_BACKEND` | `sqlite` | `postgres` for production |
| `ZAZA_DATABASE_URL` | — | `postgresql://user:pass@host:5432/db` (percent-encode the password) |
| `ZAZA_DB_HOST` / `_PORT` / `_NAME` / `_USER` | `127.0.0.1` / `5432` / `zaza` / — | separate parts (override the URL) |
| `ZAZA_DB_PASSWORD_FILE` | — | preferred: file readable only by the service account |
| `ZAZA_DB_PASSWORD` | — | alternative to the file |
| `ZAZA_DB_SSLMODE` | `prefer` | libpq sslmode |
| `ZAZA_DB_SCHEMA` | (public) | optional dedicated schema (sets `search_path`) |
| `ZAZA_DB_POOL_MIN` / `_MAX` | `1` / `4` | pool size; max is capped at 20 |
| `ZAZA_DB_POOL_TIMEOUT` | `10` s | wait for a free connection, then 503 |
| `ZAZA_DB_CONNECT_TIMEOUT` | `5` s | |
| `ZAZA_DB_STATEMENT_TIMEOUT_MS` | `15000` | per statement |

`.env.example` lists these with placeholders only. Real `.env` files are
git-ignored. Every session runs with `TimeZone=UTC`.

### 4.2 Schema (migration `0001_initial`)

| Table | Key | Purpose |
|---|---|---|
| `employees` | `employee_id` | name, role (EMPLOYEE/MANAGER/ADMIN), `is_active`, reporting `timezone` (IANA, validated by a trigger) |
| `devices` | `device_id` | owning employee, display name, `status`, `disabled_at`, `last_seen_at` |
| `device_tokens` | `token_id` | `token_hash` (SHA-256 hex only), status, `created_at` / `last_used_at` / `revoked_at` |
| `work_schedules` | `schedule_id` | a weekly rule (`day_of_week` + effective range) **or** a one-off `schedule_date`; working day, start/end time, expected seconds, timezone. Prepared only: no attendance logic yet (Phase 5). |
| `work_sessions` | `session_id` | start/end/heartbeat, status, reasons, tracked/active/idle/unknown/locked seconds |
| `activity_periods` | `period_id` | session, start/end, duration, status (+ detail), app, privacy-safe title/domain, `privacy_excluded`, reasons |
| `idle_periods` | `idle_id` | session, start/end, duration, end reason |
| `application_usage_daily` | `usage_id` | device-local `usage_date` with its exact UTC bounds, app, active/idle/unknown seconds, period count |
| `audit_logs` | `audit_id` (identity) | actor type and id, action (`entity.verb`), entity, old/new values (JSONB), `occurred_at`. **Append-only** (triggers block UPDATE, DELETE and TRUNCATE). |

**Sync and idempotency columns.** Each of the four synced tables also has:
- Identity and ownership: `device_id`, `employee_id`.
- Versioning: `record_version`, `local_seq`, `content_hash`.
- Agent timestamps: `device_created_at` and `device_updated_at` (from the
  agent).
- Server bookkeeping: `first_received_at`, `last_received_at`,
  `last_batch_id`.
- `payload`: the validated wire record as received, in JSONB.

Reporting must use the **typed columns**. `payload` exists for audit and
forward compatibility only.

**Time.** Every instant is `TIMESTAMPTZ`. Nothing stores ambiguous local
time. The only local-calendar value is `usage_date`, which is always paired
with `day_start_utc` / `day_end_utc`. Reporting time zones are separate
columns (`employees.timezone`, `work_schedules.timezone`).

**Schedule day rule (binding for Phase 5 attendance calculations).** A
schedule belongs to the local calendar date or day on which the shift
**starts** (`schedule_date` / `day_of_week`, in `work_schedules.timezone`).
- If `end_time` is later than `start_time`, the shift ends the same day.
- If `end_time` is earlier, the shift ends on the **following** local day.
- Examples: 09:00→17:00 on Monday is Monday 09:00 to Monday 17:00, an 8 h
  span. 20:00→04:00 on Monday is Monday 20:00 to Tuesday 04:00, also 8 h.
- Phase 5 turns this into UTC instants, using the time-zone rules in force
  on those dates, before comparing with activity. Schedules are never
  stored as timestamps.
- A schedule is time-of-day plus a time zone. Across a DST change the real
  elapsed time can differ from the nominal span by ±1 h. The database
  checks the nominal span; Phase 5 uses the real instants.

The Phase 5 summary tables (migration `0002_attendance_summaries`) are
described in §4.11.

**Not here, on purpose:**
- `manager_users` (Phase 7).
- A central `sync_state` table: per-record versions plus `content_hash` are
  the idempotency state, and there is no server-side watermark.

### 4.3 Relationships

```
employees ─┬─< devices ──< device_tokens
           ├─< work_schedules
           │
           │   (every synced row: device_id → devices, employee_id → employees)
           │
           └─< work_sessions ─┬─< activity_periods   FK (session_id, device_id)
                              └─< idle_periods       FK (session_id, device_id)
               application_usage_daily               UNIQUE (device_id, usage_date, app_name)

audit_logs — entity_type + entity_id (no FK: entries outlive what they describe)
```

- **Sessions are per device.** Periods reference `(session_id, device_id)` →
  `work_sessions(session_id, device_id)`, so a period can only attach to a
  session of the **same device**.
- **The employee on a record is the device's employee at sync time.** This
  is deliberately not a foreign key through `devices`, so reassigning a
  device later never rewrites history.
- **App usage is keyed by device, day and app.** The agent's deterministic
  `usage_id` (UUIDv5 of device, date and app) maps 1:1 to that unique key.
  It references employee and device directly.

### 4.4 Integrity constraints (enforced by PostgreSQL, not only Python)

All constraints are named, so a violation maps to a short, safe error
without echoing row data.
- **Keys:** primary keys; foreign keys with `ON DELETE RESTRICT`; unique
  token hash; unique device/day/app usage; and per-employee unique
  schedules (partial unique indexes).
- **Versions and ordering:** `record_version >= 1` and `local_seq >= 0`.
- **Content hash:** `content_hash` must be 64 hex characters.
- **Durations:** `BETWEEN 0 AND max`, which also rejects NaN and Infinity.
  The maximum is 10 years, or 26 h for daily app usage.
- **Time order:** end ≥ start for sessions and periods. Usage day bounds
  must be ordered and at most 26 h apart.
- **Valid states:**
  - Allowed values for every status and role.
  - `OPEN` sessions have no end; `CLOSED` and `INTERRUPTED` have one.
  - A disabled device has `disabled_at`, and a revoked token has
    `revoked_at`.
- **Privacy:** `privacy_excluded` periods may only carry the fixed
  placeholders "Excluded / Private" (title) and "Excluded / Private Site"
  (domain).
- **Text and identifiers:** text length limits mirror the wire protocol, and
  employee and device ids follow the protocol's identifier pattern.
- **Schedules:**
  - A rule is either weekly or a single date, never both.
  - A working day has a start and an end that differ (equal times are
    rejected, never read as a 24-hour shift; `24:00` is not allowed). The
    shift may cross midnight. Expected seconds must be **> 0** and no longer
    than the shift's span.
  - A day off has no hours, and expected seconds are NULL or 0.
- **Time zones:** an invalid IANA time zone is rejected by a trigger.

### 4.5 Idempotency and concurrency

The Phase 3 outcome table (§3.2) is preserved exactly; the same
conformance tests prove it. For each record, the repository runs:

1. `SELECT … FOR UPDATE` on the record's row. Concurrent requests for the
   same record queue up here.
2. If there is no row: `INSERT … ON CONFLICT (id) DO NOTHING RETURNING`.
   - A returned row means the record was inserted: `accepted`.
   - If nothing comes back, another request inserted it a moment earlier.
     The repository waits for that request's commit, re-selects the row
     `FOR UPDATE`, and decides against it.
3. `decide(existing, incoming)` runs on the **locked, committed** row. Only
   `updated` writes.

So two submissions can never both win. A lower version never overwrites a
higher one. Of N identical concurrent inserts, exactly one is `accepted` and
the rest are `already_current`. Tests cover all three races: mixed versions,
identical submissions, and same version with different content.

`last_received_at` is set from `clock_timestamp()`, never `now()`.
PostgreSQL's `now()` is the time the transaction *started*. A request that
waited on another request's row lock can have started before that row was
first received, which would break `last_received_at >= first_received_at`.
The 160-submission race test caught exactly this.

### 4.6 Transactions

**Design:** one transaction per batch, and one **savepoint per record**.
- A constraint failure (CHECK, FK or UNIQUE) or bad data rolls back to that
  record's savepoint only. That record is `rejected` with a short reason,
  and the rest of the batch commits. This keeps Phase 3's per-record
  results.
- The batch commits all its successful records together. The database is
  never left half-written by a crash mid-batch.
- A **deadlock or serialization failure** (SQLSTATE class 40) rolls back the
  whole batch transaction, which is then retried, up to 3 attempts. If every
  attempt fails, the API answers 503 and the agent retries later.
- Isolation is READ COMMITTED. Correctness comes from row locks and the
  primary-key conflict, not from SERIALIZABLE.
- Admin changes (employee, device, token) run in their own transaction
  together with their audit row, so they are audited atomically.

Within a batch, records are processed in submission order (`local_seq`). A
session therefore precedes its periods, and two concurrent batches from one
device lock rows in the same order.

### 4.7 Migrations (Alembic)

- **Location:** revisions live in
  `deskmate/zaza_server/postgres/migrations/versions/` and are version
  controlled and shipped in the wheel.
- **Format:** each revision is explicit SQL with named constraints.
- **Running them:** an operator runs `python -m deskmate.zaza_server
  migrate`, which upgrades to the latest revision.
  - It is idempotent: at the latest revision it reports "already up to date"
    and changes nothing.
  - Each revision runs in its own transaction. PostgreSQL DDL is
    transactional, so a failed migration leaves the previous revision
    intact.
- **The server never migrates.** `serve` (and the repository) checks the
  schema revision at start-up. If it is not current, it refuses to start and
  says to run `migrate`, so there are no silent or destructive schema
  changes.
- **Inspecting:** `db-status` shows the current and latest revision.
  `render_sql()` produces the DDL offline for review (unit tests check it).
- **Rules for future revisions:**
  - Preserve data: add or alter rather than drop. A column holding data is
    only removed after a copy step.
  - Keep TIMESTAMPTZ and named constraints.
  - Downgrade functions exist for completeness, but the CLI does not expose
    them.

### 4.8 Connection pooling

`psycopg_pool.ConnectionPool` uses `min_size=1`, `max_size=4` by default.
Both are configurable, and the maximum is capped at 20. Idle connections
close after 5 min, and every connection is recycled after 1 h. Five agents
syncing once a minute need about one connection. When the pool is exhausted
for `ZAZA_DB_POOL_TIMEOUT` seconds the API answers 503 with `Retry-After`,
and the agents back off. Repository calls run in the API's thread pool,
never on the event loop.

### 4.9 Indexes

| Query (known future need) | Index |
|---|---|
| employee + date range / start time | `(employee_id, started_at)` on sessions, activity periods, idle periods |
| app usage by employee / day; daily summaries input | `(employee_id, usage_date)` on `application_usage_daily` |
| currently open / recent sessions | partial `(employee_id, last_heartbeat_at) WHERE status = 'OPEN'` |
| session → periods (and the composite FKs) | `(session_id, device_id)` on activity and idle periods |
| device + sync order | `(device_id, local_seq)` on all four synced tables |
| devices / tokens of an employee or device | `devices(employee_id)`, `device_tokens(device_id)` |
| audit history of an entity / time | `(entity_type, entity_id, occurred_at)`, `(occurred_at)` |

Nothing else is indexed yet. Phase 5 adds indexes for its summary tables
when the queries are known.

### 4.10 Production deployment assumptions (Phase 10, needs separate approval)

1. **Database:** create a dedicated database (e.g. `zaza`) on the existing
   PostgreSQL 16 instance, using two roles:
   - `zaza_owner` owns the schema and runs `migrate`.
   - `zaza_app` runs the API with least privilege. The grants are in
     SECURITY.md §6.2.
2. **API placement:** the API listens on `127.0.0.1` behind an HTTPS reverse
   proxy. The database connection is local (`sslmode=prefer`), or
   `require`/`verify-full` if it is ever remote.
3. **Credentials:** use the service account's environment or
   `ZAZA_DB_PASSWORD_FILE`, never a committed file.
4. **Upgrade order:** stop the API, back up (`pg_dump`), `migrate`, start
   the API.
5. **Pool sizing:** keep it at the defaults. Check the instance's
   `max_connections` headroom before deploying.

These steps were rehearsed locally on a throwaway PostgreSQL 16:
- Owner migrated, app role granted.
- The app role was refused DROP, DELETE and CREATE.
- The live agent synced, an outage was simulated, and it caught up.
- No secret appeared in any log.

### 4.11 Attendance & work-time summaries (Phase 5)

`daily_summaries`, `weekly_summaries` and `monthly_summaries` (migration
`0002_attendance_summaries`) hold the **authoritative** attendance figures.
Migration `0003_schedule_timezone` adds the schedule-timezone rule
(§4.11.2).
Google Sheets, the dashboard and charts (Phases 6–8) read these rows and
must never recalculate them.

**Code:** `deskmate/zaza_server/attendance/`:
- `calculator.py`: the daily calculation, a pure function. The same inputs
  always give the same output.
- `schedule.py`: schedule resolution and DST-correct UTC times.
- `timeline.py`: overlap removal.
- `rollup.py`: week and month.
- `summary_service.py`: orchestration.
- `postgres_store.py`: storage.

No calculation lives in FastAPI routes.

Activity is a *proxy* for work, not proof of it. None of these figures is a
productivity score, and the UI must not present them as one.

#### 4.11.1 Inputs

- **Used:** `employees` (time zone), `work_schedules`, `activity_periods`
  (from all of the employee's devices), `work_sessions` (count, open or
  interrupted state), and `devices` (`status`, `last_seen_at`).
- **`idle_periods` are not added to the totals.** They describe the same
  time as the IDLE activity periods, so adding them would count idle time
  twice.
- **`application_usage_daily` is not folded into attendance.** Its date is
  the *device's* calendar day, which for an overnight shift is not the
  shift's date. The dashboard reads app usage from that table directly.

#### 4.11.2 Which schedule applies to a date

For employee E and local date D:
1. A one-off rule with `schedule_date = D` wins.
2. Otherwise, a weekly rule with `day_of_week = ISO weekday(D)` (1 = Monday)
   and `effective_from ≤ D ≤ effective_to` (open-ended when NULL). If
   several rules match, the newest `effective_from` wins.
3. Otherwise there is no schedule, and the status is `NO_SCHEDULE`.

**One timezone per employee (Phase 5 limitation).** A schedule's
`timezone` must equal the employee's reporting timezone
(`employees.timezone`). Date attribution (§4.11.3) uses the employee's
timezone and shift times use the schedule's, so a different zone would give
inconsistent days. This is enforced three ways:
- `add_schedule()` and `add-schedule` default to the employee's timezone and
  reject any other one with a clear error;
- the database: `work_schedules (employee_id, timezone)` is a foreign key to
  `employees (employee_id, timezone)` (`work_schedules_timezone_matches_employee`),
  so direct SQL can't insert a mismatched rule, and an employee's timezone
  can't be changed while schedules in the old zone exist;
- migration `0003` refuses to apply if existing rows already mismatch.

Per-schedule ("travel") timezones are not supported. Changing an employee's
timezone needs a deliberate migration of their schedules (and a
recalculation), not a quiet update.

**Shift times.** A shift belongs to the date it **starts** on, in the
employee's (= the rule's) time zone:
- The shift starts at D `start_time`.
- It ends at D `end_time` if `end_time > start_time`. Otherwise it ends at
  (D+1) `end_time`, i.e. the next day.
- Both are converted to UTC with the IANA rules in force on those dates. A
  local time that doesn't exist (spring-forward gap) moves forward by the
  gap; an ambiguous one (fall-back) takes its first occurrence. A shift that
  is affected gets the informational flag `DST_ADJUSTED`.
- `scheduled_seconds = min(expected_work_seconds, real shift length)`.
  Example: a 22:00→06:00 night that loses an hour to DST really lasts 7 h,
  so 7 h are expected, not 8. On the night clocks go back, the shift lasts
  9 h and 8 h are still expected.

#### 4.11.3 Which date an instant belongs to (no double counting)

Every instant belongs to **exactly one** local date:
1. the date whose shift contains it; if shifts overlap, the earliest-starting
   one (flag `SCHEDULE_OVERLAP`);
2. otherwise, the date of the nearest shift that starts or ends within
   `attribution_margin` (4 h by default) of it;
3. otherwise, its calendar date in `employees.timezone`.

Because this is a function of the instant alone, the days partition time. A
week or month never counts a moment twice. A calendar day without a shift
also follows DST, so it can be 23 or 25 h long.

Examples:
- **20:00→04:00 shift on Monday:** activity from 19:30 to 04:45 belongs to
  Monday. That is 30 min before the shift, 8 h in it, and 45 min after it.
- **09:00→17:00 shifts on Monday and Tuesday:**
  - Work until 20:30 on Monday belongs to Monday, because it's within 4 h of
    Monday's shift end.
  - Work from 00:00 to 01:00 on Tuesday belongs to Tuesday, as its calendar
    date. It counts as Tuesday time before the shift, but it can't hide a
    late arrival on Tuesday (see Late in §4.11.4).

#### 4.11.4 Daily formulas

**The timeline.** Periods from all devices are merged into one
non-overlapping timeline. Where periods overlap, the instant takes one
status, by priority:
- ACTIVE > IDLE > LOCKED > UNKNOWN.
- Overlaps are never added twice, and `OVERLAPPING_PERIODS` records that the
  input overlapped.
- Time with no data at all (PC off, agent not running) is **untracked** and
  belongs to none of the four statuses.

Everything below is measured inside the date's window from §4.11.3.

| Metric | Exact rule |
|---|---|
| **Active Hours** (`active_seconds`) | time whose status is ACTIVE. Login time is never used. |
| **Idle Hours** (`idle_seconds`) | IDLE time. The agent's 5-minute idle grace is already applied (Phase 2), and nothing more is subtracted. |
| **Unknown Hours** (`unknown_seconds`) | UNKNOWN time: monitoring unavailable or uncertain. Never counted as active or idle. |
| **Locked Hours** (`locked_seconds`) | LOCKED time. Never counted as idle. |
| **Tracked Hours** (`tracked_seconds`) | active + idle + unknown + locked: the time covered by monitoring data. A database CHECK enforces the sum. |
| `active_in_shift_seconds` | ACTIVE time inside [shift start, shift end) |
| `pre_shift_active_seconds` / `post_shift_active_seconds` | ACTIVE time in the window before / after the shift |
| **First / last activity** | first and last ACTIVE instant in the window. With no ACTIVE time they are NULL; `first_tracked_at` / `last_tracked_at` give the first and last instant with any data. |
| **Late** (`late_seconds`) | Only on a working day, and looking only at the shift neighbourhood [start − 4 h, end + 4 h).<br>• Let *f* be the first ACTIVE instant there. Late counts only if *f* − start > `late_grace` (0 by default).<br>• Then late = *f* − max(start, end of any UNKNOWN time between start and *f*). Uncertain monitoring goes to the employee's benefit, with flag `START_UNCERTAIN`.<br>• Never negative. |
| **Early Leave** (`early_leave_seconds`) | Only after the shift has ended, and only if *l* (the last ACTIVE instant in the neighbourhood) is after the shift start.<br>• Early leave = the **reliably observed** part of [*l*, end): (end − *l*) − uncertain time in [*l*, end). It counts only if it is > `early_leave_grace` (0 by default).<br>• **Uncertain time** is: UNKNOWN periods; after an INTERRUPTED session (crash, power loss), from its last heartbeat until that device's next session starts (or open-ended); after a relevant device's last contact with the server (`last_seen_at`), open-ended, because data for it may not be uploaded yet.<br>• Reliable IDLE/LOCKED time, and no-data time after a cleanly CLOSED session (PC shut down), still count. So a later crash or unsynced tail protects only the uncertain part; it doesn't erase earlier reliable inactivity. Any uncertain time sets `END_UNCERTAIN`.<br>• Examples (09:00–17:00): ACTIVE to 12:00, IDLE/LOCKED to 15:00, crash at 15:00 → 3 h. ACTIVE to 15:00 then crash → 0. ACTIVE to 12:00, IDLE to 14:00, UNKNOWN to 17:00 → 2 h. ACTIVE to 12:00, crash, agent back at 12:05 and IDLE to 17:00 → 4 h 55 min. |
| **Overtime** (`overtime_seconds`) | **ACTIVE time only.**<br>• On a working day: active before the shift (configurable, on by default) + active after the shift. Each side counts only if it is ≥ `overtime_min` (0 by default).<br>• On a day off: all ACTIVE time.<br>• With no schedule: 0, because overtime can't be defined.<br>• Idle, locked and unknown time outside the shift is never overtime. |
| **Detected Break / Idle** (`detected_break_seconds`) | Inside the shift: unbroken runs of IDLE or LOCKED time (no ACTIVE, UNKNOWN or untracked time in between) lasting at least 15 min (`break_min`). It is **not** an official break: label it "Detected Break / Idle". It is not subtracted from anything. |
| `scheduled_seconds` | §4.11.2. 0 on days off and days without a schedule. |
| `measurable_scheduled_seconds` | scheduled − UNKNOWN time in the shift. The time that could actually be observed. |

All seconds are stored as whole numbers. Tracked is the sum of the rounded
parts, so the figures always add up.

#### 4.11.5 Attendance status (one primary state per day)

| Situation | Status |
|---|---|
| No schedule rule applies | `NO_SCHEDULE` |
| Day off, no ACTIVE time | `DAY_OFF` |
| Day off with ACTIVE time | `WORKED_DAY_OFF` (all of it is overtime; never late or absent) |
| Working day, ACTIVE time in the shift | `PRESENT`, `LATE`, `EARLY_LEAVE` or `LATE_AND_EARLY` (from late/early > 0) |
| Working day, no ACTIVE in the shift, shift not over yet | `PENDING`, never "absent" while the shift is running |
| Working day, no ACTIVE in the shift, shift over, and **any** of: UNKNOWN covers ≥ 50% of the shift; the employee has no enabled device; a device hasn't contacted the server since the shift ended | `DATA_INCOMPLETE` |
| Otherwise | `ABSENT` |

- **Days without in-shift ACTIVE time carry no lateness or early leave,**
  because absence is not lateness. A database CHECK enforces this. Activity
  outside the shift on such a day still appears as overtime.
- **Overtime is a metric,** not a status, so it combines with any status.
- **ABSENT needs reliable data.** For example, an idle PC with a little
  UNKNOWN time is still ABSENT, because no input was seen all day.
- **The 50% threshold** is `incomplete_unknown_ratio` and is configurable.

#### 4.11.6 Data quality (`data_quality` + `quality_flags`)

Each day gets flags, which then set its quality level.

| Flag | Meaning |
|---|---|
| `PROVISIONAL` | the day isn't over, or a session or period is still open. The summary will change. |
| `UNKNOWN_TIME` | some time was UNKNOWN |
| `START_UNCERTAIN` / `END_UNCERTAIN` | part of the start or end couldn't be judged; that part wasn't charged as lateness / early leave |
| `AWAITING_DEVICE_SYNC` | a device hasn't contacted the server since the shift or day ended; data may be in its backlog |
| `NO_DEVICE` | the employee has no enabled device |
| `NO_SCHEDULE` | no schedule rule applies to the date |
| `INTERRUPTED_SESSION` | the agent stopped without a clean end |
| `OVERLAPPING_PERIODS` | input periods overlapped; the overlap was removed, not added |
| `SCHEDULE_OVERLAP` | this shift overlaps another date's shift |
| `DST_ADJUSTED` | informational only; doesn't lower quality |

- **`INSUFFICIENT`:** the status is `DATA_INCOMPLETE`, or UNKNOWN covers at
  least 50% of the shift.
- **`PARTIAL`:** any non-informational flag.
- **`COMPLETE`:** no flags.

Missing telemetry is never turned into employee fault. It shows up as a flag
or as `DATA_INCOMPLETE`.

**Provisional summaries.** Today can be calculated at any time while
sessions are open, and is marked provisional. Recalculate recent days
regularly, e.g. the last 7 days each night (§4.11.10), so provisional
figures and late syncs settle.

#### 4.11.7 Attendance %

Per day:
- **Basis** (`attendance_basis_seconds`) = `measurable_scheduled_seconds` on
  working days whose status is not `PENDING` or `DATA_INCOMPLETE`;
  otherwise 0.
- **Credit** (`attendance_credit_seconds`) = min(`active_in_shift_seconds`,
  basis).
- **Attendance %** = credit ÷ basis × 100, rounded to 2 decimals. It is
  NULL when the basis is 0.

For a week or month: **Attendance %** = Σ credit ÷ Σ basis × 100.

So it means "the share of scheduled, observable working time with active
computer use". It is capped at 100% per day, so overtime on one day can't
make up for an absence on another. Absent days count fully against it. Time
the system couldn't observe (UNKNOWN, or a `DATA_INCOMPLETE` day) counts on
neither side.

#### 4.11.8 Weekly summary (`weekly_summaries`)

- **Week:** ISO, Monday → Sunday local dates (`week_start` must be a Monday,
  enforced by a CHECK).
- **Source:** built only from the stored daily rows. Every seconds figure is
  the plain sum of the days.

| Field | Rule |
|---|---|
| `working_days` | days with a working-day schedule |
| `days_worked` | days with any ACTIVE time |
| `absent_days`, `late_days`, `early_leave_days`, `incomplete_days`, `worked_day_off_days`, `pending_days` | counts of those daily statuses. LATE_AND_EARLY counts in both late and early. |
| `average_active_seconds_per_worked_day` | active ÷ `days_worked`; NULL if no day was worked |
| attendance % | §4.11.7 |
| `data_quality` | COMPLETE if every day is COMPLETE; INSUFFICIENT if more than half of the working days are INSUFFICIENT; otherwise PARTIAL |
| `is_provisional` | any day is provisional, or the week isn't finished yet |

#### 4.11.9 Monthly summary (`monthly_summaries`)

The same fields and rules as the weekly summary, over calendar-month local
dates (`month` is the first day). `working_days` is the number of days
scheduled.

#### 4.11.10 Recalculation and idempotency

- **Keys:** summaries are upserted on (employee_id, local_date), (employee_id,
  week_start) and (employee_id, month). Each row stores a `summary_hash`.
  When a recalculation gives the same result, nothing is written and
  `updated_at` doesn't change.
- **New data:** a higher synced version of a period, or a new period, gives
  a different result on the next calculation, and the row is updated.
- **Rows also store** `calculation_version` and the `policy` used (as
  JSON), so rows made by older formulas can be found and recalculated.
  Version 2 (the current one) changed early leave to exclude only the
  uncertain part of the shift's end; recalculate rows with version 1.
- **`recalculate`** covers whole weeks and months, so roll-ups are always
  built from fresh days. It never creates rows after the employee's own
  local today; a week or month in progress shows its figures so far and is
  marked provisional.
- **`--recent-days N`** (N ≥ 1) means the last N local dates **including
  today**, evaluated in **each employee's own timezone**
  (`SummaryService.recalculate_recent`). On October 8 local time, 7 means
  October 2–8. Just after midnight in Dhaka, UTC (and New York) may still be
  on October 7, so a New York employee's range then ends on October 7. The
  server's date is never used.

#### 4.11.11 Commands

All need `ZAZA_SERVER_BACKEND=postgres`:
- `add-schedule --employee-id E (--weekdays 1-5 | --date D) --start 09:00 --end 17:00 --expected-hours 8 [--day-off] [--effective-from/--effective-to] [--timezone Z]`.
  The timezone is the employee's; `--timezone` is optional and rejected if
  it differs. `--effective-from` defaults to today in the employee's
  timezone. It is audited as `work_schedule.create`.
- `list-schedules --employee-id E`
- `summarize-day --date D [--employee-id E]`
- `summarize-week --date D`
- `summarize-month --month 2026-10`
- `recalculate --from D --to D | --recent-days 7`

Without `--employee-id`, every active employee is processed. There is no
production scheduler yet. A later phase runs `recalculate --recent-days 7`
periodically.

#### 4.11.12 Settings (environment)

| Variable | Default | Meaning |
|---|---|---|
| `ZAZA_ATTENDANCE_LATE_GRACE_SECONDS` | 0 | lateness up to this is ignored |
| `ZAZA_ATTENDANCE_EARLY_LEAVE_GRACE_SECONDS` | 0 | early leave up to this is ignored |
| `ZAZA_ATTENDANCE_OVERTIME_MIN_SECONDS` | 0 | minimum before or after the shift to count as overtime |
| `ZAZA_ATTENDANCE_COUNT_PRE_SHIFT_OVERTIME` | true | ACTIVE time before the shift counts as overtime (`true/false`, `1/0`, `yes/no`, `on/off`) |
| `ZAZA_ATTENDANCE_ATTRIBUTION_MARGIN_SECONDS` | 14400 | activity this close to a shift belongs to that shift's date (§4.11.3); 0–43200 |
| `ZAZA_ATTENDANCE_BREAK_MIN_SECONDS` | 900 | minimum IDLE/LOCKED run for "Detected Break / Idle" |
| `ZAZA_ATTENDANCE_INCOMPLETE_UNKNOWN_RATIO` | 0.5 | UNKNOWN share of a shift at which "absent" can't be concluded (`DATA_INCOMPLETE`); > 0 and ≤ 1 |

Every `AttendancePolicy` field has a variable. Unset or empty means the
default. Invalid values (not a number, out of range, an unknown boolean)
stop the command with an error naming the variable. The policy is global,
and every summary row records the policy it was calculated with; after
changing a setting, recalculate the affected dates.

## 5. Component 4 — Google Sheets reporting layer (Phase 6)

**PostgreSQL is authoritative; the spreadsheet is a read-only view of it.**
The export is one-way (PostgreSQL → Sheets) and server-initiated. Nothing
is ever read back from the spreadsheet: manual edits are overwritten at the
next refresh and never reach the database. Sheets never recalculates
attendance: every figure is a stored Phase 5 summary value (§4.11).

**Code:** `deskmate/zaza_server/sheets/`:
- `config.py`: environment settings, key-file validation, secret scrubbing.
- `models.py`: the five managed tabs and their columns; the display window.
- `queries.py`: one read-only PostgreSQL snapshot.
- `formatter.py`: rows → Sheet values and formatting (pure, deterministic).
- `client.py`: the `SheetsClient` interface; `GoogleSheetsClient` (the
  official `google-api-python-client` + `google-auth`, optional extra
  `zaza-sheets`); `FakeSheetsClient` (in memory, used by the tests).
- `exporter.py`: `sheets-init`, `sheets-sync`, `sheets-status`.

The sync API server never imports this package (a test enforces it), so
Google being down cannot affect activity sync or attendance calculations.

### 5.1 Authentication

- **A Google service account** (a robot Google identity) authenticates with
  its JSON key file, named by `ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE`. The scope is
  `https://www.googleapis.com/auth/spreadsheets` only; no Drive scope.
- **The spreadsheet is not created automatically.** The manager creates it,
  then shares it with the service account's email (shown by `sheets-status`,
  e.g. `zaza-sheets@<project>.iam.gserviceaccount.com`) as **Editor**. The
  service account can then reach only spreadsheets shared with it.
  `ZAZA_GOOGLE_SHEET_ID` is the part of the spreadsheet URL between `/d/` and
  `/edit`.
- Key handling is in SECURITY.md §6.5.

### 5.2 The five managed tabs

`sheets-init` and `sheets-sync` create any missing tab and reuse existing
ones. Other tabs in the spreadsheet are never touched. Within the five
managed tabs, the whole managed area is rewritten, so managers should keep
their own notes on their own tabs. Every data tab has a bold, shaded,
frozen header row, set column widths and explicit number formats.

**Activity Log**: one row per synced `activity_periods` row. The central
database has no raw activity events, so none are exported and no
LOGIN/LOGOUT events are invented. Newest first; ties by employee, then by
period.

| Column | Value |
|---|---|
| Employee, Employee ID | display name and ID |
| Timestamp, Date, Time | period start, in the employee's time zone |
| Period End | period end, in the employee's time zone |
| Time Zone | the employee's reporting time zone |
| Event / Period Type | Active / Idle / Unknown / Locked period |
| Application, Window / Activity, Domain | exactly as stored. Privacy-excluded periods stay redacted (`Excluded Application`, `Excluded / Private`, `Excluded / Private Site`). |
| Status | Active / Idle / Unknown / Locked |
| Active / Idle / Unknown / Locked Duration | the period's duration, in **its status's column only**; the other three are empty. Shown as `h:mm:ss`. |
| Notes | still open at the last sync; why UNKNOWN (monitoring unavailable, telemetry gap); agent stopped unexpectedly; privacy-excluded |

**Daily Summary**: one row per `daily_summaries` row. Newest date first,
then employee name.
- **Columns:** Employee, Employee ID, Date, Scheduled Start, Scheduled End,
  Scheduled Hours, Tracked Hours, Active Hours, Idle Hours, Unknown Hours,
  Locked Hours, Detected Break / Idle, First Activity, Last Activity, Late
  Minutes, Early Leave Minutes, Overtime, Attendance Status, Attendance %,
  Data Quality, Time Zone, Notes / Quality Flags.
- **Times** are in the employee's time zone. An overnight shift shows its
  real next-day end, e.g. `2026-10-05 20:00` → `2026-10-06 04:00`.
- **Statuses** are shown as plain words. `DATA_INCOMPLETE` appears as "Data
  incomplete", with the note "Not enough reliable data to judge attendance —
  not counted as absent".
- **Quality flags** are explained in words.
- **Wording:** the labels are "Active Hours" (never "Actual Work Hours") and
  "Detected Break / Idle" (never "Break Taken").

**Weekly Summary** (`weekly_summaries`) and **Monthly Summary**
(`monthly_summaries`): newest period first, then employee name.
- **Columns:** Employee, Employee ID, Week Start/End or Month, Working Days
  (Days Scheduled), Days Worked, Scheduled / Tracked / Active / Idle /
  Unknown / Locked Hours, Overtime, Average Active Hours / Worked Day,
  Attendance %, Absent / Late / Early Leave / Incomplete Days, Data Quality,
  Notes (provisional, pending days, worked days off).

**Dashboard**: no charts (charts are Phase 8). It shows:
- **Refresh status:** Last successful refresh, Last refresh status.
- **Context:** Report time zone, Reporting date, Active employees, when the
  summaries were last calculated, and the display windows.
- **Today / This Week / This Month:**
  - Scheduled, Tracked, Active, Idle, Unknown and Locked Hours, and Overtime;
  - Attendance %;
  - counts of late, absent and data-incomplete employees, and of employees
    without a calculated summary;
  - the names behind each count.
- **A "Today by employee" table:** status, Active Hours, Attendance % and
  Data Quality.

Dashboard values are aggregated in Python from the stored summaries of
**active** employees. Hours are plain sums of stored seconds. Team
Attendance % is Σ credit ÷ Σ basis, the §4.11.7 definition. Lateness,
absence and the other attendance figures are never recalculated.
- **Today:** each employee's daily row for **their own** local today.
- **This Week / This Month:** their weekly / monthly row for the week /
  month containing their local today.
- **For a week or month:** Late, Absent and Data-incomplete count employees
  with at least one such day.
- **Period labels are truthful.** If every active employee is in the same
  local date / week / month, the label names it ("2026-10-08",
  "2026-10-05 – 2026-10-11", "October 2026"). If not, it says so: for
  example, when Dhaka is already on October 8 and New York is still on
  October 7, Today is labelled "Per employee local date (2026-10-07,
  2026-10-08)". The same applies to "Per employee local week" and "Per
  employee local month".

### 5.3 Display formats

- **Write mode:** values are written with `valueInputOption=RAW`, so text is
  never evaluated as a formula. A window title like `=IMPORTXML(...)` stays
  text.
- **Dates and times** are written as spreadsheet serial numbers that are
  **already converted** to the right time zone, with explicit formats:
  `yyyy-mm-dd`, `yyyy-mm-dd hh:mm`, `hh:mm:ss`, `yyyy-mm`. The spreadsheet's
  own locale and time-zone settings therefore change nothing.
- **Durations** are fractions of a day shown as `[h]:mm`. Activity periods
  use `[h]:mm:ss`. They stay numeric, so they can be summed.
- **Late / Early Leave Minutes:** decimal minutes, e.g. `30.0`.
- **Attendance %:** a fraction shown as `0.00%`. It is empty when there is
  no basis (day off, data incomplete, pending).

### 5.4 Time zones

- **Database timestamps** are UTC.
- **Employee rows:** every timestamp is converted to that employee's
  reporting time zone, `employees.timezone`, which is also the schedule's
  zone (§4.11.2). The zone is shown in the row.
- **Team-level Dashboard times** (refresh time, reporting date) use
  `ZAZA_SHEETS_TIMEZONE` if set. Otherwise they use the active employees'
  common zone, or UTC if those differ. The zone is printed next to every
  such time.

### 5.5 Refresh: full, deterministic, safe

The company is small (~5 employees), so every refresh rewrites the managed
tabs completely. There is no incremental sync to get wrong.

1. **Read PostgreSQL first, on one connection.** Take the session advisory
   lock, then read everything in one `REPEATABLE READ, READ ONLY`
   transaction **on that same connection**. The transaction ends before any
   Google call; the lock is kept until the refresh finishes. A refresh
   therefore needs only one pooled connection and works with
   `ZAZA_DB_POOL_MAX=1`. Every value is prepared in memory, and a database
   problem stops the refresh before Google is touched.
2. **Prepare the spreadsheet.** Create missing tabs, grow grids if needed,
   and apply formatting. Nothing is cleared.
3. **Mark the refresh as running.** Dashboard "Last refresh status" becomes
   "IN PROGRESS since …". "Last successful refresh" is not changed.
4. **Rewrite each data tab.** The new rows are written over the managed
   range from row 1, in chunks of 5,000 rows. **Only then** are stale rows
   below and stale columns to the right cleared. A tab is never cleared
   before its replacement is written, so it is never left empty.
5. **Write the Dashboard last.** It includes "Last successful refresh" and
   status "OK", so that cell advances only when every tab was written.

Guarantees:
- **One row each:** one database row gives exactly one Sheet row.
- **Repeatable:** the sort order is deterministic, so repeating a refresh
  gives identical tabs.
- **No leftovers:** deleted or out-of-window rows disappear at the next
  refresh.
- **One refresh at a time:** a PostgreSQL session advisory lock, held from
  the database read until the last Google write, prevents two refreshes
  from interleaving. A second refresh is refused (exit code 3). The lock is
  released even after a failure; if unlocking itself fails, the connection
  is closed so the server drops the lock. Taking the lock changes no
  data.

**Failure:** if Google is unreachable, slow, out of quota or refuses access:
- the command stops with a short, scrubbed error and exit code 3;
- PostgreSQL is untouched (the export only reads), and so are activity sync
  and attendance calculations;
- a tab may then hold a mix of new and old rows, but the Dashboard still
  shows the old "Last successful refresh" and "IN PROGRESS";
- the next run redoes the whole refresh from the database, so retrying is
  always safe.

The Google client retries rate-limit and server errors (429/5xx) three
times with exponential backoff. Each request times out after
`ZAZA_GOOGLE_API_TIMEOUT_SECONDS` (30 s by default).

### 5.6 Display windows (display only)

These settings only limit what the Sheet shows. Nothing is deleted from
PostgreSQL.
- **Activity Log:** `ZAZA_SHEETS_ACTIVITY_DAYS` (default 30). Each
  employee's last N local dates, today included, the same rule as
  `recalculate --recent-days`. Periods are selected by **interval overlap**
  with the window (`ended_at > cutoff OR started_at >= cutoff`, using each
  employee's own cutoff). A period that starts just before the first shown
  date and runs into it is shown once, whole, with its true start time; it
  is never split or duplicated.
- **Summary tabs:** `ZAZA_SHEETS_SUMMARY_MONTHS` (default 12). The current
  month and the N−1 before it; weeks that overlap that range are included;
  0 shows all history.

### 5.7 Commands

All take their settings from the environment (`.env.example`):
- **`sheets-init`:** checks access, creates missing tabs, and installs
  headers and formatting. It needs no database. It never overwrites an
  existing "Last successful refresh".
- **`sheets-sync`:** a full refresh from PostgreSQL. Needs
  `ZAZA_SERVER_BACKEND=postgres`.
- **`sheets-status`:** checks the configuration and access, and prints the
  masked spreadsheet ID, the service-account email, which tabs are present
  or missing, and the last refresh and its status.

**Exit codes:** 0 OK, 2 configuration, 3 Google unavailable or a refresh
already running.

**Scheduling:** there is no scheduler yet. Phase 10 runs `recalculate
--recent-days 7` and then `sheets-sync` periodically. `sheets-sync` exports
whatever summaries are stored, and the Dashboard shows when they were last
calculated.

## 6. Component 5 — Manager Web Dashboard (Phase 7)

A server-rendered web dashboard inside the existing FastAPI server, under
`/manager`. It is built with Jinja2 templates, local CSS and one small
vanilla JavaScript file: no frontend framework, build step, CDN or external
font. It reads **PostgreSQL directly** — never Google Sheets — and works
whether or not Sheets is configured. Phase 7 is local/development only; it
is not deployed (Phase 10).

**Code:** `deskmate/zaza_server/dashboard/`:
- `config.py`, `security.py` (passwords, tokens, headers), `models.py`.
- `queries.py`: `PostgresDashboardRepository`, plus an in-memory version for
  tests.
- `auth.py`: accounts, login, sessions.
- `service.py`: period filters, KPIs, current status, schedules.
- `routes.py`: routes and rendering only; no SQL and no business logic.
- `templates/`, `static/`.

Each request goes route → `DashboardService` / `ManagerAuth` → repository →
PostgreSQL. Optional extra: `zaza-dashboard` (Jinja2, argon2-cffi).

**Localhost-only during Phase 7.** `serve` builds its app with
`build_server_app()`. The dashboard is mounted only when **both** of these
hold:
- the backend is PostgreSQL;
- the bind host is loopback: `127.0.0.1` (any 127.x), `localhost` or `::1`.

For `0.0.0.0`, `::`, LAN, public or other named hosts, the dashboard is not
mounted; `/manager` returns 404 and `serve` prints "Manager dashboard not
mounted: Phase 7 dashboard is localhost-only. HTTPS reverse-proxy deployment
is Phase 10." The sync API is unaffected and keeps its own host rules.
Phase 10 binds the application locally behind the HTTPS proxy and sets
`ZAZA_DASHBOARD_COOKIE_SECURE=true`.

### 6.1 Manager accounts, sessions and CSRF

**Accounts** (`manager_users`, migration `0004_manager_dashboard_auth`):
- **Usernames** are stored lower-case and unique case-insensitively
  (`Manager`, `manager` and `MANAGER` are one account). Display names keep
  their capitals.
- **Passwords** are Argon2id hashes (`argon2-cffi` defaults). A CHECK
  refuses anything that isn't an `$argon2id$` hash, so plaintext can't be
  stored. Policy: at least 12 characters, no leading or trailing spaces, must
  not contain the username, not too repetitive.
- **Roles:**
  - MANAGER can view everything and manage schedules.
  - ADMIN can do the same, and is reserved for account administration,
    which is CLI-only in Phase 7.
- **No default account.** Accounts come from `manager-create`, which asks
  for the password with `getpass`; a password is never a command-line
  argument.

**Login** (`GET/POST /manager/login`):
- The failure message is always "Invalid username or password." — for an
  unknown user, a wrong password or a disabled account. An unknown username
  still costs one Argon2 check, so timing doesn't reveal which accounts
  exist.
- The login form has its own double-submit token (cookie
  `zaza_manager_login` plus a hidden field).
- A successful login sets `last_login_at` and is audited.
- Passwords stored with older Argon2 settings are re-hashed on login.

**Sessions** (`manager_sessions`):
- The browser gets a 256-bit random token (`secrets.token_urlsafe(32)`) in
  the cookie `zaza_manager_session`. The cookie is HttpOnly,
  SameSite=Strict and Path=/manager, and is marked Secure when
  `ZAZA_DASHBOARD_COOKIE_SECURE=true`. Its Max-Age is
  `ZAZA_DASHBOARD_SESSION_HOURS`.
- The database stores only SHA-256(token). Identity and role are never in
  the cookie.
- **Expiry is absolute** (default 12 h) and there is no sliding renewal.
  `last_seen_at` is updated at most every 5 minutes.
- A session stops working immediately when it expires, at logout (which
  revokes it), on revocation, on a password reset (all of the account's
  sessions), or when the account is disabled.

**CSRF:**
- Every POST (logout and every schedule change) must carry the session's
  CSRF token: HMAC-SHA256(session token, `"zaza-csrf"`).
- The token is tied to the session, is useless without the cookie, is
  checked in constant time, and its SHA-256 is stored with the session.
- A request whose `Origin` header is not the dashboard's own origin is also
  refused.
- GET requests never change data. The only bookkeeping write on a GET is the
  throttled session `last_seen_at`.

**Headers** on every `/manager` response:
- `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`.
- `Content-Security-Policy: default-src 'none'; script-src 'self';
  style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self';
  frame-ancestors 'none'; base-uri 'none'`. The templates have no inline
  scripts or styles.

**XSS:** all database text is untrusted. Jinja2 runs with `autoescape=True`
for every template, the JavaScript writes only with `textContent`, and
nothing from the database is placed in a URL scheme or an HTML attribute
without escaping.

### 6.2 Pages

The navigation is Overview · Employees · Attendance · Applications ·
Schedules · Log out. There are no charts (Phase 8) and no productivity
score.

| Route | Content |
|---|---|
| `/manager` | **Filters** (period, employee); **"right now"** cards (Active Employees, Currently Active, Idle, Locked, Unknown, Online/no recent activity, Offline); **selected-period KPI cards** (Scheduled, Tracked, Active, Idle, Unknown and Locked Hours, Overtime, Attendance %, Late / Absent / Data-Incomplete Employees with names); **employee table** (Current Status, Last Seen, Today's Schedule, Today's Active/Idle/Unknown Hours, Attendance Status, Late, Overtime, Data Quality with explanations). The status counts refresh every 60 s from `/manager/api/current-status`, paused while the tab is hidden. |
| `/manager/employees` | each employee's status, time zone and last contact |
| `/manager/employees/{id}` | **Header:** name, ID, time zone, current status, last seen, today's schedule and attendance. **Sections:** selected-period summary (Scheduled … Data-Incomplete Days); daily attendance table; application usage table; recent activity periods (newest first, at most `ZAZA_DASHBOARD_ACTIVITY_ROWS`). |
| `/manager/attendance` | stored daily summaries for the filter. Every column sorts on the server through a whitelisted key; ties are broken deterministically. |
| `/manager/applications` | application usage table, grouped by application (All Employees) or by application and employee |
| `/manager/schedules` | weekly and date-specific rules per employee; add, edit and remove forms (§6.6) |
| `/manager/api/{current-status,overview,attendance,applications}` | the same figures as JSON. Same session required: 401 without one. |

### 6.3 Current status (live)

This is kept separate from the historical figures: it is never used for
attendance, and attendance is never rebuilt from it. For each employee:

1. **Only enabled devices count.** No enabled device → **No enabled
   device**.
2. **Online or offline:** a device is online if `devices.last_seen_at` is
   within `ZAZA_DASHBOARD_ONLINE_THRESHOLD_SECONDS` (default 300). Otherwise
   it is **Offline**.
3. **What an online device is doing:** the status of its latest
   `activity_periods` row (ACTIVE, IDLE, LOCKED or UNKNOWN, exactly as
   recorded) if that period ended within the threshold. Otherwise it is
   **Online, no recent activity**.
4. **Several devices:** the highest wins, ACTIVE > IDLE > LOCKED > UNKNOWN >
   ONLINE_NO_ACTIVITY > OFFLINE.

UNKNOWN is never shown as idle, and "online" is never called working. The
"Offline" card counts offline employees plus those without an enabled
device.

### 6.4 Period filters (employee-local dates)

- **Choices:** Today, Yesterday, This Week, Last Week, This Month, Last
  Month, and Custom (From/To). Employee: All Employees (active ones) or one
  employee.
- **Every period is resolved per employee** from that employee's own local
  today, in `employees.timezone`, which is also the schedule's zone. Weeks
  are ISO Monday–Sunday; months are calendar months.
- **Custom From/To** are local calendar dates for every employee, at most
  366 days. They are compared with `daily_summaries.local_date`, never with
  UTC timestamps.
- **Mixed local periods:** when employees' local periods differ (e.g. Dhaka
  is already on 8 October while New York is on 7 October), the page says
  "local periods differ between employees" and lists each employee's range.
- **Filters are resolved and summed on the server.** The page receives the
  finished values; no metric is computed in JavaScript.

### 6.5 KPI definitions (from stored Phase 5 summaries; never recalculated)

For the selected employees, sum `daily_summaries` rows whose `local_date` is
inside each employee's own range:
- **Scheduled / Tracked / Active / Idle / Unknown / Locked Hours, Overtime:**
  SUM of the matching `*_seconds` columns.
- **Attendance %** = SUM(`attendance_credit_seconds`) ÷
  SUM(`attendance_basis_seconds`) × 100; "–" when the basis is 0. It is
  **never** an average of daily percentages, and it matches §4.11.7 and the
  Sheets Dashboard.
- **Late / Absent / Data-Incomplete Employees:** employees with at least one
  LATE (or LATE_AND_EARLY) / ABSENT / DATA_INCOMPLETE day in the period.
  - They are counted by **employee ID**, never by display name, so two
    employees both called "John Smith" who were both late count as 2.
  - In the names list, a display name shared by several employees gets the
    ID appended, e.g. "John Smith (emp-01)".
  - The JSON gives both the IDs and the count.
- **Active Employees:** the number of selected employees whose account is
  active (`employees.is_active`). "All Employees" selects active employees.
  A former employee selected explicitly is still fully reportable (history
  stays available) but gives Active Employees = 0.
- **Employee page:** late and early-leave minutes, and counts of days
  worked, late, absent and data-incomplete.
- **Wording:** "Active Hours", never "Actual Work Hours".
- **Data quality** (COMPLETE / PARTIAL / INSUFFICIENT) is shown with
  plain-language explanations of its flags: UNKNOWN time, START/END
  uncertain, AWAITING_DEVICE_SYNC, DATA_INCOMPLETE "not counted as absent",
  and so on.
- **Overnight shift:** "today's schedule" is the shift that starts on the
  employee's local today, e.g. "20:00 – 04:00 (+1 day)". Its summary row is
  the shift-start date (§4.11.2).

**Application usage** (`application_usage_daily`) is supplementary activity
information, not attendance:
- Its `usage_date` is the device's calendar date, which for an overnight
  shift can differ from the attendance date.
- It is filtered by each employee's local date range and sorted by Active
  Hours.
- "Usage Days" counts distinct calendar dates.
- It is labelled as "not a productivity score".

**Recent activity** comes from `activity_periods` only: no raw events, no
payloads, no hashes, no record versions. Privacy-excluded periods show the
stored placeholders, and the Privacy column marks them.

### 6.6 Schedule management

This uses the **same write path** as the CLI, `PostgresAttendanceStore`.
There is no second validator: the database constraints validate every rule
(timezone = employee's, overnight shifts, start ≠ end, 0 < expected ≤ shift
span, day-off shape, one rule per weekday/start date and per date), and
violations come back as friendly messages. Every change writes its
`audit_logs` row in the same transaction: actor type MANAGER or ADMIN, the
username, the action, the schedule ID, and old and new values.

**History safety:** dashboard changes take effect on the employee's local
today or later, so already-calculated days never change silently.

| Rule | Allowed from the dashboard |
|---|---|
| Upcoming (weekly from ≥ today, or a date ≥ today) | edit in place; remove (DELETE, audited) |
| In effect (weekly, started before today) | **change from a date ≥ today**: the old rule ends the day before and a new rule starts (one transaction); **stop from a date ≥ today** by setting `effective_to` |
| Past (ended before today, or a past date) | read-only |

New weekly rules start today or later (the default is the employee's local
today). New date rules (override or day off) must be today or later. A date
rule overrides the weekly rule (§4.11.2). Correcting history stays an
administrator task: the CLI, followed by `recalculate`.

### 6.7 Charts (Phase 8)

**Phase 8 automatic analysis is deterministic rule-based analysis, not
artificial intelligence.** It makes no LLM or API calls, uses no
machine-learning model, and produces no productivity score.

- **How charts are drawn:** the server renders them as **SVG**
  (`dashboard/charts.py` + `templates/_charts.html`). There is no chart
  library, no JavaScript and no CDN, so the Phase 7 CSP is unchanged
  (`script-src 'self'`, no inline scripts or styles).
- **Safe text:** each chart is plain shapes with numeric coordinates; every
  label (employee and application names) is auto-escaped SVG text content.
- **Not by colour alone:** colours come from CSS classes; Idle, Unknown,
  Locked, Early Leave, Absent and Data-Incomplete also carry a pattern, and
  every bar's value is printed.
- **Accessible:** every chart has a title, units, `role="img"` with
  `<title>`/`<desc>`, a legend, hover titles on bars, and a "Show the
  numbers" table with the same values. Charts supplement the tables and
  never replace them.

| Chart | Source | Notes |
|---|---|---|
| Active Hours by Employee | `daily_summaries` (Σ `active_seconds` per employee) | Highest first. Duplicate names get their ID. Hidden for a single employee, since one bar says nothing. |
| Active, Idle, Unknown and Locked Hours | `daily_summaries` | Stacked bar per employee, or the period total for one employee. Unknown and Locked are never folded into Idle. |
| Daily Active Hours | `daily_summaries` by `local_date` | Team total per local date, or one employee. Dates after the employees' local today are not drawn, and dates without a summary are left empty. Ranges over 62 days are shown per ISO week, with totals unchanged. With mixed time zones it notes that each date is the employee's own reporting date. |
| Weekly Work Trend | `daily_summaries` bucketed into ISO weeks | Scheduled, Active, Idle and Unknown Hours. Covers at least the last 8 weeks up to the end of the selected period. Weeks are each employee's local Monday–Sunday; team weeks add up those local weeks (never a UTC week). |
| Application Usage | `application_usage_daily` | Top `ZAZA_DASHBOARD_TOP_APPLICATIONS` (10) by Active Hours, aggregated across employees. Supplementary data, not a productivity score, with no "productive/unproductive" categories. |
| Attendance | `daily_summaries.attendance_status` | Late, Early Leave, Absent and Data-Incomplete days per employee (or period totals for one). DATA_INCOMPLETE is shown separately and never as absence. |

**Where the charts appear:**
- **`/manager/analytics`** (new navigation item): all six charts, the
  automatic analysis, and a comparison table. Filters are the Phase 7 query
  parameters (`period`, `employee`, `from`, `to`), so links are
  bookmarkable.
- **Overview:** three compact charts (Active Hours by Employee, Status
  Hours, Daily Active Hours), up to 5 Period Insights, and "View full
  analysis/analytics" links that keep the filters.
- **Employee page:** Daily Active Hours, Status Hours, Application Usage and
  Attendance charts above the existing tables.
- **JSON:** `GET /manager/api/analytics` returns `{"version": 1, "period",
  "previous_period", "summaries_last_calculated", "charts", "comparison",
  "insights", "analysis"}`. It needs the same manager session (401
  without). It contains display values only (hours, day counts, labels) —
  no internal column names, hashes, tokens or database objects.

All chart data for a page comes from **one** `daily_summaries` query
covering the period, the previous period and the weekly-trend weeks, plus
one aggregated application-usage query. Historical attendance is never
rebuilt from `activity_periods`. Current status stays separate (§6.3). The
pages say when the summaries were last calculated; the charts are not live.

### 6.8 Automatic analysis (Phase 8, rule-based, `dashboard/analysis.py`)

**Periods:** the Phase 7 `Selection`, i.e. each employee's own local range.

**Previous comparable period** (per employee, local dates):

| Selected | Compared with |
|---|---|
| Today, Yesterday, Custom | the same number of days immediately before (e.g. Oct 8–14 → Oct 1–7) |
| This Week, Last Week | the ISO week before |
| This Month, Last Month | the whole calendar month before (month lengths differ) |

- **What is compared:** Active, Idle, Scheduled and Overtime Active Hours,
  and Attendance % (in percentage points).
- **How changes are shown:** as the absolute difference plus the relative
  change, when the previous value is above 0.
  - Previous value 0: "Previous period had no recorded Active Hours; current
    period recorded …", never ∞%.
  - Both 0: nothing is said.
  - No previous summaries at all: "no comparison is made".
- **No causes:** comparisons never suggest a cause. A period still in
  progress is said to be in progress.

**Summary coverage.** For each employee and period, the **expected dates**
are every selected local date up to that employee's local today. Future
dates are never missing, and a finished historical period expects every
date. A **missing summary** is an expected date with no `daily_summaries`
row.

**Comparison data-quality rule** (applied to the selected AND the previous
period):
- **incomplete:** either period has a missing expected summary, or an
  employee-day with INSUFFICIENT data quality (which includes
  DATA_INCOMPLETE).
  - The Active Hours change insight becomes a DATA_QUALITY notice ("…is not
    shown because the data is not complete enough: the previous period is
    missing N employee-days of calculated summaries").
  - No percentage and no increase/decrease is stated.
  - The comparison table still lists the stored values, marks every row
    "not comparable (incomplete data)", and gives no relative change.
- **qualified:** full coverage, but START_UNCERTAIN, END_UNCERTAIN or
  AWAITING_DEVICE_SYNC days on either side. The change is shown with
  "Interpret this change cautiously: …".
- **complete:** the change is reported as before (increase, decrease,
  unchanged, previous value zero).

The JSON gives `comparison_quality` (status, reasons, and the per-period
counts) and a `comparison_status` on every comparison row.

**Comparable employee** (used for highest/lowest, the comparable average
and the high-idle observation) — all of:
- at least one calculated working day;
- attendance basis > 0;
- not every working day DATA_INCOMPLETE;
- **no missing expected daily summary in the period.**

Employees who are not comparable are named as "Not included in the
comparison". Highest/lowest needs at least 2 comparable employees.

**Wording keeps the two groups apart:**
- "Total recorded Active Hours across available summaries" covers every
  calculated summary.
- "N employee(s) have complete enough data for employee-to-employee
  comparison. Average Active Hours among those employees: …" covers only the
  comparable group.

Charts still show all recorded data; only the comparative conclusions are
restricted.

**Insight rules, in fixed order** (at most `ZAZA_ANALYSIS_MAX_INSIGHTS`,
default 8; the Overview shows 5):
1. **Data quality:**
   - INSUFFICIENT days: "Some insights are limited because monitoring data
     is incomplete for N employee-day(s)".
   - Otherwise, START/END_UNCERTAIN or AWAITING_DEVICE_SYNC days.
   - Missing summaries, up to each employee's local today: "Some employees
     do not yet have calculated summaries…".
2. **Absence:** stored ABSENT days only. DATA_INCOMPLETE, UNKNOWN or a
   missing summary is never absence.
3. **Active Hours:**
   - The team total and the average per eligible employee.
   - "Highest recorded Active Hours: … / Lowest recorded Active Hours among
     employees with reportable data: …", each followed by "Active Hours
     measure recorded computer activity and are not a productivity score."
   - "Active time as % of scheduled time" = Σ active ÷ Σ scheduled, when
     scheduled > 0. It is explicitly separate from Attendance % (credit ÷
     basis), and qualified when INSUFFICIENT days exist or UNKNOWN exceeds
     10% of tracked time.
4. **Comparison with the previous period:** Active Hours.
5. **Late starts and early leave:** stored LATE/LATE_AND_EARLY and
   EARLY_LEAVE/LATE_AND_EARLY statuses. Phase 5 already charges no early
   leave for uncertain ends.
6. **High idle share:** idle ÷ tracked ≥ `ZAZA_ANALYSIS_HIGH_IDLE_PERCENT`
   (40%), for eligible employees with tracked > 0. The insight adds: "This
   may include legitimate non-computer work or breaks, and idle time alone
   is not a basis for conclusions about an employee."
7. **Overtime:** stored overtime Active Hours, plus "…not automatically a
   payroll entitlement."
8. **Applications:** "X had the most recorded Active Hours among
   applications…", plus the application-usage disclaimer.

**How insights are represented:** each is an `Insight(kind, severity, title,
message, employee_id, metric, current_value, previous_value)`, with severity
INFO, NOTICE or DATA_QUALITY.

**Wording rules (tested):**
- Allowed: neutral activity wording only ("recorded Active Hours", "had
  late-start days").
- Never used: "productivity/performance/efficiency/employee score", "top/worst
  performer", "most/least productive", "best/worst employee", or "Actual Work
  Hours". The only exception is the disclaimer "not a productivity score".
- No metrics are combined into a ranking number.

## 7. Naming convention (applies to UI, Sheets, and docs)

Use **"Active Hours"** (or **"Estimated Work Hours"**) for the
activity-derived metric. Never call it "actual work time" — computer activity
is a proxy for work, not proof of productivity.

## 8. Explicit non-components

No screenshot pipeline, no image storage, no Google Drive integration, no
audio/video capture, no keylogger, no clipboard reader. These are not
"disabled" — they do not exist anywhere in this architecture.
