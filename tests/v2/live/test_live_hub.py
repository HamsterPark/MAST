"""LiveHub 发布方的契约（docs/v2/design/ters_satellite_integration.md §7）。

设计要求：所有新增的数据采集都要在 MAST 页面上实时绘图。发布方是卫星枢纽、技能、驱动 ——
它们在采集线程上调 ``publish``，所以这里钉住的头一条是：**publish 不阻塞、不抛**，显示上的毛病
不能把一次真实的采集打断。其余是形状、保留上限、淘汰次序和 seq 的连续性。

从仓库根运行::

    .venv-v2-py313/Scripts/python.exe -m pytest tests/v2/live -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

import datetime as _dt  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import threading  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from mast.live import LiveError, LiveHub, get_live_hub  # noqa: E402
from mast.live import hub as hub_mod  # noqa: E402
from mast.live.testing import LiveRecorder, isolated_live_hub  # noqa: E402

MAP = dict(nx=4, ny=3, x0=0.0, y0=0.0, dx=1.0, dy=1.0, x_unit="nm", y_unit="nm")
NOW = 1_792_400_000.0


def _strict(**kw) -> LiveHub:
    return LiveHub(strict=True, clock=lambda: NOW, **kw)


def _no_nan(text: str):
    def boom(c):
        raise AssertionError(f"JSON 里出现了 {c} —— 浏览器的 JSON.parse 会整条消息报错")
    return json.loads(text, parse_constant=boom)


# ════════════════════════════════════════════════════════════════════════════
# 1 · 五种 kind 的帧形状
# ════════════════════════════════════════════════════════════════════════════

def test_each_kind_keeps_exactly_its_contract_fields():
    h = _strict()
    h.declare("s/ccd/spec", "spectrum", title="CCD", source="s")
    h.declare("s/pol/angle", "scalar", title="偏振角", source="s")
    h.declare("s/pp/run1", "xy", title="泵浦-探测", source="s")
    h.declare("s/map/run1", "map2d", title="TERS 图", source="s", meta=MAP)
    h.declare("s/log/run1", "status", title="进度", source="s")
    h.publish("s/ccd/spec", {"y": [1, 2, 3], "meta": {"exposure_ms": 100}, "junk": 1})
    h.publish("s/pol/angle", {"t": NOW - 1, "values": {"deg": 12.5}})
    h.publish("s/pp/run1", {"x": 1.25, "values": {"lockin_x": 3e-6, "lockin_y": -1e-7}})
    h.publish("s/map/run1", {"ix": 3, "iy": 2, "values": {"I": 0.5}})
    h.publish("s/log/run1", {"text": "第 3/16 点", "level": "warning"})
    assert h.latest("s/ccd/spec")["frame"] == {"t": NOW, "y": [1.0, 2.0, 3.0], "axis_ver": 0,
                                                "meta": {"exposure_ms": 100}}
    assert h.latest("s/pol/angle")["frame"] == {"t": NOW - 1, "values": {"deg": 12.5}}
    assert h.latest("s/pp/run1")["frame"] == {"t": NOW, "x": 1.25,
                                               "values": {"lockin_x": 3e-6, "lockin_y": -1e-7}}
    assert h.latest("s/map/run1")["frame"] == {"t": NOW, "ix": 3, "iy": 2, "values": {"I": 0.5}}
    assert h.latest("s/log/run1")["frame"] == {"t": NOW, "text": "第 3/16 点", "level": "warn"}


def test_numpy_inputs_are_accepted_and_non_finite_values_become_null():
    h = _strict()
    h.declare("s/ccd/spec", "spectrum", title="CCD", source="s")
    h.declare("s/map/r", "map2d", title="m", source="s", meta=MAP)
    h.publish("s/ccd/spec", {"y": np.array([1, 2, 65535], dtype=np.uint16)})
    h.publish("s/ccd/spec", {"y": np.array([1.0, np.nan, np.inf])})
    h.publish("s/map/r", {"ix": np.int64(1), "iy": 2.0,
                          "values": {"I": np.float32(0.25), "bad": float("nan")}})
    assert h.frames("s/ccd/spec", since=0)[0]["frame"]["y"] == [1.0, 2.0, 65535.0]
    assert h.latest("s/ccd/spec")["frame"]["y"] == [1.0, None, None]
    assert h.latest("s/map/r")["frame"]["values"] == {"I": 0.25, "bad": None}
    for sid in ("s/ccd/spec", "s/map/r"):
        _no_nan(h.snapshot(sid, since=0).to_json())


def test_seq_is_per_stream_contiguous_and_is_what_publish_returns():
    h = _strict()
    h.declare("a/x/1", "xy", title="a", source="t")
    h.declare("b/x/1", "xy", title="b", source="t")
    got = {"a/x/1": [], "b/x/1": []}
    for i in range(5):
        got["a/x/1"].append(h.publish("a/x/1", {"x": i, "values": {"v": i}}))
        if i < 3:
            got["b/x/1"].append(h.publish("b/x/1", {"x": i, "values": {"v": i}}))
    assert got == {"a/x/1": [1, 2, 3, 4, 5], "b/x/1": [1, 2, 3]}
    assert h.describe("a/x/1")["last_seq"] == 5 and h.describe("b/x/1")["first_seq"] == 1


# ════════════════════════════════════════════════════════════════════════════
# 2 · 不阻塞、不抛：生产单例只记日志；严格模式（测试）直接抛
# ════════════════════════════════════════════════════════════════════════════

_MISUSES = [
    ("undeclared", lambda h: h.publish("nope/x/y", {"x": 1, "values": {"v": 1}})),
    ("not a dict", lambda h: h.publish("a/x/1", None)),
    ("string value", lambda h: h.publish("a/x/1", {"x": 1, "values": {"v": "1.5"}})),
    ("no values", lambda h: h.publish("a/x/1", {"x": 1, "values": {}})),
    ("x missing", lambda h: h.publish("a/x/1", {"values": {"v": 1}})),
    ("ms timestamp", lambda h: h.publish("a/x/1", {"t": 1_792_400_000_000, "x": 1,
                                                   "values": {"v": 1}})),
    ("y not array", lambda h: h.publish("a/ccd/spec", {"y": "1,2,3"})),
    ("y 2-D", lambda h: h.publish("a/ccd/spec", {"y": [[1, 2], [3, 4]]})),
    ("future axis", lambda h: h.publish("a/ccd/spec", {"y": [1, 2, 3], "axis_ver": 9})),
    ("pixel outside", lambda h: h.publish("a/map/r", {"ix": 4, "iy": 0, "values": {"I": 1}})),
    ("bad level", lambda h: h.publish("a/log/r", {"text": "x", "level": "fatal"})),
    ("axis on xy", lambda h: h.set_axis("a/x/1", [1, 2], unit="nm")),
    ("axis with nan", lambda h: h.set_axis("a/ccd/spec", [1, float("nan")], unit="nm")),
    ("bad kind", lambda h: h.declare("a/q/1", "histogram", title="q", source="t")),
    ("bad id", lambda h: h.declare("a b", "xy", title="q", source="t")),
    ("map no geometry", lambda h: h.declare("a/map/2", "map2d", title="m", source="t",
                                            meta={"nx": 3})),
    ("end unknown", lambda h: h.end("nope/x/y")),
]


def _world(h: LiveHub) -> LiveHub:
    h.declare("a/x/1", "xy", title="a", source="t")
    h.declare("a/ccd/spec", "spectrum", title="c", source="t")
    h.declare("a/map/r", "map2d", title="m", source="t", meta=MAP)
    h.declare("a/log/r", "status", title="l", source="t")
    return h


@pytest.mark.parametrize("why, misuse", _MISUSES, ids=[m[0] for m in _MISUSES])
def test_strict_mode_raises_on_misuse(why, misuse):
    with pytest.raises(LiveError):
        misuse(_world(_strict()))


@pytest.mark.parametrize("why, misuse", _MISUSES, ids=[m[0] for m in _MISUSES])
def test_the_production_hub_never_raises_on_misuse(why, misuse, caplog):
    h = _world(LiveHub(clock=lambda: NOW))
    with caplog.at_level(logging.WARNING, logger="mast.live.hub"):
        out = misuse(h)
    assert out in (None, -1), out
    assert any(r.levelno == logging.WARNING for r in caplog.records), "用错了却一声不吭"


def test_a_repeated_misuse_is_logged_once_not_per_frame(caplog):
    h = _world(LiveHub(clock=lambda: NOW))
    with caplog.at_level(logging.WARNING, logger="mast.live.hub"):
        for _ in range(500):
            assert h.publish("a/x/1", {"x": 1, "values": {"v": "bad"}}) == -1
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_an_unexpected_internal_error_is_swallowed_in_production(monkeypatch, caplog):
    h = _world(LiveHub(clock=lambda: NOW))

    def boom(*a, **k):
        raise RuntimeError("serializer exploded")
    monkeypatch.setattr(hub_mod, "_dumps", boom)
    with caplog.at_level(logging.WARNING, logger="mast.live.hub"):
        assert h.publish("a/x/1", {"x": 1, "values": {"v": 1}}) == -1
    assert "serializer exploded" in caplog.text


def test_a_broken_listener_cannot_fail_a_publish():
    h = _world(_strict())

    def bad(ev):
        raise RuntimeError("listener bug")
    h.add_listener(bad)
    assert h.publish("a/x/1", {"x": 1, "values": {"v": 1}}) == 1


def test_publishing_after_end_is_refused_until_redeclared():
    h = _world(_strict())
    h.publish("a/x/1", {"x": 0, "values": {"v": 0}})
    h.end("a/x/1")
    assert h.describe("a/x/1")["ended"] is True
    with pytest.raises(LiveError):
        h.publish("a/x/1", {"x": 1, "values": {"v": 1}})
    h.declare("a/x/1", "xy", title="a（续）", source="t")
    d = h.describe("a/x/1")
    assert d["ended"] is False and d["title"] == "a（续）" and d["n_frames"] == 1
    assert h.publish("a/x/1", {"x": 1, "values": {"v": 1}}) == 2


# ════════════════════════════════════════════════════════════════════════════
# 3 · 横轴（spectrum）
# ════════════════════════════════════════════════════════════════════════════

def test_axis_versions_only_bump_when_the_axis_changes():
    h = _strict()
    h.declare("s/ccd/spec", "spectrum", title="CCD", source="s", meta={"laser_line_nm": 632.8})
    assert h.set_axis("s/ccd/spec", [500.0, 501.0, 502.0], unit="nm") == 1
    assert h.set_axis("s/ccd/spec", np.array([500.0, 501.0, 502.0]), unit="nm") == 1
    assert h.set_axis("s/ccd/spec", [500.0, 501.0, 502.0], unit="cm-1") == 2
    assert h.set_axis("s/ccd/spec", [600.0, 601.0], unit="nm") == 3
    assert h.axis("s/ccd/spec") == {"ver": 3, "unit": "nm", "n": 2, "x": [600.0, 601.0]}
    assert h.axis("s/ccd/spec", 1)["unit"] == "nm"


def test_a_frame_must_match_the_length_of_the_axis_it_names():
    h = _strict()
    h.declare("s/ccd/spec", "spectrum", title="CCD", source="s")
    v1 = h.set_axis("s/ccd/spec", [1.0, 2.0, 3.0], unit="nm")
    v2 = h.set_axis("s/ccd/spec", [1.0, 2.0], unit="nm")
    assert h.publish("s/ccd/spec", {"y": [5, 6]}) == 1                     # 缺省 = 当前版 v2
    assert h.latest("s/ccd/spec")["frame"]["axis_ver"] == v2
    assert h.publish("s/ccd/spec", {"y": [5, 6, 7], "axis_ver": v1}) == 2  # 旧版也行
    with pytest.raises(LiveError):
        h.publish("s/ccd/spec", {"y": [5, 6, 7]})                         # 和 v2 长度不符
    snap = h.snapshot("s/ccd/spec", since=0)
    assert snap.axis.ver == v2 and [a.ver for a in snap.axes] == [v1], "旧帧引用的旧轴没带上"


def test_axis_history_is_bounded():
    h = _strict()
    h.declare("s/ccd/spec", "spectrum", title="CCD", source="s")
    for i in range(hub_mod.AXIS_HISTORY + 4):
        h.set_axis("s/ccd/spec", [float(i), float(i) + 1], unit="nm")
    assert h.axis("s/ccd/spec", 1) is None
    assert h.axis("s/ccd/spec")["ver"] == hub_mod.AXIS_HISTORY + 4
    # 引用了已淘汰版本的帧照收（客户端按序号画），只是不再带轴
    assert h.publish("s/ccd/spec", {"y": [1, 2, 3], "axis_ver": 1}) == 1


# ════════════════════════════════════════════════════════════════════════════
# 4 · 保留与回看
# ════════════════════════════════════════════════════════════════════════════

def test_retention_is_bounded_and_a_gap_is_reported_only_past_a_real_cursor():
    h = _strict()
    h.declare("s/pp/r", "xy", title="pp", source="s", retain=10)
    for i in range(25):
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})
    d = h.describe("s/pp/r")
    assert (d["n_frames"], d["first_seq"], d["last_seq"]) == (10, 16, 25)
    snap = h.snapshot("s/pp/r", since=5)
    assert [s for s, _ in snap.frames] == list(range(16, 26)) and snap.dropped == (6, 15)
    assert h.snapshot("s/pp/r", since=0).dropped is None, "since=0 是「我什么都没有」，不叫丢"
    assert h.snapshot("s/pp/r", since=20).dropped is None
    assert [s for s, _ in h.snapshot("s/pp/r", since=-1).frames] == [25]


def test_since_defaults_differ_by_kind():
    h = _strict()
    h.declare("s/ccd/spec", "spectrum", title="c", source="s")
    h.declare("s/pp/r", "xy", title="p", source="s")
    for i in range(5):
        h.publish("s/ccd/spec", {"y": [i]})
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})
    assert [f["seq"] for f in h.frames("s/ccd/spec")] == [5], "spectrum 缺省只要最新一帧"
    assert [f["seq"] for f in h.frames("s/pp/r")] == [1, 2, 3, 4, 5], "曲线缺省要全部历史"


def test_limit_pages_forward_with_more():
    h = _strict()
    h.declare("s/pp/r", "xy", title="p", source="s")
    for i in range(12):
        h.publish("s/pp/r", {"x": i, "values": {"v": i}})
    page, seen = h.snapshot("s/pp/r", since=0, limit=5), []
    while True:
        seen += [s for s, _ in page.frames]
        if not page.more:
            break
        page = h.snapshot("s/pp/r", since=seen[-1], limit=5)
    assert seen == list(range(1, 13))


def test_map2d_keeps_the_latest_value_per_pixel_in_seq_order():
    h = _strict()
    h.declare("s/map/r", "map2d", title="m", source="s", meta=MAP)
    h.publish("s/map/r", {"ix": 0, "iy": 0, "values": {"I": 1}})
    h.publish("s/map/r", {"ix": 1, "iy": 0, "values": {"I": 2}})
    h.publish("s/map/r", {"ix": 0, "iy": 0, "values": {"I": 3}})       # 同一像素再测一次
    frames = h.frames("s/map/r", since=0)
    assert [(f["seq"], f["frame"]["ix"], f["frame"]["values"]["I"]) for f in frames] == [
        (2, 1, 2.0), (3, 0, 3.0)]
    assert h.snapshot("s/map/r", since=1).dropped is None, "被新值顶掉的旧值不算丢"


def test_map2d_retention_caps_evict_the_least_recently_updated_pixel():
    h = _strict()
    h.declare("s/map/r", "map2d", title="m", source="s", meta=MAP, retain=2)
    for ix in range(4):                                   # seq 1..4，只留得下 2 个像素
        h.publish("s/map/r", {"ix": ix, "iy": 0, "values": {"I": ix}})
    assert [(f["seq"], f["frame"]["ix"]) for f in h.frames("s/map/r", since=0)] == [(3, 2), (4, 3)]
    assert h.describe("s/map/r")["first_seq"] == 3
    # 客户端手里有到 seq 1 为止：seq 2 那个像素已被挤掉，它缺的就是这一段
    assert h.snapshot("s/map/r", since=1).dropped == (2, 2)
    assert h.snapshot("s/map/r", since=2).dropped is None


def test_a_map_larger_than_the_cap_is_still_declared_with_capped_retention():
    h = _strict()
    h.declare("s/map/big", "map2d", title="m", source="s",
              meta={**MAP, "nx": 1024, "ny": 1024})
    assert h.describe("s/map/big")["retain"] == hub_mod.MAP2D_MAX_PIXELS
    h.declare("s/map/small", "map2d", title="m", source="s", meta=MAP)
    assert h.describe("s/map/small")["retain"] == 12


# ════════════════════════════════════════════════════════════════════════════
# 5 · 重新声明、流数上限、内存上限
# ════════════════════════════════════════════════════════════════════════════

def test_redeclaring_with_another_kind_resets_but_seq_keeps_counting():
    h = _strict()
    with LiveRecorder(h) as rec:
        h.declare("s/q/1", "xy", title="q", source="s")
        for i in range(3):
            h.publish("s/q/1", {"x": i, "values": {"v": i}})
        uid = h.describe("s/q/1")["uid"]
        h.declare("s/q/1", "scalar", title="q", source="s")
        d = h.describe("s/q/1")
        assert d["uid"] != uid and d["n_frames"] == 0 and d["kind"] == "scalar"
        assert h.publish("s/q/1", {"values": {"v": 1}}) == 4, "seq 回退了：客户端的 since 会把新帧当旧帧跳过"
    assert [e["reset"] for e in rec.of("declare", "s/q/1")] == [False, True]


def test_an_identical_redeclare_is_silent():
    h = _strict()
    with LiveRecorder(h) as rec:
        for _ in range(3):
            h.declare("s/q/1", "xy", title="q", source="s")
    assert len(rec.of("declare")) == 1, "一模一样的重复声明不该每次都广播目录"


def test_stream_cap_evicts_ended_streams_first_then_the_least_recently_active():
    t = [NOW]
    h = LiveHub(strict=True, max_streams=3, clock=lambda: t[0])
    with LiveRecorder(h) as rec:
        for sid in ("a/x/1", "b/x/1", "c/x/1"):
            t[0] += 1
            h.declare(sid, "xy", title=sid, source="t")
        for sid in ("a/x/1", "c/x/1"):
            t[0] += 1
            h.publish(sid, {"x": 1, "values": {"v": 1}})
        h.end("c/x/1")
        t[0] += 1
        h.declare("d/x/1", "xy", title="d", source="t")          # 先淘汰已结束的 c
        assert [s["id"] for s in h.streams()] == ["a/x/1", "b/x/1", "d/x/1"]
        t[0] += 1
        h.declare("e/x/1", "xy", title="e", source="t")          # 再淘汰最久没动静的 b
        assert [s["id"] for s in h.streams()] == ["a/x/1", "d/x/1", "e/x/1"]
    assert [e["stream"] for e in rec.of("evict")] == ["c/x/1", "b/x/1"]
    h.declare("c/x/1", "xy", title="c 又来了", source="t")
    assert h.publish("c/x/1", {"x": 2, "values": {"v": 2}}) == 2, "淘汰后再声明，seq 要接着编"
    assert h.snapshot("c/x/1", since=1).dropped is None
    assert h.describe("c/x/1")["first_seq"] == 2


def test_the_memory_budget_evicts_ended_streams_then_trims_the_biggest():
    h = LiveHub(strict=True, max_bytes=6000, clock=lambda: NOW)
    h.declare("old/x/1", "xy", title="old", source="t")
    for i in range(20):
        h.publish("old/x/1", {"x": i, "values": {"v": i}})
    h.end("old/x/1")
    h.declare("big/ccd/spec", "spectrum", title="big", source="t")
    h.declare("small/x/1", "xy", title="small", source="t")
    h.publish("small/x/1", {"x": 0, "values": {"v": 0}})
    for _ in range(40):
        h.publish("big/ccd/spec", {"y": list(range(60))})
    ids = [s["id"] for s in h.streams()]
    assert "old/x/1" not in ids, "超预算时应先整条淘汰已结束的流"
    assert h.describe("small/x/1")["n_frames"] == 1, "小流不该替大流还账"
    big = h.describe("big/ccd/spec")
    assert 1 <= big["n_frames"] < 40
    assert sum(s["bytes"] for s in h.streams()) <= 6000
    assert h._chars == sum(s["bytes"] for s in h.streams()), "占用的账算错了"


# ════════════════════════════════════════════════════════════════════════════
# 6 · 并发、单例、测试工具
# ════════════════════════════════════════════════════════════════════════════

def test_concurrent_publishers_keep_every_stream_ordered_and_contiguous():
    h = _strict()
    sids = ("a/x/1", "b/x/1")
    for sid in sids:
        h.declare(sid, "xy", title=sid, source="t", retain=20000)
    rec = LiveRecorder(h)
    n_threads, per = 8, 400

    def worker(k: int) -> None:
        for i in range(per):
            h.publish(sids[(k + i) % 2], {"x": i, "values": {"k": k}})
    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    rec.close()
    total = n_threads * per
    for sid in sids:
        seqs = rec.seqs(sid)
        assert seqs == list(range(1, len(seqs) + 1)), "订阅者收到的 seq 乱序或有洞"
        assert [f["seq"] for f in h.frames(sid, since=0)] == seqs
    assert sum(len(rec.seqs(s)) for s in sids) == total


def test_ids_are_validated():
    h = _strict()
    for good in ("rig1/ccd/spectrum", "rig2/ters_map/run_20261019T120000+08:00",
                 "rig2/pump_probe/r-1.2@x"):
        h.declare(good, "xy", title="ok", source="t")
    for bad in ("", "a//b", "/a", "a/", "a/../b", "a/./b", "has space", "*", "中文/x",
                "x" * 201, 42):
        with pytest.raises(LiveError):
            h.declare(bad, "xy", title="bad", source="t")


def test_declare_meta_is_made_json_safe_and_bounded():
    h = _strict()
    h.declare("s/x/1", "xy", title="t", source="s",
              meta={"when": _dt.datetime(2026, 10, 19, 12, 0), "nan": float("nan"),
                    "arr": np.arange(3), "n": np.int32(7)})
    meta = h.describe("s/x/1")["meta"]
    assert meta == {"when": "2026-10-19 12:00:00", "nan": None, "arr": [0, 1, 2], "n": 7}
    with pytest.raises(LiveError):
        h.declare("s/x/2", "xy", title="t", source="s", meta={"blob": "x" * 20000})


def test_a_frame_over_the_size_cap_is_refused(monkeypatch):
    monkeypatch.setattr(hub_mod, "MAX_FRAME_CHARS", 200)
    h = _strict()
    h.declare("s/ccd/spec", "spectrum", title="c", source="s")
    with pytest.raises(LiveError):
        h.publish("s/ccd/spec", {"y": list(range(100))})


def test_get_live_hub_is_a_singleton_and_isolation_swaps_it_back():
    before = get_live_hub()
    assert get_live_hub() is before
    with isolated_live_hub() as fresh:
        assert get_live_hub() is fresh and fresh is not before and fresh.strict
    assert get_live_hub() is before and not before.strict


def test_the_recorder_flags_overflow_instead_of_dropping_silently():
    h = _world(_strict())
    rec = LiveRecorder(h, limit=2)
    for i in range(3):
        h.publish("a/x/1", {"x": i, "values": {"v": i}})
    rec.close()
    assert rec.overflowed and len(rec.events) == 2
    h.publish("a/x/1", {"x": 9, "values": {"v": 9}})
    assert len(rec.events) == 2, "close 之后还在记"


def test_the_recorder_sees_the_whole_lifecycle_in_order():
    with isolated_live_hub() as h, LiveRecorder(h) as rec:
        live = get_live_hub()
        live.declare("rig1/ccd/spectrum", "spectrum", title="CCD", source="rig1")
        ver = live.set_axis("rig1/ccd/spectrum", [1.0, 2.0], unit="nm")
        live.publish("rig1/ccd/spectrum", {"y": [3, 4], "axis_ver": ver})
        live.end("rig1/ccd/spectrum")
    assert [e["type"] for e in rec.events] == ["declare", "axis", "frame", "end"]
    assert rec.declared() == rec.ended() == ["rig1/ccd/spectrum"]
    assert rec.frames("rig1/ccd/spectrum")[0]["y"] == [3.0, 4.0]
    assert rec.axes("rig1/ccd/spectrum") == [{"ver": 1, "unit": "nm", "n": 2, "x": [1.0, 2.0]}]
