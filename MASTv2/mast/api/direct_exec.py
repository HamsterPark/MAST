"""不经 LLM 直接跑一个技能 —— 技能直调 API 与外部 agent 网关共用的内核。

## 为什么单独成一个模块

``POST /api/skills/{name}/execute`` 最初自己拼一个 ``ExecutionContext`` 就调
``run()``。直接执行仍须统一处理 agent 工具边界上的四项职责：

1. **入账**。agent 路径的每次技能调用由 ``CoreRuntime._skill_trace_recorder`` 记进
   v1/v2 ``actions``、登记 ``scan_files``、把文件归档进实验文件夹；直调路径只留下
   一条地图标记，不能替代完整的技能执行记录。
2. **run_id**。composite 的进度 sidecar 按 ``(名字, run_id)`` 分键
   （``skills/composite/graph_executor._sidecar_path``）；空 run_id 落到只按名字的
   旧文件，一次被取消或失败的 composite 会被下一次同名直调续跑跳步。
3. **样品门控**。门口判一次、放行后让子步继承 —— 与 ``wrap_skill`` 同一语义；否则
   每个子步都重判一次，且 ``ExecutionContext.run`` 本身从不置 ``_scope_admitted``。
4. **SI 字符串**。``"5n"`` 这种写法在 agent 路径上由 ``_coerce_si_params`` 还原成
   float，直调路径上没有 —— 而 live-state 渲染块教的正是这种写法。

这里把四件事收在一处，老端点与外部网关（``mast.api.ext``）都走它。**执行路径仍然
只有 ``ExecutionContext.run``**：安全闸门、仪器仲裁、中止、状态回写都在那里面。

## human 节点（只在光学站）

**光学站上**，组合技能里的 ``human`` 节点在这条路径上不再被拒。``run_and_record`` 在 ``ec.run`` 外面
开一个 ``mast.operator_hitl.human_node.direct_scope``（带着这次执行的 ``check_abort``）；
``skills.composite.interpreter.human_channel`` 看到它，就把节点变成一条结构化操作员提示，阻塞等
操作员在网页 / 启动器弹窗里答 —— 有上限（缺省 30 min，至多 4 h）、随作业取消而取消。超时、被关闭、
被取消都以明确的失败结束。作业在等的时候 ``waiting_operator``（含 prompt_id）出现在作业视图里
（``api/ext/jobs.py``）；组合技能一直持有仪器令牌（下一步要的是前几步留下的仪器状态），停止类技能
（``instrument_lock.BYPASS_NAMES``）照常能跑。LangGraph（agent）路径不设作用域，仍然是 ``interrupt()``。

**STM 站上**作用域照常开（作业归属不丢），但 ``human_nodes`` 是 False（``_waits_for_human_nodes``，读
``mast.station``）：``human_channel`` 走老路径，节点立刻明确失败（没有人工决策通道），不问谁、不弹窗、
不替一个可能不在的人占着令牌 —— 和没有操作员提示之前一模一样。``needs_human_node`` 的来源不变：
human_channel 以外的 ``GraphInterrupt``、技能自己起的线程里的 human 节点。

## 刻意不做的事

* 不取仪器令牌、不发任何 Nanonis 命令 —— 那是 ``ExecutionContext.run`` 的事。
* **不走完整的 ``_skill_trace_recorder``**。直调的顶层技能经 ``run()``，而 ``run()``
  已经通过 ``marker_sink`` 记了地图标记；再走那个 recorder 会同一动作记两条标记
  （粗动回调触发两次、定不了位的撞针计两次）。它还会把动作记进当前群聊 run 的
  核对台账（按 ``_orch_run_id``）与训练轨迹（写死 ``agent_id="IC"``）——对一个与
  群聊无关的外部调用，两者都是错的归属。入账只走
  ``CoreRuntime._record_direct_skill_call``，它只写事实表。
"""

from __future__ import annotations

from mast.i18n import tr
import contextlib
import copy
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: v1 ``actions.approval_source`` 是**出处**词表（auto / llm / human），不是闸门。
#: 外部 agent 的决定出自一个模型，记 ``llm``；没声明身份的程序化直调记 ``auto``。
#: 注意与 ``ExecutionContext._approval_source`` 区分：那个是闸门，这条路上永远
#: 保持非 ``human``（SAFE/SEMI 模式闸与五条硬闸都写着 ``!= "human"``）。
APPROVAL_AUTO = "auto"
APPROVAL_LLM = "llm"


def new_run_id(prefix: str = "direct") -> str:
    """每次直调一个新的 run id —— composite sidecar 的分键就靠它。"""
    return f"{prefix}-{secrets.token_hex(6)}"


_SLUG_BAD = re.compile(r"[^\w.\-]+")


def actor_slug(raw: Any, limit: int = 48) -> str:
    """调用方自报的名字 → 一个短标识（小写，字母数字与 ``._-``，允许中文字符）。

    只用于**归属**：记进 ``actions.context`` / v2 ``agent_id``、仪器令牌的 owner
    文案、笔记路径。它不是认证，也永远不作为拒绝请求的理由。空 → ``""``。
    """
    s = _SLUG_BAD.sub("-", str(raw or "").strip().lower()).strip("-.")
    return s[:limit].strip("-.")


def live_runtime(ctx: Any) -> Any:
    """API 上下文里挂着的活 ``CoreRuntime``（三个别名指向同一个对象）。"""
    return (getattr(ctx, "live_app", None) or getattr(ctx, "app", None)
            or getattr(ctx, "runtime", None))


# ─────────────────────────────────────────────────────────────────────
# ExecutionContext 的唯一构造点（仪器入口清单测试登记的是这个文件）
# ─────────────────────────────────────────────────────────────────────

def build_context(ctx: Any, *, owner: str, extra_aborts: Iterable[Any] = (),
                  run_id: str | None = None) -> "tuple[Any, list[str]]":
    """建一个直调用的 ``ExecutionContext``。返回 ``(ec, missing)``。

    缺什么就说缺什么（``connection_pool`` / ``state`` / ``skill_registry``），
    不报成笼统的「没连仪器」：状态对象缺失与硬件连接缺失应分别诊断。

    中止取并集：进程级的 ``_orch_abort``（急停 / E_STOP / 环境告警 / 群聊停止）
    加上调用方自己的事件（外部作业的取消）。任何一个置位，技能在下一次查询处停。
    """
    pool = getattr(ctx, "connection_pool", None)
    state = getattr(ctx, "state", None) or getattr(ctx, "instrument_state", None)
    registry = getattr(ctx, "skill_registry", None) or getattr(ctx, "registry", None)
    missing = [n for n, v in (("connection_pool", pool), ("state", state),
                              ("skill_registry", registry)) if v is None]
    if missing:
        return None, missing
    try:
        from mast.core.execution_context import ExecutionContext

        app = live_runtime(ctx)
        aborts = [e for e in (getattr(app, "_orch_abort", None), *tuple(extra_aborts))
                  if e is not None]
        ec = ExecutionContext(pool=pool, state=state, registry=registry,
                              abort_event=aborts or None, owner=owner,
                              run_id=run_id or new_run_id())
        # composite 子步骤的地图标记走这条 sink —— 与两条 agent 路径同一个记录器。
        # 直调的**顶层**技能也经 run()，所以顶层标记同样由它记（见模块 docstring）。
        if app is not None:
            try:
                from mast.core.runtime import _attach_marker_sink

                _attach_marker_sink(ec, app)
            except Exception:  # noqa: BLE001 — 记地图绝不影响执行
                pass
        return ec, []
    except Exception as exc:  # noqa: BLE001
        logger.warning("direct-exec: ExecutionContext 构建失败: %s", exc)
        return None, ["execution_context"]


# ─────────────────────────────────────────────────────────────────────
# 跑一次 + 入账
# ─────────────────────────────────────────────────────────────────────

@dataclass
class DirectRun:
    """一次直调的结果。``result`` 是技能自己的 ``SkillResult``（或同形状对象）。"""

    skill: str
    params: dict
    result: Any = None
    elapsed_s: float = 0.0
    #: 门口或结果里能**结构化**说出的拒绝来源；说不出的留 None，看 error 文本。
    #: ``si_parse`` / ``sample_gate`` / ``needs_human_node`` / ``busy``
    refused_by: str | None = None
    #: ``{"v1": bool, "v2_action_id": str|None, "experiment_id": ..., "sample_id": ...}``；
    #: 没有活 runtime（测试、独立开发模式）时为空 dict。
    recorded: dict = field(default_factory=dict)
    #: 执行结束时中止事件仍置位 ⇒ ``{"set": True, "reason": "..."}``（原因可能为空串）
    abort: dict | None = None

    @property
    def success(self) -> bool:
        return bool(getattr(self.result, "success", False))

    @property
    def error(self) -> str:
        return str(getattr(self.result, "error", "") or "")

    @property
    def summary(self) -> str:
        return str(getattr(self.result, "summary", "") or "")

    @property
    def data(self) -> dict:
        d = getattr(self.result, "data", None)
        if isinstance(d, dict):
            return d
        return {} if d is None else {"value": d}

    @property
    def nanonis_calls(self) -> int:
        return len(getattr(self.result, "nanonis_calls", None) or [])

    @property
    def busy_holder(self) -> dict | None:
        if self.refused_by != "busy":
            return None
        h = self.data.get("holder")
        return dict(h) if isinstance(h, dict) else {}


#: 要回执（``recorded`` / ``v2_action_id``）的调用方最多等入账多久（秒）。库正常时一次入账
#: 是毫秒级，回执与改动前完全一样；库被锁时等到这里就带着 ``pending: True`` 先回去，入账在
#: 后台写入器里照写（见 ``mast.logging.record_writer``）。不要回执的调用方传 ``0``。
DEFAULT_RECORD_WAIT_S = 2.0

#: 后台写入时单条记录撞锁的重试预算（秒），见 ``CoreRuntime._record_direct_skill_call``。
RECORD_LOCK_RETRY_S = 60.0


def run_and_record(ec: Any, skill: str, params: dict | None = None, *,
                   runtime: Any = None, agent_id: str | None = None,
                   thread_id: str | None = None, tool_call_id: str | None = None,
                   context: str = "", approval_source: str = APPROVAL_AUTO,
                   record_wait_s: float | None = None) -> DirectRun:
    """在 *ec* 上跑 *skill*，然后把结果记进实验记录。**永不抛。**

    ``agent_id`` / ``thread_id`` 只在给了的时候写进入账载荷 —— 不给就保持
    ``_record_v2_action`` 原来的缺省（``instrument_control`` / 当前活动线程）。
    ``context`` 写进 v1 ``actions.context``（外部身份的落点）。

    ``record_wait_s``：入账交给后台写入器之后，这里最多等它多久（默认
    :data:`DEFAULT_RECORD_WAIT_S`）。``0`` = 一刻不等（技能直调 API：它的回执里本来就没有
    ``recorded``）。等不到 ⇒ ``run.recorded`` 是 ``{"pending": True, "v1": None, ...}``——
    「还没写完」，不是「没写进去」。曾出现：入账在请求线程上同步写，实验库
    被锁时一次 ``GetSignalValues`` 的回执会卡很久，而仪器命令早已执行完。
    """
    params = dict(params or {})
    t0 = time.monotonic()
    meta = _metadata_of(ec, skill)
    wait_s = DEFAULT_RECORD_WAIT_S if record_wait_s is None else max(0.0, float(record_wait_s))

    # 1. SI 字符串还原（与 agent 路径同一个函数）。真 float 原样放行。
    if meta is not None:
        si_errors: list[str] = []
        try:
            from mast.agents._shared.skill_adapter import _coerce_si_params

            params, si_errors = _coerce_si_params(meta, params)
        except Exception as exc:  # noqa: BLE001 — 判据坏了不挡执行：run() 里还有包络与 validate
            logger.debug("direct-exec: SI 还原不可用(%s)", exc)
        if si_errors:
            res = _failed(skill, f"[{skill}] precondition_failed: {'; '.join(si_errors)}")
            return _finish(ec, skill, params, meta, res, t0, "si_parse", None,
                           runtime, agent_id, thread_id, tool_call_id, context,
                           approval_source, wait_s)

    # 2. 样品门控：门口判一次，放行后子步继承（照 wrap_skill）。
    if meta is not None:
        gate = _sample_gate_message(meta, skill)
        if gate is _GATE_UNREADABLE:
            # 门控自己坏了：门口放行（坏掉的门控不许把仪器锁死，与 wrap_skill 同向），但**不**
            # 置 _scope_admitted —— 子步交给 ExecutionContext.run 自己的门控各判一次，
            # 一次异常不该同时放开两层。
            pass
        elif gate:
            return _finish(ec, skill, params, meta, _failed(skill, gate), t0,
                           "sample_gate", None, runtime, agent_id, thread_id,
                           tool_call_id, context, approval_source, wait_s)
        else:
            try:
                ec._scope_admitted = True
            except Exception:  # noqa: BLE001 — 只读替身
                pass

    # 3. 起跑打戳：手动操作监视器靠它把这段时间的状态变化归给技能而不是人。
    _note_activity(runtime)
    state_before = _snapshot(ec)
    refused_by: str | None = None
    try:
        # 光学站上，组合技能里的 human 节点在这条路径上不再被拒：human_channel 看到这个作用域，
        # 就把节点变成一条结构化操作员提示并**阻塞等操作员答**（有上限、随 ec 的中止事件取消）。
        # STM 站上作用域的 human_nodes 是 False：老行为，立刻明确失败。LangGraph 路径不设作用域，
        # 行为不变。见 mast.operator_hitl.human_node。
        with _human_scope(ec, skill, agent_id, tool_call_id):
            result = ec.run(skill, params)
    except Exception as exc:  # noqa: BLE001 — 技能抛异常也要如实回、如实记
        if _is_graph_interrupt(exc):
            refused_by = "needs_human_node"
            result = _failed(skill, (
                tr(f"[{skill}] 这个组合技能里有需要人决定的 human 节点 —— 直调路径上没有人"
                   "接得住那个暂停，执行已停在那里。请在 MAST 界面里跑它，或改用不含 "
                   "human 节点的版本。",
                   f"[{skill}] This composite skill has a human node that needs a person's decision - "
                   "nobody on the direct-call path can handle that pause, so execution stopped there. "
                   "Run it from the MAST UI, or use a version without human nodes.")))
        else:
            result = _failed(skill, f"{type(exc).__name__}: {exc}")
    if refused_by is None and _is_busy(result):
        refused_by = "busy"
    return _finish(ec, skill, params, meta, result, t0, refused_by, state_before,
                   runtime, agent_id, thread_id, tool_call_id, context,
                   approval_source, wait_s)


#: 门口就拒了、技能根本没跑的几种 —— 它们不发 ``SKILL_STEP``。
_NOT_RUN = frozenset({"si_parse", "sample_gate", "busy"})


def _publish_skill_step(skill: str, success: bool, category: str | None = None) -> None:
    """跑完一个技能就发一条 ``SKILL_STEP``，与遗留的 ``SkillExecutor`` 同一个事件。

    电流监测的「余辉期」（进针 / 退针 / STS 这类技能刚结束的几秒里，它的瞬态不算告警，
    ``monitoring/service.py`` 的 ``_on_bus_event``）只听这一个事件，而 v2 的两条执行
    入口 —— 这里与 agent 侧的 ``skill_adapter.wrap_skill`` —— 从来不发：外部 agent 的 STS 与退针之后的电流变化
    因此曾被判成 RTN。永不抛。

    ``category``：技能的类别（``read`` / ``write`` / ``composite`` /
    ``analysis``）跟着事件走。电流监测只按**名字子串**挑要 pin 的技能，于是
    ``GetAutoApproachStatus``（含 ``autoapproach``）这种纯状态读，每调一次都对监测库做一次
    pin_range UPDATE、再开一段余辉期 —— 外部看守会每隔几秒查一次进针状态。读类技能
    不碰结，不该 pin、也不该让后面几秒的告警静音。"""
    try:
        from mast.core.events import EventBus

        EventBus.get().publish_skill_step(skill=skill, step=0, success=bool(success),
                                          category=category)
    except Exception:  # noqa: BLE001 — 通知失败绝不影响已经发生的动作
        logger.debug("direct-exec: SKILL_STEP for %s not published", skill, exc_info=True)


def _category_of(meta: Any) -> str | None:
    """技能元数据里的类别值（``"read"`` 等）；读不到 ⇒ None（事件保持老形状）。"""
    cat = getattr(meta, "category", None) if meta is not None else None
    val = getattr(cat, "value", cat)
    return str(val).lower() if val else None


def _finish(ec, skill, params, meta, result, t0, refused_by, state_before, runtime,
            agent_id, thread_id, tool_call_id, context, approval_source,
            wait_s: float = DEFAULT_RECORD_WAIT_S) -> DirectRun:
    elapsed = time.monotonic() - t0
    out = DirectRun(skill=skill, params=params, result=result, elapsed_s=elapsed,
                    refused_by=refused_by, abort=_abort_state(ec))
    if refused_by not in _NOT_RUN:
        _publish_skill_step(skill, out.success, _category_of(meta))
    rec = getattr(runtime, "_record_direct_skill_call", None) if runtime is not None else None
    if callable(rec):
        payload = record_payload(
            skill, params, meta, result, duration_ms=int(elapsed * 1000),
            state_before=state_before, state_after=_snapshot(ec),
            agent_id=agent_id, thread_id=thread_id, tool_call_id=tool_call_id,
            context=context, approval_source=approval_source)
        out.recorded = _record_in_background(runtime, rec, payload, skill=skill,
                                             wait_s=wait_s)
    return out


def _record_in_background(runtime: Any, rec: Any, payload: dict, *, skill: str,
                          wait_s: float) -> dict:
    """把一次直调的入账交给后台写入器，最多等 ``wait_s`` 秒拿回执。**永不抛、永不无界等。**

    ## 为什么

    入账原来在请求线程上同步写两行（v1 ``mast_experiments.db`` + v2）。外部看守
    高频轮询 ``GetSignalValues``，环境监测也在写同一个库，写者在 SQLite 锁上
    排队：``V1 action record failed for GetSignalValues … sqlite3.OperationalError:
    database is locked``，每次都先等满 busy timeout —— 回执可能卡很久，看守判
    「失明」而反复停止进针；而 ``StopAutoApproach`` 这类命令其实**当场**就执行了。

    ## 三件必须留在请求线程上的事

    1. **活动戳**（``_note_skill_activity``）：手动操作监视器靠它把这段时间的状态变化归给
       技能。挪到写入线程上会晚到，那几秒的变化就会被算成人手动做的。
    2. **归属快照**（``_direct_record_attribution``）：写入时「当前实验 / 会话」可能已经
       换了，记录要挂在动作发生时的那一个上。
    3. **载荷本身**：``record_payload`` 在这里已经建好，写入线程只读它。

    回执：写完了 ⇒ 与原来同形（``v1`` / ``v2_action_id`` / ``experiment_id`` /
    ``sample_id``）；没等到 ⇒ ``{"pending": True, "v1": None, "v2_action_id": None, …}``
    —— 「还没写完」与「没写进去」（``v1: False``）是两句话；队列满被丢 ⇒ ``dropped: True``。
    """
    att: dict = {}
    snap = getattr(runtime, "_direct_record_attribution", None)
    if callable(snap):
        try:
            att = dict(snap() or {})
        except Exception:  # noqa: BLE001 — 快照不到就按「写入时的当前」记（旧行为）
            att = {}
    if att:
        payload["_attribution"] = att
    # 状态快照必须在这里定格：``InstrumentState.snapshot()`` 返回的是**活的**缓存对象，
    # ``apply_patch`` 原地改它。入账推迟到写入线程之后（库被锁时晚几十秒），下一个技能
    # 打上去的补丁就会被记成这一次的 state_before / state_after。
    for key in ("state_before", "state_after"):
        st = payload.get(key)
        if st is not None:
            try:
                payload[key] = copy.deepcopy(st)
            except Exception:  # noqa: BLE001 — 拷不了就按原样记（与改动前同）
                pass
    _note_activity(runtime)
    kwargs = _record_kwargs(rec)

    def _write() -> dict:
        return dict(rec(payload, **kwargs) or {})

    try:
        from mast.logging.record_writer import get_writer

        ticket = get_writer().submit(_write, label=skill)
    except Exception as exc:  # noqa: BLE001 — 写入器起不来：退回同步写（旧行为），照旧吞
        logger.warning("direct-exec: 后台写入器不可用（%s），%s 改为同步入账", exc, skill)
        try:
            return _write()
        except Exception as exc2:  # noqa: BLE001 — 记账失败绝不能反噬已经发生的动作
            logger.warning("direct-exec: %s 入账失败: %s", skill, exc2)
            return {"v1": False, "v2_action_id": None, "error": str(exc2)[:200]}
    res = ticket.wait(wait_s)
    if res is not None:
        return res
    return {"v1": None, "v2_action_id": None, "pending": True,
            "experiment_id": att.get("experiment_id"), "sample_id": att.get("sample_id")}


def _record_kwargs(rec: Any) -> dict:
    """``rec`` 认得的写入器专用关键字参数（旧签名的替身 / 测试桩 ⇒ ``{}``）。

    不用「先带参数调、TypeError 再不带参数重调」：技能载荷里任何地方抛出的 TypeError
    都会被误当成签名不符，于是同一条记录写两遍。"""
    try:
        import inspect

        names = set(inspect.signature(rec).parameters)
    except (TypeError, ValueError):
        return {}
    out: dict = {}
    if "note_activity" in names:
        out["note_activity"] = False
    if "lock_retry_s" in names:
        out["lock_retry_s"] = RECORD_LOCK_RETRY_S
    return out


def record_payload(skill: str, params: dict, meta: Any, result: Any, *,
                   duration_ms: int, state_before: Any = None, state_after: Any = None,
                   agent_id: str | None = None, thread_id: str | None = None,
                   tool_call_id: str | None = None, context: str = "",
                   approval_source: str = APPROVAL_AUTO) -> dict:
    """入账载荷 —— 键集是 ``skill_adapter.wrap_skill`` 交给 recorder 的那一份的**超集**
    （有测试按 AST 钉着两边的键集，免得一边加了字段另一边永远是空的）。"""
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        data = {} if data is None else {"value": data}
    payload: dict[str, Any] = {
        "skill": skill,
        "skill_version": str(getattr(meta, "version", "") or "") if meta is not None else "",
        "danger_level": _danger_level(meta),
        "params": dict(params),
        "tool_call_id": tool_call_id or "",
        "success": bool(getattr(result, "success", False)),
        "error": str(getattr(result, "error", "") or "")[:1000],
        "summary": str(getattr(result, "summary", "") or "")[:1000],
        "duration_ms": int(duration_ms),
        "artifact_path": (data.get("path") or data.get("file_path") or data.get("sxm_path")),
        "rolled_back": False,
        "state_before": state_before,
        "data": data,
        "state_after": getattr(result, "state_after", None) or state_after,
        "nanonis_calls": list(getattr(result, "nanonis_calls", None) or []),
        "elapsed_s": getattr(result, "elapsed_s", None),
        "approval_source": approval_source,
        "auto_approved": _auto_approval_reason(meta, skill, params),
        "context": str(context or ""),
    }
    if agent_id:
        payload["agent_id"] = str(agent_id)
    if thread_id is not None:
        payload["thread_id"] = thread_id
    return payload


# ─────────────────────────────────────────────────────────────────────
# 小零件（全部永不抛）
# ─────────────────────────────────────────────────────────────────────

def _metadata_of(ec: Any, skill: str) -> Any:
    """技能的**生效**元数据（含管理员覆写）；读不到返回 None。

    None 只让门口的两步（SI 还原、样品门控）让位给 ``run()`` 里的同类检查 ——
    ``run()`` 自己读不到元数据时是**拒绝**执行的，所以这里不会变成无闸放行。
    """
    reg = getattr(ec, "_registry", None)
    if reg is None:
        return None
    try:
        return reg._get_metadata(reg.get(skill))
    except Exception:  # noqa: BLE001
        return None


#: 门控本身读不出结论（抛了异常）—— 与「判过、放行」(None) 分开，见 run_and_record 第 2 步。
_GATE_UNREADABLE = object()


def _sample_gate_message(meta: Any, skill: str) -> Any:
    """拒绝文案 / None（判过、放行）/ ``_GATE_UNREADABLE``（门控自己坏了）。"""
    try:
        from mast.core.sample_gate import check_sample_scope
        from mast.logging.experiment_log import get_active_log

        return check_sample_scope(meta, skill, get_active_log())
    except Exception:  # noqa: BLE001 — 坏掉的门控不许把仪器锁死（与 wrap_skill 同向）
        logger.warning("direct-exec: 样品门控判不出来（%s）—— 门口放行，子步各自再判", skill,
                       exc_info=True)
        return _GATE_UNREADABLE


def _waits_for_human_nodes() -> bool:
    """Whether a ``human`` node on this path asks a real operator: only on the optical station
    (``mast.station``). An STM station keeps what it always did - the node
    ends the job with ``needs_human_node``. Unreadable profile = STM: **永不抛**."""
    try:
        from mast import station

        return bool(station.is_optical())
    except Exception:  # noqa: BLE001
        return False


def _human_scope(ec: Any, skill: str, agent_id: str | None, tool_call_id: str | None) -> Any:
    """``ec.run`` 期间的 human 节点作用域（见 ``mast.operator_hitl.human_node``）。**永不抛**：
    取不到就是不设作用域，human 节点退回老行为（没有通道 ⇒ 明确失败）。"""
    try:
        from mast.operator_hitl import human_node

        check = getattr(ec, "check_abort", None)
        return human_node.direct_scope(check_abort=check if callable(check) else None,
                                       skill=skill, agent_id=agent_id, job_id=tool_call_id,
                                       human_nodes=_waits_for_human_nodes())
    except Exception:  # noqa: BLE001
        logger.debug("direct-exec: human 节点作用域不可用", exc_info=True)
        return contextlib.nullcontext()


def _note_activity(runtime: Any) -> None:
    fn = getattr(runtime, "_note_skill_activity", None) if runtime is not None else None
    if callable(fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


def _snapshot(ec: Any) -> Any:
    """缓存快照（1 Hz 后台刷新的那份）。**绝不** ``refresh()`` —— 那是真硬件 I/O。"""
    st = getattr(ec, "state", None)
    snap = getattr(st, "snapshot", None)
    if not callable(snap):
        return None
    try:
        return snap()
    except Exception:  # noqa: BLE001
        return None


def _abort_state(ec: Any) -> dict | None:
    check = getattr(ec, "check_abort", None)
    if not callable(check):
        return None
    try:
        if not check():
            return None
    except Exception:  # noqa: BLE001
        return None
    reason = ""
    fn = getattr(ec, "abort_reason", None)
    if callable(fn):
        try:
            reason = str(fn() or "")
        except Exception:  # noqa: BLE001
            reason = ""
    return {"set": True, "reason": reason}


def _is_busy(result: Any) -> bool:
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return False
    try:
        from mast.conduct.adapters import INSTRUMENT_BUSY_KEY
    except Exception:  # noqa: BLE001
        INSTRUMENT_BUSY_KEY = "instrument_busy"  # noqa: N806 — 单一真源不可达时的同值兜底
    return bool(data.get(INSTRUMENT_BUSY_KEY))


def _is_graph_interrupt(exc: BaseException) -> bool:
    try:
        from langgraph.errors import GraphInterrupt
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, GraphInterrupt)


def _failed(skill: str, error: str) -> Any:
    from mast.core.types import SkillResult

    return SkillResult(skill_name=skill, success=False, error=error)


def _danger_level(meta: Any) -> str:
    level = getattr(meta, "safety_level", None) if meta is not None else None
    return str(getattr(level, "name", "") or "AUTO") if level is not None else "AUTO"


def _auto_approval_reason(meta: Any, skill: str, params: dict) -> str | None:
    """「这一步在旧审批策略下会停下来等人吗」—— 与中间件、执行器同一判据。"""
    if meta is None:
        return None
    try:
        from mast.core.auto_approval import would_have_asked

        return would_have_asked(meta, tool_name=skill, args=params)
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────────────
# 返回体 → JSON（「永不 500」必须对所有返回体成立）
# ─────────────────────────────────────────────────────────────────────

def jsonable(obj: Any, _depth: int = 0) -> Any:
    """把技能的 ``data`` 变成 JSON 能装的东西。**永不抛。**

    这个端点族承诺「永不 500」，而第一版只管住了技能**抛异常**那一路，没管技能
    **成功但返回体装不进 JSON** 那一路：``GetDualScopeData`` 把原始三段信封
    ``(error, raw_bytes, body)`` 整个塞进 ``data``，那段 bytes 让序列化炸掉。

    bytes 不还原成文本（可能是二进制波形），只报长度；超长数组留头尾并报真实长度。
    """
    if _depth > 6:
        return "<嵌套过深,已截断>"
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else f"<非有限值 {obj!r}>"
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return f"<bytes len={len(bytes(obj))}>"
    if isinstance(obj, dict):
        return {str(k): jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        seq = list(obj)
        if len(seq) > 512:      # 波形数组:留头尾,别把整条谱塞进 HTTP 响应
            return {"_truncated": True, "len": len(seq),
                    "head": jsonable(seq[:8], _depth + 1),
                    "tail": jsonable(seq[-8:], _depth + 1)}
        return [jsonable(v, _depth + 1) for v in seq]
    try:                        # numpy 标量 / 数组
        import numpy as _np

        if isinstance(obj, _np.generic):
            return jsonable(obj.item(), _depth + 1)
        if isinstance(obj, _np.ndarray):
            return jsonable(obj.tolist(), _depth + 1)
    except Exception:  # noqa: BLE001
        pass
    return repr(obj)[:400]


__all__ = [
    "APPROVAL_AUTO",
    "APPROVAL_LLM",
    "DEFAULT_RECORD_WAIT_S",
    "DirectRun",
    "actor_slug",
    "build_context",
    "jsonable",
    "live_runtime",
    "new_run_id",
    "record_payload",
    "run_and_record",
]
