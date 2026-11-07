"""ConnectionPool, 2026-10-15: a command that may already have reached the controller is
not sent a second time when it acts each time it arrives.

After a socket error, a reply timeout or an empty reply, ``safe_call`` reconnected and resent
the command once — whatever it was. Such a failure usually means the command DID arrive and
the controller is executing it (or has): resending a coarse move walks 2N steps, a pulse fires
twice, a tip shaping runs twice, a sweep or a script starts again. Reads, property sets, stops
and withdrawals keep their one resend; a command that never left (no connection before
sending) is still sent once the connection is back.
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

import socket
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
    """Every method answers OK unless a behaviour is scripted for it; records what was sent."""

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


def _pool(nn: FakeNanonis):
    from mast.core.connection import ConnectionPool

    pool = ConnectionPool(FakeConfig())
    pool._connections["main"] = (MagicMock(spec=socket.socket), nn)
    return pool


ACTS_EACH_TIME = [
    ("Motor_StartMove", (0, 100, 0, 1)),        # coarse move: twice = 2N steps
    ("Bias_Pulse", (1, 0.01, 3.0, 0, 0)),
    ("DigLines_Pulse", (1, 0.01)),
    ("TipShaper_Start", (1, 5000)),
    ("BiasSpectr_Start", (1, "")),
    ("ZSpectr_Start", (1, "")),
    ("Script_Run", (1, 1)),
    ("Scan_Action", (0, 0)),                    # start
    ("Scan_Action", (3, 0)),                    # resume
    ("AutoApproach_OnOffSet", (1,)),            # on
    ("AutoApproach_OnOffSet", (True,)),
    ("Scan_Save", (1, 4000)),                   # twice = two auto-numbered files of one frame
    ("AtomTrack_DriftComp", ()),                # may add the measured drift a second time
]

RESENDABLE = [
    ("Bias_Get", ()),
    ("Bias_Set", (0.5,)),
    ("Scan_Action", (1, 0)),                    # stop
    ("Scan_Action", (2, 0)),                    # pause
    ("AutoApproach_OnOffSet", (0,)),            # off
    ("ZCtrl_Withdraw", (1, -1)),
    ("Motor_StopMove", ()),
    ("BiasSpectr_Stop", ()),
    ("PLLZoomFFT_AvgRestart", ()),
]


@pytest.mark.parametrize("method, args", ACTS_EACH_TIME)
@pytest.mark.parametrize("failure", [socket.timeout("timed out"),
                                     ConnectionResetError(10054, "reset by peer")])
def test_a_command_that_acts_each_time_is_not_resent(method, args, failure):
    from mast.core.connection import is_transport_failure

    nn = FakeNanonis({method: [failure]})
    pool = _pool(nn)
    with patch.object(pool, "_reconnect_role", return_value=True) as reconnect:
        rec = pool.safe_call(method, *args)
    assert nn.sent == [method], f"{method}{args} was sent {len(nn.sent)} times"
    assert reconnect.called, "the connection is re-established for the next command"
    assert "did not resend" in rec.error and "do it twice" in rec.error, rec.error
    assert rec.error.startswith(f"'{method}'"), "the error leads with the command and its failure"
    assert is_transport_failure(rec)


def test_an_empty_reply_to_a_coarse_move_is_not_resent():
    nn = FakeNanonis({"Motor_StartMove": [()]})
    pool = _pool(nn)
    with patch.object(pool, "_reconnect_role", return_value=True):
        rec = pool.safe_call("Motor_StartMove", 0, 100, 0, 1)
    assert nn.sent == ["Motor_StartMove"]
    assert "empty reply" in rec.error and "did not resend" in rec.error


@pytest.mark.parametrize("method, args", RESENDABLE)
def test_reads_sets_stops_and_withdrawals_keep_their_one_resend(method, args):
    nn = FakeNanonis({method: [socket.timeout("timed out")]})
    pool = _pool(nn)
    with patch.object(pool, "_reconnect_role", return_value=True):
        rec = pool.safe_call(method, *args)
    assert nn.sent == [method, method]
    assert not rec.error, rec.error


def test_a_command_that_never_left_is_sent_once_the_connection_is_back():
    """No connection BEFORE sending: nothing reached the controller, so even a coarse move
    goes out (once) on the re-established connection."""
    from mast.core.connection import ConnectionPool

    nn = FakeNanonis()
    pool = ConnectionPool(FakeConfig())                 # no connection for "main" yet

    def reconnect(role):
        pool._connections[role] = (MagicMock(spec=socket.socket), nn)
        return True

    with patch.object(pool, "_reconnect_role", side_effect=reconnect):
        rec = pool.safe_call("Motor_StartMove", 0, 100, 0, 1)
    assert nn.sent == ["Motor_StartMove"] and not rec.error, rec.error


@pytest.mark.parametrize("method, args, safe", [
    ("Scan_Action", ("start",), False),                 # unreadable code: do not guess
    ("Scan_Action", (), False),
    ("AutoApproach_OnOffSet", (None,), False),
    ("Current_Get", (), True),
    ("FolMe_XYPosSet", (1e-9, 2e-9, 1), True),
])
def test_resend_is_safe_reads_the_command_and_its_first_argument(method, args, safe):
    from mast.core.connection import resend_is_safe

    assert resend_is_safe(method, args) is safe
