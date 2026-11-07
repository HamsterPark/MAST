"""``POST /wait``：在服务端等到外部 agent 关心的事发生（设计稿 ``docs/v2/architecture/mast7_vista.md`` §6）。

条件、游标、告警分级、操作员答复的探测都在 :mod:`mast.vista.wait`；这里只做三件事：
取活的 VISTA 服务、等它的 ``WaitHub``、在结束的 act 作业或新帧触发时附上观察（观察里带
新的观察令牌，由服务的 ``observation_after_wait`` 决定给不给）。

* 等待是 ``async`` 的，不占线程池；``observation_after_wait`` 可能读盘、渲染图，放进线程里跑。
* 客户端断开就提前结束，**不**附观察 —— 给一个已经不在听的客户端铸令牌，只会让它手里
  那张作废。
* 观察取失败不吞掉已触发的条件：回答照常带 ``fired`` 与 ``cursor``，外加 ``observation_error``。
"""

from __future__ import annotations

from mast.i18n import tr
import asyncio
import inspect
import logging
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from mast.api.ext.common import ExtError, caller_of, ctx_of
from mast.vista.wait import (
    DEFAULT_WAIT_S,
    MAX_CONDITIONS,
    MAX_WAIT_S,
    ConditionError,
    CursorError,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vista"])


class WaitBody(BaseModel):
    conditions: list[dict[str, Any]] = Field(
        ..., min_length=1, max_length=MAX_CONDITIONS,
        description=("任一触发即返回：job{job_id} / frame / alert{min_level: info|warning|critical} / "
                     "operator_reply{request_id?} / detector{detector_id?} / timer{seconds}"))
    timeout_s: float = Field(DEFAULT_WAIT_S, ge=0.0, le=MAX_WAIT_S,
                             description="最长等待（秒）；服务端上限 840，客户端的工具超时设到 ≥ 900")
    since: str | None = Field(None, max_length=128,
                              description="上一次回答里的 cursor：两次等待之间发生的事不会漏")


def vista_service(request: Request) -> Any:
    """活的 VISTA 服务：``create_ext_app`` 绑在 ``app.state.vista`` 上的那个，没有就问
    ``get_service(ctx)``。没接线（冷启动、独立开发模式）→ 503 ``not_wired`` 并点名缺什么 ——
    这是降级答复，不是 500。"""
    svc = getattr(request.app.state, "vista", None)
    if svc is not None:
        return svc
    why = tr("get_service 返回了空", "get_service returned nothing")
    try:
        from mast.vista.service import get_service

        svc = get_service(ctx_of(request))
    except Exception as exc:  # noqa: BLE001 — 取不到就是没接线；原因写进 detail
        logger.warning("ext-gateway: VISTA 服务取不到(%s)", exc)
        svc, why = None, f"{type(exc).__name__}: {exc}"
    if svc is None:
        raise ExtError(503, "not_wired", tr(f"VISTA 服务未接线（{why}）", f"VISTA service not wired ({why})"), missing=["vista"])
    return svc


async def _observation_after(svc: Any, session: str, fired: list[dict]) -> Any:
    fn = getattr(svc, "observation_after_wait", None)
    if not callable(fn):
        return None
    res = await asyncio.to_thread(fn, session, fired)
    if inspect.isawaitable(res):
        res = await res
    return res


@router.post("/wait")
async def wait(body: WaitBody, request: Request):
    """等到任一条件触发或超时。回答 ``{fired:[{index,type,event,age_s?,backlog?}], timed_out,
    waited_s, cursor, observation?}``：把 ``cursor`` 作为下一次的 ``since`` 传回来就不会漏事件；
    ``backlog: true`` 表示这件事发生在这次等待开始之前（游标之后积压的），``age_s`` 是它距现在
    多少秒 —— 一条立刻返回的告警可能是几分钟前的事；``fired`` 里 ``type: gap`` 表示游标之后有
    事件已被挤出缓冲，请重新 observe。结束的 act 作业或新帧触发时附 ``observation``
    （带新的观察令牌）。"""
    svc = vista_service(request)
    hub = getattr(svc, "hub", None)
    if hub is None:
        raise ExtError(503, "not_wired", tr("VISTA 服务没有等待中枢", "The VISTA service has no wait hub"), missing=["vista.hub"])
    caller = caller_of(request)
    try:
        ans = await hub.wait(body.conditions, timeout_s=body.timeout_s, since=body.since,
                             disconnected=request.is_disconnected)
    except ConditionError as exc:
        raise ExtError(422, "invalid_condition", str(exc)) from None
    except CursorError as exc:
        raise ExtError(422, "invalid_cursor", str(exc)) from None
    if ans.get("cancelled"):
        logger.info("ext-gateway: %s 的等待因客户端断开而提前结束（%.1f s）",
                    caller.thread_id, ans.get("waited_s") or 0.0)
        return ans
    try:
        obs = await _observation_after(svc, caller.session, list(ans.get("fired") or []))
    except Exception as exc:  # noqa: BLE001 — 触发了什么、游标到哪，比观察更不能丢
        logger.exception("ext-gateway: 等待之后取观察失败")
        ans["observation_error"] = f"{type(exc).__name__}: {exc}"[:500]
        return ans
    if obs is not None:
        ans["observation"] = obs
    return ans


__all__ = ["WaitBody", "router", "vista_service"]
