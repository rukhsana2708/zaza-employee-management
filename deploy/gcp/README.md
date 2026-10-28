# ZaZa central server — Google Cloud Run container (Phase 10B)

This folder builds the **central ZaZa server** as a Linux container image
for **Google Cloud Run**, and holds its infrastructure definition
(`terraform/`).

**Current production state** (project `zaza-employee-monitor`, region
`asia-south1`):

| Exists | Does NOT exist yet |
|---|---|
| Cloud SQL `zaza-prod-pg16` (PostgreSQL 16) with the `zaza_control` database | the `zaza-server` web service: it has **NOT been deployed**, and no public application URL exists |
| control schema **`c0003_device_enrollment`** (10G-B), runtime grants applied; the enrollment tables are empty | any organization, SaaS user, membership or tenant (`zaza_t_*`) database |
| Artifact Registry `zaza` with the production image (by digest, see "Production build and migration job") | any enrollment code, device route or issued device token |
| the `zaza-migrate` Cloud Run **job** (explicit migrations / operator commands; no HTTP endpoint) | edge protection (load balancer + Cloud Armor), needed before public traffic |
| service accounts `zaza-run`, `zaza-migrate`, `zaza-build`; three password secrets; database roles | employee or customer traffic |

Production runs the image of commit `778488c` (by digest, on the
`zaza-migrate` job only), and its control schema is at the head,
`c0003_device_enrollment`. The code in this repository may be AHEAD of
production. See
[docs/zaza/CLOUD_SQL_PRODUCTION.md](../../docs/zaza/CLOUD_SQL_PRODUCTION.md)
(status table) and [docs/zaza/ENROLLMENT.md](../../docs/zaza/ENROLLMENT.md).

- **Cloud SQL connection:** the server reaches Cloud SQL through Cloud
  Run's Unix socket (`ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME`). Passwords
  come from Secret Manager files (`ZAZA_DB_PASSWORD_FILE`,
  `ZAZA_CONTROL_DB_PASSWORD_FILE`). Tenant pools are bounded, and
  `check-config --cloud-run` validates all of this.
- Billing and Google Sheets runtime identity (Application Default
  Credentials) are later phases.

The earlier single-server Windows deployment (`deploy/windows/`) is kept
unchanged as reference material.

## What is in the image

Only the central server and what it imports:

| Path | Why |
|---|---|
| `deskmate/zaza_server/**` | the server: sync API, attendance, dashboard, Sheets, PostgreSQL code and migrations |
| `deskmate/zaza/sync/protocol.py` (+ the three package `__init__.py` files) | the sync wire protocol, shared with the agent |
| `LICENSE` | |
| `/app/entrypoint.sh` | starts the web server, nothing else |
| `/opt/venv` | the server-only production dependencies, from `requirements-server.lock.txt` |

This is the same file set as `deploy/windows/server-files.txt`. The tests
in `tests/zaza_agent/test_gcp_container.py` keep the two in step.

**Not in the image:**
- the Windows employee agent, any DeskMate screen/UI/capture module, and the
  Windows installer;
- `.git`, `.env` files, secrets, password files, Google JSON credentials, and
  keys or certificates;
- SQLite databases, logs, caches (`__pycache__`, `.pytest_cache`), `*.zip`
  archives, screenshots, test output, and Claude/session files;
- pip: both the venv's pip and the base image's own pip are removed.

Two layers enforce this:
- **The build context is an allowlist.** `Dockerfile.dockerignore` excludes
  everything, lets in only the paths above, then re-excludes caches,
  archives, `.env`, `.json`, keys, databases and logs, even inside allowed
  paths.
- **The Dockerfile copies path by path.** It never does `COPY . .`.

The application files are owned by root and are read-only for the app user.

## Base image and dependencies

- **Base:** the official `python:3.14.10-slim-trixie` image (Python 3.14.10,
  Debian 13, linux/amd64), pinned by tag **and** digest in `ARG BASE_IMAGE`
  in the `Dockerfile`.
- **Dependencies:**
  - `requirements-server.txt` has the same server-only set as the Windows
    deployment. The employee-side packages (mss, Pillow, pywin32, comtypes,
    uiautomation, WinRT, OCR/audio libraries, numpy, PyInstaller, …) are
    not in it, and the tests fail if one appears, even indirectly.
  - `requirements-server.lock.txt` has the exact Linux pins: every package
    with the SHA-256 of the Linux wheel actually used, and that wheel's file
    name. The image installs with
    `pip install --require-hashes --only-binary=:all: --no-deps` and then
    runs `pip check`.
  - The lock is **not** a copy of the Windows lock. `lock-requirements.sh`
    resolves it inside the pinned base image (`pip install --dry-run
    --report`), so Linux environment markers and Linux wheels are used.
    Regenerate it after changing `requirements-server.txt` or the base
    image:

    ```sh
    sh deploy/gcp/lock-requirements.sh      # from the repository root; needs Docker
    ```

## Build locally

From the **repository root** (the build context):

```sh
docker build --platform linux/amd64 -f deploy/gcp/Dockerfile -t zaza-server:local .
```

Docker uses `deploy/gcp/Dockerfile.dockerignore` automatically (BuildKit).
The image is about 460 MB uncompressed. Most of that is the Google API
client's discovery documents, cryptography and psycopg-binary.

## Run locally

The container listens on **`0.0.0.0`** (all interfaces inside the container)
on the port in **`PORT`**. Cloud Run sets `PORT`; when it is unset the
entrypoint uses **8080**. TLS is not handled in the container; on Cloud Run,
Google's front end terminates HTTPS. Logs go to stdout/stderr, which Cloud
Logging collects; `ZAZA_LOG_FILE` is not used here.

The database backend must be chosen explicitly. A container's disk is
temporary, so SQLite would silently lose employee records. The entrypoint
therefore refuses SQLite unless a local test opts in. Local smoke test with
a throwaway SQLite database (nothing persists after the container is
removed):

```sh
docker run --rm -p 127.0.0.1:8080:8080 \
    -e PORT=8080 \
    -e ZAZA_SERVER_BACKEND=sqlite \
    -e ZAZA_CONTAINER_ALLOW_SQLITE=local-test-only \
    -e ZAZA_SERVER_DB=/tmp/zaza-smoke/central.db \
    zaza-server:local

curl http://127.0.0.1:8080/api/v1/sync/health      # {"status":"ok",...}
```

The entrypoint exits with code 64, without starting anything, if:
- `ZAZA_SERVER_BACKEND` is missing or unknown;
- SQLite is chosen without the local-test opt-in;
- `PORT` is not a number from 1 to 65535;
- any command-line argument is given.

## What the container does at start-up

Only `exec python -E -s -u -m deskmate.zaza_server serve --host 0.0.0.0
--port "$PORT"`. The server is PID 1 and receives Cloud Run's SIGTERM
directly. It **never** does any of these at start-up:
- runs `migrate`: schema changes will be a separate, controlled job, and on
  PostgreSQL `serve` refuses to start until migrations have run;
- creates manager accounts (`manager-create` is an interactive admin step);
- loads sample or fake data;
- runs the attendance or Google Sheets jobs (separate scheduled jobs later).

## Production build and migration job (Phase 10E-C)

Images are built on **Cloud Build**, never on a laptop, and run as the
dedicated service account **`zaza-build`**. They are built from a
`git archive` of an exact, pushed commit, using `deploy/gcp/cloudbuild.yaml`:

```sh
SHA=<full commit sha, pushed>
git archive --format=tar.gz -o source.tar.gz "$SHA"
gcloud storage cp source.tar.gz gs://zaza-employee-monitor-build-source/source/$SHA.tar.gz
gcloud builds submit gs://zaza-employee-monitor-build-source/source/$SHA.tar.gz \
  --region=asia-south1 --config=<deploy/gcp/cloudbuild.yaml of that commit> \
  --substitutions=_GIT_SHA=$SHA \
  --gcs-source-staging-dir=gs://zaza-employee-monitor-build-source/staging \
  --service-account=projects/zaza-employee-monitor/serviceAccounts/zaza-build@zaza-employee-monitor.iam.gserviceaccount.com
```

Always pass `--gcs-source-staging-dir`. Without it, gcloud re-stages the
source in Google's default `<project>_cloudbuild` bucket, which `zaza-build`
cannot (and should not) read.

The image gets one immutable tag (the commit SHA). Deployments reference
the **digest**. Current production image (migration job):
`asia-south1-docker.pkg.dev/zaza-employee-monitor/zaza/zaza-server@sha256:b7ad3a8923069c63a62cb6ee6839419c3c13fa7c540e7dcf6a480810537a3bc8` (commit `778488c`, Cloud Build `efe5ed6f-0a5e-4489-8f01-1cbc01779695`, Phase 10G-B).

The **`zaza-migrate` job** runs explicit commands with the migrator identity:

```sh
gcloud run jobs execute zaza-migrate --region=asia-south1 --wait                          # control-db-status (default)
gcloud run jobs execute zaza-migrate --region=asia-south1 --wait --args=control-migrate
```

## Cloud SQL (Phase 10D; created in Phase 10E-B)

On Cloud Run, the service's Cloud SQL connection mounts
`/cloudsql/PROJECT:REGION:INSTANCE`. The container is configured with:

```sh
ZAZA_SERVER_BACKEND=postgres
ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME=zaza-employee-monitor:asia-south1:<instance>
ZAZA_DB_USER=zaza_app
ZAZA_DB_PASSWORD_FILE=/secrets/db-app/password                 # Secret Manager, mounted as a file
ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME=zaza-employee-monitor:asia-south1:<instance>
ZAZA_CONTROL_DB_NAME=zaza_control
ZAZA_CONTROL_DB_USER=zaza_control_app
ZAZA_CONTROL_DB_PASSWORD_FILE=/secrets/db-control/password
ZAZA_TENANT_POOL_MAX=2  ZAZA_TENANT_POOL_CACHE_MAX=8  ZAZA_TENANT_POOL_IDLE_SECONDS=300
ZAZA_CLOUD_RUN_MAX_INSTANCES=3  ZAZA_DB_CONNECTION_BUDGET=70
```

Migrations run only as the separate `zaza-migrate` job (`control-migrate`,
`migrate`, `tenant-migrate`). The entrypoint still only serves.

**Locally verified in Phase 10D**, with disposable PostgreSQL 16 and its
socket mounted at `/cloudsql/<name>`:
- the image migrates over the socket via an explicit command;
- `check-config --cloud-run --connect` passes;
- the image serves health 200 with `--network none`, so it needs no network
  or Google API at start-up;
- it never auto-migrates or logs the password.

## Secrets

**Never build a secret into the image.** That covers database passwords,
device tokens, Google service-account JSON keys, API keys and `.env` files.
On Cloud Run they will come from Secret Manager and the service's runtime
identity, which a later phase designs. The build context policy and the
tests refuse secret-looking files, but the rule is: secrets do not go into
the repository or the image.

## Cloud Run web service: status of the requirements

Phase 10F-A resolved the HTTP items in code; the web service is still NOT
deployed (`deploy_service = false`). See
[docs/zaza/CLOUD_SQL_PRODUCTION.md](../../docs/zaza/CLOUD_SQL_PRODUCTION.md) §10a.

1. **Trusted proxy: done (10F-A).** `ZAZA_HTTP_DEPLOYMENT_MODE=cloud-run`
   (set by Terraform for the service) is a separate mode:
   - the external scheme is https by deployment;
   - `X-Forwarded-*` and `Forwarded` are never trusted;
   - the client address is unverified.

   The loopback-proxy mode for Caddy on Windows is unchanged, and
   non-loopback `ZAZA_TRUSTED_PROXY_IPS` are still refused (and refused
   outright in cloud-run mode).
2. **Dashboard origin checks: done (10F-A).** One central rule
   (`http_security.same_origin`) compares Origin with the EXTERNAL
   `https://host`. CSRF tokens and the login double-submit token are
   unchanged.
3. **Dashboard mounting: decided (10F-A).** The legacy dashboard is NEVER
   mounted in Cloud Run mode; the SaaS dashboard and SaaS authentication
   replace it. Local and loopback-proxy keep the old rule.
4. **Login rate limiting: boundary defined (10F-A).** The in-memory limiter
   is per process. The SaaS login must use shared state or an edge control
   (`RateLimiter` boundary in `dashboard/ratelimit.py`).
5. **Host check: done.** Terraform sets `ZAZA_PUBLIC_HOST` to the service's
   deterministic run.app host, and `serve` refuses Cloud Run mode without
   it.
6. **Production preflight: done.** `check-config --cloud-run [--connect]`
   also requires the cloud-run HTTP mode, the public host and a disabled
   legacy dashboard. `check-config --production` (Windows) is unchanged,
   except that it now fails if the mode is not loopback-proxy.
7. **Health checks.** `GET /api/v1/sync/health` is unauthenticated and
   returns no data. It suits a Cloud Run startup/liveness probe.
