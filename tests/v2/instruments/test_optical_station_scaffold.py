"""Contract tests for the optical-station scaffold (docs/v2/design/optical_station_witec.md §6).

Sims and in-memory operator backends only — no hardware, no service.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from mast.instruments.laser import (EmissionControlDisabled, LaserLimitError, LaserLimits,
                                    SimLaser, SimTunableLaser)
from mast.instruments.microscope import (MicroscopeLimitError, MicroscopeLimits, SimMicroscope,
                                         WriteAccessDenied)
from mast.instruments.registry import InstrumentRegistry
from mast.instruments.temperature import (SimTemperatureController, TemperatureLimitError,
                                          TemperatureWriteDisabled)
from mast.operator_hitl import optical_state, prompts


@pytest.fixture()
def registry(tmp_path):
    cfg = {"devices": [
        {"id": "witec", "type": "sim_microscope"},
        {"id": "nolock", "type": "sim_microscope", "sim_write_access": False},
        {"id": "superk", "type": "sim_tunable_laser"},
        {"id": "npi", "type": "sim_laser", "allow_emission_control": False},
        {"id": "pm", "type": "sim_power_meter"},
        {"id": "tc", "type": "sim_temperature", "time_scale": 600.0, "tau_s": 1.0},
    ]}
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    reg = InstrumentRegistry(config_path=p)
    yield reg
    reg.close_all()


@pytest.fixture(autouse=True)
def fresh_operator_backends():
    old_b = prompts.set_backend(prompts.InMemoryPromptBackend())
    old_s = optical_state.set_store(optical_state.OpticalStateStore())
    yield
    prompts.set_backend(old_b)
    optical_state.set_store(old_s)


# ── registry ────────────────────────────────────────────────────────────────

def test_registry_kinds_and_shared_microscope_spectrometer(registry):
    kinds = {d["id"]: d["kind"] for d in registry.list_devices()}
    assert kinds == {"witec": "microscope", "nolock": "microscope", "superk": "laser",
                     "npi": "laser", "pm": "power_meter", "tc": "temperature"}
    mic = registry.microscope("witec")
    assert registry.spectrometer("witec") is mic          # one object, two roles
    with pytest.raises(Exception, match="is a microscope"):
        registry.controller("witec")
    with pytest.raises(Exception, match="not a laser"):
        registry.laser("witec")
    assert registry.is_connected("witec")


def test_registry_subclass_without_init_still_works():
    """Skill tests (test_optics_stage/scan/pump_probe) subclass the registry
    without calling ``__init__``; the station cache must not break them."""
    class Bare(InstrumentRegistry):
        def __init__(self):                                # deliberately no super()
            self._lock = threading.RLock()
            self._config = {"devices": [{"id": "las", "type": "sim_laser"}]}
            self._controllers = {}

    reg = Bare()
    assert [d["connected"] for d in reg.list_devices()] == [False]
    assert reg.is_connected("las") is False
    assert reg.built_station_devices() == {}
    assert reg.stop_all(wait_s=1) == {"stopped": [], "errors": {}, "pending": [], "unconfirmed": {}}
    las = reg.laser("las")
    assert reg.is_connected("las") and reg.built_station_devices() == {"las": las}
    assert reg.release("las") is True and reg.is_connected("las") is False
    reg.laser("las")
    reg.close_all()
    assert reg.built_station_devices() == {}
    bare = Bare()                                          # teardown path of the skill tests
    bare.close_all()
    assert bare.release("nope") is False


def _reg(tmp_path, devices):
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": devices}), encoding="utf-8")
    return InstrumentRegistry(config_path=p)


def test_resolve_spectrometer_falls_back_to_the_microscope(tmp_path):
    from mast.instruments.base import InstrumentError, InstrumentUnavailable
    from mast.skills.builtins.optics_common import resolve_device

    def pick(devs):
        return resolve_device(_reg(tmp_path, devs), kind="spectrometer", role="raman_ccd",
                              device_id=None)

    mic = {"id": "witec", "type": "sim_microscope"}
    assert pick([mic]) == "witec"                         # WITec station: the only one
    # a TERS station that has a spectrometer never changes its answer
    assert pick([mic, {"id": "ccd", "type": "sim_spectrometer"}]) == "ccd"
    assert pick([mic, {"id": "ccd", "type": "sim_spectrometer", "role": "raman_ccd"}]) == "ccd"
    with pytest.raises(InstrumentError, match="pass device_id"):
        pick([mic, {"id": "w2", "type": "sim_microscope"}])
    with pytest.raises(InstrumentUnavailable):
        pick([{"id": "las", "type": "sim_laser"}])
    # other kinds never fall back
    with pytest.raises(InstrumentUnavailable):
        resolve_device(_reg(tmp_path, [mic]), kind="rotation", role=None, device_id=None)


def test_spectrometer_skill_runs_on_the_microscope(tmp_path):
    from mast.instruments.registry import reset_instrument_registry
    from mast.skills.builtins.optics_spectro import GetSpectrometerStatus

    reg = _reg(tmp_path, [{"id": "witec", "type": "sim_microscope"}])
    reset_instrument_registry(reg)
    try:
        res = GetSpectrometerStatus().execute(None, {})
        assert res.success, res.error
        assert reg.spectrometer("witec") is reg.microscope("witec")
    finally:
        reset_instrument_registry(None)


def test_stop_all_turns_lasers_off_and_stops_microscope(registry):
    las = registry.laser("superk")
    las.emission_on()
    mic = registry.microscope("witec")
    rep = registry.stop_all(wait_s=2)
    assert set(rep["stopped"]) >= {"superk", "witec"}
    assert las.emission_state() is False
    assert ("stop_all", None) in mic.calls


# ── microscope ──────────────────────────────────────────────────────────────

def test_microscope_limits_refuse_never_clamp():
    mic = SimMicroscope(microscope_limits=MicroscopeLimits(
        z_um=(-100, 100), max_step_um=50, max_laser_power_mw={"532": 20}))
    with pytest.raises(MicroscopeLimitError, match="no travel range"):
        mic.move_stage_um(x=10)                           # x range unset → fail-closed
    with pytest.raises(MicroscopeLimitError, match="max_step_um"):
        mic.move_stage_um(z=80)
    assert mic.move_stage_um(z=40)["z"] == 40
    with pytest.raises(MicroscopeLimitError, match="outside"):
        mic.set_analyzer_deg(400)
    mic.select_laser("532")
    with pytest.raises(MicroscopeLimitError, match="outside"):
        mic.set_laser_power_mw(25)
    assert mic.set_laser_power_mw(5) == 5
    mic.select_laser("355")
    with pytest.raises(MicroscopeLimitError, match="no software power control"):
        mic.set_laser_power_mw(1)


def test_write_access_must_be_handed_over(registry):
    mic = registry.microscope("nolock")
    assert mic.microscope_status()["write_access"] is False
    with pytest.raises(WriteAccessDenied, match="COM Automation"):
        mic.set_analyzer_deg(10)
    mic.set_shutter(False)                                # closing never needs write access
    mic.grant_write_access(True)                          # the operator pressed the button
    assert mic.set_analyzer_deg(10) == 10


def test_sim_spectrum_follows_shutter_and_shg_source():
    mic = SimMicroscope(seed=1)
    mic.configure(exposure_ms=100)
    dark = mic.acquire(exposure_ms=100, frames=1).frames[0]
    mic.set_shutter(True)
    lit = mic.acquire(exposure_ms=100, frames=1).frames[0]
    assert lit.max() > 3 * dark.max()
    mic.set_shutter(False)
    mic.set_center_wavelength(500.0)
    mic.shg_source = lambda: (1000.0, 0.01)
    shg = mic.acquire(exposure_ms=100, frames=1)
    axis = shg.axis_nm
    assert abs(axis[int(shg.frames[0].argmax())] - 500.0) < 2.0


# ── lasers ──────────────────────────────────────────────────────────────────

def test_emission_gate_and_safe_off(registry):
    npi = registry.laser("npi")
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        npi.emission_on()
    assert npi.emission_off() is False                    # off is always attempted


def test_tunable_limits():
    las = SimTunableLaser(limits=LaserLimits(min_nm=500, max_nm=900, max_amplitude_pct=40))
    with pytest.raises(LaserLimitError, match="outside"):
        las.set_wavelength_nm(1000)
    with pytest.raises(LaserLimitError, match="outside"):
        las.set_amplitude_pct(50)
    with pytest.raises(LaserLimitError, match="channel"):
        las.set_wavelength_nm(600, channel=9)
    assert las.set_wavelength_nm(810, channel=1) == 810
    las.set_amplitude_pct(30, channel=1)
    las.emission_on()
    nm, w = las.output()
    assert nm == 810 and w > 0


def test_write_disabled_blocks_settings_but_not_off():
    las = SimLaser(write_enabled=False)
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        las.set_power(10)
    assert las.emission_off() is False


# ── temperature ─────────────────────────────────────────────────────────────

def test_temperature_limits_and_sim(registry):
    tc = registry.temperature_controller("tc")
    with pytest.raises(TemperatureLimitError, match="outside"):
        tc.set_setpoint(1000)
    with pytest.raises(TemperatureLimitError, match="ramp"):
        tc.set_setpoint(50, ramp_c_per_min=99)
    tc.set_setpoint(40, ramp_c_per_min=20)
    deadline = time.monotonic() + 3
    while abs((tc.read_c() or 0) - 40) > 0.5 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert abs(tc.read_c() - 40) <= 0.5
    off = SimTemperatureController(write_enabled=False)
    with pytest.raises(TemperatureWriteDisabled):
        off.set_setpoint(30)
    off.stop()


# ── operator prompts & optical state ───────────────────────────────────────

def test_ask_answered_by_fake_operator():
    p = prompts.OperatorPrompt(kind="choice", title="Which plate?",
                               options=[prompts.PromptOption("a", "A"), prompts.PromptOption("b", "B")])
    with prompts.FakeOperator(lambda pr: {"choice": "b"}):
        ans = prompts.ask(p, timeout_s=3)
    assert ans.status == "answered" and ans.choice == "b" and ans.answered_by == "test-operator"


def test_attest_answer_writes_optical_state():
    p = prompts.OperatorPrompt(kind="attest", title="Swap the λ/2", attest_slots=["excitation_waveplate"],
                               options=[prompts.PromptOption("hwp_1064", "1064"),
                                        prompts.PromptOption("hwp_532", "532")])
    with prompts.FakeOperator(lambda pr: {"choice": "hwp_1064"}):
        prompts.ask(p, timeout_s=3)
    store = optical_state.get_store()
    assert store.is_valid("excitation_waveplate", expect="hwp_1064")
    assert store.waveplate_covers("excitation_waveplate", 1064) is True
    assert store.waveplate_covers("excitation_waveplate", 800) is False
    snap = store.snapshot()
    assert snap["excitation_waveplate"]["source"].startswith("prompt:")
    assert snap["laser_area_safe"]["valid"] is False


def test_ask_timeout_and_abort_cancel_the_prompt():
    p = prompts.OperatorPrompt(kind="confirm", title="Nobody answers")
    ans = prompts.ask(p, timeout_s=0.1, poll_s=0.02)
    assert ans.status == "timeout"
    assert prompts.get(ans.prompt_id).status == "timeout"
    flag = threading.Event()
    threading.Timer(0.05, flag.set).start()
    with pytest.raises(RuntimeError, match="aborted"):
        prompts.ask(prompts.OperatorPrompt(kind="confirm", title="Abort me"),
                    check_abort=flag.is_set, poll_s=0.01)
    assert not prompts.list_open()


def test_answer_validation():
    pid = prompts.post(prompts.OperatorPrompt(kind="number", title="Angle", number_min=0,
                                              number_max=360, unit="deg"))
    with pytest.raises(prompts.PromptError, match="above"):
        prompts.answer(pid, by="op", source="test", value=400)
    with pytest.raises(prompts.PromptError, match="operator's name"):
        prompts.answer(pid, by="", source="test", value=10)
    assert prompts.answer(pid, by="op", source="test", value=10).value == 10.0
    with pytest.raises(prompts.PromptError, match="already"):
        prompts.answer(pid, by="op", source="test", value=11)
    with pytest.raises(prompts.PromptError, match="kind"):
        prompts.post(prompts.OperatorPrompt(kind="sign-here", title="x"))


def test_optical_state_expiry_and_operator_only_name():
    store = optical_state.get_store()
    with pytest.raises(optical_state.OpticalStateError, match="operator's name"):
        store.set_by_operator("laser_area_safe", True, by="", source="web")
    sv = store.set_by_operator("laser_area_safe", True, by="alice", source="web")
    assert sv.valid_until and store.is_valid("laser_area_safe", expect=True)
    store._values["laser_area_safe"].valid_until = "2000-01-01T00:00:00+00:00"
    assert not store.is_valid("laser_area_safe")
    with pytest.raises(optical_state.OpticalStateError, match="not one of"):
        store.set_by_operator("objective", "200x", by="alice", source="web")
    assert [h["slot"] for h in store.history] == ["laser_area_safe"]


def test_builtin_station_modules_are_registered():
    import mast.skills.builtins as b

    for name in ("optics_witec", "optics_sources", "operator_skills", "optics_protocols"):
        assert hasattr(b, name)
