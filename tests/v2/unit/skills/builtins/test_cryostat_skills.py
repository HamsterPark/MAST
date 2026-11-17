"""``GetMagnetState`` / ``GetHeliumLevel``: read-only, and nothing writes.

Pinned here:

* both are READ / AUTO with no parameters and never touch the execution context
  (no instrument token, no Nanonis command);
* "not wired", "no such instrument" and a reading are three different answers;
* across ALL builtin skills, the only ones that mention the magnet or helium are
  these two readers plus the field-control trio of 7.2.0 (CheckMagnetFieldChange
  reads; RampMagnetField / StopMagnetRamp write, through instruments/magnet_625).
  Nothing starts a helium-level measurement;
* ``CoreRuntime.cryostat_states`` reports one instrument snapshot per link.
"""
from __future__ import annotations

import pytest

from mast.core import cryostat, magnet_control
from mast.core.runtime import CoreRuntime
from mast.core.types import SafetyLevel, SensorReading, SkillCategory
from mast.environment import cryomag_lm510 as lm
from mast.environment import lakeshore_625 as m625
from mast.skills.builtins.cryostat import GetHeliumLevel, GetMagnetState
from tests.v2.environment.test_magnet_and_level_meter import Fake625, FakeLM510


class _Ctx:
    def __getattr__(self, name):  # pragma: no cover - reaching here is the failure
        raise AssertionError(f"a read-only cryostat skill touched context.{name}")


@pytest.fixture(autouse=True)
def _isolated_sources():
    cryostat.set_magnet_source(None)
    cryostat.set_helium_source(None)
    magnet_control.set_source(None)
    yield
    cryostat.set_magnet_source(None)
    cryostat.set_helium_source(None)
    magnet_control.set_source(None)


@pytest.mark.parametrize("cls", [GetMagnetState, GetHeliumLevel])
def test_metadata_is_read_only(cls):
    m = cls().metadata()
    assert m.category is SkillCategory.READ
    assert m.safety_level is SafetyLevel.AUTO
    assert m.parameters == []


def test_three_distinct_answers_for_the_magnet():
    r = GetMagnetState().execute(_Ctx(), {})
    assert r.success and r.data["reason"] == "no_source" and r.data["can_set_field"] is False
    cryostat.set_magnet_source(lambda: [])
    r = GetMagnetState().execute(_Ctx(), {})
    assert r.success and r.data["reason"] == "no_instrument"
    cryostat.set_magnet_source(lambda: [
        {"sensor": "magnet_field", "value": 0.0007, "unit": "T", "status": "ok",
         "port": "COM13", "instrument": {"model": "MODEL625", "field": 0.0007}},
        {"sensor": "magnet_current", "value": 0.002, "unit": "A", "status": "ok",
         "port": "COM13"},
    ])
    r = GetMagnetState().execute(_Ctx(), {})
    assert r.success and "reason" not in r.data
    assert r.data["field"] == 0.0007 and r.data["field_unit"] == "T"
    assert r.data["magnet_current_a"] == 0.002
    assert "current_a" not in r.data, "current_a is the tunnelling current's name in the state cache"
    assert r.data["instruments"] == [{"model": "MODEL625", "field": 0.0007}]
    assert r.data["can_set_field"] is False


def test_three_distinct_answers_for_helium():
    assert GetHeliumLevel().execute(_Ctx(), {}).data["reason"] == "no_source"
    cryostat.set_helium_source(lambda: [])
    assert GetHeliumLevel().execute(_Ctx(), {}).data["reason"] == "no_instrument"
    cryostat.set_helium_source(lambda: [
        {"sensor": "helium_level", "value": 27.1, "unit": "cm", "status": "ok",
         "instrument": {"updates_by_itself": False}}])
    d = GetHeliumLevel().execute(_Ctx(), {}).data
    assert (d["level"], d["unit"], d["status"]) == (27.1, "cm", "ok")
    assert d["starts_measurements"] is False
    assert d["instruments"][0]["updates_by_itself"] is False


def test_a_source_that_raises_reads_as_not_wired():
    def boom():
        raise RuntimeError("monitor gone")

    cryostat.set_magnet_source(boom)
    assert GetMagnetState().execute(_Ctx(), {}).data["reason"] == "no_source"


def test_only_the_field_control_pair_can_change_the_magnet_and_nothing_starts_a_level_reading():
    import mast.skills.builtins as builtins

    hits = sorted(n for n in builtins.__all__
                  if any(t in n.lower() for t in ("magnet", "helium", "field", "lm510",
                                                  "level")))
    assert hits == ["CheckMagnetFieldChange", "GetHeliumLevel", "GetMagnetState",
                    "RampMagnetField", "StopMagnetRamp"], hits
    writers = sorted(n for n in hits
                     if getattr(builtins, n)().metadata().category is not SkillCategory.READ)
    assert writers == ["RampMagnetField", "StopMagnetRamp"], writers
    assert not any("level" in n.lower() for n in writers)


class _Monitor:
    def __init__(self, sensors, latest):
        self._sensors, self._latest = sensors, latest

    def get_latest(self):
        return self._latest


def test_runtime_reports_one_snapshot_per_instrument():
    link = m625.Model625Link(Fake625(), port="COM13", min_gap_s=0.0)
    field = m625.Model625Sensor(link, name="magnet_field", quantity="field")
    current = m625.Model625Sensor(link, name="magnet_current", quantity="current")
    level = lm.LM510LevelSensor(lm.LM510Link(FakeLM510(), port="COM15", min_gap_s=0.0))
    rt = CoreRuntime.__new__(CoreRuntime)
    rt._monitor = _Monitor(
        {"magnet_field": field, "magnet_current": current, "helium_level": level},
        {"magnet_field": SensorReading(value=0.0007, unit="T", status="ok"),
         "magnet_current": SensorReading(value=0.002, unit="A", status="ok")})
    rows = rt.cryostat_states("magnet")
    assert [r["sensor"] for r in rows] == ["magnet_field", "magnet_current"]
    assert rows[0]["instrument"]["model"] == "MODEL625"
    assert "instrument" not in rows[1], "the second sensor of the same link adds no snapshot"
    assert rows[0]["value"] == 0.0007 and rows[0]["port"] == "COM13"
    he = rt.cryostat_states("helium")
    assert len(he) == 1 and he[0]["instrument"]["level"] == pytest.approx(27.1)
    assert he[0]["value"] is None, "no cached reading yet is None, not 0"


def test_runtime_without_a_monitor_reports_no_instrument():
    rt = CoreRuntime.__new__(CoreRuntime)
    rt._monitor = None
    assert rt.cryostat_states("magnet") == []
