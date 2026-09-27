; Inno Setup script for the Windows installer.
; Built in CI (see .github/workflows/build.yml) after PyInstaller has produced
; dist\VCF Lite\ :
;   iscc /DAppVersion=0.1.0 packaging\windows\vcflite.iss
; Output: dist\VCF-Lite-Windows-x64-Setup.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "VCF Lite"
#define AppExe "VCF Lite.exe"

[Setup]
; Keep this AppId forever: it's how Windows recognises upgrades of the same app.
AppId={{6E4B1D52-8C7F-4E0B-9D6A-3F2C5A1B7E90}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Mike Saint-Antoine
AppPublisherURL=https://github.com/mikesaint-antoine/vcf_lite
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install by default (no admin prompt); users can choose all-users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename=VCF-Lite-Windows-x64-Setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\..\dist\VCF Lite\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Microsoft's small WebView2 bootstrapper (downloaded in CI); run only if the
; runtime the app's window needs is missing.
Source: "..\..\build\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Offer VCF Lite under "Open with" for .vcf / .gz without taking over .vcf,
; which Windows also uses for contact cards.
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExe}"" ""%1"""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".vcf"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".gz"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\.vcf\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\.gz\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft Edge WebView2 Runtime..."; Check: NeedsWebView2; Flags: waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
{ WebView2 Runtime is installed if its client key has a non-empty version
  (per-machine: HKLM, 64- or 32-bit view; per-user: HKCU). }
function WebView2Version(Root: Integer; Key: String): String;
begin
  if not RegQueryStringValue(Root, Key, 'pv', Result) then
    Result := '';
end;

function NeedsWebView2(): Boolean;
var
  V: String;
begin
  V := WebView2Version(HKLM64, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
  if (V = '') or (V = '0.0.0.0') then
    V := WebView2Version(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
  if (V = '') or (V = '0.0.0.0') then
    V := WebView2Version(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
  Result := (V = '') or (V = '0.0.0.0');
end;
