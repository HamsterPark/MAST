"""Polariser skills — the rotation mount in the TERS excitation path.

3 skills: GetPolarizerAngle (AUTO), SetPolarizerAngle (CONFIRM), HomePolarizer
(CONFIRM). The device is a ``satellite_rotation`` entry (the satellite's KDC101 + PRM1-Z8)
or a ``sim_rotation`` one, found by ``role: polarizer`` unless ``device_id`` is
given. Soft limits, wrap and shortest-path rules are the axis's (Layer-0,
:mod:`mast.instruments.rotation`); these skills never clamp an angle.

A KDC101 must be homed after power-up before its angle means anything; the
status' ``homed`` flag is reported on every read and a move on an un-homed
mount is refused with an instruction to run HomePolarizer first.
"""

from __future__ import annotations

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.instruments.registry import get_instrument_registry
from mast.skills.base import BaseSkill
from mast.skills.builtins.optics_common import (
    abort_probe,
    fail,
    resolve_device,
    stage_tip_result,
)

__all__ = ["GetPolarizerAngle", "SetPolarizerAngle", "HomePolarizer"]

POLARIZER_ROLE = "polarizer"
_TAGS = ["optics", "polarizer", "rotation", "ters", "optional-hardware"]


def _device_param() -> ParameterSpec:
    return ParameterSpec(
        name="device_id", type="str", required=False, default="",
        description=tr("旋转台设备 id；留空 = 唯一一台 role=polarizer 的旋转台",
                       "Rotation stage device id; leave empty = the only rotation stage with role=polarizer"))


def _axis(params: dict):
    reg = get_instrument_registry()
    dev = resolve_device(reg, kind="rotation", role=POLARIZER_ROLE,
                         device_id=params.get("device_id") or None)
    axes = reg.controller(dev).axes()
    if len(axes) != 1:
        raise ValueError(f"rotation device {dev!r} has {len(axes)} axes; expected 1")
    name, ax = next(iter(axes.items()))
    return dev, name, ax


def _reading(dev: str, name: str, ax, status) -> dict:
    out = {"device_id": dev, "axis": name, "angle_deg": status.position,
           "moving": status.moving, "homed": status.homed,
           "limits_deg": [ax.config.min_pos, ax.config.max_pos]}
    norm = getattr(ax, "normalized", None)
    if callable(norm):
        try:
            out["angle_mod_period_deg"] = norm(status.position)
        except Exception:  # noqa: BLE001
            pass
    return out


class GetPolarizerAngle(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetPolarizerAngle", version="1.0.0", category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(tr("读偏振片角度（度）、是否在转、是否已回零（homed），以及软限位。只读。"
                         "上电后未回零的读数没有意义（homed=false）。",
                            "Read the polarizer angle (degrees), whether it is rotating, whether it has been homed (homed=false means not homed), and the soft limits. Read-only. "
                            "After power-up, readings before homing are meaningless (homed=false).")),
            parameters=[_device_param()],
            estimated_duration_s=0.3, composition_level=0,
            tags=[*_TAGS, "read", "status"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev, name, ax = _axis(params)
            st = ax.get_status()
        except Exception as exc:  # noqa: BLE001
            return fail("GetPolarizerAngle", exc)
        data = _reading(dev, name, ax, st)
        return SkillResult(skill_name="GetPolarizerAngle", success=True, data=data,
                           summary=(f"polariser {dev} at {st.position:.3f}°"
                                    + ("" if st.homed is not False else " (NOT homed)")))


class SetPolarizerAngle(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetPolarizerAngle", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("把偏振片转到某个角度（度），或 relative=true 时相对转动。软限位在驱动层强制执行，越界直接拒绝"
                "（不会截到边界）；配置了 wrap 时 370° 会按等价角落在限位内，相对转动从不改写方向。"
                "未回零的旋转台拒绝移动——先跑 HomePolarizer。转动期间不要曝光（组合技能在两次采集之间转）。",
                   "Rotate the polarizer to an angle (degrees), or relative to the current angle when relative=true. Soft limits are enforced in the driver layer and out-of-range targets are refused outright "
                   "(not clamped to the boundary); with wrap configured, 370° lands inside the limits as its equivalent angle, and a relative rotation never changes direction. "
                   "A stage that has not been homed refuses to move - run HomePolarizer first. Do not expose during rotation (composite skills rotate between acquisitions).")),
            parameters=[
                _device_param(),
                ParameterSpec(name="angle_deg", type="float", unit="deg", required=True,
                              min_value=-1080.0, max_value=1080.0,
                              description=tr("目标角度（绝对）或转角（relative=true）",
                                             "Target angle (absolute) or rotation angle (relative=true)")),
                ParameterSpec(name="relative", type="bool", required=False, default=False,
                              description=tr("True = 从当前角度转 angle_deg", "True = rotate angle_deg from the current angle")),
                ParameterSpec(name="timeout_s", type="float", unit="s", required=False,
                              default=60.0, min_value=1.0, max_value=600.0,
                              description=tr("转动时限（90° 约 5–10 s）", "Rotation time limit (about 5-10 s for 90°)")),
            ],
            estimated_duration_s=6.0, composition_level=0,
            tags=[*_TAGS, "move", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev, name, ax = _axis(params)
            refused = stage_tip_result("SetPolarizerAngle", context, dev)
            if refused is not None:
                return refused
            st0 = ax.get_status()
            if st0.homed is False:
                return SkillResult(
                    skill_name="SetPolarizerAngle", success=False,
                    error=(tr(f"偏振片 {dev} 尚未回零（homed=false）——上电后的角度读数没有意义，"
                           "先跑 HomePolarizer 再设角度。",
                              f"polarizer {dev} has not been homed (homed=false) - angle readings after power-up are meaningless, "
                              "run HomePolarizer before setting the angle.")),
                    data=_reading(dev, name, ax, st0))
            kw = dict(wait=True, timeout=float(params.get("timeout_s", 60.0)),
                      check_abort=abort_probe(context))
            if params.get("relative"):
                st = ax.move_rel(float(params["angle_deg"]), **kw)
            else:
                st = ax.move_abs(float(params["angle_deg"]), **kw)
        except Exception as exc:  # noqa: BLE001
            return fail("SetPolarizerAngle", exc)
        data = _reading(dev, name, ax, st)
        data["requested_deg"] = float(params["angle_deg"])
        data["relative"] = bool(params.get("relative", False))
        data["start_deg"] = st0.position
        return SkillResult(skill_name="SetPolarizerAngle", success=True, data=data,
                           summary=f"polariser {dev}: {st0.position:.3f}° → {st.position:.3f}°")


class HomePolarizer(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="HomePolarizer", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(tr("让偏振片旋转台回零（KDC101 上电后必须先回零，最长约 36 s）。回零会转过参考位，"
                         "期间偏振方向在变——不要在采集中途做。",
                            "Home the polarizer rotation stage (a KDC101 must be homed after power-up; up to about 36 s). Homing passes through the reference position, "
                            "so the polarization direction changes meanwhile - do not do it in the middle of an acquisition.")),
            parameters=[
                _device_param(),
                ParameterSpec(name="timeout_s", type="float", unit="s", required=False,
                              default=90.0, min_value=1.0, max_value=600.0,
                              description=tr("回零时限", "Homing time limit")),
            ],
            estimated_duration_s=36.0, composition_level=0,
            tags=[*_TAGS, "home", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev, name, ax = _axis(params)
            refused = stage_tip_result("HomePolarizer", context, dev)
            if refused is not None:
                return refused
            st = ax.home(wait=True, timeout=float(params.get("timeout_s", 90.0)),
                         check_abort=abort_probe(context))
        except Exception as exc:  # noqa: BLE001
            return fail("HomePolarizer", exc)
        return SkillResult(skill_name="HomePolarizer", success=True,
                           data=_reading(dev, name, ax, st),
                           summary=f"polariser {dev} homed at {st.position:.3f}°")
