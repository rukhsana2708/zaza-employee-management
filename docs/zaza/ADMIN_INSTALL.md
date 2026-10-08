# ZaZa Work Agent — administrator guide (Phase 9)

`ZaZaWorkAgentSetup.exe` installs the privacy-safe ZaZa employee agent on a
company Windows 10/11 (x64) PC. The central server and its HTTPS address
come in Phase 10; until then, test against a local development server only.

## 1. Build

On a Windows build machine (see §9 for versions), from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

The script:
1. checks Windows;
2. creates `build\venv-package` with only the agent's runtime dependencies
   plus PyInstaller (`installer\requirements-build.txt`, pinned);
3. runs the agent and installer test suites;
4. builds `dist\ZaZaWorkAgent\ZaZaWorkAgent.exe` (PyInstaller, one folder,
   no console window);
5. runs the production privacy audit (`installer\audit_package.py`) and
   stops if anything prohibited is packaged;
6. builds `dist\ZaZaWorkAgentSetup.exe` (Inno Setup);
7. writes `dist\SHA256SUMS.txt` and `dist\package-audit.txt`.

Options:
- `-Flavor development`: console window and debug logging. TLS checks are
  the same as production.
- `-SkipTests`: skip step 3.
- `-SignToolCommand '...'`: Authenticode-sign the executable and the
  installer with a real certificate (§8).

## 2. Verify the installer

```powershell
Get-FileHash dist\ZaZaWorkAgentSetup.exe -Algorithm SHA256
```

Compare the result with `dist\SHA256SUMS.txt`, and with the hash recorded
when the build was approved for the pilot.

## 3. Install

1. Double-click `ZaZaWorkAgentSetup.exe`.
2. Approve the administrator prompt. It is needed once, to install into
   `C:\Program Files\ZaZa Work Agent\` and create the startup task.
3. Go through the pages: Welcome → What is recorded → Server and device
   enrollment → Install → Finish.
4. On Finish, "Enroll this device and start ZaZa Work Agent" opens the
   enrollment window **as the signed-in employee**.

**Silent install** (for company-managed PCs):

```powershell
ZaZaWorkAgentSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

This installs the program and the startup task only. Enrollment stays a
separate step (§4); there is deliberately no token parameter.

## 4. Enroll a device

First register the device on the server, which prints the token once:

```powershell
python -m deskmate.zaza_server register-device --device-id <id> --employee-id <id>
```

Then on the PC, **as the employee**, either:
- **Interactive:** Start menu → ZaZa Work Agent → "Enroll or re-enroll this
  device". Enter the server address, device ID and token.
- **Provisioning from a protected file** (the token never appears on a
  command line):

  ```powershell
  Get-Content provision.json | & "C:\Program Files\ZaZa Work Agent\ZaZaWorkAgent.exe" --enroll-stdin
  ```

  `provision.json` contains `{"server_url": "https://…", "device_id": "…",
  "token": "…"}`. Delete it afterwards. Exit codes: 0 enrolled; 1 the server
  refused (wrong token, network or server problem); 2 invalid input.

The token is checked with `GET /api/v1/devices/me` before anything is saved.
The employee ID comes from the server.

## 5. Server address rules

- `https://` is required for every server.
- Plain `http://` is accepted only for `127.0.0.1`, `localhost` and `[::1]`,
  for local testing.
- TLS certificates are always verified (certifi CA bundle); there is no
  option to turn this off.

## 6. Check status

- **Start menu → "ZaZa Work Agent — Status & Privacy":** agent state, sync
  state, last successful sync, records waiting to upload, device ID, server
  host, monitoring health, and what is / isn't collected.
- **Task Manager:** `ZaZaWorkAgent.exe`, running as the employee and not
  elevated.
- **Task Scheduler:** `\ZaZa\ZaZa Work Agent`
  (`schtasks /Query /TN "ZaZa\ZaZa Work Agent" /V /FO LIST`).

## 7. How startup works

The task starts `ZaZaWorkAgent.exe --background` 30 seconds after any user
signs in:
- it runs in that user's interactive session, as that user, with least
  privilege; no password is stored;
- only one instance runs;
- after a crash it restarts every 5 minutes, at most 3 times.

No Windows service and no hidden persistence are used. The agent also guards
itself with a per-user lock file, `agent.lock`, so a second copy exits
immediately.

## 8. Upgrade, uninstall, local data

**Upgrade:** run a newer `ZaZaWorkAgentSetup.exe`. Setup stops the running
agents cleanly, so their work sessions are closed. The startup task is
replaced (never duplicated), and enrollment, configuration and the local
database (including unsynced records) are preserved.

**Uninstall:** Installed apps → ZaZa Work Agent → Uninstall. This stops the
agents and removes the startup task, the program files and the Start menu
entries. **Local data is preserved by default.**

**Local data**, per Windows user, is in `%LOCALAPPDATA%\ZaZa\WorkAgent\`:

| File | Contents |
|---|---|
| `activity.db` | activity database (SQLite, WAL) with the unsynced queue |
| `device_credentials.json` | device token, DPAPI-encrypted for that user |
| `config.json` | server address, device ID, employee ID — no secrets |
| `status.json` | status for the Status window |
| `logs\agent.log` | diagnostic log, rotated at 1 MB with 5 kept; no window titles, no tokens |

Data from earlier development builds in `%USERPROFILE%\.zaza_agent` is moved
there automatically on first start.

**Removing local data** permanently deletes any records not yet uploaded.
Run it as the employee, before uninstalling:

```powershell
& "C:\Program Files\ZaZa Work Agent\ZaZaWorkAgent.exe" --remove-local-data
```

It stops the agent, shows how many records are not uploaded yet, and asks
for confirmation.

**Reinstall:** the preserved enrollment is reused automatically. Use
"Enroll or re-enroll this device" to change it. Changing to a different
device ID asks for confirmation, and records of the old device are not
uploaded under the new one.

## 9. Code signing

Builds are **unsigned development builds** unless a real Authenticode
certificate is used, so Windows SmartScreen / Smart App Control may warn or
block. To sign:

```powershell
installer\build.ps1 -SignToolCommand '"C:\Program Files (x86)\Windows Kits\10\bin\x64\signtool.exe" sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f C:\secure\zaza.pfx /p $env:ZAZA_SIGN_PASSWORD $f'
```

Never commit certificates or passwords.

## 10. Build environment (tested)

| Item | Version |
|---|---|
| OS | Windows 11 Pro (x64) |
| Python | 3.14.x, 64-bit |
| PyInstaller | 6.22.x (pinned in `installer\requirements-build.txt`) |
| Inno Setup | 6.7.x (`ISCC.exe`; per-user install is fine) |
