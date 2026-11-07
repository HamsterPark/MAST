"""WITec microscope atomic skills (docs/v2/design/optical_station_witec.md §6.3 — names and safety levels are the contract).

=========================  ========  =====================================================================
skill                      safety    what
=========================  ========  =====================================================================
GetWITecStatus             AUTO      connection, write access, sequencer, laser, analyzer, stage … (read-only)
GetAnalyzerAngle           AUTO      analyzer angle and whether it is in the beam path
ListWITecStates            AUTO      saved microscope states (presets) and the current one
ListWITecLasers            AUTO      the lasers WITec Control offers, which one is selected  (extra)
GetStagePosition           AUTO      XYZ position, µm
RequestWITecWriteAccess    CONFIRM   ask for write access; optionally ask the operator to press "COM Automation"
ReleaseWITecWriteAccess    AUTO      close every shutter, stop the sequencer, hand write access back   (extra)
SetAnalyzerAngle           CONFIRM   analyzer angle (and optionally in/out of the beam)
SetBeamPath                CONFIRM   raman / video / all_off / confocal / white_light
LoadWITecState             CONFIRM   load a saved state (grating, laser, centre … — the reliable way to change them)
SelectWITecLaser           CONFIRM   select a laser (software state only: the laser port coupler is manual)
SetWITecLaserPower         CONFIRM   laser power in mW (TruePower lasers), hub-side cap per laser
SetWITecShutter            CONFIRM   open / close the selected laser's shutter (open passes the two D7 gates)
CloseWITecShutter          AUTO      close every laser's shutter — token-free, never refused     (extra)
MoveWITecStage             CONFIRM   XYZ move, µm, limited and step-capped
AutoFocusWITec             CONFIRM   WITec's video autofocus
AcquireWITecVideoImage     CONFIRM   the video camera image to a file (+ scale in the sidecar)
RunAnalyzerSeries          CONFIRM   WITec's own analyzer series (full rotation), then save the project
SaveWITecProject           CONFIRM   store WITec Control's open project as a .wip
StopWITec                  AUTO      every shutter closed, sequencer + stage stopped — token-free, never refused
=========================  ========  =====================================================================

Spectra are NOT a skill here: the existing ``AcquireSpectrum`` runs on a ``witec_com`` device (pass its
``device_id``) because :class:`~mast.instruments.witec.microscope.WITecMicroscope` is also a ``Spectrometer``.

Every result carries ``microscope_status`` and the manual optical-state snapshot ``optical_state``
(:mod:`mast.operator_hitl.optical_state`), so an agent reading one result sees what the station looks like.

Limits are the driver's (Layer-0): out of range is refused, never clamped. WITec's write access is handed
over by a person (docs D5): a write without it fails clearly and tells the caller to run
``RequestWITecWriteAccess`` (``ask_operator=true`` posts the operator prompt and waits, bounded and abortable).

Laser emission (docs D7) has two gates, enforced in the driver for EVERY way software can put more light on the
sample, not only the shutter skill: the device entry's ``allow_emission_control`` (default false) and a valid operator
attestation ``laser_area_safe`` (the entry's ``require_laser_attestation: false`` waives that one), plus the laser's
current power within its cap. Covered: ``SetWITecShutter(open=true)``; ``SetBeamPath`` raman / confocal unless the
shutter is confirmed closed (then WITec adds no light; a shutter WITec opens by itself during the switch is closed
again at once); ``AcquireSpectrum`` unless it is a dark spectrum in the Raman path with the shutter closed (also
watched); ``RunAnalyzerSeries``; ``SetWITecLaserPower`` when it RAISES the power while the shutter is open or
unreadable; ``SelectWITecLaser`` and ``LoadWITecState`` when the laser they leave selected comes with an open shutter.
Anything that makes the light weaker or closes a shutter is never gated: ``CloseWITecShutter`` (every laser's shutter),
``StopWITec``, ``ReleaseWITecWriteAccess`` (which makes the station safe before it hands the write access back) and a
power lowering. A refusal carries ``refused_by`` (``emission_control_disabled`` / ``laser_attestation``). Project
directories (``SaveWITecProject``, ``RunAnalyzerSeries``) must lie below the MAST data root (``refused_by: directory``).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mast.core.types import (ParameterSpec, SafetyLevel, SkillCategory, SkillMetadata, SkillResult)
from mast.i18n import tr
from mast.instruments.base import (InstrumentError, InstrumentUnavailable, MotionAborted,
                                   OperationNotSupported)
from mast.instruments.microscope import BEAM_PATHS, MicroscopeLimitError, WriteAccessDenied
from mast.instruments.witec.paths import check_output_dir
from mast.instruments.registry import get_instrument_registry
from mast.operator_hitl import optical_state, prompts
from mast.skills.base import BaseSkill
from mast.skills.builtins.optics_common import (abort_probe, atomic_write_bytes, dump_json_bytes, fail,
                                                json_safe, new_run_id, resolve_device, scope_ids,
                                                stage_tip_result, staging_dir, store_in_experiment)

logger = logging.getLogger(__name__)

__all__ = [
    "GetWITecStatus", "GetAnalyzerAngle", "ListWITecStates", "ListWITecLasers", "GetStagePosition",
    "RequestWITecWriteAccess", "ReleaseWITecWriteAccess", "SetAnalyzerAngle", "SetBeamPath",
    "LoadWITecState", "SelectWITecLaser", "SetWITecLaserPower", "SetWITecShutter", "CloseWITecShutter",
    "MoveWITecStage", "AutoFocusWITec", "AcquireWITecVideoImage", "RunAnalyzerSeries",
    "SaveWITecProject", "StopWITec", "WITEC_SKILL_NAMES", "WITEC_STOP_SKILL_NAMES",
]

#: every skill this module defines. On the optical station the WITec is the instrument, so the write skills
#: take the instrument token (they are NOT in ``instrument_lock.OFF_INSTRUMENT_NAMES``); only
#: ``WITEC_STOP_SKILL_NAMES`` go to ``instrument_lock.BYPASS_NAMES``.
WITEC_SKILL_NAMES = (
    "GetWITecStatus", "GetAnalyzerAngle", "ListWITecStates", "ListWITecLasers", "GetStagePosition",
    "RequestWITecWriteAccess", "ReleaseWITecWriteAccess", "SetAnalyzerAngle", "SetBeamPath",
    "LoadWITecState", "SelectWITecLaser", "SetWITecLaserPower", "SetWITecShutter", "CloseWITecShutter",
    "MoveWITecStage", "AutoFocusWITec", "AcquireWITecVideoImage", "RunAnalyzerSeries",
    "SaveWITecProject", "StopWITec",
)
#: the ones that must run while another skill holds the instrument token (they only make things safer)
WITEC_STOP_SKILL_NAMES = ("StopWITec", "CloseWITecShutter", "ReleaseWITecWriteAccess")

# No "optional-hardware" tag: that one means "owned by a hardware-module switch" (skills/hardware_modules.py) and the WITec
# skills belong to no such module. An STM station withholds them by MODULE (skills/station_face.py STATION_ONLY_MODULES),
# the optical station shows them; a tag here would promise a switch that does not exist.
_TAGS = ["optics", "witec", "microscope"]


# ── helpers ───────────────────────────────────────────────────────────────────────────


def _device_param() -> ParameterSpec:
    return ParameterSpec(
        name="device_id", type="str", required=False, default="",
        description=tr("WITec 显微镜的设备 id（ListOpticalDevices）；留空 = 唯一一台显微镜，配了多台就必须指定",
                       "WITec microscope device id (ListOpticalDevices); empty = the only microscope, "
                       "required when several are configured"))


def _microscope(params: dict):
    reg = get_instrument_registry()
    dev_id = resolve_device(reg, kind="microscope", role=None, device_id=params.get("device_id") or None)
    return dev_id, reg.microscope(dev_id), reg


def _extras(mic: Any | None) -> dict:
    """``microscope_status`` and ``optical_state`` — attached to every result. Never raises."""
    out: dict[str, Any] = {}
    if mic is not None:
        try:
            out["microscope_status"] = json_safe(mic.microscope_status())
        except Exception as exc:  # noqa: BLE001 - a status that cannot be read is data, not a reason to fail
            out["microscope_status"] = {"connected": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        out["optical_state"] = json_safe(optical_state.get_store().snapshot())
    except Exception as exc:  # noqa: BLE001
        out["optical_state"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def _ok(name: str, mic: Any | None, data: dict, summary: str) -> SkillResult:
    return SkillResult(skill_name=name, success=True, data={**json_safe(data), **_extras(mic)}, summary=summary)


def _message(exc: BaseException) -> str:
    if type(exc).__name__ == "ShutterNotClosed":          # the driver's own words: NOT closed, close it by hand
        return str(exc)
    if isinstance(exc, WriteAccessDenied):
        return (f"{exc}. "
                + tr("先调用 RequestWITecWriteAccess（ask_operator=true 会请操作员点按钮）；不要盲目重试这一步。",
                     "Run RequestWITecWriteAccess first (ask_operator=true asks the operator to press the button); do not retry this step blindly."))
    if isinstance(exc, OperationNotSupported):
        return tr(f"not supported: {exc} —— 这台 WITec 没有文档化的做法，不是参数问题，不要重试。",
                  f"not supported: {exc} - this WITec has no documented way to do it; not a parameter problem, do not retry.")
    if isinstance(exc, MicroscopeLimitError):
        return f"refused by microscope limit: {exc}"
    return ""


def _fail(name: str, exc: BaseException, mic: Any | None = None, **data: Any) -> SkillResult:
    refused_by = {"LaserAttestationRequired": "laser_attestation",       # the WITec driver's own D7 gates
                  "EmissionControlDisabled": "emission_control_disabled"}.get(type(exc).__name__)
    if refused_by:
        data.setdefault("refused_by", refused_by)
    res = fail(name, exc, **data)
    custom = _message(exc)
    if custom:
        res.error = custom
    res.data = {**json_safe(res.data), **_extras(mic)}
    return res


def _note_stop(mic: Any | None, name: str, context: Any) -> None:
    """A stop that somebody asked for counts as an *external stop* on the device: a running protocol watches
    ``external_stop_epoch`` and ends cleanly instead of opening the shutter again at its next step. Made
    BEFORE the stop acts, so the counter has already moved when the shutter closes. Only for the stop skills - never for
    the protocol's own safe-direction calls (``SetWITecShutter`` with ``open=false``, the driver's own sweeps). Never
    raises, and never keeps a stop from acting."""
    caller = str(getattr(context, "owner", "") or "unknown caller")
    try:
        note = getattr(mic, "note_external_stop", None)
        if callable(note):
            note(f"{name} ({caller})")
    except Exception:  # noqa: BLE001
        logger.debug("%s: could not note the external stop", name, exc_info=True)


def _selection_note(report: Any) -> str:
    """The driver's note about a laser selection a stop could not put back (``""`` when there is none): the light is off,
    but nothing may emit until somebody chooses a laser (SelectWITecLaser) - the caller has to be told."""
    try:
        rep = report if isinstance(report, dict) else {}
        rep = rep.get("shutters") if isinstance(rep.get("shutters"), dict) else rep
        return str(rep.get("note") or "") if rep.get("selection_lost") else ""
    except Exception:  # noqa: BLE001
        return ""


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_stem(text: str, limit: int = 40) -> str:
    return "".join(c for c in str(text or "") if c.isalnum() or c in "-_")[:limit]


def _emission_refusal(reg: Any, dev_id: str, mic: Any | None = None) -> tuple[str, str] | None:
    """``(refused_by, why)`` if software may not open the shutter right now, else ``None``. D7 has two gates: the
    device entry's ``allow_emission_control`` (default false) and the emission guard (by default the operator's
    ``laser_area_safe`` attestation; the entry's ``require_laser_attestation: false`` removes the guard). The WITec
    driver owns the rule (``emission_refusal``), so the skill and the driver cannot disagree - the driver checks
    again when the shutter is actually opened (defence in depth). The code below only serves a microscope without
    that method: the attestation, as the driver's default guard would judge it."""
    asked = getattr(mic, "emission_refusal", None)
    if callable(asked):
        return asked()
    entry = (reg.device_config(dev_id) if callable(getattr(reg, "device_config", None)) else None) or {}
    if entry.get("require_laser_attestation") is False:
        return None
    from mast.instruments.witec.microscope import LaserAttestationGuard

    why = LaserAttestationGuard().problem()
    return ("laser_attestation", why) if why else None


# ── read-only ────────────────────────────────────────────────────────────────────────


class GetWITecStatus(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetWITecStatus", version="1.0.0", category=SkillCategory.READ, safety_level=SafetyLevel.AUTO,
            description=tr(
                "读 WITec 显微镜状态：是否连上 WITec Control、是否持有写权限、序列器在不在跑、选中的激光（波长/功率/快门）、"
                "分析器角度、光路、位移台 XYZ、物镜登记信息、当前保存状态，以及这台 WITec 支持哪些功能（参数表里有没有对应路径）。"
                "只读，不申请写权限。WITec Control 没开时也成功返回，connected=false 并说明原因。",
                "Read the WITec microscope state: whether WITec Control is reachable, whether we hold write access, whether a sequencer is running, "
                "the selected laser (wavelength/power/shutter), analyzer angle, beam path, stage XYZ, the objective as registered, the current saved state, "
                "and which features this WITec supports (whether the parameter paths exist). Read-only; does not ask for write access. "
                "Also succeeds when WITec Control is closed, with connected=false and the reason."),
            parameters=[_device_param()], estimated_duration_s=1.0, composition_level=0,
            tags=[*_TAGS, "read", "status"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev_id, mic, _reg = _microscope(params)
        except Exception as exc:  # noqa: BLE001
            return _fail("GetWITecStatus", exc)
        try:
            spec = mic.status()
        except Exception as exc:  # noqa: BLE001
            spec = {"error": f"{type(exc).__name__}: {exc}"}
        res = _ok("GetWITecStatus", mic,
                  {"device_id": dev_id, "spectrometer": spec, "microscope_limits": mic.microscope_limits.as_dict(),
                   "spectrometer_limits": mic.limits.as_dict()}, "")
        st = res.data.get("microscope_status", {})
        res.summary = (f"{dev_id}: " + (
            f"connected, write access {'yes' if st.get('write_access') else 'NO'}, "
            f"{'sequencer RUNNING' if st.get('sequencer_active') else 'idle'}, beam {st.get('beam_path', '?')}"
            if st.get("connected") else f"NOT connected - {st.get('error', 'WITec Control unreachable')}"))
        return res


class GetAnalyzerAngle(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetAnalyzerAngle", version="1.0.0", category=SkillCategory.READ, safety_level=SafetyLevel.AUTO,
            description=tr("读 WITec 电动 Analyzer 的角度（度）和它是否在光路里。只读。注意：Analyzer 轮里装的是哪一片"
                           "（λ/2 / 检偏片）WITec 不知道，看结果里的 optical_state.analyzer_optic。",
                           "Read the angle (deg) of WITec's motorised analyzer and whether it is in the beam path. Read-only. "
                           "WITec does not know which optic sits in the analyzer wheel (half-wave plate / polarizer): see optical_state.analyzer_optic in the result."),
            parameters=[_device_param()], estimated_duration_s=0.5, composition_level=0,
            tags=[*_TAGS, "read", "status", "analyzer"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev_id, mic, _reg = _microscope(params)
            angle = mic.get_analyzer_deg()
        except Exception as exc:  # noqa: BLE001
            return _fail("GetAnalyzerAngle", exc)
        res = _ok("GetAnalyzerAngle", mic, {"device_id": dev_id, "angle_deg": angle}, "")
        coupled = res.data.get("microscope_status", {}).get("analyzer_in")
        res.data["coupled"] = coupled
        res.summary = f"{dev_id}: analyzer {angle:.2f} deg" + (
            "" if coupled is None else (", in the beam" if coupled else ", OUT of the beam"))
        return res


class ListWITecStates(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ListWITecStates", version="1.0.0", category=SkillCategory.READ, safety_level=SafetyLevel.AUTO,
            description=tr("列出 WITec 已保存的显微镜状态（预设：激光 + 光栅 + 中心波长 + 相机 + 照明），以及当前状态。"
                           "COM 只能读/设当前状态名，不能枚举，所以列表来自设备配置。换光栅/中心波长最可靠的办法是 LoadWITecState。只读。",
                           "List WITec's saved microscope states (presets: laser + grating + centre wavelength + camera + illumination) and the current one. "
                           "COM can read/set the current state name but not enumerate them, so the list comes from the device configuration. "
                           "LoadWITecState is the reliable way to change grating / centre wavelength. Read-only."),
            parameters=[_device_param()], estimated_duration_s=0.5, composition_level=0,
            tags=[*_TAGS, "read", "status", "states"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev_id, mic, _reg = _microscope(params)
            states = mic.list_states()
        except Exception as exc:  # noqa: BLE001
            return _fail("ListWITecStates", exc)
        res = _ok("ListWITecStates", mic, {"device_id": dev_id, "states": states}, "")
        cur = res.data.get("microscope_status", {}).get("state")
        res.data["current"] = cur
        res.summary = f"{dev_id}: {len(states)} state(s); current {cur or '?'}"
        return res


class ListWITecLasers(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ListWITecLasers", version="1.0.0", category=SkillCategory.READ, safety_level=SafetyLevel.AUTO,
            description=tr("列出 WITec 的激光条目（序号、名称、波长、软件能否控功率）和当前选中的那个。"
                           "选中的那个波长是 WITec 实测值，其余是设备配置里的标称值（wavelength_verified=false）。"
                           "激光端口耦合器是手动的：选中只改软件状态。只读。",
                           "List WITec's laser entries (index, name, wavelength, whether software controls the power) and the selected one. "
                           "The selected one's wavelength is WITec's live value, the others are nominal values from the device configuration (wavelength_verified=false). "
                           "The laser-port coupler is manual: selecting only changes the software state. Read-only."),
            parameters=[_device_param()], estimated_duration_s=0.5, composition_level=0,
            tags=[*_TAGS, "read", "status", "laser"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev_id, mic, _reg = _microscope(params)
            lasers = mic.list_lasers()
        except Exception as exc:  # noqa: BLE001
            return _fail("ListWITecLasers", exc)
        sel = [l for l in lasers if l.get("selected")]
        return _ok("ListWITecLasers", mic, {"device_id": dev_id, "lasers": lasers,
                                            "selected": sel[0] if sel else None},
                   f"{dev_id}: {len(lasers)} laser(s); selected {sel[0]['name'] if sel else 'unknown'}")


class GetStagePosition(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetStagePosition", version="1.0.0", category=SkillCategory.READ, safety_level=SafetyLevel.AUTO,
            description=tr("读 WITec 位移台当前 XYZ 位置（µm，软件坐标）。只读。",
                           "Read the current XYZ position of the WITec stage (um, software coordinates). Read-only."),
            parameters=[_device_param()], estimated_duration_s=0.5, composition_level=0,
            tags=[*_TAGS, "read", "status", "stage"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev_id, mic, _reg = _microscope(params)
            pos = mic.get_stage_position_um()
        except Exception as exc:  # noqa: BLE001
            return _fail("GetStagePosition", exc)
        return _ok("GetStagePosition", mic, {"device_id": dev_id, "x_um": pos["x"], "y_um": pos["y"],
                                             "z_um": pos["z"]},
                   f"{dev_id}: x={pos['x']} y={pos['y']} z={pos['z']} um")


# ── write access ──────────────────────────────────────────────────────────────────────────


class RequestWITecWriteAccess(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="RequestWITecWriteAccess", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr(
                "向 WITec Control 申请写权限。写权限只能由人交出：操作员要在 WITec Control 控制树里点『COM Automation』"
                "（点开后会弹出一个窗口，锁住 WITec 的人工输入，直到点 Revoke）。没点的话本技能明确失败，不重试刷屏。"
                "ask_operator=true 时会向操作员发一条提示『请在 WITec Control 点 COM Automation』并等待（有上限、可取消），"
                "操作员确认后再试一次。WITec Control 没开也会提示操作员先打开它。",
                "Ask WITec Control for write access. Only a person can hand it over: the operator has to press 'COM Automation' in the WITec Control tree "
                "(a window opens that blocks WITec's manual input until Revoke is pressed). If it was not pressed this skill fails clearly and does not retry. "
                "With ask_operator=true it posts an operator prompt 'please press COM Automation in WITec Control' and waits (bounded, abortable), then tries once more. "
                "If WITec Control is closed, the prompt asks the operator to open it first."),
            parameters=[
                _device_param(),
                ParameterSpec(name="ask_operator", type="bool", required=False, default=False,
                              description=tr("没有写权限时向操作员发提示并等待", "when there is no write access, prompt the operator and wait")),
                ParameterSpec(name="wait_s", type="float", unit="s", required=False, default=180.0,
                              min_value=1.0, max_value=1800.0,
                              description=tr("等操作员回答的上限（秒）", "upper bound for the operator's answer (s)")),
                ParameterSpec(name="retry_s", type="float", unit="s", required=False, default=15.0,
                              min_value=0.0, max_value=120.0,
                              description=tr("操作员确认后，继续尝试拿写权限的窗口（秒）",
                                             "after the operator confirms, how long to keep trying for write access (s)")),
            ],
            estimated_duration_s=5.0, composition_level=0, tags=[*_TAGS, "write", "access"])

    @staticmethod
    def _try(mic: Any) -> tuple[bool, str]:
        """``(granted, why_not)`` — one honest attempt."""
        try:
            return bool(mic.request_write_access()), ""
        except InstrumentUnavailable as exc:
            return False, str(exc)

    def execute(self, context, params: dict) -> SkillResult:
        name = "RequestWITecWriteAccess"
        try:
            dev_id, mic, _reg = _microscope(params)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)
        try:
            granted, why = self._try(mic)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        asked = False
        if not granted and params.get("ask_operator"):
            asked = True
            wait_s = float(params.get("wait_s") or 180.0)
            probe = abort_probe(context)
            closed = bool(why)
            prompt = prompts.OperatorPrompt(
                kind="confirm", severity="action", skill=name, timeout_s=wait_s,
                title=(tr("请先打开 WITec Control，再点『COM Automation』", "Open WITec Control, then press 'COM Automation'")
                       if closed else tr("请在 WITec Control 点『COM Automation』", "Please press 'COM Automation' in WITec Control")),
                message=tr(
                    f"AI 要操作 {dev_id}（{mic.name}），需要写权限。"
                    + ("WITec Control 现在没有运行或连不上：请先打开它。" if closed else "")
                    + "请在 WITec Control 的控制树里点『COM Automation』（它会弹出一个窗口并锁住人工输入），完成后在这里回答『是』。"
                      "用完后点窗口里的 Revoke，或让 AI 调用 ReleaseWITecWriteAccess。",
                    f"The AI needs write access to {dev_id} ({mic.name}). "
                    + ("WITec Control is not running or unreachable: open it first. " if closed else "")
                    + "Press 'COM Automation' in the WITec Control tree (it opens a window that blocks manual input), then answer 'yes' here. "
                      "When done, press Revoke in that window or let the AI call ReleaseWITecWriteAccess."),
                context={"device_id": dev_id, "reason": why or "no write access"})
            try:
                ans = prompts.ask(prompt, check_abort=probe, timeout_s=wait_s)
            except RuntimeError as exc:
                return _fail(name, MotionAborted(str(exc)), mic)
            if not ans.ok:
                return _fail(name, InstrumentError(
                    f"the operator did not confirm in time or declined (prompt {ans.status}); no write access"),
                    mic, asked_operator=True, prompt_status=ans.status)
            retry = float(params.get("retry_s") if params.get("retry_s") is not None else 15.0)
            deadline = time.monotonic() + retry
            while True:
                try:
                    granted, why = self._try(mic)
                except Exception as exc:  # noqa: BLE001
                    return _fail(name, exc, mic, asked_operator=True)
                if granted or time.monotonic() >= deadline:
                    break
                if probe is not None and probe():
                    return _fail(name, MotionAborted("aborted while waiting for write access"), mic)
                time.sleep(1.0)
        if not granted:
            st = mic.microscope_status()
            hint = ("" if st.get("connected") is not False else
                    tr(" WITec Control 没开或连不上。", " WITec Control is closed or unreachable."))
            return _fail(name, WriteAccessDenied(
                f"{dev_id}: no write access - the operator has not pressed 'COM Automation' in WITec Control{hint}"),
                mic, asked_operator=asked)
        return _ok(name, mic, {"device_id": dev_id, "write_access": True, "asked_operator": asked},
                   f"{dev_id}: write access granted"
                   + tr("（WITec 的人工输入现在被锁住，直到 Revoke）", " (WITec's manual input is locked until Revoke)"))


class ReleaseWITecWriteAccess(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ReleaseWITecWriteAccess", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=tr("把写权限还给 WITec Control（作业结束时用）。还之前先让现场安全：关所有激光的快门、停序列器（还了之后 MAST 就关不了、停不了）；"
                           "做到了什么在结果的 made_safe 里。之后 WITec 的人工输入是否解锁取决于操作员是否点了 Revoke。"
                           "不需要仪器令牌，不会被拒绝，也不排在曝光后面。",
                           "Hand write access back to WITec Control (at the end of a job). First the station is made safe - every laser's shutter closed, the sequencer stopped (afterwards MAST could neither close nor stop); "
                           "what was done is in made_safe in the result. Whether WITec's manual input unlocks then depends on the operator pressing Revoke. "
                           "Needs no instrument token, is never refused and is not queued behind an exposure."),
            parameters=[_device_param()], estimated_duration_s=3.0, composition_level=0,
            tags=[*_TAGS, "write", "access", "safety", "stop"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "ReleaseWITecWriteAccess"
        try:
            dev_id, mic, _reg = _microscope(params)
            _note_stop(mic, name, context)
            mic.release_write_access()
            held = mic.has_write_access()
            made_safe = getattr(mic, "last_release_report", None) or {}
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)
        note = _selection_note(made_safe)
        return _ok(name, mic, {"device_id": dev_id, "write_access": held, "made_safe": json_safe(made_safe),
                               **({"note": note} if note else {})},
                   f"{dev_id}: write access " + ("STILL held" if held else "released") + (f" - NOTE: {note}" if note else ""))


# ── optics ─────────────────────────────────────────────────────────────────────────────────


class SetAnalyzerAngle(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetAnalyzerAngle", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("设 WITec 电动 Analyzer 的角度（度，0–360）并等它转到（回读校验）。coupled 可同时把它放进/移出光路。"
                           "超出本机限值直接拒绝，不截断。这个技能不会触发 WITec 的『更换滤光片』对话框。"
                           "Analyzer 轮里装的是哪一片由操作员登记（optical_state.analyzer_optic）。",
                           "Set the angle (deg, 0-360) of WITec's motorised analyzer and wait until it arrives (verified by read-back). coupled also puts it in / out of the beam. "
                           "Beyond this machine's limits it is refused, not clamped. This skill never triggers WITec's 'change filter' dialog. "
                           "Which optic sits in the analyzer wheel is registered by the operator (optical_state.analyzer_optic)."),
            parameters=[
                _device_param(),
                ParameterSpec(name="angle_deg", type="float", unit="deg", required=True, min_value=0.0,
                              max_value=360.0, description=tr("Analyzer 角度", "analyzer angle")),
                ParameterSpec(name="coupled", type="bool", required=False,
                              description=tr("true = 放进光路，false = 移出光路；不给 = 不动", "true = into the beam path, false = out; not given = leave")),
            ],
            estimated_duration_s=6.0, composition_level=0, tags=[*_TAGS, "write", "analyzer"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "SetAnalyzerAngle"
        mic = None
        try:
            dev_id, mic, _reg = _microscope(params)
            coupled = None
            if params.get("coupled") is not None:
                coupled = mic.set_analyzer_in(bool(params["coupled"]))
            achieved = mic.set_analyzer_deg(params["angle_deg"])
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        return _ok(name, mic, {"device_id": dev_id, "requested_deg": float(params["angle_deg"]),
                               "angle_deg": achieved, "coupled": coupled},
                   f"{dev_id}: analyzer at {achieved:.2f} deg")


class SetBeamPath(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetBeamPath", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("设 WITec 光路：raman（拉曼测量）、video（看视频像）、all_off、confocal、white_light，并等光路状态到位。"
                           "raman / confocal 只有在激光快门可能是开的（开着、读不出、或这条激光没有软件快门）时才要过出光闸；快门确认关着时切光路不出 WITec 的光，"
                           "外部光源的协议不必放开 WITec 的出光控制。WITec 切完光路若自己把快门开了，会按出光判：闸不满足就立刻关回并报错。"
                           "结果里的 microscope_status.shutter_open 是切完之后的实际状态。",
                           "Set WITec's beam path: raman (measurement), video (look at the video image), all_off, confocal, white_light, and wait for the state to arrive. "
                           "raman / confocal pass the emission gates only when the laser shutter may be open (open, unreadable, or a laser with no software shutter); with the shutter confirmed closed the switch puts no WITec light on the sample, "
                           "so a protocol with an external source needs no WITec emission control. If WITec itself opens the shutter during the switch it is judged as an emission: closed again at once and an error raised when the gates are not met. "
                           "microscope_status.shutter_open in the result is the actual state afterwards."),
            parameters=[_device_param(),
                        ParameterSpec(name="path", type="str", required=True, allowed_values=list(BEAM_PATHS),
                                      description=tr("光路", "beam path"))],
            estimated_duration_s=6.0, composition_level=0, tags=[*_TAGS, "write", "beampath"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "SetBeamPath"
        mic = None
        try:
            dev_id, mic, _reg = _microscope(params)
            state = mic.set_beam_path(params["path"])
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        return _ok(name, mic, {"device_id": dev_id, "path": params["path"], "state": state},
                   f"{dev_id}: beam path {params['path']} (WITec reports {state})")


class LoadWITecState(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="LoadWITecState", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("载入 WITec 已保存的显微镜状态（激光 + 光栅 + 中心波长 + 相机 + 照明），例如 'RayShield 532 - G600'。"
                           "COM 里没有可靠的直接设光栅/中心波长的办法，载入预设是最可靠的。名字见 ListWITecStates。"
                           "载入后会回读校验；载入期间每个轮询周期（≤0.2 s）看一次快门：预设若把快门打开而出光闸不满足，立刻关回并报错；"
                           "出光闸不满足时，载入前先把已经开着的快门关上；载入后按预设新选中的那台激光重判（上限没配、功率读不出都拒绝）。载入期间不能有序列在跑。",
                           "Load a saved WITec microscope state (laser + grating + centre wavelength + camera + illumination), e.g. 'RayShield 532 - G600'. "
                           "COM has no reliable direct way to set grating / centre wavelength; loading a preset is the dependable one. Names: ListWITecStates. "
                           "The load is verified by read-back. During the load the shutter is looked at every poll (at most 0.2 s apart): a preset that opens it while the emission gates are not met is shut again at once and an error raised; "
                           "with the gates not met, a shutter that is open is closed BEFORE the load; afterwards the laser the preset selected is judged (no cap configured or an unreadable power is a refusal). No sequence may be running."),
            parameters=[_device_param(),
                        ParameterSpec(name="name", type="str", required=True,
                                      description=tr("状态名（ListWITecStates）", "state name (ListWITecStates)"))],
            estimated_duration_s=15.0, composition_level=0, tags=[*_TAGS, "write", "state"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "LoadWITecState"
        mic = None
        try:
            dev_id, mic, _reg = _microscope(params)
            cur = mic.load_state(params["name"])
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        return _ok(name, mic, {"device_id": dev_id, "state": cur}, f"{dev_id}: state {cur!r} loaded")


class SelectWITecLaser(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SelectWITecLaser", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("选 WITec 的激光（序号、名称或波长，如 '532'）。这只改 WITec 的软件状态：激光端口耦合器和 355 nm/1064 nm 的光路是手动的，"
                           "要操作员确认实物接对了。选完用 WITec 实测的波长核对配置表，对不上就报错。新选中的激光若带着开着的快门来（人留下的），按出光判：出光闸不满足、超上限、或认不出是哪台激光，"
                           "就先把那台激光的快门关上再报错。",
                           "Select a WITec laser (index, name or wavelength, e.g. '532'). This only changes WITec's software state: the laser-port coupler and the 355/1064 nm paths are manual, "
                           "so the operator has to confirm the hardware matches. After selecting, WITec's live wavelength is checked against the configured table; a mismatch is an error. "
                           "If the newly selected laser comes with its shutter open (left so by a person) it is judged as an emission: with the gates not met, above its cap, or not identifiable, its shutter is closed first and an error raised."),
            parameters=[_device_param(),
                        ParameterSpec(name="laser", type="str", required=True,
                                      description=tr("序号、名称或波长（ListWITecLasers）", "index, name or wavelength (ListWITecLasers)"))],
            estimated_duration_s=3.0, composition_level=0, tags=[*_TAGS, "write", "laser"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "SelectWITecLaser"
        mic = None
        try:
            dev_id, mic, _reg = _microscope(params)
            las = mic.select_laser(params["laser"])
            now = [l for l in mic.list_lasers() if l.get("selected")]
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        cur = now[0] if now else las
        return _ok(name, mic, {"device_id": dev_id, "laser": cur,
                               "note": "software state only - confirm the laser-port coupler is set by hand"},
                   f"{dev_id}: laser {cur.get('name')} selected ({cur.get('wavelength_nm')} nm)")


class SetWITecLaserPower(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetWITecLaserPower", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("设选中激光的功率（mW，样品处，TruePower 激光）。每条激光有本机上限（microscope_limits.max_laser_power_mw），"
                           "超限、没配上限、或这条激光软件控不了功率（355 nm 无源、1064 nm 由外部控制）一律拒绝，不截断。"
                           "设完回读实际功率；回读高于上限会关快门并报错。快门开着（或读不出）时把功率调高，等于出光：要过两道出光闸；调低、或快门确认关着时调功率不查闸。",
                           "Set the selected laser's power (mW at the sample, TruePower lasers). Each laser has a cap on this machine (microscope_limits.max_laser_power_mw); "
                           "beyond it, with no cap configured, or for a laser software cannot control (355 nm passive, 1064 nm external) it is refused, not clamped. "
                           "The actual power is read back; a read-back above the cap closes the shutter and raises an error. Raising the power while the shutter is open (or may be) is an emission: it passes the two gates; lowering it, or setting it with the shutter confirmed closed, is not gated."),
            parameters=[_device_param(),
                        ParameterSpec(name="power_mw", type="float", unit="mW", required=True, min_value=0.0,
                                      max_value=1000.0, description=tr("功率", "power"))],
            estimated_duration_s=3.0, composition_level=0, tags=[*_TAGS, "write", "laser", "power"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "SetWITecLaserPower"
        mic = None
        try:
            dev_id, mic, _reg = _microscope(params)
            actual = mic.set_laser_power_mw(params["power_mw"])
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        return _ok(name, mic, {"device_id": dev_id, "requested_mw": float(params["power_mw"]),
                               "power_mw": actual},
                   f"{dev_id}: laser power {actual} mW")


class SetWITecShutter(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetWITecShutter", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("开/关选中激光的快门（open=true/false）。**关**永远允许：不查限值、不查签署，"
                           "不排在曝光后面，写权限不在手上时先静默要一次，关不上会明说『没关上』（要关所有激光的快门、免令牌：CloseWITecShutter）。**开**要过两道闸（D7）：设备配置 "
                           "allow_emission_control=true（默认 false，由操作员在配置里打开）、操作员已签署 laser_area_safe"
                           "（设备配置 require_laser_attestation=false 可免掉这一道）；另外要有写权限、选中的激光有软件快门，"
                           "并且当前功率不超过本机上限（上限没配置、或功率读不出来都拒绝）。"
                           "驱动对所有可能出光的动作（拉曼/共聚焦光路、采谱、检偏角系列、载入状态后快门被打开）都用同样的闸。",
                           "Open/close the selected laser's shutter (open=true/false). **Closing** is always allowed: no limits, no attestation, "
                           "it is not queued behind an exposure, asks for write access silently if it is not held, and says NOT closed when it cannot confirm (CloseWITecShutter closes EVERY laser's shutter, without the token). **Opening** passes two gates (D7): "
                           "the device entry's allow_emission_control=true (default false; the operator turns it on in the config) and a valid operator attestation laser_area_safe "
                           "(the entry's require_laser_attestation=false waives that one); it also needs write access, a selected laser with a software shutter, "
                           "and a current power within this machine's cap (refused when no cap is configured or the power cannot be read). "
                           "The driver applies the same gates to every action that may put light on the sample (Raman/confocal beam path, acquisition, "
                           "analyzer series, a loaded state that opens the shutter)."),
            parameters=[_device_param(),
                        ParameterSpec(name="open", type="bool", required=True,
                                      description=tr("true = 开，false = 关", "true = open, false = close"))],
            estimated_duration_s=1.0, composition_level=0, tags=[*_TAGS, "write", "laser", "shutter"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "SetWITecShutter"
        try:
            dev_id, mic, reg = _microscope(params)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)
        want_open = bool(params["open"])
        attest = None
        if want_open:
            refusal = _emission_refusal(reg, dev_id, mic)
            if refusal:
                code, why = refusal
                return SkillResult(skill_name=name, success=False, error=why,
                                   data={"device_id": dev_id, "refused_by": code, **_extras(mic)})
            status = getattr(mic, "attestation_status", None)
            state = status() if callable(status) else None
            try:
                sv = optical_state.get_store().get("laser_area_safe")
            except optical_state.OpticalStateError:
                sv = None
            attest = sv.to_dict() if state == "valid" and sv is not None else (
                {"waived": True} if state in (None, "waived") else {"status": state})
        try:
            state = mic.set_shutter(want_open)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        return _ok(name, mic, {"device_id": dev_id, "shutter_open": state, "attestation": attest},
                   f"{dev_id}: shutter {'OPEN' if state else 'closed'}")


class CloseWITecShutter(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CloseWITecShutter", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=tr("关激光快门：先关选中那台，再逐台关其他激光的（WITec 只给『选中激光』的快门接口，所以要依次选一下再选回来；"
                           "序列器在跑时不去换激光，要全停用 StopWITec）。永不被拒：不查限值/签署，不要仪器令牌，不排在曝光后面；"
                           "没有写权限时先静默要一次（操作员点过的 COM Automation 还在就拿得回）。关不上会明说『没关上』，请操作员在 WITec 里手动关。"
                           "355 nm 无源、没有软件快门：物理挡光。如果激光选择没能放回原处，结果的 note 会说明：之后先用 SelectWITecLaser 明确选一台，才能再开快门或加功率。",
                           "Close the laser shutters: the selected laser's first, then each other laser's (WITec only exposes the SELECTED laser's shutter, so each laser is selected in turn and the selection put back; "
                           "while a sequencer runs the lasers are not swapped - StopWITec stops it first). Never refused: no limits/attestation, no instrument token, not queued behind an exposure; "
                           "without write access it asks once, silently (the operator's COM Automation grant is still good unless revoked). A shutter that cannot be closed is reported as NOT closed - then ask the operator to close it by hand in WITec. "
                           "The 355 nm laser is passive with no software shutter: block it physically. If the laser selection could not be put back, the result's note says so: choose a laser with SelectWITecLaser before opening a shutter or raising power again."),
            parameters=[_device_param()], estimated_duration_s=3.0, composition_level=0,
            tags=[*_TAGS, "write", "laser", "shutter", "safety", "stop"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "CloseWITecShutter"
        try:
            dev_id, mic, _reg = _microscope(params)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)
        _note_stop(mic, name, context)
        try:
            closer = getattr(mic, "close_all_shutters", None)
            report = closer() if callable(closer) else None
            if report is None:
                mic.set_shutter(False)
        except Exception as exc:  # noqa: BLE001
            extra = {"physical_action": "close the shutter by hand in WITec Control, or block the beam"}
            if getattr(exc, "report", None):
                extra["shutters"] = json_safe(exc.report)
            return _fail(name, exc, mic, **extra)
        note = _selection_note(report)
        return _ok(name, mic, {"device_id": dev_id, "shutter_open": False, "shutters": json_safe(report),
                               **({"note": note} if note else {})},
                   f"{dev_id}: shutter closed" + (f" - NOTE: {note}" if note else ""))


# ── stage / focus / video ───────────────────────────────────────────────────────────────────────


class MoveWITecStage(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="MoveWITecStage", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("移动 WITec 样品台（µm，软件坐标）。x/y/z 至少给一个；relative=true 时是相对当前位置的位移。"
                           "目标必须在 microscope_limits 的行程内，单步不超过 max_step_um，WITec 自己的 Z 范围（±MaximumZStepperTravel/2）也要满足；"
                           "没配行程的轴一律拒绝（Z 撞物镜的风险）。等到位并回读校验，超时/中止会停轴。换物镜后先确认安全再对焦。",
                           "Move the WITec sample stage (um, software coordinates). Give at least one of x/y/z; relative=true treats them as offsets from the current position. "
                           "The target must be inside microscope_limits' travel, a single step no larger than max_step_um, and inside WITec's own Z range (+-MaximumZStepperTravel/2); "
                           "an axis with no configured travel is refused (risk of Z hitting the objective). Waits for arrival and verifies by read-back; timeout/abort stops the axis. After an objective change confirm it is safe before refocusing."),
            parameters=[
                _device_param(),
                ParameterSpec(name="x_um", type="float", unit="um", required=False, description="X"),
                ParameterSpec(name="y_um", type="float", unit="um", required=False, description="Y"),
                ParameterSpec(name="z_um", type="float", unit="um", required=False, description="Z"),
                ParameterSpec(name="relative", type="bool", required=False, default=False,
                              description=tr("true = 给的是相对位移", "true = the values are offsets")),
            ],
            estimated_duration_s=10.0, composition_level=0, tags=[*_TAGS, "write", "stage", "motion"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "MoveWITecStage"
        try:
            dev_id, mic, _reg = _microscope(params)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)
        refuse = stage_tip_result(name, context, dev_id)
        if refuse is not None:
            return refuse
        want = {a: params.get(f"{a}_um") for a in ("x", "y", "z")}
        if all(v is None for v in want.values()):
            return SkillResult(skill_name=name, success=False, error="give at least one of x_um, y_um, z_um")
        try:
            before = mic.get_stage_position_um()
            if params.get("relative"):
                for a, v in want.items():
                    if v is not None:
                        if before.get(a) is None:
                            raise InstrumentError(f"cannot move {a} relatively: its position is unreadable")
                        want[a] = before[a] + float(v)
            after = mic.move_stage_um(check_abort=abort_probe(context), **want)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        return _ok(name, mic, {"device_id": dev_id, "before_um": before, "after_um": after},
                   f"{dev_id}: stage at x={after.get('x')} y={after.get('y')} z={after.get('z')} um")


class AutoFocusWITec(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AutoFocusWITec", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("跑 WITec 的视频自动对焦并等它结束。它会动 Z。结束后若 Z 落在 microscope_limits 的 Z 范围之外会报错（台子已经在那里了，先检查再测）。"
                           "换物镜后第一次对焦前先确认安全：50×/100× 工作距离只有约 1 mm。",
                           "Run WITec's video autofocus and wait for it to finish. It moves Z. If Z ends outside microscope_limits' Z range an error is raised (the stage is already there: check before measuring). "
                           "After an objective change confirm it is safe before the first focus: the 50x/100x working distance is only about 1 mm."),
            parameters=[_device_param()], estimated_duration_s=30.0, composition_level=0,
            tags=[*_TAGS, "write", "focus", "motion"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "AutoFocusWITec"
        try:
            dev_id, mic, _reg = _microscope(params)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)
        refuse = stage_tip_result(name, context, dev_id)
        if refuse is not None:
            return refuse
        try:
            res = mic.autofocus(check_abort=abort_probe(context))
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        if not res.get("ok", True):
            return _fail(name, InstrumentError("WITec reports the autofocus failed (LastFailed)"), mic, **res)
        return _ok(name, mic, {"device_id": dev_id, **res}, f"{dev_id}: autofocus done, z={res.get('z_um')} um")


class AcquireWITecVideoImage(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AcquireWITecVideoImage", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("取一张 WITec 视频相机图像存成文件（会先切到 video 光路），并写同名 .json（图像物理尺寸 µm、旋转、激光点位置、"
                           "物镜登记信息、台位、光学状态快照）。有选定样品时一并归档进实验文件夹。结果返回路径，不返回像素。",
                           "Capture a WITec video-camera image to a file (switches to the video beam path first) and write a same-named .json (physical image size in um, rotation, laser spot position, "
                           "registered objective, stage position, optical-state snapshot). Archived into the experiment folder when a sample is selected. Returns paths, not pixels."),
            parameters=[_device_param(),
                        ParameterSpec(name="tag", type="str", required=False, default="",
                                      description=tr("文件名标签", "file-name label"))],
            estimated_duration_s=8.0, composition_level=0, tags=[*_TAGS, "write", "video", "snapshot"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "AcquireWITecVideoImage"
        try:
            dev_id, mic, _reg = _microscope(params)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)
        run_id = new_run_id("video")
        tag = _safe_stem(params.get("tag", ""), 24)
        stem = f"witec_video_{_stamp()}_{_safe_stem(dev_id, 20) or 'witec'}" + (f"_{tag}" if tag else "")
        sdir = staging_dir("witec_video")
        try:
            png = Path(mic.acquire_video_image(sdir / f"{stem}.png"))
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic, run_id=run_id)
        st = json_safe(mic.microscope_status())
        sidecar = {"schema": "mast.witec_video/1", "skill": name, "run_id": run_id, "device_id": dev_id,
                   "time_utc": _utc(),
                   "image": {"name": png.name, "size": png.stat().st_size if png.exists() else None},
                   "calibration": st.get("video"), "objective": st.get("objective"),
                   "magnification": st.get("magnification"), "stage_um": st.get("stage_um"),
                   "microscope_status": st, "optical_state": json_safe(optical_state.get_store().snapshot()),
                   "scope": scope_ids()}
        side = png.with_suffix(".json")
        try:
            atomic_write_bytes(side, dump_json_bytes(sidecar))
        except Exception as exc:  # noqa: BLE001
            return _fail(name, InstrumentError(f"could not write the sidecar: {exc}"), mic, path=str(png))
        stored = store_in_experiment([png, side], skill=name)
        paths = stored["paths"]
        return _ok(name, mic, {"device_id": dev_id, "run_id": run_id, "path": paths[0], "sidecar_path": paths[1],
                               "stored_in_experiment": bool(stored.get("stored")),
                               "calibration": sidecar["calibration"]},
                   f"{dev_id}: video image -> {Path(paths[0]).name}")


# ── series / project ──────────────────────────────────────────────────────────────────────────────────────


class RunAnalyzerSeries(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="RunAnalyzerSeries", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr(
                "跑 WITec 自己的 Analyzer 系列（Series Ana）：Analyzer 转完整一圈 0–360°，共 points 点，每点积分 integration_s × accumulations 次，"
                "WITec 自己负责转角、取谱和存进当前工程。需要 Analyzer 在光路里、光路和激光已设好、有写权限。部分角度范围不是 WITec 的原生能力——"
                "那种用 SetAnalyzerAngle + AcquireSpectrum 逐点做。跑完（save=true）把工程存成 .wip 并写 sidecar（含 Analyzer 轮里装的片、光学状态快照），"
                "有样品时归档进实验文件夹。要先选定样品（会产生数据）。dry_run=true 只检查限值、估时，不动硬件。可中止（会停序列器）。",
                "Run WITec's own Analyzer series (Series Ana): the analyzer turns one full revolution 0-360 deg in 'points' steps, each step integrating integration_s x accumulations; "
                "WITec itself turns the wheel, acquires and stores into the open project. The analyzer must be in the beam path, beam path and laser set, write access held. A partial angle range is not a native WITec ability - "
                "do that with SetAnalyzerAngle + AcquireSpectrum point by point. When done (save=true) the project is stored as a .wip with a sidecar (including the optic in the analyzer wheel and the optical-state snapshot) "
                "and archived into the experiment folder when a sample is selected. A sample must be selected (this produces data). dry_run=true only checks limits and estimates the duration, touching no hardware. Abortable (stops the sequencer)."),
            parameters=[
                _device_param(),
                ParameterSpec(name="points", type="int", required=True, min_value=2, max_value=3601,
                              description=tr("一圈里的点数（73 = 5° 一步，145 = 2.5°，361 = 1°）",
                                             "points per revolution (73 = 5 deg steps, 145 = 2.5, 361 = 1)")),
                ParameterSpec(name="integration_s", type="float", unit="s", required=True, min_value=0.001,
                              max_value=3600.0, description=tr("每次累加的积分时间", "integration time per accumulation")),
                ParameterSpec(name="accumulations", type="int", required=False, default=1, min_value=1,
                              max_value=100000, description=tr("每点累加次数", "accumulations per point")),
                ParameterSpec(name="start_deg", type="float", unit="deg", required=False, default=0.0,
                              description=tr("起始角，WITec 原生只支持 0", "start angle; WITec natively supports 0 only")),
                ParameterSpec(name="stop_deg", type="float", unit="deg", required=False, default=360.0,
                              description=tr("终止角，WITec 原生只支持 360", "stop angle; WITec natively supports 360 only")),
                ParameterSpec(name="name", type="str", required=False, default="",
                              description=tr("工程名（.wip 文件名，不含扩展名）；留空 = 自动", "project name (.wip file name without extension); empty = automatic")),
                ParameterSpec(name="save", type="bool", required=False, default=True,
                              description=tr("跑完把工程存成 .wip", "store the project as a .wip when done")),
                ParameterSpec(name="directory", type="str", required=False, default="",
                              description=tr("存到这个目录（不归档）：绝对路径，在 MAST 数据根之下，不接受 UNC 和 '..'；留空 = 暂存后归档进实验文件夹",
                                             "save into this folder (no archiving): an absolute path below the MAST data root, no UNC and no '..'; empty = stage, then archive into the experiment folder")),
                ParameterSpec(name="dry_run", type="bool", required=False, default=False,
                              description=tr("只检查和估时", "check and estimate only")),
            ],
            estimated_duration_s=300.0, composition_level=0,
            tags=[*_TAGS, "write", "analyzer", "series", "acquisition", "spectrum"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "RunAnalyzerSeries"
        try:
            dev_id, mic, _reg = _microscope(params)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)
        req = {"start_deg": params.get("start_deg") if params.get("start_deg") is not None else 0.0,
               "stop_deg": params.get("stop_deg") if params.get("stop_deg") is not None else 360.0,
               "points": params["points"], "integration_s": params["integration_s"],
               "accumulations": params.get("accumulations") or 1}
        checker = getattr(mic, "check_analyzer_series", None)
        try:
            plan = checker(**req) if callable(checker) else None
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        explicit = str(params.get("directory") or "").strip()
        if explicit and params.get("save", True):
            try:
                check_output_dir(explicit)           # before anything runs: a series is not worth losing to a bad folder
            except Exception as exc:  # noqa: BLE001
                return _fail(name, exc, mic, refused_by="directory")
        if params.get("dry_run"):
            return _ok(name, mic, {"dry_run": True, "device_id": dev_id, "request": req, "plan": plan},
                       f"[DRY RUN] {dev_id}: {req['points']} points, "
                       + (f"about {plan['estimated_s']:.0f} s" if plan else "limits not pre-checked on this device"))
        run_id = new_run_id("anaser")
        proj = _safe_stem(params.get("name") or "", 60) or f"analyzer_series_{_stamp()}"
        t0 = time.monotonic()
        try:
            # the driver stops the sequencer itself on abort / timeout; a refusal (busy, analyzer out of the
            # beam …) must NOT stop whatever else is running
            series = mic.run_analyzer_series(name=proj, check_abort=abort_probe(context), **req)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic, run_id=run_id)
        data: dict[str, Any] = {"device_id": dev_id, "run_id": run_id, "series": series, "project_name": proj,
                                "elapsed_s": round(time.monotonic() - t0, 2), "saved": False}
        if not params.get("save", True):
            return _ok(name, mic, data, f"{dev_id}: analyzer series done ({series.get('points')} points) - NOT saved")
        try:
            sdir = Path(explicit) if explicit else staging_dir("witec_projects")
            wip = Path(mic.save_project(sdir, proj))
        except Exception as exc:  # noqa: BLE001
            return _fail(name, InstrumentError(
                f"the series FINISHED (its data are in WITec Control's open project) but saving the project "
                f"failed: {exc} - call SaveWITecProject, do NOT rerun the series"),
                mic, series_finished=True, **data)
        data["saved"] = True
        paths = [str(wip)]
        if not explicit:
            side = wip.with_suffix(".json")
            sidecar = {"schema": "mast.witec_series/1", "skill": name, "run_id": run_id, "device_id": dev_id,
                       "time_utc": _utc(), "series": json_safe(series), "request": req, "project": wip.name,
                       "microscope_status": json_safe(mic.microscope_status()),
                       "optical_state": json_safe(optical_state.get_store().snapshot()), "scope": scope_ids()}
            try:
                atomic_write_bytes(side, dump_json_bytes(sidecar))
                stored = store_in_experiment([wip, side], skill=name)
                paths = stored["paths"]
                data["stored_in_experiment"] = bool(stored.get("stored"))
            except Exception as exc:  # noqa: BLE001
                data["archive_error"] = f"{type(exc).__name__}: {exc}"
        data["project_path"], data["paths"] = paths[0], paths
        return _ok(name, mic, data, f"{dev_id}: analyzer series {series.get('points')} points -> {Path(paths[0]).name}")


class SaveWITecProject(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SaveWITecProject", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("把 WITec Control 当前打开的工程存成 <name>.wip。WITec 不覆盖已有文件，重名会报错。默认保留工程内容"
                           "（clear_after_store=true 才在存完后清空工程，那是厂商 SDK 的默认，这里默认关）。"
                           "directory 留空 = 暂存后写 sidecar 并归档进当前样品的实验文件夹；给了 directory 就存在那里、不归档。要先选定样品。",
                           "Store the project currently open in WITec Control as <name>.wip. WITec never overwrites, so a duplicate name is an error. The project's content is kept "
                           "(clear_after_store=true empties it after storing - the vendor SDK's default, off here). directory empty = stage, write a sidecar and archive into the current sample's experiment folder; "
                           "with a directory it is saved there and not archived. A sample must be selected."),
            parameters=[
                _device_param(),
                ParameterSpec(name="name", type="str", required=True, description=tr("文件名（不含 .wip）", "file name without .wip")),
                ParameterSpec(name="directory", type="str", required=False, default="",
                              description=tr("目录，必须是绝对路径且在 MAST 数据根（项目根或实验根）之下，不接受 UNC 和 '..'；留空 = 暂存并归档",
                                             "folder: an absolute path below the MAST data root (project root or experiment root), no UNC and no '..'; empty = stage and archive")),
                ParameterSpec(name="clear_after_store", type="bool", required=False, default=False,
                              description=tr("存完后清空工程", "empty the project after storing")),
            ],
            estimated_duration_s=20.0, composition_level=0, tags=[*_TAGS, "write", "project", "record"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "SaveWITecProject"
        try:
            dev_id, mic, _reg = _microscope(params)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)
        explicit = str(params.get("directory") or "").strip()
        proj = str(params["name"]).strip()
        if explicit:
            try:
                check_output_dir(explicit)
            except Exception as exc:  # noqa: BLE001
                return _fail(name, exc, mic, refused_by="directory")
        try:
            sdir = Path(explicit) if explicit else staging_dir("witec_projects")
            kw = {"clear_after_store": True} if params.get("clear_after_store") else {}
            wip = Path(mic.save_project(sdir, proj, **kw))
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc, mic)
        paths = [str(wip)]
        data: dict[str, Any] = {"device_id": dev_id, "project_path": str(wip)}
        if not explicit:
            side = wip.with_suffix(".json")
            sidecar = {"schema": "mast.witec_project/1", "skill": name, "device_id": dev_id, "time_utc": _utc(),
                       "project": wip.name, "microscope_status": json_safe(mic.microscope_status()),
                       "optical_state": json_safe(optical_state.get_store().snapshot()), "scope": scope_ids()}
            try:
                atomic_write_bytes(side, dump_json_bytes(sidecar))
                stored = store_in_experiment([wip, side], skill=name)
                paths = stored["paths"]
                data["stored_in_experiment"] = bool(stored.get("stored"))
            except Exception as exc:  # noqa: BLE001
                data["archive_error"] = f"{type(exc).__name__}: {exc}"
        data["project_path"], data["paths"] = paths[0], paths
        return _ok(name, mic, data, f"{dev_id}: project -> {Path(paths[0]).name}")


# ── stop ──────────────────────────────────────────────────────────────────────────────────────────────────────


class StopWITec(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="StopWITec", version="1.0.0", category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=tr("立即停下 WITec：先关选中激光的快门，再停序列器（采谱/系列），再停 XY 和 Z 轴，最后逐台去关其他激光的快门并把选择放回去。"
                           "每一步各自尝试，互不影响。不给 device_id 就停所有已配置的显微镜。不需要仪器令牌，永不被拒。"
                           "WITec 只在持有写权限时接受这些命令：写权限不在手上时先静默要一次（操作员点过的 COM Automation 还在就拿得回）；"
                           "拿不回时结果会列出哪几步没做成，此时请操作员在 WITec Control 里手动停。如果激光选择没能放回原处，结果的 notes 会说明：之后先用 SelectWITecLaser 明确选一台，才能再开快门或加功率。",
                           "Stop WITec immediately: close the selected laser's shutter, stop the sequencer (spectrum/series), stop the XY and Z axes, then visit each other laser, close its shutter and put the selection back. "
                           "Each step is attempted on its own. Without device_id, stops every configured microscope. Needs no instrument token and is never refused. "
                           "WITec only accepts these commands while we hold write access: if it is not held it is asked for once, silently (the operator's COM Automation grant is still good unless revoked); "
                           "if that fails the result lists which steps did not happen - then ask the operator to stop by hand in WITec Control. If the laser selection could not be put back, the result's notes say so: choose a laser with SelectWITecLaser before opening a shutter or raising power again."),
            parameters=[ParameterSpec(name="device_id", type="str", required=False, default="",
                                      description=tr("只停这一台；留空 = 全部", "stop only this one; empty = all"))],
            estimated_duration_s=4.0, composition_level=0, tags=[*_TAGS, "stop", "safety", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        reg = get_instrument_registry()
        ids = [str(params["device_id"])] if params.get("device_id") else \
            [str(d.get("id")) for d in reg.find_devices(kind="microscope")]
        reports: dict[str, Any] = {}
        problems: dict[str, str] = {}
        notes: dict[str, str] = {}
        last_mic = None
        for dev in ids:
            try:
                mic = reg.microscope(dev)
                last_mic = mic
                _note_stop(mic, "StopWITec", context)
                mic.stop_all()
                rep = getattr(mic, "last_stop_report", None)
                reports[dev] = json_safe(rep) if rep else {"stop": "sent"}
                bad = {k: v for k, v in (rep or {}).items() if isinstance(v, str) and "not " in v}
                if rep and (rep.get("connected") is False):
                    problems[dev] = str(rep.get("error") or "not connected")
                elif bad:
                    problems[dev] = "; ".join(f"{k}: {v}" for k, v in bad.items())
                note = _selection_note(rep)
                if note:
                    notes[dev] = note
            except Exception as exc:  # noqa: BLE001 - a stop is never refused, only reported
                problems[dev] = f"{type(exc).__name__}: {exc}"
        ok = not problems
        return SkillResult(
            skill_name="StopWITec", success=ok,
            data={"stopped": [d for d in ids if d not in problems], "problems": problems, "reports": reports,
                  **({"notes": notes} if notes else {}), **_extras(last_mic if len(ids) == 1 else None)},
            error="" if ok else tr(f"停止没有完全送达：{problems}。请操作员在 WITec Control 里手动停止 / 关快门。",
                                   f"the stop did not fully reach WITec: {problems}. Ask the operator to stop / close the shutter by hand in WITec Control."),
            summary=f"stop sent to {ids or 'nothing (no microscope configured)'}" + ("" if ok else " - INCOMPLETE")
            + ("".join(f" - NOTE ({d}): {n}" for d, n in notes.items())))
