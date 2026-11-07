"""ConnectionPool, 2026-10-15: module-open recv grace, the root cause in the error
text, the transport-failure flag, and the log level of opportunistic lock refusals.

Failure shape (Nanonis Mimea, first ConfigureSTS after the simulator started):
``BiasSpectr_Open`` returned at once, ``BiasSpectr_LimitsSet`` sent no reply within
the 5 s socket timeout, the pool reconnected and resent it, the resend was reset by
the controller (WinError 10054), and the external agent was told only
``ConnectionResetError: [WinError 10054]`` — the 5 s timeout was in the service log
alone. Separately, every ~30 s during a scan the monitoring poll's deliberate
"give up after 0.1 s" on the data role was logged at ERROR.
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (as in test_connection.py) ──
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

import logging
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


class FakeConfig:
    host = "localhost"
    port_main = 6501
    port_monitor = 6502
    port_data = 6503
    port_emergency = 6504
    timeout_s = 5.0


_OK = ("", b"", [])


class FakeNanonis:
    """Every Nanonis method answers OK unless a behaviour is scripted for it."""

    def __init__(self, scripted: dict | None = None):
        self.scripted = dict(scripted or {})
        self.sent: list[str] = []

    def close(self):
        pass

    def __getattr__(self, name):
        if "_" not in name or name.startswith("__"):
            raise AttributeError(name)

        def _call(*_args):
            self.sent.append(name)
            todo = self.scripted.get(name)
            if todo:
                step = todo.pop(0)
                if isinstance(step, BaseException):
                    raise step
                return step
            return _OK
        return _call


def _pool(nn: FakeNanonis, roles=("main",)):
    from mast.core.connection import ConnectionPool

    pool = ConnectionPool(FakeConfig())
    for role in roles:
        pool._connections[role] = (MagicMock(spec=socket.socket), nn)
    return pool


def _spy_budgets(pool) -> list:
    """The recv timeout each call ran under (None = the pool's default)."""
    seen: list = []
    real = pool._raised_recv_timeout

    def spy(role, seconds):
        seen.append(seconds)
        return real(role, seconds)

    pool._raised_recv_timeout = spy
    return seen


# ── module-open grace ────────────────────────────────────────────────────────

def test_open_and_the_one_call_after_it_get_the_module_load_budget():
    from mast.core.connection import _MODULE_OPEN_RECV_S

    pool = _pool(FakeNanonis())
    seen = _spy_budgets(pool)
    pool.safe_call("BiasSpectr_Open")
    pool.safe_call("BiasSpectr_LimitsSet", -1.0, 1.0)
    pool.safe_call("BiasSpectr_PropsSet", 1, 1, 1, 128, 0.0, 1, 2)
    assert seen == [_MODULE_OPEN_RECV_S, _MODULE_OPEN_RECV_S, None], (
        "Open and the next call wait for the module to load; the grace is one shot")


def test_the_grace_belongs_to_the_role_that_opened():
    from mast.core.connection import _MODULE_OPEN_RECV_S

    pool = _pool(FakeNanonis(), roles=("main", "monitor"))
    seen = _spy_budgets(pool)
    pool.safe_call("BiasSpectr_Open")                       # main
    pool.safe_call("Current_Get", role="monitor")          # another role: no grace
    pool.safe_call("BiasSpectr_LimitsSet", -1.0, 1.0)      # main: still armed
    assert seen == [_MODULE_OPEN_RECV_S, None, _MODULE_OPEN_RECV_S]


def test_the_grace_expires():
    from mast.core.connection import _MODULE_OPEN_FOLLOWUP_WINDOW_S

    pool = _pool(FakeNanonis())
    seen = _spy_budgets(pool)
    pool.safe_call("BiasSpectr_Open")
    pool._open_followup["main"] = time.monotonic() - _MODULE_OPEN_FOLLOWUP_WINDOW_S - 1
    pool.safe_call("BiasSpectr_LimitsSet", -1.0, 1.0)
    assert seen[-1] is None, "a call long after the Open has nothing to do with loading"


def test_a_refused_open_arms_nothing():
    from mast.core.connection import _MODULE_OPEN_RECV_S

    pool = _pool(FakeNanonis({"BiasSpectr_Open": [("Error: module not licensed", b"", [])]}))
    seen = _spy_budgets(pool)
    rec = pool.safe_call("BiasSpectr_Open")
    assert rec.error
    pool.safe_call("BiasSpectr_LimitsSet", -1.0, 1.0)
    assert seen == [_MODULE_OPEN_RECV_S, None]


def test_a_larger_explicit_budget_is_never_lowered():
    pool = _pool(FakeNanonis())
    seen = _spy_budgets(pool)
    pool.safe_call("BiasSpectr_Open")
    pool.safe_call("BiasSpectr_Start", 1, "", recv_timeout_s=600.0)
    assert seen[-1] == 600.0


# ── the root cause leads; transport failures are flagged ────────────────────

def test_when_the_resend_fails_too_the_first_failure_leads_the_error():
    """The failure shape: timeout, reconnect, resend reset by the controller."""
    from mast.core.connection import is_transport_failure

    nn = FakeNanonis({"BiasSpectr_LimitsSet": [
        socket.timeout("timed out"),
        ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"),
    ]})
    pool = _pool(nn)
    with patch.object(pool, "_reconnect_role", return_value=True):
        rec = pool.safe_call("BiasSpectr_LimitsSet", -1.0, 1.0)
    assert rec.error.startswith("'BiasSpectr_LimitsSet' timed out"), rec.error
    assert "within 5.0 s" in rec.error
    assert "resent the command once" in rec.error
    assert "ConnectionResetError" in rec.error, "the second failure is still reported"
    assert is_transport_failure(rec)


def test_the_timeout_names_the_budget_that_applied():
    nn = FakeNanonis({"BiasSpectr_LimitsSet": [socket.timeout("timed out"),
                                               socket.timeout("timed out")]})
    pool = _pool(nn)
    pool.safe_call("BiasSpectr_Open")
    with patch.object(pool, "_reconnect_role", return_value=True):
        rec = pool.safe_call("BiasSpectr_LimitsSet", -1.0, 1.0)
    assert "within 30.0 s" in rec.error, rec.error


def test_no_reconnect_keeps_the_exception_first_and_says_it_was_not_resent():
    from mast.core.connection import is_transport_failure

    pool = _pool(FakeNanonis({"Util_VersionGet": [socket.timeout("timed out")]}))
    with patch.object(pool, "_reconnect_role", return_value=False):
        rec = pool.safe_call("Util_VersionGet")
    assert rec.error.startswith("TimeoutError: timed out"), rec.error
    assert "not resent" in rec.error
    assert is_transport_failure(rec)


def test_lost_connection_and_empty_reply_are_transport_failures():
    from mast.core.connection import ConnectionPool, is_transport_failure

    pool = ConnectionPool(FakeConfig())               # no connection at all
    with patch.object(pool, "_reconnect_role", return_value=False):
        rec = pool.safe_call("Util_VersionGet")
    assert "Connection lost" in rec.error and is_transport_failure(rec)

    pool2 = _pool(FakeNanonis({"Current_Get": [(), ()]}))
    with patch.object(pool2, "_reconnect_role", return_value=True):
        rec2 = pool2.safe_call("Current_Get")
    assert "空包" in rec2.error and is_transport_failure(rec2)


def test_an_application_error_is_not_a_transport_failure():
    from mast.core.connection import is_transport_failure

    pool = _pool(FakeNanonis({"LockIn_ModPhasSet": [
        ("Access Denied: Parameter is Locked", b"", [])]}))
    rec = pool.safe_call("LockIn_ModPhasSet", 1, 0.0)
    assert rec.error == "Access Denied: Parameter is Locked"
    assert not is_transport_failure(rec)


def test_records_from_elsewhere_are_never_transport_failures():
    from mast.core.connection import is_transport_failure
    from mast.core.types import NanonisCallRecord

    assert not is_transport_failure(NanonisCallRecord(method="X", error="boom"))
    assert not is_transport_failure(object())


# ── opportunistic lock refusals: summarised, not ERROR ──────────────────────

class _Holder:
    """Hold a role lock from ANOTHER thread (the lock is re-entrant per thread)."""

    def __init__(self, pool, role):
        self.lk = pool._role_lock(role)
        self.held = threading.Event()
        self.release = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self.lk:
            self.held.set()
            self.release.wait(10)

    def __enter__(self):
        self.t.start()
        assert self.held.wait(2)
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.t.join(2)


def test_a_quick_give_up_is_not_logged_as_an_error(caplog):
    from mast.core.connection import is_lock_busy, is_transport_failure

    pool = _pool(FakeNanonis(), roles=("data",))
    with _Holder(pool, "data"), caplog.at_level(logging.DEBUG, logger="mast.core.connection"):
        first = pool.safe_call("Signals_ValsGet", role="data", lock_timeout_s=0.1)
        pool.safe_call("Signals_ValsGet", role="data", lock_timeout_s=0.1)
    assert is_lock_busy(first) and not is_transport_failure(first)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1, "the first skip is announced once, the next one is not"
    assert "Signals_ValsGet" in infos[0].getMessage()


def test_skips_are_summarised_once_per_window(caplog):
    from mast.core.connection import _OPPORTUNISTIC_SUMMARY_S

    pool = _pool(FakeNanonis(), roles=("data",))
    with _Holder(pool, "data"):
        pool.safe_call("Signals_ValsGet", role="data", lock_timeout_s=0.1)   # announced
        pool.safe_call("Signals_ValsGet", role="data", lock_timeout_s=0.1)   # counted
        pool._skipped_reported_at = time.monotonic() - _OPPORTUNISTIC_SUMMARY_S - 1
        with caplog.at_level(logging.INFO, logger="mast.core.connection"):
            pool.safe_call("Signals_ValsGet", role="data", lock_timeout_s=0.1)
    summary = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(summary) == 1
    assert "2 opportunistic call(s)" in summary[0] and "×2" in summary[0], summary


def test_a_real_wait_that_times_out_is_still_an_error(caplog):
    pool = _pool(FakeNanonis(), roles=("data",))
    with _Holder(pool, "data"), caplog.at_level(logging.DEBUG, logger="mast.core.connection"):
        rec = pool.safe_call("Signals_ValsGet", role="data", lock_timeout_s=1.0)
    assert rec.error
    assert [r for r in caplog.records if r.levelno == logging.ERROR], (
        "a caller that was prepared to wait and could not is real contention")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
