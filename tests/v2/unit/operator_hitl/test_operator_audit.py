"""Where did this write come from? The server records it itself.

The write guard can tell a browser from a program; it cannot tell the operator from another
process on the same machine that can reach the port (a shell the operator gave an AI). On a
loopback service nothing technical can. So every write stores ``client`` — what the *server saw*
of the request — next to the operator's name and the client's own claim ``source``, in the answer,
in the slot value and in the history; the responses show it; the log has one line per write. An
audit goes by ``client``, never by ``source`` (the client types that).

What is pinned: the record is made from the request (never from the body: a ``client`` field is
refused), is complete and bounded, holds no credential, reaches the operator's screens and the
files and survives a restart, and never reaches an external agent, a spectrum sidecar or an MCP
tool.

The ``peer`` is the TCP connection's address and nothing a header says: a program on the machine
can send any X-Forwarded-For, so those headers are recorded as claims beside it
(``claimed_forwarding``) and never instead of it - including over a real socket, where uvicorn
would otherwise replace the address itself.
"""
from __future__ import annotations

import ast
import base64
import contextlib
import http.client
import json
import logging
import pathlib
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from mast.api.access import AccessGate
from mast.api.routes import operator as operator_routes
from mast.api.routes.operator import client_record
from mast.operator_hitl import optical_state, prompts
from mast.operator_hitl.store import (
    HISTORY_FILE,
    PROMPTS_FILE,
    STATE_FILE,
    PersistentOpticalStateStore,
    PersistentPromptBackend,
    operator_dir,
)

PAGE = "http://127.0.0.1:7862"
JSON = {"Content-Type": "application/json"}
LOGGER = "mast.api.routes.operator"

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
BROWSER = {"Origin": PAGE, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
           "User-Agent": BROWSER_UA, "Referer": PAGE + "/optical", **JSON}
URLLIB = {"User-Agent": "Python-urllib/3.13", **JSON}


LAN_USER, LAN_PASS = "op", "s3cr3t-Pa55"


def basic(user=LAN_USER, password=LAN_PASS) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def make_app(*, lan: bool = False) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AccessGate, credentials=(LAN_USER, LAN_PASS) if lan else None)
    app.include_router(operator_routes.router, prefix="/api")
    return app


def make_request(headers: dict | None = None, *, client=("203.0.113.9", 51234), app=None) -> Request:
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "PUT", "path": "/api/operator/optical-state/objective",
                    "query_string": b"", "headers": raw, "client": client, "server": ("127.0.0.1", 7862),
                    "scheme": "http", "app": app or make_app()})


@pytest.fixture
def web(memory_backends):
    return TestClient(make_app(), base_url=PAGE)


def put(c, headers, value="50x", slot="objective", **body):
    return c.put(f"/api/operator/optical-state/{slot}", json={"by": "alice", "value": value, **body},
                 headers=headers)


def contains_key(obj, key) -> bool:
    if isinstance(obj, dict):
        return key in obj or any(contains_key(v, key) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(contains_key(v, key) for v in obj)
    return False


# ── the three kinds of request ──────────────────────────────────


def test_a_browser_is_recorded_as_a_web_page(web):
    r = put(web, BROWSER)
    assert r.status_code == 200, r.text
    assert r.json()["client"] == {
        "peer": "testclient", "user_agent": BROWSER_UA, "browser": True, "origin": PAGE,
        "sec_fetch_site": "same-origin", "sec_fetch_mode": "cors", "referer": True,
        "auth": "none", "auth_enforced": False, "host": "127.0.0.1:7862", "claimed_forwarding": None}
    assert optical_state.get_store().get("objective").client == r.json()["client"]


def test_a_urllib_client_is_recorded_as_a_program(web):
    r = put(web, URLLIB, source="launcher")
    assert r.status_code == 200, r.text
    assert r.json()["client"] == {
        "peer": "testclient", "user_agent": "Python-urllib/3.13", "browser": False, "origin": None,
        "sec_fetch_site": None, "sec_fetch_mode": None, "referer": False,
        "auth": "none", "auth_enforced": False, "host": "127.0.0.1:7862", "claimed_forwarding": None}


def test_basic_credentials_are_recorded_as_a_kind_never_as_content(memory_backends, caplog):
    c = TestClient(make_app(lan=True), base_url=PAGE)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        r = put(c, {**URLLIB, "Authorization": basic()})
    assert r.status_code == 200, r.text
    rec = r.json()["client"]
    assert rec["auth"] == "basic" and rec["auth_enforced"] is True
    everything = json.dumps(optical_state.get_store().history) + r.text + caplog.text + json.dumps(rec)
    for secret in (basic(), basic().split(" ")[1], f"{LAN_USER}:{LAN_PASS}", LAN_PASS):
        assert secret not in everything, secret
    assert "Authorization" not in everything and "authorization" not in json.dumps(rec).lower()


def test_the_session_cookie_is_recorded_as_a_kind_too(memory_backends):
    c = TestClient(make_app(lan=True), base_url=PAGE)
    first = put(c, {**BROWSER, "Authorization": basic()})        # a web page: the gate hands it a session cookie
    assert first.status_code == 200 and first.json()["client"]["auth"] == "basic"
    cookie_names = [name for name in c.cookies.keys()]
    assert any(n.startswith("mast_session") for n in cookie_names), cookie_names
    # the cookie jar now carries the session; no Authorization header this time
    second = put(c, BROWSER, value="10x")
    assert second.status_code == 200, second.text
    rec = second.json()["client"]
    assert rec["auth"] == "session_cookie" and rec["auth_enforced"] is True
    cookie_value = next(iter(c.cookies.jar)).value
    assert cookie_value and cookie_value not in json.dumps(rec) + json.dumps(optical_state.get_store().history)


def test_a_basic_header_on_a_service_that_requires_no_login_is_not_taken_for_one(web):
    """On a loopback service anybody can send "Authorization: Basic ..": the record says what the
    request carried and that nothing checked it."""
    r = put(web, {**URLLIB, "Authorization": basic("anyone", "whatever")})
    assert r.status_code == 200
    assert r.json()["client"]["auth"] == "basic" and r.json()["client"]["auth_enforced"] is False


# ── where the record goes ───────────────────────────────────────────────────────


def test_every_write_stores_and_returns_the_client(web):
    pid = prompts.post(prompts.OperatorPrompt(kind="confirm", title="Continue?"))
    other = prompts.post(prompts.OperatorPrompt(kind="confirm", title="Not needed?"))
    answered = web.post(f"/api/operator/prompts/{pid}/answer", json={"by": "alice", "choice": "yes"}, headers=BROWSER)
    dismissed = web.post(f"/api/operator/prompts/{other}/dismiss", json={"by": "bob", "reason": "stale"}, headers=URLLIB)
    setv = put(web, BROWSER, slot="laser_area_safe", value=True)
    cleared = web.post("/api/operator/optical-state/objective/clear", json={"by": "bob"}, headers=URLLIB)
    for r in (answered, dismissed, setv, cleared):
        assert r.status_code == 200, r.text
    assert answered.json()["client"]["browser"] is True and answered.json()["client"]["referer"] is True
    assert dismissed.json()["client"]["user_agent"] == "Python-urllib/3.13"
    assert setv.json()["client"]["origin"] == PAGE
    assert prompts.get_backend().get_answer(pid).client == answered.json()["client"]
    assert prompts.get_backend().get_answer(other).client == dismissed.json()["client"]

    listing = web.get("/api/operator/prompts", params={"status": "all"}).json()["prompts"]
    by_id = {p["id"]: p for p in listing}
    assert by_id[pid]["answer"]["client"]["browser"] is True
    assert by_id[other]["answer"]["client"]["user_agent"] == "Python-urllib/3.13"

    history = web.get("/api/operator/optical-state/history").json()["records"]
    assert [(h["slot"], h["client"]["browser"]) for h in history] == [
        ("laser_area_safe", True), ("objective", False)], "the clear is in the history with its own client"
    values = web.get("/api/operator/optical-state").json()["values"]
    assert values["laser_area_safe"]["client"]["origin"] == PAGE, "the operator's panel can show where it came from"


def test_an_attest_answer_stamps_its_slots_with_the_answering_client(web):
    pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Area safe?", attest_slots=["laser_area_safe"]))
    r = web.post(f"/api/operator/prompts/{pid}/answer", json={"by": "alice", "value": True}, headers=BROWSER)
    assert r.status_code == 200
    slot = optical_state.get_store().get("laser_area_safe")
    assert slot.client == r.json()["client"] and slot.source == f"prompt:{pid}"
    assert optical_state.get_store().history[-1]["client"] == r.json()["client"]


def test_answers_that_mast_gives_itself_have_no_client(memory_backends):
    pid = prompts.post(prompts.OperatorPrompt(kind="confirm", title="x"))
    prompts.cancel(pid, "service restarted")
    ans = prompts.get_backend().get_answer(pid)
    assert ans.client is None and ans.source == "system"


def test_the_client_survives_a_restart(installed):
    c = TestClient(make_app(), base_url=PAGE)
    pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Area safe?", attest_slots=["laser_area_safe"]))
    other = prompts.post(prompts.OperatorPrompt(kind="confirm", title="Skip?"))
    a = c.post(f"/api/operator/prompts/{pid}/answer", json={"by": "alice", "value": True}, headers=BROWSER)
    d = c.post(f"/api/operator/prompts/{other}/dismiss", json={"by": "alice"}, headers=URLLIB)
    s = put(c, URLLIB, value="50x")
    x = c.post("/api/operator/optical-state/objective/clear", json={"by": "alice"}, headers=BROWSER)
    assert all(r.status_code == 200 for r in (a, d, s, x))

    root = installed.root
    backend = PersistentPromptBackend(root)              # the service restarts
    store = PersistentOpticalStateStore(root)
    assert backend.get_answer(pid).client == a.json()["client"]
    assert backend.get_answer(other).client == d.json()["client"]
    assert store.get("laser_area_safe").client == a.json()["client"]
    lines = [json.loads(x) for x in (operator_dir(root) / HISTORY_FILE).read_text(encoding="utf-8").splitlines()]
    assert [(r["slot"], r["client"]["browser"]) for r in lines] == [
        ("laser_area_safe", True), ("objective", False), ("objective", True)]
    assert store.history_tail(10) == lines
    for name in (PROMPTS_FILE, STATE_FILE):
        assert '"client"' in (operator_dir(root) / name).read_text(encoding="utf-8"), name


def test_a_record_written_before_clients_were_recorded_still_loads(installed):
    """Old files have no ``client`` key anywhere."""
    root = installed.root
    d = operator_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / STATE_FILE).write_text(json.dumps({"version": 1, "seq": 1, "values": {"objective": {
        "value": "50x", "set_by": "alice", "set_at": "2026-11-01T00:00:00+00:00", "source": "web",
        "valid_until": None, "note": ""}}}), encoding="utf-8")
    (d / PROMPTS_FILE).write_text(json.dumps({"version": 1, "seq": 1, "prompts": [{
        "prompt": {"kind": "confirm", "title": "old", "id": "op-0001-aaaaaa", "status": "answered",
                   "created_at": "2026-11-01T00:00:00+00:00"},
        "answer": {"prompt_id": "op-0001-aaaaaa", "status": "answered", "choice": "yes",
                   "answered_by": "alice", "source": "web", "answered_at": "2026-11-01T00:00:01+00:00"}}]}),
        encoding="utf-8")
    store, backend = PersistentOpticalStateStore(root), PersistentPromptBackend(root)
    assert store.get("objective").value == "50x" and store.get("objective").client is None
    assert backend.get_answer("op-0001-aaaaaa").client is None


# ── what a client cannot do ──────────────────────────────────────────────────────

FORGED = {"peer": "127.0.0.1", "user_agent": "the operator's own browser", "browser": True, "auth": "basic"}


@pytest.mark.parametrize("method, path, body", [
    ("post", "/api/operator/prompts/{pid}/answer", {"by": "alice", "choice": "yes"}),
    ("post", "/api/operator/prompts/{pid}/dismiss", {"by": "alice"}),
    ("put", "/api/operator/optical-state/objective", {"by": "alice", "value": "50x"}),
    ("post", "/api/operator/optical-state/objective/clear", {"by": "alice"}),
])
def test_a_client_in_the_body_is_refused_and_nothing_is_written(web, method, path, body):
    pid = prompts.post(prompts.OperatorPrompt(kind="confirm", title="x"))
    r = getattr(web, method)(path.format(pid=pid), json={**body, "client": FORGED}, headers=URLLIB)
    assert r.status_code == 422, r.text
    assert "client" in json.dumps(r.json()), "the refusal names the field"
    assert prompts.get(pid).status == "open" and optical_state.get_store().history == []
    # and the same request without the forged field goes through, recorded as what it is
    ok = getattr(web, method)(path.format(pid=pid), json=body, headers=URLLIB)
    assert ok.status_code == 200, ok.text
    # a clear answers {slot, cleared, seq}: its client is in the history record
    recorded = optical_state.get_store().history[-1]["client"] if path.endswith("/clear") else ok.json()["client"]
    assert recorded["user_agent"] == "Python-urllib/3.13"
    assert recorded["browser"] is False and recorded["auth"] == "none"


def test_source_is_what_the_client_claims_and_client_is_what_the_server_saw(web):
    """The launcher claim from a web page: the claim is kept, the evidence says browser."""
    r = put(web, BROWSER, source="launcher")
    assert r.json()["source"] == "launcher"
    assert r.json()["client"]["browser"] is True and r.json()["client"]["user_agent"] == BROWSER_UA
    r = put(web, URLLIB, source="web", value="10x")
    assert r.json()["source"] == "web"
    assert r.json()["client"]["browser"] is False and r.json()["client"]["user_agent"] == "Python-urllib/3.13"


def test_a_refused_write_leaves_no_record(web):
    forged = put(web, {**BROWSER, "Origin": "http://evil.example"})
    assert forged.status_code == 403
    nojson = web.put("/api/operator/optical-state/objective", content=b'{"by":"a","value":"10x"}',
                     headers={"Content-Type": "text/plain"})
    assert nojson.status_code == 415
    assert optical_state.get_store().history == [] and optical_state.get_store().get("objective") is None


# ── the log ─────────────────────────────────────────────────────────────────────


def lines(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.getMessage().startswith("operator write:")]


def test_a_write_the_guard_refuses_is_logged_with_its_peer(web, caplog):
    """A write the route's own guard refuses never reaches a handler (so it has no
    ``operator write:`` line) but it is in the log as a WARNING, with the address it came from."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        r = web.put("/api/operator/optical-state/objective", content=b'{"by": "a", "value": "10x"}',
                    headers={"Content-Type": "text/plain", "User-Agent": "curl/8.4"})
    assert r.status_code == 415 and lines(caplog) == []
    (rec,) = [x for x in caplog.records if x.name == LOGGER and "refused" in x.getMessage()]
    assert rec.levelno == logging.WARNING
    assert "HTTP 415" in rec.getMessage() and "peer='testclient'" in rec.getMessage()


def test_each_write_logs_one_info_line_with_action_target_name_outcome_and_client(web, caplog):
    pid = prompts.post(prompts.OperatorPrompt(kind="confirm", title="x"))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        web.post(f"/api/operator/prompts/{pid}/answer", json={"by": "alice", "choice": "yes"}, headers=BROWSER)
        put(web, URLLIB, slot="laser_area_safe", value=True)
        web.post("/api/operator/optical-state/laser_area_safe/clear", json={"by": "bob"}, headers=URLLIB)
        qid = prompts.post(prompts.OperatorPrompt(kind="confirm", title="y"))
        web.post(f"/api/operator/prompts/{qid}/dismiss", json={"by": "carol"}, headers=URLLIB)
    got = lines(caplog)
    assert [r.levelno for r in got] == [logging.INFO] * 4
    assert len(got) == 4, "one line per write"
    expect = [("answer", pid, "alice"), ("set_slot", "laser_area_safe", "alice"),
              ("clear_slot", "laser_area_safe", "bob"), ("dismiss", qid, "carol")]
    for rec, (action, target, by) in zip(got, expect):
        msg = rec.getMessage()
        assert f"action={action}" in msg and f"target={json.dumps(target)}" in msg
        assert f"by={json.dumps(by)}" in msg and "outcome=ok" in msg
        client = json.loads(msg.split("client=", 1)[1])
        assert client["peer"] == "testclient" and "browser" in client and "auth_enforced" in client
    assert json.loads(got[0].getMessage().split("client=", 1)[1])["browser"] is True
    assert json.loads(got[1].getMessage().split("client=", 1)[1])["user_agent"] == "Python-urllib/3.13"


def test_a_refused_attempt_is_in_the_trail_as_well(web, caplog):
    pid = prompts.post(prompts.OperatorPrompt(kind="confirm", title="x"))
    prompts.cancel(pid, "gone")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        web.post(f"/api/operator/prompts/{pid}/answer", json={"by": "mallory", "choice": "yes"}, headers=URLLIB)
        web.post("/api/operator/prompts/op-9999-nope/dismiss", json={"by": "mallory"}, headers=URLLIB)
        put(web, URLLIB, slot="incident_polarizer_deg", value=720)
        put(web, URLLIB, slot="no_such_slot", value=1)
    got = [r.getMessage() for r in lines(caplog)]
    assert [m.split("outcome=")[1].split(" ")[0] for m in got] == [
        "refused_409", "refused_404", "refused_422", "refused_404"]
    assert all('by="mallory"' in m or 'by="alice"' in m for m in got)


def test_a_write_mast_could_not_save_is_logged_as_a_failure_not_a_refusal(installed, monkeypatch, caplog):
    c = TestClient(make_app(), base_url=PAGE)
    store = optical_state.get_store()
    monkeypatch.setattr(store, "_write_values", lambda: (_ for _ in ()).throw(PermissionError("read-only")))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        r = put(c, URLLIB, slot="laser_area_safe", value=True)
    assert r.status_code == 503 and store.get("laser_area_safe") is None
    (rec,) = lines(caplog)
    assert "outcome=failed_503" in rec.getMessage() and rec.levelno == logging.WARNING, \
        "a failure is for the operator to look at: WARNING, and not worded as if the request were wrong"
    assert store.history == [], "and the record of the attempt is the log line, not a history entry"


def test_a_name_cannot_forge_a_log_line(web, caplog):
    evil = 'alice\nINFO operator write: action=answer by="root" outcome=ok'
    assert len(evil) <= 80, "short enough to get past the name's length limit"
    with caplog.at_level(logging.INFO, logger=LOGGER):
        r = web.put("/api/operator/optical-state/objective", json={"by": evil, "value": "10x"}, headers=URLLIB)
    assert r.status_code == 200, r.text
    (rec,) = lines(caplog)
    assert len(rec.getMessage().splitlines()) == 1, "the name is JSON-encoded: no second line"
    assert "\\nINFO operator write:" in rec.getMessage() and 'by=\\"root\\"' in rec.getMessage()


# ── client_record itself ──────────────────────────────────────────────────────────


def test_the_record_has_exactly_the_agreed_fields():
    rec = client_record(make_request({"User-Agent": "curl/8.4", "Host": "127.0.0.1:7862"}))
    assert set(rec) == {"peer", "user_agent", "browser", "origin", "sec_fetch_site", "sec_fetch_mode",
                        "referer", "auth", "auth_enforced", "host", "claimed_forwarding"}
    assert rec["peer"] == "203.0.113.9"


def test_long_values_are_cut_and_control_characters_removed():
    rec = client_record(make_request({
        "User-Agent": "x" * 500 + "\x07\x1b", "Origin": "http://h" + "o" * 300, "Host": "h" * 300 + "\x00",
        "Sec-Fetch-Site": "same-origin" + "!" * 100, "Sec-Fetch-Mode": "cors"}))
    assert rec["user_agent"] == "x" * 200
    assert rec["origin"] == ("http://h" + "o" * 300)[:120] and len(rec["host"]) == 120
    assert len(rec["sec_fetch_site"]) == 40
    ctl = client_record(make_request({"User-Agent": "Mozi\x07lla\x1b[31m/5.0\x7f"}))
    assert ctl["user_agent"] == "Mozilla[31m/5.0", "control characters are gone (nothing to forge a log line with)"


def test_referer_is_only_whether_it_was_there_and_nothing_else_is_kept():
    rec = client_record(make_request({"Referer": "https://example.org/secret?token=abc123",
                                      "X-Custom-Thing": "198.51.100.7", "X-Api-Key": "k-123"}))
    assert rec["referer"] is True
    blob = json.dumps(rec)
    for leaked in ("secret", "token", "abc123", "example.org", "198.51.100.7", "k-123"):
        assert leaked not in blob, leaked
    assert client_record(make_request({}))["referer"] is False


def test_browser_means_an_origin_or_any_sec_fetch_header():
    assert client_record(make_request({"Origin": PAGE}))["browser"] is True
    assert client_record(make_request({"Sec-Fetch-Dest": "empty"}))["browser"] is True
    assert client_record(make_request({"Sec-Fetch-User": "?1"}))["browser"] is True
    assert client_record(make_request({"User-Agent": "Mozilla/5.0 (looks like a browser)"}))["browser"] is False, \
        "the claim in the User-Agent proves nothing; the headers a page cannot omit do"
    assert client_record(make_request({"Accept": "text/html"}))["browser"] is False


@pytest.mark.parametrize("headers, auth", [
    ({}, "none"),
    ({"Authorization": "Basic dXNlcjpzZWNyZXQ="}, "basic"),
    ({"Authorization": "basic dXNlcjpzZWNyZXQ="}, "basic"),
    ({"Authorization": "  Basic dXNlcjpzZWNyZXQ="}, "basic"),
    ({"Authorization": "Basic"}, "basic"),
    ({"Authorization": "Bearer abc.def.ghi"}, "none"),
    ({"Authorization": "Basically nothing"}, "none"),
    ({"Cookie": "mast_session_7862=v1.1.2.sig"}, "session_cookie"),
    ({"Cookie": "theme=dark; mast_session=v1.1.2.sig; x=y"}, "session_cookie"),
    ({"Cookie": "other_session=1; mast_sessionx=2"}, "none"),
    ({"Cookie": "mast_session_7862=v1.1.2.sig", "Authorization": "Basic dXNlcjpzZWNyZXQ="}, "basic"),
])
def test_auth_is_the_kind_of_credential_carried(headers, auth):
    rec = client_record(make_request(headers))
    assert rec["auth"] == auth
    blob = json.dumps(rec)
    for secret in ("dXNlcjpzZWNyZXQ", "secret", "v1.1.2", "abc.def.ghi", "theme"):
        assert secret not in blob, secret


def test_auth_enforced_follows_the_apps_gate_not_the_request():
    assert client_record(make_request({"Authorization": "Basic x"}, app=make_app(lan=True)))["auth_enforced"] is True
    assert client_record(make_request({"Authorization": "Basic x"}, app=make_app(lan=False)))["auth_enforced"] is False
    assert client_record(make_request({}, app=FastAPI()))["auth_enforced"] is False, "no gate at all"


def test_a_request_without_a_peer_is_recorded_as_such():
    rec = client_record(make_request({}, client=None))
    assert rec["peer"] is None


# ── what stays away from agents ──────────────────────────────────────────────────────


def test_the_snapshot_and_the_read_skill_do_not_carry_the_client(web):
    from mast.skills.builtins.operator_skills import ReadOpticalState

    put(web, URLLIB, slot="objective", value="50x")
    store = optical_state.get_store()
    assert store.get("objective").client, "it is stored ..."
    assert not contains_key(store.snapshot(), "client"), "... but a sidecar's snapshot never has it"
    assert contains_key(store.snapshot(include_client=True), "client"), "the operator's view asks for it"
    data = ReadOpticalState().execute(None, {}).data
    assert not contains_key(data, "client") and data["values"]["objective"]["value"] == "50x"


def test_an_external_agent_sees_the_answer_but_not_the_client(web):
    from mast.operator_hitl import service
    from mast.vista.wait import _reply_event

    pid = prompts.post(prompts.OperatorPrompt(kind="choice", title="Which?", agent_id="ext:tester", options=[
        prompts.PromptOption("a", "A"), prompts.PromptOption("b", "B")]))
    r = web.post(f"/api/operator/prompts/{pid}/answer", json={"by": "alice", "choice": "b"}, headers=BROWSER)
    assert r.json()["client"]["browser"] is True
    view = service.request_view(prompts.get(pid), prompts.get_backend().get_answer(pid))
    assert view["answer"]["choice"] == "b" and not contains_key(view, "client")
    assert all(not contains_key(row, "client") for row in service.prompt_rows())
    assert not contains_key(_reply_event(view), "client")


# ── the peer is the TCP peer ──────────────────────────────────────────────────────────

FORGED_FORWARDING = {
    "X-Forwarded-For": "203.0.113.77, 198.51.100.4",
    "X-Forwarded-Host": "operator-console.example",
    "X-Forwarded-Proto": "https",
    "X-Real-IP": "203.0.113.77",
    "Forwarded": "for=203.0.113.77;host=operator-console.example;proto=https",
}


def test_a_forged_forwarding_header_never_replaces_the_peer(web):
    r = put(web, {**URLLIB, **FORGED_FORWARDING})
    assert r.status_code == 200, r.text
    rec = r.json()["client"]
    assert rec["peer"] == "testclient", "the connection's address (TestClient's), not the header's"
    assert rec["claimed_forwarding"] == {
        "x-forwarded-for": "203.0.113.77, 198.51.100.4", "x-forwarded-host": "operator-console.example",
        "x-forwarded-proto": "https", "x-real-ip": "203.0.113.77",
        "forwarded": "for=203.0.113.77;host=operator-console.example;proto=https"}
    assert rec["host"] == "127.0.0.1:7862", "the Host header stays the Host header"
    assert optical_state.get_store().get("objective").client == rec, "and that is what is stored ..."
    assert optical_state.get_store().history[-1]["client"] == rec, "... and kept in the history"


def test_the_peer_stays_the_connection_for_every_kind_of_write(web, caplog):
    pid = prompts.post(prompts.OperatorPrompt(kind="confirm", title="x"))
    qid = prompts.post(prompts.OperatorPrompt(kind="confirm", title="y"))
    headers = {**BROWSER, **FORGED_FORWARDING}
    with caplog.at_level(logging.INFO, logger=LOGGER):
        answered = web.post(f"/api/operator/prompts/{pid}/answer", json={"by": "a", "choice": "yes"}, headers=headers)
        dismissed = web.post(f"/api/operator/prompts/{qid}/dismiss", json={"by": "a"}, headers=headers)
        cleared = web.post("/api/operator/optical-state/objective/clear", json={"by": "a"}, headers=headers)
    assert answered.json()["client"]["peer"] == dismissed.json()["client"]["peer"] == "testclient"
    assert optical_state.get_store().history[-1]["client"]["peer"] == "testclient" and cleared.status_code == 200
    for rec in lines(caplog):
        logged = json.loads(rec.getMessage().split("client=", 1)[1])
        assert logged["peer"] == "testclient" and "x-forwarded-for" in logged["claimed_forwarding"]


def test_forwarding_claims_are_verbatim_cut_and_cleaned():
    rec = client_record(make_request({"X-Forwarded-For": "1.2.3.4," + "9" * 400,
                                      "Forwarded": "for=\x07x\x1b[31m", "Via": "1.1 proxy"}))
    claimed = rec["claimed_forwarding"]
    assert set(claimed) == {"x-forwarded-for", "forwarded", "via"}
    assert len(claimed["x-forwarded-for"]) == 200 and claimed["x-forwarded-for"].startswith("1.2.3.4,9")
    assert claimed["forwarded"] == "for=x[31m", "control characters are gone"
    assert claimed["via"] == "1.1 proxy"
    assert rec["peer"] == "203.0.113.9", "the connection's address, whatever was claimed"


def test_a_request_without_forwarding_headers_has_no_claim():
    assert client_record(make_request({"User-Agent": "curl/8.4"}))["claimed_forwarding"] is None


def test_an_address_that_a_proxy_layer_put_there_is_not_recorded_as_the_peer():
    """uvicorn's proxy-headers layer replaces the connection address by an X-Forwarded-For entry
    and gives it port 0; a real connection never has port 0. Such an address came from a header,
    so it is not the peer (the header itself is recorded, as a claim)."""
    rec = client_record(make_request({"X-Forwarded-For": "203.0.113.77"}, client=("203.0.113.77", 0)))
    assert rec["peer"] is None
    assert rec["claimed_forwarding"] == {"x-forwarded-for": "203.0.113.77"}


@contextlib.contextmanager
def real_server(**config):
    """The operator router behind a real uvicorn server on a free port, configured as the test
    says; yields the port."""
    import uvicorn

    app = FastAPI()
    app.include_router(operator_routes.router, prefix="/api")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                           lifespan="off", **config))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(15)


def put_over_a_socket(port: int, headers: dict) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("PUT", "/api/operator/optical-state/objective",
                     body=json.dumps({"by": "alice", "value": "50x"}),
                     headers={"Content-Type": "application/json", **headers})
        resp = conn.getresponse()
        data = json.loads(resp.read())
    finally:
        conn.close()
    assert resp.status == 200, data
    return data["client"]


def test_over_a_real_socket_the_peer_is_the_connection(memory_backends):
    """The server as MAST starts it (``proxy_headers=False``): a program on this machine that
    sends X-Forwarded-For is still recorded as 127.0.0.1."""
    with real_server(proxy_headers=False) as port:
        rec = put_over_a_socket(port, {"X-Forwarded-For": "203.0.113.77"})
    assert rec["peer"] == "127.0.0.1"
    assert rec["claimed_forwarding"] == {"x-forwarded-for": "203.0.113.77"}


def test_over_a_real_socket_a_server_that_trusts_proxy_headers_is_not_believed_either(memory_backends):
    """The control, and the second line of defence: uvicorn's *default* hands the application the
    forged address (that is why MAST turns it off), and even then it is not recorded as the peer."""
    with real_server() as port:                    # uvicorn's defaults: proxy headers on, 127.0.0.1 trusted
        rec = put_over_a_socket(port, {"X-Forwarded-For": "203.0.113.77"})
    assert rec["peer"] is None
    assert rec["claimed_forwarding"] == {"x-forwarded-for": "203.0.113.77"}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else ""


def test_the_servers_mast_starts_do_not_trust_proxy_headers():
    """Pins the two places that start the application under uvicorn: with its defaults a loopback
    client could choose the address the operator API records as the peer."""
    repo = pathlib.Path(__file__).resolve().parents[4]
    for rel in ("mast2_launcher.py", "MASTv2/mast/api/__main__.py"):
        tree = ast.parse((repo / rel).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and _dotted(n.func) in ("uvicorn.Config", "uvicorn.run")]
        assert calls, f"{rel} no longer starts uvicorn the way this test reads it - update the test"
        for call in calls:
            flag = {k.arg: k.value for k in call.keywords}.get("proxy_headers")
            assert isinstance(flag, ast.Constant) and flag.value is False, \
                f"{rel}:{call.lineno}: uvicorn must be started with proxy_headers=False"
