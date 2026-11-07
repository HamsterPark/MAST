"""A composite ``human`` node on the direct / external path asks a real operator.

Before: ``direct_exec`` turned the node's ``interrupt()`` into ``refused_by="needs_human_node"`` and
the external agent could only give up. Now ``run_and_record`` opens a scope, ``human_channel``
maps the node to a structured prompt and blocks for the answer — bounded, cancellable, and ending
in an explicit failure (not a hang, not a silent skip) when nobody decides.

The LangGraph path must not change: without a scope ``human_channel`` is the old function.

Only the optical station waits for a person. The scope ``direct_exec`` opens
carries ``human_nodes``: True on an optical station, False on an STM station, where a ``human``
node keeps exactly what it did before the operator prompts existed - the old function, which
answers "no channel" at once (no prompt, no wait, no popup, no token held for a person who may not
be there). The profile comes from ``mast.station`` (``MAST_STATION_PROFILE`` is read live), so the
tests below that expect the node to wait run under the optical profile and the ones that pin the
old behaviour under ``stm``.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from mast.i18n import tr
from mast.operator_hitl import human_node, prompts
from mast.skills.composite import graph_executor as ge_mod
from mast.skills.composite import interpreter as interp_mod
from mast.skills.composite._base import AbortRequested
from mast.skills.composite.spec import CompositeSpec

PAYLOAD = {"kind": "workflow_human", "workflow": "SwapPlate", "node_id": "swap",
           "decision_key": "run-1:swap", "message": "Swap the λ/2 to the 1064 plate",
           "inputs": {"target_nm": 1064, "plates": ("a", "b")}, "routes": ["done", "cancel"]}


@pytest.fixture(autouse=True)
def _memory(memory_backends):
    yield


def scope(**kw) -> human_node.HumanScope:
    kw.setdefault("check_abort", None)
    kw.setdefault("timeout_s", 5.0)
    return human_node.HumanScope(**kw)


def wait_for_open(timeout=5.0) -> prompts.OperatorPrompt:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = prompts.list_open()
        if found:
            return found[0]
        time.sleep(0.01)
    raise AssertionError("no prompt was posted")


def in_thread(fn, *args, **kw):
    box: dict = {}

    def run():
        try:
            box["value"] = fn(*args, **kw)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


# ── decide() ────────────────────────────────────────────────────────────────


def test_routes_become_a_choice_and_the_chosen_route_comes_back():
    with prompts.FakeOperator(lambda p: {"choice": "done", "note": "swapped it"}) as op:
        decision = human_node.decide(PAYLOAD, scope(skill="SHGExcitationScan", agent_id="ext:tester",
                                                    job_id="j_000000000001"))
    assert decision == {"route": "done", "note": "swapped it", "by": "test-operator"}
    (p,) = op.seen
    assert p.kind == "choice" and p.severity == "action"
    assert [(o.id, o.label) for o in p.options] == [("done", "done"), ("cancel", tr("取消", "Cancel"))]
    assert p.message == PAYLOAD["message"] and "SwapPlate" in p.title
    assert (p.job_id, p.skill, p.agent_id) == ("j_000000000001", "SHGExcitationScan", "ext:tester")
    assert p.context["workflow"] == "SwapPlate" and p.context["node_id"] == "swap"
    assert p.context["decision_key"] == "run-1:swap" and p.context["routes"] == ["done", "cancel"]
    assert p.context["inputs"] == {"target_nm": 1064, "plates": ["a", "b"]}
    json.dumps(p.to_dict())                                      # the prompt is storable as it is
    assert prompts.get(p.id).status == "answered"
    assert p.timeout_s == 5.0, "the effective limit is on the prompt"


def test_repeated_routes_and_oversized_text_do_not_make_the_prompt_invalid():
    """A node's own text is clipped, never a reason for the run to fail on a validation error."""
    payload = {**PAYLOAD, "routes": ["done", "cancel", "done", "cancel"], "message": "m" * 20_000,
               "inputs": {f"k{i}": "v" * 400 for i in range(40)}}
    with prompts.FakeOperator(lambda p: {"choice": "done"}) as op:
        assert human_node.decide(payload, scope())["route"] == "done"
    p = op.seen[0]
    assert [o.id for o in p.options] == ["done", "cancel"]
    assert len(p.message) == prompts.MAX_MESSAGE_CHARS
    assert p.context["inputs"]["_truncated"] is True and len(p.context["inputs"]["keys"]) == 30


def test_a_laser_or_attestation_node_is_a_safety_prompt():
    payload = {**PAYLOAD, "node_id": "laser_attest", "routes": ["laser_on", "laser_off", "cancel"]}
    with prompts.FakeOperator(lambda p: {"choice": "laser_off"}) as op:
        assert human_node.decide(payload, scope())["route"] == "laser_off"
    assert op.seen[0].severity == "safety"
    assert [o.label for o in op.seen[0].options][:2] == [tr("激光已打开", "The laser is on"),
                                                         tr("激光已关闭", "The laser is off")]
    payload = {**PAYLOAD, "severity": "info"}
    with prompts.FakeOperator(lambda p: {"choice": "done"}) as op:
        human_node.decide(payload, scope())
    assert op.seen[0].severity == "info", "a payload may say how loud it is"


def test_a_node_with_one_route_is_a_yes_no_confirmation():
    payload = {**PAYLOAD, "routes": ["resolved"]}
    with prompts.FakeOperator(lambda p: {"choice": "yes"}) as op:
        assert human_node.decide(payload, scope())["route"] == "resolved"
    assert op.seen[0].kind == "confirm"
    payload = {k: v for k, v in PAYLOAD.items() if k != "routes"}          # the spec default
    with prompts.FakeOperator(lambda p: {"choice": "yes"}):
        assert human_node.decide(payload, scope())["route"] == "resolved"


def test_answering_no_to_a_confirmation_stops_the_run_explicitly():
    payload = {**PAYLOAD, "routes": ["resolved"]}
    with prompts.FakeOperator(lambda p: {"choice": "no"}):
        with pytest.raises(human_node.HumanNodeFailed) as exc:
            human_node.decide(payload, scope())
    assert exc.value.status == "declined" and exc.value.prompt_id
    assert "SwapPlate" in str(exc.value) and exc.value.prompt_id in str(exc.value)


def test_no_answer_in_time_ends_with_an_explicit_failure_and_closes_the_prompt():
    t0 = time.monotonic()
    with pytest.raises(human_node.HumanNodeFailed) as exc:
        human_node.decide({**PAYLOAD, "timeout_s": 0.2}, scope())
    assert time.monotonic() - t0 < 3
    e = exc.value
    assert e.status == "timeout"
    assert "0.2" in str(e) and e.prompt_id in str(e)
    assert tr("没有执行", "Nothing after this node ran") in str(e)
    assert prompts.get(e.prompt_id).status == "timeout" and prompts.list_open() == []


def test_the_wait_is_bounded_even_if_the_node_asks_for_a_year():
    assert human_node.clamp_timeout(86400 * 365) == human_node.MAX_TIMEOUT_S == 4 * 3600
    assert human_node.clamp_timeout(None) == human_node.DEFAULT_TIMEOUT_S == 1800
    for bad in (0, -5, float("nan"), float("inf"), "soon"):
        assert human_node.clamp_timeout(bad) == human_node.DEFAULT_TIMEOUT_S, bad
    assert human_node.clamp_timeout(12) == 12.0
    t, box = in_thread(human_node.decide, {**PAYLOAD, "timeout_s": 10 ** 9}, scope(timeout_s=10 ** 9))
    p = wait_for_open()
    assert p.timeout_s == human_node.MAX_TIMEOUT_S
    prompts.cancel(p.id, "test over")
    t.join(5)
    assert isinstance(box["error"], human_node.HumanNodeFailed) and box["error"].status == "cancelled"


def test_a_dismissed_prompt_is_a_failure_that_says_who_dismissed_it():
    t, box = in_thread(human_node.decide, PAYLOAD, scope())
    p = wait_for_open()
    prompts.dismiss(p.id, by="alice", source="web", reason="the plate is stuck")
    t.join(5)
    e = box["error"]
    assert isinstance(e, human_node.HumanNodeFailed) and e.status == "dismissed"
    assert "the plate is stuck" in str(e)


def test_a_prompt_cancelled_by_someone_else_is_a_failure_too():
    t, box = in_thread(human_node.decide, PAYLOAD, scope())
    p = wait_for_open()
    prompts.cancel(p.id, "service restarted")
    t.join(5)
    assert box["error"].status == "cancelled" and "service restarted" in str(box["error"])


def test_an_operator_stop_ends_the_wait_at_once_and_withdraws_the_prompt():
    stop = threading.Event()
    t, box = in_thread(human_node.decide, PAYLOAD, scope(check_abort=stop.is_set))
    p = wait_for_open()
    stop.set()
    t.join(5)
    assert isinstance(box["error"], AbortRequested), box
    assert prompts.get(p.id).status == "cancelled", "the operator's card disappears"
    assert prompts.get_backend().get_answer(p.id).note == "aborted by the caller"


def test_a_run_that_is_already_stopped_asks_nobody():
    stop = threading.Event()
    stop.set()
    with pytest.raises(AbortRequested):
        human_node.decide(PAYLOAD, scope(check_abort=stop.is_set))
    assert prompts.get_backend().snapshot("all", 10)[1] == [], "no prompt flashed up for nothing"


def test_a_check_abort_that_raises_propagates_and_withdraws_the_prompt():
    class Stopped(Exception):
        pass

    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] > 2:
            raise Stopped()
        return False

    t, box = in_thread(human_node.decide, PAYLOAD, scope(check_abort=check))
    t.join(5)
    assert isinstance(box["error"], Stopped)
    assert prompts.list_open() == []


# ── human_channel and the scope ─────────────────────────────────────────────


def test_without_a_scope_human_channel_is_the_old_function():
    """No scope = the LangGraph path (or no graph at all): outside a graph runtime the old
    function answers ``None`` — no prompt, no waiting."""
    assert human_node.current_scope() is None
    assert interp_mod.human_channel(PAYLOAD) is None
    assert prompts.get_backend().snapshot("all", 10)[1] == []


def test_inside_a_scope_human_channel_asks_the_operator():
    with prompts.FakeOperator(lambda p: {"choice": "done"}):
        with human_node.direct_scope(check_abort=None, skill="X", timeout_s=5):
            assert human_node.current_scope().skill == "X"
            decision = interp_mod.human_channel(PAYLOAD)
    assert decision["route"] == "done"
    assert human_node.current_scope() is None, "the scope ends with the block"


def test_the_scope_does_not_leak_into_other_threads():
    seen = []
    with human_node.direct_scope(check_abort=None, skill="X"):
        t = threading.Thread(target=lambda: seen.append(human_node.current_scope()))
        t.start()
        t.join()
    assert seen == [None], "a thread the skill starts itself falls back to the old behaviour"


def test_scopes_nest_and_restore():
    with human_node.direct_scope(check_abort=None, skill="outer"):
        with human_node.direct_scope(check_abort=None, skill="inner"):
            assert human_node.current_scope().skill == "inner"
        assert human_node.current_scope().skill == "outer"
    assert human_node.current_scope() is None


# ── a real composite spec ───────────────────────────────────────────────────

SPEC_NODES = [
    {"type": "step", "id": "prep", "skill": "Prep", "params": {}},
    {"type": "human", "id": "ask", "message": "是否换好了 λ/2（目标 {nm} nm）？",
     "inputs": {"nm": 1064},
     "routes": {"go": [{"type": "step", "id": "measure", "skill": "Measure", "params": {}}],
                "stop": [{"type": "step", "id": "park", "skill": "Park", "params": {}}]}},
]


class _Res:
    def __init__(self):
        self.success, self.data, self.error, self.nanonis_calls = True, {}, "", []


class _Ctx:
    def __init__(self, abort=None):
        self.calls = []
        self._abort = abort

    def run(self, skill_name, params, version=None):
        self.calls.append(skill_name)
        return _Res()

    def check_abort(self):
        return bool(self._abort is not None and self._abort.is_set())


@pytest.fixture
def sidecars(tmp_path, monkeypatch):
    d = tmp_path / "composite_progress"
    d.mkdir()
    monkeypatch.setattr(ge_mod, "_sidecar_path",
                        lambda name, run_id="": d / (f"{name}__{run_id}.json" if run_id else f"{name}.json"))
    monkeypatch.setattr(ge_mod, "_sidecar_dir", lambda: d)
    return d


def _run_spec(ctx):
    from mast.skills.composite.interpreter import SpecComposite

    spec = CompositeSpec(name="HumanDirect", safety_level="confirm", params=[],
                         nodes=json.loads(json.dumps(SPEC_NODES)))
    return SpecComposite(spec).execute(ctx, {})


@pytest.mark.parametrize("route, ran, not_ran", [("go", "Measure", "Park"), ("stop", "Park", "Measure")])
def test_the_operators_route_decides_which_branch_runs(sidecars, route, ran, not_ran):
    ctx = _Ctx()
    with prompts.FakeOperator(lambda p: {"choice": route}) as op:
        with human_node.direct_scope(check_abort=ctx.check_abort, skill="HumanDirect", timeout_s=5):
            res = _run_spec(ctx)
    assert res.success, res.error
    assert ctx.calls == ["Prep", ran] and not_ran not in ctx.calls
    assert "1064" in op.seen[0].message, "the node's message template is filled in"
    assert [o.id for o in op.seen[0].options] == ["go", "stop"]


def test_the_composite_stops_where_the_operator_did_not_decide(sidecars):
    ctx = _Ctx()
    with human_node.direct_scope(check_abort=ctx.check_abort, skill="HumanDirect", timeout_s=0.2):
        with pytest.raises(human_node.HumanNodeFailed, match=tr("没有执行", "Nothing after this node ran")):
            _run_spec(ctx)
    assert ctx.calls == ["Prep"], "nothing after the human node ran"


def test_an_operator_stop_aborts_the_composite_like_any_other_abort(sidecars):
    stop = threading.Event()
    ctx = _Ctx(stop)
    box = {}

    def run():
        with human_node.direct_scope(check_abort=ctx.check_abort, skill="HumanDirect", timeout_s=30):
            box["res"] = _run_spec(ctx)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    p = wait_for_open()
    stop.set()
    t.join(5)
    res = box["res"]
    assert res.success is False and "abort" in (res.error or "").lower(), res
    assert ctx.calls == ["Prep"]
    assert prompts.get(p.id).status == "cancelled"


def test_an_answered_node_is_not_asked_again_on_a_replay(sidecars):
    asked = []

    def policy(p):
        asked.append(p.id)
        return {"choice": "go"}

    ctx = _Ctx()
    with prompts.FakeOperator(policy):
        with human_node.direct_scope(check_abort=None, skill="HumanDirect", timeout_s=5):
            from mast.skills.composite.graph_executor import GraphExecutor
            from mast.skills.composite.interpreter import SpecComposite

            spec = CompositeSpec(name="HumanDirect", safety_level="confirm", params=[],
                                 nodes=json.loads(json.dumps(SPEC_NODES)))
            ex = GraphExecutor(spec.name, ctx)
            ex.progress.partial_data["_human"] = {"ask": {"route": "stop", "note": ""}}
            ex.run_plan(SpecComposite(spec).plan_dynamic({}, ex))
    assert asked == [] and "Park" in ctx.calls, "the cached decision is used; the operator is not bothered twice"


# ── the sink: how a job learns it is waiting ─────────────────────────────────


def test_the_wait_sink_hears_the_prompt_id_and_then_that_the_wait_is_over():
    events = []
    with human_node.wait_sink(events.append):
        with prompts.FakeOperator(lambda p: {"choice": "done"}):
            human_node.decide(PAYLOAD, scope())
    assert [bool(e) for e in events] == [True, False]
    assert events[0]["prompt_id"].startswith("op-") and events[0]["kind"] == "choice"
    assert events[0]["title"] and events[0]["since"].endswith("+00:00")
    assert events[1] is None


def test_the_sink_is_told_the_wait_is_over_even_when_it_ends_badly():
    events = []
    with human_node.wait_sink(events.append):
        with pytest.raises(human_node.HumanNodeFailed):
            human_node.decide({**PAYLOAD, "timeout_s": 0.1}, scope())
    assert events[-1] is None and events[0]["prompt_id"]


def test_a_sink_that_raises_does_not_break_the_wait():
    def bad(info):
        raise RuntimeError("journal is full")

    with human_node.wait_sink(bad):
        with prompts.FakeOperator(lambda p: {"choice": "done"}):
            assert human_node.decide(PAYLOAD, scope())["route"] == "done"


# ── through direct_exec ─────────────────────────────────────────────────────


def _direct_world(monkeypatch, profile="optical"):
    monkeypatch.setenv("MAST_STATION_PROFILE", profile)
    from mast.api import direct_exec
    from mast.core.registry import SkillRegistry
    from mast.core.types import (
        HardwareState,
        SafetyLevel,
        SkillCategory,
        SkillMetadata,
        SkillResult,
    )
    from mast.skills.base import BaseSkill
    import mast.logging.experiment_log as el

    class HumanProbe(BaseSkill):
        def metadata(self):
            return SkillMetadata(name="HumanProbe", version="1.0.0", category=SkillCategory.READ,
                                 safety_level=SafetyLevel.AUTO, description="probe", tags=["read"])

        def execute(self, context, params):
            decision = interp_mod.human_channel(
                {"kind": "workflow_human", "workflow": "HumanProbe", "node_id": "n1",
                 "message": "ok to go on?", "routes": ["go", "stop"]})
            return SkillResult(skill_name="HumanProbe", success=True, data={"decision": decision})

    class State:
        def snapshot(self):
            return HardwareState(bias_v=0.1)

    monkeypatch.setattr(el, "get_active_log",
                        lambda: SimpleNamespace(current_experiment_id=None, current_sample_id=None))
    reg = SkillRegistry()
    reg.register(HumanProbe)
    abort = threading.Event()
    ctx = SimpleNamespace(connection_pool=object(), state=State(), skill_registry=reg)
    ec, missing = direct_exec.build_context(ctx, owner="外部 agent ext:tester", extra_aborts=[abort])
    assert not missing
    return direct_exec, ec, abort


def test_run_and_record_lets_the_skills_human_node_ask_the_operator(monkeypatch):
    direct_exec, ec, _ = _direct_world(monkeypatch)
    with prompts.FakeOperator(lambda p: {"choice": "go", "note": "yes please"}) as op:
        run = direct_exec.run_and_record(ec, "HumanProbe", {}, agent_id="ext:tester",
                                         tool_call_id="j_00000000abcd")
    assert run.success, run.error
    assert run.refused_by is None, "no longer needs_human_node"
    assert run.data["decision"]["route"] == "go" and run.data["decision"]["note"] == "yes please"
    (p,) = op.seen
    assert (p.agent_id, p.job_id, p.skill) == ("ext:tester", "j_00000000abcd", "HumanProbe")
    assert human_node.current_scope() is None, "the scope does not outlive the run"


def test_cancelling_the_run_withdraws_the_prompt_and_ends_the_job_cleanly(monkeypatch):
    direct_exec, ec, abort = _direct_world(monkeypatch)
    t, box = in_thread(direct_exec.run_and_record, ec, "HumanProbe", {}, agent_id="ext:tester",
                       tool_call_id="j_1")
    p = wait_for_open()
    from mast.core.execution_context import mark_abort

    mark_abort(abort, "cancelled by the agent")
    t.join(8)
    run = box["value"]
    assert run.success is False and "Abort" in run.error
    assert prompts.get(p.id).status == "cancelled" and prompts.list_open() == []


def test_a_graph_interrupt_that_is_not_a_human_channel_call_is_still_needs_human_node(monkeypatch):
    """Only ``human_channel`` is bridged: any other ``GraphInterrupt`` keeps the old readable failure."""
    direct_exec, ec, _ = _direct_world(monkeypatch)
    from langgraph.errors import GraphInterrupt
    from mast.core.types import SafetyLevel, SkillCategory, SkillMetadata
    from mast.skills.base import BaseSkill

    class Interrupting(BaseSkill):
        def metadata(self):
            return SkillMetadata(name="Interrupting", version="1.0.0", category=SkillCategory.READ,
                                 safety_level=SafetyLevel.AUTO, description="x", tags=["read"])

        def execute(self, context, params):
            raise GraphInterrupt(())

    ec._registry.register(Interrupting)
    run = direct_exec.run_and_record(ec, "Interrupting", {})
    assert run.refused_by == "needs_human_node" and run.success is False


# ── only the optical station waits for a person ──────────────────────────────────────────────


def test_a_scope_says_whether_the_nodes_of_its_run_wait_for_a_person():
    assert human_node.HumanScope(check_abort=None).human_nodes is True
    with human_node.direct_scope(check_abort=None, skill="X") as s:
        assert s.human_nodes is True, "the default: ask"
    with human_node.direct_scope(check_abort=None, skill="X", job_id="j_1", agent_id="ext:a",
                                 human_nodes=False) as s:
        assert human_node.current_scope() is s and s.human_nodes is False
        assert (s.skill, s.job_id, s.agent_id) == ("X", "j_1", "ext:a"), "the rest of the scope is there either way"


def test_a_scope_without_human_nodes_leaves_the_node_to_the_old_function():
    """STM: the node is answered exactly as without any scope - "no channel", at once."""
    with human_node.direct_scope(check_abort=None, skill="X", timeout_s=3, human_nodes=False):
        t0 = time.monotonic()
        assert interp_mod.human_channel(PAYLOAD) is None           # a node that wrongly waited would end in 3 s
        assert time.monotonic() - t0 < 2.5
    assert prompts.get_backend().snapshot("all", 10)[1] == [], "no prompt was posted"


def test_a_scope_without_a_human_nodes_attribute_does_not_wait_either():
    """A stand-in scope from an older caller: the safe reading is the old behaviour."""
    old_style = SimpleNamespace(check_abort=None, skill="X", agent_id=None, job_id=None, timeout_s=5.0)
    token = human_node._SCOPE.set(old_style)
    try:
        assert interp_mod.human_channel(PAYLOAD) is None
    finally:
        human_node._SCOPE.reset(token)
    assert prompts.get_backend().snapshot("all", 10)[1] == []


def test_a_real_composite_on_an_stm_station_stops_at_its_human_node_at_once(sidecars):
    ctx = _Ctx()
    t0 = time.monotonic()
    with human_node.direct_scope(check_abort=ctx.check_abort, skill="HumanDirect", timeout_s=3,
                                 human_nodes=False):
        with pytest.raises(RuntimeError, match=tr("需要 HITL 通道", "needs a HITL channel")):
            _run_spec(ctx)
    assert time.monotonic() - t0 < 2.5, "it did not wait for anybody"
    assert ctx.calls == ["Prep"], "nothing after the human node ran"
    assert prompts.get_backend().snapshot("all", 10)[1] == [], "and no prompt was shown"


@pytest.mark.parametrize("profile, expect", [("optical", True), ("OPTICAL ", True), ("stm", False), ("", False),
                                              ("nonsense", False)])
def test_only_the_optical_profile_waits_for_human_nodes(monkeypatch, profile, expect):
    from mast.api import direct_exec

    monkeypatch.setenv("MAST_STATION_PROFILE", profile)
    assert direct_exec._waits_for_human_nodes() is expect


def test_an_unreadable_profile_counts_as_stm(monkeypatch):
    from mast import station
    from mast.api import direct_exec

    def boom():
        raise RuntimeError("settings store is gone")

    monkeypatch.setattr(station, "is_optical", boom)
    assert direct_exec._waits_for_human_nodes() is False


def _scope_probe(seen):
    from mast.core.types import SafetyLevel, SkillCategory, SkillMetadata, SkillResult
    from mast.skills.base import BaseSkill

    class ScopeProbe(BaseSkill):
        def metadata(self):
            return SkillMetadata(name="ScopeProbe", version="1.0.0", category=SkillCategory.READ,
                                 safety_level=SafetyLevel.AUTO, description="probe", tags=["read"])

        def execute(self, context, params):
            seen.append(human_node.current_scope())
            decision = interp_mod.human_channel(PAYLOAD)
            return SkillResult(skill_name="ScopeProbe", success=True, data={"decision": decision})

    return ScopeProbe


def test_on_an_stm_station_run_and_record_keeps_the_scope_but_nothing_waits(monkeypatch):
    monkeypatch.setattr(human_node, "DEFAULT_TIMEOUT_S", 3.0)       # a node that wrongly waited fails, it does not hang
    direct_exec, ec, _ = _direct_world(monkeypatch, "stm")
    seen: list = []
    ec._registry.register(_scope_probe(seen))
    t0 = time.monotonic()
    run = direct_exec.run_and_record(ec, "ScopeProbe", {}, agent_id="ext:tester", tool_call_id="j_stm")
    assert time.monotonic() - t0 < 2.5
    (scope_seen,) = seen
    assert scope_seen.human_nodes is False
    assert (scope_seen.agent_id, scope_seen.job_id, scope_seen.skill) == ("ext:tester", "j_stm", "ScopeProbe"), \
        "who the run belongs to is recorded whatever the profile"
    assert run.data["decision"] is None and run.refused_by is None
    assert prompts.get_backend().snapshot("all", 10)[1] == []
    assert human_node.current_scope() is None


def test_on_the_optical_station_run_and_record_sets_the_scope_that_waits(monkeypatch):
    direct_exec, ec, _ = _direct_world(monkeypatch, "optical")
    seen: list = []
    ec._registry.register(_scope_probe(seen))
    with prompts.FakeOperator(lambda p: {"choice": "done"}):
        run = direct_exec.run_and_record(ec, "ScopeProbe", {}, agent_id="ext:tester", tool_call_id="j_opt")
    assert run.success, run.error
    assert seen[0].human_nodes is True and run.data["decision"]["route"] == "done"


def test_a_graph_interrupt_is_still_needs_human_node_on_either_station(monkeypatch):
    from langgraph.errors import GraphInterrupt
    from mast.core.types import SafetyLevel, SkillCategory, SkillMetadata
    from mast.skills.base import BaseSkill

    class Interrupting(BaseSkill):
        def metadata(self):
            return SkillMetadata(name="Interrupting", version="1.0.0", category=SkillCategory.READ,
                                 safety_level=SafetyLevel.AUTO, description="x", tags=["read"])

        def execute(self, context, params):
            raise GraphInterrupt(())

    for profile in ("stm", "optical"):
        direct_exec, ec, _ = _direct_world(monkeypatch, profile)
        ec._registry.register(Interrupting)
        run = direct_exec.run_and_record(ec, "Interrupting", {})
        assert run.refused_by == "needs_human_node" and run.success is False, profile
