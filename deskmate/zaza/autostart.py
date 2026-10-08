"""Transparent autostart: a Windows Task Scheduler logon task.

``\\ZaZa\\ZaZa Work Agent`` starts ``ZaZaWorkAgent.exe --background`` when
any user signs in, **inside that user's interactive session** (needed for
foreground-window, input-presence and lock/unlock monitoring — never a
Session 0 service), with the user's normal, non-elevated rights:

- principal: the built-in Users group (``S-1-5-32-545``), ``LeastPrivilege``;
  no stored password;
- 30 s logon delay; one instance (``IgnoreNew``); no time limit;
- after a crash, restarted every 5 minutes, at most 3 times (no tight loop).

It is visible in Task Scheduler, created by the installer (``/F`` replaces,
so upgrades never duplicate it) and deleted by the uninstaller.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from .edition import PRODUCT_NAME, TASK_NAME

USERS_GROUP_SID = "S-1-5-32-545"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def task_xml(exe: Path) -> str:
    exe = Path(exe)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>ZaZa</Author>
    <Description>Starts {escape(PRODUCT_NAME)} (transparent work-activity metadata recorder) when a user signs in. Created by the {escape(PRODUCT_NAME)} installer and removed by its uninstaller.</Description>
    <URI>\\{escape(TASK_NAME)}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Users">
      <GroupId>{USERS_GROUP_SID}</GroupId>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Users">
    <Exec>
      <Command>"{escape(str(exe))}"</Command>
      <Arguments>--background</Arguments>
      <WorkingDirectory>{escape(str(exe.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks.exe", *args], capture_output=True, text=True, creationflags=_NO_WINDOW,
                          check=False)


def register(exe: Path, work_dir: Path) -> None:
    """Create or replace the task (needs administrator rights: the installer)."""
    xml_file = work_dir / "zaza-autostart-task.xml"
    xml_file.write_text(task_xml(exe), encoding="utf-16")
    try:
        result = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(xml_file), "/F")
    finally:
        xml_file.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"could not create the startup task (schtasks exit {result.returncode})")


def unregister() -> bool:
    return _schtasks("/Delete", "/TN", TASK_NAME, "/F").returncode == 0


def exists() -> bool:
    return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0
