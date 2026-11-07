"""NPI Rainbow 1064 OEM driver — manual command table, LED/temperature parsing, gates.

Every expected value here is the manual's own (Rainbow 1064 OEM User Manual V2.0.0,
§3.3.2 Table 1): the LED examples 1100 / 1111 and the three temperature words
0C80h = 25 °C, 0080h = 1 °C, 0020h = 0.25 °C. The serial line is a stateful fake laser —
no port is opened anywhere in this file.
"""
from __future__ import annotations

import contextvars
import json
import threading
import time

import pytest

from mast.environment.serial_transport import SerialUnavailable
from mast.i18n import use_lang
from mast.instruments import npi_rainbow as npi
from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.instruments.laser import EmissionControlDisabled
from mast.instruments.npi_rainbow import NpiRainbow, parse_case_temp_c, parse_led_status
from mast.instruments.registry import InstrumentRegistry


@pytest.fixture(autouse=True)
def _english_and_fast(monkeypatch):
    monkeypatch.setattr(npi, "_sleep", lambda s: None)
    with use_lang("en"):
        yield


class FakeRainbow:
    """A SerialLike that behaves like the laser the manual describes.

    Effective emission: in Remote mode the software flag decides, in Local mode the
    front-panel switch does (manual §4.2.1: Remote disables the panel switch)."""

    def __init__(self, *, power=True, ready=True, error=False, panel_switch_on=False,
                 fail_to_light=False, mute=False, terminator="\r\n", case_raw="0C80"):
        self.power, self.ready, self.error = power, ready, error
        self.panel_switch_on = panel_switch_on
        self.remote = False
        self.software_emission = False
        self.fail_to_light = fail_to_light
        self.mute = mute
        self.terminator = terminator
        self.case_raw = case_raw
        self.sent: list[bytes] = []
        self.closed = False

    @property
    def laser(self) -> bool:
        return self.software_emission if self.remote else self.panel_switch_on

    def led(self) -> str:
        return "".join("1" if b else "0" for b in (self.power, self.ready, self.laser, self.error))

    def transact(self, payload, *, read_size=None, read_until=None, timeout=None):
        cmd = bytes(payload)
        self.sent.append(cmd)
        if self.mute:
            return b""
        text = cmd.decode("ascii").rstrip("\r")
        if text == "gstatus":
            reply = f"LED Status: {self.led()}"
        elif text == "gProduct":
            reply = "1064"
        elif text == "gSN":
            reply = "XZ00000000"
        elif text == "g_case_temp":
            reply = self.case_raw
        elif text == "sRemote_flag0":
            self.remote = True
            reply = "Remote Set Finished"
        elif text == "sRemote_flag1":
            self.remote = False
            reply = "Local Set Finished"
        elif text == "sEmission_flag1":
            if self.remote and not self.fail_to_light:
                self.software_emission = True
            reply = "Emission ON Finished"
        elif text == "sEmission_flag0":
            self.software_emission = False
            reply = "Emission OFF Finished"
        else:                                   # a command outside the manual: the laser would not know it
            reply = "ERR"
        return (reply + self.terminator).encode("ascii")

    def close(self):
        self.closed = True

    def commands(self) -> list[str]:
        return [c.decode().rstrip("\r") for c in self.sent]


def _laser(fake, **kw) -> NpiRainbow:
    kw.setdefault("allow_emission_control", True)
    kw.setdefault("write_enabled", True)
    return NpiRainbow(transport=fake, port="COM13", device_id="npi1064", **kw)


# ── parsing: the manual's own examples ──────────────────────────────────────────


def test_led_status_examples_from_the_manual():
    assert parse_led_status("LED Status: 1100") == {"power": True, "ready": True, "laser": False, "error": False}
    assert all(parse_led_status("LED Status: 1111").values())
    assert parse_led_status("1010\r\n")["laser"] is True
    assert parse_led_status("LED Status:") is None
    assert parse_led_status("11001") is None          # not four bits


@pytest.mark.parametrize("word,celsius", [
    ("0C80", 25.0), ("0080", 1.0), ("0020", 0.25),       # manual Table 1
    ("0x0C80", 25.0), ("0C80h", 25.0), ("Case Temp: 0C80", 25.0), ("0c80\r\n", 25.0),
    ("FF80", -1.0), ("F380", -25.0),                       # two's complement
])
def test_case_temperature_scale_is_signed16_over_128(word, celsius):
    assert parse_case_temp_c(word) == celsius


def test_case_temperature_unparseable():
    assert parse_case_temp_c("") is None
    assert parse_case_temp_c("no number here") is None


# ── the command table is closed ─────────────────────────────────────────────────


def test_allow_list_is_exactly_the_manual_table_and_no_service_command():
    assert {c.decode().rstrip("\r") for c in npi.ALLOWED_COMMANDS} == {
        "sEmission_flag1", "sEmission_flag0", "sRemote_flag0", "sRemote_flag1",
        "gstatus", "gProduct", "gSN", "g_case_temp"}
    for forbidden in ("sservice", "sfactory"):
        assert not any(c.startswith(forbidden.encode()) for c in npi.ALLOWED_COMMANDS)


@pytest.mark.parametrize("cmd", [b"sservice100\r", b"sfactory1\r", b"gstatus", b"gstatus\n", b""])
def test_anything_outside_the_table_is_refused_before_the_wire(cmd):
    fake = FakeRainbow()
    with pytest.raises(InstrumentError, match="not in the manual"):
        _laser(fake)._command(cmd)
    assert fake.sent == []


# ── reads never change the laser ────────────────────────────────────────────────


def test_status_uses_read_commands_only_and_never_touches_remote_mode():
    fake = FakeRainbow(panel_switch_on=True)
    st = _laser(fake).status()
    assert set(fake.commands()) <= {"gstatus", "gProduct", "gSN", "g_case_temp"}
    assert st["emission"] is True and st["ready"] is True
    assert st["serial_number"] == "XZ00000000" and st["product"] == "1064"
    assert st["case_temp_c"] == 25.0 and st["case_temp_raw"] == "0C80"
    assert st["led_raw"] == "1110"
    assert st["remote_set_by_mast"] is None
    assert fake.remote is False                     # the front panel is still in charge


def test_identify_reports_replies_verbatim():
    fake = FakeRainbow()
    ident = _laser(fake).identify()
    assert ident["ok"] is True
    assert ident["replies"] == {"product": "1064", "serial_number": "XZ00000000",
                                "gstatus": "LED Status: 1100", "g_case_temp": "0C80"}
    assert ident["case_temp_c"] == 25.0
    assert set(fake.commands()) == {"gProduct", "gSN", "gstatus", "g_case_temp"}


def test_lf_less_replies_still_parse():
    fake = FakeRainbow(terminator="")               # the real terminator is undocumented
    assert _laser(fake).status()["led_raw"] == "1100"


def test_silent_laser_is_unavailable_not_a_hang():
    with pytest.raises(InstrumentUnavailable, match="no reply"):
        _laser(FakeRainbow(mute=True)).status()


# ── emission on: two gates, readiness, verification ───────────────────────────────


def test_emission_on_needs_the_config_gate_and_sends_nothing_without_it():
    fake = FakeRainbow()
    las = NpiRainbow(transport=fake, port="COM13", device_id="npi1064")      # allow_emission_control default False
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control"):
        las.emission_on()
    assert fake.sent == []


def test_emission_on_needs_write_enabled():
    fake = FakeRainbow()
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        _laser(fake, write_enabled=False).emission_on()
    with pytest.raises(EmissionControlDisabled, match="write_enabled"):
        NpiRainbow(transport=fake, port="COM13", allow_emission_control=True).emission_on()     # the default is closed
    assert fake.sent == []


def test_emission_on_sequence_remote_then_on_then_verified_by_the_laser_led():
    fake = FakeRainbow()
    las = _laser(fake)
    assert las.emission_on() is True
    assert fake.commands() == ["gstatus", "sRemote_flag0", "sEmission_flag1", "gstatus"]
    assert fake.laser is True and las.emission_state() is True
    assert las.status()["remote_set_by_mast"] is True
    assert "front-panel ON/OFF switch" in las.emission_on_warning and "Remote mode" in las.emission_on_warning


def test_emission_on_is_a_no_op_when_already_emitting():
    fake = FakeRainbow(panel_switch_on=True)
    assert _laser(fake).emission_on() is True
    assert fake.commands() == ["gstatus"]            # nothing written, mode untouched


@pytest.mark.parametrize("kw,why", [
    (dict(ready=False), "not ready"),
    (dict(power=False, ready=False), "not ready"),
    (dict(error=True), "Error LED"),
])
def test_emission_on_refused_when_not_ready_or_in_error(kw, why):
    fake = FakeRainbow(**kw)
    with pytest.raises(InstrumentError, match=why):
        _laser(fake).emission_on()
    assert not any(c.startswith("sEmission") or c.startswith("sRemote") for c in fake.commands())


def test_emission_on_that_never_lights_the_laser_sends_off_and_fails():
    fake = FakeRainbow(fail_to_light=True)
    with pytest.raises(InstrumentError, match="did not light"):
        _laser(fake, emission_confirm_s=0.0).emission_on()
    assert fake.commands()[-1] == "sEmission_flag0"      # the safe direction was the last thing sent
    assert fake.software_emission is False


def test_error_led_after_switch_on_sends_off(monkeypatch):
    fake = FakeRainbow()
    orig = fake.transact

    def flaky(payload, **kw):
        out = orig(payload, **kw)
        if bytes(payload) == b"sEmission_flag1\r":
            fake.error = True
        return out

    fake.transact = flaky
    with pytest.raises(InstrumentError, match="Error LED"):
        _laser(fake).emission_on()
    assert "sEmission_flag0" in fake.commands() and fake.software_emission is False


def test_failed_write_during_switch_on_goes_to_the_safe_side():
    fake = FakeRainbow()
    orig = fake.transact
    calls = {"n": 0}

    def drop_the_on_reply(payload, **kw):
        if bytes(payload) == b"sEmission_flag1\r":
            calls["n"] += 1
            orig(payload, **kw)                      # the laser did switch on…
            return b""                               # …but the reply was lost
        return orig(payload, **kw)

    fake.transact = drop_the_on_reply
    with pytest.raises(InstrumentUnavailable):
        _laser(fake).emission_on()
    assert fake.software_emission is False           # …and the driver switched it off again


# ── emission off never raises ───────────────────────────────────────────────────


def test_emission_off_is_attempted_even_with_gates_closed():
    fake = FakeRainbow()
    fake.remote, fake.software_emission = True, True
    las = NpiRainbow(transport=fake, port="COM13", device_id="npi1064", write_enabled=False)
    assert las.emission_off() is False
    assert fake.laser is False


def test_emission_off_falls_back_to_remote_when_the_panel_switch_holds_it_on():
    fake = FakeRainbow(panel_switch_on=True)          # Local mode, switch at ON
    las = _laser(fake)
    assert las.emission_off() is False
    cmds = fake.commands()
    assert "sRemote_flag0" in cmds and cmds.count("sEmission_flag0") == 2
    assert fake.remote is True and fake.laser is False
    assert las.last_off_report == {"remote_mode_entered_for_off": True, "laser_led": False}      # the report says so


def test_emission_off_never_raises_when_the_port_is_gone(monkeypatch):
    class Dead:
        def transact(self, *a, **k):
            raise SerialUnavailable("unplugged")

        def close(self):
            pass

    assert _laser(Dead()).emission_off() is None
    assert _laser(FakeRainbow(mute=True)).emission_off() is None


def test_emission_off_never_raises_when_the_port_cannot_be_opened(monkeypatch):
    class Busy:
        def __init__(self, settings):
            pass

        def open(self):
            raise SerialUnavailable("could not open port 'COM13': PermissionError(13)")

        def close(self):
            pass

    monkeypatch.setattr(npi, "open_transport", Busy)
    las = NpiRainbow(port="COM13", device_id="npi1064", allow_emission_control=True)
    assert las.emission_off() is None


def test_emission_off_preempts_a_switch_on_that_is_still_waiting(monkeypatch):
    fake = FakeRainbow(fail_to_light=True)
    real_sleep = time.sleep
    monkeypatch.setattr(npi, "_sleep", lambda s: real_sleep(0.01))   # real short sleeps: the wait really is in progress
    las = _laser(fake, emission_confirm_s=30.0)
    errors: list[Exception] = []

    def worker():
        try:
            las.emission_on()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    th = threading.Thread(target=contextvars.copy_context().run, args=(worker,))   # keep use_lang("en")
    th.start()
    real_sleep(0.1)
    t0 = time.monotonic()
    assert las.emission_off() is False
    th.join(5)
    assert not th.is_alive() and time.monotonic() - t0 < 5
    assert errors and "pre-empted" in str(errors[0])
    assert fake.laser is False


# ── port conflicts ──────────────────────────────────────────────────────────────


def test_port_held_by_the_vendor_program_names_it_and_adds_the_guard_hint(monkeypatch):
    class Busy:
        def __init__(self, settings):
            self.settings = settings

        def open(self):
            raise SerialUnavailable("open COM13@115200 8N1 failed: could not open port 'COM13': PermissionError(13)")

    monkeypatch.setattr(npi, "open_transport", Busy)
    monkeypatch.setattr(npi, "serial_open_hint", lambda port: f" [guard hint for {port}]")
    las = NpiRainbow(port="COM13", device_id="npi1064")
    with pytest.raises(InstrumentUnavailable) as ei:
        las.status()
    msg = str(ei.value)
    assert "COM13" in msg and "Rainbow OEM" in msg and "exclusive" in msg and "[guard hint for COM13]" in msg


def test_serial_settings_are_115200_8n1(monkeypatch):
    seen = {}

    class Rec:
        def __init__(self, settings):
            seen["s"] = settings

        def open(self):
            pass

        def transact(self, payload, **kw):
            return b"LED Status: 1100\r\n"

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(npi, "open_transport", Rec)
    las = NpiRainbow(port="COM13")
    las.led_status()
    s = seen["s"]
    assert (s.port, s.baudrate, s.bytesize, s.parity, s.stopbits) == ("COM13", 115200, 8, "N", 1.0)
    las.close()
    assert seen["closed"] is True


# ── hand-back to the front panel ──────────────────────────────────────────────────


def test_release_to_front_panel_requires_a_person_to_have_seen_the_switch_off():
    fake = FakeRainbow()
    fake.remote = True
    las = _laser(fake)
    with pytest.raises(InstrumentError, match="front_switch_confirmed_off"):
        las.release_to_front_panel()
    assert "sRemote_flag1" not in fake.commands()
    assert las.release_to_front_panel(front_switch_confirmed_off=True) is True
    assert fake.remote is False
    # emission lit → refuse even with the attestation
    fake2 = FakeRainbow()
    fake2.remote, fake2.software_emission = True, True
    with pytest.raises(InstrumentError, match="switch emission off first"):
        _laser(fake2).release_to_front_panel(front_switch_confirmed_off=True)
    with pytest.raises(EmissionControlDisabled, match="allow_emission_control|disabled"):
        NpiRainbow(transport=FakeRainbow(), port="COM13").release_to_front_panel(front_switch_confirmed_off=True)


# ── capabilities & registry ────────────────────────────────────────────────────────


def test_power_is_not_software_controllable():
    from mast.instruments.laser import LaserLimitError

    las = _laser(FakeRainbow())
    assert las.SUPPORTS == frozenset({"emission"})
    with pytest.raises(LaserLimitError, match="not software-controllable"):
        las.set_power(50)


def test_registry_builds_lazily_and_stop_all_switches_the_laser_off(tmp_path, monkeypatch):
    fake = FakeRainbow()
    fake.remote, fake.software_emission = True, True
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

    monkeypatch.setattr(npi, "open_transport", Factory)
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "npi1064", "type": "npi_rainbow", "name": "NPI Rainbow 1064 OEM", "enabled": True,
         "port": "COM13", "allow_emission_control": False}]}), encoding="utf-8")
    reg = InstrumentRegistry(config_path=p)
    las = reg.laser("npi1064")
    assert isinstance(las, NpiRainbow) and las.allow_emission_control is False
    assert opened == []                                   # constructing touched no hardware
    rep = reg.stop_all(wait_s=2)
    assert "npi1064" in rep["stopped"] and opened == ["COM13"]
    assert fake.laser is False
    reg.close_all()
    assert fake.closed is True


def test_build_requires_a_port():
    with pytest.raises(InstrumentError, match="'port' is required"):
        npi.build({"id": "npi1064", "type": "npi_rainbow"})
    las = npi.build({"id": "npi1064", "type": "npi_rainbow", "port": "COM13"})
    assert las.allow_emission_control is False and las.write_enabled is False      # both closed until the bench self-test
    assert npi.build({"id": "n", "port": "COM13", "write_enabled": True}).write_enabled is True
