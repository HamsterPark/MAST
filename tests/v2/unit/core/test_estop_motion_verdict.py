"""E-STOP says motion stopped only when all three stop commands went through.

``CoreRuntime.emergency_stop`` sends AutoApproach_OnOffSet(0), Motor_StopMove and
Scan_Action(1, 0) on the emergency port, then the withdraw. It used to set
``stopped_motion = True`` as soon as one of the three calls RETURNED. But the pool
reports almost every failure in the record it returns (``rec.error``: comms breaker
refusal, busy role lock, timeout, a Nanonis error string), not as an exception. So an
E-STOP whose three stops were all refused still answered ``stopped_motion: true``.

The stand-in pool answers the way ``ConnectionPool.urgent_call`` does - one
``NanonisCallRecord`` per call, ``error`` set when it failed - and has nothing the real
pool lacks. One test uses the real pool with its breaker open.

Run from repo root:
    .venv-v2-py313/Scripts/python.exe -m pytest tests/v2/unit/core/test_estop_motion_verdict.py -v
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports ──
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

import pytest

from mast.core.types import NanonisCallRecord

STOPS = ("AutoApproach_OnOffSet", "Motor_StopMove", "Scan_Action")


class _Pool:
    """Answers like ``ConnectionPool.urgent_call``: a record per call, ``error`` set on
    failure. ``errors`` / ``raises`` script the outcome per verb; anything unscripted
    succeeds the way a completed round trip does."""

    def __init__(self, errors: dict | None = None, raises: dict | None = None) -> None:
        self.errors = dict(errors or {})
        self.raises = dict(raises or {})
        self.sent: list[tuple[str, str]] = []

    def urgent_call(self, method_name, *args, role="main", lock_timeout_s=2.0):
        self.sent.append((method_name, role))
        if method_name in self.raises:
            raise self.raises[method_name]
        if self.errors.get(method_name):
            return NanonisCallRecord(method=method_name, args=args,
                                     error=self.errors[method_name])
        return NanonisCallRecord(method=method_name, args=args, return_value=("", b"", []))


@pytest.fixture
def rt(monkeypatch):
    from mast.config import MASTConfig
    from mast.core.runtime import CoreRuntime

    r = CoreRuntime.__new__(CoreRuntime)
    r.config = MASTConfig()
    r._orch_abort = threading.Event()
    r._orch_run_aborts_lock = threading.Lock()
    r._orch_run_aborts = {}
    r._executor = None
    r._orch_interrupts = None
    r._pool = None
    r._shutdown_hooks = []
    r._station_profile = "stm"
    monkeypatch.setattr(r, "_optics_emergency_stop", lambda why: {}, raising=False)
    monkeypatch.setattr(r, "_magnet_emergency_stop", lambda why: "none", raising=False)
    return r


def _stop_errors(res: dict) -> list[str]:
    return [e for e in res["errors"] if e.split(":", 1)[0] in STOPS]


def test_three_stops_that_went_through_are_stopped_motion(rt):
    rt._pool = _Pool()
    res = rt.emergency_stop("test")
    assert [v for v, _ in rt._pool.sent][:3] == list(STOPS)
    assert res["stopped_motion"] is True
    assert res["retracted"] is True and res["errors"] == []


def test_three_refused_stops_are_not_stopped_motion(rt):
    """The case that was wrong: every stop came back with an error record (here the
    comms breaker's refusal) and nothing was raised."""
    refused = "comms_circuit_open: Nanonis TCP communication timed out repeatedly"
    rt._pool = _Pool(errors={v: refused for v in STOPS})
    res = rt.emergency_stop("test")
    assert res["stopped_motion"] is False
    assert res["errors"] == [f"{v}: {refused}" for v in STOPS]
    assert res["retracted"] is True, "the withdraw went through and still says so"


@pytest.mark.parametrize("failing", STOPS)
def test_one_stop_that_did_not_go_through_is_not_stopped_motion(rt, failing):
    rt._pool = _Pool(errors={failing: "RoleBusy: role 'emergency' busy"})
    res = rt.emergency_stop("test")
    assert res["stopped_motion"] is False
    assert _stop_errors(res) == [f"{failing}: RoleBusy: role 'emergency' busy"]


def test_a_stop_that_raises_is_reported_as_before(rt):
    """The exception path is unchanged: the message goes into errors under the verb's
    name. One raised stop is a failed stop too."""
    rt._pool = _Pool(raises={"Motor_StopMove": ConnectionError("no connection")})
    res = rt.emergency_stop("test")
    assert "Motor_StopMove: no connection" in res["errors"]
    assert res["stopped_motion"] is False


def test_the_real_pool_with_its_breaker_open_is_not_stopped_motion(rt, unused_tcp_ports):
    """End to end with the real ConnectionPool: three TCP failures open its breaker,
    then the E-STOP. Whatever the pool does with emergency calls while the breaker is
    open (refuse them, or send them to a port where nothing listens), each stop comes
    back as an error record - and the result must not claim motion stopped."""
    from mast.config import NanonisConfig
    from mast.core.connection import ConnectionPool

    main, monitor, data, emergency = unused_tcp_ports(4)
    pool = ConnectionPool(NanonisConfig(host="127.0.0.1", port_main=main,
                                        port_monitor=monitor, port_data=data,
                                        port_emergency=emergency, timeout_s=1.0))
    for _ in range(3):
        pool._breaker.record_failure("timed out")
    assert pool.comms_healthy() is False
    rt._pool = pool
    res = rt.emergency_stop("test")
    assert res["stopped_motion"] is False
    assert [e.split(":", 1)[0] for e in _stop_errors(res)] == list(STOPS)
    assert res["retracted"] is False
