"""Pure parts of the Claude runner: configuration, child environment, command line, init
validation, result classification, end markers, the hook decision and transport, path resolution."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import vista_runner_kit  # noqa: F401  (puts MASTv2 on sys.path)

from mast.vista.runner import compact_hook, paths
from mast.vista.runner.claude import (
    RunnerConfig, auth_problem, bridge_env, build_command, child_environment, classify_result, hook_settings,
    marker_outcome, mcp_config, result_usage, sanitize, user_message, validate_init)
from mast.vista.runner.prompts import SYSTEM_PROMPT, initial_message, recovery_message


def env_for(base: dict, cfg: RunnerConfig, tmp: Path) -> dict:
    return child_environment(base, cfg, hook_url="http://127.0.0.1:1/api/ext/v1/runner/r/hook",
                             hook_secret="s3cret", hook_dir=tmp)


def test_config_validation_and_summary():
    for bad in ({"effort": "huge"}, {"compact_window_tokens": 50_000}, {"compact_pct": 0}, {"auth": "apikey"},
                {"image_limit_per_session": 10, "image_reserve": 10}, {"model": "two words"}, {"max_recoveries": -1}):
        with pytest.raises(ValueError):
            RunnerConfig(**bad)
    cfg = RunnerConfig(extra_env={"A": "1"})
    summary = cfg.summary()
    assert "system_prompt" not in summary and summary["system_prompt_chars"] == len(SYSTEM_PROMPT)
    assert summary["extra_env"] == ["A"] and json.dumps(summary)
    assert (cfg.model, cfg.effort, cfg.auth) == ("claude-opus-5-5", "high", "subscription")
    assert (cfg.image_limit_per_session, cfg.image_reserve, cfg.max_segments, cfg.stall_turns) == (500, 20, 20, 3)
    assert (cfg.turn_timeout_s, cfg.max_recoveries, cfg.api_retry_backoff_s) == (3600.0, 5, (10.0, 30.0, 60.0, 120.0))
    assert cfg.compact_window_tokens == 200_000 and cfg.compact_pct == 90 and cfg.mcp_tool_timeout_s == 900.0


def test_child_environment(tmp_path):
    base = {"PATH": "p", "CLAUDECODE": "1", "CLAUDE_PID": "9", "CLAUDE_EFFORT": "max", "CLAUDE_CODE_SESSION_ID": "x",
            "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CONFIG_DIR": "keep", "DISABLE_AUTO_COMPACT": "1",
            "DISABLE_COMPACT": "1", "ANTHROPIC_LOG": "keep", "ANTHROPIC_API_KEY": "sk", "ANTHROPIC_AUTH_TOKEN": "t",
            "ANTHROPIC_BASE_URL": "http://proxy"}
    env = env_for(base, RunnerConfig(compact_window_tokens=150_000, compact_pct=80,
                                     extra_env={"MAX_MCP_OUTPUT_TOKENS": "5"}), tmp_path)
    for gone in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
                 "DISABLE_AUTO_COMPACT", "DISABLE_COMPACT", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "ANTHROPIC_BASE_URL"):
        assert gone not in env, gone
    assert env["PATH"] == "p" and env["CLAUDE_CONFIG_DIR"] == "keep" and env["ANTHROPIC_LOG"] == "keep"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "150000" and env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "80"
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false" and env["ENABLE_TOOL_SEARCH"] == "false"
    assert env["MCP_CONNECTION_NONBLOCKING"] == "0" and env["MAX_MCP_OUTPUT_TOKENS"] == "5"   # extra_env wins
    assert env["MCP_TOOL_TIMEOUT"] == "900000"
    assert env["MAST_RUNNER_SECRET"] == "s3cret" and env["MAST_RUNNER_HOOK_DIR"] == str(tmp_path)
    assert env["MAST_RUNNER_HOOK_URL"].endswith("/runner/r/hook") and env["MAST_RUNNER_HOOK_TIMEOUT"] == "25"
    passthrough = env_for(base, RunnerConfig(auth="env"), tmp_path)        # API billing: owner's approval only
    assert passthrough["ANTHROPIC_API_KEY"] == "sk" and passthrough["ANTHROPIC_BASE_URL"] == "http://proxy"


def test_auth_problem():
    assert auth_problem({"apiKeySource": "ANTHROPIC_API_KEY"}, "subscription")
    assert auth_problem({"apiKeySource": "/login managed key"}, "subscription")
    assert auth_problem({"apiKeySource": "none"}, "subscription") is None
    assert auth_problem({"apiKeySource": ""}, "subscription") is None and auth_problem({}, "subscription") is None
    assert auth_problem({"apiKeySource": "ANTHROPIC_API_KEY"}, "env") is None


def test_config_files_and_command(tmp_path):
    cfg = RunnerConfig(max_budget_usd=0.25, effort="low")
    env = bridge_env(base_url="http://127.0.0.1:7862", session="run-x", fetch_dir=tmp_path / "fetch")
    assert env == {"MAST_URL": "http://127.0.0.1:7862", "MAST_ACTOR": "runner", "MAST_SESSION": "run-x",
                   "MAST_FETCH_DIR": str(tmp_path / "fetch"), "MAST_MCP_LOG": "INFO", "PYTHONUTF8": "1"}
    assert bridge_env(base_url="u", session="s", fetch_dir=tmp_path, log_file=tmp_path / "b.log")[
        "MAST_MCP_LOG_FILE"] == str(tmp_path / "b.log")
    server = mcp_config(cfg, python="py.exe", script="run_server.py", env=env)["mcpServers"]["mast"]
    assert server["type"] == "stdio" and server["command"] == "py.exe" and server["env"] == env
    assert server["args"] == ["-I", "-X", "utf8", "run_server.py"] and server["timeout"] == 900_000
    hooks = hook_settings(cfg, python="py.exe", hook_script="hook.py")["hooks"]
    assert set(hooks) == {"PreCompact", "PostCompact", "Stop"}
    assert hooks["Stop"][0]["hooks"][0] == {"type": "command", "command": "py.exe",
                                            "args": ["-I", "-X", "utf8", "hook.py"], "timeout": 30}
    cmd = build_command(cfg, claude=["claude.exe"], system_prompt_path=tmp_path / "sp.md",
                        settings_path=tmp_path / "s.json", mcp_path=tmp_path / "m.json",
                        allowed_tools=["mcp__mast__mast_act", "mcp__mast__mast_wait"], session_id="abc")
    joined = " ".join(cmd)
    for fragment in ("-p --input-format stream-json --output-format stream-json --verbose --include-hook-events",
                     "--effort low", "--strict-mcp-config", "--permission-mode default",
                     "--allowedTools mcp__mast__mast_act,mcp__mast__mast_wait", "--session-id abc",
                     "--max-budget-usd 0.25", "--permission-prompts none", "--disable-slash-commands", "--no-chrome"):
        assert fragment in joined, fragment
    assert cmd[0] == "claude.exe" and cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""


def test_user_message_blocks():
    msg = user_message("go", [{"label": "<visual/>", "mime": "image/png", "data_b64": "iVBORw=="}, {"data_b64": "AA=="}])
    content = msg["message"]["content"]
    assert msg["type"] == "user" and msg["parent_tool_use_id"] is None
    assert [b["type"] for b in content] == ["text", "text", "image", "image"]
    assert content[2]["source"] == {"type": "base64", "media_type": "image/png", "data": "iVBORw=="}


def test_validate_init():
    good = {"tools": ["mcp__mast__mast_act", "mcp__mast__mast_wait"],
            "mcp_servers": [{"name": "mast", "status": "connected"}], "permissionMode": "default",
            "model": "claude-opus-5-5", "skills": ["x"]}
    expected = ["mcp__mast__mast_wait", "mcp__mast__mast_act"]
    assert validate_init(good, expected) == [] and validate_init(good, expected, expected_model="claude-opus-5-5") == []
    assert "extra" in validate_init({**good, "tools": good["tools"] + ["mcp__claude_ai_Drive__x"]}, expected)[0]
    assert validate_init({**good, "tools": ["mcp__mast__mast_act"]}, expected)
    assert validate_init({**good, "mcp_servers": [{"name": "mast", "status": "pending"}]}, expected)
    assert validate_init({**good, "mcp_servers": [{"name": "stm", "status": "connected"}]}, expected)
    assert validate_init({**good, "mcp_servers": good["mcp_servers"] + [{"name": "claude.ai Drive"}]}, expected)
    assert validate_init({**good, "permissionMode": "bypassPermissions"}, expected)
    assert validate_init({**good, "model": "opus"}, expected)
    assert validate_init({**good, "model": "claude-sonnet-5-5"}, expected, expected_model="claude-opus-5-5")
    assert validate_init({**good, "mcp_server_errors": [{"name": "x"}]}, expected)


def test_classify_and_usage():
    assert classify_result({"subtype": "success", "is_error": False}) == "ok"
    assert classify_result({"subtype": "error_max_budget_usd", "is_error": True}) == "budget_usd"
    assert classify_result({"subtype": "success", "is_error": True, "api_error_status": 529}) == "retryable"
    assert classify_result({"subtype": "success", "is_error": True, "api_error_status": 429,
                            "result": "You've hit your session limit · resets 8:30pm (Asia/Shanghai)"}) == "usage_limit"
    assert classify_result({"subtype": "error_during_execution", "is_error": True,
                            "errors": ["429 {\"type\":\"usage_limit_reached\"}"]}) == "usage_limit"
    assert classify_result({"subtype": "success", "is_error": True, "result": "API Error: Overloaded"}) == "retryable"
    assert classify_result({"subtype": "error_during_execution", "is_error": True,
                            "terminal_reason": "aborted_streaming"}) == "error"
    usage = result_usage({"total_cost_usd": 0.5, "usage": {"input_tokens": 1},
                          "modelUsage": {"a": {"inputTokens": 2, "outputTokens": 3, "cacheReadInputTokens": 4,
                                               "cacheCreationInputTokens": 5}, "b": {"inputTokens": 1, "outputTokens": 1}}})
    assert usage == {"input_tokens": 3, "output_tokens": 4, "cache_creation_input_tokens": 5,
                     "cache_read_input_tokens": 4, "cost_usd": 0.5}
    assert result_usage(None) is None


def test_marker_outcome_and_sanitize():
    assert marker_outcome("All set. [DONE]") == ("done", None)
    assert marker_outcome("**[DONE]**。") == ("done", None)
    assert marker_outcome("cannot reach the sample [ABORT]") == ("abort", None)
    assert marker_outcome("[DONE] is what I will write later") == (None, "marker_in_body")
    assert marker_outcome("") == (None, None)
    clean = sanitize({"token": "abc", "data": "x" * 100, "data_b64": "y" * 100, "short": {"data": "zz"}})
    assert clean["token"] == "<redacted>" and clean["data"]["chars"] == 100 and clean["data_b64"]["chars"] == 100
    assert clean["short"] == {"data": "zz"}


def test_prompts():
    for phrase in ("next_token", "`after`", "mast_act", "mast_wait", "WORKING.md", "GUIDE.md"):
        assert phrase in SYSTEM_PROMPT, phrase
    first = initial_message("Find a clean terrace.", "OBS")
    assert first.startswith("Task:\nFind a clean terrace.\n") and "[DONE]" in first and first.endswith("OBS")
    later = recovery_message("T", "OBS", kind="checkpoint", unmatched=[{"name": "mast_act", "id": "1"},
                                                                         {"name": "mast_wait", "id": "2"}])
    assert "replaced after you saved WORKING.md" in later
    assert "2 tool call(s) of the earlier context got no result (mast_act, mast_wait)" in later
    assert "no result" not in recovery_message("T", "OBS", kind="recovery")


# ── the hook ──────────────────────────────────────────────────────────────────
def test_hook_decision():
    block = {"decision": "block", "reason": compact_hook.CHECKPOINT_INSTRUCTION}
    handoff = {"decision": "block", "reason": compact_hook.HANDOFF_REASON}
    assert compact_hook.decide("PreCompact", "none") == (block, "request_checkpoint")
    assert compact_hook.decide("PreCompact", "requested") == (block, None)
    assert compact_hook.decide("PreCompact", "ready") == (handoff, None)
    assert compact_hook.decide("PreCompact", "none", terminal=True) == (handoff, None)
    assert compact_hook.decide("Stop", "requested") == (block, None)
    assert compact_hook.decide("Stop", "requested", terminal=True) == ({}, None)
    assert compact_hook.decide("Stop", "none") == ({}, None) and compact_hook.decide("PostCompact", "none") == ({}, None)
    assert compact_hook.decide("PreCompact", "bogus") == (block, "request_checkpoint")


class _HookServer:
    """A minimal MAST hook route: checks the secret, records the body, answers a fixed output."""

    def __init__(self, output: dict, status: int = 200) -> None:
        received: list = []
        self.received = received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append({"path": self.path, "secret": self.headers.get(compact_hook.HOOK_HEADER),
                                 "type": self.headers.get("Content-Type"), "body": body})
                data = json.dumps({"output": output}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/ext/v1/runner/r1/hook"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _event(name: str, **extra) -> bytes:
    return json.dumps({"session_id": "s", "hook_event_name": name, "transcript_path": "t.jsonl", **extra}).encode()


def test_hook_over_http(tmp_path):
    server = _HookServer({"decision": "block", "reason": "because"})
    try:
        env = {compact_hook.ENV_URL: server.url, compact_hook.ENV_HOOK_KEY: "k", compact_hook.ENV_DIR: str(tmp_path)}
        out = compact_hook.run(_event("Stop", last_assistant_message="x" * 3000), env)
    finally:
        server.close()
    assert out == {"decision": "block", "reason": "because"}
    (got,) = server.received
    assert got["path"] == "/api/ext/v1/runner/r1/hook" and got["secret"] == "k" and got["type"] == "application/json"
    assert got["body"]["event"] == "Stop" and got["body"]["payload"]["last_assistant_message_chars"] == 3000
    assert len(got["body"]["payload"]["last_assistant_message"]) == 2000 and "transcript_path" not in got["body"]["payload"]
    (log,) = [json.loads(line) for line in (tmp_path / compact_hook.EVENTS_FILE).read_text(encoding="utf-8").splitlines()]
    assert log["path"] == "http" and log["output"] == out


def test_hook_falls_back_to_markers(tmp_path):
    refusing = _HookServer({}, status=403)
    try:
        env = {compact_hook.ENV_URL: refusing.url, compact_hook.ENV_HOOK_KEY: "k", compact_hook.ENV_DIR: str(tmp_path)}
        out = compact_hook.run(_event("PreCompact", trigger="auto"), env)       # refused → markers, state none
    finally:
        refusing.close()
    assert out["decision"] == "block" and (tmp_path / compact_hook.REQUEST_MARKER).is_file()
    env = {compact_hook.ENV_URL: "http://127.0.0.1:9/x", compact_hook.ENV_HOOK_KEY: "k", compact_hook.ENV_DIR: str(tmp_path),
           compact_hook.ENV_TIMEOUT: "3"}
    assert compact_hook.run(_event("Stop"), env)["decision"] == "block"        # requested → Stop blocked
    (tmp_path / compact_hook.READY_MARKER).touch()
    assert compact_hook.run(_event("PreCompact"), env)["reason"] == compact_hook.HANDOFF_REASON
    assert compact_hook.run(b"not json", env) == {}                            # malformed: never blocked
    paths_taken = [json.loads(line)["path"] for line in (tmp_path / compact_hook.EVENTS_FILE).read_text(
        encoding="utf-8").splitlines()]
    assert paths_taken == ["markers", "markers", "markers", "malformed"]
    assert compact_hook.run(_event("Stop"), {}) == {}                          # nothing configured at all


def test_hook_script_runs_isolated_with_the_standard_library_only(tmp_path):
    proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(paths.HOOK_SCRIPT)], input=_event("Stop"),
                          capture_output=True, timeout=60, env={"SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
                                                                compact_hook.ENV_DIR: str(tmp_path)})
    assert proc.returncode == 0 and json.loads(proc.stdout) == {}, proc.stderr
    allowed = {"__future__", "json", "os", "sys", "time", "urllib.request", "pathlib", "typing"}
    imports = [line.split()[1] for line in paths.HOOK_SCRIPT.read_text(encoding="utf-8").splitlines()
               if line.startswith(("import ", "from "))]
    assert set(imports) <= allowed, imports


# ── paths ─────────────────────────────────────────────────────────────────────
def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_paths_development_defaults():
    repo = Path(__file__).resolve().parents[4]
    assert paths.install_root() == repo
    assert paths.resolve_bridge_script({}) == repo / "integrations" / "claude-code" / "server" / "run_server.py"
    assert paths.resolve_bridge_python({}) == Path(sys.executable)
    assert paths.resolve_hook_script() == paths.HOOK_SCRIPT and paths.HOOK_SCRIPT.parent.name == "runner"


def test_paths_overrides(tmp_path):
    script = _touch(tmp_path / "b" / "run_server.py")
    python = _touch(tmp_path / "py" / "python.exe")
    claude = _touch(tmp_path / "cc" / "claude.exe")
    env = {"MAST_BRIDGE_SCRIPT": f'"{script}"', "MAST_BRIDGE_PYTHON": str(python), "MAST_CLAUDE_EXE": str(claude)}
    runtime = paths.resolve_runtime(env)
    assert runtime.bridge_script == str(script.resolve()) and runtime.bridge_python == str(python)
    assert runtime.claude == (str(claude),)
    with pytest.raises(paths.RuntimeSetupError, match="MAST_BRIDGE_SCRIPT"):
        paths.resolve_bridge_script({"MAST_BRIDGE_SCRIPT": str(tmp_path / "missing.py")})
    with pytest.raises(paths.RuntimeSetupError, match="MAST_BRIDGE_PYTHON"):
        paths.resolve_bridge_python({"MAST_BRIDGE_PYTHON": str(tmp_path / "nope.exe"), "PATH": ""})
    with pytest.raises(paths.RuntimeSetupError, match="MAST_CLAUDE_EXE"):
        paths.resolve_claude({"MAST_CLAUDE_EXE": str(tmp_path / "nope.exe"), "PATH": ""})


def test_paths_claude_on_path(tmp_path, monkeypatch):
    seen: list[str] = []

    def which(name, path=None):
        seen.append(name)
        return str(tmp_path / name) if name == "claude.cmd" else None

    monkeypatch.setattr(paths.shutil, "which", which)
    assert paths.resolve_claude({"PATH": "x"}, windows=True) == [str(tmp_path / "claude.cmd")]
    assert seen == ["claude.exe", "claude.cmd"]                     # the native build first
    seen.clear()
    with pytest.raises(paths.RuntimeSetupError, match="'claude'"):
        paths.resolve_claude({"PATH": "x"}, windows=False)
    assert seen == ["claude"]


def test_paths_frozen_install(tmp_path, monkeypatch):
    install = tmp_path / "MAST"
    exe = _touch(install / "MAST2.exe")
    script = _touch(install / "integrations" / "claude-code" / "server" / "run_server.py")
    python = _touch(install / "MASTv2" / "pyruntime" / ("python.exe" if sys.platform == "win32" else "python3"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("MAST_PYRUNTIME_DIR", raising=False)
    assert paths.install_root() == install.resolve()
    assert paths.resolve_bridge_script({}) == script.resolve()
    assert paths.resolve_bridge_python({}) == python.resolve() or paths.resolve_bridge_python({}) == python
    python.unlink()
    with pytest.raises(paths.RuntimeSetupError, match="bundled python not found"):
        paths.resolve_bridge_python({})
    script.unlink()
    with pytest.raises(paths.RuntimeSetupError, match="run_server.py"):
        paths.resolve_bridge_script({})


# ── the frozen build ──────────────────────────────────────────────────────────
def test_no_credential_shaped_constants_for_the_frozen_build(tmp_path):
    """``mast2.spec`` aborts the build on an uppercase constant named like a credential (TOKEN,
    SECRET, API_KEY ...) holding a 12+ character string (``MASTv2/scripts/bundle_sources.py``)."""
    repo = Path(__file__).resolve().parents[4]
    name = "_runner_test_bundle_sources"
    spec = importlib.util.spec_from_file_location(name, repo / "MASTv2" / "scripts" / "bundle_sources.py")
    scanner = importlib.util.module_from_spec(spec)
    sys.modules[name] = scanner
    try:
        spec.loader.exec_module(scanner)
    finally:
        del sys.modules[name]
    files = [*scanner.iter_source_files(repo / "MASTv2" / "mast" / "vista" / "runner"),
             repo / "MASTv2" / "mast" / "api" / "ext" / "runner.py"]
    hits = {str(f): scanner.scan_credentials(f) for f in files if scanner.scan_credentials(f)}
    assert len(files) >= 7 and hits == {}
    probe = tmp_path / "probe.py"                          # the net catches the old spelling
    probe.write_text('SECRET_HEADER = "X-MAST-Runner-Secret"', encoding="utf-8")
    assert scanner.scan_credentials(probe)
