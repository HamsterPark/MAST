"""Alignment skills — beam steering (Picomotor) and the focusing XYZ stage on the
spectroscopy computer (satellite).

3 skills: GetPicomotorSteps (AUTO), PicomotorJog (CONFIRM), MoveXYZStage (CONFIRM).

* Picomotor 8742 is **open loop**: a jog commands steps, and the step counter is
  the satellite's sum of commands — not a measured position. Per-command caps
  and cumulative soft limits are enforced by the axis (Layer-0,
  :mod:`mast.instruments.openloop`); an oversize jog is refused, never split.
* The XYZ stage (UIRobot via Motion_Manipulator) moves in millimetres; soft
  limits per axis plus a per-command step cap (``max_step_mm``) apply on the hub
  side, and the satellite enforces its own copy.

Neither may move while a spectrum is being exposed — composites move between
acquisitions; these atomic skills hold the instrument token like every write.
"""

from __future__ import annotations

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.instruments.base import InstrumentError
from mast.instruments.registry import get_instrument_registry
from mast.skills.base import BaseSkill
from mast.skills.builtins.optics_common import (
    abort_probe,
    fail,
    resolve_device,
    stage_tip_result,
)
from mast.i18n import tr

__all__ = ["GetPicomotorSteps", "PicomotorJog", "MoveXYZStage"]

PICOMOTOR_ROLE = "beam_steer"
XYZ_ROLE = "focus_xyz"


def _pico(params: dict):
    reg = get_instrument_registry()
    dev = resolve_device(reg, kind="openloop", role=PICOMOTOR_ROLE,
                         device_id=params.get("device_id") or None)
    return dev, reg.controller(dev)


class GetPicomotorSteps(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetPicomotorSteps", version="1.0.0", category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(tr("读 Picomotor 各轴的累计步数（卫星记账：命令过的步数之和，**不是位置反馈**）、"
                         "是否在动，以及每轴的步数软限位与单次上限。只读。", "Read the cumulative step count of each Picomotor axis (satellite bookkeeping: the sum of commanded steps, **not position feedback**), "
"whether it is moving, and the soft step limit and per-move cap of each axis. Read-only.")),
            parameters=[ParameterSpec(name="device_id", type="str", required=False, default="",
                                      description=tr("开环台设备 id；留空 = 唯一一台 role=beam_steer 的", "Open-loop stage device id; empty = the only one with role=beam_steer"))],
            estimated_duration_s=0.3, composition_level=0,
            tags=["optics", "picomotor", "openloop", "alignment", "read", "status",
                  "optional-hardware"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev, ctrl = _pico(params)
            axes = {}
            for name, ax in ctrl.axes().items():
                st = ax.get_status()
                axes[name] = {"steps": int(st.position) if st.position == st.position else None,
                              "moving": st.moving,
                              "limits_steps": [ax.config.min_pos, ax.config.max_pos],
                              "max_steps_per_cmd": getattr(ax, "max_steps_per_cmd", None),
                              "channel": ax.config.channel}
        except Exception as exc:  # noqa: BLE001
            return fail("GetPicomotorSteps", exc)
        brief = ", ".join(f"{k}={v['steps']}" for k, v in axes.items())
        return SkillResult(skill_name="GetPicomotorSteps", success=True,
                           data={"device_id": dev, "axes": axes, "feedback": False},
                           summary=f"picomotor {dev}: {brief} (commanded steps, no feedback)")


class PicomotorJog(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="PicomotorJog", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("Picomotor 某一轴走 steps 步（正负表示方向，开环）。单次超过上限、或累计步数会越出软限位时直接拒绝"
                "（不会拆成几次、也不会截断）。标称 <30 nm/步，但开环：电机打滑也照样记步。用于对准：小步走、"
                "每步之后量一次信号（例如 AcquireSignalPoint / AcquireSpectrum）。", "Move one Picomotor axis by steps steps (sign gives direction, open loop). Refused outright if a single move exceeds the cap, or if the cumulative step count would cross the soft limit "
"(never split into several moves, never truncated). Nominally <30 nm/step, but open loop: steps are counted even when the motor slips. For alignment: take small steps and "
"measure the signal after each step (e.g. AcquireSignalPoint / AcquireSpectrum).")),
            parameters=[
                ParameterSpec(name="device_id", type="str", required=False, default="",
                              description=tr("开环台设备 id；留空 = 唯一一台 role=beam_steer 的", "Open-loop stage device id; empty = the only one with role=beam_steer")),
                ParameterSpec(name="axis", type="str", required=True,
                              description=tr("轴名（GetPicomotorSteps 列出的，例如 '2' 或 'pico_x'）", "Axis name (as listed by GetPicomotorSteps, e.g. '2' or 'pico_x')")),
                ParameterSpec(name="steps", type="int", required=True,
                              min_value=-100_000, max_value=100_000,
                              description=tr("步数（带符号）；单次上限见 GetPicomotorSteps", "Number of steps (signed); see GetPicomotorSteps for the per-move cap")),
                ParameterSpec(name="timeout_s", type="float", unit="s", required=False,
                              default=30.0, min_value=0.5, max_value=600.0,
                              description=tr("动作时限", "Action time limit")),
            ],
            estimated_duration_s=2.0, composition_level=0,
            tags=["optics", "picomotor", "openloop", "alignment", "move", "write",
                  "optional-hardware"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev, ctrl = _pico(params)
            refused = stage_tip_result("PicomotorJog", context, dev)
            if refused is not None:
                return refused
            ax = ctrl.axis(str(params["axis"]))
            before = ax.get_steps()
            st = ax.jog(int(params["steps"]), wait=True,
                        timeout=float(params.get("timeout_s", 30.0)),
                        check_abort=abort_probe(context))
            after = ax.get_steps()
        except Exception as exc:  # noqa: BLE001
            return fail("PicomotorJog", exc)
        return SkillResult(
            skill_name="PicomotorJog", success=True,
            data={"device_id": dev, "axis": str(params["axis"]),
                  "steps_commanded": int(params["steps"]), "counter_before": before,
                  "counter_after": after, "moving": st.moving, "feedback": False},
            summary=f"picomotor {dev}/{params['axis']}: {int(params['steps']):+d} steps "
                    f"(counter {before} → {after}, open loop)")


class MoveXYZStage(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="MoveXYZStage", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("移动光谱电脑（卫星）上的 XYZ 平移台（毫米）。给哪个轴就动哪个轴（依次 x→y→z），relative=true 时为相对位移。"
                "每轴软限位与单次步长上限在枢纽这边强制执行，卫星那边也有一份；越界或步子太大直接拒绝。"
                "开环步进电机：读数是编码器/计数列，不是独立测量。", "Move the XYZ translation stage on the spectrometer computer (satellite), in millimetres. Whichever axis is given is moved (in order x->y->z); with relative=true the values are relative displacements. "
"The per-axis soft limit and per-move step cap are enforced on the hub side, and the satellite has its own copy; out-of-range or oversized steps are refused outright. "
"Open-loop stepper motor: the readout is an encoder / counter column, not an independent measurement.")),
            parameters=[
                ParameterSpec(name="device_id", type="str", required=False, default="",
                              description=tr("XYZ 台设备 id；留空 = 唯一一台 role=focus_xyz 的", "XYZ stage device id; empty = the only one with role=focus_xyz")),
                ParameterSpec(name="x", type="float", unit="mm", required=False,
                              min_value=-50.0, max_value=50.0, description=tr("X 目标（或位移）", "X target (or displacement)")),
                ParameterSpec(name="y", type="float", unit="mm", required=False,
                              min_value=-50.0, max_value=50.0, description=tr("Y 目标（或位移）", "Y target (or displacement)")),
                ParameterSpec(name="z", type="float", unit="mm", required=False,
                              min_value=-50.0, max_value=50.0, description=tr("Z 目标（或位移）", "Z target (or displacement)")),
                ParameterSpec(name="relative", type="bool", required=False, default=False,
                              description=tr("True = 相对当前位置移动", "True = move relative to the current position")),
                ParameterSpec(name="timeout_s", type="float", unit="s", required=False,
                              default=60.0, min_value=0.5, max_value=600.0,
                              description=tr("每个轴的动作时限", "Action time limit per axis")),
            ],
            estimated_duration_s=5.0, composition_level=0,
            tags=["optics", "xyz", "alignment", "move", "write", "optional-hardware"])

    def execute(self, context, params: dict) -> SkillResult:
        wanted = [(k, float(params[k])) for k in ("x", "y", "z") if params.get(k) is not None]
        if not wanted:
            return SkillResult(skill_name="MoveXYZStage", success=False,
                               error="give at least one of x / y / z")
        moved: dict[str, dict] = {}
        try:
            reg = get_instrument_registry()
            dev = resolve_device(reg, kind="motion", role=XYZ_ROLE,
                                 device_id=params.get("device_id") or None)
            refused = stage_tip_result("MoveXYZStage", context, dev)
            if refused is not None:
                return refused
            ctrl = reg.controller(dev)
            by_key = {getattr(ax, "key", name): ax for name, ax in ctrl.axes().items()}
            probe = abort_probe(context)
            timeout = float(params.get("timeout_s", 60.0))
            for key, value in wanted:
                ax = by_key.get(key)
                if ax is None:
                    raise InstrumentError(f"{dev} has no {key} axis (axes: {sorted(by_key)})")
                start = ax.get_position()
                if params.get("relative"):
                    st = ax.move_rel(value, wait=True, timeout=timeout, check_abort=probe)
                else:
                    st = ax.move_abs(value, wait=True, timeout=timeout, check_abort=probe)
                moved[key] = {"start_mm": start, "end_mm": st.position,
                              "requested": value, "relative": bool(params.get("relative"))}
        except Exception as exc:  # noqa: BLE001
            return fail("MoveXYZStage", exc, moved=moved)
        brief = ", ".join(f"{k} {v['start_mm']:.4f}→{v['end_mm']:.4f}" for k, v in moved.items())
        return SkillResult(skill_name="MoveXYZStage", success=True,
                           data={"device_id": dev, "moved": moved},
                           summary=f"XYZ {dev}: {brief} mm")
