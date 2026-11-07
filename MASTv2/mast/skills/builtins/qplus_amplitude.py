"""qPlus oscillation amplitude as an INDEPENDENT tip-crash indicator.

Premise: on a rig with a qPlus sensor, the ``ocd1`` amplitude
indicates a tip crash. If the tip crashes during approach the amplitude drops
to 0, and it recovers only after the tip is withdrawn.

Why this is worth its own skill
-------------------------------
Every crash check MAST had reads the same physical quantity family — the
tunnelling current, or the variance of a scan built from it. A qPlus sensor's
oscillation amplitude is a *mechanically* independent witness: a tip in contact
is damped to a standstill regardless of what the current preamp reports. On a
rig that has one, it can catch a crash the current-based checks miss, and it
answers a question the current cannot — "is the tip still free to move?".

Design constraints, all of them safety-driven
---------------------------------------------
* **Additive, never subtractive.** Nothing here suppresses or overrides
  :class:`CheckScanForCrash` or the approach-time current checks. It adds a
  verdict; the existing ones keep theirs.
* **Never say "no crash" when the reading failed.** ``status`` is one of
  ``ok`` / ``crash`` / ``unavailable`` / ``no_baseline`` — the last two are not
  "fine", they are "cannot tell", and callers must treat them that way. A crash
  detector whose failure mode looks like success is worse than no detector.
* **Zero amplitude is only meaningful against a baseline.** An amplitude of
  0 V means nothing on its own: the oscillator may simply not be running, or
  the signal may be in different units on the instrument. So the crash verdict needs
  a baseline captured while the tip was known-free, and without one this
  reports ``no_baseline`` rather than guessing.
* **Absent hardware is not a fault.** A rig with no qPlus has no such signal;
  that is ``unavailable``, reported calmly, and it must not make an STM-only
  session look broken.
* **Compare like with like.** A baseline remembers whether the
  fork was being DRIVEN when it was taken (``qplus_amplitude_baseline_excited``),
  and a verdict is only given against a baseline taken in the SAME excitation
  state. See "Two baselines" below.

Two baselines — driven and residual
-----------------------------------
In STM mode a rig may not drive the fork (PLL output off, excitation 0 V),
yet the amplitude channel still carries a residual signal, typically one to two
orders of magnitude below the driven amplitude, and during an AutoApproach the
coarse steps ring it up. The operator's rule, the one this module exists for
(see ``docs/v2/stm_knowledge/20_qplus与振幅.md``), is stated for exactly that
undriven channel: **残余振幅归零 = 撞针** — a tip in contact cannot ring.

An earlier version refused every verdict while the excitation was off, because
the undriven reading compared against the baseline on file always looked
"collapsed". The comparison was wrong, not the channel: the baseline on file had
been taken with the fork DRIVEN (several times the residual), and an undriven
reading divided by a driven baseline is ~0.1 on every healthy tip. Refusing the
channel also switched off the operator's rule in the only mode such a rig
approaches in.

So now: the baseline carries its excitation state; a mismatch is
``no_baseline`` ("capture one in the current state"), never ``crash``; and a
baseline is refused when its own scatter is too large to tell "zero" from
"normal" (a pure noise floor scatters ~50 % of its median; a steady residual
scatters ~10 %). A legacy baseline without the flag counts as driven.

The amplitude is read through the generic signal table (``Signals_NamesGet`` /
``Signals_ValGet``) — verified against the installed ``nanonis_spm``: there is
no ``PLL_AmpGet``; ``PLL_AmpCtrlSetpntGet`` is the amplitude *setpoint*, not the
measurement, and using it would have read back a constant that never falls on a
crash.
"""
from __future__ import annotations

import logging

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill
from mast.i18n import tr

logger = logging.getLogger(__name__)

#: Substrings that identify an oscillation-amplitude channel in the Nanonis
#: signal list. The operator's rig calls it "OCD1 Amplitude"; other setups name
#: the Oscillation Control module differently, so match on the amplitude word
#: plus an oscillation-control hint rather than on one exact string.
_AMP_HINTS: tuple[str, ...] = ("amplitude", "amp")
_OSC_HINTS: tuple[str, ...] = ("oc ", "ocd", "osc", "pll", "excitation")

#: Below this FRACTION of the free-oscillation baseline the tip is judged to be
#: in contact. A crashed qPlus does not droop — it stops — so the gap between
#: "free" and "crashed" is large and the threshold does not need to be tuned
#: per rig. Deliberately not 0: thermal drift and preamp offset leave a small
#: residual, and requiring exact zero would miss real crashes.
_CRASH_FRACTION = 0.10

#: The same threshold for a RESIDUAL baseline (fork not driven). Higher than the
#: driven one because the residual is small and its own scatter is ~10 % of its
#: median: 25 % sits ~6σ below the median, so
#: noise cannot reach it, while "归零" — a fork that cannot ring — lands far
#: below it. (The aux monitor's ``amp_zero_on_approach`` uses 30 % held for
#: 5 s; a SINGLE reading here must clear a lower bar, which is what lets the
#: approach watch act on one read. The watch adds "2 in a row < 50 %" on top.)
_RESIDUAL_CRASH_FRACTION = 0.25

#: A baseline is a median of this many reads, ``_BASELINE_INTERVAL_S`` apart.
#: One read used to be the whole baseline — and one read of a channel whose
#: relative scatter is ~10 % is a denominator that is off by 10 % before
#: anything has happened.
_BASELINE_SAMPLES = 10
_BASELINE_INTERVAL_S = 0.1

#: Refuse a baseline whose robust scatter exceeds this fraction of its median.
#: A demodulator fed by noise alone has an amplitude that scatters ~50 % of its
#: median (Rayleigh-like); a steady residual scatters ~10 %. "Collapsed
#: to zero" cannot be told apart from "normal" on the former, so taking it as a
#: denominator would arm a detector that only produces false alarms.
_BASELINE_MAX_REL_SCATTER = 0.25


def crash_fraction_for(excited: "bool | None") -> float:
    """The collapse threshold for a baseline taken in this excitation state."""
    return _CRASH_FRACTION if excited else _RESIDUAL_CRASH_FRACTION


def _robust_sigma(values: "list[float]", median: float) -> float:
    """1.4826 × MAD — the scatter a single outlier cannot inflate."""
    if not values:
        return 0.0
    devs = sorted(abs(v - median) for v in values)
    mid = len(devs) // 2
    mad = devs[mid] if len(devs) % 2 else 0.5 * (devs[mid - 1] + devs[mid])
    return 1.4826 * mad


def _median(values: "list[float]") -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else 0.5 * (s[mid - 1] + s[mid])


def _signal_names(record) -> list[str]:
    """Channel names out of a ``Signals_NamesGet`` reply ([] when unreadable)."""
    rv = getattr(record, "return_value", None)
    if not (isinstance(rv, (list, tuple)) and len(rv) > 2):
        return []
    body = rv[2]
    if not isinstance(body, (list, tuple)):
        return []
    out: list[str] = []
    for el in body:
        if isinstance(el, str):
            out.append(el)
        elif isinstance(el, (list, tuple)):
            out.extend(str(x) for x in el if isinstance(x, str))
    return out


def find_amplitude_signal(context) -> tuple[int, str] | None:
    """``(index, name)`` of the oscillation-amplitude channel, or None.

    None means "this rig does not expose one" — an STM without a qPlus sensor,
    which is a normal configuration and not an error.

    Checks the remembered index first. That key
    (``qplus_amplitude_signal_index``) was written by ``_set_baseline`` and read
    by nobody — the only reader in the tree was a test — so every call re-fetched
    the whole signal table to re-derive an answer already on file. Honouring it
    also gives the operator a way to OVERRIDE the name match on a rig whose
    channel is called something the hints do not cover, which was the stated
    purpose of exposing the key at all.
    """
    remembered = _remembered_index()
    if remembered is not None:
        return remembered, ""
    rec = context.safe_call("Signals_NamesGet")
    if getattr(rec, "error", ""):
        logger.debug("qplus: Signals_NamesGet failed: %s", rec.error)
        return None
    for i, name in enumerate(_signal_names(rec)):
        low = " ".join(str(name).lower().split())
        if any(a in low for a in _AMP_HINTS) and any(o in low for o in _OSC_HINTS):
            return i, str(name)
    return None


def sample_amplitude(context, idx: int, *, n: int,
                     interval_s: float) -> "tuple[list[float], list, str]":
    """Read the amplitude channel ``n`` times. ``(values, call_records, error)``.

    Stops at the FIRST failed or unparseable read and returns its error: a
    baseline assembled from whichever reads happened to succeed on a flaky
    channel is a denominator nobody can vouch for. Never raises."""
    import time as _t

    values: list[float] = []
    calls: list = []
    for k in range(max(1, int(n))):
        if k and interval_s > 0:
            _t.sleep(interval_s)
        try:
            rec = context.safe_call("Signals_ValGet", int(idx), 1)
        except Exception as exc:  # noqa: BLE001 — a failed read is a failed read
            return values, calls, tr(f"读取振幅失败（signal {idx}）：{exc}",
                                     f"Failed to read the amplitude (signal {idx}): {exc}")
        calls.append(rec)
        if getattr(rec, "error", ""):
            return values, calls, tr(f"读取振幅失败（signal {idx}）：{rec.error}",
                                     f"Failed to read the amplitude (signal {idx}): {rec.error}")
        v = _scalar(rec)
        if v is None:
            return values, calls, tr(f"振幅读数无法解析（signal {idx}）",
                                     f"Amplitude reading could not be parsed (signal {idx})")
        values.append(float(v))
    return values, calls, ""


def baseline_refusal(median: float, sigma: float, excited: "bool | None",
                     excitation_note: str = "") -> str:
    """Why these reads must NOT become a baseline ("" = they may).

    Shared by the stored baseline (``ReadTipOscillationAmplitude``) and the
    approach-time watch (``approach.AutoApproach``), so both refuse the same
    things for the same reasons."""
    if excited is None:
        return tr("读不到 PLL 激励状态 —— 不知道这条基线是在音叉被驱动还是没被驱动时取的，"
                  "而两种状态下的振幅不是同一个量，没法拿来比。**不记基线。**"
                  + (f"（{excitation_note}）" if excitation_note else ""),
                  "The PLL excitation state cannot be read - it is unknown whether this "
                  "baseline was taken with the fork driven or not, and the amplitude in "
                  "the two states is not the same quantity, so it cannot be compared. "
                  "**No baseline recorded.**"
                  + (f" ({excitation_note})" if excitation_note else ""))
    if not median > 0:
        return tr(f"振幅中位数是 {median:.4g} —— 「归零」需要一个不为零的参照。**不记基线。**",
                  f"The amplitude median is {median:.4g} - \"dropped to zero\" needs a "
                  "non-zero reference. **No baseline recorded.**")
    rel = sigma / median
    if rel > _BASELINE_MAX_REL_SCATTER:
        return tr(f"振幅散布 σ={sigma:.3g} 是中位数 {median:.4g} 的 {rel:.0%}"
                  f"（上限 {_BASELINE_MAX_REL_SCATTER:.0%}）—— 这更像噪声底而不是一个稳定的"
                  "信号，在它上面分不出「归零」与「正常」。**不记基线。**",
                  f"The amplitude scatter sigma={sigma:.3g} is {rel:.0%} of the median "
                  f"{median:.4g} (limit {_BASELINE_MAX_REL_SCATTER:.0%}) - this looks like a "
                  "noise floor rather than a steady signal, and \"dropped to zero\" cannot "
                  "be told apart from \"normal\" on it. **No baseline recorded.**")
    return ""


class ReadTipOscillationAmplitude(BaseSkill):
    """Read the qPlus oscillation amplitude (and remember it as a baseline)."""

    #: How a baseline is sampled. Class attributes so tests can shorten the wait
    #: without a new parameter (a parameter is a three-place change).
    _baseline_samples = _BASELINE_SAMPLES
    _baseline_interval_s = _BASELINE_INTERVAL_S

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ReadTipOscillationAmplitude",
            version="1.1.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("读取 qPlus 振荡振幅（OCD1 Amplitude 等通道）。可作为**独立于电流**的"
                "针尖状态判据：针尖接触表面后振幅会被阻尼到接近 0，退针后才恢复。"
                "没有 qPlus 的机器上返回 status='unavailable'（这不是故障）。", "Read the qPlus oscillation amplitude (OCD1 Amplitude and similar channels). Can serve as a tip-state criterion **independent of the current**: once the tip touches the surface the amplitude is damped to near 0, and recovers only after withdrawing. On machines without a qPlus it returns status='unavailable' (this is not a fault).")
            ),
            parameters=[
                ParameterSpec(
                    name="signal_index",
                    type="int",
                    description=(
                        tr("显式指定振幅信号索引；-1 = 按通道名自动查找。"
                        "自动查找不到时返回 unavailable，不会猜一个索引。", "Explicitly specify the amplitude signal index; -1 = look it up automatically by channel name. If the automatic lookup finds nothing it returns unavailable and does not guess an index.")
                    ),
                    required=False,
                    default=-1,
                ),
                ParameterSpec(
                    name="set_baseline",
                    type="bool",
                    description=(
                        tr("True = 连读若干次、取中位数记为「未接触基线」，并记下取基线时"
                        "音叉有没有被驱动（激励开/关）——之后只与**同一激励状态**下的读数相比。"
                        "**只应在确认针尖未接触表面时调用**（进针前、或退针之后）。"
                        "基线是撞针判据的分母；读不到激励状态或读数散布太大（像噪声底）时拒绝记录。",
                        "True = read several times, record the median as the \"non-contact "
                        "baseline\", and record whether the fork was being driven (excitation "
                        "on/off) when it was taken - later readings are compared with it only "
                        "in the **same excitation state**. **Call only when you are sure the "
                        "tip is not in contact with the surface** (before approach, or after "
                        "withdrawing). The baseline is the denominator of the tip-crash "
                        "criterion; it is refused when the excitation state cannot be read or "
                        "the readings scatter too much (a noise floor).")
                    ),
                    required=False,
                    default=False,
                ),
            ],
            estimated_duration_s=0.3,
            tags=["qplus", "afm", "read", "tip"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        idx = int(params.get("signal_index", -1))
        name = ""
        if idx < 0:
            found = find_amplitude_signal(context)
            if found is None:
                return SkillResult(
                    skill_name="ReadTipOscillationAmplitude", success=True,
                    data={"status": "unavailable", "amplitude": None,
                          "note": (tr("信号表里没有振荡振幅通道 —— 这台机器很可能没有 "
                                   "qPlus 传感器。这不是故障，只是这条判据用不上。", "No oscillation amplitude channel in the signal table - this machine most likely has no qPlus sensor. This is not a fault, this criterion is just not applicable."))})
            idx, name = found
        if params.get("set_baseline"):
            return self._capture_baseline(context, int(idx), name)
        rec = context.safe_call("Signals_ValGet", int(idx), 1)
        if getattr(rec, "error", ""):
            return SkillResult(
                skill_name="ReadTipOscillationAmplitude", success=False,
                error=tr(f"读取振幅失败（signal {idx}）：{rec.error}", f"Failed to read the amplitude (signal {idx}): {rec.error}"),
                nanonis_calls=[rec])
        amp = _scalar(rec)
        if amp is None:
            return SkillResult(
                skill_name="ReadTipOscillationAmplitude", success=False,
                error=tr(f"振幅读数无法解析（signal {idx}）", f"Amplitude reading could not be parsed (signal {idx})"),
                nanonis_calls=[rec])
        data = {"status": "ok", "amplitude": amp, "signal_index": int(idx),
                "signal_name": name}
        return SkillResult(skill_name="ReadTipOscillationAmplitude",
                           success=True, data=data, nanonis_calls=[rec])

    def _capture_baseline(self, context, idx: int, name: str) -> SkillResult:
        """Median of several reads + the excitation state they were taken in.

        A refused baseline is ``success=False`` with the reading still in
        ``data`` (``status`` stays ``ok``: the channel answered) and
        ``baseline_set`` False. The caller asked for a baseline and did not get
        one — that must not look like success."""
        exc_on, exc_note = _excitation_state(context)
        vals, calls, err = sample_amplitude(context, idx, n=self._baseline_samples,
                                            interval_s=self._baseline_interval_s)
        if err:
            return SkillResult(skill_name="ReadTipOscillationAmplitude",
                               success=False, error=err, nanonis_calls=calls)
        med = _median(vals)
        sigma = _robust_sigma(vals, med)
        data = {"status": "ok", "amplitude": med, "signal_index": int(idx),
                "signal_name": name, "samples_n": len(vals), "sigma": sigma,
                "excitation_on": exc_on, "baseline_set": False}
        why = baseline_refusal(med, sigma, exc_on, exc_note)
        if why:
            data["note"] = why
            return SkillResult(skill_name="ReadTipOscillationAmplitude",
                               success=False, error=why, data=data,
                               nanonis_calls=calls)
        _set_baseline(context, med, idx, name, excited=exc_on, sigma=sigma)
        data["baseline_set"] = True
        data["baseline_excited"] = exc_on
        data["note"] = (
            tr(f"已记基线 {med:.4g}（{len(vals)} 次读数的中位数，σ {sigma:.2g}），"
               f"取于激励{'开' if exc_on else '关'}——之后只与激励{'开' if exc_on else '关'}"
               "时的读数相比。",
               f"Baseline recorded: {med:.4g} (median of {len(vals)} reads, sigma "
               f"{sigma:.2g}), taken with the excitation {'on' if exc_on else 'off'} - "
               f"later readings are compared with it only while the excitation is "
               f"{'on' if exc_on else 'off'}."))
        return SkillResult(skill_name="ReadTipOscillationAmplitude",
                           success=True, data=data, nanonis_calls=calls)


class CheckTipCrashByAmplitude(BaseSkill):
    """Judge tip contact from the oscillation amplitude against its baseline.

    The skill itself SUCCEEDS whenever the analysis ran; the verdict is in
    ``data["status"]``. Callers must branch on that, and must treat
    ``unavailable`` / ``no_baseline`` as "cannot tell" — never as "no crash".
    """

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CheckTipCrashByAmplitude",
            version="1.1.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("用 qPlus 振幅判断针尖是否已接触表面（撞针）。只与**同一激励状态**下取的"
                "基线比：激励开时低于基线 10% 判撞针；激励关时看残余振幅，低于残余基线 25% "
                "（即「归零」）判撞针。**这是对电流类判据的补充，不替代它们。**"
                "status ∈ ok|crash|unavailable|no_baseline —— 后两者是「判断不了」，"
                "不是「没撞」；基线与当前激励状态不一致也是 no_baseline。",
                "Use the qPlus amplitude to judge whether the tip has touched the surface "
                "(tip crash). Compared only with a baseline taken in the **same excitation "
                "state**: with the excitation on, below 10% of the baseline is a crash; with "
                "it off, the residual amplitude is used and below 25% of the residual "
                "baseline (i.e. \"dropped to zero\") is a crash. **This complements the "
                "current-based criteria, it does not replace them.** status is one of "
                "ok|crash|unavailable|no_baseline - the last two mean \"cannot tell\", not "
                "\"no crash\"; a baseline taken in the other excitation state is also "
                "no_baseline.")
            ),
            parameters=[
                ParameterSpec(
                    name="signal_index", type="int", required=False, default=-1,
                    description=tr("显式振幅信号索引；-1 = 自动查找。", "Explicit amplitude signal index; -1 = automatic lookup.")),
            ],
            estimated_duration_s=0.3,
            tags=["qplus", "afm", "crash", "tip", "safety"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        # 早先的版本在激励关着时一律拒绝判定(crash advisory 反复出现,零真阳性),
        # 但当时的诊断「音叉没被驱动时振幅通道没有判据能力」**错了一半**,改法因此也错了:
        #
        #   * 误报来自**拿未驱动的读数比驱动时取的基线**:基线是驱动时取的(量级大),
        #     激励关着的残余读数小得多 —— 每一次比都是 ~0.1,健康针尖也「塌了」。
        #   * 而残余振幅本身**正是操作员要的那条判据**:进针时它被粗动步踢起来,
        #     扎进去以后踢不动 ⇒ 归零。把激励关一律判成
        #     ``unavailable``,等于把这类机器唯一会用的那种进针模式下的判据整个关掉。
        #
        # 所以改成**同态比较**:基线记着自己取于激励开还是关(``_set_baseline``);
        # 状态一致才判,不一致报 ``no_baseline``(请在当前状态下重取基线)——绝不报
        # ``crash``。读不到激励状态仍是 ``unavailable``:不知道这个读数是哪一种量的时候,
        # 它不代表任何东西。
        exc_on, exc_note = _excitation_state(context)
        if exc_on is None:
            return SkillResult(
                skill_name="CheckTipCrashByAmplitude", success=True,
                data={"status": "unavailable", "crash_indicator": None,
                      "excitation_on": exc_on, "note": exc_note})
        read = ReadTipOscillationAmplitude().execute(
            context, {"signal_index": params.get("signal_index", -1)})
        if not read.success:
            # A failed READ is not a verdict. Report it as such.
            return SkillResult(
                skill_name="CheckTipCrashByAmplitude", success=True,
                data={"status": "unavailable", "crash_indicator": None,
                      "note": tr(f"振幅读取失败，无法判断：{read.error}", f"Amplitude read failed, cannot judge: {read.error}")},
                nanonis_calls=list(read.nanonis_calls))
        if read.data.get("status") != "ok":
            return SkillResult(
                skill_name="CheckTipCrashByAmplitude", success=True,
                data={"status": "unavailable", "crash_indicator": None,
                      "note": read.data.get("note", tr("没有可用的振幅通道", "No usable amplitude channel"))},
                nanonis_calls=list(read.nanonis_calls))

        amp = float(read.data["amplitude"])
        base = _get_baseline(context)
        if base is None or base <= 0:
            return SkillResult(
                skill_name="CheckTipCrashByAmplitude", success=True,
                data={"status": "no_baseline", "crash_indicator": None,
                      "amplitude": amp, "excitation_on": exc_on,
                      "note": (tr("没有自由振荡基线，无法判断振幅是否塌了。"
                               "请在针尖确认未接触时先调用 "
                               "ReadTipOscillationAmplitude(set_baseline=True)。", "No free oscillation baseline, cannot judge whether the amplitude has collapsed. Call ReadTipOscillationAmplitude(set_baseline=True) first, when you are sure the tip is not in contact."))},
                nanonis_calls=list(read.nanonis_calls))

        base_exc, legacy = baseline_excitation()
        if base_exc != exc_on:
            # 两种状态下的振幅不是同一个量 —— 驱动时 ~10² pm 量级,残余 ~10 pm 量级。
            # 比了就会误报:未驱动读数 / 驱动基线 ≈ 0.1,健康针尖被判「塌了」。
            # 反过来(驱动读数 / 残余基线 ≈ 10)则永远判不出撞针。两个方向都不判。
            return SkillResult(
                skill_name="CheckTipCrashByAmplitude", success=True,
                data={"status": "no_baseline", "crash_indicator": None,
                      "amplitude": amp, "baseline": base,
                      "excitation_on": exc_on, "baseline_excited": base_exc,
                      "note": (tr(
                          f"基线取于激励{'开' if base_exc else '关'}"
                          + ("（旧基线没有记录激励状态，按「开」处理）" if legacy else "")
                          + f"，现在激励{'开' if exc_on else '关'} —— 两种状态下的振幅不是"
                          "同一个量，不能相比（早先的误报就是拿未驱动读数比驱动基线）。"
                          "请在针尖确认未接触时、**在当前状态下**重新取基线："
                          "ReadTipOscillationAmplitude(set_baseline=True)。",
                          f"The baseline was taken with the excitation "
                          f"{'on' if base_exc else 'off'}"
                          + (" (the legacy baseline did not record the excitation state "
                             "and is treated as 'on')" if legacy else "")
                          + f", and the excitation is now {'on' if exc_on else 'off'} - the "
                          "amplitude in the two states is not the same quantity and cannot "
                          "be compared (the earlier false alarms came from comparing an undriven "
                          "reading with a driven baseline). Take a new baseline **in the "
                          "current state** while the tip is confirmed out of contact: "
                          "ReadTipOscillationAmplitude(set_baseline=True)."))},
                nanonis_calls=list(read.nanonis_calls))

        thr = crash_fraction_for(exc_on)
        frac = amp / base
        crashed = frac < thr
        if exc_on:
            note = (tr(f"振幅 {amp:.4g} 只有自由振荡基线 {base:.4g} 的 "
                       f"{frac:.1%}（阈值 {thr:.0%}）—— "
                       "针尖很可能已接触表面。退针后振幅应恢复；"
                       "若退针后仍不恢复，则不是撞针而是振荡回路本身的问题。", f"Amplitude {amp:.4g} is only {frac:.1%} of the free oscillation baseline {base:.4g} (threshold {thr:.0%}) - the tip has most likely touched the surface. The amplitude should recover after withdrawing; if it does not recover after withdrawing, then it is not a tip crash but a problem with the oscillation circuit itself.")
                    if crashed else
                    tr(f"振幅为基线的 {frac:.1%}，针尖仍在自由振荡。"
                       "（这不排除电流类判据发现的问题。）", f"Amplitude is {frac:.1%} of the baseline, the tip is still oscillating freely. (This does not rule out problems found by the current-based criteria.)"))
        else:
            note = (tr(f"残余振幅（激励关）{amp:.4g} 只有残余基线 {base:.4g} 的 "
                       f"{frac:.1%}（阈值 {thr:.0%}）—— 音叉振不起来了，"
                       "针尖很可能已接触表面（操作员规则：残余振幅归零 = 撞针）。"
                       "退针后残余振幅应恢复；若退针后仍不恢复，先查信号链而不是针尖。",
                       f"The residual amplitude (excitation off) {amp:.4g} is only "
                       f"{frac:.1%} of the residual baseline {base:.4g} (threshold "
                       f"{thr:.0%}) - the fork cannot ring any more, so the tip has most "
                       "likely touched the surface (operator rule: residual amplitude at "
                       "zero = crash). It should recover after withdrawing; if it does not, "
                       "check the signal chain before the tip.")
                    if crashed else
                    tr(f"残余振幅（激励关）为残余基线的 {frac:.1%}，没有归零。"
                       "（残余判据只证实撞针、不证实没撞：它没响不排除电流类判据发现的问题。）",
                       f"The residual amplitude (excitation off) is {frac:.1%} of the residual "
                       "baseline - it has not dropped to zero. (The residual criterion can "
                       "confirm a crash but not rule one out: its silence does not rule out "
                       "problems found by the current-based criteria.)"))
        return SkillResult(
            skill_name="CheckTipCrashByAmplitude", success=True,
            data={"status": "crash" if crashed else "ok",
                  "crash_indicator": bool(crashed),
                  "amplitude": amp, "baseline": base,
                  "fraction_of_baseline": round(frac, 4),
                  "threshold_fraction": thr,
                  "excitation_on": exc_on,
                  "mode": "driven" if exc_on else "residual",
                  "note": note},
            nanonis_calls=list(read.nanonis_calls))


# ── baseline storage ─────────────────────────────────────────────────
#
# The InstrumentProfile — same store the dI/dV calibration lives in, persisted
# across runs by its sink, so a baseline taken this morning still means
# something this afternoon.
#
# Both keys are registered in ``instrument_profile._CONFIG_SPEC``. That is not
# optional: the profile ``sanitize()``s every write against that spec and
# SILENTLY DROPS unknown keys. An unregistered key would have written cleanly,
# read back None forever, and left this crash detector permanently in
# "no_baseline" — the exact silent-no-op shape this repo has been bitten by
# before (SettingsStore.KNOWN_KEYS, override_store._ALL_FILES).
#
# The two companions — ``qplus_amplitude_baseline_excited`` (1.0 /
# 0.0) and ``qplus_amplitude_baseline_sigma`` — are runtime-written, so they
# live in ``_CALIB_KEYS`` (floats pass through), are echoed by the settings form
# (``INSTRUMENT_CALIB_KEYS``) and are cleared with the baseline on a tip change
# (``TIP_BOUND_KEYS``).


def _set_baseline(context, amp: float, idx: int, name: str, *,
                  excited: "bool | None" = None,
                  sigma: "float | None" = None) -> None:
    """Persist the baseline and the excitation state it was taken in.

    Best-effort; never raises. An unknown state or scatter REMOVES the stale
    companion key rather than leaving the previous baseline's flag attached to
    this one."""
    try:
        from mast.core import instrument_profile as ip

        prof = ip.get_profile()
        prof["qplus_amplitude_baseline"] = float(amp)
        prof["qplus_amplitude_signal_index"] = int(idx)
        if excited is None:
            prof.pop("qplus_amplitude_baseline_excited", None)
        else:
            prof["qplus_amplitude_baseline_excited"] = 1.0 if excited else 0.0
        if sigma is None:
            prof.pop("qplus_amplitude_baseline_sigma", None)
        else:
            prof["qplus_amplitude_baseline_sigma"] = float(sigma)
        ip.set_profile(prof)
        sink = getattr(ip, "_persist_sink", None)
        if callable(sink):
            sink(ip.get_profile())
    except Exception as exc:  # noqa: BLE001 — persistence must not break a read
        logger.debug("qplus baseline persist failed: %s", exc)


def _remembered_index() -> int | None:
    """The amplitude signal index on file, or None to fall back to a name scan.

    -1 is the documented "auto-discover" sentinel and must NOT be treated as a
    real index — passing it to ``Signals_ValGet`` would read whatever the
    controller does with a negative channel."""
    try:
        from mast.core.instrument_profile import get_config

        v = get_config("qplus_amplitude_signal_index", None)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return int(v)
    except Exception as exc:  # noqa: BLE001
        logger.debug("qplus remembered index read failed: %s", exc)
    return None


def _excitation_state(context) -> "tuple[bool | None, str]":
    """返回音叉激励是否有效：(True/False/None, 说明)。
    同时要求 PLL_OutOnOffGet 显示输出开启，且 PLL_ExcitationGet 的激励幅度大于零。
    任一证据不可读返回 None；此时振幅读数不能支持基于自由振荡基线的判定。
    """
    on = None
    exc_v = None
    try:
        rec = context.safe_call("PLL_OutOnOffGet", 1)
        if not getattr(rec, "error", ""):
            raw = _first_scalar(getattr(rec, "return_value", None))
            if raw is not None:
                on = bool(int(raw))
    except Exception:  # noqa: BLE001 — 读不到就是读不到,不抛
        logger.debug("读 PLL 输出开关失败", exc_info=True)
    try:
        rec_v = context.safe_call("PLL_ExcitationGet", 1)
        if not getattr(rec_v, "error", ""):
            exc_v = _first_scalar(getattr(rec_v, "return_value", None))
    except Exception:  # noqa: BLE001
        logger.debug("读 PLL 激励幅度失败", exc_info=True)

    if on is None or exc_v is None:
        return None, (tr("读不到 PLL 激励状态 —— **判不了**是否撞针。"
                      "音叉有没有被驱动不知道的时候,振幅读数不代表任何东西。", "Cannot read the PLL excitation state - **cannot tell** whether there was a tip crash. When it is unknown whether the tuning fork is being driven, an amplitude reading means nothing."))
    if not on or float(exc_v) <= 0.0:
        # 这句话从前是「振幅通道无判据能力」。那是误诊 —— 无判据能力
        # 的是「未驱动读数 / 驱动基线」这个**比值**,不是通道本身(见模块 docstring 的
        # 「Two baselines」)。现在只陈述事实:没被驱动,读到的是残余信号,只能与残余基线比。
        return False, (tr(f"PLL 激励关闭(输出 {'开' if on else '关'},"
                       f"激励 {float(exc_v):.4g} V)—— 音叉没被驱动,振幅通道上是**残余信号**:"
                       "只能与激励关时取的残余基线相比,不能与驱动时的自由振荡基线相比。",
                       f"PLL excitation is off (output {'on' if on else 'off'}, excitation "
                       f"{float(exc_v):.4g} V) - the fork is not driven and the amplitude "
                       "channel carries a **residual signal**: it can only be compared with a "
                       "residual baseline taken with the excitation off, never with a driven "
                       "free-oscillation baseline."))
    return True, ""


def _first_scalar(rv):
    """回包里的第一个标量(两种形态都接,§2.21)。"""
    if not isinstance(rv, (list, tuple)) or len(rv) <= 2:
        return None
    vals = rv[2]
    if isinstance(vals, (int, float)) and not isinstance(vals, bool):
        return float(vals)
    if isinstance(vals, (list, tuple)) and vals:
        item = vals[0]
        if isinstance(item, (list, tuple)) and item:
            item = item[0]
        try:
            return float(item)
        except (TypeError, ValueError):
            return None
    return None


def _get_baseline(context) -> float | None:
    try:
        from mast.core.instrument_profile import get_config

        v = get_config("qplus_amplitude_baseline", None)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v)
    except Exception as exc:  # noqa: BLE001
        logger.debug("qplus baseline read failed: %s", exc)
    return None


def baseline_excitation() -> "tuple[bool, bool]":
    """``(excited, legacy)`` for the stored baseline.

    ``legacy`` = the baseline predates the excitation-state flag and recorded no state. Such a
    baseline counts as DRIVEN: that is what ``set_baseline`` was documented to
    capture ("free oscillation"), and it is the reading the earlier false alarms came
    from (a driven baseline vs a much smaller undriven residual). Treating it as
    driven makes an undriven check answer ``no_baseline`` instead of a false
    crash."""
    try:
        from mast.core.instrument_profile import get_config

        v = get_config("qplus_amplitude_baseline_excited", None)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return bool(v >= 0.5), False
        if isinstance(v, bool):
            return v, False
    except Exception as exc:  # noqa: BLE001
        logger.debug("qplus baseline excitation read failed: %s", exc)
    return True, True


def _scalar(record) -> float | None:
    """First numeric value out of a Signals_ValGet reply."""
    rv = getattr(record, "return_value", None)
    if not (isinstance(rv, (list, tuple)) and len(rv) > 2):
        return None
    body = rv[2]
    if isinstance(body, (int, float)) and not isinstance(body, bool):
        return float(body)
    if isinstance(body, (list, tuple)):
        for el in body:
            if isinstance(el, (int, float)) and not isinstance(el, bool):
                return float(el)
    return None


__all__ = [
    "ReadTipOscillationAmplitude",
    "CheckTipCrashByAmplitude",
    "find_amplitude_signal",
    "sample_amplitude",
    "baseline_refusal",
    "baseline_excitation",
    "crash_fraction_for",
]
