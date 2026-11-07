"""Spectrometer — the abstraction for a CCD spectrometer (LightField PyLoN + HRS-300).

Same layering as :mod:`mast.instruments.base`: the public methods are template
methods that check the configured limits and only then call a driver
primitive (``_*_raw``). A caller — skill, agent, GUI — cannot get an exposure,
frame count, grating, centre wavelength or exit port past the limits by
passing a hallucinated parameter, because the check sits below them all.
Out-of-range is REFUSED (:class:`SpectrometerLimitError`), never clamped.

``acquire`` returns an :class:`AcquisitionResult` with the frames as numpy
arrays. Drivers stream frames through the optional ``on_frame`` callback as
they arrive (that is how AcquireSpectrum feeds LiveHub); ``check_abort`` follows
the same contract as the motion layer (returns truthy *or* raises).

:class:`SimSpectrometer` (device type ``sim_spectrometer``) synthesises a
Raman-like spectrum so the skill, storage and LiveHub paths can be exercised
without LightField; it is a test/office tool, not a physics model.
"""

from __future__ import annotations

import contextlib
import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np

from mast.instruments.base import AbortCheck, InstrumentError, MotionAborted

__all__ = [
    "SpectrometerLimitError",
    "SpectrometerLimits",
    "AcquisitionResult",
    "FrameCallback",
    "Spectrometer",
    "SimSpectrometer",
    "utc_now_iso",
]

#: ``on_frame(frame_index, counts, axis_nm)`` — called on the acquiring thread.
FrameCallback = Callable[[int, np.ndarray, "np.ndarray | None"], None]

_EXIT_PORTS = ("front", "side")


def utc_now_iso() -> str:
    """UTC, millisecond precision, explicit offset (MAST's local stamps are naive)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SpectrometerLimitError(InstrumentError):
    """A requested setting lies outside the configured spectrometer limits."""


@dataclass(frozen=True)
class SpectrometerLimits:
    """Hub-side limits for one spectrometer (``limits`` in the device entry).

    Defaults are deliberately wide-but-finite; a rig narrows them in config.
    ``gratings`` empty means "whatever the device reports" (the driver checks);
    non-empty is an allow-list compared verbatim.
    """

    min_exposure_ms: float = 0.1
    max_exposure_ms: float = 600_000.0
    max_frames: int = 10_000
    max_total_s: float = 3_600.0
    min_center_nm: float = 200.0
    max_center_nm: float = 1_700.0
    gratings: tuple[str, ...] = ()
    exit_ports: tuple[str, ...] = _EXIT_PORTS

    def __post_init__(self) -> None:
        if not (0 < self.min_exposure_ms < self.max_exposure_ms):
            raise ValueError("need 0 < min_exposure_ms < max_exposure_ms")
        if self.max_frames < 1 or self.max_total_s <= 0:
            raise ValueError("max_frames and max_total_s must be positive")
        if not (0 < self.min_center_nm < self.max_center_nm):
            raise ValueError("need 0 < min_center_nm < max_center_nm")

    @classmethod
    def from_dict(cls, raw: dict | None) -> "SpectrometerLimits":
        raw = dict(raw or {})
        kw: dict[str, Any] = {}
        for key in ("min_exposure_ms", "max_exposure_ms", "max_total_s",
                    "min_center_nm", "max_center_nm"):
            if raw.get(key) is not None:
                kw[key] = float(raw[key])
        if raw.get("max_frames") is not None:
            kw["max_frames"] = int(raw["max_frames"])
        if raw.get("gratings"):
            kw["gratings"] = tuple(str(g) for g in raw["gratings"])
        if raw.get("exit_ports"):
            kw["exit_ports"] = tuple(str(p).lower() for p in raw["exit_ports"])
        return cls(**kw)

    def as_dict(self) -> dict:
        return {
            "min_exposure_ms": self.min_exposure_ms,
            "max_exposure_ms": self.max_exposure_ms,
            "max_frames": self.max_frames,
            "max_total_s": self.max_total_s,
            "min_center_nm": self.min_center_nm,
            "max_center_nm": self.max_center_nm,
            "gratings": list(self.gratings),
            "exit_ports": list(self.exit_ports),
        }


@dataclass
class AcquisitionResult:
    """What one acquisition produced. ``files`` are the device-side files
    (``[{path, sha256?, size?}]``, empty when ``save=False``)."""

    frames: list[np.ndarray]
    axis_nm: np.ndarray | None
    files: list[dict] = field(default_factory=list)
    started_utc: str = ""
    finished_utc: str = ""
    meta: dict = field(default_factory=dict)


class Spectrometer(ABC):
    """One spectrometer (camera + spectrograph as a unit).

    Settings changes and acquisitions serialise on one lock, so the grating
    cannot turn mid-exposure. Waiting for it is bounded (:attr:`BUSY_WAIT_S`):
    a second caller is refused with "busy", never queued behind a long exposure.
    ``stop`` and ``status`` do not take it.
    """

    KIND = "spectrometer"
    BUSY_WAIT_S = 2.0

    def __init__(self, *, device_id: str = "", name: str = "",
                 limits: SpectrometerLimits | None = None,
                 laser_line_nm: float | None = None):
        self.device_id = str(device_id)
        self.name = str(name or device_id)
        self.limits = limits or SpectrometerLimits()
        self.laser_line_nm = float(laser_line_nm) if laser_line_nm else None
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def _hold(self, what: str):
        if not self._lock.acquire(timeout=self.BUSY_WAIT_S):
            raise InstrumentError(
                f"{self.device_id}: busy (an acquisition or setting change is "
                f"running) — {what} not sent")
        try:
            yield
        finally:
            self._lock.release()

    # -- reads -------------------------------------------------------------

    def status(self) -> dict:
        """``{state, is_running, temperature_c, temperature_locked, exposure_ms,
        frames, grating, center_wavelength_nm, exit_port, ...}`` (contract §5.4)."""
        st = dict(self._status_raw() or {})
        if self.laser_line_nm is not None and st.get("laser_line_nm") is None:
            st["laser_line_nm"] = self.laser_line_nm
        return st

    def wavelength_axis(self) -> np.ndarray | None:
        """Pixel → wavelength (nm), or None when the device cannot say."""
        ax = self._wavelength_axis_raw()
        return None if ax is None else np.asarray(ax, dtype=np.float64)

    # -- writes (limit-checked templates) -----------------------------------

    def configure(self, **settings: Any) -> dict:
        """Apply camera settings. Only the keys present are changed."""
        clean = {k: v for k, v in settings.items() if v is not None}
        if "exposure_ms" in clean:
            clean["exposure_ms"] = self._check_exposure(clean["exposure_ms"])
        if "frames" in clean:
            clean["frames"] = self._check_frames(clean["frames"])
        if "exposure_ms" in clean and "frames" in clean:
            self._check_total(clean["exposure_ms"], clean["frames"])
        with self._hold("configure"):
            return dict(self._configure_raw(clean) or {})

    def check_request(self, exposure_ms: float | None = None,
                      frames: int | None = None) -> tuple[float | None, int | None]:
        """Validate an acquisition request against the limits without starting it
        (dry runs use this). Returns the normalised ``(exposure_ms, frames)``."""
        exp = self._check_exposure(exposure_ms) if exposure_ms is not None else None
        n = self._check_frames(frames) if frames is not None else None
        if exp is not None and n is not None:
            self._check_total(exp, n)
        return exp, n

    def acquire(self, *, exposure_ms: float | None = None, frames: int | None = None,
                save: bool = True, base_name: str | None = None, tag: str | None = None,
                meta: dict | None = None, timeout_s: float | None = None,
                check_abort: AbortCheck | None = None,
                on_frame: FrameCallback | None = None) -> AcquisitionResult:
        """Expose and read out. Blocks until done, aborted, or ``timeout_s``."""
        exp, n = self.check_request(exposure_ms, frames)
        if timeout_s is None:
            timeout_s = self.default_timeout_s(exp, n)
        _abort_now(check_abort, f"{self.device_id}: acquisition")
        with self._hold("acquire"):
            return self._acquire_raw(exposure_ms=exp, frames=n, save=bool(save),
                                     base_name=base_name, tag=tag, meta=dict(meta or {}),
                                     timeout_s=float(timeout_s), check_abort=check_abort,
                                     on_frame=on_frame)

    def set_grating(self, grating: str) -> dict:
        g = str(grating).strip()
        if not g:
            raise SpectrometerLimitError("grating must be non-empty")
        if self.limits.gratings and g not in self.limits.gratings:
            raise SpectrometerLimitError(
                f"{self.device_id}: grating {g!r} not in the configured list "
                f"{list(self.limits.gratings)}")
        with self._hold("set_grating"):
            return dict(self._set_grating_raw(g) or {})

    def set_center_wavelength(self, nm: float) -> dict:
        v = float(nm)
        lo, hi = self.limits.min_center_nm, self.limits.max_center_nm
        if not (math.isfinite(v) and lo <= v <= hi):
            raise SpectrometerLimitError(
                f"{self.device_id}: centre wavelength {v} nm outside [{lo}, {hi}] nm")
        with self._hold("set_center_wavelength"):
            return dict(self._set_center_raw(v) or {})

    def set_exit_port(self, port: str) -> dict:
        p = str(port).strip().lower()
        if p not in self.limits.exit_ports:
            raise SpectrometerLimitError(
                f"{self.device_id}: exit port {port!r} not in {list(self.limits.exit_ports)}")
        with self._hold("set_exit_port"):
            return dict(self._set_exit_port_raw(p) or {})

    def stop(self) -> None:
        """Abort an acquisition. Never raises, never waits for the lock — this
        is the panic path and must work while ``acquire`` is blocked."""
        try:
            self._stop_raw()
        except Exception:  # noqa: BLE001 - panic path
            pass

    def close(self) -> None:
        pass

    # -- helpers -------------------------------------------------------------

    def default_timeout_s(self, exposure_ms: float | None, frames: int | None) -> float:
        exp = exposure_ms if exposure_ms is not None else 1000.0
        n = frames if frames is not None else 1
        return exp / 1000.0 * n * 1.5 + 30.0

    def _check_exposure(self, value: Any) -> float:
        v = float(value)
        lo, hi = self.limits.min_exposure_ms, self.limits.max_exposure_ms
        if not (math.isfinite(v) and lo <= v <= hi):
            raise SpectrometerLimitError(
                f"{self.device_id}: exposure {v} ms outside [{lo}, {hi}] ms")
        return v

    def _check_frames(self, value: Any) -> int:
        f = float(value)
        if not math.isfinite(f) or f != int(f) or not (1 <= int(f) <= self.limits.max_frames):
            raise SpectrometerLimitError(
                f"{self.device_id}: frames {value!r} must be a whole number in "
                f"[1, {self.limits.max_frames}]")
        return int(f)

    def _check_total(self, exposure_ms: float, frames: int) -> None:
        total = exposure_ms / 1000.0 * frames
        if total > self.limits.max_total_s:
            raise SpectrometerLimitError(
                f"{self.device_id}: {frames} × {exposure_ms} ms = {total:.0f} s exceeds "
                f"the per-acquisition ceiling of {self.limits.max_total_s:.0f} s")

    # -- driver primitives ------------------------------------------------------

    @abstractmethod
    def _status_raw(self) -> dict: ...

    @abstractmethod
    def _wavelength_axis_raw(self) -> "np.ndarray | list[float] | None": ...

    @abstractmethod
    def _configure_raw(self, settings: dict) -> dict: ...

    @abstractmethod
    def _acquire_raw(self, *, exposure_ms: float | None, frames: int | None, save: bool,
                     base_name: str | None, tag: str | None, meta: dict,
                     timeout_s: float, check_abort: AbortCheck | None,
                     on_frame: FrameCallback | None) -> AcquisitionResult: ...

    @abstractmethod
    def _stop_raw(self) -> None: ...

    @abstractmethod
    def _set_grating_raw(self, grating: str) -> dict: ...

    @abstractmethod
    def _set_center_raw(self, nm: float) -> dict: ...

    @abstractmethod
    def _set_exit_port_raw(self, port: str) -> dict: ...


def _abort_now(check_abort: AbortCheck | None, what: str) -> None:
    """True-or-raise predicate → raise :class:`MotionAborted` / propagate."""
    if check_abort is not None and check_abort():
        raise MotionAborted(f"{what} aborted")


# ── simulated spectrometer ────────────────────────────────────────────────


class SimSpectrometer(Spectrometer):
    """Synthetic Raman-like spectra (a few Lorentzians on a sloped background
    plus shot noise). ``time_scale`` < 1 shortens the simulated exposures."""

    #: Raman shifts (cm⁻¹) and relative heights of the synthetic lines.
    LINES = ((520.7, 1.0), (303.0, 0.08), (950.0, 0.05))

    def __init__(self, *, device_id: str = "sim_ccd", name: str = "",
                 limits: SpectrometerLimits | None = None,
                 laser_line_nm: float | None = 633.0, pixels: int = 1340,
                 nm_per_px: float = 0.0485, time_scale: float = 1.0,
                 seed: int | None = None):
        super().__init__(device_id=device_id, name=name, limits=limits,
                         laser_line_nm=laser_line_nm)
        self._px = int(pixels)
        self._disp = float(nm_per_px)
        self._scale = max(0.0, float(time_scale))
        self._rng = np.random.default_rng(seed)
        self._stop = threading.Event()
        self._running = False
        self.settings: dict[str, Any] = {
            "exposure_ms": 1000.0, "frames": 1, "grating": "[500nm,1200][0][0]",
            "center_wavelength_nm": 664.55, "exit_port": "front",
            "adc_mhz": 1.0, "roi": None, "trigger": None,
        }
        self.acquisitions = 0

    def _axis(self) -> np.ndarray:
        c = float(self.settings["center_wavelength_nm"])
        return c + (np.arange(self._px, dtype=np.float64) - (self._px - 1) / 2) * self._disp

    def _status_raw(self) -> dict:
        s = self.settings
        return {"state": "busy" if self._running else "ready",
                "is_running": self._running, "temperature_c": -70.0,
                "temperature_locked": True, "exposure_ms": s["exposure_ms"],
                "frames": s["frames"], "grating": s["grating"],
                "center_wavelength_nm": s["center_wavelength_nm"],
                "exit_port": s["exit_port"], "adc_mhz": s["adc_mhz"],
                "roi": s["roi"], "trigger_response": s["trigger"],
                "laser_line_nm": self.laser_line_nm, "operator_hold": False,
                "sim": True}

    def _wavelength_axis_raw(self):
        return self._axis()

    def _configure_raw(self, settings: dict) -> dict:
        self.settings.update(settings)
        return dict(self.settings)

    def _spectrum(self, exposure_ms: float) -> np.ndarray:
        nm = self._axis()
        laser = self.laser_line_nm or 633.0
        shift = 1e7 / laser - 1e7 / nm
        y = 200.0 + 0.05 * (nm - nm[0])
        for pos, h in self.LINES:
            y += 4000.0 * h / (1.0 + ((shift - pos) / 4.0) ** 2)
        y *= max(exposure_ms, 1e-3) / 1000.0
        return self._rng.poisson(np.clip(y, 0, None)).astype(np.float64)

    def _acquire_raw(self, *, exposure_ms, frames, save, base_name, tag, meta,
                     timeout_s, check_abort, on_frame) -> AcquisitionResult:
        exp = float(exposure_ms if exposure_ms is not None else self.settings["exposure_ms"])
        n = int(frames if frames is not None else self.settings["frames"])
        # LightField (and the satellites) keep an acquire's exposure / frame count
        # as the camera's settings afterwards — not a one-shot override.
        self.settings["exposure_ms"], self.settings["frames"] = exp, n
        self._stop.clear()
        self._running = True
        started = utc_now_iso()
        deadline = time.monotonic() + float(timeout_s)
        out: list[np.ndarray] = []
        axis = self._axis()
        try:
            for i in range(n):
                end = time.monotonic() + exp / 1000.0 * self._scale
                while True:
                    _abort_now(check_abort, f"{self.device_id}: acquisition")
                    if self._stop.is_set():
                        raise MotionAborted(f"{self.device_id}: acquisition stopped")
                    if time.monotonic() > deadline:
                        raise InstrumentError(f"{self.device_id}: acquisition timed out")
                    left = end - time.monotonic()
                    if left <= 0:
                        break
                    time.sleep(min(0.02, left))
                frame = self._spectrum(exp)
                out.append(frame)
                if on_frame is not None:
                    on_frame(i, frame, axis)
        finally:
            self._running = False
        self.acquisitions += 1
        return AcquisitionResult(frames=out, axis_nm=axis, files=[],
                                 started_utc=started, finished_utc=utc_now_iso(),
                                 meta={"exposure_ms": exp, "frames": n, "sim": True,
                                       "tag": tag or "", "base_name": base_name or ""})

    def _stop_raw(self) -> None:
        self._stop.set()

    def _set_grating_raw(self, grating: str) -> dict:
        self.settings["grating"] = grating
        return {"grating": grating}

    def _set_center_raw(self, nm: float) -> dict:
        self.settings["center_wavelength_nm"] = float(nm)
        return {"center_wavelength_nm": float(nm)}

    def _set_exit_port_raw(self, port: str) -> dict:
        self.settings["exit_port"] = port
        return {"exit_port": port}
