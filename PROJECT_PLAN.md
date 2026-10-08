# ZaZa Employee Management System — Project Plan

**Status:** Phases 0–2 approved. Phase 3 (sync API contract, development
server, agent sync client) implemented, awaiting review. Phase 4 not started.
**Last updated:** 2026-10-07

## 1. What this system is

An **activity-based remote employee work monitoring and reporting system** for a
5-person company (4 employees + 1 Project Manager). It tracks *activity metadata*
(application, window title, keyboard/mouse presence, idle/lock state, session
times) on company Windows machines, aggregates it centrally, and reports it to
managers through Google Sheets (read-only, refreshed) and a web dashboard (interactive).

It is **transparent workplace monitoring**, not stealth surveillance, and it does
**not** capture content — no screenshots, no typed text, no clipboard, no audio,
no video. See [SECURITY.md](SECURITY.md) for the full privacy boundary.

## 2. What changed from the earlier direction

An earlier exploratory direction considered screenshot capture and Google Drive
screenshot storage (inherited from DeskMate's general-purpose feature set). That
direction is **cancelled**. There is no screenshot pipeline, no screenshot
storage, and no Google Drive integration of any kind in this project. Google
Sheets is used only for summarized, text-based reporting — never for images.

## 3. Company context

- 5 employees total, including the Project Manager (who also acts as the
  manager/admin role for the dashboard).
- Employees work remotely on company-managed Windows machines.
- Central server will eventually run on an existing Windows Server 2022 VPS that
  already hosts other applications and already has PostgreSQL 16 installed.
  **No deployment happens in this phase**, and the new system must stay isolated
  from whatever else runs on that VPS (own port, own database, own service).

## 4. Phased roadmap

| Phase | Name | Output |
|---|---|---|
| 0 | Architecture & specification | This document + ARCHITECTURE.md, SECURITY.md, DEVELOPMENT_STATUS.md |
| 1 | Windows activity agent (local only) | DeskMate-derived agent that observes activity metadata, writes nothing beyond local disk |
| 2 | Local SQLite storage & aggregation | Reliable local event queue + local roll-up into activity periods, idle periods, work sessions, daily app usage; sync metadata; retention; crash recovery; privacy exclusions |
| 3 | Central synchronization API | Versioned batch API + per-device token auth + agent sync worker with offline retry/backoff, idempotent versioned upserts, per-record acknowledgement; development storage behind a repository interface |
| 4 | PostgreSQL central storage | Typed, constrained PostgreSQL schema (Alembic migrations) behind the Phase 3 repository interface; concurrency-safe idempotent upserts; device registry and append-only audit log; small connection pool. Built and tested locally; VPS database created only in Phase 10 |
| 5 | Attendance & work-time calculations | Deterministic daily/weekly/monthly summaries in PostgreSQL (authoritative for Sheets/dashboard), with DST-correct schedule resolution including overnight shifts, fair data-quality handling, and recalculation commands |
| 6 | Google Sheets reporting | One-way, read-only export from PostgreSQL into the 5 required tabs (Activity Log from activity periods, Daily/Weekly/Monthly summaries, Dashboard), via a service account; full deterministic refresh with safe failure behaviour |
| 7 | Manager web dashboard | KPIs, filters, employee detail views |
| 8 | Interactive charts & automatic analysis | Charting + deterministic insights (no AI yet) |
| 9 | Windows employee installer | Single-file install + autostart for the 5 machines |
| 10 | Production VPS deployment | Deploy to the existing VPS, isolated from other services |
| 11 | Pilot testing | Run with real employees, fix gaps |
| 12 | Optional AI analysis | Natural-language summaries layered on top of Phase 5/8 output |

**This task covers Phase 0 only.** Phase 1 does not begin until the user
explicitly approves this specification.

## 5. Version 1 scope (Phases 1–8)

V1 is considered "done" when:

- The agent runs on a Windows machine, tracks the metadata listed in
  ARCHITECTURE.md, and buffers locally when offline.
- The central API ingests agent events idempotently into PostgreSQL.
- Daily/weekly/monthly summaries are computed deterministically and stored.
- Those summaries (and a trimmed activity log) sync automatically to the 5
  required Google Sheets.
- A manager dashboard shows the required KPIs, filters, employee detail view,
  and the 6 required chart types, all driven by PostgreSQL data.
- Deterministic analysis (highest/lowest active hours, late starts, early
  finishes, idle outliers, overtime, day/week-over-day/week deltas, team
  active % vs scheduled) is computed without AI.

V1 explicitly excludes: AI-written summaries, the Windows installer, and
production deployment — those are Phases 9–12.

## 6. Explicitly out of scope (all phases, unless requirements change again)

- Screenshot capture or storage of any kind.
- Google Drive integration of any kind.
- Audio/microphone recording.
- Webcam/video recording.
- Keylogging (recording of actual keystrokes/typed text).
- Clipboard content collection.
- Any stealth or hidden-from-employee monitoring mode.

## 7. Terminology rule

Reports and UI must call the metric **"Active Hours"** (or, if a softer framing
is needed later, **"Estimated Work Hours"**) — never "actual work time." Computer
activity presence is a proxy for work, not proof of it.

## 8. Next step

Awaiting review/approval of Phase 6 (Google Sheets reporting — see
DEVELOPMENT_STATUS.md). Phase 7 (manager dashboard) does not begin until
Phase 6 is separately approved.
