"""The employee-facing privacy statement, in one place (Status & Privacy
window, installer page and employee documentation say the same thing)."""

COLLECTS = (
    "The name of the application in use (for example, the program's process name)",
    "The title of the active window",
    "Website domain only (for example, example.com), never the full address - this version does not detect "
    "website domains yet",
    "Whether the keyboard and mouse were used (yes/no only - never which keys or what was typed)",
    "Active, idle, locked or unknown status, and Windows lock/unlock",
    "Work-session start and end times",
    "Connection status to the company's ZaZa server",
)

NEVER_COLLECTS = (
    "Screenshots or screen recordings",
    "What you type, or which keys you press",
    "Clipboard contents",
    "Microphone or webcam",
    "Full web addresses (URLs), browsing history or page contents",
    "Text read from the screen (OCR)",
)

ACTIVE_HOURS_NOTE = ("Active Hours are computer-activity metadata. They are not, on their own, proof of "
                     "productivity.")
