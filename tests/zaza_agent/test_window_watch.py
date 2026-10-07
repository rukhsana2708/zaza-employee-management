"""Active application/window detection — via an injected fake probe so this
runs deterministically without depending on whatever window happens to be
focused on the test machine."""

from __future__ import annotations

from deskmate.zaza.window_watch import WindowChangeTracker, WindowSample


def test_first_poll_always_reports_initial_window():
    samples = [WindowSample("notepad.exe", "Untitled - Notepad")]
    tracker = WindowChangeTracker(probe=lambda: samples[0])
    result = tracker.poll()
    assert result == WindowSample("notepad.exe", "Untitled - Notepad")


def test_repeated_identical_window_is_not_reported_again():
    sample = WindowSample("notepad.exe", "Untitled - Notepad")
    tracker = WindowChangeTracker(probe=lambda: sample)
    assert tracker.poll() == sample
    assert tracker.poll() is None
    assert tracker.poll() is None


def test_title_change_within_same_app_is_reported():
    calls = iter(
        [
            WindowSample("chrome.exe", "Tab A - Google Chrome"),
            WindowSample("chrome.exe", "Tab B - Google Chrome"),
        ]
    )
    tracker = WindowChangeTracker(probe=lambda: next(calls))
    first = tracker.poll()
    second = tracker.poll()
    assert first.window_title == "Tab A - Google Chrome"
    assert second.window_title == "Tab B - Google Chrome"


def test_app_switch_is_reported():
    calls = iter(
        [
            WindowSample("notepad.exe", "Untitled - Notepad"),
            WindowSample("chrome.exe", "New Tab - Google Chrome"),
        ]
    )
    tracker = WindowChangeTracker(probe=lambda: next(calls))
    tracker.poll()
    second = tracker.poll()
    assert second.app_name == "chrome.exe"


def test_window_sample_only_has_metadata_fields():
    """App, title, bare domain (unpopulated until domain tracking exists),
    and the privacy flag — no field that could carry window content."""
    fields = WindowSample.__dataclass_fields__.keys()
    assert set(fields) == {"app_name", "window_title", "domain", "privacy_excluded"}
