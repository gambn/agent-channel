; agent-channel通道代理 —— Windows 安装向导（Inno Setup 6）
;
; 编译方式（推荐由 packaging/build.py --installer 调用，会自动传入版本号）：
;   ISCC.exe /DAppVersion=0.1.1 packaging\installer.iss
;
; 说明：
; - 脚本内所有相对路径都以本文件所在目录（packaging\）为基准；
; - PrivilegesRequired=lowest：本程序不需要管理员权限（零 UAC 是其设计目标），
;   默认装到用户目录，避免提权；
; - 源目录 dist\agent-channel 由 packaging\build.py 产出，内核已随包预置。

#ifndef AppVersion
  #define AppVersion "0.1.1"
#endif

#define AppName "agent-channel通道代理"
#define AppShortName "agent-channel"
#define AppPublisher "agent-channel"
#define AppExeName "agent-channel.exe"

[Setup]
AppId={{8E1F0A3C-5B2D-4C7A-9F61-2D4E8A7B3C10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppShortName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename={#AppShortName}-{#AppVersion}-setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "startupicon"; Description: "开机自动启动（托盘常驻）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; onedir 产物整体装入，含随包内核与前端静态资源
Source: "..\dist\agent-channel\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Comment: "启动 {#AppName} 并打开控制面板"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 运行时目录由 PyInstaller 生成，卸载时一并清理
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"
