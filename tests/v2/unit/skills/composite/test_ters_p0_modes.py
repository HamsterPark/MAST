"""P0 模式：M04 TERSNearFarPair、M10 PumpProbeSpectral（M01 见 test_ters_point / test_ters_common）。"""
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

import json
import math
import threading

import numpy as np
import pytest

from tests.v2.unit.skills.composite._ters_fixtures import (  # noqa: F401 — fixtures
    FAST,
    scope,
    sim_ccd,
    stm,
    ters_env,
)
from tests.v2.unit.skills.composite._ters_harness import (
    DELAY_LINE,
    DELAY_STAGE,
    KAPPA_PER_M,
    SIM_CCD,
    TriggeredSim,
    make_ctx,
    write_bench,
)

NF_FAST = {**FAST, "bands_cm1": "si:500-540", "retract_nm": 5.0, "retract_speed_nm_s": 200.0,
           "retract_settle_s": 0.0, "return_settle_s": 0.1, "cycles": 2,
           "probe_settle_s": 0.01}                  # FakeStm answers at once; Mimea needs ~0.5 s


def _nearfar(ctx, **kw):
    return ctx.run("TERSNearFarPair", {**NF_FAST, **kw})


class TestNearFar:
    def test_cycles_contrast_and_return(self, stm, sim_ccd, ters_env):
        z0 = stm.z
        res = _nearfar(make_ctx(stm))
        assert res.success, res.error
        d = res.data
        keys = [p["key"] for p in d["points"]]
        assert keys == ["c0_NF", "c0_FF", "c1_NF", "c1_FF", "NF_post"]
        con = d["analysis"]["contrast"]["si"]
        assert len(con["per_cycle"]) == 2
        # the simulator's spectrum does not depend on the gap, so C ≈ 0 —
        # what matters here is that it is computed per cycle with a spread
        assert con["C_mean"] is not None and abs(con["C_mean"]) < 0.5
        assert d["analysis"]["tip_changed"] is False
        # feedback is closed again, tip back at z_NF
        assert stm.fb and abs(stm.z - z0) < 1e-12
        # the Z ramp went up 5 nm in ≤ 1 nm steps and came back to z_NF + 0.5 nm
        zs = [a[0] for (m, a) in stm.writes if m == "ZCtrl_ZPosSet"]
        assert max(zs) == pytest.approx(z0 + 5e-9) and len(zs) >= 2 * 2 * 5
        assert min(np.diff(sorted(set(zs)))) > 0 and max(np.abs(np.diff(zs))) <= 1e-9 + 1e-15
        assert d["unrestored"] == {}
        rec = ters_env.rec
        main = f"local/ters_nearfar/{d['run_id']}"
        assert rec.frames(main) and rec.frames(f"{main}/z")

    def test_return_overcurrent_retracts_and_stops(self, stm, sim_ccd, ters_env, monkeypatch):
        # the surface crept 1 nm towards the tip while it was retracted: on the way back
        # the current blows through max_return_current_a long before z_NF + guard
        orig = stm._set_feedback

        def creeping(on):
            orig(on)
            if not on:
                stm.z_fb_ref += 1.5e-9
        stm._set_feedback = creeping
        stm.safetip_threshold = 1e-9                     # must stay ≤ 10 × max_return_current_a
        res = _nearfar(make_ctx(stm), max_return_current_a=2e-10, cycles=1)
        assert not res.success and res.data["status"] == "needs_operator"
        assert "回针途中电流" in res.error and "退针" in res.error
        assert stm.withdrawn, "SafeRetract must have withdrawn the tip"
        assert res.data["unrestored"].get("z_feedback"), "feedback left open is reported"

    def test_dry_run_motion_retracts_and_returns_under_the_same_guards(self, stm, sim_ccd, ters_env):
        """dry_run_motion really ramps Z out and back (≤ 1 nm steps, feedback closed again), exposes
        nothing — and the return over-current bail-out fires exactly as in a real run."""
        z0 = stm.z
        res = _nearfar(make_ctx(stm), dry_run=True, dry_run_motion=True, laser_state="", cycles=1)
        assert res.success, res.error
        assert res.data["status"] == "dry_run"
        zs = [a[0] for (m, a) in stm.writes if m == "ZCtrl_ZPosSet"]
        assert max(zs) == pytest.approx(z0 + 5e-9) and max(np.abs(np.diff(zs))) <= 1e-9 + 1e-15
        assert stm.fb and abs(stm.z - z0) < 1e-12
        assert sim_ccd.spectrometer("sim_ccd").acquisitions == 0
        # the same creeping surface as test_return_overcurrent_retracts_and_stops, rehearsed
        orig = stm._set_feedback

        def creeping(on):
            orig(on)
            if not on:
                stm.z_fb_ref += 1.5e-9
        stm._set_feedback = creeping
        stm.safetip_threshold = 1e-9
        res = _nearfar(make_ctx(stm), dry_run=True, dry_run_motion=True, laser_state="",
                       max_return_current_a=2e-10, cycles=1)
        assert not res.success and res.data["status"] == "needs_operator"
        assert "回针途中电流" in res.error and stm.withdrawn

    def test_current_not_dropping_after_retract_refuses_the_far_field(self, stm, sim_ccd,
                                                                      ters_env):
        orig = stm.current
        z0 = stm.z

        def stuck():
            if not stm.fb and stm.z > z0 + 0.5e-9:
                return 0.5 * stm.setpoint   # past the probe the piezo stops doing anything
            return orig()
        stm.current = stuck
        res = _nearfar(make_ctx(stm), cycles=1)
        assert not res.success and "没降到噪声" in res.error
        assert [p["key"] for p in res.data["points"]] == ["c0_NF"]
        assert stm.withdrawn

    def test_abort_while_retracted_withdraws_instead_of_hovering(self, stm, sim_ccd, ters_env):
        ev = threading.Event()
        orig = stm._set_feedback

        def press_when_off(on):
            orig(on)
            if not on:
                threading.Timer(0.05, ev.set).start()
        stm._set_feedback = press_when_off
        res = _nearfar(make_ctx(stm, abort=ev), retract_settle_s=1.0, cycles=1)
        assert not res.success and res.data["status"] == "partial"
        assert stm.withdrawn
        assert any(w.get("sent") for w in res.data["withdraws"])      # said in the result itself

    def test_safetip_threshold_must_match(self, stm, sim_ccd, ters_env):
        stm.safetip_threshold = 1e-11                      # below 1.2 × I_set
        res = _nearfar(make_ctx(stm))
        assert not res.success and "SafeTip 阈值" in res.error
        assert not [w for w in stm.writes if w[0] == "ZCtrl_OnOffSet"]

    def test_z_targets_outside_the_global_envelope_are_refused_up_front(self, stm, sim_ccd,
                                                                         ters_env):
        stm.z = -20e-9                                     # SafetyLimits.z_min_m = 0
        stm.z_fb_ref = stm.z
        res = _nearfar(make_ctx(stm))
        assert not res.success and "全局 Z 包络" in res.error
        assert not [w for w in stm.writes if w[0] in ("ZCtrl_OnOffSet", "ZCtrl_ZPosSet")]

    def test_bands_are_required(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSNearFarPair", {**NF_FAST, "bands_cm1": ""})
        assert not res.success

    def test_lateral_bare_reference(self, stm, sim_ccd, ters_env):
        res = _nearfar(make_ctx(stm), reference_kind="lateral_bare", bare_x_m=20e-9,
                       bare_y_m=20e-9, cycles=1)
        assert res.success, res.error
        assert [p["key"] for p in res.data["points"]] == ["c0_NF", "c0_FF_bare", "NF_post"]
        moves = [a for (m, a) in stm.writes if m == "FolMe_XYPosSet"]
        assert moves[0][:2] == (20e-9, 20e-9) and moves[1][:2] == (0.0, 0.0)

    def test_dry_run_plans_the_z_targets_without_moving(self, stm, sim_ccd, ters_env):
        res = _nearfar(make_ctx(stm), dry_run=True, laser_state="")
        assert res.success, res.error
        zt = res.data["plan"]["z_targets_m"]
        assert zt["z_ff"] == pytest.approx(zt["z_nf"] + 5e-9)
        assert not stm.writes


def _zset(stm) -> list[float]:
    return [a[0] for (m, a) in stm.writes if m == "ZCtrl_ZPosSet"]


class TestRetractDirection:
    """M04's open-loop Z direction comes from instrument_profile.z_extend_sign (spec §1.4 item 5),
    is checked by a first small step, and the ramp carries a current ceiling."""

    def test_undeclared_sign_refuses_before_opening_the_loop(self, stm, sim_ccd, ters_env):
        from mast.core import instrument_profile

        instrument_profile.set_profile({})              # nobody ever answered (ters_env restores)
        res = _nearfar(make_ctx(stm), cycles=1)
        assert not res.success and res.data["status"] == "refused"
        assert "z_extend_sign" in res.error and "Withdraw 后 Z 停在正轨" in res.error
        assert not [w for w in stm.writes if w[0] in ("ZCtrl_OnOffSet", "ZCtrl_ZPosSet")]
        # a lateral reference needs no open-loop Z, so it does not need the sign either
        res = _nearfar(make_ctx(stm), reference_kind="lateral_bare", bare_x_m=20e-9,
                       bare_y_m=20e-9, cycles=1)
        assert res.success, res.error

    def test_minus_one_probes_then_retracts_towards_plus_z(self, stm, sim_ccd, ters_env):
        z0 = stm.z
        res = _nearfar(make_ctx(stm), cycles=1)
        assert res.success, res.error
        zs = _zset(stm)
        assert zs[0] == pytest.approx(z0 + 20e-12, abs=2e-12)          # the probe comes first
        assert max(zs) == pytest.approx(z0 + 5e-9, abs=2e-12) and min(zs) >= z0
        assert pytest.approx(z0 + 0.5e-9, abs=2e-12) in zs             # park on the retract side
        probe = res.data["analysis"]["retract_probes"][0]
        assert probe["passed"] and probe["retract_dir"] == 1
        assert probe["drop_frac"] == pytest.approx(1 - math.exp(-2 * KAPPA_PER_M * 20e-12), abs=0.05)

    def test_plus_one_retracts_and_parks_on_minus_z(self, stm, sim_ccd, ters_env):
        from mast.core import instrument_profile

        stm.extend_sign = +1
        instrument_profile.set_profile({"z_extend_sign": "+1"})
        z0 = stm.z
        res = _nearfar(make_ctx(stm), cycles=1)
        assert res.success, res.error
        zs = _zset(stm)
        assert zs[0] == pytest.approx(z0 - 20e-12, abs=2e-12)
        assert min(zs) == pytest.approx(z0 - 5e-9, abs=2e-12) and max(zs) <= z0
        assert pytest.approx(z0 - 0.5e-9, abs=2e-12) in zs
        assert stm.fb and abs(stm.z - z0) < 1e-12
        zt = _nearfar(make_ctx(stm), dry_run=True, laser_state="", cycles=1).data["plan"]["z_targets_m"]
        assert zt["retract_dir"] == -1
        assert zt["z_ff"] == pytest.approx(zt["z_nf"] - 5e-9)
        assert zt["z_return"] == pytest.approx(zt["z_nf"] - 0.5e-9)

    def test_current_rising_after_the_probe_stops_before_any_nm_step(self, stm, sim_ccd, ters_env):
        stm.extend_sign = +1          # the rig is +1, the profile says -1: the probe goes INTO the gap
        z0 = stm.z
        res = _nearfar(make_ctx(stm), cycles=1)
        assert not res.success and res.data["status"] == "needs_operator"
        assert "z_extend_sign 可能填反" in res.error and "反而上升" in res.error
        assert _zset(stm) == [pytest.approx(z0 + 20e-12, abs=2e-12)]   # the probe and nothing more
        probe = res.data["analysis"]["retract_probes"][0]
        assert not probe["passed"] and probe["i_after_a"] > probe["i_before_a"]
        assert stm.withdrawn, "SafeRetract withdrew the tip"
        assert [p["key"] for p in res.data["points"]] == ["c0_NF"]

    def test_current_ceiling_stops_the_ramp_mid_way(self, stm, sim_ccd, ters_env):
        z0 = stm.z
        orig = stm.current

        def bumped():
            if not stm.fb and stm.z > z0 + 2e-9:
                return 10 * stm.setpoint               # the current comes back up out there
            return orig()
        stm.current = bumped
        res = _nearfar(make_ctx(stm), cycles=1)
        assert not res.success and res.data["status"] == "needs_operator"
        assert "撤针途中电流" in res.error and stm.withdrawn
        assert max(_zset(stm)) < z0 + 4e-9                 # stopped at the first step over the ceiling
        assert [p["key"] for p in res.data["points"]] == ["c0_NF"]


# ── M10 ──────────────────────────────────────────────────────────────────────

PP_FAST = {**FAST, "delay_start_ps": -2.0, "delay_stop_ps": 2.0, "points": 5,
           "settle_extra_s": 0.0, "pixel_margin_s": 0.0, "exposure_ms": 20.0,
           "samples_per_point": 2, "sample_interval_s": 0.0, "stm_check_every": 0,
           "trigger_line_verified": True, "trigger_level_verified": True, "pdxc2_trigger_unwired": True}


def _dio_writes(stm) -> list:
    return [w for w in stm.writes if str(w[0]).startswith("DigLines")]


@pytest.fixture
def pp_bench(tmp_path, stm):
    from mast.instruments.registry import reset_instrument_registry

    reg = write_bench(tmp_path, [dict(SIM_CCD), dict(DELAY_STAGE)], delay_line=dict(DELAY_LINE))
    trig = TriggeredSim(reg.spectrometer("sim_ccd"))
    stm.on_pulse = trig.trigger
    yield reg, trig
    reset_instrument_registry(None)


def _pp(ctx, **kw):
    return ctx.run("PumpProbeSpectral", {**PP_FAST, **kw})


class TestPumpProbeSpectral:
    def test_ttl_sweep_one_frame_per_trigger_reconciled(self, stm, pp_bench, ters_env):
        reg, trig = pp_bench
        res = _pp(make_ctx(stm), bands_cm1="si:500-540", directions="both")
        assert res.success, res.error
        d = res.data
        assert d["status"] == "done" and d["points_done"] == 10
        rec = d["analysis"]["reconciliation"]
        assert rec["status"] == "ok" and rec["triggers"] == rec["frames_received"] == 10
        assert rec["frame_tracking_ok"] is True
        # every pulse on Port B line 2, 5 ms wide by default
        assert len(stm.pulses) == 10 and all(p[0] == 1 and p[1] == [2] for p in stm.pulses)
        # one triggered acquisition: dark (NoResponse) + the N-frame sequence
        cfg = [a for a in stm.writes if a[0] == "DigLines_PropsSet"]
        assert cfg and cfg[0][1] == (2, 1, 1, 1)
        files = d["preflight"] if False else None
        side = Path(d["points"][0]["sidecar"])
        meta = json.loads(side.read_text(encoding="utf-8"))
        assert meta["schema"] == "mast.spectrum/1" and meta["triggered"] is True
        assert len(meta["extra"]["triggers"]) == 10
        assert meta["reconciliation"]["status"] == "ok"
        arr = np.load(d["points"][0]["file"])
        assert arr.shape[0] == 11                          # axis + 10 frames
        # the delay line went back to the start
        dl = reg.delay_line()
        assert dl.get_delay_ps() == pytest.approx(-2.0, abs=1e-6)
        main = f"local/pump_probe/{d['run_id']}"
        xs = [f["x"] for f in ters_env.rec.frames(main)]
        assert xs[:5] == pytest.approx([-2, -1, 0, 1, 2]) and xs[5:] == pytest.approx([2, 1, 0, -1, -2])
        curve = d["analysis"]["curves"]["band_si_fwd"]
        assert curve["delay_ps"] == pytest.approx([-2, -1, 0, 1, 2])

    def test_lost_trigger_is_a_count_mismatch_never_patched(self, stm, pp_bench, ters_env):
        reg, trig = pp_bench
        trig.drop = {2}
        res = _pp(make_ctx(stm))
        assert not res.success and "count_mismatch" in res.error
        st = [p["status"] for p in res.data["points"]]
        assert st[:2] == ["ok", "ok"] and len(st) == 2
        assert res.data["qc_counts"]["count_mismatch"] >= 1
        assert len(stm.pulses) == 3, "no pulse is ever re-sent"

    def test_port_a_is_refused(self, stm, pp_bench, ters_env):
        """Port A carries the PMD4 coarse controller: refused at the schema (min 1) for real and dry runs
        alike, and again by ttl_refusals should anything get past the schema."""
        from mast.skills.composite.ters_common import ttl_refusals

        for extra in ({}, {"dry_run": True, "laser_state": ""},
                      {"dry_run": True, "dry_run_motion": True, "laser_state": ""}):
            res = _pp(make_ctx(stm), trigger_port=0, **extra)
            assert not res.success, extra
        assert not stm.pulses and not _dio_writes(stm)
        for dry in (True, False):
            assert any("Port A" in r for r in ttl_refusals({"trigger_port": 0}, dry_run=dry))

    def test_ttl_needs_the_operator_confirmations(self, stm, pp_bench, ters_env):
        for flag in ("trigger_line_verified", "trigger_level_verified", "pdxc2_trigger_unwired"):
            res = _pp(make_ctx(stm), **{flag: False})
            assert not res.success and flag in res.error, flag
        assert not stm.pulses and not _dio_writes(stm)

    def test_unverified_line_numbering_allows_only_dry_run(self, stm, pp_bench, ters_env):
        """Until the DigLines numbering is checked on the instrument, S+TTL defaults to dry_run only: a real
        run is refused, a dry run is allowed and its plan says what a real run still needs."""
        defaults = {"trigger_line_verified": False, "trigger_level_verified": False,
                    "pdxc2_trigger_unwired": False}          # the parameters' defaults
        res = _pp(make_ctx(stm), **defaults)
        assert not res.success and "trigger_line_verified" in res.error
        res = _pp(make_ctx(stm), dry_run=True, laser_state="", **defaults)
        assert res.success, res.error
        needs = " ".join(res.data["plan"]["real_run_needs"])
        assert "trigger_line_verified" in needs and "pdxc2_trigger_unwired" in needs
        assert not stm.pulses and not _dio_writes(stm)

    def test_delays_outside_the_stage_are_refused(self, stm, pp_bench, ters_env):
        res = _pp(make_ctx(stm), delay_stop_ps=500.0)
        assert not res.success and "超出可达范围" in res.error

    def test_no_motion_inside_an_exposure_window(self, stm, pp_bench, ters_env):
        """Every DelayLineMoveTo happened between a frame's arrival and the next pulse."""
        reg, trig = pp_bench
        dl = reg.delay_line()
        events = []
        orig_move = dl.axis.move_abs

        def logged(*a, **k):
            events.append(("move", trig.n_trig))
            return orig_move(*a, **k)
        dl.axis.move_abs = logged
        orig_trig = trig.trigger

        def tr(*a):
            events.append(("pulse", trig.n_trig))
            orig_trig(*a)
        stm.on_pulse = tr
        res = _pp(make_ctx(stm))
        assert res.success, res.error
        # moves alternate with pulses: never two pulses without the frame/move in between,
        # never a move while a pulse's frame is still outstanding
        kinds = [e[0] for e in events]
        assert kinds[0] == "move" and kinds.count("pulse") == 5
        for a, b in zip(kinds, kinds[1:]):
            assert not (a == "pulse" and b == "pulse")

    def test_software_acquire_scheme(self, stm, pp_bench, ters_env):
        res = _pp(make_ctx(stm), trigger_scheme="software_acquire", readout="both",
                  trigger_level_verified=False, pdxc2_trigger_unwired=False)
        assert res.success, res.error
        assert not stm.pulses
        assert len([p for p in res.data["points"] if p["status"] == "ok"]) == 5

    def test_electrical_readout_is_delegated_to_pump_probe_scan(self, stm, pp_bench, ters_env):
        res = _pp(make_ctx(stm), readout="electrical", directions="both")
        assert res.success, res.error
        subs = res.data["analysis"]["sweeps"]
        assert [s["direction"] for s in subs] == ["fwd", "bwd"]
        assert all(s["success"] and s["path"] for s in subs)

    def test_abort_stops_the_triggered_sequence(self, stm, pp_bench, ters_env):
        reg, trig = pp_bench
        ev = threading.Event()
        orig = trig.trigger

        def press_on_third(*a):
            orig(*a)
            if trig.n_trig == 3:
                ev.set()
        stm.on_pulse = press_on_third
        res = _pp(make_ctx(stm, abort=ev), points=8)
        assert not res.success and res.data["status"] == "partial"
        assert len(stm.pulses) == 3
        # no return-to-start after an abort
        assert reg.delay_line().get_delay_ps() != pytest.approx(-2.0)

    def test_dry_run_motion_walks_the_delays_without_pulsing(self, stm, pp_bench, ters_env,
                                                             monkeypatch):
        """Same moves, same judging cadence and the same return to start as a real run; the DIO line
        is never configured and nothing is pulsed or acquired."""
        from mast.skills.composite import ters_common as tc

        reg, trig = pp_bench
        judged = []
        orig = tc.TersRun.tip_ready

        def counting(self, **kw):
            judged.append(kw.get("label"))
            return orig(self, **kw)
        monkeypatch.setattr(tc.TersRun, "tip_ready", counting)
        res = _pp(make_ctx(stm), dry_run=True, dry_run_motion=True, laser_state="",
                  trigger_line_verified=False, trigger_level_verified=False, stm_check_every=2)
        assert res.success, res.error
        assert res.data["plan"]["reached_delays_ps"] == pytest.approx([-2, -1, 0, 1, 2])
        assert [j for j in judged if j and j[0] == "d" and j[1:].isdigit()] == ["d0", "d2", "d4"]
        assert reg.delay_line().get_delay_ps() == pytest.approx(-2.0, abs=1e-6)   # back to start
        assert not stm.pulses and not _dio_writes(stm)
        assert reg.spectrometer("sim_ccd").acquisitions == 0

    def test_plan_only_dry_run_does_not_move(self, stm, pp_bench, ters_env):
        reg, trig = pp_bench
        before = reg.delay_line().get_delay_ps()
        res = _pp(make_ctx(stm), dry_run=True, laser_state="", trigger_level_verified=False)
        assert res.success, res.error
        assert reg.delay_line().get_delay_ps() == before and not stm.pulses
