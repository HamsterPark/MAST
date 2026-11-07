"""qPlus 振幅作为独立于电流的撞针判据。

判据::

    在有 qplus 的情况下，ocd1 amplitude 可以作为是否撞针的依据；
    若进针时撞针了，振幅会归 0，针尖拔出来之后才会恢复

在此之前 MAST 的每一个撞针判据读的都是同一族物理量 —— 隧道电流，或者由它构成的
扫描图的方差。振荡振幅是**机械上独立**的证人：接触了的针尖会被阻尼到停摆，
与电流前置放大器报什么无关。

这份测试钉住的是三件事，每一件都是安全性的：

1. **它绝不会在读不到时说「没撞」。** ``unavailable`` / ``no_baseline`` 是
   「判断不了」，不是「没事」。一个失败起来像成功的撞针检测比没有更糟。
2. **零振幅只有相对基线才有意义。** 没有自由振荡基线时必须报 ``no_baseline``
   而不是猜。
3. **没有 qPlus 的机器上它安静地不适用**，不会让纯 STM 的会话看起来坏了。

另外钉住那个静默失效的坑：基线的两个键必须登记在
``instrument_profile._CONFIG_SPEC`` 里 —— 该模块对每次写入做 ``sanitize()``，
未登记的键会被**静默丢弃**，写得进读不到，判据将永远停在 ``no_baseline``。

从仓库根运行::

    .venv-v2-py313/Scripts/python.exe -m pytest \
        tests/v2/unit/skills/test_qplus_amplitude_crash.py -q
"""
from __future__ import annotations

# ── path bootstrap ───────────────────────────────────────────────────────────
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("nanonis_spm", MagicMock())


def _find_mastv2_root() -> str:
    p = Path(__file__).resolve()
    while p.parent != p:
        candidate = p / "MASTv2"
        if candidate.is_dir():
            return str(candidate)
        p = p.parent
    raise RuntimeError("MASTv2 dir not found")


_MASTV2_ROOT = _find_mastv2_root()
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

import pytest  # noqa: E402

from mast.core.types import NanonisCallRecord  # noqa: E402
from mast.skills.builtins.qplus_amplitude import (  # noqa: E402
    CheckTipCrashByAmplitude,
    ReadTipOscillationAmplitude,
    find_amplitude_signal,
)

#: A qPlus rig's signal table. The amplitude channel is named the way the
#: operator's Nanonis names it.
QPLUS_NAMES = ["Current (A)", "Bias (V)", "Z (m)",
               "OC D1 Amplitude (m)", "Frequency Shift (Hz)"]
#: A plain STM: no oscillation control at all.
STM_NAMES = ["Current (A)", "Bias (V)", "Z (m)", "LIX 1 omega (A)"]

FREE = 1.2e-10          # free-oscillation amplitude


class _Ctx:
    """默认是一台**音叉被驱动着**的机器(PLL 输出开、激励 0.2 V)。

    这不再是可以省略的细节:激励关着时振幅通道**没有判据能力**,撞针判据
    一律返回 ``unavailable``。这些用例问的是「振幅塌了算不算撞针」,所以它们的前提
    本来就是音叉在振 —— 现在把那个前提**写出来**,而不是靠假机器碰巧返回 0。
    """

    def __init__(self, amp, names=QPLUS_NAMES, fail="", *,
                 excitation_on=1, excitation_v=0.2, amps=None):
        self.amp, self.names, self.fail = amp, names, fail
        self.excitation_on, self.excitation_v = excitation_on, excitation_v
        #: 一串依次返回的振幅(取基线时连读多次用);用完了就停在最后一个。
        self.amps = list(amps) if amps else None
        self.calls: list = []

    def safe_call(self, verb, *a, **k):
        self.calls.append(verb)
        err = self.fail if verb == "Signals_ValGet" else ""
        if verb == "Signals_NamesGet":
            rv = ("", b"", [list(self.names)])
        elif verb == "Signals_ValGet":
            if self.amps:
                v = self.amps.pop(0) if len(self.amps) > 1 else self.amps[0]
            else:
                v = self.amp
            rv = ("", b"", [v])
        elif verb == "PLL_OutOnOffGet":
            rv = ("", b"", [self.excitation_on])
        elif verb == "PLL_ExcitationGet":
            rv = ("", b"", [self.excitation_v])
        else:
            rv = ("", b"", [0.0])
        return NanonisCallRecord(method=verb, args=a, kwargs={},
                                 return_value=rv, error=err)


_BASELINE_KEYS = ("qplus_amplitude_baseline", "qplus_amplitude_signal_index",
                  "qplus_amplitude_baseline_excited", "qplus_amplitude_baseline_sigma")


@pytest.fixture(autouse=True)
def _clean_profile(monkeypatch):
    """每个用例从「没有基线」开始 —— 基线是持久化的，会串味。

    取基线现在是连读若干次;间隔在测试里归零,免得每条用例白等一秒。"""
    from mast.core import instrument_profile as ip
    monkeypatch.setattr(ReadTipOscillationAmplitude, "_baseline_interval_s", 0.0)
    prof = ip.get_profile()
    for k in _BASELINE_KEYS:
        prof.pop(k, None)
    ip.set_profile(prof)
    yield
    prof = ip.get_profile()
    for k in _BASELINE_KEYS:
        prof.pop(k, None)
    ip.set_profile(prof)


def _baseline(amp=FREE):
    ReadTipOscillationAmplitude().execute(_Ctx(amp), {"set_baseline": True})


# ════════════════════════════════════════════════════════════════════════════
# 1 · 判据本身
# ════════════════════════════════════════════════════════════════════════════

def test_a_collapsed_amplitude_is_reported_as_a_crash():
    _baseline()
    d = CheckTipCrashByAmplitude().execute(_Ctx(FREE * 0.02), {}).data
    assert d["status"] == "crash"
    assert d["crash_indicator"] is True
    assert d["fraction_of_baseline"] == pytest.approx(0.02, abs=1e-3)


def test_a_freely_oscillating_tip_is_not_a_crash():
    _baseline()
    d = CheckTipCrashByAmplitude().execute(_Ctx(FREE * 0.96), {}).data
    assert d["status"] == "ok" and d["crash_indicator"] is False


def test_the_verdict_says_what_to_do_next():
    """撞针判定必须给出可执行的下一步和一条证伪路径。"""
    _baseline()
    note = CheckTipCrashByAmplitude().execute(_Ctx(FREE * 0.01), {}).data["note"]
    assert "退针" in note, "没说退针后振幅应恢复 —— 那是唯一的证伪方式"


# ════════════════════════════════════════════════════════════════════════════
# 2 · 「判断不了」绝不能被当成「没撞」——这一节是安全性的核心
# ════════════════════════════════════════════════════════════════════════════

def test_without_a_baseline_it_refuses_to_judge():
    d = CheckTipCrashByAmplitude().execute(_Ctx(0.0), {}).data
    assert d["status"] == "no_baseline"
    assert d["crash_indicator"] is None, "没有基线却给了 True/False —— 那是在猜"


def test_a_rig_without_qplus_is_unavailable_not_ok():
    d = CheckTipCrashByAmplitude().execute(
        _Ctx(0.0, names=STM_NAMES), {}).data
    assert d["status"] == "unavailable"
    assert d["crash_indicator"] is None
    assert "不是故障" in d["note"] or "没有" in d["note"]


def test_a_failed_read_is_unavailable_not_ok():
    _baseline()
    d = CheckTipCrashByAmplitude().execute(
        _Ctx(FREE, fail="TCP timeout"), {}).data
    assert d["status"] == "unavailable"
    assert d["crash_indicator"] is None, (
        "读失败被当成了「没撞」—— 这正是失败起来像成功的那种判据")


@pytest.mark.parametrize("status", ["unavailable", "no_baseline"])
def test_the_inconclusive_statuses_are_never_ok(status):
    """回归钉：任何人把 unavailable/no_baseline 归并进 ok 都要挂。"""
    assert status != "ok"


# ════════════════════════════════════════════════════════════════════════════
# 3 · 通道查找
# ════════════════════════════════════════════════════════════════════════════

def test_the_amplitude_channel_is_found_by_name():
    found = find_amplitude_signal(_Ctx(FREE))
    assert found is not None
    idx, name = found
    assert idx == 3 and "Amplitude" in name


def test_no_amplitude_channel_returns_none_not_a_guess():
    assert find_amplitude_signal(_Ctx(0.0, names=STM_NAMES)) is None


def test_an_explicit_index_skips_the_lookup():
    ctx = _Ctx(FREE)
    ReadTipOscillationAmplitude().execute(ctx, {"signal_index": 3})
    assert "Signals_NamesGet" not in ctx.calls, "给了显式索引还去查通道表"


# ════════════════════════════════════════════════════════════════════════════
# 4 · 基线的持久化 —— 静默 no-op 的那个坑
# ════════════════════════════════════════════════════════════════════════════

def test_the_baseline_keys_are_registered_in_the_profile_spec():
    """未登记的键会被 sanitize() 静默丢弃：写得进、读不到，判据永远停在
    no_baseline。这个仓库已经被同一形状咬过两次（SettingsStore.KNOWN_KEYS /
    override_store._ALL_FILES）。"""
    from mast.core.instrument_profile import _CONFIG_SPEC

    assert "qplus_amplitude_baseline" in _CONFIG_SPEC
    assert "qplus_amplitude_signal_index" in _CONFIG_SPEC


def test_the_baseline_actually_survives_a_write_and_read():
    """不看实现，看行为：写进去之后读得回来。"""
    from mast.core.instrument_profile import get_config

    _baseline(FREE)
    assert get_config("qplus_amplitude_baseline", None) == pytest.approx(FREE)


def test_setting_a_baseline_is_opt_in():
    """默认读一次不该动基线 —— 否则在针尖已接触时读一下就把坏值当成了基线。"""
    from mast.core.instrument_profile import get_config

    ReadTipOscillationAmplitude().execute(_Ctx(FREE), {})
    assert get_config("qplus_amplitude_baseline", None) is None


# ════════════════════════════════════════════════════════════════════════════
# 5 · 接线
# ════════════════════════════════════════════════════════════════════════════

def test_both_skills_are_exported_from_builtins():
    """技能写了但没挂上，是这个仓库最常见的缺陷形状。"""
    from mast.skills import builtins as B

    for n in ("ReadTipOscillationAmplitude", "CheckTipCrashByAmplitude"):
        assert hasattr(B, n) and n in B.__all__


def test_the_existing_current_based_crash_check_is_untouched():
    """新判据是**补充**，不是替代。CheckScanForCrash 的行为必须一个字不变。"""
    import numpy as np

    from mast.skills.builtins.scan_frame import CheckScanForCrash

    class _FrameCtx:
        def __init__(self, arr):
            self.arr = arr

        def safe_call(self, verb, *a, **k):
            return NanonisCallRecord(
                method=verb, args=a, kwargs={},
                return_value=("", b"", [1, "Z", 32, 32,
                                        self.arr.astype(np.float64), 1]),
                error="")

    rng = np.random.default_rng(3)
    good = CheckScanForCrash().execute(
        _FrameCtx(rng.normal(0, 1e-11, (32, 32))), {"channel_indices": "0"})
    assert good.data.get("status") == "ok"
    bad = CheckScanForCrash().execute(
        _FrameCtx(np.zeros((32, 32))), {"channel_indices": "0"})
    assert bad.data.get("crash_indicator") is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# 激励关闭时振幅没有撞针判据能力，必须返回 unavailable，而非 ok 或 crash。


@pytest.mark.parametrize("on,exc,label", [
    (0, 0.0, "输出关、激励 0(实机现场那一组)"),
    (1, 0.0, "输出开但激励幅度是 0 —— 音叉照样没被驱动"),
    (0, 0.2, "激励有幅度但输出关着"),
])
def test_a_driven_baseline_never_judges_an_undriven_reading(on, exc, label):
    """那个症状仍然被挡住:**驱动时取的基线**永远不拿去判**未驱动的读数**。

    这里故意给一个「塌得很彻底」的振幅(基线的 1%):它在驱动状态下必然判 crash,
    而拿未驱动读数比驱动基线必须一句撞针都不说 —— 现在答的是
    ``no_baseline``(请在当前状态下重取基线),不再是 ``unavailable``(通道没用):
    没用的是那个**比值**,不是通道。
    """
    ctx = _Ctx(1e-13, excitation_on=on, excitation_v=exc)
    _baseline(1e-11)
    res = CheckTipCrashByAmplitude().execute(ctx, {})
    assert res.data["status"] == "no_baseline", label
    assert res.data["crash_indicator"] is None
    assert res.data["excitation_on"] is False
    assert res.data["baseline_excited"] is True
    assert "set_baseline=True" in res.data["note"]


def test_an_unreadable_excitation_is_also_cannot_tell():
    """读不到激励状态 ⇒ 同样是「判不了」。

    不知道音叉有没有被驱动的时候,振幅这个数不代表任何东西。
    """
    class _NoPLL(_Ctx):
        def safe_call(self, verb, *a, **k):
            rec = super().safe_call(verb, *a, **k)
            if verb in ("PLL_OutOnOffGet", "PLL_ExcitationGet"):
                return NanonisCallRecord(method=verb, args=a, kwargs={},
                                         return_value=("", b"", []),
                                         error="not supported")
            return rec

    _baseline(1e-11)
    res = CheckTipCrashByAmplitude().execute(_NoPLL(1e-13), {})
    assert res.data["status"] == "unavailable"
    assert res.data["excitation_on"] is None
    assert "判不了" in res.data["note"]


def test_a_driven_fork_still_gets_a_real_verdict():
    """探针有效性:同一个振幅在**驱动着**的时候必须照常判 crash。

    没有这一条,一个「永远说 unavailable」的实现和这次修复长得一模一样。
    """
    _baseline(1e-11)
    res = CheckTipCrashByAmplitude().execute(
        _Ctx(1e-13, excitation_on=1, excitation_v=0.2), {})
    assert res.data["status"] == "crash"
    assert res.data["crash_indicator"] is True


# ══════════════════════════════════════════════════════════════════════
# 同态比较 —— 残余振幅归零 = 撞针
# ══════════════════════════════════════════════════════════════════════
#
# 激励关、STM 模式下:残余振幅静止在很小的值,进针途中被粗动步踢到
# 大得多的值;档案里的基线却是驱动时取的(也大得多)。通行规则是「残余振幅归零 = 撞针」。
# 误报来自拿未驱动读数比驱动基线;修法是让基线记住自己的激励状态,只比同态。

RESIDUAL = 37.3e-12


def _residual_baseline(amp=RESIDUAL):
    return ReadTipOscillationAmplitude().execute(
        _Ctx(amp, excitation_on=0, excitation_v=0.0), {"set_baseline": True})


def test_a_residual_baseline_judges_an_undriven_reading():
    """激励关时取的残余基线 + 激励关时的读数 ⇒ 照常判。归零判撞针,没归零判 ok。"""
    res = _residual_baseline()
    assert res.success and res.data["baseline_set"] is True
    assert res.data["baseline_excited"] is False

    off = dict(excitation_on=0, excitation_v=0.0)
    d = CheckTipCrashByAmplitude().execute(_Ctx(RESIDUAL * 0.03, **off), {}).data
    assert d["status"] == "crash" and d["crash_indicator"] is True
    assert d["mode"] == "residual"
    assert d["threshold_fraction"] == pytest.approx(0.25)
    assert "退针" in d["note"]

    d = CheckTipCrashByAmplitude().execute(_Ctx(RESIDUAL * 0.9, **off), {}).data
    assert d["status"] == "ok" and d["crash_indicator"] is False


def test_the_residual_threshold_is_stricter_than_the_driven_one():
    """残余基线的散布约 10%,阈值必须离它够远;驱动基线沿用 10%。"""
    from mast.skills.builtins.qplus_amplitude import crash_fraction_for

    assert crash_fraction_for(True) == pytest.approx(0.10)
    assert crash_fraction_for(False) == pytest.approx(0.25)
    # 0.3 × 基线:驱动时是 ok(高于 10%),残余时也是 ok(高于 25%)—— 两边都不误报。
    _residual_baseline()
    d = CheckTipCrashByAmplitude().execute(
        _Ctx(RESIDUAL * 0.3, excitation_on=0, excitation_v=0.0), {}).data
    assert d["status"] == "ok"


def test_a_residual_baseline_never_judges_a_driven_reading():
    """反方向同样不比:驱动读数 / 残余基线 ≈ 10,永远判不出撞针 —— 那是「漏报」。"""
    _residual_baseline()
    d = CheckTipCrashByAmplitude().execute(_Ctx(1e-14), {}).data
    assert d["status"] == "no_baseline"
    assert d["crash_indicator"] is None
    assert d["baseline_excited"] is False and d["excitation_on"] is True


def test_a_legacy_baseline_without_the_flag_counts_as_driven():
    """旧版本的基线没有记录激励状态 —— 按「驱动」处理。"""
    from mast.core import instrument_profile as ip

    prof = ip.get_profile()
    prof["qplus_amplitude_baseline"] = 3.46e-10
    ip.set_profile(prof)
    off = CheckTipCrashByAmplitude().execute(
        _Ctx(RESIDUAL, excitation_on=0, excitation_v=0.0), {}).data
    assert off["status"] == "no_baseline", "旧的驱动基线拿去比残余读数 —— 正是误报的来源"
    assert "旧基线" in off["note"]
    on = CheckTipCrashByAmplitude().execute(_Ctx(3.46e-12), {}).data
    assert on["status"] == "crash"


def test_the_baseline_is_a_median_of_several_reads():
    """一个离群读数不能把基线带偏 —— 基线是若干次读数的中位数。"""
    from mast.core.instrument_profile import get_config

    amps = [37e-12, 38e-12, 36e-12, 9e-10, 37e-12, 37.5e-12, 36.5e-12, 37e-12,
            38e-12, 36e-12]
    res = ReadTipOscillationAmplitude().execute(
        _Ctx(0.0, amps=amps, excitation_on=0, excitation_v=0.0), {"set_baseline": True})
    assert res.success
    assert get_config("qplus_amplitude_baseline", None) == pytest.approx(37e-12, rel=0.02)
    assert res.data["samples_n"] == ReadTipOscillationAmplitude._baseline_samples


def test_a_noise_floor_is_refused_as_a_baseline():
    """散布像噪声底(这里 ~50%)的读数不能当基线 —— 在它上面分不出「归零」与「正常」。"""
    from mast.core.instrument_profile import get_config

    amps = [2e-12, 8e-12, 3e-12, 9e-12, 1e-12, 7e-12, 4e-12, 10e-12, 2.5e-12, 6e-12]
    res = ReadTipOscillationAmplitude().execute(
        _Ctx(0.0, amps=amps, excitation_on=0, excitation_v=0.0), {"set_baseline": True})
    assert res.success is False
    assert res.data["baseline_set"] is False
    assert "噪声底" in res.error
    assert get_config("qplus_amplitude_baseline", None) is None, "被拒的基线不许落盘"


def test_an_unknown_excitation_state_refuses_the_baseline():
    """不知道取基线时音叉有没有被驱动 ⇒ 不记 —— 将来就没法判断该跟谁比。"""
    from mast.core.instrument_profile import get_config

    class _NoPLL(_Ctx):
        def safe_call(self, verb, *a, **k):
            if verb in ("PLL_OutOnOffGet", "PLL_ExcitationGet"):
                return NanonisCallRecord(method=verb, args=a, kwargs={},
                                         return_value=("", b"", []),
                                         error="not supported")
            return super().safe_call(verb, *a, **k)

    res = ReadTipOscillationAmplitude().execute(_NoPLL(RESIDUAL), {"set_baseline": True})
    assert res.success is False and res.data["baseline_set"] is False
    assert get_config("qplus_amplitude_baseline", None) is None


def test_the_new_baseline_keys_survive_the_profile_round_trip():
    """未登记的键会被 sanitize() 静默丢弃 —— 新的两个伴随键必须写得进、读得回。"""
    from mast.core import instrument_profile as ip
    from mast.core.instrument_profile import get_config

    _residual_baseline()
    assert get_config("qplus_amplitude_baseline_excited", None) == 0.0
    assert get_config("qplus_amplitude_baseline_sigma", None) is not None
    assert "qplus_amplitude_baseline_excited" in ip.ALL_KEYS
    assert "qplus_amplitude_baseline_sigma" in ip.ALL_KEYS
    # 换针要和基线一起清掉
    assert "qplus_amplitude_baseline_excited" in ip.TIP_BOUND_KEYS
    assert "qplus_amplitude_baseline_sigma" in ip.TIP_BOUND_KEYS


def test_a_rebaseline_in_the_other_state_replaces_the_flag():
    """先驱动取一次、再激励关取一次 ⇒ 旗标跟着最新那次走,不残留。"""
    from mast.core.instrument_profile import get_config

    _baseline(1e-10)
    assert get_config("qplus_amplitude_baseline_excited", None) == 1.0
    _residual_baseline()
    assert get_config("qplus_amplitude_baseline_excited", None) == 0.0
