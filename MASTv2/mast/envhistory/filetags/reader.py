# -*- coding: utf-8 -*-
"""环境历史库的**只读**连接：图库构建与详情（``GET /api/gallery/env/…``）用它，不用 EnvHistoryStore。

EnvHistoryStore 不适合这两条路：

* 它开库就跑建表、补列、``PRAGMA user_version``、切 WAL —— 一条 GET 不该改库（老库会被迁移）；
* 它的所有查询都排在同一把 Python 锁后面，环境监视线程翻桶落库也要拿这把锁。图库构建里一条
  全表 ``GROUP BY``（``list_sensors``）在一年的库上要几秒，这几秒里监视线程就落不了库。

这里用 SQLite 的只读 URI（``mode=ro``，外加 ``PRAGMA query_only``）单开一个连接：不建表、不改
``user_version``、不切 journal 模式；库文件不存在就不开（不新建）。表或列不存在（老库）⇒ 当作没有：
没有中央记录、没有桶。候选序列按主键 ``(sensor, bucket_ts, dt_s)`` 逐个探测（skip-scan），不做全表
``GROUP BY``。连接归调用方：用完 ``close()``（或 ``with``）。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

#: 候选序列最多探这么多条（通常十几条；防一个坏库把构建拖住）。
MAX_SERIES = 512


def ro_uri(path) -> str:
    """``path`` 的只读 SQLite URI。盘符路径、POSIX 路径、UNC 路径都要能用；特殊字符按 URI 转义。"""
    from urllib.parse import quote

    p = str(Path(path).absolute()).replace("\\", "/")
    if p.startswith("//"):            # UNC：权威部分留空，路径本身以 // 开头 ⇒ file:////server/share/…
        p = "//" + p
    elif not p.startswith("/"):       # D:/x ⇒ /D:/x
        p = "/" + p
    return "file:" + quote(p, safe="/:") + "?mode=ro"


def _f_or_none(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


class ReadOnlyEnvHistory:
    """一个只读连接。方法的签名与返回形状和 EnvHistoryStore 的同名方法一样；出错一律当作没有。"""

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._conn = sqlite3.connect(ro_uri(self._path), uri=True, timeout=2.0,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA query_only=1")
        except sqlite3.Error:
            pass

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "ReadOnlyEnvHistory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── 与 EnvHistoryStore 同名的读方法 ────────────────────────────────────

    def find_file_tags(self, name: str, size: int) -> list[dict]:
        try:
            rows = self._conn.execute(
                "SELECT name_lc, size, mtime_ns, tagged_path, tagged_at, sidecar_path,"
                " sidecar_error, payload FROM file_env_tags WHERE name_lc = ? AND size = ?"
                " ORDER BY mtime_ns", [str(name).lower(), int(size)],
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:                    # 老库没有这张表：没有中央记录
            return []

    def buckets_between(self, sensors: Sequence[str], since: float, until: float) -> list[dict]:
        names = [str(s) for s in (sensors or ()) if s]
        if not names:
            return []
        try:
            rows = self._conn.execute(
                "SELECT sensor, bucket_ts, dt_s, n, mean, min, max, unit, worst_status"
                f" FROM env_buckets WHERE sensor IN ({','.join('?' * len(names))})"
                " AND bucket_ts >= ? AND bucket_ts <= ? ORDER BY sensor, bucket_ts",
                [*names, float(since), float(until)],
            ).fetchall()
        except sqlite3.Error:
            return []
        return [{"sensor": str(r["sensor"]), "bucket_ts": float(r["bucket_ts"]),
                 "dt_s": float(r["dt_s"] or 0.0), "n": int(r["n"] or 0),
                 "mean": _f_or_none(r["mean"]), "min": _f_or_none(r["min"]),
                 "max": _f_or_none(r["max"]), "unit": str(r["unit"] or ""),
                 "worst_status": str(r["worst_status"] or "ok")} for r in rows]

    def series_names(self, limit: int = MAX_SERIES) -> list[str]:
        """库里出现过的序列名，按主键逐个往后探（每步一次索引查找），不扫全表。"""
        out: list[str] = []
        last: str | None = None
        try:
            while len(out) < limit:
                if last is None:
                    row = self._conn.execute(
                        "SELECT sensor FROM env_buckets ORDER BY sensor LIMIT 1").fetchone()
                else:
                    row = self._conn.execute(
                        "SELECT sensor FROM env_buckets WHERE sensor > ? ORDER BY sensor LIMIT 1",
                        [last]).fetchone()
                if row is None:
                    break
                last = str(row["sensor"])
                out.append(last)
        except sqlite3.Error:
            return out
        return out

    def list_sensors(self) -> list[dict]:
        """``[{sensor, unit}]``：每条序列最近一个带单位的桶的单位。逐条索引查找，不做全表 GROUP BY。"""
        out: list[dict] = []
        for name in self.series_names():
            try:
                row = self._conn.execute(
                    "SELECT unit FROM env_buckets WHERE sensor = ? AND unit != ''"
                    " ORDER BY bucket_ts DESC LIMIT 1", [name]).fetchone()
            except sqlite3.Error:
                row = None
            out.append({"sensor": name, "unit": str(row["unit"]) if row is not None else ""})
        return out


def store_path_if_present() -> Path | None:
    """环境历史库文件：本进程已开的那个（服务进程、测试换上的库），或默认位置上已有的；都没有 ⇒ None。

    只看路径，**不开** EnvHistoryStore（不迁移、不新建）。"""
    try:
        from mast.envhistory import store as S
    except Exception:  # noqa: BLE001
        return None
    st = S._STORE                                # noqa: SLF001
    if st is not None:
        try:
            return Path(st.path)
        except Exception:  # noqa: BLE001
            return None
    try:
        p = S._default_path()                    # noqa: SLF001
        return p if p.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def open_reader_if_present() -> ReadOnlyEnvHistory | None:
    """只读打开环境历史库；库不存在或打不开 ⇒ None。调用方负责 ``close()``。"""
    p = store_path_if_present()
    if p is None or not p.is_file():
        return None
    try:
        return ReadOnlyEnvHistory(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("env history: read-only open failed for %s: %s", p, exc)
        return None


__all__ = ["MAX_SERIES", "ReadOnlyEnvHistory", "open_reader_if_present", "ro_uri",
           "store_path_if_present"]
