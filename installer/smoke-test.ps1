<#
.SYNOPSIS
  Windows end-to-end smoke test of the FROZEN ZaZaWorkAgentSetup.exe — run ONLY
  in a disposable Windows VM / test account, never against the production VPS.

.DESCRIPTION
  Run elevated, inside the test account's interactive desktop session (the
  agent needs it for foreground-window, input-presence and lock monitoring).
  The recorder itself is always started NON-elevated through Task Scheduler.

  Uses a LOCAL development ZaZa server (SQLite backend, http://127.0.0.1) and:
   A  frozen exe before installation (--version, version resource)
   B  silent install: files, no python.exe, no upstream branding, Start menu,
      uninstall registration
   C  the real logon task: visible, logon trigger, exe + --background, Users
      group, least privilege, no stored password, bounded restarts, one task
   D  enrollment with --enroll-stdin (token only on stdin), DPAPI, no
      plaintext token anywhere in the data directory
   E  server URL / TLS rules with the frozen exe (bogus token only)
   F  start via the real task: one agent, this user, this session, not
      SYSTEM, not elevated; status; database; single instance
   G  online sync, offline queue, queue drain
   H  upgrade while offline with pending records: data, enrollment,
      credentials, one task, one agent, records then drain
   I  uninstall: agent stopped, task/files/shortcuts/registration removed,
      local data + credentials kept
   J  reinstall: enrollment and device identity reused, no duplicate task
   K  diagnostic-log privacy
   L  --remove-local-data --yes: agent stopped first, data + credentials gone
   M  final uninstall

.PARAMETER ServerPython
  A Python with the local server's dependencies (fastapi, uvicorn, pydantic,
  pydantic-settings, httpx, psutil, tzdata). Only the TEST SERVER uses it;
  the agent never does.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File installer\smoke-test.ps1
  powershell -ExecutionPolicy Bypass -File C:\zaza\repo\installer\smoke-test.ps1 -ServerPython C:\zaza\srvvenv\Scripts\python.exe
#>
[CmdletBinding()]
param(
    [string] $Setup = "",
    [string] $UpgradeSetup = "",
    [string] $ServerPython = "",
    [string] $ServerSource = "",
    [string] $Report = "",
    [int] $Port = 8799,
    [switch] $SkipInternetTlsChecks
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Setup) { $Setup = Join-Path $Root "dist\ZaZaWorkAgentSetup.exe" }
if (-not $UpgradeSetup) { $UpgradeSetup = $Setup }
if (-not $ServerPython) { $ServerPython = Join-Path $Root ".venv\Scripts\python.exe" }
if (-not $ServerSource) { $ServerSource = $Root }
if (-not $Report) { $Report = Join-Path $Root "dist\smoke-test-results.txt" }
$App = Join-Path $env:ProgramFiles "ZaZa Work Agent"
$Exe = Join-Path $App "ZaZaWorkAgent.exe"
$Data = Join-Path $env:LOCALAPPDATA "ZaZa\WorkAgent"
$Task = "ZaZa\ZaZa Work Agent"
$UninstallKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{9A4024E8-B137-431B-B89A-AA7301ACE1FE}_is1"
$StartMenu = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\ZaZa Work Agent"
$Work = Join-Path $env:TEMP "zaza-smoke"
$ServerDb = Join-Path $Work "server.db"
$Url = "http://127.0.0.1:$Port"
$Version = (Select-String -Path (Join-Path $Root "deskmate\zaza\__init__.py") -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$results = [System.Collections.Generic.List[string]]::new()
$script:server = $null
$script:token = $null
New-Item -ItemType Directory -Force $Work | Out-Null

Add-Type @"
using System; using System.Runtime.InteropServices;
public static class ZazaSmoke {
  [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
  [DllImport("advapi32.dll")] static extern bool OpenProcessToken(IntPtr h, uint access, out IntPtr token);
  [DllImport("advapi32.dll")] static extern bool GetTokenInformation(IntPtr t, int cls, out int value, int len, out int ret);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
  // 1 = elevated, 0 = not elevated, <0 = could not tell
  public static int Elevated(int pid) {
    IntPtr h = OpenProcess(0x1000, false, pid); if (h == IntPtr.Zero) return -1;
    IntPtr t; if (!OpenProcessToken(h, 0x8, out t)) { CloseHandle(h); return -2; }
    int v, r; bool ok = GetTokenInformation(t, 20, out v, 4, out r); CloseHandle(t); CloseHandle(h);
    return ok ? v : -3;
  }
  [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx, dy, data, flags, time; public IntPtr extra; }
  [StructLayout(LayoutKind.Sequential)] struct INPUT { public int type; public MOUSEINPUT mi; public long pad; }
  [DllImport("user32.dll")] static extern uint SendInput(uint n, INPUT[] inputs, int size);
  public static void Wiggle(int dx, int dy) {
    INPUT[] i = new INPUT[1]; i[0].type = 0; i[0].mi.dx = dx; i[0].mi.dy = dy; i[0].mi.flags = 1;
    SendInput(1, i, Marshal.SizeOf(typeof(INPUT)));
  }
}
"@

function Check($name, [scriptblock] $test) {
    try { $ok = [bool](& $test) } catch { $ok = $false; $name = "$name ($($_.Exception.Message))" }
    $line = "{0}  {1}" -f $(if ($ok) { "PASS" } else { "FAIL" }), $name
    $results.Add($line); Write-Host $line -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
}
function Info($text) { $results.Add("INFO  $text"); Write-Host "INFO  $text" -ForegroundColor DarkGray }
function Section($text) { $results.Add("== $text"); Write-Host "`n== $text" -ForegroundColor Cyan }
function WaitFor([scriptblock] $cond, [int] $timeout = 120) {
    $deadline = (Get-Date).AddSeconds($timeout)
    while ((Get-Date) -lt $deadline) { try { if (& $cond) { return $true } } catch { }; Start-Sleep 3 }
    return $false
}
function Agents { @(Get-Process -Name ZaZaWorkAgent -ErrorAction SilentlyContinue) }
function Recorders {  # only the background recorder, not the status/enrollment windows
    @(Get-CimInstance Win32_Process -Filter "Name='ZaZaWorkAgent.exe'" | Where-Object { $_.CommandLine -match "--background" })
}
function RecorderInfo {  # diagnostics for the single-instance checks
    $r = @(Recorders)
    "n=$($r.Count): " + (($r | ForEach-Object { "$($_.ProcessId)<-$($_.ParentProcessId) s$($_.SessionId) $($_.CreationDate.ToString('HH:mm:ss'))" }) -join "; ")
}
function ZaZaTasks {  # every scheduled task that runs the agent, wherever it is (except the VM test harness's own folder)
    @(Get-ScheduledTask | Where-Object { $_.TaskPath -ne "\ZaZaTest\" -and
        @($_.Actions | Where-Object { $_.Execute -match "ZaZaWorkAgent\.exe" }).Count -gt 0 })
}
function Status { Get-Content (Join-Path $Data "status.json") -Raw | ConvertFrom-Json }
function Fresh { $s = Status; ((Get-Date).ToUniversalTime() - ([datetime]$s.updated_at).ToUniversalTime()).TotalSeconds -lt 30 }
function ExeOut([string[]] $arguments, [string] $stdin = $null) {
    # The production exe has no console: capture its output through a pipe.
    if ($null -ne $stdin) { $out = $stdin | & $Exe @arguments | Out-String } else { $out = & $Exe @arguments | Out-String }
    return @{ code = $LASTEXITCODE; out = $out.Trim() }
}
function Server([string[]] $arguments) {
    $env:PYTHONPATH = $ServerSource
    & $ServerPython -m deskmate.zaza_server --db $ServerDb @arguments
}
function StartServer {
    $env:PYTHONPATH = $ServerSource
    $script:server = Start-Process $ServerPython -ArgumentList "-m", "deskmate.zaza_server", "--db", $ServerDb, "serve", "--port", $Port `
        -PassThru -WindowStyle Hidden -RedirectStandardError (Join-Path $Work "server.err")
    WaitFor { ServerUp } 30 | Out-Null
}
function ServerUp {  # Windows PowerShell 5.1: a 401 answer arrives as an exception
    try { Invoke-WebRequest "$Url/api/v1/devices/me" -UseBasicParsing -TimeoutSec 3 | Out-Null; return $true }
    catch { return ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) }
}
function StopServer { if ($script:server) { Stop-Process $script:server -Force -ErrorAction SilentlyContinue; $script:server = $null } }
function ServerRecords {
    $env:PYTHONPATH = $ServerSource
    [int](& $ServerPython -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from synced_records').fetchone()[0])" $ServerDb)
}
function StartAgent {
    # Exactly what happens at sign-in: the real logon task, run on demand.
    schtasks /Run /TN $Task | Out-Null
    WaitFor { @(Recorders).Count -ge 1 } 60 | Out-Null
}
function Wiggle([int] $seconds) { for ($i = 0; $i -lt $seconds; $i++) { [ZazaSmoke]::Wiggle(5 - 10 * ($i % 2), 3); Start-Sleep 1 } }
function Install([string] $file, [string] $log) {
    $p = Start-Process $file -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$log`"" -Wait -PassThru
    return $p.ExitCode
}
function Uninstall([string] $log) {
    $u = Get-ChildItem $App -Filter "unins*.exe" | Select-Object -First 1
    $p = Start-Process $u.FullName -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$log`"" -Wait -PassThru
    WaitFor { -not (Test-Path $Exe) } 30 | Out-Null  # the uninstaller finishes in a child process
    return $p.ExitCode
}
function TokenIn([string] $dir) {
    if (-not (Test-Path $dir)) { return $false }
    # agent.lock is held with a byte-range lock while the agent runs; it only holds the agent's PID.
    foreach ($f in Get-ChildItem $dir -Recurse -File | Where-Object Name -ne "agent.lock") {
        $fs = [IO.File]::Open($f.FullName, "Open", "Read", "ReadWrite, Delete")  # the agent keeps activity.db open
        try { $bytes = New-Object byte[] $fs.Length; [void]$fs.Read($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
        foreach ($enc in [Text.Encoding]::UTF8, [Text.Encoding]::Unicode) {
            if ($enc.GetString($bytes).Contains($script:token)) { return $true }
        }
    }
    return $false
}

# ── preconditions ────────────────────────────────────────────────────────────
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "run elevated, in a disposable test VM" }
if (-not (Test-Path $Setup)) { throw "installer not found: $Setup (run installer\build.ps1)" }
if (-not (Test-Path $ServerPython)) { throw "server Python not found: $ServerPython" }
if (Test-Path $Exe) { throw "ZaZa Work Agent is already installed here; use a fresh disposable VM" }
if (Test-Path $Data) { throw "$Data already exists; use a fresh test account" }
$session = (Get-Process -Id $PID).SessionId
Info "Windows $([Environment]::OSVersion.Version), user $env:USERNAME, session $session, setup $(Split-Path -Leaf $Setup) sha256 $((Get-FileHash $Setup).Hash.ToLower())"
Remove-Item $ServerDb -ErrorAction SilentlyContinue
Server @("add-employee", "--employee-id", "smoke-emp", "--name", "Smoke Test") | Out-Null
$reg = Server @("register-device", "--device-id", "smoke-pc", "--employee-id", "smoke-emp")
$script:token = ($reg | Select-String -Pattern "zzd_\S+").Matches[0].Value
$reg = $null

# ── A: the frozen exe before installation ───────────────────────────────────
Section "A  frozen executable (before installation)"
$portable = Join-Path $Work "portable"
Remove-Item -Recurse -Force $portable -ErrorAction SilentlyContinue
$setupDir = Split-Path -Parent $Setup
if (Test-Path (Join-Path $setupDir "ZaZaWorkAgent\ZaZaWorkAgent.exe")) {
    $bundleExe = Join-Path $setupDir "ZaZaWorkAgent\ZaZaWorkAgent.exe"
    $v = (& $bundleExe --version | Out-String).Trim()
    Check "dist exe --version prints 'ZaZa Work Agent $Version' (got '$v')" { $v -eq "ZaZa Work Agent $Version" }
} else { Info "dist\ZaZaWorkAgent not next to the setup; --version is checked after installation" }

# ── B: install ───────────────────────────────────────────────────────────────
Section "B  silent install"
$code = Install $Setup (Join-Path $Work "setup-install.log")
Check "setup exit code 0 (got $code)" { $code -eq 0 }
Check "ZaZaWorkAgent.exe installed in Program Files" { Test-Path $Exe }
$v = (ExeOut @("--version")).out
Check "installed exe --version prints 'ZaZa Work Agent $Version' (got '$v')" { $v -eq "ZaZa Work Agent $Version" }
$vi = (Get-Item $Exe).VersionInfo
Check "exe version resource: ProductName ZaZa Work Agent, CompanyName ZaZa" { $vi.ProductName -eq "ZaZa Work Agent" -and $vi.CompanyName -eq "ZaZa" }
Check "no python*.exe installed" { -not (Get-ChildItem $App -Recurse -Filter "python*.exe") }
Check "no DeskMate-branded file names installed" { -not (Get-ChildItem $App -Recurse | Where-Object Name -match "deskmate") }
Check "Start-menu shortcuts created" { Test-Path (Join-Path $StartMenu "ZaZa Work Agent — Status & Privacy.lnk") }
Check "uninstall registration: ZaZa Work Agent / ZaZa / $Version" {
    $k = Get-ItemProperty $UninstallKey; $k.DisplayName -eq "ZaZa Work Agent" -and $k.Publisher -eq "ZaZa" -and $k.DisplayVersion -eq $Version
}
Check "no data written under Program Files at install" { -not (Get-ChildItem $App -Recurse -Include "*.db", "config.json", "device_credentials.json") }

# ── C: the real Task Scheduler entry ────────────────────────────────────────
Section "C  Task Scheduler entry \$Task"
$t = Get-ScheduledTask -TaskPath "\ZaZa\" -TaskName "ZaZa Work Agent"
$xml = [xml](Export-ScheduledTask -TaskPath "\ZaZa\" -TaskName "ZaZa Work Agent")
Check "task visible (not hidden) and enabled" { -not $t.Settings.Hidden -and $t.Settings.Enabled }
Check "logon trigger" { @($t.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskLogonTrigger" }).Count -eq 1 }
Check "action: installed exe with --background" { $t.Actions[0].Execute.Trim('"') -eq $Exe -and $t.Actions[0].Arguments -eq "--background" }
Check "principal: Users group (S-1-5-32-545), least privilege" { $t.Principal.GroupId -in @("S-1-5-32-545", "Users") -and $t.Principal.RunLevel -eq "Limited" }
Check "no stored password (group principal, no UserId / Password logon)" { -not $t.Principal.UserId -and $t.Principal.LogonType -ne "Password" }
Check "bounded restart policy (3 x every 5 min), no time limit" {
    $t.Settings.RestartCount -eq 3 -and $t.Settings.RestartInterval -eq "PT5M" -and $t.Settings.ExecutionTimeLimit -eq "PT0S"
}
Check "exactly one task runs the agent: $((ZaZaTasks | ForEach-Object { $_.TaskPath + $_.TaskName }) -join ', ')" { @(ZaZaTasks).Count -eq 1 }
Info "task XML principal: $($xml.Task.Principals.Principal.OuterXml)"

# ── D: enrollment, DPAPI ─────────────────────────────────────────────────────
Section "D  enrollment (frozen exe, token on stdin only)"
StartServer
$json = @{ server_url = $Url; device_id = "smoke-pc"; token = $script:token } | ConvertTo-Json -Compress
$r = ExeOut @("--enroll-stdin") $json
$json = $null
Check "enrollment succeeded: '$($r.out)'" { $r.code -eq 0 -and $r.out -like "Connection successful*" }
Check "enrollment output does not contain the token" { -not $r.out.Contains($script:token) }
Check "config.json: server + device, no token" {
    $c = Get-Content (Join-Path $Data "config.json") -Raw; ($c | ConvertFrom-Json).device_id -eq "smoke-pc" -and -not $c.Contains($script:token)
}
Check "device_credentials.json protection = dpapi, token not plaintext" {
    $c = Get-Content (Join-Path $Data "device_credentials.json") -Raw; ($c | ConvertFrom-Json).protection -eq "dpapi" -and -not $c.Contains($script:token)
}

# ── E: server URL and TLS rules ──────────────────────────────────────────────
Section "E  server URL / TLS rules (frozen exe; bogus token, nothing saved)"
$fake = "zzd_not-a-real-token-0000000000"
function TryUrl([string] $u) { ExeOut @("--enroll-stdin") (@{ server_url = $u; device_id = "smoke-pc"; token = $fake } | ConvertTo-Json -Compress) }
$lan = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -First 1).IPAddress
foreach ($u in @("http://$($lan):$Port", "http://zaza.example.com", "https://user:pw@zaza.example.com", "https://zaza.example.com/?verify=false",
                 "https://zaza.example.com/#insecure", "ftp://zaza.example.com")) {
    $r = TryUrl $u
    Check "rejected before any connection: $u -> '$($r.out)'" { $r.code -eq 2 -and $r.out -like "Enrollment failed:*" }
}
foreach ($u in @("http://127.0.0.1:$Port", "http://localhost:$Port")) {
    $r = TryUrl $u
    Check "loopback http accepted (then the bogus token is refused): $u -> '$($r.out)'" { $r.code -eq 1 -and $r.out -like "Device token was not accepted*" }
}
$r = TryUrl "http://[::1]:$Port"  # the test server listens on IPv4 only: accepted, then unreachable
Check "loopback http://[::1] accepted by validation: '$($r.out)'" { $r.code -eq 1 -and $r.out -notlike "Enrollment failed*" }
$r = TryUrl "https://example-test-host.invalid"
Check "https address accepted by validation (unreachable host): '$($r.out)'" { $r.code -eq 1 -and $r.out -like "Could not reach the server*" }
if (-not $SkipInternetTlsChecks) {
    $r = TryUrl "https://example.com"
    Check "public CA verified by the frozen exe (TLS ok, not a ZaZa server): '$($r.out)'" { $r.code -eq 1 -and $r.out -notlike "Could not reach*" }
    foreach ($u in @("https://self-signed.badssl.com", "https://expired.badssl.com", "https://wrong.host.badssl.com")) {
        $r = TryUrl $u
        Check "invalid certificate refused: $u -> '$($r.out)'" { $r.code -eq 1 -and $r.out -like "Could not reach the server*" }
    }
} else { Info "internet TLS checks skipped" }
Check "enrollment unchanged by the refused attempts" { (Get-Content (Join-Path $Data "config.json") -Raw | ConvertFrom-Json).server_url -eq $Url }
$help = (ExeOut @("--help")).out
Check "no CLI option to pass a token or weaken TLS" { $help -notmatch "(?i)token|insecure|verify|tls|certificate|--url" }

# ── F: start through the real task ──────────────────────────────────────────
Section "F  agent start (real logon task, run on demand)"
StartAgent
$info = RecorderInfo
Check "exactly one recorder process ($info)" { @(Recorders).Count -eq 1 }
$rec = (Recorders)[0]
$owner = Invoke-CimMethod -InputObject $rec -MethodName GetOwner
Check "recorder runs as $env:USERNAME (not SYSTEM), in session $session" {
    $owner.User -eq $env:USERNAME -and $owner.User -ne "SYSTEM" -and $rec.SessionId -eq $session
}
Check "recorder is not elevated" { [ZazaSmoke]::Elevated([int]$rec.ProcessId) -eq 0 }
Check "status RUNNING or DEGRADED and fresh" { WaitFor { (Status).state -in @("RUNNING", "DEGRADED") -and (Fresh) } 60 }
$s = Status
Info "status: state=$($s.state) sync=$($s.sync_state) health=$(($s.health.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ',')"
Check "status shows device smoke-pc and host 127.0.0.1, no token" {
    $raw = Get-Content (Join-Path $Data "status.json") -Raw; $s.device_id -eq "smoke-pc" -and $s.server_host -eq "127.0.0.1" -and -not $raw.Contains($script:token)
}
Check "local database created" { Test-Path (Join-Path $Data "activity.db") }
Start-Process $Exe -ArgumentList "--background" -WindowStyle Hidden
Start-Sleep 8
$info = RecorderInfo
Check "a second recorder exits at once (single instance) ($info)" { @(Recorders).Count -eq 1 }
Info "agent.log: $((Get-Content (Join-Path $Data 'logs\agent.log') -Tail 6) -join ' / ')"

# ── G: sync, offline queue, drain ───────────────────────────────────────────
Section "G  online sync, offline queue, recovery"
Wiggle 20
Check "records synchronize while the server is up" { WaitFor { (ServerRecords) -gt 0 -and (Status).last_sync_at } 240 }
Check "sync state HEALTHY" { WaitFor { (Status).sync_state -eq "HEALTHY" } 120 }
$before = ServerRecords
StopServer
Wiggle 20
Check "server down: sync OFFLINE, recording continues, records pending locally" {
    WaitFor { (Status).sync_state -eq "OFFLINE" -and (Status).pending -gt 0 -and (Status).state -in @("RUNNING", "DEGRADED") } 300
}
$pendingOffline = (Status).pending
Info "pending while offline: $pendingOffline"
StartServer
Check "queue drains after the server returns" { WaitFor { (Status).sync_state -eq "HEALTHY" -and (ServerRecords) -gt $before } 300 }

# ── H: upgrade ───────────────────────────────────────────────────────────────
Section "H  upgrade (agent running, server offline, records pending)"
StopServer
Wiggle 10
WaitFor { (Status).sync_state -eq "OFFLINE" -and (Status).pending -gt 0 } 300 | Out-Null
$pendingBefore = (Status).pending
$dbBefore = (Get-Item (Join-Path $Data "activity.db")).Length
$credHash = (Get-FileHash (Join-Path $Data "device_credentials.json")).Hash
$cfgHash = (Get-FileHash (Join-Path $Data "config.json")).Hash
Info "before upgrade: pending=$pendingBefore db=$dbBefore bytes"
$code = Install $UpgradeSetup (Join-Path $Work "setup-upgrade.log")
$log = Get-Content (Join-Path $Work "setup-upgrade.log") -Raw
Check "upgrade exit code 0, no restart needed (got $code)" { $code -eq 0 -and $log -notmatch "(?i)restart(ing)? (is )?(needed|required)" }
Check "PrepareToInstall stopped the running agent (no failure logged)" { $log -notmatch "PrepareToInstall: " -and @(Recorders).Count -eq 0 }
Check "no files-in-use replacements scheduled" { $log -notmatch "(?i)in use|MoveFileEx|restart to replace" }
Check "activity.db preserved" { (Get-Item (Join-Path $Data "activity.db")).Length -ge $dbBefore }
Check "config.json preserved" { (Get-FileHash (Join-Path $Data "config.json")).Hash -eq $cfgHash }
Check "credentials preserved" { (Get-FileHash (Join-Path $Data "device_credentials.json")).Hash -eq $credHash }
Check "exactly one task runs the agent after upgrade" { @(ZaZaTasks).Count -eq 1 }
StartAgent
Start-Sleep 5
$info = RecorderInfo
Check "exactly one recorder after upgrade ($info)" { @(Recorders).Count -eq 1 }
Check "no re-enrollment needed; pending records still there" {
    WaitFor { (Status).device_id -eq "smoke-pc" -and (Status).pending -ge 1 -and (Fresh) } 60
}
Info "after upgrade: pending=$((Status).pending)"
$before = ServerRecords
StartServer
Check "DPAPI credentials still usable: queue drains after upgrade" { WaitFor { (Status).sync_state -eq "HEALTHY" -and (ServerRecords) -gt $before } 300 }

# ── I: uninstall ─────────────────────────────────────────────────────────────
Section "I  uninstall"
$code = Uninstall (Join-Path $Work "uninstall-1.log")
Check "uninstall exit code 0 (got $code)" { $code -eq 0 }
Check "agent stopped" { WaitFor { @(Agents).Count -eq 0 } 30 }
Check "logon task removed" { -not (Get-ScheduledTask -TaskPath "\ZaZa\" -ErrorAction SilentlyContinue) }
Check "program files removed" { -not (Test-Path $App) }
Check "Start-menu shortcuts removed" { -not (Test-Path $StartMenu) }
Check "uninstall registration removed" { -not (Test-Path $UninstallKey) }
Check "local data preserved by default (activity.db, config, credentials)" {
    (Test-Path (Join-Path $Data "activity.db")) -and (Test-Path (Join-Path $Data "config.json")) -and (Test-Path (Join-Path $Data "device_credentials.json"))
}

# ── J: reinstall ─────────────────────────────────────────────────────────────
Section "J  reinstall"
$code = Install $Setup (Join-Path $Work "setup-reinstall.log")
Check "reinstall exit code 0 (got $code)" { $code -eq 0 }
Check "exactly one task runs the agent after reinstall" { @(ZaZaTasks).Count -eq 1 }
StartAgent
Check "existing enrollment reused: same device, credentials work, RUNNING" {
    WaitFor { (Status).device_id -eq "smoke-pc" -and (Status).state -in @("RUNNING", "DEGRADED") -and (Fresh) } 60
}
$before = ServerRecords
Wiggle 10
Check "reinstalled agent syncs as the same device" { WaitFor { (ServerRecords) -gt $before -and (Status).sync_state -eq "HEALTHY" } 300 }
$env:PYTHONPATH = $ServerSource
$devices = [int](& $ServerPython -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from devices').fetchone()[0])" $ServerDb)
Check "no second device identity on the server (devices = 1)" { $devices -eq 1 }

# ── K: logs ──────────────────────────────────────────────────────────────────
Section "K  diagnostic logs"
$logs = Get-ChildItem (Join-Path $Data "logs") -File -ErrorAction SilentlyContinue
$text = ($logs | ForEach-Object { Get-Content $_.FullName -Raw }) -join "`n"
Check "logs exist" { $logs.Count -ge 1 }
Check "logs contain no token / bearer header" { -not $text.Contains($script:token) -and $text -notmatch "(?i)bearer\s+\S{8}|zzd_[A-Za-z0-9]{6}" }
Check "logs contain no URL paths / window titles / per-tick activity" { $text -notmatch "https?://\S+/\S|title=|APP_CHANGE|ACTIVITY " }
Check "logs bounded (rotating 1 MB x 5; total < 6.5 MB)" { ($logs | Measure-Object Length -Sum).Sum -lt 6.5MB }
Info "log files: $(($logs | ForEach-Object { "$($_.Name)=$($_.Length)" }) -join ', ')"
Check "token in no file of the data directory (db, logs, status, config)" { -not (TokenIn $Data) }

# ── L: explicit local-data removal ──────────────────────────────────────────
Section "L  --remove-local-data (administrator override --yes)"
$r = ExeOut @("--remove-local-data", "--yes")
Check "remove-local-data stopped the recorder first and reported the unsynced count: '$($r.out)'" {
    $r.code -eq 0 -and $r.out -match "unsynced record" -and @(Recorders).Count -eq 0
}
Check "local data and DPAPI credentials removed" { -not (Test-Path $Data) }

# ── M: final uninstall ──────────────────────────────────────────────────────
Section "M  final uninstall"
$code = Uninstall (Join-Path $Work "uninstall-2.log")
Check "final uninstall exit code 0, task and program removed" { $code -eq 0 -and -not (Test-Path $App) -and -not (Get-ScheduledTask -TaskPath "\ZaZa\" -ErrorAction SilentlyContinue) }
StopServer

$script:token = $null
$failed = @($results | Where-Object { $_ -like "FAIL*" }).Count
$passed = @($results | Where-Object { $_ -like "PASS*" }).Count
$results.Add("`n$passed passed, $failed failed")
$results | Set-Content -Encoding utf8 $Report
Write-Host "`n$passed passed, $failed failed  (report: $Report)"
exit [int]($failed -gt 0)
