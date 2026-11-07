"""Stops that someone else asked for: a counter a running protocol can watch.

A person or another agent presses
``StopWITec`` / ``CloseWITecShutter`` / ``LaserEmissionOff`` / ``StopTemperatureRamp`` (or the
operator withdraws ``laser_area_safe``, or the E-STOP fires) while a measurement protocol runs.
Those stops act once; at its next step the protocol would open the shutter again, switch the
source back on or send the next temperature setpoint, undoing what was asked for. Reading the
device state back cannot tell "stopped by someone" from "stopped by me", and a TC100 without a
status register cannot be read at all.

So every station device (microscope, light source, temperature controller) counts the stops
requested from outside a run. The stop skills, ``emission_safety.safe_off_all`` (withdrawal of
the attestation, E-STOP) call :meth:`ExternalStopMixin.note_external_stop`; a protocol records
:attr:`ExternalStopMixin.external_stop_epoch` of its devices when it starts and stops cleanly
(never reopening, never re-sending) as soon as one changes. The protocol's own safe-direction
calls (closing the shutter after an exposure, its cleanup) do not count.

The counter lives on the driver object, i.e. for the life of the process; nothing persists.
Attributes are read with defaults so a subclass that skips ``__init__`` (test doubles) still
works.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

__all__ = ["ExternalStopMixin"]

_LOCK = threading.Lock()   # one tiny lock for all devices: increments are rare and instant


class ExternalStopMixin:
    """Adds :meth:`note_external_stop` and :attr:`external_stop_epoch` to a device class."""

    def note_external_stop(self, reason: str = "") -> int:
        """Record that a stop was asked for from outside the running protocol. Returns the new
        epoch. Never raises."""
        try:
            with _LOCK:
                n = int(getattr(self, "_external_stop_epoch", 0) or 0) + 1
                self._external_stop_epoch = n
                self._external_stop_reason = str(reason or "")[:200]
            logger.info("%s: external stop #%d (%s)", getattr(self, "device_id", "?"), n,
                        self._external_stop_reason or "-")
            return n
        except Exception:  # noqa: BLE001 - bookkeeping must never break a stop
            logger.debug("note_external_stop failed", exc_info=True)
            return int(getattr(self, "_external_stop_epoch", 0) or 0)

    @property
    def external_stop_epoch(self) -> int:
        """How many stops were asked for from outside a run since this driver was built."""
        return int(getattr(self, "_external_stop_epoch", 0) or 0)

    @property
    def external_stop_reason(self) -> str:
        """Why the last one was asked for ("" if none)."""
        return str(getattr(self, "_external_stop_reason", "") or "")
