-- ZaZa Employee Management: table privileges (Phase 10). REVIEW BEFORE RUNNING.
--
-- Run as zaza_employee_owner AFTER `migrate` (Initialize-ZaZaDatabase.ps1 -Step Grant).
-- Idempotent. A migration that adds a table must add it here explicitly:
-- nothing is granted on "all tables", so a new table is never readable or
-- writable by the API or the report role until it is listed below.
--
--   psql -h 127.0.0.1 -p 5432 -U zaza_employee_owner -d zaza_employee_management ^
--        -v ON_ERROR_STOP=1 -f 02-grant-privileges.sql
--
-- Same grants as SECURITY.md §6.2 / §6.5 (rehearsed in Phase 4-7), re-derived
-- from the SQL the server code runs (Phase 10B review):
--
-- zaza_employee_app (API, dashboard, attendance job)
--   SELECT, INSERT, UPDATE  sync upserts (SELECT ... FOR UPDATE needs UPDATE),
--                           device/token bookkeeping, schedules, summaries
--                           (upserted, never deleted), manager accounts and
--                           sessions (revoked/disabled, never deleted)
--   DELETE                  only work_schedules (upcoming rules removed in the
--                           dashboard; audited)
--   audit_logs              SELECT, INSERT (append-only; triggers also refuse
--                           UPDATE/DELETE/TRUNCATE)
--   alembic_version         SELECT (start-up schema check)
--   No TRUNCATE, REFERENCES, TRIGGER or DDL: the owner owns every object.
--
-- zaza_employee_report (Google Sheets export, READ ONLY transaction)
--   SELECT on exactly the six tables sheets-sync reads. Not: tokens, devices,
--   manager accounts/sessions, audit logs, schedules. sheets-sync makes NO
--   writes to PostgreSQL (its pg_try_advisory_lock needs no table privilege).

\set ON_ERROR_STOP on

BEGIN;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM zaza_employee_app, zaza_employee_report;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM zaza_employee_app, zaza_employee_report;

GRANT SELECT, INSERT, UPDATE ON
    employees, devices, device_tokens, work_schedules,
    work_sessions, activity_periods, idle_periods, application_usage_daily,
    daily_summaries, weekly_summaries, monthly_summaries,
    manager_users, manager_sessions
    TO zaza_employee_app;
GRANT DELETE ON work_schedules TO zaza_employee_app;
GRANT SELECT, INSERT ON audit_logs TO zaza_employee_app;
GRANT SELECT ON alembic_version TO zaza_employee_app;

GRANT SELECT ON
    alembic_version, employees, activity_periods,
    daily_summaries, weekly_summaries, monthly_summaries
    TO zaza_employee_report;

COMMIT;

\echo 'Privileges granted to zaza_employee_app and zaza_employee_report.'
