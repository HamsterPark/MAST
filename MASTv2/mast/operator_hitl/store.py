"""Persistent backends for operator prompts and the manual optical-state registry.

Where things live (``<root>`` is ``mast._runtime_paths.project_root()``, resolved when a
backend is built, never at import time, so ``MAST2_PROJECT_ROOT`` redirects it in tests):

==============================================  ==========================================
``<root>/artifacts/operator/prompts.json``      every prompt with its answer (bounded)
``<root>/artifacts/operator/optical_state.json``  the current slot values
``<root>/artifacts/operator/optical_state_history.jsonl``  one line per write, never rewritten
``<root>/config/optical_station.json``          slot definitions (optional; else DEFAULT_SLOTS)
==============================================  ==========================================

Rules every file here follows:

* written atomically (temp file in the same directory, ``os.replace``), UTF-8 **without** a
  BOM (``json.loads`` of a BOM-prefixed file fails, and the other loaders of this repo fall
  back to defaults silently when it does);
* read tolerant of a BOM (a hand-edited ``optical_station.json`` saved by Notepad or
  PowerShell 5.1 carries one), and **a file that cannot be read is never silent**: the
  loader logs a WARNING that names the file and the reason, then falls back (defaults /
  empty). An unreadable ``prompts.json`` is renamed ``*.corrupt-<time>`` first so the next
  save does not destroy the evidence.

Prompts left open when the service stopped are marked ``cancelled`` (reason "service
restarted") when the file is loaded: whoever asked them died with the process, and an answer
given afterwards would attest nothing to anybody.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mast.i18n import tr
from mast.operator_hitl.optical_state import (
    DEFAULT_SLOTS,
    OpticalSlot,
    OpticalStateError,
    OpticalStateStore,
    SlotValue,
    restored_valid_until,
)
from mast.operator_hitl.prompts import (
    MAX_CLOSED_KEPT,
    InMemoryPromptBackend,
    OperatorAnswer,
    OperatorPrompt,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PersistentPromptBackend", "PersistentOpticalStateStore", "load_slot_definitions",
    "operator_dir", "station_config_path", "atomic_write_json", "jsonable",
    "PROMPTS_FILE", "STATE_FILE", "HISTORY_FILE", "STATION_CONFIG_FILE",
]

PROMPTS_FILE = "prompts.json"
STATE_FILE = "optical_state.json"
HISTORY_FILE = "optical_state_history.jsonl"
STATION_CONFIG_FILE = "optical_station.json"

_ID_RE = re.compile(r"^op-(\d+)-")
#: ``os.replace`` onto a file somebody has open fails on Windows for a few ms (antivirus,
#: a reader): retry briefly before giving up
_REPLACE_RETRY_S = (0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.45)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ── paths ──────────────────────────────────────────────────────────────────────


def _root(root: str | os.PathLike | None) -> Path:
    if root is not None:
        return Path(root)
    from mast._runtime_paths import project_root

    return project_root()


def operator_dir(root: str | os.PathLike | None = None) -> Path:
    """``<root>/artifacts/operator`` (not created here)."""
    return _root(root) / "artifacts" / "operator"


def station_config_path(root: str | os.PathLike | None = None) -> Path:
    """``<root>/config/optical_station.json``."""
    return _root(root) / "config" / STATION_CONFIG_FILE


# ── JSON files ─────────────────────────────────────────────────────────────────


def jsonable(obj: Any, _depth: int = 0) -> Any:
    """*obj* as plain JSON data: unknown objects become ``str``, NaN / ±inf become
    ``None`` (strict JSON has neither), tuples become lists. Never raises."""
    if _depth > 10:
        return None
    if obj is None or isinstance(obj, (bool, str, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonable(v, _depth + 1) for v in obj]
    item = getattr(obj, "item", None)          # numpy scalars
    if callable(item):
        try:
            return jsonable(item(), _depth + 1)
        except Exception:  # noqa: BLE001
            pass
    return str(obj)


def _tmp_path(path: Path) -> Path:
    # never ".part-": the runtime sweeps those from its data roots at start-up
    return path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident():x}")


def atomic_write_json(path: Path, obj: Any) -> None:
    """Write *obj* to *path* atomically (UTF-8, no BOM, indented). Raises ``OSError``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(jsonable(obj), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    tmp = _tmp_path(path)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for delay in (*_REPLACE_RETRY_S, None):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if delay is None:
                    raise
                time.sleep(delay)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_json(path: Path, what: str) -> Any | None:
    """The parsed JSON in *path*; ``None`` if the file does not exist. A file that exists
    but cannot be read or parsed is a WARNING (naming file and reason) and ``None``."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        logger.warning("%s: cannot read %s (%s: %s); falling back", what, path,
                       type(exc).__name__, exc)
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        logger.warning("%s: %s is not valid JSON (%s); falling back", what, path, exc)
        return None


def _quarantine(path: Path) -> None:
    """Move an unreadable file aside so the next save does not overwrite it."""
    try:
        dest = path.with_name(f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        os.replace(path, dest)
        logger.warning("moved the unreadable %s to %s", path.name, dest)
    except OSError as exc:
        logger.warning("could not move the unreadable %s aside: %s", path, exc)


# ── prompts ────────────────────────────────────────────────────────────────────


class PersistentPromptBackend(InMemoryPromptBackend):
    """:class:`InMemoryPromptBackend` that saves ``prompts.json`` after every change.

    A save that fails is logged (WARNING) and exposed as :attr:`last_save_error`; it never
    blocks the operator: the prompt flow keeps working from memory. ``seq`` is persisted, so
    it keeps growing across restarts (a poller that compared "greater than" never stalls).
    """

    def __init__(self, root: str | os.PathLike | None = None, *,
                 max_closed: int = MAX_CLOSED_KEPT, **limits: Any) -> None:
        # ``limits``: max_open, max_open_external, default_lifetime_s (see InMemoryPromptBackend)
        super().__init__(max_closed=max_closed, **limits)
        self.directory = operator_dir(root)
        self.path = self.directory / PROMPTS_FILE
        self.last_save_error = ""
        self._load()

    # -- InMemoryPromptBackend hook ------------------------------------------------

    def _changed(self) -> None:
        super()._changed()
        self._save_locked()

    # -- file ------------------------------------------------------------------------

    def _save_locked(self) -> None:
        payload = {
            "version": 1, "seq": self._changes, "saved_at": _now(),
            "prompts": [{"prompt": p.to_dict(),
                         "answer": (self._answers[p.id].to_dict() if p.id in self._answers else None)}
                        for p in self._prompts.values()],
        }
        try:
            atomic_write_json(self.path, payload)
            self.last_save_error = ""
        except Exception as exc:  # noqa: BLE001 - never block the operator on a disk problem
            self.last_save_error = f"{type(exc).__name__}: {exc}"
            logger.warning("operator prompts could not be saved to %s: %s", self.path, exc)

    def _load(self) -> None:
        raw = _read_json(self.path, "operator prompts")
        if raw is None:
            if self.path.exists():              # unreadable: keep it as evidence
                _quarantine(self.path)
            return
        entries = raw.get("prompts") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            logger.warning("operator prompts: %s has no 'prompts' list; starting empty", self.path)
            _quarantine(self.path)
            return
        try:
            self._changes = max(0, int(raw.get("seq") or 0))
        except (TypeError, ValueError):
            self._changes = 0
        newest, skipped, restarted = 0, 0, 0
        for entry in entries:
            try:
                prompt = OperatorPrompt.from_dict(entry["prompt"])
                answer = (OperatorAnswer.from_dict(entry["answer"])
                          if isinstance(entry.get("answer"), dict) else None)
                if not prompt.id:
                    raise ValueError("a prompt without an id")
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                logger.warning("operator prompts: skipped an unreadable entry in %s (%s)",
                               self.path, exc)
                continue
            if prompt.status == "open":          # its asker died with the process
                prompt.status = "cancelled"
                answer = OperatorAnswer(prompt_id=prompt.id, status="cancelled",
                                        note=tr("服务重启", "service restarted"),
                                        source="system", answered_at=_now())
                restarted += 1
            self._prompts[prompt.id] = prompt
            if answer is not None:
                self._answers[prompt.id] = answer
            m = _ID_RE.match(prompt.id)
            if m:
                newest = max(newest, int(m.group(1)))
        self._ids = itertools.count(newest + 1)
        if restarted or skipped:
            self._changes += 1
            if restarted:
                logger.info("operator prompts: %d unanswered prompt(s) from the previous run "
                            "cancelled (service restarted)", restarted)
            self._trim_locked()
            self._save_locked()


# ── optical state ──────────────────────────────────────────────────────────────


def load_slot_definitions(path: Path) -> tuple[OpticalSlot, ...]:
    """The slots defined in *path* (``config/optical_station.json``), else
    :data:`DEFAULT_SLOTS`.

    Format: ``{"slots": [{id, label, kind, options, unit, min, max, valid_for_s,
    description}, ...], "extend_defaults": false}`` (a bare list works too). With
    ``extend_defaults`` true the built-in slots stay and the file overrides / adds by id.
    A missing file is normal (defaults); a file that exists but is unreadable or wrong is a
    WARNING that names the problem, and the defaults apply — never a half-applied file.
    """
    raw = _read_json(path, "optical-station config")
    if raw is None:
        return DEFAULT_SLOTS
    try:
        extend = False
        if isinstance(raw, dict):
            extend = bool(raw.get("extend_defaults", False))
            items = raw.get("slots")
        else:
            items = raw
        if not isinstance(items, list) or not items:
            raise ValueError("expected a non-empty list of slots under 'slots'")
        defined = [OpticalSlot.from_dict(item) for item in items]
        ids = [s.id for s in defined]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate slot ids in {sorted(i for i in ids if ids.count(i) > 1)}")
        for s in defined:
            if s.kind == "choice" and not s.options:
                raise ValueError(f"choice slot {s.id!r} has no options")
        if not extend:
            return tuple(defined)
        by_id = {s.id: s for s in DEFAULT_SLOTS}
        by_id.update({s.id: s for s in defined})
        return tuple(by_id.values())
    except Exception as exc:  # noqa: BLE001 - KeyError (no id), ValueError, OpticalStateError ...
        logger.warning("optical-station config %s is not usable (%s: %s); using the built-in "
                       "slots", path, type(exc).__name__, exc)
        return DEFAULT_SLOTS


class PersistentOpticalStateStore(OpticalStateStore):
    """:class:`OpticalStateStore` whose slots come from ``config/optical_station.json`` and
    whose values and history survive restarts.

    ``_persist`` runs *before* the base class commits a change to its memory: it appends the
    history line, then rewrites ``optical_state.json``. If the second step fails the history
    line is voided by a compensating line and the error propagates (the base class rolls the
    value back), so the file never claims an attestation that is not in force.
    """

    def __init__(self, root: str | os.PathLike | None = None, *,
                 slots: tuple[OpticalSlot, ...] | list[OpticalSlot] | None = None) -> None:
        self.directory = operator_dir(root)
        self.config_path = station_config_path(root)
        self.state_path = self.directory / STATE_FILE
        self.history_path = self.directory / HISTORY_FILE
        super().__init__(tuple(slots) if slots is not None else load_slot_definitions(self.config_path))
        self._load_values()

    # -- loading ---------------------------------------------------------------------

    def _load_values(self) -> None:
        self.history = self._read_history(500)
        raw = _read_json(self.state_path, "optical state")
        if raw is None:
            if self.state_path.exists():
                _quarantine(self.state_path)
            return
        values = raw.get("values") if isinstance(raw, dict) else None
        if not isinstance(values, dict):
            logger.warning("optical state: %s has no 'values' object; starting with no values",
                           self.state_path)
            _quarantine(self.state_path)
            return
        try:
            self._seq = max(0, int(raw.get("seq") or 0))
        except (TypeError, ValueError):
            self._seq = 0
        for slot_id, entry in values.items():
            try:
                slot = self.slot(slot_id)
            except OpticalStateError:
                logger.info("optical state: slot %r is no longer defined; its stored value is "
                            "ignored (the history keeps it)", slot_id)
                continue
            try:
                value = self._check(slot, entry["value"])
                # how long it lasts is not taken from the file's word: the earlier of its
                # valid_until and set_at + the slot's valid_for_s; an unreadable time = expired
                valid_until, problem = restored_valid_until(slot, entry.get("set_at"),
                                                            entry.get("valid_until"))
                if problem:
                    logger.warning("optical state: slot %r: %s; its validity is now %s", slot_id,
                                   problem, valid_until)
                self._values[slot_id] = SlotValue(
                    value=value, set_by=str(entry.get("set_by") or ""),
                    set_at=str(entry.get("set_at") or ""), source=str(entry.get("source") or ""),
                    valid_until=valid_until, note=str(entry.get("note") or ""),
                    client=entry.get("client") if isinstance(entry.get("client"), dict) else None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("optical state: the stored value of slot %r no longer fits its "
                               "definition (%s); ignored", slot_id, exc)

    # -- persisting ------------------------------------------------------------------

    def _append_history(self, rec: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(jsonable(rec), ensure_ascii=False, allow_nan=False)
        with open(self.history_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _write_values(self) -> None:
        atomic_write_json(self.state_path, {
            "version": 1,
            # the base class counts this change right after _persist returns
            "seq": self._seq + 1,
            "saved_at": _now(),
            "values": {sid: sv.to_dict() for sid, sv in self._values.items()},
        })

    def _persist(self, record: dict[str, Any]) -> None:
        self._append_history(record)
        try:
            self._write_values()
        except Exception as exc:
            try:
                self._append_history({
                    "slot": record.get("slot"), "value": None, "set_by": "system",
                    "set_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "source": "system:void", "valid_until": None,
                    "note": f"void: the record written at {record.get('set_at')} was not applied "
                            f"({type(exc).__name__}: {exc})"})
            except Exception:  # noqa: BLE001
                logger.error("optical state: could not void a history record that was not applied",
                             exc_info=True)
            raise

    # -- reading ---------------------------------------------------------------------

    def _read_history(self, limit: int) -> list[dict[str, Any]]:
        tail: deque[dict[str, Any]] = deque(maxlen=max(1, int(limit)))
        bad = 0
        try:
            with open(self.history_path, encoding="utf-8-sig") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    if isinstance(rec, dict):
                        tail.append(rec)
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            logger.warning("optical state: cannot read the history %s (%s: %s)",
                           self.history_path, type(exc).__name__, exc)
            return []
        if bad:
            logger.warning("optical state: %d unreadable line(s) skipped in %s", bad,
                           self.history_path)
        return list(tail)

    def history_tail(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return self._read_history(limit)
