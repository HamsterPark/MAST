"""枢纽侧 Python API 的错误类型（契约 §6）。"""

from __future__ import annotations


class SatelliteError(Exception):
    """卫星通道的所有错误的基类。"""

    code = "internal"

    def __init__(self, message: str = "", *, sat_id: str = "", cmd_id: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.sat_id = sat_id
        self.cmd_id = cmd_id


class SatelliteUnavailable(SatelliteError):
    """卫星不存在、未连接或状态不是 connected —— 命令根本没有发出去。"""

    code = "unavailable"


class SatelliteCommandError(SatelliteError):
    """卫星回了 ``result.ok=false``（或拒收了命令、或判为 lost_on_restart）。"""

    def __init__(self, code: str, message: str = "", *, sat_id: str = "",
                 cmd_id: str = "", result: dict | None = None) -> None:
        super().__init__(f"{code}: {message}" if message else code, sat_id=sat_id,
                         cmd_id=cmd_id)
        self.code = code
        self.message = message
        self.result = result


class SatelliteTimeout(SatelliteError):
    """在约定时间内没有拿到结果。

    ``command_expired=True``：命令自己的时限到了，枢纽已对卫星发 ``cancel``，这条命令收尾。
    ``False``：只是这一次 ``wait(timeout_s)`` 等够了，命令可能仍在卫星上运行，可以再等或取消。
    """

    code = "timeout"

    def __init__(self, message: str = "", *, sat_id: str = "", cmd_id: str = "",
                 command_expired: bool = True) -> None:
        super().__init__(message, sat_id=sat_id, cmd_id=cmd_id)
        self.command_expired = command_expired


__all__ = ["SatelliteError", "SatelliteUnavailable", "SatelliteCommandError",
           "SatelliteTimeout"]
