"""Remote AI access: MCP over streamable HTTP (``http_rpc``) and the server around it.

What an AI on another computer relies on: the key in the URL (404 when wrong, so
the client never starts an OAuth login), no answer to web pages or to senders
outside the tailnet, version negotiation the way Claude Code and Codex speak it,
calls that end within the 55 s an HTTP client waits, and connection lines that
survive a restart. The servers here are real sockets on 127.0.0.1.
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

import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mast_mcp import http_rpc, remote_server, tools
from mast_mcp import vista_surface as vs
from mast_mcp.config import load_config
from mast_mcp.http_rpc import HttpMcpServer, Peer
from mast_mcp.remote_server import RemoteServer, RemoteSettings
from mast_mcp.tools import Toolbox, ToolOutput

KEY = "0123456789abcdef0123456789abcdef"


# ── helpers ──────────────────────────────────────────────────────────────────
def _post(port: int, path: str, payload, *, headers: dict | None = None, raw: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json",
                          "Accept": "application/json, text/event-stream", **(headers or {})})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def _rpc(port: int, method: str, params=None, *, mid=1, key=KEY):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    status, _, data = _post(port, f"/mcp/{key}", msg)
    assert status == 200, (status, data)
    return json.loads(data)


def _request(port: int, method: str, path: str, *, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


class FakeBackend:
    """Records what the transport asks; ``block`` makes ``slow`` wait on its signal."""

    def __init__(self):
        self.notes: list[tuple[str, str, str]] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.signals: list = []
        self.client_info = None

    def check_key(self, key):
        return http_rpc.check_key(KEY, key)

    def peer_name(self, address):
        return "laptop" if address == "127.0.0.1" else ""

    def note(self, peer, kind, detail=""):
        self.notes.append((peer.name, kind, detail))

    def initialize(self, peer, client_info):
        self.client_info = client_info
        return {"serverInfo": {"name": "fake", "version": "1"}, "instructions": "be careful"}

    def list_tools(self, peer):
        return [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}},
                {"name": "slow", "description": "slow", "inputSchema": {"type": "object"}}]

    def has_tool(self, peer, name):
        return name in ("echo", "slow", "boom")

    def call_tool(self, peer, name, arguments, signal, meta):
        self.calls.append((peer.name, name, arguments))
        if name == "boom":
            raise RuntimeError("kaputt")
        if name == "slow":
            self.signals.append(signal)
            stopped = signal.wait(float(arguments.get("seconds", 10)))
            return ToolOutput(f"stopped={stopped}")
        return ToolOutput(json.dumps(arguments), False,
                          [{"label": "a picture", "mime": "image/jpeg", "data_b64": "AAAA"}])

    def list_resources(self):
        return [{"uri": "mast://guide/en/README.md", "name": "readme"}]

    def list_templates(self):
        return []

    def read_resource(self, uri):
        if uri != "mast://guide/en/README.md":
            raise KeyError(uri)
        return {"uri": uri, "mimeType": "text/markdown", "text": "# Guide"}


@pytest.fixture
def served():
    backend = FakeBackend()
    server = HttpMcpServer(backend, host="127.0.0.1", port=0, exact_port=True)
    server.start()
    yield backend, server
    server.stop()


# ── who may talk to the server ───────────────────────────────────────────────
@pytest.mark.parametrize("address, ok", [
    ("100.64.0.1", True), ("100.101.102.103", True), ("100.127.255.254", True),
    ("100.63.255.255", False), ("100.128.0.1", False), ("192.168.1.20", False),
    ("10.0.0.5", False), ("8.8.8.8", False), ("127.0.0.1", True), ("::1", True),
    ("::ffff:100.100.1.1", True), ("::ffff:192.168.1.1", False),
    ("fd7a:115c:a1e0::1", False), ("not-an-ip", False), ("", False),
])
def test_only_tailscale_and_this_machine_count_as_the_tailnet(address, ok):
    assert http_rpc.from_tailnet(address) is ok


def test_wrong_key_is_404_not_401_and_is_logged_once_a_minute(served):
    backend, server = served
    for _ in range(3):
        status, _, data = _post(server.port, "/mcp/" + "f" * 32, {"jsonrpc": "2.0", "id": 1,
                                                               "method": "ping"})
        assert status == 404
        assert b"wrong key" in data
    assert [n for n in backend.notes if n[1] == "badkey"] == [("laptop", "badkey", "")]


def test_an_empty_key_never_matches():
    assert not http_rpc.check_key("", "")
    assert not http_rpc.check_key(KEY, "")
    assert http_rpc.check_key(KEY, KEY)


def test_a_bearer_token_on_plain_mcp_works_too(served):
    _, server = served
    status, _, data = _post(server.port, "/mcp", {"jsonrpc": "2.0", "id": 7, "method": "ping"},
                            headers={"Authorization": "Bearer " + KEY})
    assert status == 200 and json.loads(data) == {"jsonrpc": "2.0", "id": 7, "result": {}}
    status, _, _ = _post(server.port, "/mcp", {"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert status == 404


def test_web_pages_are_refused_even_with_the_right_key(served):
    _, server = served
    status, _, data = _post(server.port, f"/mcp/{KEY}", {"jsonrpc": "2.0", "id": 1,
                                                         "method": "ping"},
                            headers={"Origin": "https://evil.example"})
    assert status == 403 and b"web pages" in data
    status, _, _ = _request(server.port, "OPTIONS", f"/mcp/{KEY}",
                            headers={"Origin": "https://evil.example"})
    assert status == 403


def test_a_sender_outside_the_tailnet_is_refused(served, monkeypatch):
    _, server = served
    monkeypatch.setattr(http_rpc, "from_tailnet", lambda address: False)
    status, _, data = _post(server.port, f"/mcp/{KEY}", {"jsonrpc": "2.0", "id": 1,
                                                         "method": "ping"})
    assert status == 403 and b"Tailscale" in data
    server.tailnet_only = False
    status, _, _ = _post(server.port, f"/mcp/{KEY}", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 200


def test_get_answers_405_and_health_answers_200(served):
    _, server = served
    status, headers, data = _request(server.port, "GET", f"/mcp/{KEY}")
    assert status == 405 and headers.get("Allow") == "POST" and b"POST" in data
    status, _, data = _request(server.port, "GET", "/health")
    assert status == 200 and data.startswith(b"mast-remote-ai ok")
    status, _, _ = _request(server.port, "GET", "/.well-known/oauth-authorization-server")
    assert status == 404


def test_a_body_without_content_length_or_too_large_is_refused(served, monkeypatch):
    _, server = served
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    sock.sendall(f"POST /mcp/{KEY} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                 "\r\n".encode("ascii"))
    assert sock.recv(4096).split(b"\r\n", 1)[0].endswith(b"411 Length Required")
    sock.close()
    monkeypatch.setattr(http_rpc, "MAX_BODY", 10)
    status, _, _ = _post(server.port, f"/mcp/{KEY}", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 413


# ── the protocol ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("asked, answered", [
    ("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"),
    ("2024-11-05", "2024-11-05"), ("2099-01-01", "2025-11-25"), (None, "2025-11-25"),
])
def test_initialize_echoes_a_known_version_and_offers_the_newest_otherwise(served, asked, answered):
    backend, server = served
    reply = _rpc(server.port, "initialize", {"protocolVersion": asked, "capabilities": {},
                                             "clientInfo": {"name": "claude-code",
                                                            "version": "2.1.310"}})
    result = reply["result"]
    assert result["protocolVersion"] == answered
    assert result["instructions"] == "be careful"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert backend.client_info == {"name": "claude-code", "version": "2.1.310"}
    assert ("laptop", "connected", "claude-code 2.1.310") in backend.notes


def test_the_discover_probe_gets_method_not_found(served):
    _, server = served
    reply = _rpc(server.port, "server/discover", {})
    assert reply["error"]["code"] == -32601


def test_tools_list_and_call_return_text_then_labelled_images(served):
    backend, server = served
    tools_ = _rpc(server.port, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools_] == ["echo", "slow"]
    result = _rpc(server.port, "tools/call", {"name": "echo", "arguments": {"x": 1}})["result"]
    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": '{"x": 1}'},
                                 {"type": "text", "text": "a picture"},
                                 {"type": "image", "data": "AAAA", "mimeType": "image/jpeg"}]
    assert "structuredContent" not in result
    assert backend.calls == [("laptop", "echo", {"x": 1})]


def test_unknown_tool_and_bad_arguments_are_protocol_errors(served):
    _, server = served
    assert _rpc(server.port, "tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert _rpc(server.port, "tools/call", {"name": "echo", "arguments": [1]})["error"]["code"] \
        == -32602


def test_a_tool_that_raises_becomes_a_tool_error(served):
    _, server = served
    result = _rpc(server.port, "tools/call", {"name": "boom"})["result"]
    assert result["isError"] is True and "kaputt" in result["content"][0]["text"]


def test_notifications_get_202_and_no_body(served):
    _, server = served
    status, headers, data = _post(server.port, f"/mcp/{KEY}",
                                  {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202 and data == b"" and headers.get("Content-Length") == "0"


def test_parse_errors_and_batches(served):
    _, server = served
    status, _, data = _post(server.port, f"/mcp/{KEY}", None, raw=b"{not json")
    assert status == 400 and json.loads(data)["error"]["code"] == -32700
    status, _, data = _post(server.port, f"/mcp/{KEY}", [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    replies = json.loads(data)
    assert status == 200 and [r["id"] for r in replies] == [1, 2]


def test_resources_are_served(served):
    _, server = served
    assert _rpc(server.port, "resources/list")["result"]["resources"][0]["name"] == "readme"
    item = _rpc(server.port, "resources/read", {"uri": "mast://guide/en/README.md"})["result"]
    assert item["contents"][0]["text"] == "# Guide"
    assert _rpc(server.port, "resources/read", {"uri": "mast://x"})["error"]["code"] == -32002


def test_cancel_reaches_the_running_call(served):
    backend, server = served
    out = {}
    worker = threading.Thread(target=lambda: out.update(
        r=_rpc(server.port, "tools/call", {"name": "slow", "arguments": {"seconds": 20}}, mid=41)))
    worker.start()
    deadline = time.monotonic() + 10
    while not backend.signals and time.monotonic() < deadline:
        time.sleep(0.02)
    started = time.monotonic()
    _post(server.port, f"/mcp/{KEY}", {"jsonrpc": "2.0", "method": "notifications/cancelled",
                                       "params": {"requestId": 41, "reason": "user"}})
    worker.join(10)
    assert time.monotonic() - started < 5
    assert out["r"]["result"]["content"][0]["text"] == "stopped=True"


def test_stop_releases_a_waiting_call_and_the_port(served):
    backend, server = served
    port = server.port
    out = {}
    worker = threading.Thread(target=lambda: out.update(
        r=_rpc(port, "tools/call", {"name": "slow", "arguments": {"seconds": 30}})))
    worker.start()
    deadline = time.monotonic() + 10
    while not backend.signals and time.monotonic() < deadline:
        time.sleep(0.02)
    started = time.monotonic()
    server.stop()
    worker.join(10)
    assert time.monotonic() - started < 6
    assert out["r"]["result"]["content"][0]["text"] == "stopped=True"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_too_many_calls_at_once_get_a_tool_error(monkeypatch):
    monkeypatch.setattr(http_rpc, "MAX_CONCURRENT_CALLS", 1)
    backend = FakeBackend()
    server = HttpMcpServer(backend, host="127.0.0.1", port=0, exact_port=True)
    server.start()
    try:
        worker = threading.Thread(target=lambda: _rpc(
            server.port, "tools/call", {"name": "slow", "arguments": {"seconds": 5}}))
        worker.start()
        deadline = time.monotonic() + 10
        while not backend.signals and time.monotonic() < deadline:
            time.sleep(0.02)
        result = _rpc(server.port, "tools/call", {"name": "echo"}, mid=2)["result"]
        assert result["isError"] is True and "already running 1 tool calls" in \
            result["content"][0]["text"]
        backend.signals[0].cancelled.set()
        worker.join(10)
    finally:
        server.stop()


def test_a_taken_port_moves_on_to_the_next_free_one():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    taken = blocker.getsockname()[1]
    server = HttpMcpServer(FakeBackend(), host="127.0.0.1", port=taken)
    try:
        assert server.start() != taken
    finally:
        server.stop()
        blocker.close()
    exact = HttpMcpServer(FakeBackend(), host="127.0.0.1", port=taken, exact_port=True)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", taken))
    blocker.listen(1)
    try:
        with pytest.raises(OSError):
            exact.start()
    finally:
        blocker.close()


# ── the short-call profile of the mast_* tools ───────────────────────────────
def test_the_long_wait_texts_are_really_in_the_description():
    """The short profile replaces these pieces; if they vanish it silently replaces nothing."""
    assert vs.WAIT_LIMITS_TEXT in vs.WAIT_DESCRIPTION
    assert vs.WAIT_CLIENT_TIMEOUT_NOTE in vs.WAIT_DESCRIPTION
    assert tools._LONG_WAIT_RULE in tools.INSTRUCTIONS


def test_stdio_toolbox_is_unchanged():
    box = Toolbox(load_config({"MAST_URL": "http://127.0.0.1:9"}))
    assert box.instructions() == tools.INSTRUCTIONS
    assert box.list_tools() == [t.as_mcp() for t in tools.TOOLS.values()]
    assert box.max_wait_s == vs.WAIT_MAX_S


def test_short_toolbox_waits_at_most_45_s_and_says_so():
    box = Toolbox(load_config({"MAST_URL": "http://127.0.0.1:9"}), call_ceiling_s=55.0)
    assert box.max_wait_s == 45
    wait = next(t for t in box.list_tools() if t["name"] == "mast_wait")
    assert wait["inputSchema"]["properties"]["timeout_s"]["maximum"] == 45
    assert wait["inputSchema"]["properties"]["timeout_s"]["default"] == 45
    assert "900" not in wait["description"] and "(default 45, max 45)" in wait["description"]
    assert "call mast_wait again" in wait["description"]
    assert "900" not in box.instructions() and "at most 45 s" in box.instructions()
    # the registry itself is untouched
    assert tools.TOOLS["mast_wait"].as_mcp()["inputSchema"]["properties"]["timeout_s"][
        "maximum"] == vs.WAIT_MAX_S


class _RecordingClient:
    def __init__(self):
        self.calls = []

    def call(self, method, path, *, query=None, body=None, timeout=30.0):
        self.calls.append((method, path, body, timeout))
        return {"timed_out": True, "waited_s": body.get("timeout_s"), "fired": [], "cursor": "c1"}


class _Signal:
    cancelled = threading.Event()

    def should_stop(self):
        return False

    def wait(self, seconds):
        return False


def test_short_toolbox_clamps_the_wait_and_the_budget():
    client = _RecordingClient()
    box = Toolbox(load_config({"MAST_URL": "http://127.0.0.1:9"}), client,
                  call_ceiling_s=55.0)
    out = box.call_tool("mast_wait", {"conditions": [{"type": "timer", "seconds": 600}],
                                      "timeout_s": 840}, _Signal())
    method, path, body, timeout = client.calls[0]
    assert (method, path, body["timeout_s"]) == ("POST", "/wait", 45)
    assert timeout <= 55.0
    assert not out.is_error and box.session.wait_cursor == "c1"
    long_box = Toolbox(load_config({"MAST_URL": "http://127.0.0.1:9"}), _RecordingClient())
    long_box.call_tool("mast_wait", {"conditions": [{"type": "timer", "seconds": 5}],
                                     "timeout_s": 840}, _Signal())
    assert long_box.client.calls[0][2]["timeout_s"] == 840


# ── the remote server ────────────────────────────────────────────────────────
def test_names_lines_and_urls():
    assert remote_server.sanitize_name("Kuchyňský počítač") == "kuchynsky-pocitac"
    assert remote_server.sanitize_name("光谱机") == "mast"
    assert remote_server.default_name("RIG2") == "mast-rig2"
    assert remote_server.default_name("mast-rig3") == "mast-rig3"
    assert remote_server.default_name("") == "mast"
    url = remote_server.mcp_url("100.86.1.2", 47890, KEY)
    assert url == f"http://100.86.1.2:47890/mcp/{KEY}"
    lines = remote_server.connection_lines("mast-rig2", url)
    text = "\n".join(lines)
    assert text.isascii() and '"' not in text and "'" not in text
    assert url in lines[0] and "claude mcp add -s user -t http mast-rig2 <URL>" in lines[1]
    assert "codex mcp add mast-rig2 --url <URL>" in lines[1]


def test_settings_survive_damage(tmp_path):
    path = tmp_path / "remote_ai.json"
    assert RemoteSettings.load(str(path)) == RemoteSettings()
    path.write_text("{broken", encoding="utf-8")
    assert RemoteSettings.load(str(path)) == RemoteSettings()
    path.write_text(json.dumps({"key": "short", "port": 70000, "name": "Lab PC!"}),
                    encoding="utf-8")
    assert RemoteSettings.load(str(path)) == RemoteSettings(key="", port=47890, name="lab-pc")
    RemoteSettings(key=KEY, port=47999, name="x").save(str(path))
    assert RemoteSettings.load(str(path)) == RemoteSettings(key=KEY, port=47999, name="x")


class FakeComputer:
    def __init__(self):
        self.closed = False
        self.calls = []

    def list_tools(self):
        return [{"name": "run_command", "description": "run", "inputSchema": {"type": "object"}}]

    def has_tool(self, name):
        return name == "run_command"

    def call_tool(self, name, arguments, signal, *, who=""):
        self.calls.append((name, who))
        return ToolOutput("ran")

    def instructions(self):
        return "- computer instructions"

    def close(self):
        self.closed = True


class FakeService:
    def __init__(self, running=True):
        self.state = {"running": running, "starting": False, "managed": True}
        self.actions = []

    def status(self):
        return dict(self.state)

    def start(self):
        self.actions.append("start")
        self.state["running"] = True
        return "Started."

    def restart(self):
        self.actions.append("restart")
        return "Restarted."

    def stop(self):
        self.actions.append("stop")
        self.state["running"] = False
        return "Stopped."


class FakeMast:
    """A MAST external API on loopback that answers /status and records the headers."""

    def __init__(self):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                seen.append((self.path, self.headers.get("X-MAST-Actor"),
                             self.headers.get("X-MAST-Session")))
                body = json.dumps({"mode": "safe", "instrument": {"connected": True}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-MAST-Ext-Version", "1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def remote(tmp_path):
    mast = FakeMast()
    activity = []
    computer = FakeComputer()
    service = FakeService()
    server = RemoteServer(settings_path=str(tmp_path / "cfg" / "remote_ai.json"),
                          host="127.0.0.1", name="mast-testpc",
                          fetch_dir=str(tmp_path / "fetch"), mast_port=mast.port,
                          computer=computer, service=service,
                          activity=lambda who, kind, detail: activity.append((who, kind, detail)),
                          peer_names=lambda: {"127.0.0.1": "laptop"})
    server.settings.port = 0          # any free port; start() remembers the one it got
    server.start()
    yield server, mast, computer, service, activity
    server.stop()
    mast.close()


def _key_of(server) -> str:
    return server.settings.key


def test_start_saves_key_and_port_and_a_restart_keeps_the_lines(remote, tmp_path):
    server, *_ = remote
    saved = RemoteSettings.load(server.settings_path)
    assert remote_server.valid_key(saved.key) and saved.port == server.port
    lines = server.lines()
    again = RemoteServer(settings_path=server.settings_path, host="127.0.0.1", name="mast-testpc",
                         fetch_dir=str(tmp_path / "fetch"), computer=FakeComputer())
    assert again.lines() == lines


def test_a_new_key_locks_out_the_old_lines(remote):
    server, *_ = remote
    old = _key_of(server)
    assert _rpc(server.port, "ping", key=old)["result"] == {}
    server.renew_key()
    status, _, _ = _post(server.port, f"/mcp/{old}", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 404
    assert _rpc(server.port, "ping", key=_key_of(server))["result"] == {}
    assert RemoteSettings.load(server.settings_path).key == _key_of(server)


def test_tools_are_mast_service_and_computer(remote):
    server, *_ = remote
    key = _key_of(server)
    names = [t["name"] for t in _rpc(server.port, "tools/list", key=key)["result"]["tools"]]
    assert names[:len(tools.TOOLS)] == list(tools.TOOLS)
    assert names[len(tools.TOOLS):] == ["mast_service", "run_command"]
    wait = next(t for t in _rpc(server.port, "tools/list", key=key)["result"]["tools"]
                if t["name"] == "mast_wait")
    assert wait["inputSchema"]["properties"]["timeout_s"]["maximum"] == 45
    init = _rpc(server.port, "initialize", {"protocolVersion": "2025-11-25",
                                            "clientInfo": {"name": "claude-code"}}, key=key)
    text = init["result"]["instructions"]
    assert "MAST computer mast-testpc" in text and "- computer instructions" in text
    assert "900" not in text and init["result"]["serverInfo"]["name"] == "mast-remote"


def test_mast_tools_carry_the_callers_name_and_reach_mast_on_loopback(remote):
    server, mast, _, _, activity = remote
    key = _key_of(server)
    _rpc(server.port, "initialize", {"protocolVersion": "2025-11-25",
                                     "clientInfo": {"name": "claude-code"}}, key=key)
    result = _rpc(server.port, "tools/call", {"name": "mast_status", "arguments": {}},
                  key=key)["result"]
    assert result["isError"] is False, result
    path, actor, session = mast.seen[-1]
    assert path.startswith("/api/ext/v1/status")
    assert actor == "claude-code-laptop" and session.startswith("laptop-")
    assert ("laptop", "mast", "mast_status") in activity


def test_mast_tools_say_how_to_start_a_stopped_service(remote):
    server, mast, _, service, _ = remote
    key = _key_of(server)
    service.state["running"] = False
    result = _rpc(server.port, "tools/call", {"name": "mast_briefing", "arguments": {}},
                  key=key)["result"]
    assert result["isError"] is True
    assert 'mast_service(action="start")' in result["content"][0]["text"]
    assert not mast.seen
    result = _rpc(server.port, "tools/call", {"name": "mast_service",
                                              "arguments": {"action": "start"}}, key=key)["result"]
    assert result["isError"] is False and "Started." in result["content"][0]["text"]
    assert "is running" in result["content"][0]["text"] and service.actions == ["start"]
    bad = _rpc(server.port, "tools/call", {"name": "mast_service", "arguments": {"action": "kill"}},
               key=key)["result"]
    assert bad["isError"] is True


def test_computer_tools_go_to_the_computer_provider_with_the_callers_name(remote):
    server, _, computer, _, _ = remote
    result = _rpc(server.port, "tools/call", {"name": "run_command",
                                              "arguments": {"command": "dir"}},
                  key=_key_of(server))["result"]
    assert result["content"][0]["text"] == "ran" and computer.calls == [("run_command", "laptop")]


def test_stop_closes_the_computer_tools(remote):
    server, _, computer, _, _ = remote
    server.stop()
    assert computer.closed and not server.running


@pytest.mark.parametrize("tool, arguments, code", [
    ("mast_detector_create", {"name": "x", "code": "def detect(f): pass",
                              "applies_to": ["scan"]}, True),
    ("mast_detectors", {"op": "test", "detector_id": "d1", "frame": "t1.s0"}, True),
    ("mast_detectors", {"op": " TEST ", "detector_id": "d1", "frame": "t1.s0"}, True),
    ("mast_detectors", {"op": "list"}, False),
    ("mast_detectors", {"op": "delete", "detector_id": "d1"}, False),
    ("mast_run", {"skill": "StopScan"}, False),
])
def test_what_counts_as_running_code(tool, arguments, code):
    assert remote_server.runs_code(tool, arguments) is code


def test_detector_code_needs_the_computer_switch(tmp_path):
    """MAST takes detector code only from its own machine; this server always reaches it from
    there, so the operator's switch has to stand in for that boundary."""
    mast = FakeMast()
    allowed = {"on": False}
    activity = []
    server = RemoteServer(settings_path=str(tmp_path / "r.json"), host="127.0.0.1", name="pc",
                          fetch_dir=str(tmp_path), mast_port=mast.port, computer=FakeComputer(),
                          computer_allowed=lambda: allowed["on"],
                          activity=lambda who, kind, detail: activity.append((kind, detail)))
    peer = Peer("127.0.0.1", "laptop")
    try:
        out = server.call_tool(peer, "mast_detector_create",
                               {"name": "x", "code": "import os", "applies_to": ["scan"]},
                               _Signal(), None)
        assert out.is_error and "commands, files and screenshots" in out.text
        assert ("refused", "mast_detector_create") in activity and not mast.seen
        out = server.call_tool(peer, "mast_detectors", {"op": "test", "detector_id": "d",
                                                        "frame": "t1.s0"}, _Signal(), None)
        assert out.is_error and not mast.seen
        server.call_tool(peer, "mast_status", {}, _Signal(), None)       # not code: goes through
        assert mast.seen and mast.seen[-1][0].startswith("/api/ext/v1/status")
        allowed["on"] = True
        before = len(mast.seen)
        server.call_tool(peer, "mast_detectors", {"op": "list"}, _Signal(), None)
        assert len(mast.seen) == before + 1
    finally:
        mast.close()


def test_lan_mode_uses_https_and_the_login(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_server, "serves_tls", lambda host, port, timeout=1.0: True)
    server = RemoteServer(settings_path=str(tmp_path / "r.json"), host="127.0.0.1", name="x",
                          fetch_dir=str(tmp_path), computer=FakeComputer(),
                          mast_login=lambda: ("admin", "s3cret"))
    box = server._toolbox(Peer("100.64.0.9", "laptop"), server._state(Peer("100.64.0.9", "laptop")))
    assert box.config.url == "https://127.0.0.1:7862"
    assert (box.config.user, box.config.password, box.config.verify_tls) == ("admin", "s3cret",
                                                                              False)
    assert box.config.fetch_dir == str(tmp_path)
