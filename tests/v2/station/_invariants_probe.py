"""Compute what a station profile decides, for the invariance pins (docs/v2/design/generic_station.md §5).

    python _invariants_probe.py <part> <profile> <project_root>

``part`` is one of ``face`` | ``briefing`` | ``boot`` | ``act``; prints one line
``PROBE_JSON {...}`` on stdout. Each part runs in its own process: ``boot`` calls the real
``CoreRuntime.setup()`` (daemons, process-wide holders, atexit hooks), ``face`` discovers the full
skill registry, and neither may leave anything behind for the next part.

The probe uses only interfaces that exist before the capability table. Pointed at a tree
through ``PYTHONPATH`` it answers for that tree, so the same file
computes "before" (a ``git archive`` export of the old tree) and "after" (the branch) and the two
answers can be compared field by field. ``tests/v2/station/golden/`` holds the "before" answers
of the parts that do not change when a skill is added (``briefing``, ``boot``, ``act``);
``test_station_invariance.py`` compares the live tree against them.

Nothing here touches hardware:

* ``ConnectionPool.connect_all`` is replaced by a recorder (no socket is dialled);
* the serial auto-detection scan (``environment.autodetect.autodetect_sensors``) is replaced by a
  recorder that returns no sensors (no COM port of the machine running the suite is probed);
* the vision warm-up's scorer is a stub (its preload is recorded, no model is loaded);
* ``_io_guard``: any connect beyond loopback and any serial open is refused and reported.

The other Nanonis-bound starts are wrapped, not replaced: they are recorded and still run.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from types import SimpleNamespace

part, profile, root = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["MAST2_PROJECT_ROOT"] = root
os.environ["MAST_STATION_PROFILE"] = profile
os.environ["MAST_LANG"] = "en"
os.makedirs(root, exist_ok=True)

try:
    import nanonis_spm  # noqa: F401
except Exception:  # the real vendor package is not needed
    from unittest.mock import MagicMock

    sys.modules["nanonis_spm"] = MagicMock()

logging.disable(logging.CRITICAL)

import _io_guard  # noqa: E402  (next to this file)

_io_guard.install()

#: the five ``station.describe()`` keys that exist before the capability table (later keys are reported apart)
_DESCRIBE_KEYS = ("profile", "configured", "source", "boot_profile", "restart_pending")


def _emit(out: dict) -> None:
    sys.stdout.write("PROBE_JSON " + json.dumps(out, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _describe() -> dict:
    from mast import station

    d = station.describe()
    return {k: d.get(k) for k in _DESCRIBE_KEYS}


# ── face: the tool face over the full registry ──────────────────────────────────────────────
def probe_face() -> dict:
    from mast.core.registry import SkillRegistry
    from mast.skills import hardware_modules, station_face, tool_face
    from mast.skills.composite.loader import load_spec_skills
    from mast.skills.composite.tool_skills import register_workflow_tool_skills

    reg = SkillRegistry()
    reg.discover()
    register_workflow_tool_skills(reg)
    load_spec_skills(reg)
    station_face.bind_registry(reg)
    f = tool_face.compute(registry=reg)
    every = sorted(str(m.name) for m in reg.list_skills())
    from mast.api.ext import agent_tools

    return {
        "all": every,
        "station": sorted(f.station),
        "hardware": sorted(f.hardware),
        "advanced": sorted(f.advanced),
        "unreadable": list(f.unreadable),
        "visible": sorted(set(every) - set(f.hardware) - set(f.advanced)),
        "hidden_names": sorted(station_face.hidden_names(reg)),
        "hardware_modules_on": sorted(hardware_modules.enabled_ids()),
        "agent_tools_offered": sorted(n for n in agent_tools.OFFERED if agent_tools.is_offered(n)),
    }


# ── briefing: the section table, the station section, the status text ──────────────────────
def _fake_request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=None, jobs=None, vista=None)))


def probe_briefing() -> dict:
    from mast.api.ext import overview as ov

    table = ov._section_table()
    out: dict = {"sections": list(table),
                 "optical_rules": list(getattr(ov, "_OPTICAL_RULES", ()))}
    if "station" in table:
        sec = table["station"](None, None)
        out["station_text"] = sec.get("text")
        out["station_data_profile"] = (sec.get("data") or {}).get("profile")
    req = _fake_request()
    s, degraded = ov._status(req)
    out["status_keys"] = sorted(s)
    out["status_station"] = s.get("station")
    out["status_degraded"] = list(degraded)
    out["status_text"] = ov._status_text(s)
    try:
        sec = ov._sec_safety(req, None)
        out["safety_text"] = sec.get("text")
        out["safety_source"] = (sec.get("data") or {}).get("limits_source")
    except Exception as exc:  # noqa: BLE001 - recorded: the same code fails the same way
        out["safety_error"] = type(exc).__name__
    asked = {}
    for name in ("tip", "instrument", "live", "prefs", "recent_files", "station", "recent_spectra"):
        asked[name] = name in table
    out["has_section"] = asked
    return out


# ── boot: what CoreRuntime.setup() starts ───────────────────────────────────────────────────
def probe_boot() -> dict:
    calls: list[str] = []          # made on the main thread, in order
    async_calls: list[str] = []    # made on a worker thread (the vision warm-up): order not pinned

    import mast.core.connection as conn
    import mast.core.state as state_mod
    import mast.core.watchdog as watchdog_mod
    import mast.environment.autodetect as autodetect_mod
    import mast.instruments.emission_safety as emission_safety
    import mast.monitoring.service as monitor_service

    def _no_dial(self):
        calls.append("connect_all")
        return {role: False for role in ("main", "monitor", "data", "emergency")}

    conn.ConnectionPool.connect_all = _no_dial

    def _wrap(owner, name, label):
        orig = getattr(owner, name)

        def wrapper(*a, **k):
            calls.append(label)
            return orig(*a, **k)
        setattr(owner, name, wrapper)

    _wrap(state_mod.InstrumentState, "start_background_refresh", "start_background_refresh")
    _wrap(watchdog_mod.SafetyWatchdog, "start", "watchdog.start")
    _wrap(monitor_service, "start_service", "current_monitor.start_service")
    _wrap(emission_safety, "install_revocation_listener", "install_revocation_listener")

    def _no_scan(*a, **k):
        calls.append("autodetect_sensors")
        return []

    autodetect_mod.autodetect_sensors = _no_scan

    # the vision warm-up: let it decide, never load a model
    try:
        import mast.vision.module as vision_module
        import mast.vision.quality_model as quality_model

        vision_module.vision_models_enabled = lambda: True

        class _Scorer:
            def preload(self):
                async_calls.append("vision_warmup.preload")

        quality_model.get_scorer = lambda: _Scorer()
    except Exception as exc:  # noqa: BLE001
        calls.append(f"vision_stub_failed:{type(exc).__name__}")

    from mast.config import MASTConfig
    from mast.core.runtime import CoreRuntime

    for meth, label in (("_start_map_activity_watcher", "map_activity_watcher"),
                        ("_build_nanonis_env_sensors", "nanonis_env_sensors"),
                        ("_install_operator_hitl", "install_operator_hitl")):
        _wrap(CoreRuntime, meth, label)

    rt = CoreRuntime(MASTConfig())
    rt.setup()
    for t in threading.enumerate():
        if t.name == "mast-vision-warmup":
            t.join(timeout=10.0)

    from mast.environment.config import autodetect_policy
    from mast.skills import hardware_modules

    out = {
        "calls": calls,
        "async_calls": sorted(async_calls),
        "profile": rt._station_profile,
        "describe": _describe(),
        "ports": rt.config.nanonis.all_ports,
        "pool": type(rt._pool).__name__ if rt._pool is not None else None,
        "state": type(rt._state).__name__ if rt._state is not None else None,
        "executor": type(rt._executor).__name__ if rt._executor is not None else None,
        "threads": sorted({t.name for t in threading.enumerate()
                           if not t.name.startswith(("Thread-", "ThreadPoolExecutor", "asyncio_",
                                                     "Dummy-", "pydevd"))}),
        "autodetect_policy": {key: list(autodetect_policy(cfg)) for key, cfg in (
            ("absent", {}), ("true", {"autodetect": True}), ("false", {"autodetect": False}))},
        "hardware_modules_on": sorted(hardware_modules.enabled_ids()),
        "reconnect": rt.reconnect() if rt._station_profile != "stm" else "not called on stm",
    }
    try:
        rt.shutdown()
    except Exception as exc:  # noqa: BLE001
        out["shutdown_error"] = str(exc)
    return out


# ── act: what mast_act refuses, and the shape of an observation ─────────────────────────────
def probe_act() -> dict:
    from mast.vista.actions import ACTIONS
    from mast.vista.service import VistaError, VistaService, _Caller

    svc = VistaService(root=os.path.join(root, "vista"), start_watcher=False)
    svc.bind(SimpleNamespace(), jobs=SimpleNamespace())
    caller = _Caller(actor="probe", session="s1")
    out: dict = {"actions": {}}
    for i, name in enumerate(sorted(ACTIONS)):
        o = svc.observe(caller)
        tok = o.get("next_token")
        try:
            svc.submit_act(caller, {"action": name, "params": {}, "after": tok})
            got = {"outcome": "accepted"}
        except VistaError as exc:
            if exc.code == "not_available_on_this_station":
                got = {"outcome": "refused", "status": exc.status, "extra": exc.extra,
                       "detail": exc.detail, "token_still_live": bool(svc.gate.is_live(tok))}
            else:
                got = {"outcome": "passed_the_station_check", "code": exc.code}
        except Exception as exc:  # noqa: BLE001 - past the station check, failed for another reason
            got = {"outcome": "passed_the_station_check", "error": type(exc).__name__}
        out["actions"][name] = got
    body = json.loads(svc.observe(caller)["text"])
    out["observe_keys"] = sorted(body)
    out["observe_station"] = body.get("station")
    out["observe_instrument_keys"] = sorted(body.get("instrument") or {})
    out["watch_once"] = len(svc.watch_once())
    svc.shutdown()
    return out


PARTS = {"face": probe_face, "briefing": probe_briefing, "boot": probe_boot, "act": probe_act}

if __name__ == "__main__":
    result = {"part": part, "profile": profile, "describe_before": None}
    try:
        import mast

        result["mast_file"] = str(mast.__file__)       # which tree answered
        result["describe_before"] = _describe()
        result.update(PARTS[part]())
        try:
            from mast import station

            caps = getattr(station, "capabilities", None)
            result["capabilities"] = sorted(caps()) if callable(caps) else None
        except Exception:  # noqa: BLE001
            result["capabilities"] = None
    except Exception as exc:  # noqa: BLE001
        import traceback

        result["probe_error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-3000:]
    _emit(result)
    os._exit(0)          # no atexit replay of what setup() registered
