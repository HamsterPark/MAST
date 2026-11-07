"""Runner routes (``/api/ext/v1/runner/*``, contract §4): request validation, one run at a time,
operator stop, the hook relay's guards, runs of earlier processes.

No run here starts a process: :class:`IdleRunner` "runs" until it is stopped. The runner itself is
tested against a scripted Claude Code in ``tests/v2/vista/runner/``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

_MASTV2 = Path(__file__).resolve().parents[4] / "MASTv2"
if str(_MASTV2) not in sys.path:
    sys.path.insert(0, str(_MASTV2))

from mast.api.ext import runner as runner_routes  # noqa: E402
from mast.api.ext.common import ExtError  # noqa: E402
from mast.vista.runner.claude import ClaudeRunner  # noqa: E402
from mast.vista.runner.compact_hook import HOOK_HEADER  # noqa: E402
from mast.vista.runner.manager import RunnerManager  # noqa: E402
from mast.vista.runner.paths import RuntimePaths, RuntimeSetupError  # noqa: E402

LOOPBACK = ("127.0.0.1", 50000)
RUNTIME = RuntimePaths(claude=("claude.exe",), bridge_python="python.exe", bridge_script="run_server.py",
                       hook_script="compact_hook.py")


class FakeService:
    def __init__(self) -> None:
        self.closed: dict[str, str] = {}

    def request_checkpoint(self, session, reason):
        pass

    def checkpoint_state(self, session):
        return "none"

    def clear_checkpoint(self, session):
        pass

    def recovery_observation(self, session):
        return {"text": "", "images": [], "next_token": None}

    def close_session(self, session, reason):
        self.closed.setdefault(session, reason)


class IdleRunner(ClaudeRunner):
    """Never starts Claude Code: it is 'running' until stopped (or 30 s pass)."""

    def run(self):
        with self._lock:
            self.state = "running"
        self._publish(write=True)
        self._stop.wait(30)
        outcome = "stopped_by_operator" if self._stop.is_set() else "error"
        self._close_session(f"runner run ended: {outcome}")
        with self._lock:
            self.outcome, self.state = outcome, "finished"
        self._publish(write=True)


@pytest.fixture()
def setup(tmp_path):
    svc = FakeService()
    mgr = RunnerManager(root=tmp_path / "runs", service=svc, runtime=RUNTIME, runner_cls=IdleRunner, use_job=False)
    app = FastAPI()
    app.state.ctx = SimpleNamespace()
    app.state.runner_manager = mgr

    @app.exception_handler(ExtError)
    async def _ext_error(request: Request, exc: ExtError):
        return JSONResponse(status_code=exc.status, content=jsonable_encoder(exc.body()))

    app.include_router(runner_routes.router)
    client = TestClient(app, client=LOOPBACK, headers={"X-MAST-Actor": "tester"})
    yield SimpleNamespace(app=app, mgr=mgr, svc=svc, client=client, root=tmp_path / "runs")
    mgr.shutdown(timeout_s=10)


def test_start_list_get_stop_one_run_at_a_time(setup):
    c = setup.client
    r = c.post("/runner/start", json={"task": "Find a clean Au(111) terrace.", "effort": "medium"})
    assert r.status_code == 202, r.text
    view = r.json()
    run_id = view["run_id"]
    assert view["session"] == f"run-{run_id}" and view["client"] == "claude" and view["started_by"] == "ext:tester"
    assert view["model"] == "claude-opus-5-5" and view["effort"] == "medium" and view["base_url"] == "http://127.0.0.1:7862"
    assert (setup.root / run_id / "status.json").is_file()
    busy = c.post("/runner/start", json={"task": "another"})
    assert busy.status_code == 409 and busy.json()["error"] == "run_active" and busy.json()["run_id"] == run_id
    listing = c.get("/runner").json()
    assert listing["count"] == 1 and listing["runs"][0]["run_id"] == run_id
    assert c.get(f"/runner/{run_id}").json()["state"] in ("starting", "running")
    stopped = c.post(f"/runner/{run_id}/stop", json={"reason": "operator needs the microscope"})
    assert stopped.status_code == 200 and stopped.json()["stop"]["reason"] == "operator needs the microscope"
    assert setup.svc.closed[f"run-{run_id}"] == "stopped by the operator: operator needs the microscope"
    assert setup.mgr.wait(run_id, 30)
    final = c.get(f"/runner/{run_id}").json()
    assert final["state"] == "finished" and final["outcome"] == "stopped_by_operator"
    assert c.post("/runner/start", json={"task": "next one"}).status_code == 202      # the slot is free again


def test_stop_without_reason_names_the_caller(setup):
    run_id = setup.client.post("/runner/start", json={"task": "t"}).json()["run_id"]
    assert setup.client.post(f"/runner/{run_id}/stop", json={}).json()["stop"]["reason"] == "stopped by ext:tester"


@pytest.mark.parametrize("body, status, code", [
    ({}, 422, "invalid_request"),
    ({"task": ""}, 422, "invalid_request"),
    ({"task": "t", "client": "codex"}, 422, "invalid_request"),
    ({"task": "t", "effort": "turbo"}, 422, "invalid_request"),
    ({"task": "t", "model": "two words"}, 400, "invalid_request"),
    ({"task": "t", "base_url": "http://<lan-host>:7862"}, 400, "invalid_request"),
    ({"task": "t", "base_url": "https://127.0.0.1:7862"}, 400, "invalid_request"),
    ({"task": "t", "base_url": "http://127.0.0.1"}, 400, "invalid_request"),
    ({"task": "t", "max_segments": 0}, 400, "invalid_request"),
    ({"task": "t", "turn_timeout_s": 5}, 400, "invalid_request"),
    ({"task": "t", "image_limit_per_session": 10, "image_reserve": 10}, 400, "invalid_request"),
])
def test_start_validation(setup, body, status, code):
    r = setup.client.post("/runner/start", json=body)
    assert r.status_code == status, r.text
    if status == 400:
        assert r.json()["error"] == code
    assert setup.mgr.list() == []


def test_start_accepts_limits_and_loopback_names(setup):
    r = setup.client.post("/runner/start", json={"task": "t", "base_url": "http://localhost:8000/", "max_segments": 4,
                                                 "image_limit_per_session": 100, "image_reserve": 10,
                                                 "max_budget_usd": 2.5, "preflight": False})
    assert r.status_code == 202, r.text
    runner = setup.mgr.runner(r.json()["run_id"])
    assert runner.base_url == "http://localhost:8000" and runner.cfg.max_segments == 4
    assert (runner.cfg.image_limit_per_session, runner.cfg.max_budget_usd, runner.cfg.preflight) == (100, 2.5, False)
    assert runner.hook_url == f"http://localhost:8000/api/ext/v1/runner/{runner.run_id}/hook"


def test_runner_unavailable(tmp_path):
    def missing():
        raise RuntimeSetupError("Claude Code executable not found (tried 'claude.exe' on PATH)")

    mgr = RunnerManager(root=tmp_path, service=FakeService(), runtime=missing, runner_cls=IdleRunner, use_job=False)
    app = FastAPI()
    app.state.runner_manager = mgr

    @app.exception_handler(ExtError)
    async def _ext_error(request: Request, exc: ExtError):
        return JSONResponse(status_code=exc.status, content=jsonable_encoder(exc.body()))

    app.include_router(runner_routes.router)
    r = TestClient(app, client=LOOPBACK).post("/runner/start", json={"task": "t"})
    assert r.status_code == 503 and r.json()["error"] == "runner_unavailable" and "claude.exe" in r.json()["detail"]


def test_hook_relay_guards(setup):
    run_id = setup.client.post("/runner/start", json={"task": "t"}).json()["run_id"]
    secret = setup.mgr._runs[run_id].secret
    body = {"event": "Stop", "payload": {"hook_event_name": "Stop", "session_id": "s"}}
    remote = TestClient(setup.app)                                    # client host "testclient": not loopback
    r = remote.post(f"/runner/{run_id}/hook", json=body, headers={HOOK_HEADER: secret})
    assert r.status_code == 403 and r.json()["error"] == "loopback_only"
    for rid, key in ((run_id, "wrong"), (run_id, None), ("20260101-000000-abcdef", secret)):
        headers = {HOOK_HEADER: key} if key else {}
        r = setup.client.post(f"/runner/{rid}/hook", json=body, headers=headers)
        assert r.status_code == 403 and r.json()["error"] == "unknown_run_or_secret", (rid, key)
    ok = setup.client.post(f"/runner/{run_id}/hook", json=body, headers={HOOK_HEADER: secret})
    assert ok.status_code == 200 and ok.json() == {"output": {}}      # between segments: no decision
    setup.client.post(f"/runner/{run_id}/stop", json={"reason": "x"})
    assert setup.mgr.wait(run_id, 30)
    late = setup.client.post(f"/runner/{run_id}/hook", json=body, headers={HOOK_HEADER: secret})
    assert late.status_code == 409 and late.json()["error"] == "run_finished"


def test_unknown_run(setup):
    for r in (setup.client.get("/runner/nope"), setup.client.post("/runner/nope/stop", json={}),
              setup.client.get("/runner/20260101-000000-abcdef")):
        assert r.status_code == 404 and r.json()["error"] == "unknown_run"


def test_runs_of_earlier_processes(setup):
    old = setup.root / "20261001-120000-0a0b0c"
    old.mkdir(parents=True)
    (old / "status.json").write_text(json.dumps({
        "run_id": old.name, "state": "running", "outcome": None, "task": "old task", "config": {"x": 1},
        "segments": [{"index": 0, "end_reason": None, "results": [{"subtype": "success"}], "init": {"tools": []}}],
        "current_segment": {"index": 1}}), encoding="utf-8")
    (setup.root / "not-a-run").mkdir()
    rows = setup.client.get("/runner").json()["runs"]
    assert [(r["run_id"], r["state"], r["segments"]) for r in rows] == [(old.name, "interrupted", 1)]
    view = setup.client.get(f"/runner/{old.name}").json()
    assert view["state"] == "interrupted" and "config" not in view and "results" not in view["segments"][0]
    assert setup.client.post(f"/runner/{old.name}/stop", json={}).json()["state"] == "interrupted"
