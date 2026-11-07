"""TC100 driver — Modbus RTU framing, an *unconfigured* register map refuses instead of guessing,
and a configured one behaves (scale, sign, word order, write order, verification).

The vendor document gives no register table (see mast/instruments/tc100.py), so the maps used
here are test fixtures chosen for the test, not claims about the real controller. A fake
Modbus slave stands in for the line; no port is opened.
"""
from __future__ import annotations

import json

import pytest

from mast.environment.serial_transport import SerialUnavailable
from mast.i18n import use_lang
from mast.instruments import tc100 as tcmod
from mast.instruments.base import InstrumentError, InstrumentUnavailable, OperationNotSupported
from mast.instruments.pztc_nm003 import ModbusExceptionError, build_rtu_frame
from mast.instruments.registry import InstrumentRegistry
from mast.instruments.tc100 import RegSpec, Tc100, Tc100RegisterMap
from mast.instruments.temperature import (TemperatureLimitError, TemperatureLimits,
                                          TemperatureWriteDisabled)


@pytest.fixture(autouse=True)
def _english():
    with use_lang("en"):
        yield


class FakeSlave:
    """A Modbus RTU slave with holding/input registers; unknown addresses raise exception 02."""

    def __init__(self, *, slave=1, holding=None, inputs=None, mute=False, drop_writes=False):
        self.slave = slave
        self.holding = dict(holding or {})
        self.inputs = dict(inputs or {})
        self.mute, self.drop_writes = mute, drop_writes
        self.sent: list[bytes] = []
        self.writes: list[tuple] = []            # (function, address, [values])
        self.on_write = None
        self.closed = False

    def transact(self, payload, *, read_size=None, read_until=None, timeout=None):
        p = bytes(payload)
        self.sent.append(p)
        if self.mute or p[0] != self.slave:
            return b""
        func, body = p[1], p[2:-2]
        addr = int.from_bytes(body[0:2], "big")
        if func in (3, 4):
            count = int.from_bytes(body[2:4], "big")
            store = self.holding if func == 3 else self.inputs
            if any(addr + i not in store for i in range(count)):
                return build_rtu_frame(self.slave, func | 0x80, b"\x02")
            data = b"".join(store[addr + i].to_bytes(2, "big") for i in range(count))
            return build_rtu_frame(self.slave, func, bytes([2 * count]) + data)
        if func == 6:
            value = int.from_bytes(body[2:4], "big")
            self.writes.append((6, addr, [value]))
            if not self.drop_writes:
                self.holding[addr] = value
                if self.on_write:
                    self.on_write(addr, value)
            return build_rtu_frame(self.slave, 6, body)
        if func == 16:
            count = int.from_bytes(body[2:4], "big")
            values = [int.from_bytes(body[5 + 2 * i:7 + 2 * i], "big") for i in range(count)]
            self.writes.append((16, addr, values))
            if not self.drop_writes:
                for i, v in enumerate(values):
                    self.holding[addr + i] = v
            return build_rtu_frame(self.slave, 16, body[:4])
        return build_rtu_frame(self.slave, func | 0x80, b"\x01")

    def close(self):
        self.closed = True


# A fixture map: PV/SV/MV/status/setpoint/rates/run at made-up addresses with tenths scale.
MAP = {
    "pv": {"address": 10, "scale": 0.1, "signed": True},
    "sv": {"address": 11, "scale": 0.1, "signed": True},
    "mv": {"address": 12, "scale": 0.1, "signed": True},
    "status": 13,
    "setpoint": {"address": 20, "scale": 0.1, "signed": True},
    "rate_up": {"address": 21, "scale": 0.1},
    "rate_down": {"address": 22, "scale": 0.1},
    "run": 23, "run_value": 0, "stop_value": 1,
}


def _slave(**kw) -> FakeSlave:
    regs = {10: 255, 11: 300, 12: 0xFFFF - 249, 13: 0, 20: 300, 21: 50, 22: 50, 23: 1}      # MV = -250 raw -> -25.0 %
    regs.update(kw.pop("holding", {}))
    return FakeSlave(holding=regs, **kw)


def _tc(fake, *, register_map=MAP, **kw) -> Tc100:
    kw.setdefault("write_enabled", True)
    kw.setdefault("limits", TemperatureLimits(min_c=-190, max_c=300, max_ramp_c_per_min=10))
    return Tc100(transport=fake, port="COM15", slave=1, device_id="tc100", register_map=register_map,
                 inter_frame_gap_s=0.0, **kw)


# ── framing ─────────────────────────────────────────────────────────────────────────────


def test_modbus_frames_are_the_standard_ones():
    # the widely published example request: slave 1, read 10 holding registers from 0 -> CRC C5 CD
    assert build_rtu_frame(1, 3, bytes.fromhex("0000000A")).hex(" ") == "01 03 00 00 00 0a c5 cd"
    fake = _slave()
    _tc(fake).status()
    assert fake.sent[0][:2] == bytes([1, 3]) and fake.sent[0] == build_rtu_frame(1, 3, (10).to_bytes(2, "big") + (1).to_bytes(2, "big"))


def test_serial_line_defaults_9600_8n1_and_is_configurable(monkeypatch):
    seen = {}

    class Rec:
        def __init__(self, settings):
            seen["s"] = settings

        def open(self):
            pass

        def transact(self, payload, **kw):
            return build_rtu_frame(1, 3, bytes([2, 0, 255]))

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(tcmod, "open_transport", Rec)
    t = Tc100(port="COM15", register_map={"pv": 10})
    t.status()
    s = seen["s"]
    assert (s.port, s.baudrate, s.bytesize, s.parity, s.stopbits) == ("COM15", 9600, 8, "N", 1.0)
    Tc100(port="COM15", parity="E", stopbits=2, register_map={"pv": 10}).status()
    assert (seen["s"].parity, seen["s"].stopbits) == ("E", 2.0)
    t.close()
    assert seen["closed"] is True


# ── an unconfigured map: nothing is guessed ───────────────────────────────────────────────


def test_unconfigured_register_map_refuses_reads_and_sends_nothing():
    fake = FakeSlave()
    t = _tc(fake, register_map=None)
    with pytest.raises(OperationNotSupported, match=r"no pv.*does not guess"):
        t.status()
    with pytest.raises(OperationNotSupported):
        t.read_c()
    assert fake.sent == []


def test_unconfigured_register_map_refuses_writes_with_the_missing_items_named():
    fake = FakeSlave()
    t = _tc(fake, register_map=None)
    with pytest.raises(OperationNotSupported, match=r"setpoint, run, run_value"):
        t.set_setpoint(30)
    partial = _tc(fake, register_map={"setpoint": 20, "run": 23, "run_value": 0})
    with pytest.raises(OperationNotSupported, match="rate_up, rate_down"):
        partial.set_setpoint(30, ramp_c_per_min=5)
    assert fake.sent == []


def test_stop_without_a_map_does_not_raise_and_does_not_pretend():
    fake = FakeSlave()
    t = _tc(fake, register_map=None)
    t.stop()                                              # the safe direction never raises...
    assert fake.sent == []                                # ...and sent nothing, so the caller must check status()
    with pytest.raises(OperationNotSupported):
        t.status()


def test_identify_without_a_map_explains_what_to_do():
    ident = _tc(FakeSlave(), register_map=None).identify()
    assert ident["ok"] is False and "scan_registers" in ident["hint"] and ident["register_map_configured"] == []


def test_register_map_validation_catches_typos():
    with pytest.raises(ValueError, match="unknown keys.*pvv"):
        Tc100RegisterMap.from_dict({"pvv": 1})
    with pytest.raises(ValueError, match="read function"):
        RegSpec.from_value({"address": 1, "function": 5})
    with pytest.raises(ValueError, match="unknown keys"):
        RegSpec.from_value({"address": 1, "scalee": 2})
    assert RegSpec.from_value(7).address == 7
    with pytest.raises(InstrumentError, match="unknown keys"):
        tcmod.build({"id": "tc100", "port": "COM15", "register_map": {"temp": 1}})


# ── reads ───────────────────────────────────────────────────────────────────────────────────────────


def test_status_applies_scale_sign_and_state_map():
    fake = _slave()
    st = _tc(fake).status()
    assert st["pv_c"] == 25.5 and st["sv_c"] == 30.0 and st["output_pct"] == -25.0
    assert st["state"] == "running" and st["status_raw"] == 0
    assert _tc(fake).read_c() == 25.5
    assert fake.writes == []


@pytest.mark.parametrize("raw,state", [(0, "running"), (1, "stopped"), (2, "holding"), (3, "unknown"), (9, "unknown")])
def test_status_codes_follow_the_vendor_document(raw, state):
    assert _tc(_slave(holding={13: raw})).status()["state"] == state


def test_negative_temperature_reads_as_signed():
    assert _tc(_slave(holding={10: 0xFFFF - 1949})).status()["pv_c"] == -195.0       # the cryo end of the range


def test_two_word_values_honour_the_word_order():
    spec = {"pv": {"address": 10, "words": 2, "scale": 0.001, "signed": True}}
    big = _tc(FakeSlave(holding={10: 0x0001, 11: 0x0000}), register_map=spec)
    assert big.status()["pv_c"] == 65.536
    small = _tc(FakeSlave(holding={10: 0x0000, 11: 0x0001}), register_map={**spec, "word_order_big": False})
    assert small.status()["pv_c"] == 65.536


def test_input_register_function_is_used_when_configured():
    fake = FakeSlave(inputs={5: 123})
    t = _tc(fake, register_map={"pv": {"address": 5, "function": 4, "scale": 0.1}})
    assert t.status()["pv_c"] == 12.3 and fake.sent[0][1] == 4


def test_a_modbus_exception_is_reported_not_swallowed():
    with pytest.raises(ModbusExceptionError, match="illegal data address"):
        _tc(FakeSlave(), register_map={"pv": 77}).status()


def test_optional_registers_failing_do_not_hide_pv():
    fake = _slave()
    del fake.holding[11]                                  # SV register absent
    st = _tc(fake).status()
    assert st["pv_c"] == 25.5 and "sv_c" not in st and "illegal data address" in st["errors"]["sv"]


def test_silent_controller_is_unavailable_with_a_hint():
    with pytest.raises(InstrumentUnavailable, match="no reply.*TC100.exe"):
        _tc(_slave(mute=True)).status()


def test_port_held_by_the_vendor_program_names_it(monkeypatch):
    class Busy:
        def __init__(self, settings):
            pass

        def open(self):
            raise SerialUnavailable("PermissionError(13)")

    monkeypatch.setattr(tcmod, "open_transport", Busy)
    monkeypatch.setattr(tcmod, "serial_open_hint", lambda p: f" [guard hint for {p}]")
    with pytest.raises(InstrumentUnavailable) as ei:
        Tc100(port="COM15", register_map={"pv": 1}).status()
    assert "TC100.exe" in str(ei.value) and "COM15" in str(ei.value) and "[guard hint for COM15]" in str(ei.value)


# ── writes ──────────────────────────────────────────────────────────────────────────────────────────


def test_writes_are_off_by_default_and_sent_nowhere():
    fake = _slave()
    t = _tc(fake, write_enabled=False)
    assert Tc100(port="COM15").write_enabled is False
    with pytest.raises(TemperatureWriteDisabled):
        t.set_setpoint(30)
    assert fake.writes == [] and fake.sent == []


def test_setpoint_with_ramp_writes_rates_then_target_then_run_and_verifies():
    fake = _slave(holding={23: 1})
    res = _tc(fake).set_setpoint(35.5, ramp_c_per_min=5)
    assert [(w[0], w[1]) for w in fake.writes] == [(6, 21), (6, 22), (6, 20), (6, 23)]     # rates, target, run
    assert fake.holding[21] == 50 and fake.holding[22] == 50 and fake.holding[20] == 355 and fake.holding[23] == 0
    assert res == {"target_c": 35.5, "ramp_c_per_min": 5.0, "written": ["rate_up", "rate_down", "setpoint", "run"]}
    # each verified write was read back with function 3 right after it
    assert fake.sent.count(build_rtu_frame(1, 3, (20).to_bytes(2, "big") + (1).to_bytes(2, "big"))) == 1


def test_the_final_target_is_reported_after_a_setpoint_because_sv_may_ramp():
    fake = _slave()
    t = _tc(fake)
    assert "target_c" not in t.status()                      # nothing written by this process yet
    t.set_setpoint(35.5, ramp_c_per_min=5)
    st = t.status()
    assert st["target_c"] == 35.5 and st["target_source"] == "last setpoint written by this process"
    assert st["sv_c"] == 30.0                                # the SV register is reported separately


def test_the_config_ramp_limit_is_applied_when_no_ramp_is_given():
    fake = _slave()
    _tc(fake).set_setpoint(35)
    assert fake.holding[21] == 100 and fake.holding[22] == 100          # max_ramp_c_per_min = 10 -> 100 tenths


def test_a_ramp_limit_that_cannot_be_enforced_refuses_instead_of_jumping():
    fake = _slave()
    no_rates = {k: v for k, v in MAP.items() if k not in ("rate_up", "rate_down")}
    with pytest.raises(OperationNotSupported, match="rate_up, rate_down"):
        _tc(fake, register_map=no_rates).set_setpoint(35)                 # config limit forces a ramp
    no_cap = _tc(fake, register_map=no_rates, limits=TemperatureLimits(min_c=-190, max_c=300))
    no_cap.set_setpoint(35)                                               # no ramp requested, none required
    assert fake.writes[-1][1] == 23 and fake.holding[20] == 350


def test_limits_refuse_never_clamp_and_nothing_is_written():
    fake = _slave()
    t = _tc(fake)
    for c in (300.1, -190.1, float("nan")):
        with pytest.raises(TemperatureLimitError):
            t.set_setpoint(c)
    with pytest.raises(TemperatureLimitError, match="ramp"):
        t.set_setpoint(30, ramp_c_per_min=10.1)
    with pytest.raises(TemperatureLimitError, match="no temperature range"):
        _tc(fake, limits=TemperatureLimits()).set_setpoint(30)
    assert fake.writes == []


def test_a_value_that_does_not_fit_the_register_is_refused():
    fake = _slave()
    fine_scale = {**MAP, "setpoint": {"address": 20, "scale": 0.001, "signed": True}}
    with pytest.raises(TemperatureLimitError, match="does not fit"):
        _tc(fake, register_map=fine_scale).set_setpoint(100, ramp_c_per_min=5)     # 100000 > 32767
    assert fake.writes == []                                  # checked up front: not even the rates were touched


def test_a_write_that_does_not_stick_is_an_error():
    fake = _slave(drop_writes=True)
    with pytest.raises(InstrumentError, match="read back"):
        _tc(fake).set_setpoint(35, ramp_c_per_min=5)


def test_two_word_setpoint_uses_function_16():
    fake = FakeSlave(holding={20: 0, 21: 0, 22: 1, 23: 1, 24: 1})
    reg = {"setpoint": {"address": 20, "words": 2, "scale": 0.001}, "rate_up": 22, "rate_down": 23,
           "run": 24, "run_value": 0, "stop_value": 1}
    _tc(fake, register_map=reg).set_setpoint(1.0, ramp_c_per_min=5)       # 1000 raw = 0x0000 0x03E8
    w20 = next(w for w in fake.writes if w[1] == 20)
    assert w20 == (16, 20, [0x0000, 0x03E8])


# ── stop ────────────────────────────────────────────────────────────────────────────────────────────


def test_stop_writes_the_stop_value_and_the_state_follows():
    fake = _slave()
    fake.on_write = lambda a, v: fake.holding.__setitem__(13, 1) if (a, v) == (23, 1) else None
    t = _tc(fake, write_enabled=True)              # the stop value goes into a register the operator named: same gate
    assert t.stop() is True
    assert fake.writes == [(6, 23, [1])]
    assert t.status()["state"] == "stopped"


def test_stop_without_write_access_writes_nothing_and_says_how_to_stop_it():
    """The register map is typed in by a person and write_enabled is the switch the operator throws after the
    bench self-test. A wrong map must not make an E-STOP or a cleanup write into some other register."""
    fake = _slave()
    t = _tc(fake, write_enabled=False)
    assert t.stop() is False                                          # NOT confirmed
    assert fake.sent == [] and fake.writes == []                      # nothing went on the line, not even a read
    err = t.last_stop_report["error"]
    assert "write_enabled" in err and "NOT stopped" in err and "TC100 program" in err and "panel" in err


def test_stop_without_write_access_does_not_even_open_the_port(monkeypatch):
    def boom(settings):
        raise AssertionError("a stop that will not be written must not open the port")

    monkeypatch.setattr(tcmod, "open_transport", boom)
    t = tcmod.build({"id": "tc100", "type": "tc100", "port": "COM15", "register_map": MAP})
    assert t.write_enabled is False and t.stop() is False


def test_stop_without_write_access_is_reported_in_chinese_for_a_chinese_ui():
    t = _tc(_slave(), write_enabled=False)
    with use_lang("zh"):
        t.stop()
    assert "不往控制器写停止值" in t.last_stop_report["error"] and "TC100 程序" in t.last_stop_report["error"]


def test_a_stop_that_went_out_leaves_no_failure_in_the_report():
    t = _tc(_slave(), write_enabled=True)
    t.last_stop_report = {"error": "left over from an earlier call"}
    assert t.stop() is True and t.last_stop_report == {}


def test_stop_never_raises_on_a_dead_line():
    class Dead:
        def transact(self, *a, **k):
            raise SerialUnavailable("unplugged")

        def close(self):
            pass

    _tc(Dead()).stop()
    _tc(_slave(mute=True)).stop()


# ── read-only discovery ───────────────────────────────────────────────────────────────────────────────


def test_scan_registers_is_read_only_and_bounded():
    fake = FakeSlave(holding={i: i * 3 for i in range(0, 40)}, inputs={i: 7 for i in range(0, 40)})
    t = _tc(fake, register_map=None, write_enabled=True)
    rows = t.scan_registers(function=3, start=4, count=3)
    assert rows == [{"address": 4, "raw": 12, "signed": 12}, {"address": 5, "raw": 15, "signed": 15},
                    {"address": 6, "raw": 18, "signed": 18}]
    assert t.scan_registers(function=4, start=0, count=1)[0]["raw"] == 7
    assert all(frame[1] in (3, 4) for frame in fake.sent) and fake.writes == []
    sent = len(fake.sent)
    for kw, why in ((dict(function=6), "function must be 3 or 4"), (dict(function=16), "function must be 3 or 4"),
                    (dict(function=3, count=33), "count must be 1..32"), (dict(function=3, count=0), "count must be 1..32"),
                    (dict(start=0xFFFF, count=2), "outside 0..65535")):
        with pytest.raises(InstrumentError, match=why):
            t.scan_registers(**kw)
    assert len(fake.sent) == sent and fake.writes == []          # refused before anything reached the line
    with pytest.raises(ValueError, match="read function"):
        RegSpec(address=1, function=6)
    fake2 = FakeSlave(holding={0: 0xFFFE})
    assert _tc(fake2, register_map=None).scan_registers(count=1)[0]["signed"] == -2


# ── registry ──────────────────────────────────────────────────────────────────────────────────────────


def test_registry_builds_lazily_stop_all_stops_and_close_all_closes(tmp_path, monkeypatch):
    fake = _slave()
    opened = []

    class Factory:
        def __init__(self, settings):
            opened.append(settings.port)

        def open(self):
            pass

        def transact(self, payload, **kw):
            return fake.transact(payload, **kw)

        def close(self):
            fake.close()

    monkeypatch.setattr(tcmod, "open_transport", Factory)
    monkeypatch.setattr(tcmod.time, "sleep", lambda s: None)
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "tc100", "type": "tc100", "enabled": True, "port": "COM15", "slave": 1,
         "limits": {"min_c": -190, "max_c": 300, "max_ramp_c_per_min": 10}, "register_map": MAP}]}),
        encoding="utf-8")
    reg = InstrumentRegistry(config_path=p)
    dev = reg.temperature_controller("tc100")
    assert isinstance(dev, Tc100) and dev.write_enabled is False and opened == []
    rep = reg.stop_all(wait_s=2)
    # a TC100 whose writes are not enabled is not written to - even by the E-STOP - and the report says so
    assert rep["stopped"] == [] and "tc100" in rep["unconfirmed"] and opened == [] and fake.writes == []
    dev.write_enabled = True                                        # after the bench self-test
    rep = reg.stop_all(wait_s=2)
    assert "tc100" in rep["stopped"] and opened == ["COM15"]
    assert fake.writes == [(6, 23, [1])]
    reg.close_all()
    assert fake.closed is True


def test_build_defaults():
    t = tcmod.build({"id": "tc100", "type": "tc100", "port": "COM15"})
    assert (t._slave, t._baudrate, t._parity, t.write_enabled) == (1, 9600, "N", False)
    assert t.map.configured() == []
    with pytest.raises(InstrumentError, match="'port' is required"):
        tcmod.build({"id": "tc100"})
