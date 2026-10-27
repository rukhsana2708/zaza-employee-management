# ZaZa central server — Google Cloud Run container (Phase 10B)

This folder builds the **central ZaZa server** as a Linux container image
for **Google Cloud Run**. It is preparation only:

- Google Cloud project ID: `zaza-employee-monitor` (project name "ZaZa
  Employee Monitor"). Intended region: `asia-south1`.
- The image has **NOT been deployed** to Google Cloud. No Google Cloud
  resource has been created, and nothing here connects to Google Cloud.
- **PostgreSQL / Cloud SQL is NOT configured yet.** There is no Cloud SQL
  connector, no database secret and no tenant database. The database design
  is a later phase.
- Multi-tenancy, billing and Google Sheets runtime identity (Application
  Default Credentials) are later phases.

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

## Secrets

**Never build a secret into the image.** That covers database passwords,
device tokens, Google service-account JSON keys, API keys and `.env` files.
On Cloud Run they will come from Secret Manager and the service's runtime
identity, which a later phase designs. The build context policy and the
tests refuse secret-looking files, but the rule is: secrets do not go into
the repository or the image.

## Requirements for the Cloud Run deployment phase

These are not changed in Phase 10B; the current behaviour is fail-closed.

1. **Trusted proxy.** `http_security.py` trusts `X-Forwarded-For` and
   `X-Forwarded-Proto` only from **loopback** addresses, which was correct
   for Caddy on the same server. Cloud Run's front end connects from a
   non-loopback address, so these headers are ignored: requests look like
   `http` from the front end's address. Non-loopback values in
   `ZAZA_TRUSTED_PROXY_IPS` are refused, and that must not simply be
   loosened. A Cloud Run mode needs its own design, for example:
   - trust the front end only when running on Cloud Run;
   - take the client address from Cloud Run's documented
     `X-Forwarded-For` semantics;
   - force the `https` scheme, because Cloud Run accepts only HTTPS at the
     front.
2. **Dashboard origin checks.** The dashboard's same-origin check compares
   `Origin` with the request's own scheme and host. Behind Cloud Run that
   scheme is `http`, so `https://` form posts would be refused. That is
   fail-closed, and it needs requirement 1 to work.
3. **Dashboard mounting.** The dashboard is mounted only with PostgreSQL and
   a loopback bind host. On `0.0.0.0` it is not mounted, and only the sync
   API is served. Mounting it on Cloud Run needs requirements 1 and 2, plus
   the Cloud SQL design.
4. **Login rate limiting.** The login rate limiter is in memory, which
   assumes a single process. Cloud Run scales to several instances, so a
   shared store or `max-instances=1` must be chosen.
5. **Host check.** `ZAZA_PUBLIC_HOST` must list the Cloud Run service host
   name(s) and any custom domain.
6. **Production preflight.** `check-config --production` still describes
   the Windows server: PostgreSQL password file, `ZAZA_LOG_FILE`, loopback
   proxies. Cloud Run needs its own, separate check mode, not a weakened
   version of this one.
7. **Health checks.** `GET /api/v1/sync/health` is unauthenticated and
   returns no data. It suits a Cloud Run startup/liveness probe.
