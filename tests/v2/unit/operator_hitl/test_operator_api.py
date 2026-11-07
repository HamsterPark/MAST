"""``/api/operator/*``: what an operator's page or the launcher sends, and what a forged request gets.

The write endpoints are the only way a person's word (this λ/2 is installed, the laser area is
safe, I confirmed the step) enters MAST, so they are tested from both sides: every legitimate
shape works and says what happened, and each way a *web page* could press them without the
operator meaning it is refused. The repo has been bitten by a write endpoint with an optional body
that a ``no-cors`` empty POST could press, hence the explicit cases.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.access import AccessGate
from mast.api.routes import operator as operator_routes
from mast.operator_hitl import optical_state, prompts, service

JSON = {"Content-Type": "application/json"}


def _app(*, lan: bool = False, dev_origins=(), gate: bool = True) -> FastAPI:
    app = FastAPI()
    if gate:
        app.add_middleware(AccessGate, credentials=("op", "pw") if lan else None,
                           dev_origins=dev_origins)
    app.include_router(operator_routes.router, prefix="/api")
    return app


@pytest.fixture
def client(memory_backends):
    return TestClient(_app(gate=False), base_url="http://127.0.0.1:7862")


@pytest.fixture
def pstore(installed):
    """Persistent backends under a temp root, behind the real access gate (loopback mode)."""
    return TestClient(_app(), base_url="http://127.0.0.1:7862")


def _post_prompt(**kw) -> str:
    kw.setdefault("kind", "confirm")
    kw.setdefault("title", "Continue?")
    return prompts.post(prompts.OperatorPrompt(**kw))


def _answer(client, pid, **body):
    body.setdefault("by", "alice")
    return client.post(f"/api/operator/prompts/{pid}/answer", json=body)


# ── reading ─────────────────────────────────────────────────────────────────


def test_the_prompt_list_has_seq_and_answers(client):
    pid = _post_prompt(title="Which plate?", kind="choice",
                       options=[prompts.PromptOption("a", "A"), prompts.PromptOption("b", "B")])
    r = client.get("/api/operator/prompts").json()
    assert r["open_count"] == 1 and r["persistent"] is False and r["persist_error"] is None
    (p,) = r["prompts"]
    assert p["id"] == pid and p["status"] == "open" and p["answer"] is None
    assert p["options"] == [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    seq = r["seq"]
    assert _answer(client, pid, choice="b", note="the 1064 one").status_code == 200
    after = client.get("/api/operator/prompts", params={"status": "all"}).json()
    assert after["seq"] > seq, "an answer changes seq so a poller refetches"
    assert after["prompts"][0]["answer"]["choice"] == "b"
    assert client.get("/api/operator/prompts").json()["prompts"] == [], "status=open hides answered ones"


def test_limit_and_status_are_validated(client):
    assert client.get("/api/operator/prompts", params={"status": "pending"}).status_code == 422
    assert client.get("/api/operator/prompts", params={"limit": 0}).status_code == 422
    assert client.get("/api/operator/prompts", params={"limit": 501}).status_code == 422


def test_the_optical_state_view(client):
    r = client.get("/api/operator/optical-state").json()
    ids = {s["id"] for s in r["slots"]}
    assert {"excitation_waveplate", "laser_area_safe", "objective"} <= ids
    wp = next(s for s in r["slots"] if s["id"] == "excitation_waveplate")
    assert wp["kind"] == "choice" and wp["options"][0]["meta"] == {"valid_nm": [500, 580]}
    assert r["values"]["laser_area_safe"] == {"value": None, "valid": False}
    assert r["seq"] == 0 and r["persistent"] is False


# ── answering ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kw, body, expect", [
    (dict(kind="confirm"), dict(choice="yes"), {"choice": "yes", "value": None}),
    (dict(kind="choice", options=[prompts.PromptOption("a", "A"), prompts.PromptOption("b", "B")]),
     dict(choice="a"), {"choice": "a", "value": "a"}),
    (dict(kind="number", number_min=0, number_max=360, unit="deg"), dict(value=45), {"value": 45.0}),
    (dict(kind="text"), dict(value="the filter is OD2"), {"value": "the filter is OD2"}),
])
def test_each_kind_can_be_answered(client, kw, body, expect):
    pid = _post_prompt(**kw)
    r = _answer(client, pid, source="launcher", **body)
    assert r.status_code == 200, r.text
    ans = r.json()
    assert ans["status"] == "answered" and ans["answered_by"] == "alice" and ans["source"] == "launcher"
    assert ans["prompt_id"] == pid and ans["answered_at"]
    for k, v in expect.items():
        assert ans[k] == v
    assert prompts.get(pid).status == "answered"


def test_an_attest_answer_sets_the_slots_in_the_same_step(client):
    pid = _post_prompt(kind="attest", title="Which λ/2 is in?", attest_slots=["excitation_waveplate"],
                       options=[prompts.PromptOption("hwp_1064", "1064"), prompts.PromptOption("hwp_532", "532")])
    assert _answer(client, pid, choice="hwp_1064").status_code == 200
    sv = optical_state.get_store().get("excitation_waveplate")
    assert sv.value == "hwp_1064" and sv.set_by == "alice" and sv.source == f"prompt:{pid}"
    seq_after = client.get("/api/operator/optical-state").json()["seq"]
    assert seq_after == 1


def test_a_bad_attest_value_is_refused_and_nothing_is_set(client):
    pid = _post_prompt(kind="attest", title="Angle?", attest_slots=["incident_polarizer_deg"],
                       unit="deg")
    r = _answer(client, pid, value=720)
    assert r.status_code == 422 and "360" in r.json()["detail"], "refused against the slot's own range"
    assert optical_state.get_store().get("incident_polarizer_deg") is None
    assert prompts.get(pid).status == "open", "the operator can correct the answer"
    assert _answer(client, pid, value=90).status_code == 200


def test_answer_error_codes(client):
    assert _answer(client, "op-9999-nope", choice="yes").status_code == 404
    pid = _post_prompt()
    assert _answer(client, pid, choice="maybe").status_code == 422
    n = _post_prompt(kind="number", title="n", number_min=1, number_max=2)
    r = _answer(client, n, value=3)
    assert r.status_code == 422 and "above" in r.json()["detail"]
    assert _answer(client, pid, choice="yes").status_code == 200
    r = _answer(client, pid, choice="yes")
    assert r.status_code == 409 and "already answered" in r.json()["detail"]


def test_a_timed_out_prompt_cannot_be_answered(client):
    pid = _post_prompt(timeout_s=0.01)
    import time
    time.sleep(0.05)
    r = _answer(client, pid, choice="yes")
    assert r.status_code == 409 and "timeout" in r.json()["detail"]


def test_dismiss(client):
    pid = _post_prompt()
    r = client.post(f"/api/operator/prompts/{pid}/dismiss", json={"by": "alice", "reason": "stale"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "dismissed" and body["answered_by"] == "alice" and body["note"] == "stale"
    assert client.post(f"/api/operator/prompts/{pid}/dismiss", json={"by": "alice"}).status_code == 409
    assert client.post("/api/operator/prompts/op-0-nope/dismiss", json={"by": "a"}).status_code == 404


def test_a_dismissed_attest_prompt_sets_nothing(client):
    pid = _post_prompt(kind="attest", title="x", attest_slots=["laser_area_safe"])
    client.post(f"/api/operator/prompts/{pid}/dismiss", json={"by": "alice"})
    assert optical_state.get_store().get("laser_area_safe") is None


# ── optical state writes ─────────────────────────────────────────────────────


def test_set_clear_and_history(client):
    r = client.put("/api/operator/optical-state/objective", json={"by": "alice", "value": "50x",
                                                                   "note": "turned in"})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "50x" and r.json()["set_by"] == "alice" and r.json()["source"] == "web"
    view = client.get("/api/operator/optical-state").json()
    assert view["values"]["objective"]["value"] == "50x" and view["values"]["objective"]["valid"] is True
    r = client.post("/api/operator/optical-state/objective/clear", json={"by": "bob"})
    assert r.status_code == 200 and r.json()["cleared"] is True and r.json()["slot"] == "objective"
    assert client.get("/api/operator/optical-state").json()["values"]["objective"]["value"] is None
    hist = client.get("/api/operator/optical-state/history").json()["records"]
    assert [(h["slot"], h["value"], h["set_by"]) for h in hist] == [
        ("objective", "50x", "alice"), ("objective", None, "bob")]
    assert client.get("/api/operator/optical-state/history", params={"limit": 1}).json()["records"][0][
        "set_by"] == "bob"
    assert client.get("/api/operator/optical-state/history", params={"limit": 0}).status_code == 422


def test_history_is_not_read_as_a_slot_id(client):
    assert client.get("/api/operator/optical-state/history").status_code == 200


@pytest.mark.parametrize("slot, value, status", [
    ("objective", "200x", 422), ("incident_polarizer_deg", 999, 422), ("incident_polarizer_deg", "x", 422),
    ("laser_area_safe", "maybe", 422), ("nope", 1, 404)])
def test_slot_write_errors(client, slot, value, status):
    r = client.put(f"/api/operator/optical-state/{slot}", json={"by": "alice", "value": value})
    assert r.status_code == status, r.text
    assert client.post(f"/api/operator/optical-state/{slot}/clear", json={"by": "a"}).status_code in (200, 404)
    assert optical_state.get_store().history == [] or slot != "nope"


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_number_never_gets_registered(client, token):
    """Python's JSON parser accepts NaN / Infinity; NaN compares false with every bound, so a
    range check alone would let it through into an attestation (and into the history file)."""
    body = ('{"by": "alice", "value": %s}' % token).encode()
    r = client.put("/api/operator/optical-state/incident_polarizer_deg", content=body, headers=JSON)
    assert r.status_code == 422 and "finite" in r.text, r.text
    pid = _post_prompt(kind="number", title="Angle?", number_min=0, number_max=360)
    r = client.post(f"/api/operator/prompts/{pid}/answer",
                    content=('{"by": "alice", "value": %s}' % token).encode(), headers=JSON)
    assert r.status_code == 422 and "finite" in r.text, r.text
    assert optical_state.get_store().get("incident_polarizer_deg") is None and prompts.get(pid).status == "open"


def test_a_flag_slot_takes_true_and_false(client):
    assert client.put("/api/operator/optical-state/laser_area_safe",
                      json={"by": "alice", "value": True}).status_code == 200
    assert optical_state.get_store().is_valid("laser_area_safe", expect=True)
    assert client.put("/api/operator/optical-state/laser_area_safe",
                      json={"by": "alice", "value": False}).status_code == 200
    assert not optical_state.get_store().is_valid("laser_area_safe", expect=True)


def test_a_write_that_cannot_be_saved_is_a_503_and_changes_nothing(pstore, monkeypatch):
    store = optical_state.get_store()
    monkeypatch.setattr(store, "_write_values", lambda: (_ for _ in ()).throw(PermissionError("read-only")))
    r = pstore.put("/api/operator/optical-state/laser_area_safe", json={"by": "alice", "value": True})
    assert r.status_code == 503
    assert any(w in r.json()["detail"] for w in ("没有生效", "Nothing was changed")), r.json()
    assert store.get("laser_area_safe") is None
    pid = _post_prompt(kind="attest", title="x", attest_slots=["laser_area_safe"])
    r = _answer(pstore, pid, value=True)
    assert r.status_code == 503
    assert prompts.get(pid).status == "open", "an attestation that was not recorded does not close its prompt"
    assert store.get("laser_area_safe") is None


def test_the_persistent_backends_serve_the_same_api(pstore):
    pid = _post_prompt(title="persisted")
    assert pstore.get("/api/operator/prompts").json()["persistent"] is True
    assert _answer(pstore, pid, choice="yes").status_code == 200
    assert pstore.put("/api/operator/optical-state/objective",
                      json={"by": "alice", "value": "10x"}).status_code == 200
    assert pstore.get("/api/operator/optical-state").json()["persistent"] is True


# ── the body is mandatory and strict ─────────────────────────────────────────

WRITES = [
    ("post", "/api/operator/prompts/{pid}/answer", {"by": "alice", "choice": "yes"}),
    ("post", "/api/operator/prompts/{pid}/dismiss", {"by": "alice"}),
    ("put", "/api/operator/optical-state/laser_area_safe", {"by": "alice", "value": True}),
    ("post", "/api/operator/optical-state/laser_area_safe/clear", {"by": "alice"}),
]


def _call(client, method, path, **kw):
    return getattr(client, method)(path, **kw)


@pytest.mark.parametrize("method, path, good", WRITES)
def test_an_empty_post_does_nothing_even_with_a_json_header(client, method, path, good):
    pid = _post_prompt()
    path = path.format(pid=pid)
    for kw in (dict(headers=JSON), dict(headers=JSON, content=b""), dict(headers=JSON, content=b"null"),
               dict(headers=JSON, content=b"{}"), dict(headers=JSON, content=b"[]")):
        r = _call(client, method, path, **kw)
        assert r.status_code == 422, (kw, r.text)
    assert prompts.get(pid).status == "open" and optical_state.get_store().history == []


@pytest.mark.parametrize("method, path, good", WRITES)
def test_a_body_without_a_json_content_type_is_refused_before_anything(client, method, path, good):
    """What a ``no-cors`` page can send: text/plain, a form, or no Content-Type at all."""
    pid = _post_prompt()
    path = path.format(pid=pid)
    raw = json.dumps(good).encode()
    for headers in ({"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"},
                    {"Content-Type": "multipart/form-data; boundary=x"}, {}):
        r = _call(client, method, path, content=raw, headers=headers)
        assert r.status_code == 415, (headers, r.status_code, r.text)
    assert prompts.get(pid).status == "open" and optical_state.get_store().history == []


@pytest.mark.parametrize("method, path, good", WRITES)
def test_unknown_fields_and_a_blank_name_are_refused(client, method, path, good):
    pid = _post_prompt()
    path = path.format(pid=pid)
    assert _call(client, method, path, json={**good, "approved": True}).status_code == 422
    assert _call(client, method, path, json={**good, "by": ""}).status_code == 422
    assert _call(client, method, path, json={**good, "by": "   "}).status_code == 422
    assert _call(client, method, path, json={k: v for k, v in good.items() if k != "by"}).status_code == 422
    assert _call(client, method, path, json={**good, "source": "ext"}).status_code == 422
    assert prompts.get(pid).status == "open" and optical_state.get_store().history == []


def test_a_slot_value_may_not_be_null(client):
    r = client.put("/api/operator/optical-state/objective", json={"by": "alice", "value": None})
    assert r.status_code == 422 and "clear endpoint" in json.dumps(r.json())


def test_get_endpoints_need_no_body_and_change_nothing(client):
    for path in ("/api/operator/prompts", "/api/operator/optical-state", "/api/operator/optical-state/history"):
        assert client.get(path).status_code == 200
    assert client.post("/api/operator/prompts").status_code in (404, 405)
    assert client.delete("/api/operator/optical-state/objective").status_code == 405


# ── origin / host checks (what a web page, rather than the operator's own UI, can do) ──

PAGE = "http://127.0.0.1:7862"


def _browser(client, *, origin=PAGE, site="same-origin", host=None, auth=None, path="/api/operator/optical-state/objective"):
    headers = {"Origin": origin, "Sec-Fetch-Site": site, "Sec-Fetch-Mode": "cors", **JSON}
    if host:
        headers["Host"] = host
    if auth:
        headers["Authorization"] = auth
    return client.put(path, json={"by": "alice", "value": "10x"}, headers=headers)


def test_the_operators_own_page_can_write(client):
    assert _browser(client).status_code == 200
    assert _browser(client, site="none").status_code == 200       # a typed-in / bookmarked navigation


@pytest.mark.parametrize("kw", [
    dict(origin="http://evil.example"),
    dict(origin="http://127.0.0.1:9999"),                         # another local page
    dict(origin="null"),
    dict(site="cross-site"),
    dict(site="same-site"),
    dict(host="evil.example:7862", origin="http://evil.example:7862"),   # DNS rebinding
])
def test_a_page_from_elsewhere_cannot_write(client, kw):
    r = _browser(client, **kw)
    assert r.status_code == 403, (kw, r.text)
    assert optical_state.get_store().history == []


def test_the_origin_check_does_not_depend_on_the_global_gate(client):
    """The router judges for itself (the gate is the first layer, not the only one)."""
    assert _browser(client, origin="http://evil.example").status_code == 403


def test_behind_the_real_gate_a_foreign_origin_is_refused_too(memory_backends):
    c = TestClient(_app(), base_url=PAGE)
    assert _browser(c, origin="http://evil.example").status_code == 403
    assert _browser(c).status_code == 200


def test_a_host_name_is_accepted_only_with_lan_credentials(memory_backends):
    """A foreign host name pointing at this machine is DNS rebinding in loopback mode; in LAN
    mode the page needs the operator's password, which a rebinding page does not have."""
    import base64

    host = "rig.tail1234.ts.net:7862"
    page = f"https://{host}"
    loop = TestClient(_app(), base_url=PAGE)
    assert _browser(loop, origin=page, host=host).status_code == 403

    basic = "Basic " + base64.b64encode(b"op:pw").decode()
    lan = TestClient(_app(lan=True), base_url=page)
    assert _browser(lan, origin=page, host=host, auth=basic).status_code == 200
    # a fresh client (the gate hands the first one a session cookie): no credentials, the gate answers
    no_password = TestClient(_app(lan=True), base_url=page)
    assert _browser(no_password, origin=page, host=host).status_code == 401
    assert _browser(TestClient(_app(lan=True), base_url=page), origin="https://evil.example",
                    host=host, auth=basic).status_code == 403


def test_dev_origins_of_the_gate_are_honoured(memory_backends):
    """npm run dev: the Vite proxy rewrites Host to the backend, the page's Origin is localhost:5173."""
    dev = ("http://localhost:*",)
    c = TestClient(_app(dev_origins=dev), base_url=PAGE)
    assert _browser(c, origin="http://localhost:5173").status_code == 200
    prod = TestClient(_app(), base_url=PAGE)
    assert _browser(prod, origin="http://localhost:5173").status_code == 403


def test_programmatic_clients_need_no_browser_headers(client):
    """The launcher (urllib) and scripts send JSON and nothing browser-like."""
    r = client.put("/api/operator/optical-state/objective", json={"by": "launcher", "value": "10x",
                                                                   "source": "launcher"})
    assert r.status_code == 200 and r.json()["source"] == "launcher"


# ── mounted in the real app ──────────────────────────────────────────────────


def test_the_routes_are_part_of_the_main_app_and_not_of_the_ext_surface():
    from mast.api.app import create_app

    app = create_app(dev_cors=False)
    paths = {(m, r.path) for r in app.routes for m in getattr(r, "methods", ()) if "operator" in r.path}
    assert ("POST", "/api/operator/prompts/{prompt_id}/answer") in paths
    assert ("PUT", "/api/operator/optical-state/{slot_id}") in paths
    assert ("GET", "/api/operator/prompts") in paths
    spec = app.openapi()
    assert "/api/operator/prompts" in spec["paths"]
    assert not [p for p in spec["paths"] if p.startswith("/api/ext")], "the ext surface stays off the main spec"


def test_a_write_through_the_real_app_is_gated_like_every_other_write(memory_backends):
    from mast.api.app import create_app

    c = TestClient(create_app(dev_cors=False), base_url=PAGE)
    assert c.put("/api/operator/optical-state/objective", json={"by": "a", "value": "10x"}).status_code == 200
    assert c.put("/api/operator/optical-state/objective", content=b'{"by":"a","value":"10x"}',
                 headers={"Content-Type": "text/plain"}).status_code == 415
    forged = c.put("/api/operator/optical-state/objective", json={"by": "a", "value": "10x"},
                   headers={"Origin": "http://evil.example"})
    assert forged.status_code == 403
    assert service.optical_history(10)[-1]["value"] == "10x" and len(service.optical_history(10)) == 1


def test_a_urllib_client_like_the_launcher_works_against_a_real_server(memory_backends):
    """The launcher polls with urllib (no browser headers) and answers with a JSON POST: prove
    it over a real socket, behind the real app, not only through TestClient."""
    import threading
    import urllib.error
    import urllib.request

    import uvicorn

    from mast.api.app import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(dev_cors=False), host="127.0.0.1", port=0,
                                           log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/api/operator"

    def call(method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={**({"Content-Type": "application/json"} if data else {}),
                                              **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    try:
        pid = _post_prompt(title="Is the beam blocked?", severity="safety")
        status, view = call("GET", "/prompts")
        assert status == 200 and view["prompts"][0]["id"] == pid and view["prompts"][0]["severity"] == "safety"
        seq = view["seq"]
        status, ans = call("POST", f"/prompts/{pid}/answer",
                           {"by": "alice", "choice": "yes", "source": "launcher"})
        assert status == 200 and ans["answered_by"] == "alice" and ans["source"] == "launcher"
        assert call("GET", "/prompts")[1]["seq"] > seq
        status, st = call("PUT", "/optical-state/objective", {"by": "alice", "value": "50x", "source": "launcher"})
        assert status == 200 and st["value"] == "50x"
        # a body-less / non-JSON write over the real socket: refused, nothing happens
        req = urllib.request.Request(base + "/optical-state/objective/clear", data=b"", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 415
        assert call("GET", "/optical-state")[1]["values"]["objective"]["value"] == "50x"
        status, bad = call("POST", "/optical-state/objective/clear", None,
                           {"Content-Type": "application/json"})
        assert status == 422
    finally:
        server.should_exit = True
        t.join(15)
