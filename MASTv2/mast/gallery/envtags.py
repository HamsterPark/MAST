# -*- coding: utf-8 -*-
"""图库里的环境标签（设计 ``docs/v2/design/data_env_tags.md`` §6）。

构建时给每个条目找一份环境记录，顺序：

1. 代表文件的 sidecar（``<目录>/.mast/envtags/<文件名>.json``），再看各副本的；
2. 中央记录（环境历史库 ``file_env_tags``，按 文件名 + size + mtime 容差 查）；
3. 都没有 ⇒ 按环境历史**推断**（``s="i"``，界面标「推断」）；
4. 再没有 ⇒ 没有 ``env``。

**图库对数据目录只读**：只列目录、读 sidecar、读数据文件头 64 KiB 核对身份，绝不写；也不给
旧文件补写 sidecar。环境历史库只用**只读连接**（``mast.envhistory.filetags.reader``）：不新建
（库文件不存在就不推断）、不迁移老库、不排在监视线程落库的那把锁后面。

**增量构建要看得见后写的 sidecar**：缓存的身份 = 数据文件 ``(size, mtime_ns)`` +
sidecar ``(mtime_ns, size)``。sidecar 是按目录列一次 ``.mast/envtags/`` 得到的，不逐个 stat。
摘要变了的条目数报给构建状态 ``n_env``，前端据此决定要不要重拉索引。

索引里的摘要不走 ``index._clean``（那里一律 ``round(v, 4)``，3e-8 Pa 会变成 0），
物理量按 4 位有效数字取整。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

#: 改了摘要或推断的口径就加 1，下次构建全部重算。2：摘要多了 ``wr``（按读数算的窗口）。
VER_ENV = 2
#: 这么新的文件每次构建都再查一次中央记录（记录可能稍后才写成）。
RECHECK_CENTRAL_S = 24 * 3600.0
#: 这么新的文件的推断每次构建都重做（当前一分钟的桶可能还没落库）。
REINFER_S = 3600.0

_SUMMARY_KEYS = {"temperature": "T", "field": "B", "pressure": "P", "level": "L"}


def _sig4(v) -> float | None:
    from mast.envhistory.filetags.kinds import sig
    return sig(v, 4)


# ── 找记录 ───────────────────────────────────────────────────────────────


def list_sidecars(dirs) -> dict[str, dict[str, tuple[int, int]]]:
    """``{规范化目录: {sidecar 文件名: (mtime_ns, size)}}``。每个目录列一次，不逐个 stat。只读。"""
    from mast.envhistory.filetags.sidecar import SUBDIR

    out: dict[str, dict[str, tuple[int, int]]] = {}
    for d in dirs:
        key = os.path.normcase(os.path.abspath(str(d)))
        if key in out:
            continue
        found: dict[str, tuple[int, int]] = {}
        try:
            with os.scandir(os.path.join(str(d), *SUBDIR)) as it:
                for e in it:
                    if e.name.endswith(".json") and ".superseded-" not in e.name:
                        try:
                            st = e.stat()
                        except OSError:
                            continue
                        found[e.name] = (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            pass
        out[key] = found
    return out


def _dir_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.dirname(str(path))))


def open_store_if_present():
    """环境历史库的**只读**连接（``ReadOnlyEnvHistory``）；库不存在 ⇒ None（不新建）。

    不开 EnvHistoryStore：那个类开库就迁移（建表、补列、改 user_version）—— 一条 GET 不该改库；
    它的查询还都排在环境监视线程落库用的那把锁后面。**调用方负责 close()。**"""
    try:
        from mast.envhistory.filetags.reader import open_reader_if_present
    except Exception:  # noqa: BLE001
        return None
    return open_reader_if_present()


def _valid(doc, size: int, mtime_ns: int, data_path: str) -> bool:
    from mast.envhistory.filetags import sidecar as SC

    if not SC.is_ours(doc):
        return False

    def head() -> str:
        return SC.head_sha256(data_path)

    return SC.identity_matches(doc, size=size, mtime_ns=mtime_ns, head=head)


def find_record(paths: list[str], *, size: int, mtime_ns: int, listing: dict | None = None,
                store: Any = None) -> tuple[str, dict | None, str]:
    """``(how, 记录, sidecar 路径)``；``how`` ∈ ``sidecar / central / none``。只读。"""
    from mast.envhistory.filetags import sidecar as SC

    for p in paths:
        name = os.path.basename(p) + ".json"
        if listing is not None and name not in listing.get(_dir_key(p), {}):
            continue
        sp = SC.sidecar_path(p)
        doc = SC.read_sidecar(sp)
        if doc is not None and _valid(doc, size, mtime_ns, p):
            return "sidecar", doc, str(sp)
    if store is not None and paths:
        try:
            rows = store.find_file_tags(os.path.basename(paths[0]), size)
        except Exception:  # noqa: BLE001
            rows = []
        for r in sorted(rows, key=lambda r: abs(int(r.get("mtime_ns") or 0) - int(mtime_ns))):
            try:
                doc = json.loads(r.get("payload") or "")
            except (TypeError, ValueError):
                continue
            if _valid(doc, size, mtime_ns, paths[0]):
                return "central", doc, ""
    return "none", None, ""


# ── 摘要（进索引）────────────────────────────────────────────────────────


def _state(v: dict) -> str:
    if v.get("source") == "inferred":
        return "i"
    if not v.get("fresh", True):
        return "o"
    return "h" if v.get("source") == "history" else "l"


def summary(record: dict | None, how: str) -> dict | None:
    """索引条目的 ``env``：主值（T/B/P/L）+ 主值传感器在采集窗口内的 [min, max]。

    窗口与值**同一个单位**（传感器自己的单位）：卡片按原单位显示，「0→20.0 kG」不能写成
    「0→2.00 T」配「20.0 kG」。单位对不上（采集中途改过单位）就不给窗口。"""
    if not record:
        return None
    values = [v for v in (record.get("values") or []) if isinstance(v, dict)]
    windows = [w for w in (record.get("window") or []) if isinstance(w, dict)]
    if not values and not windows:
        return None
    out: dict = {"s": "i" if how == "inferred" else "r"}
    primary_sensor: dict[str, str] = {}
    for v in values:
        key = _SUMMARY_KEYS.get(str(v.get("kind")))
        if key is None or not v.get("primary") or v.get("value") is None:
            continue
        e: dict = {"v": _sig4(v.get("value")), "u": str(v.get("unit") or ""),
                   "c": _sig4(v.get("canonical")), "s": _state(v)}
        if e["s"] == "o":
            try:
                e["a"] = int(round(-float(v.get("dt_s") or 0.0)))
            except (TypeError, ValueError):
                e["a"] = None
        out[key] = e
        primary_sensor[key] = str(v.get("sensor"))
    for w in windows:
        key = _SUMMARY_KEYS.get(str(w.get("kind")))
        if key is None or primary_sensor.get(key) != str(w.get("sensor")):
            continue
        if str(w.get("unit") or "") != out[key]["u"]:
            continue
        lo, hi = _sig4(w.get("min")), _sig4(w.get("max"))
        if lo is not None and hi is not None:
            out["w" + key] = [lo, hi]
            # 只有逐条读数算出的窗口才正好盖住采集期间；按 1 分钟桶算的比它宽、含前后的起伏，
            # 卡片不拿它标「采集期间变过」。
            if str(w.get("method") or "") == "readings":
                out.setdefault("wr", []).append(key)
    return out


# ── 构建阶段 ─────────────────────────────────────────────────────────────


def _infer(path: str, entry: dict, store, candidates, max_age_s: float) -> dict | None:
    from mast.envhistory.filetags.acq import acquisition_window
    from mast.envhistory.filetags.infer import infer_record

    mtime = int(entry.get("mtime_ns") or 0) / 1e9
    return infer_record(path, size=int(entry.get("size") or 0),
                        mtime_ns=int(entry.get("mtime_ns") or 0), store=store,
                        candidates=candidates, max_age_s=max_age_s,
                        acq=acquisition_window(path, mtime=mtime))


def update_env(inv: dict, cache: dict, *, enabled: set[str], now: float | None = None,
               store: Any = "auto", max_age_s: float | None = None, should_stop=None,
               dir_has_mast: dict | None = None) -> dict:
    """就地更新环境缓存 ``cache``（``{id: …}``）。返回 ``{n_env, sidecar, central, inferred, none}``。

    ``n_env`` 是摘要变了的条目数（新出现、变了、消失都算）。只读数据目录。
    ``store="auto"``：只读打开环境历史库（open_store_if_present），用完关掉。

    ``should_stop``：构建被取消时返回真；每 CANCEL_CHECK_EVERY 个条目看一次，取消就停在那里
    （已做的留在缓存里，下一轮接着做；返回值里 ``cancelled`` 为真）。``dir_has_mast``：清点时
    走过的目录 → 里面有没有 ``.mast``（``inventory.walk_roots(mast_dirs=…)``）；走过、又没有 ``.mast``
    的目录不去列 sidecar，没走过的照常列。"""
    owned = isinstance(store, str) and store == "auto"
    if owned:
        store = open_store_if_present()
    try:
        return _update_env(inv, cache, enabled=enabled, now=now, store=store, max_age_s=max_age_s,
                           should_stop=should_stop, dir_has_mast=dir_has_mast)
    finally:
        if owned and store is not None:
            store.close()


#: 构建取消检查的间隔（条目数）。推断一个条目约 2 ms，64 个约 0.1 s。
CANCEL_CHECK_EVERY = 64


def _sidecar_dirs(dirs, dir_has_mast: dict | None) -> set:
    if not dir_has_mast:
        return set(dirs)
    return {d for d in dirs if dir_has_mast.get(os.path.normcase(os.path.abspath(str(d))), True)}


def _update_env(inv: dict, cache: dict, *, enabled: set[str], now: float | None,
                store: Any, max_age_s: float | None, should_stop=None,
                dir_has_mast: dict | None = None) -> dict:
    now = time.time() if now is None else float(now)
    if max_age_s is None:
        try:
            from mast.envhistory.filetags.settings import current
            max_age_s = current()[1]
        except Exception:  # noqa: BLE001
            max_age_s = 30.0
    ids = [i for i, e in inv.items() if i.split("/", 1)[0] in enabled and not e.get("err")]
    paths_of = {i: [inv[i].get("path") or ""] + [c for c in (inv[i].get("copies") or [])
                                                 if c != inv[i].get("path")] for i in ids}
    listing = list_sidecars(_sidecar_dirs({os.path.dirname(p) for i in ids for p in paths_of[i] if p},
                                          dir_has_mast))
    candidates = None
    counts = {"n_env": 0, "sidecar": 0, "central": 0, "inferred": 0, "none": 0, "cancelled": False}
    for gone in [i for i in cache if i not in inv]:
        if (cache.pop(gone, None) or {}).get("sum") is not None:
            counts["n_env"] += 1
    for n_done, iid in enumerate(ids):
        if should_stop is not None and n_done % CANCEL_CHECK_EVERY == 0 and should_stop():
            counts["cancelled"] = True
            break
        e = inv[iid]
        paths = [p for p in paths_of[iid] if p]
        src = [e.get("size"), e.get("mtime_ns")]
        side = None
        for p in paths:
            hit = listing.get(_dir_key(p), {}).get(os.path.basename(p) + ".json")
            if hit is not None:
                side = [p, hit[0], hit[1]]
                break
        old = cache.get(iid)
        age = now - (int(e.get("mtime_ns") or 0) / 1e9)
        same = (old is not None and old.get("src") == src and old.get("v") == VER_ENV
                and old.get("side") == side)
        if same and old.get("how") in ("sidecar", "central"):
            counts[old["how"]] += 1
            continue
        if same and old.get("how") == "inferred" and age > REINFER_S:
            counts["inferred"] += 1
            continue
        if same and old.get("how") == "none" and age > RECHECK_CENTRAL_S:
            counts["none"] += 1
            continue
        how, rec, _sp = find_record(paths, size=int(e.get("size") or 0),
                                    mtime_ns=int(e.get("mtime_ns") or 0),
                                    listing=listing, store=store)
        if rec is None and store is not None and paths:
            if candidates is None:
                from mast.envhistory.filetags.infer import candidates_from_store
                candidates = candidates_from_store(store)
            rec = _infer(paths[0], e, store, candidates, float(max_age_s))
            how = "inferred" if rec is not None else "none"
        new_sum = summary(rec, how)
        if old is None or old.get("sum") != new_sum:
            if not (old is None and new_sum is None):
                counts["n_env"] += 1
        cache[iid] = {"src": src, "side": side, "how": how, "v": VER_ENV, "sum": new_sum,
                      "at": round(now, 3)}
        counts[how] += 1
    return counts


# ── 详情（GET /api/gallery/env/{id}）───────────────────────────────────

_index_cache: dict = {"key": None, "by_id": {}}
_cand_cache: dict = {"at": 0.0, "store": None, "cands": None}
_CAND_TTL_S = 60.0


def _items_by_id(lay=None) -> dict:
    """索引条目按 id（按 index.json 的 mtime 缓存；只读）。"""
    from mast.gallery import index as gindex
    from mast.gallery import paths as gpaths

    lay = lay or gpaths.layout()
    try:
        st = os.stat(lay.index)
    except OSError:
        return {}
    key = (str(lay.index), st.st_mtime_ns, st.st_size)
    if _index_cache["key"] != key:
        doc = gindex.read_index(lay) or {}
        _index_cache["by_id"] = {it.get("id"): it for it in (doc.get("items") or [])
                                 if isinstance(it, dict)}
        _index_cache["key"] = key
    return _index_cache["by_id"]


def _candidates(store) -> list:
    """候选序列，按库文件缓存 60 s（每次详情都是一个新的只读连接，按连接对象缓存永远不中）。"""
    from mast.envhistory.filetags.infer import candidates_from_store

    key = str(getattr(store, "path", "") or id(store))
    now = time.monotonic()
    if (_cand_cache["store"] != key or _cand_cache["cands"] is None
            or now - _cand_cache["at"] > _CAND_TTL_S):
        _cand_cache.update(at=now, store=key, cands=candidates_from_store(store))
    return _cand_cache["cands"]


def detail(item_id: str, lay=None) -> dict:
    """一个条目的完整环境记录：sidecar → 中央记录 → 现推断。只读，永不抛（调用方另有兜底）。

    ``{id, how, sidecar_path, record}``；``how`` ∈ ``sidecar / central / inferred / none``。"""
    item = _items_by_id(lay).get(item_id)
    out = {"id": item_id, "how": "none", "sidecar_path": "", "record": None}
    if not item:
        return out
    paths = [p for p in [item.get("p") or ""] + list(item.get("cpl") or []) if p]
    paths = list(dict.fromkeys(paths))
    entry = None
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        entry = {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
        break
    if entry is None:
        return out
    store = open_store_if_present()
    try:
        how, rec, sp = find_record(paths, size=entry["size"], mtime_ns=entry["mtime_ns"], store=store)
        if rec is None and store is not None:
            try:
                from mast.envhistory.filetags.settings import current
                max_age = current()[1]
            except Exception:  # noqa: BLE001
                max_age = 30.0
            rec = _infer(paths[0], entry, store, _candidates(store), float(max_age))
            how = "inferred" if rec is not None else "none"
    finally:
        if store is not None:
            store.close()
    out.update(how=how, sidecar_path=sp, record=rec)
    return out


__all__ = ["VER_ENV", "list_sidecars", "open_store_if_present", "find_record", "summary",
           "update_env", "detail"]
