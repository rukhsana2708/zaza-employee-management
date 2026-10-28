# ZaZa Google Cloud infrastructure (Terraform)

> **Status:** applied in stages, from remote state in
> `gs://zaza-employee-monitor-tfstate-e33ac2a6/zaza/production`. The Cloud
> Run **web service** (`deploy_service = false`) is NOT created. See
> "Rollout status" below.

Design and reasoning: [docs/zaza/CLOUD_SQL_PRODUCTION.md](../../../docs/zaza/CLOUD_SQL_PRODUCTION.md).

## What it defines

- **Project and region:** `zaza-employee-monitor`, `asia-south1` (both
  overridable); production labels on every resource.
- **APIs:** run, sqladmin, secretmanager, artifactregistry, iam, cloudbuild.
- **Artifact Registry:** the Docker repository `zaza`, with immutable tags.
  Images are deployed by digest.
- **Service accounts:**
  - `zaza-run` (web service) and `zaza-migrate` (migration job), each with
    `roles/cloudsql.client` and access to its own secrets only;
  - `zaza-build` (Cloud Build, `build.tf`): writes build logs, reads the
    private build-source bucket, and pushes to the `zaza` repository only.
    It has no secrets, no Cloud SQL and no Terraform-state access;
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
- **Build-source bucket** `<project>-build-source` (private, uniform access,
  public access prevention, 30-day lifecycle): holds `git archive` tarballs of
  the exact commits that Cloud Build builds.
- **Cloud Run**, with two separate switches:
  - `deploy_migration_job = true` creates **only** the `zaza-migrate` job.
    The job has no HTTP endpoint, runs as the migrator identity with the
    migrator secret, and by default only reports status;
  - `deploy_service = true` creates the `zaza-server` web service. It mounts
    the Cloud SQL socket and the runtime password secrets, has max instances
    3 and health probes. Its public invoker exists only together with
    `allow_unauthenticated`.

## Not here, on purpose

- **Tenant databases.** Customers are created dynamically; the
  application's provisioning job creates their `zaza_t_…` databases.
- **Database roles and passwords.** They are created with SQL, and each
  password is generated directly into Secret Manager, so no secret ever
  enters Terraform variables or state.
- **Secret versions, service-account keys, load balancers, Cloud Armor
  and DNS.** The only bucket is the private build-source bucket; the state
  bucket was created by hand in 10E-A.

## Offline validation (what Phase 10D ran)

```sh
# on a copy of this directory, in hashicorp/terraform:1.9.8
terraform fmt -check -recursive
terraform init -backend=false     # downloads the provider plugin only
terraform validate                # no credentials, no API calls
terraform providers lock -platform=linux_amd64 -platform=windows_amd64 -platform=darwin_arm64 -platform=darwin_amd64
```

`.terraform.lock.hcl` pins the validated google provider (7.46.2).

## Rollout status

| Step | What | Status |
|---|---|---|
| 10E-A | private, versioned state bucket; GCS backend; reviewed plan | done |
| 10E-B | core apply (18 resources): Cloud SQL `zaza-prod-pg16`, `zaza_control`, Artifact Registry, `zaza-run` / `zaza-migrate`, secret containers; database roles and secret versions out of band | done |
| 10E-C | `zaza-build` identity, build-source bucket, image built from an exact commit and deployed by digest, `deploy_migration_job = true`, `control-migrate` plus the control runtime grants | this phase |
| later | Cloud Run proxy/origin/dashboard fixes, then `deploy_service = true` | **not done: no web service exists** |

Working rules:
- Run Terraform as an administrator with a short-lived gcloud token
  (`GOOGLE_OAUTH_ACCESS_TOKEN`); never use a key file.
- Plan to a file, review it (DELETE must be 0), then apply exactly that
  file.
- `terraform.tfvars` is local and git-ignored. It sets
  `sql_instance_name = "zaza-prod-pg16"`, the image by digest and the two
  deploy switches.

**Never commit** `*.tfstate*`, `*.tfplan`, `terraform.tfvars` or
`.terraform/`; `.gitignore` excludes them.
