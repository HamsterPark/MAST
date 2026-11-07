"""A text over the limit is refused, never cut.

A text slot (the edge filter, a sample note) is what the next person reads in a spectrum's
sidecar; an operator who typed 600 characters and was told "saved" would leave a shortened
sentence behind under their name. The web form already stops at 500 characters; the server used
to cut quietly what got past it (a script, an attest answer). Now it answers 422 and writes
nothing — and an attest prompt stays open, so the operator can fix the text and answer again.

One constant per limit (``optical_state.MAX_SLOT_TEXT_CHARS``, ``MAX_SLOT_NOTE_CHARS``,
``prompts.MAX_TEXT_ANSWER_CHARS``): the tests move the constant and watch the checks follow.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.routes import operator as operator_routes
from mast.operator_hitl import optical_state, prompts

JSON = {"Content-Type": "application/json"}
LIMIT = optical_state.MAX_SLOT_TEXT_CHARS


@pytest.fixture
def web(memory_backends):
    app = FastAPI()
    app.include_router(operator_routes.router, prefix="/api")
    return TestClient(app, base_url="http://127.0.0.1:7862")


def put(web, text, slot="edge_filter"):
    return web.put(f"/api/operator/optical-state/{slot}", json={"by": "alice", "value": text}, headers=JSON)


def attest_prompt(slot="edge_filter"):
    return prompts.post(prompts.OperatorPrompt(kind="attest", title="Which edge filter?", attest_slots=[slot]))


def answer(web, pid, text):
    return web.post(f"/api/operator/prompts/{pid}/answer", json={"by": "alice", "value": text}, headers=JSON)


def test_the_documented_limits():
    assert optical_state.MAX_SLOT_TEXT_CHARS == 500
    assert optical_state.MAX_SLOT_NOTE_CHARS == 500
    assert prompts.MAX_TEXT_ANSWER_CHARS == 4000


# ── PUT on a text slot ────────────────────────────────────────────────────────


def test_a_text_slot_takes_exactly_the_limit_and_refuses_one_more(web):
    exact = "f" * LIMIT
    ok = put(web, exact)
    assert ok.status_code == 200, ok.text
    assert ok.json()["value"] == exact, "nothing was cut"
    store = optical_state.get_store()
    assert store.get("edge_filter").value == exact and len(store.history) == 1

    too_long = put(web, "g" * (LIMIT + 1))
    assert too_long.status_code == 422, too_long.text
    detail = too_long.json()["detail"]
    assert str(LIMIT + 1) in detail and str(LIMIT) in detail and "edge_filter" in detail
    assert store.get("edge_filter").value == exact, "the refused text replaced nothing"
    assert len(store.history) == 1 and store.seq == 1, "and left no trace"


def test_the_limit_counts_characters_not_bytes(web):
    cjk = "滤" * LIMIT                                   # 500 characters, 1500 UTF-8 bytes
    assert len(cjk.encode("utf-8")) > LIMIT
    r = put(web, cjk)
    assert r.status_code == 200 and r.json()["value"] == cjk
    assert put(web, cjk + "光").status_code == 422


def test_the_slot_note_limit_is_the_constant_too(web):
    ok = web.put("/api/operator/optical-state/edge_filter",
                 json={"by": "alice", "value": "LP532", "note": "n" * optical_state.MAX_SLOT_NOTE_CHARS},
                 headers=JSON)
    assert ok.status_code == 200 and len(ok.json()["note"]) == optical_state.MAX_SLOT_NOTE_CHARS
    over = web.put("/api/operator/optical-state/edge_filter",
                   json={"by": "alice", "value": "LP532", "note": "n" * (optical_state.MAX_SLOT_NOTE_CHARS + 1)},
                   headers=JSON)
    assert over.status_code == 422


# ── attest answers on a text slot ──────────────────────────────────────────────


def test_an_attest_answer_over_the_limit_is_refused_and_the_prompt_stays_open(web):
    pid = attest_prompt()
    r = answer(web, pid, "h" * (LIMIT + 1))
    assert r.status_code == 422, r.text
    assert "attestation refused" in r.json()["detail"] and str(LIMIT) in r.json()["detail"]
    store = optical_state.get_store()
    assert prompts.get(pid).status == "open", "the operator can fix the text and answer again"
    assert prompts.get_backend().get_answer(pid) is None
    assert store.get("edge_filter") is None and store.history == [] and store.seq == 0

    exact = "i" * LIMIT
    ok = answer(web, pid, exact)
    assert ok.status_code == 200, ok.text
    assert prompts.get(pid).status == "answered"
    assert ok.json()["value"] == exact and store.get("edge_filter").value == exact, "nothing was cut"


def test_the_attest_check_and_the_put_check_are_one_check(web):
    """The same text is accepted or refused the same way by both doors."""
    for text in ("a" * LIMIT, "a" * (LIMIT + 1)):
        via_put = put(web, text).status_code
        via_attest = answer(web, attest_prompt(), text).status_code
        assert via_put == via_attest, (len(text), via_put, via_attest)


def test_moving_the_constant_moves_every_check(web, monkeypatch):
    """No second copy of the number hides in a route or a schema."""
    monkeypatch.setattr(optical_state, "MAX_SLOT_TEXT_CHARS", 12)
    assert put(web, "x" * 12).status_code == 200
    assert put(web, "x" * 13).status_code == 422
    pid = attest_prompt()
    assert answer(web, pid, "y" * 13).status_code == 422 and prompts.get(pid).status == "open"
    assert answer(web, pid, "y" * 12).status_code == 200


def test_a_multi_slot_attest_over_the_limit_writes_none_of_its_slots(web):
    store = optical_state.get_store()
    store.set_slots([optical_state.OpticalSlot("filter_a", "A", kind="text"),
                     optical_state.OpticalSlot("filter_b", "B", kind="text")])
    pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Both?", attest_slots=["filter_a", "filter_b"]))
    r = answer(web, pid, "z" * (LIMIT + 1))
    assert r.status_code == 422
    assert store.get("filter_a") is None and store.get("filter_b") is None
    assert prompts.get(pid).status == "open" and store.history == []
    assert answer(web, pid, "LP532").status_code == 200
    assert store.get("filter_a").value == store.get("filter_b").value == "LP532"


# ── text prompts ───────────────────────────────────────────────────────────────


def test_a_text_prompt_answer_over_its_limit_is_refused_and_the_prompt_stays_open(web):
    pid = prompts.post(prompts.OperatorPrompt(kind="text", title="Describe the sample"))
    limit = prompts.MAX_TEXT_ANSWER_CHARS
    r = answer(web, pid, "j" * (limit + 1))
    assert r.status_code == 422 and str(limit) in r.json()["detail"]
    assert prompts.get(pid).status == "open" and prompts.get_backend().get_answer(pid) is None
    ok = answer(web, pid, "j" * limit)
    assert ok.status_code == 200 and ok.json()["value"] == "j" * limit, "nothing was cut"


def test_the_direct_python_api_refuses_too(memory_backends):
    """Not only the HTTP layer: ``prompts.answer`` and the store themselves."""
    pid = prompts.post(prompts.OperatorPrompt(kind="text", title="x"))
    with pytest.raises(prompts.PromptError, match="at most 4000"):
        prompts.answer(pid, by="alice", source="web", value="k" * 4001)
    with pytest.raises(optical_state.OpticalStateError, match="at most 500"):
        optical_state.get_store().set_by_operator("edge_filter", "k" * 501, by="alice", source="web")
    with pytest.raises(optical_state.OpticalStateError):
        optical_state.get_store().validate_value("edge_filter", "k" * 501)
