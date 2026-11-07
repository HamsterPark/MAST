# Derived from VISTA (MIT), github.com/joshhhhhan/VISTA, backends/claude/{runner,lifecycle,preflight}.py
"""Claude Code runner: one unattended MAST run = a chain of ``claude -p`` sessions (contract §8).

README
------
``ClaudeRunner(run_id=..., task=..., service=..., run_dir=..., paths=..., cfg=...).run()`` plays one
run and returns a :class:`RunResult`. The model reaches MAST only through MAST's own MCP bridge
(``integrations/claude-code/server/run_server.py``, tools ``mast_*``). The server enforces one act
per observation (observation tokens), so the runner has no act gate of its own. The checkpoint
state lives in the VISTA service (:mod:`mast.vista.service`); the runner calls
``request_checkpoint``, ``checkpoint_state``, ``clear_checkpoint``, ``recovery_observation`` and
``close_session``, always with the run's client session ``run-<run_id>`` (the same for every
segment: the server binds the observation token to it).

A *segment* is one Claude Code process (= one session, always fresh: ``--session-id <uuid4>``).
The runner

1. asks the bridge for its tools (``initialize`` + ``tools/list`` over stdio with the segment's
   environment, :func:`probe_bridge_tools`): the allowed tools are derived, never hard-coded.
   It writes ``segment_NNN.{settings,mcp,command}.json`` (hooks in exec form; the ``mast`` MCP
   server = ``<python> -I -X utf8 run_server.py`` with ``MAST_URL``, ``MAST_ACTOR=runner`` and
   ``MAST_SESSION=run-<run_id>``), clears the session's checkpoint (every segment is a fresh
   context) and takes ``recovery_observation`` (environment record, GUIDE.md, WORKING.md, newest
   observation with a fresh token);
2. starts ``claude`` in a neutral temporary directory with a cleaned environment
   (:func:`child_environment`; subscription mode drops the API credentials) and sends the first
   user message (task block + that observation: text, then label/image blocks) as stream-json on
   stdin, which stays open;
3. validates EVERY ``system/init`` envelope (Claude Code emits one per user turn): the tool list
   must be exactly ``mcp__mast__<each bridge tool>``, the only MCP server ``mast`` connected,
   ``permissionMode`` ``default``, a resolved model id, and in subscription mode an
   ``apiKeySource`` of none (:func:`auth_problem`). Anything else ends the run (``init_invalid``);
4. reads stdout on a thread and handles every message on the runner thread. Each assistant
   ``tool_use`` id is remembered until its ``tool_result`` arrives; ids still open at the end of
   a turn or segment are recorded as *unmatched* (the server-side jobs keep running; the next
   segment's first message tells the model to check ``mast_jobs``/``mast_wait``). Images in tool
   results and in the runner's own messages are counted: at ``image_limit_per_session -
   image_reserve`` the runner requests a checkpoint (``image_limit``; the service then refuses
   actions until WORKING.md is written); past the hard limit the segment ends at once
   (``image_limit``, the next one is a ``handoff``);
5. at each ``result`` (end of a turn): final text ending with ``[DONE]``/``[ABORT]``
   (:func:`marker_outcome`) ⇒ end; operator stop ⇒ end; checkpoint ``ready`` ⇒ hand off; API
   error ⇒ back off and continue (bounded); otherwise send the continue text (plus the
   checkpoint instruction while one is requested) as the next user message of the same process;
   ``stall_turns`` consecutive turns without a progress tool ⇒ ``stalled``;
6. ends a segment by an ``interrupt`` control request (if a turn is running) and closing stdin,
   so Claude Code still emits its ``result`` with the cumulative cost. A process that does not
   exit within ``end_grace_s`` is killed with its whole tree (``taskkill /T /F``), the last
   resort. The checkpoint state and the stop flag are polled every 0.5 s, so a handoff or an
   operator stop interrupts a running turn.

Checkpoint protocol (VISTA): the PreCompact/Stop hooks (``compact_hook.py``) reach the runner
through MAST's HTTP API (``POST /api/ext/v1/runner/<run_id>/hook``, loopback, per-run secret, →
``RunnerManager.hook`` → :meth:`ClaudeRunner.on_hook`). A PreCompact without a ready checkpoint
blocks compaction and requests a checkpoint (``context_limit``); the Stop hook is blocked while a
checkpoint is requested; once WORKING.md is written the service reports ``ready`` and the runner
starts a fresh session. Marker files in ``hooks/`` are the fallback when a hook cannot reach MAST.
A native ``compact_boundary`` ends the run with outcome ``error``; so do three consecutive
checkpoint handoffs without a successful act (VISTA's ``MAX_COMPACT_CYCLES_WITHOUT_ACTION``: the
recovery context alone already crosses the compaction threshold). A segment that crashes, stops
responding for ``turn_timeout_s`` (no stream message at all: the decision lease), or exhausts its
API retries is replaced by a fresh recovery session, at most ``max_recoveries`` times; at most
``max_segments`` segments per run.

Operator stop (:meth:`ClaudeRunner.request_stop`, from ``RunnerManager.stop``, which closes the
session at the service first so further acts are refused): the running turn is interrupted,
stdin closed, and the run ends with ``stopped_by_operator``. Every run end closes the session.

Outcomes: :data:`OUTCOMES`. Records under ``run_dir``: ``status.json`` (the run, every segment
record, usage totals, unmatched tool calls), ``system_prompt.md``, ``segment_NNN.{command.json,
settings.json, mcp.json, input.json (image data replaced by sha256 + size), stdout.jsonl
(likewise), stderr.txt, hooks.jsonl, bridge.log (the bridge's own log, MAST_MCP_LOG_FILE)}`` and
``hooks/`` (the hook script's log + fallback markers).
The optional preflight segment ("Reply exactly READY") uses the stem ``preflight``; its bridge
points at an address it refuses, so a tool call there reaches nothing.

Threads: the runner thread owns the segment and is the only writer of the ``claude`` stdin; the
stdout/stderr readers only queue; HTTP threads call :meth:`on_hook`, :meth:`request_stop` and
:meth:`snapshot` (a copy published by the runner thread).

Claude Code facts this relies on (VISTA, verified with Claude Code 2.1.295 on Windows 11):
stream-json input accepts further user messages after a ``result`` in the same
process (each turn emits a new ``system/init``; ``total_cost_usd`` and ``modelUsage`` in
``result`` are cumulative per process, ``usage`` and ``num_turns`` per turn);
``{"type": "control_request", "request": {"subtype": "interrupt"}}`` ends a running turn with a
``result`` of subtype ``error_during_execution``; claude.ai connectors join the session from the
second turn on unless ``ENABLE_CLAUDEAI_MCP_SERVERS=false``; MCP image results reach the model as
image blocks; with ``CLAUDE_CODE_AUTO_COMPACT_WINDOW=W`` and ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=P``
compaction is attempted at roughly (W - 33000) x P/100 context tokens, ~150k for the defaults.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import compact_hook
from .compact_hook import CHECKPOINT_INSTRUCTION, CHECKPOINT_STATES
from .paths import RuntimePaths, RuntimeSetupError
from .prompts import (CONTINUE_TEXT, PREFLIGHT_PROMPT, SYSTEM_PROMPT, initial_message,
                      recovery_message)

#: The MCP server name of the bridge; Claude Code names its tools ``mcp__mast__<tool>``.
SERVER_NAME = "mast"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
OUTCOMES = ("done", "abort", "error", "stalled", "max_segments", "stopped_by_operator")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable", "default")
PERMISSION_MODE = "default"
RETRYABLE_API_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
SESSION_ENV_NAMES = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")
#: Credentials that make Claude Code bill an API account instead of the logged-in subscription.
#: Dropped from the child environment unless ``RunnerConfig.auth == "env"`` (which needs the
#: owner's approval): the server process must not decide who pays for a run.
STRIPPED_AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
#: Dropped with the credentials: a subscription session talks to Anthropic, and a base URL set
#: for some other client would receive the subscription's login.
STRIPPED_ENDPOINT_VARS = ("ANTHROPIC_BASE_URL",)
AUTH_MODES = ("subscription", "env")
#: Variables that would silently disable the compaction hook path.
COMPACTION_ENV_BLOCKLIST = ("DISABLE_AUTO_COMPACT", "DISABLE_COMPACT")
INTERRUPT_WAIT_S = 20.0
#: Interrupt wait and kill grace when MAST itself is shutting down.
URGENT_WAIT_S = 3.0
#: VISTA's MAX_COMPACT_CYCLES_WITHOUT_ACTION: consecutive checkpoint handoffs without an act.
MAX_CHECKPOINTS_WITHOUT_ACT = 3
POLL_S = 0.2
STATUS_EVERY_S = 0.5
DEFAULT_BASE_URL = "http://127.0.0.1:7862"
#: Tools whose successful result counts as an executed act (checkpoint-loop guard).
ACT_TOOLS = ("mast_act", "mast_run")
#: Tools that count as progress for the stall rule (waiting on the instrument is progress).
PROGRESS_TOOLS = ("mast_act", "mast_run", "mast_wait", "mast_job", "mast_cancel",
                  "mast_ask_operator", "mast_operator_reply")
#: The checkpoint protocol cannot work without them.
REQUIRED_TOOLS = ("mast_write_working",)
#: The preflight's bridge gets a non-loopback address: it lists its tools and refuses every call.
PREFLIGHT_MAST_URL = "http://preflight.invalid"
USAGE_LIMIT_MARKERS = ("usage_limit_reached", "hit your session limit", "hit your weekly limit",
                       "hit your usage limit", "usage limit reached", "session limit · resets")


class RunnerError(RuntimeError):
    pass


# ── configuration ────────────────────────────────────────────────────────────
@dataclass
class RunnerConfig:
    """Everything the runner needs besides the service, the paths and the task."""

    model: str = "claude-opus-5-5"
    effort: str = "high"
    system_prompt: str = SYSTEM_PROMPT
    continue_text: str = CONTINUE_TEXT
    compact_window_tokens: int = 200_000
    compact_pct: int = 90
    image_limit_per_session: int = 500
    #: Request the checkpoint this many images before the limit (an inspect returns up to 16).
    image_reserve: int = 20
    max_segments: int = 20
    #: Decision lease: no stream message for this long ⇒ recover.
    turn_timeout_s: float = 3600.0
    stall_turns: int = 3
    preflight: bool = True
    extra_env: dict[str, str] = field(default_factory=dict)
    #: If set, the model id Claude Code resolves must equal it.
    expected_model: str | None = None
    #: ``--max-budget-usd`` per segment (a safety cap); ``None`` = no cap.
    max_budget_usd: float | None = None
    startup_timeout_s: float = 120.0
    end_grace_s: float = 30.0
    hook_timeout_s: int = 30
    #: Per-server MCP tool timeout; ``mast_wait`` waits up to 840 s server-side (contract §6).
    mcp_tool_timeout_s: float = 900.0
    bridge_probe_timeout_s: float = 60.0
    #: Waits before continuing after a retryable API error (one entry per retry).
    api_retry_backoff_s: tuple[float, ...] = (10.0, 30.0, 60.0, 120.0)
    max_recoveries: int = 5
    max_description_chars: int = 8192
    #: ``"subscription"``: drop API credentials and refuse a session whose init reports an API key
    #: source. ``"env"``: pass the server's credentials through (API billing; owner's approval only).
    auth: str = "subscription"
    #: Stderr log level of the bridge (``MAST_MCP_LOG``).
    bridge_log_level: str = "INFO"
    act_tools: tuple[str, ...] = ACT_TOOLS
    progress_tools: tuple[str, ...] = PROGRESS_TOOLS
    required_tools: tuple[str, ...] = REQUIRED_TOOLS

    def __post_init__(self) -> None:
        if self.auth not in AUTH_MODES:
            raise ValueError(f"auth {self.auth!r} not in {AUTH_MODES}")
        if self.effort not in EFFORT_LEVELS:
            raise ValueError(f"effort {self.effort!r} not in {EFFORT_LEVELS}")
        if not isinstance(self.model, str) or not self.model.strip() or any(c.isspace() for c in self.model):
            raise ValueError(f"model {self.model!r} is not a model name")
        if not 100_000 <= int(self.compact_window_tokens) <= 1_000_000:
            raise ValueError("compact_window_tokens must be in [100000, 1000000] "
                             "(Claude Code clamps CLAUDE_CODE_AUTO_COMPACT_WINDOW to that range)")
        if not 1 <= int(self.compact_pct) <= 100:
            raise ValueError("compact_pct must be in [1, 100]")
        if int(self.image_limit_per_session) <= int(self.image_reserve) or int(self.image_reserve) < 0:
            raise ValueError("need image_limit_per_session > image_reserve >= 0")
        if int(self.max_segments) < 1 or int(self.stall_turns) < 1:
            raise ValueError("max_segments and stall_turns must be >= 1")
        if float(self.turn_timeout_s) <= 0 or float(self.mcp_tool_timeout_s) <= 0:
            raise ValueError("turn_timeout_s and mcp_tool_timeout_s must be positive")
        if int(self.max_recoveries) < 0:
            raise ValueError("max_recoveries must be >= 0")
        self.api_retry_backoff_s = tuple(float(s) for s in self.api_retry_backoff_s)

    def summary(self) -> dict:
        """JSON-safe description for the records (prompts only as hash + size)."""
        out: dict[str, Any] = {}
        for key in self.__dataclass_fields__:
            value = getattr(self, key)
            if key in ("system_prompt", "continue_text"):
                out[f"{key}_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
                out[f"{key}_chars"] = len(value)
            elif key == "extra_env":
                out[key] = sorted(value)
            elif isinstance(value, tuple):
                out[key] = list(value)
            else:
                out[key] = value
        return out


@dataclass
class RunResult:
    outcome: str
    final_text: str
    segments: list[dict]
    usage: dict
    models: list[str]
    unmatched_tool_calls: list[dict]
    preflight: dict | None = None
    error: str | None = None


# ── pure helpers (unit-tested) ────────────────────────────────────────────────
DONE_MARK, ABORT_MARK = "[DONE]", "[ABORT]"


def marker_outcome(text: str) -> tuple[str | None, str | None]:
    """(outcome, note): ``('done'|'abort', None)`` when the stripped reply ENDS with a
    marker; ``(None, 'marker_in_body')`` when a marker appears elsewhere; ``(None, None)``.
    (STM-Bench ``stmbench.harness.ic_driver.marker_outcome``, copied: MAST does not need STM-Bench.)"""
    s = str(text or "").strip()
    # tolerate trailing punctuation / markdown emphasis after the marker ("[DONE]。", "**[DONE]**")
    s = s.rstrip(" \t\r\n.。!！*_`~>」』”\"'")      # never strip ']' — it closes the marker
    if s.endswith(DONE_MARK):
        return "done", None
    if s.endswith(ABORT_MARK):
        return "abort", None
    if DONE_MARK in s or ABORT_MARK in s:
        return None, "marker_in_body"
    return None, None


def sanitize(value: Any) -> Any:
    """Copy of ``value`` for logs: ``token`` keys redacted, long base64 ``data`` hashed."""
    if isinstance(value, dict):
        out: dict = {}
        for key, item in value.items():
            if key == "token" and isinstance(item, str):
                out[key] = "<redacted>"
            elif key in ("data", "data_b64") and isinstance(item, str) and len(item) > 64:
                out[key] = {"sha256": hashlib.sha256(item.encode("utf-8")).hexdigest(), "chars": len(item)}
            else:
                out[key] = sanitize(item)
        return out
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def auth_problem(init: Mapping[str, Any], auth: str) -> str | None:
    """Why this session must not run under ``auth``, or ``None``: in subscription mode an init
    that reports an API key source would bill that key."""
    src = init.get("apiKeySource")
    if auth == "subscription" and isinstance(src, str) and src not in ("", "none"):
        return (f"apiKeySource {src!r}: this session would bill an API key, but the runner is in "
                "subscription mode")
    return None


def child_environment(base: Mapping[str, str], cfg: RunnerConfig, *, hook_url: str, hook_secret: str,
                      hook_dir: Path) -> dict[str, str]:
    """The ``claude`` process environment: the server's minus everything that ties a process to a
    calling Claude Code session (and, in subscription mode, the API credentials), plus the
    harness settings and what the hook script needs."""
    subscription = cfg.auth == "subscription"
    env = {k: v for k, v in base.items()
           if not k.startswith("CLAUDE_CODE_") and k not in SESSION_ENV_NAMES
           and k not in COMPACTION_ENV_BLOCKLIST
           and not (subscription and (k in STRIPPED_AUTH_VARS or k in STRIPPED_ENDPOINT_VARS))}
    env.update({
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "CLAUDE_CODE_DISABLE_CRON": "1",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
        "CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH": str(int(cfg.max_description_chars)),
        "MAX_MCP_OUTPUT_TOKENS": "100000",
        "MCP_TOOL_TIMEOUT": str(int(float(cfg.mcp_tool_timeout_s) * 1000)),   # mast_wait waits up to 840 s
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(int(cfg.compact_window_tokens)),
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(int(cfg.compact_pct)),
        # Verified: without this, claude.ai connectors (Docs, Drive ...) join the
        # session from the second turn on, --strict-mcp-config notwithstanding.
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "ENABLE_TOOL_SEARCH": "false",            # MCP tools up front, no ToolSearch indirection
        "MCP_CONNECTION_NONBLOCKING": "0",        # stream-json input does not wait otherwise
        compact_hook.ENV_URL: hook_url,
        compact_hook.ENV_HOOK_KEY: hook_secret,
        compact_hook.ENV_DIR: str(hook_dir),
        compact_hook.ENV_TIMEOUT: str(max(1, int(cfg.hook_timeout_s) - 5)),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.update({str(k): str(v) for k, v in cfg.extra_env.items()})
    return env


def bridge_env(*, base_url: str, session: str, fetch_dir: Path, log_level: str = "INFO",
               log_file: Path | None = None) -> dict[str, str]:
    """The ``env`` of the ``mast`` MCP server entry (contract §9; same session for every segment).
    ``MAST_MCP_LOG`` is the bridge's log level; ``MAST_MCP_LOG_FILE`` (appended) its log file."""
    env = {"MAST_URL": base_url, "MAST_ACTOR": "runner", "MAST_SESSION": session,
           "MAST_FETCH_DIR": str(fetch_dir), "MAST_MCP_LOG": log_level, "PYTHONUTF8": "1"}
    if log_file is not None:
        env["MAST_MCP_LOG_FILE"] = str(log_file)
    return env


def mcp_config(cfg: RunnerConfig, *, python: str, script: str, env: Mapping[str, str]) -> dict:
    return {"mcpServers": {SERVER_NAME: {
        "type": "stdio",
        "command": str(python),
        "args": ["-I", "-X", "utf8", str(script)],
        "env": dict(env),
        "timeout": int(float(cfg.mcp_tool_timeout_s) * 1000),
        "alwaysLoad": True,
    }}}


def hook_settings(cfg: RunnerConfig, *, python: str, hook_script: str) -> dict:
    hook = {"type": "command", "command": str(python),
            "args": ["-I", "-X", "utf8", str(hook_script)], "timeout": int(cfg.hook_timeout_s)}
    return {"autoCompactEnabled": True,
            "hooks": {event: [{"hooks": [hook]}] for event in ("PreCompact", "PostCompact", "Stop")}}


def build_command(cfg: RunnerConfig, *, claude: Sequence[str], system_prompt_path: Path,
                  settings_path: Path, mcp_path: Path, allowed_tools: Sequence[str],
                  session_id: str) -> list[str]:
    cmd = [*claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--include-hook-events", "--model", cfg.model, "--effort", cfg.effort,
           "--system-prompt-file", str(system_prompt_path), "--setting-sources", "",
           "--settings", str(settings_path), "--mcp-config", str(mcp_path), "--strict-mcp-config",
           "--tools", "", "--allowedTools", ",".join(allowed_tools),
           "--permission-mode", PERMISSION_MODE, "--permission-prompts", "none",
           "--disable-slash-commands", "--prompt-suggestions", "false", "--no-chrome",
           "--session-id", session_id]
    if cfg.max_budget_usd is not None:
        cmd += ["--max-budget-usd", f"{float(cfg.max_budget_usd):g}"]
    return cmd


def user_message(text: str, images: Sequence[Mapping[str, Any]] = ()) -> dict:
    """One stream-json user message: the text, then per image its label and the image
    (``images`` as the service gives them: ``{"label", "mime", "data_b64"}``)."""
    content: list[dict] = [{"type": "text", "text": text}]
    for image in images:
        label = image.get("label")
        if label:
            content.append({"type": "text", "text": str(label)})
        content.append({"type": "image", "source": {"type": "base64",
                                                    "media_type": str(image.get("mime") or "image/png"),
                                                    "data": str(image.get("data_b64") or "")}})
    return {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}


def validate_init(envelope: Mapping[str, Any], expected_tools: Sequence[str], *,
                  expected_model: str | None = None) -> list[str]:
    """Problems with a ``system/init`` envelope (empty list = valid)."""
    problems: list[str] = []
    tools = envelope.get("tools")
    expected = sorted(expected_tools)
    if not isinstance(tools, list) or len(tools) != len(set(tools)) or sorted(tools) != expected:
        extra = sorted(set(tools or []) - set(expected)) if isinstance(tools, list) else tools
        missing = sorted(set(expected) - set(tools or [])) if isinstance(tools, list) else expected
        problems.append(f"unexpected tool surface (extra {extra}, missing {missing})")
    servers = envelope.get("mcp_servers")
    if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], dict) \
            or servers[0].get("name") != SERVER_NAME:
        problems.append(f"MCP servers must be exactly [{SERVER_NAME!r}], got "
                        f"{[s.get('name') for s in servers if isinstance(s, dict)] if isinstance(servers, list) else servers}")
    elif str(servers[0].get("status", "")).lower() != "connected":
        problems.append(f"MCP server {SERVER_NAME!r} status is {servers[0].get('status')!r}")
    if envelope.get("mcp_server_errors"):
        problems.append(f"mcp_server_errors: {envelope.get('mcp_server_errors')}")
    if envelope.get("permissionMode") != PERMISSION_MODE:
        problems.append(f"permissionMode {envelope.get('permissionMode')!r} != {PERMISSION_MODE!r}")
    model = envelope.get("model")
    if not isinstance(model, str) or not model.strip():
        problems.append("no resolved model id")
    elif model.strip().lower() in MODEL_ALIASES:
        problems.append(f"model {model!r} is an alias, not a resolved id")
    elif expected_model is not None and model != expected_model:
        problems.append(f"model {model!r} != expected {expected_model!r}")
    return problems


def init_summary(envelope: Mapping[str, Any] | None) -> dict | None:
    if envelope is None:
        return None
    keys = ("session_id", "model", "tools", "mcp_servers", "permissionMode", "claude_code_version",
            "skills", "plugins", "slash_commands", "output_style", "apiKeySource", "capabilities")
    return {k: envelope[k] for k in keys if k in envelope}


def usage_limit_text(message: Mapping[str, Any]) -> str | None:
    """The account's usage-limit message when this ``result`` is one, else ``None``.

    A subscription usage limit is a 429 that no retry within the run can outlast (it resets
    hours later), so it is not ``retryable``: the run ends at once and says so."""
    if message.get("subtype") == "success" and message.get("is_error") is not True:
        return None
    raw = [message.get("result"), message.get("errors")]
    text = json.dumps(raw, ensure_ascii=False).lower()
    if any(m in text for m in USAGE_LIMIT_MARKERS):
        r = message.get("result")
        return (r if isinstance(r, str) and r else json.dumps(raw, ensure_ascii=False))[:300]
    return None


def classify_result(message: Mapping[str, Any]) -> str:
    """``ok`` | ``budget_usd`` | ``usage_limit`` | ``retryable`` | ``error`` for a stream ``result``."""
    subtype = message.get("subtype")
    if subtype == "success" and message.get("is_error") is not True:
        return "ok"
    if subtype == "error_max_budget_usd":
        return "budget_usd"
    if usage_limit_text(message) is not None:
        return "usage_limit"
    status = message.get("api_error_status")
    if isinstance(status, int) and (status in RETRYABLE_API_STATUS or status >= 500):
        return "retryable"
    if message.get("terminal_reason") == "api_error":
        return "retryable"
    text = json.dumps([message.get("result"), message.get("errors")], ensure_ascii=False).lower()
    if "overloaded" in text or "rate limit" in text or "529" in text:
        return "retryable"
    return "error"


def result_usage(message: Mapping[str, Any] | None) -> dict | None:
    """Cumulative usage of the process from a ``result``'s ``modelUsage`` and ``total_cost_usd``."""
    if not isinstance(message, Mapping):
        return None
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0}
    model_usage = message.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        for item in model_usage.values():
            if not isinstance(item, dict):
                continue
            totals["input_tokens"] += int(item.get("inputTokens") or 0)
            totals["output_tokens"] += int(item.get("outputTokens") or 0)
            totals["cache_creation_input_tokens"] += int(item.get("cacheCreationInputTokens") or 0)
            totals["cache_read_input_tokens"] += int(item.get("cacheReadInputTokens") or 0)
    else:
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        for key in totals:
            totals[key] = int(usage.get(key) or 0)
    cost = message.get("total_cost_usd")
    totals["cost_usd"] = float(cost) if isinstance(cost, (int, float)) else None
    return totals


def creationflags() -> int:
    """New process group (no console signals from the server) and no console window."""
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill ``proc`` and its children (Windows: ``taskkill /T /F``). The last resort."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           timeout=30, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def probe_bridge_tools(python: str, script: str, env: Mapping[str, str], *,
                       timeout: float = 60.0) -> list[dict]:
    """The bridge's ``tools/list``, asked the way Claude Code asks it (stdio, ``initialize``
    first, cursors followed). Raises :class:`RuntimeSetupError` with the bridge's stderr tail."""
    try:
        proc = subprocess.Popen([str(python), "-I", "-X", "utf8", str(script)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(env),
                                creationflags=creationflags())
    except OSError as exc:
        raise RuntimeSetupError(f"could not start the MCP bridge ({python} {script}): {exc}") from exc
    lines: "queue.Queue[bytes | None]" = queue.Queue()
    err = bytearray()

    def pump_out() -> None:
        for raw in proc.stdout:  # type: ignore[union-attr]
            lines.put(raw)
        lines.put(None)

    def pump_err() -> None:
        for raw in proc.stderr:  # type: ignore[union-attr]
            err.extend(raw)
            del err[:-4000]

    readers = [threading.Thread(target=pump_out, name="bridge-probe-out", daemon=True),
               threading.Thread(target=pump_err, name="bridge-probe-err", daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + float(timeout)

    def send(message: dict) -> None:
        proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def answer(rid: int, method: str) -> dict:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RuntimeSetupError(f"the MCP bridge did not answer {method} within {timeout:g} s")
            try:
                raw = lines.get(timeout=min(left, 0.5))
            except queue.Empty:
                continue
            if raw is None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                readers[1].join(timeout=5)
                tail = bytes(err).decode("utf-8", "replace").strip()[-800:]
                raise RuntimeSetupError(f"the MCP bridge exited before answering {method} "
                                        f"(code {proc.poll()}): {tail or 'no stderr'}")
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(message, dict) and message.get("id") == rid:
                if message.get("error") is not None:
                    raise RuntimeSetupError(f"the MCP bridge refused {method}: {message['error']}")
                result = message.get("result")
                return result if isinstance(result, dict) else {}

    ok = False
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "mast-runner-probe", "version": "1"}}})
        answer(1, "initialize")
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        tools: list[dict] = []
        cursor = None
        for rid in range(2, 102):
            send({"jsonrpc": "2.0", "id": rid, "method": "tools/list",
                  "params": {"cursor": cursor} if cursor else {}})
            result = answer(rid, "tools/list")
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        ok = True
        return tools
    except OSError as exc:
        raise RuntimeSetupError(f"lost the MCP bridge while listing its tools: {exc}") from exc
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass
        try:
            proc.wait(timeout=10 if ok else 0.5)      # a bridge that failed the probe gets no grace
        except subprocess.TimeoutExpired:
            kill_tree(proc)


def _atomic_json(path: Path, obj: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _short(name: Any) -> str:
    return name[len(TOOL_PREFIX):] if isinstance(name, str) and name.startswith(TOOL_PREFIX) else str(name)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ── Windows: the claude process tree dies with MAST ──────────────────────────
class KillOnCloseJob:
    """A Windows job object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: every process assigned to
    it (and the children it starts afterwards) is killed when the job's last handle closes, i.e.
    when the MAST process ends, however it ends. An unattended agent must not outlive the server it
    acts through. The handle is never closed on purpose. Best effort: elsewhere, or when Windows
    refuses, :meth:`assign` returns False and nothing else changes."""

    def __init__(self) -> None:
        self._handle: Any = None
        self._kernel32: Any = None
        self.error: str | None = None
        if os.name != "nt":
            self.error = "not Windows"
            return
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                    wintypes.DWORD)
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            k32.AssignProcessToJobObject.restype = wintypes.BOOL

            class IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BasicLimits(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                            ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            info = ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            self._handle, self._kernel32 = handle, k32
        except (OSError, AttributeError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def assign(self, proc: subprocess.Popen) -> bool:
        if self._handle is None or getattr(proc, "_handle", None) is None:
            return False
        try:
            return bool(self._kernel32.AssignProcessToJobObject(self._handle, int(proc._handle)))  # type: ignore[attr-defined]
        except (OSError, ValueError, TypeError):
            return False


_JOB: KillOnCloseJob | None = None
_JOB_LOCK = threading.Lock()


def process_job() -> KillOnCloseJob:
    """The process-wide :class:`KillOnCloseJob` (created on first use)."""
    global _JOB
    with _JOB_LOCK:
        if _JOB is None:
            _JOB = KillOnCloseJob()
        return _JOB


# ── per-segment state ────────────────────────────────────────────────────────
@dataclass
class _Segment:
    index: int | None                 # None for the preflight
    stem: str
    kind: str                         # initial | checkpoint | handoff | recovery | preflight
    session_id: str
    expected_tools: list = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    started_wall: str = field(default_factory=_now)
    proc: subprocess.Popen | None = None
    queue: "queue.Queue[tuple[str, Any]]" = field(default_factory=queue.Queue)
    markers: str = "none"             # checkpoint state last mirrored into the hook markers
    init: dict | None = None
    init_count: int = 0
    claude_code_version: str | None = None
    models: set = field(default_factory=set)
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    results: list = field(default_factory=list)
    last_result: dict | None = None
    turn_open: bool = False
    turn_progress: bool = False
    stall: int = 0
    api_failures: int = 0
    final_text: str = ""
    last_text: str = ""
    end_reason: str | None = None
    marker: str | None = None
    open_tools: dict = field(default_factory=dict)      # tool_use id -> {"name", "turn"}
    unmatched: list = field(default_factory=list)       # [{"id", "name", "turn", "at"}]
    tool_results: int = 0
    acts: int = 0                     # act tools whose result was not an error (refused/failed are)
    act_errors: int = 0
    images_input: int = 0             # images in the runner's own user messages
    images_results: int = 0           # images in tool results
    tool_uses: int = 0
    tool_counts: dict = field(default_factory=dict)
    compaction_attempts: int = 0
    compact_boundaries: list = field(default_factory=list)
    hook_events: list = field(default_factory=list)
    stop_hook_blocks: int = 0
    api_retries: list = field(default_factory=list)
    permission_denials: list = field(default_factory=list)
    rate_limit: dict | None = None
    checkpoint_armed: bool = True
    image_hard_limit: bool = False
    checkpoint_requests: list = field(default_factory=list)
    interrupt_sent: bool = False
    interrupt_deadline: float = 0.0
    kill_deadline: float = 0.0
    stdin_closed: bool = False
    killed: bool = False
    exit_code: int | None = None
    last_activity: float = field(default_factory=time.monotonic)
    messages_sent: int = 0
    inputs: list = field(default_factory=list)
    stderr_tail: bytes = b""
    ended_wall: str | None = None
    duration_s: float | None = None

    @property
    def preflight(self) -> bool:
        return self.kind == "preflight"

    @property
    def images_sent(self) -> int:
        return self.images_input + self.images_results

    def record(self) -> dict:
        usage = result_usage(self.last_result)
        return {
            "index": self.index, "stem": self.stem, "kind": self.kind, "session_id": self.session_id,
            "started_at": self.started_wall, "ended_at": self.ended_wall, "duration_s": self.duration_s,
            "claude_code_version": self.claude_code_version, "models": sorted(self.models),
            "init": init_summary(self.init), "init_events": self.init_count,
            "exit_code": self.exit_code, "killed": self.killed, "end_reason": self.end_reason,
            "marker": self.marker, "usage": usage, "cost_usd": usage.get("cost_usd") if usage else None,
            "usage_complete": bool(self.last_result) and not self.turn_open,
            "num_turns": sum(int(r.get("num_turns") or 0) for r in self.results),
            "turns": len(self.results), "results": list(self.results),
            "messages_sent": self.messages_sent, "images_input": self.images_input,
            "images_results": self.images_results, "images_sent": self.images_sent,
            "tool_uses": self.tool_uses, "tool_results": self.tool_results,
            "tool_counts": dict(self.tool_counts), "acts": self.acts, "act_errors": self.act_errors,
            "open_tool_calls": len(self.open_tools), "unmatched_tool_calls": list(self.unmatched),
            "compaction_attempts": self.compaction_attempts,
            "compact_boundaries": list(self.compact_boundaries),
            "compact_boundary_count": len(self.compact_boundaries),
            "hook_events": len(self.hook_events), "stop_hook_blocks": self.stop_hook_blocks,
            "checkpoint_requests": list(self.checkpoint_requests), "api_retries": list(self.api_retries),
            "permission_denials": list(self.permission_denials), "rate_limit": self.rate_limit,
            "problems": list(self.problems), "notes": list(self.notes),
            "stderr_tail": self.stderr_tail.decode("utf-8", "replace")[-2000:],
        }


_HEAVY_SEGMENT_KEYS = ("results", "init", "stderr_tail", "permission_denials", "api_retries")


def compact_record(record: Mapping[str, Any]) -> dict:
    """A segment record without its per-turn detail (for the HTTP view; ``status.json`` keeps all)."""
    out = {k: v for k, v in record.items() if k not in _HEAVY_SEGMENT_KEYS}
    out["permission_denials"] = len(record.get("permission_denials") or [])
    out["api_retries"] = len(record.get("api_retries") or [])
    return out


# ── the runner ───────────────────────────────────────────────────────────────
class ClaudeRunner:
    """Drives one run through Claude Code segments (see the module docstring)."""

    def __init__(self, *, run_id: str, task: str, service: Any, run_dir: str | Path,
                 paths: RuntimePaths, cfg: RunnerConfig | None = None,
                 base_url: str = DEFAULT_BASE_URL, hook_secret: str | None = None,
                 base_env: Mapping[str, str] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 job: KillOnCloseJob | None = None, meta: Mapping[str, Any] | None = None) -> None:
        self.run_id = str(run_id)
        self.session = f"run-{self.run_id}"
        self.task = str(task)
        self.svc = service
        self.cfg = cfg or RunnerConfig()
        self.paths = paths
        self.base_url = str(base_url).rstrip("/")
        self.hook_url = f"{self.base_url}/api/ext/v1/runner/{self.run_id}/hook"
        self.hook_secret = hook_secret or secrets.token_urlsafe(32)
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.hook_dir = self.run_dir / "hooks"
        self.hook_dir.mkdir(exist_ok=True)
        self.fetch_dir = self.run_dir / "fetch"
        self.cwd: Path | None = None
        self.meta = dict(meta or {})
        self._base_env = dict(os.environ if base_env is None else base_env)
        self._sleep_fn = sleep
        self._job = job
        self._seg: _Segment | None = None
        self._seg_lock = threading.RLock()
        self._lock = threading.RLock()
        self._hook_log_lock = threading.Lock()
        self._stop = threading.Event()
        self._stop_info: dict | None = None
        self._urgent = False
        self._service_problems: list[str] = []
        self.segments: list[dict] = []
        self.unmatched: list[dict] = []
        self.preflight_result: dict | None = None
        self.models: set = set()
        self.claude_code_version: str | None = None
        self.state = "starting"
        self.outcome: str | None = None
        self.final_text = ""
        self.error: str | None = None
        self.created_at = _now()
        self.ended_at: str | None = None
        self.system_prompt_path = self.run_dir / "system_prompt.md"
        self.system_prompt_path.write_text(self.cfg.system_prompt or "", encoding="utf-8", newline="\n")
        self._view: dict = {}
        self._publish(write=True)

    # ── public API (any thread) ──────────────────────────────────────────────
    def request_stop(self, reason: str, *, urgent: bool = False) -> bool:
        """Operator stop: the running turn is interrupted and the run ends with
        ``stopped_by_operator``. ``urgent`` (MAST shutting down) shortens every wait. Returns
        False when a stop was already requested."""
        with self._lock:
            self._urgent = self._urgent or bool(urgent)
            if self._stop.is_set():
                return False
            self._stop_info = {"reason": str(reason or "operator stop")[:300], "requested_at": _now(),
                               "urgent": bool(urgent)}
            self._stop.set()
            if self._view:
                self._view = {**self._view, "stop": dict(self._stop_info)}
        return True

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def snapshot(self) -> dict:
        """The run's status as last published by the runner thread (a copy)."""
        with self._lock:
            return json.loads(json.dumps(self._view, default=str))

    def on_hook(self, event: str | None, payload: Mapping[str, Any]) -> dict:
        """A Claude Code hook (PreCompact/PostCompact/Stop) relayed by ``RunnerManager.hook``:
        the decision for the live checkpoint state (:func:`compact_hook.decide`)."""
        payload = dict(payload or {})
        name = payload.get("hook_event_name") or event
        with self._seg_lock:
            seg = self._seg
        record: dict[str, Any] = {"t": round(time.time(), 3), "event": payload}
        stale = seg is not None and payload.get("session_id") not in (None, seg.session_id)
        if seg is None or seg.preflight or stale:
            output: dict = {}
            record["note"] = ("stale session" if stale else "preflight" if seg is not None
                              else "no active segment")
        else:
            checkpoint = self._checkpoint_state()
            output, effect = compact_hook.decide(name, checkpoint, terminal=self._stop.is_set())
            record.update({"checkpoint": checkpoint, "effect": effect})
            if effect == "request_checkpoint":
                try:
                    self._request_checkpoint(seg, "context_limit")
                except Exception as exc:  # noqa: BLE001
                    record["error"] = f"{type(exc).__name__}: {exc}"
        record["output"] = output
        if seg is not None:
            seg.hook_events.append(record)
            with self._hook_log_lock:
                try:
                    with (self.run_dir / f"{seg.stem}.hooks.jsonl").open("a", encoding="utf-8",
                                                                         newline="\n") as fh:
                        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                except OSError:
                    pass
        return output

    def run(self) -> RunResult:
        outcome: str | None = None
        self.cwd = Path(tempfile.mkdtemp(prefix="mast_cc_"))
        with self._lock:
            self.state = "running"
        self._publish(write=True)
        try:
            outcome = self._check_bridge()
            if outcome is None and self.cfg.preflight and not self._stop.is_set():
                pf = self.preflight()
                if not pf["ok"] and not self._stop.is_set():
                    outcome = "error"
                    self.error = "preflight failed: " + "; ".join(pf["problems"])
            if outcome is None and self._stop.is_set():
                outcome = "stopped_by_operator"
            if outcome is None:
                outcome = self._episode()
        except Exception as exc:  # noqa: BLE001 — the run must always be finished
            outcome = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            try:
                (self.run_dir / "runner_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass
        finally:
            shutil.rmtree(self.cwd, ignore_errors=True)
        if outcome not in OUTCOMES:
            self.error = (self.error or "") + f" (unknown outcome {outcome!r})"
            outcome = "error"
        stop = self._stop_info or {}
        self._close_session(f"stopped by the operator: {stop.get('reason')}" if outcome == "stopped_by_operator"
                            else f"runner run ended: {outcome}")
        with self._lock:
            self.outcome = outcome
            self.state = "finished"
            self.ended_at = _now()
        self._publish(write=True)
        return RunResult(outcome=outcome, final_text=self.final_text, segments=list(self.segments),
                         usage=self._totals(), models=sorted(self.models),
                         unmatched_tool_calls=list(self.unmatched), preflight=self.preflight_result,
                         error=self.error)

    def preflight(self) -> dict:
        """Throw-away segment: same flags and tools (the bridge refuses every call), the reply must
        be READY and a Stop hook must have reached MAST."""
        seg = _Segment(index=None, stem="preflight", kind="preflight", session_id=str(uuid.uuid4()))
        problems: list[str] = []
        try:
            self._run_segment(seg)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{type(exc).__name__}: {exc}")
        reply = (seg.final_text or "").strip()
        problems += list(seg.problems)
        if seg.init is None:
            problems.append("no system/init envelope")
        if seg.end_reason != "preflight_done":
            problems.append(f"preflight segment ended with {seg.end_reason!r}")
        if reply != "READY":
            problems.append(f"reply {reply[:200]!r} is not 'READY'")
        if seg.tool_uses:
            problems.append(f"the model called tools during the preflight: {seg.tool_counts}")
        if not any(e.get("event", {}).get("hook_event_name") == "Stop" for e in seg.hook_events):
            problems.append("no Stop hook event reached MAST (hooks inactive or failing)")
        init = init_summary(seg.init) or {}
        usage = result_usage(seg.last_result)
        self.preflight_result = {
            "ok": not problems, "problems": problems, "reply": reply,
            "model": init.get("model"), "models": sorted(seg.models),
            "claude_code_version": init.get("claude_code_version"),
            "tools": init.get("tools"), "mcp_servers": init.get("mcp_servers"),
            "permission_mode": init.get("permissionMode"), "api_key_source": init.get("apiKeySource"),
            "session_id": seg.session_id, "cost_usd": usage.get("cost_usd") if usage else None,
            "usage": usage, "hook_events": len(seg.hook_events), "duration_s": seg.duration_s,
            "segment": seg.record(),
        }
        self._publish(write=True)
        return self.preflight_result

    # ── run life cycle ───────────────────────────────────────────────────────
    def _check_bridge(self) -> str | None:
        """Probe the bridge once before anything costs money: it must start, list its tools and
        offer the tools the checkpoint protocol needs."""
        env = {**self._child_env(), **self._bridge_env(preflight=False)}
        tools = probe_bridge_tools(self.paths.bridge_python, self.paths.bridge_script, env,
                                   timeout=self.cfg.bridge_probe_timeout_s)
        names = [t["name"] for t in tools]
        missing = [t for t in self.cfg.required_tools if t not in names]
        if missing:
            self.error = (f"the MCP bridge offers no {', '.join(missing)} (the checkpoint protocol needs "
                          f"it); it lists {len(names)} tools: {', '.join(names)}")
            return "error"
        return None

    def _episode(self) -> str:
        kind = "initial"
        recoveries = 0
        idle_checkpoints = 0
        while True:
            if self._stop.is_set():
                return "stopped_by_operator"
            if len(self.segments) >= int(self.cfg.max_segments):
                return "max_segments"
            index = len(self.segments)
            seg = _Segment(index=index, stem=f"segment_{index:03d}", kind=kind, session_id=str(uuid.uuid4()))
            self._run_segment(seg)
            if seg.final_text:
                self.final_text = seg.final_text
            reason = seg.end_reason
            if reason == "marker" and seg.marker in ("done", "abort"):
                return seg.marker
            if reason == "operator_stop" or self._stop.is_set():
                return "stopped_by_operator"
            if reason == "stalled":
                return "stalled"
            if reason == "usage_limit":
                self.error = f"usage_limit: {'; '.join(seg.problems) or reason}"
                return "error"
            if reason in ("init_invalid", "native_compact", "max_budget_usd"):
                self.error = f"segment {index} ended with {reason}: {'; '.join(seg.problems) or reason}"
                return "error"
            if reason == "checkpoint":
                idle_checkpoints = idle_checkpoints + 1 if seg.acts == 0 else 0
                if idle_checkpoints >= MAX_CHECKPOINTS_WITHOUT_ACT:
                    self.error = (f"{idle_checkpoints} consecutive checkpoint handoffs without a successful act "
                                  "(does the recovery context already exceed the compaction threshold?)")
                    return "error"
                kind = "checkpoint"
            elif reason == "image_limit":
                kind = "handoff"
            else:                                   # lease_expired, process_exit, api_error, ...
                recoveries += 1
                if recoveries > int(self.cfg.max_recoveries):
                    self.error = f"too many recoveries (last: segment {index} ended with {reason})"
                    return "error"
                if reason == "api_error" and self.cfg.api_retry_backoff_s:
                    self._sleep(float(self.cfg.api_retry_backoff_s[-1]))
                kind = "recovery"

    def _sleep(self, seconds: float) -> None:
        if self._sleep_fn is not None:
            self._sleep_fn(seconds)
        else:
            self._stop.wait(seconds)            # an operator stop cuts the back-off short

    # ── service calls ────────────────────────────────────────────────────────
    def _checkpoint_state(self) -> str:
        try:
            state = self.svc.checkpoint_state(self.session)
        except Exception as exc:  # noqa: BLE001
            self._note_service_problem(f"checkpoint_state raised {type(exc).__name__}: {exc}")
            return "none"
        return state if state in CHECKPOINT_STATES else "none"

    def _request_checkpoint(self, seg: _Segment, reason: str) -> None:
        self.svc.request_checkpoint(self.session, reason)
        seg.checkpoint_requests.append({"reason": reason, "t": round(time.monotonic() - seg.started, 3),
                                        "images_sent": seg.images_sent})

    def _close_session(self, reason: str) -> None:
        try:
            self.svc.close_session(self.session, reason[:200])
        except Exception as exc:  # noqa: BLE001
            self._note_service_problem(f"close_session raised {type(exc).__name__}: {exc}")

    def _note_service_problem(self, text: str) -> None:
        if text not in self._service_problems:
            self._service_problems.append(text)

    # ── one segment ──────────────────────────────────────────────────────────
    def _paths(self, stem: str) -> dict[str, Path]:
        return {name: self.run_dir / f"{stem}.{suffix}" for name, suffix in (
            ("command", "command.json"), ("settings", "settings.json"), ("mcp", "mcp.json"),
            ("input", "input.json"), ("stdout", "stdout.jsonl"), ("stderr", "stderr.txt"),
            ("hooks", "hooks.jsonl"))}

    def _child_env(self) -> dict[str, str]:
        return child_environment(self._base_env, self.cfg, hook_url=self.hook_url,
                                 hook_secret=self.hook_secret, hook_dir=self.hook_dir)

    def _bridge_env(self, *, preflight: bool, stem: str | None = None) -> dict[str, str]:
        return bridge_env(base_url=PREFLIGHT_MAST_URL if preflight else self.base_url,
                          session=f"{self.session}-preflight" if preflight else self.session,
                          fetch_dir=self.fetch_dir, log_level=self.cfg.bridge_log_level,
                          log_file=self.run_dir / f"{stem}.bridge.log" if stem else None)

    def _first_message(self, seg: _Segment) -> tuple[str, list]:
        """Task block + the service's recovery observation; clears the checkpoint first (every
        segment is a fresh context)."""
        self.svc.clear_checkpoint(self.session)
        if self._checkpoint_state() == "ready":
            seg.checkpoint_armed = False
            seg.problems.append("the checkpoint was still 'ready' after clear_checkpoint")
        obs = self.svc.recovery_observation(self.session) or {}
        text = str(obs.get("text") or "")
        images = [im for im in obs.get("images") or [] if isinstance(im, Mapping)]
        if seg.kind == "initial":
            return initial_message(self.task, text), images
        previous = self.segments[-1].get("unmatched_tool_calls") if self.segments else []
        return recovery_message(self.task, text, kind=seg.kind, unmatched=previous or ()), images

    def _run_segment(self, seg: _Segment) -> None:
        cfg = self.cfg
        paths = self._paths(seg.stem)
        threads: list[threading.Thread] = []
        with self._seg_lock:
            self._seg = seg
        try:
            env = self._child_env()
            mcp_env = self._bridge_env(preflight=seg.preflight, stem=seg.stem)
            probe_env = {**env, **mcp_env}
            probe_env.pop("MAST_MCP_LOG_FILE", None)          # the segment's log is the bridge Claude Code starts
            try:
                tools = probe_bridge_tools(self.paths.bridge_python, self.paths.bridge_script,
                                           probe_env, timeout=cfg.bridge_probe_timeout_s)
            except RuntimeSetupError as exc:
                seg.problems.append(str(exc))
                seg.end_reason = "startup_failed"
                return
            seg.expected_tools = [TOOL_PREFIX + str(t["name"]) for t in tools]
            _atomic_json(paths["settings"], hook_settings(cfg, python=self.paths.bridge_python,
                                                          hook_script=self.paths.hook_script))
            _atomic_json(paths["mcp"], mcp_config(cfg, python=self.paths.bridge_python,
                                                  script=self.paths.bridge_script, env=mcp_env))
            cmd = build_command(cfg, claude=self.paths.claude, system_prompt_path=self.system_prompt_path,
                                settings_path=paths["settings"], mcp_path=paths["mcp"],
                                allowed_tools=seg.expected_tools, session_id=seg.session_id)
            _atomic_json(paths["command"], {"argv": cmd, "cwd": str(self.cwd)})
            for marker in (compact_hook.REQUEST_MARKER, compact_hook.READY_MARKER):
                (self.hook_dir / marker).unlink(missing_ok=True)
            if seg.preflight:
                text, images = PREFLIGHT_PROMPT, []
            else:
                try:
                    text, images = self._first_message(seg)
                except Exception as exc:  # noqa: BLE001
                    seg.problems.append(f"the recovery observation failed: {type(exc).__name__}: {exc}")
                    seg.end_reason = "startup_failed"
                    return
            self._publish()
            try:
                seg.proc = subprocess.Popen(cmd, cwd=str(self.cwd), env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            creationflags=creationflags())
            except OSError as exc:
                seg.problems.append(f"could not start Claude Code: {exc}")
                seg.end_reason = "startup_failed"
                return
            if self._job is not None and not self._job.assign(seg.proc):
                seg.notes.append(f"not in the kill-on-close job ({self._job.error or 'assign failed'})")
            threads = [
                threading.Thread(target=self._read_stdout, args=(seg, seg.proc.stdout, paths["stdout"]),
                                 name=f"{self.run_id}-{seg.stem}-stdout", daemon=True),
                threading.Thread(target=self._read_stderr, args=(seg, seg.proc.stderr, paths["stderr"]),
                                 name=f"{self.run_id}-{seg.stem}-stderr", daemon=True)]
            for thread in threads:
                thread.start()
            self._send_user(seg, text, images, paths["input"])
            self._loop(seg, paths)
        finally:
            self._shutdown(seg, threads, paths)
            seg.ended_wall = _now()
            seg.duration_s = round(time.monotonic() - seg.started, 3)
            self.models |= seg.models
            if seg.claude_code_version:
                self.claude_code_version = seg.claude_code_version
            if not seg.preflight:
                record = seg.record()
                self.segments.append(record)
                self.unmatched.extend({**u, "segment": seg.index} for u in seg.unmatched)
            with self._seg_lock:
                self._seg = None
            self._publish(write=True)

    def _loop(self, seg: _Segment, paths: dict[str, Path]) -> None:
        closed = 0
        startup_deadline = time.monotonic() + float(self.cfg.startup_timeout_s)
        next_status = 0.0
        next_publish = time.monotonic() + STATUS_EVERY_S
        exited_at: float | None = None
        urgent_applied = False
        while True:
            try:
                kind, payload = seg.queue.get(timeout=POLL_S)
            except queue.Empty:
                kind, payload = "", None
            if kind == "msg":
                self._handle(seg, payload, paths)
            elif kind == "closed":
                closed += 1
            proc = seg.proc
            now = time.monotonic()
            if exited_at is None and proc is not None and proc.poll() is not None:
                exited_at = now
            # both readers at EOF, or the process is gone and a pipe is held open elsewhere
            if closed >= 2 or (exited_at is not None and now - exited_at > 10.0):
                if proc is not None and proc.poll() is None:
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                if seg.end_reason is None:
                    code = proc.poll() if proc is not None else None
                    seg.problems.append(f"Claude Code exited unexpectedly (code {code})")
                    seg.end_reason = "process_exit" if seg.init is not None else "startup_failed"
                break
            if seg.end_reason is None:
                if self._stop.is_set():
                    self._begin_end(seg, "operator_stop")
                elif seg.init is None and now > startup_deadline:
                    seg.problems.append("no system/init envelope within startup_timeout_s")
                    self._begin_end(seg, "startup_failed")
                elif not seg.preflight and now >= next_status:
                    next_status = now + STATUS_EVERY_S
                    self._periodic(seg, now)
            else:
                if self._urgent and not urgent_applied:
                    urgent_applied = True
                    if seg.interrupt_sent:
                        seg.interrupt_deadline = min(seg.interrupt_deadline, now + URGENT_WAIT_S)
                    if seg.kill_deadline:
                        seg.kill_deadline = min(seg.kill_deadline, now + 2 * URGENT_WAIT_S)
                if seg.interrupt_sent and seg.turn_open and now > seg.interrupt_deadline:
                    seg.notes.append("no result after the interrupt; closing stdin")
                    seg.turn_open = False
                    self._close_stdin(seg)
                if proc is not None and proc.poll() is None and seg.kill_deadline and now > seg.kill_deadline:
                    seg.problems.append("Claude Code did not exit after stdin closed; killed")
                    seg.killed = True
                    kill_tree(proc)
            if now >= next_publish:
                next_publish = now + STATUS_EVERY_S
                self._publish()

    def _periodic(self, seg: _Segment, now: float) -> None:
        if seg.image_hard_limit:
            self._begin_end(seg, "image_limit")
            return
        checkpoint = self._checkpoint_state()
        if checkpoint != "ready":
            seg.checkpoint_armed = True
        elif seg.checkpoint_armed:
            self._begin_end(seg, "checkpoint")
            return
        self._sync_markers(seg, checkpoint)
        if now - seg.last_activity > float(self.cfg.turn_timeout_s):
            seg.problems.append(f"decision lease expired: no stream message for {self.cfg.turn_timeout_s:g} s")
            self._begin_end(seg, "lease_expired")

    def _sync_markers(self, seg: _Segment, checkpoint: str) -> None:
        """Mirror the checkpoint state into the hook's fallback markers, and pick up a request the
        hook made through them when MAST was unreachable."""
        request = self.hook_dir / compact_hook.REQUEST_MARKER
        ready = self.hook_dir / compact_hook.READY_MARKER
        if checkpoint == "none" and seg.markers == "none" and request.exists():
            seg.notes.append("checkpoint requested through the hook's marker file")
            try:
                self._request_checkpoint(seg, "context_limit")
                checkpoint = "requested"
            except Exception as exc:  # noqa: BLE001
                seg.problems.append(f"request_checkpoint raised {type(exc).__name__}: {exc}")
        seg.markers = checkpoint
        try:
            if checkpoint in ("requested", "ready"):
                request.touch()
            else:
                request.unlink(missing_ok=True)
            if checkpoint == "ready":
                ready.touch()
            else:
                ready.unlink(missing_ok=True)
        except OSError:
            pass

    # ── stream handling ──────────────────────────────────────────────────────
    def _read_stdout(self, seg: _Segment, stream: Any, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as log:
            for raw in stream:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    log.write(json.dumps({"_unparsable": line[:4000]}, ensure_ascii=False) + "\n")
                    log.flush()
                    continue
                log.write(json.dumps(sanitize(message), ensure_ascii=False, separators=(",", ":")) + "\n")
                log.flush()
                if isinstance(message, dict):
                    seg.queue.put(("msg", message))
        seg.queue.put(("closed", "stdout"))

    def _read_stderr(self, seg: _Segment, stream: Any, path: Path) -> None:
        with path.open("wb") as log:
            for raw in stream:
                log.write(raw)
                log.flush()
                seg.stderr_tail = (seg.stderr_tail + raw)[-4096:]
        seg.queue.put(("closed", "stderr"))

    def _handle(self, seg: _Segment, message: dict, paths: dict[str, Path]) -> None:
        seg.last_activity = time.monotonic()
        kind = message.get("type")
        subtype = message.get("subtype")
        if kind == "system":
            if subtype == "init":
                self._on_init(seg, message)
            elif subtype == "compact_boundary":
                seg.compact_boundaries.append({k: message.get(k) for k in ("uuid", "compact_metadata")})
                seg.problems.append("native compaction happened (compact_boundary) before the "
                                    "checkpoint handoff")
                if seg.end_reason is None:
                    self._begin_end(seg, "native_compact")
            elif subtype == "status":
                if message.get("status") == "compacting":
                    seg.compaction_attempts += 1
            elif subtype == "api_retry":
                seg.api_retries.append({k: message.get(k) for k in
                                        ("attempt", "max_retries", "retry_delay_ms", "error_status", "error")})
            elif subtype == "hook_response" and message.get("hook_event") == "Stop":
                if '"block"' in str(message.get("output") or ""):
                    seg.stop_hook_blocks += 1
            elif subtype == "permission_denied":
                seg.permission_denials.append({k: v for k, v in message.items() if k not in ("uuid",)})
        elif kind == "assistant":
            body = message.get("message") if isinstance(message.get("message"), dict) else {}
            model = body.get("model")
            if isinstance(model, str) and model and not model.startswith("<"):
                seg.models.add(model)
            for block in body.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = _short(block.get("name", ""))
                    seg.tool_uses += 1
                    seg.tool_counts[name] = seg.tool_counts.get(name, 0) + 1
                    if name in self.cfg.progress_tools:
                        seg.turn_progress = True
                    if isinstance(block.get("id"), str):
                        seg.open_tools[block["id"]] = {"name": name, "turn": len(seg.results) + 1}
                elif block.get("type") == "text" and isinstance(block.get("text"), str):
                    seg.last_text = block["text"]
        elif kind == "user":
            body = message.get("message") if isinstance(message.get("message"), dict) else {}
            for block in body.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._on_tool_result(seg, block)
        elif kind == "result":
            self._on_result(seg, message, paths)
        elif kind == "control_request":
            self._write_stdin(seg, {"type": "control_response", "response": {
                "subtype": "error", "request_id": message.get("request_id"),
                "error": "control requests are not supported by the MAST runner"}})
            seg.notes.append(f"refused control_request {(message.get('request') or {}).get('subtype')!r}")
        elif kind == "rate_limit_event":
            info = message.get("rate_limit_info")
            if isinstance(info, dict):
                seg.rate_limit = info

    def _on_tool_result(self, seg: _Segment, block: dict) -> None:
        seg.tool_results += 1
        tid = block.get("tool_use_id")
        info = seg.open_tools.pop(tid, None) if isinstance(tid, str) else None
        name = info["name"] if info else None
        if name in self.cfg.act_tools:
            if block.get("is_error"):
                seg.act_errors += 1
            else:
                seg.acts += 1
        content = block.get("content")
        images = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image") \
            if isinstance(content, list) else 0
        if images:
            seg.images_results += images
            self._image_budget(seg)

    def _image_budget(self, seg: _Segment) -> None:
        if seg.preflight:
            return
        total = seg.images_sent
        if total > int(self.cfg.image_limit_per_session):
            if not seg.image_hard_limit:
                seg.image_hard_limit = True
                seg.notes.append(f"image limit exceeded ({total} images); handing off to a fresh session")
            return
        if total < int(self.cfg.image_limit_per_session) - int(self.cfg.image_reserve):
            return
        if self._stop.is_set() or self._checkpoint_state() != "none":
            return
        try:
            self._request_checkpoint(seg, "image_limit")
        except Exception as exc:  # noqa: BLE001
            seg.problems.append(f"request_checkpoint raised {type(exc).__name__}: {exc}")

    def _on_init(self, seg: _Segment, message: dict) -> None:
        seg.init_count += 1
        first = seg.init is None
        if first:
            seg.init = message
            version = message.get("claude_code_version")
            seg.claude_code_version = version if isinstance(version, str) else None
        model = message.get("model")
        if isinstance(model, str) and model:
            seg.models.add(model)
        problems = validate_init(message, seg.expected_tools, expected_model=self.cfg.expected_model)
        billing = auth_problem(message, self.cfg.auth)
        if billing:
            problems.append(billing)
        if first and message.get("session_id") not in (None, seg.session_id):
            problems.append(f"session id {message.get('session_id')!r} != requested {seg.session_id!r}")
        if problems:
            seg.problems.extend(f"init #{seg.init_count}: {p}" for p in problems)
            if seg.end_reason is None:
                self._begin_end(seg, "init_invalid")

    def _close_open_tools(self, seg: _Segment, at: str) -> None:
        for tid, info in list(seg.open_tools.items()):
            seg.unmatched.append({"id": tid, "name": info.get("name"), "turn": info.get("turn"), "at": at})
        seg.open_tools.clear()

    def _on_result(self, seg: _Segment, message: dict, paths: dict[str, Path]) -> None:
        seg.turn_open = False
        seg.last_result = message
        text = message.get("result") if isinstance(message.get("result"), str) else ""
        seg.results.append({k: message.get(k) for k in (
            "subtype", "is_error", "num_turns", "total_cost_usd", "terminal_reason", "result_index",
            "stop_reason", "api_error_status", "duration_ms")} | {"result_chars": len(text)})
        denials = message.get("permission_denials")
        if isinstance(denials, list) and denials:
            seg.permission_denials.extend(denials)
        if text:
            seg.final_text = text
        elif seg.last_text and classify_result(message) == "ok":
            seg.final_text = seg.last_text
        self._close_open_tools(seg, "turn_end")
        if seg.end_reason is not None:
            self._close_stdin(seg)
            return
        if seg.preflight:
            self._begin_end(seg, "preflight_done")
            return
        outcome, note = marker_outcome(text)
        if outcome in ("done", "abort"):              # the agent's own decision ends the run
            seg.marker = outcome
            self._begin_end(seg, "marker")
            return
        if self._stop.is_set():
            self._begin_end(seg, "operator_stop")
            return
        if note:
            seg.notes.append(f"turn {len(seg.results)}: {note}")
        if seg.image_hard_limit:
            self._begin_end(seg, "image_limit")
            return
        checkpoint = self._checkpoint_state()
        if checkpoint == "ready" and seg.checkpoint_armed:
            self._begin_end(seg, "checkpoint")
            return
        verdict = classify_result(message)
        if verdict == "budget_usd":
            seg.problems.append("Claude Code reached --max-budget-usd")
            self._begin_end(seg, "max_budget_usd")
            return
        if verdict == "usage_limit":
            seg.problems.append(f"usage limit: {usage_limit_text(message)}")
            self._begin_end(seg, "usage_limit")
            return
        if verdict == "retryable":
            seg.api_failures += 1
            if seg.api_failures > len(self.cfg.api_retry_backoff_s):
                seg.problems.append(f"API errors persisted after {seg.api_failures - 1} retries")
                self._begin_end(seg, "api_error")
                return
            delay = float(self.cfg.api_retry_backoff_s[seg.api_failures - 1])
            seg.notes.append(f"retryable API error (status {message.get('api_error_status')}); "
                             f"waiting {delay:g} s")
            self._sleep(delay)
            if self._stop.is_set():
                self._begin_end(seg, "operator_stop")
                return
            self._send_user(seg, self.cfg.continue_text, (), paths["input"])
            return
        if verdict == "error":
            seg.problems.append(f"turn ended with {message.get('subtype')!r} "
                                f"({message.get('terminal_reason')!r}): {str(message.get('errors') or text)[:300]}")
            self._begin_end(seg, "turn_error")
            return
        seg.api_failures = 0
        seg.stall = 0 if seg.turn_progress else seg.stall + 1
        if seg.stall >= int(self.cfg.stall_turns):
            seg.notes.append(f"{seg.stall} consecutive turns without a progress tool")
            self._begin_end(seg, "stalled")
            return
        follow_up = self.cfg.continue_text
        if checkpoint == "requested":
            follow_up = f"{follow_up}\n\n{CHECKPOINT_INSTRUCTION}"
        self._send_user(seg, follow_up, (), paths["input"])
        self._publish(write=True)

    # ── process I/O ──────────────────────────────────────────────────────────
    def _send_user(self, seg: _Segment, text: str, images: Sequence[Mapping[str, Any]], input_path: Path) -> None:
        message = user_message(text, images)
        seg.inputs.append(sanitize(message))
        try:
            _atomic_json(input_path, seg.inputs)
        except OSError as exc:
            seg.notes.append(f"input log not written: {type(exc).__name__}: {exc}")
        if self._write_stdin(seg, message):
            seg.turn_open = True
            seg.turn_progress = False
            seg.messages_sent += 1
            seg.images_input += len(images)
            seg.last_activity = time.monotonic()
            if images:
                self._image_budget(seg)

    def _write_stdin(self, seg: _Segment, message: dict) -> bool:
        proc = seg.proc
        if proc is None or proc.stdin is None or seg.stdin_closed:
            return False
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
            return True
        except (OSError, ValueError) as exc:
            seg.notes.append(f"stdin write failed: {type(exc).__name__}: {exc}")
            return False

    def _grace_s(self) -> float:
        return URGENT_WAIT_S if self._urgent else float(self.cfg.end_grace_s)

    def _close_stdin(self, seg: _Segment) -> None:
        if seg.stdin_closed or seg.proc is None or seg.proc.stdin is None:
            return
        seg.stdin_closed = True
        try:
            seg.proc.stdin.close()
        except (OSError, ValueError):
            pass
        seg.kill_deadline = time.monotonic() + self._grace_s()

    def _begin_end(self, seg: _Segment, reason: str) -> None:
        if seg.end_reason is not None:
            return
        seg.end_reason = reason
        proc = seg.proc
        if proc is not None and proc.poll() is None and seg.turn_open and not seg.stdin_closed:
            seg.interrupt_sent = self._write_stdin(seg, {
                "type": "control_request", "request_id": f"mast_interrupt_{uuid.uuid4().hex[:8]}",
                "request": {"subtype": "interrupt"}})
            seg.interrupt_deadline = time.monotonic() + (URGENT_WAIT_S if self._urgent else INTERRUPT_WAIT_S)
            seg.kill_deadline = seg.interrupt_deadline + self._grace_s()
            if not seg.interrupt_sent:
                self._close_stdin(seg)
        else:
            self._close_stdin(seg)

    def _shutdown(self, seg: _Segment, threads: list[threading.Thread], paths: dict[str, Path]) -> None:
        proc = seg.proc
        if proc is not None:
            if proc.poll() is None:
                self._close_stdin(seg)
                try:
                    proc.wait(timeout=max(1.0, self._grace_s()))
                except subprocess.TimeoutExpired:
                    seg.problems.append("Claude Code still running at segment shutdown; killed")
                    seg.killed = True
                    kill_tree(proc)
            for thread in threads:
                thread.join(timeout=10)
            seg.exit_code = proc.poll()
            if seg.end_reason is None:
                seg.end_reason = "process_exit"
            # drain what the readers queued after the loop ended (a late result carries usage)
            while True:
                try:
                    kind, payload = seg.queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "msg" and isinstance(payload, dict):
                    self._handle(seg, payload, paths)
        self._close_open_tools(seg, "segment_end")

    # ── records ──────────────────────────────────────────────────────────────
    def _totals(self) -> dict:
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "cost_usd": 0.0, "num_turns": 0,
                  "segments": len(self.segments), "compact_boundaries": 0, "usage_complete": True}
        for record in self.segments:
            usage = record.get("usage") or {}
            for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                totals[key] += int(usage.get(key) or 0)
            totals["cost_usd"] += float(record.get("cost_usd") or 0.0)
            totals["num_turns"] += int(record.get("num_turns") or 0)
            totals["compact_boundaries"] += int(record.get("compact_boundary_count") or 0)
            if not record.get("usage_complete"):
                totals["usage_complete"] = False
        totals["cost_usd"] = round(totals["cost_usd"], 6)
        if self.preflight_result is not None:
            totals["preflight_cost_usd"] = self.preflight_result.get("cost_usd")
        return totals

    def _publish(self, *, write: bool = False) -> None:
        """Rebuild the status view (runner thread only) and, with ``write``, ``status.json``."""
        with self._seg_lock:
            seg = self._seg
        current = seg.record() if seg is not None else None
        if current is not None:
            current["checkpoint"] = seg.markers
        with self._lock:
            view = {
                "run_id": self.run_id, "session": self.session, "client": "claude",
                "state": self.state, "outcome": self.outcome, "error": self.error,
                "stop": dict(self._stop_info) if self._stop_info else None,
                "task": self.task[:2000], "task_chars": len(self.task),
                "model": self.cfg.model, "effort": self.cfg.effort, "base_url": self.base_url,
                "created_at": self.created_at, "ended_at": self.ended_at, **self.meta,
                "segments": [compact_record(r) for r in self.segments],
                "current_segment": compact_record(current) if current else None,
                "unmatched_tool_calls": list(self.unmatched),
                "usage": self._totals(), "models": sorted(self.models | (seg.models if seg else set())),
                "claude_code_version": self.claude_code_version,
                "preflight": ({k: v for k, v in self.preflight_result.items() if k != "segment"}
                              if self.preflight_result else None),
                "final_text": self.final_text[-2000:],
                "service_problems": list(self._service_problems),
                "run_dir": str(self.run_dir),
            }
            self._view = view
        if write:
            doc = {**view, "status_version": 1, "updated_at": _now(), "final_text": self.final_text,
                   "segments": list(self.segments), "current_segment": current,
                   "preflight": self.preflight_result, "config": self.cfg.summary(),
                   "runtime": self.paths.summary()}
            try:
                _atomic_json(self.run_dir / "status.json", doc)
            except OSError:
                pass
