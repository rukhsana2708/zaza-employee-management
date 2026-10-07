"""Automated guard: the ZaZa agent package must not invoke screenshot, OCR,
clipboard, or audio/video capture — not "hidden from the UI," not present at
all. This is checked two ways: (1) the forbidden DeskMate modules never get
imported as a side effect of importing/running the ZaZa agent, and (2) the
ZaZa package source never references the lower-level Win32/library APIs
those features are built on.
"""

from __future__ import annotations

import ast
import importlib
import sys
import textwrap
from pathlib import Path

import pytest

import deskmate.zaza as zaza_pkg

FORBIDDEN_MODULES = (
    "deskmate.screen.capture",
    "deskmate.screen.ocr",
    "deskmate.screen.snapshot",
    "deskmate.screen.video_chunks",
    "deskmate.a11y.clipboard",
    "deskmate.a11y.uia_tree",
    "deskmate.a11y.document",
    "deskmate.audio",
)

# APIs that back screenshot/OCR/clipboard/audio/video capture. None of these
# identifiers should ever appear anywhere in the ZaZa package's own source.
FORBIDDEN_IDENTIFIERS = (
    "win32clipboard",
    "GetClipboardData",
    "CF_UNICODETEXT",
    "ImageGrab",
    "BitBlt",
    "PrintWindow",
    "mss",  # screenshot library used by deskmate/screen
    "sounddevice",
    "pyaudiowpatch",
    "faster_whisper",
    "pytesseract",
    "read_focused_value",  # upstream's full-input-box UIA text snapshot
)


def _zaza_package_dir() -> Path:
    return Path(zaza_pkg.__file__).resolve().parent


def _all_zaza_source_files() -> list[Path]:
    return sorted(_zaza_package_dir().rglob("*.py"))


def test_zaza_package_has_source_files_to_check():
    assert len(_all_zaza_source_files()) >= 5


def test_no_forbidden_identifiers_anywhere_in_zaza_source():
    hits = []
    for path in _all_zaza_source_files():
        text = path.read_text(encoding="utf-8")
        for identifier in FORBIDDEN_IDENTIFIERS:
            if identifier in text:
                hits.append(f"{path.name}: {identifier}")
    assert not hits, f"forbidden capture identifiers found: {hits}"


def test_no_zaza_module_imports_a_forbidden_deskmate_module():
    """Parses each ZaZa module's AST (no execution) and checks its import
    statements against the forbidden list — catches both ``import x`` and
    ``from x import y`` forms."""
    offenders = []
    for path in _all_zaza_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                # Resolve relative imports (level > 0) against the zaza package.
                prefix = "deskmate.zaza." if node.level else ""
                names = [f"{prefix}{node.module}"]
            else:
                continue
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_MODULES):
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, f"forbidden module imports found: {offenders}"


def test_forbidden_modules_not_in_sys_modules_after_running_agent():
    """Import and exercise the agent, then confirm none of the forbidden
    DeskMate capture modules were pulled into the process as a side effect."""
    for name in FORBIDDEN_MODULES:
        sys.modules.pop(name, None)

    from deskmate.zaza.agent import ActivityAgent
    from deskmate.zaza.config import AgentConfig
    from deskmate.zaza.storage import ActivityStore
    from deskmate.zaza.window_watch import WindowSample

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        config = AgentConfig(db_path=str(Path(tmp) / "activity.db"))
        store = ActivityStore(config.db_path)
        agent = ActivityAgent(
            config, store=store, window_probe=lambda: WindowSample("notepad.exe", "Untitled")
        )
        agent.recorder.record_keyboard()
        agent.recorder.record_mouse()
        agent.tick()
        store.close()

    loaded_forbidden = [m for m in FORBIDDEN_MODULES if m in sys.modules]
    assert not loaded_forbidden, f"forbidden modules were imported: {loaded_forbidden}"


def test_importlib_can_still_resolve_forbidden_modules_exist_upstream():
    """Sanity check that FORBIDDEN_MODULES names real upstream modules (so the
    guard above is testing something real, not typo'd dead paths)."""
    for name in FORBIDDEN_MODULES:
        assert importlib.util.find_spec(name) is not None, f"expected upstream module missing: {name}"


# ─── executable-code guards for keystroke/pointer content APIs ─────────────
#
# These names are how keystroke or pointer *content* would be read: the
# low-level hook structs (and their key-code fields), and the key-state /
# key-to-text APIs. They are checked against executable code only — names,
# attributes, imports, definitions, keyword args, and non-docstring string
# literals (which catches ``getattr(user32, "GetAsyncKeyState")``) — so the
# docstrings that explain *why* the agent doesn't read them are allowed.

FORBIDDEN_CODE_IDENTIFIERS = (
    "KBDLLHOOKSTRUCT",
    "MSLLHOOKSTRUCT",
    "vkCode",
    "scanCode",
    "ToUnicode",
    "ToUnicodeEx",
    "ToAscii",
    "ToAsciiEx",
    "GetKeyboardState",
    "GetKeyState",
    "GetAsyncKeyState",
    "GetKeyNameTextW",
    "MapVirtualKeyW",
    "MapVirtualKeyExW",
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _executable_tokens(source: str) -> set[str]:
    """Every identifier-ish token that executable code could use, ignoring
    comments (not in the AST) and docstrings."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.add(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.add(node.attr)
        elif isinstance(node, ast.alias):
            tokens.update(node.name.split("."))
            if node.asname:
                tokens.add(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            tokens.update(node.module.split("."))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            tokens.add(node.name)
        elif isinstance(node, ast.arg):
            tokens.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            tokens.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            tokens.update(t for t in FORBIDDEN_CODE_IDENTIFIERS if t in node.value)
    return tokens


def _forbidden_hits(source: str) -> set[str]:
    return set(FORBIDDEN_CODE_IDENTIFIERS) & _executable_tokens(source)


def test_no_keystroke_or_pointer_content_apis_in_executable_code():
    hits = []
    for path in _all_zaza_source_files():
        for name in sorted(_forbidden_hits(path.read_text(encoding="utf-8"))):
            hits.append(f"{path.name}: {name}")
    assert not hits, f"keystroke/pointer content APIs used in ZaZa code: {hits}"


@pytest.mark.parametrize(
    "snippet",
    [
        "class KBDLLHOOKSTRUCT(ctypes.Structure): pass",
        "data = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT))",
        "key = info.contents.vkCode",
        "code = info.contents.scanCode",
        "user32.ToUnicode(vk, sc, state, buf, 8, 0)",
        "user32.ToUnicodeEx(vk, sc, state, buf, 8, 0, layout)",
        "user32.GetKeyboardState(buf)",
        "user32.GetKeyState(0x10)",
        "fn = getattr(user32, 'GetAsyncKeyState')",
        "from ctypes.windll.user32 import GetAsyncKeyState",
    ],
)
def test_guard_catches_executable_use(snippet):
    assert _forbidden_hits(snippet), f"guard missed: {snippet}"


def test_guard_ignores_docstrings_and_comments():
    source = '''
"""Module docstring mentioning KBDLLHOOKSTRUCT and vkCode."""
# comment: never call GetAsyncKeyState or ToUnicode here


class Hooks:
    """We never read scanCode or MSLLHOOKSTRUCT."""

    def cb(self, wparam):
        """No GetKeyboardState / GetKeyState / ToUnicodeEx either."""
        return wparam
'''
    assert _forbidden_hits(source) == set()


def _lparam_uses_outside_call_next(func_source: str) -> list[str]:
    """Every load of ``lparam`` must be a direct argument to
    ``self._call_next(...)`` — i.e. forwarded opaquely, never inspected."""
    tree = ast.parse(textwrap.dedent(func_source))
    forwarded: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_call_next"
        ):
            forwarded.update(id(a) for a in node.args if isinstance(a, ast.Name))
    return [
        ast.dump(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "lparam"
        and isinstance(node.ctx, ast.Load)
        and id(node) not in forwarded
    ]


def test_hook_callbacks_only_forward_lparam():
    import inspect

    from deskmate.zaza.input_hooks import ZazaInputHooks

    for cb in (ZazaInputHooks._kb_callback, ZazaInputHooks._mouse_callback):
        assert _lparam_uses_outside_call_next(inspect.getsource(cb)) == [], cb.__name__


def test_lparam_guard_catches_inspection():
    bad = '''
def _kb_callback(self, ncode, wparam, lparam):
    info = ctypes.cast(lparam, PTR)
    return self._call_next(0, ncode, wparam, lparam)
'''
    assert _lparam_uses_outside_call_next(bad)


# ─── Phase 3: the central sync server is held to the same rules ────────────


def _server_source_files() -> list[Path]:
    import deskmate.zaza_server as server_pkg

    return sorted(Path(server_pkg.__file__).resolve().parent.rglob("*.py"))


def test_sync_server_has_no_capture_identifiers_or_content_apis():
    files = _server_source_files()
    assert len(files) >= 5
    hits = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        hits += [f"{path.name}: {i}" for i in FORBIDDEN_IDENTIFIERS if i in text]
        hits += [f"{path.name}: {i}" for i in sorted(_forbidden_hits(text))]
    assert not hits, hits


def test_sync_protocol_has_no_raw_event_or_content_record_types():
    from deskmate.zaza.sync.protocol import RECORD_TABLES

    assert set(RECORD_TABLES) == {"work_session", "activity_period", "idle_period", "app_usage_daily"}
    assert "activity_events" not in {table for table, _ in RECORD_TABLES.values()}
