"""M10 泵浦–探测（谱读出）—— PumpProbeSpectral（规格 docs/v2/design/ters_modes_spec.md M10；契约 §8.3）。

延迟线逐点步进，每点取一帧谱（和 / 或在曝光窗口内读 Nanonis 电流 / 锁相），得到谱–延迟曲线；
迁移的是既有的 LabVIEW 泵浦–探测流程。**电读出已有**：``readout=electrical`` 直接交给现有的
PumpProbeScan（每个方向 / 重复一次子技能调用），这里不重复实现。

两种谱读出方式（``trigger_scheme``）：

* ``software_pulse``（默认，S+TTL，M10.3 / M03 S+TTL 那一套）：LightField 预设 N 帧
  ReadoutPerTrigger，一次长作业（:class:`~ters_common.TriggeredAcquisition`）；MAST 每点：
  延迟线到位 → 驻留 → （每 ``stm_check_every`` 点）判稳 → 触发线打 5 ms 脉冲 → 曝光窗口
  （T + 读出 22.4 ms + 裕量）内只读电流 / 锁相 → 等这一帧到达。结束时三重对账（帧数 = 触发数、
  帧号连续、曝光间隔 ≈ 触发间隔）；对不上 ⇒ ``count_mismatch``，错位点之后的数据标 invalid、绝不修补。
  Port A 一律拒绝（接着 PMD4 粗动控制器）；线号起点（``trigger_line_verified``）与电平 / 触发沿
  （``trigger_level_verified``）没在真机核实之前只允许 dry_run；真跑还要延迟台 Trigger In 未接线
  （``pdxc2_trigger_unwired``）。
* ``software_acquire``（S，偏差：规格只写了 S+TTL）：每点一次 AcquireSpectrum，不需要触发接线，慢
  （每点多一次命令往返）；``both`` 时电流取曝光窗口里守卫的 20 Hz 采样（不读锁相）。

dry_run 的两档与 PumpProbeScan 不同：这里 ``dry_run=true`` 只检查限值与延迟范围、不动台子；
``dry_run_motion=true`` 才把延迟线走完全程（照样判稳、照样回起点），不触发、不读出 —— 这一档才相当于
PumpProbeScan 自己的 ``dry_run``。``readout=electrical`` 时 dry_run_motion 就是转交 ``PumpProbeScan(dry_run=true)``。
"""

from __future__ import annotations

import math
import time
from typing import Iterator

import numpy as np

from mast.core.types import ParameterSpec, SkillMetadata, SkillResult
from mast.i18n import tr as _tr_lang
from mast.skills.builtins.optics_acquire import parse_indices, resolve_lockin_index
from mast.skills.composite import ters_analysis as ta
from mast.skills.composite.graph_executor import CompositeStep
from mast.skills.composite.ters_common import (
    TTL_SAFE_NOTE,
    GuardSpec,
    Needs,
    TersComposite,
    TersRun,
    TriggeredAcquisition,
    common_extra,
    cp_a,
    cp_b,
    cp_c,
    ters_metadata,
    ttl_params,
    ttl_refusals,
)

__all__ = ["PumpProbeSpectral", "PYLON_READOUT_S"]

#: PyLoN LineSensor 50 行 @ 1 MHz 的读出时间（规格 §1.1）。像素周期 = 曝光 + 读出 + 裕量。
PYLON_READOUT_S = 0.0224
_EDGES = {"rising": "RisingEdge", "falling": "FallingEdge"}


class PumpProbeSpectral(TersComposite):
    MODE_ID = "M10"
    MODE_KEY = "pump_probe"

    def metadata(self) -> SkillMetadata:
        inf = _tr_lang("【默认值可调】", " [default adjustable]")
        mode = [
            ParameterSpec(name="readout", type="str", required=False, default="both",
                          allowed_values=["spectrum", "electrical", "both"],
                          description=_tr_lang("读出：谱 / 电（电流、锁相；等价于现有 PumpProbeScan，直接交给它）/ 两者。",
                                               "Readout: spectrum / electrical (current, lock-in; equivalent to the "
                                               "existing PumpProbeScan, handed to it directly) / both.") + inf),
            ParameterSpec(name="delay_start_ps", type="float", unit="ps", required=True,
                          min_value=-1e5, max_value=1e5,
                          description=_tr_lang("起始延迟（同 PumpProbeScan）。",
                                               "Start delay (same as PumpProbeScan).")),
            ParameterSpec(name="delay_stop_ps", type="float", unit="ps", required=True,
                          min_value=-1e5, max_value=1e5,
                          description=_tr_lang("终止延迟。", "Stop delay.")),
            ParameterSpec(name="points", type="int", required=True, min_value=2, max_value=10000,
                          description=_tr_lang("延迟点数（含首尾）。",
                                               "Number of delay points (endpoints included).")),
            ParameterSpec(name="delay_offset_ps", type="float", unit="ps", required=False, default=0.0,
                          min_value=-1000.0, max_value=1000.0,
                          description=_tr_lang("加到所有延迟上的偏移（t0 修正）。",
                                               "Offset added to all delays (t0 correction).") + inf),
            ParameterSpec(name="directions", type="str", required=False, default="forward",
                          allowed_values=["forward", "backward", "both"],
                          description=_tr_lang("扫描方向；both = 往返，检验滞后与漂移。",
                                               "Sweep direction; both = there and back, to check hysteresis and "
                                               "drift.") + inf),
            ParameterSpec(name="repeats", type="int", required=False, default=1, min_value=1,
                          max_value=1000,
                          description=_tr_lang("整条扫描的重复次数。",
                                               "Number of repeats of the whole sweep.") + inf),
            ParameterSpec(name="tip_state", type="str", required=False, default="tunneling",
                          allowed_values=["tunneling", "retracted", "ignore"],
                          description=_tr_lang("STM 状态要求：隧穿 / 已撤针（纯光学）/ 不检查。",
                                               "STM state requirement: tunneling / tip withdrawn (pure optics) / "
                                               "not checked.") + inf),
            ParameterSpec(name="stm_check_every", type="int", required=False, default=20,
                          min_value=0, max_value=10000,
                          description=_tr_lang("每 n 个延迟点判稳一次（0 = 不判；只在 tunneling 时）。",
                                               "Judge stability once every n delay points (0 = never; only when "
                                               "tunneling).") + inf),
            ParameterSpec(name="settle_extra_s", type="float", unit="s", required=False, default=0.1,
                          min_value=0.0, max_value=10.0,
                          description=_tr_lang("台子报到位后的额外驻留（stick-slip 振动建议 0.1–0.2）。",
                                               "Extra dwell after the stage reports in position (0.1-0.2 suggested "
                                               "for stick-slip vibration).")),
            ParameterSpec(name="samples_per_point", type="int", required=False, default=10,
                          min_value=1, max_value=1000,
                          description=_tr_lang("电读出每点平均次数。",
                                               "Number of electrical readings averaged per point.")),
            ParameterSpec(name="sample_interval_s", type="float", unit="s", required=False,
                          default=0.01, min_value=0.0, max_value=1.0,
                          description=_tr_lang("电读出两次读数间隔。",
                                               "Interval between two electrical readings.")),
            ParameterSpec(name="read_current", type="bool", required=False, default=True,
                          description=_tr_lang("电读出：隧穿电流（Current_Get）。",
                                               "Electrical readout: tunneling current (Current_Get).")),
            ParameterSpec(name="read_lockin", type="bool", required=False, default=False,
                          description=_tr_lang("电读出：锁相解调（需锁相调制开启；本模式前置检查对此例外）。",
                                               "Electrical readout: lock-in demodulation (needs lock-in "
                                               "modulation on; this mode's precheck makes an exception for "
                                               "it).")),
            ParameterSpec(name="lockin_signal_index", type="int", required=False, default=-1,
                          min_value=-1, max_value=127,
                          description=_tr_lang("锁相输出所在信号槽位；-1 = 自动发现。",
                                               "Signal slot holding the lock-in output; -1 = auto-discover.")),
            ParameterSpec(name="lockin_demod", type="int", required=False, default=1, min_value=1,
                          max_value=8,
                          description=_tr_lang("自动发现用的解调器编号。",
                                               "Demodulator number used for auto-discovery.")),
            ParameterSpec(name="extra_signal_indices", type="str", required=False, default="",
                          description=_tr_lang("每点另读的 Nanonis 信号槽位（逗号分隔，如 '14' = Z）。",
                                               "Extra Nanonis signal slots read at each point (comma-separated, "
                                               "e.g. '14' = Z).")),
            ParameterSpec(name="return_to_start", type="bool", required=False, default=True,
                          description=_tr_lang("扫完回到 delay_start_ps（中止之后不回）。",
                                               "Return to delay_start_ps after the sweep (not after an abort).")),
            ParameterSpec(name="move_timeout_s", type="float", unit="s", required=False, default=60.0,
                          min_value=0.1, max_value=600.0,
                          description=_tr_lang("每点运动时限。", "Motion time limit per point.")),
            ParameterSpec(name="bands_cm1", type="str", required=False, default="",
                          description=_tr_lang("在线强度用的谱带 '名称:lo-hi,…'（cm⁻¹）；空 = 总计数。",
                                               "Spectral bands for the online intensity, 'name:lo-hi,...' "
                                               "(cm⁻¹); empty = total counts.") + inf),
            ParameterSpec(name="baseline_cm1", type="str", required=False, default="",
                          description=_tr_lang("基线窗口；空 = 谱带两侧各 20 cm⁻¹。",
                                               "Baseline windows; empty = 20 cm⁻¹ on each side of the band.")),
            ParameterSpec(name="signal_drop_frac", type="float", required=False, default=0.0,
                          min_value=0.0, max_value=1.0,
                          description=_tr_lang("信号衰减监视（M09 同款）：低于滚动中位数 ×(1 − frac) 连续 drop_frames 点 "
                                               "⇒ 标记；0 = 关闭。",
                                               "Signal decay monitor (same as M09): below the rolling median x "
                                               "(1 - frac) for drop_frames consecutive points => flag; 0 = "
                                               "off.") + inf),
            ParameterSpec(name="drop_frames", type="int", required=False, default=5, min_value=1,
                          max_value=1000,
                          description=_tr_lang("连续多少点低于阈值才算衰减。",
                                               "How many consecutive points below the threshold count as "
                                               "decay.")),
            ParameterSpec(name="stop_on_drop", type="bool", required=False, default=False,
                          description=_tr_lang("衰减时是否停下（needs_operator）。",
                                               "Whether to stop on decay (needs_operator).") + inf),
            # trigger_* — M03 的定义
            ParameterSpec(name="trigger_scheme", type="str", required=False, default="software_pulse",
                          allowed_values=["software_pulse", "software_acquire"],
                          description=_tr_lang("software_pulse = S+TTL（Nanonis 数字线逐点触发相机，一次 N 帧采集）；"
                                               "software_acquire = S（每点一次 AcquireSpectrum，无需触发接线，较慢）。",
                                               "software_pulse = S+TTL (a Nanonis digital line triggers the camera "
                                               "point by point, one N-frame acquisition); software_acquire = S (one "
                                               "AcquireSpectrum per point, no trigger wiring needed, slower).")),
            ParameterSpec(name="trigger_port", type="int", required=False, default=1, min_value=1,
                          max_value=3,
                          description=_tr_lang("触发数字端口：1 = Port B（按 Nanonis 编号）。Port A（0）不可选：它接着 PMD4 "
                                               "粗动控制器；本机配置登记的保留口同样拒绝。",
                                               "Trigger digital port: 1 = Port B (Nanonis numbering). Port A (0) cannot be chosen: it is connected to the PMD4 "
                                               "coarse-motion controller; reserved ports registered in this "
                                               "machine's configuration are refused as well.")),
            ParameterSpec(name="trigger_line", type="int", required=False, default=2, min_value=1,
                          max_value=8,
                          description=_tr_lang("触发数字线（编号起点待真机核实，见 trigger_line_verified）。",
                                               "Trigger digital line (numbering origin to be verified on the real "
                                               "instrument, see trigger_line_verified).")),
            ParameterSpec(name="trigger_width_s", type="float", unit="s", required=False, default=0.005,
                          min_value=1e-6, max_value=60.0,
                          description=_tr_lang("脉冲宽度（默认 5 ms）。",
                                               "Pulse width (default 5 ms).")),
            ParameterSpec(name="trigger_edge", type="str", required=False, default="rising",
                          allowed_values=["rising", "falling"],
                          description=_tr_lang("相机触发沿。PyLoN 默认 Falling，外部触发源的极性请核对。",
                                               "Camera trigger edge. PyLoN defaults to Falling; check the "
                                               "polarity of the external trigger source.")),
            ParameterSpec(name="trigger_response", type="str", required=False,
                          default="ReadoutPerTrigger",
                          allowed_values=["ReadoutPerTrigger", "ExposeDuringTriggerPulse",
                                          "StartOnSingleTrigger"],
                          description=_tr_lang("LightField TriggerResponse（脉冲式泵浦–探测用 ReadoutPerTrigger）。",
                                               "LightField TriggerResponse (ReadoutPerTrigger for pulsed "
                                               "pump-probe).")),
            ParameterSpec(name="pixel_margin_s", type="float", unit="s", required=False, default=0.05,
                          min_value=0.0, max_value=5.0,
                          description=_tr_lang("点周期裕量：点周期 = 曝光 + 读出（22.4 ms）+ 裕量。",
                                               "Point-period margin: point period = exposure + readout (22.4 ms) "
                                               "+ margin.") + inf),
            ParameterSpec(name="on_count_mismatch", type="str", required=False, default="truncate",
                          allowed_values=["truncate", "abort"],
                          description=_tr_lang("帧数 ≠ 触发数：truncate = 保留错位点之前的点；abort = 整次作废。",
                                               "Frame count != trigger count: truncate = keep the points before "
                                               "the misaligned one; abort = void the whole run.") + inf),
            *ttl_params(),
            ParameterSpec(name="pdxc2_trigger_unwired", type="bool", required=False, default=False,
                          description=_tr_lang("操作员已确认延迟台的 Trigger In 没有接线（接着线时任何数字线上升沿都可能让延迟台跳动）。software_pulse 时必须为 true。",
                                               "The operator has confirmed that the delay stage Trigger In is not wired "
                                               "(when a line is connected, a rising edge on any digital line can make the delay stage "
                                               "jump). Must be true for software_pulse.")),
            # secondary_* — PumpProbeScan 的 2D 延迟图（只在 electrical 读出时转交）
            ParameterSpec(name="secondary_device_id", type="str", required=False, default="",
                          description=_tr_lang("第二台台子（2D 延迟图）。只支持 readout=electrical（转交 PumpProbeScan）。",
                                               "Second stage (2D delay map). Only supported with "
                                               "readout=electrical (handed over to PumpProbeScan).")),
            ParameterSpec(name="secondary_axis", type="str", required=False, default="",
                          description=_tr_lang("第二台台子的轴名。", "Axis name of the second stage.")),
            ParameterSpec(name="secondary_position", type="float", required=False, default=0.0,
                          min_value=-1e6, max_value=1e6,
                          description=_tr_lang("secondary_scan=false 时停在哪里（原生单位）。",
                                               "Where to rest when secondary_scan=false (native units).")),
            ParameterSpec(name="secondary_scan", type="bool", required=False, default=False,
                          description=_tr_lang("把第二台台子当第二条延迟轴扫。",
                                               "Scan the second stage as a second delay axis.")),
            ParameterSpec(name="secondary_start", type="float", required=False, default=0.0,
                          min_value=-1e6, max_value=1e6,
                          description=_tr_lang("第二台台子扫描起点（原生单位）。",
                                               "Scan start of the second stage (native units).")),
            ParameterSpec(name="secondary_stop", type="float", required=False, default=0.0,
                          min_value=-1e6, max_value=1e6,
                          description=_tr_lang("第二台台子扫描终点（原生单位）。",
                                               "Scan end of the second stage (native units).")),
            ParameterSpec(name="secondary_points", type="int", required=False, default=2, min_value=2,
                          max_value=1000,
                          description=_tr_lang("第二台台子点数。", "Number of points of the second stage.")),
        ]
        return ters_metadata(
            name="PumpProbeSpectral",
            description=_tr_lang(
                "泵浦–探测的谱读出（M10）：延迟线逐点步进，每点取一帧谱（并可在曝光窗口内读电流 / 锁相），"
                "得到谱–延迟曲线；迁移既有的 LabVIEW 泵浦–探测流程。默认 S+TTL：LightField 预设 N 帧 ReadoutPerTrigger，"
                "每点到位后在 Nanonis 触发线打一个 5 ms 脉冲（Port A 一律拒绝：PMD4 粗动控制器），结束时对账"
                "帧数 = 触发数、帧号连续、曝光间隔 ≈ 触发间隔，对不上就标 count_mismatch、不修补。线号起点与触发电平"
                "没在真机核实之前（trigger_line_verified / trigger_level_verified）只允许 dry_run；真跑还要确认 "
                "延迟台 Trigger In 未接线。readout=electrical 直接交给现有的 PumpProbeScan。"
                "LiveHub：local/pump_probe/<run_id>（x = 延迟 ps；各谱带强度、电流、锁相）。"
                "dry_run=true 只检查限值与延迟范围，不动台子；dry_run_motion=true 时延迟线走完全程（照样判稳、回起点）、"
                "不触发不读出 —— 这一档才相当于 PumpProbeScan 自己的 dry_run。",
                "Pump-probe spectral readout (M10): the delay line steps point by point and takes one spectrum "
                "frame at each point (optionally reading current / lock-in inside the exposure window), giving a "
                "spectrum-vs-delay curve; migrates the existing LabVIEW pump-probe procedure. Default is S+TTL: LightField is "
                "preset to N frames of ReadoutPerTrigger, and once each point is in position a 5 ms pulse is fired "
                "on the Nanonis trigger line (Port A is always refused: PMD4 coarse-motion controller); at the end "
                "the frame count = trigger count, consecutive frame numbers and exposure interval ~ trigger "
                "interval are reconciled, and any mismatch is flagged count_mismatch and never patched. Until the "
                "line-number origin and the trigger level have been verified on the real instrument "
                "(trigger_line_verified / trigger_level_verified) only dry_run is allowed; a real run also "
                "requires confirming that the delay stage Trigger In is not wired. readout=electrical is handed directly to "
                "the existing PumpProbeScan. LiveHub: local/pump_probe/<run_id> (x = delay in ps; per-band "
                "intensities, current, lock-in). dry_run=true only checks limits and the delay range and does not "
                "move the stage; with dry_run_motion=true the delay line walks the whole range (still judging "
                "stability and returning to the start) without triggering or reading out - only this level is "
                "equivalent to PumpProbeScan's own dry_run."),
            parameters=[*mode,
                        *cp_a(overrides={"exposure_ms": 100.0, "shutter_mode": "AlwaysOpen"}),
                        *cp_b(notes={"settle_s": _tr_lang("（只在 tip_state=tunneling 判稳时用）",
                                                          " (only used when judging stability with "
                                                          "tip_state=tunneling)")}),
                        *cp_c(), *common_extra()],
            tags=["optics", "pump_probe", "delay_line", "spectrum", "acquisition"],
            duration_s=300.0,
        )

    # -- requirements --
    def needs(self, run: TersRun) -> Needs:
        p = run.p
        tip = {"tunneling": "tunneling", "retracted": "withdrawn"}.get(p["tip_state"], "any")
        return Needs(tip=tip, spectrometer=p["readout"] != "electrical",
                     lockin_off=not p["read_lockin"], safetip=tip == "tunneling")

    def _delays(self, run: TersRun) -> list[float]:
        p = run.p
        a, b, n = float(p["delay_start_ps"]), float(p["delay_stop_ps"]), int(p["points"])
        off = float(p["delay_offset_ps"])
        return [a + (b - a) * k / (n - 1) + off for k in range(n)]

    def _sweeps(self, run: TersRun) -> list[tuple[int, str, list[float]]]:
        d = self._delays(run)
        dirs = {"forward": ["fwd"], "backward": ["bwd"], "both": ["fwd", "bwd"]}[run.p["directions"]]
        out = []
        for r in range(int(run.p["repeats"])):
            for direction in dirs:
                out.append((r, direction, d if direction == "fwd" else list(reversed(d))))
        return out

    def prepare(self, run: TersRun) -> list[str]:
        p = run.p
        out = []
        if p["readout"] != "electrical" and p.get("secondary_device_id"):
            out.append(_tr_lang("secondary_*（2D 延迟图）只支持 readout=electrical（转交 PumpProbeScan）；谱读出的 2D 图尚未实现",
                                "secondary_* (2D delay map) is only supported with readout=electrical (handed over "
                                "to PumpProbeScan); the 2D map for spectral readout is not implemented yet"))
        if p["readout"] == "electrical" and not (p["read_current"] or p["read_lockin"]
                                                 or parse_indices(p.get("extra_signal_indices") or "")):
            out.append(_tr_lang("readout=electrical 却什么都不读：打开 read_current / read_lockin 或给 extra_signal_indices",
                                "readout=electrical but nothing is read: enable read_current / read_lockin or give "
                                "extra_signal_indices"))
        scheme = p["trigger_scheme"]
        if p["readout"] != "electrical":
            if scheme == "software_pulse":
                full, hard = ttl_refusals(p, dry_run=False), ttl_refusals(p, dry_run=True)
                if not p["pdxc2_trigger_unwired"]:
                    full.append(_tr_lang("software_pulse 需要 pdxc2_trigger_unwired=true：延迟台 Trigger In 接着线时数字线"
                                         "上升沿可能让延迟台跳动",
                                         "software_pulse needs pdxc2_trigger_unwired=true: when the delay stage Trigger In is "
                                         "wired, a rising edge on a digital line can make the delay stage jump"))
                if run.dry_run:            # the plan says what a real run would still be refused for
                    out += hard
                    run.plan["real_run_needs"] = [r for r in full if r not in hard]
                else:
                    out += full
            if scheme == "software_pulse" and run.resume_of:
                out.append(_tr_lang("S+TTL 不能续跑：卫星上那次 N 帧外触发序列随中断丢失，触发与帧的对应无法接上 —— "
                                    "请重新开始（resume_run_id 留空）",
                                    "S+TTL cannot be resumed: the N-frame externally triggered sequence on the "
                                    "satellite was lost with the interruption, so triggers can no longer be matched "
                                    "to frames - please start over (leave resume_run_id empty)"))
            if scheme == "software_acquire" and p["read_lockin"]:
                out.append(_tr_lang("trigger_scheme=software_acquire 时曝光期间读不了锁相（主线程阻塞在 AcquireSpectrum）"
                                    "——要锁相请用 software_pulse，或 readout=electrical",
                                    "With trigger_scheme=software_acquire the lock-in cannot be read during the "
                                    "exposure (the main thread is blocked in AcquireSpectrum) - use software_pulse "
                                    "for lock-in, or readout=electrical"))
            if p["trigger_response"] == "ExposeDuringTriggerPulse" and scheme == "software_pulse":
                exp_s = float(p["exposure_ms"]) / 1000.0
                if abs(float(p["trigger_width_s"]) - exp_s) > 1e-3:
                    out.append(_tr_lang("ExposeDuringTriggerPulse 时 trigger_width_s 就是曝光，要等于 exposure_ms",
                                        "With ExposeDuringTriggerPulse, trigger_width_s is the exposure and must "
                                        "equal exposure_ms"))
        n_sweeps = len(self._sweeps(run))
        n = int(p["points"]) * n_sweeps
        run.total_points = n
        t_pt = (float(p["exposure_ms"]) / 1000.0 + PYLON_READOUT_S + float(p["pixel_margin_s"])
                + 0.15 + float(p["settle_extra_s"]))
        if scheme == "software_acquire":
            t_pt += 0.4
        est = 20.0 + n * t_pt
        run.plan.update({"delays_ps": [round(v, 6) for v in self._delays(run)][:50],
                         "n_delays": int(p["points"]), "sweeps": n_sweeps, "frames": n,
                         "estimated_s": round(est, 1), "scheme": scheme, "readout": p["readout"]})
        if est > float(p["max_total_s"]):
            out.append(_tr_lang(f"预计 {est:.0f} s 超过 max_total_s={float(p['max_total_s']):.0f} s",
                                f"Estimated {est:.0f} s exceeds max_total_s={float(p['max_total_s']):.0f} s"))
        return out

    def preflight_extra(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        out: list[str] = []
        try:
            from mast.instruments.registry import get_instrument_registry

            dl = get_instrument_registry().delay_line()
            lo, hi = dl.delay_range_ps
        except Exception as exc:  # noqa: BLE001
            out.append(_tr_lang(f"延迟线不可用：{type(exc).__name__}: {exc}",
                                f"Delay line unavailable: {type(exc).__name__}: {exc}"))
            return out
        run.facts["delay_range_ps"] = [lo, hi]
        bad = [d for d in self._delays(run) if not (lo <= d <= hi)]
        if bad:
            out.append(_tr_lang(f"{len(bad)} 个延迟点（含 delay_offset_ps）超出可达范围 [{lo:.2f}, {hi:.2f}] ps",
                                f"{len(bad)} delay points (including delay_offset_ps) are outside the reachable "
                                f"range [{lo:.2f}, {hi:.2f}] ps"))
        self._lockin_index = None
        if p["read_lockin"] and p["readout"] != "spectrum":
            try:
                self._lockin_index = resolve_lockin_index(run.ctx, int(p["lockin_signal_index"]),
                                                          int(p["lockin_demod"]))
            except Exception as exc:  # noqa: BLE001
                out.append(_tr_lang(f"锁相信号槽位：{exc}", f"Lock-in signal slot: {exc}"))
        return out
        yield  # noqa: B901 — generator

    def attestation_lines(self, run: TersRun) -> list[str]:
        lines = [_tr_lang(f"延迟 {run.p['delay_start_ps']} → {run.p['delay_stop_ps']} ps，{run.p['points']} 点"
                          f"（偏移 {run.p['delay_offset_ps']} ps）；超快激光的波长 / 重频 / 平均功率请写进 laser_note",
                          f"Delay {run.p['delay_start_ps']} -> {run.p['delay_stop_ps']} ps, {run.p['points']} points "
                          f"(offset {run.p['delay_offset_ps']} ps); write the ultrafast laser's wavelength / "
                          f"repetition rate / average power into laser_note")]
        if run.p["trigger_scheme"] == "software_pulse" and run.p["readout"] != "electrical":
            lines.append(TTL_SAFE_NOTE)
        return lines

    def dry_plan(self, run: TersRun) -> None:
        run.plan["note"] = _tr_lang("dry_run：未移动延迟线、未触发、未读出",
                                    "dry_run: delay line not moved, nothing triggered, nothing read out")

    # -- run --
    def run_composite(self, context, params: dict) -> SkillResult:
        p = dict(params or {})
        if (p.get("readout") or "both") == "electrical":
            return self._electrical(context, p)
        return super().run_composite(context, params)

    def _electrical(self, context, params: dict) -> SkillResult:
        """readout=electrical：每个方向 / 重复一次 PumpProbeScan 子技能（契约：电读出已有，不重复实现）。"""
        run = TersRun(self, context, params)
        try:
            refusals = list(self.prepare(run) or [])
        except (ValueError, KeyError, TypeError) as exc:
            refusals = [f"{type(exc).__name__}: {exc}"]
        if refusals:
            run.stop("refused", _tr_lang("；", "; ").join(refusals))
            return run.result()
        p = run.p
        if p["dry_run"] and not p["dry_run_motion"]:
            run.status = "dry_run"
            run.plan["delegated_to"] = "PumpProbeScan"
            return run.result()
        subs = []
        off = float(p["delay_offset_ps"])
        for r, direction, _ in self._sweeps(run):
            if run.check():
                break
            a, b = float(p["delay_start_ps"]) + off, float(p["delay_stop_ps"]) + off
            if direction == "bwd":
                a, b = b, a
            sub = {k: p[k] for k in ("points", "samples_per_point", "sample_interval_s",
                                     "settle_extra_s", "read_current", "read_lockin",
                                     "lockin_signal_index", "lockin_demod", "return_to_start",
                                     "move_timeout_s", "extra_signal_indices", "secondary_device_id",
                                     "secondary_axis", "secondary_position", "secondary_scan",
                                     "secondary_start", "secondary_stop", "secondary_points")}
            sub.update({"delay_start_ps": a, "delay_stop_ps": b, "dry_run": bool(p["dry_run"]),
                        "tag": f"{run.tag}_r{r}{direction}".strip("_")})
            res = context.run("PumpProbeScan", sub)
            subs.append({"repeat": r, "direction": direction, "success": res.success,
                         "error": res.error, "path": (res.data or {}).get("path"),
                         "live_stream": (res.data or {}).get("live_stream")})
            if not res.success:
                run.stop("needs_operator" if not run._aborted() else "partial",
                         _tr_lang(f"PumpProbeScan（{direction} #{r}）失败：{res.error}",
                                  f"PumpProbeScan ({direction} #{r}) failed: {res.error}"))
                break
        if run.status == "running":
            run.status = "dry_run" if p["dry_run"] else "done"
        run.analysis["delegated_to"] = "PumpProbeScan"
        run.analysis["sweeps"] = subs
        run.total_points = len(subs)
        return run.result()

    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        run.live.open_main("xy", title=_tr_lang("泵浦–探测 · 谱读出", "Pump-probe - spectral readout"),
                           x_label="Delay", x_unit="ps", y_label="intensity", y_unit="counts")
        self._rows: list[dict] = []
        self._drop = ta.DropDetector(float(p["signal_drop_frac"]), int(p["drop_frames"]))
        extra_idx = parse_indices(p.get("extra_signal_indices") or "")
        if p["trigger_scheme"] == "software_pulse":
            yield from self._pulse_run(run, extra_idx)
        else:
            yield from self._acquire_run(run, extra_idx)
        # dry_run_motion moved the line too, so it goes back like a real run (plan-only never gets here)
        if p["return_to_start"] and not run._aborted():
            res = yield from run.step("return_to_start", "DelayLineMoveTo",
                                      {"delay_ps": float(p["delay_start_ps"]) + float(p["delay_offset_ps"]),
                                       "wait": True, "timeout_s": float(p["move_timeout_s"])})
            if not res.success:
                run.warnings.append(_tr_lang(f"回到起点失败：{res.error}",
                                             f"Return to start failed: {res.error}"))
        self._analyse(run)

    def _move(self, run: TersRun, k: int, delay: float) -> Iterator[CompositeStep]:
        p = run.p
        res = yield from run.step(f"d{k}:move", "DelayLineMoveTo",
                                  {"delay_ps": float(delay), "wait": True,
                                   "timeout_s": float(p["move_timeout_s"])})
        if not res.success:
            run.stop("needs_operator",
                     _tr_lang(f"延迟线移到 {delay:.4g} ps 失败：{res.error}"
                              "（延迟台 USB 掉线？）—— 已采点保留",
                              f"Delay line move to {delay:.4g} ps failed: {res.error} "
                              "(delay stage USB disconnected?) - points already acquired are kept"))
            return None
        if run.sleep(float(p["settle_extra_s"])):
            run.stop("partial", "aborted by operator")
            return None
        return (res.data or {}).get("delay_ps")

    def _stm_ok(self, run: TersRun, k: int) -> bool:
        p = run.p
        every = int(p["stm_check_every"])
        if p["tip_state"] != "tunneling" or every <= 0 or k % every:
            return True
        tr = run.tip_ready(label=f"d{k}", i_target=run.i_set, feedback=True, settle_s=0.0)
        if tr.passed:
            return True
        if not run.stopped:
            run.stop("needs_operator",
                     _tr_lang(f"延迟点 {k}: 判稳不过（{tr.reason}）—— 暂停",
                              f"Delay point {k}: stability check failed ({tr.reason}) - paused"))
        return False

    def _electrical_reads(self, run: TersRun, extra_idx: list[int]) -> dict:
        p = run.p
        cur, li = [], []
        extra: dict[int, list[float]] = {i: [] for i in extra_idx}
        for s in range(int(p["samples_per_point"])):
            if s and float(p["sample_interval_s"]) > 0:
                time.sleep(float(p["sample_interval_s"]))
            if p["read_current"]:
                v = run.read_float("Current_Get")
                if v is not None:
                    cur.append(v)
            if self._lockin_index is not None:
                v = run.read_float("Signals_ValGet", int(self._lockin_index), 0)
                if v is not None:
                    li.append(v)
            for i in extra_idx:
                v = run.read_float("Signals_ValGet", int(i), 0)
                if v is not None:
                    extra[i].append(v)
        out: dict = {}
        if cur:
            out["current_mean_a"], out["current_std_a"] = float(np.mean(cur)), float(np.std(cur))
        if li:
            out["lockin_v"], out["lockin_std"] = float(np.mean(li)), float(np.std(li))
        for i, vals in extra.items():
            if vals:
                out[f"sig{i}_mean"] = float(np.mean(vals))
        return out

    def _band_values(self, run: TersRun, spectrum: np.ndarray, axis_nm) -> dict:
        if axis_nm is None or not np.isfinite(np.asarray(axis_nm)).any():
            return {"total": ta.total_counts(spectrum)}
        x = ta.nm_to_shift_cm1(axis_nm, float(run.p["laser_line_nm"]))
        if not run.bands:
            return {"total": ta.total_counts(spectrum)}
        out = {}
        for b in run.bands:
            m = ta.band_metrics(x, spectrum, b, baselines=run.baselines or None)
            out[b.name] = m.get("area") if m.get("valid") else None
        return out

    def _publish(self, run: TersRun, delay: float, direction: str, vals: dict, elec: dict) -> None:
        suffix = "" if direction == "fwd" else "_bwd"
        live = {f"{k}{suffix}": v for k, v in vals.items() if v is not None}
        if "current_mean_a" in elec:
            live[f"I_A{suffix}"] = elec["current_mean_a"]
        if "lockin_v" in elec:
            live[f"lockin{suffix}"] = elec["lockin_v"]
        if live:
            run.live.publish({"x": float(delay), "values": live})
        first = next((v for v in vals.values() if v is not None), None)
        if self._drop.update(first) and not run.analysis.get("signal_drop"):
            run.analysis["signal_drop"] = {"at_point": len(self._rows), "delay_ps": delay}
            run.qc["signal_drop"] += 1
            if run.p["stop_on_drop"]:
                run.stop("needs_operator",
                         _tr_lang(f"信号衰减（≥ {run.p['signal_drop_frac']:.0%}，连续 "
                                  f"{run.p['drop_frames']} 点）—— stop_on_drop",
                                  f"Signal decay (>= {run.p['signal_drop_frac']:.0%}, "
                                  f"{run.p['drop_frames']} consecutive points) - stop_on_drop"))

    # S+TTL
    def _pulse_run(self, run: TersRun, extra_idx: list[int]) -> Iterator[CompositeStep]:
        p = run.p
        sweeps = self._sweeps(run)
        n_total = sum(len(d) for _, _, d in sweeps)
        if run.dry_run:
            yield from self._dry_motion(run, sweeps)
            return
        ok = yield from run.configure_spectrometer(
            trigger_response=str(p["trigger_response"]), trigger_edge=_EDGES[p["trigger_edge"]],
            frames=n_total, shutter_mode=str(p["shutter_mode"]))
        if not ok:
            return
        first = sweeps[0][2][0]
        rb0 = yield from self._move(run, -1, first)
        if rb0 is None:
            return
        res = yield from run.step("trigger:line", "ConfigureDigitalLine",
                                  {"line": int(p["trigger_line"]), "port": int(p["trigger_port"]),
                                   "direction": 1, "polarity": 1})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"触发线配置失败：{res.error}",
                                                f"Trigger line configuration failed: {res.error}"))
            return
        exp_s = float(p["exposure_ms"]) / 1000.0
        period = exp_s + PYLON_READOUT_S + float(p["pixel_margin_s"])
        judge = (float(p["stable_timeout_s"]) if p["tip_state"] == "tunneling"
                 and int(p["stm_check_every"]) > 0 else 0.0)
        # worst case between two pulses: a move that uses its whole timeout, a judging that times out
        per_trigger = float(p["move_timeout_s"]) + float(p["settle_extra_s"]) + judge + period + 2.0
        acq = TriggeredAcquisition(run, n_frames=n_total, exposure_ms=float(p["exposure_ms"]),
                                   label="pp", per_trigger_s=per_trigger,
                                   extra={"delays_ps": self._delays(run)})
        err = acq.start()
        if err:
            run.stop("needs_operator", err)
            return
        self._acq = acq
        try:
            if not acq.wait_running(5.0):
                run.stop("needs_operator",
                         _tr_lang(f"外触发采集没进入等待触发：{acq.error or '超时'}",
                                  f"The externally triggered acquisition did not reach the wait-for-trigger "
                                  f"state: {acq.error or 'timeout'}"))
                return
            k = 0
            for rep, direction, delays in sweeps:
                for j, d in enumerate(delays):
                    if run.check():
                        break
                    readback = rb0 if k == 0 else (yield from self._move(run, k, d))
                    if readback is None:
                        break
                    if not self._stm_ok(run, k):
                        break
                    t_trig = time.time()
                    perr = run.nanonis_write("DigLines_Pulse", int(p["trigger_port"]),
                                             [int(p["trigger_line"])], float(p["trigger_width_s"]),
                                             0.0, 1, 1)
                    acq.triggers.append({"index": k, "t": t_trig, "delay_ps": d,
                                         "delay_readback_ps": readback, "sweep": rep,
                                         "direction": direction, "error": perr})
                    if perr:
                        run.stop("needs_operator", _tr_lang(f"触发脉冲失败：{perr}",
                                                            f"Trigger pulse failed: {perr}"))
                        break
                    with run.interlock.window(f"pp{k}"):
                        elec = (self._electrical_reads(run, extra_idx)
                                if p["readout"] == "both" else {})
                        left = period - (time.time() - t_trig)
                        if left > 0 and run.sleep(left):
                            run.stop("partial", "aborted by operator")
                            break
                        got = acq.wait_frame(k, 2.0 + exp_s)
                    if not got:
                        self._mismatch(run, k)
                        break
                    spec = acq.frames[k]
                    vals = self._band_values(run, spec, acq.axis_nm)
                    row = {"key": f"d{k}", "status": "ok", "delay_ps": d,
                           "delay_readback_ps": readback, "sweep": rep, "direction": direction,
                           "frame_index": k, "trigger_t": t_trig, **elec,
                           **{f"band_{n}": v for n, v in vals.items()}}
                    self._rows.append(row)
                    run.spectra.append((f"d{k}", np.asarray(spec, dtype=np.float32)))
                    if run.axis_nm is None and acq.axis_nm is not None:
                        run.axis_nm = np.array(acq.axis_nm)
                    run.record(None, key=f"d{k}", status="ok", values=row)
                    self._publish(run, d, direction, vals, elec)
                    k += 1
                if run.stopped:
                    break
        finally:
            if run._aborted() or run.stopped or len(acq.triggers) < acq.n:
                acq.cancel()
            acq.finish(timeout_s=10.0 + period * 2)
            rec = acq.reconcile()
            run.analysis["reconciliation"] = rec
            if rec["status"] != "ok" and not run.stopped:
                self._mismatch(run, rec.get("first_invalid_index") or 0)
            stored = acq.write_files(rec) if acq.frames else {}
            run.facts["triggered_files"] = stored
            paths = stored.get("paths") or []
            for row in run.points:
                row["file"] = paths[0] if paths else ""
                row["sidecar"] = paths[1] if len(paths) > 1 else ""
                row["sha256"] = stored.get("sha256", "")

    def _mismatch(self, run: TersRun, k: int) -> None:
        mode = run.p["on_count_mismatch"]
        run.qc["count_mismatch"] += 1
        if mode == "abort":
            for row in run.points:
                row["status"] = "invalid"
                row["qc_flags"] = "count_mismatch"
        else:
            for row in run.points[k:]:
                row["status"] = "invalid"
                row["qc_flags"] = "count_mismatch"
        run.stop("needs_operator",
                 _tr_lang(f"count_mismatch：第 {k} 个触发之后帧与触发对不上"
                          f"（on_count_mismatch={mode}）—— 永远不重放触发、不修补数据",
                          f"count_mismatch: frames no longer match triggers after trigger #{k} "
                          f"(on_count_mismatch={mode}) - triggers are never replayed and data is never patched"))

    def _dry_motion(self, run: TersRun, sweeps) -> Iterator[CompositeStep]:
        """dry_run_motion 的 S+TTL：每个延迟点照样移台、照样判稳（同真跑的 stm_check_every）；数字线不配置、
        不打脉冲、不采集。"""
        k = 0
        for rep, direction, delays in sweeps:
            for d in delays:
                if run.check():
                    return
                rb = yield from self._move(run, k, d)
                if rb is None:
                    return
                if not self._stm_ok(run, k):
                    return
                self._rows.append({"key": f"d{k}", "status": "dry_run", "delay_ps": d,
                                   "delay_readback_ps": rb, "sweep": rep, "direction": direction})
                k += 1
        run.plan["reached_delays_ps"] = [r["delay_readback_ps"] for r in self._rows][:200]

    # S
    def _acquire_run(self, run: TersRun, extra_idx: list[int]) -> Iterator[CompositeStep]:
        p = run.p
        k = 0
        frac = float(p["exposure_guard_current_frac"])
        guard = (GuardSpec(i_ref_a=run.i_set, frac=frac, expect_feedback=True)
                 if p["tip_state"] == "tunneling" else GuardSpec(expect_feedback=None))
        for rep, direction, delays in self._sweeps(run):
            for d in delays:
                if run.check():
                    return
                rb = yield from self._move(run, k, d)
                if rb is None:
                    return
                if not self._stm_ok(run, k):
                    return
                key = f"d{k}"
                out = yield from run.acquire(key, key, guard=guard,
                                             extra={"delay_ps": d, "delay_readback_ps": rb,
                                                    "sweep": rep, "direction": direction})
                if not out.ok:
                    run.record(out, key=key, values={"delay_ps": d})
                    if out.status == "dry_run":
                        k += 1
                        continue
                    run.handle(out)
                    return
                vals = {name: (m.get("area") if m.get("valid") else None)
                        for name, m in out.bands.items()} or {"total": out.intensity}
                elec = {}
                if p["readout"] == "both" and out.window.get("n"):
                    elec = {"current_mean_a": out.window.get("current_mean_a"),
                            "current_std_a": out.window.get("current_std_a")}
                row = {"delay_ps": d, "delay_readback_ps": rb, "sweep": rep,
                       "direction": direction, **elec}
                self._rows.append({"key": key, **row, **{f"band_{n}": v for n, v in vals.items()}})
                run.record(out, key=key, values=row)
                self._publish(run, d, direction, vals, elec)
                k += 1

    # analysis
    def _analyse(self, run: TersRun) -> None:
        ok = [r for r in self._rows if r.get("status", "ok") == "ok"]
        if not ok:
            return
        bands = [k for k in ok[0] if k.startswith("band_")]
        curves: dict = {}
        for b in bands:
            for direction in ("fwd", "bwd"):
                pts = [(r["delay_ps"], r[b]) for r in ok if r.get("direction") == direction
                       and r.get(b) is not None]
                if not pts:
                    continue
                pts.sort()
                ref = pts[0][1]
                curves[f"{b}_{direction}"] = {"delay_ps": [x for x, _ in pts],
                                              "S": [y for _, y in pts],
                                              "dS": [y - ref for _, y in pts],
                                              "reference_delay_ps": pts[0][0]}
        run.analysis["curves"] = {k: {kk: (vv[:400] if isinstance(vv, list) else vv)
                                      for kk, vv in v.items()} for k, v in curves.items()}

    def product_arrays(self, run: TersRun) -> dict:
        rows = getattr(self, "_rows", [])
        if not rows:
            return {}

        def col(name, default=math.nan):
            return np.asarray([r.get(name, default) if r.get(name) is not None else default
                               for r in rows], dtype=np.float64)
        return {"delay_ps": col("delay_ps"), "delay_readback_ps": col("delay_readback_ps"),
                "current_mean_a": col("current_mean_a"), "lockin_v": col("lockin_v"),
                "sweep": col("sweep"), "frame_index": col("frame_index"),
                "direction": np.asarray([str(r.get("direction", "")) for r in rows])}
