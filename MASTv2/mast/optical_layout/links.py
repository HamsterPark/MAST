"""What the components' links point at *right now*: a read-only picture, never an action.

A component may reference a MAST device (``links.device_id``) and a manual optical-state slot
(``links.slot``). The references are only references - a layout can be saved while the device is
disabled or the slot does not exist. When a layout is read, this module attaches the current
state of each reference:

* a device: from the instrument registry's *configuration* (``list_devices`` never touches
  hardware): ``enabled``, ``connected`` (open in this process); if the process already has the
  device built and it has a status reader, also ``status`` - read with the same bounded, de-duplicated
  call the briefing uses (``station_view.device_status``), in parallel, so a device that does not
  answer costs about two seconds in total, not per device. **A device is never opened for this.**
* a slot: the operator-signed value from the optical-state store's snapshot (memory only).

Nothing here raises: a failure becomes an ``error`` in the entry it concerns.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from mast.optical_layout.model import Layout

logger = logging.getLogger(__name__)

_DEVICE_KEYS = ("name", "type", "kind", "enabled", "connected")


def links_state(layout: Layout, *, registry: Any = None, read_status: bool = True) -> dict[str, dict[str, Any]]:
    """``{component_id: {"device": {...}, "slot": {...}}}`` for the components that have a link
    (an entry only has the keys the component has links for)."""
    linked = [c for c in layout.components if c.links.device_id or c.links.slot]
    if not linked:
        return {}
    devices = _device_states({c.links.device_id for c in linked if c.links.device_id}, registry, read_status)
    slots = _slot_states({c.links.slot for c in linked if c.links.slot})
    out: dict[str, dict[str, Any]] = {}
    for c in linked:
        entry: dict[str, Any] = {}
        if c.links.device_id:
            entry["device"] = devices[c.links.device_id]
        if c.links.slot:
            entry["slot"] = slots[c.links.slot]
        out[c.id] = entry
    return out


def _device_states(ids: set[str], registry: Any, read_status: bool) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    try:
        if registry is None:
            from mast.instruments.registry import get_instrument_registry

            registry = get_instrument_registry()
        listing = {str(d.get("id")): d for d in registry.list_devices()}
        built = dict(registry.built_station_devices()) if read_status else {}
    except Exception as exc:  # noqa: BLE001 - reported in the entries
        reason = f"{type(exc).__name__}: {exc}"[:200]
        logger.debug("optical layout links: the device registry is unreadable (%s)", reason)
        return {i: {"id": i, "known": None, "error": reason} for i in ids}
    out: dict[str, dict[str, Any]] = {}
    to_read: list[tuple[str, Any, str]] = []
    for did in sorted(ids):
        d = listing.get(did)
        if d is None:
            out[did] = {"id": did, "known": False}
            continue
        out[did] = {"id": did, "known": True, **{k: d.get(k) for k in _DEVICE_KEYS}}
        dev = built.get(did)
        if dev is not None:
            to_read.append((did, dev, str(d.get("kind"))))
    if to_read:
        for did, status in _read_statuses(to_read):
            if "error" in status and len(status) == 1:
                out[did]["status_error"] = status["error"]
            else:
                out[did]["status"] = status
    return out


def _read_statuses(items: list[tuple[str, Any, str]]) -> list[tuple[str, dict]]:
    from mast import station_view

    def one(item: tuple[str, Any, str]) -> tuple[str, dict]:
        did, dev, kind = item
        return did, station_view.device_status(dev, kind)

    try:
        with ThreadPoolExecutor(max_workers=min(8, len(items)), thread_name_prefix="layout-links") as pool:
            return list(pool.map(one, items))
    except Exception as exc:  # noqa: BLE001
        return [(did, {"error": f"{type(exc).__name__}: {exc}"[:200]}) for did, _, _ in items]


def _slot_states(ids: set[str]) -> dict[str, dict[str, Any]]:
    from mast import station_view

    state = station_view.manual_state()
    if state.get("error"):
        return {i: {"id": i, "known": None, "error": state["error"]} for i in ids}
    by_id = {s["id"]: s for s in state.get("slots", [])}
    out: dict[str, dict[str, Any]] = {}
    for sid in sorted(ids):
        s: Optional[dict] = by_id.get(sid)
        if s is None:
            out[sid] = {"id": sid, "known": False}
        else:
            out[sid] = {"id": sid, "known": True, "label": s.get("label"), "kind": s.get("kind"),
                        "value": s.get("value"), "valid": bool(s.get("valid")), "unit": s.get("unit"),
                        "set_by": s.get("set_by"), "set_at": s.get("set_at"),
                        "valid_until": s.get("valid_until")}
    return out


__all__ = ["links_state"]
