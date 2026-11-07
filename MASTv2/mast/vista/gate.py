"""One act, one observation: the instrument-wide observation token.

README
------
VISTA's rule is that an action executes only after the previous action's result reached the
model. Inside VISTA-SPM the runner acknowledged each delivered result; an MCP server cannot
see delivery, so MAST enforces the rule with a token instead (design contract
``docs/v2/architecture/mast7_vista.md`` §2):

* every observation the server hands out carries a fresh random token;
* the next act must quote it (``after``); quoting consumes it;
* there is exactly **one** live token per MAST process, because there is one instrument, and
  it is bound to the client session it was delivered to;
* a frame the gate did not cause (UI, operator, built-in agents, another client) invalidates
  the token, so the next act is refused together with the newest observation.

Codex "Code Mode" models call tools from JavaScript and can chain calls inside one code cell
without looking at a result in between. Every call from one cell carries the same
``_meta.itemId``; the gate accepts at most one act per item id.

This is a protocol against acting without looking, not a security boundary: any process on
the machine can call MAST's HTTP API. Safety stays with MAST's safety gates.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

#: Unambiguous characters only (no 0/O, 1/I/L, 2/Z, 5/S, 8/B), so a token read off an image
#: in strict mode is not misread.
ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"
TOKEN_CHARS = 8

#: Refusal codes, in the order the rules are checked.
SESSION_CLOSED = "session_closed"
ACT_IN_FLIGHT = "act_in_flight"
SAME_CODE_CELL = "same_code_cell"
CHECKPOINT_REQUESTED = "checkpoint_requested"
OBSERVE_FIRST = "observe_first"

REFUSAL_TEXT = {
    SESSION_CLOSED: ("This session is closed (stopped by the operator, or its unattended run ended). "
                     "No action was executed; finish with a short summary and do not act again."),
    ACT_IN_FLIGHT: ("Another action is still executing. No action was executed. Wait for it with "
                    "mast_wait (condition type job) and look at its observation first."),
    SAME_CODE_CELL: ("One action per code cell: an action from this code cell was already executed. "
                     "Nothing was executed now. End this cell, look at the previous action's "
                     "observation (call image() on its image), then act in a new cell with the "
                     "token from that observation."),
    CHECKPOINT_REQUESTED: ("A checkpoint was requested: update WORKING.md with mast_write_working "
                           "first. No action was executed."),
    OBSERVE_FIRST: ("Look at the newest observation before acting: the token you sent is missing, "
                    "already used, or older than the newest observation. No action was executed. "
                    "The newest observation and a fresh token are attached; look at it, then act "
                    "with that token."),
}


def new_token() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_CHARS))
    return f"{raw[:4]}-{raw[4:]}"


def normalise_token(value: Any) -> str:
    """Accept the token as the model may echo it: any case, with or without the dash or spaces.
    Anything that is not 8 token characters becomes ``""`` (never matches)."""
    chars = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    if len(chars) != TOKEN_CHARS or any(ch not in ALPHABET for ch in chars):
        return ""
    return f"{chars[:4]}-{chars[4:]}"


@dataclass(frozen=True)
class Refusal:
    code: str
    detail: str
    job_id: str | None = None

    @property
    def recover(self) -> bool:
        """The refusal carries the newest observation and a fresh token."""
        return self.code == OBSERVE_FIRST


@dataclass(frozen=True)
class Grant:
    """An accepted act. ``ticket`` identifies it until :meth:`ObservationGate.complete`."""

    ticket: str
    session: str
    item_id: str | None
    consumed: tuple[str | None, str | None, str | None]   # (token, session, obs) before the act


class ObservationGate:
    """Thread-safe token state (see the module docstring). Never raises on normal use."""

    def __init__(self, *, item_memory: int = 512, clock=time.time) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._token: str | None = None
        self._token_session: str | None = None
        self._token_obs: str | None = None
        self._minted_at: float | None = None
        self._in_flight: dict | None = None
        self._items: OrderedDict[str, str] = OrderedDict()
        self._item_memory = int(item_memory)
        self.invalidations = 0
        self.last_invalidation: dict | None = None

    # ── minting / invalidation ──────────────────────────────────────────────
    def mint(self, session: str, obs_id: str) -> str:
        """A fresh token for an observation delivered to ``session``; any older token dies."""
        with self._lock:
            return self._mint_locked(session, obs_id)

    def _mint_locked(self, session: str, obs_id: str) -> str:
        self._token = new_token()
        self._token_session = str(session or "")
        self._token_obs = str(obs_id or "")
        self._minted_at = self._clock()
        return self._token

    def invalidate(self, reason: str) -> bool:
        """Kill the live token (an outside frame arrived). True if a token was live."""
        with self._lock:
            live = self._token is not None
            self._token = None
            self._token_session = None
            self._token_obs = None
            self.invalidations += 1
            self.last_invalidation = {"reason": str(reason)[:200], "t": self._clock(), "had_token": live}
            return live

    def is_live(self, token: str | None) -> bool:
        with self._lock:
            return bool(token) and token == self._token

    def token_for(self, session: str) -> str | None:
        """The live token if it is bound to ``session`` (used to re-render an observation)."""
        with self._lock:
            return self._token if self._token and self._token_session == session else None

    # ── acts ────────────────────────────────────────────────────────────────
    def acquire(self, *, session: str, after: Any, item_id: str | None = None,
                checkpoint: bool = False, closed: bool = False) -> Grant | Refusal:
        """Check and consume atomically. Order: closed, in flight, same code cell, checkpoint,
        token. A refusal changes nothing."""
        session = str(session or "")
        item = str(item_id).strip()[:200] if item_id else None
        token = normalise_token(after)
        with self._lock:
            if closed:
                return Refusal(SESSION_CLOSED, REFUSAL_TEXT[SESSION_CLOSED])
            if self._in_flight is not None:
                return Refusal(ACT_IN_FLIGHT, REFUSAL_TEXT[ACT_IN_FLIGHT],
                               job_id=self._in_flight.get("job_id"))
            if item and item in self._items:
                return Refusal(SAME_CODE_CELL, REFUSAL_TEXT[SAME_CODE_CELL])
            if checkpoint:
                return Refusal(CHECKPOINT_REQUESTED, REFUSAL_TEXT[CHECKPOINT_REQUESTED])
            if not token or token != self._token or self._token_session != session:
                return Refusal(OBSERVE_FIRST, REFUSAL_TEXT[OBSERVE_FIRST])
            consumed = (self._token, self._token_session, self._token_obs)
            self._token = self._token_session = self._token_obs = None
            ticket = secrets.token_hex(8)
            self._in_flight = {"ticket": ticket, "session": session, "job_id": None,
                               "since": self._clock(), "item_id": item}
            if item:
                self._items[item] = ticket
                while len(self._items) > self._item_memory:
                    self._items.popitem(last=False)
            return Grant(ticket=ticket, session=session, item_id=item, consumed=consumed)

    def bind_job(self, ticket: str, job_id: str) -> None:
        with self._lock:
            if self._in_flight is not None and self._in_flight.get("ticket") == ticket:
                self._in_flight["job_id"] = str(job_id)

    def complete(self, ticket: str, session: str, obs_id: str) -> str:
        """The act finished and produced observation ``obs_id``: clear the in-flight mark and
        mint the next token in one step."""
        with self._lock:
            if self._in_flight is not None and self._in_flight.get("ticket") == ticket:
                self._in_flight = None
            return self._mint_locked(session, obs_id)

    def abort(self, grant: Grant) -> None:
        """The act never started (submission failed): give the consumed token back."""
        with self._lock:
            if self._in_flight is not None and self._in_flight.get("ticket") == grant.ticket:
                self._in_flight = None
            if grant.item_id and self._items.get(grant.item_id) == grant.ticket:
                self._items.pop(grant.item_id, None)
            if self._token is None:
                self._token, self._token_session, self._token_obs = grant.consumed

    def release(self, ticket: str) -> None:
        """Clear the in-flight mark without minting (the act crashed before an observation)."""
        with self._lock:
            if self._in_flight is not None and self._in_flight.get("ticket") == ticket:
                self._in_flight = None

    def in_flight(self) -> dict | None:
        with self._lock:
            return dict(self._in_flight) if self._in_flight else None

    def status(self) -> dict:
        """For status pages and briefings. Never contains the token itself."""
        with self._lock:
            return {"token_live": self._token is not None,
                    "token_session": self._token_session,
                    "token_observation": self._token_obs,
                    "token_age_s": (None if self._minted_at is None or self._token is None
                                    else round(self._clock() - self._minted_at, 1)),
                    "act_in_flight": ({k: v for k, v in self._in_flight.items() if k != "ticket"}
                                      if self._in_flight else None),
                    "invalidations": self.invalidations,
                    "last_invalidation": self.last_invalidation}


__all__ = ["ACT_IN_FLIGHT", "CHECKPOINT_REQUESTED", "Grant", "OBSERVE_FIRST", "ObservationGate",
           "REFUSAL_TEXT", "Refusal", "SAME_CODE_CELL", "SESSION_CLOSED", "ALPHABET",
           "new_token", "normalise_token"]
