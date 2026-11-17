"""EnvironmentMonitor: continuous background monitoring of all sensors."""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections import deque
from typing import Callable, NamedTuple

from mast.core.types import SensorReading
from mast.environment.alarm import worst_status
from mast.environment.base import EnvironmentSensor
from mast.logging.storage import ExperimentStorage

logger = logging.getLogger(__name__)

# Statuses that constitute an "alarm condition" worth surfacing/escalating.
_ALERT_STATUSES = ("warning", "alarm", "error")

AlarmCallback = Callable[[str, SensorReading, str], None]
# (sensor_name, reading, previous_status) -> None

#: Read cycles that may wait for the archive writer before the oldest is dropped.
#: About half an hour at the 2 s period; memory stays bounded if the disk or
#: the database never comes back.
ARCHIVE_QUEUE_CYCLES = 900
#: A cycle waiting longer than this for the writer means it is falling behind or
#: stuck. That is logged: this condition once stalled the loop for
#: minutes and nothing in the log said so.
ARCHIVE_LAG_WARN_S = 30.0
#: stop() waits this long for the writer to finish what is queued. Normally the
#: queue holds one cycle or none and this costs nothing. It is short on purpose:
#: stop() is part of the service shutdown, and a launcher that waits too long
#: falls back to a hard kill, which can leave the Nanonis port corrupted. A writer
#: still busy after that is abandoned: it never touches the database or a sink
#: again until the monitor is started again.
ARCHIVE_DRAIN_S = 2.0
#: At most one warning of each kind (lag, stuck, drop, database) per this many seconds.
_WARN_EVERY_S = 300.0

#: Optional keywords a sink's ``write`` may take; each is passed only to sinks that
#: accept it (or take ``**kwargs``).
_SINK_KEYWORDS = ("ts", "scope_ids", "captured")


class _Cycle(NamedTuple):
    """One read cycle waiting for the archive writer, with everything that must
    reflect the moment of READING rather than the moment of writing."""

    read_at: float                 # epoch seconds, taken right after the sensors were read
    readings: dict                 # name -> SensorReading
    eid: str | None                # experiment / sample current when it was read
    sid: str | None
    captured: dict                 # id(sink) -> what that sink's capture() returned
    scoped: bool                   # whether eid/sid came from a scope provider


class EnvironmentMonitor:
    """Continuously monitors all environment sensors, logs to storage.

    Over-limit handling (requirement: 如果有真空计/温度计，支持数据存档和超标报警):
      * Every reading is archived to ``environment_log`` (the SQLite store).
      * When a sensor's status crosses *into* an alert state (ok → warning /
        alarm / error), :pyattr:`on_alarm` is invoked once for that transition
        and a WARNING is logged. ``on_alarm`` is where the GUI wires a buffer
        event / banner. Re-entry into the same state is not re-fired so a
        sustained over-limit doesn't spam.
      * :meth:`alarms` returns the sensors currently in an alert state.

    Reading and archiving run on two threads. The reading thread reads every
    sensor, refreshes :meth:`get_latest`, feeds ``live_sinks``, queues the cycle
    and fires the alarm callbacks. The archive thread writes each queued cycle to
    the database and the sinks (CSV, history). Archiving used to run inline: when
    the database or the disk was slow, one cycle took minutes, the latest readings
    went stale and safety checks that want a fresh temperature refused
    (gaps of minutes in every sensor's history). Now a slow writer
    delays only the archive, never a reading or an alarm callback.

    A queued cycle carries what must reflect the moment it was read: the read time,
    the experiment/sample current then, and whatever each sink's optional
    ``capture(read_at)`` returned on the reading thread (the history sink records
    whether the instrument was quiet). A cycle written late after a sample switch
    or after a scan has started is therefore still filed where it was taken.
    ``capture`` runs on the reading thread, so it must stay in memory and never
    block; the same holds for ``live_sinks``.
    """

    def __init__(
        self,
        sensors: list[EnvironmentSensor],
        storage: ExperimentStorage | None = None,
        interval_s: float = 1.0,
        on_alarm: AlarmCallback | None = None,
        sinks: list | None = None,
        scope_provider=None,
        *,
        live_sinks: list | None = None,
        archive_queue_cycles: int = ARCHIVE_QUEUE_CYCLES,
    ):
        self._sensors: dict[str, EnvironmentSensor] = {s.name(): s for s in sensors}
        self._storage = storage
        self._interval = interval_s
        self._on_alarm = on_alarm
        # 额外的读数消费者（如 EnvironmentCsvSink）。注入式：monitor 不 import
        # core / runtime，environment 层因此不必知道实验文件夹的存在。
        self._sinks = list(sinks or [])
        # Consumers fed on the READING thread right after each read, with the read
        # time: in-memory and non-blocking only (a ring buffer, a cache). They stay
        # current while the archive writer is stalled. Anything that touches a
        # disk or a database belongs in ``sinks``.
        self._live_sinks = list(live_sinks or [])
        # () -> (experiment_id, sample_id) —— 让读数能归属到实验/样品。
        # environment_log 此前没有这两列，写了很多行却零读取方。
        self._scope_provider = scope_provider
        self._running = False
        self._thread: threading.Thread | None = None
        self._latest: dict[str, SensorReading] = {}
        self._prev_status: dict[str, str] = {}
        self._lock = threading.Lock()
        # Serialises start() and stop(). The admin rescan routes are synchronous
        # handlers on a thread pool, so two of them can overlap; a start() landing
        # in the middle of a stop() must not leave a reading thread without a writer.
        self._lifecycle = threading.RLock()
        # Set by stop() to wake an in-progress interval sleep immediately AND to
        # tell _loop it must not start another read cycle once it has been
        # cleared. Cleared at start() so a restarted monitor isn't pre-tripped.
        self._stop_event = threading.Event()
        # Archive queue of _Cycle. The condition guards the queue, the writer's
        # lifecycle flags and the counters both threads update.
        self._archive_q: deque = deque()
        self._archive_max = max(1, int(archive_queue_cycles))
        self._archive_cv = threading.Condition()
        self._archive_thread: threading.Thread | None = None
        self._archive_closing = False
        self._archive_exiting = False
        self._archive_abandoned = False
        self._archive_busy = False
        self._sink_kw: dict[int, frozenset] = {}
        self._warn_lock = threading.Lock()
        self._warned_at: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self.archive_written = 0
        self.archive_dropped = 0
        self.archive_max_lag_s = 0.0
        #: Thread name of this monitor's writer (shows up in thread dumps).
        self.archive_thread_name = f"env-archive-{id(self) & 0xFFFF:04x}"

    def start(self) -> None:
        """Start background monitoring thread."""
        with self._lifecycle:
            if self._running:
                return
            self._stop_event.clear()
            self._running = True
            self._ensure_archiver()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="env-monitor")
            self._thread.start()
        logger.info("EnvironmentMonitor started (interval=%.1fs, %d sensor(s))",
                    self._interval, len(self._sensors))

    @property
    def is_running(self) -> bool:
        """True while the background archive/alarm loop is active.

        Public because a caller that must stop() the monitor to free the serial
        bus (the live rescan) has to know whether to start() it again.
        """
        return self._running

    def stop(self) -> None:
        """Stop monitoring and CLOSE every sensor's serial handle.

        Closing matters: DL-7/Lakeshore sensors open a pyserial port on first
        read and cache it; Windows opens COM ports exclusively. If we drop the
        sensors without closing, the port stays held for the process lifetime,
        a later rescan can't re-open it, and the gauge silently goes
        "unavailable". stop() is the single teardown that frees them.

        Ordering is load-bearing: the background ``_loop`` calls ``read_all()``
        which makes each sensor lazily (re)open its COM port. If we closed the
        handles while ``_loop`` were still alive, a subsequent read cycle would
        reopen — and re-cache — the very port we just freed, leaking it for the
        process lifetime. So we must guarantee the thread has TRULY stopped
        before closing handles:

          1. signal stop (``_running``/``_stop_event``) so the interval sleep
             wakes immediately and ``_loop`` won't begin another read cycle;
          2. join — bounded, because a sensor may be blocked in a kernel serial
             read longer than ``interval``;
          3. if the thread is still alive after the bounded join (stuck in a
             blocking read), close the handles to UNBLOCK that read, then join
             again so the now-unblocked loop observes the cleared flag and exits
             before it can touch the sensors. Closing again after the final join
             is harmless (close() is idempotent / best-effort).

        The archive writer then gets up to ``ARCHIVE_DRAIN_S`` to write what is
        still queued, so the last readings reach the database and the CSV before
        the caller closes the sinks. A writer still busy after that is abandoned:
        it drops the rest and touches neither the database nor any sink again
        until the next start(). It never touches a sensor.
        """
        with self._lifecycle:
            self._running = False
            self._stop_event.set()
            thread = self._thread
            if thread is not None and thread.is_alive():
                # Generous bound: a slow serial read may exceed one interval. Cap at
                # a few seconds so stop() can't hang the GUI indefinitely.
                join_timeout = max(self._interval * 2, 3.0)
                thread.join(timeout=join_timeout)
                if thread.is_alive():
                    # Thread is wedged in a blocking read. Closing the transport is
                    # what cancels that read; do it now, then give the loop a moment
                    # to notice _running is False and return before it can reopen.
                    with self._lock:
                        self._close_sensors(list(self._sensors.values()))
                    thread.join(timeout=join_timeout)
                    if thread.is_alive():
                        logger.warning(
                            "EnvironmentMonitor._loop did not stop within %.1fs; "
                            "sensor handles closed but thread still alive (daemon — "
                            "will not block process exit)",
                            join_timeout * 2,
                        )
            self._thread = None
            # Thread is stopped (or, in the wedged case, has already had its handles
            # closed above). Close once more under the lock — idempotent — to cover
            # the normal path where the thread exited cleanly without us closing.
            with self._lock:
                sensors = list(self._sensors.values())
            self._close_sensors(sensors)
            self._stop_archiver(ARCHIVE_DRAIN_S)
        logger.info("EnvironmentMonitor stopped (%d sensor handle(s) closed)", len(sensors))

    @staticmethod
    def _close_sensors(sensors) -> None:
        """Close each sensor's transport if it exposes close() (unwraps
        _RenamedSensor). Best-effort — never raises."""
        for s in sensors:
            inner = getattr(s, "_inner", s)  # unwrap autodetect._RenamedSensor
            close = getattr(inner, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - best-effort
                    pass

    def read_all(self) -> dict[str, SensorReading]:
        """Read all sensors once (synchronous). Returns name -> reading.

        Also detects alert-state transitions and fires ``on_alarm`` for each.
        Thread-safety: the sensor set is snapshotted under the lock (so a
        concurrent replace_sensors can't mutate the dict mid-iteration), the
        slow serial reads run WITHOUT the lock, and _prev_status / _latest are
        updated under the lock. on_alarm callbacks fire AFTER releasing the lock
        so a callback that re-enters the monitor can't deadlock.
        """
        readings, _read_at, transitions = self._read_cycle()
        self._fire_alarms(transitions)
        return readings

    def _read_cycle(self) -> tuple[dict[str, SensorReading], float, list]:
        """Read every sensor, refresh the latest readings, find the transitions.

        Returns ``(readings, read_at, transitions)``; firing the callbacks is left
        to the caller, so the read time is taken before any of them runs (an alarm
        callback may take seconds: it stops a ramp and retracts the tip).
        """
        with self._lock:
            sensors = dict(self._sensors)
        readings: dict[str, SensorReading] = {}
        for name, sensor in sensors.items():
            try:
                readings[name] = sensor.read()
            except Exception as exc:
                logger.warning("Sensor '%s' read failed: %s", name, exc)
                readings[name] = SensorReading(value=0.0, unit="", status="error")
        read_at = time.time()
        transitions: list[tuple[str, SensorReading, str]] = []
        with self._lock:
            for name, reading in readings.items():
                prev = self._prev_status.get(name, "ok")
                cur = reading.status
                if cur in _ALERT_STATUSES and cur != prev:
                    transitions.append((name, reading, prev))
                self._prev_status[name] = cur
            self._latest = dict(readings)
        return readings, read_at, transitions

    def _fire_alarms(self, transitions: list) -> None:
        for name, reading, prev in transitions:
            logger.warning(
                "Environment alarm: %s %s%s → %s",
                name, reading.value,
                (" " + reading.unit) if reading.unit else "", reading.status,
            )
            if self._on_alarm is not None:
                try:
                    self._on_alarm(name, reading, prev)
                except Exception as exc:  # pragma: no cover - callback is best-effort
                    logger.debug("on_alarm callback failed for %s: %s", name, exc)

    def get_latest(self) -> dict[str, SensorReading]:
        """Get most recent cached readings."""
        with self._lock:
            return dict(self._latest)

    def alarms(self) -> dict[str, SensorReading]:
        """Sensors currently in an alert state (warning / alarm / error)."""
        with self._lock:
            return {n: r for n, r in self._latest.items()
                    if r.status in _ALERT_STATUSES}

    def overall_status(self) -> str:
        """Worst status across all sensors (alarm worst, ok best)."""
        with self._lock:
            return worst_status(*(r.status for r in self._latest.values())) \
                if self._latest else "ok"

    def check_health(self) -> dict[str, bool]:
        """Check health of all sensors."""
        return {name: sensor.is_healthy() for name, sensor in self._sensors.items()}

    def add_sensor(self, sensor: EnvironmentSensor) -> None:
        """Add a sensor at runtime."""
        self._sensors[sensor.name()] = sensor
        logger.info("Added sensor: %s", sensor.name())

    def replace_sensors(self, sensors: list[EnvironmentSensor]) -> None:
        """Swap the whole sensor set (used after a config change / rescan).

        Closes the displaced sensors (those not carried over by identity) so
        their serial ports are freed rather than leaked. NB: when the caller
        re-probes COM ports to BUILD ``sensors``, it must free the old ports
        FIRST (see MASTApp._rebuild_environment_monitor, which stop()s the old
        monitor before building) — closing here only covers the in-place swap.
        """
        new = {s.name(): s for s in sensors}
        new_ids = {id(s) for s in new.values()}
        with self._lock:
            old = list(self._sensors.values())
            self._sensors = new
            self._latest = {}
            self._prev_status = {}
        self._close_sensors([s for s in old if id(s) not in new_ids])
        logger.info("EnvironmentMonitor sensors replaced (%d sensor(s))", len(new))

    def sensor_names(self) -> list[str]:
        return list(self._sensors.keys())

    def _loop(self) -> None:
        """Background loop: read, feed the live sinks, queue the cycle, fire alarms.

        Re-checks the stop flag *before* each read cycle so that once stop()
        has signalled, the loop never starts a fresh read cycle (which would
        reopen a COM port that stop() is about to close). The inter-cycle wait
        uses the stop Event so stop() interrupts it immediately rather than
        blocking up to a full interval.

        Nothing here waits for the database or a file: the cycle is handed to the
        archive thread (see :meth:`_write_archive`), so the period stays the read
        time plus ``interval`` however slow archiving is.
        """
        while self._running and not self._stop_event.is_set():
            readings, read_at, transitions = self._read_cycle()
            eid = sid = None
            scoped = False
            if self._scope_provider is not None:
                try:
                    eid, sid = self._scope_provider()
                    scoped = True
                except Exception:  # noqa: BLE001 — scope 读不到就不归属，照常记录
                    eid = sid = None
            scope_ids = (eid, sid) if scoped else None
            if self._live_sinks:
                self._feed(self._live_sinks, read_at, readings, scope_ids, None)
            if readings and (self._storage is not None or self._sinks):
                self._enqueue_archive(_Cycle(read_at, readings, eid, sid,
                                             self._capture(read_at), scoped))
            self._fire_alarms(transitions)
            # Interruptible sleep: returns True immediately when stop() sets the
            # event, so teardown isn't delayed by a long interval.
            if self._stop_event.wait(self._interval):
                break

    def _capture(self, read_at: float) -> dict:
        """Ask each archive sink that offers ``capture(read_at)`` for what it needs
        to know about this moment (reading thread; must be cheap)."""
        out: dict = {}
        for sink in self._sinks:
            cap = getattr(sink, "capture", None)
            if not callable(cap):
                continue
            try:
                token = cap(read_at)
            except Exception as exc:  # noqa: BLE001 — a sink's capture never stops a reading
                logger.debug("env sink capture failed: %r", exc)
                continue
            if token is not None:
                out[id(sink)] = token
        return out

    # ── archive writer ────────────────────────────────────────────────

    def archive_stats(self) -> dict:
        """Queue length, cycles written / dropped, worst lag seen (seconds)."""
        with self._archive_cv:
            return {
                "queued": len(self._archive_q),
                "written": self.archive_written,
                "dropped": self.archive_dropped,
                "max_lag_s": round(self.archive_max_lag_s, 3),
                "abandoned": self._archive_abandoned,
                "writer_alive": bool(self._archive_thread is not None
                                     and self._archive_thread.is_alive()),
            }

    def wait_archived(self, timeout: float = 5.0) -> bool:
        """Block until every queued cycle has been written; False on timeout."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._archive_cv:
            while self._archive_q or self._archive_busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._archive_cv.wait(left)
        return True

    def _enqueue_archive(self, item) -> None:
        dropped_now = False
        with self._archive_cv:
            if len(self._archive_q) >= self._archive_max:
                self._archive_q.popleft()
                self.archive_dropped += 1
                dropped_now = True
            self._archive_q.append(item)
            oldest = self._archive_q[0][0]
            queued = len(self._archive_q)
            dropped = self.archive_dropped
            self._archive_cv.notify_all()
        # Said here, on the reading thread, because a stuck writer cannot say it.
        waited = time.time() - oldest
        if waited > ARCHIVE_LAG_WARN_S:
            self._warn_rate_limited(
                "stuck", "环境归档线程 %.0f s 没有写完一轮（排队 %d 轮，已丢弃 %d 轮）；读数与告警不受影响",
                waited, queued, dropped)
        if dropped_now:
            self._warn_rate_limited(
                "drop", "环境归档队列已满（%d 轮），丢弃最旧的一轮（累计 %d 轮）",
                self._archive_max, dropped)

    def _ensure_archiver(self) -> None:
        """Start the writer, or keep the one still running from a previous run.

        Started under the condition so two callers cannot both start one; the new
        thread's first step is to take that same condition, so it simply waits
        until this returns. A writer that a stop() abandoned is taken back into
        service here.
        """
        with self._archive_cv:
            self._archive_closing = False
            self._archive_abandoned = False
            t = self._archive_thread
            if t is not None and t.is_alive() and not self._archive_exiting:
                self._archive_cv.notify_all()
                return
            self._archive_exiting = False
            t = threading.Thread(target=self._archive_loop, daemon=True,
                                 name=self.archive_thread_name)
            self._archive_thread = t
            t.start()

    def _stop_archiver(self, timeout: float) -> None:
        with self._archive_cv:
            self._archive_closing = True
            self._archive_cv.notify_all()
            t = self._archive_thread
        if t is None or t is threading.current_thread():
            return
        t.join(timeout=max(0.0, float(timeout)))
        if t.is_alive():
            with self._archive_cv:
                self._archive_abandoned = True
                left = len(self._archive_q)
                self._archive_cv.notify_all()
            logger.warning("环境归档线程 %.0f s 内没有写完（还剩 %d 轮）：这些轮次放弃，"
                           "不再写库和文件", timeout, left)

    def _archive_loop(self) -> None:
        while True:
            with self._archive_cv:
                while not self._archive_q and not self._archive_closing:
                    self._archive_cv.wait()
                if not self._archive_q:
                    # Closing and drained. Flag it under the condition so a
                    # start() racing this exit starts a fresh writer.
                    self._archive_exiting = True
                    return
                item = self._archive_q.popleft()
                if self._archive_abandoned:
                    self.archive_dropped += 1
                    continue
                self._archive_busy = True
                backlog = len(self._archive_q)
            try:
                self._write_archive(item, backlog)
            except Exception as exc:  # noqa: BLE001 — the writer must outlive any one cycle
                logger.debug("environment archive cycle failed: %r", exc)
            finally:
                with self._archive_cv:
                    self._archive_busy = False
                    self._archive_cv.notify_all()

    def _write_archive(self, item, backlog: int = 0) -> None:
        cyc = item if isinstance(item, _Cycle) else _Cycle(item[0], item[1], item[2],
                                                          item[3], {}, False)
        lag = time.time() - cyc.read_at
        if lag > self.archive_max_lag_s:
            self.archive_max_lag_s = lag
        if lag > ARCHIVE_LAG_WARN_S:
            self._warn_rate_limited(
                "lag",
                "环境读数归档落后 %.0f s（还有 %d 轮排队，已丢弃 %d 轮）；读数与告警不受影响",
                lag, backlog, self.archive_dropped)
        # Abandoned by a stop() while this cycle was in hand (the database call
        # below may have been the thing it waited on): leave the closed sinks alone.
        if self._archive_abandoned:
            return
        if self._storage is not None:
            self._log_rows(cyc.read_at, cyc.readings, cyc.eid, cyc.sid)
        if self._archive_abandoned:
            return
        # 额外 sink（CSV 落盘、历史桶）。每个 sink 自己吞异常并自我禁用；这里再包
        # 一层。传的是**读数时刻**和读数当时的作用域：排队等过的读数也落在它被
        # 读到的那一秒、那个样品上。
        self._feed(self._sinks, cyc.read_at, cyc.readings,
                   (cyc.eid, cyc.sid) if cyc.scoped else None, cyc.captured,
                   archive=True)
        with self._archive_cv:
            self.archive_written += 1

    def _feed(self, sinks: list, read_at: float, readings: dict,
              scope_ids, captured, *, archive: bool = False) -> None:
        """Hand one cycle to *sinks*, with the keywords each sink takes.

        On the archive path the abandon flag is checked before every write: a write
        that was in progress when stop() gave up finishes, nothing after it starts.
        """
        for sink in sinks:
            if archive and self._archive_abandoned:
                return
            accepted = self._sink_keywords(sink)
            extra: dict = {}
            if "ts" in accepted:
                extra["ts"] = read_at
            if "scope_ids" in accepted and scope_ids is not None:
                extra["scope_ids"] = scope_ids
            if "captured" in accepted and captured and id(sink) in captured:
                extra["captured"] = captured[id(sink)]
            for name, reading in readings.items():
                if archive and self._archive_abandoned:
                    return
                try:
                    sink.write(name, reading.value, reading.unit, reading.status, **extra)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("env sink write failed for %s: %r", name, exc)

    def _log_rows(self, read_at: float, readings: dict, eid, sid) -> None:
        """One transaction for the whole cycle where the storage offers it."""
        many = getattr(self._storage, "log_environment_many", None)
        if callable(many):
            rows = [(name, r.value, r.unit, r.status) for name, r in readings.items()]
            try:
                many(rows, timestamp=read_at, experiment_id=eid, sample_id=sid)
            except Exception as exc:  # noqa: BLE001
                self._warn_rate_limited("db", "环境读数入库失败（本轮 %d 条）：%s",
                                        len(rows), exc)
            return
        for name, reading in readings.items():
            try:
                self._storage.log_environment(
                    sensor_name=name,
                    value=reading.value,
                    unit=reading.unit,
                    status=reading.status,
                    experiment_id=eid,
                    sample_id=sid,
                )
            except Exception as exc:
                logger.warning("Failed to log sensor '%s': %s", name, exc)

    def _sink_keywords(self, sink) -> frozenset:
        """Which of the optional keywords ``sink.write`` takes (checked once per sink)."""
        key = id(sink)
        hit = self._sink_kw.get(key)
        if hit is None:
            try:
                params = list(inspect.signature(sink.write).parameters.values())
                if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
                    hit = frozenset(_SINK_KEYWORDS)
                else:
                    names = {p.name for p in params}
                    hit = frozenset(k for k in _SINK_KEYWORDS if k in names)
            except (TypeError, ValueError, AttributeError):
                hit = frozenset()
            self._sink_kw[key] = hit
        return hit

    def _warn_rate_limited(self, key: str, msg: str, *args) -> None:
        now = time.monotonic()
        with self._warn_lock:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            last = self._warned_at.get(key)
            if last is not None and now - last < _WARN_EVERY_S:
                return
            count = self._suppressed.pop(key, 1)
            self._warned_at[key] = now
        if count > 1:
            logger.warning(msg + "（上次告警以来共 %d 次）", *args, count)
        else:
            logger.warning(msg, *args)
