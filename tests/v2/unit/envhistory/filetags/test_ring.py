# -*- coding: utf-8 -*-
"""读数环：监视器的 2 s 循环每拍调一次 ``write`` —— 它必须 O(1)、不做 I/O、
锁只持有微秒级，而且**内存有界**：环满后按条数与时间淘汰，传感器数也封顶。"""
from __future__ import annotations

import threading
import time

import pytest

from mast.envhistory.filetags import ring as R


class _Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_only_admitted_readings_are_used_but_all_are_kept():
    clk = _Clock()
    rg = R.ReadingRing(clock=clk)
    for st in ("ok", "error", "unavailable", "warning", "alarm"):
        rg.write("p", 1.0 if st != "error" else 0.0, "Pa", st)
        clk.t += 2.0
    entries = rg.snapshot("p")
    assert [e[3] for e in entries] == ["ok", "error", "unavailable", "warning", "alarm"]
    # error 那条（0.0，占位）离 t 最近，也绝不能被挑中
    e = R.nearest(entries, entries[1][0], 1.0)
    assert e is None or e[3] in R.ADMITTED
    assert R.nearest(entries, entries[1][0], 2.5)[3] == "ok"
    assert R.nearest_any(entries, entries[1][0], 0.5)[3] == "error"


def test_nearest_respects_max_age_both_ways():
    clk = _Clock(100.0)
    rg = R.ReadingRing(clock=clk)
    rg.write("t", 4.2, "K", "ok")
    entries = rg.snapshot("t")
    assert R.nearest(entries, 130.0, 30.0) is not None
    assert R.nearest(entries, 130.01, 30.0) is None
    assert R.nearest(entries, 70.0, 30.0) is not None
    assert R.last_before(entries, 99.9) is None and R.last_before(entries, 100.0)[1] == 4.2


def test_full_ring_evicts_by_time_and_by_count_and_memory_stays_bounded():
    clk = _Clock()
    rg = R.ReadingRing(maxlen=500, horizon_s=600.0, max_sensors=4, clock=clk)
    # 6 个传感器、每个 2000 条、2 s 一拍：远超三道上限
    for i in range(2000):
        for s in range(6):
            rg.write(f"s{s}", float(i), "K", "ok")
        clk.t += 2.0
    st = rg.stats()
    assert st["sensors"] <= 4                                  # 传感器数封顶
    for name in rg.names():
        entries = rg.snapshot(name)
        assert len(entries) <= 300                             # 600 s / 2 s：时间那道先到
        assert entries[0][0] >= clk.t - 2.0 - 600.0            # 最老一条不早于跨度
    assert st["approx_bytes"] <= st["bound_bytes"]
    assert st["bound_bytes"] == 4 * 500 * R.ENTRY_BYTES


def test_count_limit_applies_when_readings_come_faster_than_the_horizon():
    clk = _Clock()
    rg = R.ReadingRing(maxlen=100, horizon_s=1e9, clock=clk)
    for i in range(1000):
        rg.write("fast", float(i), "K", "ok")
        clk.t += 0.01
    entries = rg.snapshot("fast")
    assert len(entries) == 100 and entries[-1][1] == 999.0 and entries[0][1] == 900.0


def test_the_stalest_sensor_is_dropped_when_a_new_name_arrives():
    clk = _Clock()
    rg = R.ReadingRing(max_sensors=2, clock=clk)
    rg.write("old (COM13)", 1.0, "K", "ok")       # 重扫前的名字，之后再也不更新
    clk.t += 10
    rg.write("live", 1.0, "K", "ok")
    clk.t += 10
    rg.write("live", 1.0, "K", "ok")
    rg.write("new (COM14)", 1.0, "K", "ok")       # 重扫后的名字
    assert sorted(rg.names()) == ["live", "new (COM14)"]
    assert rg.stats()["evicted_sensors"] == 1


def test_write_is_cheap_and_never_raises():
    rg = R.ReadingRing()
    rg.write("x", "not a number", "K", "ok")     # 记不下来就不记
    rg.write("x", None, "K", "ok")
    assert rg.snapshot("x") == []
    n = 100_000
    t0 = time.perf_counter()
    for i in range(n):
        rg.write("p", 1e-8, "Pa", "ok")
    per_call_us = (time.perf_counter() - t0) / n * 1e6
    # 微秒级（约 1 µs）；门放宽到 50 µs，只防有人往里面加了 I/O 或 O(n) 的活
    assert per_call_us < 50.0, per_call_us


def test_concurrent_write_and_snapshot_do_not_raise():
    rg = R.ReadingRing(maxlen=200)
    stop = threading.Event()
    errors: list = []

    def writer():
        while not stop.is_set():
            try:
                rg.write("p", 1.0, "Pa", "ok")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    th = threading.Thread(target=writer)
    th.start()
    try:
        for _ in range(2000):
            entries = rg.snapshot("p")
            assert len(entries) <= 200
            R.window_stats(entries, 0, 1e12)
    finally:
        stop.set()
        th.join(5)
    assert errors == []


def test_window_stats_counts_only_inside_and_only_admitted():
    entries = [(10.0, 1.0, "T", "ok"), (20.0, 2.0, "T", "error"), (30.0, 5.0, "T", "ok"),
               (40.0, 9.0, "T", "ok")]
    st = R.window_stats(entries, 15.0, 35.0)
    assert (st["min"], st["max"], st["n"], st["first"], st["last"]) == (5.0, 5.0, 1, 30.0, 30.0)
    assert R.window_stats(entries, 11.0, 19.0) is None


# ── 读数时刻 ts（新版 EnvironmentMonitor 在归档线程上调 sink，传 ts=读数时刻）─────────


def test_write_has_a_parameter_named_ts_that_defaults_to_none():
    """新版监视器用 inspect.signature 找名为 ts 的参数；老监视器照旧四个位置参数。"""
    import inspect

    p = inspect.signature(R.ReadingRing.write).parameters
    assert "ts" in p and p["ts"].default is None


def test_without_ts_the_entry_is_stamped_by_the_clock():
    clk = _Clock(1000.0)
    rg = R.ReadingRing(clock=clk)
    rg.write("t", 4.3, "K", "ok")                       # 老监视器的调用方式
    assert [e[0] for e in rg.snapshot("t")] == [1000.0]


def test_with_ts_the_entry_is_stamped_at_the_reading_time_not_the_write_time():
    clk = _Clock(1000.0)
    rg = R.ReadingRing(clock=clk)
    rg.write("t", 4.3, "K", "ok", ts=940.5)
    assert [e[0] for e in rg.snapshot("t")] == [940.5]


def test_a_late_write_is_not_mistaken_for_a_live_reading():
    """归档线程落后 5 分钟：读数是 1000 时刻的，写进来时钟已是 1300。在 1300 存下的文件，这条读数
    不该算「当时的值」（±30 s 内），只能是过期值、写明早了 300 s。"""
    clk = _Clock(1300.0)
    rg = R.ReadingRing(clock=clk)
    rg.write("vac", 3e-8, "Pa", "ok", ts=1000.0)
    entries = rg.snapshot("vac")
    assert R.nearest(entries, 1300.0, 30.0) is None
    assert R.last_before(entries, 1300.0)[0] == 1000.0


def test_old_entries_are_dropped_by_reading_time():
    """剔除过期条目也按 ts：时钟远在前面，不该把还在两小时内的读数当成过期。"""
    clk = _Clock(1_000_000.0)
    rg = R.ReadingRing(horizon_s=100.0, clock=clk)
    rg.write("t", 1.0, "K", "ok", ts=0.0)
    rg.write("t", 2.0, "K", "ok", ts=50.0)
    assert [e[0] for e in rg.snapshot("t")] == [0.0, 50.0]
    rg.write("t", 3.0, "K", "ok", ts=200.0)              # 截止线 100：0 与 50 都过期
    assert [e[0] for e in rg.snapshot("t")] == [200.0]


def test_a_bad_ts_drops_the_reading_and_never_raises():
    rg = R.ReadingRing(clock=_Clock(1000.0))
    rg.write("t", 1.0, "K", "ok", ts="not a time")
    rg.write("t", 1.0, "K", "ok", ts=float("nan"))
    assert rg.snapshot("t") == []
