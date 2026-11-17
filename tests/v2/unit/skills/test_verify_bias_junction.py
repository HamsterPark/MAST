"""VerifyBiasAtJunction: does the sample bias really reach the tunnel junction?

Failure mode: a bad external bias cable contact leaves the junction at a few
mV. With feedback on the current reads about -97..-100 pA at +2 V / 100 pA
setpoint (opposite sign), does not follow the bias, and Z does not respond to
bias steps (a healthy junction moves the tip toward the sample).

The fake below models both junctions. Z sign: Z DEcreases = toward the sample
(z_extend_sign = -1 on this instrument).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4] / "MASTv2"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
for _m in [m for m in list(sys.modules) if m == "mast" or m.startswith("mast.")]:
    if "MASTv2" not in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/"):
        del sys.modules[_m]

import pytest  # noqa: E402

from mast.skills.builtins import verify_bias_junction as VB  # noqa: E402
from mast.skills.builtins import tip as tip_mod  # noqa: E402


class _Rec:
    def __init__(self, value=None, error=""):
        self.error = error
        self.return_value = ("", b"", [value]) if value is not None else None


class Ctx:
    """Fake Nanonis. ``healthy``: Z follows the bias; else dead junction."""

    Z0 = 5e-9
    #: healthy response: Z moves toward the sample (down) by this per volt of
    #: |V| reduction (model constant: 0.8 V -> 175 pm).
    PM_PER_V = 175e-12 / 0.8

    def __init__(self, *, healthy=True, bias=2.0, setpoint=100e-12,
                 current=None, nanz=False, nancur=False, feedback=1,
                 bias_get_error="", bias_set_error="", drift_m_per_call=0.0,
                 fail_restore=False, abort_at=None):
        self.healthy = healthy
        self.bias = bias
        self.set_bias = bias
        self.ref_bias = bias
        self.setpoint = setpoint
        self.current_override = current
        self.nanz, self.nancur = nanz, nancur
        self.feedback = feedback
        self.bias_get_error = bias_get_error
        self.bias_set_error = bias_set_error
        self.drift = drift_m_per_call
        self.fail_restore = fail_restore
        self.abort_at = abort_at
        self._n_abort = 0
        self._ncalls = 0
        self.calls: list[tuple] = []

    # -- model --
    def _z(self):
        self._ncalls += 1
        drift = self.drift * self._ncalls
        if self.healthy:
            # |V| below the reference -> tip toward the sample -> Z decreases
            dz = -(abs(self.ref_bias) - abs(self.set_bias)) * self.PM_PER_V
            return self.Z0 + dz + drift
        return self.Z0 + drift

    def _i(self):
        if self.current_override is not None:
            return self.current_override
        if self.healthy:
            return self.setpoint if self.bias > 0 else -self.setpoint
        # dead junction: opposite sign, ignores the bias
        return -0.97 * self.setpoint

    def safe_call(self, method, *args, role="main", allow_on_abort=False):
        self.calls.append((method, args, allow_on_abort))
        if method == "ZCtrl_OnOffGet":
            return _Rec(float(self.feedback))
        if method == "Bias_Get":
            if self.bias_get_error:
                return _Rec(error=self.bias_get_error)
            if self.fail_restore and self.set_bias != self.ref_bias:
                return _Rec(self.set_bias)
            return _Rec(self.set_bias)
        if method == "Bias_Set":
            if self.bias_set_error:
                return _Rec(error=self.bias_set_error)
            if self.fail_restore and float(args[0]) == self.ref_bias:
                return _Rec(0.0)        # command "succeeds", bias stays low
            self.set_bias = float(args[0])
            return _Rec(0.0)
        if method == "ZCtrl_SetpntGet":
            return _Rec(self.setpoint)
        if method == "Current_Get":
            return _Rec(float("nan") if self.nancur else self._i())
        if method == "ZCtrl_ZPosGet":
            return _Rec(float("nan") if self.nanz else self._z())
        return _Rec(0.0)

    def check_abort(self):
        self._n_abort += 1
        return self.abort_at is not None and self._n_abort >= self.abort_at

    def names(self):
        return [c[0] for c in self.calls]

    def bias_sets(self):
        return [c[1][0] for c in self.calls if c[0] == "Bias_Set"]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Fake clock: sleep only advances time. z_extend_sign = -1."""
    state = {"t": 0.0}
    monkeypatch.setattr(VB.time, "monotonic", lambda: state["t"])
    monkeypatch.setattr(VB.time, "sleep",
                        lambda s: state.__setitem__("t", state["t"] + float(s)))
    from mast.core import instrument_profile as ip
    monkeypatch.setattr(ip, "z_extend_sign_or_none", lambda: -1)
    return state


@pytest.fixture
def retract_calls(monkeypatch):
    """Replace SafeRetract.execute with a recorder (the real one polls)."""
    calls = []

    def fake_execute(self, context, params):
        calls.append(params)
        context.safe_call("ZCtrl_Withdraw", 0, 1)
        return VB.SkillResult(skill_name="SafeRetract", success=True,
                              data={"retracted": True})

    monkeypatch.setattr(tip_mod.SafeRetract, "execute", fake_execute)
    return calls


def _run(ctx, **p):
    return VB.VerifyBiasAtJunction().execute(ctx, p)


# -- healthy ---------------------------------------------------------------

def test_healthy_junction_is_ok(retract_calls):
    ctx = Ctx(healthy=True)
    res = _run(ctx)
    assert res.success and res.data["verdict"] == "ok"
    d = res.data
    assert d["evidence"] == "active" and d["active_check_done"]
    assert d["sign_agrees"] is True
    assert d["bias_v"] == 2.0 and d["setpoint_a"] == 100e-12
    assert d["current_median_a"] == pytest.approx(100e-12)
    # +2 -> 1.2 V: tip toward the sample, 0.8 V * (175pm/0.8V) = -175 pm
    assert d["target_bias_v"] == pytest.approx(1.2)
    assert d["dz_lowered_m"] == pytest.approx(-175e-12, rel=1e-3)
    assert d["dz_restored_m"] == pytest.approx(+175e-12, rel=1e-3)
    assert d["direction_as_expected"] is True
    assert d["bias_restored"] is True and ctx.bias_sets()[-1] == 2.0
    assert retract_calls == [], "a healthy junction must not be withdrawn"
    assert "ZCtrl_Withdraw" not in ctx.names()


def test_negative_bias_keeps_polarity(retract_calls):
    ctx = Ctx(healthy=True, bias=-2.0)
    res = _run(ctx)
    assert res.data["verdict"] == "ok"
    assert res.data["target_bias_v"] == pytest.approx(-1.2)
    assert all(b < 0 for b in ctx.bias_sets())


def test_response_in_wrong_direction_is_flagged_not_failed(retract_calls):
    ctx = Ctx(healthy=True)
    ctx.PM_PER_V = -Ctx.PM_PER_V       # tip retracts when |V| drops
    res = _run(ctx)
    assert res.data["verdict"] == "ok"
    assert res.data["direction_as_expected"] is False
    assert "warning" in res.data


# -- the incident: dead junction -------------------------------------------

def test_inverted_current_is_bias_not_at_junction_and_retracts(retract_calls):
    ctx = Ctx(healthy=False)             # I = -97 pA at +2 V, setpoint 100 pA
    res = _run(ctx)
    assert not res.success
    assert res.data["verdict"] == "bias_not_at_junction"
    assert res.data["sign_agrees"] is False
    assert res.data["current_median_a"] == pytest.approx(-97e-12)
    assert res.data["passive_bad"] is True
    assert len(retract_calls) == 1, "SafeRetract must be called"
    assert "ZCtrl_Withdraw" in ctx.names()
    # detected passively: no bias step was needed
    assert ctx.bias_sets() == []
    assert "外部偏压线" in res.error and "bias cable" in res.error or "偏压线" in res.error
    assert res.data["withdraw"]["called"] is True


def test_retract_never_uses_emergency_path(retract_calls):
    ctx = Ctx(healthy=False)
    _run(ctx)
    # EmergencyRetract turns the feedback ON first; this skill must not.
    assert "ZCtrl_OnOffSet" not in ctx.names()


def test_real_safe_retract_is_the_path(monkeypatch):
    """Without the stub: the real SafeRetract issues ZCtrl_Withdraw only."""
    import types
    monkeypatch.setattr(tip_mod, "tip_parked", lambda c: types.SimpleNamespace(
        state="unreadable", retry_useful=False, reason="stub", evidence=lambda: "stub",
        feedback_on=None, module_status=None, z_m=None, rail_m=None, gap_m=None,
        tolerance_m=None, unreadable=(), undeclared=(), read_at=0))
    monkeypatch.setattr(tip_mod.SafeRetract, "_confirm_budget_s", 0.0)
    ctx = Ctx(healthy=False)
    res = _run(ctx)
    assert res.data["verdict"] == "bias_not_at_junction"
    assert "ZCtrl_Withdraw" in ctx.names()
    assert "ZCtrl_OnOffSet" not in ctx.names()
    assert res.data["withdraw"]["called"] is True


def test_withdraw_on_fail_false_leaves_the_tip_and_says_so(retract_calls):
    ctx = Ctx(healthy=False)
    res = _run(ctx, withdraw_on_fail=False)
    assert res.data["verdict"] == "bias_not_at_junction"
    assert retract_calls == [] and "ZCtrl_Withdraw" not in ctx.names()
    assert res.data["withdraw"] == {"called": False}


def test_no_z_response_with_plausible_current_fails_active(retract_calls):
    """Sign agrees (offset current happens to be positive) but Z is dead."""
    ctx = Ctx(healthy=False, current=95e-12)
    res = _run(ctx)
    assert res.data["verdict"] == "bias_not_at_junction"
    assert res.data["evidence"] == "active"
    assert abs(res.data["dz_lowered_m"]) < 10e-12
    assert abs(res.data["dz_restored_m"]) < 10e-12
    assert len(retract_calls) == 1
    assert ctx.bias_sets() == [pytest.approx(1.2), 2.0], "bias restored before retract"


def test_slow_drift_alone_is_not_a_response(retract_calls):
    ctx = Ctx(healthy=False, current=95e-12, drift_m_per_call=0.05e-12)
    res = _run(ctx)
    assert res.data["verdict"] == "bias_not_at_junction"


def test_small_z_response_but_current_moved_is_inconclusive(retract_calls):
    ctx = Ctx(healthy=False, current=95e-12)
    orig = ctx._i
    seq = {"n": 0}

    def moving_current():
        seq["n"] += 1
        # current halves once the bias has been lowered
        return 95e-12 if ctx.set_bias == ctx.ref_bias else 40e-12

    ctx._i = moving_current
    res = _run(ctx)
    assert res.data["verdict"] == "inconclusive"
    assert retract_calls == [], "no retract on inconclusive"
    assert not res.success


# -- passive-only ----------------------------------------------------------

def test_passive_only_ok_is_labelled_weak(retract_calls):
    ctx = Ctx(healthy=True)
    res = _run(ctx, active_check=False)
    assert res.success and res.data["verdict"] == "ok"
    assert res.data["evidence"] == "passive_only"
    assert ctx.bias_sets() == []


# -- inconclusive: never ok when it could not measure ---------------------

@pytest.mark.parametrize("kw", [
    dict(bias_get_error="no reply"),
    dict(nancur=True),
    dict(feedback=0),
])
def test_unreadable_is_inconclusive(kw, retract_calls):
    res = _run(Ctx(healthy=True, **kw))
    assert res.data["verdict"] == "inconclusive"
    assert not res.success
    assert retract_calls == []


def test_unreadable_z_is_inconclusive_and_never_ok(retract_calls):
    ctx = Ctx(healthy=True, nanz=True)
    res = _run(ctx)
    assert res.data["verdict"] == "inconclusive"
    assert ctx.bias_sets() == [], "no bias step when Z cannot be read"


def test_feedback_unreadable_is_inconclusive(retract_calls):
    ctx = Ctx(healthy=True)
    orig = ctx.safe_call

    def sc(method, *a, **k):
        if method == "ZCtrl_OnOffGet":
            return _Rec(error="timeout")
        return orig(method, *a, **k)

    ctx.safe_call = sc
    assert _run(ctx).data["verdict"] == "inconclusive"


def test_current_far_below_setpoint_is_not_engaged(retract_calls):
    """Tip withdrawn: I ~ 0. Neither ok nor a bias fault, and no retract."""
    ctx = Ctx(healthy=True, current=1e-13)
    res = _run(ctx)
    assert res.data["verdict"] == "inconclusive"
    assert res.data["engaged"] is False
    assert retract_calls == []


def test_bias_set_error_is_inconclusive_without_restore_confusion(retract_calls):
    res = _run(Ctx(healthy=True, bias_set_error="refused"))
    assert res.data["verdict"] == "inconclusive"


def test_unconfirmed_restore_is_inconclusive_and_loud(retract_calls):
    ctx = Ctx(healthy=True, fail_restore=True)
    res = _run(ctx)
    assert res.data["verdict"] == "inconclusive"
    assert res.data["bias_restored"] is False
    assert "NOT" in res.error or "未" in res.error


# -- zero-crossing guard ---------------------------------------------------

@pytest.mark.parametrize("bias,frac,minb", [
    (2.0, 0.2, 0.05), (-2.0, 0.2, 0.05), (0.5, 0.3, 0.2),
    (-0.5, 0.2, 0.3), (1.0, 0.6, 0.05),
])
def test_never_crosses_zero_or_goes_below_min_bias(bias, frac, minb, retract_calls):
    ctx = Ctx(healthy=True, bias=bias)
    _run(ctx, reduce_fraction=frac, min_bias_v=minb)
    sets = ctx.bias_sets()
    assert sets, "an active step was expected"
    for b in sets[:-1]:                       # the last set is the restore
        assert (b > 0) == (bias > 0) and b != 0
        assert abs(b) >= minb - 1e-12
        assert abs(b) < abs(bias)
    assert sets[-1] == bias


def test_no_meaningful_step_above_min_bias_skips_active(retract_calls):
    ctx = Ctx(healthy=True, bias=0.06)        # step would be 10 mV < min_step_v
    res = _run(ctx)
    assert res.data["verdict"] == "inconclusive"
    assert res.data["active_check_done"] is False
    assert ctx.bias_sets() == []


def test_out_of_range_fraction_is_refused_not_clamped(retract_calls):
    ctx = Ctx(healthy=True)
    res = _run(ctx, reduce_fraction=1.2)
    assert res.data["verdict"] == "inconclusive"
    assert ctx.bias_sets() == []


# -- abort mid-active restores the bias -----------------------------------

def test_abort_during_active_still_restores_bias(retract_calls):
    ctx = Ctx(healthy=True)
    # abort kicks in after the first window, during the settle wait
    ctx.abort_at = 60
    res = _run(ctx)
    assert res.data["verdict"] == "inconclusive"
    sets = ctx.bias_sets()
    assert sets and sets[-1] == 2.0
    assert [c for c in ctx.calls if c[0] == "Bias_Set" and c[1][0] == 2.0][-1][2] is True


# -- metadata / wiring -----------------------------------------------------

def test_metadata_shape():
    m = VB.VerifyBiasAtJunction().metadata()
    assert m.name == "VerifyBiasAtJunction"
    names = {p.name for p in m.parameters}
    assert {"active_check", "reduce_fraction", "min_bias_v", "min_dz_m",
            "withdraw_on_fail"} <= names
    defaults = {p.name: p.default for p in m.parameters}
    assert defaults["active_check"] is True
    assert defaults["withdraw_on_fail"] is True
    assert defaults["reduce_fraction"] == 0.6


def test_exported_from_builtins_package():
    import mast.skills.builtins as b
    assert b.VerifyBiasAtJunction is VB.VerifyBiasAtJunction
    assert "VerifyBiasAtJunction" in b.__all__


# -- response-size regimes ------------------------------------------------

def test_large_same_direction_drift_is_never_ok(retract_calls):
    """Dead junction + slow drift (large, same direction): not a step
    response. Must not be ok; and (not provably dead) must not retract."""
    ctx = Ctx(healthy=False, current=95e-12, drift_m_per_call=3e-12)
    res = _run(ctx)
    d = res.data
    assert abs(d["dz_lowered_m"]) > 50e-12 and abs(d["dz_restored_m"]) > 50e-12
    assert d["dz_opposite_directions"] is False
    assert d["verdict"] == "inconclusive" and not res.success
    assert retract_calls == []


def test_asymmetric_response_is_not_ok(retract_calls):
    """Opposite signs but one transition 10x the other: drift on top of a
    weak step is not a clean response."""
    ctx = Ctx(healthy=True)
    ctx.PM_PER_V = 40e-12 / 0.8         # ~ -40 pm down ...
    ctx.drift = -2.5e-12                # ... swamped on the way back up
    res = _run(ctx)
    assert res.data["verdict"] != "ok"


@pytest.mark.parametrize("bias,pm_per_v", [
    (2.0, 175e-12 / 0.8),       # moderate response
    (-2.0, 1056e-12 / 0.8),     # strong response (semiconducting-like)
    (2.0, 21e-12 / 0.5),        # weak response (metallic-like area)
])
def test_measured_healthy_responses_are_ok(bias, pm_per_v, retract_calls):
    ctx = Ctx(healthy=True, bias=bias)
    ctx.PM_PER_V = pm_per_v
    res = _run(ctx, reduce_fraction=0.75 if pm_per_v == 21e-12 / 0.5 else 0.6)
    assert res.data["verdict"] == "ok", res.data
    assert retract_calls == []
