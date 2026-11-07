"""Server-side wait: one call that returns when something the agent cares about happened.

README
------
Design contract ``docs/v2/architecture/mast7_vista.md`` §6. An external agent acts
asynchronously (an act may run for minutes) and then calls ``mast_wait``; the wait happens here,
in the server, instead of in a polling loop that burns the agent's turns.

Conditions (any of them fires; all that fire in the same check are returned together):

=================  =================  ===================================================
type               fields             fires when
=================  =================  ===================================================
``job``            ``job_id``         the job (plain or act) is terminal; an unknown job
                                      fires at once with an ``error`` (never a silent hang)
``frame``          --                 a frame was archived after the baseline
``alert``          ``min_level``      an alert at or above ``info|warning|critical``
                                      (default ``warning``) after the baseline
``operator_reply`` ``request_id?``    the operator answered (that) request; a named request
                                      that is already answered fires at once
``detector``       ``detector_id?``   a detector event (hit, or auto-disable) after the baseline
``timer``          ``seconds``        that much time passed since the call started
=================  =================  ===================================================

Events and cursors
~~~~~~~~~~~~~~~~~~
``publish(kind, event)`` records an event in a per-kind ring buffer (``RING_SIZE``) under one
global, monotonically increasing ``seq``. ``cursor()`` is an opaque string encoding this hub's
epoch (random per process), the current ``seq`` and the wall time. The baseline of a wait is

* ``since`` given (a cursor of this hub): events with ``seq`` after the cursor fire at once;
* ``since`` omitted: only events published after the call started;
* ``since`` from another server run (epoch differs): events newer than the cursor's wall time,
  plus a ``gap`` entry.

Every answer carries a fresh ``cursor``; passing it back as ``since`` never misses an event
between two waits. When events after the cursor were already evicted from a ring, the answer
contains ``{"index": null, "type": "gap", ...}`` instead of silently missing them, and returns
at once: the agent should re-read the state (observe / history) rather than trust the list.

Age and backlog
~~~~~~~~~~~~~~~
A wait that resumes from a cursor can return at once with an event recorded minutes earlier
(e.g. a ``jump_burst`` alert from the end of the previous scan woke the next wait
"after 0 s", and nothing in it said it was old). Every recorded event (frame, detector, alert,
operator_reply) therefore comes back with ``age_s`` (server now minus when the hub recorded
it) and ``backlog`` (true when it was recorded before this wait call started). A job hit
carries the same two keys from the job's ``finished_at``; an operator request that was
already answered when the wait started is ``backlog: true``. They sit next to ``event``, not
inside it: the event is what happened, these two say when, relative to this wait.

Alerts
~~~~~~
:meth:`WaitHub.attach_event_bus` subscribes to MAST's ``EventBus`` (subscribers run on the
publisher's thread, so the callback only maps and enqueues). :func:`alert_from_bus_event` maps:

* ``ANOMALY`` (``core/watchdog.py``: the current stayed on the rail, emergency retract) ->
  ``critical``. Payload ``threshold_a`` and ``readings`` (summarised to ``max_reading_a``).
* ``CURRENT_MONITOR`` (``monitoring/service.py``): ``kind == "alert"`` -> its ``level``
  (``critical`` -> critical, ``warn`` -> warning); ``kind == "status"`` (monitor state
  transitions, rare) -> ``warning`` for ``comms_down`` (TCP circuit open), else ``info``;
  ``kind == "segment"`` (~1 Hz) and ``"aux"`` (5 Hz) are telemetry, never alerts (they would
  flood the ring; a confirmed problem in them is published as ``kind == "alert"`` anyway).
  A monitor alert carries, when the monitor sent them, ``segment_start`` / ``segment_end``
  (UTC ISO: the ~1 s of current the rule judged), ``measured`` (the segment's numbers: spike
  sigma, jump rate, largest step and mean in pA, ...) and ``context`` (what the instrument was
  doing: scanning or not, seconds since the last scanning segment, the skill holding the
  instrument, the last skill seen, bias, setpoint). The monitor only measures; deciding whether
  an alert is the tip or the tail of the agent's own scan is the agent's call, and these keys
  are what it needs for it.
* ``CONDUCT_ALERT`` (``conduct/adapters.py``): ``severity`` ``crit`` -> critical, ``warn`` ->
  warning, ``info`` (and anything else) -> info.

Operator replies
~~~~~~~~~~~~~~~~
The ext API's ``/requests`` live in ``mast.wishlist``; an answer sets the request's ``status``
(``pending`` -> ``done`` | ``dismissed``) plus ``note``, ``path`` and ``resolved_at``, with no
event. Structured operator prompts (``kind`` confirm / choice / number / text / attest, see
``mast.operator_hitl``) are read through the same lookup, as rows of the same shape
(``operator_hitl.service.request_view``): ``pending`` while open, ``done`` once answered,
``dismissed`` when dismissed, cancelled or timed out; their event adds ``structured``,
``prompt_status``, ``choice``, ``value``, ``answered_by`` and ``ok``. ``wishlist_lookup`` is a callable returning the current request records
(:func:`default_wishlist_lookup` reads MAST's board; ``None`` disables polling). The hub keeps a
snapshot; :meth:`poll_wishlist` publishes an ``operator_reply`` event for every request that
became answered (or was answered again) since the previous snapshot. It runs when the bus is
attached (the first snapshot), at the start of every wait, and every ``WISHLIST_POLL_S`` during
a wait that has an ``operator_reply`` condition. Answers are therefore timestamped when the hub
noticed them, never earlier: a wait may report an answer once more than strictly needed, never
miss one.

Waiting
~~~~~~~
:meth:`WaitHub.wait` is a coroutine: it checks, then sleeps on an ``asyncio.Event`` that
:meth:`publish` sets from any thread (``call_soon_threadsafe``), with a ``TICK_S`` tick for the
state-based conditions (job, timer). It is bounded by ``timeout_s`` (at most ``MAX_WAIT_S``) and
ends early, without an answer anyone reads, when the optional ``disconnected`` callable reports
that the client went away.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

#: event kinds a hub records (``publish``); ``job`` and ``timer`` are state-based
KINDS = ("frame", "detector", "alert", "operator_reply")
CONDITION_TYPES = ("job", "frame", "alert", "operator_reply", "detector", "timer")
#: events kept per kind
RING_SIZE = 500
#: server cap of one wait (clients set their MCP tool timeout to >= 900 s)
MAX_WAIT_S = 840.0
DEFAULT_WAIT_S = 600.0
MAX_CONDITIONS = 16
#: longest sleep between two checks of the state-based conditions
TICK_S = 0.25
WISHLIST_POLL_S = 2.0
DISCONNECT_POLL_S = 1.0
#: entries in one answer (the newest are kept; ``omitted`` counts the rest)
MAX_FIRED = 64
MAX_FIRED_PER_CONDITION = 20

LEVELS = ("info", "warning", "critical")
_LEVEL_ALIASES = {"info": "info", "warning": "warning", "warn": "warning",
                  "critical": "critical", "crit": "critical"}
#: wishlist request states that mean "the operator answered"
ANSWERED = ("done", "dismissed")

_FIELDS: dict[str, tuple[str, ...]] = {
    "job": ("job_id",), "frame": (), "alert": ("min_level",), "operator_reply": ("request_id",),
    "detector": ("detector_id",), "timer": ("seconds",),
}
_CURSOR_RE = re.compile(r"^c1\.([0-9a-f]{8})\.(\d{1,18})\.(\d{1,16})$")
_GAP_HINT = ("events after your cursor were dropped from the server's buffer (it keeps the "
             "last {n} per kind); re-read the state (observe / history) instead of relying on "
             "this event list")


class ConditionError(ValueError):
    """A condition list (or timeout) the hub cannot wait on; the route answers 422."""


class CursorError(ValueError):
    """A ``since`` this hub cannot read; the route answers 422."""


# ── conditions ───────────────────────────────────────────────────────────────
def _short_str(v: Any, what: str, limit: int = 128) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ConditionError(f"{what} must be a non-empty string")
    if len(v) > limit:
        raise ConditionError(f"{what} is longer than {limit} characters")
    return v.strip()


def parse_conditions(conditions: Any) -> list[dict]:
    """Validate and normalise a condition list. Raises :class:`ConditionError` with a message
    the agent can act on (unknown type, unknown field, missing or malformed value)."""
    if not isinstance(conditions, (list, tuple)) or not conditions:
        raise ConditionError("conditions must be a non-empty list, e.g. "
                             "[{\"type\": \"job\", \"job_id\": \"j_...\"}, {\"type\": \"frame\"}]")
    if len(conditions) > MAX_CONDITIONS:
        raise ConditionError(f"at most {MAX_CONDITIONS} conditions per wait")
    out: list[dict] = []
    for i, c in enumerate(conditions):
        if not isinstance(c, dict):
            raise ConditionError(f"condition {i} is not an object")
        t = c.get("type")
        if not isinstance(t, str) or t not in _FIELDS:
            raise ConditionError(f"condition {i}: type {t!r} is not one of {list(CONDITION_TYPES)}")
        extra = sorted(set(c) - {"type", *_FIELDS[t]})
        if extra:
            allowed = list(_FIELDS[t]) or "no fields"
            raise ConditionError(f"condition {i} ({t}): unknown field(s) {extra}; allowed: {allowed}")
        n: dict[str, Any] = {"type": t}
        if t == "job":
            if "job_id" not in c:
                raise ConditionError(f"condition {i} (job): job_id is required")
            n["job_id"] = _short_str(c["job_id"], f"condition {i} (job): job_id")
        elif t == "alert":
            raw = c.get("min_level", "warning")
            level = _LEVEL_ALIASES.get(str(raw).strip().lower()) if isinstance(raw, str) else None
            if level is None:
                raise ConditionError(f"condition {i} (alert): min_level {raw!r} is not one of "
                                     f"{list(LEVELS)}")
            n["min_level"] = level
        elif t in ("operator_reply", "detector"):
            key = "request_id" if t == "operator_reply" else "detector_id"
            v = c.get(key)
            n[key] = None if v is None or v == "" else _short_str(v, f"condition {i} ({t}): {key}")
        elif t == "timer":
            s = c.get("seconds")
            if isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(s) \
                    or s < 0 or s > MAX_WAIT_S:
                raise ConditionError(f"condition {i} (timer): seconds must be a number within "
                                     f"0..{MAX_WAIT_S:g}")
            n["seconds"] = float(s)
        out.append(n)
    return out


def _level_rank(level: Any) -> int:
    return LEVELS.index(_LEVEL_ALIASES.get(str(level or "").lower(), "info"))


# ── EventBus -> alert ────────────────────────────────────────────────────────
def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _iso(epoch: Any) -> str | None:
    """UTC ISO 8601 with milliseconds (the jobs' ``created_at`` style, plus ms), or None."""
    t = _num(epoch)
    if t is None:
        return None
    try:
        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return None


def _epoch_of(iso: Any) -> float | None:
    """Epoch seconds of an ISO 8601 string (naive = UTC), or None when unreadable."""
    if not isinstance(iso, str) or not iso.strip():
        return None
    try:
        dt = datetime.fromisoformat(iso.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


#: what the current monitor measured in the segment it judged (same names on the bus and here)
_MONITOR_MEASURED = ("spike_sigma", "jump_rate_hz", "max_step_pa", "mean_pa", "rms_pa",
                     "rtn_score", "rtn_gap_pa", "rtn_rate_hz")
#: what the instrument was doing during that segment (bus key -> answer key)
_MONITOR_CONTEXT = {"ctx_scanning": "scanning", "scanning_s_ago": "scanning_s_ago",
                    "ctx_zctrl_on": "zctrl_on", "ctx_skill": "skill",
                    "ctx_afterglow_skill": "afterglow_skill", "last_skill": "last_skill",
                    "last_skill_s_ago": "last_skill_s_ago", "ctx_bias_v": "bias_v",
                    "ctx_setpoint_pa": "setpoint_pa", "scan_id": "scan_id"}


def _monitor_alert_extras(data: dict) -> dict:
    """``segment_start`` / ``segment_end`` / ``measured`` / ``context`` of a current-monitor
    alert, from the scalars the monitor put on the bus (absent keys stay absent)."""
    out: dict = {}
    start, end = _iso(data.get("seg_t_start")), _iso(data.get("seg_t_end"))
    if start:
        out["segment_start"] = start
    if end:
        out["segment_end"] = end
    measured = {k: round(v, 3) for k in _MONITOR_MEASURED if (v := _num(data.get(k))) is not None}
    if measured:
        out["measured"] = measured
    context: dict = {}
    for src, dst in _MONITOR_CONTEXT.items():
        v = data.get(src)
        if isinstance(v, bool):
            context[dst] = v
        elif isinstance(v, str):
            if v:
                context[dst] = v
        elif (f := _num(v)) is not None:
            context[dst] = bool(f) if src in ("ctx_scanning", "ctx_zctrl_on") else round(f, 3)
    if context:
        out["context"] = context
    return out


def alert_from_bus_event(event: Any) -> dict | None:
    """Map one MAST ``EventBus`` event to an alert record, or ``None`` when it is not an alert
    (see the module docstring for the mapping). Never raises on a malformed payload."""
    etype = getattr(getattr(event, "type", None), "value", None)
    data = getattr(event, "data", None)
    data = data if isinstance(data, dict) else {}
    t = _num(getattr(event, "timestamp", None)) or time.time()
    if etype == "anomaly":
        readings = [abs(v) for v in (_num(x) for x in (data.get("readings") or [])) if v is not None]
        return {"level": "critical", "source": "watchdog", "code": "current_rail",
                "message": ("safety watchdog: the tunnelling current stayed above the threshold; "
                            "emergency retract triggered"),
                "threshold_a": _num(data.get("threshold_a")),
                "max_reading_a": max(readings) if readings else None, "t": t}
    if etype == "current_monitor":
        kind = data.get("kind")
        if kind == "alert":
            level = {"critical": "critical", "warn": "warning", "warning": "warning"}.get(
                str(data.get("level") or "").lower(), "info")
            return {"level": level, "source": "current_monitor", "code": str(data.get("rule") or ""),
                    "message": str(data.get("summary_zh") or ""), "alert_id": data.get("alert_id"),
                    "segment_id": data.get("seg_id"), "t": t, **_monitor_alert_extras(data)}
        if kind == "status":
            state = str(data.get("state") or "")
            return {"level": "warning" if state == "comms_down" else "info",
                    "source": "current_monitor", "code": f"monitor_{state or 'unknown'}",
                    "message": str(data.get("detail") or ""), "t": t}
        return None
    if etype == "conduct_alert":
        level = {"crit": "critical", "critical": "critical", "warn": "warning",
                 "warning": "warning"}.get(str(data.get("severity") or "").lower(), "info")
        return {"level": level, "source": "conduct", "code": str(data.get("code") or ""),
                "message": str(data.get("message") or ""), "conduct_id": data.get("conduct_id"),
                "t": t}
    return None


def _dated(hit: dict, at: float | None, now_wall: float, start_wall: float | None, *,
           backlog: bool | None = None) -> dict:
    """``hit`` plus ``age_s`` (now minus ``at``) and ``backlog`` (``at`` before the wait
    started; ``backlog`` given = that value). An unknown ``at`` adds neither guess."""
    if at is not None:
        hit["age_s"] = round(max(0.0, now_wall - at), 3)
        if backlog is None and start_wall is not None:
            backlog = at < start_wall
    if backlog is not None:
        hit["backlog"] = bool(backlog)
    return hit


# ── operator requests ────────────────────────────────────────────────────────
def default_wishlist_lookup() -> list[dict]:
    """MAST's agent -> operator requests: the board the ext API's ``/requests`` writes, plus the
    structured operator prompts (``mast.operator_hitl``) as rows of the same shape. A prompt
    answered, dismissed, cancelled or timed out counts as answered (``done`` / ``dismissed``),
    so an ``operator_reply`` wait ends for it too and its event says which, with the choice and
    value the operator gave."""
    from mast.wishlist import list_agent_requests

    rows = list(list_agent_requests())
    try:
        from mast.operator_hitl import service as _operator

        rows += _operator.prompt_rows()
    except Exception:  # noqa: BLE001 - the prompts being unreadable must not hide the wishlist
        logger.debug("WaitHub: operator prompts unreadable", exc_info=True)
    return rows


def _reply_event(r: dict) -> dict:
    msg = str(r.get("message") or "")
    ev = {"request_id": str(r.get("id") or ""), "status": str(r.get("status") or ""),
          "note": str(r.get("note") or ""), "path": str(r.get("path") or ""),
          "resolved_at": r.get("resolved_at"), "agent_id": r.get("agent_id"),
          "kind": r.get("kind"), "message": msg if len(msg) <= 300 else msg[:300] + "..."}
    if r.get("structured"):
        # a structured operator prompt: status is the wishlist's word (done | dismissed);
        # prompt_status is the exact one (answered | cancelled | timeout | dismissed)
        ans = r.get("answer") if isinstance(r.get("answer"), dict) else {}
        ev.update({"structured": True, "prompt_status": r.get("prompt_status"),
                   "choice": ans.get("choice"), "value": ans.get("value"),
                   "answered_by": ans.get("answered_by"), "ok": ans.get("ok")})
    return ev


def _job_view(job: Any) -> dict:
    view = getattr(job, "view", None)
    try:
        v = view() if callable(view) else None
    except Exception as exc:  # noqa: BLE001 - a broken view must not break the wait
        return {"terminal": True, "error": f"job view failed: {type(exc).__name__}: {exc}"}
    return dict(v) if isinstance(v, dict) else {"terminal": True}


# ── the hub ──────────────────────────────────────────────────────────────────
class WaitHub:
    """Event history + the ``wait`` coroutine (see the module docstring). Thread-safe:
    :meth:`publish` may be called from any thread; any number of waits may run at once."""

    def __init__(self, *, job_lookup: Callable[[str], Any] | None = None,
                 wishlist_lookup: Callable[[], Iterable[dict]] | None = None,
                 clock: Callable[[], float] = time.monotonic, ring_size: int = RING_SIZE) -> None:
        self._job_lookup = job_lookup
        self._wishlist_lookup = wishlist_lookup
        self._clock = clock
        self._ring_size = max(1, int(ring_size))
        self._lock = threading.Lock()
        self._epoch = secrets.token_hex(4)
        self._seq = 0
        self._rings: dict[str, deque] = {k: deque() for k in KINDS}
        self._evicted: dict[str, int] = {k: 0 for k in KINDS}      # highest seq dropped per ring
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._bus: tuple[Any, Callable] | None = None
        self._bus_lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._req_seen: dict[str, tuple] | None = None              # None = no snapshot yet
        self._req_rows: dict[str, dict] = {}

    # ── events ──────────────────────────────────────────────────────────────
    def publish(self, kind: str, event: dict) -> int:
        """Record one event and wake the waits. Returns its ``seq``. ``kind`` in :data:`KINDS`."""
        if kind not in KINDS:
            raise ValueError(f"event kind {kind!r} is not one of {list(KINDS)}")
        ev = dict(event or {})
        if kind == "alert":
            ev["level"] = _LEVEL_ALIASES.get(str(ev.get("level") or "").lower(), "info")
        with self._lock:
            self._seq += 1
            seq = self._seq
            ring = self._rings[kind]
            if len(ring) >= self._ring_size:
                self._evicted[kind] = ring.popleft()["seq"]
            ring.append({"seq": seq, "t": time.time(), "event": ev})
            waiters = list(self._waiters)
        for loop, flag in waiters:
            try:
                loop.call_soon_threadsafe(flag.set)
            except RuntimeError:            # that wait's loop is closed; it is gone
                pass
        return seq

    def recent(self, kind: str, *, limit: int = 10, within_s: float | None = None) -> list[dict]:
        """The newest recorded events of one kind, newest first: ``[{seq, t, age_s, event}]``.

        A read-only view for summaries (the ext briefing lists recent alerts from here, the same
        events ``mast_wait`` delivers). It moves no cursor and wakes nobody. ``within_s`` keeps
        only events recorded in the last that many seconds."""
        if kind not in KINDS:
            raise ValueError(f"event kind {kind!r} is not one of {list(KINDS)}")
        now = time.time()
        with self._lock:
            rows = list(self._rings[kind])
        out: list[dict] = []
        for r in reversed(rows):                         # rings are in publish order
            if within_s is not None and now - r["t"] > within_s:
                break
            out.append({"seq": r["seq"], "t": r["t"], "age_s": round(max(0.0, now - r["t"]), 3),
                        "event": dict(r["event"])})
            if len(out) >= max(1, int(limit)):
                break
        return out

    def cursor(self) -> str:
        """Opaque cursor of "now" (epoch, seq, wall time). Pass it back as ``since``."""
        with self._lock:
            seq = self._seq
        return self._make_cursor(seq)

    def _make_cursor(self, seq: int) -> str:
        return f"c1.{self._epoch}.{int(seq)}.{int(time.time() * 1000)}"

    def _parse_cursor(self, since: str) -> tuple[str, int, float]:
        m = _CURSOR_RE.match(str(since).strip())
        if not m:
            raise CursorError("since is not a cursor this server issued; pass back the 'cursor' "
                              "of an earlier wait answer, or omit since")
        return m.group(1), int(m.group(2)), int(m.group(3)) / 1000.0

    # ── EventBus ────────────────────────────────────────────────────────────
    def attach_event_bus(self, bus: Any = None) -> None:
        """Subscribe to MAST's ``EventBus`` for alerts (idempotent) and take the first snapshot
        of the operator requests. ``bus`` defaults to the process singleton."""
        with self._bus_lock:
            if self._bus is not None:
                return
            if bus is None:
                from mast.core.events import EventBus

                bus = EventBus.get()
            cb = self._on_bus_event
            bus.subscribe(cb)
            self._bus = (bus, cb)
        self.poll_wishlist()

    def detach_event_bus(self) -> None:
        with self._bus_lock:
            handle, self._bus = self._bus, None
        if handle is not None:
            bus, cb = handle
            try:
                bus.unsubscribe(cb)
            except Exception:  # noqa: BLE001
                logger.debug("WaitHub: unsubscribe failed", exc_info=True)

    def _on_bus_event(self, event: Any) -> None:
        # Runs on the publisher's thread (the watchdog, the current monitor, a conduct tick):
        # map and enqueue only.
        try:
            alert = alert_from_bus_event(event)
            if alert is not None:
                self.publish("alert", alert)
        except Exception:  # noqa: BLE001 - a bus callback must never raise
            logger.debug("WaitHub: alert mapping failed", exc_info=True)

    # ── operator requests ───────────────────────────────────────────────────
    def poll_wishlist(self) -> int:
        """Read the operator requests once; publish an ``operator_reply`` event for each request
        answered since the previous snapshot (none on the first one). Returns how many."""
        lookup = self._wishlist_lookup
        if lookup is None:
            return 0
        published = 0
        with self._req_lock:               # serialised, so an older read never overwrites a newer
            try:
                rows = [dict(r) for r in (lookup() or []) if isinstance(r, dict)]
            except Exception:  # noqa: BLE001 - an unreadable board is "nothing new"
                logger.debug("WaitHub: wishlist lookup failed", exc_info=True)
                return 0
            seeded = self._req_seen is not None
            seen = self._req_seen or {}
            nxt: dict[str, tuple] = {}
            by_id: dict[str, dict] = {}
            for r in rows:
                rid = str(r.get("id") or "").strip()
                if not rid:
                    continue
                sig = (str(r.get("status") or ""), str(r.get("resolved_at") or ""),
                       str(r.get("note") or ""), str(r.get("path") or ""))
                nxt[rid], by_id[rid] = sig, r
                if seeded and sig[0] in ANSWERED and seen.get(rid) != sig:
                    self.publish("operator_reply", _reply_event(r))
                    published += 1
            self._req_seen, self._req_rows = nxt, by_id
        return published

    def _request_state(self, rid: str) -> tuple[bool, dict | None]:
        """``(known_board, row)``: whether a snapshot exists, and the request's latest row."""
        with self._req_lock:
            return self._req_seen is not None, self._req_rows.get(rid)

    # ── wait ────────────────────────────────────────────────────────────────
    async def wait(self, conditions: list[dict], *, timeout_s: float, since: str | None = None,
                   disconnected: Callable[[], Any] | None = None) -> dict:
        """Wait until a condition fires or ``timeout_s`` passes (see the module docstring).

        Returns ``{"fired": [{"index", "type", "event", "seq"?, "age_s"?, "backlog"?}],
        "timed_out", "waited_s", "cursor"}`` (+ ``omitted`` when entries were cut, + ``cancelled``
        when the client went away). Raises :class:`ConditionError` / :class:`CursorError` before
        waiting at all."""
        conds = parse_conditions(conditions)
        try:
            timeout = float(timeout_s)
        except (TypeError, ValueError):
            raise ConditionError("timeout_s must be a number") from None
        if not math.isfinite(timeout) or timeout < 0:
            raise ConditionError("timeout_s must be a finite number >= 0")
        timeout = min(timeout, MAX_WAIT_S)
        start = self._clock()
        start_wall = time.time()
        wants_replies = any(c["type"] == "operator_reply" for c in conds)
        if self._wishlist_lookup is not None:
            await asyncio.to_thread(self.poll_wishlist)
        base = self._baseline(since, conds)

        loop = asyncio.get_running_loop()
        flag = asyncio.Event()
        waiter = (loop, flag)
        with self._lock:
            self._waiters.add(waiter)
        try:
            last_poll = last_disc = self._clock()
            while True:
                flag.clear()
                fired, omitted, upto = self._check(conds, base, start, start_wall)
                elapsed = self._clock() - start
                if fired:
                    return self._answer(fired, False, elapsed, upto, omitted)
                if elapsed >= timeout:
                    return self._answer([], True, elapsed, upto, 0)
                now = self._clock()
                if wants_replies and self._wishlist_lookup is not None \
                        and now - last_poll >= WISHLIST_POLL_S:
                    last_poll = now
                    await asyncio.to_thread(self.poll_wishlist)
                    continue
                if disconnected is not None and now - last_disc >= DISCONNECT_POLL_S:
                    last_disc = now
                    if await self._gone(disconnected):
                        out = self._answer([], False, elapsed, upto, 0)
                        out["cancelled"] = "client_disconnected"
                        return out
                try:
                    await asyncio.wait_for(flag.wait(), timeout=min(TICK_S, max(0.0, timeout - elapsed)))
                except asyncio.TimeoutError:
                    pass
        finally:
            with self._lock:
                self._waiters.discard(waiter)

    @staticmethod
    async def _gone(disconnected: Callable[[], Any]) -> bool:
        try:
            r = disconnected()
            if inspect.isawaitable(r):
                r = await r
            return bool(r)
        except Exception:  # noqa: BLE001 - "cannot tell" is not "gone"
            return False

    def _baseline(self, since: str | None, conds: list[dict]) -> dict:
        kinds = {c["type"] for c in conds} & set(KINDS)
        with self._lock:
            cur = self._seq
            evicted = dict(self._evicted)
        if since is None or not str(since).strip():
            return {"seq": cur, "wall": None, "gap": None}
        epoch, seq, wall = self._parse_cursor(since)
        if epoch != self._epoch:
            gap = {"kinds": sorted(kinds), "reason": "cursor_from_another_run",
                   "detail": ("this cursor is from an earlier server run; events of that run are "
                              "gone. Re-read the state (observe / history), then wait with the "
                              "new cursor")} if kinds else None
            return {"seq": None, "wall": wall, "gap": gap}
        if seq > cur:
            raise CursorError("since is ahead of this server's event sequence; pass back a cursor "
                              "this server issued")
        lost = sorted(k for k in kinds if evicted[k] > seq)
        gap = {"kinds": lost, "reason": "evicted",
               "detail": _GAP_HINT.format(n=self._ring_size)} if lost else None
        return {"seq": seq, "wall": None, "gap": gap}

    def _check(self, conds: list[dict], base: dict, start: float,
               start_wall: float | None = None) -> tuple[list[dict], int, int]:
        """One pass over every condition. Returns ``(fired, omitted, upto)``; ``upto`` is the
        last ``seq`` examined, which the answer's cursor encodes. ``start_wall`` (wall clock of
        the wait call) decides ``backlog``."""
        with self._lock:
            upto = self._seq
            if base["seq"] is not None:
                snap = {k: [r for r in self._rings[k] if base["seq"] < r["seq"] <= upto] for k in KINDS}
            else:
                snap = {k: [r for r in self._rings[k] if r["t"] > base["wall"] and r["seq"] <= upto]
                        for k in KINDS}
        elapsed = self._clock() - start
        now_wall = time.time()
        fired: list[dict] = []
        omitted = 0
        for i, c in enumerate(conds):
            t = c["type"]
            hits: list[dict] = []
            if t == "job":
                hits = [_dated(h, _epoch_of(h["event"].get("finished_at")), now_wall, start_wall)
                        for h in self._job_hits(i, c["job_id"])]
            elif t == "timer":
                if elapsed >= c["seconds"]:
                    hits = [{"index": i, "type": t,
                             "event": {"seconds": c["seconds"], "elapsed_s": round(elapsed, 3)}}]
            else:
                rows = snap[t]
                if t == "alert":
                    need = _level_rank(c["min_level"])
                    rows = [r for r in rows if _level_rank(r["event"].get("level")) >= need]
                elif t == "detector" and c["detector_id"]:
                    rows = [r for r in rows if r["event"].get("detector_id") == c["detector_id"]]
                elif t == "operator_reply" and c["request_id"]:
                    rows = [r for r in rows if r["event"].get("request_id") == c["request_id"]]
                # an answer is dated by when the operator gave it, not when the hub noticed it
                hits = [_dated({"index": i, "type": t, "seq": r["seq"], "event": dict(r["event"])},
                               (_epoch_of(r["event"].get("resolved_at")) if t == "operator_reply"
                                else None) or r["t"], now_wall, start_wall)
                        for r in rows]
                if t == "operator_reply" and c["request_id"] and not hits:
                    hits = [_dated(h, _epoch_of(h["event"].get("resolved_at")), now_wall, start_wall,
                                   backlog=True if "status" in h["event"] else None)
                            for h in self._request_hits(i, c["request_id"])]
            if len(hits) > MAX_FIRED_PER_CONDITION:
                omitted += len(hits) - MAX_FIRED_PER_CONDITION
                hits = hits[-MAX_FIRED_PER_CONDITION:]
            fired += hits
        if len(fired) > MAX_FIRED:
            omitted += len(fired) - MAX_FIRED
            fired = fired[-MAX_FIRED:]
        if base["gap"]:
            fired.append({"index": None, "type": "gap", "event": dict(base["gap"])})
        return fired, omitted, upto

    def _job_hits(self, i: int, job_id: str) -> list[dict]:
        if self._job_lookup is None:
            return [{"index": i, "type": "job", "event": {
                "job_id": job_id, "error": "jobs_unavailable",
                "detail": "this server cannot look jobs up (the job manager is not wired)"}}]
        try:
            job = self._job_lookup(job_id)
        except Exception as exc:  # noqa: BLE001 - a failing lookup must not hang the wait
            return [{"index": i, "type": "job", "event": {
                "job_id": job_id, "error": "lookup_failed", "detail": f"{type(exc).__name__}: {exc}"}}]
        if job is None:
            return [{"index": i, "type": "job", "event": {
                "job_id": job_id, "error": "unknown_job",
                "detail": "no job with this id (check the id; GET /jobs lists yours)"}}]
        if not getattr(job, "terminal", False):
            return []
        return [{"index": i, "type": "job", "event": _job_view(job)}]

    def _request_hits(self, i: int, rid: str) -> list[dict]:
        if self._wishlist_lookup is None:
            return []
        known, row = self._request_state(rid)
        if not known:
            return []
        if row is None:
            return [{"index": i, "type": "operator_reply", "event": {
                "request_id": rid, "error": "unknown_request",
                "detail": "no operator request with this id (GET /requests lists yours)"}}]
        if str(row.get("status") or "") in ANSWERED:
            return [{"index": i, "type": "operator_reply", "event": _reply_event(row)}]
        return []

    def _answer(self, fired: list[dict], timed_out: bool, elapsed: float, upto: int,
                omitted: int) -> dict:
        out = {"fired": fired, "timed_out": bool(timed_out), "waited_s": round(max(0.0, elapsed), 3),
               "cursor": self._make_cursor(upto)}
        if omitted:
            out["omitted"] = int(omitted)
        return out


__all__ = ["ANSWERED", "CONDITION_TYPES", "DEFAULT_WAIT_S", "KINDS", "LEVELS", "MAX_CONDITIONS",
           "MAX_WAIT_S", "RING_SIZE", "ConditionError", "CursorError", "WaitHub",
           "alert_from_bus_event", "default_wishlist_lookup", "parse_conditions"]
