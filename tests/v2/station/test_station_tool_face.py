"""The fourth tool-face gate: on an optical station only optical and generic skills show.

``mast.skills.station_face`` is a whitelist over the *real* registry (about 650 skills), so
most of these tests run against it. The classification rules are also checked on small
registries built from real classes, where the expected answer is obvious.
"""
from __future__ import annotations

import inspect
import os
import sys
import time

import pytest

from mast.core.registry import SkillRegistry
from mast.skills import station_face, tool_face
from mast.skills.composite.interpreter import make_spec_skill
from mast.skills.composite.spec import CompositeSpec


@pytest.fixture(scope="module")
def full_registry(tmp_path_factory):
    """Every skill the service registers: builtins + composites + paper + the agent-tool
    bridges + the declarative composites. ~15 s, so once per module."""
    root = tmp_path_factory.mktemp("station_registry_root")
    mp = pytest.MonkeyPatch()
    mp.setenv("MAST2_PROJECT_ROOT", str(root))
    try:
        reg = SkillRegistry()
        reg.discover()
        from mast.skills.composite.loader import load_spec_skills
        from mast.skills.composite.tool_skills import register_workflow_tool_skills

        register_workflow_tool_skills(reg)
        load_spec_skills(reg)
        yield reg
    finally:
        mp.undo()


@pytest.fixture()
def optical(set_profile, unbound_tool_face, full_registry):
    set_profile("optical")
    station_face.bind_registry(full_registry)
    return full_registry


def _hidden(reg):
    return station_face.hidden_names(reg)


def _skill_class(name, module):
    """A do-nothing skill that claims to live in ``module`` (the gate judges by module)."""
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


#: What the witec / sources / protocols / operator owners register (design §6.3). Stand-ins:
#: those modules may register nothing yet, and a test of the rule must not
#: wait for them.
FAMILY = {
    "GetWITecStatus": "mast.skills.builtins.optics_witec",
    "GetLaserStatus": "mast.skills.builtins.optics_sources",
    "SHGExcitationScan": "mast.skills.builtins.optics_protocols",
    "AskOperator": "mast.skills.builtins.operator_skills",
    "ReadOpticalState": "mast.skills.builtins.operator_skills",
}

#: The optical benches STM machines have always had. A new ``optics_*`` module is on neither
#: list by default: it is either withheld on STM (``STATION_ONLY_MODULES``) or, if STM machines
#: are meant to have it, goes here - a decision, not a default.
KNOWN_BENCH_MODULES = frozenset(f"mast.skills.builtins.{m}" for m in (
    "optics_alignment", "optics_aotf_pmt", "optics_camera", "optics_motor_groups",
    "optics_polarization", "optics_pump_probe", "optics_satellite_manual", "optics_scan",
    "optics_spectro", "optics_stage"))


#: The decision, written out where a change to it must be seen: the four modules whose skills
#: exist only because the optical station exists. An STM station does not show them (design D1).
#: The tests below read this list, not the gate's own, so editing ``STATION_ONLY_MODULES``
#: without editing this fails.
OPTICAL_STATION_MODULES = (
    "mast.skills.builtins.optics_witec",
    "mast.skills.builtins.optics_sources",
    "mast.skills.builtins.optics_protocols",
    "mast.skills.builtins.operator_skills",
)


@pytest.fixture()
def family_registry(unbound_tool_face):
    """The optical station's own skills next to a bench skill STM machines already had, an STM
    skill and a generic one; bound as the live registry."""
    reg = SkillRegistry()
    for name, module in FAMILY.items():
        reg.register(_skill_class(name, module))
    reg.register(_skill_class("AcquireSpectrum", "mast.skills.builtins.optics_spectro"))
    reg.register(_skill_class("ListOpticalDevices", "mast.skills.builtins.optics_stage"))
    reg.register(_skill_class("GetBias", "mast.skills.builtins.bias"))
    reg.register(_skill_class("GetTemperature", "mast.skills.builtins.temperature"))
    station_face.bind_registry(reg)
    return reg


# ── an STM station: its tool face is what it was ────────────────────────────────
def _module_skills(reg, module_name):
    """Names of the registered skills whose class ``module_name`` itself defines. Read off the
    module's namespace, not off ``cls.__module__`` over the registry, so it is not the gate's own
    rule run a second time. A class that lost its name to another skill (a name collision) is not
    reachable and is not counted: the duplicate-name guard owns that failure, and the gate judges
    by the class, not by the name."""
    import importlib

    mod = importlib.import_module(module_name)
    defined = {c for c in vars(mod).values() if isinstance(c, type) and c.__module__ == module_name}
    return {m.name for m in reg.list_skills() if reg.get(m.name) in defined}


def _own_skills(reg):
    return set().union(*(_module_skills(reg, m) for m in OPTICAL_STATION_MODULES))


def test_an_stm_station_withholds_exactly_the_optical_stations_own_skills(full_registry, unbound_tool_face):
    """The four modules the optical station adds (WITec, light sources / temperature, protocols,
    operator prompts) register their skills; on an STM station the fourth gate withholds all of
    them and not one other skill - the hard constraint that an STM machine's tool face is what it
    was (design D1)."""
    per_module = {m: _module_skills(full_registry, m) for m in OPTICAL_STATION_MODULES}
    empty = [m for m, names in per_module.items() if not names]
    assert not empty, f"these modules register no skill (renamed? emptied?): {empty}"
    own = set().union(*per_module.values())
    assert station_face.hidden_names(full_registry) == own
    assert station_face.hidden_names() == frozenset(), "nothing bound: nothing to classify on an STM station"
    face = tool_face.compute(registry=full_registry)
    assert face.station == own and own <= face.hardware and face.unreadable == ()
    assert face.names == face.hardware | face.advanced | face.unsubscribed
    # not one more: everything else the fourth gate was asked about stays out of FaceSkip.station
    every = {m.name for m in full_registry.list_skills()}
    assert (every - own) & face.station == set()


def test_the_optical_station_shows_every_one_of_them(optical):
    """On an optical station the same skills are on the whitelist, as before."""
    own = _own_skills(optical)
    assert own, "the optical station's modules register no skill"
    assert own <= station_face.visible_names(optical)
    assert not (own & station_face.hidden_names(optical))
    assert not (own & tool_face.compute().station)


def test_the_gate_withholds_the_four_modules_that_were_decided(unbound_tool_face):
    assert tuple(station_face.STATION_ONLY_MODULES) == OPTICAL_STATION_MODULES


def test_the_stm_description_is_unchanged_by_the_fourth_gate(unbound_tool_face):
    d = tool_face.compute().describe()
    assert "站点画像" not in d and "station profile" not in d


def test_an_stm_station_withholds_the_optical_stations_own_skills_and_nothing_else(family_registry):
    hidden = station_face.hidden_names(family_registry)
    assert hidden == set(FAMILY)
    face = tool_face.compute(registry=family_registry)
    assert face.station == hidden
    assert hidden <= face.hardware, "the external door reads hardware | advanced"
    # the bench STM machines always had is the hardware module's business, as before; the STM
    # skill and the generic one are not this gate's
    assert not ({"AcquireSpectrum", "ListOpticalDevices", "GetBias", "GetTemperature"} & face.station)
    assert "站点画像" in face.describe() or "station profile" in face.describe()


def test_the_stm_gate_goes_by_module_so_a_skill_added_tomorrow_is_withheld_too(family_registry):
    family_registry.register(_skill_class("PowerSeries", "mast.skills.builtins.optics_protocols"))
    family_registry.register(_skill_class("StopWITec", "mast.skills.builtins.optics_witec"))
    assert {"PowerSeries", "StopWITec"} <= station_face.hidden_names(family_registry)


def test_the_optical_station_shows_its_own_skills(family_registry, set_profile):
    set_profile("optical")
    assert set(FAMILY) <= station_face.visible_names(family_registry)
    assert station_face.hidden_names(family_registry) == {"GetBias"}


def test_the_tuple_is_the_knob(family_registry, monkeypatch):
    """Delete an entry and STM stations have that family: the one-line way to change the
    decision, as the docs say."""
    monkeypatch.setattr(station_face, "STATION_ONLY_MODULES", tuple(
        m for m in station_face.STATION_ONLY_MODULES if not m.endswith("operator_skills")))
    hidden = station_face.hidden_names(family_registry)
    assert {"AskOperator", "ReadOpticalState"}.isdisjoint(hidden)
    assert {"GetWITecStatus", "GetLaserStatus", "SHGExcitationScan"} <= hidden


def test_an_overlay_that_displaced_one_of_them_is_withheld_on_stm_too(unbound_tool_face):
    from mast.skills.overlay.provenance import SkillProvenance

    reg = SkillRegistry()
    reg.register(_skill_class("AskOperator", "mast.skills._overlay.a"), provenance=SkillProvenance(
        name="AskOperator", origin="overlay", displaced_module="mast.skills.builtins.operator_skills"))
    reg.register(_skill_class("AcquireSpectrum", "mast.skills._overlay.b"), provenance=SkillProvenance(
        name="AcquireSpectrum", origin="overlay", displaced_module="mast.skills.builtins.optics_spectro"))
    reg.register(_skill_class("NewOverlay", "mast.skills._overlay.c"),
                 provenance=SkillProvenance(name="NewOverlay", origin="overlay"))
    assert station_face.hidden_names(reg) == {"AskOperator"}


def test_every_optics_module_is_a_known_bench_or_listed_as_station_only(full_registry):
    """The decision a new optical module forces: if it is neither, it would reach STM machines
    by default (the hardware-module gate lists skill names and cannot know it)."""
    import importlib

    own = set(station_face.STATION_ONLY_MODULES)
    for module in own:
        importlib.import_module(module)        # a renamed module would silently stop matching
    assert not (own & KNOWN_BENCH_MODULES)
    modules = {full_registry.get(m.name).__module__ for m in full_registry.list_skills()}
    candidates = {m for m in modules if m.startswith("mast.skills.builtins.optics_")
                  or m == "mast.skills.builtins.operator_skills"}
    unknown = candidates - KNOWN_BENCH_MODULES - own
    assert not unknown, (
        f"new optical modules with skills: {sorted(unknown)}. Add each to "
        "station_face.STATION_ONLY_MODULES (withheld on STM stations) or to KNOWN_BENCH_MODULES "
        "in this test (STM stations are meant to have it).")
    assert KNOWN_BENCH_MODULES <= candidates, "a bench module lost all its skills - update the list"


# ── what an optical station shows and withholds ─────────────────────────────────
@pytest.mark.parametrize("name", [
    "AcquireSpectrum", "ConfigureSpectrometer", "SetSpectrometerCenter", "SetSpectrometerGrating",
    "GetSpectrometerStatus", "StopSpectrometer", "ListSatellites", "ListOpticalDevices",
    "OpticalStageMove", "OpticalStageGetPos", "StopOpticalStage", "DelayLineMoveTo",
    "SetPolarizerAngle", "GetPolarizerAngle", "CameraSnapshot", "PicomotorJog", "MoveXYZStage",
    "ReadPmt", "SetAotfWavelength",                      # the optical bench, Nanonis-free
    "GetTemperature", "QueryMonitorHistory", "ReadHardwareEvents",     # generic builtins
    "search_papers", "web_search", "draft_section", "produce_review",  # generic agent tools
    "lookup_sample", "query_past_experiments", "record_analysis",
])
def test_an_optical_station_shows_the_optical_and_generic_skills(optical, name):
    assert optical.has(name), f"{name} is not registered any more - update the test"
    assert name not in _hidden(optical)


@pytest.mark.parametrize("name", [
    # the STM
    "GetBias", "SetBias", "ScanAt", "FullScan", "GridSTS", "WithdrawTip", "SafeRetract",
    "TipPulse", "ForgeAuTip", "ConditionTip", "RelocateCoarseXY", "GetChamberPressure",
    # STM image / spectrum analysis
    "AnalyseAtomicLattice", "AssessHerringbone", "FindSpectralPeaks", "AssessSpectrum",
    # the optical skills that need Nanonis
    "AcquireSignalPoint", "OpticalStageScan", "PumpProbeScan", "NanonisMotorGroupMove",
    "ListNanonisMotorGroups", "TERSPointSpectrum", "TERSGridMap", "LaserFocusMap",
    "PumpProbeSpectral",
    # agent tools that read scans
    "load_scan", "plot_scan", "fit_sts_peaks", "analyze_scan_image",
])
def test_an_optical_station_withholds_the_stm_skills(optical, name):
    assert optical.has(name), f"{name} is not registered any more - update the test"
    assert name in _hidden(optical)


def test_the_declarative_composites_of_the_stm_are_withheld(optical):
    """``config/composite_skills`` ships STM recipes only (scan, condition the tip ...)."""
    specs = [m.name for m in optical.list_skills() if m.name in
             {"ConditionTipUntilSharp", "GridSTSWithPrecheck", "LineProfileSTS",
              "ScanAssessRescan", "ScanThenConditionTip"}]
    assert specs, "the shipped declarative composites are gone - update the test"
    assert set(specs) <= _hidden(optical)


def test_the_split_is_a_real_split(optical):
    every = {m.name for m in optical.list_skills()}
    shown = station_face.visible_names(optical)
    assert shown <= every
    assert len(every) > 500 and len(shown) < 0.4 * len(every), (len(shown), len(every))
    assert len(_hidden(optical)) + len(shown) == len(every)


def test_what_is_shown_never_talks_to_nanonis(optical):
    """No shown skill's source calls ``safe_call``: the structural reason the whitelist is
    safe. (Declarative composites have no source; their steps are checked by the rule.)"""
    offenders = []
    for name in station_face.visible_names(optical):
        cls = optical.get(name)
        try:
            src = inspect.getsource(cls)
        except (OSError, TypeError):
            continue
        if "safe_call(" in src or "urgent_call(" in src:
            offenders.append(name)
    assert offenders == [], f"shown on an optical station but they call Nanonis: {offenders}"


# ── the module list is derived from the code, not trusted ───────────────────────
def _optics_modules(reg):
    return sorted({cls.__module__ for cls in (reg.get(m.name) for m in reg.list_skills())
                   if cls.__module__.startswith("mast.skills.builtins.optics_")})


_NANONIS_MARKERS = ("safe_call(", "NanonisCallRecord", "Signals_ValGet", "Current_Get")


def test_nanonis_coupled_modules_are_exactly_the_optics_modules_that_use_nanonis(full_registry):
    coupled = set()
    for module in _optics_modules(full_registry):
        src = inspect.getsource(sys.modules[module])
        if any(m in src for m in _NANONIS_MARKERS):
            coupled.add(module)
    # optics_common / optics_spectro mention Current_Get for the optional tunnelling-current
    # sampling that degrades without an STM; they must not be what hides AcquireSpectrum
    sampled_only = {"mast.skills.builtins.optics_spectro"}
    assert station_face.NANONIS_COUPLED_MODULES <= coupled, (
        "a module listed as Nanonis-coupled no longer is - it is hidden for nothing")
    unlisted = coupled - station_face.NANONIS_COUPLED_MODULES - sampled_only
    assert not unlisted, (
        f"optical modules that use Nanonis but would be shown on an optical station: {sorted(unlisted)}. "
        "Add them to station_face.NANONIS_COUPLED_MODULES.")


def test_the_generic_lists_name_skills_that_exist(full_registry):
    names = {m.name for m in full_registry.list_skills()}
    assert station_face.GENERIC_SKILLS <= names
    assert station_face.GENERIC_DATA_TOOLS <= names
    from mast.api.ext import agent_tools

    assert station_face.STM_ONLY_AGENT_TOOLS <= set(agent_tools.OFFERED)


def test_the_new_station_modules_are_shown_by_module_not_by_name(full_registry):
    """Whatever the witec / sources / protocols / operator modules register is shown: the rule
    is the module prefix, so a skill added to them tomorrow needs no list edit."""
    for mod in ("optics_witec", "optics_sources", "optics_protocols", "operator_skills"):
        full = f"mast.skills.builtins.{mod}"
        assert station_face._is_optical_module(full) is True


# ── classification rules on small registries ────────────────────────────────────
def _spec(name, *skills):
    nodes = [{"type": "step", "id": f"s{i}", "skill": s, "params": {}} for i, s in enumerate(skills)]
    return make_spec_skill(CompositeSpec.from_dict(
        {"name": name, "description": name, "version": 1, "safety_level": "confirm",
         "params": [], "nodes": nodes}))


@pytest.fixture()
def small(optical):
    reg = SkillRegistry()
    for n in ("AcquireSpectrum", "ListOpticalDevices", "GetBias", "AcquireSignalPoint"):
        reg.register(optical.get(n))
    reg.register(_spec("OptOnly", "ListOpticalDevices", "AcquireSpectrum"))
    reg.register(_spec("OptThenBias", "AcquireSpectrum", "GetBias"))
    reg.register(_spec("Nested", "OptOnly"))
    reg.register(_spec("NestedBad", "OptOnly", "OptThenBias"))
    reg.register(_spec("Unknown", "NoSuchSkill"))
    reg.register(_spec("NoSteps"))
    station_face.bind_registry(reg)
    return reg


def test_a_declarative_composite_is_shown_iff_every_step_is_shown(small):
    hidden = station_face.hidden_names(small)
    assert {"OptOnly", "Nested"}.isdisjoint(hidden)
    assert {"OptThenBias", "NestedBad", "Unknown", "NoSteps", "GetBias",
            "AcquireSignalPoint"} <= hidden
    assert {"AcquireSpectrum", "ListOpticalDevices"}.isdisjoint(hidden)


def test_a_custom_skill_is_shown_and_an_overlay_follows_what_it_displaced(optical):
    from mast.skills.overlay.provenance import SkillProvenance

    make = _skill_class
    reg = SkillRegistry()
    reg.register(make("MyCustom", "mast.skills.custom.mine"))
    reg.register(make("NewOverlay", "mast.skills._overlay.new"),
                 provenance=SkillProvenance(name="NewOverlay", origin="overlay"))
    reg.register(make("OverStm", "mast.skills._overlay.a"), provenance=SkillProvenance(
        name="OverStm", origin="overlay", displaced_module="mast.skills.builtins.bias"))
    reg.register(make("OverOptical", "mast.skills._overlay.b"), provenance=SkillProvenance(
        name="OverOptical", origin="overlay",
        displaced_module="mast.skills.builtins.optics_spectro"))
    reg.register(make("Stranger", "some.other.module"))
    hidden = station_face.hidden_names(reg)
    assert hidden == {"OverStm", "Stranger"}


# ── tool_face.compute ───────────────────────────────────────────────────────────
def test_compute_folds_the_station_gate_into_hardware_and_keeps_it_apart(optical):
    face = tool_face.compute(registry=optical)
    assert face.station and face.station <= face.hardware, (
        "the external door reads hardware | advanced: the station gate must be inside it")
    assert face.names >= face.station
    assert "GetBias" in face and "AcquireSpectrum" not in face
    assert "站点画像" in face.describe()


def test_the_optical_profile_switches_the_optical_bench_module_on(optical, set_profile):
    """``AcquireSpectrum`` is owned by the ``ters_optics`` hardware module, which is OFF by
    default. On the optical station it must not take a trip to 设置 → 硬件模块 to see the
    spectrometer: the profile implies the module."""
    from mast.skills import hardware_modules as hm

    assert "ters_optics" in hm.enabled_ids()
    assert "AcquireSpectrum" not in hm.disabled_skill_names()
    states = {m["id"]: m for m in hm.module_states()}
    assert states["ters_optics"]["enabled"] is True, "the settings page must not say 'off'"
    # the operator's own switches are untouched, and an STM station is as it was
    set_profile("stm")
    assert "ters_optics" not in hm.enabled_ids()
    assert "AcquireSpectrum" in hm.disabled_skill_names()
    assert "AcquireSpectrum" in tool_face.compute()


def test_a_write_to_the_hardware_module_setting_cannot_switch_the_implied_module_off(optical):
    from mast.skills import hardware_modules as hm

    before = hm._enabled
    try:
        hm.set_enabled({"osci_hr": True})                      # a form that does not carry ters_optics
        assert "ters_optics" in hm.enabled_ids()
        assert "osci_hr" in hm.enabled_ids()
    finally:
        hm.set_enabled(before)


def test_compute_without_a_registry_uses_the_bound_one(optical):
    assert tool_face.compute().station == station_face.hidden_names(optical)


def test_an_unbound_optical_station_says_the_gate_is_unreadable_and_filters_nothing(
        set_profile, unbound_tool_face):
    set_profile("optical")
    face = tool_face.compute()
    assert face.station == frozenset()
    assert face.unreadable and "站点画像" in face.unreadable[0]
    assert "站点画像" in face.describe() or "station profile" in face.describe()


def test_computing_the_gate_is_cheap_enough_for_every_job_submission(optical):
    t0 = time.perf_counter()
    for _ in range(20):
        tool_face.compute()
    per_call = (time.perf_counter() - t0) / 20
    assert per_call < 0.25, f"{per_call * 1000:.0f} ms per compute(): cache it before this is on the hot path"


def test_tool_face_bind_registry_is_the_public_way_to_bind(unbound_tool_face, full_registry):
    tool_face.bind_registry(full_registry)
    assert station_face.bound_registry() is full_registry
