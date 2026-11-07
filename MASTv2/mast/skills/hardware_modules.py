"""Optional hardware modules — licensed, but not necessarily present.

The operator has every Nanonis licence and only some of the hardware
(2026-07-13). So MAST ships skills for all of it, and turns almost all of it OFF.

WHY OFF BY DEFAULT IS NOT JUST TIDINESS
=======================================
A skill for hardware that is not plugged in is **worse than no skill at all**:

  * The agent sees it in its tool list, calls it, gets a Nanonis error, and — if
    it is stubborn — calls it again. That is a spin, and StallGuard then has to
    clean up a mess that should never have existed.
  * The tool list is a menu the model reads on **every turn**. instrument_control
    already carries ~280 tools; adding 50 more for a KPFM controller nobody owns
    costs routing accuracy on the tools that DO work, on every single call.

So a disabled module's skills are not merely refused — they are **not in the
agent's tool list at all**. It cannot call what it cannot see. The skills stay in
the SkillRegistry (the manual / GUI executor path still reaches them, which is
right: the operator may want to poke a module they just plugged in), but the
LLM's surface only ever contains hardware that is actually there.

Turning a module on is one switch in 设置 → 硬件模块.

WHAT IS **NOT** HERE
====================
Everything the base Nanonis V5e controller always has — bias, scan, Z-control,
spectroscopy, the lock-in, the piezos, the user outputs, the function generators,
the loggers, the markers. Those are never gated: gating them would mean the agent
could not drive the microscope.

WIRING (mirrors the ``mast.vision.thresholds`` live-read holder)
================================================================
A process-level holder keeps the ACTIVE set. The API/runtime writes it (startup
hydration from SettingsStore + each POST /api/settings); the tool-list builder
reads it. One-way: the skills layer never imports ``mast.webui``.

**A toggle is not live until the tool list is rebuilt.** The agent's tool list is
frozen at graph-build time — exactly the trap that made a deleted composite skill
stay callable (runtime._request_composite_rebuild). So the settings route calls
that same rebuild after flipping a switch, and says so in the UI.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from mast.i18n import tr

logger = logging.getLogger(__name__)

# The single persisted settings key. NB: a settings key that is not in
# SettingsStore.KNOWN_KEYS is silently dropped on save — it looks like it works
# and does nothing (that bug once made the admin PIN gate accept any text).
SETTINGS_KEY = "hardware_modules"


@dataclass(frozen=True)
class HardwareModule:
    """One optional module: what it is, what it needs, and which skills it owns."""

    id: str
    name: str                 # operator-facing, Chinese
    hardware: str             # what you must physically own for this to work
    description: str          # what it lets the agent do
    prefixes: tuple[str, ...]  # the Nanonis verb prefixes it covers
    skills: tuple[str, ...]    # the skill names it owns — the gate keys on THESE
    default_on: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# The registry. `skills` is the load-bearing field: build_tools() drops exactly
# these names from the agent's tool list when the module is off.
# ─────────────────────────────────────────────────────────────────────────────

MODULES: tuple[HardwareModule, ...] = (
    HardwareModule(
        id="osci_hr",
        name=tr("高分辨示波器 (OsciHR)", "High-resolution oscilloscope (OsciHR)"),
        hardware=tr("Nanonis Oscilloscope High Resolution 模块", "Nanonis Oscilloscope High Resolution module"),
        description=tr("高分辨采集：触发、校准模式、双通道数据、FFT/PSD。",
                         "High-resolution acquisition: trigger, calibration mode, dual-channel data, FFT/PSD."),
        prefixes=("OsciHR_",),
        skills=("ConfigureHighResScope", "RunHighResScope", "GetHighResScopeData",
                "GetHighResScopeStatus"),
    ),
    HardwareModule(
        id="osci_2t",
        name=tr("双通道示波器 (Osci2T) + Signal Chart", "Two-channel oscilloscope (Osci2T) + Signal Chart"),
        hardware=tr("Nanonis Osci 2-channel / Signal Chart 模块", "Nanonis Osci 2-channel / Signal Chart module"),
        description=tr("双通道时域采集与信号图表。pump-probe 常用。",
                         "Two-channel time-domain acquisition and signal chart. Common for pump-probe."),
        prefixes=("Osci2T_", "SignalChart_"),
        skills=("ConfigureDualScope", "GetDualScopeData", "ConfigureSignalChart"),
    ),
    HardwareModule(
        id="hs_sweeper",
        name=tr("高速扫描器 (HSSwp)", "High-speed sweeper (HSSwp)"),
        hardware=tr("Nanonis High-Speed Sweeper 模块", "Nanonis High-Speed Sweeper module"),
        description=tr("高速一维扫描：任意信号、自动反向、多通道同步采集。",
                         "High-speed 1D sweep: any signal, auto-reverse, multi-channel synchronous acquisition."),
        prefixes=("HSSwp_",),
        skills=("ConfigureHighSpeedSweep", "RunHighSpeedSweep", "StopHighSpeedSweep",
                "GetHighSpeedSweepStatus"),
    ),
    HardwareModule(
        id="aprf_gen",
        name=tr("任意波形 / 射频发生器 (APRFGen)", "Arbitrary waveform / RF generator (APRFGen)"),
        hardware=tr("Nanonis APRF Generator 模块", "Nanonis APRF Generator module"),
        description=tr("任意波形与射频输出、频率扫描。TERS / pump-probe 相关。",
                         "Arbitrary waveform and RF output, frequency sweeps. Related to TERS / pump-probe."),
        prefixes=("APRFGen_",),
        skills=("ConfigureRfGenerator", "StartRfGenerator", "StopRfGenerator",
                "RunRfFrequencySweep", "GetRfGeneratorStatus"),
    ),
    HardwareModule(
        id="kelvin",
        name=tr("Kelvin 控制器 (KPFM) + CPD 补偿", "Kelvin controller (KPFM) + CPD compensation"),
        hardware=tr("Nanonis Kelvin Controller 模块（KPFM）", "Nanonis Kelvin Controller module (KPFM)"),
        description=tr("接触电位差测量与补偿：Kelvin 反馈环、偏压限值、CPD 补偿。",
                         "Contact potential difference measurement and compensation: Kelvin feedback loop, bias limits, CPD compensation."),
        prefixes=("KelvinCtrl_", "CPDComp_"),
        skills=("ConfigureKelvinController", "SetKelvinControllerOnOff",
                "GetKelvinController", "RunCpdCompensation", "GetCpdCompensation"),
    ),
    HardwareModule(
        id="interferometer",
        name=tr("干涉仪 (Interf)", "Interferometer (Interf)"),
        hardware=tr("Nanonis Interferometer 模块（干涉式挠度检测）", "Nanonis Interferometer module (interferometric deflection detection)"),
        description=tr("干涉式位移检测的控制环与校准。",
                         "Control loop and calibration for interferometric displacement detection."),
        prefixes=("Interf_",),
        skills=("ConfigureInterferometer", "SetInterferometerOnOff",
                "GetInterferometer"),
    ),
    HardwareModule(
        id="beam_deflection",
        name=tr("光束偏转 (BeamDefl)", "Beam deflection (BeamDefl)"),
        hardware=tr("Nanonis Beam Deflection 模块（AFM 光杠杆）", "Nanonis Beam Deflection module (AFM optical lever)"),
        description=tr("光杠杆的水平/垂直/和信号配置与自动调零。",
                         "Horizontal / vertical / sum signal configuration and auto-zero for the optical lever."),
        prefixes=("BeamDefl_",),
        skills=("ConfigureBeamDeflection", "GetBeamDeflection",
                "AutoZeroBeamDeflection"),
    ),
    HardwareModule(
        id="laser",
        name=tr("激光模块 (Laser)", "Laser module (Laser)"),
        hardware=tr("Nanonis Laser 模块", "Nanonis Laser module"),
        description=tr("激光开关与功率控制。", "Laser on/off and power control."),
        prefixes=("Laser_",),
        skills=("SetLaserOnOff", "SetLaserPower", "GetLaser"),
    ),
    HardwareModule(
        id="multiprobe",
        name=tr("多探针 (MProbe)", "Multi-probe (MProbe)"),
        hardware=tr("多探针系统（4-probe STM 等）", "Multi-probe system (4-probe STM etc.)"),
        description=tr("每根探针独立的 Z 控制、扫描器、偏压、电流。",
                         "Per-probe Z control, scanner, bias and current."),
        prefixes=("MProbeZCtrl_", "MProbeScanner_", "MProbeBias_", "MProbeCurrent_"),
        skills=("SetProbeZController", "GetProbeZController", "WithdrawProbe",
                "ConfigureProbeScanner", "MoveProbeXY", "StopProbeScanner",
                "SetProbeBias", "PulseProbeBias", "GetProbeBias", "GetProbeCurrent",
                "ConfigureProbeCurrentGain"),
    ),
    HardwareModule(
        id="pi_controller",
        name=tr("通用 PI 控制器 (PICtrl / GenPICtrl)", "Generic PI controller (PICtrl / GenPICtrl)"),
        hardware=tr("Nanonis Generic PI Controller 模块", "Nanonis Generic PI Controller module"),
        description=tr("把任意信号锁到设定值的通用 PI 环（V5e 与 V5 两代）。",
                         "Generic PI loop that locks any signal to a setpoint (V5e and V5 generations)."),
        prefixes=("PICtrl_", "GenPICtrl_"),
        skills=("ConfigurePiController", "SetPiControllerOnOff", "GetPiController",
                "SetGenericPiOutput", "GetGenericPiController"),
    ),
    HardwareModule(
        id="preamp_mcva5",
        name=tr("MCVA5 前置放大器", "MCVA5 preamplifier"),
        hardware="MCVA5 preamplifier",
        description=tr("前放的增益 / 耦合 / 温度与状态读取。",
                         "Preamp gain / coupling / temperature and status readout."),
        prefixes=("MCVA5_",),
        skills=("ConfigurePreamp", "GetPreamp"),
    ),
    HardwareModule(
        id="pll_analysis",
        name=tr("PLL 分析 (Zoom FFT / 相位扫描 / 信号分析仪)", "PLL analysis (Zoom FFT / phase sweep / signal analyzer)"),
        hardware=tr("Nanonis PLL 模块的分析扩展", "Analysis extension of the Nanonis PLL module"),
        description=tr("PLL 的 Zoom FFT 频谱、相位扫描、信号分析仪。",
                         "PLL Zoom FFT spectrum, phase sweep and signal analyzer."),
        prefixes=("PLLZoomFFT_", "PLLPhasSwp_", "PLLSignalAnlzr_"),
        skills=("RunPllZoomFft", "GetPllZoomFftData", "RunPllPhaseSweep",
                "StopPllPhaseSweep", "ConfigurePllSignalAnalyzer",
                "GetPllSignalAnalyzerData"),
    ),
    HardwareModule(
        id="oc_sync",
        name="OC Sync",
        hardware=tr("Nanonis OC Sync 模块", "Nanonis OC Sync module"),
        description=tr("振荡控制同步：相位角与链接。", "Oscillation control sync: phase angle and link."),
        prefixes=("OCSync_",),
        skills=("ConfigureOcSync", "GetOcSync"),
    ),
    HardwareModule(
        id="tip_recorder",
        name=tr("针尖移动记录器 (TipRec)", "Tip move recorder (TipRec)"),
        hardware=tr("Nanonis Tip Move Recorder 模块", "Nanonis Tip Move Recorder module"),
        description=tr("记录针尖的移动轨迹到缓冲区。", "Records the tip movement trajectory into a buffer."),
        prefixes=("TipRec_",),
        skills=("ConfigureTipRecorder", "GetTipRecorderData"),
    ),
    # TERS 光学侧。卫星节点（光谱电脑：LightField CCD、KDC101 偏振片、XYZ 台、
    # Picomotor、AOTF、PMT）与 Nanonis 的光学电机组（反射镜 / Beam Deflection / 透镜）。
    # 没有这套光学侧的机器（只有 STM 一组粗动的那种）就保持关闭：一台不存在的光谱仪的技能只会让
    # agent 反复撞 unavailable。已有的光学台技能（OpticalStage* / PumpProbeScan）不归它管。
    HardwareModule(
        id="ters_optics",
        name=tr("TERS 光学 / 光谱（卫星设备 + Nanonis 光学电机组）",
                "TERS optics / spectroscopy (satellite devices + Nanonis optical motor group)"),
        hardware=tr("卫星节点（光谱电脑）与 Nanonis PMD4 上标为光学用途的电机组",
                  "Satellite node (spectrometer PC) and the motor groups on the Nanonis PMD4 marked for optics"),
        description=tr("拉曼 CCD 采谱与设置、偏振片、XYZ 对准台、Picomotor 光束微调、AOTF、PMT，"
                       "Nanonis 光学电机组的列表与走步，以及建立在它们之上的 TERS 联动模式"
                       "（单点/近远场/线扫/面扫/触发面扫/时间·偏振·偏压·设定点序列/泵浦-探测光谱/"
                       "激光聚焦图），对光相机（Zelux）的连接、直播、快照与曝光/增益/ROI。",
                       "Raman CCD spectrum acquisition and setup, polarizer, XYZ alignment stage, "
                       "Picomotor beam fine-tuning, AOTF, PMT, listing and stepping of the Nanonis "
                       "optical motor groups, and the TERS linked modes built on them (single "
                       "point / near-far / line scan / area scan / triggered area scan / "
                       "time, polarization, bias and setpoint series / pump-probe spectra / "
                       "laser focus map), plus the alignment camera (Zelux): connect, live view, "
                       "snapshot, and exposure / gain / ROI."),
        # 没有专属的 Nanonis 动词前缀：卫星设备不走 Nanonis，光学电机组用的 Motor_* 动词
        # 与 STM 粗动共用（STM 粗动是基础能力，不能被这个开关关掉）。
        prefixes=(),
        skills=("ListSatellites", "GetSpectrometerStatus", "ConfigureSpectrometer",
                "SetSpectrometerGrating", "SetSpectrometerCenter", "SetSpectrometerExitPort",
                "AcquireSpectrum", "StopSpectrometer", "StopSatelliteDevices",
                "GetPolarizerAngle", "SetPolarizerAngle", "HomePolarizer",
                "GetPicomotorSteps", "PicomotorJog", "MoveXYZStage",
                "ListNanonisMotorGroups", "NanonisMotorGroupMove",
                "SetAotfWavelength", "ReadPmt", "SetPmtHighVoltage",
                # 手动控制面板的卫星设备按钮（optics_satellite_manual.py，契约 §13.1）
                "StopSatelliteDevice", "PicomotorAbort", "IdentifySatelliteDevice",
                "GetSatelliteDeviceStatus", "SetPolarizerVelocity", "PicomotorMoveAbs",
                "SetPicomotorVelocity", "PicomotorSetZero", "SetPicomotorAccel",
                # 对光相机（optics_camera.py，契约 §13.1）：不碰 Nanonis，不取仪器令牌
                "ConnectCamera", "DisconnectCamera", "CameraStartLive", "CameraStopLive",
                "CameraSnapshot", "SetCameraExposure", "SetCameraGain", "SetCameraROI",
                "GetCameraStatus",
                # TERS 组合技能（skills/composite/ters_*.py）
                "TERSPointSpectrum", "TERSNearFarPair", "PumpProbeSpectral",
                "TERSLineProfile", "TERSGridMap", "TERSTriggeredMap",
                "TERSTimeSeries", "TERSPolarizationSeries", "TERSBiasSeries",
                "TERSSetpointSeries", "LaserFocusMap"),
    ),
)

MODULE_BY_ID: dict[str, HardwareModule] = {m.id: m for m in MODULES}

# skill name → module id. Built once; the gate is a dict lookup, not a scan.
SKILL_OWNER: dict[str, str] = {
    skill: m.id for m in MODULES for skill in m.skills
}


DEFAULT_ENABLED: frozenset[str] = frozenset(m.id for m in MODULES if m.default_on)

#: Modules a **station profile** switches on by itself (``mast.station``; design
#: ``docs/v2/design/optical_station_witec.md`` D1). On an optical station the optical bench
#: (spectrometer, polarizer, camera ...) is not "licensed hardware the machine may lack": it
#: is the machine. Left to the operator's switch, a fresh install on the WITec computer would
#: refuse ``AcquireSpectrum`` at the external door until someone found 设置 → 硬件模块.
#: The implication is read live, so it survives any write to the ``hardware_modules``
#: setting (a UI form that does not send ``ters_optics`` cannot turn it off). The Nanonis-bound
#: members of the module (``NanonisMotorGroupMove``, the TERS composites) are still withheld
#: on that station by ``skills.station_face``.
PROFILE_IMPLIED_MODULES: dict[str, frozenset[str]] = {
    "optical": frozenset({"ters_optics"}),
}


def _profile_implied() -> frozenset[str]:
    try:
        from mast import station

        return PROFILE_IMPLIED_MODULES.get(station.current_profile(), frozenset())
    except Exception:  # noqa: BLE001 - no profile information: the operator's switches only
        return frozenset()


# ─────────────────────────────────────────────────────────────────────────────
# The holder + the gate
# ─────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_enabled: frozenset[str] = DEFAULT_ENABLED   # read lock-free; swapped under _lock


def enabled_ids() -> frozenset[str]:
    """The module ids currently switched ON: the operator's switches, plus what the
    station profile implies (:data:`PROFILE_IMPLIED_MODULES`; nothing on an STM station).
    Lock-free (a CPython attribute read is atomic — a reader never sees a half-updated
    set; the writer swaps the whole frozenset)."""
    on = _enabled
    implied = _profile_implied()
    return on | implied if implied else on


def set_enabled(raw) -> frozenset[str]:
    """Install the active set. Called by the API on startup hydration and on each
    settings write. Accepts the persisted ``{id: bool}`` map, or any iterable of
    ids.

    **Fail-closed on garbage.** A value we cannot read means the stored state is
    unknown, and an unknown state is not "everything is plugged in" — it is the
    defaults, which for every optional module is OFF. Unknown ids are dropped
    with a warning rather than silently kept: a typo'd id that lingers in the
    persisted blob would otherwise look enabled forever in the JSON while gating
    nothing.
    """
    global _enabled
    if isinstance(raw, dict):
        wanted = {str(k) for k, v in raw.items() if bool(v)}
    elif isinstance(raw, (list, tuple, set, frozenset)):
        wanted = {str(x) for x in raw}
    else:
        if raw is not None:
            logger.warning("hardware_modules: 无法解析的开关状态 %r；按默认（全关）", type(raw))
        wanted = set(DEFAULT_ENABLED)

    unknown = wanted - set(MODULE_BY_ID)
    if unknown:
        logger.warning("hardware_modules: 忽略未知模块 id %s", sorted(unknown))
    new = frozenset(wanted & set(MODULE_BY_ID))
    with _lock:
        _enabled = new
    logger.info("hardware_modules: 已启用 %s", sorted(new) or "（无——全部硬件模块关闭）")
    return new


def disabled_skill_names() -> frozenset[str]:
    """Skill names that must NOT reach the agent's tool list."""
    on = enabled_ids()
    return frozenset(s for s, mod in SKILL_OWNER.items() if mod not in on)


def module_states() -> list[dict]:
    """Operator-facing view: every optional module, on/off, and what it needs."""
    on = enabled_ids()
    return [
        {
            "id": m.id,
            "name": m.name,
            "hardware": m.hardware,
            "description": m.description,
            "enabled": m.id in on,
            "skill_count": len(m.skills),
            "skills": list(m.skills),
        }
        for m in MODULES
    ]


__all__ = [
    "HardwareModule", "MODULES", "MODULE_BY_ID", "SKILL_OWNER", "SETTINGS_KEY",
    "DEFAULT_ENABLED", "enabled_ids", "set_enabled", "disabled_skill_names",
    "module_states",
]
