# -*- coding: utf-8 -*-
"""CleanTipUntilBarrier —— 修针，但**每一步都用势垒验证有没有变好**。

## 它治的是什么病

典型的失败：为了改善一条平掉的 I–V 做若干次浅扎与脉冲，
**一次都没有事先想好「怎么算变好」**。结果：I–V 形态一点没变，
而逐行跳动 σ 涨了两个数量级 —— 针尖被毁了，此后的形貌全是横条纹，
后续多次浅扎也没能完全拉回原来的水平。

这类错误很容易重复出现。

⇒ 本技能把「动针尖之前先有判据」变成结构，而不是纪律：

* **先测基线。** 已经达标就**一步都不做**，直接返回 `already_clean`。
  （典型情形：被要求清洁针尖，而基线 φ
  已经是 clean —— 正确的动作是**不动**。）
* **每一步之后重测。** 没变好就是没变好，不靠感觉。
* **连续无改善就停**，并交出**过程中最好的那个状态**，而不是最后那个。
* **变差就立刻停** —— 「递进加深」的典型失败：只写了「变好就停」，
  没写「变差也要停」，于是一路扎到针尖坏掉。

## 阶梯（从温和到激进）

经验依据：**0.5–0.8 nm 的浅扎有效，1.2/1.6 nm 反而更差**；
经验判据：2 nm 以内的扎针比脉冲温和，微小的扎针破坏性远小于 pulse。
所以先扎后脉冲，且**每一发脉冲之后必须补一次扎针稳定**
（经验规则，对照：只脉冲不扎针的幅值平衡度明显更低，
脉冲后补扎针则更高）。

## 它不做的事

不换针尖、不动粗动、不找平区（那是 `FindFlatRegion` 的事）。
势垒读不出来时**如实说判不了**，不把「没测到」当成「没变好」——
两者驱动的下一步不同（前者该查测量，后者才该继续修）。
"""

from __future__ import annotations

import logging

from mast.i18n import tr
from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill

logger = logging.getLogger(__name__)

#: 默认目标势垒（eV）。3.0 是 `MeasureBarrierHeight` 判 clean 的线。
_DEFAULT_TARGET_EV = 3.0

# 动作阶梯规定顺序与参数；它是工作流策略，不代表跨样品验证过的有效性排序。
# 每步均须以新的测量结果决定是否继续。
_LADDER = (
    ("poke", 0.5),
    ("poke", 0.5),
    ("poke", 0.8),
    ("pulse", 3.0),
    ("pulse", -3.0),
    ("pulse", 4.0),
    ("pulse", -4.0),
)

#: 连续多少步没改善就停。
_MAX_STALE = 3

# 改善比率需要与测量重复性区分。此默认值是工作流判据，
# 应结合当前测量不确定度验证，不能把噪声内波动当成确定改善。
_IMPROVE_FRAC = 0.15


class CleanTipUntilBarrier(BaseSkill):
    """Condition the tip, verifying with the tunnelling barrier at every step."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CleanTipUntilBarrier",
            version="1.0.0",
            category=SkillCategory.WRITE,
            capabilities=frozenset({"tip_shaping", "bias_pulse"}),
            # 会打脉冲、会扎针 —— 与 PokeConditionTip / PrepareNobleTip 同级。
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("在贵金属基底上修针，**每一步都用 I–Z 势垒验证有没有变好**，"
                "而不是靠感觉或看图。"
                "\n\n**第一件事是测基线；已经达标就一步都不做**，返回 `already_clean`。"
                "典型情形：被要求「把针尖搞干净」，而基线 φ "
                "已经 clean —— 正确的动作是**不动**。"
                "\n\n**它治的是一类典型事故**：为了改善一条平掉的 I–V，"
                "做了几次浅扎和脉冲，**一次都没事先想好「怎么算变好」**；"
                "I–V 一点没变，而逐行跳动 σ 涨了两个数量级 —— 针尖被毁了。"
                "这类错误很容易重复出现。"
                "\n\n阶梯**先扎后脉冲**（2 nm 以内的扎针比脉冲温和得多），深度只在 "
                "0.5–0.8 nm（1.2/1.6 nm 实测反而更差），**每一发脉冲之后必须补一次扎针稳定**。"
                "\n\n三条护栏：连续 %d 步没改善就停；**变差立刻停**；"
                "结束时交出**过程中最好的那个状态**而不是最后那个。"
                "势垒读不出来时说「判不了」，不当成「没变好」—— 两者的下一步不同。",
                "Condition the tip on a noble-metal substrate, **verifying with the I-Z barrier at every step whether it got better**, rather than going by feel or by looking at images.\n\n**The first thing is to measure the baseline; if it already meets the target, do not do a single step**, and return `already_clean`. A typical case: asked to 'clean the tip' while the baseline φ was already clean - the correct action is **to do nothing**.\n\n**It treats a typical accident**: to improve a flattened I-V, a few shallow pokes and pulses are done without **ever deciding beforehand what 'better' means**; the I-V does not change at all, while the row-to-row jitter σ rises by two orders of magnitude - the tip is ruined. The same mistake is easy to repeat.\n\nThe ladder is **poke first, then pulse** (a poke within 2 nm is much gentler than a pulse), the depth only 0.5-0.8 nm (1.2/1.6 nm were measured to be worse), and **after every pulse a stabilising poke is required**.\n\nThree guardrails: stop after %d steps in a row without improvement; **stop immediately when it gets worse**; at the end hand over **the best state seen during the process**, not the last one. When the barrier cannot be read it says 'cannot judge' and does not count it as 'did not improve' - the two call for different next steps.")
                % _MAX_STALE
            ),
            parameters=[
                ParameterSpec(
                    name="target_phi_ev", type="float", unit="eV",
                    description=(tr("目标势垒。默认 %.1f eV（= MeasureBarrierHeight 判 clean 的线）。"
                                 "真空隧穿约 4–5 eV。",
                                 "Target barrier. Default %.1f eV (= the line at which MeasureBarrierHeight judges clean). Vacuum tunnelling is about 4-5 eV.") % _DEFAULT_TARGET_EV),
                    required=False, default=_DEFAULT_TARGET_EV,
                    min_value=0.5, max_value=6.0),
                ParameterSpec(
                    name="max_steps", type="int",
                    description=tr("最多做几步修针动作。到点仍未达标就如实返回未达标。", "Maximum number of tip-conditioning steps. If the target is still not met by then, honestly return not reached."),
                    required=False, default=7, min_value=1, max_value=20),
                ParameterSpec(
                    name="bias_v", type="float", unit="V",
                    description=tr("测势垒用的偏压。留空 = 用当前偏压。", "Bias used to measure the barrier. Empty = use the current bias."),
                    required=False, min_value=-10.0, max_value=10.0),
            ],
            estimated_duration_s=2400.0,
            rollback_skill="WithdrawTip",
            composition_level=2,
            tags=["tip", "conditioning", "barrier", "修针", "有判据"],
        )

    # ── 动作 ──────────────────────────────────────────────────────────
    @staticmethod
    def _poke(context, depth_nm):
        return context.run("TipShape", {
            "tip_lift_m": -abs(depth_nm) * 1e-9,
            "lift_height_m": abs(depth_nm) * 1e-9,
            "lift_time_1_s": 0.1, "lift_time_2_s": 0.1,
            "bias_lift_v": 0.02, "change_bias": False,
            "bias_settling_s": 0.5, "end_wait_s": 0.2,
            "restore_feedback": True})

    def _pulse(self, context, volts):
        res = context.run("BiasPulse", {"bias_v": float(volts), "width_s": 0.05,
                                        "z_hold": True, "absolute": True})
        # ⭐ **脉冲之后必须扎针稳定** —— 经验规则。
        # 对照实测（三方向幅值平衡度）：只脉冲不扎针的均值明显更低，脉冲后补扎针更高。
        self._poke(context, 0.5)
        return res

    def _measure(self, context, bias_v):
        p = {}
        if bias_v is not None:
            p["bias_v"] = float(bias_v)
        res = context.run("MeasureBarrierHeight", p)
        d = getattr(res, "data", None) or {}
        return d.get("phi_ev"), d.get("verdict")

    # ── 主流程 ────────────────────────────────────────────────────────
    def execute(self, context, params: dict) -> SkillResult:
        target = float(params.get("target_phi_ev") or _DEFAULT_TARGET_EV)
        max_steps = int(params.get("max_steps") or 7)
        bias_v = params.get("bias_v")

        phi0, verdict0 = self._measure(context, bias_v)
        base = {"phi_ev": phi0, "verdict": verdict0}
        if phi0 is None:
            return SkillResult(
                skill_name="CleanTipUntilBarrier", success=False,
                error=(tr("基线势垒**判不了** —— 在没有判据的情况下动针尖，正是这个技能"
                       "存在的理由所要避免的。先把 I–Z 量出来再说。",
                       "The baseline barrier **cannot be judged** - moving the tip without a criterion is exactly what this skill exists to avoid. Measure I-Z first.")),
                data={"baseline": base, "steps": []})

        # ★ 已达标就一步都不做。这是本技能最重要的一条。
        if phi0 >= target:
            return SkillResult(
                skill_name="CleanTipUntilBarrier", success=True,
                data={"outcome": "already_clean", "baseline": base,
                      "phi_ev": phi0, "target_phi_ev": target, "steps": [],
                      "message": (tr("基线 φ=%.2f eV 已达标（≥%.2f）—— **一步都没做**。"
                                  "针尖不需要修；在没有判据支持时动针尖，"
                                  "只会把好针尖弄坏。",
                                  "Baseline φ=%.2f eV already meets the target (>=%.2f) - **not a single step was done**. The tip does not need conditioning; moving the tip without criterion support only ruins a good tip.") % (phi0, target))})

        best_phi, best_at = phi0, "baseline"
        steps, stale = [], 0
        for i in range(min(max_steps, len(_LADDER))):
            kind, arg = _LADDER[i]
            act = self._pulse(context, arg) if kind == "pulse" else self._poke(context, arg)
            ok = bool(getattr(act, "success", False))
            # 动作可能改变工作点，每步之后回读并记录实际偏压。
            br = context.run("GetBias", {})
            bias_after = (getattr(br, "data", None) or {}).get("bias_v")
            phi, verdict = self._measure(context, bias_v)
            row = {"step": i + 1, "action": kind, "arg": arg, "action_ok": ok,
                   "bias_after_v": bias_after, "phi_ev": phi, "verdict": verdict}
            steps.append(row)
            if not ok:
                row["stopped"] = tr("动作失败", "action failed")
                break
            if phi is None:
                # 「没测到」≠「没变好」：不计入 stale，也不当成变差
                row["note"] = tr("势垒判不了 —— 不计入改善判断", "barrier cannot be judged - not counted in the improvement judgement")
                continue
            if phi > best_phi * (1.0 + _IMPROVE_FRAC):
                best_phi, best_at, stale = phi, "step%d" % (i + 1), 0
                row["improved"] = True
            else:
                stale += 1
            if phi >= target:
                row["stopped"] = tr("达标", "target reached")
                break
            if phi < best_phi * (1.0 - _IMPROVE_FRAC) and best_at != "baseline":
                row["stopped"] = tr("变差 —— 立刻停", "got worse - stop immediately")
                break
            if stale >= _MAX_STALE:
                row["stopped"] = tr("连续 %d 步没改善", "%d steps in a row without improvement") % _MAX_STALE
                break

        reached = best_phi >= target
        return SkillResult(
            skill_name="CleanTipUntilBarrier", success=True,
            data={
                "outcome": "reached" if reached else "not_reached",
                "baseline": base, "target_phi_ev": target,
                "best_phi_ev": best_phi, "best_at": best_at,
                "final_phi_ev": steps[-1].get("phi_ev") if steps else phi0,
                "n_steps": len(steps), "steps": steps,
                "message": (
                    tr("φ %.2f → %.2f eV（最好出现在 %s），%s目标 %.2f。"
                    "**交出的是过程中最好的那个状态，不是最后那个。**",
                    "φ %.2f -> %.2f eV (best at %s), %s the target %.2f. **What is handed over is the best state seen during the process, not the last one.**")
                    % (phi0, best_phi, best_at,
                       tr("已达到", "reached") if reached else tr("未达到", "did not reach"), target)),
            })
