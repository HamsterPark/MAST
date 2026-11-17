"""Lakeshore temperature monitor / controller driver (SCPI over RS-232/485).

Lakeshore cryogenic monitors and controllers (Model 218 / 224 / 331 / 332 /
335 / 336 / 340 …) speak a common SCPI-like ASCII protocol:

* Identify: ``*IDN?`` → ``LSCI,MODEL336,<serial>,<firmware>`` (CRLF terminated).
  The leading ``LSCI`` token is how we auto-recognise the instrument.
* Read Kelvin: ``KRDG? <input>`` → e.g. ``+273.150E+0`` (CRLF). ``CRDG?`` gives
  Celsius, ``SRDG?`` raw sensor units.
* Inputs are letters ``A``..``D`` on the 33x/335/336 family and numbers
  ``1``..``8`` on the 218; both are supported via the ``channel`` argument.

Line settings differ across the range (the 218 defaults to 9600, the 336 to
57600), all **7 data bits, odd parity, 1 stop bit**. :func:`probe_lakeshore`
tries the common combinations so auto-detection works without the user knowing
the exact baud.

The bundled "另一个串口程序" (a LabVIEW logger) talks the same serial
protocol; this driver lets MAST read the instrument directly instead of
scraping the logger, while leaving the logger free to run if the user prefers.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
import weakref

from mast.i18n import tr
from mast.core.types import SensorReading
from mast.environment.alarm import AlarmSpec
from mast.environment.base import EnvironmentSensor
from mast.environment.serial_transport import (
    SerialLike,
    SerialSettings,
    SerialTransport,
    SerialUnavailable,
    is_complete_line,
    open_transport,
)

logger = logging.getLogger(__name__)

LSCI_TERMINATOR = b"\r\n"

# (baudrate, bytesize, parity, stopbits) combos tried during auto-detection,
# most-common first. All Lakeshore serial links are 7-bit / odd-parity.
LSCI_SERIAL_CANDIDATES: tuple[tuple[int, int, str, float], ...] = (
    (57600, 7, "O", 1),   # 336 / 335 default
    (9600, 7, "O", 1),    # 218 / 33x default
    (115200, 7, "O", 1),  # 336 high-speed option
)

#: ``*IDN?`` is asked this many times at each candidate baud (see :func:`probe_lakeshore`):
#: the first query's terminator clears junk a wrong-baud candidate left in an RS-232
#: instrument's command buffer, and the second gets the answer.
IDN_ATTEMPTS = 2
#: The repeat waits less than the first query. A Lake Shore answers ``*IDN?`` within tens
#: of milliseconds even at 9600 baud, and a silent port costs this much extra per candidate.
IDN_RETRY_TIMEOUT_S = 0.25

_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")

# SECURITY: only read/query commands are ever sent. Whitelisting the command
# and restricting the channel to an alphanumeric token prevents a crafted
# config entry (reachable via the POST /environment/sensors endpoint) from
# injecting an arbitrary SCPI WRITE — e.g. channel="A\r\nRANGE 1,3" would
# otherwise drive a heater on a controller-type Lakeshore. MAST never writes
# to the instrument from this path; these are temperature reads only.
_SAFE_COMMANDS = frozenset({"KRDG?", "CRDG?", "SRDG?"})
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9]{1,4}$")

# Additional READ-ONLY queries used by discovery and the instrument-state
# readout (heater range / output / setpoint / control mode). Every entry ends
# in '?', and :func:`_query` re-checks that at call time — so no code path in
# this module can emit a Lakeshore SETTER (SETP / RANGE / MOUT / OUTMODE / PID
# without the '?'), which is what would actually drive a heater. The state
# readout exists precisely so MAST can SHOW whether a heater is on without ever
# being able to turn one on.
_STATE_QUERIES = frozenset({
    "*IDN?",       # identity
    "INNAME?",     # user-assigned input label, e.g. "SPM" / "Magnet"
    "INTYPE?",     # sensor type / range / compensation
    "RDGST?",      # reading status bits (invalid / over- / under-range)
    "RANGE?",      # heater range: 0 = OFF
    "HTR?",        # heater output, %
    "SETP?",       # setpoint
    "OUTMODE?",    # control mode + control input + powerup-enable
    "RAMP?",       # setpoint ramp on/off + rate
    # Read by temperature control (mast.instruments.heater_335) before it changes anything;
    # not part of the settings readout, so the instrument-state panel sends what it always did.
    "RAMPST?",     # 0 = not ramping, 1 = the setpoint is ramping
    "HTRSET?",     # output type, heater resistance, max current, max user current, I/P display
    "PID?",        # P, I, D
    "MOUT?",       # manual output %, ADDED to the PID output in closed loop
    "TLIMIT?",     # the instrument's own temperature limit for an input (0 = off)
    "EMUL?",       # 331/332 emulation: changes what RANGE, HTR, ZONE ... mean
    "MODE?",       # 0 local, 1 remote, 2 remote with local lockout
    # One row of an output's zone table: "ZONE? <output 1|2>,<zone 1..10>" (manual p. 126).
    # Its argument has a comma, so build_query checks it against its own pattern.
    "ZONE?",
}) | _SAFE_COMMANDS

#: Queries deliberately ABSENT although they end in '?': reading them changes the
#: instrument (Model 335 manual, chapter 6). A test pins that none of them ever becomes
#: sendable.
#: * ``HTRST?`` "Error condition is cleared upon querying the heater status, which will
#:   also clear the front panel error message" (p. 116): one poll would erase a heater
#:   open/short report before the operator saw it.
#: * ``*ESR?`` and ``OPSTR?`` return a latched register and clear it (pp. 95, 121).
#: * ``LOCK?`` returns the front-panel lock code in clear.
FORBIDDEN_QUERIES: frozenset[str] = frozenset({"HTRST?", "*ESR?", "OPSTR?", "LOCK?"})
assert not (FORBIDDEN_QUERIES & _STATE_QUERIES)


def _sanitize_command(command: str) -> str:
    cmd = str(command).strip().upper()
    if cmd not in _SAFE_COMMANDS:
        logger.warning("Lakeshore: rejecting non-whitelisted command %r → KRDG?", command)
        return "KRDG?"
    return cmd


def _sanitize_channel(channel: str) -> str:
    # None / "" must fall back to 'A' — note str(None) == "None" happens to be a
    # 4-char alphanumeric token that would otherwise pass the regex and get sent
    # as a literal channel "None".
    if channel is None:
        return "A"
    ch = str(channel).strip()
    if ch and _CHANNEL_RE.match(ch):
        return ch
    logger.warning("Lakeshore: rejecting unsafe channel %r → A", channel)
    return "A"


# Reading-unit implied by the SCPI query, so a CRDG?/SRDG? reading isn't
# mislabelled as kelvin.
_UNIT_BY_COMMAND = {"KRDG?": "K", "CRDG?": "°C", "SRDG?": ""}


class LakeshoreError(ValueError):
    """Unparseable Lakeshore reply."""


def parse_idn(raw: bytes | str) -> tuple[bool, str]:
    """Return ``(is_lakeshore, model)`` from a ``*IDN?`` reply.

    Lakeshore replies start with the manufacturer token ``LSCI`` (older units
    spell out ``LAKESHORE``). The second comma-field is the model, e.g.
    ``MODEL336``.
    """
    text = raw.decode("ascii", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    text = text.strip()
    parts = [p.strip() for p in text.split(",")]
    first = parts[0].upper() if parts else ""
    # Field-based, not substring: the manufacturer token is the FIRST CSV field
    # ("LSCI" exact, or "LAKESHORE..." on older units). A bare substring match
    # would mis-identify a device whose payload merely contains "LSCI", and an
    # empty model field must NOT be accepted as a Lakeshore.
    is_lsci = first == "LSCI" or first.startswith("LAKESHORE")
    model = parts[1] if len(parts) > 1 else ""
    return (is_lsci and bool(model)), model


def parse_reading(raw: bytes | str) -> float:
    """Parse a ``KRDG?`` reply (e.g. ``+273.150E+0``) → float kelvin.

    Anchored match (not search): a malformed reply like ``ERR 5`` must raise
    rather than silently return 5 as a plausible-but-wrong temperature."""
    text = raw.decode("ascii", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    m = _NUM_RE.match(text.strip())
    if not m:
        raise LakeshoreError(f"reply is not a leading number: {text!r}")
    return float(m.group(0))


def default_channels(model: str) -> list[str]:
    """Best-effort default input list for a model string."""
    m = (model or "").upper()
    if "218" in m:
        return [str(i) for i in range(1, 9)]   # 8 inputs
    if "224" in m:
        return ["A", "B", "C1", "C2", "C3", "C4", "C5", "D1", "D2", "D3", "D4", "D5"]
    if any(t in m for t in ("336", "350")):   # 4-input controllers
        return ["A", "B", "C", "D"]
    if any(t in m for t in ("335", "331", "332", "340")):  # 2-input
        return ["A", "B"]
    return ["A"]


class LakeshoreInfo:
    """Result of a successful probe."""

    __slots__ = ("model", "idn", "settings")

    def __init__(self, model: str, idn: str, settings: SerialSettings):
        self.model = model
        self.idn = idn
        self.settings = settings

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"LakeshoreInfo(model={self.model!r}, {self.settings.label()})"


#: Lake Shore temperature monitors / controllers / bridges this driver may poll
#: with its temperature queries. Other LSCI products answer ``*IDN?`` the same
#: way — the Model 625 superconducting-magnet supply does — and must NOT be
#: treated as a thermometer (see ``environment.lakeshore_625``).
_TEMPERATURE_MODEL_PREFIXES = ("211", "218", "224", "325", "331", "332", "335",
                             "336", "340", "350", "370", "372")


def is_temperature_model(model: str) -> bool:
    """True for a Lake Shore model string that is a temperature instrument."""
    m = (model or "").upper().replace("MODEL", "").strip()
    return any(m.startswith(t) for t in _TEMPERATURE_MODEL_PREFIXES)


def probe_lakeshore(port: str, *, candidates=LSCI_SERIAL_CANDIDATES,
                    timeout: float = 0.5, on_reply=None) -> "LakeshoreInfo | None":
    """Try to identify a Lakeshore instrument on *port*.

    Iterates the serial-parameter candidates, sends ``*IDN?``, and returns a
    :class:`LakeshoreInfo` on the first reply containing ``LSCI``. Returns
    ``None`` (never raises) if nothing matches / no pyserial / port busy.

    The returned model may be ANY Lake Shore product; callers that poll
    temperatures must check :func:`is_temperature_model` first.
    ``on_reply(bytes)`` is told about every non-empty answer, so the caller can
    tell a silent port from one where something talked.
    """
    for baud, bits, parity, stop in candidates:
        settings = SerialSettings(port=port, baudrate=baud, bytesize=bits,
                                  parity=parity, stopbits=stop, timeout=timeout)
        transport: SerialTransport | None = None
        try:
            transport = open_transport(settings)
            # Asked twice at the same baud before moving on (IDN_ATTEMPTS). The candidate
            # before this one went out at a wrong baud, and an RS-232 instrument (e.g. a 625
            # at 9600) decodes that as junk. When the junk holds no line
            # terminator it stays in the instrument's command buffer, so the first proper
            # "*IDN?" here joins it into one invalid command and gets no answer. That
            # query's own CR LF clears the buffer, so the second one is answered.
            # Motivating case: a port "answered but was not
            # recognised" while a manual scan minutes later found the 625. Only "*IDN?"
            # is ever sent, as before.
            for attempt in range(IDN_ATTEMPTS):
                reply = transport.transact(b"*IDN?" + LSCI_TERMINATOR, read_until=b"\n",
                                           timeout=timeout if attempt == 0
                                           else min(timeout, IDN_RETRY_TIMEOUT_S))
                if reply and on_reply is not None:
                    try:
                        on_reply(bytes(reply))
                    except Exception:  # noqa: BLE001
                        pass
                # Only a finished line identifies anything: "LSCI,M" cut from a 625's
                # answer would otherwise pass as a Lake Shore of model "M".
                ok, model = parse_idn(reply) if is_complete_line(reply) else (False, "")
                if ok:
                    return LakeshoreInfo(model=model, idn=reply.decode("ascii", "replace").strip(),
                                         settings=settings)
                logger.debug("autodetect: %s at %s (try %d): reply %r is not a Lakeshore *IDN?",
                             port, settings.label(), attempt + 1, reply)
        except (SerialUnavailable, Exception) as exc:
            # DEBUG, one line per candidate: a silent probe is how nobody noticed that
            # MAST had written to a laser's port (optical station).
            logger.debug("autodetect: %s at %s: no Lakeshore answer (%s: %s)",
                         port, settings.label(), type(exc).__name__, exc)
        finally:
            if transport is not None:
                transport.close()
    return None


# ── read-only instrument state ──────────────────────────────────────────────
# Decode tables for the Model 33x/335/336 family. Unknown codes fall back to the
# raw number rather than a guess, so a model we haven't verified can't be shown
# a confident-but-wrong label.
_OUTMODE_MODES = {0: tr("关闭", "Off"), 1: tr("闭环 PID", "Closed-loop PID"),
                  2: tr("分段 (Zone)", "Zone"), 3: tr("开环", "Open loop"),
                  4: tr("监视输出", "Monitor out"), 5: tr("升温电源", "Warm-up supply")}
_OUTMODE_INPUTS = {0: tr("无", "None"), 1: "A", 2: "B", 3: "C", 4: "D"}
_RANGE_LABELS = {0: tr("关闭", "Off"), 1: tr("低", "Low"), 2: tr("中", "Medium"), 3: tr("高", "High")}
_SENSOR_TYPES = {0: tr("未启用", "Disabled"), 1: tr("二极管", "Diode"),
                 2: tr("铂电阻 RTD", "Platinum RTD"), 3: tr("NTC 热敏电阻", "NTC thermistor"),
                 4: tr("热电偶", "Thermocouple"), 5: tr("电容", "Capacitance")}
# RDGST? bit flags — a non-zero status means the kelvin number is NOT trustworthy.
_RDGST_FLAGS = ((1, tr("读数无效", "invalid reading")),
                (16, tr("温度低于量程", "temperature below range")),
                (32, tr("温度高于量程", "temperature above range")),
                (64, tr("传感器读数为零", "sensor reads zero")),
                (128, tr("传感器超量程", "sensor over range")))

# The health of one input's reading, as three DISTINCT answers. The third is the
# whole point: "we asked and it is clean" and "we never got an answer" are
# different facts about the world, and only the first of them backs a number.
HEALTH_CLEAN = "clean"      # RDGST? answered; no fault bit set
HEALTH_FAULTY = "faulty"    # RDGST? answered; at least one fault bit set
HEALTH_UNKNOWN = "unknown"  # RDGST? was not asked / did not answer / unparseable


def decode_reading_status(code: int) -> list[str]:
    """Decode a ``RDGST?`` *bitmask* into human-readable faults.

    ``[]`` means "the instrument answered and every fault bit is clear". That is
    a MEASUREMENT, not a default — never manufacture it from a non-answer. Use
    :func:`parse_reading_status`, which keeps "did not answer" as ``None``.

    The argument is a bitmask, which leaves no room for an in-band sentinel: a
    ``-1`` meaning "unknown" decodes as ``-1 & 1 == 1`` → 读数无效 and then every
    other flag too, inventing a faulty sensor the instrument never reported.
    """
    return [label for bit, label in _RDGST_FLAGS if code & bit]


def parse_reading_status(reply: "str | bytes | None") -> "list[str] | None":
    """``RDGST?`` reply text → fault labels, or ``None`` when it never answered.

    This is where the tri-state has to be established, because the reply text is
    the LAST layer at which "no answer" is still distinguishable from "zero".
    Everything below turns a silent link into an empty string without raising —
    pyserial's ``read_until`` returns ``b""`` at timeout, ``transact`` passes it
    through, ``_query`` decodes it to ``""`` — and one line later the difference
    is gone for good: ``int("" or 0)`` is ``0``, and ``0`` is a perfectly valid
    *clean* status word.

    Returns ``None`` — not ``[]``, and not a sentinel bitmask — for an empty
    reply, a non-integer reply, and a negative one (the status word is 0..255; a
    negative cannot be a bitmask and decoding it would invent faults).
    """
    if reply is None:
        return None
    text = (reply.decode("ascii", "replace")
            if isinstance(reply, (bytes, bytearray)) else str(reply)).strip()
    if not text:
        return None
    try:
        code = int(text)
    except (TypeError, ValueError):
        logger.debug("Lakeshore: unparseable RDGST? reply %r — status unknown", text)
        return None
    if code < 0:
        logger.debug("Lakeshore: negative RDGST? reply %r — status unknown", text)
        return None
    return decode_reading_status(code)


def _assert_query(verb: str) -> str:
    """Gatekeeper: only whitelisted queries, and every one must end in '?'.

    Both halves matter. The whitelist stops an unexpected verb; the trailing-'?'
    check is the structural invariant — in the Lakeshore command set the setter
    and the getter differ only by that character (``RANGE 1,3`` drives a heater,
    ``RANGE? 1`` asks about one). A future edit that adds a verb without the '?'
    fails here instead of on the instrument.
    """
    v = str(verb).strip().upper()
    if not v.endswith("?") or v not in _STATE_QUERIES:
        raise LakeshoreError(f"refusing non-query command {verb!r}")
    return v


#: The only argument ``ZONE?`` takes: ``<output 1|2>,<zone 1..10>`` (manual p. 126).
_ZONE_ARG_RE = re.compile(r"[12],(?:[1-9]|10)")


def build_query(verb: str, arg: "str | int | None" = None) -> bytes:
    """The bytes of one whitelisted query, CR LF terminated. Every query this module
    sends is built here; anything else raises :class:`LakeshoreError`.

    The argument is validated strictly rather than coerced: :func:`_sanitize_channel`
    falls back to ``"A"`` on garbage, which is right for a user-supplied channel
    but wrong here — silently rewriting an output number would make the panel
    report output 1's heater state under output 3's heading. Bad argument →
    raise, so the caller degrades that one field to "unknown".

    ``ZONE?`` is the one query whose argument has a comma. It must be exactly
    ``<1|2>,<1..10>``: no spaces, no third field, nothing after it.
    """
    v = _assert_query(verb)
    payload = v
    if v == "ZONE?":
        token = "" if arg is None else str(arg).strip()
        if not _ZONE_ARG_RE.fullmatch(token):
            raise LakeshoreError(f"ZONE? needs '<output 1|2>,<zone 1..10>', got {arg!r}")
        payload = f"{v} {token}"
    elif arg is not None and str(arg).strip() != "":
        token = str(arg).strip()
        if not _CHANNEL_RE.match(token):
            raise LakeshoreError(f"unsafe query argument {arg!r}")
        payload = f"{v} {token}"
    raw = payload.encode("ascii")
    # Structural invariants on the final bytes: one query, no chaining (the 335 runs
    # ';'-separated commands), no embedded terminator, printable ASCII only.
    if b";" in raw or b"\r" in raw or b"\n" in raw or any(c < 0x20 or c > 0x7E for c in raw):
        raise LakeshoreError(f"refusing malformed payload {payload!r}")
    return raw + LSCI_TERMINATOR


def _gap_kwargs(transport: SerialLike) -> dict:
    """``{"min_gap_s": MIN_COMMAND_GAP_S}`` for a transport that spaces messages itself,
    else ``{}``.

    The 335 manual (§6.3.5, p. 104) asks for 50 ms of silence after every message and at
    most 20 messages a second. Only :class:`LakeshoreLink` used to pass the gap; the
    monitor's reads (:meth:`LakeshoreTemperatureSensor.read`) and :func:`read_snapshot`
    went out back to back, and the transport spaces a caller that passes no gap only
    after one that did.
    """
    return {"min_gap_s": MIN_COMMAND_GAP_S} if getattr(transport, "supports_min_gap", False) else {}


def _query(transport: SerialLike, verb: str, arg: str = "", *,
           timeout: float = 0.5) -> str:
    """Send one whitelisted query (:func:`build_query`) and return the trimmed reply."""
    v = _assert_query(verb)
    raw = transport.transact(build_query(v, arg), read_until=b"\n", timeout=timeout,
                             **_gap_kwargs(transport))
    if raw and not is_complete_line(raw):
        raise LakeshoreError(f"{v}: incomplete reply {bytes(raw)!r} (no line end)")
    return raw.decode("ascii", "replace").strip()


def _csv_ints(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        try:
            out.append(int(float(part.strip())))
        except ValueError:
            out.append(-1)
    return out


class ChannelState:
    """Live state of one sensor input, as shown in the settings readout.

    ``faults`` is THREE-state, and the third state is load-bearing:

      * ``[]``     — ``RDGST?`` answered and no fault bit is set. The kelvin
        number carries the instrument's own backing.
      * ``[...]``  — ``RDGST?`` answered with fault bits. The number is not
        trustworthy (an over-range sensor still returns a plausible float).
      * ``None``   — ``RDGST?`` was never asked, or never answered. **Nothing is
        known** about this reading's validity.

    It used to be two-state (``self.faults = faults or []``), which made the
    third case indistinguishable from the first: an input whose status query
    timed out was registered as a healthy thermometer, and its temperature went
    on to satisfy a conduct's 换样品 wait gate. The gate's own ``stale_after_s``
    cannot catch that — the link is alive and the readings are fresh, so
    freshness answers "is this message old?", not "is this number backed?".

    Ask :attr:`health` rather than re-spelling the three-way test per caller.
    """

    __slots__ = ("channel", "label", "kelvin", "celsius", "sensor_value",
                 "sensor_type", "faults")

    def __init__(self, channel: str, label: str = "", kelvin: float | None = None,
                 celsius: float | None = None, sensor_value: float | None = None,
                 sensor_type: str = "", faults: "list[str] | None" = None):
        self.channel = channel
        self.label = label
        self.kelvin = kelvin
        self.celsius = celsius
        self.sensor_value = sensor_value
        self.sensor_type = sensor_type
        # NOT ``faults or []`` — that fold IS the defect. An omitted argument
        # means "nobody asked this channel", which is exactly what None says.
        self.faults: "list[str] | None" = faults

    @property
    def health(self) -> str:
        """:data:`HEALTH_CLEAN` / :data:`HEALTH_FAULTY` / :data:`HEALTH_UNKNOWN`.

        The single definition of the three-way rule. A distinction is only worth
        having if every consumer draws it the same way, and the cheapest way to
        guarantee that is to give it one name and one implementation.
        """
        if self.faults is None:
            return HEALTH_UNKNOWN
        return HEALTH_FAULTY if self.faults else HEALTH_CLEAN

    def as_dict(self) -> dict:
        return {"channel": self.channel, "label": self.label, "kelvin": self.kelvin,
                "celsius": self.celsius, "sensor_value": self.sensor_value,
                "sensor_type": self.sensor_type,
                # ``None`` survives onto the wire as JSON ``null``: null = never
                # asked, [] = asked and clean. Collapsing them here would undo
                # the distinction one layer short of the panel that shows it.
                "faults": None if self.faults is None else list(self.faults),
                # Sent alongside so a client never has to re-derive the verdict.
                # A client-side ``faults ?? []`` reads null as clean — which is
                # precisely the fold this whole change removes.
                "health": self.health}


class OutputState:
    """Live state of one control output — this is the heater readout.

    ``heater_on`` is derived from the RANGE code alone: on this instrument family
    range 0 means the output is off no matter what the setpoint or control mode
    say. A parked setpoint of 500 K with range 0 is harmless, and the panel must
    not read that as "heating".
    """

    __slots__ = ("output", "range_code", "range_label", "heater_pct", "setpoint",
                 "mode", "control_input", "powerup_enabled", "ramping", "ramp_rate")

    def __init__(self, output: int, range_code: int = -1, range_label: str = "",
                 heater_pct: float | None = None, setpoint: float | None = None,
                 mode: str = "", control_input: str = "",
                 powerup_enabled: bool = False, ramping: bool = False,
                 ramp_rate: float | None = None):
        self.output = output
        self.range_code = range_code
        self.range_label = range_label
        self.heater_pct = heater_pct
        self.setpoint = setpoint
        self.mode = mode
        self.control_input = control_input
        self.powerup_enabled = powerup_enabled
        self.ramping = ramping
        self.ramp_rate = ramp_rate

    @property
    def heater_on(self) -> bool:
        return self.range_code > 0

    def as_dict(self) -> dict:
        return {"output": self.output, "range_code": self.range_code,
                "range_label": self.range_label, "heater_on": self.heater_on,
                "heater_pct": self.heater_pct, "setpoint": self.setpoint,
                "mode": self.mode, "control_input": self.control_input,
                "powerup_enabled": self.powerup_enabled, "ramping": self.ramping,
                "ramp_rate": self.ramp_rate}


class LakeshoreSnapshot:
    """Everything MAST can read off the instrument without writing to it."""

    __slots__ = ("model", "idn", "firmware", "serial_number", "port",
                 "channels", "outputs", "settings")

    def __init__(self, model: str = "", idn: str = "", firmware: str = "",
                 serial_number: str = "", port: str = "",
                 channels: list[ChannelState] | None = None,
                 outputs: list[OutputState] | None = None,
                 settings: SerialSettings | None = None):
        self.model = model
        self.idn = idn
        self.firmware = firmware
        self.serial_number = serial_number
        self.port = port
        self.channels = channels or []
        self.outputs = outputs or []
        # Line parameters that actually answered, so "adopt" pins the exact baud
        # instead of making every boot re-run the auto-baud search.
        self.settings = settings

    @property
    def any_heater_on(self) -> bool:
        return any(o.heater_on for o in self.outputs)

    def as_dict(self) -> dict:
        return {"model": self.model, "idn": self.idn, "firmware": self.firmware,
                "serial_number": self.serial_number, "port": self.port,
                "any_heater_on": self.any_heater_on,
                "channels": [c.as_dict() for c in self.channels],
                "outputs": [o.as_dict() for o in self.outputs]}


def default_outputs(model: str) -> list[int]:
    """Control-output numbers for a model (monitors have none)."""
    m = (model or "").upper()
    if any(t in m for t in ("218", "224")):
        return []                       # monitor-only, no heater
    if any(t in m for t in ("336", "350")):
        return [1, 2, 3, 4]
    return [1, 2]                       # 335 / 33x / 340


def _float_or_none(text: str) -> float | None:
    try:
        return parse_reading(text)
    except LakeshoreError:
        return None


#: How long the settings half of a snapshot (input names and types, setpoints, output mode,
#: ramp) may be reused by the instrument-state panel. These change when a person works the
#: front panel, or when MAST writes (every MAST write drops the cache at once,
#: :meth:`LakeshoreLink.mark_sent`); the readings, heater range and heater output are asked
#: every time.
SETTINGS_MAX_AGE_S = 60.0


class SettingsCache:
    """The settings half of the last complete :func:`read_snapshot`, one per transport.

    ``generation`` counts invalidations: a sweep that was running while a write went out
    (the sweep holds no link lock) sees the number changed and does not store what it read.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: "dict | None" = None
        self._at = 0.0
        self.generation = 0

    def fresh(self, max_age_s: float) -> "dict | None":
        with self._lock:
            if self._data is None or time.monotonic() - self._at >= max_age_s:
                return None
            return self._data

    def store(self, generation: int, data: dict) -> None:
        with self._lock:
            if generation == self.generation:
                self._data, self._at = data, time.monotonic()

    def invalidate(self) -> None:
        with self._lock:
            self.generation += 1
            self._data = None


_SETTINGS_BY_TRANSPORT: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_SETTINGS_REGISTRY_LOCK = threading.Lock()


def settings_cache_for(transport: object) -> "SettingsCache | None":
    """The :class:`SettingsCache` of *transport* (shared by every sensor and the link on it);
    ``None`` for an object that cannot be weakly referenced (then nothing is cached)."""
    with _SETTINGS_REGISTRY_LOCK:
        try:
            cache = _SETTINGS_BY_TRANSPORT.get(transport)
            if cache is None:
                cache = _SETTINGS_BY_TRANSPORT[transport] = SettingsCache()
            return cache
        except TypeError:
            return None


def _read_channel_settings(transport: SerialLike, ch: str, state: "ChannelState",
                           timeout: float) -> bool:
    """Ask a channel's name and type; ``True`` when the type (the reply that cannot be
    legitimately empty) was answered."""
    try:
        state.label = _query(transport, "INNAME?", ch, timeout=timeout)
    except Exception:  # noqa: BLE001 - leave the field unset
        pass
    try:
        codes = _csv_ints(_query(transport, "INTYPE?", ch, timeout=timeout))
        if codes:
            state.sensor_type = _SENSOR_TYPES.get(codes[0], str(codes[0]))
        return bool(codes) and codes[0] >= 0
    except Exception:  # noqa: BLE001
        return False


def read_snapshot(transport: SerialLike, *, model: str = "", idn: str = "",
                  port: str = "", channels: list[str] | None = None,
                  timeout: float = 0.5, settings: "SettingsCache | None" = None,
                  settings_max_age_s: float = 0.0) -> LakeshoreSnapshot:
    """Read the full read-only instrument state over an OPEN transport.

    With *settings* and ``settings_max_age_s`` > 0 the settings half (``*IDN?``, input
    name and type, output setpoint / mode / ramp) comes from the last COMPLETE sweep when
    that is younger, so a repeat call asks only the readings, fault bits, heater range and
    heater output (12 queries instead of 23). A sweep with any unanswered settings query
    is not stored. Without *settings* every field is asked, as before.

    Every failure degrades to a missing field rather than raising: a partially
    answered instrument still yields a useful panel, and this is called from a
    settings page that must never 500. Reuses the caller's transport so it can
    run against the port the EnvironmentMonitor already owns.
    """
    reuse = (settings.fresh(settings_max_age_s)
             if settings is not None and settings_max_age_s > 0 else None)
    generation = settings.generation if settings is not None else 0
    if not idn and reuse is not None:
        idn = reuse["idn"]
    if not idn:
        try:
            idn = _query(transport, "*IDN?", timeout=timeout)
        except Exception:  # noqa: BLE001 - degrade to an empty identity
            idn = ""
    parts = [p.strip() for p in idn.split(",")] if idn else []
    if not model and len(parts) > 1:
        model = parts[1]
    snap = LakeshoreSnapshot(
        model=model, idn=idn, port=port,
        serial_number=parts[2] if len(parts) > 2 else "",
        firmware=parts[3] if len(parts) > 3 else "",
    )

    chs = list(channels or default_channels(model))
    outs = default_outputs(model)
    if reuse is not None and (reuse["channels"] != chs or reuse["outputs"] != outs):
        reuse = None                     # asked for other inputs than the cached sweep covered
    kept_ch: dict = {}
    kept_out: dict = {}
    types_answered = True

    for ch in chs:
        state = ChannelState(channel=ch)
        if reuse is not None:
            state.label, state.sensor_type = reuse["ch"][ch]
        else:
            types_answered &= _read_channel_settings(transport, ch, state, timeout)
            kept_ch[ch] = (state.label, state.sensor_type)
        for verb, setter in (
            ("KRDG?", lambda v, s=state: setattr(s, "kelvin", _float_or_none(v))),
            ("CRDG?", lambda v, s=state: setattr(s, "celsius", _float_or_none(v))),
            ("SRDG?", lambda v, s=state: setattr(s, "sensor_value", _float_or_none(v))),
        ):
            try:
                setter(_query(transport, verb, ch, timeout=timeout))
            except Exception:  # noqa: BLE001 - leave the field unset
                pass
        try:
            # parse_reading_status, NOT ``int(reply or 0)``: the ``or 0`` is what
            # turned a timed-out status query into the bitmask 0, i.e. "clean".
            state.faults = parse_reading_status(
                _query(transport, "RDGST?", ch, timeout=timeout))
        except Exception:  # noqa: BLE001
            # The query itself failed (port dropped mid-snapshot, argument
            # rejected, …). Say "unknown" out loud instead of leaning on the
            # constructor default: a premise this load-bearing should not be
            # owned by a default two hundred lines away.
            state.faults = None
        snap.channels.append(state)

    for out in outs:
        o = OutputState(output=out)
        try:
            o.range_code = int(_query(transport, "RANGE?", str(out), timeout=timeout))
            o.range_label = _RANGE_LABELS.get(o.range_code, str(o.range_code))
        except Exception:  # noqa: BLE001 - unknown range → heater_on stays False
            pass
        try:
            o.heater_pct = _float_or_none(_query(transport, "HTR?", str(out), timeout=timeout))
        except Exception:  # noqa: BLE001
            pass
        if reuse is not None:
            (o.setpoint, o.mode, o.control_input, o.powerup_enabled,
             o.ramping, o.ramp_rate) = reuse["out"][out]
            snap.outputs.append(o)
            continue
        try:
            o.setpoint = _float_or_none(_query(transport, "SETP?", str(out), timeout=timeout))
        except Exception:  # noqa: BLE001
            pass
        try:
            codes = _csv_ints(_query(transport, "OUTMODE?", str(out), timeout=timeout))
            if len(codes) > 0:
                o.mode = _OUTMODE_MODES.get(codes[0], str(codes[0]))
            if len(codes) > 1:
                o.control_input = _OUTMODE_INPUTS.get(codes[1], str(codes[1]))
            if len(codes) > 2:
                o.powerup_enabled = bool(codes[2])
        except Exception:  # noqa: BLE001
            pass
        try:
            # "<on/off>,<K/min>" — the rate is a FLOAT (0.1 K/min is a normal
            # ramp), so it must not go through the int decoder used for the
            # enum-valued replies.
            fields = _query(transport, "RAMP?", str(out), timeout=timeout).split(",")
            if fields:
                o.ramping = bool(int(float(fields[0].strip() or 0)))
            if len(fields) > 1:
                o.ramp_rate = _float_or_none(fields[1].strip())
        except Exception:  # noqa: BLE001
            pass
        kept_out[out] = (o.setpoint, o.mode, o.control_input, o.powerup_enabled,
                         o.ramping, o.ramp_rate)
        snap.outputs.append(o)
    if (settings is not None and reuse is None and idn
            and types_answered
            and all(v[0] is not None and v[1] for v in kept_out.values())):
        # Complete sweep (every settings query answered): keep its settings half.
        settings.store(generation, {"idn": idn, "channels": chs, "outputs": outs,
                                    "ch": kept_ch, "out": kept_out})
    return snap


def identify_model(transport: SerialLike, *, timeout: float = 0.5) -> str:
    """One ``*IDN?`` over an open, possibly shared, transport: the model (``MODEL335``)
    or ``""``. Read-only, never raises, never closes the transport. The runtime asks it
    for inputs whose sensors were built from a config entry that did not record the
    model, so temperature control exists right after a restart."""
    try:
        reply = _query(transport, "*IDN?", timeout=timeout)
    except Exception:  # noqa: BLE001 - unknown is "", the caller tries again later
        return ""
    ok, model = parse_idn(reply)        # _query already refused a reply without its line end
    return model.strip().upper() if ok else ""


def discover_lakeshore(port: str, *, candidates=LSCI_SERIAL_CANDIDATES,
                       timeout: float = 0.5) -> "LakeshoreSnapshot | None":
    """Probe *port* and, if a Lakeshore answers, read its full state in one go.

    Used by the "扫描设备接口" flow: the confirmation dialog needs the model AND
    the live per-channel values, so the user can tell at a glance whether the
    identification is right before adopting it. Opens and closes its own
    transport; returns None (never raises) when nothing answers.
    """
    info = probe_lakeshore(port, candidates=candidates, timeout=timeout)
    if info is None:
        return None
    if not is_temperature_model(info.model):
        # A Lake Shore, but not a thermometer (e.g. the Model 625 magnet supply):
        # return the identity only, without sending it a single temperature query.
        return LakeshoreSnapshot(model=info.model, idn=info.idn, port=port,
                                 settings=info.settings)
    transport: SerialTransport | None = None
    try:
        transport = open_transport(info.settings)
        snap = read_snapshot(transport, model=info.model, idn=info.idn,
                             port=port, timeout=timeout)
        snap.settings = info.settings
        return snap
    except Exception as exc:  # noqa: BLE001 - identity is still worth returning
        logger.debug("discover_lakeshore(%s): state read failed: %s", port, exc)
        return LakeshoreSnapshot(model=info.model, idn=info.idn, port=port,
                                 settings=info.settings)
    finally:
        if transport is not None:
            transport.close()


class LakeshoreTemperatureSensor(EnvironmentSensor):
    """One Lakeshore input channel as an :class:`EnvironmentSensor` (kelvin).

    A multi-channel instrument is represented as several sensors with distinct
    names — that satisfies "可能有不止一个温度计 + 用户命名" without any special
    multiplexing in the monitor. Open/transact failures degrade to
    ``unavailable`` / ``error`` rather than crashing.
    """

    def __init__(
        self,
        *,
        name: str = "temperature",
        port: str | None = None,
        channel: str = "A",
        settings: SerialSettings | None = None,
        unit: str = "K",
        alarm: AlarmSpec | None = None,
        timeout: float = 0.5,
        transport: SerialLike | None = None,
        command: str = "KRDG?",
        model: str = "",
    ):
        self._name = name
        # The instrument's model (``MODEL335``) when known: from auto-detection, from an
        # adopted config entry, or learned from the first settings snapshot. Temperature
        # control (core/heater_control) is offered only for a known Model 335.
        self.model = str(model or "").strip()
        self._channel = _sanitize_channel(channel)
        self._alarm = alarm or AlarmSpec()
        self._timeout = timeout
        self._command = _sanitize_command(command)
        # If the caller left the default "K" but the command reads Celsius /
        # sensor units, label it correctly; an explicit non-K unit is honoured.
        if unit == "K" and self._command != "KRDG?":
            self._unit = _UNIT_BY_COMMAND.get(self._command, "K")
        else:
            self._unit = unit
        self._transport: SerialLike | None = transport
        if settings is not None:
            self._settings = settings
        elif port is not None:
            self._settings = SerialSettings(
                port=port, baudrate=57600, bytesize=7, parity="O",
                stopbits=1, timeout=timeout)
        else:
            self._settings = None
        self._query = f"{self._command} {self._channel}".encode("ascii") + LSCI_TERMINATOR

    def name(self) -> str:
        return self._name

    @property
    def transport(self) -> "SerialLike | None":
        """The (possibly shared) transport this input reads over; ``None`` before first use
        when the sensor was built from settings alone."""
        return self._transport

    def _ensure_transport(self) -> SerialLike:
        if self._transport is None:
            if self._settings is None:
                raise SerialUnavailable("no port configured")
            self._transport = open_transport(self._settings)
        return self._transport

    def _release_transport(self) -> None:
        """Close the OS handle without discarding a possibly-shared transport."""
        close = getattr(self._transport, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - best-effort
                pass

    def read(self) -> SensorReading:
        try:
            transport = self._ensure_transport()
        except SerialUnavailable:
            return SensorReading(value=0.0, unit=self._unit, status="unavailable")
        try:
            reply = transport.transact(self._query, read_until=b"\n",
                                       timeout=self._timeout, **_gap_kwargs(transport))
        except SerialUnavailable:
            # Drop the OS handle so a later read reconnects, but KEEP the
            # transport object: it is shared with the instrument's other inputs
            # (autodetect._shared_transport). Replacing it with a fresh one here
            # would open a second exclusive handle on a port a sibling sensor is
            # already holding, and that input would never recover.
            self._release_transport()
            return SensorReading(value=0.0, unit=self._unit, status="unavailable")
        if reply and not is_complete_line(reply):
            # "+29" cut from "+293.15E+0" is a plausible-looking wrong temperature.
            logger.debug("Lakeshore %s: incomplete reply %r", self._name, reply)
            return SensorReading(value=0.0, unit=self._unit, status="error")
        try:
            kelvin = parse_reading(reply)
        except LakeshoreError as exc:
            logger.debug("Lakeshore %s parse error: %s", self._name, exc)
            return SensorReading(value=0.0, unit=self._unit, status="error")
        status = self._alarm.evaluate(kelvin, base_status="ok")
        return SensorReading(value=kelvin, unit=self._unit, status=status)

    def instrument_state(self, *, channels: list[str] | None = None
                         ) -> "LakeshoreSnapshot | None":
        """Full read-only instrument state over THIS sensor's open port.

        Reuses the sensor's own transport instead of opening a second handle —
        Windows COM ports are exclusive, so a settings panel that opened its own
        connection could never read an instrument the monitor is already
        polling. Serialisation against the monitor's 2 s poll is handled by the
        transport's I/O lock. Returns None if the port isn't usable.
        """
        try:
            transport = self._ensure_transport()
        except SerialUnavailable:
            return None
        port = self._settings.port if self._settings is not None else ""
        try:
            snap = read_snapshot(transport, port=port, channels=channels,
                                 timeout=self._timeout,
                                 settings=settings_cache_for(transport),
                                 settings_max_age_s=SETTINGS_MAX_AGE_S)
        except Exception as exc:  # noqa: BLE001 - panel must never 500
            logger.debug("instrument_state on %s failed: %s", self._name, exc)
            return None
        if snap is not None and snap.model and not self.model:
            self.model = str(snap.model).strip()     # learned from *IDN?
        return snap

    def close(self) -> None:
        """Release the OS handle. KEEPS the transport object.

        Discarding it here (``self._transport = None``) is what reintroduced
        the shared-port bug that commit 0000000 had fixed. The transport is SHARED
        with this instrument's other inputs — one Lake Shore 335 on COM13 serves
        both ``SPM`` and ``Magnet``, and a Windows COM port admits exactly one
        handle. Dropping the reference means the next read calls
        ``_ensure_transport``, which opens a *private* handle from ``_settings``;
        the sibling input then can never get the port back, and the two channels
        resume the anti-phase alternation, and readings for whichever one loses the
        handle stop being recorded.

        The path this runs on is not exotic: ``EnvironmentMonitor.stop()`` →
        ``_close_sensors()``, reached every time the operator presses
        「扫描设备接口」（``/api/environment/discover`` releases the bus and then
        restarts the SAME sensor objects). ``rescan`` / ``adopt`` happened to
        survive only because they rebuild the sensor list with a fresh pool.

        ``read()``'s own failure path already got this right — see
        ``_release_transport``. The two must agree, and a test now pins that.

        Reopening still works: ``SerialTransport.close()`` drops the pyserial
        object and ``transact()`` reopens on next use.
        """
        self._release_transport()


# ── identity-checked link (for temperature control) ─────────────────────────
#: Silence the manual asks for around every message: "Guarantee that no other
#: communication is started for 50 ms after the last character is transmitted ... Not
#: initiate communication more than 20 times/s" (Model 335 manual §6.3.5, p. 104).
MIN_COMMAND_GAP_S = 0.06


class WrongController(LakeshoreError):
    """The port answered ``*IDN?``, but not as the controller the link was made for.
    Nothing else was sent on that handle."""


class LakeshoreLink:
    """One Lake Shore controller on one port, for a driver that must know WHICH
    instrument it addresses before every command: temperature control
    (``mast.instruments.heater_335``). The temperature sensors keep polling the same
    shared transport directly, as before.

    * Identity first. Every freshly opened handle is asked ``*IDN?`` and must answer
      ``LSCI,<expected model>``. The serial number of the first answer is pinned; a
      later handle that answers with another serial is refused. A COM name is not an
      identity: COM numbers of two instruments can swap when USB adapters are
      replugged.
    * Every exchange after that is pinned to the verified handle generation
      (``transact(expect_generation=...)``), so a reopened port is re-identified, not
      trusted.
    * Messages are spaced by :data:`MIN_COMMAND_GAP_S` on the transport itself
      (``transact(min_gap_s=...)``), so the monitor's polls on the same port keep that
      distance from these as well.

    It builds query bytes only (:func:`build_query`). The setters live in
    ``mast.instruments.heater_335``, which writes through :meth:`session`.
    """

    def __init__(self, transport: SerialLike, *, port: str = "",
                 expected_model: str = "MODEL335", timeout: float = 0.6,
                 min_gap_s: float = MIN_COMMAND_GAP_S, pinned_serial: str = ""):
        self._t = transport
        self.port = port or str(getattr(getattr(transport, "settings", None), "port", "") or "")
        self.expected_model = str(expected_model).strip().upper()
        self._timeout = float(timeout)
        self._gap = max(0.0, float(min_gap_s))
        self._lock = threading.RLock()
        self._last_tx = 0.0
        self._verified_gen: "int | None" = None
        self._verified_once = False
        # Carried over by the runtime when it builds a new link for the same port (after a
        # rescan), so the pin outlives one controller object.
        self.pinned_serial: str = str(pinned_serial or "")
        self.identity: dict = {}
        self.identity_error: str = ""

    # ── transport plumbing ──
    @property
    def transport(self) -> SerialLike:
        return self._t

    def _pinned(self) -> bool:
        return bool(getattr(self._t, "supports_generations", False))

    def gap_kwargs(self) -> dict:
        """``{"min_gap_s": ...}`` when the transport spaces messages itself, else ``{}``."""
        return {"min_gap_s": self._gap} if getattr(self._t, "supports_min_gap", False) else {}

    def _space(self) -> None:
        if getattr(self._t, "supports_min_gap", False):
            return                       # the transport keeps the distance (all callers)
        wait = self._last_tx + self._gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _exchange(self, payload: bytes, *, pin: "int | None") -> str:
        self._space()
        try:
            kwargs: dict = {"read_until": b"\n", "timeout": self._timeout, **self.gap_kwargs()}
            if pin is not None:
                kwargs["expect_generation"] = pin
            raw = self._t.transact(payload, **kwargs)
        finally:
            self._last_tx = time.monotonic()
        raw = bytes(raw or b"")
        if raw and not is_complete_line(raw):
            raise LakeshoreError(f"{payload.decode('ascii', 'replace').strip()}: "
                                 f"incomplete reply {raw!r} (no line end)")
        return raw.decode("ascii", "replace").strip()

    def _identity_current(self) -> bool:
        if not self._pinned():
            return self._verified_once
        if self._verified_gen is None:
            return False
        return bool(getattr(self._t, "is_open", False)) and \
            getattr(self._t, "open_generation", None) == self._verified_gen

    def _verify_identity(self) -> None:
        """Ask ``*IDN?`` on the (re)opened handle; refuse anything but the pinned unit."""
        self._verified_gen = None
        self._verified_once = False
        was_open = bool(getattr(self._t, "is_open", False))
        gen_before = getattr(self._t, "open_generation", None)
        reply = self._exchange(build_query("*IDN?"), pin=None)
        gen_after = getattr(self._t, "open_generation", None)
        if self._pinned() and was_open and gen_after != gen_before:
            # The open handle was replaced by another thread around the exchange (a closed
            # port reopening for this very query is expected and fine): the answer may
            # belong to a handle that no longer exists. Verify again on the next use.
            raise SerialUnavailable(f"{self.port}: handle reopened during the identity check")
        parts = [p.strip() for p in reply.split(",")]
        ok = (len(parts) >= 3 and parts[0].upper() == "LSCI"
              and parts[1].upper() == self.expected_model and bool(parts[2]))
        serial = parts[2] if ok else ""
        if ok and self.pinned_serial and serial != self.pinned_serial:
            ok = False
            why = f"serial {serial!r}, not the pinned {self.pinned_serial!r}"
        else:
            why = f"not LSCI,{self.expected_model}"
        if not ok:
            self.identity_error = f"{self.port}: *IDN? answered {reply!r} ({why})"
            try:
                self._t.close()          # never poll a port we cannot vouch for
            except Exception:  # noqa: BLE001
                pass
            raise WrongController(self.identity_error)
        self.pinned_serial = self.pinned_serial or serial
        self.identity = {"model": parts[1], "serial": serial,
                         "firmware": parts[3] if len(parts) > 3 else ""}
        self.identity_error = ""
        self._verified_once = True
        if self._pinned():
            self._verified_gen = getattr(self._t, "open_generation", None)

    def query(self, verb: str, arg: "str | int | None" = None) -> str:
        """One whitelisted query on an identity-verified handle; the reply text.

        Raises :class:`SerialUnavailable` (port), :class:`WrongController` (identity) or
        :class:`LakeshoreError` (refused, cut short, or no reply).
        """
        payload = build_query(verb, arg)          # refuse BEFORE touching the port
        with self._lock:
            if not self._identity_current():
                self._verify_identity()
            reply = self._exchange(payload, pin=self._verified_gen if self._pinned() else None)
        if not reply:
            raise LakeshoreError(f"{verb}: no reply within {self._timeout:.1f}s")
        return reply

    @contextlib.contextmanager
    def session(self, timeout: "float | None" = None):
        """Hold the link with the identity checked on the open handle. Yields the
        verified open generation (``None`` on a transport without generations) for
        ``transact(expect_generation=...)``. No query of this link runs inside the block
        from another thread; the monitor's own polls still interleave, spaced. Raises like
        :meth:`query`; :class:`SerialUnavailable` when the lock is not free in *timeout* s.
        """
        got = (self._lock.acquire() if timeout is None
               else self._lock.acquire(timeout=max(0.0, float(timeout))))
        if not got:
            raise SerialUnavailable(f"{self.port}: link busy for {timeout} s")
        try:
            if not self._identity_current():
                self._verify_identity()
            yield self._verified_gen if self._pinned() else None
        finally:
            self._lock.release()

    def wait_gap(self) -> None:
        """Sleep until the next message may go out (no-op when the transport spaces)."""
        self._space()

    def mark_sent(self) -> None:
        """A command just went out on this link: restart the spacing clock, and drop the
        cached instrument-state settings (the panel must not show what was just changed)."""
        self._last_tx = time.monotonic()
        cache = settings_cache_for(self._t)
        if cache is not None:
            cache.invalidate()


__all__ = [
    "build_query",
    "identify_model",
    "FORBIDDEN_QUERIES",
    "LakeshoreLink",
    "MIN_COMMAND_GAP_S",
    "WrongController",
    "parse_idn",
    "parse_reading",
    "is_temperature_model",
    "default_channels",
    "default_outputs",
    "decode_reading_status",
    "parse_reading_status",
    "HEALTH_CLEAN",
    "HEALTH_FAULTY",
    "HEALTH_UNKNOWN",
    "probe_lakeshore",
    "discover_lakeshore",
    "read_snapshot",
    "SETTINGS_MAX_AGE_S",
    "SettingsCache",
    "settings_cache_for",
    "ChannelState",
    "OutputState",
    "LakeshoreSnapshot",
    "LakeshoreInfo",
    "LakeshoreTemperatureSensor",
    "LakeshoreError",
    "LSCI_SERIAL_CANDIDATES",
]
