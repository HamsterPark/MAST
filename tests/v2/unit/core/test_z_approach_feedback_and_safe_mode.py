"""MotorMove z-approach since 7.3.3.

1. The per-call step cap stays, at 20 (7.3.2 shipped 10). Every number in
   ``test_autonomous_z_approach.py`` derives from the constant, so the cap
   itself is pinned there and in the tooltip test.
2. A z-approach does not need the Z controller off: a tip poke is commonly done
   with the feedback ON. MotorMove still DECLARES ``z_controller_off`` — lateral moves
   and z-retract need it — and ``mast.core.preconditions.effective_preconditions``
   drops it for a z-approach. Four layers evaluate preconditions, and each must
   ask that function with the call's params:

     * ``BaseSkill.check_preconditions`` via ``unmet_preconditions``
       (the agent's skill-tool wrapper, and ``ExecutionContext.run``);
     * ``SafetyGuard.validate_execution`` (the legacy ``SkillExecutor``);
     * ``SafetyGate`` Layer 2 (the agent middleware).

   Both failure directions matter, so both are pinned per layer: a layer that
   forgets the params refuses a poke that is allowed; a layer that drops the
   precondition for every direction lets a lateral move drag a tip that is
   still in feedback.
3. SAFE refuses an autonomous z-approach (``mode_refusal``). SEMI and AUTO are
   unchanged, and the human path is not mode-gated.
4. The hard gates stay, but their refusals no longer end with
   「请用户在 GUI 手动执行」 — no MAST screen offers that path (the skill
   page posts to the direct skill API, a non-human source). Their refusals
   now name Nanonis.

Run from repo root:
    .venv-v2-py313/Scripts/python.exe -m pytest \\
        tests/v2/unit/core/test_z_approach_feedback_and_safe_mode.py -q
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

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from mast.config import SafetyLimits  # noqa: E402
from mast.core import safety_escalation as esc  # noqa: E402
from mast.core.execution_context import ExecutionContext  # noqa: E402
from mast.core.executor import SkillExecutor  # noqa: E402
from mast.core.operating_mode import bind_mode_source  # noqa: E402
from mast.core.preconditions import effective_preconditions  # noqa: E402
from mast.core.registry import SkillRegistry  # noqa: E402
from mast.core.safety import (  # noqa: E402
    AUTONOMOUS_Z_APPROACH_MAX_STEPS,
    SafetyGuard,
    hard_gate_manual_path,
    mode_refusal,
)
from mast.core.types import (  # noqa: E402
    HardwareState,
    OperatingMode,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill, unmet_preconditions  # noqa: E402
from mast.skills.builtins.motor import MotorMove  # noqa: E402

_SAFE_KEY = "safe_mode_tip_processing_blocked"
_OLD_MANUAL_PATH = ("请用户在 GUI 手动执行", "manually in the GUI", "via the GUI")

FEEDBACK_ON = HardwareState(z_controller_on=True)
FEEDBACK_OFF = HardwareState(z_controller_on=False)

Z_APPROACH = {"direction": "z-approach", "steps": 1}
POKE = {"direction": "z-approach", "steps": 8}          # a typical 5–10 step poke
LATERAL = {"direction": "x+", "steps": 1}
RETRACT = {"direction": "z-retract", "steps": 1}


class _InertMotorMove(MotorMove):
    """The real MotorMove metadata — CONFIRM, steps 1..1000, ``z_controller_off``
    declared — with an execute that touches nothing and records its calls."""

    calls: list[dict] = []

    def execute(self, context, params: dict) -> SkillResult:
        type(self).calls.append(dict(params))
        return SkillResult(skill_name="MotorMove", success=True, data=dict(params))


class _State:
    """``InstrumentState`` stand-in with a fixed snapshot."""

    def __init__(self, hw: HardwareState):
        self._s = hw

    def refresh(self) -> HardwareState:
        return self._s

    def snapshot(self) -> HardwareState:
        return self._s


def _registry(*extra: type[BaseSkill]) -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(_InertMotorMove)
    for cls in extra:
        reg.register(cls)
    return reg


def _ctx(source: str, hw: HardwareState | None = None, *extra) -> ExecutionContext:
    return ExecutionContext(pool=None, state=_State(hw) if hw is not None else None,
                            registry=_registry(*extra), approval_source=source)


def _last_seq() -> int:
    from mast.core import diagnostics as diag

    rows = diag.recent(1)
    return int(rows[0].get("seq", 0)) if rows else 0


def _notices_since(seq0: int, subject: str = "MotorMove") -> list[dict]:
    from mast.core import diagnostics as diag

    return [r for r in diag.recent(200, kinds=("notice_only",))
            if r.get("seq", 0) > seq0 and r.get("subject") == subject]


@pytest.fixture(autouse=True)
def _isolate():
    _InertMotorMove.calls.clear()
    esc.clear_approach_refusal(owner=None)
    bind_mode_source(None)
    yield
    bind_mode_source(None)      # a leaked binding flips every later tip decision
    esc.clear_approach_refusal(owner=None)
    _InertMotorMove.calls.clear()


# ─────────────────────────────────────────────────────────────────────────
# 2. The Z feedback may stay on for a z-approach — the one decision
# ─────────────────────────────────────────────────────────────────────────

class TestEffectivePreconditions:
    DECLARED = ["z_controller_off"]

    @pytest.mark.parametrize("direction", ["z-approach", "z_approach", " Z-Approach "])
    def test_a_z_approach_is_exempt_from_z_controller_off(self, direction):
        params = {"direction": direction, "steps": 1}
        assert effective_preconditions("MotorMove", self.DECLARED, params) == []

    @pytest.mark.parametrize("params", [LATERAL, RETRACT, {"direction": "y-", "steps": 3}])
    def test_every_other_direction_keeps_it(self, params):
        assert effective_preconditions("MotorMove", self.DECLARED, params) == self.DECLARED

    def test_without_the_params_nothing_is_exempt(self):
        """A caller that cannot see the params stays as strict as before."""
        assert effective_preconditions("MotorMove", self.DECLARED, None) == self.DECLARED

    def test_only_motormove_is_exempt(self):
        # MotorMoveClosedLoop also declares z_controller_off; its Z move is a hard
        # gate on every autonomous path and nothing here relaxes it.
        assert effective_preconditions(
            "MotorMoveClosedLoop", self.DECLARED, Z_APPROACH) == self.DECLARED

    def test_only_that_one_precondition_is_dropped(self):
        declared = ["z_controller_off", "scan_not_running", "vacuum_ok_for_coarse"]
        assert effective_preconditions("MotorMove", declared, Z_APPROACH) == [
            "scan_not_running", "vacuum_ok_for_coarse"]

    def test_motormove_still_declares_it(self):
        """The exemption is per call. The declaration stays, because lateral
        moves and z-retract are the same skill."""
        assert "z_controller_off" in MotorMove().metadata().preconditions


class TestSkillLayer:
    """BaseSkill.check_preconditions — what the tool wrapper and ExecutionContext ask."""

    def test_without_params_feedback_on_is_still_unmet(self):
        assert MotorMove().check_preconditions(FEEDBACK_ON)

    def test_a_z_approach_with_feedback_on_is_met(self):
        assert unmet_preconditions(MotorMove(), FEEDBACK_ON, Z_APPROACH) == []

    @pytest.mark.parametrize("params", [LATERAL, RETRACT], ids=["lateral", "retract"])
    def test_lateral_and_retract_with_feedback_on_are_unmet(self, params):
        unmet = unmet_preconditions(MotorMove(), FEEDBACK_ON, params)
        assert unmet and "z_controller_off" in unmet[0]

    def test_feedback_off_still_satisfies_a_z_approach(self):
        """"Need not be off" means either state, not "must be on"."""
        assert unmet_preconditions(MotorMove(), FEEDBACK_OFF, Z_APPROACH) == []

    def test_the_helper_sits_beside_the_class_not_inside_it(self):
        """A module-level function inserted between ``check_preconditions`` and
        ``execute`` still compiles, and every method after it — the abstract
        ``execute``, ``abortable_sleep``, ``rollback`` — becomes dead code inside
        the new function, which focused tests of the new behaviour do not
        notice. Pin the structure."""
        import mast.skills.base as base_mod

        for name in ("execute", "abortable_sleep", "rollback", "check_preconditions"):
            assert name in vars(BaseSkill), f"BaseSkill lost {name}"
        assert "execute" in BaseSkill.__abstractmethods__
        assert base_mod.unmet_preconditions.__qualname__ == "unmet_preconditions"

    def test_a_one_argument_override_is_called_as_before(self):
        seen: list = []

        class _Override(BaseSkill):
            def metadata(self):
                return SkillMetadata(name="OverrideProbe", description="",
                                     category=SkillCategory.READ,
                                     safety_level=SafetyLevel.AUTO, parameters=[])

            def check_preconditions(self, state):
                seen.append(state)
                return []

            def execute(self, context, params):  # pragma: no cover
                return SkillResult(skill_name="OverrideProbe", success=True)

        assert unmet_preconditions(_Override(), FEEDBACK_ON, {"x": 1}) == []
        assert seen == [FEEDBACK_ON]


class TestExecutionContextPath:
    """Composites, conduct, the direct skill API (MAST's own skill page) and the
    external gateway all reach the hardware through here."""

    @pytest.mark.parametrize("source", ["auto", "llm"])
    def test_a_poke_with_feedback_on_runs(self, source):
        res = _ctx(source, FEEDBACK_ON).run("MotorMove", POKE)
        assert res.success is True, res.error
        assert _InertMotorMove.calls == [POKE]

    def test_a_poke_runs_when_the_state_cannot_be_refreshed(self):
        """The first check alone must honour the exemption. When it fails,
        ``_recheck_after_refresh`` asks the hardware again — and with no
        ``refresh`` it keeps the first answer, so a first check that ignored
        the params would refuse here."""
        class _SnapshotOnly:
            def snapshot(self):
                return FEEDBACK_ON

        ctx = ExecutionContext(pool=None, state=_SnapshotOnly(), registry=_registry(),
                               approval_source="llm")
        res = ctx.run("MotorMove", POKE)
        assert res.success is True, res.error
        assert _InertMotorMove.calls == [POKE]

    def test_a_retract_with_feedback_on_is_still_refused(self):
        res = _ctx("llm", FEEDBACK_ON).run("MotorMove", RETRACT)
        assert res.success is False
        assert "z_controller_off" in (res.error or "")
        assert _InertMotorMove.calls == []

    def test_a_lateral_move_with_feedback_on_is_still_refused_on_the_human_path(self):
        # The human path skips the hard gates, so this is the precondition alone.
        res = _ctx("human", FEEDBACK_ON).run("MotorMove", LATERAL)
        assert res.success is False
        assert "z_controller_off" in (res.error or "")
        assert _InertMotorMove.calls == []


class TestLegacyExecutorPath:
    def _executor(self, hw: HardwareState) -> SkillExecutor:
        return SkillExecutor(pool=MagicMock(), registry=_registry(),
                             safety=SafetyGuard(SafetyLimits()), state=_State(hw))

    def test_a_poke_with_feedback_on_runs(self):
        res = self._executor(FEEDBACK_ON).run("MotorMove", POKE, approval_source="llm")
        assert res.success is True, res.error
        assert _InertMotorMove.calls == [POKE]

    def test_a_retract_with_feedback_on_is_still_refused(self):
        res = self._executor(FEEDBACK_ON).run("MotorMove", RETRACT, approval_source="llm")
        assert res.success is False
        assert "Safety validation failed" in (res.error or "")
        assert _InertMotorMove.calls == []


class TestAgentPath:
    """SafetyGate Layer 2 and the skill-tool wrapper behind it."""

    def _gate(self, args: dict, hw: HardwareState):
        from mast.agents._shared.safety_mw import SafetyGateMiddleware
        from mast.agents._shared.skill_adapter import wrap_skill

        mw = SafetyGateMiddleware(SafetyLimits(), get_state=lambda: hw)
        tool = wrap_skill(MotorMove, lambda: MagicMock(safe_call=lambda *a, **k: None))
        req = MagicMock()
        req.tool = tool
        req.tool_call = {"name": tool.name, "args": args, "id": "t-1", "type": "tool_call"}
        req.state = {}
        req.runtime = MagicMock()
        handler = MagicMock(return_value="ran")
        return mw.wrap_tool_call(req, handler), handler

    def test_layer2_lets_a_poke_with_feedback_on_through(self):
        result, handler = self._gate(POKE, FEEDBACK_ON)
        handler.assert_called_once()
        assert result == "ran"

    def test_layer2_still_refuses_a_retract_with_feedback_on(self):
        result, handler = self._gate(RETRACT, FEEDBACK_ON)
        handler.assert_not_called()
        assert "precondition_violation" in result.content

    def _tool_run(self, args: dict, hw: HardwareState):
        from mast.agents._shared.skill_adapter import wrap_skill

        ctx = MagicMock()
        ctx.state = _State(hw)
        ctx.check_abort.return_value = False
        tool = wrap_skill(_InertMotorMove, lambda: ctx)
        return tool.func(tool_call_id="t-1", state={}, **args)

    def test_the_tool_wrapper_runs_a_poke_with_feedback_on(self):
        out = self._tool_run(POKE, FEEDBACK_ON)
        content = out.update["messages"][0].content
        assert "precondition_failed" not in content, content
        assert _InertMotorMove.calls == [POKE]

    def test_the_tool_wrapper_still_refuses_a_retract_with_feedback_on(self):
        out = self._tool_run(RETRACT, FEEDBACK_ON)
        content = out.update["messages"][0].content
        assert "precondition_failed" in content and "z_controller_off" in content
        assert _InertMotorMove.calls == []


# ─────────────────────────────────────────────────────────────────────────
# 3. SAFE refuses an autonomous z-approach
# ─────────────────────────────────────────────────────────────────────────

class TestSafeMode:
    def _meta(self):
        return MotorMove().metadata()

    def test_the_decision(self):
        meta = self._meta()
        refusal = mode_refusal("MotorMove", meta, Z_APPROACH, OperatingMode.SAFE)
        assert refusal and refusal.startswith(_SAFE_KEY)
        assert "ApproachTip" in refusal and "AutoApproach" in refusal
        for mode in (OperatingMode.SEMI, OperatingMode.AUTO, None):
            assert mode_refusal("MotorMove", meta, Z_APPROACH, mode) is None

    @pytest.mark.parametrize("params", [RETRACT, LATERAL], ids=["retract", "lateral"])
    def test_safe_has_no_opinion_on_the_other_directions(self, params):
        # Retract is always allowed; a bare lateral move is Layer 0f's business,
        # in every mode — not the mode gate's.
        assert mode_refusal("MotorMove", self._meta(), params, OperatingMode.SAFE) is None

    @pytest.mark.parametrize("source", ["auto", "llm"])
    def test_execution_context_refuses_it_in_safe_and_leaves_no_notice(self, source):
        bind_mode_source(lambda: "safe")
        seq0 = _last_seq()
        res = _ctx(source, FEEDBACK_OFF).run("MotorMove", Z_APPROACH)
        assert res.success is False
        assert _SAFE_KEY in (res.error or "")
        assert _InertMotorMove.calls == [], "refused, so the motor never moved"
        assert _notices_since(seq0) == [], "no 'executed and notified' for a refusal"

    def test_semi_runs_it_and_notifies_once(self):
        bind_mode_source(lambda: "semi")
        seq0 = _last_seq()
        res = _ctx("llm", FEEDBACK_OFF).run("MotorMove", Z_APPROACH)
        assert res.success is True, res.error
        assert _InertMotorMove.calls == [Z_APPROACH]
        assert len(_notices_since(seq0)) == 1

    def test_the_human_path_is_not_mode_gated(self):
        bind_mode_source(lambda: "safe")
        res = _ctx("human", FEEDBACK_OFF).run("MotorMove", Z_APPROACH)
        assert res.success is True, res.error

    def test_a_retract_runs_in_safe(self):
        bind_mode_source(lambda: "safe")
        res = _ctx("llm", FEEDBACK_OFF).run("MotorMove", RETRACT)
        assert res.success is True, res.error

    def test_over_the_cap_the_cap_answers_first_in_every_mode(self):
        """An over-cap call is the cap's refusal even in SAFE — the bigger
        mistake is named, and AUTO gets the same answer."""
        assert AUTONOMOUS_Z_APPROACH_MAX_STEPS is not None
        over = {"direction": "z-approach", "steps": AUTONOMOUS_Z_APPROACH_MAX_STEPS + 1}
        for mode in ("safe", "semi", "auto"):
            bind_mode_source(lambda m=mode: m)
            res = _ctx("llm", FEEDBACK_OFF).run("MotorMove", over)
            assert res.success is False
            assert ("z_approach_step_cap_exceeded" in (res.error or "")
                    or _SAFE_KEY in (res.error or "")), res.error
        assert _InertMotorMove.calls == []


# ─────────────────────────────────────────────────────────────────────────
# The hard gates name the manual path that exists
# ─────────────────────────────────────────────────────────────────────────

def _named_confirm_skill(name: str) -> type[BaseSkill]:
    class _Skill(BaseSkill):
        def metadata(self) -> SkillMetadata:
            return SkillMetadata(name=name, version="1.0.0",
                                 category=SkillCategory.WRITE,
                                 safety_level=SafetyLevel.CONFIRM,
                                 description="", parameters=[])

        def execute(self, context, params: dict) -> SkillResult:  # pragma: no cover
            return SkillResult(skill_name=name, success=True)

    _Skill.__name__ = f"_Fake{name}"
    return _Skill


_HARD_GATES = [
    ("MotorMoveClosedLoop", {"target_z_m": 1e-6}),
    ("EnableSafeTip", {"enable": False}),
    ("SetBiasCalibration", {}),
    ("SetMotorFreqAmp", {"frequency_hz": 1000.0, "amplitude_v": 100.0}),
    ("MotorMove", {"direction": "x+", "steps": 1}),
]


def _says_the_real_manual_path(text: str) -> bool:
    return "Nanonis" in text and not any(old in text for old in _OLD_MANUAL_PATH)


def test_the_manual_path_sentence_names_nanonis_and_the_skill_page():
    s = hard_gate_manual_path()
    assert "Nanonis" in s
    assert ("技能页" in s) or ("skill page" in s)


@pytest.mark.parametrize("skill,params", _HARD_GATES, ids=[s for s, _ in _HARD_GATES])
def test_execution_context_hard_gates_name_nanonis(skill, params):
    extra = () if skill == "MotorMove" else (_named_confirm_skill(skill),)
    res = _ctx("llm", FEEDBACK_OFF, *extra).run(skill, params)
    assert res.success is False
    assert _says_the_real_manual_path(res.error or ""), res.error
    assert hard_gate_manual_path() in (res.error or "")


@pytest.mark.parametrize("skill,params", [g for g in _HARD_GATES
                                          if g[0] in ("MotorMoveClosedLoop",
                                                      "SetMotorFreqAmp", "MotorMove")],
                         ids=["MotorMoveClosedLoop", "SetMotorFreqAmp", "MotorMove-x+"])
def test_legacy_executor_hard_gates_name_nanonis(skill, params):
    extra = () if skill == "MotorMove" else (_named_confirm_skill(skill),)
    ex = SkillExecutor(pool=MagicMock(), registry=_registry(*extra),
                       safety=SafetyGuard(SafetyLimits()), state=_State(FEEDBACK_OFF))
    res = ex.run(skill, params, approval_source="llm")
    assert res.success is False
    assert _says_the_real_manual_path(res.error or ""), res.error


@pytest.mark.parametrize("skill,params,key", [
    ("MotorMoveClosedLoop", {"target_z_m": 1e-6}, "coarse_sample_approach_blocked"),
    ("EnableSafeTip", {"enable": False}, "protection_disable_blocked"),
    ("SetBiasCalibration", {}, "calibration_change_blocked"),
    ("SetMotorFreqAmp", {"frequency_hz": 1000.0, "amplitude_v": 100.0},
     "coarse_drive_change_blocked"),
    ("MotorMove", {"direction": "x+", "steps": 1}, "unguarded_lateral_coarse_move_blocked"),
], ids=["closed-loop-z", "protection", "calibration", "coarse-drive", "lateral"])
def test_agent_hard_gates_name_nanonis(skill, params, key):
    from mast.agents._shared.safety_mw import SafetyGateMiddleware
    from mast.agents._shared.skill_adapter import wrap_skill

    cls = MotorMove if skill == "MotorMove" else _named_confirm_skill(skill)
    if skill == "MotorMoveClosedLoop":
        from mast.skills.builtins.motor import MotorMoveClosedLoop as cls
    mw = SafetyGateMiddleware(SafetyLimits())
    tool = wrap_skill(cls, lambda: MagicMock(safe_call=lambda *a, **k: None))
    req = MagicMock()
    req.tool = tool
    req.tool_call = {"name": tool.name, "args": params, "id": "t-1", "type": "tool_call"}
    req.state = {}
    req.runtime = MagicMock()
    handler = MagicMock(return_value="ran")
    result = mw.wrap_tool_call(req, handler)
    handler.assert_not_called()
    assert key in result.content
    assert _says_the_real_manual_path(result.content), result.content


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
