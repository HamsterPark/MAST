"""Keep the asyncio event loop off the log sink, and keep a reset-connection flood out of the log.

Two independent problems, seen in long runs on a machine with a slow spinning disk:

1. **Blocking log writes on the event-loop thread.** The service's handlers write
   to a pipe/file synchronously, inside ``logger.error(...)``, on whichever thread
   logs. On the loop thread that is the whole API: a stalled sink is a stalled
   server (an overnight freeze was traced to this). :func:`install_log_queue` swaps each
   logger's handlers for a non-blocking :class:`logging.handlers.QueueHandler`; a
   :class:`logging.handlers.QueueListener` thread owns the real handlers. Format,
   levels and the files written are unchanged -- the handlers are the same
   objects, they just run on another thread. :func:`flush_log_queue` (also
   registered with ``atexit``) drains the queue and puts the handlers back.
   Records from ALL threads are queued (stable order; worker threads no longer block
   on a saturated disk). Full queue: the loop thread drops at once and counts; other
   threads wait up to 1 s first.

   **Trade-off:** on a hard crash (a native crash, or ``os._exit`` that does not go
   through ``flush_log_queue``) the records still queued are lost, where before they
   were already written. Normally that window is tiny: the listener writes as fast as
   records arrive, so the depth is ~0-few records (estimate: the service logs well
   under 1 line/s without the reset flood); it only grows while a sink is stalled,
   up to 50,000 records.

2. **A traceback per reset connection.** On Windows' proactor loop a peer that
   resets a TLS connection makes ``_ProactorBasePipeTransport._call_connection_lost``
   raise ``ConnectionResetError`` out of ``sock.shutdown()``; asyncio's default
   handler logs a full traceback each time (thousands in a day, most of all log lines).
   :class:`ClosedClientNoise` recognises exactly that case, counts it, and logs one
   line at most once per interval. Every other exception reaches the previous
   handler untouched.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import logging.handlers
import queue
import threading
import time
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# A stalled sink must not turn into unbounded memory. On a full queue the event-loop
# thread drops at once (counted, never blocked on); any other thread waits up to
# _WORKER_WAIT_S for the listener to drain before dropping, so a stalled disk does not
# silently eat the WARNING/ERROR lines of the instrument threads.
_QUEUE_MAX = 50_000
_WORKER_WAIT_S = 1.0
_FLUSH_TIMEOUT_S = 5.0
# Root plus the loggers uvicorn gives their own handlers (propagate=False).
_DEFAULT_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


def _on_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _NonBlockingQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that never blocks and never flattens the record.

    ``QueueHandler.prepare`` formats the message and clears ``args``/``exc_info``;
    uvicorn's access formatter reads ``record.args`` to build its line, so the
    record must reach the real handlers as it was emitted.
    """

    def __init__(self, q: "queue.Queue") -> None:
        super().__init__(q)
        self.dropped = 0

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            if _on_event_loop_thread():
                self.queue.put_nowait(record)
            else:
                self.queue.put(record, timeout=_WORKER_WAIT_S)
        except queue.Full:
            self.dropped += 1
            return
        if self.dropped:
            n, self.dropped = self.dropped, 0
            note = logging.LogRecord(
                __name__, logging.WARNING, __file__, 0,
                "log queue was full: dropped %d records (a log sink stalled)",
                (n,), None)
            try:
                self.queue.put_nowait(note)
            except queue.Full:
                self.dropped += n


class _Installed:
    def __init__(self, lg: logging.Logger, originals: list, handler, listener, q) -> None:
        self.lg, self.originals, self.handler = lg, originals, handler
        self.listener, self.q = listener, q


_installed: list[_Installed] = []
_state_lock = threading.Lock()
_atexit_registered = False


def install_log_queue(logger_names: Iterable[str] = _DEFAULT_LOGGERS) -> int:
    """Move the real handlers of each named logger behind a queue. Returns how many
    loggers were converted. Idempotent; loggers without handlers are left alone."""
    global _atexit_registered
    n = 0
    with _state_lock:
        for name in logger_names:
            lg = logging.getLogger(name)
            if not lg.handlers or any(isinstance(h, _NonBlockingQueueHandler)
                                      for h in lg.handlers):
                continue
            originals = list(lg.handlers)
            q: "queue.Queue" = queue.Queue(_QUEUE_MAX)
            listener = logging.handlers.QueueListener(
                q, *originals, respect_handler_level=True)
            handler = _NonBlockingQueueHandler(q)
            listener.start()
            lg.handlers = [handler]
            _installed.append(_Installed(lg, originals, handler, listener, q))
            n += 1
        if n and not _atexit_registered:
            atexit.register(flush_log_queue)
            _atexit_registered = True
    return n


def flush_log_queue(timeout: float = _FLUSH_TIMEOUT_S) -> None:
    """Write out everything queued, then give the loggers their own handlers back.

    Safe to call more than once, and from a thread that is about to ``os._exit``.
    A sink that is itself stuck costs at most ``timeout`` seconds, not a hang.
    """
    with _state_lock:
        items = list(_installed)
        _installed.clear()
    deadline = time.monotonic() + max(0.0, float(timeout))
    for it in items:
        thread = getattr(it.listener, "_thread", None)
        try:
            it.q.put(None, timeout=max(0.0, deadline - time.monotonic()))   # sentinel
        except queue.Full:
            pass
        if thread is not None:
            thread.join(max(0.0, deadline - time.monotonic()))
        # Put the real handlers back before draining, so nothing new queues up behind us.
        it.lg.handlers = [h for h in it.lg.handlers if h is not it.handler] + it.originals
        if thread is None or not thread.is_alive():
            while True:             # records that raced in after the sentinel
                try:
                    rec = it.q.get_nowait()
                except queue.Empty:
                    break
                if rec is None:
                    continue
                for h in it.originals:
                    if rec.levelno >= h.level:
                        try:
                            h.handle(rec)
                        except Exception:  # noqa: BLE001 — flushing must not raise
                            pass


def _is_closed_client(context: dict) -> bool:
    exc = context.get("exception")
    if not isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return False
    where = f"{context.get('message', '')} {context.get('handle', '')}"
    return "connection_lost" in where


class ClosedClientNoise:
    """asyncio loop exception handler: summarise reset-while-closing, pass the rest on."""

    def __init__(self, previous: Optional[Callable] = None, *,
                 interval_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 log: Optional[logging.Logger] = None) -> None:
        self._previous = previous
        self._interval = float(interval_s)
        self._clock = clock
        self._log = log or logger
        self._last: Optional[float] = None
        self._pending = 0
        self.total = 0

    def __call__(self, loop, context: dict) -> None:
        if not _is_closed_client(context):
            if self._previous is not None:
                self._previous(loop, context)
            else:
                loop.default_exception_handler(context)
            return
        self.total += 1
        self._pending += 1
        now = self._clock()
        if self._last is None or now - self._last >= self._interval:
            first = self._last is None
            n, self._pending, self._last = self._pending, 0, now
            self._log.warning(
                "asyncio: a client reset the connection while it was being closed "
                "(%s; reported at most once per %.0f s, counted otherwise)",
                "first occurrence" if first else f"{n} since the last report",
                self._interval)


def install_loop_exception_handler(loop, **kw) -> ClosedClientNoise:
    handler = ClosedClientNoise(loop.get_exception_handler(), **kw)
    loop.set_exception_handler(handler)
    return handler


def guard_server(server) -> None:
    """Install :class:`ClosedClientNoise` on the loop a ``uvicorn.Server`` runs in.

    ``Server.run`` creates its loop inside ``asyncio.run``, so the handler has to
    be set from inside the running loop: wrap the instance's ``serve`` coroutine.
    """
    import asyncio

    original = server.serve

    async def serve(*args, **kwargs):
        install_loop_exception_handler(asyncio.get_running_loop())
        return await original(*args, **kwargs)

    server.serve = serve
