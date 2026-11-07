"""Microscope — the abstraction for a confocal Raman/SHG microscope (WITec alpha300).

Design contract: ``docs/v2/design/optical_station_witec.md`` §6.1.

Same layering as :mod:`mast.instruments.base` and :mod:`mast.instruments.spectrometer`:
public methods are template methods that check :class:`MicroscopeLimits` and the
write-access state, then call a driver primitive (``_*_raw``). Out-of-range is
REFUSED (:class:`MicroscopeLimitError`), never clamped — the vendor COM layer does
no range checking of its own ("The server dll will not check for it").

Write access is handed over by a human: on the WITec PC somebody has to press
"COM Automation" in WITec Control before any client may write. Every public
*write* method therefore goes through :meth:`Microscope._require_write`, which
asks the driver once and raises :class:`WriteAccessDenied` (with an operator hint)
instead of retrying.

A real WITec driver also implements :class:`~mast.instruments.spectrometer.Spectrometer`
(``registry.spectrometer(id)`` returns the same object), so AcquireSpectrum and the
spectrometer setting skills work unchanged. :class:`SimMicroscope` does the same on
top of :class:`~mast.instruments.spectrometer.SimSpectrometer`.
"""

from __future__ import annotations

import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from mast.instruments.base import AbortCheck, InstrumentError, MotionAborted
from mast.instruments.external_stop import ExternalStopMixin
from mast.instruments.spectrometer import SimSpectrometer, SpectrometerLimits

__all__ = [
    "BEAM_PATHS",
    "MicroscopeLimitError",
    "WriteAccessDenied",
    "MicroscopeLimits",
    "Microscope",
    "SimMicroscope",
]

#: beam-path states a skill may request (identity strings — never translated)
BEAM_PATHS = ("raman", "video", "all_off", "confocal", "white_light")


class MicroscopeLimitError(InstrumentError):
    """A requested microscope setting lies outside the configured limits."""


class WriteAccessDenied(InstrumentError):
    """The vendor software has not handed write access to this client."""


def _pair(raw: Any, key: str) -> tuple[float, float] | None:
    if raw is None:
        return None
    try:
        lo, hi = float(raw[0]), float(raw[1])
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"microscope_limits.{key} must be [min, max]") from exc
    if not lo < hi:
        raise ValueError(f"microscope_limits.{key}: need min < max")
    return (lo, hi)


@dataclass(frozen=True)
class MicroscopeLimits:
    """Hub-side limits (``microscope_limits`` in the device entry).

    Stage axes without a configured range are **fail-closed**: a move on an axis
    whose range is ``None`` is refused, because a confocal objective sits a few
    hundred µm above the sample and a guessed Z range is how lenses break.
    ``max_laser_power_mw`` is keyed by laser wavelength rounded to nm (``"532"``) or by
    laser name; a power set on a laser without a key is refused.
    """

    analyzer_deg: tuple[float, float] = (0.0, 360.0)
    x_um: tuple[float, float] | None = None
    y_um: tuple[float, float] | None = None
    z_um: tuple[float, float] | None = None
    max_step_um: float = 1000.0
    max_laser_power_mw: dict[str, float] = field(default_factory=dict)
    max_integration_s: float = 600.0
    max_series_points: int = 1441

    @classmethod
    def from_dict(cls, raw: dict | None) -> "MicroscopeLimits":
        raw = dict(raw or {})
        kw: dict[str, Any] = {}
        for key in ("analyzer_deg", "x_um", "y_um", "z_um"):
            if key in raw:
                kw[key] = _pair(raw[key], key)
        for key in ("max_step_um", "max_integration_s"):
            if raw.get(key) is not None:
                kw[key] = float(raw[key])
        if raw.get("max_series_points") is not None:
            kw["max_series_points"] = int(raw["max_series_points"])
        if raw.get("max_laser_power_mw"):
            kw["max_laser_power_mw"] = {str(k): float(v)
                                        for k, v in dict(raw["max_laser_power_mw"]).items()}
        if kw.get("analyzer_deg") is None:
            kw.pop("analyzer_deg", None)
        return cls(**kw)

    def as_dict(self) -> dict:
        return {"analyzer_deg": list(self.analyzer_deg),
                "x_um": list(self.x_um) if self.x_um else None,
                "y_um": list(self.y_um) if self.y_um else None,
                "z_um": list(self.z_um) if self.z_um else None,
                "max_step_um": self.max_step_um,
                "max_laser_power_mw": dict(self.max_laser_power_mw),
                "max_integration_s": self.max_integration_s,
                "max_series_points": self.max_series_points}


def _num(value: Any, what: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise MicroscopeLimitError(f"{what} must be a number, got {value!r}") from exc
    if not math.isfinite(v):
        raise MicroscopeLimitError(f"{what} must be finite, got {value!r}")
    return v


class Microscope(ExternalStopMixin, ABC):
    """Abstract confocal microscope. Drivers implement the ``_*_raw`` primitives."""

    def __init__(self, *, device_id: str = "", name: str = "",
                 microscope_limits: MicroscopeLimits | None = None):
        self.device_id = device_id
        self.name = name or device_id
        self.microscope_limits = microscope_limits or MicroscopeLimits()
        self._mic_lock = threading.RLock()

    # -- write access ------------------------------------------------------------

    def has_write_access(self) -> bool:
        return bool(self._has_write_access_raw())

    def request_write_access(self) -> bool:
        """Ask once; ``False`` when the operator has not allowed remote write access."""
        with self._mic_lock:
            if self._has_write_access_raw():
                return True
            return bool(self._request_write_access_raw())

    def release_write_access(self) -> None:
        with self._mic_lock:
            self._release_write_access_raw()

    def _require_write(self, what: str) -> None:
        if self.has_write_access() or self.request_write_access():
            return
        raise WriteAccessDenied(
            f"{self.device_id}: {what} needs write access — the operator must press "
            "'COM Automation' in WITec Control first")

    # -- status ------------------------------------------------------------------

    def microscope_status(self) -> dict:
        """Read-only snapshot (never needs write access)."""
        st = dict(self._microscope_status_raw() or {})
        st.setdefault("device_id", self.device_id)
        st.setdefault("write_access", self.has_write_access())
        return st

    # -- analyzer ----------------------------------------------------------------

    def get_analyzer_deg(self) -> float:
        return float(self._get_analyzer_raw())

    def set_analyzer_deg(self, deg: Any) -> float:
        d = _num(deg, "analyzer angle")
        lo, hi = self.microscope_limits.analyzer_deg
        if not lo <= d <= hi:
            raise MicroscopeLimitError(f"analyzer angle {d}° outside [{lo}, {hi}]°")
        self._require_write("set analyzer angle")
        with self._mic_lock:
            return float(self._set_analyzer_raw(d))

    def set_analyzer_in(self, inserted: bool) -> bool:
        self._require_write("move analyzer in/out")
        with self._mic_lock:
            return bool(self._set_analyzer_in_raw(bool(inserted)))

    # -- beam path ---------------------------------------------------------------

    def set_beam_path(self, path: str) -> str:
        p = str(path or "").strip().lower()
        if p not in BEAM_PATHS:
            raise MicroscopeLimitError(f"beam path {path!r} not one of {BEAM_PATHS}")
        self._require_write("set beam path")
        with self._mic_lock:
            return str(self._set_beam_path_raw(p))

    # -- lasers ------------------------------------------------------------------

    def list_lasers(self) -> list[dict]:
        """``[{index, name, wavelength_nm, power_controllable, selected}]``."""
        return [dict(x) for x in self._list_lasers_raw()]

    def _find_laser(self, ref: Any) -> dict:
        lasers = self.list_lasers()
        for las in lasers:
            if (str(ref) == str(las.get("index")) or str(ref) == str(las.get("name"))
                    or (las.get("wavelength_nm") is not None
                        and str(ref) == str(int(round(float(las["wavelength_nm"])))))):
                return las
        raise MicroscopeLimitError(
            f"no laser {ref!r}; available: {[l.get('name') for l in lasers]}")

    def select_laser(self, ref: Any) -> dict:
        las = self._find_laser(ref)
        self._require_write("select laser")
        with self._mic_lock:
            self._select_laser_raw(int(las["index"]))
        return las

    def _power_cap(self, las: dict) -> float:
        caps = self.microscope_limits.max_laser_power_mw
        keys = [str(las.get("name"))]
        if las.get("wavelength_nm") is not None:
            keys.insert(0, str(int(round(float(las["wavelength_nm"])))))
        for k in keys:
            if k in caps:
                return float(caps[k])
        raise MicroscopeLimitError(
            f"no max_laser_power_mw configured for laser {las.get('name')!r} — refused")

    def set_laser_power_mw(self, mw: Any) -> float:
        v = _num(mw, "laser power")
        sel = [l for l in self.list_lasers() if l.get("selected")]
        if not sel:
            raise MicroscopeLimitError("no laser selected")
        las = sel[0]
        if not las.get("power_controllable"):
            raise MicroscopeLimitError(f"laser {las.get('name')!r} has no software power control")
        cap = self._power_cap(las)
        if not 0.0 <= v <= cap:
            raise MicroscopeLimitError(f"laser power {v} mW outside [0, {cap}] mW")
        self._require_write("set laser power")
        with self._mic_lock:
            return float(self._set_laser_power_raw(v))

    def get_laser_power_mw(self) -> float | None:
        v = self._get_laser_power_raw()
        return None if v is None else float(v)

    def set_shutter(self, open_: bool) -> bool:
        """Closing is always allowed to be attempted; opening needs write access."""
        if open_:
            self._require_write("open laser shutter")
        with self._mic_lock:
            return bool(self._set_shutter_raw(bool(open_)))

    # -- state presets -----------------------------------------------------------

    def list_states(self) -> list[str]:
        return [str(s) for s in self._list_states_raw()]

    def load_state(self, name: str) -> str:
        states = self.list_states()
        if str(name) not in states:
            raise MicroscopeLimitError(f"no microscope state {name!r}; available: {states}")
        self._require_write("load microscope state")
        with self._mic_lock:
            return str(self._load_state_raw(str(name)))

    # -- stage ---------------------------------------------------------------------

    def get_stage_position_um(self) -> dict:
        pos = dict(self._get_stage_raw())
        return {k: (None if pos.get(k) is None else float(pos[k])) for k in ("x", "y", "z")}

    def move_stage_um(self, *, x: Any = None, y: Any = None, z: Any = None,
                      check_abort: AbortCheck | None = None) -> dict:
        lim = self.microscope_limits
        cur = self.get_stage_position_um()
        target: dict[str, float] = {}
        for axis, value, rng in (("x", x, lim.x_um), ("y", y, lim.y_um), ("z", z, lim.z_um)):
            if value is None:
                continue
            v = _num(value, f"stage {axis}")
            if rng is None:
                raise MicroscopeLimitError(
                    f"stage {axis}: no travel range configured — refused (fail-closed)")
            if not rng[0] <= v <= rng[1]:
                raise MicroscopeLimitError(f"stage {axis}={v} µm outside [{rng[0]}, {rng[1]}] µm")
            here = cur.get(axis)
            if here is None:
                # fail closed like the travel range: a step limit that cannot be applied is not a limit
                raise MicroscopeLimitError(
                    f"stage {axis}: the current position cannot be read, so the step to {v} µm cannot be checked "
                    f"against max_step_um={lim.max_step_um} — refused")
            if abs(v - float(here)) > lim.max_step_um:
                raise MicroscopeLimitError(
                    f"stage {axis}: step {abs(v - float(here)):.1f} µm exceeds "
                    f"max_step_um={lim.max_step_um}")
            target[axis] = v
        if not target:
            raise MicroscopeLimitError("move_stage_um: give at least one of x, y, z")
        self._require_write("move stage")
        with self._mic_lock:
            return dict(self._move_stage_raw(target, check_abort))

    def stop_stage(self) -> None:
        self._stop_stage_raw()

    # -- video / focus -----------------------------------------------------------

    def acquire_video_image(self, path: str | Path) -> str:
        self._require_write("acquire video image")
        with self._mic_lock:
            return str(self._acquire_video_raw(str(path)))

    def autofocus(self, *, check_abort: AbortCheck | None = None) -> dict:
        self._require_write("autofocus")
        with self._mic_lock:
            return dict(self._autofocus_raw(check_abort))

    # -- sequences -----------------------------------------------------------------

    def run_analyzer_series(self, *, start_deg: Any, stop_deg: Any, points: Any,
                            integration_s: Any, accumulations: Any = 1, name: str = "",
                            check_abort: AbortCheck | None = None) -> dict:
        lim = self.microscope_limits
        a0, a1 = _num(start_deg, "start_deg"), _num(stop_deg, "stop_deg")
        lo, hi = lim.analyzer_deg
        for a in (a0, a1):
            if not lo <= a <= hi:
                raise MicroscopeLimitError(f"analyzer angle {a}° outside [{lo}, {hi}]°")
        n = int(_num(points, "points"))
        if not 2 <= n <= lim.max_series_points:
            raise MicroscopeLimitError(f"points={n} outside [2, {lim.max_series_points}]")
        t = _num(integration_s, "integration_s")
        if not 0 < t <= lim.max_integration_s:
            raise MicroscopeLimitError(f"integration_s={t} outside (0, {lim.max_integration_s}]")
        acc = int(_num(accumulations, "accumulations"))
        if acc < 1:
            raise MicroscopeLimitError("accumulations must be ≥ 1")
        self._require_write("run analyzer series")
        with self._mic_lock:
            return dict(self._run_analyzer_series_raw(a0, a1, n, t, acc, str(name or ""),
                                                      check_abort))

    def save_project(self, directory: str | Path, name: str) -> str:
        if not str(name or "").strip():
            raise MicroscopeLimitError("project name must not be empty")
        self._require_write("save project")
        with self._mic_lock:
            return str(self._save_project_raw(str(directory), str(name)))

    def stop_all(self) -> None:
        """Panic stop: sequencers and stage. Never needs write access; never raises."""
        try:
            self._stop_all_raw()
        except Exception:  # noqa: BLE001 - a stop must not throw
            pass

    def close(self) -> None:  # pragma: no cover - drivers override
        pass

    # -- driver primitives ----------------------------------------------------

    @abstractmethod
    def _has_write_access_raw(self) -> bool: ...
    @abstractmethod
    def _request_write_access_raw(self) -> bool: ...
    @abstractmethod
    def _release_write_access_raw(self) -> None: ...
    @abstractmethod
    def _microscope_status_raw(self) -> dict: ...
    @abstractmethod
    def _get_analyzer_raw(self) -> float: ...
    @abstractmethod
    def _set_analyzer_raw(self, deg: float) -> float: ...
    @abstractmethod
    def _set_analyzer_in_raw(self, inserted: bool) -> bool: ...
    @abstractmethod
    def _set_beam_path_raw(self, path: str) -> str: ...
    @abstractmethod
    def _list_lasers_raw(self) -> list[dict]: ...
    @abstractmethod
    def _select_laser_raw(self, index: int) -> None: ...
    @abstractmethod
    def _set_laser_power_raw(self, mw: float) -> float: ...
    @abstractmethod
    def _get_laser_power_raw(self) -> float | None: ...
    @abstractmethod
    def _set_shutter_raw(self, open_: bool) -> bool: ...
    @abstractmethod
    def _list_states_raw(self) -> list[str]: ...
    @abstractmethod
    def _load_state_raw(self, name: str) -> str: ...
    @abstractmethod
    def _get_stage_raw(self) -> dict: ...
    @abstractmethod
    def _move_stage_raw(self, target: dict, check_abort: AbortCheck | None) -> dict: ...
    @abstractmethod
    def _stop_stage_raw(self) -> None: ...
    @abstractmethod
    def _acquire_video_raw(self, path: str) -> str: ...
    @abstractmethod
    def _autofocus_raw(self, check_abort: AbortCheck | None) -> dict: ...
    @abstractmethod
    def _run_analyzer_series_raw(self, start_deg: float, stop_deg: float, points: int,
                                 integration_s: float, accumulations: int, name: str,
                                 check_abort: AbortCheck | None) -> dict: ...
    @abstractmethod
    def _save_project_raw(self, directory: str, name: str) -> str: ...
    @abstractmethod
    def _stop_all_raw(self) -> None: ...


# ── simulated microscope ──────────────────────────────────────────────────


class SimMicroscope(SimSpectrometer, Microscope):
    """In-memory microscope + spectrometer for tests and offline runs.

    * write access starts ``granted`` unless ``write_access=False`` (then
      :meth:`grant_write_access` simulates the operator pressing the button);
    * the spectrum carries Raman lines for the selected laser, modulated by the
      analyzer angle (cos²); if :attr:`shg_source` is set to a callable returning
      ``(excitation_nm, power_w)`` or ``None``, an SHG peak at ``excitation_nm / 2``
      with height ∝ power² is added (what the SHG protocols test against).
    """

    LASERS = ({"index": 0, "name": "532 nm", "wavelength_nm": 532.0, "power_controllable": True},
              {"index": 1, "name": "633 nm", "wavelength_nm": 633.0, "power_controllable": True},
              {"index": 2, "name": "355 nm", "wavelength_nm": 355.0, "power_controllable": False},
              {"index": 3, "name": "1064 nm", "wavelength_nm": 1064.0, "power_controllable": False})
    STATES = ("RayShield 532 - G300", "RayShield 532 - G600", "RayShield 532 - G1800",
              "Raman 633 - G300", "Raman 355 - G300", "SHG", "variable wavelength mode")

    def __init__(self, *, device_id: str = "sim_witec", name: str = "",
                 limits: SpectrometerLimits | None = None,
                 microscope_limits: MicroscopeLimits | None = None,
                 write_access: bool = True, time_scale: float = 0.0,
                 seed: int | None = 0):
        SimSpectrometer.__init__(self, device_id=device_id, name=name, limits=limits,
                                 laser_line_nm=532.0, time_scale=time_scale, seed=seed)
        # Si 520.7 cm⁻¹ under 532 nm sits at 547 nm — the station's most common centre
        self.settings["center_wavelength_nm"] = 547.0
        Microscope.__init__(self, device_id=device_id, name=name,
                            microscope_limits=microscope_limits or MicroscopeLimits(
                                x_um=(-25000.0, 25000.0), y_um=(-25000.0, 25000.0),
                                z_um=(-100.0, 100.0), max_step_um=5000.0,
                                max_laser_power_mw={"532": 20.0, "633": 10.0}))
        self._allowed = bool(write_access)
        self._write = bool(write_access)
        self.analyzer_deg = 0.0
        self.analyzer_in = True
        self.beam_path = "video"
        self.selected_laser = 0
        self.laser_power_mw = {0: 1.0, 1: 1.0}
        self.shutter_open = False
        self.state = self.STATES[0]
        self.stage = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.shg_source: Callable[[], tuple[float, float] | None] | None = None
        self.series_runs: list[dict] = []
        self.saved_projects: list[str] = []
        self.calls: list[tuple[str, Any]] = []

    # operator simulation
    def grant_write_access(self, allowed: bool = True) -> None:
        self._allowed = bool(allowed)
        if not allowed:
            self._write = False

    def _has_write_access_raw(self) -> bool:
        return self._write

    def _request_write_access_raw(self) -> bool:
        self._write = self._allowed
        return self._write

    def _release_write_access_raw(self) -> None:
        self._write = False

    def _microscope_status_raw(self) -> dict:
        las = self.LASERS[self.selected_laser]
        return {"connected": True, "sim": True, "analyzer_deg": self.analyzer_deg,
                "analyzer_in": self.analyzer_in, "beam_path": self.beam_path,
                "laser": las["name"], "laser_wavelength_nm": las["wavelength_nm"],
                "laser_power_mw": self.laser_power_mw.get(self.selected_laser),
                "shutter_open": self.shutter_open, "state": self.state,
                "stage_um": dict(self.stage), "sequencer_active": False}

    def _get_analyzer_raw(self) -> float:
        return self.analyzer_deg

    def _set_analyzer_raw(self, deg: float) -> float:
        self.calls.append(("analyzer", deg))
        self.analyzer_deg = float(deg)
        return self.analyzer_deg

    def _set_analyzer_in_raw(self, inserted: bool) -> bool:
        self.analyzer_in = inserted
        return inserted

    def _set_beam_path_raw(self, path: str) -> str:
        self.beam_path = path
        return path

    def _list_lasers_raw(self) -> list[dict]:
        return [dict(l, selected=(l["index"] == self.selected_laser)) for l in self.LASERS]

    def _select_laser_raw(self, index: int) -> None:
        self.selected_laser = int(index)
        self.laser_line_nm = float(self.LASERS[int(index)]["wavelength_nm"])

    def _set_laser_power_raw(self, mw: float) -> float:
        self.laser_power_mw[self.selected_laser] = float(mw)
        return float(mw)

    def _get_laser_power_raw(self) -> float | None:
        return self.laser_power_mw.get(self.selected_laser)

    def _set_shutter_raw(self, open_: bool) -> bool:
        self.shutter_open = open_
        return open_

    def _list_states_raw(self) -> list[str]:
        return list(self.STATES)

    def _load_state_raw(self, name: str) -> str:
        self.state = name
        return name

    def _get_stage_raw(self) -> dict:
        return dict(self.stage)

    def _move_stage_raw(self, target: dict, check_abort: AbortCheck | None) -> dict:
        if check_abort is not None and check_abort():
            raise MotionAborted(f"{self.device_id}: stage move aborted")
        self.stage.update({k: float(v) for k, v in target.items()})
        return dict(self.stage)

    def _stop_stage_raw(self) -> None:
        self.calls.append(("stop_stage", None))

    def _acquire_video_raw(self, path: str) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        img = (np.linspace(0, 255, 64 * 64).reshape(64, 64)).astype(np.uint8)
        np.save(p.with_suffix(".npy"), img)
        return str(p.with_suffix(".npy"))

    def _autofocus_raw(self, check_abort: AbortCheck | None) -> dict:
        if check_abort is not None and check_abort():
            raise MotionAborted(f"{self.device_id}: autofocus aborted")
        return {"z_um": self.stage["z"], "ok": True}

    def _run_analyzer_series_raw(self, start_deg, stop_deg, points, integration_s,
                                 accumulations, name, check_abort) -> dict:
        angles = np.linspace(start_deg, stop_deg, points)
        rows = []
        for a in angles:
            if check_abort is not None and check_abort():
                raise MotionAborted(f"{self.device_id}: analyzer series aborted")
            self.analyzer_deg = float(a)
            rows.append(self._spectrum(integration_s * 1000.0 * accumulations))
        run = {"name": name, "angles_deg": angles.tolist(), "points": int(points),
               "integration_s": integration_s, "accumulations": accumulations,
               "axis_nm": self._axis().tolist(), "spectra": np.asarray(rows).tolist()}
        self.series_runs.append(run)
        return {"name": name, "points": int(points), "angles_deg": run["angles_deg"],
                "saved_in_project": True}

    def _save_project_raw(self, directory: str, name: str) -> str:
        path = str(Path(directory) / f"{name}.wip")
        self.saved_projects.append(path)
        return path

    def _stop_all_raw(self) -> None:
        self.calls.append(("stop_all", None))
        self._stop.set()

    # spectrum model: dark counts; + Raman lines of the selected laser (only with the
    # WITec shutter open, analyzer cos²); + an SHG peak from an external source (that
    # beam does not pass the WITec laser shutter)
    def _spectrum(self, exposure_ms: float) -> np.ndarray:
        t = max(float(exposure_ms), 1e-3) / 1000.0
        y = self._rng.poisson(np.full(self._px, 500.0 * t)).astype(np.float64)
        if self.shutter_open:
            y += SimSpectrometer._spectrum(self, exposure_ms) * (
                0.25 + 0.75 * math.cos(math.radians(self.analyzer_deg)) ** 2)
        src = self.shg_source() if self.shg_source is not None else None
        if src:
            exc_nm, power_w = float(src[0]), float(src[1])
            nm = self._axis()
            peak = 5.0e9 * power_w ** 2 * t
            y += self._rng.poisson(np.clip(
                peak * np.exp(-0.5 * ((nm - exc_nm / 2.0) / 0.8) ** 2), 0, None))
        return y

    def close(self) -> None:
        self._write = False
