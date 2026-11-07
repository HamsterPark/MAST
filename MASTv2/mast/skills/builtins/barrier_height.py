# -*- coding: utf-8 -*-
"""MeasureBarrierHeight: estimate an effective tunnelling barrier from I–Z data.

The fitted barrier describes the measured junction and can help compare
conditions. A low value alone does not identify contamination or its location.
Compare multiple positions and repeat measurements at the same position to
distinguish spatial variation from repeatability and drift. Instrument
calibration and model assumptions remain part of that interpretation.
"""

from __future__ import annotations

import logging
import math
import time

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

#: κ = sqrt(2 m φ)/ħ 的工程写法：κ[1/nm] = 5.123 · sqrt(φ[eV])。
_KAPPA_PER_SQRT_EV = 5.123

#: 真空隧穿的参照（金属功函数 4–5 eV）。判读时用它做分母。
_VACUUM_PHI_EV = 4.0

#: 默认退开序列（nm）。前段密、后段疏 —— 电流按指数掉，等间距会把大部分点浪费在噪声底上。
_DEFAULT_OFFSETS_NM = (0.0, 0.10, 0.20, 0.30, 0.40, 0.55)

# I–Z 获取是阻塞调用，超时预算需覆盖采集与传输，并保留协议处理余量。
# 不能把传输等待超时误报为势垒测量结论；具体预算需按设备行为验证。
_MAX_SWEEP_S = 3.0

#: 方向试探用的小步长（nm）。够大到能看出电流变化，够小到即使符号搞反也不会撞针。
_PROBE_NM = 0.10


def _fit_kappa(points, noise_floor_pa):
    """对退开距离与电流幅度拟合 ln|I| = -2κ·d + c，只使用高于噪声底的有效点。触底读数不能当作电流值加入拟合，否则会拉平斜率。"""
    import numpy as np

    usable = [(d, i) for d, i in points if i is not None and i > noise_floor_pa]
    if len(usable) < 3:
        return None, usable
    d = np.asarray([p[0] for p in usable], dtype=float)
    i = np.asarray([p[1] for p in usable], dtype=float)
    slope, intercept = np.polyfit(d, np.log(i), 1)
    if not math.isfinite(slope) or slope >= 0:
        # 斜率非负 = 退开反而电流变大，物理上讲不通（多半是方向弄反或针尖跳了）
        return None, usable
    kappa = -float(slope) / 2.0
    resid = np.log(i) - (slope * d + intercept)
    return {
        "kappa_per_nm": kappa,
        "phi_ev": (kappa / _KAPPA_PER_SQRT_EV) ** 2,
        "decade_nm": float(math.log(10.0) / abs(slope)),
        "n_fit": len(usable),
        "fit_resid_rms": float(np.sqrt(np.mean(resid ** 2))),
    }, usable


def _verdict(phi_ev):
    """将 φ 转为条件性解释。阈值是工作流默认值，实际解释仍需结合结状态与测量条件。"""
    if phi_ev >= 3.0:
        return "clean", (tr("φ %.2f eV 接近真空值 —— 针尖与表面都干净，"
                         "原子分辨与 STS 值得投入。",
                         "φ %.2f eV is close to the vacuum value - tip and surface are both clean, atomic resolution and STS are worth the investment.") % phi_ev)
    if phi_ev >= 1.0:
        return "contaminated", (
            tr("φ %.2f eV 只有真空值的 %.0f%% —— 针尖与样品之间隔着一层东西。"
            "原子分辨与 STS 大概率做不成；**先处理污染，别先投入整夜**。",
            "φ %.2f eV is only %.0f%% of the vacuum value - there is a layer between tip and sample. Atomic resolution and STS will most likely not work; **deal with contamination first, do not invest a whole night first**.")
            % (phi_ev, 100.0 * phi_ev / _VACUUM_PHI_EV))
    return "not_vacuum", (
        tr("φ %.2f eV —— 这已经不是真空隧穿结。在这根针尖／这片表面上排查 STS 是浪费时间；"
        "修针改的是形状，改不了中间那层东西。",
        "φ %.2f eV - this is no longer a vacuum tunnelling junction. Troubleshooting STS on this tip / this surface is a waste of time; tip conditioning changes the shape but cannot change the layer in between.") % phi_ev)


class MeasureBarrierHeight(BaseSkill):
    """Measure the effective tunnelling barrier from an I–Z spectrum."""

    #: 每一步之间的稳定时间（s）。类属性，好让测试缩短它。
    _settle_s = 0.4

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="MeasureBarrierHeight",
            version="1.0.0",
            category=SkillCategory.WRITE,
            # 只退开、不靠近，而且退开方向是**实测确认**过才走的（见 direction_check）。
            # 最坏情况是针尖离开隧穿区，电流归零 —— 不撞针。
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("在**当前针尖位置**量有效隧穿势垒 φ：把针尖一档一档退开，每档读一次电流，"
                "对 ln|I| 拟合斜率得 κ，再换算成 φ。**五分钟给一个能据以决策的数。**"
                "\n\n**进针之后、投入一整夜之前，先跑这一条。** φ 接近 4 eV 才说明针尖和"
                "表面都干净；φ ≈ 1 eV 意味着中间隔着一层东西，那时原子分辨与 STS 大概率"
                "做不成，而且**修针无效** —— 修针改的是针尖形状，改不了那层东西。"
                "漏掉这一步时，可能白白花很长时间排查 STS 链路（链路本身并无问题）"
                "和反复修针（全部无效），最后才测出势垒其实很低。"
                "\n\n**它比看形貌图硬。** 形貌图上的「脏」是观感，而 RMS／长宽比这类量在帧"
                "不平坦时描述的是地形不是针尖。I–Z 直接量隧穿结本身。"
                "\n\n**读不出来时说「判不了」，不给数。** 可用点不足 3 个（电流太快掉进噪声底，"
                "或者根本没在隧穿）⇒ verdict=undetermined。那和「势垒很低」是两回事，"
                "驱动的下一步也不同。"
                "\n\n单点测不出污染在针尖侧还是表面侧 —— 要回答那个，得在多个位置各测一条，"
                "**并在同一位置重复几次作为噪声标尺**（位置间散布要明显大于同点重复散布，"
                "才说明空间结构是真的）。",
                "Measure the effective tunnelling barrier φ at the **current tip position**: withdraw the tip step by step, read the current at each step, fit the slope of ln|I| to get κ, then convert to φ. **Five minutes gives a number to base decisions on.**\n\n**Run this right after engaging and before investing a whole night.** φ near 4 eV means both tip and surface are clean; φ ≈ 1 eV means there is a layer in between, in which case atomic resolution and STS most likely will not work, and **tip conditioning is ineffective** - conditioning changes the tip shape, not that layer. Skipping this step can cost a long time troubleshooting the STS chain (which is fine) and rounds of tip conditioning (which cannot help).\n\n**It is harder evidence than looking at a topography image.** 'Dirty' on a topography image is an impression, and quantities like RMS / aspect ratio describe the terrain, not the tip, when the frame is not flat. I-Z measures the tunnelling junction itself.\n\n**When it cannot be read, it says 'cannot judge' and gives no number.** Fewer than 3 usable points (current falls into the noise floor too fast, or there is no tunnelling at all) => verdict=undetermined. That is not the same as 'low barrier', and it drives a different next step.\n\nA single point cannot tell whether the contamination is on the tip side or the surface side - to answer that, measure one curve at each of several positions, **and repeat a few times at the same position as a noise ruler** (the scatter between positions must clearly exceed the repeat scatter at one point to show the spatial structure is real).")
            ),
            parameters=[
                ParameterSpec(
                    name="bias_v",
                    type="float",
                    description=(
                        tr("测量偏压。留空 = 用当前偏压（本技能**不替调用方决定探测什么电子态**）。"
                        "取一个电流稳定、绝对值够大的工作点即可，量的是距离依赖不是能谱。",
                        "Measurement bias. Empty = use the current bias (this skill **does not decide for the caller which electronic state to probe**). Any working point with a stable, sufficiently large current will do; what is measured is the distance dependence, not the spectrum.")),
                    required=False,
                    unit="V",
                    min_value=-10.0,
                    max_value=10.0,
                ),
                ParameterSpec(
                    name="offsets_nm",
                    type="str",
                    description=(
                        tr("退开距离序列，逗号分隔（nm，正数=远离样品）。留空用默认 "
                        "'0,0.1,0.2,0.3,0.4,0.55' —— 前密后疏，因为电流按指数掉，"
                        "等间距会把大半点数浪费在噪声底上。",
                        "Withdrawal distance sequence, comma-separated (nm, positive = away from the sample). Empty uses the default '0,0.1,0.2,0.3,0.4,0.55' - dense first, sparse later, because the current falls exponentially and equal spacing would waste most points on the noise floor.")),
                    required=False,
                ),
                ParameterSpec(
                    name="noise_floor_pa",
                    type="float",
                    description=(
                        tr("噪声底（pA）。低于它的读数算「读不到」，**不参与拟合**。"
                        "留空 = 自动量（在最远那一档上读）。"
                        "手动给值只在自动量到的底明显不对时才需要。",
                        "Noise floor (pA). Readings below it count as 'unreadable' and **do not take part in the fit**. Empty = measure automatically (read at the farthest step). A manual value is only needed when the automatically measured floor is clearly wrong.")),
                    required=False,
                    min_value=0.0,
                ),
                ParameterSpec(
                    name="points_per_step",
                    type="int",
                    description=tr("每一档里的偏压采样点数（取平均用）。默认 9。", "Number of bias sampling points within each step (for averaging). Default 9."),
                    required=False,
                    default=9,
                    min_value=3,
                    max_value=101,
                ),
            ],
            estimated_duration_s=180.0,
            composition_level=1,
            tags=["spectroscopy", "barrier", "work-function", "diagnostic",
                  "势垒", "iz", "tip-health"],
        )

    # ── 内部：STS 的工作点自备 ──────────────────────────────────────────
    def _arm_sts(self, context, points_per_step):
        """显式配置本次 STS 通道与时序，不依赖上游遗留状态。时序按 _MAX_SWEEP_S 约束，避免阻塞采集超过通信预算。"""
        integ = max(0.002, min(0.010, _MAX_SWEEP_S / max(points_per_step, 1) * 0.6))
        settl = integ * 0.5
        est = points_per_step * (integ + settl)
        chan = context.run("ConfigureSTSChannels", {"channel_indexes": "0,24,30"})
        tim = context.run("ConfigureSTSTiming", {
            "integration_s": integ,
            "settling_s": settl,
            "init_settling_s": 0.03,
            "z_avg_time_s": 0.02,
            "end_settling_s": 0.004,
            # ConfigureSTSTiming 的必需参数需完整传入；校验失败后不能假定工作点已设置成功。
            # 否则后续采集会继续使用旧时序。
            "max_slew_rate_v_s": 1000.0,
            "z_offset_m": 0.0,
        })
        return {
            "channels_ok": bool(getattr(chan, "success", False)),
            "timing_ok": bool(getattr(tim, "success", False)),
            "integration_s": integ,
            "settling_s": settl,
            "est_sweep_s": est,
            "sweep_budget_s": _MAX_SWEEP_S,
        }

    def _read_at(self, context, offset_nm, bias_v, npts):
        """在给定退开量处采一小段偏压扫，返回 |I| 的均值（pA）。"""
        import numpy as np

        span = max(0.05, abs(bias_v) * 0.2)
        context.run("ConfigureSTS", {
            "start_v": bias_v - span / 2.0,
            "end_v": bias_v + span / 2.0,
            "num_points": int(npts),
            "z_offset_m": float(offset_nm) * 1e-9,
        })
        res = context.run("AcquireSTS", {"save_basename": ""})
        data = getattr(res, "data", None) or {}
        if not data.get("spectrum_parsed"):
            return None
        cur = data.get("Current (A)")
        if not cur:
            return None
        arr = np.abs(np.asarray(cur, dtype=float))
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return None
        return float(np.mean(arr)) * 1e12

    def _check_direction(self, context, bias_v, npts):
        """用小幅探测确认退开方向：电流减小才继续，增大则翻转符号，变化不明确时拒绝继续。不能根据未经确认的 z_offset 符号约定直接执行完整序列。"""
        base = self._read_at(context, 0.0, bias_v, npts)
        if base is None or base <= 0:
            return None, {"reason": tr("基准点读不到电流", "Baseline point current unreadable"), "base_pa": base}
        probe = self._read_at(context, _PROBE_NM, bias_v, npts)
        if probe is None:
            return None, {"reason": tr("试探点读不到电流", "Probe point current unreadable"), "base_pa": base}
        ratio = probe / base
        info = {"base_pa": base, "probe_pa": probe, "ratio": ratio,
                "probe_nm": _PROBE_NM}
        # ⚠ 符号只有一个真源：写进 info 的那个就是返回的那个。
        # 早先写成「info 里记一份、return 一个字面量」，变异验证发现改了记录
        # 行为却不变 —— 那种双真源哪天漂开，报告里的 sign 就会说谎。
        if ratio < 0.85:
            info["sign"] = +1.0
            return info["sign"], info
        if ratio > 1.2:
            # 正 offset 反而靠近 —— 本机约定与预期相反，翻过来用
            info["sign"] = -1.0
            info["note"] = tr("正 z_offset 使电流上升 ⇒ 本机正号是靠近，已翻转", "A positive z_offset increases the current => on this machine positive means approaching; flipped")
            return info["sign"], info
        info["reason"] = (tr("退开 %.2f nm 电流只变了 %.0f%% —— 距离依赖太弱，"
                          "多半没在隧穿区，不继续。",
                          "Withdrawing %.2f nm changed the current by only %.0f%% - distance dependence too weak, probably not in the tunnelling regime, not continuing.") % (_PROBE_NM, 100 * (ratio - 1)))
        return None, info

    # ── 主流程 ────────────────────────────────────────────────────────
    def execute(self, context, params: dict) -> SkillResult:
        import numpy as np

        npts = int(params.get("points_per_step") or 9)
        raw_offsets = str(params.get("offsets_nm") or "").strip()
        if raw_offsets:
            try:
                offsets = [abs(float(t)) for t in raw_offsets.split(",") if t.strip()]
            except ValueError:
                return SkillResult(
                    skill_name="MeasureBarrierHeight", success=False,
                    error=tr("offsets_nm 解析不了：%r（要的是逗号分隔的数字，单位 nm）", "offsets_nm could not be parsed: %r (a comma-separated list of numbers in nm is expected)") % raw_offsets)
        else:
            offsets = list(_DEFAULT_OFFSETS_NM)
        offsets = sorted(set(offsets))
        if len(offsets) < 4:
            return SkillResult(
                skill_name="MeasureBarrierHeight", success=False,
                error=tr("至少要 4 档退开量才拟合得动（给了 %d 档）。", "At least 4 withdrawal steps are needed for the fit to work (%d given).") % len(offsets))

        bias_v = params.get("bias_v")
        if bias_v is None:
            rec = context.run("GetBias", {})
            bias_v = (getattr(rec, "data", None) or {}).get("bias_v")
            if bias_v is None:
                return SkillResult(
                    skill_name="MeasureBarrierHeight", success=False,
                    error=tr("没给 bias_v，也读不到当前偏压 —— 不猜一个值去量。", "bias_v was not given and the current bias cannot be read - not guessing a value to measure at."))
        else:
            context.run("SetBias", {"bias_v": float(bias_v)})
            time.sleep(self._settle_s)
        bias_v = float(bias_v)

        arm = self._arm_sts(context, npts)
        sign, dirinfo = self._check_direction(context, bias_v, npts)
        if sign is None:
            return SkillResult(
                skill_name="MeasureBarrierHeight", success=True,
                data={"verdict": "undetermined", "direction_check": dirinfo,
                      "sts_setup": arm, "bias_v": bias_v,
                      "message": (tr("量不了势垒：", "Cannot measure the barrier:") + str(dirinfo.get("reason", tr("方向确认失败", "direction check failed"))) +
                                  tr(" —— 这不是「势垒很低」，是「没测到」，两者驱动的下一步不同。", " - this is not 'low barrier', it is 'not measured'; the two drive different next steps."))})

        points = []
        for off in offsets:
            val = self._read_at(context, sign * off, bias_v, npts)
            points.append((off, val))
            time.sleep(self._settle_s * 0.5)
        context.run("ConfigureSTS", {"start_v": bias_v, "end_v": bias_v,
                                     "num_points": int(npts), "z_offset_m": 0.0})

        vals = [v for _, v in points if v is not None]
        if not vals:
            return SkillResult(
                skill_name="MeasureBarrierHeight", success=True,
                data={"verdict": "undetermined", "points": points, "sts_setup": arm,
                      "direction_check": dirinfo, "bias_v": bias_v,
                      "message": tr("所有档位都没读到电流 —— 没测到，不是势垒低。", "No current was read at any step - not measured, not a low barrier.")})

        floor = params.get("noise_floor_pa")
        floor_src = "explicit"
        if floor is None:
            tail = [v for _, v in points[-2:] if v is not None]
            floor = (float(np.mean(tail)) * 2.0) if tail else 0.0
            floor_src = tr("auto(最远两档均值×2)", "auto (mean of the two farthest steps x2)")
        floor = float(floor)

        fit, usable = _fit_kappa(points, floor)
        common = {
            "bias_v": bias_v, "points": points, "noise_floor_pa": floor,
            "noise_floor_source": floor_src, "n_usable": len(usable),
            "sts_setup": arm, "direction_check": dirinfo,
            "vacuum_reference_ev": _VACUUM_PHI_EV,
        }
        if fit is None:
            return SkillResult(
                skill_name="MeasureBarrierHeight", success=True,
                data=dict(common, verdict="undetermined", message=(
                    tr("可用点只有 %d 个（噪声底 %.3f pA 之上），拟合不动 —— **判不了**。"
                    "电流可能掉得太快（把 offsets 收密些再来），也可能根本没在隧穿。"
                    "这和「势垒很低」是两回事。",
                    "Only %d usable points (above the noise floor of %.3f pA), the fit cannot run - **cannot judge**. The current may fall too fast (use a denser offsets and retry), or there may be no tunnelling at all. This is not the same as 'low barrier'.") % (len(usable), floor))))

        verdict, msg = _verdict(fit["phi_ev"])
        return SkillResult(
            skill_name="MeasureBarrierHeight", success=True,
            data=dict(common, verdict=verdict, message=msg, **fit,
                      phi_fraction_of_vacuum=fit["phi_ev"] / _VACUUM_PHI_EV,
                      vacuum_decade_nm=math.log(10.0) / (2 * _KAPPA_PER_SQRT_EV
                                                         * math.sqrt(_VACUUM_PHI_EV))))
