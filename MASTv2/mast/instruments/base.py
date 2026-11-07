"""MotionController / MotionAxis abstractions for optical-bench hardware.

Every concrete driver (PZTC nm003, PI GCS, Thorlabs Kinesis) implements
these interfaces so the registry, the delay-line wrapper and the optics
skills never care which vendor is behind an axis.

Safety model — soft travel limits are LAYER-0:

``MotionAxis.move_abs`` / ``move_rel`` are concrete template methods on the
base class. They clamp-check the target against ``AxisConfig`` travel
limits and raise :class:`TravelLimitError` BEFORE delegating to the
driver's ``_move_abs_raw``. Drivers only ever see validated targets, so no
caller — skill, agent, GUI, or a hallucinated LLM parameter — can command
a move outside the configured range. This mirrors the SafetyGuard
philosophy: hard checks live below the LLM, not beside it.

Threading: one controller = one serial link = one lock. All axis calls on
a controller serialise through ``controller._io_lock`` so concurrent skill
threads cannot interleave protocol transactions.

Abort: every wait loop (settle, homing) takes an optional ``check_abort``.
Both predicate shapes used in MAST are accepted — ``ExecutionContext.check_abort``
*returns* True, the satellite hub's contract (§6) *raises*. Either way the axis is
stopped before control returns, so an operator stop never leaves a stage
walking towards a target nobody wants any more.

Beyond move / stop / position (manual control panel, contract §13.1): a driver
declares in :attr:`MotionAxis.SUPPORTS` what else its vendor documents —
``home``, ``velocity`` (:meth:`MotionAxis.set_velocity`, refused above the
configured ``max_velocity``, never clamped), ``servo``
(:meth:`MotionAxis.set_servo`) — and :meth:`MotionAxis.status_details` reports
whatever the controller can answer. Anything undocumented raises
:class:`OperationNotSupported` instead of being guessed.
"""

from __future__ import annotations

import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

__all__ = [
    "InstrumentError",
    "InstrumentUnavailable",
    "TravelLimitError",
    "MotionTimeout",
    "MotionAborted",
    "OperationNotSupported",
    "AbortCheck",
    "AxisConfig",
    "AxisStatus",
    "MotionAxis",
    "MotionController",
    "serial_open_hint",
]

#: An abort predicate: returns truthy to abort, or raises (the exception is
#: re-raised after the motion has been stopped). ``None`` = never abort.
AbortCheck = Callable[[], object]


# ── errors ────────────────────────────────────────────────────────────────


class InstrumentError(RuntimeError):
    """Base error for all instrument-driver failures."""


class InstrumentUnavailable(InstrumentError):
    """Hardware/driver stack not usable (no port, no DLL, no device).

    The graceful-degradation signal: callers catch this to report
    "unavailable" instead of crashing on machines without the bench.
    """


def serial_open_hint(port: object) -> str:
    """Who may be holding *port* (``core.port_guard``), appended to the error of
    an open that FAILED — asked only afterwards, never by opening the port. ``""``
    when nothing is known; never raises (a hint must not change the error path)."""
    try:
        from mast.core.port_guard import serial_open_hint as _hint

        return _hint(str(port or ""))
    except Exception:  # noqa: BLE001
        return ""


class TravelLimitError(InstrumentError):
    """Requested target lies outside the configured soft travel limits."""


class MotionTimeout(InstrumentError):
    """Axis did not reach the target / finish homing within the deadline."""


class MotionAborted(InstrumentError):
    """The caller's abort predicate fired; the axis was told to stop first."""


class OperationNotSupported(InstrumentError):
    """This driver has no documented way to do it — not a parameter problem."""


# ── configuration & status ────────────────────────────────────────────────


@dataclass(frozen=True)
class AxisConfig:
    """Static description of one axis, from the device inventory (config).

    Positions are in the axis's *native unit* (``unit``). Soft limits are
    mandatory: a driver refuses to construct an axis without a finite
    travel range — "unlimited" optical hardware does not exist, and an
    unconfigured range must fail loudly rather than move blindly.
    """

    name: str                 # logical axis name, e.g. "x", "delay"
    channel: int | str        # controller channel: PZTC 1-3, PI "A"/"1", ...
    min_pos: float            # soft travel limit, native unit (inclusive)
    max_pos: float            # soft travel limit, native unit (inclusive)
    unit: str = "um"          # native unit label, documentation + UI only
    default_speed: float | None = None   # native unit / s, driver-specific
    settle_s: float = 0.0     # extra settle wait after on-target, seconds
    #: native unit / s: :meth:`MotionAxis.set_velocity` refuses anything above it
    #: (None = only the driver's documented bound applies)
    max_velocity: float | None = None

    def __post_init__(self) -> None:
        if not (self.min_pos < self.max_pos):
            raise ValueError(
                f"axis {self.name!r}: min_pos {self.min_pos} must be < "
                f"max_pos {self.max_pos}"
            )
        if self.max_velocity is not None and not (
                math.isfinite(self.max_velocity) and self.max_velocity > 0):
            raise ValueError(
                f"axis {self.name!r}: max_velocity {self.max_velocity} must be > 0")


@dataclass
class AxisStatus:
    """Snapshot of one axis. ``on_target`` is None when the controller
    cannot report it (open-loop hardware)."""

    position: float
    moving: bool
    on_target: bool | None = None
    homed: bool | None = None
    raw: dict = field(default_factory=dict)


# ── axis ──────────────────────────────────────────────────────────────────


class MotionAxis(ABC):
    """One movable axis. Subclasses implement the ``_*_raw`` primitives;
    the public API (limit checks, wait loops) lives here and is final in
    spirit — drivers must NOT override ``move_abs`` / ``move_rel``.
    """

    #: what kind of axis this is: "linear" | "rotation" | "openloop"
    KIND = "linear"
    #: documented operations beyond move / stop / position: "home", "velocity",
    #: "servo". The manual panel offers exactly these (tests pin each against
    #: the driver's overrides).
    SUPPORTS: frozenset[str] = frozenset()
    #: default polling cadence for wait-until-on-target loops (seconds)
    POLL_INTERVAL_S = 0.05
    #: default motion deadline when the caller gives none (seconds)
    DEFAULT_TIMEOUT_S = 60.0

    def __init__(self, config: AxisConfig, controller: "MotionController"):
        self.config = config
        self._controller = controller

    # -- public API (template methods, limit-checked) ----------------------

    def move_abs(
        self,
        position: float,
        *,
        wait: bool = True,
        timeout: float | None = None,
        check_abort: AbortCheck | None = None,
    ) -> AxisStatus:
        """Move to *position* (native unit). Raises :class:`TravelLimitError`
        if the target is outside the soft limits; blocks until on-target
        when ``wait`` (raising :class:`MotionTimeout` on deadline, or
        :class:`MotionAborted` once ``check_abort`` fires — after stopping)."""
        target = float(position)
        self._check_limits(target)
        self._abort_point(check_abort, f"move to {target}", stop=False)
        with self._controller._io_lock:
            self._move_abs_raw(target)
        if wait:
            return self.wait_until_settled(target, timeout=timeout,
                                           check_abort=check_abort)
        return self.get_status()

    def move_rel(
        self,
        delta: float,
        *,
        wait: bool = True,
        timeout: float | None = None,
        check_abort: AbortCheck | None = None,
    ) -> AxisStatus:
        """Relative move. Resolved against a fresh position reading so the
        limit check covers the true final target."""
        current = self.get_position()
        return self.move_abs(current + float(delta), wait=wait, timeout=timeout,
                             check_abort=check_abort)

    def get_position(self) -> float:
        with self._controller._io_lock:
            return self._get_position_raw()

    def get_status(self) -> AxisStatus:
        with self._controller._io_lock:
            return self._get_status_raw()

    def stop(self) -> None:
        """Halt motion. Never limit-checked, never blocking — this is the
        panic path and must stay callable in any state."""
        with self._controller._io_lock:
            self._stop_raw()

    def status_details(self) -> dict:
        """Everything the controller can report about this axis: the
        :class:`AxisStatus` fields plus the driver's extras (velocity, servo,
        limit switches …). A field the controller cannot answer is left out;
        nothing here moves the axis."""
        st = self.get_status()
        out: dict = {"position": st.position, "moving": st.moving,
                     "on_target": st.on_target, "homed": st.homed}
        with self._controller._io_lock:
            try:
                out.update(self._status_details_raw() or {})
            except InstrumentUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - the position read above stands
                out["details_error"] = f"{type(exc).__name__}: {exc}"
        return out

    def set_velocity(self, velocity: float) -> float | None:
        """Set the closed-loop velocity (native unit / s) for later moves.
        Refused — never clamped — when not > 0 or above the configured
        ``max_velocity``. Returns the value read back (None if the controller
        cannot read it back)."""
        v = float(velocity)
        cfg = self.config
        if not (math.isfinite(v) and v > 0):
            raise InstrumentError(f"axis {cfg.name!r}: velocity {velocity} must be > 0")
        if cfg.max_velocity is not None and v > cfg.max_velocity:
            raise InstrumentError(
                f"axis {cfg.name!r}: velocity {v:g} {cfg.unit}/s above the configured "
                f"max_velocity {cfg.max_velocity:g} {cfg.unit}/s — refused (not clamped)")
        with self._controller._io_lock:
            return self._set_velocity_raw(v)

    def set_servo(self, on: bool) -> bool | None:
        """Closed-loop servo on/off. Returns the state read back (None if the
        controller cannot read it back)."""
        with self._controller._io_lock:
            return self._set_servo_raw(bool(on))

    def home(
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,
        check_abort: AbortCheck | None = None,
    ) -> AxisStatus:
        """Reference/zero the axis (drivers without a homing concept raise
        InstrumentError). Position afterwards is defined by the hardware."""
        self._abort_point(check_abort, "homing", stop=False)
        with self._controller._io_lock:
            self._home_raw()
        if wait:
            deadline = time.monotonic() + (timeout or self.DEFAULT_TIMEOUT_S)
            while time.monotonic() < deadline:
                self._abort_point(check_abort, "homing")
                status = self.get_status()
                if not status.moving and (status.homed is not False):
                    return status
                time.sleep(self.POLL_INTERVAL_S)
            raise MotionTimeout(
                f"axis {self.config.name!r}: homing did not finish within "
                f"{timeout or self.DEFAULT_TIMEOUT_S:.1f}s"
            )
        return self.get_status()

    def wait_until_settled(
        self,
        target: float,
        *,
        timeout: float | None = None,
        check_abort: AbortCheck | None = None,
    ) -> AxisStatus:
        """Poll until the controller reports on-target/not-moving, then apply
        the configured extra ``settle_s``. Falls back to a position-delta
        criterion when the hardware cannot report on-target."""
        deadline = time.monotonic() + (timeout or self.DEFAULT_TIMEOUT_S)
        status = self.get_status()
        while time.monotonic() < deadline:
            self._abort_point(check_abort, f"move to {target}")
            status = self.get_status()
            if status.on_target is True:
                break
            if status.on_target is None and not status.moving:
                break
            time.sleep(self.POLL_INTERVAL_S)
        else:
            raise MotionTimeout(
                f"axis {self.config.name!r}: move to {target} not settled "
                f"within {timeout or self.DEFAULT_TIMEOUT_S:.1f}s "
                f"(pos={status.position}, moving={status.moving})"
            )
        if self.config.settle_s > 0:
            self._sleep_checked(self.config.settle_s, check_abort,
                                f"settle after move to {target}")
            status = self.get_status()
        return status

    # -- abort plumbing --------------------------------------------------------

    def _abort_point(self, check_abort: AbortCheck | None, what: str, *,
                     stop: bool = True) -> None:
        """Consult *check_abort*; on abort stop the axis (unless nothing was
        started yet) and raise. A predicate that raises is honoured the same
        way and its own exception propagates — the hub contract's shape."""
        if check_abort is None:
            return
        try:
            fired = bool(check_abort())
        except BaseException:
            if stop:
                self._stop_quietly()
            raise
        if fired:
            if stop:
                self._stop_quietly()
            raise MotionAborted(
                f"axis {self.config.name!r}: {what} aborted"
                + (" — stop sent" if stop else " before any motion")
            )

    def _sleep_checked(self, seconds: float, check_abort: AbortCheck | None,
                       what: str) -> None:
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            self._abort_point(check_abort, what)
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(self.POLL_INTERVAL_S, left))

    def _stop_quietly(self) -> None:
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - the abort itself must still surface
            pass

    # -- limit enforcement (Layer-0) ---------------------------------------

    def _check_limits(self, target: float) -> None:
        cfg = self.config
        if not (cfg.min_pos <= target <= cfg.max_pos):
            raise TravelLimitError(
                f"axis {cfg.name!r}: target {target} {cfg.unit} outside "
                f"soft limits [{cfg.min_pos}, {cfg.max_pos}] {cfg.unit}"
            )

    # -- driver primitives (hold no locks; base class already locked) ------

    @abstractmethod
    def _move_abs_raw(self, target: float) -> None: ...

    @abstractmethod
    def _get_position_raw(self) -> float: ...

    @abstractmethod
    def _get_status_raw(self) -> AxisStatus: ...

    @abstractmethod
    def _stop_raw(self) -> None: ...

    def _home_raw(self) -> None:
        raise InstrumentError(
            f"axis {self.config.name!r}: driver has no homing support"
        )

    def _status_details_raw(self) -> dict:
        return {}

    def _set_velocity_raw(self, velocity: float) -> float | None:
        raise OperationNotSupported(
            f"axis {self.config.name!r} ({type(self).__name__}): no documented velocity "
            "setting in this driver")

    def _set_servo_raw(self, on: bool) -> bool | None:
        raise OperationNotSupported(
            f"axis {self.config.name!r} ({type(self).__name__}): no documented servo "
            "switch in this driver")


# ── controller ────────────────────────────────────────────────────────────


class MotionController(ABC):
    """One physical controller box (may drive several axes).

    Lifecycle: construction is cheap and NEVER touches hardware;
    :meth:`connect` opens the link (raising :class:`InstrumentUnavailable`
    on machines without it); :meth:`close` is idempotent. ``axis()`` on a
    non-connected controller connects lazily.
    """

    def __init__(self) -> None:
        self._io_lock = threading.RLock()
        self._axes: dict[str, MotionAxis] = {}
        self._connected = False

    # -- lifecycle ----------------------------------------------------------

    def connect(self) -> None:
        with self._io_lock:
            if self._connected:
                return
            self._connect_raw()
            self._axes = self._build_axes()
            self._connected = True

    def close(self) -> None:
        with self._io_lock:
            if not self._connected:
                return
            try:
                self._close_raw()
            finally:
                self._axes = {}
                self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # -- axis access ---------------------------------------------------------

    def axes(self) -> dict[str, MotionAxis]:
        if not self._connected:
            self.connect()
        return dict(self._axes)

    def open_axes(self) -> dict[str, MotionAxis]:
        """The axes while connected, ``{}`` otherwise — never connects (for readers
        that must not open a device, e.g. the manual panel's live readout)."""
        with self._io_lock:
            return dict(self._axes) if self._connected else {}

    def axis(self, name: str) -> MotionAxis:
        if not self._connected:
            self.connect()
        try:
            return self._axes[name]
        except KeyError:
            raise InstrumentError(
                f"{type(self).__name__}: no axis {name!r}; "
                f"configured axes: {sorted(self._axes)}"
            ) from None

    def stop_all(self) -> None:
        """Panic-stop every axis; best-effort, collects nothing."""
        for ax in list(self._axes.values()):
            try:
                ax.stop()
            except Exception:  # noqa: BLE001 - panic path must not raise
                pass

    # -- driver primitives ----------------------------------------------------

    @abstractmethod
    def _connect_raw(self) -> None:
        """Open the physical link. Raise InstrumentUnavailable when absent."""

    @abstractmethod
    def _close_raw(self) -> None: ...

    @abstractmethod
    def _build_axes(self) -> dict[str, MotionAxis]:
        """Instantiate MotionAxis objects for the configured channels."""

    # context-manager sugar
    def __enter__(self) -> "MotionController":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
