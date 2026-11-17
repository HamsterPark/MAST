"""Lake Shore Model 625 superconducting-magnet power supply — READ-ONLY driver.

This module reads the instrument and never commands it. There is no setter here,
no raw-command entry point, and nothing that turns a configuration value into
bytes on the wire. Field control (7.2.0) is a separate module,
``mast.instruments.magnet_625``. It writes only ``SETI`` and ``STOP``, through
this link's :meth:`Model625Link.session`, and tests pin it as the only place that
can build those two commands. The reasons for keeping the two apart are specific
to this instrument:

* ``SETI <A>`` / ``SETF <field>`` do not stage a value: the output "will ramp to
  [it] at the present ramp rate" the moment the command is parsed (manual §5.3,
  SETI/SETF). There is no separate "go" step. A setter that slips through is a
  field change.
* ``*RST`` "sets controller parameters to power-up settings", ``PSH 1`` turns on
  the persistent-switch heater, ``XPGM 1`` hands the output to an analogue input,
  ``*TRG`` ramps to the TRIG current. Each of these changes the magnet.
* In the 625's command set a getter and its setter differ by one ``?``
  (``SETI?`` asks, ``SETI 5`` ramps). So the guard is structural:
  :func:`build_query` accepts only exact strings from :data:`QUERIES`, every one
  of which ends in ``?``; only ``RSEGS?`` takes an argument, and only ``1``–``5``.
  A payload may not contain ``;`` (the 625 chains commands on it), so no query
  can carry a second command behind it.

Queries that are deliberately ABSENT although they end in ``?`` — reading them
changes instrument state:

* ``*ESR?``, ``OPSTR?``, ``ERSTR?`` — return a latched register **and clear it**
  (manual §5.1.4.2.2, §5.1.4.3). Reading them would erase a quench or
  over-voltage record before the operator saw it. ``OPST?`` (condition register)
  and ``ERST?`` (error status, cleared only by ``ERCL``) are used instead.
* ``KEYST?`` — "returns keypad status since the last KEYST?": reading resets it.
* ``LOCK?`` — returns the front-panel lock-out code along with the state.
* ``*TST?`` / ``*OPC?`` — nothing MAST needs.

Identity first: a COM name is not an identity (COM numbers of two
instruments can swap when USB adapters are replugged).
Every freshly opened handle is asked ``*IDN?`` and must answer ``LSCI,MODEL625``
before any other query is written on it, and every later query is pinned to that
handle's open generation (``SerialTransport.transact(expect_generation=...)``), so
a reopened port is re-identified instead of trusted.

Serial line (manual Table 5-7): RS-232C, DTE, 9600/19200/38400/57600 baud,
**7 data bits, odd parity, 1 stop bit**, CR LF terminators, no handshake, at
most 20 commands per second. Serial queries do not put the 625 into REMOTE
(only IEEE-488 or the Remote key do), so the front panel stays usable.
"""

from __future__ import annotations

import contextlib
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
    is_complete_line,
    open_transport,
)
from mast.i18n import tr

logger = logging.getLogger(__name__)

LSCI_TERMINATOR = b"\r\n"
IDN_MANUFACTURER = "LSCI"
IDN_MODEL = "MODEL625"

#: Line settings tried when identifying a 625. All four documented baud rates,
#: factory default (9600) first. The character format is fixed at 7O1.
MODEL625_SERIAL_CANDIDATES: tuple[tuple[int, int, str, float], ...] = (
    (9600, 7, "O", 1),
    (19200, 7, "O", 1),
    (38400, 7, "O", 1),
    (57600, 7, "O", 1),
)
SUPPORTED_BAUD = frozenset(b for b, *_ in MODEL625_SERIAL_CANDIDATES)

#: Every command MAST may ever write to a Model 625. Exact strings; all end in "?".
QUERIES: frozenset[str] = frozenset({
    "*IDN?",    # identity
    "*STB?",    # status byte (RQS is NOT cleared by this query, unlike a serial poll)
    "RDGI?",    # measured output current, A
    "RDGV?",    # output voltage at the supply terminals, V
    "RDGRV?",   # voltage at the remote-sense leads, V
    "RDGF?",    # computed field = measured current x field constant (FLDS units)
    "SETI?",    # output current setting
    "SETF?",    # output field setting
    "SETV?",    # compliance voltage setting
    "RATE?",    # ramp rate, A/s
    "RATEP?",   # persistent-mode ramp rate: enable, rate
    "LIMIT?",   # software limits: current, voltage, rate
    "PSH?",     # persistent switch heater: 0 off, 1 on, 2 warming, 3 cooling
    "PSHS?",    # PSH parameters: enable, current mA, delay s
    "PSHIS?",   # output current when the PSH was last turned off
    "QNCH?",    # quench detection: enable, current step limit A/s
    "XPGM?",    # external program mode: 0 internal, 1 external, 2 sum
    "OPST?",    # operational CONDITION register (not latched, not cleared)
    "ERST?",    # error status: hardware, operational, PSH (cleared only by ERCL)
    "FLDS?",    # field units (0 T/A, 1 kG/A) and field constant
    "MODE?",    # 0 local, 1 remote, 2 remote with local lockout
    "TRIG?",    # current the output would ramp to on *TRG / Trigger In
    "RSEG?",    # ramp segments enabled
    "RSEGS?",   # ramp segment parameters (argument: segment 1-5)
    "BAUD?",    # serial baud code
})

#: The only query that takes an argument, and the arguments it may take.
_ARG_QUERIES: dict[str, frozenset[str]] = {"RSEGS?": frozenset({"1", "2", "3", "4", "5"})}

#: Absent on purpose (see module docstring). Kept as data so a test can pin that
#: none of them ever becomes sendable.
FORBIDDEN_QUERIES: frozenset[str] = frozenset({"*ESR?", "OPSTR?", "ERSTR?", "KEYST?",
                                                "LOCK?", "*TST?", "*OPC?"})

#: Minimum gap between two commands. The manual allows 20 per second; 60 ms keeps
#: a margin when the monitor loop and the settings panel interleave.
MIN_COMMAND_GAP_S = 0.06

#: How long the settings half of the instrument-state readout (compliance, rates, limits,
#: ramp segments, PSH parameters, quench, modes, ...) may be reused. They move only when a
#: person works the front panel or MAST writes (MAST's own writes drop the cache at once,
#: :meth:`Model625Link.mark_sent`), so the panel's poll does not re-read ~20 of them every time.
SETTINGS_MAX_AGE_S = 60.0

#: Attributes of :class:`Magnet625State` that come from the settings queries (cached);
#: the rest is read live every time. ``setpoint_*`` is live on purpose: it is what the
#: operator watches during a ramp.
_SETTINGS_FIELDS = (
    "compliance_v", "ramp_rate_a_s", "persistent_ramp_enabled", "persistent_ramp_rate_a_s",
    "limit_current_a", "limit_voltage_v", "limit_rate_a_s", "ramp_segments_enabled",
    "ramp_segments", "trigger_current_a", "psh_enabled", "psh_current_ma", "psh_delay_s",
    "psh_last_current_a", "quench_detection_enabled", "quench_step_limit_a_s",
    "external_program_code", "interface_mode_code",
)
#: The query verbs behind :data:`_SETTINGS_FIELDS` (for the ``unanswered`` list).
_SETTINGS_VERBS = frozenset({"SETV?", "RATE?", "RATEP?", "LIMIT?", "RSEG?", "RSEGS?", "TRIG?",
                             "PSHS?", "PSHIS?", "QNCH?", "XPGM?", "MODE?"})

_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


class Model625Error(ValueError):
    """Refused or unparseable exchange with a Model 625."""


class WrongInstrument(Model625Error):
    """The port answered ``*IDN?`` but not as a Model 625. Nothing else was sent."""


def build_query(verb: str, arg: "str | int | None" = None) -> bytes:
    """The ONLY way to produce bytes for the 625: a whitelisted query, CR LF.

    Raises :class:`Model625Error` for anything else. Exact-match, upper-case
    whitelist; the argument (``RSEGS?`` only) must be one of its allowed tokens.
    """
    v = str(verb).strip().upper()
    if v not in QUERIES or not v.endswith("?"):
        raise Model625Error(f"refusing non-whitelisted command {verb!r}")
    allowed = _ARG_QUERIES.get(v)
    if arg is None or str(arg).strip() == "":
        if allowed is not None:
            raise Model625Error(f"{v} needs an argument")
        payload = v
    else:
        token = str(arg).strip()
        if allowed is None or token not in allowed:
            raise Model625Error(f"refusing argument {arg!r} for {v}")
        payload = f"{v} {token}"
    raw = payload.encode("ascii")
    # Structural invariants, checked on the final bytes: one query, no chaining,
    # no embedded terminator, printable ASCII only.
    if b";" in raw or b"\r" in raw or b"\n" in raw or any(c < 0x20 or c > 0x7E for c in raw):
        raise Model625Error(f"refusing malformed payload {payload!r}")
    return raw + LSCI_TERMINATOR


def parse_idn(raw: "bytes | str") -> tuple[bool, dict]:
    """``(is_model625, fields)`` from a ``*IDN?`` reply.

    ``LSCI,MODEL625,<serial>,<main fw>/<DAC fw>`` — field-based, not substring.
    """
    text = raw.decode("ascii", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    parts = [p.strip() for p in text.strip().split(",")]
    ok = (len(parts) >= 2 and parts[0].upper() == IDN_MANUFACTURER
          and parts[1].upper() == IDN_MODEL)
    return ok, {
        "idn": text.strip(),
        "model": parts[1] if len(parts) > 1 else "",
        "serial_number": parts[2] if len(parts) > 2 else "",
        "firmware": parts[3] if len(parts) > 3 else "",
    }


def _num(text: str) -> float:
    m = _NUM_RE.match(str(text).strip())
    if not m:
        raise Model625Error(f"reply is not a leading number: {text!r}")
    return float(m.group(0))


def _num_or_none(text: "str | None") -> "float | None":
    if text is None:
        return None
    try:
        return _num(text)
    except Model625Error:
        return None


def _fields(text: "str | None") -> list[str]:
    return [p.strip() for p in str(text or "").split(",")] if text else []


def _int_or_none(text: "str | None") -> "int | None":
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return None


# ── decoding tables (manual §5.1.4) ─────────────────────────────────────────
#: Operational CONDITION register (OPST?), bit -> meaning.
OPST_BITS = ((0, "compliance"), (1, "ramp_done"), (2, "psh_stable"))
#: Hardware error status (first ERST? field).
HW_ERROR_BITS = ((0, tr("机内温度故障(>80 °C)", "temperature fault (>80 °C)")),
                 (1, tr("市电电压过低", "low line voltage")),
                 (2, tr("输出过流(>62 A)", "output over current (>62 A)")),
                 (3, tr("输出过压(超过顺从电压)", "output over voltage (above compliance)")),
                 (4, tr("输出控制板故障", "output control failure")),
                 (5, tr("DAC 处理器无响应", "DAC processor not responding")))
#: Operational error status (second ERST? field).
OP_ERROR_BITS = ((0, tr("校准数据错误", "calibration error")),
                 (1, tr("外部编程电压过高", "external current program error")),
                 (2, tr("市电电压过高", "high line voltage")),
                 (3, tr("机内温度高(>75 °C)", "temperature high (>75 °C)")),
                 (4, tr("检测到远程禁止(Remote Inhibit)", "remote inhibit detected")),
                 (5, tr("检测到磁体失超", "magnet quench detected")),
                 (6, tr("磁体正经 crowbar 放电", "magnet discharging through crowbar")))
#: Persistent-switch-heater error status (third ERST? field).
PSH_ERROR_BITS = ((0, tr("持续开关加热器开路", "PSH open circuit")),
                  (1, tr("持续开关加热器短路", "PSH short circuit")))

PSH_STATES = {0: tr("关", "off"), 1: tr("开", "on"),
              2: tr("升温中", "warming"), 3: tr("冷却中", "cooling")}
MODE_LABELS = {0: tr("本地(面板可用)", "local (front panel active)"),
               1: tr("远程", "remote"), 2: tr("远程 + 面板锁定", "remote with local lockout")}
XPGM_LABELS = {0: tr("内部", "internal"), 1: tr("外部模拟量", "external"),
               2: tr("内部+外部叠加", "sum")}
FIELD_UNITS = {0: "T", 1: "kG"}


def decode_bits(code: "int | None", table) -> "list[str] | None":
    """Bitmask -> labels. ``None`` in (never answered) -> ``None`` out, never ``[]``."""
    if code is None or code < 0:
        return None
    return [label for bit, label in table if code & (1 << bit)]


@dataclass
class Magnet625State:
    """Everything MAST reads off a Model 625. ``None`` = not answered (unknown)."""

    port: str = ""
    idn: str = ""
    model: str = ""
    serial_number: str = ""
    firmware: str = ""
    read_at: float = 0.0                      # time.time() of this snapshot
    # live readings
    current_a: "float | None" = None
    voltage_v: "float | None" = None
    remote_voltage_v: "float | None" = None
    field: "float | None" = None              # in field_unit
    field_unit: str = ""
    field_constant: "float | None" = None     # field_unit per A
    # settings (read back, never written)
    setpoint_current_a: "float | None" = None
    setpoint_field: "float | None" = None
    compliance_v: "float | None" = None
    ramp_rate_a_s: "float | None" = None
    persistent_ramp_enabled: "bool | None" = None
    persistent_ramp_rate_a_s: "float | None" = None
    limit_current_a: "float | None" = None
    limit_voltage_v: "float | None" = None
    limit_rate_a_s: "float | None" = None
    ramp_segments_enabled: "bool | None" = None
    #: RSEGS? 1..5 when segments are enabled: [{"segment", "upper_current_a",
    #: "rate_a_s"}], the rate used while |I| is below that segment's upper current.
    ramp_segments: "list[dict] | None" = None
    trigger_current_a: "float | None" = None
    # persistent switch
    psh_enabled: "bool | None" = None
    psh_current_ma: "float | None" = None
    psh_delay_s: "float | None" = None
    psh_state_code: "int | None" = None
    psh_last_current_a: "float | None" = None
    # protection / modes
    quench_detection_enabled: "bool | None" = None
    quench_step_limit_a_s: "float | None" = None
    external_program_code: "int | None" = None
    interface_mode_code: "int | None" = None
    # status (raw codes are kept: labels are for people, decisions read bits)
    operational_status_code: "int | None" = None
    hardware_error_code: "int | None" = None
    operational_error_code: "int | None" = None
    psh_error_code: "int | None" = None
    unanswered: list[str] = dc_field(default_factory=list)

    @property
    def hardware_errors(self) -> "list[str] | None":
        return decode_bits(self.hardware_error_code, HW_ERROR_BITS)

    @property
    def operational_errors(self) -> "list[str] | None":
        return decode_bits(self.operational_error_code, OP_ERROR_BITS)

    @property
    def psh_errors(self) -> "list[str] | None":
        return decode_bits(self.psh_error_code, PSH_ERROR_BITS)

    # ── derived ──
    @property
    def ramp_done(self) -> "bool | None":
        c = self.operational_status_code
        return None if c is None else bool(c & 0b010)

    @property
    def in_compliance(self) -> "bool | None":
        c = self.operational_status_code
        return None if c is None else bool(c & 0b001)

    @property
    def ramping(self) -> "bool | None":
        """True while the output is moving towards its setpoint.

        Derived from the ramp-done condition bit; when that is unknown, from
        |I - I_set| (> 5 mA). ``None`` when neither is known.
        """
        done = self.ramp_done
        if done is not None:
            return not done
        if self.current_a is None or self.setpoint_current_a is None:
            return None
        return abs(self.current_a - self.setpoint_current_a) > 0.005

    @property
    def energized(self) -> "bool | None":
        """Output current away from zero (|I| > 10 mA). ``None`` when unread."""
        if self.current_a is None:
            return None
        return abs(self.current_a) > 0.010

    @property
    def any_error(self) -> "bool | None":
        codes = (self.hardware_error_code, self.operational_error_code, self.psh_error_code)
        if all(c is None for c in codes):
            return None
        return any(bool(c) for c in codes if c is not None and c >= 0)

    @property
    def quench_detected(self) -> "bool | None":
        """Operational error bit 5 (QNCH). ``None`` when ERST? never answered."""
        c = self.operational_error_code
        return None if c is None or c < 0 else bool(c & (1 << 5))

    @property
    def crowbar_discharge(self) -> "bool | None":
        """Operational error bit 6 (MDC): magnet discharging through the crowbar."""
        c = self.operational_error_code
        return None if c is None or c < 0 else bool(c & (1 << 6))

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d.update({
            "hardware_errors": self.hardware_errors,
            "operational_errors": self.operational_errors,
            "psh_errors": self.psh_errors,
            "crowbar_discharge": self.crowbar_discharge,
            "ramp_done": self.ramp_done,
            "in_compliance": self.in_compliance,
            "ramping": self.ramping,
            "energized": self.energized,
            "any_error": self.any_error,
            "quench_detected": self.quench_detected,
            "psh_state": (PSH_STATES.get(self.psh_state_code, str(self.psh_state_code))
                          if self.psh_state_code is not None else None),
            "interface_mode": (MODE_LABELS.get(self.interface_mode_code,
                                               str(self.interface_mode_code))
                               if self.interface_mode_code is not None else None),
            "external_program": (XPGM_LABELS.get(self.external_program_code,
                                                 str(self.external_program_code))
                                 if self.external_program_code is not None else None),
            "max_field": (None if self.limit_current_a is None or self.field_constant is None
                          else self.limit_current_a * self.field_constant),
        })
        return d


class Model625Link:
    """One Model 625 on one port. Shared by every sensor that reads it.

    All traffic goes through :meth:`query`, which builds the bytes with
    :func:`build_query`, re-identifies the instrument on every freshly opened
    handle and spaces commands by :data:`MIN_COMMAND_GAP_S`.
    """

    def __init__(self, transport: SerialLike, *, port: str = "",
                 timeout: float = 0.6, min_gap_s: float = MIN_COMMAND_GAP_S):
        self._t = transport
        self.port = port or str(getattr(getattr(transport, "settings", None), "port", "") or "")
        self._timeout = float(timeout)
        self._gap = max(0.0, float(min_gap_s))
        self._lock = threading.RLock()
        self._last_tx = 0.0
        self._verified_gen: "int | None" = None
        self._verified_once = False
        self.identity: dict = {}
        self.identity_error: str = ""
        self.field_unit: str = ""
        self.field_constant: "float | None" = None
        self._fast: "tuple[float, dict] | None" = None
        self._full: "Magnet625State | None" = None
        #: (monotonic time, {field: value}, unanswered settings verbs) of the last full sweep.
        self._settings: "tuple[float, dict, list[str]] | None" = None

    # ── transport plumbing ──
    @property
    def transport(self) -> SerialLike:
        return self._t

    def _pinned(self) -> bool:
        return bool(getattr(self._t, "supports_generations", False))

    def _space(self) -> None:
        wait = self._last_tx + self._gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _exchange(self, payload: bytes, *, pin: "int | None") -> str:
        self._space()
        try:
            kwargs = {"read_until": b"\n", "timeout": self._timeout}
            if pin is not None:
                kwargs["expect_generation"] = pin
            raw = self._t.transact(payload, **kwargs)
        finally:
            self._last_tx = time.monotonic()
        raw = bytes(raw or b"")
        if raw and not is_complete_line(raw):
            # A cut-short number is a wrong number: "+14.6800" -> "+1".
            raise Model625Error(f"{payload.decode('ascii', 'replace').strip()}: "
                                f"incomplete reply {raw!r} (no line end)")
        return raw.decode("ascii", "replace").strip()

    def _identity_current(self) -> bool:
        if not self._pinned():
            return self._verified_once
        if self._verified_gen is None:
            return False
        is_open = getattr(self._t, "is_open", None)
        gen = getattr(self._t, "open_generation", None)
        return bool(is_open) and gen == self._verified_gen

    def _verify_identity(self) -> None:
        """Ask ``*IDN?`` on the (re)opened handle; refuse anything but a 625."""
        self._verified_gen = None
        self._settings = None            # a (re)opened handle: do not vouch for old settings
        reply = self._exchange(build_query("*IDN?"), pin=None)
        ok, fields = parse_idn(reply)
        if not ok:
            self._verified_once = False
            self.identity_error = (f"{self.port}: *IDN? answered {reply!r}, not "
                                   f"{IDN_MANUFACTURER},{IDN_MODEL}")
            # Close our handle: we will not poll a port we cannot vouch for, and
            # the next attempt must start from a fresh open + *IDN? again.
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
        # Field units are part of the identity of a reading: RDGF? is in them.
        try:
            units = _fields(self._exchange(build_query("FLDS?"), pin=self._verified_gen))
            code = _int_or_none(units[0]) if units else None
            self.field_unit = FIELD_UNITS.get(code, "") if code is not None else ""
            self.field_constant = _num_or_none(units[1]) if len(units) > 1 else None
        except (SerialUnavailable, Model625Error) as exc:
            logger.debug("625 %s: FLDS? failed after identity: %s", self.port, exc)

    def query(self, verb: str, arg: "str | int | None" = None) -> str:
        """One whitelisted query on an identity-verified handle. Returns the reply.

        Raises :class:`SerialUnavailable` (port), :class:`WrongInstrument`
        (identity) or :class:`Model625Error` (refused / empty reply).
        """
        payload = build_query(verb, arg)          # refuse BEFORE touching the port
        with self._lock:
            if not self._identity_current():
                self._verify_identity()
            reply = self._exchange(payload, pin=self._verified_gen if self._pinned() else None)
        if not reply:
            raise Model625Error(f"{verb}: no reply within {self._timeout:.1f}s")
        return reply

    # ── shared access for mast.instruments.magnet_625 ──
    # Field control (7.2.0) writes SETI / STOP on THIS link's handle. It has to use
    # the same lock (so no poll lands between its command and its read-back), the
    # same identity check and the same command spacing. These three methods give it
    # that. They add no way to write: ``build_query`` is still the only thing in this
    # module that makes bytes, and it only makes whitelisted queries. The setters
    # live in the other module, which tests pin as the only one that may build them.
    @contextlib.contextmanager
    def session(self, timeout: "float | None" = None):
        """Hold the link, with the instrument's identity checked on the open handle.

        No query from another thread runs inside the block. Yields the open
        generation that was verified (``None`` on a transport without generations),
        for ``transact(expect_generation=...)``. Raises like :meth:`query`;
        :class:`SerialUnavailable` when the lock is not free within *timeout* s.
        """
        if timeout is None:
            got = self._lock.acquire()
        else:
            got = self._lock.acquire(timeout=max(0.0, float(timeout)))
        if not got:
            raise SerialUnavailable(f"{self.port}: link busy for {timeout} s")
        try:
            if not self._identity_current():
                self._verify_identity()
            yield self._verified_gen if self._pinned() else None
        finally:
            self._lock.release()

    def wait_gap(self) -> None:
        """Sleep until the next command may go out (``MIN_COMMAND_GAP_S`` after the last)."""
        self._space()

    def mark_sent(self) -> None:
        """A command just went out on this handle: restart the spacing clock, drop cached reads."""
        self._last_tx = time.monotonic()
        self._fast = None
        self._full = None
        self._settings = None

    def invalidate_settings(self) -> None:
        """Drop the cached settings: the next full readout asks the instrument again."""
        self._settings = None

    def close(self) -> None:
        """Release the OS handle (keep the shared transport object)."""
        with self._lock:
            self._verified_gen = None
            self._verified_once = False
        close = getattr(self._t, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - best-effort
                pass

    # ── readings ──
    def fast_state(self, max_age_s: float = 2.0) -> dict:
        """Current, field, condition and error registers; cached for ``max_age_s``.

        Four queries, shared by the field and current sensors so polling both
        costs one round per monitor interval, not two.
        """
        now = time.monotonic()
        with self._lock:
            if self._fast is not None and now - self._fast[0] < max_age_s:
                return self._fast[1]
            out: dict = {}
            out["current_a"] = _num(self.query("RDGI?"))
            out["field"] = _num(self.query("RDGF?"))
            out["field_unit"] = self.field_unit
            try:
                out["opst"] = _int_or_none(self.query("OPST?"))
            except Model625Error:
                out["opst"] = None
            try:
                codes = [_int_or_none(x) for x in _fields(self.query("ERST?"))]
            except Model625Error:
                codes = []
            out["hw_error_code"] = codes[0] if len(codes) > 0 else None
            out["op_error_code"] = codes[1] if len(codes) > 1 else None
            out["psh_error_code"] = codes[2] if len(codes) > 2 else None
            out["hw_errors"] = decode_bits(out["hw_error_code"], HW_ERROR_BITS)
            out["op_errors"] = decode_bits(out["op_error_code"], OP_ERROR_BITS)
            out["psh_errors"] = decode_bits(out["psh_error_code"], PSH_ERROR_BITS)
            self._fast = (time.monotonic(), out)
            return out

    def read_state(self, max_age_s: float = 0.0, *,
                   settings_max_age_s: float = 0.0) -> Magnet625State:
        """Full read-only snapshot (about 25 queries, ~2 s). Cached ``max_age_s``.

        ``settings_max_age_s`` > 0 lets the settings half come from the last full sweep when
        that is younger (and nothing was written since, see :meth:`mark_sent`): then only the
        live values are read -- the monitor's cached current/field/status (<= 2 s old) plus
        RDGV?, RDGRV?, SETI?, SETF?, PSH?. The default 0 keeps every existing caller (the
        field planner, autodetect) on a complete, fresh sweep.
        """
        with self._lock:
            if (self._full is not None and max_age_s > 0
                    and time.time() - self._full.read_at < max_age_s):
                return self._full
            cached = self._settings
            if (settings_max_age_s > 0 and cached is not None
                    and time.monotonic() - cached[0] < settings_max_age_s):
                return self._read_live_state(cached)
            st = Magnet625State(port=self.port)

            def ask(verb: str, arg=None) -> "str | None":
                try:
                    return self.query(verb, arg)
                except Model625Error as exc:
                    if isinstance(exc, WrongInstrument):
                        raise
                    st.unanswered.append(verb)
                    return None

            ask("*STB?")                      # cheap liveness + verifies identity
            st.idn = self.identity.get("idn", "")
            st.model = self.identity.get("model", "")
            st.serial_number = self.identity.get("serial_number", "")
            st.firmware = self.identity.get("firmware", "")
            st.field_unit = self.field_unit
            st.field_constant = self.field_constant
            st.current_a = _num_or_none(ask("RDGI?"))
            st.voltage_v = _num_or_none(ask("RDGV?"))
            st.remote_voltage_v = _num_or_none(ask("RDGRV?"))
            st.field = _num_or_none(ask("RDGF?"))
            st.setpoint_current_a = _num_or_none(ask("SETI?"))
            st.setpoint_field = _num_or_none(ask("SETF?"))
            st.compliance_v = _num_or_none(ask("SETV?"))
            st.ramp_rate_a_s = _num_or_none(ask("RATE?"))
            f = _fields(ask("RATEP?"))
            if f:
                st.persistent_ramp_enabled = bool(_int_or_none(f[0])) if _int_or_none(f[0]) is not None else None
                st.persistent_ramp_rate_a_s = _num_or_none(f[1]) if len(f) > 1 else None
            f = _fields(ask("LIMIT?"))
            if f:
                st.limit_current_a = _num_or_none(f[0])
                st.limit_voltage_v = _num_or_none(f[1]) if len(f) > 1 else None
                st.limit_rate_a_s = _num_or_none(f[2]) if len(f) > 2 else None
            seg = _int_or_none(ask("RSEG?"))
            st.ramp_segments_enabled = None if seg is None else bool(seg)
            if st.ramp_segments_enabled:
                segs = []
                for n in range(1, 6):
                    f = _fields(ask("RSEGS?", n))
                    if f:
                        segs.append({"segment": n, "upper_current_a": _num_or_none(f[0]),
                                     "rate_a_s": _num_or_none(f[1]) if len(f) > 1 else None})
                st.ramp_segments = segs
            st.trigger_current_a = _num_or_none(ask("TRIG?"))
            f = _fields(ask("PSHS?"))
            if f:
                en = _int_or_none(f[0])
                st.psh_enabled = None if en is None else bool(en)
                st.psh_current_ma = _num_or_none(f[1]) if len(f) > 1 else None
                st.psh_delay_s = _num_or_none(f[2]) if len(f) > 2 else None
            st.psh_state_code = _int_or_none(ask("PSH?"))
            st.psh_last_current_a = _num_or_none(ask("PSHIS?"))
            f = _fields(ask("QNCH?"))
            if f:
                en = _int_or_none(f[0])
                st.quench_detection_enabled = None if en is None else bool(en)
                st.quench_step_limit_a_s = _num_or_none(f[1]) if len(f) > 1 else None
            st.external_program_code = _int_or_none(ask("XPGM?"))
            st.interface_mode_code = _int_or_none(ask("MODE?"))
            st.operational_status_code = _int_or_none(ask("OPST?"))
            codes = [_int_or_none(x) for x in _fields(ask("ERST?"))]
            st.hardware_error_code = codes[0] if len(codes) > 0 else None
            st.operational_error_code = codes[1] if len(codes) > 1 else None
            st.psh_error_code = codes[2] if len(codes) > 2 else None
            st.read_at = time.time()
            self._full = st
            self._settings = (time.monotonic(),
                              {k: getattr(st, k) for k in _SETTINGS_FIELDS},
                              [v for v in st.unanswered if v in _SETTINGS_VERBS])
            return st

    def _read_live_state(self, cached: "tuple[float, dict, list[str]]") -> Magnet625State:
        """The snapshot with the cached settings and freshly read live values (lock held)."""
        st = Magnet625State(port=self.port)

        def ask(verb: str) -> "str | None":
            try:
                return self.query(verb)
            except Model625Error as exc:
                if isinstance(exc, WrongInstrument):
                    raise
                st.unanswered.append(verb)
                return None

        try:
            fast = self.fast_state(2.0)          # shared with the monitor: usually no I/O
        except WrongInstrument:
            raise
        except Model625Error:
            fast = {}
            st.unanswered.extend(["RDGI?", "RDGF?"])
        st.idn = self.identity.get("idn", "")
        st.model = self.identity.get("model", "")
        st.serial_number = self.identity.get("serial_number", "")
        st.firmware = self.identity.get("firmware", "")
        st.field_unit = self.field_unit
        st.field_constant = self.field_constant
        st.current_a = fast.get("current_a")
        st.field = fast.get("field")
        st.operational_status_code = fast.get("opst")
        st.hardware_error_code = fast.get("hw_error_code")
        st.operational_error_code = fast.get("op_error_code")
        st.psh_error_code = fast.get("psh_error_code")
        st.voltage_v = _num_or_none(ask("RDGV?"))
        st.remote_voltage_v = _num_or_none(ask("RDGRV?"))
        st.setpoint_current_a = _num_or_none(ask("SETI?"))
        st.setpoint_field = _num_or_none(ask("SETF?"))
        st.psh_state_code = _int_or_none(ask("PSH?"))
        for key, value in cached[1].items():
            setattr(st, key, value)
        st.unanswered.extend(cached[2])
        st.read_at = time.time()
        self._full = st
        return st


def probe_model625(port: str, *, candidates=MODEL625_SERIAL_CANDIDATES,
                   timeout: float = 0.6, on_reply=None) -> "tuple[SerialSettings, dict] | None":
    """Identify a 625 on *port*. Writes nothing but ``*IDN?``. Never raises.

    ``on_reply(bytes)`` is told about every non-empty answer, so a caller can
    tell "silent port" from "something talked, but not a 625".
    """
    for baud, bits, parity, stop in candidates:
        settings = SerialSettings(port=port, baudrate=baud, bytesize=bits,
                                  parity=parity, stopbits=stop, timeout=timeout)
        transport: "SerialTransport | None" = None
        try:
            transport = open_transport(settings)
            # Twice at the same baud, as in lakeshore_temp.probe_lakeshore: the previous
            # candidate went out at a wrong baud and may have left junk without a terminator
            # in the 625's command buffer; the first query's CR LF clears it.
            from mast.environment.lakeshore_temp import IDN_ATTEMPTS, IDN_RETRY_TIMEOUT_S

            for attempt in range(IDN_ATTEMPTS):
                reply = transport.transact(build_query("*IDN?"), read_until=b"\n",
                                           timeout=timeout if attempt == 0
                                           else min(timeout, IDN_RETRY_TIMEOUT_S))
                if reply and on_reply is not None:
                    try:
                        on_reply(bytes(reply))
                    except Exception:  # noqa: BLE001
                        pass
                ok, fields = parse_idn(reply) if is_complete_line(reply) else (False, {})
                if ok:
                    return settings, fields
        except Exception as exc:  # noqa: BLE001 - probing is best-effort
            logger.debug("probe_model625 %s at %s: %s", port, settings.label(), exc)
        finally:
            if transport is not None:
                transport.close()
    return None


# ── environment sensors ────────────────────────────────────────────────────

_QUANTITIES = {"field", "current"}


class Model625Sensor(EnvironmentSensor):
    """One quantity of a Model 625 as an environment reading (read-only).

    ``quantity="field"`` reports the computed field (RDGF?, in the instrument's
    FLDS units, T or kG); ``"current"`` the measured output current (RDGI?, A).

    Status: ``ok``; ``warning`` when the 625 reports any error bit (quench,
    crowbar discharge, remote inhibit, over-temperature, PSH open/short, ...).
    Never ``alarm`` on its own — an ``alarm`` makes MAST stop the run and retract
    the tip, and that response is the operator's to opt into through an
    explicit ``alarm`` band in ``environment_sensors.json``, not a side effect of
    a status bit this driver decoded.
    """

    counts_as_real = True
    quiet_gated = False

    def __init__(self, link: Model625Link, *, name: str = "magnet_field",
                 quantity: str = "field", alarm: "AlarmSpec | None" = None,
                 max_age_s: float = 2.0):
        q = str(quantity or "field").strip().lower()
        if q not in _QUANTITIES:
            raise ValueError(f"Model625Sensor: unknown quantity {quantity!r}")
        self._link = link
        self._name = str(name)
        self._quantity = q
        self._alarm = alarm or AlarmSpec()
        self._max_age = float(max_age_s)
        self._unit = "A" if q == "current" else (link.field_unit or "T")
        self.last_problem = ""

    def name(self) -> str:
        return self._name

    @property
    def link(self) -> Model625Link:
        return self._link

    @property
    def quantity(self) -> str:
        return self._quantity

    def read(self) -> SensorReading:
        try:
            st = self._link.fast_state(self._max_age)
        except WrongInstrument as exc:
            # WARNING once per distinct problem, DEBUG while it persists: the
            # identity check is retried every poll (2 s), and a pinned port with
            # the wrong device on it must not flood the service log.
            level = logging.DEBUG if str(exc) == self.last_problem else logging.WARNING
            self.last_problem = str(exc)
            logger.log(level, "Model 625 sensor %s: %s — not polling this port", self._name, exc)
            return SensorReading(value=0.0, unit=self._unit, status="unavailable")
        except SerialUnavailable as exc:
            self.last_problem = str(exc)
            self._link.close()
            return SensorReading(value=0.0, unit=self._unit, status="unavailable")
        except Model625Error as exc:
            self.last_problem = str(exc)
            return SensorReading(value=0.0, unit=self._unit, status="error")
        if self._quantity == "current":
            value, unit = float(st["current_a"]), "A"
        else:
            value = float(st["field"])
            unit = st.get("field_unit") or self._link.field_unit or self._unit
            self._unit = unit
        faults = [*(st.get("hw_errors") or []), *(st.get("op_errors") or []),
                  *(st.get("psh_errors") or [])]
        self.last_problem = "; ".join(faults)
        base = "warning" if faults else "ok"
        # evaluate() returns ``base`` unless a configured band is crossed, so a
        # fault bit can never be downgraded back to "ok" by the alarm spec.
        status = self._alarm.evaluate(value, base_status=base)
        return SensorReading(value=value, unit=unit, status=status)

    def instrument_state(self) -> "Magnet625State | None":
        """Full read-only snapshot over this sensor's link (cached 5 s; the settings half up to
        :data:`SETTINGS_MAX_AGE_S`, so a poll costs ~5 queries instead of ~26)."""
        try:
            return self._link.read_state(max_age_s=5.0, settings_max_age_s=SETTINGS_MAX_AGE_S)
        except Exception as exc:  # noqa: BLE001 - panel must never 500
            logger.debug("625 instrument_state on %s failed: %s", self._name, exc)
            return None

    def close(self) -> None:
        self._link.close()


__all__ = [
    "QUERIES",
    "FORBIDDEN_QUERIES",
    "MODEL625_SERIAL_CANDIDATES",
    "SUPPORTED_BAUD",
    "MIN_COMMAND_GAP_S",
    "SETTINGS_MAX_AGE_S",
    "Model625Error",
    "WrongInstrument",
    "build_query",
    "parse_idn",
    "decode_bits",
    "Magnet625State",
    "Model625Link",
    "Model625Sensor",
    "probe_model625",
]
