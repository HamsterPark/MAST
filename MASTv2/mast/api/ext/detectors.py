"""分析检测器（设计稿 ``docs/v2/architecture/mast7_vista.md`` §7）：agent 写一个 ``detect(frame)``，
MAST 在每一张新存档的扫描图 / 谱上跑它（在 pyexec 的独立解释器里，碰不到仪器、可杀、内存有界），
命中就经 ``mast_wait`` 的 ``detector`` 条件叫醒 agent。检测器**只看不动**：要动仪器仍然走 act。

端点只是当前工作区的 :class:`mast.vista.detectors.DetectorManager` 的薄壳：限额（同时启用 8 个、
单次 ≤ 120 s）、自动停用（连错 5 次 / 一小时命中 30 次以上）、唤醒熔断（全部检测器每小时
60 次）都在那里。
"""

from __future__ import annotations

from mast.i18n import tr
import ipaddress
import logging
import os
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mast.api import direct_exec
from mast.api.ext.common import ExtError, caller_of
from mast.api.ext.wait import vista_service
from mast.vista.detectors import (
    DEFAULT_RUNTIME_S,
    MAX_CODE_BYTES,
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    MAX_RUNTIME_S,
    MIN_RUNTIME_S,
    DetectorError,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vista"])


class DetectorCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_CHARS)
    code: str = Field(..., min_length=1, max_length=MAX_CODE_BYTES,
                      description="定义 def detect(frame) 的 Python 源码（≤ 64 KB）；返回假值 / True / "
                                  "{hit, message, value}")
    applies_to: list[Literal["scan", "spectrum"]] = Field(..., min_length=1, max_length=2)
    description: str = Field("", max_length=MAX_DESCRIPTION_CHARS)
    max_runtime_s: float = Field(DEFAULT_RUNTIME_S, ge=MIN_RUNTIME_S, le=MAX_RUNTIME_S,
                                 description="单次运行的墙钟上限（秒），含解释器启动")


class DetectorEnable(BaseModel):
    enabled: bool


class DetectorTestBody(BaseModel):
    frame: str = Field(..., min_length=1, max_length=64, description="帧 id，例如 t0007.s0")


def _manager(request: Request) -> tuple[Any, Any]:
    svc = vista_service(request)
    try:
        mgr = svc.detectors()
    except Exception as exc:  # noqa: BLE001
        raise ExtError(503, "not_wired", tr(f"检测器取不到：{type(exc).__name__}: {exc}", f"Cannot get the detectors: {type(exc).__name__}: {exc}"),
                       missing=["vista.detectors"]) from None
    if mgr is None:
        raise ExtError(503, "not_wired", tr("当前工作区没有检测器管理器", "The current workspace has no detector manager"), missing=["vista.detectors"])
    return svc, mgr


def _refused(exc: DetectorError) -> ExtError:
    return ExtError(exc.status, exc.code, exc.detail, **exc.extra)


#: 局域网模式下也允许远端建 / 试跑检测器（显式开关；默认只认本机）
ALLOW_REMOTE_ENV = "MAST_DETECTORS_ALLOW_REMOTE"


def _local_only(request: Request) -> None:
    """检测器代码 = 以 MAST 用户身份在这台电脑上跑任意 Python（pyexec 挡的是仪器端口和原始
    数据，挡不住读文件、起进程、联网）。本机回环的调用方本来就能做这些；局域网模式下，持有
    共享密码的任何人却会因此拿到远程执行代码的能力 —— 所以建与试跑默认只认本机，局域网要
    显式打开 ``MAST_DETECTORS_ALLOW_REMOTE=1``。启用 / 停用 / 删除 / 查看不受限。"""
    if os.environ.get(ALLOW_REMOTE_ENV, "").strip() == "1":
        return
    host = str(getattr(request.client, "host", "") or "").strip().strip("[]").lower()
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise ExtError(403, "loopback_only",
                       tr("检测器的代码以 MAST 用户身份在仪器电脑上运行，只接受本机建立 / 试跑；"
                          f"局域网使用需要操作员设置 {ALLOW_REMOTE_ENV}=1。",
                          "Detector code runs on the instrument computer as the MAST user, so it can only "
                          "be created / trial-run from this machine; LAN use requires the operator to set "
                          f"{ALLOW_REMOTE_ENV}=1."))


@router.post("/detectors", status_code=201)
def create_detector(body: DetectorCreate, request: Request):
    """登记一个检测器（立即启用）。同名同代码重发回原检测器（200，``existing: true``）；
    同名不同代码 409 —— 先删再建。代码只在服务端编译检查，从不在服务进程里执行。"""
    _local_only(request)
    _, mgr = _manager(request)
    try:
        det = mgr.create(name=body.name, code=body.code, applies_to=list(body.applies_to),
                         description=body.description, max_runtime_s=body.max_runtime_s,
                         created_by=caller_of(request).agent_id)
    except DetectorError as exc:
        raise _refused(exc) from None
    except ValueError as exc:
        raise ExtError(422, "invalid_detector", str(exc)) from None
    if det.get("existing"):
        return JSONResponse(status_code=200, content=direct_exec.jsonable(det))
    return det


@router.get("/detectors")
def list_detectors(request: Request):
    """当前工作区的检测器（不含代码与命中记录）与限额、队列状态。"""
    _, mgr = _manager(request)
    rows = mgr.list()
    status = getattr(mgr, "status", None)
    return {"count": len(rows), "detectors": rows,
            "limits": status() if callable(status) else None}


@router.get("/detectors/{detector_id}")
def get_detector(detector_id: str, request: Request):
    """一个检测器：代码、计数与最近 50 次命中（``suppressed`` = 撞了唤醒熔断，记了但没叫醒）。"""
    _, mgr = _manager(request)
    det = mgr.get(detector_id)
    if det is None:
        raise ExtError(404, "unknown_detector", tr(f"当前工作区没有检测器 {detector_id!r}", f"The current workspace has no detector {detector_id!r}"))
    return det


@router.post("/detectors/{detector_id}/enable")
def enable_detector(detector_id: str, body: DetectorEnable, request: Request):
    """启用 / 停用。重新启用会清掉自动停用的原因、连错计数与这一小时的命中窗口。"""
    _, mgr = _manager(request)
    try:
        return mgr.set_enabled(detector_id, body.enabled)
    except DetectorError as exc:
        raise _refused(exc) from None


@router.delete("/detectors/{detector_id}")
def delete_detector(detector_id: str, request: Request):
    _, mgr = _manager(request)
    if not mgr.delete(detector_id):
        raise ExtError(404, "unknown_detector", tr(f"当前工作区没有检测器 {detector_id!r}", f"The current workspace has no detector {detector_id!r}"))
    return {"deleted": detector_id}


@router.post("/detectors/{detector_id}/test")
def test_detector(detector_id: str, body: DetectorTestBody, request: Request):
    """在一张已存档的帧上同步跑一次（停用的也能跑），回结果与输出尾巴。开发用：不计数、不记命中、
    不叫醒任何人。"""
    _local_only(request)
    svc, mgr = _manager(request)
    try:
        archive = svc.workspace().archive
    except Exception as exc:  # noqa: BLE001
        raise ExtError(503, "not_wired", tr(f"当前工作区的帧存档取不到：{type(exc).__name__}: {exc}", f"Cannot get the frame archive of the current workspace: {type(exc).__name__}: {exc}"),
                       missing=["vista.workspace"]) from None
    try:
        return mgr.test(detector_id, archive, body.frame)
    except DetectorError as exc:
        raise _refused(exc) from None


__all__ = ["DetectorCreate", "DetectorEnable", "DetectorTestBody", "router"]
