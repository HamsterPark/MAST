"""The scan map's live scan stream (``mast/monitoring/scan_live.py``).

Design: ``docs/v2/design/scan_map_live.md``. What is pinned here:

* idle costs NO Nanonis call; every call is a read on the data role, with a
  short lock wait and outside the comms-health statistics — never main, never
  monitor;
* only newly completed rows are published, each exactly once, and they land
  the right way up for down- AND up-scans, whichever order the grabbed array
  comes in (the up-scan order is inferred from the NaN front — not verified on
  a real controller, which the stream's meta says);
* a stop grabs once more, publishes what is left and ends the stream;
* a failure of any kind skips one tick and never raises;
* the interval obeys every cap; the switch really switches it off;
* a new frame (front reset, geometry change) opens a new stream;
* ``Scan_FrameDataGrab`` gets the SIGNAL index ``Scan_BufferGet`` lists, never
  a position in that list, and Z is confirmed by the name the grab returns.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from mast.core.connection import LOCK_BUSY_PREFIX
from mast.core.types import NanonisCallRecord
from mast.live.testing import LiveRecorder, isolated_live_hub
from mast.monitoring import scan_live
from mast.monitoring.scan_live import (
    BYTES_PER_SAMPLE,
    DISABLED_POLL_S,
    FRAME_STREAM_PREFIX,
    IDLE_POLL_S,
    LOCK_WAIT_S,
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    STATUS_STREAM,
    ScanLiveService,
    bin_factors,
    binned_row,
    infer_row0,
    tick_interval,
    value_scale,
)

GEOM = (1e-7, 2e-7, 6e-8, 8e-8, 0.0)          # cx, cy, w, h (m), angle (deg)


def rec(body=None, error=""):
    return NanonisCallRecord(method="x", return_value=("", b"", body) if body is not None else None,
                             error=error)


class FakeScan:
    """A scan whose rows arrive as ``advance()`` is called.

    ``picture`` is the surface with its TOP row first. A down-scan acquires
    picture rows top to bottom, an up-scan bottom to top. The grabbed array is
    the picture itself (``row0="top"``) or upside down (``row0="bottom"``) —
    both orders are plausible for an up-scan, and the service must place the
    rows correctly either way. Unacquired rows are NaN, as Nanonis fills them.
    """

    def __init__(self, rows=8, cols=6, *, scan_dir="down", row0="top",
                 names=None, geom=GEOM, line_time=(0.1, 0.1), backfilled=False):
        self.rows, self.cols = rows, cols
        self.picture = (np.arange(rows * cols, dtype=float).reshape(rows, cols) + 1.0) * 1e-11
        self.done = 0
        self.running = True
        self.scan_dir, self.row0 = scan_dir, row0
        # signal index → channel name. The buffer lists 0 and 30; position 1
        # is NOT a channel index, and grabbing it is an error.
        self.names = names if names is not None else {0: "Current (A)", 30: "Z (m)"}
        self.geom = geom
        self.line_time = line_time
        self.backfilled = backfilled

    def advance(self, n=1):
        self.done = min(self.rows, self.done + n)

    def picture_rows_done(self):
        if self.scan_dir == "down":
            return list(range(self.done))
        return list(range(self.rows - self.done, self.rows))

    def frame(self, name):
        p = np.full((self.rows, self.cols), np.nan)
        src = self.picture if name.startswith("Z") else self.picture * 1e3
        if self.backfilled:
            p[:] = src                          # an instrument that never NaN-fills
        for r in self.picture_rows_done():
            p[r] = src[r]
        return p if self.row0 == "top" else p[::-1]


class FakePool:
    def __init__(self, scan: FakeScan, *, clock=None, grab_s=0.0):
        self.scan = scan
        self.calls: list[tuple] = []
        self.fail: set[str] = set()
        self.raise_on: set[str] = set()
        self.garbage: set[str] = set()
        self.busy = False
        self.clock = clock
        self.grab_s = grab_s

    def safe_call(self, method, *args, role="main", lock_timeout_s=None, count_health=True):
        self.calls.append((method, args, role, lock_timeout_s, count_health))
        s = self.scan
        if method in self.raise_on:
            raise RuntimeError("socket exploded")
        if self.busy:
            return rec(error=f"{LOCK_BUSY_PREFIX} role 'data' busy")
        if method in self.fail:
            return rec(error="Error: the controller said no")
        if method in self.garbage:
            return rec(["not", "what", "you", "expected"])
        if method == "Scan_StatusGet":
            return rec([1 if s.running else 0])
        if method == "Scan_BufferGet":
            chans = sorted(s.names)
            return rec([len(chans), [(c,) for c in chans], s.cols, s.rows])
        if method == "Signals_NamesGet":
            # The REAL shape: [size, count, [names…]] over the 128 signals.
            names = ["sig"] * 128
            for i, n in s.names.items():
                names[i] = n
            return rec([sum(len(n) for n in names), len(names), names])
        if method == "Scan_FrameGet":
            return rec(list(s.geom))
        if method == "Scan_SpeedGet":
            return rec([1e-7, 1e-7, s.line_time[0], s.line_time[1], 0, 1.0])
        if method == "Scan_FrameDataGrab":
            ch, _direction = args
            if self.clock is not None and self.grab_s:
                self.clock.advance(self.grab_s)
            name = s.names.get(int(ch))
            if name is None:
                return rec(error="Invalid channel index")
            data = s.frame(name).astype(np.float32)
            return rec([len(name), name, s.rows, s.cols, data, 1 if s.scan_dir == "up" else 0])
        return rec(error=f"unexpected {method}")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeState:
    def __init__(self, scan: FakeScan, running=True):
        self.scan = scan
        self.running = running

    def snapshot(self):
        cx, cy, w, h, a = self.scan.geom
        return SimpleNamespace(scan_running=self.running, scan_center_x_m=cx, scan_center_y_m=cy,
                               scan_width_m=w, scan_height_m=h, scan_angle_deg=a)


def thresholds(**over):
    base = {"scan_live_enabled": True, "cm_scan_live_max_hz": 2.0, "cm_scan_live_max_mbps": 2.0}
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def rig():
    """A service wired to fakes and an isolated, strict LiveHub."""
    with isolated_live_hub() as hub, LiveRecorder(hub) as recorder:
        scan = FakeScan()
        clock = Clock()
        pool = FakePool(scan, clock=clock)
        state = FakeState(scan)
        th = {"v": thresholds()}
        svc = ScanLiveService(pool_getter=lambda: pool, state_getter=lambda: state,
                              live_getter=lambda: hub, thresholds_getter=lambda: th["v"],
                              clock=clock, wall=lambda: 1.7e9)
        yield SimpleNamespace(svc=svc, scan=scan, pool=pool, state=state, clock=clock,
                              hub=hub, rec=recorder, th=th)


def tick(r, *, advance_rows=0):
    r.scan.advance(advance_rows)
    wait = r.svc.step(r.clock())
    r.clock.advance(wait)
    return wait


def grid(r, stream_id, ny, nx):
    """The picture assembled from the published ROW frames (iy = 0 at the BOTTOM)."""
    g = np.full((ny, nx), np.nan)
    for f in r.rec.frames(stream_id):
        row = next(iter(f["values"].values()))
        g[f["iy"], f["ix0"]:f["ix0"] + len(row)] = [np.nan if v is None else v for v in row]
    return g


def rows_of(r, stream_id):
    """iy of every published row frame, in publish order."""
    return [f["iy"] for f in r.rec.frames(stream_id)]


def frame_streams(r):
    return [s for s in r.rec.declared() if s.startswith(FRAME_STREAM_PREFIX)]


# ── idle ─────────────────────────────────────────────────────────────────────

def test_idle_makes_no_nanonis_calls(rig):
    rig.state.running = False
    for _ in range(5):
        assert tick(rig) == IDLE_POLL_S
    assert rig.pool.calls == [], "an idle map must not cost the controller a single call"


def test_the_switch_really_switches_it_off(rig):
    rig.th["v"] = thresholds(scan_live_enabled=False)
    assert tick(rig, advance_rows=3) == DISABLED_POLL_S
    assert rig.pool.calls == []


def test_switching_off_mid_scan_ends_the_stream(rig):
    tick(rig, advance_rows=2)
    sid = frame_streams(rig)[0]
    rig.th["v"] = thresholds(scan_live_enabled=False)
    tick(rig, advance_rows=1)
    assert sid in rig.rec.ended()


# ── the data role, and nothing else ──────────────────────────────────────────

def test_every_call_is_an_opportunistic_read_on_the_data_role(rig):
    for _ in range(rig.scan.rows + 2):
        tick(rig, advance_rows=1)
    rig.scan.running = False
    tick(rig)
    assert rig.pool.calls, "the test did not drive any call"
    for method, _args, role, lock_wait, health in rig.pool.calls:
        assert role == "data", f"{method} went out on {role!r}"
        assert lock_wait == LOCK_WAIT_S, f"{method} would queue behind a busy data role"
        assert health is False, f"{method} would reset other roles' failure streaks"
    writes = [m for m, *_ in rig.pool.calls if not m.endswith("Get") and m != "Scan_FrameDataGrab"]
    assert writes == [], f"a read-only display feature sent {writes}"


# ── rows ─────────────────────────────────────────────────────────────────────

def test_each_completed_row_is_published_exactly_once(rig):
    for _ in range(rig.scan.rows):
        tick(rig, advance_rows=1)
    sid = frame_streams(rig)[0]
    iys = rows_of(rig, sid)
    assert len(iys) == len(set(iys)), "a row was published twice"
    assert len(iys) == rig.scan.rows


def test_one_frame_per_row_on_a_row_stream(rig):
    """One frame per PIXEL costs ~390 B each in the process (1.5 GiB
    after 15 frames of 512²) and a 0.5 s full-GC pause. One frame per row."""
    tick(rig, advance_rows=2)
    sid = frame_streams(rig)[0]
    assert rig.hub.describe(sid)["kind"] == "map2d_rows"
    frames = rig.rec.frames(sid)
    assert len(frames) == 2
    for f in frames:
        assert set(f) == {"t", "iy", "ix0", "values"} and f["ix0"] == 0
        assert len(f["values"]["Z"]) == rig.scan.cols


def test_a_dead_pixel_goes_out_as_a_hole_not_a_zero(rig):
    original = rig.scan.frame

    def dead(name):
        a = original(name)
        a[0, 2] = np.inf                    # complete row, one unusable sample
        return a
    rig.scan.frame = dead
    tick(rig, advance_rows=1)
    row = rig.rec.frames(frame_streams(rig)[0])[0]["values"]["Z"]
    assert row[2] is None and all(v is not None for i, v in enumerate(row) if i != 2)


def test_a_half_acquired_row_waits_until_it_is_complete(rig):
    tick(rig, advance_rows=2)
    sid = frame_streams(rig)[0]
    n_before = len(rig.rec.frames(sid))
    # Row 3 half done: Nanonis NaN-fills the rest of it.
    rig.scan.advance(1)
    original = rig.scan.frame

    def half(name):
        a = original(name)
        a[2, rig.scan.cols // 2:] = np.nan
        return a
    rig.scan.frame = half
    rig.svc.step(rig.clock())
    assert len(rig.rec.frames(sid)) == n_before, "a half row was published as if complete"


@pytest.mark.parametrize("scan_dir,row0", [("down", "top"), ("up", "top"), ("up", "bottom"),
                                           ("down", "bottom")])
def test_rows_land_the_right_way_up(scan_dir, row0):
    """The picture must not land upside down on the map."""
    with isolated_live_hub() as hub, LiveRecorder(hub) as recorder:
        scan = FakeScan(scan_dir=scan_dir, row0=row0)
        clock = Clock()
        pool = FakePool(scan, clock=clock)
        svc = ScanLiveService(pool_getter=lambda: pool, state_getter=lambda: FakeState(scan),
                              live_getter=lambda: hub, thresholds_getter=thresholds,
                              clock=clock, wall=lambda: 1.7e9)
        r = SimpleNamespace(svc=svc, scan=scan, clock=clock, rec=recorder)
        for _ in range(scan.rows):
            tick(r, advance_rows=1)
        sid = frame_streams(r)[0]
        got = grid(r, sid, scan.rows, scan.cols)
        want = (scan.picture * 1e12)[::-1]        # iy = 0 is the BOTTOM row of the picture
        assert np.allclose(got, np.round(want, 2)), f"{scan_dir}/{row0}: picture lands wrong way up"
        meta = hub.describe(sid)["meta"]
        assert meta["row0_source"] == "nan_front", "the orientation was assumed, not inferred"
        assert meta["scan_dir"] == scan_dir


def test_a_buffer_with_no_unacquired_rows_is_not_painted_as_live():
    """The previous frame still in the buffer, or an instrument that backfills
    stale data: no NaN front, so nothing here is evidence of THIS scan."""
    with isolated_live_hub() as hub, LiveRecorder(hub) as recorder:
        scan = FakeScan(backfilled=True)
        clock = Clock()
        pool = FakePool(scan, clock=clock)
        svc = ScanLiveService(pool_getter=lambda: pool, state_getter=lambda: FakeState(scan),
                              live_getter=lambda: hub, thresholds_getter=thresholds,
                              clock=clock, wall=lambda: 1.7e9)
        r = SimpleNamespace(svc=svc, scan=scan, clock=clock, rec=recorder)
        for _ in range(3):
            tick(r, advance_rows=1)
        sid = frame_streams(r)[0]
        assert recorder.frames(sid) == []


def test_values_go_out_in_picometres(rig):
    tick(rig, advance_rows=1)
    sid = frame_streams(rig)[0]
    meta = rig.hub.describe(sid)["meta"]
    assert meta["units"] == {"Z": "pm"} and meta["value_key"] == "Z"
    f = rig.rec.frames(sid)[0]
    assert f["values"]["Z"][0] == pytest.approx(10.0, abs=0.01)   # 1e-11 m = 10 pm


# ── stop, new frames ─────────────────────────────────────────────────────────

def test_stop_publishes_the_rest_and_ends_the_stream(rig):
    tick(rig, advance_rows=3)
    sid = frame_streams(rig)[0]
    rig.scan.advance(2)                 # two more rows land between the last tick and the stop
    rig.scan.running = False
    tick(rig)
    assert len(set(rows_of(rig, sid))) == 5, "the rows acquired just before the stop were lost"
    assert sid in rig.rec.ended()
    status = rig.rec.frames(STATUS_STREAM)[-1]["values"]
    assert status["active"] == 0


def test_the_stop_publishes_a_half_row_too(rig):
    tick(rig, advance_rows=2)
    sid = frame_streams(rig)[0]
    rig.scan.advance(1)
    original = rig.scan.frame

    def half(name):
        a = original(name)
        a[2, 3:] = np.nan
        return a
    rig.scan.frame = half
    rig.scan.running = False
    tick(rig)
    iys = {f["iy"] for f in rig.rec.frames(sid)}
    assert rig.scan.rows - 1 - 2 in iys, "the stopped scan's half row is missing"


def test_the_next_frame_of_a_continuous_scan_gets_its_own_stream(rig):
    for _ in range(rig.scan.rows):
        tick(rig, advance_rows=1)
    first = frame_streams(rig)[0]
    rig.scan.done = 0                    # Nanonis starts the next frame: the front resets
    tick(rig, advance_rows=1)
    streams = frame_streams(rig)
    assert len(streams) == 2 and first in rig.rec.ended()
    assert len(rig.rec.frames(streams[1])) == 1


def test_a_moved_frame_gets_its_own_stream(rig):
    tick(rig, advance_rows=2)
    first = frame_streams(rig)[0]
    rig.scan.geom = (2e-7, 2e-7, 6e-8, 8e-8, 15.0)
    tick(rig, advance_rows=1)
    assert len(frame_streams(rig)) == 2 and first in rig.rec.ended()
    meta = rig.hub.describe(frame_streams(rig)[1])["meta"]
    assert meta["angle_deg"] == 15.0 and meta["center_x_m"] == 2e-7


# ── the channel: signal index, confirmed by name ─────────────────────────────

def test_grabs_use_the_signal_index_never_the_buffer_position(rig):
    """Scan_BufferGet lists (0, 30). Position 1 is not a channel index; the
    reply to a wrong index does not have the layout the parser expects."""
    for _ in range(3):
        tick(rig, advance_rows=1)
    grabbed = {int(args[0]) for m, args, *_ in rig.pool.calls if m == "Scan_FrameDataGrab"}
    assert grabbed <= {0, 30} and 30 in grabbed, f"grabbed {grabbed}"
    meta = rig.hub.describe(frame_streams(rig)[0])["meta"]
    assert meta["signal_index"] == 30 and meta["channel"] == "Z (m)"


def test_z_is_confirmed_by_the_name_the_grab_returns():
    """A third channel before Z in the buffer: a guess of "the first non-current
    channel" would stream Bias. The grab's own channel name decides."""
    with isolated_live_hub() as hub, LiveRecorder(hub) as recorder:
        scan = FakeScan(names={0: "Current (A)", 14: "Bias (V)", 30: "Z (m)"})
        clock = Clock()
        pool = FakePool(scan, clock=clock)
        svc = ScanLiveService(pool_getter=lambda: pool, state_getter=lambda: FakeState(scan),
                              live_getter=lambda: hub, thresholds_getter=thresholds,
                              clock=clock, wall=lambda: 1.7e9)
        r = SimpleNamespace(svc=svc, scan=scan, clock=clock, rec=recorder)
        tick(r, advance_rows=1)
        meta = hub.describe(frame_streams(r)[0])["meta"]
        assert meta["signal_index"] == 30 and meta["channel"] == "Z (m)"
        # …and the probing is not repeated every frame.
        n_grabs = sum(1 for m, *_ in pool.calls if m == "Scan_FrameDataGrab")
        for _ in range(3):
            tick(r, advance_rows=1)
        later = sum(1 for m, *_ in pool.calls if m == "Scan_FrameDataGrab") - n_grabs
        assert later == 3, f"{later} grabs for 3 ticks: the channel is being re-probed"


def test_without_z_it_streams_what_there_is():
    with isolated_live_hub() as hub, LiveRecorder(hub) as recorder:
        scan = FakeScan(names={0: "Current (A)"})
        clock = Clock()
        pool = FakePool(scan, clock=clock)
        svc = ScanLiveService(pool_getter=lambda: pool, state_getter=lambda: FakeState(scan),
                              live_getter=lambda: hub, thresholds_getter=thresholds,
                              clock=clock, wall=lambda: 1.7e9)
        r = SimpleNamespace(svc=svc, scan=scan, clock=clock, rec=recorder)
        tick(r, advance_rows=1)
        meta = hub.describe(frame_streams(r)[0])["meta"]
        assert meta["channel"] == "Current (A)" and meta["units"] == {"Current": "pA"}


# ── failures ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("how", ["raise", "error", "garbage", "busy"])
def test_a_failed_read_skips_one_tick_and_never_raises(rig, how):
    tick(rig, advance_rows=2)
    sid = frame_streams(rig)[0]
    n = len(rig.rec.frames(sid))
    if how == "raise":
        rig.pool.raise_on = {"Scan_FrameDataGrab", "Scan_StatusGet"}
    elif how == "error":
        rig.pool.fail = {"Scan_FrameDataGrab"}
    elif how == "garbage":
        rig.pool.garbage = {"Scan_FrameDataGrab", "Scan_StatusGet"}
    else:
        rig.pool.busy = True
    wait = tick(rig, advance_rows=1)          # must not raise
    assert wait > 0
    assert len(rig.rec.frames(sid)) == n, "something was published from a failed read"
    rig.pool.raise_on, rig.pool.fail, rig.pool.garbage, rig.pool.busy = set(), set(), set(), False
    tick(rig, advance_rows=0)
    assert len(rig.rec.frames(sid)) == n + 1, "the next tick did not catch up"


def test_a_busy_data_role_is_not_counted_as_a_failure(rig):
    tick(rig, advance_rows=1)
    rig.pool.busy = True
    assert tick(rig) == MIN_INTERVAL_S
    assert rig.svc.describe()["failures"] == 0


def test_a_session_whose_reads_keep_failing_is_ended(rig):
    tick(rig, advance_rows=1)
    sid = frame_streams(rig)[0]
    rig.pool.fail = {"Scan_FrameDataGrab"}
    for _ in range(20):
        tick(rig)
    assert sid in rig.rec.ended(), "a dead session kept claiming to be live"


def test_no_pool_is_quiet(rig):
    svc = ScanLiveService(pool_getter=lambda: None, state_getter=lambda: rig.state,
                          live_getter=lambda: rig.hub, thresholds_getter=thresholds,
                          clock=rig.clock)
    assert svc.step(rig.clock()) == DISABLED_POLL_S


# ── rate ─────────────────────────────────────────────────────────────────────

def _ti(**kw):
    base = dict(line_time_s=0.1, max_hz=100.0, max_bytes_per_s=1e12, frame_bytes=1, last_grab_s=0.0)
    base.update(kw)
    return tick_interval(**base)


def test_interval_floor_is_half_a_second():
    assert _ti() == (MIN_INTERVAL_S, False)


def test_interval_never_beats_one_scan_line():
    assert _ti(line_time_s=1.7) == (1.7, False)


def test_interval_obeys_max_hz():
    assert _ti(max_hz=0.5) == (2.0, False)


def test_interval_obeys_the_bandwidth_cap():
    frame = 1024 * 1024 * BYTES_PER_SAMPLE                 # 4 MiB
    iv, _ = _ti(frame_bytes=frame, max_bytes_per_s=2e6)
    assert iv == pytest.approx(frame / 2e6)


def test_interval_keeps_the_data_role_four_fifths_free():
    # the loop waits the interval AFTER the grab: 0.3 / (0.3 + 1.2) = 1/5
    assert _ti(last_grab_s=0.3) == (pytest.approx(1.2), False)


def test_interval_is_capped_and_says_so_when_a_cap_forced_it():
    iv, degraded = _ti(max_hz=0.01)
    assert iv == MAX_INTERVAL_S and degraded is True
    iv, degraded = _ti(line_time_s=60.0)                    # a slow scan is not degraded
    assert iv == MAX_INTERVAL_S and degraded is False


def test_the_cap_never_cuts_the_duty_or_bandwidth_floor():
    """Capping every term at 10 s would let a 5 s grab run every 10 s
    — a third of the data role instead of a fifth."""
    iv, degraded = _ti(last_grab_s=5.0)
    assert iv == pytest.approx(20.0) and degraded is True
    frame = 1024 * 1024 * BYTES_PER_SAMPLE
    iv, degraded = _ti(frame_bytes=frame, max_bytes_per_s=0.1e6)   # the knob's lowest setting
    assert iv == pytest.approx(frame / 0.1e6) and degraded is True


def test_interval_survives_nonsense():
    assert _ti(line_time_s=float("nan"))[0] == MIN_INTERVAL_S
    assert _ti(line_time_s=None, max_hz=0.0, max_bytes_per_s=0.0)[0] == MIN_INTERVAL_S


def test_a_slow_grab_slows_the_service_down():
    with isolated_live_hub() as hub:
        scan = FakeScan()
        clock = Clock()
        pool = FakePool(scan, clock=clock, grab_s=0.4)
        svc = ScanLiveService(pool_getter=lambda: pool, state_getter=lambda: FakeState(scan),
                              live_getter=lambda: hub, thresholds_getter=thresholds,
                              clock=clock, wall=lambda: 1.7e9)
        scan.advance(1)
        svc.step(clock())
        scan.advance(1)
        wait = svc.step(clock())
        assert wait == pytest.approx(1.6), "a 0.4 s grab must not be repeated every 0.5 s"


def test_the_configured_rate_reaches_the_interval(rig):
    rig.th["v"] = thresholds(cm_scan_live_max_hz=0.25)
    tick(rig, advance_rows=1)
    assert tick(rig, advance_rows=1) == pytest.approx(4.0)


# ── status stream ────────────────────────────────────────────────────────────

def test_status_reports_rows_and_direction(rig):
    tick(rig, advance_rows=3)
    v = rig.rec.frames(STATUS_STREAM)[-1]["values"]
    assert v["active"] == 1 and v["rows_done"] == 3 and v["rows"] == rig.scan.rows
    assert v["dir"] == -1 and v["session"] == 1


def test_idle_status_is_said_once(rig):
    rig.state.running = False
    for _ in range(4):
        tick(rig)
    assert len(rig.rec.frames(STATUS_STREAM)) == 1


# ── big frames ───────────────────────────────────────────────────────────────

def _big(rows, cols):
    hub_cm = isolated_live_hub()
    hub = hub_cm.__enter__()
    recorder = LiveRecorder(hub)
    scan = FakeScan(rows=rows, cols=cols)
    clock = Clock()
    pool = FakePool(scan, clock=clock)
    svc = ScanLiveService(pool_getter=lambda: pool, state_getter=lambda: FakeState(scan),
                          live_getter=lambda: hub, thresholds_getter=thresholds,
                          clock=clock, wall=lambda: 1.7e9)

    def close():
        recorder.close()
        hub_cm.__exit__(None, None, None)
    return SimpleNamespace(svc=svc, scan=scan, pool=pool, clock=clock, hub=hub, rec=recorder,
                           close=close)


def test_a_1024_frame_streams_binned_to_512():
    """1024² float32 is exactly 4 MiB — a common STM frame, and it must stream."""
    r = _big(1024, 1024)
    try:
        r.scan.advance(4)                                   # two binned rows (bin 2)
        r.svc.step(r.clock())
        sid = frame_streams(r)[0]
        meta = r.hub.describe(sid)["meta"]
        assert (meta["nx"], meta["ny"], meta["bin"]) == (512, 512, [2, 2])
        top = [f for f in r.rec.frames(sid) if f["iy"] == meta["ny"] - 1]
        assert len(top) == 1
        want = r.scan.picture[0:2, 0:2].mean() * 1e12
        assert top[0]["values"]["Z"][0] == pytest.approx(round(want, 2), abs=0.01)
        status = r.rec.frames(STATUS_STREAM)[-1]["values"]
        assert "too_large" not in status
    finally:
        r.close()


def test_a_2048_frame_is_not_grabbed_and_the_map_is_told_why():
    """More than 4 MiB a grab: the map keeps the 1/8 snapshots, and says so."""
    r = _big(2048, 2048)
    try:
        r.scan.advance(4)
        assert r.svc.step(r.clock()) == scan_live.RECHECK_S
        assert not [m for m, *_ in r.pool.calls if m == "Scan_FrameDataGrab"], "a 16 MiB frame was grabbed"
        assert frame_streams(r) == []
        status = r.rec.frames(STATUS_STREAM)[-1]["values"]
        assert status["active"] == 1 and status["too_large"] == 1
        assert (status["src_lines"], status["src_pixels"]) == (2048, 2048)
        assert r.svc.describe()["failures"] == 0, "a too-large frame is a state, not a failure"
    finally:
        r.close()


def test_too_large_is_strictly_more_than_4_mib():
    assert scan_live.too_large(1024, 1024) is False
    assert scan_live.too_large(1025, 1024) is True
    assert scan_live.too_large(2048, 2048) is True
    assert scan_live.too_large(None, 1024) is False         # unknown size: grab


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_bin_factors():
    assert bin_factors(256, 256) == (1, 1)
    assert bin_factors(512, 512) == (1, 1)
    assert bin_factors(1024, 513) == (2, 2)
    assert bin_factors(0, 0) == (1, 1)


def test_binned_row_ignores_nan_and_leaves_empty_cells_nan():
    block = np.array([[1.0, 3.0, np.nan, np.nan, 5.0]])
    out = binned_row(block, 2, 3)
    assert out[0] == 2.0 and math.isnan(out[1]) and out[2] == 5.0


def test_infer_row0_reads_the_nan_front():
    a = np.full((4, 2), np.nan)
    a[:2] = 1.0                                             # acquired block at the start
    assert infer_row0(a, "down") == "top"
    assert infer_row0(a, "up") == "bottom"
    b = a[::-1]
    assert infer_row0(b, "down") == "bottom"
    assert infer_row0(b, "up") == "top"
    assert infer_row0(np.ones((4, 2)), "down") is None       # no front, no evidence
    assert infer_row0(np.full((4, 2), np.nan), "down") is None


def test_value_scale():
    assert value_scale("Z (m)") == ("Z", "pm", 1e12)
    assert value_scale("Current (A)") == ("Current", "pA", 1e12)
    assert value_scale("LIY 1 omega (V)") == ("LIY 1 omega", "", 1.0)
    assert value_scale("") == ("value", "", 1.0)


# ── lifecycle ────────────────────────────────────────────────────────────────

def test_start_and_stop_service_never_raise():
    app = SimpleNamespace(_pool=None, _state=None)
    scan_live.set_service_for_test(None)
    try:
        svc = scan_live.start_service(app)
        assert svc is not None and svc.describe()["running"]
        assert scan_live.start_service(app) is svc, "a second start made a second service"
        scan_live.stop_service(join_timeout=2.0)
        assert not svc.describe()["running"]
    finally:
        scan_live.stop_service(join_timeout=2.0)
        scan_live.set_service_for_test(None)


def test_stop_service_without_a_service_is_a_noop():
    scan_live.set_service_for_test(None)
    scan_live.stop_service()


def test_started_again_while_stopping_the_worker_hands_over_itself():
    """No watcher thread: a worker that leaves while start() wanted it hands the
    slot to a new worker on its way out."""
    import time as _time
    with isolated_live_hub() as hub:
        svc = ScanLiveService(pool_getter=lambda: None, state_getter=lambda: None,
                              live_getter=lambda: hub, thresholds_getter=thresholds)
        svc.start()
        first = svc._thread
        assert first is not None and first.is_alive()
        svc.stop(join_timeout=0.0)          # do not wait: it may still be in its last tick
        svc.start()                         # the old one still owns the slot
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline and (first.is_alive() or svc._thread is first):
            _time.sleep(0.02)
        t = svc._thread
        try:
            assert not first.is_alive(), "the old worker never left"
            assert t is not None and t is not first and t.is_alive(), "nobody took over"
        finally:
            svc.stop(join_timeout=2.0)
        assert svc._thread is None or not svc._thread.is_alive()


def test_the_stop_waits_as_long_as_the_slowest_grab_needs():
    svc = ScanLiveService(pool_getter=lambda: None, state_getter=lambda: None)
    assert svc.join_budget() == scan_live.STOP_JOIN_MIN_S
    svc._max_grab_s = 4.0
    assert svc.join_budget() == pytest.approx(9.0)
    svc._max_grab_s = 60.0
    assert svc.join_budget() == scan_live.STOP_JOIN_MAX_S


# ── breaker gate, stop between calls, the last grab, old streams ──

class BreakerPool(FakePool):
    def __init__(self, scan, *, clock=None, state="closed"):
        super().__init__(scan, clock=clock)
        self.state = state

    def comms_snapshot(self):
        return {"state": self.state, "streak": 0}


@pytest.mark.parametrize("state", ["open", "half_open"])
def test_no_call_unless_the_breaker_is_closed(state):
    """comms_healthy() is True while HALF_OPEN, and a count_health=False call
    taken as the half-open probe never reports back: the breaker stays wedged
    for every role. Only CLOSED lets a call out."""
    with isolated_live_hub() as hub, LiveRecorder(hub) as recorder:
        scan = FakeScan()
        clock = Clock()
        pool = BreakerPool(scan, clock=clock, state=state)
        svc = ScanLiveService(pool_getter=lambda: pool, state_getter=lambda: FakeState(scan),
                              live_getter=lambda: hub, thresholds_getter=thresholds,
                              clock=clock, wall=lambda: 1.7e9)
        scan.advance(2)
        assert svc.step(clock()) == scan_live.RECHECK_S
        assert pool.calls == []
        assert recorder.frames(STATUS_STREAM)[-1]["values"]["comms_hold"] == 1
        pool.state = "closed"
        svc.step(clock())
        assert pool.calls, "it never resumed"
        assert "comms_hold" not in recorder.frames(STATUS_STREAM)[-1]["values"]


def test_once_stopping_no_further_call_is_started(rig):
    import threading
    tick(rig, advance_rows=1)
    n = len(rig.pool.calls)
    ev = threading.Event()
    ev.set()
    rig.svc._tick_stop = ev                 # as the worker's own stop event would be
    rig.svc.step(rig.clock())
    assert len(rig.pool.calls) == n, "a call went out after the stop"


def test_the_last_grab_is_retried_when_the_data_role_is_busy(rig):
    """The vision monitor takes its final grab exactly when a
    scan stops. Losing that race dropped the frame's last rows."""
    tick(rig, advance_rows=3)
    sid = frame_streams(rig)[0]
    rig.scan.advance(4)                     # four more rows land before the stop
    rig.scan.running = False
    original = rig.pool.safe_call
    busy = {"n": 3}

    def busy_grab(method, *a, **kw):
        if method == "Scan_FrameDataGrab" and busy["n"] > 0:
            busy["n"] -= 1
            rig.pool.calls.append((method, a, kw.get("role"), kw.get("lock_timeout_s"), kw.get("count_health")))
            return rec(error=f"{LOCK_BUSY_PREFIX} role 'data' busy")
        return original(method, *a, **kw)
    rig.pool.safe_call = busy_grab
    for _ in range(3):
        assert tick(rig) == scan_live.FINAL_RETRY_STEP_S
        assert sid not in rig.rec.ended(), "ended without the last rows"
    tick(rig)
    assert len(set(rows_of(rig, sid))) == 7 and sid in rig.rec.ended()


def test_the_last_grab_gives_up_after_five_seconds(rig):
    tick(rig, advance_rows=3)
    sid = frame_streams(rig)[0]
    rig.scan.running = False
    original = rig.pool.safe_call
    rig.pool.safe_call = lambda m, *a, **kw: (rec(error=f"{LOCK_BUSY_PREFIX} busy")
                                              if m == "Scan_FrameDataGrab" else original(m, *a, **kw))
    for _ in range(30):
        tick(rig)
        if sid in rig.rec.ended():
            break
    assert sid in rig.rec.ended(), "a busy data role kept the stream open forever"


def test_only_the_last_two_finished_frames_stay_in_the_hub(rig):
    for _frame in range(4):
        for _ in range(rig.scan.rows):
            tick(rig, advance_rows=1)
        rig.scan.done = 0                   # the next frame begins
    tick(rig, advance_rows=1)
    streams = frame_streams(rig)
    assert len(streams) == 5
    alive = [s for s in streams if rig.hub.describe(s) is not None]
    assert alive == streams[-3:], f"kept {alive}"   # 2 finished + the running one


def test_a_cached_channel_is_used_without_probing_again():
    """With no Z in the buffer every new frame would re-grab
    EVERY channel. A cached answer is used as is."""
    with isolated_live_hub() as hub, LiveRecorder(hub) as recorder:
        scan = FakeScan(names={0: "Current (A)", 14: "Bias (V)", 20: "LIY (V)"})
        clock = Clock()
        pool = FakePool(scan, clock=clock)
        svc = ScanLiveService(pool_getter=lambda: pool, state_getter=lambda: FakeState(scan),
                              live_getter=lambda: hub, thresholds_getter=thresholds,
                              clock=clock, wall=lambda: 1.7e9)
        r = SimpleNamespace(svc=svc, scan=scan, clock=clock, rec=recorder)
        tick(r, advance_rows=3)             # first frame: probes the channels once
        grabs = lambda: sum(1 for m, *_ in pool.calls if m == "Scan_FrameDataGrab")  # noqa: E731
        for _frame in range(3):
            tick(r, advance_rows=2)         # the frame goes on
            scan.done = 0                   # …and the next one begins: the front goes back
            n = grabs()
            tick(r, advance_rows=1)         # a new session: one grab, not one per channel
            assert grabs() - n == 1
        assert len(frame_streams(r)) == 4, "the test never opened a new session"


def test_z_is_recognised_by_the_vision_monitors_rule():
    for name in ("Z (m)", "Z [m]", "Z(m)", "z", "Height", "Topography (m)"):
        assert scan_live.looks_like_z(name), name
    for name in ("Bias (V)", "Current (A)", "LIY 1 omega (V)", "Zoom", ""):
        assert not scan_live.looks_like_z(name), name


def test_a_moved_frame_waits_for_its_own_rows():
    """When the frame moves, Nanonis restarts the scan — but the
    buffer still holds the OLD frame's rows until it does. Published, they
    would land in the new frame's place."""
    with isolated_live_hub() as hub, LiveRecorder(hub) as recorder:
        scan = FakeScan()
        clock = Clock()
        pool = FakePool(scan, clock=clock)
        state = FakeState(scan)
        svc = ScanLiveService(pool_getter=lambda: pool, state_getter=lambda: state,
                              live_getter=lambda: hub, thresholds_getter=thresholds,
                              clock=clock, wall=lambda: 1.7e9)
        r = SimpleNamespace(svc=svc, scan=scan, clock=clock, rec=recorder)
        tick(r, advance_rows=3)
        scan.geom = (2e-7, 2e-7, 6e-8, 8e-8, 15.0)         # moved; the buffer is still the old one
        tick(r, advance_rows=0)
        tick(r, advance_rows=0)
        new = frame_streams(r)[1]
        assert recorder.frames(new) == [], "the old frame's rows went out as the new frame's"
        scan.done = 0                       # Nanonis restarts: NaN-filled, then fresh rows
        scan.picture = scan.picture + 5e-11
        tick(r, advance_rows=1)
        assert len(recorder.frames(new)) == 1
