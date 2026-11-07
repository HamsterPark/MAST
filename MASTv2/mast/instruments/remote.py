"""Satellite-backed devices — drivers that talk to the spectroscopy computer's
(satellite's) hardware through the hub on the STM computer.

The hub (``mast.satellite``, contract §6) owns the WebSocket, the journal, the
heartbeat and the boot-id bookkeeping. This module only turns its blocking /
pending-command API into the MAST instrument abstractions, so every existing
optics skill (OpticalStageMove / GetPos / Stop / Scan / Wiggle) drives a remote
axis exactly like a local one, soft limits included:

====================================  ==========================  ===========================
class                                 satellite device kind        abstraction
====================================  ==========================  ===========================
:class:`RemoteMotionController`       ``xyz_stage``                MotionController (x/y/z mm)
:class:`RemoteRotationController`     ``rotation_stage``           MotionController of RotationAxis
:class:`RemoteOpenLoopController`     ``openloop_stepper``         MotionController of OpenLoopAxis
:class:`RemoteSpectrometer`           ``spectrometer``             Spectrometer
:class:`RemoteEndpoint`               anything (aotf, pmt, …)      thin call/stop wrapper
====================================  ==========================  ===========================

Rules carried over from the contract:

* **No token here.** The hub does not hold MAST's instrument token and neither
  does this layer; the calling skill holds it according to its safety level.
* **Stops never raise and never queue behind a move** — ``stop()`` goes straight
  to ``hub.stop_device`` without taking the controller's I/O lock.
* **Both sides keep soft limits.** The satellite enforces its own; the axis
  config here is still Layer-0 for MAST, plus an optional per-command step cap.
* ``check_abort``: the hub wants a callable that *raises* to abort. MAST's
  contexts return a bool. :func:`abort_raiser` adapts one to the other.
* Errors are mapped onto the instrument error family (``SatelliteUnavailable`` →
  :class:`InstrumentUnavailable`, ``code="limit"`` → :class:`TravelLimitError`,
  ``"cancelled"`` → :class:`MotionAborted`, ``SatelliteTimeout`` →
  :class:`MotionTimeout`, anything else → :class:`RemoteCommandError` keeping the
  satellite's ``code``).

Nothing here imports ``mast.satellite`` at module import time: the frozen
build or a hub-less test sees a clean :class:`InstrumentUnavailable` instead.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np

from mast.i18n import tr
from mast.instruments.base import (
    AbortCheck,
    AxisConfig,
    AxisStatus,
    InstrumentError,
    InstrumentUnavailable,
    MotionAborted,
    MotionAxis,
    MotionController,
    MotionTimeout,
    TravelLimitError,
)
from mast.instruments.openloop import OpenLoopAxis
from mast.instruments.rotation import RotationAxis, RotationConfig
from mast.instruments.spectrometer import (
    AcquisitionResult,
    FrameCallback,
    Spectrometer,
    SpectrometerLimits,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

__all__ = [
    "RemoteCommandError",
    "set_hub_provider",
    "get_hub",
    "abort_raiser",
    "map_hub_error",
    "RemoteEndpoint",
    "RemoteMotionController",
    "RemoteXYZAxis",
    "RemoteRotationController",
    "RemoteRotationAxis",
    "RemoteOpenLoopController",
    "RemoteOpenLoopAxis",
    "RemoteSpectrometer",
    "decode_array_b64",
]


# ── hub access (injectable for tests) ──────────────────────────────────────

_HUB_PROVIDER: Callable[[], Any] | None = None


def set_hub_provider(provider: Callable[[], Any] | None) -> None:
    """Install a hub factory (tests, or a hub-less tool). ``None`` restores the
    real ``mast.satellite.get_satellite_hub``."""
    global _HUB_PROVIDER
    _HUB_PROVIDER = provider


def get_hub() -> Any:
    """The process satellite hub, or :class:`InstrumentUnavailable`."""
    if _HUB_PROVIDER is not None:
        return _HUB_PROVIDER()
    try:
        from mast.satellite import get_satellite_hub
    except Exception as exc:  # noqa: BLE001 - absent package / frozen build
        raise InstrumentUnavailable(
            f"satellite hub not available in this build: {type(exc).__name__}: {exc}"
        ) from exc
    return get_satellite_hub()


# ── errors ──────────────────────────────────────────────────────────────────


class RemoteCommandError(InstrumentError):
    """A satellite command came back ``ok=false`` with a code we don't fold
    into a more specific instrument error (``not_supported``, ``device_busy``,
    ``invalid_args``, ``internal`` …)."""

    def __init__(self, code: str, message: str, *, where: str = ""):
        self.code = str(code or "internal")
        self.remote_message = str(message or "")
        super().__init__(f"{where}: [{self.code}] {self.remote_message}".strip(": "))


def _tag(err: InstrumentError, code: str | None) -> InstrumentError:
    try:
        err.remote_code = code  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return err


def map_hub_error(exc: BaseException, *, where: str) -> BaseException:
    """Translate a hub exception into the instrument error family (see module
    docstring). Our own abort exceptions and non-Exception BaseExceptions
    (KeyboardInterrupt, GraphInterrupt …) pass through untouched."""
    if isinstance(exc, InstrumentError):
        return exc
    if not isinstance(exc, Exception):
        return exc
    name = type(exc).__name__
    code = getattr(exc, "code", None)
    msg = str(getattr(exc, "message", "") or exc)
    if name == "SatelliteUnavailable":
        return _tag(InstrumentUnavailable(f"{where}: satellite unavailable — {msg}"), "unavailable")
    if name == "SatelliteTimeout":
        return _tag(MotionTimeout(f"{where}: no result within the timeout — {msg}"), "timeout")
    if code is not None or name == "SatelliteCommandError":
        c = str(code or "internal")
        if c == "limit":
            return _tag(TravelLimitError(f"{where}: refused by the satellite's limit — {msg}"), c)
        if c == "cancelled":
            return _tag(MotionAborted(f"{where}: cancelled"), c)
        if c == "timeout":
            return _tag(MotionTimeout(f"{where}: timed out on the satellite — {msg}"), c)
        if c in ("unavailable", "operator_hold", "lost_on_restart"):
            hint = {"operator_hold": tr("该设备在卫星上被置为「人工接管」", "this device is set to \"manual takeover\" on the satellite"),
                    "lost_on_restart": tr("卫星重启过，这条命令的结局丢失（绝不自动重放）", "the satellite restarted and the outcome of this command is lost (never replayed automatically)")}.get(c, "")
            return _tag(InstrumentUnavailable(f"{where}: [{c}] {msg} {hint}".strip()), c)
        return _tag(RemoteCommandError(c, msg, where=where), c)
    return _tag(InstrumentError(f"{where}: {name}: {msg}"), None)


def _reraise(exc: BaseException, where: str):
    """Raise the mapped form of *exc*; our own exceptions go out unchanged."""
    mapped = map_hub_error(exc, where=where)
    if mapped is exc:
        raise exc
    raise mapped from exc


def abort_raiser(check_abort: AbortCheck | None,
                 extra: Callable[[], None] | None = None) -> Callable[[], None] | None:
    """Adapt a MAST abort predicate (returns True) to the hub's contract (raises).

    *extra* runs first on every tick (frame draining, current sampling) — the hub
    calls the result about every 0.1 s on the waiting thread."""
    if check_abort is None and extra is None:
        return None

    def _tick() -> None:
        if extra is not None:
            extra()
        if check_abort is not None and check_abort():
            raise MotionAborted("aborted by operator")

    return _tick


def decode_array_b64(data_b64: str, dtype: str, n: int | None = None) -> np.ndarray:
    """Little-endian base64 array (contract §5.4: ``u16`` / ``f32`` / ``f64``)."""
    dt = {"u16": "<u2", "f32": "<f4", "f64": "<f8", "i32": "<i4", "u32": "<u4"}.get(
        str(dtype).lower(), str(dtype))
    raw = base64.b64decode(data_b64 or "")
    arr = np.frombuffer(raw, dtype=np.dtype(dt)).copy()
    if n is not None and int(n) != arr.size:
        raise InstrumentError(f"array length {arr.size} != declared n={n}")
    return arr


# ── endpoint: one satellite device ─────────────────────────────────────────


class RemoteEndpoint:
    """``(satellite, device)`` plus the hub calls every driver needs."""

    def __init__(self, satellite: str, device: str):
        if not satellite or not device:
            raise ValueError("RemoteEndpoint needs both satellite and device ids")
        self.satellite = str(satellite)
        self.device = str(device)

    @property
    def label(self) -> str:
        return f"{self.satellite}/{self.device}"

    def call(self, action: str, args: dict | None = None, *, timeout_s: float = 30.0,
             check_abort: Callable[[], None] | None = None) -> dict:
        where = f"{self.label} {action}"
        try:
            hub = get_hub()
            value = hub.call(self.satellite, self.device, action, dict(args or {}),
                             timeout_s=float(timeout_s), check_abort=check_abort)
        except BaseException as exc:  # noqa: BLE001 - mapped below
            _reraise(exc, where)
        return dict(value) if isinstance(value, dict) else {"value": value}

    def submit(self, action: str, args: dict | None = None, *, timeout_s: float = 30.0,
               request_id: str | None = None) -> Any:
        where = f"{self.label} {action}"
        try:
            return get_hub().submit(self.satellite, self.device, action, dict(args or {}),
                                    timeout_s=float(timeout_s), request_id=request_id)
        except BaseException as exc:  # noqa: BLE001
            _reraise(exc, where)

    def latest(self, kind: str = "device_state") -> dict | None:
        try:
            v = get_hub().latest(self.satellite, self.device, kind)
        except Exception:  # noqa: BLE001 - a read of a cache never breaks anything
            return None
        return dict(v) if isinstance(v, dict) else None

    def stop(self, args: dict | None = None) -> None:
        """Best-effort stop. Never raises, never waits for a result. ``args``
        (e.g. ``{"axis": 2}``) sends a targeted ``stop``; without it the hub's
        ``stop_device`` stops the whole device."""
        try:
            hub = get_hub()
        except Exception:  # noqa: BLE001
            return
        try:
            if args:
                hub.submit(self.satellite, self.device, "stop", dict(args), timeout_s=5.0)
            else:
                hub.stop_device(self.satellite, self.device)
        except Exception as exc:  # noqa: BLE001 - panic path
            logger.warning("remote stop %s failed: %s", self.label, exc)
            if args:
                try:
                    hub.stop_device(self.satellite, self.device)
                except Exception:  # noqa: BLE001
                    pass

    def descriptor(self) -> dict | None:
        """The hub's device descriptor, or None when the hub does not list it."""
        try:
            for sat, desc in get_hub().devices() or []:
                if str(sat) == self.satellite and str((desc or {}).get("id")) == self.device:
                    return dict(desc)
        except Exception:  # noqa: BLE001
            return None
        return None

    def require_present(self) -> dict:
        """Descriptor, or :class:`InstrumentUnavailable` naming what is missing."""
        try:
            hub = get_hub()
            listed = list(hub.devices() or [])
        except Exception as exc:  # noqa: BLE001
            _reraise(exc, self.label)
        for sat, desc in listed:
            if str(sat) == self.satellite and str((desc or {}).get("id")) == self.device:
                state = str((desc or {}).get("state") or "")
                if state in ("offline", "not_started"):
                    raise InstrumentUnavailable(f"{self.label}: device state is {state!r}")
                return dict(desc)
        sats = sorted({str(s) for s, _ in listed})
        raise InstrumentUnavailable(
            f"{self.label}: not listed by the satellite hub (satellites with devices: "
            f"{sats or 'none'}) — satellite not connected, or wrong device id")

    def clock(self) -> dict:
        """``{clock_offset_ms, rtt_ms, boot_id, state}`` as far as the hub knows."""
        out: dict[str, Any] = {"clock_offset_ms": None, "rtt_ms": None,
                               "boot_id": None, "state": None}
        try:
            hub = get_hub()
            out["clock_offset_ms"] = hub.clock_offset_ms(self.satellite)
            for s in hub.list_satellites() or []:
                if str(s.get("id")) == self.satellite:
                    out["rtt_ms"] = s.get("rtt_ms")
                    out["boot_id"] = s.get("boot_id")
                    out["state"] = s.get("state")
                    break
        except Exception:  # noqa: BLE001
            pass
        return out


# ── shared machinery for remote motion ───────────────────────────────────────


class _RemoteMotionBase(MotionController):
    """One satellite motion device. At most one motion command in flight."""

    #: status reads closer together than this reuse the last answer
    STATUS_TTL_S = 0.15

    def __init__(self, *, satellite: str, device: str, axes: list[AxisConfig],
                 move_timeout_s: float = 120.0, status_timeout_s: float = 10.0):
        super().__init__()
        self.endpoint = RemoteEndpoint(satellite, device)
        self._axis_configs = list(axes or [])
        if not self._axis_configs:
            raise ValueError(f"{self.endpoint.label}: configure at least one axis")
        self.move_timeout_s = float(move_timeout_s)
        self.status_timeout_s = float(status_timeout_s)
        self._pending: Any = None
        self._pending_lock = threading.Lock()
        self._status: dict = {}
        self._status_t = 0.0

    # lifecycle
    def _connect_raw(self) -> None:
        self.endpoint.require_present()

    def _close_raw(self) -> None:
        pass

    # status
    def status(self, *, max_age_s: float | None = None) -> dict:
        ttl = self.STATUS_TTL_S if max_age_s is None else float(max_age_s)
        now = time.monotonic()
        if self._status and now - self._status_t <= ttl:
            return dict(self._status)
        st = self.endpoint.call("status", timeout_s=self.status_timeout_s)
        self._status, self._status_t = st, time.monotonic()
        return dict(st)

    def live_status(self) -> dict:
        """Latest ``axis_state`` event (no network), else the cached status."""
        ev = self.endpoint.latest("axis_state")
        return ev if ev else dict(self._status)

    # motion commands
    def submit_motion(self, action: str, args: dict, *, timeout_s: float | None = None) -> None:
        self.settle_previous()
        p = self.endpoint.submit(action, args, timeout_s=timeout_s or self.move_timeout_s)
        with self._pending_lock:
            self._pending = p
        self._status_t = 0.0

    def settle_previous(self, timeout_s: float | None = None) -> None:
        """Let an earlier ``wait=False`` command finish before a new one starts."""
        with self._pending_lock:
            p = self._pending
        if p is None:
            return
        try:
            p.wait(timeout_s=timeout_s or self.move_timeout_s)
        except BaseException as exc:  # noqa: BLE001
            logger.info("%s: previous command ended with %s", self.endpoint.label, exc)
        finally:
            with self._pending_lock:
                if self._pending is p:
                    self._pending = None

    def pending_state(self) -> tuple[bool, dict | None]:
        """``(still_running, finished_value)``. Raises the command's error once."""
        with self._pending_lock:
            p = self._pending
        if p is None:
            return False, None
        if not bool(getattr(p, "done", False)):
            return True, None
        with self._pending_lock:
            if self._pending is p:
                self._pending = None
        try:
            value = p.wait(timeout_s=5.0)
        except BaseException as exc:  # noqa: BLE001
            _reraise(exc, f"{self.endpoint.label} motion")
        self._status_t = 0.0
        return False, dict(value) if isinstance(value, dict) else None

    def remote_stop(self, args: dict | None = None) -> None:
        with self._pending_lock:
            p, self._pending = self._pending, None
        if p is not None:
            try:
                p.cancel()
            except Exception:  # noqa: BLE001
                pass
        self.endpoint.stop(args)
        self._status_t = 0.0

    def stop_all(self) -> None:
        self.remote_stop()


class _RemoteAxisMixin:
    """Status/stop behaviour shared by the three remote axis kinds."""

    POLL_INTERVAL_S = 0.1
    _controller: _RemoteMotionBase
    key: str

    def _remote_position(self, st: dict) -> float:
        raise NotImplementedError

    def _get_status_raw(self) -> AxisStatus:  # type: ignore[override]
        ctrl = self._controller
        running, _value = ctrl.pending_state()
        if running:
            live = ctrl.live_status()
            try:
                pos = self._remote_position(live)
            except Exception:  # noqa: BLE001 - no position yet
                pos = float("nan")
            return AxisStatus(position=pos, moving=True, on_target=False,
                              raw={"pending": True, **live})
        st = ctrl.status(max_age_s=0.0 if _value is not None else None)
        moving = bool(st.get("moving", False))
        homed = st.get("homed")
        return AxisStatus(position=self._remote_position(st), moving=moving,
                          on_target=None if self.KIND == "openloop" else (not moving),  # type: ignore[attr-defined]
                          homed=(bool(homed) if homed is not None else None), raw=st)

    def _get_position_raw(self) -> float:
        return self._remote_position(self._controller.status())

    def stop(self) -> None:  # type: ignore[override]
        # No I/O lock: a stop must not queue behind a status poll or a move.
        self._controller.remote_stop()

    def _stop_raw(self) -> None:
        self._controller.remote_stop()

    def wait_until_settled(self, target: float, *, timeout: float | None = None,
                           check_abort: AbortCheck | None = None) -> AxisStatus:
        try:
            return super().wait_until_settled(target, timeout=timeout,  # type: ignore[misc]
                                              check_abort=check_abort)
        except MotionTimeout:
            # Across a network a move that missed its deadline is stopped rather
            # than left running with nobody watching it.
            self._controller.remote_stop()
            raise


# ── XYZ stage (Motion_Manipulator / UIRobot, mm) ─────────────────────────────


class RemoteXYZAxis(_RemoteAxisMixin, MotionAxis):
    """One axis of the satellite XYZ stage. ``channel`` (or the name) selects
    ``x`` / ``y`` / ``z``; positions are millimetres."""

    def __init__(self, config: AxisConfig, controller: "RemoteMotionController", *,
                 max_step: float | None = None):
        super().__init__(config, controller)
        key = str(config.channel if isinstance(config.channel, str) and config.channel
                  else config.name).strip().lower()
        if key not in ("x", "y", "z"):
            raise ValueError(f"XYZ axis {config.name!r}: channel must be x, y or z")
        self.key = key
        self._max_step = float(max_step) if max_step else None

    def _remote_position(self, st: dict) -> float:
        return float(st[f"{self.key}_mm"])

    def move_abs(self, position: float, *, wait: bool = True,
                 timeout: float | None = None,
                 check_abort: AbortCheck | None = None) -> AxisStatus:
        target = float(position)
        self._check_limits(target)
        if self._max_step is not None:
            cur = self.get_position()
            if abs(target - cur) > self._max_step + 1e-12:
                raise TravelLimitError(
                    f"axis {self.config.name!r}: step {target - cur:+.4g} "
                    f"{self.config.unit} exceeds the per-command cap of ±{self._max_step:g} "
                    f"{self.config.unit} — move in smaller steps")
        return super().move_abs(target, wait=wait, timeout=timeout, check_abort=check_abort)

    def _move_abs_raw(self, target: float) -> None:
        self._controller.submit_motion("move_abs", {self.key: float(target)})


class RemoteMotionController(_RemoteMotionBase):
    """Satellite ``xyz_stage`` (contract §5.4)."""

    def __init__(self, *, satellite: str, device: str, axes: list[AxisConfig],
                 max_step: float | None = None, move_timeout_s: float = 120.0,
                 status_timeout_s: float = 10.0):
        super().__init__(satellite=satellite, device=device, axes=axes,
                         move_timeout_s=move_timeout_s, status_timeout_s=status_timeout_s)
        self._max_step = max_step

    def _build_axes(self) -> dict[str, MotionAxis]:
        return {c.name: RemoteXYZAxis(c, self, max_step=self._max_step)
                for c in self._axis_configs}


# ── rotation stage (KDC101 + PRM1-Z8, degrees) ───────────────────────────────


class RemoteRotationAxis(_RemoteAxisMixin, RotationAxis):
    SUPPORTS = frozenset({"home"})

    def __init__(self, config: AxisConfig, controller: "RemoteRotationController", *,
                 rotation: RotationConfig | None = None, home_timeout_s: float = 90.0):
        super().__init__(config, controller, rotation=rotation)
        self.key = "deg"
        self._home_timeout = float(home_timeout_s)

    def _remote_position(self, st: dict) -> float:
        return float(st["position_deg"])

    def _move_abs_raw(self, target: float) -> None:
        self._controller.submit_motion("move_abs", {"deg": float(target)})

    def _home_raw(self) -> None:
        self._controller.submit_motion("home", {}, timeout_s=self._home_timeout)

    def home(self, *, wait: bool = True, timeout: float | None = None,
             check_abort: AbortCheck | None = None) -> AxisStatus:
        return super().home(wait=wait, timeout=timeout or self._home_timeout,
                            check_abort=check_abort)


class RemoteRotationController(_RemoteMotionBase):
    """Satellite ``rotation_stage``: one axis."""

    def __init__(self, *, satellite: str, device: str, axes: list[AxisConfig],
                 rotation: RotationConfig | None = None, move_timeout_s: float = 60.0,
                 home_timeout_s: float = 90.0, status_timeout_s: float = 10.0):
        super().__init__(satellite=satellite, device=device, axes=axes,
                         move_timeout_s=move_timeout_s, status_timeout_s=status_timeout_s)
        if len(self._axis_configs) != 1:
            raise ValueError(f"{self.endpoint.label}: a rotation stage has exactly one axis")
        self._rotation = rotation
        self._home_timeout = float(home_timeout_s)

    def _build_axes(self) -> dict[str, MotionAxis]:
        c = self._axis_configs[0]
        return {c.name: RemoteRotationAxis(c, self, rotation=self._rotation,
                                           home_timeout_s=self._home_timeout)}


# ── open-loop stepper (Picomotor 8742, steps) ───────────────────────────────


class RemoteOpenLoopAxis(_RemoteAxisMixin, OpenLoopAxis):
    """One Picomotor channel. The step counter lives on the satellite."""

    def __init__(self, config: AxisConfig, controller: "RemoteOpenLoopController", *,
                 max_steps_per_cmd: int):
        super().__init__(config, controller, max_steps_per_cmd=max_steps_per_cmd)
        self.key = str(config.channel)

    def _wire_axis(self) -> Any:
        return int(self.key) if self.key.isdigit() else self.key

    def _remote_position(self, st: dict) -> float:
        axes = st.get("axes") or {}
        entry = axes.get(self.key)
        if entry is None and self.key.isdigit():
            entry = axes.get(int(self.key))
        if not isinstance(entry, dict) or "steps" not in entry:
            raise InstrumentError(f"{self._controller.endpoint.label}: status has no "
                                  f"axis {self.key!r} (axes: {sorted(map(str, axes))})")
        return float(entry["steps"])

    def _get_status_raw(self) -> AxisStatus:
        ctrl = self._controller
        running, value = ctrl.pending_state()
        if running:
            return AxisStatus(position=float("nan"), moving=True, on_target=None,
                              raw={"pending": True, "feedback": False})
        st = ctrl.status(max_age_s=0.0 if value is not None else None)
        axes = st.get("axes") or {}
        entry = axes.get(self.key) or (axes.get(int(self.key)) if self.key.isdigit() else None) or {}
        return AxisStatus(position=self._remote_position(st),
                          moving=bool(entry.get("moving", False)), on_target=None,
                          raw={"feedback": False, **entry})

    def _jog_raw(self, steps: int) -> None:
        self._controller.submit_motion("jog", {"axis": self._wire_axis(), "steps": int(steps)})

    def _get_steps_raw(self) -> int:
        return int(self._remote_position(self._controller.status()))

    def _set_zero_raw(self) -> None:
        self._controller.endpoint.call("set_zero", {"axis": self._wire_axis()},
                                       timeout_s=self._controller.status_timeout_s)
        self._controller._status_t = 0.0

    def stop(self) -> None:  # type: ignore[override]
        self._controller.remote_stop({"axis": self._wire_axis()})


class RemoteOpenLoopController(_RemoteMotionBase):
    """Satellite ``openloop_stepper``."""

    def __init__(self, *, satellite: str, device: str, axes: list[AxisConfig],
                 max_steps_per_cmd: int = 2000, move_timeout_s: float = 60.0,
                 status_timeout_s: float = 10.0):
        super().__init__(satellite=satellite, device=device, axes=axes,
                         move_timeout_s=move_timeout_s, status_timeout_s=status_timeout_s)
        self._max_steps = int(max_steps_per_cmd)

    def _build_axes(self) -> dict[str, MotionAxis]:
        return {c.name: RemoteOpenLoopAxis(c, self, max_steps_per_cmd=self._max_steps)
                for c in self._axis_configs}


# ── spectrometer (LightField) ──────────────────────────────────────────────


class _FrameCollector:
    """Listener side: runs on the hub thread and ONLY enqueues (contract §6)."""

    def __init__(self, satellite: str, device: str, cmd_id: str):
        self.satellite, self.device, self.cmd_id = satellite, device, cmd_id
        self._q: list[tuple[str, dict, int]] = []
        self._lock = threading.Lock()

    def __call__(self, sat_id: str, evt: dict) -> None:
        try:
            if str(sat_id) != self.satellite or str(evt.get("device")) != self.device:
                return
            kind = str(evt.get("kind") or "")
            if kind not in ("spectrum", "wavelength_axis", "acq_progress",
                            "acq_started", "acq_completed"):
                return
            data = evt.get("data") or {}
            if kind == "spectrum" and str(data.get("cmd_id") or "") != self.cmd_id:
                return
            with self._lock:
                self._q.append((kind, dict(data), int(evt.get("ts_ms") or 0)))
        except Exception:  # noqa: BLE001 - never raise into the hub thread
            pass

    def take(self) -> list[tuple[str, dict, int]]:
        with self._lock:
            items, self._q = self._q, []
        return items


class RemoteSpectrometer(Spectrometer):
    """Satellite ``spectrometer`` (LightField). Frames arrive as ``spectrum``
    events tagged with our ``cmd_id``; the wavelength axis as ``wavelength_axis``
    events keyed by ``axis_ver``."""

    #: how long to wait after the result for frames still in flight
    FRAME_GRACE_S = 3.0

    def __init__(self, *, satellite: str, device: str, device_id: str = "",
                 name: str = "", limits: SpectrometerLimits | None = None,
                 laser_line_nm: float | None = None, status_timeout_s: float = 10.0,
                 setting_timeout_s: float = 120.0):
        super().__init__(device_id=device_id or f"{satellite}/{device}", name=name,
                         limits=limits, laser_line_nm=laser_line_nm)
        self.endpoint = RemoteEndpoint(satellite, device)
        self.status_timeout_s = float(status_timeout_s)
        self.setting_timeout_s = float(setting_timeout_s)
        self._axes: dict[int, np.ndarray] = {}
        self._axis_meta: dict[int, dict] = {}
        self._pending: Any = None

    # -- reads ---------------------------------------------------------------

    def _status_raw(self) -> dict:
        st = self.endpoint.call("status", timeout_s=self.status_timeout_s)
        if st.get("laser_line_nm") and self.laser_line_nm is None:
            self.laser_line_nm = float(st["laser_line_nm"])
        return st

    def _remember_axis(self, data: dict) -> int | None:
        try:
            if data.get("nm_b64"):
                nm = decode_array_b64(data["nm_b64"], "f64", data.get("n"))
            elif data.get("nm") is not None:
                nm = np.asarray(data["nm"], dtype=np.float64)
            else:
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: undecodable wavelength axis: %s", self.endpoint.label, exc)
            return None
        ver = int(data.get("axis_ver") or 0)
        self._axes[ver] = nm
        self._axis_meta[ver] = {k: data.get(k) for k in ("laser_line_nm", "grating", "center_nm")}
        if data.get("laser_line_nm"):
            self.laser_line_nm = float(data["laser_line_nm"])
        return ver

    def _wavelength_axis_raw(self):
        data = self.endpoint.call("wavelength_axis", timeout_s=self.status_timeout_s)
        ver = self._remember_axis(data)
        return None if ver is None else self._axes[ver]

    def axis_for(self, axis_ver: int | None) -> np.ndarray | None:
        if axis_ver is not None and int(axis_ver) in self._axes:
            return self._axes[int(axis_ver)]
        try:
            ax = self._wavelength_axis_raw()
        except InstrumentError:
            return None
        if axis_ver is not None and int(axis_ver) in self._axes:
            return self._axes[int(axis_ver)]
        return ax

    # -- writes ---------------------------------------------------------------

    def _configure_raw(self, settings: dict) -> dict:
        return self.endpoint.call("configure", settings, timeout_s=self.setting_timeout_s)

    def _set_grating_raw(self, grating: str) -> dict:
        return self.endpoint.call("set_grating", {"grating": grating},
                                  timeout_s=self.setting_timeout_s)

    def _set_center_raw(self, nm: float) -> dict:
        return self.endpoint.call("set_center_wavelength", {"nm": float(nm)},
                                  timeout_s=self.setting_timeout_s)

    def _set_exit_port_raw(self, port: str) -> dict:
        return self.endpoint.call("set_exit_port", {"port": port},
                                  timeout_s=self.setting_timeout_s)

    def _stop_raw(self) -> None:
        p, self._pending = self._pending, None
        if p is not None:
            try:
                p.cancel()
            except Exception:  # noqa: BLE001
                pass
        self.endpoint.stop()

    # -- acquire ----------------------------------------------------------------

    def _acquire_raw(self, *, exposure_ms, frames, save, base_name, tag, meta,
                     timeout_s, check_abort, on_frame) -> AcquisitionResult:
        cmd_id = str(uuid.uuid4())
        args: dict[str, Any] = {"save": bool(save)}
        if exposure_ms is not None:
            args["exposure_ms"] = float(exposure_ms)
        if frames is not None:
            args["frames"] = int(frames)
        if base_name:
            args["base_name"] = str(base_name)
        if tag:
            args["tag"] = str(tag)
        if meta:
            args["meta"] = dict(meta)

        collector = _FrameCollector(self.endpoint.satellite, self.endpoint.device, cmd_id)
        got: dict[int, np.ndarray] = {}
        frame_meta: dict[int, dict] = {}
        progress: list[dict] = []

        def drain() -> None:
            for kind, data, ts_ms in collector.take():
                if kind == "wavelength_axis":
                    self._remember_axis(data)
                elif kind == "spectrum":
                    try:
                        idx = int(data.get("frame_index", len(got)))
                        if idx in got:
                            continue          # re-sent after a reconnect
                        arr = decode_array_b64(data.get("data_b64", ""),
                                               data.get("dtype", "u16"), data.get("n"))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("%s: bad spectrum frame: %s", self.endpoint.label, exc)
                        continue
                    got[idx] = arr
                    # dtype says how the rows were combined: u16 = binned in
                    # hardware, f32 = summed on the satellite (§5.4)
                    frame_meta[idx] = {k: data.get(k) for k in (
                        "frame_tracking", "exposure_started_us", "exposure_ended_us",
                        "axis_ver", "manual")}
                    frame_meta[idx]["dtype"] = str(data.get("dtype", "u16"))
                    frame_meta[idx]["ts_sat_ms"] = ts_ms
                    if on_frame is not None:
                        try:
                            on_frame(idx, arr, self._axes.get(int(data.get("axis_ver") or -1)))
                        except Exception as exc:  # noqa: BLE001 - a viewer never breaks a run
                            logger.debug("on_frame callback failed: %s", exc)
                else:
                    progress.append({"kind": kind, **{k: v for k, v in data.items()
                                                       if not str(k).endswith("_b64")}})

        tick = abort_raiser(check_abort, extra=drain)
        hub = get_hub()
        handle = None
        if not self._axes:
            # Seed the axis cache so frames can be drawn as they arrive: the
            # hub's latest wavelength_axis event costs nothing; failing that one
            # wavelength_axis call. Neither is allowed to stop the acquisition.
            seeded = self.endpoint.latest("wavelength_axis")
            if seeded:
                self._remember_axis(seeded)
            else:
                try:
                    self._wavelength_axis_raw()
                except InstrumentError as exc:
                    logger.debug("%s: no wavelength axis before acquiring: %s",
                                 self.endpoint.label, exc)
        started_local = utc_now_iso()
        try:
            handle = hub.add_listener(collector)
            self._pending = self.endpoint.submit("acquire", args, timeout_s=timeout_s,
                                                 request_id=cmd_id)
            try:
                value = self._pending.wait(timeout_s=float(timeout_s) + 10.0,
                                           check_abort=tick)
            except BaseException as exc:  # noqa: BLE001
                _reraise(exc, f"{self.endpoint.label} acquire")
            value = dict(value) if isinstance(value, dict) else {}
            expected = int(value.get("frames") or (frames or 0) or 0)
            deadline = time.monotonic() + self.FRAME_GRACE_S
            drain()
            while len(got) < expected and time.monotonic() < deadline:
                time.sleep(0.05)
                drain()
        finally:
            self._pending = None
            if handle is not None:
                try:
                    hub.remove_listener(handle)
                except Exception:  # noqa: BLE001
                    pass
        finished_local = utc_now_iso()

        order = sorted(got)
        axis_vers = {frame_meta[i].get("axis_ver") for i in order} - {None}
        axis_ver = next(iter(axis_vers)) if len(axis_vers) == 1 else None
        axis_nm = self.axis_for(axis_ver)
        clock = self.endpoint.clock()
        offset = clock.get("clock_offset_ms")
        meta_out = {
            "cmd_id": cmd_id, "satellite": self.endpoint.satellite,
            "device": self.endpoint.device, "result": _light(value),
            "frames_expected": expected, "frames_received": len(order),
            "missing_frames": [i for i in range(expected) if i not in got],
            "axis_ver": axis_ver, "axis_versions_seen": sorted(axis_vers),
            "axis_meta": self._axis_meta.get(int(axis_ver)) if axis_ver is not None else None,
            "frame_meta": [frame_meta[i] for i in order],
            "progress": progress[-20:], **clock,
        }
        return AcquisitionResult(
            frames=[got[i] for i in order], axis_nm=axis_nm,
            files=_files_of(value),
            started_utc=_sat_ms_to_utc(value.get("started_ms"), offset) or started_local,
            finished_utc=_sat_ms_to_utc(value.get("finished_ms"), offset) or finished_local,
            meta=meta_out)


def _light(value: dict) -> dict:
    """The result dict without anything bulky."""
    return {k: v for k, v in (value or {}).items() if not str(k).endswith("_b64")}


def _files_of(value: dict) -> list[dict]:
    out: list[dict] = []
    for f in (value or {}).get("files") or []:
        if isinstance(f, dict):
            out.append(dict(f))
        elif f:
            out.append({"path": str(f)})
    return out


def _sat_ms_to_utc(ts_ms: Any, offset_ms: Any) -> str:
    """Satellite unix-ms → hub-clock ISO UTC (``ts_hub = ts_sat − θ``, §5.3)."""
    try:
        t = float(ts_ms)
    except (TypeError, ValueError):
        return ""
    try:
        t -= float(offset_ms) if offset_ms is not None else 0.0
    except (TypeError, ValueError):
        pass
    return datetime.fromtimestamp(t / 1000.0, tz=timezone.utc).isoformat(
        timespec="milliseconds")
