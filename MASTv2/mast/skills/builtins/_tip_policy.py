"""修针类技能的共享接线:qPlus 软门 + 方案表参数填充。

四个技能(TipShape / TipPulse / ConditionTip / ShapeTipOnSurface)要做同样的两
件事,所以放在一处 —— **precondition 副本漂移**是本仓踩过的坑(粗动那次:agent
路和手动路各有一份判据,改了一处另一处还是旧的)。

## 为什么 qPlus 门是软门而不是 HITL

HITL 门是 graph **构建时**从静态 ``SkillMetadata.safety_level`` 派生的
(``instrument_control/graph.py`` 的 ``_derive_hitl_map``),运行时的针尖状态改
不了它 —— 除非重建整个 orchestrator,那是 ``hardware_modules`` 级别才付的代价。
所以门做在技能入口:读当前针尖,是 qPlus 且没有显式 ``allow_on_qplus=true`` 就
拒绝执行并说清为什么。

未登记针尖时**放行**(fail-open,与 ``sample_gate`` 同一条论证):系统不知道装的
是什么针,没资格替用户否决一个可能完全正常的操作。
"""

from __future__ import annotations

import logging
from typing import Any

from mast.core.types import SkillResult
from mast.i18n import tr

logger = logging.getLogger(__name__)

#: 显式旁路参数名。技能的 ParameterSpec 里也要声明它,模型才看得见。
ALLOW_ON_QPLUS = "allow_on_qplus"


def _guard_on() -> bool:
    """返回门是否开启，默认关闭，见 qplus_gate。每次调用都读取环境变量，保证运行中的配置变更可见。"""
    import os

    return str(os.environ.get("MAST_QPLUS_POKE_GUARD", "0")).strip().lower() \
        in ("1", "true", "on", "yes")

_QPLUS_REFUSAL = (
    "当前登记的针尖是 qPlus 传感器（{name}），而 {skill} 会向表面下压。损坏传感器可能不可逆，并需要更换与重新标定。确认执行时显式提供 allow_on_qplus=true；若登记不正确，请先用 register_tip 更正。"
)
_QPLUS_REFUSAL_EN = (
    "The registered tip is a **qPlus sensor** ({name}), and {skill} presses the tip down into the "
    "surface. A quartz tuning fork that gets crushed is damaged irreversibly -- the machine has to be "
    "opened up, re-fitted and recalibrated (f0/Q), and no experiments will be possible for the next "
    "few days.\n"
    "To run it on a qPlus tip anyway, pass allow_on_qplus=true explicitly; if the tip is not actually "
    "a qPlus, correct the registration with register_tip first."
)


def qplus_gate(skill_name: str, params: dict, calls: list | None = None
               ) -> "SkillResult | None":
    """qPlus 针尖上的「戳表面」类操作。**默认放行。**

    读不到针尖状态一律放行 —— 门的失败模式必须是放过而不是拦住(误拦可能让操作
    员在针要撞上去时按不动按钮)。

    ## 为什么默认放行

    qPlus 仪器允许在一个很小的深度包络内扎针(2 nm 以内)。这道门只问「是否已确认这是 qPlus」;
    一道每次都被同一个确认解开的门,不是保护,是仪式。

    **改的是默认值,不是把保护删掉。** 真正护音叉的是另外两样,都留着:

      1. 扎针深度包络 ``max_poke_depth_m`` —— 超了**拒绝不夹紧**。
         「2 nm 以内」由它执行,不是这道门。
      2. 扎针前把偏压**缓降到 20 mV**(``_tip_phases._poke_step``)——
         qPlus 扎针必须先降到 20 mV,否则音叉起振,每次扎针都在毁针。

    ``MAST_QPLUS_POKE_GUARD=1`` 可以把它开回来(环境变量而不是配置项:
    配置项会长出第二个真源,而这是对单台仪器的一次性取舍)。
    """
    if params.get(ALLOW_ON_QPLUS):
        return None
    if not _guard_on():
        return None
    try:
        from mast.core.tip_state import current_tip_facts
        facts = current_tip_facts()
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: 读针尖失败(放行): %s", skill_name, exc)
        return None
    if not facts or facts.get("form") != "qplus":
        return None
    return SkillResult(
        skill_name=skill_name,
        success=False,
        error=tr(_QPLUS_REFUSAL, _QPLUS_REFUSAL_EN).format(
            name=facts.get("name") or tr("未命名", "unnamed"), skill=skill_name),
        nanonis_calls=list(calls or []),
    )


def apply_tip_policy(
    params: dict,
    policy_fields: "tuple[str, ...]",
    rename: "dict[str, str] | None" = None,
) -> tuple[dict, Any]:
    """按当前针尖的方案表补齐 *params* 里没给的值,并检查安全包络。

    *policy_fields* 是方案表里的字段名;*rename* 把它们映射到技能自己的参数名
    (例如方案表的 ``shaper_bias_v`` → TipShape 的 ``bias_v``)。

    返回 ``(新 params, ResolvedConditioning | None)``。第二个值为 None 表示解析
    层不可用(此时原样返回 params —— 方案表读不到绝不能让修针技能失败,技能自己的
    ParameterSpec 默认值仍在)。``plan.ok`` 为 False 时调用方**必须不执行**。
    """
    rename = rename or {}
    try:
        from mast.core.tip_conditioning_resolver import resolve_conditioning
    except Exception as exc:  # noqa: BLE001
        logger.debug("方案表不可用(用技能自带默认): %s", exc)
        return params, None

    # 技能参数名 → 方案表字段名,把调用方显式给的值带进解析。
    explicit: dict[str, Any] = {}
    for field in policy_fields:
        skill_key = rename.get(field, field)
        if params.get(skill_key) is not None:
            explicit[field] = params[skill_key]

    try:
        plan = resolve_conditioning(policy_fields, explicit)
    except Exception as exc:  # noqa: BLE001
        logger.debug("方案解析失败(用技能自带默认): %s", exc)
        return params, None

    out = dict(params)
    for field in policy_fields:
        if field in plan.params:
            out[rename.get(field, field)] = plan.params[field]
    return out, plan


def policy_fields_for_result(plan: Any) -> dict[str, Any]:
    """放进 SkillResult.data 的方案痕迹 —— 每个数字是谁给的,事后查得到。"""
    if plan is None:
        return {}
    out: dict[str, Any] = {"tip_policy": plan.human_trace()}
    if getattr(plan, "notes", None):
        out["tip_policy_notes"] = " ".join(str(n) for n in plan.notes)
    tip = getattr(plan, "tip", None)
    out["tip_registered"] = bool(tip)
    if tip:
        out["tip_name"] = tip.get("name") or ""
    return out


def shaper_bias_default(context) -> "tuple[float | None, str]":
    """Tip shaper 的 ``bias_v`` 缺省值 = **此刻的成像偏压**;读不到给 ``(None, 原因)``。

    修法:shaper 缺省取扫图偏压,而不是锁死 3 V。

    在此之前两个扎针技能都写着 ``params.get("bias_v", 3.0)``。那个 3.0 追到
    v1 移植时的 stub 占位,**没有任何物理来源** —— 它对应哪台机器、
    哪根针?哪台都不对。和 ``"Piezo"``(不存在的模块名)是同一族:
    **一个没人拥有的默认值**。

    换成读取之后有三处直接受益:

    * qPlus 那条路**一处生效**:成像偏压设成 20 mV → shaper 自带就是 20 mV → 扎。
      不再需要两处配合,也就不会再出现「一处设了、另一处顶掉」——
      曾出现过这样被顶掉的情形(设了 20 mV,回包 ``bias_v: 3.0``)。
    * 非 qPlus 针尖也对:沿用成像偏压是最小惊讶;3 V 是一个**会实际改变针尖**的
      动作,不该是默认发生的事。
    * ``change_bias`` 这个参数**保留** —— 调用方仍可显式关掉。

    **读不到不回落到 3.0。** ``lookup() || DEFAULT`` 是反复出现的出错形状;
    这里宁可让调用方看到一句「读不到当前偏压」,也不要悄悄打一个没人要的 3 V。
    """
    try:
        rec = context.safe_call("Bias_Get")
    except Exception as exc:  # noqa: BLE001
        return None, tr(f"Bias_Get 抛异常:{type(exc).__name__}: {exc}",
                        f"Bias_Get raised: {type(exc).__name__}: {exc}")
    err = getattr(rec, "error", None)
    if err:
        return None, tr(f"Bias_Get 报错:{err}", f"Bias_Get returned an error: {err}")
    raw = getattr(rec, "return_value", None)
    # 回包形状 (err, raw, body) —— 载荷在 index 2,与全仓其它解析同一走法。
    try:
        body = raw[2] if isinstance(raw, (list, tuple)) and len(raw) > 2 else None
        val = float(body[0] if isinstance(body, (list, tuple)) else body)
    except (TypeError, ValueError, IndexError):
        return None, tr(f"Bias_Get 回包读不懂(repr 前 80 字:{str(raw)[:80]})",
                        f"Cannot parse the Bias_Get reply (first 80 chars of repr: {str(raw)[:80]})")
    if val != val or val in (float("inf"), float("-inf")):   # NaN / inf
        return None, tr(f"Bias_Get 读回 {val!r}", f"Bias_Get read back {val!r}")
    return val, ""


def _num_or_none(value: Any) -> "float | None":
    """``float(value)`` or None. Not ``float(v or 0)`` — 0.0 is a real height."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolved_lift_height_m(params: dict) -> float:
    """第二段斜坡的高度:没给就 = ``-tip_lift_m``(压进去多少就抬回来多少)。

    这条规则本来只活在 composite 里 —— ``_tip_phases.py`` 的
    ``"lift_height_m": abs(depth_m)``、``shape_tip_on_surface.py:440``、
    ``builtin_composites.py:346`` 各写一遍,而**裸技能的默认是 0.0**:模型直接调
    TipShape 只给 ``tip_lift_m`` 时,针压下去就不抬了,靠 ``restore_feedback``
    把它拽回来。规则住在三个调用方而不是被调方,正是本仓「规则没下沉」那一类
    (2026-08-11)。

    显式给 0.0 仍然是 0.0 —— 「不抬」是一个合法意图,只是不该是**缺省**意图。
    """
    given = _num_or_none(params.get("lift_height_m"))
    if given is not None:
        return given
    lift = _num_or_none(params.get("tip_lift_m"))
    return -lift if lift is not None else 0.0


__all__ = ["ALLOW_ON_QPLUS", "qplus_gate", "apply_tip_policy",
           "policy_fields_for_result", "shaper_bias_default",
           "resolved_lift_height_m"]
