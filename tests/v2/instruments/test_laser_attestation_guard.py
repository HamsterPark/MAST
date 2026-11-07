"""The operator gate inside the laser drivers (gate 2): ``Laser.emission_guard`` /
``LaserAttestationGuard`` and every path that can turn "no light" into "light".

Order in front of such a path: SUPPORTS -> allow_emission_control -> write_enabled -> guard -> raw. The off direction
passes none of them and never raises. Real drivers get the guard from ``build()`` (unless
``require_laser_attestation: false``); simulators get none unless asked.

The attestation is written the way the operator's channel writes it (``set_by_operator``); the drivers and skills
under test only ever read it.
"""
from __future__ import annotations

import json

import pytest

from mast.i18n import use_lang
from mast.instruments import nkt_superk as nkt
from mast.instruments import npi_rainbow as npi
from mast.instruments.base import InstrumentError
from mast.instruments.laser import (
    EmissionControlDisabled,
    LaserAttestationGuard,
    LaserAttestationRequired,
    LaserLimits,
    SimLaser,
    SimTunableLaser,
    default_emission_guard,
)
from mast.instruments.nkt_superk import NktSuperK
from mast.instruments.npi_rainbow import NpiRainbow
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.operator_hitl import optical_state

from .test_nkt_superk import MAIN, RF, FakeNktBus
from .test_npi_rainbow import FakeRainbow


@pytest.fixture(autouse=True)
def _english_fast_and_a_fresh_operator_store(monkeypatch):
    monkeypatch.setattr(nkt, "_sleep", lambda s: None)
    monkeypatch.setattr(npi, "_sleep", lambda s: None)
    old = optical_state.set_store(optical_state.OpticalStateStore())
    with use_lang("en"):
        yield
    optical_state.set_store(old)


def sign(value=True, by="alice"):
    """What the operator's web / launcher channel does. Nothing in the code under test can."""
    return optical_state.get_store().set_by_operator("laser_area_safe", value, by=by, source="test-operator-ui")


def expire():
    optical_state.get_store()._values["laser_area_safe"].valid_until = "2000-01-01T00:00:00+00:00"


class Recorder:
    """A guard double that records what it was asked and optionally refuses."""

    def __init__(self, *, refuse_from: int | None = None):
        self.calls: list[str] = []
        self.refuse_from = refuse_from

    def __call__(self, what: str) -> None:
        self.calls.append(what)
        if self.refuse_from is not None and len(self.calls) >= self.refuse_from:
            raise LaserAttestationRequired(f"{what} - recorder refuses")


class Exploding:
    """A guard that must never be consulted in the off direction."""

    def __call__(self, what: str) -> None:
        raise AssertionError(f"the guard was consulted for {what!r}")


# ═══ the guard itself ═══════════════════════════════════════════════════════════════════════════════════


def test_the_guard_accepts_only_a_current_positive_signature():
    g = LaserAttestationGuard()
    assert g.status() == "missing" and g.problem()
    with pytest.raises(LaserAttestationRequired, match=r"^npi: emission on - no valid 'laser area safe' attestation"):
        g("npi: emission on")
    sign(True)
    assert g.status() == "valid" and g.problem() is None
    g("npi: emission on")                                              # returns
    expire()
    assert g.status() == "missing"


def test_a_signature_recorded_as_not_safe_is_its_own_state_and_never_passes():
    g = LaserAttestationGuard()
    sign(False)
    assert optical_state.get_store().get("laser_area_safe") is not None         # it is set - to "not safe"
    assert g.status() == "unsafe"
    with pytest.raises(LaserAttestationRequired, match="NOT safe"):
        g("x: emission on")
    expire()
    assert g.status() == "missing"                                     # an expired "not safe" is just a missing signature


def test_a_store_without_the_slot_cannot_confirm_safety():
    optical_state.set_store(optical_state.OpticalStateStore(slots=()))
    g = LaserAttestationGuard()
    assert g.status() == "no_slot" and "defines no 'laser_area_safe' slot" in g.problem()
    with pytest.raises(LaserAttestationRequired):
        g("x: emission on")


def test_the_guard_reads_whichever_store_is_current_when_it_is_called():
    g = LaserAttestationGuard()
    sign(True)
    assert g.status() == "valid"
    optical_state.set_store(optical_state.OpticalStateStore())          # e.g. the service swaps in the persistent one
    assert g.status() == "missing"


def test_the_message_is_in_the_ui_language_and_says_who_must_act():
    g = LaserAttestationGuard()
    assert "only the operator can do that" in g.problem() and "8 h" in g.problem()
    with use_lang("zh"):
        assert "只有操作员能做" in g.problem() and "8 小时" in g.problem()


def test_default_emission_guard_and_the_error_family():
    assert isinstance(default_emission_guard(True), LaserAttestationGuard)
    assert default_emission_guard(False) is None
    assert issubclass(LaserAttestationRequired, InstrumentError)


# ═══ Laser.emission_on: the order of the gates ═══════════════════════════════════════════════════════════


def test_the_guard_is_consulted_last_and_only_when_everything_before_it_passed():
    rec = Recorder()

    class NoEmission(SimLaser):
        SUPPORTS = frozenset({"power"})

    with pytest.raises(EmissionControlDisabled, match="no software emission control"):
        NoEmission(device_id="d", emission_guard=rec).emission_on()
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        SimLaser(device_id="d", allow_emission_control=False, emission_guard=rec).emission_on()
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        SimLaser(device_id="d", write_enabled=False, emission_guard=rec).emission_on()
    assert rec.calls == []                                             # never reached
    both_wrong = SimLaser(device_id="d", allow_emission_control=False, write_enabled=False, emission_guard=Exploding())
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):     # and the first failure is reported
        both_wrong.emission_on()
    las = SimLaser(device_id="d", emission_guard=rec)
    # asked twice: early (so a refusal does not queue behind the device lock) and again holding the device
    assert las.emission_on() is True and rec.calls == ["d: emission on", "d: emission on"]


def test_a_refusing_guard_means_the_primitive_is_never_called():
    class Counting(SimLaser):
        raw_on = 0

        def _emission_raw(self, on):
            Counting.raw_on += bool(on)
            return super()._emission_raw(on)

    las = Counting(device_id="d", emission_guard=Recorder(refuse_from=1))
    with pytest.raises(LaserAttestationRequired, match="recorder refuses"):
        las.emission_on()
    assert Counting.raw_on == 0 and las.emission_state() is False


def test_simulators_carry_no_guard_unless_one_is_passed_and_work_as_before():
    assert SimLaser().emission_guard is None and SimTunableLaser().emission_guard is None
    assert SimLaser().emission_on() is True                             # no attestation anywhere: unchanged behaviour
    guarded = SimLaser(emission_guard=default_emission_guard(True))
    with pytest.raises(LaserAttestationRequired):
        guarded.emission_on()
    sign()
    assert guarded.emission_on() is True
    optical_state.set_store(optical_state.OpticalStateStore())          # unsigned again
    tun = SimTunableLaser(emission_guard=default_emission_guard(True))
    with pytest.raises(LaserAttestationRequired):
        tun.emission_on()
    sign()
    assert tun.emission_on() is True


# ═══ the off direction is never gated and never raises ══════════════════════════════════════════════════


@pytest.mark.parametrize("record", ["none", "false", "expired"])
def test_emission_off_needs_no_signature_and_never_consults_the_guard(record):
    las = SimLaser(device_id="d", emission_guard=Exploding())
    las.emitting = True
    if record == "false":
        sign(False)
    elif record == "expired":
        sign(True)
        expire()
    assert las.emission_off() is False and las.emitting is False


def test_emission_off_swallows_a_broken_guard_too():
    class Raising(SimLaser):
        def _emission_raw(self, on):
            raise RuntimeError("line dead")

    assert Raising(device_id="d", emission_guard=Exploding()).emission_off() is None


# ═══ the factories ═════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("module,type_,port", [(npi, "npi_rainbow", "COM13"), (nkt, "nkt_superk", "COM14")])
def test_real_drivers_get_the_attestation_guard_unless_the_entry_opts_out(module, type_, port):
    entry = {"id": "x", "type": type_, "port": port}
    assert isinstance(module.build(entry).emission_guard, LaserAttestationGuard)
    assert isinstance(module.build({**entry, "require_laser_attestation": True}).emission_guard, LaserAttestationGuard)
    assert module.build({**entry, "require_laser_attestation": False}).emission_guard is None


def test_the_registry_builds_real_drivers_with_the_guard_and_sims_without(tmp_path):
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "npi", "type": "npi_rainbow", "port": "COM13"},
        {"id": "npi_off", "type": "npi_rainbow", "port": "COM19", "require_laser_attestation": False},
        {"id": "sk", "type": "nkt_superk", "port": "COM14"},
        {"id": "sim", "type": "sim_laser"}]}), encoding="utf-8")
    reg = InstrumentRegistry(config_path=p)
    assert isinstance(reg.laser("npi").emission_guard, LaserAttestationGuard)
    assert reg.laser("npi_off").emission_guard is None
    assert isinstance(reg.laser("sk").emission_guard, LaserAttestationGuard)
    assert reg.laser("sim").emission_guard is None


# ═══ NPI Rainbow ═══════════════════════════════════════════════════════════════════════════════════════════


def _npi(fake, **kw) -> NpiRainbow:
    kw.setdefault("allow_emission_control", True)
    kw.setdefault("write_enabled", True)
    kw.setdefault("emission_guard", LaserAttestationGuard())
    return NpiRainbow(transport=fake, port="COM13", device_id="npi1064", **kw)


def test_npi_emission_on_without_a_signature_puts_nothing_on_the_wire():
    fake = FakeRainbow()
    las = _npi(fake)
    with pytest.raises(LaserAttestationRequired, match=r"^npi1064: emission on - no valid 'laser area safe'"):
        las.emission_on()
    assert fake.sent == [] and fake.laser is False
    sign(False)
    with pytest.raises(LaserAttestationRequired, match="NOT safe"):
        las.emission_on()
    sign(True)
    expire()
    with pytest.raises(LaserAttestationRequired):
        las.emission_on()
    assert fake.sent == []
    sign(True)
    assert las.emission_on() is True and fake.laser is True
    assert fake.commands() == ["gstatus", "sRemote_flag0", "sEmission_flag1", "gstatus"]


def test_npi_config_gates_come_before_the_guard():
    fake = FakeRainbow()
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        _npi(fake, allow_emission_control=False).emission_on()
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        _npi(fake, write_enabled=False).emission_on()
    assert fake.sent == []


def test_npi_returning_to_the_front_panel_can_end_in_light_so_it_is_gated_too():
    fake = FakeRainbow()
    fake.remote = True
    las = _npi(fake)
    with pytest.raises(LaserAttestationRequired, match="return to local mode"):
        las.release_to_front_panel(front_switch_confirmed_off=True)
    assert "sRemote_flag1" not in fake.commands() and fake.remote is True
    sign()
    assert las.release_to_front_panel(front_switch_confirmed_off=True) is True and fake.remote is False
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        _npi(fake, allow_emission_control=False).release_to_front_panel(front_switch_confirmed_off=True)


def test_npi_emission_off_needs_no_signature():
    fake = FakeRainbow()
    fake.remote, fake.software_emission = True, True
    las = _npi(fake, emission_guard=Exploding())
    assert las.emission_off() is False and fake.laser is False
    fake2 = FakeRainbow(panel_switch_on=True)                           # the Remote fallback is not gated either
    assert _npi(fake2, emission_guard=Exploding()).emission_off() is False


# ═══ NKT SuperK ═══════════════════════════════════════════════════════════════════════════════════════════


def _nkt(bus, **kw) -> NktSuperK:
    kw.setdefault("limits", LaserLimits(min_nm=430, max_nm=900, max_amplitude_pct=50))
    kw.setdefault("allow_emission_control", True)
    kw.setdefault("write_enabled", True)
    kw.setdefault("emission_guard", LaserAttestationGuard())
    return NktSuperK(transport=bus, port="COM14", device_id="superk", **kw)


def test_nkt_rf_power_on_opens_the_output_and_passes_every_gate():
    bus = FakeNktBus()
    las = _nkt(bus)
    with pytest.raises(LaserAttestationRequired, match=r"^superk: RF power on - "):
        las.set_rf_power(True)
    assert bus.writes() == [] and bus.rf_power == 0
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        _nkt(bus, allow_emission_control=False).set_rf_power(True)
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        _nkt(bus, write_enabled=False).set_rf_power(True)
    assert bus.writes() == []
    sign()
    assert las.set_rf_power(True) is True and bus.rf_power == 1


def test_nkt_rf_power_off_is_never_gated_and_never_raises_for_the_guard():
    bus = FakeNktBus(rf_power=1)
    las = _nkt(bus, emission_guard=Exploding())
    assert las.set_rf_power(False) is False and bus.rf_power == 0
    gates_closed = NktSuperK(transport=FakeNktBus(rf_power=1), port="COM14", emission_guard=Exploding())
    assert gates_closed.set_rf_power(False) is False                    # even with allow/write both off


def test_nkt_emission_on_consults_the_guard_early_and_holding_the_device_then_for_the_rf_gate():
    rec = Recorder()
    bus = FakeNktBus()
    assert _nkt(bus, emission_guard=rec).emission_on() is True
    # "emission on": early + holding the device; "RF power on" runs inside that operation, so only the second check
    assert rec.calls == ["superk: emission on", "superk: emission on", "superk: RF power on"]
    unsigned = FakeNktBus()
    with pytest.raises(LaserAttestationRequired):
        _nkt(unsigned).emission_on()
    assert unsigned.writes() == [] and unsigned.emission == 0


def test_nkt_an_attestation_that_lapses_between_the_two_steps_switches_everything_off():
    bus = FakeNktBus()
    las = _nkt(bus, emission_guard=Recorder(refuse_from=3))             # passes both "emission on" checks, refuses "RF power on"
    with pytest.raises(LaserAttestationRequired, match="RF power on"):
        las.emission_on()
    assert bus.emission == 0 and bus.rf_power == 0                      # the main board that had just been switched on is off again
    assert las.last_off_report["main_emission"] is False


def test_nkt_with_the_fianium_already_on_the_rf_gate_alone_needs_the_signature():
    bus = FakeNktBus(emission=3, ramp_reads=0)                          # somebody pressed the button
    las = _nkt(bus, main_write_types=[])
    with pytest.raises(LaserAttestationRequired):
        las.emission_on()
    assert bus.writes() == []
    sign()
    assert las.emission_on() is True and bus.rf_power == 1 and not bus.writes(MAIN)


def test_nkt_enabling_a_channel_on_a_system_that_is_or_may_be_live_is_gated():
    live_but_dark = dict(emission=3, ramp_reads=0, rf_power=1)         # main on, RF on, every channel at 0
    bus = FakeNktBus(**live_but_dark)
    las = _nkt(bus)
    with pytest.raises(LaserAttestationRequired, match=r"channel 3 amplitude 30 % \(adds light\)"):
        las.set_amplitude_pct(30, channel=3)
    assert bus.writes() == [] and bus.amp[3] == 0
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        _nkt(bus, allow_emission_control=False).set_amplitude_pct(30, channel=3)
    assert bus.writes() == []
    sign()
    assert las.set_amplitude_pct(30, channel=3) == 30.0 and bus.amp[3] == 300


def test_nkt_enabling_a_second_channel_next_to_a_live_one_is_new_light_too():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    bus.amp[2] = 300
    with pytest.raises(LaserAttestationRequired):
        _nkt(bus).set_amplitude_pct(20, channel=5)
    assert bus.amp[5] == 0


@pytest.mark.parametrize("state", [
    dict(),                                                             # everything off: later emission/RF-on is gated
    dict(emission=3, ramp_reads=0, rf_power=0),                         # main on, RF off: RF-on is gated
    dict(emission=0, rf_power=1),                                       # RF on, main off: emission-on is gated
])
def test_nkt_amplitude_writes_that_cannot_start_light_are_not_gated(state):
    bus = FakeNktBus(**state)
    las = _nkt(bus, emission_guard=Exploding())
    assert las.set_amplitude_pct(30, channel=1) == 30.0 and bus.amp[1] == 300


def test_nkt_dimming_or_removing_light_is_never_gated():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    bus.amp[2] = 300
    las = _nkt(bus, emission_guard=Exploding())
    assert las.set_amplitude_pct(30, channel=2) == 30.0                 # the same level adds nothing
    assert las.set_amplitude_pct(20, channel=2) == 20.0                 # dimmer
    assert las.set_amplitude_pct(0, channel=2) == 0.0                   # switching a line off is the safe direction
    assert bus.amp[2] == 0


def test_nkt_making_a_lit_channel_brighter_is_more_light_on_the_sample_so_it_is_gated():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    bus.amp[2] = 300                                                    # the operator set 30 % by hand
    las = _nkt(bus)
    with pytest.raises(LaserAttestationRequired, match=r"channel 2 amplitude 45 % \(adds light\)"):
        las.set_amplitude_pct(45, channel=2)
    assert bus.writes() == [] and bus.amp[2] == 300
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        _nkt(bus, allow_emission_control=False).set_amplitude_pct(45, channel=2)
    assert bus.writes() == [] and bus.amp[2] == 300
    sign()
    assert las.set_amplitude_pct(45, channel=2) == 45.0 and bus.amp[2] == 450


def test_nkt_with_allow_emission_control_false_mast_can_dim_and_switch_off_but_never_add_light():
    """What `allow_emission_control=false` means now: manual mode - the operator lights it, MAST tunes the colour,
    turns it down and off."""
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    bus.amp[1], bus.power_level = 300, 500
    las = _nkt(bus, allow_emission_control=False, emission_guard=Exploding(),
               limits=LaserLimits(min_nm=430, max_nm=900, max_amplitude_pct=50, max_power=80.0))
    assert las.set_wavelength_nm(520.0, channel=1) == 520.0
    assert las.set_amplitude_pct(20, channel=1) == 20.0 and las.set_power(40.0) == 40.0
    for add_light in (lambda: las.set_amplitude_pct(25, channel=1), lambda: las.set_power(45.0),
                      lambda: las.set_amplitude_pct(10, channel=4), lambda: las.set_rf_power(True)):
        with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
            add_light()
    assert las.set_amplitude_pct(0, channel=1) == 0.0 and las.emission_off() is False


def test_nkt_turning_a_channel_or_the_level_to_zero_never_needs_the_system_to_be_readable():
    """The safe direction does not ask questions: with the RF driver silent, 0 % goes straight through."""
    bus = FakeNktBus(main_type=0x88, emission=3, ramp_reads=0, rf_power=1, silent={RF})
    las = _nkt(bus, emission_guard=Exploding(), limits=POWER_LIMITS)
    assert las.set_power(0.0) == 0.0 and bus.power_level == 0                       # main answers, RF does not
    bus2 = FakeNktBus(emission=3, ramp_reads=0, rf_power=1, silent={MAIN})
    bus2.amp[1] = 200
    assert _nkt(bus2, emission_guard=Exploding()).set_amplitude_pct(0, channel=1) == 0.0 and bus2.amp[1] == 0


def test_nkt_a_same_level_write_adds_nothing_even_after_rounding_to_a_tenth_of_a_percent():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    bus.amp[2] = 300
    las = _nkt(bus, emission_guard=Exploding())
    assert las.set_amplitude_pct(30.04, channel=2) == 30.0              # 30.04 % is written as 300 permille: the same


def test_nkt_an_unreadable_system_state_fails_closed_for_amplitude():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1, silent={MAIN})
    las = _nkt(bus)
    with pytest.raises(LaserAttestationRequired):
        las.set_amplitude_pct(30, channel=0)
    assert bus.writes() == []
    sign()
    assert las.set_amplitude_pct(30, channel=0) == 30.0                 # the gate is satisfied; the RF driver answers


class _AmplitudeReadFailsOnce(FakeNktBus):
    """The RF driver answers everything except the first read of an amplitude register."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.failures_left = 1

    def _read(self, addr, reg):
        if addr == RF and 0xB0 <= reg <= 0xB7 and self.failures_left:
            self.failures_left -= 1
            return None                                                 # a Nack: the register cannot be read now
        return super()._read(addr, reg)


def test_nkt_a_current_value_that_cannot_be_read_counts_as_an_increase():
    bus = _AmplitudeReadFailsOnce(emission=3, ramp_reads=0, rf_power=1)
    bus.amp[2] = 300
    las = _nkt(bus)
    with pytest.raises(LaserAttestationRequired):
        las.set_amplitude_pct(10, channel=2)                            # even a "dimmer" request: we cannot tell
    assert bus.writes() == [] and bus.amp[2] == 300
    bus.failures_left = 1
    sign()
    assert las.set_amplitude_pct(10, channel=2) == 10.0                 # with every gate satisfied it goes through


def test_nkt_things_that_cannot_start_light_by_themselves_are_not_gated():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    bus.amp[0] = 300
    las = _nkt(bus, emission_guard=Exploding(), limits=LaserLimits(min_nm=430, max_nm=900, max_amplitude_pct=50,
                                                                    max_power=80.0))
    assert las.set_wavelength_nm(520.0, channel=0) == 520.0             # colour only
    assert las.set_power(40.0) == 40.0                                  # turns the beam that is already there down (50 -> 40)
    dark = FakeNktBus(rf_power=1)
    assert _nkt(dark, emission_guard=Exploding()).select_crystal(2)["rf_power"] is False     # leaves RF off


POWER_LIMITS = LaserLimits(min_nm=430, max_nm=900, max_amplitude_pct=50, max_power=80.0)


@pytest.mark.parametrize("main_type", [0x88, 0x60])
@pytest.mark.parametrize("before,after", [(0, 40.0), (300, 40.0), (500, 90.0)])
def test_nkt_any_raise_of_the_main_level_on_a_live_chain_is_gated(main_type, before, after):
    bus = FakeNktBus(main_type=main_type, emission=3, ramp_reads=0, rf_power=1)    # level register 0x37 in both defaults
    bus.amp[0], bus.power_level = 300, before                           # a line is set; the level is what we raise
    las = _nkt(bus, limits=LaserLimits(min_nm=430, max_nm=900, max_amplitude_pct=50, max_power=100.0))
    with pytest.raises(LaserAttestationRequired, match=rf"output level {after:g} % \(adds light\)"):
        las.set_power(after)
    assert bus.power_level == before and not bus.writes(MAIN, 0x37)
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        _nkt(bus, limits=POWER_LIMITS, allow_emission_control=False).set_power(min(after, 80.0))
    assert bus.power_level == before
    sign()
    assert las.set_power(after) == after and bus.power_level == int(after * 10)


def test_nkt_a_main_level_raise_is_gated_even_when_every_channel_reads_zero():
    """The rule is "more light when the chain is or may be live" - not "when we can show a lit channel" (a SELECT
    that is bypassed, or a channel state we cannot trust, must not turn a raise into a free action)."""
    bus = FakeNktBus(main_type=0x88, emission=3, ramp_reads=0, rf_power=1)         # every channel at 0
    bus.power_level = 0
    las = _nkt(bus, limits=POWER_LIMITS)
    with pytest.raises(LaserAttestationRequired):
        las.set_power(35.0)
    assert bus.power_level == 0


@pytest.mark.parametrize("what,state", [
    ("RF power is off", dict(emission=3, ramp_reads=0, rf_power=0, level=0)),
    ("main emission is off", dict(emission=0, rf_power=1, level=0)),
])
def test_nkt_a_level_raise_while_the_chain_is_known_to_be_dark_is_not_gated(what, state):
    bus = FakeNktBus(main_type=0x88, emission=state["emission"], ramp_reads=0, rf_power=state["rf_power"])
    bus.power_level = state["level"]
    bus.amp[1] = 200
    las = _nkt(bus, limits=POWER_LIMITS, emission_guard=Exploding())
    assert las.set_power(35.0) == 35.0, what
    assert las.set_power(0.0) == 0.0                                    # and removing the level is the safe direction


def test_nkt_a_level_write_that_adds_no_light_is_not_gated():
    bus = FakeNktBus(main_type=0x88, emission=3, ramp_reads=0, rf_power=1)
    bus.amp[1], bus.power_level = 200, 500
    las = _nkt(bus, limits=POWER_LIMITS, emission_guard=Exploding())
    assert las.set_power(50.0) == 50.0                                  # the same level
    assert las.set_power(35.0) == 35.0                                  # dimmer
    assert las.set_power(0.0) == 0.0 and bus.power_level == 0


def test_nkt_an_unreadable_chain_fails_closed_for_the_level_too():
    bus = FakeNktBus(main_type=0x88, emission=3, ramp_reads=0, rf_power=1, silent={RF})
    bus.power_level = 0
    las = _nkt(bus, limits=POWER_LIMITS)
    with pytest.raises(LaserAttestationRequired):
        las.set_power(40.0)
    assert bus.power_level == 0


def test_status_says_whether_the_driver_checks_the_attestation():
    assert SimLaser().status()["operator_guard"] is False
    assert SimLaser(emission_guard=default_emission_guard(True)).status()["operator_guard"] is True
    assert _nkt(FakeNktBus()).status()["operator_guard"] is True
    assert _nkt(FakeNktBus(), emission_guard=None).status()["operator_guard"] is False


def test_nkt_emission_off_is_never_gated():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    las = _nkt(bus, emission_guard=Exploding())
    assert las.emission_off() is False and bus.emission == 0 and bus.rf_power == 0


# ═══ the skill layer on top ════════════════════════════════════════════════════════════════════════════════════


def test_skills_report_a_driver_side_refusal_in_the_guards_own_words(tmp_path, monkeypatch):
    from mast.skills.builtins.optics_sources import LaserEmissionOn, SetLightSourcePower

    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "superk", "type": "nkt_superk", "enabled": True, "port": "COM14", "write_enabled": True,
         "allow_emission_control": True, "limits": {"min_nm": 430, "max_nm": 900, "max_amplitude_pct": 50}}]}),
        encoding="utf-8")
    reg = InstrumentRegistry(config_path=p)
    reset_instrument_registry(reg)
    try:
        bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
        reg._station["superk"] = NktSuperK(transport=bus, port="COM14", device_id="superk", write_enabled=True,
                                           allow_emission_control=True,
                                           limits=LaserLimits(min_nm=430, max_nm=900, max_amplitude_pct=50),
                                           emission_guard=LaserAttestationGuard())
        res = SetLightSourcePower().execute(object(), {"power": 30.0, "channel": 1})
        assert not res.success and res.error.startswith("refused: superk: channel 1 amplitude 30 %")
        assert "only the operator can do that" in res.error and bus.amp[1] == 0
        sign()
        assert SetLightSourcePower().execute(object(), {"power": 30.0, "channel": 1}).success and bus.amp[1] == 300
        # the skill's own check passes (signed) but the driver's guard sees a lapse in between: still no light
        reg._station["superk"] = NktSuperK(transport=FakeNktBus(), port="COM14", device_id="superk", write_enabled=True,
                                           allow_emission_control=True, emission_guard=Recorder(refuse_from=1))
        res = LaserEmissionOn().execute(object(), {})
        assert not res.success and "recorder refuses" in res.error
    finally:
        reset_instrument_registry(None)
