#!/bin/sh
# ZaZa central server - Cloud Run container entrypoint (Phase 10B).
#
# Starts the web server and NOTHING else:
#   - no database migrations   (`migrate` is a separate, controlled job)
#   - no manager accounts      (`manager-create` is an interactive admin step)
#   - no sample/fake data
#   - no attendance or Google Sheets jobs (separate scheduled jobs later)
#
# Listens on 0.0.0.0 (all interfaces inside the container) on $PORT, which
# Cloud Run sets; 8080 when unset (local tests). TLS is terminated by Cloud
# Run, never in this container. Logs go to stdout/stderr (no ZAZA_LOG_FILE).
set -eu

if [ "$#" -gt 0 ]; then
    echo "entrypoint: this image only runs the ZaZa web server and takes no arguments" >&2
    exit 64
fi

PORT="${PORT:-8080}"
case "$PORT" in
    '' | *[!0-9]*) echo "entrypoint: PORT must be a number (got '$PORT')" >&2; exit 64 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "entrypoint: PORT must be 1..65535 (got $PORT)" >&2
    exit 64
fi

# The backend must be chosen explicitly. SQLite inside a container lives on an
# ephemeral disk and would silently lose employee records, so it is allowed
# only for an explicit local smoke test.
case "${ZAZA_SERVER_BACKEND:-}" in
    postgres) ;;
    sqlite)
        if [ "${ZAZA_CONTAINER_ALLOW_SQLITE:-}" != "local-test-only" ]; then
            echo "entrypoint: ZAZA_SERVER_BACKEND=sqlite is for local container tests only;" \
                 "set ZAZA_CONTAINER_ALLOW_SQLITE=local-test-only to confirm" >&2
            exit 64
        fi
        ;;
    *)
        echo "entrypoint: set ZAZA_SERVER_BACKEND explicitly (postgres; sqlite only for local tests)" >&2
        exit 64
        ;;
esac

cd /app
# exec: the server becomes PID 1 and receives Cloud Run's SIGTERM directly.
exec python -E -s -u -m deskmate.zaza_server serve --host 0.0.0.0 --port "$PORT"
