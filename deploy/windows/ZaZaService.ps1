#Requires -Version 5.1
<#
.SYNOPSIS
    Plan, install, start, stop or check the ZaZaEmployeeManagement Windows
    service (Phase 10). Uses the server's existing NSSM (C:\tools\nssm).

.DESCRIPTION
    -Action Plan     (default) print the exact service definition; change nothing
    -Action Install  register the service (NOT started), run it as the virtual
                     account NT SERVICE\ZaZaEmployeeManagement (no password),
                     and grant that account exactly what it needs:
                       read+execute  the ZaZa root (app, releases, venv, config)
                       modify        logs, run
                       read          secrets\db-app.password (and the Google key
                                     file named in the config, if any) — nothing
                                     else in secrets\, and never the owner or
                                     report role passwords
    -Action Start | Stop | Restart | Status
    -Action Remove   unregister the service (files are kept)

    Install refuses unless a release is active, the configuration passes
    `check-config --production`, the database schema is current and port
    8100 is free. Nothing here changes ZazaVoice, Caddy, PostgreSQL or the
    firewall.
#>
[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Install', 'Start', 'Stop', 'Restart', 'Status', 'Remove')][string]$Action = 'Plan'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'ZaZaProduction.psm1') -Force
$p = Get-ZaZaPaths
$svc = $p.ServiceName

$appParameters = "-E -s -u -m deskmate.zaza_server --env-file `"$($p.EnvFile)`" --backend postgres " +
                 "serve --host $($p.BindHost) --port $($p.Port)"
$settings = [ordered]@{
    Application          = $p.Python
    AppParameters        = $appParameters
    AppDirectory         = $p.App
    DisplayName          = 'ZaZa Employee Management'
    Description          = 'ZaZa central sync API and manager dashboard (127.0.0.1:8100, behind Caddy).'
    Start                = 'SERVICE_AUTO_START'
    AppStdout            = (Join-Path $p.Logs 'service-stdout.log')
    AppStderr            = (Join-Path $p.Logs 'service-stderr.log')
    AppRotateFiles       = '1'
    AppRotateOnline      = '1'
    AppRotateBytes       = '10485760'
    AppStopMethodConsole = '15000'
    AppExit              = 'Default Restart'
    AppRestartDelay      = '5000'
    AppThrottle          = '10000'
}

function Show-Plan {
    Write-Host "Service:  $svc   (account $($p.Account), no password)"
    foreach ($k in $settings.Keys) { Write-Host ("  {0,-21} {1}" -f $k, $settings[$k]) }
    Write-Host "Command:  `"$($p.Python)`" $appParameters"
    Write-Host "          (working directory $($p.App); one process, one worker)"
}

function Test-PortFree {
    $listener = Get-NetTCPConnection -LocalPort $p.Port -State Listen -ErrorAction SilentlyContinue
    return -not $listener
}

function Grant-ServiceAccess {
    $account = $p.Account
    Invoke-ZaZaNative -FilePath icacls.exe -ArgumentList @($p.Root, '/grant', "${account}:(OI)(CI)RX") | Out-Null
    foreach ($dir in $p.Logs, $p.Run) {
        Invoke-ZaZaNative -FilePath icacls.exe -ArgumentList @($dir, '/grant', "${account}:(OI)(CI)M") | Out-Null
    }
    # secrets\ does not inherit from the root: grant single files only
    $readable = @(Join-Path $p.Secrets 'db-app.password')
    $keyLine = Select-String -LiteralPath $p.EnvFile -Pattern '^\s*ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE\s*=\s*(.+?)\s*$'
    if ($keyLine) { $readable += $keyLine.Matches[0].Groups[1].Value.Trim('"', "'") }
    foreach ($file in $readable) {
        if (-not (Test-Path -LiteralPath $file)) { throw "missing secret file: $file" }
        Invoke-ZaZaNative -FilePath icacls.exe -ArgumentList @($file, '/grant', "${account}:R") | Out-Null
    }
}

switch ($Action) {
    'Plan' {
        Show-Plan
        $existing = Get-Service -Name $svc -ErrorAction SilentlyContinue
        Write-Host "Installed: $(if ($existing) { $existing.Status } else { 'no' })"
    }
    'Install' {
        Assert-ZaZaAdmin
        if (Get-Service -Name $svc -ErrorAction SilentlyContinue) { throw "$svc is already installed" }
        if (-not (Test-Path -LiteralPath $p.Nssm)) { throw "NSSM not found at $($p.Nssm)" }
        if (-not (Get-ZaZaActiveRelease)) { throw 'no active release (Deploy-ZaZaServer.ps1 -Activate)' }
        if (-not (Test-PortFree)) { throw "port $($p.Port) is already in use" }
        Invoke-ZaZaCli -Arguments @('check-config', '--production')
        Invoke-ZaZaCli -Arguments @('db-status')
        Show-Plan
        Invoke-ZaZaNative -FilePath $p.Nssm -ArgumentList @('install', $svc, $p.Python)
        foreach ($k in $settings.Keys) {
            if ($k -eq 'Application') { continue }
            $value = @($settings[$k] -split ' ')
            if ($k -in 'AppParameters', 'DisplayName', 'Description', 'AppDirectory', 'AppStdout', 'AppStderr') {
                $value = @($settings[$k])
            }
            Invoke-ZaZaNative -FilePath $p.Nssm -ArgumentList (@('set', $svc, $k) + $value)
        }
        # virtual service account: a per-service SID with no password to manage
        Invoke-ZaZaNative -FilePath sc.exe -ArgumentList @('config', $svc, 'obj=', $p.Account)
        Grant-ServiceAccess
        Write-Host "$svc installed (not started). Start it with -Action Start."
    }
    'Start' {
        Assert-ZaZaAdmin
        Start-Service -Name $svc
        & (Join-Path $PSScriptRoot 'Test-ZaZaServer.ps1') -Check Health -WaitSeconds 60
    }
    'Stop' { Assert-ZaZaAdmin; Stop-Service -Name $svc }
    'Restart' {
        Assert-ZaZaAdmin
        Restart-Service -Name $svc
        & (Join-Path $PSScriptRoot 'Test-ZaZaServer.ps1') -Check Health -WaitSeconds 60
    }
    'Status' {
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        Write-Host "Service: $(if ($s) { "$($s.Status) ($($s.StartType))" } else { 'not installed' })"
        Write-Host "Release: $(Get-ZaZaActiveRelease)"
        if ($s -and $s.Status -eq 'Running') { & (Join-Path $PSScriptRoot 'Test-ZaZaServer.ps1') -Check Health }
    }
    'Remove' {
        Assert-ZaZaAdmin
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        if (-not $s) { Write-Host "$svc is not installed."; return }
        if ($s.Status -ne 'Stopped') { Stop-Service -Name $svc }
        Invoke-ZaZaNative -FilePath $p.Nssm -ArgumentList @('remove', $svc, 'confirm')
        Write-Host "$svc removed; files under $($p.Root) are kept."
    }
}
