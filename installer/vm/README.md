# Frozen-installer validation in a disposable Hyper-V VM

Phase 9 approval needs the **frozen** product (`ZaZaWorkAgent.exe`,
`ZaZaWorkAgentSetup.exe`) to be run, not just the Python tests. An unsigned
development build is blocked by Smart App Control on a normal PC, and the
build PC's security settings must not be changed for it. So the validation
runs in a throwaway VM. Never use the production VPS for it.

## Files

| File | Runs on | Purpose |
|---|---|---|
| `New-ZaZaTestVM.ps1` | host, elevated | Builds the VM from the Microsoft Windows 11 Enterprise Evaluation ISO with DISM (no interactive Windows Setup). Creates the local test accounts `zazaadmin` (administrator), `zazaemp` and `zazaemp2` (standard users) with random passwords in `<VmRoot>\vm-accounts.json`, outside Git. Turns Smart App Control off **inside the VM only**. |
| `ZaZaVM.psm1` | host | Helpers: PowerShell Direct into the guest, running programs in a signed-in user's interactive session (one-off scheduled task), console screenshots and keyboard/mouse input through Hyper-V WMI. |
| `Start-TestServer.ps1` | guest, elevated | Starts the local development ZaZa server (SQLite, `http://127.0.0.1:<port>`, loopback only) as a hidden task, so it survives sign-outs. Optionally registers a device and writes its token to an administrators-only file; the token is never printed. |
| `..\smoke-test.ps1` | guest, elevated, in the test account's desktop session | The scripted end-to-end test of the frozen installer (sections A–M). |

## Procedure

1. On the host, enable Hyper-V (one restart; use **Restart**, not Shut down,
   when Fast Startup is on). Download the Windows 11 Enterprise Evaluation ISO
   from the Microsoft Evaluation Center.
2. Run `New-ZaZaTestVM.ps1 -Iso <iso>` elevated, then take checkpoint `clean`.
3. Copy into the VM (`Copy-ToZaZaVM`): `dist\` from `installer\build.ps1`;
   `deskmate\`, `installer\` and `tests\` (needed only for the local test
   server and the smoke script); the Python installer; and an offline wheel
   folder for the server: fastapi, uvicorn, pydantic, pydantic-settings,
   httpx, psutil, tzdata.
4. **Before Python is present**, run `ZaZaWorkAgent.exe --version` and
   `--status` from `dist\ZaZaWorkAgent`. This proves the frozen exe needs no
   Python.
5. Install Python (for the test server only) into a venv, then run
   `smoke-test.ps1` elevated in `zazaadmin`'s session:
   `Start-ZaZaInteractive -User zazaadmin -Elevated -Wait -CommandLine "powershell -ExecutionPolicy Bypass -File ...\smoke-test.ps1 -ServerPython ... -ServerSource ..."`.
6. Interactive pass as the standard user `zazaemp` (autologon, VM restart),
   with screenshots of every step:
   1. Run the installer: UAC credential prompt, then Welcome, privacy,
      enrollment explanation, Ready, Finish.
   2. Enroll: try a rejected address and a wrong token first, then the real
      token. Check the Status & Privacy window.
   3. Sign out and sign in by keyboard. The logon task must start the agent
      as `zazaemp`, in their session, not elevated.
   4. Upgrade while records are pending.
   5. `--remove-local-data`: answer No (the agent must resume), then Yes.
   6. Uninstall, then reinstall: the existing enrollment must be reused.
7. Cross-user DPAPI: sign in `zazaemp2` and copy `zazaemp`'s
   `config.json` + `device_credentials.json` into `zazaemp2`'s data folder.
   The agent must stay `WAITING_FOR_ENROLLMENT` and send nothing.
8. Run the PostgreSQL-enabled suite in the VM, against a throwaway
   loopback PostgreSQL built from the EDB binaries.

Delete the VM afterwards (`Remove-VM`, then delete the VHDX). Nothing from it
is ever reused for the pilot.
