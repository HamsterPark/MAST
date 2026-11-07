"""InstrumentRegistry — the single lookup point for optical-bench devices.

Skills and GUI panels obtain hardware exclusively through the module-level
singleton (:func:`get_instrument_registry`) — the same pattern as
``mast.io.plan_overlay``. Nothing here is stored in agent checkpoints
(controllers hold sockets/locks → the no-tensor/no-handle invariant), and
constructing the registry never touches hardware: drivers are built lazily
on first use and connect lazily on first motion.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from mast.instruments import config as inst_config
from mast.instruments.base import (
    AxisConfig,
    InstrumentError,
    MotionAxis,
    MotionController,
)
from mast.instruments.delay_line import DelayLine, DelayLineConfig

logger = logging.getLogger(__name__)

__all__ = ["InstrumentRegistry", "get_instrument_registry", "reset_instrument_registry"]


def _required(entry: dict[str, Any], key: str) -> Any:
    value = entry.get(key)
    if value is None or value == "":
        raise InstrumentError(
            f"optical device {entry.get('id')!r} ({entry.get('type')}): "
            f"{key!r} is required")
    return value


def _axis_configs(entry: dict[str, Any]) -> list[AxisConfig]:
    out: list[AxisConfig] = []
    for raw in entry.get("axes", []) or []:
        out.append(
            AxisConfig(
                name=str(raw["name"]),
                channel=raw.get("channel", 1),
                min_pos=float(raw["min_pos"]),
                max_pos=float(raw["max_pos"]),
                unit=str(raw.get("unit", "um")),
                default_speed=(
                    float(raw["default_speed"])
                    if raw.get("default_speed") is not None
                    else None
                ),
                settle_s=float(raw.get("settle_s", 0.0)),
                max_velocity=(
                    float(raw["max_velocity"])
                    if raw.get("max_velocity") is not None
                    else None
                ),
            )
        )
    return out


def _build_controller(entry: dict[str, Any]) -> MotionController:
    """Instantiate (but do not connect) the driver for one device entry."""
    dev_type = str(entry.get("type", ""))
    axes = _axis_configs(entry)
    if dev_type == "pztc_nm003":
        from mast.instruments.pztc_nm003 import PztcController, PztcRegisterMap

        reg_map = PztcRegisterMap.from_dict(entry.get("register_map") or {})
        setup = {
            str(a.get("name")): {k: a[k] for k in ("encoder_reversed", "pid1") if k in a}
            for a in entry.get("axes", []) or [] if isinstance(a, dict)
        }
        return PztcController(
            port=entry.get("port"),
            baudrate=int(entry.get("baudrate", 115200)),
            slave=int(entry.get("slave", 1)),
            register_map=reg_map,
            axes=axes,
            counts_per_unit=float(entry.get("counts_per_unit", 1.0)),
            position_tolerance=float(entry.get("position_tolerance", 5.0)),
            on_target_mode=str(entry.get("on_target", "place_achieve")),
            axis_setup={k: v for k, v in setup.items() if v},
        )
    if dev_type == "pi_gcs":
        from mast.instruments.pi_gcs import PiGcsController

        return PiGcsController(
            port=entry.get("port"),
            baudrate=int(entry.get("baudrate", 115200)),
            model=str(entry.get("model", "E-861")),
            axes=axes,
            servo_on_connect=bool(entry.get("servo_on_connect", True)),
        )
    if dev_type == "thorlabs_benchtop_piezo":
        from mast.instruments.thorlabs_kinesis import ThorlabsBenchtopPiezo

        return ThorlabsBenchtopPiezo(
            serial_no=str(entry.get("serial_no", "")),
            axes=axes,
            dll_dir=entry.get("dll_dir") or None,
            closed_loop=bool(entry.get("closed_loop", True)),
        )
    if dev_type == "thorlabs_pdxc2":
        from mast.instruments.thorlabs_pdxc2 import ThorlabsPDXC2Controller

        tol = entry.get("position_tolerance")
        return ThorlabsPDXC2Controller(
            serial_no=str(entry.get("serial_no", "")),
            axes=axes,
            dll_dir=entry.get("dll_dir") or None,
            counts_per_unit=float(entry.get("counts_per_unit", 1.0)),
            closed_loop=bool(entry.get("closed_loop", True)),
            position_tolerance=float(tol) if tol is not None else None,
            external_trigger=entry.get("external_trigger", "manual"),
        )
    if dev_type == "satellite_motion":
        from mast.instruments.remote import RemoteMotionController

        step = entry.get("max_step_mm")
        return RemoteMotionController(
            satellite=str(_required(entry, "satellite")),
            device=str(_required(entry, "device")),
            axes=axes,
            max_step=float(step) if step is not None else None,
            move_timeout_s=float(entry.get("move_timeout_s", 120.0)),
        )
    if dev_type in ("satellite_rotation", "sim_rotation"):
        from mast.instruments.rotation import RotationConfig

        rot = RotationConfig.from_dict(entry)
        if dev_type == "sim_rotation":
            from mast.instruments.rotation import SimRotationController

            return SimRotationController(axes=axes, rotation=rot,
                                         speed_deg_s=float(entry.get("speed_deg_s", 90.0)))
        from mast.instruments.remote import RemoteRotationController

        return RemoteRotationController(
            satellite=str(_required(entry, "satellite")),
            device=str(_required(entry, "device")),
            axes=axes, rotation=rot,
            move_timeout_s=float(entry.get("move_timeout_s", 60.0)),
            home_timeout_s=float(entry.get("home_timeout_s", 90.0)),
        )
    if dev_type in ("satellite_openloop", "sim_openloop"):
        max_steps = int(_required(entry, "max_steps_per_cmd"))
        if dev_type == "sim_openloop":
            from mast.instruments.openloop import SimOpenLoopController

            return SimOpenLoopController(axes=axes, max_steps_per_cmd=max_steps,
                                         steps_per_s=float(entry.get("steps_per_s", 2000.0)))
        from mast.instruments.remote import RemoteOpenLoopController

        return RemoteOpenLoopController(
            satellite=str(_required(entry, "satellite")),
            device=str(_required(entry, "device")),
            axes=axes, max_steps_per_cmd=max_steps,
            move_timeout_s=float(entry.get("move_timeout_s", 60.0)),
        )
    kind = inst_config.device_kind(dev_type)
    if kind in inst_config.NON_MOTION_KINDS:
        accessor = {"spectrometer": "spectrometer", "microscope": "microscope",
                    "laser": "laser", "power_meter": "power_meter",
                    "temperature": "temperature_controller"}[kind]
        raise InstrumentError(
            f"optical device {entry.get('id')!r} is a {kind} — use "
            f"registry.{accessor}(), not a motion controller")
    raise InstrumentError(
        f"unknown optical device type {dev_type!r} "
        f"(supported: {inst_config.DEVICE_TYPES})"
    )


def _build_spectrometer(entry: dict[str, Any]):
    """Instantiate (but do not touch) a spectrometer driver."""
    from mast.instruments.spectrometer import SimSpectrometer, SpectrometerLimits

    dev_type = str(entry.get("type", ""))
    limits = SpectrometerLimits.from_dict(entry.get("limits"))
    laser = entry.get("laser_line_nm")
    dev_id = str(entry.get("id", ""))
    name = str(entry.get("name") or dev_id)
    if dev_type == "sim_spectrometer":
        return SimSpectrometer(device_id=dev_id, name=name, limits=limits,
                               laser_line_nm=float(laser) if laser else 633.0,
                               time_scale=float(entry.get("time_scale", 1.0)))
    if dev_type == "satellite_spectrometer":
        from mast.instruments.remote import RemoteSpectrometer

        return RemoteSpectrometer(
            satellite=str(_required(entry, "satellite")),
            device=str(_required(entry, "device")),
            device_id=dev_id, name=name, limits=limits,
            laser_line_nm=float(laser) if laser else None,
            setting_timeout_s=float(entry.get("setting_timeout_s", 120.0)),
        )
    raise InstrumentError(f"optical device {dev_id!r} ({dev_type}) is not a spectrometer")


# ── optical-station devices (docs/v2/design/optical_station_witec.md §6.1) ──

#: real driver type → module exposing ``build(entry) -> device``. Imported lazily,
#: so a machine without the vendor stack (comtypes, pyserial port, TLPM DLL) still boots.
_STATION_DRIVERS: dict[str, str] = {
    "witec_com": "mast.instruments.witec.microscope",
    "nkt_superk": "mast.instruments.nkt_superk",
    "npi_rainbow": "mast.instruments.npi_rainbow",
    "thorlabs_pm": "mast.instruments.thorlabs_pm",
    "tc100": "mast.instruments.tc100",
}


def _build_station_device(entry: dict[str, Any]):
    """Instantiate (but do not connect) a microscope / laser / power meter /
    temperature controller. Sims are built here; real drivers via ``build(entry)``."""
    import importlib

    dev_type = str(entry.get("type", ""))
    dev_id = str(entry.get("id", ""))
    name = str(entry.get("name") or dev_id)
    if dev_type == "sim_microscope":
        from mast.instruments.microscope import MicroscopeLimits, SimMicroscope
        from mast.instruments.spectrometer import SpectrometerLimits

        mlim = entry.get("microscope_limits")
        return SimMicroscope(
            device_id=dev_id, name=name,
            limits=SpectrometerLimits.from_dict(entry.get("limits")),
            microscope_limits=MicroscopeLimits.from_dict(mlim) if mlim else None,
            write_access=bool(entry.get("sim_write_access", True)),
            time_scale=float(entry.get("time_scale", 0.0)))
    if dev_type in ("sim_laser", "sim_tunable_laser"):
        from mast.instruments.laser import (LaserLimits, SimLaser, SimTunableLaser,
                                            default_emission_guard)

        kw: dict[str, Any] = {"device_id": dev_id, "name": name,
                              "allow_emission_control": bool(entry.get("allow_emission_control", True)),
                              "write_enabled": bool(entry.get("write_enabled", True))}
        if entry.get("limits"):
            kw["limits"] = LaserLimits.from_dict(entry.get("limits"))
        # a sim has no operator gate unless its entry asks for one (the real drivers install it
        # by default); asking must not be silently ignored, so tests can rehearse the real gate
        if entry.get("require_laser_attestation") is True:
            kw["emission_guard"] = default_emission_guard(True)
        if dev_type == "sim_laser":
            return SimLaser(wavelength_nm=float(entry.get("wavelength_nm", 1064.0)), **kw)
        return SimTunableLaser(n_channels=int(entry.get("channels", 8)), **kw)
    if dev_type == "sim_power_meter":
        from mast.instruments.power_meter import SimPowerMeter

        return SimPowerMeter(device_id=dev_id, name=name)
    if dev_type == "sim_temperature":
        from mast.instruments.temperature import SimTemperatureController, TemperatureLimits

        kw = {"device_id": dev_id, "name": name,
              "write_enabled": bool(entry.get("write_enabled", True)),
              "time_scale": float(entry.get("time_scale", 1.0)),
              "tau_s": float(entry.get("tau_s", 20.0))}
        if entry.get("limits"):
            kw["limits"] = TemperatureLimits.from_dict(entry.get("limits"))
        return SimTemperatureController(**kw)
    module = _STATION_DRIVERS.get(dev_type)
    if module is None:
        raise InstrumentError(f"optical device {dev_id!r}: {dev_type!r} is not a station device")
    try:
        mod = importlib.import_module(module)
        build = getattr(mod, "build")
    except (ImportError, AttributeError) as exc:
        from mast.instruments.base import InstrumentUnavailable

        raise InstrumentUnavailable(
            f"optical device {dev_id!r}: driver {module} unavailable ({exc})") from exc
    return build(entry)


_KIND_ACCESSOR_CHECK = {"microscope": inst_config.MICROSCOPE_TYPES,
                        "laser": inst_config.LASER_TYPES,
                        "power_meter": inst_config.POWER_METER_TYPES,
                        "temperature": inst_config.TEMPERATURE_TYPES}


class InstrumentRegistry:
    """Config-driven device inventory with lazily-built, cached drivers."""

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path
        self._lock = threading.RLock()
        self._config = inst_config.load_config(config_path)
        self._controllers: dict[str, MotionController] = {}
        self._spectrometers: dict[str, Any] = {}
        #: optical-station devices (microscope / laser / power_meter / temperature)
        self._station: dict[str, Any] = {}

    def _station_devs(self) -> dict[str, Any]:
        """The station-device cache, created on first use: test doubles subclass
        this registry without calling ``__init__`` (as with ``_spectrometers``),
        and every public method must still work on them."""
        devs = getattr(self, "_station", None)
        if devs is None:
            devs = self._station = {}
        return devs

    # -- inventory -------------------------------------------------------------

    def list_devices(self) -> list[dict[str, Any]]:
        """Inventory summary for UI/skills. Never touches hardware."""
        out: list[dict[str, Any]] = []
        specs = getattr(self, "_spectrometers", {})
        with self._lock:
            station = self._station_devs()
            for entry in self._config.get("devices", []):
                if not isinstance(entry, dict):
                    continue
                dev_id = str(entry.get("id", ""))
                dev_type = str(entry.get("type", "?"))
                kind = inst_config.device_kind(dev_type)
                ctrl = self._controllers.get(dev_id)
                item: dict[str, Any] = {
                    "id": dev_id,
                    "name": entry.get("name", dev_id),
                    "type": dev_type,
                    "kind": kind,
                    "role": entry.get("role"),
                    "enabled": bool(entry.get("enabled", True)),
                    "connected": (bool(ctrl.is_connected) if ctrl
                                  else dev_id in specs or dev_id in station),
                    # as configured; absent = "none" (no check), an unknown value
                    # is shown as written and refuses motion
                    "tip_requirement": entry.get("tip_requirement") or "none",
                    "axes": [
                        {
                            "name": a.get("name"),
                            "unit": a.get("unit", "um"),
                            "min_pos": a.get("min_pos"),
                            "max_pos": a.get("max_pos"),
                            "role": a.get("role"),
                            "max_velocity": a.get("max_velocity"),
                        }
                        for a in entry.get("axes", []) or []
                    ],
                }
                if dev_type in inst_config.SATELLITE_TYPES:
                    item["satellite"] = entry.get("satellite")
                    item["remote_device"] = entry.get("device")
                out.append(item)
        return out

    def device_configs(self) -> list[dict[str, Any]]:
        """Copies of every device entry, enabled or not. Never touches hardware."""
        import copy

        with self._lock:
            return [copy.deepcopy(e) for e in self._config.get("devices", [])
                    if isinstance(e, dict)]

    def device_config(self, device_id: str) -> dict[str, Any] | None:
        """A copy of one device entry (enabled or not), or None."""
        import copy

        with self._lock:
            entry = inst_config.device_by_id(self._config, device_id)
            return copy.deepcopy(entry) if entry is not None else None

    def is_connected(self, device_id: str) -> bool:
        """Whether this process has the device open. No I/O."""
        with self._lock:
            ctrl = self._controllers.get(device_id)
            specs = getattr(self, "_spectrometers", {})
            return (bool(ctrl.is_connected) if ctrl is not None
                    else device_id in specs or device_id in self._station_devs())

    def release(self, device_id: str) -> bool:
        """Close one device and forget it, so its vendor software can open it
        again; the next use reconnects. Returns whether it was open."""
        with self._lock:
            ctrl = self._controllers.pop(device_id, None)
            spec = getattr(self, "_spectrometers", {}).pop(device_id, None)
            station = self._station_devs().pop(device_id, None)
        if station is spec:
            station = None          # a microscope is cached as both; close it once
        was_open = (bool(ctrl is not None and ctrl.is_connected) or spec is not None
                    or station is not None)
        for dev in (ctrl, spec, station):
            if dev is None:
                continue
            try:
                dev.close()
            except Exception as exc:  # noqa: BLE001 - forgotten either way
                logger.warning("optical device %s: close failed: %s", device_id, exc)
        return was_open

    def _entry(self, device_id: str) -> dict[str, Any]:
        entry = inst_config.device_by_id(self._config, device_id)
        if entry is None:
            known = [
                str(e.get("id"))
                for e in self._config.get("devices", [])
                if isinstance(e, dict)
            ]
            raise InstrumentError(
                f"no optical device with id {device_id!r}; configured: {known}"
            )
        if not entry.get("enabled", True):
            raise InstrumentError(f"optical device {device_id!r} is disabled")
        return entry

    # -- lookups ---------------------------------------------------------------

    def controller(self, device_id: str) -> MotionController:
        with self._lock:
            ctrl = self._controllers.get(device_id)
            if ctrl is not None:
                return ctrl
            entry = self._entry(device_id)
            ctrl = _build_controller(entry)
            self._controllers[device_id] = ctrl
            return ctrl

    def spectrometer(self, device_id: str):
        """The :class:`~mast.instruments.spectrometer.Spectrometer` for *device_id*."""
        with self._lock:
            specs = getattr(self, "_spectrometers", None)
            if specs is None:
                specs = self._spectrometers = {}
            spec = specs.get(device_id)
            if spec is not None:
                return spec
            entry = self._entry(device_id)
            if str(entry.get("type")) not in inst_config.SPECTROMETER_TYPES:
                raise InstrumentError(
                    f"optical device {device_id!r} ({entry.get('type')}) is not a spectrometer")
            if str(entry.get("type")) in inst_config.MICROSCOPE_TYPES:
                spec = self.microscope(device_id)      # one object, two roles
            else:
                spec = _build_spectrometer(entry)
            specs[device_id] = spec
            return spec

    # -- optical-station devices -------------------------------------------------

    def _station_device(self, device_id: str, kind: str):
        with self._lock:
            # kind first, cache second: a cached microscope must never come back
            # from registry.laser(id)
            entry = self._entry(device_id)
            if str(entry.get("type")) not in _KIND_ACCESSOR_CHECK[kind]:
                raise InstrumentError(
                    f"optical device {device_id!r} ({entry.get('type')}) is not a {kind}")
            cache = self._station_devs()
            dev = cache.get(device_id)
            if dev is not None:
                return dev
            dev = _build_station_device(entry)
            cache[device_id] = dev
            return dev

    def microscope(self, device_id: str):
        """The :class:`~mast.instruments.microscope.Microscope` for *device_id*."""
        return self._station_device(device_id, "microscope")

    def laser(self, device_id: str):
        """The :class:`~mast.instruments.laser.Laser` for *device_id*."""
        return self._station_device(device_id, "laser")

    def power_meter(self, device_id: str):
        """The :class:`~mast.instruments.power_meter.PowerMeter` for *device_id*."""
        return self._station_device(device_id, "power_meter")

    def temperature_controller(self, device_id: str):
        """The :class:`~mast.instruments.temperature.TemperatureController` for *device_id*."""
        return self._station_device(device_id, "temperature")

    def built_station_devices(self) -> dict[str, Any]:
        """Station devices already constructed (i.e. used since start). No I/O."""
        with self._lock:
            return dict(self._station_devs())

    def find_devices(self, *, kind: str | None = None, role: str | None = None,
                     enabled_only: bool = True) -> list[dict[str, Any]]:
        """Config entries matching a kind (``device_kind``) and/or a role — the
        device-level ``role``, or any axis's role. Never builds drivers."""
        hits: list[dict[str, Any]] = []
        with self._lock:
            for entry in self._config.get("devices", []):
                if not isinstance(entry, dict):
                    continue
                if enabled_only and not entry.get("enabled", True):
                    continue
                if kind and inst_config.device_kind(str(entry.get("type"))) != kind:
                    continue
                if role:
                    roles = {entry.get("role")} | {
                        a.get("role") for a in entry.get("axes", []) or []
                        if isinstance(a, dict)}
                    if role not in roles:
                        continue
                hits.append(dict(entry))
        return hits

    def axis(self, device_id: str, axis_name: str) -> MotionAxis:
        return self.controller(device_id).axis(axis_name)

    def built_controllers(self) -> dict[str, MotionController]:
        """Controllers already constructed (i.e. used since start). No I/O."""
        with self._lock:
            return dict(self._controllers)

    def find_axes_by_role(self, role: str) -> list[tuple[str, str, dict[str, Any]]]:
        """(device_id, axis_name, axis_entry) for every axis tagged *role*.
        Config-level only — does not build drivers."""
        hits: list[tuple[str, str, dict[str, Any]]] = []
        with self._lock:
            for entry in self._config.get("devices", []):
                if not isinstance(entry, dict) or not entry.get("enabled", True):
                    continue
                for a in entry.get("axes", []) or []:
                    if isinstance(a, dict) and a.get("role") == role:
                        hits.append((str(entry.get("id")), str(a.get("name")), a))
        return hits

    def delay_line(self) -> DelayLine:
        with self._lock:
            raw = self._config.get("delay_line")
        if not raw:
            raise InstrumentError(
                "no delay_line section in config/optical_instruments.json — "
                "configure the pump-probe delay stage first"
            )
        cfg = DelayLineConfig.from_dict(raw)
        return DelayLine(self.axis(cfg.device_id, cfg.axis), cfg)

    def delay_line_info(self) -> dict[str, Any] | None:
        """The delay-line binding from config alone — device, axis, conversion and
        the reachable delay window (from the axis soft limits) — or None when no
        delay line is configured. Builds no driver and opens nothing, unlike
        :meth:`delay_line`, whose axis connects: an inventory must not grab the
        delay stage from its vendor software (or re-grab one just released)."""
        from types import SimpleNamespace

        with self._lock:
            raw = self._config.get("delay_line")
            entry = (inst_config.device_by_id(self._config, str(raw.get("device_id")))
                     if isinstance(raw, dict) else None)
        if not raw:
            return None
        cfg = DelayLineConfig.from_dict(raw)
        if entry is None:
            raise InstrumentError(f"delay_line.device_id {cfg.device_id!r} is not a configured "
                                  "optical device")
        axis_cfg = next((a for a in _axis_configs(entry) if a.name == cfg.axis), None)
        if axis_cfg is None:
            raise InstrumentError(f"delay_line.axis {cfg.axis!r} is not an axis of "
                                  f"{cfg.device_id!r}")
        lo, hi = DelayLine(SimpleNamespace(config=axis_cfg), cfg).delay_range_ps  # math only
        return {"device_id": cfg.device_id, "axis": cfg.axis, "ps_per_mm": cfg.ps_per_mm,
                "zero_offset_mm": cfg.zero_offset_mm, "delay_range_ps": [lo, hi]}

    def open_delay_line(self) -> DelayLine | None:
        """The delay line over its axis if the delay stage is already open in this
        process, else None — never opens it (see :meth:`open_axis`)."""
        with self._lock:
            raw = self._config.get("delay_line")
        if not raw:
            return None
        cfg = DelayLineConfig.from_dict(raw)
        ax = self.open_axis(cfg.device_id, cfg.axis)
        return DelayLine(ax, cfg) if ax is not None else None

    def open_axis(self, device_id: str, axis_name: str) -> MotionAxis | None:
        """The axis if this process already has its device open, else None. Never
        builds, connects or reconnects anything — for reads that must not open a
        device (a closed device stays closed; one just released stays released)."""
        with self._lock:
            ctrl = self._controllers.get(device_id)
        return (ctrl.open_axes() if ctrl is not None else {}).get(axis_name)

    # -- lifecycle ---------------------------------------------------------------

    #: how long stop_all waits for the per-device stops before returning
    STOP_WAIT_S = 3.0

    def stop_all(self, *, wait_s: float | None = None, reason: str = "") -> dict[str, Any]:
        """Panic-stop every built controller and spectrometer. Never raises.

        Each device is stopped on its own daemon thread so one wedged serial
        port cannot hold up the others, and the call returns after at most
        ``wait_s`` (E-STOP / shutdown hooks must not block).

        A stop of the whole station is somebody else's stop for a protocol running on it: every
        station device this stops is first told so (``note_external_stop``, ``instruments/external_stop.py``),
        all of them before any stop is sent, so the protocol ends cleanly (``stopped_externally``) at its
        next step instead of opening a shutter or switching a source on again. ``reason`` says who asked
        (``"StopOpticalStage (<caller>)"``); empty = a stop of the whole station. Counting never keeps a
        stop from being sent. Returns
        ``{"stopped": [...], "errors": {...}, "pending": [...], "unconfirmed": {...}}``."""
        with self._lock:
            targets: list[tuple[str, Any, str]] = [
                (dev_id, ctrl.stop_all, "controller") for dev_id, ctrl in self._controllers.items()]
            station = dict(self._station_devs())
            targets += [(dev_id, spec.stop, "spectrometer") for dev_id, spec in
                        getattr(self, "_spectrometers", {}).items()
                        if dev_id not in station]
            # station devices stop in their safe direction: microscope sequencers and
            # stage stop, lasers turn emission OFF, temperature ramps stop
            noted: list[Any] = []
            for dev_id, dev in station.items():
                kind = inst_config.device_kind(
                    str((inst_config.device_by_id(self._config, dev_id) or {}).get("type")))
                fn = {"microscope": getattr(dev, "stop_all", None),
                      "laser": getattr(dev, "emission_off", None),
                      "temperature": getattr(dev, "stop", None)}.get(kind)
                if fn is not None:
                    targets.append((dev_id, fn, kind))
                    noted.append(dev)
        # counted BEFORE any stop is sent (the run checks the counters in front of every hardware call)
        why = (str(reason or "").strip() or "stop of the whole optical station (registry.stop_all)")[:200]
        for dev in noted:
            note = getattr(dev, "note_external_stop", None)
            if callable(note):
                try:
                    note(why)
                except Exception:  # noqa: BLE001 - panic path: counting must never keep a stop from being sent
                    pass
        # "unconfirmed": the stop was sent but the device did not confirm it (a laser whose
        # emission_off() read back anything but False, a temperature controller whose stop()
        # returned False). Those never raise, so without this they read as "stopped".
        report: dict[str, Any] = {"stopped": [], "errors": {}, "pending": [], "unconfirmed": {}}
        if not targets:
            return report
        lock = threading.Lock()

        def _one(dev_id: str, fn, kind: str) -> None:
            try:
                res = fn()
                unconfirmed = ((kind == "laser" and res is not False)
                               or (kind == "temperature" and res is False))
                if kind == "microscope":
                    # stop_all() returns None by contract; a driver that knows says so in its report
                    rep = getattr(getattr(fn, "__self__", None), "last_stop_report", None)
                    if isinstance(rep, dict) and (rep.get("shutter_confirmed") is False
                                                  or rep.get("scope") == "selected_laser_only"):
                        # not closed, or only the selected laser was looked at
                        unconfirmed, res = True, rep.get("shutter") or rep.get("scope")
                with lock:
                    if unconfirmed:
                        report["unconfirmed"][dev_id] = f"{kind} did not confirm the stop (read back {res!r})"
                    else:
                        report["stopped"].append(dev_id)
            except Exception as exc:  # noqa: BLE001 - panic path
                with lock:
                    report["errors"][dev_id] = f"{type(exc).__name__}: {exc}"

        threads = [threading.Thread(target=_one, args=t, daemon=True,
                                    name=f"optics-stop-{t[0]}") for t in targets]
        for th in threads:
            th.start()
        deadline = time.monotonic() + (self.STOP_WAIT_S if wait_s is None else float(wait_s))
        for th in threads:
            th.join(timeout=max(0.0, deadline - time.monotonic()))
        with lock:
            done = set(report["stopped"]) | set(report["errors"]) | set(report["unconfirmed"])
            report["pending"] = [d for d, _fn, _kind in targets if d not in done]
        if report["unconfirmed"]:
            logger.warning("optics stop_all: not confirmed %s", report["unconfirmed"])
        if report["pending"]:
            logger.warning("optics stop_all: still stopping %s after %.1fs",
                           report["pending"], self.STOP_WAIT_S if wait_s is None else wait_s)
        return report

    def close_all(self) -> None:
        with self._lock:
            controllers, self._controllers = list(self._controllers.values()), {}
            specs = list(getattr(self, "_spectrometers", {}).values())
            self._spectrometers = {}
            station = [d for d in self._station_devs().values()
                       if all(d is not s for s in specs)]
            self._station = {}
        specs += station
        for ctrl in controllers:
            try:
                ctrl.close()
            except Exception:  # noqa: BLE001
                pass
        for spec in specs:
            try:
                spec.close()
            except Exception:  # noqa: BLE001
                pass

    def reload(self) -> None:
        """Re-read the manifest; closes existing connections first."""
        self.close_all()
        with self._lock:
            self._config = inst_config.load_config(self._config_path)


# ── module-level singleton (skills' access path; plan_overlay precedent) ──

_registry: InstrumentRegistry | None = None
_registry_lock = threading.Lock()


def get_instrument_registry() -> InstrumentRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = InstrumentRegistry()
    return _registry


def reset_instrument_registry(new: InstrumentRegistry | None = None) -> None:
    """Swap/clear the singleton (tests; config reload from GUI)."""
    global _registry
    with _registry_lock:
        old, _registry = _registry, new
    if old is not None:
        old.close_all()
