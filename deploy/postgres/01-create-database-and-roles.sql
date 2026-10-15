-- ZaZa Employee Management: database and roles (Phase 10). REVIEW BEFORE RUNNING.
--
-- Run ONCE, as the PostgreSQL superuser, with psql 18. Initialize-ZaZaDatabase.ps1
-- does this: psql prompts for the postgres password (never stored), and the
-- script feeds psql, on standard input, three \set lines with the roles'
-- SCRAM-SHA-256 verifiers (from secrets\db-<role>.scram, made by
-- zaza_db_credentials.py) followed by \i of this file:
--
--   \set owner_verifier  'SCRAM-SHA-256$4096:...'
--   \set app_verifier    'SCRAM-SHA-256$4096:...'
--   \set report_verifier 'SCRAM-SHA-256$4096:...'
--
-- It creates three NEW roles and one NEW database. It does not alter any
-- existing role, database, pg_hba.conf or postgresql.conf, and fails (changing
-- nothing further) if a role or the database already exists.
--
-- The plaintext passwords are never part of this SQL or of any command line,
-- and statement logging is switched off for this session only, so not even
-- the verifiers reach the server log.

\set ON_ERROR_STOP on
\if :{?owner_verifier}
\else
  DO $$ BEGIN RAISE EXCEPTION 'owner_verifier is not set: run this file through Initialize-ZaZaDatabase.ps1'; END $$;
\endif
\if :{?app_verifier}
\else
  DO $$ BEGIN RAISE EXCEPTION 'app_verifier is not set: run this file through Initialize-ZaZaDatabase.ps1'; END $$;
\endif
\if :{?report_verifier}
\else
  DO $$ BEGIN RAISE EXCEPTION 'report_verifier is not set: run this file through Initialize-ZaZaDatabase.ps1'; END $$;
\endif

SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = 'panic';

BEGIN;

-- 1. Owner: owns the database and every ZaZa object; used ONLY to run migrations.
CREATE ROLE zaza_employee_owner LOGIN
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT
    CONNECTION LIMIT 3
    PASSWORD :'owner_verifier';

-- 2. Application: the API, the dashboard and the attendance job. Table
--    privileges are granted by 02-grant-privileges.sql after migrations.
CREATE ROLE zaza_employee_app LOGIN
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT
    CONNECTION LIMIT 10
    PASSWORD :'app_verifier';

-- 3. Report: read-only, for the Google Sheets export.
CREATE ROLE zaza_employee_report LOGIN
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT
    CONNECTION LIMIT 3
    PASSWORD :'report_verifier';

COMMIT;

-- CREATE DATABASE cannot run inside a transaction block.
CREATE DATABASE zaza_employee_management
    OWNER zaza_employee_owner
    ENCODING 'UTF8'
    TEMPLATE template0;

REVOKE ALL ON DATABASE zaza_employee_management FROM PUBLIC;
GRANT CONNECT ON DATABASE zaza_employee_management TO zaza_employee_app, zaza_employee_report;

\connect zaza_employee_management
SET log_statement = 'none';

-- PostgreSQL 15+: schema public is owned by pg_database_owner, i.e. by
-- zaza_employee_owner here. Nobody else may create objects in it.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO zaza_employee_app, zaza_employee_report;

\echo 'Created roles zaza_employee_owner, zaza_employee_app, zaza_employee_report and database zaza_employee_management.'
