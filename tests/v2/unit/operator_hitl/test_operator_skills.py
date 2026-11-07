"""AskOperator (CONFIRM, blocks for the operator) and ReadOpticalState (AUTO, read-only)."""
from __future__ import annotations

import threading
import time

import pytest

from mast.core.instrument_lock import needs_token
from mast.core.sample_gate import requires_sample
from mast.core.types import SafetyLevel, SkillCategory
from mast.i18n import tr
from mast.operator_hitl import human_node, optical_state, prompts
from mast.skills.builtins.operator_skills import AskOperator, ReadOpticalState


@pytest.fixture(autouse=True)
def _memory(memory_backends):
    yield


class Ctx:
    def __init__(self):
        self.stop = threading.Event()

    def check_abort(self):
        return self.stop.is_set()


def ask(params, ctx=None):
    return AskOperator().execute(ctx if ctx is not None else Ctx(), params)


def wait_for_open(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = prompts.list_open()
        if found:
            return found[0]
        time.sleep(0.01)
    raise AssertionError("no prompt was posted")


# ── metadata ────────────────────────────────────────────────────────────────


def test_the_contract_names_and_levels():
    ask_meta, read_meta = AskOperator().metadata(), ReadOpticalState().metadata()
    assert (ask_meta.name, ask_meta.safety_level) == ("AskOperator", SafetyLevel.CONFIRM)
    assert (read_meta.name, read_meta.safety_level) == ("ReadOpticalState", SafetyLevel.AUTO)
    assert ask_meta.category is read_meta.category is SkillCategory.READ


def test_neither_holds_the_instrument_token_or_needs_a_sample():
    """AskOperator may wait for hours: holding the instrument meanwhile would lock out everything."""
    for skill in (AskOperator(), ReadOpticalState()):
        meta = skill.metadata()
        assert not needs_token(meta, meta.name)
        assert not requires_sample(meta, meta.name)


def test_both_are_discovered_by_the_registry():
    from mast.core.registry import SkillRegistry

    reg = SkillRegistry()
    reg.discover("mast.skills.builtins")
    assert reg.has("AskOperator") and reg.has("ReadOpticalState")


def test_parameters_are_validated_before_anything_is_asked():
    s = AskOperator()
    assert s.validate_params({"title": "ok?"}) == []
    assert s.validate_params({}) and "title" in s.validate_params({})[0]
    assert s.validate_params({"title": "x", "kind": "sign"}), "kind is one of the five"
    assert s.validate_params({"title": "x", "severity": "loud"})
    assert s.validate_params({"title": "x", "timeout_s": 4 * 3600 + 1}), "at most 4 h"
    assert s.validate_params({"title": "x", "timeout_s": 0.5}), "at least 1 s"
    assert s.validate_params({"title": "x", "timeout_s": 4 * 3600}) == []
    assert s.validate_params({"title": "x", "sneaky": 1}) and "Unknown parameter" in s.validate_params(
        {"title": "x", "sneaky": 1})[0]


def test_descriptions_say_what_it_does_and_every_parameter_is_described():
    for skill in (AskOperator(), ReadOpticalState()):
        meta = skill.metadata()
        assert len(meta.description) > 80, meta.name
        for p in meta.parameters:
            assert p.description, (meta.name, p.name)
    assert tr("阻塞", "block") in AskOperator().metadata().description


# ── AskOperator ─────────────────────────────────────────────────────────────


def test_confirm_yes():
    with prompts.FakeOperator(lambda p: {"choice": "yes", "note": "go"}) as op:
        res = ask({"title": "Is the beam blocked?", "message": "Block the 532 line.", "kind": "confirm"})
    assert res.success, res.error
    d = res.data
    assert d["status"] == "answered" and d["ok"] is True and d["choice"] == "yes" and d["note"] == "go"
    assert d["answered_by"] == "test-operator" and d["prompt_id"] == op.seen[0].id
    assert "test-operator" in res.summary


def test_a_no_is_an_answer_not_a_failure():
    """A composite must be able to branch on it; a failed step would abort the composite instead."""
    with prompts.FakeOperator(lambda p: {"choice": "no"}):
        res = ask({"title": "Ready?"})
    assert res.success is True and res.data["ok"] is False and res.data["choice"] == "no"


@pytest.mark.parametrize("options", [
    '[{"id": "a", "label": "Alpha"}, {"id": "b", "label": "Beta"}]',
    "a=Alpha, b=Beta",
    '["a", "b"]',
    "a,b",
])
def test_choice_options_in_every_accepted_spelling(options):
    with prompts.FakeOperator(lambda p: {"choice": "b"}) as op:
        res = ask({"title": "Which?", "kind": "choice", "options": options})
    assert res.success and res.data["choice"] == "b" and res.data["value"] == "b"
    assert [o.id for o in op.seen[0].options] == ["a", "b"]
    if "Alpha" in options:
        assert [o.label for o in op.seen[0].options] == ["Alpha", "Beta"]


def test_a_number_within_bounds():
    with prompts.FakeOperator(lambda p: {"value": 45}) as op:
        res = ask({"title": "Analyzer angle?", "kind": "number", "number_min": 0, "number_max": 360,
                   "unit": "deg"})
    assert res.success and res.data["value"] == 45.0
    p = op.seen[0]
    assert (p.number_min, p.number_max, p.unit) == (0, 360, "deg")


def test_text():
    with prompts.FakeOperator(lambda p: {"value": "OD2 filter in"}):
        res = ask({"title": "Which filter?", "kind": "text"})
    assert res.success and res.data["value"] == "OD2 filter in"


def test_attest_sets_the_slots_it_names():
    with prompts.FakeOperator(lambda p: {"choice": "hwp_1064"}):
        res = ask({"title": "Which λ/2?", "kind": "attest", "attest_slots": "excitation_waveplate",
                   "options": "hwp_532=532 nm, hwp_1064=1064 nm"})
    assert res.success and res.data["choice"] == "hwp_1064"
    store = optical_state.get_store()
    assert store.is_valid("excitation_waveplate", expect="hwp_1064")
    assert store.get("excitation_waveplate").source == f"prompt:{res.data['prompt_id']}"


def test_attest_with_a_json_list_of_slots_and_a_plain_confirmation():
    with prompts.FakeOperator(lambda p: {"value": True}):
        res = ask({"title": "Laser area safe?", "kind": "attest", "severity": "safety",
                   "attest_slots": '["laser_area_safe"]'})
    assert res.success
    assert optical_state.get_store().is_valid("laser_area_safe", expect=True)


def test_an_attest_naming_an_undefined_slot_is_refused_up_front():
    res = ask({"title": "x", "kind": "attest", "attest_slots": "no_such_slot"})
    assert not res.success and "unknown optical-state slot" in res.error
    assert prompts.get_backend().snapshot("all", 10)[1] == [], "nothing was shown to the operator"


@pytest.mark.parametrize("params, why", [
    ({"title": "x", "kind": "choice", "options": "only_one"}, "at least two options"),
    ({"title": "x", "kind": "choice", "options": "[not json"}, "not valid JSON"),
    ({"title": "x", "kind": "attest"}, "must name the optical-state slots"),
    ({"title": "x", "kind": "number", "number_min": 5, "number_max": 1}, "number_min"),
    ({"title": "  "}, "title"),
])
def test_an_invalid_prompt_fails_before_the_operator_sees_anything(params, why):
    res = ask(params)
    assert not res.success and why in res.error and res.error.startswith("invalid prompt")
    assert prompts.get_backend().snapshot("all", 10)[1] == []


def test_no_answer_in_time_fails_with_the_reason_and_closes_the_prompt():
    t0 = time.monotonic()
    res = ask({"title": "Anyone there?", "timeout_s": 1.0})
    assert time.monotonic() - t0 < 4
    assert res.success is False and res.data["prompt_id"] in res.error
    assert tr("没有答复", "did not answer") in res.error
    assert res.data["status"] == "timeout" and prompts.get(res.data["prompt_id"]).status == "timeout"
    assert prompts.list_open() == []


def test_a_dismissed_prompt_fails_and_says_so():
    ctx = Ctx()
    box = {}
    t = threading.Thread(target=lambda: box.update(res=ask({"title": "Anyone?"}, ctx)), daemon=True)
    t.start()
    p = wait_for_open()
    prompts.dismiss(p.id, by="alice", source="web", reason="not needed any more")
    t.join(5)
    res = box["res"]
    assert not res.success and res.data["status"] == "dismissed"
    assert "not needed any more" in res.error and res.data["note"] == "not needed any more"


def test_a_stop_withdraws_the_prompt_and_ends_the_wait_at_once():
    ctx = Ctx()
    box = {}
    t = threading.Thread(target=lambda: box.update(res=ask({"title": "Wait for me", "timeout_s": 600}, ctx)),
                         daemon=True)
    t.start()
    p = wait_for_open()
    t0 = time.monotonic()
    ctx.stop.set()
    t.join(5)
    assert time.monotonic() - t0 < 3
    res = box["res"]
    assert not res.success and res.data == {"prompt_id": p.id, "status": "cancelled"}
    assert prompts.get(p.id).status == "cancelled"


def test_an_already_stopped_run_does_not_start_the_ask():
    ctx = Ctx()
    ctx.stop.set()
    res = ask({"title": "x"}, ctx)
    assert not res.success and "not started" in res.error
    assert prompts.get_backend().snapshot("all", 10)[1] == []


def test_the_default_wait_is_half_an_hour_and_the_longest_is_four(monkeypatch):
    meta = AskOperator().metadata()
    (timeout,) = [p for p in meta.parameters if p.name == "timeout_s"]
    assert timeout.default == 1800 and timeout.max_value == 14400
    seen = {}
    real = human_node.ask_blocking

    def spy(prompt, *, check_abort, timeout_s):
        seen["t"] = timeout_s
        prompt.timeout_s = timeout_s
        return prompts.OperatorAnswer(prompt_id="x", status="answered", choice="yes",
                                      answered_by="a", source="test")

    monkeypatch.setattr(human_node, "ask_blocking", spy)
    ask({"title": "x"})
    assert seen["t"] == 1800
    ask({"title": "x", "timeout_s": 10 ** 7})
    assert seen["t"] == 14400, "even if validation were skipped, the wait stays bounded"
    monkeypatch.setattr(human_node, "ask_blocking", real)


def test_inside_a_job_the_prompt_names_the_job_and_the_job_hears_it_wait():
    events = []
    with human_node.wait_sink(events.append):
        with human_node.direct_scope(check_abort=None, skill="SHGExcitationScan", agent_id="ext:tester",
                                     job_id="j_00000000beef"):
            with prompts.FakeOperator(lambda p: {"choice": "yes"}) as op:
                res = ask({"title": "Swap done?"})
    assert res.success
    p = op.seen[0]
    assert (p.job_id, p.skill, p.agent_id) == ("j_00000000beef", "SHGExcitationScan", "ext:tester")
    assert [bool(e) for e in events] == [True, False] and events[0]["prompt_id"] == p.id


# ── ReadOpticalState ────────────────────────────────────────────────────────


def test_read_all_slots_and_the_unset_list():
    res = ReadOpticalState().execute(None, {})
    assert res.success
    d = res.data
    assert {s["id"] for s in d["slots"]} == {s.id for s in optical_state.DEFAULT_SLOTS}
    assert d["valid"] == [] and set(d["unset"]) == set(d["values"]) and d["expired"] == []
    assert "0/" in res.summary


def test_read_reports_valid_and_expired_values():
    store = optical_state.get_store()
    store.set_by_operator("objective", "50x", by="alice", source="web")
    store.set_by_operator("laser_area_safe", True, by="alice", source="web")
    store._values["laser_area_safe"].valid_until = "2000-01-01T00:00:00+00:00"
    d = ReadOpticalState().execute(None, {}).data
    assert d["values"]["objective"]["value"] == "50x" and d["values"]["objective"]["valid"] is True
    assert d["values"]["objective"]["set_by"] == "alice"
    assert d["valid"] == ["objective"] and d["expired"] == ["laser_area_safe"]
    assert "objective" not in d["unset"] and "edge_filter" in d["unset"]


def test_read_one_slot_and_an_unknown_one():
    optical_state.get_store().set_by_operator("incident_polarizer_deg", 30, by="a", source="web")
    res = ReadOpticalState().execute(None, {"slot": "incident_polarizer_deg"})
    assert res.success and list(res.data["values"]) == ["incident_polarizer_deg"]
    assert res.data["values"]["incident_polarizer_deg"]["value"] == 30.0
    bad = ReadOpticalState().execute(None, {"slot": "nope"})
    assert not bad.success and "no optical-state slot 'nope'" in bad.error and "objective" in bad.error


def test_reading_changes_nothing():
    store = optical_state.get_store()
    ReadOpticalState().execute(None, {})
    assert store.history == [] and store.seq == 0
