# -*- coding: utf-8 -*-
"""记录器：钩子永不等、静置、去重、副本复用、写失败的容忍。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from mast.envhistory.filetags import kinds as K
from mast.envhistory.filetags import sidecar as SC
from mast.envhistory.filetags.ring import ReadingRing
from mast.envhistory.filetags.tagger import FileEnvTagger
from mast.envhistory.store import EnvHistoryStore


class _Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _file(path: Path, *, age_s: float = 100.0, body: bytes = b"x" * 512) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def _ring_at(t: float, *names_values) -> ReadingRing:
    clk = [t]
    rg = ReadingRing(clock=lambda: clk[0], horizon_s=1e9)
    for name, v, u in names_values:
        rg.write(name, v, u, "ok")
    return rg


@pytest.fixture()
def world(tmp_path):
    base = tmp_path / "w"
    store = EnvHistoryStore(base / "eh" / "env_history.sqlite")
    yield base, store
    store.close()


def _tagger(store, *, ring=None, meta=None, enabled=True, mono=None, **kw) -> FileEnvTagger:
    meta = meta if meta is not None else {"vac": K.SensorMeta("vac", K.PRESSURE, "DL7VacuumSensor")}
    return FileEnvTagger(ring=ring, latest_getter=lambda: {}, meta_getter=lambda: meta,
                         store_getter=lambda: store, settings_getter=lambda: (enabled, 30.0),
                         start_worker=kw.pop("start_worker", False),
                         monotonic=mono or time.monotonic, host="pc", mast_version="t", **kw)


def test_submit_never_waits_even_when_the_worker_is_stuck(world):
    base, store = world
    gate = threading.Event()
    entered = threading.Event()

    def stuck_settings():
        entered.set()
        gate.wait(30)                 # 例如网络盘上的配置文件读不回来
        return True, 30.0

    tg = FileEnvTagger(store_getter=lambda: store, settings_getter=stuck_settings,
                       queue_max=1024, start_worker=True)
    try:
        tg.submit([str(base / "first.sxm")], origin="skill")
        assert entered.wait(10)
        paths = [str(base / f"f{i:05d}.sxm") for i in range(10_000)]
        t0 = time.perf_counter()
        for i in range(0, 10_000, 100):
            tg.submit(paths[i:i + 100], origin="watcher")
        took = time.perf_counter() - t0
        assert took < 0.5, took        # 约 20 ms；门放宽，防的是「提交在等工作线程」
        st = tg.stats()
        assert st["pending"] == 1024 and st["dropped_full"] == 10_000 - 1024
    finally:
        gate.set()
        tg.stop(timeout=5.0)


def test_a_file_still_being_written_is_deferred_until_it_settles(world):
    base, store = world
    mono = _Mono()
    p = _file(base / "grid" / "Grid Spectroscopy001.3ds", age_s=5.0)      # .3ds 的静置门是 60 s
    tg = _tagger(store, ring=_ring_at(p.stat().st_mtime, ("vac", 3e-8, "Pa")), mono=mono)
    tg.submit([str(p)], origin="watcher")
    assert tg.process_pending() == 1
    assert tg.stats()["deferred"] == 1 and tg.stats()["pending"] == 1
    assert not SC.sidecar_path(p).exists()
    assert tg.process_pending() == 0                       # 没到点不处理
    mono.t += 61.0
    t = time.time() - 120
    os.utime(p, (t, t))
    tg._ring = _ring_at(t, ("vac", 3e-8, "Pa"))
    assert tg.process_pending() == 1
    assert SC.sidecar_path(p).exists()


def test_the_same_path_is_queued_once(world):
    base, store = world
    tg = _tagger(store)
    p = str(base / "a.sxm")
    assert tg.submit([p, p], origin="skill") == 1
    assert tg.submit([p], origin="watcher") == 0
    assert tg.stats()["merged"] == 2 and tg.stats()["pending"] == 1
    assert tg.submit([str(base / "notes.txt")], origin="skill") == 0       # 不是数据文件


def test_a_tagged_file_gets_a_sidecar_and_a_central_record_and_is_not_redone(world):
    base, store = world
    p = _file(base / "s" / "Bias-Spectroscopy001.dat")
    tg = _tagger(store, ring=_ring_at(p.stat().st_mtime, ("vac", 3.1e-8, "Pa")))
    tg.submit([str(p)], origin="skill")
    tg.process_pending()
    sp = SC.sidecar_path(p)
    doc = json.loads(sp.read_text(encoding="utf-8"))
    assert doc["values"][0]["value"] == 3.1e-8 and doc["tagged"]["origin"] == "skill"
    (row,) = store.find_file_tags(p.name, p.stat().st_size)
    assert json.loads(row["payload"]) == doc and row["sidecar_path"] == str(sp)
    raw, mt = sp.read_bytes(), sp.stat().st_mtime_ns
    tg.submit([str(p)], origin="watcher")                 # 文件监视又看到了它
    tg.process_pending()
    assert sp.read_bytes() == raw and sp.stat().st_mtime_ns == mt
    assert tg.stats()["computed"] == 1 and tg.stats()["same"] == 1


def test_a_copy_gets_the_originals_record_without_recomputing(world):
    base, store = world
    orig = _file(base / "session" / "Topo_0007.sxm")
    copy = base / "exp" / "samples" / "S01" / "raw" / "sxm" / "Topo_0007.sxm"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(orig.read_bytes())
    os.utime(copy, ns=(orig.stat().st_mtime_ns, orig.stat().st_mtime_ns))   # copystat
    tg = _tagger(store, ring=_ring_at(orig.stat().st_mtime, ("vac", 3e-8, "Pa")))
    tg.submit([str(orig)], origin="watcher")
    tg.process_pending()
    tg._ring = _ring_at(0.0, ("vac", 9e-9, "Pa"))          # 环境早变了：副本也必须用原件那份
    tg._lru.clear()                                        # 逼它走中央记录
    tg.submit([str(copy)], origin="copy")
    tg.process_pending()
    assert SC.sidecar_path(copy).read_bytes() == SC.sidecar_path(orig).read_bytes()
    assert tg.stats()["computed"] == 1 and tg.stats()["reused"] == 1


def test_an_unwritable_directory_falls_back_to_the_central_record_and_cools_down(world, caplog):
    base, store = world
    d = base / "readonly"
    p1, p2 = _file(d / "a.sxm"), _file(d / "b.sxm")
    (d / ".mast").write_text("a file where the folder should be", encoding="utf-8")
    tg = _tagger(store, ring=_ring_at(p1.stat().st_mtime, ("vac", 3e-8, "Pa")))
    with caplog.at_level(logging.WARNING, logger="mast.envhistory.filetags.tagger"):
        tg.submit([str(p1), str(p2)], origin="watcher")
        tg.process_pending()
    st = tg.stats()
    assert st["failed_write"] == 1 and st["cooldown_skipped"] == 1 and st["central_written"] == 2
    assert st["cooling_dirs"] == 1
    rows = store.find_file_tags("a.sxm", p1.stat().st_size)
    assert rows and rows[0]["sidecar_path"] is None and rows[0]["sidecar_error"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_info_is_logged_once_per_directory(world, caplog):
    base, store = world
    a1, a2 = _file(base / "A" / "1.sxm"), _file(base / "A" / "2.sxm")
    b1 = _file(base / "B" / "1.sxm")
    tg = _tagger(store, ring=_ring_at(a1.stat().st_mtime, ("vac", 3e-8, "Pa")))
    with caplog.at_level(logging.INFO, logger="mast.envhistory.filetags.tagger"):
        tg.submit([str(a1), str(a2), str(b1)], origin="watcher")
        tg.process_pending()
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 2
    assert any(str(base / "A" / ".mast" / "envtags") in m for m in infos)
    assert any(str(base / "B" / ".mast" / "envtags") in m for m in infos)


def test_switched_off_means_nothing_is_written(world):
    base, store = world
    p = _file(base / "a.sxm")
    tg = _tagger(store, ring=_ring_at(p.stat().st_mtime, ("vac", 3e-8, "Pa")), enabled=False)
    for _ in range(2):
        tg.submit([str(p)], origin="watcher")
        tg.process_pending()
    assert not SC.sidecar_path(p).exists() and tg.enabled_cached is False
    assert tg.stats()["dropped_disabled"] == 2
    assert store.find_file_tags("a.sxm", p.stat().st_size) == []


def test_switching_back_on_takes_effect(world):
    """关过一次之后，钩子不能再按缓存的「关」把后来的文件丢掉 —— 否则重新打开永远不生效。"""
    base, store = world
    p1, p2 = _file(base / "a.sxm"), _file(base / "b.sxm")
    switch = {"on": False}
    tg = FileEnvTagger(ring=_ring_at(p1.stat().st_mtime, ("vac", 3e-8, "Pa")),
                       latest_getter=lambda: {},
                       meta_getter=lambda: {"vac": K.SensorMeta("vac", K.PRESSURE, "DL7VacuumSensor")},
                       store_getter=lambda: store, settings_getter=lambda: (switch["on"], 30.0),
                       start_worker=False)
    tg.submit([str(p1)], origin="watcher")
    tg.process_pending()
    assert tg.enabled_cached is False and not SC.sidecar_path(p1).exists()
    switch["on"] = True
    assert tg.submit([str(p2)], origin="skill") == 1
    tg.process_pending()
    assert SC.sidecar_path(p2).exists() and tg.enabled_cached is True


def test_nothing_known_means_no_sidecar(world):
    base, store = world
    p = _file(base / "a.sxm")
    tg = _tagger(store, ring=None, meta={})
    tg.submit([str(p)], origin="watcher")
    tg.process_pending()
    assert not SC.sidecar_path(p).exists() and tg.stats()["empty"] == 1
    assert store.find_file_tags("a.sxm", p.stat().st_size) == []


def test_a_vanished_file_is_skipped_quietly(world):
    base, store = world
    tg = _tagger(store)
    tg.submit([str(base / "gone.sxm")], origin="skill")
    tg.process_pending()
    assert tg.stats()["vanished"] == 1


def test_the_worker_starts_on_demand_and_stop_is_bounded(world):
    base, store = world
    p = _file(base / "a.sxm")
    tg = _tagger(store, ring=_ring_at(p.stat().st_mtime, ("vac", 3e-8, "Pa")), start_worker=True,
                 idle_exit_s=0.3)
    tg.submit([str(p)], origin="skill")
    assert tg.drain(10)
    assert SC.sidecar_path(p).exists()
    deadline = time.monotonic() + 5
    while tg.stats()["worker_alive"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not tg.stats()["worker_alive"]                  # 空闲后自己退出，不常驻
    t0 = time.perf_counter()
    tg.stop(timeout=2.0)
    assert time.perf_counter() - t0 < 2.5
