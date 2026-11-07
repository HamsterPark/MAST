"""Optical-bench stage skills — TERS/THz motion hardware (non-Nanonis).

12 skills: ListOpticalDevices, OpticalStageGetPos, OpticalStageMove,
           OpticalStageWiggle, StopOpticalStage, HomeOpticalStage,
           DelayLineGetDelay, DelayLineMoveTo, and for the manual control panel
           (contract §13.1) GetOpticalStageStatus, SetOpticalStageVelocity,
           OpticalStageServo, DisconnectOpticalDevice.

Hardware access goes through the module-level
:func:`mast.instruments.registry.get_instrument_registry` singleton (the
plan_overlay pattern) — NOT through ExecutionContext, which is the Nanonis
path. Soft travel limits are enforced inside the drivers (Layer-0), so
these skills only translate errors into SkillResults; they cannot bypass
the limits even with hallucinated parameters.

Tip state: every skill that moves a device checks that device's
``tip_requirement`` (``optical_instruments.json``; absent = ``none``, which is
what these skills did before the field existed) — see
:func:`optics_common.stage_tip_refusal`.

On machines without the optical bench every skill degrades to a failed
SkillResult with a clear message — never an exception (UI 绝不冻结 /
no-hardware-no-crash).
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
from mast.instruments.base import (
    InstrumentError,
    InstrumentUnavailable,
    MotionAborted,
    MotionTimeout,
    OperationNotSupported,
    TravelLimitError,
)
from mast.instruments.registry import get_instrument_registry
from mast.skills.base import BaseSkill
from mast.skills.builtins.optics_common import abort_probe, stage_tip_result

__all__ = [
    "ListOpticalDevices",
    "OpticalStageGetPos",
    "OpticalStageMove",
    "OpticalStageWiggle",
    "StopOpticalStage",
    "HomeOpticalStage",
    "DelayLineGetDelay",
    "DelayLineMoveTo",
    "GetOpticalStageStatus",
    "SetOpticalStageVelocity",
    "OpticalStageServo",
    "DisconnectOpticalDevice",
]


def _fail(skill: str, exc: Exception) -> SkillResult:
    """Uniform failure translation for the driver exception family."""
    if isinstance(exc, MotionAborted):
        msg = f"aborted: {exc}. Stop and report; do not retry."
    elif isinstance(exc, InstrumentUnavailable):
        msg = f"optical hardware unavailable: {exc}"
    elif isinstance(exc, TravelLimitError):
        msg = f"refused by soft travel limit: {exc}"
    elif isinstance(exc, MotionTimeout):
        msg = f"motion timeout: {exc}"
    elif isinstance(exc, OperationNotSupported):
        msg = tr(f"not supported: {exc} —— 驱动没有文档化的做法，这不是参数问题，不要重试。",
                 f"not supported: {exc} - the driver has no documented way to do this; this is not a parameter problem, do not retry.")
    elif isinstance(exc, InstrumentError):
        msg = str(exc)
    else:
        msg = f"{type(exc).__name__}: {exc}"
    return SkillResult(skill_name=skill, success=False, error=msg)


def _tip_refusal(skill: str, context, *device_ids) -> SkillResult | None:
    """The moved devices' ``tip_requirement`` (absent = none = no check)."""
    return stage_tip_result(skill, context, *device_ids)


class ListOpticalDevices(BaseSkill):
    """List configured optical-bench devices (stages, delay line)."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ListOpticalDevices",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("从 config/optical_instruments.json 列出光学平台上的运动设备："
                "id、类型、各轴（含行程限位与角色）、连接状态，以及（若已配置）泵浦-探测延时线的绑定关系。"
                "先从这里开始，才知道其余光学技能接受哪些 device_id/axis 取值。只读配置，不打开任何设备。",
                   "List the motion devices on the optical table from config/optical_instruments.json: "
                   "id, type, axes (with travel limits and roles), connection state, and (if configured) the pump-probe delay-line binding. "
                   "Start here to learn which device_id/axis values the other optics skills accept. Reads the config only; does not open any device.")
            ),
            estimated_duration_s=0.1,
            composition_level=0,
            tags=["optics", "inventory", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        try:
            reg = get_instrument_registry()
            devices = reg.list_devices()
            delay_cfg = None
            try:
                # config only: an inventory must not open the delay stage (it used
                # to, through delay_line(), every time the optics page refreshed)
                delay_cfg = reg.delay_line_info()
            except Exception:  # noqa: BLE001 - misconfigured binding: the inventory still helps
                pass
            return SkillResult(
                skill_name="ListOpticalDevices",
                success=True,
                data={"devices": devices, "delay_line": delay_cfg},
                summary=(
                    f"{len(devices)} optical device(s) configured"
                    + (", delay line bound" if delay_cfg else ", no delay line")
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("ListOpticalDevices", exc)


class OpticalStageGetPos(BaseSkill):
    """Read the position of one optical stage axis."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="OpticalStageGetPos",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("读一条光学平台台子轴的当前位置（原生单位；单位与限位见 ListOpticalDevices）"
                "。",
                   "Read the current position of one optical-table stage axis (native unit; see ListOpticalDevices for units and limits)"
                   ".")
            ),
            parameters=[
                ParameterSpec(
                    name="device_id",
                    type="str",
                    description=tr("设备 id，取自 ListOpticalDevices", "Device id, from ListOpticalDevices"),
                    required=True,
                ),
                ParameterSpec(
                    name="axis",
                    type="str",
                    description=tr("该设备上的轴名", "Axis name on that device"),
                    required=True,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["optics", "stage", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        try:
            ax = get_instrument_registry().axis(params["device_id"], params["axis"])
            pos = ax.get_position()
            return SkillResult(
                skill_name="OpticalStageGetPos",
                success=True,
                data={
                    "device_id": params["device_id"],
                    "axis": params["axis"],
                    "position": pos,
                    "unit": ax.config.unit,
                    "limits": [ax.config.min_pos, ax.config.max_pos],
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("OpticalStageGetPos", exc)


class OpticalStageMove(BaseSkill):
    """Move one optical stage axis (absolute or relative)."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="OpticalStageMove",
            version="1.0.0",
            category=SkillCategory.WRITE,
            # CONFIRM: optical alignment moves are recoverable (soft limits
            # bound the travel) but a wrong large move loses tip-focus
            # alignment, so keep an operator/LLM confirmation in the loop.
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("把一条光学平台台子轴移动到某个位置（原生单位）。软行程限位由驱动强制执行，超出范围的目标在任何运动发生之前就会被拒绝。"
                "要做相对位移就用 relative=true。",
                   "Move one optical-table stage axis to a position (native unit). Soft travel limits are enforced by the driver, and out-of-range targets are rejected before any motion happens. "
                   "Use relative=true for a relative displacement.")
            ),
            parameters=[
                ParameterSpec(
                    name="device_id",
                    type="str",
                    description=tr("设备 id，取自 ListOpticalDevices", "Device id, from ListOpticalDevices"),
                    required=True,
                ),
                ParameterSpec(
                    name="axis",
                    type="str",
                    description=tr("该设备上的轴名", "Axis name on that device"),
                    required=True,
                ),
                ParameterSpec(
                    name="position",
                    type="float",
                    description=(
                        tr("目标位置（绝对）或位移量（相对），用该轴的原生单位",
                           "Target position (absolute) or displacement (relative), in the axis's native unit")
                    ),
                    required=True,
                ),
                ParameterSpec(
                    name="relative",
                    type="bool",
                    description=tr("True = 从当前位置按位移量移动", "True = move by the displacement from the current position"),
                    required=False,
                    default=False,
                ),
                ParameterSpec(
                    name="wait",
                    type="bool",
                    description=tr("阻塞直到到位（推荐）", "Block until in position (recommended)"),
                    required=False,
                    default=True,
                ),
                ParameterSpec(
                    name="timeout_s",
                    type="float",
                    description=tr("运动时限，单位秒", "Motion time limit, in seconds"),
                    required=False,
                    default=60.0,
                    min_value=0.1,
                    max_value=600.0,
                ),
            ],
            estimated_duration_s=5.0,
            composition_level=0,
            tags=["optics", "stage", "move", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        refused = _tip_refusal("OpticalStageMove", context, params["device_id"])
        if refused is not None:
            return refused
        try:
            ax = get_instrument_registry().axis(params["device_id"], params["axis"])
            wait = params.get("wait", True)
            timeout = params.get("timeout_s", 60.0)
            probe = abort_probe(context)
            if params.get("relative", False):
                status = ax.move_rel(params["position"], wait=wait, timeout=timeout,
                                     check_abort=probe)
            else:
                status = ax.move_abs(params["position"], wait=wait, timeout=timeout,
                                     check_abort=probe)
            return SkillResult(
                skill_name="OpticalStageMove",
                success=True,
                data={
                    "device_id": params["device_id"],
                    "axis": params["axis"],
                    "position": status.position,
                    "unit": ax.config.unit,
                    "on_target": status.on_target,
                    "moving": status.moving,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("OpticalStageMove", exc)


class OpticalStageWiggle(BaseSkill):
    """Bring-up self-test: nudge an axis a little and verify it moves.

    Answers the first question at the instrument: "is this axis wired,
    alive, and moving in the direction/scale I think?" — WITHOUT touching
    the STM. Reads the position, moves by a small delta, reads back, then
    returns to the start. Reports observed vs commanded displacement so a
    dead axis (no motion), a reversed encoder (wrong sign), or a wrong
    ``counts_per_unit`` scale (ratio far from 1) each show up on the spot.
    Near a soft limit it automatically wiggles the other way.
    """

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="OpticalStageWiggle",
            version="1.0.0",
            category=SkillCategory.WRITE,
            # CONFIRM: it moves hardware. Small and self-reversing, but motion
            # is motion — the operator/LLM confirms. During bring-up the human
            # is at the rig, so one confirmation per test is the right friction.
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("对一条光学台轴做诊断性抖动：按一个小位移量走过去再走回来，然后报告它到底动没动、往哪个方向动、"
                "以及实测量／指令量之比（标度核查）。装机调试期间用它确认某条轴是活的、配置也对，再去信任更大的移动。"
                "在标度得到确认之前，先从很小的位移量起步。",
                   "Diagnostic wiggle of one optical stage axis: move by a small displacement and back, then report whether it actually moved, in which direction, "
                   "and the measured/commanded ratio (scale check). Use it during commissioning to confirm an axis is alive and configured correctly before trusting larger moves. "
                   "Until the scale is confirmed, start from a very small displacement.")
            ),
            parameters=[
                ParameterSpec(
                    name="device_id",
                    type="str",
                    description=tr("设备 id，取自 ListOpticalDevices", "Device id, from ListOpticalDevices"),
                    required=True,
                ),
                ParameterSpec(
                    name="axis",
                    type="str",
                    description=tr("该设备上的轴名", "Axis name on that device"),
                    required=True,
                ),
                ParameterSpec(
                    name="delta",
                    type="float",
                    description=(
                        tr("轻推的幅度，用该轴的原生单位（标度确认之前请保持很小）",
                           "Size of the nudge, in the axis's native unit (keep it small until the scale is confirmed)")
                    ),
                    required=True,
                ),
                ParameterSpec(
                    name="tolerance",
                    type="float",
                    description=(
                        tr("算作「动了」所需的最小 |位移|；默认 = |delta| 的 20%",
                           "Minimum |displacement| required to count as 'moved'; default = 20% of |delta|")
                    ),
                    required=False,
                    default=0.0,
                    min_value=0.0,
                ),
                ParameterSpec(
                    name="return_to_start",
                    type="bool",
                    description=tr("结束后移回起始位置", "Move back to the starting position afterwards"),
                    required=False,
                    default=True,
                ),
                ParameterSpec(
                    name="timeout_s",
                    type="float",
                    description=tr("每次移动的时限，单位秒", "Time limit per move, in seconds"),
                    required=False,
                    default=30.0,
                    min_value=0.1,
                    max_value=600.0,
                ),
            ],
            estimated_duration_s=5.0,
            composition_level=0,
            tags=["optics", "stage", "diagnostic", "selftest", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        refused = _tip_refusal("OpticalStageWiggle", context, params["device_id"])
        if refused is not None:
            return refused
        try:
            ax = get_instrument_registry().axis(params["device_id"], params["axis"])
            delta = float(params["delta"])
            if delta == 0.0:
                return SkillResult(
                    skill_name="OpticalStageWiggle",
                    success=False,
                    error="delta must be non-zero",
                )
            tol = params.get("tolerance") or 0.2 * abs(delta)
            timeout = params.get("timeout_s", 30.0)
            probe = abort_probe(context)

            start = ax.get_position()

            # Move forward by delta; if that would cross a soft limit (axis is
            # parked at/near an end), wiggle the other way instead.
            used = delta
            try:
                fwd = ax.move_abs(start + delta, wait=True, timeout=timeout,
                                  check_abort=probe).position
            except TravelLimitError:
                used = -delta
                try:
                    fwd = ax.move_abs(start - delta, wait=True, timeout=timeout,
                                      check_abort=probe).position
                except TravelLimitError:
                    return SkillResult(
                        skill_name="OpticalStageWiggle",
                        success=False,
                        error=(
                            f"axis at {start:g} {ax.config.unit} cannot move ±"
                            f"{abs(delta):g} without crossing its soft limits "
                            f"[{ax.config.min_pos}, {ax.config.max_pos}] — "
                            "use a smaller delta or move off the limit first"
                        ),
                    )

            observed = fwd - start
            moved = abs(observed) >= tol
            direction_ok = (observed > 0) == (used > 0) if moved else None
            scale_ratio = observed / used if used else float("nan")

            # Return to start (best-effort; a failed return leaves a known,
            # small offset which we report rather than hide).
            final = fwd
            returned_to_start: bool | None = None
            if params.get("return_to_start", True):
                try:
                    final = ax.move_abs(start, wait=True, timeout=timeout,
                                        check_abort=probe).position
                    returned_to_start = abs(final - start) <= max(tol, 1e-9)
                except Exception:  # noqa: BLE001
                    returned_to_start = False

            if moved and direction_ok:
                summary = (
                    f"{params['axis']} moved {observed:+.4g} {ax.config.unit} "
                    f"(commanded {used:+.4g}, ratio {scale_ratio:.3f}) — alive"
                )
            elif moved and not direction_ok:
                summary = (
                    f"{params['axis']} moved {observed:+.4g} {ax.config.unit} "
                    f"but OPPOSITE to commanded {used:+.4g} — encoder/sign reversed"
                )
            else:
                summary = (
                    f"{params['axis']} did NOT move (Δ={observed:+.4g} "
                    f"{ax.config.unit} < tol {tol:g}) — check wiring/power/config"
                )

            return SkillResult(
                skill_name="OpticalStageWiggle",
                success=moved,
                data={
                    "device_id": params["device_id"],
                    "axis": params["axis"],
                    "unit": ax.config.unit,
                    "commanded_delta": used,
                    "start_position": start,
                    "forward_position": fwd,
                    "observed_delta": observed,
                    "moved": moved,
                    "direction_ok": direction_ok,
                    "scale_ratio": scale_ratio,
                    "final_position": final,
                    "returned_to_start": returned_to_start,
                },
                error="" if moved else "axis did not move within tolerance",
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("OpticalStageWiggle", exc)


class StopOpticalStage(BaseSkill):
    """Panic-stop optical stage motion (one device or the whole bench)."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="StopOpticalStage",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,  # a stop must never wait for approval
            description=(
                tr("立即停止光学台的运动。不传 device_id 则对每一台已连接的光学设备做急停。"
                "只停得了 MAST 自己打开着的设备：没打开（或未启用）的设备回 open=false —— "
                "它若还在动，是别的程序在驱动，要在那个程序里停。",
                   "Stop optical-table motion immediately. Without device_id, an emergency stop is issued to every connected optical device. "
                   "It can only stop devices that MAST itself has open: a device that is not open (or not enabled) returns open=false - "
                   "if it is still moving, another program is driving it and it must be stopped in that program.")
            ),
            parameters=[
                ParameterSpec(
                    name="device_id",
                    type="str",
                    description=tr("要停的设备；不传则停**所有**设备", "Device to stop; if omitted, stops **all** devices"),
                    required=False,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["optics", "stage", "stop", "safety"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        try:
            reg = get_instrument_registry()
            device_id = params.get("device_id")
            report: dict = {}
            if device_id:
                lookup = getattr(reg, "device_config", None)
                entry = lookup(device_id) if callable(lookup) else None
                disabled = entry is not None and not entry.get("enabled", True)
                # building the controller object never touches hardware; an unknown
                # id raises here as before
                ctrl = None if disabled else reg.controller(device_id)
                if ctrl is None or not ctrl.is_connected:
                    # nothing of MAST's is moving it — and saying "stopped" here would
                    # be false comfort if another program is driving the stage
                    return SkillResult(
                        skill_name="StopOpticalStage", success=True,
                        data={"stopped": None, "device_id": device_id, "open": False,
                              "enabled": not disabled},
                        summary=(tr(f"{device_id}：MAST 没有打开它{'（未启用）' if disabled else ''}，"
                                 "没有 MAST 发出的运动可停 —— 它若还在动，是别的程序（厂家软件、"
                                 "LabVIEW）在驱动，要在那个程序里停",
                                    f"{device_id}: MAST has not opened it{' (disabled)' if disabled else ''}, "
                                    "so there is no MAST-issued motion to stop - if it is still moving, another program (vendor software, "
                                    "LabVIEW) is driving it and it must be stopped in that program")))
                ctrl.stop_all()
                scope = device_id
            else:
                # the whole station: registry.stop_all counts it as an external stop on every station device
                # first, so a protocol running there ends cleanly instead of opening its shutter again
                owner = str(getattr(context, "owner", "") or "").strip() or "unknown caller"
                report = reg.stop_all(reason=f"StopOpticalStage ({owner})") or {}
                scope = "all"
            return SkillResult(
                skill_name="StopOpticalStage",
                success=True,
                data={"stopped": scope, **({"open": True} if device_id else {}),
                      **({"report": report} if report else {})},
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("StopOpticalStage", exc)


class HomeOpticalStage(BaseSkill):
    """Home / find-zero one optical stage axis."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="HomeOpticalStage",
            version="1.0.0",
            category=SkillCategory.WRITE,
            # CONFIRM: homing may sweep the travel or re-zero the counter —
            # never run it while an experiment depends on the current alignment.
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("给一条光学台轴回参考 / 置零 —— 各驱动做的事不一样："
                "PI E-861 发 FRF，台子**走到参考开关**，位置置为参考值（可能扫过大半行程）；"
                "泽攸 PZTC 发编码器找零（enc_look_zero），控制器**带着台子去找编码器 Z 零位**"
                "（方向与距离由控制器决定；计数是否在零位清零取决于控制器的 enc_useZ，MAST 不改）；"
                "Thorlabs PDXC2 的 Home 只把**当前位置记为 0、台子不动**，软限位随之平移；"
                "PI E-816 有绝对位置传感器，没有回参考这回事。"
                "当前坐标会丢 —— 请先确认没有测量依赖它。停止（StopOpticalStage）也会中止找零。",
                   "Reference / zero one optical-table stage axis - each driver does something different: "
                   "the PI E-861 sends FRF, the stage **travels to the reference switch** and the position is set to the reference value (it may sweep most of the travel); "
                   "the Zeyou PZTC sends encoder zero-finding (enc_look_zero), and the controller **takes the stage to find the encoder Z zero** "
                   "(direction and distance are decided by the controller; whether the count is cleared at the zero depends on the controller's enc_useZ, which MAST does not change); "
                   "for the Thorlabs PDXC2, Home only **records the current position as 0 and the stage does not move**, and the soft limits shift accordingly; "
                   "the PI E-816 has an absolute position sensor and has no referencing. "
                   "The current coordinates are lost - make sure no measurement depends on them first. Stop (StopOpticalStage) also aborts zero-finding.")
            ),
            parameters=[
                ParameterSpec(
                    name="device_id",
                    type="str",
                    description=tr("设备 id，取自 ListOpticalDevices", "Device id, from ListOpticalDevices"),
                    required=True,
                ),
                ParameterSpec(
                    name="axis",
                    type="str",
                    description=tr("该设备上的轴名", "Axis name on that device"),
                    required=True,
                ),
                ParameterSpec(
                    name="timeout_s",
                    type="float",
                    description=tr("寻零时限，单位秒", "Zero-finding time limit, in seconds"),
                    required=False,
                    default=120.0,
                    min_value=1.0,
                    max_value=600.0,
                ),
            ],
            estimated_duration_s=30.0,
            composition_level=0,
            tags=["optics", "stage", "home", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        refused = _tip_refusal("HomeOpticalStage", context, params["device_id"])
        if refused is not None:
            return refused
        try:
            ax = get_instrument_registry().axis(params["device_id"], params["axis"])
            status = ax.home(wait=True, timeout=params.get("timeout_s", 120.0),
                             check_abort=abort_probe(context))
            return SkillResult(
                skill_name="HomeOpticalStage",
                success=True,
                data={
                    "device_id": params["device_id"],
                    "axis": params["axis"],
                    "position": status.position,
                    "unit": ax.config.unit,
                    "homed": status.homed,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("HomeOpticalStage", exc)


class DelayLineGetDelay(BaseSkill):
    """Read the pump-probe optical delay."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="DelayLineGetDelay",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("读当前泵浦-探测光学延时，单位皮秒（并给出由台子行程决定的可达延时范围）。",
                   "Read the current pump-probe optical delay in picoseconds (and give the reachable delay range determined by the stage travel).")
            ),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["optics", "delay_line", "pump_probe", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dl = get_instrument_registry().delay_line()
            delay = dl.get_delay_ps()
            lo, hi = dl.delay_range_ps
            return SkillResult(
                skill_name="DelayLineGetDelay",
                success=True,
                data={
                    "delay_ps": delay,
                    "delay_range_ps": [lo, hi],
                    "stage_position": dl.axis.get_position(),
                    "stage_unit": dl.axis.config.unit,
                },
                summary=f"delay = {delay:.3f} ps (range {lo:.1f} … {hi:.1f} ps)",
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("DelayLineGetDelay", exc)


class DelayLineMoveTo(BaseSkill):
    """Move the pump-probe delay line to an optical delay (ps)."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="DelayLineMoveTo",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("把泵浦-探测延时线移到某个目标光学延时，单位皮秒（经标定过的零点偏移换算成台子位置；台子的软限位由驱动强制执行）"
                "。",
                   "Move the pump-probe delay line to a target optical delay in picoseconds (converted to a stage position via the calibrated zero offset; the stage's soft limits are enforced by the driver)"
                   ".")
            ),
            parameters=[
                ParameterSpec(
                    name="delay_ps",
                    type="float",
                    description=tr("目标光学延时，单位皮秒", "Target optical delay, in picoseconds"),
                    unit="ps",
                    required=True,
                ),
                ParameterSpec(
                    name="wait",
                    type="bool",
                    description=tr("阻塞直到到位（推荐）", "Block until in position (recommended)"),
                    required=False,
                    default=True,
                ),
                ParameterSpec(
                    name="timeout_s",
                    type="float",
                    description=tr("运动时限，单位秒", "Motion time limit, in seconds"),
                    required=False,
                    default=60.0,
                    min_value=0.1,
                    max_value=600.0,
                ),
            ],
            estimated_duration_s=3.0,
            composition_level=0,
            tags=["optics", "delay_line", "pump_probe", "move", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dl = get_instrument_registry().delay_line()
            refused = _tip_refusal("DelayLineMoveTo", context, dl.config.device_id)
            if refused is not None:
                return refused
            lo, hi = dl.delay_range_ps
            target = params["delay_ps"]
            if not (lo <= target <= hi):
                return SkillResult(
                    skill_name="DelayLineMoveTo",
                    success=False,
                    error=(
                        f"delay {target} ps outside reachable range "
                        f"[{lo:.2f}, {hi:.2f}] ps (stage travel limits)"
                    ),
                )
            status = dl.move_to_delay_ps(
                target,
                wait=params.get("wait", True),
                timeout=params.get("timeout_s", 60.0),
                check_abort=abort_probe(context),
            )
            actual = dl.position_to_delay(status.position)
            return SkillResult(
                skill_name="DelayLineMoveTo",
                success=True,
                data={
                    "delay_ps": actual,
                    "requested_ps": target,
                    "stage_position": status.position,
                    "stage_unit": dl.axis.config.unit,
                    "on_target": status.on_target,
                },
                summary=f"delay line at {actual:.3f} ps",
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("DelayLineMoveTo", exc)


# ── manual control panel (contract §13.1) ─────────────────────────────────


def _device_params() -> list[ParameterSpec]:
    return [
        ParameterSpec(name="device_id", type="str", required=True,
                      description=tr("设备 id，取自 ListOpticalDevices", "Device id, from ListOpticalDevices")),
        ParameterSpec(name="axis", type="str", required=True,
                      description=tr("该设备上的轴名", "Axis name on that device")),
    ]


class GetOpticalStageStatus(BaseSkill):
    """Everything a local optical stage's driver can report (manual panel readout)."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetOpticalStageStatus",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("读一台本机光学台的状态：每条轴的位置、是否在动、是否到位、是否已回参考，以及驱动能报的其余"
                "量 —— PI 的伺服状态、速度、控制器自己的行程、有没有限位/参考开关；泽攸 PZTC 的速度设定、"
                "当前速度、找零到位标志、闭环使能；PDXC2 的状态位（正/反向运动、闭环、错误）与控制模式。"
                "控制器答不上来的量不出现。默认 connect=false：设备还没连上时只报「未连接」，"
                "不会为了读状态去打开串口 / USB（厂家软件可能正占着它）。",
                   "Read the state of a local optical stage: per axis the position, whether it is moving, whether it is in position, whether it is referenced, plus whatever else the driver can report - "
                   "PI servo state, velocity, the controller's own travel range, limit/reference switches; Zeyou PZTC velocity setting, "
                   "current velocity, zero-found flag, closed-loop enable; PDXC2 status bits (forward/reverse motion, closed loop, errors) and control mode. "
                   "Quantities the controller cannot answer do not appear. Default connect=false: if the device is not connected yet it only reports 'not connected' "
                   "and does not open the serial port / USB just to read the state (vendor software may be holding it).")
            ),
            parameters=[
                ParameterSpec(name="device_id", type="str", required=True,
                              description=tr("设备 id，取自 ListOpticalDevices", "Device id, from ListOpticalDevices")),
                ParameterSpec(name="axis", type="str", required=False, default="",
                              description=tr("只读这一条轴；留空 = 所有轴", "Read only this one axis; leave empty = all axes")),
                ParameterSpec(name="connect", type="bool", required=False, default=False,
                              description=(
                                  tr("设备还没连上时打开它（串口 / USB）并做连接初始化：PI 伺服开、"
                                  "PDXC2 使能并把外触发设为 Manual、PZTC 写编码器方向与 PID1。"
                                  "这些都不移动台子。默认 false = 只读已连接的设备",
                                     "Open the device if it is not connected yet (serial port / USB) and run connection initialization: PI servo on, "
                                     "PDXC2 enabled with the external trigger set to Manual, PZTC encoder direction and PID1 written. "
                                     "None of these move the stage. Default false = only read devices that are already connected"))),
            ],
            estimated_duration_s=0.3,
            composition_level=0,
            tags=["optics", "stage", "status", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "GetOpticalStageStatus"
        device_id = str(params["device_id"])
        try:
            reg = get_instrument_registry()
            entry = reg.device_config(device_id)
            if entry is None:
                raise InstrumentError(f"no optical device with id {device_id!r}")
            base = {"device_id": device_id, "type": entry.get("type"),
                    "enabled": bool(entry.get("enabled", True))}
            if not base["enabled"]:
                return SkillResult(skill_name=name, success=False,
                                   data={**base, "connected": False},
                                   error=tr(f"optical device {device_id!r} is disabled（未启用）",
                                            f"optical device {device_id!r} is disabled"))
            if params.get("connect", False):
                axes_open = reg.controller(device_id).axes()      # opens it if needed
            else:
                ctrl = reg.built_controllers().get(device_id)
                axes_open = ctrl.open_axes() if ctrl is not None else {}   # never opens
            if not axes_open:
                axes = {str(a.get("name")): {"unit": a.get("unit", "um"),
                                             "limits": [a.get("min_pos"), a.get("max_pos")]}
                        for a in entry.get("axes", []) or [] if isinstance(a, dict)}
                return SkillResult(skill_name=name, success=True,
                                   data={**base, "connected": False, "axes": axes},
                                   summary=tr(f"{device_id}: 未连接（connect=true 才会打开它）",
                                              f"{device_id}: not connected (it is opened only with connect=true)"))
            names = [str(params["axis"])] if params.get("axis") else list(axes_open)
            out: dict = {}
            for axis_name in names:
                ax = axes_open.get(axis_name)
                if ax is None:
                    raise InstrumentError(f"{device_id}: no axis {axis_name!r}; configured axes: "
                                          f"{sorted(axes_open)}")
                d = ax.status_details()
                d.update({"unit": ax.config.unit,
                          "limits": [ax.config.min_pos, ax.config.max_pos],
                          "max_velocity": ax.config.max_velocity,
                          "supports": sorted(ax.SUPPORTS)})
                out[axis_name] = d
            return SkillResult(
                skill_name=name, success=True,
                data={**base, "connected": True, "axes": out},
                summary="; ".join(f"{n}={d.get('position')!r} {d.get('unit')}"
                                  for n, d in out.items()))
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)


class SetOpticalStageVelocity(BaseSkill):
    """Closed-loop velocity of one local optical stage axis."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetOpticalStageVelocity",
            version="1.0.0",
            category=SkillCategory.WRITE,
            # CONFIRM: it changes how fast every later move runs.
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("设一条本机光学台轴之后各次移动的闭环速度（轴原生单位 / 秒），写完读回核对。"
                "只在驱动有文档依据时可用：PI（VEL，控制器自己拒绝超出它上限的值）；泽攸 PZTC"
                "（速度寄存器，必须先在设备配置里给这条轴写 max_velocity，且不超过厂家说的 16 mm/s）。"
                "PDXC2 的速度参数厂家没写单位，不支持。超过配置的 max_velocity 一律拒绝、不截断。"
                "MAST 不把速度存进控制器的非易失存储。",
                   "Set the closed-loop velocity for subsequent moves of one local optical stage axis (axis native unit / s), and read it back to verify. "
                   "Available only where the driver has documentation: PI (VEL; the controller itself rejects values above its limit); Zeyou PZTC "
                   "(velocity register; max_velocity must first be written for this axis in the device config and must not exceed the vendor's 16 mm/s). "
                   "The PDXC2's velocity parameter has no unit in the vendor documentation and is not supported. Anything above the configured max_velocity is rejected, not truncated. "
                   "MAST does not store the velocity in the controller's non-volatile memory.")
            ),
            parameters=_device_params() + [
                ParameterSpec(name="velocity", type="float", required=True, min_value=0.0,
                              description=tr("速度，轴原生单位 / 秒（必须 > 0）", "Velocity, axis native unit / s (must be > 0)")),
            ],
            estimated_duration_s=0.3,
            composition_level=0,
            tags=["optics", "stage", "velocity", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "SetOpticalStageVelocity"
        try:
            ax = get_instrument_registry().axis(params["device_id"], params["axis"])
            requested = float(params["velocity"])
            back = ax.set_velocity(requested)
            unit = f"{ax.config.unit}/s"
            return SkillResult(
                skill_name=name, success=True,
                data={"device_id": params["device_id"], "axis": params["axis"],
                      "requested": requested, "velocity": back, "unit": unit,
                      "read_back": back is not None},
                summary=(f"{params['axis']} velocity {back:g} {unit} (read back)"
                         if back is not None else
                         f"{params['axis']} velocity {requested:g} {unit} sent (no readback)"))
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)


class OpticalStageServo(BaseSkill):
    """Closed-loop servo on/off for one local optical stage axis (PI only)."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="OpticalStageServo",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("打开 / 关闭一条本机光学台轴的闭环伺服，写完读回核对。目前只有 PI 控制器支持（SVO）："
                "伺服关着时 PI 拒绝闭环移动。泽攸 PZTC 的闭环使能位一写就会朝旧目标走、PDXC2 的开/闭环"
                "模式会改变位置的含义，所以都不在这里提供。查该设备的针尖要求（tip_requirement）。",
                   "Switch the closed-loop servo of one local optical stage axis on / off, and read it back to verify. Currently only PI controllers support it (SVO): "
                   "with the servo off the PI refuses closed-loop moves. The Zeyou PZTC's closed-loop enable bit makes it head for the old target as soon as it is written, and the PDXC2's open/closed-loop "
                   "mode changes the meaning of the position, so neither is offered here. Checks the device's tip requirement (tip_requirement).")
            ),
            parameters=_device_params() + [
                ParameterSpec(name="on", type="bool", required=True,
                              description=tr("true = 伺服开，false = 伺服关", "true = servo on, false = servo off")),
            ],
            estimated_duration_s=0.3,
            composition_level=0,
            tags=["optics", "stage", "servo", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "OpticalStageServo"
        refused = _tip_refusal(name, context, params["device_id"])
        if refused is not None:
            return refused
        try:
            ax = get_instrument_registry().axis(params["device_id"], params["axis"])
            on = bool(params["on"])
            back = ax.set_servo(on)
            return SkillResult(
                skill_name=name, success=True,
                data={"device_id": params["device_id"], "axis": params["axis"],
                      "requested": on, "servo": back, "read_back": back is not None},
                summary=f"{params['axis']} servo {'on' if on else 'off'}")
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)


class DisconnectOpticalDevice(BaseSkill):
    """Stop and close one local optical device so its vendor software can open it."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="DisconnectOpticalDevice",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("断开 MAST 与一台本机光学设备的连接：先发停止，再关掉串口 / USB，让厂家软件"
                "（Kinesis、PIMikroMove、LabVIEW）可以重新打开它。下次有技能用到它时 MAST 会重新连接。"
                "只关连接，不改设备配置；不会替你关别的程序。",
                   "Disconnect MAST from one local optical device: send stop first, then close the serial port / USB so that vendor software "
                   "(Kinesis, PIMikroMove, LabVIEW) can open it again. MAST reconnects the next time a skill uses it. "
                   "Only closes the connection and does not change the device config; it does not close other programs for you.")
            ),
            parameters=[
                ParameterSpec(name="device_id", type="str", required=True,
                              description=tr("设备 id，取自 ListOpticalDevices", "Device id, from ListOpticalDevices")),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["optics", "stage", "disconnect", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "DisconnectOpticalDevice"
        device_id = str(params["device_id"])
        try:
            reg = get_instrument_registry()
            entry = reg.device_config(device_id)
            if entry is None:
                raise InstrumentError(f"no optical device with id {device_id!r}")
            from mast.instruments.config import SATELLITE_TYPES

            if str(entry.get("type")) in SATELLITE_TYPES:
                raise InstrumentError(
                    tr(f"{device_id} 是卫星上的设备：它的串口 / USB 由卫星开关，这里断开的只会是枢纽"
                    "这一侧的代理 —— 不做。要让厂家软件接管它，在卫星上释放。",
                       f"{device_id} is a device on a satellite: its serial port / USB is opened and closed by the satellite, so all this would disconnect is the proxy on the hub "
                       "side - not done. To let vendor software take it over, release it on the satellite."))
            stop_error = ""
            ctrl = reg.built_controllers().get(device_id)
            if ctrl is not None and ctrl.is_connected:
                try:
                    ctrl.stop_all()
                except Exception as exc:  # noqa: BLE001 - close it anyway
                    stop_error = f"{type(exc).__name__}: {exc}"
            was_open = reg.release(device_id)
            data = {"device_id": device_id, "was_connected": was_open}
            if stop_error:
                data["stop_error"] = stop_error
            return SkillResult(
                skill_name=name, success=True, data=data,
                summary=(f"{device_id} disconnected" if was_open
                         else f"{device_id} was not connected"))
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)
