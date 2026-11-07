"""TERS 光学–STM 联动模式的共用骨架。

规格：TERS 模式规格 ``docs/v2/design/ters_modes_spec.md`` §1.3–§1.10（下文「规格」）；
契约：``docs/v2/design/ters_satellite_integration.md`` §8.3、§10、§11（下文「契约」）。

每个模式（``skills/composite/ters_*.py``）只写自己的序列，下面这些只实现一次：

* **共用参数组** CP-A（光谱采集）/ CP-B（判稳）/ CP-C（激光确认与运行记录）：:func:`cp_a` …
  带 INF 标签的默认值保持可改，描述里标「默认值可调」；评审回来的 ``param_corrections`` 以修正值为准。
* **前置检查**（§1.3）：:meth:`TersRun.preflight` —— 修针/进针在跑、端口归属冲突
  （``core/port_guard``）、卫星链路与 boot_id、CCD 温度锁定、Nanonis 状态、SafeTip、锁相调制、
  电流稳定 ≥ 5 s。任一不过 ⇒ 拒绝开始，什么都不动。
* **到位判稳**（§1.4）：:meth:`TersRun.tip_ready` —— 固定等待 → 滑动窗口（≥ 50 Hz 采 I 与 Z）
  → |⟨I⟩−I_set|、σ_I、|dz/dt|、反馈状态、越限尖峰同时满足才放行；超时 = ``unstable_tip``。
* **守卫式采集**（§1.5、§1.6）：:meth:`TersRun.acquire` —— 曝光窗口由 :class:`ExposureInterlock`
  建模：**窗口打开期间本组合技能只放行只读子技能与 Stop/退针**，其余一律拒绝（这就是「曝光期间
  禁止清单」作为持令牌期间的不变量）；同时 :class:`ExposureGuard` 以 20 Hz 读电流与 Z，
  |I| > guard × I_set 持续 > 50 ms 或反馈掉线 ⇒ 直接停光谱仪（免令牌的停止路径）、该帧作废、
  运行停在 ``needs_operator``；卫星上的人工操作（``manual_activity``）、卫星重启 / 失联也在窗口内监听。
* **每条谱元数据**（§1.7）：曝光前后的 STM 状态、曝光期间的电流统计、时钟偏差 / RTT、光谱仪
  设置、光学轴位置由 AcquireSpectrum 写进它的 sidecar；组合技能自己的标识（run_id、模式、点号、
  像素号、判稳结果、激光确认、条件）经 ``meta_json`` 写进同一个 sidecar 的 ``extra``；事后才有的
  质检标记与谱带强度写进运行级的 index.csv / summary.json。
* **LiveHub**（§1.8）：``local/<模式>/<run_id>`` 构建中的图 / 曲线 + ``…/status`` 进度流（契约 §7：
  进包代码不写站点别名，本机流一律 ``local/``）；最新谱由 AcquireSpectrum（``local/spectra/<设备>``）
  与枢纽的 LiveBridge（``<卫星>/<设备>/spectrum``）发。
* **失败与中断**（§1.9）：点与点之间、所有等待循环里查 abort；中止之后**不做还原**（契约 §10.3），
  只用免令牌的停止路径停光谱仪与光学台，结果里如实写「未还原」和原值；每点写检查点
  （``<数据根>/artifacts/ters/<run_id>/checkpoint.json``），``resume_run_id`` 从第一个未完成项续跑。

子步骤一律走 ``ctx.run``（经 :class:`GraphExecutor`，每步 ``optional``：停不停由模式决定）；
直接的 ``ctx.safe_call`` 只用于**读**（判稳、守卫）、S+TTL 的触发脉冲，以及中止之后的停止 / 退针动词，
动词一律是字面量（:data:`_READS` / :data:`_WRITES`）。

**dry_run 的两档**（与 PumpProbeScan 的 dry_run 不同，见 :func:`cp_c` 与 :func:`common_extra`）：
``dry_run=true`` 只做只读判据，并按限位 / 软限位 / 时间预算把全部目标点规划一遍，**不动任何硬件**；
``dry_run_motion=true`` 才真走运动（仍不曝光、不触发、不写数据），安全等级、联锁、判稳与守卫和真跑完全一样。
PumpProbeScan 自己的 ``dry_run`` 会真的把延迟线走一遍，相当于这里的 ``dry_run_motion``。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import shutil
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

from mast.i18n import tr as _tr_lang
from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.builtins.optics_common import (
    LiveStream,
    atomic_write_bytes,
    dump_json_bytes,
    json_safe,
    new_run_id,
    scope_ids,
    store_in_experiment,
    tip_work_refusal,
)
from mast.skills.composite._base import CompositeSkillGraph
from mast.skills.composite import ters_analysis as ta
from mast.skills.composite.graph_executor import CompositeStep, GraphExecutor

logger = logging.getLogger(__name__)

__all__ = [
    "cp_a", "cp_b", "cp_c", "common_extra", "with_defaults",
    "Needs", "ExposureInterlock", "InterlockViolation", "TipReady", "judge_window",
    "GuardSpec", "ExposureGuard", "AcqOutcome", "RunLive", "RunStore", "TersRun",
    "TersComposite", "TriggeredAcquisition", "SPECTROMETER_ROLE", "TTL_SAFE_NOTE",
    "human_channel", "ters_metadata",
]

#: Registry role of the Raman camera (契约 §5.5，与枢纽 satellites.json 一致)。
SPECTROMETER_ROLE = "raman_ccd"

#: 预飞时「电流在 setpoint ± tol 内连续 ≥ 5 s」的窗口（§1.3-5）。测试会改小。
PREFLIGHT_STABLE_S = 5.0
#: 预飞窗口里落在容差带内的样本比例下限（实现选择：逐点全部落带在噪声下几乎不可能）。
PREFLIGHT_IN_BAND_FRAC = 0.9
#: 判稳窗口的目标采样率（§1.4：≥ 50 Hz）。实际达到的速率写进元数据。
TIP_READY_HZ = 50.0
#: 曝光守卫的采样率与「持续 > 50 ms」（CP-B ``exposure_guard_current_frac`` 说明）。
GUARD_HZ = 20.0
GUARD_HOLD_S = 0.05
#: 卫星链路判据（§1.3-1）。
MAX_RTT_MS = 5.0
MAX_CLOCK_OFFSET_MS = 50.0
#: 运行目录至少要这么多空闲空间（§1.3-10，实现选择）。
MIN_FREE_BYTES = 200 * 1024 * 1024
#: 放进 SkillResult.data 的逐点摘要上限（结果会进 agent 的消息与检查点，必须有界）。
MAX_POINTS_IN_RESULT = 60

TTL_SAFE_NOTE = (_tr_lang("延迟台的 Trigger In 不得接线；"
                 "Nanonis DIO 输出电平与 PyLoN TRIGGER IN（3.3 V 逻辑）是否匹配要先核对",
                          "The delay stage Trigger In must not be wired; check "
                          "first that the Nanonis DIO output level matches PyLoN TRIGGER IN (3.3 V logic)"))

#: 本模块自己发出的全部 Nanonis 动词，每个都以字面量写在自己的 ``safe_call`` 里：中止策略检查、
#: 安全审计与 API 普查都靠 grep ``safe_call("…")`` 找动词，藏在变量里的动词它们一个都看不见。
#: 封闭清单：不在表里的动词不发。读与写分开，读辅助函数发不出写。
_READS: dict[str, Callable[..., Any]] = {
    "Bias_Get": lambda c, *a: c.safe_call("Bias_Get", *a),
    "Current_Get": lambda c, *a: c.safe_call("Current_Get", *a),
    "FolMe_XYPosGet": lambda c, *a: c.safe_call("FolMe_XYPosGet", *a),
    "LockIn_ModOnOffGet": lambda c, *a: c.safe_call("LockIn_ModOnOffGet", *a),
    "SafeTip_OnOffGet": lambda c, *a: c.safe_call("SafeTip_OnOffGet", *a),
    "Scan_StatusGet": lambda c, *a: c.safe_call("Scan_StatusGet", *a),
    "Signals_ValGet": lambda c, *a: c.safe_call("Signals_ValGet", *a),
    "ZCtrl_OnOffGet": lambda c, *a: c.safe_call("ZCtrl_OnOffGet", *a),
    "ZCtrl_SetpntGet": lambda c, *a: c.safe_call("ZCtrl_SetpntGet", *a),
    "ZCtrl_TipLiftGet": lambda c, *a: c.safe_call("ZCtrl_TipLiftGet", *a),
    "ZCtrl_ZPosGet": lambda c, *a: c.safe_call("ZCtrl_ZPosGet", *a),
}
_WRITES: dict[str, Callable[..., Any]] = {
    "DigLines_Pulse": lambda c, *a: c.safe_call("DigLines_Pulse", *a),
}

#: DigLines 的 Port 参数：0 = Port A … 3 = Port D。
PORT_A = 0


def ttl_refusals(p: dict, *, dry_run: bool) -> list[str]:
    """S+TTL 触发线的约束（M03 与 M10 的 software_pulse 共用），返回拒绝原因。

    * **Port A 一律拒绝**，dry_run 也一样（参数本身错了，不是演练能放过的）：它接着 PMD4 粗动控制器，
      Nanonis 帮助明确警告不要把（扫描像素）脉冲配到接了压电马达驱动器的线上。本机配置登记的保留数字口
      同样拒绝（``optics_acquire.trigger_port_refusal``）。
    * **线号起点在真机上核实之前只允许 dry_run**：DigLines 的线号从几数起、相机的 B2 对应哪个编号，配错
      一条线可能让接在别处的设备误动作（``trigger_line_verified``）。
    * **电平与触发沿核对之前同样只允许 dry_run**（``trigger_level_verified``）。"""
    from mast.skills.builtins.optics_acquire import trigger_port_refusal

    out: list[str] = []
    port = int(p.get("trigger_port") if p.get("trigger_port") is not None else -1)
    if port == PORT_A:
        out.append(_tr_lang("trigger_port=0（Port A）一律拒绝：Port A 接着 PMD4 粗动控制器，Nanonis 帮助明确警告不要把"
                   "脉冲配到接了压电马达驱动器的线上 —— 只能用空闲的数字线或 HS Out 3",
                            "trigger_port=0 (Port A) is always refused: Port A is connected to the PMD4 "
                            "coarse-motion controller, and the Nanonis help explicitly warns against routing pulses "
                            "to lines connected to a piezo motor driver - use a free digital line "
                            "or HS Out 3"))
    else:
        r = trigger_port_refusal(port)
        if r:
            out.append(r)
    if not dry_run:
        if not p.get("trigger_line_verified"):
            out.append(_tr_lang("trigger_line_verified=false：DigLines 的线号起点（从 1 还是从 0 数、相机触发线对应哪个编号）"
                       "还没在真机上核实 —— 在此之前本模式只允许 dry_run",
                                "trigger_line_verified=false: the DigLines line-number origin (counting from 1 or 0, "
                                "which number the camera trigger line maps to) has not been verified on the real instrument - "
                                "until then this mode is dry_run only"))
        if not p.get("trigger_level_verified"):
            out.append(_tr_lang("trigger_level_verified=false：Nanonis DIO 电平与 PyLoN TRIGGER IN（3.3 V 逻辑）的匹配、"
                       "触发沿还没核对—— 在此之前本模式只允许 dry_run",
                                "trigger_level_verified=false: the match between the Nanonis DIO level and PyLoN TRIGGER "
                                "IN (3.3 V logic) and the trigger edge have not been checked  - until "
                                "then this mode is dry_run only"))
    return out


def ttl_params() -> list[ParameterSpec]:
    """S+TTL 模式共用的两个操作员核实开关（默认 false = 只允许 dry_run）。"""
    return [
        _spec("trigger_line_verified", "bool", _tr_lang("操作员已在真机上核实 DigLines 的线号起点（从 1 还是从 0 数、相机"
              "触发线对应哪个编号）。未核实时本模式只允许 dry_run。",
                                                        "The operator has verified on the real instrument the DigLines line-number origin "
                                                        "(counting from 1 or 0, which number the camera trigger line maps to). Until verified, "
                                                        "this mode is dry_run only."), default=False),
        _spec("trigger_level_verified", "bool", _tr_lang("操作员已核对 Nanonis DIO 电平与 PyLoN TRIGGER IN（3.3 V 逻辑）"
              "的匹配及触发沿。未核对时本模式只允许 dry_run。",
                                                         "The operator has checked that the Nanonis DIO level matches PyLoN TRIGGER IN (3.3 V "
                                                         "logic) and the trigger edge . Until checked, this mode is dry_run "
                                                         "only."), default=False),
    ]


def tip_lift_refusal(run: "TersRun") -> str:
    """hold_mode=hold 的判据：关反馈后 Z 要停在判稳时的位置，tip lift 必须是 0（读不到同样拒绝）。
    前置检查（dry_run 也查）与每次关反馈之前各查一次。"""
    lift = run.read_float("ZCtrl_TipLiftGet")
    if lift is None or abs(lift) > 1e-12:
        return _tr_lang(f"hold_mode=hold 要求 tip lift = 0，读回 {lift!r}",
                        f"hold_mode=hold requires tip lift = 0, read back {lift!r}")
    return ""


def _human_channel_default(payload: dict):
    from mast.skills.composite.interpreter import human_channel as _hc

    return _hc(payload)


#: 操作员确认的通道（agent 路径 = LangGraph interrupt；图运行时之外返回 None）。测试替换它。
human_channel: Callable[[dict], Any] = _human_channel_default


# ═════════════════════════════════════════════════════════════════════════════
# 共用参数组（规格 §1.10）
# ═════════════════════════════════════════════════════════════════════════════

_INF = _tr_lang("【占位默认值，可调】",
                " [placeholder default, adjustable]")


def _spec(name, type_, desc, *, unit="", default=None, lo=None, hi=None, allowed=None,
          required=False) -> ParameterSpec:
    return ParameterSpec(name=name, type=type_, description=desc, unit=unit,
                         required=required, default=default, min_value=lo, max_value=hi,
                         allowed_values=allowed)


def _apply(specs: list[ParameterSpec], overrides: dict | None, omit: set | None,
           notes: dict | None = None) -> list[ParameterSpec]:
    out = []
    for s in specs:
        if omit and s.name in omit:
            continue
        if overrides and s.name in overrides:
            s = ParameterSpec(name=s.name, type=s.type, unit=s.unit, required=s.required,
                              default=overrides[s.name], min_value=s.min_value,
                              max_value=s.max_value, allowed_values=s.allowed_values,
                              description=s.description + _tr_lang(f"（本模式默认 {overrides[s.name]}）",
                                                                   f" (default for this mode: {overrides[s.name]})"))
        if notes and s.name in notes:
            s = ParameterSpec(name=s.name, type=s.type, unit=s.unit, required=s.required,
                              default=s.default, min_value=s.min_value, max_value=s.max_value,
                              allowed_values=s.allowed_values,
                              description=s.description + notes[s.name])
        out.append(s)
    return out


def cp_a(overrides: dict | None = None, omit: set | None = None,
         notes: dict | None = None) -> list[ParameterSpec]:
    """CP-A 光谱采集（经卫星 ccd 设备或本机模拟光谱仪）。"""
    return _apply([
        _spec("exposure_ms", "float", _tr_lang("单帧曝光。日常手动谱 1000 ms；PP_ 系列 1–100 ms；单分子 TERS 常见 1–30 s。",
                                               "Single-frame exposure. Routine manual spectra 1000 ms; PP_ series 1-100 ms; "
                                               "single-molecule TERS typically 1-30 s.")
              + _INF, unit="ms", default=2000.0, lo=1.0, hi=600000.0),
        _spec("frames", "int", _tr_lang("每点帧数。每帧单独保存，不在卫星端平均；frames ≥ 3 且 "
              "cosmic_filter=median_frames 时逐像素取中值。",
                                        "Frames per point. Each frame is saved separately, not averaged on the satellite; with "
                                        "frames >= 3 and cosmic_filter=median_frames the per-pixel median is taken.") + _INF, default=1, lo=1, hi=1000),
        _spec("grating", "str", _tr_lang("光栅 g/mm。只在运行开始前设置（换光栅 10–30 s）。设备上的全名"
              "（如 '[500nm,1200][0][0]'）按刻线数匹配。",
                                         "Grating g/mm. Set only before the run starts (a grating change takes 10-30 s). The full "
                                         "device name (e.g. '[500nm,1200][0][0]') is matched by groove density."), default="1200",
              allowed=["1200", "600", "150"]),
        _spec("center_shift_cm1", "float", _tr_lang("窗口中心的拉曼位移。750 cm⁻¹ ↔ 664.55 nm（激发线 633 nm）；"
              "1200 g/mm 时覆盖 ≈ ±650 cm⁻¹。",
                                                    "Raman shift of the window center. 750 cm^-1 <-> 664.55 nm (633 nm excitation); at 1200 "
                                                    "g/mm the coverage is about +-650 cm^-1."), unit="cm^-1", default=750.0, lo=0.0, hi=4000.0),
        _spec("laser_line_nm", "float", _tr_lang("波长→位移换算用的激发线。LightField 里若设 633；He-Ne 实为 632.8，"
              "差 0.2 nm ≈ 5 cm⁻¹。",
                                                 "Excitation line used for the wavelength-to-shift conversion. If LightField is "
                                                 "set to 633, note that He-Ne is actually 632.8, a 0.2 nm difference is about 5 cm^-1."), unit="nm", default=633.0, lo=500.0, hi=1100.0),
        _spec("roi_rows", "int", _tr_lang("垂直 ROI 行数（LineSensor，竖直 bin 成 1 行）；起始行由 LightField "
              "实验文件保存，MAST 不改。",
                                          "Number of vertical ROI rows (LineSensor, binned vertically into 1 row); the start row "
                                          "is saved in the LightField experiment file, MAST does not change it."), unit=_tr_lang("行",
                                                                   "rows"), default=50, lo=1, hi=100),
        _spec("adc_mhz", "float", _tr_lang("ADC 速率。4 MHz 读出更快但噪声更高。",
                                           "ADC rate. 4 MHz reads out faster but is noisier."), unit="MHz", default=1.0,
              lo=1.0, hi=4.0, allowed=[1.0, 4.0]),
        _spec("adc_quality", "str", _tr_lang("ADC 质量档。",
                                             "ADC quality setting."), default="LowNoise",
              allowed=["LowNoise", "HighCapacity"]),
        _spec("shutter_mode", "str", _tr_lang("Normal：每帧开关 Prontor 快门（寿命有限）；触发 / 时间序列用 "
              "AlwaysOpen；暗帧用 AlwaysClosed。快门只挡进光谱仪的光，**不挡照到样品上的激光**。",
                                              "Normal: the Prontor shutter opens and closes every frame (limited lifetime); use "
                                              "AlwaysOpen for triggered / time-series runs; use AlwaysClosed for dark frames. The "
                                              "shutter only blocks light entering the spectrometer, **not the laser hitting the "
                                              "sample**."),
              default="Normal", allowed=["Normal", "AlwaysOpen", "AlwaysClosed", "OpenBeforeTrigger"]),
        _spec("lfe_preset", "str", _tr_lang("要 Load 的 LightField 实验文件名；空 = 保持当前。卫星协议 v1 没有"
              "「加载 .lfe」的动作，非空时拒绝（不是参数问题）。",
                                            "Name of the LightField experiment file to Load; empty = keep the current one. Satellite "
                                            "protocol v1 has no \"load .lfe\" action, so a non-empty value is refused (not a "
                                            "parameter problem).") + _INF, default=""),
        _spec("save_spe", "bool", _tr_lang("让 LightField 同时存 SPE 原始文件（之后由 spe_watcher 传回枢纽）。",
                                           "Have LightField also save a raw SPE file (later sent back to the hub by spe_watcher)."),
              default=True),
        _spec("dark_every_n", "int", _tr_lang("每 n 个采集点插入一帧暗帧（AlwaysClosed）；0 = 仅开始时一次"
              "（本会话已有同参数暗帧则不重拍）。",
                                              "Insert one dark frame (AlwaysClosed) every n acquisition points; 0 = once at the start "
                                              "only (not retaken if this session already has a dark frame with the same parameters).") + _INF, default=0, lo=0, hi=10000),
        _spec("max_saturation_frac", "float", _tr_lang("帧内最大值超过满量程（65535 计数）的该比例 ⇒ 标记 "
              "saturated，不自动改曝光。",
                                                       "If the in-frame maximum exceeds this fraction of full scale (65535 counts) => flag "
                                                       "saturated; exposure is not changed automatically.") + _INF, default=0.9, lo=0.1, hi=1.0),
        _spec("cosmic_filter", "str", _tr_lang("宇宙射线：flag = 只标记尖峰；median_frames = 多帧逐像素取中值；"
              "off = 不处理。",
                                               "Cosmic rays: flag = only flag spikes; median_frames = per-pixel median over several "
                                               "frames; off = no processing.") + _INF, default="flag", allowed=["off", "flag", "median_frames"]),
    ], overrides, omit, notes)


def cp_b(overrides: dict | None = None, omit: set | None = None,
         notes: dict | None = None) -> list[ParameterSpec]:
    """CP-B 针尖就位与稳定判据。"""
    return _apply([
        _spec("settle_s", "float", _tr_lang("到位后固定等待（压电蠕变、反馈整定）。",
                                            "Fixed wait after arriving at the point (piezo creep, feedback settling).") + _INF, unit="s",
              default=1.0, lo=0.0, hi=600.0),
        _spec("stable_window_s", "float", _tr_lang("判稳窗口长度（窗口内以 ≥ 50 Hz 采电流与 Z）。",
                                                   "Length of the stability window (current and Z sampled at >= 50 Hz within the window).") + _INF,
              unit="s", default=1.0, lo=0.1, hi=30.0),
        _spec("stable_tol_frac", "float", _tr_lang("窗口内 σ_I/|⟨I⟩| 与 |⟨I⟩ − I_set|/|I_set| 的上限。",
                                                   "Upper limits of sigma_I/|<I>| and |<I> - I_set|/|I_set| within the window.") + _INF,
              default=0.2, lo=0.01, hi=1.0),
        _spec("stable_dz_pm_per_s", "float", _tr_lang("窗口内 |dz/dt| 上限（含激光热漂移）。",
                                                      "Upper limit of |dz/dt| within the window (includes laser thermal drift).") + _INF,
              unit="pm/s", default=10.0, lo=0.1, hi=1000.0),
        _spec("stable_timeout_s", "float", _tr_lang("判稳超时 ⇒ unstable_tip，不曝光。",
                                                    "Stability timeout => unstable_tip, no exposure.") + _INF, unit="s",
              default=30.0, lo=1.0, hi=600.0),
        _spec("exposure_guard_current_frac", "float", _tr_lang("曝光期间 |I| > 该倍数 × I_set 持续 > 50 ms ⇒ "
              "取消采集并标 guard_abort（判稳窗口里出现这么大的尖峰也判不稳）。",
                                                               "During exposure, |I| > this multiple x I_set for > 50 ms => cancel the acquisition and "
                                                               "flag guard_abort (a spike this large inside the stability window also counts as "
                                                               "unstable).") + _INF,
              default=3.0, lo=1.2, hi=100.0),
        _spec("on_unstable", "str", _tr_lang("判稳失败的处理：skip = 记空点继续；retry = 重判最多 retry_max 次；"
              "abort = 停下等操作员。",
                                             "What to do when stability fails: skip = record an empty point and continue; retry = "
                                             "re-check up to retry_max times; abort = stop and wait for the operator.") + _INF, default="skip", allowed=["skip", "retry", "abort"]),
        _spec("retry_max", "int", _tr_lang("on_unstable = retry 时的最大重试次数。",
                                           "Maximum number of retries when on_unstable = retry.") + _INF, default=2,
              lo=0, hi=10),
    ], overrides, omit, notes)


def cp_c(overrides: dict | None = None, omit: set | None = None,
         notes: dict | None = None) -> list[ParameterSpec]:
    """CP-C 激光确认与运行记录。"""
    return _apply([
        _spec("laser_attest", "bool", _tr_lang("开始前要求操作员确认激光状态与样品处功率（agent 路径弹人工确认；"
              "直调路径须显式给 laser_state）；dry_run 不要求。false = 不确认，元数据记 laser_state=unknown。",
                                               "Before starting, ask the operator to confirm the laser state and the power at the "
                                               "sample (agent path pops up a manual confirmation; the direct-call path must give "
                                               "laser_state explicitly); not required for dry_run. false = no confirmation, metadata "
                                               "records laser_state=unknown.")
              + _INF, default=True),
        _spec("laser_power_uW", "float", _tr_lang("操作员估计的样品处功率（没有功率计，只记录）；不给 = 未知。",
                                                  "Operator's estimate of the power at the sample (no power meter, record only); not given "
                                                  "= unknown.")
              + _INF, unit="µW", default=None, lo=0.0, hi=1_000_000.0),
        _spec("laser_note", "str", _tr_lang("自由文本：波长 / ND / 光斑位置 / 超快激光状态。",
                                            "Free text: wavelength / ND / spot position / ultrafast laser state."), default=""),
        _spec("tag", "str", _tr_lang("附加在文件名与 run 标识上的标签。",
                                     "Label appended to file names and the run identifier."), default=""),
        _spec("dry_run", "bool", _tr_lang("试跑：只做只读判据，并按限位 / 软限位 / 时间预算把全部目标点规划一遍，"
              "不动任何硬件、不曝光、不触发、不写数据；要真走运动再加 dry_run_motion=true。"
              "注意与 PumpProbeScan 的 dry_run 不同：那个会真的把延迟线走一遍（相当于这里的 dry_run_motion）。",
                                          "Trial run: only read-only checks, and plan all target points against limits / soft "
                                          "limits / the time budget; no hardware moves, no exposure, no trigger, no data written; "
                                          "add dry_run_motion=true to actually execute the motion. Note this differs from "
                                          "PumpProbeScan's dry_run: that one really runs the delay line (equivalent to "
                                          "dry_run_motion here)."),
              default=False),
        _spec("resume_run_id", "str", _tr_lang("续跑：从该运行的检查点接着做（先校验针尖 / 坐标代次 / 光谱仪设置，"
              "并先取一条参考谱）。",
                                               "Resume: continue from that run's checkpoint (first checks tip / coordinate epoch / "
                                               "spectrometer settings, and first takes a reference spectrum)."), default=""),
        _spec("max_total_s", "float", _tr_lang("整次运行的时间上限，超过则停在当前点并保存。",
                                               "Time limit for the whole run; when exceeded, stop at the current point and save.") + _INF, unit="s",
              default=14400.0, lo=60.0, hi=86400.0),
    ], overrides, omit, notes)


def common_extra() -> list[ParameterSpec]:
    """规格表之外、实现需要的三个参数（偏差，报告里列明）。"""
    return [
        _spec("device_id", "str", _tr_lang("光谱仪设备 id（ListOpticalDevices）；留空 = 唯一一台 role=raman_ccd 的。",
                                           "Spectrometer device id (ListOpticalDevices); empty = the only device with "
                                           "role=raman_ccd."),
              default=""),
        _spec("dry_run_motion", "bool", _tr_lang("与 dry_run 同用：试跑时**真的走一遍曝光 / 触发 / 存数据以外的"
              "全部动作**（MoveToXY、Z 斜坡、撤针 / 回针、hold 的关 / 开反馈、偏压 / 设定点序列、偏振、延迟线、"
              "光学台），每个点照样判稳（规格 §1.3-11 的「走完全部运动与判据」）。安全等级、联锁、判稳与守卫"
              "（Z 斜坡过流、撤针 / 回针检查）和真跑完全一样，不因为是演练而放宽；仍不曝光、不触发（数字线也不配置）、"
              "不写数据。收尾也和真跑一样（偏压 / 设定点照常还原、延迟线按 return_to_start）；LaserFocusMap 演练结束"
              "把光学轴送回起点。",
                                                 "Use together with dry_run: the trial run **really performs all actions except exposure "
                                                 "/ trigger / data saving** (MoveToXY, Z ramp, withdraw / re-approach, hold feedback off "
                                                 "/ on, bias / setpoint sequences, polarization, delay line, optical stages), and every "
                                                 "point is still stability-checked (spec §1.3-11, \"run all motion and criteria\"). "
                                                 "Safety level, interlocks, stability check and guard (Z-ramp overcurrent, withdraw / "
                                                 "re-approach checks) are exactly the same as a real run and are not relaxed because it "
                                                 "is a rehearsal; still no exposure, no trigger (digital lines are not configured "
                                                 "either), no data written. Wrap-up is also the same as a real run (bias / setpoint "
                                                 "restored as usual, delay line per return_to_start); LaserFocusMap sends the optical "
                                                 "axis back to the start at the end of the rehearsal."), default=False),
        _spec("laser_state", "str", _tr_lang("直调路径（界面按钮 / /api/skills/…/execute）上由操作员声明的激光状态："
              "attested_on / attested_off。agent 路径上由人工确认卡片给出，这里留空。外部 agent "
              "不能代为声明。",
                                             "Laser state declared by the operator on the direct-call path (UI button / "
                                             "/api/skills/.../execute): attested_on / attested_off. On the agent path it comes from "
                                             "the manual confirmation card, leave empty here. An external agent cannot declare it on "
                                             "the operator's behalf."), default="", allowed=["", "attested_on", "attested_off"]),
    ]


def with_defaults(meta: SkillMetadata, params: dict) -> dict:
    """``params`` 补上 metadata 里声明的默认值（执行层不补，skill 自己补）。None 当「没给」。"""
    out = {k: v for k, v in (params or {}).items()}
    for spec in meta.parameters:
        if out.get(spec.name) is None and spec.default is not None:
            out[spec.name] = spec.default
        out.setdefault(spec.name, None)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# 曝光期间禁止清单 = 持令牌期间的不变量（§1.5）
# ═════════════════════════════════════════════════════════════════════════════

class InterlockViolation(RuntimeError):
    """组合技能自己在曝光窗口里试图下发被禁止的动作 —— 编程错误，运行立即停下。"""


class ExposureInterlock:
    """曝光窗口。窗口打开期间：

    * 子技能只放行**只读**（category read / analysis）与停止 / 退针（``instrument_lock.BYPASS_NAMES``）；
      Nanonis XY / 偏压 / 设定点 / Z / 反馈 / 扫描 / 粗动（任何电机组）/ 数字线 / 锁相、所有光学台
      （Picomotor、XYZ、偏振、延迟线、Nanonis 光学电机组）、光谱仪任何设置与第二次采集一律拒绝；
    * 直接的 Nanonis 写只放行中止安全的停止 / 退针动词（``execution_context._ABORT_SAFE_WRITES``）。

    白名单不是黑名单：新加一个写技能不需要记得来这里登记，它默认就被拒绝。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._label: str | None = None
        self.opened = 0
        self.violations: list[str] = []

    @property
    def exposing(self) -> str | None:
        with self._lock:
            return self._label

    @contextmanager
    def window(self, label: str):
        with self._lock:
            if self._label is not None:
                raise InterlockViolation(
                    _tr_lang(f"曝光窗口 {self._label!r} 还开着，又要开 {label!r} —— 不允许两次采集叠加",
                             f"Exposure window {self._label!r} is still open, and {label!r} was requested - two "
                             f"overlapping acquisitions are not allowed"))
            self._label = str(label)
            self.opened += 1
        try:
            yield
        finally:
            with self._lock:
                self._label = None

    def refusal(self, skill_name: str, meta: Any = None) -> str:
        label = self.exposing
        if label is None:
            return ""
        from mast.core.instrument_lock import BYPASS_NAMES

        if skill_name in BYPASS_NAMES:
            return ""
        cat = str(getattr(getattr(meta, "category", None), "value", "") or "").lower()
        if cat in ("read", "analysis"):
            return ""
        msg = (_tr_lang(f"曝光窗口（{label}）内拒绝 {skill_name}：TERS 模式规格 §1.5 规定曝光期间 STM 不动、"
               "不改偏压 / 设定点 / 反馈、不扫描、不粗动、不打数字线，光学台一律不动，光谱仪设置不改；"
               "只放行只读与停止 / 退针。",
                        f"{skill_name} refused inside the exposure window ({label}): TERS mode spec §1.5 requires "
                        f"that during exposure the STM does not move, does not change bias / setpoint / feedback, "
                        f"does not scan, does not coarse-move, does not fire digital lines, optical stages stay "
                        f"still and spectrometer settings do not change; only read-only skills and stop / "
                        f"withdraw are allowed."))
        self.violations.append(msg)
        return msg

    def nanonis_refusal(self, method: str, args: tuple = ()) -> str:
        label = self.exposing
        if label is None:
            return ""
        from mast.core.execution_context import _is_abort_safe

        if _is_abort_safe(method, tuple(args)):
            return ""
        msg = _tr_lang(f"曝光窗口（{label}）内拒绝 Nanonis 写 {method}{tuple(args)!r}（§1.5）",
                       f"Nanonis write {method}{tuple(args)!r} refused inside the exposure window ({label}) "
                       f"(§1.5)")
        self.violations.append(msg)
        return msg


# ═════════════════════════════════════════════════════════════════════════════
# 到位判稳（§1.4）
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class TipReady:
    passed: bool
    reason: str = ""
    window_s: float = 0.0
    mean_a: float | None = None
    std_a: float | None = None
    dz_pm_s: float | None = None
    n: int = 0
    achieved_hz: float | None = None
    i_target_a: float | None = None
    feedback_on: bool | None = None
    waited_s: float = 0.0
    attempts: int = 0
    skipped: bool = False

    def as_meta(self) -> dict:
        return json_safe({k: getattr(self, k) for k in (
            "passed", "reason", "window_s", "mean_a", "std_a", "dz_pm_s", "n", "achieved_hz",
            "i_target_a", "feedback_on", "waited_s", "attempts", "skipped")})


#: tip-ready outcomes that say nothing about the tip: a stop that landed during the judging, or a
#: judging that could not read. Their point is recorded with this status, never as unstable_tip,
#: and they do not count towards qc.unstable_tip (an E-STOP withdraws the tip mid-judging).
NOT_A_TIP_VERDICT = {"aborted": "aborted", "feedback_state": "failed", "read_error": "failed",
                     "no_setpoint": "failed"}


def judge_window(t: list[float], i: list[float], z: list[float | None], *,
                 i_target: float, tol: float, dz_limit_pm_s: float,
                 sigma0: float | None, spike_frac: float) -> tuple[bool, str, dict]:
    """一个判稳窗口的四条判据（纯函数，便于单测）。返回 ``(passed, 失败原因, 统计)``。"""
    ia = np.asarray(i, dtype=np.float64)
    ta_ = np.asarray(t, dtype=np.float64)
    mean = float(np.mean(ia))
    std = float(np.std(ia, ddof=1)) if ia.size > 1 else 0.0
    stats: dict[str, Any] = {"mean_a": mean, "std_a": std, "n": int(ia.size)}
    zz = [v for v in z if v is not None and math.isfinite(v)]
    dz = None
    if len(zz) >= 3 and len(zz) == len(z):
        slope = float(np.polyfit(ta_ - ta_[0], np.asarray(zz, dtype=np.float64), 1)[0])
        dz = slope * 1e12
    stats["dz_pm_s"] = dz
    ref = abs(float(i_target))
    if ref <= 0:
        return False, "no_setpoint", stats
    if np.any(np.abs(ia) > spike_frac * ref):
        return False, "spike", stats
    if abs(mean - i_target) > tol * ref:
        return False, "mean_off_setpoint", stats
    std_limit = max(tol * abs(mean), float(sigma0 or 0.0))
    if std > std_limit:
        return False, "current_noisy", stats
    if dz is None:
        return False, "z_unreadable", stats
    if abs(dz) > dz_limit_pm_s:
        return False, "z_drifting", stats
    return True, "", stats


# ═════════════════════════════════════════════════════════════════════════════
# 曝光守卫（§1.6 撞针一行）
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class GuardSpec:
    """曝光期间要守什么。``i_ref_a`` × ``frac`` 是相对上限；``i_abs_limit_a`` 是绝对上限（撤针相）。"""

    i_ref_a: float | None = None
    frac: float | None = None
    i_abs_limit_a: float | None = None
    expect_feedback: bool | None = True
    hold_s: float = GUARD_HOLD_S
    poll_hz: float = GUARD_HZ

    def limit(self) -> float | None:
        lims = []
        if self.i_ref_a is not None and self.frac is not None and abs(self.i_ref_a) > 0:
            lims.append(abs(self.i_ref_a) * float(self.frac))
        if self.i_abs_limit_a is not None:
            lims.append(abs(float(self.i_abs_limit_a)))
        return min(lims) if lims else None


class ExposureGuard:
    """曝光窗口里的监视线程：只读 + 停止，不做别的。

    * 以 ``poll_hz``（默认 20 Hz）读 ``Current_Get`` 与 ``ZCtrl_ZPosGet``；超限持续 ≥ ``hold_s`` ⇒
      ``tripped``，立即调运行的「停光谱仪」路径（免令牌、不抛、不等锁）；
    * 每 0.5 s 读一次 ``ZCtrl_OnOffGet``，与 ``expect_feedback`` 不符 ⇒ ``tripped``（反馈掉线）；
    * 在卫星枢纽上挂一个监听：同一设备的 ``manual_activity`` ⇒ ``manual``（该帧作废、运行暂停）；
      同一卫星的 ``restart`` / 链路 ``lost`` ⇒ ``link``。监听在枢纽线程上只置标志，不做 I/O。
    """

    def __init__(self, run: "TersRun", spec: GuardSpec) -> None:
        self.run = run
        self.spec = spec
        self.samples: list[tuple[float, float, float | None]] = []
        self.tripped: dict | None = None
        self.manual: dict | None = None
        self.link: str | None = None
        self.errors = 0
        self.t_start = 0.0
        self.t_stop = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._over_since: float | None = None
        self._hub_handle: Any = None
        self._hub: Any = None

    # -- hub listener (hub dispatch thread: flags only) --
    def _on_hub_event(self, sat_id: str, evt: dict) -> None:
        try:
            if str(sat_id) != str(self.run.sat or ""):
                return
            kind = str(evt.get("kind") or "")
            if kind == "manual_activity" and str(evt.get("device") or "") == str(self.run.remote_dev or ""):
                self.manual = dict(evt.get("data") or {}) or {"what": "manual_activity"}
            elif kind == "restart":
                self.link = "lost_on_restart"
            elif kind == "link" and str((evt.get("data") or {}).get("state") or "") == "lost":
                self.link = self.link or "satellite_lost"
        except Exception:  # noqa: BLE001 — never raise into the hub thread
            pass

    def start(self) -> None:
        self.t_start = time.time()
        if self.run.sat:
            try:
                from mast.instruments.remote import get_hub

                self._hub = get_hub()
                self._hub_handle = self._hub.add_listener(self._on_hub_event)
            except Exception as exc:  # noqa: BLE001 — a missing hub is reported by the acquisition
                logger.debug("guard: hub listener not attached: %s", exc)
        if self.spec.limit() is None and self.spec.expect_feedback is None:
            return
        self._thread = threading.Thread(target=self._loop, name="ters-exposure-guard", daemon=True)
        self._thread.start()

    def stop(self) -> "ExposureGuard":
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._hub is not None and self._hub_handle is not None:
            try:
                self._hub.remove_listener(self._hub_handle)
            except Exception:  # noqa: BLE001
                pass
        self.t_stop = time.time()
        return self

    def _trip(self, why: str, **detail: Any) -> None:
        if self.tripped is not None:
            return
        self.tripped = {"why": why, "t": time.time(), **json_safe(detail)}
        logger.warning("TERS exposure guard tripped (%s): %s", why, detail)
        self.run.stop_spectrometer_now(f"exposure guard: {why}")

    def _loop(self) -> None:
        period = 1.0 / max(1.0, float(self.spec.poll_hz))
        limit = self.spec.limit()
        next_fb = 0.0
        while not self._stop.is_set():
            t0 = time.monotonic()
            now = time.time()
            i = self.run.read_float("Current_Get")
            z = self.run.read_float("ZCtrl_ZPosGet")
            if i is None:
                self.errors += 1
            else:
                self.samples.append((now, i, z))
                self.run._publish_stm(now, i, z)
                if limit is not None and abs(i) > limit:
                    if self._over_since is None:
                        self._over_since = now
                    elif now - self._over_since >= self.spec.hold_s - 1e-3:
                        self._trip("current", current_a=i, limit_a=limit,
                                   over_for_s=round(now - self._over_since, 3))
                else:
                    self._over_since = None
            if self.spec.expect_feedback is not None and time.monotonic() >= next_fb:
                fb = self.run.read_bool("ZCtrl_OnOffGet")
                next_fb = time.monotonic() + 0.5
                if fb is not None and fb != self.spec.expect_feedback:
                    self._trip("feedback", feedback_on=fb, expected=self.spec.expect_feedback)
            if self.tripped is not None:
                # keep sampling a little: the trace after the trip is evidence too
                pass
            self._stop.wait(max(0.0, period - (time.monotonic() - t0)))

    def window_stats(self, t0: float | None = None, t1: float | None = None) -> dict:
        """[t0, t1] 内的电流 / Z 统计（不给 = 整个守卫期间）。"""
        sel = [(t, i, z) for (t, i, z) in self.samples
               if (t0 is None or t >= t0) and (t1 is None or t <= t1)]
        if not sel:
            return {"n": 0}
        ia = np.asarray([s[1] for s in sel], dtype=np.float64)
        zs = [s[2] for s in sel if s[2] is not None]
        out = {"n": int(ia.size), "current_mean_a": float(ia.mean()),
               "current_std_a": float(ia.std(ddof=1)) if ia.size > 1 else 0.0,
               "current_max_abs_a": float(np.max(np.abs(ia))),
               "t0": sel[0][0], "t1": sel[-1][0]}
        if len(zs) >= 2:
            out["z_drift_pm"] = float((zs[-1] - zs[0]) * 1e12)
        return out


# ═════════════════════════════════════════════════════════════════════════════
# LiveHub（§1.8）
# ═════════════════════════════════════════════════════════════════════════════

class RunLive:
    """一个运行的流：主流（模式专属的图 / 曲线）+ ``/status``。dry_run 不发任何东西。"""

    def __init__(self, run: "TersRun") -> None:
        self.run = run
        self.base = f"local/{run.mode_key}/{run.run_id}"
        self.main: LiveStream | None = None
        self.status_stream: LiveStream | None = None
        self.extra: dict[str, LiveStream] = {}
        self.enabled = not run.dry_run

    def open_main(self, kind: str, *, title: str, x_label: str | None = None,
                  x_unit: str | None = None, y_label: str | None = None,
                  y_unit: str | None = None, meta: dict | None = None,
                  retain: int | None = None) -> None:
        if not self.enabled:
            return
        self.main = LiveStream(self.base, kind, title=title, source=self.run.name,
                               x_label=x_label, x_unit=x_unit, y_label=y_label, y_unit=y_unit,
                               meta={**(meta or {}), "run_id": self.run.run_id,
                                     "mode": self.run.mode_id},
                               retain=retain)

    def open_extra(self, suffix: str, kind: str, *, title: str, **kw: Any) -> None:
        if not self.enabled:
            return
        meta = dict(kw.pop("meta", None) or {})
        meta.update({"run_id": self.run.run_id, "mode": self.run.mode_id})
        self.extra[suffix] = LiveStream(f"{self.base}/{suffix}", kind, title=title,
                                        source=self.run.name, meta=meta, **kw)

    def open_status(self) -> None:
        if not self.enabled or self.status_stream is not None:
            return
        self.status_stream = LiveStream(f"{self.base}/status", "status",
                                        title=_tr_lang(f"{self.run.name} 进度",
                                                       f"{self.run.name} progress"), source=self.run.name,
                                        meta={"run_id": self.run.run_id, "mode": self.run.mode_id})

    def publish(self, frame: dict, suffix: str | None = None) -> None:
        s = self.main if suffix is None else self.extra.get(suffix)
        if s is not None:
            s.publish(frame)

    def status(self, text: str, level: str = "info") -> None:
        if self.status_stream is not None:
            self.status_stream.publish({"text": str(text)[:480], "level": level})

    def stream_ids(self) -> list[str]:
        out = [s.stream_id for s in (self.main, self.status_stream) if s is not None and s.active]
        out += [s.stream_id for s in self.extra.values() if s.active]
        return out

    def end(self) -> None:
        for s in (self.main, self.status_stream, *self.extra.values()):
            if s is not None:
                s.end()


# ═════════════════════════════════════════════════════════════════════════════
# 检查点与运行产物（§1.9、§11.3）
# ═════════════════════════════════════════════════════════════════════════════

def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunStore:
    """``<数据根>/artifacts/ters/<run_id>/``：检查点常驻；运行结束时写的 index / npz / summary
    经 ExperimentFileStore 归档进当前样品（没选样品就留在这里）。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.dir = self.root() / run_id

    @staticmethod
    def root() -> Path:
        from mast._runtime_paths import project_root

        return Path(project_root()) / "artifacts" / "ters"

    @classmethod
    def load_checkpoint(cls, run_id: str) -> dict | None:
        p = cls.root() / str(run_id) / "checkpoint.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def ensure(self) -> str:
        """建目录并查可写与空闲空间。返回拒绝原因（空串 = 可以）。"""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            probe = self.dir / ".write_probe"
            probe.write_bytes(b"ok")
            probe.unlink()
            free = shutil.disk_usage(self.dir).free
        except OSError as exc:
            return _tr_lang(f"运行目录 {self.dir} 不可写：{exc}",
                            f"Run directory {self.dir} is not writable: {exc}")
        if free < MIN_FREE_BYTES:
            return _tr_lang(f"运行目录所在磁盘只剩 {free / 2**20:.0f} MiB（< {MIN_FREE_BYTES / 2**20:.0f} MiB）",
                            f"The disk holding the run directory has only {free / 2**20:.0f} MiB left (< "
                            f"{MIN_FREE_BYTES / 2**20:.0f} MiB)")
        return ""

    def write_checkpoint(self, payload: dict) -> None:
        try:
            atomic_write_bytes(self.dir / "checkpoint.json", dump_json_bytes(payload))
        except Exception as exc:  # noqa: BLE001 — a lost checkpoint is logged, the run goes on
            logger.warning("TERS checkpoint write failed (%s): %s", self.run_id, exc)

    def write_products(self, stem: str, *, rows: list[dict], arrays: dict[str, Any],
                       summary: dict) -> dict:
        """写 ``<stem>_index.csv`` / ``<stem>.npz`` / ``<stem>_summary.json`` 并归档。永不抛。"""
        paths: list[Path] = []
        try:
            if rows:
                cols: list[str] = []
                for r in rows:
                    for k in r:
                        if k not in cols:
                            cols.append(k)
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({k: _csv_cell(r.get(k)) for k in cols})
                p = self.dir / f"{stem}_index.csv"
                atomic_write_bytes(p, buf.getvalue().encode("utf-8"))
                paths.append(p)
            if arrays:
                bio = io.BytesIO()
                np.savez_compressed(bio, **{k: np.asarray(v) for k, v in arrays.items()})
                p = self.dir / f"{stem}.npz"
                atomic_write_bytes(p, bio.getvalue())
                paths.append(p)
            p = self.dir / f"{stem}_summary.json"
            atomic_write_bytes(p, dump_json_bytes(summary))
            paths.append(p)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TERS products write failed (%s): %s", self.run_id, exc)
            return {"stored": False, "error": f"{type(exc).__name__}: {exc}",
                    "paths": [str(p) for p in paths]}
        try:
            stored = store_in_experiment(paths, skill=f"TERS:{stem}")
        except Exception as exc:  # noqa: BLE001
            stored = {"stored": False, "reason": f"{type(exc).__name__}: {exc}",
                      "paths": [str(p) for p in paths]}
        return stored


def _csv_cell(v: Any) -> Any:
    if isinstance(v, float):
        return "" if not math.isfinite(v) else repr(v)
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(json_safe(v), ensure_ascii=False)
    return v


# ═════════════════════════════════════════════════════════════════════════════
# 前置检查需要什么
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Needs:
    """一个模式对前置检查的要求。

    ``tip``: ``tunneling``（反馈开、电流在设定点附近 ≥ 5 s）/ ``withdrawn``（反馈关且已撤针）/
    ``any``（只记录）。``spectrometer``: 这个模式要不要光谱仪。``lockin_off``: 锁相调制必须关
    （M10 / M13 例外）。``safetip``: SafeTip 必须已启用（隧穿态的模式都要）。"""

    tip: str = "tunneling"
    spectrometer: bool = True
    lockin_off: bool = True
    safetip: bool = True
    scan_ok: bool = False


# ═════════════════════════════════════════════════════════════════════════════
# 一次采集的结果
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class AcqOutcome:
    key: str
    label: str
    status: str = "not_started"      # ok / dry_run / guard_abort / manual_activity / lost_on_restart /
    #                                  satellite_lost / operator_hold / aborted / failed / not_started
    error: str = ""
    path: str | None = None
    sidecar_path: str | None = None
    sha256: str | None = None
    axis_nm: np.ndarray | None = None
    x_cm1: np.ndarray | None = None
    frames: np.ndarray | None = None
    spectrum: np.ndarray | None = None
    combine: dict = field(default_factory=dict)
    qc_flags: list[str] = field(default_factory=list)
    bands: dict = field(default_factory=dict)
    intensity: float | None = None
    window: dict = field(default_factory=dict)
    guard: dict = field(default_factory=dict)
    stm_before: dict = field(default_factory=dict)
    stm_after: dict = field(default_factory=dict)
    drift_pm: float | None = None
    xy_drift_nm: float | None = None
    t_start_utc: str = ""
    t_end_utc: str = ""
    exposure_ms: float | None = None
    n_frames: int = 0
    data: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def band_value(self, name: str | None = None) -> float | None:
        """谱带 ``name`` 的净强度（不给 = 第一个谱带；没有谱带 = 总计数）。"""
        if not self.bands:
            return self.intensity
        if name is None:
            name = next(iter(self.bands))
        m = self.bands.get(name) or {}
        return m.get("area") if m.get("valid") else None

    def row(self) -> dict:
        r: dict[str, Any] = {
            "key": self.key, "label": self.label, "status": self.status,
            "qc_flags": "|".join(self.qc_flags), "file": self.path or "",
            "sidecar": self.sidecar_path or "", "sha256": self.sha256 or "",
            "t_start_utc": self.t_start_utc, "t_end_utc": self.t_end_utc,
            "exposure_ms": self.exposure_ms, "frames": self.n_frames,
            "intensity_total": self.intensity, "drift_pm": self.drift_pm,
            "xy_drift_nm": self.xy_drift_nm,
            "current_mean_a": self.window.get("current_mean_a"),
            "current_std_a": self.window.get("current_std_a"),
            "current_n": self.window.get("n"), "window_basis": self.window.get("basis"),
            "z_before_m": self.stm_before.get("z_pos_m"), "z_after_m": self.stm_after.get("z_pos_m"),
            "bias_v": self.stm_before.get("bias_v"), "setpoint_a": self.stm_before.get("setpoint_a"),
            "error": self.error[:200] if self.error else "",
        }
        for name, m in self.bands.items():
            r[f"band_{name}"] = m.get("area") if m.get("valid") else None
            r[f"band_{name}_snr"] = m.get("snr") if m.get("valid") else None
        return r


# ═════════════════════════════════════════════════════════════════════════════
# 运行引擎
# ═════════════════════════════════════════════════════════════════════════════

class _StepRecorder:
    """交给 GraphExecutor 的上下文：原样转发，另外记下最后一次 ``run`` 的 SkillResult（成败都记）。

    GraphExecutor 只把失败的**文字**交给 ``on_step_failed``；模式要看失败结果里的
    ``remote_code``（lost_on_restart / unavailable / operator_hold）才能决定怎么停。"""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self.last: SkillResult | None = None

    def run(self, skill_name: str, params: dict, version: str | None = None):
        if version is None:
            r = self._ctx.run(skill_name, params)
        else:
            r = self._ctx.run(skill_name, params, version=version)
        self.last = r
        return r

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)


def _scalar(rv: Any) -> float | None:
    """Nanonis 单值回包 ``(header, raw, [value])`` → float；读不出来 None（不猜）。"""
    if isinstance(rv, (list, tuple)) and len(rv) > 2:
        rv = rv[2]
    stack = [rv]
    while stack:
        item = stack.pop(0)
        if isinstance(item, bool):
            return float(item)
        if isinstance(item, (int, float)):
            f = float(item)
            return f if math.isfinite(f) else None
        if isinstance(item, (list, tuple)):
            stack = list(item) + stack
    return None


def _first_two(rv: Any) -> tuple[float, float] | None:
    if isinstance(rv, (list, tuple)) and len(rv) > 2:
        rv = rv[2]
    flat: list[float] = []
    stack = [rv]
    while stack and len(flat) < 2:
        item = stack.pop(0)
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            flat.append(float(item))
        elif isinstance(item, (list, tuple)):
            stack = list(item) + stack
    return (flat[0], flat[1]) if len(flat) == 2 else None


class TersRun:
    """一次 TERS 模式运行的状态与共用动作。模式的生成器拿着它写自己的序列。"""

    def __init__(self, skill: "TersComposite", ctx: Any, params: dict) -> None:
        self.skill = skill
        self.ctx = ctx
        self.meta = skill.metadata()
        self.name = self.meta.name
        self.mode_id = skill.MODE_ID
        self.mode_key = skill.MODE_KEY
        self.p = with_defaults(self.meta, params)
        self.dry_run = bool(self.p.get("dry_run"))
        self.dry_motion = self.dry_run and bool(self.p.get("dry_run_motion"))
        self.resume_of = str(self.p.get("resume_run_id") or "").strip()
        tag = "".join(c for c in str(self.p.get("tag") or "") if c.isalnum() or c in "-_")[:20]
        self.tag = tag
        self.run_id = self.resume_of or new_run_id(self.mode_key.replace("_", "-"))
        self.t0 = time.monotonic()
        self.started_utc = _utc()
        self.recorder = _StepRecorder(ctx)
        self.executor: GraphExecutor | None = None
        self.interlock = ExposureInterlock()
        self.live = RunLive(self)
        self.store = None if self.dry_run else RunStore(self.run_id)
        self.status = "running"
        self.reason = ""
        self.points: list[dict] = []
        self.spectra: list[tuple[str, np.ndarray]] = []
        self.axis_nm: np.ndarray | None = None
        self.qc = Counter()
        self.changes: dict[str, dict] = {}
        self.left_changed: dict[str, dict] = {}
        self.restored: dict[str, dict] = {}
        #: where the STM I/Z trace goes on LiveHub: None = nowhere, "" = the main stream,
        #: "stm" (or any suffix) = that extra stream
        self.stm_target: str | None = None
        self.facts: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.attestation: dict[str, Any] = {}
        self.plan: dict[str, Any] = {}
        self.analysis: dict[str, Any] = {}
        self.spec_device: str | None = None
        self.spec_status: dict = {}
        self.spec_limits: dict = {}
        self.spec_config: dict = {}
        self.sat: str | None = None
        self.remote_dev: str | None = None
        self.boot_id: str | None = None
        self.i_set: float | None = None
        self.sigma0: float | None = None
        self.dark: dict | None = None
        self.resume_done: set[str] = set()
        self.resume_checkpoint: dict | None = None
        self.bands: list[ta.Band] = []
        self.baselines: list[tuple[float, float]] = []
        self.total_points = 0
        self.products: dict = {}
        self._seq = 0
        self._acq_count = 0
        self._last_status_t = 0.0
        self._stm_live_t = 0.0

    # ── basic plumbing ─────────────────────────────────────────────────────

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.t0

    @property
    def stopped(self) -> bool:
        return self.status != "running"

    def stop(self, status: str, reason: str) -> None:
        """第一次停下的原因是真原因；之后的不覆盖它。"""
        if self.status == "running":
            self.status = status
            self.reason = str(reason)
            logger.info("[%s] %s: %s", self.name, status, reason)
            self.live.status(f"{status}: {reason}", "error" if status == "needs_operator" else "warn")

    def check(self) -> bool:
        """点与点之间的检查：中止、时间上限。返回 True = 该停了。"""
        if self.stopped:
            return True
        if self._aborted():
            self.stop("partial", "aborted by operator")
            return True
        lim = self.p.get("max_total_s")
        if lim and self.elapsed_s > float(lim):
            self.stop("partial", f"max_total_s={float(lim):.0f} s reached")
            return True
        return False

    def _aborted(self) -> bool:
        try:
            return bool(self.ctx.check_abort())
        except Exception:  # noqa: BLE001
            return False

    def sleep(self, seconds: float) -> bool:
        """可中止的等待（每 0.1 s 查一次 abort）。返回 True = 被中止了。"""
        end = time.monotonic() + max(0.0, float(seconds or 0.0))
        while True:
            if self._aborted():
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(0.1, left))

    def note_change(self, name: str, *, original: Any, current: Any, how: str = "",
                    restore: bool = True) -> None:
        """记下本次运行改过的设置（原值是改之前读到的）。

        ``restore=True``：正常结束时模式负责还原并调 :meth:`note_restored`，没还原的进「未还原」；
        ``restore=False``：本运行有意留下的设置（光谱仪的光栅 / 中心 / 出口），只记录。"""
        bucket = self.changes if restore else self.left_changed
        entry = bucket.setdefault(name, {"original": json_safe(original)})
        entry["current"] = json_safe(current)
        if how:
            entry["how"] = how

    def note_restored(self, name: str, value: Any = None) -> None:
        if name in self.changes:
            self.restored[name] = {**self.changes.pop(name), "restored_to": json_safe(value)}

    def keep_change(self, name: str, *, how: str = "") -> None:
        """运行有意留下的改动（例如对准后停在最优点）：从「未还原」挪到「有意留下」。"""
        entry = self.changes.pop(name, None)
        if entry is None:
            return
        if how:
            entry["how"] = how
        self.left_changed[name] = entry

    # ── reads (never through ctx.run: they take no token and pass every gate) ──

    def read_raw(self, method: str, *args: Any) -> tuple[Any, str]:
        call = _READS.get(method)
        if call is None:
            return None, _tr_lang(f"{method} 不在本模块的读动词表（_READS）里 —— 不发",
                                  f"{method} is not in this module's read-verb table (_READS) - not sent")
        try:
            rec = call(self.ctx, *args)
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"
        if getattr(rec, "error", ""):
            return None, str(rec.error)
        return getattr(rec, "return_value", None), ""

    def read_float(self, method: str, *args: Any) -> float | None:
        rv, err = self.read_raw(method, *args)
        return None if err else _scalar(rv)

    def read_bool(self, method: str, *args: Any) -> bool | None:
        v = self.read_float(method, *args)
        return None if v is None else bool(int(round(v)))

    def read_xy(self) -> tuple[float, float] | None:
        rv, err = self.read_raw("FolMe_XYPosGet", 1)
        return None if err else _first_two(rv)

    def nanonis_write(self, method: str, *args: Any) -> str:
        """组合技能自己的直接 Nanonis 写（只有 S+TTL 的触发脉冲）—— 过曝光联锁。返回错误文字。"""
        refusal = self.interlock.nanonis_refusal(method, args)
        if refusal:
            raise InterlockViolation(refusal)
        call = _WRITES.get(method)
        if call is None:
            return _tr_lang(f"{method} 不在本模块的写动词表（_WRITES）里 —— 不发",
                            f"{method} is not in this module's write-verb table (_WRITES) - not sent")
        try:
            rec = call(self.ctx, *args)
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"
        return str(getattr(rec, "error", "") or "")

    # ── sub-steps through ctx.run ──────────────────────────────────────────

    def _meta_of(self, skill_name: str) -> Any:
        reg = getattr(self.ctx, "_registry", None)
        if reg is None:
            return None
        try:
            return reg._get_metadata(reg.get(skill_name))
        except Exception:  # noqa: BLE001
            return None

    def step(self, phase: str, skill_name: str, params: dict, *, checkpoint: bool = False,
             window: str | None = None) -> Iterator[CompositeStep]:
        """一个子步骤（生成器）：过曝光联锁 → 交给 GraphExecutor 经 ``ctx.run`` 执行 → 返回 SkillResult。

        ``window`` 给了 = 这一步本身就是曝光（AcquireSpectrum）：窗口在它开始前打开、结束后关上。"""
        refusal = self.interlock.refusal(skill_name, self._meta_of(skill_name))
        if refusal:
            raise InterlockViolation(refusal)
        self._seq += 1
        sid = f"{self.run_id}:{self._seq:04d}:{phase}"
        self.recorder.last = None
        step = CompositeStep(step_id=sid, skill_name=skill_name, params=dict(params),
                             optional=True, checkpoint_after=checkpoint,
                             tags=(self.mode_key, phase))
        if window is None:
            yield step
        else:
            with self.interlock.window(window):
                yield step
        res = self.recorder.last
        if res is None:
            res = SkillResult(skill_name=skill_name, success=False,
                              error="step was not executed (executor skipped it)")
        return res

    # ── stop paths (direct, token-free; also legal after an abort) ─────────

    def stop_spectrometer_now(self, why: str = "") -> None:
        """停光谱仪：本机驱动的 stop（不拿锁、不抛），卫星设备由驱动转 hub.stop_device。"""
        if not self.spec_device:
            return
        try:
            from mast.instruments.registry import get_instrument_registry

            get_instrument_registry().spectrometer(self.spec_device).stop()
        except Exception as exc:  # noqa: BLE001 — panic path
            logger.warning("stop spectrometer %s failed (%s): %s", self.spec_device, why, exc)

    def stop_hardware_now(self, why: str) -> dict:
        """中止 / 守卫之后：停光谱仪、停所有已建的光学台、停本运行用到的卫星设备。不还原任何东西。"""
        report: dict[str, Any] = {"why": why}
        self.stop_spectrometer_now(why)
        try:
            from mast.instruments.registry import get_instrument_registry

            report["optics"] = get_instrument_registry().stop_all(wait_s=3.0)
        except Exception as exc:  # noqa: BLE001
            report["optics_error"] = f"{type(exc).__name__}: {exc}"
        if self.sat:
            try:
                from mast.instruments.remote import get_hub

                hub = get_hub()
                for dev in sorted(set(self.skill.satellite_devices(self)) | {self.remote_dev or ""}):
                    if dev:
                        hub.stop_device(self.sat, dev)
                report["satellite_stopped"] = True
            except Exception as exc:  # noqa: BLE001
                report["satellite_error"] = f"{type(exc).__name__}: {exc}"
        return report

    # ── preflight (§1.3) ──────────────────────────────────────────────────

    def preflight(self, needs: Needs) -> Iterator[CompositeStep]:
        """全部只读。任一不过 ⇒ ``status = refused``，返回 False。"""
        refusals: list[str] = []
        if self._aborted():
            self.stop("refused", "aborted by operator — not started")
            return False
        r = tip_work_refusal()
        if r:
            refusals.append(r)
        if str(self.p.get("lfe_preset") or "").strip():
            refusals.append(_tr_lang("lfe_preset 非空：卫星协议 v1 没有加载 LightField 实验文件的动作 —— 请在 "
                            "LightField 里先加载好，再把 lfe_preset 留空",
                                     "lfe_preset is not empty: satellite protocol v1 has no action to load a LightField "
                                     "experiment file - load it in LightField first, then leave lfe_preset empty"))
        # port ownership (contract §10.5) — never kills anything, only refuses
        try:
            from mast.core import port_guard

            rep = port_guard.check(reason=f"{self.name} 开始前")
            self.facts["port_guard"] = {"ok": bool(rep.ok), "complete": bool(rep.complete),
                                        "conflicts": len(rep.conflicts), "message": rep.message}
            if not rep.ok:
                refusals.append(rep.message)
            elif not rep.complete:
                self.warnings.append(_tr_lang("端口归属检查没跑完整：",
                                              "Port ownership check did not complete: ") + "；".join(rep.errors[:2]))
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(_tr_lang(f"端口归属检查失败：{type(exc).__name__}: {exc}",
                                          f"Port ownership check failed: {type(exc).__name__}: {exc}"))
        if needs.spectrometer:
            ok = yield from self._preflight_spectrometer(refusals)
            if ok:
                self._preflight_satellite(refusals)
        self._preflight_nanonis(needs, refusals)
        extra = yield from self.skill.preflight_extra(self)
        refusals.extend(extra or [])
        if not self.dry_run and self.store is not None and not refusals:
            err = self.store.ensure()
            if err:
                refusals.append(err)
        if self.resume_of and not refusals:
            refusals.extend(self._check_resume())
        if refusals:
            self.stop("refused", "；".join(refusals))
            return False
        return True

    def _preflight_spectrometer(self, refusals: list[str]) -> Iterator[CompositeStep]:
        params = {"device_id": str(self.p.get("device_id") or "")}
        res = yield from self.step("preflight:spectrometer", "GetSpectrometerStatus", params)
        if not res.success:
            refusals.append(_tr_lang(f"光谱仪状态读不到：{res.error}",
                                     f"Cannot read spectrometer state: {res.error}"))
            return False
        data = res.data or {}
        dev = data.get("device") or {}
        st = dict(data.get("status") or {})
        self.spec_device = str(dev.get("id") or params["device_id"] or "") or None
        self.spec_status = st
        self.spec_limits = dict(data.get("limits") or {})
        self.sat = dev.get("satellite") or None
        self.remote_dev = dev.get("remote_device") or None
        self.facts["spectrometer"] = {"device": json_safe(dev), "status": json_safe(st),
                                      "limits": self.spec_limits,
                                      "laser_line_nm_device": data.get("laser_line_nm")}
        if st.get("is_running"):
            refusals.append(_tr_lang(f"{self.spec_device} 正在采集（可能有人在 LightField 里手动采）—— 不叠加采集",
                                     f"{self.spec_device} is acquiring (someone may be acquiring manually in LightField) - not "
                                     f"stacking acquisitions"))
        if st.get("operator_hold"):
            refusals.append(_tr_lang(f"{self.spec_device} 在卫星上处于人工接管（operator_hold）",
                                     f"{self.spec_device} is under manual control on the satellite (operator_hold)"))
        locked = st.get("temperature_locked")
        if locked is not True:
            refusals.append(_tr_lang(f"CCD 温度未锁定（temperature_locked={locked!r}，"
                            f"温度 {st.get('temperature_c')!r} °C）—— §1.3-2 要求 −120 °C 且 Locked",
                                     f"CCD temperature not locked (temperature_locked={locked!r}, temperature "
                                     f"{st.get('temperature_c')!r} °C) - §1.3-2 requires -120 °C and Locked"))
        try:
            center = ta.shift_cm1_to_nm(float(self.p["center_shift_cm1"]),
                                        float(self.p["laser_line_nm"]))
        except (KeyError, TypeError, ValueError) as exc:
            refusals.append(_tr_lang(f"中心位移换算失败：{exc}",
                                     f"Center shift conversion failed: {exc}"))
            return False
        lo = self.spec_limits.get("min_center_nm")
        hi = self.spec_limits.get("max_center_nm")
        if lo is not None and hi is not None and not (float(lo) <= center <= float(hi)):
            refusals.append(_tr_lang(f"中心 {center:.2f} nm（{self.p['center_shift_cm1']} cm⁻¹ @ "
                            f"{self.p['laser_line_nm']} nm）超出光谱仪限值 [{lo}, {hi}] nm",
                                     f"Center {center:.2f} nm ({self.p['center_shift_cm1']} cm^-1 @ {self.p['laser_line_nm']} "
                                     f"nm) is outside the spectrometer limits [{lo}, {hi}] nm"))
        self.plan["center_nm"] = center
        return True

    def _preflight_satellite(self, refusals: list[str]) -> None:
        if not self.sat:
            self.facts["satellite"] = {"local_device": True}
            return
        try:
            from mast.instruments.remote import get_hub

            sats = list(get_hub().list_satellites() or [])
        except Exception as exc:  # noqa: BLE001
            refusals.append(_tr_lang(f"卫星枢纽不可用：{type(exc).__name__}: {exc}",
                                     f"Satellite hub unavailable: {type(exc).__name__}: {exc}"))
            return
        rec = next((s for s in sats if str(s.get("id")) == str(self.sat)), None)
        if rec is None:
            refusals.append(_tr_lang(f"卫星 {self.sat} 不在枢纽的名单里",
                                     f"Satellite {self.sat} is not in the hub's list"))
            return
        self.boot_id = rec.get("boot_id")
        rtt, off = rec.get("rtt_ms"), rec.get("clock_offset_ms")
        self.facts["satellite"] = {"id": self.sat, "state": rec.get("state"),
                                   "boot_id": self.boot_id, "rtt_ms": rtt,
                                   "clock_offset_ms": off}
        if rec.get("state") != "connected":
            refusals.append(_tr_lang(f"卫星 {self.sat} 状态 {rec.get('state')!r}，要求 connected",
                                     f"Satellite {self.sat} state {rec.get('state')!r}, connected required"))
        if rtt is not None and float(rtt) >= MAX_RTT_MS:
            refusals.append(_tr_lang(f"卫星 {self.sat} RTT {float(rtt):.1f} ms ≥ {MAX_RTT_MS} ms",
                                     f"Satellite {self.sat} RTT {float(rtt):.1f} ms >= {MAX_RTT_MS} ms"))
        if off is not None and abs(float(off)) >= MAX_CLOCK_OFFSET_MS:
            refusals.append(_tr_lang(f"卫星 {self.sat} 时钟偏差 {float(off):.1f} ms ≥ {MAX_CLOCK_OFFSET_MS} ms",
                                     f"Satellite {self.sat} clock offset {float(off):.1f} ms >= {MAX_CLOCK_OFFSET_MS} ms"))
        for d in rec.get("devices") or []:
            if str(d.get("id")) == str(self.remote_dev) and d.get("state") not in (None, "ready"):
                refusals.append(_tr_lang(f"{self.sat}/{self.remote_dev} 设备状态 {d.get('state')!r}，要求 ready",
                                         f"{self.sat}/{self.remote_dev} device state {d.get('state')!r}, ready required"))

    def _preflight_nanonis(self, needs: Needs, refusals: list[str]) -> None:
        reads = {
            "bias_v": self.read_float("Bias_Get"),
            "setpoint_a": self.read_float("ZCtrl_SetpntGet"),
            "feedback_on": self.read_bool("ZCtrl_OnOffGet"),
            "z_m": self.read_float("ZCtrl_ZPosGet"),
            "current_a": self.read_float("Current_Get"),
            "scan_running": self.read_bool("Scan_StatusGet"),
            "safetip_on": self.read_bool("SafeTip_OnOffGet"),
            "lockin_mod_on": self.read_bool("LockIn_ModOnOffGet", 1),
        }
        xy = self.read_xy()
        if xy is not None:
            reads["x_m"], reads["y_m"] = xy
        self.facts["nanonis"] = reads
        core = ("bias_v", "setpoint_a", "feedback_on", "current_a")
        if any(reads[k] is None for k in core):
            refusals.append(_tr_lang("Nanonis 读不到基本状态（偏压 / 设定点 / 反馈 / 电流）—— 连接不正常",
                                     "Cannot read the basic Nanonis state (bias / setpoint / feedback / current) - connection "
                                     "is not healthy"))
            return
        self.i_set = reads["setpoint_a"]
        if reads["scan_running"] and not needs.scan_ok:
            refusals.append(_tr_lang("Nanonis 正在扫描 —— 先停扫描（StopScan）",
                                     "Nanonis is scanning - stop the scan first (StopScan)"))
        if needs.lockin_off:
            if reads["lockin_mod_on"] is True:
                refusals.append(_tr_lang("锁相调制开着（LockIn_ModOnOff）—— 本模式要求关（§1.3-4）",
                                         "Lock-in modulation is on (LockIn_ModOnOff) - this mode requires it off (§1.3-4)"))
            elif reads["lockin_mod_on"] is None:
                self.warnings.append(_tr_lang("锁相调制状态读不到（不当成「关」）",
                                              "Cannot read the lock-in modulation state (not treated as \"off\")"))
        if needs.tip == "tunneling":
            if reads["feedback_on"] is not True:
                refusals.append(_tr_lang("Z 反馈没开 —— 本模式要求针尖在隧穿状态（反馈闭合）",
                                         "Z feedback is off - this mode requires the tip in the tunneling state (feedback closed)"))
            if needs.safetip and reads["safetip_on"] is not True:
                refusals.append(_tr_lang(f"SafeTip 未启用（读回 {reads['safetip_on']!r}）—— 请先 EnableSafeTip",
                                         f"SafeTip is not enabled (read back {reads['safetip_on']!r}) - run EnableSafeTip first"))
            if not refusals:
                self._preflight_current_window(refusals)
        elif needs.tip == "withdrawn":
            snap = None
            try:
                snap = self.ctx.state.snapshot()
            except Exception:  # noqa: BLE001
                pass
            withdrawn = getattr(snap, "withdrawn", None)
            self.facts["nanonis"]["withdrawn"] = withdrawn
            if reads["feedback_on"] is not False or withdrawn is not True:
                refusals.append(_tr_lang(f"本模式要求针尖已撤出（反馈关且 withdrawn）：反馈 "
                                f"{reads['feedback_on']!r}、withdrawn {withdrawn!r}",
                                         f"This mode requires the tip to be withdrawn (feedback off and withdrawn): feedback "
                                         f"{reads['feedback_on']!r}, withdrawn {withdrawn!r}"))

    def _preflight_current_window(self, refusals: list[str]) -> None:
        tol = float(self.p.get("stable_tol_frac") or 0.2)
        target = float(self.i_set or 0.0)
        if target == 0.0:
            refusals.append(_tr_lang("设定点读回为 0 —— 无法判电流",
                                     "Setpoint reads back as 0 - cannot judge the current"))
            return
        vals: list[float] = []
        end = time.monotonic() + PREFLIGHT_STABLE_S
        while time.monotonic() < end:
            if self._aborted():
                refusals.append("aborted by operator during preflight")
                return
            v = self.read_float("Current_Get")
            if v is not None:
                vals.append(v)
            time.sleep(0.1)
        if len(vals) < 3:
            refusals.append(_tr_lang("预飞电流窗口读不到足够的电流样本",
                                     "Not enough current samples in the pre-flight current window"))
            return
        a = np.asarray(vals)
        in_band = float(np.mean(np.abs(a - target) <= tol * abs(target)))
        self.sigma0 = float(np.std(a, ddof=1))
        self.facts["preflight_current"] = {"window_s": PREFLIGHT_STABLE_S, "n": len(vals),
                                           "mean_a": float(a.mean()), "std_a": self.sigma0,
                                           "in_band_frac": in_band, "setpoint_a": target}
        if in_band < PREFLIGHT_IN_BAND_FRAC or abs(float(a.mean()) - target) > tol * abs(target):
            refusals.append(_tr_lang(f"电流不在设定点 ±{tol:.0%} 内（{PREFLIGHT_STABLE_S:.0f} s 窗口里落带比例 "
                            f"{in_band:.0%}，均值 {a.mean():.3g} A vs {target:.3g} A）—— §1.3-5",
                                     f"Current is not within +-{tol:.0%} of the setpoint (fraction of the "
                                     f"{PREFLIGHT_STABLE_S:.0f} s window inside the band {in_band:.0%}, mean {a.mean():.3g} A "
                                     f"vs {target:.3g} A) - §1.3-5"))

    def _check_resume(self) -> list[str]:
        cp = RunStore.load_checkpoint(self.resume_of)
        if cp is None:
            return [_tr_lang(f"resume_run_id={self.resume_of!r} 找不到检查点",
                             f"No checkpoint found for resume_run_id={self.resume_of!r}")]
        if cp.get("skill") != self.name:
            return [_tr_lang(f"检查点属于 {cp.get('skill')!r}，不是 {self.name}",
                             f"The checkpoint belongs to {cp.get('skill')!r}, not {self.name}")]
        out = []
        tip_now = _tip_id()
        if cp.get("tip_id") is not None and tip_now is not None and cp.get("tip_id") != tip_now:
            out.append(_tr_lang(f"针尖变了（检查点 tip_id={cp.get('tip_id')!r}，现在 {tip_now!r}）—— 不续跑",
                                f"The tip changed (checkpoint tip_id={cp.get('tip_id')!r}, now {tip_now!r}) - not resuming"))
        ep_now = _coord_epoch()
        if cp.get("coord_epoch") is not None and ep_now is not None and cp.get("coord_epoch") != ep_now:
            out.append(_tr_lang(f"坐标代次变了（{cp.get('coord_epoch')} → {ep_now}，期间有过横向粗动）—— 不续跑",
                                f"The coordinate epoch changed ({cp.get('coord_epoch')} -> {ep_now}, a lateral coarse "
                                f"motion happened in between) - not resuming"))
        old = cp.get("spectrometer") or {}
        for k in ("grating", "center_shift_cm1", "exposure_ms", "frames", "roi_rows"):
            if k in old and old.get(k) != json_safe(self.p.get(k)):
                out.append(_tr_lang(f"光谱仪设置 {k} 与检查点不同（{old.get(k)!r} → {self.p.get(k)!r}）—— 不续跑",
                                    f"Spectrometer setting {k} differs from the checkpoint ({old.get(k)!r} -> "
                                    f"{self.p.get(k)!r}) - not resuming"))
        if cp.get("satellite", {}).get("boot_id") not in (None, self.boot_id):
            self.warnings.append(_tr_lang("续跑：卫星 boot_id 与检查点不同（卫星重启过）—— 全部状态已重新读取",
                                          "Resume: satellite boot_id differs from the checkpoint (the satellite restarted) - all "
                                          "state was re-read"))
        self.resume_checkpoint = cp
        self.resume_done = {k for k, v in (cp.get("points") or {}).items()
                            if v.get("status") in ("ok", "skipped")}
        return out

    # ── laser attestation (CP-C) ──────────────────────────────────────────

    def attest_laser(self) -> bool:
        p = self.p
        base = {"laser_power_uW_operator": p.get("laser_power_uW"),
                "laser_note": p.get("laser_note") or "", "calib_checked": False}
        if self.dry_run:
            self.attestation = {**base, "required": False, "laser_state": "unknown",
                                "attested_via": "dry_run"}
            return True
        if not p.get("laser_attest"):
            self.attestation = {**base, "required": False, "laser_state": "unknown",
                                "attested_via": "not_requested"}
            return True
        extra_lines = self.skill.attestation_lines(self)
        msg = (_tr_lang("请确认激光状态（MAST 关不了激光，也测不了功率）：He-Ne / 超快激光是否打开、"
               f"样品处估计功率 {p.get('laser_power_uW')!r} µW。",
                        "Please confirm the laser state (MAST cannot switch the laser off or measure its power): "
                        "is the He-Ne / ultrafast laser on, estimated power at the sample "
                        "{p.get('laser_power_uW')!r} µW.")
               + "".join(f"\n· {line}" for line in extra_lines)
               + _tr_lang("\n（可选）波数定标：用 Si 520.7 cm⁻¹ 或样品已知峰核对过吗？没核对会记 calib_checked=false。",
                          "\n(Optional) Wavenumber calibration: did you check against Si 520.7 cm^-1 or a known "
                          "sample peak? If not checked, calib_checked=false is recorded."))
        payload = {"kind": "workflow_human", "workflow": self.name, "node_id": "laser_attest",
                   "decision_key": f"{self.run_id}:laser_attest", "message": msg,
                   "inputs": json_safe({"laser_power_uW": p.get("laser_power_uW"),
                                        "laser_note": p.get("laser_note") or "",
                                        "mode": self.mode_id}),
                   "routes": ["laser_on", "laser_off", "cancel"]}
        decision = human_channel(payload)    # GraphInterrupt bubbles on the agent path
        if decision is None:
            owner = str(getattr(self.ctx, "owner", "") or "")
            if owner.startswith("ext:"):
                self.stop("refused", _tr_lang("外部 agent 不能代为确认激光状态（laser_attest=true 需要操作员）——"
                                     "请操作员在 MAST 界面确认，或以 laser_attest=false 运行（记 laser_state=unknown）",
                                              "An external agent cannot confirm the laser state on the operator's behalf "
                                              "(laser_attest=true needs the operator) - ask the operator to confirm in the MAST UI, or "
                                              "run with laser_attest=false (records laser_state=unknown)"))
                return False
            state = str(p.get("laser_state") or "")
            if state not in ("attested_on", "attested_off"):
                self.stop("refused", _tr_lang("这条执行路径没有人工确认卡片：laser_attest=true 时请由操作员显式给出 "
                                     "laser_state（attested_on / attested_off），或改 laser_attest=false",
                                              "This execution path has no manual confirmation card: with laser_attest=true the "
                                              "operator must give laser_state explicitly (attested_on / attested_off), or change to "
                                              "laser_attest=false"))
                return False
            self.attestation = {**base, "required": True, "laser_state": state,
                                "attested_via": "direct_call", "owner": owner}
            return True
        route = decision.get("route") if isinstance(decision, dict) else str(decision)
        note = decision.get("note", "") if isinstance(decision, dict) else ""
        if route not in ("laser_on", "laser_off"):
            self.stop("refused", _tr_lang(f"操作员没有确认激光状态（{route!r}）",
                                          f"The operator did not confirm the laser state ({route!r})"))
            return False
        self.attestation = {**base, "required": True,
                            "laser_state": "attested_on" if route == "laser_on" else "attested_off",
                            "attested_via": "hitl", "operator_note": note}
        return True

    # ── spectrometer configuration (the one place settings change) ────────

    def configure_spectrometer(self, *, trigger_response: str = "NoResponse",
                               trigger_edge: str | None = None, frames: int | None = None,
                               shutter_mode: str | None = None) -> Iterator[CompositeStep]:
        p = self.p
        dev = self.spec_device or ""
        st = dict(self.spec_status)
        want_density = str(p["grating"])
        cur_g = str(st.get("grating") or "")
        if _grating_density(cur_g) != want_density:
            full = _grating_full_name(want_density, self.spec_limits.get("gratings") or [])
            if full is None:
                self.stop("refused", _tr_lang(f"光栅 {want_density} g/mm 的设备全名未知（当前 {cur_g!r}）——"
                                     "请在 optical_instruments.json 的 limits.gratings 里列出全名",
                                              f"The full device name for grating {want_density} g/mm is unknown (currently {cur_g!r}) - "
                                              f"list the full name under limits.gratings in optical_instruments.json"))
                return False
            res = yield from self.step("config:grating", "SetSpectrometerGrating",
                                       {"device_id": dev, "grating": full})
            if not res.success:
                self.stop("needs_operator", _tr_lang(f"换光栅失败：{res.error}",
                                                     f"Grating change failed: {res.error}"))
                return False
            self.note_change("spectrometer.grating", original=cur_g, current=full, restore=False)
        center = float(self.plan.get("center_nm") or ta.shift_cm1_to_nm(
            float(p["center_shift_cm1"]), float(p["laser_line_nm"])))
        cur_c = st.get("center_wavelength_nm")
        if cur_c is None or abs(float(cur_c) - center) > 0.01:
            res = yield from self.step("config:center", "SetSpectrometerCenter",
                                       {"device_id": dev, "center_nm": center})
            if not res.success:
                self.stop("needs_operator", _tr_lang(f"设中心波长失败：{res.error}",
                                                     f"Setting the center wavelength failed: {res.error}"))
                return False
            self.note_change("spectrometer.center_nm", original=cur_c, current=center,
                             restore=False)
        if str(st.get("exit_port") or "front") != "front":
            res = yield from self.step("config:exit", "SetSpectrometerExitPort",
                                       {"device_id": dev, "port": "front"})
            if not res.success:
                self.stop("needs_operator", _tr_lang(f"切出口失败：{res.error}",
                                                     f"Switching the exit port failed: {res.error}"))
                return False
            self.note_change("spectrometer.exit_port", original=st.get("exit_port"), current="front",
                             restore=False)
        cfg = {"device_id": dev, "exposure_ms": float(p["exposure_ms"]),
               "frames": int(frames if frames is not None else p.get("frames") or 1),
               "adc_mhz": float(p["adc_mhz"]), "adc_quality": str(p["adc_quality"]),
               "shutter_mode": str(shutter_mode or p["shutter_mode"]),
               "trigger_response": trigger_response, "timestamps": True,
               "frame_tracking": True, "roi": f"line:{int(p['roi_rows'])}"}
        if trigger_edge:
            cfg["trigger_edge"] = trigger_edge
        res = yield from self.step("config:camera", "ConfigureSpectrometer", cfg)
        if not res.success:
            self.stop("needs_operator", _tr_lang(f"配置光谱仪失败：{res.error}",
                                                 f"Configuring the spectrometer failed: {res.error}"))
            return False
        self.spec_config = {k: v for k, v in cfg.items() if k != "device_id"}
        rb = yield from self.step("config:readback", "GetSpectrometerStatus", {"device_id": dev})
        if not rb.success:
            self.stop("needs_operator", _tr_lang(f"配置后读回失败：{rb.error}",
                                                 f"Read-back after configuration failed: {rb.error}"))
            return False
        st2 = dict((rb.data or {}).get("status") or {})
        mism = _config_mismatches(cfg, st2, want_density, center)
        self.spec_config["readback"] = json_safe(st2)
        self.spec_config["center_nm"] = center
        self.spec_config["grating_density"] = want_density
        if mism:
            self.stop("needs_operator", _tr_lang("光谱仪设置读回与请求不符：",
                                                 "Spectrometer settings read back do not match the request: ") + "；".join(mism))
            return False
        self.spec_status = st2
        return True

    def dark_frame(self, *, force: bool = False) -> Iterator[CompositeStep]:
        """暗帧（§1.3-8）：本会话没有同参数暗帧才拍；快门 AlwaysClosed → 采 → 回到本运行配置的快门模式。"""
        key = (self.spec_device, float(self.p["exposure_ms"]), int(self.p["roi_rows"]),
               float(self.p["adc_mhz"]), str(self.p["adc_quality"]), str(self.p["grating"]),
               round(float(self.plan.get("center_nm") or 0.0), 2))
        if not force and key in _SESSION_DARKS:
            self.dark = {**_SESSION_DARKS[key], "reused": True}
            return True
        dev = self.spec_device or ""
        mode_after = str(self.spec_config.get("shutter_mode") or self.p["shutter_mode"])
        res = yield from self.step("dark:shutter_closed", "ConfigureSpectrometer",
                                   {"device_id": dev, "shutter_mode": "AlwaysClosed"})
        if not res.success:
            self.stop("needs_operator", _tr_lang(f"暗帧：关快门失败：{res.error}",
                                                 f"Dark frame: closing the shutter failed: {res.error}"))
            return False
        acq = yield from self.step(
            "dark:acquire", "AcquireSpectrum",
            {"device_id": dev, "exposure_ms": float(self.p["exposure_ms"]), "frames": 1,
             "save_raw": bool(self.p.get("save_spe", True)),
             "tag": self._file_tag("dark"), "sample_current": False,
             "meta_json": json.dumps(json_safe({"run_id": self.run_id, "mode": self.mode_id,
                                                "skill": self.name, "kind": "dark",
                                                "shutter_mode": "AlwaysClosed"}))},
            window="dark")
        back = yield from self.step("dark:shutter_restore", "ConfigureSpectrometer",
                                    {"device_id": dev, "shutter_mode": mode_after})
        if not back.success:
            self.stop("needs_operator", _tr_lang(f"暗帧之后快门没能回到 {mode_after}：{back.error}",
                                                 f"After the dark frame the shutter could not return to {mode_after}: {back.error}"))
            return False
        if not acq.success:
            self.stop("needs_operator", _tr_lang(f"暗帧采集失败：{acq.error}",
                                                 f"Dark frame acquisition failed: {acq.error}"))
            return False
        d = acq.data or {}
        self.dark = {"path": d.get("path"), "sidecar_path": d.get("sidecar_path"),
                     "sha256": d.get("sha256"), "exposure_ms": float(self.p["exposure_ms"]),
                     "utc": _utc(), "reused": False}
        _SESSION_DARKS[key] = dict(self.dark)
        return True

    # ── tip-ready (§1.4) ──────────────────────────────────────────────────

    def tip_ready(self, *, label: str, i_target: float | None = None,
                  feedback: bool | None = True, settle_s: float | None = None) -> TipReady:
        p = self.p
        t_start = time.monotonic()
        if i_target is None:
            i_target = self.read_float("ZCtrl_SetpntGet") or self.i_set
        if i_target is None:
            return TipReady(False, "no_setpoint")
        settle = float(p["settle_s"] if settle_s is None else settle_s)
        if self.sleep(settle):
            return TipReady(False, "aborted", i_target_a=i_target)
        win = float(p["stable_window_s"])
        deadline = time.monotonic() + float(p["stable_timeout_s"])
        tol = float(p["stable_tol_frac"])
        dz_lim = float(p["stable_dz_pm_per_s"])
        spike = float(p["exposure_guard_current_frac"])
        samples: deque = deque()
        period = 1.0 / TIP_READY_HZ
        errors = 0
        attempts = 0
        last = "no_window"
        stats: dict = {}
        fb_state: bool | None = None
        fb_next = 0.0
        n_total = 0
        t_first = None
        while True:
            t0 = time.monotonic()
            if self._aborted():
                return TipReady(False, "aborted", i_target_a=i_target,
                                waited_s=time.monotonic() - t_start, attempts=attempts)
            if feedback is not None and t0 >= fb_next:
                fb_state = self.read_bool("ZCtrl_OnOffGet")
                fb_next = t0 + 0.5
                if fb_state is not None and fb_state != feedback:
                    return TipReady(False, "feedback_state", i_target_a=i_target,
                                    feedback_on=fb_state, waited_s=time.monotonic() - t_start)
            now = time.time()
            i = self.read_float("Current_Get")
            z = self.read_float("ZCtrl_ZPosGet")
            if i is None:
                errors += 1
                if errors > 25:
                    return TipReady(False, "read_error", i_target_a=i_target,
                                    waited_s=time.monotonic() - t_start)
            else:
                samples.append((now, i, z))
                n_total += 1
                t_first = t_first or now
                while samples and now - samples[0][0] > win:
                    samples.popleft()
                self._publish_stm(now, i, z)
                span = samples[-1][0] - samples[0][0] if len(samples) > 1 else 0.0
                if span >= 0.9 * win and len(samples) >= 3:
                    attempts += 1
                    ts, iv, zv = zip(*samples)
                    ok, last, stats = judge_window(list(ts), list(iv), list(zv),
                                                   i_target=float(i_target), tol=tol,
                                                   dz_limit_pm_s=dz_lim, sigma0=self.sigma0,
                                                   spike_frac=spike)
                    if ok:
                        hz = (len(samples) - 1) / span if span > 0 else None
                        return TipReady(True, "", window_s=span, mean_a=stats["mean_a"],
                                        std_a=stats["std_a"], dz_pm_s=stats["dz_pm_s"],
                                        n=len(samples), achieved_hz=hz, i_target_a=i_target,
                                        feedback_on=fb_state,
                                        waited_s=time.monotonic() - t_start, attempts=attempts)
            if time.monotonic() > deadline:
                span = samples[-1][0] - samples[0][0] if len(samples) > 1 else 0.0
                return TipReady(False, f"timeout:{last}", window_s=span,
                                mean_a=stats.get("mean_a"), std_a=stats.get("std_a"),
                                dz_pm_s=stats.get("dz_pm_s"), n=len(samples),
                                achieved_hz=((len(samples) - 1) / span if span > 0 else None),
                                i_target_a=i_target, feedback_on=fb_state,
                                waited_s=time.monotonic() - t_start, attempts=attempts)
            left = period - (time.monotonic() - t0)
            if left > 0:
                time.sleep(left)

    def ready_or_policy(self, *, label: str, key: str, i_target: float | None = None,
                        feedback: bool | None = True) -> TipReady:
        """判稳 + CP-B ``on_unstable``：skip（记空点，调用方继续）/ retry / abort（停下等人）。"""
        tr = self.tip_ready(label=label, i_target=i_target, feedback=feedback)
        tries = 0
        while (not tr.passed and tr.reason not in NOT_A_TIP_VERDICT and not self._aborted()
               and self.p["on_unstable"] == "retry" and tries < int(self.p["retry_max"])):
            tries += 1
            tr = self.tip_ready(label=f"{label}:retry{tries}", i_target=i_target, feedback=feedback,
                                settle_s=0.0)
        tr.attempts += tries
        if tr.passed:
            return tr
        if self._aborted():                 # whatever the window saw, the stop is the cause
            tr.reason = "aborted"
        if tr.reason == "aborted":
            self.stop("partial", "aborted by operator")
        elif tr.reason in NOT_A_TIP_VERDICT:
            what = {"feedback_state": _tr_lang("反馈状态不对",
                                               "feedback state is wrong"), "read_error": _tr_lang("读不到电流",
                                                                                   "cannot read the current"),
                    "no_setpoint": _tr_lang("读不到设定点",
                                            "cannot read the setpoint")}[tr.reason]
            self.stop("needs_operator", _tr_lang(f"{label}: 判稳时{what}（{tr.reason}）",
                                                 f"{label}: {what} during the stability check ({tr.reason})"))
        else:
            if self.p["on_unstable"] == "abort":
                self.stop("needs_operator", f"{label}: unstable_tip（{tr.reason}）—— on_unstable=abort")
            self.qc["unstable_tip"] += 1
        tr.skipped = not self.stopped
        return tr

    # ── live STM trace (for the modes that show I / Z traces) ──────────────

    def _publish_stm(self, t: float, i: float, z: float | None) -> None:
        target = self.stm_target
        if target is None or t - self._stm_live_t < 0.05:
            return
        if target and target not in self.live.extra:
            return
        self._stm_live_t = t
        vals = {"I_A": i}
        if z is not None:
            vals["Z_m"] = z
        self.live.publish({"t": t, "values": vals}, suffix=target or None)

    def handle(self, out: AcqOutcome) -> bool:
        """一次采集的结局 → 运行是否继续（§1.9）。守卫 / 人工操作 / 卫星失联 / 人工接管 / 未知失败
        一律停在 ``needs_operator``，**绝不自动重拍**；中止 = ``partial``。"""
        if out.status in ("ok", "dry_run"):
            return True
        if out.status == "aborted":
            self.stop("partial", "aborted by operator")
        elif out.status in ("lost_on_restart", "satellite_lost"):
            self.stop("needs_operator", _tr_lang(f"{out.key}: {out.status} —— 在途采集作废、绝不自动补拍；"
                                        f"重新查询状态并经操作员确认后用 resume_run_id 续跑（{out.error}）",
                                                 f"{out.key}: {out.status} - the in-flight acquisition is void and is never retaken "
                                                 f"automatically; re-query the state and, after operator confirmation, continue with "
                                                 f"resume_run_id ({out.error})"))
        elif out.status == "not_started":
            if not self.stopped:
                self.stop("needs_operator", _tr_lang(f"{out.key}: 采集没开始（{out.error}）",
                                                     f"{out.key}: acquisition did not start ({out.error})"))
        else:
            self.stop("needs_operator", f"{out.key}: {out.status}（{out.error}）")
        return False

    # ── the guarded acquisition (§1.5 / §1.6 / §1.7) ───────────────────────

    def _file_tag(self, label: str) -> str:
        """AcquireSpectrum keeps 24 filename-safe characters of the tag, so what tells two files of one
        run apart (the label) goes first and stays whole; the run's random suffix next; the operator's
        tag and the mode only while they still fit. The sidecar carries all of it in full."""
        def safe(s: str) -> str:
            return "".join(c for c in str(s or "") if c.isalnum() or c in "-_")

        out = f"{safe(label)[:17]}_{self.run_id[-6:]}"
        for more in (safe(self.tag), self.mode_key.replace("_", "")):
            if more and len(out) + 1 + len(more) <= 24:
                out = f"{out}_{more}"
        return out

    def spectrum_extra(self, label: str, key: str, extra: dict | None) -> dict:
        return json_safe({
            "schema": "mast.ters_point/1", "run_id": self.run_id, "mode": self.mode_id,
            "mode_key": self.mode_key, "skill": self.name, "key": key, "label": label,
            "step_id": f"{self.run_id}:{key}",
            **(extra or {}),
            "laser": self.attestation,
            "spectrometer_config": {k: v for k, v in self.spec_config.items() if k != "readback"},
            "laser_line_nm_param": self.p.get("laser_line_nm"),
            "satellite": {"id": self.sat, "boot_id": self.boot_id},
            "dark": {k: (self.dark or {}).get(k) for k in ("path", "sha256")} if self.dark else None,
        })

    def check_satellite_link(self) -> tuple[str, str] | None:
        """采集前查卫星：不再 connected ⇒ satellite_lost；boot_id 变了 ⇒ lost_on_restart（绝不补拍）。"""
        if not self.sat:
            return None
        try:
            from mast.instruments.remote import get_hub

            rec = next((s for s in (get_hub().list_satellites() or [])
                        if str(s.get("id")) == str(self.sat)), None)
        except Exception as exc:  # noqa: BLE001
            return "satellite_lost", _tr_lang(f"卫星枢纽不可用：{exc}",
                                              f"Satellite hub unavailable: {exc}")
        if rec is None or rec.get("state") != "connected":
            return "satellite_lost", _tr_lang(f"卫星 {self.sat} 状态 {(rec or {}).get('state')!r}",
                                              f"Satellite {self.sat} state {(rec or {}).get('state')!r}")
        if self.boot_id is not None and rec.get("boot_id") != self.boot_id:
            return "lost_on_restart", (_tr_lang(f"卫星 {self.sat} 重启过（boot_id {self.boot_id} → "
                                       f"{rec.get('boot_id')}）—— 位置 / 快门 / 相机设置都不可信",
                                                f"Satellite {self.sat} restarted (boot_id {self.boot_id} -> {rec.get('boot_id')}) - "
                                                f"position / shutter / camera settings cannot be trusted"))
        return None

    def acquire(self, label: str, key: str, *, extra: dict | None = None,
                guard: GuardSpec | None = None, frames: int | None = None,
                exposure_ms: float | None = None) -> Iterator[CompositeStep]:
        out = AcqOutcome(key=key, label=label)
        if self.check():
            out.error = self.reason
            return out
        link = self.check_satellite_link()
        if link is not None:
            out.status, out.error = link
            return out
        n = int(frames if frames is not None else (self.p.get("frames") or 1))
        exp = float(exposure_ms if exposure_ms is not None else self.p["exposure_ms"])
        out.exposure_ms, out.n_frames = exp, n
        params = {"device_id": self.spec_device or "", "exposure_ms": exp, "frames": n,
                  "save_raw": bool(self.p.get("save_spe", True)),
                  "tag": self._file_tag(label), "sample_current": True,
                  "current_interval_s": 0.25,
                  "meta_json": json.dumps(self.spectrum_extra(label, key, extra))}
        if self.dry_run:
            params["dry_run"] = True
            res = yield from self.step(f"{label}:acquire_dry", "AcquireSpectrum", params)
            out.status = "dry_run" if res.success else "failed"
            out.error = "" if res.success else res.error
            out.data = {k: (res.data or {}).get(k) for k in ("exposure_ms", "frames", "limits")}
            return out
        g = ExposureGuard(self, guard or GuardSpec(expect_feedback=None))
        out.t_start_utc = _utc()
        t_wall0 = time.time()
        g.start()
        try:
            res = yield from self.step(f"{label}:acquire", "AcquireSpectrum", params,
                                       window=label)
        finally:
            g.stop()
        t_wall1 = time.time()
        out.t_end_utc = _utc()
        self._acq_count += 1
        out.guard = {"tripped": g.tripped, "manual": g.manual, "link": g.link,
                     "samples": len(g.samples), "read_errors": g.errors,
                     "limit_a": (guard or GuardSpec()).limit()}
        self._pin_monitoring(t_wall0, t_wall1)
        if g.tripped:
            out.status, out.error = "guard_abort", _tr_lang(f"曝光守卫触发：{g.tripped.get('why')}",
                                                            f"Exposure guard tripped: {g.tripped.get('why')}")
            out.qc_flags.append("guard_abort")
            self.qc["guard_abort"] += 1
            out.window = g.window_stats()
            return out
        if g.link:
            out.status, out.error = g.link, _tr_lang(f"曝光期间卫星 {g.link}",
                                                     f"Satellite {g.link} during exposure")
            out.qc_flags.append("lost_on_restart" if g.link == "lost_on_restart" else "satellite_lost")
            return out
        if not res.success:
            out.status = self._classify(res)
            out.error = res.error
            out.window = g.window_stats()
            if out.status in ("lost_on_restart", "satellite_lost"):
                out.qc_flags.append(out.status)
            return out
        self._load_result(out, res, g, t_wall0, t_wall1)
        if g.manual:
            out.status = "manual_activity"
            out.error = _tr_lang(f"曝光期间卫星上有人工操作：{g.manual}",
                                 f"Manual operation on the satellite during exposure: {g.manual}")
            out.qc_flags.append("manual_activity")
            self.qc["manual_activity"] += 1
            return out
        out.status = "ok"
        return out

    def _classify(self, res: SkillResult) -> str:
        if self._aborted():
            return "aborted"
        data = res.data or {}
        code = str(data.get("remote_code") or "")
        err = str(res.error or "")
        if code == "lost_on_restart" or "lost_on_restart" in err:
            return "lost_on_restart"
        if code == "operator_hold" or "operator_hold" in err:
            return "operator_hold"
        if code == "unavailable" or "unavailable" in err:
            return "satellite_lost"
        return "failed"

    def _pin_monitoring(self, t0: float, t1: float) -> None:
        """把曝光窗口那段 20 kHz 电流段钉住，防清扫（RecordTERSWindow 的 pin_range 设计）。"""
        try:
            from mast.monitoring.service import get_service

            if get_service() is None:
                return
            from mast.monitoring.store import get_store

            get_store().pin_range(t0, t1, reason=f"{self.name}:{self.run_id}")
        except Exception:  # noqa: BLE001 — pinning is bookkeeping, never a failure
            pass

    def _load_result(self, out: AcqOutcome, res: SkillResult, g: ExposureGuard,
                     t_wall0: float, t_wall1: float) -> None:
        d = res.data or {}
        out.path, out.sidecar_path, out.sha256 = d.get("path"), d.get("sidecar_path"), d.get("sha256")
        out.data = {k: d.get(k) for k in ("run_id", "n_frames", "n_pixels", "peak", "counts",
                                          "missing_frames", "stored_in_experiment")}
        side: dict = {}
        try:
            side = json.loads(Path(out.sidecar_path).read_text(encoding="utf-8")) if out.sidecar_path else {}
        except (OSError, ValueError) as exc:
            out.qc_flags.append("sidecar_unreadable")
            self.warnings.append(_tr_lang(f"{out.key}: sidecar 读不了：{exc}",
                                          f"{out.key}: cannot read sidecar: {exc}"))
        stm = side.get("stm") or {}
        out.stm_before = dict(stm.get("before") or {})
        out.stm_after = dict(stm.get("after") or {})
        zb, za = out.stm_before.get("z_pos_m"), out.stm_after.get("z_pos_m")
        if isinstance(zb, (int, float)) and isinstance(za, (int, float)):
            out.drift_pm = (float(za) - float(zb)) * 1e12
        xb, yb = out.stm_before.get("x_pos_m"), out.stm_before.get("y_pos_m")
        xa, ya = out.stm_after.get("x_pos_m"), out.stm_after.get("y_pos_m")
        if all(isinstance(v, (int, float)) for v in (xb, yb, xa, ya)):
            out.xy_drift_nm = math.hypot(float(xa) - float(xb), float(ya) - float(yb)) * 1e9
        acq = side.get("acquisition") or {}
        if acq.get("missing_frames"):
            out.qc_flags.append("count_mismatch")
            self.qc["count_mismatch"] += 1
        # exposure-window current statistics: by the camera's frame events when they carry a
        # satellite timestamp (hub clock = sat − θ), otherwise the command interval
        basis = "command_interval"
        t0, t1 = t_wall0, t_wall1
        fm = [f for f in (acq.get("frame_meta") or []) if isinstance(f, dict)]
        off = acq.get("clock_offset_ms") or 0.0
        if fm and all(isinstance(f.get("ts_sat_ms"), (int, float)) for f in fm):
            last = (float(fm[-1]["ts_sat_ms"]) - float(off)) / 1000.0
            span = (out.exposure_ms or 0.0) / 1000.0 * max(1, len(fm))
            t0, t1, basis = last - span, last, "camera_frame_events"
        out.window = {**g.window_stats(t0, t1), "basis": basis}
        if out.window.get("n", 0) == 0:
            out.window = {**g.window_stats(), "basis": "guard_interval"}
        try:
            arr = np.load(out.path) if out.path else None
        except Exception as exc:  # noqa: BLE001
            arr = None
            out.qc_flags.append("array_unreadable")
            self.warnings.append(_tr_lang(f"{out.key}: 谱文件读不了：{exc}",
                                          f"{out.key}: cannot read spectrum file: {exc}"))
        if arr is None or arr.ndim != 2 or arr.shape[0] < 2:
            return
        out.axis_nm = arr[0]
        out.frames = arr[1:]
        if self.axis_nm is None and np.isfinite(out.axis_nm).any():
            self.axis_nm = np.array(out.axis_nm)
        frac = float(self.p.get("max_saturation_frac") or 0.9)
        if ta.is_saturated(out.frames, frac):
            out.qc_flags.append("saturated")
            self.qc["saturated"] += 1
        mode = str(self.p.get("cosmic_filter") or "flag")
        if mode != "off":
            hits = [len(ta.find_cosmic(f)) for f in out.frames]
            if any(hits):
                out.qc_flags.append("cosmic")
                self.qc["cosmic"] += 1
        out.spectrum, out.combine = ta.combine_frames(out.frames, mode)
        laser = float(self.p.get("laser_line_nm") or 633.0)
        if np.isfinite(out.axis_nm).any():
            out.x_cm1 = ta.nm_to_shift_cm1(out.axis_nm, laser)
            for band in self.bands:
                out.bands[band.name] = ta.band_metrics(out.x_cm1, out.spectrum, band,
                                                       baselines=self.baselines or None)
        out.intensity = ta.total_counts(out.spectrum)
        dt = self.p.get("drift_tol_pm")
        if dt is not None and out.drift_pm is not None and abs(out.drift_pm) > float(dt):
            out.qc_flags.append("drift")
            self.qc["drift"] += 1

    # ── bookkeeping per point ──────────────────────────────────────────────

    def record(self, out: AcqOutcome | None, *, key: str, values: dict | None = None,
               status: str | None = None, tip: TipReady | None = None,
               keep_spectrum: bool = True) -> dict:
        """一条点记录：进运行的 index、（非 dry_run）写检查点、更新进度流。"""
        row: dict[str, Any] = {"run_id": self.run_id, "index": len(self.points), "key": key}
        if out is not None:
            row.update(out.row())
        if status is not None:
            # callers write unstable_tip for any tip that did not pass; the reason decides
            if status == "unstable_tip" and tip is not None and tip.reason in NOT_A_TIP_VERDICT:
                status = NOT_A_TIP_VERDICT[tip.reason]
            row["status"] = status
        if tip is not None:
            row.update({"tip_ready_passed": tip.passed, "tip_ready_reason": tip.reason,
                        "tip_ready_mean_a": tip.mean_a, "tip_ready_std_a": tip.std_a,
                        "tip_ready_dz_pm_s": tip.dz_pm_s, "tip_ready_hz": tip.achieved_hz})
        row.update(json_safe(values or {}))
        self.points.append(row)
        if out is not None and out.ok and keep_spectrum and out.spectrum is not None:
            self.spectra.append((key, np.asarray(out.spectrum, dtype=np.float32)))
        self.write_checkpoint()
        self.progress(f"{row.get('status')}: {key}")
        return row

    def checkpoint_payload(self) -> dict:
        pts = {r["key"]: {"index": r["index"], "status": r.get("status"), "file": r.get("file"),
                          "sha256": r.get("sha256")} for r in self.points}
        if self.resume_checkpoint:
            prior = dict(self.resume_checkpoint.get("points") or {})
            prior.update(pts)
            pts = prior
        return {"schema": "mast.ters_checkpoint/1", "run_id": self.run_id, "skill": self.name,
                "mode": self.mode_id, "status": self.status, "reason": self.reason,
                "created_utc": self.started_utc, "updated_utc": _utc(),
                "params": json_safe({k: v for k, v in self.p.items()}),
                "satellite": {"id": self.sat, "boot_id": self.boot_id},
                "spectrometer": json_safe({k: self.p.get(k) for k in (
                    "grating", "center_shift_cm1", "exposure_ms", "frames", "roi_rows")}),
                "tip_id": _tip_id(), "coord_epoch": _coord_epoch(),
                "scope": scope_ids(), "points": pts}

    def write_checkpoint(self) -> None:
        if self.store is not None and not self.dry_run:
            self.store.write_checkpoint(self.checkpoint_payload())

    def progress(self, text: str) -> None:
        done = len(self.points)
        total = max(self.total_points, done)
        eta = ""
        if 0 < done < total:
            per = self.elapsed_s / done
            eta = _tr_lang(f" · 剩余约 {per * (total - done) / 60:.1f} min",
                           f" · about {per * (total - done) / 60:.1f} min remaining")
        self.live.status(f"{done}/{total}{eta} · {text}")

    # ── finish ─────────────────────────────────────────────────────────────

    def finalize(self) -> None:
        if self.status == "running":
            if self._aborted():
                self.stop("partial", "aborted by operator")
            elif self.executor is not None and self.executor.progress.aborted:
                self.stop("needs_operator", self.executor.progress.aborted_reason or "executor stopped")
            else:
                self.status = "dry_run" if self.dry_run else "done"
        aborted = self._aborted()
        if aborted:
            self.facts["stopped_after_abort"] = self.stop_hardware_now("operator abort")
            if self.changes:
                for v in self.changes.values():
                    v["unrestored_reason"] = _tr_lang("中止之后不做还原（契约 §10.3）",
                                                      "No restore after abort (contract §10.3)")
        elif self.status == "needs_operator" and self.qc.get("guard_abort"):
            self.facts["stopped_after_guard"] = self.stop_hardware_now("exposure guard")
        if not self.dry_run:
            self.live.status(f"{self.status}: {self.reason or 'finished'}",
                             "info" if self.status == "done" else "warn")
            self.write_checkpoint()
            self.products = self._write_products()
        self.live.end()

    def _write_products(self) -> dict:
        if self.store is None or not (self.points or self.analysis):
            return {}
        arrays = dict(self.skill.product_arrays(self) or {})
        if self.spectra:
            width = max(len(s) for _, s in self.spectra)
            stack = np.full((len(self.spectra), width), np.nan, dtype=np.float32)
            for k, (_, s) in enumerate(self.spectra):
                stack[k, :len(s)] = s
            arrays["spectra"] = stack
            arrays["spectrum_keys"] = np.asarray([k for k, _ in self.spectra])
        if self.axis_nm is not None:
            arrays["axis_nm"] = self.axis_nm
            arrays["axis_cm1"] = ta.nm_to_shift_cm1(self.axis_nm, float(self.p["laser_line_nm"]))
        stem = f"{self.mode_key}_{self.run_id}"
        return self.store.write_products(stem, rows=self.points, arrays=arrays,
                                         summary=self.summary(full=True))

    def summary(self, *, full: bool = False) -> dict:
        s = {
            "schema": "mast.ters_run/1", "skill": self.name, "mode": self.mode_id,
            "run_id": self.run_id, "status": self.status, "reason": self.reason,
            "dry_run": self.dry_run, "dry_run_motion": self.dry_motion,
            "started_utc": self.started_utc, "elapsed_s": round(self.elapsed_s, 2),
            "points_done": sum(1 for r in self.points if r.get("status") == "ok"),
            "points_recorded": len(self.points), "points_total": self.total_points,
            "qc_counts": dict(self.qc), "laser": self.attestation,
            "unrestored": self.changes, "restored": self.restored,
            "left_changed": self.left_changed,
            "warnings": self.warnings[:20], "analysis": self.analysis,
            "plan": self.plan, "dark": self.dark, "resume_of": self.resume_of or None,
            "satellite": {"id": self.sat, "boot_id": self.boot_id},
            "spectrometer": {"device_id": self.spec_device,
                             "config": {k: v for k, v in self.spec_config.items() if k != "readback"}},
            "live_streams": self.live.stream_ids(), "scope": scope_ids(),
            "interlock": {"windows": self.interlock.opened,
                          "violations": self.interlock.violations[:5]},
            "bands": [b.as_dict() for b in self.bands],
        }
        if full:
            s["params"] = json_safe(self.p)
            s["preflight"] = json_safe(self.facts)
            s["coord_epoch"] = _coord_epoch()
            s["tip_id"] = _tip_id()
        return json_safe(s)

    def result(self) -> SkillResult:
        data = self.summary()
        data["preflight"] = {k: self.facts[k] for k in (
            "port_guard", "satellite", "nanonis", "preflight_current") if k in self.facts}
        # a withdraw is the one thing a stopped run does to the tip: say it in the result itself
        data["withdraws"] = list(self.facts.get("withdraws") or [])
        pts = self.points if len(self.points) <= MAX_POINTS_IN_RESULT else []
        data["points"] = [{k: r.get(k) for k in ("key", "status", "qc_flags", "file", "sidecar",
                                                 "intensity_total", *[c for c in r if c.startswith("band_")
                                                                      and not c.endswith("_snr")])}
                          for r in pts]
        if self.products:
            data["products"] = {"stored": self.products.get("stored"),
                                "paths": self.products.get("paths"),
                                "reason": self.products.get("reason") or self.products.get("error")}
            data["path"] = (self.products.get("paths") or [None])[0]
        ok = self.status in ("done", "dry_run")
        unrest = (_tr_lang(f"；未还原：{', '.join(f'{k}（原值 {v.get('original')!r}）' for k, v in self.changes.items())}",
                           f"; not restored: "
                           f"{', '.join(f'{k} (original {v.get('original')!r})' for k, v in self.changes.items())}")
                  if self.changes else "")
        head = "[DRY RUN] " if self.dry_run else ""
        summary = (_tr_lang(f"{head}{self.name} {self.status}: {data['points_done']}/{self.total_points} 点"
                   f"{('，' + self.reason) if self.reason else ''}{unrest}",
                            f"{head}{self.name} {self.status}: {data['points_done']}/{self.total_points} "
                            f"points{(', ' + self.reason) if self.reason else ''}{unrest}"))
        return SkillResult(skill_name=self.name, success=ok, data=data,
                           error="" if ok else (self.reason or self.status) + unrest,
                           summary=summary[:600])


_SESSION_DARKS: dict[tuple, dict] = {}


# ═════════════════════════════════════════════════════════════════════════════
# S+TTL：一次 N 帧的外触发采集（M03 / M10）
# ═════════════════════════════════════════════════════════════════════════════

class TriggeredAcquisition:
    """LightField 预设 N 帧（ReadoutPerTrigger）一次 Acquire，MAST 逐点打 TTL（规格 §1.2 S+TTL）。

    **为什么这里直接用设备层而不是 ``ctx.run("AcquireSpectrum")``**：这次采集要在后台跑几分钟，
    同时主线程逐点移台子、打触发。``ctx.run`` 是阻塞的；仪器令牌按线程可重入，后台线程拿不到
    组合技能持有的令牌（会在 5 s 后被判 busy）。必须直接用设备时走 Python 设备层，这正是那种情形。
    绕过 AcquireSpectrum 少掉的三件事由这里补齐：令牌 —— 前置检查已拒绝修针 / 进针 / 正在采 / 人工接管，
    组合技能本身持着令牌；落盘 —— 文件与 sidecar（``schema = mast.spectrum/1``，``extra.triggers`` 是逐帧的
    触发记录）照样经 ExperimentFileStore 归档进实验文件夹；LiveHub —— 卫星的原始帧由枢纽转发到
    ``<卫星>/<设备>/spectrum``。中止经 ``check_abort`` 传给驱动（卫星侧 cancel），之后再 ``stop()`` 一次。
    采集期间光谱仪的锁一直被占着：主线程只调 ``status()`` / ``stop()``（不拿锁），配置都在开始之前做完。
    """

    def __init__(self, run: "TersRun", *, n_frames: int, exposure_ms: float, label: str,
                 per_trigger_s: float, extra: dict | None = None) -> None:
        """``per_trigger_s``：相邻两个触发之间最坏要等多久（移台 + 等待 + 判稳超时 + 曝光读出周期），由模式
        按自己的计划算。设备层的默认时限按自由运行算（曝光 × 帧数），对外触发太短 —— 时限一到枢纽就会让卫星
        cancel，后半段的触发全部落空。"""
        self.run = run
        self.n = int(n_frames)
        self.exposure_ms = float(exposure_ms)
        self.per_trigger_s = float(per_trigger_s)
        self.label = label
        self.extra = dict(extra or {})
        self.frames: dict[int, np.ndarray] = {}
        self.axis_nm: np.ndarray | None = None
        self.result: Any = None
        self.error: str = ""
        self.triggers: list[dict] = []
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._stop_flag = threading.Event()
        self.stm_before: dict = {}
        self.stm_after: dict = {}
        self.started_utc = ""
        self.finished_utc = ""
        self._spec = None

    def _on_frame(self, i: int, arr: Any, axis: Any) -> None:
        with self._cond:
            self.frames[int(i)] = np.asarray(arr, dtype=np.float64)
            if axis is not None and self.axis_nm is None:
                self.axis_nm = np.asarray(axis, dtype=np.float64)
            self._cond.notify_all()

    def _check(self) -> bool:
        return self._stop_flag.is_set() or self.run._aborted()

    def start(self) -> str:
        from mast.instruments.registry import get_instrument_registry
        from mast.skills.builtins.optics_common import stm_snapshot

        try:
            self._spec = get_instrument_registry().spectrometer(self.run.spec_device)
        except Exception as exc:  # noqa: BLE001
            return _tr_lang(f"光谱仪不可用：{type(exc).__name__}: {exc}",
                            f"Spectrometer unavailable: {type(exc).__name__}: {exc}")
        self.stm_before = stm_snapshot(self.run.ctx, fresh=True)
        self.started_utc = _utc()
        # the whole trigger plan, not exposure × frames (the device's free-running default)
        self.timeout_s = self.n * self.per_trigger_s + 60.0
        meta = {"mast_run_id": self.run.run_id, "mode": self.run.mode_id,
                "triggered": True, "frames": self.n}

        def _go() -> None:
            try:
                # exposure_ms and frames both given: the device checks its max_total_s only then
                self.result = self._spec.acquire(
                    exposure_ms=self.exposure_ms, frames=self.n,
                    save=bool(self.run.p.get("save_spe", True)),
                    base_name=f"mast_{self.run.run_id}_{self.label}",
                    tag=self.run._file_tag(self.label), meta=meta, timeout_s=self.timeout_s,
                    check_abort=self._check, on_frame=self._on_frame)
            except Exception as exc:  # noqa: BLE001 — reported through .error
                self.error = f"{type(exc).__name__}: {exc}"
            finally:
                if self._check():
                    try:                      # takes no lock and never raises (device contract)
                        self._spec.stop()
                    except Exception:  # noqa: BLE001
                        pass
                self.finished_utc = _utc()
                self._done.set()
                with self._cond:
                    self._cond.notify_all()

        self._thread = threading.Thread(target=_go, name="ters-triggered-acq", daemon=True)
        self._thread.start()
        return ""

    def wait_running(self, timeout_s: float = 5.0) -> bool:
        """等相机进入「等触发」——用设备状态的 is_running；读不到就按时间放行（记一条警告）。"""
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if self._done.is_set():
                return False
            try:
                if self._spec.status().get("is_running"):
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.05)
        self.run.warnings.append(_tr_lang("外触发采集：等不到 is_running，按时间放行第一个触发",
                                          "Externally triggered acquisition: is_running never came, releasing the first trigger on "
                                          "time"))
        return not self._done.is_set()

    def wait_frame(self, index: int, timeout_s: float) -> bool:
        end = time.monotonic() + max(0.0, timeout_s)
        with self._cond:
            while int(index) not in self.frames:
                left = end - time.monotonic()
                if left <= 0 or self._done.is_set() or self.run._aborted():
                    return int(index) in self.frames
                self._cond.wait(min(0.1, left))
        return True

    def cancel(self) -> None:
        self._stop_flag.set()
        if self._spec is not None:
            self._spec.stop()

    def finish(self, timeout_s: float) -> bool:
        """等后台采集收尾；没收完就停掉它（ReadoutPerTrigger 等不到的触发不会自己来）。"""
        if self._thread is None:
            return False
        if not self._done.wait(timeout_s):
            self.cancel()
            self._done.wait(10.0)
        from mast.skills.builtins.optics_common import stm_snapshot

        self.stm_after = stm_snapshot(self.run.ctx, fresh=True)
        return self.result is not None and not self.error

    def reconcile(self) -> dict:
        """三重对账（M03.3 第 11 步）：帧数 = 触发数；帧号连续；曝光起始间隔 ≈ 触发间隔。"""
        meta = getattr(self.result, "meta", {}) or {}
        fm = [f for f in (meta.get("frame_meta") or []) if isinstance(f, dict)]
        n_trig = len(self.triggers)
        n_frames = len(self.frames)
        out: dict[str, Any] = {"triggers": n_trig, "frames_received": n_frames,
                               "frames_expected": self.n, "count_ok": n_frames == n_trig}
        track = [f.get("frame_tracking") for f in fm if f.get("frame_tracking") is not None]
        if track:
            out["frame_tracking_ok"] = all(int(b) - int(a) == 1 for a, b in zip(track, track[1:]))
        starts = [f.get("exposure_started_us") for f in fm]
        if len(starts) >= 2 and all(isinstance(s, (int, float)) for s in starts) and n_trig >= 2:
            d_exp = np.diff(np.asarray(starts, dtype=np.float64)) / 1e6
            d_trg = np.diff(np.asarray([t["t"] for t in self.triggers[:len(starts)]], dtype=np.float64))
            m = min(len(d_exp), len(d_trg))
            if m:
                dev = np.abs(d_exp[:m] - d_trg[:m])
                out["interval_max_dev_s"] = float(dev.max())
                out["interval_ok"] = bool(dev.max() <= 0.05)
        first_bad = None
        if not out["count_ok"]:
            first_bad = min(n_frames, n_trig)
        out["first_invalid_index"] = first_bad
        out["status"] = "ok" if (out["count_ok"] and out.get("frame_tracking_ok", True)
                                 and out.get("interval_ok", True)) else "count_mismatch"
        return out

    def write_files(self, reconciliation: dict) -> dict:
        """一个 .npy（第 0 行波长，其后按帧号）+ 一个 sidecar，经 ExperimentFileStore 归档。"""
        import hashlib

        from mast.skills.builtins.optics_common import (
            hlc_now,
            optics_positions,
            staging_dir,
        )
        from mast.instruments.registry import get_instrument_registry

        if not self.frames:
            return {"stored": False, "reason": "no frame arrived", "paths": []}
        order = sorted(self.frames)
        width = max(len(self.frames[i]) for i in order)
        axis = self.axis_nm
        if axis is None and self.result is not None:
            axis = getattr(self.result, "axis_nm", None)
        stack = np.full((len(order) + 1, width), np.nan, dtype=np.float64)
        if axis is not None:
            stack[0, :len(axis)] = axis
        for k, i in enumerate(order, start=1):
            stack[k, :len(self.frames[i])] = self.frames[i]
        bio = io.BytesIO()
        np.save(bio, stack, allow_pickle=False)
        payload = bio.getvalue()
        sha = hashlib.sha256(payload).hexdigest()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        stem = f"spec_{stamp}_{self.run.spec_device}_{self.run._file_tag(self.label)}"
        stem = "".join(c for c in stem if c.isalnum() or c in "-_")
        sdir = staging_dir("spectra")
        npy, js = sdir / f"{stem}.npy", sdir / f"{stem}.json"
        meta = getattr(self.result, "meta", {}) or {}
        try:
            positions = optics_positions(get_instrument_registry())
        except Exception:  # noqa: BLE001
            positions = {}
        side = {
            "schema": "mast.spectrum/1",
            "layout": "npy rows: [0] = wavelength_nm (NaN when unknown), [1..] = frames in frame order",
            "skill": self.run.name, "run_id": self.run.run_id, "triggered": True,
            "time": {"started_utc": getattr(self.result, "started_utc", "") or self.started_utc,
                     "finished_utc": getattr(self.result, "finished_utc", "") or self.finished_utc,
                     "hlc_end": hlc_now(), "clock_offset_ms": meta.get("clock_offset_ms"),
                     "rtt_ms": meta.get("rtt_ms")},
            "device": {"id": self.run.spec_device, "satellite": self.run.sat,
                       "remote_device": self.run.remote_dev, "cmd_id": meta.get("cmd_id")},
            "settings": {"requested": {"exposure_ms": self.exposure_ms, "frames": self.n,
                                       "timeout_s": getattr(self, "timeout_s", None),
                                       "per_trigger_s": self.per_trigger_s},
                         "config": {k: v for k, v in self.run.spec_config.items() if k != "readback"},
                         "laser_line_nm": self.run.p.get("laser_line_nm")},
            "stm": {"before": self.stm_before, "after": self.stm_after,
                    "coord_epoch": _coord_epoch(), "tip_id": _tip_id()},
            "optics": positions, "scope": {**scope_ids(), "step_id": f"{self.run.run_id}:{self.label}"},
            "files": {"array": {"name": npy.name, "sha256": sha, "size": len(payload),
                                "shape": list(stack.shape), "dtype": "float64",
                                "frame_order": order},
                      "satellite_files": json_safe(getattr(self.result, "files", []) or [])},
            "acquisition": json_safe(meta), "reconciliation": reconciliation,
            "extra": json_safe({**self.extra, "triggers": self.triggers,
                                "laser": self.run.attestation, "mode": self.run.mode_id}),
        }
        try:
            atomic_write_bytes(npy, payload)
            atomic_write_bytes(js, dump_json_bytes(side))
        except Exception as exc:  # noqa: BLE001
            return {"stored": False, "reason": f"{type(exc).__name__}: {exc}", "paths": []}
        stored = store_in_experiment([npy, js], skill=self.run.name)
        stored["sha256"] = sha
        return stored


def _grating_density(name: str) -> str:
    """'[500nm,1200][0][0]' → '1200'；'1200' → '1200'；认不出 → ''。"""
    import re

    s = str(name or "").strip()
    if s.isdigit():
        return s
    m = re.search(r",\s*(\d+)\s*\]", s)
    return m.group(1) if m else ""


def _grating_full_name(density: str, allowed: list) -> str | None:
    for g in allowed:
        if _grating_density(str(g)) == str(density):
            return str(g)
    return None


def _config_mismatches(cfg: dict, st: dict, density: str, center_nm: float) -> list[str]:
    """读回比对：只比设备**报了**的字段；没报的不当成相同，也不当成不同（记在 readback 里）。"""
    out = []
    if st.get("exposure_ms") is not None and abs(float(st["exposure_ms"]) - cfg["exposure_ms"]) > 1e-6:
        out.append(f"exposure_ms {st['exposure_ms']} ≠ {cfg['exposure_ms']}")
    if st.get("frames") is not None and int(st["frames"]) != int(cfg["frames"]):
        out.append(f"frames {st['frames']} ≠ {cfg['frames']}")
    if st.get("grating") and _grating_density(str(st["grating"])) != density:
        out.append(f"grating {st['grating']} ≠ {density}")
    if st.get("center_wavelength_nm") is not None and abs(float(st["center_wavelength_nm"]) - center_nm) > 0.05:
        out.append(f"center {st['center_wavelength_nm']} nm ≠ {center_nm:.2f} nm")
    if st.get("exit_port") and str(st["exit_port"]) != "front":
        out.append(f"exit_port {st['exit_port']} ≠ front")
    tr = st.get("trigger_response")
    if isinstance(tr, dict):
        tr = tr.get("response")
    if tr and str(tr) != cfg["trigger_response"]:
        out.append(f"trigger_response {tr} ≠ {cfg['trigger_response']}")
    if st.get("adc_mhz") is not None and abs(float(st["adc_mhz"]) - float(cfg["adc_mhz"])) > 1e-6:
        out.append(f"adc_mhz {st['adc_mhz']} ≠ {cfg['adc_mhz']}")
    roi = st.get("roi")
    if isinstance(roi, dict) and roi.get("mode") == "line" and roi.get("rows") is not None:
        want = int(str(cfg["roi"]).split(":", 1)[1])
        if int(roi["rows"]) != want:
            out.append(f"roi rows {roi['rows']} ≠ {want}")
    return out


def _coord_epoch() -> Any:
    try:
        from mast.core.coord_epoch import read_current_epoch

        return read_current_epoch()
    except Exception:  # noqa: BLE001
        return None


def _tip_id() -> Any:
    try:
        from mast.core.tip_state import current_tip_facts

        facts = current_tip_facts()
        return (facts or {}).get("tip_id")
    except Exception:  # noqa: BLE001
        return None


# ═════════════════════════════════════════════════════════════════════════════
# 模式基类
# ═════════════════════════════════════════════════════════════════════════════

class TersComposite(CompositeSkillGraph):
    """所有 TERS 模式的基类。子类给 ``MODE_ID`` / ``MODE_KEY`` / ``NEEDS``、``metadata()``、
    ``prepare()``（只校验参数、算计划，不碰硬件）与 ``ters_plan()``（生成器）。"""

    MODE_ID = ""
    MODE_KEY = ""
    NEEDS = Needs()
    #: configure_spectrometer 的触发设置（S 级 = NoResponse；S+TTL 模式覆盖）
    TRIGGER_RESPONSE = "NoResponse"

    # -- hooks for subclasses --
    def needs(self, run: TersRun) -> Needs:
        """前置检查的要求；参数决定要求的模式（M10 的 tip_state、M12 的 tip_state_policy）覆盖它。"""
        return self.NEEDS

    def prepare(self, run: TersRun) -> list[str]:  # pragma: no cover - overridden
        return []

    def preflight_extra(self, run: TersRun) -> Iterator[CompositeStep]:
        """模式自己的只读前置检查（光学轴状态等），返回拒绝原因列表。默认没有。"""
        return []
        yield  # noqa: B901 — makes this a generator

    def attestation_lines(self, run: TersRun) -> list[str]:
        return []

    def satellite_devices(self, run: TersRun) -> list[str]:
        """中止时要一起停的卫星设备（光谱仪之外）。"""
        return []

    def product_arrays(self, run: TersRun) -> dict:
        return {}

    def dry_plan(self, run: TersRun) -> None:
        """dry_run（不动硬件）结束前把计划摘要写进 ``run.plan``。"""

    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:  # pragma: no cover
        return iter(())

    # -- driver --
    def _drive(self, run: TersRun) -> Iterator[CompositeStep]:
        needs = self.needs(run)
        ok = yield from run.preflight(needs)
        if not ok:
            return
        if not run.attest_laser():
            return
        if run.dry_run and not run.dry_motion:
            if needs.tip == "tunneling":
                tr = run.tip_ready(label="dry_run", feedback=True)
                run.plan["tip_ready_now"] = tr.as_meta()
            if needs.spectrometer:
                acq = yield from run.acquire("dry_run", "dry_run")
                run.plan["acquire_check"] = {"status": acq.status, "error": acq.error}
                if acq.status != "dry_run":
                    run.stop("refused", _tr_lang(f"AcquireSpectrum dry_run 不通过：{acq.error}",
                                                 f"AcquireSpectrum dry_run failed: {acq.error}"))
                    return
            self.dry_plan(run)
            return
        run.live.open_status()
        if not run.dry_run and needs.spectrometer:
            ok = yield from run.configure_spectrometer(trigger_response=self.TRIGGER_RESPONSE)
            if not ok:
                return
            ok = yield from run.dark_frame()
            if not ok:
                return
        yield from self.ters_plan(run)

    def run_composite(self, context, params: dict) -> SkillResult:
        run = TersRun(self, context, params)
        try:
            run.bands = ta.parse_bands(str(run.p.get("bands_cm1") or run.p.get("band_cm1") or ""))
            run.baselines = ta.parse_baselines(str(run.p.get("baseline_cm1") or ""))
        except ValueError as exc:
            return SkillResult(skill_name=run.name, success=False, error=str(exc),
                               data={"status": "refused", "run_id": run.run_id})
        try:
            refusals = list(self.prepare(run) or [])
        except (ValueError, KeyError, TypeError) as exc:
            refusals = [f"{type(exc).__name__}: {exc}"]
        if refusals:
            run.stop("refused", "；".join(refusals))
            return run.result()
        executor = GraphExecutor(composite_name=run.name, context=run.recorder,
                                 on_step_result=self.on_step_result,
                                 on_step_failed=self.on_step_failed)
        run.executor = executor
        self._executor = executor
        gen = self._drive(run)
        interrupted: BaseException | None = None
        try:
            executor.run_plan(gen)
        except BaseException as exc:  # noqa: BLE001 — classified below
            from mast.skills.composite.graph_executor import _is_graph_interrupt

            if _is_graph_interrupt(exc):
                interrupted = exc
            elif isinstance(exc, InterlockViolation):
                run.stop("needs_operator", _tr_lang(f"内部联锁拒绝：{exc}",
                                                    f"Internal interlock refused: {exc}"))
            elif isinstance(exc, Exception):
                logger.exception("[%s] run failed", run.name)
                run.stop("needs_operator", f"{type(exc).__name__}: {exc}")
            else:
                raise
        finally:
            try:
                gen.close()
            except Exception:  # noqa: BLE001
                logger.debug("generator close raised", exc_info=True)
            if interrupted is None:
                run.finalize()
        if interrupted is not None:
            raise interrupted
        if not executor.progress.aborted:
            executor.clear_sidecar()
        return run.result()


def ters_metadata(*, name: str, description: str, parameters: list[ParameterSpec],
                  tags: list[str], duration_s: float) -> SkillMetadata:
    """所有 TERS 模式共用的 metadata 形状：COMPOSITE / CONFIRM / composition_level 3。"""
    return SkillMetadata(
        name=name, version="1.0.0", category=SkillCategory.COMPOSITE,
        safety_level=SafetyLevel.CONFIRM, description=description,
        parameters=parameters, estimated_duration_s=duration_s, composition_level=3,
        tags=[*tags, "composite", "optional-hardware"],
    )
