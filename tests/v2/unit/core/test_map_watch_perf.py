"""The map activity watcher stays off the big trees and off the instrument lock.

Observed on a long-running station (hundreds of thousands of files under ``experiments/``, a spinning disk):

* the watcher walked ``<data_root>/experiments`` and ``working-sessions`` as well as the Nanonis
  session dir, ~10 s of CPU per 6 s cycle; the archive copies it had just made were walked again
  and gave a second "manual" marker;
* ``_resolve_session_dir`` waited 30 s on the ``'main'`` role while a spectroscopy sweep held it
  (one ERROR line per refusal, API callers queued behind it) and cached the failure as ``None``;
* the archive tick re-laid the scope's directories and manifests (3 DB connections and ~5 file
  writes) every cycle.
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (canonical block for tests/v2/) ──
import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

import os
import threading
import time
from types import SimpleNamespace

import pytest

from mast.core.runtime import CoreRuntime

_DAT = "Experiment\tbias spectroscopy\n\n[DATA]\nBias (V)\tCurrent (A)\n0\t0\n"


class _Pool:
    """Answers ``Util_SessionPathGet``; records the lock wait the caller asked for."""

    def __init__(self, path: str | None, *, busy: bool = False, gate: threading.Event | None = None):
        self.path, self.busy, self.gate = path, busy, gate
        self.calls: list[dict] = []

    def safe_call(self, method, *args, role="main", lock_timeout_s=None, **kw):
        assert method == "Util_SessionPathGet"
        self.calls.append({"role": role, "lock_timeout_s": lock_timeout_s})
        if self.gate is not None:
            self.gate.wait(10)
        if self.busy:
            return SimpleNamespace(error="RoleBusy: role 'main' busy", return_value=None)
        return SimpleNamespace(error=None, return_value=("", b"", [len(self.path), self.path]))


def _rt(pool, **kw) -> SimpleNamespace:
    return SimpleNamespace(_pool=pool, _session_dir_cache=None, **kw)


@pytest.fixture(autouse=True)
def _no_process_registry(monkeypatch):
    import mast.core.scan_registry as sr

    monkeypatch.setattr(sr, "record_session_dir", lambda d: None)


# ── 1. the watcher's roots ─────────────────────────────────────────────────────────────────
def test_the_watcher_walks_the_session_dir_and_nothing_else(tmp_path, monkeypatch):
    sess = tmp_path / "nanonis"
    sess.mkdir()
    proj = tmp_path / "data"
    for sub in ("experiments", "working-sessions"):
        (proj / sub).mkdir(parents=True)
    import mast._runtime_paths as rp

    monkeypatch.setattr(rp, "project_root", lambda: proj)
    stub = SimpleNamespace(_resolve_session_dir=lambda: str(sess))
    assert CoreRuntime._current_scan_dirs(stub) == (str(sess),)
    stub = SimpleNamespace(_resolve_session_dir=lambda: None)
    assert CoreRuntime._current_scan_dirs(stub) == ()


def test_the_archive_copy_under_experiments_is_not_walked_again(tmp_path, monkeypatch):
    """A manual spectrum is ingested into ``<exp>/samples/S01/raw/dat``. With the experiments
    tree in the watch set the spectrum tick saw that copy as a new file and logged a second
    manual marker."""
    sess = tmp_path / "nanonis"
    proj = tmp_path / "data"
    copy = proj / "experiments" / "E1" / "samples" / "S01" / "raw" / "dat" / "a.dat"
    for d in (sess, copy.parent):
        d.mkdir(parents=True, exist_ok=True)
    for p in (sess / "a.dat", copy):
        p.write_text(_DAT, encoding="utf-8")
        old = time.time() - 600
        os.utime(p, (old, old))
    import mast._runtime_paths as rp

    monkeypatch.setattr(rp, "project_root", lambda: proj)
    logged: list = []
    stub = SimpleNamespace(
        _storage=SimpleNamespace(log_marker=lambda **kw: logged.append(kw)),
        _experiment_log=SimpleNamespace(current_experiment_id="E1", current_sample_id="S1"),
        _map_last_skill_ts=0.0,
        _scan_search_dirs=(),
    )
    stub._resolve_session_dir = lambda: str(sess)
    stub._current_scan_dirs = lambda: CoreRuntime._current_scan_dirs(stub)
    seen: dict = {}
    CoreRuntime._map_spectrum_tick(stub, seen)
    assert set(seen) == {str(sess / "a.dat")}, "only the session dir's file is looked at"


# ── 2. _resolve_session_dir ────────────────────────────────────────────────────────────────
def test_the_lock_wait_is_short_and_opportunistic(tmp_path):
    pool = _Pool(str(tmp_path))
    rt = _rt(pool)
    assert CoreRuntime._resolve_session_dir(rt) == str(tmp_path)
    (call,) = pool.calls
    assert call["role"] == "main"
    from mast.core.connection import _OPPORTUNISTIC_LOCK_TIMEOUT_S

    assert 0 < call["lock_timeout_s"] <= 0.5 < _OPPORTUNISTIC_LOCK_TIMEOUT_S


def test_the_dir_is_reused_for_a_minute(tmp_path):
    pool = _Pool(str(tmp_path))
    rt = _rt(pool)
    CoreRuntime._resolve_session_dir(rt)
    rt._session_dir_cache = (str(tmp_path), time.monotonic() - 45.0)       # 45 s old: still good
    assert CoreRuntime._resolve_session_dir(rt) == str(tmp_path)
    assert len(pool.calls) == 1
    rt._session_dir_cache = (str(tmp_path), time.monotonic() - 61.0)
    CoreRuntime._resolve_session_dir(rt)
    assert len(pool.calls) == 2


def test_a_refused_ask_keeps_the_last_good_dir_and_is_not_repeated_at_once(tmp_path):
    good = str(tmp_path)
    pool = _Pool(good, busy=True)
    rt = _rt(pool, _session_dir=good, _session_path=good)
    rt._session_dir_cache = (good, time.monotonic() - 61.0)                # expired
    assert CoreRuntime._resolve_session_dir(rt) == good, "a refusal must not null the dir"
    assert rt._session_dir == good and rt._session_path == good
    assert len(pool.calls) == 1
    # The failure is stamped AFTER the call: the very next ask is served from the cache.
    for _ in range(5):
        assert CoreRuntime._resolve_session_dir(rt) == good
    assert len(pool.calls) == 1, "a failed ask was cached under an already expired stamp"
    # ... and is retried well before the full minute.
    stamp = rt._session_dir_cache[1]
    rt._session_dir_cache = (good, stamp - CoreRuntime._SESSION_DIR_RETRY_S - 1.0)
    CoreRuntime._resolve_session_dir(rt)
    assert len(pool.calls) == 2


def test_a_refusal_before_any_good_dir_is_none_not_an_error(tmp_path):
    rt = _rt(_Pool(str(tmp_path), busy=True))
    assert CoreRuntime._resolve_session_dir(rt) is None


def test_concurrent_askers_do_not_queue_behind_each_other(tmp_path):
    gate = threading.Event()
    pool = _Pool(str(tmp_path), gate=gate)
    rt = _rt(pool)
    first = threading.Thread(target=lambda: CoreRuntime._resolve_session_dir(rt), daemon=True)
    first.start()
    deadline = time.monotonic() + 5
    while not pool.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pool.calls, "the first ask never reached the pool"
    t0 = time.monotonic()
    assert CoreRuntime._resolve_session_dir(rt) is None            # nothing cached yet, no queueing
    assert time.monotonic() - t0 < 1.0
    assert len(pool.calls) == 1, "a second ask went to the instrument while one was in flight"
    gate.set()
    first.join(10)
    assert CoreRuntime._resolve_session_dir(rt) == str(tmp_path)
    assert len(pool.calls) == 1


# ── 3. the archive tick's scope ────────────────────────────────────────────────────────────
def _ingest_stub(scope_calls: list, *, raises: bool = False) -> SimpleNamespace:
    log = SimpleNamespace(current_experiment_id="E1", current_sample_id="S1")

    def ensure():
        scope_calls.append(1)
        if raises:
            raise RuntimeError("database is locked")
        return (Path("/nonexistent-exp"), "S01")

    return SimpleNamespace(
        _ingest=object(), _experiment_log=log, _scan_search_dirs=(),
        _ingest_watcher_enabled=lambda: True, _ensure_scope_dirs=ensure,
        _persist_ingest_state=lambda exp_dir, seen: None)


def test_the_archive_tick_lays_the_scope_down_once_not_every_cycle():
    calls: list = []
    stub = _ingest_stub(calls)
    seen: dict = {}
    for _ in range(10):
        CoreRuntime._data_ingest_tick(stub, seen)
    assert len(calls) == 1
    stub._experiment_log.current_sample_id = "S2"                  # a scope change is picked up
    CoreRuntime._data_ingest_tick(stub, seen)
    assert len(calls) == 2
    stub._ingest_scope_cache = None                                # what _on_scope_change does
    CoreRuntime._data_ingest_tick(stub, seen)
    assert len(calls) == 3


def test_the_scope_is_looked_at_again_after_its_ttl():
    calls: list = []
    stub = _ingest_stub(calls)
    CoreRuntime._ingest_scope(stub)
    key, expiry, value = stub._ingest_scope_cache
    assert 55 <= expiry - time.monotonic() <= CoreRuntime._INGEST_SCOPE_TTL_S + 1
    stub._ingest_scope_cache = (key, time.monotonic() - 1.0, value)
    CoreRuntime._ingest_scope(stub)
    assert len(calls) == 2


def test_a_locked_database_no_longer_aborts_the_archive_tick():
    calls: list = []
    stub = _ingest_stub(calls, raises=True)
    CoreRuntime._data_ingest_tick(stub, {})                        # must not raise
    CoreRuntime._data_ingest_tick(stub, {})
    assert len(calls) == 1, "a failed resolve is retried after a pause, not every cycle"


def test_on_scope_change_clears_the_archive_scope_cache():
    calls: list = []
    stub = _ingest_stub(calls)
    stub._link_v2_scope = lambda: None
    stub._chat_export_tick = lambda: None
    stub._check_stale_session_path = lambda: None
    CoreRuntime._ingest_scope(stub)
    assert stub._ingest_scope_cache is not None
    CoreRuntime._on_scope_change(stub, ("E1", "S1"), ("E1", "S1"))
    assert stub._ingest_scope_cache is None


# ── 4. a dir that is only resolved after start-up ─────────────────────────────────────────
def _spectrum_stub(sess: Path, logged: list, started_wall) -> SimpleNamespace:
    stub = SimpleNamespace(
        _storage=SimpleNamespace(log_marker=lambda **kw: logged.append(kw)),
        _experiment_log=SimpleNamespace(current_experiment_id="E1", current_sample_id="S1"),
        _map_last_skill_ts=0.0, _scan_search_dirs=(), _map_started_wall=started_wall)
    stub._current_scan_dirs = lambda: (str(sess),)
    return stub


def _always_a_marker(monkeypatch):
    import mast.io.exp_map as em

    monkeypatch.setattr(em, "manual_marker_from_spectrum_file", lambda path: SimpleNamespace(
        kind="spectrum", x_m=0.0, y_m=0.0, w_m=0.0, h_m=0.0, label="x", meta={}))


def test_old_files_in_a_dir_resolved_after_startup_make_no_markers(tmp_path, monkeypatch):
    _always_a_marker(monkeypatch)
    sess = tmp_path / "late"
    sess.mkdir()
    for n in range(3):
        p = sess / f"old{n}.dat"
        p.write_text(_DAT, encoding="utf-8")
        t = time.time() - 3600
        os.utime(p, (t, t))
    logged: list = []
    stub = _spectrum_stub(sess, logged, started_wall=time.time() - 60)
    CoreRuntime._map_spectrum_tick(stub, {})                      # empty baseline: dir was unknown
    assert logged == []


def test_a_file_saved_after_startup_in_the_late_dir_still_gets_its_marker(tmp_path, monkeypatch):
    sess = tmp_path / "late"
    sess.mkdir()
    p = sess / "new.dat"
    p.write_text(_DAT, encoding="utf-8")
    t = time.time() - 10                                           # after start, older than 5 s
    os.utime(p, (t, t))
    _always_a_marker(monkeypatch)
    logged: list = []
    stub = _spectrum_stub(sess, logged, started_wall=time.time() - 60)
    CoreRuntime._map_spectrum_tick(stub, {})
    assert len(logged) == 1
