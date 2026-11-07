# -*- coding: utf-8 -*-
"""AcquireBiasSeries：在同一位置按指定结阻取得偏压序列。

改变偏压而维持同一 setpoint 会改变 V/I，使反馈调整针尖距离。
本技能按共同结阻计算各档 setpoint，以减少这一混杂因素。
恒结阻并不保证所有样品上的距离严格不变，仍需检查结状态。

首尾重复相同条件作为对照；漂移或针尖状态变化会影响序列可比性，
不能把所有图像差异直接解释成偏压效应。
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

#: setpoint 的下界（A）。低偏压端按恒结阻算出来的电流可能低于噪声底，
#: 那时图上就没有信号 —— 宁可让那一档结阻偏小（针尖稍近），也不要采一张噪声。
_MIN_SETPOINT_A = 5e-12

#: setpoint 的上界（A）。恒结阻在高偏压端会要求很大的电流，那是另一种「太近」。
_MAX_SETPOINT_A = 500e-12


def setpoints_for(biases, r_ohm):
    """按恒定结阻给每个偏压算 setpoint，越界夹回来并**如实标记夹过**。

    返回 [(bias_v, setpoint_a, clamped)]。夹过的那几档结阻不再等于目标值，
    调用方在横向比较时必须知道这件事 —— 所以标记要跟着数据走，不能只写日志。
    """
    out = []
    for b in biases:
        want = abs(float(b)) / float(r_ohm)
        sp = min(max(want, _MIN_SETPOINT_A), _MAX_SETPOINT_A)
        out.append((float(b), float(sp), bool(abs(sp - want) > 1e-15)))
    return out


class AcquireBiasSeries(BaseSkill):
    """Bias-dependent image series at one spot, at constant junction resistance."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AcquireBiasSeries",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("在**同一个位置**按一串偏压各扫一帧，用来看偏压依赖（占据态／空态）。"
                "\n\n**关键：按恒定结阻 R=V/I 配 setpoint，不是固定 setpoint。** "
                "隧道结的距离由结阻决定；偏压降下去而 setpoint 不动 ⇒ 针尖被推近。"
                "在某个表面覆着松散团簇的样品上，把偏压往下调而 setpoint 不动，"
                "同一位置连扫几帧，RMS 会一路爬升、Z 摆动可能接近压电"
                "量程上限；拉开到更低结阻后立刻稳定。"
                "**不是针尖坏，是条件太近。**"
                "\n\n**序列首尾各加一帧相同条件做漂移对照。** 没有它，整组图不能横向比 —— "
                "曾见过：首尾 RMS 与逐行 σ 都明显下降，是针尖在这几分钟里变稳了，"
                "若没有对照就会被读成偏压的效应。"
                "\n\n每帧同时报 `AssessFrameTrust` 的逐行 MAD，好让调用方知道哪几帧可信。",
                "Scan one frame at each of a series of biases at the **same position**, to see the bias dependence (occupied / empty states).\n\n**Key: set the setpoint by constant junction resistance R=V/I, not a fixed setpoint.** The tunnel-junction distance is determined by the junction resistance; lowering the bias while the setpoint stays put => the tip is pushed closer. This is a known failure mode: several frames in a row at the same position at a low junction resistance can see the RMS climb and Z swing close to the piezo range; it looks like 'the tip is degrading', but widening to a higher junction resistance stabilises it immediately. **The tip was not bad, the conditions were too close.**\n\n**Add one frame of identical conditions at each end of the sequence as a drift control.** Without it the whole set cannot be compared across frames - in one case the RMS and row sigma both dropped clearly from first to last, meaning the tip became stable over those minutes, which without the control would be read as a bias effect.\n\nEach frame also reports the per-row MAD of `AssessFrameTrust`, so the caller knows which frames to trust.")
            ),
            parameters=[
                ParameterSpec(
                    name="center_x_m", type="float", unit="m",
                    description=tr("扫描中心 x。", "Scan centre x."), required=True),
                ParameterSpec(
                    name="center_y_m", type="float", unit="m",
                    description=tr("扫描中心 y。", "Scan centre y."), required=True),
                ParameterSpec(
                    name="size_m", type="float", unit="m",
                    description=tr("视野边长。", "Field-of-view edge length."), required=True,
                    min_value=1e-9, max_value=2e-6),
                ParameterSpec(
                    name="biases_v", type="str",
                    description=(
                        tr("偏压序列，逗号分隔（V）。**正负都给**才看得出占据态与空态的差别，"
                        "例如 '2,1.5,1,0.5,-0.5,-1,-1.5,-2'。",
                        "Bias sequence, comma-separated (V). Give **both signs** to see the difference between occupied and empty states, e.g. '2,1.5,1,0.5,-0.5,-1,-1.5,-2'.")),
                    required=True),
                ParameterSpec(
                    name="junction_r_ohm", type="float", unit="ohm",
                    description=(
                        tr("目标结阻。整条序列共用它来算每一档的 setpoint。"
                        "越大针尖越远越安全；脏表面上宜取 100 GΩ 以上"
                        "（过小的结阻会推动松散团簇）。",
                        "Target junction resistance. The whole sequence uses it to compute each step's setpoint. The larger it is, the farther and safer the tip; on a dirty surface use 100 GΩ or more (too small a junction resistance can push loose clusters).")),
                    required=False, default=1e11,
                    min_value=1e8, max_value=1e13),
                ParameterSpec(
                    name="pixels", type="int",
                    description=tr("每帧像素数。", "Pixels per frame."), required=False, default=192,
                    min_value=32, max_value=1024),
                ParameterSpec(
                    name="line_time_s", type="float", unit="s",
                    description=tr("每行时间。", "Time per line."), required=False, default=0.05,
                    min_value=0.005, max_value=5.0),
            ],
            estimated_duration_s=600.0,
            composition_level=1,
            tags=["imaging", "bias", "series", "偏压依赖", "spectroscopy"],
        )

    def validate_params(self, params: dict) -> list[str]:
        errors = list(super().validate_params(params) or [])
        raw = str(params.get("biases_v") or "").strip()
        if raw:
            try:
                vals = [float(t) for t in raw.split(",") if t.strip()]
            except ValueError:
                errors.append(tr("biases_v 解析不了：%r（要逗号分隔的数字）", "biases_v could not be parsed: %r (a comma-separated list of numbers is expected)") % raw)
            else:
                if len(vals) < 2:
                    errors.append(tr("偏压序列至少要 2 档（给了 %d）", "The bias sequence needs at least 2 steps (%d given)") % len(vals))
                if any(v == 0 for v in vals):
                    errors.append(
                        tr("偏压序列里不能有 0 V —— 恒结阻在 0 V 处算出 0 电流，"
                        "而且 0 偏压下本来就没有隧穿。",
                        "The bias sequence cannot contain 0 V - at 0 V the constant junction resistance gives 0 current, and there is no tunnelling at 0 bias anyway."))
        return errors

    def _one(self, context, x, y, size, bias, sp, px, line, tag):
        res = context.run("ScanAt", {
            "center_x_m": x, "center_y_m": y, "size_m": size,
            "pixels": int(px), "line_time_s": float(line),
            "bias_v": float(bias), "setpoint_a": float(sp),
            "purpose": "survey",
        })
        row = {"tag": tag, "bias_v": float(bias), "setpoint_a": float(sp),
               "ok": bool(getattr(res, "success", False))}
        data = getattr(res, "data", None) or {}
        path = data.get("scan_path") or data.get("path") or data.get("file")
        if path:
            row["scan_path"] = path
            trust = context.run("AssessFrameTrust", {"scan_path": path})
            td = getattr(trust, "data", None) or {}
            row["row_jump_mad_pm"] = td.get("row_jump_mad_pm")
            row["tip_verdict"] = td.get("tip_verdict")
            row["rms_pm"] = td.get("rms_pm")
        if not row["ok"]:
            row["error"] = str(getattr(res, "error", ""))[:200]
        return row

    def execute(self, context, params: dict) -> SkillResult:
        # 同 barrier_map：**不假设 validate_params 跑过**，坏参数在这里也要返回而不是炸。
        raw = str(params.get("biases_v") or "")
        try:
            biases = [float(t) for t in raw.split(",") if t.strip()]
        except ValueError:
            return SkillResult(skill_name="AcquireBiasSeries", success=False,
                               error=tr("biases_v 解析不了：%r（要逗号分隔的数字）", "biases_v could not be parsed: %r (a comma-separated list of numbers is expected)") % raw)
        biases = [b for b in biases if b != 0.0]     # 0 V 在恒结阻下算出 0 电流
        if len(biases) < 2:
            return SkillResult(
                skill_name="AcquireBiasSeries", success=False,
                error=(tr("偏压序列至少要 2 档非零值（收到 %d）。0 V 在恒结阻下算出 0 电流，"
                       "而且 0 偏压下本来就没有隧穿。",
                       "The bias sequence needs at least 2 non-zero values (received %d). At 0 V the constant junction resistance gives 0 current, and there is no tunnelling at 0 bias anyway.") % len(biases)))
        r_ohm = float(params.get("junction_r_ohm") or 1e11)
        x = float(params["center_x_m"])
        y = float(params["center_y_m"])
        size = float(params["size_m"])
        px = int(params.get("pixels") or 192)
        line = float(params.get("line_time_s") or 0.05)

        plan = setpoints_for(biases, r_ohm)
        clamped = [b for b, _, c in plan if c]
        # 漂移对照用序列里绝对值最大的那一档（信号最强、最稳）
        ref_bias, ref_sp, _ = max(plan, key=lambda t: abs(t[0]))

        frames = [self._one(context, x, y, size, ref_bias, ref_sp, px, line, "pre_drift")]
        for bias, sp, _c in plan:
            frames.append(self._one(context, x, y, size, bias, sp, px, line, "series"))
        frames.append(self._one(context, x, y, size, ref_bias, ref_sp, px, line, "post_drift"))

        pre = frames[0]
        post = frames[-1]
        drift = None
        if pre.get("row_jump_mad_pm") is not None and post.get("row_jump_mad_pm") is not None:
            a, b = pre["row_jump_mad_pm"], post["row_jump_mad_pm"]
            worse = b > max(2.0 * a, a + 20.0)
            better = a > max(2.0 * b, b + 20.0)
            drift = {
                "pre_row_mad_pm": a, "post_row_mad_pm": b,
                "comparable": not (worse or better),
                "note": (tr("针尖在序列中**变差**了（%.1f → %.1f pm）—— 这组图不能横向比，"
                         "偏压的效应和针尖的变化混在一起。",
                         "The tip got **worse** during the sequence (%.1f -> %.1f pm) - this set cannot be compared across frames; bias effects and tip changes are mixed together.") % (a, b)) if worse else
                        (tr("针尖在序列中**变稳**了（%.1f → %.1f pm）—— 别把这段变化读成偏压的效应。", "The tip got **more stable** during the sequence (%.1f -> %.1f pm) - do not read this change as a bias effect.")
                         % (a, b)) if better else
                        (tr("首尾针尖状态相当（%.1f / %.1f pm）—— 这组图可以横向比。", "First and last tip states are comparable (%.1f / %.1f pm) - this set can be compared across frames.") % (a, b)),
            }
        ok = [f for f in frames if f.get("ok")]
        return SkillResult(
            skill_name="AcquireBiasSeries", success=bool(ok),
            error="" if ok else tr("一帧都没扫成。", "No frame was scanned successfully."),
            data={
                "junction_r_ohm": r_ohm,
                "plan": [{"bias_v": b, "setpoint_a": s, "clamped": c} for b, s, c in plan],
                "clamped_biases": clamped,
                "clamp_note": (
                    tr("这几档的 setpoint 被夹在 [%.0f, %.0f] pA 之内，结阻不再等于目标值：%s"
                    " —— 横向比较时要知道它们的针尖距离与其余档不同。",
                    "The setpoint of these steps was clamped to within [%.0f, %.0f] pA, so the junction resistance no longer equals the target: %s - when comparing across steps, note that their tip distance differs from the others.")
                    % (_MIN_SETPOINT_A * 1e12, _MAX_SETPOINT_A * 1e12, clamped)) if clamped else "",
                "frames": frames,
                "n_ok": len(ok), "n_total": len(frames),
                "drift_check": drift,
            })
