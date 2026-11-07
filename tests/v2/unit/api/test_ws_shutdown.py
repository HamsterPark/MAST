"""优雅关停不许被连着的 WebSocket 拖住。

问题：有界面连着 ``/ws/events`` 时，每次优雅关停都正好多出 15 s。
处理协程只等自己的队列、从不读 socket，uvicorn 关停时关掉的连接它看不见，要等下一次发 ping 失败才
退出；``/ws/buffer`` 接上 BufferService 后是同一个形状。关停端点的退出兜底是 20 s，15 s 再加别的收尾
就可能越线被 ``os._exit`` 收场。

修法是每个循环另有一个只等断线的读任务。这里钉住两件事：
1. 真起 uvicorn、连一个客户端、触发优雅关停：三个端点都要在 3 s 内退出（修之前实测 14 s）；
2. 客户端发来的零星帧不能被当成断线，也不改变 ping 的语义。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("nanonis_spm", MagicMock())

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

import asyncio  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _QueueBuffer:
    """接上 BufferService 时 /ws/buffer 走队列分支 —— 生产就是这样，也正是会拖住关停的那条路。"""

    def get_latest_tip_status(self):
        return None, -1

    def get_latest_progress(self):
        return None, -1

    def get_latest_region(self):
        return None, -1

    def get_event_history(self, limit: int = 20):
        return []

    def subscribe_all(self, max_size: int = 100):
        return asyncio.Queue(maxsize=max_size)

    def unsubscribe_all(self, q) -> None:
        pass


def _app(wired: bool):
    from mast.api.app import create_app
    from mast.api.context import AppContext

    ctx = AppContext()
    if wired:
        ctx.wire(buffer_service=_QueueBuffer())
    return create_app(context=ctx, dev_cors=False)


def _start(wired: bool):
    """真 uvicorn（回环随机端口）。返回 (server, 线程, 端口)。"""
    uvicorn = pytest.importorskip("uvicorn")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(_app(wired), host="127.0.0.1", port=port,
                                           log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, th, port


def _stop(server, th) -> None:
    server.should_exit = True
    th.join(timeout=20)


@pytest.mark.parametrize("path, wired, greeting", [
    ("/ws/events", False, None),          # 连上后什么都不发（没有可重放的事件时）
    ("/ws/buffer", True, "snapshot"),
    ("/ws/live", False, "streams"),
])
def test_graceful_shutdown_is_not_held_up_by_a_connected_websocket(path, wired, greeting):
    websockets = pytest.importorskip("websockets")
    from mast.core.events import EventBus

    server, th, port = _start(wired)
    in_loop = threading.Event()
    closed = threading.Event()
    _, cursor = EventBus.get().recent_events_since(0)
    url = f"ws://127.0.0.1:{port}{path}" + (f"?since={cursor}" if path == "/ws/events" else "")

    def client() -> None:
        async def run() -> None:
            try:
                async with websockets.connect(url, open_timeout=10) as ws:
                    if greeting is not None:
                        assert json.loads(await ws.recv())["kind"] == greeting
                    in_loop.set()
                    while True:
                        await ws.recv()
            except Exception:  # noqa: BLE001 — 服务端关停时连接被关：预期之中
                pass
            finally:
                closed.set()
        asyncio.run(run())

    threading.Thread(target=client, daemon=True).start()
    assert in_loop.wait(10), "客户端没连上"
    time.sleep(0.5)                         # 让处理协程进入它的等待循环
    t0 = time.monotonic()
    server.should_exit = True
    th.join(timeout=20)
    took = time.monotonic() - t0
    assert not th.is_alive(), "20 s 了服务还没退出"
    assert took < 3.0, f"{path}：优雅关停用了 {took:.1f} s —— 处理协程没看见连接被关"
    assert closed.wait(5), "客户端那头连接没有被关掉"


# 下面两条也在真服务器上跑、每次读都带超时：处理协程要是错把零星帧当成断线而退出，真服务器会把连接关掉
# （客户端立刻报错），TestClient 却不会 —— 它的 receive 没有超时，会一直等下去。

def test_a_stray_client_frame_on_ws_events_is_ignored_not_taken_for_a_disconnect(monkeypatch):
    websockets = pytest.importorskip("websockets")
    from mast.api import ws as ws_mod
    from mast.core.events import Event, EventBus, EventType

    monkeypatch.setattr(ws_mod, "_PING_TIMEOUT_S", 0.3)
    bus = EventBus.get()
    _, cursor = bus.recent_events_since(0)
    tag = f"stray-{time.time_ns()}"
    server, th, port = _start(False)

    async def run() -> None:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/events?since={cursor}") as ws:
            await ws.send("hello")             # 协议里客户端不发消息；发了也只是被读掉
            await ws.send(b"\x00\x01")
            first = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert first["kind"] == "ping", "空闲时 ping 的语义变了"
            bus.publish(Event(type=EventType.HARDWARE_STATE, data={"tag": tag}))
            for _ in range(20):
                f = json.loads(await asyncio.wait_for(ws.recv(), 3))
                if f.get("kind") == "event" and (f.get("data") or {}).get("tag") == tag:
                    return
            pytest.fail("发了零星帧之后实时事件不再送达")

    try:
        asyncio.run(run())
    finally:
        _stop(server, th)


@pytest.mark.parametrize("wired", [False, True], ids=["degraded", "queue"])
def test_a_stray_client_frame_on_ws_buffer_is_ignored(monkeypatch, wired):
    websockets = pytest.importorskip("websockets")
    from mast.api import ws as ws_mod

    monkeypatch.setattr(ws_mod, "_PING_TIMEOUT_S", 0.3)
    server, th, port = _start(wired)

    async def run() -> None:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/buffer") as ws:
            assert json.loads(await asyncio.wait_for(ws.recv(), 3))["kind"] == "snapshot"
            await ws.send("hi")
            for _ in range(2):                 # 收到客户端的帧之后连接照常，ping 照常
                assert json.loads(await asyncio.wait_for(ws.recv(), 3))["kind"] == "ping"

    try:
        asyncio.run(run())
    finally:
        _stop(server, th)


def test_a_client_that_leaves_is_unsubscribed_from_the_event_bus_promptly():
    from mast.core.events import EventBus

    bus = EventBus.get()
    before = len(bus._subscribers)
    with TestClient(_app(False)).websocket_connect("/ws/events") as ws:
        ws.send_text("x")
        deadline = time.time() + 5
        while len(bus._subscribers) == before and time.time() < deadline:
            time.sleep(0.02)
        assert len(bus._subscribers) == before + 1
    deadline = time.time() + 3
    while len(bus._subscribers) > before and time.time() < deadline:
        time.sleep(0.02)
    assert len(bus._subscribers) == before, "断开的连接还挂在 EventBus 上"
