"""A scripted MCP client drives MAST 7.0 on the simulator: the whole VISTA chain, no model.

Real MAST service on stmsim (``sim_service``, a child process) + MAST's own MCP bridge as a
second child (``integrations/claude-code/server/run_server.py``) spoken to over stdio JSON-RPC,
exactly as Codex or Claude Code would. One episode, in order:

1. the tool list is the 7.0 surface;
2. ``mast_observe`` hands out the first token;
3. ``mast_act`` scan with that token comes back with the new frame AS AN IMAGE and a new token;
4. the magnifier and a ruler work on the archived frame;
5. an act with a spent token is refused (``observe_first``) and the refusal carries the newest
   observation with an image and a fresh token;
6. two acts from one Codex code cell (same ``_meta.itemId``): the second is refused
   (``same_code_cell``);
7. ``mast_wait`` on a timer returns;
8. an analysis detector runs on the next scan in pyexec and its hit wakes ``mast_wait``;
9. history and WORKING.md;
10. after ``mast_emergency_stop`` an act does not execute.

Opt-in (``MAST_E2E_SIM=1``) and STM-Bench on ``PYTHONPATH``: see ``conftest.py``.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e_sim

_RUN_SERVER = (Path(__file__).resolve().parents[3] / "integrations" / "claude-code" / "server"
               / "run_server.py")
_TOKEN = re.compile(r'"next_token":"([A-Z0-9]{4}-[A-Z0-9]{4})"')


class Mcp:
    """A minimal MCP client over the bridge's stdio."""

    def __init__(self, url: str, tmp: Path):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MAST_")}
        env.update({"MAST_URL": url, "MAST_ACTOR": "e2e-sim", "MAST_SESSION": "s-e2e-sim",
                    "MAST_FETCH_DIR": str(tmp / "fetch"), "MAST_MCP_LOG": "WARNING",
                    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        self.p = subprocess.Popen([sys.executable, "-I", "-X", "utf8", str(_RUN_SERVER)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, cwd=str(tmp))
        self.q: queue.Queue = queue.Queue()
        self.err: list[bytes] = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=lambda: self.err.extend(iter(self.p.stderr.readline, b"")),
                         daemon=True).start()
        self._id = 0

    def _pump(self):
        for line in iter(self.p.stdout.readline, b""):
            if line.strip():
                self.q.put(json.loads(line.decode("utf-8")))

    def send(self, msg: dict) -> None:
        self.p.stdin.write(json.dumps(msg).encode("utf-8") + b"\n")
        self.p.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 120.0) -> dict:
        self._id += 1
        mid = self._id
        self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"{method}: no answer; bridge stderr tail:\n"
                                     + b"".join(self.err[-30:]).decode("utf-8", "replace"))
            try:
                msg = self.q.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if msg.get("id") == mid:
                return msg

    def call(self, tool: str, args: dict | None = None, *, meta: dict | None = None,
             timeout: float = 180.0) -> tuple[str, list[dict], bool]:
        params: dict = {"name": tool, "arguments": args or {}}
        if meta:
            params["_meta"] = meta
        r = self.request("tools/call", params, timeout)
        assert "result" in r, f"{tool}: protocol error instead of a tool result: {r}"
        res = r["result"]
        assert "structuredContent" not in res          # Codex drops images when it is present
        content = res.get("content", [])
        text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        images = [c for c in content if c.get("type") == "image"]
        return text, images, bool(res.get("isError"))

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(30)
        finally:
            if self.p.poll() is None:  # pragma: no cover
                self.p.kill()


@pytest.fixture(scope="module")
def mcp(sim_service, tmp_path_factory):
    m = Mcp(sim_service.base_url, tmp_path_factory.mktemp("mcp"))
    init = m.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "e2e-sim", "version": "7.0"}})
    assert init["result"]["serverInfo"]["name"] == "mast"
    m.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield m
    m.close()


def _token(text: str) -> str:
    m = _TOKEN.search(text)
    assert m, f"no next_token in: {text[:600]}"
    return m.group(1)


def _png(images: list[dict]) -> None:
    assert images, "expected an image block"
    for im in images:
        assert im.get("mimeType") == "image/png" and len(im.get("data", "")) > 1000


SCAN = {"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 20, "pixels": 64, "line_time_s": 0.1}


def test_a_whole_vista_episode_over_mcp(mcp, sim_service):
    tools = {t["name"] for t in mcp.request("tools/list")["result"]["tools"]}
    for name in ("mast_observe", "mast_act", "mast_wait", "mast_inspect", "mast_read_values",
                 "mast_line_profile", "mast_fft_peaks", "mast_detect_blobs", "mast_diff",
                 "mast_stats", "mast_history", "mast_write_working", "mast_detector_create",
                 "mast_detectors", "mast_literature_search", "mast_run", "mast_emergency_stop"):
        assert name in tools, name

    # 2. the first token
    text, images, err = mcp.call("mast_observe")
    assert not err, text[:800]
    t0 = _token(text)

    # 3. one scan: the frame comes back as an image, with the next token
    text, images, err = mcp.call("mast_act", {"action": "scan", "params": SCAN, "after": t0},
                                 meta={"itemId": "ctc_e2e_1"})
    assert not err, text[:1200]
    assert "t0001.s0" in text and '"origin":"act"' in text, text[:1200]
    _png(images)
    t1 = _token(text)
    assert t1 != t0

    # 4. magnifier and a ruler on the archived frame
    text, images, err = mcp.call("mast_inspect", {"question": "whole frame", "views": [
        {"label": "whole", "frame": "t0001.s0"}]})
    assert not err, text[:800]
    _png(images)
    text, _, err = mcp.call("mast_stats", {"frame": "t0001.s0"})
    assert not err and "mean_pm" in text, text[:800]

    # 5. a spent token: refused, with the newest observation and a fresh token
    text, images, err = mcp.call("mast_act", {"action": "set_bias", "params": {"bias_v": 0.2},
                                              "after": t0}, meta={"itemId": "ctc_e2e_2"})
    # not an error result: its image (the newest observation) must reach the model
    assert not err and text.startswith("Refused (observe_first)"), text[:800]
    _png(images)
    t2 = _token(text)

    # 6. one act per code cell
    text, _, err = mcp.call("mast_act", {"action": "set_bias", "params": {"bias_v": 0.2},
                                         "after": t2}, meta={"itemId": "ctc_e2e_3"})
    assert not err, text[:800]
    t3 = _token(text)
    text, _, err = mcp.call("mast_act", {"action": "set_bias", "params": {"bias_v": 0.3},
                                         "after": t3}, meta={"itemId": "ctc_e2e_3"})
    assert err and "same_code_cell" in text, text[:800]

    # 7. a timer wait returns
    text, _, err = mcp.call("mast_wait", {"conditions": [{"type": "timer", "seconds": 2}],
                                          "timeout_s": 30}, timeout=90)
    assert not err and "timer" in text, text[:800]

    # 8. a detector on the next scan wakes a wait
    code = ("def detect(frame):\n"
            "    z = frame.channels.get('Z')\n"
            "    return {'hit': z is not None, 'message': 'frame ' + frame.fid, 'value': 1}\n")
    text, _, err = mcp.call("mast_detector_create", {"name": "every-scan", "code": code,
                                                     "applies_to": ["scan"]})
    assert not err, text[:800]
    text, images, err = mcp.call("mast_act", {"action": "scan", "params": SCAN, "after": t3},
                                 meta={"itemId": "ctc_e2e_4"})
    assert not err, text[:1200]
    _png(images)
    text, _, err = mcp.call("mast_wait", {"conditions": [{"type": "detector"}],
                                          "timeout_s": 180}, timeout=300)
    assert not err and "detector" in text and "frame t" in text, text[:1200]

    # 9. history and the working notes
    text, _, err = mcp.call("mast_history", {"view": "events"})
    assert not err and '"kind":"act"' in text and "detector_hit" in text, text[:1500]
    text, _, err = mcp.call("mast_write_working", {"content": "two scans of Au(111); detector ok"})
    assert not err, text[:400]
    text, _, err = mcp.call("mast_read_working")
    assert "detector ok" in text, text[:400]

    # 10. after the emergency stop an act does not execute
    tok = _token(mcp.call("mast_observe")[0])
    text, _, err = mcp.call("mast_emergency_stop", {"reason": "e2e test"})
    assert not err, text[:600]
    text, _, err = mcp.call("mast_act", {"action": "set_bias", "params": {"bias_v": 0.4},
                                         "after": tok}, meta={"itemId": "ctc_e2e_5"})
    assert err or '"ok":false' in text, text[:800]
