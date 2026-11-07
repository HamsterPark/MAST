"""Lock-in 参数组与解调侧相位对齐。

ListLockInPresets 只读展示参数组、来源与缺项；ApplyLockInPreset 按组名下发，
不让模型重写数值，并在写后回读比较。AutoPhase 读取 X/Y，使用 atan2 计算角度，
只调整解调侧 phase，不改变调制侧 phase。
设备支持情况由调用结果确认，不能根据 GUI 或配置字段推断命令一定可写。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from mast.core.lockin_presets import (
    PRESET_DIDV,
    PresetRejected,
    list_presets,
    resolve,
)
from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill
# 「用完即关」的那一份实现住在 _preflight 里(开跑前那半也在那儿)。同一个动作两份
# 代码,被修好的永远只有一份 —— 而另一份不报错,它会给出一个看着正常的错答案。
from mast.skills.composite._preflight import close_modulation
from mast.skills.verify import values_match
from mast.i18n import tr

logger = logging.getLogger(__name__)


# 参数组键名与 GetLockInConfig 回读键名使用显式映射。
# 例如写入 amplitude_v 对应回读 amplitude，不能按写入键名猜测回包。
# 测试应与真实回读实现对齐，不能用自造同名字典证明映射。
# 不以多个候选键兜底隐藏接口漂移；键名改变应同步更新契约。
_READBACK_KEYS: "dict[str, str]" = {
    "frequency_hz": "frequency_hz",
    "amplitude_v": "amplitude",
}


def _scalar(rv) -> "float | None":
    """Nanonis ``(header, body, [vals])`` 里的第一个标量。"""
    if isinstance(rv, (list, tuple)) and len(rv) > 2:
        inner = rv[2]
        if isinstance(inner, (int, float)) and not isinstance(inner, bool):
            return float(inner)
        if isinstance(inner, (list, tuple)) and inner:
            try:
                return float(inner[0])
            except (TypeError, ValueError):
                return None
    return None


class ListLockInPresets(BaseSkill):
    """只读:当前 lock-in 参数组解析成什么样。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ListLockInPresets",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("列出 lock-in 常用参数组:每个值是多少、来自哪个档案键、"
                "哪些键用户还没填(没填的**不会下发**)。"
                "调制侧相位永远不在组里 —— 有的固件不接受写它。", "List the common lock-in parameter groups: what each value is, which profile key it comes from, "
"and which keys the user has not filled in yet (unfilled ones are **not sent**). "
"The modulation-side phase is never in a group -- some firmware does not accept writing it.")),
            parameters=[],
            estimated_duration_s=0.2,
            composition_level=0,
            tags=["lockin", "preset", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        return SkillResult(skill_name="ListLockInPresets", success=True,
                           data={"presets": list_presets()})


class ApplyLockInPreset(BaseSkill):
    """按组名下发 lock-in 调制参数。零数值参数。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ApplyLockInPreset",
            version="1.0.0",
            category=SkillCategory.WRITE,
            # 与 ApplyZCtrlPreset 对齐。
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("按**参数组名**设置 lock-in 调制(频率 / 幅度),数值由代码从用户"
                "维护的仪器档案里取出,你不需要也不应该自己写任何数字。\n\n"
                f"组名:`{PRESET_DIDV}`(dI/dV 常用组)。先用 ListLockInPresets 看"
                "里面有什么、哪些键还没配。\n\n"
                "**不会下发调制侧相位** —— 有的设备 Modulate 区没有该字段,固件恒拒写"
                "(写同样的值也拒)。要调相位请用 AutoPhase 或 ConfigureLockInDemod,"
                "它们写的是解调侧 Ref. Phase。", "Set the lock-in modulation (frequency / amplitude) by **parameter group name**; the values are taken by code from the user-"
"maintained instrument profile, and you do not need to, and should not, write any number yourself.\n\n"
f"Group name: `{PRESET_DIDV}` (the common dI/dV group). First use ListLockInPresets to see "
"what is in it and which keys are not yet configured.\n\n"
"**The modulation-side phase is not sent** -- on some devices the Modulate section has no such field, and the firmware always rejects writing it "
"(even with the same value). To adjust the phase use AutoPhase or ConfigureLockInDemod, "
"which write the demodulator-side Ref. Phase.")),
            parameters=[
                ParameterSpec(
                    name="preset", type="str", required=False,
                    default=PRESET_DIDV,
                    description=tr(f"参数组名。目前只有 '{PRESET_DIDV}'。", f"Parameter group name. Currently only '{PRESET_DIDV}'.")),
                ParameterSpec(
                    name="mod_on", type="bool", required=False, default=True,
                    description=tr("下发后是否打开调制。", "Whether to turn the modulation on after sending.")),
            ],
            estimated_duration_s=3.0,
            composition_level=1,
            tags=["lockin", "preset", "write", "readback"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = str(params.get("preset") or PRESET_DIDV)
        try:
            preset = resolve(name)
        except PresetRejected as exc:
            return SkillResult(skill_name="ApplyLockInPreset", success=False,
                               error=str(exc))
        if not preset.usable:
            # 档案一个值都没有 ⇒ 拒绝。静默不下发是假成功。
            return SkillResult(skill_name="ApplyLockInPreset", success=False,
                               error=preset.why(), data=preset.as_dict())

        call_params = preset.skill_params(mod_on=bool(params.get("mod_on", True)))
        assert "phase_deg" not in call_params  # 见类 docstring
        res = context.run("ConfigureLockIn", call_params)
        ok = bool(getattr(res, "success", False))
        data: "dict[str, Any]" = preset.as_dict()
        data["applied"] = call_params
        data["configure_result"] = getattr(res, "data", None) or {}
        if not ok:
            return SkillResult(
                skill_name="ApplyLockInPreset", success=False,
                error=tr(f"下发 lock-in 参数组失败:{getattr(res, 'error', 'unknown')}", f"Failed to send the lock-in parameter group: {getattr(res, 'error', 'unknown')}"),
                data=data)

        # 写后回读比对(第 4 层防线)。读不回来不算成功 —— 「写进去了」和
        # 「我们看见它在里面」是两句话。
        rb = context.run("GetLockInConfig", {})
        rb_data = getattr(rb, "data", None) or {}
        data["readback"] = rb_data
        mismatches = []
        if not getattr(rb, "success", False):
            # 回读命令本身没跑成 ≠ 硬件里的值不对。说清楚是哪一种。
            mismatches.append(tr(f"回读命令失败:{getattr(rb, 'error', 'unknown')}", f"Readback command failed: {getattr(rb, 'error', 'unknown')}"))
        for param, want in preset.values.items():
            key = _READBACK_KEYS.get(param)
            if key is None:
                # 组里长出了新参数而这张表没跟上 —— 代码缺口,不是硬件问题。
                # 静默跳过会让一个没被核对过的值挂着「已回读验证」的牌子。
                mismatches.append(tr(f"{param}: 没有回读映射(代码缺口)", f"{param}: no readback mapping (code gap)"))
                continue
            got = rb_data.get(key)
            if got is None:
                mismatches.append(tr(f"{param}: 读不回来(回包里没有 {key})", f"{param}: cannot be read back (no {key} in the reply)"))
                continue
            # 比较规则用仓里那一份(``values_match``,rel_tol 1e-3),不自己写。
            # ``ApplyZCtrlPreset`` 里那句注释说得对:第二处比较规则会漂 —— 而这次
            # 的教训是它不只会漂,它会**把成功报成失败**。顺带白拿两样:比不了/NaN
            # 的三态判定,和一句带比值的诊断(「请求 X, 读回 Y(比值 N)」),
            # 差多少是写出来的而不是留给读的人再算一遍。
            #
            # float32 在这里不是问题:硬件回的是量化值(请求 0.02 → 读回
            # 0.019999999552965164,相对差 2e-8),而 1e-3 的相对容差比 float32 的
            # 相对精度(~1.2e-7)宽四个数量级。**用相等比较会把每一次成功写入都
            # 判成失败**,这条钉子在测试里立着。
            ok, detail = values_match(want, got)
            if not ok:
                mismatches.append(f"{param}: {detail}")
        data["readback_verified"] = not mismatches
        if mismatches:
            return SkillResult(
                skill_name="ApplyLockInPreset", success=False,
                error=(tr("下发后回读不一致:", "Readback after sending is inconsistent: ") + ";".join(mismatches)
                       + tr("。**不要按已设置继续**。", ". **Do not continue as if it were set**.")),
                data=data)
        return SkillResult(skill_name="ApplyLockInPreset", success=True, data=data)


class AutoPhase(BaseSkill):
    """读取 X/Y 并用 atan2(Y, X) 计算偏角，只写解调侧相位。
    signal_to_x 将有效信号转到 X 轴；crosstalk_to_y 用退针态的串扰参考对齐。
    X/Y 均在噪声底时拒绝写相位，避免由噪声决定角度。
    正常结束或失败后关闭调制，幅度和频率保持；中止路径不追加设置操作。
    随后若需测量 dI/dV，应重新 ApplyLockInPreset 开启调制。
    此状态变化同时在技能描述与返回值中报告。
    """

    #: 低于这个幅度就认为「没有可用信号」。与电流噪声底同量级(1 pA);
    #: lock-in 读数的单位随机器而异,所以这是**下界守卫**不是标定值 ——
    #: 它拦的是「零信号上算出来的角」,不是「小信号」。
    _MIN_SIGNAL = 1e-12

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AutoPhase",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("自动对齐 lock-in **解调相位**(GUI 上的 Auto 按钮做的事,"
                "但那个按钮没有 TCP 命令,所以这里是读 X/Y → 算角 → 写相位)。\n\n"
                "两种模式:\n"
                "- `signal_to_x`(默认,**隧穿态**用):把 dI/dV 信号转到 X 轴;\n"
                "- `crosstalk_to_y`(**退针态**用):把电容串扰转到 Y 轴,"
                "信号轴自然对齐 X —— 不用进针就能定相位轴。\n\n"
                "取样窗口内平均后再算角;**X/Y 都在噪声底时拒绝**(无信号不给相位角)。"
                "写的是解调侧,**绝不碰调制侧相位**(有的固件恒拒写它)。\n\n"
                "⚠️ **跑完会把 lock-in 调制关回 OFF**(幅度/频率保留)—— 它是这条链的"
                "终端消费者,而调制留着会污染后面每一条电流判据。**要接着测 dI/dV,"
                "请先重新 ApplyLockInPreset 打开调制**:调制关着时 lock-in 通道上"
                "没有信号,而曲线照样画得出来。", "Automatically align the lock-in **demodulation phase** (what the Auto button in the GUI does, "
"but that button has no TCP command, so here it is read X/Y -> compute angle -> write phase).\n\n"
"Two modes:\n"
"- `signal_to_x` (default, for the **tunnelling state**): rotate the dI/dV signal onto the X axis;\n"
"- `crosstalk_to_y` (for the **withdrawn state**): rotate the capacitive crosstalk onto the Y axis, "
"so the signal axis naturally aligns with X -- the phase axis can be fixed without approaching the tip.\n\n"
"The angle is computed after averaging within the sampling window; **refuses when X/Y are both at the noise floor** (no signal, no phase angle). "
"It writes the demodulator side and **never touches the modulation-side phase** (some firmware always rejects writing it).\n\n"
"⚠️ **When done it turns the lock-in modulation back OFF** (amplitude/frequency kept) -- it is the "
"terminal consumer of this chain, and leaving the modulation on would contaminate every later current criterion. **To go on measuring dI/dV, "
"first ApplyLockInPreset again to turn the modulation on**: with the modulation off there is "
"no signal on the lock-in channel, yet a curve can still be drawn.")),
            parameters=[
                ParameterSpec(
                    name="mode", type="str", required=False,
                    default="signal_to_x",
                    allowed_values=["signal_to_x", "crosstalk_to_y"],
                    description=tr("signal_to_x=把信号归 X(隧穿态);"
                                "crosstalk_to_y=把串扰归 Y(退针态)。", "signal_to_x = put the signal on X (tunnelling state); "
"crosstalk_to_y = put the crosstalk on Y (withdrawn state).")),
                ParameterSpec(
                    name="window_s", type="float", required=False, default=3.0,
                    min_value=0.2, max_value=60.0,
                    description=tr("取样窗口秒数 —— 窗口内平均再算角,不用单次快照。", "Sampling window in seconds -- averaged within the window before computing the angle, not a single snapshot.")),
                ParameterSpec(
                    name="demodulator", type="int", required=False, default=1,
                    min_value=1, max_value=8,
                    description=tr("解调器编号(读/写相位用)。", "Demodulator number (used for reading/writing the phase).")),
                ParameterSpec(
                    name="x_signal_index", type="int", required=False,
                    min_value=0, max_value=127,
                    description=(tr("承载解调 X 的 RT 信号索引。留空则取仪器档案 "
                                 "lockin_x_signal_index;两处都没有就**拒绝**"
                                 "(不猜索引 —— 猜错读到的是另一路信号,而算出来的"
                                 "角度看上去一样合理)。", "RT signal index carrying the demodulated X. Empty = taken from the instrument profile "
"lockin_x_signal_index; if neither has it, **refuses** "
"(no guessing the index -- a wrong guess reads another signal, and the computed "
"angle looks equally plausible)."))),
                ParameterSpec(
                    name="y_signal_index", type="int", required=False,
                    min_value=0, max_value=127,
                    description=tr("承载解调 Y 的 RT 信号索引;同上。", "RT signal index carrying the demodulated Y; same as above.")),
            ],
            estimated_duration_s=6.0,
            composition_level=1,
            tags=["lockin", "phase", "auto", "didv", "write", "readback"],
        )

    # 采样节奏。类属性,好让测试缩短窗口而不必假装硬件是瞬时的。
    _poll_interval_s = 0.1

    @staticmethod
    def _signal_indices(params: dict) -> "tuple[int | None, int | None, str]":
        """(X 索引, Y 索引, 拒绝理由)。两处都没配就拒绝,**不猜**。

        猜一个索引的代价不是「读不到」——是读到**另一路信号**,然后算出一个同样
        像模像样的角度写进硬件。本机 lock-in 的 X/Y 走哪两路 RT 信号是接线事实,
        软件观测不到,只能由用户填。
        """
        from mast.core import instrument_profile as ip

        def _pick(name: str, key: str) -> "int | None":
            v = params.get(name)
            if v is None:
                v = ip.get_config(key, None)
            if v in (None, ""):
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        x = _pick("x_signal_index", "lockin_x_signal_index")
        y = _pick("y_signal_index", "lockin_y_signal_index")
        if x is None or y is None:
            miss = [n for n, v in (("X", x), ("Y", y)) if v is None]
            return x, y, (
                tr(f"不知道解调 {'/'.join(miss)} 在哪一路 RT 信号上 —— **拒绝对齐相位**。"
                "请传 x_signal_index / y_signal_index,或在仪器档案里填 "
                "lockin_x_signal_index / lockin_y_signal_index。"
                "(不猜:猜错读到的是另一路信号,而算出来的角度看上去一样合理,"
                "然后它会被写进硬件。)", f"Do not know which RT signal demodulator {'/'.join(miss)} is on -- **refusing to align the phase**. "
"Pass x_signal_index / y_signal_index, or fill in "
"lockin_x_signal_index / lockin_y_signal_index in the instrument profile. "
"(No guessing: a wrong guess reads another signal, and the computed angle looks equally plausible, "
"and then it would be written into the hardware.)"))
        return x, y, ""

    def execute(self, context, params: dict) -> SkillResult:
        calls: list = []

        # ⚠️ X/Y 来自 **RT 信号**(``Signals_ValsGet``),不是
        # ``LockIn_DemodSignalGet`` —— 后者返回的是「这个解调器读的是哪一路信号」的
        # **索引**,不是 X/Y 的值。本文件第一版就是拿它当 X/Y 的:那样 atan2 算的
        # 是两个通道号的夹角,而结果看上去和真的一样合理。
        idx_x, idx_y, why = self._signal_indices(params)
        if idx_x is None or idx_y is None:
            # 纯输入校验的拒绝:一次硬件调用都没发过 ⇒ 也不发关调制那一条。
            # 一个什么都没做的拒绝不该留下写操作。
            return SkillResult(
                skill_name="AutoPhase", success=False, error=why,
                data={"x_signal_index": idx_x, "y_signal_index": idx_y})

        res = self._align(context, params, idx_x, idx_y, calls)

        # 收尾:把调制关回去(缺陷⑪)。**成功和失败都关** —— 一次失败的对齐留下的
        # 调制,和一次成功的一样会污染后面所有电流判据。
        #
        # 唯一不关的是**中止**:中止的语义是「停手,别再动仪器」,而且此刻可能正有
        # 一套急停序列在跑,往里插写操作不是收尾是打岔。中止路径上相位也没被改过,
        # 现场保持原样正是想要的。
        if not (res.data or {}).get("aborted"):
            note = close_modulation(context, skill_name="AutoPhase", calls=calls)
            if res.data is None:
                res.data = {}
            res.data.update(note)
        res.nanonis_calls = calls
        return res

    def _align(self, context, params: dict, idx_x: int, idx_y: int,
               calls: list) -> SkillResult:
        """采样 → 算角 → 写解调相位。调制的开关不归它管(见 :meth:`execute`)。"""
        mode = str(params.get("mode") or "signal_to_x")
        window_s = float(params.get("window_s", 3.0))
        demod = int(params.get("demodulator", 1))

        xs: list[float] = []
        ys: list[float] = []
        t0 = time.monotonic()
        check_abort = getattr(context, "check_abort", None)
        while time.monotonic() - t0 < window_s:
            if callable(check_abort) and check_abort():
                return SkillResult(
                    skill_name="AutoPhase", success=False,
                    error=(tr("取样期间被中止 —— 相位未改动,**调制也未改动**"
                           "(中止后不再对仪器发写操作;调制此刻可能还开着)。", "Aborted during sampling -- the phase is unchanged, **and the modulation is unchanged** "
"(no write is sent to the instrument after an abort; the modulation may still be on at this moment).")),
                    data={"aborted": True})
            rec = context.safe_call("Signals_ValsGet", [idx_x, idx_y], 0)
            calls.append(rec)
            if not rec.error:
                vals = _values_pair(getattr(rec, "return_value", None))
                if vals is not None:
                    xs.append(vals[0])
                    ys.append(vals[1])
            time.sleep(self._poll_interval_s)

        if not xs:
            return SkillResult(
                skill_name="AutoPhase", success=False,
                error=(tr("读不到解调器的 X/Y —— **判不了相位**。"
                       "先确认调制已打开、解调通道配置正确。", "Cannot read the demodulator X/Y -- **cannot judge the phase**. "
"First confirm the modulation is on and the demodulation channels are configured correctly.")),
                data={"samples": 0}, nanonis_calls=calls)

        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        r = math.hypot(x_mean, y_mean)
        evidence = {
            "mode": mode, "samples": len(xs), "window_s": window_s,
            "x_mean": x_mean, "y_mean": y_mean, "r": r,
            "x_sd": _sd(xs), "y_sd": _sd(ys),
        }
        if r < self._MIN_SIGNAL:
            # 「无信号」和「相位是 0」是两回事。噪声上算出来的角只是噪声的角。
            return SkillResult(
                skill_name="AutoPhase", success=False,
                error=(tr(f"X/Y 都在噪声底(|R| = {r:.3g},下界 {self._MIN_SIGNAL:.1g})"
                       " —— **无可用信号,不给相位角**。请先打开调制、或确认有信号源"
                       "(隧穿态下有 dI/dV,退针态下有电容串扰)。", f"X/Y are both at the noise floor (|R| = {r:.3g}, lower bound {self._MIN_SIGNAL:.1g})"
" -- **no usable signal, no phase angle given**. First turn on the modulation, or confirm there is a signal source "
"(dI/dV in the tunnelling state, capacitive crosstalk in the withdrawn state).")),
                data=evidence, nanonis_calls=calls)

        # 当前相位:算出来的是**增量**,要加到现有相位上。
        rec_cur = context.safe_call("LockIn_DemodPhasGet", demod)
        calls.append(rec_cur)
        current = _scalar(getattr(rec_cur, "return_value", None))
        if current is None:
            return SkillResult(
                skill_name="AutoPhase", success=False,
                error=(tr("读不到当前解调相位 —— 算出来的是增量,没有起点就写不了。"
                       "(假起点会把相位转到一个谁也没要的地方。)", "Cannot read the current demodulation phase -- what is computed is an increment, and without a starting point it cannot be written. "
"(A false starting point would rotate the phase to a place nobody asked for.)")),
                data=evidence, nanonis_calls=calls)

        delta = math.degrees(math.atan2(y_mean, x_mean))
        if mode == "crosstalk_to_y":
            # 把串扰转到 Y ⇒ 再多转 90°,信号轴落在 X。
            delta -= 90.0
        target = (current + delta + 180.0) % 360.0 - 180.0
        evidence.update({"current_phase_deg": current,
                         "delta_deg": delta, "target_phase_deg": target})

        rec_set = context.safe_call("LockIn_DemodPhasSet", demod, target)
        calls.append(rec_set)
        if rec_set.error:
            return SkillResult(skill_name="AutoPhase", success=False,
                               error=tr(f"写解调相位失败:{rec_set.error}", f"Failed to write the demodulation phase: {rec_set.error}"),
                               data=evidence, nanonis_calls=calls)

        rec_rb = context.safe_call("LockIn_DemodPhasGet", demod)
        calls.append(rec_rb)
        got = _scalar(getattr(rec_rb, "return_value", None))
        evidence["readback_phase_deg"] = got
        verified = got is not None and abs(got - target) < 0.5
        evidence["readback_verified"] = verified
        if not verified:
            return SkillResult(
                skill_name="AutoPhase", success=False,
                error=(tr(f"写了 {target:.2f}° 但回读是 {got} —— 不一致,"
                       "**不要按已对齐继续**。", f"Wrote {target:.2f}° but the readback is {got} -- inconsistent, "
"**do not continue as if aligned**.")),
                data=evidence, nanonis_calls=calls)
        return SkillResult(skill_name="AutoPhase", success=True, data=evidence,
                           nanonis_calls=calls)


def _values_pair(rv) -> "tuple[float, float] | None":
    """``Signals_ValsGet`` 回包里的头两个浮点。

    回包规格 ``["i", "*f"]``(先长度再数组),而 ``decodeArray`` 会把每个元素裹成
    1-元组 —— 除非 ``nanonis_patch`` 已经拆过。**两种形态都要接**:仓里为这件事
    单独记过一条,而替身只发其中一种形态正是那次没被测出来的原因。
    """
    if not isinstance(rv, (list, tuple)) or len(rv) <= 2:
        return None
    for field in rv[2]:
        if isinstance(field, (list, tuple)) and len(field) >= 2:
            out = []
            for item in field[:2]:
                if isinstance(item, (list, tuple)) and item:
                    item = item[0]
                try:
                    out.append(float(item))
                except (TypeError, ValueError):
                    return None
            return (out[0], out[1])
    return None


def _sd(vals: "list[float]") -> float:
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


__all__ = ["ListLockInPresets", "ApplyLockInPreset", "AutoPhase"]
