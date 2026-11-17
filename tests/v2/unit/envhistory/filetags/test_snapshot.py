# -*- coding: utf-8 -*-
"""组装记录（设计 §4）：新鲜 → 桶 → 过期照记 → 缺失；采集窗口的 min/max。"""
from __future__ import annotations

import pytest

from mast.core.types import SensorReading
from mast.envhistory.filetags import kinds as K
from mast.envhistory.filetags.acq import AcqWindow
from mast.envhistory.filetags.ring import ReadingRing
from mast.envhistory.filetags.snapshot import SCHEMA, build_record
from mast.envhistory.store import EnvHistoryStore

T = 1_800_000_000.0           # 保存时刻（整分钟，便于桶对齐）
NS = int(T * 1e9)


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _ring_with(series: dict[str, list[tuple]]) -> ReadingRing:
    """``{name: [(t, value, unit, status), ...]}`` → 读数环（用注入的时钟逐条写入）。"""
    clk = _Clock(0.0)
    rg = ReadingRing(clock=clk, horizon_s=1e9)
    for name, rows in series.items():
        for t, v, u, st in rows:
            clk.t = t
            rg.write(name, v, u, st)
    return rg


def _store(tmp_path, rows: list[tuple]) -> EnvHistoryStore:
    """rows: (sensor, bucket_ts, dt_s, n, mean, min, max, unit, worst_status)。"""
    st = EnvHistoryStore(tmp_path / "eh" / "env_history.sqlite")
    st.upsert_buckets([(s, b, dt, n, mean, lo, hi, 0.0, mean, u, ws, 0, None, None)
                       for s, b, dt, n, mean, lo, hi, u, ws in rows])
    return st


def _metas(**kinds_by_name) -> dict:
    drivers = {K.TEMPERATURE: "LakeshoreTemperatureSensor", K.FIELD: "Model625Sensor",
               K.PRESSURE: "DL7VacuumSensor", K.LEVEL: "LM510LevelSensor"}
    return {n: K.SensorMeta(n, k, drivers.get(k, ""), "") for n, k in kinds_by_name.items()}


def _record(*, ring=None, store=None, latest=None, sensors=None, acq=None, max_age=30.0,
            observed_at=T + 3.0, path="D:/data/Topo_0042.sxm"):
    return build_record(data_path=path, size=1234, mtime_ns=NS, head_sha256="h", acq=acq,
                        sensors=sensors, latest=latest or {}, ring=ring, store=store,
                        max_age_s=max_age, origin="watcher", observed_at=observed_at,
                        now=T + 5.0, host="pc", mast_version="7.2.0")


def _vals(rec) -> dict:
    return {v["sensor"]: v for v in rec["values"]}


def test_a_fresh_cached_reading_is_live_with_its_own_time_and_offset():
    rg = _ring_with({"temp": [(T - 40, 4.5, "K", "ok"), (T - 1.5, 4.31, "K", "ok"), (T + 0.5, 4.32, "K", "ok")]})
    rec = _record(ring=rg, sensors=_metas(temp=K.TEMPERATURE))
    v = _vals(rec)["temp"]
    assert rec["schema"] == SCHEMA
    assert (v["source"], v["fresh"], v["value"], v["unit"], v["dt_s"]) == ("live", True, 4.32, "K", 0.5)
    assert v["t"] == T + 0.5 and v["span_s"] == 0 and v["primary"] is True
    assert v["iso"].startswith("20") and ("+" in v["iso"][19:] or "-" in v["iso"][19:])


def test_without_a_cached_reading_the_bucket_holding_the_save_time_is_used(tmp_path):
    st = _store(tmp_path, [("vac", T - 60, 60.0, 28, 3.1e-8, 3.0e-8, 3.2e-8, "Pa", "ok"),
                           ("vac", T, 60.0, 30, 3.3e-8, 3.2e-8, 3.4e-8, "Pa", "ok")])
    rec = _record(store=st, sensors=_metas(vac=K.PRESSURE))
    v = _vals(rec)["vac"]
    assert (v["source"], v["fresh"], v["t"], v["span_s"], v["dt_s"]) == ("history", True, T, 60.0, 0.0)
    assert v["value"] == 3.3e-8 and v["bucket"] == {"n": 30, "min": 3.2e-8, "max": 3.4e-8, "worst_status": "ok"}
    assert v["canonical"] == 3.3e-8 and v["canonical_unit"] == "Pa"


def test_a_bucket_whose_edge_is_within_max_age_counts_with_a_signed_offset(tmp_path):
    st = _store(tmp_path, [("vac", T - 70, 60.0, 30, 3.1e-8, 3.0e-8, 3.2e-8, "Pa", "ok")])
    v = _vals(_record(store=st, sensors=_metas(vac=K.PRESSURE)))["vac"]
    assert v["fresh"] is True and v["dt_s"] == -10.0       # 桶在 T-10 结束


def test_a_stale_reading_is_recorded_with_its_age_and_never_refreshed():
    rg = _ring_with({"vac": [(T - 412.0, 3.1e-8, "Pa", "ok"), (T - 200.0, 0.0, "Pa", "unavailable")]})
    v = _vals(_record(ring=rg, sensors=_metas(vac=K.PRESSURE)))["vac"]
    assert (v["fresh"], v["source"], v["value"], v["dt_s"]) == (False, "live", 3.1e-8, -412.0)
    assert v["primary"] is True                             # 只有它，也有一个主值


def test_the_more_recent_of_stale_reading_and_stale_bucket_wins(tmp_path):
    rg = _ring_with({"vac": [(T - 3000.0, 9e-8, "Pa", "ok")]})
    st = _store(tmp_path, [("vac", T - 600, 60.0, 30, 3.1e-8, 3.0e-8, 3.2e-8, "Pa", "ok")])
    v = _vals(_record(ring=rg, store=st, sensors=_metas(vac=K.PRESSURE)))["vac"]
    assert (v["fresh"], v["source"], v["value"], v["dt_s"]) == (False, "history", 3.1e-8, -540.0)


def test_missing_says_why_and_error_values_never_appear():
    rg = _ring_with({"temp": [(T - 1.0, 0.0, "", "error")], "lvl": []})
    rec = _record(ring=rg, sensors=_metas(temp=K.TEMPERATURE, lvl=K.LEVEL))
    assert rec["values"] == []
    miss = {m["sensor"]: m for m in rec["missing"]}
    assert miss["temp"] == {"sensor": "temp", "kind": K.TEMPERATURE, "why": "invalid", "status": "error"}
    assert miss["lvl"]["why"] == "no_reading"


def test_get_latest_is_used_when_it_is_newer_than_the_ring():
    rg = _ring_with({"temp": [(T - 50.0, 4.4, "K", "ok")]})
    from datetime import datetime
    ts = datetime.fromtimestamp(T - 1.0).isoformat()
    latest = {"temp": SensorReading(value=4.3, unit="K", timestamp=ts, status="ok")}
    v = _vals(_record(ring=rg, latest=latest, sensors=_metas(temp=K.TEMPERATURE)))["temp"]
    assert (v["value"], v["fresh"], v["dt_s"]) == (4.3, True, -1.0)


def test_a_cached_reading_without_a_readable_timestamp_is_not_used():
    latest = {"temp": SensorReading(value=4.3, unit="K", timestamp="not a time", status="ok")}
    rec = _record(latest=latest, sensors=_metas(temp=K.TEMPERATURE))
    assert rec["values"] == [] and rec["missing"][0]["why"] == "no_reading"


def test_excluded_sensors_never_appear_and_without_a_sensor_map_units_decide():
    rg = _ring_with({"tunnel_current": [(T, 1e-10, "A", "ok")], "vac": [(T, 3e-8, "Pa", "ok")],
                     "instrument_quiet": [(T, 1.0, "", "ok")]})
    rec = _record(ring=rg, sensors=None)
    assert [v["sensor"] for v in rec["values"]] == ["vac"]
    assert rec["missing"] == []
    meta = {"vac": K.SensorMeta("vac", K.PRESSURE, "DL7VacuumSensor"),
            "tunnel_current": K.SensorMeta("tunnel_current", None, "InstrumentStateSensor")}
    rec2 = _record(ring=rg, sensors=meta)
    assert [v["sensor"] for v in rec2["values"]] == ["vac"]


def _ramp(t0: float, t1: float, b0: float, b1: float, unit="T") -> list[tuple]:
    out, t = [], t0
    while t <= t1:
        frac = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
        out.append((t, b0 + (b1 - b0) * frac, unit, "ok"))
        t += 2.0
    return out


def test_a_field_ramp_during_the_scan_shows_in_the_window():
    start, end = T - 300.0, T
    rows = _ramp(start - 100, start + 100, 0.0, 0.0) + _ramp(start + 102, start + 200, 0.0, 2.0) \
        + _ramp(start + 202, end + 2, 2.0, 2.0)
    rg = _ring_with({"b": rows})
    rec = _record(ring=rg, sensors=_metas(b=K.FIELD), acq=AcqWindow(start, end, "REC_DATE/REC_TIME", "mtime"))
    (w,) = rec["window"]
    assert (w["min"], w["max"], w["method"], w["complete"]) == (0.0, 2.0, "readings", True)
    assert rec["acquisition"]["duration_s"] == 300.0


def test_a_ramp_that_ended_before_the_scan_is_not_counted_by_readings_but_is_by_buckets(tmp_path):
    """桶的 min/max 覆盖整分钟，会把扫描开始前几秒的升场算进窗口。"""
    start, end = T - 300.0 + 20.0, T          # 扫描从某一分钟的第 20 s 开始
    rows = _ramp(start - 200, start - 6, 0.0, 2.0) + _ramp(start - 4, end + 2, 2.0, 2.0)
    rg = _ring_with({"b": rows})
    acq = AcqWindow(start, end, "REC_DATE/REC_TIME", "mtime")
    (w,) = _record(ring=rg, sensors=_metas(b=K.FIELD), acq=acq)["window"]
    assert (w["min"], w["max"], w["method"]) == (2.0, 2.0, "readings")
    # 同一段历史只剩 1 分钟桶时：包含起点的那个桶里有升场 ⇒ min 被拉低，覆盖区间如实早于起点
    b0 = (start // 60) * 60
    st = _store(tmp_path, [("b", b0, 60.0, 30, 1.9, 1.7, 2.0, "T", "ok")]
                + [("b", b0 + 60 * i, 60.0, 30, 2.0, 2.0, 2.0, "T", "ok") for i in range(1, 6)])
    (wb,) = _record(store=st, sensors=_metas(b=K.FIELD), acq=acq)["window"]
    assert wb["method"] == "buckets" and wb["min"] == 1.7 and wb["from"] == b0 < start


def test_a_window_older_than_the_ring_uses_buckets_plus_the_ring_tail(tmp_path):
    start, end = T - 4 * 3600.0, T             # 4 h 的网格：比读数环长
    st = _store(tmp_path, [("b", start + 60 * i, 60.0, 30, 2.0, 2.0, 2.0, "kG", "ok")
                           for i in range(int((end - 90 - start) // 60))])
    last_flushed_end = start + 60 * int((end - 90 - start) // 60)
    rg = _ring_with({"b": _ramp(last_flushed_end - 600, end, 2.0, 2.0, unit="kG")
                     + [(end - 1.0, 3.0, "kG", "ok")]})
    (w,) = _record(ring=rg, store=st, sensors=_metas(b=K.FIELD),
                   acq=AcqWindow(start, end, "Start time", "mtime"))["window"]
    assert w["method"] == "buckets+readings" and w["complete"] is True
    assert (w["min"], w["max"], w["unit"]) == (2.0, 3.0, "kG")
    assert (w["canonical_min"], w["canonical_max"], w["canonical_unit"]) == (0.2, 0.3, "T")


def test_record_shape_rounding_and_clock_skew():
    rg = _ring_with({"vac": [(T, 3.123456789e-8, "Pa", "ok")]})
    rec = _record(ring=rg, sensors=_metas(vac=K.PRESSURE), observed_at=T - 20.0)
    v = _vals(rec)["vac"]
    assert v["value"] == 3.12346e-8
    assert rec["file"] == {"name": "Topo_0042.sxm", "kind": "sxm", "size": 1234, "mtime": T,
                           "mtime_ns": NS, "head_sha256": "h", "tagged_path": "D:/data/Topo_0042.sxm"}
    assert rec["saved_at"]["from"] == "mtime" and rec["acquisition"] is None
    assert rec["tagged"]["clock_skew_s"] == 20.0 and rec["tagged"]["origin"] == "watcher"
    assert _record(ring=rg, sensors=_metas(vac=K.PRESSURE))["tagged"]["clock_skew_s"] is None
    import json
    json.dumps(rec, allow_nan=False)            # 能落成严格 JSON
