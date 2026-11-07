"""Optical-bench endpoints for the 光学台 tab.

Thin relays over the optics skills (``mast/skills/builtins/optics_*``), which
drive the process-global instrument registry
(``mast.instruments.get_instrument_registry``). No hardware logic lives here.

Two kinds of call, two paths:

* **Reads** (devices / position / delay) stay light, take no token, change
  nothing — and **never open a device**: the inventory is config
  only, and position / delay are read only from a device this process already
  has open (``connected: false`` otherwise). These GETs run on every page load,
  focus and refresh; opening a port there took devices from their vendor
  software and took back devices the operator had just released.
* **Writes** (move / wiggle / home / delay move / dry-run) go through
  :mod:`mast.api.direct_exec` — the same ``ExecutionContext.run`` path as
  ``POST /api/skills/{name}/execute``: parameter validation, the safety gates,
  the instrument token (a write is refused while another entry point drives the
  instrument), the abort latch, and a row in the experiment record. Calling
  ``execute(None, params)`` directly would skip all of it.
* **Stop** must always work, so it is the one exception: through the same path
  when nothing blocks it (it is token-free, see ``instrument_lock.BYPASS_NAMES``),
  and — because ``ExecutionContext.run`` refuses every skill once an abort is
  latched — by calling the stop skill directly when an abort is set or there is
  no live runtime. A stop never starts anything.

House style (mirrors routes/qa.py, routes/signals.py):
  - ``def`` handlers (FastAPI runs them in a threadpool) so a blocking serial
    move never stalls the event loop;
  - GRACEFUL DEGRADATION is mandatory: no bench / no hardware / no runtime
    returns an empty inventory or an ``ok=false`` envelope — never a 500. Soft
    travel limits are enforced in the driver (Layer-0), so a bad target comes
    back as ``ok=false`` too.
"""

from __future__ import annotations

import logging
from mast.i18n import tr
from typing import Any

from fastapi import APIRouter, Request

from mast.api.schemas_optics import (
    DelayLineInfo,
    DelayMoveRequest,
    DryRunRequest,
    HomeRequest,
    MoveRequest,
    OpticsActionResponse,
    OpticsAxis,
    OpticsDevice,
    OpticsDevicesResponse,
    StopRequest,
    WiggleRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["optics"])

#: This entry point's name in instrument-token refusals ("who is driving").
OWNER = "光学台面板"


def _envelope(run) -> OpticsActionResponse:
    from mast.api import direct_exec

    return OpticsActionResponse(
        ok=bool(run.success),
        error=(run.error or None),
        summary=(run.summary or None),
        data=dict(direct_exec.jsonable(run.data) or {}),
    )


def _run_write(request: Request, skill_name: str, params: dict) -> OpticsActionResponse:
    """WRITE path: the skill runs through ``ExecutionContext.run`` (token, abort,
    safety gates, validation) and is recorded like any direct skill call."""
    try:
        from mast.api import direct_exec

        ctx = getattr(request.app.state, "ctx", None)
        ec, missing = direct_exec.build_context(ctx, owner=OWNER)
        if ec is None:
            return OpticsActionResponse(
                ok=False,
                error=(tr(f"ExecutionContext 不可用（缺少: {missing}）—— 光学台的写操作必须经过"
                       "令牌/中止/安全检查，不再直接驱动设备", f"ExecutionContext unavailable (missing: {missing}). Optics-bench writes must go through token/abort/safety checks and no longer drive the device directly")),
                data={"degraded": True, "missing": list(missing)})
        run = direct_exec.run_and_record(
            ec, skill_name, params, runtime=direct_exec.live_runtime(ctx),
            agent_id="optics_panel", thread_id=None, context=OWNER,
            approval_source=direct_exec.APPROVAL_AUTO)
        return _envelope(run)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the API
        logger.warning("optics write %s failed: %s", skill_name, exc)
        return OpticsActionResponse(ok=False, error=f"{type(exc).__name__}: {exc}")


def _run_stop(request: Request, params: dict) -> OpticsActionResponse:
    """STOP path — see the module docstring. Always reaches the hardware."""
    from mast.skills.builtins.optics_stage import StopOpticalStage

    try:
        from mast.api import direct_exec

        ctx = getattr(request.app.state, "ctx", None)
        ec, _missing = direct_exec.build_context(ctx, owner=OWNER)
    except Exception as exc:  # noqa: BLE001
        logger.debug("optics stop: no ExecutionContext (%s)", exc)
        ec = None
    aborted = False
    if ec is not None:
        try:
            aborted = bool(ec.check_abort())
        except Exception:  # noqa: BLE001
            aborted = False
    if ec is not None and not aborted:
        resp = _run_write(request, "StopOpticalStage", params)
        if resp.ok:
            return resp
        logger.warning("optics stop via ExecutionContext failed (%s) — stopping directly",
                       resp.error)
    # Direct path: no runtime, an abort latched, or the gated path refused.
    import time as _time

    t0 = _time.monotonic()
    try:
        res = StopOpticalStage().execute(ec, params)
    except Exception as exc:  # noqa: BLE001
        logger.error("optics stop failed: %s", exc)
        return OpticsActionResponse(ok=False, error=f"{type(exc).__name__}: {exc}")
    _record_direct_stop(request, params, res, _time.monotonic() - t0, aborted)
    data = dict(getattr(res, "data", {}) or {})
    data["path"] = "direct" + ("(abort latched)" if aborted else "")
    return OpticsActionResponse(
        ok=bool(getattr(res, "success", False)),
        error=(getattr(res, "error", "") or None),
        summary=getattr(res, "summary", None), data=data)


def _record_direct_stop(request: Request, params: dict, res: Any, elapsed_s: float,
                        aborted: bool) -> None:
    """The direct stop is recorded like any direct skill call.
    Never raises: a record that failed must not turn a stop into an error."""
    try:
        from mast.api import direct_exec
        from mast.skills.builtins.optics_stage import StopOpticalStage

        runtime = direct_exec.live_runtime(getattr(request.app.state, "ctx", None))
        rec = getattr(runtime, "_record_direct_skill_call", None)
        if not callable(rec):
            return
        rec(direct_exec.record_payload(
            "StopOpticalStage", params, StopOpticalStage().metadata(), res,
            duration_ms=int(elapsed_s * 1000), agent_id="optics_panel",
            context=OWNER + ("（中止后直接停止）" if aborted else "（直接停止）"),
            approval_source=direct_exec.APPROVAL_AUTO))
    except Exception as exc:  # noqa: BLE001
        logger.warning("optics stop not recorded: %s", exc)


# ── GET /api/optics/devices ───────────────────────────────────────────────
@router.get("/optics/devices", response_model=OpticsDevicesResponse)
def list_devices(request: Request) -> OpticsDevicesResponse:
    """Optical-bench inventory + the pump-probe delay-line binding.

    Never touches hardware beyond a config read; empty inventory when no bench
    is configured (``degraded`` stays False — an empty bench is a valid state,
    not a failure)."""
    try:
        from mast.skills.builtins.optics_stage import ListOpticalDevices

        res = ListOpticalDevices().execute(None, {})
        if not getattr(res, "success", False):
            return OpticsDevicesResponse(
                degraded=True, detail=getattr(res, "error", None) or "unavailable"
            )
        data = res.data or {}
        devices = [
            OpticsDevice(
                id=str(d.get("id", "")),
                name=str(d.get("name", d.get("id", ""))),
                type=str(d.get("type", "?")),
                kind=str(d.get("kind") or "motion"),
                role=d.get("role"),
                satellite=d.get("satellite"),
                remote_device=d.get("remote_device"),
                enabled=bool(d.get("enabled", True)),
                connected=bool(d.get("connected", False)),
                axes=[
                    OpticsAxis(
                        name=str(a.get("name", "")),
                        unit=str(a.get("unit", "um")),
                        min_pos=a.get("min_pos"),
                        max_pos=a.get("max_pos"),
                        role=a.get("role"),
                    )
                    for a in (d.get("axes") or [])
                ],
            )
            for d in (data.get("devices") or [])
        ]
        dl_raw = data.get("delay_line")
        delay_line = None
        if dl_raw:
            delay_line = DelayLineInfo(
                device_id=str(dl_raw.get("device_id", "")),
                axis=str(dl_raw.get("axis", "")),
                ps_per_mm=float(dl_raw.get("ps_per_mm", 0.0) or 0.0),
                zero_offset_mm=float(dl_raw.get("zero_offset_mm", 0.0) or 0.0),
                delay_range_ps=[float(x) for x in (dl_raw.get("delay_range_ps") or [])],
            )
        return OpticsDevicesResponse(devices=devices, delay_line=delay_line)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash
        logger.warning("optics devices relay failed: %s", exc)
        return OpticsDevicesResponse(degraded=True, detail=f"{type(exc).__name__}: {exc}")


# ── reads ─────────────────────────────────────────────────────────────────
#: what a GET says about a device MAST has not opened (it never opens one)
_NOT_OPEN = tr("未连接：读数不会去打开设备（连接、或执行一次移动之后才有位置）", "Not connected: reading does not open the device (a position is only available after connecting or performing a move)")


@router.get("/optics/position", response_model=OpticsActionResponse)
def get_position(request: Request, device_id: str, axis: str) -> OpticsActionResponse:
    """Read one optical stage axis position — only if MAST already has the device
    open (``connected: false`` otherwise). A GET never opens hardware: pages fetch
    it on load, on window focus and on every refresh, and opening the port would
    take the device from its vendor software, or take back a device the operator
    just handed back with 断开 (DisconnectOpticalDevice). OpticalStageGetPos
    must not be used here: its registry lookup connects."""
    from mast.instruments.registry import get_instrument_registry

    try:
        reg = get_instrument_registry()
        ax = reg.open_axis(device_id, axis)
        if ax is None:
            if reg.device_config(device_id) is None:
                return OpticsActionResponse(ok=False,
                                            error=f"no optical device with id {device_id!r}")
            return OpticsActionResponse(
                ok=True, summary=tr(f"{device_id}：{_NOT_OPEN}", f"{device_id}: {_NOT_OPEN}"),
                data={"device_id": device_id, "axis": axis, "connected": False,
                      "position": None})
        return OpticsActionResponse(ok=True, data={
            "device_id": device_id, "axis": axis, "connected": True,
            "position": ax.get_position(), "unit": ax.config.unit,
            "limits": [ax.config.min_pos, ax.config.max_pos]})
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the API
        logger.warning("optics position read failed: %s", exc)
        return OpticsActionResponse(ok=False, error=f"{type(exc).__name__}: {exc}")


@router.get("/optics/delay", response_model=OpticsActionResponse)
def get_delay(request: Request) -> OpticsActionResponse:
    """Read the current pump-probe optical delay (ps) + reachable range — the
    delay only if the delay stage is already open; the range is config (see
    :func:`get_position` for why a GET never opens hardware)."""
    from mast.instruments.registry import get_instrument_registry

    try:
        reg = get_instrument_registry()
        info = reg.delay_line_info()
        if info is None:
            return OpticsActionResponse(
                ok=False, error="no delay_line section in config/optical_instruments.json")
        dl = reg.open_delay_line()
        if dl is None:
            return OpticsActionResponse(
                ok=True, summary=tr(f"延迟台 {info['device_id']}：{_NOT_OPEN}", f"Delay stage {info['device_id']}: {_NOT_OPEN}"),
                data={**info, "connected": False, "delay_ps": None})
        delay = dl.get_delay_ps()
        lo, hi = info["delay_range_ps"]
        return OpticsActionResponse(
            ok=True, summary=f"delay = {delay:.3f} ps (range {lo:.1f} … {hi:.1f} ps)",
            data={**info, "connected": True, "delay_ps": delay,
                  "stage_position": dl.axis.get_position(),
                  "stage_unit": dl.axis.config.unit})
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the API
        logger.warning("optics delay read failed: %s", exc)
        return OpticsActionResponse(ok=False, error=f"{type(exc).__name__}: {exc}")


# ── writes (ExecutionContext path) ────────────────────────────────────────
@router.post("/optics/move", response_model=OpticsActionResponse)
def move(request: Request, body: MoveRequest) -> OpticsActionResponse:
    """Move an optical stage axis (absolute, or relative delta). Soft travel
    limits are enforced in the driver — an out-of-range target is ``ok=false``."""
    return _run_write(request, "OpticalStageMove", {
        "device_id": body.device_id, "axis": body.axis,
        "position": body.position, "relative": body.relative, "wait": True,
    })


@router.post("/optics/wiggle", response_model=OpticsActionResponse)
def wiggle(request: Request, body: WiggleRequest) -> OpticsActionResponse:
    """Bring-up self-test: nudge an axis and report moved / direction / scale."""
    return _run_write(request, "OpticalStageWiggle", {
        "device_id": body.device_id, "axis": body.axis, "delta": body.delta,
    })


@router.post("/optics/home", response_model=OpticsActionResponse)
def home(request: Request, body: HomeRequest) -> OpticsActionResponse:
    """Find-zero / reference an axis (sweeps the travel — confirm no measurement
    depends on the current position)."""
    return _run_write(request, "HomeOpticalStage",
                      {"device_id": body.device_id, "axis": body.axis})


@router.post("/optics/stop", response_model=OpticsActionResponse)
def stop(request: Request, body: StopRequest) -> OpticsActionResponse:
    """Panic-stop one device, or the whole bench when device_id is omitted."""
    params: dict[str, Any] = {}
    if body.device_id:
        params["device_id"] = body.device_id
    return _run_stop(request, params)


@router.post("/optics/delay/move", response_model=OpticsActionResponse)
def delay_move(request: Request, body: DelayMoveRequest) -> OpticsActionResponse:
    """Move the pump-probe delay line to an optical delay (ps)."""
    return _run_write(request, "DelayLineMoveTo", {"delay_ps": body.delay_ps, "wait": True})


@router.post("/optics/dry-run", response_model=OpticsActionResponse)
def dry_run(request: Request, body: DryRunRequest) -> OpticsActionResponse:
    """Test-drive the delay line through a full pump-probe sweep — steps every
    point and settles, but fires no laser trigger, reads no signal and saves no
    file. Verifies the whole range is reachable before a real measurement. It
    MOVES the stage, so it takes the same write path as a move."""
    return _run_write(request, "PumpProbeScan", {
        "delay_start_ps": body.delay_start_ps,
        "delay_stop_ps": body.delay_stop_ps,
        "points": body.points,
        "dry_run": True,
    })
