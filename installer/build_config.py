"""Shared build constants for the ZaZa Work Agent package (spec + audit).

The production package is built from ONE entry point
(``deskmate.zaza.workagent``) in a dedicated build environment that contains
only the agent's runtime dependencies, so the upstream DeskMate capture
libraries are not even installed there. On top of that, the PyInstaller
spec excludes every upstream DeskMate package and every capture library
explicitly, and :mod:`audit_package` inspects the finished artifact.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def product_version() -> str:
    text = (ROOT / "deskmate" / "zaza" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"', text)
    if not match:
        raise SystemExit("cannot read the version from deskmate/zaza/__init__.py")
    return match.group(1)


# ── first-party code: only these modules may be shipped ───────────────────
FIRST_PARTY_ALLOWED = ("deskmate", "deskmate.zaza")  # the package root (version only) and the agent

# Upstream DeskMate packages that exist in this repository and must never ship.
DESKMATE_PROHIBITED = (
    "deskmate.a11y",          # UI Automation tree / accessibility text, browser URL, clipboard, raw input hooks
    "deskmate.apps",          # upstream AI apps (recaps, email, journals)
    "deskmate.audio",         # microphone / system audio capture and transcription
    "deskmate.capture",       # screenshot / visual-change capture pipeline
    "deskmate.connections",   # Gmail / Outlook connectors
    "deskmate.core",          # upstream PII/incognito filters for screen text
    "deskmate.db",            # upstream screen-text database and search
    "deskmate.engine",        # upstream daemon, API, LLM, CLI
    "deskmate.fusion",
    "deskmate.habits",
    "deskmate.learning",
    "deskmate.learning_memory",
    "deskmate.mcp",           # MCP server
    "deskmate.meeting",       # meeting detection / summaries (audio)
    "deskmate.modelsvc",
    "deskmate.pipes",
    "deskmate.platform",
    "deskmate.redact",        # OCR/image redaction models
    "deskmate.screen",        # screenshots, OCR, video chunks
    "deskmate.ui",            # upstream web UI
    "deskmate.workflow",
    "deskmate.config", "deskmate.console", "deskmate.events", "deskmate.logger", "deskmate.paths",
    "deskmate.__main__", "deskmate.model_status",
    "deskmate.zaza_server",   # the central server (not part of the employee agent)
    "deskmate.zaza.__main__",  # the developer CLI (console output; the product uses workagent)
)

# Third-party libraries that provide capture/upstream features. None may ship.
THIRD_PARTY_PROHIBITED = (
    "mss", "PIL", "pytesseract", "rapidocr", "rapidocr_onnxruntime", "openvino", "openvino_genai", "cv2",
    "numpy", "onnxruntime", "tokenizers", "torch", "transformers", "faster_whisper", "ctranslate2",
    "sounddevice", "soundfile", "pyaudiowpatch", "pyaudio", "silero_vad", "pyannote", "modelscope",
    "uiautomation", "comtypes", "win32clipboard", "pyperclip", "keyboard", "pynput", "mouse",
    "winrt", "winsdk", "pythoncom", "pywintypes", "win32api", "win32gui", "win32con",
    "fastapi", "starlette", "uvicorn", "typer", "mcp", "fastembed", "yaml",
    "psycopg", "psycopg_pool", "sqlalchemy", "alembic", "jinja2", "argon2", "googleapiclient", "google",
)

# Third-party libraries the agent needs (everything else that is not the
# standard library fails the audit). Each is justified:
THIRD_PARTY_ALLOWED = {
    "pydantic": "sync protocol models (Phase 3)",
    "pydantic_core": "pydantic runtime",
    "annotated_types": "pydantic dependency",
    "typing_extensions": "pydantic dependency",
    "typing_inspection": "pydantic dependency",
    "httpx": "HTTPS sync client (Phase 3), TLS verification always on",
    "httpcore": "httpx dependency",
    "h11": "httpx dependency (HTTP/1.1)",
    "anyio": "httpx dependency",
    "sniffio": "httpx dependency",
    "idna": "httpx dependency (host names)",
    "certifi": "CA certificates for TLS verification",
    "psutil": "foreground process name and the agent's own process lookup",
}

# Standard-library modules allowed although they contain generic functions
# that could touch a prohibited resource — only if ZaZa code never calls them
# (the source scan below enforces that):
STDLIB_EXCEPTIONS = {
    "tkinter": "employee Status & Privacy / enrollment windows. tkinter has generic clipboard methods; ZaZa code "
               "never calls them (source scan forbids 'clipboard').",
    "ctypes": "Win32 calls for input-presence hooks, foreground window, lock/unlock and DPAPI; every Win32 name "
              "used is checked by the source scan.",
}

# Win32 / library calls that would implement a prohibited capture feature.
# Shipped ZaZa source must contain none of them.
PROHIBITED_SOURCE_PATTERNS = {
    r"\bGetClipboardData\b|\bOpenClipboard\b|\bSetClipboardData\b|\bclipboard_get\b|\bclipboard_append\b":
        "clipboard access",
    r"\bBitBlt\b|\bPrintWindow\b|\bGetDIBits\b|\bCreateCompatibleBitmap\b|\bImageGrab\b|\bgrab_screen\b":
        "screenshot / screen capture",
    r"\bimport\s+mss\b|\bpytesseract\b|\brapidocr\b|\bimage_to_string\b": "OCR / screenshot libraries",
    r"\bwaveInOpen\b|\bmciSendString\b|\bsounddevice\b|\bpyaudio\b|\bIAudioClient\b": "audio recording",
    r"\bcapCreateCaptureWindow\b|\bMediaCapture\b|\bcv2\b|\bVideoCapture\b": "webcam / video",
    r"\bGetKeyNameText\w*\b|\bToUnicode\w*\b|\bToAscii\w*\b|\bMapVirtualKey\w*\b|\bGetKeyboardState\b":
        "key identity / typed text",
    r"\.vkCode\b|\.scanCode\b": "reading which key was pressed",
    r"\bIUIAutomation\w*\b|\buiautomation\b|\bAccessibleObjectFromWindow\b|\bWM_GETTEXT\b":
        "accessibility-tree / window-content capture",
    r"\bbrowser_url\b|\bGetURL\b|\burl_path\b|\bquery_params\b": "browser URL / path capture",
    r"\bverify\s*=\s*False\b|\bCERT_NONE\b|\bcheck_hostname\s*=\s*False\b": "disabling TLS verification",
}


def is_under(name: str, prefixes) -> bool:  # noqa: ANN001
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def stdlib_names() -> frozenset[str]:
    return frozenset(sys.stdlib_module_names) | {"__main__", "_tkinter", "pyimod01_archive", "pyimod02_importers",
                                                  "pyimod03_ctypes", "pyimod04_pywin32", "pyi_rth_inspect",
                                                  "pyiboot01_bootstrap"}


EXCLUDES = list(DESKMATE_PROHIBITED + THIRD_PARTY_PROHIBITED) + [
    # stdlib parts the agent never uses
    "unittest", "pydoc", "pydoc_data", "doctest", "lib2to3", "idlelib", "turtle", "turtledemo",
    "test", "xmlrpc", "ensurepip", "venv",
    # build/test tooling reachable only through optional plugin imports
    # (e.g. anyio's pytest plugin); never needed at runtime
    "pytest", "_pytest", "pluggy", "iniconfig", "pygments", "packaging", "setuptools", "pkg_resources",
    "_distutils_hack", "anyio.pytest_plugin",
]


def version_file_text(version: str) -> str:
    parts = [int(p) for p in version.split(".")] + [0]
    tup = tuple(parts[:4])
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tup}, prodvers={tup}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'ZaZa'),
      StringStruct('FileDescription', 'ZaZa Work Agent'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'ZaZaWorkAgent'),
      StringStruct('LegalCopyright', 'ZaZa'),
      StringStruct('OriginalFilename', 'ZaZaWorkAgent.exe'),
      StringStruct('ProductName', 'ZaZa Work Agent'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
