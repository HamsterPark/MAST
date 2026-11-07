"""Structured operator prompts — the "a person must do or decide this" primitive.

Design contract: ``docs/v2/design/optical_station_witec.md`` §6.2 / D8.

Who may do what:

* any caller (skills, composite ``human`` nodes, ``/api/ext`` agents) may
  :func:`post` a prompt, :func:`get` / :func:`list_open` prompts, and block in
  :func:`ask` until it is answered, cancelled or times out;
* only **operator channels** may :func:`answer` / :func:`dismiss` — the operator web routes
  and the launcher popup. They are deliberately not exposed through ``/api/ext/v1`` or
  MCP (``api/ext/agent_tools.py`` OPERATOR_ONLY precedent: an agent must never
  attest a physical event it cannot see). ``tests/v2/unit/operator_hitl`` pins this by
  scanning for callers.

``kind`` / ``status`` / option ids are identity strings (never translated); only
``title`` / ``message`` / option labels are display text.

The backend is pluggable (:func:`set_backend`). The default
:class:`InMemoryPromptBackend` keeps prompts in process memory; the service swaps
in :class:`mast.operator_hitl.store.PersistentPromptBackend`, which also survives
restarts and notifies the UI and the launcher (``mast.operator_hitl.service.install``).

Change counter
~~~~~~~~~~~~~~
Every backend carries ``seq``: a counter that goes up on every post / answer / close
(including the lazy expiry of a prompt whose ``timeout_s`` ran out). The persistent
backend keeps it monotonic across restarts. ``GET /api/operator/prompts`` returns it, so
a poller (the launcher, every 2 s) can skip the refetch while it is unchanged.

Expiry
~~~~~~
A prompt with ``timeout_s`` expires on its own: the backend closes it with status
``timeout`` when the time is up, whether or not anybody is blocked in :func:`ask` on it
(an ``/api/ext`` prompt is only posted, never waited on server-side). The clock starts
when the prompt is posted. A prompt posted **without** ``timeout_s`` is given
:data:`DEFAULT_PROMPT_LIFETIME_S` (24 hours): nothing stays in the operator's list for ever
because its asker forgot it, and a wait on it (:func:`ask` with no limit) is bounded too.
``prompt.timeout_s`` then shows the lifetime it really has.

Capacity
~~~~~~~~
At most :data:`MAX_OPEN_PROMPTS` prompts are open at a time, of which at most
:data:`MAX_OPEN_EXTERNAL_PROMPTS` may belong to external agents (``agent_id`` ``ext:…``);
:func:`post` refuses the next one with :class:`TooManyOpenPrompts`. A per-agent limit alone is
no limit: an agent chooses its own name. The share left over keeps MAST's own prompts (a laser
attestation a protocol needs) from being crowded out by a flood from outside.

What an attest prompt may say
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
What the operator is shown to sign must be what the slots will hold, so :func:`vet` takes it
from the slot definitions, not from the asker: no options on a yes/no slot, only the slot's own
options (under the slot's own labels) on a choice slot, the slot's unit and range on a number
slot. See :func:`vet`.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "PROMPT_KINDS", "PROMPT_STATUSES", "SEVERITIES",
    "PromptOption", "OperatorPrompt", "OperatorAnswer",
    "PromptError", "PromptNotFound", "PromptClosed", "PromptAborted", "TooManyOpenPrompts",
    "PromptBackend", "InMemoryPromptBackend",
    "get_backend", "set_backend", "vet", "post", "get", "list_open", "ask", "answer", "dismiss",
    "cancel", "FakeOperator",
    "MAX_OPEN_PROMPTS", "MAX_OPEN_EXTERNAL_PROMPTS", "DEFAULT_PROMPT_LIFETIME_S",
]

PROMPT_KINDS = ("confirm", "choice", "number", "text", "attest")
PROMPT_STATUSES = ("open", "answered", "cancelled", "timeout", "dismissed")
SEVERITIES = ("info", "action", "safety")

#: closed prompts an in-memory / persistent backend keeps (oldest dropped first)
MAX_CLOSED_KEPT = 500
#: size bounds of one prompt (see ``OperatorPrompt.validate``)
MAX_TITLE_CHARS = 300
MAX_MESSAGE_CHARS = 8000
MAX_OPTIONS = 24
MAX_ATTEST_SLOTS = 20
MAX_CONTEXT_CHARS = 20_000
#: longest answer to a ``text`` prompt (longer is refused with a 422, never truncated)
MAX_TEXT_ANSWER_CHARS = 4000
#: open (unanswered) prompts, all callers together; the next :func:`post` is refused. The
#: operator's list, the persisted file and every poll grow with it
MAX_OPEN_PROMPTS = 100
#: of those, how many may belong to external agents (``agent_id`` starting ``ext:``); the rest
#: is kept for MAST's own prompts
MAX_OPEN_EXTERNAL_PROMPTS = 75
#: how long a prompt posted without ``timeout_s`` stays open (then it closes as ``timeout``)
DEFAULT_PROMPT_LIFETIME_S = 24 * 3600.0


class PromptError(ValueError):
    """Malformed prompt or answer (wrong kind, unknown option, out-of-range number)."""


class PromptNotFound(PromptError):
    """No prompt with that id (an operator route answers 404)."""


class PromptClosed(PromptError):
    """The prompt is no longer open: answered, cancelled, dismissed or timed out
    (an operator route answers 409)."""


class TooManyOpenPrompts(PromptError):
    """The operator already has the most open prompts it should (``scope`` ``total``: all
    callers together; ``external``: those of external agents). An ``/api/ext`` caller gets a
    429 ``too_many_open_prompts``."""

    def __init__(self, message: str, *, scope: str, open: int, limit: int) -> None:
        super().__init__(message)
        self.scope, self.open, self.limit = scope, open, limit


class PromptAborted(RuntimeError):
    """:func:`ask` was ended by the caller's ``check_abort`` (an operator stop, a job
    cancel). The prompt was cancelled before this was raised. A ``RuntimeError`` so code
    written against the first scaffold (``pytest.raises(RuntimeError)``) still works."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class PromptOption:
    id: str
    label: str


@dataclass
class OperatorPrompt:
    """What the operator is asked. ``attest_slots``: optical-state slot ids that an
    ``attest`` answer sets (value = ``answer.value`` or the chosen option id)."""

    kind: str
    title: str
    message: str = ""
    options: list[PromptOption] = field(default_factory=list)
    number_min: float | None = None
    number_max: float | None = None
    unit: str = ""
    attest_slots: list[str] = field(default_factory=list)
    severity: str = "action"
    job_id: str | None = None
    skill: str | None = None
    agent_id: str | None = None
    timeout_s: float | None = None
    context: dict[str, Any] = field(default_factory=dict)
    # filled in by the backend
    id: str = ""
    created_at: str = ""
    status: str = "open"

    def validate(self) -> None:
        if self.kind not in PROMPT_KINDS:
            raise PromptError(f"prompt kind {self.kind!r} not one of {PROMPT_KINDS}")
        if self.severity not in SEVERITIES:
            raise PromptError(f"severity {self.severity!r} not one of {SEVERITIES}")
        if not str(self.title or "").strip():
            raise PromptError("prompt title must not be empty")
        # bounds: the persistent store rewrites every prompt on every change, so a runaway
        # agent must not be able to post a megabyte of text
        if len(str(self.title)) > MAX_TITLE_CHARS:
            raise PromptError(f"prompt title is longer than {MAX_TITLE_CHARS} characters")
        if len(str(self.message or "")) > MAX_MESSAGE_CHARS:
            raise PromptError(f"prompt message is longer than {MAX_MESSAGE_CHARS} characters")
        if len(self.options) > MAX_OPTIONS:
            raise PromptError(f"a prompt has at most {MAX_OPTIONS} options")
        if len(self.attest_slots) > MAX_ATTEST_SLOTS:
            raise PromptError(f"a prompt attests at most {MAX_ATTEST_SLOTS} slots")
        if self.kind == "choice" and len(self.options) < 2:
            raise PromptError("a choice prompt needs at least two options")
        for o in self.options:
            if not str(o.id).strip():
                raise PromptError("option ids must not be empty")
            if len(str(o.id)) > 100 or len(str(o.label)) > MAX_TITLE_CHARS:
                raise PromptError("an option id is at most 100 characters and a label "
                                  f"at most {MAX_TITLE_CHARS}")
        ids = [o.id for o in self.options]
        if len(ids) != len(set(ids)):
            raise PromptError("option ids must be unique")
        if self.kind == "attest" and not self.attest_slots:
            raise PromptError("an attest prompt must name the optical-state slots it sets "
                              "(attest_slots)")
        if self.context:
            try:
                size = len(json.dumps(self.context, default=str))
            except (TypeError, ValueError):
                size = 0
            if size > MAX_CONTEXT_CHARS:
                raise PromptError(f"prompt context is larger than {MAX_CONTEXT_CHARS} characters")
        if (self.number_min is not None and self.number_max is not None
                and not self.number_min <= self.number_max):
            raise PromptError("number_min must be ≤ number_max")
        if self.timeout_s is not None:
            try:
                ok = float(self.timeout_s) > 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                raise PromptError("timeout_s must be a positive number of seconds")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["options"] = [asdict(o) for o in self.options]
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OperatorPrompt":
        raw = dict(raw)
        raw["options"] = [o if isinstance(o, PromptOption) else PromptOption(**o)
                          for o in raw.get("options") or []]
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class OperatorAnswer:
    """``source`` is what the answering channel *says* it is ("web" / "launcher" — self-reported
    by the client; "test" / "system" are set by MAST itself). ``client`` is what the server *saw*
    of the request that gave the answer (peer address, client identification, how it
    authenticated: ``mast/api/routes/operator.py`` ``client_record``) — the one an audit goes
    by. ``None`` for answers that did not come through an HTTP request. Never shown to
    external agents (``operator_hitl.service.request_view`` leaves it out)."""

    prompt_id: str
    status: str                       # answered | cancelled | timeout | dismissed
    choice: str | None = None         # option id (confirm: "yes" / "no")
    value: Any = None                 # number / text / attested value
    note: str = ""
    answered_by: str = ""
    source: str = ""                  # "web" | "launcher" (self-reported) | "test" | "system"
    answered_at: str = ""
    client: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        """Answered, and the answer is a yes: not a confirm answered no, not a cancel, and not an
        attestation recorded as ``False`` (the operator signed "NOT safe" - that is an answer, and
        it is not an approval; read ``value`` for what was recorded)."""
        return (self.status == "answered" and self.choice not in ("no", "cancel")
                and self.value is not False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OperatorAnswer":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in dict(raw).items() if k in known})


def check_answer(prompt: OperatorPrompt, *, choice: str | None, value: Any) -> tuple[str | None, Any]:
    """Validate an answer against its prompt; returns the normalised ``(choice, value)``."""
    if prompt.kind == "confirm":
        if choice not in ("yes", "no"):
            raise PromptError("a confirm prompt is answered with choice 'yes' or 'no'")
        return choice, None
    if prompt.kind == "choice" or (prompt.kind == "attest" and prompt.options):
        if choice not in {o.id for o in prompt.options}:
            raise PromptError(f"unknown option {choice!r}")
        return choice, (value if value is not None else choice)
    if prompt.kind == "number" or (prompt.kind == "attest" and prompt.unit):
        try:
            v = float(value)
        except (TypeError, ValueError) as exc:
            raise PromptError(f"a number is required, got {value!r}") from exc
        if v != v or v in (float("inf"), float("-inf")):
            raise PromptError(f"a finite number is required, got {value!r}")
        if prompt.number_min is not None and v < prompt.number_min:
            raise PromptError(f"{v} is below the minimum {prompt.number_min}")
        if prompt.number_max is not None and v > prompt.number_max:
            raise PromptError(f"{v} is above the maximum {prompt.number_max}")
        return choice, v
    if prompt.kind == "text":
        text = "" if value is None else str(value)
        if len(text) > MAX_TEXT_ANSWER_CHARS:
            # refused, not cut: an answer the operator did not write must not be recorded
            raise PromptError(f"the text is {len(text)} characters, at most "
                              f"{MAX_TEXT_ANSWER_CHARS} are accepted")
        return choice, text
    # attest without options/unit: a plain confirmation that sets the slots to `value`
    if value is None:
        raise PromptError("an attest answer needs a value")
    return choice, value


class PromptBackend(Protocol):
    def post(self, prompt: OperatorPrompt) -> str: ...
    def get(self, prompt_id: str) -> OperatorPrompt | None: ...
    def get_answer(self, prompt_id: str) -> OperatorAnswer | None: ...
    def list_open(self) -> list[OperatorPrompt]: ...
    def answer(self, prompt_id: str, ans: OperatorAnswer, *,
               before_commit: Callable[[], None] | None = None) -> OperatorAnswer: ...
    def close(self, prompt_id: str, status: str, reason: str = "", *,
              by: str = "", source: str = "system",
              client: dict[str, Any] | None = None) -> None: ...
    def wait(self, prompt_id: str, timeout_s: float) -> OperatorAnswer | None: ...


class InMemoryPromptBackend:
    """Thread-safe in-process store (default; tests; no service).

    Besides the :class:`PromptBackend` protocol it offers ``seq`` and :meth:`snapshot`
    (what ``GET /api/operator/prompts`` serves) and ``listeners``: callables
    ``fn(event, prompt)`` with ``event`` ∈ ``posted`` | ``answered`` | ``closed``
    (cancelled / dismissed / timed out), run on the thread that caused the change,
    after the backend's lock is released. A listener that raises is ignored.

    ``max_open`` / ``max_open_external`` bound the open prompts (see the module docstring);
    ``default_lifetime_s`` is the lifetime of a prompt posted without ``timeout_s``
    (``None`` = such a prompt never expires).
    """

    def __init__(self, *, max_closed: int = MAX_CLOSED_KEPT, max_open: int = MAX_OPEN_PROMPTS,
                 max_open_external: int = MAX_OPEN_EXTERNAL_PROMPTS,
                 default_lifetime_s: float | None = DEFAULT_PROMPT_LIFETIME_S) -> None:
        self._max_open = max(1, int(max_open))
        self._max_open_external = max(0, int(max_open_external))
        self._default_lifetime_s = None if default_lifetime_s is None else float(default_lifetime_s)
        self._cv = threading.Condition()
        self._prompts: dict[str, OperatorPrompt] = {}
        self._answers: dict[str, OperatorAnswer] = {}
        self._ids = itertools.count(1)
        self._changes = 0
        self._max_closed = max(1, int(max_closed))
        #: monotonic expiry instants of open prompts that carry ``timeout_s``
        self._deadlines: dict[str, float] = {}
        self._pending_events: list[tuple[str, OperatorPrompt]] = []
        #: called with ``(event, prompt)`` after a change (the service notifies the UI here)
        self.listeners: list[Callable[[str, OperatorPrompt], None]] = []

    # -- change counter --------------------------------------------------------

    @property
    def seq(self) -> int:
        return self._changes

    def _changed(self) -> None:
        """Called with the lock held after every state change. Subclasses persist here."""
        self._changes += 1

    def _next_id(self) -> str:
        return f"op-{next(self._ids):04d}-{uuid.uuid4().hex[:6]}"

    # -- events (fired outside the lock) ------------------------------------------

    def _queue_event(self, event: str, prompt: OperatorPrompt) -> None:
        self._pending_events.append((event, prompt))

    def _flush_events(self) -> None:
        with self._cv:
            events, self._pending_events = self._pending_events, []
        for event, prompt in events:
            for fn in list(self.listeners):
                try:
                    fn(event, prompt)
                except Exception:  # noqa: BLE001 - a listener must not break the operator flow
                    logger.debug("prompt listener failed", exc_info=True)

    # -- expiry ------------------------------------------------------------------

    def _close_locked(self, p: OperatorPrompt, status: str, reason: str, *,
                      by: str = "", source: str = "system",
                      client: dict[str, Any] | None = None) -> None:
        p.status = status
        self._deadlines.pop(p.id, None)
        self._answers[p.id] = OperatorAnswer(prompt_id=p.id, status=status, note=reason,
                                             answered_by=by, source=source, answered_at=_now(),
                                             client=dict(client) if client else None)
        self._changed()
        self._queue_event("closed", p)
        self._trim_locked()
        self._cv.notify_all()

    def _expire_locked(self) -> None:
        if not self._deadlines:
            return
        now = time.monotonic()
        for pid in [k for k, t in self._deadlines.items() if t <= now]:
            p = self._prompts.get(pid)
            if p is not None and p.status == "open":
                self._close_locked(p, "timeout", "no answer before the timeout")
            else:
                self._deadlines.pop(pid, None)

    def _sweep(self) -> None:
        with self._cv:
            self._expire_locked()
        self._flush_events()

    def _trim_locked(self) -> None:
        closed = [pid for pid, p in self._prompts.items() if p.status != "open"]
        for pid in closed[:max(0, len(closed) - self._max_closed)]:
            self._prompts.pop(pid, None)
            self._answers.pop(pid, None)

    # -- PromptBackend -----------------------------------------------------------

    def _admit_locked(self, prompt: OperatorPrompt) -> None:
        """Refuse the prompt if the operator already has too many open ones. The lock is held,
        so concurrent callers cannot slip past the limit together."""
        others = [p for p in self._prompts.values() if p.status == "open" and p.id != prompt.id]
        if len(others) >= self._max_open:
            raise TooManyOpenPrompts(
                f"the operator already has {len(others)} unanswered prompts (the limit is "
                f"{self._max_open}); wait until some are answered or cancel your own",
                scope="total", open=len(others), limit=self._max_open)
        if str(prompt.agent_id or "").startswith("ext:"):
            external = sum(1 for p in others if str(p.agent_id or "").startswith("ext:"))
            if external >= self._max_open_external:
                raise TooManyOpenPrompts(
                    f"external agents already have {external} unanswered prompts in total (the "
                    f"limit is {self._max_open_external}); wait until the operator has answered "
                    "some", scope="external", open=external, limit=self._max_open_external)

    def post(self, prompt: OperatorPrompt) -> str:
        prompt.validate()
        try:
            with self._cv:
                self._expire_locked()
                self._admit_locked(prompt)
                prompt.id = prompt.id or self._next_id()
                prompt.created_at = prompt.created_at or _now()
                prompt.status = "open"
                if prompt.timeout_s is None and self._default_lifetime_s is not None:
                    prompt.timeout_s = self._default_lifetime_s      # what it really has
                self._prompts[prompt.id] = prompt
                self._answers.pop(prompt.id, None)
                if prompt.timeout_s is not None:
                    self._deadlines[prompt.id] = time.monotonic() + float(prompt.timeout_s)
                self._changed()
                self._queue_event("posted", prompt)
                self._cv.notify_all()
        finally:
            self._flush_events()          # also when refused: the sweep above may have expired some
        return prompt.id

    def get(self, prompt_id: str) -> OperatorPrompt | None:
        self._sweep()
        with self._cv:
            return self._prompts.get(prompt_id)

    def get_answer(self, prompt_id: str) -> OperatorAnswer | None:
        self._sweep()
        with self._cv:
            return self._answers.get(prompt_id)

    def list_open(self) -> list[OperatorPrompt]:
        self._sweep()
        with self._cv:
            return [p for p in self._prompts.values() if p.status == "open"]

    def snapshot(self, status: str = "all", limit: int = 100) -> tuple[int, list[dict]]:
        """``(seq, entries)`` read under one lock. ``entries``: prompt dicts with an
        ``answer`` key (``None`` while open); open ones first (oldest first), then the
        closed ones newest first. ``status="open"`` keeps only the open ones."""
        self._sweep()
        with self._cv:
            seq = self._changes
            opened = [p for p in self._prompts.values() if p.status == "open"]
            closed = [] if status == "open" else [
                p for p in reversed(list(self._prompts.values())) if p.status != "open"]
            out = []
            for p in [*opened, *closed][:max(1, int(limit))]:
                ans = self._answers.get(p.id)
                out.append({**p.to_dict(), "answer": ans.to_dict() if ans is not None else None})
        return seq, out

    def answer(self, prompt_id: str, ans: OperatorAnswer, *,
               before_commit: Callable[[], None] | None = None) -> OperatorAnswer:
        """Record *ans* and close the prompt. ``before_commit`` runs under the lock, after
        the prompt was found open and before anything changes: if it raises, the prompt
        stays open (an ``attest`` answer sets its optical-state slots there, so the
        prompt is never answered without the state being recorded)."""
        try:
            with self._cv:
                self._expire_locked()
                p = self._prompts.get(prompt_id)
                if p is None:
                    raise PromptNotFound(f"no prompt {prompt_id!r}")
                if p.status != "open":
                    raise PromptClosed(f"prompt {prompt_id!r} is already {p.status}")
                if before_commit is not None:
                    before_commit()
                p.status = ans.status
                self._deadlines.pop(prompt_id, None)
                self._answers[prompt_id] = ans
                self._changed()
                self._queue_event("answered", p)
                self._trim_locked()
                self._cv.notify_all()
        finally:
            # also when the answer was refused: the refusal may have just expired a prompt,
            # and whoever watches should hear about that now, not at the next read
            self._flush_events()
        return ans

    def close(self, prompt_id: str, status: str, reason: str = "", *,
              by: str = "", source: str = "system",
              client: dict[str, Any] | None = None) -> None:
        with self._cv:
            self._expire_locked()
            p = self._prompts.get(prompt_id)
            if p is not None and p.status == "open":
                self._close_locked(p, status, reason, by=by, source=source, client=client)
        self._flush_events()

    def wait(self, prompt_id: str, timeout_s: float) -> OperatorAnswer | None:
        """The answer (any closing status) as soon as there is one; ``None`` after
        *timeout_s*. Wakes at the prompt's own expiry, so a ``timeout_s`` limit on the
        prompt is honoured even when nobody else touches the backend."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            with self._cv:
                self._expire_locked()
                found = self._answers.get(prompt_id)
                remaining = deadline - time.monotonic()
                if found is None and remaining > 0:
                    due = self._deadlines.get(prompt_id)
                    pause = remaining if due is None else min(
                        remaining, max(0.0, due - time.monotonic()) + 0.001)
                    self._cv.wait(timeout=pause)
            self._flush_events()
            if found is not None:
                return found
            if remaining <= 0:
                return None


_backend: Any = InMemoryPromptBackend()
_backend_lock = threading.Lock()


def get_backend():
    return _backend


def set_backend(backend) -> Any:
    """Swap the backend (service start-up; tests). Returns the previous one."""
    global _backend
    with _backend_lock:
        old, _backend = _backend, backend
    return old


def _slot_option_label(option: dict) -> str:
    return str(option.get("label") or option.get("id") or "")


def _vet_attest(prompt: OperatorPrompt) -> None:
    """An ``attest`` prompt asks the operator to sign a value into registry slots. What the
    operator is shown to sign must be what the slots will hold, and the asker (an agent can
    write any title, any label) must not be able to make those differ, so everything that
    decides it comes from the slot definitions:

    * the slots are defined, and all of one kind (one answer is written to all of them): a typo
      would otherwise surface as an error on the operator's click instead of at the asker;
    * a yes/no slot takes **no options** of the asker's: it is answered yes or no. An option
      ``{"id": "yes", "label": "NO - the area is not safe"}`` would make a click on "not safe"
      record True;
    * a choice slot takes options only from its own list, and they are shown under the slot's
      own labels - the asker's labels are dropped;
    * a number slot takes its unit and range from its definition (the asker's are replaced);
      options on it are numbers in range, labelled with that number and the unit; a text
      slot's options are labelled with the text they record;
    * a yes/no slot is a safety statement whoever asks and however they rate it: the prompt
      is shown as a safety prompt, never lowered.

    Changes *prompt* (option labels, unit, range, severity); raises :class:`PromptError`."""
    if prompt.kind != "attest":
        return
    from mast.operator_hitl import optical_state

    store = optical_state.get_store()
    defined = {s.id: s for s in store.slots()}
    unknown = [s for s in prompt.attest_slots if s not in defined]
    if unknown:
        raise PromptError(f"unknown optical-state slot(s) {unknown}; defined: {sorted(defined)}")
    slots = [defined[s] for s in dict.fromkeys(prompt.attest_slots)]
    ids = [s.id for s in slots]
    kinds = sorted({s.kind for s in slots})
    if len(kinds) > 1:
        raise PromptError("an attest prompt sets slots of one kind (one answer is written to all "
                          f"of them): {ids} are {kinds}; ask for one kind at a time")
    kind = kinds[0]
    if kind != "number" and (prompt.unit or prompt.number_min is not None
                             or prompt.number_max is not None):
        raise PromptError(f"unit, number_min and number_max belong to number slots; {ids} "
                          f"are {kind} slots")
    if kind == "flag":
        if prompt.options:
            raise PromptError(
                f"an attest prompt for the yes/no slot(s) {ids} cannot carry its own options: the "
                "operator answers yes or no, and an option the asker wrote could record the "
                "opposite of what its label says")
        prompt.severity = "safety"
    elif kind == "choice":
        lists = [{str(o.get("id")): _slot_option_label(o) for o in s.options} for s in slots]
        common = [oid for oid in lists[0] if all(oid in other for other in lists[1:])]
        for o in prompt.options:
            if o.id not in common:
                raise PromptError(f"option {o.id!r} is not one of the options of slot(s) {ids}; "
                                  f"they are {common}")
        prompt.options = [PromptOption(o.id, lists[0][o.id]) for o in prompt.options]
    elif kind == "number":
        units = {s.unit or "" for s in slots}
        if len(units) > 1:
            raise PromptError(f"the slots {ids} have different units {sorted(units)}; ask for "
                              "one at a time")
        unit = units.pop()
        lows = [s.min for s in slots if s.min is not None]
        highs = [s.max for s in slots if s.max is not None]
        lo, hi = (max(lows) if lows else None), (min(highs) if highs else None)
        prompt.unit, prompt.number_min, prompt.number_max = unit, lo, hi
        for o in prompt.options:
            try:
                v = float(o.id)
            except ValueError:
                raise PromptError(f"option {o.id!r} is not a number: the options of a prompt for "
                                  f"the number slot(s) {ids} are the numbers to sign") from None
            if not math.isfinite(v) or (lo is not None and v < lo) or (hi is not None and v > hi):
                raise PromptError(f"option {o.id!r} is outside the range [{lo}, {hi}] of the "
                                  f"slot(s) {ids}")
        prompt.options = [PromptOption(o.id, f"{o.id} {unit}".strip()) for o in prompt.options]
    else:                                           # text
        prompt.options = [PromptOption(o.id, o.id) for o in prompt.options]


def vet(prompt: OperatorPrompt) -> None:
    """Validate *prompt* and make it say only what the registry allows (:func:`_vet_attest`).
    Idempotent. Raises :class:`PromptError`. :func:`post` calls it; an entry point that has to
    compare or count prompts before posting one (the ``/api/ext`` route) calls it first."""
    prompt.validate()
    _vet_attest(prompt)


def post(prompt: OperatorPrompt) -> str:
    vet(prompt)
    return get_backend().post(prompt)


def get(prompt_id: str) -> OperatorPrompt | None:
    return get_backend().get(prompt_id)


def list_open() -> list[OperatorPrompt]:
    return list(get_backend().list_open())


def answer(prompt_id: str, *, by: str, source: str, choice: str | None = None,
           value: Any = None, note: str = "", client: dict[str, Any] | None = None) -> OperatorAnswer:
    """Record an operator's answer. **Operator channels only** (see module docstring).

    ``source`` is what the answering channel says it is ("web" / "launcher") — self-reported;
    ``client`` is what the server saw of the request (peer address, client identification, how
    it authenticated) and is what an audit goes by; both are stored with the answer.

    For an ``attest`` prompt the optical-state slots are set in the same step: the values
    are validated first, then written *before* the prompt is closed, under the prompt's
    lock, so a prompt is never answered while its attestation failed to be recorded (and
    a prompt that already timed out never sets the slots). The slots' listeners
    (``OpticalStateStore.add_listener``) are called once the prompt's lock is released.
    """
    if not str(by or "").strip():
        raise PromptError("answer needs the operator's name (by=)")
    backend = get_backend()
    p = backend.get(prompt_id)
    if p is None:
        raise PromptNotFound(f"no prompt {prompt_id!r}")
    if p.status != "open":
        raise PromptClosed(f"prompt {prompt_id!r} is already {p.status}")
    choice, value = check_answer(p, choice=choice, value=value)
    store = None
    if p.kind == "attest":
        from mast.operator_hitl import optical_state

        store = optical_state.get_store()
        try:
            # what the slots will hold: the answer records that, not the raw text that was sent
            # ("false", 0 and False are all a False), so ``value`` and ``ok`` say what was signed
            value = [store.validate_value(slot, value) for slot in p.attest_slots][0]
        except optical_state.OpticalStateError as exc:
            raise PromptError(f"attestation refused: {exc}") from exc
    ans = OperatorAnswer(prompt_id=prompt_id, status="answered", choice=choice, value=value,
                         note=str(note or "")[:2000], answered_by=str(by).strip(),
                         source=str(source), answered_at=_now(),
                         client=dict(client) if client else None)
    if store is None:
        return backend.answer(prompt_id, ans)

    def _record_state() -> None:
        for slot in p.attest_slots:
            store.set_by_operator(slot, ans.value, by=ans.answered_by,
                                  source=f"prompt:{prompt_id}", note=ans.note, client=client)

    # the slots are written while the backend holds its lock: their listeners are called after
    # it is released (a listener that switches a laser off must not stall the prompt list)
    defer = getattr(store, "deferred_notifications", None)
    with (defer() if callable(defer) else contextlib.nullcontext()):
        return backend.answer(prompt_id, ans, before_commit=_record_state)


def dismiss(prompt_id: str, *, by: str, source: str, reason: str = "",
            client: dict[str, Any] | None = None) -> None:
    """The operator closes a prompt without answering it (status ``dismissed``).
    **Operator channels only.** Nothing is attested; whoever waits gets ``dismissed``.
    ``source`` (self-reported) and ``client`` (seen by the server) as for :func:`answer`."""
    if not str(by or "").strip():
        raise PromptError("dismiss needs the operator's name (by=)")
    backend = get_backend()
    p = backend.get(prompt_id)
    if p is None:
        raise PromptNotFound(f"no prompt {prompt_id!r}")
    if p.status != "open":
        raise PromptClosed(f"prompt {prompt_id!r} is already {p.status}")
    backend.close(prompt_id, "dismissed", str(reason or "")[:2000], by=str(by).strip(),
                  source=str(source), client=client)


def cancel(prompt_id: str, reason: str = "", *, status: str = "cancelled") -> None:
    if status not in ("cancelled", "dismissed", "timeout"):
        raise PromptError(f"bad close status {status!r}")
    get_backend().close(prompt_id, status, reason)


def ask(prompt: OperatorPrompt, *, check_abort: Callable[[], object] | None = None,
        poll_s: float = 0.5, timeout_s: float | None = None,
        on_posted: Callable[[OperatorPrompt], None] | None = None) -> OperatorAnswer:
    """Post *prompt* and block until it is answered, cancelled, or times out.

    ``check_abort`` follows the instrument-layer contract (returns truthy **or**
    raises); on abort the prompt is cancelled and the exception (or
    :class:`PromptAborted`) propagates. ``timeout_s`` (or ``prompt.timeout_s``)
    ``None`` = wait until answered or aborted, but never longer than the prompt's
    :data:`DEFAULT_PROMPT_LIFETIME_S` — callers on job threads must pass a
    ``check_abort`` so an operator stop always ends the wait. The effective limit is
    written to ``prompt.timeout_s`` so the backend expires the prompt at the same moment
    and the UI can show the deadline. ``on_posted(prompt)`` runs once the prompt has its
    id and before the first wait (a job reports "waiting for the operator" there).
    If the asking thread dies for any other reason the prompt is cancelled too: an open
    prompt nobody waits for must not linger in the operator's list.
    """
    limit = timeout_s if timeout_s is not None else prompt.timeout_s
    if limit is not None:
        prompt.timeout_s = float(limit)
    pid = post(prompt)
    backend = get_backend()
    deadline = None if limit is None else time.monotonic() + float(limit)
    try:
        if on_posted is not None:
            try:
                on_posted(prompt)
            except Exception:  # noqa: BLE001 - reporting must not break the wait
                logger.debug("ask(): on_posted callback failed", exc_info=True)
        while True:
            try:
                aborted = bool(check_abort()) if check_abort is not None else False
            except Exception:
                cancel(pid, "aborted by the caller")
                raise
            if aborted:
                cancel(pid, "aborted by the caller")
                raise PromptAborted("operator prompt aborted")
            step = poll_s if deadline is None else max(0.0, min(poll_s, deadline - time.monotonic()))
            ans = backend.wait(pid, step)
            if ans is not None:
                return ans
            if deadline is not None and time.monotonic() >= deadline:
                cancel(pid, "no answer before the timeout", status="timeout")
                return backend.get_answer(pid) or OperatorAnswer(prompt_id=pid, status="timeout")
    except BaseException:
        cancel(pid, "the asker stopped waiting")
        raise


class FakeOperator:
    """Test helper: answers every new prompt from a policy, on a background thread.

    ``policy(prompt) -> dict(choice=..., value=..., note=...)`` or ``None`` to leave
    the prompt open. Use as a context manager.
    """

    def __init__(self, policy: Callable[[OperatorPrompt], dict | None] | None = None,
                 *, by: str = "test-operator", interval_s: float = 0.01):
        self.policy = policy or (lambda p: {"choice": "yes"} if p.kind == "confirm" else
                                 {"choice": p.options[0].id} if p.options else {"value": 0})
        self.by = by
        self.interval_s = interval_s
        self.seen: list[OperatorPrompt] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "FakeOperator":
        self._thread = threading.Thread(target=self._run, daemon=True, name="fake-operator")
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        handled: set[str] = set()
        while not self._stop.is_set():
            for p in list_open():
                if p.id in handled:
                    continue
                handled.add(p.id)
                self.seen.append(p)
                reply = self.policy(p)
                if reply is not None:
                    try:
                        answer(p.id, by=self.by, source="test", **reply)
                    except Exception:  # noqa: BLE001 - surface through the waiting ask()
                        cancel(p.id, "fake operator could not answer", status="dismissed")
            self._stop.wait(self.interval_s)


def _iter_prompts(items: Iterable[OperatorPrompt]) -> list[dict]:  # pragma: no cover
    return [p.to_dict() for p in items]
