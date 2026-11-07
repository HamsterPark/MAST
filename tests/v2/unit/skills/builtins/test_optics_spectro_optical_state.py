"""Every AcquireSpectrum sidecar carries the manual optical state the operator registered.

The point of the registry (design D9): which λ/2 was installed, the analyzer angle, the objective,
whether the laser area was signed off — none of it readable by software — travels with every
spectrum, so the record no longer depends on someone remembering to type it into a file name.

That is the optical station's (``mast.station``). The TERS bench of an STM station writes the
sidecar it always wrote: no ``optical_state`` key, not even an empty one, and the registry is not
read at all. The tests below run as the optical station; the last ones pin the STM
side.
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (same bootstrap as test_wrap_skill_minimal) ──
import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[5] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import json
import logging
from datetime import datetime

import pytest

from mast.operator_hitl import optical_state, service
from mast.skills.builtins.optics_spectro import AcquireSpectrum

# the fixtures of the existing AcquireSpectrum tests (an active sample in a temp experiment
# folder, the simulated CCD, a context that answers the STM reads)
from tests.v2.unit.skills.builtins.test_optics_spectro import (  # noqa: F401
    Ctx,
    FakeStorage,
    scope,
    sim_bench,
)


@pytest.fixture(autouse=True)
def _fresh_store():
    old = optical_state.set_store(optical_state.OpticalStateStore())
    yield
    optical_state.set_store(old)


@pytest.fixture(autouse=True)
def _optical_station(monkeypatch):
    """These tests are about the optical station; the STM ones set their own profile."""
    monkeypatch.setenv("MAST_STATION_PROFILE", "optical")


def acquire(**extra):
    res = AcquireSpectrum().execute(Ctx(), {"exposure_ms": 50.0, "frames": 1, "sample_current": False, **extra})
    assert res.success, res.error
    return res, json.loads(Path(res.data["sidecar_path"]).read_text(encoding="utf-8"))


def test_the_sidecar_carries_the_registered_optical_state(sim_bench, scope):
    store = optical_state.get_store()
    store.set_by_operator("excitation_waveplate", "hwp_1064", by="alice", source="web", note="swapped")
    store.set_by_operator("incident_polarizer_deg", 45, by="alice", source="launcher")
    store.set_by_operator("laser_area_safe", True, by="alice", source="web")
    _, meta = acquire()
    state = meta["optical_state"]
    assert state == json.loads(json.dumps(store.snapshot())), "exactly the registry's snapshot"
    assert state["excitation_waveplate"]["value"] == "hwp_1064"
    assert state["excitation_waveplate"]["set_by"] == "alice" and state["excitation_waveplate"]["valid"] is True
    assert state["incident_polarizer_deg"]["value"] == 45.0
    assert state["laser_area_safe"]["valid"] is True and state["laser_area_safe"]["valid_until"]
    assert "optical_state_error" not in meta
    # nothing else of the sidecar moved
    assert meta["schema"] == "mast.spectrum/1" and "optics" in meta and meta["scope"]["experiment_id"] == "e1"


def test_the_sidecar_does_not_carry_where_a_value_was_written_from(sim_bench, scope):
    """The operator API records the ``client`` of each write (peer address, user agent ...) for
    the operator to check. A spectrum sidecar travels with the data: it never has that."""
    store = optical_state.get_store()
    client = {"peer": "192.0.2.77", "user_agent": "Mozilla/5.0 (secret-agent-string)", "browser": True,
              "auth": "basic", "auth_enforced": True, "host": "192.0.2.1:7862"}
    store.set_by_operator("objective", "50x", by="alice", source="web", client=client)
    store.set_by_operator("laser_area_safe", True, by="alice", source="web", client=client)
    assert store.get("objective").client == client, "recorded ..."
    res, meta = acquire()
    text = Path(res.data["sidecar_path"]).read_text(encoding="utf-8")
    assert "192.0.2.77" not in text and "secret-agent-string" not in text and '"client"' not in text, \
        "... but not in the sidecar"
    assert meta["optical_state"]["objective"]["value"] == "50x"


def test_every_defined_slot_is_in_the_sidecar_even_when_unset(sim_bench, scope):
    _, meta = acquire()
    assert set(meta["optical_state"]) == {s.id for s in optical_state.get_store().slots()}
    assert meta["optical_state"]["objective"] == {"value": None, "valid": False}


def test_an_expired_attestation_is_recorded_as_such(sim_bench, scope):
    store = optical_state.get_store()
    store.set_by_operator("laser_area_safe", True, by="alice", source="web")
    store._values["laser_area_safe"].valid_until = "2000-01-01T00:00:00+00:00"
    _, meta = acquire()
    assert meta["optical_state"]["laser_area_safe"]["value"] is True
    assert meta["optical_state"]["laser_area_safe"]["valid"] is False, "the spectrum must not claim a lapsed sign-off"


def test_a_change_during_the_exposure_shows_in_the_slots_timestamp(sim_bench, scope):
    """The snapshot is taken when the sidecar is written, i.e. right after the exposure; a slot
    changed meanwhile carries a ``set_at`` inside the acquisition's time window."""
    spec = sim_bench.spectrometer("sim_ccd")
    real = spec.acquire

    def acquire_and_swap(*a, **kw):
        out = real(*a, **kw)
        optical_state.get_store().set_by_operator("objective", "50x", by="bob", source="web")
        return out

    spec.acquire = acquire_and_swap
    _, meta = acquire()
    slot = meta["optical_state"]["objective"]
    started = datetime.fromisoformat(meta["time"]["started_utc"]).replace(microsecond=0)
    assert slot["value"] == "50x" and datetime.fromisoformat(slot["set_at"]) >= started


def test_an_unreadable_registry_is_said_not_hidden(sim_bench, scope, caplog):
    class Broken(optical_state.OpticalStateStore):
        def snapshot(self):
            raise RuntimeError("state file locked")

    optical_state.set_store(Broken())
    with caplog.at_level(logging.WARNING):
        res, meta = acquire()
    assert res.success, "the spectrum is still taken and saved"
    assert meta["optical_state"] is None
    assert meta["optical_state_error"] == "RuntimeError: state file locked"
    assert any("not recorded in the spectrum sidecar" in r.getMessage() for r in caplog.records)


def test_a_dry_run_writes_no_sidecar_and_does_not_need_the_registry(sim_bench, scope):
    class Broken(optical_state.OpticalStateStore):
        def snapshot(self):
            raise RuntimeError("never read for a dry run")

    optical_state.set_store(Broken())
    res = AcquireSpectrum().execute(Ctx(), {"exposure_ms": 50.0, "dry_run": True})
    assert res.success and res.data["dry_run"] and "sidecar_path" not in res.data


def test_the_persistent_registry_feeds_the_sidecar_across_a_restart(sim_bench, scope, tmp_path, monkeypatch):
    root = tmp_path / "opt-root"
    first = service.install(root)
    try:
        first.store.set_by_operator("analyzer_optic", "polarizer", by="alice", source="web")
    finally:
        service.uninstall()
    service.install(root)                           # the service restarted
    try:
        _, meta = acquire()
    finally:
        service.uninstall()
    assert meta["optical_state"]["analyzer_optic"]["value"] == "polarizer"
    assert meta["optical_state"]["analyzer_optic"]["set_by"] == "alice"


# ── the TERS (STM) station's sidecar is what it was ─────────────────────────────────────────


def test_an_stm_stations_sidecar_has_no_optical_state_at_all(sim_bench, scope, monkeypatch):
    monkeypatch.setenv("MAST_STATION_PROFILE", "stm")
    store = optical_state.get_store()
    store.set_by_operator("laser_area_safe", True, by="alice", source="web")      # signed, but not this bench's
    store.set_by_operator("objective", "50x", by="alice", source="web")
    res, meta = acquire()
    text = Path(res.data["sidecar_path"]).read_text(encoding="utf-8")
    assert "optical_state" not in meta and "optical_state_error" not in meta
    assert "optical_state" not in text and "laser_area_safe" not in text
    # nothing else of the sidecar moved
    assert meta["schema"] == "mast.spectrum/1" and "optics" in meta and meta["scope"]["experiment_id"] == "e1"


def test_an_stm_stations_sidecar_does_not_read_the_registry(sim_bench, scope, monkeypatch):
    """Not an empty section and not an error either: the registry is simply not consulted."""
    monkeypatch.setenv("MAST_STATION_PROFILE", "stm")

    class MustNotBeRead(optical_state.OpticalStateStore):
        def snapshot(self, include_client=False):
            raise AssertionError("the TERS bench's sidecar read the optical-state registry")

    optical_state.set_store(MustNotBeRead())
    res, meta = acquire()
    assert res.success
    assert "optical_state" not in meta and "optical_state_error" not in meta


@pytest.mark.parametrize("profile", ["stm", "", "nonsense"])
def test_anything_but_the_optical_profile_writes_the_old_sidecar(sim_bench, scope, monkeypatch, profile):
    monkeypatch.setenv("MAST_STATION_PROFILE", profile)
    _, meta = acquire()
    assert "optical_state" not in meta and "optical_state_error" not in meta


def test_an_unreadable_profile_is_an_stm_sidecar(sim_bench, scope, monkeypatch):
    from mast import station

    def boom():
        raise RuntimeError("settings store is gone")

    monkeypatch.setattr(station, "is_optical", boom)
    _, meta = acquire()
    assert "optical_state" not in meta and "optical_state_error" not in meta
