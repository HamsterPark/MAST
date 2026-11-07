"""主服务的 GZip：大响应压缩、SSE 不压、不声明 gzip 的客户端拿到原样响应。

远程打开（Tailscale 中继、校外）时前端主包约 2.2 MB 未压缩，首屏要等几十秒；压缩只是把字节变少，
不许改变任何一类客户端看到的语义 —— 尤其是 SSE：被压缩器攒着不吐的事件流就是「对话卡住」。
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
import base64  # noqa: E402
import json  # noqa: E402
import queue  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from mast.api.app import create_app

    return create_app(dev_cors=False)


def test_a_large_json_response_is_gzipped_for_a_client_that_asks(app):
    r = TestClient(app).get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
    assert "MAST API" == r.json()["info"]["title"], "解压后的内容必须原样"


def test_a_client_that_does_not_ask_for_gzip_gets_the_identity_body(app):
    """启动器、MCP 插件用 urllib，不发 Accept-Encoding。"""
    r = TestClient(app).get("/openapi.json", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200 and "content-encoding" not in r.headers
    json.loads(r.content)


def test_server_sent_events_are_never_compressed(app):
    r = TestClient(app).get("/sse/buffer", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in r.headers, "SSE 被压缩器攒着就不再是实时流了"


def test_the_session_cookie_survives_compression():
    from mast.api.app import create_app

    c = TestClient(create_app(auth=("op", "pw"), dev_cors=False))
    r = c.get("/openapi.json", headers={
        "Authorization": "Basic " + base64.b64encode(b"op:pw").decode(),
        "Accept-Encoding": "gzip", "Sec-Fetch-Mode": "navigate", "Accept": "text/html"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
    assert "mast_session_" in r.headers.get("set-cookie", "")


def test_already_compressed_media_is_passed_through_and_the_rest_still_compressed():
    """契约 §13.1：图像响应不走 GZip。钉住的是 mast/api/compression.py 借用的 Starlette 内部
    行为（start 时记 content_type_is_excluded、body 时才看）—— 升级 Starlette 时这里先红。"""
    from fastapi import FastAPI
    from fastapi.responses import Response, StreamingResponse

    from mast.api.compression import MediaAwareGZipMiddleware

    app = FastAPI()
    app.add_middleware(MediaAwareGZipMiddleware, minimum_size=100, compresslevel=6)
    blob = b"A" * 5000                                   # 极易压缩：真压了一定看得出来

    @app.get("/png")
    def png():
        return Response(blob, media_type="image/png")

    @app.get("/jpeg-upper")
    def jpeg_upper():
        return Response(blob, headers={"Content-Type": "IMAGE/JPEG"})

    @app.get("/stream-jpeg")
    def stream_jpeg():
        return StreamingResponse(iter([blob, blob]), media_type="image/jpeg")

    @app.get("/zip")
    def zipped():
        return Response(blob, media_type="application/zip")

    @app.get("/json")
    def as_json():
        return Response(b'{"a":"' + blob + b'"}', media_type="application/json")

    @app.get("/octet")
    def octet():
        return Response(blob, media_type="application/octet-stream")

    c = TestClient(app)
    gz = {"Accept-Encoding": "gzip"}
    for path, size in (("/png", 5000), ("/jpeg-upper", 5000), ("/stream-jpeg", 10000), ("/zip", 5000)):
        r = c.get(path, headers=gz)
        assert "content-encoding" not in r.headers, path
        assert len(r.content) == size, path
    for path in ("/json", "/octet"):
        r = c.get(path, headers=gz)
        assert r.headers.get("content-encoding") == "gzip", path
    assert "content-encoding" not in c.get("/json", headers={"Accept-Encoding": "identity"}).headers


# ════════════════════════════════════════════════════════════════════════════
# 真服务器上逐帧实时：每一帧都要在下一帧产生之前送到 —— 攒着不吐的压缩器过不了这一关
# ════════════════════════════════════════════════════════════════════════════

class _FakeBuffer:
    """/sse/buffer 要的最小 BufferService：空快照，外加一个能从测试线程推事件的订阅队列。"""

    def __init__(self) -> None:
        self._subs: list = []
        self._lock = threading.Lock()

    def get_latest_tip_status(self):
        return None, -1

    def get_latest_progress(self):
        return None, -1

    def get_latest_region(self):
        return None, -1

    def get_event_history(self, limit: int = 20):
        return []

    def subscribe_all(self, max_size: int = 100):
        q: asyncio.Queue = asyncio.Queue(maxsize=max_size)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe_all(self, q) -> None:
        with self._lock:
            self._subs = [(lp, x) for lp, x in self._subs if x is not q]

    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)

    def push(self, ev) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            loop.call_soon_threadsafe(q.put_nowait, ev)


@pytest.fixture(scope="module")
def real_server():
    uvicorn = pytest.importorskip("uvicorn")
    from mast.api.app import create_app
    from mast.api.context import AppContext

    buf = _FakeBuffer()
    ctx = AppContext()
    ctx.wire(buffer_service=buf)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(context=ctx, dev_cors=False),
                                           host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield port, buf
    server.should_exit = True
    th.join(timeout=10)


def test_sse_is_delivered_frame_by_frame_and_uncompressed_on_a_real_server(real_server):
    import httpx

    from mast.buffer.schemas import Severity, VisionEvent, VisionEventType

    port, buf = real_server
    got: "queue.Queue[dict]" = queue.Queue()
    headers: dict = {}
    stop = threading.Event()

    def reader() -> None:
        with httpx.Client(timeout=30) as hc, hc.stream(
                "GET", f"http://127.0.0.1:{port}/sse/buffer",
                headers={"Accept-Encoding": "gzip"}) as r:
            headers.update(r.headers)
            for line in r.iter_lines():
                if line.startswith("data:"):
                    got.put(json.loads(line[len("data:"):]))
                if stop.is_set():
                    return

    threading.Thread(target=reader, daemon=True).start()
    assert got.get(timeout=10)["kind"] == "snapshot"
    deadline = time.time() + 5
    while buf.subscribers() == 0 and time.time() < deadline:
        time.sleep(0.02)
    for i in range(1, 4):
        buf.push(VisionEvent(seqno=i, kind=VisionEventType.SCAN_COMPLETE, severity=Severity.INFO,
                             payload={"i": i}))
        frame = got.get(timeout=3)           # 第 i 帧必须在第 i+1 帧产生之前到
        assert frame["kind"] == "event" and frame["event"]["payload"] == {"i": i}
    stop.set()
    buf.push(VisionEvent(seqno=9, kind=VisionEventType.SCAN_COMPLETE, severity=Severity.INFO))
    assert headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in headers


def test_ws_events_and_ws_live_are_delivered_frame_by_frame_on_a_real_server(real_server):
    websockets = pytest.importorskip("websockets")
    from mast.core.events import Event, EventBus, EventType
    from mast.live.testing import isolated_live_hub

    port, _buf = real_server
    tag = f"gzip-{time.time_ns()}"

    async def events() -> list[int]:
        _, cursor = EventBus.get().recent_events_since(0)
        seen = []
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/events?since={cursor}",
                                      additional_headers={"Accept-Encoding": "gzip"}) as ws:
            for i in range(1, 4):
                EventBus.get().publish(Event(type=EventType.HARDWARE_STATE, data={"tag": tag, "i": i}))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    if m.get("kind") == "event" and (m.get("data") or {}).get("tag") == tag:
                        seen.append(m["data"]["i"])
                        break
        return seen

    async def live(hub) -> list[int]:
        seen = []
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/live",
                                      additional_headers={"Accept-Encoding": "gzip"}) as ws:
            assert json.loads(await asyncio.wait_for(ws.recv(), 5))["kind"] == "streams"
            await ws.send(json.dumps({"op": "subscribe", "streams": ["t/ccd/spectrum"]}))
            for i in range(1, 4):
                hub.publish("t/ccd/spectrum", {"y": [i, i, i]})
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    if m.get("kind") == "frame" and m.get("stream") == "t/ccd/spectrum":
                        seen.append(m["seq"])
                        break
        return seen

    assert asyncio.run(events()) == [1, 2, 3]
    with isolated_live_hub() as hub:
        hub.declare("t/ccd/spectrum", "spectrum", title="t", source="t")
        assert asyncio.run(live(hub)) == [1, 2, 3]
