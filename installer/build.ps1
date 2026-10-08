<#
.SYNOPSIS
  Build ZaZaWorkAgent.exe and ZaZaWorkAgentSetup.exe (Windows only).

.DESCRIPTION
  1. verify Windows and tools          5. production privacy/package audit (fails the build)
  2. create the minimal build venv      6. build ZaZaWorkAgentSetup.exe (Inno Setup)
  3. run tests                          7. optional Authenticode signing (real certificate only)
  4. build ZaZaWorkAgent.exe            8. SHA-256 of the artifacts -> dist\SHA256SUMS.txt

  Artifacts: dist\ZaZaWorkAgent\ (program folder), dist\ZaZaWorkAgentSetup.exe,
  dist\package-audit.txt, dist\SHA256SUMS.txt

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File installer\build.ps1
  powershell -ExecutionPolicy Bypass -File installer\build.ps1 -Flavor development -SkipTests
#>
[CmdletBinding()]
param(
    [ValidateSet("production", "development")] [string] $Flavor = "production",
    [string] $Python = "",
    [string] $Iscc = "",
    [string] $TestPython = "",
    [switch] $SkipTests,
    [string] $SignToolCommand = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Fail($text) { Write-Host "BUILD FAILED: $text" -ForegroundColor Red; exit 1 }
function Run($exe, [string[]] $arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { Fail "$exe $($arguments -join ' ') exited with $LASTEXITCODE" }
}

$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Root "dist"
$BuildDir = Join-Path $Root "build"
$Venv = Join-Path $BuildDir "venv-package"
Set-Location $Root

# 1. environment ---------------------------------------------------------------
Step "1/8 Checking the build environment"
if ($env:OS -ne "Windows_NT") { Fail "the installer must be built on Windows" }
if (-not [Environment]::Is64BitOperatingSystem) { Fail "a 64-bit Windows build machine is required" }
if (-not $Python) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $Python = (& py -3.14 -c "import sys; print(sys.executable)") }
}
if (-not $Python -or -not (Test-Path $Python)) { Fail "Python 3.14 (64-bit) not found; pass -Python <path to python.exe>" }
$pyver = & $Python -c "import sys, platform; print(f'{sys.version_info[0]}.{sys.version_info[1]} {platform.architecture()[0]}')"
if ($pyver -notmatch "^3\.(1[2-9]) 64bit$") { Fail "64-bit Python 3.12+ required (found $pyver)" }
if (-not $Iscc) {
    $candidates = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                    "$env:ProgramFiles\Inno Setup 6\ISCC.exe")
    $Iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) { Fail "Inno Setup 6 (ISCC.exe) not found; install it (winget install JRSoftware.InnoSetup) or pass -Iscc" }
$Version = & $Python -c "import sys; sys.path.insert(0, 'installer'); import build_config; print(build_config.product_version())"
Write-Host "Windows: $([Environment]::OSVersion.VersionString)"
Write-Host "Python:  $Python ($pyver)"
Write-Host "ISCC:    $Iscc"
Write-Host "Product: ZaZa Work Agent $Version ($Flavor build)"

# 2. build venv ----------------------------------------------------------------
Step "2/8 Preparing the minimal build environment ($Venv)"
if (-not (Test-Path "$Venv\Scripts\python.exe")) { Run $Python @("-m", "venv", $Venv) }
$VPy = "$Venv\Scripts\python.exe"
Run $VPy @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", "installer\requirements-build.txt")
$extra = & $VPy -c "import importlib.util as u; bad=[m for m in ('mss','PIL','pytesseract','sounddevice','uiautomation','comtypes','fastapi','numpy','win32api') if u.find_spec(m)]; print(','.join(bad))"
if ($extra) { Fail "the build environment contains prohibited packages: $extra (delete $Venv and rebuild)" }

# 3. tests ---------------------------------------------------------------------
if (-not $SkipTests) {
    Step "3/8 Running tests"
    Run $VPy @("-m", "pytest", "tests\zaza_agent\test_installer.py", "-q", "-p", "no:cacheprovider")
    if (-not $TestPython -and (Test-Path "$Root\.venv\Scripts\python.exe")) { $TestPython = "$Root\.venv\Scripts\python.exe" }
    if ($TestPython) { Run $TestPython @("-m", "pytest", "tests\zaza_agent", "-q", "-p", "no:cacheprovider") }
    else { Write-Host "No development environment (.venv) found: only the installer tests ran." -ForegroundColor Yellow }
} else { Step "3/8 Tests skipped (-SkipTests)" }

# 4. executable ----------------------------------------------------------------
Step "4/8 Building ZaZaWorkAgent.exe (PyInstaller)"
Remove-Item -Recurse -Force "$Dist\ZaZaWorkAgent", "$BuildDir\pyinstaller" -ErrorAction SilentlyContinue
$env:ZAZA_BUILD_FLAVOR = $Flavor
Run $VPy @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", $Dist, "--workpath", "$BuildDir\pyinstaller",
           "installer\ZaZaWorkAgent.spec")
if (-not (Test-Path "$Dist\ZaZaWorkAgent\ZaZaWorkAgent.exe")) { Fail "ZaZaWorkAgent.exe was not produced" }

# 5. audit ---------------------------------------------------------------------
Step "5/8 Production package privacy audit"
Run $VPy @("installer\audit_package.py", "$Dist\ZaZaWorkAgent", "--report", "$Dist\package-audit.txt")

# 6/7. sign the exe (optional), build the installer ----------------------------
function Sign($file) {
    if (-not $SignToolCommand) { return }
    $cmd = $SignToolCommand.Replace('$f', "`"$file`"")
    cmd /c $cmd
    if ($LASTEXITCODE -ne 0) { Fail "signing failed for $file" }
}
if ($SignToolCommand) { Step "Signing ZaZaWorkAgent.exe"; Sign "$Dist\ZaZaWorkAgent\ZaZaWorkAgent.exe" }
else { Write-Host "Unsigned development build - Windows SmartScreen may display a warning." -ForegroundColor Yellow }

Step "6/8 Building ZaZaWorkAgentSetup.exe (Inno Setup)"
Remove-Item -Force "$Dist\ZaZaWorkAgentSetup.exe" -ErrorAction SilentlyContinue
Run $Iscc @("/Q", "/DAppVersion=$Version", "/DSourceDir=$Dist\ZaZaWorkAgent", "/DOutputDir=$Dist", "installer\ZaZaWorkAgent.iss")
if (-not (Test-Path "$Dist\ZaZaWorkAgentSetup.exe")) { Fail "ZaZaWorkAgentSetup.exe was not produced" }
Step "7/8 Signing the installer"
Sign "$Dist\ZaZaWorkAgentSetup.exe"

# 8. hashes --------------------------------------------------------------------
Step "8/8 SHA-256"
$files = @("$Dist\ZaZaWorkAgentSetup.exe", "$Dist\ZaZaWorkAgent\ZaZaWorkAgent.exe")
$lines = foreach ($f in $files) {
    $h = Get-FileHash $f -Algorithm SHA256
    "{0}  {1}" -f $h.Hash.ToLower(), (Resolve-Path -Relative $f)
}
$lines | Set-Content -Encoding ascii "$Dist\SHA256SUMS.txt"
$lines | ForEach-Object { Write-Host $_ }
$size = (Get-Item "$Dist\ZaZaWorkAgentSetup.exe").Length
Write-Host ("`nBuilt dist\ZaZaWorkAgentSetup.exe ({0:N1} MB), ZaZa Work Agent {1}, {2} build{3}." -f ($size / 1MB), $Version,
            $Flavor, $(if ($SignToolCommand) { ", signed" } else { ", UNSIGNED" })) -ForegroundColor Green
