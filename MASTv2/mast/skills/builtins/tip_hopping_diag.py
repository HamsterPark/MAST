# -*- coding: utf-8 -*-
"""``DiagnoseTipAdsorbateHopping`` -- is a mobile adsorbate on the tip apex making the image jump?

## Rationale

Typical symptom: images full of line-to-line jumps while the fast junction noise
(oscilloscope traces) is quiet.  Watching Z at one fixed point (feedback on, a few Hz
sampling, tens of seconds per condition) and counting discrete jumps above a threshold
under several (bias, setpoint) conditions separates the causes.

A jump rate that grows with field and with current is the signature of a mobile adsorbate
(for example picked-up material) on the tip apex, not of vibration or electronics (those
would not care about the bias/setpoint).  The usual remedy is re-metallising the tip on a
noble-metal surface with ``MakeAtomicResolutionTip``; presses and pulses on the sample
surface that supplied the adsorbate tend to make it worse.

## What the skill does

At the current position, with feedback on, for each (bias, setpoint) condition: set it, wait
``settle_s``, sample Z (and the current) at ``sample_hz`` for ``sample_s`` seconds, count
discrete jumps ``|dZ| > jump_threshold_pm`` (consecutive over-threshold steps count as one
event), and classify the whole set:

* ``adsorbate_hopping`` -- the count grows with |V| and/or the setpoint;
* ``stable`` -- (almost) no jumps anywhere;
* ``noisy_environment`` -- jumps of about the same number under every condition;
* ``inconclusive`` -- anything else (too few conditions, non-monotonic, ...).

The original bias and setpoint are restored on every exit path (error, operator abort), and
the restore is read back.

## Safety envelope

It writes only the bias and the Z setpoint and **never crosses zero bias**: a condition of
the opposite sign to the present bias, or with |V| below ``MIN_ABS_BIAS_V``, is rejected.
With feedback on, passing through the near-zero-current gap of a semiconductor drives the
tip into the surface.  To change polarity use ``BiasSettleChange`` (zero-crossing protected
ramp) or the tip-lift procedure first, then run this skill again.  The setpoint is capped at
``MAX_SETPOINT_A`` and the current is watched at every sample.
"""
from __future__ import annotations

import logging
import math
import time

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.skills.base import BaseSkill
from mast.skills.builtins.bias_wiggle import _parse_scalar

logger = logging.getLogger(__name__)

_NAME = "DiagnoseTipAdsorbateHopping"

#: |V| below this is the zero-crossing dead zone (same value as ``BiasSettleChange``).
MIN_ABS_BIAS_V = 0.05
MAX_ABS_BIAS_V = 5.0
#: hard cap on the Z setpoint this skill will write.
MAX_SETPOINT_A = 2e-9
_STEP_V = 0.25          # largest bias step between two writes
_STEP_INTERVAL_S = 0.1

#: indirection so tests do not really wait
_sleep = time.sleep

REMEDY = ("re-metallise the tip on a noble-metal surface (e.g. MakeAtomicResolutionTip on Au(111)); "
          "do NOT press or pulse on the sample surface that supplied the adsorbate -- that tends to make it worse")

__all__ = ["DiagnoseTipAdsorbateHopping", "count_jumps", "classify_hopping",
           "parse_conditions", "MIN_ABS_BIAS_V", "MAX_SETPOINT_A"]


# --------------------------------------------------------------------------- pure helpers

def count_jumps(z, threshold: float) -> dict:
    """Discrete jumps in a Z trace: steps with ``|dz| > threshold``.

    Consecutive over-threshold steps are one event (a jump that takes two samples is one
    jump).  ``z`` and ``threshold`` share a unit.  Returns the count, the largest single step
    and the Z range.
    """
    z = [float(v) for v in z]
    if len(z) < 2:
        return {"n_jumps": 0, "max_jump": 0.0, "z_range": 0.0}
    d = [abs(b - a) for a, b in zip(z, z[1:])]
    n, prev = 0, False
    for v in d:
        over = v > threshold
        if over and not prev:
            n += 1
        prev = over
    return {"n_jumps": n, "max_jump": max(d), "z_range": max(z) - min(z)}


def classify_hopping(conditions) -> tuple[str, str]:
    """``(verdict, reason)`` from ``[{"bias_v", "setpoint_a", "n_jumps"}, ...]``.

    * all counts <= 1 -> ``stable`` (one stray step is not a pattern);
    * every condition has >= 2 jumps and the largest count is at most twice the smallest ->
      ``noisy_environment`` (the count does not depend on the condition);
    * among pairs where one condition is >= the other in |V| and in setpoint (and differs in
      at least one) the larger-stress condition has more jumps in clearly more pairs than
      fewer, the maximum is >= 3 and at least twice the minimum -> ``adsorbate_hopping``;
    * otherwise ``inconclusive``.
    """
    cs = [dict(c) for c in conditions]
    counts = [int(c["n_jumps"]) for c in cs]
    if not cs:
        return "inconclusive", "no condition measured"
    if max(counts) <= 1:
        return "stable", "no more than one jump under any condition"
    if len(cs) >= 2 and min(counts) >= 2 and max(counts) <= 2 * min(counts):
        return "noisy_environment", ("about the same number of jumps (%d..%d) under every "
                                     "condition: not bias/current dependent" % (min(counts), max(counts)))
    concord = discord = 0
    for i, a in enumerate(cs):
        for b in cs[i + 1:]:
            va, vb = abs(a["bias_v"]), abs(b["bias_v"])
            ia, ib = abs(a["setpoint_a"]), abs(b["setpoint_a"])
            tol = 1e-9
            if va <= vb + tol and ia <= ib * (1 + tol) and (vb - va > tol or ib > ia * (1 + tol)):
                lo, hi = a, b
            elif vb <= va + tol and ib <= ia * (1 + tol) and (va - vb > tol or ia > ib * (1 + tol)):
                lo, hi = b, a
            else:
                continue
            if hi["n_jumps"] > lo["n_jumps"]:
                concord += 1
            elif hi["n_jumps"] < lo["n_jumps"]:
                discord += 1
    if concord >= 1 and concord >= 2 * discord and max(counts) >= 3 and max(counts) >= 2 * min(counts):
        return "adsorbate_hopping", ("jump count grows with |V| and/or setpoint (%d concordant, %d discordant "
                                     "condition pairs; counts %d..%d)" % (concord, discord, min(counts), max(counts)))
    return "inconclusive", ("jumps present (max %d) but no clear monotonic dependence on |V|/setpoint "
                            "(%d concordant, %d discordant pairs); add conditions or repeat" % (max(counts), concord, discord))


def parse_conditions(raw: str) -> list[tuple[float, float]]:
    """``"2.0@30, 1.5@100"`` -> ``[(2.0 V, 30e-12 A), (1.5 V, 100e-12 A)]`` (bias in V @ setpoint in pA)."""
    out = []
    for chunk in str(raw or "").replace("\n", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "@" not in chunk:
            raise ValueError("condition %r is not 'bias_V@setpoint_pA'" % chunk)
        v, i = chunk.split("@", 1)
        out.append((float(v), float(i.lower().replace("pa", "")) * 1e-12))
    return out


# --------------------------------------------------------------------------- the skill

class DiagnoseTipAdsorbateHopping(BaseSkill):
    """Z-trace jump counting versus (bias, setpoint) to expose a mobile adsorbate on the tip."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=_NAME,
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr(
                "诊断针尖上是否有会跳的吸附物（图像满是行间跳变、而示波器上的快速结噪声很安静时用）。"
                "在当前位置、反馈开着，逐个 (偏压, 设定点) 条件：设定、等稳定、以约 2 Hz 采样 Z 与电流若干秒，"
                "数离散跳变（|ΔZ| > 阈值，默认 40 pm），给出每个条件的跳变数、最大跳变、Z 范围，"
                "判决 adsorbate_hopping（跳变数随 |V| 和/或设定点增大）/ stable / noisy_environment"
                "（与条件无关）/ inconclusive。只写偏压与 Z 设定点，同极性、绝不穿零；"
                "结束（含出错、中止）一定恢复原偏压与设定点并回读。需要换极性请先用 BiasSettleChange 或抬针流程。"
                "补救提示：在贵金属表面（如 Au(111) 上 MakeAtomicResolutionTip）重新金属化针尖，"
                "不要在提供吸附物的样品表面下压或打脉冲。",
                "Diagnose a mobile adsorbate on the tip apex (use when images are full of line-to-line jumps "
                "while the fast junction noise on the oscilloscope is quiet). At the current position with "
                "feedback on, for each (bias, setpoint) condition: set it, settle, sample Z and the current at "
                "about 2 Hz for some seconds, count discrete jumps (|dZ| > threshold, default 40 pm), and "
                "report per-condition jump count, largest jump and Z range, with a verdict: adsorbate_hopping "
                "(jump count grows with |V| and/or setpoint) / stable / noisy_environment (independent of the "
                "condition) / inconclusive. Writes only the bias and the Z setpoint, same polarity, never "
                "crossing zero; the original bias and setpoint are always restored (also on error or abort) and "
                "read back. To change polarity use BiasSettleChange or the tip-lift procedure first. Remedy "
                "hint: re-metallise the tip on a noble-metal surface (e.g. MakeAtomicResolutionTip on "
                "Au(111)); do not press or pulse on the sample surface that supplied the adsorbate."),
            parameters=[
                ParameterSpec(
                    name="conditions", type="str", required=False, default="",
                    description=tr("条件列表，格式 \"偏压V@设定点pA\"，逗号分隔，如 \"2.0@30,1.5@100,2.0@100,2.5@100\"。"
                                   "留空 = 以当前工作点为中心的同极性网格（0.75x/1x/1.25x 偏压 与 0.3x/1x 设定点）。"
                                   "不允许与当前偏压反号或 |V|<0.05 V。",
                                   "Condition list, format \"biasV@setpointpA\", comma separated, e.g. "
                                   "\"2.0@30,1.5@100,2.0@100,2.5@100\". Empty = a same-polarity grid around the "
                                   "current working point (0.75x/1x/1.25x bias and 0.3x/1x setpoint). Opposite "
                                   "polarity or |V|<0.05 V is rejected.")),
                ParameterSpec(
                    name="sample_s", type="float", unit="s", required=False, default=40.0,
                    min_value=2.0, max_value=300.0,
                    description=tr("每个条件的采样时长。", "Sampling time per condition.")),
                ParameterSpec(
                    name="sample_hz", type="float", unit="Hz", required=False, default=2.0,
                    min_value=0.2, max_value=20.0,
                    description=tr("Z 采样率。", "Z sampling rate.")),
                ParameterSpec(
                    name="settle_s", type="float", unit="s", required=False, default=5.0,
                    min_value=0.0, max_value=60.0,
                    description=tr("改条件后、开始采样前的等待。", "Wait after changing the condition, before sampling.")),
                ParameterSpec(
                    name="jump_threshold_pm", type="float", unit="pm", required=False, default=40.0,
                    min_value=1.0, max_value=2000.0,
                    description=tr("判为一次跳变的相邻采样 |ΔZ| 下限。", "Smallest |dZ| between adjacent samples counted as a jump.")),
                ParameterSpec(
                    name="abort_current_a", type="float", unit="A", required=False, default=5e-9,
                    min_value=1e-11, max_value=1e-6,
                    description=tr("任何采样的 |I| 超过它就中止并恢复。", "Abort and restore if any sample has |I| above this.")),
                ParameterSpec(
                    name="include_fast_noise", type="bool", required=False, default=False,
                    description=tr("每个条件再用 AcquireOsciTrace 取一段快速噪声并报告其 rms（需要 Osci1T 模块；取不到不影响判决）。",
                                   "Also pull a fast-noise trace with AcquireOsciTrace per condition and report its rms "
                                   "(needs the Osci1T module; failure does not affect the verdict).")),
            ],
            preconditions=[],
            estimated_duration_s=240.0,
            composition_level=2,
            tags=["tip", "diagnosis", "bias", "setpoint", "adsorbate", "noise"],
        )

    # ---- low-level helpers ------------------------------------------------

    @staticmethod
    def _aborted(context) -> bool:
        f = getattr(context, "check_abort", None)
        return bool(callable(f) and f())

    def _goto(self, context, calls, v_from, i_from, v_to, i_to) -> str:
        """Same-polarity walk to (v_to, i_to) in <= 0.25 V steps. Returns an error text or ''."""
        n = max(1, int(math.ceil(abs(v_to - v_from) / _STEP_V)))
        for k in range(1, n + 1):
            f = k / n
            v = v_from + (v_to - v_from) * f
            i = i_from + (i_to - i_from) * f
            r = context.safe_call("Bias_Set", float(v))
            calls.append(r)
            if r.error:
                return "Bias_Set failed: %s" % r.error
            r = context.safe_call("ZCtrl_SetpntSet", float(i))
            calls.append(r)
            if r.error:
                return "ZCtrl_SetpntSet failed: %s" % r.error
            if k < n:
                _sleep(_STEP_INTERVAL_S)
        return ""

    def _restore(self, context, calls, v0, i0) -> dict:
        """Put bias and setpoint back (allowed after abort) and read them back."""
        out = {"restored": False, "readback_bias_v": None, "readback_setpoint_a": None}
        try:
            r1 = context.safe_call("Bias_Set", float(v0), allow_on_abort=True)
            r2 = context.safe_call("ZCtrl_SetpntSet", float(i0), allow_on_abort=True)
            calls.extend([r1, r2])
            rb = context.safe_call("Bias_Get")
            rs = context.safe_call("ZCtrl_SetpntGet")
            calls.extend([rb, rs])
            vb, sb = _parse_scalar(rb), _parse_scalar(rs)
            out["readback_bias_v"], out["readback_setpoint_a"] = vb, sb
            out["restored"] = bool(
                not r1.error and not r2.error and vb is not None and sb is not None
                and abs(vb - v0) <= max(2e-3, 0.01 * abs(v0))
                and abs(sb - i0) <= max(2e-12, 0.05 * abs(i0)))
        except Exception as exc:  # noqa: BLE001 -- cleanup must never raise
            out["restore_error"] = str(exc)
        return out

    # ---- execute -----------------------------------------------------------

    def execute(self, context, params: dict) -> SkillResult:
        calls: list = []
        sample_s = float(params.get("sample_s", 40.0))
        hz = float(params.get("sample_hz", 2.0))
        settle_s = float(params.get("settle_s", 5.0))
        thr_pm = float(params.get("jump_threshold_pm", 40.0))
        abort_a = float(params.get("abort_current_a", 5e-9))
        fast = bool(params.get("include_fast_noise", False))

        def _fail(msg, **data):
            return SkillResult(skill_name=_NAME, success=False, error=msg, data=data, nanonis_calls=calls)

        # feedback must be on: Z only follows the surface (and so reveals jumps) in constant-current mode
        fb_rec = context.safe_call("ZCtrl_OnOffGet")
        calls.append(fb_rec)
        fb = _parse_scalar(fb_rec)
        if fb is None or not bool(int(fb)):
            return _fail(tr("Z 反馈是关的或读不到 —— 本诊断看的是反馈开着时 Z 的跳变，先开反馈。",
                            "Z feedback is off or unreadable -- this diagnosis watches Z jumps with feedback on; "
                            "turn the feedback on first."))
        rb = context.safe_call("Bias_Get")
        rs = context.safe_call("ZCtrl_SetpntGet")
        calls.extend([rb, rs])
        v0, i0 = _parse_scalar(rb), _parse_scalar(rs)
        if v0 is None or i0 is None:
            return _fail(tr("读不到当前偏压/设定点（Bias_Get / ZCtrl_SetpntGet）—— 不知道要恢复到哪里，拒绝执行。",
                            "Cannot read the current bias/setpoint (Bias_Get / ZCtrl_SetpntGet) -- would not know what "
                            "to restore; refusing."))
        if abs(v0) < MIN_ABS_BIAS_V:
            return _fail(tr("当前偏压 %.3g V 在零附近死区内，无法定义极性。" , "Current bias %.3g V is inside the near-zero "
                            "dead zone; polarity undefined.") % v0)

        # ---- conditions: same polarity, never near zero
        try:
            conds = parse_conditions(params.get("conditions") or "")
        except ValueError as exc:
            return _fail(str(exc))
        if not conds:
            sgn = math.copysign(1.0, v0)
            conds = [(sgn * abs(v0) * 0.75, abs(i0)), (v0, abs(i0) * 0.3), (v0, abs(i0)), (sgn * abs(v0) * 1.25, abs(i0))]
            conds = [(v, i) for v, i in conds if MIN_ABS_BIAS_V <= abs(v) <= MAX_ABS_BIAS_V]
        for v, i in conds:
            if v * v0 <= 0:
                return _fail(tr(
                    "条件 %.3g V 与当前偏压 %.3g V 反号 —— 拒绝。反馈开着时偏压穿过零附近，隧道电流塌掉、"
                    "反馈会把针尖压进样品（半导体上尤其危险）。换极性请先用 BiasSettleChange（穿零保护斜坡）"
                    "或抬针流程，然后在新极性下再运行本技能。",
                    "Condition %.3g V has the opposite sign to the current bias %.3g V -- refused. With feedback on, "
                    "crossing near zero bias collapses the tunnelling current and the loop drives the tip into the "
                    "sample (especially dangerous on a semiconductor). To change polarity first use "
                    "BiasSettleChange (zero-crossing protected ramp) or the tip-lift procedure, then run this skill "
                    "again at the new polarity.") % (v, v0))
            if not (MIN_ABS_BIAS_V <= abs(v) <= MAX_ABS_BIAS_V):
                return _fail(tr("条件偏压 %.3g V 超出允许范围 %.2f–%.1f V（下限是穿零死区）。",
                                "Condition bias %.3g V outside the allowed %.2f-%.1f V (the lower bound is the "
                                "zero-crossing dead zone).") % (v, MIN_ABS_BIAS_V, MAX_ABS_BIAS_V))
            if not (0 < abs(i) <= MAX_SETPOINT_A):
                return _fail(tr("条件设定点 %.3g A 超出 (0, %.1g] A。", "Condition setpoint %.3g A outside (0, %.1g] A.")
                             % (i, MAX_SETPOINT_A))

        n_samples = max(2, int(round(sample_s * hz)))
        results: list = []
        err = ""
        cur_v, cur_i = float(v0), float(i0)
        try:
            for (v, i) in conds:
                if self._aborted(context):
                    err = "aborted"
                    break
                e = self._goto(context, calls, cur_v, cur_i, float(v), float(i))
                if e:
                    err = e
                    break
                cur_v, cur_i = float(v), float(i)
                _sleep(settle_s)
                zs, cs, over = [], [], ""
                dt = 1.0 / hz
                for _ in range(n_samples):
                    if self._aborted(context):
                        err = "aborted"
                        break
                    rz = context.safe_call("ZCtrl_ZPosGet")
                    ri = context.safe_call("Current_Get")
                    calls.extend([rz, ri])
                    z, cur = _parse_scalar(rz), _parse_scalar(ri)
                    if z is not None:
                        zs.append(z * 1e12)
                    if cur is not None:
                        cs.append(cur)
                        if abs(cur) > abort_a:
                            over = tr("电流 %.3g A 超过中止阈 %.3g A", "Current %.3g A exceeds the abort threshold %.3g A") % (abs(cur), abort_a)
                            break
                    _sleep(dt)
                if over:
                    err = over
                if len(zs) >= 2:
                    st = count_jumps(zs, thr_pm)
                    rec = {"bias_v": float(v), "setpoint_a": float(i), "n_samples": len(zs),
                           "n_jumps": st["n_jumps"], "max_jump_pm": round(st["max_jump"], 1),
                           "z_range_pm": round(st["z_range"], 1),
                           "current_mean_a": (sum(cs) / len(cs)) if cs else None,
                           "complete": not err}
                    if fast and not err:
                        rec["fast_noise"] = self._fast_noise(context, calls)
                    results.append(rec)
                if err:
                    break
        except Exception as exc:  # noqa: BLE001 -- restore first, then report
            err = "%s: %s" % (type(exc).__name__, exc)
        finally:
            rest = self._restore(context, calls, v0, i0)

        data = {
            "start_bias_v": v0, "start_setpoint_a": i0, "conditions": results,
            "jump_threshold_pm": thr_pm, "sample_s": sample_s, "sample_hz": hz,
            "restore": rest, "remedy": REMEDY,
        }
        if not rest.get("restored"):
            data["restore_warning"] = tr(
                "偏压/设定点没能确认恢复到 %.3g V / %.3g A —— 请立即在 Nanonis 上手动核对。",
                "Bias/setpoint could not be confirmed restored to %.3g V / %.3g A -- check Nanonis by hand now.") % (v0, i0)
        if err:
            return SkillResult(
                skill_name=_NAME, success=False, data=data, nanonis_calls=calls,
                error=tr("诊断中止：%s。已完成 %d 个条件；偏压与设定点%s恢复到 %.3g V / %.3g A。",
                         "Diagnosis stopped: %s. %d condition(s) done; bias and setpoint %s restored to %.3g V / %.3g A.")
                % (err, len(results), tr("已", "were") if rest.get("restored") else tr("未能确认", "could NOT be confirmed"), v0, i0))
        if not results:
            return SkillResult(skill_name=_NAME, success=False, data=data, nanonis_calls=calls,
                               error=tr("没有采到任何条件的 Z 数据。", "No Z data was collected under any condition."))
        verdict, reason = classify_hopping(results)
        data["verdict"] = verdict
        data["verdict_reason"] = reason
        data["verdict_note"] = tr(
            "判决只基于所测条件下的跳变计数；adsorbate_hopping 是「针尖上有可动吸附物」的最可能解释，不是直接观测。",
            "The verdict rests only on jump counts under the measured conditions; adsorbate_hopping is the most likely "
            "explanation (a mobile adsorbate on the tip), not a direct observation.")
        return SkillResult(
            skill_name=_NAME, success=True, data=data, nanonis_calls=calls,
            summary=tr("针尖吸附物跳变诊断：%s（%s）；跳变数 %s；已恢复偏压/设定点。%s",
                       "Tip adsorbate hopping diagnosis: %s (%s); jump counts %s; bias/setpoint restored. %s")
            % (verdict, reason,
               ", ".join("%+.2f V/%.0f pA: %d" % (r["bias_v"], r["setpoint_a"] * 1e12, r["n_jumps"]) for r in results),
               ("Remedy: " + REMEDY + ".") if verdict == "adsorbate_hopping" else ""))

    def _fast_noise(self, context, calls) -> dict:
        """Reuse AcquireOsciTrace (does not reimplement it); failure is reported, not fatal."""
        try:
            from mast.skills.builtins.acquire_osci_trace import AcquireOsciTrace
            r = AcquireOsciTrace().execute(context, {"data_to_get": 1})
            calls.extend(r.nanonis_calls or [])
            if not r.success:
                return {"ok": False, "error": r.error}
            y = [float(v) for v in r.data["samples"]]
            m = sum(y) / len(y)
            return {"ok": True, "rms": math.sqrt(sum((v - m) ** 2 for v in y) / len(y)),
                    "fs_hz": r.data.get("fs_hz")}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


def make_tool(context_provider):
    from mast.agents._shared.skill_adapter import wrap_skill
    return wrap_skill(DiagnoseTipAdsorbateHopping, context_provider)
