"""Cloud SQL connection settings for Cloud Run (Phase 10D).

On Cloud Run the service's Cloud SQL connection (configured on the SERVICE,
never by a request) mounts a Unix socket for the instance at::

    /cloudsql/<PROJECT>:<REGION>:<INSTANCE>/.s.PGSQL.5432

The connector in front of that socket authorizes the service's runtime
identity and encrypts the traffic to the instance, so no TLS settings,
public IP allowlist or database host is needed in the application.

ZaZa enables this mode with ONE server-side setting::

    ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME=zaza-employee-monitor:asia-south1:<instance>

(``ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME`` for the control-plane
database). The value is validated strictly and turned into the socket
directory here; it never comes from HTTP input, the control database or a
tenant descriptor. Normal TCP (``ZAZA_DB_HOST``) remains the mode for local
development, tests and the historical Windows deployment.
"""

from __future__ import annotations

import re

CLOUDSQL_SOCKET_ROOT = "/cloudsql"

# <project>:<region>:<instance>
# project:  6-30 chars, lower-case letters, digits and hyphens, starts with a
#           letter, does not end with a hyphen (domain-scoped "example.com:p"
#           projects are not supported)
# region:   e.g. asia-south1, us-central1, europe-west4
# instance: 1-63 chars, lower-case letters, digits and hyphens, starts with a
#           letter, does not end with a hyphen
INSTANCE_CONNECTION_NAME_RE = re.compile(
    r"^(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])"
    r":(?P<region>[a-z]+-[a-z]+[0-9]{1,2})"
    r":(?P<instance>[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?)$"
)


class CloudSqlConfigError(ValueError):
    """Invalid Cloud SQL setting. The message never echoes a secret."""


def instance_connection_name(value: object) -> str:
    """The validated ``PROJECT:REGION:INSTANCE`` string (surrounding
    whitespace removed). Anything else — a host, URL, path, port, extra
    colons, upper case — is refused."""
    if not isinstance(value, str):
        raise CloudSqlConfigError("the Cloud SQL instance connection name must be text")
    name = value.strip()
    match = INSTANCE_CONNECTION_NAME_RE.match(name)
    if match is None or "--" in match.group("project") or "--" in match.group("instance"):
        raise CloudSqlConfigError("the Cloud SQL instance connection name must look like "
                                  "PROJECT:REGION:INSTANCE (e.g. zaza-employee-monitor:asia-south1:zaza-pg)")
    return name


def socket_directory(name: str, *, root: str = CLOUDSQL_SOCKET_ROOT) -> str:
    """``/cloudsql/<PROJECT:REGION:INSTANCE>`` — the directory libpq is given
    as ``host`` (a host that starts with ``/`` is a Unix-socket directory)."""
    if root != CLOUDSQL_SOCKET_ROOT and (not root.startswith("/") or ".." in root.split("/") or "//" in root):
        raise CloudSqlConfigError("the Cloud SQL socket root must be an absolute path")
    return f"{root.rstrip('/')}/{instance_connection_name(name)}"


def is_cloudsql_socket(host: str) -> bool:
    """True when ``host`` is a Cloud SQL socket directory under /cloudsql."""
    prefix = CLOUDSQL_SOCKET_ROOT + "/"
    if not host.startswith(prefix):
        return False
    try:
        instance_connection_name(host[len(prefix):])
    except CloudSqlConfigError:
        return False
    return True
