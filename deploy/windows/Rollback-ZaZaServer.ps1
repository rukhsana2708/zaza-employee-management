#Requires -Version 5.1
<#
.SYNOPSIS
    Point the ZaZa `app` junction back at an earlier deployed release (Phase 10).

.DESCRIPTION
    Without -Release, the release that was active before the current one
    (from run\release-history.log). Code only: the database is not touched.

    The server refuses to start when the database schema is not exactly the
    release's migration head, so rolling back across a migration needs a
    database restore first. This script compares the two releases' newest
    migration and refuses in that case unless -AllowSchemaMismatch.

    With -Restart the service is restarted afterwards (if it is installed).

.EXAMPLE
    .\Rollback-ZaZaServer.ps1 -DryRun
    .\Rollback-ZaZaServer.ps1 -Restart
    .\Rollback-ZaZaServer.ps1 -Release 305b7632a1b7a72b05eba222adb866a998466174 -Restart
#>
[CmdletBinding()]
param(
    [ValidatePattern('^[0-9a-f]{40}$')][string]$Release,
    [switch]$AllowSchemaMismatch,
    [switch]$Restart,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'ZaZaProduction.psm1') -Force
Assert-ZaZaAdmin
$p = Get-ZaZaPaths

$current = Get-ZaZaActiveRelease
if (-not $Release) {
    if (-not (Test-Path -LiteralPath $p.History)) { throw 'no release history; pass -Release' }
    $activated = @(Get-Content -LiteralPath $p.History | ForEach-Object {
        if ($_ -match ' activated ([0-9a-f]{40}) ') { $Matches[1] } })
    $earlier = @($activated | Where-Object { $_ -ne $current })
    if ($earlier.Count -eq 0) { throw 'no earlier release in the history; pass -Release' }
    $Release = $earlier[-1]
}
if ($Release -eq $current) { throw "$Release is already active" }
$target = Join-Path $p.Releases $Release
if (-not (Test-Path -LiteralPath (Join-Path $target 'DEPLOYED_COMMIT.json'))) { throw "not a deployed release: $target" }

$fromHead = if ($current) { Get-ZaZaMigrationHead -ReleasePath (Join-Path $p.Releases $current) } else { $null }
$toHead = Get-ZaZaMigrationHead -ReleasePath $target
Write-Host "Current release: $(if ($current) { $current } else { 'none' }) (migrations up to $fromHead)"
Write-Host "Roll back to:    $Release (migrations up to $toHead)"
if ($fromHead -and $toHead -ne $fromHead -and -not $AllowSchemaMismatch) {
    throw ('the releases need different database schemas; the older code will refuse to start until the ' +
           'database is restored to its schema. Restore the pre-migration backup first, then re-run with ' +
           '-AllowSchemaMismatch.')
}
if ($DryRun) { Write-Host 'Dry run: nothing changed.'; return }

Set-ZaZaActiveRelease -Release $Release
Write-Host "Active release: $Release"
if ($Restart) {
    if (Get-Service -Name $p.ServiceName -ErrorAction SilentlyContinue) {
        & (Join-Path $PSScriptRoot 'ZaZaService.ps1') -Action Restart
    } else {
        Write-Warning "$($p.ServiceName) is not installed; nothing to restart."
    }
}
