"""Archiving must never hold up a reading.

The failure mode: every sensor's history had gaps of 1–5 minutes, during field
ramps too, and a field-change check refused because the magnet temperature was
70 s old. The CSV rows of one monitor cycle, normally 4 ms apart, were 7–20 s
apart. The cause was the archive step, which ran inline on the reading thread:

* for every single reading the CSV sink and the history sink asked the runtime
  for the scope, and the runtime rebuilt the experiment folder each time (three
  SQLite connections and rewrites of experiment.json, sample.json, README.md),
  about seventeen times per 2 s cycle. Each took 4 ms normally and 10–20 s when
  the database or the disk was busy;
* the next read only started after all of that, so the latest readings, the
  alarms and the freshness checks waited too.

These tests pin the fix:
* archiving runs on its own thread;
* a queued cycle keeps what was true when it was read: time, scope, and whether
  the instrument was quiet;
* a cycle's rows go to the database in one call;
* stop() is bounded and leaves closed sinks alone;
* the runtime resolves the scope once per experiment/sample instead of per
  reading;
* the alarm callback does its stop actions before its database write.

Run from repo root:
    .venv-v2-py313/Scripts/python.exe -m pytest tests/v2/environment/test_env_archive_off_thread.py -q
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (see tests/v2/conftest.py) ──
import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import mast.environment.monitor as monitor_mod
from mast.core.types import SensorReading
from mast.environment.base import EnvironmentSensor
from mast.environment.monitor import EnvironmentMonitor


class _Counter(EnvironmentSensor):
    """Each read returns the next integer and records when it was read."""

    def __init__(self, name: str = "temperature", status: str = "ok"):
        self._name = name
        self.status = status
        self.n = 0
        self.read_times: list[float] = []

    def name(self):
        return self._name

    def read(self):
        self.n += 1
        self.read_times.append(time.time())
        return SensorReading(value=float(self.n), unit="K", status=self.status)


class _GateSink:
    """Writes block until released: a disk or a database that has stalled."""

    def __init__(self, delay_after_release: float = 0.0):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.delay = delay_after_release
        self.rows: list[tuple] = []

    def write(self, sensor, value, unit, status, *, ts=None):
        self.entered.set()
        self.release.wait(10.0)
        if self.delay:
            time.sleep(self.delay)
        self.rows.append((sensor, value, ts, time.time()))


def _wait_for(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _writers_alive(m) -> int:
    """This monitor's archive writers that are alive (other tests' writers excluded)."""
    return sum(1 for t in threading.enumerate()
               if t.name == m.archive_thread_name and t.is_alive())


# ── the reading thread never waits for the archive ─────────────────────────

def test_a_stalled_archive_does_not_stale_the_readings():
    sensor = _Counter()
    sink = _GateSink()
    m = EnvironmentMonitor([sensor], interval_s=0.02, sinks=[sink])
    m.start()
    try:
        assert sink.entered.wait(5.0)          # the writer is stuck in its first write
        first = m.get_latest()["temperature"].value
        # Readings go on while the writer is stuck: the latest value keeps moving.
        assert _wait_for(lambda: m.get_latest()["temperature"].value >= first + 5)
        assert m.archive_stats()["queued"] >= 4
    finally:
        released_at = time.time()
        sink.release.set()
        m.stop()
    # Every cycle that was read got archived, in order, stamped with its READ time:
    # the cycles read during the stall carry times from before the release (they
    # were written after it), and each stamp matches the moment its value was read.
    values = [v for _s, v, _ts, _w in sink.rows]
    assert values == [float(i) for i in range(1, sensor.n + 1)]
    assert sum(1 for _s, _v, ts, _w in sink.rows[1:] if ts < released_at) >= 4
    for _s, v, ts, _w in sink.rows:
        read_time = sensor.read_times[int(v) - 1]
        assert read_time <= ts < read_time + 0.5


def test_a_live_sink_stays_current_while_the_archive_is_stalled():
    """In-memory consumers (the environment-tag ring) are fed on the reading
    thread, so a stalled writer cannot make them stale."""
    class _Ring:
        def __init__(self):
            self.rows = []

        def write(self, sensor, value, unit, status, *, ts=None):
            self.rows.append((value, ts))

    sensor = _Counter()
    archive = _GateSink()
    ring = _Ring()
    m = EnvironmentMonitor([sensor], interval_s=0.02, sinks=[archive], live_sinks=[ring])
    m.start()
    try:
        assert archive.entered.wait(5.0)
        stuck_at = len(ring.rows)
        assert _wait_for(lambda: len(ring.rows) >= stuck_at + 5)
        assert len(archive.rows) == 0          # the archive is still stuck meanwhile
    finally:
        archive.release.set()
        m.stop()
    stamps = [ts for _v, ts in ring.rows]
    assert all(isinstance(t, float) for t in stamps) and stamps == sorted(stamps)


def test_the_archived_time_is_the_read_time_not_after_the_alarm_callbacks():
    """An alarm callback may take seconds (it stops a ramp, retracts the tip); the
    archived reading must still carry the moment it was read."""
    sensor = _Counter(status="alarm")
    seen = []

    class _Sink:
        def write(self, sensor_name, value, unit, status, *, ts=None):
            seen.append(ts)

    def slow_alarm(name, reading, prev):
        time.sleep(0.4)

    m = EnvironmentMonitor([sensor], interval_s=0.02, sinks=[_Sink()], on_alarm=slow_alarm)
    m.start()
    try:
        assert _wait_for(lambda: len(seen) >= 2)
    finally:
        m.stop()
    assert sensor.read_times[0] <= seen[0] < sensor.read_times[0] + 0.2


# ── stop() and restart ─────────────────────────────────────────────────────

def test_stop_writes_what_is_still_queued():
    sensor = _Counter()
    sink = _GateSink(delay_after_release=0.002)
    m = EnvironmentMonitor([sensor], interval_s=0.005, sinks=[sink])
    m.start()
    assert sink.entered.wait(5.0)
    assert _wait_for(lambda: m.archive_stats()["queued"] >= 25)  # ≥ 50 ms of writing left
    sink.release.set()
    m.stop()
    assert m.archive_stats()["queued"] == 0
    assert [v for _s, v, _ts, _w in sink.rows] == [float(i) for i in range(1, sensor.n + 1)]


def test_stop_does_not_hang_on_a_stuck_writer(caplog):
    """stop() is part of the service shutdown; a launcher that waits too long
    hard-kills the service. A writer stuck in a locked database must not hold it."""
    from mast.environment.monitor import ARCHIVE_DRAIN_S

    sink = _GateSink()
    m = EnvironmentMonitor([_Counter()], interval_s=0.01, sinks=[sink])
    m.start()
    try:
        assert sink.entered.wait(5.0)
        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="mast.environment.monitor"):
            m.stop()
        took = time.monotonic() - t0
    finally:
        sink.release.set()
    assert ARCHIVE_DRAIN_S <= 3.0
    assert took < ARCHIVE_DRAIN_S + 1.0        # the bounded drain, nothing more
    assert any("没有写完" in r.getMessage() for r in caplog.records)
    assert m.archive_stats()["abandoned"] is True


def test_an_abandoned_writer_leaves_the_closed_sinks_alone(monkeypatch):
    """After stop() gave up on the writer, the caller closes the sinks. The writer
    must not write the rest of the queue into them when it comes unstuck."""
    monkeypatch.setattr(monitor_mod, "ARCHIVE_DRAIN_S", 0.2)
    gate = _GateSink()
    later = []

    class _Recorder:
        def write(self, sensor, value, unit, status, *, ts=None):
            later.append((value, time.time()))

    m = EnvironmentMonitor([_Counter()], interval_s=0.01, sinks=[gate, _Recorder()])
    m.start()
    assert gate.entered.wait(5.0)
    assert _wait_for(lambda: m.archive_stats()["queued"] >= 3)
    m.stop()                                   # drain times out: the writer is abandoned
    stopped_at = time.time()
    gate.release.set()                         # ... and comes unstuck afterwards
    assert _wait_for(lambda: not m.archive_stats()["writer_alive"])
    assert [v for v, w in later if w >= stopped_at] == []
    assert m.archive_stats()["dropped"] >= 3


def test_a_restarted_monitor_keeps_archiving():
    """stop() then start() (the live rescan does this) must leave a writer running."""
    sensor = _Counter()
    seen: list[float] = []

    class _Sink:
        def write(self, sensor, value, unit, status, *, ts=None):
            seen.append(value)

    m = EnvironmentMonitor([sensor], interval_s=0.01, sinks=[_Sink()])
    for _ in range(3):
        m.start()
        before = len(seen)
        assert _wait_for(lambda: len(seen) >= before + 2)
        m.stop()
    assert m.archive_stats()["writer_alive"] is False
    assert seen == [float(i) for i in range(1, sensor.n + 1)]


def test_a_restart_takes_back_a_writer_that_is_still_busy(monkeypatch):
    """A rescan that restarts the monitor while the writer is stuck must reuse that
    writer, not start a second one beside it (two writers would feed the history
    sink concurrently), and nothing may be lost or written twice."""
    monkeypatch.setattr(monitor_mod, "ARCHIVE_DRAIN_S", 0.2)
    sensor = _Counter()
    sink = _GateSink()
    m = EnvironmentMonitor([sensor], interval_s=0.01, sinks=[sink])
    m.start()
    assert sink.entered.wait(5.0)
    m.stop()                                   # abandoned while stuck in the first write
    assert m.archive_stats()["writer_alive"] is True
    m.start()                                  # taken back into service
    try:
        assert m.archive_stats()["abandoned"] is False
        assert _writers_alive(m) == 1
        assert _wait_for(lambda: sensor.n >= 5)
        sink.release.set()
        assert _wait_for(lambda: m.wait_archived(0.05) and len(sink.rows) >= 5)
        assert _writers_alive(m) == 1
    finally:
        sink.release.set()
        m.stop()
    values = [v for _s, v, _ts, _w in sink.rows]
    assert values == sorted(set(values))       # in order, none twice
    assert values[0] == 1.0


def test_concurrent_start_and_stop_never_leave_the_readings_unarchived():
    sensor = _Counter()
    seen: list[float] = []

    class _Sink:
        def write(self, sensor, value, unit, status, *, ts=None):
            seen.append(value)

    m = EnvironmentMonitor([sensor], interval_s=0.005, sinks=[_Sink()])

    def churn(op):
        for _ in range(15):
            op()

    ts = [threading.Thread(target=churn, args=(m.start,)),
          threading.Thread(target=churn, args=(m.stop,))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    m.start()
    try:
        before = len(seen)
        assert _wait_for(lambda: len(seen) >= before + 3)
        assert _writers_alive(m) == 1
    finally:
        m.stop()


# ── the queue ──────────────────────────────────────────────────────────────

def test_the_queue_is_bounded_and_drops_the_oldest(caplog):
    m = EnvironmentMonitor([], archive_queue_cycles=3)
    base = time.time()
    with caplog.at_level(logging.WARNING, logger="mast.environment.monitor"):
        for i in range(10):
            m._enqueue_archive((base + i, {}, None, None))  # noqa: SLF001 — no writer running
    stats = m.archive_stats()
    assert stats["queued"] == 3 and stats["dropped"] == 7
    assert [item[0] for item in m._archive_q] == [base + 7, base + 8, base + 9]  # noqa: SLF001
    drops = [r for r in caplog.records if "丢弃最旧" in r.getMessage()]
    assert len(drops) == 1                     # said, and rate-limited


def test_a_stuck_writer_is_reported_from_the_reading_thread(caplog):
    """The lag warning on the writer side only fires once a cycle is finally
    written; a writer stuck for good must still show up in the log."""
    m = EnvironmentMonitor([])
    with caplog.at_level(logging.WARNING, logger="mast.environment.monitor"):
        m._enqueue_archive((time.time() - 120.0, {}, None, None))   # noqa: SLF001
        m._enqueue_archive((time.time(), {}, None, None))           # noqa: SLF001
    assert any("没有写完一轮" in r.getMessage() for r in caplog.records)


def test_a_writer_that_falls_behind_says_so_once(caplog):
    m = EnvironmentMonitor([])
    with caplog.at_level(logging.WARNING, logger="mast.environment.monitor"):
        m._write_archive((time.time() - 120.0, {}, None, None), 5)   # noqa: SLF001
        m._write_archive((time.time() - 125.0, {}, None, None), 6)   # noqa: SLF001
    lag = [r for r in caplog.records if "归档落后" in r.getMessage()]
    assert len(lag) == 1                                   # rate-limited
    assert m.archive_stats()["max_lag_s"] >= 120.0


def test_a_sink_without_the_new_keywords_still_receives_readings():
    class _Legacy:
        def __init__(self):
            self.rows = []

        def write(self, sensor, value, unit, status):
            self.rows.append((sensor, value, unit, status))

    legacy = _Legacy()
    m = EnvironmentMonitor([], sinks=[legacy])
    m._write_archive((time.time(), {"t": SensorReading(value=1.0, unit="K")},  # noqa: SLF001
                      None, None))
    assert legacy.rows == [("t", 1.0, "K", "ok")]


# ── one database call per cycle, at the read time ──────────────────────────

class _BatchStorage:
    def __init__(self):
        self.calls: list[tuple] = []

    def log_environment_many(self, rows, *, timestamp=None, experiment_id=None,
                             sample_id=None):
        self.calls.append((list(rows), timestamp, experiment_id, sample_id, time.time()))
        return len(rows)


def test_one_cycle_is_one_database_call_with_the_read_time():
    """With the first call held, later cycles queue up; each is still written in one
    call carrying its own read time, not the time it was written."""
    class _Held(_BatchStorage):
        def __init__(self):
            super().__init__()
            self.release = threading.Event()

        def log_environment_many(self, rows, **kw):
            self.release.wait(10.0)
            return super().log_environment_many(rows, **kw)

    a = _Counter("a")
    st = _Held()
    m = EnvironmentMonitor([a, _Counter("b"), _Counter("c")], storage=st,
                           interval_s=0.02, scope_provider=lambda: ("E1", "S1"))
    m.start()
    try:
        assert _wait_for(lambda: a.n >= 5)
        released_at = time.time()
        st.release.set()
        assert _wait_for(lambda: len(st.calls) >= 5)
    finally:
        st.release.set()
        m.stop()
    for k, (rows, ts, eid, sid, written_at) in enumerate(st.calls[:5]):
        assert sorted(r[0] for r in rows) == ["a", "b", "c"]
        assert (eid, sid) == ("E1", "S1")
        assert a.read_times[k] <= ts < a.read_times[k] + 0.5
    # The cycles read while the first call was held were written after the release
    # but carry their earlier read times.
    held = [ts for _r, ts, _e, _s, w in st.calls[1:5] if w >= released_at]
    assert held and all(ts < released_at for ts in held)


def test_a_database_failure_is_reported_and_archiving_goes_on(caplog):
    class _Locked(_BatchStorage):
        def log_environment_many(self, rows, **kw):
            super().log_environment_many(rows, **kw)
            raise sqlite3.OperationalError("database is locked")

    st = _Locked()
    seen = []

    class _Sink:
        def write(self, sensor, value, unit, status, *, ts=None):
            seen.append(value)

    m = EnvironmentMonitor([], storage=st, sinks=[_Sink()])
    with caplog.at_level(logging.WARNING, logger="mast.environment.monitor"):
        m._write_archive((time.time(), {"t": SensorReading(value=2.0, unit="K")},  # noqa: SLF001
                          None, None))
    assert seen == [2.0]                       # the sinks still got the cycle
    assert any("入库失败" in r.getMessage() for r in caplog.records)


def test_batched_rows_keep_the_text_timestamp_the_sweep_compares(tmp_path):
    from mast.logging.storage import ExperimentStorage

    st = ExperimentStorage(str(tmp_path / "exp.db"))
    assert st.log_environment_many([]) == 0
    old = time.time() - 20 * 86400
    assert st.log_environment_many([("vac", 1e-9, "mbar", "ok"),
                                    ("temp", 4.3, "K", "ok")],
                                   timestamp=old, experiment_id="E1", sample_id="S1") == 2
    st.log_environment_many([("vac", 2e-9, "mbar", "ok")], timestamp=time.time())
    rows = st.get_environment_history("vac")
    assert [r["value"] for r in rows] == [1e-9, 2e-9]
    assert rows[0]["timestamp"] == datetime.fromtimestamp(old).isoformat()
    assert (rows[0]["experiment_id"], rows[0]["sample_id"]) == ("E1", "S1")
    # Same text form as log_environment, so the retention sweep still finds the
    # old rows and leaves the new one.
    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    assert st.prune_environment_log(cutoff) == 2
    assert [r["value"] for r in st.get_environment_history("vac")] == [2e-9]


# ── the sinks file a late cycle where it was taken ─────────────────────────

def test_the_csv_row_carries_the_read_time_and_its_utc_day(tmp_path):
    from mast.environment.csv_sink import EnvironmentCsvSink

    exp = tmp_path / "exp"
    sink = EnvironmentCsvSink(lambda: (exp, None, "E1", None))
    ts = datetime(2026, 11, 12, 23, 59, 58, 500000, tzinfo=timezone.utc).timestamp()
    sink.write("magnet_field", 4.69, "T", "ok", ts=ts)
    sink.close()
    f = exp / "env" / "magnet_field_2026-11-12.csv"
    line = f.read_text(encoding="utf-8").splitlines()[1]
    assert line.startswith("2026-11-12T23:59:58.500+00:00,")
    assert line.split(",")[1] == f"{ts:.3f}"


def test_a_csv_row_queued_across_a_sample_switch_stays_with_its_sample(tmp_path):
    """Read in S1, written after the switch to S2: it belongs in the experiment file
    labelled S1 and must not land in S2's folder. Read in another experiment: not
    written at all (its row is in the database)."""
    from mast.environment.csv_sink import EnvironmentCsvSink

    exp = tmp_path / "exp"
    current = {"scope": (exp, "S02__b", "E1", "S2")}
    sink = EnvironmentCsvSink(lambda: current["scope"])
    sink.write("temperature", 4.3, "K", "ok", ts=time.time(), scope_ids=("E1", "S1"))
    sink.write("temperature", 4.4, "K", "ok", ts=time.time(), scope_ids=("E1", "S2"))
    sink.write("temperature", 9.9, "K", "ok", ts=time.time(), scope_ids=("E0", "S9"))
    sink.close()
    exp_rows = [ln.split(",") for f in (exp / "env").glob("temperature_*.csv")
                for ln in f.read_text(encoding="utf-8").splitlines()[1:]]
    assert [(r[3], r[7]) for r in exp_rows] == [("4.3", "S1"), ("4.4", "S2")]
    s2 = _csv_values(exp / "samples" / "S02__b" / "env" / "temperature.csv")
    assert s2 == ["4.4"]
    assert sink.stats()["rescoped"] == 2


def _csv_values(path):
    """The ``value`` column of an environment CSV. Compare values, never substrings of the
    whole text: the timestamps (``...:54.465``, ``1794799194.466``) contain digit runs like
    ``4.4`` often enough to fail a substring check about 2 % of the time."""
    import csv

    with open(path, encoding="utf-8", newline="") as fh:
        return [row["value"] for row in csv.DictReader(fh)]


def test_a_closed_csv_sink_does_not_reopen_its_files(tmp_path):
    from mast.environment.csv_sink import EnvironmentCsvSink

    exp = tmp_path / "exp"
    sink = EnvironmentCsvSink(lambda: (exp, None, "E1", None))
    sink.write("temperature", 4.3, "K", "ok")
    sink.close()
    sink.write("temperature", 4.4, "K", "ok")
    assert sink.stats()["open_files"] == 0
    assert _csv_values(next((exp / "env").glob("temperature_*.csv"))) == ["4.3"]


def _history_sink(tmp_path, state=None):
    from mast.envhistory.sink import EnvHistorySink
    from mast.envhistory.store import EnvHistoryStore
    from mast.envhistory.thresholds import EnvHistoryThresholds

    store = EnvHistoryStore(tmp_path / "eh.sqlite")
    th = EnvHistoryThresholds(eh_bucket_s=60.0)
    sink = EnvHistorySink(store_getter=lambda: store, thresholds_getter=lambda: th,
                          state_getter=lambda: state)
    sink.set_gated_sensors(["tunnel_current"])
    return sink, store


def test_history_buckets_follow_the_read_time(tmp_path):
    sink, store = _history_sink(tmp_path)
    t0 = 1_762_000_020.0                      # a minute boundary, long ago
    for ts, v in ((t0 + 5, 1.0), (t0 + 50, 3.0), (t0 + 65, 10.0)):
        sink.write("temperature", v, "K", "ok", ts=ts)
    sink.flush()
    try:
        pts = store.series("temperature")["points"]
        assert [(p["ts"], p["n"]) for p in pts] == [(t0, 2), (t0 + 60, 1)]
    finally:
        store.close()


class _Snap:
    def __init__(self, scanning):
        self.scan_running = scanning
        self.bias_v = 0.5
        self.setpoint_a = 2e-11
        self.z_pos_m = 1e-9
        self.z_controller_on = True
        self.stale = False


def test_the_quiet_gate_uses_what_was_true_when_the_current_was_read(tmp_path):
    """A tunnel current read during a scan is topography, not noise. If its cycle
    is written after the scan stopped, it must still be excluded (and a current
    read while quiet must still count even if a scan has started by then)."""
    state = SimpleNamespace(snap=_Snap(scanning=True))
    state.snapshot = lambda: state.snap
    sink, store = _history_sink(tmp_path, state)
    t0 = 1_762_000_020.0
    during_scan = sink.capture(t0 + 5)
    state.snap = _Snap(scanning=False)
    quiet = sink.capture(t0 + 7)
    state.snap = _Snap(scanning=True)          # now a scan runs while the queue drains
    sink.write("tunnel_current", 1e-10, "A", "ok", ts=t0 + 5, captured=during_scan)
    sink.write("tunnel_current", 2e-11, "A", "ok", ts=t0 + 7, captured=quiet)
    sink.flush()
    try:
        p = store.series("tunnel_current")["points"][0]
        assert (p["n"], p["n_excluded"]) == (1, 1)
        assert abs(p["mean"] - 2e-11) < 1e-15
    finally:
        store.close()


def test_history_rows_keep_the_scope_they_were_read_in(tmp_path):
    from mast.envhistory.sink import EnvHistorySink
    from mast.envhistory.store import EnvHistoryStore
    from mast.envhistory.thresholds import EnvHistoryThresholds

    store = EnvHistoryStore(tmp_path / "eh.sqlite")
    sink = EnvHistorySink(store_getter=lambda: store,
                          thresholds_getter=lambda: EnvHistoryThresholds(eh_bucket_s=60.0),
                          scope_provider=lambda: ("/exp", "S02", "E1", "S2"),
                          state_getter=lambda: None)
    seen = []
    orig = sink._feed                                           # noqa: SLF001

    def spy(sensor, ts, value, unit, status, bucket_s, *, admit, eid, sid):
        seen.append((sensor, eid, sid))
        return orig(sensor, ts, value, unit, status, bucket_s, admit=admit, eid=eid, sid=sid)

    sink._feed = spy                                            # noqa: SLF001
    sink.write("temperature", 4.3, "K", "ok", ts=1_762_000_025.0, scope_ids=("E1", "S1"))
    store.close()
    assert ("temperature", "E1", "S1") in seen


def test_a_late_cycle_does_not_trigger_the_due_checks(tmp_path):
    """on_tick decides whether to start a Z-noise burst from 'is the instrument
    quiet'. A cycle written minutes late must not answer that question."""
    from mast.envhistory.sink import EnvHistorySink
    from mast.envhistory.store import EnvHistoryStore
    from mast.envhistory.thresholds import EnvHistoryThresholds

    ticks = []
    store = EnvHistoryStore(tmp_path / "eh.sqlite")
    sink = EnvHistorySink(store_getter=lambda: store,
                          thresholds_getter=lambda: EnvHistoryThresholds(eh_bucket_s=60.0),
                          state_getter=lambda: None,
                          on_tick=lambda now, ctx, q: ticks.append(now))
    sink.write("temperature", 4.3, "K", "ok", ts=time.time() - 120.0)
    assert ticks == []
    sink.write("temperature", 4.3, "K", "ok", ts=time.time())
    assert len(ticks) == 1
    store.close()


def test_a_closed_history_sink_refuses_late_readings(tmp_path):
    """recorder.stop() closes the sink: a reading arriving afterwards must not
    replace the bucket that was just flushed with one covering half the minute."""
    sink, store = _history_sink(tmp_path)
    t0 = 1_762_000_020.0
    sink.write("temperature", 1.0, "K", "ok", ts=t0 + 5)
    sink.write("temperature", 3.0, "K", "ok", ts=t0 + 10)
    sink.close()
    sink.write("temperature", 50.0, "K", "ok", ts=t0 + 20)
    sink.flush()
    try:
        p = store.series("temperature")["points"][0]
        assert p["n"] == 2 and p["max"] == 3.0
    finally:
        store.close()


def test_the_monitor_hands_each_sink_what_it_captured_on_the_reading_thread():
    seen = {}

    class _Capturing:
        def capture(self, read_at):
            seen.setdefault("captured_on", threading.current_thread().name)
            return ("token", read_at)

        def write(self, sensor, value, unit, status, *, ts=None, scope_ids=None,
                  captured=None):
            seen.setdefault("written", (captured, ts, scope_ids))

    m = EnvironmentMonitor([_Counter()], interval_s=0.02, sinks=[_Capturing()],
                           scope_provider=lambda: ("E1", "S1"))
    m.start()
    try:
        assert _wait_for(lambda: "written" in seen)
    finally:
        m.stop()
    captured, ts, scope_ids = seen["written"]
    assert seen["captured_on"] == "env-monitor"
    assert captured == ("token", ts) and scope_ids == ("E1", "S1")


# ── the runtime resolves the scope once per experiment/sample ──────────────

class _Log:
    def __init__(self, eid, sid):
        self.current_experiment_id = eid
        self.current_sample_id = sid


def _scope_rt(log):
    from mast.core.runtime import CoreRuntime

    class _Rt:
        _ENV_SCOPE_TTL_S = CoreRuntime._ENV_SCOPE_TTL_S
        _ENV_SCOPE_RETRY_S = CoreRuntime._ENV_SCOPE_RETRY_S

        def __init__(self):
            self._experiment_log = log
            self.calls = 0
            self.fail = False

        def _ensure_scope_dirs(self):
            self.calls += 1
            if self.fail:
                raise sqlite3.OperationalError("database is locked")
            return ("EXPDIR", f"dir-{self._experiment_log.current_sample_id}")

        def scope(self):
            return CoreRuntime._env_csv_scope(self)        # noqa: SLF001

    return _Rt()


def test_the_scope_is_resolved_once_not_per_reading():
    rt = _scope_rt(_Log("E1", "S1"))
    for _ in range(40):                       # two sinks × many sensors × cycles
        assert rt.scope() == ("EXPDIR", "dir-S1", "E1", "S1")
    assert rt.calls == 1


def test_a_new_sample_or_an_expired_entry_resolves_again():
    log = _Log("E1", "S1")
    rt = _scope_rt(log)
    rt.scope()
    log.current_sample_id = "S2"
    assert rt.scope() == ("EXPDIR", "dir-S2", "E1", "S2")
    assert rt.calls == 2
    key, _expires, value = rt._env_scope_cache                 # noqa: SLF001
    rt._env_scope_cache = (key, 0.0, value)                    # noqa: SLF001 — TTL passed
    rt.scope()
    assert rt.calls == 3


def test_a_busy_database_keeps_the_last_scope_and_backs_off():
    rt = _scope_rt(_Log("E1", "S1"))
    good = rt.scope()
    key, _expires, value = rt._env_scope_cache                 # noqa: SLF001
    rt._env_scope_cache = (key, 0.0, value)                    # noqa: SLF001
    rt.fail = True
    assert rt.scope() == good                 # same folders as before; no exception
    assert rt.scope() == good
    assert rt.calls == 2                      # the second call did not retry at once


def test_no_experiment_means_no_folder_and_no_lookup():
    rt = _scope_rt(_Log(None, None))
    assert rt.scope() is None
    assert rt.calls == 0


def test_a_scope_switch_drops_the_cached_folders():
    from mast.core.runtime import CoreRuntime

    rt = _scope_rt(_Log("E1", "S1"))
    rt.scope()
    CoreRuntime._on_scope_change(rt, None, None)               # noqa: SLF001
    assert rt._env_scope_cache is None                         # noqa: SLF001
    calls = rt.calls
    rt.scope()
    assert rt.calls == calls + 1


# ── the alarm callback stops first, writes the database last ───────────────

def test_a_hard_alarm_stops_everything_before_its_database_write():
    """The record goes to SQLite (5 s busy timeout). The database can
    be busy for minutes at a time; the stop actions must not wait behind it."""
    from mast.core.runtime import CoreRuntime

    order: list[str] = []
    app = CoreRuntime.__new__(CoreRuntime)
    app._orch_abort = threading.Event()
    app._orch_abort_emergency = False
    app._orch_abort_why = ""
    app._orch_run_aborts = {}
    app._orch_run_aborts_lock = threading.Lock()
    app._executor = None
    app._env_alarm_log = []
    app._magnet_emergency_stop = lambda why: order.append("magnet stop")
    app._heater_emergency_off = lambda why: order.append("heater off")

    class _Pool:
        def safe_call(self, method, *args, **kw):
            order.append(method)
            return SimpleNamespace(error="", return_value=None)

    class _Storage:
        def log_environment(self, *a, **kw):
            order.append("database")

    app._pool = _Pool()
    app._storage = _Storage()
    app._on_env_alarm("pressure", SimpleNamespace(status="alarm", value=1e-3, unit="mbar"), "ok")
    assert app._orch_abort.is_set()
    assert order[-1] == "database"
    assert {"magnet stop", "heater off", "ZCtrl_Withdraw"} <= set(order[:-1])
