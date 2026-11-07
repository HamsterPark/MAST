"""scripts/witec/probe_witec_com.py: read-only by construction, useful when it fails, honest in its report."""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import ast
import importlib.util
import json

import pytest

from mast.instruments.witec import params as P
from mast.instruments.witec.bucs import BucsTypeLibMissing
from mast.instruments.witec.comtypes_client import ComtypesBucsClient
from mast.instruments.witec.fake_bucs import FakeBucsClient

PROBE = Path(__file__).resolve().parents[4] / "scripts" / "witec" / "probe_witec_com.py"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("probe_witec_com", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TripwireFake(FakeBucsClient):
    """A fake WITec Control that fails the test the moment anything is written, triggered, or requested."""

    def _boom(self, *a, **k):
        raise AssertionError("the read-only probe tried to write / trigger / request write access")

    set = _boom                                  # noqa: A003 - deliberately breaks the template
    trigger = _boom
    request_write_access = _boom
    _set_raw = _boom
    _trigger_raw = _boom
    _request_write_access_raw = _boom


def test_probe_never_writes_triggers_or_asks_for_write_access(probe, tmp_path):
    fake = TripwireFake()
    rc = probe.main(["--out", str(tmp_path), "--mast-root", _MASTV2_ROOT], client_factory=lambda: fake)
    assert rc == 0
    assert fake.writes == [] and [e for e in fake.log if e[0] != "get"] == []
    assert not fake.connected                                # closed afterwards


def test_the_probe_source_contains_no_call_to_any_write_method():
    tree = ast.parse(PROBE.read_text(encoding="utf-8"))
    banned = {"set", "trigger", "request_write_access", "_set_raw", "_trigger_raw", "SetValue",
              "SetValueNumeric", "SetValueString", "OperateTrigger", "RequestWriteAccess",
              "_request_write_access_raw"}
    hits = [(n.attr, n.lineno) for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in banned]
    assert hits == [], hits


def test_probe_writes_the_table_enums_features_and_reads(probe, tmp_path):
    fake = FakeBucsClient(omit=["grating", "center_wavelength"])
    assert probe.main(["--out", str(tmp_path)], client_factory=lambda: fake) == 0
    full = json.loads((tmp_path / "witec_probe.json").read_text(encoding="utf-8"))
    table = json.loads((tmp_path / "parameters.json").read_text(encoding="utf-8"))
    txt = (tmp_path / "parameters.txt").read_text(encoding="utf-8").splitlines()
    assert full["exit_code"] == 0 and full["connected"] and full["python_bits"] in (32, 64)
    assert full["program_version"].startswith("WITec Control") and full["parameter_count"] == len(table["parameters"])
    assert f"float:{P.ANALYZER}" in txt and f"trigger:{P.STOP_SEQUENCER}" in txt
    assert full["access"] == {"read": True, "write_held": False, "note": full["access"]["note"]}
    assert full["features"]["silent_spectrum"] is True and full["features"]["grating"] is False
    assert full["features"]["center_wavelength"] is False and full["missing_for_acquisition"] == []
    assert P.spectrograph_path(1, "SpectralUnit") in full["enums"]
    assert full["enums"][P.AUTOFOCUS_STATUS]["1"] == "LastSucceeded"
    r = full["reads"]
    assert r[P.PROGRAM_VERSION]["value"].startswith("WITec")
    assert r[P.spectrograph_path(1, "Grating")] == {"missing": True}          # the omitted path is reported missing
    assert r[P.ACTIVE_SEQUENCER_ACTIVITY]["error"].startswith("BucsNoValue")  # idle: no value, recorded not raised
    assert r[P.LASER_COUNT]["value"] == 5 and full["lasers"]["consistent"] is True
    assert full["guarded_groups_present"]["ApplicationControl"] >= 3


def test_probe_report_says_what_to_do_about_grating_and_centre(probe, tmp_path, capsys):
    fake = FakeBucsClient(omit=["grating"])
    probe.main(["--out", str(tmp_path)], client_factory=lambda: fake)
    out = capsys.readouterr().out
    assert "saved states (LoadWITecState)" in out and "[x] spectra over COM" in out and "[ ] grating settable" in out


def test_probe_no_enums_flag(probe, tmp_path):
    probe.main(["--out", str(tmp_path), "--no-enums"], client_factory=lambda: FakeBucsClient())
    assert "enums" not in json.loads((tmp_path / "witec_probe.json").read_text(encoding="utf-8"))


def test_witec_not_running_is_exit_2_and_says_who_has_to_open_it(probe, tmp_path, capsys):
    rc = probe.main(["--out", str(tmp_path)], client_factory=lambda: FakeBucsClient(witec_running=False))
    assert rc == 2
    assert "a person has to open it first" in capsys.readouterr().out
    full = json.loads((tmp_path / "witec_probe.json").read_text(encoding="utf-8"))
    assert full["connected"] is False and full["error"].startswith("BucsNotRunning") and full["exit_code"] == 2
    assert not (tmp_path / "parameters.json").exists()


def test_the_32_bit_problem_is_exit_4_and_forbids_touching_the_registry(probe, tmp_path, capsys):
    class Broken(FakeBucsClient):
        def _connect_raw(self):
            raise BucsTypeLibMissing("WITec COM registration problem (0x8002801D): Library not registered")

    rc = probe.main(["--out", str(tmp_path)], client_factory=lambda: Broken())
    out = capsys.readouterr().out
    assert rc == 4 and "Do NOT edit the registry" in out and "32-bit bridge" in out
    assert "0x8002801D" in json.loads((tmp_path / "witec_probe.json").read_text(encoding="utf-8"))["error"]


def test_missing_comtypes_is_exit_3(probe, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "comtypes", None)
    rc = probe.main(["--out", str(tmp_path), "--gen-dir", str(tmp_path / "gen")])
    assert rc == 3 and "--comtypes-dir" in capsys.readouterr().out


def test_default_client_is_the_comtypes_one(probe, tmp_path, monkeypatch):
    seen = {}

    class Spy(ComtypesBucsClient):
        def __init__(self, **kw):
            seen.update(kw)
            super().__init__(**kw, bundle_factory=lambda: (_ for _ in ()).throw(BucsTypeLibMissing("x")))

    monkeypatch.setattr("mast.instruments.witec.comtypes_client.ComtypesBucsClient", Spy)
    rc = probe.main(["--out", str(tmp_path), "--machine", "LABPC", "--gen-dir", str(tmp_path / "g")])
    assert rc == 4 and seen == {"machine": "LABPC", "gen_dir": str(tmp_path / "g")}


# ── the laser-safety questions: facts from the table, a check list, a read-only watch ─────────

def test_probe_reports_what_the_table_says_about_shutters_and_lists_the_manual_checks(probe, tmp_path, capsys):
    fake = TripwireFake()
    assert probe.main(["--out", str(tmp_path)], client_factory=lambda: fake) == 0
    full = json.loads((tmp_path / "witec_probe.json").read_text(encoding="utf-8"))
    facts = full["emission_facts"]
    assert P.LASER_SHUTTER in facts["shutter_paths"] and P.LASER_HAS_SHUTTER in facts["selected_laser_handles"]
    assert facts["other_shutter_paths"] == [] and any("only the SELECTED laser's" in n for n in facts["notes"])
    assert facts["select_no_laser_present"] is True
    assert facts["now"]["wavelength_nm"] == pytest.approx(532.138) and facts["now"]["shutter_open"] is False
    ids = [c["id"] for c in full["manual_checks"]]
    assert {"Q-a", "Q-a2", "Q-b1", "Q-b2", "Q-c", "Q-d"} <= set(ids)
    out = capsys.readouterr().out
    assert "still to be tried by hand" in out and "Q-b1" in out


def test_watch_mode_logs_every_change_it_sees_and_only_looks(probe):
    from mast.instruments.witec.fake_bucs import VirtualClock

    fake = TripwireFake()
    fake.connect()
    clock = VirtualClock(0.0)
    done = set()

    def person_at_the_machine(dt):               # time passes; a person opens the shutter, then selects the 633
        clock.sleep(dt)
        if clock.now() >= 1.0 and "open" not in done:
            done.add("open")
            fake.human_sets(P.LASER_SHUTTER, True)
        if clock.now() >= 2.0 and "select" not in done:
            done.add("select")
            fake._select_laser(2)                # noqa: SLF001

    lines: list[str] = []
    events = probe.watch_changes(fake, 3.0, 0.5, clock=clock.now, sleep=person_at_the_machine, emit=lines.append)
    initial = [e for e in events if e["initial"]]
    assert {e["path"] for e in initial} >= {"Selected|Shutter", "Selected|Wavelength", "Selected|HasShutter"}
    changes = [(e["path"], e["from"], e["to"]) for e in events if not e["initial"]]
    assert ("Selected|Shutter", False, True) in changes                       # the person opened it
    assert ("Selected|Wavelength", pytest.approx(532.138), 633.0) in changes  # and selected the 633 ...
    assert ("Selected|Shutter", True, False) in changes                       # ... whose own shutter reads closed
    t_open = next(e["t_s"] for e in events if e["path"] == "Selected|Shutter" and e["to"] is True)
    assert 1.0 <= t_open <= 1.6 and len(lines) == len(events)
    assert fake.writes == []


def test_watch_logs_a_value_that_cannot_be_read_as_information(probe):
    from mast.instruments.witec.bucs import BucsNoValue
    from mast.instruments.witec.fake_bucs import VirtualClock

    fake = TripwireFake()
    fake.connect()
    fake.inject(P.LASER_POWER, BucsNoValue("no value"), ops=("get",), times=1)
    clock = VirtualClock(0.0)
    events = probe.watch_changes(fake, 0.5, 0.25, clock=clock.now, sleep=clock.sleep, emit=lambda line: None)
    power = [e for e in events if e["path"] == "Selected|Power"]
    assert power[0]["to"] == "<BucsNoValue>" and power[-1]["to"] == pytest.approx(1.0)


def test_watch_flag_writes_watch_json_next_to_the_report(probe, tmp_path, capsys):
    fake = TripwireFake()
    rc = probe.main(["--out", str(tmp_path), "--watch", "0.3", "--watch-interval", "0.05"], client_factory=lambda: fake)
    assert rc == 0 and fake.writes == []
    watch = json.loads((tmp_path / "watch.json").read_text(encoding="utf-8"))
    full = json.loads((tmp_path / "witec_probe.json").read_text(encoding="utf-8"))
    assert watch["seconds"] == 0.3 and watch["events"] and full["watch"]["events"] == watch["events"]
    out = capsys.readouterr().out
    assert "watching for 0.3 s" in out and "Q-a:" in out
