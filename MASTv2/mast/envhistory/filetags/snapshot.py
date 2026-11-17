# -*- coding: utf-8 -*-
"""组装一份环境记录（sidecar 的内容，schema ``mast.envtags/1``；设计 §4）。

来源全部由调用方注入：传感器归类表（``{name: SensorMeta}``，只看类型与字段得来）、
``get_latest()`` 的拷贝、读数环、环境历史库。**这里不碰任何设备**——
读不到就是读不到，过期了就照记读数时刻与早了多久，**绝不为了刷新去重读**。

每个传感器取值的顺序：

1. 新鲜的缓存读数（``source="live"``）：可收状态、``|t − 保存时刻| <= max_age_s``，最近的一条；
2. 环境历史桶（``source="history"``）：包含保存时刻的桶，或边沿在 ``max_age_s`` 以内的桶；
3. 过期值（``fresh=false``）：保存时刻之前最后一条可收读数或 24 h 内最后一个桶，取更近的那个；
4. 都没有 ⇒ 不出值，进 ``missing``（``invalid`` + 当时的状态，或 ``no_reading``）。

采集窗口：读数环从窗口起点之前就在记这个传感器 ⇒ 逐条读数；否则库里的桶管前段、
读数环管还没落库的尾段，如实写实际覆盖的区间、``method`` 与 ``complete``。
"""
from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any, Mapping

from mast.envhistory.filetags import kinds as K
from mast.envhistory.filetags.acq import AcqWindow
from mast.envhistory.filetags.ring import (
    ADMITTED, earliest, last_before, nearest, nearest_any, window_stats,
)

SCHEMA = "mast.envtags/1"
#: 过期值最多往回找多远（只对桶；读数环本身只有约 2.5 h）。
STALE_LOOKBACK_S = 86400.0
#: 桶最宽可以是 1 h（``eh_bucket_s`` 上限）：查包含某时刻的桶要往前多看这么多。
BUCKET_LOOKBACK_S = 3600.0
#: 窗口「盖住了」的最小容差：监控 2 s 一拍，首末两条读数离窗口边沿几秒是常态。
TOL_MIN_S = 5.0
#: mtime 比 MAST 看到这个文件的时刻还晚这么多 ⇒ 记时钟偏差。
CLOCK_SKEW_NOTE_S = 5.0

_KIND_ORDER = {k: i for i, k in enumerate(K.KINDS)}


def iso(t: float) -> str:
    """epoch 秒 → 带本机 UTC 偏移的 ISO 串（毫秒）。"""
    return datetime.fromtimestamp(float(t)).astimezone().isoformat(timespec="milliseconds")


def _r3(x: float) -> float:
    return round(float(x), 3)


def _r2(x: float) -> float:
    return round(float(x), 2)


def reading_entry(reading: Any) -> tuple | None:
    """``get_latest()`` 里的一条 ``SensorReading`` → ``(t, value, unit, status)``。

    时间戳读不出 ⇒ None：不知道多旧的数不能当新鲜的用，也说不出它早了多久。"""
    if reading is None:
        return None
    try:
        t = datetime.fromisoformat(str(getattr(reading, "timestamp", ""))).timestamp()
        v = float(getattr(reading, "value"))
    except (TypeError, ValueError, OverflowError, AttributeError):
        return None
    return (t, v, str(getattr(reading, "unit", "") or ""), str(getattr(reading, "status", "") or ""))


# ── 环境历史桶 ──────────────────────────────────────────────────────────


def _bucket_rows(store: Any, name: str, since: float, until: float) -> list[dict]:
    if store is None:
        return []
    try:
        rows = store.buckets_between([name], since, until)
    except Exception:  # noqa: BLE001 — 库读不了就当没有历史
        return []
    return [r for r in rows if r.get("sensor") == name]


def _bucket_ok(r: dict) -> bool:
    try:
        return int(r.get("n") or 0) > 0 and math.isfinite(float(r.get("mean")))
    except (TypeError, ValueError):
        return False


def _signed_to_bucket(r: dict, t: float) -> float:
    """保存时刻落在桶里 ⇒ 0；否则到最近桶沿的带符号距离（读数时刻 − 保存时刻）。"""
    b0 = float(r["bucket_ts"])
    b1 = b0 + float(r.get("dt_s") or 0.0)
    if b0 <= t < b1:
        return 0.0
    return (b0 - t) if t < b0 else (b1 - t)


def bucket_at(rows: list[dict], t: float, max_age_s: float) -> tuple[dict, float] | None:
    """包含 ``t`` 的桶；没有就取边沿离 ``t`` 最近、且在 ``max_age_s`` 以内的桶。

    恰好在 ``t`` 结束的前一个桶与从 ``t`` 开始的桶距离都是 0 —— 真正包含 ``t`` 的那个优先。"""
    best, best_key = None, None
    for r in rows:
        if not _bucket_ok(r):
            continue
        d = _signed_to_bucket(r, t)
        if abs(d) > max_age_s:
            continue
        b0 = float(r["bucket_ts"])
        contains = b0 <= t < b0 + float(r.get("dt_s") or 0.0)
        key = (abs(d), 0 if contains else 1)
        if best_key is None or key < best_key:
            best, best_key = (r, d), key
    return best


def last_bucket_before(rows: list[dict], t: float) -> tuple[dict, float] | None:
    """在 ``t`` 之前（含）结束的最后一个有值的桶。"""
    best = None
    for r in rows:
        if not _bucket_ok(r):
            continue
        end = float(r["bucket_ts"]) + float(r.get("dt_s") or 0.0)
        if end <= t and (best is None or end > best[1] + t):
            best = (r, end - t)
    return best


# ── 一个值 ──────────────────────────────────────────────────────────────


def _value(meta: K.SensorMeta, *, value: float, unit: str, status: str, source: str,
           fresh: bool, t: float, span_s: float, dt_s: float, bucket: dict | None = None) -> dict:
    unit = unit or meta.unit
    canon, canon_unit = K.to_canonical(value, unit)
    out = {
        "sensor": meta.name, "driver": meta.driver, "kind": meta.kind,
        "value": K.sig(value), "unit": unit,
        "canonical": K.sig(canon), "canonical_unit": canon_unit,
        "status": status, "source": source, "fresh": bool(fresh),
        "t": _r3(t), "iso": iso(t), "span_s": _r2(span_s), "dt_s": _r2(dt_s),
    }
    if bucket is not None:
        out["bucket"] = bucket
    out["primary"] = False
    return out


def _from_entry(meta: K.SensorMeta, e: tuple, save_t: float, *, fresh: bool) -> dict:
    return _value(meta, value=e[1], unit=e[2], status=e[3], source="live", fresh=fresh,
                  t=e[0], span_s=0.0, dt_s=e[0] - save_t)


def _from_bucket(meta: K.SensorMeta, r: dict, signed: float, *, fresh: bool) -> dict:
    ws = str(r.get("worst_status") or "ok")
    return _value(meta, value=float(r["mean"]), unit=str(r.get("unit") or ""), status=ws,
                  source="history", fresh=fresh, t=float(r["bucket_ts"]),
                  span_s=float(r.get("dt_s") or 0.0), dt_s=signed,
                  bucket={"n": int(r.get("n") or 0), "min": K.sig(r.get("min")),
                          "max": K.sig(r.get("max")), "worst_status": ws})


def history_value(meta: K.SensorMeta, rows: list[dict], save_t: float, max_age_s: float,
                  *, source: str = "history") -> dict | None:
    """包含保存时刻（或边沿在 ``max_age_s`` 以内）的那个桶的均值；没有 ⇒ None。

    ``rows`` 是这个传感器在保存时刻附近的原生桶（``store.buckets_between``）。图库推断旧文件时
    也用它（``source="inferred"``）—— 推断只用这一档，不出过期值。"""
    hit = bucket_at([r for r in rows if r.get("sensor") == meta.name], save_t, max_age_s)
    if hit is None:
        return None
    v = _from_bucket(meta, hit[0], hit[1], fresh=True)
    v["source"] = source
    return v


def value_for(meta: K.SensorMeta, entries: list[tuple], save_t: float, max_age_s: float,
              store: Any) -> dict | None:
    e = nearest(entries, save_t, max_age_s)
    if e is not None:
        return _from_entry(meta, e, save_t, fresh=True)
    rows = _bucket_rows(store, meta.name, save_t - BUCKET_LOOKBACK_S - max_age_s,
                        save_t + max_age_s)
    hv = history_value(meta, rows, save_t, max_age_s)
    if hv is not None:
        return hv
    # 过期值：照记，写明早了多久 —— 不刷新、不重读
    old = last_before(entries, save_t)
    old_rows = _bucket_rows(store, meta.name, save_t - STALE_LOOKBACK_S, save_t)
    old_b = last_bucket_before(old_rows, save_t)
    if old is not None and (old_b is None or old[0] >= save_t + old_b[1]):
        return _from_entry(meta, old, save_t, fresh=False)
    if old_b is not None:
        return _from_bucket(meta, old_b[0], old_b[1], fresh=False)
    return None


def missing_for(meta: K.SensorMeta, entries: list[tuple], save_t: float, max_age_s: float) -> dict:
    e = nearest_any(entries, save_t, max_age_s)
    if e is not None and e[3] not in ADMITTED:
        return {"sensor": meta.name, "kind": meta.kind, "why": "invalid", "status": e[3]}
    return {"sensor": meta.name, "kind": meta.kind, "why": "no_reading"}


# ── 采集窗口 ────────────────────────────────────────────────────────────


def window_for(meta: K.SensorMeta, entries: list[tuple], acq: AcqWindow, store: Any) -> dict | None:
    t0, t1 = acq.start, acq.end
    first = earliest(entries)
    if first is not None and first <= t0 + TOL_MIN_S:
        st = window_stats(entries, t0, t1)
        if st is None:
            return None
        lo, hi, n, frm, to, unit = st["min"], st["max"], st["n"], st["first"], st["last"], st["unit"]
        method, tol = "readings", TOL_MIN_S
    else:
        rows = [r for r in _bucket_rows(store, meta.name, t0 - BUCKET_LOOKBACK_S, t1)
                if _bucket_ok(r) and float(r["bucket_ts"]) + float(r.get("dt_s") or 0.0) > t0
                and float(r["bucket_ts"]) <= t1]
        lo = hi = frm = to = None
        n, unit, width = 0, "", 0.0
        for r in rows:
            for v, pick in ((r.get("min"), min), (r.get("max"), max)):
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(fv):
                    if pick is min:
                        lo = fv if lo is None else min(lo, fv)
                    else:
                        hi = fv if hi is None else max(hi, fv)
            b0 = float(r["bucket_ts"])
            dt = float(r.get("dt_s") or 0.0)
            n += int(r.get("n") or 0)
            frm = b0 if frm is None else min(frm, b0)
            to = b0 + dt if to is None else max(to, b0 + dt)
            unit = str(r.get("unit") or "") or unit
            width = max(width, dt)
        # 还没落库的那一段（当前这一分钟）由读数环补上
        st = window_stats(entries, max(to if to is not None else t0, t0), t1)
        if st is not None:
            lo = st["min"] if lo is None else min(lo, st["min"])
            hi = st["max"] if hi is None else max(hi, st["max"])
            n += st["n"]
            frm = st["first"] if frm is None else frm
            to = st["last"] if to is None else max(to, st["last"])
            unit = st["unit"] or unit
        if rows and st is not None:
            method = "buckets+readings"
        elif rows:
            method = "buckets"
        elif st is not None:
            method = "readings"
        else:
            return None
        tol = max(TOL_MIN_S, width)
    unit = unit or meta.unit
    cmin, cu = K.to_canonical(lo, unit)
    cmax, _ = K.to_canonical(hi, unit)
    return {
        "sensor": meta.name, "kind": meta.kind, "unit": unit,
        "min": K.sig(lo), "max": K.sig(hi),
        "canonical_min": K.sig(cmin), "canonical_max": K.sig(cmax), "canonical_unit": cu,
        "n": int(n), "from": _r3(frm), "to": _r3(to), "method": method,
        "complete": bool(frm <= t0 + tol and to >= t1 - tol),
    }


# ── 一份记录 ────────────────────────────────────────────────────────────


def _metas(sensors: Mapping[str, K.SensorMeta] | None, latest: Mapping[str, Any],
           ring: Any) -> dict[str, K.SensorMeta]:
    """要看的传感器。有监视器的归类表就只看它（权威集合；重扫改名留在读数环里的旧名不算）；
    没有时才退到读数环与缓存里出现过的名字，按单位归类。"""
    if sensors:
        return {n: m for n, m in sensors.items() if not m.excluded}
    out: dict[str, K.SensorMeta] = {}
    names = set(latest or {}) | set(ring.names() if ring is not None else ())
    for name in names:
        unit = ""
        le = reading_entry((latest or {}).get(name))
        if le is not None:
            unit = le[2]
        elif ring is not None:
            snap = ring.snapshot(name)
            unit = snap[-1][2] if snap else ""
        m = K.classify_unit_only(name, unit)
        if not m.excluded:
            out[name] = m
    return out


def build_record(*, data_path: str, size: int, mtime_ns: int, head_sha256: str,
                 acq: AcqWindow | None, sensors: Mapping[str, K.SensorMeta] | None,
                 latest: Mapping[str, Any] | None, ring: Any, store: Any,
                 max_age_s: float, origin: str, observed_at: float, now: float,
                 host: str = "", mast_version: str = "") -> dict:
    """一份 ``mast.envtags/1`` 记录。什么值都没有、也没有窗口时 ``values`` 与 ``window`` 为空 ——
    要不要写由调用方决定。"""
    save_t = int(mtime_ns) / 1e9
    latest = dict(latest or {})
    metas = _metas(sensors, latest, ring)
    values: list[dict] = []
    windows: list[dict] = []
    missing: list[dict] = []
    for name in sorted(metas, key=lambda n: (_KIND_ORDER.get(metas[n].kind, 99), n)):
        meta = metas[name]
        entries = ring.snapshot(name) if ring is not None else []
        le = reading_entry(latest.get(name))
        if le is not None and not any(abs(e[0] - le[0]) < 1e-3 for e in entries[-4:]):
            entries.append(le)
        v = value_for(meta, entries, save_t, float(max_age_s), store)
        if v is not None:
            values.append(v)
        else:
            missing.append(missing_for(meta, entries, save_t, float(max_age_s)))
        if acq is not None:
            w = window_for(meta, entries, acq, store)
            if w is not None:
                windows.append(w)
    K.mark_primary(values)
    ext = os.path.splitext(str(data_path))[1].lower().lstrip(".")
    skew = save_t - float(observed_at)
    ring_s = None
    try:
        ring_s = float(ring.stats().get("horizon_s")) if ring is not None else None
    except Exception:  # noqa: BLE001
        ring_s = None
    return {
        "schema": SCHEMA,
        "file": {
            "name": os.path.basename(str(data_path)), "kind": ext,
            "size": int(size), "mtime": _r3(save_t), "mtime_ns": int(mtime_ns),
            "head_sha256": head_sha256, "tagged_path": str(data_path),
        },
        "saved_at": {"t": _r3(save_t), "iso": iso(save_t), "from": "mtime"},
        "acquisition": None if acq is None else {
            "start": _r3(acq.start), "start_iso": iso(acq.start), "start_from": acq.start_from,
            "end": _r3(acq.end), "end_iso": iso(acq.end), "end_from": acq.end_from,
            "duration_s": _r2(acq.duration_s),
        },
        "values": values,
        "window": windows,
        "missing": missing,
        "tagged": {
            "at": _r3(now), "iso": iso(now), "origin": str(origin),
            "observed_at": _r3(observed_at), "max_age_s": float(max_age_s),
            "ring_s": ring_s, "host": str(host or ""), "mast": str(mast_version or ""),
            "clock_skew_s": _r2(skew) if skew > CLOCK_SKEW_NOTE_S else None,
        },
    }


__all__ = ["SCHEMA", "build_record", "value_for", "history_value", "missing_for", "window_for",
           "reading_entry", "bucket_at", "last_bucket_before", "iso"]
