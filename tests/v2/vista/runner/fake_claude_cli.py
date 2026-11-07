"""A fake ``claude -p`` for the MAST runner tests (standard library only; never calls a model).

Ported from VISTA-SPM ``tests/fake_claude.py``. It speaks stream-json on stdin/stdout the way
Claude Code 2.1.295 does and drives the REAL MCP server and hook
script the runner configures: the one server in ``--mcp-config`` is started and initialised over
stdio, and the PreCompact/Stop hooks in ``--settings`` run with the documented input (so they
reach MAST's hook route over HTTP).

Scenario: ``FAKE_CLAUDE_SCENARIO`` = path of a JSON file ``{"sessions": [[turn, ...], ...]}``;
process number k (counted in ``<scenario>.count``) plays ``sessions[k]`` and writes what it saw
(argv, a few environment facts) to ``<scenario>.proc<k>.json``. A user message starts the next
turn; a turn is a list of steps (or ``{"steps": [...], "on_block": [...]}``):

* ``{"tool": name, "args": {...}}`` — assistant ``tool_use``, MCP ``tools/call``, then the ``user``
  ``tool_result`` (images as base64 ``source`` blocks, like Claude Code). With ``"hold":
  "interrupt"`` the result is never emitted and the step waits for an interrupt (a call still
  running when the turn is cut); with ``"hold": "exit"`` the process dies right after the call.
* ``{"tools": [{"tool":..., "args":...}, ...]}`` — parallel tool uses in one message.
* ``{"text": "..."}`` — assistant text (the last one becomes the result text).
* ``{"precompact": true}`` — run the PreCompact hook; if it does not block, emit a
  ``compact_boundary`` (a native compaction) and run PostCompact.
* ``{"compact_boundary": true}`` — emit a native ``compact_boundary`` without any hook.
* ``{"wait_checkpoint": state}`` — poll the test server until the session's checkpoint is
  ``state`` (a test-only route; it stands for the model thinking long enough for the runner).
* ``{"sleep": s}``, ``{"exit": code}`` (crash), ``{"wait_interrupt": s}`` (block until an
  interrupt control request arrives).
* ``{"result": {...}}`` — fields merged into this turn's result event.
* ``{"extra_tools": [...]}`` / ``{"api_key_source": "..."}`` — this turn's ``system/init``.

At the end of a turn the Stop hook runs; when it blocks, the fake emits the synthetic
"Stop hook feedback" user message and plays the turn's ``on_block`` steps (default: write
WORKING.md), at most 8 times. A user message beyond the script gets ``(no more script)``.
An interrupt aborts the running turn with a ``result`` of subtype ``error_during_execution``
whose cumulative cost INCLUDES the aborted turn, as Claude Code's does (VISTA's fake reported
the cost without it, which made a cost assertion depend on whether the runner's interrupt or the
turn's own end came first). EOF on stdin ends the process (code 0).
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid

MODEL = "claude-fake-1"
STEP_DELAY = float(os.environ.get("FAKE_CLAUDE_STEP_DELAY", "0.05"))
DEFAULT_ON_BLOCK = [{"tool": "mast_write_working", "args": {"content": "Plan: keep going. State saved by the fake."}},
                    {"text": "WORKING.md saved."}]
_EMIT_LOCK = threading.Lock()


def emit(obj: dict) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with _EMIT_LOCK:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def arg_value(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


class Mcp:
    """Minimal MCP client over stdio for one server."""

    def __init__(self, entry: dict) -> None:
        env = dict(os.environ)
        env.update(entry.get("env") or {})
        self.env = env
        self.proc = subprocess.Popen([entry["command"], *entry.get("args", [])], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.next_id = 0
        init = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "fake-claude", "version": "0"}})
        self.protocol = init.get("result", {}).get("protocolVersion")
        self.notify("notifications/initialized", {})
        listing = self.request("tools/list", {})
        self.tools = [t["name"] for t in listing.get("result", {}).get("tools", [])]

    def notify(self, method: str, params: dict) -> None:
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        rid = self.next_id
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                           "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return {"error": {"message": "MCP server closed stdout"}}
            message = json.loads(line.decode("utf-8"))
            if message.get("id") == rid:
                return message

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


class Fake:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.session_id = arg_value(argv, "--session-id") or str(uuid.uuid4())
        with open(arg_value(argv, "--settings"), encoding="utf-8") as fh:
            self.settings = json.load(fh)
        with open(arg_value(argv, "--mcp-config"), encoding="utf-8") as fh:
            self.mcp_config = json.load(fh)
        scenario_path = os.environ["FAKE_CLAUDE_SCENARIO"]
        with open(scenario_path, encoding="utf-8") as fh:
            sessions = json.load(fh)["sessions"]
        counter = scenario_path + ".count"
        index = int(open(counter).read()) if os.path.exists(counter) else 0
        with open(counter, "w") as fh:
            fh.write(str(index + 1))
        self.turns = sessions[index] if index < len(sessions) else []
        ((self.server_name, entry),) = self.mcp_config["mcpServers"].items()
        with open(f"{scenario_path}.proc{index}.json", "w", encoding="utf-8") as fh:
            json.dump({"argv": argv, "cwd": os.getcwd(),
                       "env_has": {k: k in os.environ for k in (
                           "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE",
                           "CLAUDE_CODE_ENTRYPOINT", "DISABLE_AUTO_COMPACT", "MAST_RUNNER_SECRET",
                           "MAST_RUNNER_HOOK_URL", "MAST_RUNNER_HOOK_DIR")},
                       "env": {k: os.environ.get(k) for k in (
                           "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "ENABLE_CLAUDEAI_MCP_SERVERS",
                           "MCP_CONNECTION_NONBLOCKING", "MAST_RUNNER_HOOK_URL")},
                       "mcp_env": entry.get("env")}, fh)
        self.mcp = Mcp(entry)
        self.inbox: "queue.Queue[dict | None]" = queue.Queue()
        self.interrupted = False
        self.cost = 0.0
        self.result_index = 0
        self.turn_no = 0
        threading.Thread(target=self._read_stdin, daemon=True).start()

    def _read_stdin(self) -> None:
        for raw in sys.stdin.buffer:
            raw = raw.strip()
            if not raw:
                continue
            message = json.loads(raw.decode("utf-8"))
            if message.get("type") == "control_request":
                if message.get("request", {}).get("subtype") == "interrupt":
                    self.interrupted = True
                emit({"type": "control_response", "response": {"subtype": "success",
                                                               "request_id": message.get("request_id"),
                                                               "response": {}}})
                continue
            self.inbox.put(message)
        self.inbox.put(None)

    # ── events ───────────────────────────────────────────────────────────────
    def init(self, extra: list[str], api_key_source: str) -> None:
        emit({"type": "system", "subtype": "init", "session_id": self.session_id, "model": MODEL,
              "tools": [f"mcp__{self.server_name}__{t}" for t in self.mcp.tools] + list(extra),
              "mcp_servers": [{"name": self.server_name, "status": "connected"}], "permissionMode": "default",
              "apiKeySource": api_key_source, "claude_code_version": "0.0.0-fake", "skills": [],
              "plugins": [], "slash_commands": []})

    def assistant(self, blocks: list[dict]) -> None:
        emit({"type": "assistant", "message": {"model": MODEL, "role": "assistant", "content": blocks},
              "parent_tool_use_id": None, "session_id": self.session_id})

    def call_tools(self, calls: list[dict]) -> bool:
        """False when the turn must stop (a held call was interrupted)."""
        uses = [{"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}",
                 "name": f"mcp__{self.server_name}__{c['tool']}", "input": c.get("args", {})} for c in calls]
        self.assistant(uses)
        results = []
        for use, call in zip(uses, calls):
            response = self.mcp.request("tools/call", {"name": call["tool"], "arguments": call.get("args", {})})
            if call.get("hold") == "exit":
                os._exit(3)
            if call.get("hold") == "interrupt":
                while not self.interrupted:
                    time.sleep(0.02)
                return False
            result = response.get("result") or {"content": [{"type": "text", "text": json.dumps(response.get("error"))}],
                                                "isError": True}
            content = []
            for block in result.get("content", []):
                if block.get("type") == "image":
                    content.append({"type": "image", "source": {"type": "base64", "media_type": block.get("mimeType"),
                                                                "data": block.get("data")}})
                else:
                    content.append(block)
            results.append({"type": "tool_result", "tool_use_id": use["id"], "content": content,
                            "is_error": bool(result.get("isError"))})
        emit({"type": "user", "message": {"role": "user", "content": results}, "parent_tool_use_id": None,
              "session_id": self.session_id})
        return True

    def hook(self, event_name: str, extra: dict) -> dict:
        groups = self.settings.get("hooks", {}).get(event_name) or []
        output: dict = {}
        for group in groups:
            for hook in group.get("hooks", []):
                event = {"session_id": self.session_id, "transcript_path": "fake.jsonl", "cwd": os.getcwd(),
                         "prompt_id": str(uuid.uuid4()), "hook_event_name": event_name, **extra}
                proc = subprocess.run([hook["command"], *hook.get("args", [])], input=json.dumps(event).encode(),
                                      capture_output=True, timeout=hook.get("timeout", 30))
                if event_name == "Stop":
                    emit({"type": "system", "subtype": "hook_response", "hook_event": "Stop",
                          "output": proc.stdout.decode("utf-8", "replace"), "exit_code": proc.returncode})
                if proc.returncode == 2:
                    return {"decision": "block", "reason": proc.stderr.decode("utf-8", "replace")}
                text = proc.stdout.decode("utf-8", "replace").strip()
                if text:
                    output = json.loads(text)
        return output

    def wait_checkpoint(self, state: str, timeout: float) -> None:
        env = self.mcp.env
        url = (env["MAST_URL"].rstrip("/") + "/api/ext/v1/_stub/checkpoint?"
               + urllib.parse.urlencode({"session": env.get("MAST_SESSION", "")}))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.interrupted:
            with opener.open(url, timeout=5) as response:
                if json.loads(response.read().decode("utf-8")).get("state") == state:
                    return
            time.sleep(0.05)

    def result(self, text: str, overrides: dict, num_turns: int) -> None:
        self.cost += 0.01
        message = {"type": "result", "subtype": "success", "is_error": False, "result": text,
                   "num_turns": num_turns, "total_cost_usd": round(self.cost, 6), "session_id": self.session_id,
                   "result_index": self.result_index, "terminal_reason": "completed", "stop_reason": "end_turn",
                   "api_error_status": None,
                   "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0},
                   "modelUsage": {MODEL: {"inputTokens": 10 * (self.result_index + 1),
                                          "outputTokens": 5 * (self.result_index + 1),
                                          "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                                          "costUSD": round(self.cost, 6)}}}
        message.update(overrides)
        if (message.get("subtype") != "success" or message.get("is_error")) and "result" not in overrides:
            message.pop("result", None)
        self.result_index += 1
        emit(message)

    def abort_turn(self) -> None:
        self.interrupted = False
        self.cost += 0.005                     # the aborted turn was paid for, too
        emit({"type": "user", "message": {"role": "user", "content": [{"type": "text",
              "text": "[Request interrupted by user]"}]}, "parent_tool_use_id": None})
        self.result_index += 1
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 1,
              "total_cost_usd": round(self.cost, 6), "session_id": self.session_id,
              "result_index": self.result_index - 1, "terminal_reason": "aborted_streaming",
              "errors": ["interrupted"], "usage": {},
              "modelUsage": {MODEL: {"inputTokens": 10 * self.result_index, "outputTokens": 5 * self.result_index,
                                     "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                                     "costUSD": round(self.cost, 6)}}})

    # ── turns ────────────────────────────────────────────────────────────────
    def play_steps(self, steps: list[dict], state: dict) -> bool:
        """False if the turn was aborted (interrupt) — the caller then stops the turn."""
        for step in steps:
            if self.interrupted:
                self.abort_turn()
                return False
            if "tool" in step:
                if not self.call_tools([step]):
                    self.abort_turn()
                    return False
            elif "tools" in step:
                self.call_tools(step["tools"])
            elif "text" in step:
                state["text"] = step["text"]
                self.assistant([{"type": "text", "text": step["text"]}])
            elif "precompact" in step:
                emit({"type": "system", "subtype": "status", "status": "compacting"})
                out = self.hook("PreCompact", {"trigger": "auto", "custom_instructions": None})
                emit({"type": "system", "subtype": "status", "status": None})
                if out.get("decision") != "block":
                    emit({"type": "system", "subtype": "compact_boundary",
                          "compact_metadata": {"trigger": "auto", "pre_tokens": 1000, "post_tokens": 100}})
                    self.hook("PostCompact", {"trigger": "auto", "compact_summary": "fake summary"})
            elif "compact_boundary" in step:
                emit({"type": "system", "subtype": "compact_boundary",
                      "compact_metadata": {"trigger": "auto", "pre_tokens": 1000, "post_tokens": 100}})
            elif "wait_checkpoint" in step:
                self.wait_checkpoint(str(step["wait_checkpoint"]), float(step.get("timeout", 15)))
            elif "sleep" in step:
                time.sleep(float(step["sleep"]))
            elif "exit" in step:
                os._exit(int(step["exit"]))
            elif "wait_interrupt" in step:
                deadline = time.monotonic() + float(step["wait_interrupt"])
                while time.monotonic() < deadline and not self.interrupted:
                    time.sleep(0.02)
            elif "result" in step:
                state["result"] = step["result"]
            time.sleep(STEP_DELAY)
        if self.interrupted:
            self.abort_turn()
            return False
        return True

    def play_turn(self, turn: dict | list) -> None:
        if isinstance(turn, list):
            turn = {"steps": turn}
        extra: list[str] = []
        api_key_source = "none"
        for step in turn.get("steps", []):
            if "extra_tools" in step:
                extra = list(step["extra_tools"])
            if "api_key_source" in step:
                api_key_source = str(step["api_key_source"])
        self.init(extra, api_key_source)
        state: dict = {"text": "", "result": {}}
        steps = [s for s in turn.get("steps", []) if "extra_tools" not in s and "api_key_source" not in s]
        if not self.play_steps(steps, state):
            return
        model_turns = 1
        for _ in range(8):
            decision = self.hook("Stop", {"permission_mode": "default", "effort": {"level": "low"},
                                          "stop_hook_active": model_turns > 1,
                                          "last_assistant_message": state["text"]})
            if decision.get("decision") != "block":
                break
            emit({"type": "user", "message": {"role": "user", "content": [{"type": "text",
                  "text": "Stop hook feedback:\n" + decision.get("reason", "")}]}, "isSynthetic": True})
            model_turns += 1
            if not self.play_steps(turn.get("on_block", DEFAULT_ON_BLOCK), state):
                return
        self.result(state["text"], state["result"], model_turns)

    def run(self) -> int:
        while True:
            message = self.inbox.get()
            if message is None:
                break
            if message.get("type") != "user":
                continue
            turn = self.turns[self.turn_no] if self.turn_no < len(self.turns) else [{"text": "(no more script)"}]
            self.turn_no += 1
            self.play_turn(turn)
        self.mcp.close()
        return 0


if __name__ == "__main__":
    sys.exit(Fake(sys.argv[1:]).run())
