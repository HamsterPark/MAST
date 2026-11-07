"""修针方案表 —— 按针尖类型决定用什么参数处理它。

设计决定:需要一张完整的参数方案表 —— 当前水平的 LLM 不懂 STM
实验,让它自己想参数不可靠。

这与扫图参数解析的思路同源:**不是提示模型别乱填数字,而是移除它发明数字的
诱因**。在此之前修针技能的参数只有两条来源 —— 模型在 tool call 里现编,或者
一个写死的常量(``tip_shaper`` 的 bias 3.0 V 对钨腐蚀针和铂铱剪切针是同一个数)。
两条都不对:前者让不懂 STM 的模型替物理做决定,后者假装所有针尖一样。

## 表的形状

键是 ``(material, fabrication, form)``,查表**逐级回退**:

    (W, etched, stm_wire) → (W, *, stm_wire) → (*, *, stm_wire) → 通用默认

所以只有真正需要区分的组合才写进表里,其余自动落到更粗的那一档。未登记针尖时
落到通用默认 —— 那一档刻意保守。

## 数值从哪来

出厂值取自公开文献与常见实践,**逐项注明来源**,并一律标「待真机标定」。
知识库值是近似,随仪器/样品/温度变,别当权威默认(这是本仓既有纪律)。用户
覆写存 SettingsStore 的 ``tip_conditioning_overrides``,优先级高于出厂值。

## 安全包络

每档带一组**上限**。它们与参数默认值是两回事:默认值是「不给就用这个」,上限是
「给了也不许超」。qPlus 那一档的上限尤其硬 —— 石英音叉被戳坏不可逆,要拆机重装。
超上限的处理是**拒绝,不 clamp**:clamp 属参数卫生(scan_resolver 那一层),
而这里是安全层,与粗动电压四重锁同一条哲学 ——「超上限拒绝不夹紧」。
"""

from __future__ import annotations

import logging
from typing import Any
from mast.i18n import tr

logger = logging.getLogger(__name__)

#: 通配符:表里用它表示「这一维不区分」。
ANY = "*"


# ── 参数字段 ────────────────────────────────────────────────────────────────
#
# 字段名与各技能的 ParameterSpec 一一对应,resolver 按名字发给对应技能。

#: 一档方案里可能出现的参数,以及它属于哪个技能。
FIELD_OWNERS: dict[str, tuple[str, ...]] = {
    "pulse_v": ("TipPulse", "ConditionTip"),
    "pulse_duration_s": ("TipPulse",),
    "pulse_count": ("TipPulse",),
    "shaper_bias_v": ("TipShape",),
    "shaper_lift_v": ("TipShape",),
    "shaper_depth_m": ("TipShape",),
    "poke_shallow_depth_m": ("ShapeTipOnSurface",),
    "poke_deep_depth_m": ("ShapeTipOnSurface",),
    "poke_steps": ("ShapeTipOnSurface",),
    "target_quality": ("ConditionTip",),
    "max_attempts": ("ConditionTip",),
}

#: 安全包络字段(上限,超了拒绝)。
LIMIT_FIELDS: tuple[str, ...] = (
    "max_abs_pulse_v",     # 脉冲电压绝对值上限
    "max_poke_depth_m",    # 向表面下压的最大深度(正数,米)

    "max_pulse_count",     # 单次最多打几发
)



_DEFAULT: dict[str, Any] = {
    # 未登记针尖 / 认不出的组合落到这里。刻意保守:不知道针是什么的时候,
    # 宁可处理不够也不要一发把针打没。
    "pulse_v": 3.0,
    "pulse_duration_s": 0.1,
    "pulse_count": 1,

    "shaper_depth_m": -1.0e-9,
    "poke_shallow_depth_m": -5.0e-10,
    "poke_deep_depth_m": -2.0e-9,
    "poke_steps": 5,
    "target_quality": 0.3,
    "max_attempts": 5,
    "max_abs_pulse_v": 10.0,
    "max_poke_depth_m": 1.0e-8,
    "max_pulse_count": 5,
    "_note": tr("针尖未登记或组合未收录 —— 用保守通用参数。登记针尖可得到更合适的方案。",
                  "Tip not registered or combination not listed -- using conservative generic parameters. Registering the tip gives a better-suited recipe."),
}

#: (material, fabrication, form) → 参数覆盖(只写与上一级不同的字段)。
#:
#: 查表顺序见 :func:`resolve_policy`。写表时**只写差异**,便于看出「这一档特殊
#: 在哪」。
_POLICY: dict[tuple[str, str, str], dict[str, Any]] = {

    # ── 普通金属丝针尖 ────────────────────────────────────────────────
    (ANY, ANY, "stm_wire"): {
        "pulse_v": 4.0,
        "max_abs_pulse_v": 10.0,      # Nanonis bias 常见量程,也是 TipPulse 的 spec 上限
        "max_poke_depth_m": 1.0e-8,
        "_note": tr("金属丝针尖通用档。",
                  "Generic profile for wire tips."),
    },

    # 钨:硬、耐打,但暴露大气后有氧化层,初次进针常需要更强的处理才出好态。
    # 文献常见的钨针场成形/脉冲量级在几伏到十伏;这里取中段起步。
    ("W", ANY, "stm_wire"): {
        "pulse_v": 5.0,
        "shaper_bias_v": 4.0,
        "shaper_depth_m": -1.5e-9,
        "poke_deep_depth_m": -3.0e-9,
        "_note": tr("钨针较硬、耐处理;氧化层可能需要偏强的首轮处理。待真机标定。",
                  "Tungsten tips are hard and tolerate conditioning; the oxide layer may need a stronger first round. Pending calibration on the real instrument."),
    },
    ("W", "etched", "stm_wire"): {
        # 腐蚀针顶端细,单次不宜过猛;宁可多来一轮。
        "pulse_v": 5.0,
        "max_attempts": 6,
        "_note": tr("钨电化学腐蚀针:顶端细,宁可多轮轻处理。待真机标定。",
                  "Electrochemically etched tungsten tip: fine apex, prefer several gentle rounds. Pending calibration on the real instrument."),
    },
    ("W", "cut", "stm_wire"): {
        # 剪切钨针顶端不规则、常有多个微尖 —— 双针尖先验高,需要更多轮成形。
        "pulse_v": 5.5,
        "max_attempts": 8,
        "poke_deep_depth_m": -4.0e-9,
        "_note": tr("钨剪切针:多微尖先验高,通常需要更多轮成形。待真机标定。",
                  "Cut tungsten tip: high prior of multiple micro-apexes, usually needs more shaping rounds. Pending calibration on the real instrument."),
    },

    # 铂铱:不氧化、惰性好、适合谱学,但材质软 —— 重手法容易钝掉甚至粘针。
    ("PtIr", ANY, "stm_wire"): {
        "pulse_v": 3.0,
        "shaper_bias_v": 2.5,
        "shaper_depth_m": -8.0e-10,
        "poke_shallow_depth_m": -3.0e-10,
        "poke_deep_depth_m": -1.5e-9,
        "max_abs_pulse_v": 10.0,
        "max_poke_depth_m": 1.0e-8,
        "_note": tr("铂铱软:处理要比钨针轻,深压容易钝尖/粘针。待真机标定。",
                  "Pt-Ir is soft: condition more gently than tungsten, deep pokes easily blunt or contaminate the tip. Pending calibration on the real instrument."),
    },
    ("PtIr", "cut", "stm_wire"): {
        "max_attempts": 7,
        "_note": tr("铂铱剪切针:软 + 多微尖,轻处理多轮。待真机标定。",
                  "Cut Pt-Ir tip: soft plus multiple micro-apexes, several gentle rounds. Pending calibration on the real instrument."),
    },

    ("Pt", ANY, "stm_wire"): {
        "pulse_v": 3.0, "shaper_bias_v": 2.5,
        "max_abs_pulse_v": 10.0,
        "_note": tr("铂针软且惰性,处理宜轻。待真机标定。",
                  "Platinum tips are soft and inert, condition gently. Pending calibration on the real instrument."),
    },
    ("Ir", ANY, "stm_wire"): {
        "pulse_v": 4.0,
        "_note": tr("铱针硬度高于铂铱、惰性好。待真机标定。",
                  "Iridium tips are harder than Pt-Ir and inert. Pending calibration on the real instrument."),
    },

    # 磁性针尖(自旋极化):磁构型是实验对象的一部分 —— 大力处理不只改几何,
    # 还会改磁性,把「修好了」变成「换了一根不同的针」。
    ("Fe", ANY, "stm_wire"): {
        "pulse_v": 2.5,
        "shaper_bias_v": 2.0,
        "shaper_depth_m": -5.0e-10,
        "poke_shallow_depth_m": -2.0e-10,
        "poke_deep_depth_m": -1.0e-9,
        "max_abs_pulse_v": 10.0,
        "max_poke_depth_m": 1.0e-8,
        "max_attempts": 4,
        "_note": tr("磁性针尖:处理会改变磁构型,不只是几何形状。"
                  "自旋极化实验中「修针」可能使之前的磁对比不可比。待真机标定。",
                  "Magnetic tip: conditioning changes the magnetic configuration, not just "
                  "the geometry. In spin-polarised experiments, tip conditioning may make "
                  "earlier magnetic contrast incomparable. Pending calibration on the real "
                  "instrument."),
    },
    ("Ni", ANY, "stm_wire"): {
        "pulse_v": 2.5, "max_abs_pulse_v": 10.0,
        "_note": tr("镍针为磁性针尖,处理会改变磁构型。待真机标定。",
                  "Nickel tips are magnetic; conditioning changes the magnetic configuration. Pending calibration on the real instrument."),
    },
    ("Co", ANY, "stm_wire"): {
        "pulse_v": 2.5, "max_abs_pulse_v": 10.0,
        "_note": tr("钴针为磁性针尖,处理会改变磁构型。待真机标定。",
                  "Cobalt tips are magnetic; conditioning changes the magnetic configuration. Pending calibration on the real instrument."),
    },
    ("Cr", ANY, "stm_wire"): {
        "pulse_v": 3.0, "max_abs_pulse_v": 10.0,
        "_note": tr("铬针为反铁磁针尖(杂散场小),处理会改变磁构型。待真机标定。",
                  "Chromium tips are antiferromagnetic (small stray field); conditioning changes the magnetic configuration. Pending calibration on the real instrument."),
    },

    # 超导针尖:回温、污染、强脉冲都可能破坏针尖超导能隙 —— 而那正是测量对象。
    ("Nb", ANY, "stm_wire"): {
        "pulse_v": 2.0,
        "max_abs_pulse_v": 10.0,
        "max_poke_depth_m": 1.0e-8,
        "max_attempts": 3,
        "_note": tr("超导针尖:强处理会破坏针尖能隙,而能隙正是测量对象。"
                  "优先换针而不是反复修。待真机标定。",
                  "Superconducting tip: strong conditioning destroys the tip gap, and the gap "
                  "is exactly what is being measured. Prefer swapping the tip over repeated "
                  "conditioning. Pending calibration on the real instrument."),
    },

    # ── qPlus 传感器 ──────────────────────────────────────────────────
    #
    # 石英音叉被戳坏**不可逆**,要拆机重装(往往还要重新粘针、重新标定 f₀/Q)。
    # 所以这一档的包络最硬,而且戳表面类技能另有一道软门(见 tip_state 注入块)。
    (ANY, ANY, "qplus"): {
        "pulse_v": 2.0,
        "pulse_duration_s": 0.05,
        "pulse_count": 1,

        "shaper_depth_m": -2.0e-10,
        "poke_shallow_depth_m": -1.0e-10,
        "poke_deep_depth_m": -3.0e-10,
        "poke_steps": 3,
        "max_attempts": 3,
        # **10 V —— 按实际做法确定**(原包络过于保守)。
        #
        # 实际做法就是 10 V / 500 ms,成功判据 Z 明显抬升(见操作范本
        # §一.3)。此前的 3.0 是**未标定的保守占位**,而它的代价是真的:forge 实跑被
        # 这道包络改成 2 V,打不出实际用的那一下 —— 一个未标定的数字挡住了
        # 唯一有真机依据的做法。
        #
        # ⚠️ 这一条与 ``poke_deep_depth_m`` 一样属于「有真机来源」的少数派;
        # 本表其余 qPlus 数字仍是未标定占位,不要因为这条被抬高就跟着抬别的。
        "max_abs_pulse_v": 10.0,
        # ⚠️ **这是整张表里第二个有真机来源的 qPlus 数字**(另一个是上面的
        # ``max_abs_pulse_v``)。
        #
        # 实机判断:3 nm 以内的扎针对传感器无害,
        # 随后上限改为 5 nm。此前的 5.0e-10(0.5 nm)是**未标定的保守占位**,
        # 与本表其余标着「待真机标定」的条目同一性质。
        #
        # 分辨这两类很重要:其余每一档的数字仍然是文献起点,没有任何一台真机验证过;
        # 只有这两个数字背后有实机判断。别因为这两行看起来很确定,就以为整张表
        # 都标定过了。
        #
        # (旧注释写着「同档的 max_abs_pulse_v = 3.0 未获授权,没有动」;
        #  该值随后已放开,旧理由因此失效,已删除。)
        "max_poke_depth_m": 1.0e-8,
        "max_pulse_count": 2,
        "_note": tr("qPlus 传感器:石英音叉损坏不可逆(需拆机重装 + 重新标定 f₀/Q)。"
                  "深压与大脉冲一律拒绝,不夹紧。"
                  "``max_poke_depth_m`` = 5 nm 出自一次真机判断"
                  "(「5 nm 以内的下压对该 W-qPlus 音叉无损」);本档**其余**数值"
                  "仍待真机标定。",
                  "qPlus sensor: damage to the quartz tuning fork is irreversible (needs "
                  "disassembly, refitting and recalibrating f0/Q). Deep pokes and large "
                  "pulses are always rejected, never clamped. "
                  "``max_poke_depth_m`` = 5 nm comes from a judgement on the real "
                  "instrument ('pokes within 5 nm do no harm to the W-qPlus "
                  "fork'); the **remaining** values of this profile are still pending "
                  "calibration on the real instrument."),
    },
    ("PtIr", ANY, "qplus"): {
        "pulse_v": 1.5,

        "_note": tr("qPlus + 铂铱针:软材质 + 易损传感器,最轻的一档。待真机标定。",
                  "qPlus + Pt-Ir tip: soft material plus a fragile sensor, the gentlest profile. Pending calibration on the real instrument."),
    },
    ("W", ANY, "qplus"): {
        "pulse_v": 2.0,
        "_note": tr("qPlus + 钨针:材质耐打但传感器不耐打,按传感器的包络来。待真机标定。",
                  "qPlus + tungsten tip: the material is tough but the sensor is not, so follow the sensor's envelope. Pending calibration on the real instrument."),
    },
}


def _lookup_chain(material: str, fabrication: str, form: str) -> list[dict[str, Any]]:
    """从粗到细的命中序列(后者覆盖前者)。"""
    mat = (material or "").strip() or ANY
    fab = (fabrication or "").strip() or ANY
    frm = (form or "").strip() or "stm_wire"
    keys = [
        (ANY, ANY, frm),        # 形态通用
        (mat, ANY, frm),        # 材料 × 形态
        (mat, fab, frm),        # 精确组合
    ]
    return [_POLICY[k] for k in keys if k in _POLICY]


def resolve_policy(
    facts: "dict[str, Any] | None",
    *,
    overrides: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """当前针尖对应的一整档方案(参数 + 安全包络 + 说明 + 来源痕迹)。

    *facts* 是 :func:`mast.core.tip_state.current_tip_facts` 的返回(None = 针尖
    未登记 → 通用保守档)。*overrides* 是用户在设置里填的覆盖,优先级最高。

    返回值里的 ``_sources`` 记录每个字段是哪一级给的 —— 与 scan_resolver 的
    trace 同款:一个数字从哪来,事后必须查得到。
    """
    out: dict[str, Any] = dict(_DEFAULT)
    sources: dict[str, str] = {k: "factory_default" for k in out if not k.startswith("_")}
    notes: list[str] = []
    if _DEFAULT.get("_note") and not facts:
        notes.append(str(_DEFAULT["_note"]))

    if facts:
        for layer in _lookup_chain(
                str(facts.get("material") or ""),
                str(facts.get("fabrication") or ""),
                str(facts.get("form") or "stm_wire")):
            for key, val in layer.items():
                if key == "_note":
                    notes.append(str(val))
                    continue
                out[key] = val
                sources[key] = "policy_table"

    if overrides:
        for key, val in overrides.items():
            if key.startswith("_") or key not in out:
                continue
            if val is None:
                continue
            try:
                out[key] = type(out[key])(val) if isinstance(out[key], (int, float)) else val
            except (TypeError, ValueError):
                logger.debug("修针方案覆写 %s=%r 类型不符,忽略", key, val)
                continue
            sources[key] = "operator_override"

    out["_sources"] = sources
    out["_notes"] = notes
    return out


def policy_summary(facts: "dict[str, Any] | None") -> str:
    """一行人读的方案说明(注入块/技能结果里用)。"""
    pol = resolve_policy(facts)
    notes = pol.get("_notes") or []
    return " ".join(str(n) for n in notes) if notes else ""


__all__ = [
    "ANY", "FIELD_OWNERS", "LIMIT_FIELDS",
    "resolve_policy", "policy_summary",
]
