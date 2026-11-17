# 通用站

有些实验室在一台既没有 STM（没有 Nanonis）、也没有 WITec 显微镜的电脑上运行 MAST：一台光谱仪、一两台光源、
功率计、温控，再加几个串口传感器。这就是**通用站**。它和 STM 站、光学站是同一个服务，作业、令牌、急停、
记录和 MCP 工具都一样。这一页讲的是差别。[光学站那一页](09-optical-station.md)里关于操作员和激光的内容，
大多在这里同样适用。

## 怎么知道你在哪种站上

和平常一样，先读 `mast_briefing`。通用站上它的第一段 `station` 会写明 `Station profile: generic`，
列出适用的规则和已配置的设备。`GET /api/health`（给操作员看的）里的 `station_profile` 报告同样的内容，
`station_capabilities` 列出这台站有什么。画像是一个设置（`station_profile`，或环境变量
`MAST_STATION_PROFILE`），由操作员选择；它受管理 PIN 保护，要重启 MAST 服务后才完全生效。agent 不改画像。

## 有什么不同

| | STM 站 | 光学站 | 通用站 |
|---|---|---|---|
| 仪器 | 接 Nanonis 控制器的 STM | WITec、激光器、功率计、温控 | `config/optical_instruments.json` 里的设备和 `config/environment_sensors.json` 里的传感器 |
| 仪器技能 | Nanonis 技能、STM 组合技能、STM 分析 | 光学台、WITec、光源、测量协议、操作员提示 | 光学台、光源、测量协议、操作员提示；没有 WITec 技能 |
| `mast_observe` | 最新的扫描图和 Nanonis 读数 | 最新的一条谱和光学设备 | 最新的一条谱、设备、环境读数 |
| 做事 | `mast_act` 动作或 `mast_run` | 只有 `mast_run` | 只有 `mast_run`：`mast_act` 里的命名动作会被 `not_available_on_this_station` 拒绝 |
| 简报的段 | tip、instrument、live、prefs、recent files …… | station、optical state、operator prompts、recent spectra …… | station、environment、optical state、operator prompts、recent spectra …… |
| 串口自动探测 | 默认开 | 始终关 | 关，除非操作员打开 |

其余一切不变：一动一观；动作作为作业运行；`mast_wait`；产数据的技能前要先 `mast_scope`（采谱需要样品）；
文献与写作工具；笔记、GUIDE.md 和 WORKING.md；`mast_cancel` 和 `mast_emergency_stop`。通用站上急停会停下
光源、快门和位移台；这里没有 Nanonis 可停。

这台站没有对应硬件的技能（Nanonis 技能、WITec 技能）不会出现在 `mast_find_skills` 里，对它 `mast_run`
会被 `skill_disabled` 拒绝。这是这台站的决定，不是故障：不要去找绕过的办法。

## 哪些事要由人来做

- **要动手的步骤。** 换光学件、样品托或线缆，关掉厂商软件：用 `mast_ask_operator` 请操作员处理，再用
  `mast_wait` 等答复。你可以发问题、等答复，但永远不能替人作答。
- **手动状态。** 软件读不出来的状态由操作员签署。通用站一开始只有一个槽位 `laser_area_safe`；实验室在
  `config/optical_station.json` 里定义自己的槽位（装的是哪片滤光片、哪个物镜……）。用 `ReadOpticalState`
  或简报的 `optical_state` 段读它们；你永远不写。每条谱的 sidecar 会记下采谱时已签署的状态。
- **激光出光。** 从软件打开光源（`LaserEmissionOn`）要两样：设备配置允许，而且操作员有一份有效的
  `laser_area_safe` 签署（8 小时有效）。关出光（`LaserEmissionOff`）永远允许，永不被拒。
- **串口是独占的。** 厂商软件开着的设备，MAST 打不开。技能会失败并说明是哪个口；请操作员关掉那个程序。

## `mast_observe` 返回什么

`latest_frame` 是实验文件夹里最新的一条谱，带一张图，数字和光学站上一样（峰、最大计数、是否饱和）；
还没有谱时它是 `null`。别的文件（传感器日志、图片）在实验文件夹里：用 `mast_list_data` 列出来。

`instrument` 是状态回显：

| 字段 | 含义 |
|---|---|
| `lasers`、`temperature`、`power_meter`、`microscope` | MAST **已经打开**的每台设备的状态 |
| `devices_closed` | 已配置但没打开的设备；观测从不打开设备 |
| `environment` | 每个已配置传感器的最新读数：`value`、`unit`、`status`、`age_s` |
| `optical_state` | 手动状态里已签署、已过期、未签署的槽位 |
| `operator_prompts_open` | 还有几个问题在等操作员 |

## 串口自动探测

MAST 可以逐个探测空闲串口，找它认得的真空计和温度计（向每个口发一条身份查询和一帧 Modbus）。通用站上这项
探测是关的：这些口可能属于某台光源或某个厂商程序。`config/environment_sensors.json` 里列出的传感器
不管怎样都按配置打开。操作员可以在设置里打开探测（`serial_autodetect`）；下次启动或重新扫描时生效。

## 一次采谱

```text
mast_briefing                      read the station section and the rules
mast_scope                         choose the experiment and the sample
mast_observe                       newest spectrum, device state, environment, next_token
mast_find_skills "acquire spectrum"
mast_skill_card AcquireSpectrum
mast_run AcquireSpectrum after=<next_token>
mast_wait job                      the finished observation carries new_spectra and a picture
```

下一步之前先读 `new_spectra` 里的数字：先看 `saturated`，再看峰。状态不是这次测量需要的样子时，请操作员处理，
不要绕过去。
