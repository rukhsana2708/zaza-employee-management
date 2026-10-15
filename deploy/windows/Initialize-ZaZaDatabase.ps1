#Requires -Version 5.1
<#
.SYNOPSIS
    Create the ZaZa database and roles, run the migrations and grant
    privileges (Phase 10C). PREPARED IN PHASE 10B — NOT RUN YET.

.DESCRIPTION
    Steps (in this order; -Step selects one, default All):

    Credentials     zaza_db_credentials.py writes secrets\db-{owner,app,report}.password
                    and .scram (random 40-character passwords; nothing printed;
                    existing files are never overwritten)
    CreateDatabase  psql as postgres runs deploy\postgres\01-create-database-and-roles.sql.
                    psql asks for the postgres password itself (not stored, not
                    an argument); the SCRAM verifiers go to psql on stdin.
    Migrate         `migrate` as zaza_employee_owner (Alembic 0001..head)
    Grant           psql as zaza_employee_owner runs 02-grant-privileges.sql
    Cleanup         deletes the .scram verifier files (no longer needed)

    The owner's password reaches psql through the PGPASSWORD variable of the
    psql process only. Nothing here alters existing roles/databases,
    pg_hba.conf or postgresql.conf, or touches ZazaVoice.

    -DryRun prints the plan and checks prerequisites without changing anything.
#>
[CmdletBinding()]
param(
    [ValidateSet('All', 'Credentials', 'CreateDatabase', 'Migrate', 'Grant', 'Cleanup')][string]$Step = 'All',
    [string]$Psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe',
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'ZaZaProduction.psm1') -Force
Assert-ZaZaAdmin
$p = Get-ZaZaPaths
$sqlDir = (Resolve-Path (Join-Path $PSScriptRoot '..\postgres')).Path
$roles = 'owner', 'app', 'report'
$ownerEnv = @{ ZAZA_DB_USER = 'zaza_employee_owner'; ZAZA_DB_PASSWORD_FILE = (Join-Path $p.Secrets 'db-owner.password') }
$steps = if ($Step -eq 'All') { 'Credentials', 'CreateDatabase', 'Migrate', 'Grant', 'Cleanup' } else { @($Step) }

foreach ($required in $Psql, $p.BasePython, (Join-Path $sqlDir '01-create-database-and-roles.sql'),
                      (Join-Path $sqlDir '02-grant-privileges.sql')) {
    if (-not (Test-Path -LiteralPath $required)) { throw "missing: $required" }
}
if ($DryRun) {
    Write-Host "Steps: $($steps -join ' -> ')"
    Write-Host "Secrets: $($p.Secrets) (existing: $(@(Get-ChildItem -LiteralPath $p.Secrets -File).Name -join ', '))"
    Write-Host "Database: zaza_employee_management on 127.0.0.1:5432; roles zaza_employee_owner/app/report"
    Write-Host 'Dry run: nothing changed.'
    return
}

foreach ($s in $steps) {
    Write-Host "== $s"
    switch ($s) {
        'Credentials' {
            Invoke-ZaZaNative -FilePath $p.BasePython -ArgumentList @('-I', (Join-Path $PSScriptRoot 'zaza_db_credentials.py'),
                '--secrets-dir', $p.Secrets, 'owner', 'app', 'report')
        }
        'CreateDatabase' {
            $lines = foreach ($r in $roles) {
                $v = (Get-Content -LiteralPath (Join-Path $p.Secrets "db-$r.scram") -Raw).Trim()
                if ($v -notmatch '^SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$') {
                    throw "db-$r.scram is not a SCRAM verifier"
                }
                "\set ${r}_verifier '$v'"
            }
            $script = ($lines + "\i '$((Join-Path $sqlDir '01-create-database-and-roles.sql').Replace('\', '/'))'") -join "`n"
            # psql prompts for the postgres password on the console, not on stdin
            $script | & $Psql -X -h 127.0.0.1 -p 5432 -U postgres -d postgres -v ON_ERROR_STOP=1 -f -
            if ($LASTEXITCODE -ne 0) { throw "psql failed with exit code $LASTEXITCODE" }
        }
        'Migrate' {
            Invoke-ZaZaCli -Arguments @('migrate') -Environment $ownerEnv
            Invoke-ZaZaCli -Arguments @('db-status') -Environment $ownerEnv
        }
        'Grant' {
            $env:PGPASSWORD = (Get-Content -LiteralPath $ownerEnv.ZAZA_DB_PASSWORD_FILE -Raw).Trim()
            try {
                Invoke-ZaZaNative -FilePath $Psql -ArgumentList @('-X', '-h', '127.0.0.1', '-p', '5432',
                    '-U', 'zaza_employee_owner', '-d', 'zaza_employee_management', '-v', 'ON_ERROR_STOP=1',
                    '-f', (Join-Path $sqlDir '02-grant-privileges.sql'))
            } finally {
                Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
            }
        }
        'Cleanup' {
            foreach ($r in $roles) { Remove-Item -LiteralPath (Join-Path $p.Secrets "db-$r.scram") -ErrorAction SilentlyContinue }
            Write-Host 'Verifier files removed; password files kept (Administrators/SYSTEM only).'
        }
    }
}
