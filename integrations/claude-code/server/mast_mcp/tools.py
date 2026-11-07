"""The ``mast_*`` tools, one per endpoint of MAST's external agent API.

Every result is one plain-language summary line followed by compact JSON, never
longer than the tool's text limit. The VISTA tools (archive, measure, notes,
history) answer with VISTA's own reply text instead, passed through verbatim.
A result may carry images after its text (observations, magnified views,
plots); only the text is ever clipped. Anything that goes wrong comes back as a
tool error (``isError``) with an explanation, not as a protocol error, so the
agent can read it and act on it. The one exception is a refusal that brings the
newest observation (``observe_first``): it ran nothing and is answered as a normal
result, so that its image is shown (see ``gate_refusal``).

Time: every call returns within ``CALL_BUDGET_S``, except ``mast_wait``, which
waits on the server for up to ``vista_surface.WAIT_MAX_S`` (clients set their
tool timeout to 900 s for it). Claude Code moves MCP calls that run for minutes
into the background, and a job that outlives its HTTP request is exactly what
the job model is for: submit, then wait.

One act, one observation: instrument actions carry the observation token
(``after``) and the MAST server enforces the rule. This module forwards the
call's ``_meta`` as ``client_meta`` so the server can tell two acts of one
Codex code cell apart from two acts the model looked between.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from . import vista_surface as vs
from .client import MastClient, MastError

log = logging.getLogger("mast_mcp.tools")

TEXT_LIMIT = 20_000
VISTA_TEXT_LIMIT = 150_000  # VISTA replies (values, notes, history) stay below RESULT_SIZE_CHARS
RESULT_SIZE_CHARS = 200_000  # Claude Code saves a longer result to a file the model cannot read
CALL_BUDGET_S = 55.0        # hard ceiling per tool call, all HTTP included
SERVER_MAX_WAIT_S = 30      # GET /jobs/{id}?wait_s is capped at 30 by MAST
RUN_DEFAULT_WAIT_S = 20
MAX_WAIT_S = 50
DEFAULT_TIMEOUT_S = 30.0
POST_TIMEOUT_S = 20.0
ANSWER_SLACK_S = 8.0        # what a server-side wait leaves of the budget for the answer
VISTA_TIMEOUT_S = 50.0      # archive and measure tools: rendering, registration, fits
WAIT_READ_SLACK_S = 30.0    # mast_wait: HTTP read timeout = timeout_s + this
WAIT_BUDGET_S = vs.WAIT_MAX_S + WAIT_READ_SLACK_S + 15.0
DETECTOR_BUDGET_S = vs.DETECTOR_MAX_RUNTIME_S + 40.0
MAX_IMAGES = 32
META_TEXT_MAX = 200
META_KEYS_MAX = 32
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

TERMINAL_STATES = frozenset({"succeeded", "failed", "refused_busy", "cancelled",
                             "crashed", "lost_on_restart"})
FAILED_STATES = TERMINAL_STATES - {"succeeded"}

BRIEFING_SECTIONS = ("status", "scope", "resume", "tip", "instrument", "live", "prefs",
                     "safety", "recent_actions", "recent_files", "alarms", "notes",
                     "recording", "jobs", "operator_requests",
                     # optical station only (tip, instrument, live, prefs and recent_files do not exist there)
                     "station", "optical_state", "operator_prompts", "recent_spectra",
                     # both stations; empty (and left out of the text) until someone draws a layout
                     "optical_layouts")

#: Over streamable HTTP a client stops waiting for a plain JSON answer after about 60 s,
#: so a server built for it (``Toolbox(call_ceiling_s=...)``) ends every call sooner;
#: ``mast_wait`` then waits on the server for the ceiling minus this much.
SHORT_WAIT_SLACK_S = 10.0

# The part of INSTRUCTIONS rule 4 that only holds when calls may run for minutes.
_LONG_WAIT_RULE = "(set your MCP tool timeout to at least 900 s)"

INSTRUCTIONS = """\
MAST drives a real instrument station: a scanning tunneling microscope (via Nanonis) or an optical station. Rules:
1. Start with mast_briefing: it says which kind of station this is and the rules that apply to it; then mast_observe (latest frame or spectrum, instrument state, observation token). Read the briefing again after a restart, a lost_on_restart job or any surprise.
2. Set experiment and sample with mast_scope first: without a sample, scans and spectroscopy are refused; the active experiment holds the frame archive, WORKING.md and the records. Inspect the job's recorded fields.
3. One act, one observation: every instrument action (mast_act, or mast_run of an instrument skill) needs after = next_token of the latest observation, and you look at that observation (its image) before the next act. Codex Code Mode: call image() on the result; one act per code cell. A refusal (observe_first, act_in_flight, same_code_cell, checkpoint_requested) ran nothing; observe_first returns the latest observation with a fresh token.
4. Acts run as jobs. state running: mast_wait(conditions=[{"type":"job","job_id":...}]); never resubmit. mast_wait also waits for frames, alerts, operator replies, detector hits and timers (set your MCP tool timeout to at least 900 s). Plain skill jobs can also be polled with mast_job(job_id, wait_s=30).
5. Numbers come from mast_read_values and the measure tools (mast_line_profile, mast_fft_peaks, mast_detect_blobs, mast_diff, mast_stats), not from reading pixels; mast_inspect re-renders archived frames.
6. Keep GUIDE.md (durable lessons, mast_write_guide) and WORKING.md (current plan and state, mast_write_working) up to date; read both at the start of a session.
7. Find skills by the action you need (a verb or Nanonis command: "withdraw", "set bias", "scan frame", "acquire spectrum"), not by guessing names. Read mast_skill_card before a skill's first run.
8. refused_busy: someone else holds the instrument and your action did not run. Do not retry at once; check mast_status and wait.
9. lost_on_restart: MAST restarted mid-job and never replays it. Read mast_briefing before submitting again.
10. Never approve human-in-the-loop requests for the operator; ask with mast_ask_operator and keep working on what you can.
11. The operating mode (mast_status: safe/semi/auto/unknown) is the operator's choice and you never change it. SAFE refuses autonomous tip processing (bias pulses, tip shaping), including external jobs and composite substeps; if a task needs it, ask with mast_ask_operator.
12. To stop: mast_cancel acts at the job's next check; a stop or retract skill such as StopScan or WithdrawTip (via mast_run) needs no token and is never blocked by the instrument lock; mast_emergency_stop is for real danger and only the operator clears its latch.
13. A failed skill is not an API error, and degraded is not empty: read state, result and degraded before concluding.
14. Finish with the instrument idle (no job running, scan stopped), WORKING.md saved and a mast_handover.
15. Optical station (the briefing says so): there is no STM; run skills with mast_run (mast_act's scan and withdraw actions do not exist there) and read spectrum numbers (peaks, max counts, saturation) from latest_frame / new_spectra, not from the STM measure tools. WITec write access, manual optics (λ/2 plates, analyzer, objective) and switching a laser on need the operator: ask with mast_ask_operator and mast_wait for the reply; you never answer or attest for them. Read mast://guide/en/09-optical-station.md.
Guide: mast://guide/en/README.md, mast://guide/zh/README.md."""


class ArgError(Exception):
    """The tool was called with arguments it cannot use."""


# ── registry ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    annotations: dict
    handler: object
    budget_s: float = CALL_BUDGET_S
    text_limit: int = TEXT_LIMIT
    large: bool = False         # images or long VISTA text: ask the client not to offload it

    def as_mcp(self) -> dict:
        out = {"name": self.name, "title": self.title, "description": self.description,
               "inputSchema": self.input_schema,
               "annotations": {"title": self.title, **self.annotations}}
        if self.large:
            out["_meta"] = {"anthropic/maxResultSizeChars": RESULT_SIZE_CHARS}
        return out


TOOLS: dict[str, Tool] = {}


def _tool(name: str, *, title: str, description: str, properties: dict | None = None,
          required: tuple = (), schema: dict | None = None, read_only: bool = False,
          destructive: bool = False, idempotent: bool = False, open_world: bool = False,
          large: bool = False, budget_s: float = CALL_BUDGET_S):
    """Register a tool. ``schema`` (a whole input schema, as VISTA writes them) replaces
    ``properties``/``required``; ``large`` raises the text limit to ``VISTA_TEXT_LIMIT``."""
    if schema is None:
        schema = {"type": "object", "properties": properties or {}}
        if required:
            schema["required"] = list(required)
    annotations = {"readOnlyHint": read_only, "destructiveHint": destructive,
                   "idempotentHint": idempotent, "openWorldHint": open_world}

    def register(fn):
        TOOLS[name] = Tool(name, title, description, schema, annotations, fn, budget_s=budget_s,
                           text_limit=VISTA_TEXT_LIMIT if large else TEXT_LIMIT, large=large)
        return fn
    return register


@dataclass
class ToolOutput:
    """What a tool call returns: the text, then images (``{label, mime, data_b64}``).

    Handlers may still return ``(text, is_error)``; ``as_output`` turns that into this.
    """
    text: str
    is_error: bool = False
    images: list = field(default_factory=list)


def as_output(result) -> ToolOutput:
    if isinstance(result, ToolOutput):
        return result
    text, is_error = result
    return ToolOutput(text, bool(is_error))


def _str_p(description: str, **extra) -> dict:
    return {"type": "string", "description": description, **extra}


def _int_p(description: str, **extra) -> dict:
    return {"type": "integer", "description": description, **extra}


def _num_p(description: str, **extra) -> dict:
    return {"type": "number", "description": description, **extra}


def _list_p(description: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": description}


# ── call context ─────────────────────────────────────────────────────────────
def _meta_text(value) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:META_TEXT_MAX]


def _meta_value(value):
    """A scalar as it may travel in ``client_meta``; anything else as clipped JSON."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None        # NaN and inf are not JSON
    if isinstance(value, str):
        return value[:META_TEXT_MAX]
    return compact(value)[:META_TEXT_MAX]


def client_meta(meta: dict | None, client_info: dict | None) -> dict:
    """What MAST is told about the calling client (contract §9), empty values dropped.

    ``item_id`` is Codex's ``itemId``: every call made by one Code Mode code cell
    carries the same one, which is how the server refuses a second act from it.
    """
    meta = meta if isinstance(meta, dict) else {}
    turn = meta.get("x-codex-turn-metadata")
    if isinstance(turn, dict):
        turn = {str(k)[:64]: _meta_value(v) for k, v in list(turn.items())[:META_KEYS_MAX]}
        turn = {k: v for k, v in turn.items() if v is not None and v != ""}
    else:
        turn = _meta_text(turn)
    client = None
    if isinstance(client_info, dict):
        client = {k: client_info[k][:100] for k in ("name", "title", "version")
                  if isinstance(client_info.get(k), str) and client_info[k].strip()}
    out = {"item_id": _meta_text(meta.get("itemId")), "call_id": _meta_text(meta.get("callId")),
           "turn": turn, "tool_use_id": _meta_text(meta.get("claudecode/toolUseId")),
           "client": client}
    return {k: v for k, v in out.items() if v}


class Session:
    """What one MCP connection remembers between calls: the last ``mast_wait`` cursor."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cursor: str | None = None

    @property
    def wait_cursor(self) -> str | None:
        with self._lock:
            return self._cursor

    def remember_cursor(self, cursor) -> None:
        if isinstance(cursor, str) and cursor:
            with self._lock:
                self._cursor = cursor


class CallContext:
    """One tool call: the client, its stop signal, its time budget and who is calling."""

    def __init__(self, client, config, signal, *, clock=time.monotonic, sleeper=None,
                 budget: float = CALL_BUDGET_S, meta: dict | None = None,
                 client_info: dict | None = None, session: Session | None = None,
                 max_wait_s: float = vs.WAIT_MAX_S):
        self.client = client
        self.config = config
        self.signal = signal
        self.clock = clock
        self._sleeper = sleeper or signal.wait
        self.deadline = clock() + budget
        self.meta = meta if isinstance(meta, dict) else {}
        self.client_info = client_info
        self.session = session if session is not None else Session()
        self.max_wait_s = max_wait_s     # longest server-side wait of mast_wait

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def timeout(self, preferred: float = DEFAULT_TIMEOUT_S) -> float:
        left = self.remaining() - 1.0
        if left < 1.0:
            raise MastError("budget", "This tool call ran out of its time budget before MAST "
                            "answered. If a job was involved, look it up with mast_jobs.")
        return min(preferred, left)

    def client_meta(self) -> dict:
        return client_meta(self.meta, self.client_info)

    def cancelled(self) -> bool:
        """The client withdrew this call: its answer will never be read."""
        flag = getattr(self.signal, "cancelled", None)
        return bool(flag is not None and flag.is_set())

    def get(self, path: str, query: dict | None = None, *, timeout: float = DEFAULT_TIMEOUT_S):
        return self.client.call("GET", path, query=query, timeout=self.timeout(timeout))

    def post(self, path: str, body: dict, *, timeout: float = DEFAULT_TIMEOUT_S):
        return self.client.call("POST", path, body=body, timeout=self.timeout(timeout))

    def put(self, path: str, body: dict, *, timeout: float = DEFAULT_TIMEOUT_S):
        return self.client.call("PUT", path, body=body, timeout=self.timeout(timeout))

    def delete(self, path: str, *, timeout: float = DEFAULT_TIMEOUT_S):
        # MAST refuses a write request without a JSON body (its guard against browsers).
        return self.client.call("DELETE", path, body={}, timeout=self.timeout(timeout))

    def sleep(self, seconds: float) -> bool:
        """Sleep; True when the call should stop instead."""
        return bool(self._sleeper(seconds))


def short_wait_note(max_wait_s: int) -> str:
    return (f"Over this connection one call waits at most {max_wait_s} s: when it times out, call "
            "mast_wait again; it continues from the cursor, so nothing is missed. ")


class Toolbox:
    """What the transports (``stdio_rpc``, ``http_rpc``) call: list the tools, run one.

    ``call_ceiling_s`` is for a client that cannot wait minutes for an answer (MCP over
    HTTP): no call then runs longer, and ``mast_wait`` with its description, its schema
    and the server instructions say how long it waits instead of asking for a 900 s
    client timeout. Without it (stdio) nothing changes.
    """

    def __init__(self, config, client=None, *, call_ceiling_s: float | None = None):
        self.config = config
        self.client = client if client is not None else MastClient(config)
        self.session = Session()
        self.call_ceiling_s = call_ceiling_s
        self.max_wait_s = (vs.WAIT_MAX_S if call_ceiling_s is None
                           else max(1, int(call_ceiling_s - SHORT_WAIT_SLACK_S)))

    def instructions(self) -> str:
        if self.call_ceiling_s is None:
            return INSTRUCTIONS
        return INSTRUCTIONS.replace(
            _LONG_WAIT_RULE, f"(over this connection one call waits at most {self.max_wait_s} s; "
                             "call it again to keep waiting)")

    def list_tools(self) -> list[dict]:
        tools = [t.as_mcp() for t in TOOLS.values()]
        if self.call_ceiling_s is None:
            return tools
        return [self._short_wait(t) if t["name"] == "mast_wait" else t for t in tools]

    def _short_wait(self, tool: dict) -> dict:
        tool = json.loads(json.dumps(tool))          # a deep copy; TOOLS stays as it is
        n = self.max_wait_s
        tool["description"] = (tool["description"]
                               .replace(vs.WAIT_LIMITS_TEXT, f"(default {n}, max {n})")
                               .replace(vs.WAIT_CLIENT_TIMEOUT_NOTE, short_wait_note(n)))
        tool["inputSchema"]["properties"]["timeout_s"].update(
            maximum=n, default=n, description=f"Longest wait in seconds (1-{n}).")
        return tool

    def has_tool(self, name: str) -> bool:
        return name in TOOLS

    def call_tool(self, name: str, arguments: dict, signal, *, meta: dict | None = None,
                  client_info: dict | None = None, clock=time.monotonic,
                  sleeper=None) -> ToolOutput:
        tool = TOOLS[name]
        if self.config.problem:
            return ToolOutput(clip(f"{name} did not run. {self.config.problem}"), True)
        budget = tool.budget_s
        if self.call_ceiling_s is not None:
            budget = min(budget, self.call_ceiling_s)
        ctx = CallContext(self.client, self.config, signal, clock=clock, sleeper=sleeper,
                          budget=budget, meta=meta, client_info=client_info,
                          session=self.session, max_wait_s=self.max_wait_s)
        started = time.monotonic()
        try:
            out = as_output(tool.handler(ctx, dict(arguments or {})))
        except ArgError as exc:
            out = ToolOutput(f"{name}: invalid arguments: {exc}", True)
        except MastError as exc:
            out = error_output(exc)
        except Exception as exc:  # noqa: BLE001 - reported to the agent, logged in full
            log.exception("tool %s failed", name)
            out = ToolOutput(f"{name} hit an internal error in the MAST MCP server: {exc!r}", True)
        log.info("%s -> %s in %.1f s%s", name, "error" if out.is_error else "ok",
                 time.monotonic() - started,
                 f", {len(out.images)} image(s)" if out.images else "")
        return ToolOutput(clip(out.text, tool.text_limit), bool(out.is_error), out.images)


# ── rendering ────────────────────────────────────────────────────────────────
def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def clip(text: str, limit: int = TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = limit - 160
    return (text[:cut] + f"\n...[truncated {len(text) - cut} characters; ask for less: "
            "fewer sections, a smaller limit or a narrower query]")


def render(summary: str, payload=None, *, body: str | None = None) -> str:
    parts = [summary.strip()]
    if body:
        parts.append(body.rstrip())
    if payload is not None:
        parts.append(compact(payload))
    return "\n".join(parts)


def _one_line(value, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _secs(value) -> str:
    try:
        return f"{float(value):.0f} s"
    except (TypeError, ValueError):
        return f"{value} s"


def _size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} kB"
    return f"{n} bytes"


def _rows(data, *keys: str) -> list:
    """The list inside a listing answer, whichever key it came under."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _ok_false(data) -> bool:
    return isinstance(data, dict) and data.get("ok") is False


def _problems(data) -> str:
    probs = data.get("problems") if isinstance(data, dict) else None
    if isinstance(probs, list) and probs:
        return "; ".join(_one_line(p, 200) for p in probs[:5])
    return _one_line(data.get("detail") or data.get("error") or "", 300) if isinstance(data, dict) else ""


# ── images, observations, VISTA replies ──────────────────────────────────────
def images_of(raw) -> tuple[list[dict], int]:
    """Usable images of an answer (``[{label, mime, data_b64}]``) and how many were left out.

    The base64 is passed on untouched: never decoded, re-encoded or clipped.
    """
    out: list[dict] = []
    dropped = 0
    for item in raw if isinstance(raw, list) else ():
        data = item.get("data_b64") if isinstance(item, dict) else None
        mime = str(item.get("mime") or "image/png").lower() if isinstance(item, dict) else ""
        if not isinstance(data, str) or not data or mime not in IMAGE_MIMES:
            dropped += 1
            continue
        label = item.get("label")
        out.append({"label": label[:2000] if isinstance(label, str) else "", "mime": mime,
                    "data_b64": data})
    if len(out) > MAX_IMAGES:
        dropped += len(out) - MAX_IMAGES
        out = out[:MAX_IMAGES]
    return out, dropped


def _with_images(text: str, raw_images, *, is_error: bool) -> ToolOutput:
    images, dropped = images_of(raw_images)
    if dropped:
        text += (f"\n({dropped} image(s) of this answer could not be shown: no data, an "
                 f"unsupported format, or more than {MAX_IMAGES}.)")
    return ToolOutput(text, is_error, images)


def _without_image_data(obj):
    """``obj`` with every base64 image replaced by its size: error text never carries pixels."""
    if isinstance(obj, dict):
        return {k: (f"<{len(v)} base64 characters>" if k == "data_b64" and isinstance(v, str)
                    else _without_image_data(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_without_image_data(v) for v in obj]
    return obj


def _is_tool_reply(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("text"), str) and (
        "is_error" in data or "images" in data)


def reply_output(data, what: str) -> ToolOutput:
    """A VISTA ToolReply (``{text, images, is_error}``): its text verbatim, then its images.

    Anything else is shown as compact JSON under ``what``.
    """
    if _is_tool_reply(data):
        return _with_images(data["text"], data.get("images"), is_error=bool(data.get("is_error")))
    return ToolOutput(render(what, _without_image_data(data)), False)


def observation_parts(obs) -> tuple[str, list]:
    """The text an observation gives the model (it carries ``next_token``) and its images."""
    if not isinstance(obs, dict):
        return "", []
    text = obs.get("text")
    if not isinstance(text, str) or not text.strip():
        text = compact({k: v for k, v in obs.items() if k != "images"})
    return text, obs.get("images") or []


def _token_note(obs) -> str:
    if isinstance(obs, dict) and obs.get("token_on_image"):
        return (" Strict mode: the next token is printed on the image, not in the text; read it "
                "there and pass it as after.")
    return ""


#: Seen in Claude Code (an ``observe_first`` refusal then still marked isError):
#: an error result reaches the model with its text but without its images.
ERROR_IMAGE_NOTE = (" Some clients (Claude Code among them) do not show the images of an error "
                    "result: if you see no image here, call mast_observe for the current "
                    "observation.")


def observation_output(summary: str, obs, *, is_error: bool = False,
                       marker: dict | None = None) -> ToolOutput:
    """An observation after ``summary``; ``marker`` is one machine-readable JSON line between."""
    text, raw_images = observation_parts(obs)
    if raw_images:
        summary += (" The observation image follows: look at it before the next act (Codex Code "
                    "Mode: image()).")
        if is_error:
            summary += ERROR_IMAGE_NOTE
    summary += _token_note(obs)
    lines = [summary]
    if marker:
        lines.append(compact(marker))
    if text:
        lines.append(text)
    return _with_images("\n".join(lines), raw_images, is_error=is_error)


def _job_wait_call(job_id) -> str:
    return f'mast_wait(conditions=[{{"type": "job", "job_id": "{job_id}"}}])'


#: The observation gate's refusals (contract §2), with what to do when MAST gives no detail.
#: None of them executed anything.
GATE_REFUSALS = {
    "observe_first": (
        "Your token was missing, already used, or replaced by a newer observation (frames from "
        "the operator, the user interface or another job replace it). The latest observation "
        "follows with a fresh token: look at it, then act with after = its next_token."),
    "act_in_flight": (
        "Another act is still running. Wait for it with mast_wait and look at its observation "
        "before the next act."),
    "same_code_cell": (
        "This code cell already ran an act. Look at that act's result first (Codex Code Mode: "
        "image() on it), then act from a new code cell."),
    "checkpoint_requested": (
        "MAST asked for a checkpoint: save your plan and state with mast_write_working; until "
        "then only the notes tools run."),
    "session_closed": (
        "The operator or the runner closed this session: finish with a short summary and do not "
        "act again."),
}


def gate_refusal(exc: MastError) -> ToolOutput | None:
    """A 409 of the observation gate as the model should read it, with its observation.

    MAST's own explanation is shown when it gives one; the bridge's words stand in when not.
    A refusal that brings the newest observation (``observe_first``) is not an error result:
    looking at that observation is the way back, and Claude Code does not show the images
    of an error result (in strict mode the token is printed on the image only). Its text
    still opens with ``Refused (...)``, and a JSON line ``{"refused": ..., "executed": false}``
    follows. Refusals without an observation stay errors.
    """
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    code = str(payload.get("error") or exc.kind)
    if exc.status != 409 or code not in GATE_REFUSALS:
        return None
    detail = _one_line(payload.get("detail") or "", 600)
    summary = f"Refused ({code}); nothing was executed" + (
        f": {detail}" if detail else f". {GATE_REFUSALS[code]}")
    if not summary.endswith((".", "。")):
        summary += "."
    if payload.get("job_id"):
        summary += (f" The act still running is job {payload['job_id']}: "
                    f"{_job_wait_call(payload['job_id'])}.")
    extra = {k: v for k, v in payload.items()
             if k not in ("error", "detail", "observation", "job_id")}
    if extra:
        summary += " " + compact(_without_image_data(extra))
    obs = payload.get("observation")
    if isinstance(obs, dict):
        return observation_output(summary, obs, is_error=False,
                                  marker={"refused": code, "executed": False})
    return ToolOutput(summary, True)


def error_output(exc: MastError) -> ToolOutput:
    """A failed request: a gate refusal with its observation, a VISTA error reply, or the
    explanation the client wrote."""
    refusal = gate_refusal(exc)
    if refusal is not None:
        return refusal
    if _is_tool_reply(exc.payload):
        return _with_images(exc.payload["text"], exc.payload.get("images"), is_error=True)
    return ToolOutput(render(exc.message, _without_image_data(exc.as_dict())), True)


# ── argument helpers ─────────────────────────────────────────────────────────
def _seg(value) -> str:
    return urllib.parse.quote(str(value), safe="")


def _str(args: dict, key: str, *, required: bool = False) -> str | None:
    value = args.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)                      # ids handed over as numbers
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ArgError(f"'{key}' is required")
        return None
    if not isinstance(value, str):
        raise ArgError(f"'{key}' must be a string")
    return value.strip()


def _text(args: dict, key: str, *, required: bool = False) -> str | None:
    """Like ``_str`` but keeps inner formatting (notes, code, messages)."""
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ArgError(f"'{key}' is required")
        return None
    if not isinstance(value, str):
        raise ArgError(f"'{key}' must be a string")
    return value


def _num(args: dict, key: str, *, default: float, lo: float, hi: float) -> float:
    value = args.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ArgError(f"'{key}' must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ArgError(f"'{key}' must be a number") from None
    if number != number:  # NaN
        raise ArgError(f"'{key}' must be a number")
    return max(lo, min(hi, number))


def _int(args: dict, key: str, *, default: int, lo: int, hi: int) -> int:
    return int(_num(args, key, default=default, lo=lo, hi=hi))


def _bool(args: dict, key: str, default: bool = False) -> bool:
    value = args.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise ArgError(f"'{key}' must be true or false")


def _obj(args: dict, key: str) -> dict | None:
    value = args.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ArgError(f"'{key}' must be a JSON object") from None
    if not isinstance(value, dict):
        raise ArgError(f"'{key}' must be an object")
    return value


def _names(args: dict, key: str) -> list[str] | None:
    """A list of short names; a comma-separated string or JSON array works too."""
    value = args.get(key)
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                value = text
        if isinstance(value, str):
            value = value.split(",")
    if not isinstance(value, list):
        raise ArgError(f"'{key}' must be a list of strings")
    out = [str(v).strip() for v in value if str(v).strip()]
    return out or None


def _lines(args: dict, key: str) -> list[str] | None:
    """A list of sentences; a string becomes one item per non-empty line."""
    value = args.get(key)
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        value = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", line) for line in value.splitlines()]
    if not isinstance(value, list):
        raise ArgError(f"'{key}' must be a list of strings")
    out = [str(v).strip() for v in value if str(v).strip()]
    return out or None


def _choice(args: dict, key: str, allowed: tuple, default: str | None = None) -> str | None:
    value = _str(args, key)
    if value is None:
        return default
    value = value.lower()
    if value not in allowed:
        raise ArgError(f"'{key}' must be one of {', '.join(allowed)}")
    return value


def _spec(args: dict):
    value = args.get("spec")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ArgError("'spec' is required (pass '?' to get the syntax)")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text in ("?", "？", "help"):
            return "?"
        try:
            parsed = json.loads(text)
        except ValueError:
            return text                         # MAST reports exactly what is wrong
        return parsed if isinstance(parsed, dict) else text
    raise ArgError("'spec' must be a JSON object (or '?' for the syntax)")


# ── jobs ─────────────────────────────────────────────────────────────────────
def _terminal(view) -> bool:
    return isinstance(view, dict) and (view.get("terminal") is True
                                       or view.get("state") in TERMINAL_STATES)


def _wait_for_job(ctx: CallContext, job_id: str, wait_s: float, view: dict) -> dict:
    """Long-poll ``GET /jobs/{id}?wait_s`` until the job ends or ``wait_s`` is spent.

    MAST caps one wait at 30 s, so a longer wait takes two requests. Everything
    stays inside the call's budget, with room left to answer.
    """
    end = min(ctx.clock() + wait_s, ctx.deadline - 3.0)
    path = f"/jobs/{_seg(job_id)}"
    while not _terminal(view):
        if ctx.signal.should_stop():
            break
        left = end - ctx.clock()
        if left < 1.0:
            break
        wait = int(min(SERVER_MAX_WAIT_S, left))
        asked_at = ctx.clock()
        view = ctx.get(path, {"wait_s": wait}, timeout=wait + 8)
        # A MAST that ignores wait_s answers at once; do not hammer it.
        if not _terminal(view) and ctx.clock() - asked_at < 0.5:
            if ctx.sleep(min(1.0, max(0.0, end - ctx.clock()))):
                break
    return view


#: What to do when MAST refuses a submission outright (no job was created).
_SUBMIT_REFUSALS = {
    "request_id_conflict": (
        "That request_id was already used for a different skill or different params. Reuse "
        "an id only to resubmit the same action after a timeout; a new action needs a new id."),
    "skill_disabled": (
        "The skill, or one of its declared steps, is switched off on this machine (hardware "
        "module absent or capability not granted). Choose another skill or ask the operator; "
        "wrapping it in a composite does not switch it on."),
    "use_act": (
        "This connection works with observation tokens: run instrument skills with "
        "mast_run(after=...) or use mast_act."),
}


def _with_submit_hint(exc: MastError) -> MastError:
    hint = _SUBMIT_REFUSALS.get(exc.kind)
    if not hint:
        return exc
    return MastError(exc.kind, f"{exc.message} {hint}", status=exc.status, payload=exc.payload,
                     transient=exc.transient)


def _new_request_id(ctx: CallContext) -> str:
    return f"mcp-{ctx.config.session}-{secrets.token_hex(6)}"


def _token(args: dict) -> str | None:
    """The observation token; case and spaces do not matter (it may be read off an image)."""
    value = _str(args, "after")
    return re.sub(r"\s+", "", value).upper() if value else None


def _server_wait(ctx: CallContext, wait_s: float) -> int:
    """Whole seconds MAST may hold the answer and still leave this call time to deliver it."""
    return int(max(0.0, min(wait_s, ctx.remaining() - ANSWER_SLACK_S)))


def _submit(ctx: CallContext, path: str, body: dict) -> tuple:
    """POST a submission; ``(answer, retried)``.

    The request may or may not have reached MAST when it fails in transit. The
    request_id makes a second attempt safe: MAST answers with the original job (or
    act) instead of running it twice.
    """
    timeout = float(body.get("wait_s") or 0) + POST_TIMEOUT_S
    try:
        return ctx.post(path, body, timeout=timeout), False
    except MastError as exc:
        if not exc.transient or ctx.signal.should_stop() or ctx.remaining() < 15:
            raise
        log.warning("POST %s failed (%s); retrying once with request_id %s", path, exc.kind,
                    body["request_id"])
    return ctx.post(path, body, timeout=timeout), True


def _route_missing(exc: MastError) -> bool:
    """MAST has no such endpoint: an older MAST (6.5) without the 7.0 routes."""
    return exc.status == 404 and exc.kind in ("not_found", "no_ext_api")


#: What an act's job state says beyond ok=false (the same ends as a job's, see job_summary).
_ACT_STATE_NOTES = {
    "refused_busy": ("someone else holds the instrument and the action did not run; do not retry "
                     "at once, check mast_status"),
    "cancelled": "the act was cancelled",
    "crashed": ("the act crashed inside MAST and its outcome is unknown; read mast_briefing and "
                "tell the operator"),
    "lost_on_restart": ("MAST restarted during the act; it was not replayed and its outcome is "
                        "unknown"),
}


def act_output(answer, what: str, *, retried: bool = False) -> ToolOutput:
    """An ``/vista/act`` answer of kind ``act``: done (with its observation) or running."""
    if not isinstance(answer, dict):
        return ToolOutput(render("MAST returned something that is not an act.", answer), True)
    job_id = answer.get("job_id") or "?"
    head = f"Act {what} (job {job_id}"
    if answer.get("turn") is not None:
        head += f", turn {answer['turn']}"
    head += ")"
    notes = []
    job_state = answer.get("job_state")
    if job_state in _ACT_STATE_NOTES:
        notes.append(f"{job_state}: {_ACT_STATE_NOTES[job_state]}")
    if answer.get("idempotent_replay") and retried:
        notes.append("MAST's first answer was lost in transit; resending the same request_id "
                     "returned this same act, so nothing ran twice")
    elif answer.get("idempotent_replay"):
        notes.append("this request_id was used before, so this is the ORIGINAL act; nothing new "
                     "was run")
    tail = "".join(f" ({n})" for n in notes)
    state = str(answer.get("state") or "unknown")
    if state == "running":
        summary = (f"{head}: still running{tail}. It has not finished: call "
                   f"{_job_wait_call(job_id)} to get its observation, and do not act meanwhile.")
        return ToolOutput(render(summary, _without_image_data(answer)), False)
    obs = answer.get("observation")
    failed = answer.get("ok") is False
    if state == "done" and isinstance(obs, dict):
        summary = f"{head}: done{tail}" + (", FAILED (see error)." if failed else ".")
        return observation_output(summary, obs, is_error=failed)
    if state == "done":
        summary = (f"{head}: done{tail}" + (", FAILED" if failed else "") + ", but MAST recorded "
                   "no observation for it: call mast_observe before the next act.")
        return ToolOutput(render(summary, _without_image_data(answer)), failed)
    return ToolOutput(render(f"{head}: {state}{tail}", _without_image_data(answer)), True)


def _refusal_hint(code: str) -> str:
    c = code.lower()
    if "human" in c:
        return ("the composite contains a step that needs a person; ask the operator to run "
                "it in MAST")
    if "sample" in c:
        return "the skill needs an active sample; set one with mast_scope(sample_name=...)"
    if "si" in c.split("_") or "param" in c or "parse" in c:
        return ("a parameter value could not be read; check units and si_params in the skill "
                "card, and write values such as '5n' where a prefix is required")
    if "mode" in c or "safe" in c:
        return "the current operating mode does not allow this action; ask the operator"
    if "disabled" in c:
        return "this capability is switched off on this machine"
    if "wired" in c:
        return ("MAST could not set up the execution (a subsystem is not wired, often while "
                "starting); check mast_status and tell the operator if it persists")
    return ""


def job_summary(view: dict, *, cancel_request: bool = False, replayed: bool | None = None,
                retried: bool = False) -> tuple[str, bool]:
    """Summary line for a job view, and whether it counts as a failure.

    ``replayed`` carries the submit answer's ``idempotent_replay`` past the
    polls (a poll's view does not repeat it); ``retried`` marks a resubmission
    this server made itself after MAST's first answer was lost.
    """
    state = str(view.get("state") or "unknown")
    job_id = view.get("job_id", "?")
    head = f"Job {job_id} ({view.get('skill', '?')}): {state}"
    elapsed = view.get("elapsed_s")
    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
        head += f" after {elapsed:.1f} s"
    result = view.get("result") if isinstance(view.get("result"), dict) else {}
    notes: list[str] = []
    if replayed is None:
        replayed = bool(view.get("idempotent_replay"))
    if replayed and retried:
        notes.append("MAST's first answer was lost in transit; resubmitting with the same "
                     "request_id returned this same job, so nothing ran twice")
    elif replayed:
        notes.append("this request_id was used before, so this is the ORIGINAL job; "
                     "nothing new was run")
    if state == "succeeded":
        if result.get("summary"):
            notes.append(_one_line(result["summary"]))
    elif state in ("queued", "running"):
        notes.append(f'still {state}: keep waiting with mast_job(job_id="{job_id}", '
                     "wait_s=30); do not submit it again")
        waiting = view.get("waiting_operator")
        if isinstance(waiting, dict) and waiting.get("prompt_id"):
            notes.append(f"waiting for the operator (prompt {waiting['prompt_id']}"
                         + (f": {_one_line(waiting['title'], 120)}" if waiting.get("title") else "")
                         + "): only the operator can answer it, in MAST's web UI or the launcher "
                           "popup. The job continues when they do, or fails when the prompt times "
                           "out; cancel the job to withdraw it")
    elif state == "refused_busy":
        holder = view.get("busy_holder")
        if isinstance(holder, dict):
            who = holder.get("owner") or "another user"
            if holder.get("skill"):
                who += f" running {holder['skill']}"
            if holder.get("held_s") is not None:
                who += f" for {_secs(holder['held_s'])}"
        else:
            who = str(holder or "another user")
        notes.append(f"NOT executed and not queued: the instrument is held by {who}. Do not "
                     "retry at once; check mast_status and submit again after that work ends")
    elif state == "failed":
        refused_by = view.get("refused_by")
        if refused_by:
            hint = _refusal_hint(str(refused_by))
            notes.append(f"refused by {refused_by}" + (f" ({hint})" if hint else ""))
        err = result.get("error") or (result.get("summary") if not refused_by else "")
        if err:
            notes.append(_one_line(err, 400))
    elif state == "cancelled":
        reason = view.get("cancel_reason")
        notes.append("cancelled" + (f": {_one_line(reason, 200)}" if reason else ""))
    elif state == "crashed":
        notes.append("the job crashed inside MAST and its outcome is unknown; read "
                     "mast_briefing (alarms, recent actions) before anything else and tell "
                     "the operator")
    elif state == "lost_on_restart":
        notes.append("MAST restarted while this job was in flight; it was NOT replayed and its "
                     "outcome is unknown. Read mast_briefing before deciding to submit it again")
    abort = view.get("abort")
    if isinstance(abort, dict) and abort.get("set"):
        reason = abort.get("reason") or abort.get("why")
        notes.append("the abort flag is set" + (f" ({_one_line(reason, 200)})" if reason else "")
                     + "; write actions are refused until the operator clears it")
    recorded = view.get("recorded")
    if _terminal(view) and isinstance(recorded, dict) and recorded.get("v1") is False:
        notes.append("Legacy experiment record NOT recorded (recorded.v1=false); check "
                     "mast_scope and the other recorded fields for this job")
    summary = head + (". " + "; ".join(notes) if notes else "")
    return summary, (state in FAILED_STATES and not cancel_request)


def _job_result(view, **kw) -> tuple[str, bool]:
    if not isinstance(view, dict):
        return render("MAST returned something that is not a job view.", view), True
    summary, is_error = job_summary(view, **kw)
    return render(summary, view), is_error


# ── tools ────────────────────────────────────────────────────────────────────
@_tool("mast_briefing", title="MAST briefing", read_only=True, idempotent=True, description=(
    "Read this first in every session, and again after any restart or surprise. One call "
    "returns MAST's current state as sectioned text: operating mode and abort flag, instrument "
    "lock, experiment/sample scope and whether actions are being recorded, resume notes, tip "
    "state, instrument profile and learned calibrations, live readings, the operator's "
    "parameter preferences, safety envelope, recent actions and files, alarms, notes, your jobs "
    "and your operator requests. On an optical station (no STM) the tip, instrument, live and "
    "preference sections give way to the station profile with its rules and configured devices, "
    "the operator-signed optical state, the operator prompts still waiting, and the newest "
    "spectra; a section the station does not have is refused with the list of those it has. "
    "No hardware I/O, and no device is opened for it. Keywords: 简报, 状态, 续工, 针尖, 仪器档案, 光学站."),
    properties={"sections": _list_p(
        "Optional subset of sections: " + ", ".join(BRIEFING_SECTIONS) + ". Omit for all.")})
def _briefing(ctx: CallContext, args: dict):
    data = ctx.get("/briefing", {"sections": _names(args, "sections")})
    if not isinstance(data, dict):
        return render("MAST briefing", data), False
    sections = data.get("sections")
    degraded = [str(d.get("section") or d) if isinstance(d, dict) else str(d)
                for d in (data.get("degraded") or [])]
    body = data.get("text") or ""
    rest = {k: v for k, v in data.items() if k != "text"}
    if isinstance(sections, dict):
        if not body:
            body = "\n\n".join(f"## {name}\n{sec.get('text')}" for name, sec in sections.items()
                               if isinstance(sec, dict) and sec.get("text"))
        rest["sections"] = {name: ({k: v for k, v in sec.items() if k != "text"}
                                   if isinstance(sec, dict) else sec)
                            for name, sec in sections.items()}
        count = sum(1 for sec in sections.values() if isinstance(sec, dict) and sec.get("ok"))
        summary = f"MAST briefing: {count} of {len(sections)} sections ok"
    else:
        summary = "MAST briefing"
    if degraded:
        summary += f"; degraded: {', '.join(degraded)}"
    return render(summary, rest, body=body), False


def status_summary(d: dict) -> str:
    parts = [f"mode {d.get('mode') or 'unknown'}"]
    abort = d.get("abort") if isinstance(d.get("abort"), dict) else {}
    if "abort" in d and d.get("abort") is None:
        parts.append("abort state UNREADABLE (do not assume it is clear)")
    elif abort.get("set") or abort.get("emergency"):
        why = abort.get("why") or abort.get("reason")
        parts.append("ABORT SET" + (" (emergency)" if abort.get("emergency") else "")
                     + (f": {_one_line(why, 200)}" if why else "")
                     + "; write actions are refused until the operator clears it")
    else:
        parts.append("abort clear")
    lock = d.get("lock") if isinstance(d.get("lock"), dict) else {}
    if lock.get("held"):
        text = f"instrument held by {lock.get('owner') or 'someone'}"
        if lock.get("skill"):
            text += f" running {lock['skill']}"
        if lock.get("held_s") is not None:
            text += f" for {_secs(lock['held_s'])}"
        parts.append(text)
    elif "lock" in d:
        parts.append("instrument free")
    jobs = d.get("jobs") if isinstance(d.get("jobs"), dict) else {}
    if jobs:
        parts.append(f"external jobs running {jobs.get('running', '?')}/{jobs.get('max', '?')}")
    degraded = d.get("degraded")
    if degraded:
        parts.append("degraded: " + ", ".join(str(x) for x in degraded))
    return "MAST status: " + "; ".join(parts)


@_tool("mast_status", title="MAST status", read_only=True, idempotent=True, description=(
    "Quick state check, cheap and without hardware I/O: operating mode (SAFE/SEMI/AUTO), abort "
    "flag and why, who holds the instrument lock and for how long, connection, active scope, "
    "running jobs against the limit, degraded subsystems. Use it before submitting work and "
    "after refused_busy. Keywords: 状态, 锁, 急停, 模式."))
def _status(ctx: CallContext, args: dict):
    data = ctx.get("/status")
    summary = status_summary(data) if isinstance(data, dict) else "MAST status"
    return render(summary, data), False


@_tool("mast_find_skills", title="Find MAST skills", read_only=True, idempotent=True,
       description=(
    "Search MAST's skills (instrument actions and analyses). Search by the ACTION you need, a "
    "verb or Nanonis command such as 'withdraw', 'set bias', 'z controller', 'scan frame', "
    "'bias spectroscopy', not by guessing a name: many skills are named after the problem they "
    "solve. Each hit shows origin (official or not), safety level, footprint (pure-analysis / "
    "hardware-read-only / hardware-write) and why it matched. Prefer official skills. "
    "It also finds MAST's instrument-agent tools (origin 仪器 agent 工具, footprint mast-read / "
    "mast-write): experiment and sample lists and switching, the scan map and the next scan "
    "position (get_map_analysis, get_next_scan_position), the coarse map, the current tip "
    "(get_current_tip), plans, the knowledge base, documents and conduct status. They read or "
    "write MAST's own records, never the instrument, and run with mast_run like any skill. "
    "Registering a tip and backfilling a manual coarse move are the operator's to do in MAST's "
    "UI (ask with mast_ask_operator): they clear safety interlocks. "
    "Keywords: 技能, 查找, 动作."),
    properties={"query": _str_p("What you want to do, as a verb or command."),
                "limit": _int_p("Maximum hits (1-100).", default=20, minimum=1, maximum=100)},
    required=("query",))
def _find_skills(ctx: CallContext, args: dict):
    query = _str(args, "query", required=True)
    data = ctx.get("/skills/search", {"q": query,
                                      "limit": _int(args, "limit", default=20, lo=1, hi=100)})
    hits = _rows(data, "results")
    names = [str(h.get("name")) for h in hits if isinstance(h, dict) and h.get("name")]
    count = data.get("count", len(hits)) if isinstance(data, dict) else len(hits)
    if names:
        shown = ", ".join(names[:10]) + (", ..." if len(names) > 10 else "")
        summary = f'{count} skill(s) match "{query}": {shown}. Read one with mast_skill_card.'
    else:
        summary = (f'No skill matches "{query}". Try another verb, a Nanonis command name, or a '
                   "broader word.")
    return render(summary, data), False


@_tool("mast_skill_card", title="MAST skill card", read_only=True, idempotent=True,
       description=(
    "Full card for one skill: parameters with type, unit, required, default, min/max and "
    "choices (and which accept SI strings like '5n'), preconditions, safety level, footprint, "
    "the Nanonis commands it sends, sub-skills, whether it takes the instrument lock or needs "
    "an active sample, whether it is switched off on this machine, and how long it usually "
    "takes. Read it before a skill's first run. For one of MAST's instrument-agent tools the "
    "card has the same shape (its family, and footprint mast-read or mast-write); a tool that "
    "would hand work to MAST's internal agents is refused here with the reason "
    "(not_offered). Keywords: 技能卡, 参数, 单位."),
    properties={"name": _str_p("Exact skill name, as returned by mast_find_skills.")},
    required=("name",))
def _skill_card(ctx: CallContext, args: dict):
    name = _str(args, "name", required=True)
    card = ctx.get(f"/skills/{_seg(name)}")
    if not isinstance(card, dict):
        return render(f"Skill {name}", card), False
    bits = [f"Skill {card.get('name', name)}"]
    for key in ("safety_level", "footprint"):
        if card.get(key):
            bits.append(f"{key} {card[key]}")
    if card.get("takes_instrument_token") is True:
        bits.append("takes the instrument lock")
    if card.get("requires_sample"):
        bits.append("needs an active sample (mast_scope)")
    face = card.get("tool_face")
    if isinstance(face, dict) and (face.get("disabled") or face.get("closed")):
        bits.append("SWITCHED OFF on this machine"
                    + (f" ({_one_line(face.get('reason'), 160)})" if face.get("reason") else ""))
    elif isinstance(face, dict) and "disabled" in face and face["disabled"] is None:
        bits.append("whether it is switched off on this machine is UNKNOWN")
    duration = card.get("duration") if isinstance(card.get("duration"), dict) else {}
    measured = duration.get("measured") if isinstance(duration.get("measured"), dict) else None
    if measured and measured.get("p50_s") is not None:
        bits.append(f"typically {_secs(measured['p50_s'])} (p95 {_secs(measured.get('p95_s'))}, "
                    f"n={measured.get('n', '?')})")
    elif duration.get("estimated_s") is not None:
        bits.append(f"estimated {_secs(duration['estimated_s'])}")
    return render("; ".join(bits), card), False


@_tool("mast_run", title="Run a MAST skill", destructive=True, open_world=True, large=True,
       description=(
    "Run a skill on MAST as a job: submit it, then wait up to wait_s seconds (default 20, max "
    "50) for it to end. If it is still running when this returns, keep polling with "
    "mast_job(job_id, wait_s=30) or mast_wait; never submit it again. End states: succeeded, "
    "failed, refused_busy (someone else holds the instrument; NOT executed, do not retry at "
    "once), cancelled, crashed, lost_on_restart (MAST restarted; never replayed). The answer "
    "also says the recorded fields for each recording path; experiment-linked records require "
    "an active experiment (see mast_scope). "
    "A skill that drives the instrument passes the same one-act-one-observation gate as "
    "mast_act: give after = next_token of the latest observation; the answer is then that act's "
    "observation (with its image and a fresh token) or state running (mast_wait for it). Reads, "
    "analyses and stop or retract skills (StopScan, WithdrawTip) need no token, and neither do "
    "MAST's instrument-agent tools (get_map_analysis, list_experiments, get_current_tip ...): they "
    "run as plain jobs on MAST's own records and never drive the instrument. "
    "Safety gates, the operating mode (SAFE refuses tip shaping and pulses) and the abort flag "
    "all apply. Interrupting this call does not cancel the job. "
    "Keywords: 执行, 运行, 作业, 扫描, 偏压, 进针, 退针."),
    properties={
        "skill": _str_p("Exact skill name (from mast_find_skills)."),
        "params": {"type": "object", "additionalProperties": True, "description": (
            "Skill parameters as a JSON object. Units are SI (V, A, m, s); parameters the "
            "skill card lists under si_params also accept strings such as '5n' or '200p' (the "
            "card says where the prefix is required).")},
        "after": _str_p(
            "Observation token: next_token of the latest observation you have looked at. "
            "Needed for skills that drive the instrument."),
        "request_id": _str_p(
            "Idempotency key: submitting again with the same request_id returns the original "
            "job instead of running the action twice. Use one whenever you might retry after "
            "a timeout. Generated automatically when omitted."),
        "wait_s": _num_p("Seconds to wait for the job to end (0-50).", default=20, minimum=0,
                         maximum=MAX_WAIT_S),
        "note": _str_p("Optional note stored with the job: why you ran it."),
    },
    required=("skill",))
def _run(ctx: CallContext, args: dict):
    skill = _str(args, "skill", required=True)
    params = _obj(args, "params") or {}
    request_id = _str(args, "request_id") or _new_request_id(ctx)
    note = _text(args, "note")
    after = _token(args)
    wait_s = _num(args, "wait_s", default=RUN_DEFAULT_WAIT_S, lo=0, hi=MAX_WAIT_S)
    wait_end = ctx.clock() + wait_s
    body: dict = {"skill": skill, "skill_params": params, "request_id": request_id,
                  "wait_s": _server_wait(ctx, wait_s), "client_meta": ctx.client_meta()}
    if after:
        body["after"] = after
    if note:
        body["note"] = note
    try:
        answer, retried = _submit(ctx, "/vista/act", body)
    except MastError as exc:
        if _route_missing(exc):
            log.info("MAST has no /vista/act (older than 7.0); submitting %s to /jobs", skill)
            return _run_as_job(ctx, skill, params, request_id, note, wait_s)
        raise _with_submit_hint(exc) from None
    if isinstance(answer, dict) and answer.get("kind") == "act":
        return act_output(answer, skill, retried=retried)
    view = answer.get("job") if isinstance(answer, dict) and answer.get("kind") == "job" else answer
    replayed = isinstance(answer, dict) and bool(answer.get("idempotent_replay")) or (
        isinstance(view, dict) and bool(view.get("idempotent_replay")))
    if isinstance(view, dict) and view.get("job_id") and not _terminal(view):
        left = wait_end - ctx.clock()
        if left >= 1:
            view = _wait_for_job(ctx, str(view["job_id"]), left, view)
    return _job_result(view, replayed=replayed, retried=retried)


def _run_as_job(ctx: CallContext, skill: str, params: dict, request_id: str, note: str | None,
                wait_s: float):
    """``mast_run`` on a MAST without ``/vista/act``: ``POST /jobs``, then poll."""
    body: dict = {"skill": skill, "params": params, "request_id": request_id}
    if note:
        body["note"] = note
    try:
        view, retried = _submit(ctx, "/jobs", body)
    except MastError as exc:
        raise _with_submit_hint(exc) from None
    replayed = isinstance(view, dict) and bool(view.get("idempotent_replay"))
    if isinstance(view, dict) and view.get("job_id") and not _terminal(view) and wait_s >= 1:
        view = _wait_for_job(ctx, str(view["job_id"]), wait_s, view)
    return _job_result(view, replayed=replayed, retried=retried)


@_tool("mast_job", title="MAST job status", read_only=True, idempotent=True, description=(
    "Look at one job from mast_run. With wait_s > 0 (max 50) it blocks until the job ends or "
    "the time is up: use that instead of polling in a tight loop. Interrupting this call does "
    "not cancel the job; mast_cancel does. Keywords: 作业, 轮询, 结果."),
    properties={"job_id": _str_p("The job_id from mast_run."),
                "wait_s": _num_p("Seconds to wait for the job to end (0-50).", default=0,
                                 minimum=0, maximum=MAX_WAIT_S)},
    required=("job_id",))
def _job(ctx: CallContext, args: dict):
    job_id = _str(args, "job_id", required=True)
    wait_s = _num(args, "wait_s", default=0, lo=0, hi=MAX_WAIT_S)
    view: dict = {}
    if wait_s >= 1:
        view = _wait_for_job(ctx, job_id, wait_s, view)
    if not view:
        view = ctx.get(f"/jobs/{_seg(job_id)}")
    return _job_result(view)


@_tool("mast_jobs", title="List MAST jobs", read_only=True, idempotent=True, description=(
    "List jobs submitted under this actor name (all=true: every actor's), newest first, "
    "optionally filtered by state: queued, running, succeeded, failed, refused_busy, "
    "cancelled, crashed, lost_on_restart. Keywords: 作业列表."),
    properties={"all": {"type": "boolean", "default": False,
                        "description": "Include other actors' jobs."},
                "state": _str_p("Only jobs in this state."),
                "limit": _int_p("Maximum jobs (1-200).", default=50, minimum=1, maximum=200)})
def _jobs(ctx: CallContext, args: dict):
    data = ctx.get("/jobs", {"all": _bool(args, "all"), "state": _str(args, "state"),
                             "limit": _int(args, "limit", default=50, lo=1, hi=200)})
    jobs = [j for j in _rows(data, "jobs", "items", "results") if isinstance(j, dict)]
    counts: dict[str, int] = {}
    for j in jobs:
        counts[str(j.get("state"))] = counts.get(str(j.get("state")), 0) + 1
    summary = f"{len(jobs)} job(s)"
    if counts:
        summary += ": " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    return render(summary, data), False


@_tool("mast_cancel", title="Cancel a MAST job", idempotent=True, description=(
    "Ask a running job to stop. Cancellation is cooperative: it takes effect between skills, "
    "between sub-steps and before each write command, not inside one blocking Nanonis command. "
    "To request a stop, run a skill such as StopScan with mast_run; stop skills bypass the "
    "instrument ownership lock. Check the result and instrument feedback; for real danger "
    "use mast_emergency_stop. "
    "Keywords: 取消, 停止作业."),
    properties={"job_id": _str_p("The job to cancel."),
                "reason": _str_p("Why; stored with the job.")},
    required=("job_id",))
def _cancel(ctx: CallContext, args: dict):
    job_id = _str(args, "job_id", required=True)
    reason = _text(args, "reason")
    view = ctx.post(f"/jobs/{_seg(job_id)}/cancel", {"reason": reason} if reason else {})
    if not isinstance(view, dict):
        return render("Cancel requested.", view), False
    summary, _ = job_summary(view, cancel_request=True)
    if not _terminal(view):
        summary = ("Cancel requested; it takes effect at the next step boundary. Check with "
                   "mast_job(wait_s=30). " + summary)
    return render(summary, view), False


@_tool("mast_emergency_stop", title="MAST emergency stop", destructive=True, idempotent=True,
       open_world=True, description=(
    "EMERGENCY STOP: the same action as MAST's E-STOP (stops motion and scanning, raises the "
    "abort flag) plus cancelling every external job. Only for real danger: crash risk, runaway "
    "current, anything that cannot be stopped otherwise. Afterwards write actions stay refused "
    "until the operator clears the abort flag; tell the operator what happened "
    "(mast_ask_operator). Keywords: 急停, 紧急停止, E-STOP."),
    properties={"reason": _str_p("What is going wrong; shown to the operator.")},
    required=("reason",))
def _estop(ctx: CallContext, args: dict):
    data = ctx.post("/estop", {"reason": _text(args, "reason", required=True)})
    cancelled = data.get("cancelled_jobs") if isinstance(data, dict) else None
    summary = "EMERGENCY STOP sent to MAST"
    if isinstance(cancelled, list):
        summary += f"; {len(cancelled)} external job(s) cancelled"
    summary += (". Write actions stay refused while the abort flag is set, and only the "
                "operator clears it: tell them what happened (mast_ask_operator) and check "
                "mast_status.")
    return render(summary, data), _ok_false(data)


def scope_summary(d: dict) -> str:
    scope = d.get("scope") if isinstance(d.get("scope"), dict) and "experiment" not in d else d
    exp, smp = scope.get("experiment"), scope.get("sample")
    rec = scope.get("recording") if isinstance(scope.get("recording"), dict) else {}
    if isinstance(exp, dict):
        e = f'experiment "{exp.get("name", "?")}" (id {exp.get("id", "?")})'
    else:
        e = "no active experiment"
    if isinstance(smp, dict):
        kind = f", {smp['sample_type']}" if smp.get("sample_type") else ""
        s = f'sample "{smp.get("name", "?")}" (id {smp.get("id", "?")}{kind})'
    else:
        s = "no active sample"
    out = f"Scope: {e}; {s}"
    if rec:
        out += (f"; recording v1 {'on' if rec.get('v1') else 'off'}, "
                f"v2 {'on' if rec.get('v2') else 'off'}")
        if rec.get("note"):
            out += f" ({_one_line(rec['note'], 200)})"
    changed = d.get("changed")
    if isinstance(changed, list) and changed:
        out += f"; changed: {', '.join(map(str, changed))}"
    warnings = d.get("warnings")
    if isinstance(warnings, list) and warnings:
        out += f"; warnings: {'; '.join(_one_line(w, 200) for w in warnings)}"
    if not isinstance(exp, dict):
        out += (". Actions are NOT recorded until an experiment is active: "
                "mast_scope(experiment_name=..., goal=..., sample_name=...)")
    return out


@_tool("mast_scope", title="MAST experiment and sample", idempotent=True, description=(
    "Show or set the active experiment and sample. Without arguments: the current scope and "
    "whether actions are being recorded. With arguments: switch to an existing experiment or "
    "sample by id, or open one by name (an open one with the same name is reused; a new sample "
    "needs an experiment, and both can be given in one call). Do this before running actions; "
    "an active experiment is needed for experiment-linked recording. Check each job's "
    "recorded fields. While the instrument is busy MAST "
    "refuses the switch unless force=true. Keywords: 实验, 样品, 记录."),
    properties={
        "experiment_id": _str_p("Switch to this existing experiment."),
        "experiment_name": _str_p("Open (or reuse) an experiment with this name."),
        "goal": _str_p("Goal of the experiment; only with experiment_name."),
        "sample_id": _str_p("Switch to this existing sample."),
        "sample_name": _str_p("Open (or reuse) a sample with this name."),
        "sample_type": _str_p("Kind of sample; only with sample_name."),
        "sample_description": _str_p("Free description; only with sample_name."),
        "force": {"type": "boolean", "default": False, "description": (
            "Switch even though the instrument is busy. The running action is then filed "
            "under the NEW scope; use it only when that is what you want.")},
        "reason": _str_p("Why the scope changes; stored with the switch."),
    })
def _scope(ctx: CallContext, args: dict):
    exp_id, exp_name = _str(args, "experiment_id"), _str(args, "experiment_name")
    goal = _text(args, "goal")
    s_id, s_name = _str(args, "sample_id"), _str(args, "sample_name")
    s_type, s_desc = _str(args, "sample_type"), _text(args, "sample_description")
    force, reason = _bool(args, "force"), _text(args, "reason")
    if exp_id and exp_name:
        raise ArgError("give experiment_id (switch to an existing experiment) or "
                       "experiment_name (open one by name), not both")
    if goal and not exp_name:
        raise ArgError("goal only goes with experiment_name")
    if s_id and s_name:
        raise ArgError("give sample_id or sample_name, not both")
    if (s_type or s_desc) and not s_name:
        raise ArgError("sample_type and sample_description only go with sample_name")
    body: dict = {}
    if exp_id:
        body["experiment"] = {"id": exp_id}
    elif exp_name:
        body["experiment"] = {"name": exp_name, **({"goal": goal} if goal else {})}
    if s_id:
        body["sample"] = {"id": s_id}
    elif s_name:
        body["sample"] = {"name": s_name,
                          **({"sample_type": s_type} if s_type else {}),
                          **({"description": s_desc} if s_desc else {})}
    if not body:
        if force or reason:
            raise ArgError("force and reason only go with a switch (an experiment or a sample)")
        data = ctx.get("/scope")
    else:
        if force:
            body["force"] = True
        if reason:
            body["reason"] = reason
        try:
            data = ctx.post("/scope", body)
        except MastError as exc:
            if exc.kind == "instrument_busy":
                raise MastError(exc.kind, exc.message + (
                    " Switching now would file the running action under the new scope: wait "
                    "until the instrument is free (mast_status), or pass force=true if that "
                    "is intended."), status=exc.status, payload=exc.payload) from None
            raise
    summary = scope_summary(data) if isinstance(data, dict) else "Scope"
    return render(summary, data), _ok_false(data)


@_tool("mast_list_data", title="List MAST data files", read_only=True, idempotent=True,
       description=(
    "List the most recent data files MAST can serve (.sxm images, .dat spectra and so on), "
    "newest first. Pass a returned path to mast_fetch. A file the active experiment's VISTA "
    "archive holds carries its frame id (frame), for mast_inspect and the measure tools. To find "
    "files by size, bias, setpoint, experiment, sample or date, use mast_search_data. "
    "Keywords: 数据, 文件, 图像, 谱."),
    properties={"n": _int_p("How many files (1-200).", default=20, minimum=1, maximum=200),
                "ext": _list_p("Only these extensions, e.g. [\"sxm\", \"dat\"]."),
                "offset": _int_p("Skip this many of the newest files (paging).", default=0,
                                 minimum=0)})
def _list_data(ctx: CallContext, args: dict):
    ext = _names(args, "ext")
    if ext:
        ext = [e.lstrip(".").lower() for e in ext]
    data = ctx.get("/data/files", {"n": _int(args, "n", default=20, lo=1, hi=200), "ext": ext,
                                   "offset": _int(args, "offset", default=0, lo=0, hi=10 ** 6)})
    files = [f for f in _rows(data, "files", "items", "results") if isinstance(f, dict)]
    names = [re.split(r"[\\/]", str(f.get("path") or f.get("name") or ""))[-1] for f in files]
    summary = f"{len(files)} data file(s)"
    if names:
        summary += ": " + ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
        summary += ". Download one with mast_fetch(path=...)"
    return render(summary, data), False


def _opt_num(args: dict, key: str) -> float | None:
    """An optional number: ``None`` when not given, otherwise a finite float."""
    value = args.get(key)
    if value is None or value == "":
        return None
    return _num(args, key, default=0.0, lo=-1e12, hi=1e12)


_SEARCH_NUMBERS = ("size_min_nm", "size_max_nm", "bias_min_v", "bias_max_v",
                   "setpoint_min_pa", "setpoint_max_pa")


@_tool("mast_search_data", title="Search MAST data records", read_only=True, idempotent=True,
       description=(
    "Find data files by their metadata, without changing the instrument: MAST's data gallery index "
    "(frame size, pixels, bias, setpoint, channels, a spectrum's sweep range, the operator's rating, "
    "tags and note), plus the newest files the index does not have yet, read live (up to 200). All "
    "filters are optional and combine: ext (sxm frames, dat spectra, 3ds grid spectra); experiment and "
    "sample (an id, a prefix of at least 6 hex characters, or part of the name); since and until (start "
    "time: YYYY-MM-DD or YYYY-MM-DD HH:MM[:SS] in the instrument PC's local time; a date alone in until "
    "includes that whole day); size_min_nm and size_max_nm (frame or grid width); bias_min_v and "
    "bias_max_v; setpoint_min_pa and setpoint_max_pa; text (part of the file name, path, operator note "
    "or a tag); tag; min_rating (2 key, 1 usable, -1 excluded). Newest first (order='oldest' reverses); "
    "page with n and offset (total and has_more). Each hit gives a path for mast_fetch, its experiment "
    "and sample (null when MAST cannot tell), its frame id when the active experiment's VISTA archive "
    "holds it (then mast_inspect and the measure tools take it), fetchable (false: outside the folders "
    "MAST serves), and time (start) and saved in local time. index says when the gallery index was "
    "written and which folders it covers; live says how many files were read live and whether that hit "
    "the limit (older files outside the index are then not searched). "
    "Keywords: 检索数据, 数据记录, 图库, 按偏压找图, 找谱."),
    properties={
        "ext": _list_p("Only these kinds: sxm (frames), dat (spectra), 3ds (grid spectra)."),
        "experiment": _str_p("An experiment id (or a prefix of at least 6 hex characters) or part "
                             "of its name."),
        "sample": _str_p("A sample id (or prefix) or part of its name."),
        "since": _str_p("Start time not before this: YYYY-MM-DD or YYYY-MM-DD HH:MM[:SS] (local "
                        "time of the instrument PC), ISO with a time zone, or epoch seconds."),
        "until": _str_p("Start time before this; a date alone includes that whole day."),
        "size_min_nm": _num_p("Frame (or grid) width at least this, nm.", minimum=0),
        "size_max_nm": _num_p("Frame (or grid) width at most this, nm.", minimum=0),
        "bias_min_v": _num_p("Bias at least this, V."),
        "bias_max_v": _num_p("Bias at most this, V."),
        "setpoint_min_pa": _num_p("Setpoint at least this, pA."),
        "setpoint_max_pa": _num_p("Setpoint at most this, pA."),
        "text": _str_p("Part of the file name, path, operator note or a tag (any case)."),
        "tag": _str_p("Only files the operator gave this tag."),
        "min_rating": _int_p("Operator rating at least this: 2 key, 1 usable, -1 excluded.",
                             minimum=-1, maximum=2),
        "order": _str_p("newest (default) or oldest, by start time.", enum=["newest", "oldest"]),
        "n": _int_p("How many hits (1-200).", default=50, minimum=1, maximum=200),
        "offset": _int_p("Skip this many hits (paging).", default=0, minimum=0),
        "live": {"type": "boolean", "default": True, "description": (
            "Also read the newest files the index does not have (up to 200). false: the index only, "
            "faster.")},
    })
def _search_data(ctx: CallContext, args: dict):
    ext = _names(args, "ext")
    if ext:
        ext = [e.lstrip(".").lower() for e in ext]
    query: dict = {"ext": ext, "n": _int(args, "n", default=50, lo=1, hi=200),
                   "offset": _int(args, "offset", default=0, lo=0, hi=10 ** 6),
                   "order": _choice(args, "order", ("newest", "oldest"), default="newest"),
                   "live": _bool(args, "live", default=True)}
    for key in ("experiment", "sample", "since", "until", "text", "tag"):
        value = _str(args, key)
        if value:
            query[key] = value
    for key in _SEARCH_NUMBERS:
        value = _opt_num(args, key)
        if value is not None:
            query[key] = value
    if args.get("min_rating") not in (None, ""):
        query["min_rating"] = _int(args, "min_rating", default=0, lo=-1, hi=2)
    data = ctx.get("/data/search", {k: v for k, v in query.items() if v is not None})
    hits = [h for h in _rows(data, "items") if isinstance(h, dict)]
    total = data.get("total", len(hits)) if isinstance(data, dict) else len(hits)
    summary = f"{len(hits)} of {total} data record(s)"
    names = [str(h.get("name") or "") + (f" ({h['frame']})" if h.get("frame") else "") for h in hits]
    if names:
        summary += ": " + ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
        summary += ". Download one with mast_fetch(path=...)"
    index = data.get("index") if isinstance(data, dict) else None
    if isinstance(index, dict) and not index.get("available"):
        summary += ". The data gallery has no index yet: only files read live were searched"
    live = data.get("live") if isinstance(data, dict) else None
    if isinstance(live, dict) and live.get("truncated"):
        summary += ". The live read stopped at its limit: older files outside the index were not searched"
    return render(summary, data), False


_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                     *(f"lpt{i}" for i in range(1, 10))}


def _safe_part(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text).strip("._")


def local_fetch_path(fetch_dir: str, remote: str, mode: str, channel: str | None) -> str:
    """Where a fetched file lands: ``<stem>.<hash6>[.<channel>].<ext>`` in ``fetch_dir``.

    The name is built here and never taken from MAST or the caller as a path,
    so nothing can land outside ``fetch_dir``. The short hash of the remote path
    keeps two ``Topo001.sxm`` from different folders apart; fetching the same
    file again overwrites the same local copy.
    """
    base = re.split(r"[\\/]", remote.strip())[-1]
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        stem, suffix = base, ""
    stem = _safe_part(stem)[:80] or "data"
    if stem.lower() in _WINDOWS_RESERVED:
        stem = "_" + stem
    tag = hashlib.sha1(remote.encode("utf-8")).hexdigest()[:6]
    if mode == "frame":
        name = f"{stem}.{tag}.{_safe_part(channel or 'Z')[:32] or 'Z'}.npz"
    else:
        suffix = _safe_part(suffix)[:12]
        name = f"{stem}.{tag}" + (f".{suffix}" if suffix else "")
    return os.path.join(fetch_dir, name)


def _ensure_fetch_dir(config) -> None:
    existed = os.path.isdir(config.fetch_dir)
    try:
        os.makedirs(config.fetch_dir, exist_ok=True)
    except OSError as exc:
        raise MastError("local_io", f"Cannot create the fetch directory {config.fetch_dir}: "
                        f"{exc}. Set MAST_FETCH_DIR to a writable folder.") from None
    if config.fetch_dir_is_default and not existed:
        # Keep downloaded instrument data out of the project's git history.
        try:
            with open(os.path.join(config.fetch_dir, ".gitignore"), "w", encoding="utf-8") as fh:
                fh.write("# Created by the MAST MCP server: fetched data stays out of git.\n*\n")
        except OSError:
            pass


def _frame_meta(header: str | None):
    if not header:
        return None
    try:
        header = header.encode("latin-1").decode("utf-8")   # http.client decodes as latin-1
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    for decode in (lambda h: h, lambda h: base64.b64decode(h, validate=True).decode("utf-8"),
                   urllib.parse.unquote):
        try:
            return json.loads(decode(header))
        except (ValueError, UnicodeDecodeError):
            continue
    return header


@_tool("mast_fetch", title="Download MAST data", description=(
    "Download one data file to this machine and return its local path (the content is not put "
    "into the conversation). mode='raw' saves the original bytes; mode='frame' saves an .npz "
    "with 'forward' and 'backward' arrays for one channel, orientation already normalized by "
    "MAST, plus 'meta_json'. Files go to MAST_FETCH_DIR (default .mast-fetch/ in the project). "
    "Keywords: 下载, 原始数据, 帧, npz."),
    properties={"path": _str_p("A path exactly as returned by mast_list_data."),
                "mode": _str_p("raw (original file) or frame (npz arrays).", enum=["raw", "frame"],
                               default="raw"),
                "channel": _str_p("Channel for mode=frame, e.g. Z or Current.", default="Z")},
    required=("path",))
def _fetch(ctx: CallContext, args: dict):
    remote = _str(args, "path", required=True)
    mode = _choice(args, "mode", ("raw", "frame"), default="raw")
    channel = (_str(args, "channel") or "Z") if mode == "frame" else None
    dest = local_fetch_path(ctx.config.fetch_dir, remote, mode, channel)
    _ensure_fetch_dir(ctx.config)
    endpoint = "/data/file" if mode == "raw" else "/data/frame"
    query = {"path": remote, "channel": channel}
    info = ctx.client.download(endpoint, query, dest, timeout=ctx.timeout(DEFAULT_TIMEOUT_S),
                               should_stop=ctx.signal.should_stop, deadline=ctx.deadline - 1.0,
                               clock=ctx.clock)
    payload: dict = {"saved_to": dest, "bytes": info["bytes"], "sha256": info["sha256"],
                     "content_type": info["content_type"], "remote_path": remote, "mode": mode}
    remote_name = re.split(r"[\\/]", remote)[-1]
    if mode == "frame":
        payload["channel"] = channel
        payload["meta"] = _frame_meta(info["headers"].get("X-MAST-Frame-Meta"))
        summary = (f"Saved channel {channel} of {remote_name} ({_size(info['bytes'])}) to {dest}. "
                   "numpy.load(path) gives 'forward', 'backward' (when the channel has one) and "
                   "'meta_json'; row 0 is the top edge and backward is already un-mirrored.")
    else:
        summary = f"Saved {remote_name} ({_size(info['bytes'])}) to {dest}."
    return render(summary, payload), False


@_tool("mast_note_write", title="Write a MAST note", description=(
    "Write a note into MAST's memory. MAST's own agents recall these on their own, so record "
    "findings, calibrations and caveats that others should know. scope='experiment' (default) "
    "ties the note to the active experiment; 'global' keeps it across experiments. "
    "Keywords: 笔记, 记忆, 记录发现."),
    properties={"title": _str_p("Short title."),
                "content": _str_p("The note itself (markdown is fine)."),
                "kind": _str_p("note (default), insight, summary, hypothesis or protocol."),
                "tags": _list_p("Optional tags."),
                "scope": _str_p("experiment (default) or global.", enum=["experiment", "global"])},
    required=("title", "content"))
def _note_write(ctx: CallContext, args: dict):
    title = _str(args, "title", required=True)
    body: dict = {"title": title, "content": _text(args, "content", required=True)}
    kind = _str(args, "kind")
    tags = _names(args, "tags")
    scope = _choice(args, "scope", ("experiment", "global"))
    if kind:
        body["kind"] = kind
    if tags:
        body["tags"] = tags
    if scope:
        body["scope"] = scope
    data = ctx.post("/notes", body)
    if _ok_false(data):
        return render(f"Note not saved: {_problems(data)}", data), True
    ref, warnings = None, []
    if isinstance(data, dict):
        ref = data.get("id") or data.get("path")
        warnings = [str(w) for w in (data.get("warnings") or [])]
    summary = (f'Note "{title}" saved' + (f" ({ref})" if ref else "")
               + (f", namespace {data['namespace']}" if isinstance(data, dict)
                  and data.get("namespace") else "")
               + ". MAST's agents will recall it.")
    if warnings:
        summary += " Warnings: " + "; ".join(_one_line(w, 200) for w in warnings)
    return render(summary, data), False


@_tool("mast_note_search", title="Search MAST notes", read_only=True, idempotent=True,
       description=(
    "Search MAST's notes (meaning and substring). An empty query lists the most recent ones. "
    "Each hit says how it was found (match: semantic, substring or recent) and, for a semantic "
    "hit, its cosine similarity (score, -1..1): the nearest notes always come back, so a low "
    "score marks an unrelated one. Keywords: 笔记, 检索, 回忆."),
    properties={"query": _str_p("What to look for; empty for the most recent notes."),
                "scope": _str_p("both (default), experiment or global.",
                                enum=["both", "experiment", "global"]),
                "limit": _int_p("Maximum notes (1-50).", default=10, minimum=1, maximum=50)})
def _note_search(ctx: CallContext, args: dict):
    query = _str(args, "query")
    data = ctx.get("/notes", {"q": query,
                              "scope": _choice(args, "scope", ("both", "experiment", "global"),
                                               default="both"),
                              "limit": _int(args, "limit", default=10, lo=1, hi=50)})
    notes = _rows(data, "notes", "results", "items")
    summary = f"{len(notes)} note(s)" + (f' for "{query}"' if query else ", most recent first")
    return render(summary, data), False


#: kinds of a structured operator prompt (MAST's operator_hitl.prompts.PROMPT_KINDS)
PROMPT_KINDS = ("confirm", "choice", "number", "text", "attest")
PROMPT_SEVERITIES = ("info", "action", "safety")


def _prompt_options(args: dict) -> list[dict] | None:
    """``options`` as ``[{id, label}]``: a JSON list of objects, a list of strings (each is its
    own id and label), or a JSON text of either."""
    value = args.get("options")
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ArgError("'options' must be a list of {id, label} objects") from None
    if not isinstance(value, list):
        raise ArgError("'options' must be a list of {id, label} objects")
    out = []
    for i, item in enumerate(value):
        if isinstance(item, str) and item.strip():
            out.append({"id": item.strip(), "label": item.strip()})
        elif isinstance(item, dict) and str(item.get("id") or "").strip():
            ident = str(item["id"]).strip()
            out.append({"id": ident, "label": str(item.get("label") or ident).strip()})
        else:
            raise ArgError(f"options[{i}] needs an id and a label: " + '{"id": ..., "label": ...}')
    return out


def _optional_num(args: dict, key: str) -> float | None:
    if args.get(key) is None or args.get(key) == "":
        return None
    return _num(args, key, default=0.0, lo=-1e300, hi=1e300)


@_tool("mast_ask_operator", title="Ask the MAST operator", description=(
    "Send a question or request to the human operator; it appears in MAST's request list (a "
    "structured prompt also as a card with a popup and a sound). It does not block: keep working "
    "on what you can and read the answer later with mast_operator_reply, or wait for it with "
    'mast_wait(conditions=[{"type": "operator_reply", "request_id": ...}]). Use it for anything '
    "that needs a person: approvals, sample changes, a waveplate or objective swap, actions the "
    "current mode refuses. Never approve human-in-the-loop requests yourself; you cannot answer "
    "one either, only the operator can. Free text: kind question (default), action or info; the "
    "operator replies with a note. Structured prompts exist on the optical station only; on an "
    "STM station every request is a text request: the kind is kept as you wrote it, the "
    "structured fields are ignored and the operator replies in words. Structured: kind confirm "
    "(yes/no), choice (pick one of "
    "options), number (a value between number_min and number_max, with unit), text, or attest "
    "(the operator signs off a manual optical setup, e.g. which waveplate is installed: name the "
    "slots in attest_slots; the answer sets those slots; what the operator is shown to sign comes "
    "from the slot definitions: a yes/no slot such as laser_area_safe takes no options, other "
    "slots only their own options). The answer comes back with choice / value / answered_by / ok; "
    "an attestation the operator recorded as false (value false, ok false) is an answer, not an "
    "approval. Keywords: 问操作员, 请求, 人工, 确认, 签署."),
    properties={"message": _str_p("The question or request, self-contained."),
                "kind": _str_p("question (default), action or info for free text; or a structured "
                               "prompt: confirm, choice, number, text, attest."),
                "title": _str_p("Structured: a short heading (default: the first line of message)."),
                "options": {"type": "array",
                            "items": {"type": "object",
                                      "properties": {"id": {"type": "string",
                                                            "description": "returned as the answer's choice"},
                                                     "label": {"type": "string",
                                                               "description": "what the operator reads"}},
                                      "required": ["id"]},
                            "description": "choice, and attest on a choice or number slot: the "
                                           "answers to pick from, each {id, label}; a choice needs two "
                                           "or more. An attest's options must be the slot's own and "
                                           "are shown under the slot's labels; a yes/no slot takes "
                                           "none."},
                "number_min": _num_p("number: the lowest value the operator may give."),
                "number_max": _num_p("number: the highest value the operator may give."),
                "unit": _str_p("number: the unit shown next to the value, e.g. deg."),
                "attest_slots": _list_p("attest: ids of the optical-state slots the operator's "
                                        'answer sets, e.g. ["excitation_waveplate"].'),
                "severity": _str_p("info, action (default) or safety: how loudly MAST alerts the "
                                   "operator.", enum=list(PROMPT_SEVERITIES)),
                "timeout_s": _num_p("Structured: seconds until an unanswered prompt expires "
                                    "(at most 7 days; default: 24 hours).")},
    required=("message",))
def _ask_operator(ctx: CallContext, args: dict):
    body: dict = {"message": _text(args, "message", required=True)}
    kind = _str(args, "kind")
    if kind:
        body["kind"] = kind
    structured = bool(kind) and kind.lower() in PROMPT_KINDS
    title = _str(args, "title")
    if title:
        body["title"] = title
    options = _prompt_options(args)
    if options:
        body["options"] = options
    for key in ("number_min", "number_max"):
        number = _optional_num(args, key)
        if number is not None:
            body[key] = number
    unit = _str(args, "unit")
    if unit:
        body["unit"] = unit
    slots = _names(args, "attest_slots")
    if slots:
        body["attest_slots"] = slots
    severity = _choice(args, "severity", PROMPT_SEVERITIES)
    if severity:
        body["severity"] = severity
    timeout = _optional_num(args, "timeout_s")
    if timeout is not None:
        body["timeout_s"] = timeout
    data = ctx.post("/requests", body)
    if _ok_false(data) or (isinstance(data, dict) and data.get("error")):
        return render(f"The request was not posted: {_problems(data)}", data), True
    rec = data.get("request") if isinstance(data, dict) and isinstance(
        data.get("request"), dict) else data
    rid = (rec.get("id") or rec.get("request_id")) if isinstance(rec, dict) else None
    status = rec.get("status", "pending") if isinstance(rec, dict) else "pending"
    summary = (f"Request {rid or '(no id)'} is with the operator (status {status}). It does not "
               "block: carry on with what you can and read the answer later with "
               f'mast_operator_reply(request_id="{rid}").')
    if structured:
        summary += (' Or wait for it: mast_wait(conditions=[{"type": "operator_reply", '
                    f'"request_id": "{rid}"}}]). Only the operator can answer it.')
    return render(summary, data), False


def _answer_note(rec: dict) -> str:
    """The structured part of a request record, in words: what the operator answered."""
    answer = rec.get("answer") if isinstance(rec.get("answer"), dict) else None
    if not answer:
        return ""
    bits = []
    if answer.get("choice") is not None:
        bits.append(f"choice {answer['choice']!r}")
    if answer.get("value") not in (None, "") and answer.get("value") != answer.get("choice"):
        bits.append(f"value {_one_line(answer['value'], 200)}")
    if answer.get("answered_by"):
        bits.append(f"by {answer['answered_by']}")
    return "; ".join(bits)


@_tool("mast_operator_reply", title="Read operator replies", read_only=True, idempotent=True,
       description=(
    "Read the operator's answers. With request_id: that request, including the reply note or "
    "attached path; a structured prompt also gives prompt_status (answered, dismissed, cancelled "
    "or timeout) and the answer: choice, value and who gave it. Without: every request from this "
    "actor name and its status. Keywords: 答复, 回复."),
    properties={"request_id": _str_p("A request id from mast_ask_operator; omit for all.")})
def _operator_reply(ctx: CallContext, args: dict):
    rid = _str(args, "request_id")
    if rid:
        data = ctx.get(f"/requests/{_seg(rid)}")
        rec = data.get("request") if isinstance(data, dict) and isinstance(
            data.get("request"), dict) else data
        status = rec.get("status", "?") if isinstance(rec, dict) else "?"
        if status == "pending":
            summary = f"Request {rid} is still pending: no answer yet."
        else:
            summary = f"Request {rid}: {status}"
            if isinstance(rec, dict) and rec.get("prompt_status") not in (None, status):
                summary += f" ({rec['prompt_status']})"
            if isinstance(rec, dict):
                said = _answer_note(rec)
                if said:
                    summary += f"; the operator's answer: {said}"
                if rec.get("note"):
                    summary += f"; operator's note: {_one_line(rec['note'], 600)}"
                if rec.get("path"):
                    summary += f"; attached: {rec['path']}"
        return render(summary, data), False
    data = ctx.get("/requests")
    rows = [r for r in _rows(data, "requests", "items", "results") if isinstance(r, dict)]
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r.get("status"))] = counts.get(str(r.get("status")), 0) + 1
    summary = f"{len(rows)} request(s)"
    if counts:
        summary += ": " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    return render(summary, data), False


def _split_syntax(data):
    """Move a long syntax text out of the JSON so its line breaks stay readable."""
    if isinstance(data, dict) and isinstance(data.get("syntax"), str):
        rest = {k: v for k, v in data.items() if k != "syntax"}
        return rest, data["syntax"]
    return data, None


@_tool("mast_composite_draft", title="Check a composite skill", read_only=True,
       idempotent=True, description=(
    "Validate a composite-skill spec (a JSON graph built from existing skills) without saving "
    "it. All problems come back at once, plus hints such as 'an official skill already does "
    "this'. Pass spec='?' to get the spec syntax first. Composites are the normal way to add a "
    "capability: build from existing skills before proposing Python code. "
    "Keywords: 组合技能, 草稿, 校验."),
    properties={"spec": {"type": ["object", "string"],
                         "description": "The spec as a JSON object, or '?' for the syntax."}},
    required=("spec",))
def _composite_draft(ctx: CallContext, args: dict):
    spec = _spec(args)
    data = ctx.post("/composites/draft", {"spec": spec})
    rest, syntax = _split_syntax(data)
    if spec == "?":
        summary = "Composite spec syntax, from MAST:"
    elif isinstance(data, dict) and data.get("ok"):
        summary = "The draft is valid. Save it with mast_composite_save(spec=...)."
        hints = data.get("hints")
        if hints:
            summary += " Hints: " + "; ".join(_one_line(h, 200) for h in list(hints)[:3])
    else:
        summary = f"The draft has problems: {_problems(data) or 'see below'}"
    return render(summary, rest, body=syntax), False


@_tool("mast_composite_save", title="Save a composite skill", description=(
    "Save a composite skill and hot-register it: it can be run at once with mast_run, under the "
    "same safety gates as official skills. Check it with mast_composite_draft first. To "
    "overwrite a composite you saved before, pass base_version = the version you last got "
    "(optimistic lock). Composites made by a person are never overwritten; save under a new "
    "name. Keywords: 组合技能, 保存."),
    properties={"spec": {"type": ["object", "string"],
                         "description": "The validated spec, as a JSON object."},
                "base_version": _int_p("Version you are replacing (only when overwriting).")},
    required=("spec",))
def _composite_save(ctx: CallContext, args: dict):
    spec = _spec(args)
    if spec == "?":
        raise ArgError("for the syntax call mast_composite_draft(spec='?')")
    body: dict = {"spec": spec}
    if args.get("base_version") not in (None, ""):
        body["base_version"] = _int(args, "base_version", default=-1, lo=-1, hi=10 ** 9)
    data = ctx.post("/composites", body)
    if not isinstance(data, dict) or not data.get("ok"):
        err = data.get("error") if isinstance(data, dict) else None
        return render(f"Not saved ({err or 'refused'}): {_problems(data)}", data), True
    name = data.get("name", "?")
    summary = f"Saved composite {name} v{data.get('version', '?')}"
    if data.get("unchanged"):
        summary += " (identical to the stored version; nothing new was written)"
    if data.get("hot_registered") is False:
        summary += f", but registering it failed: {_one_line(data.get('registration_error'), 300)}"
    else:
        summary += f'. Run it with mast_run(skill="{name}", params=...).'
    return render(summary, data), False


@_tool("mast_propose_skill", title="Propose a Python skill", description=(
    "Propose a NEW atomic skill as Python source, only when official skills and composites "
    "cannot express it (for example a Nanonis command nobody wraps yet). The code is written "
    "to disk for a person to review; it is NOT registered or run, and only a person can enable "
    "it. Carry on with existing skills meanwhile. code defines a BaseSkill subclass "
    "(metadata() and execute(context, params)); rationale is what the reviewer judges. "
    "Keywords: 新技能, 提议, Python."),
    properties={"name": _str_p("Skill class/file name for the proposal."),
                "code": _str_p("Complete Python source of the skill."),
                "rationale": _str_p("Why existing skills and composites are not enough.")},
    required=("name", "code", "rationale"))
def _propose_skill(ctx: CallContext, args: dict):
    name = _str(args, "name", required=True)
    data = ctx.post("/skills/proposals", {"name": name,
                                          "code": _text(args, "code", required=True),
                                          "rationale": _text(args, "rationale", required=True)})
    if not isinstance(data, dict) or not data.get("ok"):
        err = data.get("error") if isinstance(data, dict) else None
        return render(f"Proposal not accepted ({err or 'refused'}): {_problems(data)}", data), True
    where = data.get("path") or data.get("file")
    summary = (f"Proposal for {name} written" + (f" to {where}" if where else "")
               + ". It is NOT enabled: a person has to review the code and switch it on. "
               "Carry on with existing skills.")
    return render(summary, data), False


@_tool("mast_handover", title="Write a MAST handover", description=(
    "Finish a work session: MAST writes a handover report (what was done, taken from its "
    "records, plus your summary and next steps) into its document library for the operator "
    "and the next agent. Do this last, once the instrument is idle. "
    "Keywords: 交接, 报告, 收尾."),
    properties={"summary": _str_p("What you did, what you found, what state you leave."),
                "next_steps": _list_p("Suggested next steps, one per item."),
                "title": _str_p("Optional report title."),
                "since": _str_p("Optional ISO time the report should start from.")},
    required=("summary",))
def _handover(ctx: CallContext, args: dict):
    body: dict = {"summary": _text(args, "summary", required=True)}
    steps = _lines(args, "next_steps")
    title = _str(args, "title")
    since = _str(args, "since")
    if steps:
        body["next_steps"] = steps
    if title:
        body["title"] = title
    if since:
        body["since"] = since
    data = ctx.post("/handover", body)
    if _ok_false(data):
        return render(f"Handover not stored: {_problems(data)}", data), True
    ref = None
    if isinstance(data, dict):
        ref = data.get("doc_id") or data.get("document_id") or data.get("id") or data.get("path")
    summary = ("Handover report stored in MAST's document library" + (f" ({ref})" if ref else "")
               + ". The operator and the next agent will find it there.")
    return render(summary, data), False


# ── the optical layout: read, and comment ────────────────────────────────────
LAYOUT_IMAGE_WIDTH = 1000


@_tool("mast_optical_layout", title="Read the optical layout", read_only=True, idempotent=True,
       large=True, description=(
    "Read the operator's optical layout: their drawing of the optical table, with every "
    "component's position, direction, aperture, model, manufacturer, function, whether it is "
    "motorized and what it is connected to. Without layout_id it lists the layouts. With "
    "layout_id it returns a plain-language description: the light sources and, per wavelength, "
    "the components along the beam in order (position, how far off center the beam hits, angle "
    "of incidence, path length so far, links to MAST devices and optical-state slots with their "
    "current state), the problems found (a beam on the back of a mirror, a hit far off center, a "
    "beam that passes just outside a component, a beam leaving the table, components no beam "
    "reaches) and the unresolved comments; the JSON after it has the components' geometry. "
    "image=true attaches a PNG of the table. Coordinates are mm with the origin at the table's "
    "lower left, x to the right, y up; angles are degrees counter-clockwise from +x. The layout "
    "is the operator's record of what is physically on the table: you cannot change it. To ask "
    "a question or suggest a change use mast_optical_layout_comment. Reading opens no device. "
    "Keywords: 光路图, 光路, 器件, 光学平台, 对准, 镜子, 注释."),
    properties={"layout_id": _str_p("Id of the layout (see the list); omit to list the layouts."),
                "image": {"type": "boolean", "default": False,
                          "description": "Attach a PNG of the table, zoomed to the components and the beams."}})
def _optical_layout(ctx: CallContext, args: dict):
    layout_id = _str(args, "layout_id")
    want_image = _bool(args, "image")
    if layout_id is None:
        data = ctx.get("/optical-layouts")
        rows = [r for r in _rows(data, "layouts") if isinstance(r, dict)]
        if not rows:
            return render("No optical layouts have been drawn on this MAST yet.", data), False
        lines = [f"- {r.get('id')}" + (f' "{r["title"]}"' if r.get("title") else "")
                 + (f": {r.get('components')} components, {r.get('open_comments')} open comment(s), "
                    f"revision {r.get('revision')}" if not r.get("error")
                    else f": unreadable ({_one_line(r['error'], 160)})")
                 for r in rows]
        return render(f"{len(rows)} optical layout(s). Read one with layout_id.", data,
                      body="\n".join(lines)), False
    seg = _seg(layout_id)
    data = ctx.get(f"/optical-layouts/{seg}")
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        return render(f"Optical layout {layout_id}", data), False
    lay = data.get("layout") if isinstance(data.get("layout"), dict) else {}
    comps = [{k: c.get(k) for k in ("id", "type", "label", "x_mm", "y_mm", "angle_deg", "aperture_mm")}
             for c in lay.get("components", []) if isinstance(c, dict)]
    payload = {"layout_id": lay.get("id", layout_id), "revision": lay.get("revision"),
               "updated_by": lay.get("updated_by"), "updated_at": lay.get("updated_at"),
               "table": lay.get("table"), "components": comps,
               "open_comments": len(data.get("comments") or [])}
    summary = (f"Optical layout {layout_id}: {len(comps)} components, revision {lay.get('revision')}, "
               f"{payload['open_comments']} open comment(s). You cannot change it; "
               "mast_optical_layout_comment adds a comment.")
    text = render(summary, payload, body=data["summary"])
    if not want_image:
        return text, False
    path = f"/optical-layouts/{seg}/render.png"
    resp = ctx.client.request("GET", path, query={"width": LAYOUT_IMAGE_WIDTH, "fit": "content"},
                              timeout=ctx.timeout(DEFAULT_TIMEOUT_S))
    if not 200 <= resp.status < 300:
        raise ctx.client.http_error("GET", path, resp)
    if "image/png" not in resp.content_type:
        return text + "\n(The picture could not be fetched: MAST did not answer with a PNG.)", False
    image = {"label": f"Optical layout {layout_id}, revision {lay.get('revision')}", "mime": "image/png",
             "data_b64": base64.b64encode(resp.body).decode("ascii")}
    return _with_images(text + "\nThe picture follows: x to the right, y up, in mm; a yellow ring = a hit "
                        "more than 20% of the aperture off center, a red cross = a beam on a back side "
                        "or just outside a component.", [image], is_error=False)


@_tool("mast_optical_layout_comment", title="Comment on the optical layout", description=(
    "Add a comment to the operator's optical layout, optionally about one component "
    "(component_id). Use it to ask something, to point out what you see in the layout's "
    "description (a beam that misses a mirror, a component with no model recorded, a path "
    "length you need) or to suggest a change. The operator sees it on the layout page and "
    "resolves it. You cannot edit the layout or resolve comments yourself: the layout records "
    "what is physically on the table, which only the operator can say. At most 50 unresolved "
    "comments from you per layout. Keywords: 光路图, 评论, 建议, 提问."),
    properties={"layout_id": _str_p("Id of the layout."),
                "text": _str_p("The comment, up to 2000 characters."),
                "component_id": _str_p("Optional: the id of the component the comment is about "
                                       "(it must be in the layout).")},
    required=("layout_id", "text"))
def _optical_layout_comment(ctx: CallContext, args: dict):
    layout_id = _str(args, "layout_id", required=True)
    body: dict = {"text": _text(args, "text", required=True)}
    component_id = _str(args, "component_id")
    if component_id:
        body["component_id"] = component_id
    data = ctx.post(f"/optical-layouts/{_seg(layout_id)}/comments", body)
    comment = data.get("comment") if isinstance(data, dict) and isinstance(data.get("comment"), dict) else {}
    ref = comment.get("id")
    summary = (f"Comment {ref + ' ' if ref else ''}added to optical layout {layout_id}"
               + (f" (about {component_id})" if component_id else "")
               + ". The operator will see it and resolve it; you cannot.")
    return render(summary, data), False


# ── VISTA in MAST 7.0: observe, act, wait ────────────────────────────────────
@_tool("mast_observe", title="Observe the instrument", read_only=True, large=True,
       description=vs.OBSERVE_DESCRIPTION, schema=vs.no_args_schema())
def _observe(ctx: CallContext, args: dict):
    data = ctx.post("/vista/observe", {"client_meta": ctx.client_meta()}, timeout=VISTA_TIMEOUT_S)
    obs = data.get("observation") if isinstance(data, dict) else None
    if not isinstance(obs, dict):
        return render("MAST answered without an observation.", _without_image_data(data)), True
    turn = f" (turn {obs['turn']})" if obs.get("turn") is not None else ""
    summary = f"Observation{turn}."
    if not obs.get("images"):
        summary += " No frame image yet."
    return observation_output(summary, obs)


@_tool("mast_act", title="Instrument action", destructive=True, open_world=True, large=True,
       description=vs.act_description(), schema=vs.act_schema())
def _act(ctx: CallContext, args: dict):
    action = _str(args, "action", required=True)
    body: dict = {"action": action, "params": _obj(args, "params") or {},
                  "request_id": _str(args, "request_id") or _new_request_id(ctx)}
    after = _token(args)
    if after:
        body["after"] = after       # missing: MAST refuses with the latest observation and a token
    note = _text(args, "note")
    if note:
        body["note"] = note
    wait_s = _num(args, "wait_s", default=vs.ACT_WAIT_DEFAULT_S, lo=0, hi=vs.ACT_WAIT_MAX_S)
    body["wait_s"] = _server_wait(ctx, wait_s)
    body["client_meta"] = ctx.client_meta()
    try:
        answer, retried = _submit(ctx, "/vista/act", body)
    except MastError as exc:
        if exc.transient:
            raise MastError(exc.kind, exc.message + (
                " The act may or may not have started: look with mast_observe or mast_history "
                "before acting again; resending the same request_id returns the original act "
                "instead of running it twice."), status=exc.status, payload=exc.payload,
                transient=True) from None
        if _route_missing(exc):
            raise MastError(exc.kind, "This MAST has no /vista/act: it is older than 7.0 and "
                            "offers no VISTA actions. Run skills with mast_run instead.",
                            status=exc.status, payload=exc.payload) from None
        raise _with_submit_hint(exc) from None
    if isinstance(answer, dict) and answer.get("kind") == "job":
        return _job_result(answer.get("job"), retried=retried)
    return act_output(answer, action, retried=retried)


def _conditions(args: dict) -> list:
    value = args.get("conditions")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ArgError("'conditions' must be a JSON list of objects") from None
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ArgError("'conditions' is required: a list such as "
                       '[{"type": "job", "job_id": "j_..."}]')
    for i, cond in enumerate(value):
        if not isinstance(cond, dict) or not isinstance(cond.get("type"), str):
            raise ArgError(f"conditions[{i}] must be an object with a 'type' "
                           f"({', '.join(vs.WAIT_TYPES)})")
    return value


def _fired_text(fired: dict) -> str:
    if fired.get("type") == "gap":
        return ("a gap (events after the cursor were lost; read the state again with mast_observe "
                "or mast_history)")
    text = f"{fired.get('type', '?')} (condition {fired.get('index', '?')}"
    event = fired.get("event") if isinstance(fired.get("event"), dict) else {}
    if fired.get("type") == "operator_reply" and event.get("request_id"):
        text += f", request {event['request_id']}"
        if event.get("structured"):
            text += f": {event.get('prompt_status') or event.get('status')}"
            if event.get("choice") is not None:
                text += f", choice {event['choice']!r}"
            if event.get("value") not in (None, "") and event.get("value") != event.get("choice"):
                text += f", value {_one_line(event['value'], 80)}"
    return text + ")"


def wait_output(answer) -> ToolOutput:
    if not isinstance(answer, dict):
        return ToolOutput(render("MAST answered the wait with something unexpected.", answer), True)
    fired = [f for f in answer.get("fired") or [] if isinstance(f, dict)]
    waited = answer.get("waited_s")
    took = f" after {_secs(waited)}" if waited is not None else ""
    if fired:
        what = ", ".join(_fired_text(f) for f in fired)
        summary = f"mast_wait: {what} fired{took}."
    elif answer.get("timed_out"):
        summary = (f"mast_wait timed out{took}: nothing fired. Call it again to keep waiting; it "
                   "continues from this answer's cursor, so nothing is missed.")
    else:
        summary = f"mast_wait returned{took}."
    text = render(summary, _without_image_data({k: v for k, v in answer.items()
                                                if k != "observation"}))
    obs = answer.get("observation")
    if not isinstance(obs, dict):
        return ToolOutput(text, False)
    return observation_output(text + "\nThe wait brought a new observation.", obs)


@_tool("mast_wait", title="Wait for MAST", read_only=True, large=True, budget_s=WAIT_BUDGET_S,
       description=vs.WAIT_DESCRIPTION, schema=vs.wait_schema())
def _wait(ctx: CallContext, args: dict):
    longest = ctx.max_wait_s
    timeout_s = round(_num(args, "timeout_s", default=min(vs.WAIT_DEFAULT_S, longest), lo=1,
                           hi=longest))
    body: dict = {"conditions": _conditions(args), "timeout_s": timeout_s,
                  "client_meta": ctx.client_meta()}
    since = _str(args, "since") or ctx.session.wait_cursor
    if since:
        body["since"] = since
    answer = ctx.post("/wait", body, timeout=timeout_s + WAIT_READ_SLACK_S)
    # A cancelled call's answer is never read: keep the old cursor so its events are not lost.
    if isinstance(answer, dict) and not ctx.cancelled():
        ctx.session.remember_cursor(answer.get("cursor"))
    return wait_output(answer)


# ── VISTA: archive, measure, notes, history ──────────────────────────────────
def _vista_post(path: str, what: str):
    """A handler that hands the arguments to a VISTA endpoint and passes its reply on."""
    def handler(ctx: CallContext, args: dict):
        body = dict(args)
        body["client_meta"] = ctx.client_meta()
        return reply_output(ctx.post(path, body, timeout=VISTA_TIMEOUT_S), what)
    return handler


_tool("mast_inspect", title="Inspect archived frames", read_only=True, idempotent=True, large=True,
      description=vs.inspect_description(), schema=vs.inspect_schema())(
    _vista_post("/vista/inspect", "Inspect"))
_tool("mast_read_values", title="Read archived values", read_only=True, idempotent=True,
      large=True, description=vs.READ_VALUES_DESCRIPTION, schema=vs.read_values_schema())(
    _vista_post("/vista/read_values", "Read values"))


def _register_measure_tools() -> None:
    for name, (title, description, schema, idempotent) in vs.MEASURE_TOOLS.items():
        _tool(f"mast_{name}", title=title, read_only=True, idempotent=idempotent, large=True,
              description=description, schema=schema())(
            _vista_post(f"/vista/measure/{name}", title))


_register_measure_tools()


@_tool("mast_history", title="Experiment history", read_only=True, idempotent=True, large=True,
       description=vs.HISTORY_DESCRIPTION, schema=vs.history_schema())
def _history(ctx: CallContext, args: dict):
    query: dict = {"view": _choice(args, "view", ("events", "attempts"), default="events")}
    for key, lo, hi in (("start_turn", 0, 999_999), ("end_turn", 0, 999_999), ("limit", 1, 128)):
        if args.get(key) not in (None, ""):
            query[key] = _int(args, key, default=lo, lo=lo, hi=hi)
    return reply_output(ctx.get("/vista/history", query), "History")


def _notes_reader(which: str, what: str):
    def handler(ctx: CallContext, args: dict):
        return reply_output(ctx.get(f"/vista/notes/{which}"), what)
    return handler


def _notes_writer(which: str, what: str):
    def handler(ctx: CallContext, args: dict):
        body = {"content": _text(args, "content", required=True), "client_meta": ctx.client_meta()}
        return reply_output(ctx.put(f"/vista/notes/{which}", body), what)
    return handler


_tool("mast_read_guide", title="Read GUIDE.md", read_only=True, idempotent=True, large=True,
      description=vs.READ_GUIDE_DESCRIPTION, schema=vs.no_args_schema())(
    _notes_reader("guide", "GUIDE.md"))
_tool("mast_write_guide", title="Write GUIDE.md", idempotent=True,
      description=vs.WRITE_GUIDE_DESCRIPTION, schema=vs.content_schema(vs.MAX_GUIDE_CHARS))(
    _notes_writer("guide", "GUIDE.md saved"))
_tool("mast_read_working", title="Read WORKING.md", read_only=True, idempotent=True, large=True,
      description=vs.READ_WORKING_DESCRIPTION, schema=vs.no_args_schema())(
    _notes_reader("working", "WORKING.md"))
_tool("mast_write_working", title="Write WORKING.md", idempotent=True,
      description=vs.WRITE_WORKING_DESCRIPTION, schema=vs.content_schema(vs.MAX_WORKING_CHARS))(
    _notes_writer("working", "WORKING.md saved"))


# ── detectors, literature ────────────────────────────────────────────────────
def _detector_of(data) -> dict:
    if isinstance(data, dict) and isinstance(data.get("detector"), dict):
        return data["detector"]
    return data if isinstance(data, dict) else {}


def _detector_ref(det: dict, fallback: str = "?") -> str:
    ref = str(det.get("id") or det.get("detector_id") or fallback)
    return ref + (f' "{det["name"]}"' if det.get("name") else "")


def _detector_state(det: dict) -> str:
    if det.get("enabled") is True:
        return "enabled"
    if det.get("enabled") is False:
        why = det.get("disabled_reason") or det.get("reason")
        return "disabled" + (f" ({_one_line(why, 160)})" if why else "")
    return "state unknown"


@_tool("mast_detector_create", title="Create a detector", large=True,
       description=vs.DETECTOR_CREATE_DESCRIPTION, schema=vs.detector_create_schema())
def _detector_create(ctx: CallContext, args: dict):
    applies = _names(args, "applies_to")
    if not applies:
        raise ArgError("'applies_to' is required: [\"scan\"], [\"spectrum\"] or both")
    bad = [a for a in applies if a not in ("scan", "spectrum")]
    if bad:
        raise ArgError(f"'applies_to' takes scan and spectrum, not {', '.join(bad)}")
    body: dict = {"name": _str(args, "name", required=True),
                  "code": _text(args, "code", required=True), "applies_to": applies}
    description = _text(args, "description")
    if description:
        body["description"] = description
    if args.get("max_runtime_s") not in (None, ""):
        body["max_runtime_s"] = _num(args, "max_runtime_s", default=vs.DETECTOR_DEFAULT_RUNTIME_S,
                                     lo=1, hi=vs.DETECTOR_MAX_RUNTIME_S)
    data = ctx.post("/detectors", body)
    det = _detector_of(data)
    summary = (f"Detector {_detector_ref(det)} created, {_detector_state(det)}. Test it on an "
               "archived frame with mast_detectors(op=\"test\") before relying on it; its hits end "
               'a mast_wait on {"type": "detector"}.')
    return render(summary, data), False


@_tool("mast_detectors", title="MAST detectors", large=True, budget_s=DETECTOR_BUDGET_S,
       description=vs.DETECTORS_DESCRIPTION, schema=vs.detectors_schema())
def _detectors(ctx: CallContext, args: dict):
    op = _choice(args, "op", vs.DETECTOR_OPS)
    if op is None:
        raise ArgError(f"'op' is required: one of {', '.join(vs.DETECTOR_OPS)}")
    if op == "list":
        data = ctx.get("/detectors")
        rows = [d for d in _rows(data, "detectors", "items", "results") if isinstance(d, dict)]
        summary = f"{len(rows)} detector(s)"
        if rows:
            summary += ": " + "; ".join(f"{_detector_ref(d)} {_detector_state(d)}"
                                        for d in rows[:12])
        return render(summary, data), False
    did = _str(args, "detector_id", required=True)
    path = f"/detectors/{_seg(did)}"
    if op == "get":
        data = ctx.get(path)
        det = _detector_of(data)
        return render(f"Detector {_detector_ref(det, did)}: {_detector_state(det)}", data), False
    if op in ("enable", "disable"):
        data = ctx.post(path + "/enable", {"enabled": op == "enable"})
        det = _detector_of(data)
        return render(f"Detector {_detector_ref(det, did)}: {_detector_state(det)}", data), False
    if op == "delete":
        data = ctx.delete(path)
        return render(f"Detector {did} deleted.", data), False
    frame = _str(args, "frame", required=True)
    data = ctx.post(path + "/test", {"frame": frame},
                    timeout=vs.DETECTOR_MAX_RUNTIME_S + WAIT_READ_SLACK_S)
    result = data.get("result") if isinstance(data, dict) and "result" in data else data
    if isinstance(data, dict) and data.get("error"):
        outcome, failed = f"it raised an error: {_one_line(data['error'], 300)}", True
    elif isinstance(result, dict) and "hit" in result:
        outcome, failed = ("a hit" if result.get("hit") else "no hit"), False
    else:
        outcome, failed = "it ran", False
    return render(f"Test of detector {did} on {frame}: {outcome}.", data), failed


@_tool("mast_literature_search", title="Search the literature", read_only=True, idempotent=True,
       open_world=True, large=True, description=vs.LITERATURE_DESCRIPTION,
       schema=vs.literature_schema())
def _literature(ctx: CallContext, args: dict):
    query = _str(args, "query", required=True)
    body: dict = {"query": query, "k": _int(args, "k", default=vs.LITERATURE_K_DEFAULT, lo=1,
                                            hi=vs.LITERATURE_K_MAX)}
    material = _str(args, "material")
    if material:
        body["material"] = material
    data = ctx.post("/literature/search", body)
    hits = [h for h in _rows(data, "hits", "results") if isinstance(h, dict)]
    summary = f'{len(hits)} paper(s) for "{_one_line(query, 120)}"'
    if hits:
        summary += ": " + "; ".join(
            _one_line(h.get("title") or "untitled", 90)
            + (f" ({h['year']})" if h.get("year") else "") for h in hits[:5])
        summary += "; ..." if len(hits) > 5 else ""
    if isinstance(data, dict) and data.get("degraded"):
        summary += ". Degraded: " + _one_line(
            data.get("note") or "keyword matching only, or no library on this machine", 300)
    return render(summary, data), False
