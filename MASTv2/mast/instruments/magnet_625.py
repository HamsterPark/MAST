"""Lake Shore Model 625 superconducting-magnet supply: field control (``SETI`` / ``STOP``).

Read ``environment/lakeshore_625.py`` first. That module is the read-only driver, and
this one writes through its link (:meth:`Model625Link.session`). Two facts about the
625 shape everything below:

* ``SETI <A>`` does not stage a value. The output starts ramping to it the moment the
  command is parsed; there is no "go" step.
* The 625 has no watchdog. Once ``SETI`` is out, the supply ramps to it even if MAST,
  the PC or the cable dies. So every check happens before the command. Nothing
  afterwards can take the command back; ``STOP`` only freezes the output where it is.

This module may write two commands and nothing else. :func:`build_write` is its only
byte factory, and tests pin that.

* ``SETI <A>``: the target current, formatted from a float. Negative current (reverse
  field) is allowed, only within the supply's own LIMIT (a magnitude,
  manual 4.11.1) and only under the reverse-field gates below. :func:`build_write` holds the
  last guard: no value outside ``+-LIMIT``, no NaN, no inf.
* ``STOP``: freeze the output at its present value. Never gated, with one exception:
  it is not sent while the supply is protecting the magnet by itself (see below).

It never writes the ramp rate, the segment table, the limits, the field constant, the
persistent switch, the quench settings, the interface mode or the error latch. Those
belong to the instrument and the operator. The ramp speed in particular is the 625's
own segment program (``RSEG``/``RSEGS``), checked by the operator against the magnet
vendor's limits; MAST refuses to change the field while segment ramping is off.

Targets are in tesla, because people and agents think in field. On the wire it is
current. The conversion uses the field constant the operator entered at
initialization, cross-checked against the supply's own ``FLDS?`` before every change.
``DFLT`` resets FLDS to 0.1 T/A, so trusting FLDS alone would turn "1 T" into 3.4 T on
a 0.34 T/A magnet.

Gates (:meth:`Magnet625Controller.plan`). Each one refuses; none clamps.

* Any change of field:
  - the maximum field and the field constant are set at initialization;
  - the target is a finite number (not a bool), ``|target| <= max field`` (the maximum is
    a magnitude; a negative target is a reverse field);
  - the supply answers as a Model 625 and every status field was read;
  - no error bit is set;
  - FLDS? is in T/A and agrees with the configured constant within 0.5 %;
  - the target current is within the supply's own LIMIT, by magnitude;
  - output is internally programmed (XPGM 0);
  - there is no persistent switch (MAST does not run persistent mode);
  - segment ramping is on;
  - the output is not moving;
  - the ramp does not cross zero (output and target on opposite sides, each beyond the
    current tolerance): go to 0 first, then start a second ramp. Going to 0 from either
    side is allowed;
  - a negative target needs the ramp segments to be known to act on ``|I|``. The manual
    never says they do (RSEGS lists 0 to +60.1 A only, and the rate changes "based on
    the output current setting"). While ``magnet_negative_segments_verified`` is off, a
    negative target whose ``|I|`` exceeds the first segment's upper current is refused;
    the operator turns the flag on after seeing ``RATE?`` change at that boundary on the
    negative side. While it is on, the segments are taken to act on ``|I|``. Unverified, the
    supply's ``RATE`` must also not be faster than the first segment's rate (it may use RATE
    on the negative side). Only the TARGET is looked at: a ramp from a negative current
    beyond the boundary back toward 0 (-12 A to 0) is allowed, since lowering is the safe
    direction;
  - no sample-stage heating: every Lake Shore 335 heater output reads off (``RANGE? 0``
    on outputs 1 and 2), no output in zone mode still ramps its setpoint (the next zone
    could turn its heater on by itself), and MAST's own heating is off (a change, the
    watch, an off retry, a zone table / control mode write). Heating and field changes
    never happen at the same time (a design rule). With no 335 here the
    gate passes and says so; a 335 whose RANGE?, OUTMODE? or (in zone mode) RAMPST? does
    not answer refuses (unknown is not an answer). The 335 is asked in the plan, outside
    this link; holding the link, :meth:`start` asks again only MAST's own heating, from
    memory, so the two links are never held at once;
  - the tip is withdrawn. This is the default; the caller, an external agent
    included, may waive it.
* Raising ``|B|`` additionally needs:
  - the user's own words (``user_authorization``) and the field the user authorized
    (``authorized_up_to_t``, a magnitude). ``|target|`` must not exceed it, and a field
    the words themselves name, on the target's side of zero, must agree with it. A
    negative target needs words that name a negative / reverse field ("-2 T", "负2T",
    "反向2T", "reverse 2 T"); words that name only a positive field do not authorize it,
    and words that name only a negative field do not authorize a positive target. The
    helium level cannot be read here; a verbal go-ahead is the operator's condition;
  - quench detection on;
  - the magnet thermometer named at initialization, read in the last
    ``TEMPERATURE_MAX_AGE_S``, plausibly valid (a Lake Shore reports 0 for an invalid
    input) and at or below the limit entered there. The thermometer is not accurate
    and shifts with field, so the limit is the operator's number, not a constant here.

Locking. The slow checks run without holding the link: ``read_state``, the 2 s motion
window, the thermometer and the tip. The thermometer path takes the environment
monitor's lock, and the monitor's own ``stop()`` closes links while holding that lock,
so holding the link around it could deadlock. Only then is the link held, briefly:
re-read SETI?/RDGI?/OPST?/ERST?, refuse if anything changed since the plan, write SETI,
read it back.

Supervision (:meth:`Magnet625Controller.supervise`) polls once a second and reads the
status first:
* Nothing is written when the supply protects the magnet by itself (any hardware
  error, which throws the crowbar, or quench / crowbar discharge / remote inhibit,
  where it drives the output to zero), or when the set point was changed elsewhere
  (do not fight the operator).
* ``STOP`` on abort or cancel, another error bit, three failed reads in a row, a
  stalled ramp, or the supervision cap.
* A STOP that did not go out is retried every poll for up to ``STOP_GIVE_UP_S``, and
  the ramp stays "active" meanwhile. "Stopped" is reported only for a STOP that went
  out.
* Arrival is the ramp-done bit together with SETI? still equal to the target, seen
  three times in a row. It does not wait for ``|I - target|`` to fall under some
  tolerance: the supply's setting and read accuracy (±10 mA ±0.05 %, ±1 mA ±0.05 %)
  can leave tens of mA, and the residual is reported instead.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mast.environment.lakeshore_625 import (
    FIELD_UNITS,
    Magnet625State,
    Model625Error,
    Model625Link,
    WrongInstrument,
)
from mast.environment.serial_transport import SerialUnavailable
from mast.i18n import tr

logger = logging.getLogger(__name__)

TERMINATOR = b"\r\n"
#: The 625's output range is ±60.1 A; MAST asks for at most this in magnitude.
MAX_COMMAND_A = 60.0
CURRENT_DECIMALS = 4
#: |FLDS? - configured| / configured above this is refused.
FIELD_CONSTANT_REL_TOL = 0.005
#: How far apart the output and a current can be and still be "the same", for the
#: raise / nothing-to-do decisions: the 625's setting accuracy, ±10 mA ±0.05 % of the
#: setting (manual §1.3).
CURRENT_TOL_A = 0.010
CURRENT_TOL_REL = 0.0005
#: A SETI? read-back within this of what was sent counts as the same set point.
SETI_READBACK_TOL_A = 0.0002
#: Between the plan and the write the output may drift at most this (an idle or
#: frozen supply); more means it moved, and the change is refused.
PREP_DRIFT_TOL_A = 0.010
#: The magnet thermometer reading may be at most this old (s).
TEMPERATURE_MAX_AGE_S = 60.0
#: Below this a reading is not a temperature: a Lake Shore input reports 0 when the
#: sensor is open or over range, and the monitor never asks RDGST?.
TEMPERATURE_MIN_VALID_K = 0.1
#: Shortest user-authorization text accepted for raising the field.
MIN_AUTHORIZATION_CHARS = 2
#: The examples in the skill's own documentation: copied verbatim, they are not a
#: user's words.
EXAMPLE_AUTHORIZATIONS = frozenset({
    "液面够，可以升到 1 t", "helium is fine, go to 1 t",
})

#: "Is the output moving?" when the ramp-done bit is not set: two current reads this
#: far apart and a change above the threshold. A slow segment can be 1 mA/s,
#: i.e. 2 mA over the window, against a 0.1 mA readout.
MOTION_WINDOW_S = 2.0
MOTION_THRESHOLD_A = 0.001

POLL_S = 1.0
DONE_CONFIRMATIONS = 3
READ_FAILURES_BEFORE_STOP = 3
#: No movement of at least STALL_MIN_PROGRESS_A within this window, and not arrived:
#: stalled.
STALL_WINDOW_S = 180.0
STALL_MIN_PROGRESS_A = 0.002
MAX_SUPERVISE_S = 6 * 3600.0
#: How long the link may be waited for by one STOP attempt, and how long supervision
#: keeps retrying a STOP that does not go out before it gives up and says so.
STOP_LOCK_TIMEOUT_S = 10.0
STOP_GIVE_UP_S = 120.0

#: Profile keys (``core/instrument_profile``), entered on the initialization page.
PROFILE_MAX_FIELD = "magnet_max_field_t"
PROFILE_FIELD_CONSTANT = "magnet_field_constant_t_per_a"
PROFILE_TEMPERATURE_CHANNEL = "magnet_temperature_channel"
PROFILE_MAX_TEMPERATURE = "magnet_max_temperature_k"
#: "yes" only after the operator has seen RATE? change at the first segment boundary on
#: the negative side (stored as a yes / no choice, read as a bool; anything else = False).
PROFILE_NEGATIVE_SEGMENTS_VERIFIED = "magnet_negative_segments_verified"

# Operational error bits (ERST? 2nd field) on which the 625 protects the magnet by
# itself: remote inhibit (4: output set to zero), quench (5: output to 0 A), crowbar
# discharge (6). Every hardware error (1st field) throws the crowbar (manual 7-4).
_SELF_PROTECTION_OP_BITS = (1 << 4) | (1 << 5) | (1 << 6)

# What marks the sign of a field the words name:
# * a sign WORD touching the number is negative: "负2T", "反向2T", "反向场 2 T", "reverse 2 T",
#   "negative field of 2 T", "minus 2 T". The single character 负 alone is not a word here
#   ("负责", "负载"), only 负 + 向 / 的 / 场 / 磁场 or touching the number;
# * a dash (- U+2212 U+FF0D U+FE63) touching the number is a minus sign ("升到-2T", "B = -2 T"),
#   unless what comes before it (spaces skipped) is a digit, a point or a number with its unit:
#   then it is a range ("1-2 T", "1T-2T", "1T－2T"). A dash with a space after it, and the en
#   dash, are never a sign ("1 T - 2 T", "0 – 2 T");
# * an explicit plus ("+2 T", "正2T", "正向2T", "positive 2 T") says the value is positive.
# The look-behind names digits and the decimal point, not \w: \w includes CJK characters,
# so "升到1T" used to name no field at all and the consistency check was skipped.
_NEG_WORD = (
    r"(?:负(?:向|的|磁?场)?|反向(?:磁?场)?|反转"
    r"|(?<![A-Za-z])(?:reverse[ds]?|negative|minus)(?:\s+field)?(?:\s+(?:of|to))?(?![A-Za-z]))")
_POS_WORD = (
    r"(?:(?<![0-9.])\+|正向?"
    r"|(?<![A-Za-z])positive(?:\s+field)?(?:\s+(?:of|to))?(?![A-Za-z]))")
_FIELD_IN_TEXT = re.compile(
    rf"(?:(?P<neg>{_NEG_WORD})\s*|(?P<pos>{_POS_WORD})\s*|(?P<dash>[-\u2212\uff0d\ufe63]))?"
    r"(?<![0-9.])(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>mT|T|tesla|特斯拉|特)(?![A-Za-z])",
    re.IGNORECASE)
_RANGE_BEFORE_DASH = re.compile(r"(?:[0-9.]|\d\s*(?:mT|T|tesla|特斯拉|特))$", re.IGNORECASE)
#: A sign word the words use WITHOUT a number touching it ("把磁场反向升到 2T").
_DETACHED_SIGN_WORD = re.compile(
    r"反向|负向|负的|负场|负磁场|反转|(?<![A-Za-z])(?:reverse[ds]?|negative|minus)(?![A-Za-z])",
    re.IGNORECASE)


class FieldControlError(RuntimeError):
    """Field control could not do what was asked (the message says why)."""


def _protecting(hw: int, op: int, psh: int) -> bool:
    """The supply is protecting the magnet by itself: write nothing."""
    return bool(hw) or bool(op & _SELF_PROTECTION_OP_BITS)


def _current_tol(i: "float | None") -> float:
    return CURRENT_TOL_A + CURRENT_TOL_REL * abs(float(i or 0.0))


def _side(i: float) -> int:
    """Which side of zero a current is on: +1 / -1, or 0 within the current tolerance."""
    tol = _current_tol(i)
    return 1 if i > tol else -1 if i < -tol else 0


def scan_fields(text: str) -> "tuple[list[tuple[float, bool]], bool]":
    """``([(signed value in T, explicitly positive), ...], detached_sign_word)``.

    The second item is True when the text uses a sign word ("反向", "reverse", "minus", ...)
    that does not touch any number, so the sign of the field it speaks of is unclear.
    """
    s = str(text or "")
    out: list[tuple[float, bool]] = []
    rest = s
    for m in _FIELD_IN_TEXT.finditer(s):
        try:
            v = float(m.group("num"))
        except ValueError:
            continue
        if m.group("unit").lower() == "mt":
            v /= 1000.0
        negative = bool(m.group("neg"))
        if m.group("dash") is not None:
            negative = not _RANGE_BEFORE_DASH.search(s[:m.start("dash")].rstrip())
        out.append((-v if negative else v, bool(m.group("pos"))))
        rest = rest.replace(m.group(0), " " * len(m.group(0)), 1)
    return out, bool(_DETACHED_SIGN_WORD.search(rest))


def signed_fields_named_in(text: str) -> list[float]:
    """Field values (tesla) a text names, negative where the words mark a reverse field.

    ``"升到 1 T"`` -> ``[1.0]``, ``"500 mT"`` -> ``[0.5]``, ``"反向2T"`` -> ``[-2.0]``,
    ``"1T-2T"`` -> ``[1.0, 2.0]`` (a range, not a sign).
    """
    return [v for v, _ in scan_fields(text)[0]]


def fields_named_in(text: str) -> list[float]:
    """Field magnitudes (tesla) a text names: ``"升到 1 T"`` -> ``[1.0]``, ``"-500 mT"`` -> ``[0.5]``."""
    return [abs(v) for v in signed_fields_named_in(text)]


# ── the only byte factory ─────────────────────────────────────────────────────
def build_write(verb: str, current_a: "float | None" = None, *,
                limit_a: "float | None" = None) -> bytes:
    """``SETI <A>`` or ``STOP``, CR LF terminated. Anything else raises ``ValueError``.

    The last guard on the current: finite, within ``+-MAX_COMMAND_A``, and within
    ``+-limit_a`` (the supply's LIMIT, a magnitude) when given. A negative current
    (reverse field) is refused unless *limit_a* is given: it is only ever sent inside the
    instrument's own limit.
    """
    v = str(verb).strip().upper()
    if v == "STOP":
        if current_a is not None:
            raise ValueError("STOP takes no value")
        return b"STOP" + TERMINATOR
    if v != "SETI":
        raise ValueError(f"not a field-control command: {verb!r}")
    if isinstance(current_a, bool) or not isinstance(current_a, (int, float)):
        raise ValueError(f"SETI needs a number, got {current_a!r}")
    x = float(current_a)
    if not math.isfinite(x):
        raise ValueError(f"SETI needs a finite number, got {current_a!r}")
    ceiling = MAX_COMMAND_A
    if limit_a is not None:
        if isinstance(limit_a, bool) or not isinstance(limit_a, (int, float)):
            raise ValueError(f"the instrument LIMIT must be a number, got {limit_a!r}")
        lim = _finite(limit_a)
        if lim is None or lim < 0.0:
            raise ValueError(f"the instrument LIMIT must be a finite number >= 0, got {limit_a!r}")
        ceiling = min(ceiling, lim)
    elif x < 0.0:
        raise ValueError("negative current (reverse field) needs the instrument's LIMIT")
    if abs(x) > ceiling:
        raise ValueError(f"{x} A is outside +-{ceiling} A (the command ceiling / the instrument LIMIT)")
    x = round(x, CURRENT_DECIMALS) + 0.0           # + 0.0: never format "-0.0000"
    if abs(x) > ceiling:
        raise ValueError(f"{x} A is outside +-{ceiling} A (the command ceiling / the instrument LIMIT)")
    return f"SETI {x:.{CURRENT_DECIMALS}f}".encode("ascii") + TERMINATOR


# ── settings (entered on the initialization page) ─────────────────────────────
@dataclass(frozen=True)
class MagnetSettings:
    max_field_t: "float | None" = None
    field_constant_t_per_a: "float | None" = None
    temperature_channel: str = ""
    max_temperature_k: "float | None" = None
    #: The operator has seen the ramp segments act on |I| on the negative side.
    negative_segments_verified: bool = False

    @classmethod
    def from_profile(cls) -> "MagnetSettings":
        try:
            from mast.core.instrument_profile import get_config
        except Exception:  # noqa: BLE001 - no profile module: nothing configured
            return cls()

        def _f(key: str) -> "float | None":
            return _finite(get_config(key, None))

        return cls(
            max_field_t=_f(PROFILE_MAX_FIELD),
            field_constant_t_per_a=_f(PROFILE_FIELD_CONSTANT),
            temperature_channel=str(get_config(PROFILE_TEMPERATURE_CHANNEL, "") or "").strip(),
            max_temperature_k=_f(PROFILE_MAX_TEMPERATURE),
            negative_segments_verified=str(
                get_config(PROFILE_NEGATIVE_SEGMENTS_VERIFIED, "no") or "").strip().lower() == "yes",
        )

    @property
    def control_configured(self) -> bool:
        """Max field and field constant are set: MAST may change the field at all."""
        return (self.max_field_t is not None and self.max_field_t >= 0
                and self.field_constant_t_per_a is not None and self.field_constant_t_per_a > 0)

    def as_dict(self) -> dict:
        return {"max_field_t": self.max_field_t,
                "field_constant_t_per_a": self.field_constant_t_per_a,
                "temperature_channel": self.temperature_channel,
                "max_temperature_k": self.max_temperature_k,
                "negative_segments_verified": self.negative_segments_verified,
                "control_configured": self.control_configured}


# ── the plan: what a change would do, and every gate's verdict ────────────────
@dataclass
class Check:
    id: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {"id": self.id, "ok": self.ok, "detail": self.detail}


@dataclass
class FieldPlan:
    target_t: "float | None"
    target_a: "float | None" = None
    present_a: "float | None" = None
    present_setpoint_a: "float | None" = None
    #: The supply's LIMIT (a magnitude), as read for this plan; build_write is bound by it.
    limit_a: "float | None" = None
    field_constant_t_per_a: "float | None" = None
    raising: "bool | None" = None
    noop: bool = False
    eta_s: "float | None" = None
    checks: list[Check] = field(default_factory=list)

    def add(self, cid: str, ok: bool, detail: str) -> bool:
        self.checks.append(Check(cid, bool(ok), detail))
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def refusals(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def refusal_text(self) -> str:
        return "; ".join(f"[{c.id}] {c.detail}" for c in self.refusals)

    def as_dict(self) -> dict:
        return {
            "target_t": self.target_t, "target_a": self.target_a,
            "present_a": self.present_a, "present_setpoint_a": self.present_setpoint_a,
            "present_t": (None if self.present_a is None or self.field_constant_t_per_a is None
                          else round(self.present_a * self.field_constant_t_per_a, 6)),
            "field_constant_t_per_a": self.field_constant_t_per_a,
            "raising": self.raising, "noop": self.noop, "eta_s": self.eta_s,
            "ok": self.ok, "checks": [c.as_dict() for c in self.checks],
            "refusals": [c.as_dict() for c in self.refusals],
        }


def first_segment(segments: "list[dict] | None") -> "tuple[float, float] | None":
    """``(upper current, rate)`` of the first enabled ramp segment (the lowest boundary).

    A segment counts when its upper current and its rate are both positive numbers, as
    in :func:`ramp_eta_s`. ``None`` when the table is missing or has no such segment.
    """
    best: "tuple[float, float] | None" = None
    for seg in segments or []:
        up, rate = _finite(seg.get("upper_current_a")), _finite(seg.get("rate_a_s"))
        if up is not None and rate is not None and up > 0.0 and rate > 0.0:
            if best is None or up < best[0]:
                best = (up, rate)
    return best


def first_segment_upper_a(segments: "list[dict] | None") -> "float | None":
    """Upper current of the first enabled ramp segment, or ``None`` (see :func:`first_segment`)."""
    first = first_segment(segments)
    return None if first is None else first[0]


def ramp_eta_s(start_a: float, end_a: float, segments: "list[dict] | None",
               fallback_rate_a_s: "float | None") -> "float | None":
    """Seconds to move the output between two currents (either sign).

    Follows the 625's segment table: the rate in force is that of the segment whose
    upper current is above the present output, in both directions (manual 4-9), taken
    on ``|I|`` (on the negative side that is an assumption until the operator has
    verified it; see ``magnet_negative_segments_verified``). Between opposite signs the
    output passes through 0: down to 0, then up the other side. Above the last segment,
    *fallback_rate_a_s* is used. ``None`` if a needed rate is unknown.
    """
    if float(start_a) * float(end_a) < 0.0:
        down = ramp_eta_s(start_a, 0.0, segments, fallback_rate_a_s)
        up = ramp_eta_s(0.0, end_a, segments, fallback_rate_a_s)
        return None if down is None or up is None else down + up
    lo, hi = sorted((abs(float(start_a)), abs(float(end_a))))
    pts = sorted((float(s["upper_current_a"]), float(s["rate_a_s"]))
                 for s in (segments or [])
                 if s.get("upper_current_a") is not None and s.get("rate_a_s")
                 and float(s["rate_a_s"]) > 0)
    t, x = 0.0, lo
    for upper, rate in pts:
        if x >= hi:
            break
        if upper <= x:
            continue
        end = min(upper, hi)
        t += (end - x) / rate
        x = end
    if x < hi:
        if not fallback_rate_a_s or fallback_rate_a_s <= 0:
            return None
        t += (hi - x) / float(fallback_rate_a_s)
    return t


def _finite(v: Any) -> "float | None":
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _num(text: "str | None") -> "float | None":
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _int(text: Any) -> "int | None":
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return None


def _parse_flds(text: str) -> "tuple[str, float | None]":
    """``"0,+0.12345"`` -> ``("T", 0.12345)``; unknown parts as ``""`` / ``None``."""
    parts = [p.strip() for p in str(text or "").split(",")]
    unit = ""
    const: "float | None" = None
    try:
        unit = FIELD_UNITS.get(int(float(parts[0])), "") if parts and parts[0] else ""
    except ValueError:
        unit = ""
    if len(parts) > 1:
        const = _num(parts[1])
    return unit, const


def _default_temperature(channel: str):
    from mast.core.temperature import latest_temperature

    return latest_temperature(channel)


def _default_ledger_path() -> Path:
    from mast._runtime_paths import data_root

    return Path(data_root()) / "experiments" / "magnet_control_ledger.jsonl"


def panel_field_verdict(st: Any) -> "tuple[bool, str]":
    """Is the field at rest, judged from the 625's fast state (``fast_state``: the cached
    OPST? / RDGI? / ERST? reads the monitor already makes; nothing is written or asked)?
    Not at rest: the ramp-done bit (OPST? bit 1) is clear (the supply is ramping, whoever
    started it: the panel included), an error register has a bit set, or anything is missing
    (unknown is not at rest)."""
    if not isinstance(st, dict):
        return False, tr("625 的状态读不到：不知道有没有在扫场，不加热",
                         "the 625's state cannot be read: whether the field is changing is "
                         "unknown, so no heating")
    opst, cur = st.get("opst"), st.get("current_a")
    errs = [st.get(k) for k in ("hw_error_code", "op_error_code", "psh_error_code")]
    if opst is None or cur is None or any(e is None for e in errs):
        return False, tr("625 的状态读不全（OPST? / RDGI? / ERST?）：不知道有没有在扫场，不加热",
                         "the 625's state was not read in full (OPST? / RDGI? / ERST?): whether the "
                         "field is changing is unknown, so no heating")
    if not int(opst) & 0b010:
        return False, tr(f"625 正在扫场（OPST? = {opst}，ramp done 位没置；输出 {cur:+.4f} A）：不加热",
                         f"the 625 is ramping (OPST? = {opst}, the ramp-done bit is clear; output "
                         f"{cur:+.4f} A): no heating")
    if any(int(e) for e in errs):
        return False, tr(f"625 有错误位（ERST? = {errs}）：不加热",
                         f"the 625 reports an error (ERST? = {errs}): no heating")
    return True, tr("625 没有在扫场", "the 625 is not ramping")


def _default_heating_interlock() -> dict:
    """Is a Lake Shore 335 heater on, or is MAST heating? (``core.heater_control.interlock``:
    it may ask the 335, so it is called without this module's link held.)"""
    try:
        from mast.core import heater_control

        return heater_control.interlock()
    except Exception as exc:  # noqa: BLE001 - unknown is not an answer
        return {"ok": False, "detail": tr(f"读加热器状态失败：{exc}",
                                          f"reading the heater state failed: {exc}")}


def _default_heating_now() -> bool:
    """MAST's own heating, from memory only (``core.heater_control.mast_heating``)."""
    try:
        from mast.core import heater_control

        return bool(heater_control.mast_heating())
    except Exception:  # noqa: BLE001 - cannot tell: count it as heating
        return True


# ── the controller ────────────────────────────────────────────────────────────
class Magnet625Controller:
    """Field control for one Model 625, over the read-only driver's link.

    One per link (the runtime keeps it). ``plan`` is a dry run; ``start`` sends ``SETI``
    when every gate passes; ``supervise`` follows the ramp; ``stop`` freezes the output
    and never raises.
    """

    def __init__(self, link: Model625Link, *,
                 settings: "Callable[[], MagnetSettings]" = MagnetSettings.from_profile,
                 temperature: "Callable[[str], Any] | None" = None,
                 ledger_path: "Callable[[], Path] | None" = None,
                 clock: "Callable[[], float]" = time.monotonic,
                 sleep: "Callable[[float], None]" = time.sleep,
                 heating_interlock: "Callable[[], dict] | None" = None,
                 heating_now: "Callable[[], bool] | None" = None):
        self.link = link
        self._settings = settings
        self._temperature = temperature or _default_temperature
        # Heating and field changes never at the same time: the plan asks the 335 (I/O,
        # never while this link is held); start() asks MAST's heating from memory only.
        self._heating_interlock = heating_interlock or _default_heating_interlock
        self._heating_now = heating_now or _default_heating_now
        self._ledger_path = ledger_path or _default_ledger_path
        self._clock = clock
        self._sleep = sleep
        self._ramp_lock = threading.Lock()     # held while a ramp is prepared and sent
        self._state_lock = threading.Lock()
        self._stop_seq = 0                     # bumped by every stop() call
        self._stop_sent_seq = 0                # highest seq whose STOP went out (or was not needed)
        self._stops_in_flight = 0
        self._closing = False
        self._active: "dict | None" = None     # the MAST-started ramp being supervised
        self._last: "dict | None" = None       # how the previous one ended
        self._last_stop: "dict | None" = None

    # ── status ──
    def settings(self) -> MagnetSettings:
        try:
            return self._settings()
        except Exception as exc:  # noqa: BLE001 - unreadable settings = not configured
            logger.warning("magnet control: settings unreadable: %s", exc)
            return MagnetSettings()

    @property
    def active(self) -> "dict | None":
        with self._state_lock:
            return dict(self._active) if self._active else None

    @property
    def busy(self) -> bool:
        """A MAST ramp is being prepared, sent or supervised."""
        return self._ramp_lock.locked() or self.active is not None

    def status(self) -> dict:
        s = self.settings()
        busy = self.busy
        with self._state_lock:
            return {"port": self.link.port, "settings": s.as_dict(),
                    "can_set_field": s.control_configured, "busy": busy,
                    "active": dict(self._active) if self._active else None,
                    "last": dict(self._last) if self._last else None,
                    "last_stop": dict(self._last_stop) if self._last_stop else None}

    # ── dry run ──
    def plan(self, target_t: Any, *, user_authorization: str = "",
             authorized_up_to_t: Any = None, require_tip_withdrawn: bool = True,
             tip_verdict: "Callable[[], Any] | None" = None) -> FieldPlan:
        """Every gate's verdict for moving the field to *target_t* (tesla). Writes nothing.

        Never holds the link across the slow checks (see the module docstring).
        """
        tgt = _finite(target_t)
        plan = FieldPlan(target_t=tgt)
        s = self.settings()

        # 1. what the operator entered at initialization
        if not plan.add("configured", s.control_configured, tr(
                "初始化里已设定磁场上限与场常数" if s.control_configured else
                "初始化里还没有设定「磁体允许的最大磁场」和「磁体场常数」：在这之前 MAST 不改变磁场。"
                "请操作员在 设置 → 仪器初始化 里填写",
                "max field and field constant are set at initialization" if s.control_configured else
                "the maximum field and the field constant have not been set at initialization; "
                "until they are, MAST does not change the field. Ask the operator to fill them "
                "in under Settings -> Instrument initialization")):
            return plan
        k = float(s.field_constant_t_per_a)
        plan.field_constant_t_per_a = k

        # 2. the target itself
        if not plan.add("target", tgt is not None, tr(
                f"目标磁场 {target_t!r} 不是有限数字", f"target field {target_t!r} is not a finite number")
                if tgt is None else tr(f"目标 {tgt:g} T", f"target {tgt:g} T")):
            return plan
        plan.add("max_field", abs(tgt) <= float(s.max_field_t) + 1e-12, tr(
            f"目标 {tgt:g} T，初始化设定的上限 |B| = {s.max_field_t:g} T",
            f"target {tgt:g} T, limit set at initialization |B| = {s.max_field_t:g} T"))
        target_a = round(tgt / k, CURRENT_DECIMALS)
        plan.target_a = target_a

        # 3. the supply, read now (each query holds the link only briefly)
        try:
            t_read = self._clock()
            st: Magnet625State = self.link.read_state(max_age_s=0.0)
            flds_unit, flds_const = _parse_flds(self.link.query("FLDS?"))
            moved_a: "float | None" = None
            if st.ramp_done is not True and st.current_a is not None:
                wait = MOTION_WINDOW_S - (self._clock() - t_read)
                if wait > 0:
                    self._sleep(wait)
                again = _num(self.link.query("RDGI?"))
                moved_a = None if again is None else abs(again - st.current_a)
        except WrongInstrument as exc:
            plan.add("identity", False, tr(f"端口上不是 Model 625：{exc}", f"not a Model 625 on the port: {exc}"))
            return plan
        except (SerialUnavailable, Model625Error) as exc:
            plan.add("readable", False, tr(f"读不到磁体电源：{exc}", f"cannot read the magnet supply: {exc}"))
            return plan
        plan.present_a = st.current_a
        plan.present_setpoint_a = st.setpoint_current_a

        unread = [n for n, v in (("RDGI?", st.current_a), ("SETI?", st.setpoint_current_a),
                                  ("OPST?", st.operational_status_code),
                                  ("ERST? hw", st.hardware_error_code),
                                  ("ERST? op", st.operational_error_code),
                                  ("ERST? psh", st.psh_error_code),
                                  ("LIMIT?", st.limit_current_a), ("RSEG?", st.ramp_segments_enabled),
                                  ("XPGM?", st.external_program_code), ("PSHS?", st.psh_enabled),
                                  ("QNCH?", st.quench_detection_enabled))
                  if v is None]
        if not plan.add("readable", not unread, tr(
                "状态读齐了" if not unread else f"这些状态没读到：{', '.join(unread)}（读不到不是答案）",
                "state read in full" if not unread else
                f"not answered: {', '.join(unread)} (unknown is not an answer)")):
            return plan

        rel = None if flds_const is None else abs(flds_const - k) / k
        plan.add("field_constant", flds_unit == "T" and rel is not None and rel <= FIELD_CONSTANT_REL_TOL, tr(
            f"仪器 FLDS? = {flds_const} {flds_unit}/A，初始化设定 {k:g} T/A（允许差 0.5%）",
            f"instrument FLDS? = {flds_const} {flds_unit}/A, initialization {k:g} T/A "
            "(0.5 % allowed)"))
        plan.add("supply_limit", abs(target_a) <= float(st.limit_current_a) + 1e-9, tr(
            f"目标电流 {target_a:+.4f} A，仪器 LIMIT {st.limit_current_a:g} A（按绝对值）",
            f"target current {target_a:+.4f} A, supply LIMIT {st.limit_current_a:g} A (by magnitude)"))
        plan.limit_a = float(st.limit_current_a)
        self._check_reverse(plan, s, st, target_a)
        codes = (st.hardware_error_code, st.operational_error_code, st.psh_error_code)
        errs = [*(st.hardware_errors or []), *(st.operational_errors or []), *(st.psh_errors or [])]
        plan.add("no_errors", not any(codes), tr(
            "没有错误位" if not any(codes) else
            f"仪器有错误位：{'; '.join(errs)}。MAST 不清错误（不发 ERCL），请操作员在面板上处理",
            "no error bits" if not any(codes) else
            f"error bits set: {'; '.join(errs)}. MAST does not clear errors (no ERCL); "
            "the operator clears them at the front panel"))
        plan.add("internal_program", st.external_program_code == 0, tr(
            f"XPGM = {st.external_program_code}（必须是 0：内部编程）",
            f"XPGM = {st.external_program_code} (must be 0: internal programming)"))
        plan.add("no_persistent_switch", st.psh_enabled is False, tr(
            "没有持续开关（驱动模式）" if st.psh_enabled is False else
            "仪器启用了持续开关：MAST 不支持持续模式磁体",
            "no persistent switch (driven mode)" if st.psh_enabled is False else
            "a persistent switch is enabled: MAST does not run persistent-mode magnets"))
        plan.add("segment_ramping", st.ramp_segments_enabled is True, tr(
            "分段扫场程序已开启" if st.ramp_segments_enabled else
            "分段扫场（RSEG）关着：仪器会用单一速率扫场，高电流段可能超过厂家速率。请操作员在面板上打开",
            "segment ramping is on" if st.ramp_segments_enabled else
            "segment ramping (RSEG) is off: the supply would ramp at one rate, which can exceed "
            "the vendor's limit at high current. Ask the operator to turn it on at the panel"))
        # The ramp-done bit alone cannot say whether the output moves: after STOP its
        # state is not documented (manual 4.9 / STOP), and a supply frozen by STOP must
        # not lock MAST out of every later change, the way down included.
        if st.ramp_done is True:
            moving: "bool | None" = False
            how = tr("当前没有在扫场（ramp done）", "not ramping (ramp done)")
        elif moved_a is None:
            moving = None
            how = tr("判断不了输出是否在动（第二次读电流失败）",
                     "cannot tell whether the output is moving (second current read failed)")
        else:
            moving = moved_a > MOTION_THRESHOLD_A
            how = (tr(f"输出正在变化（{MOTION_WINDOW_S:.0f} s 内变了 {moved_a * 1000:.1f} mA）："
                      "先等它到位，或先用 StopMagnetRamp 停住",
                      f"the output is changing ({moved_a * 1000:.1f} mA in "
                      f"{MOTION_WINDOW_S:.0f} s): wait for it to arrive, or stop it with "
                      "StopMagnetRamp first") if moving else
                   tr(f"输出停在 {st.current_a:.4f} A（设定值 {st.setpoint_current_a:.4f} A，"
                      "扫场未完成而被停住）",
                      f"output held at {st.current_a:.4f} A (set point "
                      f"{st.setpoint_current_a:.4f} A; the ramp was stopped before it finished)"))
        plan.add("not_ramping", moving is False, how)

        # 4. direction, nothing-to-do, duration (|B| for "raising", signed for the rest)
        present = float(st.current_a)
        plan.raising = abs(target_a) > abs(present) + _current_tol(present)
        if target_a < 0 and _side(present) >= 0:
            # A reverse target from a non-negative output is a raise (of the reverse field),
            # however small: -3 mT to cancel a remanent field still needs the words naming it.
            plan.raising = True
        plan.noop = (moving is False and abs(target_a - present) <= _current_tol(present)
                     and abs(float(st.setpoint_current_a) - target_a) <= SETI_READBACK_TOL_A + 1e-9)
        plan.eta_s = ramp_eta_s(present, target_a,
                                st.ramp_segments if st.ramp_segments_enabled else None,
                                st.ramp_rate_a_s)

        # 5. raising |B| only
        if plan.raising:
            self._check_authorization(plan, tgt, user_authorization, authorized_up_to_t)
            plan.add("quench_detection", st.quench_detection_enabled is True, tr(
                "失超检测开着" if st.quench_detection_enabled else "失超检测关着：不升场",
                "quench detection is on" if st.quench_detection_enabled else
                "quench detection is off: no raise"))
            self._check_temperature(plan, s)

        # 6. heating and field changes never at the same time (the 335 is asked here,
        #    without this link held)
        self._check_heating(plan)

        # 7. the tip
        if require_tip_withdrawn:
            self._check_tip(plan, tip_verdict)
        else:
            plan.add("tip", True, tr("调用方声明：针尖不退也可以改变磁场",
                                     "the caller waived the withdrawn-tip requirement"))
        return plan

    def _check_reverse(self, plan: FieldPlan, s: MagnetSettings, st: Magnet625State,
                       target_a: float) -> None:
        """Reverse-field gates: no crossing of zero; negative segments known to act on |I|."""
        present = float(st.current_a)
        # Beyond the tolerance on one side, ANY target on the other side crosses zero, even
        # one inside the tolerance (-5 mA from +5 A would still pass through zero).
        crossing = (_side(present) > 0 and target_a < 0) or (_side(present) < 0 and target_a > 0)
        plan.add("no_zero_crossing", not crossing, tr(
            f"输出 {present:+.4f} A，目标 {target_a:+.4f} A，不跨零" if not crossing else
            f"输出 {present:+.4f} A 与目标 {target_a:+.4f} A 在零的两侧：不跨零。"
            "请先扫到 0 T，再另起一次扫场",
            f"output {present:+.4f} A and target {target_a:+.4f} A do not straddle zero" if not crossing
            else f"output {present:+.4f} A and target {target_a:+.4f} A are on opposite sides of zero: "
            "a ramp does not cross zero. Ramp to 0 T first, then start a second ramp"))
        if target_a >= 0.0:
            return
        first = first_segment(st.ramp_segments if st.ramp_segments_enabled else None)
        boundary = None if first is None else first[0]
        if s.negative_segments_verified:
            plan.add("negative_segments", True, tr(
                "操作员已确认负向分段按 |I| 生效",
                "the operator has verified that the segments act on |I| on the negative side"))
            return
        # Unverified, the supply may ignore the table on the negative side and ramp at RATE
        # there. Inside the first segment that is only safe when RATE is not faster than the
        # first segment's rate (a machine with a faster RATE would sweep faster than the table).
        rate = _finite(st.ramp_rate_a_s)
        rate_ok = first is not None and rate is not None and rate <= first[1] + 1e-9
        plan.add("negative_rate", rate_ok, tr(
            f"RATE {rate:g} A/s 不快于第一段的 {first[1]:g} A/s" if rate_ok else
            ("读不到 RATE? 或分段表：负向分段未验证时不去负向" if first is None or rate is None else
             f"RATE {rate:g} A/s 比第一段的 {first[1]:g} A/s 快：负向分段未验证时，若仪器在负向不查分段表而用 "
             "RATE，就会比分段表规定的更快。请操作员核对后在 仪器初始化 里打开「负向扫场分段已验证」"),
            f"RATE {rate:g} A/s is not faster than the first segment's {first[1]:g} A/s" if rate_ok else
            ("RATE? or the segment table could not be read: no negative target until the segments "
             "are verified" if first is None or rate is None else
             f"RATE {rate:g} A/s is faster than the first segment's {first[1]:g} A/s: while the "
             "negative-side segments are unverified, a supply that uses RATE instead of the table "
             "there would sweep faster than the table prescribes. The operator checks it, then turns "
             "on \"Negative-side ramp segments verified\" in the instrument initialization")))
        ok = boundary is not None and abs(target_a) <= boundary + 1e-9
        plan.add("negative_segments", ok, tr(
            f"负向目标 {target_a:+.4f} A，|I| 在第一段上限 {boundary:g} A 以内" if ok else
            ("读不到可用的分段表，不知道第一段上限：不去负向" if boundary is None else
             f"负向目标 {target_a:+.4f} A 的 |I| 超过第一段上限 {boundary:g} A。手册没有写明负向输出按 |I| "
             "查分段表；操作员要在负向第一次越过这个边界时看 RATE? 是否随之变化，确认后在 仪器初始化 里"
             "打开「负向扫场分段已验证」"),
            f"negative target {target_a:+.4f} A, |I| within the first segment's upper current "
            f"{boundary:g} A" if ok else
            ("no usable segment table was read, so the first boundary is unknown: no negative target"
             if boundary is None else
             f"|I| of the negative target {target_a:+.4f} A exceeds the first segment's upper current "
             f"{boundary:g} A. The manual never says the segments act on |I| for negative output; the "
             "operator must see RATE? change at this boundary on the negative side the first time, and "
             "then turns on \"Negative-side ramp segments verified\" in the instrument initialization")))

    def _check_heating(self, plan: FieldPlan) -> None:
        try:
            v = self._heating_interlock()
        except Exception as exc:  # noqa: BLE001 - unknown is not an answer
            v = {"ok": False, "detail": tr(f"读加热器状态失败：{exc}", f"reading the heater state failed: {exc}")}
        ok = bool(isinstance(v, dict) and v.get("ok") is True)
        detail = str((v or {}).get("detail") or "") if isinstance(v, dict) else repr(v)
        if isinstance(v, dict) and v.get("present") is False and ok:
            detail = detail or tr("没有温控仪", "no temperature controller")
        plan.add("no_heating", ok, detail or tr("加热器状态不明：不改磁场",
                                                "the heater state is unknown: the field does not change"))

    def _check_authorization(self, plan: FieldPlan, tgt: float, words: Any, up_to: Any) -> None:
        auth = str(words or "").strip()
        is_example = auth.casefold() in EXAMPLE_AUTHORIZATIONS
        plan.add("user_authorization", len(auth) >= MIN_AUTHORIZATION_CHARS and not is_example, tr(
            f"用户授权：「{auth}」" if auth and not is_example else
            "这是技能说明里的示例，不是用户的话：升场要用户本人的授权原话" if is_example else
            "升场需要用户的口头授权：把用户授权时说的话逐字填进 user_authorization（液面读不到，"
            "这是操作员定的条件）。没有授权就不升场",
            f"user authorization: \"{auth}\"" if auth and not is_example else
            "that is the example from the skill's documentation, not the user's words" if is_example else
            "raising the field needs the user's verbal authorization: put the user's own "
            "words in user_authorization (the helium level cannot be read; this is the "
            "operator's condition). No authorization, no raise"))
        cap = _finite(up_to)
        plan.add("authorized_field", cap is not None and abs(tgt) <= cap + 1e-9, tr(
            f"授权到 |B| = {cap:g} T，目标 {tgt:g} T" if cap is not None else
            "升场要在 authorized_up_to_t 里填用户授权到的磁场（特斯拉，取绝对值）：授权只覆盖到那个值",
            f"authorized up to |B| = {cap:g} T, target {tgt:g} T" if cap is not None else
            "raising needs authorized_up_to_t, the field (tesla, a magnitude) the user authorized: "
            "the authorization covers that far and no further"))
        info, detached = scan_fields(auth)
        signed = [v for v, _ in info]
        side = [abs(v) for v in signed if (v < 0) == (tgt < 0)]
        if tgt < 0:
            plan.add("authorization_sign", bool(side), tr(
                f"原话里说了反向场：{', '.join(f'{v:g} T' for v in signed if v < 0)}" if side else
                "目标是反向场（负），用户的话里要明确写出负向 / 反向的磁场，例如「-2 T」「负2T」「反向2T」。"
                "只说了正的磁场，或什么都没说，不算授权反向场",
                f"the words name a reverse field: {', '.join(f'{v:g} T' for v in signed if v < 0)}"
                if side else
                "the target is a reverse (negative) field: the user's words must name a negative or "
                "reverse field explicitly, e.g. \"-2 T\", \"negative 2 T\", \"reverse 2 T\". "
                "Words that name only a positive field, or none, do not authorize it"))
        elif signed and not side:
            plan.add("authorization_sign", False, tr(
                f"原话里只说了反向场（{', '.join(f'{v:g} T' for v in signed)}），不能授权正向的目标 {tgt:g} T",
                f"the words name only a reverse field ({', '.join(f'{v:g} T' for v in signed)}); "
                f"that does not authorize the positive target {tgt:g} T"))
        elif detached and not any(p for v, p in info if v > 0):
            plan.add("authorization_sign", False, tr(
                f"原话里有「反向 / 负 / reverse / minus」之类的词，但没有贴着数字，分不清说的是正场还是反向场；"
                f"目标是正的 {tgt:g} T。请用户把磁场写成「+{abs(tgt):g} T」或「-{abs(tgt):g} T」",
                f"the words use a sign word (reverse / negative / minus ...) that does not touch a "
                f"number, so it is unclear whether they mean a positive or a reverse field; the target "
                f"is the positive {tgt:g} T. Ask the user to write the field as \"+{abs(tgt):g} T\" or "
                f"\"-{abs(tgt):g} T\""))
        if cap is not None and side:
            plan.add("authorization_consistent", cap <= max(side) + 1e-9, tr(
                f"原话里说的是 {', '.join(f'{v:g} T' for v in side)}（|B|），authorized_up_to_t = {cap:g} T",
                f"the words name {', '.join(f'{v:g} T' for v in side)} (|B|), authorized_up_to_t = "
                f"{cap:g} T"))

    def _check_temperature(self, plan: FieldPlan, s: MagnetSettings) -> None:
        if not s.temperature_channel or s.max_temperature_k is None:
            plan.add("magnet_temperature", False, tr(
                "初始化里还没有设定磁体温度计和温度上限：升场前要看磁体温度。请操作员在 仪器初始化 里填写",
                "the magnet thermometer and its limit are not set at initialization: raising "
                "the field needs the magnet temperature. Ask the operator to fill them in"))
            return
        try:
            r = self._temperature(s.temperature_channel)
        except Exception as exc:  # noqa: BLE001
            r = None
            why = f"{type(exc).__name__}: {exc}"
        else:
            why = getattr(r, "reason", None) or ""
        val = _finite(getattr(r, "value_k", None))
        age = _finite(getattr(r, "age_s", None))
        if val is None:
            plan.add("magnet_temperature", False, tr(
                f"读不到磁体温度（{s.temperature_channel}：{why or '无读数'}）：不升场",
                f"magnet temperature unavailable ({s.temperature_channel}: {why or 'no reading'}): no raise"))
            return
        if val < TEMPERATURE_MIN_VALID_K:
            plan.add("magnet_temperature", False, tr(
                f"磁体温度读数 {val:g} K 不可信（Lake Shore 输入无效时报 0）：不升场，请查温度计",
                f"magnet temperature reading {val:g} K is not valid (a Lake Shore input reports 0 "
                "when the sensor is open or over range): no raise; check the thermometer"))
            return
        if age is None or age > TEMPERATURE_MAX_AGE_S:
            plan.add("magnet_temperature", False, tr(
                f"磁体温度读数太旧或年龄不明（{age} s）：不升场",
                f"magnet temperature reading too old or of unknown age ({age} s): no raise"))
            return
        plan.add("magnet_temperature", val <= s.max_temperature_k, tr(
            f"磁体温度 {val:.3f} K（{getattr(r, 'channel', s.temperature_channel)}），初始化设定上限 "
            f"{s.max_temperature_k:g} K",
            f"magnet temperature {val:.3f} K ({getattr(r, 'channel', s.temperature_channel)}), "
            f"limit set at initialization {s.max_temperature_k:g} K"))

    def _check_tip(self, plan: FieldPlan, tip_verdict: "Callable[[], Any] | None") -> None:
        if tip_verdict is None:
            plan.add("tip", False, tr(
                "没法确认针尖已退（这条路径读不到 Nanonis）。退针后再试，或显式声明不要求退针",
                "cannot confirm the tip is withdrawn (no Nanonis on this path). Withdraw "
                "it, or waive the requirement explicitly"))
            return
        try:
            v = tip_verdict()
        except Exception as exc:  # noqa: BLE001
            plan.add("tip", False, tr(f"读针尖状态失败：{exc}", f"reading the tip state failed: {exc}"))
            return
        state = str(getattr(v, "state", "") or "")
        reason = str(getattr(v, "reason", "") or "")
        plan.add("tip", state == "parked", tr(
            f"针尖：{reason or state}" + ("" if state == "parked" else
                                         "。默认要求先退针再改变磁场（WithdrawTip）；确需带针改变时显式声明"),
            f"tip: {reason or state}" + ("" if state == "parked" else
                                        ". By default the tip is withdrawn before the field changes "
                                        "(WithdrawTip); waive it explicitly if the change must be "
                                        "made with the tip in")))

    # ── short status read (inside or outside a session) ──
    def _quick_status(self) -> dict:
        cur = _num(self.link.query("RDGI?"))
        setp = _num(self.link.query("SETI?"))
        opst = _int(self.link.query("OPST?"))
        errs = [_int(x) for x in str(self.link.query("ERST?")).split(",")]
        if (cur is None or setp is None or opst is None or len(errs) < 3
                or any(e is None for e in errs[:3])):
            raise Model625Error("incomplete status")
        return {"current_a": cur, "setpoint_a": setp, "opst": opst,
                "errors": (errs[0], errs[1], errs[2])}

    @staticmethod
    def _changed_since(plan: FieldPlan, q: dict) -> str:
        hw, op, psh = q["errors"]
        if hw or op or psh:
            return tr(f"错误位出现了（{hw},{op},{psh}）", f"error bits appeared ({hw},{op},{psh})")
        if abs(q["setpoint_a"] - float(plan.present_setpoint_a)) > SETI_READBACK_TOL_A:
            return tr(f"设定值被改了（{plan.present_setpoint_a:.4f} → {q['setpoint_a']:.4f} A）",
                      f"the set point changed ({plan.present_setpoint_a:.4f} -> "
                      f"{q['setpoint_a']:.4f} A)")
        if abs(q["current_a"] - float(plan.present_a)) > PREP_DRIFT_TOL_A:
            return tr(f"输出变了（{plan.present_a:.4f} → {q['current_a']:.4f} A）",
                      f"the output moved ({plan.present_a:.4f} -> {q['current_a']:.4f} A)")
        return ""

    # ── the write ──
    def start(self, target_t: Any, *, origin: str, user_authorization: str = "",
              authorized_up_to_t: Any = None, require_tip_withdrawn: bool = True,
              tip_verdict: "Callable[[], Any] | None" = None,
              abort_check: "Callable[[], bool]" = lambda: False) -> dict:
        """Plan; then, holding the link briefly, re-check and send ``SETI``.

        Returns ``{"started": bool, "plan": {...}, ...}``. ``started`` is True only when
        the supply read back the new set point. Raises nothing for instrument conditions.
        """
        if self._closing:
            return {"started": False, "error": tr("MAST 正在关闭：不开始新的扫场",
                                                  "MAST is shutting down: no new ramp")}
        if not self._ramp_lock.acquire(blocking=False):
            return {"started": False, "error": tr("另一次 MAST 扫场还没结束",
                                                  "another MAST ramp has not finished")}
        try:
            seq0 = self._stop_seq
            plan = self.plan(target_t, user_authorization=user_authorization,
                             authorized_up_to_t=authorized_up_to_t,
                             require_tip_withdrawn=require_tip_withdrawn,
                             tip_verdict=tip_verdict)
            out: dict = {"started": False, "plan": plan.as_dict()}
            if not plan.ok:
                out["error"] = plan.refusal_text()
                self._ledger({"action": "refused", "target_t": plan.target_t,
                              "origin": origin, "authorization": user_authorization,
                              "authorized_up_to_t": authorized_up_to_t,
                              "refusals": [c.as_dict() for c in plan.refusals]})
                return out
            if plan.noop:
                out["noop"] = True
                return out
            entry: dict = {"action": "SETI", "current_a": plan.target_a, "target_t": plan.target_t,
                           "origin": origin, "authorization": user_authorization,
                           "authorized_up_to_t": authorized_up_to_t, "raising": plan.raising,
                           "eta_s": plan.eta_s, "previous_setpoint_a": plan.present_setpoint_a}
            send_error = ""
            back: "float | None" = None
            try:
                with self.link.session(timeout=10.0) as gen:
                    q = self._quick_status()
                    changed = self._changed_since(plan, q)
                    if changed:
                        out["error"] = tr(f"预检之后状态变了：{changed}。没有发设定，请重新预检",
                                          f"the state changed after the plan: {changed}. "
                                          "Nothing sent; plan again")
                        self._ledger({"action": "refused", "target_t": plan.target_t,
                                      "origin": origin, "changed": changed})
                        return out
                    if abort_check() or self._stop_seq != seq0 or self._closing:
                        out["error"] = tr("准备期间收到了中止 / STOP：不发设定",
                                          "an abort / STOP arrived while preparing: nothing sent")
                        return out
                    # Asked again from memory only: no 335 query while this link is held.
                    try:
                        heating = bool(self._heating_now())
                    except Exception:  # noqa: BLE001 - cannot tell: count it as heating
                        heating = True
                    if heating:
                        out["error"] = tr("预检之后 MAST 开始加热了：加热和改磁场不同时进行，没有发设定",
                                          "MAST started heating after the plan: heating and field "
                                          "changes never overlap; nothing sent")
                        self._ledger({"action": "refused", "target_t": plan.target_t,
                                      "origin": origin, "changed": "mast_heating"})
                        return out
                    try:
                        payload = build_write("SETI", plan.target_a, limit_a=plan.limit_a)
                    except ValueError as exc:
                        out["error"] = str(exc)
                        return out
                    self.link.wait_gap()
                    try:
                        self.link.transport.transact(payload, read_size=0, timeout=0.3,
                                                     expect_generation=gen)
                    except Exception as exc:  # noqa: BLE001 - the bytes may have left
                        send_error = f"{type(exc).__name__}: {exc}"
                    finally:
                        self.link.mark_sent()
                    if not send_error:
                        try:
                            back = _num(self.link.query("SETI?"))
                        except (SerialUnavailable, Model625Error) as exc:
                            entry["readback_error"] = str(exc)
            except WrongInstrument as exc:
                out["error"] = tr(f"端口上不是 Model 625：{exc}", f"not a Model 625 on the port: {exc}")
                return out
            except (SerialUnavailable, Model625Error) as exc:
                if not entry.get("readback_error") and not send_error:
                    out["error"] = tr(f"磁体电源不可用：{exc}", f"magnet supply unavailable: {exc}")
                    return out
            entry["readback_a"] = back

            if send_error:
                # The write raised, but its bytes may have left: treat it as sent, freeze.
                entry["result"] = "send_raised"
                entry["send_error"] = send_error
                self._ledger(entry)
                stop = self.stop(reason=f"SETI write raised ({send_error}); it may have gone out",
                                 origin="magnet control (send check)")
                out.update(stop=stop, error=tr(
                    f"发设定时出错（{send_error}），设定可能已经发出：已发 STOP"
                    + ("" if stop.get("sent") else "，但 STOP 没发出去：请操作员在面板上按 Stop Output"),
                    f"sending the set point raised ({send_error}) and it may have gone out: STOP sent"
                    + ("" if stop.get("sent") else ", but the STOP did not go out: ask the operator "
                       "to press Stop Output at the panel")))
                return out
            if back is not None and abs(back - plan.target_a) <= SETI_READBACK_TOL_A:
                entry["result"] = "accepted"
                self._ledger(entry)
                with self._state_lock:
                    self._active = {"target_t": plan.target_t, "target_a": plan.target_a,
                                    "raising": plan.raising, "eta_s": plan.eta_s,
                                    "started_at": time.time(), "origin": origin,
                                    "authorization": user_authorization,
                                    "authorized_up_to_t": authorized_up_to_t,
                                    "stop_seq": seq0}
                logger.warning("magnet control: SETI %.4f A (%.4g T) sent by %s, ETA %s s",
                               plan.target_a, plan.target_t, origin, plan.eta_s)
                out["started"] = True
                return out
            prev = plan.present_setpoint_a
            if back is not None and prev is not None and abs(back - prev) <= SETI_READBACK_TOL_A:
                # The supply kept its old set point: it did not take the command.
                entry["result"] = "not_accepted"
                self._ledger(entry)
                out.update(not_accepted=True, error=tr(
                    f"Model 625 没有接受设定（SETI? 仍读 {back:.4f} A）。面板可能处于本地模式；"
                    "MAST 不切换接口模式。请操作员在面板上按 Remote，或在面板上操作",
                    f"the Model 625 did not take the set point (SETI? still reads {back:.4f} A). "
                    "The front panel may be in local mode; MAST does not switch the interface "
                    "mode. Ask the operator to press Remote on the panel, or to make the change "
                    "at the panel"))
                return out
            # Anything else: we do not know what the supply is doing. Freeze it.
            entry["result"] = "unexpected_readback"
            self._ledger(entry)
            stop = self.stop(reason=f"SETI read-back {back!r} != {plan.target_a}",
                             origin="magnet control (read-back check)")
            out.update(stop=stop, error=tr(
                f"SETI? 读回 {back!r}，与发出的 {plan.target_a:.4f} A 不符：已发 STOP",
                f"SETI? read back {back!r}, not the {plan.target_a:.4f} A sent: STOP sent"))
            return out
        finally:
            self._ramp_lock.release()

    # ── follow the ramp ──
    def supervise(self, *, abort_check: "Callable[[], bool]" = lambda: False,
                  progress: "Callable[[dict], None] | None" = None,
                  poll_s: float = POLL_S) -> dict:
        """Follow the MAST-started ramp until it arrives or must stop. Returns how it ended."""
        act = self.active
        if not act:
            return {"outcome": "no_active_ramp"}
        target = float(act["target_a"])
        seq0 = int(act["stop_seq"])
        t0 = self._clock()
        last_move_a: "float | None" = None
        last_move_t = t0
        done_seen = 0
        fails = 0
        last_error = ""
        last: dict = {}
        why: "tuple[str, str] | None" = None    # (outcome, detail) of a STOP we asked for
        stop_asked_at: "float | None" = None
        while True:
            now = self._clock()
            status: "dict | None" = None
            try:
                status = self._quick_status()
                fails = 0
            except (SerialUnavailable, Model625Error, WrongInstrument) as exc:
                fails += 1
                last_error = str(exc)
            if status is not None:
                hw, op, psh = status["errors"]
                last = {"current_a": status["current_a"], "setpoint_a": status["setpoint_a"],
                        "opst": status["opst"], "errors": [hw, op, psh],
                        "elapsed_s": round(now - t0, 1)}
                if _protecting(hw, op, psh):
                    return self._end("instrument_protection", last, tr(
                        "仪器在自我保护（硬件错误 / 失超 / crowbar 放电 / 远程禁止）：MAST 不发任何命令，"
                        "请操作员处理",
                        "the supply is protecting the magnet (hardware error / quench / crowbar "
                        "discharge / remote inhibit): MAST sends nothing; the operator takes over"))

            # A STOP since this ramp began, from anyone (a skill, the UI, E-STOP, us).
            if self._stop_seq != seq0:
                if stop_asked_at is None:
                    stop_asked_at = now
                if self._stop_sent_seq > seq0:
                    skipped = bool((self._last_stop or {}).get("skipped"))
                    outcome, detail = why or (("instrument_protection", tr(
                        "仪器在自我保护，STOP 没有发（不需要）", "the supply protects itself; no STOP needed"))
                        if skipped else ("stopped", tr("收到 STOP，输出已冻结",
                                                       "STOP received; output frozen")))
                    return self._end(outcome, last, detail, stop=self._last_stop)
                if self._stops_in_flight == 0:
                    if now - stop_asked_at > STOP_GIVE_UP_S:
                        return self._end("stop_failed", last, tr(
                            f"{STOP_GIVE_UP_S:.0f} s 内 STOP 一直没发出去（{(self._last_stop or {}).get('error')}）："
                            "扫场可能仍在继续，请操作员立即在 625 面板上按 Stop Output",
                            f"for {STOP_GIVE_UP_S:.0f} s no STOP got out "
                            f"({(self._last_stop or {}).get('error')}): the ramp may still be "
                            "running; the operator must press Stop Output on the 625 now"),
                            stop=self._last_stop)
                    res = self.stop(reason="an earlier STOP did not go out; retrying",
                                    origin="magnet control (supervisor)")
                    if not (res.get("sent") or res.get("skipped")):
                        self._sleep(poll_s)
                    continue
                self._sleep(poll_s)
                continue

            if abort_check():
                why = ("aborted", tr("运行被中止，已发 STOP", "run aborted; STOP sent"))
                self.stop(reason="abort during ramp", origin="magnet control (abort)")
                continue
            if status is None:
                if fails >= READ_FAILURES_BEFORE_STOP:
                    why = ("lost_contact", tr(
                        f"连续 {fails} 次读不到磁体电源（{last_error}）：已发 STOP",
                        f"{fails} reads in a row failed ({last_error}): STOP sent"))
                    self.stop(reason=f"lost contact: {last_error}", origin="magnet control (supervisor)")
                    continue
                self._sleep(poll_s)
                continue
            cur, setp = status["current_a"], status["setpoint_a"]
            hw, op, psh = status["errors"]
            if op or psh:
                why = ("instrument_error", tr(f"扫场中出现错误位（{hw},{op},{psh}）：已发 STOP",
                                              f"error bits during the ramp ({hw},{op},{psh}): STOP sent"))
                self.stop(reason=f"error bits {hw},{op},{psh}", origin="magnet control (supervisor)")
                continue
            if abs(setp - target) > SETI_READBACK_TOL_A:
                return self._end("external_change", last, tr(
                    f"设定值被别处改成 {setp:.4f} A（不是 MAST 发的 {target:.4f} A）：MAST 不再跟进、不写任何命令",
                    f"the set point was changed elsewhere to {setp:.4f} A (MAST sent "
                    f"{target:.4f} A): MAST stops following and writes nothing"))
            ramp_done = bool(status["opst"] & 0b010)
            if ramp_done:
                done_seen += 1
                if done_seen >= DONE_CONFIRMATIONS:
                    residual = cur - target
                    note = ("" if abs(residual) <= _current_tol(target) else tr(
                        f"（残差 {residual * 1000:+.1f} mA，超过仪器精度，请核对）",
                        f" (residual {residual * 1000:+.1f} mA, beyond the supply's accuracy; check)"))
                    return self._end("done", dict(last, residual_a=round(residual, 4)), tr(
                        f"已到位，残差 {residual * 1000:+.1f} mA", f"arrived, residual "
                        f"{residual * 1000:+.1f} mA") + note)
            else:
                done_seen = 0
            if last_move_a is None or abs(cur - last_move_a) >= STALL_MIN_PROGRESS_A:
                last_move_a, last_move_t = cur, now
            elif not ramp_done and now - last_move_t > STALL_WINDOW_S:
                why = ("stalled", tr(f"{STALL_WINDOW_S:.0f} s 内电流几乎没动：已发 STOP",
                                     f"current barely moved in {STALL_WINDOW_S:.0f} s: STOP sent"))
                self.stop(reason="ramp stalled", origin="magnet control (supervisor)")
                continue
            if now - t0 > MAX_SUPERVISE_S:
                why = ("timeout", tr("超过监督时限：已发 STOP", "supervision time cap reached: STOP sent"))
                self.stop(reason="supervision cap reached", origin="magnet control (supervisor)")
                continue
            if progress is not None:
                try:
                    progress(dict(last, target_a=target))
                except Exception:  # noqa: BLE001 - a progress sink never stops supervision
                    pass
            self._sleep(poll_s)

    def _end(self, outcome: str, last: dict, detail: str, **extra) -> dict:
        with self._state_lock:
            act = self._active
            self._active = None
            self._last = {"outcome": outcome, "detail": detail, "ended_at": time.time(),
                          **({"target_t": act.get("target_t"), "target_a": act.get("target_a")}
                             if act else {}), **last}
        out = {"outcome": outcome, "detail": detail, "last": last, **extra}
        self._ledger({"action": "ramp_end", "outcome": outcome, "detail": detail, **last})
        level = (logging.INFO if outcome == "done" else
                 logging.CRITICAL if outcome == "stop_failed" else logging.WARNING)
        logger.log(level, "magnet control: ramp ended (%s): %s", outcome, detail)
        return out

    # ── stop ──
    def stop(self, *, reason: str, origin: str,
             lock_timeout_s: float = STOP_LOCK_TIMEOUT_S) -> dict:
        """Send ``STOP`` (freeze the output at its present value). Never raises.

        Not sent while the supply protects the magnet by itself (any hardware error, or
        quench / crowbar / remote inhibit): it is driving the output on purpose, and the
        manual does not say what STOP would do to that. ERST? unreadable: STOP is sent.
        """
        with self._state_lock:
            self._stop_seq += 1
            seq = self._stop_seq
            self._stops_in_flight += 1
        out: dict = {"seq": seq, "sent": False, "skipped": False, "error": "",
                     "reason": str(reason)[:300], "origin": str(origin)[:200], "at": time.time()}
        try:
            payload = build_write("STOP")
            with self.link.session(timeout=lock_timeout_s) as gen:
                errs: "tuple[int, int, int] | None" = None
                try:
                    raw = [_int(x) for x in str(self.link.query("ERST?")).split(",")]
                    if len(raw) >= 3 and all(x is not None for x in raw[:3]):
                        errs = (raw[0], raw[1], raw[2])
                except (SerialUnavailable, Model625Error):
                    errs = None
                if errs is not None and _protecting(*errs):
                    out["skipped"] = True
                    out["errors"] = list(errs)
                    out["error"] = tr(
                        f"仪器在自我保护（错误位 {errs}）：不发 STOP，不干预它的保护动作",
                        f"the supply is protecting the magnet (error bits {errs}): no STOP, its "
                        "protective action is left alone")
                else:
                    self.link.wait_gap()
                    try:
                        self.link.transport.transact(payload, read_size=0, timeout=0.3,
                                                     expect_generation=gen)
                    finally:
                        self.link.mark_sent()
                    out["sent"] = True
        except Exception as exc:  # noqa: BLE001 - a stop never raises
            out["error"] = f"{type(exc).__name__}: {exc}"
        with self._state_lock:
            self._stops_in_flight -= 1
            if out["sent"] or out["skipped"]:
                self._stop_sent_seq = max(self._stop_sent_seq, seq)
            self._last_stop = dict(out)
        self._ledger({"action": "STOP", **out})
        level = logging.WARNING if (out["sent"] or out["skipped"]) else logging.CRITICAL
        logger.log(level, "magnet control: STOP %s (%s; by %s)%s",
                   "sent" if out["sent"] else "skipped" if out["skipped"] else "NOT sent",
                   reason, origin, f": {out['error']}" if out["error"] else "")
        return out

    def stop_if_active(self, *, reason: str, origin: str) -> "dict | None":
        """``stop`` only while a MAST ramp is being prepared, sent or supervised."""
        if not self.busy:
            return None
        return self.stop(reason=reason, origin=origin)

    def begin_shutdown(self, *, reason: str = "MAST is shutting down",
                       origin: str = "MAST shutdown") -> "dict | None":
        """Refuse new ramps from now on; freeze a MAST ramp that is in flight."""
        self._closing = True
        return self.stop_if_active(reason=reason, origin=origin)

    # ── the ledger ──
    def _ledger(self, entry: dict) -> None:
        try:
            path = self._ledger_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "port": self.link.port, **entry}
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 - the ledger never blocks a stop
            logger.warning("magnet control: ledger write failed: %s", exc)


__all__ = [
    "CURRENT_TOL_A",
    "EXAMPLE_AUTHORIZATIONS",
    "FieldControlError",
    "FieldPlan",
    "Magnet625Controller",
    "MagnetSettings",
    "build_write",
    "fields_named_in",
    "first_segment",
    "first_segment_upper_a",
    "scan_fields",
    "ramp_eta_s",
    "signed_fields_named_in",
]
