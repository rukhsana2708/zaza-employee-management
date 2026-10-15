#Requires -Version 5.1
<#
.SYNOPSIS
    Read-only production checks for the ZaZa server (Phase 10). Prints no secrets.

.DESCRIPTION
    -Check Config  `check-config --production` with the production configuration
    -Check Schema  `db-status` (connects to PostgreSQL; shows the schema revision)
    -Check Health  GET http://127.0.0.1:8100/api/v1/sync/health
    -Check Port    who listens on 8100 (must be the ZaZa service, loopback only)
    -Check All     all of the above

    Exit code 0 when every selected check passes, 1 otherwise. Nothing is
    changed; ZazaVoice and Caddy are not contacted.
#>
[CmdletBinding()]
param(
    [ValidateSet('Config', 'Schema', 'Health', 'Port', 'All')][string[]]$Check = @('All'),
    [int]$WaitSeconds = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'ZaZaProduction.psm1') -Force
$p = Get-ZaZaPaths
if ($Check -contains 'All') { $Check = @('Config', 'Schema', 'Port', 'Health') }
$failed = 0

function Result([bool]$ok, [string]$name, [string]$detail) {
    Write-Host ('[{0}] {1,-7} {2}' -f $(if ($ok) { 'OK  ' } else { 'FAIL' }), $name, $detail)
    if (-not $ok) { $script:failed++ }
}

foreach ($c in $Check) {
    switch ($c) {
        'Config' {
            try { Invoke-ZaZaCli -Arguments @('check-config', '--production'); Result $true 'config' 'production rules pass' }
            catch { Result $false 'config' $_.Exception.Message }
        }
        'Schema' {
            try { Invoke-ZaZaCli -Arguments @('db-status'); Result $true 'schema' 'see revision above' }
            catch { Result $false 'schema' $_.Exception.Message }
        }
        'Port' {
            $listeners = @(Get-NetTCPConnection -LocalPort $p.Port -State Listen -ErrorAction SilentlyContinue)
            if ($listeners.Count -eq 0) { Result $false 'port' "nothing listens on $($p.Port)"; break }
            $nonLoopback = @($listeners | Where-Object { $_.LocalAddress -notin '127.0.0.1', '::1' })
            $service = Get-CimInstance Win32_Service -Filter "Name='$($p.ServiceName)'" -ErrorAction SilentlyContinue
            $owners = @($listeners.OwningProcess | Sort-Object -Unique)
            $ours = $false
            if ($service -and $service.ProcessId) {
                $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($service.ProcessId)" |
                              ForEach-Object { $_.ProcessId })
                $tree = @($service.ProcessId) + $children
                $tree += @($children | ForEach-Object { Get-CimInstance Win32_Process -Filter "ParentProcessId=$_" } |
                           ForEach-Object { $_.ProcessId })
                $ours = @($owners | Where-Object { $tree -notcontains $_ }).Count -eq 0
            }
            Result ($nonLoopback.Count -eq 0) 'port' ("$($p.Port) on " + (($listeners.LocalAddress | Sort-Object -Unique) -join ', '))
            Result $ours 'owner' ("process(es) " + ($owners -join ', ') + $(if ($ours) { " belong to $($p.ServiceName)" } else { " are not $($p.ServiceName)" }))
        }
        'Health' {
            $url = "http://$($p.BindHost):$($p.Port)/api/v1/sync/health"
            $deadline = (Get-Date).AddSeconds($WaitSeconds)
            do {
                try {
                    $r = Invoke-RestMethod -Uri $url -TimeoutSec 5 -UseBasicParsing
                    if ($r.status -eq 'ok') { break }
                } catch { $r = $null }
                if ((Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
            } while ((Get-Date) -lt $deadline)
            Result ($null -ne $r -and $r.status -eq 'ok') 'health' $url
        }
    }
}
exit $(if ($failed) { 1 } else { 0 })
