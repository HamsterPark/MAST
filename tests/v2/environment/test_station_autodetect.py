"""Serial auto-detection and the station profile (design pitfall 8).

Auto-detection writes ``*IDN?`` (57600/9600/115200 baud) and a Modbus frame to every free
serial port at every service start. On the optical station those ports are the lasers', the
temperature controller's and the vendors' software's, so the optical profile forces it off
whatever ``environment_sensors.json`` says; on every station each probe leaves a trace in
the log (older versions left none, failed probe or not). The STM default is unchanged:
rig finds its Lakeshore this way.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mast import station
from mast.environment import autodetect, config as env_config
from mast.environment import dl7_vacuum, lakeshore_temp
from mast.environment.serial_transport import SerialUnavailable

PORTS = [SimpleNamespace(device="COM13", description="USB-SERIAL CH340", hwid=""),
         SimpleNamespace(device="COM14", description="Silicon Labs CP210x", hwid=""),
         SimpleNamespace(device="COM15", description="", hwid="")]


@pytest.fixture(autouse=True)
def _profile_is_clean(monkeypatch):
    monkeypatch.delenv(station.ENV_VAR, raising=False)
    station.reset()
    yield
    station.reset()


@pytest.fixture()
def probes(monkeypatch):
    """Three fake ports, and probes that find nothing but remember being asked."""
    asked: dict[str, list[str]] = {"lakeshore": [], "dl7": []}
    monkeypatch.setattr(autodetect, "list_serial_ports", lambda: list(PORTS))
    monkeypatch.setattr(autodetect, "probe_lakeshore",
                        lambda port, **k: asked["lakeshore"].append(port))
    monkeypatch.setattr(autodetect, "probe_dl7",
                        lambda port, *a, **k: asked["dl7"].append(port) or False)
    return asked


def _infos(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "mast.environment.autodetect" and r.levelno == logging.INFO]


# ── the policy ──────────────────────────────────────────────────────────────────
def test_the_stm_default_is_unchanged():
    assert env_config._DEFAULT == {"autodetect": True, "sensors": []}
    assert env_config.autodetect_policy({}) [0] is True
    assert env_config.autodetect_policy({"autodetect": True})[0] is True
    on, why = env_config.autodetect_policy({"autodetect": False})
    assert on is False and "false in environment_sensors.json" in why


def test_the_optical_profile_forces_it_off_whatever_the_file_says(monkeypatch):
    monkeypatch.setenv(station.ENV_VAR, "optical")
    for cfg in ({}, {"autodetect": True}, {"autodetect": False}):
        on, why = env_config.autodetect_policy(cfg)
        assert on is False and "optical station profile" in why


# ── a scan on an STM station: unchanged, and now it says what it does ───────────
def test_an_stm_scan_probes_every_free_port_and_logs_each_one(probes, caplog):
    with caplog.at_level(logging.INFO, logger="mast.environment.autodetect"):
        autodetect.build_environment_sensors(cfg={"autodetect": True, "sensors": []})
    assert probes["lakeshore"] == ["COM13", "COM14", "COM15"]
    assert probes["dl7"] == ["COM13", "COM14", "COM15"]
    lines = _infos(caplog)
    assert [l for l in lines if l.startswith("autodetect: probing COM13")], lines
    for port in ("COM13", "COM14", "COM15"):
        assert sum(1 for l in lines if l.startswith(f"autodetect: probing {port}")) == 1
    assert any("USB-SERIAL CH340" in l for l in lines), "the port's description is in the line"


def test_a_port_claimed_by_a_configured_sensor_is_not_probed_or_logged(probes, caplog):
    cfg = {"autodetect": True, "sensors": [{"id": "t", "name": "T", "type": "lakeshore_temp",
                                            "port": "COM14"}]}
    with caplog.at_level(logging.INFO, logger="mast.environment.autodetect"):
        autodetect.build_environment_sensors(cfg=cfg)
    assert probes["lakeshore"] == ["COM13", "COM15"]
    assert not [l for l in _infos(caplog) if "probing COM14" in l]


def test_a_file_that_turns_it_off_is_obeyed_and_says_so(probes, caplog):
    with caplog.at_level(logging.INFO, logger="mast.environment.autodetect"):
        sensors = autodetect.build_environment_sensors(cfg={"autodetect": False, "sensors": []})
    assert probes == {"lakeshore": [], "dl7": []}
    assert any("serial scan skipped" in l and "environment_sensors.json" in l for l in _infos(caplog))
    assert sensors, "the placeholders are still there"


# ── an optical station never scans ──────────────────────────────────────────────
@pytest.mark.parametrize("file_says", [True, False, None])
def test_an_optical_station_never_probes_a_port(probes, caplog, monkeypatch, file_says):
    monkeypatch.setenv(station.ENV_VAR, "optical")
    cfg = {"sensors": []} if file_says is None else {"autodetect": file_says, "sensors": []}
    with caplog.at_level(logging.INFO, logger="mast.environment.autodetect"):
        sensors = autodetect.build_environment_sensors(cfg=cfg)
    assert probes == {"lakeshore": [], "dl7": []}, "a probe reached a laser's or a controller's port"
    assert any("serial scan skipped - forced off by the optical station profile" in l
               for l in _infos(caplog))
    assert not [l for l in _infos(caplog) if "probing" in l]
    assert all(not autodetect.is_real_sensor(s) for s in sensors)


def test_a_configured_sensor_is_still_built_on_an_optical_station(probes, monkeypatch):
    """Forced off means no *scan*; a sensor the operator pinned in the file is theirs."""
    monkeypatch.setenv(station.ENV_VAR, "optical")
    cfg = {"autodetect": True, "sensors": [{"id": "t", "name": "Sample T", "type": "lakeshore_temp",
                                            "port": "COM19"}]}
    names = [s.name() for s in autodetect.build_environment_sensors(cfg=cfg)]
    assert "Sample T" in names and probes["lakeshore"] == []


# ── failed probes leave a DEBUG line ────────────────────────────────────────────
def test_a_failed_lakeshore_probe_is_logged_at_debug(monkeypatch, caplog):
    def boom(settings):
        raise SerialUnavailable("port is held by TC100.exe")

    monkeypatch.setattr(lakeshore_temp, "open_transport", boom)
    with caplog.at_level(logging.DEBUG, logger="mast.environment.lakeshore_temp"):
        assert lakeshore_temp.probe_lakeshore("COM15") is None
    msgs = [r for r in caplog.records if r.name == "mast.environment.lakeshore_temp"]
    assert msgs and all(r.levelno == logging.DEBUG for r in msgs)
    assert any("COM15" in r.getMessage() and "TC100.exe" in r.getMessage() for r in msgs)
    assert len(msgs) == len(lakeshore_temp.LSCI_SERIAL_CANDIDATES), "one line per line setting tried"


def test_a_wrong_answer_to_the_lakeshore_probe_is_logged_at_debug(monkeypatch, caplog):
    class Transport:
        def transact(self, *a, **k):
            return b"NKT,SuperK,1234\r\n"

        def close(self):
            pass

    monkeypatch.setattr(lakeshore_temp, "open_transport", lambda settings: Transport())
    with caplog.at_level(logging.DEBUG, logger="mast.environment.lakeshore_temp"):
        assert lakeshore_temp.probe_lakeshore("COM14") is None
    assert any("not a Lakeshore" in r.getMessage() for r in caplog.records)


def test_a_failed_dl7_probe_is_logged_at_debug(monkeypatch, caplog):
    def boom(settings):
        raise SerialUnavailable("no such port")

    monkeypatch.setattr(dl7_vacuum, "open_transport", boom)
    with caplog.at_level(logging.DEBUG, logger="mast.environment.dl7_vacuum"):
        assert dl7_vacuum.probe_dl7("COM16") is False
    rec = [r for r in caplog.records if r.name == "mast.environment.dl7_vacuum"]
    assert len(rec) == 1 and rec[0].levelno == logging.DEBUG and "COM16" in rec[0].getMessage()


def test_a_manual_device_scan_leaves_the_same_trace(monkeypatch, caplog):
    monkeypatch.setattr(autodetect, "list_serial_ports", lambda: list(PORTS[:2]))
    monkeypatch.setattr(autodetect, "discover_lakeshore", lambda port, **k: None)
    monkeypatch.setattr(autodetect, "probe_dl7", lambda port, *a, **k: False)
    with caplog.at_level(logging.INFO, logger="mast.environment.autodetect"):
        autodetect.discover_devices()
    lines = _infos(caplog)
    assert [l for l in lines if l.startswith("autodetect: probing COM13")]
    assert [l for l in lines if l.startswith("autodetect: probing COM14")]


# ── the config file ─────────────────────────────────────────────────────────────
def test_a_config_file_with_a_bom_is_read(tmp_path):
    """Windows PowerShell 5.1 writes UTF-8 *with* a BOM; plain utf-8 rejects it and the
    failure path falls back to ``autodetect: true`` - a hand-written false turned itself
    back on."""
    p = tmp_path / "environment_sensors.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"autodetect": False, "sensors": []}).encode("utf-8"))
    assert env_config.load_config(p)["autodetect"] is False


def test_a_config_file_without_a_bom_is_read_as_before(tmp_path):
    p = tmp_path / "environment_sensors.json"
    p.write_text(json.dumps({"autodetect": False, "sensors": [{"id": "x"}]}), encoding="utf-8")
    assert env_config.load_config(p) == {"autodetect": False, "sensors": [{"id": "x"}]}


def test_a_corrupt_file_still_falls_back_to_the_default(tmp_path):
    p = tmp_path / "environment_sensors.json"
    p.write_text("{not json", encoding="utf-8")
    assert env_config.load_config(p) == {"autodetect": True, "sensors": []}
