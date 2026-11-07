"""VISTA 视觉回路的外部面：观测、动作（一动一观）、回看、读数、测量、笔记、历史。

领域逻辑全在 ``mast.vista.service``（设计契约 ``docs/v2/architecture/mast7_vista.md``）；
这里只做 HTTP：取调用方、把服务的拒绝转成外部面的错误体、在 async 里等动作作业。

* ``POST /vista/act`` 提交后最多等 ``wait_s``（≤50 s）：做完就带着观测（图 + 下一个令牌）
  回来，没做完回 ``state: running`` 和作业号，由 ``mast_wait`` 去等。
* 一动一观的拒绝一律 409；``observe_first`` 的拒绝附带最新观测与新令牌（压缩后丢了令牌
  也能自己恢复）。
* 归档工具的回答是 VISTA 的 ToolReply 原样（``text`` + ``images`` + ``is_error``），桥照搬。
"""

from __future__ import annotations

from mast.i18n import tr
import asyncio
import time
from typing import Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mast.api.ext.common import ExtError, caller_of

router = APIRouter(tags=["vista"])

ACT_WAIT_DEFAULT_S = 45.0
ACT_WAIT_MAX_S = 50.0
MEASURE_TOOLS = ("line_profile", "fft_peaks", "detect_blobs", "diff", "stats")


class MetaBody(BaseModel):
    client_meta: dict | None = Field(None, description="桥转来的客户端信息（Codex itemId 等）")


class ActBody(BaseModel):
    action: str | None = Field(None, description="VISTA 动作名（scan、set_bias、sts …）")
    params: dict | None = Field(None, description="动作参数（nm、V、pA、s）")
    skill: str | None = Field(None, description="或者：一个原始 MAST 技能名（mast_run）")
    skill_params: dict | None = Field(None, description="原始技能的参数（SI 单位）")
    note: str = Field("", max_length=1000)
    after: str | None = Field(None, description="最新观测的令牌（一动一观）")
    request_id: str | None = Field(None, max_length=120, description="幂等键")
    wait_s: float = Field(ACT_WAIT_DEFAULT_S, ge=0.0, le=ACT_WAIT_MAX_S)
    client_meta: dict | None = None


class NotesBody(BaseModel):
    content: str = Field(..., description="完整的新文件内容")
    client_meta: dict | None = None


def _svc(request: Request) -> Any:
    svc = getattr(request.app.state, "vista", None)
    if svc is None:
        raise ExtError(503, "not_wired", tr("VISTA 服务未接线", "VISTA service not wired"))
    return svc


def _ext(exc: Exception) -> ExtError:
    """``VistaError`` → ``ExtError``（同一个错误体）。"""
    return ExtError(getattr(exc, "status", 500), getattr(exc, "code", "internal"),
                    getattr(exc, "detail", str(exc)), **dict(getattr(exc, "extra", {}) or {}))


async def _call(fn, *args):
    from mast.vista.service import VistaError

    try:
        return await run_in_threadpool(fn, *args)
    except VistaError as exc:
        raise _ext(exc) from None


async def _wait_job(job: Any, wait_s: float) -> None:
    deadline = time.monotonic() + max(0.0, min(float(wait_s or 0.0), ACT_WAIT_MAX_S))
    while not job.terminal and time.monotonic() < deadline:
        await asyncio.sleep(0.2)


@router.post("/vista/hello")
async def hello(body: MetaBody, request: Request):
    """登记这个客户端会话（之后它驱动仪器的技能只能走 ``/vista/act``）并返回概况。"""
    return await _call(_svc(request).hello, caller_of(request), body.client_meta)


@router.post("/vista/observe")
async def observe(body: MetaBody, request: Request):
    """最新的观测（最新一帧的图 + 仪器读数）和一个新令牌。开始时、恢复时调用。"""
    obs = await _call(_svc(request).observe, caller_of(request), body.client_meta)
    return {"observation": obs}


@router.post("/vista/act")
async def act(body: ActBody, request: Request):
    """执行一步动作（或一个原始技能）。驱动仪器的要带 ``after``（最新观测的令牌）。"""
    caller = caller_of(request)
    svc = _svc(request)
    job, info = await _call(svc.submit_act, caller, body.model_dump())
    await _wait_job(job, body.wait_s)
    if info.get("kind") == "job":
        return {"kind": "job", "job": job.view(), "idempotent_replay": bool(info.get("replay"))}
    resp = await _call(svc.act_response, job, caller.session)
    resp["idempotent_replay"] = bool(info.get("replay"))
    return JSONResponse(status_code=200, content=resp)


@router.get("/vista/acts/{job_id}")
async def act_view(job_id: str, request: Request,
                   wait_s: float = Query(0.0, ge=0.0, le=ACT_WAIT_MAX_S)):
    """一个动作作业的当前状态；做完了就带着它的观测。"""
    svc = _svc(request)
    jm = getattr(request.app.state, "jobs", None)
    job = jm.get(job_id) if jm is not None else None
    if job is None:
        raise ExtError(404, "unknown_job", tr(f"没有作业 {job_id!r}", f"No such job {job_id!r}"))
    await _wait_job(job, wait_s)
    return await _call(svc.act_response, job, caller_of(request).session)


@router.post("/vista/inspect")
async def inspect(request: Request, args: dict[str, Any] = Body(default_factory=dict)):
    """放大镜：取档案里任意帧，裁剪、放大、换通道 / 方向 / 去背景（VISTA ``inspect``）。"""
    return await _call(_svc(request).tool, caller_of(request), "inspect", args)


@router.post("/vista/read_values")
async def read_values(request: Request, args: dict[str, Any] = Body(default_factory=dict)):
    """精确读数（VISTA ``read_values``）。"""
    return await _call(_svc(request).tool, caller_of(request), "read_values", args)


@router.post("/vista/measure/{tool}")
async def measure(tool: str, request: Request, args: dict[str, Any] = Body(default_factory=dict)):
    """量尺：``line_profile`` / ``fft_peaks`` / ``detect_blobs`` / ``diff`` / ``stats``。"""
    if tool not in MEASURE_TOOLS:
        raise ExtError(404, "unknown_tool", tr(f"没有测量工具 {tool!r}", f"No such measurement tool {tool!r}"), tools=list(MEASURE_TOOLS))
    return await _call(_svc(request).tool, caller_of(request), tool, args)


@router.get("/vista/notes/{which}")
async def notes_get(which: str, request: Request):
    """``guide``（所有实验共用的经验）或 ``working``（这个实验的工作记忆）。"""
    return await _call(_svc(request).notes_get, caller_of(request), which)


@router.put("/vista/notes/{which}")
async def notes_put(which: str, body: NotesBody, request: Request):
    """整份替换 GUIDE.md / WORKING.md。"""
    return await _call(_svc(request).notes_put, caller_of(request), which, body.content)


@router.get("/vista/history")
async def history(request: Request, view: str = Query("events"),
                  start_turn: int = Query(0, ge=0), end_turn: int | None = Query(None, ge=0),
                  limit: int = Query(64, ge=1, le=128)):
    """本实验的客观记录：``events``（按回合）或 ``attempts``（汇总）。"""
    args: dict = {"view": view, "start_turn": start_turn, "limit": limit}
    if end_turn is not None:
        args["end_turn"] = end_turn
    return await _call(_svc(request).history, caller_of(request), args)


@router.get("/vista/status")
async def status(request: Request):
    """工作区、回合、闸门状态（不含令牌本身）、客户端、监视线程。"""
    return await _call(_svc(request).status)


__all__ = ["router"]
