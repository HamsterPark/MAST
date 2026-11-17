"""Read-only drivers for the magnet cryostat: Lake Shore 625 magnet supply and
Cryomagnetics LM-510 level monitor.

The 625 can drive a 5 T superconducting magnet, and on it a setter does not stage
a value: ``SETI 5`` ramps the output the moment it is parsed. So the property these
tests pin above everything else is *what can reach the wire*: only whitelisted
queries, never a setter, never a register-clearing query, never a command chained
behind a query, and nothing at all on a handle whose ``*IDN?`` did not say 625.

The 625 replies below are a Model 625's own answers, captured read-only
(9600 7O1, firmware 1.6/1.1; serial number replaced). The LM-510 replies
follow the manual (Rev 1.4 §5); its USB echo format still has to be confirmed on
the instrument.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from mast.environment import autodetect
from mast.environment import cryomag_lm510 as lm
from mast.environment import lakeshore_625 as m625
from mast.environment.alarm import AlarmSpec
from mast.environment.lakeshore_temp import (
    LakeshoreInfo,
    LakeshoreTemperatureSensor,
    discover_lakeshore,
    is_temperature_model,
)
from mast.environment import lakeshore_temp
from mast.environment.placeholders import HeliumLevelSensor
from mast.environment.serial_transport import SerialSettings, SerialUnavailable

IDN_625 = b"LSCI,MODEL625,LSA0625,1.6/1.1\r\n"
IDN_335 = b"LSCI,MODEL335,LSA0335/#######,2.1\r\n"

#: Read-only capture from a Model 625 (serial number replaced).
REAL_625 = {
    "RDGI?": b"+00.0020\r\n", "RDGV?": b"-0.0008\r\n", "RDGRV?": b"+0.0001\r\n",
    "RDGF?": b"+00.0007E+00\r\n", "SETI?": b"+00.0000\r\n", "SETF?": b"+00.0000E+00\r\n",
    "SETV?": b"+2.0000\r\n", "RATE?": b"+0.0100\r\n", "RATEP?": b"0,+0.1000\r\n",
    "LIMIT?": b"+15.0000,+2.0000,+0.0280\r\n", "PSH?": b"0\r\n",
    "PSHS?": b"0,+010,+005\r\n", "PSHIS?": b"+00.0000\r\n", "QNCH?": b"1,+0.2000\r\n",
    "XPGM?": b"0\r\n", "OPST?": b"006\r\n", "ERST?": b"000,000,000\r\n",
    "FLDS?": b"0,+0.34060\r\n", "MODE?": b"0\r\n", "TRIG?": b"+00.0000\r\n",
    "RSEG?": b"1\r\n", "*STB?": b"016\r\n", "BAUD?": b"0\r\n",
    "RSEGS? 1": b"+09.0000,+0.0100\r\n", "RSEGS? 2": b"+12.0000,+0.0050\r\n",
    "RSEGS? 3": b"+14.0000,+0.0020\r\n", "RSEGS? 4": b"+17.0000,+0.0010\r\n",
    "RSEGS? 5": b"+17.0000,+0.0010\r\n",
}

#: Things that must never be buildable for the 625, in any spelling.
DANGEROUS_625 = [
    "SETI 5", "SETI", "SETF 1", "SETF", "SETV 2", "RATE 0.1", "RATEP 1,0.1",
    "LIMIT 60,5,1", "PSH 1", "PSH 99", "PSHS 1,10,5", "QNCH 0,1", "XPGM 1", "STOP",
    "*RST", "*TRG", "TRIG 5", "ERCL", "DFLT 99", "MODE 1", "LOCK 1,123", "FLDS 0,1",
    "RSEG 1", "RSEGS 1,5,0.1", "BAUD 3", "IEEE 0,0,4", "DISP 1,1,3", "*CLS",
    "*ESE 255", "*SRE 255", "*OPC", "OPSTE 7", "ERSTE 1,1,1",
    "seti 5", " SETI 5 ", "SETI? ;SETI 5", "RDGI?;SETI 5", "RDGI?\r\nSETI 5",
    "RDGI? 1", "SETI? 5", "RSEGS? 6", "RSEGS? 1,2",
    *sorted(m625.FORBIDDEN_QUERIES),
]


class Fake625:
    """A Model 625 behind a pinned-generation transport (like SerialTransport)."""

    supports_generations = True

    def __init__(self, replies=None, idn=IDN_625):
        self.replies = dict(REAL_625 if replies is None else replies)
        self.idn = idn
        self.writes: list[bytes] = []
        self.write_times: list[float] = []
        self._gen = 0
        self._open = False
        self.settings = SimpleNamespace(port="COM13")

    @property
    def open_generation(self):
        return self._gen

    @property
    def is_open(self):
        return self._open

    def close(self):
        self._open = False

    def transact(self, payload, *, read_until=None, read_size=None, timeout=None,
                 lines=1, expect_generation=None):
        if expect_generation is not None and (not self._open or self._gen != expect_generation):
            raise SerialUnavailable("pinned: handle not identity-verified")
        if not self._open:
            self._open = True
            self._gen += 1
        self.writes.append(bytes(payload))
        self.write_times.append(time.monotonic())
        cmd = payload.decode("ascii").strip()
        if cmd == "*IDN?":
            return self.idn
        return self.replies.get(cmd, b"")


def _link(fake=None, **kw):
    fake = fake or Fake625()
    kw.setdefault("min_gap_s", 0.0)
    return fake, m625.Model625Link(fake, port="COM13", **kw)


def _allowed_625_payloads() -> set[bytes]:
    out = {m625.build_query(q) for q in m625.QUERIES if q != "RSEGS?"}
    out |= {m625.build_query("RSEGS?", n) for n in range(1, 6)}
    return out


# ════════════════════════════════════════════════════════════════════════════
# Lake Shore 625: what may reach the wire
# ════════════════════════════════════════════════════════════════════════════
def test_every_whitelisted_625_query_ends_in_a_question_mark_and_crlf():
    for q in m625.QUERIES:
        raw = m625.build_query(q, 1 if q == "RSEGS?" else None)
        assert raw.endswith(b"\r\n") and raw.count(b"\r\n") == 1
        verb = raw[:-2].split(b" ")[0]
        assert verb.endswith(b"?"), raw
        assert b";" not in raw


@pytest.mark.parametrize("cmd", DANGEROUS_625)
def test_a_625_setter_or_side_effect_query_can_never_be_built(cmd):
    # Neither as one string nor split into verb + argument.
    with pytest.raises(m625.Model625Error):
        m625.build_query(cmd)
    verb, _, arg = cmd.strip().partition(" ")
    with pytest.raises(m625.Model625Error):
        m625.build_query(verb, arg or None)


def test_the_forbidden_list_and_the_whitelist_do_not_overlap():
    assert not (m625.FORBIDDEN_QUERIES & m625.QUERIES)
    for q in ("*ESR?", "OPSTR?", "ERSTR?", "KEYST?"):
        assert q in m625.FORBIDDEN_QUERIES, f"{q} clears instrument state when read"


def test_rsegs_takes_only_a_segment_number():
    assert m625.build_query("RSEGS?", 3) == b"RSEGS? 3\r\n"
    for bad in (0, 6, "1,2", "A", None):
        with pytest.raises(m625.Model625Error):
            m625.build_query("RSEGS?", bad)


def test_a_full_read_and_both_sensors_only_ever_write_whitelisted_queries():
    fake, link = _link()
    link.read_state()
    field = m625.Model625Sensor(link, quantity="field", max_age_s=0.0)
    current = m625.Model625Sensor(link, name="magnet_current", quantity="current",
                                  max_age_s=0.0)
    for _ in range(3):
        field.read()
        current.read()
    allowed = _allowed_625_payloads()
    assert fake.writes, "nothing was read"
    assert set(fake.writes) <= allowed, set(fake.writes) - allowed
    assert all(w.split(b" ")[0].rstrip(b"\r\n").endswith(b"?") for w in fake.writes)


# ════════════════════════════════════════════════════════════════════════════
# Lake Shore 625: identity before anything else
# ════════════════════════════════════════════════════════════════════════════
def test_a_port_that_is_not_a_625_gets_only_idn_and_is_dropped():
    fake, link = _link(Fake625(idn=IDN_335))
    with pytest.raises(m625.WrongInstrument):
        link.query("RDGI?")
    assert fake.writes == [b"*IDN?\r\n"]
    assert not fake.is_open, "a port we cannot vouch for is not kept open"
    sensor = m625.Model625Sensor(link)
    assert sensor.read().status == "unavailable"
    assert set(fake.writes) == {b"*IDN?\r\n"}


@pytest.mark.parametrize("make", ["625", "lm510"])
def test_a_wrong_device_on_a_pinned_port_warns_once_not_every_poll(make, caplog):
    if make == "625":
        _, link = _link(Fake625(idn=IDN_335))
        sensor, logname = m625.Model625Sensor(link, max_age_s=0.0), "mast.environment.lakeshore_625"
    else:
        _, link = _lm(FakeLM510(idn="LSCI,MODEL625,X,1.6/1.1"))
        sensor = lm.LM510LevelSensor(link, min_interval_s=1.0)
        sensor._min_interval = 0.0          # poll every call for the test
        logname = "mast.environment.cryomag_lm510"
    with caplog.at_level(logging.DEBUG, logger=logname):
        for _ in range(5):
            assert sensor.read().status == "unavailable"
    warns = [r for r in caplog.records if r.name == logname and r.levelno >= logging.WARNING]
    assert len(warns) == 1, [r.getMessage() for r in warns]


def test_a_reopened_port_is_identified_again_before_any_other_query():
    """COM names can shuffle on replug."""
    fake, link = _link()
    sensor = m625.Model625Sensor(link, max_age_s=0.0)
    assert sensor.read().status == "ok"
    fake.close()                     # unplugged ...
    fake.idn = IDN_335               # ... and a different instrument now on COM13
    before = len(fake.writes)
    assert sensor.read().status == "unavailable"
    assert fake.writes[before:] == [b"*IDN?\r\n"], fake.writes[before:]


def test_a_query_is_never_written_on_a_handle_other_than_the_verified_one():
    fake, link = _link()
    link.query("RDGI?")
    fake.close()                                  # closed by someone else ...
    link._identity_current = lambda: True         # ... after the identity check said OK
    before = len(fake.writes)
    with pytest.raises(SerialUnavailable):
        link.query("RDGF?")
    assert fake.writes[before:] == [], "the transport reopened by name and wrote anyway"


# ════════════════════════════════════════════════════════════════════════════
# Lake Shore 625: what it reads
# ════════════════════════════════════════════════════════════════════════════
def test_the_captured_state_reads_back_correctly():
    _, link = _link()
    st = link.read_state()
    assert st.model == "MODEL625" and st.serial_number == "LSA0625"
    assert st.firmware == "1.6/1.1"
    assert st.current_a == pytest.approx(0.0020)
    assert st.field == pytest.approx(0.0007) and st.field_unit == "T"
    assert st.field_constant == pytest.approx(0.34060)
    assert st.setpoint_current_a == 0.0 and st.setpoint_field == 0.0
    assert st.limit_current_a == pytest.approx(15.0)
    assert st.limit_rate_a_s == pytest.approx(0.028)
    assert st.ramp_segments_enabled is True
    assert [(s["upper_current_a"], s["rate_a_s"]) for s in st.ramp_segments] == [
        (9.0, 0.01), (12.0, 0.005), (14.0, 0.002), (17.0, 0.001), (17.0, 0.001)]
    d = st.as_dict()
    assert d["max_field"] == pytest.approx(15.0 * 0.3406)          # ~5.1 T
    assert st.psh_enabled is False and d["psh_state"] in ("off", "关")
    assert st.quench_detection_enabled is True
    assert st.ramp_done is True and st.ramping is False             # OPST 006
    assert st.any_error is False and st.quench_detected is False
    assert st.interface_mode_code == 0 and st.external_program_code == 0
    assert st.energized is False
    assert st.unanswered == []


def test_field_and_current_readings_are_ok_and_in_the_instrument_units():
    _, link = _link()
    f = m625.Model625Sensor(link, quantity="field").read()
    i = m625.Model625Sensor(link, name="magnet_current", quantity="current").read()
    assert (f.value, f.unit, f.status) == (pytest.approx(0.0007), "T", "ok")
    assert (i.value, i.unit, i.status) == (pytest.approx(0.002), "A", "ok")


def test_a_quench_bit_is_a_warning_never_an_alarm_by_itself():
    """`alarm` makes MAST stop the run and retract the tip; that is opt-in."""
    replies = dict(REAL_625, **{"ERST?": b"000,032,000\r\n"})
    _, link = _link(Fake625(replies))
    r = m625.Model625Sensor(link).read()
    assert r.status == "warning"
    assert link.read_state().quench_detected is True


def test_an_operator_band_can_still_raise_an_alarm():
    replies = dict(REAL_625, **{"RDGF?": b"+02.0000E+00\r\n"})
    _, link = _link(Fake625(replies))
    r = m625.Model625Sensor(link, alarm=AlarmSpec(max=1.0)).read()
    assert r.status == "alarm" and r.value == pytest.approx(2.0)


def test_two_sensors_share_one_poll_round():
    fake, link = _link()
    m625.Model625Sensor(link, quantity="field").read()
    m625.Model625Sensor(link, name="magnet_current", quantity="current").read()
    assert fake.writes.count(b"RDGI?\r\n") == 1 and fake.writes.count(b"RDGF?\r\n") == 1


def test_commands_are_spaced_below_the_20_per_second_limit():
    fake, link = _link(min_gap_s=0.03)
    link.read_state()
    gaps = [b - a for a, b in zip(fake.write_times, fake.write_times[1:])]
    assert gaps and min(gaps) >= 0.029


def test_an_unknown_quantity_is_refused():
    _, link = _link()
    with pytest.raises(ValueError):
        m625.Model625Sensor(link, quantity="setpoint")


# ════════════════════════════════════════════════════════════════════════════
# Cryomagnetics LM-510
# ════════════════════════════════════════════════════════════════════════════
LM_REPLIES = {
    "*IDN?": "Cryomagnetics,LM-510,4321,2.09", "*STB?": "0", "MEAS? 1": "27.1 cm",
    "STAT?": "8,0,0", "TYPE? 1": "0", "CTRL? 1": "Off", "CHAN?": "1", "MODE?": "Off",
    "INTVL?": "00:00:30", "LNGTH?": "30.0 cm", "BOOST?": "Smart", "L-ALM?": "0.0 cm",
    "H-ALM?": "30.0 cm", "LOW?": "0.0 cm", "HIGH?": "30.0 cm", "ERROR?": "0",
}


class FakeLM510:
    """USB behaviour from the manual: echo + CR LF, then the answer + CR LF."""

    supports_generations = True

    def __init__(self, replies=None, idn=None, echo=True):
        self.replies = dict(LM_REPLIES if replies is None else replies)
        if idn is not None:
            self.replies["*IDN?"] = idn
        self.echo = echo
        self.writes: list[bytes] = []
        self._gen = 0
        self._open = False
        self.settings = SimpleNamespace(port="COM15")

    @property
    def open_generation(self):
        return self._gen

    @property
    def is_open(self):
        return self._open

    def close(self):
        self._open = False

    def transact(self, payload, *, read_until=None, read_size=None, timeout=None,
                 lines=1, expect_generation=None):
        if expect_generation is not None and (not self._open or self._gen != expect_generation):
            raise SerialUnavailable("pinned")
        if not self._open:
            self._open = True
            self._gen += 1
        self.writes.append(bytes(payload))
        cmd = payload.decode("ascii").strip()
        out = []
        if self.echo:
            out.append(cmd.encode() + b"\r\n")
        ans = self.replies.get(cmd)
        if ans is not None and cmd:
            out.append(ans.encode() + b"\r\n")
        return b"".join(out[:max(1, lines)])


def _lm(fake=None, **kw):
    fake = fake or FakeLM510()
    kw.setdefault("min_gap_s", 0.0)
    return fake, lm.LM510Link(fake, port="COM15", **kw)


def test_meas_without_the_question_mark_can_never_be_built():
    """MEAS starts a reading: current through the sensor filament boils helium."""
    for cmd in ("MEAS", "MEAS 1", "meas", "MEAS?;MEAS", "MEAS? 1\rMEAS 1", "MEAS? 1\nMEAS 1"):
        with pytest.raises(lm.LM510Error):
            lm.build_query(cmd)
    for cmd in sorted(lm.FORBIDDEN_COMMANDS):
        with pytest.raises(lm.LM510Error):
            lm.build_query(cmd)


def test_only_the_channel_queries_take_a_channel_and_only_1_or_2():
    # LF terminator, as the vendor's own LabVIEW driver sends ("Append line feed").
    assert lm.build_query("MEAS?", 1) == b"MEAS? 1\n"
    assert lm.build_query("TYPE?", "2") == b"TYPE? 2\n"
    for verb, arg in (("MEAS?", 3), ("MEAS?", "1;MEAS"), ("UNITS?", 1), ("MODE?", "S")):
        with pytest.raises(lm.LM510Error):
            lm.build_query(verb, arg)


def test_the_usb_echo_is_dropped():
    assert lm.answer_from(b"MEAS? 1\r\n27.1 cm\r\n", b"MEAS? 1\n") == "27.1 cm"
    assert lm.answer_from(b"*IDN?\r\n\r\nCryomagnetics,LM-510,1,2\r\n", b"*IDN?\n") \
        == "Cryomagnetics,LM-510,1,2"
    assert lm.answer_from(b"MEAS? 1\n27.1 cm\n", b"MEAS? 1\n") == "27.1 cm"  # LF-only echo
    assert lm.answer_from(b"27.1 cm\r\n", b"MEAS? 1\n") == "27.1 cm"     # LAN: no echo
    assert lm.answer_from(b"MEAS? 1\r\n", b"MEAS? 1\n") == ""            # echo only


def test_the_level_reads_with_its_unit_and_needs_two_lines():
    fake, link = _lm()
    sensor = lm.LM510LevelSensor(link)
    r = sensor.read()
    assert (r.value, r.unit, r.status) == (pytest.approx(27.1), "cm", "ok")
    assert b"MEAS? 1\n" in fake.writes


@pytest.mark.parametrize("stat,status", [("32,0,0", "error"),    # open sensor
                                         ("64,0,0", "error"),    # burnout
                                         ("16,0,0", "warning"),  # alarm limit exceeded
                                         ("9,0,0", "ok")])       # reading + ctrl inhibited
def test_sensor_status_bits(stat, status):
    _, link = _lm(FakeLM510(dict(LM_REPLIES, **{"STAT?": stat})))
    assert lm.LM510LevelSensor(link).read().status == status


def test_a_blocked_meas_while_the_menu_is_open_is_unavailable_not_zero():
    replies = dict(LM_REPLIES)
    replies.pop("MEAS? 1")
    replies["STAT?"] = "8,0,1"
    _, link = _lm(FakeLM510(replies))
    r = lm.LM510LevelSensor(link).read()
    assert r.status == "unavailable"


def test_the_state_says_when_the_level_never_refreshes_by_itself():
    _, link = _lm()
    st = link.read_state(1)
    assert st.level == pytest.approx(27.1) and st.unit == "cm"
    assert st.sample_mode == "Off" and st.updates_by_itself is False
    assert st.sensor_length == "30.0 cm" and st.boost == "Smart"
    assert st.default_channel == 1 and st.as_dict()["channel_type"] == "LHe"
    assert st.sensor_fault is False and st.unanswered == []


def test_everything_written_to_the_lm510_is_a_whitelisted_query():
    fake, link = _lm()
    link.read_state(1)
    sensor = lm.LM510LevelSensor(link, min_interval_s=1.0)
    sensor.read()
    for raw in fake.writes:
        verb = raw.decode().strip().split(" ")[0]
        assert verb in lm.QUERIES and verb.endswith("?"), raw
    assert not any(raw.strip() in (b"MEAS", b"MEAS 1", b"MEAS 2") for raw in fake.writes)


def test_polling_is_rate_limited_by_the_sensor():
    fake, link = _lm()
    sensor = lm.LM510LevelSensor(link, min_interval_s=30.0)
    sensor.read()
    n = len(fake.writes)
    for _ in range(5):
        sensor.read()
    assert len(fake.writes) == n, "a cached reading must not touch the port"


def test_a_port_that_is_not_an_lm510_gets_only_idn():
    fake, link = _lm(FakeLM510(idn="LSCI,MODEL625,LSA0625,1.6/1.1"))
    assert lm.LM510LevelSensor(link).read().status == "unavailable"
    assert fake.writes == [b"*IDN?\n"]


# ════════════════════════════════════════════════════════════════════════════
# Auto-detection and configuration
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def one_port(monkeypatch):
    monkeypatch.setattr(autodetect, "list_serial_ports",
                        lambda: [SimpleNamespace(device="COM13", description="", hwid="")])
    calls = {"dl7": 0}

    def _dl7(*a, **k):
        calls["dl7"] += 1
        return False

    monkeypatch.setattr(autodetect, "probe_dl7", _dl7)
    monkeypatch.setattr(autodetect, "probe_lm510", lambda port, **k: None)
    monkeypatch.setattr(autodetect, "probe_model625", lambda port, **k: None)
    return calls


def _lsci_info(model):
    return LakeshoreInfo(model, f"LSCI,{model},X,1.0",
                         SerialSettings(port="COM13", baudrate=9600, bytesize=7, parity="O"))


def test_a_model625_is_a_magnet_supply_not_a_thermometer(monkeypatch, one_port):
    monkeypatch.setattr(autodetect, "probe_lakeshore",
                        lambda port, **k: _lsci_info("MODEL625"))
    found = autodetect.autodetect_sensors()
    assert [type(s) for s in found] == [m625.Model625Sensor, m625.Model625Sensor]
    assert {s.name() for s in found} == {"magnet_field", "magnet_current"}
    assert found[0].link is found[1].link, "one link (identity + spacing) per instrument"
    assert not any(isinstance(s, LakeshoreTemperatureSensor) for s in found)
    assert one_port["dl7"] == 0
    assert all(autodetect.is_real_sensor(s) for s in found)


def test_an_unknown_lake_shore_model_is_reported_but_never_polled(monkeypatch, one_port):
    monkeypatch.setattr(autodetect, "probe_lakeshore",
                        lambda port, **k: _lsci_info("MODEL455"))
    assert autodetect.autodetect_sensors() == []
    assert one_port["dl7"] == 0


def test_the_dl7_frame_never_goes_to_a_port_that_talked(monkeypatch, one_port):
    def _talks(port, on_reply=None, **k):
        on_reply(b"?*IDN?\r")     # something answered, just not a Lake Shore
        return None

    monkeypatch.setattr(autodetect, "probe_lakeshore", _talks)
    assert autodetect.autodetect_sensors() == []
    assert one_port["dl7"] == 0


def test_a_silent_port_still_gets_the_dl7_probe(monkeypatch, one_port):
    monkeypatch.setattr(autodetect, "probe_lakeshore", lambda port, **k: None)
    autodetect.autodetect_sensors()
    assert one_port["dl7"] == 1


def test_an_lm510_replaces_the_helium_placeholder(monkeypatch, one_port):
    monkeypatch.setattr(autodetect, "probe_lakeshore", lambda port, **k: None)
    monkeypatch.setattr(autodetect, "probe_lm510", lambda port, **k: (
        SerialSettings(port=port, baudrate=9600), {"model": "LM-510"}))
    sensors = autodetect.build_environment_sensors(cfg={"autodetect": True, "sensors": []})
    names = [s.name() for s in sensors]
    assert names.count("helium_level") == 1 and "helium_level#2" not in names
    helium = [s for s in sensors if s.name() == "helium_level"][0]
    assert isinstance(helium, lm.LM510LevelSensor)
    assert not any(isinstance(s, HeliumLevelSensor) for s in sensors)


def test_config_entries_build_read_only_sensors_on_one_link():
    cfg = {"autodetect": False, "sensors": [
        {"id": "b", "name": "B", "type": "lakeshore_625", "port": "COM13", "quantity": "field"},
        {"id": "i", "name": "I", "type": "lakeshore_625", "port": "COM13", "quantity": "current"},
        {"id": "he", "name": "LHe", "type": "cryomag_lm510", "port": "COM15", "channel": 1},
        {"id": "bad1", "type": "lakeshore_625", "port": "COM13", "baudrate": 4800},
        {"id": "bad2", "type": "lakeshore_625", "port": "COM13", "quantity": "setpoint"},
        {"id": "bad3", "type": "cryomag_lm510", "port": "COM15", "channel": 3},
        {"id": "bad4", "type": "lakeshore_625"},
    ]}
    sensors, used = autodetect.build_sensors_from_config(cfg)
    by = {s.name(): s for s in sensors}
    assert set(by) == {"B", "I", "LHe"}
    assert by["B"].link is by["I"].link
    assert by["B"].link.transport.settings.bytesize == 7          # 7O1 is fixed
    assert by["B"].link.transport.settings.parity == "O"
    assert by["LHe"].link.transport.settings.bytesize == 8
    assert {"COM13", "COM15"} <= used


def test_the_temperature_driver_sends_a_625_nothing_but_idn(monkeypatch):
    sent: list[bytes] = []

    class T:
        def transact(self, payload, **k):
            sent.append(bytes(payload))
            return IDN_625 if payload.startswith(b"*IDN?") else b""

        def close(self):
            pass

    monkeypatch.setattr(lakeshore_temp, "open_transport", lambda settings: T())
    snap = discover_lakeshore("COM13")
    assert snap is not None and snap.model == "MODEL625"
    assert snap.channels == [] and snap.outputs == []
    assert set(sent) == {b"*IDN?\r\n"}, sent


@pytest.mark.parametrize("model,is_temp", [("MODEL335", True), ("MODEL336", True),
                                           ("MODEL218S", True), ("MODEL625", False),
                                           ("MODEL455", False), ("", False)])
def test_temperature_models(model, is_temp):
    assert is_temperature_model(model) is is_temp
