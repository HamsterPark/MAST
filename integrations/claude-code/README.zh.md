# Claude Code 的 MAST 插件

[English](README.md)

在 Claude Code 里驱动由 MAST 控制的扫描隧道显微镜（Nanonis），走 MAST 的外部 agent 接口
（`/api/ext/v1`）。请从本公开快照的本地目录安装插件。

> **现状：** 6.5.0 的二十个工具在单元测试里、以及一台机器上用仪器替身做的端到端测试里验证过；
> 7.0 新增的视觉回路工具在 STM-Bench 模拟器上做过端到端测试，7.0.1 的改动在 Nanonis 模拟器上
> 做过端到端测试。这一切都还没有在真实仪器上跑过：在真实硬件上的头几次会话请有人在旁看着。

## 包含什么

- **`mast` MCP server**：三十九个 `mast_*` 工具。6.5.0 的二十个覆盖会话简报、技能检索与技能卡、
  带轮询与取消的作业、急停、实验与样品范围、原始数据下载、笔记、向操作员发问、组合技能和交接
  报告。7.0 新增的十八个是视觉回路（VISTA）：看仪器、带着观测令牌做动作，重新渲染并放大已归档
  的帧，精确读数与测量，agent 自己的 `GUIDE.md` 与 `WORKING.md`，实验历史，服务端等待，探测器，
  以及文献检索。7.0.1 新增 `mast_search_data`（按元数据找数据文件），并把 MAST 自己的仪器 agent
  用的那批工具开放给 `mast_run`：实验记录、地图分析、文档、环境等等，所有外部 agent 一视同仁。
  把活交给 MAST 的内部 agent 不属于外部接口；登记仪器旁发生的事（换针、手动粗动）仍由操作员来做。
- **两个技能**：[mast-operator](skills/mast-operator/SKILL.md)（操作流程与铁律）和
  [mast-skill-author](skills/mast-skill-author/SKILL.md)（按正确的阶梯新增能力）。
- **指南资源**：中英双语操作指南，以 `mast://guide/{en|zh}/{file}` 提供。

server 只用 Python 标准库：不用装任何包，MAST 自带的 Python 就能跑。Codex 用的也是这同一个
server（见[在 Codex 里用](#在-codex-里用)）。

## 前提

- MAST 7.0，跑在本机；或跑在仪器电脑上、经 VPN 可达。MAST 6.5.0 也能支撑那二十个老工具
  （这时 `mast_run` 会退回到它的作业接口），但视觉回路的工具一个都用不了。`mast_search_data` 和
  仪器 agent 的工具要 MAST 7.0.1。
- 运行 server 的 Python 3.10 及以上。MAST 自带的运行时就够用：MAST 安装目录下的
  `MASTv2/pyruntime/python.exe`。

## 安装

MAST 的安装目录本身就是一个插件市场。在 Claude Code 里：

```text
/plugin marketplace add <install dir>
/plugin install mast@mast
```

MAST 仓库的一份检出同样能用，`/plugin marketplace add <path-to-your-MAST-checkout>` 也行；优先用你所运行的
那个 MAST 自带的那一份，这样工具与服务端才对得上。启用插件时 Claude Code 会询问下面这些选项。

## 从另一台电脑：SSH over Tailscale

另一台电脑上的 Claude Code 或 Codex 不需要这个插件。在仪器电脑上，点 MAST 启动器的「设置 AI 远程
访问」按钮，它会开启 Windows 自带的 SSH 并授权一把密钥，只在你的 Tailscale 网络内可达（它运行的是
独立脚本 `setup-ai-ssh.ps1`）。另一台电脑上的 AI 用普通 `ssh` 连上来，在仪器电脑上干活——那台电脑
上什么都不用装，也不用额外登录。私钥保存在 `%USERPROFILE%\.ssh\ai-remote`；要收回，删掉
`authorized_keys` 里那一行，或停用 SSH 服务。步骤见[远程访问](skills/mast-operator/references/zh/01-quickstart.md#远程访问)。

想在另一台电脑的 MCP 客户端里直接用 MAST 的 `mast_*` 工具，则在那台电脑上运行本插件，隔着 VPN 调
MAST 的局域网模式接口（见下面「配置」）。

## 配置

| 选项 | 默认值 | 含义 |
|---|---|---|
| `python` | `python`；MAST 安装程序会改成自带的运行时 | 运行 server 的解释器（3.10+）：MAST 的 `MASTv2/pyruntime/python.exe` 或已安装的 Python |
| `mast_url` | `http://127.0.0.1:7862` | MAST 根地址；LAN 模式用 `https://` |
| `mast_user` | 空 | HTTP Basic 用户名，仅 LAN 模式 |
| `mast_password` | 空 | HTTP Basic 密码，仅 LAN 模式；存进安全存储 |
| `verify_tls` | `true` | 设为 `false` 以接受仪器电脑的自签证书 |
| `allow_remote` | `false` | 允许不在本机的 MAST 地址（只经 VPN） |
| `actor` | `claude-code` | 你的动作在 MAST 记录里的署名 |

### 环境变量

手动运行时，server 从 `MAST_URL`、`MAST_USER`、`MAST_PASSWORD`、`MAST_VERIFY_TLS`、
`MAST_ALLOW_REMOTE`、`MAST_ACTOR` 读同样的设置。`MAST_SESSION` 给会话起名（每条连接一个；不设
就随机生成）。`MAST_FETCH_DIR` 指定 `mast_fetch` 的落盘目录，默认是项目里的 `.mast-fetch/`，它
自带一个 `.gitignore`，拉下来的数据不会进 git。`MAST_MCP_LOG` 设日志级别（日志走 stderr）。

### 检查连接

在 Claude Code 里运行 `/mcp`：`mast` 应显示为已连接。如果显示「Failed to connect」并带
「Connection closed」，说明 `python` 选项没有指向能用的解释器（见 Windows 注意事项）。然后让 Claude
调一次 `mast_status`。如果返回错误，错误里会写明该查什么：MAST 是否在运行、http 还是 https、
登录凭据、地址是否不在本机。`mast_observe` 应当返回一次观测；实验里有了帧之后，还会带上图像。

最先碰到的往往是两种情形，错误里都会点名。一是 MAST 启动器的窗口开着，主服务却从没启动：在
启动器里点「启动主服务」，再调一次工具即可，不用重连。二是启动器里勾着「局域网访问」：这时 MAST
对 `127.0.0.1` 也用 `https://`、自签名证书加登录应答；要么取消勾选并重启主服务，要么把
`mast_url`、`mast_user`、`mast_password`、`verify_tls` 一起设好再重连（`/mcp`）。错误会一次列出
所有还缺的设置，重连一次就够。

## Windows 注意事项

- **Python 路径。** Windows 上裸的 `python` 常常是微软商店的占位程序，跑不了 server。MAST 安装
  程序会把 `python` 选项指向它自带的运行时；如果插件是从别处装的，就把它指向真正的解释器，例如
  默认安装目录下 MAST 自带的那个：

```text
C:\MAST\MASTv2\pyruntime\python.exe
```

- **https 与自签证书。** LAN 模式下 MAST 用仪器电脑自己的证书提供 `https://`。`mast_url` 写
  `https://`；除非你的电脑装了那张证书，否则把 `verify_tls` 设为 `false`。同时设好 `mast_user`
  和 `mast_password`：LAN 模式一定要登录。
- **默认只认本机。** `127.0.0.1`、`localhost`、`::1` 以外的地址在打开 `allow_remote` 之前一律拒绝，
  因为仪器控制只应发生在本机或 VPN 内（例如经 VPN 访问 `https://192.0.2.10:7862`）。绝不要把 MAST
  暴露到公网。
- **不走代理。** server 从不经系统代理发送 MAST 流量。

## 工具

所有端点都在 `/api/ext/v1` 之下。

| 工具 | 端点 | 用途 |
|---|---|---|
| `mast_briefing` | `GET /briefing` | 一次拿到全部状态；最先读 |
| `mast_status` | `GET /status` | 快速状态检查 |
| `mast_find_skills` | `GET /skills/search` | 按动作找技能 |
| `mast_skill_card` | `GET /skills/{name}` | 参数、单位、安全级、耗时 |
| `mast_run` | `POST /vista/act`, `GET /jobs/{id}` | 以作业方式执行技能或仪器 agent 的工具（7.0.1）并等待；驱动仪器的技能要带 `after`（比 7.0 旧的 MAST：`POST /jobs`） |
| `mast_job` | `GET /jobs/{id}` | 轮询单个作业 |
| `mast_jobs` | `GET /jobs` | 列出作业 |
| `mast_cancel` | `POST /jobs/{id}/cancel` | 协作式取消 |
| `mast_emergency_stop` | `POST /estop` | 急停，并取消所有外部作业 |
| `mast_scope` | `GET /scope`, `POST /scope` | 实验与样品 |
| `mast_list_data` | `GET /data/files` | 最近的数据文件 |
| `mast_search_data` | `GET /data/search` | 按元数据检索数据记录（图库索引 + 最新文件；7.0.1） |
| `mast_fetch` | `GET /data/file`, `GET /data/frame` | 把原始文件或一帧存到本地 |
| `mast_note_write` | `POST /notes` | 往 MAST 记忆库写笔记 |
| `mast_note_search` | `GET /notes` | 检索笔记 |
| `mast_ask_operator` | `POST /requests` | 向操作员发问 |
| `mast_operator_reply` | `GET /requests`, `GET /requests/{id}` | 读答复 |
| `mast_composite_draft` | `POST /composites/draft` | 校验组合技能 spec |
| `mast_composite_save` | `POST /composites` | 保存并注册组合技能 |
| `mast_propose_skill` | `POST /skills/proposals` | 提交 Python 代码待人审 |
| `mast_handover` | `POST /handover` | 交接报告 |

视觉回路，7.0 新增：

| 工具 | 端点 | 用途 |
|---|---|---|
| `mast_observe` | `POST /vista/observe` | 最新一帧的图像、仪器状态和一个新的观测令牌 |
| `mast_act` | `POST /vista/act` | 一个仪器动作，`after` 填最新的令牌；答复是它的观测，或者 `running` |
| `mast_inspect` | `POST /vista/inspect` | 重新渲染、放大已归档的帧 |
| `mast_read_values` | `POST /vista/read_values` | 读出精确的归档数值 |
| `mast_line_profile` | `POST /vista/measure/line_profile` | 沿一条线取值，量距离、台阶高度 |
| `mast_fft_peaks` | `POST /vista/measure/fft_peaks` | 从二维傅里叶谱得出周期和方向 |
| `mast_detect_blobs` | `POST /vista/measure/detect_blobs` | 数一数、定位紧凑的特征 |
| `mast_diff` | `POST /vista/measure/diff` | 对齐两帧，画出变了的地方 |
| `mast_stats` | `POST /vista/measure/stats` | 统计量、粗糙度、行噪声 |
| `mast_read_guide` | `GET /vista/notes/guide` | 读 `GUIDE.md`，所有实验共用 |
| `mast_write_guide` | `PUT /vista/notes/guide` | 重写 `GUIDE.md` |
| `mast_read_working` | `GET /vista/notes/working` | 读当前实验的 `WORKING.md` |
| `mast_write_working` | `PUT /vista/notes/working` | 重写 `WORKING.md` |
| `mast_history` | `GET /vista/history` | 这个实验的动作、外来帧、探测器事件和检查点 |
| `mast_wait` | `POST /wait` | 在服务端等：作业、新帧、告警、操作员答复、探测器命中或定时 |
| `mast_detector_create` | `POST /detectors` | 新建一个分析型探测器 |
| `mast_detectors` | `GET /detectors`、`POST /detectors/{id}/test` 等 | 列出、查看、启用、停用、删除、试跑探测器 |
| `mast_literature_search` | `POST /literature/search` | 检索操作员的文献库 |

每次工具调用都在 55 秒内返回，只有两个例外：`mast_wait` 在服务端最长等 840 s；`mast_detectors`
做 `test` 时，要等探测器跑完它被允许的时长。如果你的 Claude Code 给 MCP 工具设了更短的超时，
启动时把 `MCP_TOOL_TIMEOUT` 设成至少 `900000`（单位毫秒）。更长的工作在 MAST 上以作业形式继续
运行：用 `mast_wait` 等它，或者用 `mast_job` 轮询。中断工具调用不会取消作业，取消要用 `mast_cancel`。

## 在 Codex 里用

Codex 既不展开 `${CLAUDE_PLUGIN_ROOT}`，也不展开 `${user_config.*}`，所以用不了这个插件的
`.mcp.json`。MAST 安装目录里另有办法，把 Codex 接到同一个 server 上：

```text
<install dir>\MASTv2\pyruntime\python.exe <install dir>\integrations\setup_clients.py codex
```

它在 Codex 的 `config.toml` 里加一段 `[mcp_servers.mast]`（带着 `tool_timeout_sec = 900`），并在
你用户目录下建一个工作区 `Documents\MAST-agent`，把运行规则写进它的 `AGENTS.md`。然后用
`codex mcp list` 确认一下，用 ChatGPT 账号（而不是 API key）登录 Codex，在 Codex 里打开这个
工作区。每一步都写在[快速上手](skills/mast-operator/references/zh/01-quickstart.md)里。

## 指南

完整的操作指南在 [mast-operator 的 references](skills/mast-operator/references/zh/README.md) 里，
也以 MCP 资源 `mast://guide/zh/...` 提供。

## 开发

在仓库根目录：

```text
python -m pytest tests/v2/unit/integrations tests/v2/unit/docs -q
python scripts/sync_external_docs.py --check
```

`skills/mast-operator/references/` 下的指南文件是 `docs/external/` 的拷贝；改原件，再运行
`scripts/sync_external_docs.py`。

## 许可

MIT，与 MAST 相同。
