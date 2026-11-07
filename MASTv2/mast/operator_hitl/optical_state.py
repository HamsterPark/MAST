"""Manual optical-state registry — what a person has physically set up.

Design contract: ``docs/v2/design/optical_station_witec.md`` §6.2 / D9.

Slots describe things no software can read back: which λ/2 is in the excitation
path, the manual analyzer angle, which objective is turned in, the edge filter,
the NPI pump-current %, and the laser-area-safe attestation. Each slot value
carries who set it, when, from where, and until when it is valid.

Rules:

* **Only operators write** (:meth:`OpticalStateStore.set_by_operator` is called by
  operator web routes, the launcher popup, and :func:`mast.operator_hitl.prompts.answer`
  for ``attest`` prompts). There is deliberately no agent skill that writes a slot.
* Reads are free: skills attach :meth:`OpticalStateStore.snapshot` to every
  acquisition sidecar and may use :meth:`OpticalStateStore.is_valid` as a precondition.
* Every write is appended to the history (persistent backends: a JSONL file).
* A write that cannot be persisted does not happen: :meth:`OpticalStateStore._persist`
  raising rolls the in-memory value back and the error reaches the caller (the operator
  route answers 503). An attestation the audit trail never saw must not stay in force.
* Every write can carry ``client``: what the *server* saw of the request that made it (peer
  address, client identification, how it authenticated — ``mast/api/routes/operator.py``
  ``client_record``). It is stored with the value and in the history, never taken from the
  request body, and kept out of :meth:`OpticalStateStore.snapshot` by default (a snapshot
  goes into spectrum sidecars and to external agents; the operator API asks for it).
* A value is valid until its ``valid_until`` (aware ISO 8601). A stored time that cannot be
  trusted - no time zone, not a time at all - never raises: the value counts as expired. A
  persistent store re-derives ``valid_until`` from ``set_at + valid_for_s`` when it reads a value
  back (:func:`restored_valid_until`), so a hand-edited file cannot lengthen an attestation.
* After a write is committed, :meth:`OpticalStateStore.add_listener` callbacks run
  synchronously on the writing thread (see there): the hook for "the laser-area attestation
  was just recorded as not true / cleared: switch the emission off".

Slot ids and ``kind`` values are identity strings (never translated).
"""

from __future__ import annotations

import contextlib
import copy
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

__all__ = ["SLOT_KINDS", "OpticalSlot", "SlotValue", "OpticalStateError", "UnknownSlot",
           "OpticalStateStore", "DEFAULT_SLOTS", "get_store", "set_store",
           "MAX_SLOT_TEXT_CHARS", "MAX_SLOT_NOTE_CHARS", "parse_aware", "restored_valid_until"]

#: records kept in ``OpticalStateStore.history`` (the in-memory tail; persistent
#: stores keep the full history on disk)
MAX_HISTORY_KEPT = 2000
#: longest value a ``text`` slot accepts. Longer is refused (422 at the operator API), never
#: truncated: a recorded attestation must say what the operator said. The web UI refuses at
#: the same length.
MAX_SLOT_TEXT_CHARS = 500
#: longest note kept with a slot value (the note of an attest answer is kept whole in the
#: answer record; the slot keeps a copy of at most this many characters)
MAX_SLOT_NOTE_CHARS = 500

SLOT_KINDS = ("choice", "number", "text", "flag")


class OpticalStateError(ValueError):
    """Unknown slot, or a value that does not fit the slot."""


class UnknownSlot(OpticalStateError):
    """No slot with that id (an operator route answers 404; a bad value is a 422)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: how far ahead of the clock a stored ``set_at`` may lie before it is not believed (a clock
#: that was stepped back, a hand-edited file)
FUTURE_TOLERANCE = timedelta(minutes=10)


def parse_aware(ts: Any) -> datetime | None:
    """*ts* as a timezone-aware ``datetime``; ``None`` if it is not a string, not ISO 8601, or
    has no time zone (comparing such a value with the clock would raise ``TypeError``)."""
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo is not None and dt.utcoffset() is not None else None


def restored_valid_until(slot: "OpticalSlot", set_at: Any, valid_until: Any, *,
                         now: datetime | None = None) -> tuple[str | None, str]:
    """The ``valid_until`` to restore for a value read back from disk, and what was wrong.

    A file is not trusted to say how long an attestation lasts: the end is the *earlier* of the
    stored ``valid_until`` and ``set_at + slot.valid_for_s`` (the slot's definition as it is now).
    A time that cannot be trusted - ``set_at`` missing, not ISO 8601, without a time zone or more
    than :data:`FUTURE_TOLERANCE` ahead of the clock; a stored ``valid_until`` that is not a time
    with a time zone - makes the value count as expired as of *now* (fail closed; the value stays
    visible, so the operator sees it lapsed and signs again). Returns ``(valid_until, problem)``:
    ``valid_until`` is an aware ISO string or ``None`` (valid until changed); ``problem`` is ``""``
    when the file was fine, else a sentence for the log."""
    now = now or _now()
    lapsed = now.isoformat(timespec="seconds")
    start = parse_aware(set_at)
    if start is None:
        return lapsed, f"set_at {set_at!r} is not a time with a time zone"
    if start > now + FUTURE_TOLERANCE:
        return lapsed, f"set_at {set_at!r} is in the future"
    stored = None
    if valid_until is not None:
        stored = parse_aware(valid_until)
        if stored is None:
            return lapsed, f"valid_until {valid_until!r} is not a time with a time zone"
    limit = start + timedelta(seconds=slot.valid_for_s) if slot.valid_for_s else None
    ends = [t for t in (stored, limit) if t is not None]
    if not ends:
        return None, ""
    end = min(ends).isoformat(timespec="seconds")
    problem = ""
    if stored is not None and limit is not None and stored > limit:
        problem = (f"valid_until {valid_until} is later than set_at + valid_for_s "
                   f"({slot.valid_for_s:g} s), which is {end}")
    return end, problem


@dataclass(frozen=True)
class OpticalSlot:
    """``options``: for ``choice`` slots, ``[{id, label, meta}]`` — e.g. a waveplate's
    ``meta = {"valid_nm": [500, 580]}``. ``valid_for_s``: ``None`` = valid until changed."""

    id: str
    label: str
    kind: str = "choice"
    options: tuple[dict, ...] = ()
    unit: str = ""
    min: float | None = None
    max: float | None = None
    valid_for_s: float | None = None
    description: str = ""

    def option(self, option_id: str) -> dict | None:
        return next((dict(o) for o in self.options if str(o.get("id")) == str(option_id)), None)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["options"] = [dict(o) for o in self.options]
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OpticalSlot":
        kind = str(raw.get("kind", "choice"))
        if kind not in SLOT_KINDS:
            raise OpticalStateError(f"slot {raw.get('id')!r}: kind {kind!r} not one of {SLOT_KINDS}")
        return cls(id=str(raw["id"]), label=str(raw.get("label") or raw["id"]), kind=kind,
                   options=tuple(dict(o) for o in raw.get("options") or ()),
                   unit=str(raw.get("unit", "")),
                   min=None if raw.get("min") is None else float(raw["min"]),
                   max=None if raw.get("max") is None else float(raw["max"]),
                   valid_for_s=None if raw.get("valid_for_s") is None else float(raw["valid_for_s"]),
                   description=str(raw.get("description", "")))


@dataclass
class SlotValue:
    """``source`` is what the writer *says* it is (``web`` / ``launcher`` / ``prompt:<id>``);
    ``client`` is what the server *saw* (see the module docstring) — ``None`` for writes that
    did not come through an HTTP request."""

    value: Any
    set_by: str
    set_at: str
    source: str
    valid_until: str | None = None
    note: str = ""
    client: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: defaults for the WITec station; ``config/optical_station.json`` overrides them
DEFAULT_SLOTS: tuple[OpticalSlot, ...] = (
    OpticalSlot("excitation_waveplate", "激发光路 λ/2 片 / excitation half-wave plate",
                options=({"id": "hwp_532", "label": "λ/2 @532 nm", "meta": {"valid_nm": [500, 580]}},
                         {"id": "hwp_1064", "label": "λ/2 @1064 nm", "meta": {"valid_nm": [1000, 1100]}},
                         {"id": "none", "label": "无 / none", "meta": {}})),
    OpticalSlot("analyzer_optic", "Analyzer 座里装的片 / optic in the analyzer wheel",
                options=({"id": "hwp_532", "label": "λ/2 @532 nm", "meta": {"valid_nm": [500, 580]}},
                         {"id": "hwp_1064", "label": "λ/2 @1064 nm", "meta": {"valid_nm": [1000, 1100]}},
                         {"id": "polarizer", "label": "检偏片 / polarizer", "meta": {}},
                         {"id": "none", "label": "空 / empty", "meta": {}})),
    OpticalSlot("incident_polarizer_deg", "手动起偏/检偏角 / manual polarizer angle",
                kind="number", unit="deg", min=0.0, max=360.0),
    OpticalSlot("objective", "物镜 / objective",
                options=({"id": "10x", "label": "10×"},
                         {"id": "50x_ld", "label": "50× LD"},
                         {"id": "40x", "label": "40×"},
                         {"id": "50x", "label": "50×"},
                         {"id": "100x", "label": "100×"})),
    OpticalSlot("edge_filter", "滤光片 / edge filter", kind="text"),
    OpticalSlot("npi_power_pct", "NPI 1064 泵浦电流 % / NPI pump current %",
                kind="number", unit="%", min=0.0, max=100.0, valid_for_s=8 * 3600),
    OpticalSlot("laser_area_safe", "激光区安全（护目镜、光路封闭、无人在光路）/ laser area safe",
                kind="flag", valid_for_s=8 * 3600,
                description="签了才允许软件开出光；8 小时后失效 / required before software may "
                            "switch emission on; expires after 8 h"),
)


class OpticalStateStore:
    """In-memory store (default). Persistent backends subclass and override
    :meth:`_persist` (see :class:`mast.operator_hitl.store.PersistentOpticalStateStore`).

    ``seq`` goes up on every set / clear (a poller compares it for equality; it is
    not monotonic across restarts unless the subclass persists it).
    """

    def __init__(self, slots: tuple[OpticalSlot, ...] | list[OpticalSlot] = DEFAULT_SLOTS):
        self._lock = threading.RLock()
        self._slots: dict[str, OpticalSlot] = {s.id: s for s in slots}
        self._values: dict[str, SlotValue] = {}
        self.history: list[dict[str, Any]] = []
        self._seq = 0
        self._listeners: list[Callable[[str, dict | None], None]] = []
        self._listeners_lock = threading.Lock()
        self._defer = threading.local()

    @property
    def seq(self) -> int:
        return self._seq

    # -- write listeners -----------------------------------------------------------

    def add_listener(self, fn: Callable[[str, dict | None], None]) -> None:
        """Call ``fn(slot_id, record)`` after every committed write.

        * ``record`` is the history record of a **set** (``{slot, value, set_by, set_at,
          source, valid_until, note, client}`` — a copy the listener may keep or change) and
          ``None`` for a **clear**. What the operator recorded is ``record["value"]``: a flag
          recorded ``False`` is a legitimate value, not "unset" (so ``is_valid(slot)`` without
          ``expect=`` stays true for it).
        * Called synchronously, on the thread that made the write, **after** the write was
          persisted and committed and after this store's lock was released (a listener may
          read the store, and may write it from another thread, without deadlocking). A write
          that failed (persisting raised, so it was rolled back) calls nobody.
        * An attest answer sets its slots while the prompt backend holds its own lock;
          ``prompts.answer`` delivers those writes only after that lock is released
          (:meth:`deferred_notifications`), so a slow listener never stalls the prompt list.
        * A listener that raises is logged with its traceback (ERROR) and skipped; the write
          stands and the other listeners still run. Writers racing on different threads may
          be notified out of order — read the store for the current state when it matters.
        * Registering the same callable twice registers it once. Thread-safe, as are
          :meth:`remove_listener` and the notifications themselves.
        * A listener belongs to this store *instance*. ``service.install()`` swaps in a new
          persistent store (and a second install with another root swaps again), and does not
          carry listeners over: register on ``optical_state.get_store()`` after ``install()``.

        A lapse by time (``valid_until`` passing) is not a write and calls nobody.
        """
        if not callable(fn):
            raise TypeError("an optical-state listener must be callable")
        with self._listeners_lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[str, dict | None], None]) -> bool:
        """Stop calling *fn*. ``True`` if it was registered; never raises (it is safe to call
        from inside a listener)."""
        with self._listeners_lock:
            try:
                self._listeners.remove(fn)
            except ValueError:
                return False
        return True

    @property
    def listeners(self) -> tuple[Callable[[str, dict | None], None], ...]:
        """The registered listeners (a snapshot; use add_listener / remove_listener)."""
        with self._listeners_lock:
            return tuple(self._listeners)

    @contextlib.contextmanager
    def deferred_notifications(self) -> Iterator[None]:
        """Hold back the notifications of this thread's writes inside the block and deliver them,
        in order, when the outermost block exits (also when it exits by an exception: the writes
        were committed). For callers that write while holding a lock of their own."""
        if getattr(self._defer, "queue", None) is not None:       # nested: the outer block delivers
            yield
            return
        queue: list[tuple[str, dict | None]] = []
        self._defer.queue = queue
        try:
            yield
        finally:
            self._defer.queue = None
            for slot_id, record in queue:
                self._deliver(slot_id, record)

    def _notify(self, slot_id: str, record: dict[str, Any] | None) -> None:
        queue = getattr(self._defer, "queue", None)
        if queue is not None:
            queue.append((slot_id, record))
        else:
            self._deliver(slot_id, record)

    def _deliver(self, slot_id: str, record: dict[str, Any] | None) -> None:
        for fn in self.listeners:
            try:
                fn(slot_id, None if record is None else copy.deepcopy(record))
            except Exception:  # noqa: BLE001 - a listener must not break the operator flow
                logger.exception("optical-state listener %r failed after a write to slot %r "
                                 "(the write stands)", fn, slot_id)

    def _remember(self, rec: dict[str, Any]) -> None:
        """Append to the in-memory history tail (bounded) and count the change."""
        self.history.append(rec)
        if len(self.history) > MAX_HISTORY_KEPT:
            del self.history[:len(self.history) - MAX_HISTORY_KEPT]
        self._seq += 1

    def history_tail(self, limit: int = 200) -> list[dict[str, Any]]:
        """The newest *limit* history records, oldest first. Persistent stores read
        their file, so this reaches back beyond the in-memory tail and across restarts."""
        with self._lock:
            return [dict(r) for r in self.history[-max(1, int(limit)):]]

    # -- schema --------------------------------------------------------------

    def slots(self) -> list[OpticalSlot]:
        with self._lock:
            return list(self._slots.values())

    def slot(self, slot_id: str) -> OpticalSlot:
        with self._lock:
            s = self._slots.get(slot_id)
        if s is None:
            raise UnknownSlot(f"no optical-state slot {slot_id!r}")
        return s

    def set_slots(self, slots: list[OpticalSlot]) -> None:
        with self._lock:
            self._slots = {s.id: s for s in slots}

    # -- values ----------------------------------------------------------------

    def _check(self, slot: OpticalSlot, value: Any) -> Any:
        if slot.kind == "choice":
            if slot.option(str(value)) is None:
                raise OpticalStateError(
                    f"slot {slot.id!r}: {value!r} is not one of {[o.get('id') for o in slot.options]}")
            return str(value)
        if slot.kind == "number":
            try:
                v = float(value)
            except (TypeError, ValueError) as exc:
                raise OpticalStateError(f"slot {slot.id!r}: a number is required") from exc
            if v != v or v in (float("inf"), float("-inf")):
                # NaN compares false with every bound, so it would slip past the range check
                raise OpticalStateError(f"slot {slot.id!r}: a finite number is required")
            if slot.min is not None and v < slot.min or slot.max is not None and v > slot.max:
                raise OpticalStateError(f"slot {slot.id!r}: {v} outside [{slot.min}, {slot.max}]")
            return v
        if slot.kind == "flag":
            if not isinstance(value, bool) and str(value).lower() not in ("true", "false", "1", "0", "yes", "no"):
                raise OpticalStateError(f"slot {slot.id!r}: a yes/no flag is required")
            return value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")
        text = str(value)
        if len(text) > MAX_SLOT_TEXT_CHARS:
            raise OpticalStateError(
                f"slot {slot.id!r}: the text is {len(text)} characters, at most "
                f"{MAX_SLOT_TEXT_CHARS} are accepted")
        return text

    def validate_value(self, slot_id: str, value: Any) -> Any:
        """The value *slot_id* would store for *value* (``OpticalStateError`` if it does
        not fit the slot or the slot is unknown). Changes nothing."""
        return self._check(self.slot(slot_id), value)

    def set_by_operator(self, slot_id: str, value: Any, *, by: str, source: str,
                        note: str = "", client: dict[str, Any] | None = None) -> SlotValue:
        """**Operator channels only.** Validates, stores, appends to history, then notifies the
        listeners (:meth:`add_listener`).

        ``source`` is what the writer says it is (self-reported); ``client`` is what the server
        saw of the request (``client_record`` of the operator routes) and is the one an audit
        goes by. If persisting fails the value is rolled back and the error is raised."""
        if not str(by or "").strip():
            raise OpticalStateError("set_by_operator needs the operator's name (by=)")
        slot = self.slot(slot_id)
        v = self._check(slot, value)
        now = _now()
        until = (now + timedelta(seconds=slot.valid_for_s)).isoformat(timespec="seconds") \
            if slot.valid_for_s else None
        sv = SlotValue(value=v, set_by=str(by).strip(), set_at=now.isoformat(timespec="seconds"),
                       source=str(source), valid_until=until,
                       note=str(note or "")[:MAX_SLOT_NOTE_CHARS],
                       client=copy.deepcopy(client) if client else None)
        rec = {"slot": slot_id, **sv.to_dict()}
        with self._lock:
            prev = self._values.get(slot_id)
            self._values[slot_id] = sv
            try:
                self._persist(rec)
            except Exception:
                if prev is None:
                    self._values.pop(slot_id, None)
                else:
                    self._values[slot_id] = prev
                raise
            self._remember(rec)
        self._notify(slot_id, rec)
        return sv

    def clear_by_operator(self, slot_id: str, *, by: str, source: str,
                          client: dict[str, Any] | None = None) -> None:
        """**Operator channels only.** Forget the slot's value (the history keeps it), then
        notify the listeners with ``record=None``. ``source`` / ``client`` as for
        :meth:`set_by_operator`."""
        if not str(by or "").strip():
            raise OpticalStateError("clear_by_operator needs the operator's name (by=)")
        self.slot(slot_id)
        rec = {"slot": slot_id, "value": None, "set_by": str(by).strip(),
               "set_at": _now().isoformat(timespec="seconds"), "source": str(source),
               "valid_until": None, "note": "cleared",
               "client": copy.deepcopy(client) if client else None}
        with self._lock:
            prev = self._values.pop(slot_id, None)
            try:
                self._persist(rec)
            except Exception:
                if prev is not None:
                    self._values[slot_id] = prev
                raise
            self._remember(rec)
        self._notify(slot_id, None)

    def get(self, slot_id: str) -> SlotValue | None:
        self.slot(slot_id)
        with self._lock:
            sv = self._values.get(slot_id)
            return copy.deepcopy(sv) if sv is not None else None

    def is_valid(self, slot_id: str, *, expect: Any = None) -> bool:
        """Set, not expired, and (if *expect* is given) equal to it."""
        sv = self.get(slot_id)
        if sv is None:
            return False
        if sv.valid_until is not None:
            until = parse_aware(sv.valid_until)           # a time that cannot be read is no time left
            if until is None or until <= _now():
                return False
        return expect is None or sv.value == expect

    def snapshot(self, include_client: bool = False) -> dict[str, Any]:
        """JSON-able ``{slot_id: {value, set_by, set_at, source, valid_until, note, valid}}``
        for every defined slot (unset slots have ``value: None``).

        The server-side ``client`` record of each value is left out unless *include_client*:
        a snapshot goes into every spectrum sidecar and to external agents
        (``ReadOpticalState``), and peer addresses do not belong there."""
        out: dict[str, Any] = {}
        for s in self.slots():
            sv = self.get(s.id)
            if sv is None:
                out[s.id] = {"value": None, "valid": False}
                continue
            d = sv.to_dict()
            if not include_client:
                d.pop("client", None)
            out[s.id] = {**d, "valid": self.is_valid(s.id)}
        return out

    def waveplate_covers(self, slot_id: str, wavelength_nm: float) -> bool | None:
        """Whether the waveplate currently in *slot_id* is specified for
        *wavelength_nm* (``None`` = slot unset or option has no ``valid_nm``)."""
        sv = self.get(slot_id)
        if sv is None:
            return None
        opt = self.slot(slot_id).option(str(sv.value)) or {}
        rng = (opt.get("meta") or {}).get("valid_nm")
        if not rng:
            return None
        return float(rng[0]) <= float(wavelength_nm) <= float(rng[1])

    def _persist(self, record: dict[str, Any]) -> None:  # persistent backends override
        pass


_store: OpticalStateStore = OpticalStateStore()
_store_lock = threading.Lock()


def get_store() -> OpticalStateStore:
    return _store


def set_store(store: OpticalStateStore) -> OpticalStateStore:
    """Swap the store (service start-up; tests). Returns the previous one."""
    global _store
    with _store_lock:
        old, _store = _store, store
    return old
