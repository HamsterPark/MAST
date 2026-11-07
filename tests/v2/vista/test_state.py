"""mast.vista.state: the state echo after an act and in an observation.

Pinned here: the echo's ``withdrawn`` is the rule ``mast_status`` uses (compared with
``InstrumentState.refresh`` itself on the same controller replies); feedback state comes from
``GetZControllerState``; the current is read last; a withdrawn tip's current that has not decayed
is re-read for a bounded time after an act and flagged, never replaced by a guess; with the
feedback on, or the tip held at height, it is one read with no waiting.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from mast.vista import state as S


@dataclass
class R:
    success: bool = True
    data: dict = field(default_factory=dict)


class Clock:
    def __init__(self):
        self.t = 100.0
        self.slept: list[float] = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def runner(readings: dict, currents: list | None = None, calls: list | None = None):
    """``run(name, params)`` over canned readings; ``GetCurrent`` pops from ``currents`` (the last
    one repeats); a value that is an Exception is raised."""
    cur = list(currents or [])

    def run(name, params):
        if calls is not None:
            calls.append(name)
        if name == "GetCurrent":
            if not cur:
                return R(False)
            v = cur.pop(0) if len(cur) > 1 else cur[0]
            if isinstance(v, Exception):
                raise v
            return R(True, {"current_a": v}) if v is not None else R(False)
        v = readings.get(name)
        if isinstance(v, Exception):
            raise v
        return R(True, dict(v)) if v is not None else R(False)
    return run


ZOFF_TOP = {"controller_on": False, "module_status": 1, "module_status_name": "Off",
            "z_m": 7.500000265281415e-07, "z_limits": [7.500000265281415e-07, -7.5e-07]}
ZON = {"controller_on": True, "module_status": 2, "module_status_name": "On",
       "z_m": 3.9e-08, "z_limits": [7.5e-07, -7.5e-07]}
ZOFF_AT_HEIGHT = {"controller_on": False, "module_status": 1, "module_status_name": "Off",
                  "z_m": 3.9e-08, "z_limits": [7.5e-07, -7.5e-07]}
BASE = {"GetBias": {"bias_v": 2.0}, "GetSetpoint": {"setpoint_a": 5e-11},
        "GetZPosition": {"z_pos_m": 7.5e-07}, "GetScanXYPosition": {"x_m": 0.0, "y_m": 0.0}}


# ── withdrawn: the same rule as mast_status ─────────────────────────────────
class _Rec:
    def __init__(self, values):
        self.error = "" if values is not None else "not readable"
        self.return_value = ("", b"", list(values)) if values is not None else None


class _Pool:
    def __init__(self, replies):
        self.replies = replies

    def safe_call(self, method, *args, role="main"):
        return _Rec(self.replies.get(method))


@pytest.mark.parametrize("code, z, high", [
    (1, 7.500000265281415e-07, 7.500000265281415e-07),    # off, at the top: withdrawn
    (1, 3.9e-08, 7.5e-07),                                 # off, held at height
    (2, 7.5e-07, 7.5e-07),                                 # on
    (6, 7.5e-07, 7.5e-07),                                 # Withdrawing (not On)
    (1, None, 7.5e-07),                                    # Z unreadable
    (1, 7.5e-07, None),                                    # limits unreadable
    (None, 7.5e-07, 7.5e-07),                              # status unreadable
])
def test_withdrawn_is_the_status_rule(code, z, high):
    from mast.core.state import InstrumentState

    replies = {"Bias_Get": [2.0], "ZCtrl_SetpntGet": [5e-11], "Current_Get": [1e-15],
               "Scan_StatusGet": [0]}
    if code is not None:
        replies["ZCtrl_StatusGet"] = [code]
    if z is not None:
        replies["ZCtrl_ZPosGet"] = [z]
    if high is not None:
        replies["ZCtrl_LimitsGet"] = [high, -high]
    hw = InstrumentState(_Pool(replies)).refresh()
    on = None if code is None else code == S.ZCTRL_ON_CODE
    assert hw.z_controller_on == on
    assert S.withdrawn_from(on, z, high) == hw.withdrawn
    echo = S.state_echo(runner({"GetZControllerState": {
        "controller_on": None, "module_status": code, "module_status_name": None,
        "z_m": z, "z_limits": [high, -high] if high is not None else None}}, [1e-15]),
        skills=("GetZControllerState",), settle_budget_s=0)
    assert echo.get("withdrawn") == hw.withdrawn
    assert echo.get("z_controller_on") == hw.z_controller_on


def test_feedback_state_and_a_disagreement_are_echoed():
    out = S.state_echo(runner({**BASE, "GetZControllerState": ZON}, [5.0e-11]))
    assert (out["z_controller_on"], out["withdrawn"], out["z_controller_status"]) == (True, False, "On")
    assert "current_settling" not in out and "current_settle_s" not in out
    rt_only = dict(ZOFF_TOP, module_status=None, disagreement="⚠ 实时控制器说 OFF，而模块显示 ?")
    out = S.state_echo(runner({"GetZControllerState": rt_only}, [1e-15]), settle_budget_s=0)
    assert out["z_controller_on"] is False and out["withdrawn"] is True    # the RT read fills in
    assert out["z_controller_note"].startswith("⚠")
    out = S.state_echo(runner({**BASE}, [5e-11]))                          # not readable: left out
    assert not {"z_controller_on", "withdrawn", "z_controller_status"} & set(out)


# ── the current, read last ──────────────────────────────────────────────────
def test_the_current_is_read_last_and_the_echo_keeps_a_fixed_key_order():
    calls: list[str] = []
    out = S.state_echo(runner({**BASE, "GetZControllerState": ZON,
                               "GetDriftCompensation": {"enabled": False}}, [5.0003e-11], calls))
    assert calls[-1] == "GetCurrent" and calls.count("GetCurrent") == 1
    assert list(out)[:3] == ["bias_v", "setpoint_pa", "current_pa"]
    assert out["current_pa"] == pytest.approx(50.003)


def test_a_withdrawn_tips_current_is_reread_until_it_has_decayed():
    clk = Clock()
    out = S.state_echo(runner({**BASE, "GetZControllerState": ZOFF_TOP},
                              [5.0003e-11, 3.039e-11, 2e-13]), sleep=clk.sleep, monotonic=clk.monotonic)
    assert out["withdrawn"] is True and out["current_pa"] == pytest.approx(0.2)
    assert "current_settling" not in out
    assert out["current_settle_s"] == pytest.approx(0.5) and clk.slept == [0.25, 0.25]


def test_a_current_that_has_not_decayed_within_the_budget_is_flagged_not_guessed():
    clk = Clock()
    out = S.state_echo(runner({**BASE, "GetZControllerState": ZOFF_TOP}, [3.039e-11]),
                       sleep=clk.sleep, monotonic=clk.monotonic)
    assert out["current_pa"] == pytest.approx(30.39) and out["current_settling"] is True
    assert sum(clk.slept) <= S.CURRENT_SETTLE_BUDGET_S
    assert out["current_settle_s"] == pytest.approx(sum(clk.slept))


def test_an_observation_does_not_wait_it_only_flags():
    clk = Clock()
    out = S.state_echo(runner({**BASE, "GetZControllerState": ZOFF_TOP}, [5.0003e-11]),
                       settle_budget_s=0, sleep=clk.sleep, monotonic=clk.monotonic)
    assert clk.slept == [] and out["current_settling"] is True and out["current_settle_s"] == 0.0


def test_feedback_on_or_a_tip_held_at_height_is_one_read():
    for z in (ZON, ZOFF_AT_HEIGHT):
        clk = Clock()
        calls: list[str] = []
        out = S.state_echo(runner({**BASE, "GetZControllerState": z}, [3.0e-11, 0.0], calls),
                           sleep=clk.sleep, monotonic=clk.monotonic)
        assert clk.slept == [] and calls.count("GetCurrent") == 1
        assert out["current_pa"] == pytest.approx(30.0) and "current_settling" not in out


def test_a_failed_reread_leaves_the_last_reading_flagged():
    clk = Clock()
    out = S.state_echo(runner({**BASE, "GetZControllerState": ZOFF_TOP}, [3.0e-11, RuntimeError("socket")]),
                       sleep=clk.sleep, monotonic=clk.monotonic)
    assert out["current_pa"] == pytest.approx(30.0) and out["current_settling"] is True


def test_a_small_amplifier_offset_counts_as_settled():
    """A withdrawn tip reading a few pA off zero is an offset, not a lagging junction: the
    threshold is a tenth of the setpoint, so it neither waits nor flags."""
    clk = Clock()
    out = S.state_echo(runner({**BASE, "GetZControllerState": ZOFF_TOP}, [3.0e-12]),
                       sleep=clk.sleep, monotonic=clk.monotonic)
    assert clk.slept == [] and out["current_pa"] == pytest.approx(3.0) and "current_settling" not in out
    assert S.settled_threshold_a(5e-11) == pytest.approx(5e-12)
    assert S.settled_threshold_a(None) == S.CURRENT_SETTLED_FLOOR_A
    assert S.settled_threshold_a(-1e-9) == pytest.approx(1e-10)


def test_unreadable_and_non_finite_values_are_left_out():
    out = S.state_echo(runner({"GetBias": {"bias_v": float("nan")}, "GetSetpoint": {"setpoint_a": 5e-11}},
                              [None]))
    assert out == {"setpoint_pa": 50.0}
