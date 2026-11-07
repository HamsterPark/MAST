"""BucsClient: the write guard, type/range checks, write-access semantics, connection loss — on the fake WITec Control."""
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

import math

import pytest

from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.instruments.microscope import WriteAccessDenied
from mast.instruments.witec import params as P
from mast.instruments.witec.bucs import (BucsClient, BucsConnectionLost, BucsError, BucsForbiddenPath,
                                          BucsNoValue, BucsNoWriteAccess, BucsNotRunning,
                                          BucsParameterDisabled, BucsParameterMissing, BucsValueError,
                                          ReadOnlyBucs, check_write_path, forbidden_reason)
from mast.instruments.witec.fake_bucs import FakeBucsClient, VirtualClock


@pytest.fixture()
def fake():
    f = FakeBucsClient(clock=VirtualClock().now)
    yield f
    f.close()


@pytest.fixture()
def writable(fake):
    fake.operator_press_com_automation()
    assert fake.request_write_access(True)
    return fake


# ── the write guard ────────────────────────────────────────────────────────────────────────────

CONTRACT_BLACKLIST = ["ApplicationControl|NewProject", "ApplicationControl|ExitApplication",
                      "ApplicationControl|LoadConfiguration"]
EXTRA_REFUSED = [
    "ApplicationControl|UpdateHardware", "ApplicationControl|FileNameToAppendToProject",
    "UserParameters|COMInterop|LocalLockoutLicensed", "UserParameters|COMInterop|RevokeWriteAccess",
    "UserParameters|COMInterop|AllowReadAccess", "UserParameters|COMInterop|LocalLockout",
    "UserParameters|HardwareConstraints|ExtendedZStepperTravel",
    "UserParameters|HardwareConstraints|SetExtendedZStepperTravel",
    "UserParameters|HardwareConstraints|MaximumZStepperTravel",
    P.ROOT_MICROSCOPE + "MotorizedXYZ|XYAxes|SetSamplePositionToZero",
    P.ROOT_MICROSCOPE + "MotorizedXYZ|XYAxes|ResetCoordinateSystem",
    P.ROOT_MICROSCOPE + "MotorizedXYZ|XYAxes|MoveToCalibrationPosition",
    P.ROOT_MICROSCOPE + "MotorizedXYZ|ZAxis|SetSamplePositionZToZero",
    P.ROOT_MICROSCOPE + "MotorizedXYZ|ZAxis|SetHardwarePositionZToZero",
]


def test_contract_blacklist_constants_are_exactly_the_three():
    assert set(P.DANGEROUS_PATHS) == set(CONTRACT_BLACKLIST)


@pytest.mark.parametrize("path", CONTRACT_BLACKLIST)
def test_blacklisted_paths_are_refused_for_set_and_trigger_even_with_write_access(writable, path):
    """The contract's three: NewProject deletes the project, ExitApplication closes WITec Control (COM cannot start
    it again), LoadConfiguration swaps the whole GUI configuration."""
    for call in (lambda: writable.set(path, "x"), lambda: writable.trigger(path)):
        with pytest.raises(BucsForbiddenPath, match="write guard"):
            call()
    assert [w for w in writable.writes if w[1] == path] == []         # nothing reached the "server"


@pytest.mark.parametrize("path", EXTRA_REFUSED)
def test_extended_guard_groups_are_refused(writable, path):
    assert forbidden_reason(path) is not None
    for call in (lambda: writable.set(path, 1.0), lambda: writable.trigger(path)):
        with pytest.raises(BucsForbiddenPath):
            call()
    assert [w for w in writable.writes if w[1] == path] == []


@pytest.mark.parametrize("spelling", [
    "applicationcontrol|newproject", "APPLICATIONCONTROL|NEWPROJECT", " ApplicationControl | NewProject ",
    "ApplicationControl|NewProject ", "ApplicationControl |ExitApplication"])
def test_guard_is_case_and_whitespace_insensitive(writable, spelling):
    with pytest.raises(BucsForbiddenPath):
        writable.trigger(spelling)
    with pytest.raises(BucsForbiddenPath):
        check_write_path(spelling)


def test_guard_runs_before_anything_touches_the_server():
    f = FakeBucsClient(witec_running=False)          # WITec is not even running
    with pytest.raises(BucsForbiddenPath):
        f.trigger("ApplicationControl|ExitApplication")
    assert f.connects == 0


def test_guard_does_not_block_reads(writable):
    """Reading the loaded configuration name is harmless and useful."""
    assert writable.get("ApplicationControl|LoadConfiguration") == ""
    assert writable.get(P.MAX_Z_STEPPER_TRAVEL) == 200.0


def test_ordinary_paths_pass_the_guard(writable):
    assert forbidden_reason(P.ANALYZER) is None
    assert forbidden_reason(P.STOP_SEQUENCER) is None
    assert forbidden_reason(P.XY_MOVE) is None
    writable.set(P.ANALYZER, 12.0)
    assert writable.get(P.ANALYZER) == 12.0


@pytest.mark.parametrize("bad", ["", "   ", "A||B", "|A", "A|", "Üser|X", "A|B\x00", None, 5, "x" * 600])
def test_malformed_paths_are_value_errors(writable, bad):
    with pytest.raises(BucsValueError):
        writable.set(bad, 1)
    with pytest.raises(BucsValueError):
        writable.exists(bad)


def test_no_client_overrides_the_guarded_template_methods():
    """The guard lives in BucsClient.set / trigger. A subclass that overrides them would silently drop it."""
    def walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from walk(sub)

    import mast.instruments.witec.comtypes_client  # noqa: F401 - make sure it is loaded
    subs = [c for c in walk(BucsClient) if c.__module__.startswith("mast.")]      # not other tests' doubles
    names = {c.__name__ for c in subs}
    assert {"FakeBucsClient", "ComtypesBucsClient"} <= names
    for c in subs:
        assert c.set is BucsClient.set, f"{c.__name__} overrides set()"
        assert c.trigger is BucsClient.trigger, f"{c.__name__} overrides trigger()"


def test_read_only_view_has_no_write_methods(fake):
    ro = ReadOnlyBucs(fake)
    assert ro.get(P.PROGRAM_VERSION).startswith("WITec Control")
    assert isinstance(ro.list_parameters(), dict)
    for forbidden in ("set", "trigger", "request_write_access", "_set_raw", "_trigger_raw"):
        with pytest.raises(AttributeError):
            getattr(ro, forbidden)
    with pytest.raises(AttributeError):
        ro.anything = 1


# ── write access is handed over by a person ───────────────────────────────────────────────────────

def test_write_without_access_fails_clearly_and_is_a_write_access_denied(fake):
    with pytest.raises(BucsNoWriteAccess, match="COM Automation") as ei:
        fake.set(P.ANALYZER, 5.0)
    assert isinstance(ei.value, WriteAccessDenied) and isinstance(ei.value, InstrumentError)
    with pytest.raises(BucsNoWriteAccess):
        fake.trigger(P.STOP_SEQUENCER)
    assert fake.writes == []


def test_request_write_access_needs_the_operator_and_never_loops(fake):
    assert fake.has_write_access() is False
    assert fake.request_write_access(True) is False           # nobody pressed the button
    asks = [e for e in fake.log if e[0] == "request_write"]
    assert len(asks) == 1                                      # asked exactly once
    fake.operator_press_com_automation()
    assert fake.request_write_access(True) is True
    assert fake.has_write_access()
    assert fake.gui_locked                                     # the GUI is locked while it is held
    assert fake.request_write_access(False) is True           # handing it back
    assert not fake.has_write_access()


def test_operator_revoke_takes_write_access_away_mid_run(writable):
    writable.set(P.ANALYZER, 1.0)
    writable.operator_revoke()
    with pytest.raises(BucsNoWriteAccess):
        writable.set(P.ANALYZER, 2.0)
    assert writable.get(P.ANALYZER) == 1.0                    # reads still work


def test_witec_restart_needs_the_button_again(writable):
    writable.witec_exit()
    with pytest.raises(BucsConnectionLost):
        writable.set(P.ANALYZER, 3.0)
    assert not writable.connected
    with pytest.raises(BucsNotRunning):
        writable.connect()
    writable.witec_start()
    writable.connect()
    assert writable.request_write_access(True) is False        # consent did not survive the restart
    writable.operator_press_com_automation()
    assert writable.request_write_access(True) is True


def test_unavailable_errors_are_instrument_unavailable(fake):
    f = FakeBucsClient(witec_running=False)
    with pytest.raises(BucsNotRunning) as ei:
        f.get(P.PROGRAM_VERSION)
    assert isinstance(ei.value, InstrumentUnavailable) and isinstance(ei.value, BucsError)


# ── types and ranges: refuse, never clamp ──────────────────────────────────────────────────────────────

def test_out_of_range_is_refused_never_clamped(writable):
    before = writable.get(P.LASER_POWER)
    for bad in (-1.0, 50.5, 1e9):
        with pytest.raises(BucsValueError, match="declares"):
            writable.set(P.LASER_POWER, bad)
    assert writable.get(P.LASER_POWER) == before
    writable.set(P.Z_DESIRED, 999.0)                          # inside the parameter's own (wide) range
    with pytest.raises(BucsValueError):
        writable.set(P.Z_DESIRED, 1000.5)


@pytest.mark.parametrize("path,bad", [
    (P.ANALYZER, "10"), (P.ANALYZER, True), (P.ANALYZER, math.nan), (P.ANALYZER, math.inf),
    (P.ANALYZER, None), (P.ANALYZER_COUPLER, 1), (P.ANALYZER_COUPLER, "true"),
    (P.SERIES_ACCUMULATIONS, 2.5), (P.SERIES_ACCUMULATIONS, True), (P.AUTOSAVE_FILE_NAME, 5),
    (P.spectrograph_path(1, "Grating"), 1.5), (P.spectrograph_path(1, "Grating"), None)])
def test_wrong_types_are_refused(writable, path, bad):
    with pytest.raises(BucsValueError):
        writable.set(path, bad)


def test_integral_float_is_accepted_for_int_and_enum_takes_index_or_label(writable):
    writable.set(P.SERIES_ACCUMULATIONS, 3.0)
    assert writable.get(P.SERIES_ACCUMULATIONS) == 3
    g = P.spectrograph_path(1, "Grating")
    writable.set(g, 2)
    assert writable.get_enum(g) == (2, "G3: 1800 g/mm BLZ=500nm")
    writable.set(g, "G2: 600 g/mm BLZ=500nm")
    assert writable.get(g) == 1
    with pytest.raises(BucsError):
        writable.set(g, 7)                                     # no such index: the server says no
    assert writable.enum_values(g) == {0: "G1: 300 g/mm BLZ=500nm", 1: "G2: 600 g/mm BLZ=500nm",
                                       2: "G3: 1800 g/mm BLZ=500nm"}


def test_trigger_set_and_status_write_are_kind_errors(writable):
    with pytest.raises(BucsValueError, match="trigger"):
        writable.set(P.STOP_SEQUENCER, True)
    with pytest.raises(BucsValueError, match="not a trigger"):
        writable.trigger(P.ANALYZER)
    with pytest.raises(BucsValueError, match="read-only"):
        writable.set(P.IS_SEQUENCER_ACTIVE, True)
    with pytest.raises(BucsValueError, match="no value"):
        writable.get(P.STOP_SEQUENCER)


def test_missing_and_disabled_parameters(writable):
    with pytest.raises(BucsParameterMissing):
        writable.get("UserParameters|Nope|X")
    assert writable.exists(P.ANALYZER) and not writable.exists("UserParameters|Nope|X")
    writable.disable(P.FIELD_STOP)
    with pytest.raises(BucsParameterDisabled):
        writable.set(P.FIELD_STOP, 50.0)


def test_status_without_value_is_bucs_no_value(fake):
    """No sequencer active → ``CurrentActivity`` has no value (E_ACCESSDENIED on the real server)."""
    with pytest.raises(BucsNoValue):
        fake.get(P.ACTIVE_SEQUENCER_ACTIVITY)
    assert fake.get(P.IS_SEQUENCER_ACTIVE) is False


# ── the table ──────────────────────────────────────────────────────────────────────────────────────────

def test_list_parameters_reports_normalised_types(fake):
    table = fake.list_parameters()
    assert table[P.ANALYZER] == "float" and table[P.ANALYZER_COUPLER] == "bool"
    assert table[P.STOP_SEQUENCER] == "trigger" and table[P.spectrograph_path(1, "Grating")] == "enum"
    assert table[P.IS_SEQUENCER_ACTIVE] == "status:bool" and table[P.PROGRAM_VERSION] == "status:string"
    assert list(table) == sorted(table)


def test_omitting_a_feature_removes_exactly_its_paths():
    f = FakeBucsClient(omit=["grating", "center_wavelength"])
    assert not f.exists(P.spectrograph_path(1, "Grating"))
    assert not f.exists(P.spectrograph_path(1, "CenterWavelength"))
    assert f.exists(P.spectrograph_path(1, "SpectralUnit"))


def test_fake_is_reachable_through_the_bucs_module():
    from mast.instruments.witec import bucs

    assert bucs.FakeBucsClient is FakeBucsClient and bucs.VirtualClock is VirtualClock
    with pytest.raises(AttributeError):
        bucs.NoSuchThing
