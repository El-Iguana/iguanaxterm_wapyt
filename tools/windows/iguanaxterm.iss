; IguanaXterm's Windows installer. Built by
; tools/windows/build.py --installer, which passes the defines below.
;
; Per-user, no administrator rights: installs to
; %LOCALAPPDATA%\Programs\IguanaXterm. Data (SQLite, keys, logs) lives apart in
; %LOCALAPPDATA%\IguanaXterm, so upgrades keep it; uninstall asks about it.
; Folder downloads go to Downloads\IguanaXterm and are never removed.

#ifndef AppVersion
  #error Pass /DAppVersion=<version> (build.py does)
#endif
#ifndef BundleDir
  #error Pass /DBundleDir=<build\windows\bundle> (build.py does)
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef IconFile
  #define IconFile "iguanaxterm.ico"
#endif

[Setup]
; Never change AppId: Windows matches upgrades and the uninstaller by it.
AppId={{A3E0D5B4-7C29-4E61-8F4A-2B9C6D17E834}
AppName=IguanaXterm
AppVersion={#AppVersion}
AppVerName=IguanaXterm {#AppVersion}
AppPublisher=OldManGan
AppPublisherURL=https://github.com/El-Iguana/iguanaxterm_wapyt
DefaultDirName={localappdata}\Programs\IguanaXterm
DefaultGroupName=IguanaXterm
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=IguanaXterm-{#AppVersion}-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\app\iguanaxterm.ico
UninstallDisplayName=IguanaXterm
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile={#BundleDir}\app\LICENSE

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start IguanaXterm when I sign in (in the tray, without opening the browser)"; GroupDescription: "Startup:"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the runtime and the app wholesale, so a package or
; module dropped by the new version does not linger from the old one.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\IguanaXterm"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\iguanaxterm_launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\iguanaxterm.ico"; Comment: "SSH, SFTP, Telnet, FTP and VNC in your browser"
Name: "{group}\Reset IguanaXterm password"; Filename: "{app}\Reset IguanaXterm password.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\app\iguanaxterm.ico"
Name: "{group}\Uninstall IguanaXterm"; Filename: "{uninstallexe}"
Name: "{autodesktop}\IguanaXterm"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\iguanaxterm_launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\iguanaxterm.ico"; Tasks: desktopicon
Name: "{userstartup}\IguanaXterm"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\iguanaxterm_launcher.py"" --no-browser"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\iguanaxterm.ico"; Tasks: startup

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\iguanaxterm_launcher.py"""; WorkingDir: "{app}\app"; Description: "Start IguanaXterm now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; A running server holds files open under {app}; stop it first.
Filename: "{app}\python\python.exe"; Parameters: """{app}\app\iguanaxterm_launcher.py"" --stop"; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated; RunOnceId: "StopIguanaXterm"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\app\__pycache__"
Type: filesandordirs; Name: "{app}\app\appcode"
Type: filesandordirs; Name: "{app}\python"

[Code]
const
  DataDirName = 'IguanaXterm';

// Stop a running IguanaXterm before files are replaced during an upgrade.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Launcher, Python: String;
  ResultCode: Integer;
begin
  Result := '';
  Launcher := ExpandConstant('{app}\app\iguanaxterm_launcher.py');
  Python := ExpandConstant('{app}\python\python.exe');
  if FileExists(Launcher) and FileExists(Python) then
    Exec(Python, '"' + Launcher + '" --stop', ExpandConstant('{app}\app'),
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// Saved sessions, accounts and the key that decrypts their passwords live
// outside {app}. Keep them unless the person says otherwise; a silent
// uninstall always keeps them.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  DataDir := ExpandConstant('{localappdata}\' + DataDirName);
  if not DirExists(DataDir) or UninstallSilent then
    exit;
  if MsgBox('Also delete your IguanaXterm data?' + #13#10 + #13#10 +
            'This removes your accounts, saved sessions, known host keys and ' +
            'the key that decrypts their passwords:' + #13#10 + DataDir + #13#10 + #13#10 +
            'Your downloaded files are not touched.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(DataDir, True, True, True);
end;
