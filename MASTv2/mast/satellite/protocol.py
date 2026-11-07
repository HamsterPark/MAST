"""卫星协议 v1 的消息模型与校验（契约：docs/v2/design/ters_satellite_integration.md §5）。

枢纽（``mast.satellite.hub``）和 Python 参考卫星（``mast.satellite.sim``）共用这一份定义，
C# 卫星按它对照实现。这里只有**消息形状**；语义（幂等、重启识别、活性、对时、死人开关）
写在 hub 与 sim 里。

宽容与严格的分界
----------------
- 未知字段：保留、不报错（以后加字段不必两端同时升级）。
- 未知 ``t``：解析成 :class:`UnknownMessage`，由调用方记一条日志后忽略，不断线。
- 影响安全判断的**闭集**一律严格校验：``ack.state``、``cmd_status_reply`` 里的 ``state``。
  其余枚举（设备 kind/state、错误码、事件 kind）只校验类型：卫星端加新值时，枢纽照收不误，
  由使用方按已知值分支。
"""

from __future__ import annotations

import base64
import json
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

PROTO_VERSION = 1
WS_PATH = "/ws/satellite/v1"
#: 单帧上限（契约 §5.1）。按 UTF-8 字节数计。
MAX_FRAME_BYTES = 1 << 20

HDR_AUTH = "authorization"
HDR_ID = "x-mast-satellite-id"
HDR_PROTO = "x-mast-satellite-proto"

# ── 关闭码（accept 之后用；accept 之前一律回 HTTP 401/403） ──────────────────
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001          # 枢纽关停
CLOSE_UNSUPPORTED_DATA = 1003    # 收到二进制帧
CLOSE_TOO_BIG = 1009             # 超过 MAX_FRAME_BYTES
CLOSE_PROTOCOL = 4400            # 首条不是 hello、hello 字段不对、重复 hello 等
CLOSE_FORBIDDEN = 4403           # 连接期间被禁用（卫星停用 / 枢纽总开关关闭）
CLOSE_PROTO_MISMATCH = 4406      # 协议版本不符（契约 §5.1）
CLOSE_REPLACED = 4409            # 同一卫星的新连接顶替了这条
CLOSE_LIVENESS = 4408            # 超过 lost 阈值没有 pong，枢纽主动断开

# ── 闭集 ────────────────────────────────────────────────────────────────────
ERROR_CODES = frozenset({
    "invalid_args", "not_supported", "unavailable", "device_busy", "operator_hold",
    "limit", "timeout", "cancelled", "lost_on_restart", "internal",
})
ACK_STATES = ("accepted", "duplicate_running", "duplicate_done", "rejected")
CMD_STATUS_STATES = ("unknown", "running", "done", "lost_on_restart")
DEVICE_STATES = frozenset({"ready", "busy", "offline", "error", "not_started", "operator_hold"})
DEVICE_KINDS = frozenset({
    "spectrometer", "rotation_stage", "openloop_stepper", "xyz_stage", "aotf", "pmt",
    "file_watcher",
})
EVENT_KINDS = frozenset({
    "device_state", "axis_state", "acq_started", "spectrum", "wavelength_axis",
    "acq_progress", "acq_completed", "file_seen", "manual_activity", "deadman", "error",
})
FILE_KINDS = frozenset({"spe", "csv", "lfe", "other"})
#: 每种设备都必须实现的动作（契约 §5.4）。
REQUIRED_ACTIONS = ("status", "stop", "safe")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ProtocolError(ValueError):
    """一条消息不合协议。``code`` 是给日志与关闭原因用的短标签。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _Msg(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=False)


# ── 共用子结构 ──────────────────────────────────────────────────────────────

class DeviceDescriptor(_Msg):
    id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=64)
    driver: str = ""
    model: str = ""
    serial: str = ""
    sim: bool = False
    state: str = "not_started"
    caps: list[str] = Field(default_factory=list)
    info: dict[str, Any] = Field(default_factory=dict)


class ErrorBody(_Msg):
    code: str = Field(min_length=1, max_length=64)
    message: str = ""


class CmdStatusItem(_Msg):
    id: str = Field(min_length=1, max_length=128)
    state: Literal["unknown", "running", "done", "lost_on_restart"]
    result: dict[str, Any] | None = None


class EstopError(_Msg):
    device: str = ""
    message: str = ""


class WelcomeConfig(_Msg):
    heartbeat_s: float = 1.0
    deadman_s: float = 5.0
    max_upload_bytes_per_s: int = 1 << 20


# ── 卫星 → 枢纽 ─────────────────────────────────────────────────────────────

class Hello(_Msg):
    t: Literal["hello"]
    proto: int
    satellite_id: str = Field(min_length=1, max_length=64)
    boot_id: str = Field(min_length=1, max_length=128)
    sw: str = ""
    host: str = ""
    time_ms: int
    devices: list[DeviceDescriptor] = Field(default_factory=list)
    evt_seq: int = Field(default=0, ge=0)


class Evt(_Msg):
    t: Literal["evt"]
    seq: int = Field(ge=1)
    device: str = ""
    kind: str = Field(min_length=1, max_length=64)
    ts_ms: int
    data: dict[str, Any] = Field(default_factory=dict)


class Ack(_Msg):
    t: Literal["ack"]
    id: str = Field(min_length=1, max_length=128)
    state: Literal["accepted", "duplicate_running", "duplicate_done", "rejected"]
    reason: str | None = None


class Result(_Msg):
    t: Literal["result"]
    id: str = Field(min_length=1, max_length=128)
    ok: bool
    value: Any = None
    error: ErrorBody | None = None
    started_ms: int | None = None
    finished_ms: int | None = None


class Pong(_Msg):
    t: Literal["pong"]
    n: int
    t0: int
    t1: int
    t2: int


class FileOffer(_Msg):
    t: Literal["file_offer"]
    file_id: str
    name: str = Field(min_length=1, max_length=255)
    rel_path: str = Field(default="", max_length=1024)
    size: int = Field(ge=0)
    sha256: str
    mtime_ms: int = 0
    kind: str = "other"
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("file_id", "sha256")
    @classmethod
    def _hex64(cls, v: str) -> str:
        v = str(v or "").strip().lower()
        if not _HEX64.match(v):
            raise ValueError("必须是 64 位小写 hex（sha256）")
        return v


class FileChunk(_Msg):
    t: Literal["file_chunk"]
    file_id: str = Field(min_length=64, max_length=64)
    offset: int = Field(ge=0)
    data_b64: str

    def decoded(self) -> bytes:
        try:
            return base64.b64decode(self.data_b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError("bad_chunk", f"data_b64 不是合法 base64：{exc}") from exc


class FileEnd(_Msg):
    t: Literal["file_end"]
    file_id: str = Field(min_length=64, max_length=64)


class CmdStatusReply(_Msg):
    t: Literal["cmd_status_reply"]
    items: list[CmdStatusItem] = Field(default_factory=list)


class EstopAck(_Msg):
    t: Literal["estop_ack"]
    stopped: list[str] = Field(default_factory=list)
    errors: list[EstopError] = Field(default_factory=list)


class Log(_Msg):
    t: Literal["log"]
    level: str = "info"
    msg: str = ""
    ts_ms: int | None = None


# ── 枢纽 → 卫星 ─────────────────────────────────────────────────────────────

class Welcome(_Msg):
    t: Literal["welcome"]
    proto: int
    hub_boot_id: str
    session: str
    time_ms: int
    evt_ack: int = Field(ge=0)
    config: WelcomeConfig = Field(default_factory=WelcomeConfig)


class Cmd(_Msg):
    t: Literal["cmd"]
    id: str = Field(min_length=1, max_length=128)
    device: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(default=30.0, gt=0)


class Cancel(_Msg):
    t: Literal["cancel"]
    id: str = Field(min_length=1, max_length=128)


class Ping(_Msg):
    t: Literal["ping"]
    n: int
    t0: int


class EvtAckMsg(_Msg):
    t: Literal["evt_ack"]
    upto: int = Field(ge=0)


class FileWant(_Msg):
    t: Literal["file_want"]
    file_id: str
    offset: int = Field(ge=0)


class FileDone(_Msg):
    t: Literal["file_done"]
    file_id: str
    ok: bool
    stored: str | None = None
    error: str | None = None


class CmdStatus(_Msg):
    t: Literal["cmd_status"]
    ids: list[str] = Field(default_factory=list)


class Estop(_Msg):
    t: Literal["estop"]
    reason: str = ""


class UnknownMessage(BaseModel):
    """``t`` 不认识的消息。调用方记日志后忽略。"""

    model_config = ConfigDict(extra="allow")
    t: str


ToHub = Annotated[
    Union[Hello, Evt, Ack, Result, Pong, FileOffer, FileChunk, FileEnd, CmdStatusReply,
          EstopAck, Log],
    Field(discriminator="t"),
]
ToSatellite = Annotated[
    Union[Welcome, Cmd, Cancel, Ping, EvtAckMsg, FileWant, FileDone, CmdStatus, Estop],
    Field(discriminator="t"),
]
_TO_HUB = TypeAdapter(ToHub)
_TO_SAT = TypeAdapter(ToSatellite)
TO_HUB_TYPES = frozenset({"hello", "evt", "ack", "result", "pong", "file_offer", "file_chunk",
                          "file_end", "cmd_status_reply", "estop_ack", "log"})
TO_SAT_TYPES = frozenset({"welcome", "cmd", "cancel", "ping", "evt_ack", "file_want",
                          "file_done", "cmd_status", "estop"})


def _load(raw: str | bytes | dict) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_FRAME_BYTES:
            raise ProtocolError("too_big", f"帧大小 {len(raw)} 字节超过上限 {MAX_FRAME_BYTES}")
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("bad_utf8", str(exc)) from exc
    if frame_too_big(raw):
        raise ProtocolError("too_big", f"帧超过上限 {MAX_FRAME_BYTES} 字节")
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("bad_json", str(exc)) from exc
    if not isinstance(obj, dict):
        raise ProtocolError("not_object", "消息必须是 JSON 对象")
    return obj


def frame_too_big(text: str) -> bool:
    """按 UTF-8 字节数判断是否超过单帧上限，不必每次都整体编码。"""
    n = len(text)
    if n > MAX_FRAME_BYTES:
        return True
    if n * 4 <= MAX_FRAME_BYTES:
        return False
    return len(text.encode("utf-8")) > MAX_FRAME_BYTES


def _parse(raw, adapter: TypeAdapter, known: frozenset[str]):
    obj = _load(raw)
    t = obj.get("t")
    if not isinstance(t, str) or not t:
        raise ProtocolError("no_type", "缺少字段 t")
    if t not in known:
        return UnknownMessage.model_validate(obj)
    try:
        return adapter.validate_python(obj)
    except ValidationError as exc:
        errs = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}" for e in exc.errors()[:5])
        raise ProtocolError("invalid", f"{t}: {errs}") from exc


def parse_to_hub(raw: str | bytes | dict):
    """解析一条卫星发给枢纽的消息。非法 → :class:`ProtocolError`；未知 ``t`` → UnknownMessage。"""
    return _parse(raw, _TO_HUB, TO_HUB_TYPES)


def parse_to_satellite(raw: str | bytes | dict):
    """解析一条枢纽发给卫星的消息（参考卫星和测试用）。"""
    return _parse(raw, _TO_SAT, TO_SAT_TYPES)


def encode(msg: BaseModel | dict) -> str:
    """紧凑 JSON（一帧一条）。"""
    if isinstance(msg, BaseModel):
        obj = msg.model_dump(mode="json", exclude_none=False)
    else:
        obj = msg
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def error_code(code: str | None) -> str:
    """把卫星给的错误码规范到已知集合；不认识的原样保留（小写、截断），不发明新含义。"""
    c = str(code or "").strip().lower()[:64]
    return c or "internal"


# ── 数组编码（spectrum / wavelength_axis） ──────────────────────────────────

def encode_array(values, dtype: str) -> str:
    """把一维数组编成小端 base64。``dtype`` ∈ ``u16`` ``f32`` ``f64``。"""
    import numpy as np

    np_dtype = {"u16": "<u2", "f32": "<f4", "f64": "<f8"}[dtype]
    arr = np.asarray(values, dtype=np_dtype)
    return base64.b64encode(arr.tobytes()).decode("ascii")


def decode_array(data_b64: str, dtype: str, n: int | None = None):
    """解出小端数组（numpy）。长度与 ``n`` 不符时抛 :class:`ProtocolError`。"""
    import numpy as np

    np_dtype = {"u16": "<u2", "f32": "<f4", "f64": "<f8"}.get(str(dtype))
    if np_dtype is None:
        raise ProtocolError("bad_dtype", f"不认识的 dtype {dtype!r}")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ProtocolError("bad_b64", str(exc)) from exc
    width = np.dtype(np_dtype).itemsize
    if len(raw) % width:
        raise ProtocolError("bad_length", f"{len(raw)} 字节不是 {dtype} 的整数倍")
    arr = np.frombuffer(raw, dtype=np_dtype)
    if n is not None and int(n) != arr.size:
        raise ProtocolError("bad_length", f"声明 n={n}，实际 {arr.size} 个")
    return arr


__all__ = [
    "PROTO_VERSION", "WS_PATH", "MAX_FRAME_BYTES", "HDR_AUTH", "HDR_ID", "HDR_PROTO",
    "CLOSE_NORMAL", "CLOSE_GOING_AWAY", "CLOSE_UNSUPPORTED_DATA", "CLOSE_TOO_BIG",
    "CLOSE_PROTOCOL", "CLOSE_FORBIDDEN", "CLOSE_PROTO_MISMATCH", "CLOSE_REPLACED",
    "CLOSE_LIVENESS",
    "ERROR_CODES", "ACK_STATES", "CMD_STATUS_STATES", "DEVICE_STATES", "DEVICE_KINDS",
    "EVENT_KINDS", "FILE_KINDS", "REQUIRED_ACTIONS",
    "ProtocolError", "DeviceDescriptor", "ErrorBody", "CmdStatusItem", "EstopError",
    "WelcomeConfig", "Hello", "Evt", "Ack", "Result", "Pong", "FileOffer", "FileChunk",
    "FileEnd", "CmdStatusReply", "EstopAck", "Log", "Welcome", "Cmd", "Cancel", "Ping",
    "EvtAckMsg", "FileWant", "FileDone", "CmdStatus", "Estop", "UnknownMessage",
    "parse_to_hub", "parse_to_satellite", "encode", "frame_too_big", "error_code",
    "encode_array", "decode_array",
]
