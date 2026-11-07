"""Remote devices and the TERS skills over the REAL satellite hub and the Python
reference satellite (contract §6, §8.1, §8.2).

Same fixtures as ``tests/v2/satellite`` (hub with shortened timeouts, in-process
uvicorn on a random port, a real WebSocket, the reference satellite at 10×
speed). The unit tests in ``test_remote.py`` run against a double; these pin
that the double and the real thing agree where it matters: command ids that
tag spectrum frames, error codes, the cancel that an abort sends, homing.
Nothing here touches hardware or Nanonis.
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

import json
import time

import numpy as np
import pytest

from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.instruments.remote import set_hub_provider
from mast.live.testing import LiveRecorder, isolated_live_hub

# the hub's own fixtures, reused as-is (importing them registers them here)
from tests.v2.satellite.conftest import (  # noqa: F401
    hub,
    sat_root,
    server,
    sims,
    wait_for,
    write_sat_config,
)


def _bench(root: Path, *, xyz_max_step: float = 0.5) -> InstrumentRegistry:
    p = root / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "ccd", "type": "satellite_spectrometer", "satellite": "sim1", "device": "ccd",
         "role": "raman_ccd"},
        {"id": "pol", "type": "satellite_rotation", "satellite": "sim1", "device": "pol",
         "wrap": True,
         "axes": [{"name": "angle", "channel": 1, "min_pos": -360, "max_pos": 720,
                   "unit": "deg", "role": "polarizer"}]},
        {"id": "xyz", "type": "satellite_motion", "satellite": "sim1", "device": "xyz",
         "role": "focus_xyz", "max_step_mm": xyz_max_step,
         "axes": [{"name": a, "channel": a, "min_pos": lo, "max_pos": hi, "unit": "mm"}
                  for a, lo, hi in (("x", -2, 13.3), ("y", 0, 5), ("z", 0, 9.6))]},
        {"id": "pico", "type": "satellite_openloop", "satellite": "sim1", "device": "pico",
         "max_steps_per_cmd": 1000,
         "axes": [{"name": "2", "channel": 2, "min_pos": -20000, "max_pos": 20000,
                   "unit": "steps", "role": "beam_steer"}]},
    ], "delay_line": None}), encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)
    return reg


@pytest.fixture
def online(hub, server, sat_root, sims, tmp_path):
    """A connected reference satellite, the real hub as the process hub."""
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token, watch_dir=tmp_path / "watch")
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected", msg="connected")
    wait_for(lambda: hub.clock_offset_ms("sim1") is not None, msg="clock sample")
    set_hub_provider(None)        # get_hub() → the singleton the fixture installed
    yield sim
    reset_instrument_registry(None)


class _Ctx:
    """Just an abort predicate (no Nanonis): fires *after_s* after the first poll."""

    def __init__(self, after_s: float | None = None):
        self.after_s = after_s
        self._t0: float | None = None

    def check_abort(self) -> bool:
        if self.after_s is None:
            return False
        if self._t0 is None:
            self._t0 = time.monotonic()
        return time.monotonic() - self._t0 >= self.after_s


def test_spectrum_frames_are_matched_by_command_id_and_timed_on_the_hub_clock(online, sat_root):
    from mast.skills.builtins.optics_spectro import AcquireSpectrum

    _bench(sat_root)
    with isolated_live_hub() as live, LiveRecorder(live) as rec:
        res = AcquireSpectrum().execute(_Ctx(), {"exposure_ms": 100.0, "frames": 3,
                                                 "sample_current": False, "save_raw": False})
    assert res.success, res.error
    arr = np.load(res.data["path"])
    assert arr.shape == (4, 1340) and 630 < arr[0, 0] < arr[0, -1] < 700
    meta = json.loads(Path(res.data["sidecar_path"]).read_text(encoding="utf-8"))
    acq = meta["acquisition"]
    assert acq["frames_received"] == 3 and acq["missing_frames"] == []
    assert meta["device"]["satellite"] == "sim1" and meta["device"]["cmd_id"]
    assert meta["time"]["clock_offset_ms"] is not None
    assert abs(meta["time"]["clock_offset_ms"]) < 50          # same machine
    assert meta["time"]["started_utc"] <= meta["time"]["finished_utc"]
    assert len(rec.frames("local/spectra/ccd")) == 1          # the result; raw frames are the bridge's


def test_abort_mid_acquisition_cancels_on_the_satellite(online, hub, sat_root):
    from mast.skills.builtins.optics_spectro import AcquireSpectrum

    _bench(sat_root)
    t0 = time.monotonic()
    res = AcquireSpectrum().execute(_Ctx(after_s=0.4), {
        "exposure_ms": 2000.0, "frames": 20, "sample_current": False, "save_raw": False})
    took = time.monotonic() - t0
    assert not res.success and "abort" in res.error.lower(), res.error
    assert took < 3.0, f"abort took {took:.1f} s for a 4 s acquisition"
    wait_for(lambda: hub.call("sim1", "ccd", "status", timeout_s=5)["is_running"] is False,
             timeout=5, msg="satellite stopped exposing")


def test_polariser_must_home_then_moves(online, sat_root):
    from mast.skills.builtins.optics_polarization import (
        GetPolarizerAngle,
        HomePolarizer,
        SetPolarizerAngle,
    )

    _bench(sat_root)
    r = SetPolarizerAngle().execute(_Ctx(), {"angle_deg": 45.0})
    assert not r.success and "HomePolarizer" in r.error
    assert HomePolarizer().execute(_Ctx(), {"timeout_s": 20.0}).success
    r = SetPolarizerAngle().execute(_Ctx(), {"angle_deg": 45.0})
    assert r.success, r.error
    assert r.data["angle_deg"] == pytest.approx(45.0, abs=1e-3)
    r = GetPolarizerAngle().execute(_Ctx(), {})
    assert r.success and r.data["homed"] is True


def test_xyz_caps_on_both_sides_and_picomotor_counter(online, sat_root):
    from mast.skills.builtins.optics_alignment import GetPicomotorSteps, MoveXYZStage, PicomotorJog

    _bench(sat_root)
    r = MoveXYZStage().execute(_Ctx(), {"x": 5.3})
    assert r.success, r.error
    assert r.data["moved"]["x"]["end_mm"] == pytest.approx(5.3, abs=1e-3)
    r = MoveXYZStage().execute(_Ctx(), {"y": 4.9})           # 2.4 mm: our cap refuses
    assert not r.success and "per-command cap" in r.error
    # with our cap opened up, the satellite's own 0.5 mm limit answers "limit"
    _bench(sat_root, xyz_max_step=5.0)
    r = MoveXYZStage().execute(_Ctx(), {"y": 4.9})
    assert not r.success and "satellite's limit" in r.error, r.error

    r = PicomotorJog().execute(_Ctx(), {"axis": "2", "steps": 300})
    assert r.success, r.error
    assert r.data["counter_after"] - r.data["counter_before"] == 300
    assert GetPicomotorSteps().execute(_Ctx(), {}).data["axes"]["2"]["steps"] == 300


def test_operator_hold_is_unavailable_not_retried(online, sat_root):
    from mast.skills.builtins.optics_spectro import AcquireSpectrum, StopSpectrometer

    _bench(sat_root)
    online.sat.set_operator_hold("ccd", True)
    try:
        r = AcquireSpectrum().execute(_Ctx(), {"exposure_ms": 10.0, "sample_current": False})
        assert not r.success and ("人工接管" in r.error or "operator_hold" in r.error), r.error
        assert StopSpectrometer().execute(_Ctx(), {}).success     # stop still passes
    finally:
        online.sat.set_operator_hold("ccd", False)


def test_list_and_stop_on_the_real_hub(online, hub, sat_root):
    from mast.skills.builtins.optics_spectro import ListSatellites, StopSatelliteDevices

    reg = _bench(sat_root)
    r = ListSatellites().execute(_Ctx(), {})
    assert r.success and r.data["hub_available"]
    assert [s["id"] for s in r.data["satellites"]] == ["sim1"]
    r = StopSatelliteDevices().execute(_Ctx(), {"satellite": "sim1"})
    assert r.success and {"sim1/ccd", "sim1/pol", "sim1/xyz", "sim1/pico"} <= set(r.data["stopped"])
    reg.controller("xyz")                     # build it
    report = reg.stop_all(wait_s=3.0)
    assert "xyz" in report["stopped"] and not report["errors"]
