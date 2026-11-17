# -*- coding: utf-8 -*-
"""``DiagnoseTipAdsorbateHopping`` against a simulated junction (no instrument, no waiting)."""
from __future__ import annotations

import numpy as np
import pytest

from mast.core.types import NanonisCallRecord, SafetyLevel, SkillResult
from mast.skills.builtins import tip_hopping_diag as M
from mast.skills.builtins.tip_hopping_diag import (
    DiagnoseTipAdsorbateHopping, classify_hopping, count_jumps, parse_conditions)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(M, "_sleep", lambda s: None)


class SimCtx:
    """Fake Nanonis. ``jump_fn(bias, setpoint, k)`` returns the Z offset (m) at the k-th sample."""

    def __init__(self, bias=2.0, setpoint=100e-12, jump_fn=None, feedback=1.0, fail_on=None,
                 current=100e-12, restore_ok=True):
        self.bias, self.setpoint, self.k = bias, setpoint, 0
        self.jump_fn = jump_fn or (lambda v, i, k: 0.0)
        self.feedback, self.fail_on, self.current = feedback, fail_on, current
        self.restore_ok = restore_ok
        self.calls: list = []

    def check_abort(self):
        return False

    def safe_call(self, method, *args, allow_on_abort=False, **kw):
        self.calls.append((method, args, allow_on_abort))
        if self.fail_on and method == self.fail_on[0]:
            self.fail_on[1] -= 1
            if self.fail_on[1] <= 0:
                raise RuntimeError("boom in " + method)
        body = [0.0]
        if method == "ZCtrl_OnOffGet":
            body = [self.feedback]
        elif method == "Bias_Get":
            body = [self.bias]
        elif method == "ZCtrl_SetpntGet":
            body = [self.setpoint]
        elif method == "Bias_Set":
            if self.restore_ok or not allow_on_abort:
                self.bias = args[0]
            self.k = 0
        elif method == "ZCtrl_SetpntSet":
            self.setpoint = args[0]
        elif method == "ZCtrl_ZPosGet":
            body = [1e-9 + self.jump_fn(self.bias, self.setpoint, self.k)]
            self.k += 1
        elif method == "Current_Get":
            body = [self.current]
        return NanonisCallRecord(method=method, args=args, return_value=("", b"", body), error="")

    def writes(self):
        return [(m, a) for m, a, _ in self.calls if m in ("Bias_Set", "ZCtrl_SetpntSet")]


def telegraph(rate_fn, seed=0, amp=150e-12):
    """Z offset toggling between 0 and ``amp`` with a condition-dependent probability per sample."""
    rng = np.random.default_rng(seed)
    state = {"s": 0.0}

    def f(v, i, k):
        if rng.random() < rate_fn(v, i):
            state["s"] = amp - state["s"]
        return state["s"]
    return f


PARAMS = {"sample_s": 40.0, "sample_hz": 2.0, "settle_s": 0.0}
FIELD = "2.0@30,1.5@100,2.0@100,2.5@100"


def _run(ctx, **kw):
    p = dict(PARAMS, conditions=FIELD)
    p.update(kw)
    return DiagnoseTipAdsorbateHopping().execute(ctx, p)


def test_metadata_and_registration():
    m = DiagnoseTipAdsorbateHopping().metadata()
    assert m.safety_level == SafetyLevel.CONFIRM and m.name == "DiagnoseTipAdsorbateHopping"
    import mast.skills.builtins as B
    assert B.DiagnoseTipAdsorbateHopping is DiagnoseTipAdsorbateHopping
    assert "DiagnoseTipAdsorbateHopping" in B.__all__


def test_helpers():
    assert parse_conditions("2.0@30, 1.5@100pA") == [(2.0, 30e-12), (1.5, 100e-12)]
    with pytest.raises(ValueError):
        parse_conditions("2.0")
    r = count_jumps([0, 0, 100, 100, 0, 0, 50, 50, 60], 40)   # up, down, up = three events
    assert r["n_jumps"] == 3 and r["max_jump"] == 100 and r["z_range"] == 100
    assert count_jumps([0, 100, 200, 300], 40)["n_jumps"] == 1   # consecutive steps = one event
    assert count_jumps([0.0], 40)["n_jumps"] == 0


def test_field_numbers_classify_as_hopping():
    conds = [dict(bias_v=2.0, setpoint_a=30e-12, n_jumps=0), dict(bias_v=1.5, setpoint_a=100e-12, n_jumps=1),
             dict(bias_v=2.0, setpoint_a=100e-12, n_jumps=7), dict(bias_v=2.5, setpoint_a=100e-12, n_jumps=15)]
    assert classify_hopping(conds)[0] == "adsorbate_hopping"


def test_condition_dependent_telegraph_is_adsorbate_hopping():
    def rate(v, i):                       # grows with |V| and with setpoint
        return max(0.0, abs(v) - 1.4) * (i / 100e-12) * 0.18
    ctx = SimCtx(jump_fn=telegraph(rate, seed=3))
    r = _run(ctx)
    assert r.success, r.error
    assert r.data["verdict"] == "adsorbate_hopping", r.data["conditions"]
    n = [c["n_jumps"] for c in r.data["conditions"]]
    assert n[3] > n[2] > n[0]             # 2.5 V > 2.0 V/100 pA > 2.0 V/30 pA
    assert "Au(111)" in r.data["remedy"] and "MakeAtomicResolutionTip" in r.data["remedy"]
    assert r.data["restore"]["restored"] and ctx.bias == 2.0 and ctx.setpoint == 100e-12


def test_condition_independent_jumps_are_noisy_environment():
    ctx = SimCtx(jump_fn=telegraph(lambda v, i: 0.12, seed=5))
    r = _run(ctx)
    assert r.data["verdict"] == "noisy_environment", r.data["conditions"]


def test_flat_trace_is_stable():
    ctx = SimCtx(jump_fn=lambda v, i, k: 2e-12 * np.sin(k))
    r = _run(ctx)
    assert r.success and r.data["verdict"] == "stable"
    assert all(c["n_jumps"] == 0 for c in r.data["conditions"])


def test_unclear_dependence_is_inconclusive():
    conds = [dict(bias_v=2.0, setpoint_a=30e-12, n_jumps=4), dict(bias_v=2.5, setpoint_a=100e-12, n_jumps=1),
             dict(bias_v=1.5, setpoint_a=100e-12, n_jumps=9)]
    assert classify_hopping(conds)[0] == "inconclusive"


def test_opposite_polarity_rejected_and_nothing_written():
    ctx = SimCtx(bias=2.0)
    r = DiagnoseTipAdsorbateHopping().execute(ctx, dict(PARAMS, conditions="2.0@30,-2.0@30"))
    assert r.success is False and ("反号" in r.error or "opposite" in r.error)
    assert ctx.writes() == []             # rejected before touching anything


def test_near_zero_condition_and_zero_start_rejected():
    ctx = SimCtx(bias=2.0)
    assert not DiagnoseTipAdsorbateHopping().execute(ctx, dict(PARAMS, conditions="0.01@30")).success
    assert ctx.writes() == []
    assert not DiagnoseTipAdsorbateHopping().execute(SimCtx(bias=0.01), dict(PARAMS)).success


def test_feedback_off_refused():
    assert not _run(SimCtx(feedback=0.0)).success


def test_setpoint_cap():
    ctx = SimCtx()
    r = DiagnoseTipAdsorbateHopping().execute(ctx, dict(PARAMS, conditions="2.0@5000"))
    assert not r.success and ctx.writes() == []


def test_restore_called_on_exception():
    ctx = SimCtx(fail_on=["ZCtrl_ZPosGet", 30])     # blow up mid-sampling of the 1st condition
    r = _run(ctx)
    assert r.success is False and "boom" in r.error
    assert ctx.bias == 2.0 and ctx.setpoint == 100e-12
    last_writes = [c for c in ctx.calls if c[0] in ("Bias_Set", "ZCtrl_SetpntSet")][-2:]
    assert all(c[2] for c in last_writes)           # restore uses allow_on_abort
    assert r.data["restore"]["restored"] is True


def test_restore_failure_is_reported_not_hidden():
    ctx = SimCtx(restore_ok=False)
    r = _run(ctx)
    assert r.data["restore"]["restored"] is False and "restore_warning" in r.data


def test_overcurrent_aborts_and_restores():
    ctx = SimCtx(current=9e-9)
    r = _run(ctx)
    assert not r.success and ctx.bias == 2.0 and ctx.setpoint == 100e-12


def test_never_crosses_zero_in_any_write():
    ctx = SimCtx(jump_fn=telegraph(lambda v, i: 0.1, seed=1))
    _run(ctx)
    assert all(a[0] > 0 for m, a in ctx.writes() if m == "Bias_Set")


def test_default_grid_is_same_polarity_around_working_point():
    ctx = SimCtx(bias=-1.5, setpoint=50e-12)
    r = DiagnoseTipAdsorbateHopping().execute(ctx, dict(PARAMS, sample_s=4.0))
    assert r.success
    assert all(c["bias_v"] < 0 for c in r.data["conditions"]) and len(r.data["conditions"]) >= 3


def test_fast_noise_reuses_acquire_osci_trace(monkeypatch):
    called = []

    class FakeOsci:
        def execute(self, context, params):
            called.append(params)
            return SkillResult(skill_name="AcquireOsciTrace", success=True,
                               data={"samples": [0.0, 1e-12, -1e-12, 0.0], "fs_hz": 2e4})
    monkeypatch.setattr("mast.skills.builtins.acquire_osci_trace.AcquireOsciTrace", FakeOsci)
    r = _run(SimCtx(), include_fast_noise=True)
    assert r.success and len(called) == 4
    fn = r.data["conditions"][0]["fast_noise"]
    assert fn["ok"] and fn["rms"] > 0
