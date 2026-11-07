# MAST 外部 agent 指南

MAST 通过 Nanonis 控制器驱动一台真实的扫描隧道显微镜（STM），仪器与分析动作都注册成一份
统一的「技能」目录，跑在同一套安全层后面。这份指南面向从 MAST 之外驱动它的一方 ——
不管是一个外部 AI agent（Codex、Claude Code 或其他编程 agent），还是一个人：都经由 MAST 的
**外部 agent API**（`/api/ext/v1`）操作，而不是经 MAST 自带的那些内部 agent。读者不需要
预先了解 MAST 的内部实现：安全操作所需的一切，要么在这份指南里，要么由 API 自己给出。

> **现状：** 外部 agent API 与 Claude Code 插件是 6.5.0 加进来的，在单元测试和一台机器上的
> 端到端测试（真 HTTP、真 MCP 服务进程、仪器用替身）里验证过。7.0 加上了下面说的视觉回路，
> 并接入了 Codex；这些部分在 STM-Bench 模拟器上做过端到端测试。**这一切都还没有在真实仪器上
> 跑过。** 它们执行的每个技能走的都是 MAST 自己的 agent 在仪器上用的同一个执行层，但在真实
> 硬件上的头几次会话请有人在旁看着。

推荐用 Claude Code 搭配 Claude Opus 5.5 来驱动 MAST。

## 7.0 新增

7.0 的主题是「外部 agent 加 VISTA」。VISTA 是给多模态模型用的一套视觉回路：agent 直接看仪器
自己的图像，而不是读一段替它写好的文字摘要；它可以把任何一张旧帧调回来、放大细看，精确的数
由测量工具给出，还能自己记笔记。这套回路跑在 MAST 服务端里，所以不管用哪个客户端，帧档案、
工具和规则都是同一套：

| 内容 | 工具 | 详见 |
|---|---|---|
| 客户端用 Claude Code（推荐）或 Codex | `setup_clients.py codex`、`mast` 插件 | [快速上手](01-quickstart.md) |
| 按实验存放的无损帧档案，随时重新渲染、放大 | `mast_observe`、`mast_inspect` | [概念](02-concepts.md) |
| 从归档数据里读精确数值、做测量 | `mast_read_values`、`mast_line_profile`、`mast_fft_peaks`、`mast_detect_blobs`、`mast_diff`、`mast_stats` | [概念](02-concepts.md) |
| 一动一观，由服务端执行 | `mast_act`，以及带 `after` 的 `mast_run` | [运行规则](03-operating-rules.md) |
| 动作以作业方式运行，等待在服务端进行 | `mast_wait` | [概念](02-concepts.md) |
| agent 自己的笔记，以及实验的历史 | `mast_read_guide`、`mast_write_guide`、`mast_read_working`、`mast_write_working`、`mast_history` | [记录与上下文](04-records-and-context.md) |
| 盯着每一张新帧的探测器 | `mast_detector_create`、`mast_detectors` | [概念](02-concepts.md) |
| 由操作员启动的无人值守 Claude Code 运行 | `POST /runner/start` | [概念](02-concepts.md) |
| 操作员的文献库 | `mast_literature_search` | [概念](02-concepts.md) |

6.5.0 提供的一切都还在，用法也没有变。

## 面向谁

任何从 MAST 之外驱动它的一方：经 MCP 接入的 AI 编程 agent（Codex、Claude Code 插件，或者别的
MCP 客户端）、一段直接说 HTTP 的脚本，或者手打 `curl` 命令的人。这份指南里的规则对它们同样
有效 —— API 不知道、也不关心连接的另一端是什么。

## 它是怎么运作的

MAST 暴露的每一项能力 —— 读一个数、跑一次扫描、修一次针尖、存一份数据 —— 都是一个
带参数、单位与安全级声明的**技能**。不管从哪扇门进来，每一次技能调用都会经过同一个
执行收口：中止检查、样品门、运行模式闸、五条写死的物理安全硬闸、全局参数包络、前置
条件检查、仪器仲裁，顺序如此。外部 agent API 让你检索这份目录、执行前读一份技能的完整
卡片、把一次执行当作异步**作业**提交后轮询到结束、取原始数据文件、给操作员留笔记和问题、
收尾时写一份交接报告。**简报**端点一次调用就给你 MAST 自己的 agent 每一轮都能看到的那批
上下文：针尖、实时读数、操作员的偏好、最近的动作，以及你自己尚未得到答复的请求。

从 7.0 起，agent 对仪器的动作还要经过视觉回路。每一次观测 —— 一个动作的结果，或者用
`mast_observe` 单纯看一眼 —— 都带着最新的图像和一个一次性的**观测令牌**，下一个动作必须
带上这个令牌：agent 没拿到上一个动作的结果之前，任何动作都不会执行。每一帧都按实验无损归档，
agent 可以把任何一张旧帧调回来、放大其中一块、读出精确数值，量距离、周期、台阶高度和前后
变化。要跑一阵子的动作以作业方式执行，`mast_wait` 在服务端等着：等作业结束、新帧、告警、
操作员答复、探测器命中或者定时到点。

## 阅读顺序

| 文件 | 读它做什么 |
|---|---|
| [01-quickstart.md](01-quickstart.md) | 安装 7.0，接上 Codex 或 Claude Code（或者别的 MCP 客户端、纯 HTTP），经 Tailscale 远程接入，以及从简报到交接的完整首次会话 |
| [02-concepts.md](02-concepts.md) | 技能、执行收口、安全级、运行模式、作业；以及视觉回路：工作区、帧、观测令牌、等待、探测器、运行器 |
| [03-operating-rules.md](03-operating-rules.md) | 让一次会话保持安全的规则，附每条背后的理由 |
| [04-records-and-context.md](04-records-and-context.md) | MAST 记录你的哪些动作、工作区里留下什么，以及都记到哪里 |
| [05-authoring-skills.md](05-authoring-skills.md) | 造一个组合技能，或者提议一个新技能 |
| [06-contributing-skills.md](06-contributing-skills.md) | 把一个技能投稿进社区贡献树 |
| [07-api-reference.md](07-api-reference.md) | 自动生成的逐端点参考 |
| [08-troubleshooting.md](08-troubleshooting.md) | 出问题时查：现象、原因、处理办法 |
| [09-optical-station.md](09-optical-station.md) | 在光学站（拉曼 / SHG）上运行 MAST：和 STM 有什么不同、哪些事要操作员做、`mast_observe` 返回什么 |
| [10-optical-layout.md](10-optical-layout.md) | 操作员的光路图：怎么读（`mast_optical_layout`）、追迹和叙述说了什么、怎么评论、为什么你不能改图 |
