"""Installing the persistent operator backends, and the views the API serves.

Service start-up calls :func:`install` once (``CoreRuntime`` / the station profile; this
package does not reach into the runtime itself). It

* builds :class:`~mast.operator_hitl.store.PersistentPromptBackend` and
  :class:`~mast.operator_hitl.store.PersistentOpticalStateStore` under the data root
  (``<root>/artifacts/operator``, slot definitions from ``<root>/config/optical_station.json``),
* makes them the active backends (``prompts.set_backend`` / ``optical_state.set_store``),
* hooks the notifications (below).

Nothing is created on disk until the first prompt or slot write, so installing it on an STM
station that never asks anything leaves no trace. :func:`uninstall` restores what was active
before (tests).

Notifications
~~~~~~~~~~~~~
Every post / answer / close of a prompt and every slot write publishes an
``EventType.OPERATOR_PROMPT`` event on the process EventBus, pointers only:

``{"what": "prompt", "event": "posted"|"answered"|"closed", "prompt_id", "status", "kind",
"severity", "seq"}`` and ``{"what": "optical_state", "event": "set"|"clear", "slot", "seq"}``.

The browser gets it over ``/ws/events`` (or ``/api/recent_events`` when WebSockets are blocked)
and refetches. The launcher polls ``GET /api/operator/prompts`` every ~2 s and compares ``seq``.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mast.operator_hitl import optical_state, prompts
from mast.operator_hitl.store import (
    PersistentOpticalStateStore,
    PersistentPromptBackend,
    jsonable,
    operator_dir,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Installed", "install", "uninstall", "installed", "request_view", "prompt_rows",
    "prompts_view", "optical_state_view", "optical_history",
]


@dataclass
class Installed:
    root: Path
    prompts: PersistentPromptBackend
    store: PersistentOpticalStateStore


_lock = threading.Lock()
_installed: Installed | None = None
#: what was active before the first install (restored by :func:`uninstall`)
_previous: tuple[Any, Any] | None = None


# ── notifications ──────────────────────────────────────────────────────────────


def _publish(**data: Any) -> None:
    try:
        from mast.core.events import EventBus

        EventBus.get().publish_operator_prompt(**data)
    except Exception:  # noqa: BLE001 - a notification must never break the operator flow
        logger.debug("operator event not published", exc_info=True)


def _on_prompt_event(event: str, prompt: prompts.OperatorPrompt) -> None:
    backend = prompts.get_backend()
    _publish(what="prompt", event=event, prompt_id=prompt.id, status=prompt.status,
             kind=prompt.kind, severity=prompt.severity, seq=int(getattr(backend, "seq", 0)))


def _on_slot_event(slot_id: str, record: dict | None) -> None:
    """An ``OpticalStateStore`` write listener: ``record`` is ``None`` for a clear."""
    store = optical_state.get_store()
    _publish(what="optical_state", event="clear" if record is None else "set", slot=slot_id,
             seq=int(getattr(store, "seq", 0)))


# ── install / uninstall ────────────────────────────────────────────────────────


def install(project_root: str | os.PathLike | None = None) -> Installed:
    """Make the persistent backends the active ones. Idempotent for the same root; a
    different root replaces the previous install. Returns what is installed.

    The store this installs is a new object: listeners somebody registered on the store it
    replaces (``OpticalStateStore.add_listener``) are not carried over. Register them on
    ``optical_state.get_store()`` after ``install()`` (and again if a later install replaces it)."""
    global _installed, _previous
    with _lock:
        root = Path(project_root) if project_root is not None else _default_root()
        if _installed is not None and _installed.root == root:
            if prompts.get_backend() is not _installed.prompts:
                prompts.set_backend(_installed.prompts)
            if optical_state.get_store() is not _installed.store:
                optical_state.set_store(_installed.store)
            return _installed
        if _installed is not None:
            _detach(_installed)
        backend = PersistentPromptBackend(root)
        store = PersistentOpticalStateStore(root)
        backend.listeners.append(_on_prompt_event)
        store.add_listener(_on_slot_event)
        old_backend = prompts.set_backend(backend)
        old_store = optical_state.set_store(store)
        if _previous is None:
            _previous = (old_backend, old_store)
        _installed = Installed(root=root, prompts=backend, store=store)
        logger.info("operator prompts and optical state: persistent store at %s",
                    operator_dir(root))
        return _installed


def _default_root() -> Path:
    from mast._runtime_paths import project_root

    return project_root()


def _detach(inst: Installed) -> None:
    try:
        inst.prompts.listeners.remove(_on_prompt_event)
    except ValueError:
        pass
    inst.store.remove_listener(_on_slot_event)


def uninstall() -> None:
    """Restore the backends that were active before :func:`install` (for tests)."""
    global _installed, _previous
    with _lock:
        if _installed is not None:
            _detach(_installed)
            if prompts.get_backend() is _installed.prompts and _previous is not None:
                prompts.set_backend(_previous[0])
            if optical_state.get_store() is _installed.store and _previous is not None:
                optical_state.set_store(_previous[1])
        _installed = None
        _previous = None


def installed() -> Installed | None:
    return _installed


# ── views (what the API, the ext surface and the wait hub read) ────────────────


def _status_for_requests(status: str) -> str:
    """The wishlist's vocabulary (``pending`` | ``done`` | ``dismissed``) for a prompt
    status, so everything that already reads operator requests understands a prompt."""
    return {"open": "pending", "answered": "done"}.get(status, "dismissed")


def request_view(prompt: prompts.OperatorPrompt, answer: prompts.OperatorAnswer | None = None) -> dict:
    """A prompt as an operator *request*: the wishlist record's keys (``id``, ``agent_id``,
    ``message``, ``kind``, ``status``, ``created_at``, ``resolved_at``, ``note``, ``path``)
    plus the structured ones. ``status`` is the wishlist's (``pending`` / ``done`` /
    ``dismissed``); ``prompt_status`` is the exact one (``open`` / ``answered`` / ``cancelled``
    / ``timeout`` / ``dismissed``). ``answer`` is ``None`` while the prompt is open."""
    view: dict[str, Any] = {
        "id": prompt.id, "structured": True,
        "agent_id": prompt.agent_id or "",
        "message": prompt.message or prompt.title, "title": prompt.title, "kind": prompt.kind,
        "options": [{"id": o.id, "label": o.label} for o in prompt.options],
        "number_min": prompt.number_min, "number_max": prompt.number_max, "unit": prompt.unit,
        "attest_slots": list(prompt.attest_slots), "severity": prompt.severity,
        "timeout_s": prompt.timeout_s,
        "experiment_id": (prompt.context or {}).get("experiment_id"),
        "job_id": prompt.job_id, "skill": prompt.skill,
        "status": _status_for_requests(prompt.status), "prompt_status": prompt.status,
        "created_at": prompt.created_at,
        "resolved_at": answer.answered_at if answer is not None else None,
        "note": answer.note if answer is not None else "",
        "path": "",
        "answer": None,
    }
    if answer is not None:
        view["answer"] = {"status": answer.status, "choice": answer.choice,
                          "value": jsonable(answer.value), "note": answer.note,
                          "answered_by": answer.answered_by, "source": answer.source,
                          "answered_at": answer.answered_at, "ok": answer.ok}
    return view


def _entries(status: str, limit: int) -> tuple[int, list[dict]]:
    backend = prompts.get_backend()
    snap = getattr(backend, "snapshot", None)
    if callable(snap):
        return snap(status, limit)
    # a custom backend without snapshot(): open prompts only
    entries = []
    for p in backend.list_open():
        entries.append({**p.to_dict(), "answer": None})
    return int(getattr(backend, "seq", 0)), entries[:limit]


def prompt_rows(limit: int = 500) -> list[dict]:
    """Every prompt as a wishlist-shaped request row (``request_view``); the wait hub's
    ``operator_reply`` condition reads these next to the wishlist's own requests."""
    _seq, entries = _entries("all", limit)
    rows = []
    for e in entries:
        ans = e.get("answer")
        rows.append(request_view(prompts.OperatorPrompt.from_dict(e),
                                 prompts.OperatorAnswer.from_dict(ans) if ans else None))
    return rows


def prompts_view(status: str = "open", limit: int = 100) -> dict:
    """What ``GET /api/operator/prompts`` returns."""
    seq, entries = _entries("open" if status == "open" else "all", limit)
    backend = prompts.get_backend()
    return {
        "seq": seq,
        "prompts": [jsonable(e) for e in entries],
        "open_count": sum(1 for e in entries if e.get("status") == "open"),
        "persistent": isinstance(backend, PersistentPromptBackend),
        "persist_error": getattr(backend, "last_save_error", "") or None,
    }


def optical_state_view() -> dict:
    """What ``GET /api/operator/optical-state`` returns."""
    store = optical_state.get_store()
    return {
        "seq": int(getattr(store, "seq", 0)),
        "slots": [s.to_dict() for s in store.slots()],
        # the operator sees where each value came from; sidecars and agents never do
        "values": jsonable(store.snapshot(include_client=True)),
        "persistent": isinstance(store, PersistentOpticalStateStore),
    }


def optical_history(limit: int = 200) -> list[dict]:
    """The newest *limit* optical-state writes, oldest first (persistent: from the file)."""
    store = optical_state.get_store()
    return [jsonable(r) for r in store.history_tail(limit)]
