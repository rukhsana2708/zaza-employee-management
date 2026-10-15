# deploy/windows — ZaZa production server tooling (Phase 10)

The runbook is [docs/zaza/PRODUCTION_SERVER.md](../../docs/zaza/PRODUCTION_SERVER.md).

| File | Purpose |
|---|---|
| `server-files.txt` | git pathspecs of the files deployed to the server (server code only) |
| `requirements-server.txt` | server-only dependencies (bounded), derived from the server's imports |
| `requirements-server.lock.txt` | exact pins + SHA-256 hashes installed in production |
| `zaza-server.env.template` | production configuration, no secrets |
| `ZaZaProduction.psm1` | shared paths and helpers |
| `Deploy-ZaZaServer.ps1` | export + verify + (optionally) activate one exact commit |
| `Rollback-ZaZaServer.ps1` | switch back to an earlier release |
| `ZaZaService.ps1` | plan / install / start / stop / status of the NSSM service |
| `Test-ZaZaServer.ps1` | config, schema, port and health checks (read-only) |
| `Invoke-ZaZaJobs.ps1` | attendance recalculation, then Sheets refresh (locked, timed out, logged) |
| `Initialize-ZaZaDatabase.ps1` | credentials, database + roles, migrations, grants (Phase 10C) |
| `Install-ZaZaConfig.ps1` | install the configuration template |
| `zaza_db_credentials.py` | random role passwords + SCRAM verifiers, never printed |
| `../postgres/*.sql` | database/role creation and grants, for review |

## Regenerating the dependency lock

After changing `requirements-server.txt`, on the server:

```powershell
$tmp = "$env:TEMP\zaza-lock"; & 'C:\Program Files\Python314\python.exe' -m venv "$tmp\venv"
& "$tmp\venv\Scripts\python.exe" -m pip install --only-binary=:all: -r deploy\windows\requirements-server.txt
& "$tmp\venv\Scripts\python.exe" -m pip freeze > "$tmp\freeze.txt"
& "$tmp\venv\Scripts\python.exe" -m pip download --no-deps --only-binary=:all: -r "$tmp\freeze.txt" -d "$tmp\wheels"
```

Then write one `name==version` line per package, followed by
`--hash=sha256:<hash>` of its wheel (`Get-FileHash -Algorithm SHA256`).
Commit the result, and run `tests/zaza_agent/test_production_deploy.py`.
The tests fail if an employee-side package appears, if a pin lacks a hash,
or if a server import is not covered.
