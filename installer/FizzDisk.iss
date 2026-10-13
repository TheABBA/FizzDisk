; FizzDisk installer script (Inno Setup 6)
; Build with:  ISCC.exe /DVERSION=1.0.0 installer\FizzDisk.iss
; It packages the PyInstaller onedir output (..\dist\FizzDisk\) into a single
; FizzDiskSetup-<version>.exe that installs the app into its own folder.

#define MyAppName "FizzDisk"
#ifndef VERSION
  #define VERSION "1.0.0"
#endif
#define MyAppVersion VERSION
#define MyAppPublisher "FizzDisk"
#define MyAppExeName "FizzDisk.exe"

[Setup]
AppId={{B7C6F1C2-9A3E-4D5B-8F2A-1E9C7D4B6A10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=license.txt
; One UAC prompt on install (the "run as admin" step the user asked for)
PrivilegesRequired=admin
OutputDir=..\dist
OutputBaseFilename=FizzDiskSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\assets\fizzdisk.ico
ArchitecturesInstallIn64BitMode=x64compatible
; Give the installer a real FizzDisk version resource (not the generic "Inno Setup"
; stub) -- Defender/AV engines use company & product metadata for reputation, and
; a generic setup stub is a common false-positive trigger while unsigned.
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoOriginalFileName=FizzDiskSetup-{#MyAppVersion}.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "..\dist\FizzDisk\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
procedure InitializeWizard();
begin
  { Pin a reassuring pre-install note about the certificate (SmartScreen) on
    the Welcome page, so the user knows the "unknown publisher" warning is
    expected while our code-signing certificate is being finalized. }
  WizardForm.WelcomeLabel2.Caption :=
      WizardForm.WelcomeLabel2.Caption + #13#10#13#10 +
      'Note: FizzDisk''s code-signing certificate is still being finished, so ' +
      'the first time you open this installer Microsoft''s "Windows protected ' +
      'your PC" screen may list the publisher as "unknown". That''s normal ' +
      'while we sign the app - click "More info" then "Run anyway" to continue. ' +
      'If Windows will not open the downloaded file at all, right-click it, ' +
      'choose Properties, and under General click "Unblock". This warning ' +
      'disappears once our certificate has been approved.';
end;
