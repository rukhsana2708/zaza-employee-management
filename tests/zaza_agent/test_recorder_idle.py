"""Idle/active transitions, driven by a fake clock (no real OS timing)."""

from __future__ import annotations

from deskmate.zaza.recorder import ActivityRecorder


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_not_idle_initially_within_threshold():
    clock = FakeClock()
    rec = ActivityRecorder(idle_threshold_seconds=300, clock=clock)
    assert rec.is_idle() is False


def test_idle_after_threshold_with_no_activity():
    clock = FakeClock()
    rec = ActivityRecorder(idle_threshold_seconds=300, clock=clock)
    clock.advance(299)
    assert rec.is_idle() is False
    clock.advance(2)
    assert rec.is_idle() is True


def test_keyboard_activity_resets_idle_clock():
    clock = FakeClock()
    rec = ActivityRecorder(idle_threshold_seconds=300, clock=clock)
    clock.advance(400)
    assert rec.is_idle() is True
    rec.record_keyboard()
    assert rec.is_idle() is False


def test_mouse_activity_resets_idle_clock():
    clock = FakeClock()
    rec = ActivityRecorder(idle_threshold_seconds=300, clock=clock)
    clock.advance(400)
    assert rec.is_idle() is True
    rec.record_mouse()
    assert rec.is_idle() is False


def test_custom_idle_threshold_is_configurable():
    clock = FakeClock()
    rec = ActivityRecorder(idle_threshold_seconds=5, clock=clock)
    clock.advance(4)
    assert rec.is_idle() is False
    clock.advance(2)
    assert rec.is_idle() is True


def test_snapshot_and_reset_reports_then_clears_flags():
    clock = FakeClock()
    rec = ActivityRecorder(idle_threshold_seconds=300, clock=clock)
    rec.record_keyboard()
    rec.record_mouse()
    kb, mouse = rec.snapshot_and_reset()
    assert (kb, mouse) == (True, True)
    # cleared — a second snapshot with no new activity reports False/False
    kb2, mouse2 = rec.snapshot_and_reset()
    assert (kb2, mouse2) == (False, False)


def test_keyboard_only_activity_does_not_set_mouse_flag():
    clock = FakeClock()
    rec = ActivityRecorder(idle_threshold_seconds=300, clock=clock)
    rec.record_keyboard()
    kb, mouse = rec.snapshot_and_reset()
    assert kb is True
    assert mouse is False


def test_mouse_only_activity_does_not_set_keyboard_flag():
    clock = FakeClock()
    rec = ActivityRecorder(idle_threshold_seconds=300, clock=clock)
    rec.record_mouse()
    kb, mouse = rec.snapshot_and_reset()
    assert kb is False
    assert mouse is True
