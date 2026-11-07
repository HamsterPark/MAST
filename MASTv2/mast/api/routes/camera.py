"""对光相机的画面与光斑统计。只读，不碰设备。

* ``GET /api/camera/{camera_id}/frame.jpg?max_w=&q=&scale=&mode=`` —— 最新一帧 JPEG。
  ``mode=gray``（默认）灰度，可 ``max_w`` 缩宽；``mode=color`` 全分辨率彩色（忽略 ``max_w``）。
  没在直播回 404。前端按用户选的帧率轮询（上一张回来再要下一张）。图像不进 LiveHub；图像响应
  不走 GZip（:mod:`mast.api.compression`）。
* ``GET /api/camera/{camera_id}/stats`` —— ``{exposure_us, gain_db, fps, max, saturated_frac,
  centroid_px, fwhm_px, roi, frame_seq, t}`` 加 ``frame_w`` / ``frame_h`` 等（坐标约定见
  :mod:`mast.instruments.camera`）。没在直播时统计各项为 null；没有这台相机回 404。

开关相机、改曝光这些写操作都是技能（``POST /api/skills/{name}/execute``），这里没有。两个端点都在
访问闸门后面（LAN 模式要认证）。
"""

from __future__ import annotations

from mast.i18n import tr
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["camera"])

_NO_STORE = {"Cache-Control": "no-store"}


class CameraRoi(BaseModel):
    x: int
    y: int
    w: int
    h: int
    binning: int = 1


class CameraStats(BaseModel):
    """坐标单位是传感器像素，原点是 ROI 左上角像素的中心；画标记按 ``x / frame_w``。"""

    model_config = ConfigDict(extra="allow")

    camera_id: str
    state: str = Field(..., description="disconnected | connected | live | error")
    connected: bool
    live: bool
    error: Optional[str] = None
    exposure_us: Optional[int] = None
    gain_db: Optional[float] = None
    fps: Optional[float] = None
    roi: Optional[CameraRoi] = None
    max: Optional[float] = Field(None, description="原始像素的最大值（ADU）")
    saturated_frac: Optional[float] = Field(None, description="原始像素里达到满量程的比例")
    centroid_px: Optional[list[float]] = Field(None, description="[x, y]，ROI 内的传感器像素；没有光斑为 null")
    fwhm_px: Optional[list[float]] = Field(None, description="[x 向, y 向]，二阶矩 2.355σ，传感器像素")
    centroid_sensor_px: Optional[list[float]] = Field(None, description="[x, y]，传感器绝对坐标")
    frame_w: Optional[int] = Field(None, description="画面在传感器上跨的宽（像素）")
    frame_h: Optional[int] = Field(None, description="画面在传感器上跨的高（像素）")
    frame_seq: Optional[int] = None
    t: Optional[float] = Field(None, description="这一帧到达的时间（unix 秒）")
    settled: Optional[bool] = Field(None, description="false = 刚改过设置，这一帧可能还是旧设置拍的")


def _camera(camera_id: str) -> Any:
    from mast.instruments.camera import get_camera_manager

    try:
        return get_camera_manager().camera(camera_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0]) if exc.args else tr("没有这台相机", "No such camera")) from None


@router.get(
    "/camera/{camera_id}/frame.jpg",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}, "description": "最新一帧 JPEG（gray 或 color）"},
               404: {"description": "没有这台相机，或没在直播"}},
)
def camera_frame(
    camera_id: str,
    max_w: Optional[int] = Query(None, ge=16, le=4096, description="宽度上限（像素），超过就等比缩小；mode=color 时忽略"),
    q: int = Query(80, ge=10, le=95, description="JPEG 质量"),
    scale: Literal["full", "auto"] = Query(
        "full", description="full：0…满量程线性映射（亮度如实）；auto：按分位拉伸（暗光斑也看得见）"),
    mode: Literal["gray", "color"] = Query(
        "gray", description="gray：对焦灰度（可缩宽）；color：全分辨率彩色，不下采样"),
) -> Response:
    """对光相机的最新一帧。响应头 ``X-Frame-Seq`` / ``X-Frame-T`` 给帧号与时间（unix 秒）。"""
    got = _camera(camera_id).jpeg(max_w=max_w, quality=q, scale=scale, mode=mode)
    if got is None:
        raise HTTPException(status_code=404, detail=tr("相机没在直播", "The camera is not streaming"))
    data, frame = got
    return Response(content=data, media_type="image/jpeg",
                    headers={**_NO_STORE, "X-Frame-Seq": str(frame.seq), "X-Frame-T": f"{frame.t:.3f}",
                             "X-Frame-Mode": mode})


@router.get("/camera/{camera_id}/stats", response_model=CameraStats,
            responses={404: {"description": "没有这台相机"}})
def camera_stats(camera_id: str, response: Response) -> Any:
    """状态 + 最新一帧的光斑统计。"""
    from mast.skills.builtins.optics_common import json_safe

    response.headers.update(_NO_STORE)
    return json_safe(_camera(camera_id).stats())
