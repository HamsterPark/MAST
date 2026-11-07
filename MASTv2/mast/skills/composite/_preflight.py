"""Composite module preflight — check required Nanonis modules BEFORE executing.

WHY (field trace, s306): a conditioning flow ran four steps and only
then, deep inside TipShape, discovered "Tip Shaper module not running". The
module being off was knowable up front with one read; instead the run spent time
and instrument actions getting to the point of failure. Same shape as the Motor
Control "module not running" case.

A composite that REQUIRES a Nanonis module (Tip Shaper, Motor Control, …) probes
its read-only status here, before committing to the plan, and early-exits with a
clear, actionable message ("open/start the module in Nanonis") naming the module.

SAFETY / FAIL-OPEN: this is a best-effort EARLY warning, never a new false-block.
A healthy module answers a status Get with no error, so the common path proceeds.
It early-exits ONLY when the probe returns an error whose text clearly says the
module is down — never on an ambiguous error (a transient, a link problem, a
method our client lacks). On anything unclear it PROCEEDS, and the skill surfaces
the real error at first use exactly as it does today. That keeps a rig whose
Nanonis phrases module errors differently working unchanged.
"""

from __future__ import annotations

import logging

from mast.i18n import tr
from mast.skills.verify import values_match

logger = logging.getLogger(__name__)

# Read-only status probes per module: (probe_thunk, human label). The verb is a
# STRING LITERAL held INSIDE the thunk's safe_call(...) so the repo's grep-based
# safety tools (abort-policy checker / security audit / API-coverage census) can
# still see it — a `safe_call(var)` would be invisible to them (see
# tests/v2/unit/core/test_safe_call_verbs_are_literal.py). All three verbs are
# side-effect-free Gets that exist on the nanonis_spm client.
def _stm_motor_group() -> int:
    """The configured STM coarse group (``nanonis_motor_groups.json``, 0 when the
    file is absent). An unusable file falls back to 0 HERE ONLY: this probe is a
    read whose sole purpose is "is the module running", and the module answers
    the same for every group — the moves themselves refuse on a broken file."""
    try:
        from mast.instruments.motor_groups import stm_coarse_group

        return stm_coarse_group()
    except Exception:  # noqa: BLE001 - the probe fails open by design (module doc)
        return 0


PROBE_TIP_SHAPER: tuple = (
    lambda ctx: ctx.safe_call("TipShaper_PropsGet"), "Tip Shaper")
PROBE_MOTOR: tuple = (
    lambda ctx: ctx.safe_call("Motor_PosGet", _stm_motor_group(), 500), "Motor Control")
PROBE_SCAN: tuple = (
    lambda ctx: ctx.safe_call("Scan_StatusGet"), "Scan")

# Error-text signatures that CLEARLY mean "module is not running/available". Only
# these trigger an early-exit; anything else fails open. Kept broad across the
# phrasings Nanonis uses (and their zh equivalents) but all unambiguously "down".
_DOWN_SIGNATURES: tuple[str, ...] = (
    "not running", "not active", "not available", "unavailable",
    "not loaded", "not started", "no module", "module is not",
    "is not running", "未运行", "未加载", "未启动", "不可用",
)

# Ambiguous errors that must NOT be read as "module down" — a different layer owns
# each of these, and treating them as a module-missing would be a false-block.
_AMBIGUOUS: tuple[str, ...] = (
    "not found on nanonis",     # our client lacks the verb — a code bug, not a module
    "comms_circuit_open",       # the comms breaker already owns the link-down story
    "connectionpool is closed",
    "aborted by operator",      # the abort gate owns this
)


def _module_down_message(label: str, method: str, err: str) -> str:
    return (
        "module_missing: "
        + tr(f"需要的 Nanonis 模块「{label}」未运行或不可用"
             f"(探测 {method} 返回: {err[:160]})。请先在 Nanonis 中打开并启动该模块,"
             f"然后再重试;在模块启动前请勿反复重试此操作。",
             f"The required Nanonis module \"{label}\" is not running or unavailable "
             f"(probe {method} returned: {err[:160]}). Open and start the module in "
             f"Nanonis first, then retry; do not retry repeatedly before the module "
             f"is started.")
    )


def module_down_hint(err: "str | None", label: str) -> str:
    """If ``err`` clearly says a module is down, return an actionable suffix to
    append to a skill's error; else "" (leaf skills call this to make the raw
    Nanonis error self-explanatory without adding a probe round-trip)."""
    low = str(err or "").lower()
    if not low or any(sig in low for sig in _AMBIGUOUS):
        return ""
    if any(sig in low for sig in _DOWN_SIGNATURES):
        return tr(f" — 「{label}」模块似乎未运行/不可用,请先在 Nanonis 中打开并启动该"
                  f"模块,再重试(模块未启动前请勿反复重试)。",
                  f" - the \"{label}\" module appears not to be running/unavailable; open "
                  f"and start it in Nanonis first, then retry (do not retry repeatedly "
                  f"before it is started).")
    return ""


def preflight_modules(context, specs, *, accumulator=None,
                      unchecked=None) -> "str | None":
    """检查各必需模块并返回提前退出原因；只有成功完成的探测才可声明前置已检查。"""
    def _note(label: str, why: str) -> None:
        if unchecked is None:
            return
        try:
            unchecked.append(f"{label}: {why}")
        except Exception:  # noqa: BLE001 — 出参坏了不该反噬预检
            pass

    for spec in specs or ():
        try:
            probe, label = spec[0], spec[1]
        except (TypeError, IndexError):
            _note(str(spec), tr("spec 格式不对,连探针都取不出来",
                              "malformed spec; could not even extract a probe"))
            continue
        try:
            rec = probe(context)
        except Exception as exc:  # noqa: BLE001 — a broken probe must not block
            logger.warning("preflight probe for %s raised (fail-open, "
                           "**该模块未被验证**): %s", label, exc)
            _note(label, tr(f"探针抛异常({type(exc).__name__}: {exc})",
                            f"probe raised ({type(exc).__name__}: {exc})"))
            continue
        if accumulator is not None:
            try:
                accumulator.append(rec)
            except Exception:  # noqa: BLE001
                pass
        method = str(getattr(rec, "method", "") or "probe")
        err = str(getattr(rec, "error", "") or "").strip()
        if not err:
            continue  # module answered → present
        low = err.lower()
        if any(sig in low for sig in _AMBIGUOUS):
            # someone else owns this failure → fail open，但这个模块**没被验证**
            _note(label, tr(f"回包是别人的错误,归不到本模块头上({err[:120]})",
                            f"the error belongs to another layer, not this module ({err[:120]})"))
            continue
        if any(sig in low for sig in _DOWN_SIGNATURES):
            logger.info("preflight: module '%s' appears down (%s)", label, err)
            try:
                from mast.core.diagnostics import record
                record("module_missing", label,
                       tr("所需 Nanonis 模块未运行——已在执行前早退",
                          "The required Nanonis module is not running -- exited early before execution"),
                       probe=method, error=err[:200])
            except Exception:  # noqa: BLE001 — diagnostics never breaks a skill
                pass
            return _module_down_message(label, method, err)
        # An error we can't classify as clearly-down → fail open (proceed).
        logger.debug("preflight probe %s errored ambiguously (fail-open): %s",
                     method, err)
        _note(label, tr(f"回包报错但分不出是不是模块本身的问题({err[:120]})",
                        f"error reply, but cannot tell whether it is the module itself ({err[:120]})"))
    return None


__all__ = [
    "preflight_modules",
    "PROBE_TIP_SHAPER",
    "PROBE_MOTOR",
    "PROBE_SCAN",
]


# ─────────────────────────────────────────────────────────────────────
# Lock-in modulation hygiene — 用完即关
# ─────────────────────────────────────────────────────────────────────
#
# 调制开着的时候,电流通道上叠着一层设计内的纹波。对**不使用
# lock-in 信号**的流程 —— 进退针、扫图、修针、粗动 —— 那层纹波是纯干扰:
# `jump_rate_hz` 会被数得虚高,而电流通道上一切形态判据都跟着失义。
#
# 这里是**移除诱因**那一半,不是提示词那一半:不该开着的时候,机制自己把它关掉。
# 与监控侧的调制上下文(`ctx_lockin_on`)互补 —— 那边管「开着时监控别误报」,这条管「不该开着时根本别
# 开着」。两边都要:有些流程需要在调制下运行,那时监控侧救场;而没人要它开着的时候
# 也不该靠人记得去关。
#
# **自动关而不是拒绝**,以保证零打断;但**必须留痕** —— 操作员要知道
# 是谁动了调制,所以关掉这件事进结果 data,不只是写日志。

#: 需要「先把调制关掉」的技能标签。判据是「这个流程用不用 lock-in 信号」,
#: 不是「它危不危险」。扫图 / 修针 / 脉冲 / 粗动 / 进针都不看 lock-in 的解调输出;
#: 它们只会被那层纹波污染。
MODULATION_OFF_TAGS: frozenset = frozenset({
    "scan", "tip", "shaper", "pulse", "motor", "coarse", "approach", "conditioning",
})

#: 明确**要用** lock-in 的技能名子串 —— 它们自己开自己关,这里一律不插手。
#: (子串匹配,与仓里另外两张表同款;lockin/didv/sts/spectr 都在。)
MODULATION_USER_PATTERNS: frozenset = frozenset({
    "lockin", "didv", "autophase", "sts", "spectr", "sweep",
})


#: 明确**保留调制**的技能名子串(依据在下)。
#:
#: 与 ``MODULATION_USER_PATTERNS`` **刻意是两张表**:那张表说的是「它拿 lock-in 的
#: 解调输出当判据」,这张表说的是「它的判据不用 lock-in,但**用户要在它期间用串扰
#: 读数导航**」。合成一张就等于把两个不同的理由塞进一个名字,而下一个人会按错的那个
#: 理由增删条目。
#:
#: **退针为什么可以留着调制**:
#: * 方向安全 —— 退针是离开表面,纹波污染判据的代价上限比进针/扫图小得多;
#: * 误报有监控侧兜底 —— `ctx_lockin_on` 上下文在退针场景下能抑制
#:   jump_burst / spike_warn / critical 的误报;
#: * 价值 —— 用串扰曲线导航退针能明显提高效率。
#:
#: **要把这条反转回来需要什么**:退针期间调制开着时 `jump_burst` / `spike_warn`
#: 重新刷屏或出现误弹。那说明监控侧的抑制在这个场景不够,导航要么放弃、要么另想办法。
#: (备注:支撑这条的样本不厚。)
MODULATION_KEEP_PATTERNS: frozenset = frozenset({
    "retractforsamplechange",
})


def keeps_modulation(skill_name: str) -> bool:
    """这个技能是不是「判据不用 lock-in,但要留着调制给人导航」的那一类。"""
    low = str(skill_name or "").lower()
    return any(p in low for p in MODULATION_KEEP_PATTERNS)


def uses_lockin(skill_name: str, meta=None) -> bool:
    """这个技能是不是自己要用 lock-in 信号(⇒ 别替它关调制)。"""
    low = str(skill_name or "").lower()
    if any(p in low for p in MODULATION_USER_PATTERNS):
        return True
    tags = {str(t).lower() for t in (getattr(meta, "tags", None) or ())}
    return bool(tags & {"lockin", "didv", "sts", "spectroscopy"})


def wants_modulation_off(meta) -> bool:
    """这个技能开跑前该不该确保调制是关的。

    只对**写**类技能,且它自己不用 lock-in,且它的标签落在那几族物理动作里。
    三个条件都要 —— 读类技能不改仪器状态,凭什么替用户关他的调制。
    """
    if meta is None or uses_lockin(getattr(meta, "name", ""), meta):
        return False
    if keeps_modulation(getattr(meta, "name", "")):
        return False        # 判据不用 lock-in,但用户要在它期间用串扰导航
    category = str(getattr(getattr(meta, "category", None), "value", "")).lower()
    if category not in ("write", "composite"):
        return False
    tags = {str(t).lower() for t in (getattr(meta, "tags", None) or ())}
    return bool(tags & MODULATION_OFF_TAGS)


def ensure_modulation_off(ctx, *, skill_name: str = "") -> "dict | None":
    """调制开着就关掉,并返回留痕;本来就关着(或读不到)返回 ``None``。

    **读不到 = 不动手。** 「没读到调制状态」不等于「它开着」,更不等于「该关」——
    在一个读不回状态的机器上每次都盲发一条关调制命令,是拿一个未知去换一个写操作。
    这与 ``ctx_lockin_on`` 的极性一致(只有显式 True 才行动)。

    永不抛异常:这是开跑前的卫生动作,不是这个技能的目的;它失败不该把技能带走。
    """
    # 读状态用 _read_mod_on:回包解析(含 §2.21 的 1-元组形态)只有一份。
    # 两份解析里被修好的永远只有一份 —— 而另一份不会报错,它会给出一个看着正常的
    # 错答案。这里三态里只有**显式 True** 才动手。
    if _read_mod_on(ctx) is not True:
        return None                    # 读不到(不动手)或本来就关着

    try:
        # 只关调制。**不传 amplitude/frequency/phase** —— 关它不需要重写那些值,
        # 而多传一个就是多一次「省略 vs 显式」的机会(见 lockin.py 的默认值那条)。
        off = ctx.safe_call("LockIn_ModOnOffSet", 1, 0)
        if getattr(off, "error", ""):
            logger.warning("modulation preflight: 关调制失败:%s", off.error)
            return {"modulation_was_on_turned_off": False,
                    "modulation_note": tr(
                        f"检测到调制开着,但关不掉:{off.error}",
                        f"Modulation was found on but could not be turned off: {off.error}")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("modulation preflight: 关调制抛异常:%s", exc)
        return None

    logger.info("modulation preflight: %s 开跑前把 lock-in 调制关掉了", skill_name)
    return {
        "modulation_was_on_turned_off": True,
        "modulation_note": tr(
            f"开跑前检测到 lock-in 调制是开着的,已自动关闭 —— {skill_name} "
            "不使用 lock-in 信号,而调制会在电流通道上叠一层纹波,污染这条流程的"
            "所有判据。需要 dI/dV 时请显式重新打开(ApplyLockInPreset)。",
            f"Lock-in modulation was on before the run and has been turned off "
            f"automatically - {skill_name} does not use the lock-in signal, and "
            "modulation adds ripple on the current channel that contaminates every "
            "criterion in this procedure. Turn it back on explicitly (ApplyLockInPreset) "
            "when dI/dV is needed."),
    }


def close_modulation(ctx, *, skill_name: str = "",
                     calls: "list | None" = None) -> "dict":
    """正常结束后按策略关闭调制；与进入前保证调制关闭的动作分别记录。"""
    note: "dict" = {"modulation_closed_by": skill_name or "(unnamed)"}
    before = _read_mod_on(ctx, calls=calls)
    note["modulation_was_on_before"] = before
    try:
        # 只关调制。**不传 amplitude/frequency/phase** —— 曾有缺陷是关的时候顺手
        # 带了个省略的幅度,把 0.02 V 清成 0。关它不需要重写任何值。
        off = ctx.safe_call("LockIn_ModOnOffSet", 1, 0)
        if calls is not None:
            calls.append(off)
        if getattr(off, "error", ""):
            logger.warning("%s: 收尾关调制失败:%s", skill_name, off.error)
            note["modulation_off_after"] = False
            note["modulation_note"] = tr(
                f"用完 lock-in 后关调制**失败**:{off.error}。调制可能仍开着 —— "
                "它会在电流通道上叠一层纹波,污染后面每一条判据。请手动关闭。",
                f"Turning modulation off after the lock-in use **failed**: {off.error}. "
                "Modulation may still be on - it adds ripple on the current channel "
                "and contaminates every later criterion. Turn it off manually.")
            return note
    except Exception as exc:  # noqa: BLE001 — 收尾动作不该把技能带走
        logger.warning("%s: 收尾关调制抛异常:%s", skill_name, exc)
        note["modulation_off_after"] = None
        note["modulation_note"] = tr(
            f"用完 lock-in 后关调制时出错:{exc};状态未知。",
            f"Error turning modulation off after the lock-in use: {exc}; state unknown.")
        return note

    after = _read_mod_on(ctx, calls=calls)
    note["modulation_off_after"] = None if after is None else (not after)
    if after is None:
        note["modulation_note"] = tr(
            "已发出关调制命令,但回读不到调制状态 —— **没确认**它关上了。"
            "「写进去了」和「我们看见它在里面」是两句话。",
            "The modulation-off command was sent, but the modulation state cannot be "
            "read back - it is **not confirmed** to be off. \"Written\" and \"seen "
            "to be in effect\" are two different statements.")
    elif after:
        note["modulation_note"] = tr(
            "发出了关调制命令,回读却仍是开着 —— **不要按已关闭继续**,请手动确认。",
            "The modulation-off command was sent, but the readback still shows it on - "
            "**do not proceed as if it were off**; confirm manually.")
    else:
        note["modulation_note"] = tr(
            f"{skill_name} 用完 lock-in,已把调制关回 OFF(幅度/频率原样保留)。"
            "要接着做 dI/dV,请先用 ApplyLockInPreset 重新打开调制 —— "
            "调制关着时 lock-in 通道上没有信号,而曲线照样画得出来。",
            f"{skill_name} is done with the lock-in and turned modulation back OFF "
            "(amplitude/frequency left unchanged). To continue with dI/dV, re-enable "
            "modulation with ApplyLockInPreset first - with modulation off the lock-in "
            "channel carries no signal, yet a curve can still be drawn.")
    return note


def _read_mod_on(ctx, *, calls: "list | None" = None) -> "bool | None":
    """调制开着没有:True / False / **None(读不到)**。永不抛异常。

    三态是必须的 —— 「没读到」被当成「关着」的话,收尾留痕就会说一句它没有依据的话。
    """
    try:
        rec = ctx.safe_call("LockIn_ModOnOffGet", 1)
        if calls is not None:
            calls.append(rec)
        if getattr(rec, "error", ""):
            return None
        rv = getattr(rec, "return_value", None)
        vals = rv[2] if isinstance(rv, (list, tuple)) and len(rv) > 2 else None
        raw = None
        if isinstance(vals, (list, tuple)) and vals:
            raw = vals[0]
            if isinstance(raw, (list, tuple)) and raw:   # 1-元组形态(§2.21)
                raw = raw[0]
        elif isinstance(vals, (int, float)):
            raw = vals
        if raw is None:
            return None
        return bool(int(raw))
    except Exception:  # noqa: BLE001
        logger.debug("读调制开关状态失败(按未知处理)", exc_info=True)
        return None

# 进针前可切换到 approach 增益组，结束、失败和中止都恢复本流程修改的状态。
# 这与未修改调制时的结束策略不同：是否恢复取决于是否由本流程改动。
# 读不到原值时不切换，因为无法保证恢复。
# GetZCtrlGain 的输出键与 SetZCtrlGain 参数名由接口契约测试校验。
_ZCTRL_GAIN_KEYS: tuple = ("p_gain", "time_constant_s", "i_gain")


def _read_zctrl_gains(ctx) -> "dict[str, float] | None":
    """当前 P/T/I 三个数;读不到返回 ``None``(与「读到了 0」必须分得开)。"""
    try:
        res = ctx.run("GetZCtrlGain", {})
    except Exception:  # noqa: BLE001
        logger.debug("读 Z 控制器增益失败(按读不到处理)", exc_info=True)
        return None
    if not getattr(res, "success", False):
        return None
    data = getattr(res, "data", None) or {}
    out: "dict[str, float]" = {}
    for key in _ZCTRL_GAIN_KEYS:
        v = data.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None            # 缺一个就是读不到,不拿两个数凑一组
        out[key] = float(v)
    return out


def _read_setpoint(ctx) -> "float | None":
    try:
        res = ctx.run("GetSetpoint", {})
    except Exception:  # noqa: BLE001
        logger.debug("读设定点失败(按读不到处理)", exc_info=True)
        return None
    if not getattr(res, "success", False):
        return None
    v = (getattr(res, "data", None) or {}).get("setpoint_a")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def apply_approach_preset(ctx, *, skill_name: str = "") -> "tuple[dict | None, dict]":
    """进针前切到 approach 参数组。返回 ``(回滚快照, 留痕)``。

    快照为 ``None`` 表示**没有要放回去的东西**(没切:档案没配 / 读不到 / 本来就是
    那组值)。走 ``ApplyZCtrlPreset`` 正门 ⇒ 一个数字都不经过这里,也不经过模型。

    永不抛异常:切参数组是为了让进针快一点,它失败不该把进针带走。
    """
    note: "dict" = {"zctrl_preset": "approach", "zctrl_preset_applied": False}
    try:
        from mast.core import zctrl_presets as zp

        try:
            preset = zp.resolve(zp.PRESET_APPROACH)
        except Exception as exc:  # noqa: BLE001 — PresetRejected 及其它
            # 档案没配 ⇒ **如实跳过**。这里绝不能拿一组「常见值」顶上:进针参数是
            # 用户输入的,编一个出来会以本机标定的名义跑一次真实的进针。
            note["zctrl_preset_note"] = tr(
                f"仪器档案里没有可用的进针参数组,本次进针**沿用当前 Z 控制器增益**"
                f"(即改动前的行为):{exc}",
                f"No usable approach parameter set in the instrument profile; this "
                f"approach **keeps the current Z controller gains** (the behaviour "
                f"before this change): {exc}")
            return None, note

        before = _read_zctrl_gains(ctx)
        if before is None:
            note["zctrl_preset_note"] = tr(
                "读不到当前 Z 控制器增益 —— **不切换**,进针沿用当前增益。"
                "(能不能把它放回去,取决于切换前读到了什么;读不到就切,等于拿一个"
                "没人会知道来历的永久改动换一点速度。)",
                "Cannot read the current Z controller gains - **not switching**; the "
                "approach keeps the current gains. (Whether they can be restored "
                "depends on what was read before switching; switching blind would "
                "trade a bit of speed for a permanent change of unknown origin.)")
            return None, note
        note["zctrl_before"] = dict(before)

        want = preset.gain_params()
        same = all(values_match(want[k], before[k])[0] for k in _ZCTRL_GAIN_KEYS)

        sp_before = None
        if preset.setpoint_a is not None:
            sp_before = _read_setpoint(ctx)
            if sp_before is None:
                note["zctrl_preset_note"] = tr(
                    "进针参数组要改设定点,但读不到当前设定点 —— **不切换**"
                    "(放不回去就不该改)。",
                    "The approach parameter set changes the setpoint, but the current "
                    "setpoint cannot be read - **not switching** (what cannot be "
                    "restored should not be changed).")
                return None, note
            same = same and values_match(preset.setpoint_a, sp_before)[0]

        if same:
            # 用户可能刚人肉切过,或者这是嵌套调用(ApproachTip → AutoApproach)。
            # 不写 = 不用放回去,嵌套因此是免费的。
            note["zctrl_preset_applied"] = None
            note["zctrl_preset_note"] = tr(
                "当前已经是进针参数组的值,未改动。",
                "Already at the approach parameter set values; nothing changed.")
            return None, note

        res = ctx.run("ApplyZCtrlPreset", {"preset": zp.PRESET_APPROACH})
        ok = bool(getattr(res, "success", False))
        note["zctrl_preset_applied"] = ok
        note["zctrl_preset_trace"] = (getattr(res, "data", None) or {}).get("trace")
        if not ok:
            note["zctrl_preset_note"] = tr(
                f"切进针参数组失败:{getattr(res, 'error', 'unknown')};"
                "进针继续,但用的是切换失败后的增益 —— 结束时仍会尝试放回原值。",
                f"Switching to the approach parameter set failed: "
                f"{getattr(res, 'error', 'unknown')}; the approach continues with the "
                "gains left after the failed switch - restoring the original values "
                "will still be attempted at the end.")
        else:
            note["zctrl_preset_note"] = tr(
                f"{skill_name} 开跑前已切到进针参数组(值来自仪器档案,不经模型);"
                "结束时会放回调用前的增益。",
                f"{skill_name} switched to the approach parameter set before the run "
                "(values come from the instrument profile, not from a model); the "
                "pre-call gains are restored at the end.")
        # **失败也返回快照**:ApplyZCtrlPreset 可能已经写进了增益、随后在设定点上
        # 失败。「调用失败」不等于「什么都没改」。
        return {"gains": before, "setpoint_a": sp_before}, note
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: 切进针参数组时出错:%s", skill_name, exc)
        note["zctrl_preset_note"] = tr(
            f"切进针参数组时出错:{exc};进针沿用当前增益。",
            f"Error switching to the approach parameter set: {exc}; the approach "
            "keeps the current gains.")
        return None, note


def _operator_stopped(ctx) -> bool:
    """用户喊停了没有。读不到通道就是 False(fail-open)。永不抛异常。"""
    check = getattr(ctx, "check_abort", None)
    try:
        return bool(callable(check) and check())
    except Exception:  # noqa: BLE001
        return False


def restore_zctrl(ctx, snapshot: "dict | None", *, skill_name: str = "") -> "dict":
    """把 Z 控制器放回 ``snapshot`` 记下的值。没有快照就什么都不做。

    ``SetZCtrlGain`` / ``SetSetpoint`` 自己会写后回读比对,所以它们的 success
    已经是「硬件确实收下了」而不是「命令发出去了」。永不抛异常。
    """
    if not snapshot:
        return {}
    note: "dict" = {}
    ok = True
    try:
        gains = snapshot.get("gains")
        if gains:
            res = ctx.run("SetZCtrlGain", dict(gains))
            ok = bool(getattr(res, "success", False))
            note["zctrl_restored_gains"] = dict(gains)
            if not ok:
                note["zctrl_restore_error"] = getattr(res, "error", "unknown")
        sp = snapshot.get("setpoint_a")
        if sp is not None and _operator_stopped(ctx):
            # 软停时**不还设定点**(软停只停不动)。
            #
            # 增益和设定点在这里不是一回事:改增益只是改反馈环的响应,**不命令任何
            # 位移**;而改设定点会让 Z 环把针尖挪到新的电流目标上 —— 那是一次运动。
            # 一个刚被叫停的流程不该以「归还」的名义再动一次针。
            #
            # 于是软停后现场是:增益=调用前,设定点=进针组的值。这个组合要说出来,
            # 别让人以为一切都回去了。
            note["zctrl_setpoint_left_at_approach"] = True
            note["zctrl_setpoint_note"] = tr(
                f"用户叫停 —— **没有还原设定点**(还原会让针尖移动,而软停只停不动)。"
                f"当前设定点仍是进针组的值;调用前是 {sp}。要还原请显式 SetSetpoint。",
                f"Stopped by the user - **setpoint NOT restored** (restoring would move "
                f"the tip, and a soft stop only stops, it does not move). The current "
                f"setpoint is still the approach-set value; before the call it was {sp}. "
                f"Restore it explicitly with SetSetpoint.")
            sp = None
        if sp is not None:
            res_sp = ctx.run("SetSetpoint", {"setpoint_a": sp})
            if not getattr(res_sp, "success", False):
                ok = False
                note["zctrl_restore_error"] = getattr(res_sp, "error", "unknown")
            note["zctrl_restored_setpoint_a"] = sp
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: 放回 Z 控制器参数时出错:%s", skill_name, exc)
        note["zctrl_restored"] = None
        note["zctrl_restore_note"] = tr(
            f"放回调用前的 Z 控制器参数时出错:{exc} —— **增益可能仍停在进针组**,"
            "请核对后再扫图(进针组的快增益会一路带进成像)。",
            f"Error restoring the pre-call Z controller parameters: {exc} - **the "
            "gains may still be at the approach set**; check before scanning (the fast "
            "approach gains would otherwise carry over into imaging).")
        return note
    note["zctrl_restored"] = ok
    note["zctrl_restore_note"] = (
        tr(f"{skill_name} 结束,Z 控制器已放回调用前的值。",
           f"{skill_name} finished; the Z controller was restored to its pre-call values.")
        if ok else
        tr("放回调用前的 Z 控制器参数**失败** —— 增益可能仍停在进针组,"
           "请核对后再扫图(进针组的快增益会一路带进成像)。",
           "Restoring the pre-call Z controller parameters **failed** - the gains may "
           "still be at the approach set; check before scanning (the fast approach "
           "gains would otherwise carry over into imaging)."))
    return note


__all__ += ["MODULATION_OFF_TAGS", "MODULATION_USER_PATTERNS",
            "MODULATION_KEEP_PATTERNS", "keeps_modulation",
            "uses_lockin", "wants_modulation_off", "ensure_modulation_off",
            "close_modulation", "apply_approach_preset", "restore_zctrl"]
