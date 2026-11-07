"""Stage wait loops honour an abort (base.py:176-180 /
195-201 and pztc_nm003.py:297-304 used to poll with no abort check at all).

Both predicate shapes MAST uses are pinned: ``ExecutionContext.check_abort``
returns True; the satellite hub's contract (§6) raises. Either way the axis is
told to stop before control returns, and an abort seen BEFORE any motion sends
no stop (nothing was started). Callers that pass no predicate behave exactly
as before.
"""
from __future__ import annotations

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

import pytest

from mast.instruments.base import (
    AxisConfig,
    AxisStatus,
    MotionAborted,
    MotionAxis,
    MotionController,
    MotionTimeout,
)
from mast.instruments.delay_line import DelayLine, DelayLineConfig
from mast.instruments.pztc_nm003 import PztcController


class _SlowAxis(MotionAxis):
    """Never reaches the target on its own: only an abort or the timeout ends a wait."""

    POLL_INTERVAL_S = 0.005

    def __init__(self, config, controller):
        super().__init__(config, controller)
        self.position = 0.0
        self.stops = 0
        self.moves: list[float] = []
        self.homing = False

    def _move_abs_raw(self, target):
        self.moves.append(target)

    def _get_position_raw(self):
        return self.position

    def _get_status_raw(self):
        return AxisStatus(position=self.position, moving=True, on_target=False,
                          homed=False if self.homing else True)

    def _stop_raw(self):
        self.stops += 1

    def _home_raw(self):
        self.homing = True


class _Ctrl(MotionController):
    def __init__(self, axis_cls=_SlowAxis, settle_s=0.0):
        super().__init__()
        self._cls = axis_cls
        self._settle = settle_s

    def _connect_raw(self):
        pass

    def _close_raw(self):
        pass

    def _build_axes(self):
        return {"x": self._cls(AxisConfig(name="x", channel=1, min_pos=0, max_pos=100,
                                          settle_s=self._settle), self)}


def _countdown(n: int):
    """A bool predicate that turns True on its n-th call."""
    box = {"n": 0}

    def probe():
        box["n"] += 1
        return box["n"] >= n

    probe.calls = box  # type: ignore[attr-defined]
    return probe


class _Abort(RuntimeError):
    pass


def _raiser_after(n: int):
    box = {"n": 0}

    def probe():
        box["n"] += 1
        if box["n"] >= n:
            raise _Abort("hub cancel")

    return probe


def test_bool_predicate_stops_the_axis_and_raises_motion_aborted():
    ax = _Ctrl().axis("x")
    with pytest.raises(MotionAborted):
        ax.move_abs(10.0, timeout=5.0, check_abort=_countdown(3))
    assert ax.moves == [10.0]          # the move was issued …
    assert ax.stops == 1               # … and stopped when the abort fired


def test_raising_predicate_stops_and_propagates_its_own_exception():
    ax = _Ctrl().axis("x")
    with pytest.raises(_Abort):
        ax.move_abs(10.0, timeout=5.0, check_abort=_raiser_after(2))
    assert ax.stops == 1


def test_abort_before_motion_sends_neither_move_nor_stop():
    ax = _Ctrl().axis("x")
    with pytest.raises(MotionAborted, match="before any motion"):
        ax.move_abs(10.0, check_abort=lambda: True)
    assert ax.moves == [] and ax.stops == 0


def test_without_a_predicate_the_old_timeout_behaviour_is_unchanged():
    ax = _Ctrl().axis("x")
    with pytest.raises(MotionTimeout):
        ax.move_abs(10.0, timeout=0.05)
    assert ax.stops == 0               # the base class never stopped on timeout, still doesn't


def test_move_rel_threads_the_predicate_through():
    ax = _Ctrl().axis("x")
    with pytest.raises(MotionAborted):
        ax.move_rel(5.0, timeout=5.0, check_abort=_countdown(2))
    assert ax.stops == 1


def test_home_wait_loop_honours_abort():
    ax = _Ctrl().axis("x")
    with pytest.raises(MotionAborted):
        ax.home(timeout=5.0, check_abort=_countdown(4))
    assert ax.stops == 1


class _QuickAxis(_SlowAxis):
    def _get_status_raw(self):
        return AxisStatus(position=self.position, moving=False, on_target=True)


def test_settle_dwell_is_abortable_too():
    ax = _Ctrl(axis_cls=_QuickAxis, settle_s=5.0).axis("x")
    with pytest.raises(MotionAborted, match="settle"):
        ax.move_abs(10.0, timeout=5.0, check_abort=_countdown(4))
    assert ax.stops == 1


def test_delay_line_passes_the_predicate_to_its_axis():
    ctrl = _Ctrl()
    dl = DelayLine(ctrl.axis("x"), DelayLineConfig(device_id="d", axis="x",
                                                   unit_per_mm=1.0, ps_per_mm=1.0))
    with pytest.raises(MotionAborted):
        dl.move_to_delay_ps(5.0, timeout=5.0, check_abort=_countdown(2))
    with pytest.raises(MotionAborted):
        dl.home(timeout=5.0, check_abort=_countdown(2))


class _ZeroTransport:
    """PZTC bus that acknowledges writes and never reports find-zero complete."""

    def __init__(self):
        self.sent: list[bytes] = []

    def transact(self, payload, *, read_size=None, read_until=None, timeout=None):
        from mast.instruments.pztc_nm003 import build_rtu_frame

        self.sent.append(bytes(payload))
        func = payload[1]
        if func == 0x02:                                  # discrete input: zero_status = 0
            return build_rtu_frame(1, 0x02, bytes([1, 0]))
        if func == 0x04:                                  # input regs: position 0
            return build_rtu_frame(1, 0x04, bytes([4, 0, 0, 0, 0]))
        return bytes(payload)                             # echo writes (fn 05/06/10)

    def close(self):
        pass


def test_pztc_find_zero_loop_honours_abort_and_stops_the_channel():
    t = _ZeroTransport()
    ctrl = PztcController(transport=t, axes=[AxisConfig(name="d", channel=0, min_pos=0,
                                                        max_pos=10)])
    ax = ctrl.axis("d")
    ax.POLL_INTERVAL_S = 0.001
    with pytest.raises(MotionAborted):
        ax.home(timeout=5.0, check_abort=_countdown(3))
    # the stop is a write of 0 to the close_loop_move coil (fn 0x05, value 0x0000)
    stops = [f for f in t.sent if f[1] == 0x05 and f[4:6] == b"\x00\x00"]
    assert stops, "abort during find-zero must disable the closed-loop coil"
