"""MAST MCP server against a fake MAST: requests, polling, error shaping, downloads.

The fake is a real ``http.server`` on a loopback port that implements the part
of the ``/api/ext/v1`` contract these tests need, and records every request it
sees. The tools run in-process through ``Toolbox.call_tool``, exactly the call
``stdio_rpc`` makes.

The fake is a MAST 6.5: it has no ``/vista/*`` routes, so ``mast_run`` falls back
to ``POST /jobs`` as it does against a MAST older than 7.0. The 7.0 routes are
tested in ``test_mast_mcp_vista.py``.
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
import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from mast_mcp.config import load_config
from mast_mcp.stdio_rpc import CallSignal
from mast_mcp.tools import CallContext, Toolbox, job_summary, local_fetch_path

PREFIX = "/api/ext/v1"
RAW_BYTES = bytes(range(256)) * 300            # 76.8 kB: more than one download chunk
FRAME_META = {"channel": "Z", "pixels": [4, 4], "unit": "m", "note": "扫描"}


# ── fake MAST ────────────────────────────────────────────────────────────────
class FakeMast:
    def __init__(self):
        self.seen: list[dict] = []
        self.jobs: dict[str, dict] = {}
        self.by_request_id: dict[str, str] = {}
        self.lock = threading.Lock()
        self.job_duration = 0.6
        self.login: tuple[str, str] | None = None
        self.html_paths: set[str] = set()
        self.bare_404_paths: set[str] = set()
        self.drop_next_post: str | None = None     # route whose next POST loses its answer
        self.busy = False
        self.scope = {"experiment": None, "sample": None,
                      "recording": {"v1": False, "v2": False, "note": "no active experiment"}}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def requests_to(self, method: str, path: str) -> list[dict]:
        return [r for r in self.seen if r["method"] == method and r["path"] == PREFIX + path]

    # job model: done after job_duration seconds; skill "Busy" is refused at once
    def create_job(self, body: dict) -> tuple[int, dict]:
        with self.lock:
            if body["skill"] == "Disabled":
                return 422, {"error": "skill_disabled",
                             "detail": "Disabled is switched off on this machine"}
            rid = body.get("request_id")
            if rid in self.by_request_id:
                first = self.jobs[self.by_request_id[rid]]["body"]
                if (first["skill"], first.get("params")) != (body["skill"], body.get("params")):
                    return 409, {"error": "request_id_conflict",
                                 "detail": f"request_id {rid!r} belongs to another job"}
                view = dict(self.view(self.by_request_id[rid]), idempotent_replay=True)
                return 200, view
            job_id = f"j_{len(self.jobs) + 1:012d}"
            self.jobs[job_id] = {"body": body, "created": time.monotonic()}
            self.by_request_id[rid] = job_id
            return 202, self.view(job_id)

    def view(self, job_id: str) -> dict:
        job = self.jobs[job_id]
        body = job["body"]
        base = {"job_id": job_id, "skill": body["skill"], "params": body.get("params", {}),
                "actor": "claude-code", "request_id": body.get("request_id"),
                "run_id": f"ext-{job_id}", "refused_by": None, "busy_holder": None,
                "abort": None, "cancel_requested": False, "cancel_reason": ""}
        if body["skill"] == "Busy":
            return dict(base, state="refused_busy", terminal=True, elapsed_s=0.0,
                        busy_holder={"owner": "operator GUI", "skill": "ScanFrame", "held_s": 42},
                        result={"success": False, "error": "instrument_busy"},
                        recorded={"v1": False, "v2_action_id": None})
        age = time.monotonic() - job["created"]
        if age < self.job_duration:
            return dict(base, state="running", terminal=False, elapsed_s=round(age, 2),
                        result=None, recorded=None)
        return dict(base, state="succeeded", terminal=True, elapsed_s=self.job_duration,
                    result={"success": True, "summary": "bias is 0.5 V", "data": {"bias_v": 0.5}},
                    recorded={"v1": True, "v2_action_id": "a1", "experiment_id": "e1"})


def _handler(fake: FakeMast):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep pytest output clean
            pass

        def _send(self, status: int, obj=None, *, body: bytes | None = None,
                  ctype: str = "application/json", headers: dict | None = None,
                  ext_header: bool = True):
            data = body if body is not None else json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if ext_header:
                self.send_header("X-MAST-Ext-Version", "1")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._route("GET", None)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            self._route("POST", json.loads(raw.decode("utf-8")) if raw else None)

        def _route(self, method: str, body):
            parts = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            path = parts.path
            fake.seen.append({"method": method, "path": path, "query": query, "body": body,
                              "headers": {k.lower(): v for k, v in self.headers.items()}})
            if fake.login is not None:
                expect = "Basic " + base64.b64encode(
                    f"{fake.login[0]}:{fake.login[1]}".encode("utf-8")).decode("ascii")
                if self.headers.get("Authorization") != expect:
                    self._send(401, body=b"", ctype="text/plain")
                    return
            if path in fake.html_paths:
                self._send(200, body=b"<!doctype html><html><body><div id=root></div></body></html>",
                           ctype="text/html; charset=utf-8", ext_header=False)
                return
            if path in fake.bare_404_paths:
                self._send(404, {"detail": "Not Found"}, ext_header=False)
                return
            if not path.startswith(PREFIX):
                self._send(404, {"detail": "Not Found"}, ext_header=False)
                return
            route = path[len(PREFIX):]
            if method == "POST" and fake.drop_next_post == route:
                fake.drop_next_post = None
                if route == "/jobs":
                    fake.create_job(body)          # MAST got it; the answer is lost
                self.close_connection = True
                return
            if method == "GET" and route == "/status":
                self._send(200, {"mode": "SAFE", "abort": {"set": False, "emergency": False},
                                 "lock": {"held": False}, "jobs": {"running": 0, "max": 2},
                                 "degraded": []})
            elif method == "POST" and route == "/jobs":
                status, view = fake.create_job(body)
                self._send(status, view)
            elif method == "GET" and route.startswith("/jobs/"):
                job_id = route[len("/jobs/"):]
                if job_id not in fake.jobs:
                    self._send(404, {"error": "unknown_job", "detail": f"no job {job_id}"})
                    return
                end = time.monotonic() + float(query.get("wait_s", 0))
                view = fake.view(job_id)
                while not view["terminal"] and time.monotonic() < end:
                    time.sleep(0.05)
                    view = fake.view(job_id)
                self._send(200, view)
            elif method == "GET" and route == "/scope":
                self._send(200, fake.scope)
            elif method == "POST" and route == "/scope":
                if fake.busy and not body.get("force"):
                    self._send(409, {"error": "instrument_busy",
                                     "detail": "instrument held by operator GUI (ScanFrame)",
                                     "holder": {"held": True, "owner": "operator GUI"}})
                    return
                self._send(200, {**fake.scope, "changed": ["experiment created->e7"],
                                 "warnings": ["forced while busy"] if fake.busy else []})
            elif method == "GET" and route == "/briefing":
                self._send(200, {
                    "generated_at": "2026-01-01T00:00:00", "actor": "ext:claude-code",
                    "sections": {"status": {"ok": True, "text": "- mode: safe",
                                            "data": {"mode": "safe"}},
                                 "tip": {"ok": False, "error": "RuntimeError: no tip"}},
                    "degraded": [{"section": "tip", "reason": "RuntimeError: no tip"}],
                    "text": "## status\n- mode: safe\n\n## tip\n(unreadable)"})
            elif method == "POST" and route == "/notes":
                self._send(200, {"ok": True, "namespace": "global", "path": "ext/claude-code/ab12",
                                 "id": None, "author": "ext:claude-code",
                                 "warnings": ["no current experiment: saved to global"]})
            elif method == "POST" and route == "/requests":
                self._send(200, {"ok": True, "request": {
                    "id": "r-1", "agent_id": "ext:claude-code", "status": "pending",
                    "message": body.get("message"), "kind": body.get("kind", "question")}})
            elif method == "GET" and route == "/data/file" and query.get("path") == "stall.sxm":
                # headers and a first slice of the body, then silence
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(10 * len(RAW_BYTES)))
                self.end_headers()
                self.wfile.write(RAW_BYTES[:1000])
                self.wfile.flush()
                time.sleep(8)
                self.close_connection = True
            elif method == "GET" and route == "/data/file":
                self._send(200, body=RAW_BYTES, ctype="application/octet-stream")
            elif method == "GET" and route == "/data/frame":
                self._send(200, body=b"PK\x03\x04fake-npz", ctype="application/octet-stream",
                           headers={"X-MAST-Frame-Meta": json.dumps(FRAME_META)})
            else:
                self._send(404, {"error": "not_found", "detail": f"no route {method} {route}"})
    return Handler


@pytest.fixture()
def fake():
    server = FakeMast()
    yield server
    server.close()


def _box(url: str, tmp_path: Path, **env: str) -> Toolbox:
    environ = {"MAST_URL": url, "MAST_FETCH_DIR": str(tmp_path / "fetch"), **env}
    return Toolbox(load_config(environ))


def _call(box: Toolbox, name: str, **arguments):
    out = box.call_tool(name, arguments, CallSignal(threading.Event()))
    return out.text, out.is_error


# ── requests carry who is acting ─────────────────────────────────────────────
def test_every_request_carries_actor_session_and_basic_auth(fake, tmp_path):
    fake.login = ("alice", " s3cret pass ")
    box = _box(fake.url, tmp_path, MAST_USER="alice", MAST_PASSWORD=" s3cret pass ",
               MAST_ACTOR="Claude Code", MAST_SESSION="ab12cd34")
    text, is_error = _call(box, "mast_status")
    assert not is_error, text
    assert text.startswith("MAST status: mode SAFE; abort clear; instrument free")
    headers = fake.seen[-1]["headers"]
    assert headers["x-mast-actor"] == "claude-code"
    assert headers["x-mast-session"] == "ab12cd34"
    assert headers["authorization"] == "Basic " + base64.b64encode(
        b"alice: s3cret pass ").decode("ascii")


def test_rejected_login_says_so(fake, tmp_path):
    fake.login = ("alice", "right")
    text, is_error = _call(_box(fake.url, tmp_path, MAST_USER="alice", MAST_PASSWORD="wrong"),
                           "mast_status")
    assert is_error and "rejected" in text


def test_missing_login_names_the_options(fake, tmp_path):
    fake.login = ("alice", "right")
    text, is_error = _call(_box(fake.url, tmp_path), "mast_status")
    assert is_error and "mast_user" in text and "LAN mode" in text


# ── jobs ─────────────────────────────────────────────────────────────────────
def test_run_submits_then_polls_until_the_job_ends(fake, tmp_path):
    box = _box(fake.url, tmp_path, MAST_SESSION="feed0001")
    started = time.monotonic()
    text, is_error = _call(box, "mast_run", skill="GetBias", params={"channel": 1}, wait_s=10)
    assert not is_error, text
    assert time.monotonic() - started < 8
    assert "succeeded" in text.splitlines()[0] and "bias is 0.5 V" in text
    post = fake.requests_to("POST", "/jobs")[0]["body"]
    assert post["skill"] == "GetBias" and post["params"] == {"channel": 1}
    assert post["request_id"].startswith("mcp-feed0001-")
    polls = [r for r in fake.seen if r["method"] == "GET" and r["path"].startswith(PREFIX + "/jobs/")]
    assert polls and all(1 <= int(p["query"]["wait_s"]) <= 30 for p in polls)


def test_run_with_zero_wait_returns_the_running_job(fake, tmp_path):
    fake.job_duration = 30
    text, is_error = _call(_box(fake.url, tmp_path), "mast_run", skill="GetBias", wait_s=0)
    assert not is_error
    assert "running" in text and 'mast_job(job_id="j_000000000001"' in text
    assert not [r for r in fake.seen if r["method"] == "GET"]


def test_job_waits_with_wait_s_and_reports_the_end(fake, tmp_path):
    box = _box(fake.url, tmp_path)
    _call(box, "mast_run", skill="GetBias", wait_s=0)
    text, is_error = _call(box, "mast_job", job_id="j_000000000001", wait_s=5)
    assert not is_error and "succeeded" in text


def test_refused_busy_is_explained_and_marked_as_error(fake, tmp_path):
    text, is_error = _call(_box(fake.url, tmp_path), "mast_run", skill="Busy")
    first = text.splitlines()[0]
    assert is_error
    assert "refused_busy" in first and "NOT executed" in first and "operator GUI" in first
    assert "NOT recorded" in first and "mast_scope" in first


def test_same_request_id_returns_the_original_job_even_after_polling(fake, tmp_path):
    box = _box(fake.url, tmp_path)
    _call(box, "mast_run", skill="GetBias", request_id="pulse-1", wait_s=0)
    # wait_s > 0: the answer is a poll's view, which does not repeat idempotent_replay
    text, _ = _call(box, "mast_run", skill="GetBias", request_id="pulse-1", wait_s=5)
    assert "succeeded" in text.splitlines()[0]
    assert "ORIGINAL job" in text and len(fake.jobs) == 1


def test_refused_submissions_say_what_to_do_instead(fake, tmp_path):
    box = _box(fake.url, tmp_path)
    text, is_error = _call(box, "mast_run", skill="Disabled")
    assert is_error and "skill_disabled" in text and "does not switch it on" in text
    _call(box, "mast_run", skill="GetBias", request_id="r1", wait_s=0)
    text, is_error = _call(box, "mast_run", skill="SetBias", request_id="r1", wait_s=0)
    assert is_error and "request_id_conflict" in text and "a new action needs a new id" in text
    assert len(fake.jobs) == 1


def test_lost_answer_to_a_submit_is_retried_once_with_the_same_request_id(fake, tmp_path):
    fake.drop_next_post = "/jobs"
    text, is_error = _call(_box(fake.url, tmp_path), "mast_run", skill="GetBias", wait_s=5)
    assert not is_error, text
    posts = fake.requests_to("POST", "/jobs")
    assert len(posts) == 2
    assert posts[0]["body"]["request_id"] == posts[1]["body"]["request_id"]
    assert len(fake.jobs) == 1, "the retry must not run the action a second time"
    assert "nothing ran twice" in text and "ORIGINAL job" not in text


class _ScriptedClient:
    """Stands in for MastClient; a GET advances the fake clock by the wait it asked for."""

    def __init__(self, clock: list):
        self.clock = clock
        self.calls: list[tuple] = []

    def call(self, method, path, *, query=None, body=None, timeout=30.0):
        self.calls.append((method, path, dict(query or {}), timeout))
        if method == "POST":
            return {"job_id": "j_1", "skill": "Scan", "state": "running", "terminal": False}
        self.clock[0] += float((query or {}).get("wait_s", 0))
        return {"job_id": "j_1", "skill": "Scan", "state": "running", "terminal": False}


@pytest.mark.parametrize("wait_s, expected", [(45, [30, 15]), (50, [30, 20]), (20, [20])])
def test_long_waits_are_split_and_stay_inside_the_budget(tmp_path, wait_s, expected):
    clock = [1000.0]
    client = _ScriptedClient(clock)
    box = Toolbox(load_config({"MAST_URL": "http://127.0.0.1:1"}), client=client)
    text = box.call_tool("mast_run", {"skill": "Scan", "wait_s": wait_s},
                         CallSignal(threading.Event()), clock=lambda: clock[0]).text
    gets = [c for c in client.calls if c[0] == "GET"]
    assert [int(c[2]["wait_s"]) for c in gets] == expected
    assert all(c[3] >= int(c[2]["wait_s"]) + 2 for c in gets), "socket timeout shorter than the wait"
    assert clock[0] - 1000.0 <= 55.0
    assert "still running" in text


def test_job_summary_says_what_to_do_for_each_bad_end():
    lost, err = job_summary({"job_id": "j", "skill": "Pulse", "state": "lost_on_restart",
                             "terminal": True})
    assert err and "NOT replayed" in lost and "mast_briefing" in lost
    human, err = job_summary({"job_id": "j", "skill": "C", "state": "failed", "terminal": True,
                              "refused_by": "needs_human_node", "result": {"error": "interrupt"}})
    assert err and "needs a person" in human
    aborted, _ = job_summary({"job_id": "j", "skill": "S", "state": "failed", "terminal": True,
                              "abort": {"set": True, "reason": "E-STOP"}})
    assert "abort flag is set (E-STOP)" in aborted
    cancelled, err = job_summary({"job_id": "j", "skill": "S", "state": "cancelled",
                                  "terminal": True}, cancel_request=True)
    assert not err and "cancelled" in cancelled


# ── error shaping ────────────────────────────────────────────────────────────
def test_an_html_page_instead_of_json_is_explained(fake, tmp_path):
    fake.html_paths.add(PREFIX + "/skills/search")
    text, is_error = _call(_box(fake.url, tmp_path), "mast_find_skills", query="withdraw")
    assert is_error and "HTML page" in text and "external agent API" in text


def test_a_mast_without_the_external_api_is_named(fake, tmp_path):
    fake.bare_404_paths.add(PREFIX + "/status")
    text, is_error = _call(_box(fake.url, tmp_path), "mast_status")
    assert is_error and "has no external agent API" in text


def test_a_contract_404_passes_the_detail_through(fake, tmp_path):
    text, is_error = _call(_box(fake.url, tmp_path), "mast_job", job_id="j_404")
    assert is_error and "no job j_404" in text and "unknown_job" in text


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_connection_refused_says_what_to_check(tmp_path):
    text, is_error = _call(_box(f"http://127.0.0.1:{_closed_port()}", tmp_path), "mast_status")
    assert is_error
    assert "Cannot connect" in text and "running" in text and "https" in text
    assert "launcher" in text and "启动主服务" in text, "an open launcher is not a running service"


class _Listener:
    """A loopback port that accepts and records what each connection sent before closing."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.received: list[bytes] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                data = b""
                try:
                    while chunk := conn.recv(64):
                        data += chunk
                except OSError:
                    pass
                self.received.append(data)

    def close(self):
        self.sock.close()


def test_the_launcher_probe_finds_its_port_and_sends_nothing():
    from mast_mcp.client import launcher_port

    lst = _Listener()
    try:
        started = time.monotonic()
        assert launcher_port([_closed_port(), lst.port, _closed_port()]) == lst.port
        assert time.monotonic() - started < 2.0, "the ports are tried together"
        deadline = time.monotonic() + 3
        while not lst.received and time.monotonic() < deadline:
            time.sleep(0.02)
        # the launcher acts only on "BRING_FOREGROUND": an empty connection changes nothing
        assert lst.received == [b""]
        assert launcher_port([_closed_port()]) is None
    finally:
        lst.close()


def test_refused_at_mast_s_port_with_the_launcher_open_says_to_start_the_service(
        tmp_path, monkeypatch):
    from mast_mcp import client as mast_client

    port = _closed_port()
    lst = _Listener()
    monkeypatch.setattr(mast_client, "MAST_PORT", port)            # stands in for 7862
    monkeypatch.setattr(mast_client, "LAUNCHER_PORTS", (lst.port,))
    try:
        text, is_error = _call(_box(f"http://127.0.0.1:{port}", tmp_path), "mast_status")
    finally:
        lst.close()
    assert is_error and "Cannot connect" in text
    assert f"port {lst.port}" in text and "launcher seems to be open" in text
    assert "启动主服务" in text and "needs no restart" in text


def test_the_launcher_is_looked_for_only_at_mast_s_own_port(tmp_path, monkeypatch):
    from mast_mcp import client as mast_client

    probed = []
    monkeypatch.setattr(mast_client, "launcher_port", lambda *a, **k: probed.append(1))
    _call(_box(f"http://127.0.0.1:{_closed_port()}", tmp_path), "mast_status")
    assert probed == []


# ── LAN mode: https with a self-signed certificate and a login ──────────────────
@pytest.fixture()
def lan_mast(tmp_path):
    """A MAST in LAN mode: TLS with a self-signed certificate, HTTP Basic on every request."""
    import datetime
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MAST LAN test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "lan_cert.pem", tmp_path / "lan_key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    expected = "Basic " + base64.b64encode(b"admin:pw").decode("ascii")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.headers.get("Authorization") != expected:
                body = b'{"detail":"Unauthorized"}'
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="MAST"')
            else:
                body = json.dumps({"mode": "SAFE", "abort": {"set": False},
                                   "lock": {"held": False}}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_plain_http_to_lan_mode_names_everything_at_once(lan_mast, tmp_path):
    text, is_error = _call(_box(f"http://127.0.0.1:{lan_mast}", tmp_path), "mast_status")
    assert is_error and "answers with TLS" in text and "LAN mode" in text
    # one message, every missing setting and the other way out: one reconnect is enough
    assert f"MAST_URL=https://127.0.0.1:{lan_mast}" in text
    assert "MAST_USER and MAST_PASSWORD" in text and "MAST_VERIFY_TLS=false" in text
    assert "mast_url, mast_user, mast_password and verify_tls" in text
    assert "untick LAN access" in text and "/mcp" in text
    assert '"error":"lan_mode"' in text


def test_an_untrusted_lan_certificate_also_names_the_login(lan_mast, tmp_path):
    text, is_error = _call(_box(f"https://127.0.0.1:{lan_mast}", tmp_path), "mast_status")
    assert is_error and "not trusted" in text and "self-signed" in text
    assert "MAST_VERIFY_TLS=false" in text and "MAST_USER and MAST_PASSWORD" in text
    assert "MAST_URL=https" not in text, "the address is already https"
    # switching LAN access off instead: back to plain http, and that does need a reconnect
    assert f"MAST_URL=http://127.0.0.1:{lan_mast} works after restarting" in text


def test_a_missing_lan_login_names_only_what_is_missing(lan_mast, tmp_path):
    text, is_error = _call(_box(f"https://127.0.0.1:{lan_mast}", tmp_path,
                                MAST_VERIFY_TLS="false"), "mast_status")
    assert is_error and "LAN mode" in text and "mast_user" in text
    assert "MAST_USER and MAST_PASSWORD" in text
    assert "MAST_VERIFY_TLS" not in text, text
    assert "MAST_URL=https" not in text, text
    assert "Two ways out" in text and "untick LAN access" in text


def test_lan_mode_works_once_everything_is_set(lan_mast, tmp_path):
    text, is_error = _call(_box(f"https://127.0.0.1:{lan_mast}", tmp_path, MAST_VERIFY_TLS="false",
                                MAST_USER="admin", MAST_PASSWORD="pw"), "mast_status")
    assert not is_error, text
    assert text.startswith("MAST status: mode SAFE")


def test_https_to_a_plain_http_mast_names_the_plain_address(fake, tmp_path):
    port = urlsplit(fake.url).port
    text, is_error = _call(_box(f"https://127.0.0.1:{port}", tmp_path), "mast_status")
    assert is_error and "TLS handshake" in text
    assert f"MAST_URL=http://127.0.0.1:{port}" in text and "/mcp" in text


def test_lan_help_for_a_remote_mast_has_no_launcher_way_out():
    from mast_mcp.client import MastClient

    cfg = load_config({"MAST_URL": "http://192.0.2.10:7862", "MAST_ALLOW_REMOTE": "true"})
    help_text = MastClient(cfg).lan_mode_help(https=True, login=True, insecure=True)
    assert help_text.startswith("Set all at once: MAST_URL=https://192.0.2.10:7862")
    assert "launcher (" not in help_text and "untick" not in help_text


def test_a_system_proxy_is_never_used(fake, tmp_path, monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, f"http://127.0.0.1:{_closed_port()}")
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    text, is_error = _call(_box(fake.url, tmp_path), "mast_status")
    assert not is_error, text


def test_scope_arguments_that_contradict_each_other_never_reach_mast(fake, tmp_path):
    text, is_error = _call(_box(fake.url, tmp_path), "mast_scope", experiment_id="e1",
                           experiment_name="Cu(111)")
    assert is_error and "not both" in text and fake.seen == []


def test_scope_without_an_experiment_warns_that_nothing_is_recorded(fake, tmp_path):
    text, is_error = _call(_box(fake.url, tmp_path), "mast_scope")
    assert not is_error and "NOT recorded" in text


def test_a_busy_instrument_blocks_a_scope_switch_until_forced(fake, tmp_path):
    fake.busy = True
    box = _box(fake.url, tmp_path)
    text, is_error = _call(box, "mast_scope", experiment_name="Cu(111)", goal="step edges")
    assert is_error and "instrument_busy" in text and "force=true" in text
    text, is_error = _call(box, "mast_scope", experiment_name="Cu(111)", force=True,
                           reason="operator asked for a new experiment")
    assert not is_error
    assert "changed: experiment created->e7" in text and "warnings: forced while busy" in text
    sent = fake.requests_to("POST", "/scope")[-1]["body"]
    assert sent == {"experiment": {"name": "Cu(111)"}, "force": True,
                    "reason": "operator asked for a new experiment"}


def test_briefing_names_the_degraded_sections_and_keeps_the_text_once(fake, tmp_path):
    text, is_error = _call(_box(fake.url, tmp_path), "mast_briefing")
    assert not is_error
    first, *rest = text.splitlines()
    assert first == "MAST briefing: 1 of 2 sections ok; degraded: tip"
    assert "## status" in text and text.count("- mode: safe") == 1


def test_a_note_that_fell_back_to_global_says_so(fake, tmp_path):
    text, is_error = _call(_box(fake.url, tmp_path), "mast_note_write", title="drift",
                           content="0.3 nm/min after cooling", tags="drift, thermal")
    assert not is_error and "saved to global" in text.splitlines()[0]
    assert fake.requests_to("POST", "/notes")[0]["body"]["tags"] == ["drift", "thermal"]


def test_asking_the_operator_points_at_the_reply_tool(fake, tmp_path):
    text, is_error = _call(_box(fake.url, tmp_path), "mast_ask_operator",
                           message="Please change the sample to the second holder.")
    assert not is_error and 'mast_operator_reply(request_id="r-1")' in text
    assert fake.requests_to("POST", "/requests")[0]["body"]["message"].startswith("Please")


# ── downloads ────────────────────────────────────────────────────────────────
def test_fetch_raw_writes_the_exact_bytes_inside_the_fetch_dir(fake, tmp_path):
    box = _box(fake.url, tmp_path)
    text, is_error = _call(box, "mast_fetch", path="D:\\Data\\2026\\Topo001.sxm")
    assert not is_error, text
    payload = json.loads(text.splitlines()[-1])
    saved = Path(payload["saved_to"])
    assert saved.parent == tmp_path / "fetch" and saved.name.endswith(".sxm")
    assert saved.read_bytes() == RAW_BYTES
    assert payload["bytes"] == len(RAW_BYTES)
    assert payload["sha256"] == hashlib.sha256(RAW_BYTES).hexdigest()
    assert not list((tmp_path / "fetch").glob("*.part"))
    assert fake.seen[-1]["query"]["path"] == "D:\\Data\\2026\\Topo001.sxm"


def test_fetch_frame_saves_npz_and_returns_the_meta(fake, tmp_path):
    text, is_error = _call(_box(fake.url, tmp_path), "mast_fetch", path="Topo001.sxm",
                           mode="frame", channel="Current")
    assert not is_error, text
    payload = json.loads(text.splitlines()[-1])
    assert payload["saved_to"].endswith(".Current.npz")
    assert payload["meta"] == FRAME_META
    assert fake.seen[-1]["path"] == PREFIX + "/data/frame"
    assert fake.seen[-1]["query"]["channel"] == "Current"


def test_a_stalled_download_gives_up_at_the_budget_not_the_socket_timeout(fake, tmp_path):
    box = _box(fake.url, tmp_path)
    dest = str(tmp_path / "stall.bin")
    started = time.monotonic()
    with pytest.raises(Exception) as err:
        box.client.download("/data/file", {"path": "stall.sxm"}, dest, timeout=20.0,
                            should_stop=lambda: False, deadline=time.monotonic() + 1.5,
                            clock=time.monotonic)
    assert time.monotonic() - started < 6, "a stalled read outlived the call's budget"
    assert "did not answer" in str(err.value) or "time budget" in str(err.value)
    assert not list(tmp_path.glob("stall.bin*")), "a partial download was left behind"


def test_the_default_fetch_dir_keeps_data_out_of_git(fake, tmp_path):
    box = Toolbox(load_config({"MAST_URL": fake.url, "CLAUDE_PROJECT_DIR": str(tmp_path)}))
    text, is_error = _call(box, "mast_fetch", path="Topo001.sxm")
    assert not is_error, text
    ignore = tmp_path / ".mast-fetch" / ".gitignore"
    assert ignore.is_file() and "*" in ignore.read_text(encoding="utf-8").splitlines()


def test_an_html_answer_is_not_saved_as_data(fake, tmp_path):
    fake.html_paths.add(PREFIX + "/data/file")
    text, is_error = _call(_box(fake.url, tmp_path), "mast_fetch", path="Topo001.sxm")
    assert is_error and "HTML page" in text
    assert not [p for p in (tmp_path / "fetch").iterdir()]


@pytest.mark.parametrize("remote", ["../../evil/../x.sxm", "..\\..\\CON.sxm", "/etc/passwd",
                                    "C:\\Windows\\..\\..\\..\\boot.ini", "....", "扫描 01.sxm"])
def test_fetched_names_never_leave_the_fetch_dir(tmp_path, remote):
    local = Path(local_fetch_path(str(tmp_path), remote, "raw", None))
    assert local.parent == tmp_path
    assert not local.name.startswith(".") and local.stem.split(".")[0].lower() != "con"


# ── configuration ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", ["http://127.0.0.1:7862", "http://localhost:7862",
                                 "http://[::1]:7862", "https://127.3.2.1:7862"])
def test_loopback_addresses_are_accepted(url):
    assert load_config({"MAST_URL": url}).problem == ""


@pytest.mark.parametrize("url", ["http://192.0.2.10:7862", "https://rig.example.org:7862",
                                 "http://0.0.0.0:7862"])
def test_other_addresses_need_allow_remote(url):
    assert "not this machine" in load_config({"MAST_URL": url}).problem
    assert load_config({"MAST_URL": url, "MAST_ALLOW_REMOTE": "true"}).problem == ""


def test_unexpanded_placeholders_mean_unset():
    cfg = load_config({"MAST_URL": "${user_config.mast_url}", "MAST_USER": "${user_config.mast_user}",
                       "MAST_PASSWORD": "${user_config.mast_password}",
                       "MAST_VERIFY_TLS": "${user_config.verify_tls}",
                       "MAST_ACTOR": "${MAST_ACTOR:-}"})
    assert cfg.url == "http://127.0.0.1:7862" and cfg.problem == ""
    assert cfg.user == "" and cfg.password == "" and cfg.verify_tls is True
    assert cfg.actor == "claude-code" and len(cfg.session) == 8


def test_url_forms_are_normalized_or_refused():
    assert load_config({"MAST_URL": "http://127.0.0.1:7862/api/ext/v1/"}).api_base == \
        "http://127.0.0.1:7862/api/ext/v1"
    assert load_config({"MAST_URL": "127.0.0.1:7862"}).problem
    assert load_config({"MAST_URL": "ftp://127.0.0.1"}).problem


def test_a_call_context_refuses_to_start_a_request_with_no_time_left():
    clock = [0.0]
    ctx = CallContext(client=None, config=None, signal=CallSignal(threading.Event()),
                      clock=lambda: clock[0], budget=55.0)
    assert ctx.timeout(30) == 30
    clock[0] = 54.5
    with pytest.raises(Exception, match="time budget"):
        ctx.timeout(30)
