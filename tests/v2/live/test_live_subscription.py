"""一条 /ws/live 连接的订阅：限速、合批、有界、重放与实时的衔接（契约 §7）。

这里直接驱动 ``LiveSubscription``（不经 WS），因为要钉的几件事在 TestClient 上看不见 ——
它的发送队列是无界的，服务端永远不会被一个慢客户端顶住：

* 一个从不取走的订阅者拖不住发布线程，积压有上限，丢了什么会说出来；
* spectrum / scalar 每拍只推最新一帧；xy / map2d / status 一帧不丢；
* 订阅时的重放和之后的实时帧不重、不漏、不乱序；
* 横轴先于引用它的帧到达，且每版只发一次。
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from mast.live import LiveHub, LiveSubscription  # noqa: E402
from mast.live.subscription import frame_messages  # noqa: E402

MAP = dict(nx=8, ny=8, x0=0.0, y0=0.0, dx=1.0, dy=1.0, x_unit="nm", y_unit="nm")


def _hub() -> LiveHub:
    return LiveHub(strict=True)


def _msgs(sub: LiveSubscription) -> list[dict]:
    return [json.loads(m) for m in sub.drain()]


def _frames_of(msgs: list[dict], sid: str) -> list[int]:
    """把 frame / frames 摊平成 seq 列表（按到达顺序）。"""
    out: list[int] = []
    for m in msgs:
        if m.get("stream") != sid:
            continue
        if m["kind"] == "frame":
            out.append(m["seq"])
        elif m["kind"] == "frames":
            out += [it["seq"] for it in m["items"]]
    return out


def _kinds(msgs: list[dict], sid: str | None = None) -> list[str]:
    return [m["kind"] for m in msgs if sid is None or m.get("stream") == sid]


def _sub(h: LiveHub, streams=("*",), since=None, **kw) -> LiveSubscription:
    s = LiveSubscription(h, **kw)
    s.request("subscribe", list(streams), since)
    return s


# ════════════════════════════════════════════════════════════════════════════
# 1 · 目录
# ════════════════════════════════════════════════════════════════════════════

def test_the_first_drain_is_the_full_directory_with_the_hub_id():
    h = _hub()
    h.declare("s/pp/r", "xy", title="pp", source="s")
    s = LiveSubscription(h)
    first = _msgs(s)
    assert first[0]["kind"] == "streams" and first[0]["hub"] == h.hub_id
    assert [i["id"] for i in first[0]["items"]] == ["s/pp/r"]
    assert _msgs(s) == [], "没有变化就不该再发目录"


def test_declare_end_and_evict_each_resend_the_whole_directory():
    h = LiveHub(strict=True, max_streams=2)
    h.declare("a/x/1", "xy", title="a", source="t")
    s = LiveSubscription(h)
    _msgs(s)
    h.declare("b/x/1", "xy", title="b", source="t")
    assert [i["id"] for i in _msgs(s)[0]["items"]] == ["a/x/1", "b/x/1"]
    h.end("a/x/1")
    items = _msgs(s)[0]["items"]
    assert [i["ended"] for i in items] == [True, False]
    h.declare("c/x/1", "xy", title="c", source="t")      # 淘汰已结束的 a
    assert [i["id"] for i in _msgs(s)[0]["items"]] == ["b/x/1", "c/x/1"]


# ════════════════════════════════════════════════════════════════════════════
# 2 · 推送语义：最新一帧 vs 合批不丢
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("kind, frame", [
    ("spectrum", {"y": [1, 2, 3]}),
    ("scalar", {"values": {"deg": 1.0}}),
])
def test_latest_only_kinds_coalesce_to_one_frame_per_tick(kind, frame):
    h = _hub()
    h.declare("s/q/1", kind, title="q", source="s")
    s = _sub(h)
    _msgs(s)
    for _ in range(10):
        h.publish("s/q/1", frame)
    msgs = _msgs(s)
    assert _frames_of(msgs, "s/q/1") == [10] and "dropped" not in _kinds(msgs), (
        "被节拍覆盖掉的中间帧不算丢，不该发 dropped")


@pytest.mark.parametrize("kind, make", [
    ("xy", lambda i: {"x": i, "values": {"v": i}}),
    ("map2d", lambda i: {"ix": i % 8, "iy": i // 8, "values": {"I": i}}),
    ("status", lambda i: {"text": f"第 {i} 步"}),
])
def test_incremental_kinds_are_batched_never_dropped(kind, make):
    h = _hub()
    h.declare("s/q/1", kind, title="q", source="s", meta=MAP if kind == "map2d" else None)
    s = _sub(h)
    _msgs(s)
    for i in range(30):
        h.publish("s/q/1", make(i))
    msgs = _msgs(s)
    assert _frames_of(msgs, "s/q/1") == list(range(1, 31))
    assert _kinds(msgs, "s/q/1") == ["frames"], "30 帧应合成一批"


def test_batches_split_by_item_count_and_size():
    h = _hub()
    h.declare("s/pp/r", "xy", title="pp", source="s")
    for i in range(7):
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})
    frames = list(h._streams["s/pp/r"].frames)
    by_count = frame_messages('"s/pp/r"', frames, batch_items=3)
    assert [len(json.loads(m)["items"]) for m in by_count] == [3, 3, 1]
    by_size = frame_messages('"s/pp/r"', frames, batch_chars=len(frames[0].text) * 2 + 40)
    assert sum(len(json.loads(m)["items"]) for m in by_size) == 7 and len(by_size) > 1


# ════════════════════════════════════════════════════════════════════════════
# 3 · 慢客户端：有界、说出来、拖不住发布方
# ════════════════════════════════════════════════════════════════════════════

def test_an_undrained_subscription_never_slows_the_publisher_and_stays_bounded():
    h = _hub()
    h.declare("s/pp/r", "xy", title="pp", source="s", retain=1000)
    s = _sub(h, max_pending=500)
    _msgs(s)
    t0 = time.perf_counter()
    for i in range(20000):
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})
    dt = time.perf_counter() - t0
    assert s.pending == 500, "积压没有上限"
    assert dt / 20000 < 2e-3, f"每帧 {dt / 20000 * 1e6:.0f} µs —— 发布被订阅者拖慢了"
    msgs = _msgs(s)
    assert msgs[0] == {"kind": "dropped", "stream": "s/pp/r", "from": 1, "to": 19500}
    assert _frames_of(msgs, "s/pp/r") == list(range(19501, 20001))


def test_overflow_is_charged_to_the_stream_with_the_biggest_backlog():
    h = _hub()
    h.declare("big/x/1", "xy", title="big", source="t")
    h.declare("small/x/1", "xy", title="small", source="t")
    s = _sub(h, max_pending=100)
    _msgs(s)
    for i in range(5):
        h.publish("small/x/1", {"x": i, "values": {"v": i}})
    for i in range(150):
        h.publish("big/x/1", {"x": i, "values": {"v": i}})
    msgs = _msgs(s)
    assert _frames_of(msgs, "small/x/1") == [1, 2, 3, 4, 5], "小流替大流挨了丢"
    assert [m for m in msgs if m["kind"] == "dropped"] == [
        {"kind": "dropped", "stream": "big/x/1", "from": 1, "to": 55}]
    assert _frames_of(msgs, "big/x/1") == list(range(56, 151))


def test_wake_fires_once_per_tick_not_once_per_frame():
    h = _hub()
    h.declare("s/pp/r", "xy", title="pp", source="s")
    calls = []
    s = _sub(h, wake=lambda: calls.append(1))
    _msgs(s)
    calls.clear()
    for i in range(100):
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})
    assert len(calls) == 1
    _msgs(s)
    h.publish("s/pp/r", {"x": 0, "values": {"v": 0}})
    assert len(calls) == 2


def test_a_wake_that_raises_does_not_fail_the_publish():
    h = _hub()
    h.declare("s/pp/r", "xy", title="pp", source="s")

    def boom():
        raise RuntimeError("loop closed")
    _sub(h, wake=boom)
    assert h.publish("s/pp/r", {"x": 0, "values": {"v": 0}}) == 1


# ════════════════════════════════════════════════════════════════════════════
# 4 · 重放与实时的衔接
# ════════════════════════════════════════════════════════════════════════════

def test_subscribe_replays_then_continues_live_without_duplicates_or_gaps():
    h = _hub()
    h.declare("s/pp/r", "xy", title="pp", source="s")
    for i in range(5):
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})
    s = _sub(h, ["s/pp/r"], {"s/pp/r": 2})
    assert _frames_of(_msgs(s), "s/pp/r") == [3, 4, 5]
    for i in range(3):
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})
    assert _frames_of(_msgs(s), "s/pp/r") == [6, 7, 8]


def test_resubscribing_while_frames_are_pending_does_not_duplicate_them():
    h = _hub()
    h.declare("s/pp/r", "xy", title="pp", source="s")
    s = _sub(h, ["s/pp/r"])
    _msgs(s)
    for i in range(4):
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})        # 积压 1..4
    s.request("subscribe", ["s/pp/r"], {"s/pp/r": 2})             # 再订一次，从 2 之后
    for i in range(2):
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})        # 5, 6 —— 订阅请求处理之前
    got = _frames_of(_msgs(s), "s/pp/r")
    assert got == [3, 4, 5, 6], got


def test_a_retention_gap_on_replay_is_reported_before_the_frames():
    h = _hub()
    h.declare("s/pp/r", "xy", title="pp", source="s", retain=5)
    for i in range(12):
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})
    msgs = _msgs(_sub(h, ["s/pp/r"], {"s/pp/r": 3}))
    stream_msgs = [m for m in msgs if m.get("stream") == "s/pp/r"]
    assert stream_msgs[0] == {"kind": "dropped", "stream": "s/pp/r", "from": 4, "to": 7}
    assert _frames_of(msgs, "s/pp/r") == [8, 9, 10, 11, 12]


def test_since_defaults_by_kind_minus_one_and_star_key():
    h = _hub()
    h.declare("s/ccd/spec", "spectrum", title="c", source="s")
    h.declare("s/pp/r", "xy", title="p", source="s")
    for i in range(4):
        h.publish("s/ccd/spec", {"y": [i]})
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})
    m = _msgs(_sub(h))
    assert _frames_of(m, "s/ccd/spec") == [4] and _frames_of(m, "s/pp/r") == [1, 2, 3, 4]
    m = _msgs(_sub(h, since={"*": -1}))
    assert _frames_of(m, "s/ccd/spec") == [4] and _frames_of(m, "s/pp/r") == [4]
    m = _msgs(_sub(h, since={"*": -1, "s/ccd/spec": 0}))
    assert _frames_of(m, "s/ccd/spec") == [1, 2, 3, 4] and _frames_of(m, "s/pp/r") == [4]


# ════════════════════════════════════════════════════════════════════════════
# 5 · 横轴
# ════════════════════════════════════════════════════════════════════════════

def test_the_axis_precedes_the_frames_and_is_sent_once():
    h = _hub()
    h.declare("s/ccd/spec", "spectrum", title="c", source="s")
    v1 = h.set_axis("s/ccd/spec", [1.0, 2.0], unit="nm")
    s = _sub(h)
    msgs = _msgs(s)
    assert _kinds(msgs, "s/ccd/spec") == ["axis"] and msgs[-1]["ver"] == v1
    h.publish("s/ccd/spec", {"y": [5, 6]})
    assert _kinds(_msgs(s), "s/ccd/spec") == ["frame"], "同一版轴发了两次"
    v2 = h.set_axis("s/ccd/spec", [1.0, 2.0, 3.0], unit="nm")
    h.publish("s/ccd/spec", {"y": [5, 6, 7]})
    msgs = _msgs(s)
    assert _kinds(msgs, "s/ccd/spec") == ["axis", "frame"]
    assert msgs[-2]["ver"] == v2 and msgs[-2]["x"] == [1.0, 2.0, 3.0]
    assert msgs[-1]["frame"]["axis_ver"] == v2


def test_a_replayed_frame_brings_the_older_axis_it_was_measured_on():
    h = _hub()
    h.declare("s/ccd/spec", "spectrum", title="c", source="s")
    v1 = h.set_axis("s/ccd/spec", [1.0, 2.0], unit="nm")
    h.publish("s/ccd/spec", {"y": [1, 2]})
    v2 = h.set_axis("s/ccd/spec", [1.0, 2.0, 3.0], unit="nm")
    h.publish("s/ccd/spec", {"y": [1, 2, 3]})
    msgs = _msgs(_sub(h, since={"s/ccd/spec": 0}))
    stream = [m for m in msgs if m.get("stream") == "s/ccd/spec"]
    assert [m["kind"] for m in stream] == ["axis", "axis", "frames"]
    assert [m["ver"] for m in stream[:2]] == [v1, v2]


# ════════════════════════════════════════════════════════════════════════════
# 6 · 订阅范围
# ════════════════════════════════════════════════════════════════════════════

def test_wildcard_picks_up_new_streams_and_unsubscribe_stops_them():
    h = _hub()
    s = _sub(h)
    _msgs(s)
    h.declare("s/new/1", "xy", title="n", source="s")
    h.publish("s/new/1", {"x": 0, "values": {"v": 0}})
    msgs = _msgs(s)
    assert _kinds(msgs) == ["streams", "frame"], "目录必须先于新流的第一帧"
    s.request("unsubscribe", ["s/new/1"])
    _msgs(s)
    h.publish("s/new/1", {"x": 1, "values": {"v": 1}})
    h.declare("s/other/1", "xy", title="o", source="s")
    h.publish("s/other/1", {"x": 1, "values": {"v": 1}})
    msgs = _msgs(s)
    assert _frames_of(msgs, "s/new/1") == [] and _frames_of(msgs, "s/other/1") == [1]
    s.request("unsubscribe", ["*"])
    _msgs(s)
    h.publish("s/other/1", {"x": 2, "values": {"v": 2}})
    assert _msgs(s) == []


def test_an_explicit_subscription_can_wait_for_a_stream_not_yet_declared():
    h = _hub()
    s = _sub(h, ["rig2/ters_map/run7"])
    _msgs(s)
    h.declare("rig2/ters_map/run7", "map2d", title="m", source="rig2", meta=MAP)
    h.declare("rig2/other/run7", "xy", title="o", source="rig2")
    h.publish("rig2/ters_map/run7", {"ix": 0, "iy": 0, "values": {"I": 1}})
    h.publish("rig2/other/run7", {"x": 0, "values": {"v": 0}})
    msgs = _msgs(s)
    assert _frames_of(msgs, "rig2/ters_map/run7") == [1]
    assert _frames_of(msgs, "rig2/other/run7") == []


def test_a_kind_reset_throws_away_the_old_backlog():
    h = _hub()
    h.declare("s/q/1", "xy", title="q", source="s")
    s = _sub(h)
    _msgs(s)
    for i in range(3):
        h.publish("s/q/1", {"x": i, "values": {"v": i}})
    h.declare("s/q/1", "scalar", title="q", source="s")
    h.publish("s/q/1", {"values": {"v": 9}})
    msgs = _msgs(s)
    assert _frames_of(msgs, "s/q/1") == [4], "重置前的旧帧不该再推出去"
    assert msgs[0]["kind"] == "streams" and msgs[0]["items"][0]["kind"] == "scalar"


# ════════════════════════════════════════════════════════════════════════════
# 7 · 客户端消息、关闭、并发
# ════════════════════════════════════════════════════════════════════════════

_BAD_OPS = {
    "not-json": "not json",
    "not-object": "[1, 2]",
    "unknown-op": '{"op": "bogus", "streams": ["*"]}',
    "streams-not-list": '{"op": "subscribe", "streams": "*"}',
    "streams-empty": '{"op": "subscribe", "streams": []}',
    "streams-not-str": '{"op": "subscribe", "streams": [1]}',
    "since-not-dict": '{"op": "subscribe", "streams": ["*"], "since": [1]}',
    "since-below-minus-one": '{"op": "subscribe", "streams": ["*"], "since": {"a": -2}}',
    "since-bool": '{"op": "subscribe", "streams": ["*"], "since": {"a": true}}',
    "too-long": "x" * 70000,
}


@pytest.mark.parametrize("text", list(_BAD_OPS.values()), ids=list(_BAD_OPS))
def test_a_bad_client_message_gets_an_error_and_the_subscription_survives(text):
    h = _hub()
    h.declare("s/pp/r", "xy", title="p", source="s")
    s = LiveSubscription(h)
    _msgs(s)
    s.request_text(text)
    msgs = _msgs(s)
    assert [m["kind"] for m in msgs] == ["error"] and msgs[0]["message"]
    s.request_text('{"op": "subscribe", "streams": ["*"]}')
    h.publish("s/pp/r", {"x": 0, "values": {"v": 0}})
    assert _frames_of(_msgs(s), "s/pp/r") == [1]


def test_close_detaches_from_the_hub():
    h = _hub()
    h.declare("s/pp/r", "xy", title="p", source="s")
    before = len(h._listeners)
    s = _sub(h)
    assert len(h._listeners) == before + 1
    s.close()
    s.close()                                            # 幂等
    assert len(h._listeners) == before
    h.publish("s/pp/r", {"x": 0, "values": {"v": 0}})
    assert s.drain() == [] and s.pending == 0


def test_concurrent_publish_and_drain_lose_nothing_and_keep_order():
    h = _hub()
    h.declare("s/pp/r", "xy", title="p", source="s", retain=20000)
    s = _sub(h, ["s/pp/r"], max_pending=50000)
    _msgs(s)
    n = 6000
    done = threading.Event()

    def pub():
        for i in range(n):
            h.publish("s/pp/r", {"x": i, "values": {"v": i}})
        done.set()
    th = threading.Thread(target=pub)
    th.start()
    got: list[int] = []
    while not done.is_set() or s.pending:
        got += _frames_of(_msgs(s), "s/pp/r")
    th.join()
    got += _frames_of(_msgs(s), "s/pp/r")
    assert got == list(range(1, n + 1))
