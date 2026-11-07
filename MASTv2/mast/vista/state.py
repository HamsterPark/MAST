"""State echo: controller readbacks after an action, in the model's units.

Ported from VISTA-SPM ``host.py`` (``_state_echo`` and ``_STATE_MAP``). Explicit per-skill
mapping; an unreadable value is left out, never guessed.

MAST additions:

* **Feedback state.** ``GetZControllerState`` adds ``z_controller_on``, ``z_controller_status``
  and ``withdrawn``, decided by :func:`withdrawn_from`, the rule ``mast_status`` uses
  (``InstrumentState.refresh`` in :mod:`mast.core.state`), so an observation and the status never
  disagree about whether the tip is withdrawn. Without it an observation said what the current
  was but not whether the feedback that set it was on.
* **The current is read last, and checked against the withdrawn tip.** A controller's current
  reading can lag the junction. On the Nanonis simulator the reading right after ``WithdrawTip``
  was still the tunnelling setpoint with Z already at the top of its range, then decayed over
  several seconds toward zero. A withdrawn tip carries no
  tunnelling current, so with ``withdrawn`` true a reading at or above
  :func:`settled_threshold_a` is reported with ``current_settling: true`` and the seconds it was
  re-read for (``current_settle_s``). After an action the echo re-reads it for at most
  ``settle_budget_s`` first; an observation does not wait. The value is never replaced by a
  guess. With the feedback on, or with the tip held at height, it is one read as before.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable

#: Read in this order; the current is read after them (:func:`state_echo`).
STATE_SKILLS: tuple[str, ...] = ("GetBias", "GetSetpoint", "GetZControllerState", "GetZPosition",
                                 "GetScanXYPosition", "GetDriftCompensation")
CURRENT_SKILL = "GetCurrent"

#: ``ZCtrl_StatusGet`` code for "On" (the code ``mast.core.state`` reads ``z_controller_on`` from).
ZCTRL_ON_CODE = 2
#: Re-reading the current of a withdrawn tip after an action: at most this long, this often.
CURRENT_SETTLE_BUDGET_S = 3.0
CURRENT_SETTLE_POLL_S = 0.25
#: A withdrawn tip's current counts as settled below max(floor, fraction x |setpoint|). Not 1-2 %:
#: a current amplifier commonly sits a few pA off zero, and a threshold below that offset would
#: flag every withdrawal and make every withdraw action wait the whole budget.
CURRENT_SETTLED_FLOOR_A = 1e-12
CURRENT_SETTLED_FRACTION = 0.10

#: Key order of the echo (the model reads it as one compact JSON object); others follow.
_ORDER = ("bias_v", "setpoint_pa", "current_pa", "current_settling", "current_settle_s", "z_nm",
          "z_controller_on", "withdrawn", "z_controller_status", "z_controller_note",
          "tip_x_nm", "tip_y_nm", "drift_compensation")


def _num(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
            return float(v)
    return None


def _scaled(v: float | None, factor: float, nd: int) -> float | None:
    return None if v is None else round(v * factor, nd)


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _first_int(v: Any) -> int | None:
    """An integer status code, bare or as the single element of a list (decoded replies)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, (list, tuple)) and len(v) == 1:
        return _first_int(v[0])
    return None


def _first_number(v: Any) -> float | None:
    """The first element of a numeric pair such as ``ZCtrl_LimitsGet``'s ``[high, low]``."""
    if isinstance(v, (list, tuple)) and v:
        x = v[0]
        if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x)):
            return float(x)
    return None


def withdrawn_from(z_controller_on: bool | None, z_pos_m: float | None,
                   z_high_limit_m: float | None) -> bool | None:
    """The ``withdrawn`` rule of ``mast_status`` (``InstrumentState.refresh``, mast.core.state):
    feedback off and Z at its upper limit. Feedback on: False. Otherwise unknown (None).
    ``tests/v2/vista/test_state.py`` holds the two to the same answers."""
    if z_controller_on is False and z_pos_m is not None and z_high_limit_m is not None:
        return abs(z_pos_m - z_high_limit_m) < 1e-12
    return False if z_controller_on else None


def _zctrl_state(d: dict) -> dict:
    """``GetZControllerState`` → feedback on/off, the module's status name, withdrawn."""
    code = _first_int(d.get("module_status"))
    rt = d.get("controller_on")
    # mast_status reads the module (ZCtrl_StatusGet); the real-time controller only fills in
    # when the module could not be read
    on = (code == ZCTRL_ON_CODE) if code is not None else (rt if isinstance(rt, bool) else None)
    name = d.get("module_status_name")
    out = {"z_controller_on": on,
           "z_controller_status": name if isinstance(name, str) else None,
           "withdrawn": withdrawn_from(on, _num(d, "z_m"), _first_number(d.get("z_limits")))}
    note = d.get("disagreement")
    if isinstance(note, str) and note.strip():
        out["z_controller_note"] = note.strip()[:300]
    return _drop_none(out)


STATE_MAP: dict[str, Callable[[dict], dict]] = {
    "GetBias": lambda d: _drop_none({"bias_v": _scaled(_num(d, "bias_v", "bias"), 1.0, 6)}),
    "GetSetpoint": lambda d: _drop_none({"setpoint_pa": _scaled(_num(d, "setpoint_a", "setpoint"), 1e12, 3)}),
    "GetCurrent": lambda d: _drop_none({"current_pa": _scaled(_num(d, "current_a", "current"), 1e12, 3)}),
    "GetZPosition": lambda d: _drop_none({"z_nm": _scaled(_num(d, "z_pos_m", "z_m", "z"), 1e9, 4)}),
    "GetZControllerState": _zctrl_state,
    "GetScanXYPosition": lambda d: _drop_none({"tip_x_nm": _scaled(_num(d, "x_m", "x"), 1e9, 4),
                                               "tip_y_nm": _scaled(_num(d, "y_m", "y"), 1e9, 4)}),
    "GetDriftCompensation": lambda d: {"drift_compensation": _drop_none({
        "on": d.get("enabled") if isinstance(d.get("enabled"), bool) else None,
        "vx_nm_s": _scaled(_num(d, "vx"), 1e9, 5), "vy_nm_s": _scaled(_num(d, "vy"), 1e9, 5),
        "vz_nm_s": _scaled(_num(d, "vz"), 1e9, 5),
        "saturated": (any(bool(d.get(k)) for k in ("x_saturated", "y_saturated", "z_saturated"))
                      if any(k in d for k in ("x_saturated", "y_saturated", "z_saturated")) else None)})},
}


def normalise_reading(key: str, value: Any) -> tuple[str | None, Any]:
    """Controller readback → model units. Unknown or non-scalar fields are dropped."""
    if isinstance(value, bool):
        return key, value
    if not isinstance(value, (int, float)):
        return None, None
    v = float(value)
    if key.endswith("_a"):
        return key[:-2] + "_pa", round(v * 1e12, 3)
    if key.endswith("_m"):
        return key[:-2] + "_nm", round(v * 1e9, 4)
    if key.endswith("_m_s"):
        return key[:-4] + "_nm_s", round(v * 1e9, 5)
    return key, (round(v, 6) if abs(v) < 1e6 else v)


def settled_threshold_a(setpoint_a: float | None) -> float:
    """Below this a withdrawn tip's current reading counts as settled (A)."""
    sp = abs(float(setpoint_a)) if isinstance(setpoint_a, (int, float)) and not isinstance(
        setpoint_a, bool) and math.isfinite(float(setpoint_a)) else 0.0
    return max(CURRENT_SETTLED_FLOOR_A, CURRENT_SETTLED_FRACTION * sp)


def _read(run: Callable[[str, dict], Any], name: str) -> dict | None:
    try:
        res = run(name, {})
    except Exception:  # noqa: BLE001 — a readback that fails is left out
        return None
    d = getattr(res, "data", None)
    if not getattr(res, "success", False) or not isinstance(d, dict):
        return None
    return d


def _current_a(run: Callable[[str, dict], Any]) -> float | None:
    d = _read(run, CURRENT_SKILL)
    return None if d is None else _num(d, "current_a", "current")


def _echo_current(run: Callable[[str, dict], Any], out: dict, *, budget_s: float, poll_s: float,
                  sleep: Callable[[float], None], monotonic: Callable[[], float]) -> None:
    cur = _current_a(run)
    if cur is None:
        return
    settled, rereads, waited = True, 0, 0.0
    if out.get("withdrawn") is True:
        sp = out.get("setpoint_pa")
        threshold = settled_threshold_a(sp * 1e-12 if isinstance(sp, (int, float)) else None)
        t0 = monotonic()
        while abs(cur) >= threshold:
            if monotonic() - t0 + poll_s > budget_s:
                settled = False
                break
            sleep(poll_s)
            nxt = _current_a(run)
            rereads += 1
            if nxt is None:            # the reread failed: the last reading stands, unconfirmed
                settled = False
                break
            cur = nxt
        waited = monotonic() - t0
    out["current_pa"] = round(cur * 1e12, 3)
    if not settled:
        out["current_settling"] = True
    if rereads or not settled:
        out["current_settle_s"] = round(waited, 2)


def state_echo(run: Callable[[str, dict], Any], skills: tuple[str, ...] = STATE_SKILLS, *,
               settle_budget_s: float = CURRENT_SETTLE_BUDGET_S,
               settle_poll_s: float = CURRENT_SETTLE_POLL_S,
               sleep: Callable[[float], None] = time.sleep,
               monotonic: Callable[[], float] = time.monotonic) -> dict:
    """Run the read skills through ``run(name, params)`` and map their data; then the current
    (see the module docstring). ``settle_budget_s=0``: one current read, flagged, never waited."""
    out: dict = {}
    for name in skills:
        if name == CURRENT_SKILL:
            continue                     # read last, below
        d = _read(run, name)
        if d is None:
            continue
        mapper = STATE_MAP.get(name)
        if mapper is not None:
            out.update(mapper(d))
        else:
            for k, v in d.items():
                nk, nv = normalise_reading(k, v)
                if nk is not None:
                    out[nk] = nv
    _echo_current(run, out, budget_s=max(0.0, float(settle_budget_s)),
                  poll_s=max(0.01, float(settle_poll_s)), sleep=sleep, monotonic=monotonic)
    ordered = {k: out[k] for k in _ORDER if k in out}
    ordered.update({k: v for k, v in out.items() if k not in ordered})
    return ordered


#: the optical station's read-only skills (contract §6.3), by the device kind they report.
#: ``optical_echo`` does NOT call them: a skill may open the device it reads, and an
#: observation must never be what takes a port away from the vendor software. It reads
#: what the process already has open, through the registry (``mast.station_view``).
OPTICAL_STATE_SKILLS: tuple[str, ...] = ("GetWITecStatus", "GetLaserStatus", "ReadTemperature",
                                         "ReadOpticalState")

#: device kind -> key of the echo
_OPTICAL_KEYS = {"microscope": "microscope", "laser": "lasers", "temperature": "temperature",
                 "power_meter": "power_meter"}


def optical_echo(*, timeout_s: float | None = None, registry: Any = None) -> dict:
    """The optical station's state echo: what ``state_echo`` is to the STM.

    ``microscope`` / ``lasers`` / ``temperature`` / ``power_meter``
        the status of each device the process has open, keyed by device id (a device
        that is configured but not open is listed under ``devices_closed`` and is not
        opened);
    ``optical_state``
        the manual optical state a person signed (``{slot: value}`` for the slots that
        are set and still valid; ``stale`` lists the ones that expired, ``unset`` the
        ones nobody has signed);
    ``operator_prompts_open``
        how many questions are waiting for the operator.

    Nothing is guessed and nothing is opened: a status that could not be read is
    reported as an ``error`` string next to the device.
    """
    from mast import station_view as sv

    out: dict[str, Any] = {}
    inv = sv.inventory(registry, read_status=True,
                       timeout_s=sv.STATUS_TIMEOUT_S if timeout_s is None else timeout_s)
    if inv.get("error"):
        out["devices_error"] = inv["error"]
    closed: list[str] = []
    for d in inv.get("devices", []):
        if not d.get("enabled", True):
            continue
        key = _OPTICAL_KEYS.get(str(d.get("kind")))
        if key is None:
            continue
        if not d.get("connected"):
            closed.append(str(d.get("id")))
            continue
        row = d.get("status") if "status" in d else {"error": d.get("status_error", "not readable")}
        out.setdefault(key, {})[str(d.get("id"))] = row
    if closed:
        out["devices_closed"] = closed
    ms = sv.manual_state()
    if ms.get("error"):
        out["optical_state_error"] = ms["error"]
    else:
        valid = {s["id"]: s["value"] for s in ms["slots"] if s["valid"]}
        stale = [s["id"] for s in ms["slots"] if s["value"] is not None and not s["valid"]]
        unset = [s["id"] for s in ms["slots"] if s["value"] is None]
        out["optical_state"] = {k: v for k, v in (("signed", valid), ("expired", stale),
                                                  ("unset", unset)) if v}
    pr = sv.open_prompts(limit=0)
    if pr.get("count") is not None:
        out["operator_prompts_open"] = pr["count"]
    return out


__all__ = ["CURRENT_SETTLE_BUDGET_S", "CURRENT_SKILL", "OPTICAL_STATE_SKILLS", "STATE_MAP",
           "STATE_SKILLS", "normalise_reading", "optical_echo", "settled_threshold_a",
           "state_echo", "withdrawn_from"]
