# Host-side helpers for driving the disposable Phase 9 test VM (Hyper-V):
# PowerShell Direct into the guest, interactive-session tasks, console
# screenshots and keyboard input through Hyper-V WMI. Test tooling only.

$script:VmName = "ZaZa-Phase9-Test"
$script:VmRoot = "D:\ZaZa-TestVM"

function Set-ZaZaVM([string] $Name, [string] $Root) { if ($Name) { $script:VmName = $Name }; if ($Root) { $script:VmRoot = $Root } }

function Get-ZaZaCred([string] $User) {
    $acc = Get-Content (Join-Path $script:VmRoot "vm-accounts.json") -Raw | ConvertFrom-Json
    $pw = ConvertTo-SecureString $acc.$User -AsPlainText -Force
    New-Object System.Management.Automation.PSCredential(".\$User", $pw)
}

function Invoke-ZaZaVM([scriptblock] $Script, [object[]] $ArgumentList = @(), [string] $User = "zazaadmin") {
    Invoke-Command -VMName $script:VmName -Credential (Get-ZaZaCred $User) -ScriptBlock $Script -ArgumentList $ArgumentList
}

function Copy-ToZaZaVM([string] $Source, [string] $Destination) {
    $s = New-PSSession -VMName $script:VmName -Credential (Get-ZaZaCred "zazaadmin")
    try {
        Invoke-Command -Session $s -ScriptBlock { param($d) New-Item -ItemType Directory -Force (Split-Path -Parent $d) | Out-Null } -ArgumentList $Destination
        Copy-Item -ToSession $s -Path $Source -Destination $Destination -Recurse -Force
    } finally { Remove-PSSession $s }
}

function Copy-FromZaZaVM([string] $Source, [string] $Destination) {
    $s = New-PSSession -VMName $script:VmName -Credential (Get-ZaZaCred "zazaadmin")
    try { Copy-Item -FromSession $s -Path $Source -Destination $Destination -Recurse -Force } finally { Remove-PSSession $s }
}

# Run a command line inside $User's INTERACTIVE desktop session (they must be
# signed in): a one-off scheduled task with /IT. -Elevated only for admins.
function Start-ZaZaInteractive([string] $User, [string] $CommandLine, [string] $Execute = "", [switch] $Elevated, [switch] $Wait,
                               [int] $TimeoutSec = 3600) {
    # one hashtable: an empty string in -ArgumentList is dropped and shifts the later arguments
    $opts = @{ user = $User; cmd = "$CommandLine"; execute = "$Execute"; elevated = [bool]$Elevated; wait = [bool]$Wait; timeout = $TimeoutSec }
    Invoke-ZaZaVM -ArgumentList $opts -Script {
        param($o)
        $user, $cmd, $execute, $elevated, $wait, $timeout = $o.user, $o.cmd, $o.execute, $o.elevated, $o.wait, $o.timeout
        $name = "ZaZaTest-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
        # -Execute: run that program directly with $cmd as its arguments (no console window);
        # otherwise $cmd is a cmd.exe command line (redirection etc.).
        $action = if ($execute -and $cmd) { New-ScheduledTaskAction -Execute $execute -Argument $cmd } elseif ($execute) { New-ScheduledTaskAction -Execute $execute } else { New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd" }
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel $(if ($elevated) { "Highest" } else { "Limited" })
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries
        Register-ScheduledTask -TaskName $name -TaskPath "\ZaZaTest\" -Action $action -Principal $principal -Settings $settings | Out-Null
        Start-ScheduledTask -TaskPath "\ZaZaTest\" -TaskName $name
        if ($wait) {
            $deadline = (Get-Date).AddSeconds($timeout)
            Start-Sleep 2
            while ((Get-ScheduledTask -TaskPath "\ZaZaTest\" -TaskName $name).State -eq "Running" -and (Get-Date) -lt $deadline) { Start-Sleep 3 }
            $info = Get-ScheduledTaskInfo -TaskPath "\ZaZaTest\" -TaskName $name
            Unregister-ScheduledTask -TaskPath "\ZaZaTest\" -TaskName $name -Confirm:$false
            return $info.LastTaskResult
        }
        return $name
    }
}

function Get-ZaZaSessions { Invoke-ZaZaVM { (quser 2>$null) -join "`n" } }

# ── console screenshot (Hyper-V thumbnail of the VM's own screen) ────────────
function Save-ZaZaScreen([string] $Path, [int] $Width = 1024, [int] $Height = 768) {
    Add-Type -AssemblyName System.Drawing
    $svc = Get-WmiObject -Namespace root\virtualization\v2 -Class Msvm_VirtualSystemManagementService
    $vm = Get-WmiObject -Namespace root\virtualization\v2 -Class Msvm_ComputerSystem -Filter "ElementName='$script:VmName'"
    $vssd = $vm.GetRelated("Msvm_VirtualSystemSettingData") | Where-Object VirtualSystemType -eq "Microsoft:Hyper-V:System:Realized"
    $r = $svc.GetVirtualSystemThumbnailImage($vssd.__PATH, $Width, $Height)  # RGB565 + 4 trailing bytes
    if (-not $r.ImageData) { throw "no thumbnail (ReturnValue $($r.ReturnValue))" }
    $bmp = New-Object System.Drawing.Bitmap($Width, $Height, [System.Drawing.Imaging.PixelFormat]::Format16bppRgb565)
    $rect = New-Object System.Drawing.Rectangle(0, 0, $Width, $Height)
    $bd = $bmp.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::WriteOnly, $bmp.PixelFormat)
    [System.Runtime.InteropServices.Marshal]::Copy([byte[]]$r.ImageData, 0, $bd.Scan0, $Width * $Height * 2)
    $bmp.UnlockBits($bd)
    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    $Path
}

# ── keyboard (Msvm_Keyboard) ─────────────────────────────────────────────────
function Get-ZaZaKeyboard {
    $vm = Get-WmiObject -Namespace root\virtualization\v2 -Class Msvm_ComputerSystem -Filter "ElementName='$script:VmName'"
    # an ASSOCIATORS query: GetRelated() can hang here
    Get-WmiObject -Namespace root\virtualization\v2 -Query "ASSOCIATORS OF {$($vm.__PATH)} WHERE ResultClass = Msvm_Keyboard" | Select-Object -First 1
}
function Send-ZaZaText([string] $Text) { (Get-ZaZaKeyboard).TypeText($Text) | Out-Null }
function Send-ZaZaKey([int] $VirtualKey) { (Get-ZaZaKeyboard).TypeKey($VirtualKey) | Out-Null }
function Send-ZaZaCombo([int[]] $Keys) {
    $kb = Get-ZaZaKeyboard
    foreach ($k in $Keys) { $kb.PressKey($k) | Out-Null }
    [array]::Reverse($Keys)
    foreach ($k in $Keys) { $kb.ReleaseKey($k) | Out-Null }
}
# ── mouse (Msvm_SyntheticMouse): click at screen pixel x,y (1024x768 console) ─
function Send-ZaZaClick([int] $X, [int] $Y) {
    $vm = Get-WmiObject -Namespace root\virtualization\v2 -Class Msvm_ComputerSystem -Filter "ElementName='$script:VmName'"
    $m = Get-WmiObject -Namespace root\virtualization\v2 -Query "ASSOCIATORS OF {$($vm.__PATH)} WHERE ResultClass = Msvm_SyntheticMouse" | Select-Object -First 1
    $m.SetAbsolutePosition($X, $Y) | Out-Null
    Start-Sleep -Milliseconds 200
    $m.ClickButton(1) | Out-Null
}
function Send-ZaZaCtrlAltDel { (Get-ZaZaKeyboard).TypeCtrlAltDel() | Out-Null }

Export-ModuleMember -Function *
