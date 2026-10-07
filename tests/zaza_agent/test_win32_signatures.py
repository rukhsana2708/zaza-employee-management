"""Win64 correctness of the ctypes callback/return types.

LRESULT is LONG_PTR — 64 bits on Win64. Declaring a hook or window procedure
(or DefWindowProcW) as returning ``c_long`` truncates it to 32 bits. These
checks pin the pointer-sized type and fail if ``c_long`` reappears in
executable code in the two modules that declare Win32 callbacks."""

from __future__ import annotations

import ast
import ctypes
import os
from pathlib import Path

import pytest

import deskmate.zaza as zaza_pkg

CALLBACK_MODULES = ("input_hooks.py", "session_lock.py")


def _attribute_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


@pytest.mark.parametrize("filename", CALLBACK_MODULES)
def test_no_c_long_in_executable_code(filename):
    path = Path(zaza_pkg.__file__).resolve().parent / filename
    assert "c_long" not in _attribute_names(path)


@pytest.mark.skipif(os.name != "nt", reason="WINFUNCTYPE prototypes exist only on Windows")
def test_hookproc_returns_pointer_sized_lresult():
    from deskmate.zaza.input_hooks import HOOKPROC

    assert HOOKPROC._restype_ is ctypes.c_ssize_t


@pytest.mark.skipif(os.name != "nt", reason="WINFUNCTYPE prototypes exist only on Windows")
def test_wndproc_returns_pointer_sized_lresult():
    from deskmate.zaza.session_lock import WNDPROC

    assert WNDPROC._restype_ is ctypes.c_ssize_t


def test_def_window_proc_restype_uses_lresult():
    """DefWindowProcW's restype is assigned inside the message loop, so check
    the assignment statically: its right-hand side must be ``LRESULT``."""
    path = Path(zaza_pkg.__file__).resolve().parent / "session_lock.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "restype"
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "DefWindowProcW"
            ):
                found.append(ast.unparse(node.value))
    assert found == ["LRESULT"]
