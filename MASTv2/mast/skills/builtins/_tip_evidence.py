"""Independent witnesses to what the tip is doing. Shared by approach / retract / relocate.

Three questions, three channels, deliberately not merged into one verdict:

* **Is the tip mechanically free?** — the qPlus oscillation amplitude. A tip in
  contact is damped to a standstill no matter what the current preamp reports,
  which makes it the only witness that is not downstream of the same electronics
  as everything else MAST measures.
* **How far is the tip?** — the lock-in dI/dV magnitude, which grows roughly
  exponentially on approach and therefore carries distance information while the
  DC current is still in the noise.
* **Is it tunnelling?** — the DC current against the setpoint. That one already
  lives in ``approach.py`` and is not repeated here.

Two rules run through everything below.

**"Cannot tell" is never "fine".** Every helper returns ``{}`` or an explicit
``status`` that says the reading failed. A detector whose failure mode looks like
success is worse than no detector, because it is trusted.

**Additive only.** Nothing here overrides the current-based checks. The current
tests keep their verdicts; these add a second opinion. On an STM with no qPlus
sensor the whole amplitude channel is simply absent, and that is a normal
configuration, not a fault.

Why a shared module: the same three questions get asked at four moments — before
an approach, at each rung of a retract ladder, after the clearance retract that
precedes a lateral relocation, and between the chunks of that relocation. Copying
the "read it, sanity-check it, degrade honestly" logic four times is how the
degradations stop matching.
"""

from __future__ import annotations

import logging
from typing import Any

from mast.i18n import tr

logger = logging.getLogger(__name__)

#: Fraction of the free-oscillation baseline above which the tip counts as
#: recovered — used to CONFIRM a retract, the mirror of the crash criterion.
#: Deliberately below 1.0: after a retract the amplitude comes back to the free
#: value but not instantly and not exactly, and demanding equality would report
#: a perfectly good retract as a failure.
RECOVERED_FRACTION = 0.80

__all__ = [
    "RECOVERED_FRACTION",
    "qplus_fields",
    "qplus_says_crashed",
    "capture_qplus_baseline",
    "qplus_recovered",
    "didv_trend_fields",
]


def qplus_fields(context) -> dict[str, Any]:
    """qPlus verdict as result-dict fields, or ``{}`` when there is nothing to say.

    Splat straight into a ``SkillResult.data``; no branch needed at the call
    site. Keys (all prefixed so they cannot collide):

    * ``qplus_status`` — ``ok`` | ``crash`` | ``unavailable`` | ``no_baseline``
    * ``qplus_crash``  — True / False / **None**. None means "cannot tell", and
      callers must not treat it as False.
    * ``qplus_fraction`` — amplitude as a fraction of the free-oscillation baseline.
    """
    try:
        from mast.skills.builtins.qplus_amplitude import CheckTipCrashByAmplitude

        res = CheckTipCrashByAmplitude().execute(context, {})
        data = getattr(res, "data", None) or {}
        status = data.get("status")
        if not status:
            return {}
        out: dict[str, Any] = {"qplus_status": status,
                               "qplus_crash": data.get("crash_indicator")}
        if data.get("fraction_of_baseline") is not None:
            out["qplus_fraction"] = data["fraction_of_baseline"]
        if data.get("note"):
            out["qplus_note"] = data["note"]
        return out
    except Exception as exc:  # noqa: BLE001 — a second opinion must not break the first
        logger.debug("qplus evidence unavailable: %s", exc)
        return {}


def qplus_says_crashed(fields: dict[str, Any]) -> bool:
    """True ONLY when the amplitude channel positively reports contact.

    Written as its own function so no call site has to remember that
    ``qplus_crash`` is tri-state. ``None`` (no sensor, no baseline, read failed)
    returns False here — but that is "no positive evidence of a crash", NOT
    "verified clear", and callers must not use this to confirm anything."""
    return fields.get("qplus_status") == "crash" and fields.get("qplus_crash") is True


def capture_qplus_baseline(context, *, note: str = "") -> dict[str, Any]:
    """Record the free-oscillation amplitude as the crash criterion's denominator.

    **Call this ONLY at a moment the tip is verified clear** — right after a
    confirmed retract, never speculatively. The baseline is what every later
    "is the amplitude collapsed?" question is measured against, so one captured
    while the tip was in contact would define contact as normal and silence the
    detector permanently.

    Automating it here is the point. The baseline previously existed only if
    somebody remembered to pass ``set_baseline=True`` by hand, which in practice
    meant the amplitude crash check spent its life reporting ``no_baseline``.
    A retract that MAST performed and verified is the one moment it can know."""
    try:
        from mast.skills.builtins.qplus_amplitude import ReadTipOscillationAmplitude

        res = ReadTipOscillationAmplitude().execute(context, {"set_baseline": True})
        data = getattr(res, "data", None) or {}
        # 基线可以被**拒绝**(读不到激励状态 / 散布像噪声底):那时读数照样
        # ``status == "ok"``,但 ``baseline_set`` 是 False、技能 success=False。只看
        # status 会把一次被拒的记录报成「已记录」—— 正是本函数要防的那种假装成功。
        if data.get("status") != "ok" or not data.get("baseline_set"):
            return {"qplus_baseline_captured": False,
                    "qplus_baseline_note": (data.get("note")
                                            or getattr(res, "error", None)
                                            or tr("振幅通道不可用",
                                                  "Amplitude channel unavailable"))}
        return {"qplus_baseline_captured": True,
                "qplus_baseline": data.get("amplitude"),
                "qplus_baseline_excited": data.get("baseline_excited"),
                "qplus_baseline_note": note or tr("已在确认脱离后记录自由振荡基线",
                                                  "Free-oscillation baseline recorded after "
                                                  "confirmed disengagement")}
    except Exception as exc:  # noqa: BLE001
        logger.debug("qplus baseline capture failed: %s", exc)
        return {"qplus_baseline_captured": False,
                "qplus_baseline_note": tr(f"基线记录失败:{exc}", f"Baseline capture failed: {exc}")}


def qplus_recovered(context) -> tuple[bool | None, str]:
    """Has the oscillation come back to (near) its free value? ``(verdict, why)``.

    Used to CONFIRM a retract before a lateral move — the amplitude answers
    "is the tip free to move" directly, which the current cannot: once the tip is
    far the current is zero, and zero is also what a shorted preamp reads.

    Returns ``None`` when the channel cannot answer. **None is not True.** A
    caller confirming clearance must treat it as "this witness abstained" and
    lean on the current check, not as agreement."""
    try:
        from mast.skills.builtins.qplus_amplitude import (
            ReadTipOscillationAmplitude, _excitation_state, _get_baseline,
            baseline_excitation, crash_fraction_for,
        )

        res = ReadTipOscillationAmplitude().execute(context, {})
        data = getattr(res, "data", None) or {}
        if data.get("status") != "ok":
            return None, str(data.get("note") or tr("振幅通道不可用 —— 这条判据用不上",
                                                    "Amplitude channel unavailable -- "
                                                    "this criterion cannot be used"))
        base = _get_baseline(context)
        if not base or base <= 0:
            return None, tr("没有自由振荡基线,无法判断振幅是否已恢复",
                            "No free-oscillation baseline, so it cannot be judged whether "
                            "the amplitude has recovered")
        # 同态比较:基线取于激励开、现在激励关(或反过来)时,两边不是同一个
        # 量 —— 未驱动读数 / 驱动基线 ≈ 0.1,会被读成「仍在接触」而挡住换位。读不到激励
        # 状态时保持旧行为(这条检查只做加法,不让一个读不到的 PLL 把证人整个撤掉)。
        base_exc, _legacy = baseline_excitation()
        exc_now, _ = _excitation_state(context)
        if exc_now is not None and exc_now != base_exc:
            return None, tr(f"基线取于激励{'开' if base_exc else '关'},现在激励"
                            f"{'开' if exc_now else '关'} —— 两种状态下的振幅不能相比,"
                            "本证人弃权,请依据电流判据。",
                            f"The baseline was taken with the excitation "
                            f"{'on' if base_exc else 'off'} and it is now "
                            f"{'on' if exc_now else 'off'} - the amplitudes in the two "
                            "states cannot be compared; this witness abstains, rely on the "
                            "current criterion.")
        crash_frac = crash_fraction_for(base_exc)
        amp = float(data.get("amplitude") or 0.0)
        frac = amp / float(base)
        if frac >= RECOVERED_FRACTION:
            return True, tr(f"qPlus 振幅已恢复到自由振荡基线的 {frac:.0%}"
                            f"(≥{RECOVERED_FRACTION:.0%}),针尖机械上是自由的。",
                            f"qPlus amplitude has recovered to {frac:.0%} of the free-oscillation "
                            f"baseline (>={RECOVERED_FRACTION:.0%}); the tip is mechanically free.")

        # ── 三态,不是两态 ────────────────────────────────────────────────
        #
        # 曾有一版 `_phase_clear` 在 **79%** 上判「针尖可能仍在接触,不要横向
        # 移动」,把整条换位挡死。但接触指的是**完全不起振**(振幅恒为 0 附近):
        # **接触会把音叉压死** —— 振幅塌到基线的百分之几,不是塌到 79%。
        # 79% 离「死」差着一个数量级。
        #
        # 更根本的一条:**STM 模式下 qPlus 振幅通道基本是噪声**,
        # 它唯一可靠的用处就是「振幅归零 = 撞上了」。拿一个噪声通道去卡 80% 这条线,
        # 量的是噪声不是接触。
        #
        # 两个阈值**本来就都存在**,只是中间那一段从来没人管:
        #     frac < _CRASH_FRACTION(0.10)  = 振幅被压死 ⇒ 真的在接触
        #     frac ≥ RECOVERED_FRACTION(0.80) = 确认脱离
        # 中间 ⇒ **这个证人答不了**,而本函数的 docstring 早就写着该怎么办:
        # 「`None` 不是 `True` —— 调用方应把它当作弃权,转而依赖电流判据」。
        # 弃权机制一直在,缺的只是让中间段走进去。
        #
        # 代价方向也对:判 False 会**挡住换位**(而针尖其实是自由的);判 None 只是
        # 少一个证人,电流那条判据照样要过。**「答不了」不该有「不合格」的权力。**
        if frac < crash_frac:
            return False, tr(f"qPlus 振幅只有自由振荡基线的 {frac:.0%}"
                             f"(< {crash_frac:.0%} = 音叉被压死)—— "
                             "针尖**仍在接触**,不要横向移动。",
                             f"qPlus amplitude is only {frac:.0%} of the free-oscillation baseline "
                             f"(< {crash_frac:.0%} = tuning fork crushed) -- "
                             "the tip is **still in contact**; do not move laterally.")
        return None, tr(f"qPlus 振幅是自由振荡基线的 {frac:.0%},落在"
                        f"{crash_frac:.0%}(压死)与 {RECOVERED_FRACTION:.0%}"
                        "(确认自由)之间 —— **这个通道在这一段答不了**"
                        "(STM 下振幅通道基本是噪声,它只在归零时说得准)。"
                        "本证人弃权,请依据电流判据。",
                        f"qPlus amplitude is {frac:.0%} of the free-oscillation baseline, between "
                        f"{crash_frac:.0%} (crushed) and {RECOVERED_FRACTION:.0%} "
                        "(confirmed free) -- **this channel cannot answer in this range** "
                        "(under STM the amplitude channel is mostly noise; it is reliable only when "
                        "it drops to zero). This witness abstains; rely on the current criterion.")
    except Exception as exc:  # noqa: BLE001
        return None, tr(f"振幅判据不可用:{exc}", f"Amplitude criterion unavailable: {exc}")


def didv_trend_fields(context) -> dict[str, Any]:
    """Where the lock-in dI/dV sits relative to its at-contact calibration.

    REPORTING ONLY — this deliberately does not gate anything.

    The physics is real: with the lock-in on the current, |R| rises roughly
    exponentially as the tip closes, so it carries distance information while the
    DC current is still buried in noise. But the reference value is bootstrapped
    from the first SUCCESSFUL approach, so before there is a trustworthy
    calibration a hard threshold would refuse real approaches. Refusing an
    approach costs a night; printing a ratio costs a line. Once the calibration
    has been confirmed over several approaches on a real rig, this is where a
    gate would go — see docs/v2/design/coarse_motion_intelligence.md §3.10.

    ``approach_didv_engage_frac`` (long a config key nothing read) is the
    threshold this narrates against.
    """
    try:
        from mast.core import instrument_profile as ip

        idx = ip.get_config("lockin_signal_index", None)
        if idx is None:
            return {}
        rec = context.safe_call("Signals_ValGet", int(idx), 0)
        val = _first_scalar(getattr(rec, "return_value", None))
        if val is None:
            return {}
        didv = abs(float(val))
        # 读数名称依据已配置的解调形式选择。
        # X/Y 是带符号投影，仅在合适相位下绝对值才近似幅值；相位变化导致的穿零
        # 不能直接解释为针尖远离。未知形式必须保持通用读数名称。
        form = str(ip.get_config("lockin_readout_form", "unknown") or "unknown")
        form = form.strip().lower()
        sym = {"xy": "|X|", "r_phi": "R"}.get(form, tr("|读数|", "|reading|"))
        out: dict[str, Any] = {"didv_v": didv, "didv_readout_form": form}

        cal = ip.get_calibration() or {}
        contact = cal.get("didv_at_contact_v")
        if not isinstance(contact, (int, float)) or not contact:
            out["didv_note"] = tr("尚无「到样品 dI/dV 标定值」(首次成功进针后自动记录);"
                                  "当前只报绝对值,不作距离判断。",
                                  "There is no \"at-sample dI/dV calibration value\" yet (recorded "
                                  "automatically after the first successful approach); only the "
                                  "absolute value is reported for now, with no distance judgement.")
            return out
        frac = didv / float(contact)
        thresh = float(ip.get_config("approach_didv_engage_frac", 0.7) or 0.7)
        out["didv_frac_of_contact"] = round(frac, 3)
        out["didv_engage_frac_threshold"] = thresh
        if frac >= 1.0:
            trend = tr("已达到或超过标定值 —— 按 dI/dV 看已经到样品",
                       "reached or exceeded the calibration value -- by dI/dV the tip is at the sample")
        elif frac >= thresh:
            trend = tr(f"已达标定值的 {frac:.0%}(阈值 {thresh:.0%})—— 接触在望",
                       f"at {frac:.0%} of the calibration value (threshold {thresh:.0%}) -- contact is near")
        elif frac >= 0.1:
            trend = tr(f"为标定值的 {frac:.0%} —— 还有距离,继续接近时应指数上升",
                       f"{frac:.0%} of the calibration value -- still some distance away; "
                       "it should rise exponentially as the approach continues")
        else:
            trend = tr(f"仅为标定值的 {frac:.1%} —— 离样品还远",
                       f"only {frac:.1%} of the calibration value -- still far from the sample")
        phase_note = (
            tr("（本机以 X/Y 解调:X 是带符号投影,相位在接近途中转动时 |X| 会**穿零**——"
               "看到非单调下降先怀疑相位,不要读成针尖退开了。）",
               "(This machine demodulates as X/Y: X is a signed projection, and |X| **crosses zero** "
               "when the phase rotates during the approach -- if you see a non-monotonic decrease, "
               "suspect the phase first and do not read it as the tip retreating.) ") if form == "xy"
            else "" if form == "r_phi"
            else tr("（本机解调形式未登记:这只是读数绝对值,不保证它是幅度。）",
                    "(The demodulation form of this machine is not registered: this is only the "
                    "absolute value of the reading, not guaranteed to be the amplitude.) "))
        out["didv_note"] = tr(
            f"dI/dV {sym} = {didv:.4g},{trend}。{phase_note}"
            "（这是**趋势播报**,不是闸门:标定本身靠首次成功进针 bootstrap,"
            "在它可信之前设硬阈值会误拦真实进针。）",
            f"dI/dV {sym} = {didv:.4g}, {trend}. {phase_note}"
            "(This is a **trend report**, not a gate: the calibration itself is bootstrapped from "
            "the first successful approach, and a hard threshold before it can be trusted would "
            "wrongly block real approaches.)")
        return out
    except Exception as exc:  # noqa: BLE001 — evidence is a bonus, never a blocker
        logger.debug("didv trend unavailable: %s", exc)
        return {}


def _first_scalar(rv):
    """First scalar out of a Nanonis (header, body, [vals]) triplet, or None."""
    if isinstance(rv, (list, tuple)) and len(rv) > 2:
        inner = rv[2]
        if isinstance(inner, (int, float)) and not isinstance(inner, bool):
            return float(inner)
        if isinstance(inner, (list, tuple)) and inner:
            try:
                return float(inner[0])
            except (TypeError, ValueError):
                return None
    return None
