"""Light-source / power-meter / temperature skills (contract §6.3).

Sims and test doubles from the instrument layer stand in for the hardware; the operator's attestation
store is the real in-memory one. What matters here is the *gating*: the two-gate emission-on, the
safe-direction skills that are never refused, device resolution, and the bounded abortable wait.
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[5] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import inspect
import json
import threading

import pytest

from mast.core.instrument_lock import hold_for_skill, instrument_lock, needs_token
from mast.core.types import SafetyLevel, SkillCategory
from mast.i18n import use_lang
from mast.instruments import npi_rainbow
from mast.instruments.laser import LaserLimits, SimLaser
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.instruments.temperature import TemperatureController, TemperatureLimits
from mast.operator_hitl import optical_state
from mast.skills.builtins import optics_sources as src
from mast.skills.builtins.optics_sources import (
    GetLaserStatus,
    IdentifyOpticalSource,
    LaserEmissionOff,
    LaserEmissionOn,
    ReadOpticalPower,
    ReadTemperature,
    SetLaserWavelength,
    SetLightSourcePower,
    SetTemperature,
    StopTemperatureRamp,
    WaitTemperatureStable,
)


@pytest.fixture(autouse=True)
def _english_and_fresh_operator_store():
    old = optical_state.set_store(optical_state.OpticalStateStore())
    with use_lang("en"):
        yield
    optical_state.set_store(old)


def _bench(tmp_path, devices):
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": devices, "delay_line": None}), encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)
    return reg


@pytest.fixture
def bench(tmp_path):
    reg = _bench(tmp_path, [{"id": "npi", "type": "sim_laser", "allow_emission_control": True},
                            {"id": "tc", "type": "sim_temperature", "time_scale": 600.0, "tau_s": 1.0},
                            {"id": "pm", "type": "sim_power_meter"}])
    yield reg
    reset_instrument_registry(None)


class Ctx:
    def __init__(self, abort_after: int | None = None):
        self.calls = 0
        self.abort_after = abort_after

    def check_abort(self):
        self.calls += 1
        return self.abort_after is not None and self.calls > self.abort_after


def run(skill, params=None, ctx=None):
    sk = skill()
    errs = sk.validate_params(dict(params or {}))
    assert not errs, errs
    return sk.execute(ctx or Ctx(), dict(params or {}))


def sign_as_operator(by="alice"):
    """What the operator's web/launcher channel does - never the skills."""
    return optical_state.get_store().set_by_operator("laser_area_safe", True, by=by, source="test-operator-ui")


# ═══ the contract ═════════════════════════════════════════════════════════════════════════════════


CONTRACT = {
    GetLaserStatus: SafetyLevel.AUTO, ReadOpticalPower: SafetyLevel.AUTO, ReadTemperature: SafetyLevel.AUTO,
    IdentifyOpticalSource: SafetyLevel.AUTO,
    SetLightSourcePower: SafetyLevel.CONFIRM, SetLaserWavelength: SafetyLevel.CONFIRM, SetTemperature: SafetyLevel.CONFIRM,
    WaitTemperatureStable: SafetyLevel.CONFIRM, LaserEmissionOn: SafetyLevel.CONFIRM,
    LaserEmissionOff: SafetyLevel.AUTO, StopTemperatureRamp: SafetyLevel.AUTO,
}


@pytest.mark.parametrize("cls,level", list(CONTRACT.items()), ids=lambda v: getattr(v, "__name__", str(v)))
def test_names_and_safety_levels_are_the_contract(cls, level):
    meta = cls().metadata()
    assert meta.name == cls.__name__ and meta.safety_level is level
    assert any(p.name == "device_id" and not p.required for p in meta.parameters)    # every skill has an optional device_id
    assert "optics" in meta.tags


def test_discovery_hands_out_the_classes_of_this_module_not_a_namesake():
    """The class-level tests here import the classes directly, so they cannot see a name clash: two skills with one
    name register as one and the later one silently wins (a power skill could vanish behind the AFM skill of that
    name, and the agent had no way to set a light source's power at all). What discovery registers under each of our
    names must BE our class."""
    from mast.agents.instrument_control.tools import discover_instrument_skills

    reg = discover_instrument_skills()
    for name in src.SOURCE_SKILLS:
        got = reg.get(name)
        assert got is getattr(src, name), f"{name}: discovery registered {got.__module__}.{got.__qualname__} instead"


def test_the_power_skill_is_named_for_what_it_drives_not_for_the_afm_laser():
    # `SetLaserPower` belongs to the AFM module (Nanonis laser); this one sets a light source of the optical station
    assert "SetLaserPower" not in src.SOURCE_SKILLS and "SetLightSourcePower" in src.SOURCE_SKILLS
    assert SetLightSourcePower().metadata().name == "SetLightSourcePower"


def test_the_safe_direction_skills_are_remedies_that_never_take_the_instrument_token():
    for cls in (LaserEmissionOff, StopTemperatureRamp):
        meta = cls().metadata()
        assert meta.safety_level is SafetyLevel.AUTO and "emergency" in meta.tags
        assert needs_token(meta, cls.__name__) is False
    # ...so another run holding the instrument cannot make them wait or be refused
    done = threading.Event()
    release = threading.Event()

    def holder():
        with instrument_lock().hold(owner="someone", skill="AutoApproach", timeout_s=1.0):
            done.set()
            release.wait(5)

    th = threading.Thread(target=holder)
    th.start()
    assert done.wait(2)
    try:
        for cls in (LaserEmissionOff, StopTemperatureRamp):
            with hold_for_skill(cls().metadata(), cls.__name__, "ext:test", timeout_s=0.2):
                pass
    finally:
        release.set()
        th.join(5)


def test_waiting_is_a_read_and_the_writes_are_writes():
    assert WaitTemperatureStable().metadata().category is SkillCategory.READ
    for cls in (SetLightSourcePower, SetLaserWavelength, SetTemperature, LaserEmissionOn, LaserEmissionOff, StopTemperatureRamp):
        assert cls().metadata().category is SkillCategory.WRITE


def test_the_module_is_registered_for_frozen_discovery_and_has_no_way_to_sign():
    import mast.skills.builtins as builtins

    assert builtins.optics_sources is src
    text = inspect.getsource(src)
    for writer in ("set_by_operator", "clear_by_operator", "prompts.answer", "set_store(", "._values", "attest_slots"):
        assert writer not in text, f"optics_sources must not touch the operator's writers: {writer}"
    assert src.SAFE_DIRECTION_SKILLS == ("LaserEmissionOff", "StopTemperatureRamp")


# ═══ device resolution ════════════════════════════════════════════════════════════════════════════════


def test_no_device_configured_is_a_clear_failure(tmp_path):
    _bench(tmp_path, [])
    try:
        for cls in (GetLaserStatus, ReadOpticalPower, ReadTemperature, SetLightSourcePower):
            res = run(cls, {"power": 1.0} if cls is SetLightSourcePower else {})
            assert not res.success and "no enabled" in res.error
    finally:
        reset_instrument_registry(None)


def test_several_devices_of_a_kind_need_a_device_id_and_nothing_is_touched(tmp_path):
    reg = _bench(tmp_path, [{"id": "a", "type": "sim_laser"}, {"id": "b", "type": "sim_tunable_laser"}])
    try:
        res = run(LaserEmissionOn)
        assert not res.success and "pass device_id" in res.error
        res = run(SetLightSourcePower, {"power": 5.0})
        assert not res.success and "pass device_id" in res.error and "['a', 'b']" in res.error
        assert reg.laser("a").emission_state() is False
        assert run(GetLaserStatus, {"device_id": "a"}).success
        res = run(GetLaserStatus, {"device_id": "zzz"})
        assert not res.success and "no optical device" in res.error
    finally:
        reset_instrument_registry(None)


def test_a_device_of_the_wrong_kind_is_refused(bench):
    res = run(GetLaserStatus, {"device_id": "tc"})
    assert not res.success and "not a laser" in res.error


# ═══ LaserEmissionOn: two gates ═══════════════════════════════════════════════════════════════════════


def test_emission_on_without_the_operators_signature_is_refused_and_says_who_must_do_what(bench):
    res = run(LaserEmissionOn, {"device_id": "npi"})
    assert not res.success and res.data["refused"] == ["operator_attestation"]
    assert "mast_ask_operator" in res.error and "laser area safe" in res.error
    assert bench.laser("npi").emission_state() is False                   # nothing was switched on


def test_emission_on_needs_the_config_gate_even_with_a_signature(tmp_path):
    reg = _bench(tmp_path, [{"id": "npi", "type": "sim_laser", "allow_emission_control": False}])
    try:
        sign_as_operator()
        res = run(LaserEmissionOn)
        assert not res.success and res.data["refused"] == ["allow_emission_control"]
        assert "allow_emission_control" in res.error
        assert reg.laser("npi").emission_state() is False
    finally:
        reset_instrument_registry(None)


def test_both_gates_missing_are_reported_together(tmp_path):
    _bench(tmp_path, [{"id": "npi", "type": "sim_laser", "allow_emission_control": False}])
    try:
        res = run(LaserEmissionOn)
        assert res.data["refused"] == ["operator_attestation", "allow_emission_control"]
    finally:
        reset_instrument_registry(None)


def test_emission_on_with_both_gates_switches_on_and_records_who_signed(bench):
    sign_as_operator("alice")
    res = run(LaserEmissionOn, {"device_id": "npi"})
    assert res.success and res.data["emission"] is True and res.data["confirmed"] is True
    assert res.data["attestation"]["set_by"] == "alice" and res.data["attestation"]["valid_until"]
    assert bench.laser("npi").emission_state() is True


def test_an_expired_signature_does_not_count(bench):
    sign_as_operator()
    optical_state.get_store()._values["laser_area_safe"].valid_until = "2000-01-01T00:00:00+00:00"
    res = run(LaserEmissionOn)
    assert not res.success and res.data["refused"] == ["operator_attestation"]


def test_a_slot_that_was_set_to_false_is_not_a_signature(bench):
    optical_state.get_store().set_by_operator("laser_area_safe", False, by="alice", source="test-operator-ui")
    assert optical_state.get_store().get("laser_area_safe") is not None            # it IS set, to "not safe"
    res = run(LaserEmissionOn)
    assert not res.success and res.data["refused"] == ["operator_attestation"]
    assert bench.laser("npi").emission_state() is False


def test_other_slots_cannot_stand_in_for_the_signature(bench):
    optical_state.get_store().set_by_operator("npi_power_pct", 50, by="alice", source="test")
    assert not run(LaserEmissionOn).success


def test_the_signature_cannot_be_smuggled_in_as_a_parameter(bench):
    sk = LaserEmissionOn()
    errs = sk.validate_params({"device_id": "npi", "laser_area_safe": True})
    assert any("Unknown parameter" in e for e in errs)
    assert not optical_state.get_store().is_valid("laser_area_safe")


def test_the_remote_mode_warning_of_a_driver_is_passed_on_with_the_switch_on(bench):
    class NpiLike(SimLaser):
        emission_on_warning = "Remote mode: the front-panel switch is inactive"

    bench._station["npi"] = NpiLike(device_id="npi")
    sign_as_operator()
    res = run(LaserEmissionOn)
    assert res.success and res.data["warning"] == "Remote mode: the front-panel switch is inactive"
    bench._station["npi"] = SimLaser(device_id="npi")
    assert run(LaserEmissionOn).data["warning"] is None


def test_a_laser_without_software_emission_is_refused(bench):
    class NoEmission(SimLaser):
        SUPPORTS = frozenset()

    bench._station["npi"] = NoEmission(device_id="npi")
    sign_as_operator()
    res = run(LaserEmissionOn)
    assert not res.success and "no software emission control" in res.error


def test_a_driver_that_refuses_to_switch_on_is_reported_not_raised(bench):
    sign_as_operator()
    bench._station["npi"] = SimLaser(device_id="npi", write_enabled=False)
    res = run(LaserEmissionOn)
    assert not res.success and "write_enabled" in res.error


# ═══ LaserEmissionOff / StopTemperatureRamp: never refused ═════════════════════════════════════════════════


def test_emission_off_needs_no_signature_no_config_gate_and_confirms_by_read_back(bench):
    laser = bench.laser("npi")
    laser.emission_on()
    laser.allow_emission_control = False
    laser.write_enabled = False
    optical_state.set_store(optical_state.OpticalStateStore())             # no attestation at all
    res = run(LaserEmissionOff, {"device_id": "npi"})
    assert res.success and res.data["devices"][0]["confirmed"] is True and laser.emission_state() is False


def test_emission_off_without_device_id_switches_off_every_laser(tmp_path):
    reg = _bench(tmp_path, [{"id": "a", "type": "sim_laser"}, {"id": "b", "type": "sim_tunable_laser"},
                            {"id": "tc", "type": "sim_temperature"}])
    try:
        reg.laser("a").emission_on()
        reg.laser("b").emission_on()
        res = run(LaserEmissionOff)
        assert res.success and {d["device_id"] for d in res.data["devices"]} == {"a", "b"}
        assert reg.laser("a").emission_state() is False and reg.laser("b").emission_state() is False
    finally:
        reset_instrument_registry(None)


def test_emission_off_reports_a_laser_it_could_not_confirm_and_still_switches_the_others_off(tmp_path):
    class Stuck(SimLaser):
        def _emission_raw(self, on):
            raise RuntimeError("serial line dead")

        def _emission_state_raw(self):
            return None

        def _status_raw(self):
            raise RuntimeError("serial line dead")

    reg = _bench(tmp_path, [{"id": "a", "type": "sim_laser"}, {"id": "stuck", "type": "sim_laser"}])
    try:
        reg._station["stuck"] = Stuck(device_id="stuck")
        reg.laser("a").emission_on()
        res = run(LaserEmissionOff)
        assert not res.success and res.data["unconfirmed"] == ["stuck"]
        assert "NOT confirmed" in res.error and "Check them at the instrument" in res.error
        stuck = next(d for d in res.data["devices"] if d["device_id"] == "stuck")
        assert "serial line dead" in stuck["cause"]
        assert reg.laser("a").emission_state() is False                        # the other one was switched off anyway
    finally:
        reset_instrument_registry(None)


def test_emission_off_survives_a_laser_that_cannot_even_be_built(tmp_path):
    reg = _bench(tmp_path, [{"id": "a", "type": "sim_laser"},
                            {"id": "broken", "type": "nkt_superk", "enabled": True}])          # no port: build() fails
    try:
        reg.laser("a").emission_on()
        res = run(LaserEmissionOff)
        assert not res.success and res.data["unconfirmed"] == ["broken"]
        assert "'port' is required" in res.data["devices"][1]["cause"]
        assert reg.laser("a").emission_state() is False
    finally:
        reset_instrument_registry(None)


def test_emission_off_with_nothing_configured_says_so(tmp_path):
    _bench(tmp_path, [])
    try:
        res = run(LaserEmissionOff)
        assert not res.success and "no enabled laser device" in res.error
    finally:
        reset_instrument_registry(None)


def test_emission_off_for_a_named_but_unknown_device_fails_cleanly(bench):
    res = run(LaserEmissionOff, {"device_id": "ghost"})
    assert not res.success and res.data["unconfirmed"] == ["ghost"] and "no optical device" in res.data["devices"][0]["cause"]


def test_stop_temperature_ramp_confirms_by_the_run_state(bench):
    tc = bench.temperature_controller("tc")
    tc.set_setpoint(40, ramp_c_per_min=10)
    assert tc.status()["state"] == "running"
    tc.write_enabled = False                                                 # stopping does not need write access
    res = run(StopTemperatureRamp)
    assert res.success and res.data["devices"][0]["state"] == "stopped" and res.data["devices"][0]["confirmed"]


@pytest.mark.parametrize("state", ["running", "holding", "unknown"])
def test_stop_temperature_ramp_does_not_claim_success_when_the_state_says_otherwise(tmp_path, state):
    class DeafController(ScriptedTemp):
        def _stop_raw(self):                       # the stop is swallowed: the controller keeps its state
            pass

    reg = _bench(tmp_path, [{"id": "tcs", "type": "sim_temperature"}])
    try:
        reg._station["tcs"] = DeafController([30.0], state=state)
        res = run(StopTemperatureRamp)
        assert not res.success and res.data["unconfirmed"] == ["tcs"] and "NOT confirmed" in res.error
        assert res.data["devices"][0]["state"] == state and res.data["devices"][0]["confirmed"] is False
        reg._station["tcs"] = ScriptedTemp([30.0], state="running")
        res = run(StopTemperatureRamp)
        assert res.success and res.data["devices"][0]["state"] == "stopped"          # the honest one is confirmed
    finally:
        reset_instrument_registry(None)


def test_stop_temperature_ramp_without_a_readable_state_does_not_claim_success(tmp_path):
    _bench(tmp_path, [{"id": "tc100", "type": "tc100", "enabled": True, "port": "COM15"}])      # no register map
    try:
        res = run(StopTemperatureRamp)
        assert not res.success and "NOT confirmed" in res.error
        assert "register map" in res.data["devices"][0]["cause"] or "no pv" in res.data["devices"][0]["cause"]
    finally:
        reset_instrument_registry(None)


# ═══ the stop skills tell the device the stop came from outside a run ═════════════════════════


class OwnedCtx(Ctx):
    """What an ExecutionContext carries that matters here: WHO drives (``owner``)."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner


def test_emission_off_counts_as_an_external_stop_and_does_so_before_it_acts(tmp_path):
    seen: list[tuple[str, int]] = []

    class Spy(SimLaser):
        def _emission_raw(self, on):
            seen.append(("raw", self.external_stop_epoch))          # what the device knew when the off command ran
            return super()._emission_raw(on)

    reg = _bench(tmp_path, [{"id": "a", "type": "sim_laser"}])
    try:
        spy = Spy(device_id="a")
        reg._station["a"] = spy
        res = run(LaserEmissionOff, ctx=OwnedCtx("外部 agent ext:alice"))
        assert res.success and spy.external_stop_epoch == 1
        assert spy.external_stop_reason == "LaserEmissionOff (外部 agent ext:alice)"
        assert seen == [("raw", 1)]                                  # counted BEFORE the command, not after it
    finally:
        reset_instrument_registry(None)


def test_emission_off_without_device_id_counts_once_per_laser_even_for_one_it_cannot_confirm(tmp_path):
    class Stuck(SimLaser):
        def _emission_raw(self, on):
            raise RuntimeError("serial line dead")

        def _emission_state_raw(self):
            return None

    reg = _bench(tmp_path, [{"id": "a", "type": "sim_laser"}, {"id": "stuck", "type": "sim_laser"}])
    try:
        reg._station["stuck"] = Stuck(device_id="stuck")
        res = run(LaserEmissionOff, ctx=OwnedCtx("me"))
        assert not res.success and res.data["unconfirmed"] == ["stuck"]
        assert reg.laser("a").external_stop_epoch == 1 and reg.laser("stuck").external_stop_epoch == 1
        run(LaserEmissionOff, {"device_id": "a"}, ctx=OwnedCtx("me"))
        assert reg.laser("a").external_stop_epoch == 2 and reg.laser("stuck").external_stop_epoch == 1
    finally:
        reset_instrument_registry(None)


def test_a_caller_the_context_does_not_name_is_recorded_as_unknown(bench):
    run(LaserEmissionOff, ctx=Ctx())                                # the plain test context has no owner
    assert bench.laser("npi").external_stop_reason == "LaserEmissionOff (unknown caller)"


def test_the_drivers_own_off_is_not_an_external_stop(bench):
    laser = bench.laser("npi")
    laser.emission_on()
    assert laser.emission_off() is False and laser.external_stop_epoch == 0       # a protocol's cleanup does not count


def test_stop_temperature_ramp_counts_as_an_external_stop_before_it_acts(tmp_path):
    seen: list[int] = []

    class Spy(ScriptedTemp):
        def _stop_raw(self):
            seen.append(self.external_stop_epoch)
            self.state = "stopped"

    reg = _bench(tmp_path, [{"id": "tcs", "type": "sim_temperature"}])
    try:
        spy = Spy([30.0], state="running")
        reg._station["tcs"] = spy
        res = run(StopTemperatureRamp, ctx=OwnedCtx("手动/执行器"))
        assert res.success and spy.external_stop_epoch == 1 and seen == [1]
        assert spy.external_stop_reason == "StopTemperatureRamp (手动/执行器)"
        assert run(StopTemperatureRamp, {"device_id": "tcs"}, ctx=OwnedCtx("x")).success
        assert spy.external_stop_epoch == 2
    finally:
        reset_instrument_registry(None)


def test_a_stop_for_a_device_that_does_not_exist_counts_nothing_and_fails_cleanly(bench):
    res = run(LaserEmissionOff, {"device_id": "ghost"}, ctx=OwnedCtx("me"))
    assert not res.success and bench.laser("npi").external_stop_epoch == 0


# ═══ settings ═══════════════════════════════════════════════════════════════════════════════════════════════


def test_set_light_source_power_within_the_cap_and_refused_beyond(bench):
    res = run(SetLightSourcePower, {"power": 40.0})
    assert res.success and res.data["target"] == "power" and res.data["applied"] == 40.0 and res.data["unit"] == "pct"
    assert res.skill_name == "SetLightSourcePower"
    res = run(SetLightSourcePower, {"power": 140.0})
    assert not res.success and "never clamped" in res.error and bench.laser("npi").get_power() == 40.0
    assert res.skill_name == "SetLightSourcePower"                     # failures carry the registered name too


def test_set_light_source_power_on_a_laser_without_software_power_points_to_the_operator(bench):
    class NoPower(SimLaser):
        SUPPORTS = frozenset({"emission"})

    bench._station["npi"] = NoPower(device_id="npi")
    res = run(SetLightSourcePower, {"power": 10.0})
    assert not res.success and "npi_power_pct" in res.error and "no software power control" in res.error


def test_set_light_source_power_on_a_tunable_source_sets_the_channel_amplitude(tmp_path):
    reg = _bench(tmp_path, [{"id": "sk", "type": "sim_tunable_laser", "limits": {"min_nm": 430, "max_nm": 900,
                                                                               "max_amplitude_pct": 50}}])
    try:
        res = run(SetLightSourcePower, {"power": 33.3, "channel": 2})
        assert res.success and res.data["target"] == "amplitude" and res.data["channel"] == 2 and res.data["applied"] == 33.3
        assert reg.laser("sk").channels()[2]["amplitude_pct"] == 33.3
        assert not run(SetLightSourcePower, {"power": 50.1}).success
        assert not run(SetLightSourcePower, {"power": 5, "channel": 9}).success
        res = run(SetLightSourcePower, {"power": 5, "target": "power"})
        assert not res.success and "not software-controllable" in res.error or "no software power" in res.error
    finally:
        reset_instrument_registry(None)


def test_set_light_source_power_with_writes_disabled_is_refused_with_the_reason(tmp_path):
    reg = _bench(tmp_path, [{"id": "sk", "type": "sim_tunable_laser", "write_enabled": False,
                             "limits": {"min_nm": 430, "max_nm": 900, "max_amplitude_pct": 50}}])
    try:
        res = run(SetLightSourcePower, {"power": 10.0})
        assert not res.success and "write_enabled" in res.error and "bench self-test" in res.error
        assert reg.laser("sk").channels()[0]["amplitude_pct"] == 0.0
    finally:
        reset_instrument_registry(None)


def test_set_laser_wavelength(tmp_path):
    reg = _bench(tmp_path, [{"id": "sk", "type": "sim_tunable_laser", "limits": {"min_nm": 430, "max_nm": 900}},
                            ])
    try:
        res = run(SetLaserWavelength, {"wavelength_nm": 520.5, "channel": 1})
        assert res.success and res.data["applied"] == 520.5 and reg.laser("sk").get_wavelength_nm(channel=1) == 520.5
        for bad in (429.0, 901.0):
            res = run(SetLaserWavelength, {"wavelength_nm": bad})
            assert not res.success and "never clamped" in res.error
        res = run(SetLaserWavelength, {"wavelength_nm": 600, "crystal": 2})
        assert not res.success and "no crystal switch" in res.error                 # the sim has none; nothing was set
    finally:
        reset_instrument_registry(None)


def test_set_laser_wavelength_on_a_fixed_laser_is_refused(bench):
    res = run(SetLaserWavelength, {"wavelength_nm": 1000.0})
    assert not res.success and "fixed" in res.error


def test_set_laser_wavelength_crystal_switch_is_explicit_and_reports_rf_off(tmp_path):
    from mast.instruments.laser import SimTunableLaser

    class TwoCrystal(SimTunableLaser):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.switched = []

        def select_crystal(self, crystal):
            self.switched.append(crystal)
            return {"crystal": crystal, "changed": True, "rf_power": False}

    reg = _bench(tmp_path, [{"id": "sk", "type": "sim_tunable_laser", "limits": {"min_nm": 430, "max_nm": 900}}])
    try:
        dev = reg._station["sk"] = TwoCrystal(device_id="sk", limits=LaserLimits(min_nm=430, max_nm=900))
        res = run(SetLaserWavelength, {"wavelength_nm": 850.0, "crystal": 2})
        assert res.success and dev.switched == [2] and res.data["crystal_switch"]["rf_power"] is False
        assert "LaserEmissionOn" in res.data["note"]
        run(SetLaserWavelength, {"wavelength_nm": 800.0})
        assert dev.switched == [2]                                                   # never switched silently
    finally:
        reset_instrument_registry(None)


def test_set_temperature_within_limits_and_refused_outside(bench):
    res = run(SetTemperature, {"setpoint_c": 40.0, "ramp_c_per_min": 10.0})
    assert res.success and res.data["target_c"] == 40.0 and res.data["status_after"]["state"] == "running"
    assert not run(SetTemperature, {"setpoint_c": 1000.0}).success
    res = run(SetTemperature, {"setpoint_c": 30.0, "ramp_c_per_min": 99.0})
    assert not res.success and "never clamped" in res.error


def test_set_temperature_with_writes_disabled(tmp_path):
    _bench(tmp_path, [{"id": "tc", "type": "sim_temperature", "write_enabled": False}])
    try:
        res = run(SetTemperature, {"setpoint_c": 30.0})
        assert not res.success and "write_enabled" in res.error
    finally:
        reset_instrument_registry(None)


# ═══ reads ═════════════════════════════════════════════════════════════════════════════════════════════════════


def test_get_laser_status_carries_the_operator_state(bench):
    res = run(GetLaserStatus)
    assert res.success and res.data["emission"] is False and "power" in res.data["supports"]
    assert res.data["operator_state"]["laser_area_safe"]["valid"] is False
    sign_as_operator("bob")
    res = run(GetLaserStatus)
    assert res.data["operator_state"]["laser_area_safe"]["valid"] is True
    assert res.data["operator_state"]["laser_area_safe"]["set_by"] == "bob"


def test_read_optical_power_in_three_units_and_with_a_wavelength(bench):
    laser = bench.laser("npi")
    pm = bench.power_meter("pm")
    pm.source = laser.output_w
    laser.emission_on()
    laser.set_power(50.0)
    res = run(ReadOpticalPower, {"wavelength_nm": 1064.0, "averages": 3})
    assert res.success and res.data["power_w"] == pytest.approx(0.1) and res.data["power_mw"] == pytest.approx(100.0)
    assert res.data["power_dbm"] == pytest.approx(20.0) and res.data["wavelength_nm"] == 1064.0 and res.data["averages"] == 3
    laser.emission_off()
    res = run(ReadOpticalPower)
    assert res.success and res.data["power_w"] == 0.0 and res.data["power_dbm"] is None
    res = run(ReadOpticalPower, {"wavelength_nm": 50.0})
    assert not res.success and "outside" in res.error
    assert any("averages" in e for e in ReadOpticalPower().validate_params({"averages": 1000}))


def test_read_temperature(bench):
    res = run(ReadTemperature)
    assert res.success and res.data["state"] == "stopped" and res.data["pv_c"] == pytest.approx(22.0, abs=0.5)


def test_read_temperature_on_an_unconfigured_tc100_says_not_supported(tmp_path):
    _bench(tmp_path, [{"id": "tc100", "type": "tc100", "enabled": True, "port": "COM15"}])
    try:
        res = run(ReadTemperature)
        assert not res.success and "not supported" in res.error and "register map" in res.error
    finally:
        reset_instrument_registry(None)


def test_an_unreachable_device_is_unavailable_not_a_crash(tmp_path, monkeypatch):
    class Busy:
        def __init__(self, settings):
            pass

        def open(self):
            from mast.environment.serial_transport import SerialUnavailable

            raise SerialUnavailable("PermissionError(13)")

    monkeypatch.setattr(npi_rainbow, "open_transport", Busy)
    _bench(tmp_path, [{"id": "npi1064", "type": "npi_rainbow", "enabled": True, "port": "COM13"}])
    try:
        res = run(GetLaserStatus)
        assert not res.success and "device unavailable" in res.error and "Rainbow OEM" in res.error
    finally:
        reset_instrument_registry(None)


# ═══ WaitTemperatureStable ═══════════════════════════════════════════════════════════════════════════════════════


class ScriptedTemp(TemperatureController):
    """PV follows a script, one entry per read; SV fixed."""

    def __init__(self, pvs, *, sv=30.0, state="running", fail_at=(), targets=None, svs=None):
        super().__init__(device_id="tcs", write_enabled=True, limits=TemperatureLimits(min_c=-190, max_c=300))
        self.pvs, self.sv, self.state, self.fail_at = list(pvs), sv, state, set(fail_at)
        self.targets, self.svs = targets, svs          # optional per-read final target / ramping SV
        self.reads = 0

    def _status_raw(self):
        i = self.reads
        self.reads += 1
        if i in self.fail_at:
            raise RuntimeError("line noise")
        st = {"pv_c": self.pvs[min(i, len(self.pvs) - 1)], "state": self.state}
        sv = self.svs[min(i, len(self.svs) - 1)] if self.svs is not None else self.sv
        if sv is not None:
            st["sv_c"] = sv
        if self.targets is not None:
            st["target_c"] = self.targets[min(i, len(self.targets) - 1)]
        return st

    def _set_setpoint_raw(self, c, ramp):
        return {"target_c": c}

    def _stop_raw(self):
        self.state = "stopped"


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 0.0}
    monkeypatch.setattr(src, "_monotonic", lambda: t["now"])
    monkeypatch.setattr(src, "_sleep", lambda s: t.__setitem__("now", t["now"] + s))
    return t


def _scripted(tmp_path, dev):
    reg = _bench(tmp_path, [{"id": "tcs", "type": "sim_temperature"}])
    reg._station["tcs"] = dev
    return reg


def test_wait_succeeds_when_the_pv_stays_in_the_band_for_hold_s(tmp_path, clock):
    _scripted(tmp_path, ScriptedTemp([25, 28, 29.8, 30.1, 30.0, 29.9, 30.2, 30.0, 30.0]))
    try:
        res = run(WaitTemperatureStable, {"tolerance_c": 0.5, "hold_s": 8.0, "poll_s": 2.0, "timeout_s": 100.0})
        assert res.success and res.data["reached"] is True and res.data["target_c"] == 30.0     # SV used
        assert res.data["elapsed_s"] == 12.0                                                   # in band at t=4, held to t=12
        st = res.data["hold_stats"]
        assert st["n"] == 5 and st["min_c"] == 29.8 and st["max_c"] == 30.2
    finally:
        reset_instrument_registry(None)


def test_leaving_the_band_restarts_the_hold_timer(tmp_path, clock):
    _scripted(tmp_path, ScriptedTemp([29.9, 29.9, 31.0, 29.9, 29.9, 29.9, 29.9, 29.9]))
    try:
        res = run(WaitTemperatureStable, {"hold_s": 6.0, "poll_s": 2.0, "timeout_s": 100.0})
        assert res.success and res.data["elapsed_s"] == 12.0          # not 6.0: the excursion at t=4 reset the timer
    finally:
        reset_instrument_registry(None)


def test_wait_times_out_with_a_failure_and_says_where_it_stood(tmp_path, clock):
    _scripted(tmp_path, ScriptedTemp([25.0]))
    try:
        res = run(WaitTemperatureStable, {"hold_s": 5.0, "poll_s": 2.0, "timeout_s": 10.0})
        assert not res.success and res.data["reached"] is False and "did not settle" in res.error
        assert "last PV=25.0" in res.error and res.data["elapsed_s"] >= 10.0
    finally:
        reset_instrument_registry(None)


def test_wait_with_an_explicit_target_ignores_sv(tmp_path, clock):
    _scripted(tmp_path, ScriptedTemp([25.0, 25.1, 25.0, 25.1], sv=99.0))
    try:
        res = run(WaitTemperatureStable, {"target_c": 25.0, "tolerance_c": 0.2, "hold_s": 2.0, "poll_s": 1.0})
        assert res.success and res.data["target_c"] == 25.0
    finally:
        reset_instrument_registry(None)


def test_wait_can_be_aborted_and_does_not_claim_stability(tmp_path, clock):
    _scripted(tmp_path, ScriptedTemp([30.0]))
    try:
        res = run(WaitTemperatureStable, {"hold_s": 600.0, "poll_s": 2.0, "timeout_s": 3600.0}, ctx=Ctx(abort_after=3))
        assert not res.success and "aborted" in res.error and res.data["reached"] is False
        assert clock["now"] < 60                                       # it stopped quickly instead of waiting out the hold
    finally:
        reset_instrument_registry(None)


def test_wait_refuses_to_wait_for_a_stopped_controller(tmp_path, clock):
    _scripted(tmp_path, ScriptedTemp([22.0], state="stopped"))
    try:
        res = run(WaitTemperatureStable, {"target_c": 30.0})
        assert not res.success and "stopped" in res.error and clock["now"] == 0.0
    finally:
        reset_instrument_registry(None)


def test_wait_survives_a_few_failed_reads_but_not_three_in_a_row(tmp_path, clock):
    _scripted(tmp_path, ScriptedTemp([30.0] * 20, fail_at={1, 2}))
    try:
        assert run(WaitTemperatureStable, {"hold_s": 4.0, "poll_s": 1.0}).success
    finally:
        reset_instrument_registry(None)
    _scripted(tmp_path, ScriptedTemp([30.0] * 20, fail_at={1, 2, 3}))
    try:
        res = run(WaitTemperatureStable, {"hold_s": 4.0, "poll_s": 1.0})
        assert not res.success and "unreadable 3 times" in res.error and "line noise" in res.error
    finally:
        reset_instrument_registry(None)


def test_wait_without_a_target_waits_for_the_final_target_not_the_ramping_sv(tmp_path, clock):
    ramp = [22.0, 24.0, 26.0, 28.0, 30.0]                       # the controller's SV moves along the ramp, PV tracks it
    _scripted(tmp_path, ScriptedTemp(ramp, svs=ramp, targets=[40.0]))
    try:
        res = run(WaitTemperatureStable, {"hold_s": 4.0, "poll_s": 2.0, "timeout_s": 30.0})
        # PV sits on the ramp's SV from t=8 on, but the FINAL target is 40: tracking the ramp is not arriving
        assert not res.success and res.data["target_c"] == 40.0 and "did not settle at 40.0" in res.error
    finally:
        reset_instrument_registry(None)
    _scripted(tmp_path, ScriptedTemp(ramp + [40.0] * 6, svs=ramp + [40.0] * 6, targets=[40.0]))
    try:
        res = run(WaitTemperatureStable, {"hold_s": 4.0, "poll_s": 2.0, "timeout_s": 60.0})
        assert res.success and res.data["target_c"] == 40.0 and res.data["final_pv_c"] == 40.0
    finally:
        reset_instrument_registry(None)


def test_wait_follows_a_retarget_and_restarts_the_hold(tmp_path, clock):
    _scripted(tmp_path, ScriptedTemp([30.0] * 3 + [40.0] * 8, targets=[30.0, 30.0, 40.0]))     # re-targeted at the 3rd read
    try:
        res = run(WaitTemperatureStable, {"hold_s": 6.0, "poll_s": 2.0, "timeout_s": 60.0})
        assert res.success and res.data["target_c"] == 40.0
        # reads at t=0,2 sit at 30 (target 30); at t=4 the target becomes 40 (window reset, PV still 30);
        # PV reaches 40 at t=6, so the 6 s hold ends at t=12 - not at t=6, when the old window would have ended
        assert res.data["elapsed_s"] == 12.0
    finally:
        reset_instrument_registry(None)


def test_wait_needs_a_target_from_somewhere(tmp_path, clock):
    _scripted(tmp_path, ScriptedTemp([30.0], sv=None))
    try:
        res = run(WaitTemperatureStable)
        assert not res.success and "target_c" in res.error
    finally:
        reset_instrument_registry(None)


def test_wait_has_hard_caps_and_the_bounds_are_enforced_twice(tmp_path, clock):
    sk = WaitTemperatureStable()
    assert any("timeout_s" in e for e in sk.validate_params({"timeout_s": 1e9}))
    assert any("hold_s" in e for e in sk.validate_params({"hold_s": 1e9}))
    assert any("tolerance_c" in e for e in sk.validate_params({"tolerance_c": 0}))
    meta = {p.name: p for p in sk.metadata().parameters}
    assert meta["timeout_s"].max_value == 6 * 3600 and meta["hold_s"].max_value == 3600
    _scripted(tmp_path, ScriptedTemp([30.0]))
    try:                                                              # execute() does not trust its caller either
        res = sk.execute(Ctx(), {"timeout_s": 1e9})
        assert not res.success and "timeout_s" in res.error
    finally:
        reset_instrument_registry(None)


def test_wait_on_a_controller_that_cannot_report_is_not_supported_at_once(tmp_path, clock):
    _bench(tmp_path, [{"id": "tc100", "type": "tc100", "enabled": True, "port": "COM15"}])
    try:
        res = run(WaitTemperatureStable, {"target_c": 30.0})
        assert not res.success and "not supported" in res.error and clock["now"] == 0.0
    finally:
        reset_instrument_registry(None)


def test_wait_against_the_simulated_stage_end_to_end(bench):
    tc = bench.temperature_controller("tc")
    tc.set_setpoint(35.0, ramp_c_per_min=20.0)
    res = run(WaitTemperatureStable, {"tolerance_c": 0.5, "hold_s": 0.2, "poll_s": 0.05, "timeout_s": 20.0})
    assert res.success and abs(res.data["final_pv_c"] - 35.0) <= 0.5


# ═══ IdentifyOpticalSource ═════════════════════════════════════════════════════════════════════════════════════════


def test_identify_checks_every_enabled_device_read_only_and_lists_the_disabled_ones(tmp_path, monkeypatch):
    class Quiet:
        """An NPI Rainbow that answers like the manual says."""

        def __init__(self, settings):
            self.sent = []

        def open(self):
            pass

        def transact(self, payload, **kw):
            self.sent.append(bytes(payload))
            text = bytes(payload).decode().rstrip("\r")
            return {"gProduct": b"1064\r\n", "gSN": b"XZ00000000\r\n", "gstatus": b"LED Status: 1100\r\n",
                    "g_case_temp": b"0C80\r\n"}[text]

        def close(self):
            pass

    monkeypatch.setattr(npi_rainbow, "open_transport", Quiet)
    _bench(tmp_path, [
        {"id": "npi1064", "type": "npi_rainbow", "enabled": True, "port": "COM13"},
        {"id": "tc100", "type": "tc100", "enabled": True, "port": "COM15"},
        {"id": "superk", "type": "nkt_superk", "enabled": False, "port": "COM14"},
        {"id": "pm", "type": "sim_power_meter"}])
    try:
        res = run(IdentifyOpticalSource)
        by_id = {d["device_id"]: d for d in res.data["devices"]}
        assert by_id["npi1064"]["ok"] is True and by_id["npi1064"]["identity"]["serial_number"] == "XZ00000000"
        assert by_id["npi1064"]["gates"] == {"write_enabled": False, "allow_emission_control": False,
                                             "operator_guard": True}      # build() installed the attestation guard
        assert by_id["tc100"]["ok"] is False and "scan_registers" in by_id["tc100"]["identity"]["hint"]
        assert by_id["superk"]["ok"] is None and "enabled=false" in by_id["superk"]["note"]
        assert by_id["pm"]["ok"] is True
        assert not res.success and "tc100" in res.error                                # a failing device fails the check...
        assert set(by_id) == {"npi1064", "tc100", "superk", "pm"}                      # ...but all were reported
        one = run(IdentifyOpticalSource, {"device_id": "npi1064"})
        assert one.success and [d["device_id"] for d in one.data["devices"]] == ["npi1064"]
        lasers = run(IdentifyOpticalSource, {"kind": "laser"})
        assert {d["device_id"] for d in lasers.data["devices"]} == {"npi1064", "superk"}
        assert not run(IdentifyOpticalSource, {"device_id": "ghost"}).success
    finally:
        reset_instrument_registry(None)
