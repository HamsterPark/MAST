"""A §6 satellite-hub double with simulated devices, for instrument / skill tests.

Shapes follow the reference satellite (``mast.satellite.sim.devices``): action
names, argument keys, status fields, the ``spectrum`` / ``wavelength_axis``
events and the result dicts. Commands complete on a worker thread after
``duration_s`` so the pending/settle/abort paths run for real; errors are the
real ``mast.satellite.errors`` classes, so the error mapping is tested against
the names the hub actually raises.

    hub = FakeSatelliteHub()
    hub.add_device("rig1", "pol", "rotation_stage")
    set_hub_provider(lambda: hub)
"""
from __future__ import annotations

import base64
import threading
import time
import uuid
from typing import Any, Callable

import numpy as np

from mast.satellite.errors import (
    SatelliteCommandError,
    SatelliteTimeout,
    SatelliteUnavailable,
)


def b64(arr: np.ndarray, dtype: str) -> str:
    dt = {"u16": "<u2", "f32": "<f4", "f64": "<f8"}[dtype]
    return base64.b64encode(np.asarray(arr).astype(dt).tobytes()).decode("ascii")


class FakePending:
    def __init__(self, hub: "FakeSatelliteHub", sat: str, dev: str, action: str,
                 args: dict, timeout_s: float, cmd_id: str):
        self.hub, self.sat_id, self.device, self.action = hub, sat, dev, action
        self.args, self.timeout_s, self.id = dict(args), float(timeout_s), cmd_id
        self._event = threading.Event()
        self._value: Any = None
        self._exc: Exception | None = None
        self.cancelled = False
        self.deadline = time.monotonic() + self.timeout_s + 0.5

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def finish(self, value: Any = None, exc: Exception | None = None) -> None:
        if not self._event.is_set():
            self._value, self._exc = value, exc
            self._event.set()

    def wait(self, timeout_s: float | None = None, check_abort: Callable | None = None):
        limit = None if timeout_s is None else time.monotonic() + float(timeout_s)
        while True:
            if check_abort is not None:
                try:
                    check_abort()
                except BaseException:
                    self.cancel()
                    raise
            if self.done:
                break
            now = time.monotonic()
            if now >= self.deadline:
                self.finish(exc=SatelliteTimeout("expired", sat_id=self.sat_id, cmd_id=self.id))
                break
            if limit is not None and now >= limit:
                raise SatelliteTimeout("wait timed out", sat_id=self.sat_id, cmd_id=self.id,
                                       command_expired=False)
            self._event.wait(0.01)
        if self._exc is not None:
            raise self._exc
        return self._value

    def cancel(self) -> None:
        self.cancelled = True
        self.hub.cancels.append(self.id)
        self.finish(exc=SatelliteCommandError("cancelled", "cancelled by hub",
                                              sat_id=self.sat_id, cmd_id=self.id))


class FakeDevice:
    def __init__(self, hub: "FakeSatelliteHub", sat: str, dev: str, kind: str, **opts):
        self.hub, self.sat, self.id, self.kind = hub, sat, dev, kind
        self.opts = opts
        self.state = "ready"
        self.duration_s = float(opts.get("duration_s", 0.05))
        self.not_supported = set(opts.get("not_supported", ()))
        # rotation
        self.position_deg = float(opts.get("position_deg", 0.0))
        self.homed = bool(opts.get("homed", True))
        # xyz
        self.pos = dict(opts.get("pos", {"x": 5.0, "y": 2.5, "z": 4.0}))
        self.limits = opts.get("limits_mm", {"x": [-2.0, 13.3], "y": [0.0, 5.0], "z": [0.0, 9.6]})
        self.max_step = float(opts.get("max_step_mm", 0.5))
        # pico
        self.axes = {str(i): 0 for i in range(1, int(opts.get("n_axes", 4)) + 1)}
        self.max_steps = int(opts.get("max_steps_per_cmd", 2000))
        # spectrometer
        self.n_px = int(opts.get("n_px", 64))
        self.axis_ver = 1
        self.center = 664.55
        self.laser = float(opts.get("laser_line_nm", 633.0))
        self.settings = {"exposure_ms": 10.0, "frames": 1}
        self.running = False
        self.extra_frames: list[dict] = []   # injected foreign/duplicate events

    # -- spectrometer helpers --
    def axis_nm(self) -> np.ndarray:
        return self.center + (np.arange(self.n_px) - self.n_px / 2) * 0.05

    def axis_data(self) -> dict:
        return {"axis_ver": self.axis_ver, "n": self.n_px, "nm_b64": b64(self.axis_nm(), "f64"),
                "laser_line_nm": self.laser, "grating": "[500nm,1200][0][0]",
                "center_nm": self.center}

    def status(self) -> dict:
        if self.kind == "rotation_stage":
            return {"state": self.state, "position_deg": self.position_deg,
                    "moving": self.state == "busy", "homed": self.homed, "enabled": True}
        if self.kind == "xyz_stage":
            return {"state": self.state, "x_mm": self.pos["x"], "y_mm": self.pos["y"],
                    "z_mm": self.pos["z"], "moving": self.state == "busy"}
        if self.kind == "openloop_stepper":
            return {"state": self.state,
                    "axes": {a: {"steps": s, "moving": False} for a, s in self.axes.items()}}
        if self.kind == "spectrometer":
            return {"state": "busy" if self.running else "ready", "is_running": self.running,
                    "temperature_c": -120.0, "temperature_locked": True,
                    "exposure_ms": self.settings["exposure_ms"], "frames": self.settings["frames"],
                    "grating": "[500nm,1200][0][0]", "center_wavelength_nm": self.center,
                    "exit_port": "front", "laser_line_nm": self.laser, "operator_hold": False}
        return {"state": self.state}

    def run(self, action: str, args: dict, pending: FakePending) -> None:
        """Runs on a worker thread; finishes *pending*."""
        try:
            if action in self.not_supported:
                raise SatelliteCommandError("not_supported", f"{action} not supported",
                                            sat_id=self.sat)
            if action == "status":
                pending.finish(self.status())
                return
            if action == "stop":
                self.hub.stops.append((self.sat, self.id, dict(args)))
                self.hub._stop_flags.add((self.sat, self.id))
                pending.finish({"stopped": True})
                return
            if action == "wavelength_axis":
                pending.finish(self.axis_data())
                return
            if action == "acquire":
                self._acquire(args, pending)
                return
            if action == "configure":
                self.settings.update({k: v for k, v in args.items()
                                      if k in ("exposure_ms", "frames")})
                pending.finish(self.status())
                return
            if action == "set_center_wavelength":
                self.center = float(args["nm"])
                self.axis_ver += 1
                self.hub.emit(self.sat, self.id, "wavelength_axis", self.axis_data())
                pending.finish(self.status())
                return
            if action in ("set_grating", "set_exit_port", "set", "read", "set_hv", "hv_off"):
                pending.finish({"ok": True, "action": action, **args})
                return
            if action in ("move_abs", "home", "jog", "set_zero", "move_rel"):
                self._motion(action, args, pending)
                return
            raise SatelliteCommandError("invalid_args", f"unknown action {action}",
                                        sat_id=self.sat)
        except Exception as exc:  # noqa: BLE001
            pending.finish(exc=exc)

    def _sleep(self, pending: FakePending) -> bool:
        end = time.monotonic() + self.duration_s
        while time.monotonic() < end:
            if pending.cancelled or self.hub.stopped_flag(self.sat, self.id):
                return False
            time.sleep(0.005)
        return True

    def _motion(self, action: str, args: dict, pending: FakePending) -> None:
        self.state = "busy"
        try:
            if self.kind == "rotation_stage":
                target = 0.0 if action == "home" else float(args["deg"])
                lo, hi = self.opts.get("min_deg", -360.0), self.opts.get("max_deg", 720.0)
                if not (lo <= target <= hi):
                    raise SatelliteCommandError("limit", f"{target} out of range", sat_id=self.sat)
                if not self._sleep(pending):
                    raise SatelliteCommandError("cancelled", "stopped", sat_id=self.sat)
                self.position_deg = target
                if action == "home":
                    self.homed = True
            elif self.kind == "xyz_stage":
                for ax, v in args.items():
                    lo, hi = self.limits[ax]
                    if not (lo <= float(v) <= hi):
                        raise SatelliteCommandError("limit", f"{ax}={v} out of range",
                                                    sat_id=self.sat)
                    if abs(float(v) - self.pos[ax]) > self.max_step + 1e-12:
                        raise SatelliteCommandError("limit", f"{ax} step too big", sat_id=self.sat)
                if not self._sleep(pending):
                    raise SatelliteCommandError("cancelled", "stopped", sat_id=self.sat)
                for ax, v in args.items():
                    self.pos[ax] = float(v)
            elif self.kind == "openloop_stepper":
                ax = str(args.get("axis"))
                if action == "set_zero":
                    self.axes[ax] = 0
                else:
                    steps = int(args["steps"])
                    if abs(steps) > self.max_steps:
                        raise SatelliteCommandError("limit", "too many steps", sat_id=self.sat)
                    if not self._sleep(pending):
                        raise SatelliteCommandError("cancelled", "stopped", sat_id=self.sat)
                    self.axes[ax] += steps
            self.hub.emit(self.sat, self.id, "axis_state", self.status())
            pending.finish(None)
        finally:
            self.state = "ready"

    def _acquire(self, args: dict, pending: FakePending) -> None:
        exp = float(args.get("exposure_ms", self.settings["exposure_ms"]))
        frames = int(args.get("frames", self.settings["frames"]))
        self.running = True
        started = int(time.time() * 1000)
        try:
            for ev in self.extra_frames:
                self.hub.emit(self.sat, self.id, "spectrum", ev)
            for i in range(frames):
                if not self._sleep(pending):
                    raise SatelliteCommandError("cancelled", "acquisition cancelled",
                                                sat_id=self.sat)
                # u16 = hardware-binned rows; f32 = rows summed on the satellite
                # (custom roi with y_bin=1, contract §5.4)
                dtype = str(self.opts.get("frame_dtype", "u16"))
                frame = (np.arange(self.n_px) * 0 + 100 + i * 10).astype(
                    np.uint16 if dtype == "u16" else np.float32)
                frame[self.n_px // 3] = 4000
                data = {"cmd_id": pending.id, "frame_index": i, "n": self.n_px, "dtype": dtype,
                        "data_b64": b64(frame, dtype), "axis_ver": self.axis_ver,
                        "manual": False, "exposure_started_us": 1, "exposure_ended_us": 2}
                self.hub.emit(self.sat, self.id, "spectrum", data)
                if self.opts.get("duplicate_frames"):
                    self.hub.emit(self.sat, self.id, "spectrum", data)
            files = (["D:\\Data\\2026\\20261019\\Raman 1.spe"] if args.get("save") else [])
            pending.finish({"frames": frames, "files": files, "exposure_ms": exp,
                            "started_ms": started, "finished_ms": int(time.time() * 1000),
                            "summary": {"min": 100.0, "max": 4000.0, "mean": 160.0},
                            "axis_ver": self.axis_ver})
        finally:
            self.running = False


class FakeSatelliteHub:
    """Implements the hub surface used by ``mast.instruments.remote`` (§6)."""

    def __init__(self) -> None:
        self.devs: dict[tuple[str, str], FakeDevice] = {}
        self.connected: dict[str, bool] = {}
        self.listeners: dict[int, Callable] = {}
        self._next = 1
        self.calls: list[tuple] = []
        self.stops: list[tuple] = []
        self.cancels: list[str] = []
        self._stop_flags: set[tuple[str, str]] = set()
        self.latest_events: dict[tuple[str, str, str], dict] = {}
        self.offset_ms = 2.5
        self.estops: list[str] = []

    # -- setup --
    def add_device(self, sat: str, dev: str, kind: str, **opts) -> FakeDevice:
        d = FakeDevice(self, sat, dev, kind, **opts)
        self.devs[(sat, dev)] = d
        self.connected.setdefault(sat, True)
        return d

    def stopped_flag(self, sat: str, dev: str) -> bool:
        return (sat, dev) in self._stop_flags

    def emit(self, sat: str, dev: str, kind: str, data: dict) -> None:
        self.latest_events[(sat, dev, kind)] = dict(data)
        evt = {"seq": 0, "device": dev, "kind": kind, "ts_ms": int(time.time() * 1000),
               "data": dict(data)}
        for fn in list(self.listeners.values()):
            fn(sat, evt)

    # -- §6 API --
    def _check(self, sat: str, dev: str) -> FakeDevice:
        if not self.connected.get(sat):
            raise SatelliteUnavailable(f"satellite {sat} not connected", sat_id=sat)
        d = self.devs.get((sat, dev))
        if d is None:
            raise SatelliteCommandError("invalid_args", f"no device {dev}", sat_id=sat)
        return d

    def submit(self, sat: str, dev: str, action: str, args: dict | None = None, *,
               timeout_s: float = 30.0, request_id: str | None = None) -> FakePending:
        self.calls.append((sat, dev, action, dict(args or {})))
        d = self._check(sat, dev)
        if action != "stop":
            self._stop_flags.discard((sat, dev))
        p = FakePending(self, sat, dev, action, args or {}, timeout_s,
                        str(request_id or uuid.uuid4()))
        threading.Thread(target=d.run, args=(action, dict(args or {}), p), daemon=True).start()
        return p

    def call(self, sat: str, dev: str, action: str, args: dict | None = None, *,
             timeout_s: float = 30.0, request_id: str | None = None,
             check_abort: Callable | None = None):
        return self.submit(sat, dev, action, args, timeout_s=timeout_s,
                           request_id=request_id).wait(check_abort=check_abort)

    def stop_device(self, sat: str, dev: str) -> None:
        self.stops.append((sat, dev, {}))
        self._stop_flags.add((sat, dev))

    def emergency_stop_all(self, reason: str = "") -> dict:
        self.estops.append(reason)
        return {"sent": sorted(self.connected), "unreachable": [], "errors": []}

    def latest(self, sat: str, dev: str, kind: str = "device_state") -> dict | None:
        v = self.latest_events.get((sat, dev, kind))
        return dict(v) if v else None

    def add_listener(self, fn: Callable) -> int:
        h = self._next
        self._next += 1
        self.listeners[h] = fn
        return h

    def remove_listener(self, handle: int) -> None:
        self.listeners.pop(handle, None)

    def devices(self, kind: str | None = None) -> list[tuple[str, dict]]:
        out = []
        for (sat, dev), d in sorted(self.devs.items()):
            if kind is None or d.kind == kind:
                out.append((sat, {"id": dev, "kind": d.kind, "state": d.state,
                                  "link_state": "connected" if self.connected.get(sat) else "lost"}))
        return out

    def list_satellites(self) -> list[dict]:
        return [{"id": s, "state": "connected" if up else "lost", "boot_id": "b1",
                 "rtt_ms": 1.5, "clock_offset_ms": self.offset_ms,
                 "devices": [{"id": dev} for (sat, dev) in self.devs if sat == s]}
                for s, up in sorted(self.connected.items())]

    def clock_offset_ms(self, sat: str) -> float | None:
        return self.offset_ms if sat in self.connected else None
