"""An operation that adds light is checked TWICE - before it waits for the device lock (so a
refusal is early and nothing queues for nothing) and again once it holds the device (so nothing that changed while
it queued is ignored) - and an emission-off asked for after it started cancels it.

The old code checked the gates once, before the lock: the operator could record the area as NOT safe while a
switch-on queued behind another operation and it lit the laser the moment the lock came free. NKT and NPI cleared
their cancel flag when the switch-on started, so an emission-off that arrived during the queue was forgotten.

"Another operation holds the device" is a thread (``Holder``) that takes the device lock and sits on it; the
operation under test runs on its own thread and queues behind it.
"""
from __future__ import annotations

import contextvars
import threading
import time

import pytest

from mast.i18n import use_lang
from mast.instruments import nkt_superk as nkt
from mast.instruments import npi_rainbow as npi
from mast.instruments import safe_direction as sd
from mast.instruments.laser import (
    EmissionControlDisabled,
    EmissionPreempted,
    LaserAttestationGuard,
    LaserAttestationRequired,
    LaserLimits,
    SimLaser,
    SimTunableLaser,
)
from mast.instruments.nkt_superk import NktSuperK
from mast.instruments.npi_rainbow import NpiRainbow
from mast.operator_hitl import optical_state

from .test_nkt_superk import RF, FakeNktBus
from .test_npi_rainbow import FakeRainbow
from .test_safe_direction_locks import Holder


@pytest.fixture(autouse=True)
def _english_fast_fresh_store(monkeypatch):
    monkeypatch.setattr(sd, "SAFE_LOCK_TIMEOUT_S", 0.3)       # an emission_off that meets the held lock gives up quickly
    monkeypatch.setattr(nkt, "_sleep", lambda s: None)
    monkeypatch.setattr(npi, "_sleep", lambda s: None)
    old = optical_state.set_store(optical_state.OpticalStateStore())
    with use_lang("en"):
        yield
    optical_state.set_store(old)


def sign(value=True):
    optical_state.get_store().set_by_operator("laser_area_safe", value, by="alice", source="test-operator-ui")


class Queued:
    """An operation started on its own thread: it passes whatever it checks first and then waits for the device lock
    that a Holder has."""

    def __init__(self, fn):
        self.box: dict = {}

        def run():
            try:
                self.box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001
                self.box["error"] = exc

        self._thread = threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True)

    def start(self, settle_s: float = 0.3) -> "Queued":
        self._thread.start()
        time.sleep(settle_s)
        assert self._thread.is_alive(), f"it finished without waiting for the lock: {self.box}"
        return self

    def result(self, limit_s: float = 5.0) -> dict:
        self._thread.join(limit_s)
        assert not self._thread.is_alive(), "still waiting after the lock was released"
        return self.box


def queue_while_held(las, op, *, meanwhile=None) -> dict:
    """Run ``op`` on its own thread behind another operation that holds the device; do ``meanwhile`` while it waits;
    let the device go; return what ``op`` did (``{"value": ...}`` or ``{"error": ...}``)."""
    holder = Holder(las._lock).__enter__()                    # noqa: SLF001 - the device lock is the thing under test
    try:
        q = Queued(op).start()
        if meanwhile is not None:
            meanwhile()
    finally:
        holder.release()
    return q.result()


# ═══ the device lock is held by another operation; the operator withdraws the attestation meanwhile ══════════


def test_G9_a_switch_on_that_queued_for_the_device_is_refused_when_the_area_is_no_longer_safe():
    las = SimLaser(device_id="npi", allow_emission_control=True, emission_guard=LaserAttestationGuard())
    sign(True)
    box = queue_while_held(las, las.emission_on, meanwhile=lambda: sign(False))    # the operator records NOT safe
    assert isinstance(box.get("error"), LaserAttestationRequired), box
    assert "NOT safe" in str(box["error"]) and las.emitting is False


def test_a_signature_that_is_cleared_while_queued_counts_as_well():
    las = SimLaser(device_id="npi", allow_emission_control=True, emission_guard=LaserAttestationGuard())
    sign(True)
    box = queue_while_held(las, las.emission_on, meanwhile=lambda: optical_state.get_store().clear_by_operator(
        "laser_area_safe", by="alice", source="test-operator-ui"))
    assert isinstance(box.get("error"), LaserAttestationRequired) and las.emitting is False


def test_a_config_flag_turned_off_while_queued_is_honoured_too():
    las = SimLaser(device_id="npi", allow_emission_control=True)

    def flip():
        las.allow_emission_control = False

    box = queue_while_held(las, las.emission_on, meanwhile=flip)
    assert isinstance(box.get("error"), EmissionControlDisabled) and "allow_emission_control" in str(box["error"])
    assert las.emitting is False


def test_a_refusal_is_still_early_it_does_not_queue_behind_the_lock():
    las = SimLaser(device_id="npi", allow_emission_control=True, emission_guard=LaserAttestationGuard())   # unsigned
    with Holder(las._lock):                                   # noqa: SLF001
        box: dict = {}

        def call():
            try:
                las.emission_on()
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=call, daemon=True)
        t.start()
        t.join(2.0)
        finished = not t.is_alive()
    t.join(5)
    assert finished, "an unsigned emission_on waited for the device lock before refusing"
    assert isinstance(box["error"], LaserAttestationRequired)


def test_a_signature_that_is_still_valid_lets_the_queued_switch_on_through():
    las = SimLaser(device_id="npi", allow_emission_control=True, emission_guard=LaserAttestationGuard())
    sign(True)
    box = queue_while_held(las, las.emission_on)
    assert box.get("value") is True and las.emitting is True


def test_the_gates_are_asked_twice_once_early_and_once_holding_the_device():
    asked: list[str] = []
    las = SimLaser(device_id="npi", allow_emission_control=True, emission_guard=asked.append)
    las.emission_on()
    assert asked == ["npi: emission on", "npi: emission on"]


# ═══ an emission-off asked for after the operation started cancels it (the "shutdown generation") ═════════════


def test_an_emission_off_asked_for_while_the_switch_on_queued_cancels_it():
    class Counting(SimLaser):
        raw_on = 0

        def _emission_raw(self, on):
            Counting.raw_on += bool(on)
            return super()._emission_raw(on)

    las = Counting(device_id="npi", allow_emission_control=True)
    box = queue_while_held(las, las.emission_on, meanwhile=lambda: las.emission_off())   # it cannot get the device either
    error = box.get("error")
    assert isinstance(error, EmissionPreempted) and "pre-empted" in str(error)
    assert Counting.raw_on == 0 and las.emitting is False


def test_the_shutdown_generation_counts_every_off_and_is_never_reset():
    las = SimLaser(device_id="npi", allow_emission_control=True)
    assert las.off_epoch == 0
    las.emission_off()
    las.emission_on()
    las.emission_off()
    assert las.off_epoch == 2                                    # an on does not clear what an off recorded
    with Holder(las._lock):                                      # noqa: SLF001
        assert las.emission_off() is None                        # one that could not get the device still counts
    assert las.off_epoch == 3


def test_an_emission_on_that_starts_after_the_off_is_not_cancelled_by_it():
    las = SimLaser(device_id="npi", allow_emission_control=True)
    las.emission_off()
    assert las.emission_on() is True and las.emitting is True


def test_the_scope_does_not_leak_into_the_next_operation_of_the_same_thread():
    las = SimLaser(device_id="npi", allow_emission_control=True)
    las.emission_on()
    las.emission_off()
    assert las.emission_on() is True                             # a generation left over from the first on would cancel this
    assert las.emitting is True


def test_dimming_is_never_cancelled_by_an_emission_off():
    las = SimTunableLaser(device_id="sel", allow_emission_control=True)
    las.emitting = True
    las._ch[1]["amplitude_pct"] = 30.0                           # noqa: SLF001
    box = queue_while_held(las, lambda: las.set_amplitude_pct(10, channel=1), meanwhile=lambda: las.emission_off())
    assert box.get("value") == 10.0


def test_a_raise_that_queued_is_refused_when_the_area_stopped_being_safe_and_cancelled_by_an_off():
    las = SimTunableLaser(device_id="sel", allow_emission_control=True, emission_guard=LaserAttestationGuard())
    las.emitting = True
    sign(True)
    box = queue_while_held(las, lambda: las.set_amplitude_pct(20, channel=0), meanwhile=lambda: sign(False))
    assert isinstance(box.get("error"), LaserAttestationRequired) and las.channels()[0]["amplitude_pct"] == 0.0
    sign(True)
    box = queue_while_held(las, lambda: las.set_amplitude_pct(20, channel=0), meanwhile=lambda: las.emission_off())
    assert isinstance(box.get("error"), EmissionPreempted) and las.channels()[0]["amplitude_pct"] == 0.0


def test_a_power_raise_that_queued_is_cancelled_by_an_off():
    las = SimLaser(device_id="las", allow_emission_control=True)
    las.emitting, las.power_pct = True, 10.0
    box = queue_while_held(las, lambda: las.set_power(60), meanwhile=lambda: las.emission_off())
    assert isinstance(box.get("error"), EmissionPreempted) and las.power_pct == 10.0


# ═══ NKT SuperK ═══════════════════════════════════════════════════════════════════════════════════════════════════


def _nkt(bus, **kw) -> NktSuperK:
    kw.setdefault("limits", LaserLimits(min_nm=430, max_nm=900, max_amplitude_pct=50, max_power=80.0))
    kw.setdefault("allow_emission_control", True)
    kw.setdefault("write_enabled", True)
    kw.setdefault("emission_guard", LaserAttestationGuard())
    return NktSuperK(transport=bus, port="COM14", device_id="superk", **kw)


def test_nkt_an_emission_off_that_arrives_while_the_switch_on_queues_is_not_forgotten():
    bus = FakeNktBus()
    las = _nkt(bus)
    sign(True)
    box = queue_while_held(las, las.emission_on, meanwhile=lambda: las.emission_off())
    error = box.get("error")
    assert isinstance(error, EmissionPreempted) and "pre-empted" in str(error)
    assert bus.writes() == [] and bus.emission == 0 and bus.rf_power == 0


def test_nkt_a_withdrawn_signature_refuses_the_queued_switch_on_after_the_lock():
    bus = FakeNktBus()
    las = _nkt(bus)
    sign(True)
    box = queue_while_held(las, las.emission_on, meanwhile=lambda: sign(False))
    assert isinstance(box.get("error"), LaserAttestationRequired)
    assert bus.writes() == [] and bus.emission == 0


def test_nkt_rf_power_on_is_checked_again_after_the_lock_and_cancelled_by_an_off():
    bus = FakeNktBus(emission=3, ramp_reads=0)
    las = _nkt(bus)
    sign(True)
    box = queue_while_held(las, lambda: las.set_rf_power(True), meanwhile=lambda: sign(False))
    assert isinstance(box.get("error"), LaserAttestationRequired) and bus.rf_power == 0
    sign(True)
    box = queue_while_held(las, lambda: las.set_rf_power(True), meanwhile=lambda: las.emission_off())
    assert isinstance(box.get("error"), EmissionPreempted) and bus.rf_power == 0
    assert bus.writes(RF, 0x30) == []                                           # RF power was never written


def test_nkt_a_channel_and_a_level_raise_that_queued_are_cancelled_by_an_off():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    bus.power_level = 100
    las = _nkt(bus)
    sign(True)
    for label, op in (("channel", lambda: las.set_amplitude_pct(30, channel=2)), ("level", lambda: las.set_power(60.0))):
        box = queue_while_held(las, op, meanwhile=lambda: las.emission_off())
        assert isinstance(box.get("error"), EmissionPreempted), (label, box)
    assert bus.amp[2] == 0 and bus.power_level == 100 and bus.writes() == []


def test_nkt_a_dim_that_queued_behind_an_off_still_happens():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    bus.amp[2] = 300
    las = _nkt(bus)
    box = queue_while_held(las, lambda: las.set_amplitude_pct(10, channel=2), meanwhile=lambda: las.emission_off())
    assert box.get("value") == 10.0 and bus.amp[2] == 100


def test_nkt_an_off_between_the_main_board_and_the_rf_step_cancels_the_rf_step_and_switches_everything_off():
    bus = FakeNktBus(ramp_reads=0)
    las = _nkt(bus)
    sign(True)
    real = las.main_emission
    fired: list[bool] = []

    def main_emission_then_an_off_is_asked_for():
        state = real()
        if state[0] is True and not fired:
            fired.append(True)
            las._bump_off_epoch()                               # noqa: SLF001 - an emission_off is asked for right now
        return state

    las.main_emission = main_emission_then_an_off_is_asked_for
    with pytest.raises(EmissionPreempted):
        las.emission_on()
    assert bus.rf_power == 0 and bus.emission == 0              # the RF step never ran; the main board is switched back off
    assert all(r[3] == bytes([0]) for r in bus.writes(RF, 0x30))      # RF power was never written ON


def test_nkt_an_off_during_the_wait_for_the_main_board_aborts_the_switch_on_with_the_preempted_error(monkeypatch):
    bus = FakeNktBus(ramp_reads=10 ** 9)                        # the main board never comes up
    las = _nkt(bus, emission_wait_s=30.0)
    sign(True)
    real_sleep = time.sleep
    monkeypatch.setattr(nkt, "_sleep", lambda s: real_sleep(0.01))      # real short sleeps: the wait really is in progress
    q = Queued(las.emission_on).start(settle_s=0.2)
    assert las.emission_off() is False                          # the switch-on lets go of the device and the off gets it
    box = q.result()
    assert isinstance(box.get("error"), EmissionPreempted) and bus.emission == 0 and bus.rf_power == 0


# ═══ NPI Rainbow ═══════════════════════════════════════════════════════════════════════════════════════════════════


def _npi(fake, **kw) -> NpiRainbow:
    kw.setdefault("allow_emission_control", True)
    kw.setdefault("write_enabled", True)
    kw.setdefault("emission_guard", LaserAttestationGuard())
    return NpiRainbow(transport=fake, port="COM13", device_id="npi1064", **kw)


def test_npi_an_emission_off_that_arrives_while_the_switch_on_queues_is_not_forgotten():
    fake = FakeRainbow()
    las = _npi(fake)
    sign(True)
    box = queue_while_held(las, las.emission_on, meanwhile=lambda: las.emission_off())
    error = box.get("error")
    assert isinstance(error, EmissionPreempted) and "pre-empted" in str(error)
    assert fake.sent == [] and fake.laser is False


def test_npi_a_withdrawn_signature_refuses_the_queued_switch_on_after_the_lock():
    fake = FakeRainbow()
    las = _npi(fake)
    sign(True)
    box = queue_while_held(las, las.emission_on, meanwhile=lambda: sign(False))
    assert isinstance(box.get("error"), LaserAttestationRequired)
    assert fake.sent == [] and fake.laser is False


def test_npi_returning_to_the_front_panel_is_cancelled_by_an_off_asked_for_while_it_queued():
    fake = FakeRainbow()
    fake.remote = True
    las = _npi(fake)
    sign(True)
    box = queue_while_held(las, lambda: las.release_to_front_panel(front_switch_confirmed_off=True),
                           meanwhile=lambda: las.emission_off())
    assert isinstance(box.get("error"), EmissionPreempted)
    assert "sRemote_flag1" not in fake.commands() and fake.remote is True


def test_npi_returning_to_the_front_panel_is_checked_again_after_the_lock():
    fake = FakeRainbow()
    fake.remote = True
    las = _npi(fake)
    sign(True)
    box = queue_while_held(las, lambda: las.release_to_front_panel(front_switch_confirmed_off=True),
                           meanwhile=lambda: sign(False))
    assert isinstance(box.get("error"), LaserAttestationRequired)
    assert "sRemote_flag1" not in fake.commands() and fake.remote is True
