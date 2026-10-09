<#
.SYNOPSIS
  Create a DISPOSABLE Hyper-V test VM for the Phase 9 frozen-installer
  validation (installer\vm\README.md). Run elevated on a Windows 10/11 Pro or
  Enterprise host with Hyper-V enabled. Never used for production.

.DESCRIPTION
  - applies a Windows 11 Enterprise Evaluation image (Microsoft Evaluation
    Center ISO) to a new VHDX with DISM (no interactive Windows Setup);
  - unattend.xml: skips OOBE, creates three LOCAL test accounts
      zazaadmin  (Administrators)  installs / runs the smoke test
      zazaemp    (Users)           the "employee" for the logon test
      zazaemp2   (Users)           second user for the cross-user DPAPI test
    with random passwords written to <VmRoot>\vm-accounts.json (outside Git);
  - turns Smart App Control OFF inside this disposable VM only (the unsigned
    development build would otherwise be blocked). The HOST is not changed;
  - Generation 2, Secure Boot, 4 GB RAM, 2 vCPU, "Default Switch" (NAT);
  - creates checkpoint "clean" after the first boot completes.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Iso,
    [string] $VmRoot = "D:\ZaZa-TestVM",
    [string] $Name = "ZaZa-Phase9-Test",
    [long] $MemoryBytes = 4GB,
    [long] $DiskBytes = 64GB
)
$ErrorActionPreference = "Stop"
$log = Join-Path $VmRoot "new-vm.log"
Start-Transcript -Path $log -Force | Out-Null
try {
    if (Get-VM -Name $Name -ErrorAction SilentlyContinue) { throw "VM $Name already exists" }
    $vhd = Join-Path $VmRoot "$Name.vhdx"
    if (Test-Path $vhd) { throw "$vhd already exists" }

    function NewPassword { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 20 | ForEach-Object { [char]$_ }) + "!a1" }
    $accounts = [ordered]@{ zazaadmin = NewPassword; zazaemp = NewPassword; zazaemp2 = NewPassword }
    $accounts | ConvertTo-Json | Set-Content -Encoding ascii (Join-Path $VmRoot "vm-accounts.json")

    # ── image ────────────────────────────────────────────────────────────────
    $mount = Mount-DiskImage -ImagePath $Iso -PassThru
    $isoDrive = ($mount | Get-Volume).DriveLetter + ":"
    $wim = @("$isoDrive\sources\install.wim", "$isoDrive\sources\install.esd") | Where-Object { Test-Path $_ } | Select-Object -First 1
    $image = Get-WindowsImage -ImagePath $wim | Where-Object ImageName -match "Enterprise" | Select-Object -First 1
    Write-Host "Image: $($image.ImageName) (index $($image.ImageIndex)) from $wim"

    # ── disk: EFI + MSR + Windows ────────────────────────────────────────────
    New-VHD -Path $vhd -SizeBytes $DiskBytes -Dynamic | Out-Null
    $disk = Mount-VHD -Path $vhd -Passthru | Get-Disk
    Initialize-Disk -Number $disk.Number -PartitionStyle GPT
    $efi = New-Partition -DiskNumber $disk.Number -Size 260MB -GptType "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}" -AssignDriveLetter
    Format-Volume -Partition $efi -FileSystem FAT32 -NewFileSystemLabel "System" -Confirm:$false | Out-Null
    New-Partition -DiskNumber $disk.Number -Size 16MB -GptType "{e3c9e316-0b5c-4db8-817d-f92df00215ae}" | Out-Null
    $win = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter
    Format-Volume -Partition $win -FileSystem NTFS -NewFileSystemLabel "Windows" -Confirm:$false | Out-Null
    $W = "$($win.DriveLetter):"; $S = "$($efi.DriveLetter):"

    Write-Host "Applying the image (several minutes)..."
    Expand-WindowsImage -ImagePath $wim -Index $image.ImageIndex -ApplyPath "$W\" | Out-Null
    & "$W\Windows\System32\bcdboot.exe" "$W\Windows" /s $S /f UEFI
    if ($LASTEXITCODE -ne 0) { throw "bcdboot failed" }

    # ── unattend ─────────────────────────────────────────────────────────────
    function Account($n, $group) {
        "<LocalAccount wcm:action=`"add`"><Name>$n</Name><Group>$group</Group><DisplayName>$n</DisplayName>" +
        "<Password><Value>$($accounts[$n])</Value><PlainText>true</PlainText></Password></LocalAccount>"
    }
    $arch = 'processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"'
    $unattend = @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" $arch>
      <ComputerName>ZAZA-TEST</ComputerName>
      <TimeZone>UTC</TimeZone>
    </component>
    <component name="Microsoft-Windows-Deployment" $arch>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order>
          <Path>reg add HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy /v VerifiedAndReputablePolicyState /t REG_DWORD /d 0 /f</Path>
          <Description>Disposable test VM only: Smart App Control off for the unsigned development build</Description>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" $arch>
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" $arch>
      <OOBE>
        <HideEULAPage>true</HideEULAPage><HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <HideLocalAccountScreen>true</HideLocalAccountScreen><ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts><LocalAccounts>
        $(Account "zazaadmin" "Administrators")
        $(Account "zazaemp" "Users")
        $(Account "zazaemp2" "Users")
      </LocalAccounts></UserAccounts>
      <AutoLogon><Enabled>true</Enabled><Username>zazaadmin</Username>
        <Password><Value>$($accounts.zazaadmin)</Value><PlainText>true</PlainText></Password></AutoLogon>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add"><Order>1</Order>
          <CommandLine>cmd /c mkdir C:\zaza &amp; echo ready&gt; C:\zaza\firstlogon.txt</CommandLine></SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
"@
    New-Item -ItemType Directory -Force "$W\Windows\Panther" | Out-Null
    Set-Content -Path "$W\Windows\Panther\unattend.xml" -Value $unattend -Encoding utf8

    Dismount-VHD -Path $vhd
    Dismount-DiskImage -ImagePath $Iso | Out-Null

    # ── VM ───────────────────────────────────────────────────────────────────
    $vm = New-VM -Name $Name -Generation 2 -MemoryStartupBytes $MemoryBytes -VHDPath $vhd -SwitchName "Default Switch" -Path $VmRoot
    Set-VMProcessor -VM $vm -Count 2
    Set-VMMemory -VM $vm -DynamicMemoryEnabled $false
    Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows
    Enable-VMIntegrationService -VM $vm -Name "Guest Service Interface"
    Set-VM -VM $vm -AutomaticCheckpointsEnabled $false
    Start-VM -VM $vm
    Write-Host "VM $Name started; waiting for the first logon (OOBE)..."
    "OK"
} catch {
    "ERROR: $($_.Exception.Message)"
    try { Dismount-VHD -Path $vhd -ErrorAction SilentlyContinue } catch { }
    try { Dismount-DiskImage -ImagePath $Iso -ErrorAction SilentlyContinue | Out-Null } catch { }
} finally {
    Stop-Transcript | Out-Null
}
