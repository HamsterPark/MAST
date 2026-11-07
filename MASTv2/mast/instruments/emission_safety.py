"""Put every light source MAST can reach into the safe direction, at once.

This is the reflex behind the operator's laser-area attestation (design
``docs/v2/design/optical_station_witec.md`` D7). Software may start emission only while
``laser_area_safe`` is a valid ``True``. When the operator withdraws it, by recording the
area as NOT safe or by clearing the slot, whatever MAST has lit must go dark now, not at the
next step boundary of whatever happens to be running. :func:`safe_off_all` is that action;
:mod:`mast.operator_hitl.service` calls it from the optical-state write hook.

Expiry is deliberately not a trigger. An attestation that runs out means "sign again", not
"danger", and every emission path re-checks it before it starts: the drivers' emission
guard, and the protocols before every exposure.

What it does, each device on its own (one that cannot be reached does not stop the others):

* every enabled **laser** in the station config: ``emission_off()``, which never raises and
  returns the read-back (``False`` = confirmed off);
* every enabled **microscope**: ``stop_all()`` (sequencers and stage), then every shutter
  closed: ``close_all_shutters()`` where the driver has it (the WITec: every laser, not only
  the selected one), else ``set_shutter(False)``. Closing never needs MAST's write gate; a
  driver that cannot close (the WITec when write access cannot be regained, a laser with no
  software shutter) is reported as not confirmed.

A device not yet built in this process is built for it. Construction does no I/O (§6.1), and
a laser left on by an earlier MAST process is exactly the case this exists for.

Never raises.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["safe_off_all", "safe_off_all_in_background", "revocation_listener",
           "install_revocation_listener", "last_result", "LASER_AREA_SLOT"]

#: the last safe_off_all that had something to act on (the briefing shows it when not everything
#: was confirmed: an E-STOP that could not close a shutter must not stay in the log only)
_LAST: dict | None = None
_LAST_LOCK = threading.Lock()


def last_result() -> dict | None:
    """A copy of the last :func:`safe_off_all` result that touched a device, with ``at`` (UTC ISO)."""
    with _LAST_LOCK:
        return None if _LAST is None else dict(_LAST)


def _remember(out: dict) -> None:
    global _LAST
    from datetime import datetime, timezone

    with _LAST_LOCK:
        _LAST = dict(out, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))



def _cause(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def _note(dev: Any, reason: str) -> None:
    """Tell the driver this stop came from outside the running protocol (``external_stop``)."""
    note = getattr(dev, "note_external_stop", None)
    if callable(note):
        note(reason)


def _laser_off(reg: Any, dev_id: str, reason: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"device_id": dev_id, "kind": "laser", "action": "emission_off"}
    try:
        dev = reg.laser(dev_id)
        _note(dev, reason)
        state = dev.emission_off()               # never raises; the read-back, or None
    except Exception as exc:  # noqa: BLE001 - a device we cannot build must not stop the rest
        item.update(confirmed=False, cause=_cause(exc))
        return item
    item["emission"] = state
    item["confirmed"] = state is False
    report = getattr(dev, "last_off_report", None)
    if report:
        item["report"] = report
    return item


def _report_confirms(rep: Any) -> bool:
    """A driver report (``close_all_shutters()``) confirms "every shutter closed" only when it says
    so for every laser. No report (an older driver) and no exception count as confirmed."""
    if not isinstance(rep, dict):
        return True
    if rep.get("shutter_confirmed") is False:
        return False
    return rep.get("scope") != "selected_laser_only"


def _report_cause(rep: Any) -> str:
    if isinstance(rep, dict) and rep.get("scope") == "selected_laser_only" and rep.get("shutter_confirmed") is not False:
        return "only the selected laser was checked (close_every_laser=false)"
    return str((rep or {}).get("shutter") or "the driver did not confirm every shutter closed")[:300]


def _shutter_closed(reg: Any, dev_id: str, reason: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"device_id": dev_id, "kind": "microscope", "action": "close_shutter"}
    try:
        dev = reg.microscope(dev_id)
    except Exception as exc:  # noqa: BLE001
        item.update(confirmed=False, cause=_cause(exc))
        return item
    _note(dev, reason)
    # Stop a running sequence first (never raises by contract): a measurement must not go on with
    # light the operator withdrew, and the WITec visits the other lasers' shutters only while no
    # sequencer runs.
    try:
        dev.stop_all()
    except Exception:  # noqa: BLE001
        pass
    closer = getattr(dev, "close_all_shutters", None)
    try:
        if callable(closer):
            # every laser's shutter, not just the selected one; raises when one is not confirmed
            item["action"] = "close_all_shutters"
            rep = closer()
            item["confirmed"] = _report_confirms(rep)
            if rep:
                item["report"] = rep
            if not item["confirmed"]:
                item["cause"] = _report_cause(rep)
        else:
            now_open = dev.set_shutter(False)
            item["shutter_open"] = bool(now_open)
            item["confirmed"] = not now_open
    except Exception as exc:  # noqa: BLE001
        item.update(confirmed=False, cause=_cause(exc))
        rep = getattr(exc, "report", None)
        if rep:
            item["report"] = rep
    return item


def safe_off_all(reason: str = "", *, registry: Any = None) -> dict[str, Any]:
    """Switch every enabled laser off and close every enabled microscope's shutter.

    Returns ``{"reason", "devices": [{device_id, kind, action, confirmed, ...}],
    "all_confirmed"}``. Never raises."""
    out: dict[str, Any] = {"reason": str(reason or ""), "devices": [], "all_confirmed": True}
    try:
        if registry is None:
            from mast.instruments.registry import get_instrument_registry

            registry = get_instrument_registry()
        lasers = [str(e.get("id")) for e in registry.find_devices(kind="laser") if e.get("id")]
        scopes = [str(e.get("id")) for e in registry.find_devices(kind="microscope") if e.get("id")]
    except Exception as exc:  # noqa: BLE001
        logger.error("emission safety: cannot read the station config (%s); nothing was switched off",
                     _cause(exc))
        out.update(all_confirmed=False, error=_cause(exc))
        return out
    for dev_id in lasers:
        out["devices"].append(_laser_off(registry, dev_id, out["reason"] or "safe-off"))
    for dev_id in scopes:
        out["devices"].append(_shutter_closed(registry, dev_id, out["reason"] or "safe-off"))
    out["all_confirmed"] = all(d.get("confirmed") for d in out["devices"])
    if not out["devices"]:
        level = logging.INFO          # no lasers or microscopes configured (every STM station)
    else:
        level = logging.WARNING if out["all_confirmed"] else logging.ERROR
    if out["devices"]:
        _remember(out)
    logger.log(level, "emission safety (%s): %s", out["reason"] or "-",
               ", ".join(f"{d['device_id']}:{d['action']}={'ok' if d.get('confirmed') else 'NOT confirmed'}"
                         for d in out["devices"]) or "no lasers or microscopes configured")
    return out


def safe_off_all_in_background(reason: str = "", *, registry: Any = None,
                               on_done: Any = None) -> threading.Thread:
    """:func:`safe_off_all` on its own thread, so the HTTP request that withdrew the attestation
    is not held up by serial / COM round trips. ``on_done(result)`` is called with the result
    (its exceptions are logged and dropped)."""

    def _run() -> None:
        result = safe_off_all(reason, registry=registry)
        if on_done is not None:
            try:
                on_done(result)
            except Exception:  # noqa: BLE001
                logger.exception("emission safety: on_done failed")

    t = threading.Thread(target=_run, name="mast-emission-safe-off", daemon=True)
    t.start()
    return t


# ── the operator withdraws the attestation: wire safe_off_all to the optical-state write hook ──

#: the operator attestation every software emission path needs (design D7)
LASER_AREA_SLOT = "laser_area_safe"


def _attested_now(store: Any) -> bool:
    try:
        return bool(store.is_valid(LASER_AREA_SLOT, expect=True))
    except Exception:  # noqa: BLE001 - unreadable is not "safe"
        return False


def revocation_listener(store: Any, *, registry: Any = None):
    """A listener for ``OpticalStateStore.add_listener``: ``fn(slot_id, record)``.

    When the operator records ``laser_area_safe`` as anything but ``True``, or clears it, and
    the store does not say it is a valid ``True`` by the time we look (signed again in the
    meantime), everything is switched off in the background (the listener runs on the
    operator's HTTP request thread, which must not wait on serial or COM round trips)."""

    def laser_area_revocation(slot_id: str, record: dict | None) -> None:
        if slot_id != LASER_AREA_SLOT:
            return
        if record is not None and record.get("value") is True:
            return
        if _attested_now(store):
            return
        who = str((record or {}).get("set_by") or "the operator")[:80]
        what = "cleared" if record is None else f"recorded as {record.get('value')!r}"
        logger.warning("laser_area_safe %s by %s: switching every light source off", what, who)
        safe_off_all_in_background(f"laser_area_safe {what} by {who}", registry=registry)

    return laser_area_revocation


def install_revocation_listener(store: Any, *, registry: Any = None):
    """Register :func:`revocation_listener` on *store* (listeners belong to one store instance:
    call it again after ``operator_hitl.service.install()`` swaps the store). Returns it."""
    fn = revocation_listener(store, registry=registry)
    store.add_listener(fn)
    return fn
