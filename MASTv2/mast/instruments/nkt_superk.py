"""NKT SuperK FIANIUM + SuperK SELECT (+ RF driver) over the NKT Photonics *Interbus* protocol.

Contract: ``docs/v2/design/optical_station_witec.md`` §3 / §6.1 / D6 / D7 / 陷阱 7 and 13.

Sources (every byte on the wire and every register below comes from one of these)
==================================================================================
[SDK]  "NKT Photonics Software Development Kit - Instruction Manual", v2.1.3 (Oct 2018).
       §2 *Interbus Protocol Description* (physical layer, telegram, CRC, escaping, worked
       examples; the six example telegrams are pinned byte for byte in the tests) and §6
       *Register Files* (§6.1 general, §6.8 SuperK EXTREME main module, §6.9 RF Driver for SuperK
       SELECT, §6.10 SuperK SELECT). Hosted at
       https://nkt-tools.readthedocs.io/en/latest/_downloads/86af7626c8c607ecd09942f08ac0ae20/SDK%20Instruction%20manual.pdf
[SDK2] The same manual in the *current* SDK (release 2.1.16.3027, "SDK Current.zip" linked from
       https://lf.hamamatsu.com/download-center/ ->
       https://contentnktphotonics.s3-eu-central-1.amazonaws.com/Software/SDK/SDK%20Current.zip),
       whose **§6.23 "SuperK FIANIUM (S4x3)"** documents the FIANIUM: "The module type number
       is 0088h in a 16-bit unsigned integer. The standard module address is 15" (release notes 2.1.4:
       register file 88 added). §6.12 (EXTREME S4x2, type 0x60) notes that a few S4x2 lasers were
       marketed as "Fianium" and "should not be confused with SuperK FIANIUM, product number S4x3".
       §6.13 (RF Driver A901) and §6.15 (SELECT A203) are the same registers as [SDK].
[CTL]  NKT CONTROL 1.3.9 user manual §3.1 (Quick/Manual connect) and §3.11 (SELECT: "the RF
       power has to be off before changing crystals" when one RF driver feeds a two-crystal
       SELECT) and its release notes (FIANIUM/SELECT plugins, "16bit moduletypes").
[LOG]  NKT CONTROL's log / ini on a complete system: the bus scan finds
       0x88 @15 (FIANIUM), 0x66 @19 (RF driver), 0x67 @21 (SELECT); the saved channel
       settings confirm wavelengths in pm and amplitudes in 0.1 %.
[pylablib] ``pylablib/devices/NKT/interbus.py`` (GPL - consulted as an independent cross-check
       of the framing and of the SELECT/RF-driver register numbers; no code is copied).

Wire format  [SDK §2.1-2.5]
---------------------------
115200 baud 8N1 (USB virtual COM port the same). A telegram is ``SOT(0x0D) message EOT(0x0A)``;
the message is ``dest src type register data[0..240] CRC_hi CRC_lo``. CRC-16 CCITT (XModem:
poly 0x1021, start value 0) over *all* message bytes before the CRC, sent big-endian; the
receiver runs the CRC over everything including the CRC bytes and must get 0. Multi-byte
data are little-endian. The bytes 0x0A, 0x0D, 0x5E inside the message (CRC included) are sent
as ``0x5E, byte+0x40``; the CRC is *not* recomputed after escaping. Host address: the manual's
examples use 162 (0xA2); host addresses must be above 160. Message types: 0 Nack, 1 CRC error,
2 Busy, 3 Ack, 4 Read, 5 Write, 8 Datagram (the answer to a Read). ``tests`` pin the manual's
own example telegrams byte for byte.

Registers used  [SDK §6]  (unit/scale as the manual states them)
----------------------------------------------------------------
common (all modules)  0x61 module type (u8), 0x65 serial number (8-char string), 0x66 status bits
                      (u16), 0x64 firmware version code (§2.2)
main board EXTREME S4x2 (type 0x60, [SDK] §6.8 = [SDK2] §6.12)  0x30 emission (u8: 0 off, 3 on),
                      0x31 setup (u16: 0 constant current, 1 constant power, 2/3 externally
                      modulated, 4 power lock), 0x32 interlock (2 bytes: LSB 0 off / 1 waiting for
                      reset / 2 OK, MSB where), 0x11 inlet temperature (i16, 0.1 °C), 0x37 power
                      level (u16, 0.1 %), 0x38 current level (u16, 0.1 %), 0x36 watchdog (u8, s),
                      0x6B system type, status bit 0 = "Emission on"
main board FIANIUM S4x3 (type 0x0088 - a 16-bit value, [SDK2] §6.23)  0x30 emission (u8: 0 off,
                      3 on "if interlock circuit has been reset"; intermediate values while it
                      switches), 0x31 setup (u16: 0 internal power control, 4 external feedback /
                      power lock), 0x32 interlock (as above), 0x36 watchdog (u8, s, 0 = off),
                      0x37 *output level* (u16, 0.1 % of the optical output - the one power
                      setpoint), 0x39 NIM delay, 0x3B user setup bits, 0x66 status bits (bit 0 emission
                      on, 1 interlock relays off, 2 interlock supply voltage low, 3 interlock loop open,
                      4 output control signal low, 5 supply voltage low, 6 internal temperature out of
                      range, 7 clock battery low, 8 date/time not set, 13 CRC error on startup, 14 log
                      error, 15 system error). No 0x11 / 0x38 on this board.
RF driver (type 0x66, §6.9)   0x30 RF power (u8: 0 off, 1 on), 0x31 setup bits (u16), 0x34/0x35
                      min/max usable wavelength of the connected crystal (u32, pm), 0x38 crystal
                      temperature (i16, 0.1 °C), 0x75 connected crystal (u8; 0 = none; numbered
                      1, 2 for the first SELECT), 0x90..0x97 channel wavelengths (each a 4 x u32
                      array in pm - element 0 is the one used without FSK, and "it is OK to send
                      only one 32-bit integer"), 0xB0..0xB7 channel amplitudes (u16, 0.1 %)
SELECT (type 0x67, §6.10)     0x34 RF switch (u8: 0 normal, 1 swap; "RF power must be off before
                      the RF switch position is changed"), 0x35 monitor switch, 0x10/0x11
                      monitor readouts (u16, 0.1 %), 0x90/0x91 crystal 1 min/max wavelength
                      (u32, pm), 0xA0/0xA1 crystal 2 min/max wavelength (u32, pm)

What is NOT supported, and why  (rule: no source - no write)
-------------------------------------------------------------
* **Main-board writes are limited to the two module types whose register file is sourced**
  (``main_write_types``, default ``[0x60, 0x88]``). Any other main-board type is refused for writes
  (and for everything else: the address must answer 0x60 or 0x88). The list is also a safety knob:
  ``main_write_types: []`` makes the main board read-only for MAST (operate the FIANIUM by key /
  front panel / NKT CONTROL, MAST only gates the SELECT output with the RF power, which
  ``emission_on`` then does without touching the main board).
* The first revision of this driver was written against the 2018 manual, which has no section for
  0x88; the S4x3 section was found in the current SDK afterwards. Everything above is *documented*
  for 0x88 - what has **not** been seen on the real laser is still the replies on this system
  (e.g. how long the emission register stays in intermediate states): that is what
  ``IdentifyOpticalSource`` and the bench self-test are for, and why ``write_enabled`` defaults to false.
* The interlock is never reset by software (writing >0 to 0x32 is documented, but resetting a
  safety interlock is a physical action).
* Watchdog (0x36, "automatic shut-off if communication is lost" - a good idea for an unattended
  laser, but it needs a keep-alive thread so a long exposure does not trip it), pulse picker, FSK,
  modulation, NIM delay, user setup bits, Ethernet module: not written.

Safety model
------------
``emission_on`` = main emission on (if allowed) + RF power on; ``emission_off`` = main emission 0
+ RF power 0, every step independent, never raising, preempting a wait in progress.

Gates (D7) - ``allow_emission_control`` → ``write_enabled`` → the operator's ``laser_area_safe``
attestation (:class:`~mast.instruments.laser.LaserAttestationGuard`, installed by ``build()`` unless the
entry says ``require_laser_attestation: false``) - stand in front of **every path that adds light on the
sample**, here four: ``emission_on``; ``set_rf_power(True)`` (opens the SELECT output); a channel amplitude
made higher than it is now (a dark channel opened, or a lit one made brighter); and the main output level made
higher than it is now - the last two only while the chain is, or may be, live (main emission not off, RF power
not off), because otherwise they add nothing yet. Not knowing the present state or value counts as "adds" (fail
closed). So ``allow_emission_control=false`` means "MAST never adds light": the operator lights the system and MAST
may still tune the colour, turn the light down and switch it off. Never gated: ``set_wavelength_nm``, ``select_crystal``
(leaves RF off), the same level, a lower level, 0, and everything in the off direction (``emission_off``,
``set_rf_power(False)``). The ``LaserEmissionOn`` skill checks the attestation first, with a better message.

Every one of those paths is checked twice (before it waits for the device lock, to refuse early, and again once it
holds the device) and is cancelled by an ``emission_off()`` asked for after it started (the shutdown generation, see
:mod:`mast.instruments.laser`): the main-board wait polls it, and a switch-on cancelled half-way switches back off.

The off direction does not wait forever either: ``emission_off`` and ``set_rf_power(False)`` wait for the device
lock and the Interbus port at most ``SAFE_LOCK_TIMEOUT_S`` in total (:mod:`mast.instruments.safe_direction`), then
``emission_off`` answers ``None`` ("not confirmed") and logs - never an exception, never a hang.
Wavelength/amplitude writes need ``write_enabled``; out-of-range requests are refused (config limits, the
connected crystal's usable range, 8 channels), never clamped.

Port etiquette
--------------
**NKT CONTROL and MAST cannot hold the same COM port at the same time.** CONTROL's *Quick Connect* opens and
bus-scans *every* serial port - sending Interbus frames to any other serial device
(a laser, a TC100) as well; use *Manual Connect* (open only the NKT port) or close CONTROL before MAST runs.
USB virtual COM ports of complete systems are best used with RTS/CTS handshake [SDK §2.1]; the
shared ``SerialSettings`` has no handshake field, so the default (none, RTS asserted) is used -
if the FIANIUM does not answer on the bench, that is the first thing to check.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from mast.environment.serial_transport import (
    SerialLike,
    SerialSettings,
    SerialUnavailable,
    open_transport,
)
from mast.i18n import tr
from mast.instruments.base import (
    InstrumentError,
    InstrumentUnavailable,
    OperationNotSupported,
    serial_open_hint,
)
from mast.instruments.laser import (
    EmissionGuard,
    LaserLimitError,
    LaserLimits,
    TunableLaser,
    default_emission_guard,
)
from mast.instruments.safe_direction import LockTimeout, held, safe_direction

logger = logging.getLogger(__name__)

__all__ = [
    "NktSuperK", "InterbusLink", "InterbusError", "InterbusNack", "InterbusTimeout",
    "Telegram", "MsgType", "crc16_ccitt", "escape", "unescape", "build_telegram",
    "parse_telegram", "build",
]

#: tests replace these so polling loops run instantly
_sleep = time.sleep
_monotonic = time.monotonic

# ── protocol constants [SDK §2] ──────────────────────────────────────────────

SOT, EOT, SOE, ECC = 0x0D, 0x0A, 0x5E, 0x40
#: the SDK manual's examples use host address 162; "host addresses must be above 160"
HOST_ADDRESS = 0xA2
MAX_DATA = 240


class MsgType(enum.IntEnum):
    NACK = 0
    CRC_ERROR = 1
    BUSY = 2
    ACK = 3
    READ = 4
    WRITE = 5
    DATAGRAM = 8


class InterbusError(InstrumentError):
    """A telegram could not be exchanged correctly."""


class InterbusTimeout(InstrumentUnavailable):
    """No (complete) reply from a module in time."""


class InterbusNack(InterbusError):
    """The module answered Nack: register unknown, restricted or not applicable [SDK §2.2]."""


# ── framing (pure) ───────────────────────────────────────────────────────────


def crc16_ccitt(data: bytes, crc: int = 0) -> int:
    """CRC-16 CCITT / XModem, byte-at-a-time exactly as the SDK manual's C code (§2.2):
    ``crc = swap_bytes(crc); crc ^= b; crc ^= (crc & 0xff) >> 4; crc ^= crc << 12;
    crc ^= (crc & 0xff) << 5`` (16-bit arithmetic)."""
    for b in data:
        crc = ((crc >> 8) | (crc << 8)) & 0xFFFF
        crc ^= b
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 12) & 0xFFFF
        crc ^= ((crc & 0xFF) << 5) & 0xFFFF
    return crc & 0xFFFF


def escape(message: bytes) -> bytes:
    """0x0A / 0x0D / 0x5E -> ``0x5E, b + 0x40`` [SDK §2.3]."""
    out = bytearray()
    for b in message:
        if b in (SOT, EOT, SOE):
            out.append(SOE)
            out.append((b + ECC) & 0xFF)
        else:
            out.append(b)
    return bytes(out)


def unescape(body: bytes) -> bytes:
    out = bytearray()
    it = iter(body)
    for b in it:
        if b == SOE:
            try:
                nxt = next(it)
            except StopIteration:
                raise InterbusError("telegram ends inside an escape sequence") from None
            if nxt not in (0x4A, 0x4D, 0x9E):
                raise InterbusError(f"bad escape sequence 0x5E 0x{nxt:02X}")
            out.append((nxt - ECC) & 0xFF)
        elif b in (SOT, EOT):
            raise InterbusError("framing byte inside a telegram body")
        else:
            out.append(b)
    return bytes(out)


def build_telegram(dest: int, src: int, msg_type: int, register: int, data: bytes = b"") -> bytes:
    """``SOT escape(dest src type register data crc_hi crc_lo) EOT``."""
    if len(data) > MAX_DATA:
        raise InterbusError(f"{len(data)} data bytes - at most {MAX_DATA} per telegram")
    msg = bytes([dest & 0xFF, src & 0xFF, int(msg_type) & 0xFF, register & 0xFF]) + bytes(data)
    crc = crc16_ccitt(msg)
    msg += bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    return bytes([SOT]) + escape(msg) + bytes([EOT])


@dataclass(frozen=True)
class Telegram:
    dest: int
    src: int
    type: int
    payload: bytes          # everything between the type byte and the CRC: [register] + data

    @property
    def register(self) -> int | None:
        return self.payload[0] if self.payload else None

    @property
    def data(self) -> bytes:
        return self.payload[1:]


def parse_telegram(raw: bytes) -> Telegram:
    """Parse one telegram (``SOT .. EOT``) from *raw*; the first one found is used."""
    raw = bytes(raw or b"")
    start = raw.find(bytes([SOT]))
    if start < 0:
        raise InterbusError(f"no start-of-telegram byte in the reply: {raw.hex(' ')}")
    end = raw.find(bytes([EOT]), start + 1)
    if end < 0:
        raise InterbusError(f"reply is not terminated (no EOT): {raw.hex(' ')}")
    # a stray SOT right before the real one (line noise): the last SOT before EOT starts the telegram
    start = raw.rfind(bytes([SOT]), start, end)
    msg = unescape(raw[start + 1:end])
    if len(msg) < 5:
        raise InterbusError(f"telegram too short ({len(msg)} bytes): {raw.hex(' ')}")
    if crc16_ccitt(msg) != 0:
        raise InterbusError(f"CRC error in reply: {raw.hex(' ')}")
    return Telegram(dest=msg[0], src=msg[1], type=msg[2], payload=msg[3:-2])


# ── link layer ────────────────────────────────────────────────────────────────


class InterbusLink:
    """Request/response over a transactional transport (``SerialLike``).

    ``transport_fn`` returns the (possibly lazily opened) transport; ``on_io_error`` is called
    when the transport itself failed so the owner can drop it."""

    def __init__(self, transport_fn: Callable[[], SerialLike], *, host: int = HOST_ADDRESS,
                 timeout_s: float = 0.5, write_ack_timeout_s: float = 0.3, retries: int = 2,
                 on_io_error: Callable[[], None] | None = None, name: str = "NKT Interbus port"):
        self.name = name
        self._transport_fn = transport_fn
        self.host = int(host)
        self.timeout_s = float(timeout_s)
        self.write_ack_timeout_s = float(write_ack_timeout_s)
        self.retries = int(retries)
        self._on_io_error = on_io_error
        self._lock = threading.RLock()

    def _exchange(self, frame: bytes, timeout: float) -> bytes:
        with held(self._lock, self.name):
            t = self._transport_fn()
            try:
                return bytes(t.transact(frame, read_until=bytes([EOT]), timeout=timeout) or b"")
            except SerialUnavailable as exc:
                if self._on_io_error is not None:
                    self._on_io_error()
                raise InstrumentUnavailable(f"NKT Interbus I/O failed: {exc}") from exc

    def _request(self, dest: int, msg_type: int, register: int, data: bytes, *,
                 timeout: float, expect: tuple[int, ...], tolerate_silence: bool = False) -> Telegram | None:
        frame = build_telegram(dest, self.host, msg_type, register, data)
        last: Exception | None = None
        for _attempt in range(self.retries + 1):
            raw = self._exchange(frame, timeout)
            if not raw:
                if tolerate_silence:
                    return None
                raise InterbusTimeout(
                    f"module {dest} did not answer register 0x{register:02X} within {timeout:g}s "
                    + tr("（模块没上电、地址不对，或串口被 NKT CONTROL 占着？）",
                         "(not powered, wrong address, or is NKT CONTROL holding the port?)"))
            try:
                tg = parse_telegram(raw)
            except InterbusError as exc:                 # garbled reply: ask again
                last = exc
                continue
            if tg.dest != self.host or tg.src != dest:
                last = InterbusError(f"reply addressed {tg.src}->{tg.dest}, expected {dest}->{self.host}")
                continue
            if tg.type == MsgType.CRC_ERROR:
                last = InterbusError(f"module {dest} reports a CRC error in our telegram")
                continue
            if tg.type == MsgType.BUSY:
                last = InterbusError(f"module {dest} is busy")
                _sleep(0.1)
                continue
            if tg.type == MsgType.NACK:
                raise InterbusNack(
                    f"module {dest} refused register 0x{register:02X} (Nack: not understood, not applicable "
                    "or not allowed)")
            if tg.type not in expect:
                last = InterbusError(f"unexpected reply type {tg.type} (wanted {expect})")
                continue
            if msg_type == MsgType.READ and tg.register != register:
                last = InterbusError(f"reply is for register {tg.register}, asked 0x{register:02X}")
                continue
            return tg
        raise InterbusError(f"module {dest} register 0x{register:02X}: {last}")

    def read(self, dest: int, register: int) -> bytes:
        tg = self._request(dest, MsgType.READ, register, b"", timeout=self.timeout_s,
                           expect=(MsgType.DATAGRAM,))
        assert tg is not None
        return tg.data

    def write(self, dest: int, register: int, data: bytes) -> bool:
        """Write; True if the module acknowledged, False if it stayed silent (a few modules never
        acknowledge writes [SDK §2.6] - the caller verifies by reading back)."""
        tg = self._request(dest, MsgType.WRITE, register, data, timeout=self.write_ack_timeout_s,
                           expect=(MsgType.ACK,), tolerate_silence=True)
        return tg is not None


# ── register vocabulary ─────────────────────────────────────────────────────────

# common [SDK §6.1, §2.2]
REG_TYPE, REG_SERIAL, REG_STATUS, REG_FIRMWARE = 0x61, 0x65, 0x66, 0x64
# main board [SDK §6.8]
REG_INLET_TEMP, REG_EMISSION, REG_SETUP, REG_INTERLOCK = 0x11, 0x30, 0x31, 0x32
REG_POWER_LEVEL, REG_CURRENT_LEVEL, REG_SYSTEM_TYPE = 0x37, 0x38, 0x6B
EMISSION_OFF, EMISSION_ON = 0, 3
# RF driver [SDK §6.9]
REG_RF_POWER, REG_RF_SETUP = 0x30, 0x31
REG_RF_MIN_PM, REG_RF_MAX_PM, REG_CRYSTAL_TEMP, REG_CONNECTED_CRYSTAL = 0x34, 0x35, 0x38, 0x75
REG_WAVELENGTH0, REG_AMPLITUDE0 = 0x90, 0xB0
N_CHANNELS = 8
# SELECT [SDK §6.10]
REG_RF_SWITCH, REG_MONITOR_SWITCH = 0x34, 0x35
REG_MONITOR1, REG_MONITOR2 = 0x10, 0x11
REG_CRYSTAL1_MIN, REG_CRYSTAL1_MAX, REG_CRYSTAL2_MIN, REG_CRYSTAL2_MAX = 0x90, 0x91, 0xA0, 0xA1

TYPE_EXTREME_MAIN = 0x60      # SuperK EXTREME S4x2 main board [SDK §6.8 = SDK2 §6.12]
TYPE_FIANIUM_G3 = 0x88        # SuperK FIANIUM S4x3, 16-bit module type [SDK2 §6.23]
TYPE_RF_DRIVER = 0x66
TYPE_SELECT = 0x67
_EXPECTED_TYPES: dict[str, tuple[int, ...]] = {
    "main": (TYPE_EXTREME_MAIN, TYPE_FIANIUM_G3),
    "rf_driver": (TYPE_RF_DRIVER,),
    "select": (TYPE_SELECT,),
}
DEFAULT_ADDRESSES = {"main": 15, "rf_driver": 19, "select": 21}   # design doc §3

@dataclass(frozen=True)
class MainMap:
    """What is documented for one main-board module type (see the module docstring for the sources)."""

    type: int
    name: str
    source: str
    status_bits: dict[int, str]
    setup_modes: dict[int, str]
    #: setup mode -> the register that holds the level setpoint in effect in that mode (0.1 % steps)
    level_register: dict[int, int]
    #: documented read-only registers worth reporting: name -> (register, bytes, signed, scale)
    readings: dict[str, tuple[int, int, bool, float]]


_STATUS_COMMON = {0: "emission_on", 1: "interlock_relays_off", 2: "interlock_supply_voltage_low",
                  3: "interlock_loop_open", 4: "output_control_signal_low", 5: "supply_voltage_low",
                  7: "clock_battery_low_voltage", 13: "crc_error_on_startup", 14: "log_error_code_present",
                  15: "system_error_code_present"}
MAIN_MAPS: dict[int, MainMap] = {
    TYPE_EXTREME_MAIN: MainMap(
        TYPE_EXTREME_MAIN, "SuperK EXTREME (S4x2)", "NKT SDK manual 6.8 (v2.1.3) = 6.12 (v2.1.16)",
        {**_STATUS_COMMON, 6: "inlet_temperature_out_of_range"},
        {0: "constant current", 1: "constant power", 2: "externally modulated current",
         3: "externally modulated power", 4: "external feedback (power lock)"},
        {1: REG_POWER_LEVEL, 0: REG_CURRENT_LEVEL},
        {"inlet_temperature_c": (REG_INLET_TEMP, 2, True, 0.1), "power_level_pct": (REG_POWER_LEVEL, 2, False, 0.1),
         "current_level_pct": (REG_CURRENT_LEVEL, 2, False, 0.1)}),
    TYPE_FIANIUM_G3: MainMap(
        TYPE_FIANIUM_G3, "SuperK FIANIUM (S4x3)", "NKT SDK manual 6.23 (v2.1.16)",
        {**_STATUS_COMMON, 6: "internal_temperature_out_of_range", 8: "date_time_not_set"},
        {0: "internal power control", 4: "external feedback (power lock)"},
        {0: REG_POWER_LEVEL},
        {"output_level_pct": (REG_POWER_LEVEL, 2, False, 0.1)}),
}
#: interlock register [SDK §6.8]: LSB 0 = off (circuit open; MSB says where), 1 = waiting, 2 = OK
_INTERLOCK_WHERE = {0: "interlock circuit open", 1: "front panel interlock / key switch off",
                    2: "door switch open", 3: "external module interlock",
                    4: "application interlock", 5: "internal module interlock",
                    6: "interlock power failure", 7: "interlock disabled by light source",
                    255: "interlock circuit failure"}


def decode_interlock(raw: bytes) -> dict[str, Any]:
    if len(raw) < 2:
        raise InterbusError(f"interlock register returned {len(raw)} byte(s), expected 2")
    lsb, msb = raw[0], raw[1]
    if lsb == 2:
        text = "Interlock is OK"
    elif lsb == 1:
        text = "Waiting for interlock reset"
    elif lsb == 0:
        text = "Interlock off: " + _INTERLOCK_WHERE.get(msb, f"reason code {msb}")
    else:
        text = f"unknown interlock state {lsb}/{msb}"
    return {"lsb": lsb, "msb": msb, "ok": lsb == 2, "text": text}


def _u(data: bytes, size: int, what: str, *, signed: bool = False) -> int:
    if len(data) < size:
        raise InterbusError(f"{what}: {len(data)} byte(s) returned, {size} expected")
    return int.from_bytes(data[:size], "little", signed=signed)


def _status_flags(bits: int, table: dict[int, str]) -> list[str]:
    return [name for bit, name in table.items() if bits & (1 << bit)]


def _module_type(data: bytes, role: str) -> int:
    """Register 0x61: one byte for most modules, a 16-bit little-endian value for newer ones
    (the FIANIUM S4x3 is "0088h in a 16-bit unsigned integer" [SDK2 §6.23])."""
    if not data:
        raise InterbusError(f"{role} module type: empty reply")
    return int.from_bytes(data[:2], "little")


# ── driver ────────────────────────────────────────────────────────────────────────


class NktSuperK(TunableLaser):
    """SuperK FIANIUM (main) + RF driver + SELECT on one Interbus port."""

    #: ``power`` here is the main board's power level (0x37) - only for documented main types
    SUPPORTS = frozenset({"emission", "wavelength", "amplitude", "power"})

    def __init__(self, *, port: str | None = None, baudrate: int = 115200,
                 transport: SerialLike | None = None, device_id: str = "", name: str = "",
                 limits: LaserLimits | None = None, allow_emission_control: bool = False,
                 write_enabled: bool = False, addresses: dict[str, int | None] | None = None,
                 main_write_types: tuple[int, ...] | list[int] = (TYPE_EXTREME_MAIN, TYPE_FIANIUM_G3),
                 emission_wait_s: float = 10.0, io_timeout_s: float = 0.5,
                 emission_guard: EmissionGuard | None = None):
        super().__init__(device_id=device_id, name=name, limits=limits,
                         allow_emission_control=allow_emission_control, write_enabled=write_enabled,
                         emission_guard=emission_guard)
        if transport is None and not port:
            raise InstrumentError(f"nkt_superk {device_id or ''}: a COM port is required")
        self._port = port
        self._baudrate = int(baudrate)
        self._transport: SerialLike | None = transport
        self._owns_transport = transport is None
        self.addresses: dict[str, int | None] = dict(DEFAULT_ADDRESSES)
        for k, v in (addresses or {}).items():
            if k not in DEFAULT_ADDRESSES:
                raise InstrumentError(f"nkt_superk addresses: unknown module role {k!r} "
                                      f"(known: {sorted(DEFAULT_ADDRESSES)})")
            self.addresses[k] = None if v is None else int(v)
        self.main_write_types = tuple(int(t) for t in main_write_types)
        self.emission_wait_s = float(emission_wait_s)
        self._link = InterbusLink(self._get_transport, timeout_s=io_timeout_s,
                                  on_io_error=self._drop_transport, name=f"{device_id or 'NKT'}: Interbus port")
        self._types: dict[str, int] = {}          # role -> verified module type
        #: what the last emission_off() managed (for the skill's report)
        self.last_off_report: dict[str, Any] = {}

    # -- transport -----------------------------------------------------------------

    def _get_transport(self) -> SerialLike:
        t = self._transport
        if t is not None:
            return t
        settings = SerialSettings(port=self._port or "", baudrate=self._baudrate,
                                  timeout=self._link.timeout_s, write_timeout=self._link.timeout_s)
        t = open_transport(settings)
        try:
            t.open()
        except SerialUnavailable as exc:
            raise InstrumentUnavailable(
                f"NKT SuperK on {self._port}: {exc}"
                + tr(" —— 串口是独占的：NKT CONTROL 开着就打不开（它的 Quick Connect 会扫所有串口，连别的串口也一并占；"
                     "请关掉 CONTROL，或只用 Manual Connect 只打开 NKT 自己的口）。",
                     " -- the serial port is exclusive: it cannot be opened while NKT CONTROL is running (its Quick "
                     "Connect scans and holds every serial port; close CONTROL, or use Manual "
                     "Connect on the NKT port only).")
                + serial_open_hint(self._port)) from exc
        self._transport = t
        return t

    def _drop_transport(self) -> None:
        t, self._transport = self._transport, None
        self._types.clear()
        if t is not None and self._owns_transport:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass

    # -- module access ----------------------------------------------------------------

    def _addr(self, role: str) -> int:
        a = self.addresses.get(role)
        if a is None:
            raise OperationNotSupported(f"{self.device_id}: no {role} module configured (addresses.{role} is null)")
        return int(a)

    def _verified(self, role: str) -> int:
        """The module's address, after checking once per connection that it is what we think it is."""
        addr = self._addr(role)
        if role not in self._types:
            t = _module_type(self._link.read(addr, REG_TYPE), role)
            if t not in _EXPECTED_TYPES[role]:
                raise InterbusError(
                    f"module at address {addr} ({role}) reports type 0x{t:02X}, expected one of "
                    f"{[hex(x) for x in _EXPECTED_TYPES[role]]} - wrong address in the device config?")
            self._types[role] = t
        return addr

    def _require_main_writes(self, what: str) -> int:
        addr = self._verified("main")
        t = self._types["main"]
        if t not in self.main_write_types:
            raise OperationNotSupported(tr(
                f"{self.device_id}: {what} 未启用——设备配置的 main_write_types={[hex(x) for x in self.main_write_types]} "
                f"不含主机模块类型 0x{t:02X}（有出处的寄存器表是 0x60 和 0x88，见 NKT SDK 手册 §6.12 / §6.23）。"
                "要让 MAST 控制主机就把它加进 main_write_types；否则请在 FIANIUM 面板 / NKT CONTROL 里手动操作主机。",
                f"{self.device_id}: {what} is not enabled - main_write_types={[hex(x) for x in self.main_write_types]} "
                f"in the device config does not include the main module's type 0x{t:02X} (the sourced register files "
                "are 0x60 and 0x88, NKT SDK manual §6.12 / §6.23). Add it to main_write_types to let MAST control the "
                "main board; otherwise operate it at the FIANIUM panel / in NKT CONTROL."))
        return addr

    def _read(self, role: str, reg: int) -> bytes:
        return self._link.read(self._verified(role), reg)

    def _try(self, fn: Callable[[], Any], errors: dict[str, str], key: str) -> Any:
        try:
            return fn()
        except InterbusTimeout as exc:           # one silent module must not hide what the others say
            errors[key] = str(exc)
            return None
        except InstrumentUnavailable:            # the port itself is gone: nothing else will work
            raise
        except InstrumentError as exc:
            errors[key] = str(exc)
            return None

    # -- identification -------------------------------------------------------------------

    def _module_info(self, role: str) -> dict[str, Any]:
        addr = self._addr(role)
        info: dict[str, Any] = {"role": role, "address": addr}
        errors: dict[str, str] = {}
        t = _module_type(self._link.read(addr, REG_TYPE), role)
        info["type"], info["type_hex"] = t, f"0x{t:02X}"
        info["type_expected"] = t in _EXPECTED_TYPES[role]
        if info["type_expected"]:
            self._types[role] = t
        raw = self._try(lambda: self._link.read(addr, REG_SERIAL), errors, "serial")
        if raw is not None:
            info["serial_number"] = raw.split(b"\x00")[0].decode("ascii", errors="replace").strip()
        raw = self._try(lambda: self._link.read(addr, REG_FIRMWARE), errors, "firmware")
        if raw is not None:
            info["firmware_code_hex"] = raw.hex()
        raw = self._try(lambda: self._link.read(addr, REG_STATUS), errors, "status_bits")
        if raw is not None:
            bits = _u(raw, 2, "status bits")
            info["status_bits"] = bits
            if role == "main" and t in MAIN_MAPS:
                info["status_flags"] = _status_flags(bits, MAIN_MAPS[t].status_bits)
        if errors:
            info["errors"] = errors
        return info

    def identify(self) -> dict:
        """Read-only self-check: type / serial / firmware / status bits of the configured modules
        (registers 0x61, 0x65, 0x64, 0x66 - common to all modules [SDK §6.1, §2.2]). Writes nothing."""
        out: dict[str, Any] = {"device_id": self.device_id, "port": self._port, "modules": {}}
        problems: dict[str, str] = {}
        for role in ("main", "rf_driver", "select"):
            if self.addresses.get(role) is None:
                continue
            try:
                out["modules"][role] = self._module_info(role)
            except InterbusTimeout as exc:
                problems[role] = str(exc)
            except InstrumentUnavailable:
                raise
            except InstrumentError as exc:
                problems[role] = str(exc)
        main = out["modules"].get("main") or {}
        if main:
            mm = MAIN_MAPS.get(main.get("type"))
            out["main_register_file"] = (f"{mm.name}: documented ({mm.source})" if mm else
                                         "none for this module type - the main board is neither read nor written")
            out["main_writes_allowed"] = main.get("type") in self.main_write_types and mm is not None
        if problems:
            out["problems"] = problems
        out["ok"] = not problems and all(m.get("type_expected") for m in out["modules"].values())
        return out

    # -- reads ------------------------------------------------------------------------------

    def main_emission(self) -> tuple[bool | None, int | None]:
        """``(state, raw)`` of the main board's emission register: 3 = on, 0 = off, anything else
        (the module passes through other values while switching [SDK §6.8]) = unknown."""
        addr = self._verified("main")
        try:
            raw = _u(self._link.read(addr, REG_EMISSION), 1, "emission")
        except InterbusNack:
            raw = None
        if raw is not None:
            return (True if raw == EMISSION_ON else False if raw == EMISSION_OFF else None), raw
        # no emission register: fall back to the status bit the manual calls "Emission on" (type 0x60 family)
        try:
            bits = _u(self._link.read(addr, REG_STATUS), 2, "status bits")
        except InstrumentError:
            return None, None
        return bool(bits & 1), None

    def rf_power(self) -> bool | None:
        try:
            return bool(_u(self._read("rf_driver", REG_RF_POWER), 1, "RF power"))
        except InterbusNack:
            return None

    def connected_crystal(self) -> int | None:
        """Index of the crystal the RF driver currently feeds (0 = none connected) [SDK §6.9]."""
        return _u(self._read("rf_driver", REG_CONNECTED_CRYSTAL), 1, "connected crystal")

    def crystal_range_nm(self) -> tuple[float, float]:
        """Usable wavelength range of the *connected* crystal, from the RF driver (pm -> nm)."""
        lo = _u(self._read("rf_driver", REG_RF_MIN_PM), 4, "crystal min wavelength")
        hi = _u(self._read("rf_driver", REG_RF_MAX_PM), 4, "crystal max wavelength")
        return lo / 1000.0, hi / 1000.0

    def select_ranges_nm(self) -> dict[int, tuple[float, float]]:
        """``{1: (min, max), 2: (min, max)}`` of the two SELECT crystals [SDK §6.10]."""
        rd = lambda reg: _u(self._read("select", reg), 4, f"SELECT register 0x{reg:02X}") / 1000.0  # noqa: E731
        return {1: (rd(REG_CRYSTAL1_MIN), rd(REG_CRYSTAL1_MAX)),
                2: (rd(REG_CRYSTAL2_MIN), rd(REG_CRYSTAL2_MAX))}

    def interlock(self) -> dict[str, Any]:
        return decode_interlock(self._read("main", REG_INTERLOCK))

    def _channel_nm(self, ch: int) -> float:
        raw = self._read("rf_driver", REG_WAVELENGTH0 + ch)
        return _u(raw, 4, f"channel {ch} wavelength") / 1000.0      # element 0 of the 4 x u32 array

    def _channel_pct(self, ch: int) -> float:
        return _u(self._read("rf_driver", REG_AMPLITUDE0 + ch), 2, f"channel {ch} amplitude") / 10.0

    def _channels_raw(self) -> list[dict]:
        out = []
        for ch in range(N_CHANNELS):
            nm, pct = self._channel_nm(ch), self._channel_pct(ch)
            out.append({"channel": ch, "wavelength_nm": nm, "amplitude_pct": pct, "enabled": pct > 0})
        return out

    def get_wavelength_nm(self, *, channel: Any = 0) -> float | None:
        return self._channel_nm(self._check_channel(channel))

    def _check_channel(self, channel: Any) -> int:
        # the RF driver has eight channels [SDK §6.9]; no need to read all sixteen registers to know that
        try:
            ch = int(float(channel))
        except (TypeError, ValueError) as exc:
            raise LaserLimitError(f"{self.device_id}: channel must be an integer, got {channel!r}") from exc
        if not 0 <= ch < N_CHANNELS:
            raise LaserLimitError(f"{self.device_id}: channel {ch} outside [0, {N_CHANNELS - 1}]")
        return ch

    def _get_power_raw(self) -> float | None:
        """Main-board power level in % (0x37, 0.1 % steps); None if the module has no such register."""
        try:
            return _u(self._read("main", REG_POWER_LEVEL), 2, "power level") / 10.0
        except InterbusNack:
            return None

    def _status_raw(self) -> dict:
        out: dict[str, Any] = {"port": self._port, "model": "NKT SuperK FIANIUM + SELECT",
                               "modules": {}}
        errors: dict[str, str] = {}
        for role in ("main", "rf_driver", "select"):
            if self.addresses.get(role) is None:
                continue
            try:
                out["modules"][role] = self._module_info(role)
            except InterbusTimeout as exc:
                errors[role] = str(exc)
            except InstrumentUnavailable:
                raise
            except InstrumentError as exc:
                errors[role] = str(exc)
        main = out["modules"].get("main")
        if main and main.get("type_expected"):
            regs: dict[str, Any] = {}
            em = self._try(self.main_emission, errors, "emission")
            if em is not None:
                out["emission"], regs["emission_raw"] = em
            else:
                out["emission"] = None
            inter = self._try(self.interlock, errors, "interlock")
            if inter is not None:
                out["interlock"] = inter
            mm = MAIN_MAPS[main["type"]]
            for key, (reg, size, signed, scale) in {**mm.readings, "setup_mode_code": (REG_SETUP, 2, False, 1)}.items():
                v = self._try(lambda reg=reg, size=size, signed=signed, key=key:
                              _u(self._read("main", reg), size, key, signed=signed), errors, key)
                if v is not None:
                    regs[key] = round(v * scale, 3) if scale != 1 else v
            if "setup_mode_code" in regs:
                regs["setup_mode"] = mm.setup_modes.get(regs["setup_mode_code"], "unknown")
            regs["model"] = mm.name
            out["main_registers"] = regs
            out["main_writes_allowed"] = main.get("type") in self.main_write_types
        else:
            out["emission"] = None
        rf = out["modules"].get("rf_driver")
        if rf and rf.get("type_expected"):
            out["rf_power"] = self._try(self.rf_power, errors, "rf_power")
            out["connected_crystal"] = self._try(self.connected_crystal, errors, "connected_crystal")
            rng = self._try(self.crystal_range_nm, errors, "crystal_range")
            if rng is not None:
                out["crystal_range_nm"] = list(rng)
            temp = self._try(lambda: _u(self._read("rf_driver", REG_CRYSTAL_TEMP), 2, "crystal temp",
                                        signed=True) / 10.0, errors, "crystal_temperature")
            if temp is not None:
                out["crystal_temperature_c"] = temp
            ch = self._try(self._channels_raw, errors, "channels")
            if ch is not None:
                out["channels"] = ch
        sel = out["modules"].get("select")
        if sel and sel.get("type_expected"):
            rng2 = self._try(self.select_ranges_nm, errors, "select_ranges")
            if rng2 is not None:
                out["select_crystal_ranges_nm"] = {str(k): list(v) for k, v in rng2.items()}
            sw = self._try(lambda: _u(self._read("select", REG_RF_SWITCH), 1, "RF switch"), errors, "rf_switch")
            if sw is not None:
                out["rf_switch"] = sw
        out["output_gating"] = ("The SELECT output is live only when the main emission is on AND the RF power is on "
                                "AND a channel amplitude is above 0.")
        if errors:
            out["errors"] = errors
        return out

    def _emission_state_raw(self) -> bool | None:
        try:
            return self.main_emission()[0]
        except InstrumentError:                  # unknown is None, never an exception (contract: bool | None)
            return None

    # -- writes: SELECT channels ----------------------------------------------------------------

    def _write_verified(self, role: str, reg: int, data: bytes, what: str,
                        check: Callable[[bytes], bool]) -> bytes:
        addr = self._verified(role)
        self._link.write(addr, reg, data)                     # ack or silence; both are verified below
        back = self._link.read(addr, reg)
        if not check(back):
            raise InterbusError(f"{what}: wrote {data.hex()} to register 0x{reg:02X}, read back {back.hex()}")
        return back

    def _set_wavelength_raw(self, channel: int, nm: float) -> float:
        self._verified("rf_driver")
        crystal = self.connected_crystal()
        if not crystal:
            raise InstrumentError(tr(f"{self.device_id}: RF driver 报告没有接晶体（RF 线没接上，或 SELECT 的 RF 开关位置不对）。",
                                     f"{self.device_id}: the RF driver reports no crystal connected "
                                     "(RF cable unplugged, or the SELECT's RF switch is in an odd position)."))
        lo, hi = self.crystal_range_nm()
        if not lo <= nm <= hi:
            other = {}
            try:
                other = self.select_ranges_nm()
            except InstrumentError:
                pass
            hint = next((f" Crystal {c} covers {a:g}-{b:g} nm - select it first (select_crystal)."
                         for c, (a, b) in other.items() if a <= nm <= b and c != crystal), "")
            raise LaserLimitError(f"{self.device_id}: wavelength {nm} nm is outside the connected crystal "
                                  f"{crystal}'s usable range [{lo:g}, {hi:g}] nm.{hint}")
        pm = int(round(nm * 1000.0))
        data = pm.to_bytes(4, "little")
        back = self._write_verified("rf_driver", REG_WAVELENGTH0 + channel, data, f"channel {channel} wavelength",
                                    lambda b: len(b) >= 4 and abs(int.from_bytes(b[:4], "little") - pm) <= 50)
        return int.from_bytes(back[:4], "little") / 1000.0

    def _may_be_live(self) -> bool:
        """False only when the chain is *known* dark: main emission off, or RF power off. Raises an
        ``InstrumentError`` when a state cannot be read - the callers count that as "may be live"."""
        if self.addresses.get("main") is not None and self.main_emission()[0] is False:
            return False
        if self.addresses.get("rf_driver") is not None and self.rf_power() is False:
            return False
        return True

    def _adds_light_on_channel(self, channel: int, pct: float) -> bool:
        """Whether writing ``pct`` puts more light on the sample than there is now. False only when that is
        *known* not to happen: 0 %, a chain known to be dark, or a level that is not higher than the channel's
        present one (compared in the register's 0.1 % steps). Anything unreadable counts as "adds" (fail closed)."""
        if pct <= 0:
            return False
        try:
            if not self._may_be_live():
                return False
            return int(round(pct * 10.0)) > int(round(self._channel_pct(channel) * 10.0))
        except InstrumentError:
            return True

    def _adds_light_at_level(self, level_register: int, value: float) -> bool:
        """The same question for the main output level: more light only if the level goes up while the chain is, or
        may be, live. Anything unreadable counts as "adds"."""
        if value <= 0:
            return False
        try:
            if not self._may_be_live():
                return False
            return int(round(value * 10.0)) > _u(self._read("main", level_register), 2, "output level")
        except InstrumentError:
            return True

    def _set_amplitude_raw(self, channel: int, pct: float) -> float:
        if self._adds_light_on_channel(channel, pct):
            self._check_emission_gates(f"channel {channel} amplitude {pct:g} % (adds light)")
        permille = int(round(pct * 10.0))
        data = permille.to_bytes(2, "little")
        back = self._write_verified("rf_driver", REG_AMPLITUDE0 + channel, data, f"channel {channel} amplitude",
                                    lambda b: len(b) >= 2 and int.from_bytes(b[:2], "little") == permille)
        return int.from_bytes(back[:2], "little") / 10.0

    def _set_power_raw(self, value: float) -> float:
        """Main-board level setpoint (%), only for main-board types with a sourced register file. The
        register *in effect* depends on the type and the setup mode (0x31): EXTREME constant power ->
        0x37, constant current -> 0x38 [SDK §6.8]; FIANIUM internal power control -> 0x37 "output level"
        [SDK2 §6.23]; every other mode (modulated, power lock) is refused."""
        addr = self._require_main_writes("set power")
        mm = MAIN_MAPS[self._types["main"]]
        mode = _u(self._link.read(addr, REG_SETUP), 2, "setup mode")
        reg = mm.level_register.get(mode)
        if reg is None:
            raise OperationNotSupported(f"{self.device_id}: setup mode {mode} "
                                        f"({mm.setup_modes.get(mode, 'unknown')}) has no plain level register to write")
        if self._adds_light_at_level(reg, value):
            self._check_emission_gates(f"output level {value:g} % (adds light)")
        permille = int(round(value * 10.0))
        back = self._write_verified("main", reg, permille.to_bytes(2, "little"), "power level",
                                    lambda b: len(b) >= 2 and int.from_bytes(b[:2], "little") == permille)
        return int.from_bytes(back[:2], "little") / 10.0

    # -- writes: RF power, crystal, emission ---------------------------------------------------------

    def set_rf_power(self, on: bool) -> bool:
        """RF power of the RF driver [SDK §6.9]: 0 = off (SELECT output dark), 1 = on. *On* opens the output, so it
        passes the same gates as emission on (config, write_enabled, operator guard); *off* is the safe direction:
        no gate, always attempted."""
        if on:
            with self._adds_light("RF power on"):          # gates early, device lock, gates again (and not pre-empted)
                self._write_verified("rf_driver", REG_RF_POWER, bytes([1]), "RF power",
                                     lambda b: len(b) >= 1 and b[0] == 1)
            return True
        with safe_direction():                             # off: no endless wait on a busy device
            with held(self._lock, f"{self.device_id}: device lock"):
                self._write_verified("rf_driver", REG_RF_POWER, bytes([0]), "RF power",
                                     lambda b: len(b) >= 1 and b[0] == 0)
        return False

    def select_crystal(self, crystal: int) -> dict[str, Any]:
        """Switch the SELECT's internal RF switch so the RF driver feeds crystal 1 or 2.

        Sequence [SDK §6.10, CTL §3.11.2]: RF power off (mandatory before the switch moves) ->
        RF switch register toggled -> connected crystal re-read until it is the wanted one.
        **RF power is left OFF** afterwards: the channel wavelengths in the registers belong to the
        other crystal, so the output must not open before they are set for this one."""
        if crystal not in (1, 2):
            raise LaserLimitError(f"{self.device_id}: crystal must be 1 or 2, got {crystal!r}")
        self._require_writes("select crystal")
        with self._lock:
            current = self.connected_crystal()
            if current == crystal:
                return {"crystal": crystal, "changed": False, "rf_power": self.rf_power()}
            if not current:
                raise InstrumentError(f"{self.device_id}: no crystal reported by the RF driver - cannot tell "
                                      "which way to switch")
            was_on = self.rf_power()
            self.set_rf_power(False)                       # off is always allowed; documented precondition
            sel = self._verified("select")
            sw = _u(self._link.read(sel, REG_RF_SWITCH), 1, "RF switch")
            new = 1 - (sw & 1)
            self._link.write(sel, REG_RF_SWITCH, bytes([new]))
            deadline = _monotonic() + 3.0
            while True:
                now = self.connected_crystal()
                if now == crystal:
                    return {"crystal": crystal, "changed": True, "rf_power_was_on": bool(was_on),
                            "rf_power": False,
                            "note": "RF power left off - set the channels for this crystal, then switch it on"}
                if _monotonic() >= deadline:
                    self._link.write(sel, REG_RF_SWITCH, bytes([sw]))        # put it back
                    raise InterbusError(f"{self.device_id}: the RF driver still reports crystal {now} after the "
                                        f"RF switch was set to {new} (wanted {crystal}); switch restored")
                _sleep(0.1)

    def _emission_raw(self, on: bool) -> bool:
        return self._switch_on() if on else self._switch_off()

    def _switch_on(self) -> bool:
        main_state, _raw = self.main_emission()
        wrote = False
        try:
            if main_state is not True:
                addr = self._require_main_writes("emission on")
                il = self.interlock()
                if not il["ok"]:
                    raise InstrumentError(tr(
                        f"联锁没有就绪：{il['text']}。软件不会复位联锁——请在现场（钥匙、门、外部总线）处理。",
                        f"The interlock is not ready: {il['text']}. Software never resets the interlock - fix it "
                        "at the system (key, door, external bus)."))
                wrote = True
                self._link.write(addr, REG_EMISSION, bytes([EMISSION_ON]))
                deadline = _monotonic() + self.emission_wait_s
                while True:
                    state, raw = self.main_emission()
                    if state is True:
                        break
                    self._check_not_preempted("emission on")           # an emission-off asked for since the switch-on began
                    if _monotonic() >= deadline:
                        raise InstrumentError(tr(
                            f"{self.emission_wait_s:g} s 内主机发射寄存器没到“开”（读到 {raw}）。",
                            f"The emission register did not reach 'on' within {self.emission_wait_s:g} s "
                            f"(read {raw})."))
                    _sleep(0.2)
            # the SELECT output is gated by the RF power
            if self.addresses.get("rf_driver") is not None:
                self.set_rf_power(True)
            return True
        except BaseException:
            # After the first write the laser is not known to be off any more: go to the safe side
            # before the error propagates. (A refusal before any write needs no such cleanup - and
            # must not switch off a laser somebody else had on.)
            if wrote:
                try:
                    with safe_direction():           # a stuck port must not hold the failing switch-on hostage
                        self._switch_off()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("NKT %s: switch-off after a failed switch-on also failed: %s",
                                   self.device_id, exc)
            raise

    def _switch_off(self) -> bool:
        """Independent steps, none may stop the others; returns the main emission read-back."""
        report: dict[str, Any] = {"errors": []}
        state: bool | None = None
        if self.addresses.get("main") is not None:
            try:
                # 0x30 = 0 is "emission off" for every NKT laser module in the manual (§6.2/6.3/6.8)
                addr = self._addr("main")
                self._link.write(addr, REG_EMISSION, bytes([EMISSION_OFF]))
                state, raw = self.main_emission()
                report["main_emission"], report["main_emission_raw"] = state, raw
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(f"main emission off: {exc}")
                report["lock_timeout"] = report.get("lock_timeout", False) or isinstance(exc, LockTimeout)
                logger.warning("NKT %s: main emission off failed: %s", self.device_id, exc)
        if self.addresses.get("rf_driver") is not None:
            try:
                addr = self._addr("rf_driver")
                self._link.write(addr, REG_RF_POWER, bytes([0]))
                rf = _u(self._link.read(addr, REG_RF_POWER), 1, "RF power")
                report["rf_power"] = bool(rf)
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(f"RF power off: {exc}")
                report["lock_timeout"] = report.get("lock_timeout", False) or isinstance(exc, LockTimeout)
                logger.warning("NKT %s: RF power off failed: %s", self.device_id, exc)
        self.last_off_report = report
        if state is None and report.get("rf_power") is None and report["errors"]:
            raise InstrumentError("; ".join(report["errors"]))
        return bool(state)

    def close(self) -> None:
        self._drop_transport()


def build(entry: dict) -> NktSuperK:
    """Registry factory (``registry._STATION_DRIVERS['nkt_superk']``). Opens nothing."""
    dev_id = str(entry.get("id", ""))
    port = str(entry.get("port") or "").strip()
    if not port:
        raise InstrumentError(f"optical device {dev_id!r} (nkt_superk): 'port' is required")
    return NktSuperK(
        port=port, baudrate=int(entry.get("baudrate", 115200)),
        device_id=dev_id, name=str(entry.get("name") or dev_id),
        limits=LaserLimits.from_dict(entry.get("limits")),
        allow_emission_control=bool(entry.get("allow_emission_control", False)),
        write_enabled=bool(entry.get("write_enabled", False)),
        addresses=entry.get("addresses") or None,
        main_write_types=entry.get("main_write_types", [TYPE_EXTREME_MAIN, TYPE_FIANIUM_G3]),
        emission_wait_s=float(entry.get("emission_wait_s", 10.0)),
        emission_guard=default_emission_guard(bool(entry.get("require_laser_attestation", True))))
