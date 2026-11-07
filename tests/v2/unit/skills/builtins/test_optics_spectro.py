"""Spectrometer skills (contract §8.2, §10.2, §11.3).

AcquireSpectrum runs end to end against the in-process simulated spectrometer
and against the satellite hub double; LiveHub is the REAL one in strict mode
(a malformed frame raises), and files land in a temporary experiment folder.
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

import json
from types import SimpleNamespace

import numpy as np
import pytest

from mast.core.types import HardwareState, NanonisCallRecord
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.instruments.remote import set_hub_provider
from mast.live.testing import LiveRecorder, isolated_live_hub
from mast.logging import experiment_log as el
from mast.skills.builtins.optics_spectro import (
    AcquireSpectrum,
    ConfigureSpectrometer,
    GetSpectrometerStatus,
    ListSatellites,
    SetSpectrometerCenter,
    SetSpectrometerGrating,
    StopSatelliteDevices,
    StopSpectrometer,
)

from tests.v2.instruments.fake_hub import FakeSatelliteHub


class FakeStorage:
    def __init__(self):
        self.exp = {"e1": {"name": "TERS Au(111)", "start_time": "2026-10-19T20:00:00",
                           "dir_name": ""}}
        self.smp = {"s1": {"name": "Au tip A", "sample_index": 2, "dir_name": "",
                           "description": "", "sample_type": "", "sample_subtype": "",
                           "start_time": "2026-10-19T20:01:00"}}
        self.dir_writes: list = []

    def get_experiment(self, eid):
        return dict(self.exp[eid]) if eid in self.exp else None

    def get_sample(self, sid):
        return dict(self.smp[sid]) if sid in self.smp else None

    def sample_ordinal(self, sid):
        return 2

    def set_experiment_dir_name(self, eid, name):
        self.dir_writes.append(("exp", name))
        self.exp[eid]["dir_name"] = name

    def set_sample_dir_name(self, sid, name, idx):
        self.dir_writes.append(("smp", name))
        self.smp[sid]["dir_name"] = name

    def get_samples(self, eid):
        return [dict(v, id=k) for k, v in self.smp.items()]


@pytest.fixture
def scope(tmp_path, monkeypatch):
    """An active experiment/sample whose folders live under tmp."""
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    storage = FakeStorage()
    log = SimpleNamespace(_storage=storage, current_experiment_id="e1",
                          current_sample_id="s1")
    prev = el.get_active_log()
    el.set_active_log(log)
    yield SimpleNamespace(storage=storage, log=log, root=tmp_path)
    el.set_active_log(prev)


def _bench(tmp_path, devices):
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": devices, "delay_line": None}), encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)
    return reg


@pytest.fixture
def sim_bench(tmp_path):
    reg = _bench(tmp_path, [
        {"id": "sim_ccd", "type": "sim_spectrometer", "role": "raman_ccd",
         "laser_line_nm": 633.0, "time_scale": 0.0,
         "limits": {"max_exposure_ms": 5000, "max_frames": 10}},
        {"id": "sim_pol", "type": "sim_rotation",
         "axes": [{"name": "angle", "channel": 1, "min_pos": -360, "max_pos": 720,
                   "unit": "deg", "role": "polarizer"}]},
    ])
    yield reg
    reset_instrument_registry(None)


class Ctx:
    """safe_call + state + abort, enough for AcquireSpectrum's STM bookkeeping."""

    def __init__(self, *, current=1.2e-10, abort=False):
        self.calls = []
        self._i = current
        self._abort = abort
        snap = HardwareState(bias_v=0.5, current_a=current, z_pos_m=1e-9, x_pos_m=2e-9,
                             y_pos_m=3e-9, z_controller_on=True, setpoint_a=1e-10,
                             withdrawn=False, scan_running=False)
        self.refreshes = 0

        def refresh(_s):
            self.refreshes += 1
            return snap

        self.state = type("S", (), {"snapshot": lambda s: snap, "refresh": refresh})()

    def safe_call(self, method, *args, **kw):
        self.calls.append(method)
        if method == "Current_Get":
            return NanonisCallRecord(method=method, return_value=("", b"", [self._i]))
        return NanonisCallRecord(method=method, return_value=("", b"", []))

    def check_abort(self):
        return self._abort


# ── AcquireSpectrum (simulated spectrometer) ────────────────────────────────

class TestAcquireSpectrumSim:
    def test_files_metadata_and_live_frames(self, sim_bench, scope):
        ctx = Ctx()
        with isolated_live_hub() as hub, LiveRecorder(hub) as rec:
            res = AcquireSpectrum().execute(ctx, {
                "exposure_ms": 200.0, "frames": 3, "tag": "pt7",
                "meta_json": json.dumps({"step_id": "st-3", "ix": 3, "iy": 7}),
                "current_interval_s": 0.05})
        assert res.success, res.error
        d = res.data
        npy, side = Path(d["path"]), Path(d["sidecar_path"])
        # stored in the active sample's raw/other, staging copies removed
        assert d["stored_in_experiment"] is True
        assert "experiments" in npy.parts and npy.parent.name == "other"
        assert npy.is_file() and side.is_file()
        assert npy.name.endswith("_pt7.npy")
        arr = np.load(npy)
        assert arr.shape == (4, 1340)                       # axis row + 3 frames
        assert np.all(np.isfinite(arr[0]))
        meta = json.loads(side.read_text(encoding="utf-8"))
        assert meta["schema"] == "mast.spectrum/1"
        assert meta["files"]["array"]["sha256"] == d["sha256"]
        assert meta["stm"]["before"]["available"] and meta["stm"]["after"]["available"]
        assert meta["stm"]["before"]["bias_v"] == 0.5
        assert ctx.refreshes == 2                           # fresh read before AND after
        assert meta["stm"]["current_during_exposure"]["n"] >= 1
        assert meta["stm"]["current_during_exposure"]["mean_a"] == pytest.approx(1.2e-10)
        assert meta["scope"]["experiment_id"] == "e1" and meta["scope"]["step_id"] == "st-3"
        assert meta["extra"] == {"step_id": "st-3", "ix": 3, "iy": 7}
        assert meta["settings"]["laser_line_nm"] == 633.0
        assert meta["time"]["started_utc"].endswith("+00:00")
        assert meta["time"]["hlc_start"] and meta["time"]["hlc_end"] > meta["time"]["hlc_start"]
        assert meta["optics"]["polarizer"]["device_id"] == "sim_pol"
        assert "raman_shift_cm1" in meta["result"]["peak"]
        # top-level data must not carry HardwareState field names (state-cache patch)
        assert not {"current_a", "bias_v", "z_pos_m"} & set(d)
        # LiveHub: a local device streams every frame plus the final average
        frames = rec.frames("local/spectra/sim_ccd")
        assert len(frames) == 4 and frames[-1]["meta"]["final"] is True
        assert frames[0]["meta"]["ix"] == 3
        assert rec.of("axis", "local/spectra/sim_ccd")
        assert not rec.of("end", "local/spectra/sim_ccd"), "the per-device stream is rolling"
        assert not rec.overflowed

    def test_without_an_active_sample_the_files_stay_in_staging(self, sim_bench, tmp_path,
                                                                 monkeypatch):
        monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
        prev = el.get_active_log()
        el.set_active_log(None)
        try:
            res = AcquireSpectrum().execute(Ctx(), {"exposure_ms": 10.0, "frames": 1,
                                                    "sample_current": False})
        finally:
            el.set_active_log(prev)
        assert res.success and res.data["stored_in_experiment"] is False
        assert "artifacts" in Path(res.data["path"]).parts and Path(res.data["path"]).is_file()
        assert "没有选定样品" in res.summary

    def test_limits_are_refused_before_exposing(self, sim_bench, scope):
        res = AcquireSpectrum().execute(Ctx(), {"exposure_ms": 60000.0})
        assert not res.success and "spectrometer limit" in res.error
        assert sim_bench.spectrometer("sim_ccd").acquisitions == 0

    def test_dry_run_exposes_nothing(self, sim_bench, scope):
        res = AcquireSpectrum().execute(Ctx(), {"exposure_ms": 100.0, "dry_run": True})
        assert res.success and res.data["dry_run"]
        assert sim_bench.spectrometer("sim_ccd").acquisitions == 0
        assert "stm" in res.data and res.data["stm"]["before"]["available"]

    def test_refused_during_tip_work_and_approach(self, sim_bench, scope):
        from mast.core.instrument_lock import instrument_lock

        lk = instrument_lock()
        for holder, word in (("ConditionTip", "修针"), ("AutoApproach", "进针")):
            assert lk.acquire(owner="test", skill=holder, timeout_s=1.0)
            try:
                res = AcquireSpectrum().execute(Ctx(), {"exposure_ms": 10.0})
            finally:
                lk.release()
            assert not res.success and word in res.error
        assert sim_bench.spectrometer("sim_ccd").acquisitions == 0
        # the holder is the outermost skill: a TERS composite whose name merely
        # contains "Approach" is not an approach
        assert lk.acquire(owner="test", skill="TersApproachMap", timeout_s=1.0)
        try:
            assert AcquireSpectrum().execute(Ctx(), {"exposure_ms": 10.0,
                                                     "sample_current": False}).success
        finally:
            lk.release()

    def test_abort_latched_refuses_to_start(self, sim_bench, scope):
        res = AcquireSpectrum().execute(Ctx(abort=True), {"exposure_ms": 10.0})
        assert not res.success and "aborted" in res.error

    def test_bad_meta_json(self, sim_bench, scope):
        res = AcquireSpectrum().execute(Ctx(), {"meta_json": "[1,2]"})
        assert not res.success and "meta_json" in res.error

    def test_settings_skills(self, sim_bench):
        assert GetSpectrometerStatus().execute(None, {}).data["laser_line_nm"] == 633.0
        r = ConfigureSpectrometer().execute(None, {"exposure_ms": 500.0, "timestamps": True,
                                                   "trigger_response": "ReadoutPerTrigger",
                                                   "trigger_edge": "RisingEdge",
                                                   "roi": "line:50"})
        assert r.success, r.error
        req = r.data["requested"]
        assert req["timestamps"] == "ExposureStarted,ExposureEnded"
        assert req["trigger"] == {"response": "ReadoutPerTrigger", "edge": "RisingEdge"}
        assert req["roi"] == {"mode": "line", "rows": 50}
        assert not ConfigureSpectrometer().execute(None, {"exposure_ms": 9e6}).success
        assert not ConfigureSpectrometer().execute(None, {}).success
        assert SetSpectrometerCenter().execute(None, {"center_nm": 700.0}).success
        assert not SetSpectrometerCenter().execute(None, {"center_nm": 5000.0}).success
        assert SetSpectrometerGrating().execute(None, {"grating": "[500nm,600][1][0]"}).success

    def test_stop_spectrometer_needs_no_token_and_always_answers(self, sim_bench):
        from mast.core.instrument_lock import needs_token

        meta = StopSpectrometer().metadata()
        assert not needs_token(meta, meta.name)
        res = StopSpectrometer().execute(None, {})
        assert res.success and "sim_ccd" in res.data["stopped"]


# ── AcquireSpectrum on the satellite (hub double) ───────────────────────────

@pytest.fixture
def sat_bench(tmp_path):
    hub = FakeSatelliteHub()
    hub.add_device("rig1", "ccd", "spectrometer", n_px=64)
    hub.add_device("rig1", "pol", "rotation_stage")
    set_hub_provider(lambda: hub)
    reg = _bench(tmp_path, [
        {"id": "rig1_ccd", "type": "satellite_spectrometer", "satellite": "rig1",
         "device": "ccd", "role": "raman_ccd", "laser_line_nm": 633.0}])
    yield hub, reg
    set_hub_provider(None)
    reset_instrument_registry(None)


class TestAcquireSpectrumSatellite:
    def test_remote_acquisition_records_clock_and_cmd_and_publishes_only_the_result(
            self, sat_bench, scope):
        hub, _reg = sat_bench
        with isolated_live_hub() as live, LiveRecorder(live) as rec:
            res = AcquireSpectrum().execute(Ctx(), {"exposure_ms": 10.0, "frames": 2,
                                                    "sample_current": False})
        assert res.success, res.error
        meta = json.loads(Path(res.data["sidecar_path"]).read_text(encoding="utf-8"))
        assert meta["device"]["satellite"] == "rig1" and meta["device"]["cmd_id"]
        assert meta["time"]["clock_offset_ms"] == pytest.approx(2.5)
        assert meta["acquisition"]["frames_received"] == 2
        assert res.data["satellite_files"]                  # save_raw defaults to True
        acq = [c for c in hub.calls if c[2] == "acquire"][0]
        assert acq[3]["base_name"].startswith("mast_spec-") and acq[3]["save"] is True
        # raw satellite frames are the hub bridge's job; this stream gets the result
        frames = rec.frames("local/spectra/rig1_ccd")
        assert len(frames) == 1 and frames[0]["meta"]["final"] is True

    def test_list_and_stop_satellites(self, sat_bench):
        hub, _ = sat_bench
        r = ListSatellites().execute(None, {})
        assert r.success and r.data["hub_available"] and r.data["satellites"][0]["id"] == "rig1"
        assert r.data["registry_devices"][0]["id"] == "rig1_ccd"
        r = StopSatelliteDevices().execute(None, {"satellite": "rig1"})
        assert r.success and set(r.data["stopped"]) == {"rig1/ccd", "rig1/pol"}
        r = StopSatelliteDevices().execute(None, {"satellite": "ghost"})
        assert not r.success


def test_list_satellites_without_a_hub_is_degraded_not_empty(tmp_path):
    set_hub_provider(lambda: (_ for _ in ()).throw(RuntimeError("no hub")))
    try:
        r = ListSatellites().execute(None, {})
    finally:
        set_hub_provider(None)
    assert r.success and r.data["hub_available"] is False and "不等于没有卫星" in r.summary


# ── LightField vocabulary (contract §5.4) ──────────────────

_LF_API = (Path(__file__).resolve().parents[5] / "local-samples" / "team" / "lightfield" / "raw"
           / "api" / "api_LightFieldAddInSupportServices.txt")


def _lf_enum(text: str, name: str) -> set[str]:
    """Member names of ``enum PrincetonInstruments.LightField.AddIns.<name>`` in the
    API dump (one ``Member = value`` per indented line after the header)."""
    import re

    out: set[str] = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"enum PrincetonInstruments\.LightField\.AddIns\.{name}\b", line):
            for member in lines[i + 1:]:
                m = re.match(r"\s+(\w+)\s*=\s*-?\d+", member)
                if not m:
                    break
                out.add(m.group(1))
            break
    out.discard("UninitializedEnum")
    return out


def test_lightfield_option_names_are_pinned():
    """The names MAST sends — a change here is a contract §5.4 change, made on
    purpose and agreed with the satellite, not an edit in passing."""
    from mast.skills.builtins import optics_spectro as sp

    assert sp.TRIGGER_RESPONSES == ["NoResponse", "ReadoutPerTrigger", "ShiftPerTrigger",
                                    "ExposeDuringTriggerPulse", "StartOnSingleTrigger"]
    assert sp.TRIGGER_EDGES == ["RisingEdge", "FallingEdge"]
    assert sp.SHUTTER_MODES == ["Normal", "AlwaysOpen", "AlwaysClosed", "OpenBeforeTrigger"]
    assert sp.ADC_QUALITIES == ["LowNoise", "HighCapacity", "HighSpeed", "ElectronMultiplied"]
    assert sp.OUTPUT_SIGNALS == ["NotReadingOut", "ShutterOpen", "Busy", "AlwaysLow",
                                 "AlwaysHigh", "Acquiring", "ShiftingUnderMask", "Exposing",
                                 "EffectivelyExposing", "ReadingOut", "WaitingForTrigger"]
    # the parameters offer exactly these lists
    params = {p.name: p for p in sp.ConfigureSpectrometer().metadata().parameters}
    assert params["output_signal"].allowed_values == sp.OUTPUT_SIGNALS
    assert params["trigger_response"].allowed_values == sp.TRIGGER_RESPONSES


@pytest.mark.skipif(not _LF_API.is_file(), reason="LightField API dump not in this checkout")
def test_every_option_is_a_member_of_its_lightfield_enum():
    """Checked against LightField 6.18's own API (the API dump):
    "EffectiveExposure" was not a member — OutputSignal's name is
    "EffectivelyExposing" — and the C# satellite refused it with invalid_args."""
    from mast.skills.builtins import optics_spectro as sp

    text = _LF_API.read_text(encoding="utf-8", errors="replace")
    for enum, values in (("TriggerResponse", sp.TRIGGER_RESPONSES),
                         ("TriggerDetermination", sp.TRIGGER_EDGES),
                         ("ShutterTimingMode", sp.SHUTTER_MODES),
                         ("AdcQuality", sp.ADC_QUALITIES),
                         ("OutputSignal", sp.OUTPUT_SIGNALS),
                         ("TimeStamps", ["None", "ExposureStarted", "ExposureEnded"])):
        members = _lf_enum(text, enum)
        assert members, f"enum {enum} not found in the API dump"
        assert set(values) <= members, f"{enum}: not LightField names: {set(values) - members}"


# ── scope folders: same names as the runtime ────────────────────────────────

def test_scope_dirs_match_the_runtime_derivation(scope):
    """optics_common.resolve_scope_dirs mirrors CoreRuntime._ensure_scope_dirs
    without writing the database; both must name the same folders."""
    from mast.core.runtime import CoreRuntime
    from mast.skills.builtins.optics_common import resolve_scope_dirs

    mine = resolve_scope_dirs()
    assert scope.storage.dir_writes == [], "the skill side must not write dir names"
    stub = SimpleNamespace(_experiment_log=scope.log, _storage=scope.storage, _v2_eid=None)
    theirs = CoreRuntime._ensure_scope_dirs(stub)
    assert mine is not None and theirs is not None
    assert Path(mine[0]) == Path(theirs[0]) and mine[1] == theirs[1]
