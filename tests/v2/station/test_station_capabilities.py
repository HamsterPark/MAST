"""The station capability table (docs/v2/design/generic_station.md §1).

A profile only chooses a set of capabilities; code asks ``station.has("<capability>")``. Pinned
here: the table itself, the fallbacks (an unknown profile is the STM, an unknown capability name is
a programming error), the serial auto-detection policy, what ``/api/health`` and the settings
write say about it, and two structural rules over the production code - nothing outside
``mast/station.py`` decides by the profile *name*, and every capability a call site names exists.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from mast import station

_REPO = Path(__file__).resolve().parents[3]
_MAST = _REPO / "MASTv2" / "mast"

STM = {"nanonis", "stm_safety", "stm_actions", "scan_map"}
OPTICAL = {"witec", "optical_bench", "operator_prompts", "laser_safety"}
GENERIC = {"optical_bench", "operator_prompts", "laser_safety", "sensor_echo"}


# ── the table ──────────────────────────────────────────────────────────────────────────────
def test_the_profiles_and_their_identity_strings():
    assert station.PROFILES == ("stm", "optical", "generic")
    assert station.PROFILE_GENERIC == "generic" and station.DEFAULT_PROFILE == "stm"


def test_the_capability_names_are_exactly_these_in_this_order():
    assert station.CAPABILITIES == ("nanonis", "stm_safety", "stm_actions", "scan_map", "witec",
                                    "optical_bench", "operator_prompts", "laser_safety",
                                    "sensor_echo")


@pytest.mark.parametrize("profile, caps", [("stm", STM), ("optical", OPTICAL), ("generic", GENERIC)])
def test_each_profile_has_exactly_its_capabilities(profile, caps):
    assert set(station.capabilities_of(profile)) == caps


def test_a_generic_station_has_neither_nanonis_nor_witec():
    caps = station.capabilities_of("generic")
    assert "nanonis" not in caps and "witec" not in caps


@pytest.mark.parametrize("profile", ["stm", "optical", "generic"])
def test_every_profile_is_closed_under_the_requirements(profile):
    caps = station.capabilities_of(profile)
    for cap in caps:
        missing = station.CAPABILITY_REQUIRES.get(cap, frozenset()) - caps
        assert not missing, f"{profile}: {cap} needs {sorted(missing)}"


def test_the_requirements_name_only_known_capabilities():
    for cap, needs in station.CAPABILITY_REQUIRES.items():
        assert cap in station.CAPABILITIES and needs <= set(station.CAPABILITIES)


def test_every_capability_is_held_by_some_profile():
    held = set().union(*(station.capabilities_of(p) for p in station.PROFILES))
    assert held == set(station.CAPABILITIES)


@pytest.mark.parametrize("bad", [None, "", "Optical ", "generical", 3])
def test_an_unknown_profile_has_the_stm_capabilities(bad):
    want = station.capabilities_of("stm")
    if bad == "Optical ":
        want = station.capabilities_of("optical")     # normalised, not unknown
    assert station.capabilities_of(bad) == want


# ── has / runtime_has ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("profile, caps", [(None, STM), ("stm", STM), ("optical", OPTICAL),
                                           ("generic", GENERIC), ("nonsense", STM)])
def test_has_answers_for_the_profile_in_force(set_profile, profile, caps):
    set_profile(profile)
    for cap in station.CAPABILITIES:
        assert station.has(cap) is (cap in caps), cap
    assert station.capabilities() == frozenset(caps)


def test_an_unknown_capability_name_is_an_error_not_false(set_profile):
    for profile in ("stm", "optical", "generic"):
        set_profile(profile)
        with pytest.raises(ValueError):
            station.has("nanonsi")
        with pytest.raises(ValueError):
            station.runtime_has(SimpleNamespace(_station_profile=profile), "Nanonis")


def test_an_unreadable_profile_has_the_stm_capabilities_not_none(set_profile, monkeypatch):
    """Every caller used to fall back to "STM" when the profile could not be read; under the
    table that is the STM's set - ``nanonis`` true - not an empty set (which would stop an STM
    station dialling its controller because a settings file was locked)."""
    def boom():
        raise RuntimeError("settings store is gone")

    set_profile("generic")
    monkeypatch.setattr(station, "current_profile", boom)
    assert station.capabilities() == station.capabilities_of("stm")
    assert station.has("nanonis") is True and station.has("operator_prompts") is False


def test_runtime_has_prefers_the_profile_the_runtime_started_under(set_profile):
    set_profile("generic")
    assert station.runtime_has(SimpleNamespace(_station_profile="stm"), "nanonis") is True
    assert station.runtime_has(SimpleNamespace(_station_profile="optical"), "witec") is True
    assert station.runtime_has(SimpleNamespace(), "nanonis") is False      # the process profile
    assert station.runtime_has(object(), "sensor_echo") is True
    set_profile(None)
    assert station.runtime_has(SimpleNamespace(_station_profile=None), "nanonis") is True


def test_describe_lists_the_capabilities_in_table_order(set_profile):
    set_profile("generic")
    d = station.describe()
    assert d["profile"] == "generic"
    assert d["capabilities"] == ["optical_bench", "operator_prompts", "laser_safety", "sensor_echo"]
    assert d["serial_autodetect"] == "default_off"
    for key in ("configured", "source", "boot_profile", "restart_pending"):
        assert key in d


# ── serial auto-detection (§1.4) ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("profile, want", [("stm", "default_on"), ("optical", "forced_off"),
                                           ("generic", "default_off"), (None, "default_on")])
def test_the_auto_detection_default_of_each_profile(set_profile, profile, want):
    set_profile(profile)
    assert station.serial_autodetect_default() == want


def _store_with(tmp_path, monkeypatch, **values):
    from mast.webui import settings_store as ss

    store = ss.SettingsStore(ss.default_config_dir(tmp_path))
    if values:
        store.update(**values)
    monkeypatch.setattr(ss, "settings_store_for_runtime", lambda: store)
    return store


@pytest.mark.parametrize("stored, want", [(None, None), (True, True), (False, False)])
def test_the_auto_detection_setting_is_read_as_stored(tmp_path, monkeypatch, stored, want):
    if stored is None:
        _store_with(tmp_path, monkeypatch)
    else:
        _store_with(tmp_path, monkeypatch, serial_autodetect=stored)
    assert station.serial_autodetect_setting() is want


#: (profile, setting, file) -> (on, reason fragment); the whole table of design §1.4
POLICY = [
    # the STM with no setting: exactly what every STM station has always done
    ("stm", None, {}, True, "enabled in environment_sensors.json (default)"),
    ("stm", None, {"autodetect": True}, True, "enabled in environment_sensors.json (default)"),
    ("stm", None, {"autodetect": False}, False, "autodetect is false in environment_sensors.json"),
    ("stm", False, {}, False, "serial_autodetect"),
    ("stm", True, {}, True, "serial_autodetect"),
    ("stm", True, {"autodetect": False}, False, "autodetect is false in environment_sensors.json"),
    # the optical station: forced off, whatever anyone says
    ("optical", None, {}, False, "forced off by the optical station profile"),
    ("optical", True, {"autodetect": True}, False, "forced off by the optical station profile"),
    # the generic station: off unless the setting says on; the file can only veto
    ("generic", None, {}, False, "generic"),
    ("generic", None, {"autodetect": True}, False, "generic"),
    ("generic", False, {"autodetect": True}, False, "serial_autodetect"),
    ("generic", True, {}, True, "serial_autodetect"),
    ("generic", True, {"autodetect": True}, True, "serial_autodetect"),
    ("generic", True, {"autodetect": False}, False, "autodetect is false in environment_sensors.json"),
]


@pytest.mark.parametrize("profile, setting, cfg, on, why", POLICY)
def test_the_auto_detection_policy(tmp_path, monkeypatch, set_profile, profile, setting, cfg, on, why):
    from mast.environment.config import autodetect_policy

    set_profile(profile)
    if setting is None:
        _store_with(tmp_path, monkeypatch)
    else:
        _store_with(tmp_path, monkeypatch, serial_autodetect=setting)
    got, reason = autodetect_policy(cfg)
    assert got is on and why in reason, (got, reason)


def test_a_sensor_saved_from_the_ui_does_not_switch_a_generic_scan_on(tmp_path, monkeypatch, set_profile):
    """``update_sensor`` writes ``autodetect: true`` into a file that had no such key (the
    loader fills the default in). That write must not turn the serial scan on."""
    from mast.environment import config as env_config

    set_profile("generic")
    _store_with(tmp_path, monkeypatch)
    path = tmp_path / "environment_sensors.json"
    env_config.update_sensor("tc", type="lakeshore_temp", port="COM19", path=path)
    assert env_config.load_config(path)["autodetect"] is True
    assert env_config.autodetect_policy(env_config.load_config(path))[0] is False


# ── the API ────────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """The settings read / write, health and sensor-config routes on one app (as
    ``test_station_profile.py`` builds it)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mast.api.context import AppContext
    from mast.api.routes.admin import router as admin_router
    from mast.api.routes.health import router as health_router
    from mast.api.routes.settings import router as read_router
    from mast.api.routes.settings_admin_write import router as write_router
    from mast.webui.settings_store import set_process_store

    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    app = FastAPI()
    app.state.ctx = AppContext(user_root=str(tmp_path))
    for r in (read_router, write_router, health_router, admin_router):
        app.include_router(r, prefix="/api")
    store = app.state.ctx.settings_store
    set_process_store(store)
    return SimpleNamespace(client=TestClient(app), store=store)


@pytest.mark.parametrize("profile, caps, auto", [
    ("stm", ["nanonis", "stm_safety", "stm_actions", "scan_map"], "default_on"),
    ("optical", ["witec", "optical_bench", "operator_prompts", "laser_safety"], "forced_off"),
    ("generic", ["optical_bench", "operator_prompts", "laser_safety", "sensor_echo"], "default_off"),
])
def test_health_reports_the_capabilities_in_force(rig, set_profile, profile, caps, auto):
    set_profile(profile)
    body = rig.client.get("/api/health").json()
    assert body["station_profile"] == profile
    assert body["station_capabilities"] == caps
    assert body["station_serial_autodetect"] == auto


def test_a_generic_profile_is_accepted_by_the_settings_write(rig, admin_pin):
    r = rig.client.post("/api/settings", json={"station_profile": " Generic ", "admin_pin": admin_pin})
    assert r.json()["ok"] is True
    assert rig.store.get("station_profile") == "generic"
    assert station.configured_profile() == "generic"


def test_a_misspelt_profile_is_still_refused_and_the_message_lists_all_three(rig, admin_pin):
    r = rig.client.post("/api/settings", json={"station_profile": "generical", "admin_pin": admin_pin})
    body = r.json()
    assert body["ok"] is False and "station_profile" in body["rejected"]
    for name in ("stm", "optical", "generic"):
        assert name in body["rejected"]["station_profile"]
    assert rig.store.get("station_profile") is None


def test_the_auto_detection_setting_needs_the_admin_pin(rig, admin_pin):
    from mast.api.admin_pin import GUARDED_KEYS

    assert "serial_autodetect" in GUARDED_KEYS
    r = rig.client.post("/api/settings", json={"serial_autodetect": True})
    assert r.json()["ok"] is False and r.json()["pin_required"] is True
    assert rig.store.get("serial_autodetect") is None
    r = rig.client.post("/api/settings", json={"serial_autodetect": True, "admin_pin": admin_pin})
    body = r.json()
    assert body["ok"] is True and rig.store.get("serial_autodetect") is True
    assert "serial" in body["rebuild_note"].lower() or "串口" in body["rebuild_note"]
    assert rig.client.get("/api/settings").json()["serial_autodetect"] is True


def test_the_sensor_page_says_whether_the_scan_will_really_run(rig, set_profile):
    set_profile("optical")
    body = rig.client.get("/api/environment/sensors").json()
    assert body["autodetect"] is True, "the file's own value, unchanged"
    assert body["autodetect_effective"] is False
    assert "optical" in body["autodetect_reason"]
    set_profile("stm")
    body = rig.client.get("/api/environment/sensors").json()
    assert body["autodetect_effective"] is True


# ── structure: nobody decides by the profile name ──────────────────────────────────────────
_NAME_CHECKS = {"is_optical", "is_stm", "runtime_is_optical"}
_PROFILE_LITERALS = {"stm", "optical", "generic"}
_PROFILE_CONSTANTS = {"PROFILE_STM", "PROFILE_OPTICAL", "PROFILE_GENERIC"}


def _production_files():
    """Every ``*.py`` under ``mast/``, build-generated ones included (``_buildinfo.py``,
    ``update/_defaults.py``): the build writes those from Windows PowerShell with a UTF-8 BOM, so
    readers here decode with ``utf-8-sig``, as Python's own import does."""
    for p in sorted(_MAST.rglob("*.py")):
        if p.name == "station.py" and p.parent == _MAST:
            continue
        yield p


def _is_profile_operand(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and node.value in _PROFILE_LITERALS:
        return True
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
    return name in _PROFILE_CONSTANTS


def test_no_production_code_decides_by_the_profile_name():
    """Design §1.3: decisions ask ``has(<capability>)``. Calling ``is_optical()`` & co., or
    comparing something with ``"optical"`` / ``PROFILE_OPTICAL``, outside ``mast/station.py`` is
    the pattern the capability table replaced; a display table keyed by the profile is fine."""
    found = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in _NAME_CHECKS:
                    found.append(f"{path.relative_to(_REPO)}:{node.lineno} calls {name}()")
            elif isinstance(node, ast.Compare):
                if any(_is_profile_operand(x) for x in [node.left, *node.comparators]):
                    found.append(f"{path.relative_to(_REPO)}:{node.lineno} compares with a profile name")
    assert not found, "\n".join(found)


def test_the_guard_above_would_see_a_profile_check():
    tree = ast.parse('if station.is_optical() or prof == "optical" or x is PROFILE_GENERIC:\n    pass')
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    comps = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)]
    assert calls and calls[0].func.attr in _NAME_CHECKS
    assert sum(any(_is_profile_operand(x) for x in [c.left, *c.comparators]) for c in comps) == 2


#: thin wrappers around ``station.has`` that a module defines for itself (module -> name)
_WRAPPERS = {"api/ext/overview.py": "_has", "vista/service.py": "_has",
             "core/runtime.py": "_station_has"}


def _imports_station(tree: ast.AST) -> bool:
    """Whether the module imports ``mast.station`` (as ``station`` or ``_station``): only then is
    a ``station.has(...)`` call the capability table (``satellite/manual.py`` has a variable of
    that name with a ``has`` of its own)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "mast":
            if any(a.name == "station" for a in node.names):
                return True
    return False


def _named_capabilities(tree: ast.AST, rel: str):
    """Every capability a call site names: ``station.has("x")``, ``station.runtime_has(rt, "x")``
    (also through ``_station``), the module's own wrapper (:data:`_WRAPPERS`) and
    ``"x" in caps`` / ``"x" not in caps``."""
    wrapper = _WRAPPERS.get(rel)
    via_station = _imports_station(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            hit = False
            if via_station and isinstance(f, ast.Attribute) and f.attr in ("has", "runtime_has"):
                hit = isinstance(f.value, ast.Name) and f.value.id in ("station", "_station")
            if wrapper is not None:
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                hit = hit or name == wrapper
            if hit:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        yield node.lineno, arg.value
        elif via_station and isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) \
                and isinstance(node.left.value, str) and len(node.ops) == 1 \
                and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            right = node.comparators[0]
            rname = right.id if isinstance(right, ast.Name) else getattr(right, "attr", "")
            if rname in ("caps", "_caps", "capabilities"):
                yield node.lineno, node.left.value


def test_every_capability_a_call_site_names_is_in_the_table():
    unknown = []
    seen = set()
    for path in _production_files():
        rel = path.relative_to(_MAST).as_posix()
        for lineno, cap in _named_capabilities(ast.parse(path.read_text(encoding="utf-8-sig")), rel):
            seen.add(cap)
            if cap not in station.CAPABILITIES:
                unknown.append(f"{path.relative_to(_REPO)}:{lineno} names {cap!r}")
    assert not unknown, "\n".join(unknown)
    # the tables that name capabilities as data
    from mast.api.ext import overview
    from mast.skills import hardware_modules, station_face

    tables = {
        "station_face.STATION_MODULE_CAPABILITY": set(station_face.STATION_MODULE_CAPABILITY.values()),
        "station_face.STM_AGENT_TOOL_CAPABILITY": set(station_face.STM_AGENT_TOOL_CAPABILITY.values()),
        "hardware_modules.CAPABILITY_IMPLIED_MODULES": set(hardware_modules.CAPABILITY_IMPLIED_MODULES),
        "overview.STATION_ORDER": set().union(*(needs for _, needs in overview.STATION_ORDER)),
    }
    for where, caps in tables.items():
        assert caps <= set(station.CAPABILITIES), f"{where} names {sorted(caps - set(station.CAPABILITIES))}"
        seen |= caps
    # and the table is used: a capability nothing asks about is a capability nothing does
    assert seen >= set(station.CAPABILITIES), sorted(set(station.CAPABILITIES) - seen)


def test_the_api_type_lists_the_same_capabilities():
    import typing

    from mast.api.schemas import StationCapability

    assert typing.get_args(StationCapability) == station.CAPABILITIES


# ── the frontend's copy of the table ───────────────────────────────────────────────────────
def test_the_frontend_capability_table_is_the_backends():
    """``frontend/src/lib/stationCapabilities.json`` is generated by
    ``MASTv2/scripts/dump_station_capabilities.py``; a backend table change without regenerating
    it leaves the UI deciding by an old table."""
    import json
    got = json.loads((_REPO / "frontend" / "src" / "lib" / "stationCapabilities.json").read_text(encoding="utf-8"))
    assert got["capabilities"] == list(station.CAPABILITIES)
    assert got["profiles"] == {p: station.ordered(c) for p, c in station.PROFILE_CAPABILITIES.items()}
    assert got["requires"] == {c: station.ordered(r) for c, r in station.CAPABILITY_REQUIRES.items()}
    assert list(got["profiles"]) == list(station.PROFILES)
