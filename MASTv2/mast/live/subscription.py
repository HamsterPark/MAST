"""一条 ``/ws/live`` 连接的订阅状态：限速、合批、有界，慢客户端拖不垮发布线程。

发布线程（持着 hub 的锁）调 ``_on_event`` 入队；连接的发送协程按节拍调 ``drain()``，拿走编好的
消息文本。两边只在本对象的锁里碰头，锁里只做几次 dict / deque 操作。客户端的 subscribe /
unsubscribe 也先入队，由 ``drain()`` 在同一个消费者里执行 —— 重放快照和实时积压由同一方取走，
同一个流的帧不重、不漏、不乱序。

推送语义（契约 §7）：

* spectrum、scalar 只留最新一帧 —— 一个节拍里来了 10 帧只推最后那帧（被覆盖的不算丢）；
* xy、map2d、status 是增量：不丢，只合批成 ``frames``；
* 每条连接积压的增量帧有上限（默认 50000）。超了就从积压最多的那个流丢最旧的，下一拍先发
  ``{"kind":"dropped","stream","from","to"}`` —— 客户端可以带 ``since`` 重订或走 REST 补齐，
  而不是拿着一个自己看不见的空洞。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from mast.live.hub import LATEST_ONLY_KINDS, LiveEvent, LiveHub, _Axis, _dumps, _Frame
from mast.i18n import tr

#: 一条连接最多积压多少个增量帧（xy / map2d / status）。spectrum / scalar 每个流只占一帧。
MAX_PENDING = 50_000
#: 一条 ``frames`` 消息最多装多少帧、多少字符。
BATCH_ITEMS = 2000
BATCH_CHARS = 1024 * 1024
#: 客户端一条消息的上限，以及一次 subscribe 最多列多少个流。
MAX_OP_CHARS = 64 * 1024
MAX_OP_STREAMS = 512
_SENT_AXES_KEEP = 64


def frame_messages(sj: str, frames: list[_Frame], *, batch_items: int = BATCH_ITEMS,
                   batch_chars: int = BATCH_CHARS) -> list[str]:
    """一帧 → ``frame``；多帧 → 一条或几条 ``frames``（按条数和字符数切批）。"""
    if not frames:
        return []
    if len(frames) == 1:
        f = frames[0]
        return ['{"kind":"frame","stream":%s,"seq":%d,"frame":%s}' % (sj, f.seq, f.text)]
    out: list[str] = []
    items: list[str] = []
    size = 0
    for f in frames:
        item = '{"seq":%d,"frame":%s}' % (f.seq, f.text)
        if items and (len(items) >= batch_items or size + len(item) > batch_chars):
            out.append('{"kind":"frames","stream":%s,"items":[%s]}' % (sj, ",".join(items)))
            items, size = [], 0
        items.append(item)
        size += len(item)
    out.append('{"kind":"frames","stream":%s,"items":[%s]}' % (sj, ",".join(items)))
    return out


def axis_message(sj: str, ax: _Axis) -> str:
    return '{"kind":"axis","stream":%s,"ver":%d,"x":%s,"unit":%s}' % (
        sj, ax.ver, ax.x_text, _dumps(ax.unit))


def dropped_message(sj: str, lo: int, hi: int) -> str:
    return '{"kind":"dropped","stream":%s,"from":%d,"to":%d}' % (sj, lo, hi)


class LiveSubscription:
    """一个订阅者（通常是一条 WS 连接）。用完必须 :meth:`close`，否则 hub 一直往它身上入队。

    ``wake`` 在发布线程上被调用（持着 hub 的锁），只许把消费者叫醒（例如
    ``loop.call_soon_threadsafe(event.set)``）。每个节拍至多调一次。
    """

    def __init__(self, hub: LiveHub, *, wake: Callable[[], None] | None = None,
                 max_pending: int = MAX_PENDING, batch_items: int = BATCH_ITEMS) -> None:
        self._hub = hub
        self._wake = wake
        self._max_pending = max(1, int(max_pending))
        self._batch = max(1, int(batch_items))
        self._lock = threading.Lock()
        self._closed = False
        self._all = False                          # 订阅了 "*"
        self._wanted: set[str] = set()             # 显式订阅的流（可以还没声明）
        self._excluded: set[str] = set()           # 订了 "*" 之后又单独退订的
        self._latest: dict[str, _Frame] = {}       # 只留最新一帧的流
        self._queues: dict[str, deque[_Frame]] = {}  # 增量流：不丢，只合批
        self._pending = 0
        self._dropped: dict[str, list[int]] = {}   # 流 → [from, to]
        self._axis_dirty: set[str] = set()
        self._sent_axes: dict[str, set[int]] = {}  # 这个客户端已经收到的轴版本
        self._streams_dirty = True                 # 连上先发一份目录
        self._ops: list[tuple[str, list[str], dict[str, int]]] = []
        self._errors: list[str] = []
        self._signalled = False
        self._handle = hub.add_listener(self._on_event)

    # —— 发布线程（持 hub 锁）——
    def _wants(self, sid: str) -> bool:
        return sid in self._wanted or (self._all and sid not in self._excluded)

    def _on_event(self, ev: LiveEvent) -> None:
        with self._lock:
            if self._closed:
                return
            if ev.type == "frame":
                if not self._wants(ev.stream) or ev.ref is None:
                    return
                if ev.kind in LATEST_ONLY_KINDS:
                    self._latest[ev.stream] = ev.ref
                else:
                    q = self._queues.get(ev.stream)
                    if q is None:
                        q = self._queues[ev.stream] = deque()
                    q.append(ev.ref)
                    self._pending += 1
                    if self._pending > self._max_pending:
                        self._shed()
            elif ev.type == "axis":
                if not self._wants(ev.stream):
                    return
                self._axis_dirty.add(ev.stream)
            else:  # declare / end / evict：目录变了
                self._streams_dirty = True
                if ev.type == "evict" or ev.reset:
                    self._forget(ev.stream)      # 旧那一版流的积压对新画法没有意义
            signal = not self._signalled
            self._signalled = True
        if signal:
            self._kick()

    def _shed(self) -> None:
        """积压超限：从积压最多的流丢最旧的一帧，记进 dropped 区间。"""
        sid = max(self._queues, key=lambda k: len(self._queues[k]))
        q = self._queues[sid]
        f = q.popleft()
        self._pending -= 1
        if not q:
            del self._queues[sid]
        d = self._dropped.get(sid)
        if d is None:
            self._dropped[sid] = [f.seq, f.seq]
        else:
            d[0] = min(d[0], f.seq)
            d[1] = max(d[1], f.seq)

    def _forget(self, sid: str) -> None:
        """持本对象锁调用：扔掉这个流的全部积压。"""
        self._latest.pop(sid, None)
        q = self._queues.pop(sid, None)
        if q:
            self._pending -= len(q)
        self._dropped.pop(sid, None)
        self._axis_dirty.discard(sid)

    def _kick(self) -> None:
        if self._wake is not None:
            try:
                self._wake()
            except Exception:  # noqa: BLE001 — 叫醒失败只会晚一拍，不许影响发布
                pass

    # —— 客户端请求（任何线程）——
    def request_text(self, text: str) -> None:
        """解析客户端发来的一条消息并入队；格式不对就回一条 ``{"kind":"error"}``，连接照常。"""
        if len(text) > MAX_OP_CHARS:
            return self._error(tr(f"消息太长（> {MAX_OP_CHARS} 字符）", f"Message too long (> {MAX_OP_CHARS} characters)"))
        try:
            msg = json.loads(text)
        except ValueError:
            return self._error(tr("不是 JSON", "Not JSON"))
        if not isinstance(msg, dict):
            return self._error(tr("必须是 JSON 对象", "Must be a JSON object"))
        self.request(msg.get("op"), msg.get("streams"), msg.get("since"))

    def request(self, op: Any, streams: Any, since: Any = None) -> None:
        if op not in ("subscribe", "unsubscribe"):
            return self._error(tr(f"不认识的 op：{op!r}（只有 subscribe / unsubscribe）", f"Unknown op: {op!r} (only subscribe / unsubscribe)"), op)
        if (not isinstance(streams, list) or not streams or len(streams) > MAX_OP_STREAMS
                or not all(isinstance(s, str) for s in streams)):
            return self._error(tr(f"streams 必须是 1…{MAX_OP_STREAMS} 个字符串（\"*\" 表示全部）", f"streams must be 1…{MAX_OP_STREAMS} strings (\"*\" means all)"), op)
        clean: dict[str, int] = {}
        if since is not None:
            if not isinstance(since, dict):
                return self._error(tr("since 必须是 {流 id: seq}", "since must be {stream id: seq}"), op)
            for k, v in since.items():
                if isinstance(v, bool) or not isinstance(v, int) or v < -1:
                    return self._error(tr(f"since[{k!r}] 必须是 ≥ -1 的整数", f"since[{k!r}] must be an integer >= -1"), op)
                clean[str(k)] = v
        with self._lock:
            if self._closed:
                return
            self._ops.append((op, list(streams), clean))
            signal = not self._signalled
            self._signalled = True
        if signal:
            self._kick()

    def _error(self, message: str, op: Any = None) -> None:
        text = _dumps({"kind": "error", "op": op if isinstance(op, str) else None,
                       "message": message})
        with self._lock:
            if self._closed:
                return
            self._errors.append(text)
            signal = not self._signalled
            self._signalled = True
        if signal:
            self._kick()

    # —— 消费者（同一时刻只有一个）——
    def drain(self) -> list[str]:
        """执行积压的订阅请求，取走这一拍要发的全部消息（按顺序发）。

        顺序：目录（变了才发）→ 错误 → 重放 →（每个流）dropped → 轴 → 帧。
        """
        with self._lock:
            if self._closed:
                return []
            self._signalled = False
            ops, self._ops = self._ops, []
            errors, self._errors = self._errors, []
        replay: list[str] = []
        for op, streams, since in ops:
            if op == "subscribe":
                replay.extend(self._subscribe(streams, since))
            else:
                self._unsubscribe(streams)
        with self._lock:
            streams_dirty, self._streams_dirty = self._streams_dirty, False
            dropped, self._dropped = self._dropped, {}
            latest, self._latest = self._latest, {}
            queues, self._queues = self._queues, {}
            self._pending = 0
            axis_dirty, self._axis_dirty = self._axis_dirty, set()
        out: list[str] = []
        if streams_dirty:
            out.append(self._hub.streams_message())
        out.extend(errors)
        out.extend(replay)
        for sid in dict.fromkeys([*dropped, *latest, *queues, *axis_dirty]):
            sj = _dumps(sid)
            if sid in dropped:
                lo, hi = dropped[sid]
                out.append(dropped_message(sj, lo, hi))
            frames = [latest[sid]] if sid in latest else list(queues.get(sid, ()))
            need = {f.axis_ver for f in frames if f.axis_ver}
            if sid in axis_dirty:
                need.add(self._hub._current_axis_ver(sid))
            out.extend(self._axis_messages(sid, sj, need))
            out.extend(frame_messages(sj, frames, batch_items=self._batch))
        return out

    def _subscribe(self, streams: list[str], since: dict[str, int]) -> list[str]:
        hub = self._hub
        wildcard = "*" in streams
        explicit = [s for s in dict.fromkeys(streams) if s != "*"]
        with hub._lock:
            with self._lock:
                if self._closed:
                    return []
                if wildcard:
                    self._all = True
                    self._excluded.clear()
                self._wanted.update(explicit)
                self._excluded.difference_update(explicit)
                targets = list(hub._streams) if wildcard else [s for s in explicit if s in hub._streams]
                for sid in targets:            # 重放会覆盖它们：先扔掉积压，免得重复
                    self._forget(sid)
            snaps = []
            for sid in targets:
                got = hub._replay_locked(sid, since.get(sid, since.get("*")))
                if got is not None:
                    snaps.append((sid, *got))
        out: list[str] = []
        for sid, frames, gap, axes in snaps:
            sj = _dumps(sid)
            if gap is not None:
                out.append(dropped_message(sj, gap[0], gap[1]))
            out.extend(self._axis_messages(sid, sj, {a.ver for a in axes}, axes=axes))
            out.extend(frame_messages(sj, frames, batch_items=self._batch))
        return out

    def _unsubscribe(self, streams: list[str]) -> None:
        with self._lock:
            if "*" in streams:
                self._all = False
                self._wanted.clear()
                self._excluded.clear()
                for sid in [*self._latest, *self._queues, *self._dropped]:
                    self._forget(sid)
                return
            for sid in streams:
                self._wanted.discard(sid)
                if self._all:
                    self._excluded.add(sid)
                self._forget(sid)

    def _axis_messages(self, sid: str, sj: str, need: Iterable[int],
                       axes: list[_Axis] | None = None) -> list[str]:
        with self._lock:
            sent = self._sent_axes.setdefault(sid, set())
            missing = sorted(v for v in need if v and v not in sent)
        if not missing:
            return []
        found = ([a for a in axes if a.ver in missing] if axes is not None
                 else self._hub._axes(sid, missing))
        with self._lock:
            sent = self._sent_axes.setdefault(sid, set())
            sent.update(a.ver for a in found)
            if len(sent) > _SENT_AXES_KEEP * 2:
                keep = sorted(sent)[-_SENT_AXES_KEEP:]
                sent.clear()
                sent.update(keep)
        return [axis_message(sj, a) for a in found]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._latest.clear()
            self._queues.clear()
            self._ops.clear()
            self._pending = 0
        self._hub.remove_listener(self._handle)

    @property
    def pending(self) -> int:
        """积压的增量帧数（测试 / 诊断用）。"""
        with self._lock:
            return self._pending


__all__ = ["LiveSubscription", "MAX_PENDING", "axis_message", "dropped_message",
           "frame_messages"]
