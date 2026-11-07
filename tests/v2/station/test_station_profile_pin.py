"""The station profile is an admin-PIN setting.

``POST /api/settings {"station_profile": "optical"}`` used to be accepted from anyone who could
reach the API, with no PIN, and the settings page saved it the moment a radio button was
clicked. The choice decides at the *next start* what this process is:

* an STM station set to ``optical`` dials no Nanonis, and starts neither the tip-crash watchdog
  nor the current monitor;
* an optical station set to ``stm`` brings serial auto-detection back (a ``*IDN?`` and a Modbus
  frame to every idle port) and no longer installs the operator prompts and the laser
  revocation listener.

A mis-click costs nothing until the restart, which is exactly why the write is where it has to be
stopped. So ``station_profile`` is in ``admin_pin.GUARDED_KEYS``, with the same rules as the other
guarded keys: no PIN set means refused (fail-closed), a wrong or empty PIN means refused, and the
whole write is refused, not just the key.

``test_G10_*`` asserts the refusal of the write that used to be accepted unguarded
(``ok`` and no ``pin_required``).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast import station
from mast.api.admin_pin import GUARDED_KEYS
from mast.api.context import AppContext
from mast.api.routes import settings_admin_write as saw
from mast.api.routes.settings import router as read_router
from mast.webui.settings_store import SettingsStore, default_config_dir, set_process_store


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """The settings routes over a real store in an isolated root. No PIN is set: the tests that
    need one take the ``admin_pin`` fixture (same root)."""
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    app = FastAPI()
    app.state.ctx = AppContext(user_root=str(tmp_path))
    app.include_router(read_router, prefix="/api")
    app.include_router(saw.router, prefix="/api")
    set_process_store(app.state.ctx.settings_store)
    return TestClient(app), tmp_path


def _stored(root):
    return SettingsStore(default_config_dir(root)).get("station_profile")


def _no_settings_file(root):
    return not (default_config_dir(root) / "ui_settings.json").exists()


# ── the key is guarded ──────────────────────────────────────────────────────────
def test_the_station_profile_is_a_guarded_key():
    assert "station_profile" in GUARDED_KEYS


# ── the formerly unguarded write, now refused ──────────────────────────────────────
def test_G10_the_write_is_refused_without_the_admin_pin_and_nothing_is_stored(tmp_path):
    """A bare app, a store, no PIN anywhere: the write is refused, says why, and writes not a
    byte."""
    store = SettingsStore(tmp_path / "cfg")
    app = FastAPI()
    app.state.ctx = SimpleNamespace(settings_store=store, config=None, runtime=None, live_app=None, app=None)
    app.include_router(saw.router, prefix="/api")
    r = TestClient(app).post("/api/settings", json={"station_profile": "optical"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False and body["pin_required"] is True, body
    assert store.get("station_profile") is None
    assert not (tmp_path / "cfg" / "ui_settings.json").exists(), "a refused write leaves no bytes"


def test_a_machine_with_no_pin_cannot_change_the_profile_and_is_told_to_set_one(rig):
    client, root = rig
    body = client.post("/api/settings", json={"station_profile": "optical", "admin_pin": "1234"}).json()
    assert (body["ok"], body["pin_required"], body["pin_reason"]) == (False, True, "no_pin_set")
    note = body["rebuild_note"]
    assert "PIN" in note and ("站点画像" in note or "station profile" in note), note
    assert _stored(root) is None and _no_settings_file(root)


@pytest.mark.parametrize("sent,reason", [("0000", "wrong"), ("", "empty"), ("   ", "empty"), (None, "empty")])
def test_a_wrong_or_missing_pin_is_refused(rig, admin_pin, sent, reason):
    client, root = rig
    payload = {"station_profile": "optical"}
    if sent is not None:
        payload["admin_pin"] = sent
    body = client.post("/api/settings", json=payload).json()
    assert (body["ok"], body["pin_required"], body["pin_reason"]) == (False, True, reason), body
    assert body["persisted"] == {}
    assert _stored(root) is None and _no_settings_file(root)


def test_the_right_pin_lets_it_through_and_the_pin_itself_is_not_stored(rig, admin_pin):
    client, root = rig
    body = client.post("/api/settings", json={"station_profile": "optical", "admin_pin": admin_pin}).json()
    assert body["ok"] is True and body["pin_required"] is False and body["rejected"] == {}
    assert body["persisted"]["station_profile"] == "optical"
    assert _stored(root) == "optical"
    on_disk = (default_config_dir(root) / "ui_settings.json").read_text(encoding="utf-8")
    assert "admin_pin" not in on_disk and admin_pin not in json.loads(on_disk).values()


def test_both_directions_are_guarded(rig, admin_pin):
    """Setting it back to ``stm`` is as dangerous as setting ``optical`` (serial auto-detection
    comes back, the operator prompts go away)."""
    client, root = rig
    assert client.post("/api/settings", json={"station_profile": "optical", "admin_pin": admin_pin}).json()["ok"]
    body = client.post("/api/settings", json={"station_profile": "stm"}).json()
    assert body["pin_required"] is True and body["ok"] is False
    assert _stored(root) == "optical", "the refused write must leave the stored profile alone"
    back = client.post("/api/settings", json={"station_profile": "stm", "admin_pin": admin_pin}).json()
    assert back["ok"] is True and _stored(root) == "stm"


def test_a_refusal_takes_the_other_keys_of_the_same_write_with_it(rig):
    client, root = rig
    body = client.post("/api/settings", json={"station_profile": "optical", "theme": "Light"}).json()
    assert body["pin_required"] is True and body["ok"] is False
    assert SettingsStore(default_config_dir(root)).load() == {}


def test_a_write_that_does_not_carry_the_profile_needs_no_pin(rig):
    """Only the guarded keys cost a PIN: an appearance setting is saved as before."""
    client, root = rig
    body = client.post("/api/settings", json={"theme": "Light"}).json()
    assert body["ok"] is True and body["pin_required"] is False
    assert SettingsStore(default_config_dir(root)).get("theme") == "Light"


def test_the_pin_is_checked_before_the_value(rig, admin_pin):
    """A misspelt value without the PIN is a PIN problem; with the PIN it is a value problem.
    Either way nothing is stored."""
    client, root = rig
    no_pin = client.post("/api/settings", json={"station_profile": "optic"}).json()
    assert no_pin["pin_required"] is True and no_pin["rejected"] == {}
    with_pin = client.post("/api/settings", json={"station_profile": "optic", "admin_pin": admin_pin}).json()
    assert with_pin["pin_required"] is False and "station_profile" in with_pin["rejected"]
    assert _stored(root) is None


def test_a_refused_write_does_not_move_the_configured_profile(rig):
    client, _ = rig
    assert station.configured_profile() == "stm"
    client.post("/api/settings", json={"station_profile": "optical"})
    assert station.configured_profile() == "stm" and station.restart_pending() is False


def test_the_environment_variable_is_not_an_http_write(rig, set_profile):
    """The PIN guards the settings route. The environment variable is the operator's own, outside
    the HTTP surface, and it is how a fresh machine with no PIN yet gets set to optical."""
    set_profile("optical")
    assert station.configured_profile() == "optical" and station.resolve().source == "env"
