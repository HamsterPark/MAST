"""卫星设备的手动控制技能（契约 §13.1「手动控制面板」）。

面板上每个按钮背后都是一个已注册的技能，走 ``POST /api/skills/{name}/execute``；这里补的是
厂家软件里有、而原有技能没覆盖的那几个按钮：

* StopSatelliteDevice（AUTO，免令牌）—— 停一台卫星设备，或开环台的某一个轴；
* PicomotorAbort（AUTO，免令牌）—— Picomotor 所有轴立即停、不减速（8742 的 AB）；
* IdentifySatelliteDevice（AUTO）—— 让设备面板灯闪几下，确认是哪一台；
* GetSatelliteDeviceStatus（AUTO，只读）—— 一台卫星设备的 status（面板的轮询读数）；
* SetPolarizerVelocity（CONFIRM）—— 偏振片旋转台的速度 / 加速度；
* PicomotorMoveAbs（CONFIRM）—— 走到某个累计计数（相对 set_zero）；
* SetPicomotorVelocity（CONFIRM）—— 某一轴的步速；
* SetPicomotorAccel（CONFIRM）—— 某一轴的加速度；
* PicomotorSetZero（CONFIRM）—— 把当前位置记作 0 步。

偏振片和 Picomotor 的技能照 optics_polarization / optics_alignment 的做法找设备：
``config/optical_instruments.json`` 里 ``role: polarizer`` / ``role: beam_steer`` 的那一台，
或者显式给 ``device_id``。PicomotorMoveAbs 与 PicomotorSetZero 走设备层的开环轴
（:class:`mast.instruments.openloop.OpenLoopAxis`）：单次上限与累计软限位都在那一处强制，和
PicomotorJog 同一套。走到计数时「当前计数」每次都现读卫星（面板上显示的就是这个数），不用任何缓存：
卫星重启过、或有人在 Picomotor 软件里手动走过，差值照样按卫星的计数算。速度、加速度、立即停只有
卫星设备有，直接发卫星动作。动设备的技能（PicomotorMoveAbs）先查这台设备在 optical_instruments.json
里的 tip_requirement，与 PicomotorJog 同一个判据；只改参数、不动设备的不查。
"""

from __future__ import annotations

from typing import Any

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.instruments.registry import get_instrument_registry
from mast.skills.base import BaseSkill
from mast.skills.builtins.optics_common import (
    abort_probe,
    fail,
    json_safe,
    resolve_device,
    stage_tip_result,
)

__all__ = [
    "StopSatelliteDevice",
    "PicomotorAbort",
    "IdentifySatelliteDevice",
    "GetSatelliteDeviceStatus",
    "SetPolarizerVelocity",
    "PicomotorMoveAbs",
    "SetPicomotorVelocity",
    "SetPicomotorAccel",
    "PicomotorSetZero",
]

POLARIZER_ROLE = "polarizer"
PICOMOTOR_ROLE = "beam_steer"
#: 停类动作等卫星回话的上限：停只是「发出去」，回不回话都不该让按钮卡住。
STOP_CONFIRM_S = 2.0

_PICO_TAGS = ["optics", "picomotor", "openloop", "alignment", "manual", "optional-hardware"]
_SAT_TAGS = ["optics", "satellite", "manual", "optional-hardware"]


def _target_params(required: bool = True) -> list[ParameterSpec]:
    return [
        ParameterSpec(name="satellite", type="str", required=required,
                      default=None if required else "",
                      description=tr("卫星 id（satellites.json 里登记的 id）", "Satellite id (the id registered in satellites.json)")),
        ParameterSpec(name="device", type="str", required=required,
                      default=None if required else "",
                      description=tr("卫星上的设备 id（例如 pol、pico）", "Device id on the satellite (e.g. pol, pico)")),
    ]


def _device_id_param(role: str) -> ParameterSpec:
    return ParameterSpec(name="device_id", type="str", required=False, default="",
                         description=tr(f"设备 id（ListOpticalDevices）；留空 = 唯一一台 role={role} 的",
                                        f"Device id (ListOpticalDevices); leave empty = the only device with role={role}"))


def _endpoint(satellite: str, device: str):
    from mast.instruments.remote import RemoteEndpoint

    if not satellite or not device:
        raise InstrumentError(tr("satellite 和 device 都要给", "both satellite and device must be given"))
    return RemoteEndpoint(str(satellite), str(device))


def _registry_endpoint(kind: str, role: str, device_id: str | None):
    """``(registry device id, RemoteEndpoint)``：只认卫星设备（``satellite_*`` 类型）。"""
    reg = get_instrument_registry()
    dev = resolve_device(reg, kind=kind, role=role, device_id=device_id or None)
    entry = next((e for e in reg.find_devices(kind=kind) if str(e.get("id")) == dev), None)
    if entry is None:
        raise InstrumentUnavailable(tr(f"{dev!r} 不是启用中的 {kind} 设备（config/optical_instruments.json）",
                                       f"{dev!r} is not an enabled {kind} device (config/optical_instruments.json)"))
    if not entry.get("satellite") or not entry.get("device"):
        raise InstrumentError(tr(f"{dev!r}（type={entry.get('type')}）不是卫星设备，没有这个卫星动作",
                                 f"{dev!r} (type={entry.get('type')}) is not a satellite device and has no such satellite action"))
    return dev, _endpoint(str(entry["satellite"]), str(entry["device"]))


def _pico(params: dict):
    """``(device id, controller, axis)``。"""
    reg = get_instrument_registry()
    dev = resolve_device(reg, kind="openloop", role=PICOMOTOR_ROLE,
                         device_id=params.get("device_id") or None)
    ctrl = reg.controller(dev)
    return dev, ctrl, ctrl.axis(str(params["axis"]))


def _pico_axis(params: dict):
    dev, _ctrl, ax = _pico(params)
    return dev, ax


def _wire_axis(ax: Any) -> Any:
    """卫星那边的轴号：与设备层 RemoteOpenLoopAxis 发给卫星的同一个值。"""
    wire = getattr(ax, "_wire_axis", None)
    if callable(wire):
        return wire()
    channel = str(getattr(getattr(ax, "config", None), "channel", "") or "")
    return int(channel) if channel.isdigit() else channel


# ── 停（免令牌） ────────────────────────────────────────────────────────────


class StopSatelliteDevice(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="StopSatelliteDevice", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,   # a stop never waits for approval
            description=(
                tr("停一台卫星设备（手动面板每张卡上的 stop）。给 axis 时只停开环台的那一个轴。"
                "不需要仪器令牌（TERS 组合技能占着令牌也能停）；人工接管中的设备照样收 stop。"
                "等卫星回话最多 2 s，没回话也算已发出。它关不了激光。",
                   "Stop a satellite device (the stop on each card of the manual panel). With axis given, only that one axis of an open-loop stage is stopped. "
                   "Needs no instrument token (works even while a TERS composite skill holds the token); a device under manual takeover still accepts stop. "
                   "Waits up to 2 s for the satellite to reply; it counts as sent even with no reply. It cannot turn off the laser.")),
            parameters=[*_target_params(),
                        ParameterSpec(name="axis", type="str", required=False, default="",
                                      description=tr("只停这一个轴（开环台的轴号）；留空 = 整台设备",
                                                     "Stop only this axis (axis number of an open-loop stage); empty = the whole device"))],
            estimated_duration_s=0.5, composition_level=0,
            tags=["optics", "satellite", "stop", "safety", "manual", "optional-hardware"])

    def execute(self, context, params: dict) -> SkillResult:
        from mast.instruments.remote import get_hub, map_hub_error

        sat, dev = str(params.get("satellite") or ""), str(params.get("device") or "")
        axis = str(params.get("axis") or "")
        args = {"axis": int(axis) if axis.isdigit() else axis} if axis else {}
        target = f"{sat}/{dev}" + (f" axis {axis}" if axis else "")
        try:
            cmd = get_hub().submit(sat, dev, "stop", args, timeout_s=10.0)
        except Exception as exc:  # noqa: BLE001
            return fail("StopSatelliteDevice", map_hub_error(exc, where=f"{target} stop"),
                        satellite=sat, device=dev)
        confirmed = False
        try:
            cmd.wait(timeout_s=STOP_CONFIRM_S)
            confirmed = True
        except Exception as exc:  # noqa: BLE001 — 已发出：没回话只是信息，拒收才是失败
            if type(exc).__name__ != "SatelliteTimeout":
                # 例如真 xyz：Motion_Manipulator 没有远程停止，卫星回 not_supported —— 必须让人看到
                return fail("StopSatelliteDevice", map_hub_error(exc, where=f"{target} stop"),
                            satellite=sat, device=dev)
        return SkillResult(skill_name="StopSatelliteDevice", success=True,
                           data={"satellite": sat, "device": dev, "axis": axis or None,
                                 "confirmed": confirmed},
                           summary=f"stop → {target}" + ("" if confirmed else tr("（已发出，卫星还没回话）",
                                                                                 " (sent; the satellite has not replied yet)")))


class PicomotorAbort(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="PicomotorAbort", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("Picomotor 所有轴立即停、不减速（8742 的 abort）。不需要仪器令牌。卫星驱动不支持 abort 时"
                "改发普通 stop，并在结果里写明。",
                   "Picomotor: stop all axes immediately without deceleration (abort of the 8742). Needs no instrument token. If the satellite driver does not support abort, "
                   "a plain stop is sent instead and the result says so.")),
            parameters=[_device_id_param(PICOMOTOR_ROLE)],
            estimated_duration_s=0.5, composition_level=0,
            tags=[*_PICO_TAGS, "stop", "safety"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev, ep = _registry_endpoint("openloop", PICOMOTOR_ROLE, params.get("device_id"))
        except Exception as exc:  # noqa: BLE001
            return fail("PicomotorAbort", exc)
        try:
            st = ep.call("abort", {}, timeout_s=5.0)
            how = "abort"
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "remote_code", None) != "not_supported":
                return fail("PicomotorAbort", exc, device_id=dev)
            ep.stop()
            st, how = None, tr("stop（卫星驱动不支持 abort）", "stop (satellite driver does not support abort)")
        return SkillResult(skill_name="PicomotorAbort", success=True,
                           data={"device_id": dev, "via": how, "status": json_safe(st)},
                           summary=f"picomotor {dev}: {how}")


# ── 识别与读数 ──────────────────────────────────────────────────────────────


class IdentifySatelliteDevice(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="IdentifySatelliteDevice", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=tr("让一台卫星设备的面板灯闪几下（Kinesis 的 Identify），确认台面上是哪一台。不动轴。",
                           "Make the panel light of a satellite device blink a few times (Kinesis Identify) to confirm which unit it is on the bench. Does not move any axis."),
            parameters=_target_params(),
            estimated_duration_s=1.0, composition_level=0,
            tags=[*_SAT_TAGS, "identify"])

    def execute(self, context, params: dict) -> SkillResult:
        sat, dev = str(params.get("satellite") or ""), str(params.get("device") or "")
        try:
            st = _endpoint(sat, dev).call("identify", {}, timeout_s=10.0)
        except Exception as exc:  # noqa: BLE001
            return fail("IdentifySatelliteDevice", exc, satellite=sat, device=dev)
        return SkillResult(skill_name="IdentifySatelliteDevice", success=True,
                           data={"satellite": sat, "device": dev, "status": json_safe(st)},
                           summary=f"{sat}/{dev}: identify")


class GetSatelliteDeviceStatus(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSatelliteDeviceStatus", version="1.0.0", category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(tr("读一台卫星设备的 status（卫星当场问驱动，不是缓存）。只读，不需要仪器令牌；"
                         "人工接管中的设备照样能读。",
                            "Read the status of a satellite device (the satellite asks the driver on the spot, not a cache). Read-only, needs no instrument token; "
                            "a device under manual takeover can still be read.")),
            parameters=_target_params(),
            estimated_duration_s=0.3, composition_level=0,
            tags=[*_SAT_TAGS, "read", "status"])

    def execute(self, context, params: dict) -> SkillResult:
        sat, dev = str(params.get("satellite") or ""), str(params.get("device") or "")
        try:
            st = _endpoint(sat, dev).call("status", {}, timeout_s=5.0)
        except Exception as exc:  # noqa: BLE001
            return fail("GetSatelliteDeviceStatus", exc, satellite=sat, device=dev)
        state = st.get("state") if isinstance(st, dict) else None
        return SkillResult(skill_name="GetSatelliteDeviceStatus", success=True,
                           data={"satellite": sat, "device": dev, "status": json_safe(st)},
                           summary=f"{sat}/{dev}: {state or 'status read'}")


# ── 偏振片 ──────────────────────────────────────────────────────────────────


class SetPolarizerVelocity(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetPolarizerVelocity", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(tr("设偏振片旋转台（KDC101 + PRM1-Z8）的最大速度和加速度，下一次转动起生效。"
                         "超出驱动的上限由卫星拒绝（invalid_args），不会被截到边界。",
                            "Set the maximum velocity and acceleration of the polarizer rotation stage (KDC101 + PRM1-Z8), effective from the next rotation. "
                            "Values beyond the driver's limits are rejected by the satellite (invalid_args), not clamped.")),
            parameters=[
                _device_id_param(POLARIZER_ROLE),
                ParameterSpec(name="deg_s", type="float", unit="deg/s", required=True,
                              min_value=0.01, max_value=25.0, description=tr("最大速度", "Maximum velocity")),
                ParameterSpec(name="accel_deg_s2", type="float", unit="deg/s²", required=False,
                              min_value=0.01, max_value=25.0, description=tr("加速度；留空 = 不改", "Acceleration; empty = unchanged")),
            ],
            estimated_duration_s=0.5, composition_level=0,
            tags=["optics", "polarizer", "rotation", "manual", "write", "optional-hardware"])

    def execute(self, context, params: dict) -> SkillResult:
        args: dict[str, Any] = {"deg_s": float(params["deg_s"])}
        if params.get("accel_deg_s2") is not None:
            args["accel_deg_s2"] = float(params["accel_deg_s2"])
        try:
            dev, ep = _registry_endpoint("rotation", POLARIZER_ROLE, params.get("device_id"))
            st = ep.call("set_velocity", args, timeout_s=10.0)
        except Exception as exc:  # noqa: BLE001
            return fail("SetPolarizerVelocity", exc)
        return SkillResult(skill_name="SetPolarizerVelocity", success=True,
                           data={"device_id": dev, "requested": args, "status": json_safe(st)},
                           summary=f"polariser {dev}: {args['deg_s']:g} deg/s")


# ── Picomotor ───────────────────────────────────────────────────────────────


class PicomotorMoveAbs(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="PicomotorMoveAbs", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("Picomotor 某一轴走到累计计数 steps（相对上一次 PicomotorSetZero）。要走的步数超过单次上限、"
                "或目标越出累计软限位时直接拒绝，不拆、不截（与 PicomotorJog 同一套限制）。开环：计数是命令过的"
                "步数之和，不是位置反馈。",
                   "Move one Picomotor axis to accumulated count `steps` (relative to the last PicomotorSetZero). If the number of steps needed exceeds the per-command limit, "
                   "or the target is outside the accumulated soft limits, it is refused outright, not split or clamped (same limits as PicomotorJog). Open loop: the count is the sum of "
                   "commanded steps, not position feedback.")),
            parameters=[
                _device_id_param(PICOMOTOR_ROLE),
                ParameterSpec(name="axis", type="str", required=True,
                              description=tr("轴名（GetPicomotorSteps 列出的）", "Axis name (as listed by GetPicomotorSteps)")),
                ParameterSpec(name="steps", type="int", required=True,
                              min_value=-1_000_000, max_value=1_000_000,
                              description=tr("目标累计计数", "Target accumulated count")),
                ParameterSpec(name="timeout_s", type="float", unit="s", required=False,
                              default=30.0, min_value=0.5, max_value=600.0, description=tr("动作时限", "Action time limit")),
            ],
            estimated_duration_s=2.0, composition_level=0,
            tags=[*_PICO_TAGS, "move", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev, ctrl, ax = _pico(params)
            # 这台设备在 optical_instruments.json 里的 tip_requirement（面板卡上显示的就是它）
            refused = stage_tip_result("PicomotorMoveAbs", context, dev)
            if refused is not None:
                return refused
            # 差值按卫星**此刻**的计数算：远程控制器的状态有 0.15 s 的复用窗口，这里先强制现读一次，
            # 后面 move_abs 里两次取计数都落在这份新读数上
            refresh = getattr(ctrl, "status", None)
            if callable(refresh):
                refresh(max_age_s=0.0)
            before = ax.get_steps()
            st = ax.move_abs(int(params["steps"]), wait=True,
                             timeout=float(params.get("timeout_s", 30.0)),
                             check_abort=abort_probe(context))
            after = ax.get_steps()
        except Exception as exc:  # noqa: BLE001
            return fail("PicomotorMoveAbs", exc)
        return SkillResult(
            skill_name="PicomotorMoveAbs", success=True,
            data={"device_id": dev, "axis": str(params["axis"]), "target": int(params["steps"]),
                  "counter_before": before, "counter_after": after, "moving": st.moving,
                  "feedback": False},
            summary=f"picomotor {dev}/{params['axis']}: {before} → {after} steps (open loop)")


class SetPicomotorVelocity(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetPicomotorVelocity", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设 Picomotor 某一轴的步速（步/秒，8742 允许 1–2000），下一次走步起生效。",
                           "Set the step rate of one Picomotor axis (steps/s, the 8742 allows 1-2000), effective from the next move."),
            parameters=[
                _device_id_param(PICOMOTOR_ROLE),
                ParameterSpec(name="axis", type="str", required=True,
                              description=tr("轴名（GetPicomotorSteps 列出的）", "Axis name (as listed by GetPicomotorSteps)")),
                ParameterSpec(name="steps_s", type="int", unit="steps/s", required=True,
                              min_value=1, max_value=2000, description=tr("步速", "Step rate")),
            ],
            estimated_duration_s=0.5, composition_level=0,
            tags=[*_PICO_TAGS, "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev, ax = _pico_axis(params)
            _dev, ep = _registry_endpoint("openloop", PICOMOTOR_ROLE, dev)
            st = ep.call("set_velocity", {"axis": _wire_axis(ax), "steps_s": int(params["steps_s"])},
                         timeout_s=10.0)
        except Exception as exc:  # noqa: BLE001
            return fail("SetPicomotorVelocity", exc)
        return SkillResult(skill_name="SetPicomotorVelocity", success=True,
                           data={"device_id": dev, "axis": str(params["axis"]),
                                 "steps_s": int(params["steps_s"]), "status": json_safe(st)},
                           summary=f"picomotor {dev}/{params['axis']}: {int(params['steps_s'])} steps/s")


class SetPicomotorAccel(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetPicomotorAccel", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设 Picomotor 某一轴的加速度（步/秒²，8742 允许 1–200000），下一次走步起生效。",
                           "Set the acceleration of one Picomotor axis (steps/s², the 8742 allows 1-200000), effective from the next move."),
            parameters=[
                _device_id_param(PICOMOTOR_ROLE),
                ParameterSpec(name="axis", type="str", required=True,
                              description=tr("轴名（GetPicomotorSteps 列出的）", "Axis name (as listed by GetPicomotorSteps)")),
                ParameterSpec(name="steps_s2", type="int", unit="steps/s²", required=True,
                              min_value=1, max_value=200_000, description=tr("加速度", "Acceleration")),
            ],
            estimated_duration_s=0.5, composition_level=0,
            tags=[*_PICO_TAGS, "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev, ax = _pico_axis(params)
            _dev, ep = _registry_endpoint("openloop", PICOMOTOR_ROLE, dev)
            st = ep.call("set_accel", {"axis": _wire_axis(ax), "steps_s2": int(params["steps_s2"])},
                         timeout_s=10.0)
        except Exception as exc:  # noqa: BLE001
            return fail("SetPicomotorAccel", exc)
        return SkillResult(skill_name="SetPicomotorAccel", success=True,
                           data={"device_id": dev, "axis": str(params["axis"]),
                                 "steps_s2": int(params["steps_s2"]), "status": json_safe(st)},
                           summary=f"picomotor {dev}/{params['axis']}: {int(params['steps_s2'])} steps/s²")


class PicomotorSetZero(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="PicomotorSetZero", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(tr("把 Picomotor 某一轴的当前位置记作 0 步（不动电机）。之后的累计计数和软限位都从这里算，"
                         "清零前的计数不再有意义。",
                            "Record the current position of one Picomotor axis as 0 steps (the motor does not move). Accumulated counts and soft limits are counted from here afterwards; "
                            "counts from before the reset are no longer meaningful.")),
            parameters=[
                _device_id_param(PICOMOTOR_ROLE),
                ParameterSpec(name="axis", type="str", required=True,
                              description=tr("轴名（GetPicomotorSteps 列出的）", "Axis name (as listed by GetPicomotorSteps)")),
            ],
            estimated_duration_s=0.5, composition_level=0,
            tags=[*_PICO_TAGS, "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev, ax = _pico_axis(params)
            before = ax.get_steps()
            ax.set_zero()
            after = ax.get_steps()
        except Exception as exc:  # noqa: BLE001
            return fail("PicomotorSetZero", exc)
        return SkillResult(skill_name="PicomotorSetZero", success=True,
                           data={"device_id": dev, "axis": str(params["axis"]),
                                 "counter_before": before, "counter_after": after},
                           summary=f"picomotor {dev}/{params['axis']}: counter {before} → {after}")
