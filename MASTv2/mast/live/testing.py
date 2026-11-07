"""给测试用的 LiveHub 工具：换一个干净的单例，记下发布了什么。

    from mast.live.testing import LiveRecorder, isolated_live_hub

    def test_acquire_publishes_the_spectrum():
        with isolated_live_hub() as hub, LiveRecorder(hub) as rec:
            run_the_skill()                       # 里面调 get_live_hub().publish(...)
        assert rec.declared() == ["sat1/ccd/spectrum"]
        assert rec.frames("sat1/ccd/spectrum")[-1]["y"][:3] == [10.0, 11.0, 12.0]
        assert rec.ended() == ["sat1/ccd/spectrum"]

``isolated_live_hub`` 默认是**严格模式**：没 declare 就 publish、帧格式不对、轴长度不符都直接抛
``LiveError`` —— 生产单例里这些只记一条 WARNING，测试里要让它响。被测代码必须在用的时候调
``get_live_hub()``（别在 import 时缓存），换单例才换得到它。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mast.live import hub as _hub
from mast.live.hub import LiveEvent, LiveHub, get_live_hub


@contextmanager
def isolated_live_hub(*, strict: bool = True, **kwargs: Any) -> Iterator[LiveHub]:
    """把进程级单例换成一个全新的 LiveHub，退出时换回原来的。``kwargs`` 透传给 ``LiveHub``。"""
    fresh = LiveHub(strict=strict, **kwargs)
    prev = _hub._install(fresh)
    try:
        yield fresh
    finally:
        _hub._install(prev)


class LiveRecorder:
    """订阅一个 hub 的全部事件，按发生顺序原样记下来。

    和 ``/ws/live`` 不同：不限速、不合并、不丢 —— 记满 ``limit`` 条就把 ``overflowed`` 置真并停记
    （断言前查一下，别让「记不下」冒充「没发」）。每条记录是 dict：
    ``{"type": "declare"|"axis"|"frame"|"end"|"evict", "stream", "kind", ...}``，frame 另有
    ``seq`` / ``frame`` / ``axis_ver``，axis 另有 ``ver``，declare 另有 ``reset``。
    """

    def __init__(self, hub: LiveHub | None = None, *, limit: int = 200_000) -> None:
        self.hub = hub if hub is not None else get_live_hub()
        self.events: list[dict] = []
        self.overflowed = False
        self._limit = limit
        self._lock = threading.Lock()
        self._handle: int | None = self.hub.add_listener(self._on_event)

    def _on_event(self, ev: LiveEvent) -> None:
        rec: dict = {"type": ev.type, "stream": ev.stream, "kind": ev.kind}
        if ev.type == "frame":
            rec.update(seq=ev.seq, frame=ev.frame, axis_ver=ev.axis_ver)
        elif ev.type == "axis":
            rec["ver"] = ev.axis_ver
        elif ev.type == "declare":
            rec["reset"] = ev.reset
        with self._lock:
            if len(self.events) >= self._limit:
                self.overflowed = True
                return
            self.events.append(rec)

    # —— 查询 ——
    def of(self, type: str, stream_id: str | None = None) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e["type"] == type
                    and (stream_id is None or e["stream"] == stream_id)]

    def frames(self, stream_id: str) -> list[dict]:
        """这个流发布过的每一帧（清洗后的样子，按 seq 顺序）。"""
        return [e["frame"] for e in self.of("frame", stream_id)]

    def seqs(self, stream_id: str) -> list[int]:
        return [e["seq"] for e in self.of("frame", stream_id)]

    def declared(self) -> list[str]:
        return list(dict.fromkeys(e["stream"] for e in self.of("declare")))

    def ended(self) -> list[str]:
        return list(dict.fromkeys(e["stream"] for e in self.of("end")))

    def axes(self, stream_id: str) -> list[dict]:
        """这个流设过的每一版横轴 ``{"ver", "unit", "n", "x"}``（已被 hub 淘汰的版本跳过）。"""
        out = []
        for e in self.of("axis", stream_id):
            ax = self.hub.axis(stream_id, e["ver"])
            if ax is not None:
                out.append(ax)
        return out

    def close(self) -> None:
        if self._handle is not None:
            self.hub.remove_listener(self._handle)
            self._handle = None

    def __enter__(self) -> LiveRecorder:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


__all__ = ["LiveRecorder", "isolated_live_hub"]
