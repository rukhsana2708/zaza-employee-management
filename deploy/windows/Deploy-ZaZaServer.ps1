#Requires -Version 5.1
<#
.SYNOPSIS
    Deploy one exact Git commit of the ZaZa server to
    C:\apps\ZaZaEmployeeManagement\releases\<commit> (Phase 10).

.DESCRIPTION
    1. Resolves -Commit (a full 40-character SHA) in the repository. The
       working tree is never used: files come from `git archive` of that
       commit, limited to deploy/windows/server-files.txt AS STORED IN THAT
       COMMIT. .git, .env files, secrets, caches, archives and untracked
       files therefore cannot be deployed.
    2. Extracts into a staging folder, refuses forbidden file types, writes
       DEPLOYED_COMMIT.json (commit, time, operator, SHA-256 of every file)
       and the commit's hashed dependency lock, then renames the folder to
       releases\<commit>.
    3. -InstallDependencies: installs that lock into the venv
       (--require-hashes, wheels only, no dependency resolution).
    4. Import check with the production venv.
    5. -Activate: points the `app` junction at the new release (the previous
       one stays in releases\ for Rollback-ZaZaServer.ps1).

    It never starts, stops or registers a service, never touches the
    database, Caddy, ZazaVoice or the configuration/secrets directories.

.PARAMETER DryRun
    Show what would be deployed and run every check; write nothing.

.EXAMPLE
    .\Deploy-ZaZaServer.ps1 -Commit (git rev-parse HEAD) -DryRun
    .\Deploy-ZaZaServer.ps1 -Commit <sha> -InstallDependencies -Activate
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$Commit,
    [string]$Repository = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [switch]$InstallDependencies,
    [switch]$Activate,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'ZaZaProduction.psm1') -Force
Assert-ZaZaAdmin
$p = Get-ZaZaPaths

$Git = (Get-Command git.exe -ErrorAction Stop).Source
$Tar = Join-Path $env:SystemRoot 'System32\tar.exe'
# Names that must never appear in a release, whatever server-files.txt says.
$Forbidden = '(^|[\\/])(\.git[^\\/]*|\.env[^\\/]*|[^\\/]*\.env|__pycache__|\.claude[^\\/]*|secrets?)([\\/]|$)|\.(zip|pyc|pyo|json|pem|key|p12|pfx|db|sqlite3?|log|jsonl|output)$'

function Git { param([string[]]$GitArgs) Invoke-ZaZaNative -FilePath $Git -ArgumentList (@('-C', $Repository) + $GitArgs) }

# ── 1. the commit and its file list ─────────────────────────────────────────
$resolved = (Git @('rev-parse', '--verify', "$Commit^{commit}")) | Select-Object -First 1
if ($resolved -ne $Commit) { throw "commit $Commit not found in $Repository" }
$lines = @(Git @('show', "${Commit}:deploy/windows/server-files.txt") |
           ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
$specs = @($lines | Where-Object { -not $_.StartsWith('!') })
$excludes = @($lines | Where-Object { $_.StartsWith('!') } | ForEach-Object { $_.Substring(1) })
if ($specs.Count -eq 0) { throw 'deploy/windows/server-files.txt is empty in that commit' }
$files = @(Git (@('ls-tree', '-r', '--name-only', $Commit, '--') + $specs) |
           Where-Object { $path = $_; -not @($excludes | Where-Object { $path -like $_ }).Count })
$bad = @($files | Where-Object { $_ -match $Forbidden })
if ($bad.Count) { throw "forbidden files selected for deployment: $($bad -join ', ')" }
foreach ($required in 'deskmate/zaza_server/__main__.py', 'deskmate/zaza/sync/protocol.py',
                      'deskmate/zaza_server/postgres/migrations/env.py') {
    if ($files -notcontains $required) { throw "release would be incomplete: $required missing" }
}
$release = Join-Path $p.Releases $Commit
Write-Host "Commit:   $Commit"
Write-Host "Files:    $($files.Count) (from deploy/windows/server-files.txt in that commit)"
Write-Host "Release:  $release"
$onBranch = $true
& $Git -C $Repository merge-base --is-ancestor $Commit origin/zaza-development 2>$null
if ($LASTEXITCODE -ne 0) { $onBranch = $false; Write-Warning 'commit is not on origin/zaza-development' }

if ($DryRun) {
    $files | ForEach-Object { Write-Host "  $_" }
    Write-Host "`nDry run: nothing written."
    return
}
if (Test-Path -LiteralPath $release) { throw "release already exists: $release (activate it, or delete it first)" }

# ── 2. export, verify, record ───────────────────────────────────────────────
$staging = Join-Path $p.Releases ".staging-$Commit"
$archive = Join-Path $p.Releases ".staging-$Commit.tar"
if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging | Out-Null
try {
    # the exact file list (exclusions already applied), never a directory
    Git (@('archive', '--format=tar', "--output=$archive", $Commit, '--') + $files)
    Invoke-ZaZaNative -FilePath $Tar -ArgumentList @('-xf', $archive, '-C', $staging)
    Remove-Item -LiteralPath $archive

    $extracted = @(Get-ChildItem -LiteralPath $staging -Recurse -Force -File)
    $badExtracted = @($extracted | Where-Object { $_.FullName.Substring($staging.Length) -match $Forbidden })
    if ($badExtracted.Count) { throw "forbidden files in the export: $($badExtracted.FullName -join ', ')" }
    if ($extracted.Count -ne $files.Count) { throw "export has $($extracted.Count) files, expected $($files.Count)" }

    Git @('show', "${Commit}:deploy/windows/requirements-server.lock.txt") |
        Set-Content -LiteralPath (Join-Path $staging 'requirements-server.lock.txt') -Encoding ascii
    $manifest = [ordered]@{
        commit        = $Commit
        on_branch     = if ($onBranch) { 'origin/zaza-development' } else { $null }
        deployed_at   = (Get-Date).ToUniversalTime().ToString('o')
        deployed_by   = "$env:USERDOMAIN\$env:USERNAME"
        python        = $p.BasePython
        migration_head = (Get-ZaZaMigrationHead -ReleasePath $staging)
        files         = [ordered]@{}
    }
    foreach ($f in ($extracted | Sort-Object FullName)) {
        $rel = $f.FullName.Substring($staging.Length + 1).Replace('\', '/')
        $manifest.files[$rel] = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLower()
    }
    $manifest | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath (Join-Path $staging 'DEPLOYED_COMMIT.json') -Encoding utf8
    Rename-Item -LiteralPath $staging -NewName $Commit
} catch {
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    throw
}
Write-Host "Exported and verified $($files.Count) files; manifest DEPLOYED_COMMIT.json written."

# ── 3. dependencies (exact hashed lock of this commit) ──────────────────────
if ($InstallDependencies) {
    $service = Get-Service -Name $p.ServiceName -ErrorAction SilentlyContinue
    if ($service -and $service.Status -ne 'Stopped') { throw "stop $($p.ServiceName) before changing the venv" }
    Invoke-ZaZaNative -FilePath $p.Python -ArgumentList @('-E', '-s', '-m', 'pip', 'install',
        '--disable-pip-version-check', '--no-input', '--require-hashes', '--only-binary=:all:', '--no-deps',
        '-r', (Join-Path $release 'requirements-server.lock.txt'))
    Invoke-ZaZaNative -FilePath $p.Python -ArgumentList @('-E', '-s', '-m', 'pip', 'check')
}

# ── 4. import check (the release with the production venv) ──────────────────
Push-Location -LiteralPath $release
try {
    Invoke-ZaZaNative -FilePath $p.Python -ArgumentList @('-E', '-s', '-c',
        'import deskmate.zaza_server.__main__, deskmate.zaza_server.app, deskmate.zaza_server.postgres.migrate, ' +
        'deskmate.zaza_server.dashboard.routes, deskmate.zaza_server.sheets.exporter, deskmate.zaza_server.http_security; ' +
        'print("import check OK")')
} finally { Pop-Location }

# ── 5. activate ─────────────────────────────────────────────────────────────
if ($Activate) {
    $previous = Get-ZaZaActiveRelease
    Set-ZaZaActiveRelease -Release $Commit
    Write-Host "Active release: $Commit (previous: $(if ($previous) { $previous } else { 'none' }))"
    Write-Host 'Restart the service (ZaZaService.ps1 -Action Restart) for the new code to take effect.'
} else {
    Write-Host 'Not activated (use -Activate).'
}
