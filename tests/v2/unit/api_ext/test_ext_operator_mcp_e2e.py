"""End to end: the MCP server (a real subprocess) -> real HTTP -> the real external surface ->
the operator prompt backend. An agent asks a structured question, keeps working, waits for the
operator, reads the structured answer — and has no way to give it itself.

The harness (a uvicorn-served ext app and an MCP subprocess speaking JSON-RPC) is the one of
``test_ext_mcp_e2e.py``.

Structured prompts are the optical station's: these tests run as one (``MAST_STATION_PROFILE=
optical``; the app is served by this very process, so the setting reaches it). On an STM station
the same tool call is a text request - the last test pins that.
"""
from __future__ import annotations

import re
import threading
import time

import pytest

from test_ext_mcp_e2e import Mcp, mcp, served  # noqa: F401  (fixtures)

from mast.operator_hitl import optical_state, prompts


@pytest.fixture(autouse=True)
def _optical_station(monkeypatch):
    monkeypatch.setenv("MAST_STATION_PROFILE", "optical")


@pytest.fixture(autouse=True)
def _memory_backends():
    old_b = prompts.set_backend(prompts.InMemoryPromptBackend())
    old_s = optical_state.set_store(optical_state.OpticalStateStore())
    yield
    prompts.set_backend(old_b)
    optical_state.set_store(old_s)


_ID = re.compile(r"op-\d{4}-[0-9a-f]{6}")


def test_ask_wait_and_read_a_structured_answer(mcp, world):
    text, err = mcp.call("mast_ask_operator", {
        "message": "Which λ/2 is in the excitation path now?", "kind": "attest",
        "attest_slots": ["excitation_waveplate"], "severity": "safety", "timeout_s": 600,
        "options": [{"id": "hwp_532", "label": "532 nm plate"}, {"id": "hwp_1064", "label": "1064 nm plate"}]})
    assert not err, text
    pid = _ID.search(text).group(0)
    (p,) = prompts.list_open()
    assert p.id == pid and p.kind == "attest" and p.agent_id == "ext:e2e" and p.severity == "safety"
    assert "mast_wait" in text and "operator_reply" in text

    text, err = mcp.call("mast_operator_reply", {"request_id": pid})
    assert not err and "still pending" in text

    # the operator answers while the agent waits
    threading.Timer(0.8, prompts.answer, args=(pid,),
                    kwargs=dict(by="alice", source="launcher", choice="hwp_1064", note="swapped")).start()
    t0 = time.monotonic()
    text, err = mcp.call("mast_wait", {"conditions": [{"type": "operator_reply", "request_id": pid}],
                                       "timeout_s": 30})
    assert not err, text
    assert time.monotonic() - t0 < 20
    first = text.splitlines()[0]
    assert "operator_reply" in first and pid in first and "choice 'hwp_1064'" in first, first

    text, err = mcp.call("mast_operator_reply", {"request_id": pid})
    assert not err
    line = text.splitlines()[0]
    assert "done" in line and "choice 'hwp_1064'" in line and "by alice" in line and "swapped" in line
    # the attestation is the operator's: it is in the registry because the operator answered
    assert optical_state.get_store().is_valid("excitation_waveplate", expect="hwp_1064")


def test_an_invalid_structured_request_comes_back_as_an_error_with_the_reason(mcp, world):
    text, err = mcp.call("mast_ask_operator", {"message": "pick one", "kind": "choice",
                                               "options": [{"id": "only"}]})
    assert err and "at least two options" in text
    assert prompts.list_open() == []
    text, err = mcp.call("mast_ask_operator", {"message": "x", "kind": "question", "unit": "deg"})
    assert err and "structured_fields_need_kind" in text, text


def test_free_text_questions_still_work_through_the_same_tool(mcp, world):
    text, err = mcp.call("mast_ask_operator", {"message": "Where is the Au(111) image?"})
    assert not err and "r-1" in text
    assert prompts.list_open() == []


def test_a_job_that_waits_for_the_operator_is_visible_through_mast_job(mcp, world):
    from mast.skills.builtins.operator_skills import AskOperator

    world.reg.register(AskOperator)
    text, err = mcp.call("mast_run", {"skill": "AskOperator", "wait_s": 0,
                                      "params": {"title": "Is the beam blocked?", "timeout_s": 60}})
    assert not err, text
    jid = re.search(r"j_[0-9a-f]{12}", text).group(0)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        text, err = mcp.call("mast_job", {"job_id": jid})
        if "waiting for the operator" in text:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(text[:600])
    pid = _ID.search(text).group(0)
    assert "only the operator can answer" in text
    prompts.answer(pid, by="alice", source="web", choice="yes")
    text, err = mcp.call("mast_job", {"job_id": jid, "wait_s": 20})
    assert not err and "succeeded" in text, text[:600]


def test_on_an_stm_station_the_same_tool_call_is_a_text_request(mcp, world, monkeypatch):
    monkeypatch.setenv("MAST_STATION_PROFILE", "stm")
    text, err = mcp.call("mast_ask_operator", {
        "message": "Which lambda/2 is in the excitation path now?", "kind": "attest",
        "attest_slots": ["excitation_waveplate"], "severity": "safety", "timeout_s": 600,
        "options": [{"id": "hwp_532", "label": "532 nm plate"}, {"id": "hwp_1064", "label": "1064 nm plate"}]})
    assert not err, text
    assert "r-1" in text and not _ID.search(text), "a wishlist request, not an operator prompt"
    assert prompts.list_open() == [] and prompts.get_backend().snapshot("all", 10)[1] == []
    text, err = mcp.call("mast_operator_reply", {"request_id": "r-1"})
    assert not err and "still pending" in text
    from mast.wishlist import resolve_agent_request

    resolve_agent_request("r-1", "done", note="the 1064 one")
    text, err = mcp.call("mast_operator_reply", {"request_id": "r-1"})
    assert not err and "done" in text.splitlines()[0] and "the 1064 one" in text
    assert not optical_state.get_store().get("excitation_waveplate"), "nothing was attested by a text reply"
