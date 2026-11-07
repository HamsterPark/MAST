"""P1 模式：M02（线 / 网格）、M06、M07、M08、M09、M12（栅格）。"""
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

import math
import threading
import time

import numpy as np
import pytest

from mast.skills.composite import ters_common as tc
from tests.v2.unit.skills.composite._ters_fixtures import (  # noqa: F401 — fixtures
    FAST,
    scope,
    sim_ccd,
    stm,
    ters_env,
)
from tests.v2.unit.skills.composite._ters_harness import (
    KAPPA_PER_M,
    SIM_CCD,
    make_ctx,
    write_bench,
)

MAP_FAST = {**FAST, "pre_post_scan": False, "reference_every_n": 0}


# ── M02 ──────────────────────────────────────────────────────────────────────

class TestGridMap:
    def test_3x3_serpentine_builds_the_map(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSGridMap", {**MAP_FAST, "nx": 3, "ny": 3, "width_m": 3e-9,
                                                "height_m": 3e-9, "bands_cm1": "si:500-540"})
        assert res.success, res.error
        d = res.data
        assert d["points_done"] == 9
        keys = [p["key"] for p in d["points"]]
        assert keys == ["px_0_0", "px_1_0", "px_2_0", "px_2_1", "px_1_1", "px_0_1",
                        "px_0_2", "px_1_2", "px_2_2"]
        moves = [a[:2] for (m, a) in stm.writes if m == "FolMe_XYPosSet"]
        assert moves[0] == pytest.approx((-1e-9, -1e-9)) and moves[2] == pytest.approx((1e-9, -1e-9))
        main = f"local/ters_map/{d['run_id']}"
        decl = [e for e in ters_env.rec.of("declare") if e["stream"] == main]
        assert decl and ters_env.rec.frames(main)
        assert {(f["ix"], f["iy"]) for f in ters_env.rec.frames(main)} == {
            (i, j) for i in range(3) for j in range(3)}
        assert all("si" in f["values"] for f in ters_env.rec.frames(main))
        npz = next(p for p in d["products"]["paths"] if p.endswith(".npz"))
        arr = np.load(npz)
        assert arr["value_si"].shape == (3, 3) and np.isfinite(arr["value_si"]).all()
        assert arr["pixel_spectra"].shape[:2] == (3, 3)

    def test_reference_pixels_and_tip_changed_stop(self, stm, sim_ccd, ters_env):
        sim = sim_ccd.spectrometer("sim_ccd")
        orig = sim._spectrum
        n = {"k": 0}

        def changing(exp):
            n["k"] += 1
            y = orig(exp)
            if n["k"] > 4:          # after dark + ref0 + 2 pixels the "tip" changes
                y = y[::-1].copy()
            return y
        sim._spectrum = changing
        res = make_ctx(stm).run("TERSGridMap", {**MAP_FAST, "nx": 2, "ny": 2, "width_m": 2e-9,
                                                "height_m": 2e-9, "bands_cm1": "si:480-560",
                                                "reference_every_n": 2})
        assert not res.success and res.data["status"] == "needs_operator"
        assert "tip_changed" in res.error
        assert res.data["qc_counts"]["tip_changed"] == 1

    def test_reference_fail_flag_only(self, stm, sim_ccd, ters_env):
        sim = sim_ccd.spectrometer("sim_ccd")
        orig = sim._spectrum
        n = {"k": 0}

        def changing(exp):
            n["k"] += 1
            y = orig(exp)
            return y[::-1].copy() if n["k"] > 4 else y
        sim._spectrum = changing
        res = make_ctx(stm).run("TERSGridMap", {**MAP_FAST, "nx": 2, "ny": 2, "width_m": 2e-9,
                                                "height_m": 2e-9, "bands_cm1": "si:480-560",
                                                "reference_every_n": 2, "on_reference_fail": "flag"})
        assert res.success, res.error
        assert res.data["qc_counts"]["tip_changed"] == 1 and res.data["points_done"] >= 4

    def test_out_of_range_pixels_and_unimplemented_drift_are_refused(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSGridMap", {**MAP_FAST, "center_x_m": 1.49e-6, "width_m": 1e-7})
        assert not res.success and "MoveToXY 范围" in res.error
        res = make_ctx(stm).run("TERSGridMap", {**MAP_FAST, "drift_mode": "atom_track"})
        assert not res.success and "drift_mode" in res.error
        res = make_ctx(stm).run("TERSGridMap", {**MAP_FAST, "nx": 64, "ny": 64, "max_pixels": 100})
        assert not res.success and "max_pixels" in res.error
        assert not [w for w in stm.writes if w[0] == "FolMe_XYPosSet"]

    def test_empty_pixels_over_ten_percent_pause(self, stm, sim_ccd, ters_env, monkeypatch):
        orig = tc.TersRun.tip_ready
        calls = {"n": 0}

        def flaky(self, **kw):
            calls["n"] += 1
            if calls["n"] % 3 == 0:
                return tc.TipReady(False, "timeout:current_noisy")
            return orig(self, **kw)
        monkeypatch.setattr(tc.TersRun, "tip_ready", flaky)
        res = make_ctx(stm).run("TERSGridMap", {**MAP_FAST, "nx": 4, "ny": 4, "width_m": 4e-9,
                                                "height_m": 4e-9})
        assert not res.success and "空像素" in res.error
        assert res.data["qc_counts"]["unstable_tip"] >= 2

    def test_dry_run_motion_rehearses_every_pixel_and_the_hold(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSGridMap", {**MAP_FAST, "nx": 2, "ny": 2, "hold_mode": "hold",
                                                "dry_run": True, "dry_run_motion": True,
                                                "laser_state": ""})
        assert res.success, res.error
        fb = [bool(a[0]) for (m, a) in stm.writes if m == "ZCtrl_OnOffSet"]
        assert fb == [False, True] * 4 and stm.fb
        assert len([w for w in stm.writes if w[0] == "FolMe_XYPosSet"]) == 4
        assert sim_ccd.spectrometer("sim_ccd").acquisitions == 0

    def test_dry_run_plans_pixels_without_moving(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSGridMap", {**MAP_FAST, "dry_run": True, "laser_state": "",
                                                "nx": 4, "ny": 2})
        assert res.success, res.error
        assert res.data["plan"]["pixels"] == 8 and len(res.data["plan"]["first_pixels"]) == 5
        assert not [w for w in stm.writes if w[0] == "FolMe_XYPosSet"]

    def test_pre_post_topography_and_drift_report_are_called(self, stm, sim_ccd, ters_env,
                                                             monkeypatch):
        from mast.skills.composite import ters_map

        seen = []

        def topo(self, run, which):
            seen.append(which)
            return {"path": f"C:/fake/{which}.sxm"}
            yield  # noqa: B901

        def drift(self, run, pre, post):
            seen.append(("drift", pre["path"], post["path"]))
            run.analysis["drift_report"] = {"available": True}
            return
            yield  # noqa: B901
        monkeypatch.setattr(ters_map._TersMapBase, "_topo", topo)
        monkeypatch.setattr(ters_map._TersMapBase, "_drift_report", drift)
        res = make_ctx(stm).run("TERSGridMap", {**MAP_FAST, "nx": 2, "ny": 2,
                                                "pre_post_scan": True})
        assert res.success, res.error
        assert seen == ["pre", "post", ("drift", "C:/fake/pre.sxm", "C:/fake/post.sxm")]

    @pytest.mark.parametrize("autosave, calls", [
        ("fresh", ["ScanAt"]),            # Nanonis autosaved the frame: use it, no second copy
        ("none", ["ScanAt", "SaveScan"]),  # autosave off / session dir not visible
        ("stale", ["ScanAt", "SaveScan"]),  # only an older frame there: not this scan's
    ])
    def test_topography_frame_is_saved_once(self, tmp_path, monkeypatch, autosave, calls):
        """ScanAt's ConfigureScan sets autosave=All, and the SaveScan after it stored
        every pre/post frame twice. The autosaved file is the frame; SaveScan only without one."""
        import os

        from mast.core import scan_registry
        from mast.core.types import SkillResult
        from mast.skills.composite.ters_map import TERSGridMap

        monkeypatch.setattr(scan_registry, "known_scan_dirs", lambda: [tmp_path])
        old = tmp_path / "old_0001.sxm"
        old.write_bytes(b"old")
        os.utime(old, (time.time() - 600, time.time() - 600))
        saved = tmp_path / "frame_0002.sxm"

        class Run:
            p = {"width_m": 3e-9, "height_m": 3e-9, "angle_deg": 0.0, "scan_pixels": 64,
                 "scan_line_time_s": 0.05}
            plan = {"origin_m": [0.0, 0.0]}

            def __init__(self):
                self.facts, self.warnings, self.calls = {}, [], []

            def note_change(self, *a, **k):
                pass

            def stop(self, *a):
                raise AssertionError(a)

            def step(self, phase, skill, params, **kw):
                self.calls.append(skill)
                if skill == "ScanAt" and autosave == "fresh":
                    saved.write_bytes(b"frame")          # what autosave=All does at scan end
                data = {"saved_path": "C:/s/savescan_0003.sxm"} if skill == "SaveScan" else {}
                return SkillResult(skill_name=skill, success=True, data=data)
                yield  # noqa: B901

        run = Run()
        gen = TERSGridMap()._topo(run, "pre")
        with pytest.raises(StopIteration) as stop:
            while True:
                next(gen)
        want = str(saved) if autosave == "fresh" else "C:/s/savescan_0003.sxm"
        assert stop.value.value == {"path": want}
        assert run.calls == calls and not run.warnings
        assert run.facts["topo_pre"]["saved_by"] == ("autosave" if autosave == "fresh" else "SaveScan")


class TestLineProfile:
    def test_line_points_and_distance_axis(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSLineProfile", {**MAP_FAST, "x0_m": 0.0, "y0_m": 0.0,
                                                    "x1_m": 3e-9, "y1_m": 4e-9, "points": 3})
        assert res.success, res.error
        moves = [a[:2] for (m, a) in stm.writes if m == "FolMe_XYPosSet"]
        assert moves == [pytest.approx((0.0, 0.0)), pytest.approx((1.5e-9, 2e-9)),
                         pytest.approx((3e-9, 4e-9))]
        xs = [f["x"] for f in ters_env.rec.frames(f"local/ters_map/{res.data['run_id']}")]
        assert xs == pytest.approx([0.0, 2.5, 5.0])

    def test_degenerate_line_refused(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSLineProfile", {**MAP_FAST, "x0_m": 1e-9, "y0_m": 1e-9,
                                                    "x1_m": 1e-9, "y1_m": 1e-9})
        assert not res.success and "重合" in res.error


# ── M06 ──────────────────────────────────────────────────────────────────────

BIAS_FAST = {**FAST, "bias_settle_s": 0.0, "ref_every_n": 0}


class TestBiasSeries:
    def test_constant_resistance_scales_the_setpoint_and_restores(self, stm, sim_ccd, ters_env):
        v0, i0 = stm.bias, stm.setpoint
        res = make_ctx(stm).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "0.4,0.6",
                                                   "ref_every_n": 1})
        assert res.success, res.error
        d = res.data
        pts = {p["key"]: p for p in d["points"]}
        assert {"ref0", "v0", "v1", "ref1"} <= set(pts) or {"ref0", "v0", "v1"} <= set(pts)
        sps = [a[0] for (m, a) in stm.writes if m == "ZCtrl_SetpntSet"]
        r0 = v0 / i0
        assert sps[0] == pytest.approx(0.4 / r0) and pytest.approx(0.6 / r0) in sps
        # back where it started
        assert stm.bias == pytest.approx(v0) and stm.setpoint == pytest.approx(i0)
        assert d["unrestored"] == {}
        xs = [f["x"] for f in ters_env.rec.frames(f"local/ters_bias/{d['run_id']}")]
        assert xs == pytest.approx([0.4, 0.6])

    def test_over_cutoff_is_refused_without_the_laser_off_control(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "1.0,2.0"})
        assert not res.success and "STML" in res.error
        res = make_ctx(stm).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "1.0,2.0",
                                                   "allow_over_cutoff": True})
        assert not res.success and "laser_off_pass" in res.error

    def test_abort_in_constant_height_withdraws_instead_of_hovering(self, stm, sim_ccd, ters_env,
                                                                    monkeypatch):
        """Aborted with the loop open (constant_height): ZControllerOnOff is refused after an abort, so
        the tip is withdrawn — as M01/M02's hold and M04's retract do; bias and setpoint stay put."""
        ev = threading.Event()
        orig = tc.TersRun.tip_ready
        calls = {"n": 0}

        def stop_at_v0(self, **kw):
            calls["n"] += 1
            if calls["n"] == 2:            # ref0 is judged with the loop closed, v0 with it open
                ev.set()
                return tc.TipReady(False, "aborted")
            return orig(self, **kw)
        monkeypatch.setattr(tc.TersRun, "tip_ready", stop_at_v0)
        v0 = stm.bias
        res = make_ctx(stm, abort=ev).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "0.4,0.6",
                                                              "hold_mode": "constant_height"})
        d = res.data
        assert d["status"] == "partial" and "aborted" in d["reason"]
        assert [w for w in stm.writes if w[0] == "ZCtrl_Withdraw"] and stm.withdrawn
        assert d["withdraws"] and d["withdraws"][0]["sent"]
        assert "constant_height" in d["withdraws"][0]["why"]
        assert [bool(a[0]) for (m, a) in stm.writes if m == "ZCtrl_OnOffSet"] == [False]
        # nothing put back: the bias stays at the first series value, reported with its original
        assert stm.bias == pytest.approx(0.4)
        assert d["unrestored"]["bias_v"]["original"] == pytest.approx(v0)
        assert "z_feedback" in d["unrestored"] and d["restored"] == {}

    def test_abort_with_the_loop_closed_does_not_withdraw(self, stm, sim_ccd, ters_env, monkeypatch):
        """The withdraw is for a loop this run opened: constant_resistance keeps it closed, so an
        abort there leaves the tip in feedback (nothing restored, nothing extra done)."""
        ev = threading.Event()
        orig = tc.TersRun.tip_ready
        calls = {"n": 0}

        def stop_at_v0(self, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                ev.set()
                return tc.TipReady(False, "aborted")
            return orig(self, **kw)
        monkeypatch.setattr(tc.TersRun, "tip_ready", stop_at_v0)
        res = make_ctx(stm, abort=ev).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "0.4,0.6"})
        assert res.data["status"] == "partial"
        assert res.data["withdraws"] == [] and not stm.withdrawn and stm.fb
        assert not [w for w in stm.writes if w[0] in ("ZCtrl_Withdraw", "ZCtrl_OnOffSet")]

    def test_constant_height_recloses_the_loop_at_the_end_and_does_not_withdraw(self, stm, sim_ccd,
                                                                                ters_env):
        v0 = stm.bias
        res = make_ctx(stm).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "0.4,0.6",
                                                   "hold_mode": "constant_height"})
        assert res.success, res.error
        assert [bool(a[0]) for (m, a) in stm.writes if m == "ZCtrl_OnOffSet"] == [False, True]
        assert stm.fb and not stm.withdrawn
        assert not [w for w in stm.writes if w[0] == "ZCtrl_Withdraw"]
        assert res.data["withdraws"] == [] and res.data["unrestored"] == {}
        assert stm.bias == pytest.approx(v0)

    def test_dry_run_plan_shows_which_setpoints_were_clipped(self, stm, sim_ccd, ters_env):
        # R0 = 0.5 V / 100 pA = 5 GΩ: 0.06 V wants 12 pA, below the 20 pA floor → no longer constant R
        res = make_ctx(stm).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "0.06,0.4",
                                                   "min_current_a": 2e-11, "dry_run": True})
        assert res.success, res.error
        plan = res.data["plan"]
        assert plan["setpoints_a"] == pytest.approx([2e-11, 8e-11])
        assert plan["clipped_at_bias_v"] == [0.06]
        assert not [m for m, _ in stm.writes if m in ("Bias_Set", "ZCtrl_SetpntSet")]

    def test_deadband_points_are_skipped(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "0.02,0.4"})
        assert res.success, res.error
        assert any("死区" in w for w in res.data["warnings"])
        assert res.data["plan"]["biases_v"] == [0.4]

    def test_guard_trip_returns_to_v0_and_stops(self, stm, sim_ccd, ters_env):
        from tests.v2.unit.skills.composite.test_ters_common import during_point_acquire

        sim_ccd.spectrometer("sim_ccd")._scale = 1.0
        v0 = stm.bias

        def spike():
            import time

            now = time.monotonic()
            stm.spikes.append((now, now + 0.5, 1e-8))
        during_point_acquire(sim_ccd, spike, delay=0.2, nth=2)
        res = make_ctx(stm).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "0.4,0.6",
                                                   "exposure_ms": 1000.0})
        assert not res.success and res.data["status"] == "needs_operator"
        assert stm.bias == pytest.approx(v0)

    def test_random_order_is_seeded(self, stm, sim_ccd, ters_env):
        a = make_ctx(stm).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "0.2,0.3,0.4,0.5",
                                                 "order": "random", "shuffle_seed": 7,
                                                 "dry_run": True, "laser_state": ""})
        b = make_ctx(stm).run("TERSBiasSeries", {**BIAS_FAST, "biases_v": "0.2,0.3,0.4,0.5",
                                                 "order": "random", "shuffle_seed": 7,
                                                 "dry_run": True, "laser_state": ""})
        assert a.data["plan"]["biases_v"] == b.data["plan"]["biases_v"]
        assert sorted(a.data["plan"]["biases_v"]) == [0.2, 0.3, 0.4, 0.5]


# ── M07 ──────────────────────────────────────────────────────────────────────

class TestSetpointSeries:
    def test_up_down_with_intermediates_and_kappa(self, stm, sim_ccd, ters_env):
        i0 = stm.setpoint
        res = make_ctx(stm).run("TERSSetpointSeries", {**FAST, "setpoints_a": "1e-10,3e-10,9e-10",
                                                       "settle_s": 0.0, "order": "up_down",
                                                       "max_step_ratio": 3.0})
        assert res.success, res.error
        d = res.data
        sps = [a[0] for (m, a) in stm.writes if m == "ZCtrl_SetpntSet"]
        ratios = [b / a for a, b in zip([i0] + sps, sps)]
        assert max(max(r, 1 / r) for r in ratios) <= 3.0 + 1e-9, "no step above max_step_ratio"
        keys = [p["key"] for p in d["points"] if not p["key"].startswith("ref")]
        assert len(keys) == 5                                  # 1e-10, 3e-10, 9e-10, 3e-10, 1e-10
        fit = d["analysis"]["kappa_fit"]
        assert fit["valid"] and fit["kappa_per_nm"] == pytest.approx(KAPPA_PER_M * 1e-9, rel=0.05)
        assert stm.setpoint == pytest.approx(i0)

    def test_above_max_is_refused_not_clipped(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSSetpointSeries", {**FAST, "setpoints_a": "1e-10,5e-9"})
        assert not res.success and "不裁剪" in res.error
        assert not [w for w in stm.writes if w[0] == "ZCtrl_SetpntSet"]


# ── M08 ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def pol_bench(tmp_path):
    from mast.instruments.registry import reset_instrument_registry

    reg = write_bench(tmp_path, [dict(SIM_CCD),
                                 {"id": "pol", "type": "sim_rotation", "speed_deg_s": 1e5,
                                  "axes": [{"name": "angle", "channel": 1, "min_pos": -360,
                                            "max_pos": 720, "unit": "deg", "role": "polarizer"}]}])
    yield reg
    reset_instrument_registry(None)


POL_FAST = {**FAST, "angle_start_deg": 0.0, "angle_stop_deg": 90.0, "angle_step_deg": 45.0,
            "settle_after_move_s": 0.2}


class TestPolarization:
    def test_unidirectional_series_fit_and_return_check(self, stm, pol_bench, ters_env):
        pol_bench.controller("pol").axis("angle").home(wait=True, timeout=5)
        ax = pol_bench.controller("pol").axis("angle")
        targets = []
        orig = ax.move_abs

        def logged(pos, **kw):
            targets.append(round(float(pos), 6))
            return orig(pos, **kw)
        ax.move_abs = logged
        sim = pol_bench.spectrometer("sim_ccd")
        base = sim._spectrum

        def polarised(exp):
            th = ax.get_position()
            return base(exp) * (0.2 + math.cos(math.radians(th - 30)) ** 2)
        sim._spectrum = polarised
        res = make_ctx(stm).run("TERSPolarizationSeries", {
            **POL_FAST, "angle_step_deg": 15.0, "angle_stop_deg": 180.0,
            "bands_cm1": "si:500-540"})
        assert res.success, res.error
        # every target approached from below: θ−3° then θ
        pairs = list(zip(targets[::2], targets[1::2]))
        assert all(b - a == pytest.approx(3.0) for a, b in pairs)
        fit = res.data["analysis"]["polar_fit"]["si"]
        assert fit["valid"] and fit["theta0_deg"] == pytest.approx(30.0, abs=3.0)
        assert "return_check_rel" in res.data["analysis"]

    def test_unhomed_mount_needs_the_operator(self, stm, pol_bench, ters_env):
        res = make_ctx(stm).run("TERSPolarizationSeries", {**POL_FAST})
        assert not res.success and "回零" in res.error
        res = make_ctx(stm).run("TERSPolarizationSeries", {**POL_FAST, "ensure_homed": False})
        assert not res.success and "homed" in res.error

    def test_hitl_confirmed_homing(self, stm, pol_bench, ters_env, monkeypatch):
        routes = iter([{"route": "laser_on"}, {"route": "home"}])
        monkeypatch.setattr(tc, "human_channel", lambda payload: next(routes))
        res = make_ctx(stm).run("TERSPolarizationSeries", {**POL_FAST, "laser_state": ""})
        assert res.success, res.error
        assert pol_bench.controller("pol").axis("angle").get_status().homed is True

    def test_ff_reference_normalisation_is_refused(self, stm, pol_bench, ters_env):
        res = make_ctx(stm).run("TERSPolarizationSeries", {**POL_FAST, "normalise": "ff_reference"})
        assert not res.success and "ff_reference" in res.error


# ── M09 ──────────────────────────────────────────────────────────────────────

class TestTimeSeries:
    def test_blocks_frames_and_live_trace(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSTimeSeries", {**FAST, "n_frames": 6, "block_frames": 3,
                                                   "band_cm1": "500-540"})
        assert res.success, res.error
        ts = res.data["analysis"]["time_series"]
        assert ts["frames"] == 6 and ts["blocks"] == 2 and ts["gaps"] == [3]
        main = f"local/ters_time/{res.data['run_id']}"
        live = [f for f in ters_env.rec.frames(main) if "band" in f["values"]]
        assert len(live) == 6, "per-frame band values are published while acquiring"
        npz = next(p for p in res.data["products"]["paths"] if p.endswith(".npz"))
        arr = np.load(npz)
        assert arr["frame_value"].shape == (6,) and arr["gap_mask"].tolist() == [0, 0, 0, 1, 0, 0]

    def test_signal_drop_stops_at_the_block_boundary(self, stm, sim_ccd, ters_env):
        sim = sim_ccd.spectrometer("sim_ccd")
        orig = sim._spectrum
        n = {"k": 0}

        def fading(exp):
            n["k"] += 1
            y = orig(exp)
            return y * (0.1 if n["k"] > 8 else 1.0)
        sim._spectrum = fading
        res = make_ctx(stm).run("TERSTimeSeries", {**FAST, "n_frames": 30, "block_frames": 10,
                                                   "band_cm1": "500-540", "signal_drop_frac": 0.5,
                                                   "drop_frames": 3, "stop_on_drop": True})
        assert not res.success and "信号下降" in res.error
        assert res.data["analysis"]["time_series"]["frames"] == 10   # the block was kept, not lost

    def test_atom_tracking_between_blocks_is_refused(self, stm, sim_ccd, ters_env):
        res = make_ctx(stm).run("TERSTimeSeries", {**FAST, "tracking_every_blocks": 1,
                                                   "block_frames": 5})
        assert not res.success and "atom tracking" in res.error


# ── M12 ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def align_bench(tmp_path):
    from mast.instruments.registry import reset_instrument_registry

    reg = write_bench(tmp_path, [dict(SIM_CCD),
                                 {"id": "lens", "type": "sim_rotation", "speed_deg_s": 1e6,
                                  "axes": [{"name": "x", "channel": 1, "min_pos": 0.0,
                                            "max_pos": 10.0, "unit": "mm"},
                                           {"name": "y", "channel": 2, "min_pos": 0.0,
                                            "max_pos": 10.0, "unit": "mm"}]}])
    for a in ("x", "y"):
        reg.controller("lens").axis(a).move_abs(5.0, wait=True, timeout=5)
    yield reg
    reset_instrument_registry(None)


ALIGN_FAST = {**FAST, "axis1": "lens:x", "axis2": "lens:y", "span1": 1.0, "span2": 1.0,
              "n1": 3, "n2": 3, "repeats_per_eval": 1, "n_noise": 2,
              "tip_state_policy": "any", "max_noise_cv": 5.0}


class TestLaserFocusMap:
    def test_grid_finds_the_focus_and_stays_there(self, stm, align_bench, ters_env):
        sim = align_bench.spectrometer("sim_ccd")
        base = sim._spectrum
        ctrl = align_bench.controller("lens")

        def focus(exp):
            x, y = ctrl.axis("x").get_position(), ctrl.axis("y").get_position()
            return base(exp) * (0.1 + math.exp(-((x - 5.8) ** 2 + (y - 4.2) ** 2) / 0.5))
        sim._spectrum = focus
        res = make_ctx(stm).run("LaserFocusMap", {**ALIGN_FAST, "band_cm1": "500-540"})
        assert res.success, res.error
        an = res.data["analysis"]
        assert an["peak"]["valid"]
        assert (an["peak"]["grid_axis1"], an["peak"]["grid_axis2"]) == (6.0, 4.0)
        assert an["significant"] is True and not an.get("restored_to_start")
        assert ctrl.axis("x").get_position() == pytest.approx(6.0)
        main = f"local/align/{res.data['run_id']}"
        assert len(ters_env.rec.frames(main)) == 9

    def test_no_improvement_restores_the_start(self, stm, align_bench, ters_env):
        res = make_ctx(stm).run("LaserFocusMap", {**ALIGN_FAST, "objective": "current_mean",
                                                  "margin_sigma": 10.0})
        assert res.success, res.error
        assert res.data["analysis"]["restored_to_start"] is True
        ctrl = align_bench.controller("lens")
        assert ctrl.axis("x").get_position() == pytest.approx(5.0)
        assert ctrl.axis("y").get_position() == pytest.approx(5.0)

    def test_withdrawn_policy_refuses_a_tunnelling_tip(self, stm, align_bench, ters_env):
        res = make_ctx(stm).run("LaserFocusMap", {**ALIGN_FAST, "tip_state_policy": "withdrawn"})
        assert not res.success and "撤出" in res.error

    def test_feedback_on_ok_needs_tunnel_safe_axes(self, stm, align_bench, ters_env):
        res = make_ctx(stm).run("LaserFocusMap", {**ALIGN_FAST, "tip_state_policy": "feedback_on_ok"})
        assert not res.success and "tunnel_safe" in res.error

    def test_soft_limits_and_excursion(self, stm, align_bench, ters_env):
        res = make_ctx(stm).run("LaserFocusMap", {**ALIGN_FAST, "span1": 6.0})
        assert not res.success and "软限位" in res.error
        res = make_ctx(stm).run("LaserFocusMap", {**ALIGN_FAST, "max_excursion": "lens:x:0.5"})
        assert not res.success and "max_excursion" in res.error
        res = make_ctx(stm).run("LaserFocusMap", {**ALIGN_FAST, "algorithm": "hill_climb"})
        assert not res.success and "hill_climb" in res.error

    def test_abort_freezes_the_axes_without_restoring(self, stm, align_bench, ters_env):
        ev = threading.Event()
        sim = align_bench.spectrometer("sim_ccd")
        orig = sim._spectrum
        n = {"k": 0}

        def count(exp):
            n["k"] += 1
            if n["k"] == 5:
                ev.set()
            return orig(exp)
        sim._spectrum = count
        res = make_ctx(stm, abort=ev).run("LaserFocusMap", {**ALIGN_FAST})
        assert not res.success and res.data["status"] == "partial"
        ctrl = align_bench.controller("lens")
        assert (ctrl.axis("x").get_position(), ctrl.axis("y").get_position()) != (5.0, 5.0)
        # left where they were, and said so: each moved axis is unrestored with its start position
        un = res.data["unrestored"]
        assert un["axis:lens:x"]["original"] == pytest.approx(5.0)
        assert un["axis:lens:x"]["current"] == pytest.approx(ctrl.axis("x").get_position())
        assert "未还原" in res.error

    def test_dry_run_motion_walks_the_grid_and_puts_the_optics_back(self, stm, align_bench,
                                                                    ters_env):
        ctrl = align_bench.controller("lens")
        seen = set()
        orig = ctrl.axis("x").move_abs

        def logged(pos, *a, **k):
            seen.add(round(float(pos), 6))
            return orig(pos, *a, **k)
        ctrl.axis("x").move_abs = logged
        res = make_ctx(stm).run("LaserFocusMap", {**ALIGN_FAST, "dry_run": True, "dry_run_motion": True,
                                                  "laser_state": ""})
        assert res.success, res.error
        assert {4.0, 5.0, 6.0} <= seen                     # it really walked the grid
        assert (ctrl.axis("x").get_position(), ctrl.axis("y").get_position()) == (5.0, 5.0)
        an = res.data["analysis"]
        assert an["restored_to_start"] is True and "dry_run_motion" in an["restore_reason"]
        assert res.data["unrestored"] == {}
        assert align_bench.spectrometer("sim_ccd").acquisitions == 0

    def test_map_only_returns_to_the_start(self, stm, align_bench, ters_env):
        res = make_ctx(stm).run("LaserFocusMap", {**ALIGN_FAST, "objective": "current_mean",
                                                  "go_to_best": False})
        assert res.success, res.error
        ctrl = align_bench.controller("lens")
        assert (ctrl.axis("x").get_position(), ctrl.axis("y").get_position()) == (5.0, 5.0)
        assert "go_to_best" in res.data["analysis"]["restore_reason"]
        assert res.data["unrestored"] == {}

    def test_write_and_restore_motor_group_is_refused_up_front(self, stm, align_bench, ters_env,
                                                               monkeypatch):
        """Writing a group's drive is person-only (hard gate on the autonomous path): a composite
        must refuse such a group in preflight instead of failing on its first step."""
        from mast.instruments import motor_groups

        grp = motor_groups.MotorGroup(group=3, name="lens_xy", role="optics", axes={"X": "x"},
                                      max_steps_per_cmd=1000, chunk_steps=100, requires="withdrawn",
                                      freq_hz=1000.0, amp_v=100.0, drive_policy="write_and_restore")
        monkeypatch.setattr(motor_groups, "optical_group", lambda key, path=None: grp)
        res = make_ctx(stm).run("LaserFocusMap", {**ALIGN_FAST, "axis1": "nanonis:3:X", "axis2": "",
                                                  "objective": "current_mean"})
        assert not res.success and "drive_policy" in res.error
        assert not [w for w in stm.writes if str(w[0]).startswith("Motor")]


# ── dry_run_motion across the modes ─────────────────────────────────────────

@pytest.mark.parametrize("skill, params, bench", [
    ("TERSPointSpectrum", {"repeats": 2}, "sim_ccd"),
    ("TERSGridMap", {**MAP_FAST, "nx": 2, "ny": 2}, "sim_ccd"),
    ("TERSLineProfile", {**MAP_FAST, "x0_m": 0.0, "y0_m": 0.0, "x1_m": 2e-9, "y1_m": 0.0,
                         "points": 3}, "sim_ccd"),
    ("TERSBiasSeries", {**BIAS_FAST, "biases_v": "0.4,0.6"}, "sim_ccd"),
    ("TERSSetpointSeries", {"setpoints_a": "1e-10,3e-10", "settle_s": 0.0}, "sim_ccd"),
    ("TERSTimeSeries", {"n_frames": 4, "block_frames": 2}, "sim_ccd"),
    ("TERSPolarizationSeries", POL_FAST, "pol_bench"),
])
def test_dry_run_motion_runs_every_mode_end_to_end_without_exposing(
        stm, ters_env, request, skill, params, bench):
    """A rehearsal returns dry_run and status dry_run from every acquisition; no mode may take that
    for a failure (a mode must not bail out and withdraw at its first far-field point)."""
    reg = request.getfixturevalue(bench)
    if bench == "pol_bench":
        reg.controller("pol").axis("angle").home(wait=True, timeout=5)
    res = make_ctx(stm).run(skill, {**FAST, **params, "dry_run": True, "dry_run_motion": True,
                                    "laser_state": ""})
    assert res.success, res.error
    d = res.data
    assert d["status"] == "dry_run" and d["dry_run_motion"] is True
    assert reg.spectrometer("sim_ccd").acquisitions == 0
    assert not d["interlock"]["violations"]
    assert not [p for p in d["points"] if p.get("file")]
