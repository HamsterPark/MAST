"""Regression tests for the WITec driver's stop / close paths.

Same rule as ``test_audit_regressions``: each test pins the fix of one gap, written against the *public* API the way
the gap was reproduced (scratch tests PASSED while the gap existed; these are their reverse), and was
red before the fix.

* a stop visits the lasers by SELECTING them (WITec only exposes the selected laser's shutter), so while it runs a
      shutter must not be opened, a laser not selected, a power not changed, a state not loaded; two stops must not
      visit at the same time.
* the laser that was selected before a stop is put back by index - also when its wavelength cannot be told - and
      when that cannot be done, nothing emits until somebody chooses a laser.
* write access that a close regained only for itself is handed back right after it.
* ``last_stop_report`` carries a machine-readable ``shutter_confirmed``.
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

import contextlib
import threading
import time

import pytest

from mast.i18n import use_lang
from mast.instruments.base import MotionAborted
from mast.instruments.microscope import MicroscopeLimitError
from mast.instruments.witec import params as P
from mast.instruments.witec.bucs import BucsConnectionLost, BucsError, BucsNoValue
from mast.instruments.witec import microscope as wm
from mast.instruments.witec.fake_bucs import DEFAULT_PRESETS, VirtualClock
from mast.instruments.witec.microscope import ShutterNotClosed
from mast.operator_hitl import optical_state
from tests.v2.instruments.witec.test_audit_regressions import TimedFake, attest, make_mic


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    old = optical_state.set_store(optical_state.OpticalStateStore())
    with use_lang("en"):
        yield
    optical_state.set_store(old)


# ── harness ─────────────────────────────────────────────────────────────────────────────────────────────────────────


class Race:
    """Runs *action* on its own thread at the moment a stop's sweep has laser index *at* selected - from inside
    ``_shutter_state``, which every visit of the sweep calls. The thread is joined before the sweep goes on, so what
    the action did (or was refused) is settled when the sweep looks at the next laser."""

    def __init__(self, mic, fake, *, at: int, action):
        self.mic, self.fake, self.at, self.action = mic, fake, at, action
        self.on = False
        self.fired = False
        self.box: dict = {}
        self._real = mic._shutter_state                         # noqa: SLF001
        mic._shutter_state = self._state                        # noqa: SLF001

    def _state(self, cl):
        st = self._real(cl)
        if self.on and not self.fired and self.fake._selected == self.at:       # noqa: SLF001
            self.fired = True

            def work():
                try:
                    self.box["result"] = self.action()
                except BaseException as exc:  # noqa: BLE001 - what the racer was told is the evidence
                    self.box["error"] = exc

            t = threading.Thread(target=work, name="racer")
            t.start()
            t.join(10)
            assert not t.is_alive(), "the racing action hung"
        return st


def _is_stopping_refusal(exc) -> bool:
    """(by type: the message language belongs to the calling thread, and a racer thread has its own)"""
    return isinstance(exc, MotionAborted) and type(exc).__name__ == "StopInProgress"


class BlindFake(TimedFake):
    """A WITec whose ``Laser|Selected|Wavelength`` can be made unreadable (a laser whose line cannot be told)."""

    blind = False

    def _get_raw(self, info):
        if self.blind and info.path == P.LASER_WAVELENGTH:
            raise BucsNoValue("no value (test)")
        return super()._get_raw(info)


TWO = ({"name": "532 nm", "wavelength_nm": 532.138, "has_shutter": True, "power_readable": True, "power_mw": 1.0},
       {"name": "633 nm", "wavelength_nm": 633.0, "has_shutter": True, "power_readable": True, "power_mw": 1.0})
TWO_TABLE = [{"index": 0, "name": "532 nm", "wavelength_nm": 532.0, "power_controllable": True},
             {"index": 1, "name": "633 nm", "wavelength_nm": 633.0, "power_controllable": True}]


def _two_laser_mic(*, selected: int = 1, extra_state: dict | None = None):
    presets = dict(DEFAULT_PRESETS, **({"mine": extra_state} if extra_state else {}))
    fake = BlindFake(clock=VirtualClock().now, operator_allows_write=True, lasers=TWO, presets=presets)
    fake._select_laser(selected)                                # noqa: SLF001
    states = [*wm.DEFAULT_STATES, "mine"] if extra_state else None
    mic, fake, _ = make_mic(fake=fake, lasers=TWO_TABLE, states=states)
    attest()
    return mic, fake


# ═════════════════════════ nothing may slip in while a stop visits the lasers ═════════════════════════════════════


@pytest.mark.parametrize("entry", ["close_all_shutters", "stop_all", "release_write_access"])
def test_N1_G1_a_shutter_open_that_lands_while_the_sweep_has_another_laser_selected_is_refused(entry):
    mic, fake, _ = make_mic()
    attest()
    race = Race(mic, fake, at=3, action=lambda: mic.set_shutter(True))        # 1064 nm is selected by the sweep
    race.on = True
    getattr(mic, entry)()
    assert race.fired
    assert _is_stopping_refusal(race.box.get("error")), race.box
    assert not any(fake._shutter), "no shutter may be open after the stop"                  # noqa: SLF001
    assert fake._selected == 1                                                              # noqa: SLF001 - back on the 532


def test_N1_G1_the_report_of_the_sweep_is_true_for_the_lasers_it_names():
    mic, fake, _ = make_mic()
    attest()
    race = Race(mic, fake, at=3, action=lambda: mic.set_shutter(True))
    race.on = True
    report = mic.close_all_shutters()
    assert report["failed"] == {} and "1064 nm" in report["already_closed"] + report["closed"], report
    assert report["selection_restored"] is True and report["shutter_confirmed"] is True, report
    assert fake._shutter[3] is False                                                        # noqa: SLF001


def test_N1_G1b_the_protocols_own_close_afterwards_leaves_nothing_open():
    """The protocol (it holds the token) opens after the sweep looked at 1064; its own ``set_shutter(False)`` at the end
    of the step closes only the SELECTED laser - so a shutter the sweep missed would stay open for good."""
    mic, fake, _ = make_mic()
    attest()
    race = Race(mic, fake, at=3, action=lambda: mic.set_shutter(True))
    race.on = True
    mic.close_all_shutters()
    race.on = False
    mic.set_shutter(False)                                       # the protocol's cleanup
    assert not any(fake._shutter), "a laser's shutter was left open although every close reported success"   # noqa: SLF001


def test_N1_selecting_a_laser_while_the_sweep_runs_is_refused():
    mic, fake, _ = make_mic()
    attest()
    race = Race(mic, fake, at=3, action=lambda: mic.select_laser("633"))
    race.on = True
    mic.close_all_shutters()
    assert _is_stopping_refusal(race.box.get("error")), race.box
    assert fake._selected == 1                                                              # noqa: SLF001
    assert mic.select_laser("633")["name"] == "633 nm", "and once the stop is over it works again"


def test_N1_changing_the_power_while_the_sweep_runs_is_refused():
    mic, fake, _ = make_mic()
    attest()
    race = Race(mic, fake, at=2, action=lambda: mic.set_laser_power_mw(5.0))        # 633 nm: controllable, cap 10
    race.on = True
    mic.close_all_shutters()
    assert _is_stopping_refusal(race.box.get("error")), race.box
    assert fake._power[2] == 1.0                                                            # noqa: SLF001
    assert mic.select_laser("633") and mic.set_laser_power_mw(5.0) == pytest.approx(5.0)


def test_N1_loading_a_state_while_the_sweep_runs_is_refused():
    mic, fake, _ = make_mic()
    attest()
    race = Race(mic, fake, at=3, action=lambda: mic.load_state("Raman 633 - G300"))
    race.on = True
    mic.close_all_shutters()
    assert _is_stopping_refusal(race.box.get("error")), race.box
    assert fake._selected == 1 and fake._state_name == "RayShield 532 - G300"               # noqa: SLF001


def test_N1_an_acquisition_that_would_start_while_the_sweep_runs_is_refused():
    mic, fake, _ = make_mic()
    attest()
    race = Race(mic, fake, at=3, action=lambda: mic.acquire(exposure_ms=100, frames=1, save=False))
    race.on = True
    mic.close_all_shutters()
    assert _is_stopping_refusal(race.box.get("error")), race.box
    assert not any(fake._shutter)                                                           # noqa: SLF001


def test_N1_switching_to_the_raman_path_while_the_sweep_runs_is_refused():
    mic, fake, _ = make_mic()
    attest()
    race = Race(mic, fake, at=3, action=lambda: mic.set_beam_path("raman"))
    race.on = True
    mic.close_all_shutters()
    assert _is_stopping_refusal(race.box.get("error")), race.box
    assert fake._beam == "Video"                                                            # noqa: SLF001
    assert mic.set_beam_path("raman") == "Raman"


def test_N1_a_dark_beam_path_may_still_be_set_while_the_sweep_runs():
    """Darker is never refused: the video path puts no laser light on the sample."""
    mic, fake, _ = make_mic()
    attest()
    race = Race(mic, fake, at=3, action=lambda: mic.set_beam_path("video"))
    race.on = True
    mic.close_all_shutters()
    assert "error" not in race.box, race.box


def test_N1_an_analyzer_series_that_would_start_while_the_sweep_runs_is_refused():
    mic, fake, _ = make_mic()
    attest()
    mic.set_analyzer_in(True)
    race = Race(mic, fake, at=3, action=lambda: mic.run_analyzer_series(start_deg=0, stop_deg=360, points=3,
                                                                           integration_s=0.1))
    race.on = True
    mic.close_all_shutters()
    assert _is_stopping_refusal(race.box.get("error")), race.box
    assert fake.series_runs == []


def test_N1_a_stop_that_arrives_while_a_laser_is_settling_ends_the_wait():
    mic, fake, _ = make_mic()
    attest()
    real_sleep, fired = mic._sleep, []                           # noqa: SLF001

    def sleep(seconds):
        if not fired:
            fired.append(True)
            mic.stop_all()                                       # a stop from "outside", while select_laser is settling
        real_sleep(seconds)

    mic._sleep = sleep                                           # noqa: SLF001
    with pytest.raises(MotionAborted) as ei:
        mic.select_laser("633")
    assert type(ei.value).__name__ == "StopInProgress"
    assert not any(fake._shutter)                                                           # noqa: SLF001


def test_N1_a_beam_switch_that_a_stop_overtakes_is_given_up():
    mic, fake, _ = make_mic()
    fake.beam_switch_s = 5.0                                     # the switch takes a while: it is polled meanwhile
    real_sleep, fired = mic._sleep, []                           # noqa: SLF001

    def sleep(seconds):
        if not fired:
            fired.append(True)
            mic.stop_all()
        real_sleep(seconds)

    mic._sleep = sleep                                           # noqa: SLF001
    with pytest.raises(MotionAborted) as ei:
        mic.set_beam_path("video")
    assert type(ei.value).__name__ == "StopInProgress"


def test_N1_a_power_that_a_stop_overtook_is_not_read_back_from_another_laser():
    mic, fake, _ = make_mic()
    attest()
    mic.select_laser("633")
    real_sleep, fired = mic._sleep, []                           # noqa: SLF001

    def sleep(seconds):
        if not fired:
            fired.append(True)
            mic.stop_all()                                       # during the power settle
        real_sleep(seconds)

    mic._sleep = sleep                                           # noqa: SLF001
    with pytest.raises(MotionAborted) as ei:
        mic.set_laser_power_mw(5.0)
    assert type(ei.value).__name__ == "StopInProgress"
    assert fake._power[2] == 5.0, "the set itself went to the right laser"                  # noqa: SLF001


def test_N1_a_close_that_arrives_while_the_open_is_being_judged_wins_over_the_open():
    mic, fake, _ = make_mic(realtime=True)
    attest()
    real = mic._require_emission                                 # noqa: SLF001
    box: dict = {}

    def judged_slowly(cl, what):
        real(cl, what)
        if "closer" not in box:
            e0 = mic._stop_epoch                                 # noqa: SLF001
            t = threading.Thread(target=lambda: box.setdefault("closed", mic.set_shutter(False)))
            box["closer"] = t
            t.start()
            end = time.monotonic() + 5
            while mic._stop_epoch == e0 and time.monotonic() < end:         # noqa: SLF001 - until the close has begun
                time.sleep(0.001)

    mic._require_emission = judged_slowly                        # noqa: SLF001
    with pytest.raises(MotionAborted) as ei:
        mic.set_shutter(True)
    assert type(ei.value).__name__ == "StopInProgress"
    box["closer"].join(5)
    assert box["closed"] is False and not any(fake._shutter)                                # noqa: SLF001


@pytest.mark.parametrize("entry", ["close_all_shutters", "make_safe", "set_shutter_false"])
def test_N1_a_stop_that_starts_while_another_is_visiting_waits_for_it_and_reports_true(entry):
    """The second stop's very first act is to close the SELECTED laser: while the first has the passive 355 nm laser
    selected on its way, that is not "the selected laser has no software shutter"."""
    mic, fake, _ = make_mic(realtime=True, laser_settle_s=0.02)
    attest()
    real_state, box = mic._shutter_state, {}                     # noqa: SLF001

    def second():
        try:
            if entry == "close_all_shutters":
                box["report"] = mic.close_all_shutters()
            elif entry == "make_safe":
                box["report"] = mic._make_safe(mic._cl, stage=False)        # noqa: SLF001
            else:
                box["result"] = mic.set_shutter(False)           # the protocol's own close, at the same moment
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    def state(cl):
        st = real_state(cl)
        if fake._selected == 0 and "thread" not in box:          # noqa: SLF001 - the first stop is visiting the 355
            box["thread"] = threading.Thread(target=second)
            box["thread"].start()
            time.sleep(0.3)                                      # long enough for an unserialized second stop to act
        return st

    mic._shutter_state = state                                   # noqa: SLF001
    mic.close_all_shutters()
    mic._shutter_state = real_state                              # noqa: SLF001
    box["thread"].join(30)
    assert "error" not in box, box
    if entry != "set_shutter_false":
        rep = box["report"] if entry == "close_all_shutters" else box["report"]["shutters"]
        assert rep["selected_without_shutter"] == "" and rep["failed"] == {}, rep
        assert "532 nm" in rep["closed"], "it closed the laser that is really selected"
    else:
        assert box["result"] is False
    assert fake._selected == 1 and not any(fake._shutter)                                    # noqa: SLF001


def test_N1_the_refusals_end_with_the_stop_a_shutter_can_be_opened_again():
    mic, fake, _ = make_mic()
    attest()
    mic.close_all_shutters()
    mic.stop_all()
    assert mic.set_shutter(True) is True and fake._shutter[1] is True                       # noqa: SLF001
    assert mic._stopping == 0                                                               # noqa: SLF001 - nothing left "stopping"


def test_N1_a_stop_that_fails_half_way_does_not_leave_the_driver_refusing_for_good():
    mic, fake, _ = make_mic()
    attest()
    fake.inject(P.laser_select_path(2), BucsError("boom"), ops=("trigger",), times=1)
    mic.stop_all()                                               # one visit fails: recorded, never raised
    assert mic._stopping == 0                                                               # noqa: SLF001
    mic.select_laser("532")
    assert mic.set_shutter(True) is True


@pytest.mark.parametrize("entry", ["close_all_shutters", "make_safe"])
def test_N1_two_stops_at_the_same_time_do_not_interleave_their_visits(entry):
    """E-STOP's ``stop_all`` that timed out and ``safe_off_all`` sweeping again; the revocation listener against a human's
    close: the second must not 'restore' the selection onto the laser the first has selected on its way."""
    mic, fake, _ = make_mic(realtime=True, laser_settle_s=0.02)
    attest()
    start = threading.Barrier(2)
    reports: dict = {}

    def sweep(name):
        start.wait(5)
        try:
            if entry == "close_all_shutters":
                reports[name] = mic.close_all_shutters()
            else:
                reports[name] = mic._make_safe(mic._cl, stage=True)["shutters"]        # noqa: SLF001
        except BaseException as exc:  # noqa: BLE001 - a false "not closed" / "no software shutter" is evidence too
            reports[name] = exc

    threads = [threading.Thread(target=sweep, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)
    visits = [p for p in fake.triggers() if "SelectLaser" in p]
    one_sweep = [P.laser_select_path(i) for i in (0, 2, 3, 4)] + [P.laser_select_path(1)]
    assert visits == one_sweep * 2, f"the two sweeps interleaved: {visits}"
    assert fake._selected == 1                                                              # noqa: SLF001
    for name, rep in reports.items():
        assert isinstance(rep, dict), (name, rep)
        assert rep["selection_restored"] is True and rep["failed"] == {}, (name, rep)
        assert rep["selected_without_shutter"] == "", (name, "a laser the OTHER sweep had selected on its way", rep)
        assert "532 nm" in rep["closed"] and "633 nm" in rep["closed"] + rep["already_closed"], (name, rep)


def test_N1_a_later_stop_starts_from_the_selection_the_earlier_one_put_back():
    mic, fake, _ = make_mic()
    attest()
    mic.select_laser("633")
    mic.close_all_shutters()
    rep = mic.close_all_shutters()
    assert fake._selected == 2 and rep["selection_restored"] is True                        # noqa: SLF001
    assert "633 nm" in rep["closed"], "the laser that is selected is the first one a stop closes"


def test_N1_the_close_direction_never_waits_for_the_instrument_lock():
    mic, fake, _ = make_mic(realtime=True)
    attest()
    mic.set_shutter(True)
    done = threading.Event()

    def close():
        mic.close_all_shutters()
        mic.stop_all()
        mic.set_shutter(False)
        done.set()

    with mic._lock:                                              # noqa: SLF001 - an exposure holds it for its whole run
        t = threading.Thread(target=close)
        t.start()
        assert done.wait(5), "a close queued behind the instrument lock"
    t.join(5)
    assert not any(fake._shutter)                                                           # noqa: SLF001


# ═════════════════════════ the laser that was selected is put back ═════════════════════════════════════════════════


def test_N8_an_original_laser_that_the_table_cannot_name_is_put_back_by_its_wavelength():
    mic, fake, _ = make_mic()
    attest()
    fake.laser_table[2]["wavelength_nm"] = 700.0                 # a "633" whose line matches no table entry (±3 nm)
    fake._select_laser(2)                                       # noqa: SLF001
    mic.stop_all()
    rep = mic.last_stop_report["shutters"]
    assert fake._selected == 2, "the stop ended on whichever laser it visited last"         # noqa: SLF001
    assert rep["selection_restored"] is True and rep["selection_basis"] == "by wavelength", rep
    assert not rep["failed"] and not rep["selection_lost"]
    mic.close_all_shutters()
    assert fake._selected == 2                                                              # noqa: SLF001


def test_N8_an_original_laser_whose_wavelength_cannot_be_read_is_put_back_by_the_index_the_driver_remembers():
    fake = BlindFake(clock=VirtualClock().now, operator_allows_write=True)
    mic, fake, _ = make_mic(fake=fake)
    attest()
    mic.select_laser("633")                                      # the driver selected it: it knows the index
    fake.blind = True
    mic.stop_all()
    fake.blind = False
    rep = mic.last_stop_report["shutters"]
    assert fake._selected == 2, "ended on the last laser visited"                           # noqa: SLF001
    assert rep["selection_restored"] is True and "remembered" in rep["selection_basis"], rep
    assert not any(fake._shutter)                                                           # noqa: SLF001


def test_N8_a_remembered_index_that_does_not_read_like_the_original_is_not_trusted():
    mic, fake, _ = make_mic()
    attest()
    fake.laser_table[2]["wavelength_nm"] = 700.0                 # two lasers read 700.0 and 700.3: "by wavelength" is ambiguous
    fake.laser_table[3]["wavelength_nm"] = 700.3
    fake._select_laser(2)                                       # noqa: SLF001
    mic._last_selected_index = 1                                 # noqa: SLF001 - a stale memory (the 532)
    mic.stop_all()
    sh = mic.last_stop_report["shutters"]
    assert sh["selection_restored"] is False and sh["selection_lost"] is True, sh
    assert "reads 532" in sh["note"], "it selected the remembered laser, saw it is not the one that was selected, and said so"
    with pytest.raises(MicroscopeLimitError, match="choose a laser explicitly"):
        mic.set_shutter(True)


def test_N8_a_selection_that_cannot_be_put_back_blocks_emission_until_a_laser_is_chosen():
    mic, fake = _two_laser_mic(selected=0)                       # 532 nm
    mic._last_selected_index = None                              # noqa: SLF001 - this process never identified a laser
    fake.blind = True
    mic.stop_all()
    fake.blind = False
    rep = mic.last_stop_report
    assert rep["shutter"] == "closed" and rep["shutter_confirmed"] is True, "the light IS off; it is the selection that is lost"
    assert rep["shutters"]["selection_restored"] is False and rep["shutters"]["selection_lost"] is True, rep
    assert "explicitly" in rep["shutters"]["note"], rep["shutters"]
    assert fake._selected == 1, "it ended on the last laser it visited (633), nobody's choice"      # noqa: SLF001
    with pytest.raises(MicroscopeLimitError, match="choose a laser explicitly"):
        mic.set_shutter(True)
    assert fake._shutter == [False, False]
    with pytest.raises(MicroscopeLimitError, match="choose a laser explicitly"):
        mic.set_laser_power_mw(2.0)                              # raising: refused ...
    assert mic.set_laser_power_mw(0.5) == pytest.approx(0.5)     # ... lowering is darker, never refused
    assert mic.microscope_status()["emission"]["selection_known"] is False
    mic.select_laser("532")                                      # somebody chooses
    assert mic.microscope_status()["emission"]["selection_known"] is True
    assert mic.set_shutter(True) is True and fake._shutter[0] is True


def test_N8_a_selection_that_fails_to_come_back_is_lost_and_says_so_without_calling_the_shutters_open():
    mic, fake = _two_laser_mic(selected=1)                       # 633 nm; the sweep visits 532 and must come back
    fake.inject(P.laser_select_path(1), BucsError("select failed (test)"), ops=("trigger",), times=1)
    mic.stop_all()
    rep = mic.last_stop_report
    assert rep["shutter"] == "closed" and rep["shutter_confirmed"] is True, rep
    sh = rep["shutters"]
    assert sh["selection_restored"] is False and sh["selection_lost"] is True and "select failed" in sh["note"], sh
    assert fake._selected == 0                                                              # noqa: SLF001
    with pytest.raises(MicroscopeLimitError, match="choose a laser explicitly"):
        mic.set_shutter(True)
    mic.select_laser("633")
    assert fake._selected == 1 and mic.set_shutter(True) is True                            # noqa: SLF001


def test_N8_loading_a_state_is_an_explicit_choice_too():
    mic, fake = _two_laser_mic(selected=1, extra_state={"laser": 1, "grating": 0, "center_nm": 664.55})
    fake.inject(P.laser_select_path(1), BucsError("select failed (test)"), ops=("trigger",), times=1)
    mic.stop_all()
    assert mic._selection_lost                                                              # noqa: SLF001
    mic.load_state("mine")
    assert not mic._selection_lost and fake._selected == 1                                  # noqa: SLF001


def test_N8_the_next_stop_heals_a_lost_selection_without_a_false_no_software_shutter_alarm():
    """With the default table a lost selection ends on 'white light' (no software shutter). The stop that puts the
    selection back starts ON that accident - it must not call the laser it is about to leave 'selected without shutter'
    (safe_off_all reads that as 'not confirmed')."""
    mic, fake, _ = make_mic()
    attest()
    fake.inject(P.laser_select_path(1), BucsError("select failed (test)"), ops=("trigger",), times=1)
    mic.stop_all()                                               # 532 was selected; the way back fails; it ends on index 4
    assert fake._selected == 4 and mic._selection_lost           # noqa: SLF001
    rep = mic.close_all_shutters()                               # = what emission_safety does next: no exception
    assert fake._selected == 1 and rep["selection_restored"] is True and not mic._selection_lost      # noqa: SLF001
    assert rep["selected_without_shutter"] == "" and rep["shutter_confirmed"] is True, rep
    assert "white light" in rep["no_software_shutter"]


def test_N8_a_stop_after_a_lost_selection_goes_back_to_the_laser_that_was_lost():
    mic, fake = _two_laser_mic(selected=1)                       # 633 nm
    fake.inject(P.laser_select_path(1), BucsError("select failed (test)"), ops=("trigger",), times=1)
    mic.stop_all()                                               # ends on 532, the selection is lost
    assert fake._selected == 0 and mic._selection_lost                                      # noqa: SLF001
    mic.stop_all()                                               # the next stop: it knows where the selection belongs
    assert fake._selected == 1                                                              # noqa: SLF001
    assert mic.last_stop_report["shutters"]["selection_restored"] is True
    assert not mic._selection_lost                                                          # noqa: SLF001


# ═════════════════════════ write access regained for a close goes back ═════════════════════════════════════════════


def _released_with_open_shutter(**kw):
    mic, fake, _ = make_mic(**kw)
    attest()
    assert mic.set_shutter(True) is True
    mic.release_write_access()
    assert not mic.has_write_access() and fake.gui_locked, "the person's grant still stands"
    return mic, fake


@pytest.mark.parametrize("action", ["set_shutter_false", "close_all_shutters", "stop_all", "release_write_access"])
def test_N9_a_close_hands_back_the_write_access_it_regained_only_for_itself(action):
    mic, fake = _released_with_open_shutter()
    fake.human_sets(P.LASER_SHUTTER, True)                      # it is open again (a person, or another client)
    {"set_shutter_false": lambda: mic.set_shutter(False), "close_all_shutters": mic.close_all_shutters,
     "stop_all": mic.stop_all, "release_write_access": mic.release_write_access}[action]()
    assert fake.peek(P.LASER_SHUTTER) is False, "the close itself must still work"
    assert not mic.has_write_access(), "write access that was regained for the close was never given back"
    assert fake._holds_write is False                                                       # noqa: SLF001
    assert fake.gui_locked, "the person's grant stands: the next close is silent again"
    fake.human_sets(P.LASER_SHUTTER, True)
    mic.stop_all()
    assert fake.peek(P.LASER_SHUTTER) is False and not mic.has_write_access()


def test_N9_a_write_access_the_caller_holds_is_kept_by_a_close():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    assert mic.has_write_access()
    mic.set_shutter(False)
    mic.close_all_shutters()
    mic.stop_all()
    assert mic.has_write_access(), "a close must not take away what a running job holds"


def test_N9_write_access_is_handed_back_also_when_the_close_failed():
    mic, fake = _released_with_open_shutter()
    fake.human_sets(P.LASER_SHUTTER, True)
    fake.inject(P.LASER_SHUTTER, BucsError("refused (test)"), ops=("set",), times=50)
    with pytest.raises(ShutterNotClosed):
        mic.set_shutter(False)
    assert not mic.has_write_access()


def test_N9_access_that_somebody_asks_for_while_a_stop_has_it_is_theirs_and_stays():
    mic, fake = _released_with_open_shutter()
    race = Race(mic, fake, at=3, action=mic.request_write_access)       # a job starts while the stop is under way
    race.on = True
    mic.stop_all()
    assert race.box.get("result") is True, race.box
    assert mic.has_write_access(), "taken back from a job that had just asked for it"


def test_N9_a_write_that_relies_on_the_regained_access_adopts_it():
    mic, fake = _released_with_open_shutter()
    race = Race(mic, fake, at=3, action=lambda: mic.set_analyzer_deg(10.0))     # a write that needs access, mid-stop
    race.on = True
    mic.stop_all()
    assert "result" in race.box, race.box
    assert mic.has_write_access()


def test_N9_two_closes_at_the_same_time_do_not_take_the_access_from_each_other():
    mic, fake = _released_with_open_shutter()
    fake.human_sets(P.LASER_SHUTTER, True)
    seen: list = []
    race = Race(mic, fake, at=3, action=lambda: (mic.set_shutter(False), seen.append(mic.has_write_access()))[1])
    race.on = True
    mic.stop_all()
    assert race.box.get("error") is None, race.box
    assert seen == [True], "the access vanished under a close that was still running"
    assert not mic.has_write_access(), "and the last one out handed it back"


def test_N9_the_last_close_out_hands_the_access_back_not_the_first():
    """Two closes overlap and the one that regained the access finishes FIRST: the other is still running and must keep it."""
    mic, fake = _released_with_open_shutter()
    cl = mic._cl                                                 # noqa: SLF001
    with contextlib.ExitStack() as owner, contextlib.ExitStack() as joiner:
        assert owner.enter_context(mic._closing_access(cl)) == "regained"           # noqa: SLF001
        assert mic.has_write_access()
        assert joiner.enter_context(mic._closing_access(cl)) == "held"              # noqa: SLF001 - rides on the regain
        owner.close()                                            # the owner is done ...
        assert mic.has_write_access(), "... but the other close is still running: the access was taken from under it"
        joiner.close()
    assert not mic.has_write_access() and fake._holds_write is False                         # noqa: SLF001


# ═════════════════════════ a machine-readable verdict ══════════════════════════════════════════════════════════════


def test_N6_stop_all_still_returns_none_and_the_report_carries_shutter_confirmed():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    assert mic.stop_all() is None
    rep = mic.last_stop_report
    assert rep["shutter"] == "closed" and rep["shutter_confirmed"] is True, rep


def test_N6_not_confirmed_when_a_shutter_could_not_be_closed():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    fake.operator_revoke()                                       # nothing can be written any more
    mic.stop_all()
    rep = mic.last_stop_report
    assert rep["shutter"].startswith("not closed") and rep["shutter_confirmed"] is False, rep
    assert fake._shutter[1] is True                                                          # noqa: SLF001


def test_N6_not_confirmed_when_the_selected_laser_has_no_software_shutter():
    mic, fake, _ = make_mic()
    fake._select_laser(0)                                       # noqa: SLF001 - the passive 355 nm
    mic.stop_all()
    rep = mic.last_stop_report
    assert rep["shutter"].startswith("not closed") and rep["shutter_confirmed"] is False, rep


def test_N6_not_confirmed_when_the_stop_is_configured_not_to_touch_the_shutter():
    mic, fake, _ = make_mic(close_shutter_on_stop=False)
    attest()
    mic.set_shutter(True)
    mic.stop_all()
    rep = mic.last_stop_report
    assert "shutter" not in rep and rep["shutter_confirmed"] is False, rep
    assert fake._shutter[1] is True                                                          # noqa: SLF001


def test_N6_not_confirmed_when_there_is_no_connection():
    mic, fake, _ = make_mic(grant=False)                         # never connected ...
    fake.witec_exit()                                            # ... and WITec Control is not running
    assert mic.stop_all() is None
    rep = mic.last_stop_report
    assert rep["connected"] is False and rep["shutter_confirmed"] is False, rep


def test_N6_not_confirmed_when_the_connection_is_lost_mid_session():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    fake.witec_exit()                                            # WITec Control exits with the shutter open
    mic.stop_all()
    rep = mic.last_stop_report
    assert rep["shutter_confirmed"] is False, rep                # whichever way the report came out: nothing was confirmed
    assert rep["shutter"].startswith("not closed"), rep          # and a stop skill reading it sees a problem


def test_N6_close_all_on_a_dead_link_says_not_closed_with_a_report_not_a_raw_link_error():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    fake.witec_exit()
    with pytest.raises(ShutterNotClosed) as ei:
        mic.close_all_shutters()
    assert ei.value.report["failed"] and ei.value.report["shutter_confirmed"] is False, ei.value.report


def test_N6_close_all_does_not_swap_lasers_when_it_cannot_tell_whether_a_sequencer_runs():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    fake.inject(P.IS_SEQUENCER_ACTIVE, BucsConnectionLost("link hiccup (test)"), ops=("get",), times=1)
    mark = len(fake.log)
    rep = mic.close_all_shutters()                               # the selected laser is closed; the others are NOT visited
    assert "could not be read" in rep["skipped"] and rep["shutter_confirmed"] is False, rep
    assert fake._shutter[1] is False                             # noqa: SLF001
    assert not [e for e in fake.log[mark:] if e[0] == "trigger" and "SelectLaser" in e[1]]


def test_N6_a_stop_never_leaves_the_verdict_of_an_earlier_stop_standing():
    """The registry reads ``shutter_confirmed``: a stop that failed to report must not show the PREVIOUS stop's True."""
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    mic.stop_all()
    assert mic.last_stop_report["shutter_confirmed"] is True     # an earlier, confirmed stop
    mic.set_shutter(True)
    fake.witec_exit()
    mic.stop_all()
    assert mic.last_stop_report["shutter_confirmed"] is False

    def boom(*a, **k):
        raise RuntimeError("the stop itself blew up (test)")

    mic2, fake2, _ = make_mic()
    mic2.stop_all()
    assert mic2.last_stop_report["shutter_confirmed"] is True
    mic2._make_safe = boom                                       # noqa: SLF001 - a bug in the stop: it still must not report True
    mic2.stop_all()
    rep = mic2.last_stop_report
    assert rep["shutter_confirmed"] is False and rep["shutter"].startswith("not closed"), rep


def test_N6_a_release_whose_making_safe_blew_up_says_not_confirmed():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)

    def boom(*a, **k):
        raise RuntimeError("blew up (test)")

    mic._make_safe = boom                                        # noqa: SLF001
    mic.release_write_access()
    assert mic.last_release_report["shutter_confirmed"] is False
    assert not mic.has_write_access(), "and the release itself still went ahead"


def test_N6_the_release_report_says_it_too():
    mic, fake, _ = make_mic()
    attest()
    mic.set_shutter(True)
    mic.release_write_access()
    rep = mic.last_release_report
    assert rep["shutter"] == "closed" and rep["shutter_confirmed"] is True, rep
