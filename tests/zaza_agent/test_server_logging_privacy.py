"""Log references must not permit offline guessing of predictable identifiers."""
from __future__ import annotations

import hashlib
import re

import pytest

from deskmate.zaza_server import logs


@pytest.mark.parametrize("value", ["predictable-pilot-device", "203.0.113.42", "employee-secret", "unicode-\u03bb\nvalue"])
def test_reference_is_bounded_opaque_and_stable_within_process(value):
    reference = logs.log_reference(value)
    assert re.fullmatch(r"[0-9a-f]{16}", reference)
    assert reference == logs.log_reference(value)
    assert reference != logs.log_reference(value + "-different")
    assert reference != hashlib.sha256(value.encode()).hexdigest()[:16]
    assert value not in reference


def test_reference_changes_with_private_process_key(monkeypatch):
    monkeypatch.setattr(logs, "_LOG_REFERENCE_KEY", b"a" * 32)
    before = logs.log_reference("easily-guessed-device")
    monkeypatch.setattr(logs, "_LOG_REFERENCE_KEY", b"b" * 32)
    assert logs.log_reference("easily-guessed-device") != before


@pytest.mark.parametrize("value", [None, ""])
def test_missing_reference_is_explicit(value):
    assert logs.log_reference(value) == "-"


def test_limiter_reuses_neutral_log_reference_without_changing_counter_keys():
    from deskmate.zaza_server.ratelimit import device_key, digest

    assert digest is logs.log_reference
    assert device_key("org-a", "device-a", "ignored") == hashlib.sha256(
        b"zaza-sync-device-v1\x00org-a\x00device-a").hexdigest()


def test_storage_error_text_never_reaches_application_logs(caplog):
    import logging

    from fastapi.testclient import TestClient

    from deskmate.zaza.sync.protocol import BATCH_PATH
    from deskmate.zaza_server.app import create_app
    from deskmate.zaza_server.repository import InMemoryRepository, RepositoryUnavailable

    sensitive = "employee-private org-private device-private password=secret zzd_private_token 203.0.113.42"

    class Down(InMemoryRepository):
        def find_token(self, token_hash):
            raise RepositoryUnavailable(sensitive)

    with caplog.at_level(logging.ERROR):
        response = TestClient(create_app(Down())).post(BATCH_PATH, json={}, headers={
            "Authorization": "Bearer zzd_private_token", "X-ZaZa-Device-Id": "device-private"})
    assert response.status_code == 503
    assert response.json() == {"error": "storage unavailable"}
    assert f"error_ref={logs.log_reference(sensitive)}" in caplog.text
    for value in sensitive.split():
        assert value not in caplog.text
