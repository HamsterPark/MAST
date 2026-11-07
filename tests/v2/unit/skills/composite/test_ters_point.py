"""M01 TERSPointSpectrum —— 真 ExecutionContext + 假 STM + 本机模拟光谱仪 / 卫星枢纽替身。"""
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
import threading
import time

import numpy as np
import pytest

from tests.v2.unit.skills.composite._ters_fixtures import (  # noqa: F401 — fixtures
    FAST,
    ctx,
    scope,
    sim_ccd,
    stm,
    ters_env,
)
from tests.v2.unit.skills.composite._ters_harness import make_ctx


def _run(ctx, **params):
    return ctx.run("TERSPointSpectrum", {**FAST, **params})


class TestHappyPath:
    def test_two_repeats_land_in_the_experiment_with_full_metadata(self, ctx, stm, sim_ccd,
                                                                    ters_env):
        res = _run(ctx, repeats=2, band_cm1="500-540", tag="pt")
        assert res.success, res.error
        d = res.data
        assert d["status"] == "done" and d["points_done"] == 2
        # per-spectrum files went through AcquireSpectrum into the sample folder
        files = [p["file"] for p in d["points"]]
        assert all(Path(f).is_file() and "experiments" in Path(f).parts for f in files)
        side = json.loads(Path(files[0]).with_suffix(".json").read_text(encoding="utf-8"))
        extra = side["extra"]
        assert extra["mode"] == "M01" and extra["run_id"] == d["run_id"]
        assert extra["tip_ready"]["passed"] is True
        assert extra["laser"]["laser_state"] == "attested_on"
        assert extra["laser"]["attested_via"] == "direct_call"
        assert extra["spectrometer_config"]["trigger_response"] == "NoResponse"
        assert side["stm"]["before"]["available"] and side["stm"]["after"]["available"]
        # run-level products: index / npz / summary archived too
        prod = d["products"]
        assert prod["stored"] is True
        names = [Path(p).name for p in prod["paths"]]
        assert any(n.endswith("_index.csv") for n in names)
        npz = next(p for p in prod["paths"] if p.endswith(".npz"))
        arr = np.load(npz)
        assert arr["spectra"].shape[0] == 2 and "axis_cm1" in arr
        # the 520 cm⁻¹ line of the simulator is inside the band
        assert d["points"][0]["band_b1"] > 0
        assert d["analysis"]["band_intensity"]["b1"]["n"] == 2
        # LiveHub: main scalar trace + status, both ended
        rec = ters_env.rec
        main = f"local/ters_point/{d['run_id']}"
        assert main in rec.declared() and f"{main}/status" in rec.declared()
        assert rec.frames(main) and set(rec.ended()) >= {main, f"{main}/status"}
        # a dark frame was taken once and the shutter was put back
        assert d["dark"] and d["dark"]["path"]

    def test_checkpoint_written_per_point(self, ctx, stm, sim_ccd, ters_env):
        res = _run(ctx, repeats=2)
        assert res.success, res.error
        from mast.skills.composite.ters_common import RunStore

        cp = RunStore.load_checkpoint(res.data["run_id"])
        assert cp is not None and cp["status"] == "done"
        assert set(cp["points"]) == {"r0", "r1"}
        assert all(v["status"] == "ok" and v["sha256"] for v in cp["points"].values())


def _fb_writes(stm) -> list[bool]:
    return [bool(a[0]) for (m, a) in stm.writes if m == "ZCtrl_OnOffSet"]


class TestHoldMode:
    def test_hold_opens_the_loop_for_each_exposure_and_closes_it_again(self, ctx, stm, sim_ccd,
                                                                       ters_env):
        res = _run(ctx, repeats=2, hold_mode="hold")
        assert res.success, res.error
        assert _fb_writes(stm) == [False, True, False, True]
        assert stm.fb and res.data["unrestored"] == {}
        assert res.data["restored"].get("z_feedback")

    def test_nonzero_tip_lift_is_refused_before_anything_moves_dry_run_included(self, stm, sim_ccd,
                                                                                 ters_env):
        stm.tip_lift = 2e-10
        for extra in ({}, {"dry_run": True, "laser_state": ""}):
            res = _run(make_ctx(stm), hold_mode="hold", **extra)
            assert not res.success and "tip lift" in res.error, extra
            assert res.data["status"] == "refused"
        assert not _fb_writes(stm) and sim_ccd.spectrometer("sim_ccd").acquisitions == 0

    def test_dry_run_motion_rehearses_the_hold_without_exposing(self, stm, sim_ccd, ters_env):
        res = _run(make_ctx(stm), repeats=2, hold_mode="hold", dry_run=True, dry_run_motion=True,
                   laser_state="")
        assert res.success, res.error
        assert res.data["status"] == "dry_run"
        assert _fb_writes(stm) == [False, True, False, True] and stm.fb
        assert sim_ccd.spectrometer("sim_ccd").acquisitions == 0
