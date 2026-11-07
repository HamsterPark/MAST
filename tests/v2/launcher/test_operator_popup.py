"""The launcher's operator-prompt popup (docs/v2/design/optical_station_witec.md, D8).

An agent posts a structured prompt and waits; only a person at the instrument may
answer it. The launcher runs in the operator's desktop session, polls the local
service and puts a topmost window with a sound in front of them.

Layers, bottom first -- everything but the last needs no display:

* pure: prompt normalisation, what to draw per kind, the answer body, local validation
* ``OperatorPromptQueue``: dedupe, order, snooze, "answered elsewhere"
* ``OperatorApi``: against a REAL local HTTP server (plain and TLS + Basic), checking
  the wire format -- scheme, auth, JSON body, and that a write carries nothing a browser would
* ``OperatorPoller``: quiet back-off, one log line per kind of failure
* ``OperatorPopupController``: the whole flow with a fake view / API / clock / thread
* ``OperatorPromptView``: the Tk window, only where a Tk root can be created

Nothing here touches a real service, instrument or the launcher window.
"""
from __future__ import annotations

import ast
import http.server
import importlib.util
import json
import os
import re
import socket
import ssl
import sys
import threading
from pathlib import Path

# ── v2 import bootstrap (canonical block; keeps mast.* resolving to MASTv2/) ──
_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

import pytest  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
_LAUNCHER_PATH = _REPO / "mast2_launcher.py"
USER, PWD = "operator", "pw-for-tests"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("mast2_launcher_operator_popup", _LAUNCHER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    prev = os.environ.get("MAST_LANG")
    spec.loader.exec_module(mod)
    mod._set_lang("en")
    # _set_lang also writes MAST_LANG for the whole process (mast.i18n reads it). Give it back:
    # the per-test fixture below switches it and monkeypatch restores it after each test.
    if prev is None:
        os.environ.pop("MAST_LANG", None)
    else:
        os.environ["MAST_LANG"] = prev
    return mod


@pytest.fixture(autouse=True)
def _english(launcher, monkeypatch):
    """Every popup test runs in English, and the process language is given back afterwards.

    Left at "en", tests that pin Chinese wording and run later in the same pytest process
    (tests/conftest.py runs the suite in zh: station profile / tool face / ext surface) failed
    depending only on run order."""
    monkeypatch.setenv("MAST_LANG", os.environ.get("MAST_LANG", "zh"))
    saved = launcher._LANG
    launcher._set_lang("en")
    yield
    launcher._LANG = saved


def P(launcher, **kw):
    """A normalised prompt, the way the poller hands it over."""
    raw = {"id": "p1", "kind": "confirm", "title": "Change the waveplate",
           "message": "Put the 532 nm half-wave plate in the excitation path.",
           "severity": "action", "created_at": "2026-11-03T10:00:00+00:00", "status": "open"}
    raw.update(kw)
    p = launcher.normalize_operator_prompt(raw)
    assert p is not None
    return p


# ═══════════════════════════ normalisation and fields ═══════════════════════════

def test_normalize_needs_an_id_and_defaults_the_rest(launcher):
    assert launcher.normalize_operator_prompt("nope") is None
    assert launcher.normalize_operator_prompt({"kind": "confirm"}) is None
    assert launcher.normalize_operator_prompt({"id": "   "}) is None
    p = launcher.normalize_operator_prompt({"id": "x", "kind": "confirm", "severity": "weird"})
    assert p["severity"] == "action"          # an unknown severity must not hide a prompt
    assert p["title"] == "x" and p["options"] == [] and p["status"] == "open"


def test_normalize_options_numbers_and_slots(launcher):
    p = launcher.normalize_operator_prompt({
        "id": "x", "kind": "attest", "title": "t",
        "options": [{"id": "hwp_532", "label": "λ/2 @532"}, {"id": "none"}, {"label": "no id"}, "junk"],
        "number_min": "0", "number_max": float("inf"), "unit": "deg",
        "attest_slots": ["a", "", "b"], "timeout_s": 300})
    assert p["options"] == [{"id": "hwp_532", "label": "λ/2 @532"}, {"id": "none", "label": "none"}]
    assert p["number_min"] == 0.0 and p["number_max"] is None     # inf is not a bound
    assert p["attest_slots"] == ["a", "b"] and p["timeout_s"] == 300.0


@pytest.mark.parametrize("kind,expect_mode", [
    ("confirm", "confirm"), ("choice", "options"), ("number", "number"),
    ("text", "text"), ("attest", "flag"), ("telepathy", "unsupported")])
def test_each_kind_is_drawn_as_its_widget(launcher, kind, expect_mode):
    opts = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}] if kind == "choice" else []
    p = P(launcher, kind=kind, options=opts, attest_slots=["laser_area_safe"] if kind == "attest" else [])
    assert launcher.operator_prompt_fields(p)["mode"] == expect_mode


def test_attest_with_options_or_a_unit_is_not_a_plain_flag(launcher):
    p = P(launcher, kind="attest", attest_slots=["objective"],
          options=[{"id": "10x", "label": "10x"}, {"id": "100x", "label": "100x"}])
    f = launcher.operator_prompt_fields(p)
    assert f["mode"] == "options" and f["send"] == "choice"
    p = P(launcher, kind="attest", attest_slots=["incident_polarizer_deg"], unit="deg",
          number_min=0, number_max=360)
    f = launcher.operator_prompt_fields(p)
    assert (f["mode"], f["min"], f["max"], f["unit"]) == ("number", 0.0, 360.0, "deg")


def test_plain_attest_follows_the_slot_kind_when_it_is_known(launcher):
    def attest(slot):
        p = P(launcher, kind="attest", attest_slots=["s"])
        p["slots"] = {"s": slot}
        return launcher.operator_prompt_fields(p)

    assert attest({"kind": "flag"})["mode"] == "flag"
    assert attest({"kind": "text"})["mode"] == "text"
    n = attest({"kind": "number", "min": 0, "max": 100, "unit": "%"})
    assert (n["mode"], n["min"], n["max"], n["unit"]) == ("number", 0.0, 100.0, "%")
    c = attest({"kind": "choice", "options": [{"id": "hwp_532", "label": "532"}, {"id": "none"}]})
    assert c["mode"] == "options" and c["send"] == "value"      # a bare value, not a choice
    assert c["options"] == [("hwp_532", "532"), ("none", "none")]
    # nothing known about the slot: the common reading, a signed flag
    assert launcher.operator_prompt_fields(P(launcher, kind="attest", attest_slots=["s"]))["mode"] == "flag"


# ═══════════════════════════ the answer body, per kind ═══════════════════════════

def test_confirm_sends_the_clicked_choice_and_nothing_else(launcher):
    p = P(launcher, kind="confirm")
    assert launcher.build_operator_answer(p, {"choice": "yes"}, by="  Ana   Ruiz ") == {
        "by": "Ana Ruiz", "source": "launcher", "choice": "yes"}
    assert launcher.build_operator_answer(p, {"choice": "no", "note": " too dark "}, by="A") == {
        "by": "A", "source": "launcher", "choice": "no", "note": "too dark"}
    with pytest.raises(launcher.OperatorFormError, match="Yes or No"):
        launcher.build_operator_answer(p, {"choice": None}, by="A")
    with pytest.raises(launcher.OperatorFormError, match="Yes or No"):
        launcher.build_operator_answer(p, {"choice": "maybe"}, by="A")


def test_the_operators_name_is_required_everywhere(launcher):
    p = P(launcher)
    for by in ("", "   ", None):
        with pytest.raises(launcher.OperatorFormError, match="name"):
            launcher.build_operator_answer(p, {"choice": "yes"}, by=by)
        with pytest.raises(launcher.OperatorFormError, match="name"):
            launcher.build_operator_dismiss(by)


def test_choice_sends_the_option_id_and_only_a_listed_one(launcher):
    p = P(launcher, kind="choice", options=[{"id": "hwp_532", "label": "532"}, {"id": "hwp_1064", "label": "1064"}])
    assert launcher.build_operator_answer(p, {"choice": "hwp_1064"}, by="A")["choice"] == "hwp_1064"
    with pytest.raises(launcher.OperatorFormError, match="option first"):
        launcher.build_operator_answer(p, {"choice": ""}, by="A")
    with pytest.raises(launcher.OperatorFormError, match="not one of"):
        launcher.build_operator_answer(p, {"choice": "hwp_355"}, by="A")


def test_number_is_parsed_and_checked_against_the_range_before_any_request(launcher):
    p = P(launcher, kind="number", number_min=0, number_max=20, unit="mW")
    body = launcher.build_operator_answer(p, {"text": " 12.5 "}, by="A")
    assert body["value"] == 12.5 and isinstance(body["value"], float) and "choice" not in body
    assert launcher.build_operator_answer(p, {"text": "12,5"}, by="A")["value"] == 12.5     # decimal comma
    assert launcher.build_operator_answer(p, {"text": "2e1"}, by="A")["value"] == 20.0      # the bound itself is fine
    assert launcher.build_operator_answer(p, {"text": "0"}, by="A")["value"] == 0.0
    with pytest.raises(launcher.OperatorFormError, match=r"above the maximum 20 mW"):
        launcher.build_operator_answer(p, {"text": "20.1"}, by="A")
    with pytest.raises(launcher.OperatorFormError, match=r"below the minimum 0 mW"):
        launcher.build_operator_answer(p, {"text": "-0.1"}, by="A")


@pytest.mark.parametrize("text", ["", "   ", "abc", "nan", "inf", "-inf", "1_0", "0x10", "1.2.3", "12 mW"])
def test_number_input_that_is_not_a_finite_number_is_refused(launcher, text):
    p = P(launcher, kind="number")
    with pytest.raises(launcher.OperatorFormError):
        launcher.build_operator_answer(p, {"text": text}, by="A")


def test_number_without_bounds_accepts_anything_finite(launcher):
    p = P(launcher, kind="number")
    assert launcher.build_operator_answer(p, {"text": "-1e9"}, by="A")["value"] == -1e9


def test_range_hint_names_the_bounds_and_the_unit(launcher):
    assert launcher.operator_range_hint(0, 20, "mW") == "Allowed range: 0 – 20 mW"
    assert launcher.operator_range_hint(0.5, None, "") == "Minimum: 0.5"
    assert launcher.operator_range_hint(None, 360, "deg") == "Maximum: 360 deg"
    assert launcher.operator_range_hint(None, None, "deg") == "Unit: deg"
    assert launcher.operator_range_hint(None, None, "") == ""


def test_text_answer_is_stripped_required_and_refused_when_too_long(launcher):
    p = P(launcher, kind="text")
    assert launcher.build_operator_answer(p, {"text": "  Nikon 40x  "}, by="A")["value"] == "Nikon 40x"
    with pytest.raises(launcher.OperatorFormError, match="answer first"):
        launcher.build_operator_answer(p, {"text": "  \n "}, by="A")
    full = "x" * launcher.OPERATOR_TEXT_MAX
    assert launcher.build_operator_answer(p, {"text": full}, by="A")["value"] == full
    # too long is refused with the limit, never cut short behind the operator's back
    with pytest.raises(launcher.OperatorFormError, match=rf"at most {launcher.OPERATOR_TEXT_MAX} are allowed"):
        launcher.build_operator_answer(p, {"text": full + "x"}, by="A")


def test_attesting_a_text_slot_uses_the_slots_limit(launcher):
    p = P(launcher, kind="attest", attest_slots=["s"])
    p["slots"] = {"s": {"kind": "text"}}
    assert launcher.OPERATOR_SLOT_TEXT_MAX < launcher.OPERATOR_TEXT_MAX
    fits = "y" * launcher.OPERATOR_SLOT_TEXT_MAX
    assert launcher.build_operator_answer(p, {"text": fits}, by="A")["value"] == fits
    with pytest.raises(launcher.OperatorFormError, match=rf"at most {launcher.OPERATOR_SLOT_TEXT_MAX} are allowed"):
        launcher.build_operator_answer(p, {"text": fits + "y"}, by="A")


def test_attest_variants_send_what_the_service_checks(launcher):
    # options -> choice
    p = P(launcher, kind="attest", attest_slots=["objective"],
          options=[{"id": "10x", "label": "10x"}, {"id": "100x", "label": "100x"}])
    assert launcher.build_operator_answer(p, {"choice": "100x"}, by="A")["choice"] == "100x"
    # unit -> a number inside the prompt's range
    p = P(launcher, kind="attest", attest_slots=["incident_polarizer_deg"], unit="deg", number_min=0, number_max=360)
    assert launcher.build_operator_answer(p, {"text": "45"}, by="A")["value"] == 45.0
    with pytest.raises(launcher.OperatorFormError, match="above the maximum"):
        launcher.build_operator_answer(p, {"text": "361"}, by="A")
    # neither -> a signed flag: the box must be ticked, then the value is True
    p = P(launcher, kind="attest", attest_slots=["laser_area_safe"])
    with pytest.raises(launcher.OperatorFormError, match="Tick the box"):
        launcher.build_operator_answer(p, {"confirmed": False}, by="A")
    body = launcher.build_operator_answer(p, {"confirmed": True}, by="A")
    assert body["value"] is True and "choice" not in body


def test_attest_on_a_choice_slot_sends_the_id_as_a_value(launcher):
    p = P(launcher, kind="attest", attest_slots=["excitation_waveplate"])
    p["slots"] = {"excitation_waveplate": {"kind": "choice", "options": [{"id": "hwp_532"}, {"id": "none"}]}}
    body = launcher.build_operator_answer(p, {"choice": "hwp_532"}, by="A")
    assert body["value"] == "hwp_532" and "choice" not in body


def test_an_unknown_prompt_kind_is_never_answered_blind(launcher):
    p = P(launcher, kind="telepathy")
    with pytest.raises(launcher.OperatorFormError, match="web page"):
        launcher.build_operator_answer(p, {"choice": "yes"}, by="A")


def test_dismiss_body_carries_the_name_the_source_and_an_optional_reason(launcher):
    # the service records "web" when source is left out, so the launcher says it
    assert launcher.build_operator_dismiss("A") == {"by": "A", "source": "launcher"}
    assert launcher.build_operator_dismiss("A", " no goggles here ") == {
        "by": "A", "source": "launcher", "reason": "no goggles here"}


# ═══════════════════════════ small pure helpers ═══════════════════════════

def test_severity_styles_make_safety_unmistakable(launcher):
    safety = launcher.operator_severity_style("safety")
    action = launcher.operator_severity_style("action")
    info = launcher.operator_severity_style("info")
    assert safety["bg"] != action["bg"] != info["bg"]
    assert "SAFETY" in safety["label"] and "ACTION" in action["label"]
    assert launcher.operator_severity_style("whatever") == action          # unknown -> the action look
    assert len(launcher.operator_alert_beeps("safety")) > len(launcher.operator_alert_beeps("action")) \
        > len(launcher.operator_alert_beeps("info"))


def test_source_line_names_whichever_of_agent_job_skill_exist(launcher):
    p = P(launcher, agent_id="claude-code", job_id="j-12", skill="SetAnalyzerAngle")
    assert launcher.format_operator_source(p) == "Agent: claude-code · Job: j-12 · Skill: SetAnalyzerAngle"
    assert launcher.format_operator_source(P(launcher, skill="AskOperator")) == "Skill: AskOperator"
    assert launcher.format_operator_source(P(launcher)) == ""


def test_time_is_shown_in_local_time_and_garbage_is_left_alone(launcher):
    import datetime as dt
    now = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc).astimezone()
    same_day = now.replace(hour=11, minute=5, second=7).isoformat()
    out = launcher.format_operator_time(same_day, now=now)
    assert re.fullmatch(r"\d\d:\d\d:\d\d", out)
    other_day = (now - dt.timedelta(days=3)).isoformat()
    assert re.fullmatch(r"\d\d-\d\d \d\d:\d\d:\d\d", launcher.format_operator_time(other_day, now=now))
    assert launcher.format_operator_time("yesterday-ish") == "yesterday-ish"
    assert launcher.format_operator_time("") == ""


def test_status_text_covers_every_state(launcher):
    f = launcher.operator_status_text
    assert "no popup" in f(enabled=False, link="ok", open_count=3)
    assert "3 waiting" in f(enabled=True, link="ok", open_count=3)
    assert "nothing is waiting" in f(enabled=True, link="ok", open_count=0)
    assert "no operator prompts yet" in f(enabled=True, link="unsupported", open_count=0)
    assert "LAN credentials" in f(enabled=True, link="auth", open_count=0)
    for link in ("network", "server"):
        assert "Waiting for the main service" in f(enabled=True, link=link, open_count=0)


def test_prefs_roundtrip_survive_garbage_and_a_bom(launcher, tmp_path):
    assert launcher._load_operator_prefs(tmp_path) == {"enabled": True, "sound": True, "operator_name": ""}
    launcher._save_operator_prefs(tmp_path, {"enabled": False, "sound": True, "operator_name": "  Ana  Ruiz "})
    assert launcher._load_operator_prefs(tmp_path) == {"enabled": False, "sound": True, "operator_name": "Ana Ruiz"}
    path = launcher._operator_prefs_path(tmp_path)
    path.write_text("{ not json", encoding="utf-8")
    assert launcher._load_operator_prefs(tmp_path)["enabled"] is True
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"sound": False, "operator_name": 7, "enabled": "yes"}).encode())
    assert launcher._load_operator_prefs(tmp_path) == {"enabled": True, "sound": False, "operator_name": ""}


# ═══════════════════════════ the queue ═══════════════════════════

def test_queue_dedupes_and_reports_new_and_gone(launcher):
    q = launcher.OperatorPromptQueue()
    a, b = P(launcher, id="a"), P(launcher, id="b")
    assert q.update([a], 0) == {"new": ["a"], "gone": []}
    assert q.update([a], 1) == {"new": [], "gone": []}                   # the same prompt again is not news
    assert q.update([a, b], 2) == {"new": ["b"], "gone": []}
    assert q.update([b], 3) == {"new": [], "gone": ["a"]}                 # answered elsewhere
    assert q.ids() == ["b"] and not q.has("a")


def test_queue_only_keeps_open_prompts(launcher):
    q = launcher.OperatorPromptQueue()
    q.update([P(launcher, id="a", status="answered")], 0)
    assert len(q) == 0


def test_safety_first_then_oldest_first(launcher):
    q = launcher.OperatorPromptQueue()
    q.update([P(launcher, id="old-info", severity="info", created_at="2026-11-03T09:00:00+00:00"),
              P(launcher, id="new-action", severity="action", created_at="2026-11-03T10:05:00+00:00"),
              P(launcher, id="old-action", severity="action", created_at="2026-11-03T10:00:00+00:00"),
              P(launcher, id="late-safety", severity="safety", created_at="2026-11-03T10:30:00+00:00")], 0)
    order = []
    while len(q):
        h = q.head(0)
        order.append(h["id"])
        q.resolve(h["id"], 0)
    assert order == ["late-safety", "old-action", "new-action", "old-info"]


def test_waiting_counts_everything_but_the_prompt_on_screen(launcher):
    q = launcher.OperatorPromptQueue()
    q.update([P(launcher, id="a"), P(launcher, id="b"), P(launcher, id="c")], 0)
    assert q.waiting(exclude="a") == 2 and q.waiting() == 3
    q.snooze("b", 0)
    assert q.waiting(exclude="a") == 2          # a snoozed prompt is still waiting; it will be back


def test_snooze_hides_a_prompt_until_its_time_is_up(launcher):
    q = launcher.OperatorPromptQueue(snooze_s=60)
    q.update([P(launcher, id="a")], 0)
    q.snooze("a", 10)
    assert q.head(10) is None and q.head(69.9) is None and q.is_snoozed("a", 69.9)
    assert q.head(70)["id"] == "a" and not q.is_snoozed("a", 70)
    assert q.update([P(launcher, id="a")], 71) == {"new": [], "gone": []}   # not news after a snooze either


def test_a_prompt_answered_here_is_not_resurrected_by_a_poll_already_in_flight(launcher):
    q = launcher.OperatorPromptQueue(closed_ttl_s=300)
    a = P(launcher, id="a")
    q.update([a], 0)
    q.resolve("a", 5)
    assert q.update([a], 6) == {"new": [], "gone": []} and len(q) == 0     # the stale poll lists it again
    assert q.update([a], 400)["new"] == ["a"]                              # but not for ever


def test_local_prompts_survive_polls_that_do_not_list_them(launcher):
    q = launcher.OperatorPromptQueue()
    demo = P(launcher, id="demo-1")
    q.add_local(demo)
    assert q.update([], 1) == {"new": [], "gone": []} and q.has("demo-1")


def test_mark_shown_counts_earlier_presentations(launcher):
    q = launcher.OperatorPromptQueue()
    q.update([P(launcher, id="a")], 0)
    assert q.mark_shown("a") == 0 and q.mark_shown("a") == 1 and q.mark_shown("zzz") == 0


# ═══════════════════════════ the HTTP client, on the wire ═══════════════════════════

class _Handler(http.server.BaseHTTPRequestHandler):
    def _go(self):
        srv = self.server
        n = int(self.headers.get("Content-Length") or 0)
        srv.seen.append({"method": self.command, "path": self.path,
                         "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": self.rfile.read(n) if n else b""})
        if srv.require and self.headers.get("Authorization") != srv.require:
            payload, status = b"Unauthorized", 401
            ctype = "text/plain"
        else:
            status, obj = srv.route(self.command, self.path)
            location = obj.pop("_location", None) if isinstance(obj, dict) else None
            payload, ctype = json.dumps(obj).encode(), "application/json"
        self.send_response(status)
        if status in (301, 302, 307, 308) and location:
            self.send_header("Location", location)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _go

    def log_message(self, *_a):
        pass


@pytest.fixture(scope="module")
def lan_cert(tmp_path_factory):
    """One self-signed LAN certificate for the whole module (generating one takes ~0.6 s)."""
    from mast.net.tls import ensure_lan_cert

    d = tmp_path_factory.mktemp("lan-cert")
    cert, key = d / "lan_cert.pem", d / "lan_key.pem"
    ensure_lan_cert(cert, key, addresses=[], hostname="box")
    return cert, key


def _serve(tmp_path, *, tls=False, require=None, route=None, cert=None):
    import base64
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.seen = []
    srv.require = ("Basic " + base64.b64encode(f"{USER}:{PWD}".encode()).decode()) if require else None
    srv.route = route or (lambda m, p: (200, {"prompts": []}))
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*cert)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    # a short poll interval: shutdown() waits for one tick, and the default 0.5 s dominated the module's run time
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv


@pytest.fixture
def plain_service(tmp_path):
    srv = _serve(tmp_path)
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def lan_service(tmp_path, lan_cert):
    srv = _serve(tmp_path, tls=True, require=True, cert=lan_cert)
    yield srv
    srv.shutdown()
    srv.server_close()


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PROMPTS_REPLY = {"prompts": [
    {"id": "b", "kind": "confirm", "title": "second", "severity": "info", "created_at": "2026-11-03T10:01:00+00:00",
     "status": "open", "answer": None},
    {"id": "a", "kind": "number", "title": "first", "severity": "safety", "number_min": 0, "number_max": 20,
     "unit": "mW", "created_at": "2026-11-03T10:00:00+00:00", "status": "open", "answer": None},
    {"id": "gone", "kind": "confirm", "title": "closed", "status": "answered"},
    {"kind": "confirm", "title": "no id"}], "seq": 7}


def test_list_open_asks_for_open_prompts_and_normalises_them(launcher, plain_service):
    plain_service.route = lambda m, p: (200, PROMPTS_REPLY)
    api = launcher.OperatorApi(plain_service.server_address[1])
    got = api.list_open()
    assert [p["id"] for p in got] == ["b", "a"]               # closed and id-less rows dropped, order kept
    assert got[1]["number_max"] == 20.0 and got[1]["unit"] == "mW"
    seen = plain_service.seen[-1]
    assert seen["method"] == "GET" and seen["path"] == "/api/operator/prompts?status=open&limit=50"
    assert "authorization" not in seen["headers"] and seen["headers"]["accept"] == "application/json"


def test_lan_mode_goes_over_tls_with_basic_auth(launcher, lan_service):
    lan_service.route = lambda m, p: (200, PROMPTS_REPLY)
    api = launcher.OperatorApi(lan_service.server_address[1], auth=(USER, PWD))
    assert len(api.list_open()) == 2
    assert lan_service.seen[-1]["headers"]["authorization"] == lan_service.require


def test_credentials_are_read_per_request_so_a_lan_change_is_picked_up(launcher, lan_service):
    creds = [("", "")]
    api = launcher.OperatorApi(lan_service.server_address[1], auth=lambda: creds[0])
    with pytest.raises(launcher.OperatorApiError) as ei:
        api.list_open()
    assert ei.value.kind == "auth" and ei.value.status == 401
    creds[0] = (USER, PWD)
    assert api.list_open() == []


def test_a_write_carries_json_and_basic_auth_but_nothing_a_browser_would_send(launcher, lan_service):
    lan_service.route = lambda m, p: ((200, {"prompts": []}) if m == "GET" else
                                      (200, {"prompt_id": "a", "status": "answered", "choice": "yes"}))
    port = lan_service.server_address[1]
    api = launcher.OperatorApi(port, auth=(USER, PWD))
    api.list_open()                                           # the poll teaches it the scheme
    out = api.answer("a", {"by": "Ana", "choice": "yes", "source": "launcher"})
    assert out["status"] == "answered"
    w = lan_service.seen[-1]
    assert (w["method"], w["path"]) == ("POST", "/api/operator/prompts/a/answer")
    assert json.loads(w["body"]) == {"by": "Ana", "choice": "yes", "source": "launcher"}
    assert w["headers"]["content-type"] == "application/json"
    assert w["headers"]["authorization"] == lan_service.require
    # api/routes/operator.py::write_refusal takes a request without browser headers for a
    # programmatic client and asks only for the JSON body; an Origin would send it through
    # the browser checks (same origin, loopback / IP Host) for no reason
    assert "origin" not in w["headers"] and not any(h.startswith("sec-fetch-") for h in w["headers"])
    # the service records the client of every answer; this marks the launcher's (an audit
    # marker, not a credential)
    assert w["headers"]["user-agent"].startswith("MAST-launcher/")
    assert w["headers"]["user-agent"] == launcher._operator_user_agent()
    api.dismiss("a", {"by": "Ana", "source": "launcher", "reason": "no goggles"})
    d = lan_service.seen[-1]
    assert d["path"] == "/api/operator/prompts/a/dismiss"
    assert json.loads(d["body"]) == {"by": "Ana", "source": "launcher", "reason": "no goggles"}


def test_plain_mode_sends_no_credentials_and_ids_are_url_quoted(launcher, plain_service):
    api = launcher.OperatorApi(plain_service.server_address[1])
    api.answer("op 1/x", {"by": "A", "choice": "yes", "source": "launcher"})
    w = plain_service.seen[-1]
    assert w["path"] == "/api/operator/prompts/op%201%2Fx/answer"
    assert "authorization" not in w["headers"] and "origin" not in w["headers"]
    assert w["headers"]["content-type"] == "application/json"


def test_reads_try_the_other_scheme_and_remember_the_one_that_worked(launcher, plain_service):
    # credentials make https the first guess, but this service is plain http
    api = launcher.OperatorApi(plain_service.server_address[1], auth=(USER, PWD))
    assert api.list_open() == []
    assert api._scheme == "http"
    n = len(plain_service.seen)
    api.list_open()
    assert len(plain_service.seen) == n + 1                   # no second attempt: it remembered


def test_a_write_is_never_replayed_on_the_other_scheme(launcher, plain_service):
    api = launcher.OperatorApi(plain_service.server_address[1], auth=(USER, PWD))   # first guess: https
    with pytest.raises(launcher.OperatorApiError) as ei:
        api.answer("a", {"by": "A", "choice": "yes", "source": "launcher"})
    assert ei.value.kind == "network"
    assert plain_service.seen == []                           # nothing reached the plain-http listener


@pytest.mark.parametrize("status,reply,kind,text", [
    (400, {"detail": "7 is above the maximum 5"}, "invalid", "above the maximum"),
    (422, {"detail": [{"loc": ["body", "by"], "msg": "field required"}]}, "invalid", "field required"),
    (409, {"detail": "prompt 'a' is already answered"}, "gone", "already answered"),
    (404, {"detail": "no prompt 'a'"}, "gone", "no prompt"),
    (401, {"detail": "Unauthorized"}, "auth", "Unauthorized"),
    (403, {"detail": "Cross-site write refused (Origin: http://evil)"}, "forbidden", "Cross-site"),
    (415, {"detail": "Write requests must carry Content-Type: application/json"}, "server", "Content-Type"),
    (503, {"detail": "Could not save this. Nothing was changed."}, "server", "Could not save"),
    (500, {"error": "boom"}, "server", "boom")])
def test_a_refused_write_says_why_and_what_kind_of_refusal_it_was(launcher, plain_service, status, reply, kind, text):
    plain_service.route = lambda m, p: (status, reply)
    api = launcher.OperatorApi(plain_service.server_address[1])
    with pytest.raises(launcher.OperatorApiError) as ei:
        api.answer("a", {"by": "A", "choice": "yes", "source": "launcher"})
    assert ei.value.kind == kind and ei.value.status == status and text in str(ei.value)


def test_a_service_without_the_operator_api_is_unsupported_not_down(launcher, plain_service):
    plain_service.route = lambda m, p: (404, {"detail": "Not Found"})
    with pytest.raises(launcher.OperatorApiError) as ei:
        launcher.OperatorApi(plain_service.server_address[1]).list_open()
    assert ei.value.kind == "unsupported"


def test_nothing_listening_is_a_network_error_found_quickly(launcher):
    """Connecting to a closed port takes ~2 s on Windows and both schemes would be tried (4 s
    per poll); a quick TCP probe first makes "the service is not up yet" cost well under that."""
    import time

    api = launcher.OperatorApi(_closed_port())
    t0 = time.monotonic()
    with pytest.raises(launcher.OperatorApiError) as ei:
        api.list_open()
    assert ei.value.kind == "network" and "nothing is listening" in str(ei.value)
    with pytest.raises(launcher.OperatorApiError):
        api.list_open()                                       # and again, now that it is known to be down
    assert time.monotonic() - t0 < 2.5


def test_the_probe_is_only_for_a_service_that_was_down(launcher, plain_service):
    """Once a request has gone through, no extra connection is opened before the next one."""
    api = launcher.OperatorApi(plain_service.server_address[1])
    assert api._down is True
    api.list_open()
    assert api._down is False
    n = len(plain_service.seen)
    api.list_open()
    assert len(plain_service.seen) == n + 1                   # exactly one request, no probe connection
    # the service goes away and comes back on the same port: the client notices both ways
    port = plain_service.server_address[1]
    plain_service.shutdown()
    plain_service.server_close()
    with pytest.raises(launcher.OperatorApiError):
        api.list_open()
    assert api._down is True


def test_a_garbage_answer_is_a_server_error_not_a_crash(launcher, plain_service):
    plain_service.route = lambda m, p: (200, {"prompts": "not a list"})
    with pytest.raises(launcher.OperatorApiError) as ei:
        launcher.OperatorApi(plain_service.server_address[1]).list_open()
    assert ei.value.kind == "server"


def test_no_default_ssl_context_is_ever_built(launcher, plain_service, lan_service, monkeypatch):
    """urllib.request.build_opener builds a default HTTPSHandler, which loads the whole Windows
    certificate store; on the polling thread that has crashed the interpreter (0x80000003 in
    ssl.load_default_certs). The client builds its openers by hand and must never get there."""
    def boom(*a, **k):
        raise AssertionError("a default SSL context was built (loads the Windows certificate store)")

    monkeypatch.setattr(ssl, "create_default_context", boom)
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", boom)
    assert launcher.OperatorApi(plain_service.server_address[1]).list_open() == []                    # http
    assert launcher.OperatorApi(lan_service.server_address[1], auth=(USER, PWD)).list_open() == []    # https + Basic


def test_a_redirect_is_not_followed(launcher, plain_service):
    """Nothing is ever redirected here, and a 3xx must not carry Basic credentials elsewhere."""
    plain_service.route = lambda m, p: (307, {"_location": "/elsewhere"})
    api = launcher.OperatorApi(plain_service.server_address[1])
    with pytest.raises(launcher.OperatorApiError) as ei:          # a GET is the case urllib WOULD follow
        api.list_open()
    assert ei.value.kind == "server" and ei.value.status == 307
    with pytest.raises(launcher.OperatorApiError) as ei:
        api.answer("a", {"by": "A", "choice": "yes", "source": "launcher"})
    assert ei.value.kind == "server" and ei.value.status == 307
    assert [r["path"] for r in plain_service.seen] == ["/api/operator/prompts?status=open&limit=50",
                                                       "/api/operator/prompts/a/answer"]


def test_the_loopback_never_goes_through_a_system_proxy(launcher, plain_service, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{_closed_port()}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert launcher.OperatorApi(plain_service.server_address[1]).list_open() == []


def test_slots_are_indexed_by_id(launcher, plain_service):
    plain_service.route = lambda m, p: (200, {"slots": [{"id": "laser_area_safe", "kind": "flag"}, {"id": "objective"}, {}],
                                              "values": {}})
    got = launcher.OperatorApi(plain_service.server_address[1]).slots()
    assert sorted(got) == ["laser_area_safe", "objective"]
    assert plain_service.seen[-1]["path"] == "/api/operator/optical-state"


# ═══════════════════════════ the poller ═══════════════════════════

class FakeApi:
    def __init__(self):
        self.script = []              # items: a list of prompts, or an exception to raise
        self.polls = 0
        self.slot_calls = 0
        self.slot_table = {}
        self.slot_error = None
        self.answers, self.dismissals = [], []
        self.fail = None
        self.profile = "optical"      # what station_profile() answers, or an exception to raise
        self.profile_calls = 0

    def station_profile(self):
        self.profile_calls += 1
        if isinstance(self.profile, BaseException):
            raise self.profile
        return self.profile

    def list_open(self, limit=50):
        self.polls += 1
        item = self.script.pop(0) if self.script else []
        if isinstance(item, BaseException):
            raise item
        return item

    def slots(self):
        self.slot_calls += 1
        if self.slot_error:
            raise self.slot_error
        return self.slot_table

    def answer(self, pid, body):
        if self.fail:
            raise self.fail
        self.answers.append((pid, body))
        return {}

    def dismiss(self, pid, body):
        if self.fail:
            raise self.fail
        self.dismissals.append((pid, body))
        return {}


def test_polling_backs_off_quietly_while_the_service_is_down_and_resets_on_recovery(launcher):
    api, sink, log = FakeApi(), [], []
    err = launcher.OperatorApiError("refused", kind="network")
    api.script = [err] * 7 + [[P(launcher, id="a")]]
    poller = launcher.OperatorPoller(api, sink.append, log=log.append)
    delays = [poller.step() for _ in range(7)]
    assert delays == [2.0, 4.0, 8.0, 15.0, 30.0, 30.0, 30.0]
    assert log == []                                         # "service not up" is normal: no log line
    assert [e for e in sink if e[0] == "link"] == [("link", "network")] * 7
    assert sink[0] == ("profile", "optical")                 # announced once, not after every re-read
    assert poller.step() == launcher.OPERATOR_POLL_S         # recovered: back to the normal rate
    assert sink[-1][0] == "poll" and poller.failures == 0
    assert log == []                                         # silent failures need no recovery notice


def test_other_failures_are_logged_once_and_the_recovery_once(launcher):
    api, sink, log = FakeApi(), [], []
    api.script = [launcher.OperatorApiError("401", status=401, kind="auth")] * 3 + [[]]
    poller = launcher.OperatorPoller(api, sink.append, log=log.append)
    for _ in range(3):
        poller.step()
    assert len(log) == 1 and "auth" in log[0]
    poller.step()
    assert len(log) == 2 and "back" in log[1]
    assert [e for e in sink if e[0] == "link"] == [("link", "auth")] * 3


def test_an_old_service_is_asked_again_only_rarely(launcher):
    api, sink = FakeApi(), []
    api.script = [launcher.OperatorApiError("nf", status=404, kind="unsupported")]
    assert launcher.OperatorPoller(api, sink.append, log=lambda m: None).step() == launcher.OPERATOR_UNSUPPORTED_RETRY_S
    assert [e for e in sink if e[0] == "link"] == [("link", "unsupported")]


def test_a_surprise_exception_does_not_kill_polling(launcher):
    api, sink, log = FakeApi(), [], []
    api.script = [RuntimeError("bug"), []]
    poller = launcher.OperatorPoller(api, sink.append, log=log.append)
    assert poller.step() == 2.0 and ("link", "server") in sink and "RuntimeError" in log[0]
    assert poller.step() == launcher.OPERATOR_POLL_S


def test_a_plain_attestation_gets_its_slot_looked_up_once_per_minute(launcher):
    api, sink, now = FakeApi(), [], [0.0]
    api.slot_table = {"laser_area_safe": {"id": "laser_area_safe", "kind": "flag"}, "other": {"id": "other"}}
    attest = lambda: P(launcher, id="s", kind="attest", attest_slots=["laser_area_safe"])   # noqa: E731
    api.script = [[attest()], [attest()], [P(launcher, id="c")], [attest()]]
    poller = launcher.OperatorPoller(api, sink.append, clock=lambda: now[0])
    poller.step()
    assert sink[-1][1][0]["slots"] == {"laser_area_safe": {"id": "laser_area_safe", "kind": "flag"}}
    now[0] = 30
    poller.step()
    poller.step()                                             # no attestation in this poll: no lookup
    assert api.slot_calls == 1
    now[0] = 100
    poller.step()
    assert api.slot_calls == 2


def test_attestations_with_options_or_a_unit_need_no_slot_lookup(launcher):
    api, sink = FakeApi(), []
    api.script = [[P(launcher, id="o", kind="attest", attest_slots=["objective"], options=[{"id": "x"}, {"id": "y"}]),
                   P(launcher, id="u", kind="attest", attest_slots=["deg"], unit="deg")]]
    launcher.OperatorPoller(api, sink.append).step()
    assert api.slot_calls == 0


def test_a_failed_slot_lookup_never_costs_the_poll(launcher):
    api, sink = FakeApi(), []
    api.slot_error = launcher.OperatorApiError("down", kind="network")
    api.script = [[P(launcher, id="s", kind="attest", attest_slots=["laser_area_safe"])]]
    assert launcher.OperatorPoller(api, sink.append).step() == launcher.OPERATOR_POLL_S
    polled = next(e for e in sink if e[0] == "poll")
    assert polled[1][0].get("slots", {}) == {}
    assert launcher.operator_prompt_fields(polled[1][0])["mode"] == "flag"      # the safe reading


def test_the_run_loop_stops_on_the_event_and_idles_while_switched_off(launcher):
    api, sink = FakeApi(), []
    stop, on = threading.Event(), [False]
    poller = launcher.OperatorPoller(api, sink.append, enabled=lambda: on[0])
    t = threading.Thread(target=poller.run, args=(stop,), daemon=True)
    t.start()
    stop.wait(0.3)
    assert api.polls == 0                                     # off: not a single request
    on[0] = True
    for _ in range(100):
        if api.polls:
            break
        stop.wait(0.05)
    stop.set()
    t.join(3)
    assert api.polls >= 1 and not t.is_alive()


# ═══════════════════════════ only an optical station is listened to ═══════════════════════════
# The operator API is installed on an optical station only. On an STM station (or a service
# that does not say what it is) the launcher must not poll for prompts at all, and must not
# look as if it did anything: it reads GET /api/health -> station_profile, slowly, and
# that is all.

@pytest.mark.parametrize("health,expect", [
    ((200, {"status": "ok", "station_profile": "optical"}), "optical"),
    ((200, {"station_profile": " Optical "}), "optical"),            # case and padding do not matter
    ((200, {"status": "ok", "station_profile": "stm"}), "stm"),
    ((200, {"status": "ok"}), "stm"),                                # an older service says nothing
    ((200, {"station_profile": None}), "stm"),
    ((200, {"station_profile": ["optical"]}), "stm"),                # garbage is not "optical"
    ((200, ["optical"]), "stm"),                                     # not even an object
    ((404, {"detail": "not found"}), "stm"),
    ((403, {"detail": "refused"}), "stm"),
    ((500, {"detail": "boom"}), "stm")])
def test_the_station_profile_is_what_the_service_says_and_stm_when_it_does_not_say(launcher, plain_service, health, expect):
    plain_service.route = lambda m, p: health
    assert launcher.OperatorApi(plain_service.server_address[1]).station_profile() == expect
    assert [r["path"] for r in plain_service.seen] == ["/api/health"]


def test_a_body_that_is_not_json_reads_as_stm(launcher):
    api = launcher.OperatorApi(1)

    def junk(*a, **k):
        raise launcher.OperatorApiError("the service sent something that is not JSON", kind="server")

    api._request = junk
    assert api.station_profile() == "stm"


def test_a_service_that_cannot_be_reached_or_will_not_let_us_in_has_no_profile_yet(launcher, lan_service):
    """Unknown is not STM: these raise, so the poller backs off and asks again, instead of
    deciding there is nothing to listen to."""
    with pytest.raises(launcher.OperatorApiError) as ei:
        launcher.OperatorApi(_closed_port()).station_profile()
    assert ei.value.kind == "network"
    with pytest.raises(launcher.OperatorApiError) as ei:              # LAN mode, no credentials: 401
        launcher.OperatorApi(lan_service.server_address[1]).station_profile()
    assert ei.value.kind == "auth"
    assert launcher.OperatorApi(lan_service.server_address[1], auth=(USER, PWD)).station_profile() == "stm"


def test_an_stm_station_is_never_asked_for_prompts(launcher):
    api, sink, now = FakeApi(), [], [0.0]
    api.profile = "stm"
    recheck = launcher.OperatorPoller.PROFILE_RECHECK_S
    poller = launcher.OperatorPoller(api, sink.append, clock=lambda: now[0])
    assert poller.step() == recheck                                  # a minute until it looks again
    assert sink == [("profile", "stm")] and api.polls == 0
    now[0] = 10
    assert poller.step() == recheck - 10                             # not due: no request of any kind
    assert api.profile_calls == 1 and api.polls == 0
    now[0] = 60
    assert poller.step() == recheck
    assert api.profile_calls == 2 and api.polls == 0
    assert sink == [("profile", "stm")]                              # the same news is not repeated


def test_a_profile_that_cannot_be_read_does_not_start_the_polling(launcher):
    api, sink, now = FakeApi(), [], [0.0]
    api.profile = launcher.OperatorApiError("refused", kind="network")
    poller = launcher.OperatorPoller(api, sink.append, clock=lambda: now[0])
    delays = []
    for i in range(3):
        now[0] = i * 10
        delays.append(poller.step())
    assert delays == [2.0, 4.0, 8.0]                                 # the usual quiet back-off
    assert api.profile_calls == 3 and api.polls == 0                 # it keeps asking WHO, never for prompts
    assert sink == [("link", "network")] * 3
    api.profile = "stm"                                              # it comes up, and says STM
    now[0] = 40
    assert poller.step() == launcher.OperatorPoller.PROFILE_RECHECK_S and api.polls == 0


def test_a_refused_login_is_reported_once_and_never_polls(launcher):
    api, sink, log = FakeApi(), [], []
    api.profile = launcher.OperatorApiError("Unauthorized", status=401, kind="auth")
    poller = launcher.OperatorPoller(api, sink.append, log=log.append)
    for _ in range(3):
        poller.step()
    assert api.polls == 0 and len(log) == 1 and "auth" in log[0]
    assert sink == [("link", "auth")] * 3


def test_an_optical_station_is_polled_as_usual_and_its_profile_re_read_once_a_minute(launcher):
    api, sink, now = FakeApi(), [], [0.0]
    poller = launcher.OperatorPoller(api, sink.append, clock=lambda: now[0])
    delays = []
    for i in range(31):                                              # 0 .. 60 s in 2 s steps
        now[0] = i * 2.0
        delays.append(poller.step())
    assert set(delays) == {launcher.OPERATOR_POLL_S}
    assert api.polls == 31 and api.profile_calls == 2                # at 0 s and at 60 s
    assert sink[0] == ("profile", "optical") and sink.count(("profile", "optical")) == 1


def test_a_service_that_went_away_is_asked_what_it_is_before_anything_else(launcher):
    """It may come back as another station (restarted with another profile)."""
    api, sink, now = FakeApi(), [], [0.0]
    poller = launcher.OperatorPoller(api, sink.append, clock=lambda: now[0])
    poller.step()                                                    # optical: polled
    assert (api.profile_calls, api.polls) == (1, 1)
    api.script = [launcher.OperatorApiError("gone", kind="network")]
    now[0] = 2
    poller.step()                                                    # the poll fails: the service is lost
    assert poller.profile is None and api.polls == 2
    api.profile = "stm"                                              # ... and it is back, as an STM station
    now[0] = 4
    poller.step()
    assert (api.profile_calls, api.polls) == (2, 2)                  # asked who it is; asked for no prompts
    assert sink[-1] == ("profile", "stm")


def test_a_restart_into_stm_is_noticed_at_the_next_profile_read(launcher):
    api, sink, now = FakeApi(), [], [0.0]
    poller = launcher.OperatorPoller(api, sink.append, clock=lambda: now[0])
    poller.step()
    api.profile = "stm"                                              # switched and restarted behind our back
    now[0] = 58
    poller.step()                                                    # still believed optical: polled once more
    assert api.polls == 2
    now[0] = 60
    poller.step()                                                    # the minute is up: it says STM
    assert api.polls == 2 and sink[-1] == ("profile", "stm")


@pytest.mark.parametrize("health", [
    (200, {"status": "ok", "station_profile": "stm"}),
    (200, {"status": "ok"}),                      # a service that does not say
    (404, {"detail": "not found"}),               # one with no such endpoint
    (500, {"detail": "boom"})])                   # one that cannot answer
def test_nothing_asks_an_stm_station_for_prompts_in_a_whole_window(launcher, plain_service, monkeypatch, health):
    """Real thread, real HTTP: however the profile reads as STM, not one request for prompts
    reaches the service in a window long enough for dozens of polls -- but the profile IS read
    again and again (slowly), which is how a restart into another profile is noticed."""
    hits = {"health": 0, "operator": 0}

    def route(method, path):
        if path == "/api/health":
            hits["health"] += 1
            return health
        if path.startswith("/api/operator/"):
            hits["operator"] += 1
        return 200, {"prompts": []}

    plain_service.route = route
    monkeypatch.setattr(launcher, "OPERATOR_POLL_S", 0.03)           # a poll this fast could not hide
    monkeypatch.setattr(launcher.OperatorPoller, "PROFILE_RECHECK_S", 0.25)
    poller = launcher.OperatorPoller(launcher.OperatorApi(plain_service.server_address[1]), lambda e: None)
    stop = threading.Event()
    t = threading.Thread(target=poller.run, args=(stop,), daemon=True)
    t.start()
    stop.wait(1.3)
    stop.set()
    t.join(5)
    assert not t.is_alive()
    assert hits["operator"] == 0, "an STM station was asked for operator prompts"
    assert hits["health"] >= 3


def test_an_optical_station_is_polled_in_the_same_window(launcher, plain_service, monkeypatch):
    hits = {"health": 0, "prompts": 0}

    def route(method, path):
        if path == "/api/health":
            hits["health"] += 1
            return 200, {"status": "ok", "station_profile": "optical"}
        if path.startswith("/api/operator/prompts"):
            hits["prompts"] += 1
        return 200, {"prompts": []}

    plain_service.route = route
    monkeypatch.setattr(launcher, "OPERATOR_POLL_S", 0.03)
    monkeypatch.setattr(launcher.OperatorPoller, "PROFILE_RECHECK_S", 0.25)
    poller = launcher.OperatorPoller(launcher.OperatorApi(plain_service.server_address[1]), lambda e: None)
    stop = threading.Event()
    t = threading.Thread(target=poller.run, args=(stop,), daemon=True)
    t.start()
    stop.wait(1.3)
    stop.set()
    t.join(5)
    assert hits["prompts"] >= 8                                      # polled all along
    assert 2 <= hits["health"] <= 8                                  # and the profile re-read now and then


# ═══════════════════════════ the controller, without Tk ═══════════════════════════

class FakeWin:
    def __init__(self):
        self.scheduled, self.cancelled, self.bells = [], [], 0

    def after(self, ms, fn=None, *a):
        self.scheduled.append((ms, fn))
        return f"after#{len(self.scheduled)}"

    def after_cancel(self, ident):
        self.cancelled.append(ident)

    def bell(self):
        self.bells += 1


class FakeApp:
    def __init__(self, root_dir):
        self.win = FakeWin()
        self.root_dir = root_dir
        self.tk = self.ttk = None
        self._dpi_scale = 1.0
        self.log = []

    def _lan_auth_pair(self):
        return ("", "")

    def _log(self, msg):
        self.log.append(msg)


class FakeView:
    def __init__(self, prompt, waiting):
        self.prompt, self.prompt_id, self.waiting = prompt, prompt["id"], waiting
        self.shown = self.closed = False
        self.errors, self.busy = [], []

    def show(self):
        self.shown = True

    def update_waiting(self, n):
        self.waiting = n

    def set_busy(self, b):
        self.busy.append(b)

    def show_error(self, text):
        self.errors.append(text)

    def close(self):
        self.closed = True


class Rig:
    """A controller wired to fakes: views are recorded, calls run inline, time is ours."""

    def __init__(self, launcher, tmp_path, **kw):
        self.launcher = launcher
        self.app = FakeApp(tmp_path)
        self.api = FakeApi()
        self.now = [100.0]
        self.views, self.sounds = [], []
        self.ctl = launcher.OperatorPopupController(
            self.app, api=self.api, clock=lambda: self.now[0], view_factory=self._view,
            play=lambda sev, **k: self.sounds.append(sev), start_thread=lambda fn: fn(), **kw)

    def _view(self, prompt, waiting):
        v = FakeView(prompt, waiting)
        self.views.append(v)
        return v

    def poll(self, *prompts):
        self.ctl.inbox.put(("poll", list(prompts)))
        self.ctl.pump_once()

    @property
    def live(self):
        return self.ctl.view


@pytest.fixture
def rig(launcher, tmp_path):
    return Rig(launcher, tmp_path)


def test_a_new_prompt_opens_one_window_and_sounds_once(launcher, rig):
    a = P(launcher, id="a", severity="safety")
    rig.poll(a)
    assert len(rig.views) == 1 and rig.live.shown and rig.sounds == ["safety"]
    rig.poll(a)
    rig.poll(a)
    assert len(rig.views) == 1 and rig.sounds == ["safety"]       # the same prompt is not news again


def test_two_prompts_are_shown_one_at_a_time_with_the_rest_counted(launcher, rig):
    a, b = P(launcher, id="a", created_at="2026-11-03T10:00:00+00:00"), P(launcher, id="b", created_at="2026-11-03T10:01:00+00:00")
    rig.poll(a, b)
    assert [v.prompt_id for v in rig.views] == ["a"] and rig.live.waiting == 1
    assert len(rig.sounds) == 1                                   # one sound per poll, however many arrive
    rig.poll(a, b, P(launcher, id="c"))
    assert rig.live.waiting == 2 and len(rig.views) == 1 and len(rig.sounds) == 2    # the newcomer sounds, no new window


def test_the_most_severe_newcomer_picks_the_sound(launcher, rig):
    rig.poll(P(launcher, id="i", severity="info"), P(launcher, id="s", severity="safety"))
    assert rig.sounds == ["safety"]


def test_a_prompt_answered_elsewhere_closes_its_window_and_the_next_one_appears(launcher, rig):
    a, b = P(launcher, id="a", created_at="2026-11-03T10:00:00+00:00"), P(launcher, id="b", created_at="2026-11-03T10:01:00+00:00")
    rig.poll(a, b)
    first = rig.live
    rig.poll(b)                                                   # a is gone from the open list
    assert first.closed and rig.live.prompt_id == "b" and rig.live is not first
    assert any("no longer open" in m and "Change the waveplate" in m for m in rig.app.log)
    rig.poll()
    assert rig.live is None and rig.views[-1].closed


def test_answering_sends_the_validated_body_and_closes_on_success(launcher, rig):
    p = P(launcher, id="a", kind="number", number_min=0, number_max=20, unit="mW")
    rig.poll(p)
    rig.ctl.submit("a", {"text": "12.5", "note": "dial read 12.5"}, "Ana Ruiz")
    assert rig.live.busy == [True]
    rig.ctl.pump_once()
    assert rig.api.answers == [("a", {"by": "Ana Ruiz", "source": "launcher", "value": 12.5, "note": "dial read 12.5"})]
    assert rig.live is None and rig.views[0].closed
    assert any("Answered" in m for m in rig.app.log)
    assert launcher._load_operator_prefs(rig.app.root_dir)["operator_name"] == "Ana Ruiz"   # remembered
    rig.poll(p)                                                   # a poll that was already in flight lists it again
    assert rig.live is None                                       # ... and it stays gone


def test_an_out_of_range_number_never_reaches_the_service(launcher, rig):
    rig.poll(P(launcher, id="a", kind="number", number_min=0, number_max=20, unit="mW"))
    rig.ctl.submit("a", {"text": "25"}, "Ana")
    rig.ctl.pump_once()
    assert rig.api.answers == [] and rig.live is not None
    assert rig.live.errors and "above the maximum 20 mW" in rig.live.errors[-1]
    assert rig.live.busy == []                                    # it never even went busy
    rig.ctl.submit("a", {"text": "5"}, "   ")                     # and no name is no answer
    assert rig.api.answers == [] and "name" in rig.live.errors[-1]


def test_a_rejection_by_the_service_is_shown_and_the_window_stays(launcher, rig):
    rig.poll(P(launcher, id="a"))
    rig.api.fail = launcher.OperatorApiError("5 is not allowed here", status=400, kind="invalid")
    rig.ctl.submit("a", {"choice": "yes"}, "Ana")
    rig.ctl.pump_once()
    assert rig.live is not None and rig.live.busy == [True, False]
    assert rig.live.errors == ["5 is not allowed here"]
    assert rig.ctl.prompts.has("a")                               # still open: the operator can correct it


def test_a_prompt_handled_elsewhere_in_the_meantime_just_closes(launcher, rig):
    rig.poll(P(launcher, id="a"))
    rig.api.fail = launcher.OperatorApiError("already answered", status=409, kind="gone")
    rig.ctl.submit("a", {"choice": "yes"}, "Ana")
    rig.ctl.pump_once()
    assert rig.live is None and any("already handled elsewhere" in m for m in rig.app.log)


@pytest.mark.parametrize("kind,shown", [
    ("network", "Could not reach the main service; try again. (boom)"),      # nothing answered: say so
    ("auth", "The main service refused the launcher's LAN credentials; check them."),   # 401
    ("forbidden", "boom"),                          # 403: the write guard's own reason, as is
    ("server", "boom")])                            # 503 "could not save": the service's reason, as is
def test_a_failed_send_keeps_the_window_and_says_so(launcher, rig, kind, shown):
    rig.poll(P(launcher, id="a"))
    rig.api.fail = launcher.OperatorApiError("boom", kind=kind)
    rig.ctl.submit("a", {"choice": "yes"}, "Ana")
    rig.ctl.pump_once()
    assert rig.live is not None and rig.live.busy[-1] is False
    assert rig.live.errors[-1] == shown             # exactly: a refusal must not read like an outage


def test_any_unexpected_exception_in_a_call_still_answers_the_window(launcher, rig):
    rig.poll(P(launcher, id="a"))
    rig.api.fail = RuntimeError("bug in the call thread")
    rig.ctl.submit("a", {"choice": "yes"}, "Ana")
    rig.ctl.pump_once()
    assert rig.live is not None and rig.live.busy[-1] is False and rig.live.errors


def test_ignore_closes_without_answering_and_the_prompt_returns_with_a_sound(launcher, rig):
    rig.poll(P(launcher, id="a"))
    rig.ctl.ignore("a")
    assert rig.live is None and rig.api.answers == [] and rig.api.dismissals == []
    assert rig.ctl.prompts.has("a")                               # not answered, not dismissed: still open
    rig.now[0] += launcher.OPERATOR_SNOOZE_S - 1
    rig.poll(P(launcher, id="a"))
    assert rig.live is None                                       # a poll is not a reason to pop up again
    n_sounds = len(rig.sounds)
    rig.now[0] += 2
    rig.ctl.pump_once()
    assert rig.live is not None and rig.live.prompt_id == "a"     # time is up
    assert len(rig.sounds) == n_sounds + 1                        # and it announces itself again


def test_ignoring_one_prompt_shows_the_next_one(launcher, rig):
    a, b = P(launcher, id="a", created_at="2026-11-03T10:00:00+00:00"), P(launcher, id="b", created_at="2026-11-03T10:01:00+00:00")
    rig.poll(a, b)
    rig.ctl.ignore("a")
    assert rig.live.prompt_id == "b" and rig.live.waiting == 1    # a is still waiting, snoozed


def test_decline_sends_a_dismissal_with_the_reason(launcher, rig):
    rig.poll(P(launcher, id="a"))
    rig.ctl.reject("a", "Ana", "the half-wave plate is in another lab")
    rig.ctl.pump_once()
    assert rig.api.dismissals == [("a", {"by": "Ana", "source": "launcher",
                                         "reason": "the half-wave plate is in another lab"})]
    assert rig.api.answers == [] and rig.live is None
    assert any("Declined" in m for m in rig.app.log)
    rig.poll(P(launcher, id="b"))
    rig.ctl.reject("b", "  ")
    assert rig.api.dismissals and len(rig.api.dismissals) == 1 and "name" in rig.live.errors[-1]


def test_the_test_popup_is_local_and_sends_nothing(launcher, rig):
    rig.ctl.show_demo()
    assert rig.live is not None and rig.live.prompt["id"].startswith("demo-") and rig.sounds == ["action"]
    rig.ctl.submit(rig.live.prompt_id, {"choice": "yes"}, "Ana")
    rig.ctl.pump_once()
    assert rig.api.answers == [] and rig.live is None
    rig.poll()                                                    # the service knows nothing of it
    rig.ctl.show_demo()
    rig.poll()
    assert rig.live is not None                                   # a poll does not make a local prompt "gone"


def test_sound_off_means_silence_but_the_window_still_opens(launcher, rig):
    rig.ctl.set_sound(False)
    rig.poll(P(launcher, id="a", severity="safety"))
    assert rig.live is not None and rig.sounds == []
    assert launcher._load_operator_prefs(rig.app.root_dir)["sound"] is False


def test_switching_off_closes_the_window_forgets_the_queue_and_ignores_polls(launcher, rig):
    rig.poll(P(launcher, id="a"))
    rig.ctl.set_enabled(False)
    assert rig.live is None and len(rig.ctl.prompts) == 0 and rig.ctl.link == "off"
    rig.poll(P(launcher, id="a"))
    assert rig.live is None and len(rig.views) == 1
    rig.ctl.set_enabled(True)
    rig.poll(P(launcher, id="a"))
    assert rig.live is not None and rig.live.prompt_id == "a"     # back on: it is new again
    assert launcher._load_operator_prefs(rig.app.root_dir)["enabled"] is True


def test_link_events_drive_the_status_line(launcher, rig):
    class Var:
        def __init__(self):
            self.text = None

        def set(self, t):
            self.text = t

    var = Var()
    rig.ctl.bind_status_var(var)
    assert "Waiting for the main service" in var.text
    rig.poll()
    assert "nothing is waiting" in var.text
    rig.poll(P(launcher, id="a"), P(launcher, id="b"))
    assert "2 waiting" in var.text
    rig.ctl.inbox.put(("link", "unsupported"))
    rig.ctl.pump_once()
    assert "no operator prompts" in var.text
    rig.ctl.set_enabled(False)
    assert "no popup" in var.text


def test_a_window_that_cannot_be_built_does_not_stop_the_pump_or_spin(launcher, tmp_path):
    app, calls = FakeApp(tmp_path), []

    def boom(prompt, waiting):
        calls.append(prompt["id"])
        raise RuntimeError("Tk exploded")

    now = [0.0]
    ctl = launcher.OperatorPopupController(app, api=FakeApi(), clock=lambda: now[0], view_factory=boom,
                                           play=lambda *a, **k: None, start_thread=lambda fn: fn())
    ctl.inbox.put(("poll", [P(launcher, id="a")]))
    ctl.pump_once()
    for _ in range(5):
        ctl.pump_once()
    assert calls == ["a"]                                         # one attempt, then it is snoozed, not retried every tick
    now[0] += launcher.OPERATOR_SNOOZE_S + 1
    ctl.pump_once()
    assert calls == ["a", "a"]


def test_rebuilding_the_launcher_ui_reopens_the_window_without_a_sound(launcher, rig):
    rig.poll(P(launcher, id="a"))
    first, n = rig.live, len(rig.sounds)
    rig.ctl.prepare_ui_rebuild()
    assert first.closed and rig.live is None and rig.ctl.status_var is None
    rig.ctl.after_ui_rebuild()
    assert rig.live is not first and rig.live.prompt_id == "a" and len(rig.sounds) == n


def test_stop_closes_the_window_and_cancels_the_pump(launcher, rig):
    rig.ctl._schedule_pump()
    rig.poll(P(launcher, id="a"))
    rig.ctl.stop()
    assert rig.live is None and rig.app.win.cancelled
    rig.ctl._pump()                                               # a stray tick after stop does nothing
    assert rig.app.win.scheduled[-1][0] == rig.ctl.PUMP_MS and len(rig.app.win.scheduled) == 1


def test_the_pump_survives_a_bad_event(launcher, rig):
    rig.ctl.inbox.put(("poll", "not a list of prompts"))
    rig.ctl.inbox.put(("poll", [P(launcher, id="a")]))
    rig.ctl._pump()                                               # swallows the first, reschedules itself
    assert rig.app.win.scheduled


def test_an_stm_profile_silences_the_section_and_clears_the_screen(launcher, rig):
    class Var:
        text = None

        def set(self, t):
            self.text = t

    var, shown = Var(), []
    rig.ctl.bind_status_var(var)
    rig.ctl.bind_profile_view(shown.append)
    assert shown == [False]                                          # not known yet counts as STM: folded
    rig.poll(P(launcher, id="a"))                                    # an earlier optical service left this behind
    first = rig.live
    assert first is not None
    rig.ctl.inbox.put(("profile", "stm"))
    rig.ctl.pump_once()
    assert rig.ctl.profile == "stm" and shown == [False, False]
    assert first.closed and rig.live is None and len(rig.ctl.prompts) == 0
    assert "Only used on optical stations" in var.text
    rig.ctl.inbox.put(("profile", "stm"))                            # the same news again changes nothing
    rig.ctl.pump_once()
    assert shown == [False, False]
    rig.ctl.inbox.put(("profile", "optical"))
    rig.ctl.pump_once()
    assert shown[-1] is True and "Only used on optical stations" not in var.text
    rig.poll(P(launcher, id="b"))                                    # and prompts are listened to again
    assert rig.live is not None and rig.live.prompt_id == "b"


def test_a_view_bound_late_is_told_what_is_already_known(launcher, rig):
    rig.ctl.inbox.put(("profile", "stm"))
    rig.ctl.pump_once()
    shown = []
    rig.ctl.bind_profile_view(shown.append)                          # e.g. the UI was rebuilt in another language
    assert shown == [False]
    rig.ctl.inbox.put(("profile", "optical"))
    rig.ctl.pump_once()
    late = []
    rig.ctl.bind_profile_view(late.append)
    assert late == [True]


def test_a_failing_view_callback_is_dropped_not_fatal(launcher, rig):
    def broken(profile):
        raise RuntimeError("the widget is gone")

    rig.ctl.bind_profile_view(broken)
    rig.ctl.inbox.put(("profile", "stm"))
    rig.ctl.pump_once()                                              # no exception out of the pump
    assert rig.ctl.profile == "stm" and rig.ctl._profile_view is None


def test_switching_off_and_on_keeps_what_is_known_about_the_station(launcher, rig):
    class Var:
        text = None

        def set(self, t):
            self.text = t

    var, shown = Var(), []
    rig.ctl.bind_status_var(var)
    rig.ctl.bind_profile_view(shown.append)
    rig.ctl.inbox.put(("profile", "stm"))
    rig.ctl.pump_once()
    assert shown[-1] is False                                        # folded on an STM station
    rig.ctl.set_enabled(False)
    assert "no popup" in var.text
    assert shown[-1] is True                                         # ... but the switch stays reachable while it is off
    rig.ctl.inbox.put(("profile", "optical"))                        # news that arrives while it is off is ignored
    rig.ctl.pump_once()
    assert rig.ctl.profile == "stm"
    rig.ctl.set_enabled(True)
    assert "Only used on optical stations" in var.text               # not "Waiting for the main service"
    assert shown[-1] is False


def test_status_text_names_the_stm_station_but_the_off_switch_comes_first(launcher):
    f = launcher.operator_status_text
    assert "Only used on optical stations" in f(enabled=True, link="network", open_count=0, profile="stm")
    assert "Only used on optical stations" in f(enabled=True, link="ok", open_count=5, profile="stm")
    assert "no popup" in f(enabled=False, link="ok", open_count=0, profile="stm")
    assert "Waiting for the main service" in f(enabled=True, link="network", open_count=0, profile="optical")
    assert "Waiting for the main service" in f(enabled=True, link="network", open_count=0, profile=None)
    assert "nothing is waiting" in f(enabled=True, link="ok", open_count=0, profile="optical")


# ═══════════════════════════ sound and the taskbar ═══════════════════════════

class FakeWinsound:
    def __init__(self, fail=False):
        self.beeps, self.fail = [], fail

    def Beep(self, hz, ms):                                        # noqa: N802 - winsound's API
        if self.fail:
            raise RuntimeError("Failed to beep")
        self.beeps.append((hz, ms))


def test_each_severity_plays_its_own_pattern(launcher):
    for sev in ("safety", "action", "info"):
        ws = FakeWinsound()
        assert launcher.play_operator_alert(sev, backend=ws, in_thread=False) is True
        assert ws.beeps == launcher.operator_alert_beeps(sev)


def test_the_beeps_run_on_their_own_thread_so_the_ui_never_waits(launcher):
    done, tid = threading.Event(), []

    class Slow(FakeWinsound):
        def Beep(self, hz, ms):                                    # noqa: N802
            tid.append(threading.get_ident())
            done.set()

    assert launcher.play_operator_alert("info", backend=Slow()) is True
    assert done.wait(2) and tid[0] != threading.get_ident()


def test_no_winsound_is_not_an_error(launcher, monkeypatch):
    monkeypatch.setitem(sys.modules, "winsound", None)             # `import winsound` now raises ImportError
    assert launcher._load_winsound() is None
    assert launcher.play_operator_alert("safety") is False        # silent, no exception
    rang = []
    assert launcher.play_operator_alert("safety", bell=lambda: rang.append(1)) is True and rang == [1]
    assert launcher.play_operator_alert("safety", bell=lambda: 1 / 0) is False


def test_a_machine_without_an_audio_device_is_not_an_error(launcher):
    assert launcher.play_operator_alert("action", backend=FakeWinsound(fail=True), in_thread=False) is True


def test_flash_taskbar_fills_flashwinfo_and_never_raises(launcher):
    import ctypes

    calls = []

    class U32:
        def FlashWindowEx(self, ref):                              # noqa: N802
            info = ref._obj
            calls.append((info.cbSize, info.hwnd, info.dwFlags, info.uCount, info.dwTimeout))

    assert launcher.flash_taskbar(4321, user32=U32(), count=3) is True
    size, hwnd, flags, count, timeout = calls[0]
    assert (hwnd, flags, count, timeout) == (4321, 0x3 | 0xC, 3, 0)   # FLASHW_ALL | FLASHW_TIMERNOFG
    assert size == (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20)  # cbSize is the struct's real size

    class Broken:
        def FlashWindowEx(self, ref):                              # noqa: N802
            raise OSError("no such window")

    assert launcher.flash_taskbar(1, user32=Broken()) is False


# ═══════════════════════════ translations ═══════════════════════════

def _operator_tr_calls():
    src = _LAUNCHER_PATH.read_text(encoding="utf-8")
    lines = src.splitlines()
    start = next(i for i, ln in enumerate(lines, 1) if ln.startswith("OPERATOR_PROMPTS_API"))
    end = next(i for i, ln in enumerate(lines, 1) if ln.startswith("class LauncherApp"))
    frame = next(i for i, ln in enumerate(lines, 1) if "Operator prompts (desktop popup + sound" in ln)
    frame_end = next(i for i, ln in enumerate(lines, 1) if i > frame and "── GUI URLs" in ln)
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_tr" and len(node.args) >= 2:
            if start <= node.lineno < end or frame <= node.lineno < frame_end:
                en = node.args[1]
                assert isinstance(en, ast.Constant) and isinstance(en.value, str), (
                    f"line {node.lineno}: an f-string cannot be translated by key; use {{placeholders}} + .format()")
                out.append((node.lineno, en.value))
    return out


def test_every_popup_string_is_translated_into_all_five_languages(launcher):
    calls = _operator_tr_calls()
    assert len(calls) > 50, "the scan found suspiciously few _tr() calls in the operator section"
    problems = []
    for lineno, en in calls:
        entry = launcher._TR_CATALOG.get(en)
        if not entry:
            problems.append(f"line {lineno}: no catalog entry for {en[:60]!r}")
            continue
        want = sorted(re.findall(r"\{[a-z_]+\}", en))
        for lang in ("ja", "de", "fr", "es", "cs"):
            text = entry.get(lang)
            if not text:
                problems.append(f"line {lineno}: {lang} missing for {en[:60]!r}")
            elif sorted(re.findall(r"\{[a-z_]+\}", text)) != want:
                problems.append(f"line {lineno}: {lang} changed the placeholders of {en[:60]!r}")
    assert not problems, "\n".join(problems)


def test_the_popup_speaks_the_launchers_language(launcher):
    launcher._set_lang("de")
    assert launcher.operator_status_text(enabled=True, link="ok", open_count=2) == "Überwachung aktiv — 2 warten auf Sie."
    msg = ""
    try:
        launcher.build_operator_answer(P(launcher, kind="number"), {"text": "x"}, by="A")
    except launcher.OperatorFormError as exc:
        msg = str(exc)
    assert msg == "„x“ ist keine Zahl."
    launcher._set_lang("zh")
    assert launcher.operator_status_text(enabled=False, link="ok", open_count=0) == "已关闭：不会弹出提示窗。"


# ═══════════════════════════ how the launcher wires it in ═══════════════════════════

def test_the_launcher_builds_starts_stops_and_rebuilds_the_popup():
    src = _LAUNCHER_PATH.read_text(encoding="utf-8")
    assert src.index("self._operator_popup = OperatorPopupController(self)") < src.index("self._build_ui()\n        self._refresh_status()")
    assert "self._operator_popup.start()" in src
    exit_now = src[src.index("def _exit_now"):src.index("# ── IPC: second instance")]
    assert "pop.stop()" in exit_now
    lang = src[src.index("def _apply_language"):src.index("def _admin_pin_path")]
    assert lang.index("prepare_ui_rebuild()") < lang.index("child.destroy()") < lang.index("after_ui_rebuild()")


def test_the_popup_answers_only_through_the_operator_endpoints():
    """No agent can answer: the launcher uses /api/operator, never /api/ext, and holds no write helper for it."""
    src = _LAUNCHER_PATH.read_text(encoding="utf-8")
    section = src[src.index("OPERATOR_PROMPTS_API ="):src.index("class LauncherApp")]
    assert "/api/ext" not in section
    assert "any_scheme=False" in section        # both writes
    assert section.count("ProxyHandler({})") == 1


# ═══════════════════════════ the Tk window (only where a Tk root can exist) ═══════════════════════════

def _collect_tk_garbage():
    """Free destroyed Tk objects HERE, on the thread that made them. A destroyed Tk root is
    still garbage until the collector runs, and if the collector then runs on another thread
    (the poller thread's, say, in the end-to-end test) Tcl panics -- "Tcl_AsyncDelete: async
    handler deleted by the wrong thread" -- which on Windows is a hard crash (0x80000003)."""
    import gc
    for _ in range(3):
        gc.collect()


@pytest.fixture
def tk_root():
    import time

    tk = pytest.importorskip("tkinter")
    root, last = None, None
    for _attempt in range(4):
        try:
            root = tk.Tk()
            break
        except tk.TclError as exc:
            # No display (CI, an ssh session) fails the same way every time; a Windows
            # box under load can also fail once reading Tcl's own library files
            # ("couldn't read file .../ttk/progress.tcl"), so try a few times first.
            last = exc
            time.sleep(0.4)
    if root is None:
        pytest.skip(f"no usable Tk: {str(last)[:200]}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass
    del root
    _collect_tk_garbage()


class RecordingController:
    """Just enough of the controller for a view: modules, the root, and the callbacks recorded."""

    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.win, self.dpi_scale, self.app = tk, ttk, root, 1.0, None
        self.calls = []

    def submit(self, pid, form, name):
        self.calls.append(("submit", pid, form, name))

    def reject(self, pid, name, reason=""):
        self.calls.append(("reject", pid, name, reason))

    def ignore(self, pid):
        self.calls.append(("ignore", pid))


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _texts(widget):
    out = []
    for w in _walk(widget):
        try:
            out.append(str(w.cget("text")))
        except Exception:  # noqa: BLE001
            pass
    return out


def _view(launcher, tk_root, *, waiting=0, **kw):
    ctl = RecordingController(tk_root)
    return ctl, launcher.OperatorPromptView(ctl, P(launcher, **kw), waiting=waiting, name="")


def test_view_confirm_has_yes_no_decline_and_ignore(launcher, tk_root):
    ctl, view = _view(launcher, tk_root, severity="safety")
    try:
        texts = _texts(view.top)
        for want in ("Yes", "No", "Can't do it / decline", "Ignore for now", "Change the waveplate",
                     "SAFETY — YOUR ACTION IS NEEDED"):
            assert want in texts, (want, texts)
        assert view.top.state() == "withdrawn"                     # built hidden; show() is separate
    finally:
        view.close()


def test_view_ignores_clicks_until_it_is_armed(launcher, tk_root):
    ctl, view = _view(launcher, tk_root)
    try:
        view.name_var.set("Ana")
        view._on_answer("yes")                                     # the window has only just appeared
        assert ctl.calls == []
        assert all("disabled" in b.state() for b in view._action_buttons)
        view._arm()
        assert all("disabled" not in b.state() for b in view._action_buttons)
        view._on_answer("yes")
        assert ctl.calls == [("submit", "p1", {"choice": "yes", "text": "", "confirmed": False, "note": ""}, "Ana")]
        view.set_busy(True)
        view._on_answer("no")                                      # a double click while the request is out
        assert len(ctl.calls) == 1 and all("disabled" in b.state() for b in view._action_buttons)
        view.set_busy(False)
        assert all("disabled" not in b.state() for b in view._action_buttons)
    finally:
        view.close()


def test_view_collects_per_kind(launcher, tk_root):
    # choice: radio buttons, nothing preselected
    ctl, view = _view(launcher, tk_root, kind="choice",
                      options=[{"id": "hwp_532", "label": "λ/2 @532 nm"}, {"id": "hwp_1064", "label": "λ/2 @1064 nm"}])
    try:
        assert "λ/2 @1064 nm" in _texts(view.top) and view._form()["choice"] is None
        view.choice_var.set("hwp_1064")
        assert view._form()["choice"] == "hwp_1064"
    finally:
        view.close()
    # number: entry + unit + range hint
    ctl, view = _view(launcher, tk_root, kind="number", number_min=0, number_max=20, unit="mW")
    try:
        texts = _texts(view.top)
        assert "mW" in texts and "Allowed range: 0 – 20 mW" in texts
        view.value_var.set("7")
        assert view._form()["text"] == "7"
    finally:
        view.close()
    # text: a Text widget
    ctl, view = _view(launcher, tk_root, kind="text")
    try:
        view.text_widget.insert("1.0", "Nikon 40x")
        assert view._form()["text"] == "Nikon 40x"
    finally:
        view.close()
    # plain attestation: a tick box and the slots it will set
    ctl, view = _view(launcher, tk_root, kind="attest", attest_slots=["laser_area_safe"])
    try:
        texts = _texts(view.top)
        assert "I confirm that the above is true" in texts and "Confirm and submit" in texts
        assert any("laser_area_safe" in t for t in texts)
        assert view._form()["confirmed"] is False
        view.confirm_var.set(True)
        assert view._form()["confirmed"] is True
    finally:
        view.close()


def test_view_unknown_kind_only_offers_ignore(launcher, tk_root):
    ctl, view = _view(launcher, tk_root, kind="telepathy")
    try:
        assert view._action_buttons == []
        assert "Answer this kind of prompt in the web page." in _texts(view.top)
    finally:
        view.close()


def test_view_waiting_count_message_and_close_button_means_ignore(launcher, tk_root):
    ctl, view = _view(launcher, tk_root, waiting=2)
    try:
        assert view.waiting_var.get() == "2 more waiting"
        view.update_waiting(0)
        assert view.waiting_var.get() == ""
        view.show_error("5 is not allowed")
        assert view.message_var.get() == "5 is not allowed"
        view.top.tk.call(view.top.protocol("WM_DELETE_WINDOW"))    # what the window's X button runs
        assert ctl.calls == [("ignore", "p1")]
    finally:
        view.close()


def test_view_can_be_shown_and_closed(launcher, tk_root, monkeypatch):
    monkeypatch.setattr(launcher, "flash_taskbar", lambda *a, **k: True)
    ctl, view = _view(launcher, tk_root)
    view.show()
    try:
        assert view.top.state() == "normal"
        assert bool(view.top.attributes("-topmost"))
    finally:
        view.close()
    with pytest.raises(Exception):
        view.top.state()                                           # destroyed


def test_real_controller_and_view_end_to_end_without_a_service(launcher, tk_root, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "flash_taskbar", lambda *a, **k: True)
    import tkinter as tk
    from tkinter import ttk

    app = FakeApp(tmp_path)
    app.win, app.tk, app.ttk = tk_root, tk, ttk
    api, sounds = FakeApi(), []
    ctl = launcher.OperatorPopupController(app, api=api, play=lambda sev, **k: sounds.append(sev),
                                           start_thread=lambda fn: fn())
    try:
        ctl.inbox.put(("poll", [P(launcher, id="a", kind="number", number_min=0, number_max=20, unit="mW",
                                  severity="safety")]))
        ctl.pump_once()
        view = ctl.view
        assert isinstance(view, launcher.OperatorPromptView) and sounds == ["safety"]
        view._arm()
        view.name_var.set("Ana")
        view.value_var.set("99")
        view._on_answer()                                          # out of range: refused in the window
        assert api.answers == [] and "above the maximum" in view.message_var.get()
        view.value_var.set("12.5")
        view._on_answer()
        ctl.pump_once()
        assert api.answers == [("a", {"by": "Ana", "source": "launcher", "value": 12.5})]
        assert ctl.view is None
    finally:
        ctl.stop()


# ═══════════════════════════ the real launcher UI, built without starting anything ═══════════════════════════
# A typo in the launcher's own "Operator prompts" section would stop the whole launcher
# from opening, and nothing above runs that code. So build the real LauncherApp -- its
# root window withdrawn and never run, no tray / IPC port / Tailscale probe / port probe /
# release check / poller thread, everything under a temp data root -- and drive the new
# section's controls.

@pytest.fixture
def real_app(launcher, tmp_path, monkeypatch):
    import time
    import tkinter as tk

    real_tk = tk.Tk

    def quiet_tk(*a, **k):
        last = None
        for _attempt in range(4):             # see tk_root: a loaded Windows box can fail once
            try:
                root = real_tk(*a, **k)
                root.withdraw()
                return root
            except tk.TclError as exc:
                last = exc
                time.sleep(0.4)
        pytest.skip(f"no usable Tk: {str(last)[:200]}")

    monkeypatch.setattr(tk, "Tk", quiet_tk)
    for name in ("_write_launcher_lock", "_clear_launcher_lock", "_sweep_lan_auth_handoff"):
        monkeypatch.setattr(launcher, name, lambda *a, **k: None)
    monkeypatch.setattr(launcher, "_bind_ipc_port", lambda: None)
    monkeypatch.setattr(launcher, "_startup_enabled", lambda: False)
    monkeypatch.setattr(launcher, "_lan_ip", lambda: "")
    monkeypatch.setattr(launcher, "_remote_ai_handoff_path", lambda: tmp_path / "ai-remote.handoff.json")
    for meth in ("_init_tray", "_detect_tailscale_async", "_schedule_tailscale_redetect",
                 "_refresh_status", "_check_github_release"):
        monkeypatch.setattr(launcher.LauncherApp, meth, lambda self, *a, **k: None)
    # no polling thread: it would ask whatever listens on the launcher's port
    monkeypatch.setattr(launcher.OperatorPopupController, "_spawn", staticmethod(lambda fn: None))
    launcher._save_operator_prefs(tmp_path, {"enabled": True, "sound": True, "operator_name": "Ana Ruiz"})
    app = launcher.LauncherApp(tmp_path)
    app._operator_popup._view_factory = lambda prompt, waiting: FakeView(prompt, waiting)
    yield app
    try:
        app.win.destroy()
    except Exception:  # noqa: BLE001 - a test may have destroyed it already
        pass
    _collect_tk_garbage()


def _find(widget, cls_name, text):
    for w in _walk(widget):
        if w.winfo_class() == cls_name:
            try:
                if str(w.cget("text")) == text:
                    return w
            except Exception:  # noqa: BLE001
                pass
    return None


def test_launcher_ui_has_the_operator_section_and_remembers_the_name(launcher, real_app):
    frame = _find(real_app.win, "TLabelframe", "Operator prompts")
    assert frame is not None, "the launcher window has no Operator prompts section"
    texts = _texts(frame)
    for want in ("Pop up a window", "Play a sound", "Test popup", "Your name (filled in for you in the popup)"):
        assert want in texts, (want, texts)
    assert real_app.op_name_var.get() == "Ana Ruiz"                     # read back from the launcher's own config
    assert real_app.op_enabled_var.get() is True and real_app.op_sound_var.get() is True
    assert "nothing is waiting" not in real_app.op_status_var.get()     # nothing has been heard yet
    assert real_app._operator_popup._pump_id is not None                # start() ran


def test_launcher_toggles_are_saved_in_the_launchers_own_config(launcher, real_app, tmp_path):
    real_app.op_enabled_var.set(False)
    real_app.op_sound_var.set(False)
    real_app._on_operator_toggle()
    assert launcher._load_operator_prefs(tmp_path) == {"enabled": False, "sound": False, "operator_name": "Ana Ruiz"}
    assert "no popup" in real_app.op_status_var.get()
    real_app.op_enabled_var.set(True)
    real_app._on_operator_toggle()
    assert launcher._load_operator_prefs(tmp_path)["enabled"] is True


def test_the_test_popup_button_opens_a_local_prompt(launcher, real_app):
    button = _find(real_app.win, "TButton", "Test popup")
    assert button is not None
    button.invoke()
    pop = real_app._operator_popup
    assert pop.view is not None and pop.view.prompt["id"].startswith("demo-")


def test_switching_language_rebuilds_the_section_and_reopens_the_window(launcher, real_app):
    pop = real_app._operator_popup
    pop.show_demo()
    first = pop.view
    assert first is not None
    real_app._apply_language("de")
    try:
        assert first.closed                                              # never left standing on a destroyed widget
        assert pop.view is not None and pop.view is not first            # shown again once the UI is rebuilt
        assert _find(real_app.win, "TLabelframe", "Anfragen an den Bediener") is not None
        assert pop.status_var is not None and pop.status_var is real_app.op_status_var
        assert "Hauptdienst" in real_app.op_status_var.get()             # "Waiting for the main service", in German
    finally:
        launcher._set_lang("en")


def test_closing_the_launcher_stops_the_popup(launcher, real_app):
    pop = real_app._operator_popup
    pop.show_demo()
    assert pop.view is not None
    real_app._exit_now()
    assert pop._stopped and pop.view is None


def test_the_section_folds_away_on_an_stm_station_and_comes_back(launcher, real_app):
    pop, body = real_app._operator_popup, real_app._op_body
    assert body.winfo_manager() == ""                                # not known yet counts as STM: folded
    pop.inbox.put(("profile", "optical"))
    pop.pump_once()
    assert body.winfo_manager() == "grid"                            # the service says optical: the controls appear
    assert "Only used on optical stations" not in real_app.op_status_var.get()
    pop.inbox.put(("profile", "stm"))                                # restarted as an STM station
    pop.pump_once()
    assert body.winfo_manager() == ""
    assert "Only used on optical stations" in real_app.op_status_var.get()


def test_the_switch_stays_reachable_while_popups_are_off_whatever_the_station(launcher, real_app):
    """Folding the controls away must not leave the operator no way to switch popups back on."""
    pop, body = real_app._operator_popup, real_app._op_body
    for station, when_on in (("optical", "grid"), ("stm", "")):
        pop.inbox.put(("profile", station))
        pop.pump_once()
        assert body.winfo_manager() == when_on, station
        real_app.op_enabled_var.set(False)
        real_app._on_operator_toggle()                               # switched off ...
        assert body.winfo_manager() == "grid", station               # ... the switch is there, whatever the station
        assert launcher._load_operator_prefs(real_app.root_dir)["enabled"] is False
        real_app.op_enabled_var.set(True)
        real_app._on_operator_toggle()                               # ... and on again: as the station requires
        assert body.winfo_manager() == when_on, station
        assert launcher._load_operator_prefs(real_app.root_dir)["enabled"] is True


def test_the_fold_survives_a_language_switch(launcher, real_app):
    pop = real_app._operator_popup
    pop.inbox.put(("profile", "stm"))
    pop.pump_once()
    real_app._apply_language("de")
    try:
        assert real_app._op_body.winfo_manager() == ""               # the rebuilt section is folded too
        assert "optische" in real_app.op_status_var.get()            # and its line says so, in German
        pop.inbox.put(("profile", "optical"))
        pop.pump_once()
        assert real_app._op_body.winfo_manager() == "grid"           # and unfolds when the station turns out optical
    finally:
        launcher._set_lang("en")


def test_end_to_end_with_the_real_poller_thread_against_a_fake_service(launcher, tk_root, tmp_path, plain_service):
    """The whole chain on real threads: a prompt appears on the service, the poller thread
    finds it, the Tk-thread pump opens the window, the operator's answer goes out as a POST
    from its own thread, and the window closes -- the service then lists nothing."""
    import tkinter as tk
    import time
    from tkinter import ttk

    state = {"open": [{"id": "op-1", "kind": "number", "title": "Read the power", "severity": "safety",
                       "number_min": 0, "number_max": 20, "unit": "mW", "status": "open", "answer": None,
                       "created_at": "2026-11-03T10:00:00+00:00"}], "answers": []}

    def route(method, path):
        if path == "/api/health":
            return 200, {"status": "ok", "station_profile": "optical"}    # only an optical station is listened to
        if method == "GET":
            return 200, {"prompts": state["open"], "seq": 1}
        state["answers"].append(path)
        state["open"] = []                      # answered: the service no longer lists it
        return 200, {"prompt_id": "op-1", "status": "answered"}

    plain_service.route = route
    app = FakeApp(tmp_path)
    app.win, app.tk, app.ttk = tk_root, tk, ttk
    sounds = []
    ctl = launcher.OperatorPopupController(
        app, api=launcher.OperatorApi(plain_service.server_address[1]),
        view_factory=lambda prompt, waiting: FakeView(prompt, waiting),
        play=lambda sev, **k: sounds.append(sev))
    _collect_tk_garbage()                        # nothing stale may be left for the poller thread to free
    ctl.start()

    def spin(until, seconds=8.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            tk_root.update()
            if until():
                return True
            time.sleep(0.02)
        return False

    try:
        assert spin(lambda: ctl.view is not None), "the poller never delivered the prompt to the window"
        assert ctl.view.prompt_id == "op-1" and sounds == ["safety"]
        ctl.submit("op-1", {"text": "12.5"}, "Ana")
        assert spin(lambda: ctl.view is None), "the window did not close after the answer"
        assert state["answers"] == ["/api/operator/prompts/op-1/answer"]
        assert not ctl.prompts.has("op-1")
        time.sleep(0.2)
        spin(lambda: False, seconds=0.5)
        assert ctl.view is None                  # the next poll lists nothing: nothing comes back
    finally:
        ctl.stop()
        if ctl._thread is not None:
            ctl._thread.join(10)                 # no thread of ours may outlive the Tk root
            assert not ctl._thread.is_alive()
