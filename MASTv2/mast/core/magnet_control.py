"""Process-level port to the superconducting-magnet field controller (7.2.0).

Skills and API routes reach the controller through here, so ``mast/skills/**`` never
imports the live app. The shape matches ``core/cryostat.py``. The runtime installs
the source (``CoreRuntime.magnet_controller``): it returns the
:class:`mast.instruments.magnet_625.Magnet625Controller` for the one Model 625 the
environment monitor reads, and ``None`` when there is none or more than one.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_source: "Callable[[], Any] | None" = None
_state_source: "Callable[[], dict] | None" = None


def set_source(source: "Callable[[], Any] | None") -> None:
    """Install ``() -> controller | None`` (the runtime does this at start-up)."""
    global _source
    with _lock:
        _source = source


def set_state_source(source: "Callable[[], dict] | None") -> None:
    """Install ``() -> {"ok", "present", "detail"}``: is the field at rest? (the runtime
    does this at start-up; the heater's plan asks through :func:`field_state`)."""
    global _state_source
    with _lock:
        _state_source = source


def field_state() -> dict:
    """Is the Model 625's field at rest, for the heater's gate? ``ok`` when there is no 625
    on this machine, or the one 625 is not ramping, has no error bit and answered. Not wired
    (no runtime in this process), or any failure: ``ok`` False (unknown is not at rest).
    Never raises; may read the 625's cached state, so call it with no other link held."""
    src = _state_source
    if src is None:
        return {"ok": False, "present": None,
                "detail": "the magnet field state is not wired into this process: unknown is "
                          "not at rest, so no heating"}
    try:
        res = src()
    except Exception as exc:  # noqa: BLE001 - unknown is not an answer
        return {"ok": False, "present": None,
                "detail": f"reading the magnet field state failed: {type(exc).__name__}: {exc}"}
    return res if isinstance(res, dict) else {"ok": False, "present": None,
                                              "detail": "the magnet field state is unreadable"}


def controller() -> Any:
    """The field controller for this machine's Model 625, or ``None``. Never raises.

    ``None`` covers: no magnet supply recognised, more than one, and MAST's own side
    not wired (no runtime in this process). :func:`present` tells the last case apart.
    """
    src = _source
    if src is None:
        return None
    try:
        return src()
    except Exception as exc:  # noqa: BLE001 - asking must never break the caller
        logger.debug("magnet controller source failed: %r", exc)
        return None


def present() -> "bool | None":
    """Is a Model 625 recognised here? ``None`` = cannot tell (not wired)."""
    src = _source
    if src is None:
        return None
    try:
        return src() is not None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["controller", "field_state", "present", "set_source", "set_state_source"]
