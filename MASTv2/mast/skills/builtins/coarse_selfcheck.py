"""CoarseMotionSelfCheck — everything about coarse motion that only the rig knows.

WHY THIS EXISTS
===============
The coarse-motion subsystem shipped with a set of numbers and assumptions that
**cannot be checked on a development machine**, because they are properties of a
particular instrument, a particular gauge, and a particular controller. Some of
them are safety-relevant, and at least one of them is a GUESS I made from a
library signature (see ``freq_amp.order_unverified`` below).

Over a remote link, asking those questions one at a time is slow and easy to get
half-done. This is one read-only call that returns all of them, plus a ``todo``
list naming exactly what is still unknown and what to do about it.

STRICTLY READ-ONLY
==================
No motor command, no setpoint change, no feedback toggle. Every call it makes is
a getter that MAST already issues during ordinary operation. It is safe to run
with the tip engaged, mid-scan, at any pressure. If that ever stops being true,
this skill has been broken.

HOW TO READ THE OUTPUT
======================
``ready`` is deliberately conservative: it is True only when every safety-
relevant unknown has been resolved. A False ``ready`` with an empty ``blocking``
list is impossible by construction — if it is not ready, it says why.

``todo`` entries are ordered by what blocks what. Work top-down.
"""

from __future__ import annotations

import logging
from typing import Any

from mast.i18n import tr
from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill

logger = logging.getLogger(__name__)


class CoarseMotionSelfCheck(BaseSkill):
    """Read-only survey of everything the coarse-motion subsystem depends on."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CoarseMotionSelfCheck",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("粗动子系统**只读**自检:真空计(型号/量程/当前读数/互锁裁决)、"
                "粗动驱动电压(声明值 + 实际读回)、步进计数器是否支持、"
                "qPlus 振幅通道、lock-in 索引、退针方向配置、粗动大地图状态、"
                "以及本机的温度。\n"
                "**不发任何移动命令、不改任何设定值** —— 进针状态下、扫描过程中都可以跑。\n"
                "返回的 todo 列表就是「还差哪些只有真机能回答的数」。",
                "**Read-only** self-check of the coarse subsystem: vacuum gauge (model / range / current reading / interlock verdict), coarse drive voltage (declared value + actual readback), whether a step counter is supported, qPlus amplitude channel, lock-in index, withdraw direction configuration, coarse large-map state, and this machine's temperature.\n**Issues no move command and changes no setting** - can be run while engaged and during a scan.\nThe returned todo list is 'which numbers are still missing that only the real machine can answer'.")
            ),
            parameters=[
                ParameterSpec(
                    name="probe_step_counter", type="bool", required=False,
                    default=True,
                    description=(
                        tr("是否试读一次步进计数器(仅 Attocube ANC150 支持)。"
                        "读失败是**信息**不是故障 —— 它正是要确认的事。",
                        "Whether to try reading the step counter once (only the Attocube ANC150 supports it). A failed read is **information**, not a fault - it is exactly what is being confirmed."))),
            ],
            estimated_duration_s=4.0,
            composition_level=0,
            tags=["coarse", "motor", "vacuum", "selfcheck", "read", "commissioning"],
        )

    # ── probes ──────────────────────────────────────────────────────────

    def _vacuum(self) -> dict[str, Any]:
        from mast.core import vacuum_interlock as vac

        out: dict[str, Any] = {}
        sample = vac.current_sample()
        verdict = vac.check()
        cfg = vac._config()
        out["interlock_mode"] = cfg["mode"]
        out["permit_max_pa"] = cfg["max_pa"]
        out["gauge_band_pa"] = [cfg["min_pa"], cfg["full_scale_pa"]]
        out["corona_zone_pa"] = list(vac.CORONA_ZONE_PA)
        out["allow"] = verdict.allow
        out["reason"] = verdict.reason
        out["source"] = verdict.source
        if sample is None:
            out["gauge"] = None
            out["note"] = (tr("环境监控里没有真空读数 —— 要么没装真空计,要么 "
                           "EnvironmentMonitor 没起来。查 environment_sensors.json "
                           "与 /api/environment/readings。",
                           "There is no vacuum reading in the environment monitor - either no vacuum gauge is installed, or EnvironmentMonitor did not start. Check environment_sensors.json and /api/environment/readings."))
        else:
            out["gauge"] = {
                "sensor_name": sample.sensor_name,
                "sensor_class": sample.sensor_class,
                "raw_value": sample.value,
                "raw_unit": sample.unit,
                "status": sample.status,
                "age_s": sample.age_s(),
                "trusted_class": sample.sensor_class in vac.REAL_GAUGE_CLASSES,
                "is_placeholder": sample.sensor_class in vac.PLACEHOLDER_CLASSES,
            }
        problem = vac.gauge_config_problem(
            min_pa=cfg["min_pa"], full_scale_pa=cfg["full_scale_pa"],
            max_pa=cfg["max_pa"])
        if problem:
            out["config_problem"] = problem
        att = vac.get_attestation()
        if att is not None:
            out["attestation"] = {"reason": att.reason, "expired": att.expired(),
                                  "remaining_h": round(att.remaining_s() / 3600, 2)}
        return out

    def _drive(self, context, calls: list) -> dict[str, Any]:
        from mast.core import coarse_drive
        from mast.skills.builtins.motor import _parse_freq_amp

        out: dict[str, Any] = {
            "declared_max_amplitude_v": coarse_drive.max_amplitude_v(),
            "declared_expected_frequency_hz": coarse_drive.expected_frequency_hz(),
            "absolute_ceiling_v": coarse_drive.ABSOLUTE_MAX_AMPLITUDE_V,
        }
        rec = context.safe_call("Motor_FreqAmpGet", 0)
        calls.append(rec)
        if rec.error:
            out["readback_error"] = rec.error
            out["readback_ok"] = False
            out["note"] = (tr("读不到当前驱动值 —— 粗动会被拒绝(读不到 ≠ 没问题)。"
                           "若该控制器不支持这条命令,需要另想核对办法。",
                           "The current drive value cannot be read - coarse motion will be refused (unreadable is not the same as fine). If this controller does not support the command, another way of checking is needed."))
            return out
        freq, amp = _parse_freq_amp(rec.return_value)
        out["raw_reply"] = str(rec.return_value)[:200]
        out["parsed_as"] = {"frequency_hz": freq, "amplitude_v": amp}
        # 协议 getter 的返回顺序不必等同于 setter 的参数顺序，不能按数值大小推断字段。
        # expected_frequency_hz 是调用方提供的参考读数，用于与解析结果明确比较。
        exp_f = out.get("declared_expected_frequency_hz")
        got_f = (out.get("parsed_as") or {}).get("frequency_hz")
        got_a = (out.get("parsed_as") or {}).get("amplitude_v")
        if isinstance(exp_f, (int, float)) and isinstance(got_f, (int, float)):
            tol = max(1.0, abs(float(exp_f)) * 0.05)
            if abs(float(got_f) - float(exp_f)) <= tol:
                out["order_unverified"] = False
                out["order_hint"] = (
                    tr(f"取值顺序**已验证**:回包第一个数 {got_f:g} 与用户声明的"
                    f"驱动频率 {exp_f:g} Hz 相符,所以 (频率, 幅度) 的顺序是对的。",
                    f"Value order **verified**: the first number in the reply {got_f:g} matches the user-declared drive frequency {exp_f:g} Hz, so the (frequency, amplitude) order is correct."))
            elif (isinstance(got_a, (int, float))
                  and abs(float(got_a) - float(exp_f)) <= tol):
                # 反过来才对上 —— 这是「顺序反了」的**正面证据**,不是猜测。
                out["order_unverified"] = True
                out["order_hint"] = (
                    tr(f"⚠️ 取值顺序**很可能反了**:回包第二个数 {got_a:g} 才与用户"
                    f"声明的驱动频率 {exp_f:g} Hz 相符,而第一个数是 {got_f:g}。"
                    "请改 motor._parse_freq_amp 的取值顺序,在那之前不要相信"
                    "readback_ok —— 它比的是错的那个数。",
                    f"WARNING: the value order is **very likely reversed**: it is the second number in the reply {got_a:g} that matches the user-declared drive frequency {exp_f:g} Hz, while the first number is {got_f:g}. Change the value order in motor._parse_freq_amp, and until then do not trust readback_ok - it compares the wrong number."))
            else:
                out["order_unverified"] = True
                out["order_hint"] = (
                    tr(f"回包两个数({got_f:g}, {got_a:g})都对不上声明的驱动频率"
                    f"{exp_f:g} Hz。可能是用户改过面板设置,也可能解析本身有问题;"
                    "请重新核对面板实值再判断。",
                    f"Neither number in the reply ({got_f:g}, {got_a:g}) matches the declared drive frequency {exp_f:g} Hz. The user may have changed the panel settings, or the parsing itself may be wrong; re-check the actual panel values before judging."))
        else:
            out["order_unverified"] = True
            out["order_hint"] = (
                tr("判断哪个是频率哪个是幅度:粗动频率通常是数百~数千 Hz(≫100),"
                "驱动幅度通常是几十~几百 V(≤400)。若 parsed_as 里两个数明显对调"
                "(例如 frequency_hz=120、amplitude_v=1000),就说明 "
                "motor._parse_freq_amp 的取值顺序要反过来。"
                "**更省事的办法**:在【高级】页把 expected_frequency_hz 一并声明"
                "(从 Nanonis Motor Control 面板读),这条就会自动判定。",
                "To tell which is the frequency and which the amplitude: the coarse frequency is usually hundreds to thousands of Hz (much greater than 100), and the drive amplitude is usually tens to hundreds of V (<=400). If the two numbers in parsed_as are clearly swapped (e.g. frequency_hz=120, amplitude_v=1000), the value order in motor._parse_freq_amp must be reversed. **An easier way**: declare expected_frequency_hz on the [Advanced] page (read from the Nanonis Motor Control panel), and this is then decided automatically."))
        ok, why = coarse_drive.readback_matches(amp, freq)
        out["readback_ok"] = ok
        out["readback_note"] = why
        return out

    def _step_counter(self, context, calls: list, probe: bool) -> dict[str, Any]:
        if not probe:
            return {"probed": False}
        rec = context.safe_call("Motor_StepCounterGet", 0, 0, 0)
        calls.append(rec)
        if rec.error:
            return {"probed": True, "supported": False, "error": rec.error,
                    "note": (tr("本控制器不支持步进计数器(仅 Attocube ANC150 支持)。"
                             "换区后的步数对账将如实报告 unavailable —— "
                             "这是正常的,不是缺陷。",
                             "This controller does not support a step counter (only the Attocube ANC150 does). The step reconciliation after a change of area will honestly report unavailable - this is normal, not a defect."))}
        return {"probed": True, "supported": True,
                "raw_reply": str(rec.return_value)[:200],
                "note": tr("支持 —— 换区后可以对账实际走了多少步。", "Supported - after a change of area the number of steps actually taken can be reconciled.")}

    def _closed_loop(self, context, calls: list) -> dict[str, Any]:
        """Does this controller report an absolute coarse position?

        If it does, the open-loop odometer has a cross-check available and the
        coarse map's uncertainty blobs could eventually be replaced by real
        coordinates. Nothing depends on it today.

        Asked of the configured STM coarse group (``nanonis_motor_groups.json``,
        0 when absent) — on a multi-group PMD4 the answer is per group."""
        from mast.instruments.motor_groups import MotorGroupConfigError, stm_coarse_group

        try:
            group = stm_coarse_group()
        except MotorGroupConfigError as exc:
            return {"supported": None, "error": str(exc),
                    "note": tr("电机组配置不可用,没有探测(不是「不支持」)。", "Motor group configuration unavailable, not probed (this is not 'not supported').")}
        rec = context.safe_call("Motor_PosGet", group, 500)
        calls.append(rec)
        if rec.error:
            return {"supported": False, "error": rec.error, "group": group,
                    "note": tr("无闭环位置读回 —— 里程表只能靠步数累加(设计已假定如此)。", "No closed-loop position readback - the odometer can only accumulate step counts (the design already assumes this).")}
        return {"supported": True, "raw_reply": str(rec.return_value)[:200], "group": group,
                "note": (tr("有闭环位置读回 —— 可作为开环里程表的交叉校验。"
                         "当前设计**没有**使用它。",
                         "A closed-loop position readback exists - it can serve as a cross-check on the open-loop odometer. The current design does **not** use it."))}

    def _qplus(self, context, calls: list) -> dict[str, Any]:
        from mast.core import instrument_profile as ip
        from mast.skills.builtins.qplus_amplitude import find_amplitude_signal

        out: dict[str, Any] = {
            "baseline": ip.get_config("qplus_amplitude_baseline", None),
            "configured_index": ip.get_config("qplus_amplitude_signal_index", -1),
        }
        found = find_amplitude_signal(context)
        if found is None:
            out["channel"] = None
            out["note"] = (tr("信号表里没有振荡振幅通道 —— 这台机器很可能没有 qPlus。"
                           "这不是故障:撞针判据会退到只用电流,"
                           "换区的脱离确认也只看电流。",
                           "There is no oscillation amplitude channel in the signal list - this machine most likely has no qPlus. This is not a fault: the tip-crash criterion falls back to the current only, and the disengage confirmation after a change of area also looks only at the current."))
            return out
        out["channel"] = {"index": found[0], "name": found[1]}
        out["note"] = (tr("有 qPlus 振幅通道。基线会在**确认脱离之后**自动记录"
                       "(RetractForSampleChange 完成时 / RelocateCoarseXY 清障后)。",
                       "A qPlus amplitude channel exists. The baseline is recorded automatically **after disengage is confirmed** (when RetractForSampleChange completes / after the obstacle clearing of RelocateCoarseXY).")
                       if out["baseline"] is None else
                       tr("有 qPlus 且基线已记录 —— 撞针与脱离判据都可用。", "qPlus is present and the baseline is recorded - both the tip-crash and disengage criteria are usable."))
        return out

    def _signals(self, context, calls: list) -> dict[str, Any]:
        """The whole signal table — the ground truth several hints match against."""
        rec = context.safe_call("Signals_NamesGet")
        calls.append(rec)
        if rec.error:
            return {"error": rec.error}
        from mast.skills.builtins.qplus_amplitude import _signal_names

        names = _signal_names(rec)
        return {"count": len(names),
                "names": names[:64],
                "note": tr("对照它确认 lock-in / qPlus / 电流通道的索引是否配对。", "Compare against it to confirm that the lock-in / qPlus / current channel indices are paired correctly.")}

    def _profile(self) -> dict[str, Any]:
        from mast.core import instrument_profile as ip

        keys = ("retract_motor_dir", "z_extend_sign", "xy_coarse_motion",
                "xy_prewithdraw_steps", "xy_move_chunk_steps",
                "xy_site_spacing_steps", "xy_axis_step_budget",
                "xy_step_uncertainty_frac", "xy_motor_step_m",
                "lockin_signal_index", "z_recede_min_nm")
        out = {k: ip.get_config(k, None) for k in keys}
        out["_note"] = (
            tr("xy_prewithdraw_steps / xy_site_spacing_steps 的默认值是**起点不是测量值**。"
            "标定办法:移一次 → 扫一张图 → 看地貌是不是**完全**换了。没换 = 位移不足以"
            "跳出压电量程(±1.5 µm),要加大 xy_site_spacing_steps。"
            "低温下步长会显著变小,两组值要分别记。"
            # 2026-08-18:这一条也是「起点不是测量值」,而且此前没人说过。
            "xy_step_uncertainty_frac(出厂 0.3)同样**未经实机标定** —— "
            "它是**单步**步长的相对散布,地图上的模糊半径按 frac×√(总步数) 画。"
            "它只影响两件事:blob 画多大、以及新落点被要求离旧站点多远;"
            "**它不是安全限位**。要标它:同一方向重复走 N 步若干次,量落点的散布。",
            "The defaults of xy_prewithdraw_steps / xy_site_spacing_steps are **a starting point, not measured values**. To calibrate: move once -> scan an image -> see whether the topography changed **completely**. If not, the displacement is not enough to jump out of the piezo range (±1.5 µm), so increase xy_site_spacing_steps. At low temperature the step size becomes markedly smaller, so record the two sets of values separately. xy_step_uncertainty_frac (factory 0.3) is likewise **not calibrated on the real machine** - it is the relative scatter of a **single** step, and the blur radius on the map is drawn as frac×√(total steps). It affects only two things: how large the blob is drawn, and how far a new site is required to be from old sites; **it is not a safety limit**. To calibrate it: walk N steps in the same direction several times and measure the scatter of the landing points."))
        return out

    def _coarse_map(self) -> dict[str, Any]:
        from mast.core import coarse_map_provider

        rows, cfg = coarse_map_provider.markers_and_config()
        if rows is None:
            return {"available": False,
                    "note": (tr("读不到粗动记录(未接 provider 或无活动实验)。"
                             "换区仍可执行,但落点复核会降级为「判断不了,放行」。",
                             "Coarse records cannot be read (no provider connected or no active experiment). A change of area can still run, but the landing-site check degrades to 'cannot judge, let it pass'."))}
        from mast.io.coarse_map import build_coarse_map

        m = build_coarse_map(rows, cfg)
        return {"available": True,
                "sites": len(m.sites),
                "current_index": m.current.index,
                "position_known": m.current.position_known,
                "budget_used_steps": m.budget_used,
                "suggestion": m.suggestion.as_dict() if m.suggestion else None,
                "note": m.note}

    def _temperature(self) -> dict[str, Any]:
        from mast.core import coarse_map_provider

        t = coarse_map_provider.temperature_k()
        if t is None:
            return {"value_k": None,
                    "note": (tr("读不到温度 —— 粗动记录里不会带温度。"
                             "这只影响可比性(同样步数在 4 K 和 300 K 走的距离差几倍),"
                             "不阻断任何操作。",
                             "Temperature cannot be read - coarse records will not carry a temperature. This only affects comparability (the same number of steps travels several times farther at 4 K than at 300 K), and blocks no operation."))}
        return {"value_k": t}

    def _registry(self) -> dict[str, Any]:
        """Are the new skills actually present in THIS build?

        A frozen build enumerates nothing via pkgutil, so a module the package
        __init__ never imports is simply absent — silently, with every test on
        the dev machine green. 141 skills were missing this way once."""
        want = ("RelocateCoarseXY", "GetChamberPressure", "GetMotorFreqAmp",
                "CoarseMotionSelfCheck", "CheckTipCrashByAmplitude",
                "ReadTipOscillationAmplitude", "RetractForSampleChange",
                "ApproachTip", "MotorMove", "StopMotor")
        try:
            from mast.core.registry import SkillRegistry

            reg = SkillRegistry()
            reg.discover("mast.skills.builtins", "mast.skills.composite")
            missing = [n for n in want if not reg.has(n)]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {
            "ok": not missing, "missing": missing, "checked": len(want),
            "note": ("" if not missing else
                     tr("这些技能在本版里不存在 —— 多半是打包时包 __init__ 没 import 到"
                     "它们所在的模块(冻结环境不做 walk_packages),失败是**静默的**",
                     "These skills do not exist in this build - most likely the packaging did not import the modules they live in through __init__ (frozen environments do not walk_packages), and the failure is **silent**")),
        }

    # ── driver ──────────────────────────────────────────────────────────

    def execute(self, context, params: dict) -> SkillResult:
        calls: list = []
        data: dict[str, Any] = {}
        todo: list[str] = []
        blocking: list[str] = []

        data["registry"] = self._registry()
        if not data["registry"].get("ok"):
            blocking.append(
                tr(f"打包缺失技能:{data['registry'].get('missing')} —— "
                "先修 skills/builtins/__init__.py 与 composite/__init__.py,"
                "**别继续验收**(缺席是静默的)。",
                f"Skills missing from the package: {data['registry'].get('missing')} - first fix skills/builtins/__init__.py and composite/__init__.py, **do not continue the acceptance** (absence is silent)."))

        data["vacuum"] = self._vacuum()
        v = data["vacuum"]
        if v.get("gauge") is None:
            blocking.append(tr("没有真空读数 —— 粗动会被拒绝。确认真空计接线与 "
                            "environment_sensors.json,或由用户签署。",
                            "No vacuum reading - coarse motion will be refused. Check the vacuum gauge wiring and environment_sensors.json, or have the user sign."))
        elif v["gauge"].get("is_placeholder"):
            blocking.append(tr("真空计是占位实现(恒报 0.0)—— 粗动会被拒绝。这是对的。", "The vacuum gauge is a placeholder implementation (always reports 0.0) - coarse motion will be refused. This is correct."))
        elif not v["gauge"].get("trusted_class"):
            blocking.append(
                tr(f"真空计类 {v['gauge'].get('sensor_class')} 不在 REAL_GAUGE_CLASSES 里 —— "
                "加类名 + 在设置里填这只规的量程上下限。",
                f"Vacuum gauge class {v['gauge'].get('sensor_class')} is not in REAL_GAUGE_CLASSES - add the class name + fill in this gauge's range limits in settings."))
        if v.get("config_problem"):
            blocking.append(tr("真空计量程配置:", "Vacuum gauge range configuration:") + v["config_problem"])
        if v.get("gauge") and v["gauge"].get("trusted_class"):
            todo.append(tr("核对设置里的量程上下限与这只规铭牌一致(判据只依赖这两个数)。", "Check that the range limits in settings match this gauge's nameplate (the criterion depends only on these two numbers)."))

        data["coarse_drive"] = self._drive(context, calls)
        d = data["coarse_drive"]
        if d.get("declared_max_amplitude_v") is None:
            blocking.append(
                tr("**本机粗动耐压未声明** —— 一切驱动写入与粗动移动都会被拒绝。"
                "这是刻意的:控制器能出的电压 ≠ 这台机器的叠堆能承受的电压,"
                "而没有任何读数能告诉你是哪一种。请在【高级】页填(需 admin PIN)。",
                "**This machine's coarse withstand voltage is not declared** - every drive write and coarse move will be refused. This is deliberate: the voltage the controller can output ≠ the voltage this machine's stack can withstand, and no reading can tell you which it is. Fill it in on the [Advanced] page (needs the admin PIN)."))
        if d.get("order_unverified"):
            todo.append(
                tr("**确认 Motor_FreqAmpGet 的回包顺序**(见 coarse_drive.parsed_as / "
                "order_hint)。这是从 setter 的签名推的,没被观测过;若顺序相反,"
                "移动前的驱动电压核对比的是错的那个数。",
                "**Confirm the reply order of Motor_FreqAmpGet** (see coarse_drive.parsed_as / order_hint). It was inferred from the setter's signature and has never been observed; if the order is reversed, the pre-move drive-voltage check compares the wrong number."))
        if d.get("readback_ok") is False and d.get("declared_max_amplitude_v") is not None:
            blocking.append(tr("驱动读回核对不通过:", "Drive readback check failed:") + str(d.get("readback_note")))

        data["step_counter"] = self._step_counter(
            context, calls, bool(params.get("probe_step_counter", True)))
        data["closed_loop"] = self._closed_loop(context, calls)
        data["qplus"] = self._qplus(context, calls)
        if data["qplus"].get("channel") and data["qplus"].get("baseline") is None:
            todo.append(tr("有 qPlus 但还没有自由振荡基线 —— 跑一次 "
                        "RetractForSampleChange,或做一次 RelocateCoarseXY 的清障相位;"
                        "基线会在确认脱离后自动记录。",
                        "qPlus is present but there is no free-oscillation baseline yet - run RetractForSampleChange once, or the obstacle-clearing phase of RelocateCoarseXY; the baseline is recorded automatically after disengage is confirmed."))
        data["signals"] = self._signals(context, calls)
        data["instrument_profile"] = self._profile()
        data["coarse_map"] = self._coarse_map()
        data["temperature"] = self._temperature()

        prof = data["instrument_profile"]
        if prof.get("lockin_signal_index") is None:
            todo.append(tr("lock-in 信号索引未配置 —— dI/dV 趋势播报与进针标定都不会发生"
                        "(不阻断进针)。对照 signals.names 填。",
                        "lock-in signal index is not configured - neither the dI/dV trend report nor the engage calibration will happen (engage is not blocked). Fill it in according to signals.names."))
        if prof.get("xy_motor_step_m") is None:
            todo.append(tr("xy_motor_step_m 未标定 —— 地图上不会显示「≈多少 µm」的注释。"
                        "纯注释,不影响任何判断。",
                        "xy_motor_step_m is not calibrated - the map will not show the '≈ how many µm' annotation. Pure annotation, affects no judgement."))
        todo.append(
            tr("**方向码验证**:x+/x-/y+/y- 对应 Nanonis 的 0/1/2/3,"
            "但哪个码对应样品台的哪个物理方向从未验证过。"
            "办法:退针状态下做一次小幅 RelocateCoarseXY,再扫一张图,"
            "看地貌朝哪个方向移动 —— 记进本机文档。方向搞反不会撞针"
            "(清障已完成),但里程表会记反,「不回原地」的判据会失效。",
            "**Direction-code verification**: x+/x-/y+/y- correspond to Nanonis 0/1/2/3, but which code corresponds to which physical direction of the sample stage has never been verified. How: with the tip withdrawn, do a small RelocateCoarseXY, then scan an image and see which way the topography moved - and record it in this machine's documentation. A reversed direction will not crash the tip (obstacle clearing is already done), but the odometer would record it reversed, and the 'do not return to the same place' criterion would fail."))
        todo.append(
            tr("**xy_prewithdraw_steps / xy_site_spacing_steps 实机标定** —— "
            "见 instrument_profile._note。",
            "**Real-machine calibration of xy_prewithdraw_steps / xy_site_spacing_steps** - see instrument_profile._note."))

        data["todo"] = todo
        data["blocking"] = blocking
        data["ready"] = not blocking
        data["summary"] = (
            tr("粗动子系统就绪", "Coarse subsystem ready") if not blocking
            else tr(f"粗动子系统**未就绪**:{len(blocking)} 项阻断", f"Coarse subsystem **not ready**: {len(blocking)} blocking item(s)"))
        return SkillResult(skill_name="CoarseMotionSelfCheck", success=True,
                           data=data, nanonis_calls=calls)


__all__ = ["CoarseMotionSelfCheck"]
