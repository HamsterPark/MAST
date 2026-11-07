"""REST 响应的 Pydantic 模型（``/api/satellites*``、``/api/port-guard``）。

字段都是可选或带默认值的：卫星端加字段、端口守卫加检查项时，前端旧版本照样能解析。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class SatDevice(_Out):
    id: str
    kind: str = ""
    driver: str = ""
    model: str = ""
    serial: str = ""
    sim: bool = False
    state: str = ""                      # ready | busy | offline | error | not_started | operator_hold
    caps: list[str] = Field(default_factory=list)
    info: dict[str, Any] = Field(default_factory=dict)
    operator_hold: bool = False
    latest: Optional[dict[str, Any]] = None        # 最近一条 device_state 的 data
    latest_ts_ms: Optional[int] = None
    stale: bool = True                   # 卫星重启后（直到新 device_state 到来）或链路不是 connected 时为 True
    axis: Optional[dict[str, Any]] = None          # 最近一条 axis_state 的 data
    role: Optional[str] = None           # satellites.json 的 devices.<id>.role（raman_ccd 等）
    hub_config: dict[str, Any] = Field(default_factory=dict)   # 该设备的枢纽侧配置整段


class SatEstop(_Out):
    reason: str = ""
    sent_ms: Optional[int] = None
    ack: Optional[dict[str, Any]] = None


class SatSummary(_Out):
    id: str
    configured: bool = True
    enabled: bool = True
    note: str = ""
    state: str = "lost"                  # connected | degraded | lost
    ever_connected: bool = False
    boot_id: Optional[str] = None
    connected_since: Optional[int] = None
    disconnected_ms: Optional[int] = None
    last_seen_ms: Optional[int] = None
    peer: str = ""
    sw: str = ""
    host: str = ""
    proto: Optional[int] = None
    rtt_ms: Optional[float] = None
    clock_offset_ms: Optional[float] = None
    clock_delay_ms: Optional[float] = None
    clock_samples: int = 0
    evt_landed: int = 0
    pending_commands: int = 0
    counters: dict[str, int] = Field(default_factory=dict)
    last_estop: Optional[SatEstop] = None
    devices: list[SatDevice] = Field(default_factory=list)


class PendingSummary(_Out):
    id: str
    sat_id: str = ""
    device: str = ""
    action: str = ""
    state: str = ""
    ack: Optional[str] = None
    created_ms: Optional[int] = None
    timeout_s: Optional[float] = None
    done: bool = False
    ok: Optional[bool] = None
    error: Optional[str] = None


class SatDetail(SatSummary):
    pending: list[PendingSummary] = Field(default_factory=list)


class HubStatus(_Out):
    started: bool = False
    enabled: bool = True
    hub_boot_id: str = ""
    config_path: str = ""
    config_error: Optional[str] = None
    configured: int = 0
    connected: int = 0
    data_root: str = ""
    stm_ring: dict[str, Any] = Field(default_factory=dict)
    live: dict[str, Any] = Field(default_factory=dict)
    dispatch_dropped: int = 0


class SatellitesResponse(_Out):
    hub: HubStatus
    satellites: list[SatSummary] = Field(default_factory=list)
    degraded: bool = False


class SatDevicesResponse(_Out):
    sat_id: str
    state: str = "lost"
    devices: list[SatDevice] = Field(default_factory=list)


class SatEvent(_Out):
    seq: Optional[int] = None
    device: Optional[str] = None
    kind: Optional[str] = None
    ts_ms: Optional[int] = None
    ts_hub_ms: Optional[float] = None
    data: dict[str, Any] = Field(default_factory=dict)


class SatEventsResponse(_Out):
    sat_id: str
    events: list[SatEvent] = Field(default_factory=list)


class SatFile(_Out):
    file_id: str
    sat_id: str = ""
    name: Optional[str] = None
    rel_path: Optional[str] = None
    stored_rel: Optional[str] = None     # 相对数据根的 POSIX 路径
    meta_rel: Optional[str] = None
    size: int = 0
    sha256: Optional[str] = None
    kind: Optional[str] = None
    mtime_ms: Optional[int] = None
    received_ms: Optional[int] = None
    boot_id: Optional[str] = None
    acq_start_hub_ms: Optional[float] = None
    acq_end_hub_ms: Optional[float] = None
    spe_ok: Optional[bool] = None
    stm_matched: bool = False
    experiment_ingest: Optional[str] = None


class SatFilesResponse(_Out):
    files: list[SatFile] = Field(default_factory=list)
    count: int = 0


class PortFinding(_Out):
    kind: str                            # nanonis_tcp | nanonis_vi_server | serial_port | usb_device | vendor_gui
    resource: str
    severity: str                        # conflict | warn | info
    pid: Optional[int] = None
    process: str = ""
    exe: Optional[str] = None
    cmdline: Optional[str] = None
    remote: Optional[str] = None
    detail: str = ""
    allowed_by: Optional[str] = None


class PortGuardResponse(_Out):
    checked_ms: int = 0
    ok: bool = True                      # 没有阻断性冲突（或 enforce=false）
    enforce: bool = True
    conflicts: list[PortFinding] = Field(default_factory=list)
    warnings: list[PortFinding] = Field(default_factory=list)
    infos: list[PortFinding] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    nanonis: dict[str, Any] = Field(default_factory=dict)
    optics: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0
    complete: bool = True                # False：有检查没跑完整（原因在 errors）
    serial_handle_scan: bool = False     # port_guard.json 打开了跨进程句柄扫描
    message: str = ""
    degraded: bool = False


__all__ = ["SatDevice", "SatEstop", "SatSummary", "PendingSummary", "SatDetail", "HubStatus",
           "SatellitesResponse", "SatDevicesResponse", "SatEvent", "SatEventsResponse",
           "SatFile", "SatFilesResponse", "PortFinding", "PortGuardResponse"]
