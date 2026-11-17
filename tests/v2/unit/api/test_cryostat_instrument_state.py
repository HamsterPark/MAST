"""The settings readout and the right-rail readings for the cryostat instruments.

``/environment/instrument-state`` reports a Lake Shore 625 once (its field and
current sensors share one link) and an LM-510 by its level-meter block; only
queries cross the wire. ``/environment/readings`` puts a real LM-510 into the
helium headline slot instead of the placeholder.
"""
from __future__ import annotations

import pytest

from mast.core.types import SensorReading
from mast.environment import cryomag_lm510 as lm
from mast.environment import lakeshore_625 as m625
from tests.v2.environment.test_magnet_and_level_meter import Fake625, FakeLM510
from tests.v2.unit.api.test_admin import _client_with_monitor, _FakeMonitor


def _cryostat_monitor():
    fake625, fake510 = Fake625(), FakeLM510()
    link = m625.Model625Link(fake625, port="COM13", min_gap_s=0.0)
    field = m625.Model625Sensor(link, name="magnet_field", quantity="field")
    current = m625.Model625Sensor(link, name="magnet_current", quantity="current")
    level = lm.LM510LevelSensor(lm.LM510Link(fake510, port="COM15", min_gap_s=0.0))
    mon = _FakeMonitor(running=True)
    mon._sensors = {"magnet_field": field, "magnet_current": current, "helium_level": level}
    return mon, fake625, fake510


def test_instrument_state_reports_the_magnet_once_and_the_level_meter():
    mon, fake625, fake510 = _cryostat_monitor()
    r = _client_with_monitor(mon).get("/api/environment/instrument-state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["degraded"] is False
    kinds = sorted(i["kind"] for i in body["instruments"])
    assert kinds == ["cryomag_lm510", "lakeshore_625"], "one entry per instrument"

    mag = next(i for i in body["instruments"] if i["kind"] == "lakeshore_625")
    assert (mag["model"], mag["serial_number"], mag["port"]) == ("MODEL625", "LSA0625", "COM13")
    m = mag["magnet"]
    assert m["field_unit"] == "T" and m["field"] == pytest.approx(0.0007)
    assert m["max_field"] == pytest.approx(15.0 * 0.3406)
    assert m["quench_detected"] is False and m["any_error"] is False
    assert m["ramping"] is False and m["psh_enabled"] is False
    assert mag["channels"] == [] and mag["any_heater_on"] is False

    lev = next(i for i in body["instruments"] if i["kind"] == "cryomag_lm510")
    assert lev["level_meter"]["level"] == pytest.approx(27.1)
    assert lev["level_meter"]["unit"] == "cm"
    assert lev["level_meter"]["updates_by_itself"] is False       # Mode=Off on the instrument

    # Only queries crossed the wire, and the monitor kept the bus.
    assert all(w.split(b" ")[0].rstrip(b"\r\n").endswith(b"?") for w in fake625.writes)
    assert all(w.decode().strip().split(" ")[0] in lm.QUERIES for w in fake510.writes)
    assert "stop" not in mon.calls


def test_a_real_level_meter_fills_the_helium_headline():
    mon, *_ = _cryostat_monitor()
    latest = {"helium_level": SensorReading(value=27.1, unit="cm", status="ok"),
              "magnet_field": SensorReading(value=0.0007, unit="T", status="ok")}
    mon.get_latest = lambda: latest
    mon.overall_status = lambda: "ok"
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mast.api.context import AppContext
    from mast.api.routes.environment_readings import router

    app = FastAPI()
    ctx = AppContext()
    ctx.environment_monitor = mon
    app.state.ctx = ctx
    app.include_router(router, prefix="/api")
    body = TestClient(app).get("/api/environment/readings").json()
    assert body["helium_level"]["value"] == pytest.approx(27.1)
    rows = {s["name"]: s for s in body["sensors"]}
    assert rows["helium_level"]["kind"] == "helium_level"
    assert rows["helium_level"]["placeholder"] is False
    assert rows["magnet_field"]["kind"] is None          # its own row, not a headline
