"""Long-lived SQLite connections for the experiment databases.

Why this exists
---------------
The experiment databases run in WAL mode.  When the *last* connection to a WAL
database closes, SQLite takes an EXCLUSIVE lock, checkpoints the whole WAL,
fsyncs the database and deletes ``-wal``/``-shm``.  The stores used to open and
close a connection per call, so almost every close was the last one: each call
paid for a checkpoint, and on a busy spinning disk that stretched long enough
for other connections -- even pure readers -- to hit the 5 s busy timeout
("database is locked").

Two helpers fix that without changing any store's transaction logic:

* :func:`hold` keeps one idle *keeper* connection open per database file for the
  life of the process.  While it exists no per-call close is ever the last one.
* :class:`ConnectionPool` hands each thread its own long-lived connection (a
  ``with conn:`` block still commits/rolls back, it just no longer closes), so
  the open cost and the schema parse are paid once per thread.

Both are process-wide and keyed by the resolved file path, because several
store *instances* are created for the same file (``documents.paths.storage()``
builds a new one per call).

Tests that delete a database directory must call :func:`release_under` first:
an open handle blocks deleting the file on Windows.
"""

from __future__ import annotations

import atexit
import logging as _logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

logger = _logging.getLogger(__name__)

_registry_lock = threading.Lock()
_keepers: dict[str, sqlite3.Connection] = {}
_pools: dict[str, "ConnectionPool"] = {}
#: Seconds a failed keeper attempt is not retried for the same file.
FAIL_BACKOFF_S = 60.0
_failed_until: dict[str, float] = {}
#: Injectable clock (tests).
_clock = time.monotonic


def _key(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def hold(path: str | os.PathLike) -> bool:
    """Keep one idle connection to *path* open for the process lifetime.

    Idempotent.  Never raises: a failure only means the old per-call behaviour
    (returns False), and the failure is remembered for ``FAIL_BACKOFF_S`` so a
    busy disk is not hit with a fresh 5 s connect attempt on every database
    call.  The connection is made OUTSIDE the registry lock (it can wait on the
    disk) and published with a double check; a loser of the race closes its
    own connection.  The keeper runs no statements after attaching to the WAL,
    so it never holds a lock or a read snapshot.
    """
    k = _key(path)
    with _registry_lock:
        if k in _keepers:
            return True
        if _clock() < _failed_until.get(k, 0.0):
            return False
    try:
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # One read, so this connection really attaches to the WAL and its
            # shared-memory index: a connection that never touched the database
            # since the mode switch is not counted as a holder by SQLite.
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            conn.commit()
        except Exception:
            _close_quietly(conn)
            raise
    except Exception:  # noqa: BLE001 -- best effort, caller works without it
        logger.debug("db keeper for %s not opened", path, exc_info=True)
        with _registry_lock:
            _failed_until[k] = _clock() + FAIL_BACKOFF_S
        return False
    with _registry_lock:
        loser = k in _keepers
        if not loser:
            _keepers[k] = conn
            _failed_until.pop(k, None)
    if loser:
        _close_quietly(conn)
    return True


def is_held(path: str | os.PathLike) -> bool:
    with _registry_lock:
        return _key(path) in _keepers


class ConnectionPool:
    """One connection per live thread for one database file.

    ``factory`` builds a fully configured connection (pragmas, row_factory).
    Connections of threads that have ended are closed lazily when a new thread
    asks for one.  The caller keeps using ``with conn:`` for transactions; the
    connection itself is never closed by a store call.
    """

    def __init__(self, path: Path, factory: Callable[[], sqlite3.Connection]):
        self.path = Path(path)
        self._factory = factory
        self._lock = threading.Lock()
        # ident -> (thread, connection)
        self._conns: dict[int, tuple[threading.Thread, sqlite3.Connection]] = {}

    def connection(self) -> sqlite3.Connection:
        me = threading.current_thread()
        ident = me.ident or 0
        with self._lock:
            hit = self._conns.get(ident)
            if hit is not None and hit[0] is me:
                return hit[1]
            self._reap_locked()
            if hit is not None:  # ident reused by a new thread: drop the stale one
                _close_quietly(hit[1])
        conn = self._factory()  # outside the lock: may wait on the disk
        with self._lock:
            self._conns[ident] = (me, conn)
        return conn

    def _reap_locked(self) -> None:
        for ident, (th, conn) in list(self._conns.items()):
            if not th.is_alive():
                del self._conns[ident]
                _close_quietly(conn)

    def close_all(self) -> None:
        with self._lock:
            items = list(self._conns.values())
            self._conns.clear()
        for _th, conn in items:
            _close_quietly(conn)

    def open_count(self) -> int:
        with self._lock:
            return len(self._conns)


def pool_for(path: str | os.PathLike,
             factory: Callable[[], sqlite3.Connection]) -> ConnectionPool:
    """The process-wide pool for *path* (created on first use) and its keeper."""
    k = _key(path)
    if k in _pools and not os.path.exists(path):
        release(path)  # file deleted/recreated under us: never serve a dead inode
    with _registry_lock:
        pool = _pools.get(k)
        if pool is None:
            pool = _pools[k] = ConnectionPool(Path(path), factory)
    hold(path)
    return pool


def _close_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def release(path: str | os.PathLike) -> None:
    """Close the pool connections and the keeper for *path* (keeper last, so the
    final close checkpoints the WAL)."""
    k = _key(path)
    with _registry_lock:
        pool = _pools.pop(k, None)
        keeper = _keepers.pop(k, None)
        _failed_until.pop(k, None)
    if pool is not None:
        pool.close_all()
    if keeper is not None:
        _close_quietly(keeper)


def release_under(directory: str | os.PathLike) -> None:
    """:func:`release` every database whose file lives under *directory*."""
    root = _key(directory)
    with _registry_lock:
        keys = {k for k in (*_pools, *_keepers)
                if k == root or k.startswith(root.rstrip("\\/") + os.sep)}
    for k in keys:
        release(k)


def release_all() -> None:
    with _registry_lock:
        keys = {*_pools, *_keepers}
    for k in keys:
        release(k)


atexit.register(release_all)
