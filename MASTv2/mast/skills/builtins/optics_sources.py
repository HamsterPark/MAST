"""Light-source, power-meter and temperature skills (NKT SuperK, NPI Rainbow, Thorlabs PM, TC100).

Contract: ``docs/v2/design/optical_station_witec.md`` §6.3 (names and safety
levels are the contract). This module is imported by ``skills/builtins/__init__.py`` so the frozen
build discovers it (core/registry.py frozen fallback).

======================  ========  =========================================================
skill                   safety    what it does
======================  ========  =========================================================
GetLaserStatus          AUTO      driver status + the operator-declared optical state
ReadOpticalPower        AUTO      power meter reading (W / mW / dBm)
ReadTemperature         AUTO      PV / SV / MV / state of a temperature controller
IdentifyOpticalSource   AUTO      read-only self-check of the configured sources
SetLightSourcePower     CONFIRM   SELECT channel amplitude / main power (limits in the driver)
SetLaserWavelength      CONFIRM   SELECT channel wavelength (+ optional crystal switch)
SetTemperature          CONFIRM   setpoint (+ ramp) within the configured limits
WaitTemperatureStable   CONFIRM   wait until PV stays within a tolerance for hold_s
LaserEmissionOn         CONFIRM   needs allow_emission_control AND a valid operator attestation
LaserEmissionOff        AUTO      the safe direction: never refused, never takes the token
StopTemperatureRamp     AUTO      never refused, never takes the token (a TC100 whose writes are off is not written)
======================  ========  =========================================================

Rules that hold for every skill here
------------------------------------
* ``device_id`` may be left out when exactly one enabled device of that kind is configured; with
  several it is required and the skill refuses (no guessing which laser to act on). Exceptions:
  the two safe-direction skills act on **every** configured device of the kind when it is left out
  (a refusal must never be the reason a laser stays on), and the read-only self-check
  ``IdentifyOpticalSource`` checks them all.
* **Nothing here can write the operator's attestation.** ``laser_area_safe`` is checked
  (:meth:`OpticalStateStore.is_valid`) and never set: no parameter, no code path. Only the
  operator's web/launcher channels write it (a test pins that this module never calls the writers).
  The real laser drivers enforce the same attestation themselves (``Laser.emission_guard``, installed by
  their ``build()``), so a protocol that skips these skills and calls the driver is stopped too; the skill
  checks first only because it can tell the agent who must do what.
* Limits are the drivers' (Layer 0): out of range is refused, never clamped.
* ``LaserEmissionOff`` / ``StopTemperatureRamp`` first tell the device the stop was asked for from outside a run
  (``note_external_stop("<skill> (<caller>)")``, before they act), so a running protocol can see that someone
  pressed stop without having to read a state back - a TC100 without a status register has none to read.
* ``LaserEmissionOff`` / ``StopTemperatureRamp`` carry the ``emergency`` tag, which is how
  ``core.instrument_lock.needs_token`` knows a remedy must run while another run holds the
  instrument (same as StopAutoApproach); success is only reported when the device read back as
  off / stopped - "sent" is not "off".
* The commissioning order on the real hardware: ``enabled: true`` with every gate closed
  (``write_enabled: false``, ``allow_emission_control: false``) -> ``IdentifyOpticalSource`` ->
  operator reads the report -> gates opened one by one in the config.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from typing import Any

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
    OperationNotSupported,
)
from mast.instruments.config import device_kind
from mast.instruments.laser import (
    EmissionControlDisabled,
    LaserAttestationRequired,
    LaserLimitError,
    TunableLaser,
)
from mast.instruments.power_meter import PowerMeterError
from mast.instruments.registry import get_instrument_registry
from mast.instruments.safe_direction import safe_direction
from mast.instruments.temperature import TemperatureLimitError, TemperatureWriteDisabled
from mast.skills.base import BaseSkill
from mast.skills.builtins.optics_common import abort_probe, json_safe, resolve_device

logger = logging.getLogger(__name__)

__all__ = [
    "GetLaserStatus",
    "ReadOpticalPower",
    "ReadTemperature",
    "IdentifyOpticalSource",
    "SetLightSourcePower",
    "SetLaserWavelength",
    "SetTemperature",
    "WaitTemperatureStable",
    "LaserEmissionOn",
    "LaserEmissionOff",
    "StopTemperatureRamp",
]

#: skill names of this module (tool-face / hardware-module tables key on names)
SOURCE_SKILLS: tuple[str, ...] = tuple(__all__)
#: the safe-direction skills (carry the ``emergency`` tag: no instrument token, never refused)
SAFE_DIRECTION_SKILLS: tuple[str, ...] = ("LaserEmissionOff", "StopTemperatureRamp")

#: the operator-owned slot that gates software emission (docs §6.2 / D7)
ATTESTATION_SLOT = "laser_area_safe"

#: caps of WaitTemperatureStable (a wait must have an end)
MAX_WAIT_TIMEOUT_S = 6 * 3600.0
MAX_HOLD_S = 3600.0
#: consecutive failed reads WaitTemperatureStable tolerates before giving up
MAX_READ_FAILURES = 3

#: tests replace these so waits run on a fake clock
_sleep = time.sleep
_monotonic = time.monotonic

_KIND_ACCESSOR = {"laser": "laser", "power_meter": "power_meter", "temperature": "temperature_controller"}


# ── shared plumbing ───────────────────────────────────────────────────────────────────────


def _device_id_param(kind_text_zh: str, kind_text_en: str) -> ParameterSpec:
    return ParameterSpec(
        name="device_id", type="str", required=False,
        description=tr(f"{kind_text_zh}的设备 id（optical_instruments.json）。只配了一台时可省略；配了多台就必须指定，"
                       "否则拒绝（不替你猜动哪一台）。",
                       f"Device id of the {kind_text_en} (optical_instruments.json). May be omitted when exactly one "
                       "is configured; with several it is required and the skill refuses (it will not guess)."))


def _fail(skill: str, exc: BaseException, **data: Any) -> SkillResult:
    """One translation from the driver exception family to a failed result."""
    if isinstance(exc, OperationNotSupported):
        msg = tr(f"不支持：{exc} —— 驱动没有出处的做法，这不是参数问题，不要重试。",
                 f"not supported: {exc} - the driver has no sourced way to do this; not a parameter problem, "
                 "do not retry.")
    elif isinstance(exc, LaserAttestationRequired):
        msg = tr(f"拒绝：{exc}", f"refused: {exc}")           # the guard's text already says who must do what
    elif isinstance(exc, (EmissionControlDisabled, TemperatureWriteDisabled)):
        msg = tr(f"拒绝：{exc} —— 这是设备配置里的开关，实机自检通过后由操作员打开，不是参数问题。",
                 f"refused: {exc} - this is a switch in the device config that the operator turns on after the "
                 "bench self-test; not a parameter problem.")
    elif isinstance(exc, (LaserLimitError, TemperatureLimitError)):
        msg = tr(f"超出限值，已拒绝（不会钳位）：{exc}", f"refused by limit (never clamped): {exc}")
    elif isinstance(exc, InstrumentUnavailable):
        msg = tr(f"设备不可用：{exc}", f"device unavailable: {exc}")
    elif isinstance(exc, PowerMeterError):
        msg = tr(f"功率计拒绝：{exc}", f"power meter refused: {exc}")
    elif isinstance(exc, InstrumentError):
        msg = str(exc)
    else:
        msg = f"{type(exc).__name__}: {exc}"
    return SkillResult(skill_name=skill, success=False, error=msg, data=json_safe(data))


def _device(kind: str, device_id: Any) -> tuple[str, Any]:
    """``(id, driver)`` for *kind*; raises the InstrumentError family on any problem."""
    reg = get_instrument_registry()
    wanted = str(device_id).strip() if device_id else ""
    dev_id = resolve_device(reg, kind=kind, role=None, device_id=wanted or None)
    return dev_id, getattr(reg, _KIND_ACCESSOR[kind])(dev_id)


def _safe_targets(kind: str, device_id: Any) -> list[str]:
    reg = get_instrument_registry()
    if device_id and str(device_id).strip():
        return [str(device_id).strip()]
    return [str(e.get("id")) for e in reg.find_devices(kind=kind)]


def _operator_state() -> dict:
    """The declared optical state that matters to the sources (read only)."""
    try:
        from mast.operator_hitl.optical_state import get_store

        snap = get_store().snapshot()
        return {k: snap[k] for k in (ATTESTATION_SLOT, "npi_power_pct") if k in snap}
    except Exception as exc:  # noqa: BLE001 - informative only
        return {"error": f"{type(exc).__name__}: {exc}"}


_TAGS = ["optics", "source"]


def _meta(name: str, *, category: SkillCategory, safety_level: SafetyLevel, description: str,
          parameters: list[ParameterSpec], tags: list[str], duration: float = 1.0) -> SkillMetadata:
    return SkillMetadata(name=name, version="1.0.0", category=category, safety_level=safety_level,
                         description=description, parameters=parameters, estimated_duration_s=duration,
                         composition_level=0, tags=_TAGS + tags)


# ── reads ─────────────────────────────────────────────────────────────────────────────────


class GetLaserStatus(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "GetLaserStatus", category=SkillCategory.READ, safety_level=SafetyLevel.AUTO,
            description=tr("读一台光源（NKT SuperK / NPI Rainbow）的状态：出光与否、各 LED/通道、联锁、温度，以及它能被软件控制的哪些量"
                           "和限值；另附操作员签署的光学状态（激光区安全是否有效、NPI 泵浦电流 % 是人手设的声明值，不是读数）。"
                           "只读。",
                           "Read the state of a light source (NKT SuperK / NPI Rainbow): emission, LEDs / channels, "
                           "interlock, temperatures, what the software may control and the limits - plus the "
                           "operator-declared optical state (whether the laser-area-safe attestation is valid; the NPI "
                           "pump-current % is a declared value set by hand, not a reading). Read-only."),
            parameters=[_device_id_param("光源", "light source")], tags=["laser", "read"])

    def execute(self, context, params: dict) -> SkillResult:
        dev_id = None
        try:
            dev_id, dev = _device("laser", params.get("device_id"))
            st = dev.status()
            emission = st["emission"] if "emission" in st else dev.emission_state()
            return SkillResult(
                skill_name="GetLaserStatus", success=True,
                data=json_safe({"device_id": dev_id, "status": st, "supports": sorted(dev.SUPPORTS),
                                "limits": dev.limits.as_dict(), "emission": emission,
                                "operator_state": _operator_state()}),
                summary=f"{dev_id}: emission={emission!r}")
        except Exception as exc:  # noqa: BLE001
            return _fail("GetLaserStatus", exc, device_id=dev_id)


class ReadOpticalPower(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "ReadOpticalPower", category=SkillCategory.READ, safety_level=SafetyLevel.AUTO,
            description=tr("用功率计（Thorlabs PM）读一次光功率（可对多次读数取平均）。给 wavelength_nm 就先把功率计的修正波长设成它"
                           "（只改功率计的修正，不动任何光源）。返回 W、mW 和 dBm。",
                           "Read the optical power with the power meter (Thorlabs PM), optionally averaging several "
                           "readings. With wavelength_nm the meter's correction wavelength is set first (this only "
                           "changes the meter's correction, no light source is touched). Returns W, mW and dBm."),
            parameters=[
                _device_id_param("功率计", "power meter"),
                ParameterSpec(name="wavelength_nm", type="float", required=False, unit="nm",
                              description=tr("功率计的修正波长（要与被测光一致，否则读数有系统误差）；省略则保持功率计当前设置",
                                             "Correction wavelength of the meter (must match the light measured, "
                                             "or the reading is biased); omitted = keep the meter's setting")),
                ParameterSpec(name="averages", type="int", required=False, default=1, min_value=1, max_value=100,
                              description=tr("取平均的读数个数", "Number of readings to average"))],
            tags=["power_meter", "read"], duration=2.0)

    def execute(self, context, params: dict) -> SkillResult:
        dev_id = None
        try:
            dev_id, dev = _device("power_meter", params.get("device_id"))
            n = int(params.get("averages") or 1)
            w = dev.read_power_w(wavelength_nm=params.get("wavelength_nm"), averages=n)
            data: dict[str, Any] = {"device_id": dev_id, "power_w": w, "power_mw": w * 1e3, "averages": n}
            data["power_dbm"] = 10.0 * math.log10(w * 1e3) if w > 0 else None
            try:
                data["wavelength_nm"] = dev.status().get("wavelength_nm")
            except Exception:  # noqa: BLE001 - the reading is the point
                pass
            return SkillResult(skill_name="ReadOpticalPower", success=True, data=json_safe(data),
                               summary=f"{dev_id}: {w * 1e3:.4g} mW")
        except Exception as exc:  # noqa: BLE001
            return _fail("ReadOpticalPower", exc, device_id=dev_id)


class ReadTemperature(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "ReadTemperature", category=SkillCategory.READ, safety_level=SafetyLevel.AUTO,
            description=tr("读温控器（TC100）：测量温度 PV、设定值 SV、输出 MV 和运行状态（running/stopped/holding/unknown）。只读。"
                           "TC100 的寄存器表没有厂家文档，未在设备配置里填好 register_map 时这里会明确报“不支持”。",
                           "Read a temperature controller (TC100): measured temperature PV, set value SV, output MV and "
                           "run state (running/stopped/holding/unknown). Read-only. The TC100's register map has no "
                           "vendor documentation: until register_map is filled into the device config this reports "
                           "'not supported' explicitly."),
            parameters=[_device_id_param("温控器", "temperature controller")], tags=["temperature", "read"])

    def execute(self, context, params: dict) -> SkillResult:
        dev_id = None
        try:
            dev_id, dev = _device("temperature", params.get("device_id"))
            st = dev.status()
            return SkillResult(skill_name="ReadTemperature", success=True,
                               data=json_safe({"device_id": dev_id, **st}),
                               summary=f"{dev_id}: PV={st.get('pv_c')} °C, state: {st.get('state')}")
        except Exception as exc:  # noqa: BLE001
            return _fail("ReadTemperature", exc, device_id=dev_id)


class IdentifyOpticalSource(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "IdentifyOpticalSource", category=SkillCategory.READ, safety_level=SafetyLevel.AUTO,
            description=tr("光源/功率计/温控器的只读自检：只发有出处的读命令，报告型号、序列号、模块类型、状态和“还缺什么”"
                           "（例如 TC100 没填寄存器表、NKT 主机是 0x88 所以写被关）。实机上启用写操作之前的第一步。"
                           "device_id 省略则检查所有已启用的此类设备；配置里 enabled=false 的设备只列出、不碰。",
                           "Read-only self-check of the light sources / power meters / temperature controllers: sends "
                           "only sourced read commands and reports model, serial number, module types, state and what "
                           "is still missing (e.g. the TC100 has no register map, the NKT main board is type 0x88 so "
                           "writes stay off). The first step before enabling writes on the real hardware. Without "
                           "device_id every enabled device of the kinds is checked; devices with enabled=false are "
                           "listed and not touched."),
            parameters=[
                ParameterSpec(name="device_id", type="str", required=False,
                              description=tr("只查这一台；省略 = 查所有已启用的光源/功率计/温控器",
                                             "Check only this device; omitted = every enabled source / power meter / "
                                             "temperature controller")),
                ParameterSpec(name="kind", type="str", required=False,
                              allowed_values=["laser", "power_meter", "temperature"],
                              description=tr("只查这一类", "Restrict to one kind"))],
            tags=["identify", "read"], duration=10.0)

    def execute(self, context, params: dict) -> SkillResult:
        try:
            reg = get_instrument_registry()
            want_id = (str(params.get("device_id") or "").strip()) or None
            kinds = [params["kind"]] if params.get("kind") else list(_KIND_ACCESSOR)
            entries = [(device_kind(str(e.get("type"))), e) for e in reg.device_configs()
                       if device_kind(str(e.get("type"))) in kinds and (want_id is None or e.get("id") == want_id)]
            if want_id and not entries:
                raise InstrumentError(f"no light-source / power-meter / temperature device with id {want_id!r} "
                                      "in optical_instruments.json")
            devices = []
            for kind, e in entries:
                item: dict[str, Any] = {"device_id": e.get("id"), "kind": kind, "type": e.get("type"),
                                        "enabled": bool(e.get("enabled", True))}
                if not item["enabled"]:
                    item["ok"] = None
                    item["note"] = tr("配置里 enabled=false：没碰。要自检就先改成 enabled=true，并保持 write_enabled=false、"
                                      "allow_emission_control=false（只读）。",
                                      "enabled=false in the config: not touched. To self-check it set enabled=true "
                                      "and keep write_enabled=false and allow_emission_control=false (read-only).")
                    devices.append(item)
                    continue
                try:
                    dev = getattr(reg, _KIND_ACCESSOR[kind])(str(e.get("id")))
                    ident = dev.identify() if callable(getattr(dev, "identify", None)) else dev.status()
                    item["identity"] = ident
                    item["ok"] = bool(ident.get("ok", True)) if isinstance(ident, dict) else True
                    item["gates"] = {k: getattr(dev, k) for k in ("write_enabled", "allow_emission_control")
                                     if hasattr(dev, k)}
                    if hasattr(dev, "emission_guard"):                  # a laser: does the driver check the attestation?
                        item["gates"]["operator_guard"] = dev.emission_guard is not None
                except Exception as exc:  # noqa: BLE001 - one dead device must not hide the others
                    item.update(ok=False, error=str(exc), error_type=type(exc).__name__)
                devices.append(item)
            bad = [d["device_id"] for d in devices if d.get("ok") is False]
            return SkillResult(
                skill_name="IdentifyOpticalSource", success=not bad,
                error=(tr(f"自检未通过：{bad}", f"self-check failed for: {bad}") if bad else ""),
                data=json_safe({"devices": devices}),
                summary=f"{len(devices)} device(s), {len(bad)} failing")
        except Exception as exc:  # noqa: BLE001
            return _fail("IdentifyOpticalSource", exc)


# ── settings ──────────────────────────────────────────────────────────────────────────────


class SetLightSourcePower(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "SetLightSourcePower", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("设光源的功率。可调谐源（NKT SELECT）：设某个通道的幅度（%）；有软件功率控制的单波长激光：设功率。"
                           "上限来自设备配置（max_amplitude_pct / max_power），超限直接拒绝、不钳位；设备配置里 write_enabled 关着也拒绝。"
                           "NPI 1064 没有软件功率命令：它的泵浦电流 % 由人在 Rainbow OEM 里手设，由操作员声明（npi_power_pct），这里会拒绝。"
                           "让样品上的光变多的设置（通道从暗到亮、已亮的通道调大、主输出电平调大）和开出光一样要过闸："
                           "设备配置 allow_emission_control 打开 + 操作员签署“激光区安全”；调小、调到 0、改波长不用。",
                           "Set a light source's power. Tunable source (NKT SELECT): the amplitude (%) of one channel; "
                           "a single-line laser with software power control: its power. The cap comes from the device "
                           "config (max_amplitude_pct / max_power); anything above is refused, never clamped; refused as "
                           "well while write_enabled is off in the config. The NPI 1064 has no software power command: "
                           "its pump-current % is set by hand in Rainbow OEM and declared by the operator "
                           "(npi_power_pct), so this refuses. A setting that puts MORE light on the sample (a channel "
                           "opened, a lit channel made brighter, the output level raised) passes the same gates as "
                           "emission on - allow_emission_control in the device config AND the operator's 'laser area "
                           "safe' attestation; turning down, setting 0 or changing the wavelength does not."),
            parameters=[
                _device_id_param("光源", "light source"),
                ParameterSpec(name="power", type="float", required=True,
                              description=tr("功率值，单位见设备配置（可调谐源的通道幅度为 %）",
                                             "Power value, unit per the device config (channel amplitude of a tunable source is %)")),
                ParameterSpec(name="channel", type="int", required=False, default=0, min_value=0,
                              description=tr("可调谐源的通道号（SELECT 为 0–7）", "Channel of a tunable source (0-7 on the SELECT)")),
                ParameterSpec(name="target", type="str", required=False, default="auto",
                              allowed_values=["auto", "amplitude", "power"],
                              description=tr("auto：可调谐源改通道幅度，其它改功率；amplitude / power 强制选其一",
                                             "auto: channel amplitude for a tunable source, power for the others; "
                                             "amplitude / power force one"))],
            tags=["laser", "write"], duration=1.0)

    def execute(self, context, params: dict) -> SkillResult:
        dev_id = None
        try:
            dev_id, dev = _device("laser", params.get("device_id"))
            target = params.get("target") or "auto"
            channel = int(params.get("channel") or 0)
            use_amp = (target == "amplitude") or (target == "auto" and isinstance(dev, TunableLaser)
                                                  and "amplitude" in dev.SUPPORTS)
            if use_amp:
                if not isinstance(dev, TunableLaser):
                    raise LaserLimitError(f"{dev_id}: no channel amplitude on this laser")
                applied = dev.set_amplitude_pct(params["power"], channel=channel)
                data = {"device_id": dev_id, "target": "amplitude", "channel": channel, "requested": params["power"],
                        "applied": applied, "unit": "%"}
            else:
                if "power" not in dev.SUPPORTS:
                    raise LaserLimitError(tr(
                        f"{dev_id}: 这台激光没有软件功率控制。NPI 1064 的泵浦电流 % 请操作员在 Rainbow OEM 里设置并声明"
                        "（mast_ask_operator → 光学状态 npi_power_pct）。",
                        f"{dev_id}: this laser has no software power control. For the NPI 1064 the operator sets the "
                        "pump-current % in Rainbow OEM and declares it (mast_ask_operator -> optical state npi_power_pct)."))
                applied = dev.set_power(params["power"])
                data = {"device_id": dev_id, "target": "power", "requested": params["power"], "applied": applied,
                        "unit": dev.limits.power_unit}
            return SkillResult(skill_name="SetLightSourcePower", success=True, data=json_safe(data),
                               summary=f"{dev_id}: {data['target']} = {applied} {data['unit']}")
        except Exception as exc:  # noqa: BLE001
            return _fail("SetLightSourcePower", exc, device_id=dev_id)


class SetLaserWavelength(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "SetLaserWavelength", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("设可调谐光源（NKT SuperK SELECT）某个通道的波长。范围受设备配置限值与当前接上的晶体可用范围双重限制，"
                           "超出就拒绝（不钳位，也不会悄悄换晶体）。要换晶体就显式给 crystal=1 或 2：换晶体前 RF 功率会被关掉，"
                           "换完保持关闭——用 LaserEmissionOn 重新打开输出。write_enabled 关着时拒绝。",
                           "Set the wavelength of one channel of a tunable source (NKT SuperK SELECT). The range is "
                           "limited by the device config and by the usable range of the crystal that is connected; "
                           "outside it the request is refused (never clamped, and a crystal is never switched silently). "
                           "To switch crystal pass crystal=1 or 2 explicitly: RF power is turned off before the switch "
                           "and stays off afterwards - reopen the output with LaserEmissionOn. Refused while "
                           "write_enabled is off."),
            parameters=[
                _device_id_param("可调谐光源", "tunable source"),
                ParameterSpec(name="wavelength_nm", type="float", required=True, unit="nm",
                              description=tr("目标波长", "Target wavelength")),
                ParameterSpec(name="channel", type="int", required=False, default=0, min_value=0,
                              description=tr("通道号（SELECT 为 0–7）", "Channel (0-7 on the SELECT)")),
                ParameterSpec(name="crystal", type="int", required=False, allowed_values=[1, 2],
                              description=tr("先把 RF 切到这块晶体（1 或 2）；省略 = 用当前接上的那块",
                                             "Switch the RF to this crystal first (1 or 2); omitted = use the one connected"))],
            tags=["laser", "write", "wavelength"], duration=2.0)

    def execute(self, context, params: dict) -> SkillResult:
        dev_id = None
        try:
            dev_id, dev = _device("laser", params.get("device_id"))
            if not isinstance(dev, TunableLaser):
                raise LaserLimitError(tr(f"{dev_id}: 这台激光的波长是固定的（不是可调谐源）。",
                                         f"{dev_id}: the wavelength of this laser is fixed (not a tunable source)."))
            channel = int(params.get("channel") or 0)
            data: dict[str, Any] = {"device_id": dev_id, "channel": channel, "requested": params["wavelength_nm"]}
            if params.get("crystal") is not None:
                switch = getattr(dev, "select_crystal", None)
                if not callable(switch):
                    raise OperationNotSupported(f"{dev_id}: this source has no crystal switch")
                data["crystal_switch"] = switch(int(params["crystal"]))
            data["applied"] = dev.set_wavelength_nm(params["wavelength_nm"], channel=channel)
            if data.get("crystal_switch", {}).get("changed"):
                data["note"] = tr("RF 功率已关闭（换晶体）；设好通道后用 LaserEmissionOn 重新打开输出。",
                                  "RF power was turned off for the crystal switch; set the channels, then reopen "
                                  "the output with LaserEmissionOn.")
            return SkillResult(skill_name="SetLaserWavelength", success=True, data=json_safe(data),
                               summary=f"{dev_id} ch{channel}: {data['applied']} nm")
        except Exception as exc:  # noqa: BLE001
            return _fail("SetLaserWavelength", exc, device_id=dev_id)


class SetTemperature(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "SetTemperature", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("给温控器设目标温度（可带升降温速率 °C/min）并启动。温度范围与速率上限来自设备配置，超限拒绝、不钳位；"
                           "没给速率时用配置里的 max_ramp_c_per_min。write_enabled 关着、或 TC100 的寄存器表没配时拒绝。"
                           "之后用 WaitTemperatureStable 等稳定，不要假设温度已经到了。",
                           "Set a temperature controller's target (optionally with a ramp rate in °C/min) and start it. "
                           "The range and the ramp cap come from the device config; beyond them the request is refused, "
                           "never clamped; without a rate the config's max_ramp_c_per_min is used. Refused while "
                           "write_enabled is off or the TC100 register map is not configured. Use WaitTemperatureStable "
                           "afterwards - do not assume the temperature has arrived."),
            parameters=[
                _device_id_param("温控器", "temperature controller"),
                ParameterSpec(name="setpoint_c", type="float", required=True, unit="°C",
                              description=tr("目标温度", "Target temperature")),
                ParameterSpec(name="ramp_c_per_min", type="float", required=False, unit="°C/min", min_value=0.001,
                              description=tr("升降温速率；省略 = 用配置的上限", "Ramp rate; omitted = the configured cap"))],
            tags=["temperature", "write"], duration=1.0)

    def execute(self, context, params: dict) -> SkillResult:
        dev_id = None
        try:
            dev_id, dev = _device("temperature", params.get("device_id"))
            res = dev.set_setpoint(params["setpoint_c"], ramp_c_per_min=params.get("ramp_c_per_min"))
            data: dict[str, Any] = {"device_id": dev_id, "requested_c": params["setpoint_c"], **res}
            try:
                data["status_after"] = dev.status()
            except Exception as exc:  # noqa: BLE001 - the write is the point
                data["status_after_error"] = str(exc)
            return SkillResult(skill_name="SetTemperature", success=True, data=json_safe(data),
                               summary=f"{dev_id}: target {params['setpoint_c']} °C")
        except Exception as exc:  # noqa: BLE001
            return _fail("SetTemperature", exc, device_id=dev_id)


class WaitTemperatureStable(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "WaitTemperatureStable", category=SkillCategory.READ, safety_level=SafetyLevel.CONFIRM,
            description=tr("阻塞等待温度稳定：测量温度 PV 连续 hold_s 秒都落在目标 ±tolerance_c 之内才算到达（中途出界就重新计时）。"
                           "有硬上限 timeout_s（最多 6 小时），随时可被中止；超时、被中止、控制器已停止都返回失败，不会假装到了。"
                           "只读，不改任何设置。target_c 省略时用控制器被要求去的最终目标（驱动不报目标时才用设定值 SV）。",
                           "Block until the temperature is stable: the measured PV must stay within target ±tolerance_c "
                           "for hold_s seconds in a row (leaving the band restarts the timer). Hard cap timeout_s (at "
                           "most 6 h), abortable at any time; a timeout, an abort or a stopped controller returns "
                           "failure - it never pretends to have arrived. Read-only; changes no setting. Without target_c "
                           "the final target the controller was asked for is used (its set value SV if the driver "
                           "reports no target)."),
            parameters=[
                _device_id_param("温控器", "temperature controller"),
                ParameterSpec(name="target_c", type="float", required=False, unit="°C",
                              description=tr("等待的目标温度；省略 = 控制器被要求去的最终目标",
                                             "Target to wait for; omitted = the final target the controller was asked for")),
                ParameterSpec(name="tolerance_c", type="float", required=False, default=0.5, unit="°C",
                              min_value=0.01, max_value=50.0,
                              description=tr("允许的偏差（±）", "Allowed deviation (±)")),
                ParameterSpec(name="hold_s", type="float", required=False, default=60.0, unit="s",
                              min_value=0.0, max_value=MAX_HOLD_S,
                              description=tr("必须连续保持在带内的时间", "Time the PV must stay inside the band")),
                ParameterSpec(name="timeout_s", type="float", required=False, default=1800.0, unit="s",
                              min_value=1.0, max_value=MAX_WAIT_TIMEOUT_S,
                              description=tr("总等待上限（硬上限 6 小时）", "Total wait cap (hard cap 6 hours)")),
                ParameterSpec(name="poll_s", type="float", required=False, default=2.0, unit="s",
                              min_value=0.05, max_value=60.0,
                              description=tr("读温度的间隔", "Interval between temperature reads"))],
            tags=["temperature", "wait"], duration=600.0)

    def execute(self, context, params: dict) -> SkillResult:
        dev_id = None
        try:
            dev_id, dev = _device("temperature", params.get("device_id"))
            tol = float(params.get("tolerance_c") if params.get("tolerance_c") is not None else 0.5)
            hold = float(params.get("hold_s") if params.get("hold_s") is not None else 60.0)
            timeout = float(params.get("timeout_s") if params.get("timeout_s") is not None else 1800.0)
            poll = float(params.get("poll_s") if params.get("poll_s") is not None else 2.0)
            if not (0.0 < tol <= 50.0 and 0.0 <= hold <= MAX_HOLD_S and 0.0 < timeout <= MAX_WAIT_TIMEOUT_S
                    and 0.0 < poll <= 60.0):
                raise TemperatureLimitError(
                    f"tolerance_c in (0, 50], hold_s in [0, {MAX_HOLD_S:g}], timeout_s in (0, {MAX_WAIT_TIMEOUT_S:g}], "
                    f"poll_s in (0, 60] - got {tol}, {hold}, {timeout}, {poll}")
            return self._wait(context, dev_id, dev, params.get("target_c"), tol, hold, timeout, poll)
        except Exception as exc:  # noqa: BLE001
            return _fail("WaitTemperatureStable", exc, device_id=dev_id)

    def _wait(self, context, dev_id: str, dev: Any, target_c: Any, tol: float, hold: float,
              timeout: float, poll: float) -> SkillResult:
        probe = abort_probe(context)
        t0 = _monotonic()
        in_band_since: float | None = None
        hold_pv: list[float] = []
        trace: list[list[float]] = []
        failures = 0
        explicit = None if target_c is None else float(target_c)
        target = explicit
        base = {"device_id": dev_id, "tolerance_c": tol, "hold_s": hold, "timeout_s": timeout}

        def result(ok: bool, why: str, pv: float | None) -> SkillResult:
            data = {**base, "reached": ok, "target_c": target, "final_pv_c": pv,
                    "elapsed_s": round(_monotonic() - t0, 3), "trace": trace[-60:],
                    "hold_stats": ({"n": len(hold_pv), "min_c": min(hold_pv), "max_c": max(hold_pv),
                                    "mean_c": statistics.fmean(hold_pv),
                                    "std_c": statistics.pstdev(hold_pv) if len(hold_pv) > 1 else 0.0}
                                   if hold_pv else None)}
            return SkillResult(skill_name="WaitTemperatureStable", success=ok, error="" if ok else why,
                               data=json_safe(data),
                               summary=(f"{dev_id}: stable at {pv} °C" if ok else f"{dev_id}: {why}"))

        while True:
            if probe is not None and probe():
                return result(False, tr("已中止，温度没有确认稳定。", "aborted; the temperature was not confirmed stable."),
                              trace[-1][1] if trace else None)
            st = None
            try:
                st = dev.status()
                failures = 0
            except OperationNotSupported:
                raise
            except Exception as exc:  # noqa: BLE001
                failures += 1
                if failures >= MAX_READ_FAILURES:
                    return result(False, tr(f"连续 {failures} 次读不到温度：{type(exc).__name__}: {exc}",
                                            f"temperature unreadable {failures} times in a row: "
                                            f"{type(exc).__name__}: {exc}"),
                                  trace[-1][1] if trace else None)
            now = _monotonic()
            if st is not None:
                pv = st.get("pv_c")
                if pv is None:
                    return result(False, tr("控制器没有给出测量温度 PV。", "the controller reported no measured temperature."), None)
                pv = float(pv)
                trace.append([round(now - t0, 3), pv])
                if st.get("state") == "stopped":
                    return result(False, tr("控制器已停止，没有在保持任何设定值——不会等一个不会到来的稳定。",
                                            "the controller is stopped and holds no set value - not waiting for a "
                                            "stability that cannot come."), pv)
                if explicit is None:
                    # Which target? The FINAL one the controller was asked for (``target_c`` of the status) - a
                    # ramping controller's SV moves along the ramp and PV tracking it is not "arrived". Only when
                    # the driver reports no final target is the set value SV the best there is.
                    wanted = st.get("target_c") if st.get("target_c") is not None else st.get("sv_c")
                    if wanted is None:
                        raise InstrumentError(tr("没给 target_c，而且控制器读不出目标/设定值。",
                                                 "no target_c given and the controller reports neither a target nor "
                                                 "a set value."))
                    if target is None or float(wanted) != target:        # first read, or somebody re-targeted
                        target, in_band_since, hold_pv = float(wanted), None, []
                if abs(pv - target) <= tol:
                    if in_band_since is None:
                        in_band_since, hold_pv = now, []
                    hold_pv.append(pv)
                    if now - in_band_since >= hold:
                        return result(True, "", pv)
                else:
                    in_band_since, hold_pv = None, []
            if now - t0 >= timeout:
                last_pv = trace[-1][1] if trace else None
                return result(False, tr(f"{timeout:g} s 内温度没有稳定在 {target} ±{tol} °C（最后一次 PV={last_pv}）。",
                                        f"temperature did not settle at {target} ±{tol} °C within {timeout:g} s "
                                        f"(last PV={last_pv})."), last_pv)
            # sleep in slices so an abort is noticed within a second
            remaining = poll
            while remaining > 0:
                step = min(1.0, remaining)
                _sleep(step)
                remaining -= step
                if probe is not None and probe():
                    break


# ── emission ──────────────────────────────────────────────────────────────────────────────


class LaserEmissionOn(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "LaserEmissionOn", category=SkillCategory.WRITE, safety_level=SafetyLevel.CONFIRM,
            description=tr("用软件开出光。两道闸缺一不可：①设备配置里 allow_emission_control=true（默认关）；"
                           "②操作员已在 MAST 界面里签署“激光区安全”（护目镜、光路封闭、无人在光路；默认 8 小时有效）。"
                           "agent 没有任何办法替操作员签署——缺签署时本技能拒绝并说明要谁去做什么（用 mast_ask_operator 请操作员签）。"
                           "成功后读回出光状态。关出光用 LaserEmissionOff（永不被拒）。",
                           "Switch light emission on from software. Both gates are required: (1) allow_emission_control="
                           "true in the device config (default off); (2) a valid operator attestation 'laser area safe' "
                           "(eye protection, beam path enclosed, nobody in the beam; valid 8 h by default) signed in the "
                           "MAST UI. An agent has no way to sign for the operator - without it this refuses and says who "
                           "must do what (ask with mast_ask_operator). The emission state is read back on success. Switch "
                           "off with LaserEmissionOff (never refused)."),
            parameters=[_device_id_param("激光", "laser")], tags=["laser", "write", "emission"], duration=10.0)

    def execute(self, context, params: dict) -> SkillResult:
        dev_id = None
        try:
            dev_id, dev = _device("laser", params.get("device_id"))
            if "emission" not in dev.SUPPORTS:
                raise EmissionControlDisabled(f"{dev_id}: no software emission control")
            from mast.operator_hitl.optical_state import get_store

            store = get_store()
            missing: list[str] = []
            if not store.is_valid(ATTESTATION_SLOT, expect=True):
                missing.append("operator_attestation")
            if not dev.allow_emission_control:
                missing.append("allow_emission_control")
            if missing:
                parts = []
                if "operator_attestation" in missing:
                    parts.append(tr("操作员还没有签署“激光区安全”（或签署已过期）——只有操作员能在 MAST 界面里签；"
                                    "请用 mast_ask_operator 请他签，签了再来。",
                                    "the operator has not signed 'laser area safe' (or the signature expired) - only "
                                    "the operator can sign it in the MAST UI; ask with mast_ask_operator, then retry."))
                if "allow_emission_control" in missing:
                    parts.append(tr("设备配置里 allow_emission_control 是关的——实机自检通过后由操作员改配置打开。",
                                    "allow_emission_control is off in the device config - the operator turns it on "
                                    "after the bench self-test."))
                return SkillResult(skill_name="LaserEmissionOn", success=False,
                                   error=tr("拒绝开出光：", "emission NOT switched on: ") + " ".join(parts),
                                   data=json_safe({"device_id": dev_id, "refused": missing,
                                                   "attestation_slot": ATTESTATION_SLOT,
                                                   "operator_state": _operator_state()}))
            dev.emission_on()
            state = dev.emission_state()
            att = store.get(ATTESTATION_SLOT)
            return SkillResult(
                skill_name="LaserEmissionOn", success=state is not False,
                error="" if state is not False else tr("开出光命令发了，但读回来仍是关。",
                                                       "the on command was sent but the read-back says off."),
                data=json_safe({"device_id": dev_id, "emission": state, "confirmed": state is True,
                                "attestation": ({"set_by": att.set_by, "set_at": att.set_at,
                                                 "valid_until": att.valid_until} if att else None),
                                "warning": getattr(dev, "emission_on_warning", None)}),
                summary=f"{dev_id}: emission {'ON' if state else 'state unknown' if state is None else 'OFF'}")
        except Exception as exc:  # noqa: BLE001
            return _fail("LaserEmissionOn", exc, device_id=dev_id)


class LaserEmissionOff(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "LaserEmissionOff", category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=tr("关出光（安全方向）：永不被拒——不需要签署、不看配置闸、不取仪器令牌。省略 device_id 就对所有已启用的激光都关。"
                           "只有读回确认已关才报成功；发了命令但读回不到（串口被占、没回复）会明确报“未确认”，此时请到现场用面板/钥匙关掉。",
                           "Switch light emission off (the safe direction): never refused - needs no attestation, ignores "
                           "the config gates, takes no instrument token. Without device_id it switches off every enabled "
                           "laser. Success is reported only when the read-back confirms 'off'; if the command went out but "
                           "could not be confirmed (port busy, no reply) it says 'NOT confirmed' explicitly - then switch "
                           "it off at the laser (panel / key)."),
            parameters=[_device_id_param("激光", "laser")], tags=["laser", "safety", "emergency", "stop"],
            duration=5.0)

    def execute(self, context, params: dict) -> SkillResult:
        return _safe_direction("LaserEmissionOff", "laser", params.get("device_id"), _off_one_laser,
                               _stop_reason("LaserEmissionOff", context))


def _stop_reason(skill: str, context: Any) -> str:
    """Who asked, for the device's external-stop counter: ``"<skill> (<caller>)"``."""
    owner = str(getattr(context, "owner", "") or "").strip() or "unknown caller"
    return f"{skill} ({owner})"


def _note_external_stop(dev: Any, reason: str) -> None:
    """Count this stop as asked for from outside a running protocol. Never raises."""
    note = getattr(dev, "note_external_stop", None)
    if callable(note):
        try:
            note(reason)
        except Exception:  # noqa: BLE001 - bookkeeping must never stop a stop
            logger.debug("note_external_stop failed", exc_info=True)


def _off_one_laser(dev_id: str, reason: str = "") -> dict:
    dev = get_instrument_registry().laser(dev_id)
    _note_external_stop(dev, reason)                 # BEFORE the command: a protocol must see it even if the command fails
    state = dev.emission_off()                      # never raises, never waits forever (None = NOT confirmed)
    item: dict[str, Any] = {"device_id": dev_id, "emission": state, "confirmed": state is False}
    report = getattr(dev, "last_off_report", None)
    if report:
        item["report"] = report
    if state is not False and not (report or {}).get("lock_timeout"):
        try:                                         # why not confirmed? (port busy, silent device ...)
            with safe_direction():                   # ... and this question must not hang on a busy port either
                dev.status()
        except Exception as exc:  # noqa: BLE001
            item["cause"] = f"{type(exc).__name__}: {exc}"
    return item


class StopTemperatureRamp(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return _meta(
            "StopTemperatureRamp", category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=tr("停温控器（安全方向）：永不被拒——不看 write_enabled、不取仪器令牌。省略 device_id 就停所有已启用的温控器。"
                           "只有读回运行状态是 stopped 才报成功；读不到（TC100 没配寄存器表、串口被占）就明确报“未确认”，此时请在"
                           "TC100 软件/面板上停。TC100 的 write_enabled 关着时（寄存器表还没经台上自检确认）MAST 不往它写停止值，同样报“未确认”，"
                           "要在 TC100 程序/面板上停。",
                           "Stop a temperature controller (the safe direction): never refused - ignores write_enabled, "
                           "takes no instrument token. Without device_id it stops every enabled controller. Success is "
                           "reported only when the read-back run state is 'stopped'; if that cannot be read (TC100 without "
                           "a register map, port busy) it says 'NOT confirmed' explicitly - then stop it in the TC100 "
                           "program / at the panel. A TC100 whose write_enabled is off (its register map not yet verified "
                           "on the bench) is not written to at all: the stop is reported as NOT confirmed and has to be "
                           "done in the TC100 program / at the panel."),
            parameters=[_device_id_param("温控器", "temperature controller")],
            tags=["temperature", "safety", "emergency", "stop"], duration=3.0)

    def execute(self, context, params: dict) -> SkillResult:
        return _safe_direction("StopTemperatureRamp", "temperature", params.get("device_id"), _stop_one_controller,
                               _stop_reason("StopTemperatureRamp", context))


def _stop_one_controller(dev_id: str, reason: str = "") -> dict:
    dev = get_instrument_registry().temperature_controller(dev_id)
    _note_external_stop(dev, reason)                 # BEFORE the command (see _off_one_laser)
    sent = dev.stop()                                # never raises, never waits forever (False = NOT confirmed)
    item: dict[str, Any] = {"device_id": dev_id, "state": None, "confirmed": False, "stop_sent": sent is not False}
    report = getattr(dev, "last_stop_report", None)
    if report:                                       # why it did not go out: busy, write_enabled off, line dead ...
        item["report"] = report
    try:
        with safe_direction():                       # reading the state back must not hang on a busy port either
            st = dev.status()
        item["state"], item["pv_c"] = st.get("state"), st.get("pv_c")
        item["confirmed"] = st.get("state") == "stopped"
    except Exception as exc:  # noqa: BLE001
        item["cause"] = f"{type(exc).__name__}: {exc}"
    return item


def _safe_direction(skill: str, kind: str, device_id: Any, one, reason: str = "") -> SkillResult:
    """Run the safe action on one device or on all of the kind; never refuse, never raise."""
    try:
        targets = _safe_targets(kind, device_id)
    except Exception as exc:  # noqa: BLE001
        return _fail(skill, exc)
    if not targets:
        return SkillResult(skill_name=skill, success=False,
                           error=tr(f"没有已启用的 {kind} 设备可操作。", f"no enabled {kind} device to act on."),
                           data={"devices": []})
    items = []
    for dev_id in targets:
        try:
            items.append(one(dev_id, reason))
        except Exception as exc:  # noqa: BLE001 - one unreachable device must not stop the others
            items.append({"device_id": dev_id, "confirmed": False, "cause": f"{type(exc).__name__}: {exc}"})
    unconfirmed = [i["device_id"] for i in items if not i.get("confirmed")]
    what_zh, what_en = (("关出光", "emission off") if kind == "laser" else ("停止", "stop"))
    err = ""
    if unconfirmed:
        err = tr(f"{what_zh}命令已尽力发出，但这些设备没有读回确认：{unconfirmed}。请到现场确认/手动处理，不要当作已经完成。",
                 f"the {what_en} command was sent as far as possible, but these devices are NOT confirmed: "
                 f"{unconfirmed}. Check them at the instrument; do not treat this as done.")
    return SkillResult(skill_name=skill, success=not unconfirmed, error=err,
                       data=json_safe({"devices": items, "unconfirmed": unconfirmed}),
                       summary=("all confirmed" if not unconfirmed else f"NOT confirmed: {unconfirmed}"))
