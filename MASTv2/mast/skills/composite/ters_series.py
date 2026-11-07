"""同一位置的序列模式（规格 docs/v2/design/ters_modes_spec.md M06 / M07 / M08 / M09；契约 §8.3）。

* :class:`TERSBiasSeries`（M06）—— 偏压序列：BiasSettleChange（穿零保护）→ 按 hold_mode 调设定点 →
  等 Z 蠕变 → 判稳 → 曝光；|V| 上限 1.75 V（STML 截止），越过要激光关闭对照；定期回参考偏压取参考谱。
* :class:`TERSSetpointSeries`（M07）—— 设定点序列：反馈闭合、固定偏压，按设定点电流改变间隙；
  相邻比值超过 max_step_ratio 自动插几何中间点；结束拟合 κ_app、把电流轴换成相对间隙。
* :class:`TERSPolarizationSeries`（M08）—— 偏振序列：单向趋近转偏振台（反向间隙 ±0.3°），每个角度重新判稳；
  末尾回起点角再测一次；I(θ) 拟合 A·cos²(θ − θ0) + B。
* :class:`TERSTimeSeries`（M09）—— 时间序列：针尖不动，一次（或分块）采 N 帧，同时按 stm_log_hz 记 I、Z；
  逐帧谱带强度实时推 LiveHub，信号下降可自动停。

所有序列：结束（正常完成）时把本运行改过的偏压 / 设定点 / 反馈还原成开始前读到的值；中止之后不还原，
结果里写「未还原」和原值（契约 §10.3）。唯一的例外是反馈：M06 ``constant_height`` 关着反馈时被中止，
反馈闭合不回去（中止后 ZControllerOnOff 被拒），恒高、无人看管的针尖会被漂移带向表面 —— 这时退针
（``safe_withdraw``，与 M01 / M02 的 hold、M04 的撤针相同），偏压与设定点照旧不动。
"""

from __future__ import annotations

import math
import queue
import threading
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
# human_channel is looked up on the module at call time, so tests (and hosts) can swap it
from mast.skills.composite import ters_common as _tc

__all__ = ["TERSBiasSeries", "TERSSetpointSeries", "TERSPolarizationSeries", "TERSTimeSeries"]

_INF = "【默认值可调】"
_INF_EN = "[default adjustable]"


def _restore_conditions(run: TersRun, *, bias_v: float | None, setpoint_a: float | None,
                        feedback: bool | None = None) -> Iterator[CompositeStep]:
    """正常结束时把偏压 / 设定点 / 反馈放回开始前读到的值（中止之后不做 —— ctx.run 也会拒）。

    中止之后唯一要做的：反馈是本运行关掉的、还没闭合（constant_height）⇒ 退针。闭合本身在中止后被拒，
    留着恒高、反馈断开的针尖不是「保持现状」；偏压与设定点照旧不动，作为「未还原」报出。"""
    if run._aborted():
        if "z_feedback" in run.changes:
            safe_withdraw(run, _tr_lang("constant_height 期间被中止，反馈无法重新闭合",
                                        "aborted during constant_height, feedback cannot be re-closed"))
        return
    if feedback is True and "z_feedback" in run.changes:
        res = yield from run.step("restore:feedback", "ZControllerOnOff", {"enable": True})
        if res.success:
            run.note_restored("z_feedback", True)
        else:
            run.warnings.append(_tr_lang(f"反馈没能重新闭合：{res.error}",
                                         f"Feedback could not be re-closed: {res.error}"))
    if bias_v is not None and "bias_v" in run.changes:
        res = yield from run.step("restore:bias", "BiasSettleChange",
                                  {"bias_v": float(bias_v), "settle_s": 0.0})
        if res.success:
            run.note_restored("bias_v", bias_v)
        else:
            run.warnings.append(_tr_lang(f"偏压没能回到 {bias_v} V：{res.error}",
                                         f"Bias could not be restored to {bias_v} V: {res.error}"))
    if setpoint_a is not None and "setpoint_a" in run.changes:
        res = yield from run.step("restore:setpoint", "SetSetpoint", {"setpoint_a": float(setpoint_a)})
        if res.success:
            run.note_restored("setpoint_a", setpoint_a)
        else:
            run.warnings.append(_tr_lang(f"设定点没能回到 {setpoint_a} A：{res.error}",
                                         f"Setpoint could not be restored to {setpoint_a} A: {res.error}"))


def _band_curve(rows: list[dict], xkey: str) -> dict:
    """{band: {"x": [...], "area": [...], "peak_x": [...]}}（只取 ok 的点）。"""
    out: dict = {}
    for r in rows:
        if r.get("status") != "ok":
            continue
        for k, v in r.items():
            if k.startswith("band_") and not k.endswith(("_snr", "_peak")):
                name = k[5:]
                c = out.setdefault(name, {"x": [], "area": [], "peak_x": []})
                c["x"].append(r.get(xkey))
                c["area"].append(v)
                c["peak_x"].append(r.get(f"band_{name}_peak"))
    return out


def _ref_drift(run: TersRun, ref0, out, frac: float, key: str) -> None:
    """参考谱相对 REF_0 的变化（谱带强度）；超过 ``frac`` ⇒ tip_changed（只标记）。"""
    if not hasattr(ref0, "band_value") or not hasattr(out, "band_value"):
        return
    a, b = ref0.band_value(), out.band_value()
    if a and b is not None:
        rel = b / a - 1.0
        run.analysis.setdefault("references", []).append({"key": key, "rel_to_ref0": rel})
        if abs(rel) > frac:
            run.qc["tip_changed"] += 1
            run.warnings.append(_tr_lang(f"tip_changed：{key} 相对 REF_0 变了 {rel:+.0%}（容限 {frac:.0%}）",
                                         f"tip_changed: {key} changed by {rel:+.0%} relative to REF_0 "
                                         f"(tolerance {frac:.0%})"))


def _peak_values(out) -> dict:
    return {f"band_{n}_peak": m.get("peak_x") for n, m in out.bands.items() if m.get("valid")}


# ═════════════════════════════════════════════════════════════════════════════
# M06 偏压序列
# ═════════════════════════════════════════════════════════════════════════════

class TERSBiasSeries(TersComposite):
    MODE_ID = "M06"
    MODE_KEY = "ters_bias"
    NEEDS = Needs(tip="tunneling")

    def metadata(self) -> SkillMetadata:
        mode = [
            ParameterSpec(name="biases_v", type="str", unit="V", required=False,
                          default="0.3,0.6,0.9,1.2,1.5,-0.3,-0.6,-0.9,-1.2,-1.5",
                          description=_tr_lang("偏压列表（逗号分隔，V）。正负都给才看得出电子态差别。" + _INF,
                                               "Bias list (comma-separated, V). Give both polarities to see "
                                               "differences between electronic states. " + _INF_EN)),
            ParameterSpec(name="bias_abs_max_v", type="float", unit="V", required=False, default=1.75,
                          min_value=0.1, max_value=4.0,
                          description=_tr_lang("|V| 上限。635–693 nm ↔ 1.79–1.95 eV，|V| < 1.79 V 时单电子过程无窗内发射"
                                               "；越过需 allow_over_cutoff 且 laser_off_pass。",
                                               "Upper limit of |V|. 635-693 nm corresponds to 1.79-1.95 eV; for "
                                               "|V| < 1.79 V a single-electron process has no in-window emission "
                                               "; exceeding it needs allow_over_cutoff and laser_off_pass.")),
            ParameterSpec(name="allow_over_cutoff", type="bool", required=False, default=False,
                          description=_tr_lang("允许 |V| > bias_abs_max_v；仅当 laser_off_pass = true 时有效。" + _INF,
                                               "Allow |V| > bias_abs_max_v; only effective when "
                                               "laser_off_pass = true. " + _INF_EN)),
            ParameterSpec(name="hold_mode", type="str", required=False, default="constant_resistance",
                          allowed_values=["constant_resistance", "constant_current", "constant_height"],
                          description=_tr_lang("保持量：constant_resistance = 设定点随 V 缩放使 R = V/I 不变；"
                                               "constant_current = 设定点固定；constant_height = 在 V0 处关反馈后只改偏压。",
                                               "Quantity held constant: constant_resistance = the setpoint scales "
                                               "with V so that R = V/I stays constant; constant_current = setpoint "
                                               "fixed; constant_height = open the feedback at V0, then change only "
                                               "the bias.")),
            ParameterSpec(name="junction_r_ohm", type="float", unit="Ω", required=False, default=None,
                          min_value=1e8, max_value=1e13,
                          description=_tr_lang("目标结阻；不给 = V0 / I0（当前）。",
                                               "Target junction resistance; if omitted = V0 / I0 (current).")),
            ParameterSpec(name="min_current_a", type="float", unit="A", required=False, default=1e-11,
                          min_value=1e-12, max_value=1e-9,
                          description=_tr_lang("设定点下限（低 |V| 时裁剪，裁剪会记录）。" + _INF,
                                               "Lower limit of the setpoint (clipped at low |V|; clipping is "
                                               "recorded). " + _INF_EN)),
            ParameterSpec(name="max_current_a", type="float", unit="A", required=False, default=2e-9,
                          min_value=1e-10, max_value=2e-8,
                          description=_tr_lang("设定点 / 实测电流上限（裁剪会记录）。" + _INF,
                                               "Upper limit of the setpoint / measured current (clipping is "
                                               "recorded). " + _INF_EN)),
            ParameterSpec(name="bias_settle_s", type="float", unit="s", required=False, default=10.0,
                          min_value=0.0, max_value=600.0,
                          description=_tr_lang("偏压变化后额外等待（Z 蠕变）。" + _INF,
                                               "Extra wait after a bias change (Z creep). " + _INF_EN)),
            ParameterSpec(name="zero_deadband_v", type="float", unit="V", required=False, default=0.05,
                          min_value=0.0, max_value=0.5,
                          description=_tr_lang("穿零死区半宽（BiasSettleChange 默认 50 mV）；反馈闭合时 |V| 小于它的点不走。",
                                               "Half-width of the zero-crossing deadband (BiasSettleChange default "
                                               "50 mV); with the feedback closed, points with |V| below it are "
                                               "skipped.")),
            ParameterSpec(name="order", type="str", required=False, default="as_given",
                          allowed_values=["as_given", "random"],
                          description=_tr_lang("偏压顺序；random 用 shuffle_seed 打乱，把漂移与偏压解耦。" + _INF,
                                               "Bias order; random shuffles with shuffle_seed, decoupling drift "
                                               "from bias. " + _INF_EN)),
            ParameterSpec(name="shuffle_seed", type="int", required=False, default=0, min_value=0,
                          max_value=2147483647,
                          description=_tr_lang("random 的种子（记录进元数据）。",
                                               "Seed for random (recorded in the metadata).")),
            ParameterSpec(name="ref_every_n", type="int", required=False, default=4, min_value=0,
                          max_value=100,
                          description=_tr_lang("每 n 点回参考偏压取一条参考谱；0 = 不取。" + _INF,
                                               "Return to the reference bias every n points to take a reference "
                                               "spectrum; 0 = none. " + _INF_EN)),
            ParameterSpec(name="ref_bias_v", type="float", unit="V", required=False, default=None,
                          min_value=-4.0, max_value=4.0,
                          description=_tr_lang("参考偏压；不给 = V0。" + _INF,
                                               "Reference bias; if omitted = V0. " + _INF_EN)),
            ParameterSpec(name="laser_off_pass", type="bool", required=False, default=False,
                          description=_tr_lang("主序列之后追加一遍「激光遮挡」的 STML 背景对照：要操作员确认已遮光"
                                               "（只在有人工确认通道的路径上能做）。" + _INF,
                                               "After the main sequence, add a pass with the laser blocked as an "
                                               "STML background control: the operator must confirm that the beam "
                                               "is blocked (only possible on paths with a human confirmation "
                                               "channel). " + _INF_EN)),
            ParameterSpec(name="stml_check_min_v", type="float", unit="V", required=False, default=1.5,
                          min_value=0.5, max_value=4.0,
                          description=_tr_lang("激光关闭对照只对 |V| ≥ 该值的点做。" + _INF,
                                               "The laser-off control is done only for points with |V| >= this "
                                               "value. " + _INF_EN)),
            ParameterSpec(name="bands_cm1", type="str", required=False, default="",
                          description=_tr_lang("分析用谱带 '名称:lo-hi,…'。",
                                               "Spectral bands for the analysis, 'name:lo-hi,...'.")),
            ParameterSpec(name="nf_drift_max_frac", type="float", required=False, default=0.3,
                          min_value=0.01, max_value=1.0,
                          description=_tr_lang("参考谱强度相对 REF_0 变化的容限（超过只标记 tip_changed）。" + _INF,
                                               "Tolerance of the reference-spectrum intensity change relative to "
                                               "REF_0 (beyond it only tip_changed is flagged). " + _INF_EN)),
        ]
        return ters_metadata(
            name="TERSBiasSeries",
            description=_tr_lang(
                "偏压序列（M06）：同一位置按偏压列表逐点取 TERS 谱；每个偏压走 BiasSettleChange（穿零保护），"
                "按 hold_mode 保持结阻 / 电流 / 高度之一不变，等 bias_settle_s 的 Z 蠕变后判稳再曝光，记录 z(V)；"
                "|V| 默认 ≤ 1.75 V（STML 截止），越过要 allow_over_cutoff + laser_off_pass；每 ref_every_n 点回参考偏压"
                "取参考谱。曝光期间守卫按该点预期电流判断，触发 ⇒ 立刻回 V0 并停。结束时偏压 / 设定点 / 反馈放回开始前的值；"
                "constant_height 关着反馈时被中止则退针（反馈闭合不回去），偏压 / 设定点不动、报「未还原」。"
                "LiveHub：local/ters_bias/<run_id>（x = 偏压 V；谱带强度、z、⟨I⟩）。",
                "Bias series (M06): at one position, acquire TERS spectra point by point over a list of "
                "biases; each bias goes through BiasSettleChange (zero-crossing protection), holding one of "
                "junction resistance / current / height constant according to hold_mode; after the Z creep "
                "wait of bias_settle_s the stability check runs and then the exposure, and z(V) is recorded; "
                "|V| <= 1.75 V by default (STML cutoff), exceeding it needs allow_over_cutoff + "
                "laser_off_pass; every ref_every_n points it returns to the reference bias to take a "
                "reference spectrum. During the exposure the guard judges against the expected current of "
                "the point; if triggered => return to V0 immediately and stop. At the end the bias / "
                "setpoint / feedback are put back to their values before the run; if aborted while "
                "constant_height has the feedback open, the tip is withdrawn (the feedback is not "
                "re-closed), and the bias / setpoint are left as they are and reported as 'not restored'. "
                "LiveHub: local/ters_bias/<run_id> (x = bias V; band intensities, z, <I>)."),
            parameters=[*mode, *cp_a(), *cp_b(overrides={"settle_s": 1.0, "stable_window_s": 1.0}),
                        *cp_c(), *common_extra()],
            tags=["optics", "ters", "spectrum", "acquisition", "bias", "series"],
            duration_s=600.0,
        )

    def prepare(self, run: TersRun) -> list[str]:
        p = run.p
        out = []
        try:
            vals = ta.parse_float_list(p["biases_v"], what="biases_v")
        except ValueError as exc:
            return [str(exc)]
        cap = float(p["bias_abs_max_v"])
        over = [v for v in vals if abs(v) > cap]
        if over and not (p["allow_over_cutoff"] and p["laser_off_pass"]):
            out.append(_tr_lang(f"{over} 超过 |V| ≤ {cap} V（STML 截止）：要越过请同时给 allow_over_cutoff=true 与 "
                                "laser_off_pass=true（激光关闭对照）",
                                f"Biases {over} exceed |V| <= {cap} V (STML cutoff): to go beyond it, give both "
                                "allow_over_cutoff=true and laser_off_pass=true (laser-off control)"))
        dead = float(p["zero_deadband_v"])
        skipped = [v for v in vals if abs(v) < dead and p["hold_mode"] != "constant_height"]
        if skipped:
            run.warnings.append(_tr_lang(f"{skipped} V 落在穿零死区（< {dead} V）且反馈闭合 —— 这些点不走",
                                         f"{skipped} V fall in the zero-crossing deadband (< {dead} V) with the "
                                         "feedback closed - these points are skipped"))
        vals = [v for v in vals if v not in skipped]
        if not vals:
            out.append(_tr_lang("没有可走的偏压点", "No bias points left to visit"))
        if p["order"] == "random":
            rng = np.random.default_rng(int(p["shuffle_seed"]))
            vals = [vals[i] for i in rng.permutation(len(vals))]
        self._biases = vals
        n_ref = len(vals) // int(p["ref_every_n"]) if int(p["ref_every_n"]) else 0
        stml = [v for v in vals if abs(v) >= float(p["stml_check_min_v"])] if p["laser_off_pass"] else []
        run.total_points = 1 + len(vals) + n_ref + len(stml)
        per = (2.0 + float(p["bias_settle_s"]) + float(p["settle_s"]) + float(p["stable_window_s"])
               + float(p["exposure_ms"]) / 1000.0 * int(p["frames"]) + 0.5)
        est = 20.0 + run.total_points * per
        run.plan.update({"biases_v": vals, "references": n_ref, "stml_points": stml,
                         "estimated_s": round(est, 1), "hold_mode": p["hold_mode"]})
        if est > float(p["max_total_s"]):
            out.append(_tr_lang(f"预计 {est:.0f} s 超过 max_total_s={float(p['max_total_s']):.0f} s",
                                f"Estimated {est:.0f} s exceeds max_total_s={float(p['max_total_s']):.0f} s"))
        return out

    def dry_plan(self, run: TersRun) -> None:
        v0 = run.facts.get("nanonis", {}).get("bias_v")
        i0 = run.i_set
        r0 = run.p.get("junction_r_ohm") or (abs(v0 / i0) if v0 and i0 else None)
        sps = [self._setpoint(run, v, r0, i0) for v in self._biases] if r0 else []
        run.plan["setpoints_a"] = [s for s, _ in sps] if r0 else None
        # a clipped setpoint is no longer constant-resistance: the plan is where the expert sees that
        run.plan["clipped_at_bias_v"] = [v for v, (_, c) in zip(self._biases, sps) if c]

    def _setpoint(self, run: TersRun, v: float, r0: float | None, i0: float) -> tuple[float, bool]:
        p = run.p
        if p["hold_mode"] == "constant_current" or r0 is None:
            return float(i0), False
        raw = abs(v) / r0
        lo, hi = float(p["min_current_a"]), float(p["max_current_a"])
        clipped = min(max(raw, lo), hi)
        return clipped, clipped != raw

    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        run.live.open_main("xy", title=_tr_lang("TERS 偏压序列", "TERS bias series"), x_label="Bias", x_unit="V",
                           y_label="intensity", y_unit="counts")
        v0 = float(run.facts["nanonis"]["bias_v"])
        i0 = float(run.i_set)
        r0 = float(p["junction_r_ohm"]) if p.get("junction_r_ohm") else abs(v0 / i0)
        ref_v = float(p["ref_bias_v"]) if p.get("ref_bias_v") is not None else v0
        mode = p["hold_mode"]
        frac = float(p["exposure_guard_current_frac"])
        run.analysis["junction_r_ohm"] = r0
        # REF_0 is taken at (V0, I0) with the feedback still closed — constant_height opens
        # the loop only after it
        ref0 = yield from self._ref(run, "ref0", ref_v, i0, frac, fb_on=True)
        if ref0 is None:
            return
        if mode == "constant_height":
            res = yield from run.step("hold:fb_off", "ZControllerOnOff", {"enable": False})
            if not res.success:
                run.stop("needs_operator", _tr_lang(f"constant_height：关反馈失败：{res.error}",
                                                    f"constant_height: opening the feedback failed: {res.error}"))
                return
            run.note_change("z_feedback", original=True, current=False, how="constant_height")
        n_since_ref = 0
        for i, v in enumerate(self._biases):
            if run.check():
                break
            out = yield from self._point(run, f"v{i}", i, v, r0, i0, frac, mode, laser="as_attested")
            if out is None:
                break
            n_since_ref += 1
            if int(p["ref_every_n"]) and n_since_ref >= int(p["ref_every_n"]) and i + 1 < len(self._biases):
                n_since_ref = 0
                if mode == "constant_height":
                    run.warnings.append(_tr_lang("constant_height：参考谱在关反馈状态下取（同一高度）",
                                                 "constant_height: the reference spectrum is taken with the "
                                                 "feedback open (same height)"))
                ref = yield from self._ref(run, f"ref{i + 1}", ref_v, i0, frac,
                                           fb_on=mode != "constant_height")
                if ref is None:
                    break
                _ref_drift(run, ref0, ref, float(p["nf_drift_max_frac"]), f"ref{i + 1}")
        if p["laser_off_pass"] and not run.stopped:
            yield from self._laser_off_pass(run, r0, i0, frac, mode)
        yield from _restore_conditions(run, bias_v=v0, setpoint_a=i0,
                                       feedback=True if mode == "constant_height" else None)
        run.analysis["curves"] = _band_curve(run.points, "bias_set_v")
        run.analysis["z_vs_bias"] = [(r.get("bias_set_v"), r.get("z_before_m"))
                                     for r in run.points if r.get("status") == "ok"]

    def _ref(self, run: TersRun, key: str, ref_v: float, i0: float, frac: float, *,
             fb_on: bool) -> Iterator[CompositeStep]:
        res = yield from self._set_bias(run, key, ref_v)
        if not res:
            return None
        if fb_on:
            ok = yield from self._set_sp(run, key, i0)
            if not ok:
                return None
        tr = run.ready_or_policy(label=key, key=key, i_target=i0 if fb_on else self._measured(run),
                                 feedback=fb_on)
        if not tr.passed:
            if not run.stopped:
                run.stop("needs_operator", _tr_lang(f"{key}: 参考偏压下判稳失败（{tr.reason}）",
                                                    f"{key}: stability check failed at the reference bias "
                                                    f"({tr.reason})"))
            return None
        out = yield from run.acquire(key, key, guard=GuardSpec(
            i_ref_a=i0, frac=frac, expect_feedback=fb_on),
            extra={"reference": True, "bias_set_v": ref_v, "tip_ready": tr.as_meta()})
        run.record(out, key=key, tip=tr, values={"reference": True, "bias_set_v": ref_v,
                                                 **_peak_values(out)})
        return out if run.handle(out) else None

    @staticmethod
    def _measured(run: TersRun) -> float | None:
        vals = [run.read_float("Current_Get") for _ in range(10)]
        vals = [abs(v) for v in vals if v is not None]
        return float(np.mean(vals)) if vals else None

    def _set_bias(self, run: TersRun, key: str, v: float) -> Iterator[CompositeStep]:
        before = run.read_float("Bias_Get")
        res = yield from run.step(f"{key}:bias", "BiasSettleChange",
                                  {"bias_v": float(v), "settle_s": 0.0,
                                   "allow_stop_in_deadband": run.p["hold_mode"] == "constant_height"})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"{key}: BiasSettleChange({v}) 失败：{res.error}",
                                                f"{key}: BiasSettleChange({v}) failed: {res.error}"))
            return False
        run.note_change("bias_v", original=run.changes.get("bias_v", {}).get("original", before),
                        current=v)
        if run.sleep(float(run.p["bias_settle_s"])):
            run.stop("partial", "aborted by operator")
            return False
        return True

    def _set_sp(self, run: TersRun, key: str, i: float) -> Iterator[CompositeStep]:
        before = run.read_float("ZCtrl_SetpntGet")
        if before is not None and abs(before - i) <= 1e-3 * abs(i):
            return True
        res = yield from run.step(f"{key}:setpoint", "SetSetpoint", {"setpoint_a": float(i)})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"{key}: SetSetpoint({i:.3g}) 失败：{res.error}",
                                                f"{key}: SetSetpoint({i:.3g}) failed: {res.error}"))
            return False
        run.note_change("setpoint_a", original=run.changes.get("setpoint_a", {}).get("original", before),
                        current=i)
        return True

    def _point(self, run: TersRun, key: str, i: int, v: float, r0: float, i0: float, frac: float,
               mode: str, *, laser: str) -> Iterator[CompositeStep]:
        p = run.p
        ok = yield from self._set_bias(run, key, v)
        if not ok:
            return None
        i_k, clipped = self._setpoint(run, v, r0, i0)
        if mode == "constant_resistance":
            ok = yield from self._set_sp(run, key, i_k)
            if not ok:
                return None
        i_exp = abs(v) / r0 if mode == "constant_height" else i_k
        if clipped:
            run.warnings.append(_tr_lang(f"{key}: 设定点 |V|/R0 被裁剪到 {i_k:.3g} A",
                                         f"{key}: setpoint |V|/R0 clipped to {i_k:.3g} A"))
        # constant_height: with the loop open there is no setpoint to sit at — the stability
        # window judges noise and drift around whatever the junction gives at this bias
        tr = run.ready_or_policy(label=key, key=key,
                                 i_target=i_exp if mode != "constant_height" else self._measured(run),
                                 feedback=mode != "constant_height")
        if not tr.passed:
            run.record(None, key=key, status="unstable_tip", tip=tr,
                       values={"bias_set_v": v, "setpoint_a": i_k})
            return None if run.stopped else "skipped"
        out = yield from run.acquire(key, key, guard=GuardSpec(
            i_ref_a=i_exp, frac=frac, expect_feedback=mode != "constant_height"),
            extra={"series_index": i, "bias_set_v": v, "setpoint_a": i_k,
                   "setpoint_clipped": clipped, "hold_mode": mode,
                   "junction_r_ohm": r0, "tip_ready": tr.as_meta(), "laser_pass": laser})
        rb = out.stm_before.get("bias_v")
        run.record(out, key=key, tip=tr, values={"series_index": i, "bias_set_v": v,
                                                 "bias_readback_v": rb, "setpoint_a": i_k,
                                                 "hold_mode": mode, "laser_pass": laser,
                                                 **_peak_values(out)})
        if out.status == "guard_abort" and not run._aborted():
            # M06.6：越限 ⇒ 立刻回 V0
            yield from run.step(f"{key}:back_to_v0", "BiasSettleChange",
                                {"bias_v": float(run.facts["nanonis"]["bias_v"]), "settle_s": 0.0})
        if not run.handle(out):
            return None
        if out.ok and isinstance(rb, (int, float)) and abs(float(rb) - v) > 0.02:
            run.stop("needs_operator", _tr_lang(f"{key}: 偏压读回 {rb:.4g} V 与设置 {v} V 差 > 20 mV —— 中止该点并告警",
                                                f"{key}: bias read back {rb:.4g} V differs from the set {v} V by "
                                                "> 20 mV - aborting this point and raising a warning"))
            return None
        if out.ok:
            run.live.publish({"x": float(v), "values": {
                **{n: m["area"] for n, m in out.bands.items() if m.get("valid")},
                **({"total": out.intensity} if not out.bands and out.intensity is not None else {}),
                **({"z": float(out.stm_before["z_pos_m"])}
                   if isinstance(out.stm_before.get("z_pos_m"), (int, float)) else {}),
                **({"I_mean": out.window["current_mean_a"]}
                   if out.window.get("current_mean_a") is not None else {})}})
        return out

    def _laser_off_pass(self, run: TersRun, r0: float, i0: float, frac: float,
                        mode: str) -> Iterator[CompositeStep]:
        p = run.p
        pts = [v for v in self._biases if abs(v) >= float(p["stml_check_min_v"])]
        if not pts:
            return
        decision = _tc.human_channel({
            "kind": "workflow_human", "workflow": run.name, "node_id": "laser_off_pass",
            "decision_key": f"{run.run_id}:laser_off_pass",
            "message": _tr_lang(f"激光关闭对照：请遮挡激光（MAST 关不了激光），确认后对 {pts} V 重做一遍（STML 背景）。",
                                "Laser-off control: please block the laser (MAST cannot switch the laser off); "
                                f"after you confirm, the points at {pts} V are repeated (STML background)."),
            "inputs": {"biases_v": pts}, "routes": ["laser_blocked", "skip"]})
        route = decision.get("route") if isinstance(decision, dict) else decision
        if route != "laser_blocked":
            why = (_tr_lang("这条执行路径没有人工确认通道", "this execution path has no human confirmation channel")
                   if decision is None else _tr_lang(f"操作员选择 {route!r}", f"the operator chose {route!r}"))
            run.warnings.append(_tr_lang(f"激光关闭对照没做（{why}）", f"The laser-off control was not done ({why})"))
            run.analysis["laser_off_pass"] = {"done": False, "why": why}
            if [v for v in self._biases if abs(v) > float(p["bias_abs_max_v"])]:
                run.qc["stml_uncontrolled"] += 1
            return
        saved = dict(run.attestation)
        run.attestation = {**saved, "laser_state": "attested_off", "attested_via": "hitl",
                           "pass": "laser_off"}
        for j, v in enumerate(pts):
            if run.check():
                break
            out = yield from self._point(run, f"off{j}", j, v, r0, i0, frac, mode, laser="laser_off")
            if out is None:
                break
        run.attestation = saved
        run.analysis["laser_off_pass"] = {"done": True, "biases_v": pts}


# ═════════════════════════════════════════════════════════════════════════════
# M07 设定点序列
# ═════════════════════════════════════════════════════════════════════════════

class TERSSetpointSeries(TersComposite):
    MODE_ID = "M07"
    MODE_KEY = "ters_setpoint"
    NEEDS = Needs(tip="tunneling")

    def metadata(self) -> SkillMetadata:
        mode = [
            ParameterSpec(name="setpoints_a", type="str", unit="A", required=False,
                          default="2e-11,5e-11,1e-10,2e-10,5e-10,1e-9",
                          description=_tr_lang("设定点电流列表（逗号分隔，A）。" + _INF,
                                               "Setpoint current list (comma-separated, A). " + _INF_EN)),
            ParameterSpec(name="bias_v", type="float", unit="V", required=False, default=None,
                          min_value=-3.0, max_value=3.0,
                          description=_tr_lang("固定偏压；不给 = 当前。" + _INF,
                                               "Fixed bias; if omitted = current. " + _INF_EN)),
            ParameterSpec(name="max_setpoint_a", type="float", unit="A", required=False, default=2e-9,
                          min_value=1e-11, max_value=1e-8,
                          description=_tr_lang("设定点上限（越过即拒绝，不裁剪）。" + _INF,
                                               "Upper limit of the setpoint (refused when exceeded, not "
                                               "clipped). " + _INF_EN)),
            ParameterSpec(name="max_step_ratio", type="float", required=False, default=3.0,
                          min_value=1.2, max_value=10.0,
                          description=_tr_lang("相邻设定点比值上限；超过自动插入几何中间点（含从当前设定点出发的第一步）。" + _INF,
                                               "Upper limit of the ratio between adjacent setpoints; beyond it "
                                               "geometric intermediate points are inserted automatically "
                                               "(including the first step from the current setpoint). " + _INF_EN)),
            ParameterSpec(name="order", type="str", required=False, default="up_down",
                          allowed_values=["ascending", "descending", "up_down", "random"],
                          description=_tr_lang("up_down = 先升后降，检验滞后。" + _INF,
                                               "up_down = up first, then down, to test hysteresis. " + _INF_EN)),
            ParameterSpec(name="ref_every_n", type="int", required=False, default=0, min_value=0,
                          max_value=100,
                          description=_tr_lang("每 n 点回 I_ref 取参考谱；up_down 已含首尾参考。" + _INF,
                                               "Return to I_ref every n points to take a reference spectrum; "
                                               "up_down already includes the first and last references. " + _INF_EN)),
            ParameterSpec(name="nf_drift_max_frac", type="float", required=False, default=0.3,
                          min_value=0.01, max_value=1.0,
                          description=_tr_lang("参考谱强度相对 REF_0 的变化容限，越过标 tip_changed（规格未给，"
                                               "取 M04/M05 的 0.3）。" + _INF,
                                               "Tolerance of the reference-spectrum intensity change relative to "
                                               "REF_0, beyond it tip_changed is flagged (not given in the spec, "
                                               "taken as 0.3 from M04/M05). " + _INF_EN)),
            ParameterSpec(name="fit_kappa", type="bool", required=False, default=True,
                          description=_tr_lang("运行结束后拟合 κ_app 并把电流轴换算成相对间隙。",
                                               "After the run, fit κ_app and convert the current axis to the "
                                               "relative gap.")),
            ParameterSpec(name="bands_cm1", type="str", required=False, default="",
                          description=_tr_lang("分析用谱带 '名称:lo-hi,…'。",
                                               "Spectral bands for the analysis, 'name:lo-hi,...'.")),
            ParameterSpec(name="shuffle_seed", type="int", required=False, default=0, min_value=0,
                          max_value=2147483647,
                          description=_tr_lang("order=random 的种子。", "Seed for order=random.")),
        ]
        return ters_metadata(
            name="TERSSetpointSeries",
            description=_tr_lang(
                "设定点序列（M07）：固定偏压、反馈保持闭合，按设定点电流序列改变针尖高度，每个电流判稳后取一条谱并记 z；"
                "设定点不超过 max_setpoint_a，相邻比值超过 max_step_ratio 自动插几何中间点；曝光期间守卫按该点设定点判断，"
                "触发 ⇒ 立刻回 I_ref 并停。结束时回 I_ref 取 REF_1，拟合 κ_app（ln I 对 z），换算相对间隙。"
                "LiveHub：local/ters_setpoint/<run_id>（x = 设定点电流 A；谱带强度、z）。",
                "Setpoint series (M07): fixed bias, feedback kept closed; the tip height is changed by "
                "stepping through a series of setpoint currents, and after each current passes the stability "
                "check a spectrum is taken and z is recorded; the setpoint does not exceed max_setpoint_a, "
                "and where the ratio of adjacent setpoints exceeds max_step_ratio geometric intermediate "
                "points are inserted automatically; during the exposure the guard judges against the "
                "setpoint of that point, and if triggered => return to I_ref immediately and stop. At the "
                "end it returns to I_ref to take REF_1, fits κ_app (ln I versus z) and converts to the "
                "relative gap. LiveHub: local/ters_setpoint/<run_id> (x = setpoint current A; band "
                "intensities, z)."),
            parameters=[*mode, *cp_a(), *cp_b(overrides={"settle_s": 3.0}), *cp_c(), *common_extra()],
            tags=["optics", "ters", "spectrum", "acquisition", "setpoint", "series"],
            duration_s=420.0,
        )

    def prepare(self, run: TersRun) -> list[str]:
        p = run.p
        try:
            vals = ta.parse_float_list(p["setpoints_a"], what="setpoints_a")
        except ValueError as exc:
            return [str(exc)]
        cap = float(p["max_setpoint_a"])
        bad = [v for v in vals if not (1e-12 <= v <= cap)]
        if bad:
            return [_tr_lang(f"{bad} A 不在 [1e-12, max_setpoint_a={cap:g}] A 内（越界拒绝，不裁剪）",
                             f"{bad} A is outside [1e-12, max_setpoint_a={cap:g}] A (out-of-range values are "
                             "refused, not clipped)")]
        order = p["order"]
        asc = sorted(vals)
        if order == "ascending":
            seq = asc
        elif order == "descending":
            seq = asc[::-1]
        elif order == "up_down":
            seq = asc + asc[-2::-1]
        else:
            rng = np.random.default_rng(int(p["shuffle_seed"]))
            seq = [vals[i] for i in rng.permutation(len(vals))]
        self._seq = seq
        run.total_points = len(seq) + 2
        per = (float(p["settle_s"]) + float(p["stable_window_s"])
               + float(p["exposure_ms"]) / 1000.0 * int(p["frames"]) + 1.0)
        est = 20.0 + run.total_points * per * 1.3
        run.plan.update({"setpoints_a": seq, "estimated_s": round(est, 1)})
        if est > float(p["max_total_s"]):
            return [_tr_lang(f"预计 {est:.0f} s 超过 max_total_s={float(p['max_total_s']):.0f} s",
                             f"Estimated {est:.0f} s exceeds max_total_s={float(p['max_total_s']):.0f} s")]
        return []

    def _with_steps(self, start: float, seq: list[float], ratio: float) -> list[tuple[float, bool]]:
        """在比值超过 ``ratio`` 的相邻两点之间插几何中间点。返回 ``(I, 是否插入点)``。"""
        out: list[tuple[float, bool]] = []
        prev = start
        for target in seq:
            r = target / prev
            n = int(math.ceil(abs(math.log(r)) / math.log(ratio))) if r > 0 else 1
            for k in range(1, n):
                out.append((prev * r ** (k / n), True))
            out.append((target, False))
            prev = target
        return out

    def dry_plan(self, run: TersRun) -> None:
        run.plan["with_intermediates"] = self._with_steps(float(run.i_set), self._seq,
                                                          float(run.p["max_step_ratio"]))

    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        run.live.open_main("xy", title=_tr_lang("TERS 设定点序列", "TERS setpoint series"),
                           x_label="Setpoint", x_unit="A",
                           y_label="intensity", y_unit="counts")
        i_ref = float(run.i_set)
        v0 = float(run.facts["nanonis"]["bias_v"])
        frac = float(p["exposure_guard_current_frac"])
        if p.get("bias_v") is not None and abs(float(p["bias_v"]) - v0) > 1e-4:
            res = yield from run.step("bias", "BiasSettleChange", {"bias_v": float(p["bias_v"]),
                                                                    "settle_s": 0.0})
            if not res.success:
                run.stop("needs_operator", _tr_lang(f"BiasSettleChange 失败：{res.error}",
                                                    f"BiasSettleChange failed: {res.error}"))
                return
            run.note_change("bias_v", original=v0, current=float(p["bias_v"]))
        ref0 = yield from self._acq(run, "ref0", i_ref, i_ref, frac, reference=True)
        if ref0 is None:
            return
        plan = self._with_steps(i_ref, self._seq, float(p["max_step_ratio"]))
        run.plan["with_intermediates"] = plan
        peak_seen = 0.0
        n_since = 0
        idx = 0
        for i_k, inserted in plan:
            if run.check():
                break
            ok = yield from self._set(run, f"sp{idx}", i_k, i_ref)
            if not ok:
                break
            if inserted:
                continue
            direction = "up" if i_k >= peak_seen else "down"
            peak_seen = max(peak_seen, i_k)
            out = yield from self._acq(run, f"sp{idx}", i_k, i_ref, frac, direction=direction)
            idx += 1
            if out is None:
                break
            n_since += 1
            if int(p["ref_every_n"]) and n_since >= int(p["ref_every_n"]):
                n_since = 0
                if (yield from self._set(run, f"ref{idx}", i_ref, i_ref)):
                    ref = yield from self._acq(run, f"ref{idx}", i_ref, i_ref, frac, reference=True)
                    if ref is None:
                        break
                    _ref_drift(run, ref0, ref, float(p["nf_drift_max_frac"]), f"ref{idx}")
        if not run.stopped:
            for i_k, _ in self._with_steps(peak_seen or i_ref, [i_ref], float(p["max_step_ratio"])):
                if not (yield from self._set(run, "back", i_k, i_ref)):
                    break
            ref1 = yield from self._acq(run, "ref_end", i_ref, i_ref, frac, reference=True)
            if ref1 is not None:
                _ref_drift(run, ref0, ref1, float(p["nf_drift_max_frac"]), "ref_end")
        yield from _restore_conditions(run, bias_v=v0, setpoint_a=i_ref)
        self._analyse(run, i_ref)

    def _set(self, run: TersRun, key: str, i: float, i_ref: float) -> Iterator[CompositeStep]:
        res = yield from run.step(f"{key}:setpoint", "SetSetpoint", {"setpoint_a": float(i)})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"{key}: SetSetpoint({i:.3g}) 失败：{res.error}",
                                                f"{key}: SetSetpoint({i:.3g}) failed: {res.error}"))
            return False
        run.note_change("setpoint_a", original=run.changes.get("setpoint_a", {}).get("original", i_ref),
                        current=i)
        return True

    def _acq(self, run: TersRun, key: str, i_k: float, i_ref: float, frac: float, *,
             reference: bool = False, direction: str = "") -> Iterator[CompositeStep]:
        tr = run.ready_or_policy(label=key, key=key, i_target=i_k, feedback=True)
        if not tr.passed:
            run.record(None, key=key, status="unstable_tip", tip=tr, values={"setpoint_a": i_k})
            if run.stopped:
                return None
            return False
        out = yield from run.acquire(key, key, guard=GuardSpec(i_ref_a=i_k, frac=frac,
                                                               expect_feedback=True),
                                     extra={"setpoint_a": i_k, "reference": reference,
                                            "direction": direction, "tip_ready": tr.as_meta()})
        z = out.stm_before.get("z_pos_m")
        run.record(out, key=key, tip=tr, values={"setpoint_a": i_k, "z_m": z, "direction": direction,
                                                 "reference": reference, **_peak_values(out)})
        if out.status == "guard_abort" and not run._aborted():
            yield from run.step(f"{key}:back_to_ref", "SetSetpoint", {"setpoint_a": float(i_ref)})
        if not run.handle(out):
            return None
        if out.ok and not reference:
            vals = {n: m["area"] for n, m in out.bands.items() if m.get("valid")}
            if not vals and out.intensity is not None:
                vals["total"] = out.intensity
            if isinstance(z, (int, float)):
                vals["z"] = float(z)
            run.live.publish({"x": float(i_k), "values": vals})
        return out

    def _analyse(self, run: TersRun, i_ref: float) -> None:
        ok = [r for r in run.points if r.get("status") == "ok" and r.get("z_m") is not None]
        res: dict = {"curves": _band_curve(run.points, "setpoint_a")}
        if run.p["fit_kappa"] and len(ok) >= 2:
            fit = ta.fit_kappa_per_nm([r["setpoint_a"] for r in ok], [r["z_m"] for r in ok])
            res["kappa_fit"] = fit
            if fit.get("valid") and fit["kappa_per_nm"] > 0:
                k = fit["kappa_per_nm"]
                res["relative_gap_nm"] = {r["key"]: -math.log(r["setpoint_a"] / i_ref) / (2 * k)
                                          for r in ok}
        run.analysis.update(res)


# ═════════════════════════════════════════════════════════════════════════════
# M08 偏振序列
# ═════════════════════════════════════════════════════════════════════════════

class TERSPolarizationSeries(TersComposite):
    MODE_ID = "M08"
    MODE_KEY = "ters_pol"
    NEEDS = Needs(tip="tunneling")
    #: 单向趋近时先退到目标之前多少度再走过来（大于 PRM1-Z8 的 ±0.3° 间隙与 Kinesis 的 1° 补偿）
    APPROACH_DEG = 3.0

    def metadata(self) -> SkillMetadata:
        mode = [
            ParameterSpec(name="pol_device_id", type="str", required=False, default="",
                          description=_tr_lang("偏振旋转台的设备 id（ListOpticalDevices；规格里叫 device_id，与光谱仪的 "
                                               "device_id 同名，所以改名）；留空 = 唯一一台 role=polarizer 的。",
                                               "Device id of the polarization rotation stage (ListOpticalDevices; "
                                               "called device_id in the spec, renamed because it has the same "
                                               "name as the spectrometer's device_id); empty = the only device "
                                               "with role=polarizer.")),
            ParameterSpec(name="element", type="str", required=False, default="unknown",
                          allowed_values=["half_wave_plate", "polarizer_excitation",
                                          "analyzer_collection", "unknown"],
                          description=_tr_lang("PRM1-Z8 上的元件类型（只用于元数据与拟合：半波片转 θ ⇒ 偏振转 2θ）。",
                                               "Type of the optical element on the PRM1-Z8 ("
                                               "used only for metadata and fitting: a half-wave plate turned by "
                                               "θ turns the polarization by 2θ).")),
            ParameterSpec(name="angle_start_deg", type="float", unit="deg", required=False, default=0.0,
                          min_value=-360.0, max_value=720.0,
                          description=_tr_lang("起始角（仪器坐标）。", "Start angle (instrument coordinates).")),
            ParameterSpec(name="angle_stop_deg", type="float", unit="deg", required=False, default=180.0,
                          min_value=-360.0, max_value=720.0,
                          description=_tr_lang("终止角；偏振片 / 检偏器 180° 即一周期，半波片 90°。" + _INF,
                                               "Stop angle; a polarizer / analyzer has a period of 180°, a "
                                               "half-wave plate 90°. " + _INF_EN)),
            ParameterSpec(name="angle_step_deg", type="float", unit="deg", required=False, default=15.0,
                          min_value=1.0, max_value=90.0,
                          description=_tr_lang("角度步长（10° 约 2 s）。", "Angle step (about 2 s per 10°).")),
            ParameterSpec(name="direction", type="str", required=False, default="increasing",
                          allowed_values=["increasing", "decreasing"],
                          description=_tr_lang("单向趋近方向（反向间隙 ±0.3°）。",
                                               "Direction of the unidirectional approach (backlash ±0.3°).")),
            ParameterSpec(name="settle_after_move_s", type="float", unit="s", required=False,
                          default=0.5, min_value=0.2, max_value=30.0,
                          description=_tr_lang("到位后额外等待。", "Extra wait after arriving at the angle.")),
            ParameterSpec(name="ensure_homed", type="bool", required=False, default=True,
                          description=_tr_lang("未回零时请求回零（转整圈，最长 36 s；需操作员确认，回零期间不曝光）。",
                                               "Request homing when the stage is not homed (a full turn, up to "
                                               "36 s; needs operator confirmation, no exposure while homing).")),
            ParameterSpec(name="angle_offset_deg", type="float", unit="deg", required=False, default=0.0,
                          min_value=-360.0, max_value=360.0,
                          description=_tr_lang("仪器 0° 到实验室 0°（如 p 偏振）的偏移（只用于拟合与元数据）。" + _INF,
                                               "Offset from the instrument 0° to the lab 0° (e.g. p polarization) "
                                               "(used only for fitting and metadata). " + _INF_EN)),
            ParameterSpec(name="return_check", type="bool", required=False, default=True,
                          description=_tr_lang("序列末尾回到起始角再测一条（光束偏折 / 针尖变化的唯一在线检验）。",
                                               "Return to the start angle at the end of the sequence and measure "
                                               "once more (the only online check for beam deflection / tip "
                                               "change).")),
            ParameterSpec(name="ref_every_n", type="int", required=False, default=0, min_value=0,
                          max_value=100,
                          description=_tr_lang("每 n 个角度回起始角取参考谱；0 = 不取。" + _INF,
                                               "Return to the start angle every n angles to take a reference "
                                               "spectrum; 0 = none. " + _INF_EN)),
            ParameterSpec(name="fit_model", type="str", required=False, default="cos2_offset",
                          allowed_values=["none", "cos2", "cos2_offset"],
                          description=_tr_lang("拟合模型。", "Fit model.")),
            ParameterSpec(name="normalise", type="str", required=False, default="none",
                          allowed_values=["none", "malus", "ff_reference"],
                          description=_tr_lang("功率归一化：malus = 除以拟合的 cos²（消光附近不可靠）；ff_reference = 每个角度"
                                               "另取远场参考（需撤针，尚未实现，选它会被拒绝）。" + _INF,
                                               "Power normalisation: malus = divide by the fitted cos² (unreliable "
                                               "near extinction); ff_reference = take a far-field reference at "
                                               "every angle (needs the tip withdrawn, not implemented yet, "
                                               "selecting it is refused). " + _INF_EN)),
            ParameterSpec(name="bands_cm1", type="str", required=False, default="",
                          description=_tr_lang("分析用谱带 '名称:lo-hi,…'。",
                                               "Spectral bands for the analysis, 'name:lo-hi,...'.")),
        ]
        return ters_metadata(
            name="TERSPolarizationSeries",
            description=_tr_lang(
                "偏振序列（M08）：STM 保持不动，单向趋近地把偏振旋转台转到一组角度（每次先退到目标前 3° 再走过来，"
                "吃掉 ±0.3° 的反向间隙），每个角度到位后再等 settle_after_move_s、重新判稳、守卫式曝光，记录角度回读；"
                "末尾回起点角再测一条；I(θ) 拟合 A·cos²(θ − θ0) + B（半波片按偏振 2θ 拟合）。偏振台未回零时要操作员"
                "确认后才回零。LiveHub：local/ters_pol/<run_id>（x = 角度°；各谱带强度）。",
                "Polarization series (M08): the STM stays still while the polarization rotation stage is "
                "turned to a set of angles by unidirectional approach (each time it first backs off 3° "
                "before the target and then moves in, absorbing the ±0.3° backlash); at each angle it waits "
                "another settle_after_move_s, repeats the stability check, exposes under the guard and "
                "records the angle read-back; at the end it returns to the start angle and measures once "
                "more; I(θ) is fitted with A·cos²(θ − θ0) + B (a half-wave plate is fitted against the "
                "polarization 2θ). If the stage is not homed, it is homed only after the operator confirms. "
                "LiveHub: local/ters_pol/<run_id> (x = angle in degrees; band intensities)."),
            parameters=[*mode, *cp_a(), *cp_b(), *cp_c(), *common_extra()],
            tags=["optics", "ters", "spectrum", "acquisition", "polarization", "series"],
            duration_s=240.0,
        )

    def _angles(self, run: TersRun) -> list[float]:
        p = run.p
        a, b, s = float(p["angle_start_deg"]), float(p["angle_stop_deg"]), float(p["angle_step_deg"])
        lo, hi = min(a, b), max(a, b)
        n = int(math.floor((hi - lo) / s + 1e-9)) + 1
        vals = [lo + k * s for k in range(n)]
        return vals if p["direction"] == "increasing" else vals[::-1]

    def prepare(self, run: TersRun) -> list[str]:
        p = run.p
        if p["normalise"] == "ff_reference":
            return [_tr_lang("normalise=ff_reference（每个角度撤针取远场参考）尚未实现 —— 用 none 或 malus",
                             "normalise=ff_reference (withdraw the tip and take a far-field reference at every "
                             "angle) is not implemented yet - use none or malus")]
        angles = self._angles(run)
        self._angles_list = angles
        n_ref = len(angles) // int(p["ref_every_n"]) if int(p["ref_every_n"]) else 0
        run.total_points = len(angles) + n_ref + int(bool(p["return_check"]))
        per = (3.0 + float(p["settle_after_move_s"]) + float(p["settle_s"]) + float(p["stable_window_s"])
               + float(p["exposure_ms"]) / 1000.0 * int(p["frames"]) + 0.5)
        est = 20.0 + run.total_points * per
        run.plan.update({"angles_deg": angles, "estimated_s": round(est, 1)})
        if est > float(p["max_total_s"]):
            return [_tr_lang(f"预计 {est:.0f} s 超过 max_total_s={float(p['max_total_s']):.0f} s",
                             f"Estimated {est:.0f} s exceeds max_total_s={float(p['max_total_s']):.0f} s")]
        return []

    def preflight_extra(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        res = yield from run.step("preflight:polarizer", "GetPolarizerAngle",
                                  {"device_id": str(p.get("pol_device_id") or "")})
        if not res.success:
            return [_tr_lang(f"偏振台读不到：{res.error}", f"Cannot read the polarization stage: {res.error}")]
        d = res.data or {}
        self._pol_dev = d.get("device_id")
        run.facts["polarizer"] = d
        out = []
        if d.get("moving"):
            out.append(_tr_lang("偏振台正在转", "The polarization stage is moving"))
        lim = d.get("limits_deg") or [None, None]
        appr = self.APPROACH_DEG * (1 if p["direction"] == "increasing" else -1)
        targets = self._angles_list + [a - appr for a in self._angles_list[:1]]
        if lim[0] is not None and lim[1] is not None:
            bad = [a for a in targets if not (float(lim[0]) <= a <= float(lim[1]))]
            if bad:
                out.append(_tr_lang(f"角度 {bad} 超出偏振台软限位 {lim}",
                                    f"Angles {bad} are outside the soft limits {lim} of the polarization stage"))
        self._needs_home = d.get("homed") is False
        if self._needs_home and not p["ensure_homed"]:
            out.append(_tr_lang("偏振台未回零（homed=false），ensure_homed=false —— 角度没有意义，不开始",
                                "The polarization stage is not homed (homed=false) and ensure_homed=false - "
                                "the angles are meaningless, not starting"))
        return out

    def attestation_lines(self, run: TersRun) -> list[str]:
        lines = [_tr_lang(f"偏振元件 element={run.p['element']}",
                          f"Polarization element: element={run.p['element']}")]
        if getattr(self, "_needs_home", False):
            lines.append(_tr_lang("偏振台上电后未回零：确认后会先回零（转整圈，≤ 36 s，期间不曝光）",
                                  "The polarization stage has not been homed since power-up: after confirmation "
                                  "it is homed first (a full turn, <= 36 s, no exposure meanwhile)"))
        return lines

    def satellite_devices(self, run: TersRun) -> list[str]:
        return []

    def dry_plan(self, run: TersRun) -> None:
        run.plan["needs_home"] = getattr(self, "_needs_home", False)

    def _move(self, run: TersRun, key: str, angle: float) -> Iterator[CompositeStep]:
        """单向趋近：先到目标之前 APPROACH_DEG，再到目标；回读与目标差 > 1° ⇒ 重试一次，仍不行 ⇒ 停。"""
        p = run.p
        appr = self.APPROACH_DEG * (1 if p["direction"] == "increasing" else -1)
        dev = str(self._pol_dev or "")
        for attempt in range(2):
            for k, target in enumerate((angle - appr, angle)):
                res = yield from run.step(f"{key}:pol{attempt}{k}", "SetPolarizerAngle",
                                          {"device_id": dev, "angle_deg": float(target),
                                           "timeout_s": 60.0})
                if not res.success:
                    run.stop("needs_operator", _tr_lang(f"{key}: 偏振台转到 {target}° 失败：{res.error}",
                                                        f"{key}: turning the polarization stage to {target}° "
                                                        f"failed: {res.error}"))
                    return None
            rb = (res.data or {}).get("angle_deg")
            if rb is not None and abs(float(rb) - angle) <= 1.0:
                if run.sleep(float(p["settle_after_move_s"])):
                    run.stop("partial", "aborted by operator")
                    return None
                return float(rb)
            run.warnings.append(_tr_lang(f"{key}: 偏振台回读 {rb!r}° 与目标 {angle}° 差 > 1°（第 {attempt + 1} 次）",
                                         f"{key}: polarization stage read-back {rb!r}° differs from the target "
                                         f"{angle}° by > 1° (attempt {attempt + 1})"))
        run.stop("needs_operator", _tr_lang(f"{key}: 偏振台两次都没到 {angle}°",
                                            f"{key}: the polarization stage did not reach {angle}° in two attempts"))
        return None

    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        run.live.open_main("xy", title=_tr_lang("TERS 偏振序列", "TERS polarization series"),
                           x_label="angle", x_unit="deg",
                           y_label="intensity", y_unit="counts")
        start = run.facts["polarizer"].get("angle_deg")
        if self._needs_home:
            ok = yield from self._home(run)
            if not ok:
                return
        frac = float(p["exposure_guard_current_frac"])
        rows = []
        first_out = None
        for i, a in enumerate(self._angles_list):
            if run.check():
                break
            key = f"a{i}"
            rb = yield from self._move(run, key, a)
            if rb is None:
                break
            run.note_change("polarizer_deg", original=start, current=rb, restore=False,
                            how="polarization series")
            out = yield from self._acq(run, key, a, rb, frac, i)
            if out is None:
                break
            if out is not False:
                rows.append((a, rb, out))
                first_out = first_out or out
            if int(p["ref_every_n"]) and (i + 1) % int(p["ref_every_n"]) == 0 and i + 1 < len(self._angles_list):
                rb0 = yield from self._move(run, f"ref{i + 1}", self._angles_list[0])
                if rb0 is None:
                    break
                ref = yield from self._acq(run, f"ref{i + 1}", self._angles_list[0], rb0, frac, i,
                                           reference=True)
                if ref is None:
                    break
        if p["return_check"] and not run.stopped and first_out is not None:
            a0 = self._angles_list[0]
            rb = yield from self._move(run, "return", a0)
            if rb is not None:
                back = yield from self._acq(run, "return", a0, rb, frac, len(self._angles_list),
                                            reference=True)
                if back not in (None, False):
                    a_, b_ = first_out.band_value(), back.band_value()
                    if a_ and b_ is not None:
                        run.analysis["return_check_rel"] = b_ / a_ - 1.0
        self._fit(run, rows)

    def _home(self, run: TersRun) -> Iterator[CompositeStep]:
        decision = _tc.human_channel({
            "kind": "workflow_human", "workflow": run.name, "node_id": "pol_home",
            "decision_key": f"{run.run_id}:pol_home",
            "message": _tr_lang("偏振台未回零：回零会转整圈（≤ 36 s），回零期间不曝光。确认回零？",
                                "The polarization stage is not homed: homing turns a full turn (<= 36 s) and "
                                "nothing is exposed while homing. Confirm homing?"),
            "inputs": {"device_id": self._pol_dev}, "routes": ["home", "cancel"]})
        route = decision.get("route") if isinstance(decision, dict) else decision
        if route != "home":
            run.stop("refused" if decision is None else "needs_operator",
                     _tr_lang("偏振台未回零：需要操作员确认回零（这条执行路径没有人工确认通道 —— 请先手动 HomePolarizer）",
                              "The polarization stage is not homed: homing needs operator confirmation (this "
                              "execution path has no human confirmation channel - run HomePolarizer by hand first)")
                     if decision is None else _tr_lang(f"操作员没有同意回零（{route!r}）",
                                                       f"The operator did not agree to homing ({route!r})"))
            return False
        res = yield from run.step("pol:home", "HomePolarizer",
                                  {"device_id": str(self._pol_dev or ""), "timeout_s": 90.0})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"偏振台回零失败：{res.error}",
                                                f"Homing the polarization stage failed: {res.error}"))
            return False
        return True

    def _acq(self, run: TersRun, key: str, a: float, rb: float, frac: float, i: int, *,
             reference: bool = False) -> Iterator[CompositeStep]:
        p = run.p
        tr = run.ready_or_policy(label=key, key=key, i_target=run.i_set, feedback=True)
        if not tr.passed:
            run.record(None, key=key, status="unstable_tip", tip=tr, values={"pol_cmd_deg": a})
            return None if run.stopped else False
        out = yield from run.acquire(key, key, guard=GuardSpec(i_ref_a=run.i_set, frac=frac,
                                                               expect_feedback=True),
                                     extra={"pol_cmd_deg": a, "pol_deg": rb, "pol_element": p["element"],
                                            "pol_offset_deg": p["angle_offset_deg"],
                                            "pol_homed": True, "normalise": p["normalise"],
                                            "series_index": i, "reference": reference,
                                            "tip_ready": tr.as_meta()})
        run.record(out, key=key, tip=tr, values={"pol_cmd_deg": a, "pol_deg": rb, "reference": reference,
                                                 **_peak_values(out)})
        if not run.handle(out):
            return None
        if out.ok and not reference:
            vals = {n: m["area"] for n, m in out.bands.items() if m.get("valid")}
            if not vals and out.intensity is not None:
                vals["total"] = out.intensity
            run.live.publish({"x": float(rb), "values": vals})
        return out

    def _fit(self, run: TersRun, rows: list) -> None:
        p = run.p
        if not rows or p["fit_model"] == "none":
            return
        scale = 2.0 if p["element"] == "half_wave_plate" else 1.0
        theta = [(rb + float(p["angle_offset_deg"])) * scale for _, rb, _ in rows]
        fits = {}
        names = list(rows[0][2].bands) or ["total"]
        for name in names:
            vals = [(o.band_value(name) if name != "total" else o.intensity) for _, _, o in rows]
            ok = [(t, v) for t, v in zip(theta, vals) if v is not None]
            if len(ok) < 3:
                continue
            fit = ta.fit_cos2([t for t, _ in ok], [v for _, v in ok], model=p["fit_model"])
            if fit.get("valid") and scale != 1.0:
                fit["theta0_instrument_deg"] = fit["theta0_deg"] / scale - float(p["angle_offset_deg"])
                fit["note"] = _tr_lang("半波片：拟合横轴是偏振角 2θ",
                                       "Half-wave plate: the fit abscissa is the polarization angle 2θ")
            if fit.get("valid") and p["normalise"] == "malus":
                pred = [fit["B"] + fit["A"] * math.cos(math.radians(t - fit["theta0_deg"])) ** 2
                        for t, _ in ok]
                fit["malus_normalised"] = [v / q if q else None for (_, v), q in zip(ok, pred)]
            fits[name] = fit
        run.analysis["polar_fit"] = fits


# ═════════════════════════════════════════════════════════════════════════════
# M09 时间序列
# ═════════════════════════════════════════════════════════════════════════════

class _FrameTap:
    """采集期间从 LiveHub 上取逐帧谱（本机设备 = AcquireSpectrum 的 ``local/spectra/<dev>``，卫星 = 枢纽
    LiveBridge 的 ``<sat>/<dev>/spectrum``），算谱带强度实时推到运行的主流，并喂信号下降判据。

    LiveHub 的监听在发布线程上、持着 LiveHub 的锁被调用 —— 这里只入队，计算与发布在自己的线程里做。"""

    def __init__(self, run: TersRun, band: ta.Band | None, detector: ta.DropDetector) -> None:
        self.run = run
        self.band = band
        self.det = detector
        self.q: queue.Queue = queue.Queue(maxsize=10000)
        self.values: list[tuple[float, float]] = []
        self._stop = threading.Event()
        self._handle = None
        self._hub = None
        self._thread: threading.Thread | None = None
        names = {f"local/spectra/{run.spec_device}"}     # AcquireSpectrum's default live_stream
        if run.sat and run.remote_dev:
            names.add(f"{run.sat}/{run.remote_dev}/spectrum")
        self.streams = names

    def _listener(self, ev) -> None:
        try:
            if ev.type != "frame" or ev.stream not in self.streams:
                return
            meta = (ev.frame or {}).get("meta") or {}
            if meta.get("final") or meta.get("manual"):
                return
            self.q.put_nowait((ev.stream, ev.axis_ver, (ev.frame or {}).get("y"), time.time()))
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> None:
        try:
            from mast.live import get_live_hub

            self._hub = get_live_hub()
            self._handle = self._hub.add_listener(self._listener)
        except Exception:  # noqa: BLE001 — live display is optional
            return
        self._thread = threading.Thread(target=self._loop, name="ters-frame-tap", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._hub is not None and self._handle is not None:
            try:
                self._hub.remove_listener(self._handle)
            except Exception:  # noqa: BLE001
                pass
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not (self._stop.is_set() and self.q.empty()):
            try:
                stream, ver, y, t = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                ax = self._hub.axis(stream, ver) if ver else None
                arr = np.asarray(y, dtype=np.float64)
                if self.band is not None and ax and len(ax.get("x") or []) == arr.size:
                    x = ta.nm_to_shift_cm1(np.asarray(ax["x"], dtype=np.float64),
                                           float(self.run.p["laser_line_nm"]))
                    m = ta.band_metrics(x, arr, self.band, baselines=self.run.baselines or None)
                    v = m.get("area") if m.get("valid") else None
                else:
                    v = ta.total_counts(arr)
                if v is None:
                    continue
                self.values.append((t, v))
                self.run.live.publish({"t": t, "values": {"band": v}})
                # stop_on_drop takes effect at the block boundary: stopping the camera mid-block
                # would throw the block's frames away (the acquisition returns no file), and it
                # protects nothing — MAST cannot block the laser
                if self.det.update(v) and not self.run.analysis.get("signal_drop_live"):
                    self.run.analysis["signal_drop_live"] = {"t": t, "frame": len(self.values)}
            except Exception:  # noqa: BLE001 — a display path never breaks the run
                continue


class TERSTimeSeries(TersComposite):
    MODE_ID = "M09"
    MODE_KEY = "ters_time"
    NEEDS = Needs(tip="tunneling")

    def metadata(self) -> SkillMetadata:
        mode = [
            ParameterSpec(name="n_frames", type="int", required=False, default=100, min_value=1,
                          max_value=10000,
                          description=_tr_lang("总帧数。" + _INF, "Total number of frames. " + _INF_EN)),
            ParameterSpec(name="block_frames", type="int", required=False, default=0, min_value=0,
                          max_value=10000,
                          description=_tr_lang("每块帧数；0 = 不分块（一次采完）。块间隙在时间轴上标 gap。" + _INF,
                                               "Frames per block; 0 = no blocks (acquire everything in one go). "
                                               "Gaps between blocks are marked gap on the time axis. " + _INF_EN)),
            ParameterSpec(name="stm_log_hz", type="float", unit="Hz", required=False, default=20.0,
                          min_value=1.0, max_value=50.0,
                          description=_tr_lang("曝光期间 I / Z 的轮询速率（Signals_ValGet 路径上限约 50 Hz）。",
                                               "Polling rate of I / Z during the exposure (the Signals_ValGet "
                                               "path tops out at about 50 Hz).")),
            ParameterSpec(name="tracking_every_blocks", type="int", required=False, default=0,
                          min_value=0, max_value=1000,
                          description=_tr_lang("每 n 块做一次 atom tracking；尚未实现，> 0 会被拒绝。",
                                               "Run atom tracking every n blocks; not implemented yet, "
                                               "> 0 is refused.")),
            ParameterSpec(name="band_cm1", type="str", required=False, default="",
                          description=_tr_lang("在线时间轨迹用的谱带 'lo-hi'（cm⁻¹）；空 = 总计数。" + _INF,
                                               "Spectral band 'lo-hi' (cm⁻¹) for the online time trace; empty = "
                                               "total counts. " + _INF_EN)),
            ParameterSpec(name="baseline_cm1", type="str", required=False, default="",
                          description=_tr_lang("基线窗口；空 = 谱带两侧各 20 cm⁻¹。",
                                               "Baseline window; empty = 20 cm⁻¹ on each side of the band.")),
            ParameterSpec(name="signal_drop_frac", type="float", required=False, default=0.0,
                          min_value=0.0, max_value=1.0,
                          description=_tr_lang("信号下降阈值（相对滚动中位数）；0 = 关闭。" + _INF,
                                               "Signal-drop threshold (relative to the rolling median); 0 = off. "
                                               + _INF_EN)),
            ParameterSpec(name="drop_frames", type="int", required=False, default=5, min_value=1,
                          max_value=1000,
                          description=_tr_lang("连续多少帧低于阈值才算下降。" + _INF,
                                               "How many consecutive frames below the threshold count as a drop. "
                                               + _INF_EN)),
            ParameterSpec(name="stop_on_drop", type="bool", required=False, default=False,
                          description=_tr_lang("下降时自动停（停光谱仪，已采帧保留）；需要实时帧（本机设备或真枢纽）。" + _INF,
                                               "Stop automatically on a drop (stops the spectrometer, frames "
                                               "already acquired are kept); needs live frames (a local device or "
                                               "a real hub). " + _INF_EN)),
            ParameterSpec(name="time_base", type="str", required=False, default="camera",
                          allowed_values=["camera", "mast"],
                          description=_tr_lang("与 STM 对齐的时间基准：camera = 相机帧事件时间戳（经 θ 换算）；"
                                               "mast = 命令时刻均分。",
                                               "Time base aligned with the STM: camera = camera frame-event "
                                               "timestamps (converted via θ); mast = evenly spaced between the "
                                               "command times.")),
        ]
        return ters_metadata(
            name="TERSTimeSeries",
            description=_tr_lang(
                "光谱时间序列（M09）：针尖位置不动，判稳后一次（或分块）连续采 n_frames 帧，同时按 stm_log_hz 记录 "
                "I、Z；逐帧谱带强度实时推 LiveHub，可设信号下降自动停（超快激光下 TERS 信号可在数十秒内衰减）。"
                "事后按相机帧时间戳对齐 STM 轨迹。LiveHub：local/ters_time/<run_id>（谱带强度、I、Z 的时间轨迹）；"
                "最新帧在光谱仪自己的流上。",
                "Spectral time series (M09): the tip position stays still; after the stability check, "
                "n_frames frames are acquired continuously, in one go or in blocks, while I and Z are "
                "recorded at stm_log_hz; the per-frame band intensity is pushed to LiveHub in real time, "
                "and an automatic stop on a signal drop can be set (under an ultrafast laser the TERS "
                "signal can decay within tens of seconds). Afterwards the STM traces are aligned with the "
                "camera frame timestamps. LiveHub: local/ters_time/<run_id> (time traces of the band "
                "intensity, I and Z); the latest frame is on the spectrometer's own stream."),
            parameters=[*mode, *cp_a(overrides={"exposure_ms": 1000.0, "shutter_mode": "AlwaysOpen"},
                                     omit={"frames"}),
                        *cp_b(), *cp_c(), *common_extra()],
            tags=["optics", "ters", "spectrum", "acquisition", "time_series", "kinetics"],
            duration_s=300.0,
        )

    def prepare(self, run: TersRun) -> list[str]:
        p = run.p
        if int(p["tracking_every_blocks"]) > 0:
            return [_tr_lang("tracking_every_blocks > 0：块间 atom tracking 尚未实现",
                             "tracking_every_blocks > 0: atom tracking between blocks is not implemented "
                             "yet")]
        n = int(p["n_frames"])
        blk = int(p["block_frames"]) or n
        blocks = [min(blk, n - k) for k in range(0, n, blk)]
        self._blocks = blocks
        run.total_points = len(blocks)
        est = 20.0 + n * (float(p["exposure_ms"]) / 1000.0 + 0.0224) + len(blocks) * 2.0
        run.plan.update({"blocks": blocks, "estimated_s": round(est, 1)})
        if est > float(p["max_total_s"]):
            return [_tr_lang(f"预计 {est:.0f} s 超过 max_total_s={float(p['max_total_s']):.0f} s",
                             f"Estimated {est:.0f} s exceeds max_total_s={float(p['max_total_s']):.0f} s")]
        return []

    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        run.live.open_main("scalar", title=_tr_lang("TERS 时间序列", "TERS time series"),
                           y_label="intensity", y_unit="counts")
        run.stm_target = ""
        tr = run.ready_or_policy(label="start", key="start", i_target=run.i_set, feedback=True)
        if not tr.passed:
            if not run.stopped:
                run.stop("needs_operator", _tr_lang(f"判稳失败（{tr.reason}）—— 时间序列不开始",
                                                    f"Stability check failed ({tr.reason}) - the time series "
                                                    "does not start"))
            return
        frac = float(p["exposure_guard_current_frac"])
        det = ta.DropDetector(float(p["signal_drop_frac"]), int(p["drop_frames"]))
        band = run.bands[0] if run.bands else None
        series: list[dict] = []
        t_series0 = time.time()
        for b, n in enumerate(self._blocks):
            if run.check():
                break
            key = f"blk{b}"
            tap = _FrameTap(run, band, ta.DropDetector(float(p["signal_drop_frac"]),
                                                       int(p["drop_frames"])))
            if not run.dry_run:
                tap.start()
            try:
                out = yield from run.acquire(
                    key, key, frames=n,
                    guard=GuardSpec(i_ref_a=run.i_set, frac=frac, expect_feedback=True,
                                    poll_hz=float(p["stm_log_hz"])),
                    extra={"block_index": b, "block_frames": n, "time_base": p["time_base"],
                           "tip_ready": tr.as_meta() if b == 0 else None})
            finally:
                tap.stop()
            run.record(out, key=key, values={"block_index": b, "frames_requested": n,
                                             "live_values": len(tap.values)})
            frames = self._per_frame(run, out, b, t_series0)
            series.extend(frames)
            if not run.handle(out):
                break
            # the exact per-frame values (from the saved array) decide; the live tap only shows
            for f in frames:
                det.update(f.get("value"))
            if det.flagged and not run.analysis.get("signal_drop"):
                run.analysis["signal_drop"] = {"frame": det.flagged_at, "block": b}
                run.qc["signal_drop"] += 1
                if p["stop_on_drop"] and b + 1 < len(self._blocks):
                    run.stop("needs_operator",
                             _tr_lang(f"信号下降（≥ {float(p['signal_drop_frac']):.0%}，连续 "
                                      f"{p['drop_frames']} 帧）—— stop_on_drop，在块边界停下，已采帧保留",
                                      f"Signal drop (>= {float(p['signal_drop_frac']):.0%}, "
                                      f"{p['drop_frames']} consecutive frames) - stop_on_drop, stopping at the "
                                      "block boundary, frames already acquired are kept"))
                    break
        self._series = series
        vals = [f["value"] for f in series if f.get("value") is not None]
        run.analysis["time_series"] = {
            "frames": len(series), "blocks": len(self._blocks),
            "mean": float(np.mean(vals)) if vals else None,
            "cv": (float(np.std(vals) / np.mean(vals)) if vals and np.mean(vals) else None),
            "signal_drop": ({"frame": det.flagged_at} if det.flagged else None),
            "gaps": [i for i, f in enumerate(series) if f.get("gap_before")]}

    def _per_frame(self, run: TersRun, out, b: int, t0: float) -> list[dict]:
        """块里每一帧的谱带强度与时刻（相机帧事件 → 枢纽时钟；没有时按命令区间均分）。"""
        if out.frames is None:
            return []
        n = out.frames.shape[0]
        times = None
        if run.p["time_base"] == "camera" and out.sidecar_path:
            try:
                import json as _json
                from pathlib import Path as _P

                side = _json.loads(_P(out.sidecar_path).read_text(encoding="utf-8"))
                acq = side.get("acquisition") or {}
                fm = [f for f in (acq.get("frame_meta") or []) if isinstance(f, dict)]
                off = float(acq.get("clock_offset_ms") or 0.0)
                if len(fm) == n and all(isinstance(f.get("ts_sat_ms"), (int, float)) for f in fm):
                    times = [(float(f["ts_sat_ms"]) - off) / 1000.0 for f in fm]
            except (OSError, ValueError):
                times = None
        if times is None:
            from datetime import datetime

            try:
                a = datetime.fromisoformat(out.t_start_utc).timestamp()
                z = datetime.fromisoformat(out.t_end_utc).timestamp()
            except ValueError:
                a = z = time.time()
            times = [a + (z - a) * (k + 1) / n for k in range(n)]
        band = run.bands[0] if run.bands else None
        rows = []
        for k in range(n):
            y = out.frames[k]
            if band is not None and out.x_cm1 is not None:
                m = ta.band_metrics(out.x_cm1, y, band, baselines=run.baselines or None)
                v = m.get("area") if m.get("valid") else None
            else:
                v = ta.total_counts(y)
            rows.append({"block": b, "frame": k, "t": times[k], "t_rel_s": times[k] - t0,
                         "value": v, "gap_before": k == 0 and b > 0,
                         "cosmic": bool(ta.find_cosmic(y))})
        return rows

    def product_arrays(self, run: TersRun) -> dict:
        s = getattr(self, "_series", [])
        if not s:
            return {}
        return {"frame_t_unix_s": np.asarray([r["t"] for r in s], dtype=np.float64),
                "frame_value": np.asarray([r["value"] if r["value"] is not None else np.nan for r in s]),
                "frame_block": np.asarray([r["block"] for r in s]),
                "gap_mask": np.asarray([r["gap_before"] for r in s]),
                "cosmic_mask": np.asarray([r["cosmic"] for r in s])}
