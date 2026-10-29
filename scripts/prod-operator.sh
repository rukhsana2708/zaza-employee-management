#!/usr/bin/env bash
# Run a ZaZa operator command against PRODUCTION from your own terminal, for
# commands that need an interactive (hidden) prompt, e.g. setting a password.
#
#   scripts/prod-operator.sh control-db-status
#   scripts/prod-operator.sh saas-user-set-password --email owner@example.com
#   scripts/prod-operator.sh enrollment-code-create --slug zaza-internal --employee-id internal-pilot-1 --hours 24
#     (the code is printed to this terminal only; the Cloud Run job refuses to print codes)
#
# Opens a Cloud SQL proxy on 127.0.0.1:5433 (your gcloud login), reads the
# migrator password from Secret Manager into a private temp file that is
# mounted read-only into the production image and deleted on exit. Nothing is
# printed, logged or stored. Same identity as the zaza-migrate job.
set -euo pipefail

PROJECT=zaza-employee-monitor
INSTANCE="$PROJECT:asia-south1:zaza-prod-pg16"
IMAGE_REF="${ZAZA_IMAGE:-asia-south1-docker.pkg.dev/$PROJECT/zaza/zaza-server@sha256:dda9c50098fd9617599f93b1d00ee7792a3181dff14a717505bc0eec0c290d53}"
PROXY_IMAGE=gcr.io/cloud-sql-connectors/cloud-sql-proxy:2.14.1
[ $# -ge 1 ] || { echo "usage: $0 <zaza_server command> [args...]"; exit 2; }

WORK=$(mktemp -d); chmod 700 "$WORK"
PROXY_NAME="zaza-prod-proxy-$$"
cleanup() { docker rm -f "$PROXY_NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

gcloud secrets versions access latest --secret=zaza-db-migrator-password --project="$PROJECT" > "$WORK/pw"
chmod 444 "$WORK/pw"   # readable inside the container only; the directory is 700 and removed on exit

docker run -d --rm --name "$PROXY_NAME" --network host "$PROXY_IMAGE" \
  --address 127.0.0.1 --port 5433 --token "$(gcloud auth print-access-token)" "$INSTANCE" >/dev/null
for _ in $(seq 1 20); do
  (exec 3<>/dev/tcp/127.0.0.1/5433) 2>/dev/null && break; sleep 1
done

TTY=""; [ -t 0 ] && TTY="-it"
docker run --rm $TTY --network host \
  -v "$WORK/pw:/secrets/pw:ro" \
  -e ZAZA_SERVER_BACKEND=postgres -e ZAZA_DB_SSLMODE=disable \
  -e ZAZA_DB_HOST=127.0.0.1 -e ZAZA_DB_PORT=5433 -e ZAZA_DB_NAME=postgres \
  -e ZAZA_DB_USER=zaza_migrator -e ZAZA_DB_PASSWORD_FILE=/secrets/pw \
  -e ZAZA_CONTROL_DB_HOST=127.0.0.1 -e ZAZA_CONTROL_DB_PORT=5433 -e ZAZA_CONTROL_DB_SSLMODE=disable \
  -e ZAZA_CONTROL_DB_NAME=zaza_control -e ZAZA_CONTROL_DB_USER=zaza_migrator \
  -e ZAZA_CONTROL_DB_PASSWORD_FILE=/secrets/pw -e ZAZA_TENANT_RUNTIME_ROLE=zaza_app \
  --entrypoint python "$IMAGE_REF" -E -s -u -m deskmate.zaza_server "$@"
