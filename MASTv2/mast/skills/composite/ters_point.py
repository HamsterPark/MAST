"""M01 单点 TERS —— TERSPointSpectrum（规格 docs/v2/design/ters_modes_spec.md M01；契约 §8.3）。

在指定横向位置与隧穿条件下取 1 条（或 ``repeats`` 条）TERS 谱，每条谱都和曝光前后的 STM 状态、
判稳证据、曝光期间的电流统计绑在一起。其余模式都复用同一个守卫式采集（判稳 → 曝光 → 监视 → 复核），
那部分在 :mod:`ters_common`。

序列（M01.3）：前置检查 → 激光确认 → 配置光谱仪（唯一改设置的地方）+ 暗帧 →（可选）MoveToXY →
（可选）BiasSettleChange + SetSetpoint → 逐次重复：判稳 →（hold_mode=hold 时关反馈）→ 曝光 + 守卫 →
（重新闭合反馈）→ 质检 / 元数据 / LiveHub / 检查点。
"""

from __future__ import annotations

from typing import Iterator

from mast.core.types import ParameterSpec, SkillMetadata
from mast.i18n import tr as _tr_lang
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
    tip_lift_refusal,
)

__all__ = ["TERSPointSpectrum", "safe_withdraw"]

_INF_EN = "[default adjustable]"


def safe_withdraw(run: TersRun, why: str) -> dict:
    """退针（``ZCtrl_Withdraw`` 在中止之后也放行）。只在反馈已关、没法重新闭合时用 —— 让针尖在恒高下
    无人看管地漂，比退针更危险。结果记进 ``run.facts``。"""
    err = ""
    try:
        rec = run.ctx.safe_call("ZCtrl_Withdraw", 0, 1)
        err = str(getattr(rec, "error", "") or "")
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    entry = {"why": why, "sent": not err, "error": err}
    run.facts.setdefault("withdraws", []).append(entry)
    run.warnings.append(
        _tr_lang(f"已退针：{why}" + (f"（退针命令失败：{err}）" if err else ""),
                 f"Tip withdrawn: {why}" + (f" (withdraw command failed: {err})" if err else "")))
    return entry


class TERSPointSpectrum(TersComposite):
    MODE_ID = "M01"
    MODE_KEY = "ters_point"
    NEEDS = Needs(tip="tunneling")

    def metadata(self) -> SkillMetadata:
        mode = [
            ParameterSpec(name="use_current_position", type="bool", required=False, default=True,
                          description=_tr_lang("True = 不移动，在当前位置采谱；False 时 x_m / y_m 必填。",
                                               "True = do not move, acquire at the current position; "
                                               "x_m / y_m are required when False.")),
            ParameterSpec(name="x_m", type="float", unit="m", required=False, default=None,
                          min_value=-1.5e-6, max_value=1.5e-6,
                          description=_tr_lang("目标 X（范围同 MoveToXY）。",
                                               "Target X (range as for MoveToXY).")),
            ParameterSpec(name="y_m", type="float", unit="m", required=False, default=None,
                          min_value=-1.5e-6, max_value=1.5e-6,
                          description=_tr_lang("目标 Y。", "Target Y.")),
            ParameterSpec(name="coord_epoch", type="int", required=False, default=None,
                          description=_tr_lang(
                              "x/y 来自带代次的来源（地图标记 / 已存站位）时传入；发生过横向粗动"
                              "则拒绝移动（同 MoveToXY）。回读坐标请省略。",
                              "Pass it when x/y come from a source that carries an epoch (map marker / "
                              "saved station); the move is refused if lateral coarse motion has "
                              "happened since (as for MoveToXY). Omit it for read-back coordinates.")),
            ParameterSpec(name="apply_conditions", type="bool", required=False, default=False,
                          description=_tr_lang(
                              "True = 先按 bias_v / setpoint_a 设置条件（BiasSettleChange + "
                              "SetSetpoint，设好的条件留着，不还原）；False = 沿用当前。"
                              "【默认值可调】",
                              "True = first set the conditions from bias_v / setpoint_a "
                              "(BiasSettleChange + SetSetpoint; the new conditions are kept, "
                              "not restored); False = keep the current ones. " + _INF_EN)),
            ParameterSpec(name="bias_v", type="float", unit="V", required=False, default=None,
                          min_value=-3.0, max_value=3.0,
                          description=_tr_lang(
                              "apply_conditions 时的偏压。|V| 高于 ~1.75 V 时 STML 可进入 "
                              "635–693 nm 窗口，见 M06。【默认值可调】",
                              "Bias used when apply_conditions is set. Above |V| ~1.75 V the STML "
                              "can enter the 635-693 nm window, see M06. " + _INF_EN)),
            ParameterSpec(name="setpoint_a", type="float", unit="A", required=False, default=None,
                          min_value=1e-12, max_value=1e-8,
                          description=_tr_lang("apply_conditions 时的设定点电流。【默认值可调】",
                                               "Setpoint current used when apply_conditions is set. "
                                               + _INF_EN)),
            ParameterSpec(name="hold_mode", type="str", required=False, default="feedback",
                          allowed_values=["feedback", "hold"],
                          description=_tr_lang(
                              "曝光期间 Z 反馈：feedback = 保持闭环；hold = 关反馈保持当前 Z（恒高）。"
                              "hold 前读 tip lift 必须为 0，曝光后立即重新闭合；若中途被中止而"
                              "无法重新闭合，就退针（不让针尖在恒高下无人看管）。",
                              "Z feedback during the exposure: feedback = keep the loop closed; "
                              "hold = open the feedback and hold the current Z (constant height). "
                              "Before hold the tip lift must read 0, and the loop is re-closed right "
                              "after the exposure; if it was aborted and cannot be re-closed, the "
                              "tip is withdrawn (it is never left unattended at constant height).")),
            ParameterSpec(name="repeats", type="int", required=False, default=1, min_value=1,
                          max_value=1000,
                          description=_tr_lang("独立重复次数（每次单独判稳、曝光、复核）。【默认值可调】",
                                               "Number of independent repeats (each is settled, exposed "
                                               "and re-checked on its own). " + _INF_EN)),
            ParameterSpec(name="band_cm1", type="str", required=False, default="",
                          description=_tr_lang(
                              "在线强度 / SNR 用的谱带 'lo-hi'（cm⁻¹），如 '1560-1620'；"
                              "空 = 全窗口计数。【默认值可调】",
                              "Band 'lo-hi' (cm⁻¹) for the online intensity / SNR, e.g. '1560-1620'; "
                              "empty = count over the whole window. " + _INF_EN)),
            ParameterSpec(name="baseline_cm1", type="str", required=False, default="",
                          description=_tr_lang("基线窗口 'lo-hi,lo-hi'；空 = 谱带两侧各 20 cm⁻¹。",
                                               "Baseline windows 'lo-hi,lo-hi'; empty = 20 cm⁻¹ on each "
                                               "side of the band.")),
            ParameterSpec(name="drift_tol_pm", type="float", unit="pm", required=False, default=50.0,
                          min_value=5.0, max_value=1000.0,
                          description=_tr_lang(
                              "曝光前后 |z_B − z_A| 上限；超限标 drift（数据保留，不重拍）。"
                              "【默认值可调】",
                              "Upper limit of |z_B − z_A| before/after the exposure; beyond it the "
                              "spectrum is flagged drift (data kept, not re-taken). " + _INF_EN)),
        ]
        return ters_metadata(
            name="TERSPointSpectrum",
            description=_tr_lang(
                "单点 TERS（M01）：在当前位置或指定 (x, y)、当前或指定 (V, I_set) 下取 1 条或 repeats 条 "
                "TERS 谱；每条谱先判稳（固定等待 + 滑动窗口），曝光期间以 20 Hz 监视电流与反馈（越限 ⇒ "
                "取消采集、停在 needs_operator），并记录曝光前后的 STM 状态、漂移、判稳统计与激光确认。"
                "谱与 sidecar 归档进当前样品，运行级 index.csv / npz / summary.json 一并归档；"
                "LiveHub：local/ters_point/<run_id>（I、Z 轨迹）与 …/status。"
                "dry_run=true 只做只读判据与计划，不动硬件；dry_run_motion=true 再真走移动 / 条件 / hold 的"
                "关开反馈与判稳，仍不曝光不写数据。",
                "Single-point TERS (M01): acquire 1 or `repeats` TERS spectra at the current "
                "position or a given (x, y), under the current or a given (V, I_set). Each "
                "spectrum is first settled (fixed wait + sliding window); during the exposure "
                "the current and feedback are monitored at 20 Hz (out of limits => acquisition "
                "cancelled, stop at needs_operator), and the STM state before/after the "
                "exposure, drift, settling statistics and laser confirmation are recorded. "
                "Spectra and sidecars are archived into the current sample, together with the "
                "run-level index.csv / npz / summary.json; LiveHub: "
                "local/ters_point/<run_id> (I, Z traces) and .../status. dry_run=true only "
                "runs the read-only checks and the plan and does not touch the hardware; "
                "dry_run_motion=true additionally performs the real move / conditions / hold "
                "feedback off-on and settling, still without exposing or writing data."),
            parameters=[*mode, *cp_a(), *cp_b(), *cp_c(), *common_extra()],
            tags=["optics", "ters", "spectrum", "acquisition"],
            duration_s=60.0,
        )

    def prepare(self, run: TersRun) -> list[str]:
        p = run.p
        out = []
        if not p["use_current_position"] and (p.get("x_m") is None or p.get("y_m") is None):
            out.append(_tr_lang("use_current_position=false 时 x_m 与 y_m 都必须给",
                                "x_m and y_m are both required when use_current_position=false"))
        if p["apply_conditions"] and (p.get("bias_v") is None or p.get("setpoint_a") is None):
            out.append(_tr_lang("apply_conditions=true 时 bias_v 与 setpoint_a 都必须给",
                                "bias_v and setpoint_a are both required when apply_conditions=true"))
        reps = int(p["repeats"])
        run.total_points = reps
        per = (float(p["settle_s"]) + float(p["stable_window_s"])
               + float(p["exposure_ms"]) / 1000.0 * int(p["frames"]) + 0.5)
        est = 15.0 + reps * per
        run.plan.update({"repeats": reps, "estimated_s": round(est, 1),
                         "move_to": None if p["use_current_position"] else [p["x_m"], p["y_m"]],
                         "conditions": ({"bias_v": p["bias_v"], "setpoint_a": p["setpoint_a"]}
                                        if p["apply_conditions"] else None),
                         "hold_mode": p["hold_mode"]})
        if est > float(p["max_total_s"]):
            out.append(_tr_lang(f"预计 {est:.0f} s 超过 max_total_s={float(p['max_total_s']):.0f} s",
                                f"Estimated {est:.0f} s exceeds max_total_s={float(p['max_total_s']):.0f} s"))
        return out

    def preflight_extra(self, run: TersRun) -> Iterator[CompositeStep]:
        """hold 的 tip lift 判据在开始前就查（只规划的 dry_run 也查），每次关反馈前再查一次。"""
        if run.p["hold_mode"] == "hold":
            r = tip_lift_refusal(run)
            return [r] if r else []
        return []
        yield  # noqa: B901 — generator

    def dry_plan(self, run: TersRun) -> None:
        run.total_points = int(run.p["repeats"])

    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        run.live.open_main("scalar", title=_tr_lang("TERS 单点 · I / Z 轨迹", "TERS single point · I / Z traces"),
                           y_label="I", y_unit="A")
        run.stm_target = ""
        if not p["use_current_position"]:
            mv = {"x_m": float(p["x_m"]), "y_m": float(p["y_m"]), "wait": True}
            if p.get("coord_epoch") is not None:
                mv["coord_epoch"] = int(p["coord_epoch"])
            res = yield from run.step("move", "MoveToXY", mv)
            if not res.success:
                run.stop("needs_operator", _tr_lang(f"MoveToXY 失败：{res.error}",
                                                    f"MoveToXY failed: {res.error}"))
                return
        i_target = run.i_set
        if p["apply_conditions"]:
            v0, i0 = run.read_float("Bias_Get"), run.read_float("ZCtrl_SetpntGet")
            res = yield from run.step("bias", "BiasSettleChange",
                                      {"bias_v": float(p["bias_v"]), "settle_s": 0.0})
            if not res.success:
                run.stop("needs_operator", _tr_lang(f"BiasSettleChange 失败：{res.error}",
                                                    f"BiasSettleChange failed: {res.error}"))
                return
            run.note_change("bias_v", original=v0, current=float(p["bias_v"]), restore=False,
                            how="apply_conditions")
            res = yield from run.step("setpoint", "SetSetpoint", {"setpoint_a": float(p["setpoint_a"])})
            if not res.success:
                run.stop("needs_operator", _tr_lang(f"SetSetpoint 失败：{res.error}",
                                                    f"SetSetpoint failed: {res.error}"))
                return
            run.note_change("setpoint_a", original=i0, current=float(p["setpoint_a"]),
                            restore=False, how="apply_conditions")
            i_target = float(p["setpoint_a"])
        frac = float(p["exposure_guard_current_frac"])
        hold = p["hold_mode"] == "hold"
        intensities: dict[str, list[float]] = {}
        for r in range(int(p["repeats"])):
            key = f"r{r}"
            if key in run.resume_done:
                continue
            if run.check():
                break
            tr = run.ready_or_policy(label=key, key=key, i_target=i_target, feedback=True)
            if run.stopped:
                break
            if not tr.passed:
                run.record(None, key=key, status="unstable_tip", tip=tr, values={"repeat_index": r})
                continue
            # dry_run_motion takes this same path: the hold's feedback off / on and every check run as
            # for real; only the acquisition below comes back as dry_run (nothing exposed or written)
            if hold:
                lift_refusal = tip_lift_refusal(run)
                if lift_refusal:
                    run.stop("needs_operator", lift_refusal)
                    break
                res = yield from run.step(f"{key}:hold", "ZControllerOnOff", {"enable": False})
                if not res.success:
                    run.stop("needs_operator", _tr_lang(f"关反馈失败：{res.error}",
                                                        f"Opening the feedback failed: {res.error}"))
                    break
                run.note_change("z_feedback", original=True, current=False, how="hold_mode=hold")
            guard = GuardSpec(i_ref_a=i_target, frac=frac, expect_feedback=not hold)
            out = yield from run.acquire(
                key, key, guard=guard,
                extra={"repeat_index": r, "series_index": r, "tip_ready": tr.as_meta(),
                       "conditions": {"hold_mode": p["hold_mode"], "i_target_a": i_target,
                                      "x_m": p.get("x_m"), "y_m": p.get("y_m")}})
            if hold:
                yield from self._reclose(run, key)
            run.record(out, key=key, tip=tr, values={"repeat_index": r})
            for name, m in out.bands.items():
                if m.get("valid"):
                    intensities.setdefault(name, []).append(float(m["area"]))
            if out.ok and not out.bands and out.intensity is not None:
                intensities.setdefault("total", []).append(float(out.intensity))
            if not run.handle(out):
                break
        run.analysis["band_intensity"] = {
            k: {"n": len(v), "mean": float(sum(v) / len(v)),
                "std": (float((sum((x - sum(v) / len(v)) ** 2 for x in v) / (len(v) - 1)) ** 0.5)
                        if len(v) > 1 else 0.0)}
            for k, v in intensities.items() if v}

    @staticmethod
    def _reclose(run: TersRun, key: str) -> Iterator[CompositeStep]:
        """hold 之后重新闭合反馈；中止之后闭合会被拒（不在中止安全名单里）⇒ 退针。"""
        if run._aborted():
            safe_withdraw(run, _tr_lang(f"{key}: hold_mode=hold 期间被中止，反馈无法重新闭合",
                                        f"{key}: aborted during hold_mode=hold, feedback cannot be re-closed"))
            return
        res = yield from run.step(f"{key}:reclose", "ZControllerOnOff", {"enable": True})
        if res.success:
            run.note_restored("z_feedback", True)
            return
        run.stop("needs_operator", _tr_lang(f"{key}: 反馈没能重新闭合：{res.error}",
                                            f"{key}: feedback could not be re-closed: {res.error}"))
        safe_withdraw(run, _tr_lang(f"{key}: 反馈没能重新闭合", f"{key}: feedback could not be re-closed"))
