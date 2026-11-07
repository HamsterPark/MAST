"""TC100 temperature controller — Modbus RTU, 9600 baud, slave 1.

Contract: ``docs/v2/design/optical_station_witec.md`` §3 / §6.1 / D6 / 陷阱 7 and 13.

What is known, and from where
=============================
[DOC]  ``tc100DLL使用说明.docx`` (the vendor's note for ``TC100CSharpDLL.dll``): the DLL speaks
       Modbus RTU; ``Connect(port, baudRate [usually 9600], slaveId [usually 1])``,
       ``Run(setValue, spRateLimit, downRate, usePidFromFile)`` (target temperature, rise rate
       limit and fall rate, both in °C/min), ``Stop()``, ``Disconnect()``,
       ``GetParameters(out pv, sv, mv, status)`` - PV measured temperature, SV set value, MV
       control output in percent (-100..100), status ``0`` running / ``1`` stopped / ``2`` hold /
       ``3`` unknown -, ``SetPID(6 values)``, ``SetTemperatureDrift(t)``, ``GetLastError()``.
       **The document contains no register table, no function codes, no scaling and no parity.**
[CFG]  the vendor program's ``TC100.exe.Config`` holds ``Baud=9600  Addr=1``.
[PID]  the vendor's demo PID files (``PID_demo.txt``, ``QT.txt``) name the controller parameters by
       hex number: Ctrl 06, P 07, I 08, D 09, CTL cycle 0a, drift 10, OPH (output upper limit) 13,
       SPH (set-value upper limit) 1f, cooling P/I/D 25/26/27, CTL2 28. Those are parameter
       *numbers*; nothing says they are Modbus addresses, and PV / SV / MV / run state are not
       among them.
[LOG]  the vendor program's log may end with a run of "The operation has timed out" when the
       controller is off.

What is NOT known - therefore not hard-coded
============================================
The Modbus address, function code, scale, signedness of PV / SV / MV / status, the registers that
start and stop the controller and that carry the rate limits, the parity / stop bits of the line.
Following the rule *no source - no register*:

* the **register map is configuration** (``register_map`` in the device entry,
  :class:`Tc100RegisterMap`). Every entry defaults to *unconfigured*; an unconfigured quantity
  raises :class:`~mast.instruments.base.OperationNotSupported` with a message that says what is
  missing - the driver never guesses an address;
* writes are off (``write_enabled`` defaults to false) and additionally need the registers they use
  to be configured. That includes **stop**: the stop value goes into a register a person typed into the map, and
  ``write_enabled`` is the switch the operator throws after the bench self-test has shown the map right - a wrong
  map must not make an E-STOP or a cleanup write somewhere else. With ``write_enabled`` false ``stop()`` writes
  nothing (it does not even open the port), answers ``False`` ("not confirmed") and says to stop the controller in
  the TC100 program or at its panel;
* a setpoint with a ramp limit is refused unless the rate registers are configured - an unlimited
  jump must never be the silent fallback for a ramp the limits asked for;
* serial line: 9600 baud, 8 data bits, **parity none, 1 stop bit** as the default assumption (the
  usual NModbus setup; not documented - ``parity`` / ``stopbits`` are configurable).

How to fill the map in on the bench (read-only, nothing is written)
===================================================================
Close the vendor ``TC100.exe`` (it holds the COM port), then::

    dev = get_instrument_registry().temperature_controller("tc100")
    dev.scan_registers(function=3, start=0, count=32)      # then function=4

and compare the raw words with what ``TC100.exe`` shows for PV / SV / MV (a value that moves when
the stage temperature moves is PV; the set value you typed is SV). :meth:`Tc100.scan_registers`
issues only Modbus *read* requests (function 3 or 4, at most 32 registers).

State vocabulary (``temperature.STATES``): documented code 0 -> ``running``, 1 -> ``stopped``,
2 -> ``holding``, 3 / anything else -> ``unknown``. This is the DLL's ``GetParameters.status``;
that the raw status register uses the same codes is an **assumption** to confirm with the map.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, fields
from typing import Any, Mapping

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
from mast.instruments.pztc_nm003 import build_rtu_frame, parse_rtu_response
from mast.instruments.safe_direction import held
from mast.instruments.temperature import (
    TemperatureController,
    TemperatureLimitError,
    TemperatureLimits,
    TemperatureWriteDisabled,
)

logger = logging.getLogger(__name__)

__all__ = ["Tc100", "Tc100RegisterMap", "RegSpec", "DOCUMENTED_STATUS", "build"]

#: ``GetParameters.status`` per the vendor document ("设备状态说明")
DOCUMENTED_STATUS: dict[int, str] = {0: "running", 1: "stopped", 2: "holding", 3: "unknown"}

_READ_FUNCTIONS = (3, 4)
_WRITE_FUNCTIONS = (6, 16)
SCAN_MAX_COUNT = 32


@dataclass(frozen=True)
class RegSpec:
    """One quantity in the controller's Modbus space (configuration, not knowledge)."""

    address: int
    function: int = 3             # read function: 3 holding registers or 4 input registers
    scale: float = 1.0            # value = raw * scale + offset
    offset: float = 0.0
    signed: bool = False
    words: int = 1                # 1 or 2 registers
    write_function: int = 6       # 6 = write single register, 16 = write multiple registers
    verify: bool = True           # read a written value back (function 3) and compare

    def __post_init__(self) -> None:
        if not 0 <= int(self.address) <= 0xFFFF:
            raise ValueError(f"register address {self.address} outside 0..65535")
        if self.function not in _READ_FUNCTIONS:
            raise ValueError(f"read function must be 3 or 4, got {self.function}")
        if self.write_function not in _WRITE_FUNCTIONS:
            raise ValueError(f"write function must be 6 or 16, got {self.write_function}")
        if self.words not in (1, 2):
            raise ValueError(f"words must be 1 or 2, got {self.words}")
        if self.words == 2 and self.write_function == 6:
            object.__setattr__(self, "write_function", 16)       # a 32-bit value needs the multi-register write
        if not float(self.scale):
            raise ValueError("scale must be non-zero")

    @classmethod
    def from_value(cls, raw: Any) -> "RegSpec | None":
        if raw is None:
            return None
        if isinstance(raw, RegSpec):
            return raw
        if isinstance(raw, int) and not isinstance(raw, bool):
            return cls(address=raw)                                # shorthand: just an address
        if isinstance(raw, Mapping):
            known = {f.name for f in fields(cls)}
            unknown = set(raw) - known
            if unknown:
                raise ValueError(f"register spec: unknown keys {sorted(unknown)} (known: {sorted(known)})")
            return cls(**dict(raw))
        raise ValueError(f"register spec must be an address or an object, got {raw!r}")

    def describe(self) -> dict:
        return {"address": self.address, "function": self.function, "scale": self.scale,
                "offset": self.offset, "signed": self.signed, "words": self.words}


@dataclass(frozen=True)
class Tc100RegisterMap:
    """All entries default to *unconfigured*; see the module docstring."""

    # reads
    pv: RegSpec | None = None
    sv: RegSpec | None = None
    mv: RegSpec | None = None
    status: RegSpec | None = None
    # writes
    setpoint: RegSpec | None = None
    rate_up: RegSpec | None = None            # °C/min, the vendor's spRateLimit
    rate_down: RegSpec | None = None          # °C/min, the vendor's downRate
    run: RegSpec | None = None
    run_value: int | None = None
    stop_value: int | None = None
    status_map: Mapping[int, str] = field(default_factory=lambda: dict(DOCUMENTED_STATUS))
    word_order_big: bool = True

    _SPECS = ("pv", "sv", "mv", "status", "setpoint", "rate_up", "rate_down", "run")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "Tc100RegisterMap":
        raw = dict(raw or {})
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"tc100 register_map: unknown keys {sorted(unknown)} (known: {sorted(known)})")
        kw: dict[str, Any] = {}
        for name in cls._SPECS:
            if name in raw:
                kw[name] = RegSpec.from_value(raw[name])
        for name in ("run_value", "stop_value"):
            if raw.get(name) is not None:
                kw[name] = int(raw[name])
        if raw.get("status_map") is not None:
            kw["status_map"] = {int(k): str(v) for k, v in dict(raw["status_map"]).items()}
        if "word_order_big" in raw:
            kw["word_order_big"] = bool(raw["word_order_big"])
        return cls(**kw)

    def configured(self) -> list[str]:
        out = [n for n in self._SPECS if getattr(self, n) is not None]
        out += [n for n in ("run_value", "stop_value") if getattr(self, n) is not None]
        return out

    def describe(self) -> dict:
        d: dict[str, Any] = {n: getattr(self, n).describe() for n in self._SPECS if getattr(self, n) is not None}
        d["run_value"], d["stop_value"] = self.run_value, self.stop_value
        d["status_map"] = dict(self.status_map)
        return d


def _need(what: str, missing: list[str]) -> OperationNotSupported:
    return OperationNotSupported(tr(
        f"TC100 寄存器表里没有配置 {', '.join(missing)}（{what}）。厂家文档（tc100DLL使用说明.docx）没给寄存器地址，"
        "驱动不猜：在设备配置的 register_map 里填，查地址的方法见 mast/instruments/tc100.py 的文档（scan_registers，只读）。",
        f"The TC100 register map has no {', '.join(missing)} ({what}). The vendor document (tc100DLL使用说明.docx) "
        "gives no register addresses and the driver does not guess: fill them into register_map in the device "
        "config (how to find them: see the docstring of mast/instruments/tc100.py, scan_registers, read-only)."))


class Tc100(TemperatureController):
    """Modbus RTU temperature controller. Read-only until a register map is configured, and
    write-protected until ``write_enabled`` is set."""

    #: seconds to wait for a Modbus reply
    IO_TIMEOUT_S = 0.5

    def __init__(self, *, port: str | None = None, baudrate: int = 9600, slave: int = 1,
                 parity: str = "N", stopbits: float = 1.0,
                 register_map: Tc100RegisterMap | Mapping[str, Any] | None = None,
                 transport: SerialLike | None = None, device_id: str = "", name: str = "",
                 limits: TemperatureLimits | None = None, write_enabled: bool = False,
                 inter_frame_gap_s: float = 0.005):
        super().__init__(device_id=device_id, name=name, limits=limits, write_enabled=write_enabled)
        if transport is None and not port:
            raise InstrumentError(f"tc100 {device_id or ''}: a COM port is required")
        if not 1 <= int(slave) <= 247:
            raise InstrumentError(f"tc100: Modbus slave address {slave} outside 1..247")
        self._port = port
        self._baudrate = int(baudrate)
        self._slave = int(slave)
        self._parity = str(parity).upper()
        self._stopbits = float(stopbits)
        self.map = (register_map if isinstance(register_map, Tc100RegisterMap)
                    else Tc100RegisterMap.from_dict(register_map))
        self._transport: SerialLike | None = transport
        self._owns_transport = transport is None
        self._gap_s = float(inter_frame_gap_s)
        self._last_io = 0.0
        self._io_lock = threading.RLock()
        #: the final target this process last wrote (the SV register of a ramping controller moves along the
        #: ramp, so it cannot answer "where is it going"); None after a restart
        self._last_target_c: float | None = None

    # -- transport --------------------------------------------------------------------------------

    def _ensure_open(self) -> SerialLike:
        if self._transport is not None:
            return self._transport
        settings = SerialSettings(port=self._port or "", baudrate=self._baudrate, parity=self._parity,
                                  stopbits=self._stopbits, timeout=self.IO_TIMEOUT_S,
                                  write_timeout=self.IO_TIMEOUT_S)
        t = open_transport(settings)
        try:
            t.open()
        except SerialUnavailable as exc:
            raise InstrumentUnavailable(
                f"TC100 on {self._port}: {exc}"
                + tr(" —— 串口是独占的：厂家的 TC100.exe 开着就打不开，请先关掉它。",
                     " -- the serial port is exclusive: it cannot be opened while the vendor's TC100.exe is "
                     "running; close it first.")
                + serial_open_hint(self._port)) from exc
        self._transport = t
        return t

    def _drop_transport(self) -> None:
        t, self._transport = self._transport, None
        if t is not None and self._owns_transport:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass

    def _transact(self, func: int, payload: bytes, *, resp_len: int) -> bytes:
        with held(self._io_lock, f"{self.device_id or 'TC100'}: serial port"):
            t = self._ensure_open()
            gap = self._gap_s - (time.monotonic() - self._last_io)
            if gap > 0:
                time.sleep(gap)
            frame = build_rtu_frame(self._slave, func, payload)
            try:
                raw = t.transact(frame, read_size=resp_len, timeout=self.IO_TIMEOUT_S)
            except SerialUnavailable as exc:
                self._drop_transport()
                raise InstrumentUnavailable(f"TC100 on {self._port}: I/O failed: {exc}") from exc
            finally:
                self._last_io = time.monotonic()
        if not raw:
            raise InstrumentUnavailable(
                f"TC100 slave {self._slave} on {self._port}: no reply to Modbus function 0x{func:02X} within "
                f"{self.IO_TIMEOUT_S:g}s "
                + tr("（控制器没上电？接线/从站号/校验位不对？厂家 TC100.exe 开着会占住串口。）",
                     "(controller off? wrong wiring / slave / parity? The vendor's TC100.exe holds the port.)"))
        return parse_rtu_response(bytes(raw), slave=self._slave, func=func)

    # -- register access ------------------------------------------------------------------------------

    def _read_words(self, function: int, address: int, count: int) -> list[int]:
        data = self._transact(function, address.to_bytes(2, "big") + count.to_bytes(2, "big"),
                              resp_len=5 + 2 * count)
        if len(data) < 1 + 2 * count or data[0] != 2 * count:
            raise InstrumentError(f"TC100: Modbus read of {count} register(s) returned {data.hex(' ')}")
        return [int.from_bytes(data[1 + 2 * i:3 + 2 * i], "big") for i in range(count)]

    def _decode(self, spec: RegSpec, words: list[int]) -> int:
        if spec.words == 2:
            hi, lo = (words[0], words[1]) if self.map.word_order_big else (words[1], words[0])
            u, bits = (hi << 16) | lo, 32
        else:
            u, bits = words[0], 16
        if spec.signed and u >= 1 << (bits - 1):
            u -= 1 << bits
        return u

    def _read_spec(self, spec: RegSpec) -> tuple[float, int]:
        words = self._read_words(spec.function, spec.address, spec.words)
        raw = self._decode(spec, words)
        return raw * spec.scale + spec.offset, raw

    def _encode(self, spec: RegSpec, value: float, what: str) -> list[int]:
        raw = int(round((float(value) - spec.offset) / spec.scale))
        bits = 16 * spec.words
        lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if spec.signed else (0, (1 << bits) - 1)
        if not lo <= raw <= hi:
            raise TemperatureLimitError(
                f"{self.device_id}: {what} {value} does not fit the register ({raw} outside [{lo}, {hi}]) - refused")
        u = raw & ((1 << bits) - 1)
        if spec.words == 2:
            hi_w, lo_w = (u >> 16) & 0xFFFF, u & 0xFFFF
            return [hi_w, lo_w] if self.map.word_order_big else [lo_w, hi_w]
        return [u]

    def _write_spec(self, spec: RegSpec, value: float, what: str) -> None:
        words = self._encode(spec, value, what)
        if spec.write_function == 6:
            self._transact(6, spec.address.to_bytes(2, "big") + words[0].to_bytes(2, "big"), resp_len=8)
        else:
            payload = (spec.address.to_bytes(2, "big") + len(words).to_bytes(2, "big")
                       + bytes([2 * len(words)]) + b"".join(w.to_bytes(2, "big") for w in words))
            self._transact(16, payload, resp_len=8)
        if spec.verify:
            back = self._read_words(3, spec.address, spec.words)
            if back != words:
                raise InstrumentError(
                    f"TC100: {what} - wrote {words} to register {spec.address}, read back {back}")

    def _write_raw_word(self, spec: RegSpec, value: int) -> None:
        """Write an integer code (run/stop value) as is, no scaling."""
        words = [int(value) & 0xFFFF]
        self._transact(6, spec.address.to_bytes(2, "big") + words[0].to_bytes(2, "big"), resp_len=8)

    # -- read-only discovery ---------------------------------------------------------------------------

    def scan_registers(self, *, function: int = 3, start: int = 0, count: int = 16) -> list[dict]:
        """Read ``count`` consecutive registers (Modbus function 3 or 4) and return
        ``[{address, raw, signed}]``. Read-only; at most 32 registers per call. Used to find the PV / SV
        / MV / status registers against the vendor program's display (module docstring)."""
        if function not in _READ_FUNCTIONS:
            raise InstrumentError(f"scan_registers: function must be 3 or 4, got {function}")
        if not 1 <= int(count) <= SCAN_MAX_COUNT:
            raise InstrumentError(f"scan_registers: count must be 1..{SCAN_MAX_COUNT}")
        if not (0 <= int(start) and int(start) + int(count) <= 0x10000):
            raise InstrumentError("scan_registers: address range outside 0..65535")
        words = self._read_words(function, int(start), int(count))
        return [{"address": int(start) + i, "raw": w, "signed": w - 0x10000 if w & 0x8000 else w}
                for i, w in enumerate(words)]

    def identify(self) -> dict:
        """Read-only self-check. Without a configured map it says so (and what to do); with one it
        reads every configured quantity once."""
        out: dict[str, Any] = {"device_id": self.device_id, "port": self._port, "slave": self._slave,
                               "line": f"{self._baudrate} {self._parity} {self._stopbits:g}",
                               "register_map_configured": self.map.configured()}
        if self.map.pv is None:
            out["ok"] = False
            out["hint"] = ("no register map: the vendor document has no register table. Use scan_registers() "
                           "against the TC100.exe display, then fill register_map (see mast/instruments/tc100.py).")
            return out
        try:
            out["status"] = self._status_raw()
            out["ok"] = True
        except InstrumentError as exc:
            out["ok"] = False
            out["error"] = str(exc)
        return out

    # -- TemperatureController -----------------------------------------------------------------------------

    def _status_raw(self) -> dict:
        m = self.map
        if m.pv is None:
            raise _need("measured temperature PV", ["pv"])
        pv, pv_raw = self._read_spec(m.pv)
        out: dict[str, Any] = {"pv_c": round(pv, 4), "pv_raw": pv_raw, "port": self._port, "slave": self._slave,
                               "register_map_configured": m.configured()}
        errors: dict[str, str] = {}
        for key, spec, field_name in (("sv", m.sv, "sv_c"), ("mv", m.mv, "output_pct")):
            if spec is None:
                continue
            try:
                v, raw = self._read_spec(spec)
                out[field_name], out[f"{key}_raw"] = round(v, 4), raw
            except InstrumentUnavailable:
                raise
            except InstrumentError as exc:
                errors[key] = str(exc)
        if m.status is not None:
            try:
                _v, raw = self._read_spec(m.status)
                out["status_raw"] = raw
                out["state"] = m.status_map.get(raw, "unknown")
            except InstrumentUnavailable:
                raise
            except InstrumentError as exc:
                errors["status"] = str(exc)
                out["state"] = "unknown"
        else:
            out["state"] = "unknown"
        if self._last_target_c is not None:
            out["target_c"], out["target_source"] = self._last_target_c, "last setpoint written by this process"
        if errors:
            out["errors"] = errors
        return out

    def _set_setpoint_raw(self, c: float, ramp_c_per_min: float | None) -> dict:
        m = self.map
        missing = [n for n in ("setpoint", "run") if getattr(m, n) is None]
        if m.run_value is None:
            missing.append("run_value")
        if ramp_c_per_min is not None:
            missing += [n for n in ("rate_up", "rate_down") if getattr(m, n) is None]
        if missing:
            raise _need(tr("设置目标温度并启动", "set the target temperature and start")
                        + ("" if ramp_c_per_min is None else tr("，带升降温速率限制", ", with the ramp limit")), missing)
        # Order (a design choice, not vendor-documented): rate limits first so the controller can
        # never act on the new target without them, then the target, then the run command.
        plan: list[tuple[str, RegSpec, float, str]] = []
        if ramp_c_per_min is not None:
            plan.append(("rate_up", m.rate_up, ramp_c_per_min, "rise rate limit"))
            plan.append(("rate_down", m.rate_down, ramp_c_per_min, "fall rate"))
        plan.append(("setpoint", m.setpoint, c, "target temperature"))
        for _name, spec, value, what in plan:
            self._encode(spec, value, what)       # a value that does not fit refuses BEFORE anything is written
        written: list[str] = []
        for name, spec, value, what in plan:
            self._write_spec(spec, value, what)
            written.append(name)
        # a stop asked for while the registers were being written: do not start the controller after it
        self._check_not_stopped("start")
        self._write_raw_word(m.run, m.run_value)
        written.append("run")
        self._last_target_c = float(c)
        return {"target_c": c, "ramp_c_per_min": ramp_c_per_min, "written": written}

    def _stop_raw(self) -> None:
        if not self.write_enabled:
            raise TemperatureWriteDisabled(tr(
                f"{self.device_id}: 设备配置里 write_enabled=false（寄存器表还没经台上自检确认），所以 MAST 不往控制器写停止值——"
                "控制器没有被停。请在 TC100 程序或控制器面板上停。",
                f"{self.device_id}: write_enabled is false in the device config (the register map has not been verified on "
                "the bench), so MAST does not write the stop value - the controller was NOT stopped. Stop it in the TC100 "
                "program or at the controller panel."))
        m = self.map
        if m.run is None or m.stop_value is None:
            raise _need(tr("停止控制器", "stop the controller"),
                        [n for n, v in (("run", m.run), ("stop_value", m.stop_value)) if v is None])
        self._write_raw_word(m.run, m.stop_value)

    def close(self) -> None:
        self._drop_transport()


def build(entry: dict) -> Tc100:
    """Registry factory (``registry._STATION_DRIVERS['tc100']``). Opens nothing."""
    dev_id = str(entry.get("id", ""))
    port = str(entry.get("port") or "").strip()
    if not port:
        raise InstrumentError(f"optical device {dev_id!r} (tc100): 'port' is required")
    try:
        reg_map = Tc100RegisterMap.from_dict(entry.get("register_map"))
    except ValueError as exc:
        raise InstrumentError(f"optical device {dev_id!r} (tc100): {exc}") from exc
    return Tc100(
        port=port, baudrate=int(entry.get("baudrate", 9600)), slave=int(entry.get("slave", 1)),
        parity=str(entry.get("parity", "N")), stopbits=float(entry.get("stopbits", 1.0)),
        register_map=reg_map, device_id=dev_id, name=str(entry.get("name") or dev_id),
        limits=TemperatureLimits.from_dict(entry.get("limits")),
        write_enabled=bool(entry.get("write_enabled", False)))
