"""Test kit of the runner tests: a fake VISTA service, a real HTTP server with the runner routes,
and a rig that runs scripted Claude Code sessions (``fake_claude_cli.py`` + ``stub_bridge.py``).
The ``rig`` fixture is in ``conftest.py``.

No test here starts the real ``claude`` or talks to a model.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

_HERE = Path(__file__).resolve().parent
_MASTV2 = _HERE.parents[3] / "MASTv2"
if str(_MASTV2) not in sys.path:
    sys.path.insert(0, str(_MASTV2))

PY = sys.executable
FAKE_CLAUDE = _HERE / "fake_claude_cli.py"
STUB_BRIDGE = _HERE / "stub_bridge.py"
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
TASK = "Count the white squares and report the number."


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


class FakeVista:
    """The five ``VistaService`` methods the runner calls, and the MAST side of the stub bridge's
    tools (what ``mast.vista.service`` would do, reduced to what the runner can observe)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.states: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        self.clears: list[str] = []
        self.closed: dict[str, str] = {}
        self.recoveries: list[str] = []
        self.calls: list[dict] = []
        self.working: str | None = None
        self.acts = 0

    # service methods
    def request_checkpoint(self, session: str, reason: str) -> None:
        with self.lock:
            self.requests.append((session, reason))
            if self.states.get(session, "none") == "none":
                self.states[session] = "requested"

    def checkpoint_state(self, session: str) -> str:
        with self.lock:
            return self.states.get(session, "none")

    def clear_checkpoint(self, session: str) -> None:
        with self.lock:
            self.clears.append(session)
            self.states[session] = "none"

    def close_session(self, session: str, reason: str) -> None:
        with self.lock:
            self.closed.setdefault(session, reason)

    def recovery_observation(self, session: str) -> dict:
        with self.lock:
            self.recoveries.append(session)
            n = len(self.recoveries)
        token = f"TK{n:02d}-QQQQ"
        return {"text": json.dumps({"observe": True, "recovery": n, "next_token": token}),
                "images": [{"label": f'<visual frame="t{n:04d}.s0"/>', "mime": "image/png", "data_b64": PNG_B64}],
                "next_token": token}

    # tools behind the stub bridge
    def tool(self, session: str, actor: str, name: str, args: dict) -> dict:
        with self.lock:
            self.calls.append({"session": session, "actor": actor, "name": name, "args": dict(args)})
            state = self.states.get(session, "none")
            if name == "mast_act":
                if session in self.closed:
                    return {"content": [_text('{"error":"session_closed"}')], "isError": True}
                if state != "none":
                    return {"content": [_text('{"error":"checkpoint_requested","detail":"Update WORKING.md first. '
                                              'No action was executed."}')], "isError": True}
                self.acts += 1
                return {"content": [_text(json.dumps({"turn": self.acts, "ok": True, "next_token": "NEXT-TOKN"})),
                                    _text(f'<visual frame="t{self.acts:04d}.s0"/>'),
                                    {"type": "image", "data": PNG_B64, "mimeType": "image/png"}], "isError": False}
            if name == "mast_observe":
                return {"content": [_text('{"observe":true}'), {"type": "image", "data": PNG_B64,
                                                                "mimeType": "image/png"}], "isError": False}
            if name == "mast_write_working":
                self.working = str(args.get("content", ""))
                if state == "requested":
                    self.states[session] = "ready"
                return {"content": [_text('{"ok":true}')], "isError": False}
            if name == "mast_wait":
                return {"content": [_text('{"fired":[],"timed_out":true}')], "isError": False}
            return {"content": [_text('{"ok":true}')], "isError": False}

    def names(self, name: str) -> list[dict]:
        with self.lock:
            return [c for c in self.calls if c["name"] == name]


def build_app(get_manager, vista: FakeVista):
    """``/api/ext/v1`` with the runner routes, the ext error handler and the stub-bridge routes."""
    from mast.api.ext import runner as runner_routes
    from mast.api.ext.common import ExtError

    ext = FastAPI()
    ext.state.ctx = None

    @ext.middleware("http")
    async def _manager(request: Request, call_next):
        request.app.state.runner_manager = get_manager()
        return await call_next(request)

    @ext.exception_handler(ExtError)
    async def _ext_error(request: Request, exc: ExtError):
        return JSONResponse(status_code=exc.status, content=jsonable_encoder(exc.body()))

    ext.include_router(runner_routes.router)

    @ext.post("/_stub/tools/{name}")
    async def _stub_tool(name: str, request: Request):
        body = await request.json()
        return vista.tool(request.headers.get("x-mast-session", ""), request.headers.get("x-mast-actor", ""),
                          name, body.get("arguments") or {})

    @ext.get("/_stub/checkpoint")
    def _stub_checkpoint(session: str):
        return {"state": vista.checkpoint_state(session)}

    parent = FastAPI()
    parent.mount("/api/ext/v1", ext)
    return parent


class Rig:
    """One HTTP server per test; :meth:`run` starts a run through a fresh :class:`RunnerManager`
    (so each run may use its own configuration) and waits for it to finish."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.vista = FakeVista()
        self.scenario = tmp / "scenario.json"
        self.manager = None
        self.sleeps: list[float] = []
        self.url = ""
        self._server = None
        self._thread = None

    def start_server(self) -> None:
        import uvicorn

        app = build_app(lambda: self.manager, self.vista)
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                                     lifespan="off"))
        self._thread = threading.Thread(target=self._server.run, name="runner-test-uvicorn", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.05)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    def stop_server(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            self._thread.join(15)

    def manager_for(self, sessions: list | None = None, *, config: dict | None = None, environ: dict | None = None,
                    runtime=None):
        from mast.vista.runner.manager import RunnerManager
        from mast.vista.runner.paths import HOOK_SCRIPT, RuntimePaths

        if sessions is not None:
            self.scenario.write_text(json.dumps({"sessions": sessions}), encoding="utf-8")
        defaults = dict(effort="low", preflight=False, end_grace_s=15.0, startup_timeout_s=60.0,
                        extra_env={"FAKE_CLAUDE_SCENARIO": str(self.scenario)})
        for key, value in (config or {}).items():
            if key == "extra_env":
                defaults["extra_env"] = {**defaults["extra_env"], **value}
            else:
                defaults[key] = value
        paths = runtime or RuntimePaths(claude=(PY, "-I", "-X", "utf8", str(FAKE_CLAUDE)), bridge_python=PY,
                                        bridge_script=str(STUB_BRIDGE), hook_script=str(HOOK_SCRIPT))
        self.manager = RunnerManager(root=self.tmp / "runs", service=self.vista, runtime=paths,
                                     config_defaults=defaults, runner_options={"sleep": self.sleeps.append},
                                     environ=environ)
        return self.manager

    def start(self, sessions: list, *, config: dict | None = None, environ: dict | None = None, runtime=None,
              **request) -> tuple[str, object]:
        mgr = self.manager_for(sessions, config=config, environ=environ, runtime=runtime)
        view = mgr.start({"task": TASK, "base_url": self.url, **request}, started_by="ext:test")
        return view["run_id"], mgr.runner(view["run_id"])

    def run(self, sessions: list, *, timeout: float = 120.0, **kw) -> tuple[dict, object]:
        run_id, runner = self.start(sessions, **kw)
        assert self.manager.wait(run_id, timeout), "the run did not finish in time"
        return self.manager.get(run_id), runner

    def proc(self, k: int) -> dict:
        return json.loads(Path(f"{self.scenario}.proc{k}.json").read_text(encoding="utf-8"))

    def processes(self) -> int:
        counter = Path(f"{self.scenario}.count")
        return int(counter.read_text()) if counter.exists() else 0
