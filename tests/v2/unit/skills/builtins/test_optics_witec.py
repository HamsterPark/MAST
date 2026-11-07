"""WITec atomic skills (docs/v2/design/optical_station_witec.md §6.3) on a ``witec_com`` registry entry whose client
is the in-memory fake WITec Control — and the existing AcquireSpectrum family running on the same device."""
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
from types import SimpleNamespace

import numpy as np
import pytest

from mast.core.registry import SkillRegistry
from mast.i18n import use_lang
from mast.core.sample_gate import requires_sample
from mast.core.types import HardwareState, NanonisCallRecord, SafetyLevel, SkillCategory
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.instruments.witec import microscope as wm
from mast.instruments.witec import params as P
from mast.instruments.witec.bucs import BucsNoValue
from mast.instruments.witec.fake_bucs import DEFAULT_PRESETS, FakeBucsClient, VirtualClock
from mast.live.testing import LiveRecorder, isolated_live_hub
from mast.logging import experiment_log as el
from mast.operator_hitl import optical_state, prompts
from mast.skills.base import BaseSkill
from mast.skills.builtins import optics_witec as ow
from mast.skills.builtins.optics_spectro import (AcquireSpectrum, ConfigureSpectrometer, GetSpectrometerStatus,
                                                 SetSpectrometerCenter, SetSpectrometerGrating, StopSpectrometer)
from mast.skills.builtins.optics_witec import (AcquireWITecVideoImage, AutoFocusWITec, CloseWITecShutter,
                                               GetAnalyzerAngle, GetStagePosition, GetWITecStatus, ListWITecLasers,
                                               ListWITecStates, LoadWITecState, MoveWITecStage,
                                               ReleaseWITecWriteAccess, RequestWITecWriteAccess, RunAnalyzerSeries,
                                               SaveWITecProject, SelectWITecLaser, SetAnalyzerAngle, SetBeamPath,
                                               SetWITecLaserPower, SetWITecShutter, StopWITec)

ENTRY = {"id": "witec", "type": "witec_com", "name": "WITec alpha300 (UHTS600)", "enabled": True,
         "allow_emission_control": True,                    # D7 gate 1 - an explicit config line; default is false
         "poll_s": 0.01, "stage_poll_s": 0.01, "project_timeout_s": 3.0,
         "limits": {"min_exposure_ms": 10, "max_exposure_ms": 600000, "max_total_s": 7200,
                    "min_center_nm": 300, "max_center_nm": 1100},
         "microscope_limits": {"x_um": [-25000, 25000], "y_um": [-25000, 25000], "z_um": [-100, 100],
                               "max_step_um": 5000, "max_laser_power_mw": {"532": 20, "633": 10}}}


class FakeStorage:
    def __init__(self):
        self.exp = {"e1": {"name": "WITec Raman", "start_time": "2026-11-03T10:00:00", "dir_name": ""}}
        self.smp = {"s1": {"name": "WS2 flake", "sample_index": 1, "dir_name": "", "description": "",
                           "sample_type": "", "sample_subtype": "", "start_time": "2026-11-03T10:01:00"}}

    def get_experiment(self, eid):
        return dict(self.exp[eid]) if eid in self.exp else None

    def get_sample(self, sid):
        return dict(self.smp[sid]) if sid in self.smp else None

    def sample_ordinal(self, sid):
        return 1

    def set_experiment_dir_name(self, eid, name):
        self.exp[eid]["dir_name"] = name

    def set_sample_dir_name(self, sid, name, idx):
        self.smp[sid]["dir_name"] = name

    def get_samples(self, eid):
        return [dict(v, id=k) for k, v in self.smp.items()]


class Ctx:
    """safe_call + state + abort — enough for AcquireSpectrum's STM bookkeeping on a station with no STM."""

    def __init__(self, *, abort=None):
        self._abort = abort
        snap = HardwareState(bias_v=0.0, current_a=0.0, z_pos_m=0.0, x_pos_m=0.0, y_pos_m=0.0,
                             z_controller_on=False, setpoint_a=0.0, withdrawn=True, scan_running=False)
        self.state = type("S", (), {"snapshot": lambda s: snap, "refresh": lambda s: snap})()

    def safe_call(self, method, *args, **kw):
        return NanonisCallRecord(method=method, return_value=("", b"", [0.0]))

    def check_abort(self):
        return bool(self._abort()) if self._abort else False


@pytest.fixture(autouse=True)
def fresh_operator_backends():
    old_b = prompts.set_backend(prompts.InMemoryPromptBackend())
    old_s = optical_state.set_store(optical_state.OpticalStateStore())
    yield
    prompts.set_backend(old_b)
    optical_state.set_store(old_s)


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    storage = FakeStorage()
    log = SimpleNamespace(_storage=storage, current_experiment_id="e1", current_sample_id="s1")
    prev = el.get_active_log()
    el.set_active_log(log)
    yield SimpleNamespace(storage=storage, log=log)
    el.set_active_log(prev)


@pytest.fixture
def station(tmp_path, monkeypatch):
    """A registry with one ``witec_com`` device on a fake WITec Control and a virtual clock."""
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    clock = VirtualClock()
    fake = FakeBucsClient(clock=clock.now, work_dir=tmp_path)
    old = wm.set_client_factory(lambda entry: fake)
    cfg_path = tmp_path / "optical_instruments.json"
    cfg_path.write_text(json.dumps({"devices": [ENTRY]}), encoding="utf-8")
    reg = InstrumentRegistry(cfg_path)
    reset_instrument_registry(reg)
    mic = reg.microscope("witec")
    mic._now, mic._sleep = clock.now, clock.sleep                  # noqa: SLF001
    yield SimpleNamespace(fake=fake, clock=clock, reg=reg, mic=mic, tmp=tmp_path, path=cfg_path)
    reset_instrument_registry(None)
    wm.set_client_factory(old)


@pytest.fixture
def granted(station):
    station.fake.operator_press_com_automation()
    assert station.mic.request_write_access()
    return station


@pytest.fixture
def signed(granted):
    """write access held AND the operator has signed laser_area_safe"""
    optical_state.get_store().set_by_operator("laser_area_safe", True, by="operator", source="test")
    return granted


def run(skill_cls, ctx=None, **params):
    return skill_cls().execute(ctx, params)


def sign_laser_area_safe(by="operator"):
    optical_state.get_store().set_by_operator("laser_area_safe", True, by=by, source="test")


# ── contract ──────────────────────────────────────────────────────────────────────────────────────────────────────

CONTRACT = {
    # docs §6.3
    "GetWITecStatus": SafetyLevel.AUTO, "GetAnalyzerAngle": SafetyLevel.AUTO, "ListWITecStates": SafetyLevel.AUTO,
    "GetStagePosition": SafetyLevel.AUTO,
    "RequestWITecWriteAccess": SafetyLevel.CONFIRM, "SetAnalyzerAngle": SafetyLevel.CONFIRM,
    "SetBeamPath": SafetyLevel.CONFIRM, "LoadWITecState": SafetyLevel.CONFIRM, "SelectWITecLaser": SafetyLevel.CONFIRM,
    "SetWITecLaserPower": SafetyLevel.CONFIRM, "MoveWITecStage": SafetyLevel.CONFIRM,
    "AutoFocusWITec": SafetyLevel.CONFIRM, "AcquireWITecVideoImage": SafetyLevel.CONFIRM,
    "RunAnalyzerSeries": SafetyLevel.CONFIRM, "SaveWITecProject": SafetyLevel.CONFIRM,
    "StopWITec": SafetyLevel.AUTO,
    # SetWITecShutter(open=True) is CONFIRM, (open=False) AUTO: one skill has one level — the stricter; the AUTO
    # close is CloseWITecShutter
    "SetWITecShutter": SafetyLevel.CONFIRM,
    # additions
    "CloseWITecShutter": SafetyLevel.AUTO, "ReleaseWITecWriteAccess": SafetyLevel.AUTO,
    "ListWITecLasers": SafetyLevel.AUTO,
}


def _module_skills():
    return {n: c for n, c in inspect.getmembers(ow, inspect.isclass)
            if issubclass(c, BaseSkill) and c.__module__ == ow.__name__}


def test_names_and_safety_levels_are_the_contract():
    got = {n: c().metadata().safety_level for n, c in _module_skills().items()}
    assert got == CONTRACT
    assert set(ow.WITEC_SKILL_NAMES) == set(CONTRACT)
    assert set(ow.WITEC_STOP_SKILL_NAMES) <= set(CONTRACT)
    for n, c in _module_skills().items():
        assert c().metadata().name == n


def test_every_skill_is_discovered_by_the_registry_with_unique_names():
    reg = SkillRegistry()
    reg.discover("mast.skills.builtins")
    names = [m.name for m in reg.list_skills()]
    for n in CONTRACT:
        assert reg.has(n) and names.count(n) == 1


def test_metadata_is_well_formed():
    for n, c in _module_skills().items():
        m = c().metadata()
        assert m.description.strip() and m.version and m.composition_level == 0
        pnames = [p.name for p in m.parameters]
        assert len(pnames) == len(set(pnames)) and ("device_id" in pnames)
        assert "witec" in m.tags
        assert m.category in (SkillCategory.READ, SkillCategory.WRITE)
        if m.safety_level == SafetyLevel.AUTO and m.category == SkillCategory.READ:
            assert {"read", "status"} & set(m.tags)


def test_sample_gate_applies_to_the_data_skills_only():
    gated = {n for n, c in _module_skills().items() if requires_sample(c().metadata(), n)}
    assert gated == {"RunAnalyzerSeries", "SaveWITecProject"}
    for n in ow.WITEC_STOP_SKILL_NAMES:
        m = _module_skills()[n]().metadata()
        assert {"safety", "stop"} & set(m.tags) and not requires_sample(m, n)


# ── device resolution and the common result shape ─────────────────────────────────────────────────────────────────

def test_no_microscope_configured_is_a_clear_failure(tmp_path):
    p = tmp_path / "oi.json"
    p.write_text(json.dumps({"devices": []}), encoding="utf-8")
    reset_instrument_registry(InstrumentRegistry(p))
    try:
        res = run(GetWITecStatus)
        assert not res.success and "no enabled microscope device" in res.error
        assert "optical_state" in res.data
    finally:
        reset_instrument_registry(None)


def test_several_microscopes_need_an_explicit_device_id(station):
    cfg = {"devices": [ENTRY, dict(ENTRY, id="witec2")]}
    station.path.write_text(json.dumps(cfg), encoding="utf-8")
    station.reg.reload()
    res = run(GetWITecStatus)
    assert not res.success and "pass device_id" in res.error and "witec2" in res.error
    ok = run(GetWITecStatus, device_id="witec")
    assert ok.success and ok.data["device_id"] == "witec"


READ_CALLS = [(GetWITecStatus, {}), (GetAnalyzerAngle, {}), (ListWITecStates, {}), (ListWITecLasers, {}),
              (GetStagePosition, {})]
WRITE_CALLS = [(SetAnalyzerAngle, {"angle_deg": 12.0}), (SetBeamPath, {"path": "video"}),
               (LoadWITecState, {"name": "SHG"}), (SelectWITecLaser, {"laser": "633"}),
               (SetWITecLaserPower, {"power_mw": 4.0}), (MoveWITecStage, {"x_um": 10.0}),
               (AutoFocusWITec, {}), (CloseWITecShutter, {}), (ReleaseWITecWriteAccess, {})]


@pytest.mark.parametrize("skill,params", READ_CALLS + WRITE_CALLS, ids=lambda v: getattr(v, "__name__", None))
def test_every_result_carries_microscope_status_and_the_optical_state(granted, skill, params):
    optical_state.get_store().set_by_operator("objective", "50x", by="op", source="test")
    if skill is SetWITecLaserPower:
        granted.mic.select_laser("532")
    res = run(skill, **params)
    assert res.success, res.error
    assert res.data["microscope_status"]["connected"] is True
    assert res.data["optical_state"]["objective"]["value"] == "50x"
    assert res.data["optical_state"]["laser_area_safe"]["valid"] is False


def test_failed_results_carry_the_state_too(station):
    res = run(SetAnalyzerAngle, angle_deg=10.0)                       # no write access
    assert not res.success
    assert "microscope_status" in res.data and "optical_state" in res.data


# ── status and reads ────────────────────────────────────────────────────────────────────────────────────────────────

def test_status_of_a_closed_witec_is_data_not_a_failure(station):
    station.fake.witec_exit()
    res = run(GetWITecStatus)
    assert res.success and res.data["microscope_status"]["connected"] is False
    assert "NOT connected" in res.summary and res.data["spectrometer"]["state"] == "unavailable"


def test_status_content(granted):
    granted.mic.select_laser("532")
    res = run(GetWITecStatus)
    st = res.data["microscope_status"]
    assert st["write_access"] is True and st["sequencer_active"] is False and st["beam_path"] == "Video"
    assert st["laser_wavelength_nm"] == pytest.approx(532.138) and st["objective"].startswith("Zeiss")
    assert st["capabilities"]["silent_spectrum"] is True and st["video"]["image_width_um"] == 120.0
    assert res.data["microscope_limits"]["z_um"] == [-100.0, 100.0]
    assert "write access yes" in res.summary


def test_reads(granted):
    granted.mic.set_analyzer_deg(77.0)
    a = run(GetAnalyzerAngle)
    assert a.data["angle_deg"] == 77.0 and a.data["coupled"] is True and "in the beam" in a.summary
    s = run(ListWITecStates)
    assert s.data["current"] == "RayShield 532 - G300" and "SHG" in s.data["states"]
    ls = run(ListWITecLasers)
    assert ls.data["selected"]["name"] == "532 nm" and len(ls.data["lasers"]) == 5
    pos = run(GetStagePosition)
    assert (pos.data["x_um"], pos.data["z_um"]) == (0.0, 0.0)


# ── write access ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_write_without_access_says_what_to_do(station):
    res = run(SetAnalyzerAngle, angle_deg=10.0)
    assert not res.success
    assert "RequestWITecWriteAccess" in res.error and "COM Automation" in res.error
    assert station.fake.writes == []


def test_request_write_access_without_the_operator_fails_once_and_clearly(station):
    res = run(RequestWITecWriteAccess)
    assert not res.success and "operator has not pressed 'COM Automation'" in res.error
    assert len([e for e in station.fake.log if e[0] == "request_write"]) == 1
    assert prompts.list_open() == []                                  # no prompt unless asked for


def test_request_write_access_granted_when_the_button_was_pressed(station):
    station.fake.operator_press_com_automation()
    res = run(RequestWITecWriteAccess)
    assert res.success and res.data["write_access"] is True and res.data["asked_operator"] is False


def test_ask_operator_posts_the_prompt_and_retries_after_the_answer(station):
    def policy(p):
        station.fake.operator_press_com_automation()                  # the person walks over and presses it …
        return {"choice": "yes"}                                      # … and says so

    with prompts.FakeOperator(policy) as op:
        res = run(RequestWITecWriteAccess, ask_operator=True, wait_s=30.0, retry_s=5.0)
    assert res.success, res.error
    assert res.data["asked_operator"] is True and res.data["write_access"] is True
    (p,) = op.seen
    assert p.kind == "confirm" and "COM Automation" in p.title and p.skill == "RequestWITecWriteAccess"
    assert p.context["device_id"] == "witec" and p.severity == "action" and p.timeout_s == 30.0
    assert prompts.list_open() == []


def test_the_operator_declining_or_not_answering_is_no_write_access(station):
    with prompts.FakeOperator(lambda p: {"choice": "no"}):
        res = run(RequestWITecWriteAccess, ask_operator=True, wait_s=30.0)
    assert not res.success and res.data["asked_operator"] is True and "declined" in res.error
    res = run(RequestWITecWriteAccess, ask_operator=True, wait_s=0.3)         # nobody there
    assert not res.success and res.data["prompt_status"] == "timeout"
    assert prompts.list_open() == []


def test_saying_yes_without_pressing_the_button_still_fails_after_the_retry_window(station):
    with prompts.FakeOperator(lambda p: {"choice": "yes"}):
        res = run(RequestWITecWriteAccess, ask_operator=True, wait_s=30.0, retry_s=0.0)
    assert not res.success and "has not pressed" in res.error


def test_aborting_while_the_prompt_is_open_cancels_it(station):
    n = {"i": 0}

    def abort():
        n["i"] += 1
        return n["i"] > 2

    res = run(RequestWITecWriteAccess, Ctx(abort=abort), ask_operator=True, wait_s=60.0)
    assert not res.success and "aborted" in res.error
    assert prompts.list_open() == []


def test_a_closed_witec_makes_the_prompt_ask_for_it_to_be_opened_first(station):
    station.fake.witec_exit()

    def policy(p):
        station.fake.witec_start()
        station.fake.operator_press_com_automation()
        station.clock.sleep(5.0)                                       # the connect cool-down passes
        return {"choice": "yes"}

    with prompts.FakeOperator(policy) as op:
        res = run(RequestWITecWriteAccess, ask_operator=True, wait_s=30.0, retry_s=5.0)
    assert res.success, res.error
    assert "WITec Control" in op.seen[0].title and "Open" in op.seen[0].title or "打开" in op.seen[0].title


def test_release_hands_the_access_back(granted):
    res = run(ReleaseWITecWriteAccess)
    assert res.success and res.data["write_access"] is False
    assert not granted.mic.has_write_access()


# ── optics skills ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_analyzer_angle_and_coupling(granted):
    res = run(SetAnalyzerAngle, angle_deg=33.0, coupled=False)
    assert res.success and res.data["angle_deg"] == 33.0 and res.data["coupled"] is False
    assert granted.fake.peek(P.ANALYZER_COUPLER) is False


def test_limits_are_refusals_not_clamps(granted):
    granted.mic.select_laser("532")
    n = len(granted.fake.writes)
    res = run(SetAnalyzerAngle, angle_deg=400.0)
    assert not res.success and "refused by microscope limit" in res.error and "400" in res.error
    res = run(MoveWITecStage, z_um=500.0)
    assert not res.success and "outside" in res.error
    res = run(MoveWITecStage, x_um=9000.0)
    assert not res.success and "max_step_um" in res.error
    res = run(SetWITecLaserPower, power_mw=25.0)
    assert not res.success and "outside [0, 20.0] mW" in res.error
    assert len(granted.fake.writes) == n                       # nothing reached WITec for any of the four
    assert granted.fake.peek(P.ANALYZER) == 0.0 and granted.fake.peek(P.LASER_POWER) == 1.0


def test_beam_path_state_laser_and_power(signed):
    assert run(SetBeamPath, path="raman").data["state"] == "Raman"
    res = run(LoadWITecState, name="RayShield 532 - G600")
    assert res.success and res.data["state"] == "RayShield 532 - G600"
    assert not run(LoadWITecState, name="Nonsense").success
    sel = run(SelectWITecLaser, laser="633")
    assert sel.success and sel.data["laser"]["wavelength_nm"] == pytest.approx(633.0)
    assert "coupler" in sel.data["note"]
    assert not run(SelectWITecLaser, laser="447").success
    pw = run(SetWITecLaserPower, power_mw=4.0)
    assert pw.success and pw.data["power_mw"] == 4.0
    run(SelectWITecLaser, laser="355")
    res = run(SetWITecLaserPower, power_mw=1.0)
    assert not res.success and "no software power control" in res.error


def test_the_unsupported_ones_say_so_and_do_not_ask_to_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "r"))
    fake = FakeBucsClient(omit=["analyzer"])
    fake.operator_press_com_automation()
    old = wm.set_client_factory(lambda entry: fake)
    p = tmp_path / "oi.json"
    p.write_text(json.dumps({"devices": [ENTRY]}), encoding="utf-8")
    reset_instrument_registry(InstrumentRegistry(p))
    try:
        res = run(GetAnalyzerAngle)
        assert not res.success and "not supported" in res.error and ("不要重试" in res.error or "do not retry" in res.error)
    finally:
        reset_instrument_registry(None)
        wm.set_client_factory(old)


# ── shutter: closing is never gated, opening needs the attestation ─────────────────────────────────────────────────────────

def test_opening_needs_the_operators_laser_area_attestation(granted):
    granted.mic.select_laser("532")
    with use_lang("en"):
        res = run(SetWITecShutter, open=True)
    assert not res.success and res.data["refused_by"] == "laser_attestation"
    assert "laser_area_safe" in res.error and "only the operator can do that" in res.error
    assert granted.fake.peek(P.LASER_SHUTTER) is False
    sign_laser_area_safe("Dr. Operator")
    ok = run(SetWITecShutter, open=True)
    assert ok.success and ok.data["shutter_open"] is True
    assert ok.data["attestation"]["set_by"] == "Dr. Operator" and ok.data["attestation"]["value"] is True
    assert granted.fake.peek(P.LASER_SHUTTER) is True


def test_a_laser_area_recorded_as_not_safe_is_refused_by_the_skill_too(granted):
    granted.mic.select_laser("532")
    optical_state.get_store().set_by_operator("laser_area_safe", False, by="operator", source="test")
    with use_lang("en"):
        res = run(SetWITecShutter, open=True)
    assert not res.success and res.data["refused_by"] == "laser_attestation" and "NOT safe" in res.error
    assert granted.fake.peek(P.LASER_SHUTTER) is False
    granted.fake.human_sets(P.LASER_SHUTTER, True)                      # a shutter that is open: WITec light on the paths
    with use_lang("en"):
        beam = run(SetBeamPath, path="raman")                           # the driver says the same on its own paths
    assert not beam.success and beam.data["refused_by"] == "laser_attestation" and "NOT safe" in beam.error


def test_an_expired_attestation_does_not_open_the_shutter(granted):
    sign_laser_area_safe()
    sv = optical_state.get_store()._values["laser_area_safe"]          # noqa: SLF001
    sv.valid_until = "2000-01-01T00:00:00+00:00"
    res = run(SetWITecShutter, open=True)
    assert not res.success and res.data["refused_by"] == "laser_attestation"


def test_the_device_entry_can_waive_the_attestation(granted, station):
    station.path.write_text(json.dumps({"devices": [dict(ENTRY, require_laser_attestation=False)]}), encoding="utf-8")
    station.reg.reload()                                    # new config; the device is rebuilt on the same fake WITec
    mic = station.reg.microscope("witec")
    assert mic.request_write_access()                       # the operator's consent is still there
    mic.select_laser("532")
    res = run(SetWITecShutter, open=True)
    assert res.success and res.data["attestation"] == {"waived": True}
    assert station.fake.peek(P.LASER_SHUTTER) is True


def test_closing_is_never_refused_by_policy(granted):
    granted.mic.select_laser("532")
    sign_laser_area_safe()
    assert run(SetWITecShutter, open=True).success
    sv = optical_state.get_store()._values["laser_area_safe"]          # noqa: SLF001
    sv.valid_until = "2000-01-01T00:00:00+00:00"                        # attestation gone …
    closed = run(SetWITecShutter, open=False)                           # … closing does not care
    assert closed.success and closed.data["shutter_open"] is False
    assert run(CloseWITecShutter).success


def test_closing_without_write_access_fails_with_witecs_reason_and_a_physical_hint(station):
    res = run(CloseWITecShutter)
    assert not res.success and "COM Automation" in res.error and "attestation" not in res.error
    assert res.data["physical_action"].startswith("close the shutter by hand")


def test_a_laser_without_a_software_shutter(granted):
    granted.mic.select_laser("355")
    res = run(CloseWITecShutter)
    assert not res.success and "no software shutter" in res.error
    sign_laser_area_safe()
    assert not run(SetWITecShutter, open=True).success


# ── stage, focus, video ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_stage_absolute_and_relative(granted):
    a = run(MoveWITecStage, x_um=100.0, y_um=-20.0, z_um=10.0)
    assert a.success and a.data["after_um"] == {"x": 100.0, "y": -20.0, "z": 10.0}
    b = run(MoveWITecStage, x_um=5.0, relative=True)
    assert b.data["after_um"]["x"] == 105.0 and b.data["before_um"]["x"] == 100.0
    assert not run(MoveWITecStage).success
    assert "at least one" in run(MoveWITecStage).error


def test_autofocus_reports_z_and_failure(granted):
    res = run(AutoFocusWITec)
    assert res.success and res.data["z_um"] == 12.5 and res.data["verified"] is True
    granted.fake.focus_ok = False
    bad = run(AutoFocusWITec)
    assert not bad.success and "LastFailed" in bad.error


def test_video_image_has_a_sidecar_with_the_scale_and_is_archived(granted, scope):
    optical_state.get_store().set_by_operator("objective", "100x", by="op", source="test")
    res = run(AcquireWITecVideoImage, tag="flake A")
    assert res.success, res.error
    png, side = Path(res.data["path"]), Path(res.data["sidecar_path"])
    assert res.data["stored_in_experiment"] and "experiments" in png.parts and png.read_bytes()[:4] == b"\x89PNG"
    meta = json.loads(side.read_text(encoding="utf-8"))
    assert meta["schema"] == "mast.witec_video/1" and meta["calibration"]["image_width_um"] == 120.0
    assert meta["optical_state"]["objective"]["value"] == "100x" and meta["scope"]["sample_id"] == "s1"
    assert meta["objective"].startswith("Zeiss") and "flakeA" in png.name


# ── analyzer series and project ───────────────────────────────────────────────────────────────────────────────────────────

def test_series_dry_run_checks_and_estimates_without_touching_hardware(signed):
    granted = signed
    res = run(RunAnalyzerSeries, points=73, integration_s=1.0, accumulations=2, dry_run=True)
    assert res.success and res.data["dry_run"] and res.data["plan"]["points"] == 73
    assert "about 365 s" in res.summary
    assert P.ANALYZER_SERIES_START not in granted.fake.triggers()
    bad = run(RunAnalyzerSeries, points=10, integration_s=1.0, start_deg=0.0, stop_deg=90.0, dry_run=True)
    assert not bad.success and "full 0" in bad.error


def test_series_runs_saves_the_project_and_archives_it_with_a_sidecar(signed, scope):
    granted = signed
    optical_state.get_store().set_by_operator("analyzer_optic", "hwp_532", by="op", source="test")
    res = run(RunAnalyzerSeries, points=9, integration_s=0.2, accumulations=2, name="par-crs 532")
    assert res.success, res.error
    d = res.data
    assert d["saved"] is True and d["series"]["points"] == 9 and d["series"]["status"] == "Finished"
    wip, side = Path(d["project_path"]), Path(d["paths"][1])
    assert wip.suffix == ".wip" and wip.exists() and "experiments" in wip.parts and d["stored_in_experiment"]
    meta = json.loads(side.read_text(encoding="utf-8"))
    assert meta["schema"] == "mast.witec_series/1" and meta["request"]["points"] == 9
    assert meta["optical_state"]["analyzer_optic"]["value"] == "hwp_532"           # which λ/2 was in the wheel
    assert meta["scope"]["sample_id"] == "s1" and meta["series"]["accumulations"] == 2
    assert granted.fake.series_runs[-1]["points"] == 9
    assert "par-crs532" in wip.name


def test_series_with_an_explicit_directory_is_not_archived(signed, tmp_path):
    res = run(RunAnalyzerSeries, points=5, integration_s=0.1, name="x", directory=str(tmp_path / "root" / "wips"))
    assert res.success and Path(res.data["project_path"]).parent == tmp_path / "root" / "wips"
    assert "stored_in_experiment" not in res.data and len(res.data["paths"]) == 1


def test_series_without_save_leaves_the_data_in_witec(signed):
    granted = signed
    res = run(RunAnalyzerSeries, points=5, integration_s=0.1, save=False)
    assert res.success and res.data["saved"] is False and "NOT saved" in res.summary
    assert granted.fake.saved_projects == []


def test_series_that_finishes_but_cannot_be_saved_says_do_not_rerun(signed):
    granted = signed
    granted.fake._tree[P.AUTOSAVE_STORE].on_trigger = lambda: None         # noqa: SLF001 - WITec writes no file
    res = run(RunAnalyzerSeries, points=5, integration_s=0.1)
    assert not res.success and res.data["series_finished"] is True
    assert "FINISHED" in res.error and "do NOT rerun" in res.error and "SaveWITecProject" in res.error


def test_series_refused_because_witec_is_busy_does_not_stop_the_other_work(signed):
    granted = signed
    granted.fake._start_seq("Someone's measurement", 1000.0, 0.0)           # noqa: SLF001
    res = run(RunAnalyzerSeries, points=5, integration_s=0.1)
    assert not res.success and "busy" in res.error
    assert P.STOP_SEQUENCER not in granted.fake.triggers()                  # a refusal must not stop the human's run
    assert granted.fake.peek(P.IS_SEQUENCER_ACTIVE) is True


def test_aborting_a_series_stops_the_sequencer(signed):
    granted = signed
    granted.fake.hang_sequencer = True
    n = {"i": 0}

    def abort():
        n["i"] += 1
        return n["i"] > 4

    res = run(RunAnalyzerSeries, Ctx(abort=abort), points=5, integration_s=0.1)
    assert not res.success and "aborted" in res.error
    assert P.STOP_SEQUENCER in granted.fake.triggers()


def test_save_project_archives_or_saves_where_asked(granted, scope, tmp_path):
    res = run(SaveWITecProject, name="map 1")
    assert res.success and res.data["stored_in_experiment"] and Path(res.data["project_path"]).exists()
    explicit = run(SaveWITecProject, name="b", directory=str(tmp_path / "root" / "out"))
    assert explicit.success and Path(explicit.data["project_path"]) == tmp_path / "root" / "out" / "b.wip"
    dup = run(SaveWITecProject, name="b", directory=str(tmp_path / "root" / "out"))
    assert not dup.success and "already exists" in dup.error
    cleared = run(SaveWITecProject, name="c", directory=str(tmp_path / "root" / "out"), clear_after_store=True)
    assert cleared.success


# ── stop ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_stop_closes_the_shutter_and_stops_everything(granted):
    granted.mic.select_laser("532")
    sign_laser_area_safe()
    run(SetWITecShutter, open=True)
    granted.fake.hang_sequencer = True
    granted.fake._start_seq("Run", 1000.0, 0.0)                              # noqa: SLF001
    res = run(StopWITec)
    assert res.success, res.error
    assert res.data["reports"]["witec"]["shutter"] == "closed" and res.data["stopped"] == ["witec"]
    assert granted.fake.peek(P.LASER_SHUTTER) is False and granted.fake.peek(P.IS_SEQUENCER_ACTIVE) is False
    assert "microscope_status" in res.data


def test_stop_without_write_access_reports_what_witec_refused_instead_of_pretending(station):
    res = run(StopWITec)
    assert not res.success and "witec" in res.data["problems"]
    assert "shutter" in res.data["problems"]["witec"] and "COM Automation" in res.data["problems"]["witec"]
    assert "stop / close the shutter by hand" in res.error or "手动停止" in res.error


def test_stop_never_raises_when_witec_is_closed_or_nothing_is_configured(station, tmp_path):
    station.fake.witec_exit()
    res = run(StopWITec)
    assert not res.success and res.data["problems"]["witec"]
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"devices": []}), encoding="utf-8")
    reset_instrument_registry(InstrumentRegistry(p))
    ok = run(StopWITec)
    assert ok.success and ok.data["stopped"] == []


def test_stop_names_an_unknown_device(station):
    res = run(StopWITec, device_id="nope")
    assert not res.success and "nope" in res.data["problems"]


# ── the existing spectrometer skills on the same device ───────────────────────────────────────────────────────────────────

def test_acquire_spectrum_works_on_a_witec_com_device(granted, scope):
    granted.mic.select_laser("532")
    sign_laser_area_safe()
    assert run(SetWITecShutter, open=True).success
    with isolated_live_hub() as hub, LiveRecorder(hub) as rec:
        res = AcquireSpectrum().execute(Ctx(), {"device_id": "witec", "exposure_ms": 200.0, "frames": 2,
                                                "tag": "si", "sample_current": False})
    assert res.success, res.error
    d = res.data
    npy, side = Path(d["path"]), Path(d["sidecar_path"])
    assert d["stored_in_experiment"] and "experiments" in npy.parts and npy.is_file() and side.is_file()
    arr = np.load(npy)
    assert arr.shape == (3, 1340) and np.all(np.isfinite(arr[0]))                  # axis row + 2 accumulations
    meta = json.loads(side.read_text(encoding="utf-8"))
    assert meta["device"]["type"] == "witec_com" and meta["device"]["kind"] == "microscope"
    assert meta["settings"]["laser_line_nm"] == pytest.approx(532.138)
    acq = meta["acquisition"]
    assert acq["driver"] == "witec_com" and acq["excitation_nm"] == pytest.approx(532.138)
    assert acq["frames_returned"] == 2 and acq["warnings"] == []
    assert acq["settings_before"]["shutter_open"] is True and acq["settings_before"]["grating"].startswith("G1")
    assert meta["result"]["peak"]["raman_shift_cm1"] == pytest.approx(520.7, abs=5.0)    # the Si line
    raw = meta["files"]["satellite_files"][0]
    assert Path(raw["path"]).name.endswith(".truematch.txt") and Path(raw["path"]).exists()
    frames = rec.frames("local/spectra/witec")
    assert len(frames) == 3 and frames[-1]["meta"]["final"] is True


def test_the_spectrometer_skills_find_the_witec_without_a_device_id(granted, scope):
    """resolve_device falls back from "spectrometer" to the only microscope """
    st = GetSpectrometerStatus().execute(Ctx(), {})
    assert st.success and st.data["status"]["witec"] is True
    with use_lang("en"):
        res = AcquireSpectrum().execute(Ctx(), {"exposure_ms": 100.0, "sample_current": False})
    # found the WITec, and the driver's emission gate (nobody has signed laser_area_safe) is what stopped it
    assert not res.success and "laser_area_safe" in res.error and granted.fake.silent_requests == []
    sign_laser_area_safe()
    granted.mic.select_laser("532")
    ok = AcquireSpectrum().execute(Ctx(), {"exposure_ms": 100.0, "sample_current": False})
    assert ok.success, ok.error


def test_acquire_spectrum_dry_run_and_refusals_on_witec(granted, scope):
    ctx = Ctx()
    dry = AcquireSpectrum().execute(ctx, {"device_id": "witec", "exposure_ms": 100.0, "dry_run": True})
    assert dry.success and dry.data["dry_run"] and granted.fake.silent_requests == []
    res = AcquireSpectrum().execute(ctx, {"device_id": "witec", "exposure_ms": 5.0})
    assert not res.success and "spectrometer limit" in res.error and granted.fake.silent_requests == []
    granted.fake._start_seq("Someone's measurement", 1000.0, 0.0)                   # noqa: SLF001
    busy = AcquireSpectrum().execute(ctx, {"device_id": "witec", "exposure_ms": 100.0})
    assert not busy.success and granted.fake.silent_requests == []


def test_acquire_spectrum_without_write_access_and_with_witec_closed(station, scope):
    res = AcquireSpectrum().execute(Ctx(), {"device_id": "witec", "exposure_ms": 100.0, "sample_current": False})
    assert not res.success and "COM Automation" in res.error and station.fake.silent_requests == []
    station.fake.witec_exit()
    res = AcquireSpectrum().execute(Ctx(), {"device_id": "witec", "exposure_ms": 100.0, "sample_current": False})
    assert not res.success and "unavailable" in res.error and "WITec Control" in res.error


def test_the_spectrometer_settings_skills_follow_witecs_abilities(granted):
    st = GetSpectrometerStatus().execute(Ctx(), {"device_id": "witec"})
    assert st.success and st.data["status"]["witec"] is True and st.data["limits"]["max_total_s"] == 7200
    assert ConfigureSpectrometer().execute(Ctx(), {"device_id": "witec", "exposure_ms": 80.0, "frames": 3}).success
    bad = ConfigureSpectrometer().execute(Ctx(), {"device_id": "witec", "adc_mhz": 1.0})
    assert not bad.success and "only exposure_ms and frames" in bad.error
    centre = SetSpectrometerCenter().execute(Ctx(), {"device_id": "witec", "center_nm": 560.0})
    assert centre.success and granted.fake.peek(P.spectrograph_path(1, "CenterWavelength")) == 560.0
    grating = SetSpectrometerGrating().execute(Ctx(), {"device_id": "witec", "grating": "G3"})
    assert grating.success and granted.fake.peek(P.spectrograph_path(1, "Grating")) == 2
    out_of_range = SetSpectrometerCenter().execute(Ctx(), {"device_id": "witec", "center_nm": 2000.0})
    assert not out_of_range.success and "spectrometer limit" in out_of_range.error


def test_stop_spectrometer_reaches_the_witec_sequencer(granted):
    granted.fake.hang_sequencer = True
    granted.fake._start_seq("Run", 1000.0, 0.0)                                      # noqa: SLF001
    res = StopSpectrometer().execute(Ctx(), {"device_id": "witec"})
    assert res.success and P.STOP_SEQUENCER in granted.fake.triggers()
    assert granted.fake.peek(P.IS_SEQUENCER_ACTIVE) is False


def test_no_witec_skill_claims_to_belong_to_a_hardware_module():
    """"optional-hardware" means "owned by a hardware-module switch". The WITec skills belong to no module: an STM station
    withholds them by MODULE (skills/station_face.py), the optical station shows them. Tagging them would make
    test_every_optional_hardware_skill_is_owned red - and promise a switch that does not exist."""
    from mast.skills import hardware_modules as hm
    from mast.skills.station_face import STATION_ONLY_MODULES

    assert "mast.skills.builtins.optics_witec" in STATION_ONLY_MODULES
    for name, cls in _module_skills().items():
        assert "optional-hardware" not in (cls().metadata().tags or []), name
        assert name not in hm.SKILL_OWNER, name


def test_the_fakes_presets_are_the_nine_the_survey_names():
    assert set(wm.DEFAULT_STATES) == set(DEFAULT_PRESETS) and len(DEFAULT_PRESETS) == 9


def test_without_the_attestation_the_laser_paths_and_series_are_refused_with_the_reason(granted):
    n = len(granted.fake.writes)
    res = run(RunAnalyzerSeries, points=5, integration_s=0.1)
    assert not res.success and res.data["refused_by"] == "laser_attestation", res.error
    assert "laser_area_safe" in res.error
    assert len(granted.fake.writes) == n
    assert run(SetBeamPath, path="video").success                # the video path is not a laser path
    granted.fake.human_sets(P.LASER_SHUTTER, True)               # a shutter that may be open: the laser paths are WITec light
    res = run(SetBeamPath, path="raman")
    assert not res.success and res.data["refused_by"] == "laser_attestation", res.error
    granted.fake.human_sets(P.LASER_SHUTTER, False)
    assert run(SetBeamPath, path="raman").success                # shutter confirmed closed: an external source's path


def test_a_spectrum_that_would_switch_to_the_raman_path_needs_the_attestation(granted, scope):
    res = AcquireSpectrum().execute(Ctx(), {"device_id": "witec", "exposure_ms": 100.0, "sample_current": False})
    assert not res.success and "laser_area_safe" in res.error and granted.fake.silent_requests == []


def _rebuilt(station, **entry_changes):
    station.path.write_text(json.dumps({"devices": [dict(ENTRY, **entry_changes)]}), encoding="utf-8")
    station.reg.reload()
    mic = station.reg.microscope("witec")
    assert mic.request_write_access()
    return mic


def test_the_device_entry_must_allow_emission_before_any_skill_can_start_it(granted, station):
    _rebuilt(station, allow_emission_control=False)
    sign_laser_area_safe()                                              # a valid signature is not enough
    for skill, params in ((SetWITecShutter, {"open": True}),
                          (RunAnalyzerSeries, {"points": 5, "integration_s": 0.1})):
        with use_lang("en"):
            res = run(skill, **params)
        assert not res.success and res.data["refused_by"] == "emission_control_disabled", (skill, res.error)
        assert "allow_emission_control" in res.error
    assert station.fake.peek(P.LASER_SHUTTER) is False
    station.fake.human_sets(P.LASER_SHUTTER, True)                      # open (a person): the laser paths are WITec light
    with use_lang("en"):
        beam = run(SetBeamPath, path="raman")
    assert not beam.success and beam.data["refused_by"] == "emission_control_disabled", beam.error
    assert run(CloseWITecShutter).success and run(SetBeamPath, path="video").success
    assert run(SetBeamPath, path="raman").success                       # closed again: no WITec light, no gate


def test_the_status_says_why_emission_is_or_is_not_possible(granted, station):
    em = run(GetWITecStatus).data["microscope_status"]["emission"]
    assert em["allow_emission_control"] is True and em["attestation"] == "missing"
    assert em["software_may_start_emission"] is False
    sign_laser_area_safe()
    assert run(GetWITecStatus).data["microscope_status"]["emission"]["software_may_start_emission"] is True


def test_the_shutter_skill_refuses_above_the_cap_with_the_way_out(signed):
    signed.mic.select_laser("532")
    signed.fake.human_sets(P.LASER_POWER, 40.0)
    res = run(SetWITecShutter, open=True)
    assert not res.success and "SetWITecLaserPower" in res.error and signed.fake.peek(P.LASER_SHUTTER) is False


# ── the safety skills on the real station fixture ────────────────────────────────

def test_audit_F5b_release_closes_every_shutter_first_so_close_and_stop_still_work_afterwards(signed):
    assert run(SetWITecShutter, open=True).success
    res = run(ReleaseWITecWriteAccess)
    assert res.success and res.data["write_access"] is False
    assert signed.fake.peek(P.LASER_SHUTTER) is False, "the shutter must be closed before write access goes back"
    assert res.data["made_safe"]["shutter"] == "closed"
    assert signed.fake.gui_locked                                          # the person's grant still stands
    signed.fake.human_sets(P.LASER_SHUTTER, True)                          # opened again by somebody
    assert run(CloseWITecShutter).success and signed.fake.peek(P.LASER_SHUTTER) is False
    signed.fake.human_sets(P.LASER_SHUTTER, True)
    stop = run(StopWITec)
    assert stop.success and signed.fake.peek(P.LASER_SHUTTER) is False


def test_audit_F2b_CloseWITecShutter_closes_every_lasers_shutter_and_keeps_the_selection(signed):
    assert run(SetWITecShutter, open=True).success                         # 532 open
    assert run(SelectWITecLaser, laser="633").success                      # an agent swaps lasers
    res = run(CloseWITecShutter)
    assert res.success, res.error
    assert not any(signed.fake._shutter), "532's shutter stays open after 'CloseWITecShutter' succeeded"      # noqa: SLF001
    assert signed.fake._selected == 2                                      # noqa: SLF001
    assert "532 nm" in res.data["shutters"]["closed"] or "633 nm" in res.data["shutters"]["closed"]
    assert res.data["shutters"]["failed"] == {}


def test_audit_F2b_StopWITec_reports_every_laser(signed):
    assert run(SetWITecShutter, open=True).success
    assert run(SelectWITecLaser, laser="633").success
    res = run(StopWITec)
    assert res.success and not any(signed.fake._shutter)                   # noqa: SLF001
    assert res.data["reports"]["witec"]["shutters"]["failed"] == {}


def test_audit_a_close_that_cannot_be_confirmed_says_NOT_closed_and_what_to_do(signed):
    assert run(SetWITecShutter, open=True).success
    signed.fake.operator_revoke()
    with use_lang("en"):
        res = run(CloseWITecShutter)
    assert not res.success and "NOT closed" in res.error and "by hand" in res.error
    assert res.data["physical_action"] and res.data["shutters"]["failed"]
    assert signed.fake.peek(P.LASER_SHUTTER) is True                       # and nobody pretends otherwise


def test_audit_F6_disconnecting_the_device_leaves_no_light_behind(signed):
    from mast.skills.builtins.optics_stage import DisconnectOpticalDevice

    assert run(SetWITecShutter, open=True).success
    res = DisconnectOpticalDevice().execute(None, {"device_id": "witec"})
    assert res.success, res.error
    assert signed.fake.peek(P.LASER_SHUTTER) is False, "no stop was sent to the station device"
    assert run(CloseWITecShutter).success                                  # and the close skill still works afterwards


def test_audit_F7_the_project_skills_refuse_a_directory_outside_the_data_root(granted, scope, tmp_path):
    outside = tmp_path / "outside_data_root" / "deep"
    for bad in (str(outside), r"\evil-host\share\x", "//evil-host/share/x", "relative/dir",
                str(tmp_path / "root" / ".." / "x")):
        res = run(SaveWITecProject, name="p", directory=bad)
        assert not res.success and res.data["refused_by"] == "directory", (bad, res.error)
    assert not outside.exists() and not (tmp_path / "x").exists() and granted.fake.saved_projects == []


def test_audit_F7_a_series_with_a_bad_directory_is_refused_before_it_runs(signed, tmp_path):
    res = run(RunAnalyzerSeries, points=5, integration_s=0.1, name="s", directory=str(tmp_path / "outside"))
    assert not res.success and res.data["refused_by"] == "directory"
    assert signed.fake.series_runs == [] and P.ANALYZER_SERIES_START not in signed.fake.triggers()
    dry = run(RunAnalyzerSeries, points=5, integration_s=0.1, directory=r"\evil-host\share", dry_run=True)
    assert not dry.success and dry.data["refused_by"] == "directory"       # a dry run tells you early too


def test_audit_M4_SetBeamPath_raman_needs_no_witec_emission_gate_when_the_shutter_is_closed(granted, station):
    _rebuilt(station, allow_emission_control=False)                        # nothing may emit, and nothing is signed
    res = run(SetBeamPath, path="raman")
    assert res.success and res.data["state"] == "Raman", res.error
    assert station.fake.peek(P.LASER_SHUTTER) is False


# ── a stop that someone asked for is counted; the verdict reaches the caller ──────────────


def test_audit_D3_the_three_stop_skills_count_as_an_external_stop_with_the_caller_in_the_reason(signed):
    mic = signed.mic
    ctx = SimpleNamespace(owner="ext:alice")
    for skill_cls, name in ((StopWITec, "StopWITec"), (CloseWITecShutter, "CloseWITecShutter"),
                            (ReleaseWITecWriteAccess, "ReleaseWITecWriteAccess")):
        before = mic.external_stop_epoch
        res = run(skill_cls, ctx)
        assert res.success, (name, res.error)
        assert mic.external_stop_epoch == before + 1, f"{name} did not count as an external stop"
        assert mic.external_stop_reason == f"{name} (ext:alice)", mic.external_stop_reason
        signed.mic.request_write_access()                                  # (ReleaseWITecWriteAccess handed it back)


def test_audit_D3_a_caller_that_cannot_be_named_is_still_counted(signed):
    mic = signed.mic
    before = mic.external_stop_epoch
    assert run(StopWITec).success                                          # no context at all
    assert mic.external_stop_epoch == before + 1 and mic.external_stop_reason.startswith("StopWITec (")


def test_audit_D3_the_note_is_made_before_the_stop_acts(signed):
    """A running protocol watches the counter: it must already have moved when the shutter closes."""
    mic = signed.mic
    assert mic.set_shutter(True) is True
    seen = []
    real = mic.stop_all

    def spy():
        seen.append(mic.external_stop_epoch)
        real()

    mic.stop_all = spy
    before = mic.external_stop_epoch
    run(StopWITec)
    assert seen == [before + 1]


def test_audit_D3_the_protocols_own_closes_and_the_drivers_sweeps_do_not_count(signed):
    mic = signed.mic
    before = mic.external_stop_epoch
    assert mic.set_shutter(True) is True
    assert run(SetWITecShutter, open=False).success                        # the protocol's own close
    mic.set_shutter(False)
    mic.close_all_shutters()                                               # driver-internal sweeps
    mic.stop_all()
    mic.release_write_access()
    assert mic.external_stop_epoch == before, "a protocol's own safe-direction calls must not stop the protocol"


def test_audit_D3_a_refused_or_failed_stop_skill_is_counted_as_well(signed):
    """The stop was asked for whether or not it could be carried out."""
    mic, fake = signed.mic, signed.fake
    fake.operator_revoke()                                                 # the shutter cannot be closed any more
    before = mic.external_stop_epoch
    res = run(CloseWITecShutter)
    assert mic.external_stop_epoch == before + 1
    assert not res.success, "(the close could not be confirmed - and was counted all the same)"


def test_audit_N6_StopWITec_carries_the_machine_readable_verdict(signed):
    mic = signed.mic
    assert mic.set_shutter(True) is True
    res = run(StopWITec)
    assert res.success
    assert res.data["reports"]["witec"]["shutter_confirmed"] is True
    signed.fake.operator_revoke()
    signed.fake.human_sets(P.LASER_SHUTTER, True)
    res = run(StopWITec)
    assert not res.success and res.data["reports"]["witec"]["shutter_confirmed"] is False


def test_audit_N8_a_lost_laser_selection_is_told_to_the_caller(signed):
    """The light is off, so the stop succeeded - but the caller has to be told that nothing may emit until a laser is
    chosen again (SelectWITecLaser)."""
    mic, fake = signed.mic, signed.fake
    with use_lang("en"):
        real = fake._get_raw
        blind = {"on": False}

        def get_raw(info):
            if blind["on"] and info.path == P.LASER_WAVELENGTH:
                raise BucsNoValue("no value (test)")
            return real(info)

        fake._get_raw = get_raw
        mic._last_selected_index = None                                        # noqa: SLF001 - nothing identified yet
        blind["on"] = True
        res = run(StopWITec)
        blind["on"] = False
        assert res.success, res.error
        assert "explicitly" in res.summary and res.data["notes"]["witec"] == mic.last_stop_report["shutters"]["note"]
        refused = run(SetWITecShutter, open=True)
        assert not refused.success and "choose a laser explicitly" in refused.error
        assert run(SelectWITecLaser, laser="633").success
        assert run(SetWITecShutter, open=True).success

        blind["on"] = True
        mic._last_selected_index = None                                        # noqa: SLF001
        closed = run(CloseWITecShutter)
        blind["on"] = False
        assert closed.success and "explicitly" in closed.summary and closed.data["note"]
