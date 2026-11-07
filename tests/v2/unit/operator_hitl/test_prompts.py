"""The prompt primitive itself: bounds, ask() lifecycle, events, who-may-do-what in the API of the module.

(The scaffold's own contract tests are in ``tests/v2/instruments/test_optical_station_scaffold.py``;
persistence in ``test_persistence.py``; the HTTP side in ``test_operator_api.py``.)
"""
from __future__ import annotations

import threading
import time

import pytest

from mast.operator_hitl import optical_state, prompts


@pytest.fixture(autouse=True)
def _memory(memory_backends):
    yield


def confirm(**kw):
    kw.setdefault("title", "Continue?")
    return prompts.OperatorPrompt(kind="confirm", **kw)


# ── bounds ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kw, why", [
    ({"title": "x" * 301}, "title is longer than 300"),
    ({"message": "x" * 8001}, "message is longer than 8000"),
    ({"context": {"blob": "x" * 21_000}}, "context is larger"),
    ({"attest_slots": [f"s{i}" for i in range(21)]}, "at most 20 slots"),
])
def test_oversized_prompts_are_refused(kw, why):
    with pytest.raises(prompts.PromptError, match=why):
        prompts.post(confirm(**kw))
    assert prompts.get_backend().snapshot("all", 10)[1] == []


def test_options_are_bounded_and_named():
    opts = [prompts.PromptOption(f"o{i}", f"Option {i}") for i in range(25)]
    with pytest.raises(prompts.PromptError, match="at most 24 options"):
        prompts.post(prompts.OperatorPrompt(kind="choice", title="x", options=opts))
    with pytest.raises(prompts.PromptError, match="must not be empty"):
        prompts.post(prompts.OperatorPrompt(kind="choice", title="x", options=[
            prompts.PromptOption("a", "A"), prompts.PromptOption("  ", "blank")]))
    with pytest.raises(prompts.PromptError, match="at most 100 characters"):
        prompts.post(prompts.OperatorPrompt(kind="choice", title="x", options=[
            prompts.PromptOption("a", "A"), prompts.PromptOption("b" * 101, "B")]))
    with pytest.raises(prompts.PromptError, match="at most 100 characters"):
        prompts.post(prompts.OperatorPrompt(kind="choice", title="x", options=[
            prompts.PromptOption("a", "A"), prompts.PromptOption("b", "B" * 301)]))
    ok = prompts.OperatorPrompt(kind="choice", title="x" * 300, message="m" * 8000,
                                options=opts[:24])
    prompts.post(ok)                                   # the limits themselves are fine


def test_a_non_positive_timeout_is_refused():
    for bad in (0, -1, "soon"):
        with pytest.raises(prompts.PromptError, match="timeout_s"):
            prompts.post(confirm(timeout_s=bad))


def test_an_attest_prompt_may_only_name_defined_slots():
    with pytest.raises(prompts.PromptError, match=r"unknown optical-state slot\(s\) \['nope'\]"):
        prompts.post(prompts.OperatorPrompt(kind="attest", title="x", attest_slots=["objective", "nope"]))
    store = optical_state.OpticalStateStore(slots=[optical_state.OpticalSlot("only", "only", kind="flag")])
    old = optical_state.set_store(store)
    try:
        prompts.post(prompts.OperatorPrompt(kind="attest", title="x", attest_slots=["only"]))
        with pytest.raises(prompts.PromptError, match="unknown optical-state slot"):
            prompts.post(prompts.OperatorPrompt(kind="attest", title="x", attest_slots=["objective"]))
    finally:
        optical_state.set_store(old)


def test_attesting_a_yes_no_condition_is_always_shown_as_a_safety_prompt():
    """However an asker words or rates it, "the laser area is safe" is a safety statement: an
    agent must not be able to slip it past the operator as a casual info card."""
    p = prompts.OperatorPrompt(kind="attest", title="Is the wifi fine?", severity="info",
                               attest_slots=["laser_area_safe"])
    prompts.post(p)
    assert p.severity == "safety"
    q = prompts.OperatorPrompt(kind="attest", title="Which objective?", severity="info",
                               attest_slots=["objective"],
                               options=[prompts.PromptOption("10x", "10x"), prompts.PromptOption("50x", "50x")])
    prompts.post(q)
    assert q.severity == "info", "a choice slot is not a safety condition: not escalated"
    r = prompts.OperatorPrompt(kind="confirm", title="Fine?", severity="info")
    prompts.post(r)
    assert r.severity == "info", "only attestations of conditions are escalated"


# ── ask() ───────────────────────────────────────────────────────────────────


def test_ask_writes_its_effective_limit_on_the_prompt_and_reports_the_posting():
    seen = []
    p = confirm(timeout_s=999)
    with prompts.FakeOperator(lambda pr: {"choice": "yes"}):
        prompts.ask(p, timeout_s=7, on_posted=lambda pr: seen.append((pr.id, pr.status, pr.timeout_s)))
    assert p.timeout_s == 7.0, "the explicit limit wins, and the backend expires the prompt at that moment"
    assert seen == [(p.id, "open", 7.0)]
    q = confirm(timeout_s=3)
    with prompts.FakeOperator(lambda pr: {"choice": "yes"}):
        prompts.ask(q)
    assert q.timeout_s == 3, "without an explicit limit the prompt's own applies"


def test_an_on_posted_that_raises_does_not_break_the_ask():
    def bad(p):
        raise RuntimeError("journal full")

    with prompts.FakeOperator(lambda pr: {"choice": "yes"}):
        assert prompts.ask(confirm(), timeout_s=5, on_posted=bad).status == "answered"


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, RuntimeError, MemoryError])
def test_a_prompt_nobody_waits_for_any_more_is_withdrawn(exc, monkeypatch):
    """The asking thread can die for reasons that are not an abort. Its prompt must not stay open
    on the operator's screen asking for an answer nobody will read."""
    backend = prompts.get_backend()

    def die(prompt_id, timeout_s):
        raise exc("the asker is gone")

    monkeypatch.setattr(backend, "wait", die)
    with pytest.raises(exc):
        prompts.ask(confirm(title="Orphan"), timeout_s=30, poll_s=0.01)
    (entry,) = backend.snapshot("all", 10)[1]
    assert entry["status"] == "cancelled" and entry["answer"]["note"] == "the asker stopped waiting"
    assert prompts.list_open() == []


def test_a_check_abort_that_raises_withdraws_the_prompt_too():
    class Stop(Exception):
        pass

    def check():
        raise Stop()

    with pytest.raises(Stop):
        prompts.ask(confirm(), check_abort=check, timeout_s=5)
    assert prompts.list_open() == []


def test_ask_returns_whatever_ends_the_prompt():
    box = {}
    t = threading.Thread(target=lambda: box.update(a=prompts.ask(confirm(), timeout_s=10, poll_s=0.01)))
    t.start()
    deadline = time.monotonic() + 5
    while not prompts.list_open() and time.monotonic() < deadline:
        time.sleep(0.01)
    prompts.dismiss(prompts.list_open()[0].id, by="alice", source="web", reason="stale")
    t.join(5)
    assert box["a"].status == "dismissed" and box["a"].answered_by == "alice" and box["a"].note == "stale"


# ── answer / dismiss ────────────────────────────────────────────────────────


def test_answering_and_dismissing_need_a_name_and_an_open_prompt():
    pid = prompts.post(confirm())
    with pytest.raises(prompts.PromptError, match="operator's name"):
        prompts.dismiss(pid, by="  ", source="web")
    with pytest.raises(prompts.PromptNotFound):
        prompts.dismiss("op-0-nope", by="alice", source="web")
    prompts.dismiss(pid, by="alice", source="launcher", reason="not needed")
    with pytest.raises(prompts.PromptClosed, match="already dismissed"):
        prompts.dismiss(pid, by="alice", source="web")
    with pytest.raises(prompts.PromptClosed, match="already dismissed"):
        prompts.answer(pid, by="alice", source="web", choice="yes")
    ans = prompts.get_backend().get_answer(pid)
    assert (ans.status, ans.answered_by, ans.source, ans.note) == ("dismissed", "alice", "launcher", "not needed")


def test_the_not_found_and_closed_errors_are_still_prompt_errors_and_value_errors():
    """Callers of the first scaffold catch ``PromptError`` (a ``ValueError``)."""
    assert issubclass(prompts.PromptNotFound, prompts.PromptError)
    assert issubclass(prompts.PromptClosed, prompts.PromptError)
    assert issubclass(prompts.PromptError, ValueError)
    assert issubclass(prompts.PromptAborted, RuntimeError)


def test_an_attest_answer_is_validated_for_every_slot_before_any_is_written():
    """The slots are checked again when the answer comes in - their definitions can change
    between the post and the click - and none is written unless every one accepts the value."""
    store = optical_state.get_store()
    angle_a = optical_state.OpticalSlot("angle_a", "A", kind="number", unit="deg", min=0.0, max=360.0)
    store.set_slots([angle_a, optical_state.OpticalSlot("angle_b", "B", kind="number", unit="deg",
                                                        min=0.0, max=90.0)])
    pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Both", attest_slots=["angle_a", "angle_b"]))
    store.set_slots([angle_a, optical_state.OpticalSlot("angle_b", "B", kind="number", unit="deg",
                                                        min=0.0, max=30.0)])          # tightened meanwhile
    with pytest.raises(prompts.PromptError, match="attestation refused"):
        prompts.answer(pid, by="alice", source="web", value=45)         # fits angle_a, not angle_b any more
    assert store.get("angle_a") is None and store.get("angle_b") is None
    assert prompts.get(pid).status == "open"


def test_an_attest_answer_sets_several_slots_with_the_same_value():
    pid = prompts.post(prompts.OperatorPrompt(
        kind="attest", title="Which plate (both places)?", attest_slots=["excitation_waveplate", "analyzer_optic"],
        options=[prompts.PromptOption("hwp_532", "532"), prompts.PromptOption("hwp_1064", "1064")]))
    prompts.answer(pid, by="alice", source="web", choice="hwp_532")
    store = optical_state.get_store()
    assert store.get("excitation_waveplate").value == "hwp_532" == store.get("analyzer_optic").value


def test_an_attest_after_the_timeout_attests_nothing():
    pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Area safe?", attest_slots=["laser_area_safe"],
                                              timeout_s=0.05))
    time.sleep(0.12)
    with pytest.raises(prompts.PromptClosed, match="timeout"):
        prompts.answer(pid, by="alice", source="web", value=True)
    assert optical_state.get_store().get("laser_area_safe") is None


# ── events ──────────────────────────────────────────────────────────────────


def test_an_expiry_found_while_refusing_an_answer_is_announced_at_once():
    seen = []
    prompts.get_backend().listeners.append(lambda ev, p: seen.append((ev, p.id, p.status)))
    pid = prompts.post(confirm(timeout_s=0.05))
    time.sleep(0.12)
    with pytest.raises(prompts.PromptClosed):
        prompts.answer(pid, by="alice", source="web", choice="yes")
    assert seen == [("posted", pid, "open"), ("closed", pid, "timeout")], \
        "the refusal must not leave the expiry unannounced until some later read"


def test_a_failing_listener_never_breaks_the_flow():
    backend = prompts.get_backend()
    backend.listeners.append(lambda ev, p: 1 / 0)
    pid = prompts.post(confirm())
    assert prompts.answer(pid, by="alice", source="web", choice="yes").status == "answered"


def test_seq_counts_every_change_once():
    backend = prompts.get_backend()
    assert backend.seq == 0
    a, b = prompts.post(confirm()), prompts.post(confirm(title="b"))
    assert backend.seq == 2
    prompts.answer(a, by="x", source="web", choice="yes")
    prompts.dismiss(b, by="x", source="web")
    assert backend.seq == 4
    backend.snapshot("all", 10), backend.list_open(), backend.get(a)         # reads change nothing
    assert backend.seq == 4
    with pytest.raises(prompts.PromptClosed):
        prompts.answer(a, by="x", source="web", choice="yes")
    assert backend.seq == 4, "a refused answer is not a change"


def test_a_fake_operator_that_declines_to_answer_leaves_the_prompt_open():
    with prompts.FakeOperator(lambda p: None) as op:
        pid = prompts.post(confirm())
        time.sleep(0.1)
    assert op.seen and prompts.get(pid).status == "open"
