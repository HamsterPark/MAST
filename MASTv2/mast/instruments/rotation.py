"""RotationAxis — a MotionAxis whose native unit is degrees (polariser, waveplate).

A rotation stage is a linear axis with one extra fact: angles that differ by a
full turn point the same way. That fact is opt-in, never assumed:

* **Soft limits stay Layer-0.** ``AxisConfig.min_pos/max_pos`` are degrees and
  the base class still refuses any physical target outside them — a cable-wound
  mount must not be spun a second turn because 370° "means" 10°.
* ``wrap`` (off by default) lets a requested angle be re-expressed as
  ``angle + k·period`` when that lands inside the limits. Without it the request
  is taken literally.
* ``shortest_path`` (off by default, needs ``wrap``) picks, among the
  equivalent in-range targets, the one closest to the current angle. Without it
  the candidate closest to the request itself wins, so the reading the caller
  asked for is the one they get back whenever it is reachable.

Relative moves never wrap: ``move_rel(+30)`` turns the mount thirty degrees
forward, it does not pick a "nicer" equivalent that would turn it backwards.

:class:`SimRotationController` is an in-process simulated mount (device type
``sim_rotation``) for tests and office development; it moves at a finite speed
so the wait/abort paths are exercised for real.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from mast.instruments.base import (
    AbortCheck,
    AxisConfig,
    AxisStatus,
    InstrumentError,
    MotionAxis,
    MotionController,
    TravelLimitError,
)

__all__ = [
    "RotationConfig",
    "RotationAxis",
    "SimRotationAxis",
    "SimRotationController",
]


@dataclass(frozen=True)
class RotationConfig:
    """Equivalence rules for one rotation axis (all off by default)."""

    wrap: bool = False
    period_deg: float = 360.0
    shortest_path: bool = False

    def __post_init__(self) -> None:
        if not (self.period_deg > 0 and math.isfinite(self.period_deg)):
            raise ValueError("period_deg must be a positive finite number")

    @classmethod
    def from_dict(cls, raw: dict | None) -> "RotationConfig":
        raw = raw or {}
        return cls(
            wrap=bool(raw.get("wrap", False)),
            period_deg=float(raw.get("period_deg", 360.0)),
            shortest_path=bool(raw.get("shortest_path", False)),
        )


class RotationAxis(MotionAxis):
    """Degrees in, degrees out. Drivers implement the usual ``_*_raw``."""

    KIND = "rotation"

    def __init__(self, config: AxisConfig, controller: MotionController, *,
                 rotation: RotationConfig | None = None):
        super().__init__(config, controller)
        self.rotation = rotation or RotationConfig()

    # -- target resolution -------------------------------------------------

    def normalized(self, angle_deg: float) -> float:
        """*angle_deg* folded into ``[0, period)`` — for display / metadata."""
        p = self.rotation.period_deg
        return float(angle_deg) % p

    def resolve_target(self, angle_deg: float, *, current: float | None = None) -> float:
        """The physical target for a requested angle (see module docstring).

        Raises :class:`TravelLimitError` when no equivalent lies inside the
        soft limits; without ``wrap`` the request is returned unchanged and the
        base class performs the limit check itself."""
        req = float(angle_deg)
        if not math.isfinite(req):
            raise TravelLimitError(f"axis {self.config.name!r}: angle {req} is not finite")
        rot = self.rotation
        if not rot.wrap:
            return req
        lo, hi, p = self.config.min_pos, self.config.max_pos, rot.period_deg
        k_lo = math.ceil((lo - req) / p - 1e-12)
        k_hi = math.floor((hi - req) / p + 1e-12)
        candidates = [req + k * p for k in range(k_lo, k_hi + 1)]
        candidates = [c for c in candidates if lo <= c <= hi]
        if not candidates:
            raise TravelLimitError(
                f"axis {self.config.name!r}: no angle equivalent to {req}° "
                f"(period {p}°) lies inside soft limits [{lo}, {hi}]°")
        ref = req
        if rot.shortest_path:
            ref = self.get_position() if current is None else float(current)
        return min(candidates, key=lambda c: (abs(c - ref), c))

    # -- motion --------------------------------------------------------------

    def move_abs(self, position: float, *, wait: bool = True,
                 timeout: float | None = None,
                 check_abort: AbortCheck | None = None) -> AxisStatus:
        return super().move_abs(self.resolve_target(position), wait=wait,
                                timeout=timeout, check_abort=check_abort)

    def move_rel(self, delta: float, *, wait: bool = True,
                 timeout: float | None = None,
                 check_abort: AbortCheck | None = None) -> AxisStatus:
        # A relative turn is a physical instruction: no re-expression.
        current = self.get_position()
        return MotionAxis.move_abs(self, current + float(delta), wait=wait,
                                   timeout=timeout, check_abort=check_abort)


# ── simulated mount (tests / office development) ──────────────────────────


class SimRotationAxis(RotationAxis):
    """Finite-speed simulated mount. Not homed until :meth:`home` runs, like
    a KDC101 after power-up."""

    SUPPORTS = frozenset({"home"})

    def __init__(self, config: AxisConfig, controller: "SimRotationController", *,
                 rotation: RotationConfig | None = None, speed_deg_s: float = 90.0,
                 start_deg: float = 0.0):
        super().__init__(config, controller, rotation=rotation)
        self._speed = max(1e-6, float(speed_deg_s))
        self._pos = float(start_deg)
        self._target = float(start_deg)
        self._t0 = time.monotonic()
        self._from = float(start_deg)
        self._homed = False
        self._homing = False
        self._lock = threading.Lock()
        self.stops = 0

    def _now_pos(self) -> float:
        dt = time.monotonic() - self._t0
        span = self._target - self._from
        if abs(span) <= self._speed * dt:
            return self._target
        return self._from + math.copysign(self._speed * dt, span)

    def _move_abs_raw(self, target: float) -> None:
        with self._lock:
            self._from = self._now_pos()
            self._target = float(target)
            self._t0 = time.monotonic()

    def _get_position_raw(self) -> float:
        with self._lock:
            return self._now_pos()

    def _get_status_raw(self) -> AxisStatus:
        with self._lock:
            pos = self._now_pos()
            moving = abs(pos - self._target) > 1e-9
            if self._homing and not moving:
                self._homing = False
                self._homed = True
            return AxisStatus(position=pos, moving=moving, on_target=not moving,
                              homed=self._homed, raw={"sim": True})

    def _stop_raw(self) -> None:
        with self._lock:
            pos = self._now_pos()
            self._from = self._target = pos
            self._homing = False
            self.stops += 1

    def _home_raw(self) -> None:
        with self._lock:
            self._from = self._now_pos()
            self._target = 0.0
            self._t0 = time.monotonic()
            self._homing = True


class SimRotationController(MotionController):
    def __init__(self, *, axes: list[AxisConfig],
                 rotation: RotationConfig | None = None, speed_deg_s: float = 90.0):
        super().__init__()
        if not axes:
            raise InstrumentError("SimRotationController needs at least one axis")
        self._axis_configs = list(axes)
        self._rotation = rotation
        self._speed = float(speed_deg_s)

    def _connect_raw(self) -> None:
        pass

    def _close_raw(self) -> None:
        pass

    def _build_axes(self) -> dict[str, MotionAxis]:
        return {c.name: SimRotationAxis(c, self, rotation=self._rotation,
                                        speed_deg_s=self._speed)
                for c in self._axis_configs}
