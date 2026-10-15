#Requires -Version 5.1
<#
.SYNOPSIS
    Shared paths and helpers for the ZaZa production scripts (Phase 10).

.DESCRIPTION
    Everything lives under C:\apps\ZaZaEmployeeManagement. These helpers never
    print secret values: secret files are only checked for existence, and
    passwords are passed to child processes through files, not arguments.
    Nothing here touches ZazaVoice, Caddy, PostgreSQL configuration or any
    other service.
#>

Set-StrictMode -Version Latest

$script:Root = 'C:\apps\ZaZaEmployeeManagement'

function Get-ZaZaPaths {
    [pscustomobject]@{
        Root        = $script:Root
        App         = Join-Path $script:Root 'app'          # junction -> releases\<commit>
        Releases    = Join-Path $script:Root 'releases'
        Venv        = Join-Path $script:Root 'venv'
        Python      = Join-Path $script:Root 'venv\Scripts\python.exe'
        Config      = Join-Path $script:Root 'config'
        EnvFile     = Join-Path $script:Root 'config\zaza-server.env'
        Secrets     = Join-Path $script:Root 'secrets'
        Logs        = Join-Path $script:Root 'logs'
        Run         = Join-Path $script:Root 'run'
        Backup      = Join-Path $script:Root 'backup'
        History     = Join-Path $script:Root 'run\release-history.log'
        BasePython  = 'C:\Program Files\Python314\python.exe'
        Nssm        = 'C:\tools\nssm\nssm.exe'
        ServiceName = 'ZaZaEmployeeManagement'
        Account     = 'NT SERVICE\ZaZaEmployeeManagement'
        BindHost    = '127.0.0.1'
        Port        = 8100
    }
}

function Assert-ZaZaAdmin {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script from an elevated (Administrator) PowerShell.'
    }
}

function Invoke-ZaZaNative {
    <# Run a native command; throw on a non-zero exit code. Arguments are
       passed as an array, never through a shell string. #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [int[]]$AllowedExitCodes = @(0)
    )
    & $FilePath @ArgumentList
    if ($AllowedExitCodes -notcontains $LASTEXITCODE) {
        throw "$([IO.Path]::GetFileName($FilePath)) failed with exit code $LASTEXITCODE"
    }
}

function Invoke-ZaZaCli {
    <# Run `python -m deskmate.zaza_server` from the active release with the
       production configuration file. -Environment adds/overrides ZAZA_*
       variables for this one process only (e.g. another database role). #>
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [hashtable]$Environment = @{},
        [int[]]$AllowedExitCodes = @(0)
    )
    $p = Get-ZaZaPaths
    foreach ($required in @($p.Python, $p.EnvFile, (Join-Path $p.App 'deskmate\zaza_server\__main__.py'))) {
        if (-not (Test-Path -LiteralPath $required)) { throw "missing: $required" }
    }
    $saved = @{}
    foreach ($key in $Environment.Keys) {
        if ($key -notmatch '^ZAZA_[A-Z0-9_]+$') { throw "not a ZaZa setting: $key" }
        $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, [string]$Environment[$key], 'Process')
    }
    Push-Location -LiteralPath $p.App
    try {
        # -E: ignore PYTHON* variables, -s: no user site-packages, -u: unbuffered.
        # --env-file never overrides a variable already set in the process,
        # so -Environment (e.g. the migration or report role) wins for this
        # one command.
        Invoke-ZaZaNative -FilePath $p.Python -AllowedExitCodes $AllowedExitCodes -ArgumentList (
            @('-E', '-s', '-u', '-m', 'deskmate.zaza_server', '--env-file', $p.EnvFile) + $Arguments)
    } finally {
        Pop-Location
        foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process') }
    }
}

function Get-ZaZaActiveRelease {
    $p = Get-ZaZaPaths
    $item = Get-Item -LiteralPath $p.App -ErrorAction SilentlyContinue
    if ($null -eq $item -or -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $null }
    $target = @($item.Target)[0]
    if (-not $target) { return $null }
    return (Split-Path -Leaf $target)
}

function Set-ZaZaActiveRelease {
    param([Parameter(Mandatory)][string]$Release)
    $p = Get-ZaZaPaths
    $target = Join-Path $p.Releases $Release
    if (-not (Test-Path -LiteralPath (Join-Path $target 'DEPLOYED_COMMIT.json'))) {
        throw "not a verified release: $target"
    }
    $item = Get-Item -LiteralPath $p.App -ErrorAction SilentlyContinue
    if ($null -ne $item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            # removes the junction only, never the release it points to
            [IO.Directory]::Delete($p.App, $false)
        } elseif (@(Get-ChildItem -LiteralPath $p.App -Force).Count -eq 0) {
            Remove-Item -LiteralPath $p.App
        } else {
            throw "$($p.App) is a non-empty real directory; refusing to replace it"
        }
    }
    New-Item -ItemType Junction -Path $p.App -Target $target | Out-Null
    Add-Content -LiteralPath $p.History -Encoding utf8 -Value (
        '{0:u} activated {1} by {2}' -f (Get-Date).ToUniversalTime(), $Release, [Environment]::UserName)
}

function Get-ZaZaMigrationHead {
    <# Newest revision id among a release's migration files (0001_..., 0002_...). #>
    param([Parameter(Mandatory)][string]$ReleasePath)
    $dir = Join-Path $ReleasePath 'deskmate\zaza_server\postgres\migrations\versions'
    $names = @(Get-ChildItem -LiteralPath $dir -Filter '*.py' | Where-Object Name -Match '^\d{4}_' |
               Sort-Object Name | ForEach-Object { $_.Name.Substring(0, 4) })
    if ($names.Count -eq 0) { return $null }
    return $names[-1]
}

function Limit-ZaZaLogFiles {
    <# Keep at most -Keep files matching -Filter in the logs directory
       (newest first). Used for NSSM's rotated stdout/stderr files and job
       logs, which have no built-in count limit. #>
    param([Parameter(Mandatory)][string]$Filter, [int]$Keep = 5)
    $p = Get-ZaZaPaths
    Get-ChildItem -LiteralPath $p.Logs -Filter $Filter -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -Skip $Keep |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName }
}

Export-ModuleMember -Function Get-ZaZaPaths, Assert-ZaZaAdmin, Invoke-ZaZaNative, Invoke-ZaZaCli,
    Get-ZaZaActiveRelease, Set-ZaZaActiveRelease, Get-ZaZaMigrationHead, Limit-ZaZaLogFiles
