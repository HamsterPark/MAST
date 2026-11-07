"""``WS /ws/live`` 与 ``GET /api/live/streams[/…]`` —— 浏览器那一端看到的形状（契约 §7）。

用一个只挂 live 路由的最小 app 跑大部分用例（快、隔离）；另有一条用真 ``create_app`` 确认路由
注册上了、并且在访问闸门后面。发布方都在后台线程上 —— 真实的发布者（卫星枢纽、技能）就是这样，
线程 → 事件循环那座桥断了的话，本地同线程的测试照样会绿。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("nanonis_spm", MagicMock())

_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from mast.api.routes import live as live_routes  # noqa: E402
from mast.api.schemas_live import LiveStreamDetail, LiveStreamList  # noqa: E402
from mast.live.testing import isolated_live_hub  # noqa: E402

MAP = dict(nx=4, ny=4, x0=0.0, y0=0.0, dx=2.0, dy=2.0, x_unit="nm", y_unit="nm")


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(live_routes.router, prefix="/api")
    app.include_router(live_routes.ws_router)
    return TestClient(app)


@pytest.fixture()
def hub():
    with isolated_live_hub() as h:
        yield h


def _later(fn, delay: float = 0.1) -> threading.Thread:
    def run():
        time.sleep(delay)
        fn()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def _collect(ws, sid: str, want: int, *, max_msgs: int = 5000) -> tuple[list[int], list[dict]]:
    seqs: list[int] = []
    seen: list[dict] = []
    for _ in range(max_msgs):
        m = ws.receive_json()
        seen.append(m)
        if m.get("stream") == sid:
            if m["kind"] == "frame":
                seqs.append(m["seq"])
            elif m["kind"] == "frames":
                seqs += [it["seq"] for it in m["items"]]
        if seqs and seqs[-1] >= want:
            break
    return seqs, seen


# ════════════════════════════════════════════════════════════════════════════
# 1 · WS
# ════════════════════════════════════════════════════════════════════════════

def test_connecting_sends_the_directory_first(client, hub):
    hub.declare("rig1/ccd/spectrum", "spectrum", title="CCD", source="rig1",
                meta={"laser_line_nm": 632.8})
    with client.websocket_connect("/ws/live") as ws:
        first = ws.receive_json()
    assert first["kind"] == "streams" and first["hub"] == hub.hub_id
    item = first["items"][0]
    assert item["id"] == "rig1/ccd/spectrum" and item["meta"] == {"laser_line_nm": 632.8}


def test_frames_published_on_another_thread_arrive(client, hub):
    hub.declare("rig2/pump_probe/r1", "xy", title="pp", source="rig2")
    with client.websocket_connect("/ws/live") as ws:
        ws.receive_json()
        ws.send_json({"op": "subscribe", "streams": ["rig2/pump_probe/r1"]})
        _later(lambda: hub.publish("rig2/pump_probe/r1", {"x": 1.5, "values": {"sig": 2.0}}))
        seqs, seen = _collect(ws, "rig2/pump_probe/r1", 1)
    assert seqs == [1]
    frame = next(m for m in seen if m["kind"] == "frame")
    assert frame["frame"]["x"] == 1.5 and frame["frame"]["values"] == {"sig": 2.0}


def test_xy_frames_are_never_lost_under_a_burst(client, hub):
    sid = "rig2/pump_probe/burst"
    hub.declare(sid, "xy", title="pp", source="rig2", retain=20000)
    n = 3000
    with client.websocket_connect("/ws/live") as ws:
        ws.receive_json()
        ws.send_json({"op": "subscribe", "streams": ["*"]})
        _later(lambda: [hub.publish(sid, {"x": i, "values": {"v": i}}) for i in range(n)])
        seqs, seen = _collect(ws, sid, n)
    assert seqs == list(range(1, n + 1))
    assert not [m for m in seen if m["kind"] == "dropped"]
    assert len([m for m in seen if m.get("stream") == sid]) < n / 5, "没有合批"


def test_spectra_are_rate_limited_to_the_latest_frame(client, hub):
    sid = "rig1/ccd/spectrum"
    hub.declare(sid, "spectrum", title="CCD", source="rig1")
    n = 120
    took: list[float] = []

    def burst():
        t0 = time.monotonic()
        for _ in range(n):
            hub.publish(sid, {"y": [1, 2, 3]})
            time.sleep(0.004)                       # ≈ 250 Hz
        took.append(time.monotonic() - t0)
    with client.websocket_connect("/ws/live?max_hz=5") as ws:
        ws.receive_json()
        ws.send_json({"op": "subscribe", "streams": [sid]})
        _later(burst)
        seqs, _ = _collect(ws, sid, n)
    assert seqs[-1] == n, "最后一帧必须送到"
    budget = took[0] * 5 + 3                        # 5 Hz × 持续时间，外加首尾各一拍的余量
    assert len(seqs) <= budget, f"5 Hz 限速下 {took[0]:.2f} s 收到 {len(seqs)} 帧"
    assert len(seqs) < n / 4 and seqs == sorted(seqs)


def test_since_on_subscribe_replays_what_the_client_missed(client, hub):
    sid = "rig2/ters_map/r9"
    hub.declare(sid, "map2d", title="TERS", source="rig2", meta=MAP)
    for i in range(6):
        hub.publish(sid, {"ix": i % 4, "iy": i // 4, "values": {"I": i}})
    with client.websocket_connect("/ws/live") as ws:
        ws.receive_json()
        ws.send_json({"op": "subscribe", "streams": [sid], "since": {sid: 4}})
        seqs, _ = _collect(ws, sid, 6)
    assert seqs == [5, 6]


def test_a_quiet_socket_gets_a_ping(client, hub, monkeypatch):
    monkeypatch.setattr(live_routes, "_PING_S", 0.2)
    with client.websocket_connect("/ws/live") as ws:
        assert ws.receive_json()["kind"] == "streams"
        assert ws.receive_json() == {"kind": "ping"}


def test_a_bad_client_message_is_answered_and_the_socket_stays_open(client, hub):
    hub.declare("s/pp/r", "xy", title="pp", source="s")
    with client.websocket_connect("/ws/live") as ws:
        ws.receive_json()
        ws.send_text("{not json")
        ws.send_bytes(b"\x00\x01")                  # 二进制帧：忽略
        err = ws.receive_json()
        assert err["kind"] == "error"
        ws.send_json({"op": "subscribe", "streams": ["*"]})
        _later(lambda: hub.publish("s/pp/r", {"x": 0, "values": {"v": 0}}))
        seqs, _ = _collect(ws, "s/pp/r", 1)
    assert seqs == [1]


def test_closing_the_socket_detaches_its_subscription(client, hub):
    with client.websocket_connect("/ws/live") as ws:
        ws.receive_json()
        assert len(hub._listeners) == 1
    deadline = time.time() + 5
    while hub._listeners and time.time() < deadline:
        time.sleep(0.02)
    assert not hub._listeners, "断开的连接还挂在 hub 上 —— 之后的每一帧都会往它身上入队"


@pytest.mark.parametrize("raw, want", [(None, 20.0), ("5", 5.0), ("0", 0.5), ("1000", 60.0),
                                       ("nan", 20.0), ("abc", 20.0)])
def test_max_hz_is_clamped(raw, want):
    assert live_routes._parse_hz(raw) == want


# ════════════════════════════════════════════════════════════════════════════
# 2 · REST
# ════════════════════════════════════════════════════════════════════════════

def test_rest_lists_every_stream_with_its_latest_seq(client, hub):
    hub.declare("rig1/pol/angle", "scalar", title="偏振角", source="rig1", y_unit="deg")
    for d in (0.0, 22.5, 45.0):
        hub.publish("rig1/pol/angle", {"values": {"deg": d}})
    r = client.get("/api/live/streams")
    assert r.status_code == 200
    body = LiveStreamList.model_validate(r.json())
    assert body.hub == hub.hub_id
    assert [(s.id, s.last_seq, s.kind) for s in body.items] == [("rig1/pol/angle", 3, "scalar")]


@pytest.mark.parametrize("kind, meta, frame", [
    ("spectrum", None, {"y": [1.0, float("nan"), 3.0]}),
    ("scalar", None, {"values": {"deg": 1.0}}),
    ("xy", None, {"x": 1.0, "values": {"v": float("inf")}}),
    ("map2d", MAP, {"ix": 1, "iy": 2, "values": {"I": 1.0}}),
    ("status", None, {"text": "完成", "level": "info"}),
])
def test_rest_detail_matches_its_schema_for_every_kind(client, hub, kind, meta, frame):
    sid = f"t/{kind}/run_2026-10-19T12:00:00"
    hub.declare(sid, kind, title=kind, source="t", meta=meta)
    if kind == "spectrum":
        hub.set_axis(sid, [500.0, 501.0, 502.0], unit="nm")
    hub.publish(sid, frame)
    r = client.get(f"/api/live/streams/{sid}", params={"since": 0})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")

    def boom(c):
        raise AssertionError(f"REST 回答里有 {c}")
    raw = json.loads(r.text, parse_constant=boom)
    detail = LiveStreamDetail.model_validate(raw)
    assert detail.meta.id == sid and detail.frames[0].seq == 1
    if kind == "spectrum":
        assert detail.axis is not None and detail.axis.x == [500.0, 501.0, 502.0]
        assert raw["frames"][0]["frame"]["y"] == [1.0, None, 3.0]


def test_rest_pages_forward_and_reports_gaps(client, hub):
    sid = "t/pp/r"
    hub.declare(sid, "xy", title="pp", source="t", retain=10)
    for i in range(30):
        hub.publish(sid, {"x": i, "values": {"v": i}})
    r = client.get(f"/api/live/streams/{sid}", params={"since": 5, "limit": 4}).json()
    assert [f["seq"] for f in r["frames"]] == [21, 22, 23, 24] and r["more"] is True
    assert r["dropped"] == {"from": 6, "to": 20}
    r = client.get(f"/api/live/streams/{sid}", params={"since": -1}).json()
    assert [f["seq"] for f in r["frames"]] == [30] and r["more"] is False and r["dropped"] is None


def test_rest_unknown_stream_is_404(client, hub):
    assert client.get("/api/live/streams/no/such/stream").status_code == 404
    assert client.get("/api/live/streams/x", params={"limit": 0}).status_code == 422


# ════════════════════════════════════════════════════════════════════════════
# 3 · 真 app：注册上了，且在访问闸门后面
# ════════════════════════════════════════════════════════════════════════════

def test_the_real_app_serves_live_behind_the_access_gate(hub):
    import base64

    from starlette.testclient import WebSocketDenialResponse

    from mast.api.app import create_app

    app = create_app(auth=("op", "pw"), dev_cors=False)
    spec_paths = set(app.openapi()["paths"])
    assert {"/api/live/streams", "/api/live/streams/{stream_id}"} <= spec_paths
    c = TestClient(app)
    basic = {"Authorization": "Basic " + base64.b64encode(b"op:pw").decode()}
    assert c.get("/api/live/streams").status_code == 401
    assert c.get("/api/live/streams", headers=basic).status_code == 200
    with pytest.raises(WebSocketDenialResponse):
        with c.websocket_connect("/ws/live"):
            pass
    hub.declare("t/pp/r", "xy", title="pp", source="t")
    with c.websocket_connect("/ws/live", headers=basic) as ws:
        msg = ws.receive_json()
    assert msg["kind"] == "streams" and [i["id"] for i in msg["items"]] == ["t/pp/r"]
