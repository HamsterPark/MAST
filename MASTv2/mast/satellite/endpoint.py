"""WebSocket 端点 ``/ws/satellite/v1`` 的实现（契约 §5.1）。

认证在 **accept 之前**：``Authorization: Bearer <token>`` + ``X-MAST-Satellite-Id``，常数时间
比较令牌哈希。失败回 HTTP 401（凭据不对）/ 403（被禁用），连 WebSocket 都不建立。
协议版本头不符则 accept 后以 4406 关闭。这条路径不走操作员的 Basic 认证（中间件对
``/ws/satellite/`` 放行），所以这里**没有令牌一律拒绝**。

路由注册在 ``mast/api/routes/satellites.py``；这里只放与 FastAPI 无关的会话循环。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from mast.satellite import protocol as P
from mast.satellite.errors import SatelliteError

logger = logging.getLogger(__name__)

#: 枢纽 → 卫星的待发队列上限。卫星不读、这边越积越多时断开（它重连后能续上）。
OUTQ_MAX = 10000

_auth_fail_log: dict[str, float] = {}


def _now_ms() -> int:
    return int(time.time() * 1000)


class AsyncTransport:
    """把线程安全的 ``send``/``close`` 落到一个 asyncio 发送任务上。"""

    def __init__(self, ws: Any, loop: asyncio.AbstractEventLoop, peer: str = "") -> None:
        self._ws = ws
        self._loop = loop
        self._q: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.close_code: int | None = None
        self.close_reason = ""
        self.peer = peer
        self.sent = 0

    def _put(self, item: tuple) -> bool:
        if self.closed:
            return False
        try:
            self._loop.call_soon_threadsafe(self._put_now, item)
            return True
        except RuntimeError:                  # 事件循环已关
            self.closed = True
            return False

    def _put_now(self, item: tuple) -> None:
        if self.closed:
            return
        if self._q.qsize() >= OUTQ_MAX and item[0] != "close":
            logger.warning("卫星 %s 的待发队列满（%d），断开", self.peer, OUTQ_MAX)
            self._q.put_nowait(("close", P.CLOSE_GOING_AWAY, "send queue overflow"))
            return
        self._q.put_nowait(item)

    def send(self, obj: dict) -> bool:
        return self._put(("msg", obj))

    def send_lazy(self, fn: Callable[[], dict | None]) -> bool:
        return self._put(("lazy", fn))

    def close(self, code: int, reason: str) -> None:
        self._put(("close", int(code), str(reason)[:120]))

    async def run(self) -> None:
        try:
            while True:
                item = await self._q.get()
                if item[0] == "close":
                    self.closed = True
                    self.close_code, self.close_reason = item[1], item[2]
                    try:
                        await self._ws.close(code=item[1], reason=item[2])
                    except Exception:  # noqa: BLE001 — 对端可能已经走了
                        pass
                    return
                obj = item[1]() if item[0] == "lazy" else item[1]
                if obj is None:
                    continue
                await self._ws.send_text(P.encode(obj))
                self.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 发送失败 = 连接没了
            self.closed = True
            logger.debug("satellite sender ended: %s", exc)


async def _deny(ws: Any, status: int, reason: str) -> None:
    """accept 之前拒绝：能发 HTTP 响应就发 401/403，否则关闭（uvicorn 回 403）。"""
    try:
        if "websocket.http.response" in (ws.scope.get("extensions") or {}):
            from starlette.responses import PlainTextResponse

            headers = {"WWW-Authenticate": 'Bearer realm="mast-satellite"'} if status == 401 else None
            await ws.send_denial_response(PlainTextResponse(reason, status_code=status,
                                                            headers=headers))
            return
    except Exception:  # noqa: BLE001
        logger.debug("send_denial_response failed", exc_info=True)
    try:
        await ws.close(code=1008, reason=reason[:120])
    except Exception:  # noqa: BLE001
        pass


def _peer(ws: Any) -> str:
    c = getattr(ws, "client", None)
    try:
        return f"{c.host}:{c.port}" if c else ""
    except Exception:  # noqa: BLE001
        return ""


def _bearer(ws: Any) -> str | None:
    hdr = ws.headers.get(P.HDR_AUTH, "") or ""
    if hdr[:7].lower() != "bearer ":
        return None
    tok = hdr[7:].strip()
    return tok or None


async def serve_satellite(ws: Any, hub=None) -> None:
    """一条卫星连接的完整生命周期。"""
    from mast.satellite.hub import get_satellite_hub

    hub = hub or get_satellite_hub()
    try:
        hub.ensure_started()
    except Exception:  # noqa: BLE001
        logger.warning("SatelliteHub 启动失败", exc_info=True)
    peer = _peer(ws)
    sat_id = (ws.headers.get(P.HDR_ID, "") or "").strip()
    token = _bearer(ws)
    status, why = hub.authenticate(sat_id, token)
    if status != 200:
        key = f"{peer.rsplit(':', 1)[0]}|{sat_id}|{status}"
        now = time.monotonic()
        if now - _auth_fail_log.get(key, 0.0) > 30.0:
            _auth_fail_log[key] = now
            # 503 是枢纽自己在关停（卫星照常退避重连），不是谁的凭据有问题
            logger.log(logging.INFO if status == 503 else logging.WARNING,
                       "卫星连接被拒（%d %s）：id=%r peer=%s 带令牌=%s", status, why, sat_id,
                       peer, token is not None)
        await _deny(ws, status, why)
        return

    await ws.accept()
    proto_hdr = (ws.headers.get(P.HDR_PROTO, "") or "").strip()
    if proto_hdr != str(P.PROTO_VERSION):
        logger.warning("卫星 %s 协议版本头 %r 不是 %d —— 4406 关闭", sat_id, proto_hdr,
                       P.PROTO_VERSION)
        await _close(ws, P.CLOSE_PROTO_MISMATCH, f"proto {proto_hdr!r} != {P.PROTO_VERSION}")
        return

    # 第一条必须是 hello。
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=hub.hello_timeout_s)
    except asyncio.TimeoutError:
        await _close(ws, P.CLOSE_PROTOCOL, "no hello")
        return
    except Exception:  # noqa: BLE001
        return
    if first.get("type") == "websocket.disconnect":
        return
    text = first.get("text")
    if text is None:
        await _close(ws, P.CLOSE_UNSUPPORTED_DATA, "text frames only")
        return
    try:
        hello = P.parse_to_hub(text)
    except P.ProtocolError as exc:
        await _close(ws, P.CLOSE_PROTOCOL, exc.code)
        return
    if not isinstance(hello, P.Hello):
        await _close(ws, P.CLOSE_PROTOCOL, "first message must be hello")
        return
    if hello.proto != P.PROTO_VERSION:
        await _close(ws, P.CLOSE_PROTO_MISMATCH, f"proto {hello.proto} != {P.PROTO_VERSION}")
        return
    if hello.satellite_id != sat_id:
        await _close(ws, P.CLOSE_PROTOCOL, "satellite_id mismatch")
        return

    loop = asyncio.get_running_loop()
    transport = AsyncTransport(ws, loop, peer=peer)
    sender = asyncio.create_task(transport.run(), name=f"satellite-send-{sat_id}")
    try:
        session = hub.attach(sat_id, transport, hello)
    except SatelliteError as exc:
        transport.close(P.CLOSE_FORBIDDEN, str(exc)[:100])
        await _finish_sender(sender)
        return
    reason = "closed"
    try:
        while True:
            msg = await ws.receive()
            mtype = msg.get("type")
            if mtype == "websocket.disconnect":
                reason = f"disconnect code={msg.get('code')}"
                break
            text = msg.get("text")
            if text is None:
                transport.close(P.CLOSE_UNSUPPORTED_DATA, "text frames only")
                reason = "binary frame"
                break
            rx_ms = _now_ms()
            if P.frame_too_big(text):
                transport.close(P.CLOSE_TOO_BIG, "frame > 1 MiB")
                reason = "frame too big"
                break
            try:
                m = P.parse_to_hub(text)
            except P.ProtocolError as exc:
                hub.note_protocol_error(session, exc)
                continue
            if isinstance(m, (P.FileOffer, P.FileChunk, P.FileEnd)):
                await asyncio.to_thread(hub.on_file_message, session, m)
            else:
                hub.on_message(session, m, rx_ms)
            if transport.closed:
                reason = f"closed by hub ({transport.close_code} {transport.close_reason})"
                break
    except Exception as exc:  # noqa: BLE001 — 任何异常都只是这条连接结束
        reason = f"{type(exc).__name__}: {exc}"
    finally:
        hub.detach(session, reason)
        await _finish_sender(sender, transport)


async def _finish_sender(sender: asyncio.Task, transport: AsyncTransport | None = None) -> None:
    if transport is not None and not transport.closed:
        transport.close(P.CLOSE_NORMAL, "bye")
    try:
        await asyncio.wait_for(sender, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        sender.cancel()
    except Exception:  # noqa: BLE001
        pass


async def _close(ws: Any, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason[:120])
    except Exception:  # noqa: BLE001
        pass


__all__ = ["serve_satellite", "AsyncTransport"]
