; Inno Setup Script for LotteryBot
; Produces a single standalone Windows Installer (LotteryBot-Setup.exe).
; Upgrades cleanly replace application binaries while preserving all user data in {app}\lottery_data.

#define MyAppName "LotteryBot"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppPublisher "Abel Online Lottery"
#define MyAppURL "https://github.com/Kbrom27/Lottery-Telegram-Bot"
#define MyAppExeName "LotteryBot.exe"

[Setup]
AppId={{8F4E2D91-7C3A-4B15-9E6F-2A1D4B7C8E30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=..\installer_output
OutputBaseFilename=LotteryBot-Setup
SetupIconFile=..\app\lottery.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Clean only the bundled runtime directory before upgrading so no stale DLLs remain;
; {app}\lottery_data (config.json, lottery.db, screenshots) is NEVER deleted.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\LotteryBot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\app\lottery.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\start-lotterybot.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\stop-lotterybot.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CLIENT_SETUP_GUIDE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{app}\lottery_data"; Permissions: users-modify
Name: "{app}\lottery_data\screenshots"; Permissions: users-modify

[Icons]
Name: "{group}\Start {#MyAppName}"; Filename: "{app}\start-lotterybot.bat"; IconFilename: "{app}\{#MyAppExeName}"; Comment: "Start LotteryBot Service and Open Web Control Center"
Name: "{group}\Stop {#MyAppName}"; Filename: "{app}\stop-lotterybot.bat"; IconFilename: "shell32.dll"; IconIndex: 27; Comment: "Stop LotteryBot Background Service"
Name: "{group}\Open {#MyAppName} in Browser"; Filename: "http://127.0.0.1:8000"; Comment: "Open LotteryBot Web Control Center"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Start {#MyAppName}"; Filename: "{app}\start-lotterybot.bat"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Comment: "Start LotteryBot Service and Open Web Control Center"
Name: "{autodesktop}\Stop {#MyAppName}"; Filename: "{app}\stop-lotterybot.bat"; IconFilename: "shell32.dll"; IconIndex: 27; Tasks: desktopicon; Comment: "Stop LotteryBot Background Service"

[Run]
Filename: "{app}\start-lotterybot.bat"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: shellexec postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\stop-lotterybot.bat"; Flags: runhidden; RunOnceId: "StopLotteryBotService"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  // Stop any running instance so upgrade can replace application files cleanly without touching lottery_data
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#MyAppExeName} /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
