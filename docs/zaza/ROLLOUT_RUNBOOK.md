# Internal rollout runbook (Phases 10G-E-B to the first pilot)

Every step here changes production and needs a person (or an explicit
permission rule) to authorise it. Image: commit `bc81ce5`, digest
`asia-south1-docker.pkg.dev/zaza-employee-monitor/zaza/zaza-server@sha256:dda9c50098fd9617599f93b1d00ee7792a3181dff14a717505bc0eec0c290d53`
(Cloud Build `d83de874-d6b5-4e33-84ad-0a21c832727d`). Run `gcloud` from WSL.

## 1. 10G-E-B: put the operator image on the job

```sh
IMG=asia-south1-docker.pkg.dev/zaza-employee-monitor/zaza/zaza-server@sha256:dda9c50098fd9617599f93b1d00ee7792a3181dff14a717505bc0eec0c290d53
gcloud run jobs update zaza-migrate --region=asia-south1 --image=$IMG
gcloud run jobs execute zaza-migrate --region=asia-south1 --wait --args=control-db-status
```

Then set `container_image` in the git-ignored `terraform.tfvars` to the same
digest and confirm `terraform plan` reports **No changes** (Terraform is not
installed on the Windows host; use a machine that has it).

## 2. Internal bootstrap (no password, no device)

```sh
J="gcloud run jobs execute zaza-migrate --region=asia-south1 --wait"
$J --args=saas-user-create,--email,zazaai4u@gmail.com,--name,"ZaZa Owner",--no-password
$J --args=saas-membership-add,--email,zazaai4u@gmail.com,--slug,zaza-internal,--role,OWNER
$J --args=saas-employee-create,--slug,zaza-internal,--employee-id,internal-pilot-1,--name,"Internal Pilot Employee",--timezone,Asia/Dhaka
$J --args=saas-employee-list,--slug,zaza-internal
```

Never use `add-employee` / `list-employees` here: they open the base
database (`postgres`), not the tenant.

Expected afterwards: 1 user (no password), 1 OWNER membership, 1 employee;
0 devices, codes and routes. Take no enrollment step yet.

## 3. Edge and web service (Phase 10H)

1. In `terraform.tfvars` set `deploy_service = true`,
   `allow_unauthenticated = true`, `deploy_edge = true`,
   `edge_domain = "<your domain>"`, `container_image` = the digest above.
2. `terraform validate`, then `terraform plan`; review that only edge and
   service resources are created. Apply only after the review.
3. Point the domain's A record at the `edge_ip` output; wait until the
   managed certificate is ACTIVE.
4. Check: the default `*.run.app` URL is unreachable, the domain answers
   `/api/v1/sync/health`, and a flood from one IP gets HTTP 429.

## 4. Pilot

Only after step 3 passes: set the owner's password, issue the first
enrollment code (`enrollment-code-create`), install the agent on the pilot
machine, and enroll it. Watch `enrollment-reconcile` and the audit log.
