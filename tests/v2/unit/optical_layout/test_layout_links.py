"""What a component's links say right now - and that reading them never opens a device.

``links_state`` reads the instrument registry's configuration (``list_devices`` never touches
hardware), the devices the process already has built (``built_station_devices``) and the optical
state's snapshot. A fake registry records every call, so "no device is opened" is a statement about
the calls that were made.
"""
from __future__ import annotations

import pytest

from mast.operator_hitl import optical_state
from mast.optical_layout.links import links_state
from mast.optical_layout.model import Layout

from ._layouts import comp, laser, layout


class FakeDevice:
    def __init__(self, status=None, error=None):
        self.opened = 0
        self._status, self._error = status, error

    def status(self):
        if self._error:
            raise RuntimeError(self._error)
        return self._status

    def open(self):                      # pragma: no cover - must never be called
        self.opened += 1

    connect = open


class FakeRegistry:
    def __init__(self, devices, built=None, fail=None):
        self._devices, self._built, self._fail = devices, built or {}, fail
        self.calls: list[str] = []

    def list_devices(self):
        self.calls.append("list_devices")
        if self._fail:
            raise RuntimeError(self._fail)
        return self._devices

    def built_station_devices(self):
        self.calls.append("built_station_devices")
        return self._built

    def __getattr__(self, name):         # anything else (open, controller, build ...) is a violation
        raise AssertionError(f"links_state touched the registry's {name}()")


def lay(*specs) -> Layout:
    comps = [laser("las1", 0, 0)]
    for cid, device, slot in specs:
        c = comp(cid, "waveplate", 100, 100, 0)
        c["links"] = {"device_id": device, "slot": slot}
        comps.append(c)
    return Layout.model_validate(layout(comps))


@pytest.fixture
def state():
    old = optical_state.set_store(optical_state.OpticalStateStore())
    yield optical_state.get_store()
    optical_state.set_store(old)


def test_a_layout_without_links_has_no_link_state_and_asks_nobody():
    reg = FakeRegistry([])
    assert links_state(lay(), registry=reg) == {} and reg.calls == []


def test_a_device_is_reported_from_the_configuration_without_opening_it():
    reg = FakeRegistry([{"id": "hwp_rot", "name": "HWP rotator", "type": "pztc", "kind": "motion", "enabled": True,
                         "connected": False, "axes": [{"name": "x"}], "tip_requirement": "none"}])
    got = links_state(lay(("hwp1", "hwp_rot", None)), registry=reg)
    assert got == {"hwp1": {"device": {"id": "hwp_rot", "known": True, "name": "HWP rotator", "type": "pztc",
                                       "kind": "motion", "enabled": True, "connected": False}}}
    assert set(reg.calls) <= {"list_devices", "built_station_devices"}


def test_the_status_of_a_device_the_process_already_has_open_is_read():
    dev = FakeDevice({"power_mw": 12.5, "emission": True})
    reg = FakeRegistry([{"id": "superk", "name": "SuperK", "type": "nkt", "kind": "laser", "enabled": True, "connected": True}],
                       built={"superk": dev})
    got = links_state(lay(("hwp1", "superk", None)), registry=reg)["hwp1"]["device"]
    assert got["status"] == {"power_mw": 12.5, "emission": True} and dev.opened == 0
    broken = FakeDevice(error="no answer")
    reg = FakeRegistry([{"id": "superk", "kind": "laser", "enabled": True, "connected": True}], built={"superk": broken})
    got = links_state(lay(("hwp1", "superk", None)), registry=reg)["hwp1"]["device"]
    assert "status" not in got and "no answer" in got["status_error"]


def test_a_device_that_is_configured_but_not_built_is_not_asked_for_a_status():
    dev = FakeDevice({"x": 1})
    reg = FakeRegistry([{"id": "superk", "kind": "laser", "enabled": True, "connected": False}], built={})
    got = links_state(lay(("hwp1", "superk", None)), registry=reg)["hwp1"]["device"]
    assert "status" not in got and dev.opened == 0


def test_an_unknown_device_and_an_unreadable_registry_are_said_plainly():
    reg = FakeRegistry([{"id": "other", "kind": "laser"}])
    assert links_state(lay(("hwp1", "ghost", None)), registry=reg)["hwp1"]["device"] == {"id": "ghost", "known": False}
    reg = FakeRegistry([], fail="config unreadable")
    got = links_state(lay(("hwp1", "x", None), ("hwp2", "y", None)), registry=reg)
    assert got["hwp1"]["device"]["known"] is None and "config unreadable" in got["hwp2"]["device"]["error"]


def test_a_slot_is_read_from_the_optical_state_snapshot(state):
    state.set_by_operator("excitation_waveplate", "hwp_532", by="ana", source="test")
    got = links_state(lay(("hwp1", None, "excitation_waveplate"), ("hwp2", None, "objective"), ("hwp3", None, "nope")),
                      registry=FakeRegistry([]))
    s1 = got["hwp1"]["slot"]
    assert (s1["id"], s1["known"], s1["value"], s1["valid"], s1["set_by"]) == ("excitation_waveplate", True, "hwp_532", True, "ana")
    assert got["hwp2"]["slot"]["known"] is True and got["hwp2"]["slot"]["value"] is None and got["hwp2"]["slot"]["valid"] is False
    assert got["hwp3"]["slot"] == {"id": "nope", "known": False}


def test_a_device_and_a_slot_on_one_component_and_only_components_with_links():
    reg = FakeRegistry([{"id": "hwp_rot", "kind": "motion", "enabled": True, "connected": True}])
    got = links_state(lay(("hwp1", "hwp_rot", "excitation_waveplate"), ("plain", None, None)), registry=reg)
    assert set(got) == {"hwp1"} and set(got["hwp1"]) == {"device", "slot"}


def test_the_default_registry_is_the_process_one_and_reading_it_opens_nothing(monkeypatch):
    reg = FakeRegistry([{"id": "hwp_rot", "kind": "motion", "enabled": True, "connected": False}])
    import mast.instruments.registry as registry_module

    monkeypatch.setattr(registry_module, "get_instrument_registry", lambda: reg)
    assert links_state(lay(("hwp1", "hwp_rot", None)))["hwp1"]["device"]["known"] is True
    assert set(reg.calls) <= {"list_devices", "built_station_devices"}
