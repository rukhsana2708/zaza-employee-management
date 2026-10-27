#!/bin/sh
# Regenerate deploy/gcp/requirements-server.lock.txt (Linux x86-64, Cloud Run).
#
# Resolves requirements-server.txt INSIDE the same pinned base image the
# Dockerfile uses, so environment markers are evaluated for Linux, and records
# the SHA-256 of the exact wheel pip selected for that platform. Run from the
# repository root on a machine with Docker:
#
#     sh deploy/gcp/lock-requirements.sh
#
# Nothing is installed: pip runs with --dry-run --report.
set -eu

BASE_IMAGE=$(sed -n 's/^ARG BASE_IMAGE=//p' deploy/gcp/Dockerfile)
[ -n "$BASE_IMAGE" ] || { echo "BASE_IMAGE not found in deploy/gcp/Dockerfile" >&2; exit 1; }

docker run --rm --platform linux/amd64 --network bridge \
    -v "$(pwd)/deploy/gcp:/gcp" -w /gcp "$BASE_IMAGE" sh -eu -c '
python -m pip install --quiet --disable-pip-version-check --dry-run --ignore-installed \
    --only-binary=:all: --report /tmp/report.json -r requirements-server.txt
python - <<"EOF"
import json, platform, sys
report = json.load(open("/tmp/report.json"))
rows = []
for item in report["install"]:
    meta, info = item["metadata"], item["download_info"]
    sha = info["archive_info"]["hashes"]["sha256"]
    wheel = info["url"].rsplit("/", 1)[-1]
    rows.append((meta["name"].lower(), meta["version"], sha, wheel))
rows.sort()
with open("requirements-server.lock.txt", "w", encoding="ascii", newline="\n") as out:
    out.write("# ZaZa central server - exact Linux production pins with hashes (Phase 10B, Cloud Run).\n")
    out.write(f"# Resolved from requirements-server.txt INSIDE the pinned base image: CPython {platform.python_version()}, "
              f"{platform.system()} {platform.machine()} (manylinux wheels).\n")
    out.write("# Install ONLY with:  python -m pip install --require-hashes --only-binary=:all: --no-deps "
              "-r requirements-server.lock.txt\n")
    out.write("# Regenerate with deploy/gcp/lock-requirements.sh after changing requirements-server.txt "
              "or the base image.\n\n")
    for name, version, sha, wheel in rows:
        out.write(f"{name}=={version} \\\n    --hash=sha256:{sha}\n    # {wheel}\n")
print(f"locked {len(rows)} packages for", sys.platform, platform.machine())
EOF
'
