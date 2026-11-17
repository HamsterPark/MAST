# -*- coding: utf-8 -*-
"""量的种类、规范单位与主值。

种类按**驱动类**判，退到按**单位**判，绝不按名字 —— 同一个传感器换了名字（自动识别的名字带
COM 口，换口就改名）必须照样归对类。
"""
from __future__ import annotations

import pytest

from mast.envhistory.filetags import kinds as K


class _Link:
    field_unit = "T"
    port = "COM19"


def _625(quantity: str, name: str):
    from mast.environment.lakeshore_625 import Model625Sensor
    return Model625Sensor(_Link(), name=name, quantity=quantity)


def test_driver_class_decides_the_kind_not_the_name():
    from mast.environment.dl7_vacuum import DL7VacuumSensor
    from mast.environment.lakeshore_temp import LakeshoreTemperatureSensor

    for name in ("DL-7 真空计 (COM15)", "DL-7 真空计 (COM14)", "whatever"):
        m = K.classify_sensor(name, DL7VacuumSensor(name=name, port="COM15"))
        assert (m.kind, m.driver, m.name) == (K.PRESSURE, "DL7VacuumSensor", name)
    for name in ("MODEL336 A (COM13)", "magnet dewar"):
        m = K.classify_sensor(name, LakeshoreTemperatureSensor(name=name, port="COM13"))
        assert m.kind == K.TEMPERATURE


def test_the_625_splits_field_and_current_by_its_quantity_field():
    assert K.classify_sensor("magnet_field", _625("field", "magnet_field")).kind == K.FIELD
    assert K.classify_sensor("x", _625("current", "x")).kind == K.MAGNET_CURRENT


def test_renamed_wrapper_is_unwrapped():
    from mast.environment.autodetect import _RenamedSensor

    m = K.classify_sensor("magnet_field#2", _RenamedSensor(_625("field", "magnet_field"), "magnet_field#2"))
    assert (m.kind, m.driver) == (K.FIELD, "Model625Sensor")


def test_tip_current_mirror_and_placeholders_are_excluded():
    from mast.environment.nanonis_env import InstrumentStateSensor
    from mast.environment.placeholders import HeliumLevelSensor, VacuumSensor

    assert K.classify_sensor("tunnel_current", InstrumentStateSensor(lambda: None)).excluded
    assert K.classify_sensor("vacuum", VacuumSensor()).excluded
    assert K.classify_sensor("helium_level", HeliumLevelSensor()).excluded


def test_a_nanonis_signal_is_classified_by_its_declared_unit():
    from mast.environment.nanonis_env import NanonisSignalSensor

    s = NanonisSignalSensor(lambda: None, name="b_field", signal_name="B field", unit="T")
    assert K.classify_sensor("b_field", s).kind == K.FIELD
    odd = NanonisSignalSensor(lambda: None, name="aux", signal_name="Input 3", unit="V")
    assert K.classify_sensor("aux", odd).kind == K.OTHER


def test_unit_only_classification_refuses_what_it_cannot_tell():
    assert K.classify_unit_only("tunnel_current", "A").excluded          # 针尖电流还是磁体电流？
    assert K.classify_unit_only("magnet_current", "A").excluded
    assert K.classify_unit_only("instrument_quiet", "").excluded
    assert K.classify_unit_only("x", "").excluded
    assert K.classify_unit_only("x", "V").excluded
    assert K.classify_unit_only("DL-7 真空计 (COM15)", "Pa").kind == K.PRESSURE
    assert K.classify_unit_only("helium_level", "%").kind == K.LEVEL


@pytest.mark.parametrize("value, unit, want, cu", [
    (20.0, "kG", 2.0, "T"), (500.0, "mT", 0.5, "T"), (1.0e4, "G", 1.0, "T"),
    (3e-10, "mbar", 3e-8, "Pa"), (1e-9, "Torr", 1.33322368e-7, "Pa"), (2.5e-8, "Pa", 2.5e-8, "Pa"),
    (4.2, "K", 4.2, "K"), (-268.95, "°C", 4.2, "K"), (300.0, "mK", 0.3, "K"),
    (62.0, "%", 62.0, "%"), (30.0, "mm", 3.0, "cm"),
])
def test_canonical_units(value, unit, want, cu):
    got, got_unit = K.to_canonical(value, unit)
    assert got_unit == cu
    assert got == pytest.approx(want, rel=1e-9)


def test_unknown_unit_or_non_finite_value_has_no_canonical_value():
    assert K.to_canonical(1.0, "V") == (None, "")
    assert K.to_canonical(float("nan"), "K") == (None, "")


def test_significant_digits_keep_tiny_pressures():
    assert K.sig(3.123456789e-8) == 3.12346e-8
    assert K.sig(3e-10) == 3e-10                      # 按小数位取整会变成 0
    assert K.sig(float("inf")) is None and K.sig("x") is None and K.sig(0.0) == 0.0


def test_primary_prefers_fresh_then_dedicated_then_stage_then_name():
    vals = [
        {"sensor": "Magnet (COM13)", "kind": K.TEMPERATURE, "driver": "LakeshoreTemperatureSensor", "fresh": True},
        {"sensor": "SPM stage (COM13)", "kind": K.TEMPERATURE, "driver": "LakeshoreTemperatureSensor", "fresh": True},
        {"sensor": "aux_t", "kind": K.TEMPERATURE, "driver": "NanonisSignalSensor", "fresh": True},
        {"sensor": "b_old", "kind": K.FIELD, "driver": "Model625Sensor", "fresh": False},
        {"sensor": "b_nanonis", "kind": K.FIELD, "driver": "NanonisSignalSensor", "fresh": True},
        {"sensor": "only", "kind": K.PRESSURE, "driver": "DL7VacuumSensor", "fresh": False},
    ]
    K.mark_primary(vals)
    prim = {v["kind"]: v["sensor"] for v in vals if v["primary"]}
    assert prim == {K.TEMPERATURE: "SPM stage (COM13)",   # 样品台偏好词
                    K.FIELD: "b_nanonis",                 # 新鲜压过专用驱动
                    K.PRESSURE: "only"}                   # 只有过期的也要有一个主值
    assert sum(v["primary"] for v in vals) == 3
