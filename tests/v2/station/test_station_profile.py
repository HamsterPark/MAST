"""The station profile: how it is read, latched at boot, written and reported.

Design: ``docs/v2/design/optical_station_witec.md`` D1. The profile is ``stm`` unless the
environment variable ``MAST_STATION_PROFILE`` or the setting ``station_profile`` says
``optical``; an unrecognised value is ``stm`` plus a WARNING (a typo must never turn a
working STM station into something else); and what the running process *is* is the
profile it started under, so editing the setting changes ``configured_profile()`` and
``restart_pending()`` but not ``current_profile()``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast import station
from mast.api.context import AppContext
from mast.api.routes.health import router as health_router
from mast.api.routes.settings import router as read_router
from mast.api.routes.settings_admin_write import router as write_router
from mast.webui.settings_store import (
    KNOWN_KEYS,
    SettingsStore,
    default_config_dir,
    set_process_store,
    settings_store_for_runtime,
)

_REPO = Path(__file__).resolve().parents[3]


def _store():
    return settings_store_for_runtime()


# ── reading ─────────────────────────────────────────────────────────────────────
def test_default_is_stm():
    info = station.resolve()
    assert (info.profile, info.source, info.valid) == ("stm", "default", True)
    assert station.current_profile() == "stm" and station.is_stm() and not station.is_optical()


def test_the_identity_strings_are_exactly_what_the_api_and_the_files_carry():
    # compared, stored and sent over the API; never translated
    assert station.PROFILES == ("stm", "optical")
    assert station.SETTINGS_KEY == "station_profile" and station.SETTINGS_KEY in KNOWN_KEYS
    assert station.ENV_VAR == "MAST_STATION_PROFILE"


@pytest.mark.parametrize("raw,want", [("optical", "optical"), ("stm", "stm"),
                                      (" Optical ", "optical"), ("OPTICAL", "optical"),
                                      ("\tstm\n", "stm")])
def test_the_environment_variable_selects_the_profile(set_profile, raw, want):
    set_profile(raw)
    info = station.resolve()
    assert (info.profile, info.source) == (want, "env")
    assert station.current_profile() == want


def test_an_unknown_environment_value_is_stm_and_warns_once(set_profile, caplog):
    set_profile("opitcal")
    with caplog.at_level(logging.WARNING, logger="mast.station"):
        for _ in range(3):
            assert station.current_profile() == "stm"
    warns = [r for r in caplog.records if r.name == "mast.station"]
    assert len(warns) == 1, "one WARNING per distinct bad value, not one per call"
    assert "opitcal" in warns[0].getMessage() and "stm" in warns[0].getMessage()
    assert station.resolve().valid is False and station.resolve().raw == "opitcal"


def test_the_setting_selects_the_profile():
    _store().update(station_profile="optical")
    info = station.resolve()
    assert (info.profile, info.source) == ("optical", "settings")
    assert station.is_optical()


@pytest.mark.parametrize("bad", ["opitcal", "optical2", 5, True, ["optical"]])
def test_an_unknown_setting_is_stm_and_warns(bad, caplog):
    _store().update(station_profile=bad)
    with caplog.at_level(logging.WARNING, logger="mast.station"):
        assert station.current_profile() == "stm"
    assert any(r.name == "mast.station" for r in caplog.records)


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_values_count_as_unset(set_profile, empty):
    set_profile(empty)
    _store().update(station_profile=empty)
    assert station.resolve().source == "default" and station.current_profile() == "stm"


def test_the_environment_beats_the_setting(set_profile):
    _store().update(station_profile="optical")
    set_profile("stm")
    assert station.current_profile() == "stm"
    assert station.resolve().source == "env"


def test_the_setting_survives_a_restart_of_the_store(tmp_path):
    first = SettingsStore(default_config_dir(tmp_path))
    first.update(station_profile="optical")
    reborn = SettingsStore(default_config_dir(tmp_path))
    assert reborn.get("station_profile") == "optical"


# ── the boot latch ──────────────────────────────────────────────────────────────
def test_before_any_boot_nothing_is_pending():
    assert station.boot_profile() is None and station.restart_pending() is False


def test_editing_the_setting_after_boot_does_not_change_what_the_process_is(caplog):
    with caplog.at_level(logging.INFO, logger="mast.station"):
        assert station.note_boot() == "stm"
    assert any("station profile: stm" in r.getMessage() for r in caplog.records), \
        "the startup log must name the profile"
    _store().update(station_profile="optical")                 # the operator flips the switch
    assert station.current_profile() == "stm", "half the subsystems were started as stm"
    assert station.configured_profile() == "optical"
    assert station.boot_profile() == "stm"
    assert station.restart_pending() is True
    d = station.describe()
    assert d == {"profile": "stm", "configured": "optical", "source": "settings",
                 "boot_profile": "stm", "restart_pending": True}


def test_booting_as_optical_stays_optical_when_the_setting_is_cleared():
    _store().update(station_profile="optical")
    assert station.note_boot() == "optical"
    _store().update(station_profile="stm")
    assert station.current_profile() == "optical" and station.restart_pending() is True


def test_the_latch_is_retired_when_another_settings_store_is_published(tmp_path):
    """The suite publishes a fresh store per test; that is what keeps a latch left by an
    earlier test from deciding this one."""
    assert station.note_boot() == "stm"
    other = SettingsStore(default_config_dir(tmp_path))
    other.update(station_profile="optical")
    set_process_store(other)
    assert station.boot_profile() is None
    assert station.current_profile() == "optical" and station.restart_pending() is False


def test_the_environment_variable_pins_the_profile_whatever_was_latched(set_profile):
    station.note_boot()                                         # stm
    set_profile("optical")
    assert station.current_profile() == "optical"
    assert station.restart_pending() is False, "a restart cannot change a pinned profile"


def test_runtime_is_optical_works_on_any_object(set_profile):
    from types import SimpleNamespace

    assert station.runtime_is_optical(SimpleNamespace()) is False
    set_profile("optical")
    assert station.runtime_is_optical(SimpleNamespace()) is True
    assert station.runtime_is_optical(SimpleNamespace(_station_profile="stm")) is False
    assert station.runtime_is_optical(SimpleNamespace(_station_profile="optical")) is True
    set_profile("stm")
    assert station.runtime_is_optical(SimpleNamespace(_station_profile="optical")) is True
    assert station.runtime_is_optical(SimpleNamespace(_station_profile=None)) is False
    assert station.runtime_is_optical(object()) is False


def test_reset_forgets_the_latch():
    station.note_boot()
    _store().update(station_profile="optical")
    assert station.restart_pending() is True
    station.reset()
    assert station.boot_profile() is None and station.restart_pending() is False
    assert station.current_profile() == "optical"


# ── writing and reporting it through the API ────────────────────────────────────
@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    app = FastAPI()
    app.state.ctx = AppContext(user_root=str(tmp_path))
    for r in (read_router, write_router, health_router):
        app.include_router(r, prefix="/api")
    # bootstrap wires ctx.settings_store = rt._settings, which the runtime also publishes
    set_process_store(app.state.ctx.settings_store)
    return TestClient(app), tmp_path


def test_a_valid_write_lands_reads_back_and_says_a_restart_is_needed(rig, admin_pin):
    client, root = rig
    r = client.post("/api/settings", json={"station_profile": "optical", "admin_pin": admin_pin}).json()
    assert r["ok"] is True and r["rejected"] == {}
    assert r["persisted"]["station_profile"] == "optical"
    assert "站点画像" in r["rebuild_note"] and "下次启动生效" in r["rebuild_note"], r["rebuild_note"]
    assert client.get("/api/settings").json()["station_profile"] == "optical"
    assert SettingsStore(default_config_dir(root)).get("station_profile") == "optical"


def test_the_stored_form_is_normalised(rig, admin_pin):
    client, root = rig
    r = client.post("/api/settings", json={"station_profile": " Optical ", "admin_pin": admin_pin}).json()
    assert r["ok"] is True and r["persisted"]["station_profile"] == "optical"


@pytest.mark.parametrize("bad", ["optic", "STM2", "", "none"])
def test_a_misspelt_profile_is_refused_not_stored(rig, admin_pin, bad):
    """With the PIN right, so it is the value that is refused and not the missing PIN."""
    client, root = rig
    r = client.post("/api/settings", json={"station_profile": bad, "admin_pin": admin_pin}).json()
    assert r["pin_required"] is False
    assert r["ok"] is False and "station_profile" in r["rejected"]
    assert r["persisted"] == {}
    assert not (default_config_dir(root) / "ui_settings.json").exists(), "a refused write leaves no bytes"


def test_health_reports_the_profile_in_force_and_whether_a_restart_is_pending(rig, admin_pin):
    client, _ = rig
    h = client.get("/api/health").json()
    assert (h["station_profile"], h["station_profile_source"], h["station_restart_pending"]) == (
        "stm", "default", False)
    station.note_boot()                                         # the service has started as stm
    client.post("/api/settings", json={"station_profile": "optical", "admin_pin": admin_pin})
    h = client.get("/api/health").json()
    assert h["station_profile"] == "stm", "the UI must keep matching the subsystems that are running"
    assert h["station_restart_pending"] is True and h["station_profile_source"] == "settings"


def test_health_says_when_the_environment_pins_the_profile(rig, set_profile):
    client, _ = rig
    set_profile("optical")
    h = client.get("/api/health").json()
    assert (h["station_profile"], h["station_profile_source"], h["station_restart_pending"]) == (
        "optical", "env", False)


def test_the_generated_frontend_types_know_the_new_fields():
    """``frontend/openapi.json`` is what the TS types are generated from; a stale one makes the
    fields ``undefined`` at run time while ``tsc`` stays green."""
    spec = json.loads((_REPO / "frontend" / "openapi.json").read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    for model in ("HealthResponse", "SettingsResponse"):
        assert "station_profile" in schemas[model]["properties"], model
    assert {"station_profile_source", "station_restart_pending"} <= set(
        schemas["HealthResponse"]["properties"])
    write = [s for n, s in schemas.items() if n.startswith("SettingsWriteRequest")]
    assert write and all("station_profile" in s["properties"] for s in write)
