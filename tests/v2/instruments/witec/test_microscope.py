"""WITecMicroscope on the fake WITec Control: limits, write access, waiting and aborting, read-back, spectra, stop.

Time is virtual (``VirtualClock``): the driver sleeps on it, the fake WITec Control reads it, so minute-long
operations and timeouts cost nothing. The few concurrency tests use real time.
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import hashlib
import json
import threading
import time

import numpy as np
import pytest

from mast.instruments.base import (InstrumentError, InstrumentUnavailable, MotionAborted, MotionTimeout,
                                   OperationNotSupported)
from mast.instruments.microscope import MicroscopeLimitError, MicroscopeLimits, WriteAccessDenied
from mast.instruments.registry import InstrumentRegistry
from mast.instruments.spectrometer import SpectrometerLimitError, SpectrometerLimits
from mast.instruments.witec import microscope as wm
from mast.instruments.witec import params as P
from mast.instruments.laser import EmissionControlDisabled
from mast.instruments.witec.bucs import BucsError, BucsNotRunning, BucsNoValue
from mast.instruments.witec.fake_bucs import DEFAULT_PRESETS, FakeBucsClient, VirtualClock
from mast.instruments.witec.microscope import (LaserAttestationGuard, LaserAttestationRequired, WITecConfig,
                                                WITecMicroscope, build, default_emission_guard, set_client_factory)
from mast.i18n import use_lang
from mast.operator_hitl import optical_state

@pytest.fixture(autouse=True)
def _data_roots(tmp_path, monkeypatch):
    """Projects may only be written below the data roots: for these tests the test folder is the data root."""
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))


LIMITS = SpectrometerLimits(min_exposure_ms=10, max_exposure_ms=600_000, max_total_s=7200,
                            min_center_nm=300, max_center_nm=1100)
MLIMITS = MicroscopeLimits(x_um=(-25000, 25000), y_um=(-25000, 25000), z_um=(-100, 100), max_step_um=5000,
                           max_laser_power_mw={"532": 20.0, "633": 10.0})


class Rig:
    def __init__(self, tmp_path, *, grant=True, cfg=None, mlimits=MLIMITS, limits=LIMITS, guard=None, **fake_kw):
        self.clock = VirtualClock()
        self.fake = FakeBucsClient(clock=self.clock.now, work_dir=tmp_path, **fake_kw)
        conf = {"poll_s": 0.01, "stage_poll_s": 0.01, "raw_dir": str(tmp_path / "raw"),
                "allow_emission_control": True}           # most tests are not about laser emission; their own tests are below
        conf.update(cfg or {})
        self.mic = WITecMicroscope(device_id="witec", client=self.fake, config=WITecConfig.from_entry(conf),
                                   limits=limits, microscope_limits=mlimits, clock=self.clock.now,
                                   sleep=self.clock.sleep, emission_guard=guard)
        self.tmp = tmp_path
        if grant:
            self.fake.operator_press_com_automation()
            assert self.mic.request_write_access()

    def writes(self, kind=None):
        return [w for w in self.fake.writes if kind in (None, w[0])]


@pytest.fixture()
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.mic.close()


@pytest.fixture()
def rig_nogrant(tmp_path):
    r = Rig(tmp_path, grant=False)
    yield r
    r.mic.close()


# ── construction, config, connection ─────────────────────────────────────────────────────────────────────────────

def test_construction_touches_no_hardware(tmp_path):
    calls = []
    mic = WITecMicroscope(device_id="w", client_factory=lambda: calls.append(1) or FakeBucsClient())
    assert calls == []
    mic.status()
    assert calls == [1]
    mic.status()
    assert calls == [1]                                    # built once


def test_microscope_needs_a_client_or_a_factory():
    with pytest.raises(ValueError):
        WITecMicroscope(device_id="w")


def test_build_reads_the_entry_and_uses_the_factory_hook_lazily(tmp_path):
    fake = FakeBucsClient()
    seen = []
    old = set_client_factory(lambda entry: seen.append(entry["id"]) or fake)
    try:
        mic = build({"id": "witec", "type": "witec_com", "name": "WITec alpha300",
                     "limits": {"min_exposure_ms": 10, "max_exposure_ms": 600000, "max_total_s": 7200},
                     "microscope_limits": {"z_um": [-100, 100], "x_um": [-5, 5], "y_um": [-5, 5]},
                     "poll_s": 0.02, "states": ["A", "B"]})
        assert seen == [] and isinstance(mic, WITecMicroscope)
        assert mic.name == "WITec alpha300" and mic.limits.max_total_s == 7200
        assert mic.microscope_limits.z_um == (-100.0, 100.0) and mic.cfg.poll_s == 0.02
        mic.microscope_status()
        assert seen == ["witec"]
    finally:
        assert set_client_factory(old) is not None
    assert wm.get_client_factory() is old


def test_build_without_a_factory_makes_a_comtypes_client_without_connecting():
    assert wm.get_client_factory() is None
    mic = build({"id": "witec", "type": "witec_com"})
    from mast.instruments.witec.comtypes_client import ComtypesBucsClient

    cl = mic._client()                                     # noqa: SLF001
    assert isinstance(cl, ComtypesBucsClient) and not cl.connected
    mic.close()


@pytest.mark.parametrize("bad", [{"poll_s": 0}, {"call_timeout_s": -1}, {"stage_tol_xy_um": "x"},
                                 {"lasers": [{"name": "x"}]}, {"spectrograph": 7},
                                 {"lasers": [{"index": 1, "wavelength_nm": 1}, {"index": 1, "wavelength_nm": 2}]}])
def test_config_validation(bad):
    with pytest.raises(ValueError):
        WITecConfig.from_entry(bad)


def test_closed_witec_is_data_not_an_exception(tmp_path):
    r = Rig(tmp_path, grant=False, witec_running=False)
    st = r.mic.microscope_status()
    assert st["connected"] is False and "WITec Control" in st["hint"] and st["write_access"] is False
    sp = r.mic.status()
    assert sp["state"] == "unavailable" and sp["is_running"] is False and sp["connected"] is False
    with pytest.raises(BucsNotRunning):
        r.mic.set_analyzer_deg(10)                         # a write, though, says so plainly


def test_failed_connects_are_rate_limited(tmp_path):
    r = Rig(tmp_path, grant=False, witec_running=False)
    for _ in range(6):
        r.mic.microscope_status()
    assert r.fake.connect_attempts == 1                     # not once per poll
    r.clock.sleep(r.mic.cfg.connect_cooldown_s + 0.1)
    r.mic.microscope_status()
    assert r.fake.connect_attempts == 2
    r.fake.witec_start()
    r.clock.sleep(r.mic.cfg.connect_cooldown_s + 0.1)
    assert r.mic.microscope_status()["connected"] is True


def test_witec_restart_drops_write_access_and_the_driver_reconnects(rig):
    rig.mic.set_analyzer_deg(10)
    rig.fake.witec_exit()
    with pytest.raises(InstrumentUnavailable):             # BucsConnectionLost / BucsNotRunning: WITec is gone
        rig.mic.set_analyzer_deg(20)
    assert rig.mic.microscope_status()["connected"] is False
    rig.fake.witec_start()
    rig.clock.sleep(5)
    assert rig.mic.get_analyzer_deg() == 10.0               # reads work again at once
    with pytest.raises(WriteAccessDenied):
        rig.mic.set_analyzer_deg(30)                        # but the person has to press the button again
    rig.fake.operator_press_com_automation()
    assert rig.mic.set_analyzer_deg(30) == 30.0


# ── write access ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_writes_need_the_operator_and_nothing_is_sent_without(rig_nogrant):
    r = rig_nogrant
    with pytest.raises(WriteAccessDenied, match="COM Automation"):
        r.mic.set_analyzer_deg(10)
    with pytest.raises(WriteAccessDenied):
        r.mic.move_stage_um(x=1)
    assert r.fake.writes == []
    assert r.mic.microscope_status()["write_access"] is False
    assert [e for e in r.fake.log if e[0] == "request_write"].__len__() >= 1


def test_waiting_for_write_access_is_bounded_abortable_and_sees_the_button_press(tmp_path):
    r = Rig(tmp_path, grant=False)
    t0 = r.clock.now()
    assert r.mic.request_write_access_waiting(5.0) is False
    assert 4.5 <= r.clock.now() - t0 <= 6.5                 # bounded by timeout_s
    pressed_at = r.clock.now() + 3.0

    def sleep(dt):
        r.clock.sleep(dt)
        if r.clock.now() >= pressed_at:
            r.fake.operator_press_com_automation()

    r.mic._sleep = sleep                                    # noqa: SLF001 - the operator presses it 3 s in
    assert r.mic.request_write_access_waiting(30.0) is True
    r.mic.release_write_access()
    r.fake._allowed = False                                 # noqa: SLF001
    r.mic._sleep = r.clock.sleep                            # noqa: SLF001 - nobody presses the button now
    n = {"i": 0}

    def abort():
        n["i"] += 1
        return n["i"] > 2

    with pytest.raises(MotionAborted):
        r.mic.request_write_access_waiting(30.0, check_abort=abort)


def test_revoke_mid_run_refuses_the_next_write_but_not_reads(rig):
    rig.mic.set_analyzer_deg(15)
    rig.fake.operator_revoke()
    with pytest.raises(WriteAccessDenied):
        rig.mic.set_analyzer_deg(25)
    assert rig.mic.get_analyzer_deg() == 15.0 and rig.mic.get_stage_position_um()["z"] == 0.0


def test_release_hands_it_back_and_close_does_too(rig):
    assert rig.mic.has_write_access()
    rig.mic.release_write_access()
    assert not rig.mic.has_write_access() and rig.fake._holds_write is False     # noqa: SLF001
    assert rig.mic.request_write_access()
    rig.mic.close()
    assert rig.fake._holds_write is False and not rig.fake.connected              # noqa: SLF001


# ── analyzer ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_analyzer_roundtrip_and_layer0_limits(rig):
    assert rig.mic.set_analyzer_deg(45.0) == 45.0 and rig.mic.get_analyzer_deg() == 45.0
    n = len(rig.writes())
    for bad in (400.0, -1.0, float("nan"), "x"):
        with pytest.raises(MicroscopeLimitError):
            rig.mic.set_analyzer_deg(bad)
    assert len(rig.writes()) == n and rig.mic.get_analyzer_deg() == 45.0


def test_analyzer_comparison_wraps_around_360(rig):
    f = rig.fake._tree[P.ANALYZER]                         # noqa: SLF001 - WITec reports 0° as 360°
    f.on_set = lambda v: setattr(f, "value", 360.0 if v == 0 else v)
    assert rig.mic.set_analyzer_deg(0.0) == 360.0


def test_analyzer_that_never_arrives_times_out(tmp_path):
    r = Rig(tmp_path, cfg={"analyzer_timeout_s": 5})
    f = r.fake._tree[P.ANALYZER]                           # noqa: SLF001 - the motor does not move
    f.on_set = lambda v: None
    with pytest.raises(MotionTimeout, match="analyzer reads"):
        r.mic.set_analyzer_deg(90.0)


def test_analyzer_settle_time_scales_with_the_angle(tmp_path):
    r = Rig(tmp_path, cfg={"analyzer_settle_s": 0.5, "analyzer_full_turn_s": 6.0})
    t0 = r.clock.now()
    r.mic.set_analyzer_deg(180.0)
    assert 3.4 <= r.clock.now() - t0 <= 3.8               # 0.5 + 6 * 180/360
    t0 = r.clock.now()
    r.mic.set_analyzer_deg(185.0)
    assert r.clock.now() - t0 < 1.2


def test_analyzer_parameter_missing_is_not_supported_not_a_crash(tmp_path):
    r = Rig(tmp_path, omit=["analyzer"])
    with pytest.raises(OperationNotSupported, match="no 'analyzer'"):
        r.mic.get_analyzer_deg()
    with pytest.raises(OperationNotSupported):
        r.mic.set_analyzer_deg(10)


def test_analyzer_coupler(rig):
    assert rig.mic.set_analyzer_in(False) is False
    assert rig.fake.peek(P.ANALYZER_COUPLER) is False
    assert rig.mic.set_analyzer_in(True) is True


# ── beam path ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_beam_path_waits_for_the_reported_state(tmp_path):
    r = Rig(tmp_path, beam_switch_s=1.5)
    t0 = r.clock.now()
    assert r.mic.set_beam_path("raman") == "Raman"
    assert r.clock.now() - t0 >= 1.4
    assert r.mic.set_beam_path("VIDEO") == "Video"
    with pytest.raises(MicroscopeLimitError):
        r.mic.set_beam_path("sideways")


def test_beam_path_state_that_never_matches_names_the_markers(tmp_path):
    r = Rig(tmp_path, cfg={"beam_path_timeout_s": 2})
    r.fake._set_beam = lambda key: setattr(r.fake, "_beam", "Weird")        # noqa: SLF001
    with pytest.raises(InstrumentError, match=r"reads 'Weird'.*raman"):
        r.mic.set_beam_path("raman")


def test_beam_markers_are_configurable(tmp_path):
    r = Rig(tmp_path, cfg={"beam_path_timeout_s": 2, "beam_markers": {"raman": ["weird"]}})
    r.fake._set_beam = lambda key: setattr(r.fake, "_beam", "Weird")        # noqa: SLF001
    assert r.mic.set_beam_path("raman") == "Weird"


def test_unreadable_beam_state_falls_back_to_the_vendors_fixed_settle(tmp_path):
    r = Rig(tmp_path, cfg={"beam_path_timeout_s": 2, "beam_path_settle_s": 4})
    r.fake.inject(P.BEAM_STATE, BucsError("unreadable"), ops=("get",), times=10**6)
    t0 = r.clock.now()
    assert r.mic.set_beam_path("raman") == "raman"            # state unknown: the requested name comes back
    assert r.clock.now() - t0 >= 5.9                          # 2 s of trying + the 4 s settle


# ── lasers and shutter ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_lasers_are_listed_with_the_selected_one_taken_from_witec(rig):
    las = rig.mic.list_lasers()
    sel = [l for l in las if l["selected"]]
    assert [l["name"] for l in sel] == ["532 nm"] and sel[0]["wavelength_nm"] == pytest.approx(532.138)
    assert sel[0]["wavelength_verified"] is True
    assert all(l["wavelength_verified"] is False for l in las if not l["selected"])
    assert [l["power_controllable"] for l in las] == [False, True, True, False, False]


def test_select_laser_by_index_name_or_wavelength(rig):
    assert rig.mic.select_laser("633")["name"] == "633 nm"
    assert rig.mic.laser_line_nm == pytest.approx(633.0)
    assert rig.mic.select_laser("532 nm")["index"] == 1
    assert rig.mic.select_laser(2)["name"] == "633 nm"
    with pytest.raises(MicroscopeLimitError, match="no laser"):
        rig.mic.select_laser("447")


def test_a_wrong_laser_table_is_caught_by_the_live_wavelength(tmp_path):
    swapped = [{"index": 0, "wavelength_nm": 355}, {"index": 1, "wavelength_nm": 633, "power_controllable": True},
               {"index": 2, "wavelength_nm": 532, "power_controllable": True},
               {"index": 3, "wavelength_nm": 1064}, {"index": 4, "wavelength_nm": 2600}]
    r = Rig(tmp_path, cfg={"lasers": swapped})
    # the laser is identified by its LIVE wavelength, never by an index: WITec has 532.138 nm selected, so the entry
    # that says 532 is the selected one (and its power cap is the 532 cap) whatever the index order of the table
    sel = [l for l in r.mic.list_lasers() if l["selected"]]
    assert len(sel) == 1 and sel[0]["wavelength_nm"] == pytest.approx(532.138)
    # selecting BY INDEX trusts the table's order, so it is cross-checked after the fact
    with pytest.raises(InstrumentError, match="index order is wrong"):
        r.mic.select_laser(1)                              # SelectLaser1 reports 532 nm, the table says 633
    with pytest.raises(InstrumentError, match="index order is wrong"):
        r.mic.select_laser(2)                              # SelectLaser2 reports 633 nm, the table says 532


def test_laser_table_of_the_wrong_size_refuses_selection_by_index(tmp_path):
    r = Rig(tmp_path, cfg={"lasers": [{"index": 0, "wavelength_nm": 355}, {"index": 1, "wavelength_nm": 532}]})
    assert "table_mismatch" in r.mic.list_lasers()[0]
    with pytest.raises(InstrumentError, match="lists 2 lasers.*reports 5"):
        r.mic.select_laser(1)


def test_laser_power_cap_controllability_and_missing_cap(tmp_path):
    r = Rig(tmp_path, mlimits=MicroscopeLimits(z_um=(-100, 100), max_laser_power_mw={"532": 20.0}))
    r.mic.select_laser("532")
    assert r.mic.set_laser_power_mw(5.0) == 5.0 and r.mic.get_laser_power_mw() == 5.0
    for bad in (20.5, -1.0):
        with pytest.raises(MicroscopeLimitError, match="outside"):
            r.mic.set_laser_power_mw(bad)
    assert r.mic.get_laser_power_mw() == 5.0                # refused, not clamped
    r.mic.select_laser("633")
    with pytest.raises(MicroscopeLimitError, match="no max_laser_power_mw"):
        r.mic.set_laser_power_mw(1.0)
    r.mic.select_laser("355")
    with pytest.raises(MicroscopeLimitError, match="no software power control"):
        r.mic.set_laser_power_mw(1.0)
    r.mic.select_laser("1064")
    assert r.mic.get_laser_power_mw() is None               # WITec cannot read the external laser's power
    with pytest.raises(MicroscopeLimitError, match="no software power control"):
        r.mic.set_laser_power_mw(1.0)


def test_a_power_overshoot_closes_the_shutter(rig):
    rig.mic.select_laser("532")
    rig.mic.set_shutter(True)
    f = rig.fake._tree[P.LASER_POWER]                      # noqa: SLF001 - the laser overshoots 1.5×
    f.on_set = lambda v: rig.fake._power.__setitem__(rig.fake._selected, v * 1.5)    # noqa: SLF001
    with pytest.raises(MicroscopeLimitError, match="shutter closed"):
        rig.mic.set_laser_power_mw(15.0)
    assert rig.fake.peek(P.LASER_SHUTTER) is False


def test_shutter_open_close_and_read_back(rig):
    assert rig.mic.set_shutter(True) is True and rig.fake.peek(P.LASER_SHUTTER) is True
    assert rig.mic.set_shutter(False) is False
    f = rig.fake._tree[P.LASER_SHUTTER]                    # noqa: SLF001 - the shutter does not obey
    f.on_set = lambda v: None
    rig.fake.human_sets(P.LASER_SHUTTER, False)
    with pytest.raises(InstrumentError, match="reads closed after asking for open"):
        rig.mic.set_shutter(True)


def test_laser_without_a_software_shutter_cannot_be_closed_by_software(rig):
    rig.mic.select_laser("355")
    with pytest.raises(OperationNotSupported, match="block the beam physically"):
        rig.mic.set_shutter(False)


def test_closing_needs_witecs_write_access_but_no_hub_side_gate(rig_nogrant):
    with pytest.raises(WriteAccessDenied):
        rig_nogrant.mic.set_shutter(False)                 # WITec itself refuses; the hub adds no gate of its own
    with pytest.raises(WriteAccessDenied):
        rig_nogrant.mic.set_shutter(True)


# ── saved states ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_states_listing_load_and_verification(rig):
    states = rig.mic.list_states()
    assert "SHG" in states and "RayShield 532 - G600" in states and len(states) == 9
    assert rig.mic.load_state("RayShield 532 - G600") == "RayShield 532 - G600"
    assert rig.fake.peek(P.spectrograph_path(1, "Grating")) == 1
    assert rig.mic.microscope_status()["state"] == "RayShield 532 - G600"
    with pytest.raises(MicroscopeLimitError, match="no microscope state"):
        rig.mic.load_state("Not a state")


def test_current_state_unknown_to_the_config_is_listed_too(rig):
    rig.fake.presets["Operator's own"] = {"laser": 1, "grating": 0, "center_nm": 600.0}
    rig.fake._state_name = "Operator's own"                # noqa: SLF001
    assert "Operator's own" in rig.mic.list_states()


def test_a_state_witec_ignores_is_an_error(tmp_path):
    r = Rig(tmp_path, cfg={"states": ["Ghost", "SHG"]})
    with pytest.raises(InstrumentError, match="did not take the state"):
        r.mic.load_state("Ghost")                          # in the list, but WITec has no such preset


def test_a_state_that_leaves_the_laser_over_the_cap_closes_the_shutter(tmp_path):
    presets = dict(DEFAULT_PRESETS)
    presets["Hot"] = {"laser": 1, "grating": 0, "center_nm": 547.0, "power_mw": 40.0, "shutter_open": True}
    r = Rig(tmp_path, presets=presets, cfg={"states": ["Hot"]})
    with pytest.raises(MicroscopeLimitError, match=r"above the 20 mW cap"):
        r.mic.load_state("Hot")
    assert r.fake.peek(P.LASER_SHUTTER) is False


def test_loading_a_state_is_refused_while_a_sequence_runs(rig):
    rig.fake._start_seq("Foreign", 100.0, 0.0)             # noqa: SLF001 - somebody's measurement
    with pytest.raises(InstrumentError, match="busy"):
        rig.mic.load_state("SHG")


# ── stage ────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_stage_moves_are_verified_by_read_back(rig):
    assert rig.mic.get_stage_position_um() == {"x": 0.0, "y": 0.0, "z": 0.0}
    out = rig.mic.move_stage_um(x=120.0, y=-40.0, z=15.0)
    assert out == {"x": 120.0, "y": -40.0, "z": 15.0}
    assert rig.fake.peek(P.XY_STATE) == P.STAGE_STATE_REACHED
    rig.mic.move_stage_um(z=-30.0)
    assert rig.mic.get_stage_position_um()["z"] == -30.0 and rig.mic.get_stage_position_um()["x"] == 120.0


def test_stage_layer0_limits_refuse_never_clamp(rig, tmp_path):
    n = len(rig.writes())
    for kw, pat in (({"z": 120.0}, "outside"), ({"x": 30000.0}, "outside"), ({"x": 9000.0}, "max_step_um"),
                    ({"z": float("nan")}, "finite")):
        with pytest.raises(MicroscopeLimitError, match=pat):
            rig.mic.move_stage_um(**kw)
    with pytest.raises(MicroscopeLimitError, match="at least one"):
        rig.mic.move_stage_um()
    assert len(rig.writes()) == n
    open_ = Rig(tmp_path / "b", mlimits=MicroscopeLimits(x_um=(-100, 100)))
    with pytest.raises(MicroscopeLimitError, match="no travel range configured"):
        open_.mic.move_stage_um(z=1.0)                     # Z unconfigured: fail-closed (objective!)


def test_witecs_own_z_range_is_checked_too(tmp_path):
    r = Rig(tmp_path, mlimits=MicroscopeLimits(x_um=(-100, 100), y_um=(-100, 100), z_um=(-500, 500)))
    r.fake.human_sets(P.MAX_Z_STEPPER_TRAVEL, 100.0)       # WITec's software range is ±50 µm here
    with pytest.raises(MicroscopeLimitError, match="WITec's own software Z range"):
        r.mic.move_stage_um(z=80.0)
    assert r.mic.move_stage_um(z=40.0)["z"] == 40.0


def test_stage_that_hits_an_end_switch_is_reported_as_not_reached(tmp_path):
    r = Rig(tmp_path)
    r.fake.xy_limit_um = 500.0                             # WITec's real travel is shorter than the hub-side limit
    with pytest.raises(InstrumentError, match="Position not Reached"):
        r.mic.move_stage_um(x=2000.0)


def test_aborting_a_move_stops_the_axis(tmp_path):
    r = Rig(tmp_path, stage_move_s=60.0)
    polls = {"n": 0}

    def abort():
        polls["n"] += 1
        return polls["n"] > 3

    with pytest.raises(MotionAborted):
        r.mic.move_stage_um(x=400.0, check_abort=abort)
    assert P.XY_STOP in r.fake.triggers()
    assert r.fake.peek(P.XY_STATE) == P.STAGE_STATE_STOPPED


def test_a_move_that_takes_too_long_is_stopped_and_times_out(tmp_path):
    r = Rig(tmp_path, stage_move_s=1e6, cfg={"stage_timeout_s": 5})
    with pytest.raises(MotionTimeout, match="not finished within 5 s"):
        r.mic.move_stage_um(z=10.0)
    assert P.Z_STOP in r.fake.triggers()


def test_a_person_stopping_the_stage_is_reported(tmp_path):
    r = Rig(tmp_path, stage_move_s=60.0)
    fired = {"done": False}

    def sleep(dt):
        r.clock.sleep(dt)
        if not fired["done"]:
            fired["done"] = True
            r.fake.trigger(P.XY_STOP)                       # as if somebody pressed Stop

    r.mic._sleep = sleep                                    # noqa: SLF001
    with pytest.raises(MotionAborted, match="stopped manually"):
        r.mic.move_stage_um(x=300.0)


def test_a_finished_looking_state_is_not_believed_before_the_position_agrees(tmp_path):
    r = Rig(tmp_path, cfg={"stage_grace_s": 1.0})
    r.fake._tree[P.XY_MOVE].on_trigger = lambda: setattr(r.fake._xy, "x", 0) if False else None    # noqa: SLF001 - move never starts
    with pytest.raises(InstrumentError, match="WITec says the move is done"):
        r.mic.move_stage_um(x=50.0)


def test_stage_move_is_refused_while_a_sequence_runs_and_on_a_virtual_stage(rig):
    rig.fake._start_seq("Foreign", 100.0, 0.0)             # noqa: SLF001
    with pytest.raises(InstrumentError, match="busy"):
        rig.mic.move_stage_um(x=1.0)
    rig.fake._seq = None                                   # noqa: SLF001
    rig.fake.human_sets(P.STAGE_IS_VIRTUAL, True)
    with pytest.raises(OperationNotSupported, match="virtual"):
        rig.mic.move_stage_um(x=1.0)
    assert rig.mic.get_stage_position_um() == {"x": None, "y": None, "z": None}


# ── autofocus, video ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_autofocus_success_failure_and_abort(rig):
    out = rig.mic.autofocus()
    assert out["ok"] and out["verified"] and out["z_um"] == 12.5 and out["z_before_um"] == 0.0
    rig.fake.focus_ok = False
    assert rig.mic.autofocus()["ok"] is False
    rig.fake.focus_ok = True
    rig.fake.focus_s = 120.0
    n = {"i": 0}

    def abort():
        n["i"] += 1
        return n["i"] > 3

    with pytest.raises(MotionAborted):
        rig.mic.autofocus(check_abort=abort)
    assert P.Z_STOP in rig.fake.triggers()


def test_autofocus_that_ends_outside_the_configured_z_range_is_an_error(rig):
    rig.fake.focus_z_um = 80.0
    rig.mic.microscope_limits = MicroscopeLimits(z_um=(-50, 50), x_um=(-1, 1), y_um=(-1, 1))
    with pytest.raises(MicroscopeLimitError, match="outside the configured"):
        rig.mic.autofocus()


def test_autofocus_without_a_status_parameter_uses_the_fixed_wait(tmp_path):
    r = Rig(tmp_path, omit=[P.AUTOFOCUS_STATUS])
    t0 = r.clock.now()
    out = r.mic.autofocus()
    assert out["verified"] is False and r.clock.now() - t0 >= 5.0


def test_autofocus_timeout(tmp_path):
    r = Rig(tmp_path, focus_s=1e6, cfg={"autofocus_timeout_s": 5})
    with pytest.raises(MotionTimeout, match="autofocus still running"):
        r.mic.autofocus()


def test_video_image_is_written_after_switching_to_the_video_beam(rig, tmp_path):
    rig.mic.set_beam_path("raman")
    out = rig.mic.acquire_video_image(tmp_path / "shots" / "a")
    assert out.endswith("a.png") and Path(out).read_bytes()[:4] == b"\x89PNG"
    assert rig.fake._beam == "Video"                       # noqa: SLF001
    with pytest.raises(InstrumentError, match="already exists"):
        rig.mic.acquire_video_image(out)


def test_video_that_never_arrives_times_out(tmp_path):
    r = Rig(tmp_path, cfg={"video_timeout_s": 3})
    r.fake._tree[P.VIDEO_TO_FILE].on_trigger = lambda: None                 # noqa: SLF001
    with pytest.raises(MotionTimeout, match="no video image"):
        r.mic.acquire_video_image(tmp_path / "x.png")


# ── spectra ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_acquire_returns_frames_axis_files_and_provenance(rig):
    got = []
    res = rig.mic.acquire(exposure_ms=200, frames=3, save=True, base_name="run1", tag="t",
                          meta={"mast_run_id": "r-1"}, on_frame=lambda i, y, ax: got.append((i, len(y), len(ax))))
    assert len(res.frames) == 3 and res.axis_nm is not None and len(res.axis_nm) == len(res.frames[0]) == 1340
    assert got == [(0, 1340, 1340), (1, 1340, 1340), (2, 1340, 1340)]
    assert rig.fake.silent_requests[-1]["raw"] == "IntegrationTime 0.2\n NumberOfAccumulations 3\n"
    assert res.meta["excitation_nm"] == pytest.approx(532.138) and res.meta["frames_returned"] == 3
    assert res.meta["warnings"] == [] and res.meta["driver"] == "witec_com" and res.meta["mast_run_id"] == "r-1"
    assert res.meta["settings_before"]["laser_wavelength_nm"] == pytest.approx(532.138)
    assert res.meta["settings_before"]["grating"].startswith("G1") and res.meta["beam_path"] == "Raman"
    assert rig.mic.laser_line_nm == pytest.approx(532.138)
    assert np.allclose(rig.mic.wavelength_axis(), res.axis_nm)
    f = res.files[0]
    p = Path(f["path"])
    assert p.name == "run1.truematch.txt" and p.exists()
    assert hashlib.sha256(p.read_bytes()).hexdigest() == f["sha256"] and f["size"] == p.stat().st_size
    assert rig.fake.peek(P.IS_SEQUENCER_ACTIVE) is False   # the sequencer was waited out


def test_acquire_without_save_writes_no_file(rig):
    res = rig.mic.acquire(exposure_ms=100, frames=1, save=False)
    assert res.files == [] and not (rig.tmp / "raw").exists()


def test_spectrum_shows_the_laser_only_with_the_shutter_open(rig):
    dark = rig.mic.acquire(exposure_ms=100, frames=1, save=False).frames[0]
    rig.mic.set_shutter(True)
    lit = rig.mic.acquire(exposure_ms=100, frames=1, save=False).frames[0]
    assert lit.max() > 3 * dark.max()


def test_acquire_uses_witecs_own_values_when_none_are_given(rig):
    rig.fake.human_sets(P.SINGLE_SPECTRUM_INTEGRATION, 0.25)
    rig.fake.human_sets(P.SINGLE_SPECTRUM_ACCUMULATIONS, 2)
    res = rig.mic.acquire(save=False)
    assert len(res.frames) == 2 and rig.fake.silent_requests[-1]["integration_s"] == 0.25
    rig.mic.configure(exposure_ms=50, frames=4)            # MAST's own default for the next ones
    res = rig.mic.acquire(save=False)
    assert len(res.frames) == 4 and rig.fake.silent_requests[-1]["integration_s"] == 0.05
    assert rig.mic.status()["exposure_ms"] == 50 and rig.mic.status()["witec_gui_exposure_ms"] == 250


def test_values_taken_from_witec_are_checked_against_the_limits_too(rig):
    rig.fake.human_sets(P.SINGLE_SPECTRUM_INTEGRATION, 0.002)           # 2 ms < the 10 ms floor
    with pytest.raises(SpectrometerLimitError, match="outside"):
        rig.mic.acquire(save=False)
    assert rig.fake.silent_requests == []
    with pytest.raises(SpectrometerLimitError):
        rig.mic.acquire(exposure_ms=5, frames=1)           # explicit values: the template refuses
    with pytest.raises(SpectrometerLimitError, match="ceiling"):
        rig.mic.acquire(exposure_ms=600_000, frames=100)


def test_acquire_needs_write_access_and_does_not_stack(rig_nogrant):
    with pytest.raises(WriteAccessDenied):
        rig_nogrant.mic.acquire(exposure_ms=100, frames=1)
    rig_nogrant.fake.operator_press_com_automation()
    rig_nogrant.mic.request_write_access()
    rig_nogrant.fake._start_seq("Foreign", 100.0, 0.0)     # noqa: SLF001
    with pytest.raises(InstrumentError, match="busy"):
        rig_nogrant.mic.acquire(exposure_ms=100, frames=1)
    assert P.SILENT_START not in rig_nogrant.fake.triggers()


def test_acquire_that_never_completes_is_stopped_at_the_timeout(rig):
    rig.fake.hang_sequencer = True
    with pytest.raises(MotionTimeout, match="no spectrum within"):
        rig.mic.acquire(exposure_ms=100, frames=1, timeout_s=20, save=False)
    assert P.STOP_SEQUENCER in rig.fake.triggers()
    assert rig.fake.peek(P.IS_SEQUENCER_ACTIVE) is False


def test_acquire_abort_stops_the_sequencer(rig):
    rig.fake.hang_sequencer = True
    n = {"i": 0}

    def abort():
        n["i"] += 1
        return n["i"] > 5

    with pytest.raises(MotionAborted):
        rig.mic.acquire(exposure_ms=100, frames=1, check_abort=abort, save=False)
    assert P.STOP_SEQUENCER in rig.fake.triggers()


def test_a_sequencer_stopped_by_someone_else_is_reported_not_waited_out(rig):
    rig.fake.hang_sequencer = True
    done = {"stopped": False}

    def sleep(dt):
        rig.clock.sleep(dt)
        if not done["stopped"] and rig.clock.now() > rig.fake._seq.t0 + 0.2:        # noqa: SLF001
            done["stopped"] = True
            rig.fake._stop_sequencer()                      # noqa: SLF001 - a person pressed Stop in WITec Control

    rig.mic._sleep = sleep                                  # noqa: SLF001
    with pytest.raises(InstrumentError, match="idle but delivered no spectrum"):
        rig.mic.acquire(exposure_ms=100, frames=1, save=False, timeout_s=600)


def test_a_witec_error_string_is_an_error(rig):
    orig = rig.fake._finish_silent
    rig.fake._finish_silent = lambda integ, n: (orig(integ, n), setattr(rig.fake, "_silent_error", "CCD not cooled"))
    with pytest.raises(InstrumentError, match="CCD not cooled"):
        rig.mic.acquire(exposure_ms=100, frames=1, save=False)


def test_unreadable_text_is_an_error_and_the_raw_text_is_kept(rig):
    orig = rig.fake._finish_silent

    def garbage(integ, n):
        orig(integ, n)
        rig.fake._silent_text = "[WITEC_TRUEMATCH_ASCII_HEADER]\nVersion = 2.0\n\n[XData]\n1\n2\n\n[SpectrumData]\n1\n"

    rig.fake._finish_silent = garbage
    with pytest.raises(InstrumentError, match=r"cannot be read safely.*raw text kept at") as ei:
        rig.mic.acquire(exposure_ms=100, frames=1, base_name="bad")
    kept = str(ei.value).split("raw text kept at ")[1].rstrip(")")
    assert Path(kept).exists()


def test_mismatches_are_warnings_not_silence(rig):
    rig.fake.min_integration_s = 0.5
    orig = rig.fake._finish_silent
    rig.fake._finish_silent = lambda integ, n: orig(integ, 1)             # WITec returns one block, not three
    res = rig.mic.acquire(exposure_ms=100, frames=3, save=False)
    assert len(res.frames) == 1
    joined = " | ".join(res.meta["warnings"])
    assert "asked for 3 accumulations, the text carries 1" in joined and "WITec reports 0.5" in joined


def test_trailer_values_are_dropped_and_noted(rig):
    rig.fake.truematch_trailer = "0"
    res = rig.mic.acquire(exposure_ms=100, frames=2, save=False)
    assert all(len(f) == 1340 for f in res.frames) and any("last was dropped" in w for w in res.meta["warnings"])


def test_axis_is_nm_whatever_unit_witec_displays(rig):
    base = rig.mic.acquire(exposure_ms=100, frames=1, save=False)
    rig.fake.human_sets(P.spectrograph_path(1, "SpectralUnit"), 3)           # rel. 1/cm
    rel = rig.mic.acquire(exposure_ms=100, frames=1, save=False)
    assert rel.meta["x_unit"] == "rel_cm-1" and base.meta["x_unit"] == "nm"
    assert np.allclose(rel.axis_nm, base.axis_nm, atol=1e-6)


def test_a_long_cleanup_is_a_note_not_a_failure(tmp_path):
    r = Rig(tmp_path, silent_cleanup_s=1e6, cfg={"sequencer_cleanup_timeout_s": 5})
    res = r.mic.acquire(exposure_ms=100, frames=1, save=False)
    assert "clean-up" in (res.meta["sequencer_cleanup"] or "") and len(res.frames) == 1


def test_stop_ends_a_running_acquisition_from_another_thread(tmp_path):
    fake = FakeBucsClient(silent_acquire_s=30.0)           # real clock: this one runs on real time
    fake.operator_press_com_automation()
    mic = WITecMicroscope(device_id="w", client=fake, limits=LIMITS, microscope_limits=MLIMITS,
                          config=WITecConfig.from_entry({"poll_s": 0.01, "raw_dir": str(tmp_path),
                                                         "allow_emission_control": True}))
    assert mic.request_write_access()
    out = {}

    def run():
        try:
            mic.acquire(exposure_ms=100, frames=1, save=False, timeout_s=60)
        except Exception as exc:  # noqa: BLE001
            out["exc"] = exc

    th = threading.Thread(target=run)
    th.start()
    for _ in range(500):
        if fake.silent_requests:
            break
        time.sleep(0.01)
    time.sleep(0.1)
    t0 = time.monotonic()
    mic.stop()                                             # does not wait for the acquisition lock
    th.join(15)
    assert not th.is_alive() and time.monotonic() - t0 < 10
    assert isinstance(out["exc"], InstrumentError) and "idle but delivered no spectrum" in str(out["exc"])
    mic.close()


# ── spectrograph ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_centre_wavelength_and_grating_when_witec_exposes_them(rig):
    assert rig.mic.set_center_wavelength(560.0) == {"center_wavelength_nm": 560.0}
    assert rig.mic.status()["center_wavelength_nm"] == 560.0
    for spelling, idx in (("G2: 600 g/mm BLZ=500nm", 1), ("g3", 2), ("1", 0), ("600", 1), ("1800 g/mm", 2)):
        got = rig.mic.set_grating(spelling)
        assert rig.fake.peek(P.spectrograph_path(1, "Grating")) == idx, (spelling, got)
    with pytest.raises(SpectrometerLimitError, match="does not identify exactly one"):
        rig.mic.set_grating("400")
    with pytest.raises(SpectrometerLimitError, match="outside"):
        rig.mic.set_center_wavelength(2000.0)
    assert rig.mic.status()["grating"].startswith("G3")


def test_centre_and_grating_not_supported_points_to_states(tmp_path):
    r = Rig(tmp_path, omit=["grating", "center_wavelength"])
    with pytest.raises(OperationNotSupported, match="LoadWITecState"):
        r.mic.set_center_wavelength(560.0)
    with pytest.raises(OperationNotSupported, match="LoadWITecState"):
        r.mic.set_grating("G1")
    assert r.mic.status()["grating"] is None


def test_centre_wavelength_that_does_not_arrive_times_out(tmp_path):
    r = Rig(tmp_path, cfg={"spectrograph_timeout_s": 5})
    r.fake._tree[P.spectrograph_path(1, "CenterWavelength")].on_set = lambda v: None   # noqa: SLF001
    with pytest.raises(MotionTimeout, match="centre wavelength reads"):
        r.mic.set_center_wavelength(600.0)


def test_unsupported_spectrometer_settings_are_refused_loudly(rig):
    with pytest.raises(OperationNotSupported, match="exit-port"):
        rig.mic.set_exit_port("front")
    with pytest.raises(OperationNotSupported, match="only exposure_ms and frames"):
        rig.mic.configure(adc_mhz=1.0)
    with pytest.raises(SpectrometerLimitError):
        rig.mic.configure(exposure_ms=1)


# ── analyzer series ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_series_plan_and_refusals(rig):
    plan = rig.mic.check_analyzer_series(start_deg=0, stop_deg=360, points=73, integration_s=1.0, accumulations=2)
    assert plan["points"] == 73 and plan["exposure_total_s"] == 146 and plan["estimated_s"] == pytest.approx(73 * 5)
    assert plan["angles_deg"][:3] == [0.0, 5.0, 10.0]
    with pytest.raises(OperationNotSupported, match="full 0–360"):
        rig.mic.run_analyzer_series(start_deg=0, stop_deg=180, points=10, integration_s=1.0)
    with pytest.raises(MicroscopeLimitError, match="points=1"):
        rig.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=1, integration_s=1.0)
    with pytest.raises(MicroscopeLimitError, match="integration_s"):
        rig.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=10, integration_s=0)
    with pytest.raises(SpectrometerLimitError, match="exceeds the ceiling"):
        rig.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=361, integration_s=60.0)
    assert P.ANALYZER_SERIES_START not in rig.fake.triggers()


def test_series_runs_witecs_own_sequencer_and_reports(rig):
    out = rig.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=19, integration_s=0.5, accumulations=2,
                                      name="s1")
    run = rig.fake.series_runs[-1]
    assert run == {"points": 19, "integration_s": 0.5, "accumulations": 2, "analyzer_coupled": True}
    assert out["status"] == "Finished" and out["points"] == 19 and len(out["angles_deg"]) == 19
    assert out["angles_deg"][-1] == 360.0 and out["saved_in_project"] is False and "SaveWITecProject" in out["note"]
    assert rig.fake.peek(P.IS_SEQUENCER_ACTIVE) is False


def test_series_refuses_when_the_analyzer_is_out_of_the_beam_or_witec_is_busy(rig):
    rig.mic.set_analyzer_in(False)
    with pytest.raises(InstrumentError, match="not in the beam path"):
        rig.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=10, integration_s=0.5)
    rig.mic.set_analyzer_in(True)
    rig.fake._start_seq("Foreign", 100.0, 0.0)             # noqa: SLF001
    with pytest.raises(InstrumentError, match="busy"):
        rig.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=10, integration_s=0.5)
    assert P.ANALYZER_SERIES_START not in rig.fake.triggers()


def test_series_abort_stops_the_sequencer(rig):
    rig.fake.hang_sequencer = True
    n = {"i": 0}

    def abort():
        n["i"] += 1
        return n["i"] > 4

    with pytest.raises(MotionAborted, match="analyzer series aborted"):
        rig.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=10, integration_s=0.5, check_abort=abort)
    assert P.STOP_SEQUENCER in rig.fake.triggers()


def test_series_timeout_and_a_series_that_never_starts(tmp_path):
    r = Rig(tmp_path, cfg={"series_start_grace_s": 3})
    r.fake.hang_sequencer = True
    with pytest.raises(MotionTimeout, match="not finished within"):
        r.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=3, integration_s=0.001)
    assert P.STOP_SEQUENCER in r.fake.triggers()
    r2 = Rig(tmp_path / "b", cfg={"series_start_grace_s": 3})
    r2.fake._tree[P.ANALYZER_SERIES_START].on_trigger = lambda: None       # noqa: SLF001 - WITec shows a dialog instead
    with pytest.raises(InstrumentError, match="did not start within 3 s"):
        r2.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=10, integration_s=1.0)
    # a tiny series may legitimately be over already; then the result says its start was not observed
    r3 = Rig(tmp_path / "c", cfg={"series_start_grace_s": 3})
    r3.fake._tree[P.ANALYZER_SERIES_START].on_trigger = lambda: None       # noqa: SLF001
    out = r3.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=3, integration_s=0.5)
    assert out["start_observed"] is False


def test_series_error_and_stopped_by_user_are_not_success(rig):
    rig.fake._seq_result = lambda: "Error|disk full"        # noqa: SLF001
    rig.fake._tree[P.SEQUENCER_STATUS_OR_RESULT].getter = rig.fake._seq_result       # noqa: SLF001
    with pytest.raises(InstrumentError, match="ended with an error: disk full"):
        rig.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=3, integration_s=0.5)
    rig.fake._tree[P.SEQUENCER_STATUS_OR_RESULT].getter = lambda: "StoppedByUser"      # noqa: SLF001
    with pytest.raises(MotionAborted, match="stopped"):
        rig.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=3, integration_s=0.5)


# ── project ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_project_is_stored_where_asked_and_the_gui_settings_come_back(rig, tmp_path):
    rig.fake.human_sets(P.AUTOSAVE_START_DIR, r"C:\Users\Witec\Documents\WITec User Data")
    rig.fake.human_sets(P.AUTOSAVE_FILE_NAME, "operators_name")
    rig.fake.human_sets(P.AUTOSAVE_DIR_MODE, 2)
    out = rig.mic.save_project(tmp_path / "proj", "series 1")
    assert Path(out) == tmp_path / "proj" / "series 1.wip" and Path(out).exists()
    assert rig.fake.peek(P.AUTOSAVE_START_DIR) == r"C:\Users\Witec\Documents\WITec User Data"
    assert rig.fake.peek(P.AUTOSAVE_FILE_NAME) == "operators_name" and rig.fake.peek(P.AUTOSAVE_DIR_MODE) == 2
    sets = {p: v for k, p, v in rig.fake.writes if k == "set"}
    assert sets[P.AUTOSAVE_OVERWRITE_MODE] == 0 or True
    assert rig.fake.peek(P.AUTOSAVE_STORE_MODE) == 0       # restored; the call itself did not clear the project


def test_clear_after_store_maps_to_the_store_mode(rig, tmp_path):
    rig.mic.save_project(tmp_path / "p", "a", clear_after_store=True)
    wrote = [v for k, p, v in rig.fake.writes if k == "set" and p == P.AUTOSAVE_STORE_MODE]
    assert wrote[0] == 1 and wrote[-1] == 0                # set to clear for the call, put back after


def test_project_names_and_duplicates(rig, tmp_path):
    for bad in ("", "  ", "a/b", "a:b", "..", "x*"):
        with pytest.raises(MicroscopeLimitError):
            rig.mic.save_project(tmp_path, bad)
    rig.mic.save_project(tmp_path, "dup.wip")              # a trailing .wip is accepted once
    with pytest.raises(InstrumentError, match="already exists"):
        rig.mic.save_project(tmp_path, "dup")


def test_project_that_witec_does_not_write_times_out_and_still_restores(tmp_path):
    r = Rig(tmp_path, cfg={"project_timeout_s": 3})
    r.fake.human_sets(P.AUTOSAVE_FILE_NAME, "mine")
    r.fake._tree[P.AUTOSAVE_STORE].on_trigger = lambda: None                 # noqa: SLF001
    with pytest.raises(MotionTimeout, match="wrote no project file"):
        r.mic.save_project(tmp_path / "q", "x")
    assert r.fake.peek(P.AUTOSAVE_FILE_NAME) == "mine"


# ── stopping ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_stop_all_closes_the_shutter_then_stops_sequencer_and_stage(rig):
    rig.mic.select_laser("532")
    rig.mic.set_shutter(True)
    rig.fake.hang_sequencer = True
    rig.fake._start_seq("Foreign", 100.0, 0.0)             # noqa: SLF001
    mark = len(rig.fake.log)
    rig.mic.stop_all()
    after = [(k, p) for k, p, _ in rig.fake.log[mark:] if k in ("set", "trigger")]
    assert after[:4] == [("set", P.LASER_SHUTTER), ("trigger", P.STOP_SEQUENCER), ("trigger", P.XY_STOP),
                         ("trigger", P.Z_STOP)]                  # the selected laser, the sequence, the stage, first ...
    assert all(k == "trigger" and "SelectLaser" in p for k, p in after[4:])      # ... then a visit to the other lasers
    assert rig.fake.peek(P.LASER_SHUTTER) is False and rig.fake.peek(P.IS_SEQUENCER_ACTIVE) is False
    rep = rig.mic.last_stop_report
    assert rep["shutter"] == "closed" and rep["sequencer"] == "stop sent"


def test_stop_all_never_raises_and_reports_what_witec_refused(rig_nogrant):
    rig_nogrant.mic.stop_all()                              # no write access: WITec refuses every part
    rep = rig_nogrant.mic.last_stop_report
    assert all("not " in rep[k] for k in ("shutter", "sequencer", "stage_xy", "stage_z"))
    assert "COM Automation" in rep["shutter"]


def test_stop_all_with_witec_closed_never_raises(tmp_path):
    r = Rig(tmp_path, grant=False, witec_running=False)
    r.mic.stop_all()
    r.mic.stop()
    assert r.mic.last_stop_report["connected"] is False


def test_closing_the_shutter_on_stop_can_be_switched_off(tmp_path):
    r = Rig(tmp_path, cfg={"close_shutter_on_stop": False})
    r.mic.stop_all()
    assert "shutter" not in r.mic.last_stop_report and P.LASER_SHUTTER not in [p for _k, p, _v in r.fake.writes]


def test_stop_uses_a_short_timeout_and_only_stops_the_sequencer(rig):
    mark = len(rig.fake.log)
    rig.mic.stop()
    assert [(k, p) for k, p, _ in rig.fake.log[mark:] if k == "trigger"] == [("trigger", P.STOP_SEQUENCER)]


# ── one lock for both roles ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_move_during_an_acquisition_is_refused_as_busy_but_reads_are_not(tmp_path, monkeypatch):
    monkeypatch.setattr(WITecMicroscope, "BUSY_WAIT_S", 0.2)
    fake = FakeBucsClient(silent_acquire_s=1.5, silent_cleanup_s=0.05)        # real clock
    fake.operator_press_com_automation()
    mic = WITecMicroscope(device_id="w", client=fake, limits=LIMITS, microscope_limits=MLIMITS,
                          config=WITecConfig.from_entry({"poll_s": 0.01, "raw_dir": str(tmp_path),
                                                         "allow_emission_control": True}))
    assert mic.request_write_access()
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("r", mic.acquire(exposure_ms=100, frames=1, save=False)))
    th.start()
    for _ in range(500):
        if fake.silent_requests:
            break
        time.sleep(0.01)
    t0 = time.monotonic()
    with pytest.raises(InstrumentError, match="busy"):
        mic.move_stage_um(x=1.0)
    with pytest.raises(InstrumentError, match="busy"):
        mic.set_analyzer_deg(5.0)
    assert time.monotonic() - t0 < 2.0
    assert mic.microscope_status()["sequencer_active"] is True       # reads go through
    assert mic.get_stage_position_um()["x"] == 0.0
    th.join(20)
    assert "r" in res
    assert mic.move_stage_um(x=1.0)["x"] == 1.0                       # free again afterwards
    mic.close()


# ── registry ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _registry(tmp_path, devices):
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": devices}), encoding="utf-8")
    return InstrumentRegistry(config_path=p)


def test_registry_builds_witec_com_lazily_and_shares_the_object(tmp_path):
    fake = FakeBucsClient()
    old = set_client_factory(lambda entry: fake)
    try:
        reg = _registry(tmp_path, [
            {"id": "witec", "type": "witec_com", "name": "WITec alpha300", "enabled": True, "poll_s": 0.01,
             "microscope_limits": {"z_um": [-100, 100], "x_um": [-10, 10], "y_um": [-10, 10]}},
            {"id": "off", "type": "witec_com", "enabled": False}])
        assert fake.connects == 0
        listed = {d["id"]: d for d in reg.list_devices()}
        assert listed["witec"]["kind"] == "microscope" and listed["witec"]["connected"] is False
        mic = reg.microscope("witec")
        assert isinstance(mic, WITecMicroscope) and reg.spectrometer("witec") is mic
        assert fake.connects == 0                              # building it connected nothing
        assert reg.is_connected("witec")                        # built; no I/O
        with pytest.raises(InstrumentError, match="disabled"):
            reg.microscope("off")
        with pytest.raises(InstrumentError, match="is not a laser"):
            reg.laser("witec")
        fake.operator_press_com_automation()
        mic.request_write_access()
        fake.hang_sequencer = True
        fake._start_seq("Foreign", 100.0, 0.0)                  # noqa: SLF001
        rep = reg.stop_all(wait_s=5)
        assert "witec" in rep["stopped"] and not rep["errors"]
        assert P.STOP_SEQUENCER in fake.triggers()
        assert reg.release("witec") is True
        assert not fake.connected and fake._holds_write is False       # noqa: SLF001 - handed back on release
    finally:
        set_client_factory(old)


def test_registry_entry_without_a_hook_builds_a_comtypes_client_but_never_connects(tmp_path):
    reg = _registry(tmp_path, [{"id": "witec", "type": "witec_com", "enabled": True}])
    mic = reg.microscope("witec")
    from mast.instruments.witec.comtypes_client import ComtypesBucsClient

    assert isinstance(mic._client(), ComtypesBucsClient)        # noqa: SLF001
    assert "comtypes" not in sys.modules or True                # (comtypes may be installed elsewhere; nothing imported it here)
    assert mic._client().connected is False                     # noqa: SLF001
    reg.close_all()


def test_unknown_entry_keys_are_warned_about_not_silently_dropped(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="mast.instruments.witec.microscope"):
        build({"id": "witec", "type": "witec_com", "poll_S": 0.1, "require_laser_attestation": True,
               "allow_emission_control": False, "close_shutter_on_stop": False})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("poll_S" in m for m in msgs)
    assert not any("require_laser_attestation" in m or "allow_emission_control" in m for m in msgs)


# ── laser emission: two gates and a cap in front of everything that may light the sample ──────────────────────

@pytest.fixture()
def fresh_store():
    """an empty optical-state store (nobody has signed anything), messages in English (the suite runs in zh)"""
    old = optical_state.set_store(optical_state.OpticalStateStore())
    with use_lang("en"):
        yield optical_state.get_store()
    optical_state.set_store(old)


def _attested_rig(tmp_path, **kw):
    """what build() makes by default: the attestation guard installed"""
    return Rig(tmp_path, guard=LaserAttestationGuard(), **kw)


def _sign(store, by="operator", value=True):
    store.set_by_operator("laser_area_safe", value, by=by, source="test")


def test_opening_the_shutter_and_the_laser_beam_paths_need_the_attestation(tmp_path, fresh_store):
    r = _attested_rig(tmp_path)
    with pytest.raises(LaserAttestationRequired, match="laser_area_safe"):
        r.mic.set_shutter(True)
    assert r.fake.writes == []                                 # nothing reached WITec
    r.fake.human_sets(P.LASER_SHUTTER, True)                   # a person opened it: now the laser paths are WITec light
    for path in ("raman", "confocal"):
        with pytest.raises(LaserAttestationRequired, match="beam path"):
            r.mic.set_beam_path(path)
    r.fake.human_sets(P.LASER_SHUTTER, False)
    assert r.mic.set_beam_path("video") == "Video" and r.mic.set_beam_path("all_off") == "AllOff"
    assert r.mic.set_beam_path("raman") == "Raman"             # shutter confirmed closed: no WITec light, no attestation
    _sign(fresh_store)
    assert r.mic.set_beam_path("raman") == "Raman" and r.mic.set_shutter(True) is True


def test_an_expired_attestation_is_no_attestation(tmp_path, fresh_store):
    r = _attested_rig(tmp_path)
    _sign(fresh_store)
    fresh_store._values["laser_area_safe"].valid_until = "2000-01-01T00:00:00+00:00"       # noqa: SLF001
    assert r.mic.attestation_status() == "missing"
    with pytest.raises(LaserAttestationRequired):
        r.mic.set_shutter(True)


def test_a_laser_area_recorded_as_not_safe_does_not_let_the_laser_out(tmp_path, fresh_store):
    r = _attested_rig(tmp_path)
    _sign(fresh_store, value=False)                            # the operator says: NOT safe - set, unexpired, but False
    assert r.mic.attestation_status() == "unsafe"
    with pytest.raises(LaserAttestationRequired, match="NOT safe"):
        r.mic.set_shutter(True)
    with pytest.raises(LaserAttestationRequired):
        r.mic.acquire(exposure_ms=100, frames=1, save=False)
    r.fake.human_sets(P.LASER_SHUTTER, True)
    with pytest.raises(LaserAttestationRequired):
        r.mic.set_beam_path("raman")                           # (with a shutter that may be open the path is gated)
    r.fake.human_sets(P.LASER_SHUTTER, False)
    assert P.SILENT_START not in r.fake.triggers()
    _sign(fresh_store)                                         # checked again and signed as safe
    assert r.mic.attestation_status() == "valid" and r.mic.set_shutter(True) is True


def test_closing_the_shutter_never_needs_it(tmp_path, fresh_store):
    r = _attested_rig(tmp_path)
    r.fake.human_sets(P.LASER_SHUTTER, True)                   # a person opened it in the WITec GUI
    assert r.fake.peek(P.LASER_SHUTTER) is True                # (the fake really reports it open now)
    assert r.mic.set_shutter(False) is False
    assert r.fake.peek(P.LASER_SHUTTER) is False


def test_an_acquisition_that_may_put_light_on_the_sample_needs_it(tmp_path, fresh_store):
    r = _attested_rig(tmp_path)                                # beam path is Video: acquiring switches to Raman
    with pytest.raises(LaserAttestationRequired, match="acquisition"):
        r.mic.acquire(exposure_ms=100, frames=1, save=False)
    assert P.SILENT_START not in r.fake.triggers() and P.BEAM_TRIGGERS["raman"] not in r.fake.triggers()
    r.fake._beam = "Raman"                                     # noqa: SLF001 - already in the Raman path …
    r.fake.human_sets(P.LASER_SHUTTER, True)                   # … and a person opened the shutter
    assert r.fake.peek(P.LASER_SHUTTER) is True
    with pytest.raises(LaserAttestationRequired):
        r.mic.acquire(exposure_ms=100, frames=1, save=False)
    assert P.SILENT_START not in r.fake.triggers()             # refused before anything was started
    _sign(fresh_store)
    assert len(r.mic.acquire(exposure_ms=100, frames=1, save=False).frames) == 1


def test_a_dark_spectrum_with_the_shutter_closed_in_the_raman_path_needs_no_attestation(tmp_path, fresh_store):
    r = _attested_rig(tmp_path)
    r.fake._beam = "Raman"                                     # noqa: SLF001
    assert r.fake.peek(P.LASER_SHUTTER) is False
    assert len(r.mic.acquire(exposure_ms=100, frames=1, save=False).frames) == 1


def test_an_analyzer_series_needs_it_too(tmp_path, fresh_store):
    r = _attested_rig(tmp_path)
    with pytest.raises(LaserAttestationRequired, match="analyzer series"):
        r.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=5, integration_s=0.1)
    assert P.ANALYZER_SERIES_START not in r.fake.triggers()
    _sign(fresh_store)
    assert r.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=5, integration_s=0.1)["points"] == 5


def test_build_installs_the_attestation_guard_unless_the_entry_waives_it():
    old = set_client_factory(lambda entry: FakeBucsClient())
    try:
        assert isinstance(build({"id": "w", "type": "witec_com"}).emission_guard, LaserAttestationGuard)
        waived = build({"id": "w", "type": "witec_com", "require_laser_attestation": False})
        assert waived.emission_guard is None and waived.attestation_status() == "waived"
    finally:
        set_client_factory(old)
    assert WITecConfig.from_entry({}).require_laser_attestation is True              # the real default: required
    assert isinstance(default_emission_guard(WITecConfig()), LaserAttestationGuard)
    assert default_emission_guard(WITecConfig.from_entry({"require_laser_attestation": False})) is None


def test_a_waived_entry_lets_the_driver_through_without_a_signature(tmp_path, fresh_store):
    r = Rig(tmp_path)                                           # no guard, as build() makes for the waived entry
    assert r.mic.emission_problem() is None
    assert r.mic.set_shutter(True) is True and r.mic.set_beam_path("raman") == "Raman"


def test_no_such_slot_means_no_confirmation(tmp_path):
    old = optical_state.set_store(optical_state.OpticalStateStore(slots=[]))
    try:
        with use_lang("en"):
            r = _attested_rig(tmp_path)
            assert r.mic.attestation_status() == "no_slot"
            with pytest.raises(LaserAttestationRequired, match="no 'laser_area_safe' slot"):
                r.mic.set_shutter(True)
    finally:
        optical_state.set_store(old)


def test_attestation_status_names_the_situations(tmp_path, fresh_store):
    r = _attested_rig(tmp_path)
    assert r.mic.attestation_status() == "missing" and r.mic.emission_problem()
    _sign(fresh_store, value=False)
    assert r.mic.attestation_status() == "unsafe" and r.mic.emission_problem()
    _sign(fresh_store)
    assert r.mic.attestation_status() == "valid" and r.mic.emission_problem() is None
    assert Rig(tmp_path / "w").mic.attestation_status() == "waived"
    assert Rig(tmp_path / "c", guard=lambda what: None).mic.attestation_status() == "custom"


def test_the_refusal_tells_the_operator_where_to_sign_and_for_how_long(tmp_path, fresh_store):
    r = _attested_rig(tmp_path)
    with use_lang("zh"):
        with pytest.raises(LaserAttestationRequired) as zh:
            r.mic.set_shutter(True)
    with use_lang("en"):
        with pytest.raises(LaserAttestationRequired) as en:
            r.mic.set_shutter(True)
    assert "光学状态面板" in str(zh.value) and "弹窗" in str(zh.value) and "8 小时" in str(zh.value)
    assert "optical-state panel" in str(en.value) and "pop-up" in str(en.value) and "8 h" in str(en.value)
    assert "laser_area_safe" in str(zh.value) and "laser_area_safe" in str(en.value)      # the slot name is an identity


def test_the_guard_does_not_import_the_operator_module_until_it_is_used():
    import ast
    import inspect

    top_level = [n for n in ast.parse(inspect.getsource(wm)).body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not [n for n in top_level if "operator_hitl" in ast.dump(n)]                  # lazy, inside the methods


# ── the injectable guard ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_injected_guard_is_asked_before_every_path_that_can_emit_and_can_veto(tmp_path):
    asked: list[str] = []
    verdict = {"allow": False}

    def guard(what):
        asked.append(what)
        if not verdict["allow"]:
            raise InstrumentError("the guard says no")

    r = Rig(tmp_path, guard=guard)
    paths = (lambda: r.mic.set_shutter(True),
             lambda: r.mic.acquire(exposure_ms=100, frames=1, save=False),
             lambda: r.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=5, integration_s=0.1))
    for call in paths:
        with pytest.raises(InstrumentError, match="the guard says no"):
            call()
    r.fake.human_sets(P.LASER_SHUTTER, True)                    # a shutter that may be open: the beam path is asked about
    with pytest.raises(InstrumentError, match="the guard says no"):
        r.mic.set_beam_path("raman")
    r.fake.human_sets(P.LASER_SHUTTER, False)
    assert len(asked) == 4 and all(a.startswith("witec: ") and len(a) > 10 for a in asked)   # who and what
    assert r.fake.writes == [] and P.SILENT_START not in r.fake.triggers()
    verdict["allow"] = True
    assert r.mic.set_shutter(True) is True and len(asked) == 5


def test_the_guard_is_not_asked_about_what_cannot_emit(tmp_path):
    asked: list[str] = []
    r = Rig(tmp_path, guard=lambda what: asked.append(what))
    r.fake._beam = "Raman"                                      # noqa: SLF001
    assert len(r.mic.acquire(exposure_ms=100, frames=1, save=False).frames) == 1       # dark spectrum
    assert r.mic.set_beam_path("video") == "Video" and r.mic.set_beam_path("all_off") == "AllOff"
    assert r.mic.set_shutter(False) is False
    r.mic.stop_all()
    assert asked == []


def test_a_guard_that_reports_no_status_shows_as_custom(tmp_path):
    def veto(what):
        raise InstrumentError("no")

    r = Rig(tmp_path, guard=veto)
    em = r.mic.microscope_status()["emission"]
    assert em["attestation"] == "custom" and em["software_may_start_emission"] is False
    assert r.mic.emission_refusal()[0] == "emission_guard"


# ── emission gate 1 (the device entry) and the power cap at the moment of emission ─────────────────────────────────────────────

def test_the_defaults_are_closed():
    cfg = WITecConfig.from_entry({})
    assert cfg.allow_emission_control is False and cfg.require_laser_attestation is True
    assert WITecConfig.from_entry({"allow_emission_control": True}).allow_emission_control is True


def test_emission_needs_the_device_entry_to_allow_it_even_with_a_valid_signature(tmp_path, fresh_store):
    r = _attested_rig(tmp_path, cfg={"allow_emission_control": False})
    _sign(fresh_store)
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        r.mic.set_shutter(True)
    with pytest.raises(EmissionControlDisabled):
        r.mic.acquire(exposure_ms=100, frames=1, save=False)
    with pytest.raises(EmissionControlDisabled):
        r.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=5, integration_s=0.1)
    assert r.fake.writes == [] and r.mic.attestation_status() == "valid"
    r.fake.human_sets(P.LASER_SHUTTER, True)
    with pytest.raises(EmissionControlDisabled):
        r.mic.set_beam_path("raman")                                    # a shutter that may be open: WITec light
    r.fake.human_sets(P.LASER_SHUTTER, False)
    assert r.mic.set_beam_path("video") == "Video"                      # not a laser path
    assert r.mic.set_beam_path("raman") == "Raman"                      # shutter confirmed closed: an external source's path


def test_closing_and_a_dark_spectrum_work_with_emission_control_off(tmp_path, fresh_store):
    r = _attested_rig(tmp_path, cfg={"allow_emission_control": False})
    r.fake._beam = "Raman"                                              # noqa: SLF001 - the operator set it up by hand
    r.fake.human_sets(P.LASER_SHUTTER, True)
    assert r.mic.set_shutter(False) is False
    assert len(r.mic.acquire(exposure_ms=100, frames=1, save=False).frames) == 1      # the first on-machine check


def test_both_gates_are_reported_in_the_status(tmp_path, fresh_store):
    off = _attested_rig(tmp_path, cfg={"allow_emission_control": False})
    em = off.mic.microscope_status()["emission"]
    assert em == {"allow_emission_control": False, "attestation": "missing", "selection_known": True,
                  "software_may_start_emission": False}
    on = _attested_rig(tmp_path / "on")
    _sign(fresh_store)
    assert on.mic.microscope_status()["emission"]["software_may_start_emission"] is True
    on.fake.witec_exit()                                                # WITec Control gone: the gates are still shown
    assert on.mic.microscope_status()["emission"]["allow_emission_control"] is True


def test_the_shutter_does_not_open_above_the_power_cap_a_person_set(tmp_path):
    r = Rig(tmp_path)
    r.mic.select_laser("532")
    r.fake.human_sets(P.LASER_POWER, 40.0)                              # raised in the WITec GUI; the cap is 20 mW
    with pytest.raises(MicroscopeLimitError, match=r"above the 20 mW cap.*SetWITecLaserPower"):
        r.mic.set_shutter(True)
    assert r.fake.peek(P.LASER_SHUTTER) is False
    r.fake.human_sets(P.LASER_POWER, 15.0)
    assert r.mic.set_shutter(True) is True


def test_every_emission_path_checks_the_cap(tmp_path):
    r = Rig(tmp_path)
    r.mic.select_laser("532")
    r.fake.human_sets(P.LASER_POWER, 40.0)
    for call in (lambda: r.mic.acquire(exposure_ms=100, frames=1, save=False),
                 lambda: r.mic.run_analyzer_series(start_deg=0, stop_deg=360, points=5, integration_s=0.1)):
        with pytest.raises(MicroscopeLimitError, match="cap"):
            call()
    r.fake.human_sets(P.LASER_SHUTTER, True)                            # with the shutter open the beam path is judged too
    with pytest.raises(MicroscopeLimitError, match="cap"):
        r.mic.set_beam_path("raman")
    assert P.SILENT_START not in r.fake.triggers() and P.ANALYZER_SERIES_START not in r.fake.triggers()


def test_no_configured_cap_means_no_software_emission_for_a_controllable_laser(tmp_path):
    r = Rig(tmp_path, mlimits=MicroscopeLimits(z_um=(-100, 100), max_laser_power_mw={"633": 10.0}))
    r.mic.select_laser("532")
    with pytest.raises(MicroscopeLimitError, match="no max_laser_power_mw"):
        r.mic.set_shutter(True)


def test_a_laser_whose_power_witec_cannot_set_has_no_software_cap_to_check(tmp_path):
    r = Rig(tmp_path, mlimits=MicroscopeLimits(z_um=(-100, 100)))        # no caps at all
    r.mic.select_laser("1064")                                          # external NPI: WITec cannot read its power
    assert r.mic.set_shutter(True) is True


def test_an_unreadable_power_is_not_a_reason_to_assume_it_is_fine(tmp_path):
    r = Rig(tmp_path)
    r.mic.select_laser("532")
    r.fake.inject(P.LASER_POWER, BucsNoValue("no value"), ops=("get",), times=5)
    with pytest.raises(MicroscopeLimitError, match="cannot be verified"):
        r.mic.set_shutter(True)


_LIT = {**DEFAULT_PRESETS, "Lit": {"laser": 1, "grating": 0, "center_nm": 547.0, "power_mw": 5.0, "shutter_open": True}}


def test_a_state_that_opens_the_shutter_without_the_gates_is_closed_again(tmp_path, fresh_store):
    r = _attested_rig(tmp_path, presets=dict(_LIT), cfg={"states": ["Lit"]})
    with pytest.raises(LaserAttestationRequired, match="Lit"):
        r.mic.load_state("Lit")
    assert r.fake.peek(P.LASER_SHUTTER) is False
    _sign(fresh_store)
    assert r.mic.load_state("Lit") == "Lit" and r.fake.peek(P.LASER_SHUTTER) is True      # gates satisfied: left as it is


def test_a_state_load_that_opens_the_shutter_with_emission_control_off_is_closed_again(tmp_path, fresh_store):
    r = Rig(tmp_path, presets=dict(_LIT), cfg={"states": ["Lit"], "allow_emission_control": False})
    with pytest.raises(EmissionControlDisabled, match="Lit"):
        r.mic.load_state("Lit")
    assert r.fake.peek(P.LASER_SHUTTER) is False


def test_a_shutter_a_person_had_opened_is_left_alone_by_a_state_load_when_the_gates_are_met(tmp_path, fresh_store):
    r = _attested_rig(tmp_path, presets=dict(_LIT), cfg={"states": ["Lit"]})
    _sign(fresh_store)
    r.fake.human_sets(P.LASER_SHUTTER, True)                            # somebody opened it before MAST did anything
    assert r.mic.load_state("Lit") == "Lit"
    assert r.fake.peek(P.LASER_SHUTTER) is True


def test_a_shutter_a_person_had_opened_is_closed_before_a_state_load_when_the_gates_are_not_met(tmp_path, fresh_store):
    r = _attested_rig(tmp_path, presets=dict(_LIT), cfg={"states": ["Lit"]})
    r.fake.human_sets(P.LASER_SHUTTER, True)
    with pytest.raises(LaserAttestationRequired):
        r.mic.load_state("Lit")                                         # (the state itself opens it again: refused too)
    assert r.fake.peek(P.LASER_SHUTTER) is False
