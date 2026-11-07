"""MCP over streamable HTTP, for an AI on another computer (MAST's remote AI access).

Every JSON-RPC message is POSTed and answered with a plain ``application/json``
response. There are no sessions and no server-sent events; GET answers 405, as
the specification allows. That is the smallest form of the transport, and the
one both Claude Code and Codex handle. Such a client stops waiting for an answer
after about 60 s, so the tool providers behind this module end every call
sooner (``Toolbox(call_ceiling_s=...)``, ``computer.ComputerTools``).

Who may talk to the server is settled before any JSON is read, in this order:

* a request that carries ``Origin`` is refused (403). Browsers send it and MCP
  clients do not, so a web page can never drive this server;
* with ``tailnet_only``, a sender outside Tailscale's ``100.64.0.0/10`` other
  than this machine is refused (403), even if it reached the port somehow;
* the key is the last path segment (``/mcp/<key>``, what the connection lines
  use, so that no header has to be configured) or a bearer token on ``/mcp``. A
  wrong key answers 404, not 401 or 403: MCP clients answer those two with an
  OAuth login, which cannot work here.

``stdio_rpc`` and this module are the only two that know the wire protocol. The
tool results are built the same way (``stdio_rpc._call_result``): a text block,
then each image with its label, never ``structuredContent``.

The ``backend`` decides everything that is not protocol. It provides
``check_key(key)``, ``peer_name(address)``, ``note(peer, kind, detail)`` (kinds
``connected`` and ``badkey``, for the activity log), ``initialize(peer,
client_info) -> {"serverInfo", "instructions"}``, ``list_tools(peer)``,
``has_tool(peer, name)``, ``call_tool(peer, name, arguments, signal, meta)``,
and the guide resources (``list_resources()``, ``list_templates()``,
``read_resource(uri)``).
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .stdio_rpc import (INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND,
                        PARSE_ERROR, RESOURCE_NOT_FOUND, CallSignal, RpcError, _call_result,
                        _error, _id_key, _tool_result)

log = logging.getLogger("mast_mcp.http")

#: Newest first. Claude Code sends 2025-11-25, Codex 2025-06-18; an unknown version
#: is answered with the newest, and the client decides.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

MAX_BODY = 32 * 1024 * 1024
#: Tool calls in flight at once, all clients together; more get a tool error.
MAX_CONCURRENT_CALLS = 24
#: Seconds a connection may sit between two requests, or take to send one.
SOCKET_TIMEOUT_S = 30
#: Ports tried after the preferred one when it is taken.
PORT_TRIES = 20

_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

BAD_KEY_TEXT = ("MAST remote AI: wrong key. The connection lines were changed; copy the new two "
                "lines from the Remote AI section of the MAST launcher on that computer.")
NOT_POST_TEXT = ("MAST remote AI: this is an MCP endpoint (streamable HTTP). Add it to Claude Code "
                 "or Codex as an HTTP MCP server; it only answers POST requests.")


@dataclass(frozen=True)
class Peer:
    """The computer a request comes from."""
    address: str
    name: str


def from_tailnet(address: str) -> bool:
    """A Tailscale address (``100.64.0.0/10``) or this machine."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or (ip.version == 4 and ip in _TAILSCALE_NET)


def bearer_key(header: str | None) -> str:
    value = (header or "").strip()
    return value[7:].strip() if value[:7].lower() == "bearer " else ""


def negotiate_version(requested) -> str:
    return requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]


def _encode(obj) -> bytes:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                          default=str).encode("utf-8")
    except UnicodeEncodeError:       # a lone surrogate a client sent us
        return json.dumps(obj, ensure_ascii=True, separators=(",", ":"),
                          default=str).encode("ascii")


class Dispatcher:
    """JSON-RPC and the MCP methods, independent of the HTTP plumbing (tested alone)."""

    def __init__(self, backend):
        self.backend = backend
        self.draining = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
        self._inflight: dict[tuple[str, str], CallSignal] = {}
        self._lock = threading.Lock()

    def handle_body(self, body: bytes, peer: Peer) -> tuple[int, bytes | None]:
        """The HTTP status and the response body (None: 202 Accepted, nothing to say)."""
        try:
            msg = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return 400, _encode(_error(None, PARSE_ERROR, f"Parse error: {exc}"))
        if isinstance(msg, list):
            # Batches were dropped in 2025-06-18; an older client may still send one.
            if not msg:
                return 400, _encode(_error(None, INVALID_REQUEST, "Invalid request: empty batch"))
            replies = [r for r in (self.handle_message(m, peer) for m in msg) if r is not None]
            return (200, _encode(replies)) if replies else (202, None)
        reply = self.handle_message(msg, peer)
        return (200, _encode(reply)) if reply is not None else (202, None)

    def handle_message(self, msg, peer: Peer):
        if not isinstance(msg, dict):
            return _error(None, INVALID_REQUEST, "Invalid request: not a JSON object")
        method = msg.get("method")
        if method is None:
            return None                  # a response to a request this server never sends
        has_id = "id" in msg
        mid = msg.get("id")
        if not isinstance(method, str):
            return _error(mid, INVALID_REQUEST, "Invalid request: method must be a string") \
                if has_id else None
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(mid, INVALID_PARAMS, "Invalid params: params must be an object") \
                if has_id else None
        if not has_id:
            self._notification(method, params, peer)
            return None
        try:
            result = self._request(method, params, peer, mid)
        except RpcError as exc:
            return _error(mid, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001 - a bug here must not kill the connection
            log.exception("internal error while handling %s", method)
            return _error(mid, INTERNAL_ERROR, f"Internal error: {exc}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _request(self, method: str, params: dict, peer: Peer, mid):
        backend = self.backend
        if method == "initialize":
            client = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
            client_info = {k: client[k] for k in ("name", "title", "version")
                           if isinstance(client.get(k), str)} or None
            version = negotiate_version(params.get("protocolVersion"))
            label = " ".join(v for v in ((client_info or {}).get("name"),
                                         (client_info or {}).get("version")) if v)
            backend.note(peer, "connected", label or "MCP client")
            about = backend.initialize(peer, client_info)
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False},
                                 "resources": {"subscribe": False, "listChanged": False}},
                "serverInfo": about["serverInfo"],
                "instructions": about["instructions"],
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": backend.list_tools(peer)}
        if method == "tools/call":
            return self._call(params, peer, mid)
        if method == "resources/list":
            return {"resources": backend.list_resources()}
        if method == "resources/templates/list":
            return {"resourceTemplates": backend.list_templates()}
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri:
                raise RpcError(INVALID_PARAMS, "resources/read needs a uri")
            try:
                item = backend.read_resource(uri)
            except KeyError:
                raise RpcError(RESOURCE_NOT_FOUND, f"Resource not found: {uri}",
                               {"uri": uri}) from None
            return {"contents": [item]}
        # Includes the server/discover probe of newer Claude Code versions, which then
        # falls back to initialize.
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _notification(self, method: str, params: dict, peer: Peer) -> None:
        if method == "notifications/cancelled":
            with self._lock:
                signal = self._inflight.get((peer.address, _id_key(params.get("requestId"))))
            if signal is not None:
                signal.cancelled.set()

    def _call(self, params: dict, peer: Peer, mid) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RpcError(INVALID_PARAMS, "tools/call needs a tool name")
        if not self.backend.has_tool(peer, name):
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "tools/call arguments must be an object")
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
        if not self._slots.acquire(blocking=False):
            return _tool_result(f"This MAST computer is already running {MAX_CONCURRENT_CALLS} "
                                "tool calls; wait for some of them to finish and try again.", True)
        signal = CallSignal(self.draining)
        key = (peer.address, _id_key(mid))
        with self._lock:
            self._inflight[key] = signal
        try:
            return _call_result(self.backend.call_tool(peer, name, arguments, signal, meta))
        except Exception as exc:  # noqa: BLE001 - becomes a tool error, never a crash
            log.exception("tool %s raised", name)
            return _tool_result(f"Internal error in the MAST remote AI server while running "
                                f"{name}: {exc!r}", True)
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            self._slots.release()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S
    server_version = "MAST-remote-AI"
    sys_version = ""
    # Set on the subclass that HttpMcpServer makes.
    owner: "HttpMcpServer" = None  # type: ignore[assignment]

    def log_message(self, format, *args):  # noqa: A002 - the base class's name
        log.debug("%s %s", self.address_string(), format % args)

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8",
              headers: dict | None = None) -> None:
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, status: int, text: str, headers: dict | None = None) -> None:
        self._send(status, (text + "\n").encode("utf-8"), headers=headers)

    def _admit(self) -> Peer | None:
        """Origin, sender and key; None once a refusal (or the health answer) has been sent."""
        owner = self.owner
        route = unquote(urlsplit(self.path).path)
        if route.rstrip("/") == "/health":
            self._text(200, owner.health_text)
            return None
        if self.headers.get("Origin"):
            self._text(403, "MAST remote AI: requests from web pages are not allowed.")
            return None
        address = self.client_address[0]
        if owner.tailnet_only and not from_tailnet(address):
            self._text(403, "MAST remote AI: only reachable through Tailscale.")
            return None
        if route == "/mcp" or route == "/mcp/":
            key = bearer_key(self.headers.get("Authorization"))
        elif route.startswith("/mcp/") and route.count("/") == 2:
            key = route[len("/mcp/"):]
        else:
            self._text(404, "MAST remote AI: not found. The MCP endpoint is /mcp/<key>.")
            return None
        peer = Peer(address, owner.peer_name(address))
        if not owner.backend.check_key(key):
            owner.bad_key(peer)
            self._text(404, BAD_KEY_TEXT)
            return None
        return peer

    def do_POST(self):  # noqa: N802 - http.server's naming
        peer = self._admit()
        if peer is None:
            return
        length = self.headers.get("Content-Length")
        if length is None:
            self._text(411, "MAST remote AI: Content-Length is required.")
            return
        try:
            size = int(length)
        except ValueError:
            self._text(400, "MAST remote AI: bad Content-Length.")
            return
        if size < 0 or size > MAX_BODY:
            self.close_connection = True
            self._text(413, "MAST remote AI: request too large.")
            return
        body = self.rfile.read(size)
        status, out = self.owner.dispatcher.handle_body(body, peer)
        if out is None:
            self._send(status)
        else:
            self._send(status, out, "application/json")

    def _not_post(self):
        if self._admit() is None:
            return
        self._text(405, NOT_POST_TEXT, headers={"Allow": "POST"})

    do_GET = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _not_post  # noqa: N815


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket bind a port that is in use, so the
    # next-free-port search would never move on; there the port is taken exclusively.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class HttpMcpServer:
    """The listening socket and its threads. ``start()`` binds, ``stop()`` ends everything."""

    def __init__(self, backend, *, host: str, port: int, exact_port: bool = False,
                 tailnet_only: bool = True, health_text: str = "mast-remote-ai ok"):
        self.backend = backend
        self.host = host
        self.preferred_port = int(port)
        self.exact_port = exact_port
        self.tailnet_only = tailnet_only
        self.health_text = health_text
        self.dispatcher = Dispatcher(backend)
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None
        self._bad_key_at = 0.0

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else 0

    def start(self) -> int:
        """Bind (trying the next ports unless ``exact_port``) and serve; returns the port."""
        handler = type("Handler", (_Handler,), {"owner": self})
        tries = 1 if self.exact_port else PORT_TRIES
        first_error: OSError | None = None
        for i in range(tries):
            port = self.preferred_port + i
            if port > 65535:
                break
            try:
                self._httpd = _Server((self.host, port), handler)
                break
            except OSError as exc:
                first_error = first_error or exc
        if self._httpd is None:
            raise first_error or OSError(f"no free port from {self.preferred_port}")
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.25},
                                        name="mast-remote-ai-http", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        """Stop accepting, tell running calls to wrap up, close the socket."""
        self.dispatcher.draining.set()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(5.0)
            self._thread = None

    def peer_name(self, address: str) -> str:
        """What the activity log calls ``address``: the backend's name for it, or the address."""
        try:
            return self.backend.peer_name(address) or address
        except Exception:  # noqa: BLE001 - a name is only a nicety for the activity log
            return address

    def bad_key(self, peer: Peer) -> None:
        """Note a wrong key in the activity log, at most once a minute."""
        now = time.monotonic()
        if now - self._bad_key_at < 60.0 and self._bad_key_at:
            return
        self._bad_key_at = now
        try:
            self.backend.note(peer, "badkey", "")
        except Exception:  # noqa: BLE001
            log.exception("activity note failed")


def check_key(expected: str, given: str) -> bool:
    """Constant-time comparison; an empty key never matches."""
    return bool(expected) and bool(given) and hmac.compare_digest(
        expected.encode("utf-8"), given.encode("utf-8"))
