"""Long-lived SQLite connections + range-based environment_log pruning.

Background: on a busy spinning disk the
per-call open/close pattern made almost every close the LAST connection of a WAL
database (EXCLUSIVE checkpoint + fsync + delete of -wal/-shm), which stretched
past the 5 s busy timeout for every other connection, readers included; and the
hourly ``prune_environment_log`` full-scanned the table under the write lock.
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

import shutil
import threading
import time
from datetime import datetime, timedelta

import pytest

from mast.logging import db_keepalive
from mast.logging.storage import ExperimentStorage


@pytest.fixture()
def storage(tmp_path):
    s = ExperimentStorage(tmp_path / "exp.db")
    yield s
    db_keepalive.release_under(tmp_path)


def _ts(days_ago: float) -> str:
    return (datetime.now() - timedelta(days=days_ago)).isoformat()


def _seed(storage, rows):
    """rows: [(timestamp, status)] inserted in order (ids ascend)."""
    with storage._connect() as conn:                    # noqa: SLF001
        conn.executemany(
            "INSERT INTO environment_log"
            " (timestamp, sensor_name, value, unit, status) VALUES (?,?,?,?,?)",
            [(t, "temperature", 77.0, "K", st) for t, st in rows],
        )


def _count(storage) -> int:
    with storage._connect() as conn:                    # noqa: SLF001
        return conn.execute("SELECT COUNT(*) FROM environment_log").fetchone()[0]


# ── pruning ──────────────────────────────────────────────────────────────────

def test_prune_never_scans_the_table_and_commits_in_bounded_batches(storage):
    _seed(storage, [(_ts(30), "ok")] * 50 + [(_ts(1), "ok")] * 10)
    stmts: list[str] = []
    conn = storage._connect()                           # noqa: SLF001
    conn.set_trace_callback(stmts.append)
    try:
        deleted = storage.prune_environment_log(_ts(14), batch_rows=10,
                                                pause_s=0.0)
    finally:
        conn.set_trace_callback(None)
    assert deleted == 50 and _count(storage) == 10
    dels = [s for s in stmts if s.lstrip().upper().startswith("DELETE")]
    assert len(dels) == 5, dels                          # 50 rows / 10 per batch
    for s in dels:
        assert " IN (" not in s.upper().replace("NOT IN", ""), s
        plan = conn.execute("EXPLAIN QUERY PLAN " + s).fetchall()
        text = " ".join(str(tuple(r)) for r in plan)
        assert "SCAN" not in text, (s, text)            # SEARCH on the id range
    # one transaction per batch: every DELETE is followed by its own COMMIT
    assert sum(1 for s in stmts if s.strip().upper() == "COMMIT") >= len(dels)


def test_prune_keeps_alarm_rows_inside_the_old_range(storage):
    _seed(storage, [(_ts(30), "ok"), (_ts(30), "alarm"), (_ts(30), "error"),
                    (_ts(30), "ok"), (_ts(1), "ok")])
    assert storage.prune_environment_log(_ts(14), batch_rows=2, pause_s=0.0) == 2
    with storage._connect() as conn:                    # noqa: SLF001
        left = sorted(r[0] for r in conn.execute(
            "SELECT status FROM environment_log"))
    assert left == ["alarm", "error", "ok"]


def test_prune_never_deletes_a_newer_row_when_the_clock_stepped_back(storage):
    # ids ascend but timestamps do not: old, NEW, old, old.
    _seed(storage, [(_ts(30), "ok"), (_ts(0.5), "ok"), (_ts(30), "ok"),
                    (_ts(30), "ok")])
    storage.prune_environment_log(_ts(14), batch_rows=2, pause_s=0.0)
    with storage._connect() as conn:                    # noqa: SLF001
        stamps = [r[0] for r in conn.execute(
            "SELECT timestamp FROM environment_log")]
    assert any(t >= _ts(1) for t in stamps), "the recent row must survive"
    # and a later sweep (clock sane again) is allowed to finish the job
    assert _count(storage) <= 3


def test_prune_on_an_empty_table_and_with_nothing_due(storage):
    assert storage.prune_environment_log(_ts(14)) == 0
    _seed(storage, [(_ts(1), "ok")] * 5)
    assert storage.prune_environment_log(_ts(14)) == 0
    assert _count(storage) == 5


def test_prune_skips_the_gap_left_by_earlier_sweeps(storage):
    _seed(storage, [(_ts(30), "ok")] * 60 + [(_ts(1), "ok")] * 5)
    with storage._connect() as conn:                    # noqa: SLF001
        conn.execute("DELETE FROM environment_log WHERE id BETWEEN 1 AND 50")
    assert storage.prune_environment_log(_ts(14), batch_rows=5,
                                         pause_s=0.0) == 10
    assert _count(storage) == 5


# ── connections ──────────────────────────────────────────────────────────────

def test_same_thread_reuses_one_connection_and_threads_get_their_own(storage):
    a = storage._connect()                              # noqa: SLF001
    assert storage._connect() is a                      # noqa: SLF001
    other = ExperimentStorage(storage._db_path)         # noqa: SLF001
    assert other._connect() is a                        # noqa: SLF001  same file, same pool
    seen = []
    t = threading.Thread(target=lambda: seen.append(storage._connect()))  # noqa: SLF001
    t.start(); t.join()
    assert seen[0] is not a


def test_connections_of_ended_threads_are_reaped(storage):
    pool = db_keepalive.pool_for(storage._db_path, storage._open_connection)  # noqa: SLF001
    def work():
        storage._connect()                              # noqa: SLF001
    for _ in range(5):
        t = threading.Thread(target=work); t.start(); t.join()
    storage._connect()                                  # noqa: SLF001  triggers a reap
    t = threading.Thread(target=work); t.start(); t.join()
    # main + at most the last finished worker; not 5 leaked ones
    assert pool.open_count() <= 3


def test_synchronous_is_normal_on_every_connection(storage):
    assert storage._connect().execute("PRAGMA synchronous").fetchone()[0] == 1  # noqa: SLF001
    seen = []
    def work():
        seen.append(storage._connect().execute("PRAGMA synchronous").fetchone()[0])  # noqa: SLF001
    t = threading.Thread(target=work); t.start(); t.join()
    assert seen == [1]


def test_a_failed_transaction_rolls_back_and_leaves_the_shared_connection_clean(storage):
    with pytest.raises(RuntimeError):
        with storage._connect() as conn:                # noqa: SLF001
            conn.execute("INSERT INTO environment_log (timestamp, sensor_name,"
                         " value) VALUES ('x', 's', 1.0)")
            raise RuntimeError("boom")
    conn = storage._connect()                           # noqa: SLF001
    assert conn.in_transaction is False
    assert _count(storage) == 0


def test_wal_files_survive_a_call_so_no_close_is_the_last(tmp_path):
    from mast.logging.v2.storage import ExperimentStoreV2
    store = ExperimentStoreV2(tmp_path / "v2.db")
    try:
        for _ in range(3):
            with store.connect() as conn:
                conn.execute("SELECT COUNT(*) FROM schema_versions").fetchone()
            # Without a keeper the per-call close checkpoints and deletes these.
            assert (tmp_path / "v2.db-wal").exists()
            assert (tmp_path / "v2.db-shm").exists()
    finally:
        db_keepalive.release_under(tmp_path)


def test_release_lets_the_directory_be_deleted(tmp_path):
    d = tmp_path / "gone"
    s = ExperimentStorage(d / "exp.db")
    s.log_environment("t", 1.0, "K", "ok")
    db_keepalive.release_under(d)
    shutil.rmtree(d)
    assert not d.exists()


def test_a_deleted_and_recreated_file_is_not_served_from_a_dead_pool(tmp_path):
    p = tmp_path / "exp.db"
    s = ExperimentStorage(p)
    s.log_environment("t", 1.0, "K", "ok")
    db_keepalive.release_under(tmp_path)
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"exp.db{suffix}").unlink(missing_ok=True)
    s2 = ExperimentStorage(p)
    try:
        assert _count(s2) == 0
    finally:
        db_keepalive.release_under(tmp_path)


def test_envhistory_store_is_synchronous_normal(tmp_path):
    from mast.envhistory.store import EnvHistoryStore
    st = EnvHistoryStore(tmp_path / "eh.sqlite")
    try:
        assert st._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # noqa: SLF001
    finally:
        st.close()


# ── concurrent writers + readers + prune ─────────────────────────────────────

def test_concurrent_writers_readers_and_prune_neither_lock_nor_tear(storage):
    _seed(storage, [(_ts(30), "ok")] * 3000)
    batch = 6
    n_writers, per_writer = 3, 40
    errors: list[BaseException] = []
    torn: list[int] = []
    stop = threading.Event()

    def writer(wid: int):
        try:
            for i in range(per_writer):
                storage.log_environment_many(
                    [(f"s{k}", float(i), "K", "ok") for k in range(batch)],
                    timestamp=time.time(), experiment_id=f"w{wid}")
        except BaseException as exc:                    # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                with storage._connect() as conn:        # noqa: SLF001
                    c = conn.execute(
                        "SELECT COUNT(*) FROM environment_log"
                        " WHERE experiment_id LIKE 'w%'").fetchone()[0]
                if c % batch:
                    torn.append(c)                      # a half-committed cycle
                storage.get_environment_history("s0")
        except BaseException as exc:                    # noqa: BLE001
            errors.append(exc)

    def pruner():
        try:
            for _ in range(3):
                storage.prune_environment_log(_ts(14), batch_rows=200,
                                              pause_s=0.01)
        except BaseException as exc:                    # noqa: BLE001
            errors.append(exc)

    ws = [threading.Thread(target=writer, args=(i,)) for i in range(n_writers)]
    rs = [threading.Thread(target=reader) for _ in range(2)]
    ps = threading.Thread(target=pruner)
    t0 = time.monotonic()
    for t in (*rs, ps, *ws):
        t.start()
    for t in (*ws, ps):
        t.join(timeout=60)
    stop.set()
    for t in rs:
        t.join(timeout=10)
    assert not errors, errors
    assert not torn, torn
    assert time.monotonic() - t0 < 60
    with storage._connect() as conn:                    # noqa: SLF001
        written = conn.execute("SELECT COUNT(*) FROM environment_log"
                               " WHERE experiment_id LIKE 'w%'").fetchone()[0]
        left_old = conn.execute("SELECT COUNT(*) FROM environment_log"
                                " WHERE timestamp < ?", (_ts(14),)).fetchone()[0]
    assert written == n_writers * per_writer * batch
    assert left_old == 0


# ── recorder: first sweep is delayed ─────────────────────────────────────────

def test_first_sweep_waits_then_runs_once_per_interval():
    from mast.envhistory.recorder import EnvHistoryRecorder
    from mast.envhistory.thresholds import EnvHistoryThresholds

    class _St:
        def __init__(self):
            self.calls: list = []
        def prune_environment_log(self, cutoff, **kw):
            self.calls.append(cutoff)
            return 0

    st = _St()
    th = EnvHistoryThresholds(eh_sweep_interval_s=3600.0)
    r = EnvHistoryRecorder(thresholds_getter=lambda: th,
                           storage_getter=lambda: st,
                           first_sweep_delay_s=600.0)

    T0 = 1.0e9

    def tick(now):
        r.on_tick(now, {}, "quiet")
        t = r._sweep_thread                             # noqa: SLF001
        if t is not None:
            t.join(5)

    tick(T0)
    tick(T0 + 599.0)
    assert st.calls == [], "no sweep inside the start-up delay"
    tick(T0 + 601.0)
    assert len(st.calls) == 1
    tick(T0 + 700.0)
    assert len(st.calls) == 1                           # interval, not delay
    tick(T0 + 601.0 + 3601.0)
    assert len(st.calls) == 2


# ── keeper: failure backoff, no connect under the global lock ────────────────

def test_a_failed_keeper_is_not_retried_on_every_call(tmp_path, monkeypatch):
    import sqlite3 as _sq
    calls = []
    now = [100.0]

    def boom(*a, **k):
        calls.append(a)
        raise _sq.OperationalError("database is locked")

    monkeypatch.setattr(db_keepalive.sqlite3, "connect", boom)
    monkeypatch.setattr(db_keepalive, "_clock", lambda: now[0])
    p = tmp_path / "busy.db"
    try:
        for _ in range(6):
            assert db_keepalive.hold(p) is False
        assert len(calls) == 1, "every DB call must not retry a failing connect"
        now[0] += db_keepalive.FAIL_BACKOFF_S + 1
        assert db_keepalive.hold(p) is False
        assert len(calls) == 2, "retried once the backoff has passed"
    finally:
        db_keepalive.release_all()


def test_hold_does_not_hold_the_registry_lock_while_connecting(tmp_path, monkeypatch):
    real = db_keepalive.sqlite3.connect
    started, go = threading.Event(), threading.Event()

    def slow(*a, **k):
        started.set()
        assert go.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(db_keepalive.sqlite3, "connect", slow)
    p = tmp_path / "slow.db"
    t = threading.Thread(target=db_keepalive.hold, args=(p,))
    t.start()
    try:
        assert started.wait(5)
        probe = []
        q = threading.Thread(target=lambda: probe.append(
            db_keepalive.is_held(tmp_path / "other.db")))
        q.start(); q.join(2)
        assert not q.is_alive(), "is_held blocked behind a connect in progress"
        assert probe == [False]
    finally:
        go.set(); t.join(10)
        db_keepalive.release_all()


def test_two_threads_racing_to_hold_leave_one_keeper(tmp_path):
    p = tmp_path / "race.db"
    ts = [threading.Thread(target=db_keepalive.hold, args=(p,)) for _ in range(6)]
    for t in ts: t.start()
    for t in ts: t.join()
    try:
        assert db_keepalive.is_held(p)
        assert len([k for k in db_keepalive._keepers  # noqa: SLF001
                    if k == db_keepalive._key(p)]) == 1  # noqa: SLF001
    finally:
        db_keepalive.release_all()


# ── structure: no nested _connect() on one thread ────────────────────────────

def test_no_storage_method_nests_a_connect_inside_a_connect_block():
    """The pooled connection is shared per thread: an inner ``with
    self._connect()`` would COMMIT the outer transaction on exit.  No method may
    call (transitively) a method that opens a connection from inside a
    ``with self._connect()`` block."""
    import ast
    import mast.logging.storage as mod
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "ExperimentStorage")
    meths = {f.name: f for f in cls.body if isinstance(f, ast.FunctionDef)}

    def self_calls(node):
        return {c.func.attr for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and isinstance(c.func.value, ast.Name) and c.func.value.id == "self"}

    opens = {n for n, f in meths.items() if "_connect" in self_calls(f)}
    changed = True
    while changed:                                   # transitive closure
        changed = False
        for n, f in meths.items():
            if n not in opens and self_calls(f) & opens:
                opens.add(n); changed = True
    assert len(opens) > 40, "analysis found too few connection users"
    bad = []
    for name, f in meths.items():
        for w in ast.walk(f):
            if not isinstance(w, ast.With):
                continue
            if not any(isinstance(i.context_expr, ast.Call)
                       and getattr(i.context_expr.func, "attr", "") == "_connect"
                       for i in w.items):
                continue
            inner = set()
            for stmt in w.body:
                inner |= self_calls(stmt)
            hit = inner & (opens | {"_connect"})
            if hit:
                bad.append((name, sorted(hit)))
    assert not bad, bad
