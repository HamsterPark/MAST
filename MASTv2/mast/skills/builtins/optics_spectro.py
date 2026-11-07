"""Spectrometer skills — Raman CCD on the spectroscopy computer (satellite), and simulators.

9 skills (contract §8.2):

=========================  ========  =====================================================
skill                      safety    what
=========================  ========  =====================================================
ListSatellites             AUTO      hub view of the satellites + which registry entries use them
GetSpectrometerStatus      AUTO      camera / spectrograph state, limits, laser line
ConfigureSpectrometer      CONFIRM   exposure, frames, ADC, shutter, trigger, timestamps …
SetSpectrometerGrating     CONFIRM   turret move (10–30 s)
SetSpectrometerCenter      CONFIRM   centre wavelength
SetSpectrometerExitPort    CONFIRM   front (camera) / side (PMT)
AcquireSpectrum            CONFIRM   expose → frames + full metadata → experiment folder → LiveHub
StopSpectrometer           AUTO      stop acquisitions; token-free (instrument_lock.BYPASS_NAMES)
StopSatelliteDevices       AUTO      stop every device of one / all satellites; token-free
=========================  ========  =====================================================

Hardware access goes through the instrument registry (``satellite_spectrometer`` /
``sim_spectrometer`` entries in ``config/optical_instruments.json``) and, for the
satellite-level views, the hub. The spectrometer's own limits are Layer-0 inside
:class:`mast.instruments.spectrometer.Spectrometer`; these skills only translate.

AcquireSpectrum's guarantees (contract §8.2, §10.2, §11.3):

* holds the instrument token (it is a WRITE skill), and refuses to start while a
  tip-work or approach skill owns the instrument;
* reads the STM state immediately before and after the exposure, and samples the
  tunnel current during it;
* writes ``<stem>.npy`` (row 0 = wavelength nm, rows 1… = frames) and
  ``<stem>.json`` (all metadata, sha256 of the array) into the active sample's
  ``raw/other/`` through ``ExperimentFileStore``;
* publishes to LiveHub (``live_stream``, default ``local/spectra/<device>``): every
  frame for a local device, and the averaged spectrum of every acquisition. A
  satellite's raw frames already reach LiveHub through the hub's bridge
  (``<sat>/<dev>/spectrum``), so they are not published twice.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

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
    CurrentSampler,
    LiveStream,
    abort_probe,
    atomic_write_bytes,
    dump_json_bytes,
    fail,
    hlc_now,
    json_safe,
    new_run_id,
    no_stm,
    optics_positions,
    resolve_device,
    scope_ids,
    staging_dir,
    stm_snapshot,
    store_in_experiment,
    tip_work_refusal,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ListSatellites",
    "GetSpectrometerStatus",
    "ConfigureSpectrometer",
    "SetSpectrometerGrating",
    "SetSpectrometerCenter",
    "SetSpectrometerExitPort",
    "AcquireSpectrum",
    "StopSpectrometer",
    "StopSatelliteDevices",
]

#: Registry role of the TERS Raman camera (aligned with the hub's satellites.json).
SPECTROMETER_ROLE = "raman_ccd"

#: Option vocabularies sent to the satellite — member names of LightField 6.18's
#: own enums (``PrincetonInstruments.LightField.AddIns``), so the C# satellite
#: hands them to LightField unchanged; whether this camera supports one is the
#: satellite's check (LightField IsValid → invalid_args). Each list is the
#: contract §5.4 subset of its enum, pinned against the API dump by
#: test_optics_spectro. ("EffectivelyExposing" is OutputSignal's member name —
#: "EffectiveExposure" does not exist and the satellite refuses it.)
#: enum TriggerResponse
TRIGGER_RESPONSES = ["NoResponse", "ReadoutPerTrigger", "ShiftPerTrigger",
                     "ExposeDuringTriggerPulse", "StartOnSingleTrigger"]
#: enum TriggerDetermination
TRIGGER_EDGES = ["RisingEdge", "FallingEdge"]
#: enum ShutterTimingMode
SHUTTER_MODES = ["Normal", "AlwaysOpen", "AlwaysClosed", "OpenBeforeTrigger"]
#: enum AdcQuality
ADC_QUALITIES = ["LowNoise", "HighCapacity", "HighSpeed", "ElectronMultiplied"]
#: enum OutputSignal
OUTPUT_SIGNALS = ["NotReadingOut", "ShutterOpen", "Busy", "AlwaysLow", "AlwaysHigh",
                  "Acquiring", "ShiftingUnderMask", "Exposing", "EffectivelyExposing",
                  "ReadingOut", "WaitingForTrigger"]

_TAGS = ["optics", "spectrometer", "ters", "optional-hardware"]


def _device_param() -> ParameterSpec:
    return ParameterSpec(
        name="device_id", type="str", required=False, default="",
        description=tr("光谱仪设备 id（ListOpticalDevices）；留空 = 唯一一台 role=raman_ccd 的光谱仪",
                       "Spectrometer device id (ListOpticalDevices); leave empty = the only spectrometer with role=raman_ccd"))


def _spectrometer(params: dict):
    reg = get_instrument_registry()
    dev_id = resolve_device(reg, kind="spectrometer", role=SPECTROMETER_ROLE,
                            device_id=params.get("device_id") or None)
    return dev_id, reg.spectrometer(dev_id), reg


def _entry_info(reg: Any, dev_id: str) -> dict:
    for d in reg.list_devices():
        if d.get("id") == dev_id:
            return {k: d.get(k) for k in ("id", "name", "type", "kind", "role",
                                          "satellite", "remote_device")}
    return {"id": dev_id}


# ── read-only ──────────────────────────────────────────────────────────────


class ListSatellites(BaseSkill):
    """Satellites as the hub sees them, and the registry entries that use them."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ListSatellites",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("列出卫星节点（光谱电脑等）：连接状态（connected/degraded/lost）、RTT、时钟偏差、"
                "boot_id、每台设备的状态（含人工接管 operator_hold），以及 optical_instruments.json 里"
                "哪些设备条目指向它们。只读。卫星不在线时其余光学/光谱技能会报 unavailable。",
                   "List satellite nodes (spectrometer PC etc.): connection state (connected/degraded/lost), RTT, clock offset, "
                   "boot_id, the state of each device (including operator_hold for manual takeover), and "
                   "which device entries in optical_instruments.json point to them. Read-only. When a satellite is offline, the other optics/spectrometer skills report unavailable.")),
            estimated_duration_s=0.2,
            composition_level=0,
            tags=["optics", "satellite", "inventory", "read", "status", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        reg = get_instrument_registry()
        users = [d for d in reg.list_devices() if d.get("satellite")]
        try:
            from mast.instruments.remote import get_hub

            sats = list(get_hub().list_satellites() or [])
        except Exception as exc:  # noqa: BLE001 - degraded is not empty
            return SkillResult(
                skill_name="ListSatellites", success=True,
                data={"hub_available": False, "detail": f"{type(exc).__name__}: {exc}",
                      "satellites": [], "registry_devices": users},
                summary=tr(f"卫星枢纽不可用（{exc}）—— 无法判断卫星状态，不等于没有卫星",
                           f"satellite hub unavailable ({exc}) - cannot tell the satellite state; this does not mean there are no satellites"))
        brief = ", ".join(f"{s.get('id')}={s.get('state')}" for s in sats) or "none configured"
        return SkillResult(
            skill_name="ListSatellites", success=True,
            data={"hub_available": True, "satellites": json_safe(sats),
                  "registry_devices": users},
            summary=f"{len(sats)} satellite(s): {brief}")


class GetSpectrometerStatus(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSpectrometerStatus",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("读光谱仪状态：是否在采、CCD 温度与是否锁定、曝光/帧数、光栅、中心波长、出口、触发、ROI、ADC、"
                "激发线（laser_line_nm），以及本机配置的限值（曝光/帧数/中心波长/光栅白名单）。只读，不改设置。",
                   "Read the spectrometer state: whether it is acquiring, CCD temperature and whether it is locked, exposure/frames, grating, center wavelength, exit port, trigger, ROI, ADC, "
                   "excitation line (laser_line_nm), and this machine's configured limits (exposure/frames/center wavelength/grating whitelist). Read-only, changes no setting.")),
            parameters=[_device_param()],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=[*_TAGS, "read", "status"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev_id, spec, reg = _spectrometer(params)
            st = spec.status()
        except Exception as exc:  # noqa: BLE001
            return fail("GetSpectrometerStatus", exc)
        temp = st.get("temperature_c")
        return SkillResult(
            skill_name="GetSpectrometerStatus", success=True,
            data={"device": _entry_info(reg, dev_id), "status": json_safe(st),
                  "limits": spec.limits.as_dict(), "laser_line_nm": spec.laser_line_nm},
            summary=(f"{dev_id}: {st.get('state', '?')}, {st.get('grating', '?')} @ "
                     f"{st.get('center_wavelength_nm', '?')} nm, exposure "
                     f"{st.get('exposure_ms', '?')} ms, CCD {temp if temp is not None else '?'} °C"
                     f"{' (locked)' if st.get('temperature_locked') else ''}"))


# ── settings ────────────────────────────────────────────────────────────────


class ConfigureSpectrometer(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ConfigureSpectrometer",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("设置 CCD 采集参数：曝光、帧数、ADC 速率与模式、快门模式、外触发响应与沿、输出信号、时间戳、帧计数、"
                "ROI 与保存文件名。只改给了的项；曝光/帧数超出本机限值会被**拒绝**（不会被截到上限）。"
                "采集进行中不能改（会报 busy）。光栅、中心波长、出口各有单独的技能。",
                   "Set CCD acquisition parameters: exposure, frames, ADC rate and mode, shutter mode, external trigger response and edge, output signal, time stamps, frame tracking, "
                   "ROI and saved file name. Only the given items are changed; exposure/frames beyond this machine's limits are **rejected** (not clamped to the limit). "
                   "Cannot be changed while an acquisition is running (reports busy). Grating, center wavelength and exit port have their own skills.")),
            parameters=[
                _device_param(),
                ParameterSpec(name="exposure_ms", type="float", unit="ms", required=False,
                              min_value=0.001, max_value=3_600_000.0,
                              description=tr("曝光时间（毫秒）；本机限值见 GetSpectrometerStatus.limits",
                                             "Exposure time (ms); this machine's limits are in GetSpectrometerStatus.limits")),
                ParameterSpec(name="frames", type="int", required=False, min_value=1,
                              max_value=100_000, description=tr("每次采集的帧数", "Frames per acquisition")),
                ParameterSpec(name="adc_mhz", type="float", unit="MHz", required=False,
                              min_value=0.01, max_value=100.0,
                              description=tr("ADC 读出速率（PyLoN 常用 1 或 4 MHz）", "ADC readout rate (PyLoN commonly 1 or 4 MHz)")),
                ParameterSpec(name="adc_quality", type="str", required=False,
                              allowed_values=ADC_QUALITIES, description=tr("ADC 模式", "ADC mode")),
                ParameterSpec(name="shutter_mode", type="str", required=False,
                              allowed_values=SHUTTER_MODES,
                              description=tr("光谱仪入口快门模式（只挡进光谱仪的光，**不挡照到样品上的激光**）",
                                             "Spectrometer entrance shutter mode (blocks only the light entering the spectrometer, **not the laser hitting the sample**)")),
                ParameterSpec(name="trigger_response", type="str", required=False,
                              allowed_values=TRIGGER_RESPONSES,
                              description=tr("外触发响应（数字触发线 → PyLoN Ext Trigger）",
                                             "External trigger response (digital trigger line → PyLoN Ext Trigger)")),
                ParameterSpec(name="trigger_edge", type="str", required=False,
                              allowed_values=TRIGGER_EDGES, description=tr("外触发沿", "External trigger edge")),
                ParameterSpec(name="output_signal", type="str", required=False,
                              allowed_values=OUTPUT_SIGNALS,
                              description=tr("相机 Logic Out 输出的信号（可接回 Nanonis 输入记录曝光窗口）",
                                             "Signal on the camera Logic Out output (can be wired back to a Nanonis input to record the exposure window)")),
                ParameterSpec(name="timestamps", type="bool", required=False,
                              description=tr("记录每帧曝光起止时间戳（1 µs，ExposureStarted+ExposureEnded）",
                                             "Record per-frame exposure start/end time stamps (1 µs, ExposureStarted+ExposureEnded)")),
                ParameterSpec(name="frame_tracking", type="bool", required=False,
                              description=tr("记录帧计数（发现丢帧）", "Record frame tracking (to detect dropped frames)")),
                ParameterSpec(name="roi", type="str", required=False,
                              description=(tr("ROI：'full'、'line:<行数>'（行传感器，例如 'line:50'）或 "
                                           "JSON（例如 '{\"x\":0,\"width\":1340,\"y\":40,\"height\":20}'）",
                                              "ROI: 'full', 'line:<rows>' (line sensor, e.g. 'line:50') or "
                                              "JSON (e.g. '{\"x\":0,\"width\":1340,\"y\":40,\"height\":20}')"))),
                ParameterSpec(name="file_base_name", type="str", required=False,
                              description=tr("卫星端 .spe 文件的基名（只改基名，不改目录）",
                                             "Base name of the .spe file on the satellite (only the base name changes, not the directory)")),
                ParameterSpec(name="file_attach_increment", type="bool", required=False,
                              description=tr("文件名附加自增序号", "Append an auto-incrementing number to the file name")),
            ],
            estimated_duration_s=2.0,
            composition_level=0,
            tags=[*_TAGS, "configure", "write"],
        )

    @staticmethod
    def _roi(text: str) -> Any:
        t = str(text).strip()
        if not t or t.lower() == "full":
            return {"mode": "full"}
        if t.lower().startswith("line:"):
            rows = int(t.split(":", 1)[1])
            if rows < 1:
                raise ValueError("line ROI needs >= 1 row")
            return {"mode": "line", "rows": rows}
        obj = json.loads(t)
        if not isinstance(obj, dict):
            raise ValueError("ROI JSON must be an object")
        return {"mode": "custom", **obj}

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev_id, spec, _reg = _spectrometer(params)
        except Exception as exc:  # noqa: BLE001
            return fail("ConfigureSpectrometer", exc)
        settings: dict[str, Any] = {}
        for key in ("exposure_ms", "frames", "adc_mhz", "adc_quality", "shutter_mode",
                    "output_signal", "frame_tracking"):
            if params.get(key) not in (None, ""):
                settings[key] = params[key]
        if params.get("timestamps") is not None:
            settings["timestamps"] = ("ExposureStarted,ExposureEnded" if params["timestamps"]
                                      else "None")
        if params.get("trigger_response") or params.get("trigger_edge"):
            settings["trigger"] = {k: v for k, v in (
                ("response", params.get("trigger_response")),
                ("edge", params.get("trigger_edge"))) if v}
        if params.get("roi"):
            try:
                settings["roi"] = self._roi(params["roi"])
            except Exception as exc:  # noqa: BLE001
                return SkillResult(skill_name="ConfigureSpectrometer", success=False,
                                   error=f"bad roi {params['roi']!r}: {exc}")
        file_cfg = {k: v for k, v in (("base_name", params.get("file_base_name")),
                                      ("attach_increment", params.get("file_attach_increment")))
                    if v not in (None, "")}
        if file_cfg:
            settings["file"] = file_cfg
        if not settings:
            return SkillResult(skill_name="ConfigureSpectrometer", success=False,
                               error="nothing to configure — pass at least one setting")
        try:
            applied = spec.configure(**settings)
        except Exception as exc:  # noqa: BLE001
            return fail("ConfigureSpectrometer", exc, device_id=dev_id)
        return SkillResult(skill_name="ConfigureSpectrometer", success=True,
                           data={"device_id": dev_id, "requested": json_safe(settings),
                                 "applied": json_safe(applied)},
                           summary=f"{dev_id}: configured {sorted(settings)}")


class _SettingSkill(BaseSkill):
    """Shared execute for the one-setting spectrograph skills."""

    NAME = ""
    PARAM = ""

    def _apply(self, spec, value):  # pragma: no cover - overridden
        raise NotImplementedError

    def execute(self, context, params: dict) -> SkillResult:
        try:
            dev_id, spec, _reg = _spectrometer(params)
            before = None
            try:
                before = spec.status()
            except InstrumentError:
                pass
            if before and before.get("is_running"):
                return SkillResult(skill_name=self.NAME, success=False,
                                   error=tr(f"{dev_id} 正在采集 —— 采集期间不改光栅/中心波长/出口",
                                            f"{dev_id} is acquiring - grating/center wavelength/exit port are not changed during acquisition"))
            applied = self._apply(spec, params[self.PARAM])
        except Exception as exc:  # noqa: BLE001
            return fail(self.NAME, exc)
        return SkillResult(skill_name=self.NAME, success=True,
                           data={"device_id": dev_id, self.PARAM: params[self.PARAM],
                                 "applied": json_safe(applied)},
                           summary=f"{dev_id}: {self.PARAM} = {params[self.PARAM]}")


class SetSpectrometerGrating(_SettingSkill):
    NAME, PARAM = "SetSpectrometerGrating", "grating"

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=self.NAME, version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("换光栅（转塔转动约 10–30 s）。grating 用设备上的名字，例如 '[500nm,1200][0][0]'；"
                "本机配置了光栅白名单时只允许名单里的。采集进行中拒绝。换完波长轴会变，下一条谱自动带新轴。",
                   "Change the grating (the turret takes about 10-30 s to rotate). grating is the name on the device, e.g. '[500nm,1200][0][0]'; "
                   "when a grating whitelist is configured on this machine only listed ones are allowed. Refused while an acquisition is running. After the change the wavelength axis differs, and the next spectrum automatically carries the new axis.")),
            parameters=[_device_param(),
                        ParameterSpec(name="grating", type="str", required=True,
                                      description=tr("光栅名（见 GetSpectrometerStatus 的 status.grating 与 limits.gratings）",
                                                     "Grating name (see status.grating and limits.gratings of GetSpectrometerStatus)"))],
            estimated_duration_s=30.0, composition_level=0,
            tags=[*_TAGS, "grating", "write"])

    def _apply(self, spec, value):
        return spec.set_grating(str(value))


class SetSpectrometerCenter(_SettingSkill):
    NAME, PARAM = "SetSpectrometerCenter", "center_nm"

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=self.NAME, version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("设置光谱仪中心波长（nm）。超出本机限值直接拒绝。采集进行中拒绝。"
                "拉曼位移换算：中心 664.55 nm @ 633 nm 激发 ≈ 750 cm⁻¹。",
                   "Set the spectrometer center wavelength (nm). Beyond this machine's limits it is refused outright. Refused while an acquisition is running. "
                   "Raman shift conversion: center 664.55 nm @ 633 nm excitation ≈ 750 cm⁻¹.")),
            parameters=[_device_param(),
                        ParameterSpec(name="center_nm", type="float", unit="nm", required=True,
                                      min_value=100.0, max_value=3000.0,
                                      description=tr("中心波长（nm）", "Center wavelength (nm)"))],
            estimated_duration_s=5.0, composition_level=0,
            tags=[*_TAGS, "wavelength", "write"])

    def _apply(self, spec, value):
        return spec.set_center_wavelength(float(value))


class SetSpectrometerExitPort(_SettingSkill):
    NAME, PARAM = "SetSpectrometerExitPort", "port"

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=self.NAME, version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("切换光谱仪出口：front = CCD 相机，side = 侧出口（接 PMT）。切错出口相机就收不到光。"
                "采集进行中拒绝。",
                   "Switch the spectrometer exit port: front = CCD camera, side = side exit (to the PMT). With the wrong port the camera receives no light. "
                   "Refused while an acquisition is running.")),
            parameters=[_device_param(),
                        ParameterSpec(name="port", type="str", required=True,
                                      allowed_values=["front", "side"], description=tr("出口", "Exit port"))],
            estimated_duration_s=3.0, composition_level=0,
            tags=[*_TAGS, "exit_port", "write"])

    def _apply(self, spec, value):
        return spec.set_exit_port(str(value))


# ── acquisition ───────────────────────────────────────────────────────────


def _safe_stem(text: str, limit: int = 24) -> str:
    return "".join(c for c in str(text or "") if c.isalnum() or c in "-_")[:limit]


def _stack(frames: list[np.ndarray], axis_nm: np.ndarray | None) -> tuple[np.ndarray, bool]:
    """Row 0 = axis (NaN when unknown), rows 1… = frames, NaN-padded to one width."""
    width = max([len(f) for f in frames] + ([len(axis_nm)] if axis_nm is not None else []))
    ragged = len({len(f) for f in frames}) > 1 or (
        axis_nm is not None and len(axis_nm) != len(frames[0]))
    out = np.full((len(frames) + 1, width), np.nan, dtype=np.float64)
    if axis_nm is not None:
        out[0, :len(axis_nm)] = axis_nm
    for i, f in enumerate(frames, start=1):
        out[i, :len(f)] = np.asarray(f, dtype=np.float64)
    return out, ragged


def _peak(mean: np.ndarray, axis_nm: np.ndarray | None, laser_nm: float | None) -> dict:
    if mean.size == 0 or not np.isfinite(mean).any():
        return {}
    i = int(np.nanargmax(mean))
    out: dict[str, Any] = {"pixel": i, "counts": float(mean[i])}
    if axis_nm is not None and i < len(axis_nm) and math.isfinite(float(axis_nm[i])):
        nm = float(axis_nm[i])
        out["nm"] = nm
        if laser_nm:
            out["raman_shift_cm1"] = 1e7 / float(laser_nm) - 1e7 / nm
    return out


class AcquireSpectrum(BaseSkill):
    """One spectrum acquisition with full TERS metadata."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AcquireSpectrum",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("采一条光谱（光谱电脑（卫星）上的 LightField / PyLoN，或模拟光谱仪）：曝光前后各读一次 STM 状态，"
                "曝光期间采样隧道电流，帧数据（第 0 行 = 波长 nm，其后每行一帧）存为 .npy、全部元数据存为同名 "
                ".json，一起归档进当前样品的实验文件夹（raw/other），并推到 LiveHub 实时曲线（本机设备逐帧；"
                "卫星设备的原始帧由枢纽转发到 <卫星>/<设备>/spectrum，这里推每次采集的平均谱）。"
                "修针或进针进行中拒绝开始。只返回路径与摘要，不返回数组。exposure_ms/frames 不给就用光谱仪当前设置；"
                "给了会留在光谱仪的设置里（LightField 的行为），之后的采集沿用。"
                "dry_run=true 只做检查与状态读取、不曝光。",
                   "Acquire one spectrum (LightField / PyLoN on the spectrometer PC (satellite), or the simulated spectrometer): read the STM state once before and once after the exposure, "
                   "sample the tunneling current during the exposure, store the frame data (row 0 = wavelength nm, then one row per frame) as .npy and all metadata as a same-named "
                   ".json, archive both in the current sample's experiment folder (raw/other), and push to the LiveHub live curve (frame by frame for local devices; "
                   "for satellite devices the hub forwards the raw frames to <satellite>/<device>/spectrum, and the mean spectrum of each acquisition is pushed here). "
                   "Refused to start while tip conditioning or approach is in progress. Returns only paths and a summary, no arrays. If exposure_ms/frames are not given the spectrometer's current settings are used; "
                   "given values stay in the spectrometer's settings (LightField behavior) and later acquisitions keep using them. "
                   "dry_run=true only does the checks and state reads, with no exposure.")),
            parameters=[
                _device_param(),
                ParameterSpec(name="exposure_ms", type="float", unit="ms", required=False,
                              min_value=0.001, max_value=3_600_000.0,
                              description=tr("曝光时间（毫秒）；不给 = 用当前设置", "Exposure time (ms); not given = use the current setting")),
                ParameterSpec(name="frames", type="int", required=False, min_value=1,
                              max_value=100_000, description=tr("帧数；不给 = 用当前设置", "Frames; not given = use the current setting")),
                ParameterSpec(name="save_raw", type="bool", required=False, default=True,
                              description=tr("让卫星同时存原始 .spe（D:\\Data，之后由 spe_watcher 传回枢纽）",
                                             "Have the satellite also save the raw .spe (D:\\Data, transferred back to the hub afterwards by spe_watcher)")),
                ParameterSpec(name="tag", type="str", required=False, default="",
                              description=tr("附加在文件名上的标签", "Label appended to the file name")),
                ParameterSpec(name="sample_current", type="bool", required=False, default=True,
                              description=tr("曝光期间采样隧道电流（Current_Get），记均值与标准差",
                                             "Sample the tunneling current during the exposure (Current_Get) and record the mean and standard deviation")),
                ParameterSpec(name="current_interval_s", type="float", unit="s", required=False,
                              default=0.25, min_value=0.05, max_value=10.0,
                              description=tr("电流采样间隔", "Current sampling interval")),
                ParameterSpec(name="live_stream", type="str", required=False, default="",
                              description=tr("LiveHub 流 id；留空 = local/spectra/<device_id>",
                                             "LiveHub stream id; leave empty = local/spectra/<device_id>")),
                ParameterSpec(name="meta_json", type="str", required=False, default="",
                              description=(tr("附加元数据（JSON 对象），原样写进 sidecar 的 extra，例如组合技能的 "
                                           "{\"step_id\":…, \"ix\":3, \"iy\":7, \"mode\":\"near\"}",
                                              "Extra metadata (JSON object), written as-is into the sidecar's extra, e.g. from a composite skill "
                                              "{\"step_id\":…, \"ix\":3, \"iy\":7, \"mode\":\"near\"}"))),
                ParameterSpec(name="timeout_s", type="float", unit="s", required=False,
                              min_value=1.0, max_value=7_200.0,
                              description=tr("采集时限；不给 = 曝光×帧数×1.5 + 30 s",
                                             "Acquisition time limit; not given = exposure x frames x 1.5 + 30 s")),
                ParameterSpec(name="dry_run", type="bool", required=False, default=False,
                              description=tr("**试跑**：检查限值、读状态与 STM 快照，不曝光、不存盘",
                                             "**Dry run**: check limits, read the state and an STM snapshot; no exposure, nothing saved")),
            ],
            estimated_duration_s=5.0,
            composition_level=0,
            tags=[*_TAGS, "spectrum", "acquisition", "raman", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "AcquireSpectrum"
        try:
            dev_id, spec, reg = _spectrometer(params)
        except Exception as exc:  # noqa: BLE001
            return fail(name, exc)
        refusal = tip_work_refusal()
        if refusal:
            return SkillResult(skill_name=name, success=False, error=refusal)
        probe = abort_probe(context)
        if probe is not None and probe():
            return SkillResult(skill_name=name, success=False,
                               error="aborted by operator — acquisition not started")
        extra: dict[str, Any] = {}
        if params.get("meta_json"):
            try:
                extra = json.loads(params["meta_json"])
                if not isinstance(extra, dict):
                    raise ValueError("not a JSON object")
            except Exception as exc:  # noqa: BLE001
                return SkillResult(skill_name=name, success=False,
                                   error=f"meta_json must be a JSON object: {exc}")
        try:
            exp, n_frames = spec.check_request(params.get("exposure_ms"), params.get("frames"))
            before = spec.status()
        except Exception as exc:  # noqa: BLE001
            return fail(name, exc, device_id=dev_id)
        if before.get("is_running"):
            return SkillResult(skill_name=name, success=False,
                               error=tr(f"{dev_id} 正在采集（可能是有人在 LightField 里手动采）—— 不叠加采集",
                                        f"{dev_id} is acquiring (someone may be acquiring manually in LightField) - not stacking another acquisition"))
        if before.get("operator_hold"):
            return SkillResult(skill_name=name, success=False,
                               error=tr(f"{dev_id} 在卫星上处于人工接管（operator_hold）—— 枢纽命令一律拒绝",
                                        f"{dev_id} is under manual takeover on the satellite (operator_hold) - all hub commands are refused"))

        run_id = new_run_id("spec")
        device = _entry_info(reg, dev_id)
        positions = optics_positions(reg)
        stm_before = stm_snapshot(context, fresh=True)
        if params.get("dry_run"):
            return SkillResult(
                skill_name=name, success=True,
                data={"dry_run": True, "device": device, "run_id": run_id,
                      "exposure_ms": exp if exp is not None else before.get("exposure_ms"),
                      "frames": n_frames if n_frames is not None else before.get("frames"),
                      "status": json_safe(before), "limits": spec.limits.as_dict(),
                      "stm": {"before": stm_before}, "optics": positions},
                summary=f"[DRY RUN] {dev_id} ready — limits and state checked, nothing exposed")

        sampler = (CurrentSampler(context, interval_s=float(params.get("current_interval_s") or 0.25))
                   if params.get("sample_current", True) else None)
        laser = spec.laser_line_nm or before.get("laser_line_nm")
        # A satellite's raw frames already reach LiveHub through the hub's bridge
        # (<sat>/<dev>/spectrum, every frame, manual ones too). This stream is the
        # MAST-acquired result: per frame only for a local device, and always the
        # final spectrum of each acquisition. It is never end()ed — it is a rolling
        # per-device stream (or the caller's, whose lifecycle is the caller's).
        stream_id = str(params.get("live_stream") or f"local/spectra/{dev_id}")
        per_frame_live = not device.get("satellite")
        live = LiveStream(stream_id, "spectrum", title=tr(f"光谱 · {spec.name}", f"Spectrum · {spec.name}"),
                          source="AcquireSpectrum", x_label="Wavelength", x_unit="nm",
                          y_label="Counts", y_unit="counts",
                          meta={"laser_line_nm": laser, "device_id": dev_id,
                                "satellite": device.get("satellite")})
        axis_state: dict[str, Any] = {"axis": None, "ver": None}
        exp_label = exp if exp is not None else before.get("exposure_ms")
        point_meta = {k: extra[k] for k in ("ix", "iy", "step_id", "mode") if k in extra}

        def _axis_ver(axis_nm: np.ndarray | None) -> Any:
            if axis_nm is not None and axis_state["axis"] is not axis_nm:
                axis_state["axis"] = axis_nm
                axis_state["ver"] = live.set_axis(axis_nm, unit="nm")
            return axis_state["ver"]

        def on_frame(i: int, arr: np.ndarray, axis_nm: np.ndarray | None) -> None:
            if not per_frame_live:
                return
            live.publish({"y": np.asarray(arr, dtype=np.float64), "axis_ver": _axis_ver(axis_nm),
                          "meta": {"frame_index": i, "run_id": run_id,
                                   "exposure_ms": exp_label, **point_meta}})

        def check() -> bool:
            if sampler is not None:
                sampler.tick()
            return bool(probe()) if probe is not None else False

        hlc_start = hlc_now()
        t0 = time.monotonic()
        tag = _safe_stem(params.get("tag", ""))
        try:
            result = spec.acquire(
                exposure_ms=exp, frames=n_frames, save=bool(params.get("save_raw", True)),
                base_name=f"mast_{run_id}" + (f"_{tag}" if tag else ""), tag=tag or None,
                meta={"mast_run_id": run_id, **{k: v for k, v in extra.items()
                                                if isinstance(v, (str, int, float, bool))}},
                timeout_s=params.get("timeout_s"), check_abort=check, on_frame=on_frame)
        except Exception as exc:  # noqa: BLE001
            spec.stop()
            return fail(name, exc, device_id=dev_id, run_id=run_id)
        elapsed = time.monotonic() - t0
        hlc_end = hlc_now()
        stm_after = stm_snapshot(context, fresh=True)
        if not result.frames:
            return SkillResult(skill_name=name, success=False,
                               error=f"{dev_id}: acquisition finished but no frame arrived "
                                     f"({json_safe(result.meta.get('missing_frames'))})",
                               data={"device_id": dev_id, "run_id": run_id,
                                     "acquisition": json_safe(result.meta)})

        stack, ragged = _stack(result.frames, result.axis_nm)
        mean = np.nanmean(stack[1:], axis=0)
        if not per_frame_live or len(result.frames) > 1:
            final_axis = result.axis_nm if (result.axis_nm is not None
                                            and len(result.axis_nm) == len(mean)) else None
            live.publish({"y": mean, "axis_ver": _axis_ver(final_axis),
                          "meta": {"run_id": run_id, "final": True,
                                   "frames_averaged": len(result.frames),
                                   "exposure_ms": exp_label, **point_meta}})

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        stem = f"spec_{stamp}_{_safe_stem(dev_id, 20) or 'ccd'}" + (f"_{tag}" if tag else "")
        buf = io.BytesIO()
        np.save(buf, stack, allow_pickle=False)
        payload = buf.getvalue()
        sha = hashlib.sha256(payload).hexdigest()
        sdir = staging_dir("spectra")
        npy_path, json_path = sdir / f"{stem}.npy", sdir / f"{stem}.json"
        ids = scope_ids()
        peak = _peak(mean, result.axis_nm, laser)
        sidecar = {
            "schema": "mast.spectrum/1",
            "layout": "npy rows: [0] = wavelength_nm (NaN when unknown), [1..] = frames (counts)",
            "skill": name, "run_id": run_id,
            "time": {"started_utc": result.started_utc, "finished_utc": result.finished_utc,
                     "hlc_start": hlc_start, "hlc_end": hlc_end, "elapsed_s": round(elapsed, 3),
                     "clock_offset_ms": result.meta.get("clock_offset_ms"),
                     "rtt_ms": result.meta.get("rtt_ms"), "hub_host": platform.node()},
            "device": {**device, "cmd_id": result.meta.get("cmd_id")},
            "settings": {"requested": {"exposure_ms": exp, "frames": n_frames},
                         "before": json_safe(before), "laser_line_nm": laser},
            "stm": {"before": stm_before, "after": stm_after,
                    "current_during_exposure": sampler.stats() if sampler else None,
                    "coord_epoch": _coord_epoch(), "tip": _tip()},
            "optics": positions,
            **_optical_state_fields(),
            "scope": {**ids, "action_id": None,
                      "action_id_note": "assigned by the recorder after the skill returns; "
                                        "linked through scan_files by this file's path",
                      "step_id": extra.get("step_id")},
            "files": {"array": {"name": npy_path.name, "sha256": sha, "size": len(payload),
                                "shape": list(stack.shape), "dtype": "float64",
                                "ragged_frames": ragged},
                      "satellite_files": json_safe(result.files)},
            "result": {"frames": len(result.frames), "peak": peak,
                       "counts": {"min": float(np.nanmin(mean)), "max": float(np.nanmax(mean)),
                                  "mean": float(np.nanmean(mean))}},
            "acquisition": json_safe(result.meta),
            "extra": json_safe(extra),
            "live_stream": stream_id,
        }
        try:
            atomic_write_bytes(npy_path, payload)
            atomic_write_bytes(json_path, dump_json_bytes(sidecar))
        except Exception as exc:  # noqa: BLE001
            return fail(name, InstrumentError(f"could not write the spectrum files: {exc}"),
                        device_id=dev_id, run_id=run_id)
        stored = store_in_experiment([npy_path, json_path], skill=name)
        paths = stored["paths"]
        axis = result.axis_nm
        data = {
            "path": paths[0], "sidecar_path": paths[1], "saved_paths": paths,
            "stored_in_experiment": bool(stored.get("stored")),
            "store_detail": stored.get("reason") or stored.get("files"),
            "device_id": dev_id, "run_id": run_id, "n_frames": len(result.frames),
            "n_pixels": int(stack.shape[1]), "exposure_ms": exp_label,
            "axis_nm_range": ([float(np.nanmin(axis)), float(np.nanmax(axis))]
                              if axis is not None and np.isfinite(axis).any() else None),
            "laser_line_nm": laser, "peak": peak,
            "counts": sidecar["result"]["counts"], "sha256": sha,
            "satellite_files": json_safe(result.files),
            "missing_frames": result.meta.get("missing_frames") or [],
            "stm": {"current_during_exposure": sampler.stats() if sampler else None},
            "live_stream": stream_id, "elapsed_s": round(elapsed, 3),
        }
        pk = (f", peak {peak['nm']:.2f} nm" if "nm" in peak else "") + (
            f" ({peak['raman_shift_cm1']:.0f} cm⁻¹)" if "raman_shift_cm1" in peak else "")
        summary = (f"spectrum {len(result.frames)}×{exp_label} ms on {dev_id}{pk}, max "
                   f"{data['counts']['max']:.0f} counts → {Path(paths[0]).name}"
                   + ("" if stored.get("stored") else tr(" (未归档进实验文件夹：没有选定样品)",
                                                         " (not archived into the experiment folder: no sample selected)")))
        return SkillResult(skill_name=name, success=True, data=data, summary=summary)


def _optical_state_fields() -> dict[str, Any]:
    """The manual optical state (which λ/2 is installed, analyzer angle, objective, …) as the
    operator registered it, for the sidecar: ``{"optical_state": {slot: {value, set_by, set_at,
    source, valid_until, valid}}}``. Taken when the sidecar is written, i.e. right after the
    exposure; each slot's ``set_at`` shows a change made during it. If the registry cannot be
    read the sidecar says so (``optical_state: null`` + ``optical_state_error``) instead of
    pretending that nothing was registered.

    **Optical station only.** On an STM station (the TERS bench) the sidecar is what it was
    before the manual optical state existed: no ``optical_state`` key at all, not even an
    empty one - the TERS records, their readers and their file hashes stay as they were
    (the registry is the optical station's, ``mast.station``)."""
    if not no_stm():                 # no_stm() = "this is the optical station"
        return {}
    try:
        from mast.operator_hitl import optical_state

        return {"optical_state": json_safe(optical_state.get_store().snapshot())}
    except Exception as exc:  # noqa: BLE001
        logger.warning("optical state not recorded in the spectrum sidecar: %s", exc)
        return {"optical_state": None, "optical_state_error": f"{type(exc).__name__}: {exc}"}


def _coord_epoch() -> Any:
    try:
        from mast.core.coord_epoch import read_current_epoch

        return read_current_epoch()
    except Exception:  # noqa: BLE001
        return None


def _tip() -> Any:
    try:
        from mast.core.tip_state import current_tip_facts

        return json_safe(current_tip_facts())
    except Exception:  # noqa: BLE001
        return None


# ── stops (token-free, see instrument_lock.BYPASS_NAMES) ───────────────────


class StopSpectrometer(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="StopSpectrometer",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,   # a stop never waits for approval
            description=(
                tr("立即停止光谱采集。不给 device_id 就停所有已配置的光谱仪和卫星上所有 spectrometer 设备。"
                "不需要仪器令牌（别的技能占着仪器也能停）。注意：它只停采集，**关不了激光**。",
                   "Stop spectrum acquisition immediately. Without device_id, stops all configured spectrometers and all spectrometer devices on satellites. "
                   "Needs no instrument token (works even while another skill holds the instrument). Note: it only stops acquisition and **cannot turn off the laser**.")),
            parameters=[ParameterSpec(name="device_id", type="str", required=False, default="",
                                      description=tr("只停这一台；留空 = 全部", "Stop only this one; leave empty = all"))],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["optics", "spectrometer", "stop", "safety", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        reg = get_instrument_registry()
        stopped: list[str] = []
        errors: dict[str, str] = {}
        if params.get("device_id"):
            ids = [str(params["device_id"])]
        else:
            ids = [str(d.get("id")) for d in reg.find_devices(kind="spectrometer")]
        for dev in ids:
            try:
                reg.spectrometer(dev).stop()
                stopped.append(dev)
            except Exception as exc:  # noqa: BLE001
                errors[dev] = f"{type(exc).__name__}: {exc}"
        if not params.get("device_id"):
            try:
                from mast.instruments.remote import get_hub

                hub = get_hub()
                for sat, desc in hub.devices(kind="spectrometer") or []:
                    hub.stop_device(sat, desc.get("id"))
                    stopped.append(f"{sat}/{desc.get('id')}")
            except Exception as exc:  # noqa: BLE001 - no hub is not a failed stop
                errors.setdefault("hub", f"{type(exc).__name__}: {exc}")
        ok = bool(stopped) or not ids
        return SkillResult(skill_name="StopSpectrometer", success=ok,
                           data={"stopped": stopped, "errors": errors},
                           error="" if ok else f"nothing could be stopped: {errors}",
                           summary=f"stop sent to {stopped or 'nothing (none configured)'}")


class StopSatelliteDevices(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="StopSatelliteDevices",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("让卫星上的设备停下：每台设备发 stop（停轴、停采集、AOTF 关射频，具体由卫星驱动决定）。"
                "不给 satellite 就停所有卫星；给 device 只停这一台。不需要仪器令牌。"
                "这不是 E-STOP（E-STOP 由 MAST 急停统一下发）；它**关不了激光**。",
                   "Stop devices on satellites: sends stop to each device (stop axes, stop acquisition, turn the AOTF RF off; the exact behavior is decided by the satellite driver). "
                   "Without satellite, stops all satellites; with device, stops only that one. Needs no instrument token. "
                   "This is not an E-STOP (E-STOP is issued centrally by the MAST emergency stop); it **cannot turn off the laser**.")),
            parameters=[
                ParameterSpec(name="satellite", type="str", required=False, default="",
                              description=tr("卫星 id（satellites.json 里登记的 id）；留空 = 全部",
                                             "Satellite id (the id registered in satellites.json); leave empty = all")),
                ParameterSpec(name="device", type="str", required=False, default="",
                              description=tr("卫星上的设备 id（例如 pol）；留空 = 该卫星全部设备",
                                             "Device id on the satellite (e.g. pol); leave empty = all devices of that satellite")),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["optics", "satellite", "stop", "safety", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        sat_f = str(params.get("satellite") or "")
        dev_f = str(params.get("device") or "")
        try:
            from mast.instruments.remote import get_hub

            hub = get_hub()
            listed = list(hub.devices() or [])
        except Exception as exc:  # noqa: BLE001
            return fail("StopSatelliteDevices", InstrumentUnavailable(
                f"satellite hub unavailable, nothing was stopped: {exc}"))
        targets = [(str(s), str((d or {}).get("id"))) for s, d in listed
                   if (not sat_f or str(s) == sat_f) and (not dev_f or str((d or {}).get("id")) == dev_f)]
        for sat, dev in targets:
            try:
                hub.stop_device(sat, dev)
            except Exception as exc:  # noqa: BLE001 - stop_device must not raise; log if it does
                logger.warning("stop_device(%s, %s) raised: %s", sat, dev, exc)
        if (sat_f or dev_f) and not targets:
            return SkillResult(skill_name="StopSatelliteDevices", success=False,
                               error=f"no device matches satellite={sat_f!r} device={dev_f!r}",
                               data={"stopped": []})
        return SkillResult(skill_name="StopSatelliteDevices", success=True,
                           data={"stopped": [f"{s}/{d}" for s, d in targets]},
                           summary=f"stop sent to {len(targets)} satellite device(s)")
