"""PowerMeter — an optical power meter (Thorlabs PM100-family via TLPM, or a sim).

Design contract: ``docs/v2/design/optical_station_witec.md`` §6.1.
Reading never needs write access; ``set_wavelength_nm`` only changes the meter's
correction wavelength (no light is switched).
"""

from __future__ import annotations

import math
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable

from mast.instruments.base import InstrumentError

__all__ = ["PowerMeterError", "PowerMeter", "SimPowerMeter"]


class PowerMeterError(InstrumentError):
    """A power-meter request failed or was out of range."""


class PowerMeter(ABC):
    """Abstract power meter. Drivers implement ``_*_raw``."""

    #: correction-wavelength range the meter/sensor accepts (driver may narrow it)
    MIN_NM, MAX_NM = 200.0, 2000.0

    def __init__(self, *, device_id: str = "", name: str = ""):
        self.device_id = device_id
        self.name = name or device_id
        self._lock = threading.RLock()

    def status(self) -> dict:
        st = dict(self._status_raw() or {})
        st.setdefault("device_id", self.device_id)
        return st

    def set_wavelength_nm(self, nm: Any) -> float:
        try:
            v = float(nm)
        except (TypeError, ValueError) as exc:
            raise PowerMeterError(f"wavelength must be a number, got {nm!r}") from exc
        if not (math.isfinite(v) and self.MIN_NM <= v <= self.MAX_NM):
            raise PowerMeterError(f"wavelength {v} nm outside [{self.MIN_NM}, {self.MAX_NM}] nm")
        with self._lock:
            return float(self._set_wavelength_raw(v))

    def read_power_w(self, *, wavelength_nm: Any = None, averages: Any = 1) -> float:
        """Mean of ``averages`` readings in watts (sets the wavelength first if given)."""
        n = int(averages)
        if not 1 <= n <= 1000:
            raise PowerMeterError("averages must be in [1, 1000]")
        if wavelength_nm is not None:
            self.set_wavelength_nm(wavelength_nm)
        with self._lock:
            vals = [float(self._read_power_raw()) for _ in range(n)]
        return sum(vals) / n

    def close(self) -> None:  # pragma: no cover - drivers override
        pass

    @abstractmethod
    def _status_raw(self) -> dict: ...
    @abstractmethod
    def _set_wavelength_raw(self, nm: float) -> float: ...
    @abstractmethod
    def _read_power_raw(self) -> float: ...


class SimPowerMeter(PowerMeter):
    """Returns ``source()`` watts (a callable, e.g. a SimLaser's ``output_w``) + noise-free."""

    def __init__(self, *, source: Callable[[], float] | None = None, **kw):
        super().__init__(**kw)
        self.source = source
        self.wavelength_nm = 532.0
        self.reads = 0

    def _status_raw(self) -> dict:
        return {"sim": True, "wavelength_nm": self.wavelength_nm}

    def _set_wavelength_raw(self, nm: float) -> float:
        self.wavelength_nm = float(nm)
        return self.wavelength_nm

    def _read_power_raw(self) -> float:
        self.reads += 1
        return float(self.source()) if self.source is not None else 0.0
