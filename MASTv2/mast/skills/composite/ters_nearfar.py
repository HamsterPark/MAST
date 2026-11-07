"""M04 近场 / 远场对照 —— TERSNearFarPair（规格 docs/v2/design/ters_modes_spec.md M04；契约 §8.3）。

同一位置交替取近场（隧穿）与远场（撤针 Δz，或横向移到裸衬底）谱，按谱带给出对比度
C = I_NF / I_FF − 1 与它的离散；远场低于 ``ff_noise_sigma`` × σ 时只报下限（M04.1）。最后再取一次
NF_post，与第一次 NF 比：相对变化超过 ``nf_drift_max_frac`` ⇒ ``tip_changed``。

撤针 / 回针（M04.3 第 6、8 步）走子技能：ZControllerOnOff(False) → 每步 ≤ 1 nm、间隔按
``retract_speed_nm_s`` 的 SetZPosition 斜坡（每步读电流与 Z）。回针先停在撤针一侧的 z_NF + dir × guard
再闭合反馈，防止反馈从远处冲下来；回针途中电流 > ``max_return_current_a`` ⇒ 立即退针（SafeRetract）并停。

**开环 Z 的方向不写死**（规格 §1.4 第 5 条）：撤针方向 dir = −``z_extend_sign``，取自
``instrument_profile.z_extend_sign_or_none()``（+1 = 压电伸长、朝样品时 Z 读数增大）。本机没声明 ⇒ 前置检查
就拒绝（不关反馈）；z_FF、全局 Z 包络检查、回针停靠点都按 dir 算。方向填反的后果是在反馈断开、没有电流
上限的情况下把针往样品里推 ``retract_nm``，所以有两道防线：

1. **首个小步自检**：关反馈后先沿 dir 走 ``probe_step_pm``，前后各等 ``probe_settle_s`` 再读 5 次电流取均值；下降不到
   max(``probe_min_drop_frac`` × |I|, 3σ0)（电流上升更是）⇒ 判方向错，停在 needs_operator 并 SafeRetract，
   一个 1 nm 步都不走。前后电流写进 ``analysis.retract_probes``。
2. **撤针全程电流上限**：|I| > ``retract_current_limit_factor`` × 撤针开始时的 |I| ⇒ 停、SafeRetract。

**反馈断开期间被中止**：中止之后 SetZPosition / ZControllerOnOff 都会被拒（不在中止安全名单里），
针尖会停在恒高、反馈断开的状态 —— 这不是一个「保持住」的状态（热漂移会把它带向表面），所以这时
退针（``ZCtrl_Withdraw`` 中止后放行）。这是对 §1.9「STM 保持当前状态」的有意偏离，报告里列明。
"""

from __future__ import annotations

import math
import time
from typing import Iterator

import numpy as np

from mast.core.types import ParameterSpec, SkillMetadata
from mast.i18n import tr as _tr_lang
from mast.skills.composite import ters_analysis as ta
from mast.skills.composite.graph_executor import CompositeStep
from mast.skills.composite.ters_common import (
    GuardSpec,
    Needs,
    TersComposite,
    TersRun,
    common_extra,
    cp_a,
    cp_b,
    cp_c,
    ters_metadata,
)
from mast.skills.composite.ters_point import safe_withdraw

__all__ = ["TERSNearFarPair", "ZRamp"]

#: 斜坡单步上限（M04.3 第 6 步：步长 ≤ 1 nm、间隔 ≥ 20 ms）
MAX_STEP_M = 1e-9
MIN_STEP_INTERVAL_S = 0.02
#: 首个小步自检：前后各读几次电流取均值（等待时间是参数 probe_settle_s）
PROBE_READS = 5

_SIGN_HINT = _tr_lang("设置 → 退针 → z_extend_sign；判法：Withdraw 后 Z 停在正轨 ⇒ −1（伸长时 Z 减小），"
                      "停在负轨 ⇒ +1",
                      "Settings -> Withdraw -> z_extend_sign; how to tell: if Z stops on the positive "
                      "rail after Withdraw => -1 (Z decreases on extension), on the negative rail => +1")


def retract_dir() -> int | None:
    """撤针（远离样品）时 Z 读数变化的方向：−z_extend_sign。本机没声明 ⇒ None（不猜）。"""
    from mast.core.instrument_profile import z_extend_sign_or_none

    sign = z_extend_sign_or_none()
    return None if sign is None else -int(sign)


class ZRamp:
    """反馈断开时的 Z 斜坡（M04 / 以后的 M05 共用）。每一步都是 ``SetZPosition`` 子技能。"""

    def __init__(self, run: TersRun, *, speed_nm_s: float, current_limit_a: float | None,
                 stream: str | None = "z") -> None:
        self.run = run
        self.speed = max(1e-3, float(speed_nm_s)) * 1e-9
        self.limit = current_limit_a
        self.stream = stream
        self.trace: list[tuple[float, float, float | None]] = []
        self.over: dict | None = None

    def to(self, z_target: float, label: str) -> Iterator[CompositeStep]:
        """从当前 Z 斜坡到 ``z_target``。返回 True = 到了；False = 没到（原因在 run.reason 或 self.over）。"""
        run = self.run
        z0 = run.read_float("ZCtrl_ZPosGet")
        if z0 is None:
            run.stop("needs_operator", _tr_lang(f"{label}: 读不到 Z", f"{label}: cannot read Z"))
            return False
        dist = z_target - z0
        n = max(1, int(math.ceil(abs(dist) / MAX_STEP_M)))
        dt = max(MIN_STEP_INTERVAL_S, abs(dist) / n / self.speed)
        for k in range(1, n + 1):
            if run._aborted():
                return False
            t0 = time.monotonic()
            zk = z0 + dist * k / n
            res = yield from run.step(f"{label}:z{k}", "SetZPosition", {"z_pos_m": zk})
            if not res.success:
                run.stop("needs_operator", _tr_lang(f"{label}: SetZPosition({zk:.4g}) 失败：{res.error}",
                                                    f"{label}: SetZPosition({zk:.4g}) failed: {res.error}"))
                return False
            i = run.read_float("Current_Get")
            z = run.read_float("ZCtrl_ZPosGet")
            now = time.time()
            self.trace.append((now, i if i is not None else float("nan"), z))
            if self.stream and i is not None:
                vals = {"I_A": i}
                if z is not None:
                    vals["Z_m"] = z
                run.live.publish({"t": now, "values": vals}, suffix=self.stream)
            if self.limit is not None and i is not None and abs(i) > self.limit:
                self.over = {"z_m": z, "current_a": i, "limit_a": self.limit, "step": k}
                return False
            left = dt - (time.monotonic() - t0)
            if left > 0 and run.sleep(left):
                return False
        return True


class TERSNearFarPair(TersComposite):
    MODE_ID = "M04"
    MODE_KEY = "ters_nearfar"
    NEEDS = Needs(tip="tunneling")

    def metadata(self) -> SkillMetadata:
        inf = _tr_lang("【默认值可调】", " [default adjustable]")
        mode = [
            ParameterSpec(name="reference_kind", type="str", required=False, default="retract",
                          allowed_values=["retract", "lateral_bare", "both"],
                          description=_tr_lang("远场参考方式：撤针 / 横向移到裸衬底 / 两者都做。",
                                               "Far-field reference: retract / move laterally to bare "
                                               "substrate / both.") + inf),
            ParameterSpec(name="retract_nm", type="float", unit="nm", required=False, default=20.0,
                          min_value=2.0, max_value=500.0,
                          description=_tr_lang("撤针量 Δz（不得超过 Z 压电量程）。",
                                               "Retract distance Δz (must stay within the Z piezo range).") + inf),
            ParameterSpec(name="retract_speed_nm_s", type="float", unit="nm/s", required=False,
                          default=20.0, min_value=1.0, max_value=200.0,
                          description=_tr_lang("撤针 / 回针斜坡速率（单步 ≤ 1 nm、间隔 ≥ 20 ms）。",
                                               "Ramp rate for retract / return (steps <= 1 nm, interval "
                                               ">= 20 ms).") + inf),
            ParameterSpec(name="return_overshoot_guard_nm", type="float", unit="nm", required=False,
                          default=0.5, min_value=0.1, max_value=5.0,
                          description=_tr_lang(
                              "回针时先停在撤针一侧离 z_NF 该值处（z_NF + dir × 该值），再闭合反馈"
                              "（防止反馈从远处直接冲下来）。",
                              "On the way back, first stop this far from z_NF on the retract side "
                              "(z_NF + dir × this value), then close the feedback (so the feedback "
                              "does not rush in from far away).") + inf),
            ParameterSpec(name="probe_step_pm", type="float", unit="pm", required=False, default=20.0,
                          min_value=5.0, max_value=100.0,
                          description=_tr_lang(
                              "首个小步自检：关反馈后先沿撤针方向走这么一步，前后各读 5 次电流；电流没按"
                              "预期下降（或上升）⇒ 判 z_extend_sign 可能填反，退针停下，不走任何 1 nm 步。"
                              "κ ≈ 1 Å⁻¹ 时 20 pm 约降 33 %，方向反了也只升约 1.5 倍。",
                              "First small-step check: after opening the feedback, take this one step "
                              "along the retract direction and read the current 5 times before and "
                              "after; if the current does not fall as expected (or rises) => "
                              "z_extend_sign is probably wrong, the tip is withdrawn and the run "
                              "stops, without taking any 1 nm step. At κ ≈ 1 Å⁻¹ 20 pm drops the "
                              "current by about 33 %; in the wrong direction it rises by only about "
                              "1.5x.") + inf),
            ParameterSpec(name="probe_min_drop_frac", type="float", required=False, default=0.10,
                          min_value=0.02, max_value=0.9,
                          description=_tr_lang(
                              "小步之后 |I| 至少下降的比例（与 3σ0 取大者）；达不到 ⇒ 判方向错。Mimea 实测"
                              "κ_app ≈ 4.85 nm⁻¹，20 pm 降 15.8 %（余量约 1.6 倍）；κ ≈ 10 nm⁻¹ 的结降约 33 %。",
                              "Minimum fractional drop of |I| after the small step (the larger of this "
                              "and 3σ0); not reached => wrong direction. Measured on Mimea: "
                              "κ_app ≈ 4.85 nm⁻¹, 20 pm drops 15.8 % (margin about 1.6x); a junction "
                              "with κ ≈ 10 nm⁻¹ drops about 33 %.") + inf),
            ParameterSpec(name="probe_settle_s", type="float", unit="s", required=False, default=0.7,
                          min_value=0.01, max_value=5.0,
                          description=_tr_lang(
                              "关反馈后、以及小步之后，各等这么久再读电流。Mimea 的 Current_Get 约 0.3 s "
                              "刷新一次，台阶后第一次刷新只反映约 35 %、第二次（≈ 0.5 s）才完全反映；真机是"
                              "实时信号，这一等待只是让读数稳定。",
                              "Wait this long after opening the feedback and after the small step "
                              "before reading the current. Mimea's Current_Get refreshes about every "
                              "0.3 s; after a step the first refresh shows only about 35 %, the second "
                              "(≈ 0.5 s) shows it fully; on a real instrument the signal is real-time "
                              "and this wait just lets the reading settle.") + inf),
            ParameterSpec(name="retract_current_limit_factor", type="float", required=False, default=1.5,
                          min_value=1.05, max_value=10.0,
                          description=_tr_lang(
                              "撤针全程 |I| > 该值 × 撤针开始时的 |I| ⇒ 立即停、SafeRetract"
                              "（方向错的第二道防线：正确方向上电流只会下降）。",
                              "At any point during the retract |I| > this value × |I| at the start of "
                              "the retract => stop at once and SafeRetract (second line of defence "
                              "against a wrong direction: in the right direction the current only "
                              "falls).") + inf),
            ParameterSpec(name="retract_settle_s", type="float", unit="s", required=False, default=5.0,
                          min_value=0.0, max_value=120.0,
                          description=_tr_lang("撤针后等待（蠕变 + 热平衡）。",
                                               "Wait after the retract (creep + thermal equilibrium).") + inf),
            ParameterSpec(name="return_settle_s", type="float", unit="s", required=False, default=3.0,
                          min_value=0.0, max_value=120.0,
                          description=_tr_lang("回针闭合后等待。",
                                               "Wait after the feedback is closed on return.") + inf),
            ParameterSpec(name="z_return_tol_pm", type="float", unit="pm", required=False,
                          default=100.0, min_value=5.0, max_value=2000.0,
                          description=_tr_lang(
                              "回针后 Z 与撤针前差的上限；不满足再等一次 return_settle_s 复核，"
                              "仍不满足 ⇒ 退针并停。",
                              "Upper limit of the difference between Z after the return and before the "
                              "retract; if not met, wait one more return_settle_s and re-check; still "
                              "not met => withdraw and stop.") + inf),
            ParameterSpec(name="max_return_current_a", type="float", unit="A", required=False,
                          default=5e-9, min_value=1e-11, max_value=2e-8,
                          description=_tr_lang(
                              "回针过程中允许的最大电流；超过 ⇒ 立即退针（SafeRetract）并停。",
                              "Maximum current allowed during the return; exceeded => withdraw at once "
                              "(SafeRetract) and stop.") + inf),
            ParameterSpec(name="cycles", type="int", required=False, default=3, min_value=1,
                          max_value=20,
                          description=_tr_lang("NF–FF 循环次数（最后另加一次 NF_post）。",
                                               "Number of NF-FF cycles (one extra NF_post at the end).") + inf),
            ParameterSpec(name="bands_cm1", type="str", required=True,
                          description=_tr_lang(
                              "用于计算对比度的谱带 '名称:lo-hi,…'（cm⁻¹，必填），如 "
                              "'ring:1540-1620'。",
                              "Bands used for the contrast 'name:lo-hi,...' (cm⁻¹, required), e.g. "
                              "'ring:1540-1620'.")),
            ParameterSpec(name="baseline_cm1", type="str", required=False, default="",
                          description=_tr_lang("基线窗口 'lo-hi,lo-hi'；空 = 谱带两侧各 20 cm⁻¹。",
                                               "Baseline windows 'lo-hi,lo-hi'; empty = 20 cm⁻¹ on each "
                                               "side of the band.")),
            ParameterSpec(name="bare_x_m", type="float", unit="m", required=False, default=None,
                          min_value=-1.5e-6, max_value=1.5e-6,
                          description=_tr_lang("lateral_bare / both 时的裸衬底位置 X。",
                                               "Bare-substrate position X for lateral_bare / both.")),
            ParameterSpec(name="bare_y_m", type="float", unit="m", required=False, default=None,
                          min_value=-1.5e-6, max_value=1.5e-6,
                          description=_tr_lang("裸衬底位置 Y。", "Bare-substrate position Y.")),
            ParameterSpec(name="nf_drift_max_frac", type="float", required=False, default=0.3,
                          min_value=0.01, max_value=1.0,
                          description=_tr_lang("|NF_post / NF_pre − 1| 超过该值 ⇒ tip_changed。",
                                               "|NF_post / NF_pre − 1| above this value => tip_changed.") + inf),
            ParameterSpec(name="ff_noise_sigma", type="float", required=False, default=3.0,
                          min_value=1.0, max_value=10.0,
                          description=_tr_lang("FF 低于 N σ 时只报对比度下限。",
                                               "When FF is below N σ only a lower bound of the contrast "
                                               "is reported.") + inf),
        ]
        return ters_metadata(
            name="TERSNearFarPair",
            description=_tr_lang(
                "近场 / 远场对照（M04）：同一位置交替取近场（隧穿）谱与远场谱（撤针 retract_nm，或横向移到"
                "裸衬底），按谱带给出对比度 C = I_NF/I_FF − 1（均值 ± SD；远场低于噪声时只报下限），并用"
                "首尾两次近场谱判断针尖是否变了。撤针 / 回针是反馈断开下的 Z 斜坡（≤ 1 nm/步），方向取自本机声明的 "
                "z_extend_sign（没声明就拒绝）；撤针前先走一个 probe_step_pm 的小步，电流没按预期下降 ⇒ 判方向填反、"
                "退针停下；撤针全程 |I| 超过开始时的 retract_current_limit_factor 倍也停。回针先停在撤针一侧离 z_NF "
                "0.5 nm 处再闭合反馈，回针途中电流超过 max_return_current_a 立即退针。"
                "LiveHub：local/ters_nearfar/<run_id>（每周期 NF / FF / C）、…/z（斜坡的 Z 与 I）。"
                "dry_run=true 只做只读判据并规划 Z 目标，不撤针、不曝光；dry_run_motion=true 真的撤针 / 回针"
                "（同样的斜坡、同样的过流与回位检查），只是不曝光。",
                "Near-field / far-field comparison (M04): at the same position, alternately acquire a "
                "near-field (tunnelling) spectrum and a far-field spectrum (retract by retract_nm, or "
                "move laterally to bare substrate), and give the contrast per band C = I_NF/I_FF − 1 "
                "(mean ± SD; only a lower bound when the far field is below the noise); the first and "
                "last near-field spectra tell whether the tip changed. Retract / return are Z ramps "
                "with the feedback open (<= 1 nm/step); the direction comes from the z_extend_sign "
                "declared for this machine (refused if not declared). Before retracting, a small step of "
                "probe_step_pm is taken; if the current does not fall as expected => the direction is "
                "judged wrong, the tip is withdrawn and the run stops; the run also stops if |I| at any "
                "point during the retract exceeds retract_current_limit_factor times its value at the "
                "start. On return the tip first stops 0.5 nm from z_NF on the retract side and then "
                "closes the feedback; if the current exceeds max_return_current_a during the return the "
                "tip is withdrawn at once. LiveHub: local/ters_nearfar/<run_id> (NF / FF / C per cycle), "
                ".../z (Z and I of the ramps). dry_run=true only runs the read-only checks and plans "
                "the Z targets, without retracting or exposing; dry_run_motion=true really retracts / "
                "returns (same ramps, same overcurrent and return-position checks), just without "
                "exposing."),
            parameters=[*mode, *cp_a(), *cp_b(), *cp_c(), *common_extra()],
            tags=["optics", "ters", "spectrum", "acquisition", "reference", "contrast"],
            duration_s=300.0,
        )

    # -- plan --
    def prepare(self, run: TersRun) -> list[str]:
        p = run.p
        out = []
        if not run.bands:
            out.append(_tr_lang("bands_cm1 必填：对比度按谱带计算",
                                "bands_cm1 is required: the contrast is computed per band"))
        kind = p["reference_kind"]
        if kind in ("lateral_bare", "both") and (p.get("bare_x_m") is None or p.get("bare_y_m") is None):
            out.append(_tr_lang(f"reference_kind={kind} 时 bare_x_m / bare_y_m 必填",
                                f"bare_x_m / bare_y_m are required when reference_kind={kind}"))
        cycles = int(p["cycles"])
        per_ff = int(kind in ("retract", "both")) + int(kind in ("lateral_bare", "both"))
        run.total_points = cycles * (1 + per_ff) + 1
        t_exp = float(p["exposure_ms"]) / 1000.0 * int(p["frames"])
        ramp = 2 * float(p["retract_nm"]) / float(p["retract_speed_nm_s"]) + 2 * float(p["probe_settle_s"])
        per = (t_exp * (1 + per_ff) + float(p["settle_s"]) + float(p["stable_window_s"])
               + (ramp + float(p["retract_settle_s"]) + float(p["return_settle_s"])
                  if kind in ("retract", "both") else 0.0)
               + (10.0 if kind in ("lateral_bare", "both") else 0.0) + 1.0)
        est = 15.0 + cycles * per + t_exp
        run.plan.update({"cycles": cycles, "reference_kind": kind, "estimated_s": round(est, 1),
                         "retract_nm": float(p["retract_nm"])})
        if est > float(p["max_total_s"]):
            out.append(_tr_lang(f"预计 {est:.0f} s 超过 max_total_s={float(p['max_total_s']):.0f} s",
                                f"Estimated {est:.0f} s exceeds max_total_s={float(p['max_total_s']):.0f} s"))
        return out

    def preflight_extra(self, run: TersRun) -> Iterator[CompositeStep]:
        """SafeTip 阈值配套（M04.3 第 2 步）与 Z 目标在全局包络内（SetZPosition 的 z_pos 界）。"""
        p = run.p
        out: list[str] = []
        res = yield from run.step("preflight:safetip_props", "GetSafeTipProps", {})
        thr = (res.data or {}).get("threshold") if res.success else None
        i_set = abs(float(run.i_set or 0.0))
        run.facts["safetip_threshold_a"] = thr
        if thr is None or not thr:
            out.append(_tr_lang("SafeTip 阈值读不到（GetSafeTipProps）",
                                "Cannot read the SafeTip threshold (GetSafeTipProps)"))
        elif i_set and not (1.2 * i_set <= abs(float(thr)) <= 10 * float(p["max_return_current_a"])):
            out.append(_tr_lang(
                f"SafeTip 阈值 {float(thr):.3g} A 与本模式不配套：要求 ≥ 1.2×I_set "
                f"({1.2 * i_set:.3g} A) 且 ≤ 10×max_return_current_a "
                f"({10 * float(p['max_return_current_a']):.3g} A)",
                f"SafeTip threshold {float(thr):.3g} A does not suit this mode: it must be >= 1.2×I_set "
                f"({1.2 * i_set:.3g} A) and <= 10×max_return_current_a "
                f"({10 * float(p['max_return_current_a']):.3g} A)"))
        if p["reference_kind"] in ("retract", "both"):
            d = retract_dir()
            self._dir = d
            run.facts["retract_dir"] = d
            if d is None:
                out.append(_tr_lang(
                    "本机没有声明 z_extend_sign（压电伸长、朝样品时 Z 读数的符号）—— 开环撤针的方向未知，"
                    f"拒绝开始（不关反馈）。{_SIGN_HINT}",
                    "z_extend_sign is not declared for this machine (the sign of the Z reading when the "
                    "piezo extends towards the sample) - the direction of the open-loop retract is "
                    f"unknown, refusing to start (the feedback is not opened). {_SIGN_HINT}"))
                return out
            z = run.read_float("ZCtrl_ZPosGet")
            if z is None:
                out.append(_tr_lang("读不到 Z", "Cannot read Z"))
            else:
                lim = _z_limits(run)
                z_ff = z + d * float(p["retract_nm"]) * 1e-9
                run.plan["z_targets_m"] = {"z_nf": z, "z_ff": z_ff, "retract_dir": d,
                                           "z_probe": z + d * float(p["probe_step_pm"]) * 1e-12,
                                           "z_return": z + d * float(p["return_overshoot_guard_nm"]) * 1e-9,
                                           "global_z_limits_m": lim}
                lo, hi = min(z, z_ff), max(z, z_ff)
                if lim is not None and not (lim[0] <= lo and hi <= lim[1]):
                    out.append(_tr_lang(
                        f"撤针目标 [{lo:.4g}, {hi:.4g}] m 超出 SetZPosition 的全局 Z 包络 "
                        f"[{lim[0]:.4g}, {lim[1]:.4g}] m —— 斜坡会被安全闸拒绝，不开始",
                        f"Retract target [{lo:.4g}, {hi:.4g}] m is outside the global Z envelope of "
                        f"SetZPosition [{lim[0]:.4g}, {lim[1]:.4g}] m - the ramp would be refused by "
                        "the safety gate, not starting"))
        return out

    def attestation_lines(self, run: TersRun) -> list[str]:
        if run.p["reference_kind"] in ("retract", "both"):
            d = getattr(self, "_dir", None)
            way = {1: _tr_lang("Z 增大", "Z increases"),
                   -1: _tr_lang("Z 减小", "Z decreases")}.get(d, _tr_lang("方向未声明", "direction not declared"))
            return [_tr_lang(
                f"将在反馈断开下撤针 {run.p['retract_nm']} nm（撤针 = {way}，取自 z_extend_sign；先走 "
                f"{run.p['probe_step_pm']:g} pm 小步自检）再回针（回针电流上限 "
                f"{run.p['max_return_current_a']:.2g} A）",
                f"Will retract {run.p['retract_nm']} nm with the feedback open (retract = {way}, taken "
                f"from z_extend_sign; a {run.p['probe_step_pm']:g} pm small-step check comes first), then "
                f"return (return current limit {run.p['max_return_current_a']:.2g} A)")]
        return []

    def dry_plan(self, run: TersRun) -> None:
        run.plan["note"] = _tr_lang("dry_run：未撤针、未曝光；z_targets_m 是会走的 Z 目标",
                                    "dry_run: not retracted, not exposed; z_targets_m are the Z targets "
                                    "that would be used")

    # -- sequence --
    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        bands = [b.name for b in run.bands]
        run.live.open_main("xy", title=_tr_lang("TERS 近场 / 远场对照", "TERS near-field / far-field comparison"),
                           x_label="cycle", y_label="intensity", y_unit="counts")
        run.live.open_extra("z", "scalar", title=_tr_lang("撤针 / 回针 Z 与 I", "Retract / return Z and I"),
                            y_label="I", y_unit="A")
        run.stm_target = "z"
        z_nf = run.read_float("ZCtrl_ZPosGet")
        xy_nf = run.read_xy()
        i_set = float(run.i_set or 0.0)
        frac = float(p["exposure_guard_current_frac"])
        kind = p["reference_kind"]
        self._rows: dict[str, dict] = {}
        for c in range(int(p["cycles"])):
            if run.check():
                break
            nf = yield from self._near(run, f"c{c}_NF", c, "NF", i_set, frac)
            if nf is None:
                break
            if kind in ("retract", "both"):
                ok = yield from self._retract_ff(run, c, z_nf, i_set)
                if not ok:
                    break
            if kind in ("lateral_bare", "both"):
                ok = yield from self._bare_ff(run, c, xy_nf, i_set, frac)
                if not ok:
                    break
            self._publish_cycle(run, c, bands)
        if not run.stopped:
            yield from self._near(run, "NF_post", int(p["cycles"]), "NF_post", i_set, frac)
        self._analyse(run, bands)

    def _near(self, run: TersRun, key: str, cycle: int, phase: str, i_set: float,
              frac: float) -> Iterator[CompositeStep]:
        tr = run.ready_or_policy(label=key, key=key, i_target=i_set, feedback=True)
        if not tr.passed:
            if not run.stopped:
                run.stop("needs_operator",
                         _tr_lang(f"{key}: 近场判稳失败（{tr.reason}）—— 对照序列没有意义了",
                                  f"{key}: near-field settling failed ({tr.reason}) - the comparison "
                                  "sequence is meaningless"))
            run.record(None, key=key, status="unstable_tip", tip=tr,
                       values={"phase": phase, "cycle": cycle})
            return None
        out = yield from run.acquire(key, key, guard=GuardSpec(i_ref_a=i_set, frac=frac,
                                                               expect_feedback=True),
                                     extra={"phase": phase, "cycle": cycle,
                                            "tip_ready": tr.as_meta(),
                                            "reference_kind": run.p["reference_kind"]})
        run.record(out, key=key, tip=tr, values={"phase": phase, "cycle": cycle})
        self._rows[key] = {"phase": phase, "cycle": cycle, "out": out}
        return out if run.handle(out) else None

    def _retract_ff(self, run: TersRun, c: int, z_nf: float | None,
                    i_set: float) -> Iterator[CompositeStep]:
        p = run.p
        if z_nf is None:
            run.stop("needs_operator", _tr_lang("读不到 z_NF", "Cannot read z_NF"))
            return False
        key = f"c{c}_FF"
        d = getattr(self, "_dir", None)
        if d not in (1, -1):              # preflight refuses first; this is the belt to that brace
            run.stop("needs_operator",
                     _tr_lang(f"{key}: 撤针方向未知（z_extend_sign 未声明）—— 不撤针。{_SIGN_HINT}",
                              f"{key}: retract direction unknown (z_extend_sign not declared) - not "
                              f"retracting. {_SIGN_HINT}"))
            return False
        res = yield from run.step(f"{key}:fb_off", "ZControllerOnOff", {"enable": False})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"{key}: 关反馈失败：{res.error}",
                                                f"{key}: opening the feedback failed: {res.error}"))
            return False
        run.note_change("z_feedback", original=True, current=False, how="far-field retract")
        # 1) first small step along d: the current must fall, or the declared sign is wrong
        i_start = yield from self._probe_direction(run, key, z_nf, d)
        if i_start is None:
            return False
        # 2) the ramp itself carries a current ceiling: on the right side the current only falls
        limit = float(p["retract_current_limit_factor"]) * i_start
        ramp = ZRamp(run, speed_nm_s=float(p["retract_speed_nm_s"]), current_limit_a=limit)
        dz = float(p["retract_nm"]) * 1e-9
        z_ff = z_nf + d * dz
        ok = yield from ramp.to(z_ff, f"{key}:retract")
        if ramp.over is not None:
            yield from self._bail(run, key, _tr_lang(
                f"撤针途中电流 {ramp.over['current_a']:.3g} A 超过上限 "
                f"{limit:.3g} A（{float(p['retract_current_limit_factor']):g} × 撤针开始时的 "
                f"{i_start:.3g} A，第 {ramp.over['step']} 步）—— 针在往样品走？"
                "z_extend_sign 可能填反",
                f"current during the retract {ramp.over['current_a']:.3g} A exceeds the limit "
                f"{limit:.3g} A ({float(p['retract_current_limit_factor']):g} × {i_start:.3g} A at the "
                f"start of the retract, step {ramp.over['step']}) - is the tip moving towards the "
                "sample? z_extend_sign may be wrong"))
            return False
        if not ok:
            yield from self._bail(run, key, _tr_lang("撤针斜坡没走完", "the retract ramp did not complete"))
            return False
        if run.sleep(float(p["retract_settle_s"])):
            yield from self._bail(run, key, _tr_lang("撤针后等待时被中止", "aborted while waiting after the retract"))
            return False
        z_now = run.read_float("ZCtrl_ZPosGet")
        i_now = run.read_float("Current_Get")
        noise = max(3.0 * float(run.sigma0 or 0.0), 0.1 * abs(i_set))
        if z_now is None or abs(z_now - z_ff) > 0.1 * dz:
            yield from self._bail(run, key, _tr_lang(
                f"撤针后 Z 读回 {z_now!r} 与目标 {z_ff:.4g} 差 > 10 %",
                f"Z read back after the retract {z_now!r} differs from the target {z_ff:.4g} by > 10 %"))
            return False
        if i_now is None or abs(i_now) > noise:
            yield from self._bail(run, key, _tr_lang(
                f"撤针后电流 {i_now!r} A 没降到噪声（≤ {noise:.3g} A）—— "
                "Z 读回与真实位移不符？不曝光 FF",
                f"current after the retract {i_now!r} A did not fall to the noise (<= {noise:.3g} A) - "
                "does the Z read-back disagree with the real displacement? Not exposing FF"))
            return False
        out = yield from run.acquire(key, key, guard=GuardSpec(i_ref_a=i_set, frac=1.0,
                                                               expect_feedback=False),
                                     extra={"phase": "FF", "cycle": c, "retract_nm": p["retract_nm"],
                                            "z_nf_m": z_nf, "z_ff_m": z_now})
        run.record(out, key=key, values={"phase": "FF", "cycle": c, "z_ff_m": z_now,
                                         "reference": "retract"})
        self._rows[key] = {"phase": "FF", "cycle": c, "out": out}
        # dry_run_motion: the FF "acquisition" is the dry check and comes back dry_run — not a failure;
        # the return ramp below runs with every guard, exactly as for real
        if not out.ok and out.status != "dry_run":
            run.handle(out)
            yield from self._bail(run, key, _tr_lang(f"FF 采集 {out.status}", f"FF acquisition {out.status}"))
            return False
        ok = yield from self._return(run, key, z_nf, i_set)
        return ok

    def _probe_direction(self, run: TersRun, key: str, z_nf: float, d: int) -> Iterator[CompositeStep]:
        """首个小步自检（规格 §1.4 第 5 条）。feedback 已断开：读 |I| → 沿 d 走 probe_step_pm → 再读。

        返回撤针开始时的 |I|（斜坡电流上限的基准）；方向不对 / 读不到 ⇒ 已 _bail，返回 None。"""
        p = run.p
        step = float(p["probe_step_pm"]) * 1e-12
        settle = float(p["probe_settle_s"])
        rec: dict = {"key": key, "retract_dir": d, "probe_step_pm": float(p["probe_step_pm"]),
                     "probe_settle_s": settle, "z_nf_m": z_nf}
        run.analysis.setdefault("retract_probes", []).append(rec)
        # "before" is read after the same wait as "after": a simulator that refreshes its current
        # slowly (Mimea ≈ 0.3 s) would otherwise compare a stale value with a stale value
        if run.sleep(settle):
            rec["passed"] = False
            yield from self._bail(run, key, _tr_lang("关反馈后、小步之前被中止",
                                                     "aborted after opening the feedback, before the small step"))
            return None
        before = self._mean_abs_current(run)
        rec["i_before_a"] = before
        if before is None or before <= 0.0:
            rec["passed"] = False
            yield from self._bail(run, key, _tr_lang(
                "首个小步自检：撤针前读不到隧穿电流，没法判断方向 —— 不撤针",
                "first small-step check: no tunnelling current can be read before the retract, so the "
                "direction cannot be judged - not retracting"))
            return None
        res = yield from run.step(f"{key}:probe", "SetZPosition", {"z_pos_m": z_nf + d * step})
        if not res.success:
            rec["passed"] = False
            yield from self._bail(run, key, _tr_lang(f"首个小步 SetZPosition 失败：{res.error}",
                                                     f"first small-step SetZPosition failed: {res.error}"))
            return None
        if run.sleep(settle):
            rec["passed"] = False
            yield from self._bail(run, key, _tr_lang("首个小步之后被中止", "aborted after the first small step"))
            return None
        after = self._mean_abs_current(run)
        need = max(float(p["probe_min_drop_frac"]) * before, 3.0 * float(run.sigma0 or 0.0))
        drop = (before - after) if after is not None else None
        rec.update({"i_after_a": after, "drop_a": drop, "required_drop_a": need,
                    "drop_frac": (drop / before) if drop is not None else None,
                    "z_after_m": run.read_float("ZCtrl_ZPosGet")})
        rec["passed"] = bool(drop is not None and drop >= need)
        if not rec["passed"]:
            rising = after is not None and after > before
            trend = _tr_lang("反而上升", "rose instead") if rising else _tr_lang("没降够", "did not fall enough")
            after_s = after if after is None else format(after, '.3g')
            yield from self._bail(
                run, key, _tr_lang(
                    f"首个小步自检不过：沿撤针方向（dir={d:+d}）走 {p['probe_step_pm']:g} pm 后 |I| "
                    f"{before:.3g} → {after_s} A，{trend}"
                    f"（要求至少降 {need:.3g} A）—— z_extend_sign 可能填反，不走 1 nm 斜坡",
                    f"first small-step check failed: after {p['probe_step_pm']:g} pm along the retract "
                    f"direction (dir={d:+d}) |I| went {before:.3g} -> {after_s} A, {trend} "
                    f"(at least a {need:.3g} A drop required) - z_extend_sign may be wrong, not "
                    "taking the 1 nm ramp"))
            return None
        return before

    @staticmethod
    def _mean_abs_current(run: TersRun) -> float | None:
        vals = [run.read_float("Current_Get") for _ in range(PROBE_READS)]
        vals = [abs(v) for v in vals if v is not None]
        return float(np.mean(vals)) if vals else None

    def _return(self, run: TersRun, key: str, z_nf: float, i_set: float) -> Iterator[CompositeStep]:
        p = run.p
        ramp = ZRamp(run, speed_nm_s=float(p["retract_speed_nm_s"]),
                     current_limit_a=float(p["max_return_current_a"]))
        # the stop short of z_NF is on the retract side, whichever sign that is
        d = getattr(self, "_dir", None) or 1          # only reached after _retract_ff checked it
        guard_z = z_nf + d * float(p["return_overshoot_guard_nm"]) * 1e-9
        ok = yield from ramp.to(guard_z, f"{key}:return")
        if ramp.over is not None:
            run.stop("needs_operator", _tr_lang(
                f"{key}: 回针途中电流 {ramp.over['current_a']:.3g} A 超过上限 "
                f"{ramp.over['limit_a']:.3g} A —— 已退针",
                f"{key}: current during the return {ramp.over['current_a']:.3g} A exceeds the limit "
                f"{ramp.over['limit_a']:.3g} A - tip withdrawn"))
            yield from self._safe_retract(run, key)
            return False
        if not ok:
            yield from self._bail(run, key, _tr_lang("回针斜坡没走完", "the return ramp did not complete"))
            return False
        res = yield from run.step(f"{key}:fb_on", "ZControllerOnOff", {"enable": True})
        if not res.success:
            yield from self._bail(run, key, _tr_lang(f"回针后闭合反馈失败：{res.error}",
                                                     f"closing the feedback after the return failed: {res.error}"))
            return False
        run.note_restored("z_feedback", True)
        tol = float(p["z_return_tol_pm"]) * 1e-12
        for attempt in range(2):
            if run.sleep(float(p["return_settle_s"])):
                return False
            z = run.read_float("ZCtrl_ZPosGet")
            cur = [run.read_float("Current_Get") for _ in range(5)]
            cur = [v for v in cur if v is not None]
            i_mean = float(np.mean(cur)) if cur else None
            run.facts.setdefault("returns", []).append(
                {"key": key, "attempt": attempt, "z_m": z, "z_nf_m": z_nf, "i_mean_a": i_mean})
            if (z is not None and abs(z - z_nf) <= tol and i_mean is not None
                    and abs(i_mean - i_set) <= 0.2 * abs(i_set)):
                self._rows.setdefault(key, {})["z_return_pm"] = (z - z_nf) * 1e12
                return True
        run.stop("needs_operator", _tr_lang(
            f"{key}: 回针后 Z / 电流没回到撤针前（z={z!r}, z_NF={z_nf:.4g}, "
            f"I={i_mean!r}）—— 已退针",
            f"{key}: Z / current did not return to their pre-retract values after the return "
            f"(z={z!r}, z_NF={z_nf:.4g}, I={i_mean!r}) - tip withdrawn"))
        yield from self._safe_retract(run, key)
        return False

    def _bare_ff(self, run: TersRun, c: int, xy_nf, i_set: float,
                 frac: float) -> Iterator[CompositeStep]:
        p = run.p
        key = f"c{c}_FF_bare"
        res = yield from run.step(f"{key}:move", "MoveToXY",
                                  {"x_m": float(p["bare_x_m"]), "y_m": float(p["bare_y_m"]),
                                   "wait": True})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"{key}: 移到裸衬底失败：{res.error}",
                                                f"{key}: moving to the bare substrate failed: {res.error}"))
            return False
        tr = run.ready_or_policy(label=key, key=key, i_target=i_set, feedback=True)
        if tr.passed:
            out = yield from run.acquire(key, key, guard=GuardSpec(i_ref_a=i_set, frac=frac,
                                                                   expect_feedback=True),
                                         extra={"phase": "FF_bare", "cycle": c,
                                                "bare_xy_m": [p["bare_x_m"], p["bare_y_m"]]})
            run.record(out, key=key, tip=tr, values={"phase": "FF_bare", "cycle": c,
                                                     "reference": "lateral_bare"})
            self._rows[key] = {"phase": "FF_bare", "cycle": c, "out": out}
            if not run.handle(out):
                return False
        else:
            run.record(None, key=key, status="unstable_tip", tip=tr, values={"phase": "FF_bare"})
            if run.stopped:
                return False
        if xy_nf is None:
            run.stop("needs_operator", _tr_lang("读不到近场点的 XY，回不去",
                                                "Cannot read the XY of the near-field point, cannot go back"))
            return False
        res = yield from run.step(f"{key}:back", "MoveToXY",
                                  {"x_m": float(xy_nf[0]), "y_m": float(xy_nf[1]), "wait": True})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"{key}: 回近场点失败：{res.error}",
                                                f"{key}: returning to the near-field point failed: {res.error}"))
            return False
        return True

    def _bail(self, run: TersRun, key: str, why: str) -> Iterator[CompositeStep]:
        """撤针相出问题：反馈断开、针尖悬在半空 —— 不留在那里，退针。"""
        if not run.stopped:
            if run._aborted():
                run.stop("partial", _tr_lang(f"aborted by operator（{key}: 反馈断开期间）—— 已退针",
                                             f"aborted by operator ({key}: while the feedback was open) "
                                             "- tip withdrawn"))
            else:
                run.stop("needs_operator", _tr_lang(f"{key}: {why} —— 已退针", f"{key}: {why} - tip withdrawn"))
        yield from self._safe_retract(run, key)

    def _safe_retract(self, run: TersRun, key: str) -> Iterator[CompositeStep]:
        if run._aborted():
            safe_withdraw(run, _tr_lang(f"{key}: 反馈断开期间被中止",
                                        f"{key}: aborted while the feedback was open"))
            return
        res = yield from run.step(f"{key}:safe_retract", "SafeRetract", {})
        run.facts.setdefault("safe_retracts", []).append(
            {"key": key, "success": res.success, "error": res.error,
             "retracted": (res.data or {}).get("retracted")})
        if not res.success:
            safe_withdraw(run, _tr_lang(f"{key}: SafeRetract 失败（{res.error}）",
                                        f"{key}: SafeRetract failed ({res.error})"))

    # -- analysis / live --
    def _intens(self, out, band: str) -> tuple[float | None, float | None]:
        if out is None or not out.ok:
            return None, None
        m = out.bands.get(band) or {}
        if not m.get("valid"):
            return None, None
        sig = m.get("baseline_sigma")
        return float(m["area"]), (float(sig) * math.sqrt(m["n_px"]) if sig else None)

    def _publish_cycle(self, run: TersRun, c: int, bands: list[str]) -> None:
        vals: dict[str, float] = {}
        for b in bands:
            nf, _ = self._intens(self._rows.get(f"c{c}_NF", {}).get("out"), b)
            ff, sff = self._intens(self._rows.get(f"c{c}_FF", {}).get("out")
                                   or self._rows.get(f"c{c}_FF_bare", {}).get("out"), b)
            if nf is not None:
                vals[f"{b}_NF"] = nf
            if ff is not None:
                vals[f"{b}_FF"] = ff
            if nf is not None and ff is not None:
                con = ta.contrast(nf, ff, sff, float(run.p["ff_noise_sigma"]))
                if con.get("valid"):
                    vals[f"{b}_C"] = con["C"]
        if vals:
            run.live.publish({"x": float(c), "values": vals})

    def _analyse(self, run: TersRun, bands: list[str]) -> None:
        """analysis.contrast = {band: …}; tip_changed / nf_post_vs_pre sit beside it. Kept this
        shallow on purpose: the API's jsonable cuts at depth 6 and per-cycle rows used to land on 7."""
        p = run.p
        out: dict = {}
        run.analysis["tip_changed"] = None
        for b in bands:
            per_cycle = []
            for c in range(int(p["cycles"])):
                nf, _ = self._intens(self._rows.get(f"c{c}_NF", {}).get("out"), b)
                for ref in ("FF", "FF_bare"):
                    row = self._rows.get(f"c{c}_{ref}")
                    if not row:
                        continue
                    ff, sff = self._intens(row.get("out"), b)
                    con = ta.contrast(nf, ff, sff, float(p["ff_noise_sigma"]))
                    per_cycle.append({"cycle": c, "reference": ref, "I_NF": nf, "I_FF": ff,
                                      "sigma_FF": sff, **con})
            cs = [r["C"] for r in per_cycle if r.get("valid") and not r.get("lower_bound")]
            lbs = [r["C"] for r in per_cycle if r.get("valid") and r.get("lower_bound")]
            out[b] = {
                "per_cycle": per_cycle,
                "C_mean": float(np.mean(cs)) if cs else None,
                "C_sd": float(np.std(cs, ddof=1)) if len(cs) > 1 else None,
                "n": len(cs), "lower_bounds": lbs,
                "C_lower_bound_min": float(min(lbs)) if lbs else None,
            }
        pre = self._rows.get("c0_NF", {}).get("out")
        post = self._rows.get("NF_post", {}).get("out")
        if bands and pre is not None and post is not None:
            a, _ = self._intens(pre, bands[0])
            z, _ = self._intens(post, bands[0])
            if a and z is not None:
                rel = z / a - 1.0
                run.analysis["nf_post_vs_pre"] = rel
                run.analysis["tip_changed"] = abs(rel) > float(p["nf_drift_max_frac"])
                if run.analysis["tip_changed"]:
                    run.qc["tip_changed"] += 1
                    run.warnings.append(f"tip_changed：NF_post/NF_pre − 1 = {rel:+.2f}（{bands[0]}）")
        run.analysis["contrast"] = out

    def product_arrays(self, run: TersRun) -> dict:
        keys = [k for k, _ in run.spectra]
        return {"phase": np.asarray([str(self._rows.get(k, {}).get("phase", "")) for k in keys]),
                "cycle": np.asarray([int(self._rows.get(k, {}).get("cycle", -1)) for k in keys])}


def _z_limits(run: TersRun) -> tuple[float, float] | None:
    """SetZPosition 的全局 Z 包络（管理员覆写合并后的 SafetyLimits.z_min_m / z_max_m）。"""
    try:
        guard = run.ctx._safety_guard()
        lim = guard._limits
        return float(lim.z_min_m), float(lim.z_max_m)
    except Exception:  # noqa: BLE001
        try:
            from mast.config import SafetyLimits

            lim = SafetyLimits()
            return float(lim.z_min_m), float(lim.z_max_m)
        except Exception:  # noqa: BLE001
            return None
