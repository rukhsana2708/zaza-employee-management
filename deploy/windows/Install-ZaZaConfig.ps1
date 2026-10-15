#Requires -Version 5.1
<#
.SYNOPSIS
    Install the production configuration file from the repository template
    (Phase 10): config\zaza-server.env. Contains no secrets.

.DESCRIPTION
    Copies deploy\windows\zaza-server.env.template to
    C:\apps\ZaZaEmployeeManagement\config\zaza-server.env if it does not exist.
    An existing file is never overwritten unless -Replace, which first saves
    it to backup\zaza-server.env.<timestamp>. Then runs `check-config
    --production` (if a release is active) so <PENDING> values are listed.
#>
[CmdletBinding()]
param([switch]$Replace)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'ZaZaProduction.psm1') -Force
Assert-ZaZaAdmin
$p = Get-ZaZaPaths
$template = Join-Path $PSScriptRoot 'zaza-server.env.template'

if (Test-Path -LiteralPath $p.EnvFile) {
    if (-not $Replace) { Write-Host "$($p.EnvFile) exists; not changed (use -Replace)." }
    else {
        $saved = Join-Path $p.Backup ('zaza-server.env.{0:yyyyMMddHHmmss}' -f (Get-Date))
        Copy-Item -LiteralPath $p.EnvFile -Destination $saved
        Copy-Item -LiteralPath $template -Destination $p.EnvFile -Force
        Write-Host "Replaced $($p.EnvFile) (previous saved to $saved)."
    }
} else {
    Copy-Item -LiteralPath $template -Destination $p.EnvFile
    Write-Host "Installed $($p.EnvFile)."
}
if (Get-ZaZaActiveRelease) {
    Invoke-ZaZaCli -Arguments @('check-config', '--production') -AllowedExitCodes @(0, 2)
}
