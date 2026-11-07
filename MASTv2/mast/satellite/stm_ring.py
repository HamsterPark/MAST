"""STM 状态的 1 小时环形缓冲（契约 §11.2）。

卫星传上来的光谱文件要附「采集当时的 STM 状态」。EventBus 只重放 100 条，所以枢纽自己订阅
``HARDWARE_STATE``（``InstrumentState.refresh`` 约 1 Hz 发一次），按时间保留最近一小时，
文件落地时按采集时间窗匹配。

匹配的是**枢纽时钟**：调用方先把卫星时间换算过来（``ts_hub = ts_sat − θ``）。
订阅回调跑在发布线程（状态刷新线程）上，只做一次加锁追加。
"""

from __future__ import annotations

import bisect
import logging
import math
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

#: 数值字段：时间窗内给均值 / 标准差 / 极值。
NUMERIC_FIELDS = ("bias_v", "current_a", "z_m", "setpoint_a")
#: 布尔字段：时间窗内给 all / any。
BOOL_FIELDS = ("z_controller_on", "scan_running", "withdrawn")
#: 窗口外最多往外找多远的「前一条 / 后一条」。超过这个距离的样本不能代表那一刻。
NEIGHBOUR_MAX_S = 10.0


class StmStateRing:
    def __init__(self, max_age_s: float = 3600.0, max_len: int = 20000,
                 now_fn=time.time) -> None:
        self.max_age_s = float(max_age_s)
        self._ts: deque[float] = deque(maxlen=max_len)      # unix 秒
        self._data: deque[dict] = deque(maxlen=max_len)
        self._lock = threading.Lock()
        self._now = now_fn
        self._bus = None
        self._cb = None

    # ── 订阅 ────────────────────────────────────────────────────────────
    def start(self, bus=None) -> bool:
        """订阅 EventBus 的 HARDWARE_STATE。重复调用无副作用。"""
        if self._cb is not None:
            return True
        try:
            from mast.core.events import EventBus, EventType

            b = bus or EventBus.get()

            def _on_event(ev, _t=EventType.HARDWARE_STATE) -> None:
                if getattr(ev, "type", None) is not _t:
                    return
                self.add(float(getattr(ev, "timestamp", time.time())), dict(ev.data or {}))

            b.subscribe(_on_event)
            self._bus, self._cb = b, _on_event
            return True
        except Exception as exc:  # noqa: BLE001 — 没有总线就只是没有 STM 快照
            logger.warning("STM 状态环形缓冲订阅失败（文件元数据将没有 STM 快照）：%s", exc)
            return False

    def stop(self) -> None:
        if self._bus is not None and self._cb is not None:
            try:
                self._bus.unsubscribe(self._cb)
            except Exception:  # noqa: BLE001
                pass
        self._bus = self._cb = None

    # ── 写 ──────────────────────────────────────────────────────────────
    def add(self, ts_s: float, data: dict) -> None:
        with self._lock:
            if self._ts and ts_s < self._ts[-1]:
                # 乱序（极少见：两个线程同时发布）。按时间插回去，保持有序。
                if len(self._ts) == self._ts.maxlen:
                    self._ts.popleft()
                    self._data.popleft()
                i = bisect.bisect_right(list(self._ts), ts_s)
                self._ts.insert(i, ts_s)
                self._data.insert(i, data)
            else:
                self._ts.append(ts_s)
                self._data.append(data)
            self._prune_locked(self._now())

    def _prune_locked(self, now_s: float) -> None:
        cutoff = now_s - self.max_age_s
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()
            self._data.popleft()

    def __len__(self) -> int:
        with self._lock:
            return len(self._ts)

    # ── 读 ──────────────────────────────────────────────────────────────
    def coverage(self) -> dict:
        with self._lock:
            if not self._ts:
                return {"n": 0, "oldest_ms": None, "newest_ms": None}
            return {"n": len(self._ts), "oldest_ms": int(self._ts[0] * 1000),
                    "newest_ms": int(self._ts[-1] * 1000)}

    def window(self, t0_ms: float | None, t1_ms: float | None) -> dict:
        """``[t0, t1]``（枢纽时钟，unix ms）期间的 STM 状态。

        返回 ``{before, after, during, coverage, matched}``：
        - ``before``：t0 之前（含）最近的一条，距离不超过 10 s；
        - ``after``：t1 之后（含）最近的一条，距离不超过 10 s；还没到就是 None，``after_pending=True``；
        - ``during``：窗口内样本数，数值字段的 mean/std/min/max，布尔字段的 all/any。
        时间不明（None）时 ``matched=False``，只给 coverage。
        """
        cov = self.coverage()
        if t0_ms is None and t1_ms is None:
            return {"matched": False, "reason": "no_time", "coverage": cov}
        if t0_ms is None:
            t0_ms = t1_ms
        if t1_ms is None:
            t1_ms = t0_ms
        lo, hi = sorted((float(t0_ms) / 1000.0, float(t1_ms) / 1000.0))
        with self._lock:
            ts = list(self._ts)
            data = list(self._data)
        if not ts:
            return {"matched": False, "reason": "ring_empty", "coverage": cov}
        if hi < ts[0] - NEIGHBOUR_MAX_S:
            return {"matched": False, "reason": "older_than_ring", "coverage": cov}
        i0 = bisect.bisect_right(ts, lo) - 1          # 最后一条 ≤ lo
        i1 = bisect.bisect_left(ts, hi)               # 第一条 ≥ hi
        before = None
        if i0 >= 0 and lo - ts[i0] <= NEIGHBOUR_MAX_S:
            before = {"ts_ms": int(ts[i0] * 1000), "age_s": round(lo - ts[i0], 3), **data[i0]}
        after = None
        if i1 < len(ts) and ts[i1] - hi <= NEIGHBOUR_MAX_S:
            after = {"ts_ms": int(ts[i1] * 1000), "lag_s": round(ts[i1] - hi, 3), **data[i1]}
        a = bisect.bisect_left(ts, lo)
        b = bisect.bisect_right(ts, hi)
        inside = data[a:b]
        during: dict[str, Any] = {"n": len(inside)}
        for f in NUMERIC_FIELDS:
            vals = [float(d[f]) for d in inside
                    if isinstance(d.get(f), (int, float)) and not isinstance(d.get(f), bool)
                    and math.isfinite(float(d[f]))]
            if vals:
                m = sum(vals) / len(vals)
                var = sum((v - m) ** 2 for v in vals) / len(vals)
                during[f] = {"mean": m, "std": math.sqrt(var), "min": min(vals),
                             "max": max(vals), "n": len(vals)}
        for f in BOOL_FIELDS:
            vals = [bool(d[f]) for d in inside if isinstance(d.get(f), bool)]
            if vals:
                during[f] = {"all": all(vals), "any": any(vals), "n": len(vals)}
        return {
            "matched": before is not None or after is not None or bool(inside),
            "window_ms": [int(lo * 1000), int(hi * 1000)],
            "before": before,
            "after": after,
            "after_pending": after is None and hi > ts[-1],
            "during": during,
            "coverage": cov,
        }


__all__ = ["StmStateRing", "NUMERIC_FIELDS", "BOOL_FIELDS"]
