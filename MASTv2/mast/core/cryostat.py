"""Cryostat instruments — public READ-ONLY port for skills (magnet supply, level meter).

Same shape as :mod:`mast.core.temperature`: ``CoreRuntime`` (which owns the
EnvironmentMonitor and its sensor objects) injects two callables at boot, and the
skill layer reads through here, so ``mast/skills/**`` never imports the live app
or ``mast.environment``.

A source returns a list of per-sensor dicts (``sensor``, ``value``, ``unit``,
``status``, ``timestamp`` and, once per instrument, ``instrument`` — the driver's
read-only snapshot). Three distinct answers, kept apart on purpose:

* ``None``  — no source wired (standalone API process, monitor not built):
  says nothing about the instrument;
* ``[]``    — wired, and this machine has no such instrument connected;
* ``[...]`` — the readings.

Nothing here can change the magnet or start a level measurement: the drivers
behind the sources have no setter (``environment/lakeshore_625.py``,
``environment/cryomag_lm510.py``).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_magnet_source: "Callable[[], list[dict]] | None" = None
_helium_source: "Callable[[], list[dict]] | None" = None


def set_magnet_source(fn: "Callable[[], list[dict]] | None") -> None:
    global _magnet_source
    with _lock:
        _magnet_source = fn


def set_helium_source(fn: "Callable[[], list[dict]] | None") -> None:
    global _helium_source
    with _lock:
        _helium_source = fn


def _call(fn) -> "list[dict] | None":
    if fn is None:
        return None
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001 - a read port never raises
        logger.debug("cryostat source failed: %s", exc)
        return None
    return list(out) if out is not None else None


def magnet_states() -> "list[dict] | None":
    with _lock:
        fn = _magnet_source
    return _call(fn)


def helium_states() -> "list[dict] | None":
    with _lock:
        fn = _helium_source
    return _call(fn)


__all__ = ["set_magnet_source", "set_helium_source", "magnet_states", "helium_states"]
