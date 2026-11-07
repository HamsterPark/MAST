"""An attestation answered False is an answer, not an approval.

``OperatorAnswer.ok`` used to be True for every answered attest, so an agent that read ``ok``
after "is the laser area safe?" saw True when the operator had recorded NOT safe. Now:

* ``ok`` is False for an attest recorded as False (and for a confirm answered no, a cancel, and
  everything that ended without an answer);
* the answer's ``value`` is what the slots hold (``"false"``, ``0``, ``"no"`` are all False),
  so what is read back is what was signed;
* ``AskOperator`` returns ``data.attested`` - ``{slot: recorded value}`` - and says in its
  summary that the operator recorded False and that this is not a sign-off.
"""
from __future__ import annotations

import threading

import pytest

from mast.i18n import tr
from mast.operator_hitl import optical_state, prompts
from mast.skills.builtins.operator_skills import AskOperator


@pytest.fixture(autouse=True)
def _memory(memory_backends):
    yield


class Ctx:
    def __init__(self):
        self.stop = threading.Event()

    def check_abort(self):
        return self.stop.is_set()


def ask(params):
    return AskOperator().execute(Ctx(), params)


NOT_AN_APPROVAL = tr(" —— 操作员记成了 False（没有确认 / 不安全）：这不是批准，不要当成已签署。",
                     " - the operator recorded False (NOT confirmed / not safe): this is not an approval, "
                     "do not treat it as signed off.")


# ── ok ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status, choice, value, ok", [
    ("answered", "yes", None, True),
    ("answered", "no", None, False),
    ("answered", "cancel", None, False),
    ("answered", None, True, True),                  # an attestation recorded True
    ("answered", None, False, False),               # ... recorded False: an answer, not an approval
    ("answered", "hwp_532", "hwp_532", True),
    ("answered", None, 0.0, True),                  # a number that happens to be 0 is not False
    ("answered", None, 0, True),
    ("answered", None, "False", True),              # text typed by a person is text
    ("answered", None, "", True),
    ("timeout", None, None, False),
    ("cancelled", None, None, False),
    ("dismissed", None, None, False),
])
def test_ok_means_an_answer_that_is_a_yes(status, choice, value, ok):
    ans = prompts.OperatorAnswer(prompt_id="op-1", status=status, choice=choice, value=value)
    assert ans.ok is ok


# ── what an attest answer records ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("sent, recorded", [
    (False, False), ("false", False), ("False", False), (0, False), ("0", False), ("no", False),
    (True, True), ("true", True), ("TRUE", True), (1, True), ("1", True), ("yes", True),
])
def test_the_answer_holds_the_value_the_slot_holds(sent, recorded):
    pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Area safe?", attest_slots=["laser_area_safe"]))
    ans = prompts.answer(pid, by="alice", source="web", value=sent)
    assert ans.value is recorded
    assert optical_state.get_store().get("laser_area_safe").value is recorded
    assert ans.ok is recorded
    assert prompts.get_backend().get_answer(pid).value is recorded, "and that is what is stored and read back"


def test_a_choice_and_a_number_attest_are_ok_and_hold_their_values():
    cid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Which objective?", attest_slots=["objective"]))
    nid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Angle?", attest_slots=["incident_polarizer_deg"]))
    c = prompts.answer(cid, by="alice", source="web", value="50x")
    n = prompts.answer(nid, by="alice", source="web", value="0")
    assert (c.ok, c.value) == (True, "50x")
    assert (n.ok, n.value) == (True, 0.0), "angle 0 is a measurement, not a refusal"


def test_the_http_answer_shows_the_recorded_value(memory_backends):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mast.api.routes import operator as routes

    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    c = TestClient(app, base_url="http://127.0.0.1:7862")
    pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Area safe?", attest_slots=["laser_area_safe"]))
    r = c.post(f"/api/operator/prompts/{pid}/answer", json={"by": "alice", "value": "false"},
               headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["value"] is False


# ── AskOperator ────────────────────────────────────────────────────────────────────────────


def test_ask_operator_reports_a_no_as_a_no():
    with prompts.FakeOperator(lambda p: {"value": False}):
        res = ask({"title": "Is the laser area safe?", "kind": "attest", "severity": "safety",
                   "attest_slots": "laser_area_safe"})
    assert res.success, "the operator answered"
    assert res.data["ok"] is False and res.data["value"] is False
    assert res.data["attested"] == {"laser_area_safe": False}
    assert "laser_area_safe = False" in res.summary and NOT_AN_APPROVAL in res.summary
    assert not optical_state.get_store().is_valid("laser_area_safe", expect=True)


def test_ask_operator_reports_a_yes_without_the_warning():
    with prompts.FakeOperator(lambda p: {"value": True}):
        res = ask({"title": "Is the laser area safe?", "kind": "attest", "attest_slots": "laser_area_safe"})
    assert res.success and res.data["ok"] is True and res.data["attested"] == {"laser_area_safe": True}
    assert "laser_area_safe = True" in res.summary and NOT_AN_APPROVAL not in res.summary


def test_ask_operator_names_every_slot_it_attested():
    store = optical_state.get_store()
    store.set_slots([optical_state.OpticalSlot("filter_a", "A", kind="text"),
                     optical_state.OpticalSlot("filter_b", "B", kind="text")])
    with prompts.FakeOperator(lambda p: {"value": "LP532"}):
        res = ask({"title": "Which filter?", "kind": "attest", "attest_slots": "filter_a,filter_b"})
    assert res.data["attested"] == {"filter_a": "LP532", "filter_b": "LP532"}
    assert "filter_a = 'LP532'" in res.summary and "filter_b = 'LP532'" in res.summary


def test_only_an_attest_has_attested():
    with prompts.FakeOperator(lambda p: {"choice": "no"}):
        res = ask({"title": "Continue?", "kind": "confirm"})
    assert res.success and res.data["ok"] is False and "attested" not in res.data
    with prompts.FakeOperator(lambda p: {"value": "OD2"}):
        res = ask({"title": "Filter?", "kind": "text"})
    assert "attested" not in res.data


def test_a_refused_ask_has_no_attested_and_says_why():
    res = ask({"title": "x", "kind": "attest", "attest_slots": "laser_area_safe", "options": "yes=NO - not safe"})
    assert not res.success and "attested" not in (res.data or {}) and "own options" in res.error


def test_the_skill_card_says_what_a_false_means():
    desc = AskOperator().metadata().description
    assert "attested" in desc and "False" in desc
