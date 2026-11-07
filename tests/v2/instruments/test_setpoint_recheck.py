"""The temperature controller's twin of the laser's queued-operation gap: a setpoint that queued for the controller must not start the
heating AFTER a stop that was asked for while it waited.

``set_setpoint`` took the controller lock and wrote the target and the run command; ``stop()`` asked for in between
waited behind it, so the order was setpoint, stop - fine - but a setpoint that was still waiting when the stop came
ran AFTER the stop and the heater came back on. Now every ``stop()`` counts one "stop generation"
(``TemperatureController.stop_epoch``, never reset, counted on entry even if the stop cannot get the controller); a
setpoint reads it before it can wait, compares it once it holds the controller (and, on the TC100, before the run
command goes out) and is refused with ``TemperaturePreempted`` - nothing written.

"Another operation holds the controller" is a thread that takes its lock and sits on it (``Holder``).
"""
from __future__ import annotations

import threading

import pytest

from mast.i18n import use_lang
from mast.instruments import safe_direction as sd
from mast.instruments.registry import reset_instrument_registry
from mast.instruments.temperature import (
    SimTemperatureController,
    TemperaturePreempted,
    TemperatureWriteDisabled,
)

from .test_emission_on_recheck import queue_while_held
from .test_safe_direction_locks import Holder, _bench
from .test_tc100 import _slave, _tc


@pytest.fixture(autouse=True)
def _english_and_quick(monkeypatch):
    monkeypatch.setattr(sd, "SAFE_LOCK_TIMEOUT_S", 0.3)           # a stop that meets the held lock gives up quickly
    with use_lang("en"):
        yield


# ═══ a stop asked for while the setpoint queued ═══════════════════════════════════════════════════════════════


def test_a_stop_asked_for_while_the_setpoint_queued_cancels_it_and_nothing_is_written():
    class Counting(SimTemperatureController):
        raw = 0

        def _set_setpoint_raw(self, c, ramp):
            Counting.raw += 1
            return super()._set_setpoint_raw(c, ramp)

    ctl = Counting(device_id="sim")
    box = queue_while_held(ctl, lambda: ctl.set_setpoint(40.0), meanwhile=lambda: ctl.stop())   # the stop cannot get it either
    error = box.get("error")
    assert isinstance(error, TemperaturePreempted) and "pre-empted" in str(error)
    assert Counting.raw == 0 and ctl.state == "stopped"


def test_the_tc100_gets_not_one_byte_when_the_setpoint_is_cancelled_in_the_queue():
    fake = _slave()
    tc = _tc(fake, write_enabled=True)
    box = queue_while_held(tc, lambda: tc.set_setpoint(40.0), meanwhile=lambda: tc.stop())
    assert isinstance(box.get("error"), TemperaturePreempted)
    assert fake.sent == [] and fake.writes == []                  # not a write, not even a read


def test_a_queued_setpoint_that_nobody_stopped_goes_through():
    ctl = SimTemperatureController(device_id="sim")
    box = queue_while_held(ctl, lambda: ctl.set_setpoint(30.0))
    assert box.get("value", {}).get("target_c") == 30.0 and ctl.state == "running"


def test_a_setpoint_that_starts_after_the_stop_is_not_cancelled_by_it():
    ctl = SimTemperatureController(device_id="sim")
    assert ctl.stop() is True
    assert ctl.set_setpoint(30.0)["target_c"] == 30.0 and ctl.state == "running"


def test_the_scope_does_not_leak_out_of_the_setpoint_that_opened_it():
    ctl = SimTemperatureController(device_id="sim")
    ctl.set_setpoint(30.0)
    ctl.stop()
    ctl._check_not_stopped("a read")                              # noqa: SLF001 - outside a setpoint nothing is ever cancelled
    assert ctl.set_setpoint(31.0)["target_c"] == 31.0


def test_the_stop_generation_counts_every_stop_and_is_never_reset():
    ctl = SimTemperatureController(device_id="sim")
    assert ctl.stop_epoch == 0
    ctl.stop()
    ctl.set_setpoint(30.0)
    ctl.stop()
    assert ctl.stop_epoch == 2                                    # a setpoint does not clear what a stop recorded
    with Holder(ctl._lock):                                       # noqa: SLF001
        assert ctl.stop() is False                                # one that could not get the controller still counts
    assert ctl.stop_epoch == 3
    refused = _tc(_slave(), write_enabled=False)
    assert refused.stop() is False and refused.stop_epoch == 1    # nor does a refusal (write_enabled off) keep it from counting


def test_it_is_its_own_counter_not_the_external_stop_one():
    ctl = SimTemperatureController(device_id="sim")
    ctl.stop()
    assert ctl.stop_epoch == 1 and ctl.external_stop_epoch == 0   # a protocol's own cleanup stop is not an external one
    ctl.note_external_stop("StopTemperatureRamp (someone)")
    assert ctl.stop_epoch == 1 and ctl.external_stop_epoch == 1


# ═══ the other things that can change while it queues ═══════════════════════════════════════════════════════════


def test_a_refusal_is_still_early_it_does_not_queue_behind_the_controller():
    ctl = SimTemperatureController(device_id="sim", write_enabled=False)
    box: dict = {}

    def call():
        try:
            ctl.set_setpoint(30.0)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    with Holder(ctl._lock):                                       # noqa: SLF001
        t = threading.Thread(target=call, daemon=True)
        t.start()
        t.join(2.0)
        finished = not t.is_alive()
    t.join(5)
    assert finished, "a refused setpoint waited for the controller before refusing"
    assert isinstance(box["error"], TemperatureWriteDisabled)


def test_write_enabled_turned_off_while_queued_is_honoured_too():
    ctl = SimTemperatureController(device_id="sim")

    def flip():
        ctl.write_enabled = False

    box = queue_while_held(ctl, lambda: ctl.set_setpoint(30.0), meanwhile=flip)
    error = box.get("error")
    assert isinstance(error, TemperatureWriteDisabled) and "write_enabled" in str(error) and ctl.state == "stopped"


# ═══ a stop that arrives while the registers are being written ═════════════════════════════════════════════════


def test_a_stop_asked_for_during_the_register_sequence_keeps_the_controller_from_starting():
    fake = _slave()
    tc = _tc(fake, write_enabled=True)
    fake.on_write = lambda addr, value: tc._bump_stop_epoch() if addr == 20 else None    # noqa: SLF001 - right after the target
    with pytest.raises(TemperaturePreempted, match="start was pre-empted"):
        tc.set_setpoint(40.0)
    written = {w[1] for w in fake.writes}
    assert 20 in written and 23 not in written                    # the target went in; the run command did not
    assert tc._last_target_c is None                              # noqa: SLF001 - and nothing claims it was started


def test_a_register_sequence_nobody_interrupted_still_ends_with_the_run_command():
    fake = _slave()
    tc = _tc(fake, write_enabled=True)
    tc.set_setpoint(40.0)
    assert fake.writes[-1][1] == 23 and tc._last_target_c == 40.0  # noqa: SLF001


# ═══ at the skills: an agent sets a temperature while another presses stop ══════════════════════════════════════


def test_set_temperature_that_queued_behind_a_stop_says_so_and_changes_nothing(tmp_path):
    from mast.skills.builtins.optics_sources import SetTemperature, StopTemperatureRamp

    reg = _bench(tmp_path, [{"id": "tc", "type": "sim_temperature"}])
    try:
        ctl = reg.temperature_controller("tc")
        box = queue_while_held(ctl, lambda: SetTemperature().execute(object(), {"setpoint_c": 40.0}),
                               meanwhile=lambda: StopTemperatureRamp().execute(object(), {"device_id": "tc"}))
        res = box["value"]
        assert not res.success and "pre-empted" in res.error and ctl.state == "stopped"
    finally:
        reset_instrument_registry(None)
