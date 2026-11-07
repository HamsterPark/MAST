"""The Claude runner against a scripted ``claude -p`` (``fake_claude_cli.py``): segments, turns,
checkpoints, recovery, operator stop, unmatched tool calls, guards (ported from VISTA-SPM
``tests/test_runner.py``; the hooks travel over real HTTP to the runner routes).

Timing: no assertion here depends on which of two concurrent events wins. Where VISTA relied on the
runner reacting within one 50 ms fake step, the fake now waits for the observable effect
(``wait_checkpoint``, or the test waits for a recorded tool call before it stops the run).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from vista_runner_kit import PNG_B64, TASK

from mast.vista.runner.compact_hook import CHECKPOINT_INSTRUCTION
from mast.vista.runner.prompts import CONTINUE_TEXT, PREFLIGHT_PROMPT

ACT = {"tool": "mast_act", "args": {"action": "scan"}}
WRITE = {"tool": "mast_write_working", "args": {"content": "after 2 acts: continue scanning"}}


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def inputs(view: dict, stem: str) -> list[dict]:
    return json.loads((Path(view["run_dir"]) / f"{stem}.input.json").read_text(encoding="utf-8"))


def first_text(view: dict, stem: str) -> str:
    return inputs(view, stem)[0]["message"]["content"][0]["text"]


def test_run_done_with_continuation(rig):
    view, runner = rig.run([[
        [ACT, {"text": "I see 1 square."}],
        [ACT, {"text": "Two squares. [DONE]"}],
    ]])
    assert view["outcome"] == "done" and view["error"] is None and view["state"] == "finished"
    assert view["final_text"] == "Two squares. [DONE]" and view["started_by"] == "ext:test"
    (seg,) = view["segments"]
    assert seg["end_reason"] == "marker" and seg["kind"] == "initial" and seg["turns"] == 2
    assert seg["messages_sent"] == 2 and seg["images_input"] == 1 and seg["images_results"] == 2
    assert seg["acts"] == 2 and seg["tool_counts"] == {"mast_act": 2} and seg["unmatched_tool_calls"] == []
    assert seg["models"] == ["claude-fake-1"] and seg["exit_code"] == 0 and seg["usage_complete"]
    assert view["usage"]["cost_usd"] == pytest.approx(0.02) and view["usage"]["num_turns"] == 2
    session = f"run-{view['run_id']}"
    assert view["session"] == session
    # the service: a fresh context, one recovery observation, the session closed at the end
    assert rig.vista.clears == [session] and rig.vista.recoveries == [session]
    assert rig.vista.closed[session] == "runner run ended: done"
    assert {(c["session"], c["actor"]) for c in rig.vista.calls} == {(session, "runner")}
    # the first message: task block, then the observation and its image
    first = inputs(view, "segment_000")[0]["message"]["content"]
    assert first[0]["text"].startswith("Task:\n" + TASK) and "TK01-QQQQ" in first[0]["text"]
    assert [b["type"] for b in first] == ["text", "text", "image"]
    assert inputs(view, "segment_000")[1]["message"]["content"][0]["text"] == CONTINUE_TEXT
    run_dir = Path(view["run_dir"])
    assert PNG_B64 not in (run_dir / "segment_000.input.json").read_text(encoding="utf-8")
    assert all(PNG_B64 not in json.dumps(m) for m in lines(run_dir / "segment_000.stdout.jsonl"))
    command = json.loads((run_dir / "segment_000.command.json").read_text(encoding="utf-8"))["argv"]
    assert command[command.index("--allowedTools") + 1] == ",".join(
        f"mcp__mast__{t}" for t in ("mast_observe", "mast_act", "mast_run", "mast_wait", "mast_jobs",
                                    "mast_read_working", "mast_write_working"))
    mcp = json.loads((run_dir / "segment_000.mcp.json").read_text(encoding="utf-8"))["mcpServers"]["mast"]
    assert mcp["env"]["MAST_SESSION"] == session and mcp["env"]["MAST_ACTOR"] == "runner"
    assert mcp["env"]["MAST_URL"] == rig.url and mcp["args"][:3] == ["-I", "-X", "utf8"]
    # Stop hooks went over HTTP to the runner (no decision: no checkpoint was requested)
    hooks = lines(run_dir / "segment_000.hooks.jsonl")
    assert [h["event"]["hook_event_name"] for h in hooks] == ["Stop", "Stop"] and all(h["output"] == {} for h in hooks)
    assert all(e["path"] == "http" for e in lines(run_dir / "hooks" / "hook_events.jsonl"))
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["outcome"] == "done" and status["segments"][0]["results"] and status["config"]["effort"] == "low"
    # the per-run secret never reaches the records
    secret = rig.manager._runs[view["run_id"]].secret
    assert not any(secret in p.read_text(encoding="utf-8", errors="replace")
                   for p in run_dir.rglob("*") if p.is_file())


def test_environment_drops_api_credentials_end_to_end(rig):
    environ = {"PATH": __import__("os").environ["PATH"], "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
               "ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_BASE_URL": "http://elsewhere",
               "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "DISABLE_AUTO_COMPACT": "1"}
    view, _ = rig.run([[[{"text": "[DONE]"}]]], environ=environ)
    assert view["outcome"] == "done"
    seen = rig.proc(0)
    assert not any(seen["env_has"][k] for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                                                "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "DISABLE_AUTO_COMPACT"))
    assert seen["env_has"]["MAST_RUNNER_SECRET"] and seen["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert seen["env"]["MAST_RUNNER_HOOK_URL"] == f"{rig.url}/api/ext/v1/runner/{view['run_id']}/hook"
    assert Path(seen["cwd"]).name.startswith("mast_cc_") and not Path(seen["cwd"]).exists()   # removed after the run


def test_api_key_source_ends_the_run(rig):
    view, _ = rig.run([[[{"api_key_source": "ANTHROPIC_API_KEY"}, {"wait_interrupt": 15}]]])
    assert view["outcome"] == "error" and "init_invalid" in view["error"] and "apiKeySource" in view["error"]
    assert view["segments"][0]["end_reason"] == "init_invalid" and rig.vista.acts == 0


def test_invalid_init_is_error(rig):
    view, _ = rig.run([[[{"extra_tools": ["mcp__claude_ai_Google_Drive__search"]}, {"wait_interrupt": 15}]]])
    assert view["outcome"] == "error" and "unexpected tool surface" in view["error"]
    assert not rig.vista.calls


def test_marker_in_body_and_stall(rig):
    view, _ = rig.run([[[{"text": "I will write [DONE] at the end, not yet."}], [{"text": "thinking"}],
                        [{"text": "x"}]]], stall_turns=2)
    assert view["outcome"] == "stalled"
    (seg,) = view["segments"]
    assert seg["end_reason"] == "stalled" and seg["messages_sent"] == 2
    assert any("marker_in_body" in n for n in seg["notes"])


def test_image_limit_checkpoint_handoff(rig):
    # 1 image in the first message + 1 per act: the second act reaches the limit of 3
    view, _ = rig.run([
        [[ACT, ACT, {"wait_checkpoint": "requested"}, WRITE, {"wait_interrupt": 15}]],
        [[ACT, {"text": "[DONE]"}]],
    ], config={"image_limit_per_session": 3, "image_reserve": 0})
    assert view["outcome"] == "done", view
    seg0, seg1 = view["segments"]
    assert seg0["end_reason"] == "checkpoint" and seg1["kind"] == "checkpoint"
    assert [r["reason"] for r in seg0["checkpoint_requests"]] == ["image_limit"]
    session = view["session"]
    assert rig.vista.requests == [(session, "image_limit")] and rig.vista.working == WRITE["args"]["content"]
    assert seg0["usage_complete"] and seg0["exit_code"] == 0 and not seg0["killed"]   # interrupted, not killed
    assert seg0["acts"] == 2 and rig.vista.acts == 3
    assert rig.vista.clears == [session, session] and len(rig.vista.recoveries) == 2
    assert first_text(view, "segment_001").startswith(
        "This is a fresh context: the previous context was replaced after you saved WORKING.md")
    assert seg1["session_id"] != seg0["session_id"]


def test_checkpoint_instruction_follows_a_turn_while_requested(rig):
    # the checkpoint is requested mid-turn; the turn ends without WORKING.md (the Stop hook block
    # is answered by the on_block steps), so the continue text carries the instruction
    view, _ = rig.run([[
        {"steps": [ACT, ACT, {"wait_checkpoint": "requested"}, {"text": "done acting for now"}],
         "on_block": [{"text": "noted"}]},
        [WRITE, {"wait_interrupt": 15}],
    ], [[{"text": "[DONE]"}]]], config={"image_limit_per_session": 3, "image_reserve": 0})
    assert view["outcome"] == "done", view
    texts = [m["message"]["content"][0]["text"] for m in inputs(view, "segment_000")]
    assert texts[1] == f"{CONTINUE_TEXT}\n\n{CHECKPOINT_INSTRUCTION}"
    assert view["segments"][0]["stop_hook_blocks"] >= 1


def test_precompact_checkpoint_via_hook(rig):
    view, _ = rig.run([
        [{"steps": [ACT, {"precompact": True}, {"text": "continuing"}],
          "on_block": [{"tool": "mast_write_working", "args": {"content": "state for the next context"}},
                       {"wait_interrupt": 15}]}],
        [[ACT, {"text": "[DONE]"}]],
    ])
    assert view["outcome"] == "done", view
    seg0 = view["segments"][0]
    assert seg0["end_reason"] == "checkpoint" and seg0["compaction_attempts"] == 1
    assert seg0["compact_boundaries"] == [] and seg0["stop_hook_blocks"] >= 1
    assert rig.vista.requests == [(view["session"], "context_limit")]
    hooks = lines(Path(view["run_dir"]) / "segment_000.hooks.jsonl")
    pre = [h for h in hooks if h["event"]["hook_event_name"] == "PreCompact"]
    assert pre[0]["effect"] == "request_checkpoint" and pre[0]["output"]["decision"] == "block"
    assert any(h["event"]["hook_event_name"] == "Stop" and h["output"].get("decision") == "block" for h in hooks)


def test_native_compaction_is_error(rig):
    view, _ = rig.run([[[{"compact_boundary": True}, {"wait_interrupt": 15}]]])
    assert view["outcome"] == "error" and "native_compact" in view["error"]
    assert view["segments"][0]["end_reason"] == "native_compact"


def test_crash_recovery_reports_unmatched_tool_calls(rig):
    view, _ = rig.run([[[ACT, {"tool": "mast_act", "args": {"action": "scan"}, "hold": "exit"}]],
                       [[ACT, {"text": "[DONE]"}]]])
    assert view["outcome"] == "done"
    seg0, seg1 = view["segments"]
    assert seg0["end_reason"] == "process_exit" and seg0["exit_code"] == 3 and not seg0["usage_complete"]
    assert seg1["kind"] == "recovery" and rig.vista.acts == 3       # the held act ran on the server
    (unmatched,) = view["unmatched_tool_calls"]
    assert unmatched["name"] == "mast_act" and unmatched["segment"] == 0 and unmatched["at"] == "segment_end"
    assert seg0["unmatched_tool_calls"][0]["id"] == unmatched["id"]
    text = first_text(view, "segment_001")
    assert text.startswith("This is a fresh context: the previous context ended unexpectedly")
    assert "1 tool call(s) of the earlier context got no result (mast_act)" in text and "mast_jobs" in text


def test_operator_stop_interrupts_and_closes_the_session(rig):
    run_id, runner = rig.start([[[{"tool": "mast_observe"},
                                  {"tool": "mast_wait", "args": {"timeout_s": 600}, "hold": "interrupt"}]]])
    deadline = time.monotonic() + 60
    while not rig.vista.names("mast_wait"):
        assert time.monotonic() < deadline, "the fake never called mast_wait"
        time.sleep(0.05)
    stopped = rig.manager.stop(run_id, "operator says stop")
    assert stopped["stop"]["reason"] == "operator says stop"
    assert rig.vista.closed[f"run-{run_id}"] == "stopped by the operator: operator says stop"   # at once
    assert rig.manager.wait(run_id, 60)
    view = rig.manager.get(run_id)
    assert view["outcome"] == "stopped_by_operator" and view["error"] is None
    (seg,) = view["segments"]
    assert seg["end_reason"] == "operator_stop" and seg["exit_code"] == 0 and not seg["killed"]
    assert [(u["name"], u["at"]) for u in view["unmatched_tool_calls"]] == [("mast_wait", "turn_end")]
    assert seg["usage"]["cost_usd"] > 0                               # the interrupted turn is paid for, too
    assert rig.manager.stop(run_id, "again")["outcome"] == "stopped_by_operator"   # idempotent


def test_api_error_backoff_then_continue(rig):
    view, _ = rig.run([[[{"result": {"is_error": True, "api_error_status": 529, "result": "API Error: 529 Overloaded"}}],
                        [ACT, {"text": "[DONE]"}]]])
    assert view["outcome"] == "done" and rig.sleeps == [10.0]
    assert view["segments"][0]["messages_sent"] == 2


def test_decision_lease_expiry_recovers(rig):
    # the fake would answer by itself after 300 s; the 1.5 s lease must cut the turn first
    view, _ = rig.run([[[{"wait_interrupt": 300}]], [[ACT, {"text": "[DONE]"}]]], config={"turn_timeout_s": 1.5})
    assert view["outcome"] == "done"
    assert view["segments"][0]["end_reason"] == "lease_expired" and view["segments"][1]["kind"] == "recovery"


def test_max_segments(rig):
    view, _ = rig.run([[[ACT, ACT, {"wait_checkpoint": "requested"}, WRITE, {"wait_interrupt": 15}]]],
                      config={"image_limit_per_session": 3, "image_reserve": 0}, max_segments=1)
    assert view["outcome"] == "max_segments" and len(view["segments"]) == 1


def test_checkpoint_loop_guard(rig):
    session = [{"steps": [{"precompact": True}, {"text": "ok"}],
                "on_block": [{"tool": "mast_write_working", "args": {"content": "nothing new"}},
                             {"wait_interrupt": 15}]}]
    view, _ = rig.run([session] * 4)
    assert view["outcome"] == "error" and "consecutive checkpoint handoffs" in view["error"]
    assert len(view["segments"]) == 3 and all(s["end_reason"] == "checkpoint" for s in view["segments"])


def test_preflight_and_run(rig):
    view, _ = rig.run([[[{"text": "READY"}]], [[ACT, {"text": "[DONE]"}]]], preflight=True)
    assert view["outcome"] == "done"
    pf = view["preflight"]
    assert pf["ok"], pf["problems"]
    assert pf["model"] == "claude-fake-1" and pf["permission_mode"] == "default" and pf["hook_events"] >= 1
    assert inputs(view, "preflight")[0]["message"]["content"][0]["text"] == PREFLIGHT_PROMPT
    mcp = json.loads((Path(view["run_dir"]) / "preflight.mcp.json").read_text(encoding="utf-8"))["mcpServers"]["mast"]
    assert mcp["env"]["MAST_URL"] == "http://preflight.invalid" and mcp["env"]["MAST_SESSION"].endswith("-preflight")
    assert rig.vista.recoveries == [view["session"]] and len(view["segments"]) == 1   # the preflight is not a segment


def test_preflight_failure_ends_the_run_and_nothing_reached_mast(rig):
    view, _ = rig.run([[[ACT, {"text": "READY"}]]], preflight=True)
    assert view["outcome"] == "error" and "preflight failed" in view["error"]
    assert any("called tools" in p for p in view["preflight"]["problems"])
    assert not rig.vista.calls and view["segments"] == []


def test_bridge_without_write_working_refuses_to_run(rig):
    view, _ = rig.run([[[{"text": "[DONE]"}]]], config={"extra_env": {"STUB_BRIDGE_TOOLS": "mast_act,mast_observe"}})
    assert view["outcome"] == "error" and "mast_write_working" in view["error"]
    assert rig.processes() == 0 and view["segments"] == []            # claude never started
    assert rig.vista.closed[view["session"]] == "runner run ended: error"
