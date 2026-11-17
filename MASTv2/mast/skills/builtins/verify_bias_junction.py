"""Check that the sample bias really reaches the tunnel junction.

Failure mode
------------
The sample bias can silently stop reaching the junction (typical cause: a bad
contact in the external bias cable). It tends to show up right after coarse XY
moves. With Z feedback on
(Nanonis regulates |I|) the signature was:

* set bias +2 V, setpoint 100 pA -> the current reads about -97 ... -100 pA
  (sign OPPOSITE to the bias), stable;
* switching the set bias to -2 V leaves the current unchanged;
* stepping the bias +2 -> +1 -> +2 V at constant current gives no Z response
  beyond slow drift (a healthy junction moves the
  tip toward the sample and back);
* with the tip withdrawn the current is ~0 pA.

The junction then sees only a few mV (preamp offset / ground potential), so
the feedback drives the tip to near contact to find 100 pA, and every image
taken there is meaningless. MAST had no check for it.

What this skill does
--------------------
1. Precondition: Z feedback on and the loop converged (|I| near the setpoint).
   Reads bias, setpoint and the MEDIAN current over ``sample_s`` seconds.
2. Passive check: ``sign(I) != sign(V)`` and ``|I| > sign_fraction * |setpoint|``
   -> ``bias_not_at_junction``.
3. Active check (``active_check``, default on): lower ``|V|`` to
   ``reduce_fraction * V`` (same polarity, never crossing zero, never below
   ``min_bias_v``), wait for the loop to settle, measure the mean Z, restore
   the original bias, measure again. A healthy junction pulls the tip toward
   the sample when ``|V|`` drops. If BOTH transitions move Z by less than
   ``min_dz_m`` while the current stays put -> ``bias_not_at_junction``.
   ``ok`` needs a genuine step response: the lowered and the restored dZ must
   have OPPOSITE signs, both >= ``min_dz_m``, and similar size (smaller/larger
   >= 0.4). Large moves in the same direction are slow drift and give
   ``inconclusive``, never ``ok``.
4. Anything it cannot read or conclude is ``inconclusive``. The skill never
   returns ``ok`` for something it could not measure; ``success`` is True only
   for ``ok``.

Z sign
------
On this instrument the Z reading INCREASES as the tip moves AWAY from the
sample (withdrawn rail = +, extended = -; ``z_extend_sign = -1``, see
``instrument_profile.z_extend_sign_or_none``). So "toward the sample" means
Z decreasing, i.e. ``sign(dz) == z_extend_sign``. The verdict uses only
``|dz|`` (a response in the wrong direction is still a response, and a state
with rising LDOS can legitimately retract the tip); the direction is reported
as ``direction_as_expected`` and a mismatch adds a warning, it does not decide
the verdict. If the machine never declared the sign it is reported as
``None`` - not guessed.

Why it withdraws by default (``withdraw_on_fail=True``)
-------------------------------------------------------
With a dead bias the feedback is regulating a few-mV junction to the setpoint:
the tip sits near contact, at risk of crashing, while the operator or a
workflow thinks it is imaging normally. Leaving it there is the dangerous
default; withdrawing costs one re-approach. The withdraw goes through
``SafeRetract`` (``ZCtrl_Withdraw`` + readback confirmation) - NOT
``EmergencyRetract``, which first switches the Z feedback ON, the wrong thing
to do into a junction that is already pressing.

Typical responses
-----------------
* healthy junction, bias lowered to 0.6x at constant current: Z moves toward
  the sample by hundreds of pm and back by a similar amount on restoring the
  bias (includes drift);
* metallic-like area: only tens of pm;
* bias NOT at the junction: |dZ| at drift level and, tellingly, in the SAME
  direction for both transitions (slow drift), independent of the bias.
The default 10 pm threshold is far below the healthy responses and still
below the weakest ones seen.
Because drift can be hundreds of pm, a single |dZ| is NOT enough: the verdict
compares the two transitions (opposite sign, similar size).

Semiconducting samples
----------------------
At 0.6x the tip can approach by up to about 1 nm. The feedback
is still on and the current unchanged, so this is not dangerous, but be aware
of it. Near a band edge use ``reduce_fraction=0.8``.

Limits
------
* The active check needs a visible Z response to a bias change; a very flat
  LDOS (metallic area) with a small step can give < 10 pm on a healthy
  junction. Use a larger step (smaller ``reduce_fraction``) or compare on a
  known-good area before concluding.
* At imaging biases close to ``min_bias_v`` no step is possible; the active
  check is then skipped and (if requested) the verdict is ``inconclusive``.
  Pass ``active_check=False`` for an explicit passive-only check.
"""
from __future__ import annotations

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

SKILL_NAME = "VerifyBiasAtJunction"

VERDICT_OK = "ok"
VERDICT_BAD = "bias_not_at_junction"
VERDICT_INCONCLUSIVE = "inconclusive"

#: Hard bounds, re-checked in execute() (specs can be bypassed).
_MAX_SAMPLE_S = 10.0
_MAX_SETTLE_S = 15.0
#: relative current change under which "the current did not move" holds.
_CURRENT_UNCHANGED_REL = 0.3
#: min(|dz_lowered|, |dz_restored|) / max(...) needed to call a Z move a step
#: response. Healthy junctions give similar-size moves both ways.
_MIN_SYMMETRY = 0.4
#: below this fraction of |setpoint| the loop has not converged / tip not engaged.
_ENGAGED_FRACTION = 0.2


def _scalar(record) -> "float | None":
    """First scalar of a Nanonis ``(header, body, [values])`` reply, else None."""
    if record is None or getattr(record, "error", None):
        return None
    rv = getattr(record, "return_value", None)
    if not isinstance(rv, (list, tuple)) or len(rv) <= 2:
        return None
    body = rv[2]
    if not isinstance(body, (list, tuple)) or not body:
        return None
    try:
        v = float(body[0])
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _median(values: "list[float]") -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else 0.5 * (s[mid - 1] + s[mid])


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


class VerifyBiasAtJunction(BaseSkill):
    """Verify the set bias is actually applied across the tunnel junction."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=SKILL_NAME,
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "进针之后检查样品偏压是否真的加在隧道结上（外部偏压线接触不良时，"
                "反馈会把针尖推到近接触去凑电流）。被动检查：电流符号与偏压相反且"
                "|I| > 0.2×|setpoint| 即判 bias_not_at_junction；主动检查（默认开）："
                "把 |V| 降到 reduce_fraction×V（同极性、不过零、不低于 min_bias_v），"
                "测 Z 均值变化，再恢复偏压复测；健康结应使针尖靠近样品（本机 Z 减小）。"
                "两次 Z 响应都小于 min_dz_m 且电流不变 → bias_not_at_junction。"
                "读不到的一律 inconclusive，绝不在没测到时报 ok。失败时默认走 "
                "SafeRetract 退针（withdraw_on_fail），并提示检查外部偏压线。"
                "AutoApproach/ApproachTip 进针之后、RelocateCoarseXY 重新进针之后都应跑一次。",
                "After approach, check that the sample bias really reaches the "
                "tunnel junction (with a bad external bias contact the feedback "
                "drives the tip to near contact to find the current). Passive "
                "check: current sign opposite to the bias and |I| > 0.2 x "
                "|setpoint| -> bias_not_at_junction. Active check (default on): "
                "lower |V| to reduce_fraction x V (same polarity, never crossing "
                "zero, never below min_bias_v), measure the mean Z change, restore "
                "the bias and measure again; a healthy junction moves the tip "
                "toward the sample (Z decreases on this instrument). Both Z "
                "responses below min_dz_m with an unchanged current -> "
                "bias_not_at_junction. Anything unreadable is inconclusive; it "
                "never reports ok for something it could not measure. On failure "
                "it withdraws via SafeRetract by default (withdraw_on_fail) and "
                "tells the operator to check the external bias cable. Run it "
                "after AutoApproach/ApproachTip and after the re-approach in "
                "RelocateCoarseXY."),
            parameters=[
                ParameterSpec(
                    name="active_check", type="bool", required=False, default=True,
                    description=tr(
                        "是否做主动检查（临时降低偏压看 Z 响应）。关掉则只做被动的"
                        "电流符号检查，通过也只算弱证据（data.evidence=passive_only）。",
                        "Run the active check (temporarily lower the bias and look "
                        "for a Z response). Off = passive current-sign check only; "
                        "a pass is weak evidence (data.evidence=passive_only).")),
                ParameterSpec(
                    name="reduce_fraction", type="float", required=False, default=0.6,
                    min_value=0.2, max_value=0.95,
                    description=tr(
                        "主动检查时把 |V| 降到的比例（同极性，绝不过零）。",
                        "Fraction of |V| to step down to in the active check "
                        "(same polarity, never crosses zero).")),
                ParameterSpec(
                    name="min_bias_v", type="float", unit="V", required=False,
                    default=0.05, min_value=0.01, max_value=2.0,
                    description=tr(
                        "主动检查时 |V| 的下限；目标低于它就夹在它上面，"
                        "若连一个有意义的台阶都没有就跳过主动检查。",
                        "Lower bound of |V| during the active check; a lower "
                        "target is raised to it, and if no meaningful step is "
                        "left the active check is skipped.")),
                ParameterSpec(
                    name="min_step_v", type="float", unit="V", required=False,
                    default=0.1, min_value=0.02, max_value=2.0,
                    description=tr(
                        "主动检查所需的最小偏压台阶 |V|-|V_target|；台阶更小就跳过"
                        "主动检查（verdict=inconclusive），因为小台阶在健康结上也几乎没有 Z 响应。",
                        "Minimum bias step |V|-|V_target| for the active check; a smaller "
                        "step skips it (inconclusive) because a small step gives almost no "
                        "Z response even on a healthy junction.")),
                ParameterSpec(
                    name="min_dz_m", type="float", unit="m", required=False,
                    default=10e-12, min_value=1e-12, max_value=1e-9,
                    description=tr(
                        "判定「有 Z 响应」的最小 |ΔZ|（默认 10 pm）。",
                        "Minimum |dZ| counted as a Z response (default 10 pm).")),
                ParameterSpec(
                    name="sample_s", type="float", unit="s", required=False,
                    default=2.5, min_value=0.5, max_value=_MAX_SAMPLE_S,
                    description=tr("每个测量窗口的时长（电流中位数/Z 均值）。",
                                   "Length of each measuring window (median current / mean Z).")),
                ParameterSpec(
                    name="settle_s", type="float", unit="s", required=False,
                    default=3.0, min_value=0.2, max_value=_MAX_SETTLE_S,
                    description=tr("每次改偏压之后等反馈稳定的时间。",
                                   "Wait for the feedback to settle after each bias change.")),
                ParameterSpec(
                    name="sign_fraction", type="float", required=False, default=0.2,
                    min_value=0.01, max_value=1.0,
                    description=tr(
                        "被动检查：反号电流超过 setpoint 的这个比例才算证据。",
                        "Passive check: an opposite-sign current counts only "
                        "above this fraction of the setpoint.")),
                ParameterSpec(
                    name="withdraw_on_fail", type="bool", required=False, default=True,
                    description=tr(
                        "判定 bias_not_at_junction 时用 SafeRetract 退针（默认开）："
                        "偏压没到结上时反馈在 mV 偏压下把针尖推向近接触，留在原地是"
                        "最危险的默认。不会用 EmergencyRetract（它会先开反馈）。",
                        "On bias_not_at_junction withdraw with SafeRetract "
                        "(default on): with the bias missing the feedback pushes "
                        "the tip toward contact at mV bias, so staying put is the "
                        "dangerous default. EmergencyRetract is not used (it "
                        "turns the feedback on first).")),
            ],
            preconditions=["z_controller_on"],
            estimated_duration_s=16.0,
            composition_level=1,
            tags=["bias", "junction", "verify", "safety", "tip"],
        )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _wait(context, seconds: float) -> bool:
        """Sleep ``seconds`` with abort polling. True if aborted."""
        end = time.monotonic() + seconds
        while True:
            check = getattr(context, "check_abort", None)
            if callable(check) and check():
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(0.1, left))

    def _window(self, context, calls, *, seconds: float) -> dict:
        """Read current and Z for ``seconds``: median current, mean Z."""
        cur: list[float] = []
        zs: list[float] = []
        t_end = time.monotonic() + seconds
        aborted = False
        while True:
            c_rec = context.safe_call("Current_Get")
            calls.append(c_rec)
            c = _scalar(c_rec)
            if c is not None:
                cur.append(c)
            z_rec = context.safe_call("ZCtrl_ZPosGet")
            calls.append(z_rec)
            z = _scalar(z_rec)
            if z is not None:
                zs.append(z)
            if time.monotonic() >= t_end:
                break
            if self._wait(context, min(0.1, max(0.0, t_end - time.monotonic()))):
                aborted = True
                break
        return {
            "current_median_a": _median(cur) if cur else None,
            "z_mean_m": sum(zs) / len(zs) if zs else None,
            "n_current": len(cur),
            "n_z": len(zs),
            "aborted": aborted,
        }

    def _result(self, verdict, data, calls, *, message="", error="") -> SkillResult:
        data["verdict"] = verdict
        ok = verdict == VERDICT_OK
        return SkillResult(
            skill_name=SKILL_NAME, success=ok, data=data, nanonis_calls=calls,
            summary=message if ok else "", error="" if ok else (error or message))

    def _fail_bias(self, context, calls, data, withdraw: bool, why: str) -> SkillResult:
        data["withdraw_requested"] = withdraw
        if withdraw:
            from mast.skills.builtins.tip import SafeRetract
            try:
                rr = SafeRetract().execute(context, {})
                calls.extend(getattr(rr, "nanonis_calls", None) or [])
                data["withdraw"] = {
                    "called": True, "success": bool(rr.success),
                    "retracted": (rr.data or {}).get("retracted"),
                    "error": rr.error or ""}
                if (rr.data or {}).get("retracted") is True:
                    note = tr("已用 SafeRetract 退针并确认到位。",
                              "Tip withdrawn with SafeRetract and confirmed.")
                else:
                    note = tr(
                        "已下发 SafeRetract，但没确认到位，请手动核对针尖位置！",
                        "SafeRetract was issued but NOT confirmed in position - "
                        "check the tip position manually!")
            except Exception as exc:  # noqa: BLE001 - must still report the verdict
                data["withdraw"] = {"called": True, "success": False,
                                    "retracted": None, "error": repr(exc)}
                note = tr(f"退针调用异常（{exc!r}），请立刻手动退针！",
                          f"Withdraw call raised ({exc!r}); withdraw manually NOW!")
        else:
            data["withdraw"] = {"called": False}
            note = tr("withdraw_on_fail=False：针尖没有被退开，仍可能处于近接触！",
                      "withdraw_on_fail=False: the tip was NOT withdrawn and may be near contact!")
        msg = tr(
            f"偏压没有加到隧道结上（{why}）。{note}请检查外部偏压线/偏压接线"
            f"（常见原因是外部偏压线接触不良，重新插拔可恢复；多在粗动 XY 之后首次"
            f"出现）。修好之前这里拍的任何图都没有意义。",
            f"The bias is NOT reaching the tunnel junction ({why}). {note} "
            f"Check the external bias cable / bias wiring (a bad contact there is a "
            f"common cause, often fixed by re-plugging; it tends to first appear "
            f"after coarse XY moves). "
            f"Any image taken here is meaningless until it is fixed.")
        data["operator_message"] = msg
        return self._result(VERDICT_BAD, data, calls, error=msg)

    # -- execute ---------------------------------------------------------

    def execute(self, context, params: dict) -> SkillResult:
        calls: list = []
        active = bool(params.get("active_check", True))
        frac = float(params.get("reduce_fraction", 0.6))
        min_bias = abs(float(params.get("min_bias_v", 0.05)))
        min_dz = abs(float(params.get("min_dz_m", 10e-12)))
        min_step = abs(float(params.get("min_step_v", 0.1)))
        sample_s = float(params.get("sample_s", 2.5))
        settle_s = float(params.get("settle_s", 3.0))
        sign_frac = float(params.get("sign_fraction", 0.2))
        withdraw = bool(params.get("withdraw_on_fail", True))

        data: dict = {
            "active_check_requested": active, "active_check_done": False,
            "reduce_fraction": frac, "min_bias_v": min_bias, "min_dz_m": min_dz, "min_step_v": min_step,
            "sample_s": sample_s, "settle_s": settle_s,
            "withdraw_on_fail": withdraw, "evidence": "none",
        }

        def inconclusive(why_zh: str, why_en: str) -> SkillResult:
            data["reason"] = why_en
            return self._result(
                VERDICT_INCONCLUSIVE, data, calls,
                error=tr(f"无法判定偏压是否到结上：{why_zh}。不能当作 ok。",
                         f"Cannot tell whether the bias reaches the junction: "
                         f"{why_en}. Do NOT treat this as ok."))

        # Hard bounds (the spec can be bypassed): refuse, do not clamp.
        if not (0.2 <= frac < 1.0):
            return inconclusive(f"reduce_fraction={frac:g} 越界（需 0.2 至 <1）",
                                f"reduce_fraction={frac:g} out of range (0.2 to <1)")
        if sample_s > _MAX_SAMPLE_S or settle_s > _MAX_SETTLE_S or sample_s <= 0:
            return inconclusive("sample_s/settle_s 越界", "sample_s/settle_s out of range")

        # -- precondition: feedback on --
        fb_rec = context.safe_call("ZCtrl_OnOffGet")
        calls.append(fb_rec)
        fb = _scalar(fb_rec)
        data["feedback_on"] = None if fb is None else bool(int(fb))
        if fb is None:
            return inconclusive("读不到 Z 反馈状态", "cannot read Z feedback state")
        if not bool(int(fb)):
            return inconclusive("Z 反馈是关的（没进针或在做谱）",
                                "Z feedback is off (not engaged / spectroscopy)")

        # -- bias, setpoint --
        b_rec = context.safe_call("Bias_Get")
        calls.append(b_rec)
        bias = _scalar(b_rec)
        data["bias_v"] = bias
        if bias is None:
            return inconclusive("读不到偏压", "cannot read the bias")
        if bias == 0.0:
            return inconclusive("偏压为 0，符号检查无意义",
                                "bias is 0; the sign check is meaningless")
        sp_rec = context.safe_call("ZCtrl_SetpntGet")
        calls.append(sp_rec)
        setpoint = _scalar(sp_rec)
        data["setpoint_a"] = setpoint

        # -- measure --
        w0 = self._window(context, calls, seconds=sample_s)
        i0, z0 = w0["current_median_a"], w0["z_mean_m"]
        data.update(current_median_a=i0, z_before_m=z0,
                    n_current=w0["n_current"], n_z=w0["n_z"])
        if w0["aborted"]:
            return inconclusive("被中止", "aborted")
        if i0 is None:
            return inconclusive("读不到电流", "cannot read the current")

        # -- passive check --
        sign_agrees = _sign(i0) == _sign(bias)
        data["sign_agrees"] = sign_agrees
        passive_bad = None
        if setpoint is not None and setpoint != 0.0:
            ratio = abs(i0) / abs(setpoint)
            data["current_over_setpoint"] = ratio
            if ratio < _ENGAGED_FRACTION:
                data["engaged"] = False
                return inconclusive(
                    f"|I|={abs(i0):.3g} A 远小于 setpoint {abs(setpoint):.3g} A：针尖没进入隧穿"
                    f"或环路没收敛（既不是偏压故障的证据，也不能证明偏压正常）",
                    f"|I|={abs(i0):.3g} A is far below the setpoint {abs(setpoint):.3g} A: "
                    f"tip not tunnelling or loop not converged (neither evidence of nor "
                    f"against a bias fault)")
            passive_bad = (not sign_agrees) and ratio > sign_frac
        data["passive_bad"] = passive_bad
        if passive_bad:
            r = data["current_over_setpoint"]
            return self._fail_bias(
                context, calls, data, withdraw,
                tr(f"V={bias:+.3g} V 而 I={i0:+.3g} A，电流符号与偏压相反，|I| 是 setpoint 的 {r:.2f} 倍",
                   f"V={bias:+.3g} V but I={i0:+.3g} A: current sign opposite to the bias, "
                   f"|I| = {r:.2f} x setpoint"))

        if not active:
            data["evidence"] = "passive_only"
            if passive_bad is None:
                return inconclusive("setpoint 读不到且没做主动检查",
                                    "setpoint unreadable and no active check requested")
            return self._result(
                VERDICT_OK, data, calls,
                message=tr("被动检查通过（电流符号与偏压一致）；未做主动检查，证据较弱。",
                           "Passive check passed (current sign agrees with the bias); "
                           "active check not run, weak evidence."))

        # -- active check: target bias, same polarity, never across zero --
        mag = abs(bias)
        target_mag = max(mag * frac, min_bias)
        target = _sign(bias) * target_mag
        data["target_bias_v"] = target
        # Require a meaningful step: >= 15 % of |V| AND >= min_step_v. A tiny step
        # gives a tiny Z response even on a healthy junction, and a "no response"
        # verdict from it would be a false alarm that withdraws a good tip.
        if (target_mag > 0.85 * mag or (mag - target_mag) < min_step
                or _sign(target) != _sign(bias)):
            data["active_skipped"] = "no_meaningful_step_above_min_bias"
            return inconclusive(
                f"|V|={mag:.3g} V 太接近 min_bias_v={min_bias:g} V，没有足够的偏压台阶做主动检查"
                f"（被动检查已通过；要弱证据请 active_check=false）",
                f"|V|={mag:.3g} V is too close to min_bias_v={min_bias:g} V for an active "
                f"step (passive check passed; use active_check=false for weak evidence)")
        if z0 is None:
            return inconclusive("读不到 Z 位置，无法做主动检查",
                                "cannot read Z; no active check possible")

        restored = False
        w1 = None
        aborted = False
        try:
            rec = context.safe_call("Bias_Set", float(target))
            calls.append(rec)
            if getattr(rec, "error", ""):
                data["bias_set_error"] = rec.error
                return inconclusive(f"改偏压失败: {rec.error}", f"Bias_Set failed: {rec.error}")
            if self._wait(context, settle_s):
                aborted = True
            else:
                w1 = self._window(context, calls, seconds=sample_s)
                aborted = w1["aborted"]
        finally:
            # Restore ALWAYS (abort or exception included): allow_on_abort writes.
            try:
                rr = context.safe_call("Bias_Set", float(bias), allow_on_abort=True)
                calls.append(rr)
                chk = context.safe_call("Bias_Get")
                calls.append(chk)
                back = _scalar(chk)
                restored = (back is not None
                            and abs(back - bias) <= max(1e-3, 0.02 * abs(bias)))
                data["bias_after_restore_v"] = back
            except Exception:  # noqa: BLE001
                restored = False
        data["bias_restored"] = restored
        data["active_check_done"] = w1 is not None
        if aborted or w1 is None:
            return inconclusive(
                "主动检查被中止" + ("" if restored else "，且偏压未确认恢复！"),
                "active check aborted" + ("" if restored else "; bias NOT confirmed restored!"))
        if not restored:
            return inconclusive("偏压未能确认恢复，请先手动核对偏压！",
                                "bias restore not confirmed; verify the bias manually!")

        # Second half: wait after the restore, measure again.
        if self._wait(context, settle_s):
            return inconclusive("被中止", "aborted")
        w2 = self._window(context, calls, seconds=sample_s)
        data["settle_s_each"] = settle_s
        i1, z1 = w1["current_median_a"], w1["z_mean_m"]
        i2, z2 = w2["current_median_a"], w2["z_mean_m"]
        data.update(current_reduced_a=i1, z_reduced_m=z1,
                    current_restored_a=i2, z_restored_m=z2)
        if z1 is None or z2 is None or i1 is None:
            return inconclusive("主动检查中读不到 Z/电流",
                                "Z/current unreadable during the active check")

        dz_down = z1 - z0          # bias lowered
        dz_up = z2 - z1            # bias restored
        data["dz_lowered_m"] = dz_down
        data["dz_restored_m"] = dz_up
        data["evidence"] = "active"
        from mast.core import instrument_profile as ip
        zsign = ip.z_extend_sign_or_none()
        data["z_extend_sign"] = zsign
        data["direction_as_expected"] = None
        if zsign is not None and abs(dz_down) >= min_dz:
            # "toward the sample" = piezo extends = Z changes with z_extend_sign.
            data["direction_as_expected"] = (_sign(dz_down) == zsign)
            if not data["direction_as_expected"]:
                data["warning"] = tr(
                    "降低 |V| 后 Z 朝远离样品方向动了（与本机预期相反）：有响应，但请核对。",
                    "Z moved AWAY from the sample when |V| dropped (opposite to the "
                    "expectation here): there is a response, but please verify.")

        a_dn, a_up = abs(dz_down), abs(dz_up)
        both_small = a_dn < min_dz and a_up < min_dz
        symmetry = (min(a_dn, a_up) / max(a_dn, a_up)) if max(a_dn, a_up) > 0 else 0.0
        opposite = _sign(dz_down) * _sign(dz_up) < 0
        data["dz_symmetry"] = symmetry
        data["dz_opposite_directions"] = opposite
        # A real step response REVERSES when the bias is restored and has a
        # similar size both ways. Two big moves in the SAME direction are slow
        # drift (hundreds of pm over minutes on a dead junction), not a
        # response; so is a large/small mismatch.
        responded = (a_dn >= min_dz and a_up >= min_dz and opposite
                     and symmetry >= _MIN_SYMMETRY)
        i_rel = abs(i1 - i0) / max(abs(i0), 1e-15)
        data["current_rel_change"] = i_rel
        if not responded and not both_small:
            return inconclusive(
                f"Z 动了（降压 {dz_down * 1e12:+.1f} pm，复原 {dz_up * 1e12:+.1f} pm）但不像"
                f"台阶响应：降压/复原应方向相反、幅度接近（对称比 {symmetry:.2f}，要求 >= "
                f"{_MIN_SYMMETRY:g}）；同向大幅度是慢漂。请等漂移稳定或加大台阶后重测",
                f"Z moved (lowered {dz_down * 1e12:+.1f} pm, restored {dz_up * 1e12:+.1f} pm) "
                f"but not like a step response: the two should have opposite signs and similar "
                f"size (symmetry {symmetry:.2f}, need >= {_MIN_SYMMETRY:g}); a large same-"
                f"direction move is slow drift. Let the drift settle or use a larger step, "
                f"then repeat")
        if responded:
            return self._result(
                VERDICT_OK, data, calls,
                message=tr(f"偏压到结上：|V| 降到 {target:+.3g} V 时 Z 变化 {dz_down * 1e12:+.1f} pm，"
                           f"恢复后 {dz_up * 1e12:+.1f} pm。",
                           f"Bias reaches the junction: Z moved {dz_down * 1e12:+.1f} pm when "
                           f"|V| dropped to {target:+.3g} V and {dz_up * 1e12:+.1f} pm on restore."))
        if i_rel <= _CURRENT_UNCHANGED_REL:
            return self._fail_bias(
                context, calls, data, withdraw,
                tr(f"偏压 {bias:+.3g} → {target:+.3g} V 时 Z 几乎不动（ΔZ={dz_down * 1e12:+.1f} pm，"
                   f"恢复后 {dz_up * 1e12:+.1f} pm，阈值 {min_dz * 1e12:.0f} pm），电流也没变"
                   f"（{i0:+.3g} → {i1:+.3g} A）",
                   f"Z did not respond to bias {bias:+.3g} -> {target:+.3g} V "
                   f"(dZ={dz_down * 1e12:+.1f} pm, {dz_up * 1e12:+.1f} pm on restore, threshold "
                   f"{min_dz * 1e12:.0f} pm) and the current did not change "
                   f"({i0:+.3g} -> {i1:+.3g} A)"))
        return inconclusive(
            f"Z 响应小（{dz_down * 1e12:+.1f} pm）但电流变化了 {i_rel:.0%}，可能没稳定或有别的原因",
            f"small Z response ({dz_down * 1e12:+.1f} pm) but the current changed by "
            f"{i_rel:.0%}; not settled or another cause")


def make_tool(context_provider):
    from mast.agents._shared.skill_adapter import wrap_skill
    return wrap_skill(VerifyBiasAtJunction, context_provider)


__all__ = ["VerifyBiasAtJunction", "VERDICT_OK", "VERDICT_BAD",
           "VERDICT_INCONCLUSIVE"]
