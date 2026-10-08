# ZaZa Employee Management System — Development Status

**Last updated:** 2026-10-08

## Current phase: Phase 5 — attendance & work-time calculations (implemented, pending your review)

Phase 4 was approved after the overnight-shift schedule fix. Phase 5 adds
deterministic attendance calculations. The results are stored in PostgreSQL
and are the authoritative source for Sheets, the dashboard and charts (Phases
6–8). **No Sheets, dashboard, charts, AI, installer or VPS work.** All
PostgreSQL testing used a throwaway local instance, which has since been
deleted.

### Phase 5 summary

- **Code:** `deskmate/zaza_server/attendance/`. The daily calculation is a
  pure function, so the same inputs always give the same output.
  - `calculator.py`: the daily calculation.
  - `schedule.py`: schedule resolution, DST-correct UTC shift times, and
    date attribution.
  - `timeline.py`: the non-overlapping timeline.
  - `rollup.py`: ISO week and month roll-ups.
  - `summary_service.py`: `calculate_daily`, `calculate_week`,
    `calculate_month` and `recalculate`.
  - `store.py` and `postgres_store.py`: storage.
- **Migration `0002_attendance_summaries`:** forward-only. It adds
  `daily_summaries`, `weekly_summaries` and `monthly_summaries`, with keys,
  foreign keys and named CHECKs (for example, tracked = active + idle +
  unknown + locked; no lateness on a non-late day; ISO Monday weeks). It
  also adds team-view indexes. `0001_initial` was not touched.
- **Fairness:**
  - UNKNOWN is never treated as idle, lateness or early leave.
  - Missing or unsynced data gives `DATA_INCOMPLETE`, not `ABSENT`.
  - An agent crash or unsynced device excludes only the uncertain part
    of the shift's end from early leave.
  - Every instant belongs to exactly one date, so nothing is counted twice.
- **Formulas and statuses:** ARCHITECTURE.md §4.11; the reporting
  interpretation is in SECURITY.md §6.4.
- **CLI:** `add-schedule`, `list-schedules`, `summarize-day`,
  `summarize-week`, `summarize-month` and `recalculate`. They need the
  PostgreSQL backend.
- **Tests:**
  - Without PostgreSQL: 453 pass in `tests/zaza_agent/` and 112 skip. The
    skips are the opt-in PostgreSQL tests.
  - With `ZAZA_TEST_POSTGRES_URL` set: 565 pass and 0 skip.
  - New: 84 engine tests (`test_attendance.py`) and 13 PostgreSQL tests
    (`test_attendance_postgres.py`). The PostgreSQL tests include exact
    equality between the PostgreSQL and in-memory results.

### Phase 5 review fixes (pending your approval)

1. **Schedule timezone = employee timezone.** `add_schedule` /
   `add-schedule` default to the employee's timezone and reject any other;
   migration `0003_schedule_timezone` adds a composite foreign key so direct
   SQL can't create a mismatch (and refuses to apply over existing
   mismatches). Per-schedule travel timezones are not supported.
2. **`--recent-days N`** = the last N local dates including today, per
   employee timezone (`recalculate_recent`); N ≥ 1. `add-schedule`'s default
   `--effective-from` is the employee's local today.
3. **Early leave** excludes only uncertain time (UNKNOWN, after a crash until
   the agent restarts, after a device's last contact). Earlier reliable
   inactivity still counts. `calculation_version` is now 2.
4. **Policy settings:** every `AttendancePolicy` field is configurable from
   the environment with validation (`.env.example`).

### Phase 5 known limitations / risks

- **No production scheduler.** Summaries are only as fresh as the last
  `recalculate`. Phase 10 should run `recalculate --recent-days 7`
  periodically.
- **"Device has synced since" uses `devices.last_seen_at`.** It is a single
  timestamp, so a backlog still uploading after a reconnect could briefly
  look complete.
- **Attribution margin (4 h).** Work more than 4 h after a shift and past
  midnight counts on the next calendar day. It is still counted exactly once,
  and it never hides lateness, because punctuality only looks at the shift
  neighbourhood.
- **Overlapping shifts** (allowed by the schedule constraints) are resolved
  by the earliest-starting shift and flagged `SCHEDULE_OVERLAP`. They are not
  blocked at entry.
- **Schedule rules can't be edited or removed yet.** Only `add-schedule`
  exists, so managing schedules properly is a Phase 7 dashboard feature. A
  newer weekly rule supersedes an older one from its `effective_from` date.
- **The policy is global.** It is not per employee, and changing it
  requires recalculating.
- **One timezone per employee.** Schedules can't use a different zone
  (travel, remote shifts in another region). Changing an employee's timezone
  is blocked while they have schedules; it needs a deliberate migration.
- **Crash point = last heartbeat.** After an INTERRUPTED session, uncertainty
  starts at its last heartbeat; reliable data synced just after it (within
  one heartbeat interval) is not charged as early leave. This errs in the
  employee's favour.
- **`application_usage_daily` is not part of the attendance figures,** by
  design, because of its device-local dates.

### Phase 4 (approved)

Approved after the overnight-shift fix to `work_schedules_hours_valid`.

Phase 4 added a production
PostgreSQL repository behind the unchanged Phase 3 repository interface. The
wire protocol and the agent are unchanged. **Nothing connected to the
production VPS**: all development and testing used a throwaway local
PostgreSQL 16 in a temporary folder, which has been deleted. No Sheets, no
dashboard, no attendance calculations.

### Phase 4 summary

- **Library:** psycopg 3 driver + `psycopg_pool`, using plain SQL with no
  ORM. Alembic handles migrations only. Everything is in the optional extra
  `zaza-postgres`.
- **Code:** `deskmate/zaza_server/postgres/`, with three parts:
  - `repository.py`: `PostgresRepository`.
  - `migrate.py`: upgrade, status, and offline SQL rendering.
  - `migrations/versions/0001_initial_schema.py`: the schema.

  Configuration is in `zaza_server/config.py`, and backend selection in
  `zaza_server/backends.py`.
- **Schema:**
  - Tables: employees, devices, device_tokens, work_schedules (prepared
    only), work_sessions, activity_periods, idle_periods,
    application_usage_daily, and audit_logs (append-only).
  - All columns are typed, and every instant is TIMESTAMPTZ. Each synced row
    also keeps its wire record in JSONB for audit only.
  - See ARCHITECTURE.md §4.2–4.4 and §4.9.
- **Integrity:** primary keys, foreign keys (periods → a session of the
  *same* device), unique keys, named CHECKs, and triggers for time zones,
  `updated_at` and audit append-only. Python validation is not the only
  guard.
- **Idempotency and concurrency:** the shared `decide()` runs on a row
  locked with `SELECT … FOR UPDATE`. A new record uses `INSERT … ON CONFLICT
  DO NOTHING` and then re-decides if it lost a race. See ARCHITECTURE.md
  §4.5.
- **Transactions:** one transaction per batch with a savepoint per record,
  so a constraint failure rejects only that record. A deadlock or
  serialization failure rolls back the whole batch and retries it, up to 3
  times, then answers 503. See ARCHITECTURE.md §4.6.
- **Migrations:** run only by an operator (`migrate`) and idempotent. The
  server refuses to start on an out-of-date schema and never migrates
  itself.
- **Device auth:** the registry is now in PostgreSQL. Token hashes are
  CHECK-constrained to SHA-256 hex, so a raw token can't be stored.
  `last_seen_at` (connectivity, not activity) and token `last_used_at` are
  updated after successful authentication.
- **Shared-interface changes:**
  - Employees are now part of the interface. A device needs an existing
    employee, so the CLI gains `add-employee` and `list-employees`.
  - Every backend now rejects an orphan period, or one that points to another
    device's session.
  - The API runs repository calls in the thread pool and answers 503 when
    storage is unavailable.
- **Tests:**
  - Without PostgreSQL: 369 pass in `tests/zaza_agent/` and 99 skip. The
    skips are the opt-in PostgreSQL tests.
  - With `ZAZA_TEST_POSTGRES_URL` set: 468 pass and 0 skip.
  - A real-process rehearsal (owner/app roles, live agent, outage and
    catch-up, leak checks) also passed.

### Phase 4 review fix (2026-10-08): overnight shifts

`work_schedules_hours_valid` now allows shifts that cross midnight
(20:00→04:00 = 8 h, ending the next local day).
- Equal start and end times, and `24:00`, are rejected: neither is read as
  a 24-hour shift.
- A working day must expect more than 0 seconds, and no more than the span.
- No Phase 4 database had been deployed or kept, so the initial migration
  `0001_initial` was corrected in place rather than adding a revision.
- The day rule for Phase 5 is in ARCHITECTURE.md §4.2.

### Phase 4 known limitations / risks

- **Not yet run on the real VPS.** The tests used PostgreSQL 16.2 locally,
  and the VPS runs 16.x. Creating the database and roles there is Phase 10
  (ARCHITECTURE.md §4.10, SECURITY.md §6.2).
- **The `payload` JSONB duplicates the typed columns,** window titles
  included. It is kept for audit and forward compatibility. It roughly
  doubles storage per row, which is small for 5 employees.
- **Composite session FK:** a period whose session never reaches the server
  is rejected and retried each cycle, which shows as BACKLOG. The agent's
  ordering makes this an edge case: a session that is itself rejected, or
  data lost on the server.
- **Durations use `double precision`.** That's exact enough for seconds, but
  Phase 5 sums should round for display.
- **No central retention or deletion yet** (Phase 5). The API role has no
  DELETE privilege.
- **Coupling to the agent's privacy placeholders:** the
  `privacy_excluded` CHECK mirrors the agent's placeholder strings, so
  changing them in the agent needs a migration.
- **Smart App Control blocks SQLAlchemy's compiled extensions on this PC.**
  `migrate.py` forces SQLAlchemy's pure-Python mode, so migrations work
  regardless.

### Phase 3 (approved)

Approved after the two client fixes: an invalid `stale` reply is never marked
synced, and any closed unconfirmed record reports BACKLOG.

### Phase 3 summary

- **Wire contract** (`deskmate/zaza/sync/protocol.py`, Pydantic):
  `GET /api/v1/sync/health`, `GET /api/v1/devices/me`, and
  `POST /api/v1/sync/batch` (1–500 records, 4 MiB). Requests are strict
  (unknown fields rejected, UTC-only timestamps). Each record is validated
  on its own, so one bad record doesn't fail the batch. See ARCHITECTURE.md
  §3.1.
- **Development server** (`deskmate/zaza_server/`): FastAPI app →
  `SyncService` → `CentralRepository`, with in-memory (tests) and SQLite
  development implementations. A CLI handles device registration, token
  rotation, disabling devices, and viewing records.
- **Idempotency:** keyed on `(record_type, record_id)` + `record_version` +
  content hash. Results are accepted / already_current / updated / stale
  (server keeps the newer) / conflict / rejected, for each record. See
  ARCHITECTURE.md §3.2–3.3.
- **Device auth:** an opaque per-device token stored only as a hash on the
  server, DPAPI-encrypted on the agent outside SQLite. Disable, revoke and
  rotate are supported. See SECURITY.md §6.1.
- **Sync worker** (`deskmate/zaza/sync/worker.py`): its own thread and DB
  connection. Batches go out in device order, and records are marked synced
  only for the version the server confirmed. Backoff is exponential with
  jitter (5s→300s), honours 429 Retry-After, and uses a long fixed retry for
  auth errors. A 413 halves the batch size. Sync health is HEALTHY /
  BACKLOG / OFFLINE / AUTH_ERROR / SERVER_ERROR / NOT_CONFIGURED. See
  ARCHITECTURE.md §3.4–3.5.
- **Agent CLI:** `set-credentials`, `sync-now`, `sync-status`, `report`. The
  normal run starts the sync worker automatically when it is configured.
- **Tests:** 329 in `tests/zaza_agent/`, all passing (137 new for Phase 3, including 11 review-fix regressions).
  A real end-to-end run (uvicorn server + live agent + DPAPI credentials
  + server down/up) also passed.

### Phase 3 known limitations / risks

- **No server-side rate limiting or TLS** in the development server. Both
  belong at the Phase 10 reverse proxy.
- **The employee can read their own token.** DPAPI protects the token from
  other accounts and machines, not from the employee's own account. The
  server limits the damage: a token can only write its own device/employee
  records.
- **Open records are re-sent each cycle** while open, because their version
  moves every tick. That's about 3 small records per minute: fine for 5
  devices, but worth revisiting at scale.
- **A permanently rejected record** stays FAILED and is retried every cycle,
  which keeps the state at BACKLOG. That's visible, but not auto-resolved.
- **`stale` after a local database restore:** the server's newer copy is
  kept, and the local version adopts the server's number.
- **App usage day bounds** come from the agent's current timezone when it
  is sent. If the PC's timezone changes, a later resend can produce a
  `conflict`, which resolves automatically by re-sending a newer version.
- **Concurrent sync processes** (e.g. `sync-now` while the agent runs) may
  send the same records twice. The server is idempotent, so the result is
  harmless.

### Phase 2 (approved)

Phase 1 was approved after Windows manual testing. Phase 2 is implemented
locally. Nothing in it touches the network, PostgreSQL, Google Sheets, a
dashboard, or AI.

### Phase 2 summary

- **Schema v2** (`deskmate/zaza/schema.py`): versioned forward-only
  migrations (a Phase 1 v1 database is upgraded in place), WAL mode. New
  tables: `work_sessions`, `activity_periods`, `idle_periods`,
  `app_usage_daily`, `sync_state`, `counters`. Raw `activity_events` gained
  `event_id`, `session_id`, `status`, `privacy_excluded`. See
  ARCHITECTURE.md §2.5.
- **Aggregation** (`aggregator.py`): raw ticks become contiguous periods
  keyed by status + app + domain. Window titles are normalized, and a title
  change splits a period only after 30s. Idle begins at last input + the idle
  threshold, since the threshold is a grace period. Idle periods are merged.
  See ARCHITECTURE.md §2.6.
- **ACTIVE / IDLE / UNKNOWN / LOCKED:** input is trusted only while both
  hooks are HEALTHY, and lock state only while the session-lock watcher is
  HEALTHY with a lock state established during that stint. DEGRADED/ERROR
  monitoring, an untrusted or stale lock state, a new or restarted session
  with no input yet, and telemetry gaps are all UNKNOWN, never idle. See
  ARCHITECTURE.md §2.3.
- **Work sessions & crash recovery:** a session per agent run, with
  heartbeats and totals. On restart, an OPEN session left by a dead run is
  marked INTERRUPTED at its last heartbeat. A backwards clock step closes the
  session (`CLOCK_CHANGED`). See ARCHITECTURE.md §2.7.
- **App usage** (`rollup.py`): per local day and app, recomputed from
  periods whenever one closes, split at midnight, with deterministic IDs.
- **Privacy exclusions** (`privacy.py`): excluded apps (the title is never
  read), hidden app names, and excluded domains. Domains are reduced to a
  bare hostname. See SECURITY.md §2.3.
- **Retention:** raw events are kept 7 days, including during a long OPEN
  session. Synced records are kept 30 days after being synced.
  PENDING/FAILED records are never deleted. See ARCHITECTURE.md §2.8.
- **Sync readiness (no network):** UUID record IDs, a device-wide
  `local_seq`, `record_version`, sync status/attempts/error fields, and
  `pending_sync` / `mark_synced` (version-checked) / `mark_sync_failed`.
- **Console:** `python -m deskmate.zaza --report` shows stored sessions,
  periods, idle periods, and app usage.
- **Tests:** 192 in `tests/zaza_agent/`, all passing.

### Phase 2 review fixes (2026-10-07)

1. **Idle threshold is a grace period.** IDLE now starts at
   `last input + threshold` (or `trusted since + threshold`), not at the last
   input. Before the first input, the pre-threshold time stays
   UNKNOWN/AWAITING_INPUT and is not converted to idle.
2. **Lock-watcher health gates IDLE.** Without a trusted lock state (watcher
   not HEALTHY, or a state from before an outage), the agent reports
   UNKNOWN/LOCK_STATE_UNAVAILABLE instead of IDLE, and never a stale LOCKED.
   The watcher now queries the current lock state when it registers, and the
   health registry has a per-component generation counter so stale state is
   detectable.
3. **Raw retention applies to OPEN sessions too.** The blanket OPEN-session
   exemption is removed. Nothing reads raw events to rebuild state.

Regression tests: `tests/zaza_agent/test_review_fixes.py`, plus
`test_retention_sync.py::test_long_running_open_session_old_raw_events_deleted_summaries_kept`
and `::test_crash_recovery_works_without_any_raw_events`.

### Phase 2 known limitations / risks

- **Single instance assumed.** If two agent processes share one database,
  the second treats the first's OPEN session as interrupted. The Phase 9
  installer should enforce one instance (e.g. a named mutex).
- **Gap detection** relies on ticks running. A hung (not dead) tick thread
  shows up as a gap only when it resumes, and if the process dies the time
  isn't recorded at all, as intended.
- **Low-level hook silent removal** (`LowLevelHooksTimeout`) is still
  undetectable. Such time would read as idle. Carried over from Phase 1.
- **Input as proof of presence while the lock watcher is down** assumes
  low-level hooks don't receive input typed on the secure lock-screen
  desktop. If that assumption were wrong, lock-screen input could count as
  up to one threshold of ACTIVE time. It would still never count as IDLE.
- **A missed LOCK while the watcher is down** after recent input: up to one
  threshold (5 min) is counted ACTIVE before the time turns
  UNKNOWN/LOCK_STATE_UNAVAILABLE.
- **Lock-state query** uses Windows 10/11 `SessionFlags` semantics. Windows
  7 / Server 2008 R2 report these inverted and are not supported targets.
- **Unsynced data grows** until Phase 3 exists (estimated at a few MB per
  year).
- `synchronous=NORMAL`: an OS crash or power loss can lose the last few
  seconds of committed ticks. An application crash cannot.
- Raw `activity_events` still holds per-tick window titles for 7 days.

### Phase 1 (approved)

Phase 0 (architecture & specification) is **approved**, subject to two
documentation corrections that have been applied:

1. **README.md** now represents the ZaZa Employee Management System first —
   what it is, what it never collects, PostgreSQL/Sheets/dashboard framing,
   and current under-development status — with DeskMate attribution/MIT
   license information kept intact. The original, unmodified DeskMate README
   is preserved at
   [docs/UPSTREAM_DESKMATE_README.md](docs/UPSTREAM_DESKMATE_README.md) for
   upstream reference.
2. **ARCHITECTURE.md** and **SECURITY.md** now explicitly state that browser
   tracking means only the *currently active domain*, where technically
   practical — never full URL paths, browser history, page contents, or
   search/query parameters.

Phase 1 then implemented the privacy-safe local Windows activity agent:

- **New, clearly separated module:** `deskmate/zaza/` — zero changes to any
  existing upstream `deskmate/` file. Upstream DeskMate source is left
  exactly as it was, so the two remain directly comparable.
- **What it tracks:** timestamp, active application/process, active window
  title, keyboard activity presence (boolean), mouse activity presence
  (boolean), idle/active status (default 5-minute threshold, configurable),
  and Windows lock/unlock events.
- **What it cannot capture:** typed characters/key names, clipboard
  contents, screenshots, OCR/accessibility screen text, audio, video — not
  hidden from a UI, not present in any code path or storage column. See
  `tests/zaza_agent/test_no_prohibited_capture.py` for the automated guard.
- **Storage:** a new, separate SQLite database (`~/.zaza_agent/activity.db`,
  overridable via `ZAZA_HOME`) with a single `activity_events` table and a
  fixed, narrow column set. No sync, no PostgreSQL, no network calls.
- **Manual run:** `python -m deskmate.zaza` — console output only, per the
  Phase 1 allowance.
- **Tests:** 82 tests in `tests/zaza_agent/` at Phase 1 approval. Existing
  DeskMate test suite re-run and unaffected (pre-existing, unrelated
  failures noted below, not caused by this work).

### Phase 1 review fixes (2026-10-07)

- **Win64 ctypes types:** `HOOKPROC` (`input_hooks.py`), `WNDPROC` and
  `DefWindowProcW.restype` (`session_lock.py`), and `CallNextHookEx.restype`
  now use `LRESULT = ctypes.c_ssize_t` (pointer-sized, signed) instead of
  `c_long`. `test_win32_signatures.py` guards against regressions.
- **Component health:** new `deskmate/zaza/health.py` — keyboard hook, mouse
  hook, session-lock watcher, foreground-window watcher, SQLite storage, each
  `HEALTHY` / `DEGRADED` / `ERROR`, plus an overall state. A failed hook is
  `ERROR`, not silent: ACTIVITY rows store NULL for the affected flag and for
  `idle`, and IDLE/CONTINUE are suppressed. Shown in the console run
  (startup table + a `[health]` line on each change). `test_health.py`
  simulates hook-install failure with fake `user32`/`kernel32`.
- **Privacy guards:** AST-based checks for keystroke/pointer content APIs in
  executable code (docstrings/comments ignored), a check that hook callbacks
  only forward `lparam` to `CallNextHookEx`, and self-tests proving the
  guards catch offending code. Existing screenshot/OCR/clipboard/audio/video
  guards are unchanged.
- **Known limitation:** Windows can silently remove a low-level hook whose
  callback exceeds `LowLevelHooksTimeout`; that can't be detected directly
  and is not reported as a health change.

## Phase checklist

- [x] Phase 0 — Architecture & specification documents (approved, with README/domain corrections applied)
- [x] Phase 1 — Privacy-safe local DeskMate-derived Windows activity agent (`deskmate/zaza/`)
- [x] Phase 2 — Local SQLite activity storage & aggregation (approved)
- [x] Phase 3 — Central synchronization API (approved)
- [x] Phase 4 — PostgreSQL central storage (approved; local testing only, no VPS)
- [x] Phase 5 — Attendance & work-time calculations (implemented, pending review)
- [ ] Phase 6 — Google Sheets live synchronization
- [ ] Phase 7 — Manager dashboard
- [ ] Phase 8 — Interactive charts & automatic analysis
- [ ] Phase 9 — Windows employee installer
- [ ] Phase 10 — Production VPS deployment
- [ ] Phase 11 — Pilot testing
- [ ] Phase 12 — Optional AI analysis

## Known pre-existing issues (not introduced by this work)

Running the full upstream test suite in this environment surfaces a few
pre-existing issues unrelated to the ZaZa module (confirmed via `git status`
— none of the affected files were touched in this or the prior pass):

- `tests/test_live_translation.py` fails to collect — `numpy` is not
  installed in this venv (an optional extra, not installed here).
- `tests/test_ai_prompt_journal.py` (2 tests), `tests/test_day_recap_context.py`
  (1 test) — timezone/locale-sensitive assertions that don't match this
  machine's local timezone.
- `tests/test_console.py::test_safe_stream_handler_falls_back_on_invalid_handle`
  and `tests/test_smoke.py::test_rapidocr_maps_result_to_normalized_words` —
  also pre-existing, the latter due to the same missing `numpy`.

None of these are in `deskmate/zaza/` or `tests/zaza_agent/`.

## Blocking item

**Awaiting your review of Phase 5** before Phase 6 begins. Google Sheets,
the dashboard, charts, AI, the installer and VPS deployment are explicitly
**not** started. The production PostgreSQL database has **not** been
created or touched.

## Explicitly cancelled from any earlier direction

- Screenshot recording/storage
- Google Drive screenshot storage (or any Google Drive integration)
- Audio recording, webcam/video recording, keylogging, clipboard collection

None of the above exist in the current specification or in the Phase 1
implementation, and none should be reintroduced without a new, explicit
requirements change.
