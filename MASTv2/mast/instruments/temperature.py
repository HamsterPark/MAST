"""TemperatureController — sample heating/cooling stage controller (TC100 or a sim).

Design contract: ``docs/v2/design/optical_station_witec.md`` §6.1.

``set_setpoint`` is refused outside :class:`TemperatureLimits` (never clamped) and
needs ``write_enabled`` (real drivers default false until the bench self-test).
``stop`` is the safe direction: always attempted, never raises, and never waits forever for a busy device
(the device lock and the port are waited for at most ``SAFE_LOCK_TIMEOUT_S`` in total); it answers whether the
stop command went out.

A setpoint is checked twice and a stop cancels it. ``write_enabled`` is checked again once the setpoint holds the
controller (the first check only refuses early, so nothing queues for nothing), and every ``stop()`` counts one "stop
generation" (:attr:`TemperatureController.stop_epoch`, never reset, counted on entry even if the stop cannot get the
controller): the setpoint reads it before it can wait, compares it when it gets the controller - and, on the TC100,
again right before the run command - and is refused with :class:`TemperaturePreempted`, nothing written, if a stop was
asked for since. A stop asked for while a setpoint was queued used to run BEFORE it, and the heater came back on.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from mast.i18n import tr
from mast.instruments.base import InstrumentError
from mast.instruments.external_stop import ExternalStopMixin
from mast.instruments.safe_direction import LockTimeout, held, safe_direction

logger = logging.getLogger(__name__)

_EPOCH_LOCK = threading.Lock()   # the stop generation changes rarely and instantly: one tiny lock for every controller

__all__ = ["TemperatureLimitError", "TemperatureWriteDisabled", "TemperaturePreempted", "TemperatureLimits",
           "TemperatureController", "SimTemperatureController"]

#: controller states (identity strings — never translated)
STATES = ("running", "stopped", "holding", "unknown")


class TemperatureLimitError(InstrumentError):
    """A requested setpoint or ramp lies outside the configured limits."""


class TemperatureWriteDisabled(InstrumentError):
    """Writes are disabled for this controller in the device config."""


class TemperaturePreempted(InstrumentError):
    """A setpoint was cancelled because a stop was asked for after it started (while it waited for the controller, or
    while its registers were being written). The stop wins."""


@dataclass(frozen=True)
class TemperatureLimits:
    min_c: float | None = None
    max_c: float | None = None
    max_ramp_c_per_min: float | None = None

    @classmethod
    def from_dict(cls, raw: dict | None) -> "TemperatureLimits":
        raw = dict(raw or {})
        kw = {k: float(raw[k]) for k in ("min_c", "max_c", "max_ramp_c_per_min")
              if raw.get(k) is not None}
        lim = cls(**kw)
        if lim.min_c is not None and lim.max_c is not None and not lim.min_c < lim.max_c:
            raise ValueError("temperature limits: need min_c < max_c")
        return lim

    def as_dict(self) -> dict:
        return {"min_c": self.min_c, "max_c": self.max_c,
                "max_ramp_c_per_min": self.max_ramp_c_per_min}


class TemperatureController(ExternalStopMixin, ABC):
    """Abstract temperature controller. Drivers implement ``_*_raw``."""

    def __init__(self, *, device_id: str = "", name: str = "",
                 limits: TemperatureLimits | None = None, write_enabled: bool = True):
        self.device_id = device_id
        self.name = name or device_id
        self.limits = limits or TemperatureLimits()
        self.write_enabled = bool(write_enabled)
        #: what the last ``stop()`` did: ``{}`` = the command went out; otherwise ``error`` says why not (and
        #: ``lock_timeout`` is true when the device stayed busy) - for the skill's report
        self.last_stop_report: dict[str, Any] = {}
        #: the stop generation (see :attr:`stop_epoch`) and, per thread, the one the running setpoint started under
        self._stop_epoch = 0
        self._op = threading.local()
        self._lock = threading.RLock()

    # -- the second check and the stop generation -------------------------------------------------------------------

    @property
    def stop_epoch(self) -> int:
        """The stop generation: how many ``stop()`` calls were asked for since this driver was built. Never reset. A
        setpoint reads it when it starts - before it can wait for the controller - and is cancelled
        (:class:`TemperaturePreempted`) if it has moved by the time the setpoint gets the controller: a stop asked for
        after the setpoint started wins, whoever asked and whenever the lock came free. (``external_stop_epoch`` counts
        only the stops that came from outside a run, for a protocol to notice; this one counts every stop.)"""
        return int(getattr(self, "_stop_epoch", 0) or 0)

    def _bump_stop_epoch(self) -> int:
        with _EPOCH_LOCK:
            n = int(getattr(self, "_stop_epoch", 0) or 0) + 1
            self._stop_epoch = n
            return n

    def _op_state(self) -> threading.local:
        st = getattr(self, "_op", None)
        if st is None:                       # a subclass that skipped __init__ (test double)
            with _EPOCH_LOCK:
                st = getattr(self, "_op", None)
                if st is None:
                    st = self._op = threading.local()
        return st

    def _check_not_stopped(self, what: str) -> None:
        """Inside a setpoint that holds the controller: raise :class:`TemperaturePreempted` if a stop was asked for since
        it started. Outside one (a read, a stop) it does nothing."""
        entry = getattr(self._op_state(), "epoch", None)
        if entry is not None and entry != self.stop_epoch:
            raise TemperaturePreempted(tr(f"{self.device_id}: {what}被停止指令打断，没有执行",
                                          f"{self.device_id}: {what} was pre-empted by a stop and not carried out"))

    @contextlib.contextmanager
    def _setpoint_scope(self, epoch: int):
        """Hold the controller for a setpoint that started under the stop generation ``epoch`` (read before the lock was
        waited for)."""
        st = self._op_state()
        with self._lock:
            st.epoch = int(epoch)
            try:
                yield
            finally:
                st.epoch = None

    def _require_write(self, what: str) -> None:
        if not self.write_enabled:
            raise TemperatureWriteDisabled(
                f"{self.device_id}: {what} refused — write_enabled is false in the device config")

    def status(self) -> dict:
        """``{pv_c, sv_c, output_pct, state, ...}``."""
        st = dict(self._status_raw() or {})
        st.setdefault("device_id", self.device_id)
        st.setdefault("write_enabled", self.write_enabled)
        return st

    def read_c(self) -> float | None:
        v = self.status().get("pv_c")
        return None if v is None else float(v)

    def set_setpoint(self, c: Any, *, ramp_c_per_min: Any = None) -> dict:
        epoch = self.stop_epoch              # first, before anything can make this wait for the controller
        try:
            v = float(c)
        except (TypeError, ValueError) as exc:
            raise TemperatureLimitError(f"setpoint must be a number, got {c!r}") from exc
        if not math.isfinite(v):
            raise TemperatureLimitError("setpoint must be finite")
        lo, hi = self.limits.min_c, self.limits.max_c
        if lo is None or hi is None:
            raise TemperatureLimitError(f"{self.device_id}: no temperature range configured — refused")
        if not lo <= v <= hi:
            raise TemperatureLimitError(f"{self.device_id}: setpoint {v} °C outside [{lo}, {hi}] °C")
        ramp = None
        if ramp_c_per_min is not None:
            ramp = float(ramp_c_per_min)
            cap = self.limits.max_ramp_c_per_min
            if not (math.isfinite(ramp) and ramp > 0):
                raise TemperatureLimitError("ramp must be a positive number")
            if cap is not None and ramp > cap:
                raise TemperatureLimitError(f"{self.device_id}: ramp {ramp} °C/min exceeds {cap}")
        elif self.limits.max_ramp_c_per_min is not None:
            ramp = float(self.limits.max_ramp_c_per_min)
        self._require_write("setpoint")      # early: refuse before queueing behind another operation
        with self._setpoint_scope(epoch):
            self._require_write("setpoint")  # again, now that this holds the controller
            self._check_not_stopped("setpoint")
            return dict(self._set_setpoint_raw(v, ramp))

    def stop(self) -> bool:
        """The safe direction: always attempted, never raises. ``True`` = the stop command went out; ``False`` =
        NOT confirmed (the device was busy for longer than ``SAFE_LOCK_TIMEOUT_S``, or the driver could not do it;
        the log says which; :attr:`last_stop_report` has the reason). Read the state back to know more. Asking for it
        counts one stop generation FIRST, so a setpoint that started earlier and is still waiting for the controller is
        cancelled even if this stop cannot get the controller itself."""
        self._bump_stop_epoch()
        self.last_stop_report = {}
        try:
            with safe_direction():
                with held(self._lock, f"{self.device_id}: device lock"):
                    self._stop_raw()
            return True
        except LockTimeout as exc:
            logger.warning("%s: stop NOT confirmed - %s", self.device_id, exc)
            self.last_stop_report = {"lock_timeout": True, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - safe direction must not throw
            logger.warning("%s: stop NOT confirmed - %s: %s", self.device_id, type(exc).__name__, exc)
            self.last_stop_report = {"error": str(exc)}
        return False

    def close(self) -> None:  # pragma: no cover - drivers override
        pass

    @abstractmethod
    def _status_raw(self) -> dict: ...
    @abstractmethod
    def _set_setpoint_raw(self, c: float, ramp_c_per_min: float | None) -> dict: ...
    @abstractmethod
    def _stop_raw(self) -> None: ...


class SimTemperatureController(TemperatureController):
    """First-order stage model: the setpoint path follows the ramp, PV lags it by ``tau_s``.

    ``time_scale`` multiplies simulated time (e.g. 60 = one simulated minute per second).
    """

    def __init__(self, *, start_c: float = 22.0, tau_s: float = 20.0,
                 time_scale: float = 1.0, **kw):
        kw.setdefault("limits", TemperatureLimits(min_c=-196.0, max_c=400.0, max_ramp_c_per_min=20.0))
        super().__init__(**kw)
        self.pv = float(start_c)
        self.sv = float(start_c)
        self.target = float(start_c)
        self.ramp = None
        self.tau = float(tau_s)
        self.scale = float(time_scale)
        self.state = "stopped"
        self._t = time.monotonic()

    def _advance(self) -> None:
        now = time.monotonic()
        dt = (now - self._t) * self.scale
        self._t = now
        if self.state != "running" or dt <= 0:
            return
        if self.ramp:
            step = self.ramp / 60.0 * dt
            if abs(self.target - self.sv) <= step:
                self.sv = self.target
            else:
                self.sv += step if self.target > self.sv else -step
        else:
            self.sv = self.target
        self.pv += (self.sv - self.pv) * (1.0 - math.exp(-dt / max(self.tau, 1e-6)))

    def _status_raw(self) -> dict:
        self._advance()
        return {"sim": True, "pv_c": round(self.pv, 3), "sv_c": round(self.sv, 3),
                "target_c": self.target, "output_pct": 0.0, "state": self.state,
                "ramp_c_per_min": self.ramp}

    def _set_setpoint_raw(self, c: float, ramp_c_per_min: float | None) -> dict:
        self._advance()
        self.target, self.ramp, self.state = float(c), ramp_c_per_min, "running"
        return {"target_c": self.target, "ramp_c_per_min": self.ramp}

    def _stop_raw(self) -> None:
        self._advance()
        self.state = "stopped"
