# -*- coding: utf-8 -*-
"""环境历史库里的中央记录表与原生桶查询（设计 §4.4）。"""
from __future__ import annotations

import sqlite3

from mast.envhistory.store import EnvHistoryStore


def test_an_old_database_gets_the_new_table_on_open(tmp_path):
    db = tmp_path / "eh" / "env_history.sqlite"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE env_buckets (sensor TEXT NOT NULL, bucket_ts REAL NOT NULL,"
                      " dt_s REAL NOT NULL DEFAULT 60, n INTEGER NOT NULL, mean REAL, min REAL,"
                      " max REAL, std REAL, last REAL, unit TEXT NOT NULL DEFAULT '',"
                      " worst_status TEXT NOT NULL DEFAULT 'ok', n_excluded INTEGER NOT NULL DEFAULT 0,"
                      " experiment_id TEXT, sample_id TEXT, PRIMARY KEY (sensor, bucket_ts, dt_s))"
                      " WITHOUT ROWID; PRAGMA user_version=2;")
    con.commit()
    con.close()
    st = EnvHistoryStore(db)
    assert st.put_file_tag(name="A.sxm", size=10, mtime_ns=5, tagged_path="x", tagged_at=1.0,
                           payload="{}")
    assert len(st.find_file_tags("a.SXM", 10)) == 1
    st.close()


def test_a_record_is_written_once(tmp_path):
    st = EnvHistoryStore(tmp_path / "eh.sqlite")
    assert st.put_file_tag(name="Topo.sxm", size=7, mtime_ns=9, tagged_path="D:/a/Topo.sxm",
                           tagged_at=1.0, payload='{"v": 1}', sidecar_path="D:/a/.mast/envtags/Topo.sxm.json")
    assert st.put_file_tag(name="Topo.sxm", size=7, mtime_ns=9, tagged_path="E:/copy/Topo.sxm",
                           tagged_at=2.0, payload='{"v": 2}', sidecar_error="read-only")
    (row,) = st.find_file_tags("topo.sxm", 7)
    assert (row["payload"], row["tagged_path"], row["sidecar_error"]) == ('{"v": 1}', "D:/a/Topo.sxm", None)
    assert st.find_file_tags("topo.sxm", 8) == []
    st.close()


def test_buckets_between_returns_native_buckets_with_their_width(tmp_path):
    st = EnvHistoryStore(tmp_path / "eh.sqlite")
    st.upsert_buckets([("b", 60.0 * i, 60.0, 30, 1.0, 0.5, 1.5, 0.1, 1.0, "T", "ok", 0, None, None)
                       for i in range(10)] +
                      [("t", 120.0, 300.0, 5, 4.2, 4.1, 4.3, 0.0, 4.2, "K", "warning", 1, None, None)])
    rows = st.buckets_between(["b", "t"], 100.0, 300.0)
    assert [(r["sensor"], r["bucket_ts"], r["dt_s"]) for r in rows] == [
        ("b", 120.0, 60.0), ("b", 180.0, 60.0), ("b", 240.0, 60.0), ("b", 300.0, 60.0),
        ("t", 120.0, 300.0)]
    assert rows[-1]["worst_status"] == "warning" and rows[-1]["mean"] == 4.2
    assert st.buckets_between([], 0, 1e12) == []
    st.close()
