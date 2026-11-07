"""The runner with MAST's real MCP bridge, over real HTTP routes, and the kill-on-close job."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest
from vista_runner_kit import FAKE_CLAUDE, PY, TASK

from mast.vista.runner import paths
from mast.vista.runner.claude import TOOL_PREFIX, probe_bridge_tools

BRIDGE = paths.resolve_bridge_script({})


def _bridge_toolbox_names() -> list[str]:
    """The bridge's own tool list, imported from its files under a private package name."""
    pkg_dir = BRIDGE.parent / "mast_mcp"
    name = "_runner_test_mast_mcp"
    spec = importlib.util.spec_from_file_location(name, pkg_dir / "__init__.py",
                                                  submodule_search_locations=[str(pkg_dir)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    try:
        spec.loader.exec_module(package)
        tools = importlib.import_module(f"{name}.tools")
        config = importlib.import_module(f"{name}.config")
        return [t["name"] for t in tools.Toolbox(config.load_config({"MAST_URL": "http://127.0.0.1:9"})).list_tools()]
    finally:
        for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
            del sys.modules[key]


def test_probe_lists_exactly_the_bridge_tools():
    env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
           "MAST_URL": "http://127.0.0.1:9", "MAST_SESSION": "run-probe", "MAST_ACTOR": "runner"}
    probed = [t["name"] for t in probe_bridge_tools(PY, str(BRIDGE), env, timeout=60)]
    assert probed == _bridge_toolbox_names()
    assert "mast_write_working" in probed and "mast_act" in probed and len(set(probed)) == len(probed)


def test_probe_reports_a_broken_bridge(tmp_path):
    broken = tmp_path / "run_server.py"
    broken.write_text("import sys\nsys.stderr.write('bridge says no\\n')\nsys.exit(2)\n", encoding="utf-8")
    with pytest.raises(paths.RuntimeSetupError, match="bridge says no"):
        probe_bridge_tools(PY, str(broken), {"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}, timeout=30)
    silent = tmp_path / "silent.py"
    silent.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    t0 = time.monotonic()
    with pytest.raises(paths.RuntimeSetupError, match="did not answer initialize"):
        probe_bridge_tools(PY, str(silent), {"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}, timeout=1.5)
    assert time.monotonic() - t0 < 60                       # killed at once, not after a grace period


def test_run_with_the_real_bridge(rig):
    """Init validation against the full real tool list, hooks over HTTP (no tool call: the bridge's
    tools talk to MAST's real routes, which this test server does not have)."""
    runtime = paths.RuntimePaths(claude=(PY, "-I", "-X", "utf8", str(FAKE_CLAUDE)), bridge_python=PY,
                                 bridge_script=str(BRIDGE), hook_script=str(paths.HOOK_SCRIPT))
    view, _ = rig.run([[[{"text": "Nothing to do here. [DONE]"}]]], runtime=runtime)
    assert view["outcome"] == "done", view
    (seg,) = view["segments"]
    status = json.loads((Path(view["run_dir"]) / "status.json").read_text(encoding="utf-8"))
    tools = status["segments"][0]["init"]["tools"]
    assert sorted(tools) == sorted(TOOL_PREFIX + n for n in _bridge_toolbox_names())
    assert seg["hook_events"] == 1 and seg["problems"] == []
    mcp = json.loads((Path(view["run_dir"]) / "segment_000.mcp.json").read_text(encoding="utf-8"))["mcpServers"]["mast"]
    log = Path(mcp["env"]["MAST_MCP_LOG_FILE"])
    assert log == Path(view["run_dir"]) / "segment_000.bridge.log" and mcp["env"]["MAST_MCP_LOG"] == "INFO"
    assert log.is_file() and log.stat().st_size > 0                     # the bridge Claude Code started wrote it


def _request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json",
                                                                         "X-MAST-Actor": "operator-ui"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_start_get_stop_over_http(rig):
    rig.manager_for([[[{"tool": "mast_observe"}, {"wait_interrupt": 60}]]])
    base = f"{rig.url}/api/ext/v1"
    code, view = _request("POST", f"{base}/runner/start", {"task": TASK, "base_url": rig.url})
    assert code == 202 and view["state"] in ("starting", "running") and view["started_by"] == "ext:operator-ui"
    run_id = view["run_id"]
    code, again = _request("POST", f"{base}/runner/start", {"task": TASK, "base_url": rig.url})
    assert code == 409 and again["error"] == "run_active" and again["run_id"] == run_id
    deadline = time.monotonic() + 60
    while not rig.vista.names("mast_observe"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    code, live = _request("GET", f"{base}/runner/{run_id}")
    assert code == 200 and live["state"] == "running" and live["current_segment"]["kind"] == "initial"
    code, stopped = _request("POST", f"{base}/runner/{run_id}/stop", {"reason": "lunch"})
    assert code == 200 and stopped["stop"]["reason"] == "lunch"
    assert rig.manager.wait(run_id, 60)
    code, final = _request("GET", f"{base}/runner/{run_id}")
    assert code == 200 and final["outcome"] == "stopped_by_operator" and final["state"] == "finished"
    code, listing = _request("GET", f"{base}/runner")
    assert code == 200 and listing["runs"][0]["run_id"] == run_id and listing["runs"][0]["segments"] == 1
    code, hook = _request("POST", f"{base}/runner/{run_id}/hook", {"event": "Stop", "payload": {}})
    assert code == 403 and hook["error"] == "unknown_run_or_secret"       # no secret header


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_claude_process_tree_dies_with_mast(tmp_path):
    """A process that assigned a child to the kill-on-close job exits: the child goes with it."""
    script = tmp_path / "parent.py"
    script.write_text(textwrap.dedent(f"""
        import subprocess, sys
        sys.path.insert(0, {str(Path(paths.__file__).parents[3])!r})
        from mast.vista.runner.claude import KillOnCloseJob
        job = KillOnCloseJob()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        print(child.pid, job.assign(child), flush=True)
    """), encoding="utf-8")
    out = subprocess.run([PY, str(script)], capture_output=True, text=True, timeout=60)
    pid, assigned = out.stdout.split()
    assert assigned == "True", out.stderr
    import psutil

    def alive() -> bool:
        try:
            return psutil.Process(int(pid)).is_running()
        except psutil.NoSuchProcess:
            return False

    deadline = time.monotonic() + 15
    while alive():
        assert time.monotonic() < deadline, "the child outlived its job's last handle"
        time.sleep(0.1)
