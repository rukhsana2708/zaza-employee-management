#Requires -Version 5.1
<#
.SYNOPSIS
    Run the recurring ZaZa jobs (Phase 10): attendance recalculation, then
    the Google Sheets refresh. Prepared in Phase 10B; scheduled in 10C.

.DESCRIPTION
    -Job Attendance           recalculate --recent-days N (application role)
    -Job Sheets               sheets-sync (read-only report role)
    -Job AttendanceThenSheets (default) attendance; Sheets ONLY if it succeeded

    Overlap: the whole run holds an exclusive lock file (run\jobs.lock,
    FileShare.None). A second copy exits at once with code 75 (busy). The
    lock is released by Windows if the process dies. sheets-sync also takes
    its own PostgreSQL advisory lock.

    Timeout: each step is killed after -TimeoutMinutes (default 20).

    Log: logs\jobs.log, rotated at 5 MiB, 5 files kept. Command output is
    logged; the commands print no secrets.

    Database roles: the attendance step uses the production configuration
    (zaza_employee_app). The Sheets step runs with ZAZA_DB_USER /
    ZAZA_DB_PASSWORD_FILE overridden to the read-only zaza_employee_report
    role — it only reads PostgreSQL.

    Exit codes: 0 ok, 1 a step failed, 75 another run is active.
#>
[CmdletBinding()]
param(
    [ValidateSet('Attendance', 'Sheets', 'AttendanceThenSheets')][string]$Job = 'AttendanceThenSheets',
    [ValidateRange(1, 62)][int]$RecentDays = 7,
    [ValidateRange(1, 120)][int]$TimeoutMinutes = 20
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'ZaZaProduction.psm1') -Force
$p = Get-ZaZaPaths
$log = Join-Path $p.Logs 'jobs.log'
$reportEnv = @{
    ZAZA_DB_USER          = 'zaza_employee_report'
    ZAZA_DB_PASSWORD_FILE = (Join-Path $p.Secrets 'db-report.password')
}

function Write-JobLog([string]$text) {
    $line = '{0:u} {1}' -f (Get-Date).ToUniversalTime(), $text
    Write-Host $line
    Add-Content -LiteralPath $log -Value $line -Encoding utf8
}

function Step([string]$name, [string[]]$cliArgs, [hashtable]$environment) {
    Write-JobLog "start $name"
    $out = Join-Path $p.Run "job-$name.out"
    $saved = @{}
    foreach ($k in $environment.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        [Environment]::SetEnvironmentVariable($k, $environment[$k], 'Process')
    }
    try {
        $arguments = @('-E', '-s', '-u', '-m', 'deskmate.zaza_server', '--env-file', "`"$($p.EnvFile)`"") + $cliArgs
        $proc = Start-Process -FilePath $p.Python -ArgumentList $arguments -WorkingDirectory $p.App -NoNewWindow `
            -PassThru -RedirectStandardOutput $out -RedirectStandardError "$out.err"
        if (-not $proc.WaitForExit($TimeoutMinutes * 60 * 1000)) {
            # kill the whole tree (the venv launcher starts the real interpreter as a child)
            & taskkill.exe /PID $proc.Id /T /F | Out-Null
            Write-JobLog "FAILED ${name}: timed out after $TimeoutMinutes min"
            return $false
        }
        $proc.WaitForExit()
        foreach ($f in $out, "$out.err") {
            if (Test-Path -LiteralPath $f) { Get-Content -LiteralPath $f | ForEach-Object { Write-JobLog "  $_" } }
        }
        if ($proc.ExitCode -ne 0) { Write-JobLog "FAILED ${name}: exit code $($proc.ExitCode)"; return $false }
        Write-JobLog "done $name"
        return $true
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
        Remove-Item -LiteralPath $out, "$out.err" -ErrorAction SilentlyContinue
    }
}

# bounded log: 5 MiB per file, jobs.log + jobs.1.log .. jobs.4.log
if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 5MB) {
    for ($i = 4; $i -ge 1; $i--) {
        $from = if ($i -eq 1) { $log } else { Join-Path $p.Logs "jobs.$($i - 1).log" }
        if (Test-Path -LiteralPath $from) { Move-Item -LiteralPath $from -Destination (Join-Path $p.Logs "jobs.$i.log") -Force }
    }
}
# NSSM's rotated stdout/stderr files have no count limit: keep the newest 5 of each
Limit-ZaZaLogFiles -Filter 'service-stdout-*.log' -Keep 5
Limit-ZaZaLogFiles -Filter 'service-stderr-*.log' -Keep 5

$lock = $null
try {
    $lock = [IO.File]::Open((Join-Path $p.Run 'jobs.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
} catch [IO.IOException] {
    Write-JobLog "skipped ${Job}: another job run is active"
    exit 75
}
try {
    $ok = $true
    if ($Job -in 'Attendance', 'AttendanceThenSheets') {
        $ok = Step 'attendance' @('recalculate', '--recent-days', "$RecentDays") @{}
    }
    if ($Job -eq 'Sheets' -or ($Job -eq 'AttendanceThenSheets' -and $ok)) {
        $ok = (Step 'sheets' @('sheets-sync') $reportEnv) -and $ok
    } elseif ($Job -eq 'AttendanceThenSheets') {
        Write-JobLog 'sheets skipped: attendance did not succeed'
    }
    exit $(if ($ok) { 0 } else { 1 })
} finally {
    $lock.Dispose()
}
