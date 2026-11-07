"""safe_off_all: the reflex when the operator withdraws the laser-area attestation."""

from __future__ import annotations

import json

import pytest

from mast.instruments import emission_safety
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
        {"id": "spare", "type": "sim_laser", "enabled": False},
    ])
    yield reg
    reg.close_all()


def test_every_enabled_laser_goes_dark_and_every_shutter_closes(station):
    las, tun, mic = station.laser("las"), station.laser("tun"), station.microscope("witec")
    las.emission_on()
    tun.emission_on()
    mic.set_shutter(True)
    assert las.emission_state() and tun.emission_state() and mic.shutter_open

    res = emission_safety.safe_off_all("operator recorded the laser area as not safe", registry=station)

    assert res["all_confirmed"] is True
    assert sorted(d["device_id"] for d in res["devices"]) == ["las", "tun", "witec"]
    assert las.emission_state() is False and tun.emission_state() is False
    assert mic.shutter_open is False
    # a disabled entry is not touched, not even built
    assert "spare" not in station.built_station_devices()


def test_a_device_never_built_in_this_process_is_switched_off_too(station):
    # e.g. a laser an earlier MAST process left on: nothing has built it since the restart
    assert "las" not in station.built_station_devices()
    res = emission_safety.safe_off_all(registry=station)
    assert "las" in station.built_station_devices()
    assert {d["device_id"]: d["confirmed"] for d in res["devices"]}["las"] is True


def test_one_device_failing_does_not_stop_the_others(station, monkeypatch):
    real_laser = station.laser

    def laser(dev_id):
        if dev_id == "las":
            raise OSError("COM13 is held by Rainbow OEM")
        return real_laser(dev_id)

    monkeypatch.setattr(station, "laser", laser)
    tun = real_laser("tun")
    tun.emission_on()
    res = emission_safety.safe_off_all(registry=station)
    by_id = {d["device_id"]: d for d in res["devices"]}
    assert by_id["las"]["confirmed"] is False and "COM13" in by_id["las"]["cause"]
    assert by_id["tun"]["confirmed"] is True and tun.emission_state() is False
    assert by_id["witec"]["confirmed"] is True
    assert res["all_confirmed"] is False


def test_an_unconfirmed_read_back_is_reported_as_such(station, monkeypatch):
    las = station.laser("las")
    monkeypatch.setattr(las, "emission_off", lambda: None)      # sent, but no read-back
    res = emission_safety.safe_off_all(registry=station)
    assert {d["device_id"]: d["confirmed"] for d in res["devices"]}["las"] is False
    assert res["all_confirmed"] is False


def test_it_never_raises_even_without_a_readable_config():
    class Broken:
        def find_devices(self, **_kw):
            raise OSError("config unreadable")

    res = emission_safety.safe_off_all("x", registry=Broken())
    assert res["all_confirmed"] is False and "config unreadable" in res["error"]


def test_the_background_variant_reports_back(station):
    station.laser("las").emission_on()
    got: list[dict] = []
    t = emission_safety.safe_off_all_in_background("bg", registry=station, on_done=got.append)
    t.join(10)
    assert not t.is_alive()
    assert got and got[0]["all_confirmed"] is True and got[0]["reason"] == "bg"
    assert station.laser("las").emission_state() is False


# ── the operator withdraws laser_area_safe: the write hook takes everything dark ──

from mast.operator_hitl import optical_state  # noqa: E402


@pytest.fixture
def fresh_store(monkeypatch):
    prev = optical_state.get_store()
    store = optical_state.OpticalStateStore()
    optical_state.set_store(store)
    yield store
    optical_state.set_store(prev)


@pytest.fixture
def offs(monkeypatch):
    """Record what the listener would switch off, synchronously."""
    calls: list[str] = []

    def fake(reason="", *, registry=None, on_done=None):
        calls.append(reason)

    monkeypatch.setattr(emission_safety, "safe_off_all_in_background", fake)
    return calls


def test_recording_the_area_as_not_safe_switches_everything_off(fresh_store, offs):
    emission_safety.install_revocation_listener(fresh_store)
    fresh_store.set_by_operator("laser_area_safe", True, by="ana", source="web")
    assert offs == []                                       # signing does nothing
    fresh_store.set_by_operator("laser_area_safe", False, by="ana", source="web")
    assert len(offs) == 1 and "False" in offs[0] and "ana" in offs[0]


def test_clearing_the_attestation_switches_everything_off(fresh_store, offs):
    emission_safety.install_revocation_listener(fresh_store)
    fresh_store.set_by_operator("laser_area_safe", True, by="ana", source="web")
    fresh_store.clear_by_operator("laser_area_safe", by="ana", source="web")
    assert len(offs) == 1 and "cleared" in offs[0]


def test_other_slots_and_a_re_signed_area_do_nothing(fresh_store, offs):
    listener = emission_safety.install_revocation_listener(fresh_store)
    fresh_store.set_by_operator("objective", "50x", by="ana", source="web")
    assert offs == []
    # a False record arriving after the area was signed again (concurrent writes): the store says
    # it is safe now, so nothing is switched off
    fresh_store.set_by_operator("laser_area_safe", True, by="ana", source="web")
    listener("laser_area_safe", {"value": False, "set_by": "late"})
    assert offs == []


def test_the_revocation_really_switches_a_sim_laser_off(fresh_store, station):
    las = station.laser("las")
    las.emission_on()
    emission_safety.install_revocation_listener(fresh_store, registry=station)
    fresh_store.set_by_operator("laser_area_safe", True, by="ana", source="web")
    fresh_store.set_by_operator("laser_area_safe", False, by="ana", source="web")
    for t in list(__import__("threading").enumerate()):
        if t.name == "mast-emission-safe-off":
            t.join(10)
    assert las.emission_state() is False


def test_the_runtime_wires_the_listener_after_installing_the_operator_service(fresh_store, monkeypatch):
    from mast.core.runtime import CoreRuntime
    from mast.operator_hitl import service

    monkeypatch.setattr(service, "install", lambda *a, **k: None)
    rt = CoreRuntime.__new__(CoreRuntime)
    rt._install_operator_hitl()
    names = [getattr(fn, "__name__", "") for fn in fresh_store.listeners]
    assert "laser_area_revocation" in names


# ── registry.stop_all must not report an unconfirmed stop as "stopped" ──

def test_stop_all_reports_an_unconfirmed_laser_and_controller(tmp_path, monkeypatch):
    reg = _reg(tmp_path, [{"id": "las", "type": "sim_laser"}, {"id": "ok", "type": "sim_laser"},
                          {"id": "tc", "type": "sim_temperature"}])
    try:
        las, ok, tc = reg.laser("las"), reg.laser("ok"), reg.temperature_controller("tc")
        monkeypatch.setattr(las, "emission_off", lambda: None)      # sent, no read-back
        monkeypatch.setattr(tc, "stop", lambda: False)              # stop not confirmed
        rep = reg.stop_all(wait_s=5.0)
        assert "ok" in rep["stopped"]
        assert "las" not in rep["stopped"] and "las" in rep["unconfirmed"]
        assert "tc" not in rep["stopped"] and "tc" in rep["unconfirmed"]
        assert rep["pending"] == []
    finally:
        reg.close_all()


# ── a WITec: every laser's shutter, not only the selected one ──

def test_a_microscope_with_close_all_shutters_gets_every_shutter_closed(station, monkeypatch):
    mic = station.microscope("witec")
    calls: list[str] = []
    monkeypatch.setattr(mic, "stop_all", lambda: calls.append("stop_all"))
    monkeypatch.setattr(mic, "close_all_shutters", lambda: calls.append("close_all") or {"closed": ["532", "633"]},
                        raising=False)
    monkeypatch.setattr(mic, "set_shutter", lambda open_: calls.append("set_shutter") or False)
    res = emission_safety.safe_off_all(registry=station)
    item = {d["device_id"]: d for d in res["devices"]}["witec"]
    assert calls == ["stop_all", "close_all"]                 # the sequence stopped first, then all shutters
    assert item["action"] == "close_all_shutters" and item["confirmed"] is True
    assert item["report"] == {"closed": ["532", "633"]}


def test_a_shutter_that_cannot_be_closed_is_reported_with_its_report(station, monkeypatch):
    from mast.instruments.base import InstrumentError

    class NotClosed(InstrumentError):
        def __init__(self, msg, report):
            super().__init__(msg)
            self.report = report

    mic = station.microscope("witec")

    def refuse():
        raise NotClosed("633 NOT closed: no write access", {"failed": ["633"]})

    monkeypatch.setattr(mic, "close_all_shutters", refuse, raising=False)
    res = emission_safety.safe_off_all(registry=station)
    item = {d["device_id"]: d for d in res["devices"]}["witec"]
    assert item["confirmed"] is False and "NOT closed" in item["cause"]
    assert item["report"] == {"failed": ["633"]}
    assert res["all_confirmed"] is False


# ── an unconfirmed safe-off must not stay in the log only ──

def test_the_last_result_is_kept_when_something_was_touched(station, monkeypatch):
    monkeypatch.setattr(emission_safety, "_LAST", None)
    las = station.laser("las")
    monkeypatch.setattr(las, "emission_off", lambda: None)          # not confirmed
    emission_safety.safe_off_all("E-STOP: test", registry=station)
    last = emission_safety.last_result()
    assert last is not None and last["all_confirmed"] is False and last["reason"] == "E-STOP: test"
    assert last["at"]


def test_a_station_without_lasers_or_microscopes_keeps_no_result(tmp_path, monkeypatch):
    monkeypatch.setattr(emission_safety, "_LAST", None)
    reg = _reg(tmp_path, [])
    try:
        emission_safety.safe_off_all("E-STOP", registry=reg)
        assert emission_safety.last_result() is None
    finally:
        reg.close_all()


def test_stop_all_reads_a_microscope_stop_report(station, monkeypatch):
    mic = station.microscope("witec")
    monkeypatch.setattr(mic, "last_stop_report", {"shutter_confirmed": False, "shutter": "not closed: 633"},
                        raising=False)
    rep = station.stop_all(wait_s=5.0)
    assert "witec" in rep["unconfirmed"] and "witec" not in rep["stopped"]


def test_the_optical_briefing_flags_an_unconfirmed_safe_off(monkeypatch):
    from mast import station_view
    from mast.api.ext import overview

    monkeypatch.setattr(station_view, "inventory", lambda: {"devices": []})
    monkeypatch.setattr(emission_safety, "_LAST", {
        "reason": "E-STOP: operator", "at": "2026-11-03T14:00:00+00:00", "all_confirmed": False,
        "devices": [{"device_id": "witec", "action": "close_all_shutters", "confirmed": False,
                     "cause": "ShutterNotClosed: 633 NOT closed"}]})
    sec = overview._sec_station(None, None)
    assert "witec" in sec["text"] and "633 NOT closed" in sec["text"]
    assert sec["data"]["last_safe_off"]["all_confirmed"] is False
    monkeypatch.setattr(emission_safety, "_LAST", dict(emission_safety._LAST, all_confirmed=True))
    assert "witec:" not in overview._sec_station(None, None)["text"]


# ── a driver report decides "confirmed": every shutter, every laser ──

def test_a_report_that_only_checked_the_selected_laser_is_not_confirmed(station, monkeypatch):
    mic = station.microscope("witec")
    monkeypatch.setattr(mic, "close_all_shutters",
                        lambda: {"shutter_confirmed": True, "scope": "selected_laser_only"}, raising=False)
    item = {d["device_id"]: d for d in emission_safety.safe_off_all(registry=station)["devices"]}["witec"]
    assert item["confirmed"] is False and "only the selected laser" in item["cause"]


def test_a_report_saying_not_confirmed_is_not_confirmed(station, monkeypatch):
    mic = station.microscope("witec")
    monkeypatch.setattr(mic, "close_all_shutters",
                        lambda: {"shutter_confirmed": False, "scope": "all_lasers", "shutter": "not closed: 633"},
                        raising=False)
    item = {d["device_id"]: d for d in emission_safety.safe_off_all(registry=station)["devices"]}["witec"]
    assert item["confirmed"] is False and "633" in item["cause"]


def test_stop_all_counts_a_selected_only_report_as_unconfirmed(station, monkeypatch):
    mic = station.microscope("witec")
    monkeypatch.setattr(mic, "last_stop_report", {"shutter_confirmed": True, "scope": "selected_laser_only"},
                        raising=False)
    rep = station.stop_all(wait_s=5.0)
    assert "witec" in rep["unconfirmed"]
