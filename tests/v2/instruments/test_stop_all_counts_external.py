"""A stop of the whole optical station counts as an external stop on every station device it stops.

``InstrumentRegistry.stop_all`` (StopOpticalStage without a device_id, the E-STOP and shutdown hooks, a TERS
composite's cleanup) used to stop the station without ``note_external_stop``: a protocol running on it never saw
that somebody had stopped it, and opened its shutter again at the next step (found by protocols with the real
skill: ``test_stop_optical_stage_without_a_device_id_ends_a_running_protocol``). The counters move BEFORE any stop
is sent, because the run checks them in front of every hardware call.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mast.instruments.registry import InstrumentRegistry


def _reg(tmp_path, devices):
    p = tmp_path / "cfg" / "optical_instruments.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"devices": devices}), encoding="utf-8")
    return InstrumentRegistry(config_path=p)


@pytest.fixture
def station(tmp_path):
    reg = _reg(tmp_path, [
        {"id": "las", "type": "sim_laser"},
        {"id": "tun", "type": "sim_tunable_laser"},
        {"id": "witec", "type": "sim_microscope"},
    ])
    # built in this process, as a running protocol would have them
    devs = {"las": reg.laser("las"), "tun": reg.laser("tun"), "witec": reg.microscope("witec")}
    yield reg, devs
    reg.close_all()


def _epochs(devs):
    return {k: int(getattr(d, "external_stop_epoch", 0) or 0) for k, d in devs.items()}


def test_every_station_device_counts_the_stop_before_it_is_stopped(station, monkeypatch):
    reg, devs = station
    before = _epochs(devs)
    seen: dict[str, int] = {}

    def watch(key, dev, name):
        orig = getattr(dev, name)

        def wrapped(*a, **kw):
            seen[key] = int(getattr(dev, "external_stop_epoch", 0) or 0)   # the counter at the moment of the stop
            return orig(*a, **kw)
        monkeypatch.setattr(dev, name, wrapped)

    watch("las", devs["las"], "emission_off")
    watch("tun", devs["tun"], "emission_off")
    watch("witec", devs["witec"], "stop_all")

    rep = reg.stop_all(wait_s=5.0, reason="StopOpticalStage (ext:bob)")

    assert sorted(rep["stopped"]) == ["las", "tun", "witec"], rep
    assert _epochs(devs) == {k: v + 1 for k, v in before.items()}
    assert seen == {k: v + 1 for k, v in before.items()}, "a device was stopped before its counter moved"
    assert {k: d.external_stop_reason for k, d in devs.items()} == dict.fromkeys(devs, "StopOpticalStage (ext:bob)")


def test_without_a_reason_it_says_the_whole_station_was_stopped(station):
    reg, devs = station
    reg.stop_all(wait_s=5.0)
    for d in devs.values():
        assert "whole optical station" in d.external_stop_reason


def test_a_count_that_fails_does_not_keep_the_stop_from_being_sent(station, monkeypatch):
    reg, devs = station

    def boom(reason):
        raise RuntimeError("counter broken")
    monkeypatch.setattr(devs["las"], "note_external_stop", boom)
    rep = reg.stop_all(wait_s=5.0, reason="test")
    assert sorted(rep["stopped"]) == ["las", "tun", "witec"], rep     # "las" too: its stop was still sent
    assert devs["tun"].external_stop_reason == "test" and devs["witec"].external_stop_reason == "test"


def test_stop_optical_stage_without_a_device_id_names_its_caller(station, monkeypatch):
    from mast.skills.builtins import optics_stage

    reg, devs = station
    monkeypatch.setattr(optics_stage, "get_instrument_registry", lambda: reg)
    res = optics_stage.StopOpticalStage().execute(SimpleNamespace(owner="ext:bob"), {})
    assert res.success, res
    assert {d.external_stop_reason for d in devs.values()} == {"StopOpticalStage (ext:bob)"}
