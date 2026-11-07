"""在真 uvicorn 上验一遍 WS 认证（回环、随机端口、几秒钟）。

这类漏洞本来就是「HTTP 中间件管得着、真服务器上的 WebSocket 握手管不着」—— TestClient 只是模仿
uvicorn 的 ASGI 行为，所以最要紧的两条在真服务器 + 真客户端（``websockets``）上再钉一次：
不带凭据的握手拿到 HTTP 401（Denial Response 扩展），带签名 cookie 的同源握手能连上。
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
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402

import pytest  # noqa: E402

uvicorn = pytest.importorskip("uvicorn")
websockets = pytest.importorskip("websockets")

BASIC = "Basic " + base64.b64encode(b"op:pw").decode()


@pytest.fixture(scope="module")
def lan_server():
    from mast.api.app import create_app

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(auth=("op", "pw"), dev_cors=False),
                                           host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn 没起来"
    yield port
    server.should_exit = True
    th.join(timeout=10)


async def _handshake(url: str, headers: dict):
    try:
        async with websockets.connect(url, additional_headers=headers, open_timeout=10) as ws:
            return "open", json.loads(await asyncio.wait_for(ws.recv(), 10))
    except websockets.exceptions.InvalidStatus as e:
        return e.response.status_code, e.response.headers.get("www-authenticate")


def test_a_real_server_refuses_an_unauthenticated_websocket_with_401(lan_server):
    status, challenge = asyncio.run(_handshake(f"ws://127.0.0.1:{lan_server}/ws/live", {}))
    assert status == 401 and challenge == 'Basic realm="MAST"'
    status, _ = asyncio.run(_handshake(f"ws://127.0.0.1:{lan_server}/ws/live",
                                       {"Authorization": BASIC, "Origin": "https://evil.example"}))
    assert status == 403


def test_a_real_server_accepts_the_session_cookie_on_a_same_origin_handshake(lan_server):
    req = urllib.request.Request(f"http://127.0.0.1:{lan_server}/api/health", headers={
        "Authorization": BASIC, "Sec-Fetch-Mode": "navigate", "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=10) as r:      # noqa: S310 — 回环
        cookies = r.headers.get_all("set-cookie") or []
    assert len(cookies) == 1 and "HttpOnly" in cookies[0] and "SameSite=Strict" in cookies[0]
    pair = cookies[0].split(";", 1)[0]
    status, first = asyncio.run(_handshake(
        f"ws://127.0.0.1:{lan_server}/ws/live",
        {"Cookie": pair, "Origin": f"http://127.0.0.1:{lan_server}"}))
    assert status == "open" and first["kind"] == "streams"
