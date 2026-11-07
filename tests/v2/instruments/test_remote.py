"""Satellite-backed devices (``mast.instruments.remote``) over a §6 hub double.

Pins: soft limits and step caps on the hub side; a stop never waits on the I/O
lock or a result; abort cancels the pending command; frames are collected by
``cmd_id`` (foreign frames ignored, re-sent frames de-duplicated) and the
wavelength axis is looked up by ``axis_ver``; hub errors map onto the
instrument error family; and the EXISTING optics skills drive remote axes
unchanged (contract §8.1).
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
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

from mast.instruments.base import (
    AxisConfig,
    InstrumentError,
    InstrumentUnavailable,
    MotionAborted,
    MotionTimeout,
    TravelLimitError,
)
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.instruments.remote import (
    RemoteCommandError,
    RemoteMotionController,
    RemoteOpenLoopController,
    RemoteRotationController,
    RemoteSpectrometer,
    map_hub_error,
    set_hub_provider,
)
from mast.instruments.rotation import RotationConfig
from mast.satellite.errors import SatelliteCommandError, SatelliteTimeout, SatelliteUnavailable

from .fake_hub import FakeSatelliteHub, b64


@pytest.fixture
def hub():
    h = FakeSatelliteHub()
    set_hub_provider(lambda: h)
    yield h
    set_hub_provider(None)


def _xyz(hub, **dev_opts):
    hub.add_device("rig1", "xyz", "xyz_stage", **dev_opts)
    axes = [AxisConfig(name=a, channel=a, min_pos=lo, max_pos=hi, unit="mm")
            for a, lo, hi in (("x", -2.0, 13.3), ("y", 0.0, 5.0), ("z", 0.0, 9.6))]
    return RemoteMotionController(satellite="rig1", device="xyz", axes=axes, max_step=0.5)


# ── error mapping ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc,cls", [
    (SatelliteUnavailable("down", sat_id="t"), InstrumentUnavailable),
    (SatelliteTimeout("slow"), MotionTimeout),
    (SatelliteCommandError("limit", "x"), TravelLimitError),
    (SatelliteCommandError("cancelled", "x"), MotionAborted),
    (SatelliteCommandError("operator_hold", "x"), InstrumentUnavailable),
    (SatelliteCommandError("lost_on_restart", "x"), InstrumentUnavailable),
    (SatelliteCommandError("not_supported", "x"), RemoteCommandError),
    (SatelliteCommandError("device_busy", "x"), RemoteCommandError),
])
def test_hub_errors_map_onto_the_instrument_family(exc, cls):
    out = map_hub_error(exc, where="rig1/dev act")
    assert isinstance(out, cls)
    assert getattr(out, "remote_code", None) == exc.code


def test_our_own_abort_passes_through_unchanged():
    mine = MotionAborted("operator")
    assert map_hub_error(mine, where="x") is mine


# ── XYZ ─────────────────────────────────────────────────────────────────────

class TestXYZ:
    def test_move_settles_and_reports_the_satellite_position(self, hub):
        ctrl = _xyz(hub)
        st = ctrl.axis("x").move_abs(5.3, timeout=5.0)
        assert st.position == pytest.approx(5.3) and st.on_target is True
        assert ("rig1", "xyz", "move_abs", {"x": 5.3}) in hub.calls

    def test_soft_limit_refused_before_anything_is_sent(self, hub):
        ctrl = _xyz(hub)
        with pytest.raises(TravelLimitError):
            ctrl.axis("y").move_abs(7.0)
        assert not [c for c in hub.calls if c[2] == "move_abs"]

    def test_per_command_step_cap(self, hub):
        ctrl = _xyz(hub)
        with pytest.raises(TravelLimitError, match="per-command cap"):
            ctrl.axis("x").move_abs(6.0)                  # 5.0 → 6.0 = 1 mm > 0.5
        ctrl.axis("x").move_rel(0.4, timeout=5.0)
        assert ctrl.axis("x").get_position() == pytest.approx(5.4)

    def test_satellite_side_limit_maps_to_travel_limit(self, hub):
        ctrl = _xyz(hub, limits_mm={"x": [0.0, 5.1], "y": [0.0, 5.0], "z": [0.0, 9.6]})
        with pytest.raises(TravelLimitError, match="satellite"):
            ctrl.axis("x").move_abs(5.4, timeout=5.0)

    def test_abort_cancels_and_stops(self, hub):
        ctrl = _xyz(hub, duration_s=5.0)
        with pytest.raises(MotionAborted):
            ctrl.axis("z").move_abs(4.2, timeout=10.0,
                                    check_abort=_after(3))
        assert hub.cancels, "the pending move must be cancelled"
        assert ("rig1", "xyz", {}) in hub.stops

    def test_timeout_stops_the_remote_move(self, hub):
        ctrl = _xyz(hub, duration_s=5.0)
        with pytest.raises(MotionTimeout):
            ctrl.axis("z").move_abs(4.2, timeout=0.2)
        assert ("rig1", "xyz", {}) in hub.stops

    def test_stop_bypasses_the_io_lock(self, hub):
        ctrl = _xyz(hub)
        ax = ctrl.axis("x")
        with ctrl._io_lock:
            done = threading.Event()
            threading.Thread(target=lambda: (ax.stop(), done.set()), daemon=True).start()
            assert done.wait(1.0), "stop() queued behind the controller lock"

    def test_satellite_offline_is_unavailable(self, hub):
        ctrl = _xyz(hub)
        hub.connected["rig1"] = False
        with pytest.raises(InstrumentUnavailable):
            ctrl.axis("x").get_position()

    def test_not_listed_device_is_unavailable_with_a_reason(self, hub):
        ctrl = RemoteMotionController(satellite="rig1", device="ghost",
                                      axes=[AxisConfig(name="x", channel="x", min_pos=0,
                                                       max_pos=1, unit="mm")])
        with pytest.raises(InstrumentUnavailable, match="not listed"):
            ctrl.connect()


def _after(n):
    box = {"n": 0}

    def probe():
        box["n"] += 1
        return box["n"] >= n

    return probe


# ── rotation ────────────────────────────────────────────────────────────────

class TestRotation:
    def _ctrl(self, hub, **opts):
        hub.add_device("rig1", "pol", "rotation_stage", **opts)
        return RemoteRotationController(
            satellite="rig1", device="pol",
            axes=[AxisConfig(name="angle", channel=1, min_pos=-360, max_pos=720, unit="deg")],
            rotation=RotationConfig(wrap=True, shortest_path=False))

    def test_move_and_home(self, hub):
        ax = self._ctrl(hub, homed=False).axis("angle")
        assert ax.get_status().homed is False
        assert ax.home(timeout=5.0).homed is True
        assert ax.move_abs(45.0, timeout=5.0).position == pytest.approx(45.0)
        assert ("rig1", "pol", "move_abs", {"deg": 45.0}) in hub.calls

    def test_wrap_happens_on_the_hub_side(self, hub):
        ax = self._ctrl(hub).axis("angle")
        ax.move_abs(800.0, timeout=5.0)                   # ≡ 80°, the request is out of range
        assert ("rig1", "pol", "move_abs", {"deg": 440.0}) in hub.calls or \
               ("rig1", "pol", "move_abs", {"deg": 80.0}) in hub.calls


# ── picomotor ───────────────────────────────────────────────────────────────

class TestPicomotor:
    def _ctrl(self, hub):
        hub.add_device("rig1", "pico", "openloop_stepper", max_steps_per_cmd=2000)
        return RemoteOpenLoopController(
            satellite="rig1", device="pico", max_steps_per_cmd=500,
            axes=[AxisConfig(name="2", channel=2, min_pos=-10000, max_pos=10000,
                             unit="steps")])

    def test_jog_uses_the_satellite_counter(self, hub):
        ax = self._ctrl(hub).axis("2")
        ax.jog(300, timeout=5.0)
        ax.jog(-100, timeout=5.0)
        assert ax.get_steps() == 200
        assert ("rig1", "pico", "jog", {"axis": 2, "steps": 300}) in hub.calls

    def test_hub_side_cap_is_tighter_than_the_satellite_one(self, hub):
        ax = self._ctrl(hub).axis("2")
        with pytest.raises(TravelLimitError):
            ax.jog(600)                                    # 500 here, 2000 on the satellite
        assert not [c for c in hub.calls if c[2] == "jog"]

    def test_axis_stop_is_targeted_and_fire_and_forget(self, hub):
        ax = self._ctrl(hub).axis("2")
        ax.stop()
        time.sleep(0.05)
        assert ("rig1", "pico", "stop", {"axis": 2}) in hub.calls

    def test_set_zero(self, hub):
        ax = self._ctrl(hub).axis("2")
        ax.jog(50, timeout=5.0)
        ax.set_zero()
        assert ax.get_steps() == 0


# ── spectrometer ────────────────────────────────────────────────────────────

class TestRemoteSpectrometer:
    def _spec(self, hub, **opts):
        dev = hub.add_device("rig1", "ccd", "spectrometer", **opts)
        return dev, RemoteSpectrometer(satellite="rig1", device="ccd", device_id="ccd")

    def test_frames_collected_by_cmd_id_with_axis(self, hub):
        dev, spec = self._spec(hub)
        # a frame of someone else's acquisition must not be mixed in
        dev.extra_frames = [{"cmd_id": "other", "frame_index": 0, "n": 64, "dtype": "u16",
                             "data_b64": b64(np.zeros(64), "u16"), "axis_ver": 1}]
        hub.emit("rig1", "ccd", "wavelength_axis", dev.axis_data())
        seen = []
        res = spec.acquire(exposure_ms=10.0, frames=3,
                           on_frame=lambda i, a, ax: seen.append((i, a.dtype, ax is not None)))
        assert len(res.frames) == 3 and res.frames[1][0] == 110
        assert [s[0] for s in seen] == [0, 1, 2] and all(s[2] for s in seen)
        assert res.axis_nm is not None and res.axis_nm.shape == (64,)
        assert res.meta["frames_received"] == 3 and res.meta["missing_frames"] == []
        assert res.meta["clock_offset_ms"] == pytest.approx(2.5)
        assert res.started_utc.endswith("+00:00")
        assert not hub.listeners, "the frame listener must be removed afterwards"

    def test_resent_frames_are_deduplicated(self, hub):
        _dev, spec = self._spec(hub, duplicate_frames=True)
        res = spec.acquire(exposure_ms=10.0, frames=2)
        assert len(res.frames) == 2

    def test_summed_rows_arrive_as_f32_and_say_so(self, hub):
        """A custom roi with y_bin=1 is summed on the satellite and
        sent as f32 (hardware binning stays u16) — contract §5.4 allows both."""
        _dev, spec = self._spec(hub, frame_dtype="f32")
        res = spec.acquire(exposure_ms=10.0, frames=2)
        assert [f.dtype for f in res.frames] == [np.float32, np.float32]
        assert res.frames[0][64 // 3] == pytest.approx(4000.0)
        assert [m["dtype"] for m in res.meta["frame_meta"]] == ["f32", "f32"]
        _dev, spec = self._spec(hub)
        assert spec.acquire(exposure_ms=10.0, frames=1).meta["frame_meta"][0]["dtype"] == "u16"

    def test_axis_fetched_when_the_event_was_missed(self, hub):
        _dev, spec = self._spec(hub)
        res = spec.acquire(exposure_ms=10.0, frames=1)
        assert res.axis_nm is not None
        assert ("rig1", "ccd", "wavelength_axis", {}) in hub.calls

    def test_abort_cancels_the_acquisition(self, hub):
        _dev, spec = self._spec(hub, duration_s=5.0)
        with pytest.raises(MotionAborted):
            spec.acquire(exposure_ms=5000.0, frames=1, check_abort=_after(3))
        assert hub.cancels

    def test_save_true_reports_satellite_files(self, hub):
        _dev, spec = self._spec(hub)
        res = spec.acquire(exposure_ms=10.0, frames=1, save=True)
        assert res.files and res.files[0]["path"].endswith(".spe")

    def test_status_and_settings_round_trip(self, hub):
        _dev, spec = self._spec(hub)
        assert spec.status()["laser_line_nm"] == 633.0
        assert spec.laser_line_nm == 633.0
        spec.set_center_wavelength(700.0)
        assert spec.status()["center_wavelength_nm"] == 700.0

    def test_not_supported_keeps_its_code(self, hub):
        _dev, spec = self._spec(hub, not_supported={"set_exit_port"})
        with pytest.raises(RemoteCommandError) as ei:
            spec.set_exit_port("side")
        assert ei.value.code == "not_supported"


# ── registry + the existing skills on remote axes ───────────────────────────

def _remote_config(tmp_path: Path) -> Path:
    cfg = {"devices": [
        {"id": "rig1_xyz", "type": "satellite_motion", "satellite": "rig1", "device": "xyz",
         "role": "focus_xyz", "max_step_mm": 0.5,
         "axes": [{"name": "x", "channel": "x", "min_pos": -2, "max_pos": 13.3, "unit": "mm"},
                  {"name": "y", "channel": "y", "min_pos": 0, "max_pos": 5, "unit": "mm"},
                  {"name": "z", "channel": "z", "min_pos": 0, "max_pos": 9.6, "unit": "mm"}]},
        {"id": "rig1_pol", "type": "satellite_rotation", "satellite": "rig1",
         "device": "pol", "wrap": True,
         "axes": [{"name": "angle", "channel": 1, "min_pos": -360, "max_pos": 720,
                   "unit": "deg", "role": "polarizer"}]},
        {"id": "rig1_pico", "type": "satellite_openloop", "satellite": "rig1",
         "device": "pico", "max_steps_per_cmd": 500,
         "axes": [{"name": "2", "channel": 2, "min_pos": -10000, "max_pos": 10000,
                   "unit": "steps", "role": "beam_steer"}]},
        {"id": "rig1_ccd", "type": "satellite_spectrometer", "satellite": "rig1",
         "device": "ccd", "role": "raman_ccd", "laser_line_nm": 633.0,
         "limits": {"max_exposure_ms": 60000}},
    ], "delay_line": None}
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


@pytest.fixture
def remote_bench(hub, tmp_path):
    for dev, kind in (("xyz", "xyz_stage"), ("pol", "rotation_stage"),
                      ("pico", "openloop_stepper"), ("ccd", "spectrometer")):
        hub.add_device("rig1", dev, kind)
    reg = InstrumentRegistry(_remote_config(tmp_path))
    reset_instrument_registry(reg)
    yield reg
    reset_instrument_registry(None)


class TestRegistryAndExistingSkills:
    def test_inventory_reports_kind_role_and_satellite(self, remote_bench):
        by_id = {d["id"]: d for d in remote_bench.list_devices()}
        assert by_id["rig1_pol"]["kind"] == "rotation"
        assert by_id["rig1_pico"]["kind"] == "openloop"
        assert by_id["rig1_ccd"]["kind"] == "spectrometer"
        assert by_id["rig1_ccd"]["role"] == "raman_ccd"
        assert by_id["rig1_xyz"]["satellite"] == "rig1"
        assert by_id["rig1_xyz"]["remote_device"] == "xyz"
        assert [d["id"] for d in remote_bench.find_devices(role="polarizer")] == ["rig1_pol"]

    def test_spectrometer_is_not_a_motion_controller(self, remote_bench):
        with pytest.raises(InstrumentError, match="spectrometer"):
            remote_bench.controller("rig1_ccd")
        assert remote_bench.spectrometer("rig1_ccd").laser_line_nm == 633.0

    def test_required_keys_fail_loudly(self, tmp_path):
        p = tmp_path / "o.json"
        p.write_text(json.dumps({"devices": [
            {"id": "bad", "type": "satellite_openloop", "satellite": "rig1", "device": "pico",
             "axes": [{"name": "1", "channel": 1, "min_pos": 0, "max_pos": 1}]}]}),
            encoding="utf-8")
        with pytest.raises(InstrumentError, match="max_steps_per_cmd"):
            InstrumentRegistry(p).controller("bad")

    def test_existing_optics_skills_drive_remote_axes_unchanged(self, remote_bench):
        from mast.skills.builtins.optics_stage import (
            OpticalStageGetPos,
            OpticalStageMove,
            StopOpticalStage,
        )

        r = OpticalStageMove().execute(None, {"device_id": "rig1_xyz", "axis": "z",
                                              "position": 4.3, "timeout_s": 5.0})
        assert r.success, r.error
        assert r.data["position"] == pytest.approx(4.3)
        r = OpticalStageGetPos().execute(None, {"device_id": "rig1_pol", "axis": "angle"})
        assert r.success and r.data["unit"] == "deg"
        r = OpticalStageMove().execute(None, {"device_id": "rig1_xyz", "axis": "z",
                                              "position": 10.0})
        assert not r.success and "outside soft limits" in r.error
        r = OpticalStageMove().execute(None, {"device_id": "rig1_xyz", "axis": "z",
                                              "position": 9.0})
        assert not r.success and "per-command cap" in r.error
        r = StopOpticalStage().execute(None, {})
        assert r.success
        assert r.data["report"]["stopped"]

    def test_existing_scan_rasters_a_remote_axis(self, remote_bench, tmp_path, monkeypatch):
        from mast.skills.builtins import optics_scan as osc
        from mast.skills.builtins.optics_scan import OpticalStageScan

        monkeypatch.setattr(osc, "project_root", lambda: tmp_path)
        r = OpticalStageScan().execute(None, {
            "device1": "rig1_xyz", "axis1": "x", "start1": 5.0, "stop1": 5.4, "points1": 3,
            "dry_run": True, "settle_extra_s": 0.0, "return_to_start": False})
        assert r.success, r.error
        assert r.data["n_points"] == 3

    def test_stop_all_reaches_built_remote_devices_without_blocking(self, remote_bench, hub):
        remote_bench.controller("rig1_xyz").axes()
        remote_bench.spectrometer("rig1_ccd")
        t0 = time.monotonic()
        rep = remote_bench.stop_all()
        assert time.monotonic() - t0 < 2.0
        assert set(rep["stopped"]) == {"rig1_xyz", "rig1_ccd"}
        assert ("rig1", "xyz", {}) in hub.stops and ("rig1", "ccd", {}) in hub.stops
