; SageSetup.iss - the Windows installer for a Sage agent (sage-core, 2026-10-28; one click since 2026-11-14). Built by build.ps1:
;   ISCC.exe /DVersion=<engine version> /DNumVersion=<a.b.c.d> /DZip=<sage-core-*.zip> /DDist=<folder holding the zip + release.json>
;            /DAssets=<folder make_assets.py wrote> [/DReleaseUrl=<feed>] [/DBundle=<folder>] SageSetup.iss
; Per user, no admin: everything lands under %LOCALAPPDATA%\Sage. One click: no questions, no pages to press Next on. The
; installer shows what it is doing ("Step 3 of 6: Installing the building blocks"), and when it ends the app opens in a
; window of its own (assistant/app.py), where the organization is named and the agent set up. The organization's own
; folder is made there and is never touched by install or uninstall.
#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef NumVersion
  #define NumVersion "0.0.0.0"
#endif
#ifndef Zip
  #error Pass /DZip=<the release zip's file name>
#endif
#ifndef Dist
  #define Dist "..\..\..\dist"
#endif
#ifndef Assets
  #error Pass /DAssets=<the folder make_assets.py wrote sage.ico and wizard-small-*.bmp into>
#endif
#ifndef ReleaseUrl
  #define ReleaseUrl ""
#endif

[Setup]
AppId={{7E3C2B9A-5D41-4F8E-9B6A-2C1D0E7F4A58}
AppName=Sage
AppVersion={#Version}
AppVerName=Sage
AppPublisher=sage-core
VersionInfoVersion={#NumVersion}
VersionInfoProductName=Sage
VersionInfoDescription=Sage Setup
VersionInfoProductTextVersion={#Version}
DefaultDirName={localappdata}\Sage
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
PrivilegesRequired=lowest
OutputDir={#Dist}
OutputBaseFilename=SageSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=100
SetupIconFile={#Assets}\sage.ico
WizardSmallImageFile={#Assets}\wizard-small-55.bmp,{#Assets}\wizard-small-83.bmp,{#Assets}\wizard-small-110.bmp,{#Assets}\wizard-small-138.bmp
UninstallDisplayName=Sage
UninstallDisplayIcon={app}\sage.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupLogging=yes
CloseApplications=no
; Setup's guard against folder links it did not make is inherited by everything it starts, and pip, the browser download
; and Python itself then refuse any folder the owner has moved to another drive with a link (seen: the pip cache). This
; installer runs as the user with no elevation, so there is no boundary for that guard to hold here.
RedirectionGuard=no

[Messages]
SetupAppTitle=Sage
SetupWindowTitle=Installing Sage
WizardInstalling=Installing Sage
InstallingLabel=Putting the files in place.
WizardReady=Welcome to Sage
ReadyLabel1=Sage sets up an AI employee for your organization, on this computer.
ReadyLabel2b=Click Install. It takes a few minutes and needs no restart. When it is done, Sage opens by itself.
ExitSetupTitle=Stop installing Sage?
ExitSetupMessage=Sage is not installed yet. You can run the installer again any time; it picks up where it stopped.%n%nStop now?

[Files]
Source: "install.ps1"; DestDir: "{app}\bootstrap"; Flags: ignoreversion
Source: "{#Dist}\{#Zip}"; DestDir: "{app}\bootstrap"; Flags: ignoreversion
Source: "{#Dist}\release.json"; DestDir: "{app}\bootstrap"; Flags: ignoreversion
Source: "{#Assets}\sage.ico"; DestDir: "{app}"; Flags: ignoreversion
; build.ps1 -Bundle (2026-11-14): Python, pip, every package, the browser and the app's typefaces ride inside the installer,
; and install.ps1 (which looks for a `bundle` folder next to itself) downloads none of them. Without /DBundle the installer
; stays small and fetches them at install time, as before.
#ifdef Bundle
Source: "{#Bundle}\*"; DestDir: "{app}\bootstrap\bundle"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif

[Icons]
Name: "{autoprograms}\Sage"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\engine\assistant\app.py"""; WorkingDir: "{app}"; IconFilename: "{app}\sage.ico"; Comment: "Sage: your agent"
Name: "{autodesktop}\Sage"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\engine\assistant\app.py"""; WorkingDir: "{app}"; IconFilename: "{app}\sage.ico"; Comment: "Sage: your agent"

[UninstallRun]
Filename: "{app}\python\python.exe"; Parameters: """{app}\engine\assistant\app.py"" --quit"; Flags: runhidden waituntilterminated; RunOnceId: "SageAppQuit"
Filename: "{app}\python\python.exe"; Parameters: """{app}\engine\assistant\welcome.py"" --uninstall-startup"; Flags: runhidden waituntilterminated; RunOnceId: "SageStop"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\engine"
Type: filesandordirs; Name: "{app}\bootstrap"
Type: filesandordirs; Name: "{app}\fonts"
Type: filesandordirs; Name: "{app}\app-window"
Type: files; Name: "{app}\install.json"
Type: files; Name: "{app}\app.json"
; pack.json (the pointer to the organization's folder) and the organization's folder itself stay: they are the owner's data

[Code]
{ The work after the files are in place is install.ps1's (Python, the engine, the packages, the browser, Claude). It runs
  with no window of its own; each line it prints comes here, and the lines that start "##step" move the bar and the title.
  Extra arguments for it (a test passes -SkipBrowser -SkipClaude) come from the command line: /InstallArgs="...". A run
  with /SILENT or /VERYSILENT does not open the app at the end unless /OpenApp=1 is given. }
var
  Progress: TOutputProgressWizardPage;
  Title, Why, LastLine: String;
  StepNo, StepMax: Integer;

procedure InitializeWizard;
begin
  Progress := CreateOutputProgressPage('Setting up Sage', 'A few minutes. You can keep using your computer.');
  StepNo := 0;
  StepMax := 6;
  Title := 'Getting ready';
end;

procedure OnLine(const S: String; const Error, FirstLine: Boolean);
var
  T, Pct: String;
  P: Integer;
begin
  T := Trim(S);
  if T = '' then
    exit;
  Log('install: ' + T);
  if Pos('##step ', T) = 1 then begin
    Delete(T, 1, 7);
    P := Pos('/', T);
    StepNo := StrToIntDef(Copy(T, 1, P - 1), StepNo);
    Delete(T, 1, P);
    P := Pos(' ', T);
    StepMax := StrToIntDef(Copy(T, 1, P - 1), StepMax);
    Delete(T, 1, P);
    Title := 'Step ' + IntToStr(StepNo) + ' of ' + IntToStr(StepMax) + ': ' + T;
    Progress.SetText(Title, '');
    Progress.SetProgress((StepNo - 1) * 100, StepMax * 100);
  end else if Pos('##fail ', T) = 1 then
    Why := Copy(T, 8, 400)
  else begin
    P := Pos('% of ', T);
    if (P > 3) then begin
      { a download's own progress line: "...  42% of 131.2 MiB" }
      Pct := Trim(Copy(T, P - 3, 3));
      Progress.SetText(Title, 'Downloading: ' + Pct + '%');
      Progress.SetProgress((StepNo - 1) * 100 + StrToIntDef(Pct, 0), StepMax * 100);
    end else if Pos('[sage] ', T) = 1 then
      LastLine := Copy(T, 8, 300);       { the script's own notes are for the log; the window shows the step and a download's percent }
  end;
end;

function Quoted(const S: String): String;
begin
  Result := '"' + S + '"';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  RC: Integer;
  Params: String;
  Ok: Boolean;
begin
  if CurStep <> ssPostInstall then
    exit;
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + Quoted(ExpandConstant('{app}\bootstrap\install.ps1')) +
    ' -Root ' + Quoted(ExpandConstant('{app}')) + ' -EngineZip ' + Quoted(ExpandConstant('{app}\bootstrap\{#Zip}')) +
    ' -ReleaseJson ' + Quoted(ExpandConstant('{app}\bootstrap\release.json')) + ' -NoLaunch';
  if '{#ReleaseUrl}' <> '' then
    Params := Params + ' -ReleaseUrl ' + Quoted('{#ReleaseUrl}');
  if ExpandConstant('{param:InstallArgs|}') <> '' then
    Params := Params + ' ' + ExpandConstant('{param:InstallArgs|}');
  Why := '';
  RC := -1;
  Progress.SetText(Title, '');
  Progress.SetProgress(0, 100);
  Progress.Show;
  try
    Ok := ExecAndLogOutput(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, ExpandConstant('{app}'), SW_HIDE,
      ewWaitUntilTerminated, RC, @OnLine) and (RC = 0);
    if Ok then begin
      Progress.SetText('Opening Sage', '');
      Progress.SetProgress(100, 100);
    end;
  finally
    Progress.Hide;
  end;
  if not Ok then begin
    if Why = '' then
      Why := LastLine;
    if Why = '' then
      Why := 'The set-up stopped before it finished.';
    Log('install.ps1 failed (' + IntToStr(RC) + '): ' + Why);
    SuppressibleMsgBox('Sage could not finish installing.' + #13#10#13#10 + Why + #13#10#13#10 +
      'Nothing is broken. Check the internet connection and run the installer again: it picks up where it stopped.',
      mbError, MB_OK, IDOK);
    exit;
  end;
  if (not WizardSilent) or (ExpandConstant('{param:OpenApp|0}') = '1') then
    Exec(ExpandConstant('{app}\python\pythonw.exe'), Quoted(ExpandConstant('{app}\engine\assistant\app.py')), ExpandConstant('{app}'),
      SW_SHOWNORMAL, ewNoWait, RC);
end;
