"""Spectroscopy skills: STS (Bias Spectroscopy) and Z Spectroscopy.

vendored from v1 mast/skills/builtins/spectroscopy.py 2026-04-23. Zero behavioural changes.
38 skills: AcquireSTS, ConfigureSTS, ConfigureZSpectr, AcquireZSpectr,
           ConfigureSTSTiming, StopSTS, StopZSpectr, ConfigureSTSChannels,
           ConfigureZSpectrTiming, GetSTSChannels, SetSTSChannels, GetSTSLimits,
           SetSTSAdvancedProps, GetSTSTiming, GetSTSAltZCtrl,
           GetZSpectrChannels, SetZSpectrChannels, GetZSpectrRange, SetZSpectrRange,
           GetZSpectrRetract, SetZSpectrRetract,
           GetSTSDigSync, GetSTSTTLSync, GetSTSPulseSeqSync, GetSTSZOffRevert,
           GetSTSMLSLockinPerSeg, SetSTSMLSMode, SetSTSMLSVals,
           SetSTSSafeCond1, GetSTSSafeCond1, SetSTSSafeCond2,
           SetZSpectrAdvProps, GetZSpectrDigSync, GetZSpectrPulseSeqSync,
           GetZSpectrRetract2nd, SetZSpectrRetractDelay, GetZSpectrTTLSync,
           GetZSpectrTiming.
"""

from __future__ import annotations

import logging
import time

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.io.nanonis_files import channel_ids_from_buffer
from mast.skills.base import BaseSkill
from mast.i18n import tr


def _reshape_spectrum(variables):
    """将 BiasSpectr/ZSpectr.Start 的 Variables 解析成通道到完整谱线的映射。
    
    二维布局为行=通道、列=扫掠点；转置会混合不同通道。
    返回 (channel_map, num_points, reason)，成功时 reason 为空。
    解码失败必须带原因，与一条有效但零点数的谱明确区分，不能静默缺少字段。"""
    if not (isinstance(variables, (list, tuple)) and len(variables) >= 6):
        n = len(variables) if isinstance(variables, (list, tuple)) else tr("非序列", "not a sequence")
        return {}, 0, tr(f"Variables 不足 6 项(实际 {n}),不是一个谱数据块", f"Variables has fewer than 6 items (actual {n}), not a spectrum data block")
    ch_names = variables[2] if isinstance(variables[2], list) else []
    data_2d = variables[5]
    try:
        rows = int(variables[3])  # rows = channels
        cols = int(variables[4])  # cols = sweep points
    except (TypeError, ValueError):
        return {}, 0, (tr(f"行列数不是整数(rows={variables[3]!r}, "
                       f"cols={variables[4]!r})", f"Row/column counts are not integers (rows={variables[3]!r}, cols={variables[4]!r})"))
    if hasattr(data_2d, "tolist"):
        data_2d = data_2d.tolist()
    if not (isinstance(data_2d, list) and rows > 0 and cols > 0):
        return {}, 0, (tr(f"数据块不是列表或行列数非正(rows={rows}, cols={cols}, "
                       f"data={type(data_2d).__name__})", f"Data block is not a list or row/column counts are not positive (rows={rows}, cols={cols}, data={type(data_2d).__name__})"))
    import numpy as np
    try:
        arr = np.array(data_2d, dtype=np.float64).reshape(rows, cols)
    except (ValueError, TypeError) as exc:
        return {}, 0, tr(f"{rows}×{cols} 装不下这段数据: {exc}", f"{rows}x{cols} cannot hold this data: {exc}")
    channel_map: dict = {}
    for i, name in enumerate(ch_names[:rows]):
        channel_map[name] = arr[i, :].tolist()
    if not channel_map:
        return {}, 0, tr(f"数据解开了({rows}×{cols}),但一个通道名都没有", f"Data was unpacked ({rows}x{cols}), but there is not a single channel name")
    return channel_map, cols, ""


def _match_channel(channel_map: dict, needles: tuple[str, ...]):
    """First channel whose (lower-cased) name contains any needle, else None."""
    for name, trace in channel_map.items():
        low = str(name).lower()
        if any(n in low for n in needles):
            return trace
    return None


def _coerce_int_list(value) -> list[int]:
    """Coerce a list-valued parameter to ``list[int]``.

    The agent tool-schema builder (``agents/_shared/skill_adapter._TYPE_MAP``)
    only understands the scalar ParameterSpec types int/float/str/bool, so a
    ``list[int]`` spec is silently presented to the LLM as a *string*. Declaring
    these params as ``str`` (comma-separated) and coercing here makes the skill
    work over BOTH paths: agent (arrives as "0, 1, 2") and direct/programmatic
    (arrives as a real ``[0, 1, 2]`` list).
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [int(float(x.strip())) for x in value.replace(";", ",").split(",") if x.strip()]
    if isinstance(value, (list, tuple)):
        return [int(x) for x in value]
    return [int(value)]


def _coerce_float_list(value) -> list[float]:
    """Coerce a list-valued parameter to ``list[float]`` (see _coerce_int_list)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [float(x.strip()) for x in value.replace(";", ",").split(",") if x.strip()]
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    return [float(value)]


logger = logging.getLogger(__name__)


#: 幂等调用在传输层失败后、重做之前停多久（秒）。
#:
#: 曾出现过：ConfigureSTS 的 ``LimitsSet`` 超时 → 连接池重连并原样重发 →
#: 重发在替换连接上被控制器重置（它还在忙上一条）→ 技能失败；**20 ms 后**另一个
#: 调用方在同一角色上重连一次就成功了。所以「隔一会儿再做一次」能救回来，而
#: 连接池自己那一次重发救不回来。停一秒是给控制器把手头那条做完。
_IDEMPOTENT_RETRY_PAUSE_S = 1.0


def _call_idempotent(calls: list, call):
    """Run ``call`` — a thunk holding ONE literal ``context.safe_call("…")`` of an
    IDEMPOTENT command — and run it once more after a transport-level failure;
    every attempt goes into ``calls``.

    A thunk, not a verb argument: the verb has to stay a literal inside
    ``safe_call(...)``, because the abort-policy checker, the security audit and
    the coverage census all find Nanonis commands by grepping for exactly that
    (``tests/v2/unit/core/test_safe_call_verbs_are_literal.py``).

    **Only for commands that set a property to an absolute value, or read one**
    (``*_Open``, ``*_LimitsSet``, ``*_PropsSet``, ``*_PropsGet``…): sending them
    twice leaves the controller exactly where once would. Never for ``*_Start``,
    motion, pulses — a resend there can run the action twice.

    Only a record the real connection pool flagged as a transport failure
    (``connection.is_transport_failure``) is retried; an application error (a
    locked parameter, a value out of range) comes back as it is, because doing
    it again cannot change the answer. Records from anything else (the abort
    gate, test doubles) carry no flag and pass straight through."""
    from mast.core.connection import is_transport_failure

    rec = call()
    calls.append(rec)
    if not (getattr(rec, "error", "") and is_transport_failure(rec)):
        return rec
    logger.warning("%s failed at the transport level (%s); retrying once in %.1f s",
                   getattr(rec, "method", "?"), rec.error, _IDEMPOTENT_RETRY_PAUSE_S)
    time.sleep(_IDEMPOTENT_RETRY_PAUSE_S)
    again = call()
    calls.append(again)
    if getattr(again, "error", ""):
        again.error = (f"{again.error} (retried once after a transport failure; "
                       f"first attempt: {rec.error})")
    return again


def _attach_saved_dat(context, data: dict) -> None:
    """Put the .dat Nanonis just autosaved into ``data["path"]``.

    WHY: ConfigureSTS turns autosave on, so every
    acquisition DOES land a .dat on disk — but the skill never reported where,
    so ``data`` carried no ``path``. The tool adapter only harvests
    ``path``/``file_path``/``sxm_path`` into ``scan_paths``, so the handoff to
    data_processing had no real file to point at. All it could offer was the
    tool-return sidecar (a summary-overflow dump), which data_processing then
    fed to ``load_scan`` — and that crashed.

    Nanonis picks the directory and the index itself, so the only way to name
    the file is to look for the newest .dat right after the sweep. Best-effort
    by design: no .dat (autosave off) simply means no ``path`` — never a guess,
    because a wrong path here points the analysis agent at someone else's data.
    """
    try:
        from mast.skills.builtins.scan_extra import (
            _candidate_save_dirs,
            find_latest_saved,
        )

        latest = find_latest_saved(_candidate_save_dirs(context), "*.dat",
                                   max_age_s=120)
        if latest is None:
            return
        data["path"] = str(latest)
        try:
            from mast.core.scan_registry import record_scan_path
            record_scan_path(latest)
        except Exception:  # noqa: BLE001 — registry is best-effort
            pass
    except Exception as exc:  # noqa: BLE001 — never fail an acquisition over this
        logger.debug("AcquireSTS: could not resolve autosaved .dat: %s", exc)

# 扫掠接收预算包含采集比例余量和存盘等固定开销。
# 预算不足会导致迟到回包破坏后续流同步；预算更长则延后故障返回。
_SWEEP_BUDGET_FACTOR = 1.35
_SWEEP_BUDGET_PAD_S = 45.0
#: 读不到设定时的回退。**「读不到」不是「很快」**——退回一个小数字正是
#: 这个 bug 的形状，所以这里退到上限那一侧。
_SWEEP_BUDGET_FALLBACK_S = 600.0


def _sweep_duration_s(context, calls: list) -> tuple[float, dict]:
    """从仪器**自己的设定**算一条谱要跑多久（秒），外加算给谁看的明细。

    不写死数字：用户随时会改点数 / 积分时间 / sweep 数，写死的预算下一次
    就又不够。这两次读是 ~50 ms 的代价，换的是这个技能对任何配置都成立。

        时长 ≈ 点数 × (settling + integration) × (往返 ? 2 : 1) × sweep 数
               + sweep 数 × (initial_settling + end_settling + z_control + z_avg)
    """
    npts = nsw = None
    bwd = True
    rec = _call_idempotent(calls, lambda: context.safe_call("BiasSpectr_PropsGet"))
    v = rec.return_value
    var = v[2] if isinstance(v, (list, tuple)) and len(v) > 2 else None
    # BiasSpectr.PropsGet Variables =
    #   [Save all, Number of sweeps, Backward sweep, Number of points, ...]
    if isinstance(var, (list, tuple)) and len(var) >= 4:
        try:
            nsw = max(int(var[1]), 1)
            bwd = bool(int(var[2]))
            npts = max(int(var[3]), 1)
        except (TypeError, ValueError):
            npts = nsw = None

    t: dict = {}
    rec2 = _call_idempotent(calls, lambda: context.safe_call("BiasSpectr_TimingGet"))
    v2 = rec2.return_value
    var2 = v2[2] if isinstance(v2, (list, tuple)) and len(v2) > 2 else None
    keys = ["z_averaging_time_s", "z_offset_m", "initial_settling_time_s",
            "max_slew_rate_v_per_s", "settling_time_s", "integration_time_s",
            "end_settling_time_s", "z_control_time_s"]
    if isinstance(var2, (list, tuple)):
        for i, k in enumerate(keys):
            if i < len(var2):
                try:
                    t[k] = float(var2[i])
                except (TypeError, ValueError):
                    pass

    if npts is None or nsw is None or "integration_time_s" not in t:
        return _SWEEP_BUDGET_FALLBACK_S, {
            "why": tr("读不到扫掠设定，退回保守上限", "Could not read the sweep settings, falling back to a conservative limit"),
            "npts": npts, "nsweeps": nsw, "timing_keys": sorted(t),
        }
    per_pt = t.get("settling_time_s", 0.0) + t["integration_time_s"]
    per_sweep = (t.get("initial_settling_time_s", 0.0)
                 + t.get("end_settling_time_s", 0.0)
                 + t.get("z_control_time_s", 0.0)
                 + t.get("z_averaging_time_s", 0.0))
    core = npts * per_pt * (2 if bwd else 1) * nsw + per_sweep * nsw
    return core, {"npts": npts, "nsweeps": nsw, "backward": bwd,
                  "per_point_s": round(per_pt, 4), "core_s": round(core, 1)}


def _plain_int(v) -> "int | None":
    """An int field of a decoded reply, bare or as a one-element sequence (Nanonis array
    fields arrive as tuples of 1-tuples, scalars sometimes as one-element lists)."""
    while isinstance(v, (list, tuple)) and len(v) == 1:
        v = v[0]
    if isinstance(v, bool):
        return int(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _read_current_num_sweeps(context, calls: list) -> "int | None":
    """当前 BiasSpectr 的扫掠数（读不到给 ``None``）。

    ``BiasSpectr.PropsGet`` 的 Variables = [Save all, **Number of sweeps**,
    Backward sweep, Number of points, …]（与 :func:`_sweep_duration_s` 同一份回包、
    同一处取法：``var[1]``）。``ConfigureSTS`` 用它在「调用方没显式给扫掠数」时**保持
    操作员原来的设置**，而不是把它打回 1。

    ⚠️ 读不到时返回 ``None``（不是 1）—— 把「读不到」折成一个具体值正是
    一类典型的错误形状。调用方拿到 ``None`` 时自己决定怎么办（见 ConfigureSTS）。
    """
    rec = _call_idempotent(calls, lambda: context.safe_call("BiasSpectr_PropsGet"))
    v = rec.return_value
    var = v[2] if isinstance(v, (list, tuple)) and len(v) > 2 else None
    if isinstance(var, (list, tuple)) and len(var) >= 2:
        n = _plain_int(var[1])
        if n is not None and n >= 1:
            return n
    return None


def _abort_fn(context):
    """操作员中止回调（``context.check_abort``），拿不到就给 ``None``。"""
    a = getattr(context, "check_abort", None)
    return a if callable(a) else None


def _read_sweep_start_v(context, calls: list):
    """测谱**起始偏压**（``BiasSpectr.LimitsGet`` 的 start），读不到给 ``(None, 原因)``。

    Variables = [start_v, end_v]（与 ``GetSTSLimits`` 同一处取法）。``AcquireSTS`` 的
    ``ramp_before`` 用它当「要 ramp 到的测谱偏压」—— 不另加一个偏压目标参数，起始偏压
    就是 ConfigureSTS 已经配好的那个。
    """
    rec = _call_idempotent(calls, lambda: context.safe_call("BiasSpectr_LimitsGet"))
    v = rec.return_value
    var = v[2] if isinstance(v, (list, tuple)) and len(v) > 2 else None
    if isinstance(var, (list, tuple)) and len(var) >= 1:
        s = var[0]
        while isinstance(s, (list, tuple)) and len(s) == 1:
            s = s[0]
        try:
            return float(s), ""
        except (TypeError, ValueError):
            return None, tr(f"start 不是数: {s!r}", f"start is not a number: {s!r}")
    return None, (getattr(rec, "error", "") or tr("BiasSpectr_LimitsGet 回包里没有上下限",
                                                  "BiasSpectr_LimitsGet reply has no limits"))


def _ramp_working_point(context, calls: list, *, bias_to, setpoint_to,
                        slew_v_per_s, abort=None) -> dict:
    """把偏压（和可选的电流 setpoint）从**当前读到的值**分步 ramp 到目标值。

    返回 ``{"ok", "error", "bias_from", "setpoint_from"}``。``bias_from`` /
    ``setpoint_from`` 是 ramp **开始前读到的当前值**，供调用方做测谱后的还原快照
    （还原成读到的值，不是写死值）。

    ── 安全模式（复刻 ``bias.SetBias``）─────────────────────────────────────
    * ramp 前必须读到当前偏压，**读不到就拒绝**——绝不从臆断的 0 V 起步，那正是
      slew 要防的那一跳。
    * 每步 ``Bias_Set``(+``ZCtrl_SetpntSet``) 后停 100 ms，整个过程可被操作员中止
      （停在两个都过了边界检查的中间值上是安全的）。
    * ``slew_v_per_s`` 为空 / ≤0 时走**瞬时设定**，与 ``SetBias`` 的默认一致
      （ramp 速率沿用 SetBias 默认）。
    * 偏压与 setpoint **同步**走同样的步数（一个平滑过渡），feedback 全程是调用方
      的当前状态（成像态 ON）——随后才由 ConfigureSTS 的 Z-hold 进入扫掠。

    ``setpoint_to is None`` ⇒ 只 ramp 偏压、不碰 setpoint；但无论如何都读一次当前
    setpoint 放进 ``setpoint_from``（还原快照要用）。
    """
    import numpy as np

    def _read_scalar(call):
        # ``call`` 是一个 thunk，里面是**字面量** ``context.safe_call("…")``：verb 必须留在
        # safe_call(...) 里的字面量上，否则 abort 检查 / 安全审计 / 覆盖普查都 grep 不到它
        # （tests/v2/unit/core/test_safe_call_verbs_are_literal.py）。同文件 _call_idempotent
        # 用的是同一套路。
        rec = call()
        calls.append(rec)
        rv = getattr(rec, "return_value", None)
        if not getattr(rec, "error", "") and isinstance(rv, (list, tuple)) and len(rv) > 2:
            x = rv[2]
            while isinstance(x, (list, tuple)) and len(x) == 1:
                x = x[0]
            if isinstance(x, (list, tuple)) and x:
                x = x[0]
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        return None

    v0 = _read_scalar(lambda: context.safe_call("Bias_Get"))
    if v0 is None:
        return {"ok": False, "bias_from": None, "setpoint_from": None,
                "error": tr("读不到当前偏压(Bias_Get)，拒绝 ramp——不从臆断的 0 V 起步。",
                            "Could not read the current bias (Bias_Get); refusing to ramp — "
                            "will not start from an assumed 0 V.")}
    i0 = _read_scalar(lambda: context.safe_call("ZCtrl_SetpntGet"))
    if setpoint_to is not None and i0 is None:
        return {"ok": False, "bias_from": v0, "setpoint_from": None,
                "error": tr("读不到当前电流 setpoint(ZCtrl_SetpntGet)，而本次要 ramp setpoint，拒绝。",
                            "Could not read the current setpoint (ZCtrl_SetpntGet) while a "
                            "setpoint ramp was requested; refusing.")}

    def _set_once(bv, sv):
        r1 = context.safe_call("Bias_Set", float(bv))
        calls.append(r1)
        if getattr(r1, "error", ""):
            return f"Bias_Set @ {bv:.4f} V: {r1.error}"
        if sv is not None:
            r2 = context.safe_call("ZCtrl_SetpntSet", float(sv))
            calls.append(r2)
            if getattr(r2, "error", ""):
                return f"ZCtrl_SetpntSet @ {sv:.3e} A: {r2.error}"
        return ""

    # 瞬时：无 slew，或偏压变化 < 1 mV（与 SetBias 同一门槛）。
    if not (slew_v_per_s and slew_v_per_s > 0) or abs(float(bias_to) - v0) <= 0.001:
        err = _set_once(bias_to, setpoint_to)
        return {"ok": not err, "error": err, "bias_from": v0, "setpoint_from": i0}

    step_interval = 0.1
    n_steps = max(1, int(abs(float(bias_to) - v0) / (slew_v_per_s * step_interval)))
    bias_steps = np.linspace(v0, float(bias_to), n_steps + 1)[1:]
    if setpoint_to is not None and i0 is not None:
        setp_steps = list(np.linspace(i0, float(setpoint_to), n_steps + 1)[1:])
    else:
        setp_steps = [None] * n_steps
    for i, (bv, sv) in enumerate(zip(bias_steps, setp_steps)):
        if callable(abort) and abort():
            return {"ok": False, "bias_from": v0, "setpoint_from": i0,
                    "error": tr(f"operator 在 ramp 中途中止(第 {i}/{n_steps} 步，偏压 {bv:.4f} V)；"
                                f"工作点停在安全中间值。",
                                f"aborted by operator mid-ramp (step {i}/{n_steps}, bias {bv:.4f} V); "
                                f"working point left at a safe intermediate value.")}
        err = _set_once(bv, sv)
        if err:
            return {"ok": False, "error": err, "bias_from": v0, "setpoint_from": i0}
        time.sleep(step_interval)
    return {"ok": True, "error": "", "bias_from": v0, "setpoint_from": i0}


def _zsweep_duration_s(context, calls: list) -> tuple[float, dict]:
    """How long one Z spectrum takes by the controller's own settings (s), and how that was
    worked out — the Z counterpart of :func:`_sweep_duration_s`.

    ``ZSpectr_Start`` with Get data = 1 blocks for the whole sweep before it answers, exactly
    like ``BiasSpectr_Start``; until 7.0.1 nothing raised the socket's recv timeout for it,
    so a Z spectrum longer than the pool's ~5 s left its reply on a socket nobody read and
    misaligned every later call on that connection (a past failure of
    ``AcquireSTS``). Same model, the reply layouts from ``nanonis_spm``:

    * ``ZSpectr.PropsGet`` -> [Backward sweep (H), Number of points (i), Parameters size,
      Number of parameters, Parameters (*+c), Fixed parameters size, Number of fixed
      parameters, Fixed parameters (*+c), Number of sweeps (H), Save all (H)];
    * ``ZSpectr.TimingGet`` -> [Z averaging, Initial settling, Max slew rate, Settling,
      Integration, End settling, Z control time] (7 float32).

    Unreadable settings fall back to :data:`_SWEEP_BUDGET_FALLBACK_S` (the long side)."""
    npts = nsw = None
    bwd = True
    rec = _call_idempotent(calls, lambda: context.safe_call("ZSpectr_PropsGet"))
    v = rec.return_value
    var = v[2] if isinstance(v, (list, tuple)) and len(v) > 2 else None
    if isinstance(var, (list, tuple)) and len(var) >= 9:
        b, n, s = _plain_int(var[0]), _plain_int(var[1]), _plain_int(var[8])
        if n is not None and s is not None:
            npts, nsw = max(n, 1), max(s, 1)
            bwd = bool(b) if b is not None else True

    t: dict = {}
    rec2 = _call_idempotent(calls, lambda: context.safe_call("ZSpectr_TimingGet"))
    v2 = rec2.return_value
    var2 = v2[2] if isinstance(v2, (list, tuple)) and len(v2) > 2 else None
    keys = ["z_averaging_time_s", "initial_settling_time_s", "max_slew_rate_v_per_s",
            "settling_time_s", "integration_time_s", "end_settling_time_s", "z_control_time_s"]
    if isinstance(var2, (list, tuple)):
        for i, k in enumerate(keys):
            if i < len(var2):
                try:
                    x = var2[i]
                    while isinstance(x, (list, tuple)) and len(x) == 1:
                        x = x[0]
                    t[k] = float(x)
                except (TypeError, ValueError):
                    pass

    if npts is None or nsw is None or "integration_time_s" not in t:
        return _SWEEP_BUDGET_FALLBACK_S, {
            "why": tr("读不到 Z 谱扫掠设定，退回保守上限", "Could not read the Z spectroscopy sweep settings, falling back to a conservative limit"),
            "npts": npts, "nsweeps": nsw, "timing_keys": sorted(t),
        }
    per_pt = t.get("settling_time_s", 0.0) + t["integration_time_s"]
    per_sweep = (t.get("initial_settling_time_s", 0.0)
                 + t.get("end_settling_time_s", 0.0)
                 + t.get("z_control_time_s", 0.0)
                 + t.get("z_averaging_time_s", 0.0))
    core = npts * per_pt * (2 if bwd else 1) * nsw + per_sweep * nsw
    return core, {"npts": npts, "nsweeps": nsw, "backward": bwd,
                  "per_point_s": round(per_pt, 4), "core_s": round(core, 1)}


def zsweep_recv_budget_s(context, calls: list) -> tuple[float, float, dict]:
    """``(recv timeout for ZSpectr_Start, estimated sweep s, how)``: the estimate from
    :func:`_zsweep_duration_s` with the same margin ``AcquireSTS`` gives a bias sweep."""
    core_s, detail = _zsweep_duration_s(context, calls)
    return core_s * _SWEEP_BUDGET_FACTOR + _SWEEP_BUDGET_PAD_S, core_s, detail


class AcquireSTS(BaseSkill):
    """Acquire a single STS spectrum at the current position."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AcquireSTS",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("在当前针尖位置采一条 STS 谱。使用当前的 lock-in 与 bias 扫描设置。传 save_basename "
                "可以控制保存下来的 .dat 文件名（为了可追溯 —— 例如按网格点命名）。", "Acquire one STS spectrum at the current tip position. Uses the current lock-in and bias sweep settings. Pass save_basename to control the file name of the saved .dat (for traceability - e.g. named by grid point).")
            ),
            parameters=[
                ParameterSpec(
                    name="save_basename",
                    type="str",
                    description=(tr("保存下来的谱文件的基名（留空 = 沿用模块当前的基名）。"
                                 "它让网格/批量测量能把每一个 .dat 与它的坐标对应起来。", "Base name of the saved spectrum file (empty = keep the module's current base name). It lets grid/batch measurements match each .dat to its coordinates.")),
                    required=False,
                    default="",
                ),
                ParameterSpec(
                    name="ramp_before",
                    type="bool",
                    description=(tr("测谱前把偏压从成像值**分步 ramp** 到测谱起始偏压（取自 "
                                 "ConfigureSTS 配的上下限的 start），别一步突变以免伤针。默认关="
                                 "现有行为不变。给了 spectro_setpoint_a 时电流 setpoint 一起同步 ramp。",
                                 "Before the sweep, **ramp** the bias step by step from its imaging value to the "
                                 "spectroscopy start bias (the start of the limits set via ConfigureSTS) instead of "
                                 "jumping, to protect the tip. Off by default (no change to existing behaviour). When "
                                 "spectro_setpoint_a is given, the current setpoint is ramped in sync.")),
                    required=False,
                    default=False,
                ),
                ParameterSpec(
                    name="restore_after",
                    type="bool",
                    description=(tr("测谱后把偏压（和被改过的电流 setpoint）**ramp 回**测谱前快照到的成像值。"
                                 "默认关。谱已拿到的前提下，恢复失败只告警、不判这次采集失败。",
                                 "After the sweep, **ramp** the bias (and the current setpoint, if it was changed) "
                                 "back to the imaging values snapshotted before spectroscopy. Off by default. Once the "
                                 "spectrum is in hand, a failed restore only warns and does not fail the acquisition.")),
                    required=False,
                    default=False,
                ),
                ParameterSpec(
                    name="spectro_setpoint_a",
                    type="float",
                    description=(tr("测谱用的电流 setpoint（安培）。给了才在 ramp_before 时把 setpoint "
                                 "一起 ramp 过去、并在 restore_after 时还原；留空 = 全程不碰 setpoint。",
                                 "Current setpoint for spectroscopy, in amps. Only when given is the setpoint ramped "
                                 "across during ramp_before and restored during restore_after; empty = the setpoint is "
                                 "never touched.")),
                    unit="A",
                    required=False,
                    default=None,
                ),
                ParameterSpec(
                    name="ramp_slew_rate_v_per_s",
                    type="float",
                    description=(tr("偏压 ramp 速率（V/s），与 SetBias 同语义：留空 = 瞬时切换（SetBias 的默认）；"
                                 "给了才分步限速。只在 ramp_before / restore_after 打开时起作用。",
                                 "Bias ramp rate (V/s), same semantics as SetBias: empty = switch instantly (SetBias's "
                                 "default); give a value for a rate-limited stepped ramp. Only matters when ramp_before "
                                 "/ restore_after is on.")),
                    unit="V/s",
                    required=False,
                    default=None,
                    min_value=0.01,
                    max_value=100.0,
                ),
            ],
            preconditions=["z_controller_on"],
            estimated_duration_s=30.0,
            composition_level=1,
            tags=["spectroscopy", "sts", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        calls = []
        save_basename = str(params.get("save_basename", "") or "")
        ramp_before = bool(params.get("ramp_before", False))
        restore_after = bool(params.get("restore_after", False))
        spectro_setpoint_a = params.get("spectro_setpoint_a")
        ramp_slew = params.get("ramp_slew_rate_v_per_s")
        # restore 只还原「这次真改过的量」：setpoint 只有在 ramp_before 时给了
        # spectro_setpoint_a 才动过，才需要还原。
        changed_setpoint = ramp_before and spectro_setpoint_a is not None
        ramp_snapshot: dict | None = None

        _call_idempotent(calls, lambda: context.safe_call("BiasSpectr_Open"))

        # ── 测谱前把偏压(和 setpoint)从成像值分步 ramp 到测谱值 ──────
        # 偏压/电流要从扫图值慢慢变到测谱值，别一步突变以免伤针。
        # ramp 到的偏压 = ConfigureSTS 配的 start（这里读回来），setpoint = 调用方给的
        # spectro_setpoint_a（不给就不碰）。先快照当前成像值，供 restore_after 还原。
        # ramp 失败（读不到当前偏压 / Bias_Set 失败）就**拒绝起扫**——没能安全建立测谱
        # 条件，不如不扫。
        if ramp_before:
            start_v, lim_err = _read_sweep_start_v(context, calls)
            if start_v is None:
                return SkillResult(
                    skill_name="AcquireSTS", success=False,
                    error=tr(f"ramp_before=True，但读不到测谱起始偏压({lim_err}) —— 拒绝起扫："
                             f"不知道要 ramp 到哪。先用 ConfigureSTS 配好上下限，或关掉 ramp_before。",
                             f"ramp_before=True, but the spectroscopy start bias could not be read ({lim_err}) "
                             f"- refusing to sweep: nowhere to ramp to. Configure the limits via ConfigureSTS "
                             f"first, or turn ramp_before off."),
                    data={"working_point_ramped": False}, nanonis_calls=calls)
            rb = _ramp_working_point(
                context, calls, bias_to=start_v, setpoint_to=spectro_setpoint_a,
                slew_v_per_s=ramp_slew, abort=_abort_fn(context))
            if not rb["ok"]:
                return SkillResult(
                    skill_name="AcquireSTS", success=False,
                    error=tr(f"测谱前 ramp 工作点失败：{rb['error']} —— 拒绝起扫（没能安全建立测谱条件）。",
                             f"Failed to ramp the working point before spectroscopy: {rb['error']} "
                             f"- refusing to sweep (could not safely establish spectroscopy conditions)."),
                    data={"working_point_ramped": False}, nanonis_calls=calls)
            ramp_snapshot = {"bias_v": rb["bias_from"], "setpoint_a": rb["setpoint_from"]}

        # NOTE: AcquireSTS deliberately does NOT call BiasSpectr_PropsSet here.
        # PropsSet's Z-offset arg has no "no change" sentinel and BiasSpectr_
        # PropsGet does not even return it, so the old hardcoded
        # PropsSet(1,0,0,0,0.0,1,2) silently ZEROED the operator's configured
        # protective pre-STS Z retract on every acquire.
        # Save/autosave/Z-offset are configured via ConfigureSTS; Acquire only
        # starts the sweep and (Get data=1) returns the spectrum regardless.

        # BiasSpectr_Start 阻塞直至整条扫掠结束，recv 预算必须覆盖实际采集时长。
        # 过早超时会使迟到回包污染下一次调用，也可能令控制器状态停在采集中间。
        # 根据仪器当前点数、积分时间和 sweep 数推导预算，不写死某次采集的时长。
        core_s, budget_detail = _sweep_duration_s(context, calls)
        recv_budget_s = core_s * _SWEEP_BUDGET_FACTOR + _SWEEP_BUDGET_PAD_S
        record = context.safe_call("BiasSpectr_Start", 1, save_basename,
                                   recv_timeout_s=recv_budget_s)
        calls.append(record)
        if record.error:
            return SkillResult(
                skill_name="AcquireSTS",
                success=False,
                error=record.error,
                nanonis_calls=calls,
            )
        # ``acquisition_complete`` 说的是**这次扫掠调用跑完了**(``record.error``
        # 上面已经挡过),不是「谱拿到了」。这两件事在这个技能里是分开的 ——
        # 见下面 success 的取法。
        data: dict = {"acquisition_complete": True}
        # 测谱前有没有做 ramp、从哪个成像工作点来的（供追溯 / restore 对账）。
        data["working_point_ramped"] = bool(ramp_before)
        if ramp_snapshot is not None:
            data["imaging_bias_v"] = ramp_snapshot["bias_v"]
            if changed_setpoint:
                data["imaging_setpoint_a"] = ramp_snapshot["setpoint_a"]
        # 记账：这条谱按仪器设定算出来要多久、我们把 recv 超时抬到了多少。
        # 出问题时这是第一现场 —— 抬得不够会表现成「连接莫名其妙废了」。
        data["sweep_estimate_s"] = round(core_s, 1)
        data["recv_timeout_s"] = round(recv_budget_s, 1)
        data["sweep_settings"] = budget_detail
        if save_basename:
            data["save_basename"] = save_basename
        parsed = record.return_value
        if isinstance(parsed, (list, tuple)) and len(parsed) > 2:
            channel_map, num_points, why = _reshape_spectrum(parsed[2])
        else:
            channel_map, num_points, why = {}, 0, (
                tr(f"return_value 不是 (err, raw, Variables) 三元组: "
                f"{type(parsed).__name__}", f"return_value is not an (err, raw, Variables) triple: {type(parsed).__name__}"))
        # **总是**落这个键。只在失败时出现的标志,会让按它分支的调用方在成功
        # 路径上读到 KeyError —— 那是把一个诚实标志变成一颗新的雷。
        data["spectrum_parsed"] = bool(channel_map)
        if channel_map:
            data["channel_names"] = list(channel_map.keys())
            data["num_points"] = num_points
            data.update(channel_map)   # one trace per channel name
            # Convenience aliases resolved BY CHANNEL NAME (not by a fixed
            # column index, which is what the transposed parse got wrong).
            volt = _match_channel(channel_map, ("bias", "volt"))
            if volt is not None:
                data["voltage"] = volt
            cur = _match_channel(channel_map, ("current",))
            if cur is not None:
                data["current"] = cur
        else:
            data["spectrum_unparsed_reason"] = why
            logger.warning("AcquireSTS: 返回的数据块解不开 —— %s", why)
        _attach_saved_dat(context, data)

        # ── 测谱后把工作点 ramp 回成像态（可选）────────────────────
        # 谱已经拿到（上面）。恢复是善后：把偏压（和被改过的 setpoint）ramp 回**测谱前
        # 快照到的成像值**（还原成读到的值，不是写死值）。恢复失败只告警、不翻盘这次
        # 采集——否则会把一次拿到了谱的采集判成失败。两条返回路径（下面的失败与成功）
        # 都在这之后，所以恢复对两者都生效。
        if restore_after and ramp_snapshot is not None:
            rr = _ramp_working_point(
                context, calls,
                bias_to=ramp_snapshot["bias_v"],
                setpoint_to=(ramp_snapshot["setpoint_a"] if changed_setpoint else None),
                slew_v_per_s=ramp_slew, abort=_abort_fn(context))
            data["working_point_restored"] = rr["ok"]
            if not rr["ok"]:
                data["working_point_restore_error"] = rr["error"]
                logger.warning(
                    "AcquireSTS: 测谱后恢复工作点失败：%s —— 谱已拿到，但针尖仍停在测谱"
                    "条件，下次成像前请手动复位偏压/电流。", rr["error"])

        # ── success 的取法(2026-08-15 普查 A3;判据写在这里,别照搬别处)──
        # ConfigureSTS 打开 autosave ⇒ 每次采集都有一份 .dat 落盘。所以
        # 「内联块解不开」时数据**可能仍然在盘上**;那时报失败会把 agent 推去
        # 重扫同一个点 —— 多一次针尖停留、多一次针尖变化的机会,而好数据本来
        # 就在。反过来,内联解不开**且**盘上也没找到,这次采集就没有在任何地方
        # 留下东西,说成成功就是把一次故障答成了数据。
        #
        # ⚠️ 这条判断的弱处写在这儿,好让下一个人能推翻它:``_attach_saved_dat``
        # 找的是「最近 120 s 内最新的 .dat」,是个启发式,它**可能漏**(慢盘、
        # 目录不在候选表里)。所以 error 文本必须告诉人去盘上自己看一眼。
        saved = str(data.get("path") or "")
        if not channel_map and not saved:
            return SkillResult(
                skill_name="AcquireSTS",
                success=False,
                error=(tr(f"扫掠跑完了,但返回的数据块解不开({why}),"
                       f"而且没有找到自动保存的 .dat —— 这次采集没有在任何地方"
                       f"留下可用的谱。若 autosave 是开着的,请到 Nanonis 的保存"
                       f"目录自己确认一次(这里只看最近 120 s 内最新的 .dat)。", f"The sweep finished, but the returned data block could not be unpacked ({why}), and no autosaved .dat was found - this acquisition left no usable spectrum anywhere. If autosave is on, check the Nanonis save directory yourself (this only looks at the newest .dat from the last 120 s).")),
                data=data,
                nanonis_calls=calls,
            )
        return SkillResult(
            skill_name="AcquireSTS",
            success=True,
            data=data,
            nanonis_calls=calls,
        )


class ConfigureSTS(BaseSkill):
    """Configure STS sweep parameters."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ConfigureSTS",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("配置 STS 的 bias 扫描参数。", "Configure the bias sweep parameters of STS."),
            parameters=[
                ParameterSpec(
                    name="start_v",
                    type="float",
                    description=tr("扫描起始电压，单位伏特", "Sweep start voltage, in volts"),
                    unit="V",
                    required=True,
                    min_value=-10.0,
                    max_value=10.0,
                ),
                ParameterSpec(
                    name="end_v",
                    type="float",
                    description=tr("扫描终止电压，单位伏特", "Sweep end voltage, in volts"),
                    unit="V",
                    required=True,
                    min_value=-10.0,
                    max_value=10.0,
                ),
                ParameterSpec(
                    name="num_points",
                    type="int",
                    description=tr("扫描的点数", "Number of points in the sweep"),
                    required=True,
                    min_value=2,
                    max_value=10000,
                ),
                ParameterSpec(
                    name="num_sweeps",
                    type="int",
                    description=(
                        tr("平均的扫掠数（往返算一次）。留空 / 0 = **保持操作员在 "
                        "Nanonis 里当前设的值不动**；给 ≥1 才覆盖它。原来这里被写死成 "
                        "1，于是用户在 GUI 里设的多次平均每次配置都被打回 1。", "Number of sweeps to average (forth and back count as one). Empty / 0 = **leave the value the operator currently set in Nanonis untouched**; give >= 1 to override it. This used to be hard-coded to 1 here, so the multiple averages a user set in the GUI were reset to 1 on every configuration.")
                    ),
                    required=False,
                    default=0,
                    min_value=0,
                    max_value=1000,
                ),
                ParameterSpec(
                    name="z_offset_m",
                    type="float",
                    description=tr("谱学期间的 Z 偏移，单位米", "Z offset during spectroscopy, in metres"),
                    unit="m",
                    required=False,
                    default=0.0,
                ),
            ],
            estimated_duration_s=1.0,
            composition_level=1,
            tags=["spectroscopy", "sts", "configure", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        start_v = params["start_v"]
        end_v = params["end_v"]
        num_points = params["num_points"]
        z_offset_m = params.get("z_offset_m", 0.0)
        num_sweeps_param = int(params.get("num_sweeps", 0) or 0)
        calls = []

        # Every call here sets an absolute value, so a transport failure is
        # retried once (``_call_idempotent``; a first-load timeout).
        rec_open = _call_idempotent(calls, lambda: context.safe_call("BiasSpectr_Open"))
        if rec_open.error:
            return SkillResult(
                skill_name="ConfigureSTS",
                success=False,
                error=rec_open.error,
                nanonis_calls=calls,
            )

        rec_limits = _call_idempotent(
            calls, lambda: context.safe_call("BiasSpectr_LimitsSet", start_v, end_v))
        if rec_limits.error:
            return SkillResult(
                skill_name="ConfigureSTS",
                success=False,
                error=rec_limits.error,
                nanonis_calls=calls,
            )

        # ── 扫掠数（PropsSet 第 2 个实参）──────────────────────────────────
        # 原来写死成 1，于是用户在 Nanonis GUI 里设的多次平均每次 ConfigureSTS
        # 都被打回 1（曾出现过：测谱时 SweepS 自动变为 1）。``BiasSpectr_
        # PropsSet`` 的扫掠数**没有「不改」哨兵值**，所以「保持」= 先读回来再原样
        # 写。显式给 num_sweeps(≥1) 才覆盖；读不到当前值时退回 1 并说出来
        # （「读不到」不默默折成一个让人以为保持住了的值）。
        if num_sweeps_param >= 1:
            effective_sweeps = num_sweeps_param
            sweeps_source = "override"
        else:
            current_sweeps = _read_current_num_sweeps(context, calls)
            if current_sweeps is not None:
                effective_sweeps = current_sweeps
                sweeps_source = "preserved"
            else:
                effective_sweeps = 1
                sweeps_source = "fallback_unreadable"
                logger.warning(
                    "ConfigureSTS: 读不到当前扫掠数，退回 1 —— "
                    "无法确认是否保持了操作员在 GUI 里设的平均扫掠数。")

        rec_props = _call_idempotent(calls, lambda: context.safe_call(
            "BiasSpectr_PropsSet", 1, effective_sweeps, 1, num_points, z_offset_m, 1, 2,
        ))
        if rec_props.error:
            return SkillResult(
                skill_name="ConfigureSTS",
                success=False,
                error=rec_props.error,
                nanonis_calls=calls,
            )

        # Advanced props: pin the two behaviours the STS
        # chain used to inherit from whatever the operator last left in the
        # Nanonis GUI — a dangerous ambiguity. AdvPropsSet(reset_bias,
        # z_ctrl_hold, record_final_z, lockin_run), each 0=no change/1=On/2=Off:
        #   • Z-Controller Hold = On → feedback is SUSPENDED during the bias
        #     sweep (constant-height STS). Without this, feedback stays active
        #     and as the sweep crosses 0 V it drives the tip into the surface.
        #   • Reset Bias = On → bias returns to the imaging value after the sweep
        #     (don't leave the junction parked at the sweep end voltage).
        rec_adv = _call_idempotent(
            calls, lambda: context.safe_call("BiasSpectr_AdvPropsSet", 1, 1, 0, 0))
        # Non-fatal: some controllers/firmware may reject AdvPropsSet; the sweep
        # can still run. Surface it in data rather than failing the config.
        adv_ok = not getattr(rec_adv, "error", "")

        return SkillResult(
            skill_name="ConfigureSTS",
            success=True,
            data={
                "start_v": start_v,
                "end_v": end_v,
                "num_points": num_points,
                "z_offset_m": z_offset_m,
                # 实际设下去的扫掠数，以及它从哪来：override（调用方给的）/
                # preserved（保持了操作员原值）/ fallback_unreadable（读不到退回 1）。
                "num_sweeps": effective_sweeps,
                "num_sweeps_source": sweeps_source,
                "z_controller_hold": True if adv_ok else None,
                "reset_bias": True if adv_ok else None,
            },
            nanonis_calls=calls,
        )


class ConfigureZSpectr(BaseSkill):
    """Configure Z spectroscopy parameters."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ConfigureZSpectr",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("配置 Z 谱学：Z 偏移、扫描距离与点数。", "Configure Z spectroscopy: Z offset, sweep distance and number of points."),
            parameters=[
                ParameterSpec(
                    name="z_offset_m",
                    type="float",
                    description=tr("Z 偏移，单位米", "Z offset, in metres"),
                    unit="m",
                    required=True,
                ),
                ParameterSpec(
                    name="z_sweep_distance_m",
                    type="float",
                    description=tr("Z 扫描距离，单位米", "Z sweep distance, in metres"),
                    unit="m",
                    required=True,
                    min_value=0.0,
                ),
                ParameterSpec(
                    name="num_points",
                    type="int",
                    description=tr("扫描的点数", "Number of points in the sweep"),
                    required=True,
                    min_value=2,
                    max_value=10000,
                ),
                ParameterSpec(
                    name="backward_sweep",
                    type="bool",
                    description=tr("启用反扫", "Enable backward sweep"),
                    required=False,
                    default=True,
                ),
            ],
            estimated_duration_s=1.0,
            composition_level=1,
            tags=["spectroscopy", "z", "configure", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        z_offset = params["z_offset_m"]
        z_distance = params["z_sweep_distance_m"]
        num_points = params["num_points"]
        backward = params.get("backward_sweep", True)
        calls = []

        # All three set absolute values: retried once on a transport failure.
        rec_open = _call_idempotent(calls, lambda: context.safe_call("ZSpectr_Open"))
        if rec_open.error:
            return SkillResult(
                skill_name="ConfigureZSpectr",
                success=False,
                error=rec_open.error,
                nanonis_calls=calls,
            )

        rec_range = _call_idempotent(
            calls, lambda: context.safe_call("ZSpectr_RangeSet", z_offset, z_distance))
        if rec_range.error:
            return SkillResult(
                skill_name="ConfigureZSpectr",
                success=False,
                error=rec_range.error,
                nanonis_calls=calls,
            )

        rec_props = _call_idempotent(calls, lambda: context.safe_call(
            "ZSpectr_PropsSet",
            int(backward), num_points, 1, 1, 2, 1,
        ))
        if rec_props.error:
            return SkillResult(
                skill_name="ConfigureZSpectr",
                success=False,
                error=rec_props.error,
                nanonis_calls=calls,
            )

        return SkillResult(
            skill_name="ConfigureZSpectr",
            success=True,
            data={
                "z_offset_m": z_offset,
                "z_sweep_distance_m": z_distance,
                "num_points": num_points,
                "backward_sweep": backward,
            },
            nanonis_calls=calls,
        )


class AcquireZSpectr(BaseSkill):
    """Acquire a Z spectrum at the current position."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AcquireZSpectr",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("在当前针尖位置采一条 Z 谱。", "Acquire one Z spectrum at the current tip position."),
            preconditions=["z_controller_on"],
            estimated_duration_s=30.0,
            composition_level=1,
            tags=["spectroscopy", "z", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        calls = []

        # Open and PropsSet are idempotent (retried once on a transport failure);
        # ZSpectr_Start is NOT — a resend can run a second sweep — so it is not.
        _call_idempotent(calls, lambda: context.safe_call("ZSpectr_Open"))
        _call_idempotent(calls, lambda: context.safe_call("ZSpectr_PropsSet", 0, 0, 0, 1, 2, 1))

        # ZSpectr_Start blocks for the whole sweep: the recv timeout has to cover it (see
        # _zsweep_duration_s; without this a sweep longer than ~5 s broke the connection).
        recv_budget_s, core_s, budget_detail = zsweep_recv_budget_s(context, calls)
        record = context.safe_call("ZSpectr_Start", 1, "", recv_timeout_s=recv_budget_s)
        calls.append(record)
        if record.error:
            return SkillResult(
                skill_name="AcquireZSpectr",
                success=False,
                error=record.error,
                nanonis_calls=calls,
            )

        data: dict = {"acquisition_complete": True,
                      "sweep_estimate_s": round(core_s, 1),
                      "recv_timeout_s": round(recv_budget_s, 1),
                      "sweep_settings": budget_detail}
        parsed = record.return_value
        # return_value is (error_string, raw_bytes, Variables). For ZSpectr.Start
        # the Variables (ResponseTypes ["i","i","*+c","i","i","2f","i","*f"]) are:
        #   [0] channel-names byte size (int)   [1] number of channels (int)
        #   [2] channel names (1D str array)    [3] data rows (int)
        #   [4] data columns (int)              [5] data (2D float32) <- the spectrum
        #   [6] number of parameters (int)      [7] parameters (1D float32)
        # The OLD code read variables[0]/[1] (header ints) as the z/current
        # arrays — i.e. it returned the channel-count, never the spectrum.
        if isinstance(parsed, (list, tuple)) and len(parsed) > 2:
            channel_map, num_points, why = _reshape_spectrum(parsed[2])
        else:
            channel_map, num_points, why = {}, 0, (
                tr(f"return_value 不是 (err, raw, Variables) 三元组: "
                f"{type(parsed).__name__}", f"return_value is not an (err, raw, Variables) triple: {type(parsed).__name__}"))
        data["spectrum_parsed"] = bool(channel_map)
        if channel_map:
            data["channel_names"] = list(channel_map.keys())
            data["num_points"] = num_points
            data.update(channel_map)
            # Resolve z / current / dIdV BY CHANNEL NAME (the old fixed-column
            # parse was transposed and mislabelled the traces).
            z = _match_channel(channel_map, ("z (", "z(", "z_m", "z pos"))
            if z is None:
                # fall back: a bare-"z"-named channel
                for name, trace in channel_map.items():
                    if str(name).strip().lower().startswith("z"):
                        z = trace
                        break
            if z is not None:
                data["z"] = z
            cur = _match_channel(channel_map, ("current",))
            if cur is not None:
                data["current"] = cur
            did = _match_channel(channel_map, ("lix", "did", "di/dv", "di_dv"))
            if did is not None:
                data["dIdV"] = did
        else:
            # ⚠️ **这里的结论和 AcquireSTS 不一样,而不一样是有依据的**:
            # 这个技能不调 ``_attach_saved_dat``,``ZSpectr_Start(1, "")`` 也不
            # 给 basename —— **内联这一份就是唯一的一份**。解不开 = 什么都没
            # 拿到,没有第二处可以去找。所以这里一律报失败,而 STS 那边只在
            # 「盘上也没有」时才报。(2026-08-15 普查 A3)
            data["spectrum_unparsed_reason"] = why
            logger.warning("AcquireZSpectr: 返回的数据块解不开 —— %s", why)
            return SkillResult(
                skill_name="AcquireZSpectr",
                success=False,
                error=(tr(f"扫掠跑完了,但返回的数据块解不开({why})。"
                       f"这个技能不落盘,内联返回是唯一的一份 —— "
                       f"这次采集没有留下任何可用的谱。", f"The sweep finished, but the returned data block could not be unpacked ({why}). This skill does not save to disk, the inline return is the only copy - this acquisition left no usable spectrum.")),
                data=data,
                nanonis_calls=calls,
            )
        return SkillResult(
            skill_name="AcquireZSpectr",
            success=True,
            data=data,
            nanonis_calls=calls,
        )


class ConfigureSTSTiming(BaseSkill):
    """Configure STS timing parameters."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ConfigureSTSTiming",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("配置 Bias Spectroscopy 的时序参数。", "Configure the timing parameters of Bias Spectroscopy."),
            parameters=[
                ParameterSpec(
                    name="z_avg_time_s", type="float",
                    description=tr("Z 平均时间", "Z averaging time"), unit="s",
                    required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="z_offset_m", type="float",
                    description=tr("Z 偏移", "Z offset"), unit="m",
                    required=False, default=0.0,
                ),
                ParameterSpec(
                    name="init_settling_s", type="float",
                    description=tr("初始建立时间", "Initial settling time"), unit="s",
                    required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="max_slew_rate_v_s", type="float",
                    description=tr("最大压摆率", "Maximum slew rate"), unit="V/s",
                    required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="settling_s", type="float",
                    description=tr("每点的建立时间", "Settling time per point"), unit="s",
                    required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="integration_s", type="float",
                    description=tr("每点的积分时间", "Integration time per point"), unit="s",
                    required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="end_settling_s", type="float",
                    description=tr("结束时的建立时间", "End settling time"), unit="s",
                    required=False, default=0.0, min_value=0.0,
                ),
                ParameterSpec(
                    name="z_ctrl_time_s", type="float",
                    description=tr("Z 控制时间", "Z control time"), unit="s",
                    required=False, default=0.0, min_value=0.0,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "timing", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        calls: list = []
        record = _call_idempotent(calls, lambda: context.safe_call(
            "BiasSpectr_TimingSet",
            params["z_avg_time_s"],
            params.get("z_offset_m", 0.0),
            params["init_settling_s"],
            params["max_slew_rate_v_s"],
            params["settling_s"],
            params["integration_s"],
            params.get("end_settling_s", 0.0),
            params.get("z_ctrl_time_s", 0.0),
        ))
        if record.error:
            return SkillResult(
                skill_name="ConfigureSTSTiming",
                success=False,
                error=record.error,
                nanonis_calls=calls,
            )
        return SkillResult(
            skill_name="ConfigureSTSTiming",
            success=True,
            data={k: params.get(k, 0.0) for k in [
                "z_avg_time_s", "z_offset_m", "init_settling_s",
                "max_slew_rate_v_s", "settling_s", "integration_s",
                "end_settling_s", "z_ctrl_time_s",
            ]},
            nanonis_calls=calls,
        )


class StopSTS(BaseSkill):
    """Stop a running Bias Spectroscopy measurement."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="StopSTS",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=tr("停止当前的 Bias Spectroscopy 测量。", "Stop the Bias Spectroscopy measurement that is currently running."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "stop"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("BiasSpectr_Stop")
        if record.error:
            return SkillResult(
                skill_name="StopSTS", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="StopSTS", success=True,
            data={"stopped": True}, nanonis_calls=[record],
        )


class StopZSpectr(BaseSkill):
    """Stop a running Z Spectroscopy measurement."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="StopZSpectr",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=tr("停止当前的 Z Spectroscopy 测量。", "Stop the Z Spectroscopy measurement that is currently running."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "stop"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("ZSpectr_Stop")
        if record.error:
            return SkillResult(
                skill_name="StopZSpectr", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="StopZSpectr", success=True,
            data={"stopped": True}, nanonis_calls=[record],
        )


class ConfigureSTSChannels(BaseSkill):
    """Configure STS recorded channels."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ConfigureSTSChannels",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设置 Bias Spectroscopy 期间记录哪些通道。", "Set which channels are recorded during Bias Spectroscopy."),
            parameters=[
                ParameterSpec(
                    name="channel_indexes",
                    type="str",
                    description=tr("逗号分隔的通道索引（0-23）", "Comma-separated channel indexes (0-23)"),
                    required=True,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "channels", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        idx_str = params["channel_indexes"]
        indexes = [int(x.strip()) for x in idx_str.split(",") if x.strip()]
        calls: list = []
        record = _call_idempotent(calls, lambda: context.safe_call("BiasSpectr_ChsSet", indexes))
        if record.error:
            return SkillResult(
                skill_name="ConfigureSTSChannels", success=False,
                error=record.error, nanonis_calls=calls,
            )
        return SkillResult(
            skill_name="ConfigureSTSChannels", success=True,
            data={"channel_indexes": indexes}, nanonis_calls=calls,
        )


class ConfigureZSpectrTiming(BaseSkill):
    """Configure Z spectroscopy timing parameters."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ConfigureZSpectrTiming",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("配置 Z Spectroscopy 的时序参数。", "Configure the timing parameters of Z Spectroscopy."),
            parameters=[
                ParameterSpec(
                    name="z_avg_time_s", type="float",
                    description=tr("Z 平均时间", "Z averaging time"), unit="s",
                    required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="init_settling_s", type="float",
                    description=tr("初始建立时间", "Initial settling time"), unit="s",
                    required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="max_slew_rate_v_s", type="float",
                    description=tr("最大压摆率", "Maximum slew rate"), unit="V/s",
                    required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="settling_s", type="float",
                    description=tr("每点的建立时间", "Settling time per point"), unit="s",
                    required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="integration_s", type="float",
                    description=tr("每点的积分时间", "Integration time per point"), unit="s",
                    required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="end_settling_s", type="float",
                    description=tr("结束时的建立时间", "End settling time"), unit="s",
                    required=False, default=0.0, min_value=0.0,
                ),
                ParameterSpec(
                    name="z_ctrl_time_s", type="float",
                    description=tr("Z 控制时间", "Z control time"), unit="s",
                    required=False, default=0.0, min_value=0.0,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "timing", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        calls: list = []
        record = _call_idempotent(calls, lambda: context.safe_call(
            "ZSpectr_TimingSet",
            params["z_avg_time_s"],
            params["init_settling_s"],
            params["max_slew_rate_v_s"],
            params["settling_s"],
            params["integration_s"],
            params.get("end_settling_s", 0.0),
            params.get("z_ctrl_time_s", 0.0),
        ))
        if record.error:
            return SkillResult(
                skill_name="ConfigureZSpectrTiming",
                success=False,
                error=record.error,
                nanonis_calls=calls,
            )
        return SkillResult(
            skill_name="ConfigureZSpectrTiming",
            success=True,
            data={k: params.get(k, 0.0) for k in [
                "z_avg_time_s", "init_settling_s", "max_slew_rate_v_s",
                "settling_s", "integration_s", "end_settling_s", "z_ctrl_time_s",
            ]},
            nanonis_calls=calls,
        )


class GetSTSChannels(BaseSkill):
    """Get the list of recorded channels for Bias Spectroscopy."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSTSChannels",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Bias Spectroscopy 记录的通道列表。", "Get the list of channels recorded by Bias Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "channels", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("BiasSpectr_ChsGet")
        if record.error:
            return SkillResult(
                skill_name="GetSTSChannels", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # Decode the response envelope before interpreting its channel fields.
        # Numeric channel indexes and string names use different parser paths;
        # preserve the distinction instead of applying numeric tuple handling
        # indiscriminately to strings. core.nanonis_patch normalizes the parser,
        # while channel_ids_from_buffer also accepts supported test-double forms.
        parsed = record.return_value
        channel_indexes: list = []
        channel_names: list = []
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)):
            if len(variables) > 1 and isinstance(variables[1], (list, tuple)):
                channel_indexes = channel_ids_from_buffer(variables)
            if len(variables) > 4 and isinstance(variables[4], (list, tuple)):
                channel_names = list(variables[4])
        return SkillResult(
            skill_name="GetSTSChannels", success=True,
            data={"channel_indexes": channel_indexes, "channel_names": channel_names},
            nanonis_calls=[record],
        )


class SetSTSChannels(BaseSkill):
    """Set the recorded channels for Bias Spectroscopy."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetSTSChannels",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设置 Bias Spectroscopy 记录的通道。", "Set the channels recorded by Bias Spectroscopy."),
            parameters=[
                ParameterSpec(
                    name="channel_indexes",
                    # NOTE: declared as str (comma-separated) because the agent
                    # schema builder has no list type — execute() coerces to a
                    # real list. See _coerce_int_list.
                    type="str",
                    description=tr("通道索引（0-127），逗号分隔，例如 '0, 1, 2'", "Channel indexes (0-127), comma-separated, e.g. '0, 1, 2'"),
                    required=True,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "channels", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        channel_indexes = _coerce_int_list(params["channel_indexes"])
        # BiasSpectr_ChsSet(Channel_indexes: list) takes ONE arg — the library's
        # "+*i" wire format sends the array length itself, so passing len() too
        # is a TypeError on real hardware. (ConfigureSTSChannels above is the
        # correct pattern.) — spectroscopy channel-config fix 2026-06-02.
        record = context.safe_call("BiasSpectr_ChsSet", channel_indexes)
        if record.error:
            return SkillResult(
                skill_name="SetSTSChannels", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="SetSTSChannels", success=True,
            data={"channel_indexes": channel_indexes},
            nanonis_calls=[record],
        )


class GetSTSLimits(BaseSkill):
    """Get the bias voltage range for Bias Spectroscopy."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSTSLimits",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Bias Spectroscopy 的 bias 电压范围。", "Get the bias voltage range of Bias Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "limits", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("BiasSpectr_LimitsGet")
        if record.error:
            return SkillResult(
                skill_name="GetSTSLimits", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). BiasSpectr.LimitsGet
        # ResponseTypes ["f","f"] -> Variables [start_v, end_v]. OLD code read
        # parsed[0]/[1] (the empty error string + raw bytes) -> float("") ValueError.
        parsed = record.return_value
        start_v = 0.0
        end_v = 0.0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) >= 2:
            start_v = float(variables[0])
            end_v = float(variables[1])
        return SkillResult(
            skill_name="GetSTSLimits", success=True,
            data={"start_v": start_v, "end_v": end_v},
            nanonis_calls=[record],
        )


class SetSTSAdvancedProps(BaseSkill):
    """Set Bias Spectroscopy advanced properties."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetSTSAdvancedProps",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设置 Bias Spectroscopy 的高级属性。", "Set the advanced properties of Bias Spectroscopy."),
            parameters=[
                ParameterSpec(
                    name="reset_bias", type="int",
                    description=tr("扫描后复位 bias（0=不改，1=On，2=Off）", "Reset bias after the sweep (0=no change, 1=On, 2=Off)"),
                    required=True, min_value=0, max_value=2,
                ),
                ParameterSpec(
                    name="z_controller_hold", type="int",
                    description=tr("扫描期间保持 Z 控制器（0=不改，1=On，2=Off）", "Hold the Z controller during the sweep (0=no change, 1=On, 2=Off)"),
                    required=True, min_value=0, max_value=2,
                ),
                ParameterSpec(
                    name="record_final_z", type="int",
                    description=tr("记录最终的 Z（0=不改，1=On，2=Off）", "Record the final Z (0=no change, 1=On, 2=Off)"),
                    required=True, min_value=0, max_value=2,
                ),
                ParameterSpec(
                    name="lockin_run", type="int",
                    description=tr("扫描期间运行 Lock-In（0=不改，1=On，2=Off）", "Run the Lock-In during the sweep (0=no change, 1=On, 2=Off)"),
                    required=True, min_value=0, max_value=2,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "advanced", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call(
            "BiasSpectr_AdvPropsSet",
            params["reset_bias"],
            params["z_controller_hold"],
            params["record_final_z"],
            params["lockin_run"],
        )
        if record.error:
            return SkillResult(
                skill_name="SetSTSAdvancedProps", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="SetSTSAdvancedProps", success=True,
            data={
                "reset_bias": params["reset_bias"],
                "z_controller_hold": params["z_controller_hold"],
                "record_final_z": params["record_final_z"],
                "lockin_run": params["lockin_run"],
            },
            nanonis_calls=[record],
        )


class GetSTSTiming(BaseSkill):
    """Get Bias Spectroscopy timing parameters."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSTSTiming",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Bias Spectroscopy 的时序参数。", "Get the timing parameters of Bias Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "timing", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("BiasSpectr_TimingGet")
        if record.error:
            return SkillResult(
                skill_name="GetSTSTiming", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). BiasSpectr.TimingGet
        # ResponseTypes are 8 floats -> read them from Variables (parsed[2]), not
        # from parsed[i] (which is error string / raw bytes / then out of range).
        parsed = record.return_value
        data: dict = {}
        keys = [
            "z_averaging_time_s", "z_offset_m", "initial_settling_time_s",
            "max_slew_rate_v_per_s", "settling_time_s", "integration_time_s",
            "end_settling_time_s", "z_control_time_s",
        ]
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)):
            for i, key in enumerate(keys):
                if i < len(variables):
                    data[key] = float(variables[i])
        return SkillResult(
            skill_name="GetSTSTiming", success=True,
            data=data, nanonis_calls=[record],
        )


class GetSTSAltZCtrl(BaseSkill):
    """Get Bias Spectroscopy alternative Z controller settings."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSTSAltZCtrl",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Bias Spectroscopy 的备用 Z 控制器设置。", "Get the alternate Z controller settings of Bias Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "zctrl", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("BiasSpectr_AltZCtrlGet")
        if record.error:
            return SkillResult(
                skill_name="GetSTSAltZCtrl", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). BiasSpectr.AltZCtrlGet
        # ResponseTypes ["H","f","f"] -> Variables [enabled, setpoint, settling_time_s].
        parsed = record.return_value
        enabled = False
        setpoint = 0.0
        settling_time_s = 0.0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) >= 3:
            enabled = bool(variables[0])
            setpoint = float(variables[1])
            settling_time_s = float(variables[2])
        return SkillResult(
            skill_name="GetSTSAltZCtrl", success=True,
            data={
                "enabled": enabled,
                "setpoint": setpoint,
                "settling_time_s": settling_time_s,
            },
            nanonis_calls=[record],
        )


class GetZSpectrChannels(BaseSkill):
    """Get the list of recorded channels for Z Spectroscopy."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetZSpectrChannels",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Z Spectroscopy 记录的通道列表。", "Get the list of channels recorded by Z Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "channels", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("ZSpectr_ChsGet")
        if record.error:
            return SkillResult(
                skill_name="GetZSpectrChannels", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). For ZSpectr.ChsGet
        # (ResponseTypes ["i","*i","i","i","*+c"]) Variables[1]=channel indexes
        # (1D int array), Variables[4]=channel names. OLD code read parsed[0]/[1]
        # = the error string / raw bytes, never the data. Index unwrapping is the
        # byte-for-byte twin of GetSTSChannels above — see the note there.
        parsed = record.return_value
        channel_indexes: list = []
        channel_names: list = []
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)):
            if len(variables) > 1 and isinstance(variables[1], (list, tuple)):
                channel_indexes = channel_ids_from_buffer(variables)
            if len(variables) > 4 and isinstance(variables[4], (list, tuple)):
                channel_names = list(variables[4])
        return SkillResult(
            skill_name="GetZSpectrChannels", success=True,
            data={"channel_indexes": channel_indexes, "channel_names": channel_names},
            nanonis_calls=[record],
        )


class SetZSpectrChannels(BaseSkill):
    """Set the recorded channels for Z Spectroscopy."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetZSpectrChannels",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设置 Z Spectroscopy 记录的通道。", "Set the channels recorded by Z Spectroscopy."),
            parameters=[
                ParameterSpec(
                    name="channel_indexes",
                    # declared str (comma-separated); execute() coerces — agent
                    # schema has no list type. See _coerce_int_list.
                    type="str",
                    description=tr("通道索引（0-127），逗号分隔，例如 '0, 1, 2'", "Channel indexes (0-127), comma-separated, e.g. '0, 1, 2'"),
                    required=True,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "channels", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        channel_indexes = _coerce_int_list(params["channel_indexes"])
        # ZSpectr_ChsSet(Channel_indexes) takes ONE arg (the "+*i" wire format
        # sends the length); passing len() too is a TypeError on hardware.
        record = context.safe_call("ZSpectr_ChsSet", channel_indexes)
        if record.error:
            return SkillResult(
                skill_name="SetZSpectrChannels", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="SetZSpectrChannels", success=True,
            data={"channel_indexes": channel_indexes},
            nanonis_calls=[record],
        )


class GetZSpectrRange(BaseSkill):
    """Get Z Spectroscopy range settings."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetZSpectrRange",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Z Spectroscopy 的量程设置。", "Get the range settings of Z Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "range", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("ZSpectr_RangeGet")
        if record.error:
            return SkillResult(
                skill_name="GetZSpectrRange", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). ZSpectr.RangeGet
        # ResponseTypes ["f","f"] -> Variables [z_offset_m, z_sweep_distance_m].
        parsed = record.return_value
        z_offset_m = 0.0
        z_sweep_distance_m = 0.0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) >= 2:
            z_offset_m = float(variables[0])
            z_sweep_distance_m = float(variables[1])
        return SkillResult(
            skill_name="GetZSpectrRange", success=True,
            data={"z_offset_m": z_offset_m, "z_sweep_distance_m": z_sweep_distance_m},
            nanonis_calls=[record],
        )


class SetZSpectrRange(BaseSkill):
    """Set Z Spectroscopy range."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetZSpectrRange",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设置 Z Spectroscopy 的量程。", "Set the range of Z Spectroscopy."),
            parameters=[
                ParameterSpec(
                    name="z_offset_m", type="float",
                    description=tr("Z 偏移，单位米", "Z offset, in metres"), unit="m",
                    required=True,
                ),
                ParameterSpec(
                    name="z_sweep_distance_m", type="float",
                    description=tr("Z 扫描距离，单位米", "Z sweep distance, in metres"), unit="m",
                    required=True, min_value=0.0,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "range", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call(
            "ZSpectr_RangeSet",
            params["z_offset_m"],
            params["z_sweep_distance_m"],
        )
        if record.error:
            return SkillResult(
                skill_name="SetZSpectrRange", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="SetZSpectrRange", success=True,
            data={
                "z_offset_m": params["z_offset_m"],
                "z_sweep_distance_m": params["z_sweep_distance_m"],
            },
            nanonis_calls=[record],
        )


class GetZSpectrRetract(BaseSkill):
    """Get Z Spectroscopy auto-retract configuration."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetZSpectrRetract",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Z Spectroscopy 的自动退针配置。", "Get the automatic withdraw configuration of Z Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "retract", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("ZSpectr_RetractGet")
        if record.error:
            return SkillResult(
                skill_name="GetZSpectrRetract", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). ZSpectr.RetractGet
        # ResponseTypes ["H","f","i","H"] -> Variables [enable, threshold,
        # signal_index, comparison].
        parsed = record.return_value
        enabled = False
        threshold = 0.0
        signal_index = 0
        comparison = ">"
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) >= 4:
            enabled = bool(variables[0])
            threshold = float(variables[1])
            signal_index = int(variables[2])
            comparison = ">" if int(variables[3]) == 0 else "<"
        return SkillResult(
            skill_name="GetZSpectrRetract", success=True,
            data={
                "enabled": enabled,
                "threshold": threshold,
                "signal_index": signal_index,
                "comparison": comparison,
            },
            nanonis_calls=[record],
        )


class SetZSpectrRetract(BaseSkill):
    """Set Z Spectroscopy auto-retract conditions."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetZSpectrRetract",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设置 Z Spectroscopy 的自动退针条件。", "Set the automatic withdraw condition of Z Spectroscopy."),
            parameters=[
                ParameterSpec(
                    name="enabled", type="int",
                    description=tr("启用退针（0=不改，1=On，2=Off）", "Enable withdraw (0=no change, 1=On, 2=Off)"),
                    required=True, min_value=0, max_value=2,
                ),
                ParameterSpec(
                    name="threshold", type="float",
                    description=tr("退针的阈值", "Withdraw threshold"),
                    required=True,
                ),
                ParameterSpec(
                    name="signal_index", type="int",
                    description=tr("阈值所用的信号索引（-1=不改）", "Signal index used for the threshold (-1 = no change)"),
                    required=True,
                ),
                ParameterSpec(
                    name="comparison", type="int",
                    description=tr("比较运算符（0=>，1=<，2=不改）", "Comparison operator (0=>, 1=<, 2=no change)"),
                    required=True, min_value=0, max_value=2,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "retract", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call(
            "ZSpectr_RetractSet",
            params["enabled"],
            params["threshold"],
            params["signal_index"],
            params["comparison"],
        )
        if record.error:
            return SkillResult(
                skill_name="SetZSpectrRetract", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="SetZSpectrRetract", success=True,
            data={
                "enabled": params["enabled"],
                "threshold": params["threshold"],
                "signal_index": params["signal_index"],
                "comparison": params["comparison"],
            },
            nanonis_calls=[record],
        )


class GetSTSDigSync(BaseSkill):
    """Get Bias Spectroscopy digital synchronization setting."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSTSDigSync",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Bias Spectroscopy 的数字同步模式（Off/TTL/PulseSeq）。", "Get the digital sync mode of Bias Spectroscopy (Off/TTL/PulseSeq)."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "digsync", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("BiasSpectr_DigSyncGet")
        if record.error:
            return SkillResult(
                skill_name="GetSTSDigSync", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). DigSyncGet
        # ResponseTypes ["H"] -> Variables[0] = dig_sync.
        parsed = record.return_value
        dig_sync = 0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) > 0:
            dig_sync = int(variables[0])
        _DIGSYNC_MAP = {0: "Off", 1: "TTL Sync", 2: "Pulse Sequence"}
        return SkillResult(
            skill_name="GetSTSDigSync", success=True,
            data={"dig_sync": dig_sync, "dig_sync_label": _DIGSYNC_MAP.get(dig_sync, str(dig_sync))},
            nanonis_calls=[record],
        )


class GetSTSTTLSync(BaseSkill):
    """Get Bias Spectroscopy TTL synchronization configuration."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSTSTTLSync",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Bias Spectroscopy 的 TTL 同步配置。", "Get the TTL sync configuration of Bias Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "ttlsync", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("BiasSpectr_TTLSyncGet")
        if record.error:
            return SkillResult(
                skill_name="GetSTSTTLSync", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). TTLSyncGet
        # ResponseTypes ["H","H","f","f"] -> Variables [ttl_line, ttl_polarity,
        # time_to_on_s, on_duration_s].
        parsed = record.return_value
        ttl_line = 0
        ttl_polarity = 0
        time_to_on_s = 0.0
        on_duration_s = 0.0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) >= 4:
            ttl_line = int(variables[0])
            ttl_polarity = int(variables[1])
            time_to_on_s = float(variables[2])
            on_duration_s = float(variables[3])
        return SkillResult(
            skill_name="GetSTSTTLSync", success=True,
            data={
                "ttl_line": ttl_line,
                "ttl_polarity": ttl_polarity,
                "time_to_on_s": time_to_on_s,
                "on_duration_s": on_duration_s,
            },
            nanonis_calls=[record],
        )


class GetSTSPulseSeqSync(BaseSkill):
    """Get Bias Spectroscopy pulse sequence synchronization configuration."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSTSPulseSeqSync",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Bias Spectroscopy 的脉冲序列同步配置。", "Get the pulse sequence sync configuration of Bias Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "pulseseq", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("BiasSpectr_PulseSeqSyncGet")
        if record.error:
            return SkillResult(
                skill_name="GetSTSPulseSeqSync", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). PulseSeqSyncGet
        # ResponseTypes ["H","I"] -> Variables [pulse_seq_nr, nr_periods].
        parsed = record.return_value
        pulse_seq_nr = 0
        nr_periods = 0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) >= 2:
            pulse_seq_nr = int(variables[0])
            nr_periods = int(variables[1])
        return SkillResult(
            skill_name="GetSTSPulseSeqSync", success=True,
            data={"pulse_seq_nr": pulse_seq_nr, "nr_periods": nr_periods},
            nanonis_calls=[record],
        )


class GetSTSZOffRevert(BaseSkill):
    """Get Bias Spectroscopy Z Offset Revert flag."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSTSZOffRevert",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Bias Spectroscopy 的 Z Offset Revert 标志。", "Get the Z Offset Revert flag of Bias Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "zoffrevert", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("BiasSpectr_ZOffRevertGet")
        if record.error:
            return SkillResult(
                skill_name="GetSTSZOffRevert", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). ZOffRevertGet
        # ResponseTypes ["H"] -> Variables[0] = z_off_revert.
        parsed = record.return_value
        z_off_revert = 0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) > 0:
            z_off_revert = int(variables[0])
        return SkillResult(
            skill_name="GetSTSZOffRevert", success=True,
            data={"z_off_revert": bool(z_off_revert)},
            nanonis_calls=[record],
        )


class GetSTSMLSLockinPerSeg(BaseSkill):
    """Get Bias Spectroscopy MLS Lock-In per segment flag."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSTSMLSLockinPerSeg",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 MLS 模式下的 Lock-In per Segment 标志。", "Get the Lock-In per Segment flag in MLS mode."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "mls", "lockin", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("BiasSpectr_MLSLockinPerSegGet")
        if record.error:
            return SkillResult(
                skill_name="GetSTSMLSLockinPerSeg", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). MLSLockinPerSegGet
        # ResponseTypes ["I"] -> Variables[0] = lockin_per_segment.
        parsed = record.return_value
        lockin_per_seg = 0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) > 0:
            lockin_per_seg = int(variables[0])
        return SkillResult(
            skill_name="GetSTSMLSLockinPerSeg", success=True,
            data={"lockin_per_segment": bool(lockin_per_seg)},
            nanonis_calls=[record],
        )


class SetSTSMLSMode(BaseSkill):
    """Set Bias Spectroscopy sweep mode (Linear / MLS)."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetSTSMLSMode",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设置 Bias Spectroscopy 的扫描模式：Linear 或 MLS。", "Set the sweep mode of Bias Spectroscopy: Linear or MLS."),
            parameters=[
                ParameterSpec(
                    name="mode",
                    type="str",
                    description=tr("'Linear' 或 'MLS'", "'Linear' or 'MLS'"),
                    required=True,
                    allowed_values=["Linear", "MLS"],
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "mls", "mode", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        mode = params["mode"]
        # BiasSpectr_MLSModeSet(Sweep_mode: str) takes ONE arg — the "+*c" wire
        # format sends the string length itself, so passing len(mode) too is a
        # TypeError on real hardware. (Same pattern as the *ChsSet fixes.)
        record = context.safe_call("BiasSpectr_MLSModeSet", mode)
        if record.error:
            return SkillResult(
                skill_name="SetSTSMLSMode", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="SetSTSMLSMode", success=True,
            data={"mode": mode},
            nanonis_calls=[record],
        )


class SetSTSMLSVals(BaseSkill):
    """Set Bias Spectroscopy MLS segment configuration."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetSTSMLSVals",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设置 Bias Spectroscopy MLS 模式的多线段配置。", "Set the multi-line-segment configuration of Bias Spectroscopy MLS mode."),
            # NOTE: all per-segment arrays are declared as str (comma-separated)
            # because the agent schema builder has no list type — execute()
            # coerces each via _coerce_float_list / _coerce_int_list. Programmatic
            # callers may still pass real lists.
            parameters=[
                ParameterSpec(
                    name="bias_start_v",
                    type="str",
                    description=tr("每段的起始 bias（V），逗号分隔，例如 '-1.0, 0.5'", "Start bias of each segment (V), comma-separated, e.g. '-1.0, 0.5'"),
                    required=True,
                ),
                ParameterSpec(
                    name="bias_end_v",
                    type="str",
                    description=tr("每段的终止 bias（V），逗号分隔", "End bias of each segment (V), comma-separated"),
                    required=True,
                ),
                ParameterSpec(
                    name="initial_settling_s",
                    type="str",
                    description=tr("每段的初始建立时间（s），逗号分隔", "Initial settling time of each segment (s), comma-separated"),
                    required=True,
                ),
                ParameterSpec(
                    name="settling_s",
                    type="str",
                    description=tr("每段的建立时间（s），逗号分隔", "Settling time of each segment (s), comma-separated"),
                    required=True,
                ),
                ParameterSpec(
                    name="integration_s",
                    type="str",
                    description=tr("每段的积分时间（s），逗号分隔", "Integration time of each segment (s), comma-separated"),
                    required=True,
                ),
                ParameterSpec(
                    name="steps",
                    type="str",
                    description=tr("每段的步数，逗号分隔的整数", "Number of steps of each segment, comma-separated integers"),
                    required=True,
                ),
                ParameterSpec(
                    name="lockin_run",
                    type="str",
                    description=tr("每段的 Lock-In 运行标志（0=Off，1=On），逗号分隔", "Lock-In run flag of each segment (0=Off, 1=On), comma-separated"),
                    required=True,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "mls", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        bias_start = _coerce_float_list(params["bias_start_v"])
        bias_end = _coerce_float_list(params["bias_end_v"])
        init_settling = _coerce_float_list(params["initial_settling_s"])
        settling = _coerce_float_list(params["settling_s"])
        integration = _coerce_float_list(params["integration_s"])
        steps = _coerce_int_list(params["steps"])
        lockin_run = _coerce_int_list(params["lockin_run"])
        num_segments = len(bias_start)

        # Equal-length validation: all 7 per-segment arrays
        # MUST have the same length. BiasSpectr_MLSValsSet sends num_segments then
        # each array; if they disagree the wire body contradicts the declared
        # segment count → misframed TCP / garbage segment config on the hardware.
        lengths = {
            "bias_start_v": len(bias_start), "bias_end_v": len(bias_end),
            "initial_settling_s": len(init_settling), "settling_s": len(settling),
            "integration_s": len(integration), "steps": len(steps),
            "lockin_run": len(lockin_run),
        }
        if num_segments == 0:
            return SkillResult(
                skill_name="SetSTSMLSVals", success=False,
                error="no MLS segments provided (bias_start_v is empty)")
        mismatched = {k: n for k, n in lengths.items() if n != num_segments}
        if mismatched:
            return SkillResult(
                skill_name="SetSTSMLSVals", success=False,
                error=(f"MLS per-segment arrays must all have {num_segments} "
                       f"elements; got lengths {lengths}"))

        # Bias-bound validation: these arrays are str-typed to
        # fit the agent tool schema, so the automatic ParameterSpec + global
        # bias-limit checks (which only run on NUMERIC params) are BYPASSED. An
        # LLM-hallucinated segment start/end could otherwise reach the junction
        # far outside ±10 V. Enforce the effective global bias bound here.
        try:
            from mast.config import SafetyLimits
            from mast.core.safety import _get_effective_limits
            lim = _get_effective_limits(SafetyLimits())
            lo, hi = float(lim.bias_min_v), float(lim.bias_max_v)
        except Exception:  # pragma: no cover - fall back to spec default
            lo, hi = -10.0, 10.0
        for pname, arr in (("bias_start_v", bias_start), ("bias_end_v", bias_end)):
            for i, v in enumerate(arr):
                if v < lo or v > hi:
                    return SkillResult(
                        skill_name="SetSTSMLSVals", success=False,
                        error=(f"MLS {pname}[{i}] = {v} V is outside the global "
                               f"bias safety bound [{lo}, {hi}] V"))

        record = context.safe_call(
            "BiasSpectr_MLSValsSet",
            num_segments, bias_start, bias_end,
            init_settling, settling, integration,
            steps, lockin_run,
        )
        if record.error:
            return SkillResult(
                skill_name="SetSTSMLSVals", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="SetSTSMLSVals", success=True,
            data={"num_segments": num_segments},
            nanonis_calls=[record],
        )


class SetSTSSafeCond1(BaseSkill):
    """Set Bias Spectroscopy 1st safe condition."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetSTSSafeCond1",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("[DEPRECATED —— Nanonis 里 Bias Spectroscopy 没有 safe-condition API；它总是失败。"
                "请改用 Z 谱学的 SetZSpectrRetract。] 设置 Bias Spectroscopy 的第一条 safe "
                "condition。", "[DEPRECATED - Bias Spectroscopy has no safe-condition API in Nanonis; it always fails. Use SetZSpectrRetract of Z spectroscopy instead.] Set the first safe condition of Bias Spectroscopy.")
            ),
            parameters=[
                ParameterSpec(
                    name="condition", type="int",
                    description=tr("动作：0=不改，1=Off，2=Reverse，3=Stop", "Action: 0=no change, 1=Off, 2=Reverse, 3=Stop"),
                    required=True, min_value=0, max_value=3,
                ),
                ParameterSpec(
                    name="threshold", type="float",
                    description=tr("阈值（NaN=不改）", "Threshold (NaN = no change)"),
                    required=True,
                ),
                ParameterSpec(
                    name="signal_index", type="int",
                    description=tr("信号索引（0-127，-1=不改）", "Signal index (0-127, -1 = no change)"),
                    required=True,
                ),
                ParameterSpec(
                    name="comparison", type="int",
                    description=tr("0=高于，1=低于，2=不改", "0=above, 1=below, 2=no change"),
                    required=True, min_value=0, max_value=2,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "safecond", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        # The Nanonis Bias Spectroscopy module has NO safe-condition / auto-retract
        # feature in nanonis_spm (no BiasSpectr_SafeCond* method exists; grep
        # NanonisClass.py confirms BiasSpectr_* ends at MLSValsGet). The auto-retract
        # safe-condition feature exists only for *Z* Spectroscopy
        # (SetZSpectrRetract / SetZSpectrRetractSecond). The old code called the
        # non-existent BiasSpectr_SafeCond1Set, which only ever returned a cryptic
        # "Method 'BiasSpectr_SafeCond1Set' not found on Nanonis instance" — and
        # only on real hardware. Fail fast with a clear, deterministic message.
        return SkillResult(
            skill_name="SetSTSSafeCond1",
            success=False,
            error=(
                "Bias Spectroscopy has no safe-condition / auto-retract feature in "
                "the Nanonis API. This control only exists for Z Spectroscopy — use "
                "SetZSpectrRetract (main condition) or the 2nd-condition Z retract "
                "skills instead."
            ),
            nanonis_calls=[],
        )


class GetSTSSafeCond1(BaseSkill):
    """Get Bias Spectroscopy 1st safe condition."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetSTSSafeCond1",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("[DEPRECATED —— Nanonis 里 Bias Spectroscopy 没有 safe-condition API；它总是失败。"
                "请改用 Z 谱学的 GetZSpectrRetract。] 取 Bias Spectroscopy 的第一条 safe condition。", "[DEPRECATED - Bias Spectroscopy has no safe-condition API in Nanonis; it always fails. Use GetZSpectrRetract of Z spectroscopy instead.] Get the first safe condition of Bias Spectroscopy.")
            ),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "safecond", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        # No BiasSpectr_SafeCond1Get method exists in nanonis_spm — the
        # safe-condition / auto-retract feature is Z-Spectroscopy-only
        # (GetZSpectrRetract). The old call to BiasSpectr_SafeCond1Get returned
        # only "Method ... not found" on real hardware. Fail fast and clearly.
        return SkillResult(
            skill_name="GetSTSSafeCond1",
            success=False,
            error=(
                "Bias Spectroscopy has no safe-condition / auto-retract feature in "
                "the Nanonis API. This control only exists for Z Spectroscopy — use "
                "GetZSpectrRetract / GetZSpectrRetract2nd instead."
            ),
            nanonis_calls=[],
        )


class SetSTSSafeCond2(BaseSkill):
    """Set Bias Spectroscopy 2nd safe condition."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetSTSSafeCond2",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("[DEPRECATED —— Nanonis 里 Bias Spectroscopy 没有 safe-condition API；它总是失败。"
                "请改用 Z 谱学的第二条 retract（GetZSpectrRetract2nd）。] 设置 Bias Spectroscopy "
                "的第二条 safe condition。", "[DEPRECATED - Bias Spectroscopy has no safe-condition API in Nanonis; it always fails. Use the second retract of Z spectroscopy (GetZSpectrRetract2nd) of Z spectroscopy instead.] Set the second safe condition of Bias Spectroscopy.")
            ),
            parameters=[
                ParameterSpec(
                    name="condition", type="int",
                    description=tr("逻辑：-1=不改，0=Off，1=OR，2=AND，3=THEN", "Logic: -1=no change, 0=Off, 1=OR, 2=AND, 3=THEN"),
                    required=True, min_value=-1, max_value=3,
                ),
                ParameterSpec(
                    name="threshold", type="float",
                    description=tr("阈值（NaN=不改）", "Threshold (NaN = no change)"),
                    required=True,
                ),
                ParameterSpec(
                    name="signal_index", type="int",
                    description=tr("信号索引（0-127，-1=不改）", "Signal index (0-127, -1 = no change)"),
                    required=True,
                ),
                ParameterSpec(
                    name="comparison", type="int",
                    description=tr("0=高于，1=低于，2=不改", "0=above, 1=below, 2=no change"),
                    required=True, min_value=0, max_value=2,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "sts", "safecond", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        # No BiasSpectr_SafeCond2Set method exists in nanonis_spm — the
        # 2nd-condition auto-retract logic (OR/AND/THEN) is Z-Spectroscopy-only
        # (ZSpectr_RetractSecondSet). The old call to BiasSpectr_SafeCond2Set
        # returned only "Method ... not found" on real hardware. Fail fast.
        return SkillResult(
            skill_name="SetSTSSafeCond2",
            success=False,
            error=(
                "Bias Spectroscopy has no 2nd safe-condition / auto-retract feature "
                "in the Nanonis API. This control only exists for Z Spectroscopy — "
                "the 2nd-condition (OR/AND/THEN) retract logic is configured via the "
                "Z Spectroscopy auto-retract skills."
            ),
            nanonis_calls=[],
        )


class SetZSpectrAdvProps(BaseSkill):
    """Set Z Spectroscopy advanced properties."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetZSpectrAdvProps",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设置 Z Spectroscopy 的高级属性。", "Set the advanced properties of Z Spectroscopy."),
            parameters=[
                ParameterSpec(
                    name="time_between_sweeps_s", type="float",
                    description=tr("正扫与反扫之间的时间（s）", "Time between forward and backward sweep (s)"),
                    unit="s", required=True, min_value=0.0,
                ),
                ParameterSpec(
                    name="record_final_z", type="int",
                    description=tr("记录最终的 Z（0=不改，1=On，2=Off）", "Record the final Z (0=no change, 1=On, 2=Off)"),
                    required=True, min_value=0, max_value=2,
                ),
                ParameterSpec(
                    name="lockin_run", type="int",
                    description=tr("运行 Lock-In（0=不改，1=On，2=Off）", "Run the Lock-In (0=no change, 1=On, 2=Off)"),
                    required=True, min_value=0, max_value=2,
                ),
                ParameterSpec(
                    name="reset_z", type="int",
                    description=tr("扫描后复位 Z（0=不改，1=On，2=Off）", "Reset Z after the sweep (0=no change, 1=On, 2=Off)"),
                    required=True, min_value=0, max_value=2,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "advanced", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call(
            "ZSpectr_AdvPropsSet",
            params["time_between_sweeps_s"],
            params["record_final_z"],
            params["lockin_run"],
            params["reset_z"],
        )
        if record.error:
            return SkillResult(
                skill_name="SetZSpectrAdvProps", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="SetZSpectrAdvProps", success=True,
            data={
                "time_between_sweeps_s": params["time_between_sweeps_s"],
                "record_final_z": params["record_final_z"],
                "lockin_run": params["lockin_run"],
                "reset_z": params["reset_z"],
            },
            nanonis_calls=[record],
        )


class GetZSpectrDigSync(BaseSkill):
    """Get Z Spectroscopy digital synchronization setting."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetZSpectrDigSync",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Z Spectroscopy 的数字同步模式（Off/TTL/PulseSeq）。", "Get the digital sync mode of Z Spectroscopy (Off/TTL/PulseSeq)."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "digsync", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("ZSpectr_DigSyncGet")
        if record.error:
            return SkillResult(
                skill_name="GetZSpectrDigSync", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). ZSpectr.DigSyncGet
        # ResponseTypes ["H"] -> Variables[0] = dig_sync.
        parsed = record.return_value
        dig_sync = 0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) > 0:
            dig_sync = int(variables[0])
        _DIGSYNC_MAP = {0: "Off", 1: "TTL Sync", 2: "Pulse Sequence"}
        return SkillResult(
            skill_name="GetZSpectrDigSync", success=True,
            data={"dig_sync": dig_sync, "dig_sync_label": _DIGSYNC_MAP.get(dig_sync, str(dig_sync))},
            nanonis_calls=[record],
        )


class GetZSpectrPulseSeqSync(BaseSkill):
    """Get Z Spectroscopy pulse sequence synchronization configuration."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetZSpectrPulseSeqSync",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Z Spectroscopy 的脉冲序列同步配置。", "Get the pulse sequence sync configuration of Z Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "pulseseq", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("ZSpectr_PulseSeqSyncGet")
        if record.error:
            return SkillResult(
                skill_name="GetZSpectrPulseSeqSync", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). PulseSeqSyncGet
        # ResponseTypes ["H","I"] -> Variables [pulse_seq_nr, nr_periods].
        parsed = record.return_value
        pulse_seq_nr = 0
        nr_periods = 0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) >= 2:
            pulse_seq_nr = int(variables[0])
            nr_periods = int(variables[1])
        return SkillResult(
            skill_name="GetZSpectrPulseSeqSync", success=True,
            data={"pulse_seq_nr": pulse_seq_nr, "nr_periods": nr_periods},
            nanonis_calls=[record],
        )


class GetZSpectrRetract2nd(BaseSkill):
    """Get Z Spectroscopy 2nd auto-retract condition."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetZSpectrRetract2nd",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Z Spectroscopy 的第二条自动退针条件。", "Get the second automatic withdraw condition of Z Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "retract", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        # Real method is ZSpectr_RetractSecondGet (the old "ZSpectr_Retract2ndGet"
        # does not exist -> "Method not found" on hardware). return_value is
        # (error_string, raw_bytes, Variables); ResponseTypes ["i","f","i","H"]
        # -> Variables [second_condition, threshold, signal_index, comparison].
        record = context.safe_call("ZSpectr_RetractSecondGet")
        if record.error:
            return SkillResult(
                skill_name="GetZSpectrRetract2nd", success=False,
                error=record.error, nanonis_calls=[record],
            )
        parsed = record.return_value
        condition = 0
        threshold = 0.0
        signal_index = 0
        comparison = 0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) >= 4:
            condition = int(variables[0])
            threshold = float(variables[1])
            signal_index = int(variables[2])
            comparison = int(variables[3])
        _COND_MAP = {0: "-No-", 1: "OR", 2: "AND", 3: "THEN"}
        return SkillResult(
            skill_name="GetZSpectrRetract2nd", success=True,
            data={
                "condition": condition,
                "condition_label": _COND_MAP.get(condition, str(condition)),
                "threshold": threshold,
                "signal_index": signal_index,
                "comparison": ">" if comparison == 0 else "<",
            },
            nanonis_calls=[record],
        )


class SetZSpectrRetractDelay(BaseSkill):
    """Set Z Spectroscopy retract delay."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetZSpectrRetractDelay",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr("设置 Z Spectroscopy 中正扫与反扫之间的退针延迟（s）。", "Set the withdraw delay between forward and backward sweep in Z Spectroscopy (s)."),
            parameters=[
                ParameterSpec(
                    name="retract_delay_s", type="float",
                    description=tr("退针延迟，单位秒", "Withdraw delay, in seconds"),
                    unit="s", required=True, min_value=0.0,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "retract", "delay", "write"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call(
            "ZSpectr_RetractDelaySet", params["retract_delay_s"],
        )
        if record.error:
            return SkillResult(
                skill_name="SetZSpectrRetractDelay", success=False,
                error=record.error, nanonis_calls=[record],
            )
        return SkillResult(
            skill_name="SetZSpectrRetractDelay", success=True,
            data={"retract_delay_s": params["retract_delay_s"]},
            nanonis_calls=[record],
        )


class GetZSpectrTTLSync(BaseSkill):
    """Get Z Spectroscopy TTL synchronization configuration."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetZSpectrTTLSync",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Z Spectroscopy 的 TTL 同步配置。", "Get the TTL sync configuration of Z Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "ttlsync", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("ZSpectr_TTLSyncGet")
        if record.error:
            return SkillResult(
                skill_name="GetZSpectrTTLSync", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). ZSpectr.TTLSyncGet
        # ResponseTypes ["H","H","f","f"] -> Variables [ttl_line, ttl_polarity,
        # time_to_on_s, on_duration_s].
        parsed = record.return_value
        ttl_line = 0
        ttl_polarity = 0
        time_to_on_s = 0.0
        on_duration_s = 0.0
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)) and len(variables) >= 4:
            ttl_line = int(variables[0])
            ttl_polarity = int(variables[1])
            time_to_on_s = float(variables[2])
            on_duration_s = float(variables[3])
        return SkillResult(
            skill_name="GetZSpectrTTLSync", success=True,
            data={
                "ttl_line": ttl_line,
                "ttl_polarity": ttl_polarity,
                "time_to_on_s": time_to_on_s,
                "on_duration_s": on_duration_s,
            },
            nanonis_calls=[record],
        )


class GetZSpectrTiming(BaseSkill):
    """Get Z Spectroscopy timing parameters."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetZSpectrTiming",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr("取 Z Spectroscopy 的时序参数。", "Get the timing parameters of Z Spectroscopy."),
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["spectroscopy", "z", "timing", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        record = context.safe_call("ZSpectr_TimingGet")
        if record.error:
            return SkillResult(
                skill_name="GetZSpectrTiming", success=False,
                error=record.error, nanonis_calls=[record],
            )
        # return_value is (error_string, raw_bytes, Variables). ZSpectr.TimingGet
        # ResponseTypes are 7 floats -> read them from Variables (parsed[2]).
        parsed = record.return_value
        data: dict = {}
        keys = [
            "z_averaging_time_s", "initial_settling_time_s",
            "max_slew_rate_v_per_s", "settling_time_s",
            "integration_time_s", "end_settling_time_s",
            "z_control_time_s",
        ]
        variables = parsed[2] if isinstance(parsed, (list, tuple)) and len(parsed) > 2 else None
        if isinstance(variables, (list, tuple)):
            for i, key in enumerate(keys):
                if i < len(variables):
                    data[key] = float(variables[i])
        return SkillResult(
            skill_name="GetZSpectrTiming", success=True,
            data=data, nanonis_calls=[record],
        )
