"""The scan in progress, row by row, for the scan map (Nanonis-like live view).

Design: ``docs/v2/design/scan_map_live.md``.

The map used to show a scan in progress only through the vision monitor's
1/8-milestone PNGs: eight jumps per frame, no scan line, and nothing at all
when the vision model is not loaded or the operator started the scan by hand
in Nanonis. This service streams the frame as it is acquired:

* **Idle costs nothing.** While no scan runs it only reads the instrument-state
  snapshot the 1 s state poller already maintains. Zero Nanonis calls.
* **Only while ``Scan_StatusGet`` says running.** Every tick re-reads the
  status itself before grabbing; when the scan stops it grabs once more,
  publishes what is left (including the half row), ends the stream.
* **Data role only, opportunistically.** Every call goes out on ``role="data"``
  with a short lock wait and ``count_health=False``: when another reader holds
  the data role (the current monitor's ~20 Hz oscilloscope pump, the vision
  monitor's grabs) this tick is skipped, never queued. Never ``main``, never
  ``monitor`` (the crash watchdog's budget lives there). Read-only: no command
  this module sends can change anything on the instrument.
* **Rate-limited by measurement, not by estimate.** The pace is the larger of
  0.5 s, one line time and ``1/max_hz``, capped at 10 s; on top of that two
  floors the cap never cuts: frame bytes over the bandwidth knob, and four
  times the last grab's duration. The loop waits that long AFTER each grab,
  so a grab takes at most a fifth of the data role however slow the controller
  turns out to be. A frame of more than 4 MiB (2048² and up) is not grabbed at
  all: the map keeps the 1/8 snapshots and the status stream says why.
* **Not while the link is in trouble.** No call goes out unless the TCP circuit
  breaker is CLOSED: a ``count_health=False`` call taken as the half-open probe
  would never report back and leave the breaker wedged.
* **Only new rows.** Rows are taken from the NaN front (Nanonis NaN-fills what
  it has not acquired yet) and published once each, ONE LiveHub frame per row
  (kind ``map2d_rows``): one stream per scan frame (``local/scan/live/<n>``),
  of which only the last two finished ones are kept, plus a ``scalar`` status
  stream (``local/scan/live``) carrying the session number, rows done and
  direction for the scan line. (One frame per PIXEL costs
  ~390 B each in the process and a 0.5 s full-GC pause after a few frames.)

Channel selection reuses the shared readout path
(:func:`mast.skills.builtins._scan_readout.resolve_readout`):
``Scan_FrameDataGrab`` takes the SIGNAL index that ``Scan_BufferGet`` returns,
not a position in the buffer — the wrong one gives a reply whose layout does
not match. The shared helpers are called through :class:`_DataRole`, which pins
every call to the data role.

Row order for up-scans (bottom row first) is INFERRED from which end of the
grabbed array the NaN front starts at; it has not been checked on a real
controller. The stream says so (``meta.row0_source``) and the map says so.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from mast.i18n import tr

logger = logging.getLogger(__name__)

STATUS_STREAM = "local/scan/live"
FRAME_STREAM_PREFIX = "local/scan/live/"
SOURCE = "local/scan"
DATA_ROLE = "data"

#: Lock wait on the data role. Short on purpose: a busy role means another
#: reader is mid-transaction, and the right answer is to skip this tick.
LOCK_WAIT_S = 0.25
MIN_INTERVAL_S = 0.5
#: Ceiling of the PACE (line time, max_hz). Never applied to the two floors
#: below: capping them is what let a slow grab run the data role at > 1/5.
MAX_INTERVAL_S = 10.0
#: The loop waits interval >= DUTY_FACTOR x the last grab's duration AFTER each
#: grab, so a grab takes at most 1/(1 + DUTY_FACTOR) = 1/5 of the data role.
DUTY_FACTOR = 4.0
IDLE_POLL_S = 0.5
DISABLED_POLL_S = 2.0
BACKOFF_S = (1.0, 2.0, 5.0)
#: A session whose reads keep failing for this long is ended rather than left
#: claiming to be live.
STALE_SESSION_S = 30.0
#: Published rows are binned to at most this many pixels a side.
MAX_SIDE = 512
#: Scan_FrameDataGrab ships float32 samples.
BYTES_PER_SAMPLE = 4
#: A frame of MORE than this is not grabbed (strictly more: 1024 x 1024 float32
#: is exactly 4 MiB, a common STM frame, and still streams). 2048² would move
#: 16 MiB per grab over the data role; the map keeps the 1/8 snapshots.
MAX_GRAB_BYTES = 4 * 1024 * 1024
#: How often a too-large frame, or a held breaker, is looked at again.
RECHECK_S = 5.0
#: Finished frame streams kept in the hub; older ones are dropped at once.
KEEP_ENDED = 2
#: When the scan stops and the data role is busy for the last grab, retry for
#: this long before ending the stream without the last rows.
FINAL_RETRY_S = 5.0
FINAL_RETRY_STEP_S = 0.5
#: stop() waits for a worker in mid-call: 2 x the longest grab seen + 1 s,
#: between these bounds. Closing the pool under a call in flight is what
#: corrupts the Nanonis port; close_all() then waits another 5 s on the lock.
STOP_JOIN_MIN_S = 3.0
STOP_JOIN_MAX_S = 20.0


# ── pure helpers (tested directly) ─────────────────────────────────────────

def tick_interval(*, line_time_s: float | None, max_hz: float, max_bytes_per_s: float,
                  frame_bytes: int, last_grab_s: float | None) -> tuple[float, bool]:
    """Seconds to wait after this grab, and whether a LIMIT (not the scan) made
    it longer than :data:`MAX_INTERVAL_S` — the live view is then visibly
    slower than the scan, which the status stream reports as degraded.

    The pace (line time, ``1/max_hz``) is capped at :data:`MAX_INTERVAL_S`; the
    bandwidth floor (``frame_bytes / max_bytes_per_s``, the knob promises "not
    above it") and the duty floor (``DUTY_FACTOR x last grab``) are not."""
    base = max(MIN_INTERVAL_S, float(line_time_s or 0.0) if math.isfinite(line_time_s or 0.0) else 0.0)
    hz_floor = 1.0 / max_hz if max_hz and max_hz > 0 else MIN_INTERVAL_S
    byte_floor = (frame_bytes / max_bytes_per_s
                  if max_bytes_per_s and max_bytes_per_s > 0 and frame_bytes > 0 else 0.0)
    duty_floor = DUTY_FACTOR * last_grab_s if last_grab_s and last_grab_s > 0 else 0.0
    interval = max(min(max(base, hz_floor), MAX_INTERVAL_S), byte_floor, duty_floor)
    return interval, max(hz_floor, byte_floor, duty_floor) > MAX_INTERVAL_S


def too_large(pixels: int | None, lines: int | None) -> bool:
    """A frame of more than :data:`MAX_GRAB_BYTES` is not grabbed (unknown size: grab)."""
    if not pixels or not lines:
        return False
    return int(pixels) * int(lines) * BYTES_PER_SAMPLE > MAX_GRAB_BYTES


def looks_like_z(name: str) -> bool:
    """The topography channel, by the same rule as ``ScanVisionMonitor``."""
    low = (name or "").strip().lower()
    return low == "z" or any(k in low for k in ("z (", "z [", "z(", "height", "topo"))


def bin_factors(rows: int, cols: int, max_side: int = MAX_SIDE) -> tuple[int, int]:
    """Integer binning so neither published side exceeds ``max_side``."""
    by = max(1, math.ceil(rows / max_side)) if rows > 0 else 1
    bx = max(1, math.ceil(cols / max_side)) if cols > 0 else 1
    return by, bx


def infer_row0(arr: np.ndarray, scan_dir: str) -> str | None:
    """Which edge of the picture array row 0 is — ``"top"``, ``"bottom"`` — or
    ``None`` when this grab cannot tell.

    Nanonis NaN-fills the rows it has not acquired, so mid-scan the acquired
    rows form one block at the end of the array where acquisition STARTED. A
    down-scan starts at the top of the picture, an up-scan at the bottom; the
    block's end therefore says which edge row 0 is. A frame with no NaN rows
    (or only NaN rows, or a block touching neither end) says nothing.
    """
    if arr.ndim != 2 or arr.shape[0] < 2:
        return None
    acq = ~np.isnan(arr).all(axis=1)
    if acq.all() or not acq.any():
        return None
    at_start, at_end = bool(acq[0]), bool(acq[-1])
    if at_start == at_end:
        return None
    starts_at_top = scan_dir != "up"
    if at_start:
        return "top" if starts_at_top else "bottom"
    return "bottom" if starts_at_top else "top"


def value_scale(channel_name: str) -> tuple[str, str, float]:
    """``(value key, unit, factor)`` for a Nanonis channel name.

    Z in metres goes out in picometres and a current in amperes in picoamperes,
    each kept to 0.01 — far below a DAC step, and half the JSON per pixel."""
    name = (channel_name or "").strip()
    low = name.lower()
    key = name.split("(")[0].strip() or "value"
    if "(m)" in low.replace(" ", ""):
        return key, "pm", 1e12
    if "(a)" in low.replace(" ", ""):
        return key, "pA", 1e12
    return key, "", 1.0


def _round(v: float, factor: float) -> float:
    x = float(v) * factor
    return round(x, 2) if factor != 1.0 else float(f"{x:.6g}")


def binned_row(block: np.ndarray, bx: int, nx: int) -> np.ndarray:
    """Mean of each ``rows x bx`` cell of ``block`` (finite samples only), as
    ``nx`` values; a cell with no finite sample is NaN. Vectorised, and silent
    on all-NaN cells (``np.nanmean`` would warn)."""
    rows, cols = block.shape
    pad = nx * bx - cols
    if pad > 0:
        block = np.pad(block, ((0, 0), (0, pad)), constant_values=np.nan)
    cells = block[:, :nx * bx].reshape(rows, nx, bx)
    finite = np.isfinite(cells)
    total = np.where(finite, cells, 0.0).sum(axis=(0, 2))
    count = finite.sum(axis=(0, 2))
    out = np.full(nx, np.nan)
    np.divide(total, count, out=out, where=count > 0)
    return out


# ── calls ──────────────────────────────────────────────────────────────────

#: ``record.error`` of a call refused because the service is stopping.
STOPPING_ERROR = "scan-live: stopping"


class _DataRole:
    """The minimal context the shared scan helpers call. Every call goes out on
    the data role, opportunistically, and outside the comms-health statistics
    (a background reader must not reset other roles' failure streaks).

    Once the service is told to stop, no further call is STARTED: a tick that
    would make several (probing channels grabs one after another) unwinds at
    the next one, instead of keeping the role while shutdown waits to close the
    sockets."""

    def __init__(self, pool: Any, stopping: Callable[[], bool] | None = None) -> None:
        self._pool = pool
        self._stopping = stopping

    def safe_call(self, method: str, *args: Any, **_ignored: Any) -> Any:
        if self._stopping is not None and self._stopping():
            from mast.core.types import NanonisCallRecord
            return NanonisCallRecord(method=method, error=STOPPING_ERROR)
        return self._pool.safe_call(method, *args, role=DATA_ROLE,
                                    lock_timeout_s=LOCK_WAIT_S, count_health=False)


def breaker_closed(pool: Any) -> bool:
    """True only when the TCP circuit breaker is CLOSED. ``comms_healthy()`` is
    also True while HALF_OPEN — and a ``count_health=False`` call that takes
    the half-open probe never reports back, wedging the breaker for every role.
    A pool without the snapshot (a test double) has no breaker to respect."""
    snap_fn = getattr(pool, "comms_snapshot", None)
    if snap_fn is None:
        return True
    try:
        return (snap_fn() or {}).get("state") == "closed"
    except Exception:  # noqa: BLE001 — unreadable means "not known to be closed"
        return False


def _body(rec: Any) -> list | None:
    if rec is None or getattr(rec, "error", ""):
        return None
    rv = getattr(rec, "return_value", None)
    if isinstance(rv, (list, tuple)) and len(rv) > 2 and isinstance(rv[2], (list, tuple)):
        return list(rv[2])
    return None


def _floats(body: list | None, n: int) -> list[float] | None:
    from mast.io.nanonis_files import scalar_float
    if not body or len(body) < n:
        return None
    out = [scalar_float(x) for x in body[:n]]
    return None if any(v is None for v in out) else [float(v) for v in out]  # type: ignore[arg-type]


# ── the service ────────────────────────────────────────────────────────────

@dataclass
class _Session:
    n: int
    stream_id: str
    signal_index: int
    src_rows: int
    src_cols: int
    by: int
    bx: int
    ny: int
    nx: int
    scan_dir: str
    row0: str
    row0_source: str
    geom: tuple[float, float, float, float, float]
    line_time_s: float | None
    key: str
    factor: float
    channel: str = ""
    published: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    acquired_prev: int = 0
    last_ok: float = 0.0
    #: This session has seen unacquired (all-NaN) rows — see _tick.
    saw_front: bool = False
    #: Opened because the frame MOVED: the buffer may still hold the old frame's
    #: rows. (first row, last row, acquired rows) of that buffer; nothing is
    #: published until the buffer shows something else — see _fresh_data.
    stale_edges: tuple[np.ndarray, np.ndarray, int] | None = None


class ScanLiveService:
    """See the module docstring. ``step(now)`` is one tick and never raises;
    the thread only sleeps between ticks."""

    def __init__(self, *, pool_getter: Callable[[], Any], state_getter: Callable[[], Any],
                 live_getter: Callable[[], Any] | None = None,
                 thresholds_getter: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self._pool_getter = pool_getter
        self._state_getter = state_getter
        self._live_getter = live_getter
        self._thresholds_getter = thresholds_getter
        self._clock = clock
        self._wall = wall
        self._session: _Session | None = None
        self._n = 0
        self._failures = 0
        self._last_grab_s: float | None = None
        #: The longest single grab seen — sizes stop()'s wait for a call in flight.
        self._max_grab_s = 0.0
        self._status_declared = False
        self._last_status: dict | None = None
        self._stop = threading.Event()
        #: The stop event of the worker running the current tick (None: driven by hand).
        self._tick_stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._wanted = False
        self._lock = threading.Lock()
        #: buffer channel list → the signal index streamed for it (see _pick_channel)
        self._channel_cache: dict[tuple[int, ...], int] = {}
        #: Finished frame streams still in the hub, oldest first (KEEP_ENDED of them).
        self._ended: deque[str] = deque()
        #: (lines, pixels) of a running frame too large to grab, else None.
        self._too_large: tuple[int, int] | None = None
        #: The breaker was not closed at the last tick.
        self._comms_hold = False
        #: When the stop's last grab first found the data role busy (see _step).
        self._finishing_since: float | None = None

    # —— lifecycle ——
    def start(self) -> None:
        """Idempotent. Never runs two workers at once.

        ``stop()`` joins for a bounded time, and a worker can outlive it while
        it finishes a call. Starting again then — the reconnect path does
        exactly that — must not spawn a second worker next to the retiring one,
        and must not silently do nothing either. The slot ``_thread`` is held
        until the worker's very last step, which hands it over: a worker that
        leaves while ``_wanted`` is set starts its successor itself."""
        with self._lock:
            self._wanted = True
            if self._thread is None:
                self._spawn()

    def _spawn(self) -> None:
        # Each worker gets its OWN stop event: replacing a shared attribute
        # while an old worker still loops on it would revive that worker.
        stop = threading.Event()
        self._stop = stop
        self._thread = threading.Thread(target=self._run, args=(stop,), daemon=True,
                                        name="ScanLiveService")
        self._thread.start()

    def join_budget(self) -> float:
        """How long :meth:`stop` waits for a worker in mid-call."""
        return min(STOP_JOIN_MAX_S, max(STOP_JOIN_MIN_S, 2.0 * self._max_grab_s + 1.0))

    def stop(self, join_timeout: float | None = None) -> None:
        """Stop the worker and wait for it — by default for :meth:`join_budget`,
        which covers the slowest grab seen. A worker past it is left to finish
        its call (never interrupted: closing a socket under a call in flight is
        what damages the Nanonis port) and is named in a warning."""
        with self._lock:
            self._wanted = False
            self._stop.set()
            t = self._thread
        if t is None or t is threading.current_thread():
            return
        budget = self.join_budget() if join_timeout is None else join_timeout
        t.join(timeout=budget)
        if t.is_alive():
            logger.warning("scan-live: worker still inside a Nanonis call %.1f s after stop "
                           "(longest grab seen %.1f s); not waiting longer", budget, self._max_grab_s)

    def _run(self, stop: threading.Event) -> None:
        self._tick_stop = stop
        try:
            while not stop.is_set():
                wait = self.step(self._clock())
                stop.wait(max(0.05, wait))
            try:
                self._finish()
            except Exception:  # noqa: BLE001 — leaving must reach the hand-over below
                logger.debug("scan-live: final status failed", exc_info=True)
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                    if self._wanted:            # started again while we were leaving
                        self._spawn()

    def _stopping(self) -> bool:
        ev = self._tick_stop
        return ev is not None and ev.is_set()

    # —— collaborators ——
    def _live(self) -> Any:
        if self._live_getter is not None:
            return self._live_getter()
        from mast.live import get_live_hub
        return get_live_hub()

    def _thresholds(self) -> Any:
        if self._thresholds_getter is not None:
            return self._thresholds_getter()
        from mast.monitoring.thresholds import get_monitor_thresholds
        return get_monitor_thresholds()

    # —— one tick ——
    def step(self, now: float) -> float:
        try:
            return self._step(now)
        except Exception:  # noqa: BLE001 — a display feature must never surface into a scan
            logger.debug("scan-live: tick failed", exc_info=True)
            return self._failed(now)

    def _step(self, now: float) -> float:
        th = self._thresholds()
        if not getattr(th, "scan_live_enabled", True):
            self._finish()
            return DISABLED_POLL_S
        pool = self._pool_getter()
        if pool is None:
            self._finish()
            return DISABLED_POLL_S
        if self._session is None:
            state = self._state_getter()
            snap = state.snapshot() if state is not None else None
            if getattr(snap, "scan_running", None) is not True:
                self._too_large = None
                self._status(active=0)
                return IDLE_POLL_S

        # Not one call unless the breaker is CLOSED (see breaker_closed).
        if not breaker_closed(pool):
            self._comms_hold = True
            sess = self._session
            if sess is not None and now - sess.last_ok > STALE_SESSION_S:
                self._finish()
            else:
                self._status(active=1, session=sess, interval=RECHECK_S)
            return RECHECK_S
        self._comms_hold = False

        ctx = _DataRole(pool, self._stopping)
        running = self._read_running(ctx)
        if running == "busy":
            return MIN_INTERVAL_S              # someone else's turn on the data role
        if running is None:
            return self._failed(now)
        if running is False:
            return self._wind_up(ctx, now)
        self._finishing_since = None
        return self._tick(ctx, now)

    def _wind_up(self, ctx: _DataRole, now: float) -> float:
        """The scan stopped: grab once more for the rows since the last tick
        (the half row included), then end the stream.

        A busy data role is retried for :data:`FINAL_RETRY_S` instead of giving
        up at once: the vision monitor takes its own final grab exactly when a
        scan stops, and losing that race dropped the last rows of the frame."""
        from mast.core.connection import is_lock_busy

        sess = self._session
        if sess is None:
            self._finishing_since = None
            self._finish()
            return IDLE_POLL_S
        arr, rec, dur = self._grab(ctx, sess.signal_index)
        if arr is None and is_lock_busy(rec):
            if self._finishing_since is None:
                self._finishing_since = now
            if now - self._finishing_since < FINAL_RETRY_S:
                return FINAL_RETRY_STEP_S
        if arr is not None:
            self._note_grab(dur)
            if arr.shape == (sess.src_rows, sess.src_cols) and self._fresh_data(sess, arr):
                self._publish_rows(sess, arr, final=True)
        self._finishing_since = None
        self._finish()
        return IDLE_POLL_S

    def _read_running(self, ctx: _DataRole) -> bool | None | str:
        """True / False from ``Scan_StatusGet``; ``"busy"`` when the data role
        was taken (not a failure); ``None`` when the reply is unusable."""
        from mast.core.connection import is_lock_busy
        from mast.io.nanonis_files import scalar_int
        r = ctx.safe_call("Scan_StatusGet")
        if is_lock_busy(r):
            return "busy"
        body = _body(r)
        if not body:
            return None
        v = scalar_int(body[0])
        return None if v is None else v != 0

    def _grab(self, ctx: _DataRole, signal_index: int) -> tuple[np.ndarray | None, Any, float]:
        from mast.skills.builtins.scan_frame import _grab_channel_array
        t0 = self._clock()
        arr, rec = _grab_channel_array(ctx, int(signal_index), 1, shape_2d=True)
        dur = max(0.0, self._clock() - t0)
        if arr is not None and arr.ndim != 2:
            arr = None
        return arr, rec, dur

    def _note_grab(self, dur: float) -> None:
        self._last_grab_s = dur
        self._max_grab_s = max(self._max_grab_s, dur)

    def _tick(self, ctx: _DataRole, now: float) -> float:
        from mast.core.connection import is_lock_busy

        sess = self._session
        if sess is None:
            opened = self._open(ctx, now)
            if opened is None:
                return self._not_opened(now)
            sess, arr = opened
        else:
            arr, rec, dur = self._grab(ctx, sess.signal_index)
            if arr is None:
                if is_lock_busy(rec):
                    return MIN_INTERVAL_S      # someone else's turn; not a failure
                return self._failed(now)
            self._note_grab(dur)
            why = self._is_new_frame(sess, arr, self._direction(rec))
            if why is not None:
                self._end_session()
                opened = self._open(ctx, now, arr=arr, rec=rec, grabbed_index=sess.signal_index,
                                    stale=(why == "moved"))
                if opened is None:
                    return self._not_opened(now)
                sess, arr = opened
        if not self._fresh_data(sess, arr):
            # The frame moved and the buffer still holds the old frame's rows:
            # nothing of THIS frame to show yet. Keep the status alive.
            sess.last_ok = now
            self._failures = 0
            interval, degraded = self._interval(sess)
            self._status(active=1, session=sess, interval=interval, degraded=degraded)
            return interval
        blank_rows = bool(np.isnan(arr).all(axis=1).any())
        if blank_rows:
            sess.saw_front = True
        if sess.row0_source != "nan_front":
            r0 = infer_row0(arr, sess.scan_dir)
            if r0 is not None and not sess.published.any():
                # Nothing is on the map yet, so the orientation can still be
                # corrected — and the stream's meta with it.
                sess.row0, sess.row0_source = r0, "nan_front"
                self._declare(sess)
        # Publish only once this session has SEEN the NaN front. A running scan
        # whose buffer has no unacquired row at all is either the previous frame
        # still sitting there or an instrument that backfills stale data; either
        # way, painting it as "live" would be a lie. The final flush on stop is
        # exempt — by then the buffer is what this scan produced.
        if sess.saw_front:
            self._publish_rows(sess, arr, final=False)
        sess.acquired_prev = int((~np.isnan(arr).all(axis=1)).sum())
        sess.last_ok = now
        self._failures = 0
        interval, degraded = self._interval(sess)
        self._status(active=1, session=sess, interval=interval, degraded=degraded)
        return interval

    def _interval(self, sess: _Session) -> tuple[float, bool]:
        th = self._thresholds()
        return tick_interval(
            line_time_s=sess.line_time_s,
            max_hz=float(getattr(th, "cm_scan_live_max_hz", 2.0)),
            max_bytes_per_s=float(getattr(th, "cm_scan_live_max_mbps", 2.0)) * 1e6,
            frame_bytes=sess.src_rows * sess.src_cols * BYTES_PER_SAMPLE,
            last_grab_s=self._last_grab_s)

    def _failed(self, now: float) -> float:
        self._failures += 1
        sess = self._session
        if sess is not None and now - sess.last_ok > STALE_SESSION_S:
            logger.info("scan-live: reads failing for %.0f s, ending session %d",
                        now - sess.last_ok, sess.n)
            self._finish()
        return BACKOFF_S[min(self._failures, len(BACKOFF_S)) - 1]

    def _not_opened(self, now: float) -> float:
        """No session could be opened. A frame too large to grab is a state, not
        a failure: say so on the status stream and look again later."""
        if self._too_large is not None:
            self._failures = 0
            self._status(active=1, interval=RECHECK_S)
            return RECHECK_S
        return self._failed(now)

    def _fresh_data(self, sess: _Session, arr: np.ndarray) -> bool:
        """False while a session opened for a MOVED frame still sees the buffer
        it was opened on: Nanonis restarts the scan when the frame moves, and
        until it does the buffer holds the old frame's rows — which, published,
        would land in the new frame's place. Fresh once either edge row or the
        acquired-row count differs from that buffer (a restart NaN-fills it)."""
        e = sess.stale_edges
        if e is None:
            return True
        first, last, acquired0 = e
        acquired = int((~np.isnan(arr).all(axis=1)).sum())
        if (acquired < acquired0 or not np.array_equal(arr[0], first, equal_nan=True)
                or not np.array_equal(arr[-1], last, equal_nan=True)):
            sess.stale_edges = None
            return True
        return False

    # —— sessions ——
    @staticmethod
    def _direction(rec: Any) -> str:
        from mast.vision.scan_monitor import ScanVisionMonitor
        return "up" if ScanVisionMonitor._frame_direction(_body(rec)) == 1 else "down"

    def _geometry(self, ctx: _DataRole) -> tuple[float, float, float, float, float] | None:
        state = self._state_getter()
        snap = state.snapshot() if state is not None else None
        g = (getattr(snap, "scan_center_x_m", None), getattr(snap, "scan_center_y_m", None),
             getattr(snap, "scan_width_m", None), getattr(snap, "scan_height_m", None),
             getattr(snap, "scan_angle_deg", None))
        if all(isinstance(v, (int, float)) and math.isfinite(v) for v in g) and g[2] > 0 and g[3] > 0:
            return tuple(float(v) for v in g)  # type: ignore[return-value]
        vals = _floats(_body(ctx.safe_call("Scan_FrameGet")), 5)
        if vals and vals[2] > 0 and vals[3] > 0:
            return tuple(vals)  # type: ignore[return-value]
        return None

    def _is_new_frame(self, sess: _Session, arr: np.ndarray, scan_dir: str) -> str | None:
        """Why this grab belongs to a different frame than the session's —
        ``"shape"``, ``"dir"``, ``"front"`` (the front went back: the next frame
        began), ``"moved"`` (the scan frame was moved) — or None."""
        if arr.shape != (sess.src_rows, sess.src_cols):
            return "shape"
        if scan_dir != sess.scan_dir:
            return "dir"
        acquired = int((~np.isnan(arr).all(axis=1)).sum())
        if acquired + 1 < sess.acquired_prev:
            return "front"
        state = self._state_getter()
        snap = state.snapshot() if state is not None else None
        g = (getattr(snap, "scan_center_x_m", None), getattr(snap, "scan_center_y_m", None),
             getattr(snap, "scan_width_m", None), getattr(snap, "scan_height_m", None),
             getattr(snap, "scan_angle_deg", None))
        if all(isinstance(v, (int, float)) and math.isfinite(v) for v in g):
            tol = (1e-12, 1e-12, 1e-12, 1e-12, 1e-6)
            if any(abs(float(a) - b) > t for a, b, t in zip(g, sess.geom, tol)):
                return "moved"
        return None

    @staticmethod
    def _channel_name(rec: Any) -> str:
        return next((x for x in (_body(rec) or []) if isinstance(x, str)), "") or ""

    def _pick_channel(self, ctx: _DataRole, arr: np.ndarray | None, rec: Any,
                      grabbed_index: int | None) -> tuple[int, np.ndarray, Any, str] | None:
        """The channel to stream — Z when the buffer has it — with a grab of it.

        ``Scan_FrameDataGrab`` takes an index from the list ``Scan_BufferGet``
        returns, NOT a position in that list: the wrong one gives a reply whose
        layout does not match. :func:`resolve_readout` reads that list (and
        makes its first guess at Z); the guess is then CONFIRMED by the channel
        name the grab itself returns — the one name that cannot belong to a
        different index space — trying the other acquired channels the way
        ``ScanVisionMonitor._resolve_channel`` does (same name rule,
        :func:`looks_like_z`). The answer is cached per buffer configuration and
        a cached channel is used as is — no re-probing of every channel on every
        frame, which is what a buffer without Z would cost.

        A frame of more than :data:`MAX_GRAB_BYTES` (from ``Scan_BufferGet``'s
        pixels x lines) is not grabbed at all: ``self._too_large`` is set and
        None returned.
        """
        from mast.core.connection import is_lock_busy
        from mast.skills.builtins._scan_readout import resolve_readout

        self._too_large = None
        readout = resolve_readout(ctx, [])
        channels = [int(c) for c in readout.channels]
        if not channels:
            logger.debug("scan-live: no acquired channels (%s)", readout.why)
            return None
        if too_large(readout.pixels, readout.lines):
            self._too_large = (int(readout.lines or 0), int(readout.pixels or 0))
            return None
        key = tuple(channels)
        cached = self._channel_cache.get(key)
        if cached is not None and cached in channels:
            if arr is not None and grabbed_index == cached:
                return cached, arr, rec, self._channel_name(rec)
            got_arr, got_rec, dur = self._grab(ctx, cached)
            if got_arr is not None:
                self._note_grab(dur)
                return cached, got_arr, got_rec, self._channel_name(got_rec)
            if is_lock_busy(got_rec):
                return None                     # someone else's turn; try again next tick
            # The cached channel no longer answers: probe again below.
        order = ([readout.z_index] if readout.z_index is not None else []) + channels
        seen: set[int] = set()
        first: tuple[int, np.ndarray, Any, str] | None = None
        for sig in order:
            if sig in seen or sig not in channels:
                continue
            seen.add(sig)
            if arr is not None and grabbed_index == sig:
                got_arr, got_rec = arr, rec
            else:
                got_arr, got_rec, dur = self._grab(ctx, sig)
                if got_arr is None:
                    continue
                self._note_grab(dur)
            name = self._channel_name(got_rec)
            hit = (sig, got_arr, got_rec, name)
            if first is None:
                first = hit
            if looks_like_z(name):
                self._channel_cache[key] = sig
                return hit
        if first is not None:
            # No Z among the acquired channels: show what there is, and say so.
            self._channel_cache[key] = first[0]
        return first

    def _open(self, ctx: _DataRole, now: float, *, arr: np.ndarray | None = None,
              rec: Any = None, grabbed_index: int | None = None, stale: bool = False
              ) -> tuple[_Session, np.ndarray] | None:
        """Start a session for the frame being acquired. Returns the session and
        the frame it is based on (the caller's grab is reused when it was of the
        chosen channel). ``stale``: the frame was MOVED, and this buffer may still
        be the old frame's (see :meth:`_fresh_data`)."""
        picked = self._pick_channel(ctx, arr, rec, grabbed_index)
        if picked is None:
            return None
        sig, arr, rec, name = picked
        geom = self._geometry(ctx)
        if geom is None:
            logger.debug("scan-live: scan frame geometry unreadable; not streaming")
            return None
        line_time = None
        speed = _floats(_body(ctx.safe_call("Scan_SpeedGet")), 4)
        if speed and speed[2] + speed[3] > 0:
            line_time = speed[2] + speed[3]
        key, unit, factor = value_scale(name)
        scan_dir = self._direction(rec)
        r0 = infer_row0(arr, scan_dir)
        rows, cols = arr.shape
        by, bx = bin_factors(rows, cols)
        ny, nx = math.ceil(rows / by), math.ceil(cols / bx)
        self._n += 1
        sess = _Session(
            n=self._n, stream_id=f"{FRAME_STREAM_PREFIX}{self._n}", signal_index=int(sig),
            src_rows=rows, src_cols=cols, by=by, bx=bx, ny=ny, nx=nx, scan_dir=scan_dir,
            row0=r0 or "top", row0_source="nan_front" if r0 else "assumed", geom=geom,
            line_time_s=line_time, key=key, factor=factor, channel=name,
            published=np.zeros(ny, dtype=bool), last_ok=now)
        if stale and rows > 0:
            sess.stale_edges = (arr[0].copy(), arr[-1].copy(),
                                int((~np.isnan(arr).all(axis=1)).sum()))
        self._declare(sess, unit=unit)
        self._session = sess
        return sess, arr

    def _declare(self, sess: _Session, *, unit: str | None = None) -> None:
        cx, cy, w, h, ang = sess.geom
        dx, dy = w / sess.nx, h / sess.ny
        if unit is None:
            unit = value_scale(sess.channel)[1]
        meta = {
            "nx": sess.nx, "ny": sess.ny, "x0": cx - w / 2 + dx / 2, "y0": cy - h / 2 + dy / 2,
            "dx": dx, "dy": dy, "x_unit": "m", "y_unit": "m",
            "center_x_m": cx, "center_y_m": cy, "width_m": w, "height_m": h, "angle_deg": ang,
            "scan_dir": sess.scan_dir, "row0": sess.row0, "row0_source": sess.row0_source,
            "channel": sess.channel, "signal_index": sess.signal_index, "value_key": sess.key,
            "units": {sess.key: unit}, "line_time_s": sess.line_time_s,
            "src_pixels": sess.src_cols, "src_lines": sess.src_rows, "bin": [sess.by, sess.bx],
        }
        self._live().declare(sess.stream_id, "map2d_rows",
                             title=tr(f"实时扫描 #{sess.n}", f"Live scan #{sess.n}"),
                             source=SOURCE, x_label="x", x_unit="m", y_label="y", y_unit="m",
                             meta=meta)

    def _end_session(self) -> None:
        """End the session's stream, and drop finished ones beyond the last
        :data:`KEEP_ENDED` — a continuous scan makes one per frame, and the hub
        would otherwise keep them until its budget forces evictions."""
        sess, self._session = self._session, None
        if sess is None:
            return
        live = self._live()
        live.end(sess.stream_id)
        self._ended.append(sess.stream_id)
        while len(self._ended) > KEEP_ENDED:
            live.drop(self._ended.popleft())

    def _finish(self) -> None:
        self._end_session()
        self._too_large = None
        self._status(active=0)

    # —— rows ——
    def _row_iy(self, sess: _Session, b: int) -> int:
        """Binned array row b → map2d row (iy = 0 at the bottom of the picture)."""
        return (sess.ny - 1 - b) if sess.row0 == "top" else b

    def _publish_rows(self, sess: _Session, arr: np.ndarray, *, final: bool) -> None:
        nan = np.isnan(arr)
        complete = ~nan.any(axis=1)
        acquired = ~nan.all(axis=1)
        live = self._live()
        t = self._wall()
        for b in range(sess.ny):
            if sess.published[b]:
                continue
            lo, hi = b * sess.by, min((b + 1) * sess.by, sess.src_rows)
            if complete[lo:hi].all():
                pass
            elif final and acquired[lo:hi].any():
                pass                            # the stopped scan's half row
            else:
                continue
            block = arr[lo:hi]
            values = (block[0] if sess.bx == 1 and block.shape[0] == 1
                      else binned_row(block, sess.bx, sess.nx))
            # ONE frame for the row: a dead pixel is a null (a hole, never a 0).
            row = [_round(v, sess.factor) if math.isfinite(v) else None for v in values.tolist()]
            live.publish(sess.stream_id, {"t": t, "iy": self._row_iy(sess, b), "ix0": 0,
                                          "values": {sess.key: row}})
            sess.published[b] = True

    # —— status stream ——
    def _status(self, *, active: int, session: _Session | None = None,
                interval: float | None = None, degraded: bool = False) -> None:
        values: dict[str, float] = {"active": float(active)}
        if session is not None:
            done = int(session.published.sum())
            values.update({
                "session": float(session.n), "rows_done": float(done), "rows": float(session.ny),
                "dir": 1.0 if session.scan_dir == "up" else -1.0,
                "interval_s": round(float(interval or 0.0), 3), "degraded": 1.0 if degraded else 0.0,
            })
        elif active and interval:
            values["interval_s"] = round(float(interval), 3)
        if active and self._too_large is not None:
            lines, pixels = self._too_large
            values.update({"too_large": 1.0, "src_lines": float(lines), "src_pixels": float(pixels)})
        if active and self._comms_hold:
            values["comms_hold"] = 1.0
        if values == self._last_status and active == 0:
            return                              # idle: say it once, not every half second
        live = self._live()
        if not self._status_declared:
            live.declare(STATUS_STREAM, "scalar",
                         title=tr("实时扫描状态", "Live scan status"), source=SOURCE,
                         meta={"frames_prefix": FRAME_STREAM_PREFIX})
            self._status_declared = True
        live.publish(STATUS_STREAM, {"t": self._wall(), "values": values})
        self._last_status = values

    def describe(self) -> dict:
        """For diagnostics: what the service is doing right now."""
        s = self._session
        return {
            "running": bool(self._thread is not None and self._thread.is_alive()),
            "session": s.n if s else None, "stream": s.stream_id if s else None,
            "rows_done": int(s.published.sum()) if s else 0, "rows": s.ny if s else 0,
            "failures": self._failures, "last_grab_s": self._last_grab_s,
            "max_grab_s": self._max_grab_s, "too_large": self._too_large,
            "comms_hold": self._comms_hold, "finishing": self._finishing_since is not None,
        }


# ── process singleton (same shape as monitoring.service) ───────────────────

_SERVICE: ScanLiveService | None = None
_SERVICE_LOCK = threading.Lock()


def start_service(app: Any) -> ScanLiveService | None:
    """Create (once) and start the daemon for a CoreRuntime. Never raises.
    The getters close over the runtime, so a reconnect that swaps the pool is
    picked up without re-wiring."""
    global _SERVICE
    try:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = ScanLiveService(
                    pool_getter=lambda: getattr(app, "_pool", None),
                    state_getter=lambda: getattr(app, "_state", None))
            svc = _SERVICE
        svc.start()
        return svc
    except Exception:  # noqa: BLE001 — a display feature must never break boot
        logger.warning("scan-live service did not start", exc_info=True)
        return None


def stop_service(join_timeout: float | None = None) -> None:
    """Stop the daemon if running, waiting for a call in flight as long as the
    slowest grab seen needs (:meth:`ScanLiveService.join_budget`). Never raises."""
    try:
        svc = _SERVICE
        if svc is not None:
            svc.stop(join_timeout=join_timeout)
    except Exception:  # noqa: BLE001
        logger.debug("scan-live stop failed (swallowed)", exc_info=True)


def get_service() -> ScanLiveService | None:
    return _SERVICE


def set_service_for_test(svc: ScanLiveService | None) -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = svc


__all__ = [
    "FRAME_STREAM_PREFIX", "KEEP_ENDED", "MAX_GRAB_BYTES", "STATUS_STREAM", "ScanLiveService",
    "bin_factors", "binned_row", "breaker_closed", "get_service", "infer_row0", "looks_like_z",
    "set_service_for_test", "start_service", "stop_service", "tick_interval", "too_large",
    "value_scale",
]
