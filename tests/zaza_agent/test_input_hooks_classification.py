"""Keyboard/mouse message classification — pure functions, no real OS hook
needed. These are exactly what the low-level hook callbacks consult before
(and instead of) ever touching key/pointer content."""

from __future__ import annotations

from deskmate.zaza.input_hooks import (
    WM_KEYDOWN,
    WM_LBUTTONDOWN,
    WM_MBUTTONDOWN,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_RBUTTONDOWN,
    WM_SYSKEYDOWN,
    WM_XBUTTONDOWN,
    is_key_down_message,
    is_mouse_activity_message,
)


def test_keydown_messages_are_classified_as_keyboard_activity():
    assert is_key_down_message(WM_KEYDOWN) is True
    assert is_key_down_message(WM_SYSKEYDOWN) is True


def test_non_keydown_message_is_not_keyboard_activity():
    assert is_key_down_message(WM_LBUTTONDOWN) is False
    assert is_key_down_message(0) is False


def test_mouse_button_wheel_and_move_are_mouse_activity():
    for wparam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN, WM_MOUSEWHEEL, WM_MOUSEMOVE):
        assert is_mouse_activity_message(wparam) is True


def test_keyboard_message_is_not_mouse_activity():
    assert is_mouse_activity_message(WM_KEYDOWN) is False


def test_hook_callbacks_never_cast_lparam_to_a_struct():
    """Static guard: the keyboard/mouse hook callbacks must never dereference
    ``lparam`` (that's the only place a vkCode or pointer coordinate could be
    read from). Source-level check so the invariant can't quietly regress."""
    import inspect

    from deskmate.zaza.input_hooks import ZazaInputHooks

    kb_src = inspect.getsource(ZazaInputHooks._kb_callback)
    mouse_src = inspect.getsource(ZazaInputHooks._mouse_callback)
    for src in (kb_src, mouse_src):
        assert "cast" not in src
        assert "lparam" in src  # still present as a parameter, just unused for content
