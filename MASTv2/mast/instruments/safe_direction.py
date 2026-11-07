"""Bounded waiting for the safe direction (emission off, heater stop).

An action that makes things safer must not queue forever behind a lock that some other operation
holds - a switch-on still waiting for the laser to come up, a transaction stuck in a serial driver
that never returns. Every device keeps two kinds of lock: the device lock (one operation at a time)
and the port lock (one transaction at a time on the line); the off path needs both.

Inside ``with safe_direction():`` every :func:`held` wait ends at one shared deadline
(:data:`SAFE_LOCK_TIMEOUT_S` after the block was entered, so a stuck port costs the whole operation
that long, not that long per step) and then raises :class:`LockTimeout` instead of waiting on. The
caller of the safe operation turns that into "not confirmed": it logs, returns the *unknown* value
(``None`` / ``False``) and never raises. Outside such a block :func:`held` is a plain ``with lock:``,
so normal operations behave exactly as before.

A lock that is free is always taken, even when the deadline has passed: the deadline limits waiting,
it does not forbid working.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from typing import Any, Iterator

from mast.i18n import tr
from mast.instruments.base import InstrumentUnavailable

logger = logging.getLogger(__name__)

__all__ = ["SAFE_LOCK_TIMEOUT_S", "LockTimeout", "safe_direction", "held", "in_safe_direction"]

#: seconds a safe-direction operation waits, in total, for the locks it needs
SAFE_LOCK_TIMEOUT_S = 5.0


class LockTimeout(InstrumentUnavailable):
    """A safe-direction operation could not get a device or port lock in time. The lock is held by
    another operation that has not finished; this operation was NOT carried out (or not completed)."""


_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("mast_safe_direction_deadline", default=None)


@contextlib.contextmanager
def safe_direction(budget_s: float | None = None) -> Iterator[None]:
    """Bound every :func:`held` wait inside the block by ``budget_s`` seconds from now (default
    :data:`SAFE_LOCK_TIMEOUT_S`, read when the block is entered). Nested blocks keep the earlier deadline."""
    budget = SAFE_LOCK_TIMEOUT_S if budget_s is None else float(budget_s)
    new = time.monotonic() + max(0.0, budget)
    outer = _deadline.get()
    token = _deadline.set(new if outer is None else min(outer, new))
    try:
        yield
    finally:
        _deadline.reset(token)


def in_safe_direction() -> bool:
    """Whether the current thread is inside a :func:`safe_direction` block."""
    return _deadline.get() is not None


@contextlib.contextmanager
def held(lock: Any, what: str) -> Iterator[None]:
    """``with lock:``; inside :func:`safe_direction` the wait ends at the block's deadline and raises
    :class:`LockTimeout` (logged), naming ``what`` so the message says which lock was busy."""
    deadline = _deadline.get()
    if deadline is None:
        with lock:
            yield
        return
    started = time.monotonic()
    if not lock.acquire(timeout=max(0.0, deadline - started)):
        waited = time.monotonic() - started
        logger.warning("%s: still held by another operation after %.1f s; the safe-direction step that needed "
                       "it was NOT carried out", what, waited)
        raise LockTimeout(tr(f"{what} 被另一个操作占着（等了 {waited:.1f} s）——这一步没有执行",
                             f"{what} is held by another operation (waited {waited:.1f} s) - not carried out"))
    try:
        yield
    finally:
        lock.release()
