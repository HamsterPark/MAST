"""The tool face on the paths that never run ``CoreRuntime.setup`` .

The station gate (the fourth gate of ``mast.skills.tool_face``) classifies skills by their *class*,
so it needs the live skill registry. ``CoreRuntime.setup`` binds it (``tool_face.bind_registry``);
the one-shot CLI (``python -m mast -i ...``) builds its own registry in
``pipeline.main._build_minimal_environment`` and never ran ``setup``, and the callers of
``tool_face.compute`` passed no ``registry=``. So in the CLI process nothing was bound and the gate
saw nothing:

* on an STM station the CLI's instrument-control agent got the optical station's skills (AskOperator,
  LaserEmissionOn, LoadWITecState ...) as tools - the opposite of "an STM machine's tool face is
  what it was";
* on an optical station the gate was unreadable (fail-open, recorded in ``FaceSkip.unreadable``) and
  the STM skills leaked the other way.

The fix is two layers. Every caller that holds a registry hands it to ``compute`` itself (the tool
builder, its fingerprint probe, the ``/agents/tools`` mirror, the external door, the skill card);
and the CLI binds the registry it builds, for any zero-argument caller in the same process.

These tests build the registry exactly as the CLI does and keep it *unbound*, so what they check is
the explicit ``registry=``; one test checks the CLI's own binding.
"""
from __future__ import annotations

import importlib

import pytest

from mast.agents.instrument_control import tools as ic_tools
from mast.skills import station_face, tool_face

#: The four modules whose skills exist only because the optical station exists. Written out here
#: on purpose, so that editing ``station_face.STATION_ONLY_MODULES`` without editing the tests fails.
OPTICAL_STATION_MODULES = (
    "mast.skills.builtins.optics_witec",
    "mast.skills.builtins.optics_sources",
    "mast.skills.builtins.optics_protocols",
    "mast.skills.builtins.operator_skills",
)

#: Skills that exist in the CLI's builtins-only registry and belong to the STM only.
STM_ONLY_SAMPLE = ("GetBias", "SetBias", "StartScan")


def _own_skills(reg) -> set[str]:
    """Names of the registered skills whose class the four optical-station modules define."""
    defined = set()
    for module in OPTICAL_STATION_MODULES:
        mod = importlib.import_module(module)
        defined |= {c for c in vars(mod).values() if isinstance(c, type) and c.__module__ == module}
    return {m.name for m in reg.list_skills() if reg.get(m.name) in defined}


def _names(tools) -> set[str]:
    return {getattr(t, "name", "") for t in tools}


@pytest.fixture(scope="module")
def cli_env(tmp_path_factory):
    """What the one-shot CLI builds: ``(context_provider, registry)``, builtins only. The registry
    comes back *unbound*: the CLI binds it on the way, and these tests are about what happens when
    nothing is."""
    root = tmp_path_factory.mktemp("cli_root")
    mp = pytest.MonkeyPatch()
    mp.setenv("MAST2_PROJECT_ROOT", str(root))
    try:
        from mast.pipeline.main import _build_minimal_environment

        _buf, context_provider, registry = _build_minimal_environment(no_hardware=True, no_buffer=True)
        station_face.bind_registry(None)
        yield context_provider, registry
    finally:
        station_face.bind_registry(None)
        mp.undo()


def _surface(cli_env):
    """The tools the CLI's instrument-control loop is built from (before it narrows what the model
    sees): ``build_instrument_loop`` -> ``_instrument_tool_surface`` -> ``build_tools``."""
    from mast.agentruntime.ic_assembly import _instrument_tool_surface

    context_provider, registry = cli_env
    raw, _visibility = _instrument_tool_surface(None, registry, context_provider)
    return _names(raw)


# ── the CLI binds what it builds ────────────────────────────────────────────────
def test_the_one_shot_cli_binds_the_registry_it_builds(cli_env, unbound_tool_face):
    from mast.pipeline.main import _build_minimal_environment

    assert station_face.bound_registry() is None
    _buf, _ctx, registry = _build_minimal_environment(no_hardware=True, no_buffer=True)
    assert station_face.bound_registry() is registry


# ── the CLI's tool surface, nothing bound ───────────────────────────────────────
def test_an_stm_cli_agent_is_not_handed_the_optical_stations_skills(cli_env, unbound_tool_face, set_profile):
    set_profile("stm")
    assert station_face.bound_registry() is None
    names = _surface(cli_env)
    own = _own_skills(cli_env[1])
    assert len(own) >= 30, f"the optical station's modules register only {sorted(own)}"
    assert not (own & names), f"offered to an STM agent: {sorted(own & names)}"
    assert {"GetBias", "SetBias", "StartScan"} <= names, "the STM's own skills are still there"


def test_an_optical_cli_agent_is_not_handed_the_stm_skills(cli_env, unbound_tool_face, set_profile):
    set_profile("optical")
    assert station_face.bound_registry() is None
    names = _surface(cli_env)
    hidden = station_face.hidden_names(cli_env[1])
    assert len(hidden) > 300 and set(STM_ONLY_SAMPLE) <= hidden
    assert not (hidden & names), f"offered to an optical agent: {sorted(hidden & names)[:12]}"
    assert {"AcquireSpectrum", "GetWITecStatus", "AskOperator"} <= names, "the optical skills are there"


# ── assembly and probe read the same gate ───────────────────────────────────────
@pytest.mark.parametrize("profile", ["stm", "optical"])
def test_the_wrap_fingerprint_probe_sees_the_gate_the_assembly_applied(
        cli_env, unbound_tool_face, set_profile, profile):
    """The probe says "has the tool table caught up with the registry". If only one side classifies
    the registry it holds, the two answer different questions: on a profile where the station gate
    matters, "caught up" would be reported as "not yet" for ever (or the other way round)."""
    from mast.skills.overlay.provenance import fingerprint, registry_triples

    set_profile(profile)
    registry = cli_env[1]
    ic_tools.build_instrument_skill_tools(registry, cli_env[0])
    built = ic_tools.LAST_WRAP["fingerprint"]
    expected, _overlaid = ic_tools.expected_wrap_fingerprint(registry)
    assert built == expected

    # ... and both are the registry minus the union computed with the registry given explicitly
    skip = tool_face.compute(tool_face.names_from_registry(registry), registry=registry).names
    own = _own_skills(registry)
    if profile == "stm":
        assert own <= skip
    else:
        assert set(STM_ONLY_SAMPLE) <= skip
    assert built == fingerprint([t for t in registry_triples(registry) if t[0] not in skip])


# ── the other callers that hold a registry ──────────────────────────────────────
@pytest.mark.parametrize("profile", ["stm", "optical"])
def test_the_agent_tools_mirror_lists_what_the_agent_holds(cli_env, unbound_tool_face, set_profile, profile):
    """``GET /agents/tools`` is a second way to say what the instrument agent holds."""
    from mast.webui import agents_api

    set_profile(profile)
    registry = cli_env[1]
    agents_api.set_live_registry(registry)
    try:
        listed = {t["name"] for t in agents_api._compute_agent_tools()["instrument_control"]}
    finally:
        agents_api.set_live_registry(None)
    own = _own_skills(registry)
    if profile == "stm":
        assert not (own & listed), sorted(own & listed)
        assert "GetBias" in listed
    else:
        hidden = station_face.hidden_names(registry)
        assert not (hidden & listed), sorted(hidden & listed)[:12]
        assert "AcquireSpectrum" in listed


@pytest.mark.parametrize("profile", ["stm", "optical"])
def test_the_external_doors_disabled_list_classifies_the_registry_it_is_given(
        cli_env, unbound_tool_face, set_profile, profile):
    from mast.api.ext import jobs

    set_profile(profile)
    registry = cli_env[1]
    disabled = jobs._disabled_names(registry)
    own = _own_skills(registry)
    if profile == "stm":
        assert own <= disabled
    else:
        assert set(STM_ONLY_SAMPLE) <= disabled and not (own & disabled)


def test_a_zero_argument_call_with_nothing_bound_is_the_documented_fallback(unbound_tool_face, set_profile):
    """What is left unprotected is spelled out in the design (section 6.4): a caller that has no
    registry and finds none bound. On STM the gate then withholds nothing; on an optical station it
    says it could not read itself (``unreadable``) and withholds nothing."""
    set_profile("stm")
    face = tool_face.compute()
    assert face.station == frozenset() and face.unreadable == ()
    set_profile("optical")
    face = tool_face.compute()
    assert face.station == frozenset() and face.unreadable and "站点画像" in face.unreadable[0]


# ── the skill forge: the agent's catalog, its drafts and its composite runner ───
# ``build_tools`` also hands the instrument agent the skill forge. Its catalog, its draft-time lint
# and ``run_composite`` used to read a disabled list of their own (hardware modules + advanced
# capabilities) that does not know the station gate: the tool table was sealed, what the agent could
# *read and run by name* was not.

def _forge(registry, context_provider):
    from mast.agents._shared.skill_forge_tools import make_skill_forge_tools

    return {t.name: t for t in make_skill_forge_tools(registry, context_provider)}


def _in_catalog(tools, name: str) -> bool:
    import json

    rows = json.loads(tools["skill_catalog"].invoke({"query": name}))["skills"]
    return any(r["name"] == name for r in rows)


def test_an_stm_agents_skill_catalog_does_not_list_the_optical_stations_skills(
        cli_env, unbound_tool_face, set_profile):
    set_profile("stm")
    tools = _forge(cli_env[1], cli_env[0])
    own = sorted(_own_skills(cli_env[1]))
    assert len(own) >= 30
    assert [n for n in own if _in_catalog(tools, n)] == []
    assert _in_catalog(tools, "GetBias"), "the STM's own skills are still listed"


def test_an_optical_agents_skill_catalog_does_not_list_the_stm_skills(cli_env, unbound_tool_face, set_profile):
    set_profile("optical")
    tools = _forge(cli_env[1], cli_env[0])
    assert [n for n in STM_ONLY_SAMPLE if _in_catalog(tools, n)] == []
    assert _in_catalog(tools, "GetWITecStatus") and _in_catalog(tools, "AcquireSpectrum")


def _composite_registry(*steps_of: tuple[str, tuple[str, ...]]):
    """A small registry: the real classes of every skill named in a step, plus one declarative
    composite per ``(name, steps)``."""
    from mast.core.registry import SkillRegistry
    from mast.skills.composite.interpreter import make_spec_skill
    from mast.skills.composite.spec import CompositeSpec

    full = SkillRegistry()
    full.discover("mast.skills.builtins")
    small = SkillRegistry()
    for comp_name, steps in steps_of:
        for s in steps:
            if not small.has(s):
                small.register(full.get(s))
        nodes = [{"type": "step", "id": f"s{i}", "skill": s, "params": {}} for i, s in enumerate(steps)]
        small.register(make_spec_skill(CompositeSpec.from_dict(
            {"name": comp_name, "description": comp_name, "version": 1, "safety_level": "confirm",
             "params": [], "nodes": nodes})))
    return small


def test_run_composite_refuses_what_the_station_withholds_and_still_runs_the_rest(
        cli_env, unbound_tool_face, set_profile, monkeypatch):
    from mast.agents._shared import skill_forge_tools as forge

    monkeypatch.setattr(forge, "_dispatch", lambda forged, params, tool_call_id, name: "DISPATCHED")
    reg = _composite_registry(("StmOnlyComposite", ("GetBias", "GetBias")),
                              ("StationOnlyComposite", ("AskOperator", "ReadOpticalState")))
    tool = _forge(reg, lambda: None)["run_composite"]

    def run(name):      # the tool takes an injected tool_call_id: call the function itself
        return tool.func(name=name, params_json="{}", tool_call_id="tc-1")

    set_profile("stm")
    assert run("StmOnlyComposite") == "DISPATCHED"
    refused = run("StationOnlyComposite")
    assert refused != "DISPATCHED" and "AskOperator" in refused and "关闭" in refused, refused

    set_profile("optical")
    assert run("StationOnlyComposite") == "DISPATCHED"
    refused = run("StmOnlyComposite")        # a composite of STM steps is itself off the whitelist
    assert refused != "DISPATCHED" and "StmOnlyComposite" in refused and "关闭" in refused, refused


@pytest.mark.parametrize("profile,skill", [("stm", "AskOperator"), ("optical", "GetBias")])
def test_a_draft_cannot_launder_what_the_station_withholds(
        cli_env, unbound_tool_face, set_profile, profile, skill):
    from mast.agents._shared.skill_forge_tools import agent_track_lint

    set_profile(profile)
    spec = {"name": "Draft", "nodes": [
        {"type": "step", "id": "a", "skill": skill, "params": {}},
        {"type": "step", "id": "b", "skill": "GetBias" if profile == "stm" else "AcquireSpectrum", "params": {}}]}
    problems = agent_track_lint(spec, cli_env[1])
    assert any(skill in p and "关闭" in p for p in problems), problems
