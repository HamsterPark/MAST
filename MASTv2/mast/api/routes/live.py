"""实时数据流 —— ``WS /ws/live`` 推送 + ``GET /api/live/streams[/…]`` 轮询兜底。

数据来自 :mod:`mast.live`（卫星光谱、偏振角、泵浦-探测曲线、TERS 图……）。认证与其余 API 相同：
``api/app.py`` 的 ASGI 中间件对 http 和 websocket 都生效。

WS 协议
-------
连接：``/ws/live``，可选 ``?max_hz=``（每条流每秒最多推几次，默认 20，范围 0.5–60）。

客户端 → 服务端::

    {"op":"subscribe","streams":["*"] 或 ["sat1/ccd/spectrum", …],"since":{"<流 id>": seq, "*": seq}}
    {"op":"unsubscribe","streams":["*"] 或 […]}

``since`` 是可选的，按流给（``"*"`` 是没单独给的流的缺省值）：

* 不给：spectrum 只重放最新一帧，其余 kind 重放保留区里的全部（曲线 / 地图没有历史画不出来）；
* ``-1``：只要最新一帧；
* ``n ≥ 0``：重放 ``seq > n`` 的帧；``n > 0`` 且中间有一段已经不在保留区里，先发 ``dropped``。

``"*"`` 订阅之后新声明的流自动订上。订阅还没声明的流 id 也可以：声明后帧就会推过来。

服务端 → 客户端（每条一个 JSON 文本消息）::

    {"kind":"streams","hub":"<id>","items":[<LiveStreamMeta>…]}  连上时、以及流声明/结束/淘汰时；
                                                                    每次都是**完整**目录，整表替换
    {"kind":"axis","stream":id,"ver":n,"x":[…],"unit":"nm"}        横轴（只发客户端还没有的版本）
    {"kind":"frame","stream":id,"seq":n,"frame":{…}}               单帧
    {"kind":"frames","stream":id,"items":[{"seq":n,"frame":{…}},…]} 合批（增量流、重放）
    {"kind":"dropped","stream":id,"from":a,"to":b}                  这段没送到（积压超限 / 不在保留区）
    {"kind":"ping"}                                                  15 s 没别的消息时
    {"kind":"error","op":…,"message":…}                             客户端消息格式不对（连接照常）

顺序保证：同一个流内 dropped → axis → 帧，seq 升序、不重复。spectrum / scalar 每拍只推最新一帧
（中间被覆盖的不算丢，不发 dropped）；xy / map2d / status 不丢，只合批。``hub`` 变了说明 MAST
重启过：丢掉手里的 since，从头订。未知的 ``kind`` 一律忽略（以后会加新的）。
"""

from __future__ import annotations

from mast.i18n import tr
import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Query, WebSocket
from fastapi.responses import Response

from mast.api.schemas_live import LiveStreamDetail, LiveStreamList
from mast.live import LiveSubscription, get_live_hub

logger = logging.getLogger(__name__)

router = APIRouter(tags=["live"])        # 挂在 /api 下
ws_router = APIRouter(tags=["live"])     # 全路径 /ws/live，不加前缀

_PING_S = 15.0
_DEFAULT_HZ = 20.0
_MIN_HZ, _MAX_HZ = 0.5, 60.0


@router.get("/live/streams", response_model=LiveStreamList)
def list_live_streams() -> LiveStreamList:
    """所有实时流的目录，含每个流的最新 seq。"""
    hub = get_live_hub()
    return LiveStreamList(hub=hub.hub_id, items=hub.streams())


@router.get("/live/streams/{stream_id:path}", response_model=LiveStreamDetail,
            responses={404: {"description": "没有这个流"}})
def get_live_stream(
    stream_id: str,
    since: int | None = Query(None, ge=-1, description="缺省：spectrum 只给最新一帧，其余全给；"
                                                      "-1：只要最新一帧；n：seq > n 的帧"),
    limit: int = Query(500, ge=1, le=5000, description="最多返回多少帧（按 seq 升序，超了 more=true）"),
) -> Response:
    """一个流的描述、横轴与帧 —— WS 不通时的轮询兜底，也用于回看。"""
    snap = get_live_hub().snapshot(stream_id, since=since, limit=limit)
    if snap is None:
        raise HTTPException(status_code=404, detail=tr(f"没有这个实时流：{stream_id}", f"No such live stream: {stream_id}"))
    # 帧在发布时就序列化好了：直接拼，不把几百条光谱解析回 dict 再过一遍校验。
    return Response(content=snap.to_json(), media_type="application/json")


def _parse_hz(raw: str | None) -> float:
    try:
        hz = float(raw) if raw else _DEFAULT_HZ
    except ValueError:
        return _DEFAULT_HZ
    if hz != hz:  # NaN
        return _DEFAULT_HZ
    return min(_MAX_HZ, max(_MIN_HZ, hz))


@ws_router.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()

    def _wake() -> None:
        # 在发布线程上被调用（持着 hub 的锁）：只把事件循环叫醒，别的什么都不做。
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass                      # 事件循环已经在关

    interval = 1.0 / _parse_hz(ws.query_params.get("max_hz"))
    sub = LiveSubscription(get_live_hub(), wake=_wake)
    wake.set()                        # 连上先发一份目录

    async def _receive() -> None:
        while True:
            try:
                msg = await ws.receive()
            except Exception as exc:  # noqa: BLE001 — 连接已经坏了：当作断开
                logger.debug("ws_live receive ended: %r", exc)
                return
            if msg.get("type") == "websocket.disconnect":
                return
            text = msg.get("text")
            if text is not None:      # 二进制帧不在契约里，忽略
                sub.request_text(text)

    async def _send() -> None:
        last = 0.0
        try:
            while True:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=_PING_S)
                except asyncio.TimeoutError:
                    await ws.send_text('{"kind":"ping"}')
                    continue
                # 按节拍限速：离上次发送不足一个间隔就等满，期间到的帧并进同一批。
                gap = interval - (time.monotonic() - last)
                if gap > 0:
                    await asyncio.sleep(gap)
                wake.clear()          # 先清再取：取的过程中到的新帧会再叫醒一次
                # 拼大批量的消息文本（整幅地图的重放）放到线程里，不卡事件循环。
                msgs = await asyncio.to_thread(sub.drain)
                last = time.monotonic()
                for m in msgs:
                    await ws.send_text(m)
        except Exception as exc:  # noqa: BLE001 — 发不出去（客户端走了）或意外：关连接，让接收端收尾
            logger.debug("ws_live sender ended: %r", exc)
            try:
                await ws.close(code=1011)
            except Exception:  # noqa: BLE001
                pass

    # 接收在本任务里跑（客户端一断它就返回），发送单开一个任务。收尾只取消、不等待：
    # 被取消时在 finally 里 await 会和外层的取消搅在一起（TestClient 的 anyio cancel scope 会因此
    # 漏出 CancelledError）；发送任务下一次 await 就会退出。
    sender = asyncio.create_task(_send())
    try:
        await _receive()
    finally:
        sender.cancel()
        sub.close()


__all__ = ["router", "ws_router"]
