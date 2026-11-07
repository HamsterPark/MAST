"""MotorMove's Z labels follow the rig's DECLARED coarse retract direction.

``z-retract`` used to be fixed at Z+ (code 4) and ``z-approach`` at Z-
(code 5), whatever ``retract_motor_dir`` said. A rig that declares
``z-``: every 'z-retract' stepped TOWARD the sample — outside the
human-approval gate, which keys on the 'z-approach' label — and
RecoverTipFromSaturation's six-rung ladder after a crash (20/30/50/100/200/400
steps, 800 by default) would have stepped up to 800 steps into the sample.

Now: declared ⇒ z-retract = the declared code, z-approach = the other one;
undeclared ⇒ the Nanonis standard (4 / 5) as before, with a warning to declare it.

Needs no motor groups or drive latch.
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[5] / "MASTv2")
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

from mast.core import instrument_profile as ip
from mast.core.types import NanonisCallRecord
from mast.skills.builtins.motor import MotorMove

UNDECLARED_HINT = "设置 → 退针 → 退针方向"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """Keep any motor-group config / drive latch of the data root out (neither
    need exist)."""
    try:
        from mast.instruments import motor_groups as mg
    except ImportError:
        yield
        return
    monkeypatch.setattr(mg, "config_path", lambda: tmp_path / mg.CONFIG_FILENAME)
    mg.reset_for_tests()
    yield
    mg.reset_for_tests()


@pytest.fixture
def retract_dir():
    """Declare (``"z+"`` / ``"z-"``) or leave undeclared (``None``) the coarse retract
    direction in the in-memory instrument profile; restored afterwards."""
    before = ip.get_profile()

    def declare(value):
        prof = {k: v for k, v in before.items() if k != "retract_motor_dir"}
        if value is not None:
            prof["retract_motor_dir"] = value
        ip.set_profile(prof)

    declare(None)
    yield declare
    ip.set_profile(before)


class _Nanonis:
    """Records Motor_StartMove; every other call succeeds with an empty reply."""

    def __init__(self):
        self.moves: list[tuple] = []
        self.state = None

    def safe_call(self, method, *args, **kw):
        if method == "Motor_StartMove":
            self.moves.append(args)
        return NanonisCallRecord(method=method, args=args, return_value=("", b"", []))

    def check_abort(self):
        return False


def _codes(ctx):
    return [m[0] for m in ctx.moves]


@pytest.mark.parametrize("declared,retract,approach,basis", [
    ("z-", 5, 4, "declared_z-"),
    ("z+", 4, 5, "declared_z+"),
    (None, 4, 5, "undeclared_nanonis_standard"),
])
def test_z_labels_map_by_the_declared_retract_direction(retract_dir, declared, retract,
                                                        approach, basis):
    retract_dir(declared)
    for direction, code in (("z-retract", retract), ("z-approach", approach)):
        ctx = _Nanonis()
        res = MotorMove().execute(ctx, {"direction": direction, "steps": 7})
        assert res.success, res.error
        assert _codes(ctx) == [code]
        assert ctx.moves[0][1] == 7
        assert res.data["direction_code"] == code
        assert res.data["z_direction_basis"] == basis
        if declared is None:
            assert UNDECLARED_HINT in res.data["warning"]
            assert UNDECLARED_HINT in res.summary
        else:
            assert "warning" not in res.data and res.summary is None


def test_lateral_codes_are_untouched(retract_dir):
    retract_dir("z-")
    for direction, code in (("x+", 0), ("x-", 1), ("y+", 2), ("y-", 3)):
        ctx = _Nanonis()
        res = MotorMove().execute(ctx, {"direction": direction, "steps": 3})
        assert res.success and _codes(ctx) == [code]
        assert "direction_code" not in res.data and "warning" not in res.data


def test_an_unreadable_profile_sends_no_coarse_z(retract_dir, monkeypatch):
    def boom():
        raise RuntimeError("profile unreadable")

    monkeypatch.setattr(ip, "retract_dir_code_or_none", boom)
    ctx = _Nanonis()
    res = MotorMove().execute(ctx, {"direction": "z-retract", "steps": 5})
    assert not res.success and ctx.moves == []


def test_the_human_gate_still_keys_on_the_label_which_now_means_what_it_says(retract_dir):
    """``is_coarse_sample_approach`` gates 'z-approach' only — right exactly
    because the label now maps onto the toward-the-sample code on every rig."""
    from mast.core.safety import is_coarse_sample_approach

    assert is_coarse_sample_approach("MotorMove", {"direction": "z-approach"}) is True
    assert is_coarse_sample_approach("MotorMove", {"direction": "z-retract"}) is False
    retract_dir("z-")                               # Z- is AWAY on the instrument
    ctx = _Nanonis()
    MotorMove().execute(ctx, {"direction": "z-approach", "steps": 1})
    assert _codes(ctx) == [4]                       # the gated label sends the approach


class _RigCtx(_Nanonis):
    """RecoverTipFromSaturation's context, physical: the tip is jammed in the
    sample; current stays saturated until the coarse motor has backed off
    ``clear_after`` steps in the rig's real away direction (Z- here). MotorMove
    runs for real."""

    AWAY_CODE = 5

    def __init__(self, clear_after: int = 800):
        super().__init__()
        self.away = 0
        self.clear_after = clear_after

    def safe_call(self, method, *args, **kw):
        if method == "Motor_StartMove":
            code, steps = args[0], int(args[1])
            self.away += steps if code == self.AWAY_CODE else -steps
        return super().safe_call(method, *args, **kw)

    def run(self, name, params=None):
        params = params or {}
        if name == "MotorMove":
            return MotorMove().execute(self, params)
        from mast.core.types import SkillResult

        if name == "GetCurrent":
            current = 1.00036e-8 if self.away < self.clear_after else 1e-12
            return SkillResult(skill_name=name, success=True, data={"current_a": current})
        return SkillResult(skill_name=name, success=True, data={})


def _recover(ctx, monkeypatch):
    from mast.skills.builtins import RecoverTipFromSaturation
    from mast.skills.builtins import saturation_recovery as sr

    monkeypatch.setattr(sr.time, "sleep", lambda s: None)
    skill = RecoverTipFromSaturation()
    skill._settle_s = 0.0
    return skill.execute(ctx, {})


def test_saturation_recovery_backs_off_on_a_rig_declared_z_minus(retract_dir, monkeypatch):
    """The ladder that mattered: six rungs, 20/30/50/100/200/400 = the default
    800-step budget. On a rig declared z− every rung must be Z- (5). With the old
    fixed map each rung was Z+ — into the sample — and all six ran with the
    current still pinned."""
    from mast.skills.builtins import saturation_recovery as sr

    assert sr._LADDER == (20, 30, 50, 100, 200, 400) and sum(sr._LADDER) == 800
    retract_dir("z-")
    ctx = _RigCtx(clear_after=800)            # clears only on the last rung
    res = _recover(ctx, monkeypatch)
    rungs = [m for m in ctx.moves if int(m[1]) > 0]
    assert [int(m[1]) for m in rungs] == list(sr._LADDER)
    assert all(m[0] == 5 for m in rungs), ctx.moves
    assert res.success and res.data["outcome"] == "recovered", res.error
    assert res.data["coarse_steps_used"] == 800 and ctx.away == 800
    logged = [s for s in res.data["steps"] if s["stage"] == "coarse_z"]
    assert {(s["direction_code"], s["z_direction_basis"]) for s in logged} == {
        (5, "declared_z-")}


def test_a_failed_recovery_names_the_direction_it_used(retract_dir, monkeypatch):
    """Not cleared after the whole budget: the message says which way the rungs
    went and where the direction is declared — the question to ask next."""
    retract_dir("z-")
    res = _recover(_RigCtx(clear_after=10_000), monkeypatch)
    assert not res.success and res.data["outcome"] == "not_recovered"
    assert res.data["coarse_steps_used"] == 800
    assert "本机声明的 Z−（方向码 5）" in res.error and UNDECLARED_HINT in res.error
    retract_dir(None)
    res = _recover(_RigCtx(clear_after=10_000), monkeypatch)
    assert "Nanonis 标准 Z+（本机没声明退针方向）" in res.error
