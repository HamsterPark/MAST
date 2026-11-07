"""OpenLoopAxis — stepping actuators without position feedback (Picomotor 8742,
slip-stick motors run open loop).

The interface is **steps**, not position: :meth:`OpenLoopAxis.jog` is the
primary call. The step counter that comes back is bookkeeping — the sum of the
steps commanded since the counter was last zeroed — and nothing more. A motor
that slipped, stalled or hit a mechanical end still adds its commanded steps.

Safety (Layer-0, enforced here so no caller can skip it):

* every command is capped at ``max_steps_per_cmd`` (from config) and an
  oversize request is REFUSED, never split or clamped behind the caller's back;
* ``AxisConfig.min_pos/max_pos`` bound the **cumulative** counter, in steps.
  That is a fence around how far the motor can have been told to go, which is
  the only thing an open-loop axis knows. After a restart the counter is only
  as good as whoever holds it (the satellite persists it for remote axes;
  :class:`SimOpenLoopAxis` does not).

``move_abs`` / ``move_rel`` are kept so the generic optical skills keep working:
they are expressed against the counter (``move_abs(n)`` = jog by ``n - counter``),
which is also what the 8742's own ``PA`` command means.
"""

from __future__ import annotations

import math
import threading
import time
from abc import abstractmethod

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
    "OpenLoopAxis",
    "SimOpenLoopAxis",
    "SimOpenLoopController",
]


class OpenLoopAxis(MotionAxis):
    """Steps in, a step counter out. Drivers implement ``_jog_raw``,
    ``_get_steps_raw``, ``_set_zero_raw``, ``_get_status_raw``, ``_stop_raw``."""

    KIND = "openloop"

    def __init__(self, config: AxisConfig, controller: MotionController, *,
                 max_steps_per_cmd: int):
        super().__init__(config, controller)
        if int(max_steps_per_cmd) < 1:
            raise ValueError("max_steps_per_cmd must be >= 1")
        self.max_steps_per_cmd = int(max_steps_per_cmd)

    # -- public API ----------------------------------------------------------

    def jog(self, steps: int, *, wait: bool = True, timeout: float | None = None,
            check_abort: AbortCheck | None = None) -> AxisStatus:
        """Step by *steps* (signed). Refuses a command above the per-command cap
        or one that would carry the counter outside the soft limits."""
        n = _as_steps(steps)
        if n == 0:
            return self.get_status()
        if abs(n) > self.max_steps_per_cmd:
            raise TravelLimitError(
                f"axis {self.config.name!r}: {n:+d} steps exceeds the per-command "
                f"cap of {self.max_steps_per_cmd} — issue smaller jogs")
        current = self.get_steps()
        target = current + n
        self._check_limits(target)
        self._abort_point(check_abort, f"jog {n:+d} steps", stop=False)
        with self._controller._io_lock:
            self._jog_raw(n)
        if wait:
            return self.wait_until_settled(target, timeout=timeout,
                                           check_abort=check_abort)
        return self.get_status()

    def get_steps(self) -> int:
        with self._controller._io_lock:
            return int(self._get_steps_raw())

    def set_zero(self) -> None:
        """Declare the current place as step 0 (no motion)."""
        with self._controller._io_lock:
            self._set_zero_raw()

    def move_abs(self, position: float, *, wait: bool = True,
                 timeout: float | None = None,
                 check_abort: AbortCheck | None = None) -> AxisStatus:
        """Counter-absolute: jog by ``position - counter`` (8742 ``PA`` semantics)."""
        return self.jog(_as_steps(position) - self.get_steps(), wait=wait,
                        timeout=timeout, check_abort=check_abort)

    def move_rel(self, delta: float, *, wait: bool = True,
                 timeout: float | None = None,
                 check_abort: AbortCheck | None = None) -> AxisStatus:
        return self.jog(_as_steps(delta), wait=wait, timeout=timeout,
                        check_abort=check_abort)

    # -- MotionAxis primitives in step terms ----------------------------------

    def _move_abs_raw(self, target: float) -> None:
        self._jog_raw(_as_steps(target) - int(self._get_steps_raw()))

    def _get_position_raw(self) -> float:
        return float(self._get_steps_raw())

    def _home_raw(self) -> None:
        raise InstrumentError(
            f"axis {self.config.name!r} is open loop: it has no reference to home "
            "to. Move it where it belongs and declare that step 0 (set_zero).")

    # -- driver primitives -------------------------------------------------------

    @abstractmethod
    def _jog_raw(self, steps: int) -> None: ...

    @abstractmethod
    def _get_steps_raw(self) -> int: ...

    @abstractmethod
    def _set_zero_raw(self) -> None: ...


def _as_steps(value: float) -> int:
    """An integer step count. A fractional request is refused, not rounded:
    rounding 0.4 steps to 0 would report a move that never happened."""
    if isinstance(value, bool):
        raise TypeError("steps must be a number, not bool")
    f = float(value)
    if not math.isfinite(f) or f != int(f):
        raise TravelLimitError(f"steps must be a whole number, got {value!r}")
    return int(f)


# ── simulated stepper (tests / office development) ─────────────────────────


class SimOpenLoopAxis(OpenLoopAxis):
    """Finite-rate simulated stepper; the counter advances while it moves."""

    def __init__(self, config: AxisConfig, controller: "SimOpenLoopController", *,
                 max_steps_per_cmd: int, steps_per_s: float = 2000.0):
        super().__init__(config, controller, max_steps_per_cmd=max_steps_per_cmd)
        self._rate = max(1e-6, float(steps_per_s))
        self._base = 0
        self._pending = 0
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self.jogs: list[int] = []
        self.stops = 0

    def _progress(self) -> int:
        done = int(self._rate * (time.monotonic() - self._t0))
        return int(math.copysign(min(abs(self._pending), done), self._pending)) \
            if self._pending else 0

    def _jog_raw(self, steps: int) -> None:
        with self._lock:
            self._base += self._progress()
            self._pending = int(steps)
            self._t0 = time.monotonic()
            self.jogs.append(int(steps))

    def _get_steps_raw(self) -> int:
        with self._lock:
            return self._base + self._progress()

    def _set_zero_raw(self) -> None:
        with self._lock:
            self._base = -self._progress()

    def _get_status_raw(self) -> AxisStatus:
        with self._lock:
            prog = self._progress()
            moving = abs(prog) < abs(self._pending)
            steps = self._base + prog
            if not moving and self._pending:
                self._base, self._pending = steps, 0
            return AxisStatus(position=float(steps), moving=moving, on_target=None,
                              raw={"steps": steps, "feedback": False, "sim": True})

    def _stop_raw(self) -> None:
        with self._lock:
            self._base += self._progress()
            self._pending = 0
            self.stops += 1


class SimOpenLoopController(MotionController):
    def __init__(self, *, axes: list[AxisConfig], max_steps_per_cmd: int = 2000,
                 steps_per_s: float = 2000.0):
        super().__init__()
        if not axes:
            raise InstrumentError("SimOpenLoopController needs at least one axis")
        self._axis_configs = list(axes)
        self._max = int(max_steps_per_cmd)
        self._rate = float(steps_per_s)

    def _connect_raw(self) -> None:
        pass

    def _close_raw(self) -> None:
        pass

    def _build_axes(self) -> dict[str, MotionAxis]:
        return {c.name: SimOpenLoopAxis(c, self, max_steps_per_cmd=self._max,
                                        steps_per_s=self._rate)
                for c in self._axis_configs}
