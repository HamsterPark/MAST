# -*- coding: utf-8 -*-
"""图库里的环境标签（docs/v2/design/data_env_tags.md §6）。

构建读 sidecar / 中央记录，没有就按环境历史推断并标「推断」；后写的 sidecar 下一轮增量构建
看得见（``n_env``）；图库对数据目录只读；索引里 3e-8 Pa 还是 3e-8；配置新键省略即不变；
详情端点四种出处，永不 500。合成文件用本目录 conftest 的 ``synth``（与图库其它测试同一套）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.context import AppContext
from mast.api.routes.gallery import router
from mast.envhistory.filetags import kinds as K
from mast.envhistory.filetags import sidecar as SC
from mast.envhistory.filetags.acq import acquisition_window
from mast.envhistory.filetags.ring import ReadingRing
from mast.envhistory.filetags.snapshot import build_record
from mast.envhistory.store import get_store
from mast.gallery import build, index, inventory
from mast.gallery import config as gcfg

DAY = "2001/200109/20010910"


def _configure(root, name="SPM"):
    roots, errors = gcfg.normalise_roots([{"name": name, "path": str(root)}])
    assert errors == []
    gcfg.save_config(gcfg.GalleryConfig(roots=roots, workers=2))


def _sxm(synth, path, mtime_ns):
    return synth.sxm(path, np.random.default_rng(0).normal(0, 1e-11, (32, 32)),
                     rec_time="15:00:00", mtime_ns=mtime_ns)


def _tag(path: Path, *, kinds=(("vac", K.PRESSURE, "DL7VacuumSensor", 3.1e-8, "Pa"),
                               ("b", K.FIELD, "Model625Sensor", 20.0, "kG"),
                               ("t", K.TEMPERATURE, "LakeshoreTemperatureSensor", 4.31, "K"))) -> dict:
    """照记录器的做法给 ``path`` 写一份 sidecar（读数就在保存时刻）。"""
    st = path.stat()
    t = st.st_mtime_ns / 1e9
    ring = ReadingRing(clock=lambda: t, horizon_s=1e9)
    for name, _k, _d, v, u in kinds:
        ring.write(name, v, u, "ok")
    rec = build_record(data_path=str(path), size=st.st_size, mtime_ns=st.st_mtime_ns,
                       head_sha256=SC.head_sha256(path),
                       acq=acquisition_window(path, mtime=t),
                       sensors={n: K.SensorMeta(n, k, d) for n, k, d, _v, _u in kinds},
                       latest={}, ring=ring, store=None, max_age_s=30.0, origin="watcher",
                       observed_at=t + 2, now=t + 3, host="pc", mast_version="t")
    status, _sp, err = SC.write_sidecar(path, rec, size=st.st_size, mtime_ns=st.st_mtime_ns)
    assert status == "written", err
    return rec


def _items() -> dict:
    return {it["fn"]: it for it in index.read_index()["items"]}


def _tree(root: Path) -> dict:
    out = {}
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            p = Path(dp, fn)
            out[str(p)] = (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


# ── 采集窗口与图库同一套时刻───────────────────────────────────────


def test_the_window_start_is_the_gallery_start(tmp_path, synth):
    from mast.envhistory.filetags.acq import parse_local

    # 这里要的是 mtime 与头里时刻的相对关系，所以 mtime 都从合成头里的时刻推出来，不借共享的 T0_NS
    # （T0_NS 的年份换了，这条测试也不该跟着变）。
    d = tmp_path / "w"
    rec = parse_local("10.09.2001 15:07:12")
    sxm = synth.sxm(d / "a.sxm", np.zeros((8, 8)), rec_date="10.09.2001", rec_time="15:07:12",
                    acq_s=60.0, mtime_ns=int((rec + 60 + 5 * 60) * 1e9))   # 扫 60 s，扫完又过五分钟才存盘
    saved = int(parse_local("10.09.2001 19:06:08") * 1e9)       # 合成 .dat 头里的 Saved Date
    dat = synth.dat(d / "b.dat", ["Bias calc (V)", "Current (A)"], np.zeros((4, 2)), mtime_ns=saved)
    far = synth.dat(d / "far.dat", ["Bias calc (V)", "Current (A)"], np.zeros((4, 2)),
                    mtime_ns=saved + 25 * 365 * 86400 * 10**9)
    assert acquisition_window(far) is None              # 头里的开始时刻比 mtime 早 25 年：不可信
    g = synth.grid(d / "c.3ds", nx=2, ny=2, npts=3, channels={"Current (A)": np.zeros((2, 2, 3))})
    for p in (sxm, dat, g):
        w = acquisition_window(p)
        assert w is not None, p
        assert w.start == pytest.approx(inventory.SUMMARISERS[p.suffix](str(p))["t0"]), p
    w = acquisition_window(g)
    assert w.end == pytest.approx(inventory.SUMMARISERS[".3ds"](str(g))["t1"]) and w.end_from == "End time"
    w = acquisition_window(sxm)          # mtime 在扫完五分钟之后：终点按 REC + ACQ，不算空等
    assert w.duration_s == pytest.approx(60.0) and w.end_from == "REC+ACQ_TIME"


# ── 构建 ─────────────────────────────────────────────────────────────────


def test_a_sidecar_lands_in_the_index_with_tiny_pressures_intact(tmp_path, synth, gallery_dir, t0_ns):
    root = tmp_path / "SPM"
    p = _sxm(synth, root / DAY / "topo_0001.sxm", t0_ns)
    _tag(p)
    _configure(root)
    res = build.run_build(state=gallery_dir)
    assert res["phase"] == "done" and res["n_env"] == 1
    env = _items()["topo_0001.sxm"]["env"]
    assert env["s"] == "r"
    assert env["P"] == {"v": 3.1e-8, "u": "Pa", "c": 3.1e-8, "s": "l"}        # 没变成 0
    assert env["B"] == {"v": 20.0, "u": "kG", "c": 2.0, "s": "l"}             # 原单位 + 规范值
    assert env["T"]["v"] == 4.31


def test_a_field_change_during_the_scan_reaches_the_index_in_the_sensors_own_unit(
        tmp_path, synth, gallery_dir):
    from mast.envhistory.filetags.acq import parse_local

    root = tmp_path / "SPM"
    start = parse_local("10.09.2001 15:00:00")
    p = synth.sxm(root / DAY / "topo_0001.sxm", np.zeros((16, 16)), rec_date="10.09.2001",
                  rec_time="15:00:00", acq_s=60.0, mtime_ns=int((start + 70) * 1e9))
    st = p.stat()
    clk = [start - 10.0]
    ring = ReadingRing(clock=lambda: clk[0], horizon_s=1e9)
    while clk[0] <= start + 70:
        ring.write("b", 0.0 if clk[0] < start + 30 else 20.0, "kG", "ok")   # 扫到一半升场
        clk[0] += 2.0
    rec = build_record(data_path=str(p), size=st.st_size, mtime_ns=st.st_mtime_ns,
                       head_sha256=SC.head_sha256(p), acq=acquisition_window(p),
                       sensors={"b": K.SensorMeta("b", K.FIELD, "Model625Sensor")}, latest={},
                       ring=ring, store=None, max_age_s=30.0, origin="watcher",
                       observed_at=start + 72, now=start + 73)
    assert SC.write_sidecar(p, rec, size=st.st_size, mtime_ns=st.st_mtime_ns)[0] == "written"
    _configure(root)
    build.run_build(state=gallery_dir)
    env = _items()["topo_0001.sxm"]["env"]
    assert env["B"]["u"] == "kG" and env["B"]["v"] == 20.0 and env["B"]["c"] == 2.0
    assert env["wB"] == [0.0, 20.0]                    # 与值同一个单位，卡片上「0→20.0 kG」
    assert env["wr"] == ["B"]                          # 逐条读数算的窗口：卡片可以标「采集期间变过」


def test_only_windows_from_individual_readings_are_marked_as_covering_the_acquisition():
    """按 1 分钟桶算的窗口比采集期间宽：摘要里照样给 [min, max]，但不进 ``wr``。"""
    from mast.gallery.envtags import summary

    def rec(method: str) -> dict:
        return {"values": [{"kind": "field", "sensor": "b", "primary": True, "value": 20.0, "unit": "kG",
                            "canonical": 2.0, "source": "history", "fresh": True}],
                "window": [{"kind": "field", "sensor": "b", "unit": "kG", "min": 0.0, "max": 20.0,
                            "method": method}]}

    assert summary(rec("readings"), "sidecar")["wr"] == ["B"]
    for method in ("buckets", "buckets+readings"):
        s = summary(rec(method), "sidecar")
        assert s["wB"] == [0.0, 20.0] and "wr" not in s


def test_a_sidecar_written_after_the_first_build_shows_up_in_the_next_incremental_one(
        tmp_path, synth, gallery_dir, t0_ns):
    root = tmp_path / "SPM"
    p = _sxm(synth, root / DAY / "topo_0001.sxm", t0_ns)
    _configure(root)
    first = build.run_build(state=gallery_dir)
    assert first["n_render"] == 1 and "env" not in _items()["topo_0001.sxm"]
    _tag(p)                                            # 记录器晚了一步才写成
    second = build.run_build(state=gallery_dir)
    assert second["n_env"] == 1 and second["n_render"] == 0 and second["n_new"] == 0
    assert _items()["topo_0001.sxm"]["env"]["s"] == "r"
    third = build.run_build(state=gallery_dir)
    assert third["n_env"] == 0                          # 没再变就不报变化


def test_a_sidecar_that_describes_another_file_is_ignored(tmp_path, synth, gallery_dir, t0_ns):
    root = tmp_path / "SPM"
    p = _sxm(synth, root / DAY / "topo_0001.sxm", t0_ns)
    _tag(p)
    _sxm(synth, p, t0_ns + 3600 * 10**9)               # Nanonis 重用编号：同名的新测量
    os.utime(p, ns=(t0_ns + 3600 * 10**9, t0_ns + 3600 * 10**9))
    with open(p, "ab") as fh:                          # 大小也不同
        fh.write(b"\0" * 16)
    _configure(root)
    build.run_build(state=gallery_dir)
    assert "env" not in _items()["topo_0001.sxm"]


def test_a_copy_without_a_sidecar_is_found_through_the_central_record(tmp_path, synth, gallery_dir, t0_ns):
    root = tmp_path / "SPM"
    p = _sxm(synth, root / DAY / "topo_0001.sxm", t0_ns)
    rec = _tag(p)
    import json
    st = p.stat()
    get_store().put_file_tag(name=p.name, size=st.st_size, mtime_ns=st.st_mtime_ns,
                             tagged_path=str(p), tagged_at=1.0, payload=json.dumps(rec))
    SC.sidecar_path(p).unlink()                        # 只剩中央记录（例：只拷走了数据文件）
    _configure(root)
    build.run_build(state=gallery_dir)
    assert _items()["topo_0001.sxm"]["env"]["s"] == "r"


def test_an_old_file_without_a_record_is_inferred_from_the_history(tmp_path, synth, gallery_dir, t0_ns):
    root = tmp_path / "SPM"
    _sxm(synth, root / DAY / "topo_0001.sxm", t0_ns)
    t = t0_ns / 1e9
    b0 = (t // 60) * 60
    get_store().upsert_buckets([("DL-7 真空计 (COM15)", b0, 60.0, 30, 3.3e-8, 3.2e-8, 3.4e-8, 0.0,
                                 3.3e-8, "Pa", "ok", 0, None, None),
                                ("tunnel_current", b0, 60.0, 30, 1e-10, 1e-10, 1e-10, 0.0, 1e-10,
                                 "A", "ok", 0, None, None)])
    _configure(root)
    build.run_build(state=gallery_dir)
    env = _items()["topo_0001.sxm"]["env"]
    assert env["s"] == "i" and env["P"]["s"] == "i" and env["P"]["v"] == 3.3e-8
    assert set(env) - {"s", "wP"} == {"P"}             # 单位 A 的序列（针尖电流？）不用


def test_no_history_means_no_env(tmp_path, synth, gallery_dir, t0_ns):
    root = tmp_path / "SPM"
    _sxm(synth, root / DAY / "topo_0001.sxm", t0_ns)
    _configure(root)
    build.run_build(state=gallery_dir)
    assert "env" not in _items()["topo_0001.sxm"]


def test_the_build_writes_nothing_into_the_data_directory(tmp_path, synth, gallery_dir, t0_ns):
    root = tmp_path / "SPM"
    p = _sxm(synth, root / DAY / "topo_0001.sxm", t0_ns)
    _sxm(synth, root / DAY / "topo_0002.sxm", t0_ns + 10**9)
    _tag(p)
    _configure(root)
    before = _tree(root)
    build.run_build(state=gallery_dir)
    build.run_build(state=gallery_dir)
    assert _tree(root) == before


# ── API ──────────────────────────────────────────────────────────────────


def _client() -> TestClient:
    app = FastAPI()
    app.state.ctx = AppContext()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_env_tag_settings_have_their_own_endpoint_and_an_omitted_key_is_unchanged(gallery_dir):
    c = _client()
    body = c.get("/api/gallery/env-tags").json()
    assert (body["ok"], body["enabled"], body["max_age_s"]) == (True, True, 30.0)
    assert body["status"] is None                       # 这个进程里没有记录器
    assert not gallery_dir.exists()                     # 读不创建任何东西
    r = c.post("/api/gallery/env-tags", json={"enabled": False, "max_age_s": 90}).json()
    assert (r["ok"], r["enabled"], r["max_age_s"]) == (True, False, 90.0)
    r = c.post("/api/gallery/env-tags", json={"enabled": True}).json()
    assert (r["enabled"], r["max_age_s"]) == (True, 90.0)          # 省略的键不变
    assert c.post("/api/gallery/env-tags", json={"max_age_s": 1}).status_code == 422
    assert c.post("/api/gallery/env-tags").status_code == 422      # 请求体必填
    from mast.envhistory.filetags.settings import current
    assert current() == (True, 90.0)                    # 记录器读的就是这份（POST 清了缓存）


def test_saving_the_data_roots_leaves_the_env_tag_settings_alone(tmp_path, gallery_dir):
    """图库配置那条路由整条重写 config.json。设置不在那份文件里，所以保存数据根不会把它们重置。"""
    c = _client()
    data = tmp_path / "SPM"
    data.mkdir()
    c.post("/api/gallery/env-tags", json={"enabled": False, "max_age_s": 90})
    for extra in ({}, {"workers": 3}, {"auto_build": False}):
        r = c.post("/api/gallery/config", json={"roots": [{"name": "SPM", "path": str(data)}], **extra})
        assert r.json()["ok"] is True
    body = c.get("/api/gallery/env-tags").json()
    assert (body["enabled"], body["max_age_s"]) == (False, 90.0)
    assert "env_tags" not in (gallery_dir / "config.json").read_text(encoding="utf-8")


def test_a_missing_or_broken_settings_file_reads_the_defaults(gallery_dir):
    from mast.envhistory.filetags import settings as envset

    assert envset.load() == (True, 30.0)
    assert not gallery_dir.exists()
    gallery_dir.mkdir(parents=True)
    envset.settings_path().write_text("{not json", encoding="utf-8")
    assert envset.load() == (True, 30.0)
    envset.settings_path().write_text('{"version": 1, "enabled": "no", "max_age_s": 1e9}', encoding="utf-8")
    assert envset.load() == (True, 600.0)               # 字段不对按默认；阈值夹到 5–600


def test_changing_the_settings_reaches_the_recorder_at_once(gallery_dir):
    from mast.envhistory.filetags.tagger import FileEnvTagger, set_tagger

    set_tagger(FileEnvTagger(ring=None, latest_getter=dict, meta_getter=dict,
                             store_getter=lambda: None, start_worker=False))
    st = _client().post("/api/gallery/env-tags", json={"enabled": False, "max_age_s": 45}).json()["status"]
    assert (st["active"], st["enabled"], st["max_age_s"]) == (True, False, 45.0)


def test_detail_says_where_the_record_came_from(tmp_path, synth, gallery_dir, t0_ns):
    root = tmp_path / "SPM"
    p1 = _sxm(synth, root / DAY / "topo_0001.sxm", t0_ns)
    _sxm(synth, root / DAY / "topo_0002.sxm", t0_ns + 10**9)
    _tag(p1)
    _configure(root)
    build.run_build(state=gallery_dir)
    c = _client()
    d1 = c.get("/api/gallery/env/SPM/" + DAY + "/topo_0001.sxm").json()
    assert d1["how"] == "sidecar" and d1["sidecar_path"].endswith("topo_0001.sxm.json")
    vals = {v["kind"]: v for v in d1["record"]["values"]}
    assert vals["field"]["unit"] == "kG" and vals["field"]["canonical"] == 2.0
    assert d1["record"]["schema"] == "mast.envtags/1"
    d2 = c.get("/api/gallery/env/SPM/" + DAY + "/topo_0002.sxm").json()
    assert d2["how"] == "none" and d2["record"] is None and not d2["degraded"]
    d3 = c.get("/api/gallery/env/nope/x.sxm").json()
    assert d3["how"] == "none" and not d3["degraded"]


# ── 环境历史库只读────────────────────────────────────

_OLD_BUCKETS_DDL = (
    "CREATE TABLE env_buckets (sensor TEXT NOT NULL, bucket_ts REAL NOT NULL,"
    " dt_s REAL NOT NULL DEFAULT 60, n INTEGER NOT NULL, mean REAL, min REAL,"
    " max REAL, std REAL, last REAL, unit TEXT NOT NULL DEFAULT '',"
    " worst_status TEXT NOT NULL DEFAULT 'ok', n_excluded INTEGER NOT NULL DEFAULT 0,"
    " experiment_id TEXT, sample_id TEXT, PRIMARY KEY (sensor, bucket_ts, dt_s)) WITHOUT ROWID")


def test_the_env_step_does_not_wait_for_the_lock_the_monitor_flushes_under(tmp_path):
    """监视线程落库要拿 EnvHistoryStore 的锁；图库构建那一步走自己的只读连接，不排在它后面。"""
    from mast.gallery.envtags import update_env

    st = get_store()
    t_save = time.time() - 3600
    b0 = (t_save // 60) * 60
    st.upsert_buckets([("DL-7 vac", b0 + 60 * i, 60.0, 30, 3e-8, 3e-8, 3e-8, 0.0, 3e-8, "Pa", "ok", 0,
                        None, None) for i in range(-3, 3)])
    p = tmp_path / "w" / "SPM" / "Bias-Spectroscopy001.dat"
    p.parent.mkdir(parents=True)
    p.write_text("Experiment\tbias spectroscopy\n\n[DATA]\nBias (V)\tCurrent (A)\n0\t0\n", encoding="utf-8")
    os.utime(p, (t_save, t_save))
    s = p.stat()
    iid = "SPM/Bias-Spectroscopy001.dat"
    inv = {iid: {"path": str(p), "size": s.st_size, "mtime_ns": s.st_mtime_ns, "kind": "s"}}
    cache: dict = {}
    out: dict = {}
    th = threading.Thread(target=lambda: out.update(update_env(inv, cache, enabled={"SPM"}, max_age_s=30.0)))
    with st._lock:                                   # 监视线程正在落库（或别人在跑一条长查询）
        th.start()
        th.join(20)
        waited = th.is_alive()
    th.join(20)
    assert not waited, "the gallery env step waited for EnvHistoryStore's lock"
    assert out["inferred"] == 1 and cache[iid]["sum"]["P"]["v"] == 3e-8


def test_the_detail_route_does_not_migrate_or_open_an_old_library(tmp_path, gallery_dir, monkeypatch):
    """服务没开过环境历史（独立 API 进程）、磁盘上是个老库：GET 不迁移它，也不顺手开出单例。"""
    from mast.envhistory import store as S
    from mast.envhistory.filetags.reader import ro_uri

    monkeypatch.setattr(S, "_STORE", None)
    db = S._default_path()
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(str(db))
    con.execute(_OLD_BUCKETS_DDL)
    con.execute("PRAGMA user_version=2")
    con.commit()
    con.close()
    f = tmp_path / "w" / "Topo_001.sxm"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"SXM" * 100)
    gallery_dir.mkdir(parents=True)
    (gallery_dir / "index.json").write_text(json.dumps({"version": 1, "items": [
        {"id": "root/Topo_001.sxm", "k": "f", "p": str(f)}]}), encoding="utf-8")
    before = (sorted(p.name for p in db.parent.iterdir()), hashlib.sha256(db.read_bytes()).hexdigest())
    r = _client().get("/api/gallery/env/root/Topo_001.sxm")
    assert r.status_code == 200 and r.json()["how"] == "none" and not r.json()["degraded"]
    assert S._STORE is None
    assert (sorted(p.name for p in db.parent.iterdir()), hashlib.sha256(db.read_bytes()).hexdigest()) == before
    c = sqlite3.connect(ro_uri(db), uri=True)
    try:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
        assert [t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["env_buckets"]
    finally:
        c.close()


# ── 构建可取消、只去列有 .mast 的目录────────────────────────


def test_a_cancelled_build_stops_the_env_step_where_it_is_and_the_next_one_goes_on(tmp_path):
    from mast.gallery import envtags as GE

    n = GE.CANCEL_CHECK_EVERY * 2 + 3
    inv = {f"SPM/f{i:03d}.dat": {"path": str(tmp_path / "w" / f"f{i:03d}.dat"), "size": 10, "mtime_ns": 1}
           for i in range(n)}
    asked: list[int] = []

    def stop() -> bool:
        asked.append(1)
        return len(asked) > 1                          # 第一次放行；做完一批再问时已经取消了

    cache: dict = {}
    out = GE.update_env(inv, cache, enabled={"SPM"}, store=None, max_age_s=30.0, should_stop=stop)
    assert out["cancelled"] is True and len(cache) == GE.CANCEL_CHECK_EVERY
    out = GE.update_env(inv, cache, enabled={"SPM"}, store=None, max_age_s=30.0)
    assert out["cancelled"] is False and len(cache) == n


def test_the_env_step_lists_sidecars_only_where_the_inventory_saw_a_mast_folder(tmp_path, monkeypatch):
    from mast.gallery import envtags as GE

    a, b, c = (tmp_path / "w" / x for x in ("A", "B", "C"))
    inv = {f"SPM/{d.name}/x.dat": {"path": str(d / "x.dat"), "size": 10, "mtime_ns": 1} for d in (a, b, c)}
    norm = lambda p: os.path.normcase(os.path.abspath(str(p)))    # noqa: E731
    asked: list = []
    monkeypatch.setattr(GE, "list_sidecars", lambda dirs: asked.append({norm(d) for d in dirs}) or {})
    GE.update_env(inv, {}, enabled={"SPM"}, store=None, max_age_s=30.0,
                  dir_has_mast={norm(a): True, norm(b): False})      # C 这次没走到：照常列
    assert asked == [{norm(a), norm(c)}]


def test_the_inventory_notes_which_directories_hold_a_mast_folder(tmp_path):
    root = tmp_path / "SPM"
    (root / "day1" / ".mast" / "envtags").mkdir(parents=True)
    (root / "day2").mkdir(parents=True)
    cfg = gcfg.GalleryConfig(roots=[gcfg.RootSpec(name="SPM", path=str(root))])
    seen: dict = {}
    inventory.walk_roots(cfg, mast_dirs=seen)
    norm = lambda p: os.path.normcase(os.path.abspath(str(p)))    # noqa: E731
    assert seen[norm(root / "day1")] is True and seen[norm(root / "day2")] is False
    assert seen[norm(root)] is False
    assert norm(root / "day1" / ".mast") not in seen                # .mast 本身不进清点
