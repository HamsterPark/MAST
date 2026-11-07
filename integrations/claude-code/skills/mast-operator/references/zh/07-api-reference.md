# MAST 外部 agent API v1 参考

> 本页由 `scripts/gen_ext_api_reference.py` 从 API 自己的 OpenAPI 生成，请勿手改。

基础地址：`http://127.0.0.1:7862/api/ext/v1`（远程见 [快速上手](01-quickstart.md)）。机器可读的契约在 `GET /openapi.json`，交互式文档在 `/docs`。v1 之内只增不减。

## 通用约定

| 请求头 | 含义 |
|---|---|
| `X-MAST-Actor` | 你的名字（可选）。清洗成小写的 `[\w.-]`，最长 48；空 = `anonymous`。它只用于**归属**：动作、笔记、请求、组合技能都署 `ext:<名字>`。它不是认证，也永远不会是拒绝请求的理由。 |
| `X-MAST-Session` | 会话名（可选，最长 64）。实验记录里的署名变成 `ext:<名字>/<会话>`，用来区分同一个名字的两次接入。 |

认证与主 API 相同：本机回环不需要；局域网模式是 HTTP Basic + TLS。每个响应都带 `X-MAST-Ext-Version: 1`。

外部面只给程序化客户端（MCP server、脚本、`curl`）用：**每个 POST 都必须带 `Content-Type: application/json` 和一个 JSON 请求体**（没有内容就发 `{}`，`/estop` 与 `/jobs/{job_id}/cancel` 也一样），浏览器发起的跨站请求一律拒绝。

除 `/data/file` 与 `/data/frame` 外，响应都是 JSON。错误体的形状固定，`detail` 是给人读的说明（目前是中文）：

```json
{"error": "request_id_conflict", "detail": "...", "existing_job_id": "j_0123456789ab"}
```

| 状态码 | 含义 |
|---|---|
| 200 / 202 | 成功；202 = 新作业已受理。**技能自身失败也是 200**：看作业的 `state` 与 `result.success`。 |
| 403 | 路径不在允许的根目录之内，或扩展名不在白名单里；或 `cross_origin_refused`：浏览器发起的跨站请求（`Origin` 不是本服务、`Sec-Fetch-Site` 是跨站，或经陌生主机名访问 —— DNS 重绑定的形状）。 |
| 404 | 资源不存在（技能、作业、实验、样品、请求、文件、通道、路径）。 |
| 409 | 冲突：`request_id` 已用于另一份内容；仪器正被占用时切作用域；一动一观的拒绝（`observe_first`、`act_in_flight`、`same_code_cell`、`checkpoint_requested`、`session_closed`、`use_act`）；另一个运行在跑。 |
| 415 | `unsupported_media_type`：写请求没有带 `application/json`。 |
| 422 | 请求不合法；技能在本机被关闭。 |
| 429 | 同时在跑的作业达到上限。 |
| 503 | 子系统未接线（带 `missing`），或服务正在关停。 |

## 端点一览

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 外部面自己的健康与接线 |
| GET | `/guide` | 面向 agent 的铁律 |
| GET | `/status` | 快速状态（零硬件 I/O） |
| GET | `/briefing` | 分段简报：开始工作前先读 |
| GET | `/scope` | 当前实验与样品；动作会不会被记录 |
| POST | `/scope` | 切换或新建实验 / 样品 |
| POST | `/estop` | 硬件急停并取消外部作业 |
| POST | `/jobs` | 提交技能作业（立即返回） |
| GET | `/jobs` | 你最近的作业 |
| GET | `/jobs/{job_id}` | 作业视图，可长轮询 |
| POST | `/jobs/{job_id}/cancel` | 协作式取消 |
| GET | `/skills/search` | 按动作 / Nanonis 命令找技能 |
| GET | `/skills/{name}` | 技能卡 |
| POST | `/composites/draft` | 校验组合技能草稿（不保存） |
| POST | `/composites` | 保存并热注册组合技能 |
| POST | `/skills/proposals` | 提议 Python 技能（待人审） |
| GET | `/data/files` | 最近的数据文件 |
| GET | `/data/search` | 按元数据检索数据记录 |
| GET | `/data/file` | 原始字节 |
| GET | `/data/frame` | 统一朝向后的 `.sxm` 帧（`.npz`） |
| POST | `/notes` | 写笔记进 MAST 记忆库 |
| GET | `/notes` | 检索笔记（含内部 agent 写的） |
| POST | `/requests` | 向操作员发问 |
| GET | `/requests` | 你的请求与答复 |
| GET | `/requests/{request_id}` | 单条请求与答复 |
| POST | `/handover` | 交接报告存进文档库 |
| POST | `/vista/hello` | 登记客户端会话（之后驱动仪器只能走动作） |
| POST | `/vista/observe` | 最新观测与新令牌 |
| POST | `/vista/act` | 执行一步动作（带令牌） |
| GET | `/vista/acts/{job_id}` | 动作作业的状态与观测 |
| GET | `/vista/status` | 工作区与闸门状态（不含令牌） |
| POST | `/vista/inspect` | 放大镜：回看任意帧 |
| POST | `/vista/read_values` | 精确读数 |
| POST | `/vista/measure/{tool}` | 量尺：剖面、FFT、计数、差分、统计 |
| GET | `/vista/notes/{which}` | 读 GUIDE.md / WORKING.md |
| PUT | `/vista/notes/{which}` | 写 GUIDE.md / WORKING.md |
| GET | `/vista/history` | 本实验的动作记录与汇总 |
| POST | `/wait` | 服务端等待：作业、新帧、告警、答复、探测器、定时 |
| POST | `/detectors` | 建一个分析探测器（pyexec） |
| GET | `/detectors` | 探测器清单 |
| GET | `/detectors/{detector_id}` | 一个探测器及其命中 |
| POST | `/detectors/{detector_id}/enable` | 启用 / 停用 |
| DELETE | `/detectors/{detector_id}` | 删除探测器 |
| POST | `/detectors/{detector_id}/test` | 在一帧上试跑一次 |
| POST | `/literature/search` | 文献知识库检索 |
| POST | `/runner/start` | 启动无人值守运行（Claude Code，订阅） |
| GET | `/runner` | 最近的运行 |
| GET | `/runner/{run_id}` | 一次运行的状态 |
| POST | `/runner/{run_id}/stop` | 操作员叫停 |
| POST | `/runner/{run_id}/hook` | 钩子中转（运行器自用） |
| GET | `/optical-layouts` | 用户画的光路图清单 |
| GET | `/optical-layouts/{layout_id}` | 一张光路图：几何、追迹、文字叙述、链接状态、评论 |
| GET | `/optical-layouts/{layout_id}/render.png` | 这张图的 PNG |
| POST | `/optical-layouts/{layout_id}/comments` | 发评论（不能改图、不能解决评论） |

## 总览、作用域与急停

### `GET /health`

外部面自己的健康：各子系统有没有接上（`runtime`、`registry`、`pool`、`state`、`storage`、`cognition`、`jobs`）。`missing` 非空时，依赖那些子系统的端点会回 503 `not_wired`。不需要仪器在线。

### `GET /guide`

面向 agent 的铁律清单，`lang=zh|en`。返回 `{lang, rules:[{id, title, text}], docs_hint}`。规则的原因与细节见 [操作规则](03-operating-rules.md)。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `lang` | query | `string` |  | `"en"` |  |

### `GET /status`

快速状态，只读内存与缓存快照、**不碰硬件**：`mode`（`safe` / `semi` / `auto`；没人设置过时是 `unknown`，不会被说成某个具体值）、`abort`（`{set, emergency, why}`）、`lock`（仪器锁：`{held:false}` 或 `{held, owner, skill, held_s}`）、`connection`（各 TCP 角色连没连上）、`live`（偏压、电流、设定点、Z、Z 控制、扫描中、是否退针、`stale`）、`scope`、`jobs`（`{running, max}`）、`degraded`（读不到的字段名）。

### `GET /briefing`

分段简报：MAST 自己的 agent 每一轮看到的上下文块，加上「在你之前谁做了什么」。`sections=a,b` 只取某几段（空 = 全部；未知段名 422，回执里有 `available`）。每段 `{ok, text?, data?, error?}`；**任何一段读不到只让那一段 `ok:false` 并进 `degraded`**，其余照给。顶层 `text` 是各段文字拼成的 markdown，截断到约 12000 字符。零硬件 I/O。各段内容见下文 [简报的段](#简报的段)。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `sections` | query | `string` |  | `""` |  |

### `GET /scope`

当前实验、样品与实验记录服务是否可用：`{experiment:{id, name, goal}|null, sample:{id, name, sample_type}|null, recording:{v1, v2, note}}`。没有当前实验时，动作不能归入实验动作记录，但独立作业日志与全局笔记仍可存在。这里描述当前配置；实际写入结果见各作业的 `recorded`。

### `POST /scope`

切换或新建实验 / 样品。给 `id` 就切换到已有的（切回一个实验时恢复它上次用过的样品；切到别的实验下的样品会把实验一起切过去）；给 `name` 就按名字复用，没有才新建（样品在当前实验内查找）。仪器正被占用时默认 409 `instrument_busy`（在跑的动作会记到新作用域下），`force:true` 才切并在 `warnings` 里写明。其他错误：404 `unknown_experiment` / `unknown_sample`、409 `no_experiment`（没有实验就建样品）、422 `empty_request`。回执 = `GET /scope` 的内容 + `changed` + `warnings`。

**请求体** (`ScopeBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `experiment` | `ScopeRef \| null` |  |  |  |
| `sample` | `ScopeRef \| null` |  |  |  |
| `force` | `boolean` |  | `false` |  |
| `reason` | `string` |  | `""` | `maxLength=300` |

其中 `ScopeRef`:

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `id` | `string \| null` |  |  | `maxLength=120` |
| `name` | `string \| null` |  |  | `maxLength=200` |
| `goal` | `string` |  | `""` | `maxLength=2000` |
| `description` | `string` |  | `""` | `maxLength=2000` |
| `sample_type` | `string` |  | `""` | `maxLength=120` |

```json
{"experiment": {"name": "Au(111) reconstruction", "goal": "map the herringbone"},
 "sample": {"name": "Au(111) #3", "sample_type": "metal"}}
```

### `POST /estop`

硬件急停：调用与操作员急停按钮相同的流程，先挂中止与急停闩，再尝试停止运动和退针。闩上的原因写成「外部 agent ext:<名字> 触发急停：<reason>」；然后请求取消仍在运行的外部作业。回执含 `why`、`cancelled_jobs` 与急停动作本身的结果（`errors`、`retracted` 等）。闩已置位不代表硬件动作成功，必须检查结果并核实仪器状态。**解闩是操作员的事**，外部面不提供解闩。

**请求体** (`EstopBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `reason` | `string` |  | `""` | `maxLength=300` |

## 作业

### `POST /jobs`

提交一个技能作业，立即返回（新作业 202 + 作业视图）。门口按顺序检查：技能存在（否则 404 `unknown_skill`，带 `did_you_mean`）→ 技能及其声明式子步都没被本机关闭（否则 422 `skill_disabled`）→ `request_id` 幂等（同一调用方同一 id：内容相同回原作业 200 + `idempotent_replay:true`；内容不同 409 `request_id_conflict`）→ 并发上限（429 `too_many_jobs`；停止 / 退针类技能如 `StopScan` 不受上限约束）。带量纲的参数可写 `"5n"` 这类带 SI 前缀的字符串。**技能自身失败不是 HTTP 错误**：作业以终态结束。提交丢了回执时，用**同一个** `request_id` 重发。

**请求体** (`JobSubmit`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `skill` | `string` | 是 |  | `minLength=1, maxLength=120` |
| `params` | `object` |  |  |  |
| `request_id` | `string \| null` |  |  | `maxLength=128` |
| `note` | `string` |  | `""` | `maxLength=500` |

```bash
curl -s -X POST http://127.0.0.1:7862/api/ext/v1/jobs \
  -H "Content-Type: application/json" -H "X-MAST-Actor: claude-code" \
  -d '{"skill": "GetBias", "params": {}, "request_id": "getbias-001"}'
curl -s "http://127.0.0.1:7862/api/ext/v1/jobs/j_0123456789ab?wait_s=30" \
  -H "X-MAST-Actor: claude-code"
```

### `GET /jobs`

你最近提交的作业（新的在前）：`{count, jobs:[作业视图]}`。`all=true` 看所有调用方的，`state=` 按状态过滤。丢了上下文之后用它找回自己提交过什么。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `all` | query | `boolean` |  | `false` |  |
| `state` | query | `string` |  | `""` |  |
| `limit` | query | `integer` |  | `50` | `min=1, max=300` |

### `GET /jobs/{job_id}`

作业视图（见 [作业视图](#作业视图)）。`wait_s`（0–30）> 0 时等到作业结束或超时再回；服务端等待不占线程。超过保留数量的旧作业会被丢弃（404 `unknown_job`）。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `job_id` | path | `string` | 是 |  |  |
| `wait_s` | query | `number` |  | `0.0` | `min=0.0, max=30.0` |

### `POST /jobs/{job_id}/cancel`

协作式取消：技能在下一次检查中止处停下，原因（谁、为什么）会传到技能与记录里。卡在一条阻塞的 Nanonis 命令里时要等那条命令返回。请求物理停止时，运行不受仪器锁阻挡的停止类技能（例如 `StopScan`），危险时调用 `POST /estop`；检查响应并确认仪器状态。对已结束的作业是空操作。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `job_id` | path | `string` | 是 |  |  |

**请求体** (`CancelBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `reason` | `string` |  | `""` | `maxLength=300` |

## 技能

### `GET /skills/search`

按**动作**找技能：技能按「治什么问题」命名，按名字猜很难猜中。`q` 会同时匹配名字、Nanonis 命令名（如 `Bias_Set`、`Scan_Action`，从技能源码静态读出）、意图词、标签、参数名与描述；官方技能排在前面。每条结果带 `footprint`（`pure-analysis` / `hardware-read-only` / `hardware-write` / `unknown`）、`matched_on`（为什么命中）与 `origin_code`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `q` | query | `string` | 是 |  | `minLength=1, maxLength=200` |
| `limit` | query | `integer` |  | `20` | `min=1, max=100` |

### `GET /skills/{name}`

技能卡：运行前该读的一切。字段见下文 [技能卡](#技能卡)。未知名字 404，带 `did_you_mean`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `name` | path | `string` | 是 |  |  |

### `POST /composites/draft`

校验一份组合技能草稿（`CompositeSpec` JSON），**不落盘、不注册**，问题整批返回，外加与已有官方技能的重合提示。`spec: "?"` 返回格式说明。写法见 [编写技能](05-authoring-skills.md)。

**请求体** (`CompositeBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `spec` | `object \| string` | 是 |  |  |
| `base_version` | `integer` |  | `-1` |  |

### `POST /composites`

保存组合技能并热注册，署名 `ext:<名字>`。之后它就是一个普通技能：经 `POST /jobs` 执行，每个子步都过全部安全闸。改自己存过的同名技能要带 `base_version`（乐观锁；缺了回 `base_version_required`）；人做的技能不能被覆盖。业务上的拒绝是 200 + `ok:false` + `error`。

**请求体** (`CompositeBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `spec` | `object \| string` | 是 |  |  |
| `base_version` | `integer` |  | `-1` |  |

### `POST /skills/proposals`

提议一个新的原子技能（Python 源码）—— 组合表达不了时才用。写进本机的自定义技能目录**等人审**：不注册、不执行；要用，操作员审过代码、在启用清单里打开、重启。回执附一份合规报告 `compliance`（与投稿校验器同一套判据）。含禁用写法（如 `import os`）的代码直接 `ok:false`、`error:"rejected"`，不落盘。

**请求体** (`ProposalBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `name` | `string` | 是 |  | `minLength=1, maxLength=80` |
| `code` | `string` | 是 |  | `minLength=1, maxLength=200000` |
| `rationale` | `string` | 是 |  | `minLength=1, maxLength=4000` |

## 数据

### `GET /data/files`

最近的数据文件（mtime 新的在前；同一份测量的多个拷贝折叠成一条，`locations` 列出每一份）：`{files:[{path, name, ext, mtime, size_bytes, kind, copies, locations}], count, has_more, degraded, counts_by_ext}`。只列允许根目录之内的文件；**不接受客户端指定目录**。已在当前实验 VISTA 档案里的文件另带 `frame`（帧号）。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `n` | query | `integer` |  | `20` | `min=1, max=200` |
| `ext` | query | `string \| null` |  |  |  |
| `offset` | query | `integer` |  | `0` | `min=0` |

### `GET /data/search`

按元数据检索数据记录：数据图库的索引（尺寸、像素、偏压、设定点、通道、谱的扫描范围、操作员的评级 / 标签 / 备注），加上索引里还没有的最新文件（现场读头，一次最多 200 个）。查询参数都可选、可组合：`ext`（`sxm,dat,3ds`）、`experiment` / `sample`（id、≥6 位十六进制前缀或名字的一部分）、`since` / `until`（开始时间；`YYYY-MM-DD[ HH:MM[:SS]]`、ISO 或 epoch 秒，不带时区按仪器电脑本地时间；`until` 只给日期时含那一整天）、`size_min_nm` / `size_max_nm`、`bias_min_v` / `bias_max_v`、`setpoint_min_pa` / `setpoint_max_pa`、`text`、`tag`、`min_rating`（2 重点、1 可用、-1 排除）、`order`、`n`、`offset`、`live`。回答：`{items:[{path, name, ext, kind, time, saved, experiment, sample, frame, fetchable, source, …}], count, total, offset, has_more, query, index:{available, written_at, items, roots}, live:{examined, added, truncated, max}, frames:{workspace, known}, notes?, degraded}`；`experiment` / `sample` / `frame` 说不出时为 null；`fetchable: false` 表示路径在数据出口允许的根之外（约束不变）。读不懂的时间或类型 422 `bad_query`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `ext` | query | `string \| null` |  |  |  |
| `experiment` | query | `string \| null` |  |  | `maxLength=200` |
| `sample` | query | `string \| null` |  |  | `maxLength=200` |
| `since` | query | `string \| null` |  |  | `maxLength=40` |
| `until` | query | `string \| null` |  |  | `maxLength=40` |
| `size_min_nm` | query | `number \| null` |  |  | `min=0` |
| `size_max_nm` | query | `number \| null` |  |  | `min=0` |
| `bias_min_v` | query | `number \| null` |  |  |  |
| `bias_max_v` | query | `number \| null` |  |  |  |
| `setpoint_min_pa` | query | `number \| null` |  |  |  |
| `setpoint_max_pa` | query | `number \| null` |  |  |  |
| `text` | query | `string \| null` |  |  | `maxLength=200` |
| `tag` | query | `string \| null` |  |  | `maxLength=80` |
| `min_rating` | query | `integer \| null` |  |  | `min=-1, max=2` |
| `order` | query | `string` |  | `"newest"` |  |
| `n` | query | `integer` |  | `50` | `min=1, max=200` |
| `offset` | query | `integer` |  | `0` | `min=0` |
| `live` | query | `boolean` |  | `true` |  |

### `GET /data/file`

原始字节（`application/octet-stream`）。路径先解析（`..`、符号链接）再判归属：只许数据搜索目录与实验根目录之内、扩展名在白名单里（`.sxm .dat .3ds .txt .csv .png .npy .npz`）的文件；网络路径一律拒绝。错误：403 `path_not_allowed` / `extension_not_allowed`、404 `not_found`、422 `bad_path`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `path` | query | `string` | 是 |  |  |

### `GET /data/frame`

一个 `.sxm` 通道的帧，**服务端统一朝向**后打包成 `.npz`：`forward` / `backward` 的第 0 行是图的顶边（`scan_dir=up` 已翻转），`backward` 已去镜像（与 `forward` 同一几何朝向）；只有反向数据的通道，那一块作为 `forward` 给出，`meta_json` 的 `served_block` 如实写明。`meta_json` 另含宽高（nm）、nm/px、偏压、设定点、扫描方向、单位、可用通道；同一份元数据也在响应头 `X-MAST-Frame-Meta`（ASCII JSON）。未知通道 404 `unknown_channel`（带 `channels`）；非 `.sxm` 422 `not_sxm`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `path` | query | `string` | 是 |  |  |
| `channel` | query | `string` |  | `"Z"` |  |

```python
import io, json, urllib.parse, urllib.request
import numpy as np

BASE = "http://127.0.0.1:7862/api/ext/v1"
q = urllib.parse.urlencode({"path": path, "channel": "Z"})
body = urllib.request.urlopen(f"{BASE}/data/frame?{q}").read()
z = np.load(io.BytesIO(body), allow_pickle=False)
meta = json.loads(str(z["meta_json"]))
forward = z["forward"]
backward = z["backward"] if "backward" in z.files else None
```

## 笔记、发问与交接

### `POST /notes`

写一条笔记进 MAST 的记忆库，**MAST 自己的 agent 会自动召回它**。`scope=experiment` 存进当前实验（没有实验时退到 global 并在 `warnings` 里写明），`global` 跨实验。署名 `ext:<名字>`；同标题同内容重发落在同一条上（路径由内容哈希决定），不会重复。

**请求体** (`NoteBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `title` | `string` | 是 |  | `minLength=1, maxLength=200` |
| `content` | `string` | 是 |  | `minLength=1, maxLength=20000` |
| `kind` | `string` |  | `"note"` | `maxLength=40` |
| `tags` | `array<string>` |  |  |  |
| `scope` | `"experiment" \| "global"` |  | `"experiment"` |  |

```json
{"title": "tip state", "content": "atomic contrast at -1.2 V / 50 pA",
 "kind": "insight", "tags": ["tip"], "scope": "experiment"}
```

### `GET /notes`

检索笔记：`q` 非空时召回（当前实验 ∪ global；语义索引不可用时退回子串匹配），为空时列最近的。MAST 自己的 agent 写下的记忆也在这里（看 `author`）。每条带 `match`（semantic / substring / recent）与 `score`（语义召回时为余弦相似度 −1..1，其余 null）：最近的几条总会回来，分数低的就是无关的。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `q` | query | `string` |  | `""` | `maxLength=500` |
| `scope` | query | `string` |  | `"both"` |  |
| `limit` | query | `integer` |  | `10` | `min=1, max=50` |

### `POST /requests`

向操作员发一个问题或请求，界面上会亮起来。回执 `{ok, request:{id, status, ...}}`。**不要在这里等** —— 答复是异步的，之后用 `GET /requests/{request_id}` 或简报的 `operator_requests` 段看，或在 `POST /wait` 里等 `operator_reply`。同一个未答的问题重发不会重复。`kind` 是 `question` / `action` / `info`（或别的自由词）时是自由文本请求，操作员回一段文字。`kind` 是 `confirm` / `choice` / `number` / `text` / `attest` 时是**结构化提示**：`options`（`[{id, label}]`，choice 至少两个）、`number_min` / `number_max` / `unit`、`attest_slots`（attest：操作员的答复要写进的手动光学状态槽位，必填）、`severity`（`info` / `action` / `safety`）、`timeout_s`（至多 7 天，不给就是 24 小时，之后按超时关闭）；网页提示卡、启动器弹窗和声音同时提醒，**只有操作员能答，你不能**。结构化字段配自由文本 `kind` 回 422 `structured_fields_need_kind`，提示本身不合法回 422 `invalid_prompt`。**结构化提示只有光学站有**：STM 站上每个请求都是文字请求，`kind` 写什么都原样存，结构化字段被忽略、不报错。attest 里操作员看到的内容来自槽位定义，不来自你：是非槽位（如 `laser_area_safe`）不能带 `options`，choice 槽位只能从它自己的选项里挑（显示的是槽位定义的名字），number 槽位的单位和范围取槽位定义，一个提示里的槽位必须同一类型。你自己同时有 25 个、所有外部 agent 合计有 75 个没答的结构化提示时再发新的回 429 `too_many_open_prompts`（带 `scope`；换名字也一样，操作员那里总共最多 100 个）。

**请求体** (`RequestBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `message` | `string` | 是 |  | `minLength=1, maxLength=4000` |
| `kind` | `string` |  | `"question"` | `maxLength=40` |
| `title` | `string \| null` |  |  | `maxLength=200` |
| `options` | `array<RequestOption> \| null` |  |  |  |
| `number_min` | `number \| null` |  |  |  |
| `number_max` | `number \| null` |  |  |  |
| `unit` | `string \| null` |  |  | `maxLength=20` |
| `attest_slots` | `array<string> \| null` |  |  |  |
| `severity` | `string \| null` |  |  | `maxLength=20` |
| `timeout_s` | `number \| null` |  |  | `>=0.0, max=604800.0` |

其中 `RequestOption`:

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `id` | `string` | 是 |  | `minLength=1, maxLength=64` |
| `label` | `string` | 是 |  | `minLength=1, maxLength=200` |

### `GET /requests`

你发过的请求与答复（未答的在前）：`{count, requests}`，自由文本请求和结构化提示都在。`status` 过滤：`pending`（未答，同义 `open`）、`done`、`dismissed`；未知值回 422 `unknown_status`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `status` | query | `string` |  | `""` |  |

### `GET /requests/{request_id}`

单条请求：`note` 是文字答复，`path` 是操作员给的文件或目录路径（两者可能都有）。结构化提示（id 形如 `op-0001-…`）另带 `structured: true`、精确状态 `prompt_status`（`open` / `answered` / `cancelled` / `timeout` / `dismissed`；`status` 仍是 `pending` / `done` / `dismissed`）和 `answer`：`{status, choice, value, note, answered_by, source, answered_at, ok}`，答复前是 null。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `request_id` | path | `string` | 是 |  |  |

### `POST /handover`

生成交接报告存进 MAST 的文档库（操作员在「报告」页能看到），署名 `ext:<名字>`：你写的 `summary` 与 `next_steps`，你这次提交的作业及结局，实验记录里署你名字的动作，你写的笔记。`since`（ISO 时间）只汇总此后的内容。回执 `{ok, doc_id, version, path, title, jobs, actions, notes}`。会话结束前写一份。

**请求体** (`HandoverBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `summary` | `string` | 是 |  | `minLength=1, maxLength=20000` |
| `next_steps` | `string \| array<string>` |  |  |  |
| `title` | `string` |  | `""` | `maxLength=200` |
| `since` | `string \| null` |  |  | `maxLength=40` |

## VISTA：观测与动作（一动一观）

### `POST /vista/hello`

登记这个客户端会话（`X-MAST-Session`）并返回概况：`{session, actor, workspace, turn, strict, gate, frames, spectra, notes}`。任何 `/vista/*` 调用都会顺带登记；登记之后，这个会话里**驱动仪器的技能不能再走 `POST /jobs`**（409 `use_act`），只能走 `POST /vista/act` 并带上观测令牌。读、分析、停止 / 退针不受影响。`client_meta` 是桥转来的客户端信息（Codex 的 `itemId`、模型名等），只用于判据与记录。

**请求体** (`MetaBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `client_meta` | `object \| null` |  |  |  |

### `POST /vista/observe`

最新的观测：工作区里最新一帧（扫描或谱）的图、它的摘要、仪器读数（偏压、设定点、电流、Z、针尖 XY、漂移补偿）、上一个动作的记录，以及**一个新令牌**。开始工作时、令牌丢了时、外来新帧之后都先调它。回答 `{observation:{obs_id, turn, frames, text, images, next_token, token_on_image}}`；`text` 是给模型读的 JSON（里面也有 `next_token`），`images` 至多一张。严格模式下令牌只印在图下方的条带里，`next_token` 为 null。

**请求体** (`MetaBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `client_meta` | `object \| null` |  |  |  |

### `POST /vista/act`

执行**一步**动作：`action` + `params`（VISTA 的动作表：`scan`、`set_bias`、`set_current`、`feedback`、`move_tip`、`pulse`、`poke`、`sts`、`df_curve`、`pll`、`move_atom`、`drift_compensation`、`wait`、`withdraw`、`approach`；单位 nm、V、pA、s；位置是 `{x_nm, y_nm}` 或某个已归档帧的显示像素 `{frame, x, y}`），或者一个原始技能 `skill` + `skill_params`（SI 单位）。驱动仪器的都要带 `after` = 最新观测的令牌；不驱动仪器的原始技能当普通作业跑（`kind: "job"`，令牌不变）。动作是一个 `kind: "act"` 的作业：幂等（`request_id`）、可取消、进 journal、撞锁拒绝。最多等 `wait_s`（≤ 50 s）：做完回 `{kind:"act", state:"done", job_id, turn, ok, job_state, observation}`，没做完回 `state:"running"`，用 `POST /wait`（条件 `{type:"job", job_id}`）去等。一动一观的拒绝都是 409：`session_closed`、`act_in_flight`（附 `job_id`）、`same_code_cell`（同一段 Codex 代码里已经执行过一个动作）、`checkpoint_requested`、`observe_first`（令牌缺失、用过或过期；**附带最新观测与新令牌**）。被拒的动作什么也没执行。

**请求体** (`ActBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `action` | `string \| null` |  |  |  |
| `params` | `object \| null` |  |  |  |
| `skill` | `string \| null` |  |  |  |
| `skill_params` | `object \| null` |  |  |  |
| `note` | `string` |  | `""` | `maxLength=1000` |
| `after` | `string \| null` |  |  |  |
| `request_id` | `string \| null` |  |  | `maxLength=120` |
| `wait_s` | `number` |  | `45.0` | `min=0.0, max=50.0` |
| `client_meta` | `object \| null` |  |  |  |

### `GET /vista/acts/{job_id}`

一个动作作业的当前状态（`wait_s` ≤ 50 可长轮询），形状与 `POST /vista/act` 的回答相同；做完时带着它的观测。这个观测的令牌如果已被更新的观测取代，`next_token` 为 null 并附 `token_note`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `job_id` | path | `string` | 是 |  |  |
| `wait_s` | query | `number` |  | `0.0` | `min=0.0, max=50.0` |

### `GET /vista/status`

工作区、回合号、帧与谱的数量、严格模式、闸门状态（令牌是否有效、绑在哪个会话、哪个动作在跑、失效次数 —— **从不包含令牌本身**）、已登记的客户端、监视线程。零硬件 I/O。

## VISTA：回看、读数、量尺、工作笔记与历史

### `POST /vista/inspect`

放大镜（VISTA `inspect`）：一次最多 16 个视图，每个视图取档案里任意一帧，可裁剪区域、换通道 / 方向、去背景、调对比度，按整数倍放大。回答是 VISTA 的工具回复 `{text, images, is_error}`，每个视图一张图。只读，不碰仪器。

### `POST /vista/read_values`

精确读数（VISTA `read_values`）：在档案帧上按点、线、区域取物理量数值（带单位后缀），最多 64 个视图。精确数字一律从这里取，不要从图上估。

### `POST /vista/measure/{tool}`

量尺：`line_profile`（两点之间的剖面、距离、台阶高度）、`fft_peaks`（周期与晶格）、`detect_blobs`（计数与位置）、`diff`（两帧之差与漂移）、`stats`（统计）。参数与 VISTA 的同名工具相同；结果图作为派生帧（`m`）归档，不算仪器帧，不影响令牌。工具只测量，不下结论。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `tool` | path | `string` | 是 |  |  |

### `GET /vista/notes/{which}`

读 `guide`（GUIDE.md：所有实验共用的长期经验）或 `working`（WORKING.md：这个实验的工作记忆）。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `which` | path | `string` | 是 |  |  |

### `PUT /vista/notes/{which}`

整份替换 GUIDE.md（≤ 64 KiB）或 WORKING.md（≤ 16 KiB）。运行器要求检查点时，写好 WORKING.md 就把检查点标为就绪。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `which` | path | `string` | 是 |  |  |

**请求体** (`NotesBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `content` | `string` | 是 |  |  |
| `client_meta` | `object \| null` |  |  |  |

### `GET /vista/history`

这个实验的客观记录：`view=events` 按回合范围给出动作记录（解析后的参数、控制器结果、之后的仪器状态、新帧编号）、外来帧、探测器命中与检查点；`view=attempts` 是汇总（各动作次数与失败数、帧与谱清单）。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `view` | query | `string` |  | `"events"` |  |
| `start_turn` | query | `integer` |  | `0` | `min=0` |
| `end_turn` | query | `integer \| null` |  |  | `min=0` |
| `limit` | query | `integer` |  | `64` | `min=1, max=128` |

## 等待与探测器

### `POST /wait`

在服务端等到某件事发生，最多 `timeout_s`（默认 600，上限 840；客户端把 MCP 工具超时设到 ≥ 900 s，Codex 是 `tool_timeout_sec = 900`）。`conditions` 里任何一条满足就返回：`job`（`job_id` 的作业结束，动作或普通作业都行）、`frame`（有新帧入档）、`alert`（异常、电流监控或 conduct 告警到 `min_level`：`info` / `warning` / `critical`，默认 `warning`）、`operator_reply`（操作员答复；可指定 `request_id`）、`detector`（探测器命中；可指定 `detector_id`）、`timer`（`seconds` 秒后）。回答 `{fired:[{index, type, event, seq, age_s, backlog}], timed_out, waited_s, cursor, observation?}`：`backlog: true` 表示那件事发生在这次等待开始之前（积压在游标之后），`age_s` 是距现在的秒数 —— 立刻返回的告警可能是几分钟前的事；电流监控告警的 `event` 另带 `segment_start` / `segment_end`（被判的约 1 s 电流，UTC）、`measured`（σ、突跳率、最大台阶与均值，pA）与 `context`（是否在扫描、距上一段扫描几秒、持有仪器的技能与最近一次见到的技能、偏压、设定点）。等到一个做完的动作或一帧新图时，`observation` 带着那次观测（图 + 新令牌）。把 `cursor` 作为下一次的 `since` 传回，两次等待之间的事件一个都不会丢；游标太旧（被挤出缓冲）或来自上一次进程时，立即回一条 `type: "gap"`。客户端断开就提前收场。条件不合法回 422 `invalid_condition` / `invalid_cursor`。

**请求体** (`WaitBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `conditions` | `array<object>` | 是 |  |  |
| `timeout_s` | `number` |  | `600.0` | `min=0.0, max=840.0` |
| `since` | `string \| null` |  |  | `maxLength=128` |

### `POST /detectors`

给当前实验建一个分析探测器：agent 写的 Python，定义 `def detect(frame)`，MAST 在每一帧新入档的扫描或谱（`applies_to`）上用独立解释器（pyexec：碰不到仪器、改不了原始数据、超时就杀、内存有上限）跑一遍。`frame` 有 `kind`、`fid`、`channels`（通道 → 正向二维数组，SI 单位）、`backward`、`meta`，谱另有 `columns` 与 `units`。返回假值（没命中）、`True`，或 `{hit, message, value}`。命中记进工作区事件，并叫醒等 `detector` 的 `/wait`。限额：每个实验最多 8 个启用；单次 `max_runtime_s` 默认 20、上限 120；连错 5 次或一小时命中超 30 次自动停用并写明原因；全体每小时唤醒超 60 次只记不叫醒。同名同代码重复创建回原来那个（`existing: true`）。探测器从不动手：要按命中动作，用 `/vista/act` 或组合技能。代码上限 64 KB，必须能编译。代码以 MAST 用户的身份在仪器电脑上运行，所以默认只接受本机回环发来的请求（局域网回 403 `loopback_only`，操作员设 `MAST_DETECTORS_ALLOW_REMOTE=1` 才放开）。

**请求体** (`DetectorCreate`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `name` | `string` | 是 |  | `minLength=1, maxLength=80` |
| `code` | `string` | 是 |  | `minLength=1, maxLength=65536` |
| `applies_to` | `array<"scan" \| "spectrum">` | 是 |  |  |
| `description` | `string` |  | `""` | `maxLength=500` |
| `max_runtime_s` | `number` |  | `20.0` | `min=1.0, max=120.0` |

### `GET /detectors`

当前实验的全部探测器：是否启用（停用的写明原因）、运行与命中计数、最近的命中。

### `GET /detectors/{detector_id}`

一个探测器：代码、状态、计数与最近 50 次命中。未知 id 回 404 `unknown_detector`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `detector_id` | path | `string` | 是 |  |  |

### `POST /detectors/{detector_id}/enable`

`{enabled: true|false}` 打开或关闭一个探测器。启用数已到上限时回 409 `too_many_enabled`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `detector_id` | path | `string` | 是 |  |  |

**请求体** (`DetectorEnable`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `enabled` | `boolean` | 是 |  |  |

### `DELETE /detectors/{detector_id}`

删除一个探测器（请求体发 `{}`）。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `detector_id` | path | `string` | 是 |  |  |

### `POST /detectors/{detector_id}/test`

立刻在一帧已归档的帧上（`frame`，如 `t0007.s0`）同步跑一次，回它返回的东西或抛出的错误（带 traceback）。不计入命中、不叫醒任何人。帧种类与 `applies_to` 不符回 422 `frame_not_applicable`；帧不存在回 404 `unknown_frame`。与新建一样默认只接受本机请求。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `detector_id` | path | `string` | 是 |  |  |

**请求体** (`DetectorTestBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `frame` | `string` | 是 |  | `minLength=1, maxLength=64` |

## 文献

### `POST /literature/search`

在 MAST 的文献知识库（操作员的论文库）里按语义检索，不碰仪器：每条命中有标题、年份、期刊、DOI、相关度与摘要片段。`material` 把检索聚焦到一种材料（如 `Cu(111)`）。与主 API 的 `/api/literature/search` 同一条实现；向量检索要用 DashScope 嵌入，不可用时退回关键词匹配并标 `degraded: true`。

**请求体** (`LiteratureSearchBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `query` | `string` | 是 |  | `minLength=1, maxLength=500` |
| `k` | `integer` |  | `8` | `min=1.0, max=50.0` |
| `material` | `string` |  | `""` | `maxLength=120` |
| `library_id` | `string \| null` |  |  | `maxLength=120` |

## 运行器（无人值守会话）

### `POST /runner/start`

启动一次无人值守运行：MAST 自己起 Claude Code（`claude -p`，stream-json），只给它 MAST 的 MCP 桥这一个工具服务器，`task` 是给它的任务。只走订阅：子进程里清掉 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`，init 报告的 `apiKeySource` 不是 none 就不启动。按图片数和上下文长度自动换会话：先要求 agent 写好 WORKING.md（这期间只有笔记工具可用），新会话从笔记、任务和最新观测接着做；每一次工具调用有没有回音都会核对。一次只许一个运行，另一个在跑时回 409 `run_active`；Claude Code、桥或桥的 Python 找不到时回 503 `runner_unavailable`（可执行文件只认环境变量覆盖，从不经 HTTP 指定）。回 202 与运行视图。Codex 版运行器在下一个小版本。

**请求体** (`RunnerStart`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `task` | `string` | 是 |  | `minLength=1, maxLength=20000` |
| `client` | `"claude"` |  | `"claude"` |  |
| `model` | `string \| null` |  |  | `maxLength=80` |
| `effort` | `"low" \| "medium" \| "high" \| "xhigh" \| "max" \| null` |  |  |  |
| `base_url` | `string \| null` |  |  | `maxLength=200` |
| `preflight` | `boolean \| null` |  |  |  |
| `max_segments` | `integer \| null` |  |  |  |
| `turn_timeout_s` | `number \| null` |  |  |  |
| `stall_turns` | `integer \| null` |  |  |  |
| `image_limit_per_session` | `integer \| null` |  |  |  |
| `image_reserve` | `integer \| null` |  |  |  |
| `max_budget_usd` | `number \| null` |  |  |  |
| `max_recoveries` | `integer \| null` |  |  |  |
| `compact_window_tokens` | `integer \| null` |  |  |  |
| `compact_pct` | `integer \| null` |  |  |  |

### `GET /runner`

最近的运行（本进程与之前进程留下的记录，新的在前）。

### `GET /runner/{run_id}`

一次运行：状态、结局、各会话段、用量、没有回音的工具调用。未知 id 回 404 `unknown_run`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `run_id` | path | `string` | 是 |  |  |

### `POST /runner/{run_id}/stop`

操作员叫停：这个会话的动作立刻被拒（`session_closed`），Claude Code 被打断，运行以 `stopped_by_operator` 结束。急停另走 `/estop`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `run_id` | path | `string` | 是 |  |  |

**请求体** (`RunnerStop`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `reason` | `string` |  | `""` | `maxLength=300` |

### `POST /runner/{run_id}/hook`

Claude Code 钩子（PreCompact / PostCompact / Stop）的中转，只接受本机回环、带这次运行的 `X-MAST-Runner-Secret`。回 `{output: <钩子决定>}`。给运行器自己用，agent 不调它。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `run_id` | path | `string` | 是 |  |  |

**请求体** (`RunnerHook`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `event` | `string \| null` |  |  | `maxLength=64` |
| `payload` | `object` |  |  |  |

## 光路图（只读 + 评论）

### `GET /optical-layouts`

用户在网页里画过的光路图（光学平台上器件的位置、方向、型号、是否电动、接到哪里）：`{layouts:[{id, title, revision, updated_at, updated_by, components, open_comments}]}`，最近保存的在前；读不了的文件多一个 `error`。没有图就是空列表。只读，不碰任何设备。

### `GET /optical-layouts/{layout_id}`

一张光路图：`layout`（几何、器件、注释、链接；单位 mm 与度，原点在平台左下角，x 向右、y 向上，角度从 +x 逆时针量）、`trace`（每个光源每个波长一棵树：折线、命中点、`offset_mm`（命中点偏离器件中心的有符号距离）、`incidence_deg`、`path_mm`（累计路径长））、`summary`（沿光束顺序的文字叙述和问题清单，**先读它**；语言取请求的 `X-MAST-Lang`）、`links_state`（器件链接的设备 / 手动光学状态槽位当下的状态；读已打开设备的状态，**从不为此打开设备**）、`comments`（未解决的评论）。未知 id 回 404 `unknown_layout`，文件读不了回 500 `layout_unreadable`（文件原样保留）。**这张图你改不了**：它记录台面上实际摆的是什么，只有操作员知道。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `layout_id` | path | `string` | 是 |  |  |

### `GET /optical-layouts/{layout_id}/render.png`

这张图的 PNG（`image/png`）：器件是简单图形，光束按波长着色（红外 / 紫外虚线，粗细随强度），命中偏离中心超过口径 20% 的地方画黄圈，打在背面或险些没中的地方画红叉。`width` 是像素宽（200–3000，缺省 1200）；`fit=content`（缺省）缩放到器件与光束所在处，`fit=table` 画整个平台（坐标轴始终是平台坐标）。不需要显示器。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `layout_id` | path | `string` | 是 |  |  |
| `width` | query | `integer` |  | `1200` | `min=200, max=3000` |
| `fit` | query | `"table" \| "content"` |  | `"content"` |  |

### `POST /optical-layouts/{layout_id}/comments`

在这张图上发一条评论（质疑、提醒、建议），可以挂在某个器件上（`component_id`，必须在图里，否则 422 `invalid_comment`）。作者记成你的 agent id（`ext:<X-MAST-Actor>`），不收请求体里的署名；请求体里多出任何字段都是 422。操作员在网页里看到并由操作员解决——**你不能解决评论，也不能改图**。每个 agent 在一张图上没解决的评论 ≤ 50（429 `too_many_open_comments`），一张图的评论总数 ≤ 1000（满了先淘汰最老的已解决评论；全是未解决的就回 429 `layout_comments_full`）。回 `{ok, comment}`。

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|---|
| `layout_id` | path | `string` | 是 |  |  |

**请求体** (`LayoutCommentBody`)

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `text` | `string` | 是 |  | `minLength=1, maxLength=2000` |
| `component_id` | `string \| null` |  |  | `maxLength=40` |

```json
{"text": "M2 is hit 6 mm off center; is the mount shimmed?", "component_id": "m2"}
```

## 作业视图

```json
{
  "job_id": "j_0123456789ab", "kind": "skill", "skill": "GetBias", "params": {},
  "params_used": {},
  "actor": "claude-code", "session": "", "request_id": "getbias-001", "note": "",
  "run_id": "ext-j_0123456789ab",
  "state": "succeeded", "terminal": true,
  "created_at": "2026-01-01T12:00:00", "started_at": "2026-01-01T12:00:00",
  "finished_at": "2026-01-01T12:00:01", "elapsed_s": 0.41,
  "cancel_requested": false, "cancel_reason": "",
  "result": {"success": true, "summary": "", "error": "", "data": {},
             "nanonis_calls": 1, "elapsed_s": 0.4},
  "refused_by": null, "busy_holder": null, "abort": null,
  "recorded": {"v1": true, "v2_action_id": "...", "experiment_id": "...", "sample_id": "..."}
}
```

| `state` | 含义 |
|---|---|
| `queued` / `running` | 还没结束。 |
| `succeeded` | 技能报告成功。 |
| `failed` | 技能失败或被拒，详情见 `result.error`。`refused_by` 只标识部分拒绝类别（如 `sample_gate`、`si_parse`、`needs_human_node`），也可能为 null。 |
| `refused_busy` | 仪器正被别人占用，**不排队**；`busy_holder` 说明是谁在开车。 |
| `cancelled` | 已请求取消，且作业未报告成功。仪器最终状态需要另行核实。 |
| `crashed` | 执行线程自己出了意外。 |
| `lost_on_restart` | 服务重启时它还没结束，不知道它在仪器上做到了哪一步。**绝不重放**；用同一个 `request_id` 重发拿到的就是这一条。先读简报再决定下一步。 |

`recorded` 是实验记录写入回执。`v1: false` 表示 v1 写入未成功，可能是缺作用域、存储未接线或写入错误；结合 `experiment_id`、`sample_id`、`v2_action_id` 和可能的 `error` 判断。空回执不代表已记录，独立作业日志也不等于实验动作记录。`abort` 在技能结束时中止事件已置位的情况下给出 `{set, reason}`。`result.data` 过大时会被截断。

## 简报的段

| 段 | 内容 |
|---|---|
| `status` | 与 `GET /status` 相同的内容，写成文字。 |
| `scope` | 当前实验、样品、目标；没有时说明后果。 |
| `resume` | 续工块：这个实验此前做到了哪里（与 MAST 自己的 agent 看到的相同）。 |
| `tip` | 当前针尖的登记信息。 |
| `instrument` | 仪器档案与已学到的标定。 |
| `live` | live 读数（缓存快照）。 |
| `prefs` | 操作员设置的默认参数偏好。 |
| `safety` | 此刻生效的安全包络，以及挂着的闩和解除办法。 |
| `recent_actions` | 本实验最近的动作，带署名（谁做的）。 |
| `recent_files` | 最近的数据文件。 |
| `alarms` | 环境监控的总体状态与最近的告警。 |
| `notes` | 本实验最近的笔记（任何作者）。 |
| `recording` | 你的署名，以及当前作用域是否配置了实验记录。 |
| `jobs` | 你最近的作业。 |
| `operator_requests` | 你发给操作员的请求与答复。 |
| `optical_layouts` | 用户画过的光路图：几张、各自器件数、未解决评论数、怎么读。**没有图时这一段为空**，不进顶层 `text`。两种站点都有。 |

## 技能卡

除参数外，技能卡给出：

| 字段 | 内容 |
|---|---|
| `parameters` | 每个参数的类型、单位、必填、默认值、上下限、取值集。 |
| `safety_level / capabilities` | 安全级与能力标签（例如是否属于针尖处理）。 |
| `preconditions` | 运行前必须成立的条件。 |
| `category / tags` | 类别与标签。 |
| `origin / origin_code / official` | 来源：`origin` 是给人读的标签（目前是中文），`origin_code` 是语言中立的代号（`builtin`、`composite`、`paper`、`user_composite`、`custom`、`agent_tool`、`overlay`、`other`）；`official` 只对前三种为真。 |
| `footprint / footprint_basis / footprint_reasons` | 对仪器做什么（`pure-analysis` / `hardware-read-only` / `hardware-write` / `unknown`）。与投稿校验器 `scripts/skill_check.py` 出自同一份静态分析：`footprint_basis` 为 `static` 是从源码读出来的，`declared` 是源码看不透、按类别保守取的，`footprint_reasons` 说明为什么看不透。 |
| `verbs / verbs_unknown` | 会发的 Nanonis 命令；分析不完整时 `verbs_unknown:true`，不把缺失报成空集。 |
| `sub_skills` | 组合技能的子步。 |
| `takes_instrument_token` | 是否占用仪器锁（占用时别人会被 `refused_busy`）。 |
| `requires_sample` | 是否必须先选样品。 |
| `si_params` | 哪些参数接受 `"5n"` 这类写法，以及是否必须带前缀。 |
| `tool_face` | 本机是否关闭了它，以及原因。 |
| `duration` | `{estimated_s, measured:{n, p50_s, p95_s, max_s}\|null, note}` —— `measured` 取自这台仪器上的成功记录。 |
