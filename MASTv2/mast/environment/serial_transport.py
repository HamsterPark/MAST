"""Serial transport for RS-485 / RS-232 environment sensors.

Thin wrapper over *pyserial* so the rest of ``mast.environment`` never imports
``serial`` directly. Two design constraints drive this module:

1. **No-hardware must not crash** (explicit user requirement: 没有真空计或
   温度计，要求程序也不崩溃). When pyserial is absent, when no COM port exists,
   or when opening a port fails, this layer degrades gracefully —
   :func:`list_serial_ports` returns ``[]`` and :class:`SerialTransport`
   raises :class:`SerialUnavailable`, which the sensor classes catch to report
   ``status="unavailable"`` / ``"error"`` instead of propagating.

2. **Testable without hardware.** Sensors talk to a transport through the
   tiny :class:`SerialLike` protocol (just ``transact`` / ``close``), so unit
   tests inject a fake transport with canned frames — no serial port, no
   pyserial, no DL-7 / Lakeshore on the bench.

Half-duplex RS-485 buses (DL-7) and request/response SCPI links (Lakeshore)
are both *transactional*: write a request, read the reply. :meth:`transact`
encapsulates that with an input-buffer flush, a bounded read, and short
timeouts so a silent/wrong device can never hang the monitor thread.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── pyserial availability (probed once, lazily importable) ───────────────
try:  # pragma: no cover - trivial import guard
    import serial as _pyserial  # type: ignore
    from serial.tools import list_ports as _list_ports  # type: ignore

    HAS_PYSERIAL = True
except Exception:  # pragma: no cover - exercised on machines without pyserial
    _pyserial = None  # type: ignore
    _list_ports = None  # type: ignore
    HAS_PYSERIAL = False


class SerialUnavailable(RuntimeError):
    """Raised when a serial port cannot be opened (no pyserial / no port)."""


@dataclass(frozen=True)
class SerialPortInfo:
    """One enumerated serial port."""

    device: str           # e.g. "COM15" / "/dev/ttyUSB0"
    description: str = ""  # human string from the OS, e.g. "USB-SERIAL CH340"
    hwid: str = ""         # VID:PID etc.


@dataclass(frozen=True)
class SerialSettings:
    """Serial line parameters. String parity ('N'/'E'/'O') keeps this importable
    without pyserial; it is mapped to the pyserial constant only at open()."""

    port: str
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"        # 'N' | 'E' | 'O' | 'M' | 'S'
    stopbits: float = 1.0    # 1 | 1.5 | 2
    timeout: float = 0.4     # read timeout (s) — short so a dead bus can't hang
    write_timeout: float = 0.4

    def label(self) -> str:
        return f"{self.port}@{self.baudrate} {self.bytesize}{self.parity}{int(self.stopbits)}"


@runtime_checkable
class SerialLike(Protocol):
    """Minimal interface the sensor drivers depend on (real or fake)."""

    def transact(
        self,
        payload: bytes,
        *,
        read_size: int | None = ...,
        read_until: bytes | None = ...,
        timeout: float | None = ...,
    ) -> bytes: ...

    def close(self) -> None: ...


def list_serial_ports() -> list[SerialPortInfo]:
    """Enumerate available serial ports. Returns ``[]`` if pyserial is absent
    or enumeration fails — never raises."""
    if not HAS_PYSERIAL:
        return []
    try:
        out: list[SerialPortInfo] = []
        for p in _list_ports.comports():
            out.append(
                SerialPortInfo(
                    device=getattr(p, "device", "") or "",
                    description=getattr(p, "description", "") or "",
                    hwid=getattr(p, "hwid", "") or "",
                )
            )
        return out
    except Exception as exc:  # pragma: no cover - OS quirk
        logger.debug("list_serial_ports failed: %s", exc)
        return []


_PARITY_MAP = {"N": "PARITY_NONE", "E": "PARITY_EVEN", "O": "PARITY_ODD",
               "M": "PARITY_MARK", "S": "PARITY_SPACE"}
_STOPBITS_MAP = {1.0: "STOPBITS_ONE", 1.5: "STOPBITS_ONE_POINT_FIVE",
                 2.0: "STOPBITS_TWO"}
_BYTESIZE_MAP = {5: "FIVEBITS", 6: "SIXBITS", 7: "SEVENBITS", 8: "EIGHTBITS"}

#: Bounds for one terminator-delimited read. A device that streams without ever
#: sending the terminator must not hold the port (and its lock) indefinitely.
_MAX_REPLY_BYTES = 8192
_MAX_READ_S_FLOOR = 5.0


def is_complete_line(raw: "bytes | bytearray | None", terminator: bytes = b"\n") -> bool:
    """True when *raw* ends with *terminator*, i.e. the instrument finished its answer.

    A reply that stops short of its terminator is not a shorter answer but a
    broken one: ``+14.6800`` cut after two bytes parses as ``+1``, and
    ``+1.2E-01`` cut after seven as ``+1.2E-0``, ten times too large. Drivers
    that turn replies into values check this before parsing.
    """
    return bool(raw) and bytes(raw).endswith(terminator)


def _end_of_nth(buf: "bytes | bytearray", term: bytes, n: int) -> "int | None":
    """Index just past the *n*-th *term* in *buf*, or ``None`` if there are fewer."""
    start = 0
    for _ in range(n):
        i = buf.find(term, start)
        if i < 0:
            return None
        start = i + len(term)
    return start


def _read_lines(ser, term: bytes, lines: int, timeout: "float | None") -> bytes:
    """Read until *lines* terminators have arrived, or no byte arrives within the timeout.

    This replaces pyserial's ``read_until``. That function reads one byte per call
    and checks a single absolute deadline after every byte. Each ``read(1)`` is
    several ctypes calls, and each call hands the GIL back. With other Python
    threads busy, every byte waits for the GIL and the deadline passes in the
    middle of a reply that has already arrived.

    Observed at MAST start-up: auto-detection logged that the Lake
    Shore 625 "answered but was not recognised". The same probe run on its own
    identified the supply 7 times out of 7. With three busy threads in the
    process, the 31-byte ``*IDN?`` answer came back as ``b'LSCI,M'``.

    Here the timeout bounds only the wait for the NEXT byte, and whatever the
    driver has already buffered is taken in a single ``read``. A starved thread
    therefore still collects a reply that has fully arrived.

    Returns the bytes up to and including the *lines*-th terminator. Anything
    after that is dropped; the next transaction flushes the input buffer anyway.
    A reply that stops before its terminator is returned unchanged; see
    :func:`is_complete_line`.
    """
    want = max(1, int(lines))
    buf = bytearray()
    budget = max(10.0 * float(timeout or 0.0), _MAX_READ_S_FLOOR)
    hard_stop = time.monotonic() + budget
    while len(buf) < _MAX_REPLY_BYTES:
        try:
            waiting = int(getattr(ser, "in_waiting", 0) or 0)
        except Exception:  # noqa: BLE001 - only a shortcut; fall back to one byte
            waiting = 0
        room = _MAX_REPLY_BYTES - len(buf)
        chunk = bytes(ser.read(min(waiting, room) if waiting > 0 else 1) or b"")
        if not chunk:
            break  # nothing more within the read timeout: the device has stopped
        buf += chunk
        end = _end_of_nth(buf, term, want)
        if end is not None:
            return bytes(buf[:end])
        if time.monotonic() > hard_stop:
            logger.debug("serial read: %d byte(s), fewer than %d terminator(s) after "
                         "%.1f s; giving up", len(buf), want, budget)
            break
    return bytes(buf)


class SerialTransport:
    """Real pyserial-backed transport. Lazy — the port is opened on first use.

    Construction never raises; :meth:`open` / :meth:`transact` raise
    :class:`SerialUnavailable` so callers handle a missing port in one place.
    """

    #: Drivers that verify an instrument's identity before talking to it check this
    #: (``getattr(transport, "supports_generations", False)``) to know they can pin
    #: each query to the handle generation they verified.
    supports_generations = True
    #: ``transact(min_gap_s=...)`` is honoured (see there). Drivers check this the same way.
    supports_min_gap = True

    def __init__(self, settings: SerialSettings):
        self.settings = settings
        self._ser = None  # serial.Serial | None
        # Incremented on every successful open(). A COM *name* is not an identity:
        # unplug two USB adapters and plug them back in, and COM13/COM14 can swap
        # devices. A driver that checked ``*IDN?``
        # once and then let ``transact`` silently reopen the port by name would
        # be talking to whatever now answers on that name. Drivers record the
        # generation they verified and pass it back as ``expect_generation``.
        self._generation = 0
        # A request/response link is only coherent if one transaction completes
        # before the next begins. Two threads DO share a transport now: the
        # EnvironmentMonitor loop polls temperature every interval while the
        # settings panel reads instrument state (heater range/setpoint) on
        # demand — interleaved write/read would splice one reply onto the other
        # request. Guard the whole flush→write→read sequence, not just the write.
        self._io_lock = threading.RLock()
        # Message spacing (transact(min_gap_s=...)). When the last transaction on this
        # transport ended, and before when no transaction may start, whoever makes it.
        self._last_end = 0.0
        self._quiet_until = 0.0

    @property
    def open_generation(self) -> int:
        """How many times this transport has successfully opened its port.

        ``0`` = never opened. Paired with :attr:`is_open`, it tells a driver
        whether the handle it verified is still the handle it would use.
        """
        return self._generation

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if self._ser is not None:
            return
        if not HAS_PYSERIAL:
            raise SerialUnavailable("pyserial not installed")
        s = self.settings
        try:
            self._ser = _pyserial.Serial(
                port=s.port,
                baudrate=s.baudrate,
                bytesize=getattr(_pyserial, _BYTESIZE_MAP.get(s.bytesize, "EIGHTBITS")),
                parity=getattr(_pyserial, _PARITY_MAP.get(s.parity.upper(), "PARITY_NONE")),
                stopbits=getattr(_pyserial, _STOPBITS_MAP.get(float(s.stopbits), "STOPBITS_ONE")),
                timeout=s.timeout,
                write_timeout=s.write_timeout,
            )
        except Exception as exc:
            self._ser = None
            raise SerialUnavailable(f"open {s.label()} failed: {exc}") from exc
        self._generation += 1

    def close(self) -> None:
        # Deliberately does NOT take _io_lock. Closing the port is what CANCELS
        # a read that is blocked in the kernel — EnvironmentMonitor.stop() relies
        # on exactly that to unblock a wedged loop thread. Taking the lock here
        # would make close() wait on the very read it is meant to interrupt.
        ser = self._ser
        self._ser = None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # pragma: no cover
                pass

    @property
    def is_open(self) -> bool:
        return self._ser is not None and bool(getattr(self._ser, "is_open", False))

    # -- transactional I/O -------------------------------------------------
    def transact(
        self,
        payload: bytes,
        *,
        read_size: int | None = None,
        read_until: bytes | None = None,
        timeout: float | None = None,
        lines: int = 1,
        expect_generation: int | None = None,
        min_gap_s: float | None = None,
    ) -> bytes:
        """Write *payload*, then read the reply.

        Exactly one of ``read_size`` (Modbus: fixed-length frame) or
        ``read_until`` (SCPI: terminator-delimited line) should be given;
        if both are None a single short read is returned. Flushes stale input
        before writing so a previous partial reply can't corrupt this one.
        Raises :class:`SerialUnavailable` if the port isn't usable.

        ``lines`` (with ``read_until``) reads that many terminator-delimited
        lines in ONE locked transaction — for an instrument that echoes the
        command before answering it (Cryomagnetics over USB). Stops early on a
        read timeout; whatever arrived is returned. The timeout bounds the wait
        for each next byte, not the whole reply (:func:`_read_lines`), so a reply
        is not cut short because this thread was slow to get the GIL back. A
        reply that still ends without its terminator is returned as is: check
        :func:`is_complete_line` before parsing a value from it.

        ``expect_generation``: refuse (``SerialUnavailable``, nothing written)
        unless the port is open RIGHT NOW with exactly this open generation.
        Never reopens. This is how an identity-checked driver guarantees that a
        query is only ever written on the handle whose ``*IDN?`` it checked;
        reopening is left to the driver's identity probe.

        ``min_gap_s``: the instrument needs this much silence around every message
        (Lake Shore 335 manual §6.3.5: nothing for 50 ms after a command or a reply,
        at most 20 messages per second, or communication fails intermittently).
        Inside the I/O lock, this call waits until ``min_gap_s`` after the previous
        transaction on this transport ended, whoever made it, and the next
        transaction, whoever makes it, waits until ``min_gap_s`` after this one.
        Callers that do not pass it are spaced only after a call that did.
        """
        with self._io_lock:
            if expect_generation is not None:
                if self._ser is None or self._generation != expect_generation:
                    raise SerialUnavailable(
                        f"{self.settings.port}: handle is not the identity-verified "
                        f"one (want generation {expect_generation}, have "
                        f"{self._generation if self._ser is not None else 'closed'}); "
                        "nothing written")
            if self._ser is None:
                self.open()
            ser = self._ser
            if ser is None:  # pragma: no cover - open() would have raised
                raise SerialUnavailable("port not open")

            if timeout is not None:
                try:
                    ser.timeout = timeout
                except Exception:  # pragma: no cover
                    pass

            gap = float(min_gap_s) if min_gap_s else 0.0
            wait = max(self._quiet_until, self._last_end + gap) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                return self._exchange(ser, payload, read_size=read_size,
                                      read_until=read_until, timeout=timeout, lines=lines)
            finally:
                self._last_end = time.monotonic()
                if gap:
                    self._quiet_until = self._last_end + gap

    def _exchange(self, ser, payload: bytes, *, read_size, read_until, timeout,
                  lines: int) -> bytes:
        """flush -> write -> read. The caller holds the I/O lock."""
        try:
            try:
                ser.reset_input_buffer()
            except Exception:  # pragma: no cover
                pass
            ser.write(payload)
            try:
                ser.flush()
            except Exception:  # pragma: no cover
                pass

            if read_until is not None:
                # Not pyserial's read_until: see _read_lines for the start-up
                # failure it caused.
                data = _read_lines(ser, read_until, lines, timeout)
            elif read_size is not None:
                data = ser.read(read_size)
            else:
                data = ser.read(256)
            return bytes(data or b"")
        except SerialUnavailable:
            raise
        except Exception as exc:
            raise SerialUnavailable(
                f"transact on {self.settings.label()} failed: {exc}") from exc

    # context-manager sugar
    def __enter__(self) -> "SerialTransport":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_transport(settings: SerialSettings) -> SerialTransport:
    """Factory used by sensors; kept as a seam tests can monkeypatch."""
    return SerialTransport(settings)


__all__ = [
    "HAS_PYSERIAL",
    "SerialUnavailable",
    "SerialPortInfo",
    "SerialSettings",
    "SerialLike",
    "SerialTransport",
    "list_serial_ports",
    "open_transport",
]
