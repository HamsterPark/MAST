"""Log sink off the event-loop thread; reset-while-closing is one line, not a traceback each.

A stalled log sink can freeze the API (the loop thread is writing a record),
and a flood of identical asyncio tracebacks can dominate the log.
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (see tests/v2/conftest.py) ──
import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import asyncio
import itertools
import logging
import threading
import time

import pytest

from mast.core import log_hygiene as lh

_ids = itertools.count()


class Sink(logging.Handler):
    """A handler that can be told to stall, and remembers where it was called."""

    def __init__(self, fmt="[%(levelname)s] %(message)s"):
        super().__init__()
        self.setFormatter(logging.Formatter(fmt))
        self.gate = threading.Event()
        self.gate.set()
        self.lines: list[str] = []
        self.threads: set[str] = set()

    def emit(self, record):
        self.gate.wait(10)
        self.threads.add(threading.current_thread().name)
        self.lines.append(self.format(record))


@pytest.fixture()
def lg():
    name = f"t.lh{next(_ids)}"
    logger = logging.getLogger(name)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    sink = Sink()
    logger.addHandler(sink)
    yield logger, sink, name
    lh.flush_log_queue(1.0)
    logger.handlers.clear()


def _in_thread(fn, timeout=3.0):
    th = threading.Thread(target=fn, daemon=True)
    t0 = time.monotonic()
    th.start()
    th.join(timeout)
    return (not th.is_alive()), time.monotonic() - t0


def test_a_stalled_sink_does_not_block_the_thread_that_logs(lg):
    logger, sink, name = lg
    assert lh.install_log_queue([name]) == 1
    sink.gate.clear()                                  # the pipe/disk is stuck

    done, _ = _in_thread(lambda: [logger.info("rec %d", i) for i in range(20)])
    assert done, "logging blocked on a stalled sink"
    assert sink.lines == []

    sink.gate.set()
    lh.flush_log_queue(3.0)
    assert sink.lines == [f"[INFO] rec {i}" for i in range(20)], "lost or reordered records"
    assert sink.threads and threading.current_thread().name not in sink.threads
    assert threading.main_thread().name not in sink.threads


def test_format_args_and_tracebacks_are_unchanged(lg):
    logger, sink, name = lg
    direct_logger = logging.getLogger(name + ".direct")
    direct_logger.propagate = False
    direct = Sink()
    direct_logger.addHandler(direct)

    def emit(target):
        try:
            raise ValueError("boom")
        except ValueError:
            target.error("failed %s/%d", "x", 7, exc_info=True)

    emit(direct_logger)
    lh.install_log_queue([name])
    emit(logger)
    lh.flush_log_queue(3.0)

    assert sink.lines == direct.lines
    assert "Traceback" in sink.lines[0] and "failed x/7" in sink.lines[0]


def test_the_record_reaches_the_handler_with_its_args(lg):
    """uvicorn's access formatter unpacks record.args; a flattened record breaks it."""
    logger, sink, name = lg

    class ArgsFormatter(logging.Formatter):
        def format(self, record):
            addr, method, path = record.args
            return f"{addr} {method} {path}"

    sink.setFormatter(ArgsFormatter())
    lh.install_log_queue([name])
    logger.info('%s - "%s %s"', "192.0.2.1", "GET", "/api/x")
    lh.flush_log_queue(3.0)
    assert sink.lines == ["192.0.2.1 GET /api/x"]


def test_flush_restores_the_handlers_and_is_repeatable(lg):
    logger, sink, name = lg
    lh.install_log_queue([name])
    assert lh.install_log_queue([name]) == 0               # idempotent
    assert logger.handlers != [sink]
    lh.flush_log_queue(3.0)
    lh.flush_log_queue(3.0)
    assert logger.handlers == [sink]
    logger.info("after")                                   # synchronous again
    assert sink.lines == ["[INFO] after"]
    assert threading.current_thread().name in sink.threads


def test_flush_with_a_stuck_sink_returns_within_its_timeout(lg):
    logger, sink, name = lg
    lh.install_log_queue([name])
    sink.gate.clear()
    logger.info("stuck")
    done, took = _in_thread(lambda: lh.flush_log_queue(0.3))
    assert done and took < 2.0
    sink.gate.set()


def test_a_full_queue_drops_and_says_so_without_blocking(lg, monkeypatch):
    logger, sink, name = lg
    monkeypatch.setattr(lh, "_QUEUE_MAX", 5)
    lh.install_log_queue([name])
    sink.gate.clear()
    async def burst():                                  # the loop thread drops at once
        for i in range(60):
            logger.info("rec %d", i)

    done, _ = _in_thread(lambda: asyncio.run(burst()))
    assert done, "logging blocked on a full queue"
    sink.gate.set()
    q = lh._installed[-1].q                              # noqa: SLF001
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not q.empty():
        time.sleep(0.01)
    logger.info("after the stall")
    lh.flush_log_queue(3.0)
    assert len(sink.lines) < 60
    assert any("dropped" in ln for ln in sink.lines)
    assert any(ln.endswith("after the stall") for ln in sink.lines)


# ── the asyncio flood ────────────────────────────────────────────────────────

class _Transport:
    def _call_connection_lost(self, exc):
        raise ConnectionResetError(10054, "forcibly closed by the remote host")


def _raise_other():
    raise ConnectionResetError(10054, "not from a transport teardown")


def test_reset_while_closing_is_one_rate_limited_line(caplog):
    clock = [1000.0]
    noise_log = logging.getLogger("t.noise")

    async def main():
        loop = asyncio.get_running_loop()
        h = lh.install_loop_exception_handler(
            loop, interval_s=60.0, clock=lambda: clock[0], log=noise_log)
        for _ in range(50):
            loop.call_soon(_Transport()._call_connection_lost, None)
        await asyncio.sleep(0.05)
        first = h.total
        clock[0] += 61.0
        loop.call_soon(_Transport()._call_connection_lost, None)
        await asyncio.sleep(0.05)
        return first, h.total

    with caplog.at_level(logging.DEBUG):
        first, total = asyncio.run(main())

    assert (first, total) == (50, 51)
    asyncio_errors = [r for r in caplog.records
                      if r.name == "asyncio" and r.levelno >= logging.ERROR]
    assert asyncio_errors == [], "the default handler still logged a traceback"
    ours = [r.getMessage() for r in caplog.records if r.name == "t.noise"]
    assert len(ours) == 2, ours
    assert "first occurrence" in ours[0]
    assert "50 since the last report" in ours[1]


def test_everything_else_still_reaches_asyncio_s_handler(caplog):
    async def main():
        loop = asyncio.get_running_loop()
        lh.install_loop_exception_handler(loop, log=logging.getLogger("t.noise2"))
        loop.call_soon(_raise_other)                     # same exception type, other callback
        loop.call_soon(lambda: 1 / 0)
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(main())

    msgs = [r.getMessage() for r in caplog.records
            if r.name == "asyncio" and r.levelno >= logging.ERROR]
    assert len(msgs) == 2 and all("Exception in callback" in m for m in msgs), msgs


def test_guard_server_installs_the_handler_inside_the_running_loop():
    seen = {}

    class FakeServer:
        async def serve(self, sockets=None):
            seen["handler"] = asyncio.get_running_loop().get_exception_handler()
            seen["sockets"] = sockets

    server = FakeServer()
    lh.guard_server(server)
    asyncio.run(server.serve(sockets=None))
    assert isinstance(seen["handler"], lh.ClosedClientNoise)


def test_the_exit_backstop_flushes_the_log_queue_before_os_exit(monkeypatch):
    """os._exit skips atexit: records still queued would be the last words lost."""
    import os

    from mast.api.routes import admin

    order = []
    monkeypatch.setattr(admin, "_EXIT_BACKSTOP_S", 0.02)
    monkeypatch.setattr(os, "_exit", lambda code: order.append("exit"))
    monkeypatch.setattr(lh, "flush_log_queue", lambda *a, **k: order.append("flush"))
    admin._arm_exit_backstop()
    deadline = time.monotonic() + 3.0
    while "exit" not in order and time.monotonic() < deadline:
        time.sleep(0.02)
    assert order == ["flush", "exit"], order


def _fill_queue(logger, sink, q):
    """Listener stuck inside the sink with one record, queue then filled to the brim
    (from a thread that has a running loop, so these puts never wait)."""
    sink.gate.clear()
    logger.info("first")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not q.empty():
        time.sleep(0.01)

    async def fill():
        for i in range(q.maxsize):
            logger.info("fill %d", i)

    asyncio.run(fill())
    assert q.full()


def test_the_loop_thread_drops_at_once_on_a_full_queue(lg, monkeypatch):
    logger, sink, name = lg
    monkeypatch.setattr(lh, "_QUEUE_MAX", 3)
    lh.install_log_queue([name])
    it = lh._installed[-1]                                    # noqa: SLF001
    _fill_queue(logger, sink, it.q)
    took = []

    async def on_loop():
        t0 = time.monotonic()
        logger.warning("from the loop")
        took.append(time.monotonic() - t0)

    asyncio.run(on_loop())
    assert took[0] < 0.5, "the event-loop thread waited on a full queue"
    assert it.handler.dropped == 1
    sink.gate.set()


def test_a_worker_thread_waits_for_room_instead_of_dropping(lg, monkeypatch):
    logger, sink, name = lg
    monkeypatch.setattr(lh, "_QUEUE_MAX", 3)
    lh.install_log_queue([name])
    it = lh._installed[-1]                                    # noqa: SLF001
    _fill_queue(logger, sink, it.q)
    threading.Timer(0.3, sink.gate.set).start()               # the disk recovers
    done, took = _in_thread(lambda: logger.error("late but kept"))
    assert done and 0.2 < took < 0.9, took
    assert it.handler.dropped == 0
    lh.flush_log_queue(3.0)
    assert any(ln.endswith("late but kept") for ln in sink.lines)
