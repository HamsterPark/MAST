# 快速上手

这一篇带你从零走完一次完整的首次会话：安装 MAST、接上客户端、读简报、选样品、看一眼仪器、
做一个动作、量一量它的结果、交接。接下来读 [概念](02-concepts.md) 了解你刚才每一步背后的
道理，读 [运行规则](03-operating-rules.md) 了解规则本身与它们各自的理由。

## 安装 MAST 7.0

MAST 7.0 是一个完整的 Windows 安装包，不走在线更新。在驱动仪器（或模拟器）的那台电脑上手动
安装，上一版的安装包留着，万一要退回去用得上。除了 MAST 本身，安装目录里还带着客户端需要的
一切：

| 在 `<install dir>` 里 | 是什么 |
|---|---|
| `MASTv2\pyruntime\python.exe` | MAST 自带的 Python；MCP 服务器靠它跑，不用另装任何东西 |
| `integrations\claude-code\` | Claude Code 插件：`mast` MCP 服务器、`mast-operator` 与 `mast-skill-author` 两个技能，以及这份指南 |
| `.claude-plugin\marketplace.json` | 让安装目录本身成为一个 Claude Code 插件市场 |
| `integrations\codex\AGENTS.md` | 写进 Codex 工作区的运行规则 |
| `integrations\setup_clients.py` | 接入 Codex，并把 Claude Code 插件指向自带的 Python |
| `docs\external\` | 这份指南 |

安装程序已经把插件的 `python` 选项指向了自带的 Python。接客户端之前先启动 MAST，并确认它的
主服务在跑：只打开 MAST 启动器的窗口还不够，要在里面点「启动主服务」。服务默认在本机的
`http://127.0.0.1:7862` 应答；如果启动器里勾着「局域网访问」，它改用 `https://` 加登录应答，
对本机也一样（见[远程访问](#远程访问)）。

## 接入方式

Codex 和 Claude Code 启动的是同一个 MCP 服务器，它连的是同一个 API（`/api/ext/v1`）；纯 HTTP
则直接连这个 API。哪条路都受同一套规则约束。推荐的客户端是 Claude Code 搭配 Claude Opus 5.5；也支持 Codex。

### A. Claude Code

插件给 Claude Code 同样一组 `mast_*` 工具，外加两个技能：`mast-operator`（驱动 MAST 的工作
流程）与 `mast-skill-author`（写组合技能与 Python 提议，并把它们投稿上游）。安装目录本身就是
一个插件市场，从它安装：

```text
/plugin marketplace add <install dir>
/plugin install mast@mast
```

MAST 仓库的一份检出同样能用（`/plugin marketplace add <你的-MAST-检出路径>`），公开仓库也行
（`/plugin marketplace add HamsterPark/MAST`）。优先用你所运行的那个 MAST 自带的那一份，这样
工具与服务端才对得上。

Claude Code 随后会问这些选项：

| 选项 | 默认值 | 含义 |
|---|---|---|
| `python` | `python`；安装程序会把它换成 `<install dir>\MASTv2\pyruntime\python.exe` | 跑服务器的解释器（3.10+）；Windows 上裸 `python` 若打开微软商店，请指向一个真的解释器 |
| `mast_url` | `http://127.0.0.1:7862` | MAST 的地址；LAN 模式用 `https://` |
| `mast_user` | 空 | HTTP Basic 用户名，只在 LAN 模式下用 |
| `mast_password` | 空 | HTTP Basic 密码，只在 LAN 模式下用 |
| `verify_tls` | `true` | `false` 接受自签名证书 |
| `allow_remote` | `false` | 允许一个不是本机的地址 |
| `actor` | `claude-code` | 你的动作在 MAST 记录里署的名字 |

完整的选项清单与精确默认值在插件自己的包里（就在插件源码旁边的 `README.md`）；这张表是摘要。
`mast_wait` 最长会阻塞 840 s：如果你的 Claude Code 给 MCP 工具设了更短的超时，启动时把
`MCP_TOOL_TIMEOUT` 设成至少 `900000`（单位毫秒）。接上之后，先让 agent 去读简报 —— 这是
[运行规则](03-operating-rules.md) 的第一条。

### B. Codex

每个 Windows 用户跑一次（安装目录名里有空格的话，给两个路径加上引号）：

```text
<install dir>\MASTv2\pyruntime\python.exe <install dir>\integrations\setup_clients.py codex
codex mcp list
```

`setup_clients.py codex` 只做两件事，别的一概不碰：

1. 在 Codex 的 `config.toml`（你用户目录下的 `.codex` 里，或者 `CODEX_HOME` 指定的目录里）
   加一段 `[mcp_servers.mast]`。再跑一次只会替换这一段；每次改动之前都会先在原文件旁边留一份
   备份。
2. 建一个 agent 工作区，也就是你用户目录下的 `Documents\MAST-agent` 文件夹，把运行规则写进
   它的 `AGENTS.md`。

它写进去的那一段大致如下（注释行略去）：

```toml
[mcp_servers.mast]
command = '<install dir>\MASTv2\pyruntime\python.exe'
args = ["-I", "-X", "utf8", '<install dir>\integrations\claude-code\server\run_server.py']
cwd = '<workspace>'
startup_timeout_sec = 30
tool_timeout_sec = 900
default_tools_approval_mode = "approve"

[mcp_servers.mast.env]
MAST_URL = "http://127.0.0.1:7862"
MAST_ACTOR = "codex"
PYTHONUTF8 = "1"
PYTHONIOENCODING = "utf-8"
```

| 设置 | 为什么 |
|---|---|
| `tool_timeout_sec = 900` | `mast_wait` 在服务端最长要等 840 s；Codex 默认的工具超时比这短，会把它掐断 |
| `supports_parallel_tool_calls` 不设 | 这样 Codex 会让 MAST 的写工具一个接一个地跑；只读工具仍可以并行 |
| `default_tools_approval_mode = "approve"` | 每个 MAST 工具都不再弹 Codex 的确认。把关在 MAST 这一侧：每个动作都要过它的安全闸，一个动作不带最新观测的令牌就会被拒。加 `--approve read_only` 则只预先批准只读工具，其余工具 Codex 每次都会先问 |
| `MAST_ACTOR = "codex"` | 你的动作在记录里署的名字 |

| `setup_clients.py codex` 的选项 | 含义 |
|---|---|
| `--workspace DIR` | 换一个工作区文件夹 |
| `--mast-url URL` | MAST 的地址（默认 `http://127.0.0.1:7862`） |
| `--actor NAME` | 在 MAST 记录里的署名（默认 `codex`；只用小写字母、数字、`.`、`-` 和 `_`） |
| `--approve read_only` | 只预先批准只读的 MAST 工具 |
| `--codex-home DIR` | Codex 的目录，当它既不是 `CODEX_HOME` 也不是你用户目录下的 `.codex` 时用 |
| `--dry-run` | 只显示会改什么，什么都不写 |
| `--uninstall` | 把 `[mcp_servers.mast]` 那一段删掉；工作区保留 |

然后：

1. **确认** Codex 看得到这个服务器：`codex mcp list` 里列着 `mast`。
2. **用 ChatGPT 账号登录**，不要用 API key：`codex login`。确认环境里 `OPENAI_API_KEY`、
   `CODEX_API_KEY`、`CODEX_ACCESS_TOKEN` 一个都没设：只要设了其中一个，Codex 就会用那个 key
   登录，计费也就落到 API 账户上，而不是你的 ChatGPT 套餐。在 `config.toml` 里写上
   `forced_login_method = "chatgpt"`，Codex 就会拒绝其他任何登录方式。
3. **在 Codex 里打开工作区文件夹**：Codex 应用里直接打开这个文件夹；终端里用
   `codex -C "<workspace>"`。Codex 会读那个文件夹里 `AGENTS.md` 写的规则。

每次升级 MAST 之后再跑一次 `setup_clients.py codex`：它会刷新那一段配置和工作区里的 `AGENTS.md`
（你改过的那份会先备份）。

### C. 其他 MCP 客户端

任何兼容 MCP 的客户端都能直接跑同一个服务器，不必经过插件市场：把 MCP 配置指向插件目录里的
`server/run_server.py`（装好的 MAST 里就是 `<install dir>\integrations\claude-code\server\run_server.py`）。
这样跑的话，服务器从环境变量读设置，而不是插件的那些选项：

| 变量 | 含义 |
|---|---|
| `MAST_URL` | MAST 的地址，与上面的 `mast_url` 相同 |
| `MAST_USER` / `MAST_PASSWORD` | HTTP Basic 登录，只在 LAN 模式下用 |
| `MAST_VERIFY_TLS` | `false` 接受自签名证书 |
| `MAST_ALLOW_REMOTE` | 允许一个不是本机的地址 |
| `MAST_ACTOR` | 你的动作署的名字 |
| `MAST_SESSION` | 会话名；不设置时用一个随机的 |
| `MAST_FETCH_DIR` | 取回的文件存在哪；默认是工作目录下的 `.mast-fetch/` |
| `MAST_MCP_LOG` | 服务器的日志级别（日志走 stderr） |

```json
{
  "mcpServers": {
    "mast": {
      "command": "<install dir>\\MASTv2\\pyruntime\\python.exe",
      "args": ["-I", "-X", "utf8", "<install dir>\\integrations\\claude-code\\server\\run_server.py"],
      "env": {
        "MAST_URL": "http://127.0.0.1:7862",
        "MAST_ACTOR": "my-agent"
      }
    }
  }
}
```

给这个客户端的 MCP 工具超时设成至少 900 s，留给 `mast_wait`。客户端最好能显示工具结果里的
图像；不能显示的客户端照样能用，只是每次观测只读得到文字那一半。

### D. 纯 HTTP

这个 API 根本不需要专门的客户端：任何 HTTP 库，或者 `curl`，都能直接调它。这是最底层的接入
方式，也最适合初次看清 API 到底返回什么；这一篇最后一节用的就是它。视觉回路在
`/api/ext/v1/vista/` 下面，靠 `X-MAST-Session` 头区分不同的客户端。一个会话一旦用过视觉回路，
它驱动仪器的技能就必须带着令牌走 `/vista/act`；`POST /jobs` 会以 `use_act` 拒绝它们。从不碰
视觉回路的调用方，行为与 6.5.0 完全一样。

## 远程访问

以上所有方式默认都只在本机。要从另一台电脑操作仪器，有两种方式，都通过私有 VPN（最省事的是
[Tailscale](https://tailscale.com/download)，个人使用免费、不用改路由器）把 MAST 留在公网之外。

- **SSH over Tailscale（最简单）。** 在仪器电脑上点 MAST 启动器的「设置 AI 远程访问」按钮，它会开启
  Windows 自带的 SSH 并授权一把密钥，只在你的 Tailscale 网络内可达。另一台电脑上的 Claude Code 或
  Codex 用普通 `ssh` 连上来、在仪器电脑上干活，那台电脑上什么都不用装。要用 MAST 就像坐在那台机器前
  一样用。
- **经 VPN 用插件。** 另一台电脑自己运行 `mast` MCP 服务，隔着 VPN 调 MAST 的局域网模式接口
  （https 加登录）。想在另一台电脑的 MCP 客户端里拿到 MAST 的 `mast_*` 工具时，用这种方式。

### 用 SSH over Tailscale 接入，一步步

1. **两台电脑都安装 Tailscale**，并登录同一个 Tailscale 账号。
2. **在仪器电脑上点 MAST 启动器的「设置 AI 远程访问」按钮**，按向导完成（需要管理员权限，动手前会先
   弹窗征得同意）。它会开启 Windows 自带的 OpenSSH（之后开机自启——仪器电脑靠它保持可达）、生成一把
   专用密钥并授权、在防火墙里把 SSH 只放行给 Tailscale 网段（`100.64.0.0/10`），并打印一行 `ssh`
   命令。私钥保存在 `%USERPROFILE%\.ssh\ai-remote`。
3. **把私钥文件和那行 `ssh` 命令拿到另一台电脑**，让 Claude Code 或 Codex 用它连接，例如
   `ssh -i <私钥> -p 22 <用户>@<Tailscale IP>`。它用自己的 shell 跑 `ssh`，那台电脑上不用装别的、也
   不用额外登录。
4. **在这个命令行里、于仪器电脑上工作。** 连上来的 AI 能做登录账户在那台电脑上能做的一切。要通过
   MAST 操仪器，就在那台机器上照本机方式用 MAST（`mast` 命令行）；想在远程 MCP 客户端里拿到完整的
   `mast_*` 工具，走下面「经 VPN 用插件」。

还要知道的几点：

- **收回访问：** 删掉脚本指出的 `authorized_keys` 文件里那一行密钥，或者直接停用 SSH 服务
  （`Stop-Service sshd; Set-Service sshd -StartupType Manual`）。
- **谁能连上：** 只有你 tailnet 里、并且持有密钥的设备。SSH 自身不提权；无遥测、不回连。
- **设置脚本是独立的**（`setup-ai-ssh.ps1`，随 MAST 发布、但与 MAST 无耦合），启动器按钮只是替你跑它。

### 共享仪器机：换人（不同时）

多人轮流用同一台仪器电脑时，按操作员换 Tailscale 账号即可，不必同时在线。

1. **上一任结束：** 仪器空闲，写好 `WORKING.md`，再 `mast_handover`。
2. **仪器机切到新人的 Tailscale 账号**（或登出旧账号）。
3. **等启动器刷新：** 「远程 - Tailscale」地址与 AI 远程访问里的 `ssh` 粘贴行会跟上新的 `100.x`（约一分钟，也可点「刷新连接信息」）。
4. **若走 LAN/MCP 插件路径：** 重启主服务一次，让证书 SAN 带上新地址。
5. **新人把更新后的 `ssh` 行**（以及原有私钥文件路径）拷到自己的工作机。

**不必吊销旧 SSH 密钥：** 仪器机离开旧 tailnet 后，上一任即不可达。只有在你主动要收回同一 tailnet 内的访问时，才需要卸载 AI 远程访问或删 `authorized_keys`。

### 经 VPN 用插件

从另一台电脑调用 MAST 的接口，只支持经 VPN，而且要先打开 LAN 模式：MAST 这时会用 `https://`
（自签名证书）加 HTTP Basic 登录来提供服务。没有别的认证层兜底。

MCP 服务器自己就会强制回环默认值这一点：不是本机的地址会被拒绝，直到打开 `allow_remote`。
它没有办法核实你打开之后用的那个地址是不是真的在 VPN 里 —— 让它待在那里是你自己的责任；
绝不要把它指向一个公网可达的地址。自签名证书需要把 `verify_tls` 设为 `false`，或者在本机装上
那张证书。对 Codex 来说，这几个开关是你要手动加进 `[mcp_servers.mast.env]` 的环境变量：
`MAST_ALLOW_REMOTE = "true"`、`MAST_USER`、`MAST_PASSWORD`，碰上自签名证书再加
`MAST_VERIFY_TLS = "false"`。还有一个提前知道能省不少事的坑：如果你填的是一个恰好落在浏览器
内置 HSTS 预加载名单里的**主机名**，浏览器会连「仍要继续」这个接受自签名证书的选项都不给 ——
这种情况下改用数字 IP 地址。

打开局域网访问，也会改变同一台电脑上客户端的接法：这时 `127.0.0.1` 也用 `https://` 应答，带着
自签名证书和登录。没人需要远程访问的话，在启动器里取消勾选局域网访问并重启主服务，默认地址
就能用。否则把 `mast_url`（`https://127.0.0.1:7862`）、`mast_user`、`mast_password` 和
`verify_tls`（`false`）一起设好，再重启 MCP 服务器 —— Claude Code 里是 `/mcp`，然后重连
`mast`。一次设齐：每漏一项，就要多重连一次。

### 经 Tailscale 用插件，一步步

1. **两台电脑都安装 Tailscale**（仪器电脑，和你实际工作的那台），并登录同一个 Tailscale 账号。
   在仪器电脑上运行 `tailscale ip -4` 可看到它的 Tailscale 地址（以 `100.` 开头）；MAST 设置页的
   「跨网远程访问（Tailscale）」里显示的也是这个地址。
2. **在仪器电脑上打开远程访问。** 在 MAST 启动器的「局域网 / 远程访问」里勾选启用
   （*启用：其它电脑可通过局域网 IP 或 Tailscale 远程访问 MAST*），设置用户名和密码，
   然后点「重启主服务」。不重启不会生效。
3. **先用浏览器验证。** 在另一台电脑上打开 `https://<Tailscale IP>:7862`，第一次提示证书不受信任时
   点「高级」再「继续」，输入用户名和密码登录。请用数字地址，**不要**用 `*.ts.net` 名字：
   浏览器对这个域名上的自签名证书直接拒绝，连「继续」都不给。
4. **让 Claude Code 连过来。** 在另一台电脑上按 [A. Claude Code](#a-claude-code) 安装插件
   （`/plugin marketplace add HamsterPark/MAST`，再 `/plugin install mast@mast`），
   并设置插件选项：

```text
mast_url       https://<Tailscale IP>:7862
mast_user      <the username from step 2>
mast_password  <the password from step 2>
verify_tls     false
allow_remote   true
```

然后重连服务器（`/mcp`，选 `mast`），让 Claude 先读 briefing。工具、安全闸门和「一次动作、一次观察」
的规则与在仪器电脑上完全一样。有两点不同：那台电脑上要有 Python 3.10 或更新版本给 `mast` 插件用
（把 `python` 选项指向真正的解释器）；探测器相关的接口仍只接受仪器电脑本机的请求
（见[故障排查](08-troubleshooting.md)）。连不上时，同一页列了常见原因（没登录同一个 Tailscale
账号、防火墙、代理）。

请把 MAST 留在 tailnet 内：不要用 Tailscale Funnel 或任何端口转发把它公开出去。

## 首次会话

Codex 和 Claude Code 调的是同一组工具。下面每一步都是一次调用；第一次接入时按顺序走一遍。
在真实仪器上，头几个动作先和操作员商量好：那里的头几次会话都要有人在旁看着。

1. **简报。** `mast_briefing` —— 先读它再做别的。里面有针尖、实时读数、操作员的偏好、
   所有人最近的动作，以及你自己尚未得到答复的请求。
2. **作用域。** `mast_scope`，带上 `experiment_name`、`goal`、`sample_name` —— 选定或新建
   实验与样品。没有样品，扫描与谱学在门口就被拒；没有实验，你做的一切都不会进记录。实验同时
   决定了工作区：它的帧档案、它的 `WORKING.md`、它的历史。
3. **笔记。** `mast_read_guide` 与 `mast_read_working` —— 之前的会话对这台仪器摸清了什么，
   这个实验进行到了哪一步。
4. **看一眼。** `mast_observe` —— 最新一帧的图像、仪器状态，外加一个 `next_token`。看那张图。
5. **做一个动作。** `mast_act`，一个动作，`after` 填刚才那个 `next_token`，比如一次小范围扫描
   （参数见下）。动作在 `wait_s`（默认 45 s）之内做完的话，答复就是它的观测：新的一帧和一个
   新的 `next_token`。还在跑的话，答复会写 `state: running` 并给出 `job_id`：调用 `mast_wait`，
   条件是 `{"type": "job", "job_id": ...}`，拿它的观测；在此期间不要再做动作。
6. **看结果。** 决定任何事之前先看新的图像（在 Codex 的 Code Mode 里，对结果调用 `image()`）。
7. **量一量。** 数值从归档数据里读，不要从图上估：精确数值用 `mast_read_values`，距离和台阶
   高度用 `mast_line_profile`，周期用 `mast_fft_peaks`，数特征、定位置用 `mast_detect_blobs`，
   粗糙度和噪声用 `mast_stats`，两帧对比用 `mast_diff`，放大细看用 `mast_inspect`。
8. **记下来。** `mast_write_working`：计划、下一步预期看到什么、还要用到的帧 id。以后的实验也
   成立的经验写进 `mast_write_guide`。
9. **下一个动作**，带上你拿到的最新那次观测的令牌。
10. **交接。** 让仪器空闲（没有在跑的动作，扫描已停），确认 `WORKING.md` 已经存好，然后调用
    `mast_handover` 写一份总结，哪怕会话很短；它让下一个接手的人或 agent 知道发生过什么。

第 5 步那个动作的参数：

```json
{"action": "scan", "params": {"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 50}, "after": "K7QM-29XA", "note": "expect terraces separated by monatomic steps"}
```

令牌是占位符：换成你自己那次观测返回的 `next_token`。动作的单位是 nm、V、pA、s 和 nm/s；位置
写成扫描坐标系里的 `{"x_nm": .., "y_nm": ..}`，或者写成某张已归档帧上的显示像素
`{"frame": "t0007.s0", "x": .., "y": ..}`。扫描里没填的可选参数，按仪器针对这个尺寸的扫描策略
补齐。用 `mast_run` 跑的技能仍用它们自己的单位，也就是 SI 基本单位（见 [概念](02-concepts.md)）。

## 用纯 HTTP 走一遍

6.5.0 的这一遍直接用作业 API：健康检查、简报、作用域、找一个技能、读它的卡片、把一次无害的
读取当作业提交、轮询它、交接。

```bash
BASE=http://127.0.0.1:7862/api/ext/v1
curl -s "$BASE/health"
curl -s "$BASE/briefing" -H "X-MAST-Actor: claude-code"
curl -s -X POST "$BASE/scope" -H "Content-Type: application/json" -H "X-MAST-Actor: claude-code" \
  -d '{"experiment": {"name": "Quickstart"}, "sample": {"name": "Bench sample"}}'
curl -s "$BASE/skills/search?q=bias" -H "X-MAST-Actor: claude-code"
curl -s "$BASE/skills/GetBias" -H "X-MAST-Actor: claude-code"
curl -s -X POST "$BASE/jobs" -H "Content-Type: application/json" -H "X-MAST-Actor: claude-code" \
  -d '{"skill": "GetBias", "params": {}, "request_id": "quickstart-001"}'
curl -s "$BASE/jobs/j_0123456789ab?wait_s=30" -H "X-MAST-Actor: claude-code"
curl -s -X POST "$BASE/handover" -H "Content-Type: application/json" -H "X-MAST-Actor: claude-code" \
  -d '{"summary": "Quickstart walkthrough: read the bias once."}'
```

用 HTTP 走视觉回路：先观测一次；再带着这次观测的令牌做一个只让时间流过的动作（`wait`，5 秒），
并让它立刻返回、不等自己做完（`wait_s` 为 0）；最后在服务端等这个动作的作业，等到的答复里就带着
这个动作的观测。

```bash
BASE=http://127.0.0.1:7862/api/ext/v1
H1="Content-Type: application/json"; H2="X-MAST-Actor: my-script"; H3="X-MAST-Session: quickstart"
curl -s -X POST "$BASE/vista/observe" -H "$H1" -H "$H2" -H "$H3" -d '{}'
curl -s -X POST "$BASE/vista/act" -H "$H1" -H "$H2" -H "$H3" \
  -d '{"action": "wait", "params": {"seconds": 5}, "after": "K7QM-29XA", "wait_s": 0, "request_id": "quickstart-act-001"}'
curl -s -X POST "$BASE/wait" -H "$H1" -H "$H2" -H "$H3" \
  -d '{"conditions": [{"type": "job", "job_id": "j_0123456789ab"}], "timeout_s": 600}'
```

令牌和作业 id 都是占位符：换成你自己的答复里返回的那些。这些答复里每个字段的含义见
[API 参考](07-api-reference.md)；你先定了作用域之后到底记下了什么，见
[记录与上下文](04-records-and-context.md)。
