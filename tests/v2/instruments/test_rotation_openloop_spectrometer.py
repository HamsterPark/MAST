"""RotationAxis / OpenLoopAxis / Spectrometer — the three abstractions added for
TERS (contract §8.1), exercised through their in-process simulators.

The properties pinned here are the Layer-0 ones: no caller can get an angle,
a step count or a spectrometer setting past the configured limits, and an
out-of-range request is refused rather than clamped.
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

import threading
import time

import numpy as np
import pytest

from mast.instruments.base import (
    AxisConfig,
    InstrumentError,
    MotionAborted,
    TravelLimitError,
)
from mast.instruments.openloop import SimOpenLoopController
from mast.instruments.rotation import RotationConfig, SimRotationController
from mast.instruments.spectrometer import (
    SimSpectrometer,
    SpectrometerLimitError,
    SpectrometerLimits,
)


def _rot(lo=-360.0, hi=720.0, **rot):
    ctrl = SimRotationController(
        axes=[AxisConfig(name="angle", channel=1, min_pos=lo, max_pos=hi, unit="deg")],
        rotation=RotationConfig(**rot), speed_deg_s=1e6)
    return ctrl.axis("angle")


# ── rotation ────────────────────────────────────────────────────────────────

class TestRotation:
    def test_literal_without_wrap(self):
        ax = _rot(lo=0, hi=180)
        assert ax.move_abs(90.0).position == pytest.approx(90.0)
        with pytest.raises(TravelLimitError):
            ax.move_abs(270.0)                    # no wrap: 270 is just out of range

    def test_wrap_re_expresses_inside_the_limits(self):
        ax = _rot(lo=0, hi=180, wrap=True, period_deg=180.0)
        assert ax.move_abs(270.0).position == pytest.approx(90.0)
        assert ax.move_abs(-170.0).position == pytest.approx(10.0)

    def test_wrap_prefers_the_request_itself_when_reachable(self):
        ax = _rot(lo=-360, hi=720, wrap=True)
        assert ax.move_abs(370.0).position == pytest.approx(370.0)

    def test_shortest_path_picks_the_equivalent_nearest_the_current_angle(self):
        ax = _rot(lo=-360, hi=720, wrap=True, shortest_path=True)
        ax.move_rel(350.0)                       # literal: relative moves never wrap
        # 10° ≡ 370° ≡ −350°; from 350° the near one is 370°
        assert ax.move_abs(10.0).position == pytest.approx(370.0)
        # and from 0° the same request resolves to the nearest candidate, −10° for 350°
        ax2 = _rot(lo=-360, hi=720, wrap=True, shortest_path=True)
        assert ax2.move_abs(350.0).position == pytest.approx(-10.0)

    def test_wrap_with_no_equivalent_in_range_is_refused(self):
        ax = _rot(lo=0, hi=90, wrap=True)
        with pytest.raises(TravelLimitError, match="no angle equivalent"):
            ax.move_abs(180.0)

    def test_relative_move_never_rewrites_the_direction(self):
        ax = _rot(lo=-360, hi=720, wrap=True, shortest_path=True)
        ax.move_rel(300.0)
        assert ax.move_rel(100.0).position == pytest.approx(400.0)
        with pytest.raises(TravelLimitError):
            ax.move_rel(400.0)                    # 800° > 720°: refused, not wrapped

    def test_sim_is_unhomed_until_homed(self):
        ax = _rot()
        assert ax.get_status().homed is False
        assert ax.home(timeout=5.0).homed is True

    def test_non_finite_angle_is_refused(self):
        with pytest.raises(TravelLimitError):
            _rot(wrap=True).move_abs(float("nan"))


# ── open loop ───────────────────────────────────────────────────────────────

def _pico(lo=-5000, hi=5000, cap=200, rate=1e6):
    ctrl = SimOpenLoopController(
        axes=[AxisConfig(name="1", channel=1, min_pos=lo, max_pos=hi, unit="steps")],
        max_steps_per_cmd=cap, steps_per_s=rate)
    return ctrl.axis("1")


class TestOpenLoop:
    def test_jog_accumulates_the_counter(self):
        ax = _pico()
        ax.jog(150)
        ax.jog(-50)
        assert ax.get_steps() == 100
        assert ax.get_status().on_target is None          # no feedback, ever

    def test_per_command_cap_is_refused_not_split(self):
        ax = _pico(cap=200)
        with pytest.raises(TravelLimitError, match="per-command cap"):
            ax.jog(201)
        assert ax.jogs == [] and ax.get_steps() == 0

    def test_cumulative_soft_limit(self):
        ax = _pico(lo=-100, hi=100, cap=200)
        ax.jog(90)
        with pytest.raises(TravelLimitError):
            ax.jog(20)                                    # would reach 110 > 100
        assert ax.get_steps() == 90

    def test_fractional_steps_are_refused(self):
        with pytest.raises(TravelLimitError, match="whole number"):
            _pico().jog(2.5)

    def test_move_abs_is_counter_absolute_and_set_zero_redefines_it(self):
        ax = _pico()
        ax.move_abs(120)
        assert ax.get_steps() == 120
        ax.set_zero()
        assert ax.get_steps() == 0
        ax.move_rel(-30)
        assert ax.get_steps() == -30

    def test_home_is_not_a_thing_for_open_loop(self):
        with pytest.raises(InstrumentError, match="open loop"):
            _pico().home()

    def test_slow_jog_is_abortable(self):
        ax = _pico(cap=2000, rate=100.0)                  # 2000 steps = 20 s
        box = {"n": 0}

        def probe():
            box["n"] += 1
            return box["n"] > 3

        with pytest.raises(MotionAborted):
            ax.jog(2000, timeout=30.0, check_abort=probe)
        assert ax.stops == 1
        assert 0 <= ax.get_steps() < 2000


# ── spectrometer ────────────────────────────────────────────────────────────

def _spec(**limits):
    return SimSpectrometer(device_id="sim_ccd", limits=SpectrometerLimits(**limits),
                           pixels=128, time_scale=0.0, seed=1)


class TestSpectrometer:
    def test_limits_refuse_rather_than_clamp(self):
        s = _spec(max_exposure_ms=1000.0, max_frames=5, max_total_s=2.0,
                  min_center_nm=500.0, max_center_nm=800.0,
                  gratings=("[500nm,1200][0][0]",))
        with pytest.raises(SpectrometerLimitError):
            s.acquire(exposure_ms=5000.0)
        with pytest.raises(SpectrometerLimitError):
            s.acquire(frames=6)
        with pytest.raises(SpectrometerLimitError, match="ceiling"):
            s.acquire(exposure_ms=900.0, frames=3)        # 2.7 s > 2 s
        with pytest.raises(SpectrometerLimitError):
            s.set_center_wavelength(900.0)
        with pytest.raises(SpectrometerLimitError):
            s.set_grating("[800nm,150][2][0]")
        with pytest.raises(SpectrometerLimitError):
            s.set_exit_port("top")
        with pytest.raises(SpectrometerLimitError):
            s.configure(frames=2.5)
        assert s.acquisitions == 0

    def test_acquire_returns_frames_axis_and_utc_times(self):
        s = _spec()
        seen = []
        res = s.acquire(exposure_ms=100.0, frames=3, on_frame=lambda i, a, ax: seen.append(i))
        assert len(res.frames) == 3 and seen == [0, 1, 2]
        assert res.axis_nm is not None and res.axis_nm.shape == (128,)
        assert res.started_utc.endswith("+00:00") and res.finished_utc.endswith("+00:00")
        assert np.all(res.frames[0] >= 0)

    def test_an_acquires_exposure_and_frames_stay_as_the_settings(self):
        """LightField and both satellites keep them: an acquire is not
        a one-shot override, and the simulator must not suggest that it is."""
        s = _spec()
        s.acquire(exposure_ms=250.0, frames=2)
        st = s.status()
        assert (st["exposure_ms"], st["frames"]) == (250.0, 2)
        res = s.acquire()                                # no arguments: the kept ones
        assert len(res.frames) == 2 and res.meta["exposure_ms"] == 250.0

    def test_check_request_validates_without_exposing(self):
        s = _spec(max_frames=2)
        assert s.check_request(10.0, 2) == (10.0, 2)
        with pytest.raises(SpectrometerLimitError):
            s.check_request(10.0, 3)
        assert s.acquisitions == 0

    def test_abort_and_stop_end_an_acquisition(self):
        s = SimSpectrometer(device_id="sim", pixels=16, time_scale=1.0)
        with pytest.raises(MotionAborted):
            s.acquire(exposure_ms=5000.0, check_abort=lambda: True)
        s2 = SimSpectrometer(device_id="sim2", pixels=16, time_scale=1.0)
        t = threading.Timer(0.1, s2.stop)
        t.start()
        t0 = time.monotonic()
        with pytest.raises(MotionAborted, match="stopped"):
            s2.acquire(exposure_ms=5000.0)
        assert time.monotonic() - t0 < 2.0

    def test_settings_are_refused_as_busy_while_acquiring(self):
        s = SimSpectrometer(device_id="sim", pixels=16, time_scale=1.0)
        s.BUSY_WAIT_S = 0.05
        th = threading.Thread(target=lambda: s.acquire(exposure_ms=600.0))
        th.start()
        time.sleep(0.1)
        with pytest.raises(InstrumentError, match="busy"):
            s.set_grating("[500nm,600][1][0]")
        assert s.status()["is_running"] is True           # status never waits for the lock
        th.join()

    def test_limits_from_config_dict(self):
        lim = SpectrometerLimits.from_dict({"max_exposure_ms": 10, "gratings": ["a", "b"],
                                            "exit_ports": ["FRONT"]})
        assert lim.max_exposure_ms == 10 and lim.gratings == ("a", "b")
        assert lim.exit_ports == ("front",)
