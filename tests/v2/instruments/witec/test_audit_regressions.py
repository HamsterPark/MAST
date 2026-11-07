"""Regression tests for the WITec driver's emission gates and close paths.

Each test pins the fix of one gap and is written against the *public* API, the way the gap was reproduced
(scratch tests PASSED while the gap existed; these are their reverse): they were red before the fix.

Principle behind the fixes: anything that makes MORE light on the sample goes through the emission check - also dark ->
bright, also a power raise while the shutter is open, also a laser swap that brings an open shutter along; anything that
makes it darker or closes something is never refused by policy. A state that cannot be read counts as "may emit".
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

import json
import os
import threading
import time

import pytest

from mast.i18n import use_lang
from mast.instruments.base import MotionAborted
from mast.instruments.laser import EmissionControlDisabled, LaserAttestationRequired
from mast.instruments.microscope import MicroscopeLimitError, WriteAccessDenied
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.instruments.witec import microscope as wm
from mast.instruments.witec import params as P
from mast.instruments.witec.bucs import BucsNoValue
from mast.instruments.witec.comtypes_client import ComtypesBucsClient
from mast.instruments.witec.fake_bucs import DEFAULT_LASERS, DEFAULT_PRESETS, FakeBucsClient, VirtualClock
from mast.operator_hitl import optical_state
from tests.v2.instruments.witec.fake_com import ComWorld

STATES = list(wm.DEFAULT_STATES)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    old = optical_state.set_store(optical_state.OpticalStateStore())
    with use_lang("en"):
        yield
    optical_state.set_store(old)


def attest(value: bool = True) -> None:
    optical_state.get_store().set_by_operator("laser_area_safe", value, by="operator", source="test")


class TimedFake(FakeBucsClient):
    """Records ``(time, shutter_open, selected_laser, what)`` whenever a shutter can change."""

    def __init__(self, *a, **kw):
        self.events: list[tuple[float, bool, int, str]] = []
        super().__init__(*a, **kw)

    def _set_shutter(self, value):
        super()._set_shutter(value)
        self.events.append((self._clock(), bool(self._shutter[self._selected]), self._selected, "shutter"))

    def _set_state_name(self, name):
        super()._set_state_name(name)
        self.events.append((self._clock(), bool(self._shutter[self._selected]), self._selected, "state"))


def make_mic(*, allow: bool = True, caps=None, states=None, fake=None, grant: bool = True, realtime: bool = False,
             **entry_extra):
    """A WITecMicroscope built the way the registry builds it (default guard included) on a fake WITec Control.
    Time is virtual unless ``realtime`` (the few threaded tests)."""
    clock = VirtualClock()
    fake = fake or TimedFake(clock=clock.now, operator_allows_write=True)
    if not realtime and getattr(fake, "_clock", None) is not clock.now:
        fake._clock = clock.now                                    # noqa: SLF001
    entry = {"id": "witec", "type": "witec_com", "enabled": True, "allow_emission_control": allow,
             "poll_s": 0.01, "stage_poll_s": 0.01, "project_timeout_s": 3.0, "states": states or STATES,
             "microscope_limits": {"x_um": [-25000, 25000], "y_um": [-25000, 25000], "z_um": [-100, 100],
                                   "max_step_um": 5000,
                                   "max_laser_power_mw": caps if caps is not None else {"532": 20, "633": 10}},
             **entry_extra}
    old = wm.set_client_factory(lambda e: fake)
    try:
        mic = wm.build(entry)
    finally:
        wm.set_client_factory(old)
    if realtime:
        mic._now, mic._sleep = time.monotonic, time.sleep          # noqa: SLF001
    else:
        mic._now, mic._sleep = clock.now, clock.sleep              # noqa: SLF001
    if grant:
        fake.operator_press_com_automation()
        assert mic.request_write_access()
    return mic, fake, clock


# ═════════════════════════ the close direction must work ══════════════════════════════════════════════════════════


def test_S3a_F5_release_closes_the_shutter_first_and_the_close_paths_keep_working():
    mic, fake, _ = make_mic()
    attest()
    assert mic.set_shutter(True) is True
    mic.release_write_access()                                  # = ReleaseWITecWriteAccess (token-free)
    assert fake.gui_locked, "the person's COM Automation grant still stands"
    assert fake.peek(P.LASER_SHUTTER) is False, "release must close the shutter BEFORE write access goes back"
    assert mic.set_shutter(True) is True                        # (write access comes back for the open)
    mic.release_write_access()
    assert mic.has_write_access() is False
    assert mic.set_shutter(False) is False                      # = CloseWITecShutter, after a release: one silent request
    mic.stop_all()                                              # = StopWITec
    assert mic.last_stop_report["shutter"] == "closed"


def test_S3a_stop_and_close_get_write_access_back_silently_when_the_grant_stands():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    mic.release_write_access()
    fake.human_sets(P.LASER_SHUTTER, True)                      # it is open again (a person, or another client)
    assert mic.has_write_access() is False and fake.gui_locked
    mic.stop_all()
    assert mic.last_stop_report["shutter"] == "closed" and fake.peek(P.LASER_SHUTTER) is False
    fake.human_sets(P.LASER_SHUTTER, True)
    mic.release_write_access()
    assert fake.peek(P.LASER_SHUTTER) is False


def test_S3a_a_close_that_cannot_be_confirmed_is_reported_not_swallowed():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    fake.operator_revoke()                                      # the person pressed Revoke: nothing can be sent
    with pytest.raises(WriteAccessDenied, match="NOT closed"):
        mic.set_shutter(False)
    mic.stop_all()                                              # never raises
    rep = mic.last_stop_report
    assert rep["shutter"].startswith("not closed") and "COM Automation" in rep["shutter"]
    assert fake.peek(P.LASER_SHUTTER) is True                   # and nobody pretends otherwise


def test_S3b_F18_closing_works_while_an_exposure_runs_and_does_not_wait_for_it():
    fake = FakeBucsClient(clock=time.monotonic, operator_allows_write=True, silent_acquire_s=2.5)
    mic, fake, _ = make_mic(fake=fake, realtime=True)
    attest()
    mic.set_shutter(True)
    box: dict = {}
    th = threading.Thread(target=lambda: box.update(r=_try(lambda: mic.acquire(exposure_ms=10, frames=1, save=False))),
                          daemon=True)
    th.start()
    for _ in range(300):
        if fake.silent_requests:
            break
        time.sleep(0.01)
    assert fake.silent_requests and th.is_alive(), "the exposure is running"
    t0 = time.monotonic()
    assert mic.set_shutter(False) is False                      # = CloseWITecShutter / SetWITecShutter(open=false)
    assert time.monotonic() - t0 < 1.5, "the close must not queue behind the exposure"
    assert fake.peek(P.LASER_SHUTTER) is False and th.is_alive()
    th.join(15)
    assert not th.is_alive() and not isinstance(box.get("r"), BaseException), box


def _try(fn):
    try:
        return fn()
    except BaseException as exc:  # noqa: BLE001
        return exc


def test_S3b_the_com_thread_can_queue_the_close_during_a_long_exposure():
    """The same, through the real client class on a COM-shaped fake: every COM call goes through ONE dedicated
    thread, so the question is whether the exposure's polling leaves room for the close. It does: the exposure is a
    series of short calls with sleeps between them, never one long call."""
    backend = FakeBucsClient(clock=time.monotonic, operator_allows_write=True, silent_acquire_s=2.5)
    world = ComWorld(backend)
    client = ComtypesBucsClient(bundle_factory=world.bundle, call_timeout_s=3.0, connect_timeout_s=10.0)
    entry_cfg = wm.WITecConfig.from_entry({"allow_emission_control": True, "poll_s": 0.02})
    from mast.instruments.microscope import MicroscopeLimits
    from mast.instruments.spectrometer import SpectrometerLimits

    mic = wm.WITecMicroscope(device_id="w", client=client, config=entry_cfg,
                             limits=SpectrometerLimits(max_total_s=7200),
                             microscope_limits=MicroscopeLimits(max_laser_power_mw={"532": 20.0}))
    try:
        assert mic.request_write_access()
        mic.select_laser("532")
        assert mic.set_shutter(True) is True
        th = threading.Thread(target=lambda: _try(lambda: mic.acquire(exposure_ms=10, frames=1, save=False)),
                              daemon=True)
        th.start()
        for _ in range(300):
            if backend.silent_requests:
                break
            time.sleep(0.01)
        t0 = time.monotonic()
        assert mic.set_shutter(False) is False
        assert time.monotonic() - t0 < 1.5 and backend.peek(P.LASER_SHUTTER) is False
        assert len({tid for _, tid in world.calls}) == 1        # still one COM thread
        th.join(15)
    finally:
        mic.close()


def test_S3b_a_stop_that_lands_while_an_open_is_on_its_way_wins():
    mic, fake, _ = make_mic()
    attest()
    mic.emission_guard = lambda what: mic.stop_all()            # the stop arrives after the gates, before the write
    with pytest.raises(MotionAborted, match="stop"):
        mic.set_shutter(True)
    assert fake.peek(P.LASER_SHUTTER) is False


def test_S3c_F6_close_closes_the_shutter_before_it_gives_write_access_back():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    mic.close()                                                 # registry.release / DisconnectOpticalDevice / shutdown
    assert fake.peek(P.LASER_SHUTTER) is False, "no light may be left behind when the connection is dropped"
    assert fake._holds_write is False                           # noqa: SLF001


def _registry_with_a_witec(tmp_path):
    clock = VirtualClock()
    fake = TimedFake(clock=clock.now, operator_allows_write=True, work_dir=tmp_path)
    old = wm.set_client_factory(lambda entry: fake)
    entry = {"id": "witec", "type": "witec_com", "name": "WITec", "enabled": True, "allow_emission_control": True,
             "poll_s": 0.01, "microscope_limits": {"max_laser_power_mw": {"532": 20, "633": 10}}}
    cfg = tmp_path / "optical_instruments.json"
    cfg.write_text(json.dumps({"devices": [entry]}), encoding="utf-8")
    reg = InstrumentRegistry(cfg)
    reset_instrument_registry(reg)
    mic = reg.microscope("witec")
    mic._now, mic._sleep = clock.now, clock.sleep                  # noqa: SLF001
    fake.operator_press_com_automation()
    assert mic.request_write_access()
    return reg, mic, fake, old


def test_S3c_F6_registry_release_and_close_all_leave_no_light(tmp_path):
    reg, mic, fake, old = _registry_with_a_witec(tmp_path)
    try:
        attest()
        mic.set_shutter(True)
        assert reg.release("witec") is True                         # DisconnectOpticalDevice
        assert fake.peek(P.LASER_SHUTTER) is False and fake._holds_write is False       # noqa: SLF001
        mic2 = reg.microscope("witec")                              # the next use reconnects; light on again, then shut down
        mic2._now, mic2._sleep = mic._now, mic._sleep               # noqa: SLF001
        assert mic2.set_shutter(True) is True
        reg.close_all()                                             # the application shuts down
        assert fake.peek(P.LASER_SHUTTER) is False
    finally:
        reset_instrument_registry(None)
        wm.set_client_factory(old)


def test_S3c_the_registrys_panic_stop_closes_every_lasers_shutter(tmp_path):
    reg, mic, fake, old = _registry_with_a_witec(tmp_path)
    try:
        attest()
        mic.set_shutter(True)
        mic.select_laser("633")
        rep = reg.stop_all(wait_s=10)                               # E-STOP / shutdown hook
        assert "witec" in rep["stopped"] and not rep["errors"]
        assert not any(fake._shutter)                               # noqa: SLF001
    finally:
        reset_instrument_registry(None)
        wm.set_client_factory(old)


def test_S3c_close_does_not_connect_to_a_witec_it_never_talked_to():
    fake = FakeBucsClient(operator_allows_write=True)
    mic = wm.WITecMicroscope(device_id="w", client=fake, config=wm.WITecConfig.from_entry({"allow_emission_control": True}))
    assert not fake.connected                                   # the client exists, nobody has used it
    mic.close()                                                 # the registry closes every device it built at shutdown
    assert fake.connects == 0 and fake.log == [] and not fake.connected


# ═════════════════════════ a power raise with the shutter open ═══════════════════════════════════════════════════


def test_S4_F1_raising_power_while_the_shutter_is_open_goes_through_both_gates():
    mic, fake, _ = make_mic(allow=False)
    fake.human_sets(P.LASER_POWER, 0.0)
    fake.human_sets(P.LASER_SHUTTER, True)                      # a person left it open before pressing COM Automation
    with pytest.raises(EmissionControlDisabled):
        mic.set_shutter(True)                                   # the shutter path refuses ...
    with pytest.raises(EmissionControlDisabled):
        mic.set_laser_power_mw(20.0)                            # ... and so must the power path
    assert fake.peek(P.LASER_POWER) == 0.0


def test_S4_with_the_gates_met_a_raise_is_fine_and_a_lowering_is_never_gated():
    mic, fake, _ = make_mic(allow=False)
    fake.human_sets(P.LASER_POWER, 15.0)
    fake.human_sets(P.LASER_SHUTTER, True)
    assert mic.set_laser_power_mw(5.0) == 5.0                   # dimmer: allowed with every gate shut
    with pytest.raises(EmissionControlDisabled):
        mic.set_laser_power_mw(6.0)                             # brighter: refused
    mic2, fake2, _ = make_mic(allow=True)
    attest()
    fake2.human_sets(P.LASER_POWER, 1.0)
    fake2.human_sets(P.LASER_SHUTTER, True)
    assert mic2.set_laser_power_mw(12.0) == 12.0                # gates met: allowed (cap 20)


def test_S4_an_unreadable_shutter_counts_as_open_for_a_raise():
    mic, fake, _ = make_mic(allow=False)
    fake.inject(P.LASER_SHUTTER, BucsNoValue("no value"), ops=("get",), times=20)
    with pytest.raises(EmissionControlDisabled):
        mic.set_laser_power_mw(5.0)                             # from 1.0 mW: a raise, state unknown
    fake._inject.clear()                                        # noqa: SLF001
    assert mic.set_laser_power_mw(5.0) == 5.0                   # shutter confirmed closed: dark preparation, no gate


# ═════════════════════════ swapping lasers ═══════════════════════════════════════════════════════════════════════


def _633_left_open(fake, mw=30.0):
    fake._select_laser(2)                                       # noqa: SLF001
    fake.human_sets(P.LASER_POWER, mw)
    fake.human_sets(P.LASER_SHUTTER, True)
    fake._select_laser(1)                                       # noqa: SLF001


def test_S5_F2_selecting_a_laser_whose_shutter_is_open_is_judged_and_the_shutter_closed_when_refused():
    mic, fake, _ = make_mic(allow=False)
    _633_left_open(fake)
    assert fake.peek(P.LASER_SHUTTER) is False
    with pytest.raises(EmissionControlDisabled):
        mic.select_laser("633")
    assert fake._selected == 2 and fake._shutter[2] is False    # noqa: SLF001 - the new laser's shutter was closed


def test_S5_a_selected_laser_over_its_cap_is_refused_and_closed_even_with_the_gates_met():
    mic, fake, _ = make_mic(allow=True)
    attest()
    _633_left_open(fake, mw=30.0)                               # cap 10
    with pytest.raises(MicroscopeLimitError, match="cap"):
        mic.select_laser("633")
    assert fake._shutter[2] is False                            # noqa: SLF001


def test_S5_a_selected_laser_whose_shutter_cannot_be_read_counts_as_possibly_open():
    from mast.instruments.witec.microscope import ShutterNotClosed

    mic, fake, _ = make_mic(allow=False)
    fake.inject(P.LASER_SHUTTER, BucsNoValue("no value"), ops=("get",), times=200)
    with pytest.raises(ShutterNotClosed) as ei:                 # judged (gates shut) - and the close cannot be confirmed either
        mic.select_laser("633")
    assert isinstance(ei.value.__cause__, EmissionControlDisabled)


def test_S5_a_selected_laser_that_arrives_open_within_gates_and_cap_is_accepted():
    mic, fake, _ = make_mic(allow=True)
    attest()
    _633_left_open(fake, mw=5.0)
    assert mic.select_laser("633")["name"] == "633 nm"
    assert fake._shutter[2] is True                             # noqa: SLF001 - judged, allowed, left as it is


def test_S5_F2b_stop_all_closes_every_lasers_shutter_and_keeps_the_selection():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)                                       # 532 open, legitimately
    mic.select_laser("633")                                     # an agent switches lasers
    mic.stop_all()
    assert mic.last_stop_report["shutter"] == "closed"
    assert not any(fake._shutter), "no shutter may stay open after StopWITec reported 'closed'"      # noqa: SLF001
    assert fake._selected == 2                                  # noqa: SLF001 - and the selection is where it was
    assert mic.last_stop_report["shutters"]["closed"] and not mic.last_stop_report["shutters"]["failed"]


@pytest.mark.parametrize("select_closes", [False, True])
def test_S5_Qb1_a_stop_is_right_whichever_way_selecting_a_laser_treats_the_old_shutter(select_closes):
    fake = TimedFake(clock=VirtualClock().now, operator_allows_write=True)
    fake.select_closes_previous_shutter = select_closes         # WITec's behaviour is not documented: both must work
    mic, fake, _ = make_mic(fake=fake)
    attest()
    mic.set_shutter(True)
    fake.human_sets(P.LASER_SHUTTER, True)
    mic.select_laser("633")
    fake.human_sets(P.LASER_SHUTTER, True)                      # (a person opens the 633 as well, when it can still be open)
    mic.stop_all()
    assert not any(fake._shutter) and mic.last_stop_report["shutter"] == "closed"      # noqa: SLF001
    assert fake._selected == 2                                  # noqa: SLF001


def test_S5_close_all_shutters_leaves_other_lasers_alone_while_a_sequencer_runs():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    mic.select_laser("633")
    fake._start_seq("Someone's measurement", 100.0, 0.0)        # noqa: SLF001
    mark = len(fake.log)
    rep = mic.close_all_shutters()
    assert fake._selected == 2 and fake._shutter[2] is False    # noqa: SLF001
    assert fake._shutter[1] is True, "swapping lasers under a running sequence is not done"      # noqa: SLF001
    assert not [e for e in fake.log[mark:] if e[0] == "trigger" and "SelectLaser" in e[1]]
    assert rep["skipped"]
    mic.stop_all()                                              # the real stop sweeps (it stops the sequencer first)
    assert not any(fake._shutter)                               # noqa: SLF001


def test_S5_the_sweep_can_be_switched_off_and_a_stop_without_shutter_handling_touches_none():
    mic, fake, _ = make_mic(close_every_laser=False)
    attest()
    mic.set_shutter(True)
    mic.select_laser("633")
    mic.stop_all()
    assert fake._shutter[1] is True                             # noqa: SLF001 - documented: only the selected laser
    mic2, fake2, _ = make_mic(close_shutter_on_stop=False)
    attest()
    mic2.set_shutter(True)
    mic2.stop_all()
    assert fake2.peek(P.LASER_SHUTTER) is True and "shutter" not in mic2.last_stop_report


# ═════════════════════════ loading a state ═══════════════════════════════════════════════════════════════════════


def _preset_fake(**presets):
    clock = VirtualClock()
    return TimedFake(clock=clock.now, operator_allows_write=True, presets=dict(DEFAULT_PRESETS, **presets))


def test_S6_F3a_a_shutter_the_state_opens_is_closed_within_the_poll_interval_not_after_the_settle_time():
    fake = _preset_fake(Danger={"laser": 1, "shutter_open": True, "power_mw": 5.0})
    mic, fake, _ = make_mic(allow=False, states=STATES + ["Danger"], fake=fake)
    with pytest.raises(EmissionControlDisabled):
        mic.load_state("Danger")
    opened = [t for t, open_, _s, what in fake.events if what == "state" and open_]
    closed = [t for t, open_, _s, what in fake.events if what == "shutter" and not open_]
    assert opened and closed
    assert closed[-1] - opened[0] <= 0.25, "light on for the whole settle time (3 s) with the gates shut"
    assert fake.peek(P.LASER_SHUTTER) is False


def test_S6_a_shutter_that_opens_late_in_the_settle_is_still_caught_within_the_poll_interval():
    fake = _preset_fake()
    mic, fake, clock = make_mic(allow=False, fake=fake)
    sched = {"t": clock.now() + 1.0, "done": False}             # WITec opens it one second into the load, on the clock
    tree = fake._tree[P.LASER_SHUTTER]                          # noqa: SLF001
    real_getter = tree.getter

    def getter():
        if not sched["done"] and clock.now() >= sched["t"]:
            sched["done"] = True
            fake._shutter[fake._selected] = True                # noqa: SLF001
            fake.events.append((sched["t"], True, fake._selected, "late-open"))      # (stamped with when it happened)
        return real_getter()

    tree.getter = getter
    with pytest.raises(EmissionControlDisabled):
        mic.load_state("RayShield 532 - G600")
    opened = next(t for t, open_, _s, what in fake.events if what == "late-open")
    closed = [t for t, open_, _s, what in fake.events if what == "shutter" and not open_]
    assert closed and closed[-1] - opened <= 0.25, "light stayed on until the settle time was over"
    assert fake.peek(P.LASER_SHUTTER) is False


def test_S6_a_shutter_that_cannot_be_read_after_the_load_counts_as_possibly_open():
    mic, fake, _ = make_mic(allow=False)
    real = fake._tree[P.LASER_SHUTTER].getter                       # noqa: SLF001
    state = {"loaded": False}

    def getter():
        if state["loaded"]:
            raise BucsNoValue("no value")                           # unreadable once the state is in
        return real()

    fake._tree[P.LASER_SHUTTER].getter = getter                     # noqa: SLF001
    on_set = fake._tree[P.STATE_NAME].on_set                        # noqa: SLF001
    fake._tree[P.STATE_NAME].on_set = lambda name: (on_set(name), state.update(loaded=True))      # noqa: SLF001
    from mast.instruments.witec.microscope import ShutterNotClosed

    with pytest.raises(ShutterNotClosed, match="could not be closed again") as ei:
        mic.load_state("RayShield 532 - G600")      # refused ... and the close cannot be confirmed either (unreadable)
    assert isinstance(ei.value.__cause__, EmissionControlDisabled)


def test_S6_a_shutter_that_was_open_before_the_load_is_closed_first_when_the_gates_are_not_met():
    fake = _preset_fake()
    mic, fake, _ = make_mic(allow=False, fake=fake)
    fake.human_sets(P.LASER_SHUTTER, True)                      # open on the 532, gates shut
    mic.load_state("RayShield 532 - G600")                      # a harmless state: no refusal needed ...
    assert fake.peek(P.LASER_SHUTTER) is False                  # ... but the open shutter was closed before the load
    first_write = next(e for e in fake.log if e[0] == "set")
    assert first_write[1] == P.LASER_SHUTTER and first_write[2] is False


def test_S6_F3b_a_state_that_leaves_a_laser_without_a_cap_open_is_refused_and_closed():
    fake = _preset_fake(**{"633 open": {"laser": 2, "shutter_open": True, "power_mw": 30.0}})
    mic, fake, _ = make_mic(allow=True, caps={"532": 20}, states=STATES + ["633 open"], fake=fake)
    attest()
    with pytest.raises(MicroscopeLimitError, match="no max_laser_power_mw"):
        mic.load_state("633 open")
    assert fake.peek(P.LASER_SHUTTER) is False
    with pytest.raises(MicroscopeLimitError, match="no max_laser_power_mw"):
        mic.set_shutter(True)                                   # the shutter path says the same


def test_S6_F3b_an_unreadable_power_after_a_load_is_not_waved_through():
    fake = _preset_fake(**{"532 lit": {"laser": 1, "shutter_open": True, "power_mw": 5.0}})
    mic, fake, _ = make_mic(allow=True, states=STATES + ["532 lit"], fake=fake)
    attest()
    fake.inject(P.LASER_POWER, BucsNoValue("no value"), ops=("get",), times=50)
    with pytest.raises(MicroscopeLimitError, match="cannot be verified"):
        mic.load_state("532 lit")
    assert fake.peek(P.LASER_SHUTTER) is False


def test_S6_F3c_the_laser_the_state_selected_is_the_one_that_is_judged():
    fake = _preset_fake(**{"633 open": {"laser": 2, "shutter_open": True, "power_mw": 5.0}})
    mic, fake, _ = make_mic(allow=True, states=STATES + ["633 open"], fake=fake)
    attest()
    mic.set_shutter(True)                                       # 532 open while the area was safe
    attest(False)                                               # the operator: NOT safe now
    with pytest.raises(LaserAttestationRequired):
        mic.load_state("633 open")
    assert fake._shutter[2] is False                            # noqa: SLF001 - the 633 the state opened is shut again


def test_S6_gates_met_and_power_within_the_cap_the_state_may_leave_the_shutter_open():
    fake = _preset_fake(**{"633 open": {"laser": 2, "shutter_open": True, "power_mw": 5.0}})
    mic, fake, _ = make_mic(allow=True, states=STATES + ["633 open"], fake=fake)
    attest()
    assert mic.load_state("633 open") == "633 open"
    assert fake.peek(P.LASER_SHUTTER) is True and fake.peek(P.LASER_POWER) == 5.0


# ═════════════════════════ an unidentified laser ═════════════════════════════════════════════════════════════════


def test_M1_F4_a_selected_laser_that_matches_no_table_entry_never_emits_in_software():
    lasers = [dict(l) for l in DEFAULT_LASERS]
    lasers[1] = {"name": "532 nm", "wavelength_nm": 537.5, "has_shutter": True, "power_readable": True,
                 "power_mw": 45.0}                              # 5.5 nm off the table (laser_match_nm = 3)
    clock = VirtualClock()
    fake = TimedFake(clock=clock.now, operator_allows_write=True, lasers=lasers)
    mic, fake, _ = make_mic(allow=True, fake=fake)
    attest()
    assert not [l for l in mic.list_lasers() if l.get("selected")]
    with pytest.raises(MicroscopeLimitError, match="cannot be identified"):
        mic.set_shutter(True)
    assert fake.peek(P.LASER_SHUTTER) is False
    with pytest.raises(MicroscopeLimitError, match="cannot be identified"):
        mic.acquire(exposure_ms=100, frames=1, save=False)
    assert P.SILENT_START not in fake.triggers()


# ═════════════════════════ an external-source protocol must not need WITec's own emission gate ═══════════════════


def test_M4_F14_the_raman_path_with_the_shutter_confirmed_closed_needs_no_witec_emission_gate():
    mic, fake, _ = make_mic(allow=False)                        # allow_emission_control=false, nothing attested
    assert fake.peek(P.LASER_SHUTTER) is False
    assert mic.set_beam_path("raman") == "Raman"                # no WITec light: the path of an external source
    assert mic.set_beam_path("confocal") == "Confocal"
    fake._beam = "Raman"                                        # noqa: SLF001
    spec = mic.acquire(exposure_ms=100, frames=1, save=False)   # a dark spectrum in the Raman path stays free
    assert len(spec.frames) == 1


def test_M4_an_open_or_unreadable_shutter_is_gated_on_the_beam_switch():
    mic, fake, _ = make_mic(allow=False)
    fake.human_sets(P.LASER_SHUTTER, True)
    with pytest.raises(EmissionControlDisabled):
        mic.set_beam_path("raman")
    fake.human_sets(P.LASER_SHUTTER, False)
    fake.inject(P.LASER_SHUTTER, BucsNoValue("no value"), ops=("get",), times=20)
    with pytest.raises(EmissionControlDisabled):
        mic.set_beam_path("raman")


def test_M4_a_laser_without_a_software_shutter_is_always_on_so_the_switch_is_gated():
    mic, fake, _ = make_mic(allow=False)
    fake._select_laser(0)                                       # noqa: SLF001 - the passive 355 nm line
    with pytest.raises(EmissionControlDisabled):
        mic.set_beam_path("raman")


def test_M4_a_switch_that_makes_witec_open_the_shutter_by_itself_is_closed_again():
    mic, fake, _ = make_mic(allow=False)
    trig = fake._tree[P.BEAM_TRIGGERS["raman"]]                 # noqa: SLF001

    def raman_and_open():
        fake._set_beam("raman")                                 # noqa: SLF001
        fake._shutter[fake._selected] = True                    # noqa: SLF001 - what WITec might do (survey says: unknown)

    trig.on_trigger = raman_and_open
    with pytest.raises(EmissionControlDisabled):
        mic.set_beam_path("raman")
    assert fake.peek(P.LASER_SHUTTER) is False


def test_Q_a_a_dark_spectrum_is_watched_for_a_shutter_that_opens_during_the_sequence():
    mic, fake, _ = make_mic(allow=False)
    fake._beam = "Raman"                                        # noqa: SLF001
    start = fake._tree[P.SILENT_START]                          # noqa: SLF001
    original = start.on_trigger

    def start_and_open():
        original()
        fake._shutter[fake._selected] = True                    # noqa: SLF001 - WITec opens its own shutter for the exposure

    start.on_trigger = start_and_open
    with pytest.raises(EmissionControlDisabled):
        mic.acquire(exposure_ms=100, frames=1, save=False)
    assert fake.peek(P.LASER_SHUTTER) is False
    assert fake.peek(P.IS_SEQUENCER_ACTIVE) is False            # and the sequence was stopped


# ═════════════════════════ where a project may be written ════════════════════════════════════════════════════════


@pytest.fixture
def roots(tmp_path):
    (tmp_path / "root").mkdir(exist_ok=True)
    (tmp_path / "experiments").mkdir(exist_ok=True)
    return tmp_path


def test_M6_F7_save_project_only_writes_under_the_data_roots(roots):
    mic, fake, _ = make_mic()
    inside = roots / "root" / "artifacts" / "projects"
    assert Path(mic.save_project(inside, "p1")).parent == inside
    exp = roots / "experiments" / "e1" / "raw"
    assert Path(mic.save_project(exp, "p2")).parent == exp
    outside = roots / "elsewhere" / "deep" / "dir"
    with pytest.raises(MicroscopeLimitError, match="data root"):
        mic.save_project(outside, "p3")
    assert not (roots / "elsewhere").exists(), "a refused directory must not even be created"


@pytest.mark.parametrize("raw", [
    r"\\evil-host\share\x", "//evil-host/share/x", r"\\?\C:\Windows\Temp", "relative/dir",
    "{root}/artifacts/../../elsewhere", "{root}/artifacts/./../x",
])
def test_M6_F7_unc_dotdot_device_and_relative_directories_are_refused(roots, raw):
    mic, fake, _ = make_mic()
    d = raw.replace("{root}", str(roots / "root"))
    with pytest.raises(MicroscopeLimitError):
        mic.save_project(d, "p")
    assert not (roots / "elsewhere").exists() and not fake.saved_projects


def test_M6_a_directory_that_resolves_outside_through_a_link_is_refused(roots):
    outside = roots / "elsewhere"
    outside.mkdir()
    link = roots / "root" / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a symlink here (no privilege)")
    mic, fake, _ = make_mic()
    with pytest.raises(MicroscopeLimitError, match="data root"):
        mic.save_project(link / "sub", "p")
    assert not (outside / "sub").exists()


# ═════════════════════════ a move needs a readable position ══════════════════════════════════════════════════════


def test_L3_a_move_whose_current_position_cannot_be_read_is_refused():
    mic, fake, _ = make_mic()
    fake.inject(P.XY_CURRENT_X, BucsNoValue("no value"), ops=("get",), times=50)
    with pytest.raises(MicroscopeLimitError, match="cannot be read"):
        mic.move_stage_um(x=4000.0)                             # 4 mm from the true position: no step check possible
    assert fake.peek(P.XY_DESIRED_X) == 0.0 and P.XY_MOVE not in fake.triggers()
    fake._inject.clear()                                        # noqa: SLF001
    assert mic.move_stage_um(x=40.0)["x"] == 40.0


def test_L3_a_virtual_stage_still_says_so():
    from mast.instruments.base import OperationNotSupported

    mic, fake, _ = make_mic()
    fake.human_sets(P.STAGE_IS_VIRTUAL, True)
    with pytest.raises(OperationNotSupported, match="virtual"):
        mic.move_stage_um(x=1.0)


def test_L3_the_simulated_microscope_is_unaffected():
    from mast.instruments.microscope import MicroscopeLimits, SimMicroscope

    sim = SimMicroscope(microscope_limits=MicroscopeLimits(x_um=(-100, 100), y_um=(-100, 100), z_um=(-100, 100)))
    assert sim.move_stage_um(x=5.0)["x"] == 5.0


def test_S5_a_table_with_more_lasers_than_the_machine_has_does_not_make_a_stop_look_incomplete():
    mic, fake, _ = make_mic(lasers=[{"index": i, "name": f"L{i}", "wavelength_nm": w, "power_controllable": pc}
                                    for i, (w, pc) in enumerate(((355.0, False), (532.0, True), (633.0, True),
                                                                 (1064.0, False), (2600.0, False), (800.0, False)))])
    attest()
    mic.set_shutter(True)
    mic.stop_all()                                              # the machine has 5 lasers, the table lists 6
    rep = mic.last_stop_report
    assert rep["shutter"] == "closed" and not rep["shutters"]["failed"] and not any(fake._shutter)       # noqa: SLF001


def test_a_refusal_that_cannot_shut_the_shutter_again_says_so():
    """The person presses Revoke while the laser is being selected: the check says no, the shutter cannot be closed
    (no write access), and the error must not read as "nothing happened"."""
    mic, fake, _ = make_mic(allow=False)
    _633_left_open(fake)
    select = fake._tree[P.laser_select_path(2)]                 # noqa: SLF001
    select.on_trigger = lambda: (fake._select_laser(2), fake.operator_revoke())      # noqa: SLF001
    from mast.instruments.witec.microscope import ShutterNotClosed

    with pytest.raises(ShutterNotClosed, match="could not be closed again") as ei:
        mic.select_laser("633")
    assert "allow_emission_control" in str(ei.value) and isinstance(ei.value.__cause__, EmissionControlDisabled)
    assert fake._shutter[2] is True                             # noqa: SLF001 - still open: the error says so


def test_a_power_overshoot_that_cannot_be_shut_off_says_so():
    mic, fake, _ = make_mic()
    mic.select_laser("532")
    attest()
    mic.set_shutter(True)
    f = fake._tree[P.LASER_POWER]                               # noqa: SLF001 - the laser overshoots 1.5x, and the grant is revoked
    f.on_set = lambda v: (fake._power.__setitem__(fake._selected, v * 1.5), fake.operator_revoke())      # noqa: SLF001
    with pytest.raises(MicroscopeLimitError, match="could NOT be closed"):
        mic.set_laser_power_mw(15.0)


def test_S5_a_stop_also_visits_lasers_the_table_does_not_list():
    mic, fake, _ = make_mic(lasers=[{"index": i, "name": f"L{i}", "wavelength_nm": w, "power_controllable": pc}
                                    for i, (w, pc) in enumerate(((355.0, False), (532.0, True), (633.0, True)))])
    attest()
    mic.set_shutter(True)
    fake._select_laser(3)                                       # noqa: SLF001 - the machine has a 1064 the table does not list
    fake.human_sets(P.LASER_SHUTTER, True)
    fake._select_laser(1)                                       # noqa: SLF001
    mic.stop_all()
    assert not any(fake._shutter), "the 1064's shutter was left open"       # noqa: SLF001
    assert mic.last_stop_report["shutter"] == "closed" and fake._selected == 1       # noqa: SLF001
