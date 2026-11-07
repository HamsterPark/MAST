"""The external agent's side of the operator-in-the-loop: ask, read, wait — never answer.

* ``POST /requests`` with a structured ``kind`` makes an operator prompt; the free-text path
  (the wishlist) is exactly what it was;
* ``GET /requests[/{id}]`` return both kinds, with the structured answer;
* ``/wait`` ``operator_reply`` fires for prompts (answered, dismissed, cancelled or timed out);
* a job whose composite has a ``human`` node (or that runs ``AskOperator``) shows
  ``waiting_operator`` with the prompt id, ends when the operator answers, can be cancelled, and
  fails explicitly when nobody does — without holding the instrument meanwhile.

The operator's answer is given here the way a person's click gives it: ``prompts.answer`` (the
web route calls the same function; ``tests/v2/unit/operator_hitl/test_operator_api.py`` covers the
route). No ext route can do it — ``test_operator_only.py`` pins that.

All of it is the **optical station's** (``mast.station``, design D1 over D8): the tests run under
``MAST_STATION_PROFILE=optical``. An STM station keeps what it had before operator prompts
existed - every ``/requests`` call is a text request and a composite's ``human`` node ends the
job at once - and the last section of this file pins that side.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from _ext_world import H, RAN, wait_terminal

from mast.core.types import SafetyLevel, SkillCategory, SkillMetadata, SkillResult
from mast.i18n import tr
from mast.operator_hitl import human_node, optical_state, prompts
from mast.skills.base import BaseSkill


@pytest.fixture(autouse=True)
def _optical_station(monkeypatch):
    """The tests of this file are about the optical station; the STM ones set their own profile.

    The ext world's probe skills stand for skills an optical station would show. The station
    gate is a whitelist by module (``mast.skills.station_face``) and now classifies the registry
    the door is handed, so a probe defined in a test module would be refused as
    ``skill_disabled``. Give the probes the module of what they stand for: a skill made on this
    machine (``.skills.custom``), which both stations show."""
    monkeypatch.setenv("MAST_STATION_PROFILE", "optical")
    import _ext_world

    for cls in (_ext_world.ExtRead, _ext_world.ExtSlow, _ext_world.ExtWrite, _ext_world.ExtPulse,
                _ext_world.ExtBoom, _ext_world.ExtStopScan, ExtHumanProbe):
        monkeypatch.setattr(cls, "__module__", "mast.skills.custom.ext_probes")


@pytest.fixture(autouse=True)
def _memory_backends():
    old_b = prompts.set_backend(prompts.InMemoryPromptBackend())
    old_s = optical_state.set_store(optical_state.OpticalStateStore())
    yield
    prompts.set_backend(old_b)
    optical_state.set_store(old_s)


def ask(world, body, actor="tester"):
    return world.client.post("/requests", json=body, headers=H(actor))


def get(world, rid, actor="tester"):
    return world.client.get(f"/requests/{rid}", headers=H(actor))


CHOICE = {"message": "Which λ/2 is in the excitation path now?", "kind": "choice",
          "options": [{"id": "hwp_532", "label": "532 nm plate"}, {"id": "hwp_1064", "label": "1064 nm plate"}]}


# ── asking ──────────────────────────────────────────────────────────────────


def test_free_text_requests_are_exactly_what_they_were(world):
    r = ask(world, {"message": "那张图在哪？"}).json()
    assert r["ok"] and r["request"]["id"] == "r-1" and r["request"]["kind"] == "question"
    assert r["request"]["agent_id"] == "ext:tester" and "structured" not in r["request"]
    assert prompts.get_backend().snapshot("all", 10)[1] == [], "a free-text request is not a prompt"
    from mast.wishlist import resolve_agent_request

    resolve_agent_request("r-1", "done", note="在这里", path="D:/data")
    got = get(world, "r-1").json()["request"]
    assert got["status"] == "done" and got["path"] == "D:/data" and "answer" not in got


@pytest.mark.parametrize("kind", ["question", "action", "info", "anything-else", "Confirm?"])
def test_other_kinds_stay_on_the_wishlist(world, kind):
    assert ask(world, {"message": "hello", "kind": kind}).json()["request"]["id"].startswith("r-")


def test_a_structured_choice_becomes_an_operator_prompt(world):
    r = ask(world, {**CHOICE, "title": "Which plate?", "severity": "safety", "timeout_s": 600}).json()
    assert r["ok"]
    req = r["request"]
    assert req["id"].startswith("op-") and req["status"] == "pending" and req["prompt_status"] == "open"
    assert req["structured"] is True and req["kind"] == "choice" and req["severity"] == "safety"
    assert req["agent_id"] == "ext:tester" and req["answer"] is None and req["timeout_s"] == 600
    assert [o["id"] for o in req["options"]] == ["hwp_532", "hwp_1064"]
    (p,) = prompts.list_open()
    assert (p.id, p.kind, p.title, p.agent_id) == (req["id"], "choice", "Which plate?", "ext:tester")
    assert p.message == CHOICE["message"]
    assert p.context["actor"] == "tester" and p.context["session"] == "s1"
    assert p.context["experiment_id"] == world.eid


def test_kind_matching_ignores_case_and_the_title_defaults_to_the_first_line(world):
    req = ask(world, {"message": "First line is the heading\nsecond line explains", "kind": "CONFIRM"}).json()["request"]
    assert req["kind"] == "confirm" and req["title"] == "First line is the heading"


def test_every_structured_kind_can_be_asked(world):
    number = ask(world, {"message": "Angle?", "kind": "number", "number_min": 0, "number_max": 360,
                         "unit": "deg"}).json()["request"]
    assert (number["number_min"], number["number_max"], number["unit"]) == (0, 360, "deg")
    text = ask(world, {"message": "Which filter?", "kind": "text"}).json()["request"]
    confirm = ask(world, {"message": "Beam blocked?", "kind": "confirm"}).json()["request"]
    attest = ask(world, {"message": "Sign off the λ/2", "kind": "attest", "attest_slots": ["excitation_waveplate"],
                         "options": CHOICE["options"]}).json()["request"]
    assert attest["attest_slots"] == ["excitation_waveplate"]
    assert {r["id"] for r in (number, text, confirm, attest)} == {p.id for p in prompts.list_open()}


@pytest.mark.parametrize("body, code_part", [
    ({"message": "x", "kind": "choice", "options": [{"id": "a", "label": "A"}]}, "at least two options"),
    ({"message": "x", "kind": "choice"}, "at least two options"),
    ({"message": "x", "kind": "attest"}, "attest_slots"),
    ({"message": "x", "kind": "attest", "attest_slots": ["no_such_slot"]}, "unknown optical-state slot"),
    ({"message": "x", "kind": "number", "number_min": 5, "number_max": 1}, "number_min"),
    ({"message": "x", "kind": "confirm", "severity": "urgent"}, "severity"),
    ({"message": "x", "kind": "choice", "options": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]}, "unique"),
])
def test_an_invalid_prompt_is_a_422_and_nothing_is_shown_to_the_operator(world, body, code_part):
    r = ask(world, body)
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "invalid_prompt" and code_part in r.json()["detail"]
    assert prompts.list_open() == []


@pytest.mark.parametrize("field", [
    {"options": [{"id": "a", "label": "A"}]}, {"number_min": 1.0}, {"unit": "deg"}, {"attest_slots": ["x"]},
    {"severity": "safety"}, {"timeout_s": 5}, {"title": "t"}])
def test_structured_fields_with_a_free_text_kind_are_refused_not_ignored(world, field):
    r = ask(world, {"message": "x", "kind": "question", **field})
    assert r.status_code == 422 and r.json()["error"] == "structured_fields_need_kind", r.text
    assert "confirm" in r.json()["kinds"]
    assert world.client.get("/requests", headers=H()).json()["count"] == 0


@pytest.mark.parametrize("field", [
    {"timeout_s": 0}, {"timeout_s": 10 ** 7}, {"number_min": float("inf")}, {"title": "x" * 201},
    {"options": [{"id": "", "label": "A"}]}, {"unit": "x" * 21}])
def test_out_of_range_structured_fields_are_rejected_by_the_schema(world, field):
    import json

    r = world.client.post("/requests", content=json.dumps({"message": "x", "kind": "confirm", **field}, allow_nan=True),
                          headers={**H(), "Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request", r.text


def test_the_same_open_question_twice_is_one_prompt(world):
    a = ask(world, CHOICE).json()["request"]["id"]
    b = ask(world, CHOICE).json()["request"]["id"]
    assert a == b and len(prompts.list_open()) == 1
    assert ask(world, {**CHOICE, "message": "A different question"}).json()["request"]["id"] != a
    assert ask(world, CHOICE, actor="other").json()["request"]["id"] != a, "another agent's identical question is its own"


def test_an_agent_cannot_bury_the_operator_under_its_own_questions(world):
    from mast.api.ext.collab import MAX_OPEN_PROMPTS_PER_AGENT

    ids = [ask(world, {"message": f"question {i}", "kind": "confirm"}).json()["request"]["id"]
           for i in range(MAX_OPEN_PROMPTS_PER_AGENT)]
    r = ask(world, {"message": "one too many", "kind": "confirm"})
    assert r.status_code == 429 and r.json()["error"] == "too_many_open_prompts", r.text
    assert r.json()["open"] == MAX_OPEN_PROMPTS_PER_AGENT
    assert len(prompts.list_open()) == MAX_OPEN_PROMPTS_PER_AGENT
    # asking the same open question again is still answered with the original, at the limit
    assert ask(world, {"message": "question 0", "kind": "confirm"}).json()["request"]["id"] == ids[0]
    # another agent is not held up by this one
    assert ask(world, {"message": "mine", "kind": "confirm"}, actor="other").status_code == 200
    # an answer frees a place
    prompts.answer(ids[0], by="alice", source="web", choice="yes")
    assert ask(world, {"message": "now it fits", "kind": "confirm"}).status_code == 200


def test_oversized_text_is_a_422_invalid_prompt(world):
    r = ask(world, {"message": "x" * 3900, "kind": "confirm", "title": "t" * 200})
    assert r.status_code == 200, "within the schema's and the prompt's bounds"
    r = ask(world, {"message": "x", "kind": "choice",
                    "options": [{"id": f"o{i}", "label": "l"} for i in range(24)]})
    assert r.status_code == 200
    assert ask(world, {"message": "x", "kind": "choice",
                       "options": [{"id": f"p{i}", "label": "l"} for i in range(25)]}).status_code == 422


def test_a_prompt_with_a_timeout_expires_and_says_so(world):
    rid = ask(world, {"message": "quick?", "kind": "confirm", "timeout_s": 0.05}).json()["request"]["id"]
    time.sleep(0.15)
    req = get(world, rid).json()["request"]
    assert req["status"] == "dismissed" and req["prompt_status"] == "timeout"
    assert req["answer"]["status"] == "timeout" and req["answer"]["ok"] is False


# ── reading ─────────────────────────────────────────────────────────────────


def test_the_structured_answer_comes_back(world):
    rid = ask(world, CHOICE).json()["request"]["id"]
    assert get(world, rid).json()["request"]["status"] == "pending"
    prompts.answer(rid, by="alice", source="web", choice="hwp_1064", note="the thick one")
    req = get(world, rid).json()["request"]
    assert req["status"] == "done" and req["prompt_status"] == "answered"
    assert req["answer"] == {"status": "answered", "choice": "hwp_1064", "value": "hwp_1064",
                             "note": "the thick one", "answered_by": "alice", "source": "web",
                             "answered_at": req["answer"]["answered_at"], "ok": True}
    assert req["note"] == "the thick one" and req["resolved_at"] == req["answer"]["answered_at"]


def test_where_an_answer_came_from_is_for_the_operator_not_for_the_agent(world, hub):
    """The operator API stores ``client`` (peer address, user agent, how the request
    authenticated) with every answer so the operator can check it afterwards. None of it is
    in what an external agent reads or waits for: the contract (``contract_v1.json``) has no such
    field and an agent has no use for another person's address."""
    import json

    client = {"peer": "192.0.2.77", "user_agent": "Mozilla/5.0 (secret-agent-string)", "browser": True,
              "origin": "http://192.0.2.1:7862", "sec_fetch_site": "same-origin", "sec_fetch_mode": "cors",
              "referer": True, "auth": "basic", "auth_enforced": True, "host": "192.0.2.1:7862"}
    rid = ask(world, CHOICE).json()["request"]["id"]
    prompts.answer(rid, by="alice", source="web", choice="hwp_532", client=client)
    assert prompts.get_backend().get_answer(rid).client == client, "it is recorded ..."

    one = get(world, rid).json()
    listing = world.client.get("/requests", headers=H()).json()
    out = _wait(hub, [{"type": "operator_reply", "request_id": rid}], timeout_s=5)
    for name, body in (("GET /requests/{id}", one), ("GET /requests", listing), ("/wait", out)):
        text = json.dumps(body)
        assert "192.0.2.77" not in text and "secret-agent-string" not in text, name
        assert '"client"' not in text and '"peer"' not in text and '"auth_enforced"' not in text, name
    assert set(one["request"]["answer"]) == {"status", "choice", "value", "note", "answered_by", "source",
                                             "answered_at", "ok"}, "... and the agent sees exactly what it saw before"


def test_numbers_and_text_come_back_as_values(world):
    n = ask(world, {"message": "Angle?", "kind": "number", "unit": "deg"}).json()["request"]["id"]
    t = ask(world, {"message": "Filter?", "kind": "text"}).json()["request"]["id"]
    prompts.answer(n, by="alice", source="launcher", value=47.5)
    prompts.answer(t, by="alice", source="web", value="OD2")
    assert get(world, n).json()["request"]["answer"]["value"] == 47.5
    assert get(world, t).json()["request"]["answer"]["value"] == "OD2"


def test_a_dismissed_or_cancelled_prompt_reads_as_dismissed(world):
    a = ask(world, {"message": "a", "kind": "confirm"}).json()["request"]["id"]
    b = ask(world, {"message": "b", "kind": "confirm"}).json()["request"]["id"]
    prompts.dismiss(a, by="alice", source="web", reason="no longer needed")
    prompts.cancel(b, "service restarted")
    ra, rb = get(world, a).json()["request"], get(world, b).json()["request"]
    assert (ra["status"], ra["prompt_status"], ra["note"]) == ("dismissed", "dismissed", "no longer needed")
    assert (rb["status"], rb["prompt_status"]) == ("dismissed", "cancelled")
    assert ra["answer"]["answered_by"] == "alice" and rb["answer"]["source"] == "system"


def test_an_agent_cannot_play_down_a_laser_area_attestation(world):
    req = ask(world, {"message": "Quick check, nothing important", "kind": "attest", "severity": "info",
                      "attest_slots": ["laser_area_safe"]}).json()["request"]
    assert req["severity"] == "safety" and prompts.get(req["id"]).severity == "safety"


def test_an_attest_prompt_can_only_be_answered_by_the_operator_path(world):
    rid = ask(world, {"message": "Sign off", "kind": "attest", "attest_slots": ["excitation_waveplate"],
                      "options": CHOICE["options"]}).json()["request"]["id"]
    for method, path in (("post", f"/requests/{rid}/answer"), ("put", f"/requests/{rid}")):
        assert getattr(world.client, method)(path, json={"by": "tester", "choice": "hwp_532"},
                                             headers=H()).status_code in (404, 405)
    assert optical_state.get_store().get("excitation_waveplate") is None
    prompts.answer(rid, by="alice", source="web", choice="hwp_532")
    assert optical_state.get_store().get("excitation_waveplate").value == "hwp_532"
    assert get(world, rid).json()["request"]["attest_slots"] == ["excitation_waveplate"]


def test_listing_shows_my_requests_of_both_kinds_open_first(world):
    wish = ask(world, {"message": "free text"}).json()["request"]["id"]
    done = ask(world, {"message": "will be answered", "kind": "confirm"}).json()["request"]["id"]
    still = ask(world, {"message": "stays open", "kind": "confirm"}).json()["request"]["id"]
    theirs = ask(world, {"message": "not mine", "kind": "confirm"}, actor="other").json()["request"]["id"]
    prompts.answer(done, by="alice", source="web", choice="yes")
    rows = world.client.get("/requests", headers=H()).json()
    assert rows["count"] == 3 and theirs not in [r["id"] for r in rows["requests"]]
    assert [r["status"] for r in rows["requests"]] == ["pending", "pending", "done"]
    assert {r["id"] for r in rows["requests"][:2]} == {wish, still}
    only_done = world.client.get("/requests", params={"status": "done"}, headers=H()).json()["requests"]
    assert [r["id"] for r in only_done] == [done]
    only_open = world.client.get("/requests", params={"status": "open"}, headers=H()).json()["requests"]
    assert {r["id"] for r in only_open} == {wish, still}


def test_an_unknown_request_is_404(world):
    r = get(world, "op-9999-nope")
    assert r.status_code == 404 and r.json()["error"] == "unknown_request"


# ── waiting ─────────────────────────────────────────────────────────────────


def _wait(hub, conditions, timeout_s=5.0):
    return asyncio.run(hub.wait(conditions, timeout_s=timeout_s))


@pytest.fixture
def hub():
    from mast.vista.wait import WaitHub, default_wishlist_lookup

    return WaitHub(wishlist_lookup=default_wishlist_lookup)


def test_an_operator_reply_wait_ends_when_the_prompt_is_answered(world, hub):
    rid = ask(world, CHOICE).json()["request"]["id"]
    threading.Timer(0.3, prompts.answer, args=(rid,),
                    kwargs=dict(by="alice", source="web", choice="hwp_532", note="ok")).start()
    t0 = time.monotonic()
    out = _wait(hub, [{"type": "operator_reply", "request_id": rid}], timeout_s=10)
    assert time.monotonic() - t0 < 5 and not out["timed_out"]
    (hit,) = out["fired"]
    ev = hit["event"]
    assert hit["type"] == "operator_reply" and ev["request_id"] == rid
    assert ev["structured"] is True and ev["status"] == "done" and ev["prompt_status"] == "answered"
    assert (ev["choice"], ev["value"], ev["answered_by"], ev["ok"]) == ("hwp_532", "hwp_532", "alice", True)
    assert ev["note"] == "ok" and hit["backlog"] is False


def test_an_already_answered_prompt_fires_at_once_and_says_it_is_old(world, hub):
    rid = ask(world, {"message": "Angle?", "kind": "number"}).json()["request"]["id"]
    prompts.answer(rid, by="alice", source="web", value=12)
    t0 = time.monotonic()
    out = _wait(hub, [{"type": "operator_reply", "request_id": rid}], timeout_s=10)
    assert time.monotonic() - t0 < 3
    assert out["fired"][0]["event"]["value"] == 12.0 and out["fired"][0]["backlog"] is True


def test_dismissal_cancellation_and_timeout_end_the_wait_too(world, hub):
    ids = {k: ask(world, {"message": k, "kind": "confirm", **({"timeout_s": 0.1} if k == "timeout" else {})}
                  ).json()["request"]["id"] for k in ("dismiss", "cancel", "timeout")}
    prompts.dismiss(ids["dismiss"], by="alice", source="web")
    prompts.cancel(ids["cancel"], "service restarted")
    time.sleep(0.2)
    for kind, expect in (("dismiss", "dismissed"), ("cancel", "cancelled"), ("timeout", "timeout")):
        out = _wait(hub, [{"type": "operator_reply", "request_id": ids[kind]}], timeout_s=10)
        ev = out["fired"][0]["event"]
        assert (ev["status"], ev["prompt_status"], ev["ok"]) == ("dismissed", expect, False), kind


def test_an_unanswered_prompt_does_not_end_the_wait(world, hub):
    rid = ask(world, CHOICE).json()["request"]["id"]
    out = _wait(hub, [{"type": "operator_reply", "request_id": rid}], timeout_s=0.5)
    assert out["timed_out"] and out["fired"] == []


def test_waiting_for_any_reply_hears_a_prompt_answered_meanwhile(world, hub):
    rid = ask(world, CHOICE).json()["request"]["id"]
    _wait(hub, [{"type": "timer", "seconds": 0}], timeout_s=1)          # the hub has now seen it open
    threading.Timer(0.3, prompts.answer, args=(rid,),
                    kwargs=dict(by="alice", source="web", choice="hwp_532")).start()
    out = _wait(hub, [{"type": "operator_reply"}], timeout_s=10)
    assert [f["event"]["request_id"] for f in out["fired"]] == [rid]


def test_wishlist_replies_are_unchanged_for_the_wait(world, hub):
    from mast.wishlist import resolve_agent_request

    rid = ask(world, {"message": "where is the file?"}).json()["request"]["id"]
    _wait(hub, [{"type": "timer", "seconds": 0}], timeout_s=1)
    threading.Timer(0.3, resolve_agent_request, args=(rid, "done"), kwargs=dict(note="here", path="D:/x")).start()
    ev = _wait(hub, [{"type": "operator_reply", "request_id": rid}], timeout_s=10)["fired"][0]["event"]
    assert ev["status"] == "done" and ev["path"] == "D:/x" and "structured" not in ev


# ── jobs: a human node (or AskOperator) waiting for the operator ──────────────


class ExtHumanProbe(BaseSkill):
    """A skill with a human node, as a composite would have it (READ: takes no instrument token)."""

    def metadata(self):
        return SkillMetadata(name="ExtHumanProbe", version="1.0.0", category=SkillCategory.READ,
                             safety_level=SafetyLevel.AUTO, description="human node probe", tags=["read"])

    def execute(self, context, params):
        from mast.skills.composite.interpreter import human_channel

        RAN.append(("ExtHumanProbe", dict(params)))
        decision = human_channel({"kind": "workflow_human", "workflow": "ExtHumanProbe", "node_id": "swap",
                                  "message": "Swap the plate, then press done", "routes": ["done", "cancel"]})
        return SkillResult(skill_name="ExtHumanProbe", success=True, data={"decision": decision})


@pytest.fixture
def probe_world(world):
    from mast.skills.builtins.operator_skills import AskOperator

    world.reg.register(ExtHumanProbe)
    world.reg.register(AskOperator)
    return world


def submit(world, skill, params=None, actor="tester"):
    r = world.client.post("/jobs", json={"skill": skill, "params": params or {}}, headers=H(actor))
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def job(world, jid, actor="tester"):
    return world.client.get(f"/jobs/{jid}", headers=H(actor)).json()


def wait_waiting(world, jid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = job(world, jid)
        if v.get("waiting_operator"):
            return v
        assert not v["terminal"], v
        time.sleep(0.02)
    raise AssertionError("the job never showed waiting_operator")


def test_a_job_waiting_for_the_operator_says_so_and_carries_on_when_answered(probe_world):
    w = probe_world
    jid = submit(w, "ExtHumanProbe")
    v = wait_waiting(w, jid)
    wo = v["waiting_operator"]
    assert v["state"] == "running" and wo["kind"] == "choice" and wo["title"] and wo["since"]
    p = prompts.get(wo["prompt_id"])
    assert p.status == "open" and p.job_id == jid and p.skill == "ExtHumanProbe" and p.agent_id == "ext:tester"
    assert [o.id for o in p.options] == ["done", "cancel"]
    # the agent can find the prompt among its own requests and wait on it by id
    mine = w.client.get("/requests", headers=H()).json()["requests"]
    assert [r["id"] for r in mine] == [wo["prompt_id"]]
    prompts.answer(p.id, by="alice", source="web", choice="done", note="swapped")
    final = wait_terminal(w.client, jid)
    assert final["state"] == "succeeded" and final["result"]["data"]["decision"]["route"] == "done"
    assert final["result"]["data"]["decision"]["note"] == "swapped"
    assert "waiting_operator" not in final and final["refused_by"] is None
    assert "waiting_operator" not in w.jm.journal_path.read_text(encoding="utf-8"), "a live-only field"


def test_ask_operator_on_its_own_holds_no_instrument_token(probe_world):
    """``AskOperator`` is a READ skill: a job that only asks takes no token, so waiting for the
    operator does not stop anybody else from using the instrument. (This is all that "the wait
    holds no token" ever held for. A *composite* with a human node is another matter - see
    ``test_a_composite_waiting_for_the_operator_keeps_the_instrument``.)"""
    from mast.core.instrument_lock import instrument_lock

    w = probe_world
    jid = submit(w, "AskOperator", {"title": "Ready?", "kind": "confirm", "timeout_s": 60})
    pid = wait_waiting(w, jid)["waiting_operator"]["prompt_id"]
    assert instrument_lock().snapshot() is None, "no token while only asking"
    other = submit(w, "ExtWrite", actor="other")
    assert wait_terminal(w.client, other, actor="other")["state"] == "succeeded", "not refused_busy"
    prompts.cancel(pid, "test over")
    wait_terminal(w.client, jid)


HUMAN_COMPOSITE = {
    "name": "HumanComposite", "description": "step, a person's decision, step", "version": 1,
    "safety_level": "confirm", "params": [],
    "nodes": [
        {"type": "step", "id": "first", "skill": "ExtWrite", "params": {}},
        {"type": "human", "id": "swap", "message": "Swap the plate, then press done",
         "routes": {"done": [{"type": "step", "id": "second", "skill": "ExtWrite", "params": {}}],
                    "cancel": []}},
    ],
}


@pytest.fixture
def composite_world(probe_world, tmp_path, monkeypatch):
    """``probe_world`` plus a real composite skill (a WRITE step, a ``human`` node, a WRITE step)
    and the real ``StopSpectrometer`` (a stop that works while somebody else holds the token)."""
    from mast.skills.builtins.optics_spectro import StopSpectrometer
    from mast.skills.composite import graph_executor as ge
    from mast.skills.composite.interpreter import make_spec_skill
    from mast.skills.composite.spec import CompositeSpec

    d = tmp_path / "composite_progress"
    d.mkdir()
    monkeypatch.setattr(ge, "_sidecar_path",
                        lambda name, run_id="": d / (f"{name}__{run_id}.json" if run_id else f"{name}.json"))
    monkeypatch.setattr(ge, "_sidecar_dir", lambda: d)
    probe_world.reg.register(make_spec_skill(CompositeSpec.from_dict(HUMAN_COMPOSITE), probe_world.reg))
    probe_world.reg.register(StopSpectrometer)
    return probe_world


def test_a_composite_waiting_for_the_operator_keeps_the_instrument(composite_world):
    """Decision: on the optical station a composite that waits for a person
    **keeps the instrument token** for the whole wait, like a measurement protocol does.

    The alternative - letting go while the person decides - would let another agent change the
    instrument in the meantime, and the composite's next step would run on an instrument that is
    no longer as its first steps left it (the plate the person swaps to is the one the *next* step
    assumes; so is every setting the earlier steps made). The price is that nobody else can write
    to the instrument until the person has answered, the job is cancelled or the wait ends (at
    most 4 h, 30 min unless the skill says otherwise); the refusal names the holder
    (``refused_busy``) and the stop-class skills still work (``BYPASS_NAMES``), so safety does not
    wait for a person.

    (The probe this test replaces was a READ skill, which never takes the token: it verified
    nothing. A composite is what takes it, and AskOperator, a READ skill, holds none.)"""
    from mast.core.instrument_lock import BYPASS_NAMES, instrument_lock

    w = composite_world
    jid = submit(w, "HumanComposite")
    pid = wait_waiting(w, jid)["waiting_operator"]["prompt_id"]
    ran_before = [n for n, _ in RAN]
    assert ran_before == ["ExtWrite"], "the first step ran, the second waits for the person"

    # 1. the token is held - by this composite, for this agent - while the person decides
    held = instrument_lock().snapshot()
    assert held is not None and held["skill"] == "HumanComposite" and "ext:tester" in held["owner"], held
    assert instrument_lock().acquire(owner="a thread of the test", skill="x", timeout_s=0) is False

    # 2. so another write is refused, with the holder named - not run on an instrument that may be about to change
    other = submit(w, "ExtWrite", actor="other")
    refused = wait_terminal(w.client, other, actor="other", total_s=30)
    assert refused["state"] == "refused_busy", refused
    assert refused["busy_holder"]["skill"] == "HumanComposite" and "ext:tester" in refused["busy_holder"]["owner"]
    assert [n for n, _ in RAN] == ran_before, "the refused write never reached the instrument"

    # 3. a stop-class skill still runs: safety does not wait for a person
    assert "StopSpectrometer" in BYPASS_NAMES
    stop = submit(w, "StopSpectrometer", actor="other")
    assert wait_terminal(w.client, stop, actor="other")["state"] == "succeeded"
    assert instrument_lock().snapshot()["skill"] == "HumanComposite", "and it did not disturb the holder"

    # 4. cancelling the job withdraws the prompt and gives the token back
    assert w.client.post(f"/jobs/{jid}/cancel", json={"reason": "done waiting"}, headers=H()).status_code == 200
    final = wait_terminal(w.client, jid)
    assert final["state"] == "cancelled" and prompts.get(pid).status == "cancelled"
    assert instrument_lock().snapshot() is None, "the token is free again"
    again = submit(w, "ExtWrite", actor="other")
    assert wait_terminal(w.client, again, actor="other")["state"] == "succeeded"


def test_answering_lets_the_composite_finish_and_frees_the_token(composite_world):
    from mast.core.instrument_lock import instrument_lock

    w = composite_world
    jid = submit(w, "HumanComposite")
    pid = wait_waiting(w, jid)["waiting_operator"]["prompt_id"]
    assert instrument_lock().snapshot()["skill"] == "HumanComposite"
    prompts.answer(pid, by="alice", source="web", choice="done")
    final = wait_terminal(w.client, jid)
    assert final["state"] == "succeeded", final
    assert [n for n, _ in RAN] == ["ExtWrite", "ExtWrite"], "both steps ran, in order, around the wait"
    assert instrument_lock().snapshot() is None


def test_cancelling_a_waiting_job_withdraws_the_prompt(probe_world):
    w = probe_world
    jid = submit(w, "ExtHumanProbe")
    pid = wait_waiting(w, jid)["waiting_operator"]["prompt_id"]
    t0 = time.monotonic()
    assert w.client.post(f"/jobs/{jid}/cancel", json={"reason": "changed my mind"}, headers=H()).status_code == 200
    final = wait_terminal(w.client, jid)
    assert time.monotonic() - t0 < 5
    assert final["state"] == "cancelled" and "waiting_operator" not in final
    assert prompts.get(pid).status == "cancelled" and prompts.list_open() == []
    assert "changed my mind" in w.jm.get(jid).cancel_reason


def test_a_job_whose_prompt_nobody_answers_fails_explicitly(probe_world, monkeypatch):
    monkeypatch.setattr(human_node, "DEFAULT_TIMEOUT_S", 0.4)
    w = probe_world
    jid = submit(w, "ExtHumanProbe")
    pid = wait_waiting(w, jid)["waiting_operator"]["prompt_id"]
    final = wait_terminal(w.client, jid)
    assert final["state"] == "failed" and final["refused_by"] is None
    err = final["result"]["error"]
    assert "HumanNodeFailed" in err and pid in err and "0.4" in err
    assert prompts.get(pid).status == "timeout"
    assert "waiting_operator" not in final


def test_a_dismissed_prompt_fails_the_job_and_says_who_dismissed_it(probe_world):
    w = probe_world
    jid = submit(w, "ExtHumanProbe")
    pid = wait_waiting(w, jid)["waiting_operator"]["prompt_id"]
    prompts.dismiss(pid, by="alice", source="web", reason="the sample is gone")
    final = wait_terminal(w.client, jid)
    assert final["state"] == "failed" and "the sample is gone" in final["result"]["error"]


def test_ask_operator_as_a_job_shows_waiting_operator_and_returns_the_answer(probe_world):
    w = probe_world
    jid = submit(w, "AskOperator", {"title": "Is the polarizer at 45 deg?", "kind": "number",
                                    "number_min": 0, "number_max": 360, "unit": "deg", "timeout_s": 60})
    v = wait_waiting(w, jid)
    p = prompts.get(v["waiting_operator"]["prompt_id"])
    assert p.kind == "number" and p.job_id == jid and p.skill == "AskOperator" and p.agent_id == "ext:tester"
    prompts.answer(p.id, by="alice", source="launcher", value=44.5)
    final = wait_terminal(w.client, jid)
    assert final["state"] == "succeeded"
    data = final["result"]["data"]
    assert data["value"] == 44.5 and data["answered_by"] == "alice" and data["status"] == "answered"
    assert "waiting_operator" not in final


def test_ask_operator_timing_out_is_a_failed_job_with_the_reason(probe_world):
    w = probe_world
    jid = submit(w, "AskOperator", {"title": "Anyone?", "timeout_s": 1})
    pid = wait_waiting(w, jid)["waiting_operator"]["prompt_id"]
    final = wait_terminal(w.client, jid)
    assert final["state"] == "failed" and pid in final["result"]["error"]
    assert final["result"]["data"]["status"] == "timeout"


def test_the_job_view_of_an_ordinary_job_is_unchanged(probe_world):
    jid = submit(probe_world, "ExtRead")
    final = wait_terminal(probe_world.client, jid)
    assert final["state"] == "succeeded" and "waiting_operator" not in final


# ── what an agent's attest prompt may say, and how many prompts it may leave open ────────


def test_an_attest_prompt_cannot_make_a_click_on_not_safe_sign_safe(world):
    """Options [{"id": "yes", "label": "NO - NOT safe"}] on laser_area_safe."""
    body = {"message": "Pick one to close this card.", "title": "Please acknowledge the shift summary",
            "kind": "attest", "attest_slots": ["laser_area_safe"],
            "options": [{"id": "yes", "label": "NO - the laser area is NOT safe"}, {"id": "no", "label": "Later"}]}
    r = ask(world, body)
    assert r.status_code == 422 and r.json()["error"] == "invalid_prompt", r.text
    assert "cannot carry its own options" in r.json()["detail"]
    assert prompts.list_open() == [], "the operator was shown nothing"
    # the words are innocent, the ids are the point
    body["options"] = [{"id": "true", "label": "Dismiss"}, {"id": "false", "label": "Keep"}]
    assert ask(world, body).status_code == 422
    # the same prompt without options is a normal safety prompt for the operator to answer
    body.pop("options")
    r = ask(world, body)
    assert r.status_code == 200 and r.json()["request"]["severity"] == "safety"
    assert r.json()["request"]["options"] == []


def test_a_choice_slot_attest_shows_the_slots_labels_not_the_agents(world):
    r = ask(world, {"message": "Which plate is in?", "kind": "attest", "attest_slots": ["excitation_waveplate"],
                    "options": [{"id": "hwp_1064", "label": "The 532 nm plate, safe to pick"},
                                {"id": "hwp_532", "label": "1064"}]})
    assert r.status_code == 200, r.text
    assert [(o["id"], o["label"]) for o in r.json()["request"]["options"]] == [
        ("hwp_1064", "λ/2 @1064 nm"), ("hwp_532", "λ/2 @532 nm")]
    bad = ask(world, {"message": "Which plate is in?", "kind": "attest", "attest_slots": ["excitation_waveplate"],
                      "options": [{"id": "hwp_532", "label": "x"}, {"id": "a_plate_nobody_has", "label": "y"}]})
    assert bad.status_code == 422 and "a_plate_nobody_has" in bad.json()["detail"]


def test_a_number_slot_attest_takes_its_unit_and_range_from_the_slot(world):
    r = ask(world, {"message": "Angle?", "kind": "attest", "attest_slots": ["incident_polarizer_deg"],
                    "unit": "mW", "number_min": -50, "number_max": 99999})
    req = r.json()["request"]
    assert (req["unit"], req["number_min"], req["number_max"]) == ("deg", 0.0, 360.0)


def test_slots_of_different_kinds_are_not_signed_with_one_click(world):
    r = ask(world, {"message": "Edge filter fitted?", "kind": "attest",
                    "attest_slots": ["edge_filter", "laser_area_safe"]})
    assert r.status_code == 422 and "slots of one kind" in r.json()["detail"]


def test_a_retried_attest_is_still_recognised_as_the_same_question(world):
    body = {"message": "Which plate is in?", "kind": "attest", "attest_slots": ["excitation_waveplate"],
            "options": [{"id": "hwp_532", "label": "my label"}, {"id": "hwp_1064", "label": "my other label"}]}
    first = ask(world, body).json()["request"]["id"]
    assert ask(world, body).json()["request"]["id"] == first
    assert len(prompts.list_open()) == 1


def test_changing_its_name_does_not_get_an_agent_past_the_limit(world):
    """The per-agent limit counts by the name the agent gives itself."""
    prompts.set_backend(prompts.InMemoryPromptBackend(max_open=6, max_open_external=4))
    statuses = [ask(world, {"message": f"q{i}", "kind": "confirm"}, actor=f"rotating-{i}") for i in range(6)]
    assert [r.status_code for r in statuses] == [200, 200, 200, 200, 429, 429]
    refused = statuses[4].json()
    assert refused["error"] == "too_many_open_prompts"
    assert (refused["scope"], refused["open"], refused["limit"]) == ("external", 4, 4)
    assert len(prompts.list_open()) == 4
    # what is left over is for MAST's own prompts
    prompts.post(prompts.OperatorPrompt(kind="confirm", title="a protocol's own question"))
    prompts.post(prompts.OperatorPrompt(kind="confirm", title="and another"))
    r = ask(world, {"message": "now the whole list is full", "kind": "confirm"}, actor="yet-another-name")
    assert r.status_code == 429 and r.json()["scope"] == "total"


def test_a_prompt_without_a_timeout_shows_the_lifetime_it_really_has(world):
    req = ask(world, {"message": "no timeout given", "kind": "confirm"}).json()["request"]
    assert req["timeout_s"] == prompts.DEFAULT_PROMPT_LIFETIME_S == 86400.0
    assert ask(world, {"message": "a week", "kind": "confirm", "timeout_s": 604800}).json()["request"]["timeout_s"] == 604800


def test_an_attest_recorded_false_reads_as_not_ok_to_the_agent(world):
    rid = ask(world, {"message": "Is the laser area safe?", "kind": "attest",
                      "attest_slots": ["laser_area_safe"]}).json()["request"]["id"]
    prompts.answer(rid, by="alice", source="web", value="no")
    ans = get(world, rid).json()["request"]["answer"]
    assert ans["status"] == "answered" and ans["value"] is False and ans["ok"] is False


# ── an STM station: nothing waits for a person, every request is text ─────────────────────────


@pytest.fixture
def stm(monkeypatch):
    monkeypatch.setenv("MAST_STATION_PROFILE", "stm")
    # a human node that wrongly waited for a person must fail its test, not hang the session
    monkeypatch.setattr(human_node, "DEFAULT_TIMEOUT_S", 4.0)


STRUCTURED_BODIES = [
    {"message": "Continue?", "kind": "confirm"},
    {"message": "Which plate?", "kind": "choice", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
    {"message": "Angle?", "kind": "number", "unit": "deg", "number_min": 0, "number_max": 360},
    {"message": "Filter?", "kind": "text", "title": "Edge filter", "severity": "info"},
    {"message": "Area safe?", "kind": "attest", "attest_slots": ["laser_area_safe"], "timeout_s": 600},
]


@pytest.mark.parametrize("body", STRUCTURED_BODIES, ids=lambda b: b["kind"])
def test_on_an_stm_station_every_kind_is_a_text_request(world, stm, body):
    r = ask(world, body)
    assert r.status_code == 200, r.text
    req = r.json()["request"]
    assert req["id"].startswith("r-") and req["kind"] == body["kind"], "the kind is stored as written"
    assert req["message"] == body["message"] and req["status"] == "pending"
    assert "structured" not in req and "options" not in req and "prompt_status" not in req
    assert prompts.get_backend().snapshot("all", 10)[1] == [], "no operator prompt"
    assert prompts.list_open() == []


def test_on_an_stm_station_a_request_is_what_it_was_before_operator_prompts(world, stm):
    plain = ask(world, {"message": "Where is the Au(111) image?"}).json()["request"]
    dressed = ask(world, {"message": "Where is the Au(111) image 2?", "kind": "attest", "title": "T",
                          "attest_slots": ["laser_area_safe"], "options": [{"id": "x", "label": "X"}],
                          "unit": "deg", "number_min": 1, "number_max": 2, "severity": "safety",
                          "timeout_s": 5}).json()["request"]
    assert set(dressed) == set(plain), "the same fields: nothing structured is added"
    assert dressed["kind"] == "attest" and plain["kind"] == "question"


@pytest.mark.parametrize("junk", [
    {"timeout_s": -5}, {"timeout_s": "soon"}, {"options": "not a list"}, {"options": [{"nope": 1}]},
    {"number_min": "low"}, {"title": 7}, {"severity": ["x"]}, {"attest_slots": "laser_area_safe"},
])
def test_on_an_stm_station_the_structured_fields_are_ignored_even_when_malformed(world, stm, junk):
    """An unknown field in a body always was ignored; on an STM station these are unknown fields."""
    r = ask(world, {"message": "hello", "kind": "confirm", **junk})
    assert r.status_code == 200, r.text
    assert r.json()["request"]["id"].startswith("r-")


def test_on_an_stm_station_a_structured_field_with_a_text_kind_is_not_an_error(world, stm):
    """On the optical station that is a 422 structured_fields_need_kind; an STM station never said so."""
    r = ask(world, {"message": "hello", "kind": "question", "unit": "deg"})
    assert r.status_code == 200 and r.json()["request"]["kind"] == "question"


def test_on_an_stm_station_the_text_limits_of_the_old_body_still_apply(world, stm):
    assert ask(world, {"message": "x" * 4001}).status_code == 422
    assert ask(world, {"message": "x", "kind": "k" * 41}).status_code == 422
    assert ask(world, {"message": ""}).status_code == 422


def test_on_an_stm_station_a_text_request_is_answered_the_old_way(world, stm, hub):
    from mast.wishlist import resolve_agent_request

    rid = ask(world, {"message": "Which plate?", "kind": "choice"}).json()["request"]["id"]
    _wait(hub, [{"type": "timer", "seconds": 0}], timeout_s=1)
    threading.Timer(0.3, resolve_agent_request, args=(rid, "done"), kwargs=dict(note="the thick one")).start()
    ev = _wait(hub, [{"type": "operator_reply", "request_id": rid}], timeout_s=10)["fired"][0]["event"]
    assert ev["status"] == "done" and ev["note"] == "the thick one" and "structured" not in ev
    assert get(world, rid).json()["request"]["note"] == "the thick one"


def test_the_same_text_request_on_both_stations_has_the_same_shape(world, monkeypatch):
    shapes = {}
    for profile in ("stm", "optical"):
        monkeypatch.setenv("MAST_STATION_PROFILE", profile)
        r = ask(world, {"message": f"free text on {profile}", "kind": "info"}, actor=f"a-{profile}")
        shapes[profile] = set(r.json()["request"])
    assert shapes["stm"] == shapes["optical"]


def test_on_an_stm_station_a_human_node_does_not_wait_for_an_operator(probe_world, stm):
    """The node is answered "no channel" at once: no prompt, no ``waiting_operator``, no popup."""
    w = probe_world
    jid = submit(w, "ExtHumanProbe")
    final = wait_terminal(w.client, jid, total_s=10)
    assert final["state"] == "succeeded" and final["result"]["data"]["decision"] is None
    assert "waiting_operator" not in final and prompts.get_backend().snapshot("all", 10)[1] == []


def test_on_an_stm_station_a_composite_stops_at_its_human_node_and_lets_go_of_the_instrument(composite_world, stm):
    from mast.core.instrument_lock import instrument_lock

    w = composite_world
    t0 = time.monotonic()
    jid = submit(w, "HumanComposite")
    final = wait_terminal(w.client, jid, total_s=10)
    assert time.monotonic() - t0 < 3.5, "it did not wait for anybody"
    assert final["state"] == "failed" and final["refused_by"] is None
    assert tr("需要 HITL 通道", "needs a HITL channel") in final["result"]["error"]
    assert [n for n, _ in RAN] == ["ExtWrite"], "the step after the human node never ran"
    assert "waiting_operator" not in final and prompts.get_backend().snapshot("all", 10)[1] == []
    assert instrument_lock().snapshot() is None, "no token is held for a person who is not asked"
    other = submit(w, "ExtWrite", actor="other")
    assert wait_terminal(w.client, other, actor="other")["state"] == "succeeded"


@pytest.mark.parametrize("kind", ["Confirm", "ATTEST", "Choice", "nUmBeR"])
def test_on_an_stm_station_the_kind_is_stored_exactly_as_written(world, stm, kind):
    """The optical station reads ``Confirm`` as the structured kind ``confirm`` (case does not
    matter there). An STM station never looked at the word: it is stored as it was written."""
    req = ask(world, {"message": "hello", "kind": kind}).json()["request"]
    assert req["id"].startswith("r-") and req["kind"] == kind
    assert prompts.get_backend().snapshot("all", 10)[1] == []


def test_on_the_optical_station_the_same_word_is_a_prompt(world):
    req = ask(world, {"message": "hello", "kind": "Confirm"}).json()["request"]
    assert req["id"].startswith("op-") and req["kind"] == "confirm" and req["structured"] is True


def test_an_unreadable_station_profile_is_an_stm_station_for_requests(world, monkeypatch):
    """No profile information = STM, the profile every existing machine runs: a request is text
    and a malformed structured field is ignored - in the route and in the body model alike."""
    from mast import station

    def boom():
        raise RuntimeError("settings store is gone")

    monkeypatch.setattr(station, "is_optical", boom)
    r = ask(world, {"message": "hello", "kind": "confirm", "timeout_s": -5, "options": "junk"})
    assert r.status_code == 200, r.text
    assert r.json()["request"]["id"].startswith("r-") and r.json()["request"]["kind"] == "confirm"
    assert prompts.get_backend().snapshot("all", 10)[1] == []


STM_BAD_VALUES = {"title": 7, "options": "junk", "number_min": "low", "number_max": "high", "unit": "x" * 50,
                  "attest_slots": "laser_area_safe", "severity": ["x"], "timeout_s": -5}


def test_on_an_stm_station_every_structured_field_is_dropped_before_it_is_validated(world, stm):
    """One malformed value per structured field. If the field is not in ``STRUCTURED_FIELDS`` the old
    two-field body would be validating it on an STM station: a 422 where an unknown field was ignored."""
    from mast.api.ext.schemas import STRUCTURED_FIELDS, RequestBody

    declared = set(RequestBody.model_fields) - {"message", "kind"}
    assert declared == set(STRUCTURED_FIELDS) == set(STM_BAD_VALUES), \
        "a structured field was added: put it in STRUCTURED_FIELDS and give it a malformed value here"
    for name, bad in STM_BAD_VALUES.items():
        r = ask(world, {"message": f"malformed {name}", "kind": "confirm", name: bad})
        assert r.status_code == 200, (name, r.text)
        assert r.json()["request"]["id"].startswith("r-"), name
