"""LiveHub —— 新增数据采集的实时绘图通道（契约：docs/v2/design/ters_satellite_integration.md §7）。

每一路新增的数据采集都要能在 MAST 页面上实时绘图。采集方（卫星枢纽、技能、驱动）往这里发帧，
``WS /ws/live`` 推给浏览器，``GET /api/live/streams/…`` 是轮询兜底::

    from mast.live import get_live_hub
    live = get_live_hub()
    live.declare("sat1/ccd/spectrum", "spectrum", title="拉曼 CCD", source="sat1",
                 x_label="波长", x_unit="nm", y_label="计数", meta={"laser_line_nm": 632.8})
    ver = live.set_axis("sat1/ccd/spectrum", wavelengths_nm, unit="nm")
    live.publish("sat1/ccd/spectrum", {"t": time.time(), "y": counts, "axis_ver": ver})
    live.end("sat1/ccd/spectrum")

为什么不走 EventBus：它只重放 100 条（``core/events.py``），几十帧光谱就把别的事件全挤出去；
EventBus 上只发指针式的状态变化（契约陷阱 12）。

线程模型
--------
* ``publish`` 任何线程可调。校验和序列化在锁外做；持锁的那一小段只分配 seq、写进环形缓冲、
  把帧交给每个订阅者的入队函数。**不做 I/O，不等任何人，不抛异常**（严格模式除外，见下）。
* 订阅者（``/ws/live`` 的每条连接、测试里的 ``LiveRecorder``）在发布线程上、持着 hub 的锁被同步
  调用，**只许入队** —— 和 EventBus、卫星枢纽 ``add_listener`` 是同一条规矩。持锁调用是为了让
  同一个流的帧按 seq 的顺序到达每个订阅者，并且「登记订阅 + 取重放快照」是原子的（不重不漏）。
  慢客户端只会让它自己那条连接的积压满、收到 ``dropped``，碰不到发布线程。
* 帧在发布时序列化成 JSON 文本只存一份：N 个浏览器共用同一份文本，不重复编码；也比存 Python
  float 列表省内存（1340 点的光谱约 13 KB 文本，列表约 43 KB）。

``seq`` 按流编号、从 1 起连续递增；同一个流 id 在进程内永不回退（重新 declare、换 kind 重置、
被淘汰后再声明都接着编）。进程重启后 ``hub_id`` 会变 —— 客户端看到它变了就该丢掉手里的 since。

严格模式（``LiveHub(strict=True)``；测试用 ``mast.live.testing.isolated_live_hub``）：用错就抛
``LiveError``。生产单例不严格：用错只记一条 WARNING（同一操作、同一个流只记一次），``publish`` /
``set_axis`` 返回 -1，``declare`` / ``end`` 什么也不做 —— 显示上的问题不许拖垮一次真实的采集。
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

import numpy as np
from mast.i18n import tr

logger = logging.getLogger(__name__)

KINDS: tuple[str, ...] = ("spectrum", "scalar", "xy", "map2d", "status")
#: 推给浏览器时只留最新一帧的 kind（契约 §7）。其余是增量：不丢，只合批。
LATEST_ONLY_KINDS: frozenset[str] = frozenset({"spectrum", "scalar"})

#: 每个流保留多少帧（map2d 是像素数）。契约 §7 的默认值；``declare(retain=)`` 可改，但不超过上限。
DEFAULT_RETAIN: dict[str, int] = {"spectrum": 200, "scalar": 5000, "xy": 20000,
                                  "map2d": 262144, "status": 500}
MAX_RETAIN: dict[str, int] = {"spectrum": 2000, "scalar": 100_000, "xy": 200_000,
                              "map2d": 262144, "status": 5000}
MAP2D_MAX_PIXELS = 262144
#: 流的总数上限；超出时先淘汰最早结束的流，没有已结束的再淘汰最久没动静的。
MAX_STREAMS = 256
#: 所有流保留的帧文本合计上限（按字符数近似字节）。超了先整条淘汰已结束的流，再从占用最大的
#: 活动流里剪最旧的帧（每个流至少留最新一帧）。
MAX_BYTES = 256 * 1024 * 1024
MAX_FRAME_CHARS = 2 * 1024 * 1024
MAX_POINTS = 131072
MAX_VALUES = 64
MAX_NAME = 64
MAX_TEXT = 4000
MAX_META_CHARS = 16 * 1024
#: 每个 spectrum 流保留最近多少个横轴版本（重放旧帧时要带上它引用的那版轴）。
AXIS_HISTORY = 16
STATUS_LEVELS: frozenset[str] = frozenset({"debug", "info", "warn", "error"})

_TOMBSTONES = 4096           # 被淘汰的流 id → 最后的 seq / 轴版本，再声明时接着编号
_MS_LIKE = 1e11              # |t| 比这大就不像 unix 秒了（公元 5138 年），像是毫秒
_MAX_ID = 200
_ID_RE = re.compile(r"[A-Za-z0-9_.:+@-]+(?:/[A-Za-z0-9_.:+@-]+)*")
_MAP_KEYS = ("nx", "ny", "x0", "y0", "dx", "dy", "x_unit", "y_unit")


class LiveError(ValueError):
    """用错了 LiveHub：没 declare 的流、帧格式不对、轴长度不符……只在严格模式下抛出来。"""


# ── 校验与清洗（锁外执行）────────────────────────────────────────────────────

def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _check_id(stream_id: Any) -> str:
    if (not isinstance(stream_id, str) or len(stream_id) > _MAX_ID
            or not _ID_RE.fullmatch(stream_id)
            or any(seg in (".", "..") for seg in stream_id.split("/"))):
        raise LiveError(tr(f"stream_id 不合法：{stream_id!r}（形如 来源/设备/通道；"
                           f"字符限 A-Z a-z 0-9 _ . : + @ -，≤ {_MAX_ID} 字符）",
                           f"Invalid stream_id: {stream_id!r} (form source/device/channel; "
                           f"allowed characters A-Z a-z 0-9 _ . : + @ -, at most {_MAX_ID} characters)"))
    return stream_id


def _text(v: Any, what: str, *, limit: int = 200, required: bool = False) -> str | None:
    if v is None:
        if required:
            raise LiveError(tr(f"{what} 必填", f"{what} is required"))
        return None
    if not isinstance(v, str):
        raise LiveError(tr(f"{what} 必须是字符串：{type(v).__name__}", f"{what} must be a string: {type(v).__name__}"))
    v = v.strip()[:limit]
    if required and not v:
        raise LiveError(tr(f"{what} 不能为空", f"{what} must not be empty"))
    return v or None


def _num(v: Any, what: str) -> float | None:
    """一个数 → float；NaN / ±Inf / None → None（JSON 没有 NaN，浏览器的 JSON.parse 会整条消息报错）。"""
    if v is None:
        return None
    if isinstance(v, (bool, str, bytes, bytearray)):
        raise LiveError(tr(f"{what} 不是数：{v!r}", f"{what} is not a number: {v!r}"))
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise LiveError(tr(f"{what} 不是数：{type(v).__name__}", f"{what} is not a number: {type(v).__name__}")) from None
    return f if math.isfinite(f) else None


def _finite(v: Any, what: str) -> float:
    f = _num(v, what)
    if f is None:
        raise LiveError(tr(f"{what} 必须是有限的数：{v!r}", f"{what} must be a finite number: {v!r}"))
    return f


def _int(v: Any, what: str) -> int:
    if isinstance(v, bool):
        raise LiveError(tr(f"{what} 必须是整数：{v!r}", f"{what} must be an integer: {v!r}"))
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)) and float(v).is_integer():
        return int(v)
    raise LiveError(tr(f"{what} 必须是整数：{v!r}", f"{what} must be an integer: {v!r}"))


def _num_array(seq: Any, what: str, *, finite: bool = False) -> list:
    """一维数值数组（list / tuple / numpy）→ list[float]；非有限值 → None（``finite`` 时直接拒绝）。"""
    if seq is None or isinstance(seq, (str, bytes, bytearray, Mapping)):
        raise LiveError(tr(f"{what} 必须是一维数值数组", f"{what} must be a 1-D numeric array"))
    try:
        a = np.asarray(seq, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise LiveError(tr(f"{what} 不是数值数组：{exc}", f"{what} is not a numeric array: {exc}")) from None
    if a.ndim != 1 or not 1 <= a.shape[0] <= MAX_POINTS:
        raise LiveError(tr(f"{what} 必须是一维、1…{MAX_POINTS} 个点（实际形状 {a.shape}）", f"{what} must be 1-D with 1…{MAX_POINTS} points (actual shape {a.shape})"))
    ok = np.isfinite(a)
    if bool(ok.all()):
        return a.tolist()
    if finite:
        raise LiveError(tr(f"{what} 里有 NaN / Inf", f"{what} contains NaN / Inf"))
    return [v if good else None for v, good in zip(a.tolist(), ok.tolist())]


def _values(v: Any) -> dict[str, float | None]:
    if not isinstance(v, Mapping) or not v:
        raise LiveError(tr("values 必须是非空的 {名称: 数}", "values must be a non-empty {name: number}"))
    if len(v) > MAX_VALUES:
        raise LiveError(tr(f"values 最多 {MAX_VALUES} 个通道（实际 {len(v)}）", f"values allows at most {MAX_VALUES} channels (actual {len(v)})"))
    out: dict[str, float | None] = {}
    for name, x in v.items():
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
            raise LiveError(tr(f"values 的通道名不合法：{name!r}", f"Invalid channel name in values: {name!r}"))
        out[name] = _num(x, f"values[{name!r}]")
    return out


def _json_safe(obj: Any, depth: int = 0) -> Any:
    """meta 只留 JSON 表达得了的东西。非有限 float → None；numpy → Python；别的对象 → str()。
    meta 是给人看的描述，一个 datetime 不值得让整条流声明失败。"""
    if depth > 8:
        raise LiveError(tr("meta 嵌套太深（> 8 层）", "meta is nested too deeply (> 8 levels)"))
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, np.generic):
        return _json_safe(obj.item(), depth + 1)
    if isinstance(obj, np.ndarray):
        return _json_safe(obj.tolist(), depth + 1)
    if isinstance(obj, Mapping):
        return {str(k): _json_safe(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v, depth + 1) for v in obj]
    return str(obj)


def _clean_meta(meta: Any) -> dict:
    if meta is None:
        return {}
    if not isinstance(meta, Mapping):
        raise LiveError(tr(f"meta 必须是 dict：{type(meta).__name__}", f"meta must be a dict: {type(meta).__name__}"))
    clean = _json_safe(meta)
    if len(_dumps(clean)) > MAX_META_CHARS:
        raise LiveError(tr(f"meta 太大（> {MAX_META_CHARS} 字符）—— 大块数据请作为帧发", f"meta is too large (> {MAX_META_CHARS} characters) - send large data as a frame"))
    return clean


def _map_geometry(meta: dict) -> tuple[int, int]:
    missing = [k for k in _MAP_KEYS if k not in meta]
    if missing:
        raise LiveError(tr(f"map2d 的 meta 必须给 {list(_MAP_KEYS)}，缺 {missing}", f"meta of map2d must give {list(_MAP_KEYS)}, missing {missing}"))
    nx, ny = meta["nx"], meta["ny"]
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in (nx, ny)):
        raise LiveError(tr(f"map2d 的 nx / ny 必须是正整数：{nx!r}, {ny!r}", f"nx / ny of map2d must be positive integers: {nx!r}, {ny!r}"))
    for k in ("x0", "y0", "dx", "dy"):
        v = meta[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise LiveError(tr(f"map2d 的 {k} 必须是有限的数：{v!r}", f"{k} of map2d must be a finite number: {v!r}"))
    if meta["dx"] == 0 or meta["dy"] == 0:
        raise LiveError(tr("map2d 的 dx / dy 不能为 0", "dx / dy of map2d must not be 0"))
    for k in ("x_unit", "y_unit"):
        if not isinstance(meta[k], str):
            raise LiveError(tr(f"map2d 的 {k} 必须是字符串：{meta[k]!r}", f"{k} of map2d must be a string: {meta[k]!r}"))
    return nx, ny


def _retain_for(kind: str, retain: Any, geometry: tuple[int, int] | None) -> int:
    hi = MAX_RETAIN[kind]
    if geometry is not None:
        hi = min(hi, geometry[0] * geometry[1])
    if retain is None:
        return min(DEFAULT_RETAIN[kind], hi)
    if isinstance(retain, bool) or not isinstance(retain, int) or retain < 1:
        raise LiveError(tr(f"retain 必须是正整数：{retain!r}", f"retain must be a positive integer: {retain!r}"))
    return min(retain, hi)


def _time(v: Any, now: float) -> float:
    if v is None:
        return now
    t = _finite(v, "t")
    if abs(t) > _MS_LIKE:
        raise LiveError(tr(f"t={t!r} 不像 unix 秒（像毫秒？t 的单位是秒）", f"t={t!r} does not look like unix seconds (milliseconds? the unit of t is seconds)"))
    return t


def _build_frame(kind: str, frame: Any, geometry: tuple[int, int] | None,
                 cur_ver: int, now: float) -> tuple[dict, tuple[int, int] | None]:
    """按 kind 校验并重建一帧（只留契约里的字段）。返回 (帧, map2d 的像素键)。"""
    if not isinstance(frame, Mapping):
        raise LiveError(tr(f"帧必须是 dict：{type(frame).__name__}", f"A frame must be a dict: {type(frame).__name__}"))
    t = _time(frame.get("t"), now)
    if kind == "spectrum":
        y = _num_array(frame.get("y"), "y")
        ver = frame.get("axis_ver")
        ver = cur_ver if ver is None else _int(ver, "axis_ver")
        if ver < 0:
            raise LiveError(tr(f"axis_ver 不能为负：{ver}", f"axis_ver must not be negative: {ver}"))
        body: dict = {"t": t, "y": y, "axis_ver": ver}
        if frame.get("meta") is not None:
            body["meta"] = _clean_meta(frame["meta"])
        return body, None
    if kind == "scalar":
        return {"t": t, "values": _values(frame.get("values"))}, None
    if kind == "xy":
        return {"t": t, "x": _finite(frame.get("x"), "x"),
                "values": _values(frame.get("values"))}, None
    if kind == "map2d":
        assert geometry is not None
        nx, ny = geometry
        ix, iy = _int(frame.get("ix"), "ix"), _int(frame.get("iy"), "iy")
        if not (0 <= ix < nx and 0 <= iy < ny):
            raise LiveError(tr(f"像素 ({ix}, {iy}) 不在 {nx}×{ny} 的图里", f"Pixel ({ix}, {iy}) is not inside the {nx}×{ny} map"))
        return {"t": t, "ix": ix, "iy": iy, "values": _values(frame.get("values"))}, (ix, iy)
    # status
    text = frame.get("text")
    if not isinstance(text, str):
        raise LiveError(tr("status 帧的 text 必须是字符串", "text of a status frame must be a string"))
    level = frame.get("level") or "info"
    if level == "warning":
        level = "warn"
    if level not in STATUS_LEVELS:
        raise LiveError(tr(f"status 的 level 必须是 {sorted(STATUS_LEVELS)} 之一：{level!r}", f"level of status must be one of {sorted(STATUS_LEVELS)}: {level!r}"))
    return {"t": t, "text": text[:MAX_TEXT], "level": level}, None


# ── 存储 ──────────────────────────────────────────────────────────────────────

class _Frame:
    """一帧的存档：seq、序列化好的 JSON 文本、引用的轴版本（spectrum）、像素键（map2d）。"""

    __slots__ = ("seq", "text", "axis_ver", "key")

    def __init__(self, seq: int, text: str, axis_ver: int, key: tuple[int, int] | None) -> None:
        self.seq = seq
        self.text = text
        self.axis_ver = axis_ver
        self.key = key


class _Axis:
    __slots__ = ("ver", "unit", "x", "x_text")

    def __init__(self, ver: int, unit: str, x: tuple[float, ...]) -> None:
        self.ver = ver
        self.unit = unit
        self.x = x
        self.x_text = _dumps(list(x))

    def as_dict(self) -> dict:
        return {"ver": self.ver, "unit": self.unit, "n": len(self.x), "x": list(self.x)}

    def json_obj(self) -> str:
        return '{"ver":%d,"unit":%s,"n":%d,"x":%s}' % (self.ver, _dumps(self.unit),
                                                     len(self.x), self.x_text)


class _Stream:
    __slots__ = ("id", "kind", "title", "source", "x_label", "x_unit", "y_label", "y_unit",
                 "meta_text", "geometry", "retain", "uid", "created_t", "last_t", "ended_t",
                 "seq", "axis_ver", "axes", "frames", "chars", "evicted_upto")

    def __init__(self, sid: str, kind: str, geometry: tuple[int, int] | None, retain: int, *,
                 seq: int, axis_ver: int, now: float) -> None:
        self.id = sid
        self.kind = kind
        self.title = self.source = ""
        self.x_label = self.x_unit = self.y_label = self.y_unit = None
        self.meta_text = "{}"
        self.geometry = geometry
        self.retain = retain
        self.uid = secrets.token_hex(4)          # 每次（重）建流换一个：客户端据此清屏
        self.created_t = now
        self.last_t: float | None = None
        self.ended_t: float | None = None
        self.seq = seq
        self.axis_ver = axis_ver
        self.axes: OrderedDict[int, _Axis] = OrderedDict()
        self.frames: Any = OrderedDict() if kind == "map2d" else deque()
        self.chars = 0
        # 已经不在保留区里的最大 seq（容量淘汰；map2d 同像素被新值覆盖不算）。新建的流 = 之前的都不在了。
        self.evicted_upto = seq

    def newest_first(self) -> Iterator[_Frame]:
        return reversed(self.frames.values()) if self.kind == "map2d" else reversed(self.frames)

    def newest(self) -> _Frame | None:
        return next(self.newest_first(), None)

    def oldest(self) -> _Frame | None:
        it = self.frames.values() if self.kind == "map2d" else self.frames
        return next(iter(it), None)

    def pop_oldest(self) -> _Frame:
        if self.kind == "map2d":
            return self.frames.popitem(last=False)[1]
        return self.frames.popleft()

    def describe(self) -> dict:
        first = self.oldest()
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "source": self.source,
            "x_label": self.x_label, "x_unit": self.x_unit,
            "y_label": self.y_label, "y_unit": self.y_unit,
            "meta": json.loads(self.meta_text), "retain": self.retain, "uid": self.uid,
            "created_t": self.created_t, "last_t": self.last_t,
            "ended": self.ended_t is not None, "ended_t": self.ended_t,
            "last_seq": self.seq, "first_seq": first.seq if first is not None else None,
            "n_frames": len(self.frames), "axis_ver": self.axis_ver, "bytes": self.chars,
        }


def _select(st: _Stream, since: int | None,
            limit: int | None) -> tuple[list[_Frame], bool, tuple[int, int] | None]:
    """取 ``seq > since`` 的帧（按 seq 升序），外加「客户端缺了哪段」。

    * ``since`` 缺省：spectrum 只给最新一帧，其余给保留区里的全部（曲线 / 地图没有历史就画不出来）；
    * ``since == -1``：只给最新一帧（任何 kind）；
    * ``since >= 0``：给 ``seq > since`` 的；``limit`` 截断时 ``more=True``，客户端接着用最后一帧的
      seq 翻页。只有 ``since > 0``（客户端声称已经有到 since 为止的帧）时才报缺口 —— since=0 是
      「我什么都没有」，没赶上不叫丢。
    """
    if since is None:
        since = -1 if st.kind == "spectrum" else 0
    if since < 0:
        f = st.newest()
        return ([f] if f is not None else []), False, None
    out: list[_Frame] = []
    for f in st.newest_first():
        if f.seq <= since:
            break
        out.append(f)
    out.reverse()
    more = False
    if limit is not None and len(out) > limit:
        out, more = out[:limit], True
    gap = (since + 1, st.evicted_upto) if since > 0 and st.evicted_upto > since else None
    return out, more, gap


class LiveEvent:
    """发给订阅者的一条事件。``type`` ∈ declare / axis / frame / end / evict。

    ``frame`` 是清洗后的帧 dict（只读，别改它）；``reset`` 只在 declare 换了 kind / 地图尺寸时为真。
    """

    __slots__ = ("type", "stream", "kind", "uid", "seq", "frame", "axis_ver", "reset", "ref")

    def __init__(self, type: str, stream: str, kind: str, *, uid: str = "", seq: int = 0,
                 frame: dict | None = None, axis_ver: int = 0, reset: bool = False,
                 ref: _Frame | None = None) -> None:
        self.type = type
        self.stream = stream
        self.kind = kind
        self.uid = uid
        self.seq = seq
        self.frame = frame
        self.axis_ver = axis_ver
        self.reset = reset
        self.ref = ref

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"LiveEvent({self.type!r}, {self.stream!r}, seq={self.seq})"


class LiveSnapshot:
    """``snapshot()`` 的结果：一个流的描述、当前轴、帧引用到的旧轴、帧（seq, JSON 文本）、翻页与缺口。"""

    __slots__ = ("hub", "meta", "axis", "axes", "frames", "more", "dropped")

    def __init__(self, hub: str, meta: dict, axis: _Axis | None, axes: list[_Axis],
                 frames: list[tuple[int, str]], more: bool,
                 dropped: tuple[int, int] | None) -> None:
        self.hub = hub
        self.meta = meta
        self.axis = axis
        self.axes = axes
        self.frames = frames
        self.more = more
        self.dropped = dropped

    def to_json(self) -> str:
        """拼成 REST 回答的 JSON 文本 —— 帧本来就是序列化好的，不再解析一遍。"""
        frames = ",".join('{"seq":%d,"frame":%s}' % (s, t) for s, t in self.frames)
        dropped = (_dumps({"from": self.dropped[0], "to": self.dropped[1]})
                   if self.dropped else "null")
        return ('{"hub":%s,"meta":%s,"axis":%s,"axes":[%s],"frames":[%s],"more":%s,"dropped":%s}'
                % (_dumps(self.hub), _dumps(self.meta),
                   self.axis.json_obj() if self.axis is not None else "null",
                   ",".join(a.json_obj() for a in self.axes), frames,
                   "true" if self.more else "false", dropped))


# ── 枢纽 ──────────────────────────────────────────────────────────────────────

class LiveHub:
    """实时数据流的进程内枢纽。生产代码用 :func:`get_live_hub` 拿单例，别自己建。"""

    def __init__(self, *, strict: bool = False, max_streams: int = MAX_STREAMS,
                 max_bytes: int = MAX_BYTES, clock: Callable[[], float] = time.time) -> None:
        self.hub_id = secrets.token_hex(6)
        self._strict = bool(strict)
        self._max_streams = max(1, int(max_streams))
        self._max_chars = max(1, int(max_bytes))
        self._clock = clock
        # RLock：订阅者在持锁时被调用，万一它回头读 hub（describe 之类）也不会自锁死。
        self._lock = threading.RLock()
        self._streams: dict[str, _Stream] = {}
        self._tomb: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._chars = 0
        self._listeners: dict[int, Callable[[LiveEvent], None]] = {}
        self._next_handle = 1
        self._warned: set[str] = set()

    @property
    def strict(self) -> bool:
        return self._strict

    # —— 出错处理：严格模式抛，生产只记一次 ——
    def _refuse(self, exc: BaseException, key: str) -> None:
        if self._strict:
            raise exc
        if key in self._warned:
            return
        if len(self._warned) < 2000:
            self._warned.add(key)
        if isinstance(exc, LiveError):
            logger.warning("LiveHub: %s", exc)
        else:
            logger.warning("LiveHub: %s 意外失败：%s", key, exc, exc_info=exc)

    def _emit(self, ev: LiveEvent) -> None:
        """持锁调用。订阅者只许入队；它抛的异常吞掉 —— 一个坏订阅者不能让发布失败。"""
        for fn in tuple(self._listeners.values()):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001
                logger.debug("LiveHub listener failed", exc_info=True)

    # —— 订阅 ——
    def add_listener(self, fn: Callable[[LiveEvent], None]) -> int:
        """登记一个订阅者。``fn(event)`` 在发布线程上、持着 hub 的锁被同步调用：**只许入队**，
        不许做 I/O、不许等别的锁。返回句柄，交给 :meth:`remove_listener`。"""
        with self._lock:
            handle = self._next_handle
            self._next_handle += 1
            self._listeners[handle] = fn
            return handle

    def remove_listener(self, handle: int) -> None:
        with self._lock:
            self._listeners.pop(handle, None)

    # —— 写 ——
    def declare(self, stream_id: str, kind: str, *, title: str, source: str,
                x_label: str | None = None, x_unit: str | None = None,
                y_label: str | None = None, y_unit: str | None = None,
                meta: Mapping[str, Any] | None = None, retain: int | None = None) -> None:
        """声明（或重新声明）一个流。同 id 同 kind（map2d 还要同尺寸）：更新描述、重新打开，帧与 seq
        保留；换了 kind 或地图尺寸：原地重置（清掉旧帧与轴，seq 与轴版本接着编号）。"""
        try:
            sid = _check_id(stream_id)
            if kind not in KINDS:
                raise LiveError(tr(f"kind 必须是 {list(KINDS)} 之一：{kind!r}", f"kind must be one of {list(KINDS)}: {kind!r}"))
            fields = {
                "title": _text(title, "title", required=True),
                "source": _text(source, "source", required=True),
                "x_label": _text(x_label, "x_label"), "x_unit": _text(x_unit, "x_unit", limit=40),
                "y_label": _text(y_label, "y_label"), "y_unit": _text(y_unit, "y_unit", limit=40),
            }
            meta_d = _clean_meta(meta)
            geometry = _map_geometry(meta_d) if kind == "map2d" else None
            keep = _retain_for(kind, retain, geometry)
            meta_text = _dumps(meta_d)
        except Exception as exc:  # noqa: BLE001
            self._refuse(exc, f"declare:{stream_id}")
            return
        now = self._clock()
        with self._lock:
            st = self._streams.get(sid)
            reset = False
            if st is None:
                self._make_room()
                seq0, ver0 = self._tomb.pop(sid, (0, 0))
                st = _Stream(sid, kind, geometry, keep, seq=seq0, axis_ver=ver0, now=now)
                self._streams[sid] = st
                changed = True
            else:
                if st.kind != kind or st.geometry != geometry:
                    self._chars -= st.chars
                    st.chars = 0
                    st.kind, st.geometry = kind, geometry
                    st.frames = OrderedDict() if kind == "map2d" else deque()
                    st.axes.clear()
                    st.uid = secrets.token_hex(4)
                    st.evicted_upto = st.seq
                    st.created_t = now
                    reset = True
                before = (st.title, st.source, st.x_label, st.x_unit, st.y_label, st.y_unit,
                          st.meta_text, st.retain, st.ended_t)
                changed = reset or before != (*fields.values(), meta_text, keep, None)
            st.retain = keep
            for name, value in fields.items():
                setattr(st, name, value)
            st.meta_text = meta_text
            st.ended_t = None
            self._trim(st)
            if changed:
                self._emit(LiveEvent("declare", sid, kind, uid=st.uid, reset=reset))

    def set_axis(self, stream_id: str, x: Iterable[float], *, unit: str) -> int:
        """设 spectrum 流的共享横轴，返回轴版本（从 1 起）。和当前那版一模一样就不升版本。"""
        try:
            xs = tuple(_num_array(x, "x", finite=True))
            u = _text(unit, "unit", limit=40, required=True)
            with self._lock:
                st = self._streams.get(stream_id) if isinstance(stream_id, str) else None
                if st is None:
                    raise LiveError(tr(f"流 {stream_id!r} 还没 declare", f"Stream {stream_id!r} has not been declared yet"))
                if st.kind != "spectrum":
                    raise LiveError(tr(f"只有 spectrum 流有共享横轴（{stream_id} 是 {st.kind}）", f"Only spectrum streams have a shared x axis ({stream_id} is {st.kind})"))
                cur = st.axes.get(st.axis_ver)
                if cur is not None and cur.unit == u and cur.x == xs:
                    return st.axis_ver
                ver = st.axis_ver + 1
                st.axis_ver = ver
                st.axes[ver] = _Axis(ver, u, xs)
                while len(st.axes) > AXIS_HISTORY:
                    st.axes.popitem(last=False)
                self._emit(LiveEvent("axis", stream_id, st.kind, uid=st.uid, axis_ver=ver))
                return ver
        except Exception as exc:  # noqa: BLE001
            self._refuse(exc, f"set_axis:{stream_id}")
        return -1

    def publish(self, stream_id: str, frame: Mapping[str, Any]) -> int:
        """发一帧，返回它的 seq（失败返回 -1）。任何线程可调；不阻塞，不抛（严格模式除外）。"""
        try:
            return self._publish(stream_id, frame)
        except Exception as exc:  # noqa: BLE001 — 显示上的问题不许冒到采集线程里
            self._refuse(exc, f"publish:{stream_id}")
        return -1

    def _publish(self, stream_id: str, frame: Mapping[str, Any]) -> int:
        now = self._clock()
        with self._lock:
            st = self._streams.get(stream_id) if isinstance(stream_id, str) else None
            if st is None:
                raise LiveError(tr(f"流 {stream_id!r} 还没 declare", f"Stream {stream_id!r} has not been declared yet"))
            if st.ended_t is not None:
                raise LiveError(tr(f"流 {stream_id!r} 已经 end；要接着发，先重新 declare", f"Stream {stream_id!r} has already ended; to keep sending, declare it again first"))
            kind, uid, geometry, cur_ver = st.kind, st.uid, st.geometry, st.axis_ver
        body, key = _build_frame(kind, frame, geometry, cur_ver, now)
        text = _dumps(body)
        if len(text) > MAX_FRAME_CHARS:
            raise LiveError(tr(f"一帧 {len(text)} 字符，超过上限 {MAX_FRAME_CHARS}", f"A frame of {len(text)} characters exceeds the limit {MAX_FRAME_CHARS}"))
        with self._lock:
            st = self._streams.get(stream_id)
            if st is None or st.uid != uid or st.ended_t is not None:
                raise LiveError(tr(f"流 {stream_id!r} 在这一帧发布途中被重置、淘汰或结束", f"Stream {stream_id!r} was reset, evicted or ended while this frame was being published"))
            ver = 0
            if kind == "spectrum":
                ver = body["axis_ver"]
                if ver > st.axis_ver:
                    raise LiveError(tr(f"axis_ver={ver} 还没 set_axis（{stream_id} 当前是 {st.axis_ver}）", f"axis_ver={ver} has not been set with set_axis yet ({stream_id} is currently {st.axis_ver})"))
                ax = st.axes.get(ver)
                if ax is not None and len(ax.x) != len(body["y"]):
                    raise LiveError(tr(f"y 有 {len(body['y'])} 个点，轴 v{ver} 有 {len(ax.x)} 个", f"y has {len(body['y'])} points but axis v{ver} has {len(ax.x)}"))
            seq = st.seq + 1
            st.seq = seq
            st.last_t = now
            f = _Frame(seq, text, ver, key)
            self._store(st, f)
            self._emit(LiveEvent("frame", stream_id, kind, uid=uid, seq=seq, frame=body,
                                 axis_ver=ver, ref=f))
            self._enforce_budget()
        return seq

    def end(self, stream_id: str) -> None:
        """标记结束。结束的流仍可列出与回看；要接着发先重新 declare。"""
        try:
            with self._lock:
                st = self._streams.get(stream_id) if isinstance(stream_id, str) else None
                if st is None:
                    raise LiveError(tr(f"流 {stream_id!r} 还没 declare", f"Stream {stream_id!r} has not been declared yet"))
                if st.ended_t is None:
                    st.ended_t = self._clock()
                    self._emit(LiveEvent("end", stream_id, st.kind, uid=st.uid))
        except Exception as exc:  # noqa: BLE001
            self._refuse(exc, f"end:{stream_id}")

    # —— 保留与淘汰（持锁）——
    def _store(self, st: _Stream, f: _Frame) -> None:
        if st.kind == "map2d":
            old = st.frames.pop(f.key, None)
            if old is not None:          # 同一像素的新值顶掉旧值：不算丢
                st.chars -= len(old.text)
                self._chars -= len(old.text)
            st.frames[f.key] = f
        else:
            st.frames.append(f)
        st.chars += len(f.text)
        self._chars += len(f.text)
        self._trim(st)

    def _trim(self, st: _Stream, keep: int | None = None) -> None:
        limit = st.retain if keep is None else keep
        while len(st.frames) > limit:
            f = st.pop_oldest()
            st.chars -= len(f.text)
            self._chars -= len(f.text)
            st.evicted_upto = max(st.evicted_upto, f.seq)

    def _evict(self, st: _Stream, why: str) -> None:
        del self._streams[st.id]
        self._chars -= st.chars
        self._tomb[st.id] = (st.seq, st.axis_ver)
        self._tomb.move_to_end(st.id)
        while len(self._tomb) > _TOMBSTONES:
            self._tomb.popitem(last=False)
        logger.info("LiveHub: 淘汰流 %s（%s）", st.id, why)
        self._emit(LiveEvent("evict", st.id, st.kind, uid=st.uid))

    def _make_room(self) -> None:
        while len(self._streams) >= self._max_streams:
            ended = [s for s in self._streams.values() if s.ended_t is not None]
            if ended:
                victim = min(ended, key=lambda s: s.ended_t)
            else:
                victim = min(self._streams.values(), key=lambda s: s.last_t or s.created_t)
            self._evict(victim, f"流的总数到了上限 {self._max_streams}")

    def _enforce_budget(self) -> None:
        if self._chars <= self._max_chars:
            return
        for st in sorted((s for s in self._streams.values() if s.ended_t is not None),
                         key=lambda s: s.ended_t):
            if self._chars <= self._max_chars:
                return
            self._evict(st, "实时数据总内存到了上限")
        while self._chars > self._max_chars:
            big = max(self._streams.values(), key=lambda s: s.chars, default=None)
            if big is None or len(big.frames) <= 1:
                return
            self._trim(big, keep=len(big.frames) - 1)

    # —— 读 ——
    def streams(self) -> list[dict]:
        """所有流的描述（含 last_seq / first_seq / ended），按声明先后。"""
        with self._lock:
            return [st.describe() for st in self._streams.values()]

    def describe(self, stream_id: str) -> dict | None:
        with self._lock:
            st = self._streams.get(stream_id)
            return st.describe() if st is not None else None

    def streams_message(self) -> str:
        """``/ws/live`` 的目录消息（编好的 JSON 文本）。"""
        return '{"kind":"streams","hub":%s,"items":%s}' % (_dumps(self.hub_id),
                                                         _dumps(self.streams()))

    def latest(self, stream_id: str) -> dict | None:
        """最新一帧 ``{"seq", "frame"}``；没有就 None。"""
        with self._lock:
            st = self._streams.get(stream_id)
            f = st.newest() if st is not None else None
        return {"seq": f.seq, "frame": json.loads(f.text)} if f is not None else None

    def frames(self, stream_id: str, *, since: int | None = None,
               limit: int | None = None) -> list[dict]:
        """保留区里的帧 ``[{"seq", "frame"}]``（since 的语义同 :func:`_select`）。"""
        snap = self.snapshot(stream_id, since=since, limit=limit)
        if snap is None:
            return []
        return [{"seq": s, "frame": json.loads(t)} for s, t in snap.frames]

    def axis(self, stream_id: str, ver: int | None = None) -> dict | None:
        """某一版横轴（缺省 = 当前版）``{"ver", "unit", "n", "x"}``；没有就 None。"""
        with self._lock:
            st = self._streams.get(stream_id)
            if st is None:
                return None
            ax = st.axes.get(st.axis_ver if ver is None else ver)
            return ax.as_dict() if ax is not None else None

    def snapshot(self, stream_id: str, *, since: int | None = None,
                 limit: int | None = None) -> LiveSnapshot | None:
        with self._lock:
            st = self._streams.get(stream_id)
            if st is None:
                return None
            frames, more, gap = _select(st, since, limit)
            cur = st.axes.get(st.axis_ver)
            used = sorted({f.axis_ver for f in frames if f.axis_ver and f.axis_ver != st.axis_ver})
            older = [st.axes[v] for v in used if v in st.axes]
            meta = st.describe()
        return LiveSnapshot(self.hub_id, meta, cur, older,
                            [(f.seq, f.text) for f in frames], more, gap)

    # —— 给 LiveSubscription 用（持锁调用）——
    def _replay_locked(self, stream_id: str, since: int | None
                       ) -> tuple[list[_Frame], tuple[int, int] | None, list[_Axis]] | None:
        st = self._streams.get(stream_id)
        if st is None:
            return None
        frames, _more, gap = _select(st, since, None)
        vers = {f.axis_ver for f in frames if f.axis_ver}
        if st.axis_ver:
            vers.add(st.axis_ver)
        return frames, gap, [st.axes[v] for v in sorted(vers) if v in st.axes]

    def _axes(self, stream_id: str, vers: Iterable[int]) -> list[_Axis]:
        with self._lock:
            st = self._streams.get(stream_id)
            if st is None:
                return []
            return [st.axes[v] for v in sorted(set(vers)) if v in st.axes]

    def _current_axis_ver(self, stream_id: str) -> int:
        with self._lock:
            st = self._streams.get(stream_id)
            return st.axis_ver if st is not None and st.axis_ver in st.axes else 0


# ── 进程级单例 ────────────────────────────────────────────────────────────────

_HUB: LiveHub | None = None
_HUB_LOCK = threading.Lock()


def get_live_hub() -> LiveHub:
    """进程级的 LiveHub。每次用的时候调一下，别在 import 时缓存 —— 测试会换掉它。"""
    global _HUB
    hub = _HUB
    if hub is None:
        with _HUB_LOCK:
            if _HUB is None:
                _HUB = LiveHub()
            hub = _HUB
    return hub


def _install(hub: LiveHub | None) -> LiveHub | None:
    """换掉进程级单例，返回原来的（测试用：``mast.live.testing.isolated_live_hub``）。"""
    global _HUB
    with _HUB_LOCK:
        prev, _HUB = _HUB, hub
    return prev


__all__ = [
    "DEFAULT_RETAIN", "KINDS", "LATEST_ONLY_KINDS", "LiveError", "LiveEvent", "LiveHub",
    "LiveSnapshot", "MAX_RETAIN", "MAX_STREAMS", "get_live_hub",
]
