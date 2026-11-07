"""A read-only picture of the optical station, for the briefing and for ``observe``.

Three things an AI on an optical station needs to see and cannot get from an STM's
live readings:

* the **devices**: what is configured (WITec, lasers, power meter, temperature
  controller, stages), which of them MAST has open, and what an open one reports;
* the **manual optical state**: what a person has physically set up (which λ/2 plate is
  in, the objective, the laser-area attestation), signed by the operator;
* the **open operator prompts**: what is waiting for a person.

The rule that shapes this module is the one in ``api/routes/optics.py``: *a read never
opens a device*. The briefing and ``observe`` are called constantly, from many clients;
opening a serial port there would take the device back from the vendor software the
operator has just handed it to, or hold a port the operator is about to give to NKT
CONTROL. So the inventory comes from the configuration, and a device's status is read
only if the process already has it built (``InstrumentRegistry.built_station_devices``).
Even that read goes to hardware, so it is bounded: a COM call that hangs (WITec Control
frozen behind a modal dialog, a serial device not answering) must cost the briefing a
couple of seconds, not hang it.

Nothing here raises: every part degrades on its own to an ``error`` field.
"""
from __future__ import annotations

import logging
import math
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: how long one device's status read may take before it is reported as timed out
STATUS_TIMEOUT_S = 2.0
#: cap on one device's reported status (characters of its JSON)
STATUS_TEXT_CAP = 1200

_KIND_STATUS_METHOD = {
    "microscope": "microscope_status",
    "laser": "status",
    "power_meter": "status",
    "temperature": "status",
}


def plain(obj: Any, _depth: int = 0) -> Any:
    """JSON-able copy of a device report: numbers, strings, bools, lists, dicts; anything
    else becomes its ``str``; non-finite floats become ``None``; depth and length are
    bounded."""
    if _depth > 6:
        return str(obj)[:80]
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if hasattr(obj, "item") and callable(obj.item):          # numpy scalar
        try:
            return plain(obj.item(), _depth + 1)
        except Exception:  # noqa: BLE001
            return str(obj)[:80]
    if isinstance(obj, dict):
        return {str(k): plain(v, _depth + 1) for k, v in list(obj.items())[:60]}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [plain(v, _depth + 1) for v in list(obj)[:40]]
    return str(obj)[:200]


_inflight: dict[str, threading.Thread] = {}
_inflight_lock = threading.Lock()


def bounded(fn: Callable[[], Any], timeout_s: float = STATUS_TIMEOUT_S, *,
            key: str | None = None) -> tuple[bool, Any]:
    """Run ``fn`` on a daemon thread and wait at most ``timeout_s``.

    ``(True, value)``, or ``(False, "ErrorType: message")``, or ``(False, "timed out after
    N s")``. A timed-out call is not killed (a thread cannot be); it is abandoned and
    finishes whenever the hardware lets it.

    ``key`` names what is being read. While an abandoned read for the same key is still
    running, a new one is **not started** (``(False, "...still waiting...")``): a device that
    never answers would otherwise cost one stuck thread per observation, i.e. thousands
    in a night of polling."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported, never raised
            box["error"] = f"{type(exc).__name__}: {exc}"[:300]

    t = threading.Thread(target=run, name="station-view-read", daemon=True)
    if key is not None:
        with _inflight_lock:
            prev = _inflight.get(key)
            if prev is not None and prev.is_alive():
                return False, "an earlier status read is still waiting for the device (it is not answering)"
            _inflight[key] = t
    t.start()
    t.join(max(0.05, float(timeout_s)))
    if t.is_alive():
        return False, f"timed out after {timeout_s:g} s"
    if key is not None:
        with _inflight_lock:
            if _inflight.get(key) is t:
                del _inflight[key]
    if "error" in box:
        return False, box["error"]
    return True, box.get("value")


def device_status(device: Any, kind: str, *, timeout_s: float = STATUS_TIMEOUT_S) -> dict:
    """The status of one already-built device, or ``{"error": ...}``. Bounded."""
    method = _KIND_STATUS_METHOD.get(kind)
    fn = getattr(device, method, None) if method else None
    if not callable(fn):
        return {"error": f"no status reader for kind {kind!r}"}
    ok, got = bounded(fn, timeout_s, key=f"{kind}:{getattr(device, 'device_id', id(device))}")
    if not ok:
        return {"error": str(got)}
    out = plain(got)
    return out if isinstance(out, dict) else {"value": out}


def inventory(registry: Any = None, *, read_status: bool = True,
              timeout_s: float = STATUS_TIMEOUT_S) -> dict:
    """``{"devices": [...], "error"?: str}``: the configured optical devices, and the
    status of those the process has open. **Never opens a device.**

    Each device: ``id, name, type, kind, enabled, connected`` and, when it is open and
    ``read_status`` is true, ``status`` (or ``status_error``)."""
    try:
        if registry is None:
            from mast.instruments.registry import get_instrument_registry

            registry = get_instrument_registry()
        listing = registry.list_devices()
        built = registry.built_station_devices() if read_status else {}
    except Exception as exc:  # noqa: BLE001
        return {"devices": [], "error": f"{type(exc).__name__}: {exc}"[:300]}
    devices: list[dict] = []
    for d in listing:
        item = {k: d.get(k) for k in ("id", "name", "type", "kind", "role", "enabled", "connected")}
        dev = built.get(str(d.get("id")))
        if dev is not None and d.get("kind") in _KIND_STATUS_METHOD:
            st = device_status(dev, str(d.get("kind")), timeout_s=timeout_s)
            if "error" in st and len(st) == 1:
                item["status_error"] = st["error"]
            else:
                text = str(st)
                item["status"] = st if len(text) <= STATUS_TEXT_CAP else {"truncated": text[:STATUS_TEXT_CAP]}
        devices.append(item)
    return {"devices": devices}


def manual_state() -> dict:
    """``{"slots": [{id, label, kind, value, set_by, set_at, valid, valid_until, source}],
    "error"?}``: the operator-signed manual optical state."""
    try:
        from mast.operator_hitl.optical_state import get_store

        store = get_store()
        snap = store.snapshot()
        slots = []
        for s in store.slots():
            row = snap.get(s.id) or {}
            slots.append({
                "id": s.id, "label": s.label, "kind": s.kind, "unit": s.unit or None,
                "value": plain(row.get("value")), "valid": bool(row.get("valid")),
                "set_by": row.get("set_by"), "set_at": row.get("set_at"),
                "valid_until": row.get("valid_until"), "source": row.get("source"),
            })
        return {"slots": slots}
    except Exception as exc:  # noqa: BLE001
        return {"slots": [], "error": f"{type(exc).__name__}: {exc}"[:300]}


def open_prompts(limit: int = 8) -> dict:
    """``{"count": n, "items": [...]}``: the operator prompts nobody has answered yet."""
    try:
        from mast.operator_hitl import prompts

        rows = list(prompts.list_open())
    except Exception as exc:  # noqa: BLE001
        return {"count": None, "items": [], "error": f"{type(exc).__name__}: {exc}"[:300]}
    items = []
    for p in rows[:max(0, int(limit))]:
        items.append({
            "id": getattr(p, "id", ""), "kind": getattr(p, "kind", ""),
            "severity": getattr(p, "severity", ""),
            "title": str(getattr(p, "title", "") or "")[:160],
            "skill": getattr(p, "skill", None), "agent_id": getattr(p, "agent_id", None),
            "job_id": getattr(p, "job_id", None),
            "created_at": getattr(p, "created_at", ""),
        })
    return {"count": len(rows), "items": items}


__all__ = ["STATUS_TIMEOUT_S", "bounded", "device_status", "inventory", "manual_state",
           "open_prompts", "plain"]
