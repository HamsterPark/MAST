"""Workspace: the VISTA state of one MAST experiment (archive, notes, events, turns).

README
------
VISTA-SPM kept everything per episode. In MAST the unit that persists is the experiment: a
workspace lives in ``<project_root>/experiments/vista/<workspace_id>/`` and is reopened after a
restart (design contract ``docs/v2/architecture/mast7_vista.md`` §1):

* ``archive/`` — the lossless frame archive (:class:`mast.vista.archive.Archive`);
* ``notes/WORKING.md`` — the agent's working memory for this experiment; ``GUIDE.md`` (durable
  lessons) is shared by all workspaces and lives one level up;
* ``events.jsonl`` — act, observe, refusal, external-frame, checkpoint and detector records,
  read back by the ``history`` tool;
* ``detectors.json`` — written by :mod:`mast.vista.detectors`.

The turn counter grows by one per accepted act and is restored from the archive and the events.
Nothing here talks to the instrument.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .actions import ActionError
from .archive import Archive
from .notes import Notes
from .protocol import DISPLAY_MAX_DEFAULT, ToolReply, error_reply, json_reply

GUIDE_SHARED_NAME = "GUIDE.md"
EVENTS_NAME = "events.jsonl"
#: events kept in memory per workspace (the files keep all of them)
EVENTS_IN_MEMORY = 5000
#: events.jsonl is rotated (renamed to events-<time>.jsonl) above this size, so a chatty client
#: cannot grow one file without bound; the turn high-water mark survives in state.json
EVENTS_ROTATE_BYTES = 32 * 1024 * 1024
STATE_NAME = "state.json"
HISTORY_LIMIT_MAX = 128

_GUIDE_LOCK = threading.RLock()


def workspace_id_for(experiment_id: Any) -> str:
    """``exp-<id>`` with filesystem-safe characters, or ``no-experiment``."""
    raw = str(experiment_id or "").strip()
    if not raw:
        return "no-experiment"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)[:80].strip("._") or "unnamed"
    return f"exp-{safe}"


class _WorkspaceNotes(Notes):
    """VISTA notes with GUIDE.md shared by every workspace of this MAST installation."""

    def __init__(self, root: Path, guide_path: Path, **kw: Any) -> None:
        self._shared_guide = Path(guide_path)
        self._shared_guide.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(root, **kw)
        self._lock = _GUIDE_LOCK          # one lock for the shared file across workspaces

    @property
    def guide_path(self) -> Path:          # type: ignore[override]
        return self._shared_guide


class Workspace:
    """Archive + notes + event log of one experiment. Thread-safe."""

    def __init__(self, root: str | Path, workspace_id: str, *, guide_path: str | Path,
                 display_max: int = DISPLAY_MAX_DEFAULT, grid: bool = True,
                 provenance=None) -> None:
        self.id = str(workspace_id)
        self.dir = Path(root) / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.archive = Archive(self.dir / "archive", display_max=display_max, grid=grid)
        self.notes = _WorkspaceNotes(self.dir / "notes", Path(guide_path), provenance=provenance)
        self._lock = threading.RLock()
        self._events: list[dict] = []
        self._seq = 0
        self._turn = int(self.archive.current_turn)
        self._load_events()
        try:
            saved = json.loads((self.dir / STATE_NAME).read_text(encoding="utf-8"))
            self._turn = max(self._turn, int(saved.get("turn") or 0))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    # ── events ──────────────────────────────────────────────────────────────
    @property
    def events_path(self) -> Path:
        return self.dir / EVENTS_NAME

    def _load_events(self) -> None:
        p = self.events_path
        if not p.exists():
            return
        rows: list[dict] = []
        with p.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue                      # a torn last line after a power cut
                if isinstance(rec, dict):
                    rows.append(rec)
        self._events = rows[-EVENTS_IN_MEMORY:]
        for rec in rows:
            self._seq = max(self._seq, int(rec.get("seq") or 0))
            if rec.get("kind") == "act":
                self._turn = max(self._turn, int(rec.get("turn") or 0))

    def append_event(self, record: dict) -> dict:
        """Append one record (adds ``seq`` and ``t``). A write failure is not fatal."""
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "t": round(time.time(), 3), **record}
            self._events.append(rec)
            if len(self._events) > EVENTS_IN_MEMORY:
                del self._events[: len(self._events) - EVENTS_IN_MEMORY]
            try:
                with self.events_path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                    size = fh.tell()
                if size > EVENTS_ROTATE_BYTES:
                    os.replace(self.events_path,
                               self.dir / f"events-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
            except OSError:
                rec["persist_failed"] = True
            return rec

    def events(self, kind: str | None = None) -> list[dict]:
        with self._lock:
            return [e for e in self._events if kind is None or e.get("kind") == kind]

    # ── turns ───────────────────────────────────────────────────────────────
    @property
    def turn(self) -> int:
        with self._lock:
            return self._turn

    def next_turn(self) -> int:
        """Reserve the turn number of an accepted act."""
        with self._lock:
            self._turn += 1
            self.archive.set_turn(self._turn)
            try:
                tmp = self.dir / (STATE_NAME + ".tmp")
                tmp.write_text(json.dumps({"turn": self._turn}), encoding="utf-8")
                os.replace(tmp, self.dir / STATE_NAME)
            except OSError:
                pass                      # the archive and events still carry the turn
            return self._turn

    # ── positions ───────────────────────────────────────────────────────────
    def resolve_point(self, spec: Any) -> tuple[float, float, dict]:
        """``{x_nm, y_nm}`` or ``{frame, x, y}`` (display pixels of an archived scan) → scan nm.
        Same rules and messages as VISTA's host."""
        if not isinstance(spec, dict):
            raise ActionError("a position must be {x_nm, y_nm} or {frame, x, y}")
        if "x_nm" in spec or "y_nm" in spec:
            x, y = spec.get("x_nm"), spec.get("y_nm")
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y)):
                raise ActionError("x_nm and y_nm must both be numbers")
            return float(x), float(y), {"x_nm": float(x), "y_nm": float(y)}
        fid, x, y = spec.get("frame"), spec.get("x"), spec.get("y")
        if not isinstance(fid, str) or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                               for v in (x, y)):
            raise ActionError("a frame position needs frame (id), x and y (display pixels)")
        try:
            entry = self.archive.get(fid)
        except KeyError as exc:
            raise ActionError(str(exc).strip("'\"")) from None
        if entry.kind not in ("s", "p"):
            raise ActionError(f"{fid} is not a scan frame")
        from .frames import display_to_scan_nm
        X, Y = display_to_scan_nm(entry, float(x), float(y))
        return float(X), float(Y), {"frame": fid, "x": float(x), "y": float(y),
                                    "x_nm": round(float(X), 4), "y_nm": round(float(Y), 4)}

    # ── history tool ────────────────────────────────────────────────────────
    def history(self, args: dict) -> ToolReply:
        """VISTA's ``history`` tool: ``events`` (records by turn range) or ``attempts``."""
        args = dict(args or {})
        view = args.get("view", "events")
        if view == "attempts":
            return json_reply(self.attempts())
        if view != "events":
            return error_reply("view must be 'events' or 'attempts'")
        try:
            lo = int(args.get("start_turn", 0) or 0)
            hi = args.get("end_turn")
            hi = int(hi) if isinstance(hi, (int, float)) and not isinstance(hi, bool) else None
            limit = max(1, min(HISTORY_LIMIT_MAX, int(args.get("limit", 64) or 64)))
        except (TypeError, ValueError):
            return error_reply("start_turn, end_turn and limit must be integers")
        rows = [public_event(e) for e in self.events()
                if e.get("kind") in PUBLIC_KINDS and int(e.get("turn") or 0) >= lo
                and (hi is None or int(e.get("turn") or 0) <= hi)]
        return json_reply({"view": "events", "n": len(rows), "events": rows[-limit:],
                           "truncated": len(rows) > limit})

    def attempts(self) -> dict:
        by_action: dict[str, dict] = {}
        for e in self.events("act"):
            d = by_action.setdefault(str(e.get("action")), {"n": 0, "failed": 0})
            d["n"] += 1
            d["failed"] += 0 if e.get("ok") else 1
        frames = [e.fid for e in self.archive.frames(kind="s")]
        spectra = [e.fid for e in self.archive.frames(kind="d")]
        external = sum(1 for _ in self.events("external_frame"))
        return {"view": "attempts", "workspace": self.id, "turn": self.turn, "actions": by_action,
                "frames": frames[-40:], "n_frames": len(frames),
                "spectra": spectra[-40:], "n_spectra": len(spectra),
                "external_frames": external}

    def last_act(self) -> dict | None:
        acts = self.events("act")
        return public_event(acts[-1]) if acts else None


#: event kinds the model may read through ``history``
PUBLIC_KINDS = ("act", "external_frame", "detector_hit", "detector_disabled", "checkpoint")
_PUBLIC_KEYS = ("kind", "turn", "action", "skill", "params", "note", "ok", "error", "facts",
                "resolved", "new_frames", "state", "elapsed_s", "frame", "origin", "detector",
                "detector_id", "message", "suppressed", "reason")


def public_event(e: dict) -> dict:
    """An event as the model may read it (no step-level MAST internals, no tokens)."""
    return {k: e[k] for k in _PUBLIC_KEYS if k in e}


__all__ = ["GUIDE_SHARED_NAME", "PUBLIC_KINDS", "Workspace", "public_event", "workspace_id_for"]
