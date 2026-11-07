"""End to end (contract section 10): draw a layout in the web API -> the external API and the MCP
tools read the same trace and the same description; an agent's comment shows up for the operator.

The real application (``create_app``: the web routes and the external surface mounted at
``/api/ext/v1``, the access gate, the language middleware) runs under uvicorn on a loopback port; the
MCP server runs as the subprocess Claude Code would start, and talks JSON-RPC to it over stdio.
Assertions go by the files on disk and the other API's answers, the tool text only helps.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ._layouts import comp, det, laser, layout, mirror

_REPO = Path(__file__).resolve().parents[4]
_RUN_SERVER = _REPO / "integrations" / "claude-code" / "server" / "run_server.py"


@pytest.fixture
def served(layout_root, monkeypatch):
    import uvicorn

    monkeypatch.setenv("MAST_LANG", "en")      # the MCP server sends no language header: the process language decides

    from mast.api.app import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(dev_cors=False), host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, name="layout-e2e-uvicorn", daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(20)


def http(base, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={**({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


class Mcp:
    def __init__(self, url: str, tmp: Path):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MAST_")}
        env.update({"MAST_URL": url, "MAST_ACTOR": "e2e", "MAST_SESSION": "s-e2e", "MAST_FETCH_DIR": str(tmp / "fetch"),
                    "MAST_MCP_LOG": "WARNING", "PYTHONIOENCODING": "cp1252"})
        self.p = subprocess.Popen([sys.executable, str(_RUN_SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, cwd=str(tmp))
        self.q: queue.Queue = queue.Queue()
        self.err: list[bytes] = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=lambda: self.err.extend(iter(self.p.stderr.readline, b"")), daemon=True).start()
        self._id = 0

    def _pump(self):
        for line in iter(self.p.stdout.readline, b""):
            if line.strip():
                self.q.put(json.loads(line.decode("utf-8")))

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg).encode("utf-8") + b"\n")
        self.p.stdin.flush()

    def request(self, method, params=None, timeout=90.0):
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"{method}: no answer; stderr:\n" + b"".join(self.err[-30:]).decode("utf-8", "replace"))
            try:
                msg = self.q.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if msg.get("id") == mid:
                return msg

    def call(self, tool, args=None):
        r = self.request("tools/call", {"name": tool, "arguments": args or {}})
        assert "result" in r, r
        res = r["result"]
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        images = [c for c in res.get("content", []) if c.get("type") == "image"]
        return text, bool(res.get("isError")), images

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(30)
        finally:
            if self.p.poll() is None:  # pragma: no cover
                self.p.kill()


@pytest.fixture
def mcp(served, tmp_path):
    m = Mcp(served, tmp_path)
    init = m.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}})
    assert init["result"]["serverInfo"]["name"] == "mast"
    m._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield m
    m.close()


def test_a_layout_drawn_in_the_web_api_is_read_by_the_agent_and_commented_on(served, mcp, layout_root):
    hwp = comp("hwp1", "waveplate", 200, 62.5, 0, label="HWP1", props={"retardance": "half"}, manufacturer="Thorlabs", model="WPH10",
               motorized=True, actuator="K10CR1", connection="COM15")
    drawn = layout([laser("las1", 50, 62.5, label="L1", manufacturer="Cobolt", model="Samba"), hwp,
                    mirror("m1", 312.5, 62.5, 135, label="M1"), det("d", 312.5, 300, 270, label="D1")], lid="shg", title="SHG excitation")
    web = {"Origin": served, "Sec-Fetch-Site": "same-origin", "User-Agent": "browser"}

    # the operator draws it in the page
    status, raw, _ = http(served, "PUT", "/api/optical-layouts/shg", {"layout": drawn, "base_revision": 0, "by": "ana"}, web)
    assert status == 200, raw
    saved = json.loads(raw)
    assert saved["layout"]["revision"] == 1 and len(saved["trace"][0]["branches"][0]["events"]) == 3

    # the external API serves the same layout and the same trace, and a description
    status, raw, _ = http(served, "GET", "/api/ext/v1/optical-layouts/shg", headers={"X-MAST-Actor": "e2e", "X-MAST-Lang": "en"})
    ext = json.loads(raw)
    assert status == 200 and ext["layout"] == saved["layout"] and ext["trace"] == saved["trace"]
    assert "1) L1 laser (532 nm, Cobolt Samba" in ext["summary"] and "motorized: yes, K10CR1, connected to COM15" in ext["summary"]

    # the MCP tool shows the model that description and the geometry; no picture unless asked
    text, err, images = mcp.call("mast_optical_layout")
    assert not err and "1 optical layout(s)" in text and 'shg "SHG excitation": 4 components' in text
    text, err, images = mcp.call("mast_optical_layout", {"layout_id": "shg"})
    assert not err and not images, text[:400]
    assert "Optical layout shg: 4 components, revision 1, 0 open comment(s)" in text
    assert "L1 laser" in text and "ends at D1 detector" in text and '"angle_deg":135.0' in text
    text, err, images = mcp.call("mast_optical_layout", {"layout_id": "shg", "image": True})
    assert not err and len(images) == 1 and images[0]["mimeType"] == "image/png"
    import base64

    assert base64.b64decode(images[0]["data"])[:8] == b"\x89PNG\r\n\x1a\n"

    # the briefing mentions it (and says how to read it)
    text, err, _ = mcp.call("mast_briefing", {"sections": ["optical_layouts"]})
    assert not err and "shg" in text and "mast_optical_layout" in text

    # the agent comments; the operator sees it, with who wrote it
    text, err, _ = mcp.call("mast_optical_layout_comment", {"layout_id": "shg", "text": "HWP1 has no fast axis recorded.", "component_id": "hwp1"})
    assert not err and "Comment c1 added to optical layout shg (about hwp1)" in text, text
    status, raw, _ = http(served, "GET", "/api/optical-layouts/shg/comments", headers=web)
    comments = json.loads(raw)
    assert comments["open_count"] == 1
    c = comments["comments"][0]
    assert (c["author"], c["by"], c["component_id"], c["resolved"]) == ("agent", "ext:e2e", "hwp1", False)
    text, _, _ = mcp.call("mast_optical_layout", {"layout_id": "shg"})
    assert "Unresolved comments (1):" in text and "c1 [agent ext:e2e · hwp1] HWP1 has no fast axis recorded." in text

    # only the operator resolves it
    status, raw, _ = http(served, "POST", "/api/optical-layouts/shg/comments/c1/resolve", {"by": "ana"}, web)
    assert status == 200 and json.loads(raw)["resolved_by"] == "ana"
    text, _, _ = mcp.call("mast_optical_layout", {"layout_id": "shg"})
    assert "Unresolved comments" not in text and "0 open comment(s)" in text

    # and the layout itself was never touched by the agent
    status, raw, _ = http(served, "GET", "/api/optical-layouts/shg", headers=web)
    assert json.loads(raw)["layout"]["revision"] == 1


def test_an_unknown_layout_and_an_installation_without_layouts_are_plain_answers(served, mcp):
    text, err, _ = mcp.call("mast_optical_layout")
    assert not err and "No optical layouts have been drawn on this MAST yet." in text
    text, err, _ = mcp.call("mast_optical_layout", {"layout_id": "nope"})
    assert err and "unknown_layout" in text
    text, err, _ = mcp.call("mast_optical_layout_comment", {"layout_id": "nope", "text": "x"})
    assert err and "unknown_layout" in text
    text, err, _ = mcp.call("mast_briefing")
    assert not err and "## status" in text and "## optical_layouts" not in text, "an empty section is not in the digest"
    assert '"optical_layouts":{"ok":true' in text, "but it is in the data, empty"
