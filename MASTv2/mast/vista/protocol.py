"""Shared types of the VISTA harness in MAST. Every module of :mod:`mast.vista` codes against this file.

Standard library only: nothing here imports numpy, PIL or the rest of MAST. Design contract:
``docs/v2/architecture/mast7_vista.md``.

Contents
--------
* :class:`ImageBlock` / :class:`ToolReply` — what a tool returns, and its MCP rendering;
  :class:`mast.vista.service.VistaService` sends it over HTTP as ``{text, images, is_error}``.
* :class:`ToolDef` — a tool's name, description and JSON schema, and its MCP rendering.
* :class:`ToolHandler` / :class:`RunnerHost` — VISTA-SPM's in-process interfaces, kept for
  reference; their docstrings say what replaces them in MAST.
* Frame ids (:func:`frame_id`, :func:`parse_frame_id`) and the display-coordinate
  convention (see the docstring of :data:`DISPLAY_CONVENTION`).
"""
from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: VISTA-SPM's MCP server name, kept for reference. MAST's MCP server is ``mast``
#: (``integrations/claude-code/server/mast_mcp``), so its tools appear as ``mcp__mast__<tool>``.
SERVER_NAME = "stm"
#: MCP protocol revision VISTA-SPM's bridge announced; MAST's bridge negotiates its own
#: (``mast_mcp.stdio_rpc``).
MCP_PROTOCOL = "2025-06-18"

#: Longest side, in pixels, of a rendered observation unless the archive is built with another
#: ``display_max``.
DISPLAY_MAX_DEFAULT = 768

#: Hard caps shared by the tool schemas and their implementations (VISTA's numbers where
#: they exist).
MAX_INSPECT_VIEWS = 16
MAX_READ_VIEWS = 64
MAX_READ_SAMPLES = 4096
MAX_QUESTION_CHARS = 1024
MAX_LABEL_CHARS = 128
MAX_GUIDE_CHARS = 64 * 1024
MAX_WORKING_CHARS = 16 * 1024

DISPLAY_CONVENTION = """
Display coordinates (what the model sees and points at)
--------------------------------------------------------
A frame with ``nx`` x ``ny`` pixels is shown as an image of ``nx*k`` x ``ny*k`` pixels,
``k = max(1, DISPLAY_MAX // max(nx, ny))`` (integer nearest-neighbour upscaling, never
smoothing). A frame larger than ``DISPLAY_MAX`` is shown at ``k = 1/ceil(max(nx,ny)/DISPLAY_MAX)``
(integer block-mean downsampling); ``k`` is always reported next to the image.

Display coordinates ``(x, y)`` are continuous, origin at the top-left CORNER of the image,
``x`` to the right, ``y`` down. Row 0 of the image is the frame's top edge (MAST's
``sxm_oriented_frames`` convention: ``scan_dir=up`` already flipped, backward already
un-mirrored). The native continuous coordinate is ``(x/k, y/k)``; native pixel ``i`` covers
``[i, i+1)`` and its centre is ``i + 0.5``.

Scan-frame nm (what the instrument and the actions use):
``u = -w/2 + (x/k) * w/nx``, ``v = +h/2 - (y/k) * h/ny`` (w, h in nm), then rotate by the
frame's scan angle about its centre and add the centre::

    X = cx + cos(a) u - sin(a) v ,   Y = cy + sin(a) u + cos(a) v

This is :func:`mast.vista.frames.display_to_scan_nm` (inverse:
:func:`mast.vista.frames.scan_nm_to_display`).
"""

#: Frame-id kinds: ``s`` saved scan (.sxm), ``p`` partial snapshot of a scan in progress,
#: ``d`` saved spectrum (.dat), ``m`` derived image a measurement tool produced.
FRAME_KINDS = ("s", "p", "d", "m")
_FRAME_RE = re.compile(r"^t(\d{4,})\.([spdm])(\d+)$")


def frame_id(turn: int, kind: str, index: int) -> str:
    """``frame_id(7, "s", 0) == "t0007.s0"``. ``turn`` is the action turn that produced it
    (turn 0 = frames archived before the workspace's first act)."""
    if kind not in FRAME_KINDS:
        raise ValueError(f"frame kind {kind!r} not in {FRAME_KINDS}")
    if turn < 0 or index < 0:
        raise ValueError("turn and index must be >= 0")
    return f"t{int(turn):04d}.{kind}{int(index)}"


def parse_frame_id(fid: str) -> tuple[int, str, int]:
    m = _FRAME_RE.match(str(fid or "").strip())
    if not m:
        raise ValueError(f"not a frame id: {fid!r} (expected like 't0007.s0')")
    return int(m.group(1)), m.group(2), int(m.group(3))


def compact_json(obj: Any) -> str:
    """The text block of a tool reply: compact, UTF-8, numbers as numbers. NaN and +-Infinity are
    not JSON, so they are written as ``null`` (a reply carrying them would not parse)."""
    return json.dumps(_finite(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                      default=_json_default)


def _finite(o: Any) -> Any:
    """``o`` with every non-finite float replaced by ``None``. numpy scalars and arrays become the
    Python values :func:`_json_default` would give them, so finite values serialize as before."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    if isinstance(o, (str, int, bool)) or o is None:
        return o
    converted = _json_default(o)                 # str(o) stays a string; numpy values are checked
    return converted if isinstance(converted, str) else _finite(converted)


def _json_default(o: Any) -> Any:
    item = getattr(o, "item", None)              # numpy scalars
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    tolist = getattr(o, "tolist", None)          # numpy arrays
    if callable(tolist):
        return tolist()
    return str(o)


@dataclass(frozen=True)
class ImageBlock:
    """One image of a tool reply. ``label`` is the text block placed right before it, e.g.
    ``<visual kind="current" frame="t0007.s0" channel="Z" direction="forward" scale="2" size="768x768"/>``.
    Images carry no text inside them; everything the model needs to read is in the label
    or in the reply text (VISTA's rule)."""

    png: bytes
    label: str

    def b64(self) -> str:
        return base64.b64encode(self.png).decode("ascii")


@dataclass
class ToolReply:
    """What every tool returns.

    ``text`` is the first content block (compact JSON from :func:`compact_json`, or a short
    sentence for errors). ``images`` follow, each as a label text block plus an image block.
    ``effect``: ``"none"`` (read-only), ``"notes"`` (GUIDE/WORKING changed), ``"report"``
    (a result/diagnosis was recorded), ``"environment"`` (the instrument acted — only
    ``act``). ``effect`` and ``meta`` are for the server's own records and are never sent to
    the model (:func:`mast.vista.service.reply_dict` drops them).
    """

    text: str
    images: list[ImageBlock] = field(default_factory=list)
    is_error: bool = False
    effect: str = "none"
    meta: dict = field(default_factory=dict)

    def to_mcp(self) -> dict:
        content: list[dict] = [{"type": "text", "text": self.text}]
        for im in self.images:
            content.append({"type": "text", "text": im.label})
            content.append({"type": "image", "data": im.b64(), "mimeType": "image/png"})
        return {"content": content, "isError": bool(self.is_error)}


def json_reply(obj: Any, *, images: list[ImageBlock] | None = None, effect: str = "none",
               meta: dict | None = None) -> ToolReply:
    return ToolReply(text=compact_json(obj), images=list(images or []), effect=effect, meta=dict(meta or {}))


def error_reply(message: str, **details: Any) -> ToolReply:
    """A refused or failed call. The message says what to do differently; nothing executed
    unless the message says otherwise."""
    body = {"error": str(message)}
    if details:
        body.update(details)
    return ToolReply(text=compact_json(body), is_error=True)


@dataclass(frozen=True)
class ToolDef:
    """A tool as the model sees it. ``input_schema`` is JSON Schema (draft 2020-12) with
    ``additionalProperties: false`` at the top level."""

    name: str
    description: str
    input_schema: dict
    title: str = ""
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = False

    def to_mcp(self) -> dict:
        return {"name": self.name, "title": self.title or self.name, "description": self.description,
                "inputSchema": self.input_schema,
                "annotations": {"readOnlyHint": self.read_only, "destructiveHint": self.destructive,
                                "idempotentHint": self.idempotent, "openWorldHint": False}}


@runtime_checkable
class ToolHandler(Protocol):
    """VISTA-SPM's interface between its RPC server and the tools, kept for reference: in MAST the
    HTTP API (:mod:`mast.api.ext.vista`) calls :class:`mast.vista.service.VistaService`."""

    def list_tools(self) -> list[dict]:
        """MCP tool dicts (:meth:`ToolDef.to_mcp`), in the order the model should see them."""
        ...

    def call(self, name: str, args: dict) -> ToolReply:
        """Execute one tool call. Must never raise: failures come back as ``is_error`` replies."""
        ...


@runtime_checkable
class RunnerHost(ToolHandler, Protocol):
    """VISTA-SPM's in-process contract between its Claude Code runner and its episode host, kept
    for reference; nothing in MAST implements it. In MAST the host role is
    :class:`mast.vista.service.VistaService` (state per experiment in :mod:`mast.vista.workspace`),
    the runner is :mod:`mast.vista.runner`, and the two meet over MAST's HTTP API and MCP bridge.
    One act per observation is enforced by the service's observation token
    (:mod:`mast.vista.gate`) instead of ``acknowledge_act``.
    """

    def initial_message(self) -> tuple[str, list[ImageBlock]]:
        """Task text + the starting observation, for the first user turn of segment 0."""
        ...

    def recovery_message(self) -> tuple[str, list[ImageBlock]]:
        """First user turn of a fresh session after a checkpoint or a crash: environment
        record (current observation metadata, last action result, recent history), the
        agent's notes (GUIDE.md, WORKING.md), and the current frame image."""
        ...

    def acknowledge_act(self) -> None:
        """The result of the last ``act`` is now in the model's context: the next ``act``
        may execute (VISTA's one-action-one-observation gate)."""
        ...

    def request_checkpoint(self, reason: str) -> None:
        """Ask the model to save WORKING.md before the context is replaced. While a
        checkpoint is requested only the notes tools execute."""
        ...

    def status(self) -> dict:
        """``{"terminal": bool, "instrument_closed": bool, "stop_reason": str | None,
        "checkpoint": "none"|"requested"|"ready", "images_delivered": int, "acts": int, "turn": int,
        "budget": {...}}``."""
        ...

    def begin_segment(self, index: int, session_id: str) -> None:
        ...

    def end_segment(self, index: int, info: dict) -> None:
        """``info``: exit code, context boundary reason, usage of the segment."""
        ...

    def finish(self, outcome: str, final_text: str) -> None:
        """``outcome`` in {"done", "abort", "budget", "error", "stalled", "max_segments"}."""
        ...
