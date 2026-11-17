"""Lake Shore Model 335 temperature control: heat the sample stage (``SETP`` / ``RAMP`` /
``RANGE``), and the zone table and control mode it runs under (``ZONE`` / ``OUTMODE``).

Read ``environment/lakeshore_temp.py`` first. It is the read-only driver, and its
:class:`~mast.environment.lakeshore_temp.LakeshoreLink` is the identity-checked link
this module writes through. The facts below are from the Model 335 user manual (Rev 1.6,
printed page numbers):

* A setpoint ramp runs from the OLD SETPOINT to the new one, not from the measured
  temperature (§4.5.1.7.7 p. 63; RAMP p. 122). While the heater is off the setpoint can
  sit anywhere (a parked value of 40 K or 500 K, say). Turning the heater on there
  drives straight at it.
* The 335 has no communication watchdog (the manual names none). Once the heater is
  on, it regulates to the setpoint until something turns it off, whatever happens to
  MAST or the PC. Its own temperature limit (``TLIMIT``, p. 125: exceeded, "all control
  outputs will be turned off") is the net that does not depend on MAST, so a change is
  refused unless that limit is set on the control input, and set no higher than the
  maximum setpoint (``heater_max_setpoint_k``), which is the heater stage's rated
  maximum, configured per site.
* The instrument turns the heater range off by itself on a control-sensor error
  (T./S. over/under range), a TLIMIT trip, or a setup change to the control input
  (p. 64). ALL OFF on the front panel turns both outputs off, and works in remote mode
  and with the keypad locked (p. 64).
* Zone mode (§4.5.1.6.2 p. 58, §5.3 p. 69): "the instrument will update the control
  settings each time the setpoint crosses into a new zone". P, I, D, manual output,
  heater range, ramp rate and control input then come from the zone the SETPOINT is in.
  "If the settings are changed manually, the controller will use the new setting while
  it is in the same zone and will update to the programmed zone table settings when the
  setpoint crosses into a new zone." Zone 1 starts at 0 K and each zone ends at its upper
  bound (the manual's example: zone 1 to 4 K, zone 2 from 4.001 K). Zone mode is meant
  to be used with setpoint ramping (p. 59). The manual does not say whether entering zone
  mode, or writing a zone, applies a zone at once, nor which zone a table whose upper
  bounds do not ascend applies: after every configuration write MAST reads RANGE? and
  turns the heater off if it is not off, and it refuses tables that do not ascend.
* The manual does not say whether the 335 takes setting commands over USB in local
  mode, nor whether ``SETP?`` reports the live ramping setpoint or the final one.
  Nothing here depends on either: every write is read back, a readback is accepted
  under both readings of SETP?, and every step of the sequence below is safe if the
  instrument ignores the command.

This module may write these commands and nothing else; :func:`build_write` is its only
byte factory, and tests pin that:

* ``SETP <out>,<K>``;
* ``RAMP <out>,<0|1>,<K/min>``: 0.1 to 100 in steps of 0.1. A rate of 0 means "no ramp"
  to the 335 and is never sent;
* ``RANGE <out>,<0..3>``: off, low (1 % of the output's power), medium (10 %), high;
* ``ZONE <out>,<zone>,<upper bound K>,<P>,<I>,<D>,<manual output %>,<range>,<input>,<K/min>``:
  one row of the zone table (p. 126). Each field is checked against the manual's range
  (zone 1-10, P and I 0.1-1000, D 0-200 %, range 0-3, input 0 = default / 1 = A / 2 = B,
  rate 0.1-100 K/min; upper bound 0-500 K here), and the manual output at most the
  maximum manual output set at initialization (``heater_max_mout_pct``, default 0 = none:
  in closed loop it is added to the PID output, p. 61; it is a percentage of the range's
  full scale, and RANGE 0 ("an Off setting for turning the output off", p. 64) takes the
  output away whatever the manual output is). MAST writes a manual output only as a zone
  row's field, never with ``MOUT`` itself. Only
  :meth:`Heater335Controller.write_zone_table` sends it. Zone mode sets the heater range
  and the ramp rate by itself from this table, so its values are commissioning decisions
  made with the operator present (autotune is not used),
  checked against the initialization limits before anything is written;
* ``OUTMODE <out>,<1|2>,<control input>,0``: closed-loop PID or zone (p. 121) on the
  configured control input, power-up enable off. Only :meth:`set_control_mode` sends it,
  and only when nothing but the mode changes;
* a bare line terminator before a heater-off: it ends whatever a write that failed
  half-way may have left in the instrument's input buffer, so ``RANGE <out>,0`` is not
  glued onto it.

It never writes PID, manual output (MOUT), heater setup (HTRSET), the temperature limit
(TLIMIT), any other control mode, control input or power-up setting, sensor types,
curves, emulation, the interface mode, autotune (ATUNE) or a reset. Those belong to the
operator: MAST reads them and refuses when they are not what a safe change needs.

Turning a heater on (:meth:`Heater335Controller.start`), each step read back, and an
abort / heater-off / hold looked for before every step:

1. ``RAMP <out>,0,<rate>``: ramping off, so the next setpoint takes effect at once;
2. ``SETP <out>,<T now>``: the setpoint becomes the measured temperature (the interface
   equivalent of holding the Setpoint key for 3 s). The heater is still off, except in
   zone mode: when the setpoint crosses into another zone the 335 may load that zone's
   range. In zone mode MAST reads RANGE? here; no answer, or a range above the maximum,
   turns the heater off;
3. ``RAMP <out>,1,<rate>``;
4. ``RANGE <out>,<range>``: the heater comes on, regulating at the present temperature.
   In zone mode the range is the start zone's, and the step is skipped when RANGE?
   already reads it;
5. ``SETP <out>,<target>``: the ramp from here to the target begins.

With the heater already on, a change is steps 3 and 5 (and 4 when the range changes).
When a step is not taken: a heater this change turned on is turned off; a range this
change raised on a heater that was already on is put back (turned off if that does not
land); a command the instrument ignored before any of that leaves the heater as it was.

Holding (:meth:`hold`): ``SETP <out>,<T now>`` while ramping is still on, so nothing
jumps (the setpoint ramps toward the present temperature); then ``RAMP <out>,0,<rate>``
(the setpoint "will remain on the most current setting", p. 63). T now is read on the
input the output is actually controlled by, and only if it reads in kelvin; otherwise
the hold is ``RAMP <out>,0`` alone. Held means RAMP? reads ramping off and RAMPST? reads
0.

Heater off (:meth:`heater_off`): a bare terminator, then ``RANGE <out>,0``, then
``RANGE?``. In zone mode that is not enough: "If the settings are changed manually, the
controller will use the new setting while it is in the same zone and will update to the
programmed zone table settings when the setpoint crosses into a new zone" (§5.3 p. 69),
so a setpoint still ramping would load the next zone's range and turn the heater back on
by itself. So, FIRST, each output in zone mode (or whose mode does not answer) whose
setpoint ramps (or whose RAMPST? does not answer) gets ``RAMP <out>,0,<rate>`` with the
rate it already has, sent only (no readback, no sleep): "When an incomplete ramp is shut
off, the setpoint will remain on the most current setting" (p. 63), so stopping it first
leaves no window in which a boundary can be crossed. Then every ``RANGE <out>,0``, and
last RANGE? and RAMPST? are read together. Done means RANGE? reads 0 and, in zone mode,
RAMPST? reads 0; not that the bytes left. An off that is not done is retried in the
background until it is. In closed-loop PID nothing else is written. Available where
temperature control is set up, or while MAST's own heating is on (a change, the watch,
a retry); on a station that never set it up MAST writes nothing to the 335. Never raises.

Gates (:meth:`plan`). Each one refuses; none clamps.

* Any change:
  - output, control input, maximum setpoint, maximum heater range, the heater's rated
    current and maximum ramp rate are set at initialization;
  - the rated current: from ``HTRSET?`` (read, never written) the High full-scale current
    is the max user current when the max-current code is 0, otherwise the code's table
    value (``HTRSET_MAX_CURRENT_A``); each range step down divides the current by
    sqrt(10) (§4.5.1.7.8, p. 64); the maximum range's full-scale current must not exceed
    the rating (a margin for float noise only). An unreadable ``HTRSET?`` refuses. The
    refusal names both fixes: a lower maximum range, or a lower max user current at the
    panel;
  - the target is a finite number, at least ``MIN_TARGET_K`` and at most the maximum
    setpoint; the rate and the range are within their maxima;
  - the port answers as the pinned Model 335 and every reading below was answered;
  - no emulation (``EMUL? = 0,0``), the output is a current-mode heater output, the
    control mode is closed-loop PID or zone on the configured input, the heater stays
    off after a power cut (power-up enable off), manual output is within the maximum
    manual output (0 % unless set at initialization), and the control
    input reads in kelvin;
  - the control reading is valid (``RDGST? = 0``) and not above the maximum setpoint;
  - the 335's own temperature limit on the control input is on, at least the target
    plus the overshoot allowance or the maximum setpoint, whichever is lower, and no
    higher than the maximum setpoint (the hardware cut never sits above the stage's
    maximum);
  - the setpoint is not ramping now; the field is at rest: MAST is not changing it, and
    the Model 625's own cached state (ramp-done bit, error registers; read before this
    link is taken) shows no ramp, whoever started it, and no error. No 625 on this
    machine is at rest; more than one, or one that cannot be read, is not;
  - a change is not being supervised and no heater-off is waiting to read back;
  - the guard thermometer (optional, set at initialization) is fresh, valid and under
    its limit;
  - the tip is withdrawn. This is the default; the caller may waive it, and the waiver
    is written to the ledger.
  These are read again, holding the link, just before the first write.
* Zone mode additionally: the zone table passes :func:`validate_zone_table`; every zone
  the setpoint passes through from the start setpoint (T now, or the present setpoint
  when the heater is on) to the target has the heater on (range 1 to the maximum
  range); the range is the start zone's (a requested ``heater_range`` is ignored, and
  the plan says so); the ramp rate is the lower of the requested rate and the start
  zone's. Within a tolerance of a zone boundary both neighbouring zones count as on
  the way. The path is computed again, holding the link, before the first write.
* Raising (a target above the present temperature, or a higher range on a heater that
  is on, in zone mode the highest range on the way) additionally needs the user's own
  words (``user_authorization``), which must name a temperature, and the temperature the
  user authorized (``authorized_up_to_k``). The target must not exceed it, nor it the
  temperature the words name.

Supervision (:meth:`supervise`) polls every ``POLL_S`` and turns the heater off on a
control-sensor fault, a temperature above the overshoot ceiling (above the highest of
target, start setpoint and start temperature, never above the maximum setpoint), a heater
range above the maximum range, a present range whose full-scale current is above the
rated current or a manual output (``MOUT?``) above its maximum (both read again every
``ZONE_RECHECK_POLLS`` polls), the guard
thermometer over its limit, a heater saturated
for ``SATURATION_S`` without reaching the target, or three failed reads in a row. It
writes nothing when the instrument or the panel took over, with one exception below. In
zone mode a range change as the setpoint crosses zones is expected: a range of any zone
between the start setpoint and the target is accepted (SETP? may report the live or the
final setpoint), and the control mode leaving zone mode, the control input changing, or
the zone table changing (read again every ``ZONE_RECHECK_POLLS`` polls) counts as taken
over. It ends on arrival (the ramp finished and the temperature inside the settle band
for ``settle_s``), and in the other cases its docstring lists.

The watch (:meth:`background_step`): when supervision ends with the heater still on,
MAST keeps watching that output every ``WATCH_POLL_S`` and turns it off on the same
hard conditions (sensor fault, overtemperature, range above the maximum, guard,
saturation, contact lost for ``WATCH_LOST_S``), and writes nothing else, with the same
exception. It ends when the heater reads off (in zone mode: off and not ramping). The
ledger lets a restarted MAST pick it up again.

The exception: when heating MAST started or watches
is turned off elsewhere (ALL OFF, a TLIMIT trip, a sensor error, the panel) while the
setpoint ramps in zone mode, the next zone boundary would turn the heater back on
(§5.3 p. 69). MAST then writes ``RAMP <out>,0,<rate it has>`` and nothing else (the
setpoint stays where it is, p. 63), says so loudly, records it in the ledger as a
de-escalating write after a takeover, and keeps watching until the heater reads off and
the setpoint is not ramping. Heating MAST did not start is never written to.

Configuration (:meth:`write_zone_table`, :meth:`set_control_mode`): only where temperature
control is set up, with the output's heater reading off (``RANGE? = 0``), the setpoint not
ramping, no emulation, MAST not heating and not changing the field. The old table / mode is
read first and written to the ledger; the new one is validated before anything is sent
(zone mode needs the stored table to validate); every write is read back, and RANGE? must
still read 0 after each. On any failure MAST turns the heater off if it does not read off,
writes the old values back, and reports zone by zone what the instrument holds.

Heating and field changes never happen at the same time: a change is refused while MAST changes the field, and the field-control
module (``instruments/magnet_625``) refuses a field change while a heater output reads on
or MAST's heating is on (:meth:`Heater335Controller.interlock_state`).
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
from typing import Any, Callable, Sequence

from mast.environment.lakeshore_temp import LakeshoreError, LakeshoreLink, WrongController
from mast.environment.serial_transport import SerialUnavailable
from mast.i18n import tr

logger = logging.getLogger(__name__)

TERMINATOR = b"\r\n"
OUTPUTS = (1, 2)
INPUTS = ("A", "B")
#: The highest setpoint this module will ever format. The station's own maximum (set at
#: initialization) is lower; this is the structural ceiling of the byte factory.
MAX_COMMAND_K = 500.0
SETPOINT_DECIMALS = 3
RATE_MIN_K_PER_MIN = 0.1
RATE_MAX_K_PER_MIN = 100.0
RANGE_CODES = (0, 1, 2, 3)
RANGE_NAMES = {0: "off", 1: "low", 2: "medium", 3: "high"}
#: Lowest target accepted (K). Below this a reading is not a temperature anyway.
MIN_TARGET_K = 1.0
#: Below this a reading is not a temperature: an input with nothing on it reads 0.
TEMPERATURE_MIN_VALID_K = 0.1
#: A SETP? readback within this of what was sent is the same setpoint.
SETP_TOL_K = 0.01
#: A RAMP? rate readback within this of what was sent is the same rate.
RATE_TOL_K_PER_MIN = 0.05
#: The 335 may answer the old value right after a setting (QCoDeS waits 0.5 s too).
READBACK_DELAY_S = 0.5
#: "Raising" means a target this far above the present temperature.
RAISE_TOL_K = 0.05
#: How far above the reference the temperature may go before the heater is turned off:
#: max(absolute, relative x reference). PID overshoot of a degree or two is normal.
OVERSHOOT_ABS_K = 2.0
OVERSHOOT_REL = 0.10
#: Settle band: max(absolute, relative x target); and how long inside it is "arrived".
SETTLE_BAND_ABS_K = 0.1
SETTLE_BAND_REL = 0.01
SETTLE_S = 60.0
#: After the ramp has finished, how long the temperature gets to settle before
#: supervision ends with "not settled" (the heater keeps regulating, watched).
SETTLE_TIMEOUT_S = 1800.0
#: Guard thermometer reading may be at most this old (s).
GUARD_MAX_AGE_S = 60.0
#: A ramp still running this long after its expected end (1.5 x ETA + this) is overdue:
#: supervision ends and says so (the heater keeps regulating, watched). And a hard cap.
RAMP_GRACE_S = 600.0
MAX_SUPERVISE_S = 12 * 3600.0
POLL_S = 2.0
READ_FAILURES_BEFORE_OFF = 3
#: How long one heater-off attempt waits for the link; how long supervision keeps
#: retrying an off that does not read back before it ends (the retry goes on in the
#: background).
OFF_LOCK_TIMEOUT_S = 10.0
OFF_GIVE_UP_S = 120.0
#: Background retry of a heater-off that did not read back off: this often for
#: ``OFF_GIVE_UP_S``, then every ``OFF_RETRY_SLOW_S``, until it reads off.
OFF_RETRY_S = 2.0
OFF_RETRY_SLOW_S = 30.0
#: Heater output (HTR?, % of the range) at or above this is saturated. Saturated this
#: long while the temperature stays below the setpoint means the control sensor may not
#: see the heater (the classic runaway: the stage heats, the sensor does not), or the
#: range is too low; either way the heater goes off.
SATURATED_PCT = 99.5
SATURATION_S = 600.0
#: The watch over MAST-started heating after supervision ends.
WATCH_POLL_S = 10.0
WATCH_LOST_S = 120.0
#: Zone tables (§5.3 p. 69; ZONE p. 126): ten zones per output; the manual's ranges for
#: P, I and D; input 0 = "default" (keep the output's control input), 1 = A, 2 = B.
ZONE_NUMBERS = tuple(range(1, 11))
ZONE_P_RANGE = (0.1, 1000.0)
ZONE_I_RANGE = (0.1, 1000.0)
ZONE_D_RANGE = (0.0, 200.0)
ZONE_INPUT_CODES = (0, 1, 2)
#: The manual output (MOUT, ZONE row) is 0-100 % of the range's full scale (p. 61, p. 120).
MOUT_RANGE_PCT = (0.0, 100.0)
#: A ZONE? readback within these of what was sent is the same zone (upper bound: like a
#: setpoint; P, I, D: half their 0.1 resolution; manual output: half its 0.01 %).
ZONE_PID_TOL = 0.05
ZONE_MOUT_TOL_PCT = 0.005
#: Supervision in zone mode reads the zone table again every this many polls (ten
#: queries); a table that changed counts as taken over.
ZONE_RECHECK_POLLS = 15
#: The recheck (HTRSET? / MOUT? / TLIMIT?) that cannot read or parse this many rounds in a
#: row turns the heater off.
RECHECK_FAILS_BEFORE_OFF = 2
#: Control modes (OUTMODE, p. 121). MAST switches between 1 and 2 and heats in either.
MODE_CLOSED_LOOP = 1
MODE_ZONE = 2
CONTROL_MODES = (MODE_CLOSED_LOOP, MODE_ZONE)
MODE_NAMES = {0: "off", 1: "closed-loop PID", 2: "zone", 3: "open loop", 4: "monitor out",
              5: "warm-up supply"}
#: Shortest user-authorization text accepted for raising the temperature.
MIN_AUTHORIZATION_CHARS = 2
#: The examples in the skill's own documentation: copied verbatim, they are not a
#: user's words.
EXAMPLE_AUTHORIZATIONS = frozenset({"可以加热到 20 k", "ok to heat to 20 k"})

#: Profile keys (``core/instrument_profile``), entered on the initialization page.
PROFILE_OUTPUT = "heater_output"
PROFILE_CONTROL_INPUT = "heater_control_input"
PROFILE_MAX_SETPOINT = "heater_max_setpoint_k"
PROFILE_MAX_RANGE = "heater_max_range"
PROFILE_MAX_CURRENT = "heater_max_current_a"
PROFILE_MAX_MOUT = "heater_max_mout_pct"
PROFILE_MAX_RATE = "heater_max_rate_k_per_min"
PROFILE_GUARD_CHANNEL = "heater_guard_channel"
PROFILE_GUARD_MAX = "heater_guard_max_k"

# A number directly after CJK text ("加热到20K") must count: \w would include the CJK
# character and hide the number, so the look-behind names digits and the decimal point.
_TEMP_IN_TEXT = re.compile(
    r"(?<![0-9.])(\d+(?:\.\d+)?)\s*(K|kelvin|开尔文)(?![A-Za-z/])", re.IGNORECASE)

#: Supervision outcomes after which the heater is still on, regulating: the watch takes
#: over (when the last reading confirms the range is on).
_STAYS_ON = frozenset({"done", "not_settled", "ramp_overdue", "timeout", "held", "aborted",
                       "external_change"})


class HeaterControlError(RuntimeError):
    """Temperature control could not do what was asked (the message says why)."""


def temperatures_named_in(text: str) -> list[float]:
    """Temperatures (kelvin) a text names: ``"可以加热到20K"`` -> ``[20.0]``. ``K/min`` is not one."""
    out: list[float] = []
    for num, _unit in _TEMP_IN_TEXT.findall(str(text or "")):
        try:
            out.append(float(num))
        except ValueError:
            continue
    return out


def overshoot_allowance_k(reference_k: float) -> float:
    return max(OVERSHOOT_ABS_K, OVERSHOOT_REL * abs(float(reference_k)))


def settle_band_k(target_k: float) -> float:
    return max(SETTLE_BAND_ABS_K, SETTLE_BAND_REL * abs(float(target_k)))


# ── the only byte factory ─────────────────────────────────────────────────────
def _whole(v: Any, allowed: tuple[int, ...], what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v not in allowed:
        raise ValueError(f"{what} must be one of {allowed}, got {v!r}")
    return int(v)


def _finite(v: Any) -> "float | None":
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _number(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ValueError(f"{what} needs a finite number, got {v!r}")
    return float(v)


def _within(x: float, lo: float, hi: float, what: str) -> float:
    if not lo <= x <= hi:
        raise ValueError(f"{what} {x:g} is outside {lo:g}..{hi:g}")
    return x


def build_write(verb: str, output: Any = None, *values: Any, max_mout_pct: float = 0.0,
                max_setpoint_k: "float | None" = None) -> bytes:
    """``SETP`` / ``RAMP`` / ``RANGE`` / ``ZONE`` / ``OUTMODE`` for one output, CR LF
    terminated, or ``FLUSH`` (a bare terminator). Anything else raises ``ValueError``.

    ``ZONE`` takes zone, upper bound (K), P, I, D (%), manual output (%), range, input and
    rate (K/min), in the manual's order (p. 126), and refuses a manual output outside
    0 to *max_mout_pct* (the initialization cap, default 0: none). ``SETP`` and a ``ZONE``
    row's upper bound need *max_setpoint_k* (the initialization limit): without it, or above
    it, nothing is built (no setpoint is ever written above the stage's maximum). ``OUTMODE`` takes mode (1 closed-loop PID or 2 zone), control input (1 = A,
    2 = B) and power-up enable, which must be 0 (p. 121)."""
    v = str(verb).strip().upper()
    if v == "FLUSH":
        if output is not None or values:
            raise ValueError("FLUSH takes nothing")
        return TERMINATOR
    out = _whole(output, OUTPUTS, "output")
    if v == "RANGE":
        if len(values) != 1:
            raise ValueError("RANGE takes one value")
        payload = f"RANGE {out},{_whole(values[0], RANGE_CODES, 'heater range')}"
    elif v == "SETP":
        if len(values) != 1:
            raise ValueError("SETP takes one value")
        k = values[0]
        if isinstance(k, bool) or not isinstance(k, (int, float)) or not math.isfinite(float(k)):
            raise ValueError(f"SETP needs a finite number, got {k!r}")
        k = round(float(k), SETPOINT_DECIMALS) + 0.0
        if not 0.0 <= k <= MAX_COMMAND_K:
            raise ValueError(f"setpoint {k} K is outside 0..{MAX_COMMAND_K} K")
        cap_k = _finite(max_setpoint_k)
        if cap_k is None:
            raise ValueError("SETP needs the maximum setpoint set at initialization")
        if k > cap_k + 1e-9:
            raise ValueError(f"setpoint {k} K is above the maximum setpoint {cap_k:g} K")
        payload = f"SETP {out},{k:.{SETPOINT_DECIMALS}f}"
    elif v == "RAMP":
        if len(values) != 2:
            raise ValueError("RAMP takes on/off and a rate")
        on = _whole(values[0], (0, 1), "ramp on/off")
        r = values[1]
        if isinstance(r, bool) or not isinstance(r, (int, float)) or not math.isfinite(float(r)):
            raise ValueError(f"ramp rate needs a finite number, got {r!r}")
        r = round(float(r), 1)
        if not RATE_MIN_K_PER_MIN <= r <= RATE_MAX_K_PER_MIN:
            raise ValueError(f"ramp rate {r} K/min is outside {RATE_MIN_K_PER_MIN}.."
                             f"{RATE_MAX_K_PER_MIN} (0 would mean 'no ramp')")
        payload = f"RAMP {out},{on},{r:.1f}"
    elif v == "ZONE":
        if len(values) != 9:
            raise ValueError("ZONE takes zone, upper bound, P, I, D, manual output, range, "
                             "input and rate")
        zone = _whole(values[0], ZONE_NUMBERS, "zone")
        ub = _within(round(_number(values[1], "zone upper bound"), SETPOINT_DECIMALS) + 0.0,
                     0.0, MAX_COMMAND_K, "zone upper bound (K)")
        cap_k = _finite(max_setpoint_k)
        if cap_k is None:
            raise ValueError("ZONE needs the maximum setpoint set at initialization")
        if ub > cap_k + 1e-9:
            raise ValueError(f"zone upper bound {ub} K is above the maximum setpoint {cap_k:g} K")
        p = _within(round(_number(values[2], "P"), 1) + 0.0, *ZONE_P_RANGE, "P")
        i = _within(round(_number(values[3], "I"), 1) + 0.0, *ZONE_I_RANGE, "I")
        d = _within(round(_number(values[4], "D"), 1) + 0.0, *ZONE_D_RANGE, "D (%)")
        cap = min(max(float(max_mout_pct), MOUT_RANGE_PCT[0]), MOUT_RANGE_PCT[1])
        mout = round(_number(values[5], "zone manual output"), 2) + 0.0
        if not 0.0 <= mout <= cap:
            raise ValueError(f"zone manual output {values[5]!r} % is outside 0..{cap:g} % (the "
                             "maximum manual output set at initialization)")
        rng = _whole(values[6], RANGE_CODES, "zone heater range")
        inp = _whole(values[7], ZONE_INPUT_CODES, "zone control input")
        rate = _within(round(_number(values[8], "zone ramp rate"), 1) + 0.0,
                       RATE_MIN_K_PER_MIN, RATE_MAX_K_PER_MIN, "zone ramp rate (K/min)")
        payload = (f"ZONE {out},{zone},{ub:.{SETPOINT_DECIMALS}f},{p:.1f},{i:.1f},{d:.1f},"
                   f"{mout:.2f},{rng},{inp},{rate:.1f}")
    elif v == "OUTMODE":
        if len(values) != 3:
            raise ValueError("OUTMODE takes mode, control input and power-up enable")
        mode = _whole(values[0], CONTROL_MODES, "control mode")
        inp = _whole(values[1], (1, 2), "control input")
        powerup = _whole(values[2], (0,), "power-up enable")
        payload = f"OUTMODE {out},{mode},{inp},{powerup}"
    else:
        raise ValueError(f"not a temperature-control command: {verb!r}")
    raw = payload.encode("ascii")
    if b";" in raw or b"\r" in raw or b"\n" in raw:      # structurally impossible; pinned
        raise ValueError(f"malformed payload {payload!r}")
    return raw + TERMINATOR


# ── settings (entered on the initialization page) ─────────────────────────────
def _choice_int(v: Any, allowed: tuple[int, ...]) -> "int | None":
    if isinstance(v, bool):
        return None
    try:
        i = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return i if i in allowed else None


_RANGE_BY_NAME = {"low": 1, "medium": 2, "med": 2, "high": 3, "低": 1, "中": 2, "高": 3}

#: ``HTRSET`` max-current codes other than 0 = user specified (manual p. 116). Code 3 is
#: printed 1.141 A on that page and 1.414 A in §4.5.1.3, Table 4-11 and the specifications:
#: a transposed digit. The larger is used here, which can only refuse more.
HTRSET_MAX_CURRENT_A = {1: 0.707, 2: 1.0, 3: 1.414, 4: 1.732}
#: Float noise only: the rating gate compares the full-scale current with the rating
#: within this relative margin.
RATING_REL_TOL = 1e-6


def parse_htrset(setup: Any) -> "tuple | None":
    """``HTRSET? <out>`` replies ``<type>,<htr resistance>,<max current>,<max user current>,
    <current/power>`` (``n,n,n,+n.nnn,n``, manual p. 116) for output 1 and output 2 alike:
    exactly five fields, type 0/1, resistance 1/2, max-current code 0-4, a finite user
    current, current/power 1/2. ``None`` for anything else (a format that does not match is
    refused, never guessed at)."""
    if not isinstance(setup, (list, tuple)) or len(setup) != 5:
        return None
    typ, res, code, user, cp = _int(setup[0]), _int(setup[1]), _int(setup[2]), _num(setup[3]), _int(setup[4])
    if typ not in (0, 1) or res not in (1, 2) or code not in (0, 1, 2, 3, 4) or user is None \
            or cp not in (1, 2):
        return None
    return typ, res, code, user, cp


def heater_full_scale_a(setup: Any, heater_range: int) -> "float | None":
    """Full-scale current (A) of a heater range, from ``HTRSET?``'s fields (type,
    resistance, max-current code, max user current, current/power): the High full scale is
    the max user current when the code is 0 (user specified), otherwise the code's table
    value; each range step down divides power by 10, so current by sqrt(10) (§4.5.1.7.8,
    p. 64). Range 0 (off) is 0 A. ``None`` when the fields cannot be read as that."""
    if heater_range not in (0, 1, 2, 3) or parse_htrset(setup) is None:
        return None
    code = _int(setup[2])
    if code == 0:
        high = _num(setup[3])
        if high is None or high <= 0:
            return None
    elif code in HTRSET_MAX_CURRENT_A:
        high = HTRSET_MAX_CURRENT_A[code]
    else:
        return None
    if heater_range == 0:
        return 0.0
    return high / math.sqrt(10.0) ** (3 - heater_range)


def rating_verdict(setup: Any, heater_range: int, rating_a: "float | None") -> "tuple[bool, str]":
    """Whether *heater_range* at the ``HTRSET?`` setup stays within the heater's rated
    current. Not readable, or no rating configured: not ok (unknown is not safe)."""
    name = RANGE_NAMES.get(heater_range, heater_range)
    shown = ",".join(str(x) for x in setup) if isinstance(setup, (list, tuple)) else repr(setup)
    if rating_a is None or not rating_a > 0:
        return False, tr("初始化里没有填加热器额定电流（A）：在这之前 MAST 不开加热器",
                         "the heater's rated current (A) is not set at initialization; until it is, "
                         "MAST does not turn a heater on")
    fs = heater_full_scale_a(setup, heater_range)
    if fs is None:
        return False, tr(f"HTRSET? 读到 {shown}，算不出{name}量程的满量程电流：读不到不是答案",
                         f"HTRSET? read {shown}: the full-scale current of the {name} range cannot "
                         "be worked out (unknown is not an answer)")
    if fs > rating_a * (1.0 + RATING_REL_TOL):
        return False, tr(
            f"{name}量程的满量程电流 {fs * 1000:.0f} mA 超过加热器额定 {rating_a * 1000:.0f} mA"
            f"（HTRSET? {shown}）。改小最大加热量程，或在面板上把该输出 HTRSET 的最大用户电流设为 "
            f"{rating_a:g} A（MAST 从不写 HTRSET）",
            f"the {name} range's full-scale current {fs * 1000:.0f} mA is above the heater's rating "
            f"{rating_a * 1000:.0f} mA (HTRSET? {shown}). Set the maximum range lower, or set the "
            f"output's HTRSET max user current to {rating_a:g} A at the panel (MAST never writes "
            "HTRSET)")
    return True, tr(f"{name}量程的满量程电流 {fs * 1000:.0f} mA，不超过额定 {rating_a * 1000:.0f} mA",
                    f"the {name} range's full-scale current {fs * 1000:.0f} mA is within the "
                    f"{rating_a * 1000:.0f} mA rating")


def _range_code(v: Any) -> "int | None":
    """A heater range 1..3 from ``1``/``"1"``/``"low"``/``"低"``; anything else ``None``.
    0 (off) is not a range a change asks for: that is :meth:`heater_off`."""
    if isinstance(v, str) and v.strip().lower() in _RANGE_BY_NAME:
        return _RANGE_BY_NAME[v.strip().lower()]
    return _choice_int(v, (1, 2, 3))


_MODE_BY_NAME = {"closed_loop": MODE_CLOSED_LOOP, "closed-loop": MODE_CLOSED_LOOP,
                 "pid": MODE_CLOSED_LOOP, "闭环": MODE_CLOSED_LOOP, "zone": MODE_ZONE,
                 "分区": MODE_ZONE}


def _control_mode_code(v: Any) -> "int | None":
    """1 (closed-loop PID) or 2 (zone) from ``1``/``"1"``/``"closed_loop"``/``"zone"``."""
    if isinstance(v, str) and v.strip().lower() in _MODE_BY_NAME:
        return _MODE_BY_NAME[v.strip().lower()]
    return _choice_int(v, CONTROL_MODES)


def _mout_cap(v: "float | None") -> float:
    """The configured manual-output cap: 0 (off) unless a number from 0 to 100 is set."""
    return float(v) if v is not None and MOUT_RANGE_PCT[0] <= v <= MOUT_RANGE_PCT[1] else 0.0


@dataclass(frozen=True)
class HeaterSettings:
    output: "int | None" = None
    control_input: str = ""
    max_setpoint_k: "float | None" = None
    max_range: "int | None" = None
    max_current_a: "float | None" = None
    #: Upper limit of the per-zone feed-forward manual output (MOUT), %; 0 = none.
    max_mout_pct: float = 0.0
    max_rate_k_per_min: "float | None" = None
    guard_channel: str = ""
    guard_max_k: "float | None" = None

    @classmethod
    def from_profile(cls) -> "HeaterSettings":
        try:
            from mast.core.instrument_profile import get_config
        except Exception:  # noqa: BLE001 - no profile module: nothing configured
            return cls()

        def _f(key: str) -> "float | None":
            return _finite(get_config(key, None))

        inp = str(get_config(PROFILE_CONTROL_INPUT, "") or "").strip().upper()
        return cls(
            output=_choice_int(get_config(PROFILE_OUTPUT, None), OUTPUTS),
            control_input=inp if inp in INPUTS else "",
            max_setpoint_k=_f(PROFILE_MAX_SETPOINT),
            max_range=_choice_int(get_config(PROFILE_MAX_RANGE, None), (1, 2, 3)),
            max_current_a=_f(PROFILE_MAX_CURRENT),
            max_mout_pct=_mout_cap(_f(PROFILE_MAX_MOUT)),
            max_rate_k_per_min=_f(PROFILE_MAX_RATE),
            guard_channel=str(get_config(PROFILE_GUARD_CHANNEL, "") or "").strip(),
            guard_max_k=_f(PROFILE_GUARD_MAX),
        )

    @property
    def control_configured(self) -> bool:
        """Everything a change needs is set: MAST may turn a heater on at all."""
        return (self.output in OUTPUTS and self.control_input in INPUTS
                and self.max_setpoint_k is not None
                and MIN_TARGET_K <= self.max_setpoint_k <= MAX_COMMAND_K
                and self.max_range in (1, 2, 3)
                and self.max_current_a is not None and self.max_current_a > 0
                and self.max_rate_k_per_min is not None
                and RATE_MIN_K_PER_MIN <= self.max_rate_k_per_min <= RATE_MAX_K_PER_MIN)

    @property
    def guard_configured(self) -> bool:
        return bool(self.guard_channel) and self.guard_max_k is not None

    def as_dict(self) -> dict:
        return {"output": self.output, "control_input": self.control_input,
                "max_setpoint_k": self.max_setpoint_k, "max_range": self.max_range,
                "max_current_a": self.max_current_a, "max_mout_pct": self.max_mout_pct,
                "max_rate_k_per_min": self.max_rate_k_per_min,
                "guard_channel": self.guard_channel, "guard_max_k": self.guard_max_k,
                "control_configured": self.control_configured}


# ── zone tables (§5.3 p. 69; ZONE / ZONE? p. 126) ─────────────────────────────
@dataclass(frozen=True)
class Zone:
    """One row of an output's zone table, in the order ``ZONE`` takes it (p. 126)."""

    upper_bound_k: float
    p: float
    i: float
    d: float
    mout_pct: float
    heater_range: int
    input_code: int
    rate_k_per_min: float

    def values(self) -> tuple:
        """The fields in ``ZONE``'s order, for :func:`build_write`."""
        return (self.upper_bound_k, self.p, self.i, self.d, self.mout_pct, self.heater_range,
                self.input_code, self.rate_k_per_min)

    def as_dict(self) -> dict:
        return {"upper_bound_k": self.upper_bound_k, "p": self.p, "i": self.i, "d": self.d,
                "mout_pct": self.mout_pct, "range": self.heater_range,
                "input": self.input_code, "rate_k_per_min": self.rate_k_per_min}

    @classmethod
    def parse(cls, text: Any) -> "Zone | None":
        """A ``ZONE?`` reply (``<upper bound>,<P>,<I>,<D>,<mout>,<range>,<input>,<rate>``),
        or ``None`` when the text is not one."""
        f = _fields(text)
        if len(f) != 8:
            return None
        nums = [_num(x) for x in f]
        rng, inp = _int(f[5]), _int(f[6])
        if any(x is None for x in nums) or rng is None or inp is None:
            return None
        return cls(nums[0], nums[1], nums[2], nums[3], nums[4], rng, inp, nums[7])

    @classmethod
    def from_dict(cls, d: Any) -> "Zone | None":
        """The inverse of :meth:`as_dict`; ``None`` stays ``None`` (a zone not read)."""
        if d is None:
            return None
        return cls(float(d["upper_bound_k"]), float(d["p"]), float(d["i"]), float(d["d"]),
                   float(d["mout_pct"]), int(d["range"]), int(d["input"]),
                   float(d["rate_k_per_min"]))

    def same_as(self, other: "Zone | None") -> bool:
        """The same zone within the readback tolerances (formatting rounds; the 335 may
        report fewer decimals than were sent)."""
        if other is None:
            return False
        return (abs(self.upper_bound_k - other.upper_bound_k) <= SETP_TOL_K
                and abs(self.p - other.p) <= ZONE_PID_TOL
                and abs(self.i - other.i) <= ZONE_PID_TOL
                and abs(self.d - other.d) <= ZONE_PID_TOL
                and abs(self.mout_pct - other.mout_pct) <= ZONE_MOUT_TOL_PCT
                and self.heater_range == other.heater_range
                and self.input_code == other.input_code
                and abs(self.rate_k_per_min - other.rate_k_per_min) <= RATE_TOL_K_PER_MIN)


#: A zone nobody set up: the factory defaults (Table 8-1, p. 134), upper bound 0 K.
UNUSED_ZONE = Zone(0.0, 50.0, 20.0, 0.0, 0.0, 0, 0, 0.1)

_ZONE_FIELDS = {
    "upper_bound_k": ("upper_bound_k", "upper_bound", "ub"),
    "p": ("p",), "i": ("i",), "d": ("d",),
    "mout_pct": ("mout_pct", "mout", "manual_output"),
    "range": ("range", "heater_range"),
    "input": ("input", "control_input"),
    "rate_k_per_min": ("rate_k_per_min", "rate"),
}
_ZONE_ALIASES = {alias: key for key, names in _ZONE_FIELDS.items() for alias in names}


def _zone_range_code(v: Any) -> "int | None":
    """0..3 from ``0``..``3`` / ``"off"`` / ``"low"`` / ``"medium"`` / ``"high"`` / 关低中高."""
    if isinstance(v, str) and v.strip().lower() in ("off", "关"):
        return 0
    if isinstance(v, str) and v.strip().lower() in _RANGE_BY_NAME:
        return _RANGE_BY_NAME[v.strip().lower()]
    return _choice_int(v, RANGE_CODES)


def _zone_input_code(v: Any) -> "int | None":
    """0 (default: keep the output's control input) / 1 (A) / 2 (B)."""
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "default", "默认")):
        return 0
    if isinstance(v, str) and v.strip().upper() in INPUTS:
        return INPUTS.index(v.strip().upper()) + 1
    return _choice_int(v, ZONE_INPUT_CODES)


def zone_table_from(value: Any, *, max_mout_pct: float = 0.0) -> list[Zone]:
    """Ten zones from what a caller gives: a JSON array (text) or a list of one to ten
    objects, zone 1 first. Each needs ``upper_bound_k``, ``p``, ``i``, ``d``, ``range``
    (0-3 or off / low / medium / high) and ``rate_k_per_min``; ``mout_pct`` may be given
    and must be 0 to *max_mout_pct* (the initialization cap, default 0); ``input`` is 0 / "default" (keep the output's control input) or the
    control input ("A" / "B"). The zones after the last one given are unused
    (:data:`UNUSED_ZONE`). Raises :class:`HeaterControlError` with the reason."""
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise HeaterControlError(tr(f"分区表不是合法的 JSON：{exc}",
                                        f"the zone table is not valid JSON: {exc}")) from exc
    if isinstance(raw, dict) and "zones" in raw:
        raw = raw["zones"]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise HeaterControlError(tr("分区表要是一个数组，第 1 区在前，1 到 10 个分区",
                                    "the zone table must be an array, zone 1 first, of 1 to 10 "
                                    "zones"))
    if len(raw) > len(ZONE_NUMBERS):
        raise HeaterControlError(tr(f"分区表最多 10 区，给了 {len(raw)} 区",
                                    f"a zone table has at most 10 zones, got {len(raw)}"))
    out: list[Zone] = []
    for n, item in enumerate(raw, 1):
        if isinstance(item, Zone):
            out.append(item)
            continue
        if not isinstance(item, dict):
            raise HeaterControlError(tr(f"第 {n} 区不是一个对象：{item!r}",
                                        f"zone {n} is not an object: {item!r}"))
        got: dict = {}
        for k, v in item.items():
            key = _ZONE_ALIASES.get(str(k).strip().lower())
            if key is None:
                raise HeaterControlError(tr(
                    f"第 {n} 区有不认识的字段 {k!r}（可用：{', '.join(_ZONE_FIELDS)}）",
                    f"zone {n} has an unknown field {k!r} (known: {', '.join(_ZONE_FIELDS)})"))
            if key in got:
                raise HeaterControlError(tr(f"第 {n} 区的 {key} 给了两次", f"zone {n} gives {key} twice"))
            got[key] = v
        missing = [k for k in ("upper_bound_k", "p", "i", "d", "range", "rate_k_per_min")
                   if k not in got]
        if missing:
            raise HeaterControlError(tr(f"第 {n} 区缺少 {', '.join(missing)}",
                                        f"zone {n} lacks {', '.join(missing)}"))
        nums: dict = {}
        for k in ("upper_bound_k", "p", "i", "d", "rate_k_per_min"):
            x = _finite(got[k])
            if x is None:
                raise HeaterControlError(tr(f"第 {n} 区的 {k} 不是有限数字：{got[k]!r}",
                                            f"zone {n}: {k} is not a finite number: {got[k]!r}"))
            nums[k] = x
        mout = _finite(got.get("mout_pct", 0.0))
        if mout is None or not 0.0 <= mout <= max_mout_pct + ZONE_MOUT_TOL_PCT:
            raise HeaterControlError(tr(
                f"第 {n} 区的手动输出必须在 0–{max_mout_pct:g} %（初始化里的最大手动输出），"
                f"给的是 {got.get('mout_pct')!r}",
                f"zone {n}: the manual output must be 0-{max_mout_pct:g} % (the maximum manual "
                f"output set at initialization), got {got.get('mout_pct')!r}"))
        mout = min(round(mout, 2), max_mout_pct)
        rng = _zone_range_code(got["range"])
        if rng is None:
            raise HeaterControlError(tr(f"第 {n} 区的量程 {got['range']!r} 不是 0-3 / off / low / medium / high",
                                        f"zone {n}: range {got['range']!r} is not 0-3 / off / "
                                        "low / medium / high"))
        inp = _zone_input_code(got.get("input"))
        if inp is None:
            raise HeaterControlError(tr(f"第 {n} 区的输入 {got.get('input')!r} 不是 default / A / B",
                                        f"zone {n}: input {got.get('input')!r} is not default / "
                                        "A / B"))
        out.append(Zone(nums["upper_bound_k"], nums["p"], nums["i"], nums["d"], mout, rng, inp,
                        nums["rate_k_per_min"]))
    while len(out) < len(ZONE_NUMBERS):
        out.append(UNUSED_ZONE)
    return out


@dataclass
class ZoneTableVerdict:
    ok: bool
    used: list[int]
    problems: list[str]
    covers_k: "float | None" = None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "used": list(self.used), "problems": list(self.problems),
                "covers_k": self.covers_k}


def used_zones(zones: Sequence["Zone | None"]) -> list[int]:
    """The zones in use: the leading ones whose upper bounds are above 0 K and strictly
    ascending (zone 1 starts at 0 K and each zone ends at its upper bound, p. 69)."""
    used: list[int] = []
    prev = 0.0
    for n, z in enumerate(zones, 1):
        if z is None or not z.upper_bound_k > prev:
            break
        used.append(n)
        prev = z.upper_bound_k
    return used


def validate_zone_table(zones: Sequence["Zone | None"], *, control_input: str, max_range: int,
                        max_rate_k_per_min: float, max_setpoint_k: float,
                        max_mout_pct: float = 0.0) -> ZoneTableVerdict:
    """May MAST heat in zone mode with this table? Pure; every problem is listed.

    * all ten zones were read (unknown is not an answer);
    * the used zones (:func:`used_zones`) are followed only by unused ones (upper bound
      0 K): the manual does not say which zone the 335 applies when the bounds do not
      ascend;
    * each used zone: P 0.1-1000, I 0.1-1000, D 0-200 % (p. 126), manual output 0 to
      *max_mout_pct* (the initialization cap; 0 = none), heater range at most the maximum range, input 0 (default)
      or the configured control input (MAST never switches sensors: switching "could
      cause a temporary instability", p. 69), rate 0.1 K/min to the maximum ramp rate;
    * the last used zone reaches the maximum setpoint, so no setpoint MAST may ask for
      lies outside the table.
    """
    zs = list(zones)
    problems: list[str] = []
    if len(zs) != len(ZONE_NUMBERS):
        problems.append(tr(f"分区表有 {len(zs)} 区，应为 10 区", f"the table has {len(zs)} zones, not 10"))
        return ZoneTableVerdict(False, [], problems)
    missing = [n for n, z in enumerate(zs, 1) if z is None]
    if missing:
        problems.append(tr(f"这些分区没读到：{missing}（读不到不是答案）",
                           f"zones not read: {missing} (unknown is not an answer)"))
        return ZoneTableVerdict(False, [], problems)
    used = used_zones(zs)
    if not used:
        problems.append(tr("分区表是空的：第 1 区的上界是 0 K，没有在用的分区",
                           "the zone table is empty: zone 1's upper bound is 0 K, no zone is in use"))
        return ZoneTableVerdict(False, [], problems)
    last = used[-1]
    for n in range(last + 1, len(zs) + 1):
        ub = zs[n - 1].upper_bound_k
        if ub != 0.0:
            problems.append(tr(
                f"第 {n} 区的上界 {ub:g} K 不在第 {last} 区的 {zs[last - 1].upper_bound_k:g} K 之上接着升："
                "说明书没说这时 335 用哪一区。不用的分区请把上界设成 0 K",
                f"zone {n}'s upper bound {ub:g} K does not continue upwards from zone {last}'s "
                f"{zs[last - 1].upper_bound_k:g} K: the manual does not say which zone the 335 "
                "then uses. Set the upper bound of every unused zone to 0 K"))
    want_in = INPUTS.index(control_input) + 1 if control_input in INPUTS else None
    for n in used:
        z = zs[n - 1]
        for name, x, (lo, hi) in (("P", z.p, ZONE_P_RANGE), ("I", z.i, ZONE_I_RANGE),
                                  ("D", z.d, ZONE_D_RANGE)):
            if not lo <= x <= hi:
                problems.append(tr(f"第 {n} 区的 {name} = {x:g}，应在 {lo:g}–{hi:g}",
                                   f"zone {n}: {name} = {x:g}, allowed {lo:g}-{hi:g}"))
        if not -ZONE_MOUT_TOL_PCT <= z.mout_pct <= max_mout_pct + ZONE_MOUT_TOL_PCT:
            problems.append(tr(f"第 {n} 区的手动输出是 {z.mout_pct:g} %：必须在 0–{max_mout_pct:g} %"
                               "（初始化里的最大手动输出；闭环下它直接叠加在 PID 输出上）",
                               f"zone {n}: manual output {z.mout_pct:g} %: it must be 0-"
                               f"{max_mout_pct:g} % (the maximum manual output set at "
                               "initialization; in closed loop it is added to the PID output)"))
        if not 0 <= z.heater_range <= max_range:
            problems.append(tr(
                f"第 {n} 区的量程是 {RANGE_NAMES.get(z.heater_range, z.heater_range)}，初始化允许到 "
                f"{RANGE_NAMES.get(max_range, max_range)}",
                f"zone {n}: heater range {RANGE_NAMES.get(z.heater_range, z.heater_range)}, "
                f"initialization allows up to {RANGE_NAMES.get(max_range, max_range)}"))
        if z.input_code not in (0, want_in):
            problems.append(tr(
                f"第 {n} 区把控制输入换成代码 {z.input_code}：MAST 不换传感器，只用 0（默认）或初始化设定的 "
                f"{control_input}",
                f"zone {n} switches the control input to code {z.input_code}: MAST never switches "
                f"sensors; use 0 (default) or the configured input {control_input}"))
        if not RATE_MIN_K_PER_MIN <= z.rate_k_per_min <= max_rate_k_per_min + 1e-9:
            problems.append(tr(
                f"第 {n} 区的扫温速率 {z.rate_k_per_min:g} K/min，允许 {RATE_MIN_K_PER_MIN:g}–"
                f"{max_rate_k_per_min:g} K/min",
                f"zone {n}: ramp rate {z.rate_k_per_min:g} K/min, allowed {RATE_MIN_K_PER_MIN:g}-"
                f"{max_rate_k_per_min:g} K/min"))
    covers = zs[last - 1].upper_bound_k
    if covers < max_setpoint_k - 1e-9:
        problems.append(tr(
            f"最后一个在用的分区（第 {last} 区）只到 {covers:g} K，低于最高设定温度 {max_setpoint_k:g} K："
            "更高的设定值会落在分区表外",
            f"the last zone in use (zone {last}) ends at {covers:g} K, below the maximum "
            f"setpoint {max_setpoint_k:g} K: higher setpoints would lie outside the table"))
    return ZoneTableVerdict(not problems, used, problems, covers)


def zone_of(setpoint_k: float, zones: Sequence[Zone], used: Sequence[int]) -> "int | None":
    """The used zone a setpoint is in: the first whose upper bound it does not exceed."""
    for n in used:
        if setpoint_k <= zones[n - 1].upper_bound_k + 1e-9:
            return n
    return None


def zones_on_path(a_k: float, b_k: float, zones: Sequence[Zone], used: Sequence[int], *,
                  tol: float = SETP_TOL_K) -> "list[int] | None":
    """The used zones a setpoint moving between *a_k* and *b_k* passes through. Within
    *tol* of a boundary both neighbours count (which side the 335 picks exactly at a
    boundary is not something to bet a heater range on). ``None`` when part of the way
    lies above the last used zone."""
    lo, hi = sorted((float(a_k), float(b_k)))
    if not used or hi > zones[used[-1] - 1].upper_bound_k + 1e-9:
        return None
    out: list[int] = []
    lower = 0.0                                # zone 1 starts at 0 K
    for n in used:
        upper = zones[n - 1].upper_bound_k
        if upper >= lo - tol and lower < hi + tol:
            out.append(n)
        lower = upper
    return out


def zone_heating_path(zones: Sequence[Zone], used: Sequence[int], start_setpoint_k: float,
                      target_k: float, *, control_input: str, max_range: int,
                      max_rate_k_per_min: float) -> dict:
    """What zone mode does on the way from *start_setpoint_k* to *target_k*, and what is
    wrong with it: every zone on the way must have the heater on (range 1 to
    *max_range*), keep the control input and ramp no faster than the maximum.

    ``{"path", "start_zone", "range" (the start zone's), "ranges" (on the way),
    "rate_k_per_min" (the start zone's), "problems"}``."""
    out: dict = {"path": None, "start_zone": None, "range": None, "ranges": [],
                 "rate_k_per_min": None, "problems": []}
    path = zones_on_path(start_setpoint_k, target_k, zones, used)
    start = zone_of(start_setpoint_k, zones, used)
    if path is None or start is None:
        top = zones[used[-1] - 1].upper_bound_k if used else 0.0
        out["problems"].append(tr(
            f"从 {start_setpoint_k:g} K 到 {target_k:g} K 有一段在分区表之外（最后一区到 {top:g} K）",
            f"part of the way from {start_setpoint_k:g} K to {target_k:g} K lies outside the "
            f"zone table (the last zone ends at {top:g} K)"))
        return out
    want_in = INPUTS.index(control_input) + 1 if control_input in INPUTS else None
    out.update(path=path, start_zone=start, range=zones[start - 1].heater_range,
               ranges=sorted({zones[n - 1].heater_range for n in path}),
               rate_k_per_min=zones[start - 1].rate_k_per_min)
    for n in path:
        z = zones[n - 1]
        if not 1 <= z.heater_range <= max_range:
            out["problems"].append(tr(
                f"途经的第 {n} 区量程是 {RANGE_NAMES.get(z.heater_range, z.heater_range)}："
                + ("加热器会在半路被分区表关掉" if z.heater_range == 0 else "超过初始化允许的最大量程"),
                f"zone {n} on the way has range {RANGE_NAMES.get(z.heater_range, z.heater_range)}: "
                + ("the zone table would turn the heater off half way" if z.heater_range == 0 else
                   "above the maximum range set at initialization")))
        if z.input_code not in (0, want_in):
            out["problems"].append(tr(f"途经的第 {n} 区会换控制输入（代码 {z.input_code}）",
                                      f"zone {n} on the way switches the control input (code "
                                      f"{z.input_code})"))
        if z.rate_k_per_min > max_rate_k_per_min + 1e-9:
            out["problems"].append(tr(f"途经的第 {n} 区扫温速率 {z.rate_k_per_min:g} K/min 超过最大速率",
                                      f"zone {n} on the way ramps at {z.rate_k_per_min:g} K/min, "
                                      "above the maximum rate"))
    return out


def zone_eta_s(start_setpoint_k: float, target_k: float, zones: Sequence[Zone],
               used: Sequence[int], first_rate_k_per_min: float) -> "float | None":
    """Seconds for the setpoint to ramp from start to target in zone mode: MAST's rate
    until the first zone boundary, then each zone's own rate."""
    lo, hi = sorted((float(start_setpoint_k), float(target_k)))
    start = zone_of(start_setpoint_k, zones, used)
    path = zones_on_path(lo, hi, zones, used, tol=0.0)
    if path is None or start is None or first_rate_k_per_min <= 0:
        return None
    total, lower = 0.0, 0.0
    for n in used:
        upper = zones[n - 1].upper_bound_k
        if n in path:
            seg = max(0.0, min(hi, upper) - max(lo, lower))
            rate = first_rate_k_per_min if n == start else zones[n - 1].rate_k_per_min
            if seg > 0:
                if rate <= 0:
                    return None
                total += seg / rate * 60.0
        lower = upper
    return total


# ── the plan: what a change would do, and every gate's verdict ────────────────
@dataclass
class Check:
    id: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {"id": self.id, "ok": self.ok, "detail": self.detail}


@dataclass
class HeaterPlan:
    target_k: "float | None"
    rate_k_per_min: "float | None" = None
    heater_range: "int | None" = None
    output: "int | None" = None
    control_input: str = ""
    present_k: "float | None" = None
    present_setpoint_k: "float | None" = None
    present_range: "int | None" = None
    temperature_limit_k: "float | None" = None
    raising: "bool | None" = None
    noop: bool = False
    eta_s: "float | None" = None
    #: The control mode read (1 closed-loop PID, 2 zone); in zone mode the table's verdict,
    #: the zones on the way, their ranges, and the range / rate asked for (ignored / capped).
    control_mode: "int | None" = None
    zone_table: "dict | None" = None
    zone_path: "list[int] | None" = None
    zone_ranges: "list[int] | None" = None
    requested_range: "int | None" = None
    requested_rate_k_per_min: "float | None" = None
    instrument: dict = field(default_factory=dict)
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
            "target_k": self.target_k, "rate_k_per_min": self.rate_k_per_min,
            "heater_range": self.heater_range,
            "heater_range_name": RANGE_NAMES.get(self.heater_range or -1),
            "output": self.output, "control_input": self.control_input,
            "present_k": self.present_k, "present_setpoint_k": self.present_setpoint_k,
            "present_range": self.present_range,
            "temperature_limit_k": self.temperature_limit_k,
            "raising": self.raising, "noop": self.noop, "eta_s": self.eta_s,
            "control_mode": self.control_mode,
            "control_mode_name": MODE_NAMES.get(self.control_mode if self.control_mode is not None else -1),
            "zone_table": dict(self.zone_table) if self.zone_table else None,
            "zone_path": list(self.zone_path) if self.zone_path is not None else None,
            "zone_ranges": list(self.zone_ranges) if self.zone_ranges is not None else None,
            "requested_range": self.requested_range,
            "requested_rate_k_per_min": self.requested_rate_k_per_min,
            "instrument": dict(self.instrument),
            "ok": self.ok, "checks": [c.as_dict() for c in self.checks],
            "refusals": [c.as_dict() for c in self.refusals],
        }


def _num(text: Any) -> "float | None":
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _fields(text: Any) -> list[str]:
    return [p.strip() for p in str(text or "").split(",")]


def _int(text: Any) -> "int | None":
    v = _num(text)
    return None if v is None or v != int(v) else int(v)


def _ints(text: Any) -> "list[int | None]":
    return [_int(p) for p in _fields(text)]


def _default_temperature(channel: str):
    from mast.core.temperature import latest_temperature

    return latest_temperature(channel)


def _default_ledger_path() -> Path:
    from mast._runtime_paths import data_root

    return Path(data_root()) / "experiments" / "heater_control_ledger.jsonl"


def _default_field_busy() -> bool:
    """MAST is changing the magnet's field right now (7.2.0 field control). Raises when it
    cannot be asked: the callers treat that as "not known", never as "not busy". Whether a
    Model 625 is there at all, and what its panel is doing, is :func:`_default_field_state`."""
    from mast.core import magnet_control

    ctl = magnet_control.controller()
    return bool(getattr(ctl, "busy", False)) if ctl is not None else False


def _default_field_state() -> dict:
    """``core.magnet_control.field_state()``: ``{"ok", "present", "detail"}`` for the one
    Model 625 (no 625 here = ok; unknown = not ok). It may read the 625's cached state, so
    it is called without this module's link held."""
    from mast.core import magnet_control

    return magnet_control.field_state()


#: The configuration a change depends on, re-read just before the first write.
_CONFIG_KEYS = ("emulation", "outmode", "input_type", "temperature_limit_k",
                "manual_output_pct", "heater_setup")


# ── the controller ────────────────────────────────────────────────────────────
class Heater335Controller:
    """Temperature control for one Model 335, over an identity-checked link.

    One per link (the runtime keeps it). ``plan`` is a dry run; ``start`` turns the
    heater on / changes the setpoint when every gate passes; ``supervise`` follows the
    change; ``hold`` stops a ramp where it is; ``heater_off`` never raises; the
    background thread retries offs that did not read back and watches MAST's heating
    after supervision (tests drive it through :meth:`background_step`).
    """

    def __init__(self, link: LakeshoreLink, *,
                 settings: "Callable[[], HeaterSettings]" = HeaterSettings.from_profile,
                 temperature: "Callable[[str], Any] | None" = None,
                 field_busy: "Callable[[], bool] | None" = None,
                 field_state: "Callable[[], dict] | None" = None,
                 ledger_path: "Callable[[], Path] | None" = None,
                 clock: "Callable[[], float]" = time.monotonic,
                 sleep: "Callable[[float], None]" = time.sleep,
                 background: bool = True):
        self.link = link
        self._settings = settings
        self._temperature = temperature or _default_temperature
        self._field_busy = field_busy or _default_field_busy
        self._field_state = field_state or _default_field_state
        self._ledger_path = ledger_path or _default_ledger_path
        self._clock = clock
        self._sleep = sleep
        self._background = bool(background)
        self._change_lock = threading.Lock()   # held while a change is prepared and sent
        self._state_lock = threading.Lock()
        self._off_seq = 0                      # bumped by every heater_off() request
        self._off_req: dict[int, int] = {}     # output -> latest off request covering it
        self._off_ok: dict[int, int] = {}      # output -> latest off whose RANGE? read 0
        self._offs_in_flight = 0
        self._hold_seq = 0                     # bumped by every hold() call
        self._closing = False
        self._active: "dict | None" = None     # the MAST change being supervised
        self._last: "dict | None" = None       # how the previous one ended
        self._last_off: "dict | None" = None
        self._last_hold: "dict | None" = None
        self._watch: "dict | None" = None      # MAST heating left on after supervision
        self._retry_off: dict[int, float] = {}  # output -> when the unconfirmed off began
        self._bg_thread: "threading.Thread | None" = None
        self._bg_lock = threading.Lock()
        self._last_loud = 0.0
        resumed = self._resume_from_ledger()
        if resumed is not None:
            self._watch = resumed
            logger.warning("heater control: MAST heating on output %s found in the ledger; "
                           "watching it again", resumed.get("output"))
            self._ensure_background()

    # ── status (no I/O) ──
    def settings(self) -> HeaterSettings:
        try:
            return self._settings()
        except Exception as exc:  # noqa: BLE001 - unreadable settings = not configured
            logger.warning("heater control: settings unreadable: %s", exc)
            return HeaterSettings()

    @property
    def active(self) -> "dict | None":
        with self._state_lock:
            return dict(self._active) if self._active else None

    @property
    def busy(self) -> bool:
        """A MAST change is being prepared, sent or supervised, or a zone table / control
        mode write is running (they hold the same lock)."""
        return self._change_lock.locked() or self.active is not None

    @property
    def watching(self) -> bool:
        return self._watch is not None

    @property
    def retrying(self) -> bool:
        return bool(self._retry_off)

    def mast_heating(self) -> bool:
        """MAST's own heating is on or being turned off: a change, the watch, or a retry."""
        return self.busy or self.watching or self.retrying

    def status(self) -> dict:
        s = self.settings()
        busy = self.busy
        with self._state_lock:
            return {"port": self.link.port, "settings": s.as_dict(),
                    "can_heat": s.control_configured, "busy": busy,
                    "identity": dict(self.link.identity),
                    "active": dict(self._active) if self._active else None,
                    "watch": dict(self._watch) if self._watch else None,
                    "retrying_off": sorted(self._retry_off),
                    "last": dict(self._last) if self._last else None,
                    "last_off": dict(self._last_off) if self._last_off else None,
                    "last_hold": dict(self._last_hold) if self._last_hold else None}

    # ── reads ──
    def read_instrument(self, output: int, control_input: str) -> dict:
        """Everything a change looks at, for one output and its control input. Each
        field is ``None`` when it was not answered (unknown is not an answer). Raises
        :class:`WrongController` / :class:`SerialUnavailable` for the port as a whole."""
        q = self.link.query
        out = str(int(output))
        inp = str(control_input)
        r: dict = {"output": int(output), "control_input": inp}

        def _ask(key: str, verb: str, arg: "str | None", parse: Callable[[str], Any]) -> None:
            try:
                r[key] = parse(q(verb, arg))
            except (LakeshoreError, ValueError) as exc:
                if isinstance(exc, WrongController):
                    raise
                r[key] = None

        _ask("emulation", "EMUL?", None, _ints)
        _ask("interface_mode", "MODE?", None, _int)
        _ask("kelvin", "KRDG?", inp, _num)
        _ask("reading_status", "RDGST?", inp, _int)
        _ask("input_type", "INTYPE?", inp, _ints)
        _ask("temperature_limit_k", "TLIMIT?", inp, _num)
        _ask("outmode", "OUTMODE?", out, _ints)
        _ask("heater_setup", "HTRSET?", out, _fields)
        _ask("range", "RANGE?", out, _int)
        _ask("setpoint_k", "SETP?", out, _num)
        _ask("ramp", "RAMP?", out, _fields)
        _ask("ramping", "RAMPST?", out, _int)
        _ask("manual_output_pct", "MOUT?", out, _num)
        _ask("heater_pct", "HTR?", out, _num)
        _ask("pid", "PID?", out, _fields)
        r["zones"] = self._read_zones(int(output))
        return r

    def _read_zones(self, output: int) -> "list[dict | None]":
        """The output's ten zones (``ZONE? <out>,<n>``) as dicts; a zone not answered is
        ``None``. Raises :class:`WrongController` / :class:`SerialUnavailable`."""
        zones: "list[dict | None]" = []
        for n in ZONE_NUMBERS:
            try:
                z = Zone.parse(self.link.query("ZONE?", f"{int(output)},{n}"))
            except WrongController:
                raise
            except (LakeshoreError, ValueError):
                z = None
            zones.append(z.as_dict() if z is not None else None)
        return zones

    def _rating_recheck(self, output: int, control_input: str, present_range: int,
                        s: HeaterSettings, polls: int, state: dict,
                        basis_k: "float | None") -> "tuple[str, str] | None":
        """Supervision and the watch, every ``ZONE_RECHECK_POLLS`` polls (the same cadence as
        the zone table), queries only: ``HTRSET?`` against the rated current (a hard
        condition when the present range's full scale is above it: someone raised the
        maximum current at the panel), ``MOUT?`` against the maximum manual output, and
        ``TLIMIT?`` (the 335's own cut: read as off, above the maximum setpoint, or, when the
        basis is known, below the target plus the overshoot allowance, is a hard condition).
        A read that does not answer or parse is tried again; ``RECHECK_FAILS_BEFORE_OFF``
        rounds in a row (counted in *state*) are a hard condition too."""
        if polls % ZONE_RECHECK_POLLS != ZONE_RECHECK_POLLS - 1:
            return None
        q = self.link.query
        o = str(int(output))
        try:
            setup = _fields(q("HTRSET?", o))
            mout = _num(q("MOUT?", o))
            tl = _num(q("TLIMIT?", control_input))
        except WrongController:
            raise
        except (SerialUnavailable, LakeshoreError, ValueError):
            setup, mout, tl = None, None, None
        if (setup is None or mout is None or tl is None
                or (s.max_current_a is not None
                    and heater_full_scale_a(setup, int(present_range)) is None)):
            state["recheck_fails"] = int(state.get("recheck_fails") or 0) + 1
            if state["recheck_fails"] >= RECHECK_FAILS_BEFORE_OFF:
                return ("recheck_unreadable", tr(
                    f"HTRSET? / MOUT? / TLIMIT? 连续 {state['recheck_fails']} 次读不到或读不懂："
                    "不知道额定电流、手动输出和 335 自己的温度上限还守不守得住：已关加热器",
                    f"HTRSET? / MOUT? / TLIMIT? could not be read or understood {state['recheck_fails']} "
                    "times in a row: the rated current, the manual output and the 335's own limit "
                    "cannot be vouched for: heater turned off"))
            return None
        state["recheck_fails"] = 0
        ok, detail = rating_verdict(setup, int(present_range), s.max_current_a)
        if not ok:
            return ("current_over_rating", detail + tr("：已关加热器", ": heater turned off"))
        # The manual output is added to the PID output: above the cap it is a hard condition.
        cap = float(s.max_mout_pct)
        if mout > cap + 1e-6:
            return ("mout_over_cap", tr(
                f"手动输出 {mout:g} % 超过初始化的最大手动输出 {cap:g} %：已关加热器",
                f"manual output {mout:g} % is above the maximum manual output {cap:g} % set at "
                "initialization: heater turned off"))
        if s.max_setpoint_k is not None:
            max_sp = float(s.max_setpoint_k)
            if tl <= 0:
                bad = tr("335 自己的温度上限被关掉了", "the 335's own temperature limit was turned off")
            elif tl > max_sp + SETP_TOL_K:
                bad = tr(f"335 自己的温度上限 {tl:g} K 高于最高设定温度 {max_sp:g} K",
                         f"the 335's own temperature limit {tl:g} K is above the maximum setpoint "
                         f"{max_sp:g} K")
            elif basis_k is not None and tl < min(float(basis_k) + overshoot_allowance_k(basis_k),
                                                  max_sp) - 1e-9:
                bad = tr(f"335 自己的温度上限 {tl:g} K 低于目标加允许过冲",
                         f"the 335's own temperature limit {tl:g} K is below the target plus the "
                         "overshoot allowance")
            else:
                bad = ""
            if bad:
                return ("temperature_limit", bad + tr("：已关加热器", ": heater turned off"))
        return None

    def _quick_status(self, output: int, control_input: str, *, outmode: bool = False) -> dict:
        """The few readings supervision, the watch and the pre-write recheck need (and
        ``OUTMODE?`` when asked: supervision in zone mode). Raises :class:`LakeshoreError`
        when any is missing."""
        q = self.link.query
        out = str(int(output))
        k = _num(q("KRDG?", control_input))
        st = _int(q("RDGST?", control_input))
        setp = _num(q("SETP?", out))
        rng = _int(q("RANGE?", out))
        ramping = _int(q("RAMPST?", out))
        htr = _num(q("HTR?", out))
        if None in (k, st, setp, rng, ramping):
            raise LakeshoreError("incomplete status")
        res = {"kelvin": k, "reading_status": st, "setpoint_k": setp, "range": rng,
               "ramping": ramping, "heater_pct": htr}
        if outmode:
            om = _ints(q("OUTMODE?", out))
            if len(om) < 3 or None in om[:3]:
                raise LakeshoreError("incomplete status (OUTMODE?)")
            res["outmode"] = om[:3]
        return res

    # ── dry run ──
    def plan(self, target_k: Any, *, rate_k_per_min: Any = None, heater_range: Any = None,
             user_authorization: str = "", authorized_up_to_k: Any = None,
             require_tip_withdrawn: bool = True,
             tip_verdict: "Callable[[], Any] | None" = None) -> HeaterPlan:
        """Every gate's verdict for regulating at *target_k* (kelvin). Writes nothing.

        The slow checks (tip, guard thermometer) run without holding the link.
        """
        tgt = _finite(target_k)
        plan = HeaterPlan(target_k=tgt)
        s = self.settings()

        # 1. what the operator entered at initialization
        if not plan.add("configured", s.control_configured, tr(
                "初始化里已设定加热输出、控制输入、最高设定温度、最大加热量程、加热器额定电流与最大升温速率"
                if s.control_configured else
                "初始化里还没有设定控温（加热输出、控制输入、最高设定温度、最大加热量程、加热器额定电流、"
                "最大升温速率）：在这之前 MAST 不开加热器。请操作员在 设置 → 仪器初始化 里填写",
                "output, control input, maximum setpoint, maximum heater range, heater rated "
                "current and maximum ramp rate are set at initialization" if s.control_configured else
                "temperature control is not set up at initialization (heater output, control "
                "input, maximum setpoint, maximum heater range, heater rated current, maximum "
                "ramp rate); until it is, MAST does not turn a heater on. Ask the operator to fill it in under "
                "Settings -> Instrument initialization")):
            return plan
        out, inp = int(s.output), s.control_input
        plan.output, plan.control_input = out, inp
        max_sp = float(s.max_setpoint_k)

        # 2. the request itself
        if not plan.add("target", tgt is not None, tr(
                f"目标温度 {target_k!r} 不是有限数字", f"target {target_k!r} is not a finite number")
                if tgt is None else tr(f"目标 {tgt:g} K", f"target {tgt:g} K")):
            return plan
        plan.add("max_setpoint", MIN_TARGET_K <= tgt <= max_sp + 1e-9, tr(
            f"目标 {tgt:g} K，允许 {MIN_TARGET_K:g}–{max_sp:g} K（上限是初始化设定的最高设定温度）",
            f"target {tgt:g} K, allowed {MIN_TARGET_K:g}-{max_sp:g} K (the maximum setpoint "
            "set at initialization)"))
        rate = _finite(rate_k_per_min) if rate_k_per_min is not None else float(s.max_rate_k_per_min)
        if rate is not None:
            rate = round(rate, 1)
        plan.rate_k_per_min = plan.requested_rate_k_per_min = rate
        plan.add("rate", rate is not None and RATE_MIN_K_PER_MIN <= rate <= float(s.max_rate_k_per_min) + 1e-9,
                 tr(f"升温速率 {rate} K/min，允许 {RATE_MIN_K_PER_MIN}–{s.max_rate_k_per_min:g} K/min",
                    f"ramp rate {rate} K/min, allowed {RATE_MIN_K_PER_MIN}-"
                    f"{s.max_rate_k_per_min:g} K/min"))
        rng = _range_code(heater_range) if heater_range is not None else int(s.max_range)
        plan.heater_range = plan.requested_range = rng
        plan.add("heater_range", rng is not None and rng <= int(s.max_range), tr(
            f"加热量程 {RANGE_NAMES.get(rng or -1, heater_range)}，初始化允许到 {RANGE_NAMES[int(s.max_range)]}",
            f"heater range {RANGE_NAMES.get(rng or -1, heater_range)}, initialization allows up to "
            f"{RANGE_NAMES[int(s.max_range)]}"))

        # 3. the instrument, read now (each query holds the link only briefly)
        try:
            r = self.read_instrument(out, inp)
        except WrongController as exc:
            plan.add("identity", False, tr(f"端口上不是那台 Model 335：{exc}",
                                           f"not the Model 335 on the port: {exc}"))
            return plan
        except (SerialUnavailable, LakeshoreError) as exc:
            plan.add("readable", False, tr(f"读不到温控仪：{exc}", f"cannot read the controller: {exc}"))
            return plan
        plan.instrument = {k: v for k, v in r.items()}
        plan.present_k = r.get("kelvin")
        plan.present_setpoint_k = r.get("setpoint_k")
        plan.present_range = r.get("range")
        plan.temperature_limit_k = r.get("temperature_limit_k")
        needed = ("emulation", "kelvin", "reading_status", "input_type", "temperature_limit_k",
                  "outmode", "heater_setup", "range", "setpoint_k", "ramp", "ramping",
                  "manual_output_pct")
        unread = [k for k in needed if r.get(k) is None]
        if not plan.add("readable", not unread, tr(
                "状态读齐了" if not unread else f"这些状态没读到：{', '.join(unread)}（读不到不是答案）",
                "state read in full" if not unread else
                f"not answered: {', '.join(unread)} (unknown is not an answer)")):
            return plan

        emul = r["emulation"]
        plan.add("no_emulation", len(emul) >= 1 and emul[0] == 0, tr(
            f"EMUL? = {','.join(str(x) for x in emul)}" + ("" if emul and emul[0] == 0 else
                                                           "：仪器在仿真 331/332，命令含义不同，MAST 不操作"),
            f"EMUL? = {','.join(str(x) for x in emul)}" + ("" if emul and emul[0] == 0 else
                                                           ": the controller emulates a 331/332, where "
                                                           "commands mean other things; MAST does not "
                                                           "operate it")))
        setup = r["heater_setup"]
        htr_type = _int(setup[0]) if setup else None
        plan.add("current_output", out == 1 or htr_type == 0, tr(
            f"输出 {out} 是电流型加热输出" if out == 1 or htr_type == 0 else
            f"输出 {out} 配置成电压输出（HTRSET? 类型 {htr_type}）：不是加热输出，MAST 不操作",
            f"output {out} is a current-mode heater output" if out == 1 or htr_type == 0 else
            f"output {out} is configured as a voltage output (HTRSET? type {htr_type}): not a "
            "heater output; MAST does not operate it"))
        # The largest current the configured maximum range can put into the heater, from
        # the setup as read now (never written), against the rated current.
        plan.add("current_rating", *rating_verdict(setup, int(s.max_range), s.max_current_a))
        om = r["outmode"]
        mode = om[0] if len(om) > 0 else None
        om_in = om[1] if len(om) > 1 else None
        powerup = om[2] if len(om) > 2 else None
        want_in = INPUTS.index(inp) + 1
        plan.control_mode = mode
        plan.add("closed_loop", mode in CONTROL_MODES, tr(
            f"控制模式 {mode}" + ("（闭环 PID）" if mode == MODE_CLOSED_LOOP else
                                 "（分区：量程、速率、PID 跟着设定值所在的分区走）" if mode == MODE_ZONE else
                                 "：必须是闭环 PID（1）或分区（2）。开环（3）没有任何调节。"
                                 "用 SetHeaterControlMode 在 1 和 2 之间切换；其他模式请操作员在面板上处理"),
            f"control mode {mode}" + (" (closed-loop PID)" if mode == MODE_CLOSED_LOOP else
                                     " (zone: range, rate and PID follow the zone the setpoint "
                                     "is in)" if mode == MODE_ZONE else
                                     ": must be closed-loop PID (1) or zone (2). Open loop (3) "
                                     "regulates nothing. SetHeaterControlMode switches between "
                                     "1 and 2; any other mode is the operator's, at the panel")))
        plan.add("control_input", om_in == want_in, tr(
            f"输出 {out} 的控制输入是 {om_in}（初始化设定 {inp} = {want_in}）",
            f"output {out} is controlled by input {om_in} (initialization: {inp} = {want_in})"))
        plan.add("powerup_off", powerup == 0, tr(
            "断电恢复后加热器保持关闭（powerup enable = 0）" if powerup == 0 else
            "断电恢复后加热器会自己回到原量程（powerup enable = 1）：MAST 不在场时不能这样。"
            "请操作员在面板上关掉 Powerup Enable",
            "after a power cut the heater stays off (powerup enable = 0)" if powerup == 0 else
            "after a power cut the heater would come back on by itself (powerup enable = 1); "
            "with nobody supervising that must not happen. Ask the operator to turn Powerup "
            "Enable off at the panel"))
        mout = r["manual_output_pct"]
        cap = float(s.max_mout_pct)
        mout_ok = mout is not None and -1e-6 <= mout <= cap + 1e-6
        plan.add("no_manual_output", mout_ok, tr(
            f"手动输出 {mout} %，不超过初始化的最大手动输出 {cap:g} %" if mout_ok else
            f"手动输出 {mout} %：闭环下它直接叠加在 PID 输出上，必须在 0–{cap:g} %（初始化里的最大手动输出）",
            f"manual output {mout} %, within the maximum manual output {cap:g} % set at "
            "initialization" if mout_ok else
            f"manual output {mout} %: in closed loop it is added to the PID output; it must be "
            f"0-{cap:g} % (the maximum manual output set at initialization)"))
        itype = r["input_type"]
        units = itype[4] if len(itype) > 4 else None
        plan.add("kelvin_units", units == 1, tr(
            f"输入 {inp} 的设定值单位代码 {units}" + ("（开尔文）" if units == 1 else
                                                 "：必须是开尔文（1），否则 SETP 的数不是温度、扫温也不工作"),
            f"input {inp} setpoint units code {units}" + (" (kelvin)" if units == 1 else
                                                         ": must be kelvin (1); otherwise a "
                                                         "setpoint is not a temperature and "
                                                         "ramping does not work")))
        k, st = r["kelvin"], r["reading_status"]
        valid = st == 0 and k is not None and k >= TEMPERATURE_MIN_VALID_K
        plan.add("reading_valid", valid, tr(
            f"输入 {inp} 读数 {k} K，RDGST? = {st}" + ("" if valid else "：读数无效，不加热"),
            f"input {inp} reads {k} K, RDGST? = {st}" + ("" if valid else
                                                        ": the reading is not valid; no heating")))
        if valid:
            plan.add("present_below_max", k <= max_sp + SETP_TOL_K, tr(
                f"当前 {k:.3f} K，最高设定温度 {max_sp:g} K",
                f"present {k:.3f} K, maximum setpoint {max_sp:g} K"))
        plan.add("instrument_limit", *self._limit_verdict(r["temperature_limit_k"], tgt, max_sp, inp))
        plan.add("not_ramping", r["ramping"] == 0, tr(
            "设定值没有在扫" if r["ramping"] == 0 else
            "设定值正在扫温：先等它结束，或用 HoldSampleTemperature 停住",
            "the setpoint is not ramping" if r["ramping"] == 0 else
            "the setpoint is ramping: wait for it, or stop it with HoldSampleTemperature first"))
        field_ok, field_detail = self._field_verdict()
        plan.add("field_steady", field_ok, field_detail)

        # 4. the zone table; in zone mode, what it will do on the way. The way starts where
        #    the write sequence starts it: T now, or the present setpoint with the heater on.
        start_sp: "float | None" = None
        if valid:
            start_sp = (float(r["setpoint_k"]) if int(r["range"] or 0) > 0
                        else round(float(k), SETPOINT_DECIMALS))
        zones, used = self._plan_zones(plan, r, s, tgt, start_sp, gate=mode == MODE_ZONE)

        # 5. direction, nothing-to-do, duration (with the range and rate in force)
        if valid:
            present_rng = int(r["range"] or 0)
            eff_rng = plan.heater_range
            # More power is raising too: a higher range on a heater that is on (in zone
            # mode, the highest range any zone on the way will load).
            most = max(plan.zone_ranges) if mode == MODE_ZONE and plan.zone_ranges else eff_rng
            plan.raising = (tgt > k + RAISE_TOL_K
                            or (present_rng > 0 and most is not None and most > present_rng))
            sp = r["setpoint_k"]
            plan.noop = (r["range"] == eff_rng and r["ramping"] == 0
                         and abs(float(sp) - tgt) <= SETP_TOL_K)
            if plan.rate_k_per_min and start_sp is not None:
                if mode == MODE_ZONE and zones is not None and plan.zone_path:
                    plan.eta_s = zone_eta_s(start_sp, tgt, zones, used, plan.rate_k_per_min)
                elif mode != MODE_ZONE:
                    plan.eta_s = abs(tgt - start_sp) / plan.rate_k_per_min * 60.0

        # 6. raising only
        if plan.raising:
            self._check_authorization(plan, tgt, user_authorization, authorized_up_to_k)

        # 7. slow checks without the link: guard thermometer, tip
        if s.guard_configured:
            self._check_guard(plan, s)
        if require_tip_withdrawn:
            self._check_tip(plan, tip_verdict)
        else:
            plan.add("tip", True, tr("调用方声明：针尖不退也可以改变温度",
                                     "the caller waived the withdrawn-tip requirement"))
        return plan

    @staticmethod
    def _limit_verdict(tl: "float | None", tgt: float, max_sp: float, inp: str) -> "tuple[bool, str]":
        """The 335's own temperature limit (``TLIMIT``, p. 125) on the control input: on, at
        least the target plus the overshoot allowance or the maximum setpoint, whichever is
        lower, and no higher than the maximum setpoint (plus ``SETP_TOL_K``). The maximum
        setpoint is the stage's absolute maximum, so the hardware cut never sits above it,
        and a target near the maximum needs the limit right at the maximum."""
        need = min(tgt + overshoot_allowance_k(tgt), max_sp)
        if tl is None or tl <= 0:
            return False, tr(
                f"335 自己的温度上限（TLIMIT {inp}）没开：MAST 不在场时它是唯一不靠 MAST 的保护。"
                f"请操作员在面板上为控制输入设 Temperature Limit（{need:g}–{max_sp:g} K）",
                f"the 335's own temperature limit (TLIMIT {inp}) is off. It is the only protection "
                "that does not depend on MAST. Ask the operator to set Temperature Limit on the "
                f"control input at the panel ({need:g}-{max_sp:g} K)")
        if tl > max_sp + SETP_TOL_K:
            return False, tr(
                f"335 自己的温度上限（TLIMIT {inp}）= {tl:g} K，高于最高设定温度 {max_sp:g} K。最高设定温度是"
                f"样品台的绝对上限，硬件切断不能设得比它高：请操作员把 TLIMIT 设到 {need:g}–{max_sp:g} K",
                f"the 335's own temperature limit (TLIMIT {inp}) = {tl:g} K is above the maximum "
                f"setpoint {max_sp:g} K. The maximum setpoint is the stage's absolute maximum, and "
                "the hardware cut must not sit above it: ask the operator to set TLIMIT to "
                f"{need:g}-{max_sp:g} K")
        if tl < need - 1e-9:
            return False, tr(
                f"335 自己的温度上限（TLIMIT {inp}）= {tl:g} K：目标 {tgt:g} K 加允许过冲要 ≥ {need:g} K"
                f"（封顶于最高设定温度 {max_sp:g} K）",
                f"the 335's own temperature limit (TLIMIT {inp}) = {tl:g} K: the target {tgt:g} K "
                f"plus the overshoot allowance needs >= {need:g} K (capped at the maximum setpoint "
                f"{max_sp:g} K)")
        return True, tr(f"335 自己的温度上限（TLIMIT {inp}）= {tl:g} K，在 {need:g}–{max_sp:g} K 之内",
                        f"the 335's own temperature limit (TLIMIT {inp}) = {tl:g} K, within "
                        f"{need:g}-{max_sp:g} K")

    def _plan_zones(self, plan: HeaterPlan, r: dict, s: HeaterSettings, tgt: float,
                    start_sp: "float | None", *, gate: bool
                    ) -> "tuple[list[Zone | None] | None, list[int]]":
        """The zone table's verdict, always shown; in zone mode (*gate*) also the gates
        ``zone_table`` and ``zone_path``, and the range and rate zone mode imposes: the
        start zone's range replaces the requested one, and the rate is the lower of the
        requested one and the start zone's."""
        raw = r.get("zones")
        if not isinstance(raw, list):
            if gate:
                plan.add("zone_table", False, tr("分区表没读到（读不到不是答案）",
                                                 "the zone table was not read (unknown is not an answer)"))
            return None, []
        zones = [Zone.from_dict(d) for d in raw]
        verdict = validate_zone_table(zones, control_input=s.control_input,
                                      max_range=int(s.max_range),
                                      max_rate_k_per_min=float(s.max_rate_k_per_min),
                                      max_setpoint_k=float(s.max_setpoint_k),
                                      max_mout_pct=float(s.max_mout_pct))
        plan.zone_table = verdict.as_dict()
        if not gate:
            return zones, verdict.used
        if not plan.add("zone_table", verdict.ok, tr(
                f"分区表可用：在用第 {verdict.used[0]}–{verdict.used[-1]} 区，最后一区到 {verdict.covers_k:g} K"
                if verdict.ok else "分区表不能用于加热：" + "；".join(verdict.problems),
                f"the zone table is usable: zones {verdict.used[0]}-{verdict.used[-1]} in use, the "
                f"last ending at {verdict.covers_k:g} K" if verdict.ok else
                "the zone table cannot be used for heating: " + "; ".join(verdict.problems))):
            return zones, verdict.used
        if start_sp is None:                     # no valid reading: reading_valid refused
            return zones, verdict.used
        hp = zone_heating_path(zones, verdict.used, start_sp, tgt, control_input=s.control_input,
                               max_range=int(s.max_range),
                               max_rate_k_per_min=float(s.max_rate_k_per_min))
        plan.zone_path, plan.zone_ranges = hp["path"], hp["ranges"]
        names = ", ".join(f"{n}: {RANGE_NAMES.get(zones[n - 1].heater_range)}" for n in hp["path"] or [])
        plan.add("zone_path", not hp["problems"], tr(
            f"途经分区 {names}（从 {start_sp:g} K 到 {tgt:g} K）" if not hp["problems"] else
            "分区路径不能用：" + "；".join(hp["problems"]),
            f"zones on the way {names} (from {start_sp:g} K to {tgt:g} K)" if not hp["problems"] else
            "the way through the zones cannot be used: " + "; ".join(hp["problems"])))
        if hp["start_zone"] is not None:
            zr, zrate = hp["range"], hp["rate_k_per_min"]
            req_rng, req_rate = plan.requested_range, plan.requested_rate_k_per_min
            plan.heater_range = zr
            if req_rate is not None and zrate is not None:
                plan.rate_k_per_min = round(min(req_rate, zrate), 1)
            plan.add("zone_range", True, tr(
                f"分区模式：起点在第 {hp['start_zone']} 区，量程 {RANGE_NAMES.get(zr, zr)}、速率 "
                f"{plan.rate_k_per_min} K/min 由分区表定（取请求速率与该区速率的较小者）"
                + (f"；请求的量程 {RANGE_NAMES.get(req_rng, req_rng)} 不用" if req_rng is not None else ""),
                f"zone mode: the way starts in zone {hp['start_zone']}; range "
                f"{RANGE_NAMES.get(zr, zr)} and rate {plan.rate_k_per_min} K/min come from the "
                "zone table (the rate is the lower of the requested one and the zone's)"
                + (f"; the requested range {RANGE_NAMES.get(req_rng, req_rng)} is ignored"
                   if req_rng is not None else "")))
        return zones, verdict.used

    def _check_authorization(self, plan: HeaterPlan, tgt: float, words: Any, up_to: Any) -> None:
        auth = str(words or "").strip()
        is_example = auth.casefold() in EXAMPLE_AUTHORIZATIONS
        plan.add("user_authorization", len(auth) >= MIN_AUTHORIZATION_CHARS and not is_example, tr(
            f"用户授权：「{auth}」" if auth and not is_example else
            "这是技能说明里的示例，不是用户的话：升温要用户本人的授权原话" if is_example else
            "升温需要用户的授权：把用户授权时说的话逐字填进 user_authorization。没有授权就不升温",
            f"user authorization: \"{auth}\"" if auth and not is_example else
            "that is the example from the skill's documentation, not the user's words" if is_example else
            "raising the temperature needs the user's authorization: put the user's own words "
            "in user_authorization. No authorization, no heating"))
        named = temperatures_named_in(auth)
        plan.add("authorization_names_temperature", bool(named), tr(
            f"原话里说到的温度：{', '.join(f'{v:g} K' for v in named)}" if named else
            "授权原话里没有说到温度（例如「可以加热到 20 K」）：授权要说清楚到多少开尔文",
            f"the words name {', '.join(f'{v:g} K' for v in named)}" if named else
            "the authorization does not name a temperature (e.g. \"ok to heat to 20 K\"): it must "
            "say how far, in kelvin"))
        cap = _finite(up_to)
        plan.add("authorized_temperature", cap is not None and tgt <= cap + 1e-9, tr(
            f"授权到 {cap:g} K，目标 {tgt:g} K" if cap is not None else
            "升温要在 authorized_up_to_k 里填用户授权到的温度（开尔文）：授权只覆盖到那个值",
            f"authorized up to {cap:g} K, target {tgt:g} K" if cap is not None else
            "raising needs authorized_up_to_k, the temperature (kelvin) the user authorized: the "
            "authorization covers that far and no further"))
        if cap is not None and named:
            plan.add("authorization_consistent", cap <= max(named) + 1e-9, tr(
                f"原话里说的是 {', '.join(f'{v:g} K' for v in named)}，authorized_up_to_k = {cap:g} K",
                f"the words name {', '.join(f'{v:g} K' for v in named)}, authorized_up_to_k = "
                f"{cap:g} K"))

    def _check_guard(self, plan: HeaterPlan, s: HeaterSettings) -> None:
        ok, detail = self._guard_verdict(s)
        plan.add("guard_temperature", ok, detail)

    def _guard_verdict(self, s: HeaterSettings) -> "tuple[bool, str]":
        try:
            r = self._temperature(s.guard_channel)
        except Exception as exc:  # noqa: BLE001
            r, why = None, f"{type(exc).__name__}: {exc}"
        else:
            why = getattr(r, "reason", None) or ""
        val = _finite(getattr(r, "value_k", None))
        age = _finite(getattr(r, "age_s", None))
        ch = s.guard_channel
        if val is None or val < TEMPERATURE_MIN_VALID_K:
            return False, tr(f"读不到守护温度计（{ch}：{why or val}）",
                             f"guard thermometer unavailable ({ch}: {why or val})")
        if age is None or age > GUARD_MAX_AGE_S:
            return False, tr(f"守护温度计读数太旧或年龄不明（{age} s）",
                             f"guard thermometer reading too old or of unknown age ({age} s)")
        ok = val <= float(s.guard_max_k)
        return ok, tr(f"守护温度计 {ch} = {val:.3f} K，上限 {s.guard_max_k:g} K",
                      f"guard thermometer {ch} = {val:.3f} K, limit {s.guard_max_k:g} K")

    def _check_tip(self, plan: HeaterPlan, tip_verdict: "Callable[[], Any] | None") -> None:
        if tip_verdict is None:
            plan.add("tip", False, tr(
                "没法确认针尖已退（这条路径读不到 Nanonis）。退针后再试，或显式声明不要求退针",
                "cannot confirm the tip is withdrawn (no Nanonis on this path). Withdraw it, "
                "or waive the requirement explicitly"))
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
                                         "。默认要求先退针再改变温度（WithdrawTip）；确需带针改变时显式声明"),
            f"tip: {reason or state}" + ("" if state == "parked" else
                                        ". By default the tip is withdrawn before the temperature "
                                        "changes (WithdrawTip); waive it explicitly if the change "
                                        "must be made with the tip in")))

    @staticmethod
    def _changed_since(plan: HeaterPlan, q: dict, again: "dict | None" = None) -> str:
        if q["reading_status"] != 0:
            return tr(f"控制输入读数变成无效（RDGST? = {q['reading_status']}）",
                      f"the control reading became invalid (RDGST? = {q['reading_status']})")
        if q["range"] != plan.present_range:
            return tr(f"加热量程被改了（{plan.present_range} → {q['range']}）",
                      f"the heater range changed ({plan.present_range} -> {q['range']})")
        if abs(q["setpoint_k"] - float(plan.present_setpoint_k)) > SETP_TOL_K:
            return tr(f"设定值被改了（{plan.present_setpoint_k} → {q['setpoint_k']} K）",
                      f"the setpoint changed ({plan.present_setpoint_k} -> {q['setpoint_k']} K)")
        if q["ramping"] != 0:
            return tr("设定值开始扫温了", "the setpoint started ramping")
        if again is not None:
            for key in _CONFIG_KEYS:
                if again.get(key) != plan.instrument.get(key):
                    return tr(f"{key} 变了（{plan.instrument.get(key)} → {again.get(key)}）",
                              f"{key} changed ({plan.instrument.get(key)} -> {again.get(key)})")
            if plan.control_mode == MODE_ZONE and again.get("zones") != plan.instrument.get("zones"):
                return tr("分区表变了", "the zone table changed")
        return ""

    # ── the write ──
    def _send(self, gen: "int | None", verb: str, output: Any = None, *values: Any) -> str:
        """Write one command on the verified handle; ``""`` or what went wrong. Its bytes
        may have left even when this reports an error. ``ValueError`` from
        :func:`build_write` propagates: then nothing was sent."""
        s = self.settings()
        cap = s.max_mout_pct if str(verb).strip().upper() == "ZONE" else 0.0
        payload = build_write(verb, output, *values, max_mout_pct=cap,
                              max_setpoint_k=s.max_setpoint_k)
        self.link.wait_gap()
        try:
            self.link.transport.transact(payload, read_size=0, timeout=0.3,
                                         expect_generation=gen, **self.link.gap_kwargs())
            return ""
        except Exception as exc:  # noqa: BLE001 - reported; the caller decides
            return f"{type(exc).__name__}: {exc}"
        finally:
            self.link.mark_sent()

    def _readback(self, verb: str, arg: str) -> "str | None":
        self._sleep(READBACK_DELAY_S)
        try:
            return self.link.query(verb, arg)
        except (SerialUnavailable, LakeshoreError):
            return None

    def _step(self, gen, out: int, verb: str, *values: Any,
              prev: "int | None" = None) -> "tuple[str, str | None]":
        """Send one step and read it back:
        ``("taken" | "ignored" | "unexpected" | "error" | "refused", detail)``.

        ``ignored``: the instrument answered, with something other than what was sent
        (for RANGE: what it had, *prev*). ``unexpected``: no answer, or a RANGE that is
        neither. ``error``: the write raised; its bytes may have left. ``refused``: the
        byte factory refused the values, nothing was sent.
        """
        try:
            err = self._send(gen, verb, out, *values)
        except ValueError as exc:
            return "refused", str(exc)
        if err:
            return "error", err
        if verb == "RANGE":
            back = self._readback("RANGE?", str(out))
            code = _int(back)
            if code == values[0]:
                return "taken", back
            return ("ignored" if code is not None and code == prev else "unexpected"), back
        if verb == "RAMP":
            back = self._readback("RAMP?", str(out))
            if back is None:
                return "unexpected", back
            f = _fields(back)
            on, rate = (_int(f[0]) if f else None), (_num(f[1]) if len(f) > 1 else None)
            if on == values[0] and rate is not None and abs(rate - round(float(values[1]), 1)) <= RATE_TOL_K_PER_MIN:
                return "taken", back
            return "ignored", back
        # SETP
        back = self._readback("SETP?", str(out))
        sp = _num(back)
        if sp is None:
            return "unexpected", back
        if abs(sp - float(values[0])) <= SETP_TOL_K:
            return "taken", back
        return "ignored", back

    def start(self, target_k: Any, *, origin: str, rate_k_per_min: Any = None,
              heater_range: Any = None, user_authorization: str = "",
              authorized_up_to_k: Any = None, require_tip_withdrawn: bool = True,
              tip_verdict: "Callable[[], Any] | None" = None,
              abort_check: "Callable[[], bool]" = lambda: False) -> dict:
        """Plan; then, holding the link, re-check and run the write sequence (module
        docstring). Returns ``{"started": bool, "plan": {...}, ...}``. Raises nothing for
        instrument conditions."""
        if self._closing:
            return {"started": False, "error": tr("MAST 正在关闭：不开始新的温度变化",
                                                  "MAST is shutting down: no new change")}
        if not self._change_lock.acquire(blocking=False):
            return {"started": False, "error": tr("另一次 MAST 温度变化还没结束",
                                                  "another MAST temperature change has not finished")}
        try:
            if self.active is not None:
                return {"started": False, "error": tr(
                    "上一次 MAST 温度变化还在监督中：等它结束，或先停住 / 关加热器",
                    "the previous MAST temperature change is still supervised: wait for it, or "
                    "hold / turn the heater off first")}
            if self._retry_off:
                return {"started": False, "error": tr(
                    "还有没读到关闭的关加热器请求（MAST 在后台重试）：先让它读到关闭",
                    "a heater-off has not read back yet (MAST is retrying in the background): "
                    "let it read off first")}
            off0, hold0 = self._off_seq, self._hold_seq

            def interrupted() -> bool:
                return bool(abort_check() or self._off_seq != off0 or self._hold_seq != hold0
                            or self._closing)

            plan = self.plan(target_k, rate_k_per_min=rate_k_per_min, heater_range=heater_range,
                             user_authorization=user_authorization,
                             authorized_up_to_k=authorized_up_to_k,
                             require_tip_withdrawn=require_tip_withdrawn, tip_verdict=tip_verdict)
            out: dict = {"started": False, "plan": plan.as_dict()}
            base = {"target_k": plan.target_k, "origin": origin,
                    "authorization": user_authorization, "authorized_up_to_k": authorized_up_to_k,
                    "tip_waived": not require_tip_withdrawn}
            if not plan.ok:
                out["error"] = plan.refusal_text()
                self._ledger({"action": "refused", **base,
                              "refusals": [c.as_dict() for c in plan.refusals]})
                return out
            if plan.noop:
                out["noop"] = True
                return out
            o, inp = int(plan.output), plan.control_input
            rate, rng, tgt = float(plan.rate_k_per_min), int(plan.heater_range), float(plan.target_k)
            zone_mode = plan.control_mode == MODE_ZONE
            s = self.settings()
            entry: dict = {"action": "start", **base, "output": o, "control_input": inp,
                           "rate_k_per_min": rate, "heater_range": rng, "raising": plan.raising,
                           "eta_s": plan.eta_s, "previous_setpoint_k": plan.present_setpoint_k,
                           "previous_range": plan.present_range, "control_mode": plan.control_mode,
                           "zone_path": plan.zone_path, "zone_ranges": plan.zone_ranges,
                           "steps": []}
            turned_on = False                  # this change turned an off heater on
            raised_from: "int | None" = None   # this change changed the range of a heater that was on
            steps: list[tuple] = []
            start_sp = start_k = 0.0
            zones_now: "list | None" = None
            try:
                with self.link.session(timeout=10.0) as gen:
                    q = self._quick_status(o, inp)
                    again = self.read_instrument(o, inp)
                    heater_was_on = q["range"] > 0
                    start_k = float(q["kelvin"])
                    start_sp = float(q["setpoint_k"]) if heater_was_on else round(start_k, 3)
                    changed = self._changed_since(plan, q, again)
                    if not changed and zone_mode:
                        zones_now = again.get("zones")
                        changed = self._zone_path_changed(plan, zones_now, s, start_sp, tgt)
                    if changed:
                        out["error"] = tr(f"预检之后状态变了：{changed}。没有写任何东西，请重新预检",
                                          f"the state changed after the plan: {changed}. Nothing "
                                          "written; plan again")
                        self._ledger({"action": "refused", **base, "changed": changed})
                        return out
                    if interrupted():
                        out["error"] = tr("准备期间收到了中止 / 关加热器 / 停住：不写",
                                          "an abort / heater-off / hold arrived while preparing: "
                                          "nothing written")
                        return out
                    if heater_was_on:
                        steps = [("RAMP", 1, rate)]
                        if not zone_mode and start_sp > start_k + SETP_TOL_K:
                            # The panel's setpoint is above the temperature: the heater is
                            # pushing up, whatever the target. Bring the setpoint down to T now
                            # (ramping off, so at once) before the ramp starts from there.
                            start_sp = round(min(start_k, float(s.max_setpoint_k)), SETPOINT_DECIMALS)
                            steps = [("RAMP", 0, rate), ("SETP", start_sp)] + steps
                        if q["range"] != rng:
                            steps.append(("RANGE", rng))
                        steps.append(("SETP", tgt))
                    else:
                        steps = [("RAMP", 0, rate), ("SETP", start_sp)]
                        if zone_mode:
                            # The setpoint may have crossed into the start zone, and the 335
                            # may have loaded its range: look before going on.
                            steps.append(("ZONE_LOADED",))
                        steps += [("RAMP", 1, rate), ("RANGE", rng), ("SETP", tgt)]
                    entry["basis_k"] = max(tgt, start_sp, start_k)
                    prev_range = q["range"]
                    for i, (verb, *vals) in enumerate(steps):
                        if i > 0 and interrupted():
                            entry["steps"].append({"cmd": verb, "values": vals,
                                                   "result": "interrupted", "readback": None})
                            break
                        skipped = False
                        if i == len(steps) - 1:          # always SETP <target>
                            result, back = self._final_setp(gen, o, tgt, start_sp)
                        elif verb == "ZONE_LOADED":
                            result, back, loaded = self._zone_loaded(o, s)
                            if loaded != 0:              # on, or unknown: it may be on
                                turned_on = True
                            if loaded is not None:
                                prev_range = loaded
                        elif verb == "RANGE" and prev_range == rng:
                            # Zone mode: the 335 already loaded the start zone's range.
                            result, back, skipped = "taken", str(prev_range), True
                        else:
                            result, back = self._step(gen, o, verb, *vals, prev=prev_range)
                        entry["steps"].append({"cmd": verb, "values": vals, "result": result,
                                               "readback": back,
                                               **({"skipped": True} if skipped else {})})
                        if verb == "RANGE" and result not in ("ignored", "refused"):
                            if heater_was_on:
                                raised_from = q["range"]
                            else:
                                turned_on = True         # the heater may be on now
                        if result != "taken":
                            break
            except WrongController as exc:
                out["error"] = tr(f"端口上不是那台 Model 335：{exc}", f"not the Model 335 on the port: {exc}")
                return out
            except (SerialUnavailable, LakeshoreError) as exc:
                if not entry["steps"]:
                    out["error"] = tr(f"温控仪不可用：{exc}", f"controller unavailable: {exc}")
                    return out
                entry["steps"].append({"cmd": "-", "result": "error", "readback": str(exc)})

            last = entry["steps"][-1] if entry["steps"] else {"result": "error", "cmd": "-"}
            if (steps and len(entry["steps"]) == len(steps)
                    and all(st["result"] == "taken" for st in entry["steps"])):
                entry["result"] = "accepted"
                self._ledger(entry)
                with self._state_lock:
                    self._active = {"target_k": tgt, "output": o, "control_input": inp,
                                    "rate_k_per_min": rate, "heater_range": rng,
                                    "start_setpoint_k": start_sp, "start_kelvin": start_k,
                                    "raising": plan.raising, "eta_s": plan.eta_s,
                                    "started_at": time.time(), "origin": origin,
                                    "authorization": user_authorization,
                                    "authorized_up_to_k": authorized_up_to_k,
                                    "off_seq": off0, "hold_seq": hold0,
                                    "control_mode": plan.control_mode,
                                    "zone_path": list(plan.zone_path or []),
                                    "zone_ranges": list(plan.zone_ranges or []),
                                    "zones": zones_now}
                logger.warning("heater control: output %d -> %.3f K at %.1f K/min, range %s, by %s",
                               o, tgt, rate, RANGE_NAMES[rng], origin)
                out["started"] = True
                return out
            what = f"{last.get('cmd')}: {last.get('result')} ({last.get('readback')!r})"
            if (not turned_on and raised_from is None
                    and last["result"] in ("ignored", "refused", "interrupted")):
                # Nothing that matters changed: the heater is as this change found it.
                entry["result"] = "interrupted" if last["result"] == "interrupted" else "not_accepted"
                self._ledger(entry)
                if last["result"] == "interrupted":
                    out.update(interrupted=True, error=tr(
                        "写入途中收到了中止 / 关加热器 / 停住：后面的步骤没有写，加热器保持原样",
                        "an abort / heater-off / hold arrived during the change: the remaining "
                        "steps were not written; the heater is as it was"))
                else:
                    out.update(not_accepted=True, error=tr(
                        f"Model 335 没有接受（{what}）。可能处于本地模式，或设定值超出了传感器曲线的上限；"
                        "MAST 不切换接口模式。加热器保持原样",
                        f"the Model 335 did not take it ({what}). It may be in local mode, or the "
                        "setpoint is above the sensor curve's limit; MAST does not switch the "
                        "interface mode. The heater is as it was"))
                return out
            if raised_from is not None and not turned_on and last["result"] != "error":
                # The heater was on and this change moved its range: put the range back.
                restore = self._restore_range(o, raised_from)
                entry["restore"] = restore
                if restore.get("taken"):
                    entry["result"] = "restored"
                    self._ledger(entry)
                    out.update(restored=True, error=tr(
                        f"没有做完（{what}）：已把加热量程恢复成 {RANGE_NAMES.get(raised_from)}，"
                        "加热器按原来的设定值调节",
                        f"not finished ({what}): the heater range was put back to "
                        f"{RANGE_NAMES.get(raised_from)}; the heater regulates at its previous "
                        "setpoint"))
                    return out
            # A heater this change turned on and could not finish, a write that raised, or
            # a range that could not be put back: turn it off.
            entry["result"] = "failed"
            self._ledger(entry)
            off = self.heater_off(reason=f"start failed at {what}", origin="heater control (start check)",
                                  outputs=(o,))
            out.update(off=off, error=tr(
                f"写入出了问题（{what}）：已关加热器"
                + ("" if off.get("confirmed") else
                   "，但还没读到加热器关闭：MAST 会在后台一直重试，请操作员在 335 面板上按 ALL OFF"),
                f"the change went wrong ({what}): heater turned off"
                + ("" if off.get("confirmed") else
                   ", but the heater does not read off yet: MAST keeps retrying in the "
                   "background; the operator should press ALL OFF on the 335")))
            return out
        finally:
            self._change_lock.release()

    def _zone_path_changed(self, plan: HeaterPlan, zones_raw: Any, s: HeaterSettings,
                           start_sp: float, tgt: float) -> str:
        """Zone mode, holding the link: the way through the zones from the start setpoint
        read now, against the plan's. ``""`` when it is the same way with the same start
        range and rate; else what changed (nothing is written then)."""
        zones = [Zone.from_dict(d) for d in (zones_raw or [])]
        used = used_zones(zones)
        hp = zone_heating_path(zones, used, start_sp, tgt, control_input=s.control_input,
                               max_range=int(s.max_range),
                               max_rate_k_per_min=float(s.max_rate_k_per_min))
        rate = (round(min(float(plan.requested_rate_k_per_min), float(hp["rate_k_per_min"])), 1)
                if hp["rate_k_per_min"] is not None and plan.requested_rate_k_per_min is not None
                else None)
        if (hp["problems"] or hp["path"] != plan.zone_path or hp["range"] != plan.heater_range
                or rate != plan.rate_k_per_min):
            return tr(f"分区路径变了（预检：{plan.zone_path}，量程 {plan.heater_range}；现在从 {start_sp:g} K："
                      f"{hp['path']}，量程 {hp['range']}）" + ("；" + "；".join(hp["problems"]) if hp["problems"] else ""),
                      f"the way through the zones changed (plan: {plan.zone_path}, range "
                      f"{plan.heater_range}; now from {start_sp:g} K: {hp['path']}, range "
                      f"{hp['range']})" + ("; " + "; ".join(hp["problems"]) if hp["problems"] else ""))
        return ""

    def _zone_loaded(self, out: int, s: HeaterSettings) -> "tuple[str, str | None, int | None]":
        """Zone mode, after ``SETP <out>,<T now>``: the range the 335 has now, which it may
        have loaded with the start zone. ``("taken", reply, range)``; ``("unexpected",
        reply, range)`` with no answer or a range above the maximum (the heater may be on,
        so the caller turns it off)."""
        back = self._readback("RANGE?", str(out))
        rr = _int(back)
        if rr is None or rr not in RANGE_CODES or rr > int(s.max_range or 0):
            return "unexpected", back, rr
        return "taken", back, rr

    def _restore_range(self, out: int, code: int) -> dict:
        res: dict = {"range": code, "taken": False, "detail": ""}
        try:
            with self.link.session(timeout=OFF_LOCK_TIMEOUT_S) as gen:
                result, back = self._step(gen, out, "RANGE", code)
            res.update(taken=result == "taken", detail=f"{result} ({back!r})")
        except Exception as exc:  # noqa: BLE001
            res["detail"] = f"{type(exc).__name__}: {exc}"
        return res

    def _final_setp(self, gen, out: int, tgt: float, start_sp: float) -> "tuple[str, str | None]":
        """The last step, ``SETP <target>`` with ramping on. Taken when SETP? reads the
        target, or (SETP? reporting the live ramp) when the setpoint is ramping and reads
        between the start and the target. Ignored when it still reads the start."""
        try:
            err = self._send(gen, "SETP", out, tgt)
        except ValueError as exc:
            return "refused", str(exc)
        if err:
            return "error", err
        back = self._readback("SETP?", str(out))
        sp = _num(back)
        ramping = _int(self._readback("RAMPST?", str(out)))
        lo, hi = sorted((start_sp, tgt))
        if sp is not None and abs(sp - tgt) <= SETP_TOL_K:
            return "taken", back
        if sp is not None and ramping == 1 and lo - SETP_TOL_K <= sp <= hi + SETP_TOL_K:
            return "taken", back
        if sp is not None and abs(sp - start_sp) <= SETP_TOL_K and ramping == 0:
            return "ignored", back
        return "unexpected", back

    # ── follow the change ──
    def supervise(self, *, abort_check: "Callable[[], bool]" = lambda: False,
                  progress: "Callable[[dict], None] | None" = None,
                  poll_s: float = POLL_S, settle_s: float = SETTLE_S,
                  band_k: "float | None" = None) -> dict:
        """Follow the MAST-started change until it settles or must end. Returns how it ended.

        Ends with the heater off: a heater-off for this output that reads back off
        (``heater_off`` or the reason MAST asked for it), or one that did not read off in
        ``OFF_GIVE_UP_S`` (``off_failed``; the retry goes on in the background); the
        instrument or the panel turning it off (``turned_off_elsewhere``). Ends with the
        heater on and watched: ``done``, ``held`` (a hold that held), ``aborted`` (the
        run was cancelled and the ramp held), ``external_change`` (the setpoint, range
        or ramp was changed elsewhere; in zone mode also the control mode, the control
        input or the zone table), ``not_settled``, ``ramp_overdue``, ``timeout``.

        In zone mode a range that is the range of a zone on the way is MAST's own change
        (the 335 loads it as the setpoint crosses into that zone, p. 69). A range above the
        maximum range turns the heater off in either mode.
        """
        act = self.active
        if not act:
            return {"outcome": "no_active_change"}
        tgt = float(act["target_k"])
        o, inp = int(act["output"]), str(act["control_input"])
        rng = int(act["heater_range"])
        zone_mode = act.get("control_mode") == MODE_ZONE
        ok_ranges = set(act.get("zone_ranges") or ()) | {rng} if zone_mode else {rng}
        zones0 = act.get("zones")
        want_in = INPUTS.index(inp) + 1 if inp in INPUTS else None
        polls = 0
        rating_polls = 0
        recheck: dict = {}
        start_sp = float(act["start_setpoint_k"])
        lo, hi = sorted((start_sp, tgt))
        off0, hold0 = int(act["off_seq"]), int(act["hold_seq"])
        band = float(band_k) if band_k and band_k > 0 else settle_band_k(tgt)
        s = self.settings()
        basis = max(tgt, start_sp, float(act.get("start_kelvin", 0.0)))
        ceiling = self._ceiling(basis, s)
        t0 = self._clock()
        ramp_done_at: "float | None" = None
        in_band_since: "float | None" = None
        sat_since: "float | None" = None
        fails = 0
        last_error = ""
        last: dict = {}
        why: "tuple[str, str] | None" = None   # (outcome, detail) of a heater-off we asked for
        off_asked_at: "float | None" = None
        while True:
            now = self._clock()
            q: "dict | None" = None
            try:
                q = self._quick_status(o, inp, outmode=True)
                fails = 0
            except (SerialUnavailable, LakeshoreError) as exc:
                fails += 1
                last_error = str(exc)
            if q is not None:
                last = {"kelvin": q["kelvin"], "setpoint_k": q["setpoint_k"], "range": q["range"],
                        "ramping": q["ramping"], "heater_pct": q["heater_pct"],
                        "reading_status": q["reading_status"], "elapsed_s": round(now - t0, 1)}
                last["outmode"] = q.get("outmode")

            # A heater-off for this output since the change began, from anyone. Done when
            # the heater READS off; until then it is sent again.
            if self._off_req.get(o, 0) > off0:
                if off_asked_at is None:
                    off_asked_at = now
                if (q is not None and q["range"] == 0) or (q is None and self._off_ok.get(o, 0) > off0):
                    outcome, detail = why or ("heater_off", tr("加热器已关（读回 RANGE 0）",
                                                               "heater off (RANGE reads 0)"))
                    return self._end(outcome, last, detail, off=self._last_off)
                if now - off_asked_at > OFF_GIVE_UP_S:
                    self._retry_later(o)
                    return self._end("off_failed", last, tr(
                        f"{OFF_GIVE_UP_S:.0f} s 内一直没读到加热器关闭（{(self._last_off or {}).get('error')}）："
                        "MAST 会在后台一直重试；请操作员立即在 335 面板上按 ALL OFF",
                        f"for {OFF_GIVE_UP_S:.0f} s the heater never read off "
                        f"({(self._last_off or {}).get('error')}): MAST keeps retrying in the "
                        "background; the operator must press ALL OFF on the 335 now"),
                        off=self._last_off)
                if self._offs_in_flight == 0:
                    self.heater_off(reason="the heater does not read off yet; sending again",
                                    origin="heater control (supervisor)", outputs=(o,))
                self._sleep(poll_s)
                continue
            # A hold since the change began: the change ends only if it held.
            if self._hold_seq != hold0:
                lh = self._last_hold or {}
                if lh.get("held") or lh.get("nothing_to_hold"):
                    return self._end("held", last, tr("扫温已停住，加热器在当前温度附近保持",
                                                      "ramp stopped; the heater holds near the "
                                                      "present temperature"), hold=lh)
                hold0 = self._hold_seq
                logger.warning("heater control: a hold did not hold (%s); still supervising",
                               lh.get("error"))
            if abort_check():
                res = self.hold(reason="abort during the change", origin="heater control (abort)")
                if res.get("held") or res.get("nothing_to_hold"):
                    hold0 = self._hold_seq
                    return self._end("aborted", last, tr("运行被中止：扫温停住，加热器保持",
                                                         "run aborted: ramp stopped, heater holds"),
                                     hold=res)
                why = ("aborted", tr("运行被中止，停住失败：已关加热器",
                                     "run aborted and the hold failed: heater turned off"))
                self.heater_off(reason="abort; hold failed", origin="heater control (abort)",
                                outputs=(o,))
                continue
            if q is None:
                if fails >= READ_FAILURES_BEFORE_OFF:
                    why = ("lost_contact", tr(f"连续 {fails} 次读不到温控仪（{last_error}）：已关加热器",
                                              f"{fails} reads in a row failed ({last_error}): heater "
                                              "turned off"))
                    self.heater_off(reason=f"lost contact: {last_error}",
                                    origin="heater control (supervisor)", outputs=(o,))
                    continue
                self._sleep(poll_s)
                continue
            k, sp = float(q["kelvin"]), float(q["setpoint_k"])
            if q["range"] == 0:
                detail = tr(
                    "加热器被关了，不是 MAST 关的：335 自己（温度上限 TLIMIT、传感器错误）、面板 ALL OFF，"
                    "或有人在面板上关了。MAST 不再跟进",
                    "the heater was turned off, not by MAST: the 335 itself (TLIMIT, sensor "
                    "error), ALL OFF, or someone at the panel. MAST stops following")
                if q["ramping"] == 0:
                    return self._end("turned_off_elsewhere", last, detail)
                # Still ramping: in zone mode the next zone would turn it back on (p. 69).
                stop = self._zone_takeover_stop(o, s, origin="heater control (supervisor)")
                if stop["zone_mode"] is False:
                    return self._end("turned_off_elsewhere", last, detail)
                still = stop["stopped"] and stop["range"] == 0
                ended = self._end("turned_off_elsewhere", last, detail + tr(
                    "。分区模式下设定值还在扫，下一个分区会把加热器重新打开：MAST 只发了 RAMP 0 让设定值停住"
                    + ("" if still else "，还没读到停住，MAST 继续看守并重发"),
                    ". The setpoint was still ramping in zone mode, and the next zone would turn "
                    "the heater back on: MAST wrote RAMP 0 only, so the setpoint stays"
                    + ("" if still else "; it does not read stopped yet, so MAST keeps watching "
                       "and sends it again")), ramp_stop=stop)
                if not still:
                    self._start_watch(o, inp, None, why="turned off elsewhere while ramping in zone mode")
                return ended
            ramping = q["ramping"] == 1
            # More power than the initialization allows is a hard limit in either mode, and
            # whoever set it: judged before anything counts as taken over.
            over = self._range_over_max(q, s)
            if over is None:
                over = self._rating_recheck(o, inp, int(q["range"]), s, rating_polls, recheck,
                                            basis)
                rating_polls += 1
            if over is not None:
                why = over
                self.heater_off(reason=f"{over[0]}: {over[1]}", origin="heater control (supervisor)",
                                outputs=(o,))
                continue
            # Someone else took over before the hard limits are judged against MAST's own
            # target: a setpoint raised at the panel moves the temperature within a poll,
            # and turning the heater off for that would fight the operator. The watch keeps
            # the hard limits, against the maximum setpoint.
            if q["range"] not in ok_ranges:
                return self._end("external_change", last, tr(
                    (f"加热量程被别处改成 {q['range']}（途经分区的量程是 {sorted(ok_ranges)}）"
                     if zone_mode else f"加热量程被别处改成 {q['range']}（MAST 设的是 {rng}）")
                    + "：MAST 不再跟进，只看守硬限",
                    (f"the heater range was changed elsewhere to {q['range']} (the zones on the way "
                     f"have {sorted(ok_ranges)})" if zone_mode else
                     f"the heater range was changed elsewhere to {q['range']} (MAST set {rng})")
                    + ": MAST stops following and only watches the hard limits"))
            if zone_mode:
                taken = self._zone_mode_taken_over(q, o, want_in, zones0, polls)
                polls += 1
                if taken:
                    return self._end("external_change", last, taken)
            else:
                om = q.get("outmode") or []
                if len(om) < 2 or om[0] != MODE_CLOSED_LOOP or om[1] != want_in:
                    return self._end("external_change", last, tr(
                        f"控制模式 / 输入被别处改成 {om}（MAST 开始时是闭环 PID、输入 {want_in}）："
                        "MAST 不再跟进，只看守硬限",
                        f"the control mode / input was changed elsewhere to {om} (MAST started in "
                        f"closed-loop PID on input {want_in}): MAST stops following and only "
                        "watches the hard limits"))
            sp_ok = (lo - SETP_TOL_K <= sp <= hi + SETP_TOL_K) if ramping else abs(sp - tgt) <= SETP_TOL_K
            if not sp_ok:
                return self._end("external_change", last, tr(
                    f"设定值被别处改成 {sp:g} K（MAST 设的是 {tgt:g} K）：MAST 不再跟进，只看守硬限",
                    f"the setpoint was changed elsewhere to {sp:g} K (MAST set {tgt:g} K): MAST "
                    "stops following and only watches the hard limits"))
            hard = self._hard_condition(q, ceiling, s)
            if hard is not None:
                why = hard
                self.heater_off(reason=f"{hard[0]}: {hard[1]}", origin="heater control (supervisor)",
                                outputs=(o,))
                continue
            if not ramping:
                ramp_done_at = ramp_done_at if ramp_done_at is not None else now
                saturated = (q["heater_pct"] or 0.0) >= SATURATED_PCT and k < tgt - band
                sat_since = (sat_since if sat_since is not None else now) if saturated else None
                if sat_since is not None and now - sat_since >= SATURATION_S:
                    why = ("saturated", self._saturation_text(k, tgt))
                    self.heater_off(reason="heater saturated without reaching the target",
                                    origin="heater control (supervisor)", outputs=(o,))
                    continue
                if abs(k - tgt) <= band:
                    in_band_since = in_band_since if in_band_since is not None else now
                    if now - in_band_since >= settle_s:
                        return self._end("done", dict(last, band_k=band), tr(
                            f"已稳定在 {k:.3f} K（目标 {tgt:g} K ± {band:g} K，{settle_s:.0f} s）：加热器保持",
                            f"settled at {k:.3f} K (target {tgt:g} K +/- {band:g} K for "
                            f"{settle_s:.0f} s); the heater holds"))
                else:
                    in_band_since = None
                if now - ramp_done_at > SETTLE_TIMEOUT_S:
                    return self._end("not_settled", last, tr(
                        f"扫温结束 {SETTLE_TIMEOUT_S / 60:.0f} 分钟后仍未进入 ±{band:g} K（当前 {k:.3f} K）："
                        "加热器保持调节，MAST 继续看守。可能量程不够或 PID 需要调，请操作员查看",
                        f"{SETTLE_TIMEOUT_S / 60:.0f} min after the ramp ended the temperature "
                        f"({k:.3f} K) is still outside +/- {band:g} K; the heater keeps "
                        "regulating, watched. The range may be too low or the PID may need tuning"))
            else:
                ramp_done_at = None
                in_band_since = None
                sat_since = None
                eta = act.get("eta_s")
                if eta is not None and now - t0 > 1.5 * float(eta) + RAMP_GRACE_S:
                    return self._end("ramp_overdue", last, tr(
                        f"扫温比预计（{float(eta) / 60:.0f} 分钟）晚太久还没结束（RAMPST? 仍为 1）：MAST 不再跟进，"
                        "加热器保持调节，MAST 继续看守。请操作员查看",
                        f"the ramp is long overdue (expected {float(eta) / 60:.0f} min; RAMPST? "
                        "still 1): MAST stops following; the heater keeps regulating, watched. "
                        "The operator should look"))
            if now - t0 > MAX_SUPERVISE_S:
                return self._end("timeout", last, tr("超过监督时限：MAST 不再跟进，加热器保持调节，MAST 继续看守",
                                                     "supervision time cap reached: MAST stops "
                                                     "following; the heater keeps regulating, "
                                                     "watched"))
            if progress is not None:
                try:
                    progress(dict(last, target_k=tgt))
                except Exception:  # noqa: BLE001 - a progress sink never stops supervision
                    pass
            self._sleep(poll_s)

    def _ceiling(self, basis_k: "float | None", s: HeaterSettings) -> float:
        """Above this the heater goes off: the basis (highest of target, start setpoint and
        start temperature) plus the overshoot allowance, never above the maximum setpoint,
        the stage's absolute maximum. Without a basis: the maximum setpoint."""
        cap = float(s.max_setpoint_k) if s.max_setpoint_k is not None else math.inf
        if basis_k is None:
            return cap if math.isfinite(cap) else MAX_COMMAND_K
        return min(float(basis_k) + overshoot_allowance_k(basis_k), cap)

    @staticmethod
    def _range_over_max(q: dict, s: HeaterSettings) -> "tuple[str, str] | None":
        """A heater range above the maximum range set at initialization: a hard limit."""
        mr = s.max_range
        rng = int(q.get("range") or 0)
        if mr in (1, 2, 3) and rng > mr:
            return ("range_over_max", tr(
                f"加热量程 {RANGE_NAMES.get(rng, rng)} 超过初始化允许的最大量程 {RANGE_NAMES[mr]}：已关加热器",
                f"heater range {RANGE_NAMES.get(rng, rng)} is above the maximum range set at "
                f"initialization ({RANGE_NAMES[mr]}): heater turned off"))
        return None

    def _zone_mode_taken_over(self, q: dict, out: int, want_in: "int | None",
                              zones0: Any, polls: int) -> str:
        """Zone mode, supervising: ``""``, or how the control was taken over: the output
        left zone mode, its control input changed, or (read every ``ZONE_RECHECK_POLLS``
        polls) the zone table changed."""
        om = q.get("outmode") or []
        if len(om) < 2 or om[0] != MODE_ZONE or om[1] != want_in:
            return tr(f"控制模式 / 输入被别处改成 {om}（MAST 开始时是分区模式、输入 {want_in}）：MAST 不再跟进，只看守硬限",
                      f"the control mode / input was changed elsewhere to {om} (MAST started in "
                      f"zone mode on input {want_in}): MAST stops following and only watches "
                      "the hard limits")
        if zones0 is not None and polls % ZONE_RECHECK_POLLS == ZONE_RECHECK_POLLS - 1:
            try:
                zones = self._read_zones(out)
            except (SerialUnavailable, LakeshoreError):
                return ""                        # not read this time; read again next time
            if None not in zones and zones != zones0:
                return tr("分区表被别处改了：MAST 不再跟进，只看守硬限",
                          "the zone table was changed elsewhere: MAST stops following and only "
                          "watches the hard limits")
        return ""

    def _hard_condition(self, q: dict, ceiling: float,
                        s: HeaterSettings) -> "tuple[str, str] | None":
        """The conditions that turn the heater off wherever MAST's heating is on."""
        k = float(q["kelvin"])
        if q["reading_status"] != 0 or k < TEMPERATURE_MIN_VALID_K:
            return ("sensor_fault", tr(f"控制输入读数无效（RDGST? = {q['reading_status']}，{k} K）：已关加热器",
                                       f"the control reading is not valid (RDGST? = "
                                       f"{q['reading_status']}, {k} K): heater turned off"))
        if k > ceiling:
            return ("overtemperature", tr(f"温度 {k:.3f} K 超过上限 {ceiling:.3f} K：已关加热器",
                                          f"temperature {k:.3f} K is above the ceiling "
                                          f"{ceiling:.3f} K: heater turned off"))
        over = self._range_over_max(q, s)
        if over is not None:
            return over
        if s.guard_configured:
            gok, gdetail = self._guard_verdict(s)
            if not gok:
                return ("guard", tr(f"守护温度计不满足：{gdetail}。已关加热器",
                                    f"guard thermometer: {gdetail}. Heater turned off"))
        return None

    @staticmethod
    def _saturation_text(k: float, sp: float) -> str:
        return tr(f"加热器满功率 {SATURATION_S / 60:.0f} 分钟仍到不了设定值（{k:.3f} K < {sp:g} K）："
                  "控制温度计可能没贴住样品台，或量程太小。已关加热器",
                  f"the heater ran at full power for {SATURATION_S / 60:.0f} min without "
                  f"reaching the setpoint ({k:.3f} K < {sp:g} K): the control thermometer may "
                  "not see the heater, or the range is too low. Heater turned off")

    def _end(self, outcome: str, last: dict, detail: str, **extra) -> dict:
        with self._state_lock:
            act = self._active
            self._active = None
            self._last = {"outcome": outcome, "detail": detail, "ended_at": time.time(),
                          **({"target_k": act.get("target_k"), "output": act.get("output")}
                             if act else {}), **last}
        out = {"outcome": outcome, "detail": detail, "last": last, **extra}
        row = {"action": "change_end", "outcome": outcome, "detail": detail, **last}
        if act:
            row.update(output=act.get("output"), control_input=act.get("control_input"),
                       target_k=act.get("target_k"))
        self._ledger(row)
        level = (logging.INFO if outcome in ("done", "held") else
                 logging.CRITICAL if outcome == "off_failed" else logging.WARNING)
        logger.log(level, "heater control: change ended (%s): %s", outcome, detail)
        # A status never read ("range" missing) is not "off": the heater may be on, so it is
        # watched too (the basis is then MAST's own start).
        if act and outcome in _STAYS_ON and ("range" not in last or int(last.get("range") or 0) > 0):
            known = [float(act.get("target_k") or 0.0), float(last.get("setpoint_k") or 0.0),
                     float(last.get("kelvin") or 0.0)]
            if "range" not in last:
                known += [float(act.get("start_setpoint_k") or 0.0),
                          float(act.get("start_kelvin") or 0.0)]
            basis = None if outcome == "external_change" else max(known)
            self._start_watch(int(act["output"]), str(act["control_input"]), basis,
                              why=outcome)
        return out

    # ── the watch over MAST's heating, and the off retry ──
    def _start_watch(self, output: int, control_input: str, basis_k: "float | None", *,
                     why: str) -> None:
        w = {"output": int(output), "control_input": str(control_input), "basis_k": basis_k,
             "since": time.time(), "why": why, "fails_since": None, "sat_since": None}
        with self._state_lock:
            self._watch = w
        self._ledger({"action": "watch_start", **{k: v for k, v in w.items()
                                                   if k not in ("fails_since", "sat_since")}})
        self._ensure_background()

    def _stop_watch(self, outcome: str, detail: str) -> None:
        with self._state_lock:
            w, self._watch = self._watch, None
        if w is not None:
            self._ledger({"action": "watch_end", "outcome": outcome, "detail": detail,
                          "output": w.get("output")})
            logger.log(logging.INFO if outcome == "heater_off" else logging.WARNING,
                       "heater control: watch ended (%s): %s", outcome, detail)

    def _retry_later(self, output: int) -> None:
        with self._state_lock:
            self._retry_off.setdefault(int(output), self._clock())
        self._ensure_background()

    def _ensure_background(self) -> None:
        if not self._background:
            return
        with self._bg_lock:
            if self._bg_thread is not None and self._bg_thread.is_alive():
                return
            self._bg_thread = threading.Thread(target=self._bg_run, name="heater-watch",
                                               daemon=True)
            self._bg_thread.start()

    def _bg_run(self) -> None:
        while True:
            try:
                more = self.background_step()
            except Exception as exc:  # noqa: BLE001 - the watch must not die on one error
                logger.warning("heater control: background step failed: %s", exc)
                more = True
            if not more:
                return
            if self._retry_off:
                oldest = min(self._retry_off.values())
                time.sleep(OFF_RETRY_S if self._clock() - oldest < OFF_GIVE_UP_S
                           else OFF_RETRY_SLOW_S)
            else:
                time.sleep(WATCH_POLL_S)

    def background_step(self) -> bool:
        """One pass of the background duties; returns whether there is more to do.

        1. Heater-offs that did not read back off are sent again (bare terminator first)
           until RANGE? reads 0, loudly.
        2. The watch: MAST-started heating left on after supervision is checked for the
           hard conditions (sensor fault, overtemperature, range above the maximum, guard,
           saturation, contact lost for ``WATCH_LOST_S``) and turned off on any; nothing
           else is written, except ``RAMP <out>,0`` when the heater reads off while the
           setpoint still ramps in zone mode (the module docstring's one exception). It
           pauses while a new MAST change is supervised and ends when the heater reads
           off and the setpoint does not ramp, and when MAST shuts down (the 335 keeps
           regulating; its TLIMIT stays).
        """
        retry = tuple(sorted(self._retry_off))
        if retry:
            res = self.heater_off(reason="the heater has not read off; sending again",
                                  origin="heater control (retry)", outputs=retry)
            if not res.get("confirmed"):
                now = self._clock()
                if now - self._last_loud > 60.0:
                    self._last_loud = now
                    logger.critical("heater control: output(s) %s still do not read off (%s) — "
                                    "the operator should press ALL OFF on the 335", list(retry),
                                    res.get("error") or res.get("readback"))
            return True
        w = self._watch
        if w is None or self._closing:
            return False
        if self.busy:
            return True                      # a new MAST change: supervision owns the output
        now = self._clock()
        o, inp = int(w["output"]), str(w["control_input"])
        try:
            q = self._quick_status(o, inp)
            w["fails_since"] = None
        except (SerialUnavailable, LakeshoreError) as exc:
            w["fails_since"] = w["fails_since"] if w.get("fails_since") is not None else now
            if now - w["fails_since"] > WATCH_LOST_S:
                self._watch_off(w, "lost_contact", tr(
                    f"{WATCH_LOST_S:.0f} s 读不到温控仪（{exc}）", f"controller unreadable for "
                    f"{WATCH_LOST_S:.0f} s ({exc})"))
            return True
        s = self.settings()
        if q["range"] == 0:
            if q["ramping"] != 0:
                # Off, but the setpoint still ramps: in zone mode the next zone would turn
                # the heater back on (p. 69). Stop the ramp, and watch until it reads stopped.
                stop = self._zone_takeover_stop(o, s, origin="heater control (watch)")
                if stop["zone_mode"] is not False and not (stop["stopped"] and stop["range"] == 0):
                    return True
            self._stop_watch("heater_off", tr("加热器读到已关", "the heater reads off"))
            return bool(self._retry_off)
        hard = self._hard_condition(q, self._ceiling(w.get("basis_k"), s), s)
        if hard is None:
            hard = self._rating_recheck(o, inp, int(q["range"]), s, int(w.get("rating_polls") or 0),
                                        w, w.get("basis_k"))
            w["rating_polls"] = int(w.get("rating_polls") or 0) + 1
        if hard is not None:
            self._watch_off(w, *hard)
            return True
        saturated = ((q["heater_pct"] or 0.0) >= SATURATED_PCT
                     and float(q["kelvin"]) < float(q["setpoint_k"]) - settle_band_k(q["setpoint_k"]))
        if saturated:
            w["sat_since"] = w["sat_since"] if w.get("sat_since") is not None else now
            if now - w["sat_since"] >= SATURATION_S:
                self._watch_off(w, "saturated", self._saturation_text(q["kelvin"], q["setpoint_k"]))
                return True
        else:
            w["sat_since"] = None
        return True

    def _watch_off(self, w: dict, outcome: str, detail: str) -> None:
        res = self.heater_off(reason=f"watch: {outcome}: {detail}", origin="heater control (watch)",
                              outputs=(int(w["output"]),))
        self._stop_watch(outcome, detail + ("" if res.get("confirmed") else
                                            tr("（还没读到关闭，后台重试）",
                                               " (does not read off yet; retrying)")))

    def _resume_from_ledger(self) -> "dict | None":
        """MAST heating left on by an earlier MAST process on this port, per the ledger:
        the latest of start / change_end / watch_start / watch_end / heater_off decides."""
        try:
            path = self._ledger_path()
            if not path.exists():
                return None
            lines = path.read_text(encoding="utf-8").splitlines()[-500:]
        except Exception:  # noqa: BLE001 - no ledger: nothing to resume
            return None
        port = self.link.port
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("port") != port:
                continue
            a = row.get("action")
            if a == "heater_off" and row.get("confirmed"):
                return None
            if a == "watch_end":
                return None
            if a == "watch_start" and row.get("output") in OUTPUTS:
                return {"output": int(row["output"]), "control_input": str(row.get("control_input") or ""),
                        "basis_k": row.get("basis_k"), "since": time.time(), "why": "resumed",
                        "fails_since": None, "sat_since": None}
            if a == "change_end":
                if row.get("outcome") not in _STAYS_ON or not int(row.get("range") or 0):
                    return None
                if row.get("output") in OUTPUTS:
                    return {"output": int(row["output"]),
                            "control_input": str(row.get("control_input") or ""),
                            "basis_k": None if row.get("outcome") == "external_change" else
                            max(float(row.get("target_k") or 0), float(row.get("setpoint_k") or 0),
                                float(row.get("kelvin") or 0)),
                            "since": time.time(), "why": "resumed", "fails_since": None,
                            "sat_since": None}
                return None
            if a == "start" and row.get("result") == "accepted" and row.get("output") in OUTPUTS:
                return {"output": int(row["output"]), "control_input": str(row.get("control_input") or ""),
                        "basis_k": row.get("basis_k"), "since": time.time(), "why": "resumed",
                        "fails_since": None, "sat_since": None}
        return None

    # ── the magnet's interlock: heating and field changes never at the same time ──
    def interlock_state(self) -> dict:
        """For the field-control gate (``instruments/magnet_625``): ``RANGE?`` on both
        outputs (queries only, each holding the link briefly) and whether MAST's own heating
        is on. ``ok`` only when both outputs read 0 and MAST is not heating; an output that
        does not answer is not "off". An output with RANGE 0 is not "off" either when it is
        in zone mode and its setpoint still ramps: the next zone the setpoint crosses may
        turn the heater on by itself (§5.3 p. 69). Its mode (OUTMODE?) and, in zone mode, its
        ramp state (RAMPST?) must answer: an unknown state is not "off". Never raises."""
        ranges: "dict[int, int | None]" = {}
        errors: list[str] = []
        not_off: "list[tuple[int, str, str]]" = []   # (output, zh, en) beyond RANGE itself
        for o in OUTPUTS:
            try:
                ranges[o] = _int(self.link.query("RANGE?", str(o)))
            except Exception as exc:  # noqa: BLE001 - unknown, and said so
                ranges[o] = None
                errors.append(f"{type(exc).__name__}: {exc}")
            if ranges[o] != 0:
                continue
            try:
                om = _ints(self.link.query("OUTMODE?", str(o)))
            except Exception:  # noqa: BLE001 - unknown, and said so below
                om = []
            mode = om[0] if om else None
            if mode is None:
                not_off.append((o, f"输出 {o}：OUTMODE? 没读到，不知道是不是分区模式",
                                f"output {o}: OUTMODE? not answered, so zone mode cannot be ruled out"))
                continue
            if mode != MODE_ZONE:
                continue
            try:
                ramping = _int(self.link.query("RAMPST?", str(o)))
            except Exception:  # noqa: BLE001 - unknown, and said so below
                ramping = None
            if ramping is None:
                not_off.append((o, f"输出 {o}：分区模式，RAMPST? 没读到，不知道设定值是否还在扫",
                                f"output {o}: zone mode, RAMPST? not answered, so the setpoint may "
                                "still be ramping"))
            elif ramping != 0:
                not_off.append((o, f"输出 {o}：分区模式，设定值还在扫，下一个分区可能自己把加热器打开"
                                "（用 HoldSampleTemperature 停住扫温）",
                                f"output {o}: zone mode, setpoint still ramping; the next zone may "
                                "turn the heater on by itself (stop the ramp with "
                                "HoldSampleTemperature)"))
        try:
            mast = bool(self.mast_heating())
        except Exception:  # noqa: BLE001 - cannot tell: count it as heating
            mast = True
        unknown = [o for o, r in ranges.items() if r is None]
        on = [o for o, r in ranges.items() if r]
        ok = not unknown and not on and not not_off and not mast
        if ok:
            detail = tr("Lake Shore 335 两路加热输出都关着（RANGE 0），MAST 也没有在加热",
                        "both Lake Shore 335 heater outputs read off (RANGE 0) and MAST is not heating")
        else:
            parts_zh, parts_en = [], []
            if on:
                parts_zh.append("加热输出 " + "、".join(f"{o}（量程 {ranges[o]}）" for o in on) + " 开着")
                parts_en.append("heater output(s) " + ", ".join(f"{o} (range {ranges[o]})" for o in on)
                                + " on")
            if unknown:
                parts_zh.append(f"输出 {unknown} 的 RANGE? 没读到（{'; '.join(errors)}）")
                parts_en.append(f"RANGE? of output(s) {unknown} not answered ({'; '.join(errors)})")
            for _o, zh, en in not_off:
                parts_zh.append(zh)
                parts_en.append(en)
            if mast:
                parts_zh.append("MAST 自己的加热还在（变温、看守、关加热器重试或改分区表 / 控制模式）")
                parts_en.append("MAST's own heating is on (a change, the watch, an off retry, or a zone "
                                "table / control mode write)")
            detail = tr("加热和改磁场不同时进行：" + "；".join(parts_zh) + "。先用 HeaterOff 关加热器",
                        "heating and field changes never overlap: " + "; ".join(parts_en)
                        + ". Turn the heater off first (HeaterOff)")
        return {"ok": ok, "present": True, "ranges": ranges,
                "not_off": [o for o, _zh, _en in not_off], "mast_heating": mast, "detail": detail}

    # ── configuration: the zone table and the control mode ──
    def _config_refusal(self, s: HeaterSettings) -> str:
        """Refusals common to both configuration writes that need no I/O. Called holding
        ``_change_lock``, so MAST's own heating is asked about piece by piece."""
        if not s.control_configured:
            return tr("初始化里还没有设定控温：MAST 不改这台 335 的分区表或控制模式",
                      "temperature control is not set up at initialization: MAST changes "
                      "neither the zone table nor the control mode of this 335")
        if self.active is not None or self.watching or self.retrying:
            return tr("MAST 自己的加热还在（变温、看守或关加热器重试）：先 HeaterOff，等它读到关闭",
                      "MAST's own heating is on (a change, the watch or an off retry): turn it "
                      "off first (HeaterOff) and let it read off")
        field_ok, field_detail = self._field_verdict()
        if not field_ok:
            return field_detail
        return ""

    def _field_verdict(self) -> "tuple[bool, str]":
        """Is the field at rest? Heating and field changes never happen at the same time.
        Not ok when MAST is changing it, when the Model 625 itself is ramping, has an error
        bit, cannot be read, is more than one, or cannot be told apart from "none" (unknown
        is not at rest). No 625 on this machine is at rest. Called with no link held."""
        try:
            busy = bool(self._field_busy())
        except Exception as exc:  # noqa: BLE001 - not known is not "not busy"
            return False, tr(f"问不到 MAST 调场的状态（{type(exc).__name__}）：不加热",
                             f"cannot ask whether MAST is changing the field ({type(exc).__name__}): "
                             "no heating")
        if busy:
            return False, tr("MAST 正在改变磁场：一次只做一项低温环境变化，等调场结束",
                             "MAST is changing the magnet's field: one cryostat change at a time; "
                             "wait for it")
        try:
            st = self._field_state()
        except Exception as exc:  # noqa: BLE001
            return False, tr(f"读磁体电源的状态失败（{type(exc).__name__}: {exc}）：不加热",
                             f"reading the magnet supply's state failed ({type(exc).__name__}: "
                             f"{exc}): no heating")
        if not isinstance(st, dict) or st.get("ok") is not True:
            why = str(st.get("detail") or "") if isinstance(st, dict) else ""
            return False, why or tr("磁体电源的状态不明：不加热",
                                    "the magnet supply's state is unknown: no heating")
        return True, tr("磁场没有在改变", "the field is not changing")

    def _config_state(self, out: int, *, zones: bool) -> dict:
        """What a configuration write looks at, read now; a field not answered is
        ``None``. Raises :class:`WrongController` / :class:`SerialUnavailable`."""
        q = self.link.query
        o = str(int(out))
        r: dict = {}

        def _ask(key: str, verb: str, arg: "str | None", parse: Callable[[str], Any]) -> None:
            try:
                r[key] = parse(q(verb, arg))
            except (LakeshoreError, ValueError) as exc:
                if isinstance(exc, WrongController):
                    raise
                r[key] = None

        _ask("emulation", "EMUL?", None, _ints)
        _ask("heater_setup", "HTRSET?", o, _fields)
        _ask("range", "RANGE?", o, _int)
        _ask("ramping", "RAMPST?", o, _int)
        _ask("outmode", "OUTMODE?", o, _ints)
        if zones:
            r["zones"] = self._read_zones(int(out))
        return r

    @staticmethod
    def _config_state_refusal(st: dict, out: int, *, zones: bool) -> str:
        """Refusals from the state read: unknown, emulation, not a current-mode output,
        the heater on, the setpoint ramping, a zone not read."""
        unread = [k for k in ("emulation", "heater_setup", "range", "ramping", "outmode")
                  if st.get(k) is None]
        om = st.get("outmode") or []
        if "outmode" not in unread and (len(om) < 3 or None in om[:3]):
            unread.append("outmode")
        if unread:
            return tr(f"这些状态没读到：{', '.join(unread)}（读不到不是答案）",
                      f"not answered: {', '.join(unread)} (unknown is not an answer)")
        emul = st["emulation"]
        if not emul or emul[0] != 0:
            return tr(f"EMUL? = {emul}：仪器在仿真 331/332，ZONE 等命令含义不同（§5.11.3），MAST 不改它的配置",
                      f"EMUL? = {emul}: the controller emulates a 331/332, where ZONE and other "
                      "commands mean other things (§5.11.3); MAST does not configure it")
        htr_type = _int(st["heater_setup"][0]) if st["heater_setup"] else None
        if not (int(out) == 1 or htr_type == 0):
            return tr(f"输出 {out} 配置成电压输出（HTRSET? 类型 {htr_type}）：不是加热输出",
                      f"output {out} is configured as a voltage output (HTRSET? type {htr_type}): "
                      "not a heater output")
        if st["range"] != 0:
            return tr(f"输出 {out} 的加热器开着（RANGE? = {st['range']}）：只在加热器关着时改分区表或控制模式。"
                      "先 HeaterOff",
                      f"output {out}'s heater is on (RANGE? = {st['range']}): the zone table and "
                      "the control mode change only with the heater off. Turn it off first "
                      "(HeaterOff)")
        if st["ramping"] != 0:
            return tr("设定值正在扫温：分区模式下扫过分区边界会装入那一区的量程。先 HoldSampleTemperature",
                      "the setpoint is ramping: in zone mode a ramp crossing a zone boundary loads "
                      "that zone's range. Stop it first (HoldSampleTemperature)")
        if zones:
            missing = [n for n, z in enumerate(st.get("zones") or [], 1) if z is None]
            if missing or len(st.get("zones") or []) != len(ZONE_NUMBERS):
                return tr(f"这些分区没读到：{missing}（读不到不是答案：没有旧表就没法恢复）",
                          f"zones not read: {missing} (unknown is not an answer: without the old "
                          "table nothing could be restored)")
        return ""

    def _write_zone(self, gen, out: int, n: int, z: Zone) -> dict:
        """Send one ``ZONE`` and read back ``ZONE?`` and ``RANGE?``. The readback is
        asked even when the send raised: its bytes may have left. ``refused``: the byte
        factory refused the values (an old zone with a manual output, say); nothing sent."""
        try:
            err = self._send(gen, "ZONE", out, n, *z.values())
        except ValueError as exc:
            return {"error": f"refused: {exc}", "refused": True, "readback": None, "zone": None,
                    "range": None, "ok": False}
        back = self._readback("ZONE?", f"{int(out)},{n}")
        got = Zone.parse(back) if back is not None else None
        try:
            rr = _int(self.link.query("RANGE?", str(out)))
        except (SerialUnavailable, LakeshoreError):
            rr = None
        return {"error": err, "readback": back, "zone": got, "range": rr,
                "ok": not err and z.same_as(got) and rr == 0}

    @staticmethod
    def _zone_state(got: "Zone | None", new: Zone, old: Zone) -> str:
        if new.same_as(got):
            return "new"
        if old.same_as(got):
            return "old"
        return "unknown"

    def write_zone_table(self, table: Any, *, origin: str,
                         abort_check: "Callable[[], bool]" = lambda: False) -> dict:
        """Write the configured output's zone table (``ZONE``), each zone read back.

        *table*: what :func:`zone_table_from` takes (zones after the last one given are
        written as unused). Only where temperature control is set up, with the output's
        heater reading off, the setpoint not ramping, no emulation, MAST not heating and
        not changing the field. The new table must pass :func:`validate_zone_table` and
        :func:`build_write` before anything is sent; zones that already hold the new values
        are not written. The old table is read first and goes into the ledger.

        After each zone ``ZONE?`` must read back what was sent and ``RANGE?`` must still
        read 0 (the manual does not say whether writing a zone applies it at once). On any
        failure, an abort, a heater-off request or a shutdown: the heater is turned off if
        it does not read 0, the zones written so far (the failed one included) get their
        old values back, and ``zones`` says zone by zone what the instrument holds: ``new``,
        ``old`` or ``unknown``. Never raises.
        """
        res: dict = {"written": False, "noop": False, "error": "", "output": None,
                     "old": None, "new": None, "verdict": None, "zones": [], "off": None,
                     "restored": None, "origin": str(origin)[:200]}
        try:
            new = zone_table_from(table, max_mout_pct=self.settings().max_mout_pct)
        except HeaterControlError as exc:
            res["error"] = str(exc)
            return res
        res["new"] = [z.as_dict() for z in new]
        if self._closing:
            res["error"] = tr("MAST 正在关闭：不改分区表", "MAST is shutting down: no zone table change")
            return res
        if not self._change_lock.acquire(blocking=False):
            res["error"] = tr("另一次 MAST 温度变化或配置写入还没结束",
                              "another MAST temperature change or configuration write has not "
                              "finished")
            return res
        try:
            return self._write_zone_table_locked(new, res, origin=origin, abort_check=abort_check)
        except Exception as exc:  # noqa: BLE001 - never raises; what happened is in the ledger
            logger.exception("heater control: zone table write failed")
            res["error"] = res["error"] or f"{type(exc).__name__}: {exc}"
            return res
        finally:
            self._change_lock.release()

    def _write_zone_table_locked(self, new: list[Zone], res: dict, *, origin: str,
                                 abort_check: "Callable[[], bool]") -> dict:
        s = self.settings()
        why = self._config_refusal(s)
        if why:
            res["error"] = why
            self._ledger({"action": "zone_table", "result": "refused", "error": why,
                          "origin": origin, "new": res["new"]})
            return res
        o = int(s.output)
        res["output"] = o
        verdict = validate_zone_table(new, control_input=s.control_input, max_range=int(s.max_range),
                                      max_rate_k_per_min=float(s.max_rate_k_per_min),
                                      max_setpoint_k=float(s.max_setpoint_k),
                                      max_mout_pct=float(s.max_mout_pct))
        res["verdict"] = verdict.as_dict()
        try:
            for n in ZONE_NUMBERS:              # every byte is buildable before one is sent
                build_write("ZONE", o, n, *new[n - 1].values(), max_mout_pct=float(s.max_mout_pct),
                            max_setpoint_k=s.max_setpoint_k)
        except ValueError as exc:
            verdict.problems.append(str(exc))
            verdict.ok = False
        if not verdict.ok:
            res["error"] = (tr("新分区表不能用，没有写任何东西：", "the new zone table cannot be used; "
                                                       "nothing written: ")
                            + "; ".join(verdict.problems))
            self._ledger({"action": "zone_table", "result": "refused", "error": res["error"],
                          "origin": origin, "output": o, "new": res["new"]})
            return res
        try:
            st = self._config_state(o, zones=True)
        except WrongController as exc:
            res["error"] = tr(f"端口上不是那台 Model 335：{exc}", f"not the Model 335 on the port: {exc}")
            return res
        except (SerialUnavailable, LakeshoreError) as exc:
            res["error"] = tr(f"读不到温控仪：{exc}", f"cannot read the controller: {exc}")
            return res
        why = self._config_state_refusal(st, o, zones=True)
        if why:
            res["error"] = why
            self._ledger({"action": "zone_table", "result": "refused", "error": why,
                          "origin": origin, "output": o, "new": res["new"]})
            return res
        res["old"] = st["zones"]
        old = [Zone.from_dict(d) for d in st["zones"]]
        todo = [n for n in ZONE_NUMBERS if not new[n - 1].same_as(old[n - 1])]
        states = {n: "old" for n in ZONE_NUMBERS}
        if not todo:
            res.update(noop=True, written=True, zones=[{"zone": n, "state": "new"} for n in ZONE_NUMBERS])
            return res
        self._ledger({"action": "zone_table", "phase": "begin", "origin": origin, "output": o,
                      "old": st["zones"], "new": res["new"], "writing": todo})
        off0 = self._off_seq
        readbacks: dict = {}
        failure = ""
        came_on = False
        attempted: list[int] = []
        try:
            with self.link.session(timeout=10.0) as gen:
                again = self._config_state(o, zones=True)
                if (self._config_state_refusal(again, o, zones=True)
                        or again.get("zones") != st["zones"] or again.get("outmode") != st["outmode"]):
                    res["error"] = tr("读完旧表之后状态变了：没有写任何东西，请再试",
                                      "the state changed after the old table was read: nothing "
                                      "written; try again")
                    self._ledger({"action": "zone_table", "result": "refused", "error": res["error"],
                                  "origin": origin, "output": o})
                    return res
                for n in todo:
                    if abort_check() or self._off_seq != off0 or self._closing:
                        failure = tr("写入途中收到了中止 / 关加热器 / 关闭",
                                     "an abort / heater-off / shutdown arrived during the write")
                        break
                    attempted.append(n)
                    w = self._write_zone(gen, o, n, new[n - 1])
                    readbacks[n] = w["readback"]
                    states[n] = self._zone_state(w["zone"], new[n - 1], old[n - 1])
                    if w["range"] != 0:
                        came_on = True
                    if not w["ok"]:
                        bits = [w["error"]] if w["error"] else []
                        if not new[n - 1].same_as(w["zone"]):
                            bits.append(tr(f"读回 {w['readback']!r}", f"read back {w['readback']!r}"))
                        if w["range"] != 0:
                            bits.append(tr(f"写完后 RANGE? = {w['range']}", f"RANGE? after it = {w['range']}"))
                        failure = tr(f"第 {n} 区：", f"zone {n}: ") + "; ".join(bits)
                        break
                if failure:
                    if came_on:
                        res["off"] = self.heater_off(reason=f"zone table write: {failure}",
                                                     origin="heater control (zone table)", outputs=(o,))
                    res["restored"] = True
                    for n in attempted:
                        if came_on and not (res["off"] or {}).get("confirmed"):
                            res["restored"] = False   # not while the heater may be on
                            break
                        w = self._write_zone(gen, o, n, old[n - 1])
                        if w.get("refused"):          # MAST cannot write that old zone back
                            res["restored"] = False
                            res.setdefault("not_restorable", []).append(n)
                            continue
                        readbacks[n] = w["readback"]
                        states[n] = self._zone_state(w["zone"], new[n - 1], old[n - 1])
                        if states[n] != "old":
                            res["restored"] = False
                        if w["range"] != 0:
                            came_on = True
                            res["restored"] = False
                            res["off"] = self.heater_off(reason="zone table restore: RANGE? not 0",
                                                         origin="heater control (zone table)",
                                                         outputs=(o,))
                            break
        except WrongController as exc:
            failure = failure or tr(f"端口上不是那台 Model 335：{exc}", f"not the Model 335 on the port: {exc}")
        except (SerialUnavailable, LakeshoreError) as exc:
            failure = failure or tr(f"温控仪不可用：{exc}", f"controller unavailable: {exc}")
        res["zones"] = [{"zone": n, "state": states[n], "readback": readbacks.get(n)}
                        for n in ZONE_NUMBERS]
        if not failure:
            res["written"] = True
            self._ledger({"action": "zone_table", "result": "written", "origin": origin,
                          "output": o, "zones": res["zones"]})
            logger.warning("heater control: zone table of output %d written (zones %s) by %s",
                           o, todo, origin)
            return res
        changed = [f"{z['zone']}: {z['state']}" for z in res["zones"] if z["state"] != "old"]
        where = (tr("旧表已恢复。", "The old table is back. ") if res["restored"] else
                 tr("仪器里不是旧值的分区：", "Zones not holding their old values: ")
                 + (", ".join(changed) or "-") + tr("（其余是旧值）。", " (the rest hold the old). "))
        if res.get("not_restorable"):
            where += tr(f"第 {res['not_restorable']} 区的旧值 MAST 写不回去（字节工厂拒绝，例如手动输出不是 0）。",
                        f"MAST cannot write back the old values of zone(s) {res['not_restorable']} "
                        "(the byte factory refuses them, e.g. a manual output other than 0). ")
        heater = ""
        if res["off"]:
            heater = (tr("写入后加热器读到开着，已关。", "The heater read on after a write and was turned off.")
                      if (res["off"] or {}).get("confirmed") else
                      tr("写入后加热器读到开着，还没读到关闭：请操作员在 335 面板上按 ALL OFF。",
                         "The heater read on after a write and does not read off yet: the operator "
                         "should press ALL OFF on the 335."))
        res["error"] = (tr(f"分区表没有写完（{failure}）。", f"the zone table was not written ({failure}). ")
                        + where + heater)
        self._ledger({"action": "zone_table", "result": "restored" if res["restored"] else "failed",
                      "origin": origin, "output": o, "error": res["error"], "zones": res["zones"]})
        logger.warning("heater control: zone table write on output %d failed: %s", o, res["error"])
        return res

    def set_control_mode(self, mode: Any, *, origin: str) -> dict:
        """Switch the configured output between closed-loop PID (1) and zone (2):
        ``OUTMODE <out>,<mode>,<control input>,0``, read back.

        Preconditions as for :meth:`write_zone_table`; besides, the output must now be in
        closed-loop PID or zone mode on the configured control input with power-up enable
        off, so that the write changes the mode and nothing else, and zone mode needs the
        stored zone table to pass :func:`validate_zone_table`. The old mode goes into the
        ledger first. After the write ``OUTMODE?`` must read the new mode and ``RANGE?``
        must still read 0 (the manual does not say whether entering zone mode applies a
        zone at once). On any failure the heater is turned off if it does not read 0 and
        the old mode is written back. Never raises."""
        res: dict = {"set": False, "noop": False, "error": "", "output": None, "old_mode": None,
                     "mode": None, "restored": None, "off": None, "origin": str(origin)[:200]}
        m = _control_mode_code(mode)
        if m is None:
            res["error"] = tr(f"控制模式 {mode!r} 不是 1（闭环 PID）或 2（分区）",
                              f"control mode {mode!r} is neither 1 (closed-loop PID) nor 2 (zone)")
            return res
        res["mode"] = m
        if self._closing:
            res["error"] = tr("MAST 正在关闭：不改控制模式", "MAST is shutting down: no mode change")
            return res
        if not self._change_lock.acquire(blocking=False):
            res["error"] = tr("另一次 MAST 温度变化或配置写入还没结束",
                              "another MAST temperature change or configuration write has not "
                              "finished")
            return res
        try:
            return self._set_control_mode_locked(m, res, origin=origin)
        except Exception as exc:  # noqa: BLE001 - never raises; what happened is in the ledger
            logger.exception("heater control: control mode write failed")
            res["error"] = res["error"] or f"{type(exc).__name__}: {exc}"
            return res
        finally:
            self._change_lock.release()

    def _set_control_mode_locked(self, m: int, res: dict, *, origin: str) -> dict:
        s = self.settings()
        why = self._config_refusal(s)
        if why:
            res["error"] = why
            self._ledger({"action": "control_mode", "result": "refused", "error": why,
                          "origin": origin, "mode": m})
            return res
        o = int(s.output)
        want_in = INPUTS.index(s.control_input) + 1
        res["output"] = o
        try:
            st = self._config_state(o, zones=m == MODE_ZONE)
        except WrongController as exc:
            res["error"] = tr(f"端口上不是那台 Model 335：{exc}", f"not the Model 335 on the port: {exc}")
            return res
        except (SerialUnavailable, LakeshoreError) as exc:
            res["error"] = tr(f"读不到温控仪：{exc}", f"cannot read the controller: {exc}")
            return res
        why = self._config_state_refusal(st, o, zones=m == MODE_ZONE)
        old_mode = old_in = old_pu = None
        if not why:                              # OUTMODE? answered all three fields
            old_mode, old_in, old_pu = st["outmode"][:3]
        res["old_mode"] = old_mode
        if not why and old_mode not in CONTROL_MODES:
            why = tr(f"输出 {o} 现在是 {MODE_NAMES.get(old_mode, old_mode)} 模式：MAST 只在闭环 PID 和分区之间切换，"
                     "其他模式请操作员在面板上处理",
                     f"output {o} is now in {MODE_NAMES.get(old_mode, old_mode)} mode: MAST switches "
                     "only between closed-loop PID and zone; any other mode is the operator's, at "
                     "the panel")
        if not why and old_in != want_in:
            why = tr(f"输出 {o} 的控制输入是 {old_in}，初始化设定的是 {s.control_input}（{want_in}）：MAST 不换"
                     "控制输入，请操作员在面板上处理",
                     f"output {o} is controlled by input {old_in}, initialization says "
                     f"{s.control_input} ({want_in}): MAST does not change the control input; the "
                     "operator sets it at the panel")
        if not why and old_pu != 0:
            why = tr("断电恢复后加热器会自己回到原量程（powerup enable = 1）：MAST 只改模式，不改它。"
                     "请操作员在面板上关掉 Powerup Enable",
                     "after a power cut the heater would come back on by itself (powerup enable "
                     "= 1): MAST changes only the mode, not that. Ask the operator to turn Powerup "
                     "Enable off at the panel")
        if not why and m == MODE_ZONE:
            verdict = validate_zone_table([Zone.from_dict(d) for d in st["zones"]],
                                          control_input=s.control_input, max_range=int(s.max_range),
                                          max_rate_k_per_min=float(s.max_rate_k_per_min),
                                          max_setpoint_k=float(s.max_setpoint_k),
                                          max_mout_pct=float(s.max_mout_pct))
            res["verdict"] = verdict.as_dict()
            if not verdict.ok:
                why = (tr("存着的分区表不能用，不切到分区模式：", "the stored zone table cannot be used; "
                                                       "not switching to zone mode: ")
                       + "; ".join(verdict.problems))
        if why:
            res["error"] = why
            self._ledger({"action": "control_mode", "result": "refused", "error": why,
                          "origin": origin, "output": o, "mode": m, "old_mode": old_mode})
            return res
        if old_mode == m:
            res.update(noop=True, set=True)
            return res
        self._ledger({"action": "control_mode", "phase": "begin", "origin": origin, "output": o,
                      "old_mode": old_mode, "mode": m, "control_input": want_in})
        failure = ""
        back: "list | None" = None
        rr: "int | None" = None
        try:
            with self.link.session(timeout=10.0) as gen:
                again = self._config_state(o, zones=m == MODE_ZONE)
                if (self._config_state_refusal(again, o, zones=m == MODE_ZONE)
                        or again.get("outmode") != st.get("outmode")
                        or again.get("zones") != st.get("zones")):
                    res["error"] = tr("读完之后状态变了：没有写任何东西，请再试",
                                      "the state changed after it was read: nothing written; try "
                                      "again")
                    self._ledger({"action": "control_mode", "result": "refused",
                                  "error": res["error"], "origin": origin, "output": o})
                    return res
                err = self._send(gen, "OUTMODE", o, m, want_in, 0)
                back = _ints(self._readback("OUTMODE?", str(o)) or "")
                try:
                    rr = _int(self.link.query("RANGE?", str(o)))
                except (SerialUnavailable, LakeshoreError):
                    rr = None
                if err or back[:3] != [m, want_in, 0] or rr != 0:
                    bits = [err] if err else []
                    if back[:3] != [m, want_in, 0]:
                        bits.append(tr(f"OUTMODE? 读回 {back}", f"OUTMODE? read back {back}"))
                    if rr != 0:
                        bits.append(tr(f"写完后 RANGE? = {rr}", f"RANGE? after it = {rr}"))
                    failure = "; ".join(bits)
                    if rr != 0:
                        res["off"] = self.heater_off(reason=f"control mode write: {failure}",
                                                     origin="heater control (control mode)",
                                                     outputs=(o,))
                    if back[:3] != [old_mode, want_in, 0]:
                        # The mode changed, or is unknown: put the old one back.
                        err2 = self._send(gen, "OUTMODE", o, old_mode, want_in, 0)
                        back = _ints(self._readback("OUTMODE?", str(o)) or "")
                        res["restored"] = not err2 and back[:3] == [old_mode, want_in, 0]
                        try:
                            rr = _int(self.link.query("RANGE?", str(o)))
                        except (SerialUnavailable, LakeshoreError):
                            rr = None
                        if rr != 0:
                            res["off"] = self.heater_off(reason="control mode restore: RANGE? not 0",
                                                         origin="heater control (control mode)",
                                                         outputs=(o,))
                    else:
                        res["restored"] = True        # ignored: the old mode is still there
        except WrongController as exc:
            failure = failure or tr(f"端口上不是那台 Model 335：{exc}", f"not the Model 335 on the port: {exc}")
        except (SerialUnavailable, LakeshoreError) as exc:
            failure = failure or tr(f"温控仪不可用：{exc}", f"controller unavailable: {exc}")
        res["readback"] = back
        if not failure:
            res["set"] = True
            self._ledger({"action": "control_mode", "result": "set", "origin": origin, "output": o,
                          "old_mode": old_mode, "mode": m})
            logger.warning("heater control: output %d control mode %s -> %s by %s", o,
                           MODE_NAMES.get(old_mode), MODE_NAMES.get(m), origin)
            return res
        now = MODE_NAMES.get(back[0], back[0]) if back else tr("不知道", "unknown")
        res["error"] = (tr(f"控制模式没有改成 {MODE_NAMES[m]}（{failure}）。现在读到：{now}",
                           f"the control mode was not changed to {MODE_NAMES[m]} ({failure}). It now "
                           f"reads: {now}")
                        + ("" if not res["off"] else
                           tr("；加热器读到开着，已关" if (res["off"] or {}).get("confirmed") else
                              "；加热器读到开着，还没读到关闭：请操作员在 335 面板上按 ALL OFF",
                              "; the heater read on and was turned off"
                              if (res["off"] or {}).get("confirmed") else
                              "; the heater read on and does not read off yet: the operator should "
                              "press ALL OFF on the 335")))
        self._ledger({"action": "control_mode", "result": "restored" if res["restored"] else "failed",
                      "origin": origin, "output": o, "old_mode": old_mode, "mode": m,
                      "error": res["error"], "readback": back})
        logger.warning("heater control: control mode write on output %d failed: %s", o, res["error"])
        return res

    # ── hold ──
    def hold(self, *, reason: str, origin: str, lock_timeout_s: float = OFF_LOCK_TIMEOUT_S) -> dict:
        """Stop a setpoint ramp near the present temperature; the heater stays on. Never raises.

        ``SETP <out>,<T now>`` with ramping still on, then ``RAMP <out>,0,<rate>``. T now
        comes from the input the output is controlled by (``OUTMODE?``), only if it reads
        in kelvin; otherwise the hold is ``RAMP <out>,0`` alone. In zone mode with the
        heater not reading on, it is ``RAMP <out>,0`` alone too: a SETP that crosses into
        another zone would load that zone's range and turn the heater on (§5.3 p. 69).
        Nothing is written when the setpoint is not ramping (``nothing_to_hold``).
        """
        s = self.settings()
        with self._state_lock:
            self._hold_seq += 1
            seq = self._hold_seq
        out: dict = {"seq": seq, "held": False, "nothing_to_hold": False, "error": "",
                     "reason": str(reason)[:300], "origin": str(origin)[:200], "at": time.time()}
        act = self.active
        w = self._watch
        o = int(act["output"]) if act else int(w["output"]) if w else s.output
        if o not in OUTPUTS:
            out["error"] = tr("控温没有配置：不知道停哪一路", "temperature control is not set up: no output to hold")
            return self._record_hold(out)
        ctrl = ""
        t_now: "float | None" = None
        try:
            with self.link.session(timeout=lock_timeout_s) as gen:
                ramping = _int(self.link.query("RAMPST?", str(o)))
                if ramping != 1:
                    out["nothing_to_hold"] = True
                    return self._record_hold(out)
                ramp = _fields(self.link.query("RAMP?", str(o)))
                rate = _num(ramp[1]) if len(ramp) > 1 else None
                rate = rate if rate is not None and rate >= RATE_MIN_K_PER_MIN else RATE_MIN_K_PER_MIN
                om = _ints(self.link.query("OUTMODE?", str(o)))
                ctrl = INPUTS[om[1] - 1] if len(om) > 1 and om[1] in (1, 2) else ""
                # Zone mode (or a mode that does not answer) with the heater not reading on:
                # a SETP crossing into another zone would load its range and turn the heater
                # on (p. 69). The hold is RAMP 0 alone then.
                read_t = bool(ctrl)
                if read_t and (not om or om[0] != MODE_CLOSED_LOOP):
                    try:
                        read_t = (_int(self.link.query("RANGE?", str(o))) or 0) > 0
                    except (SerialUnavailable, LakeshoreError):
                        read_t = False
                    out["zone_heater_off"] = not read_t
                if read_t:
                    units = _ints(self.link.query("INTYPE?", ctrl))
                    k = _num(self.link.query("KRDG?", ctrl))
                    st = _int(self.link.query("RDGST?", ctrl))
                    if (len(units) > 4 and units[4] == 1 and st == 0 and k is not None
                            and k >= TEMPERATURE_MIN_VALID_K):
                        t_now = round(k, SETPOINT_DECIMALS)
                        if s.max_setpoint_k is not None:      # never a setpoint above the maximum
                            t_now = min(t_now, float(s.max_setpoint_k))
                out["steps"] = []
                if t_now is not None:
                    res1, back1 = self._step(gen, o, "SETP", t_now)
                    out["steps"].append({"cmd": "SETP", "value": t_now, "result": res1, "readback": back1})
                    if res1 == "error":
                        raise LakeshoreError(f"SETP: {back1}")
                res2, back2 = self._step(gen, o, "RAMP", 0, rate)
                out["steps"].append({"cmd": "RAMP", "values": [0, rate], "result": res2,
                                     "readback": back2})
                # Held = ramping reads off AND the setpoint reads not ramping: the same rule
                # as a heater-off's in zone mode, where a ramp left running reloads ranges.
                try:
                    still = _int(self.link.query("RAMPST?", str(o)))
                except (SerialUnavailable, LakeshoreError):
                    still = None
                out["ramping"] = still
                out["held"] = res2 == "taken" and still == 0
                if not out["held"]:
                    out["error"] = tr(f"RAMP 读回 {back2!r}，RAMPST? {still}：没停住",
                                      f"RAMP read back {back2!r}, RAMPST? {still}: not held")
                sp_after = _num(self.link.query("SETP?", str(o)))
                rng_after = _int(self.link.query("RANGE?", str(o)))
                out.update(setpoint_k=sp_after, range=rng_after)
        except Exception as exc:  # noqa: BLE001 - a hold never raises
            out["error"] = f"{type(exc).__name__}: {exc}"
        out = self._record_hold(out)
        ctrl_in = ctrl or (str(act["control_input"]) if act else s.control_input)
        if (out["held"] and not self.busy and (out.get("range") or 0) > 0 and w is None
                and ctrl_in in INPUTS):
            self._start_watch(o, ctrl_in, max(float(out.get("setpoint_k") or 0.0),
                                              float(t_now or 0.0)), why="held")
        return out

    def _record_hold(self, out: dict) -> dict:
        with self._state_lock:
            self._last_hold = dict(out)
        self._ledger({"action": "hold", **out})
        logger.warning("heater control: hold %s (%s; by %s)%s",
                       "done" if out["held"] else "nothing to hold" if out["nothing_to_hold"] else "FAILED",
                       out["reason"], out["origin"], f": {out['error']}" if out["error"] else "")
        return out

    # ── a ramping setpoint in zone mode, after the heater went off ──
    def _present_rate(self, out: int, s: HeaterSettings) -> float:
        """The ramp rate the output already has (``RAMP?``), so stopping a ramp does not
        also change the rate; the configured maximum rate when RAMP? gives none (the 335
        ignores the rate with ramping off, but build_write needs one)."""
        try:
            f = _fields(self.link.query("RAMP?", str(out)))
            rate = _num(f[1]) if len(f) > 1 else None
        except (SerialUnavailable, LakeshoreError):
            rate = None
        if rate is not None and RATE_MIN_K_PER_MIN <= round(rate, 1) <= RATE_MAX_K_PER_MIN:
            return round(rate, 1)
        mr = s.max_rate_k_per_min
        if mr is not None and RATE_MIN_K_PER_MIN <= round(float(mr), 1) <= RATE_MAX_K_PER_MIN:
            return round(float(mr), 1)
        return RATE_MIN_K_PER_MIN

    def _stop_zone_ramp(self, gen, out: int, s: HeaterSettings, *, send_only: bool = False) -> dict:
        """Holding the link, around a heater-off: in zone mode a RANGE 0 holds only
        until the setpoint crosses into another zone, which loads that zone's range again
        (§5.3 p. 69). So when the output is in zone mode (or its mode does not answer) and
        its setpoint ramps (or RAMPST? does not answer): ``RAMP <out>,0,<rate it has>``, and
        the setpoint "will remain on the most current setting" (p. 63).

        ``{"zone_mode": bool | None, "ramping": int | None, "sent": str | None, "stopped":
        bool}``. ``stopped`` is the second half of the off's confirm rule: the output is not
        in zone mode, or RAMPST? reads 0 now. In closed-loop PID nothing is written.

        ``send_only`` (the first phase of :meth:`heater_off`, before any RANGE 0): the
        ``RAMP <out>,0,<rate>`` goes out with no readback and no sleep, so no zone boundary
        can be crossed between it and the RANGE 0; ``sent`` is then ``"sent"`` and
        ``stopped`` is ``None`` until :meth:`_finish_zone_ramp` reads RAMPST? back."""
        r: dict = {"zone_mode": None, "ramping": None, "sent": None, "stopped": False}
        try:
            om = _ints(self.link.query("OUTMODE?", str(out)))
        except (SerialUnavailable, LakeshoreError):
            om = []
        if om and om[0] is not None:
            r["zone_mode"] = om[0] == MODE_ZONE
        if r["zone_mode"] is False:
            r["stopped"] = True                  # no zone table in force: nothing reloads
            return r
        try:
            ramping = _int(self.link.query("RAMPST?", str(out)))
        except (SerialUnavailable, LakeshoreError):
            ramping = None
        r["ramping"] = ramping
        if ramping == 0:
            r["stopped"] = True
            return r
        rate = self._present_rate(out, s)
        if send_only:
            try:
                err = self._send(gen, "RAMP", out, 0, rate)
                result = "error" if err else "sent"
            except ValueError:
                result, err = "refused", ""
            r.update(sent=result, rate=rate, readback=None, stopped=None, error=err)
            return r
        result, back = self._step(gen, out, "RAMP", 0, rate)
        try:
            after = _int(self.link.query("RAMPST?", str(out)))
        except (SerialUnavailable, LakeshoreError):
            after = None
        r.update(sent=result, rate=rate, readback=back, ramping=after, stopped=after == 0)
        logger.warning("heater control: output %d in zone mode was ramping with the heater off: "
                       "RAMP %d,0,%.1f sent (%s; RAMPST? now %s) so that no zone boundary turns "
                       "the heater back on", out, out, rate, result, after)
        return r

    def _finish_zone_ramp(self, out: int, zr: dict) -> None:
        """After the RANGE 0 and its readback: read RAMPST? for an output whose ramp was
        stopped in the send-only phase, and settle ``sent`` / ``stopped``."""
        if zr.get("zone_mode") is False or zr.get("stopped") is not None:
            return
        try:
            after = _int(self.link.query("RAMPST?", str(out)))
        except (SerialUnavailable, LakeshoreError):
            after = None
        zr.update(ramping=after, stopped=after == 0)
        if zr.get("sent") == "sent":
            zr["sent"] = "taken" if after == 0 else "unexpected"
        logger.warning("heater control: output %d in zone mode was ramping: RAMP %d,0,%.1f went "
                       "out before RANGE 0 (RAMPST? now %s) so that no zone boundary turns the "
                       "heater back on", out, out, float(zr.get("rate") or 0.0), after)

    def _zone_takeover_stop(self, out: int, s: HeaterSettings, *, origin: str) -> dict:
        """Heating MAST started or watches was turned off elsewhere while its setpoint
        ramps: in zone mode the next zone boundary would turn it back on (p. 69). The one
        write MAST makes after a takeover: ``RAMP <out>,0`` (see :meth:`_stop_zone_ramp`),
        logged loudly and recorded in the ledger. Also reads RANGE? again. Never raises."""
        res: dict = {"zone_mode": None, "ramping": None, "sent": None, "stopped": False,
                     "range": None, "error": ""}
        try:
            with self.link.session(timeout=OFF_LOCK_TIMEOUT_S) as gen:
                res.update(self._stop_zone_ramp(gen, out, s))
                if res["zone_mode"] is not False:
                    res["range"] = _int(self.link.query("RANGE?", str(out)))
        except Exception as exc:  # noqa: BLE001 - never raises
            res["error"] = f"{type(exc).__name__}: {exc}"
        if res["sent"] is not None:
            self._ledger({"action": "zone_ramp_stop", "output": out, "origin": origin,
                          "kind": "de-escalating write after a takeover", **res})
            logger.log(logging.WARNING if res["stopped"] else logging.CRITICAL,
                       "heater control: output %d was turned off elsewhere while its setpoint "
                       "ramped in zone mode; MAST wrote RAMP %d,0 so the next zone does not "
                       "turn it back on (%s; stopped: %s)", out, out, res["sent"], res["stopped"])
        return res

    # ── heater off ──
    def heater_off(self, *, reason: str, origin: str, outputs: "tuple[int, ...] | None" = None,
                   lock_timeout_s: float = OFF_LOCK_TIMEOUT_S) -> dict:
        """A bare terminator, ``RANGE <out>,0`` for each output, then ``RANGE?``; in zone
        mode a ramping setpoint is then stopped (:meth:`_stop_zone_ramp`). Never raises.

        Default outputs: the configured one, plus the one a MAST change or the watch is
        on; both when there is none (like ALL OFF). ``sent`` = every RANGE write went out;
        ``confirmed`` = every output reads RANGE? 0 and, in zone mode (or when its mode does
        not answer), RAMPST? 0 (``zone_ramp`` says what was done per output). An output not
        confirmed is retried in the background until it is. Refused (``refused``) where
        temperature control is not set up and MAST's own heating is not on: there MAST
        writes nothing to the 335.
        """
        s = self.settings()
        act = self.active
        w = self._watch
        res: dict = {"outputs": [], "sent": False, "confirmed": False, "refused": False,
                     "error": "", "reason": str(reason)[:300], "origin": str(origin)[:200],
                     "at": time.time(), "readback": {}, "zone_ramp": {}}
        if not (s.control_configured or act is not None or w is not None or self._retry_off):
            res.update(refused=True, error=tr(
                "这台 335 没有在「仪器初始化」里设置控温，MAST 也没有在加热：MAST 不写这台仪器。"
                "要关加热器请在 335 面板上按 ALL OFF",
                "temperature control is not set up for this 335 and MAST is not heating: MAST "
                "writes nothing to it. To turn a heater off press ALL OFF on the 335"))
            return res
        if outputs is None:
            chosen = {int(s.output)} if s.output in OUTPUTS else set()
            if act:
                chosen.add(int(act["output"]))
            if w:
                chosen.add(int(w["output"]))
            outs = tuple(sorted(chosen)) or OUTPUTS
        else:
            outs = tuple(sorted({int(x) for x in outputs if x in OUTPUTS})) or OUTPUTS
        with self._state_lock:
            self._off_seq += 1
            seq = self._off_seq
            for o in outs:
                self._off_req[o] = seq
            self._offs_in_flight += 1
        res.update(seq=seq, outputs=list(outs))
        confirmed: list[int] = []
        checked: set[int] = set()
        try:
            with self.link.session(timeout=lock_timeout_s) as gen:
                errors = []
                flush = self._send(gen, "FLUSH")
                if flush:
                    errors.append(f"terminator: {flush}")
                # Zone mode: RANGE 0 holds only until the setpoint crosses into another
                # zone, which loads that zone's range again (p. 69). A stopped ramp leaves
                # the setpoint "on the most current setting" (p. 63), so it is stopped
                # first, sent only (no readback, no sleep), before any RANGE 0.
                for o in outs:
                    try:
                        res["zone_ramp"][o] = self._stop_zone_ramp(gen, o, s, send_only=True)
                    except Exception as exc:  # noqa: BLE001 - the RANGE 0 below goes out anyway
                        res["zone_ramp"][o] = {"zone_mode": None, "ramping": None, "sent": None,
                                               "stopped": False, "error": f"{type(exc).__name__}: {exc}"}
                for o in outs:
                    err = self._send(gen, "RANGE", o, 0)
                    if err:
                        errors.append(f"output {o}: {err}")
                res["sent"] = not any(e.startswith("output") for e in errors)
                if errors:
                    res["error"] = "; ".join(errors)
                for o in outs:
                    back = self._readback("RANGE?", str(o))
                    res["readback"][o] = back
                    if _int(back) == 0:
                        confirmed.append(o)
                # Done means RANGE? 0 and, in zone mode (or when the mode did not answer),
                # RAMPST? 0, all read back together after every RANGE 0 went out.
                for o in outs:
                    zr = res["zone_ramp"][o]
                    if zr.get("error") and "zone_mode" in zr and zr["zone_mode"] is None \
                            and zr.get("sent") is None and zr.get("stopped") is False:
                        continue                 # the first phase broke: not checked, retried
                    self._finish_zone_ramp(o, zr)
                    if zr["zone_mode"] is not False:
                        zr["range"] = _int(res["readback"].get(o))
                        if o in confirmed and not zr["stopped"]:
                            confirmed.remove(o)
                    checked.add(o)               # closed-loop PID: RANGE? 0 was enough
        except Exception as exc:  # noqa: BLE001 - a heater-off never raises
            res["error"] = (res["error"] + "; " if res["error"] else "") + f"{type(exc).__name__}: {exc}"
        # An output whose zone-mode check did not run (the link failed half way) is not done.
        confirmed = [o for o in confirmed if o in checked]
        res["confirmed"] = len(confirmed) == len(outs)
        with self._state_lock:
            self._offs_in_flight -= 1
            for o in confirmed:
                self._off_ok[o] = max(self._off_ok.get(o, 0), seq)
                self._retry_off.pop(o, None)
            for o in outs:
                if o not in confirmed:
                    self._retry_off.setdefault(o, self._clock())
            self._last_off = dict(res)
        if not res["confirmed"]:
            self._ensure_background()
        if w is not None and int(w["output"]) in confirmed:
            self._stop_watch("heater_off", tr("加热器已关", "heater turned off"))
        self._ledger({"action": "heater_off", **res})
        level = logging.WARNING if res["confirmed"] else logging.CRITICAL
        logger.log(level, "heater control: RANGE 0 on %s %s (%s; by %s)%s", list(outs),
                   "confirmed" if res["confirmed"] else "NOT confirmed", reason, origin,
                   f": {res['error']}" if res["error"] else "")
        return res

    def hold_if_active(self, *, reason: str, origin: str) -> "dict | None":
        """``hold`` only while a MAST change is being prepared, sent or supervised."""
        if not self.busy:
            return None
        return self.hold(reason=reason, origin=origin)

    def begin_shutdown(self, *, reason: str = "MAST is shutting down",
                       origin: str = "MAST shutdown") -> "dict | None":
        """Refuse new changes from now on; stop a MAST ramp in flight where it is. The
        heater stays on, regulating (by design): the 335 needs no
        host to hold a setpoint, and its TLIMIT (required before MAST heats) does not
        depend on MAST. The ledger lets the next MAST process watch it again."""
        self._closing = True
        return self.hold_if_active(reason=reason, origin=origin)

    # ── the ledger ──
    def _ledger(self, entry: dict) -> None:
        try:
            path = self._ledger_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "port": self.link.port, **entry}
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 - the ledger never blocks a heater-off
            logger.warning("heater control: ledger write failed: %s", exc)


__all__ = [
    "EXAMPLE_AUTHORIZATIONS",
    "Heater335Controller",
    "HeaterControlError",
    "HeaterPlan",
    "HeaterSettings",
    "UNUSED_ZONE",
    "Zone",
    "ZoneTableVerdict",
    "build_write",
    "overshoot_allowance_k",
    "settle_band_k",
    "temperatures_named_in",
    "used_zones",
    "validate_zone_table",
    "zone_eta_s",
    "zone_heating_path",
    "zone_of",
    "zone_table_from",
    "zones_on_path",
]
