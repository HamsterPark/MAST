"""Client smoke test: a real external agent drives MAST 7.0 on the simulator over MCP.

What it does
------------
1. Starts the STM-Bench simulator and the real MAST service in a child process
   (``vista_sim.SimServiceProcess``), in a fresh data root under ``--out``.
2. Runs one external agent against it through MAST's own MCP bridge
   (``integrations/claude-code/server/run_server.py``):

   * ``codex``: ``codex exec --json --ignore-user-config`` with the bridge configured by ``-c``
     overrides (shell, web search and other built-ins off; ``tool_timeout_sec = 900``; MAST tools
     auto-approved). Login stays the user's ChatGPT login; ``OPENAI_API_KEY``,
     ``CODEX_API_KEY`` and ``CODEX_ACCESS_TOKEN`` are removed from the child environment.
   * ``claude``: ``claude -p`` with ``--tools ""``, ``--strict-mcp-config`` and the bridge as
     the only MCP server, ``--setting-sources ""``; ``ANTHROPIC_API_KEY`` /
     ``ANTHROPIC_AUTH_TOKEN`` and the parent session's ``CLAUDE*`` variables are removed, and
     the run is refused unless the init event reports ``apiKeySource`` none (subscription only).

3. Gives the agent a scripted checklist (briefing, scope, observe, one scan, magnifier and a
   ruler, WORKING.md, a wait, two acts in ONE code cell to exercise the one-act-per-cell rule,
   handover) and then checks MAST's own records, not the agent's account of them: accepted acts,
   archived frames, refusal codes, client name and model recorded with the acts.

The result is ``<out>/summary.json`` plus the raw evidence (client event stream, MAST service
log, the workspace's ``events.jsonl``). Exit code 0 when every required check passed.

Nothing here is part of MAST at runtime; it is a development and release tool. It costs model
usage on the user's subscription (one short session).

Usage::

    set PYTHONUTF8=1
    set PYTHONPATH=<repo>\\MASTv2;<STM-Bench checkout>
    python MASTv2\\scripts\\vista_client_smoke.py codex --out <dir> [--codex <codex.exe>] [--model gpt-6.1-sol]
    python MASTv2\\scripts\\vista_client_smoke.py claude --out <dir> [--model claude-opus-5-5]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve()
REPO_ROOT = SCRIPT.parents[2]
BRIDGE = REPO_ROOT / "integrations" / "claude-code" / "server" / "run_server.py"
sys.path.insert(0, str(SCRIPT.parent))

from vista_sim import SimServiceProcess, http_json  # noqa: E402

CODEX_AUTH_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
CLAUDE_AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

TASK = """\
You are testing a scanning tunnelling microscope controlled through the MAST tools (an
STM simulator stands in for the instrument). Work through this checklist in order, using only
the mast_* tools. Keep it short; this is a functional test, not a research task.

1. mast_briefing with sections status and scope.
2. mast_scope: create and select experiment "smoke-{client}" and sample "Au(111) smoke sample"
   if no sample is active.
3. mast_observe. Note the next_token.
4. mast_act: action "scan", params {{"center": {{"x_nm": 0, "y_nm": 0}}, "size_nm": 20,
   "pixels": 64, "line_time_s": 0.2}}, after = that token. If the answer says the action is still running,
   call mast_wait with condition {{"type": "job", "job_id": <its job_id>}} until it ends.
   Look at the image you get.
5. mast_inspect the new frame once (the whole frame), and mast_stats on it.
6. mast_write_working with three lines: what you scanned, what you saw, the next step.
7. mast_wait with condition {{"type": "timer", "seconds": 3}}.
8. One-act-per-step check: {cell_instruction}
9. mast_handover with a two-sentence summary.

Finish with one line per step: the step number, the tool(s) you called, and any error code a
tool returned (for example observe_first, same_code_cell, act_in_flight). End with [DONE].
"""

CELL_CODEX = (
    "in ONE single code cell (one exec call), call mast_act twice in a row: first "
    "set_bias with bias_v 0.5 using your newest token, then immediately set_bias with "
    "bias_v 0.6 using the next_token from the first result, without looking at anything in "
    "between. Report what the second call returned. Then call mast_observe and set_bias back "
    "to the original bias with the fresh token.")
CELL_CLAUDE = (
    "call mast_act set_bias with bias_v 0.5 using your newest token, then call mast_act "
    "set_bias with bias_v 0.6 using the SAME token again (not the new one). Report what the "
    "second call returned. Then act once more with the newest token to set the bias back to "
    "its original value.")


def _env_without(names: tuple[str, ...], *, drop_claude: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    for k in names:
        env.pop(k, None)
    if drop_claude:
        for k in list(env):
            if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_") or k in ("CLAUDE_PID", "CLAUDE_EFFORT"):
                env.pop(k, None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _toml(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def run_codex(args, sim: SimServiceProcess, out: Path) -> dict:
    codex = args.codex or os.environ.get("CODEX_CLI_PATH") or shutil.which("codex") or "codex"
    work = out / "codex_cwd"
    work.mkdir(exist_ok=True)
    mcp_env = {"MAST_URL": sim.base_url, "MAST_ACTOR": "codex-smoke",
               "MAST_MCP_LOG": str(out / "bridge.log"), "PYTHONUTF8": "1",
               "PYTHONIOENCODING": "utf-8"}
    settings: dict[str, Any] = {
        "model": args.model or "gpt-6.1-sol", "model_reasoning_effort": args.effort or "medium",
        "model_provider": "openai", "forced_login_method": "chatgpt", "web_search": "disabled",
        "features.shell_tool": False, "features.browser_use": False,
        "features.browser_use_external": False, "features.in_app_browser": False,
        "features.computer_use": False, "features.plugins": False, "features.apps": False,
        "features.multi_agent": False, "features.image_generation": False,
        "mcp_servers.mast.command": sys.executable,
        "mcp_servers.mast.args": ["-I", "-X", "utf8", str(BRIDGE)],
        "mcp_servers.mast.startup_timeout_sec": 30, "mcp_servers.mast.tool_timeout_sec": 900,
        "mcp_servers.mast.default_tools_approval_mode": "approve",
    }
    if args.code_mode:
        settings["features.code_mode_host"] = True   # code-mode-only models need it; default is on
    for k, v in mcp_env.items():
        settings[f"mcp_servers.mast.env.{k}"] = v
    cmd = [codex, "exec", "--json", "--ignore-user-config", "--skip-git-repo-check",
           "--color", "never", "-C", str(work)]
    for k, v in settings.items():
        cmd += ["-c", f"{k}={_toml(v)}"]
    cmd += ["-"]
    prompt = TASK.format(client="codex", cell_instruction=CELL_CODEX)
    (out / "client_command.json").write_text(json.dumps({"argv": cmd, "prompt": prompt},
                                                        ensure_ascii=False, indent=1), encoding="utf-8")
    return _run(cmd, prompt, out, _env_without(CODEX_AUTH_VARS), args.timeout)


def run_claude(args, sim: SimServiceProcess, out: Path) -> dict:
    claude = args.claude or shutil.which("claude") or "claude"
    work = out / "claude_cwd"
    work.mkdir(exist_ok=True)
    mcp = {"mcpServers": {"mast": {"command": sys.executable,
                                   "args": ["-I", "-X", "utf8", str(BRIDGE)],
                                   "env": {"MAST_URL": sim.base_url, "MAST_ACTOR": "claude-smoke",
                                           "MAST_MCP_LOG": str(out / "bridge.log"),
                                           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}}}}
    mcp_path = out / "claude_mcp.json"
    mcp_path.write_text(json.dumps(mcp, ensure_ascii=False, indent=1), encoding="utf-8")
    cmd = [claude, "-p", "--output-format", "stream-json", "--verbose",
           "--model", args.model or "claude-opus-5-5", "--setting-sources", "",
           "--mcp-config", str(mcp_path), "--strict-mcp-config", "--tools", "",
           "--allowedTools", "mcp__mast", "--permission-mode", "default",
           "--disable-slash-commands"]
    prompt = TASK.format(client="claude", cell_instruction=CELL_CLAUDE)
    (out / "client_command.json").write_text(json.dumps({"argv": cmd, "prompt": prompt},
                                                        ensure_ascii=False, indent=1), encoding="utf-8")
    return _run(cmd, prompt, out, _env_without(CLAUDE_AUTH_VARS, drop_claude=True), args.timeout,
                cwd=work)


def _run(cmd: list[str], prompt: str, out: Path, env: dict, timeout: float,
         cwd: Path | None = None) -> dict:
    t0 = time.monotonic()
    with (out / "client_events.jsonl").open("w", encoding="utf-8") as fo, \
            (out / "client_stderr.txt").open("w", encoding="utf-8") as fe:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=fo, stderr=fe, env=env, text=True,
                             encoding="utf-8", cwd=str(cwd) if cwd else None)
        try:
            p.communicate(prompt, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            timed_out = True
    rows = []
    for line in (out / "client_events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return {"exit_code": p.returncode, "timed_out": timed_out,
            "elapsed_s": round(time.monotonic() - t0, 1), "events": rows}


def _final_text(rows: list[dict]) -> str:
    texts = []
    for r in rows:
        item = r.get("item") if isinstance(r.get("item"), dict) else None
        if item and item.get("type") == "agent_message":                  # codex
            texts.append(str(item.get("text") or ""))
        if r.get("type") == "result" and isinstance(r.get("result"), str):  # claude
            texts.append(r["result"])
    return texts[-1] if texts else ""


def _api_key_source(rows: list[dict]) -> str | None:
    for r in rows:
        if r.get("type") == "system" and r.get("subtype") == "init":
            return str(r.get("apiKeySource"))
    return None


def check(client: str, sim: SimServiceProcess, run: dict, out: Path, *, code_mode: bool = False) -> dict:
    status_code, status = http_json("GET", sim.ext_url + "/vista/status")
    ws_dir = sim.root / "mast_root"
    events = []
    for p in sorted(Path(sim.info.get("mast_root") or ws_dir).glob("experiments/vista/*/events.jsonl")):
        events += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        shutil.copy2(p, out / f"events_{p.parent.name}.jsonl")
    acts = [e for e in events if e.get("kind") == "act"]
    refusals = [e.get("code") for e in events if e.get("kind") == "refusal"]
    frames = sorted({f for e in acts for f in (e.get("new_frames") or [])})
    clients = {str((e.get("client") or {}).get("client")) for e in acts if e.get("client")}
    models = {str(((e.get("client") or {}).get("turn") or {}).get("model"))
              for e in acts if isinstance((e.get("client") or {}).get("turn"), dict)}
    final = _final_text(run["events"])
    # Codex Code Mode chains calls in one code cell (same _meta.itemId): the second act must be
    # refused. Direct tool calls carry distinct ids, and the second act quotes the fresh token, so
    # nothing is refused there. Claude is told to reuse a spent token: observe_first.
    # Code Mode shows in the records: the code cell's item id starts with "ctc_"
    code_mode = code_mode or any(str(((e.get("client") or {}).get("item_id") or "")).startswith("ctc_")
                                 for e in events if e.get("kind") in ("act", "refusal"))
    expected_refusal = ("same_code_cell" if client == "codex" and code_mode
                        else None if client == "codex" else "observe_first")
    checks = {
        "client_exited_ok": run["exit_code"] == 0 and not run["timed_out"],
        "acts_accepted_ge_3": len(acts) >= 3,
        "scan_frame_archived": any(f.endswith(".s0") for f in frames),
        "client_name_recorded": any(c and c != "None" for c in clients),
        "done_marker": "[DONE]" in final,
    }
    if expected_refusal:
        checks[f"refusal_{expected_refusal}_seen"] = expected_refusal in refusals
    if client == "claude":
        checks["subscription_only"] = _api_key_source(run["events"]) in ("none", "")
    images = sum(1 for r in run["events"]
                 for c in (((r.get("item") or {}).get("result") or {}).get("content") or [])
                 if isinstance(c, dict) and c.get("type") == "image")
    summary = {"client": client, "base_url": sim.base_url, "checks": checks, "code_mode": code_mode,
               "images_delivered": images,
               "passed": all(checks.values()), "acts": len(acts), "frames": frames,
               "refusals": refusals, "clients": sorted(clients), "models": sorted(models),
               "elapsed_s": run["elapsed_s"], "final_text": final[-3000:],
               "vista_status_http": status_code, "vista_status": status}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                                      encoding="utf-8")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vista_client_smoke", description=__doc__.split("\n\n")[0])
    ap.add_argument("client", choices=("codex", "claude"))
    ap.add_argument("--out", required=True, help="evidence folder (created)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None, help="codex reasoning effort")
    ap.add_argument("--codex", default=None, help="codex executable (default: CODEX_CLI_PATH or PATH)")
    ap.add_argument("--claude", default=None, help="claude executable (default: PATH)")
    ap.add_argument("--timeout", type=float, default=1800.0, help="client wall-clock limit, s")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--strict", action="store_true", help="MAST_VISTA_STRICT=1 in the service")
    ap.add_argument("--code-mode", action="store_true",
                    help="codex: features.code_mode_host=true (the model calls tools from JavaScript)")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if a.strict:
        env["MAST_VISTA_STRICT"] = "1"
    sim = SimServiceProcess(out / "sim", seed=a.seed, material="Au(111)", env=env)
    sim.start()
    try:
        run = run_codex(a, sim, out) if a.client == "codex" else run_claude(a, sim, out)
        summary = check(a.client, sim, run, out, code_mode=a.code_mode)
    finally:
        sim.stop()
        try:
            shutil.copy2(sim.log_file, out / "service.log")
        except OSError:
            pass
    print(json.dumps({k: summary[k] for k in ("client", "passed", "checks", "acts", "frames",
                                              "refusals", "clients", "models", "elapsed_s")},
                     ensure_ascii=False, indent=1))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
