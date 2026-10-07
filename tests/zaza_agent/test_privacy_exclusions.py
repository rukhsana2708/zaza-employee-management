"""Sensitive application / site exclusions: the real title, hidden app name,
or excluded domain must never reach any table, and the real foreground probe
must not even read an excluded app's title."""

from __future__ import annotations

import ctypes
import os
from types import SimpleNamespace

import pytest

from deskmate.zaza import window_watch
from deskmate.zaza.privacy import (
    EXCLUDED_APP_NAME,
    EXCLUDED_SITE,
    EXCLUDED_TITLE,
    PrivacyFilter,
    normalize_domain,
    normalize_title,
)
from deskmate.zaza.window_watch import WindowSample, get_foreground_window

TABLES = ("activity_events", "activity_periods", "idle_periods", "work_sessions", "app_usage_daily")


def _db_text(store) -> str:
    """Every text value stored anywhere in the agent's tables, lowercased."""
    chunks = []
    for table in TABLES:
        for row in store._query(f"SELECT * FROM {table}"):
            chunks.extend(str(v) for v in row.values() if isinstance(v, str))
    return "\n".join(chunks).lower()


def _run(agent, clock, seconds=60):
    agent.recorder.record_keyboard()
    agent.tick()
    for _ in range(seconds // 10):
        clock.advance(10)
        agent.recorder.record_keyboard()
        agent.tick()
    agent.end_session()


def test_excluded_application_title_never_persisted(make_agent, clock, probe):
    probe.set("KeePassXC.exe", "Bank passwords.kdbx - KeePassXC")
    agent = make_agent(excluded_apps=("keepassxc",))
    _run(agent, clock)
    periods = agent.store.periods()
    assert periods[0]["app_name"] == "KeePassXC.exe"
    assert periods[0]["window_title"] == EXCLUDED_TITLE
    assert periods[0]["privacy_excluded"] == 1
    app_change = [e for e in agent.store.recent_events(1000) if e["event_type"] == "APP_CHANGE"][0]
    assert app_change["window_title"] == EXCLUDED_TITLE and app_change["privacy_excluded"] == 1
    text = _db_text(agent.store)
    assert "bank passwords" not in text and ".kdbx" not in text


def test_hidden_application_name_never_persisted(make_agent, clock, probe):
    probe.set("SecretClient.exe", "Case 4411 - SecretClient")
    agent = make_agent(hidden_app_names=("SecretClient.exe",))
    _run(agent, clock)
    assert agent.store.periods()[0]["app_name"] == EXCLUDED_APP_NAME
    assert [u["app_name"] for u in agent.store.app_usage()] == [EXCLUDED_APP_NAME]
    text = _db_text(agent.store)
    assert "secretclient" not in text and "case 4411" not in text


def test_excluded_domain_never_persisted(make_agent, clock, probe):
    probe.set("chrome.exe", "My Account - Example Bank", domain="https://online.bank.example.com/accounts?id=7")
    agent = make_agent(excluded_domains=("bank.example.com",))
    _run(agent, clock)
    period = agent.store.periods()[0]
    assert period["domain"] == EXCLUDED_SITE
    assert period["window_title"] == EXCLUDED_TITLE  # a browser title reveals the page
    assert period["privacy_excluded"] == 1
    text = _db_text(agent.store)
    assert "bank.example" not in text and "my account" not in text and "accounts?id" not in text


def test_allowed_domain_stored_as_bare_hostname_only(make_agent, clock, probe):
    probe.set("chrome.exe", "Pull request - GitHub", domain="https://www.github.com/org/repo/pull/12?tab=files")
    agent = make_agent()
    _run(agent, clock)
    assert agent.store.periods()[0]["domain"] == "github.com"
    assert "/org/repo" not in _db_text(agent.store)


def test_excluded_app_does_not_split_on_title_changes(make_agent, clock, probe):
    probe.set("KeePassXC.exe", "a.kdbx")
    agent = make_agent(excluded_apps=("KeePassXC.exe",), title_debounce_seconds=0)
    agent.recorder.record_keyboard()
    agent.tick()
    probe.set("KeePassXC.exe", "b.kdbx")
    clock.advance(10)
    agent.recorder.record_keyboard()
    agent.tick()
    assert len(agent.store.periods(agent.session_id)) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("github.com", "github.com"),
        ("WWW.GitHub.com", "github.com"),
        ("https://user:pw@mail.example.org:8443/inbox?q=secret#x", "mail.example.org"),
        ("docs.example.com/path/to/file", "docs.example.com"),
        ("not a domain", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_subdomains_of_excluded_domain_are_excluded():
    f = PrivacyFilter(excluded_domains=["bank.example.com"])
    assert f.filter_domain("online.bank.example.com") == EXCLUDED_SITE
    assert f.filter_domain("bank.example.com") == EXCLUDED_SITE
    assert f.filter_domain("notbank.example.com") == "notbank.example.com"


def test_filter_is_idempotent():
    f = PrivacyFilter(excluded_apps=["a.exe"], hidden_app_names=["h.exe"], excluded_domains=["x.com"])
    for sample in (
        WindowSample("a.exe", "secret"),
        WindowSample("h.exe", "secret"),
        WindowSample("chrome.exe", "secret", domain="x.com"),
        WindowSample("code.exe", "(2) file.py *", domain="https://github.com/a"),
    ):
        once = f.apply(sample)
        assert f.apply(once) == once


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(12) Inbox - Outlook", "Inbox - Outlook"),
        ("[3] Slack - general", "Slack - general"),
        ("● main.py - Visual Studio Code", "main.py - Visual Studio Code"),
        ("notes.txt* - Notepad", "notes.txt* - Notepad"),
        ("draft.docx - Word *", "draft.docx - Word"),
        ("a\tb\n  c", "a b c"),
        (None, ""),
    ],
)
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


def test_normalize_title_caps_length():
    assert len(normalize_title("x" * 5000)) == 256


@pytest.mark.skipif(os.name != "nt", reason="exercises the Win32 probe path")
def test_real_probe_never_reads_title_of_excluded_app(monkeypatch):
    calls = []

    def get_window_text(hwnd, buf, size):  # noqa: ANN001
        calls.append("GetWindowTextW")
        buf.value = "Bank passwords.kdbx"
        return len(buf.value)

    # Plain functions (not bound methods) so the probe can set argtypes/restype.
    user32 = SimpleNamespace(
        GetForegroundWindow=lambda: 1234,
        GetWindowThreadProcessId=lambda hwnd, pid_ref: 1,
        GetWindowTextW=get_window_text,
    )
    class FakeWindll:
        pass

    FakeWindll.user32 = user32

    class FakeCtypes:
        windll = FakeWindll()
        byref = staticmethod(ctypes.byref)
        create_unicode_buffer = staticmethod(ctypes.create_unicode_buffer)
        POINTER = staticmethod(ctypes.POINTER)

    monkeypatch.setattr(window_watch, "ctypes", FakeCtypes)
    monkeypatch.setattr(window_watch, "_foreground_app_name", lambda pid: "KeePassXC.exe")

    excluded = get_foreground_window(PrivacyFilter(excluded_apps=["KeePassXC.exe"]))
    assert excluded.window_title == EXCLUDED_TITLE and excluded.privacy_excluded
    assert calls == []  # title was never read

    allowed = get_foreground_window(PrivacyFilter())
    assert allowed.window_title == "Bank passwords.kdbx"
    assert calls == ["GetWindowTextW"]
