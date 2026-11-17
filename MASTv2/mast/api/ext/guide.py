"""面向外部 agent 的铁律清单（``GET /api/ext/v1/guide``）。

完整的双语指南在仓库的 ``docs/external/{zh,en}/``（Claude Code 插件把它随包带着）。
这里提供操作入口与执行约束的简要说明；配置、参数与实际执行结果仍需逐项检查。
完整指南区分作业状态、实验记录和物理仪器反馈。
"""

from __future__ import annotations

RULES: tuple[dict, ...] = (
    {"id": "briefing-first",
     "en": ("Read the briefing first",
            "Start every session with GET /briefing. It carries what MAST's own agents see "
            "every turn: tip, instrument profile, live readings, operator preferences, the "
            "resume block, recent actions by anyone, and your open requests."),
     "zh": ("先读简报",
            "每次接手先 GET /briefing。里面是 MAST 自己的 agent 每一轮都能看到的东西：针尖、"
            "仪器档案、实时读数、操作员偏好、续工块、所有人最近的动作、你发出的请求。")},
    {"id": "scope-before-data",
     "en": ("Set the scope before producing data",
            "Choose or create the experiment and sample (GET/POST /scope) first. Without a "
            "sample, scans and spectroscopy are refused. An active experiment is needed for "
            "experiment-linked recording; inspect each job's recorded fields."),
     "zh": ("先定实验与样品",
            "先用 GET/POST /scope 选定或新建实验与样品。没有样品，扫描与谱学在门口就被拒；"
            "实验关联记录需要活动实验；逐项检查作业的 recorded 字段。")},
    {"id": "find-by-action",
     "en": ("Find skills by what they do",
            "Search with /skills/search using the action you need (and Nanonis command names); "
            "skills are named after the problem they solve. Read the skill card before running: "
            "units, bounds, preconditions, measured duration on this instrument."),
     "zh": ("按动作找技能",
            "用 /skills/search 按「要做的动作」（以及 Nanonis 命令名）搜；技能是按它解决的问题"
            "命名的。执行前读技能卡：单位、范围、前置条件、这台仪器上的实测耗时。")},
    {"id": "jobs-not-timeouts",
     "en": ("Run skills as jobs",
            "Submit with POST /jobs and poll GET /jobs/{id}?wait_s=30. A client-side timeout "
            "is not a failure: the skill keeps running and keeps the instrument. Retry a lost "
            "submission with the same request_id, never with a new one."),
     "zh": ("用作业跑技能",
            "用 POST /jobs 提交、GET /jobs/{id}?wait_s=30 轮询。客户端超时不等于失败：技能还在跑、"
            "还占着仪器。提交丢了就用**同一个** request_id 重发，绝不换新的。")},
    {"id": "busy-means-wait",
     "en": ("Busy means someone else is driving",
            "refused_busy means another driver holds the instrument (busy_holder says who). "
            "Retrying immediately will not make it finish sooner: wait for it, or ask the operator."),
     "zh": ("撞锁就是别人在开车",
            "refused_busy 表示另一条链路正占着仪器（busy_holder 说是谁）。立刻重试不会让它更快"
            "结束：等它，或者问操作员。")},
    {"id": "how-to-stop",
     "en": ("How to stop",
            "POST /jobs/{id}/cancel requests cooperative cancellation at the next check. "
            "A stop skill such as StopScan bypasses the instrument ownership lock; POST /estop "
            "sets the emergency latch and requests stops. Verify the result and instrument "
            "feedback. Only the operator clears the latch."),
     "zh": ("怎么停",
            "POST /jobs/{id}/cancel 请求在下一次检查处协作式取消。StopScan 这类停止技能可绕过"
            "仪器占用锁；POST /estop 设置急停闩并请求停止。检查返回结果与仪器反馈，"
            "急停闩仅由操作员解除。")},
    {"id": "three-states",
     "en": ("ok, success and degraded are different facts",
            "A failed skill is not an endpoint error, and a degraded answer (a subsystem not "
            "wired) is not an empty answer. Read all three before concluding anything."),
     "zh": ("ok / success / degraded 是三件事",
            "技能失败不是端点错误；degraded（某个子系统没接线）不是「空」。三者都看完再下结论。")},
    {"id": "operating-mode",
     "en": ("The operating mode is the operator's",
            "SAFE refuses autonomous tip processing (bias pulses, tip shaping), including "
            "external jobs and composite substeps. Do not route around it or change the mode; if "
            "you need tip processing, ask the operator through /requests."),
     "zh": ("运行模式是操作员定的",
            "SAFE 拒绝自主路径中的针尖处理（电脉冲、机械修针），包括外部作业与组合子步。不要绕、"
            "不要改模式；需要针尖处理就经 /requests 问操作员。")},
    {"id": "no-proxy-approval",
     "en": ("Never decide for the operator",
            "Do not approve or resolve human-in-the-loop prompts on the operator's behalf. Ask "
            "through /requests and wait for the answer."),
     "zh": ("不替人拍板",
            "不要替操作员批准或处理任何需要人决定的提示。经 /requests 问，等回答。")},
    {"id": "after-restart",
     "en": ("After a restart, look before you act",
            "Jobs that were running when the service restarted are marked lost_on_restart and "
            "are never replayed. Read /briefing to learn what state the instrument is in first."),
     "zh": ("重启之后先看再动",
            "服务重启时还在跑的作业标为 lost_on_restart，绝不重放。先读 /briefing 弄清仪器处在"
            "什么状态。")},
    {"id": "raw-data",
     "en": ("Use the server's frames",
            "Fetch scan images with /data/frame: the server applies the orientation conventions "
            "(backward-direction mirror, scan-direction flip) once and correctly."),
     "zh": ("用服务端给的帧",
            "扫描图用 /data/frame 取：服务端已经把朝向约定（反向扫描的镜像、扫描方向的翻转）"
            "一次处理对了。")},
    {"id": "capture-skills",
     "en": ("Capture what worked as a local skill",
            "When real work with MAST makes you write a procedure or tool that proved useful "
            "(validated on real data or on the instrument), save it as a reusable skill: a composite "
            "(POST /composites) if official skills can express it, else a Python proposal "
            "(POST /skills/proposals, or scripts/skill_scaffold.py new). Default is LOCAL (config/custom_skills/, "
            "gitignored, enabled only by the operator); tell the user what you saved and what evidence "
            "validated it. Open a pull request (contrib/skills/) only if the user wants one: follow their "
            "stated preference, else ask once. A captured skill never bypasses a safety gate."),
     "zh": ("好用的做法存成本地技能",
            "在 MAST 的真实工作里写出了被验证有用的流程或小工具（真实数据或仪器上验证过），就把它存成"
            "可复用技能：官方技能能表达就做组合技能（POST /composites），否则提交 Python 提议"
            "（POST /skills/proposals，或 scripts/skill_scaffold.py new）。默认存**本地**"
            "（config/custom_skills/，已 gitignore，只有操作员能启用），并告诉用户存了什么、用什么证据"
            "验证过。要不要提 PR（contrib/skills/）由用户决定：按其已表达的偏好，否则问一次。"
            "存下来的技能不能绕过任何安全门。")},
    {"id": "magnet-field",
     "en": ("Change a magnet's field only as the operator set it up",
            "On a station with a Lake Shore 625 magnet supply, change the field only with "
            "RampMagnetField, after CheckMagnetFieldChange. Before raising |B|, ask the user; pass "
            "their own words as user_authorization and the field they authorized (a magnitude) as "
            "authorized_up_to_t. Never write either yourself, and going higher means asking again. "
            "A negative target is a reverse field: the user's words must name it (\"-2 T\"), and a "
            "ramp never crosses zero (ramp to 0 first, then start a second ramp). "
            "The tip must be withdrawn unless you pass require_tip_withdrawn=false. Heating and "
            "field changes never overlap: a field change is refused while a Lake Shore 335 heater "
            "is on or MAST is heating (HeaterOff first). StopMagnetRamp freezes a ramp at any time "
            "(an E-STOP sends STOP too); it does not bring the field to zero."),
     "zh": ("磁场只按操作员设定的方式改",
            "在接了 Lake Shore 625 磁体电源的站上，改变磁场只用 RampMagnetField，先跑 "
            "CheckMagnetFieldChange。升场前先问用户，把用户授权时说的话逐字填进 user_authorization、"
            "用户授权到的磁场（绝对值）填进 authorized_up_to_t。两者都不要自己编；要升得更高就重新问。"
            "负的目标是反向场：用户的话里要明确写出（如「-2 T」），扫场不跨零（先扫到 0，再另起一次）。"
            "默认要求针尖已退，除非传 require_tip_withdrawn=false。加热和改磁场不同时进行："
            "Lake Shore 335 的加热器开着或 MAST 在加热时，改磁场一律拒绝（先 HeaterOff）。"
            "StopMagnetRamp 随时冻结扫场"
            "（急停也会发 STOP），但不会把磁场归零。")},
    {"id": "stage-heating",
     "en": ("Heat the sample stage only as the operator set it up",
            "On a station with a Lake Shore 335, change the stage temperature only with "
            "SetSampleTemperature, after CheckSampleTemperatureChange. Before raising the "
            "temperature, ask the user; pass their own words, naming the temperature, as "
            "user_authorization and that temperature as authorized_up_to_k. Never write either "
            "yourself. The tip must be withdrawn unless that was agreed with the user and you pass "
            "require_tip_withdrawn=false. No heating while MAST changes the field. HeaterOff turns "
            "MAST's heating off at any time (an E-STOP does too); HoldSampleTemperature stops a ramp "
            "where it is. Zone tables and the control mode (SetHeaterZoneTable, "
            "SetHeaterControlMode) are commissioning steps taken with the operator: never invent "
            "zone values."),
     "zh": ("样品台加热只按操作员设定的方式做",
            "在接了 Lake Shore 335 的站上，改变样品台温度只用 SetSampleTemperature，先跑 "
            "CheckSampleTemperatureChange。升温前先问用户，把用户授权时说的话逐字填进 "
            "user_authorization（要说到温度）、那个温度填进 authorized_up_to_k，两者都不要自己编。"
            "默认要求针尖已退；只有事先和用户约定过，才传 require_tip_withdrawn=false。MAST 改磁场时"
            "不加热。HeaterOff 随时关掉 MAST 的加热（急停也会关），HoldSampleTemperature 把扫温停在"
            "当前温度附近。分区表和控制模式（SetHeaterZoneTable、SetHeaterControlMode）是和操作员一起"
            "调试时做的事，分区参数不要自己编。")},
    {"id": "leave-a-trail",
     "en": ("Leave a trail",
            "Write conclusions as notes (POST /notes): MAST's own agents recall them. End with "
            "POST /handover and leave the instrument idle: no running jobs, scan stopped."),
     "zh": ("留下痕迹",
            "结论写成笔记（POST /notes），MAST 自己的 agent 会召回它们。结束时 POST /handover，"
            "并让仪器空闲：没有在跑的作业、扫描已停。")},
)

DOCS_HINT = {
    "en": "Full bilingual guide: docs/external/en/ (and docs/external/zh/) in the MAST repository.",
    "zh": "完整双语指南：MAST 仓库的 docs/external/zh/（英文版 docs/external/en/）。",
}


def rules(lang: str = "en") -> list[dict]:
    lang = "zh" if str(lang).lower().startswith("zh") else "en"
    return [{"id": r["id"], "title": r[lang][0], "text": r[lang][1]} for r in RULES]


def instructions_text(lang: str = "en") -> str:
    """一段纯文本，给只会读字符串的客户端（例如 MCP 的 initialize.instructions）。"""
    return "\n".join(f"- {r['title']}: {r['text']}" for r in rules(lang))


__all__ = ["DOCS_HINT", "RULES", "instructions_text", "rules"]
