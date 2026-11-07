"""MAST 枢纽侧的卫星接入（docs/v2/design/ters_satellite_integration.md §5、§6、§10、§11）。

光谱电脑（卫星）上的卫星进程主动外连 MAST 的 ``/ws/satellite/v1``，MAST 通过这里的
:class:`SatelliteHub` 代管卫星上的设备（光谱仪、偏振旋转台、Picomotor、XYZ、文件监视）。

公开 API（契约 §6）::

    from mast.satellite import (get_satellite_hub, SatelliteError, SatelliteUnavailable,
                                SatelliteCommandError, SatelliteTimeout)

这里用惰性导出：``python -m mast.satellite.admin`` 只该拉起标准库，不该把 pydantic /
numpy / 枢纽线程一起拖进来。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "get_satellite_hub", "peek_satellite_hub", "reset_satellite_hub", "SatelliteHub",
    "PendingCommand", "SatelliteError", "SatelliteUnavailable", "SatelliteCommandError",
    "SatelliteTimeout", "PROTO_VERSION", "WS_PATH",
]

_LAZY = {
    "get_satellite_hub": ("mast.satellite.hub", "get_satellite_hub"),
    "peek_satellite_hub": ("mast.satellite.hub", "peek_satellite_hub"),
    "reset_satellite_hub": ("mast.satellite.hub", "reset_satellite_hub"),
    "SatelliteHub": ("mast.satellite.hub", "SatelliteHub"),
    "PendingCommand": ("mast.satellite.hub", "PendingCommand"),
    "SatelliteError": ("mast.satellite.errors", "SatelliteError"),
    "SatelliteUnavailable": ("mast.satellite.errors", "SatelliteUnavailable"),
    "SatelliteCommandError": ("mast.satellite.errors", "SatelliteCommandError"),
    "SatelliteTimeout": ("mast.satellite.errors", "SatelliteTimeout"),
    "PROTO_VERSION": ("mast.satellite.protocol", "PROTO_VERSION"),
    "WS_PATH": ("mast.satellite.protocol", "WS_PATH"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'mast.satellite' has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


if TYPE_CHECKING:  # pragma: no cover
    from mast.satellite.errors import (  # noqa: F401
        SatelliteCommandError,
        SatelliteError,
        SatelliteTimeout,
        SatelliteUnavailable,
    )
    from mast.satellite.hub import (  # noqa: F401
        PendingCommand,
        SatelliteHub,
        get_satellite_hub,
        peek_satellite_hub,
        reset_satellite_hub,
    )
    from mast.satellite.protocol import PROTO_VERSION, WS_PATH  # noqa: F401
