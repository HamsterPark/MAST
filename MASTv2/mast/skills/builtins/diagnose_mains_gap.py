# -*- coding: utf-8 -*-
"""``DiagnoseMainsGapModulation`` -- how many pm is the tunnelling gap modulated at mains harmonics?

Read-only: pulls Osci1T traces of the tunnelling current through :class:`AcquireOsciTrace` (the
scope channel must already show the current; ``signal_index`` can assign it) and never changes
bias, setpoint, feedback or position.  Numerical core: :mod:`mast.monitoring.mains_gap`.
"""
from __future__ import annotations

import logging

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.skills.base import BaseSkill
from mast.skills.builtins.acquire_osci_trace import AcquireOsciTrace

logger = logging.getLogger(__name__)

_NAME = "DiagnoseMainsGapModulation"

__all__ = ["DiagnoseMainsGapModulation"]


def guidance(res: dict) -> list:
    """Human guidance lines for a successful analysis dict."""
    v = res["verdict"]
    off = res["offset_from_harmonic_hz"]
    out = []
    if v == "ok":
        out.append(tr("调制 ≲10 pm：原子衬度通常仍可见，无需处理。",
                      "Modulation <=10 pm: atomic contrast is normally still visible; nothing to fix."))
    elif v == "marginal":
        out.append(tr("10–35 pm：提高设定电流（约 300 pA）并做多帧平均仍可用。",
                      "10-35 pm: usable with a higher setpoint (~300 pA) and multi-frame averaging."))
    else:
        out.append(tr("> 35 pm：先找干扰源，不要去修针尖；停在小电流等人处理。",
                      "> 35 pm: fix the source first, do not condition the tip; park at a small current."))
    if res["mains_locked"]:
        out.append(tr("主导频率锁在电网谐波上（偏差 %.2f Hz）。" % off,
                      "Dominant frequency is locked to a mains harmonic (offset %.2f Hz)." % off))
    else:
        out.append(tr("主导频率不在电网谐波上（偏差 %.2f Hz）：更像机械/其他来源，不是电网。" % off,
                      "Dominant frequency is NOT on a mains harmonic (offset %.2f Hz): more likely "
                      "mechanical / another source." % off))
    out.append(tr("有效性前提（任何结论都先看这条，读数小时尤其要看）：上面的 pm 值假设隧道结正常，"
                  "即 2κ≈19 nm⁻¹（参数 two_kappa_nm）。任何针尖事件之后（撞针、重新进针、对扫描头做机械操作，"
                  "例如锁紧悬挂），先验证 2κ：关反馈、冻结 Z，以小步（25–150 pm）回退 Z 并观察电流。"
                  "正常约 10–25 nm⁻¹；若电流几乎不变（2κ ≲ 3 nm⁻¹），说明针尖尖端没有跟着压电走，"
                  "此时调制读数很小也毫无意义。",
                  "Validity precondition (check this for every verdict, above all when the reading is small): "
                  "the pm values assume a normal tunnelling junction, 2kappa ~ 19 /nm (parameter "
                  "two_kappa_nm). After any tip event (crash, re-approach, mechanical work on the head such "
                  "as locking the suspension), first verify 2kappa: feedback off, Z frozen, retract Z in "
                  "small steps (25-150 pm) and watch the current. Normal is about 10-25 /nm. If the current "
                  "barely changes (2kappa <~ 3 /nm), the tip apex is not following the piezo and a small "
                  "modulation reading means nothing."))
    if v == "fix_source":
        out.append(tr("现场检查项之一：扫描头悬挂是否真的悬空（锁紧再松开；先退针，之后再重新进针）。"
                      "此项未经验证，是否有影响未知。整夜不降说明是常开设备，不是白天才开的设备。",
                      "One of the on-site checks: whether the scan-head suspension hangs free (lock + release; "
                      "retract the tip first, re-approach afterwards). This is unverified; whether it matters "
                      "is unknown. A modulation that does not drop overnight points to an always-on source, "
                      "not daytime equipment."))
    out.append(tr("区分结上交流电压与机械振动：在工作偏压下关反馈、确认 Z 已冻结，再把偏压设为 0 V"
                  "（带隙内，或任何不产生隧穿电流的偏压）并记录电流。出现电网谐波正弦 → 结上有交流电压"
                  "（查偏压线 / 屏蔽 / 接地）；没有 → 机械振动或压电高压（HVA）纹波"
                  "（用示波器看 HVA 输出，并查三相设备：6 脉冲整流纹波正是 300 Hz）。"
                  "重新开反馈前先恢复原偏压，绝不在带隙内闭环。不要用幅度随偏压的变化来判断："
                  "pm 值用固定 2κ 换算，而 2κ 随偏压变化，幅度本身也随时间漂移。",
                  "Tell an AC voltage across the junction from mechanical vibration: at the working "
                  "bias switch the feedback off, confirm Z is frozen, set the bias to 0 V (inside the "
                  "band gap, or any bias where no tunnelling current flows) and record the current. A "
                  "sinusoid at the mains harmonic -> AC voltage across the junction (check bias cable / "
                  "shield / grounding). No sinusoid -> mechanical vibration or piezo high-voltage (HVA) "
                  "ripple (check the HVA outputs with a scope, and three-phase equipment: a 6-pulse "
                  "rectifier ripple is 300 Hz). Restore the original bias before switching the feedback "
                  "back on, and never close the loop inside a band gap. Do not use the bias dependence "
                  "of the amplitude for this: the pm values use a fixed 2kappa, which changes with bias, "
                  "and the amplitude drifts in time."))
    out.append(tr("慢扫没用：Nanonis 的像素是点采样，不是时间平均。",
                  "Slow scanning does not help: Nanonis scan pixels are point samples, not time averages."))
    out.append(tr("实时电流读数高于设定值约 I0(A) 倍就是这个效应（对数反馈使几何平均=设定值，算术平均被抬高），"
                  "不是反馈故障。本次 算术/几何 = %.2f，I0(A) 预测 %.2f。"
                  % (res["arith_over_geo"], res["predicted_i0"]),
                  "A live current reading above the setpoint by about the I0(A) factor is this effect "
                  "(log feedback pins the geometric mean at the setpoint, the arithmetic mean is "
                  "inflated), not a feedback fault. Here arith/geo = %.2f, I0(A) predicts %.2f."
                  % (res["arith_over_geo"], res["predicted_i0"])))
    return out


class DiagnoseMainsGapModulation(BaseSkill):
    """Measure tunnelling-gap modulation (pm) at mains harmonics from Osci1T current traces."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=_NAME,
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "只读诊断：取若干段 Osci1T 隧穿电流示波器轨迹，对 ln|I| 做各电网谐波的最小二乘拟合，"
                "按 A/(2κ) 换算成间隙调制（pm），给出主导谐波、精细频率（是否锁在电网上）、算术/几何平均"
                "|I| 之比与 I0(A) 预测、判定与排查建议。不改偏压、设定电流、反馈或位置。要求 Osci1T "
                "已加载且通道为电流。",
                "Read-only diagnosis: pull several Osci1T tunnelling-current traces, fit ln|I| at every "
                "mains harmonic by least squares, convert A/(2 kappa) to gap modulation (pm), and report "
                "the dominant harmonic, its fine frequency (mains-locked or not), the arithmetic/geometric "
                "mean |I| ratio vs the I0(A) prediction, a verdict and troubleshooting guidance. Never "
                "changes bias, setpoint, feedback or position. Needs Osci1T loaded with the current channel.",
            ),
            parameters=[
                ParameterSpec(name="n_traces", type="int", required=False, default=6,
                              min_value=1, max_value=50,
                              description=tr("取几段轨迹（取中位数）。", "Number of traces (median is taken).")),
                ParameterSpec(name="mains_hz", type="float", required=False, default=50.0,
                              min_value=40.0, max_value=70.0,
                              description=tr("电网频率：50 或 60 Hz。", "Mains frequency: 50 or 60 Hz.")),
                ParameterSpec(name="max_harmonic", type="int", required=False, default=0,
                              min_value=0, max_value=40,
                              description=tr("最高谐波次数；0 = 取到奈奎斯特以下能放下的全部。",
                                             "Highest harmonic; 0 = as many as fit below Nyquist.")),
                ParameterSpec(name="two_kappa_nm", type="float", required=False, default=19.0,
                              min_value=1.0, max_value=60.0,
                              description=tr("2κ（nm⁻¹），I(z) 标定值。", "2 kappa in nm^-1 (I(z) calibration).")),
                ParameterSpec(name="floor_pa", type="float", required=False, default=0.5,
                              min_value=0.0001, max_value=1000.0,
                              description=tr("|I| 下限（pA），低于它按它算。",
                                             "|I| floor in pA; smaller values are clipped to it.")),
                ParameterSpec(name="signal_index", type="int", required=False, default=-1,
                              min_value=-1, max_value=15,
                              description=tr("可选：采集前把 Osci1T 通道设为该信号（-1 = 保持）。",
                                             "Optional: assign this signal to Osci1T first (-1 = keep).")),
            ],
            estimated_duration_s=10.0,
            composition_level=2,
            tags=["oscilloscope", "mains", "gap-modulation", "diagnosis", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        from mast.monitoring.mains_gap import analyze_traces

        n_traces = int(params.get("n_traces", 6))
        acq = AcquireOsciTrace()
        traces, calls, dt = [], [], 0.0
        for i in range(n_traces):
            r = acq.execute(context, {"data_to_get": 1,
                                      "signal_index": int(params.get("signal_index", -1)) if i == 0 else -1})
            calls += list(r.nanonis_calls or [])
            if not r.success:
                return SkillResult(skill_name=_NAME, success=False, nanonis_calls=calls,
                                   error=tr("取示波器轨迹失败（Osci1T 未加载或读取出错）：%s",
                                            "Oscilloscope trace failed (Osci1T not loaded or read error): %s")
                                   % r.error)
            s = (r.data or {}).get("samples") or []
            if not s:
                return SkillResult(skill_name=_NAME, success=False, nanonis_calls=calls,
                                   error=tr("示波器返回 0 个采样点", "Oscilloscope returned no samples"))
            traces.append(s)
            dt = float(r.data["dt_s"])
        res = analyze_traces(traces, dt, f_mains=float(params.get("mains_hz", 50.0)),
                             max_harmonic=int(params.get("max_harmonic", 0)),
                             two_kappa_nm=float(params.get("two_kappa_nm", 19.0)),
                             floor_pa=float(params.get("floor_pa", 0.5)))
        if not res.ok:
            return SkillResult(skill_name=_NAME, success=False, error=res.error, nanonis_calls=calls)
        d = res.to_dict()
        d["guidance"] = guidance(d)
        lock = tr("锁在电网上", "mains-locked") if d["mains_locked"] else tr("不在电网上", "not mains-locked")
        d["summary"] = tr(
            "主导 %.0f Hz：间隙调制 %.1f pm（%d 段中位数）；精细频率 %.2f Hz（偏差 %+.2f Hz → %s）；"
            "算术/几何 |I| = %.2f（I0(A) 预测 %.2f）；判定：%s。",
            "Dominant %.0f Hz: gap modulation %.1f pm (median of %d traces); fine frequency %.2f Hz "
            "(offset %+.2f Hz -> %s); arith/geo |I| = %.2f (I0(A) predicts %.2f); verdict: %s.",
        ) % (d["dominant_hz"], d["dominant_pm"], d["n_traces_used"], d["fine_freq_hz"],
             d["offset_from_harmonic_hz"], lock, d["arith_over_geo"], d["predicted_i0"], d["verdict"])
        d["source"] = "nanonis_osci1t"
        return SkillResult(skill_name=_NAME, success=True, data=d, nanonis_calls=calls)
