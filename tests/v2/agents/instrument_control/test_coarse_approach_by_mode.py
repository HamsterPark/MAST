"""What each operating mode does with coarse approach.

An operator's natural question is whether safe mode leaves the coarse motor
alone. Nothing told them, and the one thing that spoke told them
wrong: the AUTO tooltip read "自动模式：全部允许（默认，无限制）". That was false,
and false in the direction that crashes a tip — it invited the operator to
believe auto mode lifts every limit on the coarse motor.

The truth since 7.3.3:

* An open-loop coarse Z step TOWARD the sample (``MotorMove`` with
  ``direction='z-approach'``) is refused on the autonomous path in **safe** — it
  is the step a tip poke is made of, and safe says the tip is not repaired
  (``mast.core.safety.mode_refusal``). In **semi** and **auto** it runs and
  notifies up to the per-call step cap
  ``mast.core.safety.AUTONOMOUS_Z_APPROACH_MAX_STEPS``.
* A call above the cap is refused in all three, by SafetyGate **Layer 0**, which
  runs before the operating-mode layer.
* A closed-loop coarse move with a Z target is refused in all three (Layer 0).
* ``AutoApproach`` / ``ApproachTip`` — current feedback, stop on tunnelling —
  are allowed in all three; stepping AWAY from the sample is never gated.

(History: before 7.3.2 the open-loop step was refused outright on every
autonomous path and the tooltip said 「…在三种模式下一律禁止，须用户手动执行」 —
a manual path no MAST button offered. In 7.3.2 it was the same in every mode,
and this file was ``test_coarse_approach_is_mode_independent``.)

The tooltip says exactly what the gates do. **A UI string asserting a safety
property is a claim about the backend, and a claim nobody checks is how the
last one rotted.** So the property is pinned here: the day someone changes what
a mode does with coarse approach, or changes the cap, this fails and they know
the UI has to change with it.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _find_mastv2_root() -> str:
    p = Path(__file__).resolve()
    while p.parent != p:
        if (p / "MASTv2").is_dir():
            return str(p / "MASTv2")
        p = p.parent
    raise RuntimeError("MASTv2 not found")


_MASTV2_ROOT = _find_mastv2_root()
if sys.path[0] != _MASTV2_ROOT:
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

from mast.core.safety import (  # noqa: E402
    AUTONOMOUS_Z_APPROACH_MAX_STEPS,
    is_coarse_sample_approach,
)

_MODES = ("safe", "semi", "auto")
_RUNS_IN = ("semi", "auto")


def _ui_claim() -> str:
    """The sentence the three tooltips carry (TopBar.tsx ``COARSE_NOTE``).

    Derived from the cap, so changing the cap without the tooltip fails here.
    With the cap switched off (``None``) the tooltip must say the call runs and
    notifies with no step limit, starting with this sentence."""
    cap = AUTONOMOUS_Z_APPROACH_MAX_STEPS
    head = "开环粗逼近（MotorMove z-approach）：安全模式下自主调用一律拒绝；"
    if cap is None:
        return head + "半自动和自动模式下自主调用照常执行并通知"
    return head + f"半自动和自动模式下自主调用每次最多 {cap} 步"


def _gate(mode: str, skill_cls, args: dict):
    """Run one agent tool call through SafetyGateMiddleware with *mode* bound.

    Returns ``(result, handler)``; ``handler.called`` means the gate let the
    call through to the skill."""
    from mast.agents._shared.safety_mw import SafetyGateMiddleware
    from mast.agents._shared.skill_adapter import wrap_skill
    from mast.config import SafetyLimits
    from mast.core import safety_escalation as esc

    # A coarse-approach refusal another test left live would gate the call at
    # Layer 0g, which is about the sequence, not about the mode.
    esc.clear_approach_refusal(owner=None)
    mw = SafetyGateMiddleware(SafetyLimits(), get_mode=lambda: mode)
    tool = wrap_skill(skill_cls, lambda: MagicMock(safe_call=lambda *a, **k: None))
    req = MagicMock()
    req.tool = tool
    req.tool_call = {"name": tool.name, "args": args, "id": "t-1", "type": "tool_call"}
    req.state = {}
    req.runtime = MagicMock()
    handler = MagicMock(return_value="ran")
    return mw.wrap_tool_call(req, handler), handler


class TestTheAnswerToTheOperatorsQuestion:
    def test_safe_mode_refuses_an_open_loop_z_approach(self):
        """Yes — safe mode does not step the coarse motor toward the sample on its
        own, not even one step. The refusal points at the feedback-stopped way."""
        from mast.skills.builtins.motor import MotorMove

        result, handler = _gate("safe", MotorMove, {"direction": "z-approach", "steps": 1})
        handler.assert_not_called()
        assert "safe_mode_tip_processing_blocked" in result.content
        assert "ApproachTip" in result.content
        assert result.status == "error"

    @pytest.mark.parametrize("mode", _RUNS_IN)
    def test_semi_and_auto_run_one_within_the_cap(self, mode):
        """The step a poke or a fine-Z re-centre needs passes in semi and auto."""
        from mast.skills.builtins.motor import MotorMove

        result, handler = _gate(mode, MotorMove, {"direction": "z-approach", "steps": 1})
        handler.assert_called_once()
        assert result == "ran"

    @pytest.mark.parametrize("mode", _MODES)
    def test_open_loop_z_approach_over_the_cap_is_refused_in_every_mode(self, mode):
        """The cap does not take a mode — no mode, auto included, lets a single
        autonomous call step further toward the sample blind. Layer 0 answers
        before the mode layer, so safe gets the cap's message too."""
        if AUTONOMOUS_Z_APPROACH_MAX_STEPS is None:
            pytest.skip("the autonomous z-approach step cap is switched off")
        from mast.skills.builtins.motor import MotorMove

        result, handler = _gate(mode, MotorMove, {
            "direction": "z-approach", "steps": AUTONOMOUS_Z_APPROACH_MAX_STEPS + 1})
        handler.assert_not_called()
        assert "z_approach_step_cap_exceeded" in result.content
        assert result.status == "error"

    @pytest.mark.parametrize("mode", _MODES)
    def test_closed_loop_coarse_z_is_refused_in_every_mode(self, mode):
        """A closed-loop coarse move with a Z target servos to a position and
        does not stop on contact — still the hard gate, in every mode."""
        from mast.skills.builtins.motor import MotorMoveClosedLoop

        result, handler = _gate(mode, MotorMoveClosedLoop, {"target_z_m": 1e-6})
        handler.assert_not_called()
        assert "coarse_sample_approach_blocked" in result.content

    @pytest.mark.parametrize("mode", _MODES)
    def test_retracting_runs_in_every_mode(self, mode):
        """The coarse Z rules are directional: stepping AWAY from the sample is
        always fine. A gate on retraction would strand the tip."""
        from mast.skills.builtins.motor import MotorMove

        result, handler = _gate(mode, MotorMove, {"direction": "z-retract", "steps": 50})
        handler.assert_called_once()
        assert is_coarse_sample_approach(
            "MotorMove", {"direction": "z-retract", "steps": 500}) is False

    def test_auto_approach_is_a_different_thing_and_stays_allowed(self):
        """AutoApproach is current-feedback — it stops itself on tunnelling. It is
        NOT what the coarse Z rules gate, and the tooltip must not imply the tip
        can never be engaged autonomously."""
        assert is_coarse_sample_approach("AutoApproach", {}) is False
        assert is_coarse_sample_approach("ApproachTip", {}) is False


def test_the_ui_says_what_the_backend_does():
    """The tooltip is the ONLY place the operator can learn this, so its text is
    load-bearing. Pin the sentence itself: if someone softens it, changes the cap
    without it, or drops it back to "无限制", the mismatch fails here rather than
    at the tip."""
    top_bar = (Path(_MASTV2_ROOT).parent / "frontend" / "src" / "components"
               / "shell" / "TopBar.tsx").read_text(encoding="utf-8")

    assert _ui_claim() in top_bar, (
        "the coarse-approach note is gone from the operating-mode tooltips, or it "
        "no longer states what safe mode does and the step cap in mast.core.safety "
        "— the operator has no way left to learn what each mode does with the "
        "coarse motor")

    # Only the tooltip STRINGS, not the file: a comment quoting the old text
    # (which is exactly what this file's post-mortem does) is not a lie in the UI.
    # A tooltip may be a bilingual `title: tr(\n  zh,\n  en,\n)` spanning lines; take
    # the whole call so both language variants are checked.
    lines = top_bar.splitlines()
    titles = []
    for i, ln in enumerate(lines):
        if not ln.strip().startswith("title:"):
            continue
        block = ln
        if ln.strip() == "title: tr(":
            j = i + 1
            while j < len(lines) and lines[j].strip() != "),":
                block += "\n" + lines[j]
                j += 1
        titles.append(block)
    assert len(titles) == 3, f"expected 3 mode tooltips, found {len(titles)}"
    for t in titles:
        assert "COARSE_NOTE" in t, (
            f"this mode's tooltip dropped the coarse-approach note: {t.strip()}")
        assert "无限制" not in t, (
            f"a tooltip claims 无限制 while coarse Z is still step-capped and the "
            f"closed-loop Z move still refused in every mode — it is telling the "
            f"operator the opposite of the truth: {t.strip()}")
        assert "需人工确认" not in t and "manual confirmation" not in t, (
            f"a tooltip still says something waits for a human confirmation; "
            f"nothing in the mode gate does: {t.strip()}")
