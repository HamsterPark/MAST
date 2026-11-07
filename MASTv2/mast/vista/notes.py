# Derived from VISTA (MIT), github.com/joshhhhhan/VISTA, core/{tools,dispatcher,session}.py
"""GUIDE.md and WORKING.md: the agent-authored notes (``docs/v2/architecture/mast7_vista.md`` §1).

README
------
``Notes(root)`` keeps two Markdown files under ``root``:

* ``GUIDE.md`` — durable, revisable understanding of the instrument, the sample and the
  tip (one file for all workspaces). It starts as ``"No reliable model yet.\\n"`` (VISTA's seed) and is
  at most :data:`~mast.vista.protocol.MAX_GUIDE_CHARS` characters.
* ``WORKING.md`` — the current plan and state, one per workspace. It survives context
  replacement: a fresh session of :mod:`mast.vista.runner` starts from it. At most
  :data:`~mast.vista.protocol.MAX_WORKING_CHARS` characters. It does not exist until
  the agent writes it.

Writes replace the whole file atomically (temporary file + ``os.replace``); the stored
text is the stripped content plus one newline, as in VISTA. Every write (and every
archive) is appended to ``notes_log.jsonl`` together with its *provenance*: whatever the
``provenance()`` callback given to :class:`Notes` returns at that moment (turn, acts ...).
The provenance of the last WORKING.md write is kept in
``working.provenance.json`` so a recovery message can say when the state was saved
(VISTA's ``saved_at``). ``archive_working(reason)`` moves WORKING.md into
``working_archive/`` when the owner decides the scratch state is obsolete.

:data:`NOTES_TOOLS` holds the four tool definitions (``read_guide``, ``write_guide``,
``read_working``, ``write_working``); :meth:`Notes.call` executes one of them and never
raises — invalid input comes back as an ``is_error`` reply that says what to change.
The checkpoint protocol (a WORKING.md write that completes a requested checkpoint) is the
business of :class:`mast.vista.service.VistaService`: it wraps :meth:`Notes.call`.

Thread safety: every public method takes one lock, so concurrent HTTP requests may share
one instance.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .protocol import (
    MAX_GUIDE_CHARS,
    MAX_WORKING_CHARS,
    ToolDef,
    ToolReply,
    compact_json,
    error_reply,
    json_reply,
)

GUIDE_FILE = "GUIDE.md"
WORKING_FILE = "WORKING.md"
LOG_FILE = "notes_log.jsonl"
WORKING_PROVENANCE_FILE = "working.provenance.json"
ARCHIVE_DIR = "working_archive"
GUIDE_INITIAL = "No reliable model yet.\n"


class NotesError(ValueError):
    """Invalid notes content (empty, too long, not a string)."""


def _no_args_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "properties": {}}


def _content_schema(limit: int) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["content"],
            "properties": {"content": {"type": "string", "minLength": 1, "maxLength": limit}}}


NOTES_TOOLS: dict[str, ToolDef] = {
    "read_guide": ToolDef(
        name="read_guide",
        title="Read GUIDE.md",
        description=("Read `GUIDE.md`: your durable notes for this episode about the instrument, "
                     "the sample and the tip."),
        input_schema=_no_args_schema(),
        read_only=True, destructive=False, idempotent=True),
    "write_guide": ToolDef(
        name="write_guide",
        title="Write GUIDE.md",
        description=("Write the complete contents of `GUIDE.md`, replacing its previous contents. "
                     "Keep in it concise, durable, revisable understanding of the instrument, the "
                     "sample and the tip for this episode, and revise it when new evidence changes "
                     f"what is supported. At most {MAX_GUIDE_CHARS} characters."),
        input_schema=_content_schema(MAX_GUIDE_CHARS),
        read_only=False, destructive=False, idempotent=True),
    "read_working": ToolDef(
        name="read_working",
        title="Read WORKING.md",
        description=("Read `WORKING.md`: your temporary state for this episode — the current plan "
                     "and where you are in it."),
        input_schema=_no_args_schema(),
        read_only=True, destructive=False, idempotent=True),
    "write_working": ToolDef(
        name="write_working",
        title="Write WORKING.md",
        description=("Replace the contents of `WORKING.md`, your temporary state: the active plan, "
                     "the next expected observation, unresolved contradictions and the frame ids "
                     "you still need. It persists until you replace it and survives context "
                     "replacement: a fresh context receives GUIDE.md, WORKING.md and the current "
                     f"observation. At most {MAX_WORKING_CHARS} characters."),
        input_schema=_content_schema(MAX_WORKING_CHARS),
        read_only=False, destructive=False, idempotent=True),
}

#: Names of the notes tools (what the service lets through while a checkpoint is requested).
NOTES_TOOL_NAMES = frozenset(NOTES_TOOLS)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


class Notes:
    """The GUIDE.md / WORKING.md store of one workspace (see the module docstring)."""

    def __init__(self, root: str | Path, *, provenance: Callable[[], dict] | None = None,
                 guide_initial: str = GUIDE_INITIAL) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._provenance = provenance
        self._lock = threading.RLock()
        if not self.guide_path.exists():
            _atomic_write(self.guide_path, guide_initial)
            self._log({"op": "init", "file": GUIDE_FILE, "chars": len(guide_initial),
                       "sha256": _sha256(guide_initial)})

    # ── paths ────────────────────────────────────────────────────────────────
    @property
    def guide_path(self) -> Path:
        return self.root / GUIDE_FILE

    @property
    def working_path(self) -> Path:
        return self.root / WORKING_FILE

    @property
    def log_path(self) -> Path:
        return self.root / LOG_FILE

    @property
    def archive_dir(self) -> Path:
        return self.root / ARCHIVE_DIR

    # ── reads ────────────────────────────────────────────────────────────────
    def read_guide(self) -> str:
        with self._lock:
            return self.guide_path.read_text(encoding="utf-8", errors="replace")

    def read_working(self) -> str | None:
        """WORKING.md, or ``None`` when the agent has not written it (or it was archived)."""
        with self._lock:
            try:
                return self.working_path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                return None

    def working_saved_at(self) -> dict | None:
        """Provenance record of the last WORKING.md write (``None`` if there is none)."""
        with self._lock:
            path = self.root / WORKING_PROVENANCE_FILE
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return None
            return value if isinstance(value, dict) else None

    def snapshot(self) -> dict:
        """``{"guide", "working", "working_saved_at"}`` — what a recovery message carries."""
        with self._lock:
            return {"guide": self.read_guide(), "working": self.read_working(),
                    "working_saved_at": self.working_saved_at()}

    # ── writes ───────────────────────────────────────────────────────────────
    def write_guide(self, content: Any) -> dict:
        """Replace GUIDE.md. Returns the log record; raises :class:`NotesError`."""
        text = self._validated(content, MAX_GUIDE_CHARS, GUIDE_FILE)
        with self._lock:
            _atomic_write(self.guide_path, text)
            return self._log({"op": "write", "file": GUIDE_FILE, "chars": len(text),
                              "sha256": _sha256(text)})

    def write_working(self, content: Any) -> dict:
        """Replace WORKING.md. Returns the log record; raises :class:`NotesError`."""
        text = self._validated(content, MAX_WORKING_CHARS, WORKING_FILE)
        with self._lock:
            _atomic_write(self.working_path, text)
            record = self._log({"op": "write", "file": WORKING_FILE, "chars": len(text),
                                "sha256": _sha256(text)})
            _atomic_write(self.root / WORKING_PROVENANCE_FILE,
                          json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            return record

    def archive_working(self, reason: str) -> Path | None:
        """Move WORKING.md to ``working_archive/NNN_<reason>.md``; ``None`` if it does not exist."""
        with self._lock:
            if not self.working_path.exists():
                return None
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            index = len(list(self.archive_dir.glob("*.md")))
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(reason))[:48] or "archived"
            target = self.archive_dir / f"{index:03d}_{safe}.md"
            os.replace(self.working_path, target)
            prov = self.root / WORKING_PROVENANCE_FILE
            if prov.exists():
                os.replace(prov, target.with_suffix(".provenance.json"))
            self._log({"op": "archive", "file": WORKING_FILE, "reason": str(reason),
                       "archived_as": target.name})
            return target

    # ── tools ────────────────────────────────────────────────────────────────
    def call(self, name: str, args: Any) -> ToolReply:
        """Execute one notes tool. Never raises."""
        if not isinstance(args, dict):
            return error_reply(f"{name}: arguments must be a JSON object")
        try:
            if name in ("read_guide", "read_working"):
                if args:
                    return error_reply(f"{name} takes no arguments", got=sorted(args))
                if name == "read_guide":
                    text = self.read_guide()
                    return ToolReply(text=text, meta={"file": GUIDE_FILE, "chars": len(text)})
                text = self.read_working()
                if text is None:
                    return ToolReply(text="WORKING.md does not exist yet.",
                                     meta={"file": WORKING_FILE, "chars": 0})
                return ToolReply(text=text, meta={"file": WORKING_FILE, "chars": len(text)})
            if name in ("write_guide", "write_working"):
                extra = sorted(set(args) - {"content"})
                if extra:
                    return error_reply(f"{name} takes only 'content'", unexpected=extra)
                if "content" not in args:
                    return error_reply(f"{name} needs 'content' (the complete new file)")
                if name == "write_guide":
                    record = self.write_guide(args["content"])
                    body = {"saved": GUIDE_FILE, "chars": record["chars"]}
                else:
                    record = self.write_working(args["content"])
                    body = {"saved": WORKING_FILE, "chars": record["chars"]}
                return json_reply(body, effect="notes", meta={"record": record})
            return error_reply(f"unknown notes tool {name!r}", known=sorted(NOTES_TOOLS))
        except NotesError as exc:
            return error_reply(str(exc))
        except OSError as exc:
            return error_reply(f"{name} failed: {type(exc).__name__}: {exc}")

    #: Alias of :meth:`call` (the name VISTA-SPM's host called; MAST's service uses ``call``).
    handle = call

    # ── internals ────────────────────────────────────────────────────────────
    @staticmethod
    def _validated(content: Any, limit: int, file: str) -> str:
        if not isinstance(content, str):
            raise NotesError(f"{file} content must be a string")
        if not content.strip():
            raise NotesError(f"{file} content must not be empty")
        if len(content) > limit:
            raise NotesError(f"{file} content has {len(content)} characters; the limit is {limit}")
        return content.strip() + "\n"

    def _log(self, record: dict) -> dict:
        full = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
        if self._provenance is not None:
            try:
                prov = self._provenance()
            except Exception as exc:  # noqa: BLE001 — provenance must never block a write
                prov = {"provenance_error": f"{type(exc).__name__}: {exc}"}
            if isinstance(prov, dict):
                full["provenance"] = prov
        with self.log_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(compact_json(full) + "\n")
        return full
