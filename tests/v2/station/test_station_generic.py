"""The generic station: neither an STM nor a WITec (docs/v2/design/generic_station.md §3).

Its capabilities are ``optical_bench``, ``operator_prompts``, ``laser_safety`` and
``sensor_echo``. Each aspect below is what those capabilities make of it:

* start-up: nothing of Nanonis, the operator prompt service and the laser revocation listener,
  serial auto-detection off;
* the tool face: the optical station's whitelist without the WITec module (and without what
  depends on it);
* the briefing: the generic section table, the station section with its rules, the
  environment section;
* ``observe`` / ``mast_act``: spectra, the devices, the environment readings and the signed
  state; the named actions refused, the token kept;
* operator prompts and the manual state; the e-stop; the texts that name the instrument.
"""
from __future__ import annotations

import json
import threading
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from _invariants import STATION_THREADS, normalize, run_probe
from _spectra import write_spectrum
from mast import station, station_view
from mast.core.registry import SkillRegistry
from mast.core.types import SensorReading
from mast.operator_hitl import optical_state, prompts
from mast.skills import hardware_modules, station_face, tool_face
from mast.skills.composite.interpreter import make_spec_skill
from mast.skills.composite.spec import CompositeSpec

WITEC = "mast.skills.builtins.optics_witec"


@pytest.fixture(autouse=True)
def generic(set_profile):
    set_profile("generic")


@pytest.fixture(autouse=True)
def fresh_operator_backends():
    old_b = prompts.set_backend(prompts.InMemoryPromptBackend())
    old_s = optical_state.set_store(optical_state.OpticalStateStore())
    yield
    prompts.set_backend(old_b)
    optical_state.set_store(old_s)


# ── start-up ────────────────────────────────────────────────────────────────────────────────
def test_a_generic_boot_dials_nothing_and_installs_the_operator_side(tmp_path):
    out = normalize("boot", run_probe("boot", "generic", tmp_path / "root"))
    assert out["describe"]["profile"] == out["describe"]["boot_profile"] == "generic"
    assert out["calls"] == ["install_operator_hitl", "install_revocation_listener"], out["calls"]
    assert out["async_calls"] == [], "no tip-quality warm-up without a tip"
    assert out["ports"] == [0, 0, 0, 0]
    assert (out["pool"], out["state"], out["executor"]) == (
        "ConnectionPool", "InstrumentState", "SkillExecutor")
    assert not set(out["threads"]) & STATION_THREADS
    assert out["hardware_modules_on"] == ["ters_optics"]
    assert out["reconnect"] == 0
    for key in ("absent", "true"):
        on, why = out["autodetect_policy"][key]
        assert on is False and "generic" in why, (key, why)
    assert out["autodetect_policy"]["false"][0] is False


# ── the tool face ───────────────────────────────────────────────────────────────────────────
def _skill_class(name, module):
    from mast.core.types import SafetyLevel, SkillCategory, SkillMetadata, SkillResult
    from mast.skills.base import BaseSkill

    class _S(BaseSkill):
        def metadata(self):
            return SkillMetadata(name=name, version="1.0.0", category=SkillCategory.READ,
                                 safety_level=SafetyLevel.AUTO, description=name)

        def execute(self, context, params):
            return SkillResult(skill_name=name, success=True)
    _S.__module__ = module
    return _S


def _spec(name, *skills):
    nodes = [{"type": "step", "id": f"s{i}", "skill": s, "params": {}} for i, s in enumerate(skills)]
    return make_spec_skill(CompositeSpec.from_dict(
        {"name": name, "description": name, "version": 1, "safety_level": "confirm",
         "params": [], "nodes": nodes}))


@pytest.fixture()
def small_registry(unbound_tool_face):
    reg = SkillRegistry()
    for name, module in {
        "GetWITecStatus": WITEC,
        "GetLaserStatus": "mast.skills.builtins.optics_sources",
        "PowerSeries": "mast.skills.builtins.optics_protocols",
        "AskOperator": "mast.skills.builtins.operator_skills",
        "AcquireSpectrum": "mast.skills.builtins.optics_spectro",
        "ListOpticalDevices": "mast.skills.builtins.optics_stage",
        "PumpProbeScan": "mast.skills.builtins.optics_pump_probe",
        "GetBias": "mast.skills.builtins.bias",
        "GetTemperature": "mast.skills.builtins.temperature",
        "MyProbe": "mast.skills.custom.lab_probe",
    }.items():
        reg.register(_skill_class(name, module))
    reg.register(_spec("BenchOnly", "ListOpticalDevices", "AcquireSpectrum"))
    reg.register(_spec("NeedsWitec", "AcquireSpectrum", "GetWITecStatus"))
    reg.register(_spec("OfComposites", "BenchOnly", "GetLaserStatus"))
    station_face.bind_registry(reg)
    return reg


def test_the_generic_face_is_the_optical_whitelist_without_the_witec(small_registry, set_profile):
    hidden = station_face.hidden_names(small_registry)
    assert hidden == {"GetWITecStatus", "NeedsWitec", "PumpProbeScan", "GetBias"}
    set_profile("optical")
    assert station_face.hidden_names(small_registry) == {"PumpProbeScan", "GetBias"}


def test_a_composite_with_a_witec_step_is_withheld_but_one_of_bench_steps_is_shown(small_registry):
    visible = station_face.visible_names(small_registry)
    assert {"BenchOnly", "OfComposites"} <= visible
    assert "NeedsWitec" not in visible


def test_an_overlay_that_replaced_a_witec_builtin_is_withheld(small_registry, monkeypatch, set_profile):
    small_registry.register(_skill_class("StopWITec", "mast.skills._overlay.stop_witec"))
    monkeypatch.setattr(small_registry, "provenance", lambda name: SimpleNamespace(
        displaced_module=WITEC if name == "StopWITec" else ""), raising=False)
    assert "StopWITec" in station_face.hidden_names(small_registry)
    set_profile("optical")
    assert "StopWITec" not in station_face.hidden_names(small_registry), "the optical station keeps it"


def test_the_capability_table_is_the_decision_not_the_profile_name(small_registry, monkeypatch):
    """Give the generic station the WITec capability and its skills come back: the gate asks
    the capability, nothing else."""
    caps = dict(station.PROFILE_CAPABILITIES)
    caps["generic"] = caps["generic"] | {"witec"}
    monkeypatch.setattr(station, "PROFILE_CAPABILITIES", caps)
    assert "GetWITecStatus" not in station_face.hidden_names(small_registry)


@pytest.fixture(scope="module")
def full_registry(tmp_path_factory):
    root = tmp_path_factory.mktemp("generic_registry_root")
    mp = pytest.MonkeyPatch()
    mp.setenv("MAST2_PROJECT_ROOT", str(root))
    try:
        from mast.skills.composite.loader import load_spec_skills
        from mast.skills.composite.tool_skills import register_workflow_tool_skills

        reg = SkillRegistry()
        reg.discover()
        register_workflow_tool_skills(reg)
        load_spec_skills(reg)
        yield reg
    finally:
        mp.undo()


def test_on_the_real_registry_generic_is_optical_minus_the_witec_module(full_registry, set_profile,
                                                                        unbound_tool_face):
    import importlib

    station_face.bind_registry(full_registry)
    generic_visible = station_face.visible_names(full_registry)
    set_profile("optical")
    optical_visible = station_face.visible_names(full_registry)
    mod = importlib.import_module(WITEC)
    witec = {m.name for m in full_registry.list_skills()
             if getattr(full_registry.get(m.name), "__module__", "") == mod.__name__}
    assert len(witec) >= 15, f"the WITec module registers {len(witec)} skill(s)?"
    gone = optical_visible - generic_visible
    assert witec <= gone, "every WITec skill is withheld on a generic station"
    extra = gone - witec
    # anything else that went is a composite / overlay that stands on a WITec skill
    for name in extra:
        steps = station_face._spec_steps(full_registry.get(name)) or set()
        assert steps & (witec | extra), f"{name} was withheld on a generic station for no WITec reason"
    assert generic_visible <= optical_visible, "the generic station shows nothing the optical one hides"


def test_the_bench_is_on_by_itself(set_profile):
    assert "ters_optics" in hardware_modules.enabled_ids()
    set_profile("stm")
    assert "ters_optics" not in hardware_modules.enabled_ids() - hardware_modules._detected_implied()


def test_the_stm_agent_tools_are_not_offered():
    from mast.api.ext import agent_tools

    for name in station_face.STM_ONLY_AGENT_TOOLS:
        assert station_face.is_stm_only_agent_tool(name) is True
        assert not agent_tools.is_offered(name)
    assert station_face.is_stm_only_agent_tool("list_experiments") is False


def test_the_tool_face_reports_the_gate(small_registry):
    face = tool_face.compute(registry=small_registry)
    assert face.station == station_face.hidden_names(small_registry)
    assert face.station <= face.hardware


# ── the briefing ────────────────────────────────────────────────────────────────────────────
def _request(monitor=None):
    ctx = SimpleNamespace(environment_monitor=monitor)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=ctx, jobs=None, vista=None)))


class Monitor:
    """An environment monitor with one real thermometer and one placeholder."""

    def __init__(self, readings):
        from mast.environment.lakeshore_temp import LakeshoreTemperatureSensor

        real = LakeshoreTemperatureSensor.__new__(LakeshoreTemperatureSensor)
        self._sensors = {"stage_T": real, "vacuum": object()}
        self._latest = dict(readings)

    def get_latest(self):
        return dict(self._latest)


def test_the_generic_briefing_has_its_own_sections_in_order():
    from mast.api.ext import overview as ov

    table = ov._section_table()
    assert tuple(table) == ov.GENERIC_ORDER == (
        "station", "status", "scope", "resume", "environment", "optical_state",
        "operator_prompts", "safety", "recent_actions", "recent_spectra", "alarms", "notes",
        "recording", "jobs", "operator_requests", "optical_layouts")
    for stm_only in ("tip", "instrument", "live", "prefs", "recent_files"):
        assert stm_only not in table


def test_the_station_section_names_the_generic_station_and_its_rules(tmp_path, monkeypatch):
    from mast.api.ext import overview as ov

    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    text = ov._sec_station(None, None)["text"]
    assert "generic" in text
    for needle in ("mast_run", "config/optical_instruments.json", "config/environment_sensors.json",
                   "mast_ask_operator", "laser_area_safe", "LaserEmissionOff"):
        assert needle in text, needle
    assert "WITec Control" not in text and "COM Automation" not in text
    assert ov._sec_station(None, None)["data"]["profile"] == "generic"


def test_the_environment_section_lists_the_real_sensors_from_the_cache():
    from mast.api.ext import overview as ov

    mon = Monitor({"stage_T": SensorReading(4.2, "K", timestamp=datetime.now().isoformat()),
                   "vacuum": SensorReading(0.0, "mbar", status="unavailable")})
    sec = ov._sec_environment(_request(mon), None)
    assert "stage_T: 4.2 K [ok" in sec["text"]
    assert "vacuum" not in sec["text"], "a placeholder is not a sensor of this station"
    assert set(sec["data"]["sensors"]) == {"stage_T"}


def test_a_station_without_sensors_says_so_and_an_unwired_monitor_degrades():
    from mast.api.ext import overview as ov

    empty = Monitor({})
    empty._sensors = {}
    assert "no environment sensors" in ov._sec_environment(_request(empty), None)["text"] \
        or "没有配置环境传感器" in ov._sec_environment(_request(empty), None)["text"]
    with pytest.raises(RuntimeError):
        ov._sec_environment(_request(None), None)


def test_status_and_safety_have_no_nanonis_rows_and_no_stm_envelope():
    from mast.api.ext import overview as ov

    s, _ = ov._status(_request())
    assert s["station"] == "generic" and "connection" not in s and "live" not in s
    assert "generic" in ov._status_text(s) and "Nanonis connection" not in ov._status_text(s)
    sec = ov._sec_safety(_request(), None)
    assert sec["data"]["limits_source"] == "generic station profile" and sec["data"]["limits"] == {}


# ── observe and mast_act ────────────────────────────────────────────────────────────────────
@pytest.fixture()
def staging(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    from mast.logging import experiment_log as el

    prev = el.get_active_log()
    el.set_active_log(None)
    d = tmp_path / "root" / "artifacts" / "spectra" / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True)
    yield d
    el.set_active_log(prev)


@pytest.fixture()
def bench(tmp_path):
    from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry

    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "ccd", "type": "sim_spectrometer", "time_scale": 0.0},
        {"id": "src", "type": "sim_laser"},
        {"id": "pm", "type": "sim_power_meter"}], "delay_line": None}), encoding="utf-8")
    reg = InstrumentRegistry(config_path=p)
    reset_instrument_registry(reg)
    yield reg
    reset_instrument_registry(None)


@pytest.fixture()
def svc(tmp_path):
    from mast.vista.service import VistaService

    s = VistaService(root=tmp_path / "vista", start_watcher=False)
    yield s
    s.shutdown()


def _observe(svc, session="s1"):
    from mast.vista.service import _Caller

    return svc.observe(_Caller(actor="tester", session=session))


def test_a_generic_observation_has_the_spectrum_the_devices_the_environment_and_the_state(
        svc, staging, bench):
    mon = Monitor({"stage_T": SensorReading(77.0, "K", timestamp=datetime.now().isoformat())})
    svc.bind(SimpleNamespace(environment_monitor=mon), jobs=SimpleNamespace())
    bench.power_meter("pm")
    write_spectrum(staging)
    o = _observe(svc)
    body = json.loads(o["text"])
    assert body["station"] == "generic" and body["latest_frame"]["kind"] == "spectrum"
    inst = body["instrument"]
    assert set(inst["power_meter"]) == {"pm"} and "src" in inst["devices_closed"]
    assert inst["environment"] == {"stage_T": {"value": 77.0, "unit": "K", "status": "ok",
                                               "age_s": inst["environment"]["stage_T"]["age_s"]}}
    assert "optical_state" in inst and inst["operator_prompts_open"] == 0
    assert set(inst["optical_state"]) <= {"signed", "expired", "unset"}
    assert len(o["images"]) == 1 and o["next_token"]


def test_the_optical_echo_is_unchanged_by_the_environment_echo(svc, staging, bench, set_profile):
    set_profile("optical")
    mon = Monitor({"stage_T": SensorReading(77.0, "K")})
    svc.bind(SimpleNamespace(environment_monitor=mon), jobs=SimpleNamespace())
    inst = json.loads(_observe(svc)["text"])["instrument"]
    assert "environment" not in inst and "environment_error" not in inst


def test_an_unwired_monitor_is_reported_not_hidden(svc, staging, bench):
    svc.bind(SimpleNamespace(), jobs=SimpleNamespace())
    inst = json.loads(_observe(svc)["text"])["instrument"]
    assert "environment" not in inst and "not wired" in inst["environment_error"]


def test_named_actions_are_refused_on_a_generic_station_and_the_token_survives(svc):
    from mast.vista.actions import ACTIONS
    from mast.vista.service import VistaError, _Caller

    svc.bind(SimpleNamespace(), jobs=SimpleNamespace())
    caller = _Caller(actor="tester", session="s1")
    tok = _observe(svc)["next_token"]
    for action in sorted(ACTIONS):
        with pytest.raises(VistaError) as ei:
            svc.submit_act(caller, {"action": action, "params": {}, "after": tok})
        assert ei.value.status == 422 and ei.value.code == "not_available_on_this_station"
        assert ei.value.extra == {"station": "generic"}
        assert "a generic station" in ei.value.detail and "mast_run" in ei.value.detail
    assert svc.gate.is_live(tok)
    assert svc.watch_once() == []


# ── operator prompts, the manual state, human nodes ─────────────────────────────────────────
def test_structured_prompts_and_human_nodes_exist_on_a_generic_station():
    from mast.api import direct_exec
    from mast.api.ext import collab
    from mast.api.ext.schemas import RequestBody

    assert collab._structured_prompts_here() is True
    assert direct_exec._waits_for_human_nodes() is True
    body = RequestBody(message="Is the filter in?", kind="confirm", title="Filter")
    assert body.title == "Filter", "the structured fields are kept"


def test_the_generic_station_starts_with_the_laser_attestation_only(tmp_path, monkeypatch):
    from mast.core.runtime import CoreRuntime
    from mast.operator_hitl import service

    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    rt = CoreRuntime.__new__(CoreRuntime)
    rt._station_profile = "generic"
    try:
        rt._install_operator_hitl()
        assert [s.id for s in optical_state.get_store().slots()] == ["laser_area_safe"]
    finally:
        service.uninstall()
    rt._station_profile = "optical"
    try:
        rt._install_operator_hitl()
        ids = [s.id for s in optical_state.get_store().slots()]
        assert ids == [s.id for s in optical_state.DEFAULT_SLOTS], "the WITec bench's, as before"
    finally:
        service.uninstall()


def test_a_lab_can_define_or_extend_its_own_slots(tmp_path):
    from mast.operator_hitl.store import load_slot_definitions

    path = tmp_path / "optical_station.json"
    path.write_text(json.dumps({"extend_defaults": True, "slots": [
        {"id": "filter", "label": "filter", "kind": "text"}]}), encoding="utf-8")
    got = load_slot_definitions(path, defaults=optical_state.GENERIC_DEFAULT_SLOTS)
    assert [s.id for s in got] == ["laser_area_safe", "filter"]
    assert load_slot_definitions(tmp_path / "missing.json",
                                 defaults=optical_state.GENERIC_DEFAULT_SLOTS) == optical_state.GENERIC_DEFAULT_SLOTS
    assert load_slot_definitions(tmp_path / "missing.json") == optical_state.DEFAULT_SLOTS


def test_the_spectrum_sidecar_carries_the_signed_state_and_reads_no_stm():
    from mast.skills.builtins import optics_common, optics_spectro

    assert "optical_state" in optics_spectro._optical_state_fields()
    assert optics_common.no_stm() is True
    snap = optics_common.stm_snapshot(SimpleNamespace(state=None))
    assert snap["available"] is False and "generic" in snap["why"]


# ── e-stop, self-check, lock wording ────────────────────────────────────────────────────────
class RecordingPool:
    def __init__(self):
        self.calls: list = []
        self._config = types.SimpleNamespace(port_emergency=6504)

    def urgent_call(self, verb, *a, **k):
        self.calls.append(verb)
        raise ConnectionError("no connection")

    safe_call = urgent_call


def test_the_estop_stops_the_optics_and_sends_nothing_to_nanonis(monkeypatch):
    import mast.instruments.registry as inst_registry
    import mast.satellite as sat_pkg
    from mast.config import MASTConfig
    from mast.core.runtime import CoreRuntime

    monkeypatch.setattr(sat_pkg, "get_satellite_hub", lambda: SimpleNamespace(
        emergency_stop_all=lambda reason="": {"sent": [], "unreachable": [], "errors": []}))
    monkeypatch.setattr(inst_registry, "get_instrument_registry",
                        lambda: SimpleNamespace(stop_all=lambda: None))
    rt = CoreRuntime.__new__(CoreRuntime)
    rt.config = MASTConfig()
    rt._orch_abort = threading.Event()
    rt._orch_run_aborts_lock = threading.Lock()
    rt._orch_run_aborts = {}
    rt._executor = None
    rt._orch_interrupts = None
    rt._shutdown_hooks = []
    rt._pool = RecordingPool()
    res = rt.emergency_stop("operator")
    assert rt._pool.calls == []
    assert res["nanonis"] == "not used (generic station profile)"
    assert res["optics"]["local_optics"] == "dispatched" and res["errors"] == []


def test_the_self_check_says_there_is_no_nanonis():
    from mast.webui.dashboard import run_system_check

    app = SimpleNamespace(
        _station_profile="generic", _pool=RecordingPool(), _storage=None, _planner=None,
        _monitor=None, _registry=None,
        config=SimpleNamespace(nanonis=SimpleNamespace(), db_path="x",
                               llm=SimpleNamespace(api_key="", provider="p", model="m")))
    rows = run_system_check(app)
    assert rows[0] == {"name": "Station profile", "status": "ok",
                       "detail": "Generic station - Nanonis is not used"}
    assert not any(r["name"].startswith("Nanonis") for r in rows)
    assert app._pool.calls == [], "no version query either"


def test_the_busy_message_names_the_instrument_not_nanonis():
    from mast.core.instrument_lock import InstrumentBusy

    msg = InstrumentBusy({"owner": "someone", "skill": "AcquireSpectrum"}, "PowerSeries", 1.0).message()
    assert "Nanonis" not in msg


def test_the_skill_card_reason_names_the_generic_station():
    from mast.api.ext import skills

    reason = skills._withheld_reason()
    assert "generic" in reason
