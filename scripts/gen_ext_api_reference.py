"""Generate ``docs/external/{zh,en}/07-api-reference.md`` from the external-agent API itself.

The parameter and body tables come from the API's own OpenAPI document, so they
cannot drift from the code. The prose for each endpoint lives in ``ENDPOINTS``
below, in both languages; an endpoint that exists in the API but has no prose
here makes generation fail (and the drift test with it).

Usage::

    python scripts/gen_ext_api_reference.py           # write both files
    python scripts/gen_ext_api_reference.py --check   # exit 1 if a file on disk differs

Only the columns that do not depend on language (name, location, type, required,
default, limits) are taken from OpenAPI; the field descriptions in the code are
not used, so the English page contains no Chinese and vice versa.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
OUT = {lang: REPO / "docs" / "external" / lang / "07-api-reference.md" for lang in ("zh", "en")}

_MASTV2 = str(REPO / "MASTv2")
if _MASTV2 not in sys.path:
    sys.path.insert(0, _MASTV2)


# ─────────────────────────────────────────────────────────────────────
# Prose. Code blocks are shared by both languages (the bilingual guard
# requires them to be byte-identical), so they carry no natural language.
# ─────────────────────────────────────────────────────────────────────

GROUPS = [
    ("overview", "总览、作用域与急停", "Overview, scope and emergency stop"),
    ("jobs", "作业", "Jobs"),
    ("skills", "技能", "Skills"),
    ("data", "数据", "Data"),
    ("collab", "笔记、发问与交接", "Notes, questions and handover"),
    ("vista", "VISTA：观测与动作（一动一观）", "VISTA: observations and actions (one act, one observation)"),
    ("vista_tools", "VISTA：回看、读数、量尺、工作笔记与历史",
     "VISTA: magnifier, readings, rulers, working notes and history"),
    ("wait", "等待与探测器", "Waits and detectors"),
    ("literature", "文献", "Literature"),
    ("runner", "运行器（无人值守会话）", "Runner (unattended sessions)"),
    ("optical_layout", "光路图（只读 + 评论）", "Optical layouts (read, and comment)"),
]

EX_SUBMIT = """\
```bash
curl -s -X POST http://127.0.0.1:7862/api/ext/v1/jobs \\
  -H "Content-Type: application/json" -H "X-MAST-Actor: claude-code" \\
  -d '{"skill": "GetBias", "params": {}, "request_id": "getbias-001"}'
curl -s "http://127.0.0.1:7862/api/ext/v1/jobs/j_0123456789ab?wait_s=30" \\
  -H "X-MAST-Actor: claude-code"
```"""

EX_FRAME = """\
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
```"""

EX_SCOPE = """\
```json
{"experiment": {"name": "Au(111) reconstruction", "goal": "map the herringbone"},
 "sample": {"name": "Au(111) #3", "sample_type": "metal"}}
```"""

EX_LAYOUT_COMMENT = """\
```json
{"text": "M2 is hit 6 mm off center; is the mount shimmed?", "component_id": "m2"}
```"""

EX_NOTE = """\
```json
{"title": "tip state", "content": "atomic contrast at -1.2 V / 50 pA",
 "kind": "insight", "tags": ["tip"], "scope": "experiment"}
```"""

# (method, path) -> group, zh, en[, example]
ENDPOINTS: dict[tuple[str, str], dict] = {
    ("GET", "/health"): dict(
        group="overview",
        zh="外部面自己的健康：各子系统有没有接上（`runtime`、`registry`、`pool`、`state`、`storage`、"
           "`cognition`、`jobs`）。`missing` 非空时，依赖那些子系统的端点会回 503 `not_wired`。"
           "不需要仪器在线。",
        en="Health of the external surface itself: which subsystems are wired (`runtime`, "
           "`registry`, `pool`, `state`, `storage`, `cognition`, `jobs`). When `missing` is not "
           "empty, the endpoints that need those subsystems answer 503 `not_wired`. Works with "
           "the instrument offline."),
    ("GET", "/guide"): dict(
        group="overview",
        zh="面向 agent 的铁律清单，`lang=zh|en`。返回 `{lang, rules:[{id, title, text}], docs_hint}`。"
           "规则的原因与细节见 [操作规则](03-operating-rules.md)。",
        en="The operating rules for agents, `lang=zh|en`. Returns `{lang, rules:[{id, title, "
           "text}], docs_hint}`. The reasons behind each rule are in "
           "[Operating rules](03-operating-rules.md)."),
    ("GET", "/status"): dict(
        group="overview",
        zh="快速状态，只读内存与缓存快照、**不碰硬件**：`mode`（`safe` / `semi` / `auto`；没人设置过时"
           "是 `unknown`，不会被说成某个具体值）、`abort`（`{set, emergency, why}`）、`lock`（仪器锁："
           "`{held:false}` 或 `{held, owner, skill, held_s}`）、`connection`（各 TCP 角色连没连上）、"
           "`live`（偏压、电流、设定点、Z、Z 控制、扫描中、是否退针、`stale`）、`scope`、`jobs`"
           "（`{running, max}`）、`degraded`（读不到的字段名）。",
        en="Quick status from in-memory state and cached snapshots — **no hardware I/O**: "
           "`mode` (`safe` / `semi` / `auto`; `unknown` when nobody has set it, never reported "
           "as a concrete value), `abort` (`{set, emergency, why}`), `lock` (the instrument lock: "
           "`{held:false}` or `{held, owner, skill, held_s}`), `connection` (which TCP roles are "
           "connected), `live` (bias, current, setpoint, Z, Z controller, scanning, withdrawn, "
           "`stale`), `scope`, `jobs` (`{running, max}`), `degraded` (fields that could not be "
           "read)."),
    ("GET", "/briefing"): dict(
        group="overview",
        zh="分段简报：MAST 自己的 agent 每一轮看到的上下文块，加上「在你之前谁做了什么」。"
           "`sections=a,b` 只取某几段（空 = 全部；未知段名 422，回执里有 `available`）。每段 "
           "`{ok, text?, data?, error?}`；**任何一段读不到只让那一段 `ok:false` 并进 `degraded`**，"
           "其余照给。顶层 `text` 是各段文字拼成的 markdown，截断到约 12000 字符。零硬件 I/O。"
           "各段内容见下文 [简报的段](#简报的段)。",
        en="Sectioned briefing: the context blocks MAST's own agents see every turn, plus "
           "\"who did what before you\". `sections=a,b` selects sections (empty = all; an "
           "unknown name is a 422 whose body lists `available`). Each section is `{ok, text?, "
           "data?, error?}`; **a section that cannot be read turns only itself `ok:false` and "
           "is listed in `degraded`** — the rest still arrive. Top-level `text` joins the "
           "sections into markdown, cut at about 12000 characters. No hardware I/O. See "
           "[Briefing sections](#briefing-sections) below."),
    ("GET", "/scope"): dict(
        group="overview",
        zh="当前实验、样品与实验记录服务是否可用：`{experiment:{id, name, goal}|null, "
           "sample:{id, name, sample_type}|null, recording:{v1, v2, note}}`。没有当前实验时，"
           "动作不能归入实验动作记录，但独立作业日志与全局笔记仍可存在。这里描述当前配置；"
           "实际写入结果见各作业的 `recorded`。",
        en="The current experiment, sample and experiment-recording availability: "
           "`{experiment:{id, name, goal}|null, sample:{id, name, sample_type}|null, "
           "recording:{v1, v2, note}}`. Without an active experiment, actions cannot be filed "
           "in its action record; the separate job journal and global notes can still exist. "
           "This describes current configuration; each job's `recorded` reports actual writes."),
    ("POST", "/scope"): dict(
        group="overview",
        zh="切换或新建实验 / 样品。给 `id` 就切换到已有的（切回一个实验时恢复它上次用过的样品；"
           "切到别的实验下的样品会把实验一起切过去）；给 `name` 就按名字复用，没有才新建（样品在当前"
           "实验内查找）。仪器正被占用时默认 409 `instrument_busy`（在跑的动作会记到新作用域下），"
           "`force:true` 才切并在 `warnings` 里写明。其他错误：404 `unknown_experiment` / "
           "`unknown_sample`、409 `no_experiment`（没有实验就建样品）、422 `empty_request`。"
           "回执 = `GET /scope` 的内容 + `changed` + `warnings`。",
        en="Switch to, or create, an experiment and/or sample. With `id` it switches to an "
           "existing one (switching back to an experiment restores the sample last used in it; "
           "switching to a sample of another experiment switches the experiment too). With "
           "`name` it reuses a same-named one and only creates when there is none (samples are "
           "looked up inside the current experiment). While the instrument is in use the "
           "default answer is 409 `instrument_busy` (the running action would be recorded "
           "under the new scope); `force:true` switches anyway and says so in `warnings`. Other "
           "errors: 404 `unknown_experiment` / `unknown_sample`, 409 `no_experiment` (a sample "
           "without an experiment), 422 `empty_request`. The reply is the `GET /scope` body "
           "plus `changed` and `warnings`.",
        example=EX_SCOPE),
    ("POST", "/estop"): dict(
        group="overview",
        zh="硬件急停：调用与操作员急停按钮相同的流程，先挂中止与急停闩，再尝试停止运动和退针。闩上的原因"
           "写成「外部 agent ext:<名字> 触发急停：<reason>」；然后请求取消仍在运行的外部作业。回执含 `why`、"
           "`cancelled_jobs` 与急停动作本身的结果（`errors`、`retracted` 等）。闩已置位不代表硬件动作成功，"
           "必须检查结果并核实仪器状态。**解闩是操作员的事**，外部面"
           "不提供解闩。",
        en="Hardware emergency stop: the same procedure as the operator's E-STOP button. It "
           "sets abort and emergency latches, then attempts to stop motion and withdraw the tip, with the latch "
           "reason recorded as the external agent that pressed it; then cancellation is requested for "
           "running external jobs. The reply carries `why`, `cancelled_jobs` and the result of the stop "
           "itself (`errors`, `retracted`, …). A set latch is not proof of hardware success; "
           "inspect the result and verify instrument state. **Releasing the latch is the operator's decision**; the "
           "external surface has no release."),
    ("POST", "/jobs"): dict(
        group="jobs",
        zh="提交一个技能作业，立即返回（新作业 202 + 作业视图）。门口按顺序检查：技能存在（否则 404 "
           "`unknown_skill`，带 `did_you_mean`）→ 技能及其声明式子步都没被本机关闭（否则 422 "
           "`skill_disabled`）→ `request_id` 幂等（同一调用方同一 id：内容相同回原作业 200 + "
           "`idempotent_replay:true`；内容不同 409 `request_id_conflict`）→ 并发上限（429 "
           "`too_many_jobs`；停止 / 退针类技能如 `StopScan` 不受上限约束）。带量纲的参数可写 `\"5n\"` 这类带 SI 前缀的字符串。**技能自身失败不是 "
           "HTTP 错误**：作业以终态结束。提交丢了回执时，用**同一个** `request_id` 重发。",
        en="Submit a skill as a job; returns at once (a new job is a 202 with the job view). "
           "Checks at the door, in order: the skill exists (else 404 `unknown_skill` with "
           "`did_you_mean`) → neither it nor its declared sub-steps are switched off on this "
           "instrument (else 422 `skill_disabled`) → `request_id` idempotency (same caller, "
           "same id: same body returns the original job, 200 with `idempotent_replay:true`; a "
           "different body is 409 `request_id_conflict`) → the concurrency cap (429 "
           "`too_many_jobs`; stop and retract skills such as `StopScan` are exempt). Dimensional parameters accept SI-prefixed strings such as "
           "`\"5n\"`. **A skill that fails is not an HTTP error**: the job ends in a terminal "
           "state. If the reply to a submission was lost, resend with the **same** "
           "`request_id`.",
        example=EX_SUBMIT),
    ("GET", "/jobs"): dict(
        group="jobs",
        zh="你最近提交的作业（新的在前）：`{count, jobs:[作业视图]}`。`all=true` 看所有调用方的，"
           "`state=` 按状态过滤。丢了上下文之后用它找回自己提交过什么。",
        en="Your recent jobs, newest first: `{count, jobs:[job view]}`. `all=true` lists every "
           "caller's jobs; `state=` filters by state. Use it to find what you submitted after "
           "losing context."),
    ("GET", "/jobs/{job_id}"): dict(
        group="jobs",
        zh="作业视图（见 [作业视图](#作业视图)）。`wait_s`（0–30）> 0 时等到作业结束或超时再回；"
           "服务端等待不占线程。超过保留数量的旧作业会被丢弃（404 `unknown_job`）。",
        en="The job view (see [Job view](#job-view)). With `wait_s` (0–30) > 0 the reply "
           "waits until the job ends or the time is up; the server-side wait holds no thread. "
           "Old jobs beyond the retention limit are dropped (404 `unknown_job`)."),
    ("POST", "/jobs/{job_id}/cancel"): dict(
        group="jobs",
        zh="协作式取消：技能在下一次检查中止处停下，原因（谁、为什么）会传到技能与记录里。卡在一条"
           "阻塞的 Nanonis 命令里时要等那条命令返回。请求物理停止时，运行不受仪器锁阻挡的停止类技能"
           "（例如 `StopScan`），危险时调用 `POST /estop`；检查响应并确认仪器状态。对已结束的作业是空操作。",
        en="Cooperative cancel: the skill stops at its next abort check, and the reason (who "
           "and why) reaches the skill and the record. A skill blocked inside a single Nanonis "
           "command stops only after that command returns. Request physical stopping with "
           "a stop skill exempt from the instrument lock (such as `StopScan`), or use "
           "`POST /estop` for danger; inspect the response and confirm instrument state. "
           "A no-op on a finished job."),
    ("GET", "/skills/search"): dict(
        group="skills",
        zh="按**动作**找技能：技能按「治什么问题」命名，按名字猜很难猜中。`q` 会同时匹配名字、"
           "Nanonis 命令名（如 `Bias_Set`、`Scan_Action`，从技能源码静态读出）、意图词、标签、"
           "参数名与描述；官方技能排在前面。每条结果带 `footprint`（`pure-analysis` / "
           "`hardware-read-only` / `hardware-write` / `unknown`）、`matched_on`（为什么命中）与 "
           "`origin_code`。",
        en="Find skills by **action**: skills are named after the problem they solve, which is "
           "hard to guess. `q` matches names, Nanonis command names (such as `Bias_Set` or "
           "`Scan_Action`, read statically from the skill source), intent words, tags, "
           "parameter names and descriptions; official skills rank first. Each result carries "
           "a `footprint` (`pure-analysis` / `hardware-read-only` / `hardware-write` / "
           "`unknown`), `matched_on` (why it matched) and `origin_code`."),
    ("GET", "/skills/{name}"): dict(
        group="skills",
        zh="技能卡：运行前该读的一切。字段见下文 [技能卡](#技能卡)。未知名字 404，带 `did_you_mean`。",
        en="The skill card: everything to read before running. Fields are listed under "
           "[Skill card](#skill-card) below. An unknown name is a 404 with `did_you_mean`."),
    ("POST", "/composites/draft"): dict(
        group="skills",
        zh="校验一份组合技能草稿（`CompositeSpec` JSON），**不落盘、不注册**，问题整批返回，外加与"
           "已有官方技能的重合提示。`spec: \"?\"` 返回格式说明。写法见 "
           "[编写技能](05-authoring-skills.md)。",
        en="Validate a composite-skill draft (`CompositeSpec` JSON) **without saving or "
           "registering it**; all problems come back at once, plus hints about overlap with "
           "existing official skills. `spec: \"?\"` returns the format. See "
           "[Authoring skills](05-authoring-skills.md)."),
    ("POST", "/composites"): dict(
        group="skills",
        zh="保存组合技能并热注册，署名 `ext:<名字>`。之后它就是一个普通技能：经 `POST /jobs` 执行，"
           "每个子步都过全部安全闸。改自己存过的同名技能要带 `base_version`（乐观锁；缺了回 "
           "`base_version_required`）；人做的技能不能被覆盖。业务上的拒绝是 200 + `ok:false` + "
           "`error`。",
        en="Save a composite skill and register it live, signed `ext:<name>`. From then on it "
           "is an ordinary skill: run it with `POST /jobs`; every sub-step passes every safety "
           "gate. Overwriting your own earlier version needs `base_version` (optimistic lock; "
           "without it the answer is `base_version_required`); skills made by people cannot "
           "be overwritten. A refusal is a 200 with `ok:false` and `error`."),
    ("POST", "/skills/proposals"): dict(
        group="skills",
        zh="提议一个新的原子技能（Python 源码）—— 组合表达不了时才用。写进本机的自定义技能目录"
           "**等人审**：不注册、不执行；要用，操作员审过代码、在启用清单里打开、重启。回执附一份合规"
           "报告 `compliance`（与投稿校验器同一套判据）。含禁用写法（如 `import os`）的代码直接 "
           "`ok:false`、`error:\"rejected\"`，不落盘。",
        en="Propose a new atomic skill (Python source) — only when a composite cannot express "
           "it. It is written to this installation's custom-skill folder **for review**: not "
           "registered, not run; to use it the operator reviews the code, enables it and "
           "restarts. The reply includes a `compliance` report (the same checks as the "
           "contribution checker). Code using a forbidden construct (such as `import os`) is "
           "`ok:false`, `error:\"rejected\"` and is not written."),
    ("GET", "/data/files"): dict(
        group="data",
        zh="最近的数据文件（mtime 新的在前；同一份测量的多个拷贝折叠成一条，`locations` 列出每一份）："
           "`{files:[{path, name, ext, mtime, size_bytes, kind, copies, locations}], count, "
           "has_more, degraded, counts_by_ext}`。只列允许根目录之内的文件；**不接受客户端指定目录**。"
           "已在当前实验 VISTA 档案里的文件另带 `frame`（帧号）。",
        en="Recent data files, newest first (several copies of one measurement fold into one "
           "entry; `locations` lists each copy): `{files:[{path, name, ext, mtime, size_bytes, "
           "kind, copies, locations}], count, has_more, degraded, counts_by_ext}`. Only files "
           "inside the allowed roots are listed; **a directory chosen by the client is never "
           "accepted**. A file the active experiment's VISTA archive holds also carries `frame` "
           "(its frame id)."),
    ("GET", "/data/search"): dict(
        group="data",
        zh="按元数据检索数据记录：数据图库的索引（尺寸、像素、偏压、设定点、通道、谱的扫描范围、操作员的评级 / "
           "标签 / 备注），加上索引里还没有的最新文件（现场读头，一次最多 200 个）。查询参数都可选、可组合："
           "`ext`（`sxm,dat,3ds`）、`experiment` / `sample`（id、≥6 位十六进制前缀或名字的一部分）、"
           "`since` / `until`（开始时间；`YYYY-MM-DD[ HH:MM[:SS]]`、ISO 或 epoch 秒，不带时区按仪器电脑"
           "本地时间；`until` 只给日期时含那一整天）、`size_min_nm` / `size_max_nm`、`bias_min_v` / "
           "`bias_max_v`、`setpoint_min_pa` / `setpoint_max_pa`、`text`、`tag`、`min_rating`（2 重点、"
           "1 可用、-1 排除）、`order`、`n`、`offset`、`live`。回答：`{items:[{path, name, ext, kind, "
           "time, saved, experiment, sample, frame, fetchable, source, …}], count, total, offset, "
           "has_more, query, index:{available, written_at, items, roots}, live:{examined, added, "
           "truncated, max}, frames:{workspace, known}, notes?, degraded}`；`experiment` / `sample` / "
           "`frame` 说不出时为 null；`fetchable: false` 表示路径在数据出口允许的根之外（约束不变）。"
           "读不懂的时间或类型 422 `bad_query`。",
        en="Search data records by metadata: the data gallery's index (size, pixels, bias, "
           "setpoint, channels, a spectrum's sweep range, the operator's rating / tags / note), "
           "plus the newest files the index does not have yet (headers read on the spot, at most "
           "200 per search). All query parameters are optional and combine: `ext` "
           "(`sxm,dat,3ds`), `experiment` / `sample` (an id, a prefix of at least 6 hex "
           "characters, or part of the name), `since` / `until` (start time; "
           "`YYYY-MM-DD[ HH:MM[:SS]]`, ISO or epoch seconds, in the instrument PC's local time "
           "without a time zone; a date alone in `until` includes that whole day), `size_min_nm` "
           "/ `size_max_nm`, `bias_min_v` / `bias_max_v`, `setpoint_min_pa` / `setpoint_max_pa`, "
           "`text`, `tag`, `min_rating` (2 key, 1 usable, -1 excluded), `order`, `n`, `offset`, "
           "`live`. Answer: `{items:[{path, name, ext, kind, time, saved, experiment, sample, "
           "frame, fetchable, source, …}], count, total, offset, has_more, query, "
           "index:{available, written_at, items, roots}, live:{examined, added, truncated, max}, "
           "frames:{workspace, known}, notes?, degraded}`; `experiment` / `sample` / `frame` are "
           "null when MAST cannot tell; `fetchable: false` means the path lies outside the roots "
           "the data egress allows (unchanged). An unreadable time or kind is 422 `bad_query`."),
    ("GET", "/data/file"): dict(
        group="data",
        zh="原始字节（`application/octet-stream`）。路径先解析（`..`、符号链接）再判归属：只许数据"
           "搜索目录与实验根目录之内、扩展名在白名单里（`.sxm .dat .3ds .txt .csv .png .npy .npz`）"
           "的文件；网络路径一律拒绝。错误：403 `path_not_allowed` / `extension_not_allowed`、404 "
           "`not_found`、422 `bad_path`。",
        en="Raw bytes (`application/octet-stream`). The path is resolved first (`..`, symbolic "
           "links) and then checked: only files inside the data search folders and the "
           "experiment root, with a whitelisted extension (`.sxm .dat .3ds .txt .csv .png .npy "
           ".npz`); network paths are always refused. Errors: 403 `path_not_allowed` / "
           "`extension_not_allowed`, 404 `not_found`, 422 `bad_path`."),
    ("GET", "/data/frame"): dict(
        group="data",
        zh="一个 `.sxm` 通道的帧，**服务端统一朝向**后打包成 `.npz`：`forward` / `backward` 的第 0 行"
           "是图的顶边（`scan_dir=up` 已翻转），`backward` 已去镜像（与 `forward` 同一几何朝向）；只有"
           "反向数据的通道，那一块作为 `forward` 给出，`meta_json` 的 `served_block` 如实写明。"
           "`meta_json` 另含宽高（nm）、nm/px、偏压、设定点、扫描方向、单位、可用通道；同一份元数据"
           "也在响应头 `X-MAST-Frame-Meta`（ASCII JSON）。未知通道 404 `unknown_channel`（带 "
           "`channels`）；非 `.sxm` 422 `not_sxm`。",
        en="One channel of an `.sxm` file, **oriented on the server**, packed as `.npz`: row 0 "
           "of `forward` / `backward` is the top edge of the image (`scan_dir=up` already "
           "flipped), and `backward` is un-mirrored (same geometry as `forward`); for a channel "
           "recorded only backward, that block is served as `forward` and `served_block` in "
           "`meta_json` says so. `meta_json` also carries width and height (nm), nm/px, bias, "
           "setpoint, scan direction, unit and the available channels; the same metadata is in "
           "the `X-MAST-Frame-Meta` response header (ASCII JSON). An unknown channel is 404 "
           "`unknown_channel` (with `channels`); a non-`.sxm` file is 422 `not_sxm`.",
        example=EX_FRAME),
    ("POST", "/notes"): dict(
        group="collab",
        zh="写一条笔记进 MAST 的记忆库，**MAST 自己的 agent 会自动召回它**。`scope=experiment` 存进当前"
           "实验（没有实验时退到 global 并在 `warnings` 里写明），`global` 跨实验。署名 `ext:<名字>`；"
           "同标题同内容重发落在同一条上（路径由内容哈希决定），不会重复。",
        en="Write a note into MAST's memory; **MAST's own agents recall it automatically**. "
           "`scope=experiment` stores it with the current experiment (falling back to global, "
           "with a warning, when there is none); `global` spans experiments. Signed "
           "`ext:<name>`; resending the same title and content lands on the same entry (its "
           "path is a content hash), so there are no duplicates.",
        example=EX_NOTE),
    ("GET", "/notes"): dict(
        group="collab",
        zh="检索笔记：`q` 非空时召回（当前实验 ∪ global；语义索引不可用时退回子串匹配），为空时列最近的。"
           "MAST 自己的 agent 写下的记忆也在这里（看 `author`）。每条带 `match`（semantic / substring / "
           "recent）与 `score`（语义召回时为余弦相似度 −1..1，其余 null）：最近的几条总会回来，分数低的就是"
           "无关的。",
        en="Search notes: with `q`, recall across the current experiment and global (falling "
           "back to substring search when the semantic index is unavailable); without `q`, the "
           "most recent. Memories written by MAST's own agents are here too (see `author`). Each "
           "note carries `match` (semantic / substring / recent) and `score` (cosine similarity "
           "for semantic hits, −1..1; null otherwise): the nearest notes always come back, so a "
           "low score marks an unrelated one."),
    ("POST", "/requests"): dict(
        group="collab",
        zh="向操作员发一个问题或请求，界面上会亮起来。回执 `{ok, request:{id, status, ...}}`。"
           "**不要在这里等** —— 答复是异步的，之后用 `GET /requests/{request_id}` 或简报的 "
           "`operator_requests` 段看，或在 `POST /wait` 里等 `operator_reply`。同一个未答的问题重发不会重复。"
           "`kind` 是 `question` / `action` / `info`（或别的自由词）时是自由文本请求，操作员回一段文字。"
           "`kind` 是 `confirm` / `choice` / `number` / `text` / `attest` 时是**结构化提示**：`options`"
           "（`[{id, label}]`，choice 至少两个）、`number_min` / `number_max` / `unit`、`attest_slots`"
           "（attest：操作员的答复要写进的手动光学状态槽位，必填）、`severity`（`info` / `action` / `safety`）、"
           "`timeout_s`（至多 7 天，不给就是 24 小时，之后按超时关闭）；网页提示卡、启动器弹窗和声音同时提醒，"
           "**只有操作员能答，你不能**。结构化字段配自由文本 `kind` 回 422 `structured_fields_need_kind`，"
           "提示本身不合法回 422 `invalid_prompt`。**结构化提示只有光学站有**：STM 站上每个请求都是文字请求，"
           "`kind` 写什么都原样存，结构化字段被忽略、不报错。attest 里操作员看到的内容来自槽位定义，不来自你：是非槽位"
           "（如 `laser_area_safe`）不能带 `options`，choice 槽位只能从它自己的选项里挑（显示的是槽位定义的名字），"
           "number 槽位的单位和范围取槽位定义，一个提示里的槽位必须同一类型。你自己同时有 25 个、所有外部 agent "
           "合计有 75 个没答的结构化提示时再发新的回 429 `too_many_open_prompts`（带 `scope`；换名字也一样，"
           "操作员那里总共最多 100 个）。",
        en="Ask the operator a question or make a request; it lights up in the user interface. "
           "The reply is `{ok, request:{id, status, ...}}`. **Do not wait here** — the answer is "
           "asynchronous; read it later with `GET /requests/{request_id}` or the "
           "`operator_requests` briefing section, or wait for `operator_reply` in `POST /wait`. "
           "Resending an unanswered question does not duplicate it. With `kind` `question` / "
           "`action` / `info` (or any other word) it is a free-text request and the operator "
           "replies with a note. With `kind` `confirm` / `choice` / `number` / `text` / `attest` "
           "it is a **structured prompt**: `options` (`[{id, label}]`, a choice needs two or "
           "more), `number_min` / `number_max` / `unit`, `attest_slots` (attest: the manual "
           "optical-state slots the operator's answer sets; required), `severity` (`info` / "
           "`action` / `safety`), `timeout_s` (at most 7 days; omitted = 24 hours, then it "
           "closes as a timeout). A card in the web UI, a launcher popup and a sound alert the "
           "operator at once, and **only the operator can answer it, you cannot**. Structured "
           "fields with a free-text `kind` are a 422 `structured_fields_need_kind`; a prompt that "
           "is not valid is a 422 `invalid_prompt`. **Structured prompts exist on the optical "
           "station only**: on an STM station every request is a text request - the `kind` is "
           "stored as written and the structured fields are ignored without an error. What the "
           "operator sees to sign in an attest "
           "comes from the slot definitions, not from you: a yes/no slot (such as "
           "`laser_area_safe`) takes no `options`, a choice slot only options from its own list "
           "(shown under the slot's own names), a number slot has its own unit and range, and one "
           "prompt names slots of one kind. A new prompt while you have 25 unanswered ones, or "
           "while external agents together have 75 (the operator's list holds 100 in all), is a "
           "429 `too_many_open_prompts` (with a `scope`; changing your name does not help)."),
    ("GET", "/requests"): dict(
        group="collab",
        zh="你发过的请求与答复（未答的在前）：`{count, requests}`，自由文本请求和结构化提示都在。`status` 过滤："
           "`pending`（未答，同义 `open`）、`done`、`dismissed`；未知值回 422 `unknown_status`。",
        en="Your requests and their answers, unanswered first: `{count, requests}`, free-text "
           "requests and structured prompts alike. Filter with "
           "`status`: `pending` (unanswered; `open` means the same), `done`, `dismissed`; an "
           "unknown value is a 422 `unknown_status`."),
    ("GET", "/requests/{request_id}"): dict(
        group="collab",
        zh="单条请求：`note` 是文字答复，`path` 是操作员给的文件或目录路径（两者可能都有）。结构化提示"
           "（id 形如 `op-0001-…`）另带 `structured: true`、精确状态 `prompt_status`（`open` / `answered` / "
           "`cancelled` / `timeout` / `dismissed`；`status` 仍是 `pending` / `done` / `dismissed`）和 "
           "`answer`：`{status, choice, value, note, answered_by, source, answered_at, ok}`，答复前是 null。",
        en="One request: `note` is the written answer, `path` a file or folder the operator "
           "pointed to (there may be both). A structured prompt (its id looks like `op-0001-…`) "
           "also carries `structured: true`, the exact state `prompt_status` (`open` / "
           "`answered` / `cancelled` / `timeout` / `dismissed`; `status` stays `pending` / `done` "
           "/ `dismissed`) and `answer`: `{status, choice, value, note, answered_by, source, "
           "answered_at, ok}`, null until it is answered."),
    ("POST", "/handover"): dict(
        group="collab",
        zh="生成交接报告存进 MAST 的文档库（操作员在「报告」页能看到），署名 `ext:<名字>`：你写的 "
           "`summary` 与 `next_steps`，你这次提交的作业及结局，实验记录里署你名字的动作，你写的笔记。"
           "`since`（ISO 时间）只汇总此后的内容。回执 `{ok, doc_id, version, path, title, jobs, "
           "actions, notes}`。会话结束前写一份。",
        en="Write a handover report into MAST's document library (the operator sees it on the "
           "reports page), signed `ext:<name>`: your `summary` and `next_steps`, the jobs you "
           "submitted and how they ended, the actions recorded under your name, and your notes. "
           "`since` (ISO time) limits it to what came after. The reply is `{ok, doc_id, "
           "version, path, title, jobs, actions, notes}`. Write one before you leave."),
    ("POST", "/vista/hello"): dict(
        group="vista",
        zh="登记这个客户端会话（`X-MAST-Session`）并返回概况：`{session, actor, workspace, turn, "
           "strict, gate, frames, spectra, notes}`。任何 `/vista/*` 调用都会顺带登记；登记之后，这个会话"
           "里**驱动仪器的技能不能再走 `POST /jobs`**（409 `use_act`），只能走 `POST /vista/act` 并带上"
           "观测令牌。读、分析、停止 / 退针不受影响。`client_meta` 是桥转来的客户端信息（Codex 的 "
           "`itemId`、模型名等），只用于判据与记录。",
        en="Register this client session (`X-MAST-Session`) and get an overview: `{session, actor, "
           "workspace, turn, strict, gate, frames, spectra, notes}`. Any `/vista/*` call registers "
           "the session too; from then on, **skills that drive the instrument cannot be submitted "
           "through `POST /jobs` from this session** (409 `use_act`) — use `POST /vista/act` with "
           "the observation token. Reads, analysis and stop / withdraw are not affected. "
           "`client_meta` is what the bridge forwards about the client (Codex `itemId`, model "
           "name ...), used only for the gate and the records."),
    ("POST", "/vista/observe"): dict(
        group="vista",
        zh="最新的观测：工作区里最新一帧（扫描或谱）的图、它的摘要、仪器读数（偏压、设定点、电流、Z、针尖 "
           "XY、漂移补偿）、上一个动作的记录，以及**一个新令牌**。开始工作时、令牌丢了时、外来新帧之后都先"
           "调它。回答 `{observation:{obs_id, turn, frames, text, images, next_token, token_on_image}}`；"
           "`text` 是给模型读的 JSON（里面也有 `next_token`），`images` 至多一张。严格模式下令牌只印在图下方"
           "的条带里，`next_token` 为 null。",
        en="The newest observation: the newest frame (scan or spectrum) of the workspace as an "
           "image, its summary, the instrument readbacks (bias, setpoint, current, Z, tip XY, drift "
           "compensation), the last action's record, and **a fresh token**. Call it at the start, "
           "when the token is lost, and after a frame from outside. The answer is "
           "`{observation:{obs_id, turn, frames, text, images, next_token, token_on_image}}`; "
           "`text` is the JSON the model reads (it carries `next_token` too) and `images` holds at "
           "most one image. In strict mode the token is printed only in a band under the image "
           "and `next_token` is null."),
    ("POST", "/vista/act"): dict(
        group="vista",
        zh="执行**一步**动作：`action` + `params`（VISTA 的动作表：`scan`、`set_bias`、`set_current`、"
           "`feedback`、`move_tip`、`pulse`、`poke`、`sts`、`df_curve`、`pll`、`move_atom`、"
           "`drift_compensation`、`wait`、`withdraw`、`approach`；单位 nm、V、pA、s；位置是 "
           "`{x_nm, y_nm}` 或某个已归档帧的显示像素 `{frame, x, y}`），或者一个原始技能 `skill` + "
           "`skill_params`（SI 单位）。驱动仪器的都要带 `after` = 最新观测的令牌；不驱动仪器的原始技能当普通"
           "作业跑（`kind: \"job\"`，令牌不变）。动作是一个 `kind: \"act\"` 的作业：幂等（`request_id`）、"
           "可取消、进 journal、撞锁拒绝。最多等 `wait_s`（≤ 50 s）：做完回 `{kind:\"act\", state:\"done\", "
           "job_id, turn, ok, job_state, observation}`，没做完回 `state:\"running\"`，用 `POST /wait`（条件 "
           "`{type:\"job\", job_id}`）去等。一动一观的拒绝都是 409：`session_closed`、`act_in_flight`（附 "
           "`job_id`）、`same_code_cell`（同一段 Codex 代码里已经执行过一个动作）、`checkpoint_requested`、"
           "`observe_first`（令牌缺失、用过或过期；**附带最新观测与新令牌**）。被拒的动作什么也没执行。",
        en="Execute **one** action: `action` + `params` (VISTA's action table: `scan`, "
           "`set_bias`, `set_current`, `feedback`, `move_tip`, `pulse`, `poke`, `sts`, `df_curve`, "
           "`pll`, `move_atom`, `drift_compensation`, `wait`, `withdraw`, `approach`; units nm, V, "
           "pA, s; a position is `{x_nm, y_nm}` or display pixels of an archived frame `{frame, x, "
           "y}`), or a raw skill `skill` + `skill_params` (SI units). Anything that drives the "
           "instrument needs `after` = the newest observation's token; a raw skill that does not "
           "drive the instrument runs as a plain job (`kind: \"job\"`, token untouched). An action "
           "is a job of `kind: \"act\"`: idempotent (`request_id`), cancellable, journaled, refused "
           "when the instrument is busy. It waits up to `wait_s` (≤ 50 s): when done, the answer "
           "is `{kind:\"act\", state:\"done\", job_id, turn, ok, job_state, observation}`; when "
           "not, `state:\"running\"` — wait for it with `POST /wait` (condition `{type:\"job\", "
           "job_id}`). Gate refusals are all 409: `session_closed`, `act_in_flight` (with "
           "`job_id`), `same_code_cell` (an action from the same Codex code cell already ran), "
           "`checkpoint_requested`, `observe_first` (token missing, used or superseded; **carries "
           "the newest observation and a fresh token**). A refused action executed nothing."),
    ("GET", "/vista/acts/{job_id}"): dict(
        group="vista",
        zh="一个动作作业的当前状态（`wait_s` ≤ 50 可长轮询），形状与 `POST /vista/act` 的回答相同；做完时"
           "带着它的观测。这个观测的令牌如果已被更新的观测取代，`next_token` 为 null 并附 `token_note`。",
        en="The current state of an action job (long poll with `wait_s` ≤ 50), in the same shape "
           "as the `POST /vista/act` answer, with its observation once done. If a newer "
           "observation superseded its token, `next_token` is null and `token_note` says so."),
    ("GET", "/vista/status"): dict(
        group="vista",
        zh="工作区、回合号、帧与谱的数量、严格模式、闸门状态（令牌是否有效、绑在哪个会话、哪个动作在跑、"
           "失效次数 —— **从不包含令牌本身**）、已登记的客户端、监视线程。零硬件 I/O。",
        en="Workspace, turn, number of frames and spectra, strict mode, the gate (whether a token "
           "is live, which session it is bound to, which action is running, invalidations — "
           "**never the token itself**), registered clients, the watcher. No hardware I/O."),
    ("POST", "/vista/inspect"): dict(
        group="vista_tools",
        zh="放大镜（VISTA `inspect`）：一次最多 16 个视图，每个视图取档案里任意一帧，可裁剪区域、换通道 / "
           "方向、去背景、调对比度，按整数倍放大。回答是 VISTA 的工具回复 `{text, images, is_error}`，每个"
           "视图一张图。只读，不碰仪器。",
        en="The magnifier (VISTA `inspect`): up to 16 views per call; each takes any archived frame "
           "and can crop a region, switch channel / direction, remove the background and set the "
           "contrast, enlarged by whole numbers. The answer is VISTA's tool reply `{text, images, "
           "is_error}`, one image per view. Read-only; never touches the instrument."),
    ("POST", "/vista/read_values"): dict(
        group="vista_tools",
        zh="精确读数（VISTA `read_values`）：在档案帧上按点、线、区域取物理量数值（带单位后缀），最多 64 "
           "个视图。精确数字一律从这里取，不要从图上估。",
        en="Exact readings (VISTA `read_values`): physical values at points, along lines or over "
           "regions of archived frames (unit in the key suffix), up to 64 views. Take exact "
           "numbers from here, never by reading pixels."),
    ("POST", "/vista/measure/{tool}"): dict(
        group="vista_tools",
        zh="量尺：`line_profile`（两点之间的剖面、距离、台阶高度）、`fft_peaks`（周期与晶格）、"
           "`detect_blobs`（计数与位置）、`diff`（两帧之差与漂移）、`stats`（统计）。参数与 VISTA 的同名工具"
           "相同；结果图作为派生帧（`m`）归档，不算仪器帧，不影响令牌。工具只测量，不下结论。",
        en="Rulers: `line_profile` (profile between two points, distances, step heights), "
           "`fft_peaks` (periods and lattices), `detect_blobs` (counts and positions), `diff` "
           "(difference of two frames, drift), `stats` (statistics). Arguments as in VISTA's "
           "tools of the same name; result images are archived as derived frames (`m`), which are "
           "not instrument frames and do not affect the token. The tools measure; they never "
           "interpret."),
    ("GET", "/vista/notes/{which}"): dict(
        group="vista_tools",
        zh="读 `guide`（GUIDE.md：所有实验共用的长期经验）或 `working`（WORKING.md：这个实验的工作记忆）。",
        en="Read `guide` (GUIDE.md: durable lessons shared by all experiments) or `working` "
           "(WORKING.md: this experiment's working memory)."),
    ("PUT", "/vista/notes/{which}"): dict(
        group="vista_tools",
        zh="整份替换 GUIDE.md（≤ 64 KiB）或 WORKING.md（≤ 16 KiB）。运行器要求检查点时，写好 WORKING.md "
           "就把检查点标为就绪。",
        en="Replace GUIDE.md (≤ 64 KiB) or WORKING.md (≤ 16 KiB) as a whole. When the runner has "
           "asked for a checkpoint, writing WORKING.md marks it ready."),
    ("GET", "/vista/history"): dict(
        group="vista_tools",
        zh="这个实验的客观记录：`view=events` 按回合范围给出动作记录（解析后的参数、控制器结果、之后的仪器"
           "状态、新帧编号）、外来帧、探测器命中与检查点；`view=attempts` 是汇总（各动作次数与失败数、帧与谱"
           "清单）。",
        en="This experiment's objective record: `view=events` gives action records by turn range "
           "(resolved parameters, controller result, instrument state afterwards, new frame ids), "
           "frames from outside, detector hits and checkpoints; `view=attempts` summarises "
           "(actions and failures per kind, frames and spectra)."),
    ("POST", "/wait"): dict(
        group="wait",
        zh="在服务端等到某件事发生，最多 `timeout_s`（默认 600，上限 840；客户端把 MCP 工具超时设到 "
           "≥ 900 s，Codex 是 `tool_timeout_sec = 900`）。`conditions` 里任何一条满足就返回："
           "`job`（`job_id` 的作业结束，动作或普通作业都行）、`frame`（有新帧入档）、`alert`（异常、"
           "电流监控或 conduct 告警到 `min_level`：`info` / `warning` / `critical`，默认 `warning`）、"
           "`operator_reply`（操作员答复；可指定 `request_id`）、`detector`（探测器命中；可指定 "
           "`detector_id`）、`timer`（`seconds` 秒后）。回答 `{fired:[{index, type, event, seq, age_s, "
           "backlog}], timed_out, waited_s, cursor, observation?}`：`backlog: true` 表示那件事发生在这次"
           "等待开始之前（积压在游标之后），`age_s` 是距现在的秒数 —— 立刻返回的告警可能是几分钟前的事；"
           "电流监控告警的 `event` 另带 `segment_start` / `segment_end`（被判的约 1 s 电流，UTC）、"
           "`measured`（σ、突跳率、最大台阶与均值，pA）与 `context`（是否在扫描、距上一段扫描几秒、"
           "持有仪器的技能与最近一次见到的技能、偏压、设定点）。等到一个做完的动作或一帧新图时，`observation` "
           "带着那次观测（图 + 新令牌）。把 `cursor` 作为下一次的 `since` 传回，两次等待之间的事件一个都"
           "不会丢；游标太旧（被挤出缓冲）或来自上一次进程时，立即回一条 `type: \"gap\"`。客户端断开就"
           "提前收场。条件不合法回 422 `invalid_condition` / `invalid_cursor`。",
        en="Wait on the server until something happens, at most `timeout_s` (default 600, max "
           "840; set the client's MCP tool timeout to ≥ 900 s — Codex: `tool_timeout_sec = 900`). "
           "Any one of `conditions` ends the wait: `job` (the job `job_id` ends, an action or a "
           "plain job), `frame` (a new frame is archived), `alert` (an anomaly, current-monitor "
           "or conduct alert at `min_level` or above: `info` / `warning` / `critical`, default "
           "`warning`), `operator_reply` (the operator answers; optionally one `request_id`), "
           "`detector` (a detector hit; optionally one `detector_id`), `timer` (after `seconds`). "
           "The answer is `{fired:[{index, type, event, seq, age_s, backlog}], timed_out, waited_s, "
           "cursor, observation?}`; `backlog: true` means it happened before this wait started "
           "(queued after your cursor) and `age_s` is how many seconds ago — an alert that returns "
           "at once may be minutes old; a current-monitor alert's `event` also carries "
           "`segment_start` / `segment_end` (the ~1 s of current judged, UTC), `measured` (sigma, "
           "jump rate, largest step and mean in pA) and `context` (scanning or not, seconds since "
           "the last scanning segment, the skill holding the instrument and the last one seen, bias, "
           "setpoint). When a finished action or a new frame fired, `observation` carries "
           "that observation (image + fresh token). Pass `cursor` back as the next `since` and no "
           "event between two waits is lost; a cursor that is too old (pushed out of the buffer) "
           "or from an earlier process returns at once with a `type: \"gap\"` entry. A client that "
           "disconnects ends the wait early. Invalid input is a 422 `invalid_condition` / "
           "`invalid_cursor`."),
    ("POST", "/detectors"): dict(
        group="wait",
        zh="给当前实验建一个分析探测器：agent 写的 Python，定义 `def detect(frame)`，MAST 在每一帧新入档的"
           "扫描或谱（`applies_to`）上用独立解释器（pyexec：碰不到仪器、改不了原始数据、超时就杀、内存有"
           "上限）跑一遍。`frame` 有 `kind`、`fid`、`channels`（通道 → 正向二维数组，SI 单位）、"
           "`backward`、`meta`，谱另有 `columns` 与 `units`。返回假值（没命中）、`True`，或 `{hit, "
           "message, value}`。命中记进工作区事件，并叫醒等 `detector` 的 `/wait`。限额：每个实验最多 8 个"
           "启用；单次 `max_runtime_s` 默认 20、上限 120；连错 5 次或一小时命中超 30 次自动停用并写明原因；"
           "全体每小时唤醒超 60 次只记不叫醒。同名同代码重复创建回原来那个（`existing: true`）。探测器从不"
           "动手：要按命中动作，用 `/vista/act` 或组合技能。代码上限 64 KB，必须能编译。代码以 MAST 用户的身份在"
           "仪器电脑上运行，所以默认只接受本机回环发来的请求（局域网回 403 `loopback_only`，操作员设 "
           "`MAST_DETECTORS_ALLOW_REMOTE=1` 才放开）。",
        en="Create an analysis detector for the current experiment: agent-written Python that "
           "defines `def detect(frame)`, run by MAST on every newly archived scan or spectrum "
           "(`applies_to`) in a separate interpreter (pyexec: no instrument access, no write "
           "access to raw data, killed on timeout, memory-capped). `frame` has `kind`, `fid`, "
           "`channels` (channel → forward 2-D array, SI units), `backward`, `meta`; spectra also "
           "`columns` and `units`. Return a falsy value (no hit), `True`, or `{hit, message, "
           "value}`. A hit is logged in the workspace events and wakes a `/wait` on `detector`. "
           "Limits: at most 8 enabled per experiment; `max_runtime_s` 20 by default, at most 120; "
           "5 errors in a row or more than 30 hits an hour disable it with a reason; beyond 60 "
           "wakes an hour across detectors, hits are recorded without waking anyone. Creating the "
           "same name with the same code again returns the existing one (`existing: true`). "
           "Detectors never act: to act on a hit, use `/vista/act` or a composite skill. Code is "
           "at most 64 KB and must compile. The code runs as the MAST user on the instrument PC, "
           "so by default only loopback requests are accepted (over the LAN: 403 `loopback_only`, "
           "unless the operator sets `MAST_DETECTORS_ALLOW_REMOTE=1`)."),
    ("GET", "/detectors"): dict(
        group="wait",
        zh="当前实验的全部探测器：是否启用（停用的写明原因）、运行与命中计数、最近的命中。",
        en="All detectors of the current experiment: enabled or not (with the reason when "
           "disabled), run and hit counts, recent hits."),
    ("GET", "/detectors/{detector_id}"): dict(
        group="wait",
        zh="一个探测器：代码、状态、计数与最近 50 次命中。未知 id 回 404 `unknown_detector`。",
        en="One detector: its code, state, counters and last 50 hits. An unknown id is a 404 "
           "`unknown_detector`."),
    ("POST", "/detectors/{detector_id}/enable"): dict(
        group="wait",
        zh="`{enabled: true|false}` 打开或关闭一个探测器。启用数已到上限时回 409 `too_many_enabled`。",
        en="`{enabled: true|false}` switches a detector on or off. At the limit of enabled "
           "detectors the answer is a 409 `too_many_enabled`."),
    ("DELETE", "/detectors/{detector_id}"): dict(
        group="wait",
        zh="删除一个探测器（请求体发 `{}`）。",
        en="Delete a detector (send `{}` as the body)."),
    ("POST", "/detectors/{detector_id}/test"): dict(
        group="wait",
        zh="立刻在一帧已归档的帧上（`frame`，如 `t0007.s0`）同步跑一次，回它返回的东西或抛出的错误"
           "（带 traceback）。不计入命中、不叫醒任何人。帧种类与 `applies_to` 不符回 422 "
           "`frame_not_applicable`；帧不存在回 404 `unknown_frame`。与新建一样默认只接受本机请求。",
        en="Run it once, now, synchronously, on an archived frame (`frame`, e.g. `t0007.s0`) and "
           "return what it returned or the error it raised (with the traceback). Not counted as "
           "a hit, wakes nobody. A frame kind outside `applies_to` is a 422 "
           "`frame_not_applicable`; an unknown frame a 404 `unknown_frame`. Like creation, "
           "loopback only by default."),
    ("POST", "/literature/search"): dict(
        group="literature",
        zh="在 MAST 的文献知识库（操作员的论文库）里按语义检索，不碰仪器：每条命中有标题、年份、期刊、"
           "DOI、相关度与摘要片段。`material` 把检索聚焦到一种材料（如 `Cu(111)`）。与主 API 的 "
           "`/api/literature/search` 同一条实现；向量检索要用 DashScope 嵌入，不可用时退回关键词匹配并标 "
           "`degraded: true`。",
        en="Search MAST's literature knowledge base (the operator's library of papers) by meaning, "
           "without touching the instrument: each hit gives title, year, journal, DOI, a "
           "relevance score and an abstract excerpt. `material` focuses the search on one "
           "material (e.g. `Cu(111)`). The same implementation as the main API's "
           "`/api/literature/search`; vector search needs DashScope embeddings and falls back to "
           "keyword matching, flagged `degraded: true`, when they are unavailable."),
    ("POST", "/runner/start"): dict(
        group="runner",
        zh="启动一次无人值守运行：MAST 自己起 Claude Code（`claude -p`，stream-json），只给它 MAST 的 "
           "MCP 桥这一个工具服务器，`task` 是给它的任务。只走订阅：子进程里清掉 `ANTHROPIC_API_KEY` / "
           "`ANTHROPIC_AUTH_TOKEN`，init 报告的 `apiKeySource` 不是 none 就不启动。按图片数和上下文长度"
           "自动换会话：先要求 agent 写好 WORKING.md（这期间只有笔记工具可用），新会话从笔记、任务和最新"
           "观测接着做；每一次工具调用有没有回音都会核对。一次只许一个运行，另一个在跑时回 409 "
           "`run_active`；Claude Code、桥或桥的 Python 找不到时回 503 `runner_unavailable`（可执行文件"
           "只认环境变量覆盖，从不经 HTTP 指定）。回 202 与运行视图。Codex 版运行器在下一个小版本。",
        en="Start an unattended run: MAST launches Claude Code itself (`claude -p`, stream-json) with "
           "MAST's MCP bridge as its only tool server, and `task` as its assignment. Subscription "
           "only: `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are removed from the child, and a "
           "session whose init reports an `apiKeySource` other than none is not started. Sessions "
           "rotate on an image budget and on context length: the agent first writes WORKING.md "
           "(only the notes tools work meanwhile) and the next session continues from the notes, "
           "the task and the newest observation; every tool call is checked for its result. One "
           "run at a time: another active run is a 409 `run_active`; a missing Claude Code, bridge "
           "or bridge Python is a 503 `runner_unavailable` (executables come only from environment "
           "overrides, never over HTTP). Answers 202 with the run view. The Codex runner comes in a "
           "later minor version."),
    ("GET", "/runner"): dict(
        group="runner",
        zh="最近的运行（本进程与之前进程留下的记录，新的在前）。",
        en="Recent runs (this process and earlier ones), newest first."),
    ("GET", "/runner/{run_id}"): dict(
        group="runner",
        zh="一次运行：状态、结局、各会话段、用量、没有回音的工具调用。未知 id 回 404 `unknown_run`。",
        en="One run: state, outcome, segments, usage, tool calls that got no result. An unknown id "
           "is a 404 `unknown_run`."),
    ("POST", "/runner/{run_id}/stop"): dict(
        group="runner",
        zh="操作员叫停：这个会话的动作立刻被拒（`session_closed`），Claude Code 被打断，运行以 "
           "`stopped_by_operator` 结束。急停另走 `/estop`。",
        en="Operator stop: the session's actions are refused at once (`session_closed`), Claude "
           "Code is interrupted and the run ends as `stopped_by_operator`. The emergency stop is "
           "still `/estop`."),
    ("GET", "/optical-layouts"): dict(
        group="optical_layout",
        zh="用户在网页里画过的光路图（光学平台上器件的位置、方向、型号、是否电动、接到哪里）：`{layouts:[{id, "
           "title, revision, updated_at, updated_by, components, open_comments}]}`，最近保存的在前；读不了的"
           "文件多一个 `error`。没有图就是空列表。只读，不碰任何设备。",
        en="The optical layouts people drew in the web page (where the components on the optical table are, "
           "their direction, model, whether they are motorized, what they connect to): `{layouts:[{id, "
           "title, revision, updated_at, updated_by, components, open_comments}]}`, most recently saved "
           "first; a file that cannot be read has an extra `error`. An empty list when nothing has been "
           "drawn. Read-only; no device is touched."),
    ("GET", "/optical-layouts/{layout_id}"): dict(
        group="optical_layout",
        zh="一张光路图：`layout`（几何、器件、注释、链接；单位 mm 与度，原点在平台左下角，x 向右、y 向上，"
           "角度从 +x 逆时针量）、`trace`（每个光源每个波长一棵树：折线、命中点、`offset_mm`（命中点偏离"
           "器件中心的有符号距离）、`incidence_deg`、`path_mm`（累计路径长））、`summary`（沿光束顺序的"
           "文字叙述和问题清单，**先读它**；语言取请求的 `X-MAST-Lang`）、`links_state`（器件链接的设备 / "
           "手动光学状态槽位当下的状态；读已打开设备的状态，**从不为此打开设备**）、`comments`（未解决的"
           "评论）。未知 id 回 404 `unknown_layout`，文件读不了回 500 `layout_unreadable`（文件原样保留）。"
           "**这张图你改不了**：它记录台面上实际摆的是什么，只有操作员知道。",
        en="One optical layout: `layout` (geometry, components, annotations, links; mm and degrees, origin at "
           "the table's lower left, x to the right, y up, angles counter-clockwise from +x), `trace` (one "
           "tree per light source and wavelength: polylines, hit points, `offset_mm` (the signed distance of "
           "the hit from the component's center), `incidence_deg`, `path_mm` (path length so far)), `summary` "
           "(the beam paths in order and the list of problems - **read this first**; in the language of the "
           "request's `X-MAST-Lang`), `links_state` (the current state behind each device / manual "
           "optical-state slot link; devices the process already has open are read, **a device is never "
           "opened for this**) and `comments` (the unresolved ones). An unknown id is a 404 "
           "`unknown_layout`, a file that cannot be read a 500 `layout_unreadable` (the file is left as it "
           "is). **You cannot change this layout**: it records what is physically on the table, which only "
           "the operator knows."),
    ("GET", "/optical-layouts/{layout_id}/render.png"): dict(
        group="optical_layout",
        zh="这张图的 PNG（`image/png`）：器件是简单图形，光束按波长着色（红外 / 紫外虚线，粗细随强度），命中"
           "偏离中心超过口径 20% 的地方画黄圈，打在背面或险些没中的地方画红叉。`width` 是像素宽（200–3000，"
           "缺省 1200）；`fit=content`（缺省）缩放到器件与光束所在处，`fit=table` 画整个平台（坐标轴始终是"
           "平台坐标）。不需要显示器。",
        en="A PNG (`image/png`) of the layout: components as simple shapes, beams coloured by wavelength "
           "(infrared / ultraviolet dashed, weight following the intensity), a yellow ring where a beam hits "
           "more than 20% of the aperture off center, a red cross where it meets a back side or just misses "
           "a component. `width` is the width in pixels (200-3000, default 1200); `fit=content` (the "
           "default) zooms to where the components and beams are, `fit=table` draws the whole table (the axes "
           "always carry table coordinates). Needs no display."),
    ("POST", "/optical-layouts/{layout_id}/comments"): dict(
        group="optical_layout",
        zh="在这张图上发一条评论（质疑、提醒、建议），可以挂在某个器件上（`component_id`，必须在图里，否则 422 "
           "`invalid_comment`）。作者记成你的 agent id（`ext:<X-MAST-Actor>`），不收请求体里的署名；请求体里"
           "多出任何字段都是 422。操作员在网页里看到并由操作员解决——**你不能解决评论，也不能改图**。每个 agent "
           "在一张图上没解决的评论 ≤ 50（429 `too_many_open_comments`），一张图的评论总数 ≤ 1000（满了先淘汰最老"
           "的已解决评论；全是未解决的就回 429 `layout_comments_full`）。回 `{ok, comment}`。",
        en="Add a comment to this layout (a question, a warning, a suggestion), optionally about one component "
           "(`component_id`, which must be in the layout, else 422 `invalid_comment`). The author is recorded "
           "as your agent id (`ext:<X-MAST-Actor>`); a name in the body is not accepted and any extra field "
           "is a 422. The operator sees it in the web page and resolves it - **you cannot resolve a comment "
           "or change the layout**. An agent may have at most 50 unresolved comments on one layout (429 "
           "`too_many_open_comments`) and a layout holds at most 1000 comments (when full the oldest resolved "
           "ones make room; all unresolved answers 429 `layout_comments_full`). Answers `{ok, comment}`.",
        example=EX_LAYOUT_COMMENT),
    ("POST", "/runner/{run_id}/hook"): dict(
        group="runner",
        zh="Claude Code 钩子（PreCompact / PostCompact / Stop）的中转，只接受本机回环、带这次运行的 "
           "`X-MAST-Runner-Secret`。回 `{output: <钩子决定>}`。给运行器自己用，agent 不调它。",
        en="Relay for Claude Code's hooks (PreCompact / PostCompact / Stop), loopback only, with "
           "the run's `X-MAST-Runner-Secret`. Answers `{output: <hook decision>}`. For the runner "
           "itself; agents never call it."),
}

#: One line per endpoint for the at-a-glance table: (zh, en).
BRIEF: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/health"): ("外部面自己的健康与接线", "Health and wiring of the external surface"),
    ("GET", "/guide"): ("面向 agent 的铁律", "The operating rules for agents"),
    ("GET", "/status"): ("快速状态（零硬件 I/O）", "Quick status (no hardware I/O)"),
    ("GET", "/briefing"): ("分段简报：开始工作前先读", "Sectioned briefing: read it before anything else"),
    ("GET", "/scope"): ("当前实验与样品；动作会不会被记录", "Current experiment and sample; whether actions are recorded"),
    ("POST", "/scope"): ("切换或新建实验 / 样品", "Switch to, or create, an experiment or sample"),
    ("POST", "/estop"): ("硬件急停并取消外部作业", "Hardware emergency stop; cancels external jobs"),
    ("POST", "/jobs"): ("提交技能作业（立即返回）", "Submit a skill as a job (returns at once)"),
    ("GET", "/jobs"): ("你最近的作业", "Your recent jobs"),
    ("GET", "/jobs/{job_id}"): ("作业视图，可长轮询", "The job view, with long polling"),
    ("POST", "/jobs/{job_id}/cancel"): ("协作式取消", "Cooperative cancel"),
    ("GET", "/skills/search"): ("按动作 / Nanonis 命令找技能", "Find skills by action or Nanonis command"),
    ("GET", "/skills/{name}"): ("技能卡", "The skill card"),
    ("POST", "/composites/draft"): ("校验组合技能草稿（不保存）", "Validate a composite draft (nothing saved)"),
    ("POST", "/composites"): ("保存并热注册组合技能", "Save and register a composite skill"),
    ("POST", "/skills/proposals"): ("提议 Python 技能（待人审）", "Propose a Python skill (for review)"),
    ("GET", "/data/files"): ("最近的数据文件", "Recent data files"),
    ("GET", "/data/search"): ("按元数据检索数据记录", "Search data records by metadata"),
    ("GET", "/data/file"): ("原始字节", "Raw bytes"),
    ("GET", "/data/frame"): ("统一朝向后的 `.sxm` 帧（`.npz`）", "An oriented `.sxm` frame (`.npz`)"),
    ("POST", "/notes"): ("写笔记进 MAST 记忆库", "Write a note into MAST's memory"),
    ("GET", "/notes"): ("检索笔记（含内部 agent 写的）", "Search notes (including those by MAST's agents)"),
    ("POST", "/requests"): ("向操作员发问", "Ask the operator"),
    ("GET", "/requests"): ("你的请求与答复", "Your requests and the answers"),
    ("GET", "/requests/{request_id}"): ("单条请求与答复", "One request and its answer"),
    ("POST", "/handover"): ("交接报告存进文档库", "A handover report in the document library"),
    ("POST", "/vista/hello"): ("登记客户端会话（之后驱动仪器只能走动作）",
                              "Register the client session (instrument-driving then only through act)"),
    ("POST", "/vista/observe"): ("最新观测与新令牌", "The newest observation and a fresh token"),
    ("POST", "/vista/act"): ("执行一步动作（带令牌）", "Execute one action (with the token)"),
    ("GET", "/vista/acts/{job_id}"): ("动作作业的状态与观测", "An action job's state and observation"),
    ("GET", "/vista/status"): ("工作区与闸门状态（不含令牌）", "Workspace and gate state (never the token)"),
    ("POST", "/vista/inspect"): ("放大镜：回看任意帧", "The magnifier: look at any archived frame"),
    ("POST", "/vista/read_values"): ("精确读数", "Exact readings"),
    ("POST", "/vista/measure/{tool}"): ("量尺：剖面、FFT、计数、差分、统计",
                                       "Rulers: profile, FFT, blobs, difference, statistics"),
    ("GET", "/vista/notes/{which}"): ("读 GUIDE.md / WORKING.md", "Read GUIDE.md / WORKING.md"),
    ("PUT", "/vista/notes/{which}"): ("写 GUIDE.md / WORKING.md", "Write GUIDE.md / WORKING.md"),
    ("GET", "/vista/history"): ("本实验的动作记录与汇总", "This experiment's action records and summary"),
    ("POST", "/wait"): ("服务端等待：作业、新帧、告警、答复、探测器、定时",
                        "Server-side wait: job, frame, alert, reply, detector, timer"),
    ("POST", "/detectors"): ("建一个分析探测器（pyexec）", "Create an analysis detector (pyexec)"),
    ("GET", "/detectors"): ("探测器清单", "List the detectors"),
    ("GET", "/detectors/{detector_id}"): ("一个探测器及其命中", "One detector and its hits"),
    ("POST", "/detectors/{detector_id}/enable"): ("启用 / 停用", "Enable / disable"),
    ("DELETE", "/detectors/{detector_id}"): ("删除探测器", "Delete a detector"),
    ("POST", "/detectors/{detector_id}/test"): ("在一帧上试跑一次", "Try it once on one frame"),
    ("POST", "/literature/search"): ("文献知识库检索", "Search the literature knowledge base"),
    ("POST", "/runner/start"): ("启动无人值守运行（Claude Code，订阅）", "Start an unattended run (Claude Code, subscription)"),
    ("GET", "/runner"): ("最近的运行", "Recent runs"),
    ("GET", "/runner/{run_id}"): ("一次运行的状态", "One run's state"),
    ("POST", "/runner/{run_id}/stop"): ("操作员叫停", "Operator stop"),
    ("POST", "/runner/{run_id}/hook"): ("钩子中转（运行器自用）", "Hook relay (the runner's own)"),
    ("GET", "/optical-layouts"): ("用户画的光路图清单", "The optical layouts people drew"),
    ("GET", "/optical-layouts/{layout_id}"): ("一张光路图：几何、追迹、文字叙述、链接状态、评论",
                                             "One layout: geometry, trace, description, link state, comments"),
    ("GET", "/optical-layouts/{layout_id}/render.png"): ("这张图的 PNG", "A PNG of the layout"),
    ("POST", "/optical-layouts/{layout_id}/comments"): ("发评论（不能改图、不能解决评论）",
                                                       "Comment (you cannot edit the layout or resolve comments)"),
}

INTRO = {
    "zh": ("# MAST 外部 agent API v1 参考\n\n"
           "> 本页由 `scripts/gen_ext_api_reference.py` 从 API 自己的 OpenAPI 生成，请勿手改。\n\n"
           "基础地址：`http://127.0.0.1:7862/api/ext/v1`（远程见 [快速上手](01-quickstart.md)）。"
           "机器可读的契约在 `GET /openapi.json`，交互式文档在 `/docs`。v1 之内只增不减。\n"),
    "en": ("# MAST external-agent API v1 reference\n\n"
           "> Generated by `scripts/gen_ext_api_reference.py` from the API's own OpenAPI. Do not "
           "edit by hand.\n\n"
           "Base address: `http://127.0.0.1:7862/api/ext/v1` (remote access: see "
           "[Quickstart](01-quickstart.md)). The machine-readable contract is `GET "
           "/openapi.json`; interactive documentation is at `/docs`. Within v1 the contract only "
           "grows.\n"),
}

CONVENTIONS = {
    "zh": """## 通用约定

| 请求头 | 含义 |
|---|---|
| `X-MAST-Actor` | 你的名字（可选）。清洗成小写的 `[\\w.-]`，最长 48；空 = `anonymous`。它只用于**归属**：动作、笔记、请求、组合技能都署 `ext:<名字>`。它不是认证，也永远不会是拒绝请求的理由。 |
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
""",
    "en": """## Conventions

| Request header | Meaning |
|---|---|
| `X-MAST-Actor` | Your name (optional). Cleaned to lower-case `[\\w.-]`, at most 48 characters; empty = `anonymous`. It is used only for **attribution**: actions, notes, requests and composite skills are signed `ext:<name>`. It is not authentication and never a reason to refuse a request. |
| `X-MAST-Session` | Session name (optional, at most 64). The record signature becomes `ext:<name>/<session>`, telling two connections under one name apart. |

Authentication is the main API's: none on the local loopback; HTTP Basic plus TLS in LAN mode. Every response carries `X-MAST-Ext-Version: 1`.

The external surface is for programmatic clients (the MCP server, scripts, `curl`): **every POST must carry `Content-Type: application/json` and a JSON body** (send `{}` when you have nothing to say, `/estop` and `/jobs/{job_id}/cancel` included), and browser-originated cross-site requests are refused.

Responses are JSON except `/data/file` and `/data/frame`. Errors have one shape; `detail` is a human-readable explanation (currently in Chinese):

```json
{"error": "request_id_conflict", "detail": "...", "existing_job_id": "j_0123456789ab"}
```

| Status | Meaning |
|---|---|
| 200 / 202 | Success; 202 = a new job was accepted. **A skill that fails is still a 200**: read the job's `state` and `result.success`. |
| 403 | The path is outside the allowed roots, or the extension is not whitelisted; or `cross_origin_refused`: a browser-originated cross-site request (an `Origin` that is not this service, a cross-site `Sec-Fetch-Site`, or a strange host name — the shape of DNS rebinding). |
| 404 | No such resource (skill, job, experiment, sample, request, file, channel, path). |
| 409 | Conflict: the `request_id` was used for a different body; switching scope while the instrument is in use; an observation-gate refusal (`observe_first`, `act_in_flight`, `same_code_cell`, `checkpoint_requested`, `session_closed`, `use_act`); another run is active. |
| 415 | `unsupported_media_type`: a write request without `application/json`. |
| 422 | Invalid request; the skill is switched off on this instrument. |
| 429 | Too many jobs running at once. |
| 503 | A subsystem is not wired (see `missing`), or the service is shutting down. |
""",
}

JOB_VIEW = """\
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
```"""

JOB_SECTION = {
    "zh": f"""## 作业视图

{JOB_VIEW}

| `state` | 含义 |
|---|---|
| `queued` / `running` | 还没结束。 |
| `succeeded` | 技能报告成功。 |
| `failed` | 技能失败或被拒，详情见 `result.error`。`refused_by` 只标识部分拒绝类别（如 `sample_gate`、`si_parse`、`needs_human_node`），也可能为 null。 |
| `refused_busy` | 仪器正被别人占用，**不排队**；`busy_holder` 说明是谁在开车。 |
| `cancelled` | 已请求取消，且作业未报告成功。仪器最终状态需要另行核实。 |
| `crashed` | 执行线程自己出了意外。 |
| `lost_on_restart` | 服务重启时它还没结束，不知道它在仪器上做到了哪一步。**绝不重放**；用同一个 `request_id` 重发拿到的就是这一条。先读简报再决定下一步。 |

`recorded` 是实验记录写入回执。`v1: false` 表示 v1 写入未成功，可能是缺作用域、存储未接线或写入错误；结合 `experiment_id`、`sample_id`、`v2_action_id` 和可能的 `error` 判断。空回执不代表已记录，独立作业日志也不等于实验动作记录。`abort` 在技能结束时中止事件已置位的情况下给出 `{{set, reason}}`。`result.data` 过大时会被截断。
""",
    "en": f"""## Job view

{JOB_VIEW}

| `state` | Meaning |
|---|---|
| `queued` / `running` | Not finished yet. |
| `succeeded` | The skill reported success. |
| `failed` | The skill failed or was refused; read `result.error`. `refused_by` identifies selected categories (such as `sample_gate`, `si_parse`, `needs_human_node`) and can be null. |
| `refused_busy` | Someone else holds the instrument; **nothing is queued**. `busy_holder` says who is driving. |
| `cancelled` | Cancellation was requested and the job did not report success. Verify the resulting instrument state separately. |
| `crashed` | The job's own thread failed unexpectedly. |
| `lost_on_restart` | It had not finished when the service restarted, so nobody knows how far it got on the instrument. **Never replayed**; resending with the same `request_id` returns this entry. Read the briefing before deciding what to do next. |

`recorded` is the experiment-write receipt. `v1: false` means that v1 write did not succeed: missing scope, unavailable storage or a write error are possible causes. Inspect `experiment_id`, `sample_id`, `v2_action_id` and any `error`. An empty receipt does not confirm recording, and the separate job journal is not an experiment action record. `abort` is `{{set, reason}}` when the abort event was set as the skill finished. A very large `result.data` is truncated.
""",
}

BRIEFING_ROWS = [
    ("status", "与 `GET /status` 相同的内容，写成文字。", "The same as `GET /status`, as text."),
    ("scope", "当前实验、样品、目标；没有时说明后果。",
     "Current experiment, sample and goal; the consequence when there is none."),
    ("resume", "续工块：这个实验此前做到了哪里（与 MAST 自己的 agent 看到的相同）。",
     "The resume block: how far this experiment has come (the same text MAST's own agents "
     "see)."),
    ("tip", "当前针尖的登记信息。", "The registered facts about the current tip."),
    ("instrument", "仪器档案与已学到的标定。", "The instrument profile and learned calibrations."),
    ("live", "live 读数（缓存快照）。", "Live readings (cached snapshot)."),
    ("prefs", "操作员设置的默认参数偏好。", "The operator's default-parameter preferences."),
    ("safety", "此刻生效的安全包络，以及挂着的闩和解除办法。",
     "The safety envelope in force, and any latched stops with how to release them."),
    ("recent_actions", "本实验最近的动作，带署名（谁做的）。",
     "Recent actions in this experiment, with who did them."),
    ("recent_files", "最近的数据文件。", "Recent data files."),
    ("alarms", "环境监控的总体状态与最近的告警。",
     "The environment monitor's overall status and recent alarms."),
    ("notes", "本实验最近的笔记（任何作者）。", "Recent notes in this experiment (any author)."),
    ("recording", "你的署名，以及当前作用域是否配置了实验记录。",
     "Your signature and whether experiment recording is configured for the current scope."),
    ("jobs", "你最近的作业。", "Your recent jobs."),
    ("operator_requests", "你发给操作员的请求与答复。",
     "Your requests to the operator, and the answers."),
    ("optical_layouts", "用户画过的光路图：几张、各自器件数、未解决评论数、怎么读。**没有图时这一段为空**，"
     "不进顶层 `text`。两种站点都有。",
     "The optical layouts people drew: how many, their component counts, unresolved comments, how to read "
     "one. **Empty when nothing has been drawn** and then left out of the top-level `text`. On both "
     "station profiles."),
]

CARD_ROWS = [
    ("parameters", "每个参数的类型、单位、必填、默认值、上下限、取值集。",
     "Each parameter's type, unit, required flag, default, bounds and allowed values."),
    ("safety_level / capabilities", "安全级与能力标签（例如是否属于针尖处理）。",
     "Safety level and capability tags (for example tip processing)."),
    ("preconditions", "运行前必须成立的条件。", "Conditions that must hold before running."),
    ("category / tags", "类别与标签。", "Category and tags."),
    ("origin / origin_code / official", "来源：`origin` 是给人读的标签（目前是中文），`origin_code` "
     "是语言中立的代号（`builtin`、`composite`、`paper`、`user_composite`、`custom`、`agent_tool`、"
     "`overlay`、`other`）；`official` 只对前三种为真。",
     "Where it comes from: `origin` is a human label (currently in Chinese), `origin_code` a "
     "language-neutral code (`builtin`, `composite`, `paper`, `user_composite`, `custom`, "
     "`agent_tool`, `overlay`, `other`); `official` is true only for the first three."),
    ("footprint / footprint_basis / footprint_reasons",
     "对仪器做什么（`pure-analysis` / `hardware-read-only` / `hardware-write` / `unknown`）。与投稿校验器 "
     "`scripts/skill_check.py` 出自同一份静态分析：`footprint_basis` 为 `static` 是从源码读出来的，"
     "`declared` 是源码看不透、按类别保守取的，`footprint_reasons` 说明为什么看不透。",
     "What it does to the instrument (`pure-analysis` / `hardware-read-only` / `hardware-write` / "
     "`unknown`). The same static analysis as the contribution checker `scripts/skill_check.py`: "
     "`footprint_basis` `static` means read from the source, `declared` means the source could not "
     "be analysed and the category gave a conservative answer; `footprint_reasons` says why."),
    ("verbs / verbs_unknown", "会发的 Nanonis 命令；分析不完整时 `verbs_unknown:true`，不把缺失报成空集。",
     "The Nanonis commands it sends; `verbs_unknown:true` when the analysis is incomplete, never an "
     "empty list standing in for \"unknown\"."),
    ("sub_skills", "组合技能的子步。", "The sub-steps of a composite skill."),
    ("takes_instrument_token", "是否占用仪器锁（占用时别人会被 `refused_busy`）。",
     "Whether it holds the instrument lock (others get `refused_busy` meanwhile)."),
    ("requires_sample", "是否必须先选样品。", "Whether a sample must be selected first."),
    ("si_params", "哪些参数接受 `\"5n\"` 这类写法，以及是否必须带前缀。",
     "Which parameters accept strings such as `\"5n\"`, and whether the prefix is required."),
    ("tool_face", "本机是否关闭了它，以及原因。", "Whether it is switched off here, and why."),
    ("duration", "`{estimated_s, measured:{n, p50_s, p95_s, max_s}|null, note}` —— `measured` "
     "取自这台仪器上的成功记录。", "`{estimated_s, measured:{n, p50_s, p95_s, max_s}|null, "
     "note}` — `measured` comes from successful runs on this instrument."),
]


# ─────────────────────────────────────────────────────────────────────
# OpenAPI → tables
# ─────────────────────────────────────────────────────────────────────

L = {
    "zh": dict(glance="端点一览", method="方法", path="路径", purpose="作用",
               param="参数", where="位置", type="类型", req="必填", default="默认",
               limits="约束", field="字段", yes="是", body="请求体", also="其中",
               briefing="简报的段", section="段", contents="内容", card="技能卡",
               card_intro="除参数外，技能卡给出：", key="字段"),
    "en": dict(glance="Endpoints at a glance", method="Method", path="Path", purpose="Purpose",
               param="Parameter", where="In", type="Type", req="Required", default="Default",
               limits="Limits", field="Field", yes="yes", body="Request body", also="where",
               briefing="Briefing sections", section="Section", contents="Contents",
               card="Skill card", card_intro="Besides the parameters, the skill card gives:",
               key="Field"),
}


def _cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _type(schema: dict) -> str:
    if not schema:
        return "any"
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return " | ".join(_type(s) for s in schema[key])
    if "enum" in schema:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"])
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    t = schema.get("type")
    if t == "array":
        return f"array<{_type(schema.get('items') or {})}>"
    return str(t or "any")


def _limits(schema: dict) -> str:
    parts = []
    for key, label in (("minimum", "min"), ("exclusiveMinimum", ">"), ("maximum", "max"),
                       ("exclusiveMaximum", "<"), ("minLength", "minLength"),
                       ("maxLength", "maxLength")):
        if key in schema:
            parts.append(f"{label}={schema[key]}")
    for sub in schema.get("anyOf", ()):
        if sub.get("type") != "null":
            lim = _limits(sub)
            if lim:
                parts.append(lim)
    return ", ".join(parts)


def _default(schema: dict, required: bool) -> str:
    if required:
        return ""
    if "default" in schema:
        return "`" + json.dumps(schema["default"], ensure_ascii=False) + "`"
    return ""


def _code(s: str) -> str:
    return f"`{s}`" if s else ""


def _param_table(op: dict, lang: str) -> str:
    params = [p for p in op.get("parameters", []) if p.get("in") in ("path", "query")]
    if not params:
        return ""
    t = L[lang]
    rows = [f"| {t['param']} | {t['where']} | {t['type']} | {t['req']} | {t['default']} | "
            f"{t['limits']} |", "|---|---|---|---|---|---|"]
    for p in params:
        s = p.get("schema") or {}
        req = bool(p.get("required"))
        rows.append("| " + " | ".join(_cell(x) for x in (
            f"`{p['name']}`", p["in"], _code(_type(s)), t["yes"] if req else "",
            _default(s, req), _code(_limits(s)))) + " |")
    return "\n".join(rows) + "\n"


def _schema_table(name: str, comps: dict, lang: str) -> list[str]:
    t = L[lang]
    sch = comps.get(name) or {}
    req = set(sch.get("required") or ())
    rows = [f"| {t['field']} | {t['type']} | {t['req']} | {t['default']} | {t['limits']} |",
            "|---|---|---|---|---|"]
    for fname, fs in (sch.get("properties") or {}).items():
        r = fname in req
        rows.append("| " + " | ".join(_cell(x) for x in (
            f"`{fname}`", _code(_type(fs)), t["yes"] if r else "", _default(fs, r),
            _code(_limits(fs)))) + " |")
    return rows


def _refs(schema: dict) -> list[str]:
    out: list[str] = []
    if "$ref" in schema:
        out.append(schema["$ref"].rsplit("/", 1)[-1])
    for key in ("anyOf", "oneOf"):
        for s in schema.get(key, ()):
            out += _refs(s)
    if schema.get("type") == "array":
        out += _refs(schema.get("items") or {})
    return out


def _body_table(op: dict, comps: dict, lang: str) -> str:
    body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {})
    roots = _refs(body.get("schema") or {})
    if not roots:
        return ""
    t = L[lang]
    out = [f"**{t['body']}** (`{roots[0]}`)", ""] + _schema_table(roots[0], comps, lang)
    nested = []
    for fs in ((comps.get(roots[0]) or {}).get("properties") or {}).values():
        for r in _refs(fs):
            if r not in nested and r != roots[0]:
                nested.append(r)
    for r in nested:
        out += ["", f"{t['also']} `{r}`:", ""] + _schema_table(r, comps, lang)
    return "\n".join(out) + "\n"


def _anchor_heading(method: str, path: str) -> str:
    return f"### `{method} {path}`"


def build(spec: dict) -> dict[str, str]:
    paths = spec.get("paths") or {}
    comps = (spec.get("components") or {}).get("schemas") or {}
    have = {(m.upper(), p) for p, ops in paths.items() for m in ops}
    missing = sorted(have - set(ENDPOINTS)) + sorted(have - set(BRIEF))
    stale = sorted(set(ENDPOINTS) - have) + sorted(set(BRIEF) - have)
    if missing or stale:
        raise SystemExit(f"gen_ext_api_reference: endpoints without prose {missing}; prose for "
                         f"endpoints that no longer exist {stale}")
    out: dict[str, str] = {}
    for lang in ("zh", "en"):
        t = L[lang]
        parts = [INTRO[lang], CONVENTIONS[lang]]
        glance = [f"## {t['glance']}", "", f"| {t['method']} | {t['path']} | {t['purpose']} |",
                  "|---|---|---|"]
        for gkey, gzh, gen in GROUPS:
            for (m, p), d in ENDPOINTS.items():
                if d["group"] == gkey:
                    brief = BRIEF[(m, p)][0 if lang == "zh" else 1]
                    glance.append(f"| {m} | `{_cell(p)}` | {_cell(brief)} |")
        parts.append("\n".join(glance) + "\n")
        for gkey, gzh, gen in GROUPS:
            sec = [f"## {gzh if lang == 'zh' else gen}", ""]
            for (m, p), d in ENDPOINTS.items():
                if d["group"] != gkey:
                    continue
                op = paths[p][m.lower()]
                sec += [_anchor_heading(m, p), "", d[lang], ""]
                pt = _param_table(op, lang)
                if pt:
                    sec += [pt]
                bt = _body_table(op, comps, lang)
                if bt:
                    sec += [bt]
                if d.get("example"):
                    sec += [d["example"], ""]
            parts.append("\n".join(sec).rstrip() + "\n")
        parts.append(JOB_SECTION[lang])
        rows = [f"## {t['briefing']}", "", f"| {t['section']} | {t['contents']} |", "|---|---|"]
        rows += [f"| `{n}` | {_cell(zh if lang == 'zh' else en)} |" for n, zh, en in BRIEFING_ROWS]
        parts.append("\n".join(rows) + "\n")
        rows = [f"## {t['card']}", "", t["card_intro"], "", f"| {t['key']} | {t['contents']} |",
                "|---|---|"]
        rows += [f"| `{_cell(n)}` | {_cell(zh if lang == 'zh' else en)} |" for n, zh, en in CARD_ROWS]
        parts.append("\n".join(rows) + "\n")
        text = "\n".join(p.rstrip("\n") + "\n" for p in parts)
        out[lang] = text.replace("\r\n", "\n")
    return out


def ext_openapi() -> dict:
    from mast.api.ext.app import create_ext_app
    from mast.api.ext.jobs import JobManager

    return create_ext_app(SimpleNamespace(), job_manager=JobManager("unused")).openapi()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if a file on disk differs")
    args = ap.parse_args(argv)
    docs = build(ext_openapi())
    stale = []
    for lang, text in docs.items():
        path = OUT[lang]
        current = path.read_bytes().decode("utf-8") if path.is_file() else None
        if current == text:
            continue
        if args.check:
            stale.append(str(path.relative_to(REPO)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        print(f"wrote {path.relative_to(REPO)}")
    if stale:
        print("out of date (run: python scripts/gen_ext_api_reference.py): " + ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
