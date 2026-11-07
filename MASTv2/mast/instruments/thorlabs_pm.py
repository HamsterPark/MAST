"""Thorlabs power meter (PM100 family / PM101U and compatible consoles) through the TLPMX driver DLL.

Contract: ``docs/v2/design/optical_station_witec.md`` §3 / §6.1 / D6.

Sources
=======
[TLPMX-py]  Thorlabs' own ctypes example, ``Thorlabs/Light_Analysis_Examples`` on GitHub,
            ``Python/Thorlabs PMxxx Power Meters/Examples using the TLPMX dll/Examples using the dll
            with c_types/PM_SimpleExample.py`` (TLPMX_64.dll 5.6.4983.666): ``TLPMX_findRsrc``,
            ``TLPMX_getRsrcName``, ``TLPMX_init``, ``TLPMX_errorMessage``, ``TLPMX_setWavelength(handle, nm,
            channel)``, ``TLPMX_measPower(handle, &power, channel)``, ``TLPMX_close``.
[TLPMX-ext] same repo, ``PM5020_ExtendedExample.py``: ``TLPMX_getWavelength(handle, attribute, &nm,
            channel)`` with ``TLPM_ATTR_SET_VAL = 0``, ``TLPM_ATTR_MIN_VAL = 1``, ``TLPM_ATTR_MAX_VAL = 2``;
            ``TLPM_SENSOR_CHANNEL1 = 1``.
[TLPMX-cpp] same repo, ``C++/.../TLPMX_dll/Simple Example/Powermeter Simple Example.cpp``:
            ``TLPMX_init(rsrcDescr, VI_ON, VI_OFF, &handle)`` (ID query on, no reset),
            ``TLPMX_identificationQuery(handle, vendor, name, serial, revision)``,
            ``TLPMX_getSensorInfo(handle, name, serial, message, &type, &stype, &flags, channel)``.
[README]    the example's Readme: "The TLPMX_64.dll will be installed in the folder
            ``C:\\Program Files\\IVI Foundation\\VISA\\Win64\\Bin\\TLPMX_64.dll``" (with the Optical Power
            Monitor software).

All power values are watts (``measPower``); the correction wavelength is nanometres. Status codes
are VISA ``ViStatus``: 0 success, negative error, positive warning.

Behaviour
=========
* Nothing is loaded or opened when the device is built (the registry builds lazily); the DLL is loaded and
  the meter opened on first use. No DLL (a development machine, Optical Power Monitor not installed) ->
  :class:`~mast.instruments.base.InstrumentUnavailable`, never a crash at import time.
* The DLL location is configuration: ``dll_path`` (full path) or ``dll_dir`` (folder), default the IVI folder
  above. ``resource: "auto"`` uses the single meter found; several meters need an explicit resource string.
* **One owner at a time.** The Thorlabs *Optical Power Monitor* program holds the meter while it runs
  (USB, so port_guard only advises); an ``init`` failure says so.
* The correction wavelength is checked against the connected sensor's own limits
  (``TLPMX_getWavelength`` MIN/MAX) before it is sent; out of range is refused, never clamped.
* ``init`` is called with the ID query on and **no reset** (so the meter keeps the operator's settings).
"""

from __future__ import annotations

import ctypes
import logging
import math
import os
import sys
import threading
from pathlib import Path
from typing import Any

from mast.i18n import tr
from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.instruments.power_meter import PowerMeter, PowerMeterError

logger = logging.getLogger(__name__)

__all__ = ["ThorlabsPM", "DEFAULT_DLL_DIR", "DLL_NAME", "build"]

DLL_NAME = "TLPMX_64.dll"
DEFAULT_DLL_DIR = r"C:\Program Files\IVI Foundation\VISA\Win64\Bin"
TLPM_ATTR_SET_VAL, TLPM_ATTR_MIN_VAL, TLPM_ATTR_MAX_VAL = 0, 1, 2
TLPM_SENSOR_CHANNEL1 = 1
_BUF = 256


def _load_library(path: str):
    """The one place the DLL is loaded (tests replace it)."""
    return ctypes.CDLL(path)


def _declare(lib: Any) -> None:
    """Argument and result types of the functions used, as in the Thorlabs headers/examples. ViSession is
    a 32-bit handle, ViBoolean 16-bit, ViReal64 a double."""
    c = ctypes
    sigs = {
        "TLPMX_findRsrc": [c.c_uint32, c.POINTER(c.c_uint32)],
        "TLPMX_getRsrcName": [c.c_uint32, c.c_uint32, c.c_char_p],
        "TLPMX_init": [c.c_char_p, c.c_uint16, c.c_uint16, c.POINTER(c.c_uint32)],
        "TLPMX_close": [c.c_uint32],
        "TLPMX_errorMessage": [c.c_uint32, c.c_int32, c.c_char_p],
        "TLPMX_setWavelength": [c.c_uint32, c.c_double, c.c_int16],
        "TLPMX_getWavelength": [c.c_uint32, c.c_int16, c.POINTER(c.c_double), c.c_int16],
        "TLPMX_measPower": [c.c_uint32, c.POINTER(c.c_double), c.c_int16],
        "TLPMX_identificationQuery": [c.c_uint32, c.c_char_p, c.c_char_p, c.c_char_p, c.c_char_p],
        "TLPMX_getSensorInfo": [c.c_uint32, c.c_char_p, c.c_char_p, c.c_char_p, c.POINTER(c.c_int16),
                                c.POINTER(c.c_int16), c.POINTER(c.c_int16), c.c_int16],
    }
    for name, argtypes in sigs.items():
        fn = getattr(lib, name)
        try:
            fn.argtypes = argtypes
            fn.restype = c.c_int32
        except AttributeError:       # a test double whose attribute cannot carry them
            pass


def _text(buf) -> str:
    raw = buf.value if hasattr(buf, "value") else buf
    return bytes(raw).decode("ascii", errors="replace").strip() if raw else ""


class ThorlabsPM(PowerMeter):
    """A Thorlabs console read through ``TLPMX_64.dll``."""

    def __init__(self, *, resource: str = "auto", dll_path: str | None = None, dll_dir: str | None = None,
                 channel: int = TLPM_SENSOR_CHANNEL1, device_id: str = "", name: str = "", lib: Any = None):
        super().__init__(device_id=device_id, name=name)
        self.resource = str(resource or "auto")
        self.channel = int(channel)
        self._dll_path = dll_path
        self._dll_dir = dll_dir or DEFAULT_DLL_DIR
        self._lib = lib
        self._handle: int = 0
        self._rsrc_open = ""
        self._ident: dict[str, str] = {}
        self._open_lock = threading.RLock()
        if lib is not None:
            _declare(lib)

    # -- DLL & session ----------------------------------------------------------------------------

    def dll_location(self) -> str:
        return str(self._dll_path or (Path(self._dll_dir) / DLL_NAME))

    def _library(self):
        if self._lib is not None:
            return self._lib
        path = self.dll_location()
        if sys.platform != "win32":
            raise InstrumentUnavailable(f"{DLL_NAME} is a Windows DLL (this is {sys.platform}); the Thorlabs "
                                        "power meter is only available on the optical station")
        if not os.path.isfile(path):
            raise InstrumentUnavailable(tr(
                f"找不到 Thorlabs 功率计驱动 {path}。它随 Thorlabs Optical Power Monitor 软件安装"
                f"（默认在 {DEFAULT_DLL_DIR}）；DLL 在别处就在设备配置里写 dll_dir / dll_path。",
                f"Thorlabs power-meter driver not found at {path}. It is installed with the Thorlabs Optical "
                f"Power Monitor software (default folder {DEFAULT_DLL_DIR}); if the DLL lives elsewhere set "
                "dll_dir / dll_path in the device config."))
        try:
            add = getattr(os, "add_dll_directory", None)
            if add is not None:
                self._dll_dir_handle = add(str(Path(path).parent))      # the VISA libraries it depends on
            lib = _load_library(path)
        except OSError as exc:
            raise InstrumentUnavailable(f"cannot load {path}: {exc}") from exc
        _declare(lib)
        self._lib = lib
        return lib

    def _err_text(self, lib, code: int) -> str:
        buf = ctypes.create_string_buffer(_BUF)
        try:
            lib.TLPMX_errorMessage(self._handle or 0, int(code), buf)
        except Exception:  # noqa: BLE001 - a broken error lookup must not hide the real error
            return ""
        return _text(buf)

    def _check(self, lib, code: int, what: str, *, exc: type[Exception] = PowerMeterError) -> None:
        code = int(code)
        if code < 0:
            msg = self._err_text(lib, code)
            raise exc(f"Thorlabs power meter: {what} failed with 0x{code & 0xFFFFFFFF:08X}"
                      + (f" ({msg})" if msg else ""))
        if code > 0:
            logger.debug("Thorlabs power meter: %s returned warning 0x%08X", what, code)

    def list_resources(self) -> list[str]:
        """VISA resource strings of the meters the driver sees (does not open any)."""
        lib = self._library()
        count = ctypes.c_uint32(0)
        self._check(lib, lib.TLPMX_findRsrc(0, ctypes.byref(count)), "findRsrc")
        names: list[str] = []
        for i in range(int(count.value)):
            buf = ctypes.create_string_buffer(_BUF)
            self._check(lib, lib.TLPMX_getRsrcName(0, i, buf), f"getRsrcName({i})")
            names.append(_text(buf))
        return names

    def _ensure_open(self):
        with self._open_lock:
            lib = self._lib = self._library()           # kept: close() must release through the same library
            if self._handle:
                return lib
            rsrc = self.resource
            if rsrc.lower() == "auto":
                found = self.list_resources()
                if not found:
                    raise InstrumentUnavailable(tr(
                        "没找到 Thorlabs 功率计：USB 线、驱动，或 Optical Power Monitor 正占着它？",
                        "No Thorlabs power meter found: USB cable, driver, or is the Optical Power Monitor "
                        "program holding it?"))
                if len(found) > 1:
                    raise InstrumentError(f"{len(found)} Thorlabs power meters found {found} - set 'resource' "
                                          "in the device config to the one to use")
                rsrc = found[0]
            handle = ctypes.c_uint32(0)
            code = int(lib.TLPMX_init(rsrc.encode("ascii"), 1, 0, ctypes.byref(handle)))     # ID query on, no reset
            if code < 0:
                self._handle = 0
                msg = self._err_text(lib, code)
                raise InstrumentUnavailable(
                    f"Thorlabs power meter {rsrc}: init failed with 0x{code & 0xFFFFFFFF:08X}"
                    + (f" ({msg})" if msg else "")
                    + tr(" —— 设备是独占的：Thorlabs Optical Power Monitor 开着会占住它，请先关掉。",
                         " -- the meter is exclusive: the Thorlabs Optical Power Monitor program holds it while "
                         "it runs; close it first."))
            self._handle, self._rsrc_open = int(handle.value), rsrc
            self._read_identity(lib)
            self._read_wavelength_limits(lib)
            return lib

    def _read_identity(self, lib) -> None:
        vendor, model, serial, rev = (ctypes.create_string_buffer(_BUF) for _ in range(4))
        try:
            if int(lib.TLPMX_identificationQuery(self._handle, vendor, model, serial, rev)) >= 0:
                self._ident.update(vendor=_text(vendor), model=_text(model), serial_number=_text(serial),
                                   firmware=_text(rev))
        except Exception as exc:  # noqa: BLE001 - identity is informative only
            logger.debug("TLPMX_identificationQuery failed: %s", exc)
        name, snr, msg = (ctypes.create_string_buffer(_BUF) for _ in range(3))
        t, st, fl = ctypes.c_int16(0), ctypes.c_int16(0), ctypes.c_int16(0)
        try:
            if int(lib.TLPMX_getSensorInfo(self._handle, name, snr, msg, ctypes.byref(t), ctypes.byref(st),
                                           ctypes.byref(fl), self.channel)) >= 0:
                self._ident.update(sensor_name=_text(name), sensor_serial=_text(snr), sensor_message=_text(msg))
        except Exception as exc:  # noqa: BLE001
            logger.debug("TLPMX_getSensorInfo failed: %s", exc)

    def _get_wavelength(self, lib, attribute: int) -> float:
        wl = ctypes.c_double(0.0)
        self._check(lib, lib.TLPMX_getWavelength(self._handle, attribute, ctypes.byref(wl), self.channel),
                    "getWavelength")
        return float(wl.value)

    def _read_wavelength_limits(self, lib) -> None:
        """The connected sensor's own correction-wavelength range replaces the generic 200-2000 nm."""
        try:
            lo, hi = self._get_wavelength(lib, TLPM_ATTR_MIN_VAL), self._get_wavelength(lib, TLPM_ATTR_MAX_VAL)
        except PowerMeterError as exc:
            logger.debug("sensor wavelength limits unavailable: %s", exc)
            return
        if math.isfinite(lo) and math.isfinite(hi) and 0 < lo < hi:
            self.MIN_NM, self.MAX_NM = lo, hi

    # -- PowerMeter ---------------------------------------------------------------------------------

    def set_wavelength_nm(self, nm: Any) -> float:
        self._ensure_open()                  # so the sensor's limits are known before the range check
        return super().set_wavelength_nm(nm)

    def _set_wavelength_raw(self, nm: float) -> float:
        lib = self._ensure_open()
        self._check(lib, lib.TLPMX_setWavelength(self._handle, float(nm), self.channel), "setWavelength")
        return self._get_wavelength(lib, TLPM_ATTR_SET_VAL)

    def _read_power_raw(self) -> float:
        lib = self._ensure_open()
        power = ctypes.c_double(float("nan"))
        self._check(lib, lib.TLPMX_measPower(self._handle, ctypes.byref(power), self.channel), "measPower")
        if not math.isfinite(power.value):
            raise PowerMeterError("Thorlabs power meter returned a non-finite power (no sensor connected?)")
        return float(power.value)

    def _status_raw(self) -> dict:
        lib = self._ensure_open()
        out: dict[str, Any] = {"resource": self._rsrc_open, "dll": self.dll_location(), "channel": self.channel,
                               "wavelength_range_nm": [self.MIN_NM, self.MAX_NM], **self._ident}
        try:
            out["wavelength_nm"] = self._get_wavelength(lib, TLPM_ATTR_SET_VAL)
        except PowerMeterError as exc:
            out["wavelength_error"] = str(exc)
        return out

    def identify(self) -> dict:
        """Read-only self-check: the meters visible to the driver and, for the one in use, its identity,
        sensor and wavelength range. Sets nothing."""
        out: dict[str, Any] = {"device_id": self.device_id, "dll": self.dll_location(), "configured_resource": self.resource}
        try:
            out["resources_seen"] = [] if self._handle else self.list_resources()
            out.update(self._status_raw())
            out["ok"] = True
        except InstrumentError as exc:
            out["ok"] = False
            out["error"] = str(exc)
        return out

    def close(self) -> None:
        with self._open_lock:
            h, self._handle = self._handle, 0
            if h and self._lib is not None:
                try:
                    self._lib.TLPMX_close(h)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("TLPMX_close failed: %s", exc)


def build(entry: dict) -> ThorlabsPM:
    """Registry factory (``registry._STATION_DRIVERS['thorlabs_pm']``). Loads and opens nothing."""
    dev_id = str(entry.get("id", ""))
    return ThorlabsPM(resource=str(entry.get("resource") or "auto"), dll_path=entry.get("dll_path") or None,
                      dll_dir=entry.get("dll_dir") or None, channel=int(entry.get("channel", TLPM_SENSOR_CHANNEL1)),
                      device_id=dev_id, name=str(entry.get("name") or dev_id))
