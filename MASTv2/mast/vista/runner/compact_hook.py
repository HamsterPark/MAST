#!/usr/bin/env python3
# Derived from VISTA (MIT), github.com/joshhhhhan/VISTA, backends/claude/compact_hook.py
"""Claude Code hook (PreCompact, PostCompact, Stop) of the MAST runner's checkpoint protocol.

README
------
Each segment's ``settings.json`` runs this file (exec form: ``command`` = the bridge's python,
``args`` = ``["-I", "-X", "utf8", "<this file>"]``) on PreCompact, PostCompact and Stop.
Standard library only, because it runs as a script under the bridge's python (MAST's bundled
embeddable runtime in a frozen install); importable as ``mast.vista.runner.compact_hook`` so the
runner applies the same :func:`decide`.

Decision logic (VISTA's), from the run's checkpoint state ``none | requested | ready``:

* PreCompact, state ``none`` or ``requested`` → block compaction with
  :data:`CHECKPOINT_INSTRUCTION`; from ``none`` the runner asks the service for a checkpoint
  (``request_checkpoint``), which refuses actions until WORKING.md is written.
* PreCompact, state ``ready`` → block with :data:`HANDOFF_REASON` (the runner is about to
  replace the session).
* Stop, state ``requested`` → block the stop with :data:`CHECKPOINT_INSTRUCTION` (Claude
  Code feeds the reason back to the model as "Stop hook feedback").
* Everything else (Stop otherwise, PostCompact) → no decision (``{}``). PostCompact means
  a native compaction happened; the runner treats that as an error.
* When the run is ending (operator stop): PreCompact is blocked with the handoff reason and
  Stop is allowed.

Transport: ``POST $MAST_RUNNER_HOOK_URL`` (``<MAST>/api/ext/v1/runner/<run_id>/hook``, loopback,
no proxy) with the header :data:`HOOK_HEADER` = ``$MAST_RUNNER_SECRET`` and the body
``{"event": <hook_event_name>, "payload": <sanitized event>}``; MAST answers
``{"output": {...}}`` (``RunnerManager.hook`` applies :func:`decide` to the live checkpoint
state) and this script prints the output. If MAST cannot be reached or refuses, the fallback
uses marker files in ``$MAST_RUNNER_HOOK_DIR`` (``checkpoint.requested`` / ``checkpoint.ready``,
which the runner mirrors from the service state and picks up again). Every invocation is
appended to ``$MAST_RUNNER_HOOK_DIR/hook_events.jsonl``. The script always exits 0 and prints
one JSON object (``{}`` = no decision); a malformed event is allowed through, never blocked.

Claude Code behaviour this relies on (VISTA, verified with Claude Code 2.1.295 on Windows,
2026-10-09):

* PreCompact input: ``session_id, transcript_path, cwd, prompt_id, hook_event_name,
  trigger ("auto"|"manual"), custom_instructions``; it fires before an API request once the
  context passes ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` x ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` %.
  Both exit code 2 and stdout ``{"decision": "block", "reason": ...}`` block it: the
  stream shows ``system/status compacting`` then ``status null`` with no
  ``compact_boundary``, and the turn continues uncompacted (the model is not told).
* Stop input: ``... permission_mode, effort, stop_hook_active, last_assistant_message,
  background_tasks, session_crons``. ``{"decision": "block", "reason": R}`` makes Claude
  Code inject a user message ``"Stop hook feedback:\\nR"`` and continue the turn.
* PostCompact input carries ``trigger`` and ``compact_summary``.
* ``--include-hook-events`` streams ``hook_started``/``hook_response`` for Stop only.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

CHECKPOINT_INSTRUCTION = (
    "Review and update WORKING.md so it preserves relevant existing information and "
    "contains the complete continuation state needed after compaction."
)
HANDOFF_REASON = "The checkpoint is saved; end this response for handoff."
REQUEST_MARKER = "checkpoint.requested"
READY_MARKER = "checkpoint.ready"
EVENTS_FILE = "hook_events.jsonl"
CHECKPOINT_STATES = ("none", "requested", "ready")
#: Header that carries the per-run secret (the HTTP route checks it).
HOOK_HEADER = "X-MAST-Runner-Secret"
#: Environment the runner gives the ``claude`` process (hooks inherit it).
ENV_URL = "MAST_RUNNER_HOOK_URL"
ENV_HOOK_KEY = "MAST_RUNNER_SECRET"
ENV_DIR = "MAST_RUNNER_HOOK_DIR"
ENV_TIMEOUT = "MAST_RUNNER_HOOK_TIMEOUT"
_KEEP_KEYS = ("session_id", "hook_event_name", "trigger", "custom_instructions",
              "stop_hook_active", "prompt_id", "permission_mode")


def decide(event_name: str | None, checkpoint: str, *,
           terminal: bool = False) -> tuple[dict, str | None]:
    """``(hook output, side effect)``; the side effect is ``"request_checkpoint"`` or ``None``."""
    if checkpoint not in CHECKPOINT_STATES:
        checkpoint = "none"
    if event_name == "PreCompact":
        if terminal or checkpoint == "ready":
            return {"decision": "block", "reason": HANDOFF_REASON}, None
        effect = "request_checkpoint" if checkpoint == "none" else None
        return {"decision": "block", "reason": CHECKPOINT_INSTRUCTION}, effect
    if event_name == "Stop" and not terminal and checkpoint == "requested":
        return {"decision": "block", "reason": CHECKPOINT_INSTRUCTION}, None
    return {}, None


def sanitize_event(event: Any) -> dict:
    """The fields worth logging and forwarding (summaries and messages are truncated)."""
    if not isinstance(event, dict):
        return {"hook_event_name": None, "malformed": True}
    record = {key: event[key] for key in _KEEP_KEYS if key in event}
    for key in ("last_assistant_message", "compact_summary"):
        value = event.get(key)
        if isinstance(value, str):
            record[key] = value[:2000]
            record[key + "_chars"] = len(value)
    return record


def marker_state(hook_dir: Path | None) -> str:
    if hook_dir is None:
        return "none"
    if (hook_dir / READY_MARKER).is_file():
        return "ready"
    if (hook_dir / REQUEST_MARKER).is_file():
        return "requested"
    return "none"


def _http_hook(record: dict, timeout: float, env: Any) -> dict:
    url = env.get(ENV_URL, "")
    secret = env.get(ENV_HOOK_KEY, "")
    if not url or not secret:
        raise RuntimeError(f"{ENV_URL}/{ENV_HOOK_KEY} not set")
    body = json.dumps({"event": record.get("hook_event_name"), "payload": record},
                      ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", HOOK_HEADER: secret})
    # MAST is on this machine: a system proxy would only get in the way.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    output = data.get("output") if isinstance(data, dict) else None
    if not isinstance(output, dict):
        raise RuntimeError("MAST returned no hook output")
    return output


def _append(path: Path, record: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


def run(raw: bytes, environ: Any = None) -> dict:
    """Process one hook invocation; returns the output to print. Never raises."""
    env = os.environ if environ is None else environ
    hook_dir_value = env.get(ENV_DIR, "")
    hook_dir = Path(hook_dir_value) if hook_dir_value else None
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        event = None
    record = sanitize_event(event)
    log: dict = {"t": round(time.time(), 3), "event": record}
    output: dict = {}
    if record.get("malformed"):
        log["path"] = "malformed"
    else:
        try:
            timeout = float(env.get(ENV_TIMEOUT, "20"))
        except ValueError:
            timeout = 20.0
        try:
            output = _http_hook(record, timeout, env)
            log["path"] = "http"
        except Exception as exc:  # noqa: BLE001 — fall back to marker files
            log["path"] = "markers"
            log["http_error"] = f"{type(exc).__name__}: {exc}"[:500]
            state = marker_state(hook_dir)
            output, effect = decide(record.get("hook_event_name"), state)
            log["checkpoint"] = state
            if effect == "request_checkpoint" and hook_dir is not None:
                try:
                    hook_dir.mkdir(parents=True, exist_ok=True)
                    (hook_dir / REQUEST_MARKER).touch()
                    log["effect"] = effect
                except OSError as mexc:
                    log["marker_error"] = f"{type(mexc).__name__}: {mexc}"
    log["output"] = output
    if hook_dir is not None:
        _append(hook_dir / EVENTS_FILE, log)
    return output


def main() -> int:
    try:
        raw = sys.stdin.buffer.read()
    except (OSError, ValueError):
        raw = b""
    output = run(raw)
    sys.stdout.buffer.write((json.dumps(output, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
