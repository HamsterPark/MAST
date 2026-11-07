# 排障

现象、原因、处理办法 —— 遇到什么就从上往下找。这些现象背后的机制见
[概念](02-concepts.md) 与[运行规则](03-operating-rules.md)；一个错误体的精确形状见
[API 参考](07-api-reference.md)。

## 接入

| 现象 | 原因 | 处理办法 |
|---|---|---|
| `Cannot connect to MAST …（connection refused）`，可 MAST 的窗口明明开着 | 开着的是 MAST 启动器，它的主服务（端口 7862 上的 API）还没有启动。MCP 服务器发现启动器自己的端口有应答时，错误里会直接这么说 | 在启动器里点「启动主服务」；服务起来之后再调一次工具即可 —— MCP 服务器不用重启 |
| 同一台电脑上的客户端连不上，而启动器里勾着「局域网访问」 | 打开局域网访问后，MAST 用 `https://`、自签名证书加 HTTP Basic 登录来提供服务 —— 对 `127.0.0.1` 也一样 | 两条路任选：在启动器里取消勾选局域网访问并重启主服务（默认的 `http://127.0.0.1:7862` 随即可用，不用重连）；或者四项一起设好 —— `mast_url` 设为 `https://127.0.0.1:7862`，`mast_user`、`mast_password`，`verify_tls` 设为 `false`（环境变量分别是 `MAST_URL`、`MAST_USER`、`MAST_PASSWORD`、`MAST_VERIFY_TLS`）—— 然后重启 MCP 服务器（Claude Code 里 `/mcp`，重连 `mast`）。MCP 服务器的错误会一次列出所有还缺的设置 |
| 对一台你确定在 LAN 模式下跑的 MAST，`curl` 报 `HTTP=000` 或连接错误 | LAN 模式在传输层就是 TLS | 用 `https://`，不是 `http://` |
| `401 Unauthorized` | LAN 模式用 HTTP Basic，要么没发凭据，要么发错了 | 设置登录信息（插件里是 `mast_user`/`mast_password`，环境变量是 `MAST_USER`/`MAST_PASSWORD`） |
| `403`，`cross_origin_refused` | 这次请求看起来像是浏览器发的（带了 `Origin` 或某个 `Sec-Fetch-*` 头），而且要么跨了源，要么是经一个主机名（而不是回环名或字面 IP 地址）到达服务的 | 直接调 API，不要从网页里调；用 `127.0.0.1` 或数字地址而不是主机名 —— curl、MCP server 这类程序化客户端不受影响，因为它们两个头都不带 |
| `415`，`unsupported_media_type` | 一次写请求 —— 包括 `POST /estop` 与 `POST /jobs/{job_id}/cancel` —— 发出时没带 `Content-Type: application/json` 与一个 JSON 请求体 | 带上 `Content-Type: application/json` 和一个 JSON 请求体；没有别的要说就发 `{}` |
| 拿到一个 HTML 页面而不是 JSON | 路径前缀写错了，或者这份 MAST 没有外部 agent API | 确认基础路径是 `/api/ext/v1` —— 外部面对每一个请求都答 JSON，包括 404 |
| TLS 证书错误 | 仪器机上的证书是自签名的 | 把 `verify_tls` 设为 false，或者在本机装上那张证书 |
| 一个远程地址被拒绝 | 不是本机的地址默认拒绝，除非显式打开了远程访问 | 只在 VPN 内打开它 —— 绝不要把 MAST 暴露到公网 |
| MCP 服务器起不来，或显示「Failed to connect」 | 配置的 Python 解释器是个占位符（Windows 上常是微软商店的那个），不是真的解释器 | 把解释器选项指向一个真的 Python 3.10+ |
| 非 ASCII 文字打印成乱码 | 终端或客户端没有按 UTF-8 读这份响应 | 把客户端或终端设成 UTF-8；这个 API 的回答永远是 UTF-8 编码的 JSON |

## 跑技能

| 现象 | 原因 | 处理办法 |
|---|---|---|
| 所有作业立刻全部失败，错误里说 `aborted by operator — refusing to start ...` | 急停闩已置位 —— 由 E-STOP、`POST /estop`、一次环境告警或某个硬件安全信号置位；`GET /status` 会把它报告成 `abort.emergency: true`，带着 `abort.why` | 不要重试：闩置位期间任何作业都会在开始之前被拒，停止类技能也不例外，因为置位它的那次 E-STOP 已经停了运动、退了针；告诉操作员，闩由他们在 MAST 自己的界面里解除 |
| `503`、一份 `degraded` 列表、或一份 `missing` 列表 | 某个子系统没接线，往往是因为 MAST 还在启动 —— 这和「空答复」不是一回事 | 过一会儿再试一次健康检查或状态检查；一直这样就告诉操作员 |
| `refused_busy` | 另一条链路已经占着仪器 | 等它，或者问操作员；立刻重试不会让它更快结束 |
| `failed`，`refused_by: sample_gate` | 这个技能产数据，需要一个当前样品 | 先用 `POST /scope` 定一个 |
| `failed`，`refused_by: si_parse` | 一个带量纲参数的值解析不出来 —— 往往是一个字符串漏掉了必须要有的 SI 前缀 | 查技能卡的 `si_params`；写 `"5n"` 这样的值，或者直接写 SI 基本单位下的一个数 —— 前缀规则只对字符串生效 |
| 在跑的作业带着 `waiting_operator` | 作业走到一步只有人能决定的地方（或在跑 `AskOperator`），MAST 已经去问操作员了，提示 id 在 `waiting_operator.prompt_id` | 没出问题：用 `mast_wait` 等，或先去做别的；只有操作员能答。取消作业会撤回提示 |
| `failed`，错误 `HumanNodeFailed` | 操作员没做决定：时限内没有答复（缺省 30 分钟）、提示被关闭或取消、或对确认回答了「否」 | 读错误信息（里面有提示 id）；那一步还需要的话，用 `mast_ask_operator` 问操作员，再重新提交 |
| `failed`，`refused_by: needs_human_node` | MAST 没法把那次暂停变成给操作员的问题（技能自己起的线程里的 `human` 节点，或别的暂停） | 改到 MAST 自己的界面里跑，或者去问操作员 |
| `422`，`skill_disabled` | 这个技能，或者它用到的某个子步骤，需要本机关闭了的硬件或能力 | 换一个技能，或者请操作员打开它 |
| `429` | 同时在跑的作业太多了 | 等一个跑完，或者取消一个你不再需要的 —— 停止类或撤回类技能（比如 `StopScan`）不受这个上限约束，永远能提交 |
| `503`，`shutting_down` | 服务正在关停，拒绝接新作业 | 等它回来，再重新连接 |
| `lost_on_restart` | 作业还在跑的时候 MAST 进程重启了；它绝不会被重放 | 决定要不要再提交一次之前先读简报 |
| 拒绝里点名 `safe_mode_tip_processing_blocked` | 操作员设了 SAFE，它在每条路径上都拒绝针尖处理，组合技能里也一样 | 不要重试；如果这件事确实需要，去问操作员 |
| 拒绝描述的是五条硬闸之一，说没有批准会到来 | 你调用了（或者组合技能的某一步调用了）这条路径上无条件拒绝的某个物理危险动作之一 | 别再重试 —— 什么都改变不了这个结局；确实需要的话，得由人在 MAST 自己的界面里去做 |

## 接入客户端（7.0 新增）

| 现象 | 原因 | 处理办法 |
|---|---|---|
| `codex mcp list` 里没有 `mast` | 这个 Windows 用户还没跑过 `setup_clients.py codex`，或者 Codex 读的是另一个目录（`CODEX_HOME`） | 再跑一次（用 `--codex-home` 指定目录）；`--dry-run` 会显示它要写什么 |
| Codex 报告 `mast` 服务器启动失败 | `[mcp_servers.mast]` 里的解释器或桥的路径已经不存在了，通常是 MAST 挪了位置或者重装到了别处 | 从当前的安装目录再跑一次 `setup_clients.py codex` |
| Codex 走 API 账户计费，或者向你要 API key | 环境里设了 `OPENAI_API_KEY`、`CODEX_API_KEY` 或 `CODEX_ACCESS_TOKEN`，Codex 于是用那个 key 登录 | 把它们从环境里删掉，用 ChatGPT 账号登录（`codex login`）；在 `config.toml` 里写上 `forced_login_method = "chatgpt"`，其他登录方式一律拒绝 |
| `mast_wait` 跑了几分钟后以客户端超时失败 | 客户端的 MCP 工具超时比这次等待短（等待最长 840 s） | Codex：`[mcp_servers.mast]` 里的 `tool_timeout_sec = 900`（安装工具会写上）；Claude Code：`MCP_TOOL_TIMEOUT` 至少设 `900000`；其他客户端：把它们的工具超时设到至少 900 s |
| 插件向你要 `python`，或者它的服务器显示「Failed to connect」 | 插件不是从安装目录装的，或者是在安装程序设好 `python` 之前装的 | 把 `python` 选项指向 `<install dir>\MASTv2\pyruntime\python.exe`，或者跑一次 `setup_clients.py patch-plugin` |
| `mast_act` 答复「This MAST has no /vista/act」 | 服务端比 7.0 旧 | 装 MAST 7.0；在那之前用 `mast_run` 跑技能，它会退回到 6.5.0 的作业 API |

## AI 远程访问（SSH over Tailscale）

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 另一台电脑经 SSH 连不上仪器电脑 | 两台不在同一个 tailnet、有一台的 Tailscale 没开、代理（Clash 之类）截走了 `100.x` 地址、SSH 服务没在跑，或者防火墙没放行 tailnet | 两台登录同一个 Tailscale 账号，让代理绕过 `100.64.0.0/10`；在仪器电脑上确认 `sshd` 在运行（`Get-Service sshd`）。设置脚本的防火墙规则已经只放行来自 `100.64.0.0/10` 的 SSH |
| 管理员账户的密钥被 SSH 拒绝 | 管理员的密钥要放在 `%ProgramData%\ssh\administrators_authorized_keys` 且 ACL 严格，而不是用户自己的文件 | 重跑「设置 AI 远程访问」/ `setup-ai-ssh.ps1`，它会把密钥放到正确位置 |
| 设置窗口没等看清 `ssh` 那行就关了 | 旧版本结束时没有暂停 | 用新版本重跑；它现在会等你按回车再关 |

## 图像（7.0 新增）

| 现象 | 原因 | 处理办法 |
|---|---|---|
| Codex 的 Code Mode：模型没看过结果里的图就接着干 | Code Mode 里，图像要等代码对它调用 `image()` 才会送到模型眼前 | 每次观测都调用 `image()`，一段代码只做一个动作 |
| Codex：不管什么模式都看不到图 | 带 `structuredContent` 的结果，在 Codex 里会丢掉图像。MAST 的服务器从不发它；夹在 Codex 和服务器之间的代理或包装层可能会加上它 | 让 Codex 直连 MAST 的服务器，就像 `setup_clients.py codex` 配的那样 |
| 从图上读出来的数和工具给的对不上 | 模型从图上估位置和尺寸常常不准，一次来好几张图时读错得更多 | 数值从 `mast_read_values` 和测量工具里取；一次观测只带一张图，是有意这么设计的 |
| Claude Code：一个动作回来是 `FAILED`，它的观测里看不到图 | Claude Code 不显示错误结果里的图 | 调 `mast_observe` 拿当前的观测。附带最新观测的拒绝（`observe_first`）是作为普通结果返回的，所以它的图能显示 |
| Claude Code：答复被截断，或者被存成文件而没有显示出来 | 答复超出了 Claude Code 对一个 MCP 结果的大小上限。插件已经给它带图的工具调高了上限，但特别大的答复仍会超 | 少要几个视图、缩小区域，或者少取些采样点；也可以启动 Claude Code 时把 `MAX_MCP_OUTPUT_TOKENS` 设大一些 |
| 观测里写着「No frame image yet」 | 这个工作区里还没有归档过任何帧（比如一个新实验） | 做一个动作 —— 扫一张图就有了第一帧 |
| `next_token` 是 `null`，而 `token_on_image` 是 `true` | 严格模式开着 | 从图像下方的横带上读出令牌，作为 `after` 传入 |

## 观测闸（7.0 新增）

| 现象 | 原因 | 处理办法 |
|---|---|---|
| 一次又一次 `observe_first` | 你用的是旧令牌。有更新的观测顶替了它：你自己调的 `mast_observe` 或 `mast_wait`、一个并行发出的调用、发给另一个已连接客户端的观测，或者一张不是你的动作产生的帧（MAST 的界面、操作员、MAST 自带的内部 agent、另一个客户端）。你的 MCP 服务器或 MAST 重启，也会让旧令牌作废 | 用拒绝带回来的那次观测的令牌，读到它之后、行动之前不要再调任何会产生观测的工具。`mast_history` 里能看到外来帧；要是有人一直在扫或者在观测，去问操作员 |
| 一次又一次 `same_code_cell` | 一段 Codex 代码里发了不止一个动作 | 一段代码只做一个动作：对结果调用 `image()`，结束这段，在下一段里再动 |
| `act_in_flight` | 之前的某个动作（你的，或者别的会话的）还在执行 | 用拒绝里给出的作业，以 `{"type": "job", "job_id": ...}` 调 `mast_wait`，看过它的观测再动 |
| `checkpoint_requested`，除笔记工具外的每个工具都拒绝 | 运行器要求做检查点 | 用 `mast_write_working` 写好 `WORKING.md`；之后会话继续，或者由一个新会话接手 |
| `session_closed` | 操作员停掉了这个会话 | 不要再做动作；写一句简短的总结收尾 |
| `POST /jobs` 返回 `409`，`use_act` | 这个会话用过视觉回路，所以它驱动仪器的技能必须带令牌 | 用带 `after` 的 `mast_run`，或者 `mast_act`；只读、分析和停止类技能仍可走 `POST /jobs` |
| `409`，`request_id_conflict` | 这个 `request_id` 已经用于另一个动作或另一组参数 | 只有在超时后重发同一个动作时才复用 `request_id`；新动作要用新的 |
| `next_token` 是 `null`，并附有 `token_note` | 那次观测已经被更新的观测取代，或者会话已关闭 | 调 `mast_observe` 拿最新的观测 |

## 帧、等待与探测器（7.0 新增）

| 现象 | 原因 | 处理办法 |
|---|---|---|
| 动作的文字里有 `frames_unavailable`：「the controller did not report its save directory」 | MAST 找不到控制器存新文件的那个文件夹，所以新的扫描和谱没有归档 | 告诉操作员：MAST 得知道控制器的保存目录。在模拟器或测试环境上，启动 MAST 时把 `MAST_VISTA_SESSION_DIR` 设成文件实际出现的那个文件夹 |
| 出现了不是你扫的帧，带着 `origin: external` | 有别人存了数据：MAST 的界面、操作员、MAST 自带的内部 agent，或者另一个客户端。每一张这样的帧也会让当前令牌作废 | 正常现象；看看它们（`mast_inspect`），然后用最新那次观测的令牌行动 |
| `mast_wait` 超时，什么都没触发 | `timeout_s` 之内什么都没发生 | 再调一次：它会从上一个游标接着等，什么都不会漏 |
| `mast_wait` 每次都立刻返回 | 某个条件早已成立：作业已经结束、定时设得很短，或者自上一个游标以来事件已经堆了一批 | 先看触发了什么、处理掉，再接着等 |
| 某个探测器显示为已停用，附着原因 | 它连续出错 5 次，或者一小时内命中超过 30 次 | `mast_detectors`，`op` 填 `get`，能看到它的代码和原因；改好，在已归档的帧上试过，再重新启用 |
| 某个探测器的命中被标成 `suppressed` | 一小时内探测器唤醒 agent 的次数超过了 60 次 | 命中照样记下，但不唤醒任何人；把探测器的条件收紧一些 |
| 新建或启用探测器被拒，代码 `too_many_enabled` | 这个实验里已经启用了 8 个探测器 | 停用或删掉一个不再需要的 |
| 新建或试跑探测器被拒，代码 `loopback_only` | 探测器的代码以 MAST 用户的身份在仪器电脑上运行，所以默认只接受本机发来的请求；你是经局域网连的 MAST | 在仪器电脑本机上建；确实需要从局域网建时，由操作员设环境变量 `MAST_DETECTORS_ALLOW_REMOTE=1` 后重启 MAST（这等于把局域网密码当成了在这台电脑上运行代码的许可） |
| 在 STM-Bench 模拟器上，动作以控制器超时失败 | 模拟器渲染一帧的时间超出了控制器 5 s 的超时 | 启动 MAST 时把 `MAST_NANONIS_TIMEOUT_S` 设大一些（0.5 到 600 s；超出范围的值会被忽略）；真实仪器上保持默认值 |

## 运行器（7.0 新增）

| 现象 | 原因 | 处理办法 |
|---|---|---|
| `POST /runner/start` 返回 `409`，`run_active` | 同一时刻只能有一次运行，另一次正在进行 | 等它结束，或者停掉它（`POST /runner/{run_id}/stop`） |
| `POST /runner/start` 返回 `503`，`runner_unavailable` | 这台电脑上缺 Claude Code、MCP 桥、桥用的 Python，或者视觉回路 | 给运行 MAST 的那个账户装好 Claude Code，并检查安装目录 |
| 一次运行刚开始就结束，记录里写着 `init_invalid` | 会话报告了 API key 来源（运行器只走 Claude 订阅），或者它的工具、MCP 服务器不是预期的那一套 | 在这台电脑上用订阅账号登录 Claude Code，并把 API key 从它的配置里删掉 |
| 一次运行显示为 `interrupted` | 运行进行中 MAST 停下了 | 开始新的运行之前，先读简报和这次运行的记录 |
