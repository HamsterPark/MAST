"""实时数据通道 LiveHub（契约：docs/v2/design/ters_satellite_integration.md §7）。

所有新增的数据采集（光谱、偏振角、泵浦-探测曲线、TERS 图……）都从这里推到 MAST 页面实时绘图::

    from mast.live import get_live_hub
    live = get_live_hub()
    live.declare("local/pump_probe/run42", "xy", title="泵浦-探测", source="local",
                 x_label="延迟", x_unit="ps", y_label="信号", y_unit="V")
    live.publish("local/pump_probe/run42", {"x": 1.25, "values": {"lockin_x": 3.1e-6}})
    live.end("local/pump_probe/run42")

流名约定（契约 §7）：枢纽本机产生的流用 ``local/…``，卫星的流用 ``<sat_id>/<设备>/<通道>``，
系统级的流用 ``mast/…``。

测试里用 ``mast.live.testing`` 的 ``isolated_live_hub`` / ``LiveRecorder`` 断言发布了什么。
"""

from mast.live.hub import (
    DEFAULT_RETAIN,
    KINDS,
    LATEST_ONLY_KINDS,
    MAX_RETAIN,
    MAX_STREAMS,
    LiveError,
    LiveEvent,
    LiveHub,
    LiveSnapshot,
    get_live_hub,
)
from mast.live.subscription import LiveSubscription

__all__ = [
    "DEFAULT_RETAIN",
    "KINDS",
    "LATEST_ONLY_KINDS",
    "MAX_RETAIN",
    "MAX_STREAMS",
    "LiveError",
    "LiveEvent",
    "LiveHub",
    "LiveSnapshot",
    "LiveSubscription",
    "get_live_hub",
]
