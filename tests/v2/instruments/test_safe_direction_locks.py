"""The safe direction (emission off, heater stop) never waits forever for a device lock or a port lock.

The off path used to queue behind whatever held the device lock (a
switch-on still waiting for the laser, a transaction stuck in a serial driver). Now every wait in the
off path ends at one shared deadline (``safe_direction.SAFE_LOCK_TIMEOUT_S``, 5 s in production, shrunk
here), and the operation answers "not confirmed" instead - ``None`` for ``emission_off``, ``False`` for
``stop`` - logs it, and never raises.

A stuck operation is played by a thread that takes the lock and sits on it. Every call under test runs
on its own daemon thread with a hard limit, so code that still waits forever FAILS the test instead of
hanging the run.
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time

import pytest

from mast.environment.serial_transport import SerialUnavailable
from mast.i18n import use_lang
from mast.instruments import nkt_superk as nkt
from mast.instruments import npi_rainbow as npi
from mast.instruments import safe_direction as sd
from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.instruments.laser import LaserLimits, SimLaser, SimTunableLaser
from mast.instruments.nkt_superk import NktSuperK
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.instruments.temperature import SimTemperatureController

from .test_nkt_superk import MAIN, RF, FakeNktBus, ref_deframe
from .test_npi_rainbow import FakeRainbow
from .test_tc100 import MAP, _slave, _tc

LIMIT_S = 3.0           # a call that has not answered after this long is "hung"


@pytest.fixture(autouse=True)
def _short_wait_english_no_sleeps(monkeypatch):
    monkeypatch.setattr(sd, "SAFE_LOCK_TIMEOUT_S", 0.3)
    monkeypatch.setattr(nkt, "_sleep", lambda s: None)
    monkeypatch.setattr(npi, "_sleep", lambda s: None)
    with use_lang("en"):
        yield


class Holder:
    """Another operation that took the given locks and does not let go until the ``with`` block ends."""

    def __init__(self, *locks):
        self._locks = locks
        self._ready, self._release = threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True, name="stuck-operation")

    def _hold(self):
        for lock in self._locks:
            lock.acquire()
        self._ready.set()
        self._release.wait(60)
        for lock in reversed(self._locks):
            lock.release()

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(5), "the holder thread never got its locks"
        return self

    def __exit__(self, *exc):
        self.release()

    def release(self):
        self._release.set()
        self._thread.join(5)


def run_bounded(fn, limit_s: float = LIMIT_S):
    """``(finished, value, error, seconds)``; ``fn`` runs on a daemon thread, so a hang ends the wait here."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t0 = time.monotonic()
    th = threading.Thread(target=contextvars.copy_context().run, args=(target,), daemon=True, name="under-test")
    th.start()
    th.join(limit_s)
    return (not th.is_alive()), box.get("value"), box.get("error"), time.monotonic() - t0


def assert_answered_without_waiting_forever(outcome, *, expected, why=""):
    finished, value, error, seconds = outcome
    assert finished, f"still waiting after {LIMIT_S:g} s - the safe direction is hung on a lock {why}"
    assert error is None, f"the safe direction raised {error!r}"
    assert value is expected, f"returned {value!r}, wanted {expected!r}"
    assert seconds < LIMIT_S


def warned(caplog, *fragments) -> bool:
    return any(r.levelno >= logging.WARNING and all(f in r.getMessage() for f in fragments) for r in caplog.records)


# ═══ the helper ════════════════════════════════════════════════════════════════════════════════════════════════


def test_outside_a_safe_direction_block_held_is_a_plain_lock_that_waits():
    lock = threading.RLock()
    holder = Holder(lock).__enter__()
    threading.Timer(0.5, holder.release).start()
    t0 = time.monotonic()
    with sd.held(lock, "port"):
        waited = time.monotonic() - t0
    assert waited >= 0.4 and not sd.in_safe_direction()


def test_inside_a_block_held_gives_up_at_the_deadline_and_says_which_lock(caplog):
    lock = threading.RLock()
    with Holder(lock):
        t0 = time.monotonic()
        with pytest.raises(sd.LockTimeout, match="NPI serial port is held by another operation"):
            with sd.safe_direction():
                with sd.held(lock, "NPI serial port"):
                    pytest.fail("the lock cannot be free")
        assert 0.2 <= time.monotonic() - t0 < 1.5
    assert warned(caplog, "NPI serial port", "NOT carried out")
    assert issubclass(sd.LockTimeout, InstrumentUnavailable)


def test_the_deadline_is_shared_by_every_wait_in_the_block():
    locks = [threading.RLock() for _ in range(3)]
    with Holder(*locks):
        t0 = time.monotonic()
        with sd.safe_direction(0.2):
            for i, lock in enumerate(locks):
                with pytest.raises(sd.LockTimeout):
                    with sd.held(lock, f"lock {i}"):         # after the first, the budget is spent: no new wait
                        pass
        assert time.monotonic() - t0 < 0.45                  # one budget for all three (separate ones: 0.6 s or more)


def test_a_free_lock_is_taken_even_when_the_deadline_has_passed():
    lock = threading.RLock()
    with sd.safe_direction(0.0):
        with sd.held(lock, "free"):
            assert lock.acquire(blocking=False)         # we own it (re-entrant)
            lock.release()


def test_the_block_is_left_cleanly_and_nested_blocks_keep_the_earlier_deadline():
    lock = threading.RLock()
    assert not sd.in_safe_direction()
    with Holder(lock):
        t0 = time.monotonic()
        with sd.safe_direction(0.2):
            with sd.safe_direction(3.0):                 # a longer inner budget must not extend the outer one
                assert sd.in_safe_direction()
                with pytest.raises(sd.LockTimeout):
                    with sd.held(lock, "x"):
                        pass
        assert time.monotonic() - t0 < 1.5
    assert not sd.in_safe_direction()


def test_held_releases_the_lock_when_the_body_raises():
    lock = threading.RLock()
    with pytest.raises(ValueError):
        with sd.safe_direction():
            with sd.held(lock, "x"):
                raise ValueError("boom")
    assert Holder(lock).__enter__().release() is None       # another thread can take it at once


# ═══ Laser.emission_off (the base class, here a simulator) ═════════════════════════════════════════════════════


def test_emission_off_on_a_held_device_lock_answers_unconfirmed_and_does_not_hang(caplog):
    las = SimLaser(device_id="sim")
    las.emitting = True
    with Holder(las._lock):
        out = run_bounded(las.emission_off)
        assert_answered_without_waiting_forever(out, expected=None, why="(the device lock)")
    assert las.emitting is True                              # it was NOT switched off, and it says so
    assert las.last_off_report.get("lock_timeout") is True and "held by another operation" in las.last_off_report["error"]
    assert warned(caplog, "sim", "NOT confirmed")
    assert las.emission_off() is False and las.emitting is False        # and it works as soon as the lock is free


def test_a_stale_off_report_is_not_shown_for_a_later_unconfirmed_off():
    las = SimLaser(device_id="sim")
    las.last_off_report = {"laser_led": False}                # what an earlier, successful call left behind
    with Holder(las._lock):
        run_bounded(las.emission_off)
    assert "laser_led" not in las.last_off_report


def test_a_stale_off_report_is_not_shown_for_a_later_failed_off_either():
    class Dead(SimLaser):
        def _emission_raw(self, on):
            raise RuntimeError("dead")

    las = Dead(device_id="dead")
    las.last_off_report = {"laser_led": False}
    assert las.emission_off() is None
    assert las.last_off_report == {}                          # nothing about THIS call was learnt, and it says nothing


def test_emission_off_on_a_free_lock_is_as_before():
    las = SimLaser(device_id="sim")
    las.emitting = True
    assert las.emission_off() is False and las.emitting is False
    tun = SimTunableLaser(device_id="sel")
    tun.emitting = True
    assert tun.emission_off() is False and tun.emitting is False


def test_emission_off_still_never_raises_and_now_says_why_in_the_log(caplog):
    class Dead(SimLaser):
        def _emission_raw(self, on):
            raise RuntimeError("serial line dead")

    assert Dead(device_id="dead").emission_off() is None
    assert warned(caplog, "dead", "serial line dead")


# ═══ NPI Rainbow ═══════════════════════════════════════════════════════════════════════════════════════════════════


def _npi(fake, **kw):
    kw.setdefault("allow_emission_control", True)
    kw.setdefault("write_enabled", True)
    return npi.NpiRainbow(transport=fake, port="COM13", device_id="npi1064", **kw)


@pytest.mark.parametrize("which", ["device lock", "port lock"])
def test_npi_emission_off_with_a_stuck_lock_answers_unconfirmed_and_sends_nothing(which, caplog):
    fake = FakeRainbow()
    fake.remote = fake.software_emission = True
    las = _npi(fake)
    with Holder(las._lock if which == "device lock" else las._io_lock):
        out = run_bounded(las.emission_off)
        assert_answered_without_waiting_forever(out, expected=None, why=f"({which})")
    assert fake.sent == [] and fake.laser is True
    assert las.last_off_report.get("lock_timeout") is True              # the skill can tell "busy" from "silent"
    assert warned(caplog, "npi1064")
    assert las.emission_off() is False and fake.laser is False


def test_npi_a_stuck_port_still_lets_the_next_off_through_once_it_is_free():
    fake = FakeRainbow()
    fake.remote = fake.software_emission = True
    las = _npi(fake)
    holder = Holder(las._io_lock).__enter__()
    assert_answered_without_waiting_forever(run_bounded(las.emission_off), expected=None, why="(the port lock)")
    holder.release()
    assert las.emission_off() is False and fake.laser is False
    assert not las.last_off_report.get("lock_timeout")                  # and the report is about THIS call


class RainbowBusyAt(FakeRainbow):
    """The port lock is 'busy' (a LockTimeout out of the transport call) the n-th time ``command`` is sent."""

    def __init__(self, command, nth, **kw):
        super().__init__(**kw)
        self._busy_command, self._busy_nth, self._seen = command, nth, 0

    def transact(self, payload, **kw):
        if bytes(payload).decode("ascii").rstrip("\r") == self._busy_command:
            self._seen += 1
            if self._seen == self._busy_nth:
                raise sd.LockTimeout("simulated busy port")
        return super().transact(payload, **kw)


@pytest.mark.parametrize("command,nth,remote,site", [
    ("sEmission_flag0", 1, True, "the first off command"),
    ("gstatus", 1, True, "the Laser LED read-back"),
    # Local mode with the panel switch at ON keeps it lit after the first off, so the off is repeated in Remote mode
    ("sEmission_flag0", 2, False, "the off command repeated after Remote mode"),
])
def test_npi_every_step_of_the_off_path_notes_a_busy_port(command, nth, remote, site):
    fake = RainbowBusyAt(command, nth, panel_switch_on=not remote)
    fake.remote, fake.software_emission = remote, remote
    las = _npi(fake)
    las.emission_off()                                          # never raises
    assert las.last_off_report.get("lock_timeout") is True, site


def test_npi_the_cleanup_after_a_failed_switch_on_is_bounded_too(monkeypatch):
    fake = FakeRainbow(fail_to_light=True)
    las = _npi(fake, emission_confirm_s=0.0)
    seen: list[bool] = []
    real = las._command

    def spy(cmd, **kw):
        if cmd == npi.CMD_EMISSION_OFF:
            seen.append(sd.in_safe_direction())
        return real(cmd, **kw)

    monkeypatch.setattr(las, "_command", spy)
    with pytest.raises(InstrumentError, match="did not light"):
        las.emission_on()
    assert seen == [True]


# ═══ NKT SuperK ════════════════════════════════════════════════════════════════════════════════════════════════════


def _nkt(bus, **kw):
    kw.setdefault("limits", LaserLimits(min_nm=430, max_nm=900, max_amplitude_pct=50))
    kw.setdefault("allow_emission_control", True)
    kw.setdefault("write_enabled", True)
    return NktSuperK(transport=bus, port="COM14", device_id="superk", **kw)


@pytest.mark.parametrize("which", ["device lock", "port lock"])
def test_nkt_emission_off_with_a_stuck_lock_answers_unconfirmed_and_writes_nothing(which, caplog):
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    las = _nkt(bus)
    with Holder(las._lock if which == "device lock" else las._link._lock):
        out = run_bounded(las.emission_off)
        assert_answered_without_waiting_forever(out, expected=None, why=f"({which})")
    assert bus.writes() == [] and bus.emission == 3 and bus.rf_power == 1
    assert las.last_off_report.get("lock_timeout") is True              # the skill can tell "busy" from "silent"
    assert warned(caplog, "superk")
    assert las.emission_off() is False and bus.emission == 0 and bus.rf_power == 0
    assert not las.last_off_report.get("lock_timeout")                  # and the report is about THIS call


class NktBusyFor(FakeNktBus):
    """The port lock is 'busy' (a LockTimeout out of the transport call) for telegrams to one module only."""

    def __init__(self, busy_dest, **kw):
        super().__init__(**kw)
        self.busy_dest = busy_dest

    def transact(self, payload, **kw):
        if ref_deframe(bytes(payload))[0] == self.busy_dest:
            raise sd.LockTimeout("simulated busy port")
        return super().transact(payload, **kw)


@pytest.mark.parametrize("busy_dest,step", [(MAIN, "the main-board step"), (RF, "the RF-power step")])
def test_nkt_each_step_of_the_off_path_notes_a_busy_port(busy_dest, step):
    bus = NktBusyFor(busy_dest, emission=3, ramp_reads=0, rf_power=1)
    las = _nkt(bus)
    las.emission_off()                                          # never raises
    assert las.last_off_report.get("lock_timeout") is True, step
    if busy_dest == RF:
        assert bus.emission == 0                                # the other step still did its job
    else:
        assert bus.rf_power == 0


def test_nkt_a_stuck_port_is_waited_for_once_not_once_per_step():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    las = _nkt(bus)
    with Holder(las._link._lock):
        finished, value, error, seconds = run_bounded(las.emission_off)
    assert finished and value is None and error is None
    assert seconds < 0.3 * 2                                   # main step + RF step + read-backs share one budget


def test_nkt_rf_power_off_gives_up_on_a_stuck_lock_instead_of_hanging():
    bus = FakeNktBus(rf_power=1)
    las = _nkt(bus)
    with Holder(las._lock):
        finished, value, error, _s = run_bounded(lambda: las.set_rf_power(False))
    assert finished and isinstance(error, sd.LockTimeout)
    assert bus.rf_power == 1 and bus.writes() == []
    assert las.set_rf_power(False) is False and bus.rf_power == 0


def test_nkt_the_cleanup_after_a_failed_switch_on_is_bounded_too(monkeypatch):
    bus = FakeNktBus(ramp_reads=10 ** 9)
    ticks = iter(range(0, 10 ** 6))
    monkeypatch.setattr(nkt, "_monotonic", lambda: next(ticks))
    las = _nkt(bus, emission_wait_s=3.0)
    seen: list[bool] = []
    real = las._switch_off

    def spy():
        seen.append(sd.in_safe_direction())
        return real()

    monkeypatch.setattr(las, "_switch_off", spy)
    with pytest.raises(InstrumentError, match="did not reach 'on'"):
        las.emission_on()
    assert seen == [True] and bus.emission == 0


def test_nkt_normal_traffic_still_waits_for_the_port_it_is_not_cut_short():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    las = _nkt(bus)
    holder = Holder(las._link._lock).__enter__()
    threading.Timer(0.6, holder.release).start()
    t0 = time.monotonic()
    state = las.main_emission()                                # a plain read: waits longer than the safe budget
    assert state[0] is True and time.monotonic() - t0 >= 0.5


# ═══ TC100 and the simulated controller ═════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("which", ["device lock", "port lock"])
def test_tc100_stop_with_a_stuck_lock_answers_false_and_writes_nothing(which, caplog):
    fake = _slave()
    tc = _tc(fake, write_enabled=True)                                  # (a write-disabled one would not write anyway)
    with Holder(tc._lock if which == "device lock" else tc._io_lock):
        out = run_bounded(tc.stop)
        assert_answered_without_waiting_forever(out, expected=False, why=f"({which})")
    assert fake.writes == []
    assert warned(caplog, "tc100")
    assert tc.stop() is True and fake.writes == [(6, 23, [1])]


def test_stop_says_whether_the_command_went_out():
    assert SimTemperatureController(device_id="sim").stop() is True
    assert _tc(_slave(mute=True)).stop() is False                       # a silent controller: not confirmed

    class Dead:
        def transact(self, *a, **k):
            raise SerialUnavailable("unplugged")

        def close(self):
            pass

    assert _tc(Dead()).stop() is False
    assert _tc(_slave(), register_map=None).stop() is False            # no register map: nothing it may write


def test_sim_controller_stop_with_a_held_device_lock_answers_false():
    ctl = SimTemperatureController(device_id="simtc")
    ctl.set_setpoint(30.0)
    with Holder(ctl._lock):
        out = run_bounded(ctl.stop)
        assert_answered_without_waiting_forever(out, expected=False, why="(the device lock)")
    assert ctl.state == "running"
    assert ctl.stop() is True and ctl.state == "stopped"


# ═══ the skills and the reflex built on top ═══════════════════════════════════════════════════════════════════


def _bench(tmp_path, devices):
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": devices, "delay_line": None}), encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)
    return reg


def test_laser_emission_off_skill_reports_a_stuck_laser_and_does_not_hang(tmp_path, monkeypatch):
    from mast.skills.builtins.optics_sources import LaserEmissionOff

    reg = _bench(tmp_path, [{"id": "a", "type": "sim_laser"}, {"id": "stuck", "type": "sim_laser"}])
    try:
        reg.laser("a").emission_on()
        reg.laser("stuck").emission_on()
        asked_why: list[int] = []
        monkeypatch.setattr(reg.laser("stuck"), "status", lambda: asked_why.append(1) or {})
        with Holder(reg.laser("stuck")._lock):
            finished, res, error, seconds = run_bounded(lambda: LaserEmissionOff().execute(object(), {}))
        assert finished and error is None and seconds < LIMIT_S
        assert not res.success and res.data["unconfirmed"] == ["stuck"]
        stuck = next(d for d in res.data["devices"] if d["device_id"] == "stuck")
        assert "held by another operation" in stuck["report"]["error"]
        assert asked_why == []                         # "busy" is already the answer: no second wait to find a cause
        assert "NOT confirmed" in res.error
        assert reg.laser("a").emission_state() is False                    # the other one was switched off anyway
    finally:
        reset_instrument_registry(None)


def test_laser_emission_off_skill_bounds_the_why_question_too(tmp_path):
    """The off command failed for some other reason and the question 'why?' (a status read) meets a busy port."""
    from mast.skills.builtins.optics_sources import LaserEmissionOff

    class Quiet(SimLaser):
        port = threading.RLock()

        def _emission_raw(self, on):
            raise RuntimeError("no answer")

        def _status_raw(self):
            with sd.held(self.port, "quiet: port"):
                return {}

    reg = _bench(tmp_path, [{"id": "q", "type": "sim_laser"}])
    try:
        quiet = Quiet(device_id="q")
        reg._station["q"] = quiet
        with Holder(quiet.port):
            finished, res, error, seconds = run_bounded(lambda: LaserEmissionOff().execute(object(), {}))
        assert finished and error is None and seconds < LIMIT_S
        assert not res.success and "held by another operation" in res.data["devices"][0]["cause"]
    finally:
        reset_instrument_registry(None)


def test_stop_temperature_skill_reports_a_stuck_tc100_and_does_not_hang(tmp_path, monkeypatch):
    from mast.instruments import tc100 as tcmod
    from mast.skills.builtins.optics_sources import StopTemperatureRamp

    fake = _slave()

    class Factory:
        def __init__(self, settings):
            pass

        def open(self):
            pass

        def transact(self, payload, **kw):
            return fake.transact(payload, **kw)

        def close(self):
            pass

    monkeypatch.setattr(tcmod, "open_transport", Factory)
    reg = _bench(tmp_path, [{"id": "tc100", "type": "tc100", "enabled": True, "port": "COM15", "slave": 1,
                             "write_enabled": True, "limits": {"min_c": -190, "max_c": 300}, "register_map": MAP}])
    try:
        tc = reg.temperature_controller("tc100")
        with Holder(tc._io_lock):
            finished, res, error, seconds = run_bounded(lambda: StopTemperatureRamp().execute(object(), {}))
        assert finished and error is None and seconds < LIMIT_S
        assert not res.success and res.data["unconfirmed"] == ["tc100"] and fake.writes == []
        assert "held by another operation" in res.data["devices"][0]["cause"]
        assert res.data["devices"][0]["stop_sent"] is False
    finally:
        reset_instrument_registry(None)


def test_stop_temperature_skill_on_a_write_disabled_tc100_writes_nothing_and_says_how_to_stop_it(tmp_path, monkeypatch):
    """At the skill level: the unverified register map is never written, the read-back still tells the truth, and the
    operator is told where to stop it."""
    from mast.instruments import tc100 as tcmod
    from mast.skills.builtins.optics_sources import StopTemperatureRamp

    fake = _slave()

    class Factory:
        def __init__(self, settings):
            pass

        def open(self):
            pass

        def transact(self, payload, **kw):
            return fake.transact(payload, **kw)

        def close(self):
            pass

    monkeypatch.setattr(tcmod, "open_transport", Factory)
    reg = _bench(tmp_path, [{"id": "tc100", "type": "tc100", "enabled": True, "port": "COM15", "slave": 1,
                             "limits": {"min_c": -190, "max_c": 300}, "register_map": MAP}])     # write_enabled: default false
    try:
        assert reg.temperature_controller("tc100").write_enabled is False
        res = StopTemperatureRamp().execute(object(), {})
        item = res.data["devices"][0]
        assert not res.success and res.data["unconfirmed"] == ["tc100"] and item["stop_sent"] is False
        assert fake.writes == [] and all(frame[1] in (3, 4) for frame in fake.sent)             # only the read-back
        assert "TC100 program" in item["report"]["error"] and "write_enabled" in item["report"]["error"]
        assert item["state"] is not None and item["confirmed"] is False                          # it reads 'running', not 'stopped'
    finally:
        reset_instrument_registry(None)


def test_stop_temperature_skill_trusts_the_read_back_not_the_stop_command(tmp_path):
    """The device lock was busy so the stop did not go out - but the controller reads 'stopped': that is the answer."""
    from mast.skills.builtins.optics_sources import StopTemperatureRamp

    reg = _bench(tmp_path, [{"id": "tc", "type": "sim_temperature"}])
    try:
        tc = reg.temperature_controller("tc")
        with Holder(tc._lock):
            finished, res, error, _s = run_bounded(lambda: StopTemperatureRamp().execute(object(), {}))
        assert finished and error is None and res.success
        item = res.data["devices"][0]
        assert item["stop_sent"] is False and item["state"] == "stopped" and item["confirmed"] is True
    finally:
        reset_instrument_registry(None)


def test_safe_off_all_gets_past_a_stuck_laser(tmp_path):
    from mast.instruments.emission_safety import safe_off_all

    reg = _bench(tmp_path, [{"id": "a", "type": "sim_laser"}, {"id": "stuck", "type": "sim_tunable_laser"}])
    try:
        reg.laser("a").emission_on()
        reg.laser("stuck").emission_on()
        with Holder(reg.laser("stuck")._lock):
            finished, out, error, seconds = run_bounded(lambda: safe_off_all("test", registry=reg))
        assert finished and error is None and seconds < LIMIT_S
        by_id = {d["device_id"]: d for d in out["devices"]}
        assert by_id["a"]["confirmed"] is True and by_id["stuck"]["confirmed"] is False
        assert out["all_confirmed"] is False
    finally:
        reset_instrument_registry(None)
