"""Polariser / alignment / AOTF / PMT skills, their registration and gating, and
the LiveHub view of the pre-existing optical scans (contract §7, §8.2)."""
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

import pytest

from mast.core.types import NanonisCallRecord, SafetyLevel
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.instruments.remote import set_hub_provider
from mast.live.testing import LiveRecorder, isolated_live_hub

from tests.v2.instruments.fake_hub import FakeSatelliteHub

NEW_SKILLS = ("ListSatellites", "GetSpectrometerStatus", "ConfigureSpectrometer",
              "SetSpectrometerGrating", "SetSpectrometerCenter", "SetSpectrometerExitPort",
              "AcquireSpectrum", "StopSpectrometer", "StopSatelliteDevices",
              "GetPolarizerAngle", "SetPolarizerAngle", "HomePolarizer",
              "GetPicomotorSteps", "PicomotorJog", "MoveXYZStage",
              "ListNanonisMotorGroups", "NanonisMotorGroupMove",
              "SetAotfWavelength", "ReadPmt", "SetPmtHighVoltage")

#: contract §8.2 safety table
EXPECTED_SAFETY = {
    **{n: SafetyLevel.AUTO for n in ("ListSatellites", "GetSpectrometerStatus",
                                     "GetPolarizerAngle", "GetPicomotorSteps",
                                     "ListNanonisMotorGroups", "StopSpectrometer",
                                     "StopSatelliteDevices")},
    **{n: SafetyLevel.CONFIRM for n in ("ConfigureSpectrometer", "SetSpectrometerGrating",
                                        "SetSpectrometerCenter", "SetSpectrometerExitPort",
                                        "AcquireSpectrum", "SetPolarizerAngle", "HomePolarizer",
                                        "PicomotorJog", "MoveXYZStage", "NanonisMotorGroupMove",
                                        "SetAotfWavelength", "ReadPmt")},
    "SetPmtHighVoltage": SafetyLevel.DANGEROUS,
}


@pytest.fixture(scope="module")
def metas():
    from mast.core.registry import SkillRegistry

    reg = SkillRegistry()
    reg.discover("mast.skills.builtins")
    return {n: reg.get(n)().metadata() for n in NEW_SKILLS}


# ── registration, gating, packs ─────────────────────────────────────────────

def test_safety_levels_follow_the_contract(metas):
    assert {n: m.safety_level for n, m in metas.items()} == EXPECTED_SAFETY


def test_every_new_skill_is_in_the_optics_pack_and_the_module(metas):
    from mast.agents._shared.tool_packs import classify
    from mast.skills import hardware_modules as hm

    for name, m in metas.items():
        assert "optics" in m.tags and "optional-hardware" in m.tags, name
        assert "optics" in classify(name, m), name
        assert hm.SKILL_OWNER.get(name) == "ters_optics", name
    assert not hm.MODULE_BY_ID["ters_optics"].default_on


def test_stops_bypass_the_token_and_writes_take_it(metas):
    from mast.core.instrument_lock import BYPASS_NAMES, needs_token

    for name, m in metas.items():
        if name.startswith("Stop"):
            assert name in BYPASS_NAMES and not needs_token(m, name)
        elif m.category.value == "write":
            assert needs_token(m, name), name
    assert "StopOpticalStage" in BYPASS_NAMES


def test_every_float_parameter_with_a_unit_has_bounds(metas):
    """ParameterSpec with units and limits written out."""
    for name, m in metas.items():
        for p in m.parameters:
            if p.type == "float":
                assert p.unit, f"{name}.{p.name} has no unit"
                assert p.min_value is not None and p.max_value is not None, f"{name}.{p.name}"


def test_new_skills_survive_the_frozen_build_import_path():
    import mast.skills.builtins as b

    for n in NEW_SKILLS:
        assert n in b.__all__ and hasattr(b, n), n


# ── polariser / alignment on simulated and remote devices ───────────────────

@pytest.fixture
def bench(tmp_path):
    hub = FakeSatelliteHub()
    hub.add_device("rig1", "pico", "openloop_stepper")
    hub.add_device("rig1", "xyz", "xyz_stage")
    hub.add_device("rig1", "aotf", "aotf", not_supported={"set"})
    hub.add_device("rig1", "pmt", "pmt")
    set_hub_provider(lambda: hub)
    p = tmp_path / "oi.json"
    p.write_text(json.dumps({"devices": [
        {"id": "pol", "type": "sim_rotation", "speed_deg_s": 1e5, "wrap": True,
         "axes": [{"name": "angle", "channel": 1, "min_pos": -360, "max_pos": 720,
                   "unit": "deg", "role": "polarizer"}]},
        {"id": "pico", "type": "satellite_openloop", "satellite": "rig1", "device": "pico",
         "max_steps_per_cmd": 500,
         "axes": [{"name": "2", "channel": 2, "min_pos": -5000, "max_pos": 5000,
                   "unit": "steps", "role": "beam_steer"}]},
        {"id": "xyz", "type": "satellite_motion", "satellite": "rig1", "device": "xyz",
         "role": "focus_xyz", "max_step_mm": 0.5,
         "axes": [{"name": a, "channel": a, "min_pos": lo, "max_pos": hi, "unit": "mm"}
                  for a, lo, hi in (("x", -2, 13.3), ("y", 0, 5), ("z", 0, 9.6))]},
    ], "delay_line": None}), encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)
    yield hub, reg
    set_hub_provider(None)
    reset_instrument_registry(None)


class TestPolariser:
    def test_unhomed_mount_is_refused_until_homed(self, bench):
        from mast.skills.builtins.optics_polarization import (
            GetPolarizerAngle,
            HomePolarizer,
            SetPolarizerAngle,
        )

        r = SetPolarizerAngle().execute(None, {"angle_deg": 45.0})
        assert not r.success and "HomePolarizer" in r.error
        assert HomePolarizer().execute(None, {"timeout_s": 5.0}).success
        r = SetPolarizerAngle().execute(None, {"angle_deg": 45.0})
        assert r.success and r.data["angle_deg"] == pytest.approx(45.0)
        # 800° is out of range; with wrap the in-range equivalent nearest the
        # REQUEST is used (440°, not 80°) — shortest_path is off in this config
        r = SetPolarizerAngle().execute(None, {"angle_deg": 800.0})
        assert r.success and r.data["angle_deg"] == pytest.approx(440.0)
        r = SetPolarizerAngle().execute(None, {"angle_deg": 300.0, "relative": True})
        assert not r.success and "soft limit" in r.error                 # 740° > 720°
        r = GetPolarizerAngle().execute(None, {})
        assert r.success and r.data["homed"] is True and r.data["limits_deg"] == [-360.0, 720.0]


class TestAlignment:
    def test_picomotor_jog_and_counter(self, bench):
        from mast.skills.builtins.optics_alignment import GetPicomotorSteps, PicomotorJog

        r = PicomotorJog().execute(None, {"axis": "2", "steps": 300})
        assert r.success, r.error
        assert r.data["counter_before"] == 0 and r.data["counter_after"] == 300
        assert not PicomotorJog().execute(None, {"axis": "2", "steps": 501}).success
        r = GetPicomotorSteps().execute(None, {})
        assert r.success and r.data["axes"]["2"]["steps"] == 300 and r.data["feedback"] is False

    def test_xyz_moves_each_given_axis(self, bench):
        from mast.skills.builtins.optics_alignment import MoveXYZStage

        r = MoveXYZStage().execute(None, {"x": 5.3, "z": 4.2})
        assert r.success, r.error
        assert set(r.data["moved"]) == {"x", "z"}
        assert r.data["moved"]["x"]["end_mm"] == pytest.approx(5.3)
        r = MoveXYZStage().execute(None, {"y": 0.3, "relative": True})
        assert r.success and r.data["moved"]["y"]["end_mm"] == pytest.approx(2.8)
        r = MoveXYZStage().execute(None, {"y": 4.9})                     # 2.1 mm step
        assert not r.success and "per-command cap" in r.error
        assert not MoveXYZStage().execute(None, {}).success


class TestAotfPmt:
    def test_aotf_not_supported_says_so_plainly(self, bench):
        from mast.skills.builtins.optics_aotf_pmt import SetAotfWavelength

        r = SetAotfWavelength().execute(None, {"wavelength_nm": 633.0})
        assert not r.success and r.data["remote_code"] == "not_supported"
        assert "不要重试" in r.error
        assert not SetAotfWavelength().execute(None, {"wavelength_nm": 1.0,
                                                      "frequency_mhz": 2.0}).success

    def test_pmt_read_and_high_voltage(self, bench):
        from mast.skills.builtins.optics_aotf_pmt import ReadPmt, SetPmtHighVoltage

        hub, _ = bench
        assert ReadPmt().execute(None, {"integration_ms": 50.0}).success
        assert SetPmtHighVoltage().execute(None, {"volts": 650.0}).success
        assert SetPmtHighVoltage().execute(None, {"volts": 0.0}).success
        actions = [c[2] for c in hub.calls if c[1] == "pmt"]
        assert actions == ["read", "set_hv", "hv_off"]

    def test_absent_device_is_unavailable(self, bench):
        from mast.skills.builtins.optics_aotf_pmt import ReadPmt

        hub, _ = bench
        hub.devs.pop(("rig1", "pmt"))
        r = ReadPmt().execute(None, {})
        assert not r.success and "unavailable" in r.error


# ── LiveHub for the pre-existing scans (strict LiveHub) ─────────────────────

class _Ctx:
    def __init__(self):
        self.calls = []

    def safe_call(self, method, *args, **kw):
        self.calls.append(method)
        if method in ("Current_Get", "Signals_ValGet"):
            return NanonisCallRecord(method=method, return_value=("", b"", [1.5e-9]))
        return NanonisCallRecord(method=method, return_value=("", b"", []))

    def check_abort(self):
        return False


@pytest.fixture
def stage_bench(tmp_path, monkeypatch):
    from mast.skills.builtins import optics_pump_probe as opp
    from mast.skills.builtins import optics_scan as osc

    monkeypatch.setattr(osc, "project_root", lambda: tmp_path)
    monkeypatch.setattr(opp, "project_root", lambda: tmp_path)
    p = tmp_path / "oi.json"
    # a simulated mount without wrap is a plain linear axis — a stand-in stage
    p.write_text(json.dumps({"devices": [
        {"id": "s", "type": "sim_rotation", "speed_deg_s": 1e9,
         "axes": [{"name": "a", "channel": 1, "min_pos": -1e5, "max_pos": 1e5, "unit": "um"},
                  {"name": "b", "channel": 2, "min_pos": -1e5, "max_pos": 1e5, "unit": "um"}]},
    ], "delay_line": {"device_id": "s", "axis": "a", "unit_per_mm": 1000.0,
                      "ps_per_mm": 6.6713, "zero_offset_mm": 0.0, "sign": 1}}),
        encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)
    yield reg
    reset_instrument_registry(None)


_FAST = {"samples_per_point": 1, "sample_interval_s": 0.0, "settle_extra_s": 0.0}


def test_stage_scan_1d_publishes_xy_and_2d_publishes_map2d(stage_bench):
    from mast.skills.builtins.optics_scan import OpticalStageScan

    with isolated_live_hub() as hub, LiveRecorder(hub) as rec:
        r1 = OpticalStageScan().execute(_Ctx(), {"device1": "s", "axis1": "a", "start1": 0,
                                                 "stop1": 40, "points1": 3, **_FAST})
        r2 = OpticalStageScan().execute(_Ctx(), {
            "device1": "s", "axis1": "a", "start1": 0, "stop1": 20, "points1": 3,
            "device2": "s", "axis2": "b", "start2": 0, "stop2": 10, "points2": 2, **_FAST})
    assert r1.success and r2.success, (r1.error, r2.error)
    f1 = rec.frames(r1.data["live_stream"])
    assert [f["x"] for f in f1] == [0.0, 20.0, 40.0] and "current_a" in f1[0]["values"]
    f2 = rec.frames(r2.data["live_stream"])
    assert sorted((f["ix"], f["iy"]) for f in f2) == [(i, j) for i in range(3) for j in range(2)]
    # serpentine: the second row is visited right-to-left but lands at the physical column
    assert [f["ix"] for f in f2 if f["iy"] == 1] == [2, 1, 0]
    assert rec.ended() == [r1.data["live_stream"], r2.data["live_stream"]]


def test_pump_probe_publishes_an_xy_curve_and_the_csv_is_unchanged(stage_bench):
    from mast.skills.builtins.optics_pump_probe import PumpProbeScan

    with isolated_live_hub() as hub, LiveRecorder(hub) as rec:
        r = PumpProbeScan().execute(_Ctx(), {"delay_start_ps": 0.0, "delay_stop_ps": 1.0,
                                             "points": 3, "read_lockin": False, **_FAST})
    assert r.success, r.error
    frames = rec.frames(r.data["live_stream"])
    assert len(frames) == 3 and frames[0]["values"]["current_a"] == pytest.approx(1.5e-9)
    assert Path(r.data["path"]).is_file()


class _AbortAfter(_Ctx):
    """Abort latches once *n* points have been measured (counted by Current_Get)."""

    def __init__(self, n):
        super().__init__()
        self.n = n

    def check_abort(self):
        return self.calls.count("Current_Get") >= self.n


def test_an_aborted_scan_does_not_return_to_start(stage_bench):
    """Contract §10.3: no new motion once the operator has aborted — the stage
    stays where the stop left it, and the result says so."""
    from mast.skills.builtins.optics_pump_probe import PumpProbeScan
    from mast.skills.builtins.optics_scan import OpticalStageScan

    ax = stage_bench.axis("s", "a")
    r = OpticalStageScan().execute(_AbortAfter(2), {"device1": "s", "axis1": "a", "start1": 0,
                                                    "stop1": 40, "points1": 5, **_FAST})
    assert not r.success and r.data["n_points"] == 2
    assert r.data["return_to_start"] == "skipped: aborted"
    assert ax.get_position() == pytest.approx(10.0)
    r = OpticalStageScan().execute(_Ctx(), {"device1": "s", "axis1": "a", "start1": 0,
                                            "stop1": 40, "points1": 3, **_FAST})
    assert r.success and r.data["return_to_start"] == "sent"
    r = PumpProbeScan().execute(_AbortAfter(2), {"delay_start_ps": 0.0, "delay_stop_ps": 1.0,
                                                 "points": 4, "read_lockin": False, **_FAST})
    assert not r.success and r.data["return_to_start"] == "skipped: aborted"


def test_scan_streams_carry_value_units(stage_bench):
    """frontend: map2d's y_unit is the y axis, so value units travel in meta.units."""
    from mast.skills.builtins import optics_scan as osc
    from mast.skills.builtins.optics_pump_probe import PumpProbeScan

    with isolated_live_hub() as hub, LiveRecorder(hub):
        r1 = osc.OpticalStageScan().execute(_Ctx(), {
            "device1": "s", "axis1": "a", "start1": 0, "stop1": 20, "points1": 2,
            "device2": "s", "axis2": "b", "start2": 0, "stop2": 10, "points2": 2,
            "signal_indices": "14", **_FAST})
        r2 = PumpProbeScan().execute(_Ctx(), {"delay_start_ps": 0.0, "delay_stop_ps": 1.0,
                                              "points": 2, "read_lockin": False, **_FAST})
        assert osc.AcquireSignalPoint().execute(_Ctx(), {"samples": 1}).success
        m1 = hub.describe(r1.data["live_stream"])["meta"]
        m2 = hub.describe(r2.data["live_stream"])["meta"]
        m3 = hub.describe(osc.SIGNAL_POINT_STREAM)["meta"]
    assert m1["units"] == {"current_a": "A"} and "sig14_mean" in m1["channels"]
    assert m2 == {"channels": ["current_a"], "units": {"current_a": "A"}}
    assert m3["units"] == {"current_a": "A"}


def test_dry_runs_publish_nothing(stage_bench):
    from mast.skills.builtins.optics_pump_probe import PumpProbeScan

    with isolated_live_hub() as hub, LiveRecorder(hub) as rec:
        r = PumpProbeScan().execute(_Ctx(), {"delay_start_ps": 0.0, "delay_stop_ps": 1.0,
                                             "points": 3, "dry_run": True, **_FAST})
    assert r.success and not rec.of("declare")


def test_signal_point_feeds_one_scalar_stream():
    from mast.skills.builtins import optics_scan as osc

    for _round in range(2):          # a fresh LiveHub each time: nothing cached across
        with isolated_live_hub() as hub, LiveRecorder(hub) as rec:
            for _ in range(2):
                assert osc.AcquireSignalPoint().execute(_Ctx(), {"samples": 1}).success
        assert len(rec.frames(osc.SIGNAL_POINT_STREAM)) == 2
        assert rec.seqs(osc.SIGNAL_POINT_STREAM) == [1, 2]
