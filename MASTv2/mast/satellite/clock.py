"""逐心跳估计两机时钟偏差（契约 §5.3「对时」）。

一次 ping/pong 给出四个时刻（都是 unix ms）：t0 枢纽发出、t1 卫星收到、t2 卫星发出、
t3 枢纽收到。

    θ = ((t1 − t0) + (t2 − t3)) / 2      卫星钟比枢纽钟快多少
    δ = (t3 − t0) − (t2 − t1)            网络往返时延（扣掉卫星处理时间）

单个样本的 θ 误差上界是 δ/2，所以取最近 32 个样本里 δ 最小的那个的 θ（NTP 的做法）。
卫星时间换到枢纽时钟：``ts_hub = ts_sat − θ``。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ClockSample:
    theta_ms: float
    delta_ms: float
    t3_ms: int


class ClockEstimator:
    #: 两边的钟在一个样本期间被校时，δ 会算成负的。小于这个阈值的样本直接丢弃。
    NEGATIVE_DELTA_TOLERANCE_MS = 5.0

    def __init__(self, window: int = 32) -> None:
        self._samples: deque[ClockSample] = deque(maxlen=max(1, int(window)))
        self._lock = threading.Lock()
        self.rejected = 0

    def add(self, t0: int, t1: int, t2: int, t3: int) -> ClockSample | None:
        """加一个样本。不合理的样本（δ 明显为负、卫星处理时间为负）返回 None 并计数。"""
        theta = ((t1 - t0) + (t2 - t3)) / 2.0
        delta = (t3 - t0) - (t2 - t1)
        if (t2 - t1) < -self.NEGATIVE_DELTA_TOLERANCE_MS or delta < -self.NEGATIVE_DELTA_TOLERANCE_MS:
            with self._lock:
                self.rejected += 1
            return None
        s = ClockSample(theta_ms=float(theta), delta_ms=float(max(0.0, delta)), t3_ms=int(t3))
        with self._lock:
            self._samples.append(s)
        return s

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()

    def _best(self) -> ClockSample | None:
        with self._lock:
            if not self._samples:
                return None
            return min(self._samples, key=lambda s: s.delta_ms)

    @property
    def offset_ms(self) -> float | None:
        b = self._best()
        return None if b is None else b.theta_ms

    @property
    def delay_ms(self) -> float | None:
        """最好样本的往返时延（也就是 θ 的不确定度的两倍上界）。"""
        b = self._best()
        return None if b is None else b.delta_ms

    @property
    def rtt_ms(self) -> float | None:
        """最近一个样本的往返时延（反映当前链路，而不是最好时刻）。"""
        with self._lock:
            return self._samples[-1].delta_ms if self._samples else None

    @property
    def n(self) -> int:
        with self._lock:
            return len(self._samples)

    def to_hub_ms(self, ts_sat_ms: float | int | None) -> float | None:
        if ts_sat_ms is None:
            return None
        th = self.offset_ms
        return float(ts_sat_ms) - (th if th is not None else 0.0)

    def snapshot(self) -> dict:
        return {"offset_ms": self.offset_ms, "delay_ms": self.delay_ms,
                "rtt_ms": self.rtt_ms, "samples": self.n, "rejected": self.rejected}


__all__ = ["ClockEstimator", "ClockSample"]
