# 用 Codex 操作 MAST

[English](README.md)

Codex（桌面应用或命令行）与 Claude Code 插件用的是同一个 MCP 服务器
`integrations/claude-code/server`，同一组 `mast_*` 工具，同一套规则。Codex 不展开
`${CLAUDE_PLUGIN_ROOT}` 和 `${user_config.*}`，所以插件里的 `.mcp.json` 用不上：Codex 需要在
自己的 `config.toml` 里有一段写着绝对路径的配置，由 `setup_clients.py` 生成。

> **现状：** Codex 是受支持的客户端（推荐的客户端是 Claude Code 搭配 Claude Opus 5.5）。连接和视觉回路在 STM-Bench 模拟器上做过端到端
> 测试，还没有在真实仪器上跑过：在真实硬件上的头几次会话请有人在旁看着。

## 连接

在装了 MAST 的电脑上打开终端运行（MAST 默认装在 `C:\MAST`；装到了别处的话，换成你自己的安装
目录）：

```text
C:\MAST\MASTv2\pyruntime\python.exe C:\MAST\integrations\setup_clients.py codex
```

它会：

- 在 `%USERPROFILE%\.codex\config.toml`（设了 `CODEX_HOME` 就用那里的）里加上
  `[mcp_servers.mast]` 这几张表。写之前先备份（`config.toml.bak-<时间>`），这几张表以外的内容
  一个字节都不改。再运行一次，会替换它上次写的那几张表。
- 建一个 agent 工作区 `%USERPROFILE%\Documents\MAST-agent`，里面放一份写着操作规则的
  `AGENTS.md`。你改过的 `AGENTS.md` 会先备份再替换。

然后：

1. 启动 MAST。
2. 运行 `codex mcp list`，列表里应当有 `mast`。
3. 在 Codex 里打开工作区：桌面应用里打开 `MAST-agent` 文件夹；命令行用
   `codex -C "%USERPROFILE%\Documents\MAST-agent"`。

Codex 要用 ChatGPT 账号登录（`codex login`），不要用 API key。把 `OPENAI_API_KEY`、
`CODEX_API_KEY`、`CODEX_ACCESS_TOKEN` 从环境变量里删掉：只要设了其中一个，Codex 就会改用它，
计费也就落到 API 账户上。`setup_clients.py` 发现它们时会提醒；在 `config.toml` 里写上
`forced_login_method = "chatgpt"`，Codex 就会拒绝其他任何登录方式。

每次升级 MAST 之后再运行一次这条命令：它会刷新那段配置和工作区里的 `AGENTS.md`。

## 选项

| 选项 | 含义 |
|---|---|
| `--mast-url URL` | MAST 的地址，默认 `http://127.0.0.1:7862` |
| `--actor NAME` | 你的动作在 MAST 记录里的署名，默认 `codex` |
| `--workspace DIR` | agent 工作区，默认 `%USERPROFILE%\Documents\MAST-agent` |
| `--approve all` | 默认：MAST 的所有工具都不经 Codex 弹窗确认 |
| `--approve read_only` | 只预先批准 MAST 的只读工具，其余工具由 Codex 逐次询问 |
| `--codex-home DIR` | Codex 的配置目录，默认 `CODEX_HOME`，否则 `%USERPROFILE%\.codex` |
| `--dry-run` | 只显示会改什么，不写任何文件 |
| `--uninstall` | 删掉 `[mcp_servers.mast]` 这几张表（先备份） |

## 这段配置为什么这样写

- **默认 `--approve all`。** 不预先批准的话，`codex exec` 会直接拒绝 MAST 的写工具（没有人可问），
  桌面应用里每个动作弹一次确认也会打断长时间的工作。保护在 MAST 这一侧：安全闸检查每一个动作，
  而且动作必须带上最新一次观测的令牌，否则被拒绝，所以 agent 不看就动不了手。
  用 `--approve read_only` 的话，升级 MAST 之后要再运行一次：新版本可能加了工具。
- **`tool_timeout_sec = 900`。** `mast_wait` 在 MAST 里最长会等 840 s，而 Codex 放弃一个调用时
  不会通知服务端。
- **不设 `supports_parallel_tool_calls`。** 设了它，Codex 会让 MAST 的写工具并行执行；
  不设，写工具就排队。
- **`cwd` 指向工作区。** `mast_fetch` 下载的文件落在工作区的 `.mast-fetch` 里。
- **MAST 在另一台电脑上时**，桥默认拒绝连接，要在 `[mcp_servers.mast.env]` 里加
  `MAST_ALLOW_REMOTE = "true"`；局域网模式还要加 `MAST_USER` 和 `MAST_PASSWORD`。
  这些只能手工加，而且每次运行 `setup_clients.py codex` 之后都要重新加，因为它会重写这张表。
- **本机开着局域网模式时**，也就是 MAST 启动器里勾着「局域网访问」，MAST 对 `127.0.0.1` 也用
  `https://`、自签名证书加登录应答。要么取消勾选并重启主服务；要么把
  `MAST_URL = "https://127.0.0.1:7862"`、`MAST_USER`、`MAST_PASSWORD` 和
  `MAST_VERIFY_TLS = "false"` 一起写进 `[mcp_servers.mast.env]`（同样手工加），再开一个新会话。

## 在 Codex 里操作 MAST

工作区里的 `AGENTS.md` 写着操作规则，和 Claude Code 插件的 `mast-operator` 技能是同一套；`mast`
服务器还把完整的指南作为 MCP 资源提供（`mast://guide/en/README.md`、`mast://guide/zh/README.md`）。
一次会话的顺序是：先 `mast_briefing`，然后 `mast_scope`、`mast_read_guide` 和
`mast_read_working`，再 `mast_observe`；做一个 `mast_act`，`after` 填这次观测的 `next_token`；
看新的图像；用 `mast_read_values` 和测量工具取数；`mast_write_working`；带着新令牌做下一个动作；
最后 `mast_handover`。在 Code Mode 里，每次观测都要对它调用 `image()`，因为图像只有这样才会送到
模型眼前；一段代码最多发一个动作，第二个会被 MAST 拒绝（`same_code_cell`）。
[快速上手](../../docs/external/zh/01-quickstart.md)里有完整的首次会话。

## 出问题时

| 现象 | 处理办法 |
|---|---|
| `codex mcp list` 里没有 `mast` | 以这个 Windows 用户的身份再跑一次 `setup_clients.py codex`；Codex 用的是别的目录的话，加 `--codex-home` |
| Codex 走 API 账户计费 | 删掉那三个 API key 环境变量，用 ChatGPT 账号登录（`codex login`） |
| `mast_wait` 跑了几分钟就被掐断 | `[mcp_servers.mast]` 里必须有 `tool_timeout_sec = 900`：再跑一次这条命令 |
| 模型没看图就接着动手 | Code Mode：每次观测都调用 `image()`，一段代码只发一个动作 |
| 模型一张图都收不到 | 让 Codex 直连 MAST 的服务器：中间若有包装层给结果加上 `structuredContent`，Codex 会把图丢掉 |
| 一次又一次 `observe_first` | 令牌被别的观测顶替了：用最新那次观测的令牌行动，同时别让别的客户端或 MAST 的界面在这期间扫图 |
| `Cannot connect …（connection refused）`，可 MAST 的窗口明明开着 | 开着的是启动器：在里面点「启动主服务」，再调一次工具 |

其余情况见指南的[排障](../../docs/external/zh/08-troubleshooting.md)。

[`mast_mcp.example.toml`](mast_mcp.example.toml) 是同一段配置，路径用占位符，供手工填写。
