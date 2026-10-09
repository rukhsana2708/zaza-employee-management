<#
.SYNOPSIS
  Inside the disposable test VM (run elevated): start the LOCAL development
  ZaZa server (SQLite, http://127.0.0.1:<Port>, loopback only) as a hidden
  scheduled task, so it keeps running across sign-outs during the interactive
  installer pass. Optionally registers a device and writes its token to an
  administrators-only file (never printed).
#>
param(
    [int] $Port = 8800,
    [string] $Python = "C:\zaza\srvvenv\Scripts\python.exe",
    [string] $Source = "C:\zaza\src",
    [string] $Root = "C:\zaza\iserver",
    [string] $DeviceId = "",
    [string] $EmployeeId = "emp-1",
    [string] $RunAs = "zazaadmin",
    [string] $RunAsPassword = "",
    [switch] $Stop
)
$ErrorActionPreference = "Stop"
$task = "ZaZaTestServer$Port"
if ($Stop) { Stop-ScheduledTask -TaskPath "\ZaZaTest\" -TaskName $task -ErrorAction SilentlyContinue; return }
New-Item -ItemType Directory -Force $Root, "$Root\secret" | Out-Null
icacls "$Root\secret" /inheritance:r /grant:r "Administrators:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
$db = Join-Path $Root "server.db"
$env:PYTHONPATH = $Source
if ($DeviceId) {
    & $Python -m deskmate.zaza_server --db $db add-employee --employee-id $EmployeeId --name "Interactive Test Employee" 2>$null | Out-Null
    $out = & $Python -m deskmate.zaza_server --db $db register-device --device-id $DeviceId --employee-id $EmployeeId
    $token = ($out | Select-String -Pattern "zzd_\S+").Matches[0].Value
    Set-Content -Path "$Root\secret\$DeviceId.token" -Value $token -NoNewline -Encoding ascii
    $out = $null; $token = $null
    "device $DeviceId registered (token in $Root\secret, administrators only)"
}
$cmd = "/c set PYTHONPATH=$Source&& `"$Python`" -m deskmate.zaza_server --db `"$db`" serve --host 127.0.0.1 --port $Port > `"$Root\server-$Port.log`" 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries
Register-ScheduledTask -TaskPath "\ZaZaTest\" -TaskName $task -Action $action -Settings $settings -User $RunAs -Password $RunAsPassword -Force | Out-Null
Start-ScheduledTask -TaskPath "\ZaZaTest\" -TaskName $task
Start-Sleep 5
try { Invoke-WebRequest "http://127.0.0.1:$Port/api/v1/devices/me" -UseBasicParsing -TimeoutSec 5 | Out-Null; "unexpected 2xx" }
catch { "server on :$Port answered $([int]$_.Exception.Response.StatusCode)" }
