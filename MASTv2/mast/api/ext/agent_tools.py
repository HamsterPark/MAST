"""仪器 agent 的工具对外部 agent 开放 —— 不分角色，委托除外（7.0.1）。

## 为什么有这个目录

外部 agent 能用的「技能」是技能注册表：仪器技能，加上 5 个非仪器 agent 导出的 ``@tool``
（``skills/composite/tool_skills.py`` 自动桥接成 AUTO 分析技能）。**仪器控制 agent 私聊里额外拿到的
那几族工具不在注册表里**（``CoreRuntime._chat_agent_extra_tools("instrument_control")``）：

* meta tool（``agents/_shared/meta_tools.py``）：实验 / 样品的列举与切换、知识库与指导、扫描地图与
  选点、粗动大地图、多步计划与 ``plan_scan_batch``、针尖登记；
* 文档（``document_tools``）、环境调查（``environment_tools``）、技能市场（``market_tools``）、
  conduct（``conduct_tools``）。

于是简报照着内部 agent 的上下文写「一律调 get_map_analysis」「要登记请调 register_tip」，外部 agent
手上却没有这些工具。设计决定：一切活都由外部 agent 来做，外部 agent 不分角色地可以访问
所有能力，但不提供外部 agent 交任务给内部 agent。

## 怎么开放

**不进全局技能注册表**：仪器控制 agent 的工具表由注册表生成，再注册一遍就是同一个工具出现两次。这里是
外部面自己的目录 —— ``/skills/search`` 与 ``/skills/{name}`` 把它们和技能一起列出（来源
:data:`ORIGIN_LABEL`），``POST /jobs`` 与 ``/vista/act`` 的原始技能分支按名字认出它们，作为
``kind="tool"`` 的作业在作业线程里跑：不驱动仪器，所以不取仪器令牌、不过观测令牌；入账形状与直调技能
相同（v1 + v2，署名 ``ext:<名字>/<会话>``）。工具按调用方现场构造：conduct 与技能市场两族把构造时给的
名字写进审计署名（``approved_by`` / op 的 ``by`` / 推荐的 ``by_agent``），在这里就是外部 agent 自己。

外部面在几处比内部 agent 多守一道（:func:`_door`）：改作用域的工具在仪器被占用时拒绝（与 ``/scope``
同一条）；带文件路径的工具只读数据搜索目录与实验根目录之内（与 ``/data/*`` 同一道
``api.ext.data.confine``）；``conduct_post_op`` 只收叫停与收回的 op。

## 不开放的（:data:`EXCLUDED`，逐个写着理由）

两类：

* 把活交给内部 agent 或后台运行时去做的那几个。
* :data:`OPERATOR_ONLY`：替操作员陈述一件**发生在仪器旁的物理事件**（换了针、在 Nanonis 里手动粗动过），
  而且登记它会清掉安全联锁或学习量 —— 补记粗动清空同一点撞针两次之后的封锁表、推进坐标代次；登记 / 注销
  针尖清掉 qPlus 撞针判据的振幅基线，修针的缺省剂量也按登记的材料与形态查表。远端的 agent 看不见那件事
  有没有发生；一个被封锁拦住的 agent 又正好有动机去「补记」一次。这是安全审查找出来的：
  真空计可能是占位实现，真粗动被真空互锁拦着，撞针两次之后补记一次假粗动是唯一能清掉封锁的路。
  仓里给设计 agent 只读针尖工具也是同一个理由（``meta_tools.DESIGN_TOOL_NAMES`` 的注释）。操作员在界面上
  都有自己的路：针尖卡片（``/api/tips``）、扫描地图页的补记粗动（``/api/scan-map/coarse-move``）。

记忆、问操作员、阻塞式 ask_user 外部面另有入口（notes、requests、wait operator_reply），这里不重复；
数据处理 agent 的代码执行类工具也不在本目录。

## 硬件 I/O

这里没有一个工具自己发控制器命令（逐个读过实现）：它们读写的是 MAST 自己的记录库、知识库、计划库、扫描
地图标记、conduct 意图队列和磁盘文件。唯一的控制器读取是 meta 这一族的 provider 每次调用时刷新 Nanonis
的保存目录（``CoreRuntime._resolve_session_dir`` → ``Util_SessionPathGet``，15 s 缓存，只读）——
与仪器控制 agent 调同一个工具时一模一样。``plan_scan_batch`` 读针尖 XY 排序的那一步在这条 provider 上
拿不到执行上下文，按原点排序（只影响走位顺序，不影响每一帧扫在哪里）。

一个例外要说清：``conduct_post_op`` 的 ``abort`` 只往意图队列里放一条，但指挥线程消费它时会执行
``SafeRetract``（确认式退针，失败重试一次，见 ``conduct.director._abort``）。退针是停止类动作、外部面本来
就随时可做；卡片的 ``sub_skills`` 与说明照实写着它。
"""

from __future__ import annotations

from mast.i18n import tr
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from mast.api import direct_exec
from mast.api.ext.common import EXT_PREFIX, Caller, ExtError

logger = logging.getLogger(__name__)

#: 卡片与检索结果里的来源（给人读的 / 语言中立的代号）。不是「官方」：与桥接过来的
#: 非仪器 agent 工具（``origin_code: agent_tool``）同一个约定。
ORIGIN_LABEL = tr("仪器 agent 工具", "Instrument agent tool")
ORIGIN_CODE = "ic_agent_tool"
CATEGORY = "agent_tool"

#: 足迹：只读 MAST 自己的东西 / 写 MAST 自己的东西。两者都**不发控制器命令**。
READ = "mast-read"
WRITE = "mast-write"
FOOTPRINT_TEXT = {
    READ: tr("只读 MAST 自己的记录 / 知识库 / 文件，不发控制器命令",
             "Reads MAST's own records / knowledge base / files only; sends no controller commands"),
    WRITE: tr("写 MAST 自己的记录或状态（实验 / 样品作用域、针尖登记、扫描地图标记、计划、"
              "conduct 意图队列、订阅推荐），不发控制器命令",
              "Writes MAST's own records or state (experiment / sample scope, tip registration, scan-map "
              "markers, plans, the conduct intent queue, subscription recommendations); sends no "
              "controller commands"),
}

#: 开放的工具：名字 → (家族, 足迹)。与 :func:`build` 构造出的实物一一对应
#: （测试钉着：实物里多一个没分类的、或这里多一个实物没有的，都红）。
OFFERED: dict[str, tuple[str, str]] = {
    # ── 实验 / 样品（meta）──
    "list_experiments": ("meta", READ),
    "list_samples": ("meta", READ),
    "start_experiment": ("meta", WRITE),
    "end_experiment": ("meta", WRITE),
    "start_sample": ("meta", WRITE),
    "end_sample": ("meta", WRITE),
    "switch_experiment": ("meta", WRITE),
    "switch_sample": ("meta", WRITE),
    "clear_sample": ("meta", WRITE),
    "rename_experiment": ("meta", WRITE),
    "rename_sample": ("meta", WRITE),
    # ── 针尖登记（meta）：只读这两件；登记 / 修改 / 注销是操作员的事（OPERATOR_ONLY）──
    "get_current_tip": ("meta", READ),
    "list_tips": ("meta", READ),
    # ── 扫描文件（meta）──
    "get_latest_scan_info": ("meta", READ),
    "load_scan_file": ("meta", READ),
    # ── 知识与指导（meta）──
    "get_workflow_advice": ("meta", READ),
    "get_skill_guidance": ("meta", READ),
    "get_literature_parameters": ("meta", READ),
    "get_fault_diagnosis": ("meta", READ),
    "get_noise_reference": ("meta", READ),
    "get_measurement_template": ("meta", READ),
    "search_deep_reference": ("meta", READ),
    "read_reference_section": ("meta", READ),
    "query_knowledge": ("meta", READ),
    # ── 扫描地图 / 粗动地图（meta）──
    "get_map_analysis": ("meta", READ),
    "get_next_scan_position": ("meta", READ),
    "get_markers_near": ("meta", READ),
    "get_coarse_map": ("meta", READ),
    "mark_area_used": ("meta", WRITE),
    # ── 计划（meta）：执行就是 agent 自己的循环，这些只登记、批准与记进度 ──
    "plan_scan_batch": ("meta", WRITE),          # 默认把路线发布到扫描地图上
    "create_plan": ("meta", WRITE),
    "approve_plan": ("meta", WRITE),
    "list_plans": ("meta", READ),
    "get_plan_progress": ("meta", READ),
    "advance_plan": ("meta", WRITE),
    "pause_plan": ("meta", WRITE),
    "resume_plan": ("meta", WRITE),
    "show_plan_on_map": ("meta", WRITE),
    "clear_plan_on_map": ("meta", WRITE),
    # ── 文档 / 环境 / 技能市场 ──
    "load_document": ("document", READ),
    "list_documents": ("document", READ),
    "survey_environment": ("environment", READ),
    "search_skill_market": ("market", READ),
    "recommend_skill_subscription": ("market", WRITE),
    # ── conduct：读状态、试编译、叫停 / 收回 ──
    "conduct_status": ("conduct", READ),
    "conduct_list": ("conduct", READ),
    "conduct_compile_from_plan": ("conduct", READ),   # 试编译，不落库、不建任何东西
    "conduct_post_op": ("conduct", WRITE),            # 只收 CONDUCT_OPS_OFFERED
}

_ASK_OPERATOR = tr("请操作员在 MAST 界面上做（用 mast_ask_operator 请他）；外部 agent 看不见仪器旁发生了什么，"
                   "外部面不替操作员陈述它。",
                   "Ask the operator to do this in the MAST UI (use mast_ask_operator); an external agent "
                   "cannot see what happens at the instrument, and the external surface does not attest "
                   "it on the operator's behalf.")

#: 不开放的工具 → 理由（卡片与提交时原样回给调用方）。两类：把活交给内部 agent / 后台运行时；
#: 替操作员陈述仪器旁的物理事件（:data:`OPERATOR_ONLY`）。
EXCLUDED: dict[str, str] = {
    "record_coarse_move": (
        tr("record_coarse_move 补记一次在 Nanonis 里手动做的粗动：坐标代次加一，计划路线与撞针封锁一并清空"
           "（同一点撞针两次之后的封锁正靠这张表）。手动粗动发生在仪器旁 —— ",
           "record_coarse_move logs a coarse move made by hand in Nanonis: the coordinate generation is "
           "incremented and planned routes and tip-crash blocks are cleared (the block after two tip "
           "crashes at the same point relies on this table). A manual coarse move happens at the "
           "instrument - ") + _ASK_OPERATOR
        + tr("操作员在扫描地图页补记。经 MAST 做的粗动（RelocateCoarseXY、MotorMove）自动记录，不用补。",
             " The operator logs it on the scan-map page. Coarse moves made through MAST "
             "(RelocateCoarseXY, MotorMove) are recorded automatically and need no entry.")),
    "register_tip": (
        tr("register_tip 登记一根新针：清掉绑在上一根针上的学习量（qPlus 撞针判据的振幅基线、进针的 dI/dV "
           "标定），修针技能的缺省剂量也按新登记的材料与形态查表。换针发生在仪器旁 —— ",
           "register_tip registers a new tip: it clears what was learned on the previous tip (the qPlus "
           "tip-crash amplitude baseline, the approach dI/dV calibration), and the default dose of the "
           "tip-conditioning skills is looked up from the newly registered material and shape. A tip "
           "change happens at the instrument - ") + _ASK_OPERATOR
        + tr("操作员在右栏针尖卡片里登记。读当前针尖用 get_current_tip / list_tips。",
             " The operator registers it in the tip card in the right column. To read the current tip use "
             "get_current_tip / list_tips.")),
    "update_tip": (
        tr("update_tip 改当前针尖的登记（材料、制备、形态……），修针技能的缺省剂量随之改变 —— ",
           "update_tip edits the current tip's registration (material, preparation, shape, ...); the "
           "default dose of the tip-conditioning skills changes with it - ") + _ASK_OPERATOR
        + tr("操作员在右栏针尖卡片里改。", " The operator edits it in the tip card in the right column.")),
    "remove_current_tip": (
        tr("remove_current_tip 注销当前针尖，清掉绑在它上面的学习量（含 qPlus 撞针判据的振幅基线）—— ",
           "remove_current_tip unregisters the current tip and clears what was learned on it (including "
           "the qPlus tip-crash amplitude baseline) - ")
        + _ASK_OPERATOR + tr("操作员在右栏针尖卡片里做。",
                             " The operator does it in the tip card in the right column.")),
    "spawn_background_task": (
        tr("spawn_background_task 把一个子任务交给后台的内部 agent 去跑 —— 外部面不提供把活交给内部 "
           "agent：一切活由外部 agent 自己做（MAST 7.0.1 的决定）。文献、分析这些能力本身都能直接 "
           "mast_run（例如 deep_read_papers、search_fulltext）。",
           "spawn_background_task hands a sub-task to a background internal agent - the external surface "
           "does not hand work to internal agents: the external agent does all the work itself (a MAST "
           "7.0.1 decision). The literature and analysis capabilities can be called directly with "
           "mast_run (for example deep_read_papers, search_fulltext).")),
    "request_activation": (
        tr("request_activation 是建议编排器激活另一个内部 agent 接着干（交任务）；它的效果是编排器图里的 "
           "routing_hints，在外部面上没有意义。外部 agent 自己做那件事。",
           "request_activation suggests that the orchestrator activate another internal agent to carry on "
           "(handing off work); its effect is routing_hints in the orchestrator graph, which is "
           "meaningless on the external surface. The external agent does that work itself.")),
    "conduct_create": (
        tr("conduct_create 建一份交给 conduct 指挥线程执行的多步实验（交任务）—— 外部面不提供。外部 agent "
           "自己按计划逐步执行（create_plan / advance_plan 记进度）。",
           "conduct_create creates a multi-step experiment handed to the conduct commander thread "
           "(handing off work) - not offered on the external surface. The external agent executes the "
           "plan step by step itself (record progress with create_plan / advance_plan).")),
    "conduct_approve": (
        tr("conduct_approve 批准指挥线程开跑一份 conduct（交任务）—— 外部面不提供。",
           "conduct_approve approves the commander thread to start running a conduct (handing off work) "
           "- not offered on the external surface.")),
}

#: 不开放的第二类：替操作员陈述仪器旁的物理事件，登记它会清掉安全联锁或学习量（见模块 docstring）。
#: 简报里提到它们时，外部 agent 读到的是「请操作员做」而不是 ``mast_run(...)``（``overview._ext_tool_calls``）。
OPERATOR_ONLY: frozenset[str] = frozenset({
    "record_coarse_move", "register_tip", "update_tip", "remove_current_tip",
})

#: ``conduct_post_op`` 在外部面上只接受这几种 op：叫停与收回不是交任务。
CONDUCT_OPS_OFFERED: tuple[str, ...] = ("pause", "abort", "takeover")

#: 改作用域（当前实验 / 样品）的工具：仪器被占用时拒绝 —— 与 ``POST /scope`` 同一条（在跑的动作会被
#: 记到新的实验 / 样品下）。占用时确实要切，用 ``mast_scope(force=true)``。
SCOPE_CHANGING: frozenset[str] = frozenset({
    "start_experiment", "end_experiment", "start_sample", "end_sample",
    "switch_experiment", "switch_sample", "clear_sample",
})

#: 带文件路径参数的工具：路径须在数据搜索目录与实验根目录之内（与 ``/data/*`` 同一道判据）。
PATH_ARGS: dict[str, tuple[str, ...]] = {"load_scan_file": ("path",)}

#: 卡片的前置条件与描述末尾那句「外部面上的不同」。
_EXT_NOTES: dict[str, str] = {
    "conduct_post_op": tr("（外部面：op 只接受 pause / abort / takeover。start / resume 是把一份 conduct "
                          "交给指挥线程去跑，属于交任务，外部面不提供。abort 会让指挥线程退针：执行 SafeRetract，"
                          "失败重试一次，等待态里针已经退了的直接中止。）",
                          "(External surface: op accepts only pause / abort / takeover. start / resume hand a "
                          "conduct to the commander thread to run, which is handing off work and is not "
                          "offered. abort makes the commander thread withdraw the tip: it runs SafeRetract, "
                          "retries once on failure, and aborts directly if the tip is already withdrawn "
                          "while waiting.)"),
    "recommend_skill_subscription": tr("（外部 agent：外部面不受订阅限制，任何技能都能直接 mast_run；"
                                       "这条推荐只影响内部 agent 的工具表，由操作员决定接不接受。）",
                                       "(External agent: the external surface is not limited by "
                                       "subscriptions; any skill can be run directly with mast_run. This "
                                       "recommendation only affects the internal agents' tool table, and "
                                       "the operator decides whether to accept it.)"),
    "load_scan_file": tr("（外部面：path 须在数据搜索目录或实验根目录之内，与 mast_fetch 同一道判据。）",
                         "(External surface: path must be inside the data search directories or the "
                         "experiment root, the same check as mast_fetch.)"),
}
for _n in SCOPE_CHANGING:
    _EXT_NOTES[_n] = tr("（外部面：仪器被占用时拒绝，与 mast_scope 同一条 —— 在跑的动作会被记到新的作用域下；"
                        "确需在占用时切，用 mast_scope(force=true)。）",
                        "(External surface: refused while the instrument is busy, the same rule as "
                        "mast_scope - the running action would be recorded under the new scope; if you "
                        "must switch while busy, use mast_scope(force=true).)")

#: 工具自己不发命令、但它交出去的活会跑这些技能（卡片的 ``sub_skills``）。安全审查发现
#: conduct_post_op 的卡片原来写着「不发控制器命令」，而 abort 会让指挥线程执行 SafeRetract。
_SUB_SKILLS: dict[str, tuple[str, ...]] = {"conduct_post_op": ("SafeRetract",)}
_SUB_SKILL_REASONS: dict[str, str] = {
    "conduct_post_op": tr("op=abort 时指挥线程执行 SafeRetract（确认式退针，失败重试一次）；"
                          "pause / takeover 只停指挥线程，不发命令",
                          "With op=abort the commander thread runs SafeRetract (confirmed withdraw, one "
                          "retry on failure); pause / takeover only stop the commander thread and send no "
                          "commands"),
}

_PRECONDITIONS: dict[str, tuple[str, ...]] = {
    "conduct_post_op": ("op ∈ pause / abort / takeover",),
    "load_scan_file": (tr("path 在数据搜索目录或实验根目录之内",
                          "path is inside the data search directories or the experiment root"),),
}
for _n in SCOPE_CHANGING:
    _PRECONDITIONS[_n] = (tr("仪器空闲（被占用时拒绝，与 mast_scope 同一条）",
                             "Instrument idle (refused while busy, the same rule as mast_scope)"),)


# ─────────────────────────────────────────────────────────────────────
# 构造
# ─────────────────────────────────────────────────────────────────────

def _meta(runtime: Any, who: str) -> list:
    from mast.agents._shared.meta_tools import make_meta_tools

    source = getattr(runtime, "_meta_tool_context", None) if runtime is not None else None

    def provider() -> dict:
        try:
            d = dict(source() or {}) if callable(source) else {}
        except Exception as exc:  # noqa: BLE001 — 每个工具自己会说「不可用」
            logger.debug("ext agent-tools: meta provider 失败(%s)", exc)
            d = {}
        # spawn_background_task 的后端：即使排除名单被绕过，这里也够不到（7.0.1：不把活交给内部 agent）
        d.pop("spawn_background", None)
        return d

    return make_meta_tools(provider)


def _documents(runtime: Any, who: str) -> list:
    from mast.agents._shared.document_tools import DOCUMENT_TOOLS

    return list(DOCUMENT_TOOLS)


def _environment(runtime: Any, who: str) -> list:
    from mast.agents._shared.environment_tools import ENVIRONMENT_TOOLS

    return list(ENVIRONMENT_TOOLS)


def _market(runtime: Any, who: str) -> list:
    from mast.agents._shared.market_tools import make_market_tools

    return make_market_tools(who)


def _conduct(runtime: Any, who: str) -> list:
    from mast.agents._shared.conduct_tools import make_conduct_tools

    def context() -> dict:
        source = getattr(runtime, "_conduct_tool_context", None) if runtime is not None else None
        try:
            return dict(source() or {}) if callable(source) else {}
        except Exception as exc:  # noqa: BLE001 — 编译类工具自己会说「plan 库没接上」
            logger.debug("ext agent-tools: conduct context 失败(%s)", exc)
            return {}

    return make_conduct_tools(who, context=context)


#: (家族, 构造函数)。构造函数拿活 runtime（可为 None：只要描述时）与写进审计署名的名字。
FAMILIES: tuple[tuple[str, Callable[[Any, str], list]], ...] = (
    ("meta", _meta), ("document", _documents), ("environment", _environment),
    ("market", _market), ("conduct", _conduct),
)


def build(runtime: Any, who: str) -> dict[str, tuple[str, Any]]:
    """``{名字: (家族, 工具对象)}`` —— 各族构造出来的**全部**工具（开放与否由调用方判）。永不抛：
    一族导入失败只让那一族缺席（并记日志）。"""
    out: dict[str, tuple[str, Any]] = {}
    for fam, builder in FAMILIES:
        try:
            tools = builder(runtime, who)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ext agent-tools: %s 这一族建不出来（%s）", fam, exc)
            continue
        for t in tools or ():
            name = str(getattr(t, "name", "") or "")
            if name and name not in out:
                out[name] = (fam, t)
    return out


# ─────────────────────────────────────────────────────────────────────
# 描述（检索与卡片用；不碰 runtime）
# ─────────────────────────────────────────────────────────────────────

_JSON_TYPES = {"integer": "int", "number": "float", "string": "str", "boolean": "bool",
               "array": "list", "object": "dict"}


def _metre_names() -> frozenset[str]:
    try:
        from mast.agents._shared.meta_tools import _METRE_POSITION_ARGS, _METRE_SIZE_ARGS

        return frozenset(_METRE_POSITION_ARGS) | frozenset(_METRE_SIZE_ARGS)
    except Exception:  # noqa: BLE001
        return frozenset()


def _json_type(p: dict) -> str:
    """JSON Schema 片段 → 技能卡的类型词（``float|str``、``list[PlanStep]``）。"""
    if "$ref" in p:
        return str(p["$ref"]).rsplit("/", 1)[-1]
    if isinstance(p.get("anyOf"), list):
        kinds = [_json_type(x) for x in p["anyOf"] if isinstance(x, dict) and x.get("type") != "null"]
        return "|".join(dict.fromkeys(k for k in kinds if k)) or "any"
    t = p.get("type")
    if t == "array":
        items = p.get("items") if isinstance(p.get("items"), dict) else {}
        inner = _json_type(items) if items else ""
        return f"list[{inner}]" if inner else "list"
    return _JSON_TYPES.get(str(t), str(t or "any"))


def _unit(name: str, metre: frozenset[str]) -> str | None:
    if name in metre:
        return "m"
    for suffix, unit in (("_nm", "nm"), ("_hz", "Hz"), ("_mm", "mm"), ("_minutes", "min")):
        if name.endswith(suffix):
            return unit
    return None


def _parameters(tool: Any) -> list[dict]:
    """``tool_call_schema``（不含 ``InjectedToolCallId`` 这类注入参数）→ 技能卡的参数行。"""
    try:
        schema = tool.tool_call_schema.model_json_schema()
    except Exception:  # noqa: BLE001
        return []
    props = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    metre = _metre_names()
    out = []
    for name, p in props.items():
        if not isinstance(p, dict):
            continue
        out.append({
            "name": str(name), "type": _json_type(p), "unit": _unit(str(name), metre),
            "required": name in required, "default": p.get("default"),
            "min": p.get("minimum"), "max": p.get("maximum"),
            "allowed": list(p["enum"]) if isinstance(p.get("enum"), list) else None,
            "description": str(p.get("description") or "")[:200],
        })
    return out


def _description(tool: Any, name: str) -> str:
    text = str(getattr(tool, "description", "") or "").strip()
    note = _EXT_NOTES.get(name)
    return f"{text}\n\n{note}" if note else text


_DESCR_LOCK = threading.Lock()
_DESCR: dict[str, dict] | None = None


def describe() -> dict[str, dict]:
    """开放工具的描述：``{名字: {name, family, footprint, description, parameters}}``。

    在一个空 provider 上构造一次（不碰 runtime，不发任何命令）并缓存；有一族没建出来时不缓存，下次重试。"""
    global _DESCR
    with _DESCR_LOCK:
        if _DESCR is not None:
            return _DESCR
        built = build(None, f"{EXT_PREFIX}catalog")
        out: dict[str, dict] = {}
        for name, (fam, footprint) in OFFERED.items():
            hit = built.get(name)
            if hit is None:
                continue
            out[name] = {"name": name, "family": fam, "footprint": footprint,
                         "description": _description(hit[1], name), "parameters": _parameters(hit[1])}
        if len(out) == len(OFFERED):
            _DESCR = out
        return out


def _station_hides(name: str) -> bool:
    """Optical station profile: the tools that read STM state (tip registry, scan files,
    scan / coarse maps) are withheld - ``station_face.STM_ONLY_AGENT_TOOLS``. Never true
    on an STM station."""
    try:
        from mast.skills.station_face import is_stm_only_agent_tool

        return is_stm_only_agent_tool(name)
    except Exception:  # noqa: BLE001
        return False


def is_offered(name: str) -> bool:
    n = str(name or "")
    return n in OFFERED and not _station_hides(n)


def excluded_reason(name: str) -> str | None:
    return EXCLUDED.get(str(name or ""))


def search(terms: list[str]) -> list[tuple[int, str, list[str]]]:
    """``(分数, 名字, 命中依据)`` —— 计分与 ``/skills/search`` 对技能的一样（名字、标签、参数名、描述）。"""
    out = []
    for name, d in describe().items():
        if _station_hides(name):
            continue
        lname, desc = name.lower(), d["description"].lower()
        pnames = [p["name"].lower() for p in d["parameters"]]
        tags = [d["family"], CATEGORY]
        score, why = 0, []
        for t in terms:
            if t == lname:
                score += 100
                why.append("name")
            elif t in lname:
                score += 40
                why.append("name")
            if any(t in tg for tg in tags):
                score += 20
                why.append("tag")
            if any(t in pn for pn in pnames):
                score += 10
                why.append("param")
            if t in desc:
                score += 15
                why.append("description")
        if score:
            out.append((score, name, list(dict.fromkeys(why))))
    return out


def card(name: str) -> dict | None:
    """一张与技能卡同形状的卡（再加 ``family`` 与 ``runs_as``）；不是开放工具 → None。
    ``duration`` 由调用方填（实测耗时从 v2 记录里按名字查，与技能卡同一个查询）。"""
    d = describe().get(name)
    if d is None or _station_hides(name):
        return None
    fam, footprint = OFFERED[name]
    metre = _metre_names()
    reasons = [FOOTPRINT_TEXT[footprint]]
    verbs: list[str] = []
    if fam == "meta":
        verbs = ["Util_SessionPathGet"]
        reasons.append(tr("每次调用经 provider 刷新一次 Nanonis 保存目录（Util_SessionPathGet，15 s 缓存，"
                          "只读）—— 与仪器控制 agent 调它时相同",
                          "Each call refreshes the Nanonis save directory through the provider "
                          "(Util_SessionPathGet, 15 s cache, read-only) - the same as when the instrument "
                          "control agent calls it"))
    sub_skills = list(_SUB_SKILLS.get(name, ()))
    runs_as = "plain job: mast_run without a token (POST /jobs); it never drives the instrument"
    if sub_skills:
        reasons.append(_SUB_SKILL_REASONS[name])
        runs_as = ("plain job: mast_run without a token (POST /jobs); the tool itself sends no command, "
                   f"but the work it hands on runs {', '.join(sub_skills)}")
    return {
        "name": name, "origin": ORIGIN_LABEL, "official": False, "description": d["description"],
        "safety_level": "auto", "capabilities": [], "preconditions": list(_PRECONDITIONS.get(name, ())),
        "estimated_duration_s": None, "parameters": d["parameters"],
        "origin_code": ORIGIN_CODE, "category": CATEGORY, "tags": [fam, CATEGORY],
        "composition_level": 0, "family": fam,
        "footprint": footprint, "footprint_basis": "declared", "footprint_reasons": reasons,
        "verbs": verbs, "verbs_unknown": False, "sub_skills": sub_skills,
        "takes_instrument_token": False, "requires_sample": False,
        "si_params": {p["name"]: "prefix_optional" for p in d["parameters"] if p["name"] in metre},
        "tool_face": {"disabled": False, "reason": ""},
        "runs_as": runs_as,
    }


# ─────────────────────────────────────────────────────────────────────
# 执行
# ─────────────────────────────────────────────────────────────────────

@dataclass
class ToolRun:
    """一次工具调用的结果（作业把它变成 ``result`` / ``recorded``）。"""

    name: str
    params: dict
    success: bool = False
    summary: str = ""
    error: str = ""
    data: dict = field(default_factory=dict)
    #: 门口拒绝的来源：``instrument_busy`` / ``path_not_allowed`` / ``not_offered`` /
    #: ``invalid_params``（以及 ``confine`` 的其它错误码）；没被拒为 None
    refused_by: str | None = None
    recorded: dict = field(default_factory=dict)
    elapsed_s: float = 0.0


def _door(ctx: Any, name: str, params: dict) -> tuple[str, str] | None:
    """外部面多守的那几道（``(拒绝码, 说明)`` 或 None）。可能改写 ``params``（路径解析成绝对路径）。"""
    if name in SCOPE_CHANGING:
        try:
            from mast.core.instrument_lock import instrument_lock

            snap = instrument_lock().snapshot() or {}
        except Exception:  # noqa: BLE001 — 读不到占用就不按它拒（与 /status 同向）
            snap = {}
        if snap:
            return ("instrument_busy",
                    tr(f"仪器正被 {snap.get('owner')} 占用（{snap.get('skill')}）—— 现在改作用域，它接下来的"
                       "动作会记到新的实验 / 样品下。等它结束，或用 mast_scope(force=true)。",
                       f"The instrument is in use by {snap.get('owner')} ({snap.get('skill')}) - changing "
                       "the scope now would record its next actions under the new experiment / sample. "
                       "Wait for it to finish, or use mast_scope(force=true)."))
    for arg in PATH_ARGS.get(name, ()):
        raw = params.get(arg)
        if raw in (None, ""):
            continue
        from mast.api.ext.data import confine

        shim = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=ctx)))
        try:
            params[arg] = str(confine(shim, str(raw)))
        except ExtError as exc:
            return (exc.code, exc.detail or exc.code)
    if name == "conduct_post_op":
        op = str(params.get("op") or "")
        if op not in CONDUCT_OPS_OFFERED:
            return ("not_offered",
                    tr(f"conduct_post_op 在外部面上只接受 op={'/'.join(CONDUCT_OPS_OFFERED)}；"
                       f"{op or '(空)'} —— start / resume 是把一份 conduct 交给指挥线程去跑（交任务），"
                       "外部面不提供。",
                       f"conduct_post_op accepts only op={'/'.join(CONDUCT_OPS_OFFERED)} on the external "
                       f"surface; {op or '(empty)'} - start / resume hand a conduct to the commander "
                       "thread to run (handing off work) and are not offered."))
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, str):
                parts.append(blk)
            elif isinstance(blk, dict) and isinstance(blk.get("text"), str):
                parts.append(blk["text"])
        return "\n".join(parts)
    return str(content)


def _text_of(out: Any) -> str:
    """工具的回答 → 文字。ToolCall 形式调用时，普通返回包成 ``ToolMessage``；返回 ``Command`` 的
    （``ArtifactToolReturn`` 这类「摘要 + 状态写」）原样回来 —— 摘要在 ``_summary`` 或它的消息里。"""
    if isinstance(out, str):
        return out
    content = getattr(out, "content", None)
    if content is not None:
        return _content_text(content)
    summary = getattr(out, "_summary", None)
    if isinstance(summary, str):
        return summary
    update = getattr(out, "update", None)
    if isinstance(update, dict):
        for msg in reversed(list(update.get("messages") or ())):
            c = getattr(msg, "content", None)
            if c is not None:
                return _content_text(c)
    return str(out)


#: 与 ``tool_skills._ERROR_PREFIX`` 同一个判据（「xxx failed: …」），再加这几族工具自己的失败措辞 ——
#: 只看首行、且「失败」后面要紧跟冒号或括号：「（这是确认过的空，不是查询失败。）」不算。
_ERROR_PREFIX = re.compile(r"^\s*\w[\w .]*?(failed|error)[:：]", re.IGNORECASE)
_FAIL_WORDS = re.compile(r"不可用|读不到|找不到|打不开|失败[：:（(]")


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _verdict(text: str) -> tuple[bool, str]:
    """``(成功?, 错误说明)``。JSON 回答按它自己的 ``success`` / ``ok``；纯文本按首行的失败措辞保守判。
    原文总在 ``data.text`` 里 —— 判据只决定作业状态，不替调用方读回答。"""
    j = _parse_json(text)
    if isinstance(j, dict):
        for key in ("success", "ok"):
            if key in j and j[key] is False:
                return False, str(j.get("error") or j.get("reason") or text)[:300]
        return True, ""
    if isinstance(j, list):
        return True, ""
    lines = str(text or "").strip().splitlines()
    first = lines[0] if lines else ""
    if _ERROR_PREFIX.match(first) or _FAIL_WORDS.search(first):
        return False, first[:300]
    return True, ""


def _first_line(text: str, limit: int = 200) -> str:
    lines = str(text or "").strip().splitlines()
    return (lines[0] if lines else "")[:limit]


#: 进记录库的回答截到这么长（v1 / v2 行里放的是摘要，不是全文；全文在作业结果里）。
_RECORD_TEXT_CAP = 4000


def _record(runtime: Any, caller: Caller, run: ToolRun, job_id: str) -> dict:
    rec = getattr(runtime, "_record_direct_skill_call", None) if runtime is not None else None
    if not callable(rec):
        return {}
    from mast.core.types import SkillResult

    data = {"text": str(run.data.get("text") or "")[:_RECORD_TEXT_CAP], "agent_tool": True}
    if run.refused_by:
        data["refused_by"] = run.refused_by
    result = SkillResult(skill_name=run.name, success=run.success, error=run.error,
                         summary=run.summary, data=data)
    payload = direct_exec.record_payload(
        run.name, run.params, None, result, duration_ms=int(run.elapsed_s * 1000),
        agent_id=caller.agent_id, thread_id=caller.thread_id, tool_call_id=job_id,
        context=caller.thread_id, approval_source=direct_exec.APPROVAL_LLM)
    try:
        return dict(rec(payload) or {})
    except Exception as exc:  # noqa: BLE001 — 记账失败绝不能反噬已经发生的动作
        logger.warning("ext agent-tools: %s 入账失败: %s", run.name, exc)
        return {"v1": False, "v2_action_id": None, "error": str(exc)[:200]}


def run_and_record(ctx: Any, caller: Caller, name: str, params: dict | None, *,
                   job_id: str) -> ToolRun:
    """跑一个开放的工具并入账。**永不抛。** 门口拒绝的也入账（与直调技能在门口被拒同一做法）。"""
    from pydantic import ValidationError

    t0 = time.monotonic()
    run = ToolRun(name=str(name), params=dict(params or {}))
    runtime = direct_exec.live_runtime(ctx)
    if not is_offered(run.name):
        run.refused_by = "not_offered"
        run.error = excluded_reason(run.name) or tr(f"{run.name} 不是开放给外部 agent 的工具", f"{run.name} is not a tool offered to external agents")
    else:
        refusal = _door(ctx, run.name, run.params)
        if refusal is not None:
            run.refused_by, run.error = refusal
        else:
            try:
                hit = build(runtime, caller.agent_id).get(run.name)
                if hit is None:
                    run.error = tr(f"{run.name} 这一族工具在这台 MAST 上建不出来（导入失败，见服务日志）", f"The tool family of {run.name} cannot be built on this MAST (import failed, see the service log)")
                else:
                    out = hit[1].invoke({"args": dict(run.params), "name": run.name,
                                         "type": "tool_call", "id": job_id})
                    text = _text_of(out)
                    run.success, run.error = _verdict(text)
                    parsed = _parse_json(text)
                    run.data = {"text": text, **({"json": parsed} if parsed is not None else {})}
                    run.summary = _first_line(text)
            except ValidationError as exc:
                run.refused_by = "invalid_params"
                run.error = tr(f"参数不合法：{exc}", f"Invalid parameters: {exc}")[:1000]
            except Exception as exc:  # noqa: BLE001 — 工具崩了也要如实回、如实记
                run.error = f"{type(exc).__name__}: {exc}"[:1000]
    run.elapsed_s = time.monotonic() - t0
    run.recorded = _record(runtime, caller, run, job_id)
    return run


#: :meth:`JobManager.door` 认出一个开放工具时的返回值（它不是注册表里的技能类）。
AGENT_TOOL = object()


__all__ = [
    "AGENT_TOOL", "CATEGORY", "CONDUCT_OPS_OFFERED", "EXCLUDED", "FAMILIES", "OFFERED",
    "OPERATOR_ONLY", "ORIGIN_CODE", "ORIGIN_LABEL", "PATH_ARGS", "READ", "SCOPE_CHANGING",
    "ToolRun", "WRITE",
    "build", "card", "describe", "excluded_reason", "is_offered", "run_and_record", "search",
]
