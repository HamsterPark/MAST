"""The runtime under the optical station profile: no Nanonis is dialled, none of its threads
start, and every STM-profile start is exactly what it was.

Three layers, because ``CoreRuntime.setup()`` is too heavy to call inside the test process:

* the helper methods (``_build_idle_pool``, ``reconnect``, ``emergency_stop`` ...) on a
  runtime made with ``__new__``;
* the structure of ``setup()``: every Nanonis-bound start sits in the branch that runs only
  when the station has the capability it needs (``"nanonis" in caps`` ...; an AST proof that
  survives edits which a run-time test would only notice if it happened to exercise the
  branch);
* a real boot in a child process (``_boot_probe.py``) in both profiles, with the dial
  replaced by a recorder.

The generic station's boot is in ``test_station_generic.py``; the boot of both older profiles
is also pinned against the pre-capability-table tree in ``test_station_invariance.py``.
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


# ── the station's capabilities ─────────────────────────────────────────────────
def test_an_unwired_runtime_follows_the_current_profile(rt, set_profile):
    assert rt._station_has("nanonis") is True and rt._station_has("witec") is False
    set_profile("optical")
    assert rt._station_has("nanonis") is False and rt._station_has("witec") is True
    set_profile("generic")
    assert rt._station_caps() == {"optical_bench", "operator_prompts", "laser_safety", "sensor_echo"}


def test_a_booted_runtime_keeps_the_profile_it_started_under(rt, set_profile):
    rt._station_profile = "stm"
    set_profile("optical")
    assert rt._station_has("nanonis") is True, "decided once at setup(), not re-read per call"
    rt._station_profile = "optical"
    set_profile("stm")
    assert rt._station_has("nanonis") is False and rt._station_has("witec") is True


def test_a_misspelt_capability_is_an_error_on_the_runtime_too(rt):
    with pytest.raises(ValueError):
        rt._station_has("nanonsi")


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
    ``SimpleNamespace`` as ``self``. A method that says ``self._station_has(...)`` (once:
    ``self._is_optical()``) is an AttributeError there."""
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


def _cap_test(test: ast.AST) -> tuple[str, bool] | None:
    """``("nanonis", True)`` for ``"nanonis" in caps``, ``("nanonis", False)`` for
    ``"nanonis" not in caps``; ``None`` for any other test."""
    if (isinstance(test, ast.Compare) and len(test.ops) == 1
            and isinstance(test.left, ast.Constant) and isinstance(test.left.value, str)
            and isinstance(test.comparators[0], ast.Name) and test.comparators[0].id == "caps"):
        if isinstance(test.ops[0], ast.In):
            return test.left.value, True
        if isinstance(test.ops[0], ast.NotIn):
            return test.left.value, False
    return None


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


def _guarded_by(node: ast.AST, parents: dict, cap: str) -> bool:
    """``node`` sits in the branch of an ``if`` / conditional expression that runs only when the
    station has ``cap``: the body of ``"cap" in caps`` or the else of ``"cap" not in caps``.
    Stricter than "some enclosing test mentions it": the wrong branch does not count."""
    child, cur = node, parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.If, ast.IfExp)):
            got = _cap_test(cur.test)
            if got is not None and got[0] == cap:
                if isinstance(cur, ast.If):
                    in_body, in_else = child in cur.body, child in cur.orelse
                else:
                    in_body, in_else = child is cur.body, child is cur.orelse
                if (got[1] and in_body) or (not got[1] and in_else):
                    return True
        child, cur = cur, parents.get(cur)
    return False


#: every start in setup() that only some stations may make, and the capability it needs
GUARDED_STARTS = {
    "connect_all": "nanonis",                       # the four-port dial
    "start_background_refresh": "nanonis",          # 1 s state polling
    "_build_nanonis_env_sensors": "nanonis",        # tunnel_current / nanonis_signal sensors
    "_ensure_watchdog": "stm_safety",               # the tip-crash watchdog
    "_start_current_monitor": "stm_safety",         # the tunnelling-current monitor
    "_start_map_activity_watcher": "scan_map",      # hand-driven Nanonis activity -> map markers
    "_install_operator_hitl": "operator_prompts",   # the operator prompt service
}


@pytest.mark.parametrize("call, cap", sorted(GUARDED_STARTS.items()))
def test_every_station_bound_start_in_setup_sits_under_its_capability(call, cap):
    fn = _setup_fn()
    parents = _parents(fn)
    found = list(_calls_named(fn, call))
    assert found, f"setup() no longer calls {call}() - update this test with the code"
    unguarded = [n.lineno for n in found if not _guarded_by(n, parents, cap)]
    assert not unguarded, (
        f"{call}() is reached on a station without {cap!r} (setup() line(s) {unguarded}): it "
        "would dial, poll or install something on a machine that has none")


def test_the_vision_warm_up_is_skipped_without_a_tip():
    """The warm-up thread is started after a guard that raises out of its ``try`` when the
    station has no ``stm_safety`` (the scorer judges STM tip quality)."""
    fn = _setup_fn()
    parents = _parents(fn)
    starts = [n for n in _calls_named(fn, "Thread")
              if any(isinstance(k.value, ast.Constant) and k.value.value == "mast-vision-warmup"
                     for k in n.keywords)]
    assert starts, "setup() no longer starts the vision warm-up thread - update this test"
    for start in starts:
        cur = start
        while cur in parents and not isinstance(cur, ast.Try):
            cur = parents[cur]
        assert isinstance(cur, ast.Try)
        guards = [s for s in cur.body if isinstance(s, ast.If) and _cap_test(s.test) == ("stm_safety", False)
                  and any(isinstance(b, ast.Raise) for b in s.body)]
        assert guards and guards[0].lineno < start.lineno


def test_the_guard_reader_sees_the_branch():
    tree = ast.parse("def f(caps):\n    if 'nanonis' in caps:\n        a()\n    else:\n        b()\n"
                     "    if 'nanonis' not in caps:\n        c()\n    x = d() if 'nanonis' in caps else e()\n")
    fn = tree.body[0]
    parents = _parents(fn)
    calls = {n.func.id: n for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert _guarded_by(calls["a"], parents, "nanonis") and _guarded_by(calls["d"], parents, "nanonis")
    assert not _guarded_by(calls["b"], parents, "nanonis")
    assert not _guarded_by(calls["c"], parents, "nanonis")
    assert not _guarded_by(calls["e"], parents, "nanonis")
    assert not _guarded_by(calls["a"], parents, "stm_safety")


def test_setup_records_the_profile_before_it_starts_anything():
    fn = _setup_fn()
    boot = next(n.lineno for n in _calls_named(fn, "note_boot"))
    first_start = min(n.lineno for n in _calls_named(
        fn, "connect_all", "start_background_refresh", "_ensure_watchdog", "_start_current_monitor"))
    assert boot < first_start


def test_the_laser_revocation_listener_needs_laser_safety():
    """setup() asks for the listener only on a station with ``laser_safety``
    (``_install_operator_hitl(revocation="laser_safety" in caps)``), and the method installs it
    only when asked (the service itself is guarded above)."""
    fn = _setup_fn()
    installs = list(_calls_named(fn, "_install_operator_hitl"))
    assert installs
    for call in installs:
        kw = {k.arg: k.value for k in call.keywords}
        assert "revocation" in kw and _cap_test(kw["revocation"]) == ("laser_safety", True), \
            "setup() must pass revocation=\"laser_safety\" in caps"
    tree = ast.parse(_RUNTIME_SRC.read_text(encoding="utf-8"))
    meth = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_install_operator_hitl")
    listener = next(_calls_named(meth, "install_revocation_listener"))
    gates = [n for n in ast.walk(meth) if isinstance(n, ast.If)
             and isinstance(n.test, ast.UnaryOp) and isinstance(n.test.operand, ast.Name)
             and n.test.operand.id == "revocation" and any(isinstance(b, ast.Return) for b in n.body)]
    assert gates and gates[0].lineno < listener.lineno


def test_the_listener_is_installed_only_when_asked(rt, monkeypatch):
    from mast.instruments import emission_safety
    from mast.operator_hitl import service

    seen = []
    monkeypatch.setattr(service, "install", lambda *a, **k: None)
    monkeypatch.setattr(emission_safety, "install_revocation_listener", lambda store: seen.append(store))
    rt._install_operator_hitl(revocation=False)
    assert seen == []
    rt._install_operator_hitl()
    assert len(seen) == 1


# ── a real boot, in a child process ─────────────────────────────────────────────
def _no_blocked_io(stderr: str) -> None:
    """Fail when the child's ``_io_guard`` refused a connect beyond loopback or a serial open."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_io_guard", Path(__file__).with_name("_io_guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.assert_no_blocked_io(stderr)


def _boot(profile: str, root: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_HERE / "_boot_probe.py"), profile, str(root)],
        cwd=str(_REPO), capture_output=True, text=True, timeout=300,
        env={**__import__("os").environ, "PYTHONPATH": str(_REPO / "MASTv2")})
    _no_blocked_io(proc.stderr)
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
    # "autodetect_sensors" is the serial scan: on an STM it runs at boot (default on), and the probe's
    # recorder answers it with no sensors (no COM port of this machine is probed)
    assert out["calls"] == ["connect_all", "start_background_refresh", "watchdog.start",
                            "current_monitor.start_service", "autodetect_sensors"], out["calls"]
    assert out["ports"] == [6501, 6502, 6503, 6504]
    assert (out["pool"], out["state"], out["executor"]) == (
        "ConnectionPool", "InstrumentState", "SkillExecutor")
