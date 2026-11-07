# MAST 卫星（rig1 光谱电脑）

`MastSatellite.exe` 跑在 rig1 上，替 MAST 枢纽（rig2）托管这台电脑上的设备，并把新光谱文件传回 rig2：

| 设备 id | 驱动 | 硬件 | 接口（运行时晚绑定，编译期不引用任何厂家 DLL） |
|---|---|---|---|
| ccd | `lightfield` | PyLoN 100B + HRS-300 | LightField 6 Automation V5（反射加载 `%LIGHTFIELD_ROOT%`） |
| pol | `kinesis_kdc101` | KDC101 + PRM1-Z8 | Kinesis C API `Thorlabs.MotionControl.KCube.DCServo.dll`（`CC_*`） |
| pico | `picomotor8742` | New Focus 8742 | 8742 ASCII 命令；传输可换：USB（`usbdll.dll`）、CmdLib8742.dll、TCP 23 |
| xyz | `motion_manipulator` | UIRobot XYZ | **不能动，只读位置**：Motion_Manipulator 没有文档化的远程协议，卫星不发任何运动命令；配了 `coordinates_dir` 时，从它写的 `Coordinates_*.csv` 读最近一次运动的端点 |
| aotf | `ds1` | Brimrose DS1 | 串口 + 可配置命令模板；厂家协议到手前只有 status/stop |
| pmt | `spectrasense` | SpectraSense + SpectraHub | COM `SpectraSense.SpectraSense_Interface`（实验性，默认关） |
| spe | `spe_watcher` | 数据目录 | 文件监视 + SPE 3.0 文件尾解析 + 断点续传上传 |

每个设备都可以加 `"sim": true` 改用模拟驱动（行为对齐枢纽的 Python 参考卫星 `MASTv2/mast/satellite/sim/`）。协议与语义见 `docs/v2/design/ters_satellite_integration.md` §5。

目录：

```
satellite/
  MastSatellite/          C# 工程（SDK 风格，net48，x64，WinExe）
  config/                 rig1.satellite.json（契约 §5.5）、rig1.phaseB.satellite.json（只开 spe）、sim.satellite.json
  deploy/                 install.ps1、set-token.ps1、uninstall.ps1（PowerShell 5.1，纯 ASCII）
```

## 1. 构建（在构建机上）

需要 .NET 8 SDK。构建机上若只有 .NET 8 运行时，可以便携安装 SDK：

- **安装位置**：`%LOCALAPPDATA%\Microsoft\dotnet`（SDK 8.0.x，约 700 MB）。不改 PATH，不设 DOTNET_ROOT，不写注册表；PATH 上原有的 `C:\Program Files\dotnet` 不受影响。
- **来源只用官方**：dot.net 的 `dotnet-install.ps1`、微软 CDN、nuget.org，必要时可以经本机代理。**不用第三方 NuGet 镜像。** 万一 nuget.org 经代理也不通、只能临时用镜像，用完必须对每个包跑 `dotnet nuget verify --all <nupkg>`，确认作者签名是 Microsoft Corporation。
- **只用到一个微软包**：Microsoft.NETFramework.ReferenceAssemblies 1.0.3，连同它依赖的 `.net48` 包。它只在编译时提供 net48 的引用程序集，不进产物。核对【实证】：两个包都是从 api.nuget.org 还原的（`.nupkg.metadata` 的 source）；`dotnet nuget verify` 通过，作者签名是 CN=Microsoft Corporation，另有 NuGet.org 仓库副署。
- **回退**：删掉 `%LOCALAPPDATA%\Microsoft\dotnet` 即可。首次构建还会生成以下文件和目录，不需要可以一并删掉：
  - `%USERPROFILE%\.dotnet`
  - `%USERPROFILE%\.nuget\packages\microsoft.netframework.referenceassemblies*`
  - `%LOCALAPPDATA%\NuGet\v3-cache`
  - `%TEMP%\NuGetScratch`
  - `%APPDATA%\NuGet\NuGet.Config`（默认内容，只有 nuget.org 一个源）

```powershell
# 一次性安装（直连不通时加 -ProxyAddress <代理地址>）
& .\dotnet-install.ps1 -Channel 8.0 -InstallDir "$env:LOCALAPPDATA\Microsoft\dotnet" -NoPath

# 构建前设置的环境变量
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'       # 不发遥测
$env:DOTNET_NOLOGO = '1'                     # 不打印欢迎信息
$env:HTTPS_PROXY = 'http://<代理地址>'   # 只在首次还原、nuget.org 直连不通时需要

& "$env:LOCALAPPDATA\Microsoft\dotnet\dotnet.exe" build satellite\MastSatellite\MastSatellite.csproj -c Release -p:Platform=x64
```

产物在 `satellite\MastSatellite\bin\x64\Release\`：`MastSatellite.exe`（约 350 KB，调试符号已嵌入）和 `MastSatellite.exe.config`。产物只依赖 .NET Framework 4.8，不依赖 .NET 8；rig1 上不用装任何东西。

后备方案：系统自带的 `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe` 也能零安装编译，但它只支持 C# 5。现在的代码用了 C# 7–10 的语法（字符串插值、元组、out var、模式匹配等），要走这条路，得先把代码改写成 C# 5 子集。SDK 已经装好，这一步没有做。

## 2. 测试

```powershell
# 离线自检（20 项）：协议编解码、journal 幂等、限速器、SPE 解析（rig1 真实文件尾样例）、模拟设备、
# 光谱仪 configure 的词汇与拒收、safe 不打断人工采集、axis_state 的 cmd_id、xyz 的 Coordinates CSV（含 rig1 的真实文件），
# 以及对一个进程内假枢纽的完整协议往返（断线重连、死人开关、E-STOP、续传、401/4406/4409 退避、卡死命令）
satellite\MastSatellite\bin\x64\Release\MastSatellite.exe --selftest
#   真实样例（rig1 拷回的 SPE 文件尾、Coordinates CSV）在本地样例目录里，不在仓库里。
#   自检会从当前目录往上找本地样例目录；
#   别的机器上用 --spe-samples <dir> 指定 SPE 文件尾样例目录（*.footer.xml），找不到时这一项记为 SKIP；
#   xyz 那项里读真实 CSV 的一段找不到样例时自动略过，合成数据的部分照常跑。

# 对真枢纽的端到端测试（exe 不存在就跳过）；在仓库根目录运行
$env:PYTHONPATH = "$PWD\MASTv2"; $env:PYTHONUTF8 = "1"
& .venv-v2-py313\Scripts\python.exe -m pytest tests/v2/satellite/test_csharp_e2e.py -q -p no:cacheprovider
```

注意：exe 是 GUI 子系统程序。在 PowerShell 里要拿退出码，用 `Start-Process -Wait -PassThru -NoNewWindow`，或者把输出管道出去（`| Out-Host`）；直接 `& exe` 时 PowerShell 不等它退出。

## 3. 部署到 rig1（维护窗口，用户在场）

rig1 在用，大流量传输可能引起不稳定：只拷 exe、config 和三个脚本（合计约 0.4 MB），不要往 rig1 传大文件。

1. **枢纽侧（rig2）**：先把 MAST 升级到带卫星枢纽的版本。rig2 上没有 Python，下面几条走装好的 `MAST.exe` 自带的 `--satellite-admin` 入口（子命令与 `python -m mast.satellite.admin` 相同）。在 PowerShell 里（本机或经远程执行）**一律经 `cmd /c` 运行**：MAST.exe 是窗口程序，直接 `& MAST.exe` 时 PowerShell 不等它退出，拿不到退出码，经 ssh 时连输出都收不到；`cmd /c` 会等它结束，输出和退出码都能拿到。
   - 生成令牌：
     ```powershell
     cmd /c 'C:\MAST\MAST.exe --satellite-admin new-token rig1 --note "Spec 光谱电脑"'
     ```
     标准输出只有一行，就是令牌，只显示这一次。说明文字走标准错误，里面写着哈希存进了哪个 `satellites.json`：应当是数据根下那份（`<数据根>\config\satellites.json`），不是安装目录。已有令牌时会拒绝（退出码 2），要轮换就加 `--rotate`，旧令牌立刻失效。没看到令牌那一行，就当这次没生成，用上面的写法加 `--rotate` 重跑。在没有标准输出的情况下，命令什么都不改，退出码为 3。
   - 读钉值：
     ```powershell
     cmd /c 'C:\MAST\MAST.exe --satellite-admin show-pin'
     ```
     输出是 LAN 证书公钥的 SHA-256（64 位 hex）。证书在 `<数据根>\api key\lan_cert.pem`，MAST 以 LAN 模式启动过一次才会有。
   - 核对：`cmd /c 'C:\MAST\MAST.exe --satellite-admin list'`，不显示令牌。运行中的 MAST 在 2 s 内读到新配置，不用重启。
   - 令牌只经这条命令的输出交出：rig2 上只存它的 SHA-256，MAST 的日志里也没有它。
2. **拷文件**：把 `MastSatellite.exe`、`MastSatellite.exe.config`、`config\`、`deploy\` 拷到 rig1 上同一个目录，例如 `C:\Temp\mast-sat\`。下一步用 `-SourceDir` 指向这个目录；不传时，`install.ps1` 默认去 `deploy\..\MastSatellite\bin\x64\Release` 找，那是仓库里的构建目录。
   - 拷完先核对：在 rig1 上对 `MastSatellite.exe` 和 `MastSatellite.exe.config` 运行 `Get-FileHash -Algorithm SHA256`，结果必须与构建机上本次 `dotnet build` 产物（`satellite\MastSatellite\bin\x64\Release\` 里的这两个文件）的值逐位一致。对不上就重拷，不要安装。哈希每次重编都会变，所以这里不写具体值，每次以构建机当场算出的为准。
     ```powershell
     Get-FileHash -Algorithm SHA256 C:\Temp\mast-sat\MastSatellite.exe, C:\Temp\mast-sat\MastSatellite.exe.config
     ```
3. **安装**（管理员 PowerShell）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\deploy\install.ps1 -UserName <用户名> -SourceDir C:\Temp\mast-sat -ConfigTemplate .\config\rig1.phaseB.satellite.json
   ```
   - `-UserName` 和 `-ConfigTemplate` 是必填参数：站点值只写在这里和 `config\*.json` 里，脚本和代码里没有。
   - 程序装到 `C:\MAST-Satellite\`；状态目录是 `%ProgramData%\MAST-Satellite\`（存配置、令牌、journal、日志、上传清单）。
   - 注册计划任务"MAST Satellite"：该用户登录时启动，仅在用户登录时运行，不提权。
   - 已有的 `satellite.json` 不会被覆盖。
4. **写令牌**（以该用户身份，在 rig1 的桌面上，不提权）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\deploy\set-token.ps1
   ```
   粘贴第 1 步的令牌，它会用 DPAPI（CurrentUser）加密写到 `%ProgramData%\MAST-Satellite\token.dpapi`。SSH 下用密钥登录时 DPAPI 可能不可用，请在控制台运行。实在不行可以加 `-Scope LocalMachine`，但这样本机任何进程都能解开令牌，安全性较低。
5. **填钉值**：把第 1 步的 64 位 hex 填进 `%ProgramData%\MAST-Satellite\satellite.json` 的 `hub.spki_sha256`。没填或填错时卫星拒绝连接，令牌也不会发出去。
6. **自查**：`C:\MAST-Satellite\MastSatellite.exe --check-config | Out-Host`。它校验配置，解一次令牌，并检查各厂家文件是否存在，不加载、不打开任何东西。看到 `READY` 再继续。
7. **启动**：`Start-ScheduledTask -TaskName 'MAST Satellite'`，或者注销后重新登录。托盘出现图标，窗口显示"枢纽：已连接"；rig2 的 MAST 页面能看到卫星和设备。
8. **阶段 B 验收**：只开了 `spe`。在 LightField 里手动采一条谱，它会自动出现在 rig2 的 MAST 里，元数据带 STM 状态。阶段 B 的配置把上传限速设为 512 KiB/s，补传窗口设为 6 h：先保守设置，确认稳定后再调到 1 MiB/s、24 h。
9. **以后打开设备**：把 `satellite.json` 换成 `rig1.satellite.json` 的设备段，逐个把 `enabled` 改成 true，再按第 9 节的维护窗口实测清单逐项实测。打开设备前，先关掉占着该设备的程序：LightField 手动窗口、Kinesis、PicomotorApp、SpectraSense。
   - Picomotor 默认走 `"transport": "usb"`，手动面板上只有 jog、move_abs、set_zero、stop。要有速度、加速度和 abort 按钮，把 pico 的 transport 改成 `"cmdlib"`（走 CmdLib8742 文档化的方法），并按第 9 节先实测这条传输。改完重启卫星，caps 会随之变化。

卸载：`powershell -ExecutionPolicy Bypass -File .\deploy\uninstall.ps1`。会优雅停止、删计划任务和程序目录；状态目录默认保留，加 `-RemoveState` 才删。被监视的数据目录永远不动。

## 4. 配置（`satellite.json`）

| 键 | 说明 |
|---|---|
| `satellite_id` | 与枢纽 `satellites.json` 里的 id 一致 |
| `hub.url` | `wss://<枢纽地址>:7862/ws/satellite/v1`。地址只出现在这一处；`_comment_url` 只是注释，程序不读 |
| `hub.spki_sha256` | 枢纽证书 SubjectPublicKeyInfo 的 SHA-256，小写 hex。与 `show-pin` 用的 `mast.net.tls.spki_sha256_of` 同一定义，等同于 `openssl x509 -pubkey \| openssl pkey -pubin -outform der \| sha256sum`。只校验公钥，不校验 CA 和主机名 |
| `hub.token_file` | DPAPI 加密的令牌文件 |
| `hub.backoff_max_s` / `auth_retry_s` / `proto_retry_s` | 断线重连的指数退避上限（默认 30 s）/ 401、403、4403 和 4409（同 id 被新连接顶替）之后的等待（60 s）/ 4406 之后的等待（300 s） |
| `state_dir` | journal、日志（`logs\`，保留 14 天）、上传清单、Picomotor 步数计数 |
| `upload.max_bytes_per_s` / `chunk_bytes` | 上传限速（与枢纽 welcome 给的值取较小者）/ 块大小（≤512 KiB） |
| `event_buffer.max_events` / `max_bytes` | 未确认事件缓冲（默认 4000 条 / 32 MiB，满了丢最旧的） |
| `devices[]` | `id`、`driver`、`enabled`、`sim`、`operator_hold`（默认 false：启动时就处于人工接管），其余是驱动自己的键（见下） |

驱动键（括号里是默认值；标"必填"的是站点值，代码里没有默认值）：

- `lightfield`：`lightfield_root`（`%LIGHTFIELD_ROOT%`）、`experiment`（启动后 Load 的实验名，空 = 不 Load）、`visible`（true）、`suppress_dialogs`（true，无人值守必须开）、`settable_keys`（`set_settings` 白名单，LightField 设置键原文）、`laser_line_nm`（不填就报 null）、`require_temperature_locked`（true，CCD 没锁温就拒绝采集）、`manual_frame_max_hz`（5，窗口里手动采集的实时帧限频）、`start_timeout_s`（20，Acquire 之后多久没开始就报错）、`grating_timeout_s`（90）、`center_timeout_s`（60）。
- `kinesis_kdc101`：`serial`（必填）、`kinesis_dir`（`C:\Program Files\Thorlabs\Kinesis`）、`min_deg`/`max_deg`（软限位）、`counts_per_deg`（1919.64，仅在 Kinesis 换算函数不可用时使用，同时用来交叉核对）、`require_home_for_abs`（true：上电后回零前不接受 move_abs）、`home_timeout_s`（60）、`poll_ms`（100）、`retry_open_s`（30，设备被 Kinesis 界面占着时多久重试一次）、`max_velocity_deg_s` / `max_accel_deg_s2`（25 / 25，set_velocity 的上限；按 PRM1-Z8 的规格取的，推断，维护窗口核对）。
- `picomotor8742`：`transport`（`usb`｜`cmdlib`｜`tcp`）、`usb_dll`、`usb_product_id`（0x4000）、`serial`（在 `newp_usb_get_device_info` 返回的设备描述里匹配，多台 8742 时用）、`cmdlib_path`、`device_key`、`host`/`port`/`timeout_ms`（tcp，23，2000）、`address`（RS-485 从机地址）、`axes`（4）、`max_steps_per_cmd`（2000，jog 和 move_abs 的单次上限）、`steps_per_s`（2000，只用于估超时）、`max_velocity_steps_s` / `max_accel_steps_s2`（2000 / 200000，set_velocity / set_accel 的上限；推断，待手册核对）。
- `motion_manipulator`：`coordinates_dir`（站点值，不填就没有位置）、`coordinates_poll_s`（1）、`limits_mm`（站点值，代码里没有默认值）、`max_step_mm`（0.5）。
  - 卫星只读文件，不发命令，所以 caps 里没有 move_*。
  - 跟随最新的 `Coordinates_yyyyMMddHHmmss.csv`（每个会话一份，文件名时刻 = 该会话第一行的时刻）。status 给出最后一行的 `x_mm/y_mm/z_mm`、`position_event`（BeginMotion / EndMotion / Configured）、`position_at_ms`、`position_age_s`。这是最近一次运动的端点，不是实时位置。
  - `moving` 是推断的：最后一行是 BeginMotion 且不到 120 s 时为 true；超过 120 s 为 null（未知）。
  - 运行中新出现的行会发 `axis_state`；BeginMotion 和 Configured（坐标重定义，不是运动）还会发 `manual_activity`。
  - 表头与已知的 17 个字段不一致时，不猜格式：设备进 error，并报 `coordinates_problem`。
  - **卫星停不了这个台子**：CSV 显示静止时，stop 和 E-STOP 才算成功；可能在动或不知道时，stop 回 `not_supported`，E-STOP 在 `estop_ack.errors` 里列出 xyz。要停，在 Motion_Manipulator 里按 STOP。
- `ds1`：`port`（配了 `commands` 之后必填）、`baud`（128000）、`commands`（命令模板，如 `{"set": "...{channel}...{frequency_mhz}...", "rf_off": "..."}`；没有就一律 not_supported，也不打开串口）、`terminator`、`reply_terminator`、`expect_ok`。
- `spectrasense`：`channel`（1）、`max_hv`（1000）、`safe_hv_off`（true）。
- `spe_watcher`：`root`（必填）、`backfill_hours`（24）、`stable_s`（2）、`scan_interval_s`（300）、`extensions`（`[".spe", ".csv"]`）、`max_file_mb`（2048）。
- 模拟驱动的附加键：`readout_ms`、`data_dir`（sim 光谱仪存 SPE 的目录）、`accel_deg_s2` / `accel_steps_s2`（初始加速度）、`sim_manual_every_s` / `sim_manual_frames` / `sim_manual_exposure_ms`（每隔几秒模拟一次"有人在 LightField 窗口里点了采集"，默认不模拟，对应参考卫星的 manual_acq_every_s）、`speed_deg_s`、`home_time_s`、`sim_start_homed`、`steps_per_s`、`speed_mm_s`、`max_hv` 等。

## 5. 钉证书与换光纤

- 卫星**钉的是 MAST LAN 证书的公钥**（SPKI），不是证书本身，也不看主机名和 SAN。IP 变化时 MAST 会重签证书；只要重签时复用原私钥（契约陷阱 10），钉值就不变。已实测：新版 MAST 重签时复用私钥，SPKI 不变。
- **默认走 LAN**：`wss://198.51.100.11:7862/ws/satellite/v1`。Tailscale 账号切换后地址会变，所以 Tailscale 只作备用：配置里的 `_comment_url` 记着备用地址，而且只有 rig1 也在同一个 tailnet 时才可用。
- **换光纤时只改 `hub.url`**，例如改成 `wss://<rig2 的光纤 IP>:7862/ws/satellite/v1`，然后重启卫星（托盘退出后 `Start-ScheduledTask`）。证书 SAN 里有没有这个 IP 不影响卫星。只有私钥换了，才需要按第 3 节第 1 步重新读钉值、填进去。
- 没有 rig2 现场条件时，`MastSatellite.exe --print-spki <host>:7862` 可以连一次读出对方证书的钉值。这是首次信任（TOFU），必须和 rig2 上 `show-pin` 的输出逐位对照之后才能使用。

## 6. 本地窗口、人工接管、停止

- 窗口显示：连接状态、最近一次 ping、重连次数、事件缓冲、上传进度、设备列表、日志尾。关闭窗口只是隐藏到托盘；只有托盘菜单"退出卫星"或 `--stop` 才会真正结束。
- **人工接管**：在设备行前打勾，或在配置里给该设备写 `"operator_hold": true`（启动时就处于接管状态）。接管期间，MAST 对该设备的命令只放行 stop、safe、abort（都只是停）和只读动作（status、wavelength_axis、get_settings），其余回 `operator_hold`。勾上时，正在执行的枢纽命令会被停下。
- **MAST 正常关停**时，枢纽给每个设备发 `safe{reason}`，不发 estop（契约 §5.3 第 7 条）。safe 的语义与死人开关相同：只停枢纽发起的运动和采集，有人在 LightField 窗口里做的采集照常进行。被停下的枢纽命令以 `cancelled` 结束，message 带上这个 reason。E-STOP 和看门狗异常仍然发 estop，什么都停。
- **本机操作**：选中设备行，可以执行驱动提供的本地动作：重启 LightField、重新连接 KDC101、重新连接 Picomotor。
- **优雅停止**：`MastSatellite.exe --stop [--config ...]`，任何会话（包括 SSH）都能用。它写一个停止请求文件，卫星 1 s 内收到后：结束在途命令（结果写进 journal）、停设备、释放厂家句柄、以 1000 关闭连接，然后退出。卫星连着的厂家程序不要强杀。
- 没有桌面会话时（session 0，比如被 SSH 启动），卫星以无窗口模式运行，并拒绝启动 LightField 和 SpectraSense，因为它们必须在登录用户的桌面里。
- **E-STOP 的回执**（契约 §5.3 第 5、12 条）：
  - `stopped` 列出每个 stop 正常做完的设备，本来就空闲的也算；
  - `errors` 是 `[{device, message}]`；
  - `active` 是当时确实在动或在采的设备，只作信息：枢纽同时还会给每个设备发 stop，谁先打断作业是竞态。
- **手动控制面板的动作**（契约 §13.1）：枢纽完全按设备上报的 `caps` 生成面板按钮，所以 caps 只列有文档依据、已实现的动作。
  | 设备 | 动作 | 实现依据 | caps 里有没有 |
  |---|---|---|---|
  | KDC101（真、模拟） | `set_velocity{deg_s, accel_deg_s2?}` | CC_SetVelParams 写入，CC_RequestVelParams + CC_GetVelParams 读回；单位用 CC_GetDeviceUnitFromRealValue / CC_GetRealValueFromDeviceUnit（unitType 1 速度、2 加速度）。读回与设定相差 1% 以上就报错；Kinesis 单位换算不可用时回 unavailable | 有 |
  | KDC101（真、模拟） | `identify` | CC_Identify（控制器指示灯闪） | 有 |
  | 8742（真，任何传输；模拟） | `move_abs{axis, steps}` | 目标是 set_zero 以来卫星记的累计步数，走"目标减当前计数"的相对移动，与 jog 同一条 PR 路径、同一个单次上限 | 有 |
  | 8742（真，transport=cmdlib；模拟） | `set_velocity{axis, steps_s}`、`set_accel{axis, steps_s2}`、`abort` | CmdLib8742 文档化的 SetVelocity/GetVelocity、SetAcceleration/GetAcceleration、AbortMotion（Picomotor Samples.pdf 第 5 节），写完读回 | 有 |
  | 8742（真，transport=usb/tcp） | `set_velocity`、`set_accel`、`abort` | 需要 ASCII 的 VA、AC、AB。8742 用户手册原文不在手边，**待手册核对**，核对之前不进 caps | 没有 |

  返回形状按 §5.4：转台回 `{position_deg, moving, homed, enabled, velocity_deg_s, accel_deg_s2}`；8742 回 `{axes: {"<n>": {steps, moving, velocity_steps_s, accel_steps_s2}}}`，真 8742 只有读得到时才带速度和加速度。

  约束：
  - 速度、加速度的取值范围在卫星这边也检查，越界回 `invalid_args`（与参考卫星一致）；move_abs 超过单次上限回 `limit`。
  - set_velocity / set_accel 要求设备空闲：运动中回 `device_busy`。
  - move_abs 是运动，受死人开关约束，运动期间 axis_state 带 cmd_id。
  - abort 和 stop 一样设备在忙时也收，所有轴立即停、不减速，正在跑的 jog / move_abs 以 cancelled 结束。
  - 人工接管期间，set_velocity、set_accel、identify、move_abs 回 `operator_hold`；abort 和 stop、safe 一样照常放行。
  - E-STOP 时：cmdlib 传输用 AbortMotion；usb/tcp 发 AB，并同时给每个轴发 ST 兜底，因为 AB 还没有对过手册。
- **axis_state 与命令**（契约 §5.3 第 11 条）：转台、Picomotor 和模拟的 xyz 在执行枢纽发来的运动命令期间，axis_state 带 `cmd_id`，枢纽据此把命令显示为 running。运动一结束（走完、stop、死人开关），立刻补发一条 `moving=false`、不带 cmd_id 的 axis_state。空闲和人工运动都不带 cmd_id。
- **光谱仪 configure 只认 LightField 自己的枚举名**（契约 §5.4）。`satellite/MastSatellite/Devices/SimDevices.cs` 里 `SpectrometerArgs` 的几个名单数组被 `tests/v2/satellite/test_sim_devices.py` 按正则解析，并和 Python 参考卫星的名单比对：改名、改格式或增删值时，要同时改参考卫星和那条测试。不认识的键、取值或类型一律回 `invalid_args`，并列出可选名字；一项被拒，整条命令都不生效，不会静默忽略。

## 7. 排障

| 现象 | 先看 |
|---|---|
| 窗口显示"被枢纽拒绝" | 令牌不对，或枢纽上这颗卫星被停用（401/403/4403），或协议版本不符（4406）。日志里有 HTTP 码或关闭码。令牌轮换后要重跑 set-token.ps1 |
| 日志出现 `hub connect failed (HTTP 503 ...)` 或 `closed by hub: 1001` | 枢纽在关停或重启（MAST 正常退出前会先给每个设备发 safe，再以 1001 关连接）。不是凭据问题，卫星按普通退避自动重连，不用处理 |
| 日志出现 `(4409)`：`hub handed satellite id ... to a newer connection` | 另一处有一份用同一个 satellite_id 的卫星（或拷走的配置）在连枢纽。卫星会等 60 s 再连，免得两边互相顶替；找到并停掉另一份 |
| 日志出现 `pin mismatch: presented spki_sha256=...` | `hub.spki_sha256` 与枢纽证书不符。核对 rig2 上 `show-pin` 的输出；证书是否被不复用私钥地重签过 |
| `无法读取连接 MAST 的令牌` | 令牌文件不是用当前 Windows 用户写的（DPAPI CurrentUser）；以该用户重新运行 set-token.ps1 |
| ccd：`a LightField window opened by hand is running` | 手动打开的 LightField 占着相机。关掉它，卫星 2 s 内会自己托管一个新的 LightField 窗口；之后在那个窗口里操作 |
| ccd：`needs the logged-on user's desktop` | 卫星跑在 session 0 了。由计划任务（登录时）启动，不要从 SSH 启动 |
| ccd：`CCD temperature not locked` | 液氮不够或相机没冷却（−120 °C Locked 才采集）；`require_temperature_locked` 可以关，但不建议 |
| pol：`not listed (... held by another program such as the Kinesis GUI)` | 关掉 Kinesis 界面；30 s 内自动重连，或在窗口里点"重新连接 KDC101" |
| pol：`stage not homed since power-up` | 先让 MAST 发 `home`（最长约 36 s，会转一整圈，宜遮光或抬针时做） |
| pico：`newp_usb_open_devices ... failed` | 关掉 PicomotorApp；USB 驱动应是 Newport USB Driver（WinUSB） |
| spe：`watch root not found` | `root` 路径不对 |
| xyz：`unrecognized Coordinates CSV header` | Motion_Manipulator 换了 CSV 格式（比如升级了版本）。卫星不猜，位置报 null；把新文件的前几行发给开发 |
| xyz：stop 回 `not_supported`（`cannot stop the XYZ stage`） | 卫星停不了这个台子，而 CSV 显示它可能在动。在 Motion_Manipulator 里按 STOP |
| ccd：configure 回 `... is not a OutputSignal name (one of: ...)` | 取值不是 LightField 的枚举名。用列出的名字之一，例如 `EffectivelyExposing`，不是 `EffectiveExposure` |
| 文件没出现在 rig2 | 窗口里看"上传：排队/当前"；状态目录 `uploads-spe.jsonl` 是已传清单（删掉它会触发重传最近 `backfill_hours` 小时的文件，枢纽按 sha256 去重） |
| 设备显示 error：`... looks hung` | 一条厂家调用在超时后 30 s 还没返回。用本机操作重连该设备，或重启卫星，并把日志发给开发 |

日志：`%ProgramData%\MAST-Satellite\logs\satellite-YYYYMMDD.log`。命令记录：同目录 `journal.jsonl`（最近 1 万条 request_id 的状态与结果）。

## 8. 边界

- 只用厂家文档化的接口：SDK 手册、头文件、类型库、示例、公开的 .NET 元数据。不反编译、不反汇编厂家程序。没有公开协议的设备（DS1；Motion_Manipulator 也暂时算在内）做成占位，要问厂家的问题列在第 10 节。
- 被监视的数据目录只读：文件以共享读方式打开，从不修改、移动或删除。
- 令牌只以 DPAPI 密文落盘；连接只认钉住的公钥；明文 `ws://` 仅限 127.0.0.1，且必须显式加 `--allow-plain-loopback`（测试用）。
- 激光不在控制范围内：卫星能停采集、停轴、关 PMT 高压，但关不了激光。XYZ 台子也停不了（见第 4 节的 motion_manipulator）。
- Motion_Manipulator 的 `Coordinates_*.csv` 只读：以共享读方式打开，不加锁，不写入。

