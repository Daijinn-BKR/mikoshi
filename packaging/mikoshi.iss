; Instalador de Mikoshi. Se compila con build.ps1 si Inno Setup 6 está instalado.
#define AppVersion "0.5.3"

[Setup]
AppId={{7C1F0E52-3B8A-4E6D-9A41-5D2B7E90C3A1}
AppName=Mikoshi
AppVersion={#AppVersion}
DefaultDirName={autopf}\Mikoshi
DefaultGroupName=Mikoshi
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Mikoshi-{#AppVersion}-instalador
SetupIconFile=..\mikoshi\assets\mikoshi.ico
UninstallDisplayIcon={app}\Mikoshi.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Mikoshi\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Mikoshi"; Filename: "{app}\Mikoshi.exe"
Name: "{autodesktop}\Mikoshi"; Filename: "{app}\Mikoshi.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Mikoshi.exe"; Description: "{cm:LaunchProgram,Mikoshi}"; Flags: nowait postinstall skipifsilent
