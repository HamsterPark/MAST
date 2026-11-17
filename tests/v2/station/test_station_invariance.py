"""The STM station and the optical station do exactly what they did before the capability table.

Design: ``docs/v2/design/generic_station.md`` §5. Two groups, one per profile that existed before the
capability table, each over the four things a profile decides:

* **start-up** - which Nanonis-bound starts run, which station threads exist, the ports, the
  auto-detection policy, the hardware modules a profile switches on (``golden/boot.json``);
* **briefing** - the section table, the station section, the status text, the safety source
  (``golden/briefing.json``);
* **mast_act** - which named actions are refused, with what, and the shape of an observation
  (``golden/act.json``);
* **tool face** - which skills and agent tools are withheld, classified over the live registry by
  the new code and by a frozen copy of the old decision (``_legacy_face_pre_capabilities.py``).

The golden files are the answers of ``_invariants_probe.py`` on a ``git archive`` export of the old tree;
the same probe runs here against this tree. The tool face has no golden file on purpose: the
registry grows with every new skill, and a list of names would turn red for each of them. The
oracle classifies whatever the registry holds, so it only turns red when the decision changes.
"""
from __future__ import annotations

import pytest

import _legacy_face_pre_capabilities as legacy
from _invariants import PROFILES, load_golden, normalize, run_probe

_CACHE: dict[tuple[str, str], dict] = {}


def _live(part: str, profile: str, tmp_path_factory) -> dict:
    key = (part, profile)
    if key not in _CACHE:
        root = tmp_path_factory.mktemp(f"probe_{part}_{profile}")
        _CACHE[key] = normalize(part, run_probe(part, profile, root))
    return _CACHE[key]


def _diff(golden: dict, live: dict) -> str:
    keys = sorted(set(golden) | set(live))
    bad = [f"  {k}:\n    before:   {golden.get(k)!r}\n    now:      {live.get(k)!r}"
           for k in keys if golden.get(k) != live.get(k)]
    return "\n".join(bad)


def _check(part: str, profile: str, tmp_path_factory) -> None:
    golden = load_golden(part)
    assert golden["source"] == "pre-capability-table"
    live = _live(part, profile, tmp_path_factory)
    assert live == golden[profile], (
        f"the {profile} station's {part} is not what it was before the capability table:\n"
        + _diff(golden[profile], live))


# ── the full registry, for the tool face ────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def full_registry(tmp_path_factory):
    """Every skill the service registers (builtins, composites, paper reproductions, agent-tool
    bridges, declarative composites). ~15 s, so once per module."""
    root = tmp_path_factory.mktemp("invariance_registry_root")
    mp = pytest.MonkeyPatch()
    mp.setenv("MAST2_PROJECT_ROOT", str(root))
    try:
        from mast.core.registry import SkillRegistry
        from mast.skills.composite.loader import load_spec_skills
        from mast.skills.composite.tool_skills import register_workflow_tool_skills

        reg = SkillRegistry()
        reg.discover()
        register_workflow_tool_skills(reg)
        load_spec_skills(reg)
        yield reg
    finally:
        mp.undo()


def _check_tool_face(profile: str, registry, set_profile, unbound_tool_face) -> None:
    from mast.api.ext import agent_tools
    from mast.skills import hardware_modules, station_face

    set_profile(profile)
    station_face.bind_registry(registry)
    now = station_face.hidden_names(registry)
    then = legacy.hidden_names(registry, profile)
    assert now == then, (
        f"the {profile} tool face changed: newly withheld {sorted(now - then)}, "
        f"newly shown {sorted(then - now)}")
    assert len(then) > 0 or profile == "stm", "the oracle classified nothing: is the registry empty?"
    offered_now = {n for n in agent_tools.OFFERED if agent_tools.is_offered(n)}
    offered_then = {n for n in agent_tools.OFFERED if not legacy.is_stm_only_agent_tool(n, profile)}
    assert offered_now == offered_then
    implied_now = hardware_modules.enabled_ids() - hardware_modules._detected_implied()
    implied_then = hardware_modules.DEFAULT_ENABLED | legacy.PROFILE_IMPLIED_MODULES.get(
        profile, frozenset())
    assert implied_now == implied_then


@pytest.mark.parametrize("profile", PROFILES)
def test_the_golden_files_cover_the_profiles_that_existed(profile):
    for part in ("boot", "briefing", "act"):
        assert profile in load_golden(part)


# ── the STM station ──────────────────────────────────────────────────────────────────────────
class TestTheSTMStationIsUnchanged:
    PROFILE = "stm"

    def test_start_up(self, tmp_path_factory):
        _check("boot", self.PROFILE, tmp_path_factory)

    def test_briefing(self, tmp_path_factory):
        _check("briefing", self.PROFILE, tmp_path_factory)

    def test_mast_act_and_observe(self, tmp_path_factory):
        _check("act", self.PROFILE, tmp_path_factory)

    def test_tool_face(self, full_registry, set_profile, unbound_tool_face):
        _check_tool_face(self.PROFILE, full_registry, set_profile, unbound_tool_face)


# ── the optical station ──────────────────────────────────────────────────────────────────────
class TestTheOpticalStationIsUnchanged:
    PROFILE = "optical"

    def test_start_up(self, tmp_path_factory):
        _check("boot", self.PROFILE, tmp_path_factory)

    def test_briefing(self, tmp_path_factory):
        _check("briefing", self.PROFILE, tmp_path_factory)

    def test_mast_act_and_observe(self, tmp_path_factory):
        _check("act", self.PROFILE, tmp_path_factory)

    def test_tool_face(self, full_registry, set_profile, unbound_tool_face):
        _check_tool_face(self.PROFILE, full_registry, set_profile, unbound_tool_face)
