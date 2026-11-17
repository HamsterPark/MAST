"""The retention sweep must not hold the store lock while it scans.

A long-running monitor DB can reach several GB (millions of segment rows, each with
an envelope blob). The sweep used to run three whole-table queries under the one
lock the acquisition thread commits through: a multi-second stall every sweep, with
capture lost meanwhile (the oscilloscope buffer is overwritten after a few seconds).

What these tests pin is the *structure* that makes the stall impossible, not a
wall-clock number (which would be flaky): no scan statement ever runs on the
shared connection, and the shared connection is only touched in short, bounded,
single-commit claim batches.
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

import threading
import time

import pytest

from mast.monitoring import store as store_mod
from mast.monitoring.store import CurrentMonitorStore

FILE_BYTES = 1000
GB = 1 << 30


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_SWEEP_YIELD_S", 0.0)
    s = CurrentMonitorStore(tmp_path / "m.sqlite", tmp_path)
    yield s
    s.close()


def _seed(store, i, *, t_start, pinned=False, with_file=True):
    p = None
    if with_file:
        p = store.data_dir / "segments" / f"seg_{i}.npy"
        p.write_bytes(b"x" * FILE_BYTES)
    return store.add_segment(
        {"t_start": t_start, "t_end": t_start + 1.0, "fs_hz": 2000.0, "n_samples": 2000,
         "npy_path": str(p) if p else None, "npy_bytes": FILE_BYTES if p else 0,
         "pinned": pinned},
        b"\0" * 64, 0.01)


def _present(store):
    with store._lock:                                         # noqa: SLF001
        return {r["id"] for r in store._conn.execute(         # noqa: SLF001
            "SELECT id FROM segments WHERE npy_path IS NOT NULL")}


class _OwnedLock:
    """threading.Lock that knows who holds it and for how many statements."""

    def __init__(self):
        self._l = threading.Lock()
        self.owner = None
        self.holds = []                  # statements executed in each hold
        self._n = 0

    def __enter__(self):
        self._l.acquire()
        self.owner = threading.get_ident()
        self._n = 0
        return self

    def __exit__(self, *a):
        self.holds.append(self._n)
        self.owner = None
        self._l.release()

    def held_here(self):
        return self.owner == threading.get_ident()

    def stmt(self):
        self._n += 1


class _SpyConn:
    """Delegates to the real connection and records what ran under the lock."""

    def __init__(self, real, lock):
        self._real, self._lock = real, lock
        self.executed = []               # (sql, held_the_lock)
        self.commits = 0

    def execute(self, sql, *a, **k):
        held = self._lock.held_here()
        if held:
            self._lock.stmt()
        self.executed.append((sql, held))
        return self._real.execute(sql, *a, **k)

    def commit(self):
        self.commits += 1
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _spy(store):
    lock = _OwnedLock()
    store._lock = lock                                        # noqa: SLF001
    spy = _SpyConn(store._conn, lock)                         # noqa: SLF001
    store._conn = spy                                         # noqa: SLF001
    return lock, spy


def test_the_scan_never_runs_on_the_shared_connection_and_claims_are_bounded(store, monkeypatch):
    monkeypatch.setattr(store_mod, "_SWEEP_BATCH", 5)
    old = time.time() - 72 * 3600
    for i in range(23):
        _seed(store, i, t_start=old + i)
    lock, spy = _spy(store)

    res = store.retention_sweep(keep_hours=24.0, keep_gb=100.0)

    assert res["removed"] == 23
    selects = [sql for sql, _ in spy.executed if sql.lstrip().upper().startswith("SELECT")]
    assert selects == [], f"scan statements ran on the shared connection: {selects[:2]}"
    assert all(held for _, held in spy.executed), "a write ran without the lock"
    assert lock.holds, "the sweep never took the lock to claim anything"
    assert max(lock.holds) <= 5, f"a lock hold covered {max(lock.holds)} statements (batch is 5)"
    assert len(lock.holds) == 5 == spy.commits, "claims must be one transaction per batch"


def test_age_pass_across_many_batches_keeps_pinned_and_fresh(store, monkeypatch):
    monkeypatch.setattr(store_mod, "_SWEEP_BATCH", 4)
    now = time.time()
    stale = [_seed(store, i, t_start=now - 72 * 3600 + i) for i in range(12)]
    pinned_old = [_seed(store, 100 + i, t_start=now - 72 * 3600 + i, pinned=True) for i in range(2)]
    fresh = [_seed(store, 200 + i, t_start=now - 60 + i) for i in range(3)]

    res = store.retention_sweep(keep_hours=24.0, keep_gb=100.0)

    assert res["removed"] == 12 and res["freed_bytes"] == 12 * FILE_BYTES
    assert res["pinned_bytes"] == 2 * FILE_BYTES and res["over_budget"] is False
    assert _present(store) == set(pinned_old) | set(fresh)
    assert not set(stale) & _present(store)


def test_budget_pass_is_oldest_first_across_batches_and_keeps_ties(store, monkeypatch):
    monkeypatch.setattr(store_mod, "_SWEEP_BATCH", 3)
    now = time.time() - 600
    # 11 unpinned, all fresh (age rule does not apply). Rows 2,3,4 share t_start and
    # straddle the first batch edge (batch = 3): a keyset on t_start alone would skip
    # the ties that come after the edge.
    t = [now + x for x in (0, 1, 3, 3, 3, 6, 7, 8, 9, 10, 11)]
    ids = [_seed(store, i, t_start=ts) for i, ts in enumerate(t)]
    keep = 4

    res = store.retention_sweep(keep_hours=24.0, keep_gb=(keep * FILE_BYTES + 1) / GB)

    assert res["removed"] == len(ids) - keep and res["over_budget"] is False
    assert _present(store) == set(ids[-keep:]), "budget pass did not delete oldest-first, exactly"


def test_over_budget_when_pinned_alone_exceeds_it(store, monkeypatch):
    monkeypatch.setattr(store_mod, "_SWEEP_BATCH", 3)
    now = time.time() - 600
    pins = [_seed(store, i, t_start=now + i, pinned=True) for i in range(5)]
    loose = [_seed(store, 50 + i, t_start=now + 10 + i) for i in range(5)]

    res = store.retention_sweep(keep_hours=24.0, keep_gb=(2 * FILE_BYTES) / GB)

    assert res["over_budget"] is True and res["pinned_bytes"] == 5 * FILE_BYTES
    assert _present(store) == set(pins), "pinned segments must survive, all loose ones go"
    assert not set(loose) & _present(store)


def test_a_segment_pinned_between_scan_and_claim_survives_a_batch(store):
    old = time.time() - 72 * 3600
    ids = [_seed(store, i, t_start=old + i) for i in range(6)]
    with store._lock:                                         # noqa: SLF001
        rows = store._conn.execute(                           # noqa: SLF001
            "SELECT id, npy_path, npy_bytes FROM segments").fetchall()
    store.set_pin(ids[3], True, "labeled:good")               # lands after the scan

    removed, freed = store._drop_npy_batch(rows)              # noqa: SLF001

    assert removed == 5 and freed == 5 * FILE_BYTES
    assert _present(store) == {ids[3]}
    assert Path(store.segment_meta(ids[3])["npy_path"]).is_file()


def test_one_unremovable_file_restores_only_its_own_row(store, monkeypatch):
    old = time.time() - 72 * 3600
    ids = [_seed(store, i, t_start=old + i) for i in range(4)]
    real_remove = store_mod.os.remove
    victim = store.segment_meta(ids[1])["npy_path"]

    def fake(p):
        if str(p) == victim:
            raise PermissionError("mapped")
        return real_remove(p)

    monkeypatch.setattr(store_mod.os, "remove", fake)
    res = store.retention_sweep(keep_hours=24.0, keep_gb=100.0)

    assert res["removed"] == 3 and res["freed_bytes"] == 3 * FILE_BYTES
    assert _present(store) == {ids[1]}
    assert Path(victim).is_file()


def test_sweep_still_works_when_the_scan_connection_cannot_be_opened(store, monkeypatch):
    monkeypatch.setattr(store_mod, "_SWEEP_BATCH", 2)
    old = time.time() - 72 * 3600
    for i in range(5):
        _seed(store, i, t_start=old + i)
    monkeypatch.setattr(store, "_open_scan_conn", lambda: None)

    assert store.retention_sweep(keep_hours=24.0, keep_gb=100.0)["removed"] == 5
    assert _present(store) == set()


def test_a_concurrent_writer_is_not_lost_while_the_sweep_runs(store, monkeypatch):
    monkeypatch.setattr(store_mod, "_SWEEP_BATCH", 10)
    old = time.time() - 72 * 3600
    for i in range(300):
        _seed(store, i, t_start=old + i)
    written = []
    stop = threading.Event()

    def writer():
        i = 1000
        while not stop.is_set():
            written.append(_seed(store, i, t_start=time.time(), with_file=False))
            i += 1

    th = threading.Thread(target=writer)
    th.start()
    try:
        assert store.retention_sweep(keep_hours=24.0, keep_gb=100.0)["removed"] == 300
    finally:
        stop.set()
        th.join()
    assert written and None not in written
    with store._lock:                                         # noqa: SLF001
        n = store._conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    assert n == 300 + len(written)


def test_wal_with_normal_synchronous(store):
    """FULL fsyncs every one of the acquisition thread's ~7 commits/s."""
    with store._lock:                                         # noqa: SLF001
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"  # noqa: SLF001
        assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 1               # noqa: SLF001
