"""The launcher's graceful-stop step 0 works in LAN mode.

``_http_shutdown_request`` POSTs ``/api/admin/shutdown`` to the local service —
the only stop request a windowless frozen service hears. In LAN mode that
service sits behind HTTP basic auth over TLS. The request once
carried no credentials: the service answered 401, step 0 failed silently, and
the launcher went on to TerminateProcess the Nanonis-holding service (the launcher
log then showed 「主服务在 15s 内未优雅退出 — 将强制结束」).

A fake endpoint stands in for the service: plain http for loopback mode, TLS
with a real LAN certificate for LAN mode. Nothing here touches a real service.
"""
from __future__ import annotations

import base64
import http.server
import importlib.util
import json
import socket
import ssl
import sys
import threading
import time
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

_LAUNCHER_PATH = Path(__file__).resolve().parents[3] / "mast2_launcher.py"
USER, PWD = "admin", "pw-for-tests"
BASIC = "Basic " + base64.b64encode(f"{USER}:{PWD}".encode()).decode()


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("mast2_launcher", _LAUNCHER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 — http.server API
        srv = self.server
        n = int(self.headers.get("Content-Length") or 0)
        # header names are case-insensitive on the wire (urllib sends "X-mast-…")
        srv.seen.append({"path": self.path,
                         "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": self.rfile.read(n)})
        if srv.require and self.headers.get("Authorization") != srv.require:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="MAST"')
            self.send_header("Content-Length", "12")
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return
        payload = json.dumps(srv.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_a):
        pass


def _serve(tmp_path: Path, *, tls: bool, require: str | None, reply: dict):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.seen, srv.require, srv.reply = [], require, reply
    if tls:
        from mast.net.tls import ensure_lan_cert

        cert, key = tmp_path / "lan_cert.pem", tmp_path / "lan_key.pem"
        ensure_lan_cert(cert, key, addresses=[], hostname="box")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def lan_service(tmp_path):
    srv = _serve(tmp_path, tls=True, require=BASIC, reply={"ok": True, "exiting": True})
    yield srv
    srv.shutdown()
    srv.server_close()


def test_lan_mode_request_carries_basic_auth_and_a_json_body(launcher, lan_service):
    port = lan_service.server_address[1]
    assert launcher._http_shutdown_request(port, auth=(USER, PWD)) is True
    got = lan_service.seen[-1]
    assert got["path"] == "/api/admin/shutdown"
    assert got["headers"]["authorization"] == BASIC
    assert got["headers"]["content-type"] == "application/json"
    assert got["body"] == b"{}"


def test_lan_mode_without_credentials_is_reported_not_swallowed(launcher, lan_service):
    said: list[str] = []
    assert launcher._http_shutdown_request(lan_service.server_address[1], log=said.append) is False
    assert any("401" in m for m in said)


def test_ok_false_is_a_failure(launcher, tmp_path):
    srv = _serve(tmp_path, tls=True, require=BASIC,
                 reply={"ok": False, "error": "bad shutdown token"})
    try:
        said: list[str] = []
        assert launcher._http_shutdown_request(srv.server_address[1], auth=(USER, PWD),
                                               log=said.append) is False
        assert any("bad shutdown token" in m for m in said)
    finally:
        srv.shutdown()
        srv.server_close()


class _SlowHandler(_Handler):
    """Answers only after the server's ``delay_s`` — the shutdown endpoint replies
    when the service's shutdown hooks have finished, which can outlast the
    launcher's 3 s read timeout."""

    def do_POST(self):  # noqa: N802 — http.server API
        self.server.arrived.append(self.path)     # counted on arrival, before the wait
        time.sleep(self.server.delay_s)
        super().do_POST()


def test_slow_answer_counts_as_delivered_not_refused(launcher, tmp_path):
    """A response that is slower than the client timeout is a shutdown in
    progress. Treating it as a refusal sent the launcher on to taskkill and, 15 s
    later, a hard kill of a service that was still closing its Nanonis link."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    srv.seen, srv.require, srv.reply, srv.delay_s = [], None, {"ok": True}, 4.0
    srv.arrived = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        said: list[str] = []
        assert launcher._http_shutdown_request(srv.server_address[1], log=said.append) is True
        assert srv.arrived == ["/api/admin/shutdown"], (
            "the request must reach the service exactly once — no retry over https")
        assert any("delivered" in m for m in said)
    finally:
        srv.shutdown()
        srv.server_close()


def test_service_bounds_the_graceful_shutdown(launcher):
    """An SSE client still reading keeps its streaming response alive, and uvicorn
    waits for open responses without limit unless told otherwise — the process then
    hangs until the shutdown endpoint's 20 s os._exit backstop. run_service must
    hand uvicorn a bound."""
    import inspect

    src = inspect.getsource(launcher.run_service)
    assert "timeout_graceful_shutdown=" in src


def test_nothing_listening_is_still_a_failure(launcher):
    """Connection refused is not a timeout: nothing received the request."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert launcher._http_shutdown_request(port) is False


def test_loopback_mode_plain_http_still_works(launcher, tmp_path):
    srv = _serve(tmp_path, tls=False, require=None, reply={"ok": True})
    try:
        assert launcher._http_shutdown_request(srv.server_address[1]) is True
        assert "authorization" not in srv.seen[-1]["headers"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_shutdown_token_is_forwarded(launcher, lan_service, monkeypatch):
    monkeypatch.setenv("MAST2_SHUTDOWN_TOKEN", "tok-123")
    assert launcher._http_shutdown_request(lan_service.server_address[1], auth=(USER, PWD))
    assert lan_service.seen[-1]["headers"]["x-mast-shutdown-token"] == "tok-123"


# ── _graceful_stop_proc wiring ───────────────────────────────────────────────

class _FakeProc:
    pid = None                      # no PID → the taskkill branch is skipped

    def __init__(self):
        self.dead = False

    def poll(self):
        return 0 if self.dead else None

    def terminate(self):
        self.dead = True

    def kill(self):
        self.dead = True


def test_saved_lan_credentials_are_used_by_default(launcher, monkeypatch, tmp_path):
    launcher.save_lan_auth(tmp_path, USER, PWD)
    monkeypatch.setattr(launcher, "_user_root", lambda: tmp_path)
    proc, calls = _FakeProc(), []

    def fake(port, *, log=None, auth=None):
        calls.append(auth)
        proc.dead = True
        return True

    monkeypatch.setattr(launcher, "_http_shutdown_request", fake)
    assert launcher._graceful_stop_proc(proc, graceful_timeout=1.0) == "graceful"
    assert calls == [(USER, PWD)]


def test_push_server_stop_does_not_ask_the_main_service_to_exit(launcher, monkeypatch):
    calls: list = []
    monkeypatch.setattr(launcher, "_http_shutdown_request",
                        lambda *a, **k: calls.append((a, k)) or True)
    assert launcher._graceful_stop_proc(_FakeProc(), graceful_timeout=0.4,
                                        http_shutdown=False) == "graceful"
    assert calls == []


def test_launcher_wires_credentials_and_the_push_server_flag():
    src = _LAUNCHER_PATH.read_text(encoding="utf-8")
    assert "self._make_proc_stopper(proc, main=(attr == \"proc\"))" in src
    assert src.count("auth=self._lan_auth_pair()") >= 4   # stop, proc stopper, pid stopper, exit
