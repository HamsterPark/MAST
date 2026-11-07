; MAST — Inno Setup script  (renamed from "MAST2"; v2.6.0+)
;
; Builds a single-file installer (MAST-Setup-v<version>-windows-x64.exe) that:
;   * installs MAST.exe + _internal/ to C:\MAST\ (default — user changeable)
;   * creates Desktop + Start Menu shortcuts
;   * on uninstall, removes ONLY the binary + _internal/, preserves all user data
;   * (Option A rename) uses a FRESH AppId; on install it DETECTS the legacy
;     v1 "MAST" and v2 "MAST2" products and offers to uninstall them first
;     (decoupled from the standalone MAST-Legacy-Uninstall.exe remover).
;
; NOTE: the file is still named mast2_setup.iss (build-internal; mast2_build.ps1
; invokes it) but it now builds the rebranded "MAST" product. The old v1
; installer mast_setup.iss is dead (kept only as the source of v1's AppId).
;
; Build:  ISCC.exe /DMAST_VERSION=2.6.0 installer\mast2_setup.iss
;
; Unattended install (7.0+):  MAST-Setup-....exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;   /DIR="D:\MAST" /DATADIR="D:\MAST-data" /MERGETASKS="!desktopicon" [/NOICONS] [/LOG=...]
; Every question in [Code] goes through SuppressibleMsgBox, whose silent default is the
; cautious answer: upgrade in place yes; uninstall a legacy product no; close a running
; MAST no (the install is cancelled instead — a force-kill can wedge the Nanonis port).
; A plain MsgBox is NOT suppressed by /SUPPRESSMSGBOXES: it would wait, invisibly, forever.
; Prerequisite: PyInstaller --onedir build at dist\MAST\ has run.

#ifndef MAST_VERSION
  #define MAST_VERSION "2.6.0"
#endif
#define MAST_NAME    "MAST"
#define MAST_PUBLISHER "Yuanhao Lyu"
#define MAST_URL ""
; Legacy product AppIds (for Option-A detect-and-offer-remove on install).
; NOTE the trailing `}}` — the products' `AppId={{GUID}}` registers the _is1 key
; with a literal double-close brace (verified live). Single-brace would not match.
#define V1_APPID   "{B6F2C4D7-7C4A-4E63-9F7A-MASTSTM2026}}"
#define V2_APPID   "{C8E3D6F2-9D3E-4C95-A1D9-MAST2STM2026}}"

[Setup]
; FRESH AppId for the renamed MAST line — NOT reused from v1 (B6F2) or MAST2
; (C8E3); a different codebase must not masquerade as an upgrade of either.
AppId={{D9A4E1B7-6F2C-4D85-B3E0-MAST26STM2026}}
AppName={#MAST_NAME}
AppVersion={#MAST_VERSION}
AppVerName={#MAST_NAME} v{#MAST_VERSION}
AppPublisher={#MAST_PUBLISHER}
AppPublisherURL={#MAST_URL}
AppSupportURL={#MAST_URL}
AppUpdatesURL={#MAST_URL}
DefaultDirName=C:\{#MAST_NAME}
DefaultGroupName={#MAST_NAME}
AllowNoIcons=yes
LicenseFile=
InfoBeforeFile=
InfoAfterFile=
OutputDir=..\dist
OutputBaseFilename={#MAST_NAME}-Setup-v{#MAST_VERSION}-windows-x64
SetupIconFile=..\logo\MAST2_logo.ico
; Build-speed: the bulk of the bundle is the incompressible M12 backbone
; (1.13 GB float .safetensors) + the literature index (.npy/.parquet) — those
; are stored uncompressed (see [Files] nocompression below). The remaining
; compressible payload (torch DLLs + Python) uses lzma2/normal with
; multi-threaded LZMA2 block compression.
Compression=lzma2/normal
LZMANumBlockThreads=8
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
DisableProgramGroupPage=yes
; Disable Inno's built-in close prompt: it sends WM_CLOSE, which can't reach the
; windowless `--push-server-mode` MAST.exe child (→ "can't close"). We close
; running instances ourselves in PrepareToInstall (graceful then force).
CloseApplications=no
UninstallDisplayIcon={app}\MAST.exe
UninstallDisplayName={#MAST_NAME} v{#MAST_VERSION}
DisableDirPage=no
; Always offer the language page; do not auto-pick from the OS locale.
; English is first in [Languages] so it is the default selection; Chinese
; (and Czech) remain available. ShowLanguageDialog=auto previously hid the
; page on matching locales (e.g. zh-CN Windows → silent Chinese, no choice).
ShowLanguageDialog=yes
LanguageDetectionMethod=none
; Tell Explorer to refresh icons/associations after install. Replaces the
; previous hand-rolled SHChangeNotify external call, which could raise a
; Pascal Script "Runtime error … Type mismatch" at ssPostInstall on some
; Inno/Windows combinations even though the files were already installed.
ChangesAssociations=yes

[Languages]
Name: "english";  MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "czech"; MessagesFile: "Czech.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; 升级时先清掉**上一版的源码副本**，再让 [Files] 铺新的。
;
; Inno 只覆盖、不删除 —— 新版里没有的文件会原样留在那儿。2026-08-21 升到 6.3.0
; 之后实测：`_internal\mast\_src` 里有 787 个 .py，而这一版只该有 767，
; 多出来的 20 个全是改名前的 `campaign/`（那次改名把它整棵树换成了 `conduct/`）。
;
; 为什么单独清这一棵：`_src` 是 **eject 功能的源码来源**，也是 DP 分析子进程的
; 只读视图。留着旧副本 = 源码副本不再等于跑着的代码 —— 导出一个已经不存在的模块、
; 或者把一段死代码当成现行实现去读，而每一步看起来都正常。它还会随每次改名
; 永久累积。
;
; 只清下面点名的这两棵，不碰 `{app}` 下别的东西：`config\`、`api key\`、
; `experiments\` 都可能有操作员的数据。
Type: filesandordirs; Name: "{app}\_internal\mast\_src"

; 同一个形状的第二处：前端 bundle。Vite 的文件名带内容哈希，所以每改一次前端
; 就多出一对 `index-<hash>.js` + `.map`（约 9.5 MB），旧的**永远不会被覆盖**。
; 2026-08-21 装完 6.3.1 实测，rig 上并存着三个版本的产物：
;
;   index-BWy5p-gB.css   08-19        ← 6.2.42 一带
;   index-8rdzM7Qg.js*   08-20 21:19  ← 6.2.45
;   index-BHNm3pjG.js*   08-21 15:29  ← 6.3.1，index.html 只引用这一对
;
; 不是正确性问题（服务端读 `index.html` 决定送哪个，`/assets` 只是静态挂载，
; **不 glob 选入口**），但它每发一版涨约 9.5 MB，而且任何按 `assets/index-*.js`
; 通配去找入口的东西都会在装机树上撞到多个候选 —— 产物校验器的前端声明就是
; 那么写的。整棵 `assets\` 都是构建产物，清掉之后 [Files] 立刻铺回当前那份。
Type: filesandordirs; Name: "{app}\_internal\frontend\dist\assets"

; 第三处（7.0 起）：客户端集成 —— MCP 桥、Claude Code 插件与市场清单、Codex 配置工具、
; 外部 agent 文档。这次留下旧文件不只是占地方：Codex 直接从
; `{app}\integrations\claude-code\server` 启动 MCP 桥，Claude Code 从 `{app}` 这个本地
; 市场装插件。桥的模块改名或删掉之后，旧文件会原样留着，与新版混在同一个包里被 import。
; 这三棵全是构建产物，没有操作员数据（Codex 的 config.toml 和 agent 工作区在用户目录里，
; 不在这里）；插件 python 路径的改写由下面 [Run] 在每次安装后重做。
Type: filesandordirs; Name: "{app}\integrations"
Type: filesandordirs; Name: "{app}\.claude-plugin"
Type: filesandordirs; Name: "{app}\docs\external"

; 第四处（7.1.3 起）：被精简掉的 torch 全家桶。视觉模型权重不再随包，torch / timm /
; transformers 等约 4 GB 纯死重量已整个从打包里排除（见 mast2.spec 的 excludes）。但 Inno
; 只覆盖不删 —— 从 7.0.x / 7.1.0–7.1.2 升级上来的老机器，`_internal\torch` 等旧目录会原样
; 留着继续占约 4 GB。所以这里显式清掉这些包目录和各自的 *.dist-info（通配）。全是构建产物，
; 没有操作员数据。OTA 差量升级则通过清单里的 removed[] 清除。
Type: filesandordirs; Name: "{app}\_internal\torch*"
Type: filesandordirs; Name: "{app}\_internal\functorch*"
Type: filesandordirs; Name: "{app}\_internal\timm*"
Type: filesandordirs; Name: "{app}\_internal\peft*"
Type: filesandordirs; Name: "{app}\_internal\transformers*"
Type: filesandordirs; Name: "{app}\_internal\accelerate*"
Type: filesandordirs; Name: "{app}\_internal\safetensors*"
Type: filesandordirs; Name: "{app}\_internal\huggingface_hub*"
Type: filesandordirs; Name: "{app}\_internal\tokenizers*"
Type: filesandordirs; Name: "{app}\_internal\sympy*"
Type: filesandordirs; Name: "{app}\_internal\mpmath*"

[Files]
; Incompressible binary assets (DINOv3 backbone weights + literature index
; vectors/parquet) — store WITHOUT compression. Must precede the catch-all + be
; excluded from it so each file is packed exactly once.
; skipifsourcedoesntexist：构建脚本默认不带这两类资产（-IncludeLiteratureIndex /
; -IncludeVisionModels 才拷），这时 dist\MAST\MASTv2\artifacts 不存在，而 ISCC 遇到
; 一个什么都匹配不上的通配会直接中止编译。
Source: "..\dist\MAST\MASTv2\artifacts\*"; DestDir: "{app}\MASTv2\artifacts"; Flags: ignoreversion recursesubdirs createallsubdirs nocompression skipifsourcedoesntexist
; Everything else (MAST.exe + _internal torch/python — compressible).
Source: "..\dist\MAST\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "MASTv2\artifacts\*"

[Dirs]
Name: "{app}\api key";          Permissions: users-modify
Name: "{app}\experiments";      Permissions: users-modify
Name: "{app}\working-sessions"; Permissions: users-modify
Name: "{app}\models";           Permissions: users-modify
Name: "{app}\config\overrides"; Permissions: users-modify

[Icons]
Name: "{group}\{#MAST_NAME}";          Filename: "{app}\MAST.exe"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MAST_NAME}}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MAST_NAME}";   Filename: "{app}\MAST.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MAST_NAME}"; Filename: "{app}\MAST.exe"; WorkingDir: "{app}"; Tasks: quicklaunchicon

[Run]
; 把 Claude Code 插件的 `python` 缺省值改成**这次安装**的随包运行时的绝对路径（插件里写的
; 是 "python"，在 Windows 上常常打开的是应用商店）。之后用户只需在 Claude Code 里
; `/plugin marketplace add <安装目录>`、`/plugin install mast@mast`。
; 没有随包运行时（只含代码的构建）就由 Check 跳过；它失败也不拦安装 —— 插件仍可在
; Claude Code 的插件选项里手工填 python。
; Parameters 里 `""` 是 Inno 字符串里的一个双引号：路径带空格也是一个参数。
Filename: "{app}\MASTv2\pyruntime\python.exe"; Parameters: "-I -X utf8 ""{app}\integrations\setup_clients.py"" patch-plugin --app ""{app}"""; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; Check: PyRuntimePresent
Filename: "{app}\MAST.exe"; Description: "{cm:LaunchProgram,{#StringChange(MAST_NAME, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files;          Name: "{app}\MAST.exe"
Type: filesandordirs; Name: "{app}\_internal"
; 客户端集成同样是程序文件：Codex 从这里启动桥时 Python 可能写下 __pycache__，
; 那不在安装清单里，卸载不会自己删。
Type: filesandordirs; Name: "{app}\integrations"
Type: filesandordirs; Name: "{app}\.claude-plugin"
Type: filesandordirs; Name: "{app}\docs\external"

[CustomMessages]
; [Code] 里所有面向用户的文字。占位符 %1、%2 由 FmtMessage 填；%n 是换行。
; 三种语言的键必须一一对应（缺一种会回落到英文/默认语言，而不是报错）。
english.LegacyFound=Found an old %1 v%2.%nThis version renames MAST2 to MAST. Uninstall it now, before installing?%n(Only program files are removed; user data is kept. You can also clean up later with MAST-Legacy-Uninstall.exe.)
chinesesimplified.LegacyFound=检测到旧版 %1 v%2。%n本版已将 MAST2 更名为 MAST。是否现在先卸载它？%n(仅删除程序文件，保留用户数据；也可日后用 MAST-Legacy-Uninstall.exe 清理。)
czech.LegacyFound=Byla nalezena stará verze %1 v%2.%nTato verze přejmenovává MAST2 na MAST. Chcete ji nyní odinstalovat před instalací?%n(Odstraní se pouze soubory programu, uživatelská data zůstanou zachována. Později lze také použít MAST-Legacy-Uninstall.exe.)

english.UpgradeFound=Found an installed MAST v%1%nUpgrading to v%2 will overwrite MAST.exe and _internal\, but all user data is kept.%n%nContinue with the installation?
chinesesimplified.UpgradeFound=检测到已安装的 MAST v%1%n本次升级到 v%2 将覆盖 MAST.exe 与 _internal\，但保留所有用户数据。%n%n继续安装？
czech.UpgradeFound=Byl nalezen nainstalovaný MAST v%1%nAktualizace na v%2 přepíše MAST.exe a _internal\, ale všechna uživatelská data zůstanou zachována.%n%nPokračovat v instalaci?

english.DataDirTitle=Data directory
chinesesimplified.DataDirTitle=数据保存目录
czech.DataDirTitle=Adresář s daty

english.DataDirSubTitle=All experiment data, chat history, API keys, logs and so on are stored here
chinesesimplified.DataDirSubTitle=所有实验数据、对话历史、API key、日志等都会保存在这里
czech.DataDirSubTitle=Zde se ukládají všechna experimentální data, historie konverzací, klíče API, protokoly atd.

english.DataDirBody=Choose MAST's "user data directory".%n%nEverything produced after installation (experiment records, conversations, API keys, TTS cache, Nanonis sessions, trained models, config overrides, launcher/service logs) is stored under this directory.%n%nUpgrading or uninstalling MAST will **not** delete or overwrite its contents.
chinesesimplified.DataDirBody=请选择 MAST 的"用户数据目录"。%n%n安装后产生的所有数据（实验记录、对话、API key、TTS 缓存、Nanonis 会话、训练好的模型、配置 override、启动器/服务日志）都保存在此目录下。%n%n升级或卸载 MAST 时，此目录的内容**不会**被删除或覆盖。
czech.DataDirBody=Vyberte „adresář s uživatelskými daty“ aplikace MAST.%n%nVšechna data vzniklá po instalaci (záznamy experimentů, konverzace, klíče API, mezipaměť TTS, relace Nanonis, natrénované modely, přepsání konfigurace, protokoly spouštěče a služby) se ukládají do tohoto adresáře.%n%nPři aktualizaci ani odinstalaci aplikace MAST se jeho obsah **nesmaže** ani nepřepíše.

english.DataDirLabel=Data directory:
chinesesimplified.DataDirLabel=数据目录:
czech.DataDirLabel=Adresář s daty:

english.BackupFailed=Could not back up the literature index (robocopy returned %1).%nContinuing will overwrite %2,%nwhich holds the papers this machine has collected on its own.%n%nCopy that directory manually first, then continue.
chinesesimplified.BackupFailed=无法备份文献索引（robocopy 返回 %1）。%n继续安装会覆盖 %2，%n其中包含本机自主取文攒下的文献。%n%n建议先手动复制该目录再继续。
czech.BackupFailed=Zálohování indexu literatury se nezdařilo (robocopy vrátil %1).%nPokračováním se přepíše %2,%nkde jsou uloženy články, které tento počítač sám shromáždil.%n%nNejprve tento adresář ručně zkopírujte a poté pokračujte.

english.RunningPrompt=MAST is running (this may include the windowless background update server).%nIt has to be closed before its program files can be overwritten.%n%nFirst save unfinished work in MAST and stop any scan in progress.%n%nClick "Yes" = close MAST and continue; click "No" = cancel the installation.
chinesesimplified.RunningPrompt=检测到 MAST 正在运行（可能包括无窗口的后台更新服务器）。%n安装需要先关闭它才能覆盖程序文件。%n%n请先在 MAST 中保存未完成的工作、停止正在进行的扫描。%n%n点"是"= 关闭 MAST 并继续安装；点"否"= 取消安装。
czech.RunningPrompt=Aplikace MAST je spuštěna (může jít i o službu aktualizací na pozadí bez okna).%nPřed přepsáním souborů programu je nutné ji zavřít.%n%nNejprve v aplikaci MAST uložte rozdělanou práci a zastavte probíhající skenování.%n%nKlepnutím na „Ano“ aplikaci MAST zavřete a pokračujete v instalaci; klepnutím na „Ne“ instalaci zrušíte.

english.StillRunning=MAST is still running, so the installation cannot continue.%nEnd all MAST.exe processes manually in Task Manager, then run the installer again.
chinesesimplified.StillRunning=仍检测到 MAST 在运行，无法继续安装。%n请在任务管理器中手动结束所有 MAST.exe 后，重新运行安装程序。
czech.StillRunning=Aplikace MAST stále běží, instalaci nelze dokončit.%nUkončete ve Správci úloh ručně všechny procesy MAST.exe a poté instalační program spusťte znovu.

english.InstallCancelled=Installation cancelled: close MAST first, then run the installer again.
chinesesimplified.InstallCancelled=安装已取消：请先关闭 MAST，再运行安装程序。
czech.InstallCancelled=Instalace byla zrušena: nejprve zavřete aplikaci MAST a poté instalační program spusťte znovu.

english.UninstallSummary=MAST has been uninstalled (program files only).%nThe following user data was kept in %1:%n  - experiments\ (experiment records, conversations, TTS cache, plans, logs)%n  - api key\ (LLM API keys + LAN auth)%n  - working-sessions\ (Nanonis scan sessions)%n  - models\ (trained ML models)%n  - config\ (admin overrides)%n%nTo remove everything, delete these directories manually.
chinesesimplified.UninstallSummary=MAST 已卸载（仅删除程序文件）。%n以下用户数据被保留在 %1：%n  - experiments\（实验记录、对话、TTS 缓存、计划、日志）%n  - api key\（LLM API key + LAN auth）%n  - working-sessions\（Nanonis 扫描会话）%n  - models\（训练好的 ML 模型）%n  - config\（admin override）%n%n如需彻底清除，请手动删除上述目录。
czech.UninstallSummary=Aplikace MAST byla odinstalována (pouze soubory programu).%nNásledující uživatelská data zůstala zachována v %1:%n  - experiments\ (záznamy experimentů, konverzace, mezipaměť TTS, plány, protokoly)%n  - api key\ (klíče API LLM + ověření v síti LAN)%n  - working-sessions\ (relace skenování Nanonis)%n  - models\ (natrénované modely ML)%n  - config\ (přepsání konfigurace správcem)%n%nChcete-li vše odstranit, výše uvedené adresáře smažte ručně.
[Code]
var
  DataDirPage: TInputDirWizardPage;

function UninstKey(AppId: String): String;
begin
  Result := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' + AppId + '_is1';
end;

// [Run] 里 patch-plugin 那一行的 Check：随包运行时和脚本都在才跑（只含代码的构建没有
// MASTv2\pyruntime）。[Run] 的条目在文件装完之后才求值，所以这里看到的是新装的树。
function PyRuntimePresent(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\MASTv2\pyruntime\python.exe')) and
            FileExists(ExpandConstant('{app}\integrations\setup_clients.py'));
end;

// Graceful-then-force close of a running product exe (port-fragility: a
// mid-TCP force-kill can corrupt the Nanonis port until it restarts).
procedure KillExe(ExeName: String);
var rc: Integer;
begin
  Exec(ExpandConstant('{cmd}'), '/C taskkill /IM ' + ExeName, '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(4000);
  Exec(ExpandConstant('{cmd}'), '/C taskkill /F /T /IM ' + ExeName, '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(600);
end;

// Option A: offer to uninstall a legacy product (v1 MAST / v2 MAST2) before
// installing the renamed MAST. Returns silently if not present / declined.
procedure OfferRemoveLegacy(AppId, ExeName, ProdLabel: String);
var unins, ver: String; rc: Integer;
begin
  if not RegQueryStringValue(HKLM64, UninstKey(AppId), 'DisplayVersion', ver) then
    Exit;
  if SuppressibleMsgBox(FmtMessage(CustomMessage('LegacyFound'), [ProdLabel, ver]),
            mbConfirmation, MB_YESNO, IDNO) = IDYES then
  begin
    KillExe(ExeName);
    if RegQueryStringValue(HKLM64, UninstKey(AppId), 'UninstallString', unins) then
      Exec(RemoveQuotes(unins), '/SILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_SHOW, ewWaitUntilTerminated, rc);
  end;
end;

function InitializeSetup(): Boolean;
var
  PriorVersion: String;
  Confirmed: Integer;
begin
  Result := True;
  // In-place upgrade of MAST itself (same fresh AppId). The registered _is1 key
  // carries a trailing `}}` (AppId={{...}}), so match that here too.
  if RegQueryStringValue(HKLM64, UninstKey('{D9A4E1B7-6F2C-4D85-B3E0-MAST26STM2026}}'), 'DisplayVersion', PriorVersion) then
  begin
    Confirmed := SuppressibleMsgBox(
      FmtMessage(CustomMessage('UpgradeFound'), [PriorVersion, '{#MAST_VERSION}']),
      mbConfirmation, MB_YESNO, IDYES);
    if Confirmed = IDNO then
    begin
      Result := False;
      Exit;
    end;
  end;
  // Legacy products (the rename's whole point): offer to clean them up.
  OfferRemoveLegacy('{#V1_APPID}', 'MAST.exe', 'MAST v1');
  OfferRemoveLegacy('{#V2_APPID}', 'MAST2.exe', 'MAST2');
end;

procedure InitializeWizard;
begin
  DataDirPage := CreateInputDirPage(
    wpSelectDir,
    CustomMessage('DataDirTitle'),
    CustomMessage('DataDirSubTitle'),
    CustomMessage('DataDirBody'),
    False, '');
  DataDirPage.Add(CustomMessage('DataDirLabel'));
  // 无人值守安装可以用 /DATADIR="D:\MAST-data" 直接给出；没给就是 C:\MAST-data。
  DataDirPage.Values[0] := ExpandConstant('{param:DATADIR|{sd}\MAST-data}');
end;

procedure CurPageChanged(CurPageID: Integer);
var
  PriorMarker: String;
  PriorMarkerLines: TArrayOfString;
  ExistingPath: String;
begin
  if (DataDirPage <> nil) and (CurPageID = DataDirPage.ID) then
  begin
    // 命令行明确给了 /DATADIR，就以它为准（不被上一版的 data_dir.txt 覆盖）。
    if ExpandConstant('{param:DATADIR|}') <> '' then
      Exit;
    PriorMarker := ExpandConstant('{app}\data_dir.txt');
    if FileExists(PriorMarker) then
    begin
      if LoadStringsFromFile(PriorMarker, PriorMarkerLines) and
         (GetArrayLength(PriorMarkerLines) >= 1) then
      begin
        ExistingPath := Trim(PriorMarkerLines[0]);
        if (ExistingPath <> '') and DirExists(ExistingPath) then
          DataDirPage.Values[0] := ExistingPath;
      end;
    end
    else if DirExists(ExpandConstant('{app}\experiments')) or
            DirExists(ExpandConstant('{app}\api key')) then
    begin
      DataDirPage.Values[0] := ExpandConstant('{app}');
    end;
  end;
end;

function IsMASTRunning(): Boolean;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{cmd}'),
       '/C tasklist /FI "IMAGENAME eq MAST.exe" /NH | find /I "MAST.exe"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := (ResultCode = 0);
end;

// KNOWN_ISSUES §3.1 — the factory literature index ships in the package and the
// [Files] entry above copies it over {app}\MASTv2\artifacts\literature_index\
// with `ignoreversion`. That is the SAME directory the agent's own fetched
// papers are appended into (ingest._promote_to_big writes metadata.parquet +
// vectors.npy there), and nothing merges. So an upgrade used to delete every
// paper the operator's rig had collected, silently. The documented mitigation
// was "remember to back it up first", which is not a mitigation.
//
// Snapshot it here — BEFORE any file is overwritten. A COPY, not a move: the
// machine must never be in a state where it has no index, not even for the
// seconds between this and the file copy. mast.knowledge.index_merge picks the
// snapshot up on the next launch, merges back the rows the new index lacks, and
// then DELETES snapshots that turned out to add nothing (those are redundant by
// construction, so deleting them cannot lose information) — that is what keeps
// 205 MB from accumulating per upgrade.
procedure SnapshotLiteratureIndex();
var
  IndexDir, SnapDir, Stamp: String;
  rc: Integer;
begin
  IndexDir := ExpandConstant('{app}\MASTv2\artifacts\literature_index');
  // metadata.parquet, not the directory: an empty dir is nothing to protect.
  if not FileExists(IndexDir + '\metadata.parquet') then
    Exit;
  Stamp := GetDateTimeString('yyyymmdd-hhnnss', '-', '');
  SnapDir := ExpandConstant('{app}\MASTv2\artifacts\literature_index.pre-upgrade-') + Stamp;
  // robocopy exit codes 0..7 are success; 8+ are real failures. Exec's own
  // Boolean result must be checked too — if the shell never launched, rc is
  // never assigned and testing it alone would report a phantom success.
  rc := 16;
  if not Exec(ExpandConstant('{cmd}'),
       '/C robocopy "' + IndexDir + '" "' + SnapDir + '" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP',
       '', SW_HIDE, ewWaitUntilTerminated, rc) then
    rc := 16;
  if rc >= 8 then
    SuppressibleMsgBox(FmtMessage(CustomMessage('BackupFailed'), [IntToStr(rc), IndexDir]),
           mbError, MB_OK, IDOK);
end;

// Run BEFORE overwriting files (can abort the install). Prompt first.
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsMASTRunning() then
  begin
    // 静默安装时默认「否」：无人确认就强关一个可能正连着 Nanonis 的 MAST，代价太大。
    if SuppressibleMsgBox(CustomMessage('RunningPrompt'),
         mbConfirmation, MB_YESNO, IDNO) = IDYES then
    begin
      KillExe('MAST.exe');
      if IsMASTRunning() then
      begin
        Result := CustomMessage('StillRunning');
        Exit;
      end;
    end
    else
    begin
      Result := CustomMessage('InstallCancelled');
      Exit;
    end;
  end;
  // Past this point the install WILL overwrite files, and MAST is confirmed
  // stopped — so the index is not mid-write. Both conditions matter, which is
  // why the snapshot sits here rather than at the top.
  SnapshotLiteratureIndex();
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ChosenDir: String;
  Sub: String;
  Subs: array[0..5] of String;
  i: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    ChosenDir := DataDirPage.Values[0];
    if Trim(ChosenDir) = '' then
      ChosenDir := ExpandConstant('{app}');

    ForceDirectories(ChosenDir);

    Subs[0] := 'experiments';
    Subs[1] := 'experiments\logs';
    Subs[2] := 'api key';
    Subs[3] := 'working-sessions';
    Subs[4] := 'models';
    Subs[5] := 'config\overrides';
    for i := 0 to 5 do
    begin
      Sub := ChosenDir + '\' + Subs[i];
      ForceDirectories(Sub);
    end;

    SaveStringToFile(
      ExpandConstant('{app}\data_dir.txt'),
      ChosenDir + #13#10,
      False);
    // Icon/association refresh: ChangesAssociations=yes in [Setup].
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDirContent: TArrayOfString;
  DataDirPath: String;
  MarkerPath: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDirPath := ExpandConstant('{app}');
    MarkerPath := ExpandConstant('{app}\data_dir.txt');
    if FileExists(MarkerPath) then
    begin
      if LoadStringsFromFile(MarkerPath, DataDirContent) and
         (GetArrayLength(DataDirContent) >= 1) then
      begin
        if Trim(DataDirContent[0]) <> '' then
          DataDirPath := Trim(DataDirContent[0]);
      end;
    end;

    SuppressibleMsgBox(FmtMessage(CustomMessage('UninstallSummary'), [DataDirPath]),
      mbInformation, MB_OK, IDOK);
  end;
end;
