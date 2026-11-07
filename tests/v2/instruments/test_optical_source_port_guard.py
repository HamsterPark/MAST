"""port_guard knows the vendor programs of the optical-station sources (NPI Rainbow OEM, NKT CONTROL,
TC100, Thorlabs Optical Power Monitor), so a busy COM port / USB meter is blamed on the right program.

Process names are those of the vendor programs as installed on a typical optical-station PC.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from mast.core import port_guard as pg
from mast.i18n import use_lang


@pytest.fixture(autouse=True)
def _english():
    with use_lang("en"):
        yield


def _guard(optics, procs, tmp_path):
    return pg.PortGuard(
        nanonis=SimpleNamespace(host="127.0.0.1", port_main=6501, port_monitor=6502,
                                port_data=6503, port_emergency=6504),
        guard_config_path=tmp_path / "port_guard.json", optics_config=optics,
        connections_fn=lambda: [], local_ips_fn=lambda: {"127.0.0.1"},
        process_info_fn=lambda pid: {"name": "x", "exe": None, "cmdline": None},
        processes_fn=lambda: procs,
        serial_owners_fn=lambda ports: {"owners": {}, "complete": True},
        own_pid=1, publish=False)


def test_vendor_process_names_are_registered_and_the_old_ones_are_untouched():
    for name in ("rainbow oem.exe", "rainbow_oem.exe", "control.exe", "tc100.exe",
                 "thorlabs optical power monitor.exe"):
        assert name in pg.DEFAULT_VENDOR_PROCESSES
    for name in ("rainbow oem.exe", "rainbow_oem.exe", "control.exe", "tc100.exe"):
        assert name in pg.DEFAULT_SERIAL_VENDOR_PROCESSES
    # the power meter is a USB device: its GUI is an advisory for it, never a serial-port suspect
    assert "thorlabs optical power monitor.exe" not in pg.DEFAULT_SERIAL_VENDOR_PROCESSES
    assert "thorlabs optical power monitor.exe" in pg._USB_VENDORS["thorlabs_pm"]
    assert pg.DEFAULT_SERIAL_VENDOR_PROCESSES[:2] == ("labview.exe", "pimikromove.exe")
    assert "labview.exe" in pg.DEFAULT_VENDOR_PROCESSES and "kinesis.exe" in pg.DEFAULT_VENDOR_PROCESSES
    assert pg.DEFAULT_VENDOR_PROCESSES["control.exe"].startswith("NKT CONTROL")


@pytest.mark.parametrize("port,proc", [("COM13", "Rainbow OEM.exe"), ("COM14", "CONTROL.exe"), ("COM15", "TC100.exe")])
def test_a_failed_open_names_the_running_vendor_program(tmp_path, port, proc):
    hint = pg.serial_open_hint(port, processes_fn=lambda: [{"pid": 4242, "name": proc},
                                                           {"pid": 9, "name": "explorer.exe"}],
                               guard_config_path=tmp_path / "none.json")
    assert f"{port} cannot be opened and may be in use" in hint
    assert f"{proc} (pid 4242)" in hint and "explorer" not in hint


def test_a_running_vendor_program_is_a_warning_for_a_configured_serial_device(tmp_path):
    optics = {"devices": [{"id": "superk", "type": "nkt_superk", "enabled": True, "port": "COM14"}]}
    rep = _guard(optics, [{"pid": 77, "name": "CONTROL.exe"}], tmp_path).check()
    assert rep.ok                                                   # advice, not a block
    hits = [f for f in rep.warnings if f.kind == "serial_port"]
    assert hits and hits[0].process == "CONTROL.exe" and "COM14" in hits[0].resource
    assert "NKT CONTROL" in hits[0].detail
    off = {"devices": [{"id": "superk", "type": "nkt_superk", "enabled": False, "port": "COM14"}]}
    assert not [f for f in _guard(off, [{"pid": 77, "name": "CONTROL.exe"}], tmp_path).check().warnings
                if f.kind == "serial_port"]


def test_the_power_meter_gui_is_a_usb_advisory_for_a_configured_meter(tmp_path):
    optics = {"devices": [{"id": "pm", "type": "thorlabs_pm", "enabled": True}]}
    rep = _guard(optics, [{"pid": 31, "name": "Thorlabs Optical Power Monitor.exe"}], tmp_path).check()
    usb = [f for f in rep.warnings if f.kind == "usb_device"]
    assert rep.ok and usb and usb[0].process == "Thorlabs Optical Power Monitor.exe"
    assert "Thorlabs Optical Power Monitor" in usb[0].detail
