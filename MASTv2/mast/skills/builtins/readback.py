"""Read back what you configured — the other half of every Set.

A census of the Nanonis surface against MAST's skills (2026-07-13) turned up 37
verbs where MAST could WRITE a setting and had no way to READ IT BACK. The agent
could change the lock-in's modulation signal, the PLL's excitation, the piezo range,
the tip-shaper's parameters — and then had to take its own word for it.

That is the same defect class as "hardware stopped != target reached". A write that returns without a TCP error has been
*accepted*, not necessarily *applied*, and nothing downstream can tell the
difference. Verification is not paranoia here; it is the only way the claim in a
SkillResult is anything more than a restatement of the request.

SHAPE
=====
Not 37 tool-sized getters — the instrument_control agent already carries ~285 tools
and each one costs routing accuracy on every turn. Instead, ONE skill per subsystem
that reads the whole thing back in a single call, which is also the shape the model
actually wants: "I configured the lock-in; show me the lock-in."

Every read is independent: one unsupported getter blanks its own key and the rest
still come back. A half-answer beats a blanket failure when you are trying to work
out which half is wrong.

THE MOST IMPORTANT ONE
======================
``GetZControllerState`` reads ``ZCtrl_OnOffGet`` — the REAL-TIME controller's view
of whether the feedback loop is closed, which Nanonis' manual distinguishes from the
Z-Controller MODULE's view (``ZCtrl_StatusGet``, what the GUI shows) and tells you
to prefer before starting an experiment. MAST had only the module's view. See
``mast.skills.verify``.
"""

from __future__ import annotations

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill
from mast.skills.builtins.zctrl_gain import gain_reading_notes
from mast.i18n import tr

_ZCTRL_MODULE_STATUS = {
    1: "Off", 2: "On", 3: "Hold", 4: "SwitchingOff", 5: "SafeTip", 6: "Withdrawing",
}


def _decode_nanonis(v):
    """Return the decoded payload of a Nanonis TCP reply.

    Do not expose the error/raw/body envelope as a controller value. Unwrap a
    single-element payload to a scalar and retain multi-element payloads such
    as gains and limits as lists.
    """
    # Share the decoder with all readback paths to keep envelope handling aligned.
    from mast.io.nanonis_files import decode_reply

    return decode_reply(v)


def _rv(record):
    return _decode_nanonis(getattr(record, "return_value", None))


def _as_bool(v) -> bool | None:
    """A Nanonis 0/1 flag as a real bool — or None when it was not readable.

    None and False must stay distinguishable. ``_read_many`` writes None into a
    key whose getter errored (and lists it under ``_unreadable``), so folding
    that into False would turn "we could not ask" into "it is off" — the exact
    substitution this file exists to prevent, one flag over.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, (list, tuple)):
        # Flatten: callers normally hand us a ``_decode_nanonis``-unwrapped
        # scalar, but the raw wire form is ``('', b'…', [flag])`` — the flag is
        # INSIDE the third element. A top-level-only scan returns None there,
        # i.e. "could not read", which would silently retire the warning this
        # helper exists to raise. Same trap that made `controller_on` come back
        # None on every call for a working Z controller (see _decode_nanonis).
        for x in v:
            if isinstance(x, (list, tuple)):
                nested = _as_bool(x)
                if nested is not None:
                    return nested
            elif isinstance(x, (bool, int, float)):
                return bool(x)
    return None


#: Tip-shaper readback fields in response order. Mapping positions to names and
#: units prevents a bias value from being interpreted as a Z displacement.
#: An empty unit denotes a dimensionless flag.
_TIP_SHAPER_PROPS: tuple[tuple[str, str], ...] = (
    ("switch_off_delay_s", "s"),   # Z averaged this long before the loop opens
    ("change_bias", ""),           # PropsGet: 0=False, 1=True
    ("bias_v", "V"),               # applied before the FIRST Z ramp
    ("tip_lift_m", "m"),           # FIRST ramp, relative to current Z
    ("lift_time_1_s", "s"),
    ("bias_lift_v", "V"),          # applied just AFTER the first Z ramp
    ("bias_settling_s", "s"),
    ("lift_height_m", "m"),        # SECOND ramp height
    ("lift_time_2_s", "s"),
    ("end_wait_s", "s"),
    ("restore_feedback", ""),      # PropsGet: 0=False, 1=True
)


def _name_tip_shaper_props(props) -> dict:
    """Attach the protocol's field names to the 11-element props array.

    Keeps ``props`` untouched alongside — naming is an interpretation, and the
    raw array is the evidence for it.

    **Conservative on a length mismatch.** If the instrument returns anything
    other than 11 values, the mapping this table encodes is no longer known to
    apply, so nothing is named and the discrepancy is reported instead. Naming
    the leading fields "as far as they go" would be the worse failure: it looks
    authoritative and silently shifts every field after the divergence.

    Note the write/read asymmetry on the two flags, which is a live trap:
    ``PropsSet`` takes 0=no change / 1=True / 2=False, while ``PropsGet``
    returns 0=False / 1=True. ``TipShape`` sends 2 for False — and that reads
    back as 0. The bools below follow the GET convention, which is the only one
    that applies to what we are decoding here.
    """
    if not isinstance(props, (list, tuple)):
        return {}
    vals = list(props)
    if len(vals) != len(_TIP_SHAPER_PROPS):
        return {"props_named": None,
                "props_named_error": (
                    tr(f"本机返回 {len(vals)} 个值，而协议 "
                    f"(TCP-reference/tcp_protocol.txt, TipShaper.PropsGet) "
                    f"定义了 {len(_TIP_SHAPER_PROPS)} 个 —— 字段顺序无法确证，"
                    f"故不做具名。请对着协议原文人工核对 props。", f"This machine returned {len(vals)} values, while the protocol (TCP-reference/tcp_protocol.txt, TipShaper.PropsGet) defines {len(_TIP_SHAPER_PROPS)} - the field order cannot be confirmed, so no names are given. Check props manually against the original protocol text."))}
    named: dict = {}
    for (key, unit), value in zip(_TIP_SHAPER_PROPS, vals, strict=True):
        named[key] = bool(value) if unit == "" else value
        if unit == "":
            named[key + "_raw"] = value      # keep the code, 0/1/(2) all differ
    return {"props_named": named}


def _read_many(name: str, reads, data: dict | None = None) -> SkillResult:
    """Read a batch of getters. One failure blanks its own key, never the batch.

    ``reads`` is ``((key, thunk), …)`` and each thunk performs exactly ONE
    ``safe_call("LITERAL_VERB", …)``.

    **The verb must be a literal inside the thunk.** Every safety tool in this repo
    finds Nanonis calls by grepping ``safe_call("…")`` — the abort-policy checker,
    the security audit, the API-coverage census. A verb passed through a variable is
    invisible to all of them: the call still happens, and no tool that guards this
    system can see it. (That is not hypothetical — a table-driven loop here made the
    coverage census report 40 unreadable settings that MAST could in fact read.)
    """
    calls: list = []
    out: dict = dict(data or {})
    failed: list[str] = []
    for key, thunk in reads:
        rec = thunk()
        calls.append(rec)
        if rec.error:
            out[key] = None
            failed.append(key)
        else:
            out[key] = _rv(rec)
    if failed:
        out["_unreadable"] = failed   # be explicit: None could also be a real value
    return SkillResult(skill_name=name, success=True, nanonis_calls=calls, data=out)


class GetZControllerState(BaseSkill):
    """Everything about the Z feedback loop, in one call — including the RT truth."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetZControllerState",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("一次调用读回**整个** Z 控制器状态：环是否闭合、设定值、增益、Z 位置、Z 限值及其是否启用、"
                "tip lift、退针速率、关断延时，以及 home 位置。\n"
                "\n"
                "**「环关了吗？」有两个不同的答案，而它们并不是一回事。** `controller_on` 来自**实时控制器**（ZCtrl_OnOffGet）"
                "—— 这一个才是 Nanonis 手册要你在开始任何需要开环的动作之前去读的，因为模块那边可能还在说「Off」"
                "，而实时控制器其实还没跟上。`module_status` 是 Nanonis 界面上显示的那个。"
                "两者不一致时，信 `controller_on`。\n"
                "\n"
                "改动过 Z 环的任何东西之后、以及任何一次粗进针之前，都调它一下。", "Read back the **entire** Z controller state in one call: whether the loop is closed, setpoint, gains, Z position, Z limits and whether they are enabled, tip lift, withdraw rate, switch-off delay, and the home position.\n\n**\"Is the loop off?\" has two different answers, and they are not the same thing.** `controller_on` comes from the **real-time controller** (ZCtrl_OnOffGet) - this is the one the Nanonis manual tells you to read before any action that needs an open loop, because the module side may still say \"Off\" while the real-time controller has not caught up. `module_status` is the one shown in the Nanonis UI. When the two disagree, trust `controller_on`.\n\nCall it after changing anything about the Z loop, and before any coarse approach.")
            ),
            parameters=[],
            estimated_duration_s=1.5,
            composition_level=0,
            tags=["z", "controller", "read", "readback", "verify"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        res = _read_many("GetZControllerState", (
            ("controller_on", lambda: context.safe_call("ZCtrl_OnOffGet")),        # ← the RT truth
            ("module_status", lambda: context.safe_call("ZCtrl_StatusGet")),        # ← what the GUI shows
            ("setpoint", lambda: context.safe_call("ZCtrl_SetpntGet")),
            ("gains", lambda: context.safe_call("ZCtrl_GainGet")),
            ("z_m", lambda: context.safe_call("ZCtrl_ZPosGet")),
            ("z_limits", lambda: context.safe_call("ZCtrl_LimitsGet")),
            ("z_limits_enabled", lambda: context.safe_call("ZCtrl_LimitsEnabledGet")),
            ("tip_lift", lambda: context.safe_call("ZCtrl_TipLiftGet")),
            ("withdraw_rate", lambda: context.safe_call("ZCtrl_WithdrawRateGet")),
            ("switch_off_delay", lambda: context.safe_call("ZCtrl_SwitchOffDelayGet")),
            ("home", lambda: context.safe_call("ZCtrl_HomePropsGet")),
        ))
        d = res.data
        # Surface the disagreement explicitly rather than leaving two raw numbers for
        # the model to reconcile. A silent disagreement is exactly the failure mode.
        raw = d.get("module_status")
        code = None
        if isinstance(raw, (list, tuple)):
            for x in raw:
                if isinstance(x, int):
                    code = x
                    break
        elif isinstance(raw, int):
            code = raw
        if code is not None:
            d["module_status_name"] = _ZCTRL_MODULE_STATUS.get(code, f"?({code})")
        rt = d.get("controller_on")
        rt_on = None
        if isinstance(rt, (list, tuple)):
            for x in rt:
                if isinstance(x, (int, bool)):
                    rt_on = bool(x)
                    break
        elif isinstance(rt, (int, bool)):
            rt_on = bool(rt)
        d["controller_on"] = rt_on
        if rt_on is not None and code is not None:
            module_on = code == 2
            if module_on != rt_on:
                d["disagreement"] = (
                    tr(f"⚠ 实时控制器说 {'ON' if rt_on else 'OFF'}，"
                    f"而 Z-Controller 模块显示 {d['module_status_name']}。"
                    "以实时控制器为准（Nanonis 手册：通信延迟期间两者会不一致）。", f"WARNING: the real-time controller says {'ON' if rt_on else 'OFF'}, while the Z-Controller module shows {d['module_status_name']}. The real-time controller is authoritative (Nanonis manual: the two can disagree during communication delay).")
                )
        # gains contains P (m), T (s) and I (m/s); annotate units and implausible
        # magnitudes so a malformed readback cannot look like successful agreement.
        # Report both Z limits and their enable flag: correct limit values provide
        # no bound when the controller has disabled their enforcement.
        d["z_limits_enabled"] = _as_bool(d.get("z_limits_enabled"))
        if d["z_limits_enabled"] is False:
            d["z_limits_warning"] = (
                tr("⚠ Z 位置限值**未启用**（z_limits_enabled = 0）。z_limits 里的那对数字"
                "此刻不起任何作用 —— Nanonis 手册：限值未启用时设置限值「has no "
                "effect」。若你正依赖 Z 限值兜底（尤其是为了某个较高的样品把限值收窄），"
                "先用 SetZLimits(enable=true) 启用；未启用时唯一还在拦 Z 的是压电本身"
                "的行程与 SafeTip。", "WARNING: the Z position limits are **not enabled** (z_limits_enabled = 0). The pair of numbers in z_limits has no effect right now - Nanonis manual: setting limits while they are not enabled \"has no effect\". If you are relying on the Z limits as a backstop (especially having narrowed them for a taller sample), first enable them with SetZLimits(enable=true); while not enabled, the only things still stopping Z are the piezo travel itself and SafeTip.")
            )

        gains = d.get("gains")
        if isinstance(gains, (list, tuple)) and len(gains) >= 3:
            try:
                notes = gain_reading_notes({
                    "p_gain": float(gains[0]),
                    "time_constant_s": float(gains[1]),
                    "i_gain": float(gains[2]),
                })
            except (TypeError, ValueError):  # pragma: no cover — defensive
                notes = []
            if notes:
                d["gain_warnings"] = notes
            d["gain_units"] = {"p_gain": "m", "time_constant_s": "s",
                               "i_gain": "m/s"}
        return res


class GetSpectroscopyConfig(BaseSkill):
    """Bias- and Z-spectroscopy parameters, read back."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSpectroscopyConfig",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("回读谱学配置 —— 偏压谱、Z 谱，或两者都读：扫描属性、高级属性（Z 控制器是否保持、"
                "终点 Z、是否记录终点 Z）、多线段（MLS）模式及其分段取值，以及 Z 谱的退针延时。"
                "\n"
                "\n"
                "用它在跑之前确认一次谱学确实是按你要的那样设好的。一张糟糕的 MLS 分段表、或一个没料到的「Z 控制器保持开启」"
                "，都不会报错 —— 它只是产出一条含义与你所想不同的曲线。", "Read back the spectroscopy configuration - bias spectroscopy, Z spectroscopy, or both: scan properties, advanced properties (whether the Z controller is held, end-point Z, whether end-point Z is recorded), multi-line-segment (MLS) mode and its segment values, and the Z spectroscopy retract delay.\n\nUse it to confirm once before running that spectroscopy is really set up the way you want. A bad MLS segment table, or an unexpected \"Z controller held on\", will not raise an error - it just produces a curve whose meaning differs from what you think.")
            ),
            parameters=[
                ParameterSpec(name="which", type="str",
                              description="bias | z | both",
                              required=False, default="both",
                              allowed_values=["bias", "z", "both"]),
            ],
            estimated_duration_s=2.0,
            composition_level=0,
            tags=["spectroscopy", "sts", "read", "readback", "verify"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        which = str(params.get("which", "both") or "both")
        reads: list = []
        if which in ("bias", "both"):
            reads += [
                ("bias_props", lambda: context.safe_call("BiasSpectr_PropsGet")),
                ("bias_adv_props", lambda: context.safe_call("BiasSpectr_AdvPropsGet")),
                ("bias_mls_mode", lambda: context.safe_call("BiasSpectr_MLSModeGet")),
                ("bias_mls_values", lambda: context.safe_call("BiasSpectr_MLSValsGet")),
                ("bias_status", lambda: context.safe_call("BiasSpectr_StatusGet")),
            ]
        if which in ("z", "both"):
            reads += [
                ("z_props", lambda: context.safe_call("ZSpectr_PropsGet")),
                ("z_adv_props", lambda: context.safe_call("ZSpectr_AdvPropsGet")),
                ("z_retract_delay", lambda: context.safe_call("ZSpectr_RetractDelayGet")),
                ("z_status", lambda: context.safe_call("ZSpectr_StatusGet")),
            ]
        return _read_many("GetSpectroscopyConfig", reads, {"which": which})


# NB: the lock-in's readback lives in lockin.py, folded into the EXISTING
# GetLockInConfig rather than duplicated here. A second skill by that name would
# have SILENTLY OVERWRITTEN the first — SkillRegistry only logs a warning on a
# name collision and then replaces the entry, so the original skill (with its
# careful scalar parsing of amplitude/frequency/phase) would simply have vanished.
# tests/v2/unit/skills/test_no_duplicate_skill_names.py now makes that fatal.

class GetPllConfig(BaseSkill):
    """The PLL's excitation, bandwidths and ranges, read back."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetPllConfig",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("回读 PLL：**激励输出是否开着**、幅度控制器的设定值与带宽、相位控制器的带宽、解调器的相位参考与谐波次数、"
                "输入量程，以及频率／激励的覆写。\n"
                "\n"
                "`excitation_on` 是最该先看的一项：MAST 从前能把 PLL 激励打开，"
                "却没有任何办法问它到底开没开。一路被忘在开启状态的激励，意味着你以为静止的悬臂其实正在被驱动。", "Read back the PLL: **whether the excitation output is on**, setpoint and bandwidth of the amplitude controller, bandwidth of the phase controller, phase reference and harmonic order of the demodulator, input range, and frequency/excitation overrides.\n\n`excitation_on` is the item to look at first: MAST could previously turn the PLL excitation on but had no way to ask whether it was really on. An excitation forgotten in the on state means the cantilever you think is still is actually being driven.")
            ),
            parameters=[
                ParameterSpec(name="modulator", type="int",
                              description=tr("调制器序号", "Modulator index"),
                              required=False, default=1, min_value=1, max_value=8),
                ParameterSpec(name="demodulator", type="int",
                              description=tr("解调器序号", "Demodulator index"),
                              required=False, default=1, min_value=1, max_value=8),
            ],
            estimated_duration_s=2.0,
            composition_level=0,
            tags=["pll", "read", "readback", "verify"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        m = int(params.get("modulator", 1) or 1)
        d = int(params.get("demodulator", 1) or 1)
        return _read_many("GetPllConfig", (
            ("excitation_on", lambda: context.safe_call("PLL_OutOnOffGet", m)),
            ("amp_ctrl_setpoint", lambda: context.safe_call("PLL_AmpCtrlSetpntGet", m)),
            ("amp_ctrl_bandwidth", lambda: context.safe_call("PLL_AmpCtrlBandwidthGet", m)),
            ("phase_ctrl_bandwidth", lambda: context.safe_call("PLL_PhasCtrlBandwidthGet", m)),
            ("freq_exc_overwrite", lambda: context.safe_call("PLL_FreqExcOverwriteGet", m)),
            ("demod_phase_ref", lambda: context.safe_call("PLL_DemodPhasRefGet", d)),
            ("demod_harmonic", lambda: context.safe_call("PLL_DemodHarmonicGet", d)),
            ("input_range", lambda: context.safe_call("PLL_InpRangeGet", d)),
            ("add_on_off", lambda: context.safe_call("PLL_AddOnOffGet", m)),
        ), {"modulator": m, "demodulator": d})


class GetPiezoConfig(BaseSkill):
    """Piezo range, calibration, hysteresis and XYZ limits, read back."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetPiezoConfig",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("回读压电：它的**量程**、标定、灵敏度、迟滞校正（开／关及其系数）、漂移补偿、倾斜，以及 **XYZ 电压限值**。"
                "\n"
                "\n"
                "量程和限值是把一个下达的电压变成一段距离、并框住它能走多远的那两样东西。MAST 从前能设量程却读不回来 —— 也就是说，"
                "它算出的每一个位置，都建立在一个它无从核对的数字上。", "Read back the piezo: its **range**, calibration, sensitivity, hysteresis correction (on/off and its coefficients), drift compensation, tilt, and the **XYZ voltage limits**.\n\nRange and limits are the two things that turn a commanded voltage into a distance and bound how far it can go. MAST could previously set the range but not read it back - meaning every position it computed rested on a number it had no way to check.")
            ),
            parameters=[],
            estimated_duration_s=2.0,
            composition_level=0,
            tags=["piezo", "read", "readback", "verify"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        return _read_many("GetPiezoConfig", (
            ("range", lambda: context.safe_call("Piezo_RangeGet")),
            ("calibration", lambda: context.safe_call("Piezo_CalibrGet")),
            ("sensitivity", lambda: context.safe_call("Piezo_SensGet")),
            ("hysteresis_on", lambda: context.safe_call("Piezo_HystOnOffGet")),
            ("hysteresis_values", lambda: context.safe_call("Piezo_HystValsGet")),
            ("xyz_limits", lambda: context.safe_call("Piezo_XYZLimitsGet")),
            ("drift_comp", lambda: context.safe_call("Piezo_DriftCompGet")),
            ("tilt", lambda: context.safe_call("Piezo_TiltGet")),
        ))


class GetTipShaperConfig(BaseSkill):
    """What TipShape will actually do, read back before you run it."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetTipShaperConfig",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("回读 tip-shaper 配置：关断延时、是否改变偏压、所用的偏压与 lift、下压／回撤速度、"
                "各段等待时间，以及事后是否恢复反馈。\n"
                "\n"
                "**请用 `props_named`** —— 同一批数值，按协议自己的字段名索引（`bias_v`、"
                "`tip_lift_m`、`lift_height_m`……）。`props` 是原始的 11 元素线上数组，"
                "保留下来作为证据；**不要**靠猜顺序去下标取值。它把**电压**和 **Z 抬升**混在一起，"
                "所以错一位就会把一个偏压读成一个高度。`props_named` 为 null 时，说明仪器返回的值个数出乎意料，"
                "`props_named_error` 会说明这一点 —— 那种情况下请把顺序当作未知。"
                "\n"
                "\n"
                "在 TipShape **之前**读这个。修针是蓄意把针尖**扎进**表面 —— 这些参数就是「一次受控的轻戳」"
                "与「一根埋进去的针」之间的分界，而在此之前 MAST 能设它们却看不到它们。", "Read back the tip-shaper configuration: switch-off delay, whether bias is changed, the bias and lift used, push/pull speeds, wait times of each phase, and whether feedback is restored afterwards.\n\n**Use `props_named`** - the same values indexed by the protocol's own field names (`bias_v`, `tip_lift_m`, `lift_height_m`...). `props` is the raw 11-element on-wire array, kept as evidence; **do not** index into it by guessing the order. It mixes **voltages** and **Z lifts**, so being off by one reads a bias as a height. When `props_named` is null, the instrument returned an unexpected number of values, and `props_named_error` explains this - in that case treat the order as unknown.\n\nRead this **before** TipShape. Tip conditioning deliberately **pushes the tip into** the surface - these parameters are the dividing line between \"a controlled light poke\" and \"a buried tip\", and until now MAST could set them but not see them.")
            ),
            parameters=[],
            estimated_duration_s=1.0,
            composition_level=0,
            tags=["tip", "tipshaper", "read", "readback", "verify"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        res = _read_many("GetTipShaperConfig", (
            ("props", lambda: context.safe_call("TipShaper_PropsGet")),
        ))
        res.data.update(_name_tip_shaper_props(res.data.get("props")))
        return res


class GetScanPatternConfig(BaseSkill):
    """Grid / line pattern and the pattern experiment, read back."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetScanPatternConfig",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("回读图案模块：网格定义、线定义，以及图案实验的属性（跑哪个实验、文件基名、测量前延时）。"
                "\n"
                "\n"
                "一个跑在错误网格上的网格实验，就是在错误的地方测上好几个小时 —— 而这个网格此前是只写的。", "Read back the pattern module: grid definition, line definition, and the properties of the pattern experiment (which experiment to run, file base name, delay before measurement).\n\nA grid experiment running on the wrong grid means hours of measuring in the wrong places - and this grid used to be write-only.")
            ),
            parameters=[],
            estimated_duration_s=1.5,
            composition_level=0,
            tags=["pattern", "grid", "read", "readback", "verify"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        return _read_many("GetScanPatternConfig", (
            ("grid", lambda: context.safe_call("Pattern_GridGet")),
            ("line", lambda: context.safe_call("Pattern_LineGet")),
            ("experiment", lambda: context.safe_call("Pattern_PropsGet")),
        ))


class GetMiscInstrumentConfig(BaseSkill):
    """The stragglers: bias range, current calibration/gains, motor, sweep limits, scope."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetMiscInstrumentConfig",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("回读那些不归属于某个更大子系统的设置：**偏压量程**、电流前放的标定与可用增益档、原子跟踪器的参数、"
                "粗动马达的频率／幅度、偏压扫描器与通用扫描器的限值、Follow-Me 的过采样与 point-&-shoot 设置、"
                "函数发生器的空闲值与所驱动的信号，以及 1 通道示波器的通道。\n"
                "\n"
                "单看每一项都很小；合起来，它们是其余每一个读数被缩放所依据的那些数字。尤其是电流**标定**："
                "它错了，MAST 有史以来报出的每一个电流都会差一个固定倍数，而且悄无声息。", "Read back settings that do not belong to any larger subsystem: the **bias range**, current preamplifier calibration and available gain ranges, atom-tracker parameters, coarse-motion motor frequency/amplitude, limits of the bias scanner and general scanner, Follow-Me oversampling and point-&-shoot settings, function generator idle value and driven signal, and the channel of the 1-channel oscilloscope.\n\nEach one is small on its own; together they are the numbers every other reading is scaled by. The current **calibration** in particular: if it is wrong, every current MAST has ever reported is off by a fixed factor, silently.")
            ),
            parameters=[],
            estimated_duration_s=3.0,
            composition_level=0,
            tags=["read", "readback", "verify", "calibration"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        return _read_many("GetMiscInstrumentConfig", (
            ("bias_range", lambda: context.safe_call("Bias_RangeGet")),
            ("current_calibration", lambda: context.safe_call("Current_CalibrGet")),
            ("current_gains", lambda: context.safe_call("Current_GainsGet")),
            ("atom_track", lambda: context.safe_call("AtomTrack_PropsGet", 1)),
            # Axis 0 = "all". The argument is NOT optional — the library signature is
            # Motor_FreqAmpGet(Axis), and calling it bare raised TypeError, which the
            # pool's blanket except turned into an error string on this ONE field.
            # The field therefore never carried a reading; nobody noticed because a
            # per-field error looks exactly like "that module isn't installed"
            # (2026-07-31). The coarse-drive readback check depends on this call.
            ("motor_freq_amp", lambda: context.safe_call("Motor_FreqAmpGet", 0)),
            ("bias_sweep_limits", lambda: context.safe_call("BiasSwp_LimitsGet")),
            ("gen_sweep_limits", lambda: context.safe_call("GenSwp_LimitsGet")),
            ("gen_sweep_signals", lambda: context.safe_call("GenSwp_SwpSignalListGet")),
            ("folme_oversampling", lambda: context.safe_call("FolMe_OversamplGet")),
            ("folme_ps_experiment", lambda: context.safe_call("FolMe_PSExpGet")),
            ("folme_ps_props", lambda: context.safe_call("FolMe_PSPropsGet")),
            ("fungen2_idle", lambda: context.safe_call("FunGen2Ch_IdleGet", 1)),
            ("fungen2_signal", lambda: context.safe_call("FunGen2Ch_SignalGet", 1)),
            ("osci1t_channel", lambda: context.safe_call("Osci1T_ChGet")),
            ("osci1t_trigger", lambda: context.safe_call("Osci1T_TrigGet", 0, 0, 0, 0.0, 0.0, 0.0)),
            ("lockin_freqswp_signal", lambda: context.safe_call("LockInFreqSwp_SignalGet")),
        ))
