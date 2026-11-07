"""A stand-in for MAST's MCP bridge in the runner tests (standard library only).

Same launch and environment as the real one (``python -I -X utf8 stub_bridge.py`` with
``MAST_URL``, ``MAST_SESSION``, ``MAST_ACTOR``), MCP over stdio, a fixed tool list (``STUB_BRIDGE_TOOLS``,
comma-separated, overrides it). Every ``tools/call`` is forwarded to the test server as
``POST {MAST_URL}/api/ext/v1/_stub/tools/<name>`` with the session headers, and the server's MCP
result is passed back verbatim. Like the real bridge, a MAST address that is not loopback is
refused locally: the call fails without any network traffic.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import urllib.request
from urllib.parse import urlsplit

DEFAULT_TOOLS = ("mast_observe", "mast_act", "mast_run", "mast_wait", "mast_jobs", "mast_read_working",
                 "mast_write_working")


def _tools() -> list[str]:
    raw = os.environ.get("STUB_BRIDGE_TOOLS", "")
    return [t.strip() for t in raw.split(",") if t.strip()] if raw else list(DEFAULT_TOOLS)


def _loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _call(name: str, arguments: dict) -> dict:
    url = os.environ.get("MAST_URL", "").rstrip("/")
    if not _loopback(url):
        return {"content": [{"type": "text", "text": f"{name} did not run: MAST_URL {url!r} is not this machine"}],
                "isError": True}
    request = urllib.request.Request(f"{url}/api/ext/v1/_stub/tools/{name}", method="POST",
                                     data=json.dumps({"arguments": arguments}).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "X-MAST-Session": os.environ.get("MAST_SESSION", ""),
                                              "X-MAST-Actor": os.environ.get("MAST_ACTOR", "")})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"{name} failed: {exc}"}], "isError": True}


def main() -> int:
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        message = json.loads(raw.decode("utf-8"))
        mid, method = message.get("id"), message.get("method")
        if mid is None:
            continue
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "mast", "version": "stub"}}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": f"stub {n}",
                                 "inputSchema": {"type": "object", "properties": {}}} for n in _tools()]}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = _call(str(params.get("name")), params.get("arguments") or {})
        else:
            out.write((json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": method}})
                       + "\n").encode("utf-8"))
            out.flush()
            continue
        out.write((json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n").encode("utf-8"))
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
