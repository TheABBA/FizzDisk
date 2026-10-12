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
