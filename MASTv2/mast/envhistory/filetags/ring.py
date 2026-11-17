# -*- coding: utf-8 -*-
"""读数环：``EnvironmentMonitor`` 的一个 sink，留每个传感器最近约 2 h 的逐条读数（内存）。

为什么要它：

* **窗口统计要逐条读数**：环境历史是 1 分钟桶，桶的 min/max 覆盖整分钟 —— 升场刚结束、扫描随即
  开始，桶里仍有升场时的读数，就成了假的「扫描中磁场变了」；而且当前这一分钟的桶
  还在内存里没落库。
* 文件晚几分钟才被发现，仍能拿到保存那一刻的那条读数。

**它不读任何东西**。监视器自己的 2 s 循环在 ``read_all()`` 之后照常调每个 sink 的
``write(name, value, unit, status)``，这里只是把那条数记下来。新版监视器在归档线程上调，并传
``ts=读数时刻``：条目的时间就是读数的时间，不是写进来的时间（见 ``write``）。

``write`` 的纪律（它跑在监视器线程上，那个线程的首要职责是发现真空 / 温度异常并告警）：

* **O(1) 均摊追加，不做 I/O，不抛**；时间戳在锁外取；
* 锁里只有一次字典查找、一次 ``append``、以及从左边弹掉超过 :data:`HORIZON_S` 的旧条目
  （每条最多被弹一次 ⇒ 均摊 O(1)），持有微秒级；
* 新传感器名进来而环已满 :data:`MAX_SENSORS` 条时，丢掉最久没更新的那条环（O(传感器数)，
  只在出现新名字时发生 —— 重扫改名留下的旧名就这样被淘汰）。

**内存上界** = 传感器数 × 环长 × 每条大小。每条是 ``(t, value, unit, status)``：一个 4 元组加两个
float，单位与状态串是驱动里共享的对象 —— 实测 128 B/条（8 × 4096 条 ≈ 4.2 MB）。硬上界
``MAX_SENSORS × MAXLEN × ENTRY_BYTES`` ≈ 17 MB。条数（``maxlen``）与时间（``horizon_s``）两道
淘汰同时生效。

读的一方（打标的工作线程）在锁里只做一次 ``list(deque)`` 拷贝，查找都在锁外。
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Callable, Iterable

#: 每个传感器最多留的条数。2 s 一拍约 2.3 h。
MAXLEN = 4096
#: 比这更旧的条目在下一次写入时被弹掉（2.5 h）。
HORIZON_S = 9000.0
#: 最多跟踪的传感器名数。
MAX_SENSORS = 32
#: 实测每条约占的字节数（Python 3.13，见模块说明）。只用于 ``stats()`` 的估算与上界。
ENTRY_BYTES = 128

#: 「这一拍真的读到了一个数」的状态。warning / alarm 是越带，不是读不到；
#: error / unavailable 的值是占位的 0.0，不是读数。
ADMITTED = frozenset({"ok", "warning", "alarm"})

Entry = tuple  # (t: float, value: float, unit: str, status: str)


class ReadingRing:
    """每个传感器一个有界 ``deque``。线程安全；``write`` 永不抛。"""

    def __init__(self, *, maxlen: int = MAXLEN, horizon_s: float = HORIZON_S,
                 max_sensors: int = MAX_SENSORS,
                 clock: Callable[[], float] = time.time) -> None:
        self._maxlen = max(1, int(maxlen))
        self._horizon = float(horizon_s)
        self._max_sensors = max(1, int(max_sensors))
        self._clock = clock
        self._lock = threading.Lock()
        self._rings: dict[str, deque] = {}
        self.writes = 0
        self.evicted_sensors = 0

    # ── sink 协议（监视器线程）────────────────────────────────────────

    def write(self, name, value, unit, status, ts=None) -> None:
        """记一条读数。O(1) 均摊；不做 I/O；永不抛。

        ``ts``：读数时刻（``time.time()`` 的浮点秒）。给了就用它打时间戳、也按它剔除过期条目；没给
        （老的 EnvironmentMonitor）就用自己的时钟。新版监视器把各 sink 的写入挪到自己的归档线程，
        用 ``inspect.signature`` 看这里有没有名为 ``ts`` 的参数，有就传读数时刻 —— 归档落后时，写入
        时刻比读数时刻晚，按写入时刻打标会让一条旧读数在文件保存时刻看起来像实时的。"""
        try:
            t = float(self._clock()) if ts is None else float(ts)
            v = float(value)
        except Exception:  # noqa: BLE001 — 记不下来就不记，监视循环照常
            return
        if not math.isfinite(t):
            return
        entry = (t, v, unit if isinstance(unit, str) else str(unit or ""),
                 status if isinstance(status, str) else str(status or ""))
        key = name if isinstance(name, str) else str(name)
        cutoff = t - self._horizon
        try:
            with self._lock:
                dq = self._rings.get(key)
                if dq is None:
                    if len(self._rings) >= self._max_sensors:
                        self._evict_stalest_locked()
                    dq = self._rings[key] = deque(maxlen=self._maxlen)
                dq.append(entry)
                while dq and dq[0][0] < cutoff:
                    dq.popleft()
                self.writes += 1
        except Exception:  # noqa: BLE001
            pass

    def _evict_stalest_locked(self) -> None:
        stalest, stalest_t = None, math.inf
        for k, dq in self._rings.items():
            last = dq[-1][0] if dq else -math.inf
            if last < stalest_t:
                stalest, stalest_t = k, last
        if stalest is not None:
            del self._rings[stalest]
            self.evicted_sensors += 1

    # ── 读（打标的工作线程）──────────────────────────────────────────

    def snapshot(self, name: str) -> list[Entry]:
        """这个传感器的全部条目（拷贝）。锁里只拷一次。"""
        with self._lock:
            dq = self._rings.get(str(name))
            return list(dq) if dq else []

    def names(self) -> list[str]:
        with self._lock:
            return list(self._rings)

    def stats(self) -> dict:
        with self._lock:
            n_entries = sum(len(dq) for dq in self._rings.values())
            n_sensors = len(self._rings)
        return {
            "sensors": n_sensors, "entries": n_entries, "writes": self.writes,
            "evicted_sensors": self.evicted_sensors,
            "approx_bytes": n_entries * ENTRY_BYTES,
            "bound_bytes": self._max_sensors * self._maxlen * ENTRY_BYTES,
            "maxlen": self._maxlen, "horizon_s": self._horizon,
            "max_sensors": self._max_sensors,
        }


# ── 纯函数：在一份条目拷贝上查找（锁外）──────────────────────────────


def _usable(e: Entry, admitted: frozenset) -> bool:
    return e[3] in admitted and math.isfinite(e[1])


def nearest(entries: Iterable[Entry], t: float, max_dt: float,
            admitted: frozenset = ADMITTED) -> Entry | None:
    """离 ``t`` 最近、且 ``|Δt| <= max_dt`` 的可收读数。条目不要求按时间有序（时钟回跳）。"""
    best, best_d = None, math.inf
    for e in entries:
        if not _usable(e, admitted):
            continue
        d = abs(e[0] - t)
        if d <= max_dt and d < best_d:
            best, best_d = e, d
    return best


def last_before(entries: Iterable[Entry], t: float,
                admitted: frozenset = ADMITTED) -> Entry | None:
    """``t`` 之前（含）最后一条可收读数 —— 过期值的来源。"""
    best = None
    for e in entries:
        if _usable(e, admitted) and e[0] <= t and (best is None or e[0] > best[0]):
            best = e
    return best


def nearest_any(entries: Iterable[Entry], t: float, max_dt: float) -> Entry | None:
    """离 ``t`` 最近的任意一条（含 error / unavailable）—— 给「缺失」说原因用。"""
    best, best_d = None, math.inf
    for e in entries:
        d = abs(e[0] - t)
        if d <= max_dt and d < best_d:
            best, best_d = e, d
    return best


def earliest(entries: Iterable[Entry]) -> float | None:
    ts = [e[0] for e in entries]
    return min(ts) if ts else None


def window_stats(entries: Iterable[Entry], t0: float, t1: float,
                 admitted: frozenset = ADMITTED) -> dict | None:
    """``[t0, t1]`` 内可收读数的 ``{min, max, n, first, last, unit}``；一条都没有 ⇒ None。"""
    lo = hi = first = last = None
    n = 0
    unit = ""
    for e in entries:
        if not (t0 <= e[0] <= t1) or not _usable(e, admitted):
            continue
        n += 1
        v = e[1]
        lo = v if lo is None else min(lo, v)
        hi = v if hi is None else max(hi, v)
        first = e[0] if first is None else min(first, e[0])
        if last is None or e[0] >= last:
            last, unit = e[0], e[2]
    if n == 0:
        return None
    return {"min": lo, "max": hi, "n": n, "first": first, "last": last, "unit": unit}


__all__ = ["ReadingRing", "ADMITTED", "MAXLEN", "HORIZON_S", "MAX_SENSORS", "ENTRY_BYTES",
           "nearest", "last_before", "nearest_any", "earliest", "window_stats"]
