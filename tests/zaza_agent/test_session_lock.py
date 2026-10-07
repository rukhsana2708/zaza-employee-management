"""Lock/unlock classification. The real watcher needs a live Windows session
message loop, which isn't practical to drive from an automated test (see
MANUAL TEST STEPS in the Phase 1 report) — but the wParam -> LOCK/UNLOCK
mapping it depends on is a pure function and is fully covered here."""

from __future__ import annotations

from deskmate.zaza.session_lock import (
    WTS_SESSION_LOCK,
    WTS_SESSION_UNLOCK,
    classify_session_change,
)


def test_session_lock_code_maps_to_lock():
    assert classify_session_change(WTS_SESSION_LOCK) == "LOCK"


def test_session_unlock_code_maps_to_unlock():
    assert classify_session_change(WTS_SESSION_UNLOCK) == "UNLOCK"


def test_unrelated_wparam_maps_to_none():
    assert classify_session_change(0) is None
    assert classify_session_change(999) is None
