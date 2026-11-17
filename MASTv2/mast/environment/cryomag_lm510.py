"""Cryomagnetics LM-510 liquid-cryogen level monitor — READ-ONLY driver.

MAST reads the LM-510 and never commands it. In particular it never sends
``MEAS`` (without the ``?``): on a liquid-helium channel that starts a reading,
i.e. drives current through the superconducting sensor filament (plus an
optional boost pulse), which boils off helium. ``MEAS?`` — the only reading
query used here — "returns latest reading ... If a reading for the selected
channel is in progress, the previous reading is returned" (manual §5.7). So how
often the level is refreshed is decided at the instrument (Mode: Sample/Hold +
interval, Continuous, or Off), by the operator, not by MAST's poll rate.

Not sent either: ``MODE``/``INTVL``/``UNITS``/``BOOST``/``CHAN`` (change what or how
the unit measures), ``CTRL`` (starts an automated refill: closes the control
relay), alarm/threshold setters, ``*RST`` ("terminates any control (refill)
sequence in progress"), ``REMOTE``/``RWLOCK`` (lock the front panel), ``ERROR``,
``*ESR?`` (clears the event register when read) and ``*TST?``.

Parameters without a channel argument (``UNITS?``, ``MODE?``, ``INTVL?``, ...)
refer to the "default channel" chosen by ``CHAN`` (channel 1 after power-up).
MAST never sends ``CHAN``; it reports ``CHAN?`` alongside so a reader knows which
channel those settings describe. ``MEAS?``, ``TYPE?`` and ``CTRL?`` take the
channel explicitly.

Interface (manual §5): USB is an FTDI virtual COM port, 8N1, baud set on the
front panel (9600/19200/38400/57600/115200, factory 9600). Only one interface is
active at a time (Setup → Interface: USB / LAN / GPIB / RS-232). Over USB the unit
**echoes** every command, followed by a line end when processing completes; the
answer follows on its own line::

    *IDN?<CR><LF>
    Cryomagnetics,LM-510,2002,2.00<CR><LF>

so one exchange reads two LF-terminated lines and drops the echo.

The terminator is LF. The manual says commands end in ``<RETURN>``, but the
vendor's own LabVIEW driver (``LM510_17-03-14 Released``: ``LM-510 Write.vi``
"Append line feed"; ``LM-510 Initialize.vi`` "End of Line is Line Feed", 1 s
timeout; "If serial interface then read command echo") sends LF — and that is
what this driver follows. (Its setup VI also sends ``*CLS;ERROR 0; REMOTE``; MAST
sends none of those — ``REMOTE`` locks the front panel.)

Status: the echo format above is from the manual and the vendor VIs, not yet
confirmed on the wire. The identity check means a silent or different device
never receives anything but ``*IDN?``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from dataclasses import field as dc_field

from mast.core.types import SensorReading
from mast.environment.alarm import AlarmSpec
from mast.environment.base import EnvironmentSensor
from mast.environment.serial_transport import (
    SerialLike,
    SerialSettings,
    SerialTransport,
    SerialUnavailable,
    open_transport,
)
from mast.i18n import tr

logger = logging.getLogger(__name__)

TERMINATOR = b"\n"   # LF, as the vendor's LabVIEW driver sends (see module docstring)
IDN_MANUFACTURER = "CRYOMAGNETICS"
IDN_MODEL = "LM-510"

#: USB/RS-232 line settings tried when identifying an LM-510 (8N1; factory 9600 first).
LM510_SERIAL_CANDIDATES: tuple[tuple[int, int, str, float], ...] = (
    (9600, 8, "N", 1),
    (115200, 8, "N", 1),
    (57600, 8, "N", 1),
    (38400, 8, "N", 1),
    (19200, 8, "N", 1),
)
SUPPORTED_BAUD = frozenset(b for b, *_ in LM510_SERIAL_CANDIDATES)

#: Every command MAST may ever write to an LM-510. Exact strings; all end in "?".
QUERIES: frozenset[str] = frozenset({
    "*IDN?",   # identity
    "*STB?",   # status byte (data-ready bits per channel)
    "MEAS?",   # LAST COMPLETED reading of a channel - does not start one
    "STAT?",   # detailed status per channel + menu mode
    "TYPE?",   # 0 = LHe sensor, 1 = LN2 sensor
    "CTRL?",   # refill status / elapsed fill time
    "CHAN?",   # which channel the channel-less queries below refer to
    "UNITS?",  # cm / in / %
    "MODE?",   # Sample/Hold, Continuous, Off
    "INTVL?",  # sample interval HH:MM:SS
    "LNGTH?",  # active sensor length
    "BOOST?",  # Off / On / Smart
    "L-ALM?", "H-ALM?",   # alarm thresholds
    "LOW?", "HIGH?",      # refill control thresholds
    "ERROR?",  # error-message mode of the serial interface
})

#: Queries that take a channel argument (1 or 2; omitted = default channel).
_CHANNEL_QUERIES = frozenset({"MEAS?", "TYPE?", "CTRL?"})
_CHANNELS = frozenset({"1", "2"})

#: Absent on purpose (see module docstring). ``MEAS`` is listed so a test can pin it.
FORBIDDEN_COMMANDS: frozenset[str] = frozenset({
    "MEAS", "CHAN", "CTRL", "MODE", "INTVL", "UNITS", "BOOST", "LOW", "HIGH",
    "L-ALM", "H-ALM", "ERROR", "REMOTE", "RWLOCK", "LOCAL", "*RST", "*CLS",
    "*ESE", "*SRE", "*OPC", "*WAI", "*ESR?", "*TST?", "CAPHI", "CAPLO",
})

MIN_COMMAND_GAP_S = 0.05

_LEVEL_RE = re.compile(r"^\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([A-Za-z%]*)\s*$")


class LM510Error(ValueError):
    """Refused or unparseable exchange with an LM-510."""


class WrongInstrument(LM510Error):
    """The port answered ``*IDN?`` but not as an LM-510. Nothing else was sent."""


def build_query(verb: str, channel: "str | int | None" = None) -> bytes:
    """The ONLY way to produce bytes for the LM-510: a whitelisted query + CR."""
    v = str(verb).strip().upper()
    if v not in QUERIES or not v.endswith("?"):
        raise LM510Error(f"refusing non-whitelisted command {verb!r}")
    if channel is None or str(channel).strip() == "":
        payload = v
    else:
        token = str(channel).strip()
        if v not in _CHANNEL_QUERIES or token not in _CHANNELS:
            raise LM510Error(f"refusing argument {channel!r} for {v}")
        payload = f"{v} {token}"
    raw = payload.encode("ascii")
    if b";" in raw or b"\r" in raw or b"\n" in raw or any(c < 0x20 or c > 0x7E for c in raw):
        raise LM510Error(f"refusing malformed payload {payload!r}")
    return raw + TERMINATOR


def parse_idn(raw: "bytes | str") -> tuple[bool, dict]:
    """``(is_lm510, fields)`` from a ``*IDN?`` answer line."""
    text = raw.decode("ascii", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    parts = [p.strip() for p in text.strip().split(",")]
    ok = (len(parts) >= 2 and parts[0].upper() == IDN_MANUFACTURER
          and parts[1].upper() == IDN_MODEL)
    return ok, {"idn": text.strip(), "model": parts[1] if len(parts) > 1 else "",
                "serial_number": parts[2] if len(parts) > 2 else "",
                "firmware": parts[3] if len(parts) > 3 else ""}


def answer_from(raw: "bytes | str", sent: "bytes | str") -> str:
    """Drop the USB echo (and blank lines) and return the answer line, or ``""``.

    Robust to the unit sending the echo with or without a terminator and to the
    LAN interface, which does not echo at all.
    """
    text = raw.decode("ascii", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    cmd = (sent.decode("ascii", "replace") if isinstance(sent, (bytes, bytearray))
           else str(sent)).strip().upper()
    # Only lines the unit finished count. A reply cut off mid-line ("27.1 c" of
    # "27.1 cm", "2" of "27.1 cm") must not be read as a level.
    if text and not text.endswith(("\n", "\r")):
        cut = max(text.rfind("\n"), text.rfind("\r"))
        text = text[:cut + 1] if cut >= 0 else ""
    for line in re.split(r"\r\n|\r|\n", text):
        s = line.strip()
        if not s or s.upper() == cmd:
            continue
        return s
    return ""


def parse_level(text: str) -> tuple[float, str]:
    """``"27.1 cm"`` -> ``(27.1, "cm")``. Units: cm / in / %. Raises on anything else."""
    m = _LEVEL_RE.match(str(text))
    if not m:
        raise LM510Error(f"not a level reading: {text!r}")
    unit = (m.group(2) or "").strip()
    unit = {"CM": "cm", "IN": "in", "PERCENT": "%"}.get(unit.upper(), unit)
    return float(m.group(1)), unit


#: STAT? channel bits (manual §5.7, bit 0 first).
STAT_BITS = ((0, tr("读数进行中", "read in progress")),
             (1, tr("补液控制激活", "control (refill) active")),
             (2, tr("补液超时", "ctrl timeout occurred")),
             (3, tr("补液控制被禁止(Ctrl Mode=Off 或超时)", "control inhibited (Ctrl Mode off or timeout)")),
             (4, tr("超过报警限", "alarm limit exceeded")),
             (5, tr("探头开路", "open sensor detected")),
             (6, tr("探头烧断", "burnout detected")))
_BIT_OPEN, _BIT_BURNOUT, _BIT_ALARM, _BIT_READING = 5, 6, 4, 0


def decode_stat(code: "int | None") -> "list[str] | None":
    if code is None or code < 0:
        return None
    return [label for bit, label in STAT_BITS if code & (1 << bit)]


def _int_or_none(text: "str | None") -> "int | None":
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return None


@dataclass
class LM510State:
    """Everything MAST reads off an LM-510. ``None`` = not answered (unknown)."""

    port: str = ""
    idn: str = ""
    model: str = ""
    serial_number: str = ""
    firmware: str = ""
    read_at: float = 0.0
    channel: int = 1
    level: "float | None" = None
    unit: str = ""
    channel_type_code: "int | None" = None    # 0 LHe, 1 LN2
    status_code: "int | None" = None          # STAT? field for this channel
    menu_open: "bool | None" = None           # STAT? third field
    control: str = ""                          # CTRL?: Off / Timeout / "15 min"
    default_channel: "int | None" = None       # CHAN?
    sample_mode: str = ""                      # MODE? (default channel)
    sample_interval: str = ""                  # INTVL? (default channel)
    sensor_length: str = ""                    # LNGTH?
    boost: str = ""
    low_alarm: str = ""
    high_alarm: str = ""
    ctrl_low: str = ""
    ctrl_high: str = ""
    unanswered: list[str] = dc_field(default_factory=list)

    @property
    def status_flags(self) -> "list[str] | None":
        return decode_stat(self.status_code)

    @property
    def sensor_fault(self) -> "bool | None":
        c = self.status_code
        return None if c is None or c < 0 else bool(c & ((1 << _BIT_OPEN) | (1 << _BIT_BURNOUT)))

    @property
    def updates_by_itself(self) -> "bool | None":
        """False when the channel's sample mode is Off: the level never refreshes."""
        m = (self.sample_mode or "").strip().lower()
        if not m:
            return None
        return not m.startswith("off")

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d.update({
            "status_flags": self.status_flags,
            "sensor_fault": self.sensor_fault,
            "updates_by_itself": self.updates_by_itself,
            "channel_type": ({0: "LHe", 1: "LN2"}.get(self.channel_type_code, "")
                             if self.channel_type_code is not None else None),
        })
        return d


class LM510Link:
    """One LM-510 on one port; shared by its sensors. All traffic via :meth:`query`."""

    def __init__(self, transport: SerialLike, *, port: str = "", timeout: float = 0.8,
                 min_gap_s: float = MIN_COMMAND_GAP_S):
        self._t = transport
        self.port = port or str(getattr(getattr(transport, "settings", None), "port", "") or "")
        self._timeout = float(timeout)
        self._gap = max(0.0, float(min_gap_s))
        self._lock = threading.RLock()
        self._last_tx = 0.0
        self._verified_gen: "int | None" = None
        self._verified_once = False
        self.identity: dict = {}
        self.identity_error = ""
        self._full: "LM510State | None" = None

    @property
    def transport(self) -> SerialLike:
        return self._t

    def _pinned(self) -> bool:
        return bool(getattr(self._t, "supports_generations", False))

    def _exchange(self, payload: bytes, *, pin: "int | None") -> str:
        wait = self._last_tx + self._gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            kwargs = {"read_until": b"\n", "timeout": self._timeout, "lines": 2}
            if pin is not None:
                kwargs["expect_generation"] = pin
            raw = self._t.transact(payload, **kwargs)
        finally:
            self._last_tx = time.monotonic()
        return answer_from(raw or b"", payload)

    def _identity_current(self) -> bool:
        if not self._pinned():
            return self._verified_once
        if self._verified_gen is None:
            return False
        return bool(getattr(self._t, "is_open", False)) and \
            getattr(self._t, "open_generation", None) == self._verified_gen

    def _verify_identity(self) -> None:
        self._verified_gen = None
        reply = self._exchange(build_query("*IDN?"), pin=None)
        ok, fields = parse_idn(reply)
        if not ok:
            self._verified_once = False
            self.identity_error = (f"{self.port}: *IDN? answered {reply!r}, not "
                                   f"Cryomagnetics,{IDN_MODEL}")
            try:
                self._t.close()
            except Exception:  # noqa: BLE001
                pass
            raise WrongInstrument(self.identity_error)
        self.identity = fields
        self.identity_error = ""
        self._verified_once = True
        if self._pinned():
            self._verified_gen = getattr(self._t, "open_generation", None)

    def query(self, verb: str, channel: "str | int | None" = None) -> str:
        payload = build_query(verb, channel)     # refuse BEFORE touching the port
        with self._lock:
            if not self._identity_current():
                self._verify_identity()
            reply = self._exchange(payload, pin=self._verified_gen if self._pinned() else None)
        if not reply:
            raise LM510Error(f"{verb}: no answer within {self._timeout:.1f}s")
        return reply

    def close(self) -> None:
        with self._lock:
            self._verified_gen = None
            self._verified_once = False
        close = getattr(self._t, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover
                pass

    def read_level(self, channel: int = 1) -> tuple[float, str, "int | None", "bool | None"]:
        """``(level, unit, channel status code, menu open)`` — two queries."""
        ch = str(int(channel))
        with self._lock:
            value, unit = parse_level(self.query("MEAS?", ch))
            status = None
            menu = None
            try:
                parts = [p.strip() for p in self.query("STAT?").split(",")]
                idx = 0 if ch == "1" else 1
                status = _int_or_none(parts[idx]) if len(parts) > idx else None
                menu = bool(_int_or_none(parts[2])) if len(parts) > 2 and _int_or_none(parts[2]) is not None else None
            except LM510Error:
                pass
            return value, unit, status, menu

    def read_state(self, channel: int = 1, max_age_s: float = 0.0) -> LM510State:
        """Full read-only snapshot of one channel (about 16 queries)."""
        with self._lock:
            if (self._full is not None and max_age_s > 0 and self._full.channel == channel
                    and time.time() - self._full.read_at < max_age_s):
                return self._full
            st = LM510State(port=self.port, channel=int(channel))
            ch = str(int(channel))

            def ask(verb: str, arg=None) -> "str | None":
                try:
                    return self.query(verb, arg)
                except LM510Error as exc:
                    if isinstance(exc, WrongInstrument):
                        raise
                    st.unanswered.append(verb)
                    return None

            ask("*STB?")
            st.idn = self.identity.get("idn", "")
            st.model = self.identity.get("model", "")
            st.serial_number = self.identity.get("serial_number", "")
            st.firmware = self.identity.get("firmware", "")
            meas = ask("MEAS?", ch)
            if meas is not None:
                try:
                    st.level, st.unit = parse_level(meas)
                except LM510Error:
                    st.unanswered.append("MEAS?")
            st.channel_type_code = _int_or_none(ask("TYPE?", ch))
            stat = ask("STAT?")
            if stat is not None:
                parts = [p.strip() for p in stat.split(",")]
                idx = 0 if ch == "1" else 1
                st.status_code = _int_or_none(parts[idx]) if len(parts) > idx else None
                if len(parts) > 2 and _int_or_none(parts[2]) is not None:
                    st.menu_open = bool(_int_or_none(parts[2]))
            st.control = ask("CTRL?", ch) or ""
            st.default_channel = _int_or_none(ask("CHAN?"))
            st.sample_mode = ask("MODE?") or ""
            st.sample_interval = ask("INTVL?") or ""
            st.sensor_length = ask("LNGTH?") or ""
            st.boost = ask("BOOST?") or ""
            st.low_alarm = ask("L-ALM?") or ""
            st.high_alarm = ask("H-ALM?") or ""
            st.ctrl_low = ask("LOW?") or ""
            st.ctrl_high = ask("HIGH?") or ""
            st.read_at = time.time()
            self._full = st
            return st


def probe_lm510(port: str, *, candidates=LM510_SERIAL_CANDIDATES, timeout: float = 0.8,
                on_reply=None) -> "tuple[SerialSettings, dict] | None":
    """Identify an LM-510 on *port*. Writes a bare LF, then ``*IDN?`` LF only. Never raises.

    The bare line end first flushes whatever an earlier probe at another line
    setting left in the unit's command buffer (an empty command is a no-op).
    """
    for baud, bits, parity, stop in candidates:
        settings = SerialSettings(port=port, baudrate=baud, bytesize=bits,
                                  parity=parity, stopbits=stop, timeout=timeout)
        transport: "SerialTransport | None" = None
        try:
            transport = open_transport(settings)
            flush = transport.transact(TERMINATOR, read_until=b"\n", timeout=0.2)
            payload = build_query("*IDN?")
            raw = transport.transact(payload, read_until=b"\n", timeout=timeout, lines=2)
            for heard in (flush, raw):
                if heard and on_reply is not None:
                    try:
                        on_reply(bytes(heard))
                    except Exception:  # noqa: BLE001
                        pass
            ok, fields = parse_idn(answer_from(raw or b"", payload))
            if ok:
                return settings, fields
        except Exception as exc:  # noqa: BLE001 - probing is best-effort
            logger.debug("probe_lm510 %s at %s: %s", port, settings.label(), exc)
        finally:
            if transport is not None:
                transport.close()
    return None


class LM510LevelSensor(EnvironmentSensor):
    """One LM-510 channel's liquid level as an environment reading (read-only).

    Polls ``MEAS?`` (last completed reading) and ``STAT?`` at most every
    ``min_interval_s`` (default 10 s); in between it returns the cached reading.

    Status: ``ok``; ``error`` when the unit reports an open or burnt-out sensor
    (the number is not a level); ``warning`` when the unit's own alarm limit is
    exceeded; ``unavailable`` when the port or the instrument does not answer
    (including while an operator has the front-panel menu open, during which
    ``MEAS?`` is blocked). Never ``alarm`` unless the operator configured a band.
    """

    counts_as_real = True
    quiet_gated = False

    def __init__(self, link: LM510Link, *, name: str = "helium_level", channel: int = 1,
                 alarm: "AlarmSpec | None" = None, min_interval_s: float = 10.0):
        if int(channel) not in (1, 2):
            raise ValueError(f"LM510LevelSensor: channel must be 1 or 2, not {channel!r}")
        self._link = link
        self._name = str(name)
        self._channel = int(channel)
        self._alarm = alarm or AlarmSpec()
        self._min_interval = max(1.0, float(min_interval_s))
        self._cached: "tuple[float, SensorReading] | None" = None
        self._unit = "cm"
        self.last_problem = ""

    def name(self) -> str:
        return self._name

    @property
    def link(self) -> LM510Link:
        return self._link

    @property
    def channel(self) -> int:
        return self._channel

    def read(self) -> SensorReading:
        now = time.monotonic()
        if self._cached is not None and now - self._cached[0] < self._min_interval:
            return self._cached[1]
        reading = self._read_now()
        self._cached = (now, reading)
        return reading

    def _read_now(self) -> SensorReading:
        try:
            value, unit, code, menu = self._link.read_level(self._channel)
        except WrongInstrument as exc:
            # WARNING once per distinct problem, DEBUG while it persists (a silent
            # or wrong device on a pinned port would otherwise warn every poll).
            level = logging.DEBUG if str(exc) == self.last_problem else logging.WARNING
            self.last_problem = str(exc)
            logger.log(level, "LM-510 sensor %s: %s — not polling this port", self._name, exc)
            return SensorReading(value=0.0, unit=self._unit, status="unavailable")
        except SerialUnavailable as exc:
            self.last_problem = str(exc)
            self._link.close()
            return SensorReading(value=0.0, unit=self._unit, status="unavailable")
        except LM510Error as exc:
            self.last_problem = str(exc)
            return SensorReading(value=0.0, unit=self._unit, status="unavailable")
        self._unit = unit or self._unit
        flags = decode_stat(code) or []
        self.last_problem = "; ".join(flags)
        if code is not None and code & ((1 << _BIT_OPEN) | (1 << _BIT_BURNOUT)):
            return SensorReading(value=value, unit=self._unit, status="error")
        base = "warning" if (code is not None and code & (1 << _BIT_ALARM)) else "ok"
        return SensorReading(value=value, unit=self._unit,
                             status=self._alarm.evaluate(value, base_status=base))

    def instrument_state(self) -> "LM510State | None":
        try:
            return self._link.read_state(self._channel, max_age_s=10.0)
        except Exception as exc:  # noqa: BLE001 - panel must never 500
            logger.debug("LM-510 instrument_state on %s failed: %s", self._name, exc)
            return None

    def close(self) -> None:
        self._link.close()


__all__ = [
    "QUERIES",
    "FORBIDDEN_COMMANDS",
    "LM510_SERIAL_CANDIDATES",
    "SUPPORTED_BAUD",
    "LM510Error",
    "WrongInstrument",
    "build_query",
    "parse_idn",
    "parse_level",
    "answer_from",
    "decode_stat",
    "LM510State",
    "LM510Link",
    "LM510LevelSensor",
    "probe_lm510",
]
