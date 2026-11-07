"""The runtime under the optical station profile: no Nanonis is dialled, none of its threads
start, and every STM-profile start is exactly what it was.

Three layers, because ``CoreRuntime.setup()`` is too heavy to call inside the test process:

* the helper methods (``_build_idle_pool``, ``reconnect``, ``emergency_stop`` ...) on a
  runtime made with ``__new__``;
* the structure of ``setup()``: every Nanonis-bound start sits under an ``optical`` test
  (an AST proof that survives edits which a run-time test would only notice if it
  happened to exercise the branch);
* a real boot in a child process (``_boot_probe.py``) in both profiles, with the dial
  replaced by a recorder.
"""
from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import mast.instruments.registry as inst_registry
import mast.satellite as sat_pkg
from mast.config import MASTConfig
from mast.core.runtime import CoreRuntime

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
_RUNTIME_SRC = _REPO / "MASTv2" / "mast" / "core" / "runtime.py"


@pytest.fixture
def rt():
    r = CoreRuntime.__new__(CoreRuntime)
    r.config = MASTConfig()
    r._orch_abort = threading.Event()
    r._orch_run_aborts_lock = threading.Lock()
    r._orch_run_aborts = {}
    r._executor = None
    r._orch_interrupts = None
    r._pool = None
    r._shutdown_hooks = []
    return r


class RecordingPool:
    """A pool that records every command and refuses all of them."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._config = types.SimpleNamespace(port_emergency=6504)

    def urgent_call(self, verb, *a, **k):
        self.calls.append((verb, k.get("role")))
        raise ConnectionError("no connection")

    safe_call = urgent_call


class QuietHub:
    def emergency_stop_all(self, reason=""):
        return {"sent": [], "unreachable": [], "errors": []}


class QuietRegistry:
    def stop_all(self):
        pass


# ── _is_optical ────────────────────────────────────────────────────────────────
def test_an_unwired_runtime_follows_the_current_profile(rt, set_profile):
    assert rt._is_optical() is False
    set_profile("optical")
    assert rt._is_optical() is True


def test_a_booted_runtime_keeps_the_profile_it_started_under(rt, set_profile):
    rt._station_profile = "stm"
    set_profile("optical")
    assert rt._is_optical() is False, "decided once at setup(), not re-read per call"
    rt._station_profile = "optical"
    set_profile("stm")
    assert rt._is_optical() is True


# ── the idle pool ───────────────────────────────────────────────────────────────
def test_the_optical_pool_exists_but_never_dials(rt, monkeypatch):
    import mast.core.connection as conn

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("the optical station dialled a socket")

    monkeypatch.setattr(conn.socket, "socket", Boom)
    monkeypatch.setattr(conn.ConnectionPool, "connect_all",
                        lambda self: pytest.fail("connect_all() was called"))
    pool = rt._build_idle_pool()
    assert isinstance(pool, conn.ConnectionPool)
    assert rt.config.nanonis.all_ports == [0, 0, 0, 0]
    for role in ("main", "monitor", "data", "emergency"):
        with pytest.raises(ConnectionError):
            pool.get(role)
    # a stray Nanonis call fails cleanly and immediately (an error record, as with any dead
    # connection): no reconnect attempt reaches a socket, so Boom above never fires
    t0 = time.monotonic()
    rec = pool.safe_call("Util_VersionGet", role="main")
    assert getattr(rec, "error", None), "the call must not look successful"
    assert time.monotonic() - t0 < 1.0


def test_reconnect_does_nothing_on_an_optical_station(rt, set_profile, monkeypatch, caplog):
    import mast.core.connection as conn

    set_profile("optical")
    monkeypatch.setattr(conn, "ConnectionPool",
                        lambda *a, **k: pytest.fail("reconnect() built a ConnectionPool"))
    with caplog.at_level(logging.INFO, logger="mast.core.runtime"):
        assert rt.reconnect() == 0
    assert any("optical" in r.getMessage() for r in caplog.records)


# ── emergency stop ──────────────────────────────────────────────────────────────
@pytest.fixture
def quiet_optics(monkeypatch):
    monkeypatch.setattr(sat_pkg, "get_satellite_hub", lambda: QuietHub())
    monkeypatch.setattr(inst_registry, "get_instrument_registry", lambda: QuietRegistry())


def test_the_optical_estop_sends_nothing_to_nanonis_and_reports_no_false_errors(
        rt, set_profile, quiet_optics):
    set_profile("optical")
    rt._pool = RecordingPool()
    res = rt.emergency_stop("operator")
    assert rt._pool.calls == []
    assert res["aborted"] is True and res["errors"] == []
    assert res["nanonis"].startswith("not used")
    assert res["optics"]["local_optics"] == "dispatched"


def test_the_stm_estop_is_unchanged(rt, quiet_optics):
    rt._pool = RecordingPool()
    res = rt.emergency_stop("operator")
    verbs = [v for v, _ in rt._pool.calls]
    assert verbs[:3] == ["AutoApproach_OnOffSet", "Motor_StopMove", "Scan_Action"]
    assert "ZCtrl_Withdraw" in verbs
    assert "nanonis" not in res and res["errors"], "the failures of a dead pool are still reported"


# ── methods that tests call with a stand-in for self ────────────────────────────
def test_methods_called_with_a_stand_in_for_self_do_not_need_the_helper_methods(set_profile):
    """``test_session_path_non_ascii`` calls ``CoreRuntime._resolve_session_dir`` with a
    ``SimpleNamespace`` as ``self``. A method that says ``self._is_optical()`` is an
    AttributeError there (it was, once)."""
    stub = types.SimpleNamespace(_pool=RecordingPool(), _session_dir_cache=None)
    CoreRuntime._resolve_session_dir(stub)
    assert [v for v, _ in stub._pool.calls] == ["Util_SessionPathGet"]
    set_profile("optical")
    stub = types.SimpleNamespace(_pool=RecordingPool(), _session_dir_cache=None)
    assert CoreRuntime._resolve_session_dir(stub) is None and stub._pool.calls == []
    # the profile a booted runtime started under wins over the process profile
    stub = types.SimpleNamespace(_pool=RecordingPool(), _session_dir_cache=None,
                                 _station_profile="stm")
    CoreRuntime._resolve_session_dir(stub)
    assert [v for v, _ in stub._pool.calls] == ["Util_SessionPathGet"]


# ── the Nanonis session folder ──────────────────────────────────────────────────
def test_the_session_dir_is_not_asked_of_nanonis_on_an_optical_station(rt, set_profile):
    set_profile("optical")
    rt._pool = RecordingPool()
    assert rt._resolve_session_dir() is None
    assert rt._pool.calls == []


def test_the_session_dir_is_still_asked_of_nanonis_on_an_stm_station(rt):
    rt._pool = RecordingPool()
    rt._resolve_session_dir()
    assert [v for v, _ in rt._pool.calls] == ["Util_SessionPathGet"]


# ── operator prompt service ─────────────────────────────────────────────────────
def test_a_build_without_the_operator_service_boots_with_one_line(rt, caplog, monkeypatch):
    monkeypatch.delitem(sys.modules, "mast.operator_hitl.service", raising=False)
    import importlib
    real = importlib.import_module

    def absent(name, *a, **k):
        if name == "mast.operator_hitl.service":
            raise ModuleNotFoundError("no module", name=name)
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", absent)
    with caplog.at_level(logging.INFO, logger="mast.core.runtime"):
        rt._install_operator_hitl()
    msgs = [r for r in caplog.records if "operator_hitl" in r.getMessage()]
    assert len(msgs) == 1 and msgs[0].levelno == logging.INFO


def test_the_operator_service_is_installed_when_present(rt, monkeypatch):
    seen = []
    fake = types.ModuleType("mast.operator_hitl.service")
    fake.install = lambda: seen.append("installed")
    monkeypatch.setitem(sys.modules, "mast.operator_hitl.service", fake)
    rt._install_operator_hitl()
    assert seen == ["installed"]


def test_a_failing_operator_service_warns_and_does_not_stop_the_boot(rt, caplog, monkeypatch):
    fake = types.ModuleType("mast.operator_hitl.service")

    def boom():
        raise RuntimeError("popup host unavailable")

    fake.install = boom
    monkeypatch.setitem(sys.modules, "mast.operator_hitl.service", fake)
    with caplog.at_level(logging.WARNING, logger="mast.core.runtime"):
        rt._install_operator_hitl()
    assert any("install failed" in r.getMessage() and "popup host" in r.getMessage()
               for r in caplog.records)


def test_a_broken_operator_service_is_loud_not_mistaken_for_an_absent_one(rt, caplog, monkeypatch):
    import importlib
    real = importlib.import_module

    def broken(name, *a, **k):
        if name == "mast.operator_hitl.service":
            raise ModuleNotFoundError("No module named 'winsound_helper'", name="winsound_helper")
        return real(name, *a, **k)

    monkeypatch.delitem(sys.modules, "mast.operator_hitl.service", raising=False)
    monkeypatch.setattr(importlib, "import_module", broken)
    with caplog.at_level(logging.INFO, logger="mast.core.runtime"):
        rt._install_operator_hitl()
    rec = [r for r in caplog.records if "operator_hitl" in r.getMessage()]
    assert rec and rec[0].levelno == logging.WARNING and "winsound_helper" in rec[0].getMessage()


# ── the structure of setup() ────────────────────────────────────────────────────
def _setup_fn() -> ast.FunctionDef:
    tree = ast.parse(_RUNTIME_SRC.read_text(encoding="utf-8"))
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "CoreRuntime":
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "setup":
                    return fn
    pytest.fail("CoreRuntime.setup() not found")


def _mentions_optical(test: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "optical" for n in ast.walk(test))


def _calls_named(fn: ast.AST, *names: str):
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if nm in names:
                yield n


def _parents(fn: ast.AST) -> dict:
    out = {}
    for parent in ast.walk(fn):
        for child in ast.iter_child_nodes(parent):
            out[child] = parent
    return out


def _guarded_by_optical(node: ast.AST, parents: dict) -> bool:
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.If, ast.IfExp)) and _mentions_optical(cur.test):
            return True
    return False


@pytest.mark.parametrize("call", [
    "connect_all",                    # the four-port dial
    "start_background_refresh",       # 1 s state polling
    "_ensure_watchdog",               # the tip-crash watchdog
    "_start_current_monitor",         # the tunnelling-current monitor
    "_start_map_activity_watcher",    # hand-driven Nanonis activity -> map markers
    "_build_nanonis_env_sensors",     # tunnel_current / nanonis_signal sensors
])
def test_every_nanonis_bound_start_in_setup_sits_under_the_optical_test(call):
    fn = _setup_fn()
    parents = _parents(fn)
    found = list(_calls_named(fn, call))
    assert found, f"setup() no longer calls {call}() - update this test with the code"
    unguarded = [n.lineno for n in found if not _guarded_by_optical(n, parents)]
    assert not unguarded, (
        f"{call}() is reached on an optical station (setup() line(s) {unguarded}): it would dial "
        "or poll Nanonis on a machine that has none")


def test_setup_records_the_profile_before_it_starts_anything():
    fn = _setup_fn()
    boot = next(n.lineno for n in _calls_named(fn, "note_boot"))
    first_start = min(n.lineno for n in _calls_named(
        fn, "connect_all", "start_background_refresh", "_ensure_watchdog", "_start_current_monitor"))
    assert boot < first_start


def test_the_operator_service_is_installed_only_on_an_optical_station():
    fn = _setup_fn()
    parents = _parents(fn)
    installs = list(_calls_named(fn, "_install_operator_hitl"))
    assert installs and all(_guarded_by_optical(n, parents) for n in installs)


# ── a real boot, in a child process ─────────────────────────────────────────────
def _boot(profile: str, root: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_HERE / "_boot_probe.py"), profile, str(root)],
        cwd=str(_REPO), capture_output=True, text=True, timeout=300,
        env={**__import__("os").environ, "PYTHONPATH": str(_REPO / "MASTv2")})
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE_JSON ")]
    assert lines, f"the probe printed no result (rc {proc.returncode}):\n{proc.stdout[-1500:]}\n{proc.stderr[-2500:]}"
    return json.loads(lines[-1][len("PROBE_JSON "):])


def test_an_optical_boot_dials_nothing_and_starts_none_of_the_nanonis_threads(tmp_path):
    out = _boot("optical", tmp_path / "root")
    assert out["profile"] == "optical" and out["station"]["boot_profile"] == "optical"
    assert out["calls"] == [], f"started Nanonis-bound things on an optical station: {out['calls']}"
    assert out["ports"] == [0, 0, 0, 0]
    # ExecutionContext needs all three (api/direct_exec.py build_context)
    assert (out["pool"], out["state"], out["executor"]) == (
        "ConnectionPool", "InstrumentState", "SkillExecutor")
    assert "InstrumentStateRefresh" not in out["threads"]
    assert "CurrentMonitorService" not in out["threads"]
    assert "shutdown_error" not in out


def test_an_stm_boot_still_starts_everything_it_always_started(tmp_path):
    out = _boot("stm", tmp_path / "root")
    assert out["profile"] == "stm"
    assert out["calls"] == ["connect_all", "start_background_refresh", "watchdog.start",
                            "current_monitor.start_service"], out["calls"]
    assert out["ports"] == [6501, 6502, 6503, 6504]
    assert (out["pool"], out["state"], out["executor"]) == (
        "ConnectionPool", "InstrumentState", "SkillExecutor")
