"""AutoApproach 等待相的 qPlus 振幅看守。

规则(``docs/v2/stm_knowledge/20_qplus与振幅.md``):

    进针时振幅归 0 = 扎进去了 —— 针尖不导电时,这是唯一的证人。

而 AutoApproach 在模块跑着的时候**从不读振幅**:振幅证据只在进针结束之后附上
(``_approach_evidence``)。没有这层看守时,只能靠一个外部脚本定期读振幅、
自己停模块、自己退针。

这里的假机器是一台**贯穿状态**的机器(模块开关、啄木鸟 Z、振幅随状态变),同
``test_approach_progress_witness.py`` 的理由:单步 mock 抓不到「每一步单看都对、
合起来讲错了故事」的缺陷。

Run:
    .venv-v2-py313/Scripts/python.exe -m pytest \
        tests/v2/unit/skills/builtins/test_approach_qplus_watch.py -q
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports ──
import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[5] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

from dataclasses import dataclass, field
from typing import Any

import pytest

from mast.core.types import NanonisCallRecord
from mast.skills.builtins import qplus_amplitude as qa
from mast.skills.builtins.approach import AutoApproach

_QPLUS_NAMES = ["Current (A)", "Bias (V)", "Z (m)", "OC D1 Phase (deg)",
                "OC D1 Amplitude (m)", "OC M1 Freq. Shift (Hz)", "OC M1 Excitation (V)"]
_STM_NAMES = ["Current (A)", "Bias (V)", "Z (m)", "LI Demod 1 X (A)"]
_AMP_INDEX = 4

#: 静止残余振幅、进针途中被粗动步踢到的量级(合成值)。
REST = 37.3e-12
RING = 3.0e-9

_Z_TOP, _Z_BOTTOM = 169.5e-9, -157.0e-9
_WOODPECKER = (_Z_TOP, 60e-9, -60e-9, _Z_BOTTOM, 60e-9)


@dataclass
class QRig:
    """模块开关 + 啄木鸟 Z + 随状态变的 qPlus 振幅 + 退针。时间由轮询次数推进。"""

    #: 模块跑满这么多次状态轮询后自己停(到达设定点)。
    stop_after_polls: int = 40
    #: 从第 N 次轮询起针尖扎进表面:振幅归零(None = 不撞)。
    crash_at_poll: "int | None" = None
    #: 撞了以后电流:默认 0 —— 针尖不导电,电流判据瞎着,这正是看守存在的理由。
    current_after_crash_a: float = 0.0
    #: 模块自己停下(到设定点)时电流 = 设定点。
    setpoint_a: float = 5e-11
    names: list = field(default_factory=lambda: list(_QPLUS_NAMES))
    excitation_on: int = 0
    excitation_v: float = 0.0
    rest_amp: float = REST
    ring_amp: float = RING
    zero_amp: float = 0.4e-12
    #: 静止时的读数序列(取基线用);None = 恒为 ``rest_amp``。
    rest_script: "list[float] | None" = None
    #: 跑着时逐次读数的脚本(按「跑着时的第几次振幅读数」取);用完回落到默认规则。
    run_script: "list[float | None] | None" = None
    #: 从第 N 次轮询起激励被人关掉(振幅随之落到 ``rest_amp``)。
    excitation_off_at_poll: "int | None" = None
    #: 跑着时 Signals_ValGet 全部失败。
    amp_reads_fail: bool = False

    running: bool = False
    polls: int = 0
    z_index: int = 0
    run_amp_reads: int = 0
    withdrawn: bool = False
    stopped_at_poll: "int | None" = None
    started_at_call: "int | None" = None
    calls: list = field(default_factory=list)

    # -- 状态 -----------------------------------------------------------------
    @property
    def crashed(self) -> bool:
        return self.crash_at_poll is not None and self.polls >= self.crash_at_poll

    def _exc_on(self) -> int:
        if self.excitation_off_at_poll is not None and self.polls >= self.excitation_off_at_poll:
            return 0
        return self.excitation_on

    # -- Nanonis 面 -------------------------------------------------------------
    def safe_call(self, method: str, *args: Any, **kw: Any) -> NanonisCallRecord:
        self.calls.append((method, args))
        if method == "AutoApproach_Open":
            return self._ok([])
        if method == "AutoApproach_OnOffSet":
            on = bool(args[0]) if args else False
            if on and self.started_at_call is None:
                self.started_at_call = len(self.calls)
            if not on and self.running:
                self.stopped_at_poll = self.polls
            self.running = on
            return self._ok([])
        if method == "AutoApproach_OnOffGet":
            self.polls += 1
            if self.running and not self.crashed and self.polls >= self.stop_after_polls:
                self.running = False                  # 到设定点,模块自己停
            return self._ok([1 if self.running else 0])
        if method == "ZCtrl_ZPosGet":
            self.z_index += 1 if self.running else 0
            return self._ok([_WOODPECKER[self.z_index % len(_WOODPECKER)]])
        if method == "Current_Get":
            if self.crashed:
                return self._ok([self.current_after_crash_a])
            return self._ok([self.setpoint_a if not self.running and self.polls else 4e-14])
        if method == "ZCtrl_SetpntGet":
            return self._ok([self.setpoint_a])
        if method == "Signals_NamesGet":
            return self._ok([len(self.names), list(self.names)])
        if method == "Signals_ValGet":
            return self._amp()
        if method == "PLL_OutOnOffGet":
            return self._ok([self._exc_on()])
        if method == "PLL_ExcitationGet":
            return self._ok([self.excitation_v if self._exc_on() else 0.0])
        if method == "ZCtrl_Withdraw":
            self.withdrawn = True
            return self._ok([])
        return NanonisCallRecord(method=method, args=args, error=f"unmocked: {method}")

    def _amp(self) -> NanonisCallRecord:
        if not self.running:
            if self.rest_script:
                v = self.rest_script.pop(0) if len(self.rest_script) > 1 else self.rest_script[0]
                return self._ok([v])
            return self._ok([self.zero_amp if self.crashed else self.rest_amp])
        if self.amp_reads_fail:
            return NanonisCallRecord(method="Signals_ValGet", error="TCP timeout")
        self.run_amp_reads += 1
        if self.run_script and self.run_amp_reads <= len(self.run_script):
            v = self.run_script[self.run_amp_reads - 1]
            if v is None:
                return NanonisCallRecord(method="Signals_ValGet", error="TCP timeout")
            return self._ok([v])
        if self.crashed:
            return self._ok([self.zero_amp])
        if self.excitation_off_at_poll is not None and self.polls >= self.excitation_off_at_poll:
            return self._ok([self.rest_amp])
        return self._ok([self.ring_amp])

    @staticmethod
    def _ok(vals: list) -> NanonisCallRecord:
        return NanonisCallRecord(method="", args=(), return_value=("", b"", vals))

    # -- 技能面 -----------------------------------------------------------------
    def run(self, skill_name: str, params: dict):
        """参数组切换会走这条路;这台替身没有参数组 ⇒ 不切、不放回。"""
        raise RuntimeError(f"no skill {skill_name} on this fake rig")

    def count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from mast.core import instrument_profile as ip

    monkeypatch.setattr(qa, "_remembered_index", lambda: None)
    before = ip.get_profile()
    prof = dict(before)
    prof.pop("approach_qplus_watch", None)
    ip.set_profile(prof)
    yield
    ip.set_profile(before)


@pytest.fixture
def skill(monkeypatch) -> AutoApproach:
    """所有等待窗口都缩短、但**每一层机制都还在**的 AutoApproach。"""
    for name, val in (("_poll_interval_s", 0.0), ("_grace_s", 0.0),
                      ("_z_sample_every_s", 1e-9), ("_engage_interval_s", 0.0),
                      ("_engage_budget_s", 0.02), ("_crosstalk_every_s", 0.0),
                      ("_amp_sample_every_s", 1e-9), ("_amp_baseline_interval_s", 0.0)):
        monkeypatch.setattr(AutoApproach, name, val, raising=False)
    return AutoApproach()


def _run(skill: AutoApproach, rig: QRig, **params):
    return skill.run_composite(rig, dict(params))


# ── 它存在的理由:不导电的针尖扎进去,电流不响,振幅归零 ─────────────────────────

def test_a_crash_mid_approach_stops_the_module_and_withdraws(skill):
    """针尖在第 10 次轮询扎进表面、电流始终为 0:看守必须停模块并退针。

    去掉等待循环里的看守 → 模块一直跑到它自己的终点(第 40 次轮询之后才停),
    不退针 → 这条红。"""
    rig = QRig(crash_at_poll=10, stop_after_polls=40)
    # 预算给短:看守一旦失效,这条要快速红,而不是陪着一台永不停的假模块挂半小时。
    res = _run(skill, rig, wait_timeout_s=2.0)

    assert res.success is False
    assert rig.withdrawn is True, "看守触发了却没有退针"
    assert rig.stopped_at_poll is not None and rig.stopped_at_poll <= 13, rig.stopped_at_poll
    assert "归零" in (res.error or ""), res.error
    assert "ZCtrl_Withdraw" in (res.error or "")
    assert res.data["qplus_collapse"] is True
    watch = res.data["qplus_watch"]
    assert watch["armed"] is True
    assert watch["baseline"] == pytest.approx(REST)


def test_the_baseline_is_taken_at_rest_before_the_module_starts(skill):
    """基线必须在 OnOffSet(1) 之前取 —— 起跑后取到的是 3 nm 的起振,真归零永远够不着阈值。"""
    rig = QRig(crash_at_poll=10)
    _run(skill, rig, wait_timeout_s=2.0)

    first_val = next(i for i, (m, _) in enumerate(rig.calls, 1) if m == "Signals_ValGet")
    assert first_val < rig.started_at_call, "第一次读振幅发生在起跑之后"
    assert rig.count("Signals_ValGet") >= AutoApproach._amp_baseline_samples


# ── 不能有的误报 ───────────────────────────────────────────────────────────────

def test_ring_up_during_a_normal_approach_is_not_a_crash(skill):
    """粗动步把振幅踢到 80 倍 —— 方向向上,结构上不可能造成假撞针。"""
    rig = QRig(stop_after_polls=40)
    res = _run(skill, rig)

    assert res.success is True, res.error
    assert rig.withdrawn is False
    assert res.data["qplus_collapse"] is False
    wp = res.data["wait_progress"]
    assert wp["amp_reads_n"] > 0
    assert wp["amp_max"] == pytest.approx(RING)
    assert wp["amp_median"] == pytest.approx(RING)


def test_a_single_dip_is_not_enough_in_soft_range(skill):
    """一次掉到 40%(在 25%–50% 之间)不判 —— 「连续 2 次 < 50%」才判。"""
    rig = QRig(stop_after_polls=40, run_script=[RING, 0.4 * REST, RING, RING])
    res = _run(skill, rig)

    assert res.success is True, res.error
    assert rig.withdrawn is False


def test_two_consecutive_low_reads_trip_the_soft_rule(skill):
    rig = QRig(stop_after_polls=40, run_script=[RING, 0.4 * REST, 0.45 * REST])
    res = _run(skill, rig, wait_timeout_s=2.0)

    assert res.success is False
    assert rig.withdrawn is True
    assert res.data["qplus_collapse"] is True
    assert "连续 2 次" in res.error


def test_unreadable_amplitude_reads_never_trip_the_watch(skill):
    """读不到 ≠ 归零。读不到的拍单独计数,进针照常走到它自己的终点。"""
    rig = QRig(stop_after_polls=30, amp_reads_fail=True)
    res = _run(skill, rig)

    assert res.success is True, res.error
    assert rig.withdrawn is False
    wp = res.data["wait_progress"]
    assert wp["amp_unreadable_n"] > 0 and wp["amp_reads_n"] == 0


def test_an_unreadable_read_between_two_lows_does_not_reset_the_run(skill):
    """读不到的一拍当作「没观测」:不清零连续段,也不算一次「低」。"""
    rig = QRig(stop_after_polls=40, run_script=[RING, 0.4 * REST, None, 0.4 * REST])
    res = _run(skill, rig, wait_timeout_s=2.0)

    assert res.success is False and rig.withdrawn is True


# ── 驱动模式:只用「连续 2 次 < 10%」 ─────────────────────────────────────────

def test_a_driven_fork_uses_the_consecutive_ten_percent_rule(skill):
    driven = dict(excitation_on=1, excitation_v=0.2, rest_amp=346e-12, ring_amp=346e-12)
    one_dip = QRig(stop_after_polls=40, run_script=[346e-12, 5e-12, 346e-12], **driven)
    assert _run(skill, one_dip).success is True
    assert one_dip.withdrawn is False

    crash = QRig(stop_after_polls=40, crash_at_poll=8, **driven)
    res = _run(AutoApproach(), crash, wait_timeout_s=2.0)
    assert res.success is False and crash.withdrawn is True
    assert res.data["qplus_watch"]["excited"] is True
    assert res.data["qplus_watch"]["hard_frac"] is None


def test_switching_the_excitation_off_mid_approach_disarms_instead_of_withdrawing(skill):
    """有人在进针途中关掉激励:驱动振幅落到残余(< 10%),那不是撞针 —— 解除看守,不退针。"""
    rig = QRig(stop_after_polls=40, excitation_on=1, excitation_v=0.2,
               rest_amp=400e-12, ring_amp=400e-12, excitation_off_at_poll=10)
    rig_rest = 30e-12

    # 关激励之后振幅落到残余水平
    orig = rig._amp

    def _amp():
        if rig.running and rig.polls >= 10:
            return rig._ok([rig_rest])
        return orig()

    rig._amp = _amp  # type: ignore[method-assign]
    res = _run(skill, rig)

    assert rig.withdrawn is False
    assert res.success is True, res.error
    assert res.data["wait_progress"]["amp_watch_disarmed"]


# ── 不看守的时候要说清为什么 ───────────────────────────────────────────────────

def test_no_amplitude_channel_means_no_watch_and_says_why(skill):
    rig = QRig(names=list(_STM_NAMES))
    res = _run(skill, rig)

    assert res.success is True, res.error
    watch = res.data["qplus_watch"]
    assert watch["armed"] is False and watch["why"]
    assert rig.count("Signals_ValGet") == 0


def test_the_profile_switch_turns_the_watch_off(skill):
    from mast.core import instrument_profile as ip

    prof = ip.get_profile()
    prof["approach_qplus_watch"] = "off"
    ip.set_profile(prof)
    assert ip.approach_qplus_watch_enabled() is False

    # 撞了以后这台替身的模块不会自己停 —— 没有看守,只能靠预算收场。
    rig = QRig(crash_at_poll=10, stop_after_polls=25)
    res = _run(skill, rig, wait_timeout_s=0.05)

    assert res.data["qplus_watch"]["armed"] is False
    assert "approach_qplus_watch=off" in res.data["qplus_watch"]["why"]
    assert rig.withdrawn is False, "关掉的看守仍然动了手"
    assert rig.count("Signals_ValGet") == 0


def test_a_noise_floor_at_rest_refuses_to_arm(skill):
    noisy = [2e-12, 8e-12, 3e-12, 9e-12, 1e-12, 7e-12, 4e-12, 10e-12, 2.5e-12, 6e-12]
    rig = QRig(rest_script=noisy, stop_after_polls=20)
    res = _run(skill, rig)

    watch = res.data["qplus_watch"]
    assert watch["armed"] is False
    assert "噪声底" in watch["why"]
    assert rig.withdrawn is False


def test_an_unreadable_excitation_state_refuses_to_arm(skill):
    rig = QRig(stop_after_polls=20)
    orig = rig.safe_call

    def _no_pll(method, *a, **k):
        if method in ("PLL_OutOnOffGet", "PLL_ExcitationGet"):
            rig.calls.append((method, a))
            return NanonisCallRecord(method=method, error="not supported")
        return orig(method, *a, **k)

    rig.safe_call = _no_pll  # type: ignore[method-assign]
    res = _run(skill, rig)

    assert res.data["qplus_watch"]["armed"] is False
    assert res.success is True, res.error


# ── 基本盘不动 ─────────────────────────────────────────────────────────────────

def test_the_watch_never_removes_an_existing_stop(skill):
    """看守是加法:预算到点照样停模块、照样报「仍在推进」。"""
    rig = QRig(stop_after_polls=10**9)
    res = _run(skill, rig, wait_timeout_s=0.05)

    assert res.success is False
    assert "从当前粗动位置继续" in res.error, res.error
    assert rig.stopped_at_poll is not None
    assert rig.withdrawn is False


def test_the_plan_still_has_four_phases():
    """看守不新增阶段 —— 基线在 start 阶段里取,图的形状不变。"""
    assert [s.step_id for s in AutoApproach().plan({})] == [
        "open_module", "start_approach", "wait_complete", "verify_status"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
