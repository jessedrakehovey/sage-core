; SageSetup.iss - the Windows installer for a Sage agent (sage-core, 2026-10-28). Built by build.ps1:
;   ISCC.exe /DVersion=<engine version> /DZip=<sage-core-*.zip> /DDist=<folder holding the zip + release.json> [/DReleaseUrl=<feed>] SageSetup.iss
; Per user, no admin: everything lands under %LOCALAPPDATA%\Sage. The organization's own folder is made afterwards by the
; setup page (assistant/welcome.py) and is never touched by install or uninstall.
#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Zip
  #error Pass /DZip=<the release zip's file name>
#endif
#ifndef Dist
  #define Dist "..\..\..\dist"
#endif
#ifndef ReleaseUrl
  #define ReleaseUrl ""
#endif

[Setup]
AppId={{7E3C2B9A-5D41-4F8E-9B6A-2C1D0E7F4A58}
AppName=Sage
AppVersion={#Version}
AppVerName=Sage (engine {#Version})
AppPublisher=sage-core
DefaultDirName={localappdata}\Sage
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#Dist}
OutputBaseFilename=SageSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Sage
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupLogging=yes

[Messages]
WelcomeLabel2=This installs Sage on this computer for your user account only: a private Python, the engine, its browser and the Claude Code CLI.%n%nAfter the install a page opens in your browser and asks three things: your organization's name, what to call the agent, and who the owners are. Nothing is sent anywhere without an owner's tap.

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "install.ps1"; DestDir: "{app}\bootstrap"; Flags: ignoreversion
Source: "{#Dist}\{#Zip}"; DestDir: "{app}\bootstrap"; Flags: ignoreversion
Source: "{#Dist}\release.json"; DestDir: "{app}\bootstrap"; Flags: ignoreversion

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\bootstrap\install.ps1"" -Root ""{app}"" -EngineZip ""{app}\bootstrap\{#Zip}"" -ReleaseJson ""{app}\bootstrap\release.json"" -ReleaseUrl ""{#ReleaseUrl}"" -NoLaunch"; StatusMsg: "Installing Python, the browser and the Claude CLI (a few minutes; downloads)..."; Flags: waituntilterminated
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\engine\assistant\welcome.py"" --open"; WorkingDir: "{app}\engine"; Description: "Open the setup page now"; Flags: postinstall nowait skipifsilent

[Icons]
Name: "{autoprograms}\Sage"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\engine\assistant\welcome.py"" --open"; WorkingDir: "{app}\engine"; Comment: "Sage: the agent's page, or the setup page"
Name: "{autodesktop}\Sage"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\engine\assistant\welcome.py"" --open"; WorkingDir: "{app}\engine"; Tasks: desktopicon

[UninstallRun]
Filename: "{app}\python\python.exe"; Parameters: """{app}\engine\assistant\welcome.py"" --uninstall-startup"; Flags: runhidden waituntilterminated; RunOnceId: "SageStop"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\engine"
Type: filesandordirs; Name: "{app}\bootstrap"
Type: files; Name: "{app}\install.json"
; pack.json (the pointer to the organization's folder) and the organization's folder itself stay: they are the owner's data
