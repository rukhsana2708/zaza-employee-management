# ZaZa production recovery runbook (Cloud SQL backup and PITR)

Applies to the Cloud SQL instance `zaza-prod-pg16` (project
`zaza-employee-monitor`, region `asia-south1`, PostgreSQL 16). It holds
`zaza_control` (the control plane) and every tenant database `zaza_t_*`.
Instance settings: [CLOUD_SQL_PRODUCTION.md §11](CLOUD_SQL_PRODUCTION.md#11-backups-and-point-in-time-recovery).

**Key fact:** Cloud SQL backups and point-in-time recovery (PITR) are
**instance-level**. A restore produces a whole instance — the control plane
and all tenants, as of one moment. There is no Cloud SQL backup of a single
tenant database. Per-tenant recovery goes through a temporary instance and
PostgreSQL logical tools (section B).

## Rules for every recovery

- **Never restore onto `zaza-prod-pg16`.** A backup restore or PITR always
  targets a NEW instance. Overwriting the live instance destroys every
  change since the recovery point, for every tenant.
- Name temporary instances `zaza-restore-drill-<suffix>` (backup restore)
  or `zaza-pitr-drill-<suffix>` (PITR) for drills, and a clearly named
  `zaza-recovery-<suffix>` for a real recovery. A deleted instance name
  cannot be reused for about a week, so use a fresh suffix.
- Expect the new instance to carry source settings. In the 10G-D drill,
  both the backup-restore instance and the PITR clone came up with
  deletion protection ON and automated backups and PITR ON; the backup
  restore ignored `--no-deletion-protection`. Labels differed:
  - **backup restore:** the instance had **no user labels**;
  - **PITR clone:** it **copied the source's production labels**
    (`app=zaza`, `environment=production`, `managed-by=terraform`).

  This is what one drill observed, not a documented guarantee: inspect
  every new instance. Relabel a temporary instance right after creation
  with the beta command (or the Admin API), e.g.
  `gcloud beta sql instances patch INSTANCE --update-labels=purpose=dr-drill,environment=dr-drill,temporary=true`
  plus `--remove-labels=managed-by`, so it is never mistaken for production.
  (The GA `gcloud sql instances patch` rejected these label flags in the
  drill; labels can still be changed through beta or the API.) Turn
  deletion protection off on the temporary instance only, right before
  deleting it.
- Keep the source's exposure: public IP with **no authorized networks**,
  connector enforcement REQUIRED, `ENCRYPTED_ONLY`. Connect only through
  the Cloud SQL Auth Proxy. Never open PostgreSQL to the internet.
- Restored instances carry the source's database roles and password
  hashes, so the existing Secret Manager passwords work. Do not rotate or
  print them; pass them to tools over stdin or files that are deleted
  afterwards.
- Do not import temporary instances into Terraform; delete them when done
  and confirm `terraform plan` shows no changes.
- Changing what Cloud Run connects to (instance connection name, secrets,
  Terraform `cloudsql.tf`, DNS) is a **separate, explicitly approved
  disaster decision**, never part of verification.

## Inputs

| What | Where |
|---|---|
| Backups | `gcloud sql backups list --instance zaza-prod-pg16` (automated daily at 20:30 UTC, 14 retained; on-demand backups are kept until deleted, see "Backup location and retention") |
| Backup resource name (needed to restore to a NEW instance) | `gcloud sql backups list --filter=instance:zaza-prod-pg16` → `projects/zaza-employee-monitor/backups/<uuid>` |
| PITR window | `gcloud beta sql instances get-latest-recovery-time zaza-prod-pg16` → `earliestRecoveryTime`, `latestRecoveryTime` (7 days of logs) |
| Expected content | control revision, organizations and `tenant_databases` rows; each tenant's `alembic_version` |

## Backup location and retention

- **Location.** Automated backups are configured for `asia-south1`. An
  on-demand backup created without `--location` goes to Cloud SQL's
  default multi-region (the 10G-D drill backup is in `asia`). That is a
  valid Cloud SQL default and the backup is fully usable; the drill
  restored it successfully. For consistency with the configured production
  backup location, or any regional data-residency requirement, future
  on-demand backups **should** pass `--location asia-south1`:
  `gcloud sql backups create --instance zaza-prod-pg16 --location asia-south1 --description "…"`.
- **Retention.** Standard on-demand backups are **not** part of the rolling
  count of 14 automated backups: they stay, and keep costing storage, until
  someone deletes them. Every on-demand backup needs a deliberate review
  and keep-or-delete decision, recorded here.

| On-demand backup | Purpose | Location | Decision |
|---|---|---|---|
| `1793272723324` (2026-10-29) | 10G-D restore drill; the first verified restore-drill backup (contains the first tenant) | `asia` (multi-region) | **retained intentionally**; review when a later drill produces a newer verified backup, or before customer data exists |

## A. Complete instance loss (or whole-instance corruption)

1. **Choose the recovery point.**
   - Backup: the newest SUCCESSFUL backup before the incident.
   - PITR: a UTC RFC 3339 timestamp inside the PITR window and before the
     incident. PITR needs the source instance (or its retained logs) to
     still exist; if the instance was deleted, use a backup.
2. **Restore to a NEW instance.**
   - Backup:
     `gcloud sql backups restore projects/zaza-employee-monitor/backups/<uuid> --restore-instance=zaza-recovery-<suffix> --tier=db-custom-1-3840 --edition=ENTERPRISE --availability-type=zonal --storage-type=SSD --storage-size=20 --assign-ip --connector-enforcement=REQUIRED --ssl-mode=ENCRYPTED_ONLY --async`
   - PITR:
     `gcloud sql instances clone zaza-prod-pg16 zaza-recovery-<suffix> --point-in-time=<RFC3339 UTC> --async`
   - Wait until the instance is RUNNABLE **and** its RESTORE/CLONE
     operation is DONE (`gcloud sql operations list --instance …`).
3. **Inspect before connecting:** PostgreSQL 16, no authorized networks,
   connector enforcement REQUIRED, no Cloud Run or DNS pointing at it.
4. **Validate the control plane** (read-only session as `zaza_migrator`
   through the Auth Proxy, `default_transaction_read_only = on`):
   - database inventory: `postgres`, `zaza_control`, every `zaza_t_*`
     listed in `tenant_databases`, and no stray `zaza` database;
   - `zaza_control_version` at the expected control head;
   - organizations, memberships and users as expected for the recovery
     point; every `tenant_databases` row has a database of that name.
5. **Validate every tenant database:** `alembic_version` at the tenant
   head; the tenant tables only, with no control-plane tables; row counts
   plausible for the recovery point.
6. **Validate roles and privileges:** `zaza_migrator`, `zaza_app`,
   `zaza_control_app` exist, none is superuser; tenant objects are owned by
   `zaza_migrator`; the tenant database ACL is
   `{zaza_migrator=CTc/zaza_migrator,zaza_app=Tc/zaza_migrator}`; `zaza_app`
   has exactly SELECT/INSERT/UPDATE/DELETE on tables and USAGE+SELECT on
   sequences; default privileges are intact; `zaza_control_app` still has
   its control grants and no tenant access.
7. **Only after explicit disaster approval:** point production at the
   recovered instance (Terraform instance/connection name, Cloud Run job
   and service configuration, secrets if needed) as a reviewed change.
   Then re-enable deletion protection and confirm backups and PITR on the
   new instance.

## B. Single-tenant logical corruption

Do **not** overwrite the whole production instance to recover one tenant:
it would roll back the control plane and every other tenant.

Planned tenant-specific recovery mechanism:

1. Restore a backup, or PITR to a moment before the corruption, into a
   **temporary** instance (section A, steps 1–3).
2. On the temporary instance, confirm the affected tenant through its
   `tenant_databases` row (the database name always comes from the control
   directory, never from memory) and validate its schema revision.
3. **Extract only that database** with PostgreSQL logical tools, e.g.
   `pg_dump --format=custom --no-owner --no-acl` of `zaza_t_<…>` through the
   Auth Proxy, into a protected location (the dump contains employee
   monitoring data: treat it as confidential and delete it afterwards).
4. **Inspect and validate** the dump: restore it into a scratch database on
   the temporary instance, check `alembic_version` and the affected rows.
5. **Restore or reconcile only that tenant** in production under a
   separately approved procedure: for example mark the tenant non-READY,
   restore into a NEW server-named database, re-apply the runtime grants
   and default privileges, switch the `tenant_databases` row, then mark it
   READY; or reconcile selected rows. Never write into another tenant's
   database.
6. Delete the temporary instance and every dump.

This procedure has not been exercised against production; it is the
designed path. No tenant replacement was performed in Phase 10G-D.

## C. Cleanup after any drill or recovery

- Temporary instances deleted (`gcloud sql instances list` shows only the
  intended instances).
- No Auth Proxy container, Docker network, or password file left behind.
- Source backups, PITR and deletion protection unchanged.
- `terraform plan`: no changes.

## Drill history

### Phase 10G-D (2026-10-29): PASS

First verified drill, after the first internal tenant (Phase 10G-C).
Source `zaza-prod-pg16` was never restored onto, overwritten, restarted or
reconfigured; the only production write was the on-demand backup.

| Step | Result |
|---|---|
| On-demand backup | ID `1793272723324` (Backups API name `d31512a1-cd7e-41e5-96b3-7cad6587d870`), "Phase 10G-D restore drill after first tenant", SNAPSHOT, SUCCESSFUL, 2026-10-29 11:18:43 → 11:20:14 UTC. It is the **first verified restore-drill backup** and is kept. Stored in the `asia` multi-region (an on-demand backup without `--location` defaults to the multi-region; pass `--location asia-south1` next time). The newest automated backup at the time (2026-10-28 20:30 UTC) predates the tenant (provisioned 2026-10-29 09:47–09:53 UTC). |
| Backup restore | NEW instance `zaza-restore-drill-20261029a` (asia-south1, PostgreSQL 16): restore 11:35:56 → 11:42:09 UTC (about 6 min). Public IP, no authorized networks, connector enforcement REQUIRED, `ENCRYPTED_ONLY`; no Cloud Run, DNS or load balancer. **PASS**: databases `postgres`, `zaza_control`, `zaza_t_7jihzm6q6wvdogkv535q45mhdy`, no `zaza`; control `c0003_device_enrollment`; 1 organization `zaza-internal` ACTIVE; 1 tenant row READY at `0004_manager_dashboard_auth` pointing at the restored database; tenant at `0004`, 15 tenant tables, 0 business rows, no control tables; roles, ownership (`zaza_migrator`), ACL, `zaza_app` DML grants and default privileges intact. Deleted 11:46 UTC. |
| PITR window | earliest 2026-10-27T20:25:09.586Z, latest 2026-10-29T11:44:51.228Z (queried 11:46:22Z) |
| PITR | requested point **2026-10-29T10:30:00Z** (after the last 10G-C control write at 09:53:31.936Z, before the request at 11:46:31Z) → NEW instance `zaza-pitr-drill-20261029a`: clone 11:46:35 → 11:55:20 UTC (about 9 min). **PASS**: identical checks to the backup restore; newest control timestamp 09:53:31.936Z, consistent with the requested point. Cloud SQL does not report a finer achieved recovery time, and no write happened between 09:53 and 10:30, so the drill proves recovery to the minute range only. Deleted 11:59 UTC. |
| Cleanup | 0 temporary instances, no proxy/Docker resources or password files left; source backups, PITR and deletion protection unchanged; Terraform: no changes |

Observed while drilling:
- Both temporary instances came up with deletion protection ON and
  automated backups ON despite `--no-deletion-protection` on the restore.
  The backup-restore instance had no user labels; the PITR clone copied the
  production labels (`environment=production`, `managed-by=terraform`).
  The GA `gcloud sql instances patch` rejected the label flags, and the
  beta command was not tried, so both drill instances were identified by
  name only. For the next drill, relabel them with
  `gcloud beta sql instances patch … --update-labels …` (or the API) right
  after creation, and keep the drill short.
- Deleting the temporary instances left no backups behind.
