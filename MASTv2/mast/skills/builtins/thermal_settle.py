# -*- coding: utf-8 -*-
"""WaitForThermalSettle：用温度变化速率判断是否趋稳。

温度低于某个绝对值时仍可能快速变化，绝对阈值不能单独代表热稳定。
接近目标温度的缓慢漂移同样需要由速率描述；速率由最近多个点的线性拟合估计。
可选的绝对温度上限与速率上限同时检查，默认值须按测量条件验证。

只读取温度并等待，不控温、不操作加热器。读不到温度时明确报告不可用，
不能把未知状态当作稳定。
"""

from __future__ import annotations

import logging
import time

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill
from mast.i18n import tr

logger = logging.getLogger(__name__)

# 默认稳定速率上限（K/min），为未标定的工作流参数；应按当前热环境与测量要求验证。
_DEFAULT_RATE_K_PER_MIN = 0.03

#: 拟合速率至少要几个采样点 —— 两点连线算出来的「速率」全是噪声。
_MIN_SAMPLES = 5

#: 连续读不到温度多少次就放弃（每次都当「稳了」是最危险的降级）。
_MAX_MISSES = 5


def rate_k_per_min(samples):
    """对 (t_s, T_K) 线性拟合，返回 K/min。点不够返回 None（**不是 0**）。"""
    import numpy as np

    pts = [(t, v) for t, v in samples if v is not None]
    if len(pts) < _MIN_SAMPLES:
        return None
    a = np.asarray(pts, dtype=float)
    if a[-1, 0] - a[0, 0] <= 0:
        return None
    return float(np.polyfit(a[:, 0], a[:, 1], 1)[0] * 60.0)


class WaitForThermalSettle(BaseSkill):
    """Wait until the temperature stops changing, not until it drops below a number."""

    #: 轮询间隔（s）。类属性，测试可以缩短。
    _poll_s = 25.0

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="WaitForThermalSettle",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("等温度**不再变**再开工。判据是**变化速率**，不是绝对值。"
                "\n\n换样品后温度会从几十 K 掉回基温，途中任何时刻都可能「已经低于阈值」"
                "却仍在快速下降。可能还要过几分钟才真正稳住。"
                "在这段时间里进针、量势垒、扫图，"
                "读到的都是跟着温度走的东西 —— Z 在漂、结阻在变、势垒带着趋势。"
                "\n\n**同时看两条**：温度低于 `max_temp_k`（可选，留空=不管绝对值）"
                "**并且**速率的绝对值低于 `max_rate_k_per_min`。速率用最近若干点线性拟合，"
                "少于 %d 点不作数（两点连线算出的「速率」全是噪声）。"
                "\n\n**读不到温度就说读不到**，不假装稳定 —— 那会让调用方在未知状态下开工。"
                "只读温度，不控温、不碰仪器。", "Wait until the temperature **stops changing** before starting work. The criterion is the **rate of change**, not the absolute value.\n\nAfter a sample change the temperature falls from tens of K back to base temperature, and at any moment on the way it may be \"already below the threshold\" while still dropping fast. It may take several more minutes to truly settle. If you approach, measure the barrier and scan during that time, everything you read follows the temperature - Z drifts, junction resistance changes, the barrier carries a trend.\n\n**Look at two things together**: the temperature below `max_temp_k` (optional, empty = ignore the absolute value) **and** the absolute value of the rate below `max_rate_k_per_min`. The rate is a linear fit over the most recent points; fewer than %d points do not count (a \"rate\" computed from two points is all noise).\n\n**If the temperature cannot be read, say it cannot be read**, do not pretend it is stable - that would let the caller start work in an unknown state. Reads temperature only, does not control temperature or touch the instrument.") % _MIN_SAMPLES
            ),
            parameters=[
                ParameterSpec(
                    name="max_rate_k_per_min", type="float", unit="K/min",
                    description=(tr("算「稳了」的速率上限（绝对值）。默认 %.2f —— "
                                 "到这个量级时 Z 与结阻都不再跟着走。", "Upper limit of the rate (absolute value) that counts as \"settled\". Default %.2f - at this order of magnitude, Z and junction resistance no longer followed it.")
                                 % _DEFAULT_RATE_K_PER_MIN),
                    required=False, default=_DEFAULT_RATE_K_PER_MIN,
                    min_value=0.001, max_value=5.0),
                ParameterSpec(
                    name="max_temp_k", type="float", unit="K",
                    description=(tr("温度上限。**留空 = 只看速率**（比如在 77 K 上工作时"
                                 "就不该要求它降到 5 K）。给了值就是两条**并且**。", "Upper temperature limit. **Empty = look at the rate only** (e.g. when working at 77 K you should not require it to drop to 5 K). If given, it is both conditions **together**.")),
                    required=False, min_value=0.0, max_value=500.0),
                ParameterSpec(
                    name="timeout_s", type="float", unit="s",
                    description=tr("最长等待。到点仍未稳就如实返回未稳，**不假装成功**。", "Maximum wait. If still not settled by then, honestly return not settled, **do not pretend success**."),
                    required=False, default=3600.0,
                    min_value=10.0, max_value=86400.0),
                ParameterSpec(
                    name="window", type="int",
                    description=tr("拟合速率用的采样点数（滑动窗口）。", "Number of sample points used to fit the rate (sliding window)."),
                    required=False, default=6,
                    min_value=_MIN_SAMPLES, max_value=60),
            ],
            estimated_duration_s=600.0,
            composition_level=0,
            tags=["thermal", "wait", "settle", "温度", "换样品"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        max_rate = abs(float(params.get("max_rate_k_per_min")
                             or _DEFAULT_RATE_K_PER_MIN))
        max_temp = params.get("max_temp_k")
        max_temp = float(max_temp) if max_temp is not None else None
        budget = float(params.get("timeout_s") or 3600.0)
        window = max(_MIN_SAMPLES, int(params.get("window") or 6))

        t0 = time.monotonic()
        samples, misses = [], 0
        while time.monotonic() - t0 < budget:
            res = context.run("GetTemperature", {})
            val = (getattr(res, "data", None) or {}).get("value_k")
            if val is None:
                misses += 1
                if misses >= _MAX_MISSES:
                    return SkillResult(
                        skill_name="WaitForThermalSettle", success=False,
                        error=(tr("连续 %d 次读不到温度 —— **不当成「稳了」**。"
                               "读不到与稳定是两件事，把前者当后者会让调用方"
                               "在未知热状态下开工。", "Could not read the temperature %d times in a row - **not counted as \"settled\"**. Being unreadable and being stable are two different things; treating the former as the latter would let the caller start work in an unknown thermal state.") % misses),
                        data={"samples": samples, "misses": misses})
            else:
                misses = 0
                samples.append((time.monotonic() - t0, float(val)))
                samples = samples[-window:]
                rate = rate_k_per_min(samples)
                if rate is not None:
                    temp_ok = (max_temp is None) or (val <= max_temp)
                    if temp_ok and abs(rate) <= max_rate:
                        return SkillResult(
                            skill_name="WaitForThermalSettle", success=True,
                            data={"settled": True, "temperature_k": float(val),
                                  "rate_k_per_min": rate,
                                  "elapsed_s": time.monotonic() - t0,
                                  "n_samples": len(samples),
                                  "max_rate_k_per_min": max_rate,
                                  "max_temp_k": max_temp,
                                  "message": (tr("%.3f K，速率 %+.3f K/min —— 稳了，"
                                              "可以开工。", "%.3f K, rate %+.3f K/min - settled, ready to start work.") % (val, rate))})
            time.sleep(self._poll_s)

        last = samples[-1][1] if samples else None
        rate = rate_k_per_min(samples)
        return SkillResult(
            skill_name="WaitForThermalSettle", success=False,
            error=(tr("等了 %.0f min 仍未稳：%s，速率 %s。**没有假装成功** —— "
                   "在这个状态下开工，读到的东西会跟着温度走。", "Waited %.0f min and still not settled: %s, rate %s. **No pretending success** - starting work in this state, what you read will follow the temperature.")
                   % (budget / 60.0,
                      ("%.3f K" % last) if last is not None else tr("读不到温度", "temperature unreadable"),
                      ("%+.3f K/min" % rate) if rate is not None else tr("算不出", "cannot compute"))),
            data={"settled": False, "temperature_k": last,
                  "rate_k_per_min": rate, "elapsed_s": time.monotonic() - t0,
                  "n_samples": len(samples)})
