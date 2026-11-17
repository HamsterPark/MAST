# -*- coding: utf-8 -*-
"""环境历史库的只读连接：图库构建与详情用它，不用 EnvHistoryStore。

EnvHistoryStore 开库就迁移（建表、补列、改 user_version、切 WAL），查询还都排在环境监视线程落库
用的那把锁后面。只读连接：不改库、不建库、不拿那把锁，候选序列按主键逐个探测而不是全表 GROUP BY。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from mast.envhistory import store as S
from mast.envhistory.filetags.reader import ReadOnlyEnvHistory, open_reader_if_present, ro_uri

_OLD_BUCKETS_DDL = (
    "CREATE TABLE env_buckets (sensor TEXT NOT NULL, bucket_ts REAL NOT NULL,"
    " dt_s REAL NOT NULL DEFAULT 60, n INTEGER NOT NULL, mean REAL, min REAL,"
    " max REAL, std REAL, last REAL, unit TEXT NOT NULL DEFAULT '',"
    " worst_status TEXT NOT NULL DEFAULT 'ok', n_excluded INTEGER NOT NULL DEFAULT 0,"
    " experiment_id TEXT, sample_id TEXT, PRIMARY KEY (sensor, bucket_ts, dt_s)) WITHOUT ROWID")


def _old_library(path: Path, rows) -> Path:
    """一个老库：只有桶表、user_version 2、默认的回滚日志（不是 WAL）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute(_OLD_BUCKETS_DDL)
    con.executemany("INSERT INTO env_buckets (sensor, bucket_ts, dt_s, n, mean, min, max, unit)"
                    " VALUES (?,?,?,?,?,?,?,?)", rows)
    con.execute("PRAGMA user_version=2")
    con.commit()
    con.close()
    return path


def _fingerprint(db: Path):
    c = sqlite3.connect(ro_uri(db), uri=True)
    try:
        tables = sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        user_version = c.execute("PRAGMA user_version").fetchone()[0]
        journal = c.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        c.close()
    return (tables, user_version, journal, sorted(p.name for p in db.parent.iterdir()),
            hashlib.sha256(db.read_bytes()).hexdigest())


def test_reading_an_old_library_changes_nothing(tmp_path):
    db = _old_library(tmp_path / "eh" / "env_history.sqlite",
                      [("stage", 1000.0, 60.0, 30, 4.3, 4.2, 4.4, "K")])
    before = _fingerprint(db)
    with ReadOnlyEnvHistory(db) as r:
        assert r.find_file_tags("Topo_001.sxm", 300) == []          # 没有这张表：当作没有中央记录
        assert [x["mean"] for x in r.buckets_between(["stage"], 0, 2000)] == [4.3]
        assert r.list_sensors() == [{"sensor": "stage", "unit": "K"}]
    assert _fingerprint(db) == before
    assert before[0] == ["env_buckets"] and before[1] == 2


def test_the_connection_cannot_write(tmp_path):
    db = _old_library(tmp_path / "eh" / "env_history.sqlite", [("s", 1.0, 60.0, 1, 1.0, 1.0, 1.0, "K")])
    with ReadOnlyEnvHistory(db) as r:
        with pytest.raises(sqlite3.Error):
            r._conn.execute("CREATE TABLE file_env_tags (a)")
        with pytest.raises(sqlite3.Error):
            r._conn.execute("PRAGMA user_version=3")


def test_candidate_series_come_from_index_probes_not_a_group_by(tmp_path):
    rows = [("b_vac", 1000.0 + 60 * i, 60.0, 30, 3e-8, 3e-8, 3e-8, "Pa") for i in range(3)]
    rows.append(("b_vac", 1180.0, 60.0, 0, None, None, None, ""))       # 最后一个桶读不到：没有单位
    rows += [("a_stage", 1000.0 + 60 * i, 60.0, 30, 4.3, 4.3, 4.3, "K") for i in range(3)]
    db = _old_library(tmp_path / "eh" / "env_history.sqlite", rows)
    statements: list[str] = []
    with ReadOnlyEnvHistory(db) as r:
        r._conn.set_trace_callback(statements.append)
        got = r.list_sensors()
    assert got == [{"sensor": "a_stage", "unit": "K"}, {"sensor": "b_vac", "unit": "Pa"}]
    assert statements and not any("GROUP BY" in s.upper() for s in statements)


def test_a_path_with_spaces_and_a_hash_opens(tmp_path):
    db = _old_library(tmp_path / "a b#c" / "env history.sqlite", [("s", 1.0, 60.0, 1, 1.0, 1.0, 1.0, "K")])
    with ReadOnlyEnvHistory(db) as r:
        assert r.series_names() == ["s"]
    assert ro_uri(db).startswith("file:") and ro_uri(db).endswith("?mode=ro")
    assert "%20" in ro_uri(db) and "%23" in ro_uri(db)


def test_no_library_means_no_reader_and_nothing_is_created(monkeypatch):
    monkeypatch.setattr(S, "_STORE", None)
    p = S._default_path()
    assert not p.exists()
    assert open_reader_if_present() is None
    assert not p.exists()


def test_the_reader_sees_what_the_live_store_has_committed():
    """服务进程里：环境监视线程用 EnvHistoryStore（WAL）写，图库另开只读连接读同一个文件。"""
    st = S.get_store()
    st.upsert_buckets([("vac", 1000.0, 60.0, 30, 3e-8, 3e-8, 3e-8, 0.0, 3e-8, "Pa", "ok", 0, None, None)])
    r = open_reader_if_present()
    assert r is not None
    try:
        assert Path(r.path) == Path(st.path)
        assert [x["sensor"] for x in r.buckets_between(["vac"], 0, 2000)] == ["vac"]
    finally:
        r.close()
