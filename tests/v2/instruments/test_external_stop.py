"""external_stop: a counter of stops asked for from outside a running protocol."""

from __future__ import annotations

import json
import threading

from mast.instruments import emission_safety
from mast.instruments.laser import SimLaser
from mast.instruments.microscope import Microscope, SimMicroscope
from mast.instruments.registry import InstrumentRegistry
from mast.instruments.temperature import SimTemperatureController, TemperatureController


def test_every_station_device_kind_counts_external_stops():
    for dev in (SimLaser(device_id="las"), SimMicroscope(device_id="mic"),
                SimTemperatureController(device_id="tc")):
        assert dev.external_stop_epoch == 0 and dev.external_stop_reason == ""
        assert dev.note_external_stop("StopWITec by ext:alice") == 1
        assert dev.note_external_stop("") == 2
        assert dev.external_stop_epoch == 2


def test_a_subclass_that_skips_init_still_works():
    class Bare(TemperatureController):  # a test double that never calls __init__
        def __init__(self):  # noqa: D401 - deliberately no super().__init__()
            pass

        def _status_raw(self):
            return {}

        def _set_setpoint_raw(self, *a, **k):
            return 0.0

        def _stop_raw(self):
            return None

    bare = Bare.__new__(Bare)
    assert bare.external_stop_epoch == 0
    assert bare.note_external_stop("x") == 1


def test_the_base_classes_carry_it():
    assert hasattr(Microscope, "note_external_stop")
    assert hasattr(TemperatureController, "external_stop_epoch")


def test_concurrent_notes_are_all_counted():
    dev = SimLaser(device_id="las")
    threads = [threading.Thread(target=lambda: [dev.note_external_stop("t") for _ in range(200)])
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert dev.external_stop_epoch == 1600


def test_safe_off_all_counts_as_an_external_stop(tmp_path):
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [{"id": "las", "type": "sim_laser"},
                                          {"id": "witec", "type": "sim_microscope"}]}), encoding="utf-8")
    reg = InstrumentRegistry(config_path=p)
    try:
        emission_safety.safe_off_all("laser_area_safe recorded as False by ana", registry=reg)
        assert reg.laser("las").external_stop_epoch == 1
        assert reg.microscope("witec").external_stop_epoch == 1
        assert "laser_area_safe" in reg.laser("las").external_stop_reason
    finally:
        reg.close_all()
