"""ComtypesBucsClient against a COM-shaped fake (no comtypes, no COM): the connection recipe, the dedicated COM
thread, the write guard, HRESULT mapping, timeouts, the generated-code cache directory.

What this cannot tell: whether a real 64-bit comtypes reaches the 32-bit-only registration. That is
the first thing the probe script answers."""
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

import os
import socket
import subprocess
import threading
import types

import pytest

from mast.instruments.base import InstrumentUnavailable
from mast.instruments.witec import params as P
from mast.instruments.witec.bucs import (ParamInfo, BucsConnectionLost, BucsDependencyMissing, BucsError,
                                          BucsForbiddenPath, BucsNoValue, BucsNoWriteAccess, BucsNotRunning,
                                          BucsParameterMissing, BucsTimeout, BucsTypeLibMissing,
                                          BucsValueError)
from mast.instruments.witec.comtypes_client import (HRESULT, ComtypesBucsClient, com_text, default_gen_dir,
                                                    hresult_of, map_com_error, prepare_comtypes_gen_cache)
from mast.instruments.witec.fake_bucs import FakeBucsClient
from tests.v2.instruments.witec.fake_com import ComWorld, com_error, os_error

from tests.v2.instruments.witec.test_bucs import CONTRACT_BLACKLIST, EXTRA_REFUSED


class Rig:
    def __init__(self, **client_kw):
        self.backend = FakeBucsClient()
        self.world = ComWorld(self.backend)
        kw = dict(bundle_factory=self.world.bundle, call_timeout_s=3.0, connect_timeout_s=10.0)
        kw.update(client_kw)
        self.client = ComtypesBucsClient(**kw)

    def grant(self):
        self.backend.operator_press_com_automation()
        assert self.client.request_write_access(True)


@pytest.fixture()
def rig():
    r = Rig()
    yield r
    r.client.close()


# ── lazy import ───────────────────────────────────────────────────────────────────────────────

def test_importing_the_client_module_never_imports_comtypes():
    code = (f"import sys; sys.path.insert(0, {_MASTV2_ROOT!r}); "
            "import mast.instruments.witec.comtypes_client, mast.instruments.witec.microscope, "
            "mast.skills.builtins.optics_witec; assert 'comtypes' not in sys.modules, 'comtypes was imported'")
    env = dict(os.environ, MAST_LANG="zh")
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                          timeout=180, cwd=os.environ.get("TEMP") or None)
    assert proc.returncode == 0, proc.stderr[-800:]


def test_missing_comtypes_is_a_clear_unavailable_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "comtypes", None)           # makes `import comtypes` raise ImportError
    client = ComtypesBucsClient(gen_dir=tmp_path)
    try:
        with pytest.raises(BucsDependencyMissing, match="comtypes is not importable") as ei:
            client.connect()
        assert isinstance(ei.value, InstrumentUnavailable)
        assert not client.connected
    finally:
        client.close()


# ── the vendor's recipe ───────────────────────────────────────────────────────────────────────────

def test_connect_follows_the_vendor_recipe(rig):
    rig.client.connect()
    w = rig.world
    assert w.get_module_args == [(P.TYPELIB_ID, 1, 0)]
    assert len(w.create_args) == 1
    ca = w.create_args[0]
    assert ca["cls"] is w.lib.CBUCSCore and ca["interface"] is w.lib.IBUCSCore
    assert ca["machine"] == socket.gethostname()               # the local host name, as the SDK does (not None)
    assert ("<core>", "IBUCSAccess") in w.qi_log
    assert w.init_calls == 1 and rig.client.connected


def test_machine_can_be_overridden():
    r = Rig(machine="LABPC")
    try:
        r.client.connect()
        assert r.world.create_args[0]["machine"] == "LABPC"
    finally:
        r.client.close()


def test_all_com_calls_run_on_one_dedicated_thread(rig):
    rig.client.get(P.PROGRAM_VERSION)
    rig.client.list_parameters()
    rig.grant()
    rig.client.set(P.ANALYZER, 3.0)
    threads = {tid for _, tid in rig.world.calls}
    assert len(threads) == 1 and threading.get_ident() not in threads


def test_calls_from_many_threads_work_because_com_is_funnelled(rig):
    rig.client.connect()
    errors, results = [], []

    def work(i):
        try:
            for _ in range(10):
                results.append(rig.client.get(P.PROGRAM_VERSION))
                rig.client.exists(P.ANALYZER)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert not errors and len(results) == 80
    assert len({tid for _, tid in rig.world.calls}) == 1


def test_parameter_table_matches_the_servers_and_skips_group_nodes(rig):
    got = rig.client.list_parameters()
    assert got == rig.backend.list_parameters()
    assert "UserParameters|SomeGroup" not in got                # IID 000… = grouping node


# ── values through the right interfaces ──────────────────────────────────────────────────────────────

def test_values_use_the_interface_of_their_kind(rig):
    rig.grant()
    c, w = rig.client, rig.world
    c.set(P.ANALYZER, 33.5)
    assert c.get(P.ANALYZER) == 33.5
    c.set(P.ANALYZER_COUPLER, False)
    assert c.get(P.ANALYZER_COUPLER) is False
    c.set(P.AUTOSAVE_FILE_NAME, "abc")
    assert c.get(P.AUTOSAVE_FILE_NAME) == "abc"
    c.set(P.SERIES_ACCUMULATIONS, 4)
    assert c.get(P.SERIES_ACCUMULATIONS) == 4
    g = P.spectrograph_path(1, "Grating")
    c.set(g, 1)
    assert c.get(g) == 1 and c.get_enum(g) == (1, "G2: 600 g/mm BLZ=500nm")
    assert c.enum_values(g)[2].startswith("G3")
    c.set(g, "G1: 300 g/mm BLZ=500nm")
    assert c.get(g) == 0
    assert c.get(P.IS_SEQUENCER_ACTIVE) is False                 # status bool via GetSingleValueAsInt
    assert c.get(P.PROGRAM_VERSION).startswith("WITec Control")  # status string
    assert isinstance(c.get(P.LASER_WAVELENGTH), float)
    c.trigger(P.STOP_SEQUENCER)
    bound = {}
    for path, iface in w.qi_log:
        bound.setdefault(path, set()).add(iface)
    assert "IBUCSFloat" in bound[P.ANALYZER] and "IBUCSSingleValueHasLimits" in bound[P.ANALYZER]
    assert "IBUCSFloatMinMaxValues" in bound[P.ANALYZER]
    assert "IBUCSBool" in bound[P.ANALYZER_COUPLER]
    assert "IBUCSString" in bound[P.AUTOSAVE_FILE_NAME]
    assert {"IBUCSInt", "IBUCSIntMinMaxValues"} <= bound[P.SERIES_ACCUMULATIONS]
    assert "IBUCSEnum" in bound[g]
    assert "IBUCSTrigger" in bound[P.STOP_SEQUENCER] and "IBUCSStatusContainer" in bound[P.IS_SEQUENCER_ACTIVE]
    assert not any(i in ("IBUCSFloat", "IBUCSBool") for i in bound[P.STOP_SEQUENCER])


def test_a_status_without_value_is_bucs_no_value(rig):
    with pytest.raises(BucsNoValue):
        rig.client.get(P.ACTIVE_SEQUENCER_NAME)                 # E_ACCESSDENIED while idle


def test_unknown_parameter_is_missing_not_a_com_error(rig):
    with pytest.raises(BucsParameterMissing):
        rig.client.get("UserParameters|DoesNotExist")
    # and the server itself says "Invalid Class-ID" when asked for an object it does not have
    ghost = ParamInfo("UserParameters|Nope", "float", "float", "")
    with pytest.raises(BucsParameterMissing, match="no such parameter"):
        rig.client._run(lambda: rig.client._bind(ghost), phase="read")        # noqa: SLF001


# ── the write guard on the real-COM path ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", CONTRACT_BLACKLIST + EXTRA_REFUSED)
def test_guard_holds_on_the_com_client_nothing_reaches_the_server(rig, path):
    rig.grant()
    before = list(rig.world.writes)
    for call in (lambda: rig.client.set(path, "x"), lambda: rig.client.trigger(path)):
        with pytest.raises(BucsForbiddenPath):
            call()
    assert rig.world.writes == before


def test_range_comes_from_the_limits_the_parameter_declares(rig):
    rig.grant()
    n = len(rig.world.writes)
    for bad in (-0.5, 51.0):
        with pytest.raises(BucsValueError, match="declares"):
            rig.client.set(P.LASER_POWER, bad)
    assert len(rig.world.writes) == n                             # refused before sending
    rig.client.set(P.LASER_POWER, 7.0)
    assert rig.client.get(P.LASER_POWER) == 7.0


def test_write_without_access_never_reaches_the_server(rig):
    with pytest.raises(BucsNoWriteAccess):
        rig.client.set(P.ANALYZER, 5.0)
    with pytest.raises(BucsNoWriteAccess):
        rig.client.trigger(P.STOP_SEQUENCER)
    assert rig.world.writes == []
    assert rig.client.request_write_access(True) is False        # the person has not pressed the button
    rig.grant()
    rig.client.set(P.ANALYZER, 5.0)
    assert ("set", P.ANALYZER, 5.0) in rig.world.writes


def test_trigger_that_the_server_would_accept_is_sent_once(rig):
    rig.grant()
    rig.client.trigger(P.STOP_SEQUENCER)
    assert [w for w in rig.world.writes if w[0] == "trigger"] == [("trigger", P.STOP_SEQUENCER, None)]


# ── HRESULT mapping ─────────────────────────────────────────────────────────────────────────────────────────

NOCB = "No callback function pointer registered for getting the default interface for a Sub-System Name"


@pytest.mark.parametrize("exc,phase,cls", [
    (os_error(HRESULT.REGDB_E_CLASSNOTREG, "Class not registered"), "connect", BucsNotRunning),
    (os_error(HRESULT.E_ACCESSDENIED, "Access is denied"), "connect", BucsNotRunning),
    (os_error(HRESULT.RPC_S_SERVER_UNAVAILABLE, "The RPC server is unavailable"), "connect", BucsNotRunning),
    (com_error(HRESULT.RPC_S_SERVER_UNAVAILABLE, "The RPC server is unavailable."), "read", BucsConnectionLost),
    (com_error(HRESULT.RPC_S_CALL_FAILED, "The remote procedure call failed."), "write", BucsConnectionLost),
    (com_error(HRESULT.RPC_S_CALL_FAILED_DNE, "failed and did not execute"), "trigger", BucsConnectionLost),
    (com_error(HRESULT.RPC_E_DISCONNECTED, "The object invoked has disconnected"), "status", BucsConnectionLost),
    (com_error(HRESULT.RPC_E_SERVER_DIED, "server died"), "read", BucsConnectionLost),
    (com_error(HRESULT.E_FAIL, "Unspecified error", NOCB), "table", BucsConnectionLost),
    (os_error(HRESULT.TYPE_E_LIBNOTREGISTERED, "Library not registered"), "typelib", BucsTypeLibMissing),
    (com_error(HRESULT.REGDB_E_IIDNOTREG, "Interface not registered"), "read", BucsTypeLibMissing),
    (com_error(HRESULT.E_FAIL, "Interface not registered"), "read", BucsTypeLibMissing),
    (com_error(HRESULT.E_NOINTERFACE, "No such interface supported"), "typelib", BucsTypeLibMissing),
    (com_error(HRESULT.E_NOINTERFACE, "No such interface supported"), "read", BucsError),
    (com_error(HRESULT.E_FAIL, "Unspecified error", "Invalid Class-ID (GUID_NULL) for Sub-System with name: X"),
     "table", BucsParameterMissing),
    (com_error(HRESULT.E_ACCESSDENIED, "Access is denied."), "status", BucsNoValue),
    (com_error(HRESULT.E_ACCESSDENIED, "Access is denied."), "read", BucsNoValue),
    (com_error(HRESULT.E_ACCESSDENIED, "Access is denied."), "write", BucsNoWriteAccess),
    (com_error(HRESULT.E_ACCESSDENIED, "Access is denied."), "trigger", BucsNoWriteAccess),
])
def test_hresult_mapping(exc, phase, cls):
    got = map_com_error(exc, phase=phase, path="X|Y")
    assert type(got) is cls, (type(got), str(got))


def test_exact_class_matters_for_the_ones_that_mean_unavailable():
    assert isinstance(map_com_error(com_error(HRESULT.RPC_S_SERVER_UNAVAILABLE, "x"), phase="read"),
                      InstrumentUnavailable)
    assert not isinstance(map_com_error(com_error(HRESULT.E_NOINTERFACE, "x"), phase="read"), InstrumentUnavailable)


def test_type_library_error_says_do_not_touch_the_registry_and_names_the_bridge():
    msg = str(map_com_error(os_error(HRESULT.TYPE_E_LIBNOTREGISTERED, "Library not registered"), phase="typelib"))
    assert "Do NOT edit the registry" in msg and "32-bit" in msg and "bridge" in msg
    assert "0x8002801D" in msg


def test_not_running_message_tells_the_person_to_open_witec():
    msg = str(map_com_error(os_error(HRESULT.REGDB_E_CLASSNOTREG, "Class not registered"), phase="connect"))
    assert "a person has to open WITec Control" in msg and "0x80040154" in msg


def test_unsigned_hresults_and_plain_args_are_understood():
    assert hresult_of(com_error(0x80070005, "x")) == HRESULT.E_ACCESSDENIED
    assert hresult_of(RuntimeError(HRESULT.RPC_S_CALL_FAILED)) == HRESULT.RPC_S_CALL_FAILED
    assert hresult_of(RuntimeError("no code")) is None
    assert hresult_of(ValueError(5)) is None
    assert "Class not registered" in com_text(os_error(HRESULT.REGDB_E_CLASSNOTREG, "Class not registered"))


def test_unknown_hresult_keeps_code_and_text_and_an_existing_error_passes_through():
    got = map_com_error(com_error(-2147352567, "Exception occurred."), phase="read", path="A|B")
    assert type(got) is BucsError and "0x80020009" in str(got) and "[A|B]" in str(got)
    e = BucsTimeout("x")
    assert map_com_error(e) is e


# ── connection failures through the client ──────────────────────────────────────────────────────────────────

def test_witec_closed_is_not_running(rig):
    rig.backend.witec_exit()
    with pytest.raises(BucsNotRunning, match="a person has to open WITec Control"):
        rig.client.connect()
    assert not rig.client.connected
    rig.backend.witec_start()
    rig.client.connect()                                        # a later attempt works
    assert rig.client.get(P.PROGRAM_VERSION).startswith("WITec")


def test_missing_type_library_is_reported_with_the_bridge_advice(rig):
    rig.world.fail_get_module = os_error(HRESULT.TYPE_E_LIBNOTREGISTERED, "Library not registered")
    with pytest.raises(BucsTypeLibMissing, match="32-bit"):
        rig.client.connect()
    assert not rig.client.connected and rig.world.create_args == []     # never got as far as CreateObject


def test_create_object_rpc_failure_means_witec_is_not_reachable(rig):
    rig.world.fail_create = com_error(HRESULT.RPC_S_SERVER_UNAVAILABLE, "The RPC server is unavailable.")
    with pytest.raises(BucsNotRunning):
        rig.client.connect()


def test_witec_dying_mid_session_is_connection_lost_and_reconnect_needs_the_button_again(rig):
    rig.grant()
    rig.client.set(P.ANALYZER, 9.0)
    rig.backend.witec_exit()
    with pytest.raises(BucsConnectionLost):
        rig.client.get(P.ANALYZER)
    assert not rig.client.connected
    rig.backend.witec_start()
    rig.client.connect()
    assert rig.world.get_module_args.count((P.TYPELIB_ID, 1, 0)) == 2   # a fresh connect, a fresh table
    assert rig.client.has_write_access() is False
    with pytest.raises(BucsNoWriteAccess):
        rig.client.set(P.ANALYZER, 1.0)


def test_a_bug_in_our_own_code_keeps_its_type(rig):
    rig.client.connect()
    rig.world.fail_next = [ValueError("my bug")]
    with pytest.raises(ValueError, match="my bug"):
        rig.client.get(P.PROGRAM_VERSION)


# ── timeouts ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_call_that_hangs_times_out_and_the_client_recovers(rig):
    rig.client.connect()
    ev = threading.Event()
    rig.world.hang_event = ev
    with rig.client.call_timeout(0.3):
        with pytest.raises(BucsTimeout, match="did not answer"):
            rig.client.get(P.PROGRAM_VERSION)
    ev.set()
    assert rig.client.get(P.PROGRAM_VERSION).startswith("WITec")


def test_a_call_that_never_started_is_dropped_not_executed_late(rig):
    rig.grant()
    rig.client.list_parameters()                      # table cached, so the trigger goes straight to the COM thread
    ev = threading.Event()
    rig.world.hang_event = ev
    t = threading.Thread(target=lambda: _swallow(lambda: rig.client.get(P.IS_SEQUENCER_ACTIVE)), daemon=True)
    t.start()
    for _ in range(300):                              # until the hung call is really inside the COM thread
        if rig.world.hang_event is None:
            break
        threading.Event().wait(0.01)
    n_writes = len(rig.world.writes)
    with rig.client.call_timeout(0.3):
        with pytest.raises(BucsTimeout, match="NOT executed"):
            rig.client.trigger(P.STOP_SEQUENCER)
    ev.set()
    t.join(10)
    rig.client.get(P.PROGRAM_VERSION)                 # the queue drained; the dropped trigger never ran
    assert len(rig.world.writes) == n_writes


def test_close_gives_write_access_back_stops_the_thread_and_is_idempotent(rig):
    rig.grant()
    actor = rig.client._actor                                    # noqa: SLF001
    assert rig.backend.has_write_access() if False else True
    rig.client.close()
    assert not rig.client.connected and rig.world.uninit_calls == 1
    assert rig.backend._holds_write is False                     # noqa: SLF001 - released on the way out
    assert not actor.alive
    rig.client.close()                                           # again: nothing happens
    assert rig.client.get(P.PROGRAM_VERSION).startswith("WITec")  # and it reconnects on demand
    assert rig.world.get_module_args.count((P.TYPELIB_ID, 1, 0)) == 2


# ── the generated-code cache directory ─────────────────────────────────────────────────────────────────────────

def _fake_comtypes(monkeypatch, *, gen: types.ModuleType | None):
    com = types.ModuleType("comtypes")
    com.COMError = Exception
    client = types.ModuleType("comtypes.client")
    client.gen_dir = r"C:\somewhere\read-only"
    com.client = client
    monkeypatch.setitem(sys.modules, "comtypes", com)
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    if gen is None:
        monkeypatch.delitem(sys.modules, "comtypes.gen", raising=False)
    else:
        monkeypatch.setitem(sys.modules, "comtypes.gen", gen)
    return com, client


def test_gen_cache_is_seeded_before_comtypes_picks_its_own(monkeypatch, tmp_path):
    com, client = _fake_comtypes(monkeypatch, gen=None)
    cache = prepare_comtypes_gen_cache(tmp_path / "gen")
    gen = sys.modules["comtypes.gen"]
    assert cache == tmp_path / "gen" and cache.is_dir()
    assert list(gen.__path__) == [str(cache)] and com.gen is gen
    assert client.gen_dir == str(cache)                           # and comtypes writes there


def test_gen_cache_is_redirected_when_comtypes_was_already_imported(monkeypatch, tmp_path):
    old = types.ModuleType("comtypes.gen")
    old.__path__ = [r"C:\Program Files\MAST\comtypes\gen"]
    _com, client = _fake_comtypes(monkeypatch, gen=old)
    cache = prepare_comtypes_gen_cache(tmp_path / "g2")
    assert list(old.__path__)[0] == str(cache) and r"C:\Program Files\MAST\comtypes\gen" in old.__path__
    assert client.gen_dir == str(cache)


def test_gen_cache_directory_that_cannot_be_written_is_an_error(monkeypatch, tmp_path):
    _fake_comtypes(monkeypatch, gen=None)
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    with pytest.raises(BucsError, match="not writable"):
        prepare_comtypes_gen_cache(blocker / "sub")


def test_default_gen_dir_is_under_the_data_root_and_overridable(monkeypatch, tmp_path):
    monkeypatch.delenv("MAST_COMTYPES_GEN_DIR", raising=False)
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    assert default_gen_dir() == tmp_path.resolve() / "artifacts" / "witec" / "comtypes_gen"
    monkeypatch.setenv("MAST_COMTYPES_GEN_DIR", str(tmp_path / "x"))
    assert default_gen_dir() == tmp_path / "x"


# ── a wedged COM thread must not wedge MAST ────────────────────────────────────────────────────────────────

def _hang_one_call(rig):
    """Park the COM thread inside a call; returns (release_event, thread)."""
    ev = threading.Event()
    rig.world.hang_event = ev
    t = threading.Thread(target=lambda: _swallow(lambda: rig.client.get(P.IS_SEQUENCER_ACTIVE)), daemon=True)
    t.start()
    for _ in range(300):
        if rig.world.hang_event is None:
            break
        threading.Event().wait(0.01)
    return ev, t


def _swallow(fn):
    try:
        fn()
    except BucsError:
        pass


def test_callers_fail_fast_while_the_com_thread_is_stuck(rig):
    import time

    rig.client.connect()
    rig.client.list_parameters()                      # table cached
    ev, t = _hang_one_call(rig)
    try:
        with rig.client.call_timeout(0.2):
            first_failed_after = time.monotonic()
            with pytest.raises(BucsTimeout):
                rig.client.get(P.PROGRAM_VERSION)
            first_failed_after = time.monotonic() - first_failed_after
        assert first_failed_after < 2.0
        t0 = time.monotonic()
        with rig.client.call_timeout(0.2):
            with pytest.raises(BucsTimeout, match="NOT executed"):
                rig.client.get(P.PROGRAM_VERSION)         # the thread has now been stuck longer than 0.2 s
        assert time.monotonic() - t0 < 0.5                # failed at once, did not wait another 0.2 s per caller
    finally:
        ev.set()
        t.join(10)
    assert rig.client.get(P.PROGRAM_VERSION).startswith("WITec")


def test_other_threads_do_not_queue_on_the_lock_behind_a_hung_table_load(rig):
    """A hung connect / table load holds the client lock; a stop from another thread must give up within its own
    short timeout instead of waiting for the 180 s connect timeout."""
    import time

    rig.client.connect()
    ev = threading.Event()
    rig.world.hang_event = ev                         # the table load (first COM call after this) hangs
    loader = threading.Thread(target=lambda: _swallow(rig.client.list_parameters), daemon=True)
    loader.start()
    for _ in range(300):
        if rig.world.hang_event is None:
            break
        threading.Event().wait(0.01)
    try:
        rig.client.reload_parameters()                # but the table is not cached yet, so the next call must load
        t0 = time.monotonic()
        with rig.client.call_timeout(0.3):
            with pytest.raises(BucsTimeout):
                rig.client.get(P.PROGRAM_VERSION)
        assert time.monotonic() - t0 < 3.0
    finally:
        ev.set()
        loader.join(15)


def test_close_does_not_wait_for_a_stuck_call(rig):
    import time

    rig.client.connect()
    ev, t = _hang_one_call(rig)
    t0 = time.monotonic()
    try:
        rig.client.close()
        took = time.monotonic() - t0
    finally:
        ev.set()
        t.join(15)
    assert took < 6.0                                  # bounded: the lock wait, the release attempt, the join
    assert not rig.client.connected
