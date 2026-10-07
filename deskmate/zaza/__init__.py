"""ZaZa Employee Management — privacy-safe local activity agent.

This package is a clean, separated path derived from DeskMate (see
``deskmate/`` for the upstream project it is built from and the MIT license
that covers it). It intentionally does **not** import any of DeskMate's
screenshot, OCR, clipboard, or audio capture code.

Tracked: active application, active window title, keyboard/mouse activity
*presence* (boolean only), idle/active status, and Windows lock/unlock
events. Nothing else.

Never captured here: screenshots, screen/OCR text, typed characters or key
names, clipboard contents, audio, video. See ``SECURITY.md`` at the repo
root for the full policy this package implements.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.3.0"
