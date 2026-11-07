# 光学站

本指南的大部分内容讲的是 MAST 在 STM 上的用法，也就是 MAST 驱动 Nanonis 控制器。MAST 也能运行在
**光学站**上：一台带共聚焦拉曼 / SHG 显微镜（WITec）以及激光器、功率计、温控的电脑，**完全没有 STM，
也没有 Nanonis**。它是同一个服务，作业、令牌、急停、记录和 MCP 工具都一样。变的是你看到的内容，
以及哪些步骤要由人来做。这一页讲的就是这个差别。

## 怎么知道你在哪种站上

和平常一样，先读 `mast_briefing`。光学站上它的第一段 `station` 会写明 `Station profile: optical`，
列出适用的规则和已配置的光学设备。`GET /api/health`（给操作员看的）里的 `station_profile` 报告同样的内容。
画像是一个设置（`station_profile`，或环境变量 `MAST_STATION_PROFILE`），由操作员选择；它要重启 MAST
服务后才完全生效，因为有哪些后台线程是在启动时决定的。改这个设置要操作员的管理 PIN（它属于受 PIN 保护的
设置）：这不是你该做的事，agent 不改画像。

## 有什么不同

| | STM 站 | 光学站 |
|---|---|---|
| 仪器技能 | Nanonis 技能、STM 组合技能、STM 分析 | 光学台、WITec、激光器、功率计、温控、操作员提示，以及通用的文献 / 写作 / 实验设计工具 |
| `mast_observe` | 最新的扫描图和 Nanonis 读数（偏压、电流、Z） | 最新的一条谱（一张图和它的数字）和光学设备的状态 |
| 做事 | `mast_act` 动作或 `mast_run` | 只有 `mast_run`：`mast_act` 里的命名动作（扫描、退针……）会被 `not_available_on_this_station` 拒绝 |
| 简报的段 | tip、instrument、live、prefs、recent files …… | station、optical state、operator prompts、recent spectra ……（没有 tip、instrument、live、prefs、recent files） |
| Nanonis | 连接、轮询、看守 | 不用：不拨号，也没有 Nanonis 线程 |

其余一切不变：一动一观；动作作为作业运行；`mast_wait`；产数据的技能前要先 `mast_scope`（采谱需要样品）；
笔记、GUIDE.md 和 WORKING.md；`mast_cancel` 和 `mast_emergency_stop`。

只适用于 STM 的技能不会出现在 `mast_find_skills` 里，对它 `mast_run` 会被 `skill_disabled` 拒绝。
这是这台站的决定，不是故障：不要去找绕过的办法。

## 哪些事要由人来做

光学站有一些软件做不了的步骤，也有一些软件读不出来的状态。MAST 让操作员成为唯一能为它们作证的人。

- **WITec 写权限。** WITec Control 只在操作员按下 *COM Automation* 之后才允许远程客户端改东西，
  这会锁住 GUI 的人工输入，直到他按 *Revoke*。没有写权限时，要写的技能会明确失败。不要循环重试：
  用 `mast_ask_operator` 请操作员处理，再用 `mast_wait` 等答复。
- **手动光学状态。** 激发光路里装的是哪片 λ/2、Analyzer 里是哪片光学件、手动偏振片角度、物镜、截止滤光片，
  以及 1064 nm 激光的泵浦电流百分比，都是手动设置的。由操作员签署；你可以读
  （`ReadOpticalState`，或简报的 `optical_state` 段），但不能写。每条谱的 sidecar 都会记下采集时已签署的状态。
  实验需要换一片 λ/2 或换物镜时，请操作员去换，并签署新的状态。
- **开出光。** 从软件开出光需要两样东西：设备配置允许，并且操作员有一份当前有效的*激光区安全*签署
  （8 小时有效）。关出光永远允许，从不被拒绝。
- **串口是独占的。** 厂商软件开着的激光器或控制器，MAST 打不开。技能会失败并说出是哪个口；
  请操作员关掉那个程序。光学站上 MAST 不扫描串口。

你可以发问并等待，但永远不能替人作答。`mast_wait` 的 `operator_reply` 条件会等到答复
（把 MCP 工具超时设到至少 900 s）。

## `mast_observe` 返回什么

`latest_frame` 是实验文件夹里最新的一条谱，带一张图和下面这些数字：

| 字段 | 含义 |
|---|---|
| `peaks` | 最显著的几个峰，最强的在前：`nm`、`raman_shift_cm1`（激光线已知时）、`counts`、`prominence` |
| `max_counts`、`max_at` | 平均谱的最大值及位置 |
| `frame_max_counts`、`saturated`、`saturation_threshold` | 任何一帧里的最大值；达到阈值（默认 60000 counts）即饱和；被削顶的谱看起来也可能没问题 |
| `exposure_ms`、`frames`、`laser_nm` | 采集条件 |
| `file`、`path`、`age_s` | 文件在哪、多久以前采的 |

`instrument` 是状态回显：`microscope`、`lasers`、`temperature`、`power_meter` 里是 MAST **已经打开**的每台设备的状态；
配置了但没打开的设备列在 `devices_closed` 下。观察从不打开设备：打开设备是技能有意去做的事，
这样观察就永远不会把串口从厂商软件手里抢走。`optical_state` 列出已签署、已失效和未签署的槽位，
`operator_prompts_open` 是还在等答复的提问数。

## 一次采谱

```text
mast_briefing                      read the station section and the rules
mast_scope                         choose the experiment and the sample
mast_observe                       newest spectrum, device state, next_token
mast_find_skills "acquire spectrum"
mast_skill_card AcquireSpectrum
mast_run AcquireSpectrum after=<next_token>
mast_wait job                      the finished observation carries new_spectra and a picture
```

做下一步之前先读 `new_spectra` 里的数字：先看 `saturated`，再看峰。如果状态不是测量需要的样子，
就给操作员发一个提问，不要绕过去。
