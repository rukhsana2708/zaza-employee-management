; ZaZa Work Agent — Windows installer (Inno Setup 6.x).
; Built by installer/build.ps1:  ISCC.exe /DAppVersion=0.9.0 installer\ZaZaWorkAgent.iss
;
; - Installs the one-folder PyInstaller bundle into Program Files (admin once).
; - Registers the transparent logon task (\ZaZa\ZaZa Work Agent) via the app
;   itself (--register-autostart; /F replaces, so upgrades never duplicate it).
; - Enrollment (server, device ID, token) runs AFTER installation as the
;   signed-in employee (runasoriginaluser), because the token is protected
;   with Windows DPAPI for that employee's account; an elevated installer may
;   run as a different (administrator) account. The token is never a setup
;   parameter or command-line argument.
; - Uninstall stops the agent, removes the task, files and shortcuts, and
;   PRESERVES local data (unsynced records) by default.

#ifndef AppVersion
  #error AppVersion must be defined (build.ps1 passes /DAppVersion=x.y.z)
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\ZaZaWorkAgent"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
; Stable product identity: Windows recognises upgrades as the same product. Never change.
AppId={{9A4024E8-B137-431B-B89A-AA7301ACE1FE}
AppName=ZaZa Work Agent
AppVersion={#AppVersion}
AppVerName=ZaZa Work Agent {#AppVersion}
AppPublisher=ZaZa
VersionInfoCompany=ZaZa
VersionInfoProductName=ZaZa Work Agent
VersionInfoDescription=ZaZa Work Agent Setup
VersionInfoVersion={#AppVersion}
; Inno Setup 6 hides the Welcome page by default; ZaZa's flow starts with it.
DisableWelcomePage=no
DefaultDirName={autopf}\ZaZa Work Agent
DisableDirPage=yes
UsePreviousAppDir=yes
DefaultGroupName=ZaZa Work Agent
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=ZaZaWorkAgentSetup
SetupIconFile=assets\zaza.ico
UninstallDisplayIcon={app}\ZaZaWorkAgent.exe
UninstallDisplayName=ZaZa Work Agent
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Running agents are stopped by the app's own --stop-agents (see [Code]).
CloseApplications=no
RestartApplications=no
SetupLogging=yes
; Code signing (only with a real Authenticode certificate; never committed):
;   build.ps1 -SignToolCommand '"signtool.exe" sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f cert.pfx /p ***'
#ifdef SignTool
SignTool={#SignTool}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nZaZa Work Agent records work-activity metadata (which application and window is in use, and whether the computer is being used) for your organisation's attendance reporting. It is visible on this computer at all times.%n%nThe next page explains exactly what is and is not recorded.

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\docs\zaza\EMPLOYEE_PRIVACY.md"; DestDir: "{app}"; DestName: "ZaZa Work Agent - What is recorded.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\ZaZa Work Agent — Status & Privacy"; Filename: "{app}\ZaZaWorkAgent.exe"; Parameters: "--status"; Comment: "Shows that ZaZa Work Agent is running, its sync state, and what it does and does not record"
Name: "{group}\Enroll or re-enroll this device"; Filename: "{app}\ZaZaWorkAgent.exe"; Parameters: "--enroll"
Name: "{group}\What ZaZa records"; Filename: "{app}\ZaZa Work Agent - What is recorded.txt"
Name: "{group}\Uninstall ZaZa Work Agent"; Filename: "{uninstallexe}"

[Run]
; The logon task is created in [Code] (CurStepChanged), which checks the result.
; As the signed-in employee: enroll if needed, start the agent, show its status.
Filename: "{app}\ZaZaWorkAgent.exe"; Parameters: "--first-run"; Description: "Enroll this device and start ZaZa Work Agent"; Flags: postinstall runasoriginaluser nowait skipifsilent

[UninstallDelete]
; Only the program folder (e.g. a leftover stop.request). Per-user data in
; %LOCALAPPDATA%\ZaZa\WorkAgent is NOT touched.
Type: filesandordirs; Name: "{app}"

[Code]
var
  PrivacyPage: TOutputMsgMemoWizardPage;
  EnrollPage: TOutputMsgWizardPage;

procedure InitializeWizard;
begin
  PrivacyPage := CreateOutputMsgMemoPage(wpWelcome,
    'What ZaZa Work Agent records', 'Please read before continuing.',
    'ZaZa Work Agent is transparent workplace monitoring. It appears in Installed apps, the Start menu, Task Manager and Task Scheduler.',
    'ZaZa RECORDS:' + #13#10 +
    '  - the name of the application in use' + #13#10 +
    '  - the title of the active window' + #13#10 +
    '  - website domain only (e.g. example.com) - this version does not detect domains yet' + #13#10 +
    '  - whether the keyboard and mouse were used (yes/no only)' + #13#10 +
    '  - active / idle / locked / unknown status and Windows lock/unlock' + #13#10 +
    '  - work-session start and end times' + #13#10 +
    '  - connection status to your organisation''s ZaZa server' + #13#10 + #13#10 +
    'ZaZa does NOT record:' + #13#10 +
    '  - screenshots or screen video' + #13#10 +
    '  - what you type, or which keys you press' + #13#10 +
    '  - clipboard contents' + #13#10 +
    '  - microphone or webcam' + #13#10 +
    '  - full web addresses (URLs), browsing history or page contents' + #13#10 +
    '  - text read from the screen (OCR)' + #13#10 + #13#10 +
    'Active Hours are computer-activity metadata and are not, on their own, proof of productivity.');
  EnrollPage := CreateOutputMsgPage(PrivacyPage.ID,
    'Server and device enrollment', 'You will need three values from your administrator.',
    'After installation, a ZaZa enrollment window opens for your Windows account. Enter:' + #13#10 + #13#10 +
    '  1. the server address (starts with https://)' + #13#10 +
    '  2. the device ID' + #13#10 +
    '  3. the device token' + #13#10 + #13#10 +
    'The token is checked with the server and then stored protected by Windows for your account only. ' +
    'It is never shown again. If this computer was enrolled before, the existing enrollment is kept.' + #13#10 + #13#10 +
    'You can enroll later from Start menu > ZaZa Work Agent > Enroll or re-enroll this device.');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  OldExe: String;
  ResultCode: Integer;
begin
  Result := '';
  { Upgrade: stop the running agents of the existing installation first, so
    files can be replaced and no second agent starts. }
  OldExe := ExpandConstant('{app}\ZaZaWorkAgent.exe');
  if FileExists(OldExe) then
  begin
    { A failure leaves the existing installation untouched (nothing replaced yet). }
    if not Exec(OldExe, '--stop-agents', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      Result := 'The running ZaZa Work Agent could not be stopped (' + SysErrorMessage(ResultCode) + '). ' +
        'Nothing was changed. Close it and run setup again.'
    else if ResultCode <> 0 then
      Result := 'The running ZaZa Work Agent could not be stopped (exit code ' + IntToStr(ResultCode) + '). ' +
        'Nothing was changed. Sign out the other Windows users or restart the computer, then run setup again.';
    if Result <> '' then
      Log('PrepareToInstall: ' + Result);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  { Elevated (this is the installer): create or replace the logon task (/F, so never duplicated). }
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := 'Setting up start at sign-in...';
    if not Exec(ExpandConstant('{app}\ZaZaWorkAgent.exe'), '--register-autostart', '', SW_HIDE,
                ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    begin
      Log('Startup task registration failed, code ' + IntToStr(ResultCode));
      SuppressibleMsgBox('ZaZa Work Agent was installed, but its start at sign-in could not be set up ' +
        '(code ' + IntToStr(ResultCode) + '). Run setup again, or ask your administrator.',
        mbError, MB_OK, IDOK);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  { Before any file is removed: stop every running agent cleanly (work session
    closed), then remove the logon task. Local data is never touched. }
  if CurUninstallStep = usUninstall then
  begin
    if not Exec(ExpandConstant('{app}\ZaZaWorkAgent.exe'), '--uninstall-cleanup', '', SW_HIDE,
                ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    begin
      Log('Uninstall cleanup failed, code ' + IntToStr(ResultCode));
      if not UninstallSilent then
        MsgBox('ZaZa Work Agent could not be stopped completely (code ' + IntToStr(ResultCode) + '). ' +
          'Some files may remain until the computer is restarted.', mbError, MB_OK);
    end;
  end;

  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    MsgBox('ZaZa Work Agent was removed.' + #13#10 + #13#10 +
      'Local activity data (including any records not yet uploaded) and the stored device credentials were ' +
      'preserved in %LOCALAPPDATA%\ZaZa\WorkAgent for each Windows user, so reinstalling continues where it ' +
      'left off. To delete them, see the administrator guide ("Removing local data").',
      mbInformation, MB_OK);
end;
