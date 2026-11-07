"""TERS 组合技能的登记、元数据形状，以及 M03（S+TTL）。"""
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

import numpy as np
import pytest

from mast.core.types import SafetyLevel, SkillCategory
from tests.v2.unit.skills.composite._ters_fixtures import (  # noqa: F401 — fixtures
    FAST,
    scope,
    stm,
    ters_env,
)
from tests.v2.unit.skills.composite._ters_harness import (
    SIM_CCD,
    TriggeredSim,
    make_ctx,
    skill_registry,
    write_bench,
)

TERS_SKILLS = ("TERSPointSpectrum", "TERSNearFarPair", "PumpProbeSpectral", "TERSLineProfile",
               "TERSGridMap", "TERSTriggeredMap", "TERSTimeSeries", "TERSPolarizationSeries",
               "TERSBiasSeries", "TERSSetpointSeries", "LaserFocusMap")


@pytest.fixture(scope="module")
def metas():
    reg = skill_registry()
    return {n: reg._get_metadata(reg.get(n)) for n in TERS_SKILLS}


def test_every_mode_is_registered_and_exported_for_the_frozen_build(metas):
    import mast.skills.composite as comp

    for n in TERS_SKILLS:
        assert n in comp.__all__ and hasattr(comp, n), n


def test_metadata_shape(metas):
    from mast.core.instrument_lock import needs_token
    from mast.core.sample_gate import requires_sample
    from mast.core.tip_intent import is_tip_work

    for n, m in metas.items():
        assert m.safety_level == SafetyLevel.CONFIRM, n
        assert m.category == SkillCategory.COMPOSITE and m.composition_level == 3, n
        assert "optics" in m.tags and "optional-hardware" in m.tags, n
        assert not is_tip_work(n), f"{n}: a tip-work substring makes AcquireSpectrum refuse"
        assert n not in ("AutoApproach", "ApproachTip")
        assert needs_token(m, n), n
        assert requires_sample(m, n), f"{n} produces data and must be sample-gated"
        names = [p.name for p in m.parameters]
        assert len(names) == len(set(names)), f"{n}: duplicate parameter names"
        assert {"dry_run", "dry_run_motion", "laser_attest", "resume_run_id", "max_total_s"} <= set(names)


def test_every_float_parameter_with_a_unit_has_bounds(metas):
    for n, m in metas.items():
        for p in m.parameters:
            if p.type == "float" and p.unit:
                assert p.min_value is not None and p.max_value is not None, f"{n}.{p.name}"


def test_inf_defaults_are_marked_for_the_experts(metas):
    m = metas["TERSPointSpectrum"]
    by = {p.name: p for p in m.parameters}
    for name in ("exposure_ms", "settle_s", "stable_tol_frac", "laser_attest", "drift_tol_pm"):
        assert "可调" in by[name].description, name
    assert by["exposure_ms"].default == 2000.0 and by["stable_window_s"].default == 1.0


def test_mode_specific_default_overrides(metas):
    by = {n: {p.name: p for p in m.parameters} for n, m in metas.items()}
    assert by["TERSGridMap"]["exposure_ms"].default == 1000.0
    assert by["TERSGridMap"]["settle_s"].default == 0.3
    assert by["TERSGridMap"]["stable_timeout_s"].default == 10.0
    assert by["PumpProbeSpectral"]["exposure_ms"].default == 100.0
    assert by["PumpProbeSpectral"]["shutter_mode"].default == "AlwaysOpen"
    assert by["TERSTimeSeries"]["shutter_mode"].default == "AlwaysOpen"
    assert "frames" not in by["TERSTimeSeries"]
    assert by["TERSBiasSeries"]["stable_window_s"].default == 1.0
    assert by["TERSSetpointSeries"]["settle_s"].default == 3.0
    assert by["TERSTriggeredMap"]["shutter_mode"].default == "AlwaysOpen"
    assert by["TERSNearFarPair"]["bands_cm1"].required is True


# ── M03 S+TTL ────────────────────────────────────────────────────────────────

@pytest.fixture
def ttl_bench(tmp_path, stm):
    from mast.instruments.registry import reset_instrument_registry

    reg = write_bench(tmp_path, [dict(SIM_CCD)])
    trig = TriggeredSim(reg.spectrometer("sim_ccd"))
    stm.on_pulse = trig.trigger
    yield reg, trig
    reset_instrument_registry(None)


TTL_FAST = {**FAST, "pre_post_scan": False, "nx": 3, "ny": 2, "width_m": 3e-9, "height_m": 2e-9,
            "exposure_ms": 20.0, "pixel_margin_s": 0.0, "trigger_line_verified": True,
            "trigger_level_verified": True, "bands_cm1": "si:500-540"}


def _dio_writes(stm) -> list:
    return [w for w in stm.writes if str(w[0]).startswith("DigLines")]


def test_triggered_map_one_pulse_per_pixel(stm, ttl_bench, ters_env):
    reg, trig = ttl_bench
    res = make_ctx(stm).run("TERSTriggeredMap", {**TTL_FAST, "reference_frames": 1})
    assert res.success, res.error
    d = res.data
    rec = d["analysis"]["reconciliation"]
    assert rec["status"] == "ok" and rec["triggers"] == rec["frames_received"] == 7
    assert len(stm.pulses) == 7 and all(p[:2] == (1, [2]) for p in stm.pulses)
    assert d["analysis"]["map"]["valid"] == 6
    main = f"local/ters_map/{d['run_id']}"
    assert {(f["ix"], f["iy"]) for f in ters_env.rec.frames(main)} == {
        (i, j) for i in range(3) for j in range(2)}
    assert ters_env.rec.frames(f"{main}/trig")
    meta = json.loads(Path(d["points"][0]["sidecar"]).read_text(encoding="utf-8"))
    assert meta["triggered"] and len(meta["extra"]["triggers"]) == 7


def test_triggered_acquisition_time_limit_covers_the_trigger_plan(stm, ttl_bench, ters_env):
    """The device's default limit is free-running (exposure × frames); with external triggers every
    pixel may spend its whole judging timeout before its pulse, so the limit handed to the satellite
    must cover n × (stable_timeout + period) or the hub cancels the sequence half way."""
    res = make_ctx(stm).run("TERSTriggeredMap", {**TTL_FAST, "stable_timeout_s": 30.0})
    assert res.success, res.error
    meta = json.loads(Path(res.data["points"][0]["sidecar"]).read_text(encoding="utf-8"))
    req = meta["settings"]["requested"]
    assert req["frames"] == 6 and req["timeout_s"] >= 6 * (30.0 + 0.02)
    assert req["per_trigger_s"] >= 30.0


def test_triggered_map_lost_trigger_marks_the_rest_invalid(stm, ttl_bench, ters_env):
    reg, trig = ttl_bench
    trig.drop = {3}
    res = make_ctx(stm).run("TERSTriggeredMap", {**TTL_FAST})
    assert not res.success and "count_mismatch" in res.error
    st = [p["status"] for p in res.data["points"]]
    assert st[:3] == ["ok", "ok", "ok"] and "ok" not in st[3:]


def test_triggered_map_refuses_h_level_port_a_and_unverified_lines(stm, ttl_bench, ters_env):
    for bad, word in (({"trigger_scheme": "grid_ttl_sync"}, "H 级"),
                      ({"trigger_line_verified": False}, "trigger_line_verified"),
                      ({"trigger_level_verified": False}, "trigger_level_verified")):
        res = make_ctx(stm).run("TERSTriggeredMap", {**TTL_FAST, **bad})
        assert not res.success and word in res.error, bad
    # Port A (PMD4) is outside the schema, for real and rehearsed runs alike
    for extra in ({}, {"dry_run": True, "laser_state": ""},
                  {"dry_run": True, "dry_run_motion": True, "laser_state": ""}):
        res = make_ctx(stm).run("TERSTriggeredMap", {**TTL_FAST, "trigger_port": 0, **extra})
        assert not res.success, extra
    assert not stm.pulses and not _dio_writes(stm)


def test_triggered_map_dry_run_motion_walks_and_judges_without_touching_the_dio(
        stm, ttl_bench, ters_env, monkeypatch):
    """Unverified line numbering ⇒ dry_run only. The rehearsal walks the reference point and every
    pixel with the real judging policy; the DIO line is not configured, nothing is pulsed or acquired."""
    from mast.skills.composite import ters_common as tc

    reg, trig = ttl_bench
    judged = []
    orig = tc.TersRun.ready_or_policy

    def counting(self, **kw):
        judged.append(kw.get("key"))
        return orig(self, **kw)
    monkeypatch.setattr(tc.TersRun, "ready_or_policy", counting)
    params = {**TTL_FAST, "trigger_line_verified": False, "trigger_level_verified": False,
              "reference_frames": 1, "laser_state": ""}
    res = make_ctx(stm).run("TERSTriggeredMap", params)
    assert not res.success and "trigger_line_verified" in res.error
    res = make_ctx(stm).run("TERSTriggeredMap", {**params, "dry_run": True, "dry_run_motion": True})
    assert res.success, res.error
    assert judged == ["ref0"] + [f"px_{i}_{j}" for j, row in enumerate([range(3), range(2, -1, -1)])
                                 for i in row]
    moves = [w for w in stm.writes if w[0] == "FolMe_XYPosSet"]
    assert len(moves) == 7
    assert "trigger_line_verified" in " ".join(res.data["plan"]["real_run_needs"])
    assert not stm.pulses and not _dio_writes(stm)
    assert reg.spectrometer("sim_ccd").acquisitions == 0


def test_triggered_map_unstable_pixel_gets_no_trigger(stm, ttl_bench, ters_env, monkeypatch):
    from mast.skills.composite import ters_common as tc

    orig = tc.TersRun.tip_ready
    n = {"k": 0}

    def flaky(self, **kw):
        n["k"] += 1
        if n["k"] == 2:
            return tc.TipReady(False, "timeout:z_drifting")
        return orig(self, **kw)
    monkeypatch.setattr(tc.TersRun, "tip_ready", flaky)
    res = make_ctx(stm).run("TERSTriggeredMap", {**TTL_FAST})
    assert res.success, res.error
    assert len(stm.pulses) == 5 and res.data["analysis"]["reconciliation"]["status"] == "ok"
