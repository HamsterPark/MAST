"""Which skills an optical station shows: the fourth gate of :mod:`mast.skills.tool_face`.

MAST registers about 650 skills, roughly 400 of them Nanonis/STM skills and the rest
STM image analysis, STM composites, paper reproductions and so on. On the optical
station (station profile ``optical``, :mod:`mast.station`; design
``docs/v2/design/optical_station_witec.md``) there is no Nanonis at all. An AI that
sees all of them wastes its context on tools that can only fail, and a skill search for
"scan" or "bias" finds nothing it can use. So on an optical station the tool face is a
**whitelist**: a skill is shown only if it is one of

1. an **optical** skill: it lives in a ``mast.skills.builtins.optics_*`` module (the
   existing optical bench, plus the WITec / laser / protocol modules of the optical
   station) or in ``operator_skills`` (asking the operator, reading the manual optical
   state) - *except* the three modules that drive Nanonis (:data:`NANONIS_COUPLED_MODULES`:
   signal acquisition at an optical position, the pump-probe scan, the Nanonis motor
   groups);
2. a **generic** skill that does not touch an STM: the literature / paper-writing /
   paper-review / experiment-design tool bridges, ``record_analysis``, and the few
   builtins listed in :data:`GENERIC_SKILLS`;
3. a **user-made** skill: a custom skill installed on this machine, an overlay that adds
   a new name, or a declarative composite *all of whose steps are themselves shown*
   (a composite that scans is not shown, one that only moves optics and acquires
   spectra is).

Everything else is withheld. A whitelist, not a blacklist, on purpose: a new STM skill
added next year must not appear on the optical station by default, and the cost of a
missed optical skill is a test failure (``tests/v2/station``), not a silent leak.

Why modules, and not the ``optics`` tag: the eleven TERS composites carry the ``optics``
tag and all need Nanonis (they read the tip state in their preflight). The module is the
fact; the tag is the author's description.

STM profile: the same gate, the other direction. An STM station withholds exactly the
skills of the optical station's own modules (:data:`STATION_ONLY_MODULES`: WITec, the
laser / temperature / power-meter sources, the measurement protocols, the operator
prompts) and nothing else. Those skills exist only because the optical station exists,
and "an STM machine's tool face is what it was" is the hard constraint (design D1) - no
other gate would keep them off: the hardware-module gate lists skill names one by one and
knows none of them, the capability and subscription gates are open by default. They are
kept off **by module, not by name**, so a skill an owner adds to one of those modules
tomorrow does not appear on an STM machine because nobody edited a list. The optical
bench modules STM machines have always had (``optics_stage``, ``optics_spectro`` ...) are
not in that tuple; the hardware-module gate keeps deciding about those exactly as before.
While the optical station's modules register nothing the STM answer is the empty set.

The registry is found through :func:`bind_registry` (``CoreRuntime.setup`` binds the live
one): the external door and the skill card call :func:`mast.skills.tool_face.compute`
without arguments, and a classification needs the skill classes, not just their names.
With no registry bound the optical gate cannot be computed: ``tool_face`` then reports it
in ``unreadable`` and filters nothing (a gate we cannot read is not a gate). An STM
station with no registry bound has nothing to classify and withholds nothing, as before.
"""
from __future__ import annotations

import logging
import threading
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

_lock = threading.Lock()
_bound: Any = None


def bind_registry(registry: Any) -> None:
    """Publish the live skill registry (``CoreRuntime.setup``); ``None`` unbinds (tests)."""
    global _bound
    with _lock:
        _bound = registry


def bound_registry() -> Any:
    with _lock:
        return _bound


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


def hidden_names(registry: Any = None) -> frozenset[str]:
    """Skills this station profile withholds.

    Optical: everything that is not on the whitelist. STM: only the skills of the optical
    station's own modules (:data:`STATION_ONLY_MODULES`), and the empty set when there is no
    registry to classify with - an STM station never fails here.

    Raises ``LookupError`` when the optical profile is in force and no registry is
    available to classify (the caller reports an unreadable gate, it does not guess).
    """
    from mast import station

    reg = registry if registry is not None else bound_registry()
    if not station.is_optical():
        return station_only_names(reg) if reg is not None else frozenset()
    if reg is None:
        raise LookupError("no skill registry bound: cannot classify skills for the optical profile")
    every = frozenset(str(m.name) for m in reg.list_skills())
    return every - visible_names(reg)


def is_stm_only_agent_tool(name: str) -> bool:
    """True when ``name`` is an instrument-agent tool that is withheld on this station."""
    if str(name or "") not in STM_ONLY_AGENT_TOOLS:
        return False
    try:
        from mast import station

        return station.is_optical()
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "GENERIC_AGENT_TOOL_SOURCES", "GENERIC_DATA_TOOLS", "GENERIC_SKILLS",
    "NANONIS_COUPLED_MODULES", "OPTICAL_MODULE_PREFIXES", "STATION_ONLY_MODULES",
    "STM_ONLY_AGENT_TOOLS", "bind_registry", "bound_registry", "hidden_names",
    "is_stm_only_agent_tool", "station_only_names", "visible_names",
]
