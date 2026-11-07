"""AOTF and PMT skills — satellite devices whose drivers are not ready yet.

3 skills: SetAotfWavelength (CONFIRM), ReadPmt (CONFIRM), SetPmtHighVoltage
(DANGEROUS).

* The Brimrose DS1 AOTF serial protocol is not supported yet: the satellite's ``ds1``
  driver answers every action except ``status`` / ``stop`` with ``not_supported``, and
  these skills say so plainly.
* SpectraSense / the PMT is phase P5 and ships disabled. A PMT's high voltage is
  a hardware hazard no Nanonis limit bounds — light on a biased photocathode
  destroys it — hence DANGEROUS (an explicit human decision every time). Setting
  0 V sends ``hv_off``; StopSatelliteDevices also stops the device.

Both are addressed through the hub directly (``hub.devices(kind=…)``), not
through optical_instruments.json: there is no MAST-side abstraction to add yet.
"""

from __future__ import annotations

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.skills.base import BaseSkill
from mast.skills.builtins.optics_common import fail, json_safe
from mast.i18n import tr

__all__ = ["SetAotfWavelength", "ReadPmt", "SetPmtHighVoltage", "satellite_endpoint"]


def satellite_endpoint(kind: str, satellite: str = "", device: str = ""):
    """The one satellite device of *kind* (optionally narrowed), as an endpoint."""
    from mast.instruments.remote import RemoteEndpoint, get_hub

    hub = get_hub()
    try:
        listed = list(hub.devices(kind=kind) or [])
    except Exception as exc:  # noqa: BLE001
        raise InstrumentUnavailable(f"satellite hub cannot list {kind} devices: {exc}") from exc
    hits = [(str(s), str((d or {}).get("id"))) for s, d in listed
            if (not satellite or str(s) == satellite)
            and (not device or str((d or {}).get("id")) == device)]
    if len(hits) == 1:
        return RemoteEndpoint(*hits[0])
    if not hits:
        raise InstrumentUnavailable(
            f"no {kind} device on a connected satellite"
            + (f" (satellite={satellite!r}, device={device!r})" if satellite or device else "")
            + " — the driver may be disabled in satellite.json (AOTF waits for the vendor "
              "protocol; PMT is phase P5)")
    raise InstrumentError(f"{len(hits)} {kind} devices match {hits} — pass satellite/device")


def _target_params() -> list[ParameterSpec]:
    return [
        ParameterSpec(name="satellite", type="str", required=False, default="",
                      description=tr("卫星 id；留空 = 唯一提供该设备的卫星", "Satellite id; empty = the only satellite that provides this device")),
        ParameterSpec(name="device", type="str", required=False, default="",
                      description=tr("卫星上的设备 id；留空 = 唯一一台该类设备", "Device id on the satellite; empty = the only device of this kind")),
    ]


class SetAotfWavelength(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetAotfWavelength", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("设置 AOTF（Brimrose DS1，接在光谱电脑（卫星）上）某一通道的波长或射频频率与幅度。"
                "wavelength_nm 与 frequency_mhz 二选一。卫星驱动目前对它回 not_supported —— "
                "那不是参数错误，不要重试。", "Set the wavelength or RF frequency and amplitude of one channel of the AOTF (Brimrose DS1, attached to the spectrometer computer (satellite)). "
"Give either wavelength_nm or frequency_mhz. The satellite driver currently answers not_supported -- "
"that is not a parameter error, do not retry.")),
            parameters=[
                *_target_params(),
                ParameterSpec(name="channel", type="int", required=False, default=1,
                              min_value=1, max_value=8, description=tr("AOTF 通道", "AOTF channel")),
                ParameterSpec(name="wavelength_nm", type="float", unit="nm", required=False,
                              min_value=200.0, max_value=5000.0, description=tr("透过波长（nm）", "Transmitted wavelength (nm)")),
                ParameterSpec(name="frequency_mhz", type="float", unit="MHz", required=False,
                              min_value=1.0, max_value=1000.0, description=tr("射频频率（MHz）", "RF frequency (MHz)")),
                ParameterSpec(name="amplitude_pct", type="float", unit="%", required=False,
                              min_value=0.0, max_value=100.0, description=tr("射频幅度（满量程百分比）", "RF amplitude (percent of full scale)")),
            ],
            estimated_duration_s=0.5, composition_level=0,
            tags=["optics", "aotf", "ters", "write", "optional-hardware"])

    def execute(self, context, params: dict) -> SkillResult:
        wl, fr = params.get("wavelength_nm"), params.get("frequency_mhz")
        if (wl is None) == (fr is None):
            return SkillResult(skill_name="SetAotfWavelength", success=False,
                               error="give exactly one of wavelength_nm / frequency_mhz")
        args = {"channel": int(params.get("channel") or 1)}
        args["wavelength_nm" if wl is not None else "frequency_mhz"] = float(wl if wl is not None else fr)
        if params.get("amplitude_pct") is not None:
            args["amplitude"] = float(params["amplitude_pct"]) / 100.0
        try:
            ep = satellite_endpoint("aotf", params.get("satellite") or "", params.get("device") or "")
            value = ep.call("set", args, timeout_s=10.0)
        except Exception as exc:  # noqa: BLE001
            return fail("SetAotfWavelength", exc, request=args)
        return SkillResult(skill_name="SetAotfWavelength", success=True,
                           data={"device": ep.label, "request": args, "result": json_safe(value)},
                           summary=f"AOTF {ep.label} ch{args['channel']} set")


class ReadPmt(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ReadPmt", version="1.0.0", category=SkillCategory.READ,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("读一次 PMT（SpectraSense，光谱仪侧出口）：积分 integration_ms 毫秒的计数。需要出口切到 side、"
                "PMT 高压已开。PMT 属第 5 阶段，默认关闭；驱动不在时报 unavailable / not_supported。", "Read the PMT once (SpectraSense, spectrometer side exit): counts integrated for integration_ms milliseconds. Requires the exit switched to side "
"and the PMT high voltage on. The PMT belongs to phase 5 and is off by default; when the driver is absent it reports unavailable / not_supported.")),
            parameters=[
                *_target_params(),
                ParameterSpec(name="integration_ms", type="float", unit="ms", required=False,
                              default=100.0, min_value=1.0, max_value=60_000.0,
                              description=tr("积分时间（毫秒）", "Integration time (milliseconds)")),
            ],
            estimated_duration_s=0.5, composition_level=0,
            tags=["optics", "pmt", "ters", "read", "optional-hardware"])

    def execute(self, context, params: dict) -> SkillResult:
        integ = float(params.get("integration_ms") or 100.0)
        try:
            ep = satellite_endpoint("pmt", params.get("satellite") or "", params.get("device") or "")
            value = ep.call("read", {"integration_ms": integ}, timeout_s=integ / 1000.0 + 10.0)
        except Exception as exc:  # noqa: BLE001
            return fail("ReadPmt", exc)
        return SkillResult(skill_name="ReadPmt", success=True,
                           data={"device": ep.label, "integration_ms": integ,
                                 "reading": json_safe(value)},
                           summary=f"PMT {ep.label}: {json_safe(value)}")


class SetPmtHighVoltage(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetPmtHighVoltage", version="1.0.0", category=SkillCategory.WRITE,
            # DANGEROUS: a PMT's high voltage is a hazard Nanonis bounds nothing
            # about — light on a biased photocathode destroys it, and MAST cannot
            # see whether the side exit is dark. A human decides every time.
            safety_level=SafetyLevel.DANGEROUS,
            description=(
                tr("设置 PMT 高压（伏）。volts=0 = 关高压（hv_off）。**每次都要人工批准**：带高压的光电阴极见强光会损坏，"
                "而 MAST 看不到侧出口是不是暗的。具体上限由卫星的 PMT 驱动配置决定。", "Set the PMT high voltage (volts). volts=0 = high voltage off (hv_off). **Needs human approval every time**: a photocathode under high voltage is damaged by bright light, "
"and MAST cannot see whether the side exit is dark. The specific upper limit is set by the satellite's PMT driver configuration.")),
            parameters=[
                *_target_params(),
                ParameterSpec(name="volts", type="float", unit="V", required=True,
                              min_value=0.0, max_value=1500.0,
                              description=tr("高压（V）；0 = 关", "High voltage (V); 0 = off")),
            ],
            estimated_duration_s=2.0, composition_level=0,
            tags=["optics", "pmt", "high_voltage", "ters", "write", "optional-hardware"])

    def execute(self, context, params: dict) -> SkillResult:
        volts = float(params["volts"])
        try:
            ep = satellite_endpoint("pmt", params.get("satellite") or "", params.get("device") or "")
            if volts <= 0.0:
                value = ep.call("hv_off", {}, timeout_s=10.0)
            else:
                value = ep.call("set_hv", {"volts": volts}, timeout_s=30.0)
        except Exception as exc:  # noqa: BLE001
            return fail("SetPmtHighVoltage", exc, volts=volts)
        return SkillResult(skill_name="SetPmtHighVoltage", success=True,
                           data={"device": ep.label, "volts": volts, "result": json_safe(value)},
                           summary=(f"PMT {ep.label}: HV off" if volts <= 0
                                    else f"PMT {ep.label}: HV {volts:g} V"))
