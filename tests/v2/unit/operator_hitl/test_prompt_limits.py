"""How many prompts the operator can be asked at once, and for how long.

The ext route limits one agent to 25 open prompts - by the name the agent gives itself, so an
agent that changes its name every time is not limited at all (a flood of prompts from rotating names gets through).
The backend therefore also bounds the open prompts of all callers together (100), and the share
that external agents (``agent_id`` ``ext:...``) may hold (75), so that a flood from outside cannot
crowd out MAST's own prompts - a laser attestation a protocol needs. A prompt posted without
``timeout_s`` is no longer open for ever: it gets a lifetime (24 hours) and shows it.
"""
from __future__ import annotations

import threading
import time

import pytest

from mast.operator_hitl import prompts
from mast.operator_hitl.prompts import InMemoryPromptBackend, TooManyOpenPrompts


@pytest.fixture(autouse=True)
def _memory(memory_backends):
    yield


def confirm(agent_id=None, **kw):
    kw.setdefault("title", "Continue?")
    return prompts.OperatorPrompt(kind="confirm", agent_id=agent_id, **kw)


def small(**kw) -> InMemoryPromptBackend:
    backend = InMemoryPromptBackend(**kw)
    prompts.set_backend(backend)
    return backend


def test_the_documented_numbers():
    assert prompts.MAX_OPEN_PROMPTS == 100
    assert prompts.MAX_OPEN_EXTERNAL_PROMPTS == 75
    assert prompts.DEFAULT_PROMPT_LIFETIME_S == 24 * 3600


# ── capacity ──────────────────────────────────────────────────────────────────────────────


def test_the_next_prompt_beyond_the_total_is_refused_and_leaves_no_trace():
    backend = small(max_open=3, max_open_external=2)
    for i in range(3):
        prompts.post(confirm(title=f"q{i}"))
    seq = backend.seq
    refused = confirm(title="one too many")
    with pytest.raises(TooManyOpenPrompts) as exc:
        prompts.post(refused)
    assert (exc.value.scope, exc.value.open, exc.value.limit) == ("total", 3, 3)
    assert isinstance(exc.value, prompts.PromptError), "callers that handle PromptError keep working"
    assert refused.id == "" and backend.seq == seq and len(prompts.list_open()) == 3


def test_external_agents_share_a_smaller_limit_whatever_names_they_use():
    small(max_open=5, max_open_external=2)
    prompts.post(confirm("ext:a", title="1"))
    prompts.post(confirm("ext:b", title="2"))                    # a different name each time
    with pytest.raises(TooManyOpenPrompts) as exc:
        prompts.post(confirm("ext:c", title="3"))
    assert (exc.value.scope, exc.value.open, exc.value.limit) == ("external", 2, 2)


def test_mast_own_prompts_still_fit_when_external_agents_have_used_their_share():
    small(max_open=4, max_open_external=2)
    prompts.post(confirm("ext:a", title="1"))
    prompts.post(confirm("ext:b", title="2"))
    prompts.post(confirm(None, title="a protocol's laser attestation"))
    prompts.post(confirm("mast:internal", title="another of MAST's own"))
    with pytest.raises(TooManyOpenPrompts) as exc:
        prompts.post(confirm(None, title="and now the whole list is full"))
    assert exc.value.scope == "total"


def test_with_the_real_numbers_a_rotating_name_gets_75_and_the_rest_is_kept_for_mast():
    for i in range(prompts.MAX_OPEN_EXTERNAL_PROMPTS):
        prompts.post(confirm(f"ext:rotating-{i}", title=f"q{i}"))        # 75 names, one prompt each
    with pytest.raises(TooManyOpenPrompts, match="external agents already have 75"):
        prompts.post(confirm("ext:rotating-76", title="q76"))
    for i in range(prompts.MAX_OPEN_PROMPTS - prompts.MAX_OPEN_EXTERNAL_PROMPTS):
        prompts.post(confirm(None, title=f"own {i}"))
    with pytest.raises(TooManyOpenPrompts, match="already has 100 unanswered"):
        prompts.post(confirm(None, title="own 25"))
    assert len(prompts.list_open()) == 100


def test_answering_or_closing_a_prompt_frees_a_place():
    small(max_open=2, max_open_external=2)
    a = prompts.post(confirm("ext:a", title="1"))
    b = prompts.post(confirm("ext:b", title="2"))
    with pytest.raises(TooManyOpenPrompts):
        prompts.post(confirm("ext:c", title="3"))
    prompts.answer(a, by="alice", source="web", choice="yes")
    c = prompts.post(confirm("ext:c", title="3"))
    prompts.cancel(b, "no longer needed")
    prompts.dismiss(c, by="alice", source="web")
    prompts.post(confirm("ext:d", title="4"))
    prompts.post(confirm("ext:e", title="5"))


def test_an_expired_prompt_frees_its_place_without_anybody_reading_the_list():
    small(max_open=1, max_open_external=1)
    prompts.post(confirm("ext:a", title="1", timeout_s=0.05))
    with pytest.raises(TooManyOpenPrompts):
        prompts.post(confirm("ext:b", title="2"))
    time.sleep(0.12)
    prompts.post(confirm("ext:b", title="2"))                    # the sweep inside post() found a place


def test_concurrent_posts_cannot_slip_past_the_limit_together():
    small(max_open=10, max_open_external=10)
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(24)

    def post_one(i):
        barrier.wait()
        try:
            prompts.post(confirm(f"ext:t{i}", title=f"q{i}"))
            result = "ok"
        except TooManyOpenPrompts:
            result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=post_one, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert sorted(outcomes) == ["ok"] * 10 + ["refused"] * 14
    assert len(prompts.list_open()) == 10


def test_the_persistent_backend_has_the_same_limits_and_keeps_its_file_whole(op_root):
    from mast.operator_hitl.store import PROMPTS_FILE, PersistentPromptBackend, operator_dir

    backend = PersistentPromptBackend(op_root, max_open=2, max_open_external=1)
    prompts.set_backend(backend)
    prompts.post(confirm("ext:a", title="1"))
    with pytest.raises(TooManyOpenPrompts):
        prompts.post(confirm("ext:b", title="2"))
    prompts.post(confirm(None, title="3"))
    with pytest.raises(TooManyOpenPrompts):
        prompts.post(confirm(None, title="4"))
    reloaded = PersistentPromptBackend(op_root)
    assert len(reloaded.snapshot("all", 10)[1]) == 2, (operator_dir(op_root) / PROMPTS_FILE).read_text()


# ── lifetime ──────────────────────────────────────────────────────────────────────────────


def test_a_prompt_without_a_timeout_gets_the_default_lifetime_and_shows_it():
    p = confirm(title="no timeout given")
    assert p.timeout_s is None
    prompts.post(p)
    assert p.timeout_s == prompts.DEFAULT_PROMPT_LIFETIME_S == 86400.0
    snap = prompts.get_backend().snapshot("open", 10)[1]
    assert snap[0]["timeout_s"] == 86400.0, "what the operator's list and an agent's view show"


def test_an_explicit_timeout_is_kept_as_it_is():
    p = confirm(title="x", timeout_s=600)
    prompts.post(p)
    assert p.timeout_s == 600
    q = confirm(title="y", timeout_s=7 * 24 * 3600)
    prompts.post(q)
    assert q.timeout_s == 7 * 24 * 3600, "a caller that asks for longer gets it (the ext schema allows 7 days)"


def test_a_prompt_nobody_answers_closes_itself_when_its_lifetime_is_over():
    backend = small(default_lifetime_s=0.05)
    pid = prompts.post(confirm(title="forgotten"))
    assert prompts.get(pid).status == "open"
    time.sleep(0.15)
    assert prompts.get(pid).status == "timeout"
    ans = backend.get_answer(pid)
    assert ans.status == "timeout" and ans.source == "system" and not ans.ok
    assert prompts.list_open() == []


def test_a_wait_without_a_limit_ends_at_the_prompts_lifetime():
    small(default_lifetime_s=0.2)
    result: list[prompts.OperatorAnswer] = []
    t = threading.Thread(target=lambda: result.append(prompts.ask(confirm(title="wait for ever?"), poll_s=0.02)),
                         daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(10)
    assert not t.is_alive(), "ask() with no limit used to wait until somebody answered"
    assert result[0].status == "timeout" and time.monotonic() - t0 < 5


def test_the_old_behaviour_is_still_there_when_asked_for():
    small(default_lifetime_s=None)
    p = confirm(title="never expires")
    prompts.post(p)
    assert p.timeout_s is None and prompts.get_backend()._deadlines == {}


def test_the_lifetime_survives_in_the_persisted_prompt(op_root):
    import json

    from mast.operator_hitl.store import PROMPTS_FILE, PersistentPromptBackend, operator_dir

    prompts.set_backend(PersistentPromptBackend(op_root))
    prompts.post(confirm(title="saved with its lifetime"))
    saved = json.loads((operator_dir(op_root) / PROMPTS_FILE).read_text(encoding="utf-8"))
    assert saved["prompts"][0]["prompt"]["timeout_s"] == 86400.0
