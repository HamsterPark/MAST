"""把卫星事件转发到 LiveHub（契约 §7；LiveHub 由 ``mast.live`` 实现）。

只在枢纽的分发线程上调用。LiveHub 的 ``publish`` 契约是「任何线程可调、不阻塞、不抛」，
这里仍然整体包一层 try：实时绘图坏了不许影响事件分发。

流（``stream_id``）：

=================================  ========  ==========================================
``<sat>/<dev>/spectrum``           spectrum  每个 ``spectrum`` 事件一帧；``wavelength_axis``
                                             事件到来时 ``set_axis``（单位 nm）
``<sat>/<dev>/angle``              scalar    rotation_stage 的 ``axis_state``（position_deg）
``<sat>/<dev>/position``           scalar    xyz_stage 的 ``axis_state``（x_mm/y_mm/z_mm）
``<sat>/<dev>/steps``              scalar    openloop_stepper 的 ``axis_state``（每轴累计步数）
``<sat>/<dev>/state``              scalar    其他设备 ``axis_state`` 里的数值字段；没有轴流的设备
                                             （光谱仪、AOTF…）``device_state`` 里的数值字段
``<sat>/health``                   scalar    每个 pong：rtt_ms、clock_offset_ms、clock_delay_ms
``<sat>/events``                   status    采集开始/结束、死人开关、急停、人工操作、错误、链路
=================================  ========  ==========================================

光谱帧先于波长轴到达时（枢纽刚重启、或卫星没发轴），按像素号发帧（``axis_ver=0``），并向
卫星要一次 ``wavelength_axis``（同一设备 30 s 内最多要一次）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from mast.i18n import tr
from mast.satellite import protocol as P

logger = logging.getLogger(__name__)

_AXIS_STREAM = {"rotation_stage": "angle", "xyz_stage": "position",
                "openloop_stepper": "steps"}
_AXIS_REQUEST_EVERY_S = 30.0
_IMPORT_RETRY_S = 60.0


class LiveBridge:
    def __init__(self, hub, live=None) -> None:
        self._hub = hub
        self._live = live
        self._live_injected = live is not None
        self._import_failed_mono: float | None = None
        self._declared: dict[str, tuple] = {}
        # (sat, dev) → {sat_axis_ver: live_axis_ver}
        self._axis_map: dict[tuple[str, str], dict[int, int]] = {}
        self._axis_meta: dict[tuple[str, str], dict] = {}
        self._axis_requested: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self.published = 0
        self.errors = 0
        self.last_error: str | None = None

    # ── LiveHub 句柄 ──
    def _get_live(self):
        if self._live is not None:
            return self._live
        if self._live_injected:
            return None
        now = time.monotonic()
        if self._import_failed_mono is not None and now - self._import_failed_mono < _IMPORT_RETRY_S:
            return None
        try:
            from mast.live import get_live_hub

            self._live = get_live_hub()
            self._import_failed_mono = None
            return self._live
        except Exception as exc:  # noqa: BLE001 — 没有 LiveHub 只是没有实时图
            if self._import_failed_mono is None:
                logger.info("LiveHub 不可用（%s）—— 卫星数据暂不推实时图", exc)
            self._import_failed_mono = now
            return None

    def status(self) -> dict:
        return {"available": self._live is not None, "published": self.published,
                "errors": self.errors, "last_error": self.last_error,
                "streams": len(self._declared)}

    def _declare(self, live, stream_id: str, kind: str, **kw) -> None:
        sig = (kind, tuple(sorted((k, repr(v)) for k, v in kw.items())))
        if self._declared.get(stream_id) == sig:
            return
        live.declare(stream_id, kind, **kw)
        self._declared[stream_id] = sig

    def _guard(self, fn, *a, **kw) -> Any:
        try:
            return fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.errors in (1, 10) or self.errors % 1000 == 0:
                logger.warning("推 LiveHub 失败（累计 %d 次）：%s", self.errors, exc)
            return None

    # ── 入口（分发线程） ──
    def on_event(self, sat_id: str, evt: dict) -> None:
        live = self._get_live()
        if live is None:
            return
        kind = evt.get("kind")
        if kind == "spectrum":
            self._guard(self._spectrum, live, sat_id, evt)
        elif kind == "wavelength_axis":
            self._guard(self._axis_from_data, live, sat_id, str(evt.get("device") or ""),
                        evt.get("data") or {})
        elif kind == "axis_state":
            self._guard(self._axis_state, live, sat_id, evt)
        elif kind == "device_state":
            self._guard(self._device_state, live, sat_id, evt)
        elif kind in ("acq_started", "acq_completed", "deadman", "manual_activity", "error"):
            self._guard(self._status, live, sat_id, evt)

    def on_hub_event(self, sat_id: str, data: dict) -> None:
        live = self._get_live()
        if live is None or not sat_id:
            return
        kind = data.get("kind")
        texts = {
            "link": lambda d: (tr(f"链路 {d.get('prev')} → {d.get('state')}（{d.get('reason', '')}）",
                                  f"Link {d.get('prev')} -> {d.get('state')} ({d.get('reason', '')})"),
                               "info" if d.get("state") == "connected" else "warn"),
            "restart": lambda d: (tr(f"卫星重启：{len(d.get('lost_commands') or [])} 条在途命令判为丢失",
                                     f"Satellite restarted: {len(d.get('lost_commands') or [])} "
                                     f"in-flight command(s) marked lost"),
                                  "warn"),
            "estop_ack": lambda d: (
                (tr(f"急停回执：已停 {d.get('stopped')}；没停下来 {d.get('error_devices')}"
                    f"（{d.get('first_error')}）",
                    f"E-stop acknowledged: stopped {d.get('stopped')}; not stopped "
                    f"{d.get('error_devices')} ({d.get('first_error')})")) if d.get("errors")
                else tr(f"急停回执：已停 {d.get('stopped')}",
                        f"E-stop acknowledged: stopped {d.get('stopped')}"),
                "error" if d.get("errors") else "info"),
            "file": lambda d: (tr(f"文件入库：{d.get('name')}", f"File stored: {d.get('name')}"),
                               "info"),
        }
        f = texts.get(kind)
        if f is None:
            return
        text, level = f(data)
        self._guard(self._publish_status, live, sat_id, text, level)

    def on_health(self, sat_id: str, snap: dict, link: str | None) -> None:
        live = self._get_live()
        if live is None:
            return

        def _go():
            sid = f"{sat_id}/health"
            self._declare(live, sid, "scalar", title=tr(f"{sat_id} 链路", f"{sat_id} link"),
                          source=f"satellite:{sat_id}",
                          y_label=tr("时延 / 偏差", "Delay / offset"), y_unit="ms")
            values = {k: float(snap[k]) for k in ("rtt_ms", "offset_ms", "delay_ms")
                      if isinstance(snap.get(k), (int, float))}
            if "offset_ms" in values:
                values["clock_offset_ms"] = values.pop("offset_ms")
            if "delay_ms" in values:
                values["clock_delay_ms"] = values.pop("delay_ms")
            if values:
                live.publish(sid, {"t": time.time(), "values": values})
                self.published += 1
        self._guard(_go)

    # ── 具体流 ──
    def _spectrum(self, live, sat_id: str, evt: dict) -> None:
        dev = str(evt.get("device") or "")
        d = evt.get("data") or {}
        arr = P.decode_array(d.get("data_b64", ""), d.get("dtype", "u16"), d.get("n"))
        sid = f"{sat_id}/{dev}/spectrum"
        axis_info = self._axis_meta.get((sat_id, dev), {})
        self._declare(live, sid, "spectrum", title=tr(f"{sat_id} {dev} 光谱", f"{sat_id} {dev} spectrum"),
                      source=f"satellite:{sat_id}", x_label=tr("波长", "Wavelength"), x_unit="nm",
                      y_label=tr("计数", "Counts"), y_unit="counts",
                      meta={k: v for k, v in axis_info.items() if v is not None} or None)
        sat_ver = d.get("axis_ver")
        with self._lock:
            live_ver = self._axis_map.get((sat_id, dev), {}).get(int(sat_ver)) \
                if isinstance(sat_ver, int) else None
        if live_ver is None:
            live_ver = 0
            self._request_axis(sat_id, dev)
        meta = {k: d.get(k) for k in ("cmd_id", "frame_index", "frame_tracking",
                                      "exposure_started_us", "exposure_ended_us", "received_us",
                                      "timestamps_source", "ts_origin_ms", "manual")
                if d.get(k) is not None}
        meta["sat_axis_ver"] = sat_ver
        meta["clock_offset_ms"] = evt.get("clock_offset_ms")
        live.publish(sid, {"t": float(evt.get("ts_hub_ms") or time.time() * 1000) / 1000.0,
                           "y": [float(v) for v in arr.tolist()], "axis_ver": live_ver,
                           "meta": meta})
        self.published += 1

    def _axis_from_data(self, live, sat_id: str, dev: str, data: dict) -> None:
        if not dev or not data.get("nm_b64"):
            return
        nm = P.decode_array(data["nm_b64"], "f64", data.get("n"))
        sid = f"{sat_id}/{dev}/spectrum"
        info = {"laser_line_nm": data.get("laser_line_nm"), "grating": data.get("grating"),
                "center_nm": data.get("center_nm")}
        self._axis_meta[(sat_id, dev)] = info
        self._declare(live, sid, "spectrum", title=tr(f"{sat_id} {dev} 光谱", f"{sat_id} {dev} spectrum"),
                      source=f"satellite:{sat_id}", x_label=tr("波长", "Wavelength"), x_unit="nm",
                      y_label=tr("计数", "Counts"), y_unit="counts",
                      meta={k: v for k, v in info.items() if v is not None} or None)
        ver = live.set_axis(sid, [float(v) for v in nm.tolist()], unit="nm")
        sat_ver = data.get("axis_ver")
        if isinstance(sat_ver, int) and isinstance(ver, int):
            with self._lock:
                self._axis_map.setdefault((sat_id, dev), {})[sat_ver] = ver

    def _request_axis(self, sat_id: str, dev: str) -> None:
        key = (sat_id, dev)
        now = time.monotonic()
        with self._lock:
            last = self._axis_requested.get(key)
            if last is not None and now - last < _AXIS_REQUEST_EVERY_S:
                return
            self._axis_requested[key] = now
        try:
            cmd = self._hub.submit(sat_id, dev, "wavelength_axis", {}, timeout_s=10.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("wavelength_axis request failed: %s", exc)
            return

        def _done(c, _sat=sat_id, _dev=dev):
            if c.ok and isinstance(c._value, dict):
                # 回到分发线程上做，别在 WebSocket 线程里碰 LiveHub。
                self._hub._enqueue(("call", lambda: self._guard(
                    self._axis_from_data, self._get_live(), _sat, _dev, c._value)))
        cmd.add_done_callback(_done)

    def _axis_state(self, live, sat_id: str, evt: dict) -> None:
        dev = str(evt.get("device") or "")
        d = evt.get("data") or {}
        kind = self._hub.device_kind(sat_id, dev)
        chan = _AXIS_STREAM.get(kind, "state")
        values: dict[str, float] = {}
        if kind == "openloop_stepper" and isinstance(d.get("axes"), dict):
            for ax, st in d["axes"].items():
                if isinstance(st, dict) and isinstance(st.get("steps"), (int, float)):
                    values[str(ax)] = float(st["steps"])
        else:
            for k, v in d.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    values[k] = float(v)
        if not values:
            return
        sid = f"{sat_id}/{dev}/{chan}"
        unit = {"angle": "deg", "position": "mm", "steps": "steps"}.get(chan)
        self._declare(live, sid, "scalar", title=f"{sat_id} {dev}", source=f"satellite:{sat_id}",
                      y_label=chan, y_unit=unit)
        live.publish(sid, {"t": float(evt.get("ts_hub_ms") or time.time() * 1000) / 1000.0,
                           "values": values})
        self.published += 1

    def _device_state(self, live, sat_id: str, evt: dict) -> None:
        """没有轴流的设备（光谱仪、AOTF…）：device_state 里的数值字段进 ``<sat>/<dev>/state``，
        手动面板的读数就看这条流（读数不走技能执行入口，契约 §13.1）。运动类设备的读数在
        轴流里，这里不重复发；文件监视不是要看的读数。"""
        dev = str(evt.get("device") or "")
        kind = self._hub.device_kind(sat_id, dev)
        if kind in _AXIS_STREAM or kind in ("file_watcher", ""):
            return
        d = evt.get("data") or {}
        values = {k: float(v) for k, v in d.items()
                  if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if not values:
            return
        sid = f"{sat_id}/{dev}/state"
        self._declare(live, sid, "scalar", title=f"{sat_id} {dev}", source=f"satellite:{sat_id}",
                      y_label="state", y_unit=None)
        live.publish(sid, {"t": float(evt.get("ts_hub_ms") or time.time() * 1000) / 1000.0,
                           "values": values})
        self.published += 1

    def _status(self, live, sat_id: str, evt: dict) -> None:
        d = evt.get("data") or {}
        dev = evt.get("device") or ""
        kind = evt.get("kind")
        if kind == "acq_started":
            text, level = tr(f"{dev} 开始采集（{d.get('frames', '?')} 帧 × {d.get('exposure_ms', '?')} ms"
                             f"{'，人工' if d.get('manual') else ''}）",
                             f"{dev} acquisition started ({d.get('frames', '?')} frames x "
                             f"{d.get('exposure_ms', '?')} ms"
                             f"{', manual' if d.get('manual') else ''})"), "info"
        elif kind == "acq_completed":
            text, level = tr(f"{dev} 采集完成（{d.get('frames', '?')} 帧）",
                             f"{dev} acquisition completed ({d.get('frames', '?')} frames)"), "info"
        elif kind == "deadman":
            text, level = tr(f"死人开关：{d.get('reason') or d.get('message') or ''}",
                             f"Dead-man switch: {d.get('reason') or d.get('message') or ''}"), "warn"
        elif kind == "manual_activity":
            text, level = tr(f"{dev} 有人工操作：{d.get('what') or d.get('message') or ''}",
                             f"{dev} manual operation: {d.get('what') or d.get('message') or ''}"), "info"
        else:
            text, level = tr(f"{dev} 错误：{d.get('message') or ''}",
                             f"{dev} error: {d.get('message') or ''}"), "error"
        self._publish_status(live, sat_id, text, level)

    def _publish_status(self, live, sat_id: str, text: str, level: str) -> None:
        sid = f"{sat_id}/events"
        self._declare(live, sid, "status", title=tr(f"{sat_id} 事件", f"{sat_id} events"),
                      source=f"satellite:{sat_id}")
        live.publish(sid, {"t": time.time(), "text": str(text)[:300], "level": level})
        self.published += 1


__all__ = ["LiveBridge"]
