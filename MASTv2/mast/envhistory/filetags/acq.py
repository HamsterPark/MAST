# -*- coding: utf-8 -*-
"""数据文件头 → 采集窗口。只读文件头，不读数据块。

时刻的解析与图库同一套（``mast.gallery.inventory``：格式串 ``%d.%m.%Y %H:%M:%S``、按本机时区、
同样的兜底），测试钉住两边的起点相等 —— sidecar 里的窗口起点与图库卡片上的开始时刻必须是
同一个数，否则一个文件会有两个「什么时候开始扫的」。

==========  =======================================  ============================================
文件        起点                                      终点
==========  =======================================  ============================================
``.sxm``    ``REC_DATE + REC_TIME``；没有 ⇒ mtime − ACQ  ``min(mtime, 起点 + ACQ_TIME)``；没 ACQ ⇒ mtime
``.dat``    ``Start time``；没有 ⇒ ``Saved Date``          mtime
``.3ds``    ``Start time``                            ``End time``；没有 ⇒ mtime
==========  =======================================  ============================================

``.sxm`` 的终点取 ``min``：扫完之后隔了几分钟才点「保存」时，mtime 比采集结束晚，窗口不该把
那段空等也算进去。起点晚于终点、跨度 < 1 s 或 > 7 天 ⇒ 不给窗口（头里的时刻不可信）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

_FMT = "%d.%m.%Y %H:%M:%S"
MIN_SPAN_S = 1.0
MAX_SPAN_S = 7 * 86400.0
#: 起点最多比 mtime 晚这么多（时钟、秒级取整）；再晚就是头里的时刻不对。
FUTURE_SLACK_S = 60.0


@dataclass(frozen=True)
class AcqWindow:
    start: float
    end: float
    start_from: str
    end_from: str

    @property
    def duration_s(self) -> float:
        return self.end - self.start


def parse_local(text) -> float | None:
    """``"10.09.2001 15:07:12"`` → epoch 秒（本机时区）。解析不了 ⇒ None。"""
    s = str(text or "").strip()
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s, _FMT))
    except (ValueError, OverflowError):
        return None


def _num(text) -> float | None:
    try:
        v = float(str(text).split()[0])
    except (ValueError, IndexError, TypeError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _sxm(path: str, mtime: float) -> tuple | None:
    from mast.io.nanonis_files import read_sxm_header

    h = read_sxm_header(path)
    if not h:
        return None
    rec_d = str(h.get("rec_date") or "").strip()
    rec_t = str(h.get("rec_time") or "").strip()
    acq = _num(h.get("acq_time"))
    start = parse_local(f"{rec_d} {rec_t}") if (rec_d and rec_t) else None
    if start is not None:
        if acq and acq > 0 and start + acq < mtime:
            return start, "REC_DATE/REC_TIME", start + acq, "REC+ACQ_TIME"
        return start, "REC_DATE/REC_TIME", mtime, "mtime"
    if acq and acq > 0:
        return mtime - acq, "mtime-ACQ_TIME", mtime, "mtime"
    return None


#: .dat 文件头最多读这么多行（Nanonis 的头几十行；再往后没有 [DATA] 就不是我们要的格式）。
DAT_HEADER_MAX_LINES = 2000


def dat_header(path: str) -> dict[str, str]:
    """.dat 的文件头：``[DATA]`` 之前的 ``key<TAB>value``，**只读头，不解析数据块**。

    规则与 ``mast.io.nanonis_files.read_dat`` 的头部分逐字相同（同样的打开方式、同样的切分），
    测试钉住两边对同一个文件给出同一份头。"""
    header: dict[str, str] = {}
    with open(path, "r", errors="replace") as f:
        for i, line in enumerate(f):
            if i >= DAT_HEADER_MAX_LINES:
                break
            s = line.rstrip("\r\n")
            if s.strip() == "[DATA]":
                break
            if not s.strip():
                continue
            parts = s.split("\t")
            key = parts[0].strip()
            if not key:
                continue
            header[key] = "\t".join(parts[1:]).strip() if len(parts) > 1 else ""
    return header


def _dat(path: str, mtime: float) -> tuple | None:
    hdr = dat_header(path)
    for key in ("Start time", "Saved Date"):
        start = parse_local(hdr.get(key))
        if start is not None:
            return start, key, mtime, "mtime"
    return None


def _3ds(path: str, mtime: float) -> tuple | None:
    from mast.gallery.grid3ds import read_3ds

    g = (read_3ds(path, data=False) or {}).get("hdr") or {}
    start = parse_local(g.get("Start time"))
    if start is None:
        return None
    end = parse_local(g.get("End time"))
    if end is not None:
        return start, "Start time", end, "End time"
    return start, "Start time", mtime, "mtime"


_READERS = {".sxm": _sxm, ".dat": _dat, ".3ds": _3ds}


def acquisition_window(path, *, mtime: float | None = None) -> AcqWindow | None:
    """采集窗口；头读不出、或时刻不可信 ⇒ None。永不抛。"""
    p = str(path)
    reader = _READERS.get(os.path.splitext(p)[1].lower())
    if reader is None:
        return None
    try:
        mt = float(mtime) if mtime is not None else os.path.getmtime(p)
        got = reader(p, mt)
    except Exception:  # noqa: BLE001 — 坏文件 / 正在写的文件：没有窗口，不是事故
        return None
    if not got:
        return None
    start, start_from, end, end_from = got
    span = end - start
    if span < MIN_SPAN_S or span > MAX_SPAN_S or start > mt + FUTURE_SLACK_S:
        return None
    return AcqWindow(float(start), float(end), start_from, end_from)


__all__ = ["AcqWindow", "DAT_HEADER_MAX_LINES", "acquisition_window", "dat_header", "parse_local"]
