"""ExecutionContext.run enforces global SafetyGate bounds on composite sub-skills.

审查: composites invoked sub-skills with per-step params
that bypassed the global numeric caps. ExecutionContext.run now applies them.

修复项 2026-06-11: ExecutionContext.run also enforces the APPROVAL GATE at this
leaf call point. Previously this path bypassed the gate entirely
(SafetyGateMiddleware sees only the composite's top-level tool call;
SkillExecutor guards only the manual path). What the gate does there has
changed twice since: a baseline-DANGEROUS sub-step runs and notifies, and an
open-loop MotorMove direction='z-approach' runs and notifies within the
autonomous per-call step cap and is refused above it. A closed-loop coarse
move with a Z target stays a hard gate.

Run from repo root:
    .venv-v2-py313/Scripts/python.exe -m pytest tests/v2/unit/test_composite_bounds.py -x -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

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

from mast.core.execution_context import ExecutionContext
from mast.core.registry import SkillRegistry
from mast.core.types import (
    ParameterSpec, SafetyLevel, SkillCategory, SkillMetadata, SkillResult,
)
from mast.skills.base import BaseSkill


class FakeSetBias(BaseSkill):
    """bias_v param with NO per-skill cap — only the GLOBAL bound can catch it."""
    def metadata(self):
        return SkillMetadata(
            name="FakeSetBias", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM, description="",
            parameters=[ParameterSpec(name="bias_v", type="float", unit="V",
                                      required=True)],  # no min/max here
        )
    def execute(self, ctx, params):
        return SkillResult(skill_name="FakeSetBias", success=True, data=params)


def _ctx():
    reg = SkillRegistry()
    reg.register(FakeSetBias)
    return ExecutionContext(pool=None, state=None, registry=reg)


def test_out_of_bounds_subskill_rejected():
    res = _ctx().run("FakeSetBias", {"bias_v": 1_000_000.0})  # 1 MV — absurd
    assert res.success is False
    assert "global safety bounds" in res.error


def test_in_bounds_subskill_passes():
    res = _ctx().run("FakeSetBias", {"bias_v": 0.5})
    assert res.success is True
    assert res.data["bias_v"] == 0.5


# ── 修复项 approval gate at the composite sub-step leaf point ─────────────────

class FakeDangerous(BaseSkill):
    def metadata(self):
        return SkillMetadata(
            name="FakeDangerous", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.DANGEROUS, description="", parameters=[],
        )
    def execute(self, ctx, params):
        return SkillResult(skill_name="FakeDangerous", success=True, data={})


def _gate_ctx(approval_source: str = "auto"):
    from mast.skills.builtins.motor import MotorMove
    reg = SkillRegistry()
    reg.register(MotorMove)
    reg.register(FakeDangerous)
    return ExecutionContext(
        pool=None, state=None, registry=reg, approval_source=approval_source,
    )


# ── 这一节里**两种「要人」被拆开了** ─────────────────────────
#
# 原来五条测试都对着同一句 "Human approval required",因为代码里也确实是同一个
# ``required = "human"``:参数条件硬闸(z-approach)和基线 ``safety_level=DANGEROUS``
# 混在一起,一律拒绝并让人「去审批面板」。
#
# 审批面板对 DANGEROUS 技能已经不再产生任何东西(确认框整条链路割掉了),所以那条
# 拒绝语从此指向一扇不存在的门 —— 而 agent 会照着它反复重试(「一个错的路标比没有
# 路标更坏」)。拆开之后:
#
#   * **硬闸**(带 Z 目标的闭环粗动 / 粗动驱动电压 / 裸横向粗动)照旧拒绝,而且
#     拒绝语明说「这不是等待审批,没有任何批准会到来」;出路是用户走手动路径;
#     开环 z-approach 原来也在这一组,现在改成步数上限内照跑并通知、
#     超过上限整条拒绝(见下面那一节);
#   * **基线 DANGEROUS** 转成「执行 + 留痕 + 通知」,与 agent 顶层路径同口径 ——
#     同一个技能不该因为「是不是被复合技能调用的」而有两种安全语义。
#
# 断言因此分成两组,而不是继续共用一个字符串。

_HARD_GATE_MARK = "硬闸拒绝"          # 「硬闸拒绝」


# ── 开环 z-approach 子步骤:上限内照跑并通知,超过整条拒绝 ──────
#
# 原来这里是 ``test_coarse_z_approach_subskill_blocked`` 与
# ``test_coarse_z_approach_blocked_even_with_llm_source``:steps=10 的 z-approach
# 子步骤一律硬闸拒绝(「没有任何批准会到来」),llm 来源也一样。现在:上限内过闸
# (之后在 execute 处因 pool=None 失败,证明它过了闸)并留一行 notice_only;超过
# 上限整条拒绝,拒绝语以 ``z_approach_step_cap_exceeded`` 开头,而且不是硬闸那句
# —— 拆成小步就能做,不该再说「换参数也绕不过去」。

_CAP_KEY = "z_approach_step_cap_exceeded"


def _cap() -> int:
    from mast.core.safety import AUTONOMOUS_Z_APPROACH_MAX_STEPS

    assert AUTONOMOUS_Z_APPROACH_MAX_STEPS is not None, (
        "these tests pin the capped behaviour; if the cap is removed, rewrite them")
    return AUTONOMOUS_Z_APPROACH_MAX_STEPS


def _last_seq() -> int:
    from mast.core import diagnostics as diag

    rows = diag.recent(1)
    return int(rows[0].get("seq", 0)) if rows else 0


def _notices_since(seq0: int, subject: str) -> list[dict]:
    """``seq`` 是进程内单调递增的,按它切比数「最近 200 行」稳:台账满了以后
    窗口会滑动,前后两次计数可能恰好抵消。"""
    from mast.core import diagnostics as diag

    return [r for r in diag.recent(200, kinds=("notice_only",))
            if r.get("seq", 0) > seq0 and r.get("subject") == subject]


@pytest.mark.parametrize("source", ["auto", "llm"])
def test_coarse_z_approach_subskill_within_cap_runs_and_is_noticed(source):
    seq0 = _last_seq()
    res = _gate_ctx(source).run(
        "MotorMove", {"direction": "z-approach", "steps": _cap()})
    err = res.error or ""
    assert _HARD_GATE_MARK not in err, "开环 z-approach 子步骤又成了硬闸"
    assert _CAP_KEY not in err, "上限之内的步数被当成了超限"
    rows = _notices_since(seq0, "MotorMove")
    assert len(rows) == 1, "跑了却没留下通知(或者留了不止一行)"
    assert rows[0]["approval_source"] == source
    assert "z-approach" in rows[0]["reason"]


@pytest.mark.parametrize("source", ["auto", "llm"])
def test_coarse_z_approach_subskill_over_cap_refused(source):
    seq0 = _last_seq()
    res = _gate_ctx(source).run(
        "MotorMove", {"direction": "z-approach", "steps": _cap() + 1})
    assert res.success is False
    assert _CAP_KEY in res.error
    assert _HARD_GATE_MARK not in res.error
    assert _notices_since(seq0, "MotorMove") == [], (
        "没有执行的调用不该留下「已执行并通知」")


def test_closed_loop_coarse_z_subskill_still_hard_gated():
    """带 Z 目标的闭环粗动仍是硬闸:位置反馈,碰到样品不会停。"""
    from mast.skills.builtins.motor import MotorMoveClosedLoop

    reg = SkillRegistry()
    reg.register(MotorMoveClosedLoop)
    ctx = ExecutionContext(pool=None, state=None, registry=reg,
                           approval_source="llm")
    res = ctx.run("MotorMoveClosedLoop", {"target_z_m": 1e-6})
    assert res.success is False
    assert _HARD_GATE_MARK in res.error
    assert "没有任何批准会到来" in res.error, (
        "拒绝语必须说清「这不是在等审批」——审批面板已经不会为它产生任何东西，"
        "指向一扇不存在的门比不给提示更坏")


def test_z_retract_subskill_passes_gate():
    """Retract (away from sample) is safe — gate must not fire. The call then
    fails later at execute (pool=None), which proves it got past the gate."""
    res = _gate_ctx().run("MotorMove", {"direction": "z-retract", "steps": 10})
    assert _HARD_GATE_MARK not in (res.error or "")


def test_dangerous_subskill_now_runs_and_is_noticed():
    """**语义反转的正主。** 原名 ``test_dangerous_subskill_blocked_without_human``。

    基线 DANGEROUS 的子步骤不再被拒 —— 它跑,并且在诊断台账里留一行。
    通知那一半必须一起钉:少了它,这条改动就只是「悄悄放开」。"""
    from mast.core import diagnostics as diag

    before = len([r for r in diag.recent(200, kinds=("notice_only",))
                  if r.get("subject") == "FakeDangerous"])
    res = _gate_ctx().run("FakeDangerous", {})
    assert res.success is True, "基线 DANGEROUS 子步骤又被拒了"
    rows = [r for r in diag.recent(200, kinds=("notice_only",))
            if r.get("subject") == "FakeDangerous"]
    assert len(rows) == before + 1, "跑了却没留下通知"
    assert rows[0]["composite_substep"] is True
    assert "DANGEROUS" in rows[0]["reason"]


def test_dangerous_subskill_runs_with_human_source():
    res = _gate_ctx("human").run("FakeDangerous", {})
    assert res.success is True


def test_a_human_context_does_not_get_a_pointless_notice():
    """approval_source=human ⇒ 本来就有人授权,不该再报「本来会等人批准」。

    这条是通知的**另一侧**:只钉「该通知时通知了」的话,一个恒真的判据也会绿。"""
    from mast.core import diagnostics as diag

    before = len([r for r in diag.recent(200, kinds=("notice_only",))
                  if r.get("subject") == "FakeDangerous"])
    _gate_ctx("human").run("FakeDangerous", {})
    after = len([r for r in diag.recent(200, kinds=("notice_only",))
                 if r.get("subject") == "FakeDangerous"])
    assert after == before


def test_coarse_z_approach_human_source_is_not_capped_and_not_noticed():
    """原名 ``test_coarse_z_approach_passes_gate_with_human_source``(steps=10)。
    人工手动路径不受自主步数上限约束,也不该收到「本来会等人批准」的通知。"""
    seq0 = _last_seq()
    res = _gate_ctx("human").run(
        "MotorMove", {"direction": "z-approach", "steps": _cap() * 5})
    err = res.error or ""
    assert _HARD_GATE_MARK not in err
    assert _CAP_KEY not in err
    assert _notices_since(seq0, "MotorMove") == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
