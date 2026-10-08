ZaZa Work Agent - what it records and what it does not
=======================================================

ZaZa Work Agent is transparent workplace monitoring installed by your
organisation. It is always visible on this computer: in Installed apps, in the
Start menu ("ZaZa Work Agent - Status & Privacy"), in Task Manager
(ZaZaWorkAgent.exe) and in Task Scheduler (ZaZa\ZaZa Work Agent).

WHAT ZAZA WORK AGENT RECORDS
----------------------------
- The name of the application in use (the program's process name).
- The title of the active window.
- Website domain only (for example, example.com), never the full address.
  This version does not detect website domains yet.
- Whether the keyboard and mouse were used - yes or no only.
- Active, idle, locked or unknown status, and Windows lock/unlock.
  You count as active until 5 minutes without keyboard or mouse use; only
  the time after those 5 minutes counts as idle. When monitoring cannot be
  trusted (for example after a technical problem), the time is recorded as
  "unknown", never as idle.
- Work-session start and end times.
- Connection status to your organisation's ZaZa server.

WHAT IT DOES NOT RECORD
-----------------------
- Screenshots or screen video.
- What you type, or which keys you press.
- Clipboard contents.
- Microphone or webcam.
- Full web addresses (URLs), browsing history or page contents.
- Text read from the screen (OCR).

HOW THE DATA IS HANDLED
-----------------------
- It is saved on this computer first and uploaded to your organisation's
  ZaZa server over an encrypted (HTTPS) connection. If the server cannot be
  reached, recording continues and the records are uploaded later.
- The device token that identifies this computer is stored protected by
  Windows for your account only and is never shown.

WHAT "ACTIVE HOURS" MEANS
-------------------------
Active Hours are computer-activity metadata. They are not, on their own,
proof of productivity: work away from the computer (meetings, phone calls,
reading on paper) does not appear as computer activity.

Questions about how this information is used should go to your manager or
administrator.
