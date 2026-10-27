# syntax=docker/dockerfile:1
#
# ZaZa central server - Linux container for Google Cloud Run (Phase 10B).
#
# Contains ONLY the central server: deskmate/zaza_server/** plus the shared
# sync wire protocol it imports, and the server-only production dependencies
# (exact pins with hashes). No employee agent, no DeskMate UI/capture code,
# no Windows installer, no credentials, no data.
#
# Build from the REPOSITORY ROOT (the build context); the context itself is an
# allowlist (deploy/gcp/Dockerfile.dockerignore):
#
#     docker build --platform linux/amd64 -f deploy/gcp/Dockerfile -t zaza-server:local .
#
# Official Python 3.14 slim image on Debian 13 (trixie), pinned by tag AND digest.
ARG BASE_IMAGE=python:3.14.10-slim-trixie@sha256:82abc9f53600280b0757126f83c5a7d47088592d2acb6b4c0b9e97f76865508c

# ---- dependencies: exact Linux pins, every wheel verified by its SHA-256 ----
FROM ${BASE_IMAGE} AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
COPY deploy/gcp/requirements-server.lock.txt /tmp/requirements-server.lock.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-deps \
        -r /tmp/requirements-server.lock.txt \
 && /opt/venv/bin/python -m pip check \
 && /opt/venv/bin/python -m pip uninstall --yes pip

# ---- runtime ----
FROM ${BASE_IMAGE}
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080
# No package installer at runtime: also remove the base image's own pip.
RUN python -m pip uninstall --yes pip \
 && groupadd --system --gid 10001 zaza \
 && useradd --system --uid 10001 --gid zaza --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin zaza
COPY --from=deps /opt/venv /opt/venv

# Selective copies only (never `COPY . .`): the server import closure, the same
# set as deploy/windows/server-files.txt. Owned by root, read-only to the app.
WORKDIR /app
COPY LICENSE /app/LICENSE
COPY deskmate/__init__.py /app/deskmate/__init__.py
COPY deskmate/zaza/__init__.py /app/deskmate/zaza/__init__.py
COPY deskmate/zaza/sync/__init__.py /app/deskmate/zaza/sync/__init__.py
COPY deskmate/zaza/sync/protocol.py /app/deskmate/zaza/sync/protocol.py
COPY deskmate/zaza_server /app/deskmate/zaza_server
COPY deploy/gcp/entrypoint.sh /app/entrypoint.sh
# Root-owned and not writable by the app user, whatever the build host's file
# modes were (e.g. 0777 on a Windows drive mounted in WSL).
RUN chown -R root:root /app \
 && chmod -R u=rwX,go=rX /app \
 && chmod 0755 /app/entrypoint.sh

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
