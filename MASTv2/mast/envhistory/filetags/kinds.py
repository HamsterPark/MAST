# -*- coding: utf-8 -*-
"""传感器 → 量的种类、规范单位、主值。纯函数，无 I/O。

**种类按驱动类判（实时），退到按单位判（历史、或类认不出时），绝不按名字**：
自动识别的名字带 COM 口，换口、重扫就改名；``environment_sensors.json``
里的名字由用户写。名字只作来源记录。

:func:`classify_sensor` **只看对象的类型与字段，不调它的任何方法**：
基类 ``EnvironmentSensor.is_healthy()`` 里面就是一次 ``read()``，而 625 的读与磁场扫场
监督共用一把链路锁。625 的 ``quantity`` 属性今天没有 I/O，这里照样读私有字段
``_quantity`` —— 免得哪天有人把它改成「顺手问一下仪器」，打标路径就悄悄碰了设备。
"""
from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from typing import Any, Iterable

TEMPERATURE = "temperature"
FIELD = "field"
PRESSURE = "pressure"
LEVEL = "level"
MAGNET_CURRENT = "magnet_current"
OTHER = "other"
KINDS = (TEMPERATURE, FIELD, PRESSURE, LEVEL, MAGNET_CURRENT, OTHER)

#: 上卡片、进索引摘要与筛选的四种量，及其短键。
SUMMARY_KEYS: dict[str, str] = {TEMPERATURE: "T", FIELD: "B", PRESSURE: "P", LEVEL: "L"}

#: 单位（小写、去空白）→ (种类, 乘数, 加数, 规范单位)。规范值 = 原值 × 乘数 + 加数。
_UNITS: dict[str, tuple[str, float, float, str]] = {
    "k": (TEMPERATURE, 1.0, 0.0, "K"),
    "kelvin": (TEMPERATURE, 1.0, 0.0, "K"),
    "mk": (TEMPERATURE, 1e-3, 0.0, "K"),
    "c": (TEMPERATURE, 1.0, 273.15, "K"),
    "°c": (TEMPERATURE, 1.0, 273.15, "K"),
    "℃": (TEMPERATURE, 1.0, 273.15, "K"),
    "degc": (TEMPERATURE, 1.0, 273.15, "K"),
    "celsius": (TEMPERATURE, 1.0, 273.15, "K"),
    "t": (FIELD, 1.0, 0.0, "T"),
    "tesla": (FIELD, 1.0, 0.0, "T"),
    "mt": (FIELD, 1e-3, 0.0, "T"),
    "kg": (FIELD, 0.1, 0.0, "T"),           # 625 的 FLDS 可以设成 kG
    "g": (FIELD, 1e-4, 0.0, "T"),
    "gauss": (FIELD, 1e-4, 0.0, "T"),
    "pa": (PRESSURE, 1.0, 0.0, "Pa"),
    "hpa": (PRESSURE, 100.0, 0.0, "Pa"),
    "kpa": (PRESSURE, 1e3, 0.0, "Pa"),
    "mbar": (PRESSURE, 100.0, 0.0, "Pa"),
    "bar": (PRESSURE, 1e5, 0.0, "Pa"),
    "torr": (PRESSURE, 133.322368, 0.0, "Pa"),
    "mtorr": (PRESSURE, 0.133322368, 0.0, "Pa"),
    "%": (LEVEL, 1.0, 0.0, "%"),
    "cm": (LEVEL, 1.0, 0.0, "cm"),
    "mm": (LEVEL, 0.1, 0.0, "cm"),
    "in": (LEVEL, 2.54, 0.0, "cm"),
    "inch": (LEVEL, 2.54, 0.0, "cm"),
}

#: 每种量的**专用驱动**：挑主值时优先。值是驱动类名。
DEDICATED: dict[str, str] = {
    TEMPERATURE: "LakeshoreTemperatureSensor",
    FIELD: "Model625Sensor",
    PRESSURE: "DL7VacuumSensor",
    LEVEL: "LM510LevelSensor",
}

#: (模块, 类名, 种类)。种类 ``None`` = **排除**（占位实现、针尖电流镜像）；``"625"`` = 看
#: ``_quantity`` 字段分 field / magnet_current；其余是固定种类。认不出的类（含
#: ``NanonisSignalSensor``）按单位判。
_DRIVERS: tuple[tuple[str, str, str | None], ...] = (
    ("mast.environment.placeholders", "PlaceholderSensor", None),
    ("mast.environment.nanonis_env", "InstrumentStateSensor", None),
    ("mast.environment.lakeshore_temp", "LakeshoreTemperatureSensor", TEMPERATURE),
    ("mast.environment.lakeshore_625", "Model625Sensor", "625"),
    ("mast.environment.dl7_vacuum", "DL7VacuumSensor", PRESSURE),
    ("mast.environment.cryomag_lm510", "LM510LevelSensor", LEVEL),
)

#: 温度有多路时优先样品台（与 ``core.temperature`` 同一份偏好词，不另抄一份）。
try:
    from mast.core.temperature import _STAGE_HINTS as STAGE_HINTS
except Exception:  # noqa: BLE001 — 纯函数层不因一个 import 失败而失灵
    STAGE_HINTS = ("spm", "stage", "sample", "tip", "cryo")


def _norm_unit(unit: Any) -> str:
    return str(unit or "").strip().lower()


def kind_from_unit(unit: Any) -> str | None:
    """单位 → 种类；认不出 ⇒ None。"""
    row = _UNITS.get(_norm_unit(unit))
    return row[0] if row else None


def to_canonical(value: Any, unit: Any) -> tuple[float | None, str]:
    """``(规范值, 规范单位)``；单位认不出或值不是有限数 ⇒ ``(None, "")``。"""
    row = _UNITS.get(_norm_unit(unit))
    if row is None:
        return None, ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, ""
    if not math.isfinite(v):
        return None, ""
    return v * row[1] + row[2], row[3]


def sig(value: Any, digits: int = 6) -> float | None:
    """按**有效数字**取整（不是小数位：3e-8 Pa 按小数位取整会变成 0）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if v == 0.0:
        return 0.0
    return float(f"{v:.{int(digits)}g}")


# ── 归类 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SensorMeta:
    """一个传感器归完类的样子。``kind is None`` ⇔ 排除（不打标）。"""

    name: str
    kind: str | None
    driver: str = ""
    unit: str = ""          # 驱动字段里声明的单位（没读过时读数上没有单位）

    @property
    def excluded(self) -> bool:
        return self.kind is None


_CLASS_CACHE: dict[tuple[str, str], Any] = {}


def _load(module: str, name: str):
    key = (module, name)
    if key not in _CLASS_CACHE:
        try:
            _CLASS_CACHE[key] = getattr(importlib.import_module(module), name)
        except Exception:  # noqa: BLE001 — 驱动不可用就只靠单位判
            _CLASS_CACHE[key] = None
    return _CLASS_CACHE[key]


def classify_sensor(name: str, sensor: Any, unit_hint: str = "") -> SensorMeta:
    """按驱动类归类，认不出的类按单位。**只读类型与字段，不调任何方法。**"""
    inner = getattr(sensor, "_inner", sensor)          # 拆 autodetect._RenamedSensor
    driver = type(inner).__name__ if inner is not None else ""
    declared = str(getattr(inner, "_unit", "") or "") if inner is not None else ""
    for module, cls_name, kind in _DRIVERS:
        cls = _load(module, cls_name)
        if cls is None or inner is None or not isinstance(inner, cls):
            continue
        if kind is None:
            return SensorMeta(str(name), None, driver, declared)
        if kind == "625":
            q = str(getattr(inner, "_quantity", "field") or "field").strip().lower()
            kind = FIELD if q == "field" else MAGNET_CURRENT
        # 记认出来的那个驱动名（子类也算这个驱动）：主值按它挑「专用驱动」
        return SensorMeta(str(name), kind, cls_name, declared)
    unit = unit_hint or declared
    return SensorMeta(str(name), kind_from_unit(unit) or OTHER, driver, declared)


def classify_unit_only(name: str, unit: str, *, quiet_series: str | None = None) -> SensorMeta:
    """没有传感器对象时（只有读数或历史序列）按单位归类。

    单位 A 的序列一律排除：可能是针尖电流（``tunnel_current``）也可能是磁体电流，分不出就不用；合成序列 ``instrument_quiet`` 排除；空单位与认不出的单位也排除 —— 没有类、
    又没有单位，就说不出它是什么量。"""
    if quiet_series is None:
        try:
            from mast.envhistory.sink import QUIET_SERIES as quiet_series
        except Exception:  # noqa: BLE001
            quiet_series = "instrument_quiet"
    if str(name) == quiet_series:
        return SensorMeta(str(name), None, "", str(unit or ""))
    kind = kind_from_unit(unit)
    return SensorMeta(str(name), kind, "", str(unit or ""))


# ── 主值 ────────────────────────────────────────────────────────────────


def _stage_rank(sensor: str) -> int:
    s = str(sensor or "").lower()
    return 0 if any(h in s for h in STAGE_HINTS) else 1


def primary_key(kind: str, entry: dict) -> tuple:
    """越小越优先：新鲜 > 过期；专用驱动 > 其它；（温度）样品台偏好词；传感器名。"""
    return (
        0 if entry.get("fresh") else 1,
        0 if entry.get("driver") == DEDICATED.get(kind) else 1,
        _stage_rank(entry.get("sensor", "")) if kind == TEMPERATURE else 0,
        str(entry.get("sensor", "")),
    )


def mark_primary(values: Iterable[dict]) -> None:
    """就地给每个出现过的种类恰好标一个 ``primary=True``。"""
    by_kind: dict[str, list[dict]] = {}
    for v in values:
        v["primary"] = False
        by_kind.setdefault(str(v.get("kind")), []).append(v)
    for kind, vs in by_kind.items():
        min(vs, key=lambda e: primary_key(kind, e))["primary"] = True


__all__ = [
    "TEMPERATURE", "FIELD", "PRESSURE", "LEVEL", "MAGNET_CURRENT", "OTHER", "KINDS",
    "SUMMARY_KEYS", "DEDICATED", "STAGE_HINTS", "SensorMeta",
    "kind_from_unit", "to_canonical", "sig", "classify_sensor", "classify_unit_only",
    "primary_key", "mark_primary",
]
