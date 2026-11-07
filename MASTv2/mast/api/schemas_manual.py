"""``GET /api/manual/devices`` 的响应模型：手动控制面板的设备描述（契约 §13.1）。

面板上的每个按钮都是一个 :class:`ManualAction`，背后是一个已注册的技能，按钮只走
``POST /api/skills/{name}/execute``。``params`` 的名字、类型、单位、范围由端点按技能注册的
ParameterSpec 逐字填写；``safety`` 取技能生效的 safety_level，``bypass_lock`` 只给免令牌的
stop 类技能 —— 提供者填的这几项只是初值，以技能为准。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

ManualKind = Literal["linear_stage", "rotation_stage", "openloop_stepper", "xyz_stage",
                     "nanonis_motor_group", "camera", "spectrometer", "aotf"]


class ManualParamSpec(BaseModel):
    """技能 ParameterSpec 的一份拷贝（端点按技能注册的定义逐字覆盖）。"""

    name: str
    type: str = "str"                    # float | int | str | bool
    unit: str = ""
    required: bool = False
    default: Any = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    allowed_values: Optional[list[Any]] = None
    description: str = ""


class ManualAction(BaseModel):
    id: str = Field(..., description="jog / move_abs / move_rel / home / stop / set_velocity / "
                                     "identify / set_zero / abort / camera_connect / … ")
    label: str
    skill: str = Field(..., description="已注册的技能名；按钮执行 POST /api/skills/{skill}/execute")
    fixed: dict[str, Any] = Field(default_factory=dict,
                                  description="固定传给技能的参数（设备 id、relative 之类）")
    params: list[ManualParamSpec] = Field(default_factory=list,
                                          description="让人填的参数，是技能参数的子集")
    per_axis: bool = Field(False, description="为真时前端对 axes 的每一项各画一个按钮，轴名填进技能的 axis 参数")
    direction: Literal["none", "signed"] = "none"
    direction_param: Optional[str] = Field(None, description="direction=signed 时取正负号的那个参数")
    safety: Literal["auto", "confirm", "dangerous"] = "confirm"
    bypass_lock: bool = Field(False, description="免仪器令牌（stop 类，instrument_lock.BYPASS_NAMES）")
    hold_repeat_ms: Optional[int] = Field(
        None, description="非空才允许按住连续点动：上一次返回之后、且距上一次至少这么久才发下一次")
    stop_on_release: bool = Field(False, description="按住点动松开后再补发一次这台设备的 stop")
    hint: Optional[str] = Field(None, description="一句话：这个按钮实际做什么（厂家调用的真实效果）")


class ManualAxis(BaseModel):
    name: str
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    position: Optional[float] = None
    moving: Optional[bool] = None
    homed: Optional[bool] = None
    extra: Optional[dict[str, Any]] = None


class ManualReadout(BaseModel):
    """``{kind:"live", stream}`` 或 ``{kind:"poll", interval_s}``。

    读数不走技能执行入口（每次执行都要入账）：卫星设备看 LiveHub 的流；本机台子按
    ``interval_s`` 轮询 ``GET /api/manual/readouts?ids=…``；相机轮询 ``/api/camera/{id}/stats``；
    Nanonis 光学组没有位置反馈，``readout`` 为 null。提供者多给的字段（旧的 skill / params）不往外发。
    """

    kind: Literal["live", "poll"]
    stream: Optional[str] = None
    interval_s: Optional[float] = None


class ManualDevice(BaseModel):
    id: str = Field(..., description="同流名：local/stage/<id>、local/nanonis_group/<g>、"
                                     "local/camera/<id>、<satellite_id>/<device_id>")
    machine: str = Field(..., description="local = MAST 所在这台；卫星设备填卫星 id")
    kind: ManualKind
    label: str
    vendor: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    available: bool = True
    unavailable_reason: Optional[str] = None
    occupied_by: Optional[str] = Field(
        None, description="只填有证据的占用方；null 表示不知道，不等于空闲")
    tip_requirement: Optional[str] = Field(None, description="tip_withdrawn | z_feedback_on | none；null = 没有配置")
    axes: list[ManualAxis] = Field(default_factory=list)
    actions: list[ManualAction] = Field(default_factory=list)
    readout: Optional[ManualReadout] = None


class ManualDevicesResponse(BaseModel):
    devices: list[ManualDevice] = Field(default_factory=list)
    degraded: bool = Field(False, description="有一类设备读不到（见 degraded_sources / notes）")
    degraded_sources: list[Literal["local", "camera", "satellite"]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ManualReadoutAxis(BaseModel):
    name: str
    position: Optional[float] = None
    moving: Optional[bool] = None
    homed: Optional[bool] = None
    extra: Optional[dict[str, Any]] = None


class ManualReadoutValue(BaseModel):
    t: Optional[float] = Field(None, description="读数时刻（unix 秒）")
    axes: list[ManualReadoutAxis] = Field(default_factory=list)
    error: Optional[str] = None


class ManualReadoutsResponse(BaseModel):
    """``GET /api/manual/readouts?ids=a,b``：本机台子、Nanonis 光学组（``local/…``）与卫星设备
    （枢纽缓存）的批量只读读数。相机与认不出的 id 各回一条 error。"""

    readouts: dict[str, ManualReadoutValue] = Field(default_factory=dict)
