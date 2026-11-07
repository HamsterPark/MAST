"""参考卫星的模拟设备（契约 §5.4）。

每个设备都实现 ``status`` / ``stop``（总是放行）/ ``safe``（死人开关动作），外加各自 kind 的
动作。设备跑在卫星的 asyncio 循环上；``ctx.emit(kind, data)`` 发事件。

``origin`` 区分动作由谁发起：``"hub"``（枢纽命令）或 ``"manual"``（人在卫星本地操作，例如
有人在 LightField 窗口里点了采集）。死人开关只停枢纽发起的（契约 §5.3）；急停全停。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from mast.satellite import protocol as P
from mast.satellite.sim import spectra

if TYPE_CHECKING:  # pragma: no cover
    from mast.satellite.sim.client import SimSatellite

logger = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


class DeviceError(Exception):
    """动作失败。``code`` 是契约 §5.2 的错误码。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ActionCtx:
    def __init__(self, cmd_id: str | None, origin: str = "hub") -> None:
        self.cmd_id = cmd_id
        self.origin = origin


#: operator_hold 时仍放行的动作（契约 §5.3）：stop 与只读动作。``safe`` 是 stop
#: 的子集（只停枢纽发起的活），MAST 正常关停时会发它，同样放行；``abort``（开环台立即停、
#: 不减速，§13.1）也是停。
HOLD_ALLOWED = frozenset({"stop", "safe", "abort", "status", "wavelength_axis", "get_settings"})


class SimDevice:
    kind = "device"
    driver = "sim"
    caps: tuple[str, ...] = ("status", "stop", "safe")
    #: 动作 → 是否会占用设备（同一时刻只能有一个占用性动作）。
    busy_actions: frozenset[str] = frozenset()

    def __init__(self, sat: "SimSatellite", dev_id: str, *, speed: float = 1.0,
                 **opts: Any) -> None:
        self.sat = sat
        self.id = dev_id
        self.speed = max(1e-3, float(speed))
        self.opts = opts
        #: 停一个正在跑的作业要花的时间（秒；不随 speed 缩放）。真设备不是瞬停的：LightField
        #: 停采集要等这一帧读出、转台要减速。默认 0；测「慢卫星」时用设备选项 stop_latency_s 打开。
        self.stop_latency_s = max(0.0, float(opts.get("stop_latency_s", 0.0) or 0.0))
        self.operator_hold = False
        self.error: str | None = None
        self._job: asyncio.Task | None = None
        self._job_origin: str | None = None
        self._job_action: str | None = None
        self._job_cmd_id: str | None = None
        self._last_state_sig: tuple | None = None
        self._last_state_ms = 0

    # ── 描述与状态 ──
    @property
    def state(self) -> str:
        if self.operator_hold:
            return "operator_hold"
        if self.error:
            return "error"
        return "busy" if self.busy else "ready"

    @property
    def busy(self) -> bool:
        return self._job is not None and not self._job.done()

    def descriptor(self) -> dict:
        return {"id": self.id, "kind": self.kind, "driver": self.driver,
                "model": self.opts.get("model", f"SIM-{self.kind}"),
                "serial": self.opts.get("serial", f"SIM-{self.id}"), "sim": True,
                "state": self.state, "caps": list(self.caps), "info": self.info()}

    def info(self) -> dict:
        return {}

    def status(self) -> dict:
        return {"state": self.state, "operator_hold": self.operator_hold}

    def emit(self, kind: str, data: dict) -> None:
        self.sat.emit(self.id, kind, data)

    def emit_state(self, *, force: bool = False) -> None:
        st = self.status()
        sig = tuple(sorted((k, repr(v)) for k, v in st.items()))
        now = now_ms()
        if force or sig != self._last_state_sig or now - self._last_state_ms >= 5000:
            self._last_state_sig = sig
            self._last_state_ms = now
            self.emit("device_state", st)

    def set_operator_hold(self, on: bool) -> None:
        self.operator_hold = bool(on)
        self.emit_state(force=True)

    # ── 动作 ──
    def hub_active(self) -> bool:
        return self.busy and self._job_origin == "hub"

    def check(self, action: str, args: dict) -> None:
        if action not in self.caps:
            raise DeviceError("not_supported", f"{self.kind} 不支持 {action}")
        if self.operator_hold and action not in HOLD_ALLOWED:
            raise DeviceError("operator_hold", f"{self.id} 处于人工接管，只放行 stop 与只读动作")
        if action in self.busy_actions and self.busy:
            raise DeviceError("device_busy", f"{self.id} 正在执行 {self._job_action}")

    async def run(self, action: str, args: dict, ctx: ActionCtx) -> Any:
        self.check(action, args)
        if action == "status":
            return self.status()
        if action == "stop":
            await self.stop(args, origin=ctx.origin)
            return self.status()
        if action == "safe":
            return {"stopped": await self.safe()}
        handler = getattr(self, f"act_{action}", None)
        if handler is None:
            raise DeviceError("not_supported", action)
        if action in self.busy_actions:
            out = await self._run_job(action, handler(args, ctx), ctx)
            # 运动类动作返回 None：结果取作业结束**之后**的状态（moving=false）。
            return self.status() if out is None else out
        return await handler(args, ctx)

    def _axis_extra(self) -> dict:
        """axis_state 里带上正在执行的枢纽命令 id（运动中才有；空闲或人工运动时不带）。
        枢纽据此把 move 类 PendingCommand 从 accepted 推到 running。"""
        if self.busy and self._job_origin == "hub" and self._job_cmd_id:
            return {"cmd_id": self._job_cmd_id}
        return {}

    def _axis_evt(self) -> None:
        """运动类设备覆写：发一条 axis_state。其余设备没有轴，什么也不发。"""

    async def _run_job(self, action: str, coro, ctx: ActionCtx) -> Any:
        task = asyncio.ensure_future(coro)
        self._job, self._job_origin, self._job_action = task, ctx.origin, action
        self._job_cmd_id = ctx.cmd_id
        self.emit_state(force=True)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 命令被取消 → 把设备上的活停掉，再把取消传上去。
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001
                    pass
            raise
        finally:
            if self._job is task:
                self._job = self._job_origin = self._job_action = self._job_cmd_id = None
            self.emit_state(force=True)
            # 运动协程里最后一条 axis_state 发出时作业还没结束（moving 仍是 true）；作业清掉之后
            # 立刻补一条，免得「停了」要等到 1 Hz 的空闲心跳才看得见。
            self._axis_evt()

    async def stop(self, args: dict | None = None, *, origin: str = "hub") -> list[str]:
        job = self._job
        if job is not None and not job.done():
            if self.stop_latency_s:
                await asyncio.sleep(self.stop_latency_s)
            job.cancel()
            try:
                await job
            except BaseException:  # noqa: BLE001
                pass
            return [self.id]
        return []

    async def safe(self) -> list[str]:
        """死人开关：只停枢纽发起的。"""
        if self.hub_active():
            return await self.stop(origin="deadman")
        return []

    async def background(self) -> None:
        """周期发 device_state（变化时立刻、另每 5 s 一次）。"""
        while True:
            self.emit_state()
            await asyncio.sleep(1.0)


# ── 光谱仪 ──────────────────────────────────────────────────────────────────

#: LightField 6.18 的枚举名（``PrincetonInstruments.LightField.AddIns``，AddInSupportServices
#: 元数据，去掉 UninitializedEnum）。与 C# 卫星 ``SpectrometerArgs`` 是同一张表：参考卫星要按它
#: 校验，否则一个拼错的名字（例如 ``EffectiveExposure``，正确的是 ``EffectivelyExposing``）在这里
#: 测得通、到真机上才 invalid_args。某台相机支不支持某个值是 LightField ``IsValid`` 的事；这里
#: 只拦「根本不存在的名字」。
_LF = {
    "trigger.response": ("NoResponse", "ReadoutPerTrigger", "ShiftPerTrigger",
                         "ExposeDuringTriggerPulse", "StartOnSingleTrigger", "GatePerTrigger"),
    "trigger.edge": ("RisingEdge", "FallingEdge", "PositivePolarity", "NegativePolarity",
                     "AlternatingEdgeRising", "AlternatingEdgeFalling"),
    "shutter_mode": ("Normal", "AlwaysOpen", "AlwaysClosed", "OpenBeforeTrigger"),
    "adc_quality": ("LowNoise", "HighCapacity", "HighSpeed", "ElectronMultiplied",
                    "HighDynamicRange", "LongExposure"),
    "output_signal": ("NotReadingOut", "ShutterOpen", "Busy", "AlwaysLow", "AlwaysHigh",
                      "Acquiring", "ShiftingUnderMask", "Exposing", "EffectivelyExposing",
                      "EffectivelyExposingAlternation", "ReadingOut", "WaitingForTrigger",
                      "InternalTriggerT0", "Gate", "AuxOutput"),
}
_STAMP_NAMES = ("None", "ExposureStarted", "ExposureEnded")
_CONFIGURE_KEYS = ("exposure_ms", "frames", "roi", "adc_mhz", "adc_quality", "shutter_mode",
                   "trigger", "output_signal", "timestamps", "frame_tracking", "file")
_SUB_KEYS = {"trigger": ("response", "edge"),
             "file": ("directory", "base_name", "attach_date", "attach_increment"),
             "roi": ("mode", "rows", "x", "y", "width", "height", "x_bin", "y_bin", "regions")}
_ADC_MHZ = (0.1, 1.0, 4.0)
_SENSOR_ROWS = 100


def _one_of(key: str, value: Any, names: tuple[str, ...]) -> str:
    hit = next((n for n in names if isinstance(value, str) and n.lower() == value.lower()), None)
    if hit is None:
        raise DeviceError("invalid_args",
                          f"{key} {value!r} 不是 LightField 的名字（可选：{', '.join(names)}）")
    return hit


def _strict_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise DeviceError("invalid_args", f"{key} 必须是 true 或 false")


def _strict_str(key: str, value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    raise DeviceError("invalid_args", f"{key} 必须是非空字符串")


def _timestamps(value: Any) -> str:
    """LightField 的 TimeStamps 标志：bool（true = 两个都要）或 "None" / "ExposureStarted" /
    "ExposureEnded" / "ExposureStarted,ExposureEnded"。返回标准的逗号写法。"""
    if isinstance(value, bool):
        return "ExposureStarted,ExposureEnded" if value else "None"
    if not isinstance(value, str) or not value.strip():
        raise DeviceError("invalid_args", 'timestamps 必须是 "None"、"ExposureStarted"、'
                                          '"ExposureEnded" 或 "ExposureStarted,ExposureEnded"')
    parts = [_one_of("timestamps", p.strip(), _STAMP_NAMES) for p in value.split(",")]
    if "None" in parts and len(parts) > 1:
        raise DeviceError("invalid_args", "timestamps：None 不能和别的标志一起给")
    started, ended = "ExposureStarted" in parts, "ExposureEnded" in parts
    if started and ended:
        return "ExposureStarted,ExposureEnded"
    return "ExposureStarted" if started else ("ExposureEnded" if ended else "None")


def _is_int(v: Any) -> bool:
    return (not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
            and v == int(v))


def _number_in(v: Any, name: str, lo: float, hi: float) -> float:
    """``lo < v <= hi`` 的有限实数（bool 不算数），否则 invalid_args。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or not (lo < float(v) <= hi):
        raise DeviceError("invalid_args", f"{name} 必须是 ({lo:g}, {hi:g}] 里的数")
    return float(v)


def _int_in(v: Any, name: str, lo: int, hi: int) -> int:
    if not _is_int(v) or not (lo <= int(v) <= hi):
        raise DeviceError("invalid_args", f"{name} 必须是 {lo}..{hi} 的整数")
    return int(v)


def _pos_int(d: dict, key: str, name: str, *, minimum: int = 1) -> int:
    v = d.get(key)
    if not _is_int(v) or int(v) < minimum:
        raise DeviceError("invalid_args", f"{name} 必须是 ≥{minimum} 的整数")
    return int(v)


def _parse_roi(roi: Any, n_pixels: int) -> dict:
    """roi：{mode:"full"} / {mode:"line", rows} / {mode:"binned", x_bin, y_bin} /
    {mode:"custom", x, y, width, height, x_bin?, y_bin?} 或 {mode:"custom", regions:[…]}。"""
    if not isinstance(roi, dict):
        raise DeviceError("invalid_args", "roi 必须是对象")
    mode = roi.get("mode")
    if mode == "full":
        return {"mode": "full"}
    if mode == "line":
        rows = _pos_int(roi, "rows", "roi.rows")
        if rows > _SENSOR_ROWS:
            raise DeviceError("invalid_args", f"roi.rows 必须在 1..{_SENSOR_ROWS}")
        return {"mode": "line", "rows": rows}
    if mode == "binned":
        xb, yb = _pos_int(roi, "x_bin", "roi.x_bin"), _pos_int(roi, "y_bin", "roi.y_bin")
        if xb > n_pixels or yb > _SENSOR_ROWS:
            raise DeviceError("invalid_args", f"roi 的合并超出传感器（{n_pixels} × {_SENSOR_ROWS}）")
        return {"mode": "binned", "x_bin": xb, "y_bin": yb}
    if mode == "custom":
        regs = roi.get("regions")
        flat = any(k in roi for k in ("x", "y", "width", "height"))
        if regs is not None and flat:
            raise DeviceError("invalid_args", "roi：x/y/width/height 与 regions 只能二选一")
        if regs is None:
            regs = [roi]
        if not isinstance(regs, list) or not regs:
            raise DeviceError("invalid_args", "roi.regions 不能为空")
        out = []
        for r in regs:
            if not isinstance(r, dict):
                raise DeviceError("invalid_args", "roi.regions 的每一项必须是对象")
            x, y = _pos_int(r, "x", "roi.x", minimum=0), _pos_int(r, "y", "roi.y", minimum=0)
            w, h = _pos_int(r, "width", "roi.width"), _pos_int(r, "height", "roi.height")
            xb = _pos_int(r, "x_bin", "roi.x_bin") if "x_bin" in r else 1
            yb = _pos_int(r, "y_bin", "roi.y_bin") if "y_bin" in r else h
            if x + w > n_pixels or y + h > _SENSOR_ROWS:
                raise DeviceError("invalid_args",
                                  f"roi 区域超出 {n_pixels} × {_SENSOR_ROWS} 的传感器")
            if xb > w or yb > h:
                raise DeviceError("invalid_args", "roi 的合并大于区域本身")
            out.append({"x": x, "y": y, "width": w, "height": h, "x_bin": xb, "y_bin": yb})
        return {"mode": "custom", "regions": out}
    raise DeviceError("invalid_args", "roi.mode 必须是 full、line、binned 或 custom")


def _validate_spectrometer_values(a: dict, *, configure: bool) -> dict:
    """校验一组光谱仪设置，返回标准化后的值；任一项不合法就整笔 invalid_args（不留半套）。"""
    if configure:
        bad = [k for k in a if k not in _CONFIGURE_KEYS]
        if bad:
            raise DeviceError("invalid_args", f"不认识的 configure 键：{bad}（可用：{list(_CONFIGURE_KEYS)}）")
    for k, sub in _SUB_KEYS.items():
        if k in a:
            if not isinstance(a[k], dict):
                raise DeviceError("invalid_args", f"{k} 必须是对象")
            badsub = [s for s in a[k] if s not in sub]
            if badsub:
                raise DeviceError("invalid_args", f"不认识的 {k} 键：{badsub}（可用：{list(sub)}）")
    out: dict[str, Any] = {}
    for k, v in a.items():
        if k == "exposure_ms":
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise DeviceError("invalid_args", "exposure_ms 不是数") from None
            if isinstance(v, bool) or not (0.001 <= f <= 3_600_000):
                raise DeviceError("invalid_args", "exposure_ms 超出范围（0.001 .. 3.6e6）")
            out[k] = f
        elif k == "frames":
            if not _is_int(v) or not (1 <= int(v) <= 1_000_000):
                raise DeviceError("invalid_args", "frames 必须是 1 .. 1e6 的整数")
            out[k] = int(v)
        elif k == "adc_mhz":
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise DeviceError("invalid_args", "adc_mhz 不是数") from None
            if isinstance(v, bool) or not any(abs(f - x) < 1e-9 for x in _ADC_MHZ):
                raise DeviceError("invalid_args", "adc_mhz 必须是 0.1、1 或 4（sim）")
            out[k] = f
        elif k in ("shutter_mode", "adc_quality", "output_signal"):
            out[k] = _one_of(k, v, _LF[k])
        elif k == "timestamps":
            out[k] = _timestamps(v)
        elif k == "frame_tracking":
            out[k] = _strict_bool(k, v)
        elif k == "trigger":
            t: dict[str, Any] = {}
            if "response" in v:
                t["response"] = _one_of("trigger.response", v["response"], _LF["trigger.response"])
            if "edge" in v:
                e = v["edge"]
                if isinstance(e, str) and e.lower() in ("rising", "falling"):
                    t["edge"] = "RisingEdge" if e.lower() == "rising" else "FallingEdge"
                else:
                    t["edge"] = _one_of("trigger.edge", e, _LF["trigger.edge"])
            out[k] = t
        elif k == "file":
            fl: dict[str, Any] = {}
            for sk in ("directory", "base_name"):
                if sk in v:
                    fl[sk] = _strict_str(f"file.{sk}", v[sk])
            for sk in ("attach_date", "attach_increment"):
                if sk in v:
                    fl[sk] = _strict_bool(f"file.{sk}", v[sk])
            out[k] = fl
        elif k == "roi":
            out[k] = _parse_roi(v, spectra.N_PIXELS)
        elif not configure:
            raise DeviceError("invalid_args", f"{k} 不能用 set_settings 改")
    return out


class SimSpectrometer(SimDevice):
    kind = "spectrometer"
    caps = ("status", "stop", "safe", "wavelength_axis", "configure", "acquire", "set_grating",
            "set_center_wavelength", "set_exit_port", "get_settings", "set_settings")
    busy_actions = frozenset({"acquire"})
    SETTABLE = frozenset({"exposure_ms", "frames", "adc_mhz", "adc_quality", "shutter_mode",
                          "output_signal", "timestamps", "frame_tracking"})

    def __init__(self, sat, dev_id: str, **opts: Any) -> None:
        super().__init__(sat, dev_id, **opts)
        self.settings: dict[str, Any] = {
            "exposure_ms": 100.0, "frames": 1, "roi": None, "adc_mhz": 1.0,
            "adc_quality": "LowNoise", "shutter_mode": "Normal",
            "trigger": {"response": "NoResponse", "edge": "RisingEdge"},
            "output_signal": "ShutterOpen", "timestamps": "None", "frame_tracking": False,
            "file": {"directory": str(opts.get("watch_dir") or ""), "base_name": "Raman",
                     "attach_date": False, "attach_increment": True},
        }
        self.grating = "[500nm,1200][0][0]"
        self.center_nm = spectra.DEFAULT_CENTER_NM
        self.exit_port = "front"
        self.laser_line_nm = float(opts.get("laser_line_nm", spectra.DEFAULT_LASER_NM))
        self.temperature_c = -120.0
        self.axis_ver = 0
        self._axis = None
        self.increment = 0
        self.intensity = float(opts.get("intensity", 1.0))
        self._rng = np.random.default_rng(int(opts.get("seed", 1234)))
        self._new_axis(emit=True)

    def info(self) -> dict:
        return {"n_pixels": spectra.N_PIXELS, "laser_line_nm": self.laser_line_nm,
                "gratings": ["[500nm,1200][0][0]", "[500nm,600][1][0]", "[800nm,150][2][0]"]}

    def _new_axis(self, *, emit: bool) -> None:
        self.axis_ver += 1
        self._axis = spectra.wavelength_axis(self.center_nm, self.grating)
        if emit:
            self.emit("wavelength_axis", self.axis_data())

    def axis_data(self) -> dict:
        return {"axis_ver": self.axis_ver, "n": int(self._axis.size),
                "nm_b64": P.encode_array(self._axis, "f64"), "laser_line_nm": self.laser_line_nm,
                "grating": self.grating, "center_nm": self.center_nm}

    def status(self) -> dict:
        return {"state": self.state, "is_running": self.busy,
                "temperature_c": self.temperature_c, "temperature_locked": True,
                "exposure_ms": self.settings["exposure_ms"], "frames": self.settings["frames"],
                "grating": self.grating, "center_wavelength_nm": self.center_nm,
                "exit_port": self.exit_port,
                "trigger_response": self.settings["trigger"]["response"],
                "roi": self.settings["roi"], "adc_mhz": self.settings["adc_mhz"],
                "laser_line_nm": self.laser_line_nm, "operator_hold": self.operator_hold,
                "acquiring_origin": self._job_origin}

    async def act_wavelength_axis(self, args, ctx):
        return self.axis_data()

    async def act_configure(self, args, ctx):
        """校验与 C# 卫星（``SpectrometerArgs``）一致、**全有或全无**：先把每个值都校验完再一起
        写入 —— 一个键错了就整笔拒收（invalid_args），不留半套设置。枚举值按 LightField 的
        名字比（不区分大小写），存成标准写法。"""
        if self.busy:
            raise DeviceError("device_busy", "采集进行中不能改设置")
        new = _validate_spectrometer_values(dict(args or {}), configure=True)
        for k, v in new.items():
            if k in ("trigger", "file"):
                self.settings[k] = {**self.settings[k], **v}
            else:
                self.settings[k] = v
        self.emit_state(force=True)
        return self.status()

    async def act_set_grating(self, args, ctx):
        g = str((args or {}).get("grating") or "")
        if g not in self.info()["gratings"]:
            raise DeviceError("invalid_args", f"没有光栅 {g!r}")
        await asyncio.sleep(0.2 / self.speed)
        self.grating = g
        self._new_axis(emit=True)
        return self.status()

    async def act_set_center_wavelength(self, args, ctx):
        try:
            nm = float((args or {})["nm"])
        except (KeyError, TypeError, ValueError):
            raise DeviceError("invalid_args", "需要 nm") from None
        if not (200.0 <= nm <= 1100.0):
            raise DeviceError("limit", f"中心波长 {nm} nm 超出 200–1100")
        await asyncio.sleep(0.2 / self.speed)
        self.center_nm = nm
        self._new_axis(emit=True)
        return self.status()

    async def act_set_exit_port(self, args, ctx):
        port = str((args or {}).get("port") or "")
        if port not in ("front", "side"):
            raise DeviceError("invalid_args", "port 必须是 front 或 side")
        self.exit_port = port
        return self.status()

    async def act_get_settings(self, args, ctx):
        keys = (args or {}).get("keys") or list(self.settings)
        return {k: self.settings.get(k) for k in keys}

    async def act_set_settings(self, args, ctx):
        values = (args or {}).get("values") or {}
        if not isinstance(values, dict):
            raise DeviceError("invalid_args", "values 必须是对象")
        bad = [k for k in values if k not in self.SETTABLE]
        if bad:
            raise DeviceError("invalid_args", f"这些键不在白名单里：{bad}")
        new = _validate_spectrometer_values(dict(values), configure=False)
        self.settings.update(new)
        return {k: self.settings.get(k) for k in values}

    async def act_acquire(self, args, ctx):
        a = args or {}
        # 同 C# 卫星：acquire 带的 exposure_ms / frames / base_name 会**留在**设置里（LightField
        # 改的就是实验本身的设置），之后的 get_settings 看得到。
        new = _validate_spectrometer_values(
            {k: a[k] for k in ("exposure_ms", "frames") if k in a}, configure=True)
        self.settings.update(new)
        if a.get("base_name"):
            self.settings["file"]["base_name"] = _strict_str("base_name", a["base_name"])
        exposure = float(self.settings["exposure_ms"])
        frames = int(self.settings["frames"])
        save = bool(a.get("save", True))
        base = str(self.settings["file"].get("base_name") or "Raman")
        manual = ctx.origin == "manual"
        started = now_ms()
        self.emit("acq_started", {"cmd_id": ctx.cmd_id, "frames": frames, "exposure_ms": exposure,
                                  "manual": manual, "tag": a.get("tag"), "axis_ver": self.axis_ver})
        stack = []
        t_us0 = int(time.time() * 1e6)
        for i in range(frames):
            t0 = int(time.time() * 1e6)
            await asyncio.sleep(exposure / 1000.0 / self.speed)
            t1 = int(time.time() * 1e6)
            frame = spectra.synth_frame(self._axis, exposure_ms=exposure,
                                        laser_nm=self.laser_line_nm, intensity=self.intensity,
                                        rng=self._rng)
            stack.append(frame)
            self.emit("spectrum", {"cmd_id": ctx.cmd_id, "frame_index": i, "n": int(frame.size),
                                   "dtype": "u16", "data_b64": P.encode_array(frame, "u16"),
                                   "axis_ver": self.axis_ver, "manual": manual,
                                   "exposure_started_us": t0, "exposure_ended_us": t1})
            self.emit("acq_progress", {"cmd_id": ctx.cmd_id, "frame": i + 1, "frames": frames})
        finished = now_ms()
        files: list[str] = []
        if save and self.settings["file"].get("directory"):
            files.append(self._save(stack, base, exposure, started, finished, a.get("meta")))
        arr = np.asarray(stack, dtype=np.float64)
        summary = {"min": float(arr.min()), "max": float(arr.max()), "mean": float(arr.mean())}
        self.emit("acq_completed", {"cmd_id": ctx.cmd_id, "frames": frames, "files": files,
                                    "manual": manual, "summary": summary})
        return {"frames": frames, "files": files, "exposure_ms": exposure, "started_ms": started,
                "finished_ms": finished, "summary": summary, "axis_ver": self.axis_ver,
                "first_exposure_started_us": t_us0}

    def _save(self, stack, base: str, exposure: float, started_ms: int, finished_ms: int,
              meta: Any) -> str:
        from mast.satellite.spe import build_footer_xml, write_spe

        self.increment += 1
        d = Path(self.settings["file"]["directory"])
        now = datetime.now().astimezone()
        name = f"{base} {self.increment}.spe"
        if self.settings["file"].get("attach_date"):
            name = f"{base} {now:%Y-%m-%d} {self.increment}.spe"
        path = d / f"{now:%Y}" / f"{now:%Y%m%d}" / name
        created = datetime.fromtimestamp(finished_ms / 1000.0, tz=timezone.utc).astimezone()
        origin = datetime.fromtimestamp(started_ms / 1000.0, tz=timezone.utc).astimezone()
        footer = build_footer_xml(
            width=int(self._axis.size), height=1, frames=len(stack), wavelengths_nm=self._axis,
            laser_line_nm=self.laser_line_nm, exposure_ms=exposure, center_nm=self.center_nm,
            grating=self.grating, created_iso=created.isoformat(),
            origin_created_iso=origin.isoformat(), adc_speed_mhz=float(self.settings["adc_mhz"]),
            adc_quality=str(self.settings["adc_quality"]), temperature_c=self.temperature_c,
            trigger_response=str(self.settings["trigger"].get("response")))
        write_spe(path, np.asarray(stack), footer)
        # 采集时刻交给文件监视器，随 file_offer.meta 上传（契约 §11.2：时间换算到枢纽时钟）。
        self.sat.note_file_meta(path, {"acq_started_ms": started_ms, "acq_finished_ms": finished_ms,
                                       "device": self.id, "exposure_ms": exposure,
                                       "frames": len(stack), "user_meta": meta})
        return str(path)


# ── 运动类 ──────────────────────────────────────────────────────────────────

class _MotionMixin:
    """把一个「从当前值匀速走到目标」的过程做成可取消的任务，运动中 ≤20 Hz 发 axis_state。"""

    AXIS_RATE_S = 0.05

    async def _move(self, getter: Callable[[], float], setter: Callable[[float], None],
                    target: float, velocity: float, emit_axis: Callable[[], None]) -> None:
        start = getter()
        dist = target - start
        if dist == 0:
            emit_axis()
            return
        dur = abs(dist) / max(1e-9, velocity)
        t0 = time.monotonic()
        last_emit = 0.0
        try:
            while True:
                el = time.monotonic() - t0
                frac = min(1.0, el / dur)
                setter(start + dist * frac)
                if el - last_emit >= self.AXIS_RATE_S or frac >= 1.0:
                    last_emit = el
                    emit_axis()
                if frac >= 1.0:
                    return
                await asyncio.sleep(min(self.AXIS_RATE_S, max(0.0, dur - el)))
        finally:
            emit_axis()


class SimRotationStage(_MotionMixin, SimDevice):
    kind = "rotation_stage"
    caps = ("status", "stop", "safe", "move_abs", "move_rel", "home", "set_velocity", "identify")
    busy_actions = frozenset({"move_abs", "move_rel", "home"})
    #: PRM1-Z8 的标称上限（25°/s、25°/s²）；set_velocity 按它校验。
    MAX_VELOCITY_DEG_S = 25.0
    MAX_ACCEL_DEG_S2 = 25.0

    def __init__(self, sat, dev_id: str, **opts: Any) -> None:
        super().__init__(sat, dev_id, **opts)
        self.position = 0.0
        self.homed = False
        self.min_deg = float(opts.get("min_deg", -360.0))
        self.max_deg = float(opts.get("max_deg", 720.0))
        self.velocity = float(opts.get("velocity_deg_s", 20.0))
        self.accel = float(opts.get("accel_deg_s2", 20.0))
        self.identify_count = 0
        # PRM1-Z8 上电后不回零就没有绝对角度：真 KDC101 与 C# 卫星都拒 homed=false 时的
        # move_abs（回 unavailable）。参考卫星照做，免得技能只在它这儿测得通。
        self.require_home = bool(opts.get("require_home", True))

    def info(self) -> dict:
        return {"unit": "deg", "min_deg": self.min_deg, "max_deg": self.max_deg}

    def status(self) -> dict:
        return {"state": self.state, "position_deg": round(self.position, 6), "moving": self.busy,
                "homed": self.homed, "enabled": True, "operator_hold": self.operator_hold,
                "velocity_deg_s": self.velocity, "accel_deg_s2": self.accel}

    def _axis_evt(self) -> None:
        self.emit("axis_state", {"position_deg": round(self.position, 6), "moving": self.busy,
                                 **self._axis_extra()})

    async def _go(self, target: float) -> None:
        if not (self.min_deg <= target <= self.max_deg):
            raise DeviceError("limit", f"{target}° 超出 [{self.min_deg}, {self.max_deg}]")
        await self._move(lambda: self.position, lambda v: setattr(self, "position", v), target,
                         self.velocity * self.speed, self._axis_evt)

    def check(self, action: str, args: dict) -> None:
        super().check(action, args)
        if action == "move_abs" and self.require_home and not self.homed:
            raise DeviceError("unavailable", f"{self.id} 还没回零（homed=false），没有绝对角度；"
                                             "先 home")

    async def act_move_abs(self, args, ctx):
        try:
            target = float(args["deg"])
        except (KeyError, TypeError, ValueError):
            raise DeviceError("invalid_args", "需要 deg") from None
        await self._go(target)

    async def act_move_rel(self, args, ctx):
        try:
            d = float(args["deg"])
        except (KeyError, TypeError, ValueError):
            raise DeviceError("invalid_args", "需要 deg") from None
        await self._go(self.position + d)

    async def act_home(self, args, ctx):
        await self._go(0.0)
        self.homed = True

    async def act_set_velocity(self, args, ctx):
        """KDC101 的速度参数：下一次运动起生效（正在转的这一段不变）。"""
        a = args or {}
        v = _number_in(a.get("deg_s"), "deg_s", 0.0, self.MAX_VELOCITY_DEG_S)
        acc = (self.accel if a.get("accel_deg_s2") is None
               else _number_in(a["accel_deg_s2"], "accel_deg_s2", 0.0, self.MAX_ACCEL_DEG_S2))
        self.velocity, self.accel = v, acc
        self.emit_state(force=True)
        return self.status()

    async def act_identify(self, args, ctx):
        """Kinesis 的 Identify：控制器面板灯闪几下，不动轴。"""
        self.identify_count += 1
        return self.status()

    async def background(self) -> None:
        while True:
            self.emit_state()
            if not self.busy:
                self._axis_evt()                  # 空闲 1 Hz
            await asyncio.sleep(1.0)


class SimPicomotor(SimDevice):
    kind = "openloop_stepper"
    caps = ("status", "stop", "safe", "jog", "move_abs", "set_zero", "set_velocity", "set_accel",
            "abort")
    busy_actions = frozenset({"jog", "move_abs"})
    #: 8742 手册 VA（速度）与 AC（加速度）的取值范围。
    VELOCITY_RANGE = (1, 2000)
    ACCEL_RANGE = (1, 200_000)

    def __init__(self, sat, dev_id: str, **opts: Any) -> None:
        super().__init__(sat, dev_id, **opts)
        self.axes = {str(i): 0 for i in range(1, int(opts.get("n_axes", 4)) + 1)}
        self.moving: set[str] = set()
        self.max_steps = int(opts.get("max_steps_per_cmd", 2000))
        rate = float(opts.get("steps_per_s", 1000.0))
        self.velocity = {a: rate for a in self.axes}
        self.accel = {a: float(opts.get("accel_steps_s2", 100_000.0)) for a in self.axes}

    def info(self) -> dict:
        return {"axes": list(self.axes), "max_steps_per_cmd": self.max_steps,
                "note": "steps 是卫星维护的累计计数，不是位置反馈"}

    def status(self) -> dict:
        return {"state": self.state, "operator_hold": self.operator_hold,
                "axes": {a: {"steps": s, "moving": a in self.moving,
                             "velocity_steps_s": self.velocity[a], "accel_steps_s2": self.accel[a]}
                         for a, s in self.axes.items()}}

    def _axis_evt(self) -> None:
        self.emit("axis_state", {"axes": {a: {"steps": s, "moving": a in self.moving}
                                          for a, s in self.axes.items()}, **self._axis_extra()})

    def _axis_arg(self, args) -> str:
        ax = str((args or {}).get("axis", ""))
        if ax not in self.axes:
            raise DeviceError("invalid_args", f"没有轴 {ax!r}（有 {list(self.axes)}）")
        return ax

    async def _run_steps(self, ax: str, steps: int) -> None:
        self.moving.add(ax)
        start = self.axes[ax]
        dur = abs(steps) / (self.velocity[ax] * self.speed)
        t0 = time.monotonic()
        try:
            while True:
                frac = min(1.0, (time.monotonic() - t0) / dur) if dur > 0 else 1.0
                self.axes[ax] = start + int(round(steps * frac))
                self._axis_evt()
                if frac >= 1.0:
                    break
                await asyncio.sleep(0.05)
        finally:
            self.moving.discard(ax)
            self._axis_evt()

    async def act_jog(self, args, ctx):
        ax = self._axis_arg(args)
        try:
            steps = int((args or {})["steps"])
        except (KeyError, TypeError, ValueError):
            raise DeviceError("invalid_args", "需要 steps") from None
        if abs(steps) > self.max_steps:
            raise DeviceError("limit", f"单次 {steps} 步超过上限 {self.max_steps}")
        await self._run_steps(ax, steps)

    async def act_move_abs(self, args, ctx):
        """走到相对 set_zero 的累计计数 ``steps``；这一步要走的步数受单次上限约束。"""
        ax = self._axis_arg(args)
        target = (args or {}).get("steps")
        if not _is_int(target):
            raise DeviceError("invalid_args", "需要整数 steps（目标计数）")
        delta = int(target) - self.axes[ax]
        if abs(delta) > self.max_steps:
            raise DeviceError("limit", f"从 {self.axes[ax]} 到 {int(target)} 要走 {delta} 步，"
                                       f"超过单次上限 {self.max_steps}")
        await self._run_steps(ax, delta)

    async def act_set_velocity(self, args, ctx):
        ax = self._axis_arg(args)
        self.velocity[ax] = float(_int_in((args or {}).get("steps_s"), "steps_s",
                                          *self.VELOCITY_RANGE))
        self.emit_state(force=True)
        return self.status()

    async def act_set_accel(self, args, ctx):
        ax = self._axis_arg(args)
        self.accel[ax] = float(_int_in((args or {}).get("steps_s2"), "steps_s2",
                                       *self.ACCEL_RANGE))
        self.emit_state(force=True)
        return self.status()

    async def act_abort(self, args, ctx):
        """立即停所有轴、不减速（8742 的 AB）：不走 stop 的减速延迟。"""
        job = self._job
        if job is not None and not job.done():
            job.cancel()
            try:
                await job
            except BaseException:  # noqa: BLE001
                pass
        return self.status()

    async def act_set_zero(self, args, ctx):
        ax = self._axis_arg(args)
        if ax in self.moving:
            raise DeviceError("device_busy", f"轴 {ax} 在动")
        self.axes[ax] = 0
        self._axis_evt()
        return self.status()

    async def background(self) -> None:
        while True:
            self.emit_state()
            if not self.busy:
                self._axis_evt()
            await asyncio.sleep(1.0)


class SimXYZ(_MotionMixin, SimDevice):
    kind = "xyz_stage"
    caps = ("status", "stop", "safe", "move_abs", "move_rel")
    busy_actions = frozenset({"move_abs", "move_rel"})

    def __init__(self, sat, dev_id: str, **opts: Any) -> None:
        super().__init__(sat, dev_id, **opts)
        self.pos = {"x": 5.0, "y": 2.5, "z": 4.0}
        self.limits = opts.get("limits_mm") or {"x": [-2.0, 13.3], "y": [0.0, 5.0], "z": [0.0, 9.6]}
        self.max_step = float(opts.get("max_step_mm", 0.5))
        self.velocity = float(opts.get("velocity_mm_s", 0.5))

    def info(self) -> dict:
        return {"unit": "mm", "limits_mm": self.limits, "max_step_mm": self.max_step}

    def status(self) -> dict:
        return {"state": self.state, "x_mm": round(self.pos["x"], 6), "y_mm": round(self.pos["y"], 6),
                "z_mm": round(self.pos["z"], 6), "moving": self.busy,
                "operator_hold": self.operator_hold}

    def _axis_evt(self) -> None:
        self.emit("axis_state", {"x_mm": round(self.pos["x"], 6), "y_mm": round(self.pos["y"], 6),
                                 "z_mm": round(self.pos["z"], 6), "moving": self.busy,
                                 **self._axis_extra()})

    def _validate(self, target: dict[str, float]) -> None:
        for ax, v in target.items():
            lo, hi = self.limits[ax]
            if not (lo <= v <= hi):
                raise DeviceError("limit", f"{ax}={v} mm 超出 [{lo}, {hi}]")
            if abs(v - self.pos[ax]) > self.max_step + 1e-12:
                raise DeviceError("limit", f"{ax} 一步 {abs(v - self.pos[ax]):.4g} mm 超过上限 "
                                           f"{self.max_step} mm")

    async def _go(self, target: dict[str, float]) -> None:
        self._validate(target)
        start = dict(self.pos)
        dist = math.sqrt(sum((target[a] - start[a]) ** 2 for a in target)) or 0.0
        if dist == 0:
            self._axis_evt()
            return
        frac_box = {"f": 0.0}

        def _set(f: float) -> None:
            frac_box["f"] = f
            for a in target:
                self.pos[a] = start[a] + (target[a] - start[a]) * f

        await self._move(lambda: frac_box["f"], _set, 1.0,
                         self.velocity * self.speed / dist, self._axis_evt)

    async def act_move_abs(self, args, ctx):
        tgt = {}
        for ax in ("x", "y", "z"):
            if (args or {}).get(ax) is not None:
                try:
                    tgt[ax] = float(args[ax])
                except (TypeError, ValueError):
                    raise DeviceError("invalid_args", f"{ax} 不是数") from None
        if not tgt:
            raise DeviceError("invalid_args", "至少给一个 x / y / z")
        await self._go(tgt)

    async def act_move_rel(self, args, ctx):
        tgt = {}
        for ax, key in (("x", "dx"), ("y", "dy"), ("z", "dz")):
            if (args or {}).get(key) is not None:
                try:
                    tgt[ax] = self.pos[ax] + float(args[key])
                except (TypeError, ValueError):
                    raise DeviceError("invalid_args", f"{key} 不是数") from None
        if not tgt:
            raise DeviceError("invalid_args", "至少给一个 dx / dy / dz")
        await self._go(tgt)

    async def background(self) -> None:
        while True:
            self.emit_state()
            if not self.busy:
                self._axis_evt()
            await asyncio.sleep(1.0)


# ── 文件监视 ────────────────────────────────────────────────────────────────

class SimFileWatcher(SimDevice):
    """监视一个目录（真卫星上是采集软件的数据目录）：文件稳定 2 s 后算 sha256，交给上传器。"""

    kind = "file_watcher"
    driver = "spe_watcher"
    caps = ("status", "stop", "safe", "rescan", "pause", "resume")
    SUFFIX_KIND = {".spe": "spe", ".csv": "csv", ".lfe": "lfe"}
    STABLE_S = 2.0

    def __init__(self, sat, dev_id: str, **opts: Any) -> None:
        super().__init__(sat, dev_id, **opts)
        self.root = Path(opts["root"]) if opts.get("root") else None
        self.backfill_s = float(opts.get("backfill_hours", 24.0)) * 3600.0
        self.stable_s = float(opts.get("stable_s", self.STABLE_S))
        self.paused = False
        self._seen: dict[str, tuple[int, int, float]] = {}   # path → (size, mtime_ns, 首次稳定时刻)
        self._handled: set[str] = set()
        self.queued = 0
        self.uploaded = 0
        self.bytes_uploaded = 0
        self.last_file: str | None = None
        self._started = time.time()
        self._rescan = asyncio.Event()

    def info(self) -> dict:
        return {"root": str(self.root or ""), "backfill_hours": self.backfill_s / 3600.0}

    def status(self) -> dict:
        return {"state": self.state, "operator_hold": self.operator_hold,
                "root": str(self.root or ""), "seen": len(self._seen), "queued": self.queued,
                "uploaded": self.uploaded, "bytes_uploaded": self.bytes_uploaded,
                "last_file": self.last_file, "paused": self.paused}

    async def act_rescan(self, args, ctx):
        self._handled.clear()
        self._rescan.set()
        return self.status()

    async def act_pause(self, args, ctx):
        self.paused = True
        self.emit_state(force=True)
        return self.status()

    async def act_resume(self, args, ctx):
        self.paused = False
        self._rescan.set()
        self.emit_state(force=True)
        return self.status()

    def _scan(self) -> list[Path]:
        ready: list[Path] = []
        if self.root is None or not self.root.exists():
            return ready
        now = time.time()
        for p in self.root.rglob("*"):
            try:
                if not p.is_file() or p.suffix.lower() not in self.SUFFIX_KIND:
                    continue
                st = p.stat()
            except OSError:
                continue
            key = str(p)
            if key in self._handled:
                continue
            if now - st.st_mtime > self.backfill_s and st.st_mtime < self._started:
                continue
            prev = self._seen.get(key)
            sig = (st.st_size, st.st_mtime_ns)
            if prev is None or prev[:2] != sig:
                self._seen[key] = (*sig, now)
                continue
            if now - prev[2] >= self.stable_s and st.st_size > 0:
                ready.append(p)
        return ready

    async def background(self) -> None:
        while True:
            self.emit_state()
            if not self.paused:
                for p in self._scan():
                    self._handled.add(str(p))
                    try:
                        await self._offer(p)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("文件 %s 入队失败：%s", p, exc)
                        self._handled.discard(str(p))
            try:
                await asyncio.wait_for(self._rescan.wait(), timeout=0.5)
                self._rescan.clear()
            except asyncio.TimeoutError:
                pass

    async def _offer(self, p: Path) -> None:
        data = await asyncio.to_thread(p.read_bytes)
        sha = hashlib.sha256(data).hexdigest()
        rel = p.relative_to(self.root).as_posix() if self.root else p.name
        st = p.stat()
        kind = self.SUFFIX_KIND.get(p.suffix.lower(), "other")
        self.emit("file_seen", {"name": p.name, "rel_path": rel, "size": len(data), "sha256": sha})
        meta = dict(self.sat.file_meta_for(p) or {})
        queued = self.sat.uploader.enqueue(path=p, data=data, sha=sha, rel_path=rel, kind=kind,
                                           mtime_ms=int(st.st_mtime * 1000), meta=meta,
                                           on_done=self._on_uploaded)
        if queued:
            self.queued += 1

    def _on_uploaded(self, name: str, size: int) -> None:
        self.uploaded += 1
        self.bytes_uploaded += size
        self.last_file = name
        self.queued = max(0, self.queued - 1)
        self.emit_state(force=True)


DEVICE_CLASSES = {
    "spectrometer": SimSpectrometer,
    "rotation_stage": SimRotationStage,
    "openloop_stepper": SimPicomotor,
    "xyz_stage": SimXYZ,
    "file_watcher": SimFileWatcher,
}

#: 默认设备清单：与契约 §5.5 示例里光谱电脑（卫星）的设备同名。
DEFAULT_DEVICES = {"ccd": "spectrometer", "pol": "rotation_stage", "pico": "openloop_stepper",
                   "xyz": "xyz_stage", "spe": "file_watcher"}


__all__ = ["DeviceError", "ActionCtx", "SimDevice", "SimSpectrometer", "SimRotationStage",
           "SimPicomotor", "SimXYZ", "SimFileWatcher", "DEVICE_CLASSES", "DEFAULT_DEVICES",
           "HOLD_ALLOWED"]
