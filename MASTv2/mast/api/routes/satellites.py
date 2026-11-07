"""卫星节点（光谱电脑等）的只读 REST + WebSocket 端点。

- ``WS  /ws/satellite/v1``：卫星的控制通道（契约 §5）。**自带令牌认证**：accept 之前验
  ``Authorization: Bearer``，操作员的 Basic 认证中间件对 ``/ws/satellite/`` 放行。实现在
  ``mast.satellite.endpoint``。
- ``GET /api/satellites``：枢纽状态 + 每颗卫星的链路、对时、设备。
- ``GET /api/satellites/files``：已入库的卫星文件（新的在前）。
- ``GET /api/satellites/{sat_id}``、``/devices``、``/events``：单颗卫星的细节、设备、最近事件摘要。
- ``GET /api/port-guard``：端口归属检查结果（``?refresh=true`` 现查一次）。

这里**没有写端点**：控制按钮走 ``POST /api/skills/{name}/execute``（完整保留安全闸门，
契约 §13），不另开直连驱动的口子。

路由全部用完整路径、注册时不加前缀（与 ``api/ws.py`` 相同）。字面量段 ``/satellites/files``
声明在 ``/satellites/{sat_id}`` 之前 —— 以免路由遮蔽。``files`` 也是保留 id。
"""

from __future__ import annotations

import logging
from mast.i18n import tr

from fastapi import APIRouter, HTTPException, Query, WebSocket

from mast.satellite.schemas import (
    HubStatus,
    PortGuardResponse,
    SatDetail,
    SatDevicesResponse,
    SatellitesResponse,
    SatEventsResponse,
    SatFilesResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["satellites"])


def _hub():
    from mast.satellite.hub import get_satellite_hub

    return get_satellite_hub()


@router.websocket("/ws/satellite/v1")
async def ws_satellite(ws: WebSocket) -> None:
    from mast.satellite.endpoint import serve_satellite

    await serve_satellite(ws)


@router.get("/api/satellites", response_model=SatellitesResponse)
def list_satellites() -> SatellitesResponse:
    try:
        hub = _hub()
        return SatellitesResponse(hub=HubStatus(**hub.status()), satellites=hub.list_satellites())
    except Exception as exc:  # noqa: BLE001 — 状态页永远不 500
        logger.warning("GET /api/satellites degraded: %s", exc, exc_info=True)
        return SatellitesResponse(hub=HubStatus(), satellites=[], degraded=True)


@router.get("/api/satellites/files", response_model=SatFilesResponse)
def list_satellite_files(sat_id: str | None = Query(default=None, max_length=64),
                         limit: int = Query(default=100, ge=1, le=1000),
                         since_ms: int | None = Query(default=None, ge=0)) -> SatFilesResponse:
    try:
        rows = _hub().files().list(sat_id, limit=limit, since_ms=since_ms)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GET /api/satellites/files failed: %s", exc)
        rows = []
    return SatFilesResponse(files=rows, count=len(rows))


@router.get("/api/satellites/{sat_id}", response_model=SatDetail)
def get_satellite(sat_id: str) -> SatDetail:
    view = _hub().satellite(sat_id)
    if view is None:
        raise HTTPException(status_code=404, detail=tr(f"没有这颗卫星：{sat_id}", f"No such satellite: {sat_id}"))
    return SatDetail(**view)


@router.get("/api/satellites/{sat_id}/devices", response_model=SatDevicesResponse)
def get_satellite_devices(sat_id: str) -> SatDevicesResponse:
    view = _hub().satellite(sat_id)
    if view is None:
        raise HTTPException(status_code=404, detail=tr(f"没有这颗卫星：{sat_id}", f"No such satellite: {sat_id}"))
    return SatDevicesResponse(sat_id=sat_id, state=view.get("state", "lost"),
                              devices=view.get("devices") or [])


@router.get("/api/satellites/{sat_id}/events", response_model=SatEventsResponse)
def get_satellite_events(sat_id: str,
                         limit: int = Query(default=100, ge=1, le=500),
                         kind: str | None = Query(default=None, max_length=64),
                         device: str | None = Query(default=None, max_length=64)) -> SatEventsResponse:
    hub = _hub()
    if hub.satellite(sat_id) is None:
        raise HTTPException(status_code=404, detail=tr(f"没有这颗卫星：{sat_id}", f"No such satellite: {sat_id}"))
    return SatEventsResponse(sat_id=sat_id,
                             events=hub.recent_events(sat_id, limit=limit, kind=kind, device=device))


@router.get("/api/port-guard", response_model=PortGuardResponse)
def port_guard(refresh: bool = Query(default=False)) -> PortGuardResponse:
    try:
        from mast.core.port_guard import check, last_report

        rep = check(refresh=True) if refresh else (last_report() or check())
        return PortGuardResponse(**rep.to_dict())
    except Exception as exc:  # noqa: BLE001
        logger.warning("GET /api/port-guard degraded: %s", exc, exc_info=True)
        return PortGuardResponse(ok=True, degraded=True, errors=[f"{type(exc).__name__}: {exc}"],
                                 message=tr("端口归属检查没跑成（见 errors）", "Port ownership check did not complete (see errors)"))


__all__ = ["router"]
