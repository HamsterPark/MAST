"""pytest fixtures shared by the TERS composite tests (import them into the test module)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.v2.unit.skills.composite._ters_harness import (
    SIM_CCD,
    FakeStm,
    OkGuard,
    make_ctx,
    write_bench,
)


class FakeStorage:
    def __init__(self):
        self.exp = {"e1": {"name": "TERS test", "start_time": "2026-10-20T10:00:00",
                           "dir_name": ""}}
        self.smp = {"s1": {"name": "Au tip", "sample_index": 1, "dir_name": "",
                           "description": "", "sample_type": "", "sample_subtype": "",
                           "start_time": "2026-10-20T10:01:00"}}

    def get_experiment(self, eid):
        return dict(self.exp[eid]) if eid in self.exp else None

    def get_sample(self, sid):
        return dict(self.smp[sid]) if sid in self.smp else None

    def sample_ordinal(self, sid):
        return 1

    def set_experiment_dir_name(self, eid, name):
        self.exp[eid]["dir_name"] = name

    def set_sample_dir_name(self, sid, name, idx):
        self.smp[sid]["dir_name"] = name

    def get_samples(self, eid):
        return [dict(v, id=k) for k, v in self.smp.items()]


@pytest.fixture
def scope(tmp_path, monkeypatch):
    """Active experiment/sample under tmp + the data root redirected to tmp."""
    from mast.logging import experiment_log as el

    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    storage = FakeStorage()
    log = SimpleNamespace(_storage=storage, current_experiment_id="e1", current_sample_id="s1")
    prev = el.get_active_log()
    el.set_active_log(log)
    yield SimpleNamespace(storage=storage, log=log, root=tmp_path)
    el.set_active_log(prev)


@pytest.fixture
def ters_env(scope, monkeypatch):
    """LiveHub (strict), port guard OK, short preflight window, no HITL channel, and this "rig"
    declares z_extend_sign = -1 — FakeStm's default orientation (extension lowers Z, like Mimea)."""
    from mast.core import instrument_profile, port_guard
    from mast.live.testing import LiveRecorder, isolated_live_hub
    from mast.skills.composite import ters_common

    monkeypatch.setattr(ters_common, "PREFLIGHT_STABLE_S", 0.3)
    monkeypatch.setattr(ters_common, "human_channel", lambda payload: None)
    ters_common._SESSION_DARKS.clear()
    saved_profile = instrument_profile.get_profile()          # process-global: put it back after
    instrument_profile.set_profile({**saved_profile, "z_extend_sign": "-1"})
    guard = OkGuard()
    port_guard.set_port_guard(guard)
    try:
        with isolated_live_hub() as hub, LiveRecorder(hub) as rec:
            yield SimpleNamespace(hub=hub, rec=rec, guard=guard, scope=scope)
    finally:
        port_guard.set_port_guard(None)
        instrument_profile.set_profile(saved_profile)


@pytest.fixture
def sim_ccd(tmp_path):
    from mast.instruments.registry import reset_instrument_registry

    reg = write_bench(tmp_path, [dict(SIM_CCD)])
    yield reg
    reset_instrument_registry(None)


@pytest.fixture
def stm():
    return FakeStm()


@pytest.fixture
def ctx(stm):
    return make_ctx(stm)


#: fast CP-B / CP-A settings for unit tests
FAST = {"settle_s": 0.0, "stable_window_s": 0.15, "stable_timeout_s": 3.0,
        "exposure_ms": 50.0, "laser_state": "attested_on", "laser_power_uW": 10.0}
