"""Thorlabs power meter driver — TLPMX call sequence, error mapping and the sensor's wavelength range,
against a fake ``TLPMX_64.dll`` (a Python object with the same functions). The signatures are the ones in
Thorlabs' own examples (Light_Analysis_Examples: PM_SimpleExample.py, PM5020_ExtendedExample.py, the C++ simple
example). The real DLL is never loaded here.
"""
from __future__ import annotations

import ctypes
import json
import sys

import pytest

from mast.i18n import use_lang
from mast.instruments import thorlabs_pm as tpm
from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.instruments.power_meter import PowerMeterError
from mast.instruments.registry import InstrumentRegistry
from mast.instruments.thorlabs_pm import ThorlabsPM

RSRC = "USB0::0x1313::0x8076::M00000001::INSTR"
ERR_BUSY = -1073807246          # an arbitrary negative ViStatus (0xBFFF0072)
ERR_RANGE = -1073807343


@pytest.fixture(autouse=True)
def _english():
    with use_lang("en"):
        yield


def _ref(p):
    """The ctypes object behind ``ctypes.byref(x)`` (or a char buffer passed as is)."""
    return getattr(p, "_obj", p)


class FakeTlpm:
    """TLPMX_* functions with the real argument order; results land in the byref targets."""

    def __init__(self, *, devices=(RSRC,), busy=False, power_w=1.234e-3, wl_range=(400.0, 1100.0)):
        self.devices = list(devices)
        self.busy = busy
        self.power_w = power_w
        self.wl_range = wl_range
        self.wl = 633.0
        self.calls: list[tuple] = []
        self.open_handles: set[int] = set()
        self.init_args = None
        self._next = 1000

        def findRsrc(handle, count):
            self.calls.append(("findRsrc", handle))
            _ref(count).value = len(self.devices)
            return 0

        def getRsrcName(handle, index, buf):
            self.calls.append(("getRsrcName", handle, index))
            buf.value = self.devices[index].encode()
            return 0

        def init(rsrc, id_query, reset, handle):
            self.calls.append(("init", bytes(rsrc).decode(), id_query, reset))
            self.init_args = (id_query, reset)
            if self.busy:
                return ERR_BUSY
            self._next += 1
            _ref(handle).value = self._next
            self.open_handles.add(self._next)
            return 0

        def close(handle):
            self.calls.append(("close", handle))
            self.open_handles.discard(handle)
            return 0

        def errorMessage(handle, code, buf):
            buf.value = {ERR_BUSY: b"Resource busy", ERR_RANGE: b"Wavelength out of range"}.get(code, b"error")
            return 0

        def setWavelength(handle, nm, channel):
            self.calls.append(("setWavelength", handle, nm, channel))
            if not self.wl_range[0] <= nm <= self.wl_range[1]:
                return ERR_RANGE
            self.wl = nm
            return 0

        def getWavelength(handle, attribute, ref, channel):
            self.calls.append(("getWavelength", handle, attribute, channel))
            _ref(ref).value = {0: self.wl, 1: self.wl_range[0], 2: self.wl_range[1]}[attribute]
            return 0

        def measPower(handle, ref, channel):
            self.calls.append(("measPower", handle, channel))
            _ref(ref).value = self.power_w
            return 0

        def identificationQuery(handle, vendor, name, serial, rev):
            vendor.value, name.value, serial.value, rev.value = b"Thorlabs", b"PM101U", b"M00000001", b"1.4.0"
            return 0

        def getSensorInfo(handle, name, snr, msg, t, st, fl, channel):
            name.value, snr.value, msg.value = b"S120VC", b"000000001", b"Sensor connected"
            return 0

        for fn in (findRsrc, getRsrcName, init, close, errorMessage, setWavelength, getWavelength, measPower,
                   identificationQuery, getSensorInfo):
            setattr(self, "TLPMX_" + fn.__name__, fn)

    def names(self):
        return [c[0] for c in self.calls]


def _pm(lib, **kw) -> ThorlabsPM:
    return ThorlabsPM(lib=lib, device_id="pm", **kw)


# ── DLL handling ────────────────────────────────────────────────────────────────────────────


def test_default_dll_location_is_the_ivi_visa_folder_and_configurable(tmp_path):
    pm = ThorlabsPM(device_id="pm")
    assert pm.dll_location() == str(tpm.Path(r"C:\Program Files\IVI Foundation\VISA\Win64\Bin") / "TLPMX_64.dll")
    assert tpm.DEFAULT_DLL_DIR == r"C:\Program Files\IVI Foundation\VISA\Win64\Bin"
    assert ThorlabsPM(dll_dir=str(tmp_path)).dll_location() == str(tmp_path / "TLPMX_64.dll")
    assert ThorlabsPM(dll_path=str(tmp_path / "x.dll"), dll_dir="ignored").dll_location() == str(tmp_path / "x.dll")


def test_build_loads_nothing(monkeypatch):
    monkeypatch.setattr(tpm, "_load_library", lambda p: pytest.fail("build() must not load the DLL"))
    pm = tpm.build({"id": "pm", "type": "thorlabs_pm", "resource": "auto", "dll_dir": r"D:\vendor\bin"})
    assert isinstance(pm, ThorlabsPM) and pm.dll_location().endswith("TLPMX_64.dll")


def test_missing_dll_is_unavailable_not_a_crash(tmp_path):
    pm = ThorlabsPM(dll_dir=str(tmp_path / "nowhere"), device_id="pm")
    for call in (pm.status, pm.read_power_w, lambda: pm.set_wavelength_nm(532)):
        with pytest.raises(InstrumentUnavailable):
            call()
    if sys.platform == "win32":
        with pytest.raises(InstrumentUnavailable, match=r"not found at .*nowhere.*Optical Power Monitor"):
            pm.read_power_w()


def test_a_dll_that_does_not_load_is_unavailable(tmp_path, monkeypatch):
    dll = tmp_path / "TLPMX_64.dll"
    dll.write_bytes(b"not a dll")
    if sys.platform == "win32":
        with pytest.raises(InstrumentUnavailable, match="cannot load"):
            ThorlabsPM(dll_dir=str(tmp_path)).status()


def test_the_declared_signatures_match_the_thorlabs_headers():
    lib = FakeTlpm()
    _pm(lib)
    c = ctypes
    assert lib.TLPMX_measPower.argtypes == [c.c_uint32, c.POINTER(c.c_double), c.c_int16]
    assert lib.TLPMX_setWavelength.argtypes == [c.c_uint32, c.c_double, c.c_int16]
    assert lib.TLPMX_getWavelength.argtypes == [c.c_uint32, c.c_int16, c.POINTER(c.c_double), c.c_int16]
    assert lib.TLPMX_init.argtypes == [c.c_char_p, c.c_uint16, c.c_uint16, c.POINTER(c.c_uint32)]
    assert lib.TLPMX_findRsrc.argtypes == [c.c_uint32, c.POINTER(c.c_uint32)]
    assert lib.TLPMX_measPower.restype == c.c_int32


# ── session ──────────────────────────────────────────────────────────────────────────────────────


def test_auto_resource_finds_inits_without_reset_and_reads_power():
    lib = FakeTlpm()
    pm = _pm(lib)
    assert pm.read_power_w() == pytest.approx(1.234e-3)
    assert lib.names()[:4] == ["findRsrc", "getRsrcName", "init", "getWavelength"]
    assert lib.calls[2] == ("init", RSRC, 1, 0)                       # ID query on, reset off
    assert lib.init_args == (1, 0)
    assert ("measPower", 1001, 1) in lib.calls                        # channel 1
    pm.read_power_w()
    assert lib.names().count("init") == 1                             # one session, reused


def test_averaging_reads_the_requested_number_of_times():
    lib = FakeTlpm()
    pm = _pm(lib)
    pm.read_power_w(averages=5)
    assert lib.names().count("measPower") == 5
    with pytest.raises(PowerMeterError, match="averages"):
        pm.read_power_w(averages=0)


def test_explicit_resource_skips_the_search():
    lib = FakeTlpm(devices=("a", "b"))
    pm = _pm(lib, resource=RSRC)
    pm.read_power_w()
    assert "findRsrc" not in lib.names() and ("init", RSRC, 1, 0) in lib.calls


def test_no_meter_and_several_meters_are_reported():
    with pytest.raises(InstrumentUnavailable, match="No Thorlabs power meter found.*Optical Power Monitor"):
        _pm(FakeTlpm(devices=())).read_power_w()
    with pytest.raises(InstrumentError, match=r"2 Thorlabs power meters found.*set 'resource'"):
        _pm(FakeTlpm(devices=("USB0::a", "USB0::b"))).read_power_w()


def test_a_meter_held_by_another_program_says_so():
    with pytest.raises(InstrumentUnavailable) as ei:
        _pm(FakeTlpm(busy=True)).read_power_w()
    msg = str(ei.value)
    assert "init failed" in msg and "Resource busy" in msg and "Optical Power Monitor" in msg
    assert "0xBFFF0072" in msg


def test_close_releases_the_session_and_reopens_on_demand():
    lib = FakeTlpm()
    pm = _pm(lib)
    pm.read_power_w()
    pm.close()
    pm.close()                                                          # idempotent
    assert lib.open_handles == set() and lib.names().count("close") == 1
    pm.read_power_w()
    assert lib.names().count("init") == 2


# ── wavelength ───────────────────────────────────────────────────────────────────────────────────────


def test_wavelength_is_checked_against_the_sensors_own_range_before_the_dll():
    lib = FakeTlpm(wl_range=(400.0, 1100.0))
    pm = _pm(lib)
    assert pm.set_wavelength_nm(532) == 532.0
    assert ("setWavelength", 1001, 532.0, 1) in lib.calls
    n = lib.names().count("setWavelength")
    for nm in (350, 1200, float("nan")):
        with pytest.raises(PowerMeterError, match="wavelength"):
            pm.set_wavelength_nm(nm)
    assert lib.names().count("setWavelength") == n                     # refused, never sent, never clamped
    assert pm.status()["wavelength_range_nm"] == [400.0, 1100.0]


def test_read_power_at_a_wavelength_sets_it_first():
    lib = FakeTlpm()
    pm = _pm(lib)
    pm.read_power_w(wavelength_nm=785)
    names = lib.names()
    assert names.index("setWavelength") < names.index("measPower") and lib.wl == 785.0


def test_a_dll_error_on_set_wavelength_is_mapped_with_its_message():
    lib = FakeTlpm(wl_range=(400.0, 1100.0))
    pm = _pm(lib)
    pm.read_power_w()
    pm.MIN_NM, pm.MAX_NM = 200.0, 2000.0                                # pretend the sensor limits were unknown
    with pytest.raises(PowerMeterError, match=r"setWavelength failed with 0xBFFF0011.*Wavelength out of range"):
        pm.set_wavelength_nm(1500)


# ── data checks ──────────────────────────────────────────────────────────────────────────────────────


def test_a_non_finite_reading_is_an_error_not_a_number():
    with pytest.raises(PowerMeterError, match="non-finite"):
        _pm(FakeTlpm(power_w=float("nan"))).read_power_w()


def test_status_and_identify_report_identity_and_sensor():
    lib = FakeTlpm()
    pm = _pm(lib)
    st = pm.status()
    assert st["resource"] == RSRC and st["model"] == "PM101U" and st["serial_number"] == "M00000001"
    assert st["sensor_name"] == "S120VC" and st["sensor_serial"] == "000000001" and st["wavelength_nm"] == 633.0
    assert st["device_id"] == "pm"
    ident = _pm(FakeTlpm()).identify()
    assert ident["ok"] is True and ident["resources_seen"] == [RSRC] and ident["model"] == "PM101U"
    bad = _pm(FakeTlpm(devices=())).identify()
    assert bad["ok"] is False and "No Thorlabs power meter" in bad["error"]


def test_registry_builds_lazily(tmp_path, monkeypatch):
    lib = FakeTlpm()
    monkeypatch.setattr(ThorlabsPM, "_library", lambda self: lib)
    monkeypatch.setattr(tpm, "_declare", lambda l: None)
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "pm", "type": "thorlabs_pm", "name": "Thorlabs PM101U", "enabled": True, "resource": "auto"}]}),
        encoding="utf-8")
    reg = InstrumentRegistry(config_path=p)
    pm = reg.power_meter("pm")
    assert isinstance(pm, ThorlabsPM) and lib.calls == []                # building touched nothing
    assert pm.read_power_w() == pytest.approx(1.234e-3)
    reg.close_all()
    assert lib.open_handles == set()
