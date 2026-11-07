# -*- coding: utf-8 -*-
"""``ReadSiteMarks`` —— 读「位点标注」页里人标的点,精确化,再换算成 Nanonis 扫描器坐标(米)。

位点标注的 C 件(设计:``docs/v2/design/id23_site_marking_20261026.md``)。纯读 + 纯计算:
不碰仪器、不写 ``marks.json``(精确化的结果只返回,要不要写回页面是调用方的事)。

## 三步

1. 读 ``<project_root>/artifacts/site_marking/<page_id>/marks.json``。文档形如
   ``{"sites": {id: {n, x, y, kind, wide, low, note, ...}}, "plan/marking": {...}}``,
   也接受裸 list。
2. (``refine`` 且给了 ``frame_path``)在参考帧上找分子极大值,把每个标注吸附到
   分子中心 / 氢键中点 / 环心(蓝本 ``refine.py``,阈值见 :class:`SnapProfile`)。
3. 页面坐标 → 帧内偏移 → 扫描器绝对坐标(米)。

## 坐标约定(三层,别搞混)

* **页面坐标**:标注存下来的 ``x, y``,就是页面 ``viewBox`` 里的 nm。这个坐标系由页面 META 的
  ``x0, y1`` 定义(页面自己用它们画 viewBox),所以这里**读它,不假设它**:帧心在页面坐标里的位置
  是 ``(x0 + w/2, y1 − h/2)``。A 件现在填 ``x0=cx−w/2``,页面坐标是绝对 nm(未旋转);契约原文的
  「帧内」写法填 ``x0=-w/2``,帧心就是原点。两种都得到同一个站位。META 没有 x0/y1 才退回
  「帧心为原点」,并靠下面的出框检查兜底。
* **帧内偏移**:页面坐标减去帧心的页面位置 —— 帧心为原点、+y 向上、轴沿图像自己的行列
  (**不含**扫描偏移,**不含**扫描角)。吸附、``in_frame`` 检查都在这一层做,所以与扫描角无关。
  出框的站位**不给**扫描器坐标;全部出框但减去帧心后全在框内,点名「看起来是绝对坐标」。
* **扫描器坐标**:``Xs = cx + x·cosθ + y·sinθ, Ys = cy − x·sinθ + y·cosθ``,一律经
  :func:`mast.io.mosaic.px_to_m` —— 全仓唯一一份像素/帧内→米的换算,
  ``test_px_to_m_single_source`` 守着不许有第二份。θ=0 时就是蓝本的无旋转式。

## 扫描角符号

**正 SCAN_ANGLE = 顺时针**(转角系列帧的实测:正角顺时针,反号的相关性明显更低)。``px_to_m`` 现在用 ``R(−θ)``,是图库
``to_frame``(``R(+θ)``)的逆,与实测一致 —— ``tests/v2/unit/io/test_px_to_m_rotation.py``
用「px_to_m∘to_frame = 恒等」钉住。**所以 θ≠0 的坐标现在是可信的**(之前这里因为
符号未核实把转过的帧一律置 ``coords_trustworthy=false`` + ``angle_sign_warning``,
订正后那一层去掉了;``angle_unverified`` 恒为 false、``angle_sign_warning`` 恒为空,
键保留只为向后兼容)。仍不可信的只剩:**扫描角读不到**(未知≠0)、非方形页面、
页面与 frame_path 几何不符、站位出框。θ≠0 批量测前先扫一张配准图确认第一个站位
仍是好习惯(D 的漂移追踪会做),但不再是判据。

## 输出(``SkillResult.data``;成功与失败是同一套键)

* 整体:``page_id, count, sites, refined, refine_note, angle_deg, angle_known, angle_unverified,
  angle_sign_warning, geometry_source, frame, coords_trustworthy, anchor_molecules, snap_profile,
  skipped, warnings, marking_done, degraded, reason``。``angle_deg`` 读不到是 ``None``,不是 0;
  ``frame`` 是用到的几何(含 ``page_origin_nm``)。
* 每个站位(按 ``n`` 排序):``id, n, kind, wide, low, note, status``;``x_nm, y_nm`` 帧内偏移,
  ``page_x_nm, page_y_nm`` 页面里存的值;精确化了才有 ``rx_nm, ry_nm, page_rx_nm, page_ry_nm,
  rkind, rnote, shift_nm``;``position_source``(``refined`` / ``marked``)、``anchor``(下标)、
  ``anchor_offset_nm``;``in_frame``、``scanner_x_m, scanner_y_m``(米;出框 / 角度未知 / 没有几何
  时是 ``None``)。
* ``anchor_molecules``:被引用到的分子下标 → 其帧内与扫描器坐标,给漂移追踪按分子重定位用
  (下标只在**本次**检测里有意义)。
* ``coords_trustworthy`` 是给「拿结果去移针」的那一位看的总开关,全部满足才为真:几何齐全、
  **角度已知**(未知≠0,见上;但转过的帧现在可信)、页面与 frame_path 一致、没有出框、
  不是非方形页面。

## 「读不到」与「没有」是两件事

* marks.json 不存在 / 读不出 / 不是认得的形状 → ``success=False``,``degraded=True``,
  ``reason`` 给机器可读的原因,``sites=[]``。
* 文档在、``sites`` 里一个都没有 → ``success=True``,``count=0``,``degraded=False``。
* 有站位但**一个都换算不出来**(没有几何 / 角度未知 / 全部出框)→ ``success=False``,
  ``degraded=True``;原始 ``x_nm,y_nm`` 仍在 ``sites`` 里,不丢信息。
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import warnings
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.io.mosaic import parse_xy_meta, px_to_m
from mast.skills.base import BaseSkill

logger = logging.getLogger(__name__)

_SKILL_NAME = "ReadSiteMarks"

#: page_id 会拼进文件路径,所以只认一个保守的字符集 —— 与 A 件建页、B 件端点同一条规则
#: (它们建得出来的 id,这里都读得到;它们建不出来的,这里也不去碰盘)。
_PAGE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: Windows 设备名:``con`` / ``nul`` / ``com1`` 这类纯字母数字名当目录名会落到设备上。B 件同一张表。
_WIN_RESERVED = frozenset({"con", "prn", "aux", "nul",
                           *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})


#: 页面 META 与帧头几何算「一致」的容差。
_GEO_TOL_NM = 0.01
_GEO_TOL_DEG = 0.01

#: 检出的分子少于这个数就不做精确化(吸附到 1-2 个点上没有意义)。
_MIN_MOLECULES = 3


# ── 吸附参数 ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SnapProfile:
    """分子吸附的全部数值,整体传,不散在代码里。

    默认值取自某一分子体系（12 nm / 256 px）的蓝本实现:
    分子间距约 0.7 nm,氢键邻居 < 0.85 nm,六聚体环。**这些是样品相关的** ——
    换成别的样品,要换的是这张表,不是去改函数。输出里的 ``snap_profile`` 会回显用过的值。
    """

    #: 高斯平滑(蓝本 2.5 px @ 0.047 nm/px)与极大值窗口(两分子至少相隔多远)
    sigma_nm: float = 0.12
    min_sep_nm: float = 0.45
    #: 阈值 = 中位数 + thr_frac × (99.7 分位 − 中位数)
    thr_frac: float = 0.45
    #: 近邻(氢键)距离上限;成环个数与环的几何判据
    neighbour_max_nm: float = 0.85
    ring_size: int = 6
    ring_min_radius_nm: float = 0.55
    ring_radius_std_nm: float = 0.08
    #: 搜环只看离任一站位这么近的分子(≥ max_snap_nm + 环直径),让代价随站位数而不是整帧分子数涨
    ring_reach_nm: float = 3.0
    #: kind=auto 的判定半径
    ring_snap_nm: float = 0.3
    mol_snap_nm: float = 0.22
    hbond_snap_nm: float = 0.25
    au_far_nm: float = 1.0
    #: 明确要求吸附(mol/hbond/ring)时最多允许挪这么远;够不着就按标注原位。
    #: 蓝本这里没有上限 —— 标「分子」的点离最近分子 3 nm 也会被拽过去。
    max_snap_nm: float = 1.0


DEFAULT_PROFILE = SnapProfile()


# ── 路径与读取 ────────────────────────────────────────────────────────────

def valid_page_id(page_id: Any) -> bool:
    return (isinstance(page_id, str) and _PAGE_ID_RE.fullmatch(page_id) is not None
            and page_id.lower() not in _WIN_RESERVED)


def site_marking_dir(page_id: str) -> Path:
    """标注页的目录。定义只有一份,在 A 件(``site_marking.site_marking_dir``);这里加 page_id 校验。

    C 要读两样东西:B 件写的 ``marks.json``,和 A 件落的 ``meta.json`` —— 它们必须在同一个目录,
    所以不在这里再拼一遍路径。
    """
    if not valid_page_id(page_id):
        raise ValueError(f"invalid page_id: {page_id!r}")
    from mast.skills.builtins.site_marking import site_marking_dir as shared

    return shared(page_id)


def marks_path(page_id: str) -> Path:
    return site_marking_dir(page_id) / "marks.json"


#: B 件整体替换 marks.json 的那一瞬,Windows 上读者可能短暂打不开(PermissionError);
#: 写是原子替换,所以读到的要么是旧的整份要么是新的整份,撞上占用等一下重读一次就行。
_RETRY_PAUSE_S = 0.15


def _read_json(path: Path) -> tuple[Any, str, str]:
    """``(doc, reason, detail)``;读成功时 reason 为空。"""
    if not path.is_file():
        return None, "marks_missing", str(path)
    last: Exception | None = None
    for attempt in range(2):
        try:
            return json.loads(path.read_text(encoding="utf-8")), "", ""
        except PermissionError as exc:           # 撞上替换的那一瞬:等一下重读一次
            last = exc
            if attempt == 0:
                time.sleep(_RETRY_PAUSE_S)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            last = exc
            break
    return None, "marks_unreadable", f"{type(last).__name__}: {last}"


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _as_int(v: Any) -> int | None:
    f = _as_float(v)
    return int(f) if f is not None and f == int(f) else None


def _as_bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


def _extract_sites(doc: Any) -> tuple[list[dict], list[dict], list[str], int]:
    """``(sites, skipped, warnings, n_raw)``:规整后的站位(已按 n 排序)、被丢的条目与原因。

    被丢的条目不静默消失 —— 一个坐标坏掉的标注若直接少一个,测量序列就悄悄缺一点。
    """
    if isinstance(doc, dict) and "sites" in doc:
        body = doc["sites"]
    elif isinstance(doc, dict) and doc and all(
            isinstance(v, dict) and "x" in v and "y" in v for v in doc.values()):
        body = doc                                   # 直接是 id → 站位
    elif isinstance(doc, dict):
        body = {}                                    # 只有 plan/* 之类,还没标点
    else:
        body = doc
    if isinstance(body, dict):
        items = list(body.items())
    elif isinstance(body, list):
        items = [(None, v) for v in body]
    else:
        items = []

    sites: list[dict] = []
    skipped: list[dict] = []
    notes: list[str] = []
    for idx, (key, raw) in enumerate(items):
        sid = str(key) if key is not None else (
            str(raw.get("id")) if isinstance(raw, dict) and raw.get("id") else f"#{idx}")
        if not isinstance(raw, dict):
            skipped.append({"id": sid, "reason": tr("不是对象", "not an object")})
            continue
        x, y = _as_float(raw.get("x")), _as_float(raw.get("y"))
        if x is None or y is None:
            skipped.append({"id": sid, "reason": tr("x/y 缺失或不是有限数",
                                                    "x/y missing or not finite numbers")})
            continue
        sites.append({
            "id": sid, "n": _as_int(raw.get("n")), "x": x, "y": y,
            "kind": str(raw.get("kind") or "auto").strip().lower(),
            "wide": _as_bool(raw.get("wide"), True),
            "low": _as_bool(raw.get("low"), True),
            "note": str(raw.get("note") or "")[:200],
            "status": str(raw.get("status") or "todo"),
        })

    sites.sort(key=lambda s: (s["n"] is None, s["n"] if s["n"] is not None else 0, s["id"]))
    no_n = [s["id"] for s in sites if s["n"] is None]
    if no_n:
        notes.append(tr(f"{len(no_n)} 个站位没有整数 n(测量顺序),排在最后: {no_n[:5]}",
                        f"{len(no_n)} site(s) have no integer n (measurement order) and are ordered last: {no_n[:5]}"))
    seen: dict[int, int] = {}
    for s in sites:
        if s["n"] is not None:
            seen[s["n"]] = seen.get(s["n"], 0) + 1
    dup = sorted(n for n, c in seen.items() if c > 1)
    if dup:
        notes.append(tr(f"n 重复(测量顺序有歧义): {dup[:10]}",
                        f"Duplicate n (ambiguous measurement order): {dup[:10]}"))
    return sites, skipped, notes, len(items)


def _marking_done(doc: Any) -> bool | None:
    """页面上「标完了」按钮的状态;文档里没有这一项就是 None(不是 False)。"""
    if not isinstance(doc, dict):
        return None
    for key in ("plan/marking", "marking"):
        v = doc.get(key)
        if isinstance(v, dict) and "done" in v:
            return bool(v["done"])
    return None


# ── 帧几何 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FrameGeometry:
    """一帧的几何,全部 nm / 度。``angle_known=False`` 时 ``angle_deg`` 只是占位 0.0。"""

    cx_nm: float
    cy_nm: float
    w_nm: float
    h_nm: float
    angle_deg: float
    angle_known: bool
    source: str
    file: str = ""
    #: 页面坐标系里**帧心**的位置:页面坐标 = 帧内偏移 + page_origin。
    #: META 给了 x0/y1 就取 (x0 + w/2, y1 − h/2) —— 页面的 viewBox 就是它自己的坐标定义;
    #: A 件现在填 x0=cx−w/2,所以这里是 (cx, cy)(绝对 nm);契约原文的「帧内」写法给 (0, 0)。
    page_origin_nm: tuple[float, float] = (0.0, 0.0)
    #: 原点是 META 明说的,还是缺省假定「帧心为原点」的
    origin_from_meta: bool = False
    #: A 件标了「非方形帧,页面按方形显示,几何失真」
    nonsquare: bool = False

    def as_dict(self) -> dict:
        return {"cx_nm": self.cx_nm, "cy_nm": self.cy_nm, "w_nm": self.w_nm, "h_nm": self.h_nm,
                "angle_deg": self.angle_deg if self.angle_known else None,
                "angle_known": self.angle_known, "source": self.source, "file": self.file,
                "page_origin_nm": list(self.page_origin_nm), "origin_from_meta": self.origin_from_meta,
                "nonsquare": self.nonsquare}


def _geometry_from_meta(meta: Any) -> FrameGeometry | None:
    """页面 META(cx, cy, w[, h][, angle][, file][, x0, y1][, nonsquare];nm / 度)。

    缺 h 当方形,缺 angle 当未知;x0 与 y1 **同时**给出才算页面原点有定义。
    """
    if not isinstance(meta, dict):
        return None
    cx, cy, w = _as_float(meta.get("cx")), _as_float(meta.get("cy")), _as_float(meta.get("w"))
    h = _as_float(meta.get("h"))
    h = w if h is None else h
    if cx is None or cy is None or w is None or h is None or w <= 0 or h <= 0:
        return None
    ang = _as_float(meta.get("angle"))
    x0, y1 = _as_float(meta.get("x0")), _as_float(meta.get("y1"))
    has_origin = x0 is not None and y1 is not None
    return FrameGeometry(
        cx, cy, w, h, 0.0 if ang is None else ang, ang is not None, "page_meta",
        str(meta.get("file") or ""),
        page_origin_nm=(x0 + w / 2.0, y1 - h / 2.0) if has_origin else (0.0, 0.0),
        origin_from_meta=has_origin, nonsquare=bool(meta.get("nonsquare")))


def _geometry_from_header(header: dict, file: str) -> FrameGeometry | None:
    m = parse_xy_meta(header)                       # 米
    if m is None:
        return None
    return FrameGeometry(m["cx"] * 1e9, m["cy"] * 1e9, m["w"] * 1e9, m["h"] * 1e9,
                         float(m["angle"]), bool(m["angle_known"]), "frame_header", file)


def _page_meta(doc: Any, page_dir: Path) -> Any:
    """A 件存的 META:文档里的 ``meta``,否则 ``<page_dir>/meta.json``。读不到就是 None。"""
    if isinstance(doc, dict) and isinstance(doc.get("meta"), dict):
        return doc["meta"]
    meta, why, _ = _read_json(page_dir / "meta.json")
    return None if why else meta


def _choose_geometry(meta_geo: FrameGeometry | None,
                     frame_geo: FrameGeometry | None) -> tuple[FrameGeometry | None, list[str]]:
    """页面 META 优先:标注是画在那张图上的,帧内坐标相对的是**它**的帧心。

    两者都有且不一致 → 仍用 META,但把差异说出来(多半是 ``frame_path`` 给错了帧)。
    META 没带角度而帧头一致时,角度取帧头的。
    """
    if meta_geo is None:
        return frame_geo, []
    if frame_geo is None:
        return meta_geo, []
    diffs = []
    for name, a, b in (("cx", meta_geo.cx_nm, frame_geo.cx_nm), ("cy", meta_geo.cy_nm, frame_geo.cy_nm),
                       ("w", meta_geo.w_nm, frame_geo.w_nm), ("h", meta_geo.h_nm, frame_geo.h_nm)):
        if abs(a - b) > _GEO_TOL_NM:
            diffs.append(f"{name}: page {a:.4f} vs file {b:.4f} nm")
    geo = meta_geo
    if meta_geo.angle_known and frame_geo.angle_known:
        if abs(meta_geo.angle_deg - frame_geo.angle_deg) > _GEO_TOL_DEG:
            diffs.append(f"angle: page {meta_geo.angle_deg:.3f} vs file {frame_geo.angle_deg:.3f} deg")
    elif not meta_geo.angle_known and frame_geo.angle_known and not diffs:
        geo = replace(meta_geo, angle_deg=frame_geo.angle_deg, angle_known=True,
                      source="page_meta+frame_header")
    return geo, diffs


def frame_nm_to_scanner_m(x_nm: float, y_nm: float, geo: FrameGeometry) -> tuple[float, float]:
    """帧内 nm(帧心为原点、+y 向上)→ Nanonis 扫描器绝对坐标(米)。

    ``Xs = cx + x·cosθ + y·sinθ``、``Ys = cy − x·sinθ + y·cosθ``(R(−θ),正角顺时针),
    但经 :func:`px_to_m` 算 —— 符号约定只该存在于那一处。它要的是像素下标,所以借一个
    1×1 的虚拟像素格:像素尺寸在往返里抵消,留下来的只有它的「帧心 + 旋转」。
    此时 ``px_x = x/w``、``px_y = −y/h`` 恰好还原出帧内偏移 ``(x, y)``。
    """
    return px_to_m(
        x_nm / geo.w_nm, -y_nm / geo.h_nm, nx=1, ny=1,
        cx_m=geo.cx_nm * 1e-9, cy_m=geo.cy_nm * 1e-9,
        w_m=geo.w_nm * 1e-9, h_m=geo.h_nm * 1e-9,
        angle_deg=geo.angle_deg,
    )


def _in_frame(x_nm: float, y_nm: float, geo: FrameGeometry) -> bool:
    tol = max(0.05, 0.02 * max(geo.w_nm, geo.h_nm))
    return abs(x_nm) <= geo.w_nm / 2.0 + tol and abs(y_nm) <= geo.h_nm / 2.0 + tol


def _looks_absolute(xy: list[tuple[float, float]], geo: FrameGeometry) -> bool:
    """全部出框,但把偏移减掉后全在框内 —— 页面吐的多半是绝对坐标而不是帧内坐标。"""
    if not xy or any(_in_frame(x, y, geo) for x, y in xy):
        return False
    return all(_in_frame(x - geo.cx_nm, y - geo.cy_nm, geo) for x, y in xy)


# ── 帧像素 → 分子 ─────────────────────────────────────────────────────────

@dataclass
class _FrameData:
    geo: FrameGeometry | None = None
    z: Any = None                       # 已调平,行 0 = 帧上沿
    nm_per_px: float | None = None
    error: str = ""


def _level(z: Any) -> "np.ndarray | None":
    """最小二乘平面 + 逐行中位数;NaN(没扫到的行)补成中位数。蓝本 ``level()`` 的 NaN 安全版。"""
    a = np.array(z, dtype=float)
    if a.ndim != 2 or a.size == 0:
        return None
    ok = np.isfinite(a)
    if int(ok.sum()) < 3:
        return None
    a[~ok] = np.nan
    ny, nx = a.shape
    yy, xx = np.mgrid[:ny, :nx]
    coef, *_ = np.linalg.lstsq(np.c_[xx[ok], yy[ok], np.ones(int(ok.sum()))], a[ok], rcond=None)
    a = a - (coef[0] * xx + coef[1] * yy + coef[2])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)      # 整行 NaN
        a = a - np.nanmedian(a, axis=1, keepdims=True)
        fill = float(np.nanmedian(a))
    return np.nan_to_num(a, nan=fill if math.isfinite(fill) else 0.0)


def _load_frame(frame_path: str, *, need_pixels: bool) -> _FrameData:
    """读参考帧。``need_pixels=False`` 只读文件头(给几何用),不碰二进制块。"""
    from mast.io.nanonis_files import read_sxm, read_sxm_header, sxm_oriented_frames

    p = Path(frame_path)
    if not p.is_file():
        return _FrameData(error=tr(f"文件不存在: {frame_path}", f"File does not exist: {frame_path}"))
    scan: dict = {}
    try:
        if need_pixels:
            scan = read_sxm(str(p))
            header = scan.get("header") or {}
        else:
            header = read_sxm_header(str(p))
    except Exception as exc:  # noqa: BLE001 — 坏文件不该让整个技能失败
        return _FrameData(error=tr(f".sxm 读取失败: {exc}", f"Failed to read .sxm: {exc}"))
    geo = _geometry_from_header(header, p.name)
    if geo is None:
        return _FrameData(error=tr("文件头里没有可用的 SCAN_OFFSET / SCAN_RANGE",
                                   "No usable SCAN_OFFSET / SCAN_RANGE in the file header"))
    if not need_pixels:
        return _FrameData(geo=geo)
    fr = sxm_oriented_frames(scan, "Z")            # 行 0 = 上沿,反扫已去镜像
    z = _level(fr.get("forward")) if fr.get("forward") is not None else None
    if z is None or not fr.get("nm_per_px"):
        return _FrameData(geo=geo, error=tr("没有可用的 Z 通道像素", "No usable Z-channel pixels"))
    return _FrameData(geo=geo, z=z, nm_per_px=float(fr["nm_per_px"]))


def _parabolic(lo: float, mid: float, hi: float) -> float:
    """三点抛物线顶点相对中点的偏移(像素),限在 ±0.5。"""
    den = lo - 2.0 * mid + hi
    if den == 0.0 or not math.isfinite(den):
        return 0.0
    return float(np.clip(0.5 * (lo - hi) / den, -0.5, 0.5))


def _drop_tied_peaks(ii: "np.ndarray", jj: "np.ndarray", vals: "np.ndarray",
                     *, radius_px: int) -> tuple["np.ndarray", "np.ndarray"]:
    """同一个峰被报两次(例如分子恰好落在两行像素的正中,两行的值逐位相等)→ 只留一个。

    ``s == maximum_filter(s)`` 对并列值全放行。两个**真**极大值在窗口半径之内不可能同时
    成立,所以半径之内的邻居只能是并列的重复,按高度(并列取先出现者)抑制掉。蓝本没有这一步;
    漏掉它,重复点之间的距离为 0,会被当成一对氢键邻居。
    """
    if len(ii) < 2:
        return ii, jj
    from scipy.spatial import cKDTree

    pts = np.c_[ii, jj]
    tree = cKDTree(pts)
    alive = np.ones(len(pts), dtype=bool)
    for k in np.argsort(-vals, kind="stable"):
        if not alive[k]:
            continue
        for m in tree.query_ball_point(pts[k], r=radius_px, p=np.inf):
            if m != k:
                alive[m] = False
    return ii[alive], jj[alive]


def detect_molecules(z: Any, *, nm_per_px: float, w_nm: float, h_nm: float,
                     profile: SnapProfile = DEFAULT_PROFILE) -> "np.ndarray":
    """已调平的帧(行 0 = 上沿)里的分子极大值 → ``(m, 2)`` **帧内 nm**(帧心原点、+y 向上)。

    蓝本 ``refimage.molecules`` 的改写:平滑与窗口按 nm 给(蓝本按像素,只在 12 nm/256 px
    下对),子像素用抛物线;像素→帧内长度走 ``px_to_m``(不带偏移、不带旋转),
    所以与帧的 ``scan_dir`` / 扫描角无关 —— 角度只在最后一步换算时才进来。
    """
    from scipy import ndimage as ndi

    a = np.asarray(z, dtype=float)
    ny, nx = a.shape
    if min(ny, nx) < 8 or not nm_per_px > 0:
        return np.empty((0, 2))
    s = ndi.gaussian_filter(a, max(0.5, profile.sigma_nm / nm_per_px))
    win = max(3, int(round(profile.min_sep_nm / nm_per_px)) | 1)
    peaks = s == ndi.maximum_filter(s, size=win)
    lo, hi = np.percentile(s, [50, 99.7])
    if not hi > lo:
        return np.empty((0, 2))
    ii, jj = np.nonzero(peaks & (s > lo + profile.thr_frac * (hi - lo)))
    ii, jj = _drop_tied_peaks(ii, jj, s[ii, jj], radius_px=(win - 1) // 2)
    out = []
    for i, j in zip(ii, jj):
        di = dj = 0.0
        if 2 <= i < ny - 2 and 2 <= j < nx - 2:
            di = _parabolic(s[i - 1, j], s[i, j], s[i + 1, j])
            dj = _parabolic(s[i, j - 1], s[i, j], s[i, j + 1])
        x_m, y_m = px_to_m(j + dj, i + di, nx=nx, ny=ny, cx_m=0.0, cy_m=0.0,
                           w_m=w_nm * 1e-9, h_m=h_nm * 1e-9, angle_deg=0.0)
        out.append((x_m * 1e9, y_m * 1e9))
    return np.array(out, dtype=float).reshape(-1, 2)


def _neighbour_pairs(M: "np.ndarray", dmax: float) -> "np.ndarray":
    """相距 < dmax 的分子对(氢键邻居),``(k, 2)`` 下标,有序。"""
    if len(M) < 2:
        return np.empty((0, 2), dtype=int)
    from scipy.spatial import cKDTree

    pr = cKDTree(M).query_pairs(r=dmax, output_type="ndarray").astype(int).reshape(-1, 2)
    return pr[np.lexsort((pr[:, 1], pr[:, 0]))] if len(pr) else pr


def _find_rings(M: "np.ndarray", pairs: "np.ndarray", probes: "np.ndarray",
                profile: SnapProfile) -> list[tuple[tuple[int, ...], "np.ndarray"]]:
    """闭合的 ``ring_size`` 元环(蓝本:六聚体)→ ``[(分子下标元组, 环心)]``。

    只在离任一探针点 ``ring_reach_nm`` 以内的分子上搜:全帧搜是 O(分子数 × 度^5),
    大帧上会卡住一个循环任务。
    """
    if len(pairs) == 0 or len(probes) == 0:
        return []
    near = np.min(np.hypot(M[:, None, 0] - probes[None, :, 0],
                           M[:, None, 1] - probes[None, :, 1]), axis=1) <= profile.ring_reach_nm
    adj: dict[int, set[int]] = {}
    for i, j in pairs:
        if near[i] and near[j]:
            adj.setdefault(int(i), set()).add(int(j))
            adj.setdefault(int(j), set()).add(int(i))
    k = profile.ring_size
    found: dict[tuple[int, ...], np.ndarray] = {}
    for start in sorted(adj):
        stack = [(start, [start])]
        while stack:
            v, path = stack.pop()
            for w in adj[v]:
                if w == start and len(path) == k:
                    key = tuple(sorted(path))
                    if key in found:
                        continue
                    pts = M[list(key)]
                    c = pts.mean(0)
                    rr = np.hypot(*(pts - c).T)
                    if rr.min() > profile.ring_min_radius_nm and rr.std() < profile.ring_radius_std_nm:
                        found[key] = c
                elif w not in path and len(path) < k and w > start:
                    stack.append((w, path + [w]))
    return list(found.items())


def snap_site(x: float, y: float, kind: str, M: "np.ndarray", pairs: "np.ndarray",
              rings: list, profile: SnapProfile = DEFAULT_PROFILE) -> dict:
    """一个标注(帧内 nm)→ ``{rx, ry, rkind, rnote, anchor, anchor_offset_nm, shift_nm}``。

    蓝本 ``refine.refine`` 的改写,多出来的都是蓝本会崩或会乱拽的地方:没有氢键对 / 没有环
    却明确要求 hbond / ring(蓝本 ``TypeError``);明确要求吸附但最近的对象在几 nm 外
    (蓝本照拽)→ 超过 ``max_snap_nm`` 就按标注原位并写明原因。``M`` 至少要有 1 个分子。
    """
    p = np.array([x, y], dtype=float)
    d = np.hypot(M[:, 0] - p[0], M[:, 1] - p[1])
    i0 = int(np.argmin(d))
    d0 = float(d[i0])

    pair, pair_d, pair_len = None, math.inf, 0.0
    if len(pairs):
        mids = (M[pairs[:, 0]] + M[pairs[:, 1]]) / 2.0
        dm = np.hypot(mids[:, 0] - p[0], mids[:, 1] - p[1])
        k = int(np.argmin(dm))
        pair, pair_d = (int(pairs[k, 0]), int(pairs[k, 1])), float(dm[k])
        pair_len = float(np.hypot(*(M[pair[0]] - M[pair[1]])))
    ring = min(rings, key=lambda r: float(np.hypot(*(r[1] - p)))) if rings else None
    ring_d = float(np.hypot(*(ring[1] - p))) if ring is not None else math.inf

    asked = (kind or "auto").strip().lower()
    chosen = asked
    if asked == "auto":
        if ring_d < profile.ring_snap_nm:
            chosen = "ring"
        elif d0 < profile.mol_snap_nm and d0 <= pair_d:
            chosen = "mol"
        elif pair is not None and pair_d < profile.hbond_snap_nm:
            chosen = "hbond"
        elif d0 > profile.au_far_nm:
            chosen = "au"
        else:
            chosen = "fixed"

    cap = profile.max_snap_nm
    q, anchor, rkind = p, [i0], chosen            # 缺省:按标注原位,记住离最近分子的偏移
    if chosen == "mol" and d0 <= cap:
        q, anchor = M[i0], [i0]
        note = tr(f"分子中心（标注偏 {d0:.2f} nm）", f"Molecule centre (mark was {d0:.2f} nm off)")
    elif chosen == "hbond" and pair is not None and pair_d <= cap:
        q, anchor = (M[pair[0]] + M[pair[1]]) / 2.0, list(pair)
        note = tr(f"氢键位置：两分子中点（分子间距 {pair_len:.2f} nm）",
                  f"H-bond site: midpoint of two molecules (spacing {pair_len:.2f} nm)")
    elif chosen == "ring" and ring is not None and ring_d <= cap:
        q, anchor = ring[1], list(ring[0])
        note = tr(f"{len(ring[0])} 分子环中心（孔）", f"Centre of a {len(ring[0])}-molecule ring (pore)")
    elif chosen == "au":
        anchor = []
        note = tr(f"裸衬底（距最近分子 {d0:.2f} nm）", f"Bare substrate (nearest molecule {d0:.2f} nm away)")
    elif chosen in ("mol", "hbond", "ring"):
        rkind = "fixed"
        note = tr(f"要求吸附到 {chosen}，但 {cap:.1f} nm 内没有，按标注原位",
                  f"Asked to snap to {chosen}, but none within {cap:.1f} nm; kept where marked")
    else:
        if chosen not in ("edge", "fixed"):
            rkind = "fixed"
        note = tr(f"按标注原位（距最近分子 {d0:.2f} nm）",
                  f"Kept where marked (nearest molecule {d0:.2f} nm away)")

    off = (q - M[anchor].mean(0)).tolist() if anchor else None
    return {"rx": float(q[0]), "ry": float(q[1]), "rkind": rkind, "rnote": note,
            "anchor": [int(i) for i in anchor],
            "anchor_offset_nm": [round(float(v), 4) for v in off] if off else None,
            "shift_nm": float(np.hypot(*(q - p)))}


# ── 结果拼装 ──────────────────────────────────────────────────────────────

def _blank_data(page_id: str) -> dict:
    """成功与失败共用的键集:调用方不用先判断键在不在。"""
    return {"page_id": page_id, "count": 0, "refined": False, "refine_note": "",
            "angle_deg": None, "angle_known": False, "angle_unverified": False,
            "angle_sign_warning": "",
            "geometry_source": "none", "frame": None, "coords_trustworthy": False,
            "sites": [], "anchor_molecules": {}, "snap_profile": None, "skipped": [],
            "warnings": [], "marking_done": None, "degraded": False, "reason": ""}


def _failure(page_id: str, reason: str, error: str, *, degraded: bool = True,
             data: dict | None = None) -> SkillResult:
    d = data if data is not None else _blank_data(page_id)
    d.update(degraded=degraded, reason=reason, coords_trustworthy=False)
    return SkillResult(skill_name=_SKILL_NAME, success=False, error=error, data=d)


class ReadSiteMarks(BaseSkill):
    """读位点标注页的标注 → (可选)吸附到分子 → 扫描器米坐标。纯读,不碰仪器。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=_SKILL_NAME,
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "读「位点标注」页里人标的测谱点（marks.json），可选地吸附到参考帧上的分子中心 / "
                "氢键中点 / 环心，并把标注坐标（页面 nm）换算成 Nanonis 扫描器绝对坐标（米，带扫描角）。"
                "只读：不碰仪器，不写回 marks.json。读不到标注是 degraded，不等于「没有站位」；"
                "拿结果去移针前先看 coords_trustworthy（几何齐全、扫描角已知、页面与帧一致、"
                "站位在框内才为真；θ≠0 也可信）、每个站位的 in_frame 与 warnings。",
                "Read the measurement sites a person marked on the site-marking page (marks.json), "
                "optionally snap them to molecule centres / H-bond midpoints / ring centres on the "
                "reference frame, and convert the mark coordinates (page nm) to Nanonis scanner "
                "coordinates (metres, scan angle included). Read-only: never touches the "
                "instrument and never writes marks.json back. Being unable to read the marks is "
                "'degraded', not 'no sites'; before moving the tip with the result, check "
                "coords_trustworthy (true when geometry is complete, the scan angle is known, the "
                "page matches the frame and sites are in-frame; a non-zero "
                "scan angle is trustworthy too), each site's in_frame and warnings."),
            parameters=[
                ParameterSpec(
                    name="page_id", type="str", required=True,
                    description=tr("标注页的 id（PrepareSiteMarkingPage 返回的那个）。",
                                   "Id of the site-marking page (the one PrepareSiteMarkingPage returned)."),
                ),
                ParameterSpec(
                    name="refine", type="bool", required=False, default=True,
                    description=tr("把每个标注吸附到分子中心 / 氢键中点 / 环心（要 frame_path）。"
                                   "false = 标注原样使用（页里存的 rx/ry 一律不用）。",
                                   "Snap each mark to a molecule centre / H-bond midpoint / ring centre "
                                   "(needs frame_path). false = use the marks exactly as clicked "
                                   "(any rx/ry stored in the page is ignored)."),
                ),
                ParameterSpec(
                    name="frame_path", type="str", required=False, default="",
                    description=tr("标注页所基于的那张 .sxm。精确化要用它的像素；页里没有帧几何"
                                   "（中心、尺寸、扫描角）时也从它的文件头取。留空 = 不精确化。",
                                   "The .sxm the marking page was made from. Refinement uses its pixels; "
                                   "its header also supplies the frame geometry (centre, size, scan "
                                   "angle) when the page has none. Empty = no refinement."),
                ),
            ],
            estimated_duration_s=2.0,
            composition_level=2,
            tags=["site-marking", "marks", "coordinates", "read", "analysis"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        page_id = str((params or {}).get("page_id") or "").strip()
        try:
            return self._run(page_id, params or {})
        except Exception as exc:  # noqa: BLE001 — 技能不抛异常,否则到不了 agent 的错误路径
            logger.exception("ReadSiteMarks failed unexpectedly (page_id=%s)", page_id)
            return _failure(page_id, "internal_error",
                            tr(f"读取站位时出错: {type(exc).__name__}: {exc}",
                               f"Unexpected error while reading the sites: {type(exc).__name__}: {exc}"))

    def _run(self, page_id: str, params: dict) -> SkillResult:
        refine = _as_bool(params.get("refine"), True)
        frame_path = str(params.get("frame_path") or "").strip()

        if not valid_page_id(page_id):
            return _failure(page_id, "invalid_page_id",
                            tr("page_id 只能含字母、数字、下划线、连字符，最长 64 个字符，且不能是 Windows 设备名。",
                               "page_id may only contain letters, digits, underscores and hyphens "
                               "(at most 64 characters) and must not be a Windows device name."),
                            degraded=False)

        page_dir = site_marking_dir(page_id)
        doc, why, detail = _read_json(page_dir / "marks.json")
        if why:
            return _failure(page_id, why, tr(
                f"读不到标注（{why}）: {detail}。这不等于没有站位 —— 页面还没存过，或文件坏了。",
                f"Cannot read the marks ({why}): {detail}. This is not the same as 'no sites': "
                f"the page may never have saved, or the file is corrupt."))

        if not isinstance(doc, (dict, list)):
            return _failure(page_id, "marks_malformed", tr(
                f"marks.json 的顶层是 {type(doc).__name__}，不是标注文档（应是对象或列表）。",
                f"The top level of marks.json is a {type(doc).__name__}, not a marks document "
                f"(expected an object or a list)."))

        sites, skipped, warns, n_raw = _extract_sites(doc)
        data = _blank_data(page_id)
        data.update(skipped=skipped, marking_done=_marking_done(doc))
        if n_raw and not sites:
            return _failure(page_id, "marks_malformed", tr(
                f"marks.json 里有 {n_raw} 个条目，但没有一个带有限的 x/y。",
                f"marks.json has {n_raw} entries, none with finite x/y."), data=data)

        # ── 几何:页面 META 优先,没有就用帧头 ───────────────────────────
        meta_geo = _geometry_from_meta(_page_meta(doc, page_dir))
        frame = (_load_frame(frame_path, need_pixels=refine and bool(sites))
                 if frame_path else _FrameData())
        geo, diffs = _choose_geometry(meta_geo, frame.geo)
        if diffs:
            warns.append(tr(
                "页面记录的帧几何与 frame_path 的文件头不一致（" + "; ".join(diffs) + "）。"
                "按页面的来换算；frame_path 多半给错了帧，所以也不做精确化。",
                "The frame geometry recorded by the page differs from the frame_path header ("
                + "; ".join(diffs) + "). Converting with the page's geometry; frame_path is "
                "probably the wrong frame, so no refinement is done either."))
        data["geometry_source"] = geo.source if geo else "none"
        data["frame"] = geo.as_dict() if geo else None
        data["angle_known"] = bool(geo and geo.angle_known)
        data["angle_deg"] = geo.angle_deg if geo and geo.angle_known else None
        # SCAN_ANGLE 符号在 px_to_m 里按 R(−θ) 处理(正角顺时针),
        # θ≠0 不再因「符号未核实」而降级 —— angle_unverified 恒为 false、angle_sign_warning
        # 恒为空(键保留只为向后兼容)。仍降级的只剩未知角/非方形/几何不符/出框。
        if geo is not None and geo.nonsquare:
            warns.append(tr(
                "标注页把非方形帧按方形显示（META.nonsquare）：页面的 y 坐标有失真，换算结果不可信。",
                "The marking page showed a non-square frame as a square (META.nonsquare): its y "
                "coordinates are distorted, so the converted coordinates cannot be trusted."))

        # 页面坐标 → 帧内偏移(帧心为原点、+y 向上、沿图像自己的轴)。后面的吸附、出框检查、
        # 旋转换算都只认帧内偏移;页面坐标原样留着,回写页面的人用得上。
        ox, oy = geo.page_origin_nm if geo is not None else (0.0, 0.0)
        for s in sites:
            s["page_x"], s["page_y"] = s["x"], s["y"]
            s["x"], s["y"] = s["x"] - ox, s["y"] - oy

        # ── 精确化 ────────────────────────────────────────────────────────
        snapped: dict[str, dict] = {}
        molecules = None
        if not refine:
            data["refine_note"] = tr("未请求精确化，标注原样使用", "Refinement not requested; marks used as clicked")
        elif not sites:
            data["refine_note"] = tr("没有站位可精确化", "No sites to refine")
        elif not frame_path:
            data["refine_note"] = tr("没给 frame_path，标注原样使用", "No frame_path; marks used as clicked")
        elif diffs:
            data["refine_note"] = tr("frame_path 与标注页的帧不一致，未精确化",
                                     "frame_path does not match the marking page's frame; not refined")
        elif frame.z is None:
            data["refine_note"] = tr(f"参考帧不可用（{frame.error}），标注原样使用",
                                     f"Reference frame unusable ({frame.error}); marks used as clicked")
        else:
            try:
                molecules, snapped, data["refine_note"] = self._refine(sites, frame, data)
            except Exception as exc:  # noqa: BLE001 — 精确化失败不该拖垮读取
                logger.exception("ReadSiteMarks: refinement failed")
                molecules, snapped = None, {}
                data["refine_note"] = tr(f"精确化出错（{type(exc).__name__}: {exc}），标注原样使用",
                                         f"Refinement failed ({type(exc).__name__}: {exc}); marks used as clicked")
        data["refined"] = bool(snapped)

        # ── 换算 ──────────────────────────────────────────────────────────
        out_sites = [self._site_out(s, snapped.get(s["id"]), geo, (ox, oy)) for s in sites]
        used_xy = [(s["rx_nm"] if "rx_nm" in s else s["x_nm"], s["ry_nm"] if "ry_nm" in s else s["y_nm"])
                   for s in out_sites]
        if geo is not None and snapped and molecules is not None:
            idx = sorted({i for o in snapped.values() for i in o["anchor"]})
            data["anchor_molecules"] = {str(i): self._molecule_out(molecules[i], geo) for i in idx}

        usable = [s for s in out_sites if s["scanner_x_m"] is not None]
        out_of_frame = [s["id"] for s in out_sites if s["in_frame"] is False]
        looks_abs = geo is not None and _looks_absolute(used_xy, geo)
        if out_of_frame:
            warns.append(tr(
                f"{len(out_of_frame)} 个站位在帧外（{out_of_frame[:5]}），没给扫描器坐标。",
                f"{len(out_of_frame)} site(s) lie outside the frame ({out_of_frame[:5]}); no scanner "
                f"coordinates given."))
        if looks_abs:
            warns.append(tr(
                "全部标注都在帧外，但把帧心偏移减掉后全落在框内 —— 标注看起来是**绝对坐标**，"
                + ("可页面 META 声明的原点(x0/y1)与此不符。" if geo.origin_from_meta else
                   "而页面 META 没有 x0/y1，没法确定原点。"),
                "Every mark is outside the frame but all fall inside once the frame-centre offset is "
                "subtracted - the marks look like ABSOLUTE coordinates, "
                + ("yet the origin the page META declares (x0/y1) says otherwise."
                   if geo.origin_from_meta else "and the page META has no x0/y1, so the origin is unknown.")))

        data.update(count=len(out_sites), sites=out_sites, warnings=warns)
        data["coords_trustworthy"] = bool(
            geo is not None and geo.angle_known
            and not geo.nonsquare and not diffs and not out_of_frame and usable)

        if out_sites and not usable:
            if geo is None:
                why_frame = (tr(f"frame_path 不可用（{frame.error}）", f"frame_path unusable ({frame.error})")
                             if frame_path and frame.error else tr("没给 frame_path", "no frame_path given"))
                reason, msg = "no_geometry", tr(
                    f"没有帧几何（页里没有 META，{why_frame}），换算不了。给一个能读的 frame_path。",
                    f"No frame geometry (the page has no META and {why_frame}); cannot convert. "
                    f"Pass a readable frame_path.")
            elif not geo.angle_known:
                reason, msg = "angle_unknown", tr(
                    "扫描角读不到：不能当 0 处理（未知不等于零），没给扫描器坐标。给能读到 SCAN_ANGLE 的 frame_path。",
                    "The scan angle is unknown: it is not treated as 0 (unknown is not zero), so no scanner "
                    "coordinates are given. Pass a frame_path whose header carries SCAN_ANGLE.")
            elif looks_abs:
                reason, msg = "marks_look_absolute", tr(
                    "标注看起来是绝对坐标，不是帧内坐标，没给扫描器坐标（再加一次偏移会差几十 nm）。",
                    "The marks look like absolute coordinates, not in-frame ones; no scanner coordinates "
                    "given (adding the offset again would be off by tens of nm).")
            else:
                reason, msg = "all_sites_out_of_frame", tr(
                    "所有站位都在帧外，没给扫描器坐标。", "Every site is outside the frame; no scanner coordinates given.")
            return _failure(page_id, reason, msg, data=data)

        n_ref = sum(1 for s in out_sites if s["position_source"] == "refined")
        theta = "?" if data["angle_deg"] is None else f"{data['angle_deg']:.2f}"
        summary = tr(
            f"读到 {len(out_sites)} 个站位（精确化 {n_ref} 个，θ={theta}°，{len(warns)} 条警告）",
            f"Read {len(out_sites)} site(s) (refined {n_ref}, theta={theta} deg, {len(warns)} warning(s))")
        return SkillResult(skill_name=_SKILL_NAME, success=True, data=data, summary=summary)

    # ── 内部 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _refine(sites: list[dict], frame: _FrameData, data: dict):
        geo = frame.geo
        M = detect_molecules(frame.z, nm_per_px=frame.nm_per_px, w_nm=geo.w_nm, h_nm=geo.h_nm)
        if len(M) < _MIN_MOLECULES:
            return None, {}, tr(f"参考帧上只检出 {len(M)} 个分子，不做精确化，标注原样使用",
                                f"Only {len(M)} molecule(s) detected on the reference frame; not refined, "
                                f"marks used as clicked")
        pairs = _neighbour_pairs(M, DEFAULT_PROFILE.neighbour_max_nm)
        rings = _find_rings(M, pairs, np.array([[s["x"], s["y"]] for s in sites]), DEFAULT_PROFILE)
        snapped = {s["id"]: snap_site(s["x"], s["y"], s["kind"], M, pairs, rings) for s in sites}
        data["snap_profile"] = asdict(DEFAULT_PROFILE)
        return M, snapped, tr(
            f"检出 {len(M)} 个分子、{len(pairs)} 对近邻、{len(rings)} 个环；{len(snapped)} 个站位已精确化",
            f"Detected {len(M)} molecules, {len(pairs)} neighbour pairs, {len(rings)} rings; "
            f"{len(snapped)} site(s) refined")

    @staticmethod
    def _molecule_out(xy, geo: FrameGeometry) -> dict:
        sx, sy = frame_nm_to_scanner_m(float(xy[0]), float(xy[1]), geo)
        return {"x_nm": round(float(xy[0]), 4), "y_nm": round(float(xy[1]), 4),
                "scanner_x_m": sx, "scanner_y_m": sy}

    @staticmethod
    def _site_out(s: dict, snap: dict | None, geo: FrameGeometry | None,
                  origin: tuple[float, float]) -> dict:
        """``x_nm / y_nm`` 等是**帧内**偏移;``page_*`` 是同一个点在页面坐标系里的值(页面怎么存的、
        模板画 rx/ry 连线用的就是它)。回写页面的人用 ``page_rx_nm`` / ``page_ry_nm``,别自己加原点。"""
        ox, oy = origin
        out: dict = {"id": s["id"], "n": s["n"], "kind": s["kind"], "wide": s["wide"], "low": s["low"],
                     "note": s["note"], "status": s["status"],
                     "x_nm": round(s["x"], 4), "y_nm": round(s["y"], 4),
                     "page_x_nm": round(s["page_x"], 4), "page_y_nm": round(s["page_y"], 4)}
        x, y = s["x"], s["y"]
        if snap is not None:
            x, y = snap["rx"], snap["ry"]
            out.update(rx_nm=round(x, 4), ry_nm=round(y, 4),
                       page_rx_nm=round(x + ox, 4), page_ry_nm=round(y + oy, 4),
                       rkind=snap["rkind"], rnote=snap["rnote"], shift_nm=round(snap["shift_nm"], 4))
        out["position_source"] = "refined" if snap is not None else "marked"
        out["anchor"] = list(snap["anchor"]) if snap is not None else []
        out["anchor_offset_nm"] = snap["anchor_offset_nm"] if snap is not None else None
        out["in_frame"] = _in_frame(x, y, geo) if geo is not None else None
        out["scanner_x_m"] = out["scanner_y_m"] = None
        if geo is not None and geo.angle_known and out["in_frame"]:
            out["scanner_x_m"], out["scanner_y_m"] = frame_nm_to_scanner_m(x, y, geo)
        return out
