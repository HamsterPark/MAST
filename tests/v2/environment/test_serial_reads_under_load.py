"""A slow reader thread still gets the whole reply; a cut-short reply is never parsed.

At start-up, auto-detection once logged that the Lake Shore Model 625 on its port
"answered but was not recognised". The same ``identify_port()``, run alone,
identified the supply every time. With three busy Python threads in the process it read ``b'LSCI,M'`` (and classified
that as a Lake Shore of model "M").

The cause is pyserial's ``read_until``. It reads one byte per call and checks a single
deadline after each byte. Every ``read(1)`` is several ctypes calls that hand the GIL
back, so with start-up threads busy, each byte costs tens of milliseconds and the
deadline passes in the middle of a reply that has already arrived.

A cut-short reply is worse than none. ``+14.6800`` A cut to ``+1`` parses as 1 A,
and ``+1.2E-01`` cut to ``+1.2E-0`` is ten times too large. The drivers must refuse
such a reply rather than parse it.
"""

from __future__ import annotations

import time

import pytest

from mast.environment import autodetect
from mast.environment import cryomag_lm510 as lm
from mast.environment import lakeshore_625 as m625
from mast.environment import lakeshore_temp as lsci
from mast.environment import serial_transport as st
from mast.environment.serial_transport import (
    SerialSettings,
    SerialTransport,
    SerialUnavailable,
    is_complete_line,
)

IDN_625 = b"LSCI,MODEL625,LSA0625,1.6/1.1\r\n"   # placeholder serial number


class StarvedSerial:
    """A pyserial stand-in in which every ``read()`` costs ``per_call`` seconds.

    That delay is what a thread pays to get the GIL back after each ctypes call
    while other threads are busy. The device answers the moment the request is
    written.
    """

    def __init__(self, reply: bytes = b"", *, per_call: float = 0.0, timeout: float = 0.1):
        self.reply = bytes(reply)
        self.per_call = per_call
        self.timeout = timeout
        self.buf = bytearray()
        self.read_calls = 0
        self.is_open = True

    def reset_input_buffer(self):
        self.buf.clear()

    def write(self, data):
        self.buf += self.reply
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self.buf)

    def read(self, size=1):
        self.read_calls += 1
        if self.per_call:
            time.sleep(self.per_call)
        out = bytes(self.buf[:size])
        del self.buf[:size]
        return out

    def read_until(self, expected=b"\n", size=None):
        """Same shape as pyserial 3.5 ``SerialBase.read_until``: one byte per read, one deadline."""
        line = bytearray()
        deadline = time.monotonic() + self.timeout
        while True:
            c = self.read(1)
            if not c:
                break
            line += c
            if line[-len(expected):] == expected:
                break
            if time.monotonic() > deadline:
                break
        return bytes(line)

    def close(self):
        self.is_open = False


def _transport(ser) -> SerialTransport:
    t = SerialTransport(SerialSettings(port="COM19", timeout=ser.timeout))
    t._ser = ser            # an open handle, without going through pyserial
    t._generation = 1
    return t


# ── the transport ────────────────────────────────────────────────────────────
def test_the_double_reproduces_the_start_up_truncation():
    """Self-check: with pyserial's own loop this double cuts the reply short."""
    ser = StarvedSerial(IDN_625, per_call=0.02, timeout=0.1)
    ser.write(b"*IDN?\r\n")
    cut = ser.read_until(b"\n")
    assert cut and not cut.endswith(b"\n"), cut


def test_transact_returns_the_whole_line_to_a_starved_reader():
    ser = StarvedSerial(IDN_625, per_call=0.02, timeout=0.1)
    assert _transport(ser).transact(b"*IDN?\r\n", read_until=b"\n", timeout=0.1) == IDN_625
    assert ser.read_calls <= 2, "what has arrived is taken in one read, not byte by byte"


def test_two_lines_that_arrive_together_are_both_returned():
    """LM-510 over USB: echo + answer may sit in the buffer together."""
    both = b"*IDN?\r\nCryomagnetics,LM-510,1,2\r\n"
    ser = StarvedSerial(both)
    assert _transport(ser).transact(b"*IDN?\n", read_until=b"\n", timeout=0.1, lines=2) == both


def test_bytes_after_the_last_wanted_line_are_dropped():
    ser = StarvedSerial(b"+4.40\r\n+9.99\r\n")
    assert _transport(ser).transact(b"KRDG? A\r\n", read_until=b"\n", timeout=0.1) == b"+4.40\r\n"


def test_a_silent_device_gives_an_empty_reply():
    assert _transport(StarvedSerial(b"")).transact(b"*IDN?\r\n", read_until=b"\n",
                                                   timeout=0.1) == b""


def test_a_reply_that_stops_mid_line_comes_back_unterminated():
    got = _transport(StarvedSerial(b"+14.6")).transact(b"RDGI?\r\n", read_until=b"\n",
                                                       timeout=0.1)
    assert got == b"+14.6"
    assert not is_complete_line(got)


def test_a_device_that_never_sends_the_terminator_cannot_hold_the_port():
    class Endless(StarvedSerial):
        @property
        def in_waiting(self):
            return 1000

        def read(self, size=1):
            self.read_calls += 1
            return b"x" * size

    got = _transport(Endless()).transact(b"?\r\n", read_until=b"\n", timeout=0.1)
    assert len(got) == st._MAX_REPLY_BYTES


def test_a_handle_without_in_waiting_is_read_one_byte_at_a_time():
    class Plain:
        timeout = 0.1

        def __init__(self):
            self.buf = bytearray()

        def reset_input_buffer(self):
            self.buf.clear()

        def write(self, data):
            self.buf += b"+4.40\r\n"

        def flush(self):
            pass

        def read(self, size=1):
            out = bytes(self.buf[:size])
            del self.buf[:size]
            return out

    assert _transport(Plain()).transact(b"KRDG? A\r\n", read_until=b"\n",
                                        timeout=0.1) == b"+4.40\r\n"


def test_is_complete_line():
    assert is_complete_line(b"+4.40\r\n")
    assert is_complete_line(b"\n")
    assert not is_complete_line(b"+4.4")
    assert not is_complete_line(b"")
    assert not is_complete_line(None)


# ── end to end: auto-detection of the 625 with slow reads ────────────────────
class _SlowPyserial:
    """pyserial module stand-in: only 9600 7O1 answers, as a Model 625 does.

    Every read costs 25 ms, so a 0.5 s deadline buys 20 of the 31 bytes.
    """

    def __getattr__(self, name):            # SEVENBITS, PARITY_ODD, ... -> their names
        return name

    def Serial(self, *, port, baudrate, bytesize, parity, stopbits, timeout,  # noqa: N802
               write_timeout):
        talks = (baudrate, bytesize, parity) == (9600, "SEVENBITS", "PARITY_ODD")
        return StarvedSerial(IDN_625 if talks else b"", per_call=0.025, timeout=timeout)


def test_identify_port_finds_the_625_when_every_read_is_slow(monkeypatch):
    monkeypatch.setattr(st, "_pyserial", _SlowPyserial())
    kind, info, heard = autodetect.identify_port("COM19")
    assert kind == autodetect.KIND_LAKESHORE_625
    assert heard


def test_with_pyserials_read_until_the_same_probe_misses_the_625(monkeypatch):
    """Check that the test above exercises the fix: with pyserial's loop back in place, it fails."""
    monkeypatch.setattr(st, "_pyserial", _SlowPyserial())
    monkeypatch.setattr(st, "_read_lines",
                        lambda ser, term, lines, timeout: ser.read_until(term))
    kind, _info, heard = autodetect.identify_port("COM19")
    assert kind != autodetect.KIND_LAKESHORE_625
    assert heard, "a port that talked, even cut short, must count as heard"


# ── the drivers refuse a cut-short reply ─────────────────────────────────────
class _Canned:
    """Transport double returning one canned reply to every request."""

    supports_generations = True

    def __init__(self, reply: bytes = b"", replies: "dict | None" = None):
        self.reply = reply
        self.replies = replies or {}
        self._gen = 0
        self._open = False
        self.settings = SerialSettings(port="COM19")

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
        return self.replies.get(payload.decode("ascii").strip(), self.reply)


def test_probe_lakeshore_never_identifies_a_cut_short_idn(monkeypatch):
    heard: list[bytes] = []
    monkeypatch.setattr(lsci, "open_transport", lambda settings: _Canned(b"LSCI,M"))
    assert lsci.probe_lakeshore("COM19", on_reply=heard.append) is None
    # Every reply is reported (asked IDN_ATTEMPTS times per baud since 7.2.1), none identifies.
    assert heard == [b"LSCI,M"] * (len(lsci.LSCI_SERIAL_CANDIDATES) * lsci.IDN_ATTEMPTS)


def test_probe_lakeshore_identifies_the_finished_idn(monkeypatch):
    monkeypatch.setattr(lsci, "open_transport", lambda settings: _Canned(IDN_625))
    info = lsci.probe_lakeshore("COM19")
    assert info is not None and info.model == "MODEL625"


def test_probe_model625_never_identifies_a_cut_short_idn(monkeypatch):
    monkeypatch.setattr(m625, "open_transport",
                        lambda settings: _Canned(b"LSCI,MODEL625,LSA06"))
    assert m625.probe_model625("COM19") is None


def _link_with(rdgi: bytes) -> "m625.Model625Link":
    fake = _Canned(replies={"*IDN?": IDN_625, "FLDS?": b"1,+0.3406\r\n", "RDGI?": rdgi,
                            "RDGF?": b"+00.0007E+00\r\n", "OPST?": b"000\r\n",
                            "ERST?": b"0,0,0\r\n"})
    return m625.Model625Link(fake, port="COM19", min_gap_s=0.0)


def test_625_link_refuses_a_cut_short_number():
    with pytest.raises(m625.Model625Error, match="incomplete"):
        _link_with(b"+1").query("RDGI?")          # "+14.6800" cut after two bytes


def test_625_current_sensor_reports_error_not_the_cut_short_value():
    r = m625.Model625Sensor(_link_with(b"+1"), quantity="current").read()
    assert r.status == "error"
    assert r.value != 1.0


def test_625_current_sensor_reads_a_finished_reply():
    r = m625.Model625Sensor(_link_with(b"+14.6800\r\n"), quantity="current").read()
    assert r.status == "ok" and r.value == pytest.approx(14.68)


def test_temperature_sensor_reports_error_for_a_cut_short_reading():
    s = lsci.LakeshoreTemperatureSensor(name="T", channel="A", transport=_Canned(b"+29"))
    r = s.read()
    assert r.status == "error"
    assert r.value != 29.0                      # "+293.15E+0" cut after three bytes


def test_temperature_state_query_refuses_a_cut_short_reply():
    with pytest.raises(lsci.LakeshoreError, match="incomplete"):
        lsci._query(_Canned(b"+50"), "SETP?", "1")


def test_lm510_ignores_an_unfinished_answer_line():
    assert lm.answer_from(b"MEAS? 1\r\n27.1 c", b"MEAS? 1\n") == ""
    assert lm.answer_from(b"2", b"MEAS? 1\n") == ""
    assert lm.answer_from(b"MEAS? 1\r\n27.1 cm\r\n", b"MEAS? 1\n") == "27.1 cm"
