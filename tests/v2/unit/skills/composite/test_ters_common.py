"""ters_common 的不变量 —— 每条都有负例（modes_spec §0-5：「期间禁止」一栏 = 持令牌期间的不变量）。

跑在真 ExecutionContext 上（ctx.run 的全部闸门、令牌、中止都在），STM 是 _ters_harness.FakeStm，
光谱仪是本机模拟光谱仪或卫星枢纽替身（tests/v2/instruments/fake_hub.py）。
"""
from __future__ import annotations

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

import threading
import time

import numpy as np
import pytest

from mast.skills.composite import ters_analysis as ta
from mast.skills.composite import ters_common as tc
from tests.v2.instruments.fake_hub import FakeSatelliteHub
from tests.v2.unit.skills.composite._ters_fixtures import (  # noqa: F401 — fixtures
    FAST,
    ctx,
    scope,
    sim_ccd,
    stm,
    ters_env,
)
from tests.v2.unit.skills.composite._ters_harness import (
    SIM_CCD,
    FakeStm,
    OkGuard,
    make_ctx,
    skill_registry,
    write_bench,
)


def _point(ctx, **params):
    return ctx.run("TERSPointSpectrum", {**FAST, **params})


def _acquisitions(reg, dev="sim_ccd") -> int:
    return reg.spectrometer(dev).acquisitions


def during_point_acquire(reg, fn, *, delay=0.2, nth=1, dev="sim_ccd"):
    """Run ``fn()`` ``delay`` s after the ``nth`` non-dark acquisition on the simulated
    spectrometer has started — event-driven, so preflight / dark-frame timing cannot shift it."""
    spec = reg.spectrometer(dev)
    orig = spec._acquire_raw
    seen = {"n": 0}

    def wrapped(**kw):
        if "dark" not in str(kw.get("tag") or ""):
            seen["n"] += 1
            if seen["n"] == nth:
                threading.Timer(delay, fn).start()
        return orig(**kw)
    spec._acquire_raw = wrapped
    return seen


def during_sat_acquire(dev, fn, *, delay=0.2, nth=1):
    """Same for the satellite hub double's spectrometer device."""
    orig = dev._acquire
    seen = {"n": 0}

    def wrapped(args, pending):
        if "dark" not in str(args.get("tag") or ""):
            seen["n"] += 1
            if seen["n"] == nth:
                threading.Timer(delay, fn).start()
        return orig(args, pending)
    dev._acquire = wrapped
    return seen


# ── the exposure interlock, unit level ──────────────────────────────────────

FORBIDDEN_DURING_EXPOSURE = [
    # STM: XY, bias, setpoint, Z, feedback, scan, coarse (any group), digital lines, lock-in
    "MoveToXY", "SetBias", "BiasSettleChange", "SetBiasRamp", "SetSetpoint", "SetZPosition",
    "ZControllerOnOff", "SetTipLift", "StartScan", "ConfigureScan", "MotorMove",
    "NanonisMotorGroupMove", "PulseDigitalLine", "SetDigitalLineStatus", "ConfigureDigitalLine",
    "ConfigureLockIn",
    # optics: every stage, the polariser, the delay line, Picomotor, XYZ
    "OpticalStageMove", "OpticalStageWiggle", "HomeOpticalStage", "DelayLineMoveTo",
    "SetPolarizerAngle", "HomePolarizer", "PicomotorJog", "MoveXYZStage", "OpticalStageScan",
    # spectrometer settings and a second acquisition
    "ConfigureSpectrometer", "SetSpectrometerGrating", "SetSpectrometerCenter",
    "SetSpectrometerExitPort", "AcquireSpectrum",
]
ALLOWED_DURING_EXPOSURE = ["GetCurrent", "GetZPosition", "GetBias", "GetSpectrometerStatus",
                           "GetPolarizerAngle", "StopScan", "SafeRetract", "WithdrawTip",
                           "StopSpectrometer", "StopSatelliteDevices", "StopOpticalStage",
                           "StopMotor"]


@pytest.fixture(scope="module")
def metas():
    reg = skill_registry()
    names = FORBIDDEN_DURING_EXPOSURE + ALLOWED_DURING_EXPOSURE
    out = {}
    for n in names:
        try:
            out[n] = reg._get_metadata(reg.get(n))
        except KeyError:
            out[n] = None
    return out


@pytest.mark.parametrize("name", FORBIDDEN_DURING_EXPOSURE)
def test_every_forbidden_action_is_refused_inside_the_window(name, metas):
    assert metas[name] is not None, f"{name} not registered — the forbidden list is stale"
    lk = tc.ExposureInterlock()
    assert lk.refusal(name, metas[name]) == ""          # outside: allowed
    with lk.window("px_3_5"):
        msg = lk.refusal(name, metas[name])
    assert msg and "px_3_5" in msg and name in msg


@pytest.mark.parametrize("name", ALLOWED_DURING_EXPOSURE)
def test_reads_and_stops_stay_allowed_inside_the_window(name, metas):
    assert metas[name] is not None, name
    lk = tc.ExposureInterlock()
    with lk.window("w"):
        assert lk.refusal(name, metas[name]) == ""


def test_direct_nanonis_writes_inside_the_window():
    lk = tc.ExposureInterlock()
    with lk.window("w"):
        for m, a in (("Bias_Set", (1.0,)), ("ZCtrl_ZPosSet", (1e-9,)), ("FolMe_XYPosSet", (0, 0, 0)),
                     ("DigLines_Pulse", (1, [2], 0.005, 0, 1, 1)), ("Motor_StartMove", (0, 1, 0, 1)),
                     ("ZCtrl_OnOffSet", (0,)), ("LockIn_ModOnOffSet", (1, 1))):
            assert lk.nanonis_refusal(m, a), m
        for m, a in (("ZCtrl_Withdraw", (0, 1)), ("Motor_StopMove", ()), ("Current_Get", ()),
                     ("Scan_Action", (1, 0))):
            assert lk.nanonis_refusal(m, a) == "", m


def test_two_windows_never_overlap():
    lk = tc.ExposureInterlock()
    with lk.window("a"):
        with pytest.raises(tc.InterlockViolation):
            with lk.window("b"):
                pass


class _BuggyMode(tc.TersComposite):
    """A mode that (wrongly) tries to move the tip while the camera integrates."""

    MODE_ID, MODE_KEY = "MX", "ters_buggy"

    def metadata(self):
        return tc.ters_metadata(name="TERSBuggyTestOnly", description="test double",
                                parameters=[*tc.cp_a(), *tc.cp_b(), *tc.cp_c(),
                                            *tc.common_extra()],
                                tags=["optics", "ters"], duration_s=1.0)

    def ters_plan(self, run):
        with run.interlock.window("px_0_0"):
            yield from run.step("move", "MoveToXY", {"x_m": 1e-9, "y_m": 1e-9, "wait": True})


def test_a_mode_that_moves_during_exposure_is_stopped_before_the_hardware_sees_it(stm, sim_ccd,
                                                                                ters_env):
    ctx = make_ctx(stm)
    res = _BuggyMode().execute(ctx, {**FAST})
    assert not res.success and res.data["status"] == "needs_operator"
    assert "内部联锁拒绝" in res.error
    assert not [w for w in stm.writes if w[0] == "FolMe_XYPosSet"]


# ── exposure guard ──────────────────────────────────────────────────────────

@pytest.fixture
def slow_ccd(tmp_path):
    from mast.instruments.registry import reset_instrument_registry

    reg = write_bench(tmp_path, [{**SIM_CCD, "time_scale": 1.0}])
    yield reg
    reset_instrument_registry(None)


def test_current_spike_during_exposure_trips_the_guard_and_stops(stm, slow_ccd, ters_env):
    ctx = make_ctx(stm)

    def spike():
        now = time.monotonic()
        stm.spikes.append((now, now + 0.4, 1e-9))      # 10 × setpoint for 400 ms
    during_point_acquire(slow_ccd, spike, delay=0.3, nth=2)
    t0 = time.monotonic()
    res = _point(ctx, repeats=3, exposure_ms=1500.0)
    assert not res.success
    d = res.data
    assert d["status"] == "needs_operator" and "guard_abort" in res.error
    assert d["qc_counts"].get("guard_abort") == 1
    assert [p["status"] for p in d["points"]] == ["ok", "guard_abort"]
    # the guard stopped the second exposure early: no third acquisition, and the
    # run took far less than dark + three full 1.5 s exposures
    assert _acquisitions(slow_ccd) == 2                  # dark + r0 (r1 was stopped)
    assert time.monotonic() - t0 < 5.5


def test_feedback_dropping_during_exposure_trips_the_guard(stm, slow_ccd, ters_env):
    ctx = make_ctx(stm)

    def drop():
        with stm.lock:
            stm.fb = False
    during_point_acquire(slow_ccd, drop, delay=0.3)
    res = _point(ctx, repeats=2, exposure_ms=2000.0)
    assert not res.success and res.data["status"] == "needs_operator"
    assert res.data["points"][0]["status"] == "guard_abort"


def test_guard_spec_limit():
    assert tc.GuardSpec(i_ref_a=1e-10, frac=3).limit() == pytest.approx(3e-10)
    assert tc.GuardSpec(i_ref_a=1e-10, frac=3, i_abs_limit_a=2e-10).limit() == pytest.approx(2e-10)
    assert tc.GuardSpec(expect_feedback=None).limit() is None


# ── abort ───────────────────────────────────────────────────────────────────

def test_abort_mid_run_stops_without_restoring(stm, slow_ccd, ters_env):
    ev = threading.Event()
    ctx = make_ctx(stm, abort=ev)
    during_point_acquire(slow_ccd, ev.set, delay=0.2, nth=2)
    res = _point(ctx, repeats=5, exposure_ms=400.0, apply_conditions=True, bias_v=0.6,
                 setpoint_a=1.2e-10)
    assert not res.success
    d = res.data
    assert d["status"] == "partial" and "aborted" in d["reason"]
    assert 0 < sum(1 for p in d["points"] if p["status"] == "ok") < 5
    # nothing was put back after the abort: bias / setpoint stay at the run's values
    assert stm.bias == pytest.approx(0.6) and stm.setpoint == pytest.approx(1.2e-10)
    assert ev.is_set()


@pytest.mark.parametrize("skill, params", [
    ("TERSPointSpectrum", {"repeats": 3}),
    ("TERSBiasSeries", {"biases_v": "0.4,0.6", "bias_settle_s": 0.0, "ref_every_n": 0}),
])
@pytest.mark.parametrize("judged", ["aborted", "feedback_state", "timeout:mean_off_setpoint"])
def test_stop_during_the_judging_is_not_an_unstable_tip(stm, sim_ccd, ters_env, monkeypatch,
                                                        skill, params, judged):
    """An E-STOP sets the abort, then withdraws the tip while the next point is being
    judged — TERSBiasSeries recorded that point as unstable_tip. Whatever the window saw (abort /
    feedback off / current gone), the point was stopped, not unstable: it is recorded as aborted
    or not at all, qc has no unstable_tip, and the run is partial, not needs_operator."""
    ev = threading.Event()
    orig = tc.TersRun.tip_ready
    calls = {"n": 0}

    def judged_then_stopped(self, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            ev.set()
            return tc.TipReady(False, judged)
        return orig(self, **kw)
    monkeypatch.setattr(tc.TersRun, "tip_ready", judged_then_stopped)
    res = make_ctx(stm, abort=ev).run(skill, {**FAST, **params})
    d = res.data
    assert d["status"] == "partial" and "aborted" in d["reason"], d["reason"]
    st = [p["status"] for p in d["points"]]
    assert st[0] == "ok" and set(st[1:]) <= {"aborted"}, st
    assert "unstable_tip" not in d["qc_counts"]


def test_abort_latched_before_start_refuses_everything(stm, sim_ccd, ters_env):
    ev = threading.Event()
    ev.set()
    ctx = make_ctx(stm, abort=ev)
    res = _point(ctx)
    assert not res.success and "aborted" in res.error
    assert _acquisitions(sim_ccd) == 0 and not stm.writes


# ── preflight refusals: nothing moves, nothing is exposed ───────────────────

@pytest.mark.parametrize("setup, word", [
    (lambda s: setattr(s, "fb", False), "反馈没开"),
    (lambda s: setattr(s, "safetip", False), "SafeTip"),
    (lambda s: setattr(s, "lockin", True), "锁相"),
    (lambda s: setattr(s, "scan", True), "扫描"),
    (lambda s: setattr(s, "noise", 0.8), "电流不在设定点"),
    (lambda s: s.fail.add("Current_Get"), "读不到"),
])
def test_preflight_refuses_and_touches_nothing(stm, sim_ccd, ters_env, setup, word):
    setup(stm)
    res = _point(make_ctx(stm))
    assert not res.success and res.data["status"] == "refused"
    assert word in res.error
    assert _acquisitions(sim_ccd) == 0
    assert not [w for w in stm.writes if w[0] not in ("DigLines_PropsSet",)]


def test_port_guard_conflict_refuses(stm, sim_ccd, ters_env):
    from mast.core.port_guard import Finding

    ters_env.guard.conflicts = [Finding(kind="nanonis_tcp", resource="tcp:6501(main)",
                                        severity="conflict", pid=4242, process="LabVIEW.exe")]
    res = _point(make_ctx(stm))
    assert not res.success and "LabVIEW.exe" in res.error and "6501" in res.error
    assert _acquisitions(sim_ccd) == 0


def test_tip_work_holding_the_token_refuses(stm, sim_ccd, ters_env):
    from mast.core.instrument_lock import instrument_lock

    lk = instrument_lock()
    assert lk.acquire(owner="test", skill="PrepareNobleTip", timeout_s=1.0)
    try:
        res = _point(make_ctx(stm))
    finally:
        lk.release()
    assert not res.success and "修针" in res.error
    assert _acquisitions(sim_ccd) == 0


def test_lfe_preset_is_refused_honestly(stm, sim_ccd, ters_env):
    res = _point(make_ctx(stm), lfe_preset="ters_50rows.lfe")
    assert not res.success and "lfe_preset" in res.error


@pytest.mark.parametrize("bad", [
    {"exposure_ms": 0.0}, {"exposure_ms": 700000.0}, {"repeats": 0}, {"frames": 1001},
    {"bias_v": 5.0, "apply_conditions": True, "setpoint_a": 1e-10},
    {"grating": "1800"}, {"shutter_mode": "Open"}, {"stable_tol_frac": 0.0},
    {"exposure_guard_current_frac": 1.0}, {"x_m": 2e-6, "use_current_position": False, "y_m": 0.0},
])
def test_out_of_range_parameters_are_refused_by_the_gate(stm, sim_ccd, ters_env, bad):
    res = _point(make_ctx(stm), **bad)
    assert not res.success
    assert _acquisitions(sim_ccd) == 0 and not stm.writes


def test_missing_conditional_parameters_are_refused(stm, sim_ccd, ters_env):
    res = _point(make_ctx(stm), use_current_position=False)
    assert not res.success and "x_m" in res.error
    res = _point(make_ctx(stm), apply_conditions=True)
    assert not res.success and "bias_v" in res.error


# ── laser attestation (CP-C) ────────────────────────────────────────────────

def test_direct_call_without_laser_state_is_refused(stm, sim_ccd, ters_env):
    res = ctx_run = make_ctx(stm).run("TERSPointSpectrum", {**FAST, "laser_state": ""})
    assert not ctx_run.success and "laser_state" in res.error
    assert _acquisitions(sim_ccd) == 0


def test_external_agent_cannot_attest(stm, sim_ccd, ters_env):
    res = make_ctx(stm, owner="ext:claude").run("TERSPointSpectrum", {**FAST})
    assert not res.success and "外部 agent" in res.error
    assert _acquisitions(sim_ccd) == 0


def test_laser_attest_false_records_unknown(stm, sim_ccd, ters_env):
    res = _point(make_ctx(stm), laser_attest=False, laser_state="")
    assert res.success and res.data["laser"]["laser_state"] == "unknown"


def test_hitl_decision_is_recorded(stm, sim_ccd, ters_env, monkeypatch):
    seen = []

    def channel(payload):
        seen.append(payload)
        return {"route": "laser_off", "note": "shutter closed"}
    monkeypatch.setattr(tc, "human_channel", channel)
    res = _point(make_ctx(stm), laser_state="")
    assert res.success, res.error
    assert seen and seen[0]["kind"] == "workflow_human" and "cancel" in seen[0]["routes"]
    assert res.data["laser"]["laser_state"] == "attested_off"
    assert res.data["laser"]["attested_via"] == "hitl"


def test_hitl_cancel_refuses(stm, sim_ccd, ters_env, monkeypatch):
    monkeypatch.setattr(tc, "human_channel", lambda p: {"route": "cancel"})
    res = _point(make_ctx(stm), laser_state="")
    assert not res.success and _acquisitions(sim_ccd) == 0


def test_graph_interrupt_bubbles_before_any_write(stm, sim_ccd, ters_env, monkeypatch):
    from langgraph.errors import GraphInterrupt

    def channel(payload):
        raise GraphInterrupt(())
    monkeypatch.setattr(tc, "human_channel", channel)
    with pytest.raises(GraphInterrupt):
        _point(make_ctx(stm), laser_state="")
    assert _acquisitions(sim_ccd) == 0 and not stm.writes


# ── dry run ─────────────────────────────────────────────────────────────────

def test_dry_run_exposes_nothing_writes_nothing_publishes_nothing(stm, sim_ccd, ters_env):
    res = _point(make_ctx(stm), dry_run=True, laser_state="", repeats=3,
                 use_current_position=False, x_m=5e-9, y_m=5e-9)
    assert res.success, res.error
    d = res.data
    assert d["status"] == "dry_run" and d["plan"]["estimated_s"] > 0
    assert d["plan"]["tip_ready_now"]["passed"] is True
    assert d["plan"]["acquire_check"]["status"] == "dry_run"
    assert _acquisitions(sim_ccd) == 0
    assert not stm.writes, stm.writes                        # did not even move
    assert not ters_env.rec.declared()
    from mast.skills.composite.ters_common import RunStore

    assert not (RunStore.root() / d["run_id"]).exists()


def test_dry_run_motion_moves_but_never_exposes(stm, sim_ccd, ters_env):
    res = _point(make_ctx(stm), dry_run=True, dry_run_motion=True, laser_state="",
                 use_current_position=False, x_m=5e-9, y_m=6e-9)
    assert res.success, res.error
    assert ("FolMe_XYPosSet", (5e-9, 6e-9, 0)) in stm.writes
    assert _acquisitions(sim_ccd) == 0
    assert not ters_env.rec.declared()


# ── unstable tip ────────────────────────────────────────────────────────────

def test_unstable_tip_is_skipped_or_stops_by_policy(stm, sim_ccd, ters_env, monkeypatch):
    # preflight passes on a quiet junction; right after it the junction turns noisy
    orig_window = tc.TersRun._preflight_current_window

    def then_noisy(self, refusals):
        stm.noise = 0.01
        orig_window(self, refusals)
        stm.noise = 0.9
    monkeypatch.setattr(tc.TersRun, "_preflight_current_window", then_noisy)
    res = _point(make_ctx(stm), repeats=2, stable_timeout_s=1.0, on_unstable="skip")
    assert res.success, res.error
    assert [p["status"] for p in res.data["points"]] == ["unstable_tip", "unstable_tip"]
    assert res.data["qc_counts"]["unstable_tip"] == 2
    assert _acquisitions(sim_ccd) == 1                    # only the dark frame
    res = _point(make_ctx(stm), repeats=2, stable_timeout_s=1.0, on_unstable="abort")
    assert not res.success and res.data["status"] == "needs_operator"
    assert "unstable_tip" in res.error
    res = _point(make_ctx(stm), repeats=1, stable_timeout_s=1.0, on_unstable="retry",
                 retry_max=2)
    assert res.success and res.data["points"][0]["status"] == "unstable_tip"


# ── satellite: hold, dropout, restart ───────────────────────────────────────

@pytest.fixture
def sat(tmp_path):
    from mast.instruments.registry import reset_instrument_registry
    from mast.instruments.remote import set_hub_provider

    hub = FakeSatelliteHub()
    dev = hub.add_device("rig1", "ccd", "spectrometer", n_px=128, duration_s=0.05)
    set_hub_provider(lambda: hub)
    reg = write_bench(tmp_path, [{"id": "rig1_ccd", "type": "satellite_spectrometer",
                                  "satellite": "rig1", "device": "ccd", "role": "raman_ccd",
                                  "laser_line_nm": 633.0}])
    yield hub, dev, reg
    set_hub_provider(None)
    reset_instrument_registry(None)


def _sat_acqs(hub) -> int:
    return sum(1 for c in hub.calls if c[2] == "acquire")


def test_satellite_run_records_clock_and_boot_id(stm, sat, ters_env):
    hub, dev, reg = sat
    res = _point(make_ctx(stm), repeats=2, band_cm1="")
    assert res.success, res.error
    d = res.data
    assert d["satellite"] == {"id": "rig1", "boot_id": "b1"}
    assert d["preflight"]["satellite"]["clock_offset_ms"] == pytest.approx(2.5)
    assert _sat_acqs(hub) == 3                            # dark + 2 points
    # the fake device returns identical frames, so the store dedups the .npy against the
    # dark frame — the sidecar is per acquisition and is what carries the metadata
    side = Path(d["points"][0]["sidecar"])
    import json as _j

    meta = _j.loads(side.read_text(encoding="utf-8"))
    assert meta["extra"]["satellite"]["boot_id"] == "b1"
    assert meta["time"]["clock_offset_ms"] == pytest.approx(2.5)


def test_operator_hold_refuses_at_preflight(stm, sat, ters_env):
    hub, dev, reg = sat
    orig = dev.status
    dev.status = lambda: {**orig(), "operator_hold": True}
    res = _point(make_ctx(stm))
    assert not res.success and "operator_hold" in res.error and _sat_acqs(hub) == 0


def test_satellite_not_connected_refuses(stm, sat, ters_env):
    hub, dev, reg = sat
    hub.connected["rig1"] = False
    res = _point(make_ctx(stm))
    assert not res.success and res.data["status"] == "refused" and _sat_acqs(hub) == 0


def test_satellite_restart_mid_run_stops_without_reshooting(stm, sat, ters_env):
    hub, dev, reg = sat
    orig = hub.list_satellites
    n = {"calls": 0}

    def flaky():
        n["calls"] += 1
        out = orig()
        if n["calls"] > 3:                       # after preflight + the first point
            for s in out:
                s["boot_id"] = "b2"
        return out
    hub.list_satellites = flaky
    res = _point(make_ctx(stm), repeats=4)
    assert not res.success and res.data["status"] == "needs_operator"
    assert "lost_on_restart" in res.error and "绝不自动补拍" in res.error
    assert _sat_acqs(hub) < 5


def test_satellite_dropping_during_exposure_is_lost_not_retried(stm, sat, ters_env):
    hub, dev, reg = sat
    dev.duration_s = 0.6

    def drop():
        hub.connected["rig1"] = False
        for p in list(hub.listeners.values()):
            p("rig1", {"kind": "link", "device": "", "data": {"state": "lost"}})
    during_sat_acquire(dev, drop, delay=0.2, nth=2)
    res = _point(make_ctx(stm), repeats=3)
    assert not res.success and res.data["status"] == "needs_operator"
    assert [p["status"] for p in res.data["points"]] == ["ok", "satellite_lost"]
    assert "绝不自动补拍" in res.error
    assert _sat_acqs(hub) == 3                     # dark + r0 + the lost r1, never re-shot


def test_manual_activity_during_exposure_voids_the_frame(stm, sat, ters_env):
    hub, dev, reg = sat
    dev.duration_s = 0.5

    def poke():
        hub.emit("rig1", "ccd", "manual_activity", {"what": "someone clicked in LightField"})
    during_sat_acquire(dev, poke, delay=0.2, nth=1)
    res = _point(make_ctx(stm), repeats=3)
    assert not res.success and res.data["status"] == "needs_operator"
    assert "manual_activity" in res.error
    assert res.data["qc_counts"].get("manual_activity") == 1
    assert [p["status"] for p in res.data["points"]] == ["manual_activity"]


# ── resume ──────────────────────────────────────────────────────────────────

def test_resume_continues_from_the_first_unfinished_point(stm, slow_ccd, ters_env):
    ev = threading.Event()
    ctx = make_ctx(stm, abort=ev)
    during_point_acquire(slow_ccd, ev.set, delay=0.1, nth=3)
    first = _point(ctx, repeats=4, exposure_ms=300.0)
    assert first.data["status"] == "partial"
    done = [p["key"] for p in first.data["points"] if p["status"] == "ok"]
    assert 0 < len(done) < 4
    second = _point(make_ctx(stm), repeats=4, exposure_ms=300.0,
                    resume_run_id=first.data["run_id"])
    assert second.success, second.error
    new_keys = [p["key"] for p in second.data["points"]]
    assert not set(new_keys) & set(done) and len(done) + len(new_keys) == 4


def test_resume_refuses_when_the_spectrometer_settings_changed(stm, sim_ccd, ters_env):
    first = _point(make_ctx(stm), repeats=1)
    assert first.success
    res = _point(make_ctx(stm), repeats=1, exposure_ms=80.0, resume_run_id=first.data["run_id"])
    assert not res.success and "exposure_ms" in res.error


def test_resume_of_an_unknown_run_is_refused(stm, sim_ccd, ters_env):
    res = _point(make_ctx(stm), resume_run_id="ters-point-nope")
    assert not res.success and "检查点" in res.error


# ── judge_window (pure) ─────────────────────────────────────────────────────

def test_judge_window_criteria():
    t = list(np.linspace(0, 1, 51))
    z = [1e-9] * 51
    ok, why, _ = tc.judge_window(t, [1e-10] * 51, z, i_target=1e-10, tol=0.2,
                                 dz_limit_pm_s=10, sigma0=None, spike_frac=3)
    assert ok, why
    ok, why, _ = tc.judge_window(t, [1.5e-10] * 51, z, i_target=1e-10, tol=0.2,
                                 dz_limit_pm_s=10, sigma0=None, spike_frac=3)
    assert not ok and why == "mean_off_setpoint"
    i = [1e-10] * 51
    i[10] = 5e-10
    ok, why, _ = tc.judge_window(t, i, z, i_target=1e-10, tol=0.2, dz_limit_pm_s=10,
                                 sigma0=None, spike_frac=3)
    assert not ok and why == "spike"
    zd = [1e-9 + 50e-12 * k / 50 for k in range(51)]          # 50 pm/s
    ok, why, _ = tc.judge_window(t, [1e-10] * 51, zd, i_target=1e-10, tol=0.2,
                                 dz_limit_pm_s=10, sigma0=None, spike_frac=3)
    assert not ok and why == "z_drifting"
    noisy = list(1e-10 * (1 + 0.3 * np.sin(np.arange(51))))
    ok, why, _ = tc.judge_window(t, noisy, z, i_target=1e-10, tol=0.2, dz_limit_pm_s=10,
                                 sigma0=None, spike_frac=3)
    assert not ok and why == "current_noisy"
    ok, why, _ = tc.judge_window(t, noisy, z, i_target=1e-10, tol=0.2, dz_limit_pm_s=10,
                                 sigma0=1e-10, spike_frac=3)
    assert ok, "σ0 floor (low setpoint) must relax the noise criterion"


# ── analysis helpers ────────────────────────────────────────────────────────

def test_band_parsing_and_metrics():
    assert ta.parse_range("1560-1620") == (1560.0, 1620.0)
    assert ta.parse_range("-50--20") == (-50.0, -20.0)
    with pytest.raises(ValueError):
        ta.parse_range("1620-1560")
    bands = ta.parse_bands("ring:1540-1620,990-1010")
    assert [b.name for b in bands] == ["ring", "b2"]
    with pytest.raises(ValueError):
        ta.parse_bands("a:1-2,a:3-4")
    x = np.linspace(400, 700, 301)
    y = 100 + 0.1 * x + 500 * np.exp(-((x - 520) / 4) ** 2)
    m = ta.band_metrics(x, y, ta.Band("si", 500, 540))
    assert m["valid"] and m["area"] == pytest.approx(500 * 4 * np.sqrt(np.pi), rel=0.05)
    assert abs(m["peak_x"] - 520) < 1.5
    assert not ta.band_metrics(x, y, ta.Band("far", 1500, 1600))["valid"]
    assert abs(ta.shift_cm1_to_nm(750, 633) - 664.55) < 0.01


def test_cosmic_saturation_and_median():
    rng = np.random.default_rng(0)
    y = rng.poisson(200, 400).astype(float)
    assert ta.find_cosmic(y) == []
    y[123] += 5000
    assert ta.find_cosmic(y) == [123]
    assert ta.is_saturated(np.array([[60000.0]]), 0.9) and not ta.is_saturated(np.array([[100.0]]), 0.9)
    frames = np.vstack([y, y, y])
    frames[0, 123] = 200
    spec, info = ta.combine_frames(frames, "median_frames")
    assert info["combine"] == "median"
    spec2, info2 = ta.combine_frames(frames[:2], "median_frames")
    assert info2["combine"] == "mean" and "note" in info2


def test_contrast_cos2_kappa_drop_peak():
    c = ta.contrast(1000.0, 100.0, 10.0, 3)
    assert c["valid"] and not c["lower_bound"] and c["C"] == pytest.approx(9.0)
    c = ta.contrast(1000.0, 5.0, 10.0, 3)
    assert c["lower_bound"] and c["C"] == pytest.approx(1000 / 30 - 1)
    th = np.arange(0, 181, 15.0)
    i = 2 + 10 * np.cos(np.radians(th - 40)) ** 2
    f = ta.fit_cos2(th, i)
    assert f["valid"] and f["theta0_deg"] == pytest.approx(40, abs=0.5)
    assert f["A"] == pytest.approx(10, rel=1e-3) and f["B"] == pytest.approx(2, rel=1e-3)
    z = np.array([0.0, -0.1e-9, -0.2e-9])
    cur = 1e-10 * np.exp(2 * 10 * 0.1 * np.arange(3))
    k = ta.fit_kappa_per_nm(cur, z)
    assert k["valid"] and k["kappa_per_nm"] == pytest.approx(10, rel=1e-6)
    d = ta.DropDetector(0.5, 3)
    flags = [d.update(v) for v in [10, 10, 10, 10, 4, 4, 4, 4]]
    assert flags[-1] and d.flagged_at == 7
    v = np.zeros((3, 5))
    v[1, 2] = 5
    v[1, 1] = v[1, 3] = 3
    pk = ta.peak_of_grid(v, [0, 1, 2, 3, 4], [10, 20, 30])
    assert pk["index"] == [2, 1] and pk["axis1_best"] == pytest.approx(2.0)
