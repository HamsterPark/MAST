"""Unattended runner routes (``/api/ext/v1/runner``).

* ``POST /runner/start`` — ``{task, client: "claude", model?, effort?, base_url?, preflight?,
  ...limits}`` → 202 with the run view. 409 ``run_active`` while another run is active, 503
  ``runner_unavailable`` when Claude Code, the MCP bridge, its python or the VISTA service is
  missing. Executables are never chosen over HTTP (environment overrides only, see
  ``mast.vista.runner.paths``).
* ``GET /runner`` — recent runs (this process and earlier ones, newest first).
* ``GET /runner/{run_id}`` — one run: state, outcome, segments, usage, unmatched tool calls.
* ``POST /runner/{run_id}/stop`` — ``{reason}``: operator stop (the session's acts are refused at
  once; the run ends with ``stopped_by_operator``).
* ``POST /runner/{run_id}/hook`` — ``{event, payload}``, loopback only, header
  ``X-MAST-Runner-Secret``: the Claude Code hook relay; answers ``{"output": <hook decision>}``.

The manager is ``request.app.state.runner_manager`` when set (tests), else the process singleton.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mast.api.ext.common import ExtError, caller_of, ctx_of
from mast.vista.runner.compact_hook import HOOK_HEADER
from mast.vista.runner.manager import RunnerManager, RunnerRefused, get_runner_manager

router = APIRouter(tags=["runner"])


class RunnerStart(BaseModel):
    task: str = Field(..., min_length=1, max_length=20_000, description="what the agent should do")
    client: Literal["claude"] = "claude"
    model: str | None = Field(None, max_length=80, description="Claude model, default claude-opus-5-5")
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    base_url: str | None = Field(None, max_length=200,
                                 description="this MAST as the bridge reaches it, default http://127.0.0.1:7862")
    preflight: bool | None = Field(None, description="run the READY preflight segment first (default true)")
    max_segments: int | None = None
    turn_timeout_s: float | None = None
    stall_turns: int | None = None
    image_limit_per_session: int | None = None
    image_reserve: int | None = None
    max_budget_usd: float | None = Field(None, description="--max-budget-usd per segment")
    max_recoveries: int | None = None
    compact_window_tokens: int | None = None
    compact_pct: int | None = None


class RunnerStop(BaseModel):
    reason: str = Field("", max_length=300)


class RunnerHook(BaseModel):
    event: str | None = Field(None, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


def _manager(request: Request) -> RunnerManager:
    mgr = getattr(request.app.state, "runner_manager", None)
    return mgr if mgr is not None else get_runner_manager()


def _refusal(exc: RunnerRefused) -> ExtError:
    return ExtError(exc.status, exc.code, exc.detail, **exc.extra)


@router.post("/runner/start", status_code=202)
def start_run(body: RunnerStart, request: Request):
    """Start an unattended Claude Code run (one at a time)."""
    caller = caller_of(request)
    try:
        view = _manager(request).start(body.model_dump(exclude_none=True), ctx=ctx_of(request),
                                       started_by=caller.agent_id)
    except RunnerRefused as exc:
        raise _refusal(exc) from None
    return JSONResponse(status_code=202, content=view)


@router.get("/runner")
def list_runs(request: Request):
    runs = _manager(request).list()
    return {"count": len(runs), "runs": runs}


@router.get("/runner/{run_id}")
def get_run(run_id: str, request: Request):
    view = _manager(request).get(run_id)
    if view is None:
        raise ExtError(404, "unknown_run", f"no run {run_id!r}")
    return view


@router.post("/runner/{run_id}/stop")
def stop_run(run_id: str, body: RunnerStop, request: Request):
    """Operator stop: the session's acts are refused at once, Claude Code is interrupted."""
    reason = body.reason or f"stopped by {caller_of(request).agent_id}"
    try:
        return _manager(request).stop(run_id, reason)
    except RunnerRefused as exc:
        raise _refusal(exc) from None


@router.post("/runner/{run_id}/hook")
def runner_hook(run_id: str, body: RunnerHook, request: Request):
    """Claude Code hook relay (loopback, per-run secret): the decision for the live checkpoint."""
    client = request.client.host if request.client is not None else None
    try:
        return _manager(request).hook(run_id, secret=request.headers.get(HOOK_HEADER), event=body.event,
                                      payload=body.payload, client_host=client)
    except RunnerRefused as exc:
        raise _refusal(exc) from None


__all__ = ["RunnerHook", "RunnerStart", "RunnerStop", "router"]
