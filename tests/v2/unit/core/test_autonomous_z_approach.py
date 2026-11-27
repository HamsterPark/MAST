"""Autonomous open-loop coarse Z approach: it runs and notifies, with a per-call step cap.

``MotorMove(direction='z-approach')`` used to be refused outright on every
non-human path (the agent, composites, conduct, the direct skill API, the
external gateway), and the refusal told the agent to have the user run it "in
the GUI" — which MAST's own skill buttons cannot do, because they post to the
direct skill API, a non-human source. Since 2026-11-21 it is treated like
every other formerly approval-gated action: execute + leave a trace + notify.
What remains is a per-call step cap (``AUTONOMOUS_Z_APPROACH_MAX_STEPS``);
above it the whole call is refused, never clamped. The human manual path is
not capped.

What this file pins, by layer:

  * ``mast.core.safety`` — the two narrow predicates behind the umbrella
    ``is_coarse_sample_approach`` and the cap helper (whole-number parsing,
    fail-closed on anything else, switchable off with ``None``);
  * ``mast.core.auto_approval.would_have_asked`` — the reason that ends up in
    the approvals row and the notice, only for calls that will actually run;
  * ``mast.core.safety_escalation.is_approach_escalation`` — a z-approach
    counts as an escalation once its params are known;
  * ``SafetyGateMiddleware`` Layer 0g — a z-approach while an ApproachTip
    refusal is live is still refused (it was refused before this change too);
  * ``ExecutionContext.run`` and the legacy ``SkillExecutor.run`` — within
    the cap the call runs and leaves exactly one ``notice_only`` row; over the
    cap it is refused with no row; the human path is uncapped and gets no row;
  * the five refusal-type gates that did NOT change — the closed-loop coarse
    move with a Z target and the four others — still refuse on both paths.

Run from repo root:
    .venv-v2-py313/Scripts/python.exe -m pytest \\
        tests/v2/unit/core/test_autonomous_z_approach.py -q
"""
from __future__ import annotations

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

from typing import Any
from unittest.mock import MagicMock

import pytest

from mast.config import SafetyLimits
from mast.core import safety
from mast.core import safety_escalation as esc
from mast.core.auto_approval import would_have_asked
from mast.core.execution_context import ExecutionContext
from mast.core.executor import SkillExecutor
from mast.core.registry import SkillRegistry
from mast.core.safety import (
    SafetyGuard,
    autonomous_z_approach_cap_note,
    autonomous_z_approach_refusal,
    is_closed_loop_coarse_z_move,
    is_coarse_sample_approach,
    is_open_loop_z_approach,
)
from mast.core.types import (
    HardwareState,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill
from mast.skills.builtins.motor import MotorMove

_CAP_KEY = "z_approach_step_cap_exceeded"
_HARD_GATE_MARKS = ("硬闸拒绝", "Hard gate refused")


def _cap() -> int:
    cap = safety.AUTONOMOUS_Z_APPROACH_MAX_STEPS
    assert cap is not None, (
        "these tests pin the capped behaviour; if the cap is removed, rewrite them")
    return cap


def _z(steps: Any) -> dict:
    return {"direction": "z-approach", "steps": steps}


def _is_hard_gate(err: str | None) -> bool:
    return any(m in (err or "") for m in _HARD_GATE_MARKS)


@pytest.fixture(autouse=True)
def _no_live_approach_refusal():
    """The ApproachTip refusal is process-global; never inherit or leak one."""
    esc.clear_approach_refusal(owner=None)
    yield
    esc.clear_approach_refusal(owner=None)


# ── Diagnostics ledger helpers (``seq`` is process-wide and monotonic) ───────

def _last_seq() -> int:
    from mast.core import diagnostics as diag

    rows = diag.recent(1)
    return int(rows[0].get("seq", 0)) if rows else 0


def _notices_since(seq0: int, subject: str = "MotorMove") -> list[dict]:
    from mast.core import diagnostics as diag

    return [r for r in diag.recent(200, kinds=("notice_only",))
            if r.get("seq", 0) > seq0 and r.get("subject") == subject]


# ─────────────────────────────────────────────────────────────────────────
# mast.core.safety — predicates and the cap helper
# ─────────────────────────────────────────────────────────────────────────

class TestPredicates:
    @pytest.mark.parametrize("direction",
                             ["z-approach", "z_approach", "zapproach", " Z-Approach "])
    def test_open_loop_z_approach_spellings(self, direction):
        params = {"direction": direction, "steps": 1}
        assert is_open_loop_z_approach("MotorMove", params)
        assert is_coarse_sample_approach("MotorMove", params)
        assert not is_closed_loop_coarse_z_move("MotorMove", params)

    @pytest.mark.parametrize("direction", ["z-retract", "x+", "x-", "y+", "y-", ""])
    def test_other_motor_directions_are_not_a_z_approach(self, direction):
        params = {"direction": direction, "steps": 1}
        assert not is_open_loop_z_approach("MotorMove", params)
        assert not is_coarse_sample_approach("MotorMove", params)

    def test_the_direction_only_counts_on_motormove(self):
        assert not is_open_loop_z_approach("AutoApproach", _z(1))
        assert not is_open_loop_z_approach("MotorMoveClosedLoop", _z(1))

    def test_closed_loop_with_z_target_is_the_other_half_of_the_umbrella(self):
        params = {"target_z_m": 1e-6}
        assert is_closed_loop_coarse_z_move("MotorMoveClosedLoop", params)
        assert is_coarse_sample_approach("MotorMoveClosedLoop", params)
        assert not is_open_loop_z_approach("MotorMoveClosedLoop", params)

    def test_closed_loop_pure_xy_is_neither(self):
        params = {"target_x_m": 1e-6}
        assert not is_closed_loop_coarse_z_move("MotorMoveClosedLoop", params)
        assert not is_coarse_sample_approach("MotorMoveClosedLoop", params)


class TestStepCapHelper:
    def test_default_cap_is_small_and_whole(self):
        assert isinstance(_cap(), int) and 1 <= _cap() <= 1000

    @pytest.mark.parametrize("steps", [1, 2, "3", 3.0, " 4 "])
    def test_whole_numbers_within_the_cap_pass(self, steps):
        assert autonomous_z_approach_refusal(_z(steps)) is None

    def test_exactly_the_cap_passes(self):
        assert autonomous_z_approach_refusal(_z(_cap())) is None
        assert autonomous_z_approach_refusal(_z(str(_cap()))) is None

    @pytest.mark.parametrize("steps", ["over", True, False, None, "abc", 2.5, "2.5", [1], {}])
    def test_anything_else_is_refused_fail_closed(self, steps):
        if steps == "over":
            steps = _cap() + 1
        msg = autonomous_z_approach_refusal(_z(steps))
        assert msg is not None and msg.startswith(_CAP_KEY), msg

    def test_missing_steps_is_refused(self):
        assert autonomous_z_approach_refusal({"direction": "z-approach"}).startswith(_CAP_KEY)
        assert autonomous_z_approach_refusal(None).startswith(_CAP_KEY)

    def test_values_below_one_are_left_to_parameter_validation(self):
        # The skill's own ParameterSpec (min_value=1) rejects these; the cap is
        # about how far one call can step blind, not about malformed counts.
        assert autonomous_z_approach_refusal(_z(0)) is None
        assert autonomous_z_approach_refusal(_z(-3)) is None

    def test_the_refusal_names_the_cap_the_value_and_the_way_out(self):
        msg = autonomous_z_approach_refusal(_z(_cap() + 1))
        assert str(_cap()) in msg
        assert repr(_cap() + 1) in msg
        assert "AutoApproach" in msg and "ApproachTip" in msg
        assert "Nanonis" in msg, "the uncapped path has to be named"

    def test_the_cap_switches_off_with_none(self, monkeypatch):
        monkeypatch.setattr(safety, "AUTONOMOUS_Z_APPROACH_MAX_STEPS", None)
        assert autonomous_z_approach_refusal(_z(1000)) is None
        assert autonomous_z_approach_refusal(_z(10 ** 6)) is None

    def test_the_note_follows_the_constant(self, monkeypatch):
        cap = _cap()
        capped = autonomous_z_approach_cap_note()
        assert str(cap) in capped
        monkeypatch.setattr(safety, "AUTONOMOUS_Z_APPROACH_MAX_STEPS", None)
        uncapped = autonomous_z_approach_cap_note()
        assert uncapped != capped
        assert str(cap) not in uncapped

    def test_motormove_description_carries_the_note(self):
        desc = MotorMove().metadata().description
        assert autonomous_z_approach_cap_note() in desc
        assert "human approval" not in desc.lower()
        assert "人工批准" not in desc


# ─────────────────────────────────────────────────────────────────────────
# auto_approval.would_have_asked — the reason behind the row and the notice
# ─────────────────────────────────────────────────────────────────────────

class TestWouldHaveAsked:
    def setup_method(self):
        self.meta = MotorMove().metadata()

    def test_motormove_stays_confirm(self):
        # The z-approach rule keys on the direction; the baseline level must not
        # drift to DANGEROUS (that would notify every lateral / retract move).
        assert self.meta.safety_level == SafetyLevel.CONFIRM

    def test_within_the_cap_there_is_a_reason(self):
        reason = would_have_asked(self.meta, tool_name="MotorMove", args=_z(1))
        assert reason and "z-approach" in reason and "steps=1" in reason

    def test_over_the_cap_there_is_none(self):
        # Such a call is refused before it runs; it must not leave an
        # "executed and notified" trace.
        assert would_have_asked(self.meta, tool_name="MotorMove",
                                args=_z(_cap() + 1)) is None

    @pytest.mark.parametrize("direction", ["z-retract", "x+"])
    def test_other_directions_have_none(self, direction):
        assert would_have_asked(self.meta, tool_name="MotorMove",
                                args={"direction": direction, "steps": 1}) is None

    def test_tool_name_falls_back_to_the_metadata_name(self):
        assert would_have_asked(self.meta, args=_z(1))


# ─────────────────────────────────────────────────────────────────────────
# safety_escalation + SafetyGateMiddleware Layer 0g
# ─────────────────────────────────────────────────────────────────────────

class TestApproachEscalation:
    def test_name_only_callers_keep_the_old_answer(self):
        assert esc.is_approach_escalation("AutoApproach")
        assert not esc.is_approach_escalation("MotorMove")

    def test_a_z_approach_is_an_escalation_once_params_are_known(self):
        assert esc.is_approach_escalation("MotorMove", _z(1))
        assert not esc.is_approach_escalation(
            "MotorMove", {"direction": "z-retract", "steps": 1})
        assert not esc.is_approach_escalation(
            "MotorMove", {"direction": "x+", "steps": 1})


def _make_request(tool, args: dict, call_id: str = "zap-1") -> Any:
    req = MagicMock()
    req.tool = tool
    req.tool_call = {"name": tool.name, "args": args, "id": call_id, "type": "tool_call"}
    req.state = {}
    req.runtime = MagicMock()
    return req


class TestLayer0gStillCoversZApproach:
    """Before this change Layer 0 refused every agent z-approach, so a refused
    ApproachTip could never be followed by one. With Layer 0 letting a small
    z-approach through, Layer 0g keeps that one sequence closed: it is the less
    protected side door (no current-feedback stop at all)."""

    def setup_method(self):
        from mast.agents._shared.safety_mw import SafetyGateMiddleware
        from mast.agents._shared.skill_adapter import wrap_skill

        self.mw = SafetyGateMiddleware(SafetyLimits())
        self.tool = wrap_skill(MotorMove, lambda: MagicMock(safe_call=lambda *a, **k: None))

    def _call(self, args: dict):
        handler = MagicMock(return_value="ok")
        return self.mw.wrap_tool_call(_make_request(self.tool, args), handler), handler

    def test_z_approach_is_refused_while_an_approach_refusal_is_live(self):
        esc.record_approach_refusal("Z feedback state unreadable")
        result, handler = self._call(_z(1))
        handler.assert_not_called()
        assert result.status == "error"
        assert "approach_escalation_refused" in result.content
        assert "ApproachTip" in result.content, "the refusal must name the way out"

    def test_z_retract_is_not_affected_by_a_live_refusal(self):
        esc.record_approach_refusal("Z feedback state unreadable")
        result, handler = self._call({"direction": "z-retract", "steps": 1})
        handler.assert_called_once()
        assert result == "ok"

    def test_once_the_refusal_is_cleared_the_z_approach_runs(self):
        esc.record_approach_refusal("Z feedback state unreadable")
        esc.clear_approach_refusal("fresh ApproachTip decision", owner=None)
        result, handler = self._call(_z(1))
        handler.assert_called_once()
        assert result == "ok"


# ─────────────────────────────────────────────────────────────────────────
# ExecutionContext.run and SkillExecutor.run
# ─────────────────────────────────────────────────────────────────────────

class _InertMotorMove(MotorMove):
    """The real MotorMove metadata (CONFIRM, steps 1..1000, ``z_controller_off``)
    with an execute that touches nothing and counts its calls."""

    calls: list[dict] = []

    def execute(self, context, params: dict) -> SkillResult:
        type(self).calls.append(dict(params))
        return SkillResult(skill_name="MotorMove", success=True, data=dict(params))


def _named_confirm_skill(name: str) -> type[BaseSkill]:
    """A CONFIRM skill with a gated name. The refusal-type gates key on the
    skill name and the params only, so a stand-in is enough to reach them."""

    class _Skill(BaseSkill):
        def metadata(self) -> SkillMetadata:
            return SkillMetadata(name=name, version="1.0.0",
                                 category=SkillCategory.WRITE,
                                 safety_level=SafetyLevel.CONFIRM,
                                 description="", parameters=[])

        def execute(self, context, params: dict) -> SkillResult:
            return SkillResult(skill_name=name, success=True, data=dict(params))

    _Skill.__name__ = f"_Fake{name}"
    return _Skill


# (skill, params) for each refusal-type gate this change left alone.
_UNCHANGED_HARD_GATES = [
    ("MotorMoveClosedLoop", {"target_z_m": 1e-6}),
    ("EnableSafeTip", {"enable": False}),
    ("SetBiasCalibration", {}),
    ("SetMotorFreqAmp", {"frequency_hz": 1000.0, "amplitude_v": 100.0}),
    ("MotorMove", {"direction": "x+", "steps": 1}),
]


def _registry(*extra: type[BaseSkill]) -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(_InertMotorMove)
    for cls in extra:
        reg.register(cls)
    return reg


class TestExecutionContextPath:
    """Composites, conduct and the direct skill API all end up here."""

    def setup_method(self):
        _InertMotorMove.calls.clear()

    def _ctx(self, source: str, *extra: type[BaseSkill]) -> ExecutionContext:
        return ExecutionContext(pool=None, state=None, registry=_registry(*extra),
                                approval_source=source)

    @pytest.mark.parametrize("source", ["auto", "llm"])
    def test_within_the_cap_it_runs_and_leaves_one_notice(self, source):
        seq0 = _last_seq()
        res = self._ctx(source).run("MotorMove", _z(1))
        assert res.success is True, res.error
        assert _InertMotorMove.calls == [_z(1)]
        rows = _notices_since(seq0)
        assert len(rows) == 1
        assert rows[0]["approval_source"] == source
        assert rows[0]["path"] == "execution_context"
        assert "z-approach" in rows[0]["reason"]

    @pytest.mark.parametrize("source", ["auto", "llm"])
    def test_over_the_cap_it_is_refused_whole_and_leaves_no_notice(self, source):
        seq0 = _last_seq()
        res = self._ctx(source).run("MotorMove", _z(_cap() + 1))
        assert res.success is False
        assert _CAP_KEY in res.error
        assert not _is_hard_gate(res.error), "over the cap is not a hard gate any more"
        assert _InertMotorMove.calls == [], "refused, never clamped to the cap"
        assert _notices_since(seq0) == []

    def test_the_human_path_is_uncapped_and_not_notified(self):
        seq0 = _last_seq()
        res = self._ctx("human").run("MotorMove", _z(_cap() * 5))
        assert res.success is True, res.error
        assert _InertMotorMove.calls == [_z(_cap() * 5)]
        assert _notices_since(seq0) == []

    def test_z_retract_is_not_notified(self):
        seq0 = _last_seq()
        res = self._ctx("auto").run("MotorMove", {"direction": "z-retract", "steps": 50})
        assert res.success is True, res.error
        assert _notices_since(seq0) == []

    @pytest.mark.parametrize("skill,params", _UNCHANGED_HARD_GATES,
                             ids=[s for s, _ in _UNCHANGED_HARD_GATES])
    def test_the_unchanged_hard_gates_still_refuse(self, skill, params):
        extra = () if skill == "MotorMove" else (_named_confirm_skill(skill),)
        res = self._ctx("llm", *extra).run(skill, params)
        assert res.success is False
        assert _is_hard_gate(res.error), res.error
        assert _InertMotorMove.calls == []


class _State:
    """``InstrumentState`` stand-in: feedback off, so MotorMove's
    ``z_controller_off`` precondition is met and only the gates decide."""

    def __init__(self):
        self._s = HardwareState(z_controller_on=False)

    def refresh(self) -> HardwareState:
        return self._s

    def snapshot(self) -> HardwareState:
        return self._s


class TestLegacySkillExecutorPath:
    def setup_method(self):
        _InertMotorMove.calls.clear()

    def _executor(self, *extra: type[BaseSkill]) -> SkillExecutor:
        return SkillExecutor(pool=MagicMock(), registry=_registry(*extra),
                             safety=SafetyGuard(SafetyLimits()), state=_State())

    def test_within_the_cap_it_runs_and_leaves_one_notice(self):
        seq0 = _last_seq()
        res = self._executor().run("MotorMove", _z(1), approval_source="llm")
        assert res.success is True, res.error
        assert _InertMotorMove.calls == [_z(1)]
        rows = _notices_since(seq0)
        assert len(rows) == 1
        assert rows[0]["path"] == "executor"
        assert rows[0]["approval_source"] == "llm"

    def test_over_the_cap_it_is_refused_whole_and_leaves_no_notice(self):
        seq0 = _last_seq()
        res = self._executor().run("MotorMove", _z(_cap() + 1), approval_source="llm")
        assert res.success is False
        assert _CAP_KEY in res.error
        assert not _is_hard_gate(res.error)
        assert _InertMotorMove.calls == []
        assert _notices_since(seq0) == []

    def test_a_call_the_confirm_rule_rejects_leaves_no_notice(self):
        # This executor rejects every CONFIRM skill under source "auto" (it
        # predates this change). The notice is sent after that check, so a call
        # that never runs does not claim it was "executed and notified".
        seq0 = _last_seq()
        res = self._executor().run("MotorMove", _z(1), approval_source="auto")
        assert res.success is False
        assert _CAP_KEY not in res.error and not _is_hard_gate(res.error)
        assert _InertMotorMove.calls == []
        assert _notices_since(seq0) == []

    def test_the_human_path_is_uncapped_and_not_notified(self):
        seq0 = _last_seq()
        res = self._executor().run("MotorMove", _z(_cap() * 5), approval_source="human")
        assert res.success is True, res.error
        assert _notices_since(seq0) == []

    @pytest.mark.parametrize(
        "skill,params",
        [g for g in _UNCHANGED_HARD_GATES if g[0] != "EnableSafeTip"
         and g[0] != "SetBiasCalibration"],
        ids=["MotorMoveClosedLoop", "SetMotorFreqAmp", "MotorMove-x+"])
    def test_its_hard_gates_still_refuse(self, skill, params):
        # This executor carries three of the five gates (no protection-disable
        # or calibration gate here); all three must still fire.
        extra = () if skill == "MotorMove" else (_named_confirm_skill(skill),)
        res = self._executor(*extra).run(skill, params, approval_source="llm")
        assert res.success is False
        assert _is_hard_gate(res.error), res.error
        assert _InertMotorMove.calls == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
