# ZaZa Google Cloud infrastructure (Terraform)

> **Core applied in Phase 10E-B (2026-10-28):** 18 resources, using remote
> state in `gs://zaza-employee-monitor-tfstate-e33ac2a6/zaza/production`.
> The Cloud Run service and job (`deploy_service = false`) are NOT created.
> Phase 10D wrote and validated this configuration; Phase 10E-A planned it.

Design and reasoning: [docs/zaza/CLOUD_SQL_PRODUCTION.md](../../../docs/zaza/CLOUD_SQL_PRODUCTION.md).

## What it defines

- **Project and region:** `zaza-employee-monitor`, `asia-south1` (both
  overridable); production labels on every resource.
- **APIs:** run, sqladmin, secretmanager, artifactregistry, iam.
- **Artifact Registry:** the Docker repository `zaza`, with immutable tags.
  Images are deployed by digest.
- **Service accounts:**
  - `zaza-run` (web service) and `zaza-migrate` (migration job), each with
    `roles/cloudsql.client` and access to its own secrets only;
  - **no keys are created**.
- **Cloud SQL for PostgreSQL 16:**
  - one instance, Enterprise edition, `db-custom-1-3840`, zonal for the
    pilot;
  - SSD storage with auto-resize up to a limit;
  - connector-only access, no authorized networks;
  - **daily backups (14 kept), point-in-time recovery (7 days)**;
  - **deletion protection** at the Terraform and API levels;
  - the `zaza_control` database.
- **Secret Manager:** the containers `zaza-db-app-password`,
  `zaza-db-control-app-password` and `zaza-db-migrator-password`, replicated
  in `asia-south1`. **No values.**
- **Cloud Run** (only when `deploy_service = true`):
  - the service mounts the Cloud SQL socket at `/cloudsql` and the password
    secrets as files;
  - max instances 3; health probes on `/api/v1/sync/health`;
  - the `zaza-migrate` job runs explicit migration commands with the
    migrator identity.

## Not here, on purpose

- **Tenant databases.** Customers are created dynamically; the
  application's provisioning job creates their `zaza_t_…` databases.
- **Database roles and passwords.** They are created with SQL, and each
  password is generated directly into Secret Manager, so no secret ever
  enters Terraform variables or state.
- **Secret versions, service-account keys, load balancers, Cloud Armor,
  DNS and Cloud Storage.**

## Offline validation (what Phase 10D ran)

```sh
# on a copy of this directory, in hashicorp/terraform:1.9.8
terraform fmt -check -recursive
terraform init -backend=false     # downloads the provider plugin only
terraform validate                # no credentials, no API calls
terraform providers lock -platform=linux_amd64 -platform=windows_amd64 -platform=darwin_arm64 -platform=darwin_amd64
```

`.terraform.lock.hcl` pins the validated google provider (7.46.2).

## Phase 10E runbook outline (not done)

1. Create a versioned GCS bucket for the state, then enable the `backend
   "gcs"` block in `versions.tf`.
2. Copy `terraform.tfvars.example` to `terraform.tfvars` (git-ignored) and
   choose `sql_instance_name`.
3. Run `terraform plan`, review it, then `terraform apply` with
   `deploy_service = false`.
4. As the Cloud SQL admin user, create the roles `zaza_app`,
   `zaza_control_app` and `zaza_migrator` (CREATEDB). None of them gets
   SUPERUSER or CREATEROLE. Generate each password straight into its secret.
5. Build and push the image (`deploy/gcp/README.md`), and record the digest.
6. Set `container_image` and `deploy_service = true`, then plan and apply.
7. Run the migration job with `control-migrate`. Verify
   `check-config --cloud-run --connect`, health, and the backup, PITR and
   deletion-protection settings.

**Never commit** `*.tfstate*`, `*.tfplan`, `terraform.tfvars` or
`.terraform/`; `.gitignore` excludes them.
