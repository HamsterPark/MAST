"""Fixtures for the station-profile tests (docs/v2/design/optical_station_witec.md D1).

The profile is read from the environment variable ``MAST_STATION_PROFILE`` first, so
a test selects a profile with ``monkeypatch.setenv`` and nothing else has to be touched.
``station.reset()`` drops the boot latch and the warned-once memory around each test.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _station_is_reset(monkeypatch):
    """No profile leaks in from the environment of the machine running the suite, and none
    leaks out of a test."""
    from mast import station

    monkeypatch.delenv(station.ENV_VAR, raising=False)
    station.reset()
    yield
    station.reset()


@pytest.fixture()
def set_profile(monkeypatch):
    """``set_profile("optical")`` / ``set_profile(None)`` for the rest of the test."""
    from mast import station

    def _set(name):
        if name is None:
            monkeypatch.delenv(station.ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(station.ENV_VAR, name)
    return _set


@pytest.fixture()
def admin_pin(tmp_path, monkeypatch):
    """An admin PIN set in the isolated project root; returns the PIN.

    ``station_profile`` is in ``admin_pin.GUARDED_KEYS``: a write that carries it is refused
    unless the request brings the PIN and the machine has one. The PIN file lives under
    ``project_root()``, so the root is pointed at ``tmp_path`` here as well (the settings file is
    written under the same root by the tests that use it)."""
    import hashlib

    from mast.webui.settings_store import default_config_dir

    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    pin = "4321"
    cfg = default_config_dir(tmp_path)
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "admin_pin.txt").write_text(hashlib.sha256(pin.encode("utf-8")).hexdigest(), encoding="utf-8")
    return pin


@pytest.fixture()
def unbound_tool_face():
    """The station gate reads the registry bound to it; undo whatever a test binds."""
    from mast.skills import station_face

    station_face.bind_registry(None)
    yield station_face
    station_face.bind_registry(None)
