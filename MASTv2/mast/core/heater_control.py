"""Process-level port to the temperature controller (Lake Shore 335 heater control).

Skills and API routes reach the controller through here, so ``mast/skills/**`` never
imports the live app. The shape matches ``core/magnet_control.py``. The runtime installs
the source (``CoreRuntime.heater_controller``): it returns the
:class:`mast.instruments.heater_335.Heater335Controller` for the one Model 335 the
environment monitor reads, and ``None`` when there is none or more than one.

The field-control gate (``instruments/magnet_625``) asks here whether a heater is on:
heating and field changes never happen at the same time (a design decision). :func:`interlock` may do I/O (two ``RANGE?`` queries on the 335's own link);
:func:`mast_heating` reads memory only, so it may be asked while another instrument's
link is held.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from mast.i18n import tr

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_source: "Callable[[], Any] | None" = None
_interlock_source: "Callable[[], dict] | None" = None


def set_source(source: "Callable[[], Any] | None") -> None:
    """Install ``() -> controller | None`` (the runtime does this at start-up)."""
    global _source
    with _lock:
        _source = source


def set_interlock_source(source: "Callable[[], dict] | None") -> None:
    """Install ``() -> verdict`` for :func:`interlock` (the runtime's ``heater_interlock``)."""
    global _interlock_source
    with _lock:
        _interlock_source = source


def interlock() -> dict:
    """May the magnet's field change, as far as heating goes? Never raises.

    ``{"ok": bool, "present": bool | None, "detail": str, ...}``. ``ok`` with no Model 335
    here ("no temperature controller"), or with the one 335 reading RANGE 0 on both
    outputs and MAST's own heating off. Not ``ok`` when a heater is on, MAST is heating, a
    RANGE? is not answered, there is more than one 335, an input of unknown model might be
    a 335, or this process has no runtime to ask (unknown is not an answer).
    """
    src = _interlock_source
    if src is None:
        return {"ok": False, "present": None, "detail": tr(
            "这个进程里没有接上温控（没有 MAST 运行时）：不知道 335 的加热器开没开，不改磁场",
            "temperature control is not wired in this process (no MAST runtime): whether a 335 "
            "heater is on is unknown, so the field does not change")}
    try:
        v = src()
    except Exception as exc:  # noqa: BLE001 - unknown is not an answer
        return {"ok": False, "present": None, "detail": tr(
            f"读加热器状态失败：{type(exc).__name__}: {exc}",
            f"reading the heater state failed: {type(exc).__name__}: {exc}")}
    if not isinstance(v, dict):
        return {"ok": False, "present": None, "detail": tr(f"加热器状态不可读：{v!r}",
                                                           f"heater state unreadable: {v!r}")}
    return dict(v, ok=bool(v.get("ok")))


def mast_heating() -> bool:
    """MAST's own heating is on: a change, the watch, an off retry, or a zone table /
    control mode write. Memory only (no I/O, no instrument lock), so the field control may
    ask it while holding the magnet supply's link. Never raises; a controller that cannot
    say counts as heating."""
    ctl = controller()
    if ctl is None:
        return False
    try:
        return bool(ctl.mast_heating())
    except Exception:  # noqa: BLE001 - cannot tell: count it as heating
        return True


def controller(*, learn: bool = False) -> Any:
    """The heater controller for this machine's Model 335, or ``None``. Never raises.

    ``None`` covers: no Model 335 recognised, more than one, and MAST's own side not
    wired (no runtime in this process). :func:`present` tells the last case apart.
    ``learn``: the caller may wait for I/O (a skill, the heater-off route); when inputs
    of unknown model stand between it and a controller, ask them ``*IDN?`` first.
    """
    src = _source
    if src is None:
        return None
    try:
        if learn:
            try:
                return src(learn=True)
            except TypeError:            # a source that takes no arguments
                pass
        return src()
    except Exception as exc:  # noqa: BLE001 - asking must never break the caller
        logger.debug("heater controller source failed: %r", exc)
        return None


def present() -> "bool | None":
    """Is a Model 335 recognised here? ``None`` = cannot tell (not wired)."""
    src = _source
    if src is None:
        return None
    try:
        return src() is not None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["controller", "interlock", "mast_heating", "present", "set_interlock_source",
           "set_source"]
