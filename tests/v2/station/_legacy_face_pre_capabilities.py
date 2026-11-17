"""A frozen copy of the station-face decision before the capability table.

``test_station_invariance.py`` classifies the live registry with this copy and with
``mast.skills.station_face`` and requires the same answer, skill by skill, on the STM and the
optical profile. It is the oracle for the tool face: unlike a list of names, it keeps working when
skills are added, because it classifies whatever the registry holds.

Copied from ``MASTv2/mast/skills/station_face.py`` as it was before the capability table. Two changes only, both
marked ``# ORACLE``: the profile is a parameter instead of ``mast.station.is_optical()``, and the
registry binding (with its ``threading`` import) is gone. Plus the one line of ``mast.skills.hardware_modules`` the face depends on
(``PROFILE_IMPLIED_MODULES``). Do not "fix" this file: it is meant to say what the code said.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Modules whose skills are optical-station skills (prefix match on ``cls.__module__``).
OPTICAL_MODULE_PREFIXES: tuple[str, ...] = (
    "mast.skills.builtins.optics_",
    "mast.skills.builtins.operator_skills",
)

#: Optical modules that nevertheless **need Nanonis** (they call ``context.safe_call`` or
#: read Nanonis signals). ``tests/v2/station/test_station_tool_face.py`` re-derives this
#: set from the source of every ``optics_*`` module, so a new optical module that talks to
#: Nanonis fails the suite until it is listed here.
NANONIS_COUPLED_MODULES: frozenset[str] = frozenset({
    "mast.skills.builtins.optics_scan",           # AcquireSignalPoint, OpticalStageScan
    "mast.skills.builtins.optics_pump_probe",     # PumpProbeScan
    "mast.skills.builtins.optics_motor_groups",   # ListNanonisMotorGroups, NanonisMotorGroupMove
})

#: The optical station's own skill modules (design §6.3): WITec, the laser / temperature /
#: power-meter sources, the measurement protocols, the operator prompts. An STM station
#: does not show their skills (module docstring). Delete an entry to let STM stations have
#: that family - for example ``operator_skills``, if the STM should be able to ask the
#: operator structured questions too. ``tests/v2/station/test_station_tool_face.py`` pins
#: that every ``optics_*`` module is either listed here or one of the benches STM machines
#: already had, so a new module cannot slip onto STM stations unnoticed.
STATION_ONLY_MODULES: tuple[str, ...] = (
    "mast.skills.builtins.optics_witec",
    "mast.skills.builtins.optics_sources",
    "mast.skills.builtins.optics_protocols",
    "mast.skills.builtins.operator_skills",
)

#: Builtin skills that are useful on any station and touch no STM.
GENERIC_SKILLS: frozenset[str] = frozenset({
    "GetTemperature",        # the environment monitor's temperature channel
    "QueryMonitorHistory",   # environment history
    "ReadHardwareEvents",    # hardware / operator events
})

#: Agent-tool bridges (``skills/composite/tool_skills.py``) whose tools are generic.
#: ``data_processing`` is deliberately not here: every one of its tools reads STM scans
#: or STS curves (apart from :data:`GENERIC_DATA_TOOLS`).
GENERIC_AGENT_TOOL_SOURCES: frozenset[str] = frozenset({
    "literature", "paper_writing", "paper_review", "experiment_design",
})
GENERIC_DATA_TOOLS: frozenset[str] = frozenset({"record_analysis"})

#: The instrument-agent tools of the external surface (``mast.api.ext.agent_tools``) that
#: read STM state: the tip registry, scan files and the scan / coarse maps. The rest of
#: that catalogue (experiments, samples, knowledge, plans, documents, environment, the
#: skill market, conduct) is generic and stays.
STM_ONLY_AGENT_TOOLS: frozenset[str] = frozenset({
    "get_current_tip", "list_tips",
    "get_latest_scan_info", "load_scan_file",
    "get_map_analysis", "get_next_scan_position", "get_markers_near", "get_coarse_map",
    "mark_area_used", "plan_scan_batch", "show_plan_on_map", "clear_plan_on_map",
})

def _is_optical_module(module: str) -> bool:
    if module in NANONIS_COUPLED_MODULES:
        return False
    return any(module.startswith(p) for p in OPTICAL_MODULE_PREFIXES)


def _spec_steps(cls: Any) -> set[str] | None:
    """The sub-skills of a declarative composite, or ``None`` when ``cls`` is not one."""
    try:
        from mast.skills.composite.interpreter import SpecComposite

        if not (isinstance(cls, type) and issubclass(cls, SpecComposite)):
            return None
        from mast.agents._shared.skill_forge_tools import _spec_step_skills_of

        return set(_spec_step_skills_of(cls))
    except Exception as exc:  # noqa: BLE001
        logger.debug("station face: cannot read the steps of %s (%s)", cls, exc)
        return None


def _overlay_verdict(name: str, registry: Any) -> bool:
    """An overlay replaces a builtin of the same name (judge by the module it displaced)
    or adds a name of its own (an operator's choice: shown)."""
    try:
        displaced = str(registry.provenance(name).displaced_module or "")
    except Exception:  # noqa: BLE001
        displaced = ""
    if not displaced:
        return True
    return name in GENERIC_SKILLS or _is_optical_module(displaced)


def _direct_verdict(name: str, cls: Any, registry: Any) -> bool | None:
    """True = shown, False = withheld, ``None`` = a declarative composite (decided by its steps)."""
    if cls is None:
        return False
    source = getattr(cls, "_AGENT_TOOL_SOURCE", None)
    if source:
        return source in GENERIC_AGENT_TOOL_SOURCES or (
            source == "data_processing" and name in GENERIC_DATA_TOOLS)
    module = str(getattr(cls, "__module__", "") or "")
    if ".skills._overlay" in module:
        return _overlay_verdict(name, registry)
    if _spec_steps(cls) is not None:
        return None
    if name in GENERIC_SKILLS and module.startswith("mast.skills.builtins"):
        return True
    if _is_optical_module(module):
        return True
    if ".skills.custom" in module:
        return True
    return False


def visible_names(registry: Any) -> frozenset[str]:
    """The skills an optical station shows."""
    metas = list(registry.list_skills())
    names = [str(m.name) for m in metas]
    visible: set[str] = set()
    specs: dict[str, set[str]] = {}
    for name in names:
        try:
            cls = registry.get(name)
        except Exception:  # noqa: BLE001
            cls = None
        verdict = _direct_verdict(name, cls, registry)
        if verdict is True:
            visible.add(name)
        elif verdict is None:
            specs[name] = _spec_steps(cls) or set()
    # a declarative composite is shown iff every step is shown (composites of composites:
    # repeat until nothing changes)
    progress = True
    while progress and specs:
        progress = False
        for name, steps in list(specs.items()):
            if steps and steps <= visible:
                visible.add(name)
                del specs[name]
                progress = True
    return frozenset(visible)


def _module_of(name: str, cls: Any, registry: Any) -> str:
    """Where a skill really comes from: its module, or - for an overlay - the module of the
    builtin it displaced."""
    module = str(getattr(cls, "__module__", "") or "")
    if ".skills._overlay" in module:
        try:
            return str(registry.provenance(name).displaced_module or "") or module
        except Exception:  # noqa: BLE001
            return module
    return module


def station_only_names(registry: Any) -> frozenset[str]:
    """The skills of the optical station's own modules (:data:`STATION_ONLY_MODULES`)."""
    out: set[str] = set()
    for meta in registry.list_skills():
        name = str(meta.name)
        try:
            cls = registry.get(name)
        except Exception:  # noqa: BLE001
            continue
        if _module_of(name, cls, registry) in STATION_ONLY_MODULES:
            out.add(name)
    return frozenset(out)


def hidden_names(registry: Any, profile: str) -> frozenset[str]:   # ORACLE: profile is a parameter
    """Skills this station profile withholds (before the capability table)."""
    if profile != "optical":                                          # ORACLE: was station.is_optical()
        return station_only_names(registry)
    every = frozenset(str(m.name) for m in registry.list_skills())
    return every - visible_names(registry)


def is_stm_only_agent_tool(name: str, profile: str) -> bool:      # ORACLE: profile is a parameter
    """True when ``name`` is an instrument-agent tool that is withheld on this station (before the capability table)."""
    if str(name or "") not in STM_ONLY_AGENT_TOOLS:
        return False
    return profile == "optical"                                       # ORACLE: was station.is_optical()


#: ``mast.skills.hardware_modules.PROFILE_IMPLIED_MODULES`` before the capability table
PROFILE_IMPLIED_MODULES: dict[str, frozenset[str]] = {
    "optical": frozenset({"ters_optics"}),
}
