"""The launcher's Remote AI section: switches, the server's life cycle, mast_service.

The server itself is tested in tests/v2/unit/integrations/test_mast_mcp_remote.py;
here: the launcher serves exactly while the switch is on and Tailscale has an
address, follows that address, does not retry a failed start on every refresh,
and lets ``mast_service`` do what the service buttons do, without their dialogs.
No Tk window is built: the methods run on a bare instance with the widgets
stubbed out.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

# ── v2 import bootstrap (canonical block; keeps mast.* resolving to MASTv2/) ──
_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

import pytest  # noqa: E402

from mast.net.tailscale import TailscaleStatus  # noqa: E402

_LAUNCHER_PATH = Path(__file__).resolve().parents[3] / "mast2_launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("mast2_launcher_remote_ai", _LAUNCHER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ready(ip: str, magic: str = "rig2.tail1234.ts.net") -> TailscaleStatus:
    return TailscaleStatus(installed=True, running=True, backend_state="Running",
                           self_ips=[ip, "fd7a:115c:a1e0::1"], magic_dns=magic,
                           device_name="DESKTOP-ABC")


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_prefs_default_off_and_survive_damage(launcher, tmp_path):
    assert launcher._load_remote_ai_prefs(tmp_path) == {"enabled": False, "computer": False}
    launcher._save_remote_ai_prefs(tmp_path, {"enabled": True, "computer": False})
    assert launcher._load_remote_ai_prefs(tmp_path) == {"enabled": True, "computer": False}
    path = launcher._remote_ai_prefs_path(tmp_path)
    path.write_text('{"enabled": "yes", "computer": true}', encoding="utf-8")
    assert launcher._load_remote_ai_prefs(tmp_path) == {"enabled": False, "computer": True}
    path.write_text("{broken", encoding="utf-8")
    assert launcher._load_remote_ai_prefs(tmp_path) == {"enabled": False, "computer": False}


def test_tailscale_address_and_machine_name(launcher):
    assert launcher._tailscale_v4(None) == ""
    assert launcher._tailscale_v4(TailscaleStatus(installed=True, backend_state="Stopped")) == ""
    assert launcher._tailscale_v4(_ready("<rig-host>")) == "<rig-host>"
    assert launcher._tailscale_machine(_ready("<rig-host>")) == "rig2"
    assert launcher._tailscale_machine(_ready("<rig-host>", magic="")) == "DESKTOP-ABC"


@pytest.mark.parametrize("kind, detail, expected", [
    ("connected", "claude-code 2.1.310", "connected (claude-code 2.1.310)"),
    ("badkey", "", "tried to connect with a wrong or old key"),
    ("mast", "mast_run StopScan", "called mast_run StopScan"),
    ("run", "Get-Process", "ran a command: Get-Process"),
    ("refused", "run_command", "tried run_command, but commands, files and screenshots are not "
                               "allowed"),
    ("guarded", "taskkill /IM Nanonis.exe /F",
     "was stopped from running a command that would end Nanonis, LabVIEW or MAST: "
     "taskkill /IM Nanonis.exe /F"),
    ("service", "restart", "restarted the main service"),
    ("service", "dance", "asked the main service to dance"),
    ("unknown", "x", "x"),
])
def test_activity_lines(launcher, kind, detail, expected):
    assert launcher._remote_ai_activity_text(kind, detail) == expected


def test_every_kind_the_server_reports_has_a_line(launcher):
    """The computer tools and the server report these kinds; each needs its own text."""
    launcher._remote_ai_module()                     # puts mast_mcp on sys.path
    from mast_mcp import computer
    kinds = set(computer.ACTIVITY_KINDS) | {"connected", "badkey", "mast"}
    assert kinds <= set(launcher._REMOTE_AI_ACTIVITY)
    for kind in kinds:
        zh, en = launcher._REMOTE_AI_ACTIVITY[kind]
        assert zh and en and zh != en


def test_the_server_package_imports_from_the_checkout(launcher):
    mod = launcher._remote_ai_module()
    assert mod.__name__ == "mast_mcp.remote_server"
    assert Path(mod.__file__).resolve().is_relative_to(
        Path(_LAUNCHER_PATH).parent / "integrations" / "claude-code" / "server")


# ── serving follows the switch and the address ───────────────────────────────
class _Srv:
    def __init__(self, host):
        self.host = host


def _bare_app(launcher, *, enabled=True, status=None):
    app = launcher.LauncherApp.__new__(launcher.LauncherApp)
    app._remote_ai_prefs = {"enabled": enabled, "computer": False}
    app._ts_status = status
    app._remote_ai = None
    app._remote_ai_error = ""
    app._remote_ai_failed_ip = ""
    app.logs, app.started, app.stopped, app.renders = [], [], [], 0
    app._log = app.logs.append

    def start(ip):
        app.started.append(ip)
        if ip == "<rig-host>":                      # an address where the bind fails
            app._remote_ai_failed_ip, app._remote_ai_error = ip, "OSError: in use"
            return False
        app._remote_ai = _Srv(ip)
        return True

    def stop(*, wait=False):
        if app._remote_ai is not None:
            app.stopped.append(app._remote_ai.host)
        app._remote_ai = None

    def render():
        app.renders += 1

    app._start_remote_ai, app._stop_remote_ai, app._render_remote_ai = start, stop, render
    return app


# NOTE: the launcher's in-process Remote AI server was retired —
# remote access is now set up as SSH over Tailscale (the "Set up AI remote access"
# button runs setup-ai-ssh.ps1). The tests that exercised _sync_remote_ai's
# serving / address-following and the enable toggle were removed with the feature.
# Removing the now-dead _sync/_start/_render/_on_* helpers, the mast_mcp in-launcher
# server modules (http_rpc / remote_server / computer*) and their mast2.spec
# hiddenimports is a possible cleanup; meanwhile the dead code and the remaining
# tests below (prefs, mast_service, activity text) stay.


def test_taking_computer_control_back_ends_running_commands(launcher, tmp_path):
    app = _bare_app(launcher, status=_ready("<rig-host>"))
    app.root_dir = tmp_path
    app._remote_ai_prefs["computer"] = True
    stopped = threading.Event()
    app._remote_ai = types.SimpleNamespace(
        host="<rig-host>", computer=types.SimpleNamespace(stop_all=stopped.set))
    app.remote_ai_computer_var = types.SimpleNamespace(get=lambda: False)
    app._on_remote_ai_computer_toggle()
    assert stopped.wait(5)
    assert launcher._load_remote_ai_prefs(tmp_path)["computer"] is False
    assert app._remote_ai_prefs["computer"] is False


# ── mast_service through the launcher ────────────────────────────────────────
class _Proc:
    def __init__(self, alive=True):
        self.alive = alive

    def poll(self):
        return None if self.alive else 0


class _Win:
    """Tk's after(0, fn) as the remote AI threads use it: run fn on 'the Tk thread'."""

    def __init__(self):
        self.ran_on = []

    def after(self, _ms, fn, *args):
        t = threading.Thread(target=lambda: (self.ran_on.append(threading.current_thread().name),
                                             fn(*args)), name="tk")
        t.start()


def _service_app(launcher, monkeypatch, *, up: bool, owned: bool):
    state = {"up": up}
    monkeypatch.setattr(launcher, "gui_running", lambda: state["up"])
    app = types.SimpleNamespace(proc=_Proc() if owned else None, win=_Win(), logs=[])
    app._log = app.logs.append

    def do_start():
        app.proc = _Proc()
        state["up"] = True

    def restart():
        app.proc = _Proc()
        state["up"] = True
        app.logs.append("restart")

    def stop():
        app.proc = None
        state["up"] = False

    app._do_start_service, app.restart_service, app.stop_service = do_start, restart, stop
    return app, state


def test_service_status(launcher, monkeypatch):
    app, state = _service_app(launcher, monkeypatch, up=False, owned=True)
    svc = launcher._RemoteService(app)
    assert svc.status() == {"running": False, "starting": True, "managed": True}
    state["up"] = True
    assert svc.status() == {"running": True, "starting": False, "managed": True}


def test_service_start_runs_on_the_tk_thread_without_the_dialog(launcher, monkeypatch):
    app, _ = _service_app(launcher, monkeypatch, up=False, owned=False)
    svc = launcher._RemoteService(app)
    assert svc.start() == "Started MAST's main service."
    assert app.win.ran_on == ["tk"] and app.proc is not None
    assert svc.start() == "MAST's main service was already running."


def test_service_restart_and_stop(launcher, monkeypatch):
    app, state = _service_app(launcher, monkeypatch, up=True, owned=True)
    svc = launcher._RemoteService(app)
    assert svc.restart() == "Restarted MAST's main service." and "restart" in app.logs
    assert svc.stop() == "Stopped MAST's main service." and state["up"] is False
    assert svc.stop() == "MAST's main service was not running."


def test_a_service_started_elsewhere_is_left_alone(launcher, monkeypatch):
    app, state = _service_app(launcher, monkeypatch, up=True, owned=False)
    svc = launcher._RemoteService(app)
    assert "outside this launcher" in svc.restart()
    assert "outside this launcher" in svc.stop()
    assert state["up"] is True and app.win.ran_on == []


def test_shutdown_stops_the_remote_ai_first():
    src = _LAUNCHER_PATH.read_text(encoding="utf-8")
    exit_now = src[src.index("def _exit_now"):]
    assert exit_now.index("self._stop_remote_ai(wait=True)") < exit_now.index("_graceful_stop_proc")
    shutdown = src[src.index("def _run_shutdown_with_progress"):src.index("def _exit_now")]
    assert "self._stop_remote_ai()" in shutdown
