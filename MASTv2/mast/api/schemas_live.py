"""实时数据流（LiveHub）的 REST 模型 —— 契约 docs/v2/design/ters_satellite_integration.md §7。

帧的形状由流的 ``kind`` 决定（帧里不重复带 kind）：

* spectrum ``{t, y, axis_ver, meta?}`` —— 横轴另发（``axis``），帧里只带版本号；
* scalar ``{t, values}`` / xy ``{t, x, values}`` / map2d ``{t, ix, iy, values}`` / status ``{t, text, level}``。

``t`` 是 unix 秒（枢纽时钟）。非有限的数（NaN / Inf）一律是 ``null``。

``WS /ws/live`` 的消息（服务端 → 客户端）不走 OpenAPI，见 ``mast/api/routes/live.py`` 模块说明。
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

LiveKind = Literal["spectrum", "scalar", "xy", "map2d", "status"]


class LiveStreamMeta(BaseModel):
    """一个流的描述。``uid`` 变了 = 流被重置（换了 kind 或地图尺寸），客户端该清掉旧画面。"""

    id: str
    kind: LiveKind
    title: str
    source: str
    x_label: Optional[str] = None
    x_unit: Optional[str] = None
    y_label: Optional[str] = None
    y_unit: Optional[str] = None
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="声明时给的描述。map2d 必有 nx, ny, x0, y0, dx, dy, x_unit, y_unit；"
                    "spectrum 可带 laser_line_nm（前端切 cm⁻¹ 用）")
    retain: int = Field(description="保留的帧数上限（map2d 是像素数）")
    uid: str
    created_t: float
    last_t: Optional[float] = None
    ended: bool = False
    ended_t: Optional[float] = None
    last_seq: int = Field(0, description="最近一帧的 seq；还没有帧时是 0（或流被淘汰重建前的值）")
    first_seq: Optional[int] = Field(None, description="保留区里最旧那帧的 seq")
    n_frames: int = 0
    axis_ver: int = Field(0, description="spectrum 当前横轴版本；0 = 还没设轴")
    bytes: int = Field(0, description="保留区占用（近似字节）")


class LiveStreamList(BaseModel):
    hub: str = Field(description="本进程的 LiveHub id；变了说明 MAST 重启过，客户端手里的 since 作废")
    items: list[LiveStreamMeta]


class LiveAxis(BaseModel):
    ver: int
    unit: str
    n: int
    x: list[float]


class LiveSpectrumFrame(BaseModel):
    t: float
    y: list[Optional[float]]
    axis_ver: int = 0
    meta: Optional[dict[str, Any]] = None


class LiveScalarFrame(BaseModel):
    t: float
    values: dict[str, Optional[float]]


class LiveXYFrame(BaseModel):
    t: float
    x: float
    values: dict[str, Optional[float]]


class LiveMap2DFrame(BaseModel):
    t: float
    ix: int
    iy: int
    values: dict[str, Optional[float]]


class LiveStatusFrame(BaseModel):
    t: float
    text: str
    level: Literal["debug", "info", "warn", "error"] = "info"


class LiveFrameItem(BaseModel):
    seq: int
    frame: Union[LiveSpectrumFrame, LiveScalarFrame, LiveXYFrame, LiveMap2DFrame,
                 LiveStatusFrame]


class LiveGap(BaseModel):
    """客户端缺的那一段 seq（已经不在保留区里了）。"""

    model_config = ConfigDict(populate_by_name=True)

    from_: int = Field(alias="from")
    to: int


class LiveStreamDetail(BaseModel):
    """``GET /api/live/streams/{stream_id}`` —— 轮询兜底与回看。"""

    hub: str
    meta: LiveStreamMeta
    axis: Optional[LiveAxis] = Field(None, description="当前横轴（spectrum）")
    axes: list[LiveAxis] = Field(default_factory=list,
                                 description="返回的帧引用到的其它（较旧的）横轴版本")
    frames: list[LiveFrameItem]
    more: bool = Field(False, description="被 limit 截断了；用最后一帧的 seq 当 since 接着取")
    dropped: Optional[LiveGap] = Field(
        None, description="since 之后有一段已经不在保留区里（只在 since > 0 时报告）")


__all__ = [
    "LiveAxis", "LiveFrameItem", "LiveGap", "LiveKind", "LiveMap2DFrame", "LiveScalarFrame",
    "LiveSpectrumFrame", "LiveStatusFrame", "LiveStreamDetail", "LiveStreamList",
    "LiveStreamMeta", "LiveXYFrame",
]
