"""MAST MCP server against a fake MAST 7.0: images, ``_meta``, the observation gate, waiting.

The fake is a real ``http.server`` on a loopback port. Each test tells it what to
answer per route (the shapes of contract ``docs/v2/architecture/mast7_vista.md`` §3-§7)
and reads back every request it got. Tools run in-process through ``Toolbox.call_tool``;
the wire-level tests drive ``stdio_rpc.McpServer`` over in-memory streams, the way a
client would.
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (same bootstrap as test_wrap_skill_minimal) ──
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_MASTV2_ROOT = str(_REPO / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]
_SERVER_DIR = _REPO / "integrations" / "claude-code" / "server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from mast_mcp.config import load_config
from mast_mcp.resources import GuideResources
from mast_mcp.stdio_rpc import CallSignal, McpServer
from mast_mcp.tools import INSTRUCTIONS, VISTA_TEXT_LIMIT, Toolbox, client_meta

PREFIX = "/api/ext/v1"
# Not a decodable PNG, and it does not need to be: the bridge never decodes images.
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 64).decode("ascii")
PNG2_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(range(255, -1, -1)) * 64).decode("ascii")
LABEL = ('<visual kind="current" frame="t0007.s0" channel="Z" direction="forward" scale="2" '
         'size="768x768"/>')
CODEX_META = {
    "callId": "exec-16c50c8b", "itemId": "ctc_04fcc1e3703651b1", "progressToken": 1,
    "threadId": "01a135ab-c666",
    "x-codex-turn-metadata": {"session_id": "01a135ab-c666", "turn_id": "01a135ab-c6a4",
                              "model": "gpt-6-astra", "node_repl_disabled": False,
                              "sandbox": "windows_sandbox"},
}


def _observation(turn: int = 7, token: str | None = "K7QM-29XA", *, image: bool = True,
                 on_image: bool = False) -> dict:
    text = {"turn": turn, "action": "scan", "ok": True,
            "new_frames": [{"frame": f"t{turn:04d}.s0"}], "instrument": {"bias_v": 0.5},
            "elapsed_s": 12.5, "next_token": token}
    if on_image:
        text["token_on_image"] = True
    return {"obs_id": f"o{turn:06d}", "turn": turn, "frames": [f"t{turn:04d}.s0"],
            "text": json.dumps(text, separators=(",", ":")),
            "images": [{"label": LABEL, "mime": "image/png", "data_b64": PNG_B64}] if image else [],
            "next_token": token, "token_on_image": on_image}


def _reply(text: str, *images: str, is_error: bool = False) -> dict:
    return {"text": text, "is_error": is_error,
            "images": [{"label": f'<visual kind="view" index="{i}"/>', "mime": "image/png",
                        "data_b64": data} for i, data in enumerate(images, 1)]}


def _job_view(job_id: str = "j_000000000001", state: str = "succeeded") -> dict:
    view = {"job_id": job_id, "skill": "GetBias", "state": state, "terminal": state != "running",
            "elapsed_s": 0.4, "refused_by": None, "busy_holder": None, "abort": None}
    if state == "succeeded":
        view["result"] = {"success": True, "summary": "bias is 0.5 V"}
        view["recorded"] = {"v1": True, "v2_action_id": "a1"}
    return view


# ── fake MAST 7.0 ────────────────────────────────────────────────────────────
class FakeMast7:
    """Answers per ``(method, route)``: a ``(status, body)`` pair or a callable
    ``(body, query) -> (status, body)``. Unknown routes get the ext API's own 404."""

    def __init__(self):
        self.seen: list[dict] = []
        self.answers: dict[tuple[str, str], object] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def on(self, method: str, route: str, answer) -> None:
        self.answers[(method, route)] = answer

    def requests_to(self, method: str, route: str) -> list[dict]:
        return [r for r in self.seen if r["method"] == method and r["path"] == PREFIX + route]


def _handler(fake: FakeMast7):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status: int, obj) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-MAST-Ext-Version", "1")
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else None
            parts = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            fake.seen.append({"method": method, "path": parts.path, "query": query, "body": body,
                              "headers": {k.lower(): v for k, v in self.headers.items()}})
            answer = fake.answers.get((method, parts.path[len(PREFIX):]))
            if answer is None:
                self._send(404, {"error": "not_found",
                                 "detail": f"外部面没有这个端点：{parts.path}"})
                return
            status, obj = answer(body, query) if callable(answer) else answer
            self._send(status, obj)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")
    return Handler


@pytest.fixture()
def fake():
    server = FakeMast7()
    yield server
    server.close()


def _box(url: str, **env: str) -> Toolbox:
    return Toolbox(load_config({"MAST_URL": url, "MAST_SESSION": "feed0001", **env}))


def _call(box: Toolbox, tool: str, /, *, meta: dict | None = None, signal=None, **arguments):
    return box.call_tool(tool, arguments, signal or CallSignal(threading.Event()), meta=meta,
                         client_info={"name": "codex-mcp-client", "version": "0.153.4"})


# ── observe and act ──────────────────────────────────────────────────────────
def test_observe_gives_the_observation_text_and_its_image(fake):
    fake.on("POST", "/vista/observe", (200, {"observation": _observation(3)}))
    out = _call(_box(fake.url), "mast_observe")
    assert not out.is_error, out.text
    first, rest = out.text.split("\n", 1)
    assert first.startswith("Observation (turn 3).") and "image()" in first
    assert rest == _observation(3)["text"]                       # verbatim, next_token included
    assert out.images == [{"label": LABEL, "mime": "image/png", "data_b64": PNG_B64}]
    body = fake.requests_to("POST", "/vista/observe")[0]["body"]
    assert body == {"client_meta": {"client": {"name": "codex-mcp-client", "version": "0.153.4"}}}


def test_strict_mode_says_the_token_is_on_the_image(fake):
    fake.on("POST", "/vista/observe", (200, {"observation": _observation(3, None, on_image=True)}))
    out = _call(_box(fake.url), "mast_observe")
    assert "printed on the image" in out.text.splitlines()[0] and len(out.images) == 1


def test_an_act_that_finished_returns_its_observation(fake):
    fake.on("POST", "/vista/act", (200, {"kind": "act", "state": "done", "job_id": "j_7",
                                         "turn": 7, "ok": True, "observation": _observation(7)}))
    out = _call(_box(fake.url), "mast_act", meta=CODEX_META, action="scan",
                params={"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 20}, after=" k7qm-29xa ",
                note="expect terraces")
    assert not out.is_error, out.text
    assert out.text.startswith("Act scan (job j_7, turn 7): done.")
    assert out.text.endswith(_observation(7)["text"]) and len(out.images) == 1
    body = fake.requests_to("POST", "/vista/act")[0]["body"]
    assert body["action"] == "scan" and body["params"]["size_nm"] == 20
    assert body["after"] == "K7QM-29XA", "a token read off an image is normalized, not refused"
    assert body["request_id"].startswith("mcp-feed0001-") and body["note"] == "expect terraces"
    assert body["wait_s"] == 45
    assert body["client_meta"]["item_id"] == "ctc_04fcc1e3703651b1"


def test_an_act_without_a_token_is_still_sent_so_mast_can_hand_one_out(fake):
    fake.on("POST", "/vista/act", (409, {"error": "observe_first", "detail": "no token",
                                         "observation": _observation(4)}))
    out = _call(_box(fake.url), "mast_act", action="withdraw", params={})
    assert "after" not in fake.requests_to("POST", "/vista/act")[0]["body"]
    assert not out.is_error and len(out.images) == 1
    assert out.text.startswith("Refused (observe_first); nothing was executed: no token.")


def test_a_refusal_shows_the_latest_observation_with_its_image(fake):
    fake.on("POST", "/vista/act", (409, {"error": "observe_first",
                                         "detail": "token K7QM-29XA was superseded",
                                         "observation": _observation(8, "PQ34-RT67")}))
    out = _call(_box(fake.url), "mast_act", action="set_bias", params={"bias_v": 0.2},
                after="K7QM-29XA")
    # Not an error result: Claude Code shows no images of an error result, and looking at
    # this observation is the way back (in strict mode its token is on the image only).
    assert not out.is_error
    first, marker, rest = out.text.split("\n", 2)
    assert first == ("Refused (observe_first); nothing was executed: token K7QM-29XA was "
                     "superseded. The observation image follows: look at it before the next act "
                     "(Codex Code Mode: image()).")
    assert json.loads(marker) == {"refused": "observe_first", "executed": False}
    assert rest == _observation(8, "PQ34-RT67")["text"]
    assert out.images == [{"label": LABEL, "mime": "image/png", "data_b64": PNG_B64}]
    assert PNG_B64[:200] not in out.text


def test_a_strict_mode_refusal_points_at_the_token_on_its_image(fake):
    fake.on("POST", "/vista/act", (409, {"error": "observe_first", "detail": "token spent",
                                         "observation": _observation(8, None, on_image=True)}))
    out = _call(_box(fake.url), "mast_act", action="wait", params={"seconds": 1}, after="AAAA-CCCC")
    assert not out.is_error and len(out.images) == 1
    assert "printed on the image" in out.text.splitlines()[0]


def test_an_observe_first_without_an_observation_stays_an_error(fake):
    fake.on("POST", "/vista/act", (409, {"error": "observe_first", "detail": "no token"}))
    out = _call(_box(fake.url), "mast_act", action="wait", params={"seconds": 1}, after="AAAA-CCCC")
    assert out.is_error and out.images == []
    assert out.text == "Refused (observe_first); nothing was executed: no token."


@pytest.mark.parametrize("code, extra, must", [
    ("act_in_flight", {"job_id": "j_9"},
     'mast_wait(conditions=[{"type": "job", "job_id": "j_9"}])'),
    ("same_code_cell", {}, "new code cell"),
    ("checkpoint_requested", {}, "mast_write_working"),
    ("session_closed", {}, "do not act again"),
])
def test_refusals_without_an_observation_say_what_to_do(fake, code, extra, must):
    fake.on("POST", "/vista/act", (409, {"error": code, "detail": "", **extra}))
    out = _call(_box(fake.url), "mast_act", action="wait", params={"seconds": 5}, after="AAAA-CCCC")
    assert out.is_error and out.images == []
    assert out.text.startswith(f"Refused ({code}); nothing was executed. ") and must in out.text


def test_mast_explains_a_refusal_in_its_own_words(fake):
    detail = ("One action per code cell: an action from this code cell was already executed. "
              "Nothing was executed now.")
    fake.on("POST", "/vista/act", (409, {"error": "same_code_cell", "detail": detail}))
    out = _call(_box(fake.url), "mast_act", action="wait", params={"seconds": 5}, after="AAAA-CCCC")
    assert out.is_error and out.text == f"Refused (same_code_cell); nothing was executed: {detail}"


def test_an_act_without_an_observation_says_to_observe(fake):
    fake.on("POST", "/vista/act", (200, {"kind": "act", "state": "done", "job_id": "j_4", "turn": 4,
                                         "job_state": "crashed", "ok": False, "observation": None,
                                         "error": "crashed"}))
    out = _call(_box(fake.url), "mast_act", action="wait", params={"seconds": 1}, after="AAAA-CCCC")
    assert out.is_error and out.images == []
    first = out.text.splitlines()[0]
    assert first.startswith("Act wait (job j_4, turn 4): done (crashed: the act crashed inside "
                            "MAST")
    assert first.endswith(", FAILED, but MAST recorded no observation for it: call mast_observe "
                          "before the next act.")


def test_a_running_act_points_at_mast_wait(fake):
    fake.on("POST", "/vista/act", (202, {"kind": "act", "state": "running", "job_id": "j_2",
                                         "turn": 9, "hint": "mast_wait(job_id)"}))
    out = _call(_box(fake.url), "mast_act", action="approach", params={}, after="AAAA-CCCC",
                wait_s=5)
    assert not out.is_error and out.images == []
    assert out.text.startswith("Act approach (job j_2, turn 9): still running.")
    assert 'mast_wait(conditions=[{"type": "job", "job_id": "j_2"}])' in out.text
    assert fake.requests_to("POST", "/vista/act")[0]["body"]["wait_s"] == 5


def test_a_failed_act_is_an_error_that_still_shows_its_observation(fake):
    fake.on("POST", "/vista/act", (200, {"kind": "act", "state": "done", "job_id": "j_3", "turn": 3,
                                         "ok": False, "observation": _observation(3)}))
    out = _call(_box(fake.url), "mast_act", action="move_tip",
                params={"to": {"x_nm": 1, "y_nm": 2}}, after="AAAA-CCCC")
    first = out.text.splitlines()[0]
    assert out.is_error and "FAILED" in first and len(out.images) == 1
    # an error result's image may not be shown: the text says how to get it anyway
    assert "do not show the images of an error result" in first and "mast_observe" in first


def test_an_error_without_an_image_does_not_mention_images(fake):
    fake.on("POST", "/vista/act", (200, {"kind": "act", "state": "done", "job_id": "j_3", "turn": 3,
                                         "ok": False, "observation": _observation(3, image=False)}))
    out = _call(_box(fake.url), "mast_act", action="wait", params={"seconds": 1}, after="AAAA-CCCC")
    assert out.is_error and out.images == [] and "image" not in out.text.splitlines()[0]


def test_an_act_refused_for_a_busy_instrument_says_so(fake):
    fake.on("POST", "/vista/act", (200, {"kind": "act", "state": "done", "job_id": "j_6", "turn": 6,
                                         "job_state": "refused_busy", "ok": False,
                                         "observation": _observation(6)}))
    out = _call(_box(fake.url), "mast_act", action="scan", params={}, after="AAAA-CCCC")
    first = out.text.splitlines()[0]
    assert out.is_error and len(out.images) == 1
    assert first.startswith("Act scan (job j_6, turn 6): done (refused_busy: someone else holds "
                            "the instrument and the action did not run")


def test_other_errors_never_put_image_data_into_the_text(fake):
    fake.on("POST", "/vista/act", (500, {"error": "internal", "detail": "boom",
                                         "observation": _observation(5)}))
    out = _call(_box(fake.url), "mast_act", action="wait", params={"seconds": 1}, after="AAAA-CCCC")
    assert out.is_error and "boom" in out.text
    assert PNG_B64[:200] not in out.text and "base64 characters" in out.text


# ── mast_run through /vista/act ──────────────────────────────────────────────
def test_run_of_a_plain_skill_renders_the_job_view(fake):
    fake.on("POST", "/vista/act", (200, {"kind": "job", "job": _job_view()}))
    out = _call(_box(fake.url), "mast_run", meta={"claudecode/toolUseId": "toolu_01"},
                skill="GetBias", params={"channel": 1}, note="check")
    assert not out.is_error, out.text
    assert "succeeded" in out.text.splitlines()[0] and "bias is 0.5 V" in out.text
    body = fake.requests_to("POST", "/vista/act")[0]["body"]
    assert body["skill"] == "GetBias" and body["skill_params"] == {"channel": 1}
    assert "params" not in body and "after" not in body and body["note"] == "check"
    assert body["wait_s"] == 20 and body["request_id"].startswith("mcp-feed0001-")
    assert body["client_meta"]["tool_use_id"] == "toolu_01"
    assert not fake.requests_to("POST", "/jobs")


def test_run_of_an_instrument_skill_renders_the_act(fake):
    fake.on("POST", "/vista/act", (200, {"kind": "act", "state": "done", "job_id": "j_5", "turn": 5,
                                         "ok": True, "observation": _observation(5)}))
    out = _call(_box(fake.url), "mast_run", skill="ScanFrame", params={}, after="PQ34-RT67")
    assert not out.is_error and out.text.startswith("Act ScanFrame (job j_5, turn 5): done.")
    assert len(out.images) == 1
    assert fake.requests_to("POST", "/vista/act")[0]["body"]["after"] == "PQ34-RT67"


def test_run_keeps_polling_a_plain_job_that_outlives_the_server_wait(fake):
    fake.on("POST", "/vista/act", (200, {"kind": "job", "job": _job_view(state="running")}))
    fake.on("GET", "/jobs/j_000000000001", (200, _job_view()))
    out = _call(_box(fake.url), "mast_run", skill="GetBias", wait_s=10)
    assert not out.is_error and "succeeded" in out.text.splitlines()[0]


def test_run_falls_back_to_jobs_on_a_mast_older_than_7(fake):
    fake.on("POST", "/jobs", (202, _job_view(state="running")))
    fake.on("GET", "/jobs/j_000000000001", (200, _job_view()))
    out = _call(_box(fake.url), "mast_run", skill="GetBias", params={"channel": 1}, wait_s=5,
                after="AAAA-CCCC")
    assert not out.is_error and "succeeded" in out.text.splitlines()[0]
    act = fake.requests_to("POST", "/vista/act")[0]["body"]
    job = fake.requests_to("POST", "/jobs")[0]["body"]
    assert job == {"skill": "GetBias", "params": {"channel": 1}, "request_id": act["request_id"]}


def test_a_contract_404_is_not_taken_for_an_older_mast(fake):
    fake.on("POST", "/vista/act", (404, {"error": "unknown_skill", "detail": "no skill Nope"}))
    out = _call(_box(fake.url), "mast_run", skill="Nope")
    assert out.is_error and "no skill Nope" in out.text
    assert not fake.requests_to("POST", "/jobs")


# ── mast_wait ────────────────────────────────────────────────────────────────
def _wait_answers(fake: FakeMast7, *answers: dict) -> None:
    queue = list(answers)
    fake.on("POST", "/wait", lambda body, query: (200, queue.pop(0)))


def test_wait_sends_the_last_cursor_when_the_model_gives_none(fake):
    _wait_answers(fake,
                  {"fired": [], "timed_out": True, "waited_s": 1.0, "cursor": "c1"},
                  {"fired": [{"index": 0, "type": "frame", "event": {"fid": "t0007.s0"}}],
                   "timed_out": False, "waited_s": 0.2, "cursor": "c2"},
                  {"fired": [], "timed_out": True, "waited_s": 1.0, "cursor": "c3"},
                  {"fired": [], "timed_out": True, "waited_s": 1.0, "cursor": "c4"})
    box = _box(fake.url)
    first = _call(box, "mast_wait", conditions=[{"type": "frame"}], timeout_s=1)
    assert not first.is_error and "timed out" in first.text.splitlines()[0]
    _call(box, "mast_wait", conditions=[{"type": "frame"}])
    _call(box, "mast_wait", conditions=[{"type": "frame"}], since="c0")
    _call(box, "mast_wait", conditions={"type": "timer", "seconds": 1})
    bodies = [r["body"] for r in fake.requests_to("POST", "/wait")]
    assert "since" not in bodies[0] and bodies[0]["timeout_s"] == 1
    assert bodies[1]["since"] == "c1" and bodies[1]["timeout_s"] == 600
    assert bodies[2]["since"] == "c0", "an explicit since wins"
    assert bodies[3]["since"] == "c3"
    assert bodies[3]["conditions"] == [{"type": "timer", "seconds": 1}]
    assert all(b["client_meta"]["client"]["name"] == "codex-mcp-client" for b in bodies)


def test_a_cancelled_wait_does_not_move_the_cursor(fake):
    _wait_answers(fake, {"fired": [], "timed_out": True, "waited_s": 1.0, "cursor": "c1"},
                  {"fired": [], "timed_out": True, "waited_s": 1.0, "cursor": "c2"},
                  {"fired": [], "timed_out": True, "waited_s": 1.0, "cursor": "c3"})
    box = _box(fake.url)
    _call(box, "mast_wait", conditions=[{"type": "frame"}])
    withdrawn = CallSignal(threading.Event())
    withdrawn.cancelled.set()                    # the client gave up; nobody reads this answer
    _call(box, "mast_wait", signal=withdrawn, conditions=[{"type": "frame"}])
    _call(box, "mast_wait", conditions=[{"type": "frame"}])
    assert [r["body"].get("since") for r in fake.requests_to("POST", "/wait")] == [None, "c1", "c1"]


def test_a_wait_that_brought_an_observation_shows_it(fake):
    _wait_answers(fake, {"fired": [{"index": 0, "type": "job", "event": {"job_id": "j_2"}}],
                         "timed_out": False, "waited_s": 31.5, "cursor": "c9",
                         "observation": _observation(9)})
    out = _call(_box(fake.url), "mast_wait", conditions=[{"type": "job", "job_id": "j_2"}])
    assert not out.is_error
    assert out.text.startswith("mast_wait: job (condition 0) fired after 32 s.")
    assert out.text.endswith(_observation(9)["text"]) and len(out.images) == 1
    assert '"cursor":"c9"' in out.text and PNG_B64[:200] not in out.text


@pytest.mark.parametrize("conditions", [None, [], "not json", [{"job_id": "j_1"}], [3]])
def test_wait_needs_conditions_with_a_type(fake, conditions):
    out = _call(_box(fake.url), "mast_wait", conditions=conditions)
    assert out.is_error and "conditions" in out.text and fake.seen == []


class _RecordingClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def call(self, method, path, *, query=None, body=None, timeout=30.0):
        self.calls.append((method, path, body, timeout))
        return {"fired": [], "timed_out": True, "waited_s": body["timeout_s"], "cursor": "c"}


@pytest.mark.parametrize("asked, sent", [(840, 840), (5000, 840), (10, 10)])
def test_wait_gets_its_own_time_budget(asked, sent):
    client = _RecordingClient()
    box = Toolbox(load_config({"MAST_URL": "http://127.0.0.1:1"}), client=client)
    clock = [1000.0]
    out = box.call_tool("mast_wait", {"conditions": [{"type": "frame"}], "timeout_s": asked},
                        CallSignal(threading.Event()), clock=lambda: clock[0])
    assert not out.is_error, out.text
    method, path, body, timeout = client.calls[0]
    assert (method, path, body["timeout_s"]) == ("POST", "/wait", sent)
    assert timeout == sent + 30, "the read timeout must outlast the server-side wait"


# ── archive, measure, notes, history ─────────────────────────────────────────
def test_archive_tools_forward_the_arguments_and_pass_the_reply_through(fake):
    reply_text = '{"views":[{"index":1,"label":"step"},{"index":2,"label":"atoms"}]}'
    fake.on("POST", "/vista/inspect", (200, _reply(reply_text, PNG_B64, PNG2_B64)))
    args = {"question": "Is the step edge straight?",
            "views": [{"label": "step", "frame": "t0007.s0", "region": {"x": 0, "y": 0, "width": 64,
                                                                         "height": 64}},
                      {"label": "atoms", "frame": "t0007.s0", "flatten": "line"}]}
    out = _call(_box(fake.url), "mast_inspect", meta=CODEX_META, **args)
    assert not out.is_error and out.text == reply_text
    assert [im["data_b64"] for im in out.images] == [PNG_B64, PNG2_B64]
    assert [im["label"] for im in out.images] == ['<visual kind="view" index="1"/>',
                                                  '<visual kind="view" index="2"/>']
    body = fake.requests_to("POST", "/vista/inspect")[0]["body"]
    assert {k: v for k, v in body.items() if k != "client_meta"} == args
    assert body["client_meta"]["item_id"] == CODEX_META["itemId"]


@pytest.mark.parametrize("tool", ["line_profile", "fft_peaks", "detect_blobs", "diff", "stats"])
def test_measure_tools_post_to_their_endpoint(fake, tool):
    fake.on("POST", f"/vista/measure/{tool}", (200, _reply('{"instrument_unchanged":true}')))
    out = _call(_box(fake.url), f"mast_{tool}", frame="t0001.s0")
    assert not out.is_error and out.text == '{"instrument_unchanged":true}'
    assert fake.requests_to("POST", f"/vista/measure/{tool}")[0]["body"]["frame"] == "t0001.s0"


def test_a_vista_error_reply_is_a_tool_error_with_its_text(fake):
    fake.on("POST", "/vista/read_values", (200, _reply('{"error":"read_values: views must be a '
                                                       'non-empty list"}', is_error=True)))
    out = _call(_box(fake.url), "mast_read_values", question="z?", views=[])
    assert out.is_error and out.text == '{"error":"read_values: views must be a non-empty list"}'
    fake.on("POST", "/vista/measure/stats", (422, _reply('{"error":"stats: frame t9 unknown"}',
                                                         is_error=True)))
    out = _call(_box(fake.url), "mast_stats", frame="t0009.s0")
    assert out.is_error and out.text == '{"error":"stats: frame t9 unknown"}'


def test_notes_are_read_with_get_and_written_with_put(fake):
    fake.on("GET", "/vista/notes/guide", (200, _reply('{"content":"No reliable model yet.\\n"}')))
    fake.on("PUT", "/vista/notes/working", (200, _reply('{"saved":"WORKING.md","chars":9}')))
    box = _box(fake.url)
    out = _call(box, "mast_read_guide")
    assert not out.is_error and "No reliable model yet." in out.text
    out = _call(box, "mast_write_working", content="Plan: 1.\n")
    assert not out.is_error and out.text == '{"saved":"WORKING.md","chars":9}'
    body = fake.requests_to("PUT", "/vista/notes/working")[0]["body"]
    assert body["content"] == "Plan: 1.\n" and "client_meta" in body
    out = _call(box, "mast_write_guide", content="   ")
    assert out.is_error and "content" in out.text
    assert not fake.requests_to("PUT", "/vista/notes/guide")


def test_history_passes_its_range_as_a_query(fake):
    fake.on("GET", "/vista/history", (200, _reply('{"view":"events","n":0,"events":[]}')))
    out = _call(_box(fake.url), "mast_history", view="events", start_turn=3, end_turn=9, limit=500)
    assert not out.is_error
    assert fake.requests_to("GET", "/vista/history")[0]["query"] == {
        "view": "events", "start_turn": "3", "end_turn": "9", "limit": "128"}


# ── detectors, literature ────────────────────────────────────────────────────
def test_detector_operations_map_onto_the_endpoints(fake):
    det = {"id": "d1", "name": "step-finder", "enabled": True}
    fake.on("POST", "/detectors", (201, det))
    fake.on("GET", "/detectors", (200, {"detectors": [det]}))
    fake.on("GET", "/detectors/d1", (200, {**det, "hits": []}))
    fake.on("POST", "/detectors/d1/enable",
            lambda body, q: (200, {**det, "enabled": body["enabled"]}))
    fake.on("DELETE", "/detectors/d1", (200, {"deleted": "d1"}))
    fake.on("POST", "/detectors/d1/test", (200, {"result": {"hit": True, "message": "step"}}))
    box = _box(fake.url)
    code = "def detect(frame):\n    return True\n"
    out = _call(box, "mast_detector_create", name="step-finder", code=code, applies_to=["scan"],
                max_runtime_s=500)
    assert not out.is_error and out.text.startswith('Detector d1 "step-finder" created, enabled.')
    assert fake.requests_to("POST", "/detectors")[0]["body"] == {
        "name": "step-finder", "code": code, "applies_to": ["scan"], "max_runtime_s": 120}
    assert "1 detector(s)" in _call(box, "mast_detectors", op="list").text
    assert "enabled" in _call(box, "mast_detectors", op="get", detector_id="d1").text
    assert "disabled" in _call(box, "mast_detectors", op="disable", detector_id="d1").text
    assert "deleted" in _call(box, "mast_detectors", op="delete", detector_id="d1").text
    # MAST refuses write requests without a JSON body, DELETE included
    assert fake.requests_to("DELETE", "/detectors/d1")[0]["body"] == {}
    out = _call(box, "mast_detectors", op="test", detector_id="d1", frame="t0007.s0")
    assert not out.is_error and "a hit" in out.text
    enables = fake.requests_to("POST", "/detectors/d1/enable")
    assert [r["body"] for r in enables] == [{"enabled": False}]
    assert fake.requests_to("POST", "/detectors/d1/test")[0]["body"] == {"frame": "t0007.s0"}
    out = _call(box, "mast_detectors", op="test", detector_id="d1")
    assert out.is_error and "frame" in out.text


def test_literature_search_posts_the_query(fake):
    fake.on("POST", "/literature/search", (200, {"hits": [
        {"title": "Surface states on Cu(111)", "year": 1993, "doi": "10.1/x"}], "count": 1,
        "degraded": True}))
    out = _call(_box(fake.url), "mast_literature_search", query="Cu(111) surface state", k=99,
                material="Cu(111)")
    assert not out.is_error
    assert out.text.startswith('1 paper(s) for "Cu(111) surface state": Surface states on Cu(111) '
                               "(1993). Degraded")
    assert fake.requests_to("POST", "/literature/search")[0]["body"] == {
        "query": "Cu(111) surface state", "k": 50, "material": "Cu(111)"}


# ── the wire: content blocks, _meta, clientInfo ──────────────────────────────
def _serve(box: Toolbox, messages: list[dict], tmp_path: Path) -> dict:
    data = b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in messages)
    out = io.BytesIO()
    McpServer(server_info={"name": "mast", "version": "test"}, instructions=INSTRUCTIONS,
              tools=box, resources=GuideResources(str(tmp_path / "no-guide")),
              instream=io.BytesIO(data), outstream=out).serve()
    answers = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines() if line]
    return {a.get("id"): a for a in answers}


def _initialize(client: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": client}}


def test_images_reach_the_client_as_label_and_image_blocks(fake, tmp_path):
    fake.on("POST", "/vista/inspect", (200, _reply('{"views":2}', PNG_B64, PNG2_B64)))
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "mast_inspect", "_meta": CODEX_META,
                       "arguments": {"question": "q",
                                     "views": [{"label": "a", "frame": "t0001.s0"}]}}}
    by_id = _serve(_box(fake.url), [_initialize({"name": "codex-mcp-client", "title": "Codex",
                                                 "version": "0.153.4"}), call], tmp_path)
    result = by_id[2]["result"]
    assert set(result) == {"content", "isError"}, "never structuredContent: Codex drops images then"
    assert result["isError"] is False
    assert result["content"] == [
        {"type": "text", "text": '{"views":2}'},
        {"type": "text", "text": '<visual kind="view" index="1"/>'},
        {"type": "image", "data": PNG_B64, "mimeType": "image/png"},
        {"type": "text", "text": '<visual kind="view" index="2"/>'},
        {"type": "image", "data": PNG2_B64, "mimeType": "image/png"},
    ]
    sent = fake.requests_to("POST", "/vista/inspect")[0]["body"]["client_meta"]
    assert sent == {"item_id": "ctc_04fcc1e3703651b1", "call_id": "exec-16c50c8b",
                    "turn": CODEX_META["x-codex-turn-metadata"],
                    "client": {"name": "codex-mcp-client", "title": "Codex", "version": "0.153.4"}}


def test_claude_codes_tool_use_id_is_forwarded(fake, tmp_path):
    fake.on("POST", "/vista/observe", (200, {"observation": _observation(1, image=False)}))
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "mast_observe", "arguments": {},
                       "_meta": {"claudecode/toolUseId": "toolu_01ABC", "progressToken": 7}}}
    by_id = _serve(_box(fake.url), [_initialize({"name": "claude-code", "version": "2.1"}), call],
                   tmp_path)
    assert [c["type"] for c in by_id[2]["result"]["content"]] == ["text"]
    assert fake.requests_to("POST", "/vista/observe")[0]["body"]["client_meta"] == {
        "tool_use_id": "toolu_01ABC", "client": {"name": "claude-code", "version": "2.1"}}


def test_text_is_clipped_but_image_data_never_is(fake, tmp_path):
    fake.on("POST", "/vista/read_values", (200, _reply("v" * (VISTA_TEXT_LIMIT + 5000), PNG_B64)))
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "mast_read_values", "arguments": {"question": "q", "views": []}}}
    by_id = _serve(_box(fake.url), [_initialize({"name": "t"}), call], tmp_path)
    content = by_id[2]["result"]["content"]
    assert len(content[0]["text"]) <= VISTA_TEXT_LIMIT and "truncated" in content[0]["text"]
    assert content[2] == {"type": "image", "data": PNG_B64, "mimeType": "image/png"}


def test_old_text_tools_still_answer_one_text_block(fake, tmp_path):
    fake.on("GET", "/status", (200, {"mode": "SAFE", "abort": {"set": False},
                                     "lock": {"held": False}}))
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "mast_status", "arguments": {}}}
    result = _serve(_box(fake.url), [_initialize({"name": "t"}), call], tmp_path)[2]["result"]
    assert [c["type"] for c in result["content"]] == ["text"] and result["isError"] is False


def test_images_without_data_or_in_odd_formats_are_left_out_and_said_so(fake):
    reply = _reply("{}", PNG_B64)
    reply["images"] += [{"label": "x", "mime": "image/svg+xml", "data_b64": "PHN2Zz4="},
                        {"label": "y", "mime": "image/png", "data_b64": ""}]
    fake.on("POST", "/vista/inspect", (200, reply))
    out = _call(_box(fake.url), "mast_inspect", question="q",
                views=[{"label": "a", "frame": "t0001.s0"}])
    assert len(out.images) == 1 and "2 image(s) of this answer could not be shown" in out.text


# ── client_meta ──────────────────────────────────────────────────────────────
def test_client_meta_drops_empty_values_and_clips_long_ones():
    turn = {"empty": "", "none": None, "flag": False, "nested": {"deep": ["x" * 500]}}
    turn.update({f"k{i}": i for i in range(50)})
    meta = client_meta({"itemId": "i" * 1000, "callId": 12345, "x-codex-turn-metadata": turn,
                        "claudecode/toolUseId": "  ", "progressToken": 3},
                       {"name": "codex", "version": "", "extra": "ignored"})
    assert meta["item_id"] == "i" * 200 and meta["call_id"] == "12345"
    assert list(meta["turn"])[:3] == ["flag", "nested", "k0"] and len(meta["turn"]) == 30
    assert meta["turn"]["flag"] is False and len(meta["turn"]["nested"]) == 200
    assert "tool_use_id" not in meta and meta["client"] == {"name": "codex"}
    assert client_meta(None, None) == {}
    assert client_meta({"x-codex-turn-metadata": {"flag": False, "nested": {"a": 1}}}, None) == {
        "turn": {"flag": False, "nested": '{"a":1}'}}
