"""NKT SuperK FIANIUM + SELECT driver — Interbus framing against the SDK manual's own telegrams,
register behaviour against a fake bus.

Sources: NKT Photonics SDK Instruction Manual v2.1.3 §2.4 (worked example telegrams, used
byte for byte below), §6.8-6.10 (registers). The fake bus has its own CRC/escaping code
(bitwise XMODEM), independent of the driver's, so a framing mistake cannot cancel itself out.
No port is opened anywhere in this file.
"""
from __future__ import annotations

import contextvars
import json
import random
import threading
import time

import pytest

from mast.environment.serial_transport import SerialUnavailable
from mast.i18n import use_lang
from mast.instruments import nkt_superk as nkt
from mast.instruments.base import InstrumentError, InstrumentUnavailable, OperationNotSupported
from mast.instruments.laser import EmissionControlDisabled, LaserLimitError, LaserLimits
from mast.instruments.nkt_superk import (InterbusError, InterbusLink, InterbusNack, InterbusTimeout, NktSuperK,
                                         build_telegram, crc16_ccitt, escape, parse_telegram, unescape)
from mast.instruments.registry import InstrumentRegistry


@pytest.fixture(autouse=True)
def _english_and_fast(monkeypatch):
    monkeypatch.setattr(nkt, "_sleep", lambda s: None)
    with use_lang("en"):
        yield


# ── an independent reference for the wire format ─────────────────────────────────


def ref_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def ref_frame(dest, src, typ, payload=b"") -> bytes:
    msg = bytes([dest, src, typ]) + bytes(payload)
    msg += ref_xmodem(msg).to_bytes(2, "big")
    out = bytearray([0x0D])
    for b in msg:
        if b in (0x0A, 0x0D, 0x5E):
            out += bytes([0x5E, b + 0x40])
        else:
            out.append(b)
    out.append(0x0A)
    return bytes(out)


def ref_deframe(raw: bytes):
    assert raw[0] == 0x0D and raw[-1] == 0x0A, raw.hex(" ")
    it, msg = iter(raw[1:-1]), bytearray()
    for b in it:
        msg.append(next(it) - 0x40 if b == 0x5E else b)
    assert ref_xmodem(bytes(msg)) == 0, "the driver sent a telegram with a bad CRC"
    return msg[0], msg[1], msg[2], bytes(msg[3:-2])


# ── a fake NKT system on one port ───────────────────────────────────────────────────

MAIN, RF, SEL = 15, 19, 21
HOST = 0xA2


class FakeNktBus:
    """Three modules at 15 (main), 19 (RF driver), 21 (SELECT). Writes are acknowledged unless
    ``ack_writes`` is false. Crystal ranges are example sweep ranges
    (crystal 1: 430-700 nm, crystal 2: 800-900 nm)."""

    def __init__(self, *, main_type=0x60, interlock=2, ack_writes=True, ramp_reads=1,
                 crystal_follows_switch=True, drop_writes=False, main_knows_regs=True,
                 emission=0, rf_power=0, silent=()):
        self.types = {MAIN: main_type, RF: 0x66, SEL: 0x67}
        self.setup_mode = 1 if main_type == 0x60 else 0          # S4x3: 0 = internal power control
        self.interlock = interlock
        self.ack_writes = ack_writes
        self.ramp_reads = ramp_reads
        self.crystal_follows_switch = crystal_follows_switch
        self.drop_writes = drop_writes
        self.main_knows_regs = main_knows_regs
        self.emission = emission                # register value 0 / 3
        self._ramp = 0
        self.rf_power = rf_power
        self.rf_switch = 0
        self.wl = [(480 + 20 * i) * 1000 for i in range(8)]       # pm
        self.amp = [0] * 8                                          # permille
        self.power_level, self.current_level = 500, 300
        self.silent = set(silent)
        self.corrupt_next = 0
        self.busy_next = 0
        self.requests: list[tuple] = []         # (dest, type, register, data)
        self.violations: list[str] = []
        self.closed = False

    # -- helpers
    def crystal(self) -> int:
        return 1 + (self.rf_switch if self.crystal_follows_switch else 0)

    def crystal_range_pm(self, c):
        return {1: (430_000, 700_000), 2: (800_000, 900_000)}[c]

    def writes(self, dest=None, register=None):
        return [r for r in self.requests if r[1] == 5 and (dest is None or r[0] == dest)
                and (register is None or r[2] == register)]

    # -- registers
    def _read(self, addr, reg):
        t = self.types[addr]
        if reg == 0x61:
            return t.to_bytes(2, "little") if t > 0xFF or addr == MAIN and t == 0x88 else bytes([t])   # 0x0088 is 16-bit
        if reg == 0x65:
            return {MAIN: b"0000A001", RF: b"0000B002", SEL: b"K0000003"}[addr]
        if reg == 0x64:
            return b"\x0a\x01"
        if addr == MAIN:
            if reg == 0x66:
                bits = 1 if self.emission == 3 else 0
                bits |= 0 if self.interlock == 2 else 2
                return bits.to_bytes(2, "little")
            if not self.main_knows_regs:
                return None
            if reg == 0x30:
                if self.emission == 3 and self._ramp < self.ramp_reads:
                    self._ramp += 1
                    return bytes([1])
                return bytes([self.emission])
            if reg == 0x31:
                return self.setup_mode.to_bytes(2, "little")
            if reg == 0x32:
                return bytes([self.interlock, 0 if self.interlock == 2 else 2])
            if reg == 0x37:
                return self.power_level.to_bytes(2, "little")
            if self.types[MAIN] == 0x60:                  # only the EXTREME S4x2 has these [SDK §6.8]; the FIANIUM S4x3 does not
                if reg == 0x11:
                    return (245).to_bytes(2, "little", signed=True)
                if reg == 0x38:
                    return self.current_level.to_bytes(2, "little")
                if reg == 0x6B:
                    return bytes([1])
        if addr == RF:
            if reg == 0x66:
                return (0).to_bytes(2, "little")
            if reg == 0x30:
                return bytes([self.rf_power])
            if reg == 0x31:
                return (3).to_bytes(2, "little")
            if reg in (0x34, 0x35):
                lo, hi = self.crystal_range_pm(self.crystal())
                return (lo if reg == 0x34 else hi).to_bytes(4, "little")
            if reg == 0x38:
                return (253).to_bytes(2, "little", signed=True)
            if reg == 0x75:
                return bytes([self.crystal()])
            if 0x90 <= reg <= 0x97:
                return self.wl[reg - 0x90].to_bytes(4, "little") + bytes(12)    # 4 x u32 array
            if 0xB0 <= reg <= 0xB7:
                return self.amp[reg - 0xB0].to_bytes(2, "little")
        if addr == SEL:
            if reg == 0x66:
                return (0).to_bytes(2, "little")
            if reg == 0x34:
                return bytes([self.rf_switch])
            if reg in (0x90, 0x91, 0xA0, 0xA1):
                lo, hi = self.crystal_range_pm(1 if reg < 0xA0 else 2)
                return (lo if reg in (0x90, 0xA0) else hi).to_bytes(4, "little")
        return None

    def _write(self, addr, reg, data) -> bool:
        if self.drop_writes:
            return True                          # acknowledged but ignored
        if addr == MAIN and self.main_knows_regs:
            if reg == 0x30 and data:
                if data[0] == 3 and self.interlock == 2:
                    self.emission, self._ramp = 3, 0
                elif data[0] == 0:
                    self.emission = 0
                return True
            if reg == 0x37:
                self.power_level = int.from_bytes(data, "little")
                return True
            if reg == 0x38 and self.types[MAIN] == 0x60:
                self.current_level = int.from_bytes(data, "little")
                return True
        if addr == RF:
            if reg == 0x30:
                self.rf_power = data[0]
                return True
            if 0x90 <= reg <= 0x97:
                self.wl[reg - 0x90] = int.from_bytes(data[:4], "little")
                return True
            if 0xB0 <= reg <= 0xB7:
                self.amp[reg - 0xB0] = int.from_bytes(data[:2], "little")
                return True
        if addr == SEL and reg == 0x34:
            if self.rf_power:
                self.violations.append("RF switch moved while RF power was on")
                return False
            self.rf_switch = data[0]
            return True
        return False

    # -- SerialLike
    def transact(self, payload, *, read_size=None, read_until=None, timeout=None):
        dest, src, typ, body = ref_deframe(bytes(payload))
        assert src == HOST and read_until == b"\x0a"
        reg, data = body[0], body[1:]
        self.requests.append((dest, typ, reg, data))
        if dest not in self.types or dest in self.silent:
            return b""
        if self.busy_next:
            self.busy_next -= 1
            return ref_frame(HOST, dest, 2, bytes([reg]))
        if typ == 4:
            val = self._read(dest, reg)
            reply = ref_frame(HOST, dest, 0, bytes([reg])) if val is None else ref_frame(HOST, dest, 8, bytes([reg]) + val)
        elif typ == 5:
            ok = self._write(dest, reg, data)
            if not ok:
                reply = ref_frame(HOST, dest, 0, bytes([reg]))
            elif not self.ack_writes:
                return b""
            else:
                reply = ref_frame(HOST, dest, 3, bytes([reg]))
        else:
            reply = ref_frame(HOST, dest, 0, bytes([reg]))
        if self.corrupt_next:
            self.corrupt_next -= 1
            reply = reply[:-3] + bytes([reply[-3] ^ 0x55]) + reply[-2:]
        return reply

    def close(self):
        self.closed = True


def _laser(bus, **kw) -> NktSuperK:
    kw.setdefault("limits", LaserLimits(min_nm=430, max_nm=900, max_amplitude_pct=50))
    kw.setdefault("write_enabled", True)
    kw.setdefault("allow_emission_control", True)
    return NktSuperK(transport=bus, port="COM14", device_id="superk", **kw)


# ═══ framing ═══════════════════════════════════════════════════════════════════════


MANUAL_TELEGRAMS = [
    # (dest, src, type, register, data, telegram bytes)               SDK manual §2.4
    (0x0F, 0xA2, 5, 0x30, bytes([3]), "0D 0F A2 05 30 03 BC E1 0A"),                   # Example 1: emission on
    (0xA2, 0x0F, 3, 0x30, b"", "0D A2 0F 03 30 48 2F 0A"),                              #   its acknowledge
    (0x0A, 0xA2, 5, 0x23, bytes([0x88, 0x13]), "0D 5E 4A A2 05 23 88 13 3B 55 0A"),    # Example 2: dest needs escaping
    (0xA2, 0x0A, 3, 0x23, b"", "0D A2 5E 4A 03 23 81 8D 0A"),                           #   its acknowledge
    (0x0A, 0xA2, 4, 0x11, b"", "0D 5E 4A A2 04 11 75 83 0A"),                           # Example 3: read temperature
    (0xA2, 0x0A, 8, 0x11, bytes([0x5E, 0x91]), "0D A2 5E 4A 08 11 5E 9E 91 63 7E 0A"),  #   datagram, data has 0x5E
]


@pytest.mark.parametrize("dest,src,typ,reg,data,wire", MANUAL_TELEGRAMS)
def test_sdk_manual_example_telegrams_byte_for_byte(dest, src, typ, reg, data, wire):
    assert build_telegram(dest, src, typ, reg, data) == bytes.fromhex(wire)
    tg = parse_telegram(bytes.fromhex(wire))
    assert (tg.dest, tg.src, tg.type, tg.register, tg.data) == (dest, src, typ, reg, data)


def test_the_temperature_example_decodes_to_37_214_celsius():
    tg = parse_telegram(bytes.fromhex("0D A2 5E 4A 08 11 5E 9E 91 63 7E 0A"))
    assert int.from_bytes(tg.data, "little") == 0x915E == 37214      # manual: "37.214 °C (37214 m°C)"


def test_crc_equals_a_generic_xmodem_implementation():
    rnd = random.Random(7)
    for _ in range(500):
        data = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 60)))
        assert crc16_ccitt(data) == ref_xmodem(data)
    assert crc16_ccitt(b"123456789") == 0x31C3                       # the standard XMODEM check value


def test_escape_roundtrip_and_special_bytes():
    assert escape(bytes([0x0A, 0x0D, 0x5E, 0x41])) == bytes([0x5E, 0x4A, 0x5E, 0x4D, 0x5E, 0x9E, 0x41])
    allb = bytes(range(256))
    assert unescape(escape(allb)) == allb


def test_telegram_data_with_every_special_byte_survives_the_wire():
    data = bytes([0x0A, 0x0D, 0x5E, 0x00, 0xFF])
    wire = build_telegram(21, HOST, 5, 0x90, data)
    assert wire[0] == 0x0D and wire[-1] == 0x0A and 0x0A not in wire[1:-1] and 0x0D not in wire[1:-1]
    assert ref_deframe(wire) == (21, HOST, 5, bytes([0x90]) + data)
    assert parse_telegram(wire).data == data


@pytest.mark.parametrize("raw,why", [
    (b"", "no start"),
    (bytes.fromhex("0D A2 0F 03 30 48"), "not terminated"),
    (bytes.fromhex("0D A2 0F 03 30 48 2E 0A"), "CRC"),                  # one CRC bit wrong
    (bytes.fromhex("0D A2 0F 0A"), "too short"),
    (bytes.fromhex("0D A2 5E 41 03 30 48 2F 0A"), "escape"),            # 0x5E followed by a non-escape code
])
def test_malformed_telegrams_are_rejected(raw, why):
    with pytest.raises(InterbusError, match=why):
        parse_telegram(raw)


def test_data_longer_than_240_bytes_is_refused():
    with pytest.raises(InterbusError, match="240"):
        build_telegram(15, HOST, 5, 0x90, bytes(241))


# ═══ link layer ═══════════════════════════════════════════════════════════════════════


def _link(bus, **kw) -> InterbusLink:
    return InterbusLink(lambda: bus, **kw)


def test_link_read_and_nack_and_silence():
    bus = FakeNktBus()
    link = _link(bus)
    assert link.read(MAIN, 0x61) == bytes([0x60])
    with pytest.raises(InterbusNack, match="refused register 0x99"):
        link.read(MAIN, 0x99)
    with pytest.raises(InterbusTimeout, match="NKT CONTROL"):
        link.read(30, 0x61)                                    # nobody at address 30


def test_link_retries_a_garbled_reply_and_a_busy_module():
    bus = FakeNktBus()
    bus.corrupt_next = 1
    assert _link(bus).read(MAIN, 0x61) == bytes([0x60])        # second attempt is clean
    bus.busy_next = 2
    assert _link(bus).read(MAIN, 0x61) == bytes([0x60])
    bus.corrupt_next = 99
    with pytest.raises(InterbusError, match="CRC"):
        _link(bus, retries=2).read(MAIN, 0x61)


class Canned:
    """Returns the same reply to everything and records requests."""

    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def transact(self, payload, **kw):
        self.requests.append(bytes(payload))
        return self.reply


def test_link_rejects_a_reply_from_the_wrong_module_or_for_the_wrong_register():
    from_other = Canned(ref_frame(HOST, RF, 8, bytes([0x61, 0x60])))                 # asked module 15, 19 answers
    with pytest.raises(InterbusError, match="addressed"):
        _link(from_other, retries=1).read(MAIN, 0x61)
    assert len(from_other.requests) == 2                                              # asked again, then gave up
    other_reg = Canned(ref_frame(HOST, MAIN, 8, bytes([0x65]) + b"0000A001"))        # asked 0x61, got 0x65
    with pytest.raises(InterbusError, match="register"):
        _link(other_reg, retries=0).read(MAIN, 0x61)
    to_someone_else = Canned(ref_frame(0xA3, MAIN, 8, bytes([0x61, 0x60])))          # addressed to another host
    with pytest.raises(InterbusError, match="addressed"):
        _link(to_someone_else, retries=0).read(MAIN, 0x61)
    wrong_type = Canned(ref_frame(HOST, MAIN, 3, bytes([0x61])))                      # an Ack is not a datagram
    with pytest.raises(InterbusError, match="unexpected reply type"):
        _link(wrong_type, retries=0).read(MAIN, 0x61)


def test_link_write_ack_or_silence():
    bus = FakeNktBus()
    assert _link(bus).write(RF, 0xB0, (100).to_bytes(2, "little")) is True
    quiet = FakeNktBus(ack_writes=False)
    assert _link(quiet).write(RF, 0xB0, (100).to_bytes(2, "little")) is False     # no ack: the caller reads back
    assert quiet.amp[0] == 100
    with pytest.raises(InterbusNack):
        _link(bus).write(RF, 0x42, b"\x01")


def test_link_drops_the_transport_when_the_port_fails():
    class Dead:
        def transact(self, *a, **k):
            raise SerialUnavailable("unplugged")

    dropped = []
    link = InterbusLink(lambda: Dead(), on_io_error=lambda: dropped.append(1))
    with pytest.raises(InstrumentUnavailable, match="I/O failed"):
        link.read(MAIN, 0x61)
    assert dropped == [1]


# ═══ identification & status ════════════════════════════════════════════════════════════


def test_identify_is_read_only_and_reports_the_labs_modules():
    bus = FakeNktBus(main_type=0x88)
    ident = _laser(bus).identify()
    assert ident["ok"] is True
    assert {r: m["type_hex"] for r, m in ident["modules"].items()} == {"main": "0x88", "rf_driver": "0x66",
                                                                       "select": "0x67"}
    assert ident["modules"]["main"]["serial_number"] == "0000A001"
    assert ident["modules"]["select"]["serial_number"] == "K0000003"
    assert ident["modules"]["main"]["type"] == 0x88                       # decoded from the 16-bit register value
    assert ident["main_writes_allowed"] is True and "SuperK FIANIUM (S4x3): documented" in ident["main_register_file"]
    assert "6.23" in ident["main_register_file"]
    assert bus.writes() == []
    assert {r[2] for r in bus.requests} <= {0x61, 0x65, 0x64, 0x66}      # only the common registers


def test_identify_flags_a_module_that_is_not_what_the_config_says():
    bus = FakeNktBus()
    bus.types[RF] = 0x67                                                 # address 19 answers like a SELECT
    ident = _laser(bus).identify()
    assert ident["ok"] is False and ident["modules"]["rf_driver"]["type_expected"] is False
    with pytest.raises(InterbusError, match="reports type 0x67"):
        _laser(bus).rf_power()


def test_identify_reports_a_silent_module_without_failing_the_rest():
    bus = FakeNktBus(silent={SEL})
    ident = _laser(bus).identify()
    assert "select" in ident["problems"] and "main" in ident["modules"] and ident["ok"] is False


def test_status_decodes_main_rf_and_select():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    bus.amp[2] = 333
    st = _laser(bus).status()
    assert st["emission"] is True and st["rf_power"] is True and st["connected_crystal"] == 1
    assert st["interlock"]["ok"] is True
    assert st["main_registers"]["inlet_temperature_c"] == 24.5
    assert st["main_registers"]["power_level_pct"] == 50.0 and st["main_registers"]["setup_mode"] == "constant power"
    assert st["crystal_range_nm"] == [430.0, 700.0]
    assert st["crystal_temperature_c"] == 25.3
    assert st["select_crystal_ranges_nm"] == {"1": [430.0, 700.0], "2": [800.0, 900.0]}
    ch = st["channels"]
    assert len(ch) == 8 and ch[0]["wavelength_nm"] == 480.0 and ch[2]["amplitude_pct"] == 33.3 and ch[2]["enabled"]
    assert st["modules"]["main"]["status_flags"] == ["emission_on"]
    assert st["write_enabled"] is True and st["allow_emission_control"] is True
    assert bus.writes() == []


def test_status_of_the_fianium_s4x3_uses_its_own_register_file():
    bus = FakeNktBus(main_type=0x88, emission=3, ramp_reads=0)
    bus.power_level = 640
    st = _laser(bus).status()
    assert st["modules"]["main"]["type_hex"] == "0x88" and st["main_writes_allowed"] is True
    regs = st["main_registers"]
    assert regs["model"] == "SuperK FIANIUM (S4x3)" and regs["output_level_pct"] == 64.0
    assert regs["setup_mode"] == "internal power control"
    assert "inlet_temperature_c" not in regs and "current_level_pct" not in regs     # S4x3 has no 0x11 / 0x38
    assert st["modules"]["main"]["status_flags"] == ["emission_on"]
    assert "errors" not in st                                    # ...and the driver never asked for them
    assert 0x11 not in {r[2] for r in bus.requests if r[0] == MAIN} and 0x38 not in {r[2] for r in bus.requests if r[0] == MAIN}


def test_main_board_of_an_unknown_type_is_neither_read_nor_written():
    bus = FakeNktBus()
    bus.types[MAIN] = 0x89                                        # something else at address 15
    st = _laser(bus).status()
    assert st["modules"]["main"]["type_expected"] is False and "main_registers" not in st and st["emission"] is None
    with pytest.raises(InterbusError, match="reports type 0x89"):
        _laser(bus).emission_on()
    assert bus.writes() == []


def test_the_status_bits_of_the_two_main_boards_are_decoded_with_their_own_tables():
    bus = FakeNktBus(main_type=0x88, emission=3, ramp_reads=0)
    bus.interlock = 0
    flags = _laser(bus).identify()["modules"]["main"]["status_flags"]
    assert flags == ["emission_on", "interlock_relays_off"]
    from mast.instruments.nkt_superk import MAIN_MAPS

    assert MAIN_MAPS[0x88].status_bits[6] == "internal_temperature_out_of_range"      # S4x3 wording
    assert MAIN_MAPS[0x88].status_bits[8] == "date_time_not_set"
    assert MAIN_MAPS[0x60].status_bits[6] == "inlet_temperature_out_of_range"          # S4x2 wording
    assert 8 not in MAIN_MAPS[0x60].status_bits


def test_status_when_the_main_board_does_not_know_the_registers():
    st = _laser(FakeNktBus(main_type=0x88, main_knows_regs=False, emission=3)).status()
    assert st["emission"] is True                                  # fell back to the status register's bit 0
    assert "emission" not in st["errors"]                          # ...which is not an error
    assert "interlock" in st["errors"] and "refused register 0x32" in st["errors"]["interlock"]   # Nack, reported
    assert "interlock" not in st                                   # and never invented


# ═══ serial line & port conflicts ═══════════════════════════════════════════════════════


def test_serial_settings_are_115200_8n1(monkeypatch):
    seen = {}

    class Rec:
        def __init__(self, settings):
            seen["s"] = settings

        def open(self):
            pass

        def transact(self, payload, **kw):
            return ref_frame(HOST, MAIN, 8, bytes([0x61, 0x60]))

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(nkt, "open_transport", Rec)
    las = NktSuperK(port="COM14")
    las.identify()
    s = seen["s"]
    assert (s.port, s.baudrate, s.bytesize, s.parity, s.stopbits) == ("COM14", 115200, 8, "N", 1.0)
    las.close()
    assert seen["closed"] is True


def test_port_held_by_nkt_control_says_so(monkeypatch):
    class Busy:
        def __init__(self, settings):
            pass

        def open(self):
            raise SerialUnavailable("could not open port 'COM14': PermissionError(13)")

    monkeypatch.setattr(nkt, "open_transport", Busy)
    monkeypatch.setattr(nkt, "serial_open_hint", lambda port: f" [guard hint for {port}]")
    with pytest.raises(InstrumentUnavailable) as ei:
        NktSuperK(port="COM14").status()
    msg = str(ei.value)
    assert "COM14" in msg and "NKT CONTROL" in msg and "Quick" in msg and "[guard hint for COM14]" in msg


# ═══ SELECT channels ═════════════════════════════════════════════════════════════════════


def test_writes_are_off_by_default_and_nothing_reaches_the_bus():
    bus = FakeNktBus()
    las = NktSuperK(transport=bus, port="COM14", device_id="superk",
                    limits=LaserLimits(min_nm=430, max_nm=900, max_amplitude_pct=50))
    assert las.write_enabled is False
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        las.set_wavelength_nm(500)
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        las.set_amplitude_pct(10)
    with pytest.raises(EmissionControlDisabled):
        las.emission_on()
    assert bus.requests == []


def test_set_wavelength_writes_exactly_one_u32_in_picometres_and_reads_back():
    bus = FakeNktBus()
    got = _laser(bus).set_wavelength_nm(520.5, channel=2)
    assert got == 520.5
    w = bus.writes(RF, 0x92)
    assert len(w) == 1 and w[0][3] == (520_500).to_bytes(4, "little")      # "OK to send only one 32-bit integer"
    assert bus.wl[2] == 520_500
    assert _laser(bus).get_wavelength_nm(channel=2) == 520.5


@pytest.mark.parametrize("nm", [429.9, 900.1, float("nan"), float("inf")])
def test_wavelength_outside_the_config_limits_is_refused_before_any_traffic(nm):
    bus = FakeNktBus()
    with pytest.raises(LaserLimitError):
        _laser(bus).set_wavelength_nm(nm)
    assert bus.writes() == []


def test_wavelength_outside_the_connected_crystal_is_refused_and_names_the_other_crystal():
    bus = FakeNktBus()
    with pytest.raises(LaserLimitError, match=r"connected crystal 1.*Crystal 2 covers 800-900"):
        _laser(bus).set_wavelength_nm(850)
    assert bus.writes() == []
    bus.rf_switch = 1                                                # now crystal 2 is connected
    assert _laser(bus).set_wavelength_nm(850, channel=0) == 850.0


def test_no_crystal_connected_refuses():
    bus = FakeNktBus()
    bus.crystal = lambda: 0
    with pytest.raises(InstrumentError, match="no crystal"):
        _laser(bus).set_wavelength_nm(500)


@pytest.mark.parametrize("ch", [-1, 8, 9, "x", None])
def test_channel_must_be_one_of_the_eight(ch):
    bus = FakeNktBus()
    with pytest.raises(LaserLimitError, match="channel"):
        _laser(bus).set_wavelength_nm(500, channel=ch)
    assert bus.requests == []


def test_amplitude_is_tenths_of_a_percent_and_capped_by_config():
    bus = FakeNktBus()
    las = _laser(bus)
    assert las.set_amplitude_pct(33.3, channel=1) == 33.3
    assert bus.writes(RF, 0xB1)[0][3] == (333).to_bytes(2, "little")
    with pytest.raises(LaserLimitError, match="outside"):
        las.set_amplitude_pct(50.1)
    with pytest.raises(LaserLimitError, match="outside"):
        las.set_amplitude_pct(-1)
    n = len(bus.writes())
    with pytest.raises(LaserLimitError, match="max_amplitude_pct"):
        _laser(bus, limits=LaserLimits(min_nm=430, max_nm=900)).set_amplitude_pct(1)
    assert len(bus.writes()) == n


def test_a_write_that_does_not_stick_is_reported():
    bus = FakeNktBus(drop_writes=True)
    with pytest.raises(InterbusError, match="read back"):
        _laser(bus).set_amplitude_pct(10)


def test_channels_lists_all_eight():
    bus = FakeNktBus()
    bus.amp[5] = 1000
    ch = _laser(bus).channels()
    assert [c["channel"] for c in ch] == list(range(8)) and ch[5]["amplitude_pct"] == 100.0


# ═══ RF power & crystal switching ═══════════════════════════════════════════════════════


def test_select_crystal_switches_rf_off_first_and_leaves_it_off():
    bus = FakeNktBus(rf_power=1)
    res = _laser(bus).select_crystal(2)
    assert res == {"crystal": 2, "changed": True, "rf_power_was_on": True, "rf_power": False,
                   "note": "RF power left off - set the channels for this crystal, then switch it on"}
    assert bus.violations == [] and bus.rf_power == 0 and bus.rf_switch == 1 and bus.crystal() == 2
    order = [(r[0], r[2]) for r in bus.writes()]
    assert order.index((RF, 0x30)) < order.index((SEL, 0x34))


def test_select_crystal_is_a_no_op_when_already_selected():
    bus = FakeNktBus()
    res = _laser(bus).select_crystal(1)
    assert res["changed"] is False and bus.writes() == []


def test_select_crystal_puts_the_switch_back_if_the_crystal_does_not_follow(monkeypatch):
    bus = FakeNktBus(crystal_follows_switch=False)
    t = iter(range(0, 1000))
    monkeypatch.setattr(nkt, "_monotonic", lambda: next(t))
    with pytest.raises(InterbusError, match="switch restored"):
        _laser(bus).select_crystal(2)
    assert bus.rf_switch == 0


def test_select_crystal_validates_and_needs_write_enabled():
    bus = FakeNktBus()
    with pytest.raises(LaserLimitError, match="1 or 2"):
        _laser(bus).select_crystal(3)
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        _laser(bus, write_enabled=False).select_crystal(2)
    assert bus.writes() == []


def test_rf_power_on_is_gated_like_emission_but_off_is_not():
    bus = FakeNktBus(rf_power=1)
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        _laser(bus, allow_emission_control=False).set_rf_power(True)
    las = _laser(bus, allow_emission_control=False, write_enabled=False)
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        _laser(bus, write_enabled=False).set_rf_power(True)
    assert las.set_rf_power(False) is False and bus.rf_power == 0          # off: always attempted


# ═══ emission ════════════════════════════════════════════════════════════════════════════


def test_transitional_emission_register_values_are_unknown_not_on():
    bus = FakeNktBus(emission=3, ramp_reads=2)                       # reads 1, 1, then 3 while switching on
    las = _laser(bus)
    assert las.emission_state() is None and las.emission_state() is None
    assert las.emission_state() is True
    assert _laser(FakeNktBus(emission=0)).emission_state() is False


def test_a_main_board_module_of_the_wrong_type_is_never_written_to():
    bus = FakeNktBus()
    bus.types[MAIN] = 0x61                                           # the front panel's type, not a laser mainboard
    las = _laser(bus)
    with pytest.raises(InterbusError, match="reports type 0x61"):
        las.emission_on()
    assert bus.writes() == []


def test_emission_on_gates_send_nothing():
    bus = FakeNktBus()
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        _laser(bus, allow_emission_control=False).emission_on()
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        _laser(bus, write_enabled=False).emission_on()
    assert bus.requests == []


def test_emission_on_for_the_documented_main_board():
    bus = FakeNktBus(main_type=0x60, ramp_reads=2)
    las = _laser(bus)
    assert las.emission_on() is True
    assert bus.emission == 3 and bus.rf_power == 1
    assert bus.writes(MAIN, 0x30)[0][3] == bytes([3])                      # "the value 3 turns it on"
    assert [r for r in bus.requests if r[2] == 0x32]                       # the interlock was looked at first
    assert bus.requests.index(next(r for r in bus.requests if r[2] == 0x32)) < \
        bus.requests.index(bus.writes(MAIN, 0x30)[0])
    assert las.emission_state() is True


@pytest.mark.parametrize("interlock", [0, 1])
def test_emission_on_never_resets_the_interlock(interlock):
    bus = FakeNktBus(interlock=interlock)
    with pytest.raises(InstrumentError, match="never resets the interlock"):
        _laser(bus).emission_on()
    assert bus.writes(MAIN) == [] and bus.writes(RF) == []


def test_emission_on_for_the_labs_fianium_s4x3_follows_its_register_file():
    bus = FakeNktBus(main_type=0x88, ramp_reads=2)                         # SDK2 6.23: 0x30 = 3 turns it on
    las = _laser(bus)
    assert las.emission_on() is True and bus.emission == 3 and bus.rf_power == 1
    assert bus.writes(MAIN, 0x30)[0][3] == bytes([3])
    assert [r for r in bus.requests if r[2] == 0x32]                       # interlock checked first, never reset
    assert not [r for r in bus.writes() if r[2] == 0x32]


def test_a_restricted_main_write_list_keeps_the_main_board_hands_off():
    bus = FakeNktBus(main_type=0x88)
    with pytest.raises(OperationNotSupported, match="main_write_types") as ei:
        _laser(bus, main_write_types=[0x60]).emission_on()
    assert "0x88" in str(ei.value) and "6.23" in str(ei.value)
    assert bus.writes() == []
    with pytest.raises(OperationNotSupported, match="main_write_types"):
        _laser(FakeNktBus(), main_write_types=[]).emission_on()


def test_with_the_main_board_hands_off_and_the_fianium_already_on_only_the_select_output_opens():
    bus = FakeNktBus(main_type=0x88, emission=3, ramp_reads=0)             # somebody pressed the button
    assert _laser(bus, main_write_types=[]).emission_on() is True
    assert bus.writes(MAIN) == [] and bus.rf_power == 1
    assert [(r[0], r[2]) for r in bus.writes()] == [(RF, 0x30)]


def test_emission_on_that_never_reaches_on_switches_everything_off(monkeypatch):
    bus = FakeNktBus(ramp_reads=10 ** 9)
    t = iter(range(0, 10 ** 6))
    monkeypatch.setattr(nkt, "_monotonic", lambda: next(t))
    with pytest.raises(InstrumentError, match="did not reach 'on'"):
        _laser(bus, emission_wait_s=3.0).emission_on()
    assert bus.emission == 0 and bus.rf_power == 0


def test_emission_off_writes_main_and_rf_and_reports():
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    las = _laser(bus)
    assert las.emission_off() is False
    assert bus.emission == 0 and bus.rf_power == 0
    assert las.last_off_report["main_emission"] is False and las.last_off_report["rf_power"] is False
    assert not las.last_off_report["errors"]


def test_emission_off_works_with_all_gates_closed_and_on_the_0x88_board():
    bus = FakeNktBus(main_type=0x88, emission=3, ramp_reads=0, rf_power=1)
    las = NktSuperK(transport=bus, port="COM14", device_id="superk")        # no allow flag, write_enabled False
    assert las.emission_off() is False and bus.emission == 0 and bus.rf_power == 0


def test_emission_off_without_a_main_module_answer_still_closes_the_rf_gate():
    bus = FakeNktBus(emission=3, rf_power=1, silent={MAIN})
    las = _laser(bus)
    assert las.emission_off() is None                                       # state unknown
    assert bus.rf_power == 0
    assert las.last_off_report["rf_power"] is False and las.last_off_report["errors"]


def test_emission_off_never_raises_when_the_port_is_gone():
    class Dead:
        def transact(self, *a, **k):
            raise SerialUnavailable("unplugged")

        def close(self):
            pass

    assert _laser(Dead()).emission_off() is None


def test_emission_off_never_raises_when_the_port_cannot_be_opened(monkeypatch):
    class Busy:
        def __init__(self, settings):
            pass

        def open(self):
            raise SerialUnavailable("PermissionError(13)")

    monkeypatch.setattr(nkt, "open_transport", Busy)
    assert NktSuperK(port="COM14", allow_emission_control=True).emission_off() is None


def test_emission_off_preempts_a_switch_on_in_progress(monkeypatch):
    bus = FakeNktBus(ramp_reads=10 ** 9)
    real_sleep = time.sleep
    monkeypatch.setattr(nkt, "_sleep", lambda s: real_sleep(0.01))
    las = _laser(bus, emission_wait_s=30.0)
    errors: list[Exception] = []

    def worker():
        try:
            las.emission_on()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    th = threading.Thread(target=contextvars.copy_context().run, args=(worker,))
    th.start()
    real_sleep(0.1)
    t0 = time.monotonic()
    las.emission_off()
    th.join(5)
    assert not th.is_alive() and time.monotonic() - t0 < 5
    assert errors and "pre-empted" in str(errors[0])
    assert bus.emission == 0 and bus.rf_power == 0


# ═══ main-board power level ═══════════════════════════════════════════════════════════════


def test_power_level_for_the_documented_board_follows_the_setup_mode():
    bus = FakeNktBus()
    las = _laser(bus, limits=LaserLimits(min_nm=430, max_nm=900, max_power=80.0, power_unit="pct"))
    assert las.set_power(42.5) == 42.5                                      # mode 1 = constant power -> 0x37
    assert bus.writes(MAIN, 0x37)[0][3] == (425).to_bytes(2, "little")
    bus.setup_mode = 0
    assert las.set_power(10.0) == 10.0 and bus.writes(MAIN, 0x38)                # mode 0 = constant current -> 0x38
    bus.setup_mode = 4                                                       # power lock: no plain level register
    with pytest.raises(OperationNotSupported, match="setup mode 4"):
        las.set_power(10.0)
    with pytest.raises(LaserLimitError, match="outside"):
        las.set_power(80.1)


def test_output_level_of_the_fianium_s4x3_is_register_0x37_in_internal_power_control():
    bus = FakeNktBus(main_type=0x88)                                        # setup mode 0 = internal power control
    las = _laser(bus, limits=LaserLimits(min_nm=430, max_nm=900, max_power=80.0))
    assert las.set_power(42.5) == 42.5 and bus.power_level == 425
    assert bus.writes(MAIN, 0x37)[0][3] == (425).to_bytes(2, "little") and not bus.writes(MAIN, 0x38)
    assert las.get_power() == 42.5
    bus.setup_mode = 4                                                      # power lock: the level is not a plain setpoint
    with pytest.raises(OperationNotSupported, match="external feedback"):
        las.set_power(10)
    with pytest.raises(LaserLimitError, match="max_power"):
        _laser(bus).set_power(10)                                           # no cap configured
    with pytest.raises(OperationNotSupported, match="main_write_types"):
        _laser(FakeNktBus(main_type=0x88), main_write_types=[0x60],
               limits=LaserLimits(min_nm=430, max_nm=900, max_power=80.0)).set_power(10)


# ═══ registry ════════════════════════════════════════════════════════════════════════════════


def test_build_reads_addresses_and_defaults_to_read_only():
    las = nkt.build({"id": "superk", "type": "nkt_superk", "port": "COM14",
                     "addresses": {"main": 16, "select": None},
                     "limits": {"min_nm": 430, "max_nm": 900, "max_amplitude_pct": 50}})
    assert las.addresses == {"main": 16, "rf_driver": 19, "select": None}
    assert las.write_enabled is False and las.allow_emission_control is False
    assert las.main_write_types == (0x60, 0x88)                                   # both documented main boards
    assert nkt.build({"id": "s", "port": "COM14", "main_write_types": []}).main_write_types == ()
    with pytest.raises(InstrumentError, match="'port' is required"):
        nkt.build({"id": "superk"})
    with pytest.raises(InstrumentError, match="unknown module role"):
        NktSuperK(port="COM14", addresses={"filter": 3})


def test_registry_builds_lazily_and_stop_all_switches_the_laser_off(tmp_path, monkeypatch):
    bus = FakeNktBus(emission=3, ramp_reads=0, rf_power=1)
    opened = []

    class Factory:
        def __init__(self, settings):
            opened.append(settings.port)

        def open(self):
            pass

        def transact(self, payload, **kw):
            return bus.transact(payload, **kw)

        def close(self):
            bus.close()

    monkeypatch.setattr(nkt, "open_transport", Factory)
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "superk", "type": "nkt_superk", "enabled": True, "port": "COM14",
         "limits": {"min_nm": 430, "max_nm": 900, "max_amplitude_pct": 50}}]}), encoding="utf-8")
    reg = InstrumentRegistry(config_path=p)
    las = reg.laser("superk")
    assert isinstance(las, NktSuperK) and opened == []
    rep = reg.stop_all(wait_s=2)
    assert "superk" in rep["stopped"] and opened == ["COM14"]
    assert bus.emission == 0 and bus.rf_power == 0
    reg.close_all()
    assert bus.closed is True
