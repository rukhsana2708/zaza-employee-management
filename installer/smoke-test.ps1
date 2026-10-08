<#
.SYNOPSIS
  Windows end-to-end smoke test of ZaZaWorkAgentSetup.exe — run ONLY in a
  disposable Windows VM / test account, as an administrator, never against the
  production VPS.

.DESCRIPTION
  Uses a LOCAL development ZaZa server (SQLite backend, http://127.0.0.1) and:
   1  installs silently              9  stops the server -> pending queue grows
   2  checks installed files         10 restarts the server -> queue drains
   3  checks the logon task          11 upgrades (re-runs the installer)
   4  registers a test device        12 checks data, credentials, task, one agent
   5  enrolls via --enroll-stdin     13 uninstalls silently
   6  starts the agent               14 checks task/program removed, data kept
   7  checks status.json / database
   8  generates activity (input simulation)

  Requires: the repository's development environment (.venv) for the local
  server, and a built dist\ZaZaWorkAgentSetup.exe (installer\build.ps1).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File installer\smoke-test.ps1
#>
[CmdletBinding()]
param(
    [string] $Setup = "",
    [string] $UpgradeSetup = "",
    [int] $Port = 8799
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Setup) { $Setup = Join-Path $Root "dist\ZaZaWorkAgentSetup.exe" }
if (-not $UpgradeSetup) { $UpgradeSetup = $Setup }
$DevPy = Join-Path $Root ".venv\Scripts\python.exe"
$App = Join-Path $env:ProgramFiles "ZaZa Work Agent"
$Exe = Join-Path $App "ZaZaWorkAgent.exe"
$Data = Join-Path $env:LOCALAPPDATA "ZaZa\WorkAgent"
$Task = "ZaZa\ZaZa Work Agent"
$ServerDb = Join-Path $env:TEMP "zaza-smoke-server.db"
$Url = "http://127.0.0.1:$Port"
$results = [System.Collections.Generic.List[string]]::new()

function Check($name, [scriptblock] $test) {
    try { if (& $test) { $results.Add("PASS  $name") } else { $results.Add("FAIL  $name") } }
    catch { $results.Add("FAIL  $name ($($_.Exception.Message))") }
}
function Agents { @(Get-Process -Name ZaZaWorkAgent -ErrorAction SilentlyContinue) }
function Status { Get-Content (Join-Path $Data "status.json") -Raw | ConvertFrom-Json }
function StartServer {
    $p = Start-Process $DevPy -ArgumentList "-m", "deskmate.zaza_server", "--db", $ServerDb, "serve", "--port", $Port `
        -PassThru -WindowStyle Hidden
    Start-Sleep 4
    return $p
}

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "run this smoke test as an administrator, in a disposable test VM"
}
if (-not (Test-Path $Setup)) { throw "installer not found: $Setup (run installer\build.ps1)" }

Remove-Item $ServerDb -ErrorAction SilentlyContinue
& $DevPy -m deskmate.zaza_server --db $ServerDb add-employee --employee-id smoke-emp --name "Smoke Test" | Out-Null
$reg = & $DevPy -m deskmate.zaza_server --db $ServerDb register-device --device-id smoke-pc --employee-id smoke-emp
$token = ($reg | Select-String -Pattern "zzd_\S+").Matches[0].Value

# 1-3 install ------------------------------------------------------------------
Start-Process $Setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
Check "installed executable exists" { Test-Path $Exe }
Check "no python.exe in the program folder" { -not (Get-ChildItem $App -Recurse -Filter "python*.exe") }
Check "no upstream branding in the program folder" { -not (Get-ChildItem $App -Recurse | Where-Object Name -match "deskmate") }
Check "logon task exists" { schtasks /Query /TN $Task | Out-Null; $LASTEXITCODE -eq 0 }
$xml = [xml](schtasks /Query /TN $Task /XML)
Check "task runs as Users group, least privilege, no password" {
    $xml.Task.Principals.Principal.GroupId -eq "S-1-5-32-545" -and $xml.Task.Principals.Principal.RunLevel -eq "LeastPrivilege"
}
Check "task has a bounded restart policy" { $xml.Task.Settings.RestartOnFailure.Count -eq "3" }

# 4-7 enroll and start ----------------------------------------------------------
$server = StartServer
$json = @{ server_url = $Url; device_id = "smoke-pc"; token = $token } | ConvertTo-Json -Compress
$json | & $Exe --enroll-stdin | Out-Null
Check "enrollment succeeded" { $LASTEXITCODE -eq 0 }
Check "token not in config.json" { -not (Select-String -Path (Join-Path $Data "config.json") -SimpleMatch $token -Quiet) }
Check "token stored DPAPI-protected" { (Get-Content (Join-Path $Data "device_credentials.json") -Raw | ConvertFrom-Json).protection -eq "dpapi" }
Start-Process $Exe -ArgumentList "--background"
Start-Sleep 20
Check "one agent process running" { (Agents).Count -eq 1 }
Check "status.json reports RUNNING or DEGRADED" { (Status).state -in @("RUNNING", "DEGRADED") }
Check "local database created" { Test-Path (Join-Path $Data "activity.db") }
Start-Process $Exe -ArgumentList "--background"; Start-Sleep 5
Check "second instance exits (single instance)" { (Agents).Count -eq 1 }

# 8-10 activity, offline queue, drain -------------------------------------------
Add-Type -AssemblyName System.Windows.Forms
for ($i = 0; $i -lt 20; $i++) { [System.Windows.Forms.Cursor]::Position = New-Object Drawing.Point((100 + $i * 5), 100); Start-Sleep 1 }
Start-Sleep 70
Check "records synced while the server is up" { (Status).sync_state -in @("HEALTHY", "BACKLOG") -and (Status).last_sync_at }
Stop-Process $server -Force
for ($i = 0; $i -lt 20; $i++) { [System.Windows.Forms.Cursor]::Position = New-Object Drawing.Point((300 + $i * 5), 200); Start-Sleep 1 }
Start-Sleep 90
Check "offline: sync state OFFLINE and records kept" { (Status).sync_state -eq "OFFLINE" -and (Status).pending -gt 0 }
$server = StartServer
Start-Sleep 120
Check "queue drains after the server returns" { (Status).sync_state -eq "HEALTHY" }

# 11-12 upgrade ----------------------------------------------------------------
$dbBefore = (Get-Item (Join-Path $Data "activity.db")).Length
Start-Process $UpgradeSetup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
Check "upgrade preserved the database" { (Get-Item (Join-Path $Data "activity.db")).Length -ge $dbBefore }
Check "upgrade preserved the enrollment" { (Get-Content (Join-Path $Data "config.json") -Raw | ConvertFrom-Json).device_id -eq "smoke-pc" }
Check "upgrade preserved the credentials" { Test-Path (Join-Path $Data "device_credentials.json") }
Check "upgrade left exactly one logon task" { @(schtasks /Query /FO CSV | Select-String -SimpleMatch "ZaZa Work Agent").Count -eq 1 }
Start-Process $Exe -ArgumentList "--background"; Start-Sleep 15
Check "one agent after upgrade" { (Agents).Count -eq 1 }

# 13-15 uninstall ----------------------------------------------------------------
$uninstaller = Get-ChildItem $App -Filter "unins*.exe" | Select-Object -First 1
Start-Process $uninstaller.FullName -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
Start-Sleep 5
Check "agent stopped by uninstall" { (Agents).Count -eq 0 }
Check "logon task removed" { schtasks /Query /TN $Task 2>$null | Out-Null; $LASTEXITCODE -ne 0 }
Check "program files removed" { -not (Test-Path $Exe) }
Check "local data preserved by default" { Test-Path (Join-Path $Data "activity.db") }
Stop-Process $server -Force -ErrorAction SilentlyContinue

$results | ForEach-Object { Write-Host $_ }
$failed = @($results | Where-Object { $_ -like "FAIL*" }).Count
Write-Host "`n$($results.Count - $failed) passed, $failed failed"
Write-Host "Local data was kept in $Data (delete it manually in this disposable VM)."
exit [int]($failed -gt 0)
