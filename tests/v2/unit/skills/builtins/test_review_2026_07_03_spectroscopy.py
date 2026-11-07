"""Regression pins for the 2026-07-03 review — spectroscopy / sweep cluster.

Covers:
  * BiasSpectr/ZSpectr.Start data is parsed as rows=CHANNELS, cols=points (was
    transposed → garbage spectra on real hardware).
  * AcquireSTS no longer zeroes the operator's Z offset (no PropsSet).
  * ConfigureSTS pins Z-controller hold + reset bias via AdvPropsSet.
  * AcquireBiasSweep turns feedback OFF during the sweep (Z-Ctrl=1) + restores
    the signal (Reset=1), and no longer wipes the settling times.
  * SetSTSMLSVals validates equal-length segment arrays + the bias bound (str
    params bypass the automatic checks).

Run from repo root:
    .venv-v2-py313/Scripts/python.exe -m pytest \
        tests/v2/unit/skills/builtins/test_review_2026_07_03_spectroscopy.py -q
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_MASTV2_ROOT = str(Path(__file__).resolve().parents[5] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

from mast.core.types import NanonisCallRecord
from mast.skills.builtins.spectroscopy import AcquireSTS, ConfigureSTS, SetSTSMLSVals
from mast.skills.builtins.sweep import AcquireBiasSweep


@dataclass
class FakeCtx:
    canned: dict[str, Any] = field(default_factory=dict)
    calls: list = field(default_factory=list)
    recv_timeouts: list = field(default_factory=list)

    def safe_call(self, method, *args, role="main",
                  recv_timeout_s=None):
        # 跟上真 safe_call 的签名（2026-09-09 加的长扫掠 recv 预算），
        # 并记下来 —— 只吞掉的话「传没传预算」永远没人看得见。
        self.calls.append((method, args))
        self.recv_timeouts.append((method, recv_timeout_s))
        entry = self.canned.get(method, {})
        return NanonisCallRecord(method=method, args=args,
                                 return_value=entry.get("return_value"),
                                 error=entry.get("error", ""))


# ── row=channel parse ────────────────────────────────────────────────────────
def test_acquire_sts_rows_are_channels():
    # 2 channels × 3 points, channel-major (row 0 = Bias, row 1 = Current).
    variables = [16, 2, ["Bias (V)", "Current (A)"], 2, 3,
                 [0.0, 0.5, 1.0, 1e-9, 2e-9, 3e-9], 0, []]
    ctx = FakeCtx(canned={"BiasSpectr_Start": {"return_value": ("", b"", variables)}})
    res = AcquireSTS().execute(ctx, {})
    assert res.success
    assert res.data["num_points"] == 3
    assert res.data["voltage"] == [0.0, 0.5, 1.0]
    assert res.data["current"] == [1e-9, 2e-9, 3e-9]


def test_acquire_sts_does_not_zero_z_offset():
    ctx = FakeCtx(canned={"BiasSpectr_Start": {"return_value": ("", b"", [])}})
    AcquireSTS().execute(ctx, {})
    # No PropsSet at all → the operator's configured Z offset is untouched.
    assert not any(m == "BiasSpectr_PropsSet" for m, _ in ctx.calls)


def test_configure_sts_sets_advprops_hold_and_reset():
    ctx = FakeCtx()
    ConfigureSTS().execute(ctx, {"start_v": -1.0, "end_v": 1.0, "num_points": 100})
    adv = [args for m, args in ctx.calls if m == "BiasSpectr_AdvPropsSet"]
    assert adv, "ConfigureSTS must set advanced props"
    # (reset_bias=1 On, z_ctrl_hold=1 On, record_final_z=0, lockin_run=0)
    assert adv[0][0] == 1 and adv[0][1] == 1


# ── num_sweeps: 保持操作员的值，别写死成 1────────────────────
# PropsSet 的第 2 个实参是「扫掠数」。BiasSpectr_PropsGet.Variables =
# [Save all, Number of sweeps, Backward sweep, Number of points, …]。
def _props_get_with_sweeps(n):
    return {"BiasSpectr_PropsGet": {"return_value": ("", b"", [1, n, 1, 100])}}


def test_configure_sts_preserves_operator_sweeps_when_not_given():
    # 操作员在 GUI 里设了 4 次平均；没显式传 num_sweeps ⇒ 必须原样保持 4，
    # 不能像以前那样打回 1。
    ctx = FakeCtx(canned=_props_get_with_sweeps(4))
    res = ConfigureSTS().execute(ctx, {"start_v": -1.0, "end_v": 1.0,
                                       "num_points": 100})
    props = [args for m, args in ctx.calls if m == "BiasSpectr_PropsSet"]
    assert props, "ConfigureSTS must set props"
    assert props[0][1] == 4, "sweep count must be PRESERVED, not forced to 1"
    assert res.data["num_sweeps"] == 4
    assert res.data["num_sweeps_source"] == "preserved"


def test_configure_sts_num_sweeps_override():
    # 显式传 ⇒ 用它；此时不需要去读当前值。
    ctx = FakeCtx(canned=_props_get_with_sweeps(4))
    res = ConfigureSTS().execute(ctx, {"start_v": -1.0, "end_v": 1.0,
                                       "num_points": 100, "num_sweeps": 8})
    props = [args for m, args in ctx.calls if m == "BiasSpectr_PropsSet"]
    assert props[0][1] == 8
    assert res.data["num_sweeps_source"] == "override"
    assert not any(m == "BiasSpectr_PropsGet" for m, _ in ctx.calls)


def test_configure_sts_sweeps_fallback_when_unreadable():
    # 读不到当前扫掠数 ⇒ 退回 1，并如实说「没确认保持住」。
    ctx = FakeCtx()  # BiasSpectr_PropsGet 回 None
    res = ConfigureSTS().execute(ctx, {"start_v": -1.0, "end_v": 1.0,
                                       "num_points": 100})
    props = [args for m, args in ctx.calls if m == "BiasSpectr_PropsSet"]
    assert props[0][1] == 1
    assert res.data["num_sweeps_source"] == "fallback_unreadable"


# ── 测谱前把偏压/setpoint 从成像值分步 ramp 到测谱值、测谱后恢复 ────────
# BiasSpectr.Start 的可解析 Variables（2 通道 × 3 点），让这条谱「成功」。
_STS_VARS = [16, 2, ["Bias (V)", "Current (A)"], 2, 3,
             [0.0, 0.5, 1.0, 1e-9, 2e-9, 3e-9], 0, []]


def _ramp_ctx(v0=0.5, i0=1e-9, start_v=-0.5):
    # 成像偏压 v0 / setpoint i0；ConfigureSTS 配的起始偏压 start_v。
    return FakeCtx(canned={
        "Bias_Get": {"return_value": ("", b"", [v0])},
        "ZCtrl_SetpntGet": {"return_value": ("", b"", [i0])},
        "BiasSpectr_LimitsGet": {"return_value": ("", b"", [start_v, 0.5])},
        "BiasSpectr_Start": {"return_value": ("", b"", _STS_VARS)},
    })


def test_acquire_sts_no_ramp_by_default():
    # 默认不碰偏压/setpoint（回归：现有行为完全不变）。
    ctx = _ramp_ctx()
    res = AcquireSTS().execute(ctx, {})
    assert res.success
    assert not any(m in ("Bias_Get", "Bias_Set", "ZCtrl_SetpntSet")
                   for m, _ in ctx.calls)
    assert res.data["working_point_ramped"] is False


def test_acquire_sts_ramp_before_steps_bias_to_sweep_start():
    # ramp_before + slew ⇒ 偏压分步（不是一步）走到 ConfigureSTS 的起始偏压。
    ctx = _ramp_ctx(v0=0.5, start_v=-0.5)
    res = AcquireSTS().execute(ctx, {"ramp_before": True,
                                     "ramp_slew_rate_v_per_s": 2.0})
    assert res.success
    sets = [a[0] for m, a in ctx.calls if m == "Bias_Set"]
    assert len(sets) >= 2, "应分步，不是一步到位"
    assert abs(sets[-1] - (-0.5)) < 1e-9, "最后一步应到测谱起始偏压"
    assert res.data["working_point_ramped"] is True
    assert abs(res.data["imaging_bias_v"] - 0.5) < 1e-9, "应快照到成像偏压供还原"


def test_acquire_sts_ramp_before_refuses_when_bias_unreadable():
    # 读不到当前偏压 ⇒ 拒绝 ramp、拒绝起扫（不从臆断的 0 V 起步）。
    ctx = _ramp_ctx()
    ctx.canned["Bias_Get"] = {"return_value": None}
    res = AcquireSTS().execute(ctx, {"ramp_before": True,
                                     "ramp_slew_rate_v_per_s": 2.0})
    assert not res.success
    assert not any(m == "BiasSpectr_Start" for m, _ in ctx.calls), \
        "ramp 失败绝不该起扫"


def test_acquire_sts_slew_none_is_instant():
    # 不给 slew ⇒ 与 SetBias 默认一致：瞬时一步到位。
    ctx = _ramp_ctx(v0=0.5, start_v=-0.5)
    res = AcquireSTS().execute(ctx, {"ramp_before": True})
    assert res.success
    sets = [a[0] for m, a in ctx.calls if m == "Bias_Set"]
    assert sets == [-0.5], "无 slew 应一步到位"


def test_acquire_sts_ramps_setpoint_when_given_and_restores_both():
    # 给了 spectro_setpoint_a ⇒ setpoint 一起 ramp；restore_after ⇒ 偏压+setpoint
    # 都还原到成像快照值。
    ctx = _ramp_ctx(v0=0.5, i0=1e-9, start_v=-0.5)
    res = AcquireSTS().execute(ctx, {"ramp_before": True, "restore_after": True,
                                     "spectro_setpoint_a": 5e-10,
                                     "ramp_slew_rate_v_per_s": 2.0})
    assert res.success
    setp = [a[0] for m, a in ctx.calls if m == "ZCtrl_SetpntSet"]
    assert setp, "给了 spectro_setpoint_a 就该动 setpoint"
    assert abs(setp[-1] - 1e-9) < 1e-18, "最后应恢复到成像 setpoint"
    biassets = [a[0] for m, a in ctx.calls if m == "Bias_Set"]
    assert abs(biassets[-1] - 0.5) < 1e-9, "偏压最后应恢复到成像值"
    assert res.data["working_point_restored"] is True


def test_acquire_sts_leaves_setpoint_alone_when_not_given():
    # 没给 spectro_setpoint_a ⇒ 全程不碰 setpoint（连 restore 也不碰）。
    ctx = _ramp_ctx(v0=0.5, start_v=-0.5)
    res = AcquireSTS().execute(ctx, {"ramp_before": True, "restore_after": True,
                                     "ramp_slew_rate_v_per_s": 2.0})
    assert res.success
    assert not any(m == "ZCtrl_SetpntSet" for m, _ in ctx.calls), \
        "没给 setpoint 就不该动它"


# ── sweep feedback + reset ───────────────────────────────────────────────────
def test_acquire_bias_sweep_holds_feedback_and_resets():
    ctx = FakeCtx()
    AcquireBiasSweep().execute(ctx, {})
    starts = [args for m, args in ctx.calls if m == "GenSwp_Start"]
    assert starts, "sweep must start"
    # GenSwp_Start(Get_data, Direction, Basename, Reset_signal, Z-Ctrl)
    get_data, direction, basename, reset_signal, zctrl = starts[0]
    assert reset_signal == 1, "signal must be restored after sweep"
    assert zctrl == 1, "feedback must be turned OFF during the sweep"


def test_acquire_bias_sweep_does_not_wipe_settling():
    ctx = FakeCtx()
    AcquireBiasSweep().execute(ctx, {})
    assert not any(m == "GenSwp_PropsSet" for m, _ in ctx.calls)


# ── MLS validation ───────────────────────────────────────────────────────────
def test_mls_rejects_unequal_length_arrays():
    ctx = FakeCtx()
    res = SetSTSMLSVals().execute(ctx, {
        "bias_start_v": "-1.0, 0.5",
        "bias_end_v": "1.0",            # only 1 element ≠ 2
        "initial_settling_s": "0.1, 0.1",
        "settling_s": "0.1, 0.1",
        "integration_s": "0.1, 0.1",
        "steps": "100, 100",
        "lockin_run": "0, 0",
    })
    assert not res.success
    assert "same" in res.error or "elements" in res.error
    assert not any(m == "BiasSpectr_MLSValsSet" for m, _ in ctx.calls)


def test_mls_rejects_out_of_bound_bias():
    ctx = FakeCtx()
    res = SetSTSMLSVals().execute(ctx, {
        "bias_start_v": "-50.0, 0.5",   # -50 V is outside ±10 V
        "bias_end_v": "1.0, 2.0",
        "initial_settling_s": "0.1, 0.1",
        "settling_s": "0.1, 0.1",
        "integration_s": "0.1, 0.1",
        "steps": "100, 100",
        "lockin_run": "0, 0",
    })
    assert not res.success
    assert "bias" in res.error.lower()
    assert not any(m == "BiasSpectr_MLSValsSet" for m, _ in ctx.calls)


def test_mls_accepts_valid_config():
    ctx = FakeCtx(canned={"BiasSpectr_MLSValsSet": {"return_value": ("", b"", [])}})
    res = SetSTSMLSVals().execute(ctx, {
        "bias_start_v": "-1.0, 0.5",
        "bias_end_v": "1.0, 2.0",
        "initial_settling_s": "0.1, 0.1",
        "settling_s": "0.1, 0.1",
        "integration_s": "0.1, 0.1",
        "steps": "100, 100",
        "lockin_run": "0, 0",
    })
    assert res.success
    assert res.data["num_segments"] == 2
