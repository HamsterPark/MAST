"""按元数据检索数据记录 —— ``GET /data/search`` 背后的那一层。

## 为什么不扩展 ``/data/files``

``/data/files`` 是一次磁盘清点：数据搜索目录 + 实验根里最新的文件，按 mtime 倒序，
字节级副本折叠；它不读文件头，也不知道哪个文件属于哪个实验。检索要的是另一份东西 ——
每个文件的尺寸、像素、偏压、设定点、谱的扫描范围、操作员的评级与标签 —— 而 MAST 里
已经有一份：数据图库（``mast.gallery``）增量建好的索引（``index.json``，界面在
``/records/gallery``）。两者的来源、字段与新鲜度都不同，塞进同一个端点只会让
``/data/files`` 变重、含义变含糊；那个端点原样不动（只多一个 ``frame`` 字段）。

## 三个来源，一份条目

1. **图库索引**：``mast.gallery.marks.items_meta``（按索引文件的 mtime + size 缓存，只读）。
   通道名不在索引里，取自同一个状态目录的清点缓存 ``cache/inventory.json``（同样按
   mtime + size 缓存，只留 id → 通道）。
2. **索引之外的文件**：``/data/files`` 同一份清点里、索引没收的那些（刚扫的、还没进下一次
   构建的；或者不在图库根里的），现场读头 —— 与图库构建同一套 ``inventory.SUMMARISERS``，
   .sxm 只读头。最多读最新的 :data:`LIVE_MAX` 个，读不完就 ``live.truncated``，不假装全了。
3. **操作员的标记**（``marks.json``：评级、标签、备注），只读；坏了就报 ``degraded``，
   不当成「没有标记」。

## 归属

实验 / 样品取自实验根之下的目录名（``YYYY-MM-DD__<名>__<id8>/samples/S<NN>__<名>__<sid6>/``）
与这两级目录里的 ``experiment.json`` / ``sample.json``（浅读，30 s 缓存）。Nanonis 会话目录里的
原件看它折叠进来的副本里有没有实验文件夹里的那一份。说不出归属就是 ``null``，不猜。

## 帧号

当前实验的 VISTA 档案按 ``source_path`` 记着每一帧来自哪个文件；档案目录里的拷贝
（``t0005.d0.dat``）文件名就是帧号，并入它的来源那一条，不单列。

## 路径约束不变

条目路径若不在 ``/data/file`` 允许的根（数据搜索目录 + 实验根）之内，``fetchable`` 为
``false``：图库的根是操作员配的，可能比数据出口宽 —— 知道它在，但从外部面取不到。
有一份副本在允许的根里时，``path`` 给的就是那一份。
"""

from __future__ import annotations

from mast.i18n import tr
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

#: 图库按扩展名认的三种数据。
KIND_BY_EXT = {".sxm": "f", ".dat": "s", ".3ds": "g"}
KIND_NAMES = {"f": "frame", "s": "spectrum", "g": "grid"}
#: 一次检索最多现场读几个索引之外的文件的头（最新的在前）。
LIVE_MAX = 200
#: 一页最多几条。
MAX_N = 200
#: 实验目录映射的缓存时长（秒）。
EXP_MAP_TTL_S = 30.0
#: 现场读头的缓存条数上限。
HEADER_CACHE_MAX = 4096

FRAME_ID_RE = re.compile(r"^t\d{4,}\.[spdm]\d+$")
_HEX = re.compile(r"[^0-9a-z]")


def _norm(p: Any) -> str:
    """比较路径用的规范形（大小写、分隔符）；不解析符号链接，也不碰磁盘。"""
    return os.path.normcase(os.path.normpath(str(p)))


def _under(path_norm: str, root_norm: str) -> bool:
    """``path_norm`` 是 ``root_norm`` 本身或在它之下（都已 :func:`_norm`）。盘符根 ``c:\\``
    自带分隔符，不能再拼一个。"""
    if path_norm == root_norm:
        return True
    prefix = root_norm if root_norm.endswith(os.sep) else root_norm + os.sep
    return path_norm.startswith(prefix)


def _rel_parts(path_norm: str, root_norm: str) -> list[str]:
    """``root_norm`` 之下的相对路径段（调用方已确认 :func:`_under`）。"""
    rest = path_norm[len(root_norm):].lstrip(os.sep)
    return rest.split(os.sep) if rest else []


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _r(v: Any, nd: int = 4) -> float | None:
    f = _finite(v)
    return None if f is None else round(f, nd)


def iso_local(t: Any) -> str | None:
    """epoch 秒 → 本机本地时间的 ISO（到秒）。仪器写文件头用的就是本机时间。"""
    f = _finite(t)
    if f is None:
        return None
    try:
        return datetime.fromtimestamp(f).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────
# 查询
# ─────────────────────────────────────────────────────────────────────

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_time(value: Any, *, end: bool = False) -> float | None:
    """时间边界 → epoch 秒。空 ⇒ None；读不懂 ⇒ ``ValueError``（由调用方回 422）。

    接受 epoch 秒、``YYYY-MM-DD``、``YYYY-MM-DD HH:MM[:SS]``、``YYYY-MM-DDTHH:MM[:SS]``，
    可带时区（``+08:00`` / ``Z``）；不带时区按本机本地时间。只给日期时，``end=True``
    （``until``）取那一天结束，于是 ``since=until=2026-10-15`` 就是那一整天。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    if _DATE_ONLY.match(text):
        d = datetime.strptime(text, "%Y-%m-%d")
        if end:
            d += timedelta(days=1)
        return d.timestamp()
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(tr(f"读不懂的时间 {text!r}：用 YYYY-MM-DD、YYYY-MM-DD HH:MM[:SS] 或 epoch 秒", f"Cannot parse time {text!r}: use YYYY-MM-DD, YYYY-MM-DD HH:MM[:SS] or epoch seconds")) from None
    return d.timestamp()


def parse_exts(value: Any) -> set[str] | None:
    """``"sxm,dat"`` → ``{".sxm", ".dat"}``；空 ⇒ None（不限）。认不出的类型报错，不静默放过。"""
    if value is None:
        return None
    parts = [p.strip().lower() for p in str(value).split(",") if p.strip()]
    if not parts:
        return None
    out: set[str] = set()
    for p in parts:
        e = p if p.startswith(".") else "." + p
        if e not in KIND_BY_EXT:
            raise ValueError(tr(f"不认识的类型 {p!r}：只有 sxm（图）、dat（谱）、3ds（网格谱）", f"Unknown type {p!r}: only sxm (image), dat (spectrum), 3ds (grid spectrum)"))
        out.add(e)
    return out


@dataclass
class Query:
    """一次检索的条件。数值边界都是闭区间；没有那个量的条目在给了边界时不算命中。"""

    exts: set[str] | None = None
    experiment: str = ""
    sample: str = ""
    since: float | None = None
    until: float | None = None
    size_min_nm: float | None = None
    size_max_nm: float | None = None
    bias_min_v: float | None = None
    bias_max_v: float | None = None
    setpoint_min_pa: float | None = None
    setpoint_max_pa: float | None = None
    text: str = ""
    tag: str = ""
    min_rating: int | None = None
    oldest_first: bool = False
    n: int = 50
    offset: int = 0
    live: bool = True

    def echo(self) -> dict:
        """回答里回显的条件（只列给了的）。"""
        out: dict = {}
        if self.exts:
            out["ext"] = sorted(e.lstrip(".") for e in self.exts)
        for key in ("experiment", "sample", "text", "tag"):
            v = getattr(self, key)
            if v:
                out[key] = v
        for key in ("since", "until"):
            v = getattr(self, key)
            if v is not None:
                out[key] = iso_local(v)
        for key in ("size_min_nm", "size_max_nm", "bias_min_v", "bias_max_v",
                    "setpoint_min_pa", "setpoint_max_pa", "min_rating"):
            v = getattr(self, key)
            if v is not None:
                out[key] = v
        out["order"] = "oldest" if self.oldest_first else "newest"
        return out


# ─────────────────────────────────────────────────────────────────────
# 实验 / 样品目录映射
# ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExpEntry:
    dir: str
    id: str                      # mast_scope 认的那个（v1 行 id，读不到就用目录里写的）
    ids: tuple[str, ...]         # 这个实验的每一种拼写（v1 / v2 / experiment.json 的 id）
    name: str
    samples: dict = field(default_factory=dict)   # 样品目录名 → {"id", "name", "dir"}

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "dir": self.dir}


_EXP_CACHE: dict = {"key": None, "t": 0.0, "map": {}}
_EXP_LOCK = threading.Lock()


def _read_json(path: Path) -> dict:
    try:
        from mast.logging.v2.manifest import read_json

        d = read_json(path)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — 一个坏清单只让那个实验说不出名字
        return {}


def _split_dir(name: str) -> tuple[str, str]:
    """``YYYY-MM-DD__<slug>__<id8>`` / ``S01__<slug>__<sid6>`` → ``(slug, id 前缀)``。"""
    parts = name.split("__")
    if len(parts) >= 3:
        return "__".join(parts[1:-1]), parts[-1]
    return name, ""


def _exp_entry(d: Path) -> ExpEntry | None:
    meta = _read_json(d / "experiment.json")
    slug, id8 = _split_dir(d.name)
    if not meta and not id8:
        return None                       # 不是一个实验文件夹（vista、_gallery、index…）
    prov = meta.get("provenance") or {}
    ids = [str(x) for x in (prov.get("v1_row_id"), meta.get("id"), prov.get("v2_experiment_id"))
           if x]
    ids = list(dict.fromkeys(ids))
    # 目录名里的 id8 取自 v1 行 id：优先给出与它对得上的那一个（mast_scope 认它）
    primary = next((i for i in ids if id8 and _HEX.sub("", i.lower()).startswith(id8.lower())),
                   ids[0] if ids else id8)
    samples: dict = {}
    sdir = d / "samples"
    try:
        children = sorted(p for p in sdir.iterdir() if p.is_dir()) if sdir.is_dir() else []
    except OSError:
        children = []
    for s in children:
        sm = _read_json(s / "sample.json")
        sslug, sid6 = _split_dir(s.name)
        samples[s.name] = {"id": str(sm.get("id") or sid6), "name": str(sm.get("name") or sslug),
                           "dir": s.name}
    return ExpEntry(dir=d.name, id=primary, ids=tuple(ids or ([id8] if id8 else [])),
                    name=str(meta.get("title") or slug), samples=samples)


def experiment_map(root: Path) -> dict[str, ExpEntry]:
    """实验根下每个实验文件夹 → :class:`ExpEntry`。浅读两级清单，缓存 ``EXP_MAP_TTL_S``。只读。"""
    try:
        st = os.stat(root)
    except OSError:
        return {}
    key = (_norm(root), st.st_mtime_ns)
    now = time.monotonic()
    with _EXP_LOCK:
        if _EXP_CACHE["key"] == key and now - _EXP_CACHE["t"] < EXP_MAP_TTL_S:
            return _EXP_CACHE["map"]
    out: dict[str, ExpEntry] = {}
    try:
        dirs = sorted(p for p in Path(root).iterdir() if p.is_dir())
    except OSError:
        dirs = []
    for d in dirs:
        if d.name.startswith(("_", ".")):
            continue
        e = _exp_entry(d)
        if e is not None:
            out[d.name] = e
    with _EXP_LOCK:
        _EXP_CACHE.update(key=key, t=now, map=out)
    return out


def attribute(paths: Iterable[str], root: Path | None,
              exps: dict[str, ExpEntry]) -> tuple[ExpEntry | None, dict | None]:
    """这些路径里第一个落在实验文件夹里的，属于哪个实验 / 样品。说不出就 ``(None, None)``。

    ``exps`` 的键按 :func:`experiment_map` 给的目录名；比较不分大小写（Windows）。"""
    if root is None or not exps:
        return None, None
    return _attribute_norm(paths, _norm(root), {os.path.normcase(n): e for n, e in exps.items()})


def _attribute_norm(paths: Iterable[str], base: str,
                    by_dir: dict[str, ExpEntry]) -> tuple[ExpEntry | None, dict | None]:
    """:func:`attribute` 的内层：``base`` 已规范化，``by_dir`` 的键已 ``normcase``。"""
    if not by_dir:
        return None, None
    for p in paths:
        np_ = _norm(p)
        if not _under(np_, base):
            continue
        parts = _rel_parts(np_, base)
        exp = by_dir.get(parts[0]) if parts else None
        if exp is None:
            continue
        sample = None
        if len(parts) >= 3 and parts[1] == "samples":
            sample = next((s for name, s in exp.samples.items()
                           if os.path.normcase(name) == parts[2]), None)
        return exp, sample
    return None, None


def _ident_matches(q: str, ids: Iterable[str], *names: str) -> bool:
    """``q`` 是这些 id 之一、某个 id 的前缀（≥6 个十六进制字符），或某个名字的子串（不分大小写）。"""
    qn = q.strip().lower()
    if not qn:
        return True
    compact = _HEX.sub("", qn)
    for i in ids:
        il = str(i).lower()
        if qn == il or (len(compact) >= 6 and _HEX.sub("", il).startswith(compact)):
            return True
    return any(qn in str(n).lower() for n in names if n)


# ─────────────────────────────────────────────────────────────────────
# 图库：索引、通道、标记
# ─────────────────────────────────────────────────────────────────────

_CH_CACHE: dict = {"key": None, "map": {}}
_CH_LOCK = threading.Lock()


def index_items() -> tuple[dict, dict]:
    """``({id: 索引条目}, 关于索引的说明)``。只读；没建过 ⇒ 空 + ``available: false``。"""
    info: dict = {"available": False}
    try:
        from mast.gallery import marks as gmarks
        from mast.gallery import paths as gpaths

        lay = gpaths.layout()
        try:
            st = os.stat(lay.index)
        except OSError:
            info["detail"] = tr("数据图库还没有建过索引（界面 /records/gallery 里「建立索引」）", "The data gallery has no index yet (use \"Build index\" in the UI at /records/gallery)")
            return {}, info
        by, _dirs = gmarks.items_meta(lay)
        info.update(available=True, written_at=iso_local(st.st_mtime), items=len(by))
        try:
            from mast.gallery.config import load_config

            info["roots"] = [{"name": r.name, "path": r.path, "enabled": bool(r.enabled)}
                             for r in load_config(lay).roots]
        except Exception:  # noqa: BLE001
            pass
        return by, info
    except Exception as exc:  # noqa: BLE001 — 图库坏了：降级到只有现场读取那一路
        logger.warning("ext data search: gallery index unreadable: %s", exc)
        info["detail"] = tr(f"图库索引读不了：{type(exc).__name__}: {exc}", f"Cannot read the gallery index: {type(exc).__name__}: {exc}")
        info["degraded"] = True
        return {}, info


def channel_map() -> dict[str, list[str]]:
    """图库清点缓存里每个 .sxm 的通道名（id → 名单）。按缓存文件的 mtime + size 缓存。只读。"""
    try:
        from mast.gallery import paths as gpaths

        p = gpaths.layout().cache_file("inventory.json")
        st = os.stat(p)
    except Exception:  # noqa: BLE001
        return {}
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _CH_LOCK:
        if _CH_CACHE["key"] == key:
            return _CH_CACHE["map"]
    try:
        from mast.gallery import paths as gpaths

        inv = gpaths.read_json(p, {})
    except Exception:  # noqa: BLE001
        inv = {}
    out = {iid: [str(c) for c in e.get("channels")]
           for iid, e in (inv.items() if isinstance(inv, dict) else ())
           if isinstance(e, dict) and isinstance(e.get("channels"), list)}
    with _CH_LOCK:
        _CH_CACHE.update(key=key, map=out)
    return out


def marks_items() -> tuple[dict, str | None]:
    """操作员的标记 ``({id: mark}, 读不了时的原因)``。文件损坏不当成「没有标记」。"""
    try:
        from mast.gallery import marks as gmarks

        doc = gmarks.load_marks()
        items = doc.get("items") or {}
        return (items if isinstance(items, dict) else {}), None
    except Exception as exc:  # noqa: BLE001
        return {}, tr(f"图库标记读不了（{type(exc).__name__}: {exc}）：评级 / 标签 / 备注这次不可用", f"Cannot read the gallery marks ({type(exc).__name__}: {exc}): rating / tag / note unavailable this time")


# ─────────────────────────────────────────────────────────────────────
# 条目
# ─────────────────────────────────────────────────────────────────────

def _setpoint(v: Any) -> float | None:
    """谱 / 网格谱的设定点（pA）。图库的 .dat / .3ds 摘要在头里没有这个键时写的是 ``0.0``
    （``(num(...) or 0.0)``）—— 有的控制器存的谱就没有模块参数。反馈的设定点不会恰好是 0，
    所以 0 在这里是「文件没说」，不报。"""
    f = _r(v)
    return None if f is None or f == 0.0 else f


def _put(out: dict, key: str, value: Any) -> None:
    if value is None or value == [] or value == "":
        return
    if isinstance(value, list) and all(v is None for v in value):
        return
    out[key] = value


def record_from_index(it: dict, channels: dict[str, list[str]]) -> dict:
    """图库索引条目（短键，见 ``schemas_gallery.GalleryItem``）→ 检索条目。"""
    k = it.get("k")
    fn = str(it.get("fn") or "")
    rec: dict = {"path": str(it.get("p") or ""), "name": fn, "ext": Path(fn).suffix.lower(),
                 "kind": KIND_NAMES.get(k, str(k)), "source": "index",
                 "gallery_id": it.get("id"), "_t": _finite(it.get("t")) or _finite(it.get("mt")),
                 "_mt": _finite(it.get("mt")), "_copies": [str(c) for c in (it.get("cpl") or [])]}
    if k == "f":
        _put(rec, "width_nm", _r(it.get("w")))
        _put(rec, "height_nm", _r(it.get("hn")))
        _put(rec, "pixels", [it.get("nx"), it.get("ny")] if it.get("nx") else None)
        _put(rec, "center_nm", [_r(it.get("cx")), _r(it.get("cy"))])
        _put(rec, "angle_deg", _r(it.get("ang")))
        _put(rec, "scan_dir", {"u": "up", "d": "down"}.get(str(it.get("sd") or "")))
        _put(rec, "bias_v", _r(it.get("b"), 6))
        _put(rec, "setpoint_pa", _r(it.get("sp")))
        _put(rec, "acq_s", _r(it.get("acq"), 1))
        if it.get("rall"):
            rec["rows_acquired"], rec["rows_total"] = int(it.get("rows") or 0), int(it["rall"])
        _put(rec, "channels", channels.get(str(it.get("id"))))
    elif k == "s":
        _put(rec, "bias_v", _r(it.get("b"), 6))
        _put(rec, "setpoint_pa", _setpoint(it.get("sp")))
        _put(rec, "sweep_v", [_r(it.get("v0"), 6), _r(it.get("v1"), 6)])
        _put(rec, "points", it.get("n"))
        _put(rec, "sweeps", it.get("sw"))
        _put(rec, "position_nm", [_r(it.get("x")), _r(it.get("y"))])
        _put(rec, "z_offset_pm", _r(it.get("zo")))
        _put(rec, "measurement", it.get("ex"))
    elif k == "g":
        _put(rec, "grid", [it.get("gx"), it.get("gy")] if it.get("gx") else None)
        _put(rec, "points", it.get("n"))
        _put(rec, "points_done", it.get("have"))
        _put(rec, "width_nm", _r(it.get("w")))
        _put(rec, "height_nm", _r(it.get("hn")))
        _put(rec, "center_nm", [_r(it.get("cx")), _r(it.get("cy"))])
        _put(rec, "angle_deg", _r(it.get("ang")))
        _put(rec, "sweep_v", [_r(it.get("v0"), 6), _r(it.get("v1"), 6)])
        _put(rec, "bias_v", _r(it.get("b"), 6))
        _put(rec, "setpoint_pa", _setpoint(it.get("sp")))
        _put(rec, "end_time", iso_local(it.get("t1")))
    return rec


def record_from_summary(path: str, ext: str, summary: dict | None, *, mtime: float | None,
                        copies: list[str], error: str | None = None) -> dict:
    """现场读到的头摘要（``mast.gallery.inventory`` 的 ``*_summary``）→ 检索条目。"""
    k = KIND_BY_EXT.get(ext)
    s = summary or {}
    rec: dict = {"path": path, "name": Path(path).name, "ext": ext,
                 "kind": KIND_NAMES.get(k or "", ext.lstrip(".")), "source": "live",
                 "_t": _finite(s.get("t0")) or _finite(mtime), "_mt": _finite(mtime),
                 "_copies": list(copies)}
    if error:
        rec["unreadable"] = error[:200]
        return rec
    if k == "f":
        _put(rec, "width_nm", _r(s.get("w_nm")))
        _put(rec, "height_nm", _r(s.get("h_nm")))
        _put(rec, "pixels", [s.get("nx"), s.get("ny")] if s.get("nx") else None)
        _put(rec, "center_nm", [_r(s.get("cx_nm")), _r(s.get("cy_nm"))])
        _put(rec, "angle_deg", _r(s.get("angle")))
        _put(rec, "scan_dir", (s.get("scan_dir") or None) if s.get("scan_dir") in ("up", "down") else None)
        _put(rec, "bias_v", _r(s.get("bias_V"), 6))
        _put(rec, "setpoint_pa", _r(s.get("setpoint_pA")))
        _put(rec, "acq_s", _r(s.get("acq_s"), 1))
        _put(rec, "channels", [str(c) for c in (s.get("channels") or [])])
    elif k == "s":
        _put(rec, "bias_v", _r(s.get("bias_V"), 6))
        _put(rec, "setpoint_pa", _setpoint(s.get("setpoint_pA")))
        _put(rec, "sweep_v", [_r(s.get("Vmin"), 6), _r(s.get("Vmax"), 6)])
        _put(rec, "points", s.get("n"))
        _put(rec, "sweeps", s.get("sweeps"))
        _put(rec, "position_nm", [_r(s.get("x_nm")), _r(s.get("y_nm"))])
        _put(rec, "z_offset_pm", _r(s.get("zoff_pm")))
        exp = str(s.get("exp") or "").strip()
        if exp and exp.lower() != "bias spectroscopy":
            rec["measurement"] = exp
    elif k == "g":
        _put(rec, "grid", [s.get("nx"), s.get("ny")] if s.get("nx") else None)
        _put(rec, "points", s.get("npts"))
        _put(rec, "points_done", s.get("have"))
        _put(rec, "width_nm", _r(s.get("w_nm")))
        _put(rec, "height_nm", _r(s.get("h_nm")))
        _put(rec, "center_nm", [_r(s.get("cx_nm")), _r(s.get("cy_nm"))])
        _put(rec, "angle_deg", _r(s.get("angle")))
        _put(rec, "sweep_v", [_r(s.get("v0"), 6), _r(s.get("v1"), 6)])
        _put(rec, "bias_v", _r(s.get("bias_V"), 6))
        _put(rec, "setpoint_pa", _setpoint(s.get("setpoint_pA")))
        _put(rec, "end_time", iso_local(s.get("t1")))
    return rec


_HDR_CACHE: dict = {}
_HDR_LOCK = threading.Lock()


def read_summary(path: str, size: Any, mtime: Any) -> tuple[dict | None, str | None]:
    """一个文件的头摘要（与图库构建同一套解析）。按 ``(size, mtime)`` 缓存；读不了 ⇒ ``(None, 原因)``。"""
    ext = Path(path).suffix.lower()
    key = _norm(path)
    ident = (size, mtime)
    with _HDR_LOCK:
        hit = _HDR_CACHE.get(key)
        if hit is not None and hit[0] == ident:
            return hit[1], hit[2]
    summary, error = None, None
    try:
        from mast.gallery.inventory import SUMMARISERS

        fn = SUMMARISERS.get(ext)
        if fn is None:
            error = tr(f"不认识的类型 {ext}", f"Unknown type {ext}")
        else:
            summary = fn(path)
    except Exception as exc:  # noqa: BLE001 — 一个坏文件只让它自己说不出元数据
        error = f"{type(exc).__name__}: {exc}"
    with _HDR_LOCK:
        if len(_HDR_CACHE) >= HEADER_CACHE_MAX:
            for old in list(_HDR_CACHE)[: HEADER_CACHE_MAX // 4]:
                _HDR_CACHE.pop(old, None)
        _HDR_CACHE[key] = (ident, summary, error)
    return summary, error


# ─────────────────────────────────────────────────────────────────────
# VISTA 帧号
# ─────────────────────────────────────────────────────────────────────

@dataclass
class FrameMap:
    """当前实验的 VISTA 档案：来源文件 → 帧号，以及档案目录（里面的拷贝以帧号命名）。"""

    by_path: dict = field(default_factory=dict)
    archive_root: str | None = None
    workspace: str | None = None
    detail: str | None = None

    def frame_for(self, paths: Iterable[str]) -> str | None:
        for p in paths:
            fid = self.by_path.get(_norm(p))
            if fid:
                return fid
        for p in paths:
            fid = self.archived_as(p)
            if fid:
                return fid
        return None

    def archived_as(self, path: str) -> str | None:
        """档案目录里的拷贝 ``tNNNN.k0.<ext>`` 的帧号；不是档案拷贝 ⇒ None。"""
        if not self.archive_root:
            return None
        np_ = _norm(path)
        if np_ == self.archive_root or not _under(np_, self.archive_root):
            return None
        stem = Path(path).stem
        return stem if FRAME_ID_RE.match(stem) else None


def frame_map(svc: Any) -> FrameMap:
    """VISTA 服务（可以是 None）→ :class:`FrameMap`。取不到就是空的，说明原因。"""
    if svc is None:
        return FrameMap(detail=tr("VISTA 服务未接线：帧号不可用", "VISTA service not wired: frame numbers unavailable"))
    try:
        ws = svc.workspace()
        by: dict = {}
        for e in ws.archive.frames():
            src = (getattr(e, "extra", None) or {}).get("source_path")
            if src:
                by.setdefault(_norm(src), e.fid)
        return FrameMap(by_path=by, archive_root=_norm(ws.archive.root), workspace=ws.id)
    except Exception as exc:  # noqa: BLE001
        return FrameMap(detail=tr(f"VISTA 档案读不了（{type(exc).__name__}: {exc}）：帧号不可用", f"Cannot read the VISTA archive ({type(exc).__name__}: {exc}): frame numbers unavailable"))


# ─────────────────────────────────────────────────────────────────────
# 过滤
# ─────────────────────────────────────────────────────────────────────

def _in(v: Any, lo: float | None, hi: float | None) -> bool:
    if lo is None and hi is None:
        return True
    f = _finite(v)
    if f is None:
        return False
    return (lo is None or f >= lo) and (hi is None or f <= hi)


def matches(rec: dict, q: Query) -> bool:
    """一条（已归属、已带标记的）检索条目是否满足条件。"""
    if q.exts and rec.get("ext") not in q.exts:
        return False
    if q.experiment:
        e = rec.get("_exp")
        if e is None or not _ident_matches(q.experiment, e.ids, e.name, e.dir):
            return False
    if q.sample:
        s = rec.get("sample")
        if s is None or not _ident_matches(q.sample, [s.get("id") or ""], s.get("name") or "",
                                           s.get("dir") or ""):
            return False
    t = rec.get("_t")
    if q.since is not None and (t is None or t < q.since):
        return False
    if q.until is not None and (t is None or t >= q.until):
        return False
    if not _in(rec.get("width_nm"), q.size_min_nm, q.size_max_nm):
        return False
    if not _in(rec.get("bias_v"), q.bias_min_v, q.bias_max_v):
        return False
    if not _in(rec.get("setpoint_pa"), q.setpoint_min_pa, q.setpoint_max_pa):
        return False
    if q.min_rating is not None and int(rec.get("rating") or 0) < q.min_rating:
        return False
    tags = [str(t).lower() for t in (rec.get("tags") or [])]
    if q.tag and q.tag.strip().lower() not in tags:
        return False
    if q.text:
        needle = q.text.strip().lower()
        hay = " ".join(str(x) for x in (
            rec.get("name"), rec.get("path"), rec.get("gallery_id"), rec.get("note"),
            rec.get("measurement"), " ".join(tags)) if x).lower()
        if needle not in hay:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────
# 一次检索
# ─────────────────────────────────────────────────────────────────────

def run(q: Query, *, listing: Callable[[int], dict], roots: list[Path], exp_root: Path | None,
        svc: Any) -> dict:
    """检索。``listing(n)``：``/data/files`` 那份清点的最新 ``n`` 条（不改它的行为）；
    ``roots``：``/data/file`` 允许的根；``exp_root``：实验根；``svc``：VISTA 服务或 None。"""
    notes: list[str] = []
    by_id, index_info = index_items()
    channels = channel_map() if by_id else {}
    marks, marks_err = marks_items()
    if marks_err:
        notes.append(marks_err)

    records: list[dict] = []
    known: set[str] = set()
    for it in by_id.values():
        if not isinstance(it, dict):
            continue
        rec = record_from_index(it, channels)
        m = marks.get(str(it.get("id"))) if marks else None
        if isinstance(m, dict) and not m.get("del"):
            if m.get("r"):
                rec["rating"] = int(m.get("r") or 0)
            _put(rec, "tags", [str(t) for t in (m.get("tags") or [])])
            _put(rec, "note", str(m.get("note") or "").strip())
        records.append(rec)
        known.update(_norm(p) for p in [rec["path"], *rec["_copies"]] if p)

    live_info: dict = {"enabled": bool(q.live), "max": LIVE_MAX}
    if q.live:
        try:
            body = listing(LIVE_MAX) or {}
        except Exception as exc:  # noqa: BLE001
            body = {}
            notes.append(tr(f"现场清点失败（{type(exc).__name__}: {exc}）：只有索引里的文件", f"Live listing failed ({type(exc).__name__}: {exc}): only files in the index"))
        files = [f for f in (body.get("files") or []) if isinstance(f, dict)]
        examined = added = 0
        for f in files:
            path = str(f.get("path") or "")
            ext = Path(path).suffix.lower()
            if ext not in KIND_BY_EXT:
                continue
            locs = [str(x.get("path")) for x in (f.get("locations") or [])
                    if isinstance(x, dict) and x.get("path")]
            members = [path, *[p for p in locs if _norm(p) != _norm(path)]]
            examined += 1
            if any(_norm(p) in known for p in members):
                continue
            summary, err = read_summary(path, f.get("size_bytes"), f.get("mtime"))
            rec = record_from_summary(path, ext, summary, mtime=_finite(f.get("mtime")),
                                      copies=members[1:], error=err)
            records.append(rec)
            known.update(_norm(p) for p in members)
            added += 1
        # has_more：清点里还有比这 LIVE_MAX 条更旧的文件没读 —— 索引之外的旧文件这次查不到
        live_info.update(examined=examined, added=added, truncated=bool(body.get("has_more")))
        if body.get("degraded"):
            live_info["degraded"] = True

    # 归属、帧号、能否取到
    exps = experiment_map(exp_root) if exp_root is not None else {}
    exp_base = _norm(exp_root) if exp_root is not None else ""
    exp_by_dir = {os.path.normcase(n): e for n, e in exps.items()}
    fmap = frame_map(svc)
    if fmap.detail:
        notes.append(fmap.detail)
    root_norms = [_norm(r) for r in roots]

    def allowed(p: str) -> bool:
        np_ = _norm(p)
        return any(_under(np_, r) for r in root_norms)

    by_frame: dict[str, dict] = {}
    archived: list[dict] = []
    for rec in records:
        members = [rec["path"], *rec["_copies"]]
        exp, sample = _attribute_norm(members, exp_base, exp_by_dir)
        rec["_exp"] = exp
        rec["experiment"] = exp.public() if exp is not None else None
        rec["sample"] = dict(sample) if sample is not None else None
        fid = fmap.frame_for(members)
        rec["frame"] = fid
        ok = [p for p in members if p and allowed(p)]
        rec["fetchable"] = bool(ok)
        if ok and not allowed(rec["path"]):
            rec["path"] = ok[0]
        if fid:
            if all(fmap.archived_as(p) for p in members):
                archived.append(rec)          # 只是档案目录里的拷贝
            else:
                by_frame.setdefault(fid, rec)
    # 档案拷贝并进它的来源那一条（同一帧不列两次）
    drop: set[int] = set()
    for rec in archived:
        src = by_frame.get(rec["frame"])
        if src is not None:
            src["_copies"] = list(dict.fromkeys([*src["_copies"], rec["path"], *rec["_copies"]]))
            drop.add(id(rec))
    records = [r for r in records if id(r) not in drop]

    hits = [r for r in records if matches(r, q)]
    hits.sort(key=lambda r: (r.get("_t") or 0.0, r.get("name") or ""), reverse=not q.oldest_first)
    page = hits[q.offset: q.offset + q.n]
    items = [public_record(r) for r in page]
    out: dict = {"items": items, "count": len(items), "total": len(hits), "offset": q.offset,
                 "has_more": q.offset + len(items) < len(hits), "query": q.echo(),
                 "index": index_info, "live": live_info,
                 "frames": {"workspace": fmap.workspace, "known": len(fmap.by_path)}}
    if notes:
        out["notes"] = notes
    out["degraded"] = bool(index_info.get("degraded") or marks_err or live_info.get("degraded"))
    return out


#: 未知时照样给出（``null``），让读的人看得出「说不出」而不是「没这个字段」。
_ALWAYS = ("experiment", "sample", "frame")


def public_record(rec: dict) -> dict:
    """内部键（``_t`` 等）换成对外的字段；``None`` 省略（:data:`_ALWAYS` 除外），常用的键在前。"""
    out: dict = {"path": rec.get("path"), "name": rec.get("name"), "ext": rec.get("ext"),
                 "kind": rec.get("kind"), "time": iso_local(rec.get("_t")),
                 "saved": iso_local(rec.get("_mt")), "experiment": rec.get("experiment"),
                 "sample": rec.get("sample"), "frame": rec.get("frame"),
                 "fetchable": rec.get("fetchable"), "source": rec.get("source")}
    copies = [p for p in rec.get("_copies") or [] if p and _norm(p) != _norm(rec.get("path") or "")]
    if copies:
        out["copies"] = copies
    for k, v in rec.items():
        if k.startswith("_") or k in out or v is None:
            continue
        out[k] = v
    return {k: v for k, v in out.items() if v is not None or k in _ALWAYS}


__all__ = ["FrameMap", "KIND_BY_EXT", "LIVE_MAX", "MAX_N", "Query", "attribute",
           "experiment_map", "frame_map", "matches", "parse_exts", "parse_time",
           "record_from_index", "record_from_summary", "run"]
