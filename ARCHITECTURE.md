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
                                  │ live reporting layer     │                             │ reads from PostgreSQL   │
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

Phase 3 implements the sync contract, a development server
(`deskmate/zaza_server/`), and the agent's sync client
(`deskmate/zaza/sync/`). PostgreSQL (Phase 4) and deployment (Phase 10) are
not part of it.

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
                         └─ PostgreSQL           (Phase 4, same interface)
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

### 3.7 Phase 4 repository boundary

`CentralRepository` (`zaza_server/repository.py`) is the only persistence
interface. It covers device and token registry calls (`add_device`,
`get_device`, `set_device_status`, `add_token`, `find_token`,
`revoke_tokens`) and record calls (`upsert_records` — batch-atomic,
returning one outcome per record; `get_record`, `list_records`,
`count_records`).

The version and idempotency decision is the shared pure function
`decide()`, so every backend behaves identically. Phase 4 adds a PostgreSQL
implementation, with typed tables instead of the dev store's JSON payloads,
and runs the same repository tests against it. The wire protocol and the
agent do not change.

### 3.8 Development server

`python -m deskmate.zaza_server serve` runs on `127.0.0.1:8765` with a SQLite
development database (`~/.zaza_server_dev/central_dev.db`). Admin commands:
`register-device`, `rotate-token`, `disable-device` / `enable-device`,
`list-devices`, and `records`. Plain HTTP is for localhost development
only. The agent refuses `http://` to any other host unless
`ZAZA_SYNC_ALLOW_INSECURE_HTTP=1` is set explicitly.

## 4. Component 3 — PostgreSQL central storage

Uses the VPS's existing PostgreSQL 16 instance. **No new PostgreSQL server is
created**; this system gets its own database/schema within it, and existing
VPS services/databases are left untouched.

### 4.1 Core data structures (eventual, built out across Phases 4–5)

- `employees` — identity, role, employment status
- `manager_users` — dashboard login accounts (PM / manager role)
- `devices` — registered machine per employee
- `work_schedules` — configured expected hours per employee/day
- `work_sessions` — login→logout spans
- `activity_periods` — summarized active/idle windows (not raw per-second ticks)
- `application_usage` — time-per-application rollups
- `idle_periods` — idle spans with start/end/duration
- `daily_summaries`, `weekly_summaries`, `monthly_summaries` — precomputed
  attendance/work-time rollups (see ARCHITECTURE §6)
- `sync_state` — per-device sync watermark / dedup bookkeeping
- `audit_logs` — who changed what (schedules, employee records, manager
  actions) — this is an access/change audit log, not an activity surveillance
  log

Raw per-tick agent events are retained only as long as needed to build
`activity_periods`/`application_usage`; the system is designed around
summarized periods, not an unbounded raw event firehose.

## 5. Component 4 — Google Sheets live reporting layer

- **Not** the database. A one-way, server-driven export of summarized data
  from PostgreSQL into a shared Google Sheets workbook, modeled on the
  existing "Working Time Tracker" workbook but extended to all 5 employees.
- Writes are **periodic/batched summaries**, never a row-per-tick firehose.
  Example: instead of three rows for 10:00:10 / 10:00:20 / 10:00:30 "VS Code,"
  write one row for 10:00–10:05, "Visual Studio Code, Active 4m48s / Idle 12s."

### 5.1 Required sheets

1. **Activity Log** — columns: Employee, Employee ID, Timestamp, Date, Time,
   Event, Application, Window or Activity, Status, Active Duration, Idle
   Duration, Notes. Events: `LOGIN`, `LOGOUT`, `ACTIVITY`, `APP_CHANGE`,
   `IDLE`, `CONTINUE`, `LOCK`, `UNLOCK`, `OFFLINE`, `ONLINE`.
2. **Daily Summary** — Employee, Date, Scheduled Hours, First Login, Last
   Logout, Tracked Hours, Active Hours, Idle Hours, Break Hours, Late Minutes,
   Early Leave Minutes, Overtime, Attendance Status.
3. **Weekly Summary** — Employee, Week Start, Week End, Working Days,
   Scheduled Hours, Tracked Hours, Active Hours, Idle Hours, Overtime, Average
   Active Hours Per Day, Attendance Percentage, Status.
4. **Monthly Summary** — Employee, Month, Days Scheduled, Days Worked,
   Scheduled Hours, Tracked Hours, Active Hours, Idle Hours, Overtime, Average
   Active Hours Per Day, Attendance Percentage.
5. **Dashboard** — summary tab mirroring the web dashboard's top-level KPIs,
   for quick viewing directly in Sheets.

## 6. Component 5 — Manager Web Dashboard

A separate manager-facing web app, reading only from PostgreSQL (not Sheets).

### 6.1 Date scope & filters

Ranges: Today, Yesterday, This Week, Last Week, This Month, Last Month, custom
range. Employee filter: All employees, or one individual employee.

### 6.2 KPIs

Employee count; currently working / idle / offline counts; scheduled hours;
tracked hours; active hours; idle hours; average active hours; attendance
percentage.

### 6.3 Employee detail view

Schedule; first/last activity; tracked/active/idle hours; late time; early
finish; overtime; application usage breakdown; daily timeline; calendar view;
daily report; weekly report.

### 6.4 Interactive charts (Phase 8)

1. Active Hours by Employee
2. Active vs Idle Hours by Employee
3. Daily Active Hours Trend
4. Weekly Work Trend
5. Application Usage
6. Attendance / Late Start analysis

All charts respond to the active employee + date filters, and all are built
from `daily_summaries`/`weekly_summaries`/`monthly_summaries` and
`application_usage` — not from raw events.

### 6.5 Deterministic analysis (Phase 8, no AI required for V1)

Highest/lowest active-hours employee, team average active hours, late
starts, early finishes, unusually high idle time, overtime, day-over-day and
week-over-week deltas, team active % vs. scheduled hours. AI-generated
written summaries are an optional later layer (Phase 12) on top of this same
deterministic data — never a V1 requirement.

## 7. Naming convention (applies to UI, Sheets, and docs)

Use **"Active Hours"** (or **"Estimated Work Hours"**) for the
activity-derived metric. Never call it "actual work time" — computer activity
is a proxy for work, not proof of productivity.

## 8. Explicit non-components

No screenshot pipeline, no image storage, no Google Drive integration, no
audio/video capture, no keylogger, no clipboard reader. These are not
"disabled" — they do not exist anywhere in this architecture.
