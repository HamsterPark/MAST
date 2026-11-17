# -*- coding: utf-8 -*-
"""图库用：没有保存时记录的数据文件，按环境历史**推断**（设计 §6.1）。

本功能上线前存的旧文件没有 sidecar；图库**不回头补写**（对数据目录只读），而是在构建时按
环境历史库里的 1 分钟桶推一份同样形状的记录，每个值 ``source="inferred"``，界面明确标「推断」。

口径比保存时更严：只用「包含保存时刻、或边沿在 ``max_age_s`` 以内的桶」，**不出过期值** ——
一个推断出来、又过了期的数，告诉人的东西太少，却像一条数据。窗口只用桶（图库进程里没有
读数环），如实写覆盖区间（会比窗口宽，桶是整分钟的）。

种类只能按**单位**判（桶里没有驱动类），所以单位 A 的序列（针尖电流还是磁体电流分不出）、
空单位、认不出的单位、合成序列 ``instrument_quiet`` 都不用。
"""
from __future__ import annotations

import os
from typing import Any, Iterable

from mast.envhistory.filetags import kinds as K
from mast.envhistory.filetags import snapshot as S
from mast.envhistory.filetags.acq import AcqWindow


def candidates_from_store(store: Any) -> list[K.SensorMeta]:
    """环境历史里出现过、能按单位归类的序列。**每次构建调一次**。

    图库传进来的是只读连接（``reader.ReadOnlyEnvHistory``），它的 ``list_sensors`` 按主键逐个探测；
    EnvHistoryStore 的同名方法是全表 GROUP BY，还要拿监视线程落库用的锁 —— 图库不该用它。"""
    if store is None:
        return []
    try:
        rows = store.list_sensors()
    except Exception:  # noqa: BLE001
        return []
    out: list[K.SensorMeta] = []
    for r in rows:
        m = K.classify_unit_only(str(r.get("sensor") or ""), str(r.get("unit") or ""))
        if not m.excluded:
            out.append(m)
    return out


def infer_record(data_path: str, *, size: int, mtime_ns: int, store: Any,
                 candidates: Iterable[K.SensorMeta], max_age_s: float = 30.0,
                 acq: AcqWindow | None = None, now: float | None = None) -> dict | None:
    """一份 ``source="inferred"`` 的记录；环境历史在那个时刻什么都没有 ⇒ None。"""
    cands = [c for c in candidates if not c.excluded]
    if store is None or not cands:
        return None
    save_t = int(mtime_ns) / 1e9
    try:
        rows = store.buckets_between([c.name for c in cands],
                                     save_t - S.BUCKET_LOOKBACK_S - max_age_s, save_t + max_age_s)
    except Exception:  # noqa: BLE001
        return None
    values: list[dict] = []
    windows: list[dict] = []
    for meta in cands:
        v = S.history_value(meta, rows, save_t, max_age_s, source="inferred")
        if v is not None:
            values.append(v)
        if acq is not None:
            w = S.window_for(meta, [], acq, store)
            if w is not None:
                windows.append(w)
    if not values and not windows:
        return None
    K.mark_primary(values)
    rec = S.build_record(data_path=data_path, size=size, mtime_ns=mtime_ns, head_sha256="",
                         acq=acq, sensors={}, latest={}, ring=None, store=None,
                         max_age_s=max_age_s, origin="inferred", observed_at=save_t,
                         now=save_t if now is None else now)
    rec["values"], rec["window"], rec["missing"] = values, windows, []
    rec["inferred"] = True
    rec["file"]["name"] = os.path.basename(str(data_path))
    return rec


__all__ = ["candidates_from_store", "infer_record"]
